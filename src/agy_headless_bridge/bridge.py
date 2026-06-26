#!/usr/bin/env python3
"""
agy_headless_bridge.bridge — Make the Google Antigravity CLI (`agy`) callable
headlessly (from any non-TTY context: a subprocess, a pipe, an MCP server,
Claude Code's Bash tool, CI).

WHY THIS EXISTS
---------------
`agy -p "<prompt>"` gates its stdout on `isatty()` (upstream bug #76). When
stdout is NOT attached to a real terminal it emits nothing and exits 0. So a
plain `subprocess.run(["agy", "-p", prompt])` returns an empty string — which
makes `agy` unusable as a delegate from any automated context.

The known community workaround, `winpty agy -p "..."`, requires a *pre-existing*
terminal, so it still fails from a subprocess.

THE FIX
-------
Allocate a *fresh* pseudo-terminal and spawn `agy` attached to it. `agy` then
sees a real tty on stdout and emits normally. We read the pty master, strip the
ANSI / TUI control noise, and return the clean model response.

  * Windows : ConPTY via the `pywinpty` library (`PtyProcess`). ConPTY creates a
              brand-new pty and does NOT require the parent process to already
              own a tty — so this works from any subprocess.
  * POSIX   : the stdlib `pty` module (`os.openpty` + `subprocess.Popen`).

Public API
----------
    from agy_headless_bridge.bridge import run
    text = run("reply with exactly: OK")
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time

# Hard ceiling (absolute wall). A real coding task — file edits + a test run —
# can run many minutes; this is only the backstop, not the normal stop signal.
# The idle timer below is what actually ends a *stalled* run. Override with
# $AGY_BRIDGE_TIMEOUT.
DEFAULT_TIMEOUT = float(os.environ.get("AGY_BRIDGE_TIMEOUT", "900"))

# Idle (inactivity) timeout: kill only after agy has emitted NOTHING for this
# many seconds. Reset on every chunk read, so a task that keeps streaming output
# stays alive regardless of total elapsed time, while a truly hung agy dies fast.
# This is the in-process "is it still printing?" check — the caller need not
# poll. Override with $AGY_BRIDGE_IDLE_TIMEOUT.
DEFAULT_IDLE_TIMEOUT = float(os.environ.get("AGY_BRIDGE_IDLE_TIMEOUT", "120"))

# --- ANSI / TUI noise stripping -------------------------------------------

# CSI sequences (colors, cursor moves), OSC sequences (window titles), lone esc.
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ANSI_OTHER = re.compile(r"\x1b[@-Z\\-_]")
# Box-drawing / spinner glyphs agy uses for its TUI chrome.
_SPINNER = set(
    "⠁⠂⠄⡀⢀⠠⠐⠈⣾⣽⣻⢿⡿⣟⣯⣷⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    "│─┌┐└┘├┤┬┴┼╭╮╰╯═║╔╗╚╝▌▐█▏▕"
)


def _strip_ansi(text: str) -> str:
    text = _ANSI_OSC.sub("", text)
    text = _ANSI_CSI.sub("", text)
    text = _ANSI_OTHER.sub("", text)
    return text


def _collapse_carriage_returns(text: str) -> str:
    """A spinner repaints one line via \\r. Keep only the final paint per line."""
    text = text.replace("\r\n", "\n")  # normalize CRLF first
    out_lines = []
    for line in text.split("\n"):
        # Each remaining \r overwrites from column 0; the last segment was visible.
        out_lines.append(line.split("\r")[-1])
    return "\n".join(out_lines)


def clean(raw: str) -> str:
    """Strip ANSI escapes, spinner repaints, and TUI chrome from agy output."""
    text = _strip_ansi(raw)
    text = _collapse_carriage_returns(text)
    # Drop remaining control chars except tab/newline.
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 0x20)
    cleaned = []
    for line in text.split("\n"):
        stripped = "".join(c for c in line if c not in _SPINNER).strip()
        if stripped:
            cleaned.append(stripped)
    return "\n".join(cleaned).strip()


# --- agy discovery ---------------------------------------------------------


def find_agy() -> str | None:
    """Locate the `agy` binary. Honors $AGY_PATH, then PATH, then OS defaults."""
    explicit = os.environ.get("AGY_PATH")
    if explicit and os.path.exists(explicit):
        return explicit

    found = shutil.which("agy") or shutil.which("agy.exe")
    if found:
        return found

    home = os.path.expanduser("~")
    if sys.platform == "win32":
        candidates = [
            os.path.join(home, "AppData", "Local", "agy", "bin", "agy.exe"),
            os.path.join(home, "AppData", "Roaming", "agy", "bin", "agy.exe"),
        ]
    else:
        candidates = [
            os.path.join(home, ".local", "bin", "agy"),
            "/opt/antigravity/bin/agy",
            "/usr/local/bin/agy",
        ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


class AgyNotFoundError(RuntimeError):
    pass


class AgyTimeoutError(TimeoutError):
    """Raised when agy is killed by the idle or hard timeout.

    Carries whatever cleaned stdout agy emitted before the kill, so the caller
    can surface partial work instead of dropping it. A parent can read `.partial`
    and decide to resume the session with `agy -c`.
    """

    def __init__(self, message: str, partial: str = "") -> None:
        super().__init__(message)
        self.partial = partial


# --- platform pty runners --------------------------------------------------


def _run_windows(
    argv: list[str], timeout: float, idle_timeout: float = DEFAULT_IDLE_TIMEOUT
) -> str:
    try:
        from winpty import PtyProcess  # type: ignore
    except ImportError as exc:  # pragma: no cover - env-specific
        raise RuntimeError(
            "pywinpty is required on Windows. Install: pip install pywinpty"
        ) from exc

    # Wide cols so agy does not hard-wrap; tall rows to avoid paging.
    # NB: ConPTY batches output and may not surface a partial (un-terminated)
    # line until the child exits, so `.partial` on a Windows timeout is
    # best-effort — it holds whatever ConPTY had already flushed, often empty.
    proc = PtyProcess.spawn(argv, dimensions=(50, 200))
    chunks: list[str] = []
    # A 1-slot mutable timestamp the reader bumps on every chunk; the main loop
    # polls it to detect a stall without blocking on the read itself.
    last_activity = [time.monotonic()]
    done = threading.Event()

    def _reader() -> None:
        try:
            while True:
                data = proc.read(4096)
                if data:
                    chunks.append(data)
                    last_activity[0] = time.monotonic()
                elif not proc.isalive():
                    break
        except EOFError:
            pass
        finally:
            done.set()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    start = time.monotonic()
    while not done.wait(1.0):  # poll in ~1s slices
        now = time.monotonic()
        if now - last_activity[0] > idle_timeout:
            _terminate_windows(proc, t)
            raise AgyTimeoutError(
                f"agy idle (no output) for {idle_timeout:.0f}s", clean("".join(chunks))
            )
        if now - start > timeout:
            _terminate_windows(proc, t)
            raise AgyTimeoutError(
                f"agy exceeded hard timeout {timeout:.0f}s", clean("".join(chunks))
            )

    return clean("".join(chunks))


def _terminate_windows(proc, t: threading.Thread) -> None:
    try:
        proc.terminate(force=True)
    except Exception:
        pass
    t.join(5)


def _run_posix(
    argv: list[str], timeout: float, idle_timeout: float = DEFAULT_IDLE_TIMEOUT
) -> str:
    import pty
    import select
    import subprocess

    master_fd, slave_fd = pty.openpty()
    # Hint a wide terminal so agy doesn't hard-wrap its answer.
    env = {**os.environ, "COLUMNS": "200", "LINES": "50", "TERM": "xterm-256color"}
    try:
        proc = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env=env,
        )
    finally:
        os.close(slave_fd)  # parent keeps only the master end

    chunks: list[bytes] = []
    timed_out: AgyTimeoutError | None = None
    start = last = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            if now - last > idle_timeout:
                proc.kill()
                timed_out = AgyTimeoutError(
                    f"agy idle (no output) for {idle_timeout:.0f}s", ""
                )
                break
            if now - start > timeout:
                proc.kill()
                timed_out = AgyTimeoutError(
                    f"agy exceeded hard timeout {timeout:.0f}s", ""
                )
                break
            # Poll so a stalled child can't block us forever on os.read.
            r, _, _ = select.select([master_fd], [], [], 1.0)
            if not r:
                continue
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                break  # master closed: child exited
            if not data:
                break
            chunks.append(data)
            last = time.monotonic()  # progress resets the idle timer
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    cleaned = clean(b"".join(chunks).decode("utf-8", errors="replace"))
    if timed_out is not None:
        timed_out.partial = cleaned  # carry whatever agy produced before the kill
        raise timed_out
    return cleaned


# --- public API ------------------------------------------------------------


def _pty_run(
    argv: list[str], timeout: float, idle_timeout: float = DEFAULT_IDLE_TIMEOUT
) -> str:
    """Spawn argv attached to a fresh pty; return its cleaned stdout.

    Platform-agnostic seam: `run()` calls this with the agy command, and the
    test suite calls it with a stub command to exercise the real pty machinery
    without needing `agy` installed.

    Raises AgyTimeoutError (carrying partial output) on idle or hard timeout.
    """
    if sys.platform == "win32":
        return _run_windows(argv, timeout, idle_timeout)
    return _run_posix(argv, timeout, idle_timeout)


def resolve_add_dirs(
    explicit: list[str] | None, *, use_cwd_default: bool
) -> list[str]:
    """Decide which directories agy should see, intent-aware.

    - Caller passed explicit dirs -> honour them verbatim (caller knows best).
    - Otherwise, coding-shaped calls (`use_cwd_default=True`) default to the
      current working dir so agy can actually see the repo — without this,
      `agy -p` runs blind in its scratch workspace and silently does nothing.
    - Research / Q&A calls (`use_cwd_default=False`) get NO workspace: feeding
      the repo there only wastes agy's context and can mislead it.
    """
    if explicit:
        return list(explicit)
    if use_cwd_default:
        return [os.getcwd()]
    return []


def build_argv(
    path: str,
    prompt: str,
    *,
    add_dirs: list[str] | None = None,
    model: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Assemble the agy argv. Split out so it is testable without spawning.

    `add_dirs` is the critical one: without `--add-dir`, `agy -p` runs in its
    own scratch workspace and never sees the caller's repo, so any delegated
    coding task silently does nothing. Pass the repo root to fix that.
    """
    argv = [path]
    for d in add_dirs or []:
        argv += ["--add-dir", d]
    if model:
        argv += ["--model", model]
    # Tell agy to give up ~15s before our pty hard-kills it, so it can emit a
    # clean message instead of being severed mid-write.
    inner = max(30, int(timeout) - 15)
    argv += ["--print-timeout", f"{inner}s"]
    argv += list(extra_args or [])
    argv += ["-p", prompt]
    return argv


def run(
    prompt: str,
    timeout: float = DEFAULT_TIMEOUT,
    agy_path: str | None = None,
    *,
    add_dirs: list[str] | None = None,
    model: str | None = None,
    extra_args: list[str] | None = None,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
) -> str:
    """
    Run `agy -p <prompt>` through a fresh pty and return its cleaned stdout.

    `add_dirs` are passed as `--add-dir` so agy operates on the caller's repo
    (essential for coding delegation). `model` maps to `--model`.

    `timeout` is the hard wall (absolute ceiling); `idle_timeout` ends a run that
    has gone silent. A long-but-active task survives the wall; a stalled one dies
    at the idle bound.

    Raises AgyNotFoundError if `agy` can't be located, AgyTimeoutError (with
    `.partial`) on timeout. Returns "" if agy genuinely emitted nothing.
    """
    if not prompt or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")

    path = agy_path or find_agy()
    if not path:
        raise AgyNotFoundError(
            "agy binary not found. Set $AGY_PATH or install the Antigravity CLI: "
            "https://antigravity.google/cli"
        )

    argv = build_argv(
        path, prompt, add_dirs=add_dirs, model=model,
        timeout=timeout, extra_args=extra_args,
    )
    return _pty_run(argv, timeout, idle_timeout)


def main(argv: list[str] | None = None) -> int:
    import argparse

    argv = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="agy-bridge",
        description="Call the Antigravity CLI (agy) headlessly via a pty.",
    )
    parser.add_argument("prompt", nargs="+", help="prompt to send to agy")
    parser.add_argument(
        "--add-dir", action="append", default=[], metavar="DIR",
        help="add a directory to agy's workspace (repeatable). Pass your repo "
             "root here for coding tasks, else agy can't see your files.",
    )
    parser.add_argument("--model", default=None, help="agy --model to use")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help=f"hard timeout (absolute ceiling) in seconds "
             f"(default {int(DEFAULT_TIMEOUT)})",
    )
    parser.add_argument(
        "--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT,
        help=f"kill agy after this many seconds of no output "
             f"(default {int(DEFAULT_IDLE_TIMEOUT)})",
    )
    parser.add_argument(
        "--no-workspace", action="store_true",
        help="do not auto-add the current directory to agy's workspace "
             "(use for research / Q&A that needs no repo context)",
    )
    args = parser.parse_args(argv)
    prompt = " ".join(args.prompt)
    # Coding-shaped by default: inject cwd unless explicitly opted out or the
    # caller already named dirs. resolve_add_dirs keeps the policy in one place.
    add_dirs = resolve_add_dirs(args.add_dir, use_cwd_default=not args.no_workspace)
    try:
        output = run(
            prompt, timeout=args.timeout, idle_timeout=args.idle_timeout,
            add_dirs=add_dirs, model=args.model,
        )
    except AgyNotFoundError as exc:
        sys.stderr.write(f"[agy-bridge] {exc}\n")
        return 127
    except AgyTimeoutError as exc:
        if exc.partial:
            print(exc.partial)  # surface partial work instead of dropping it
        sys.stderr.write(
            f"[agy-bridge] {exc}; partial output above; resume with 'agy -c'\n"
        )
        return 1
    except TimeoutError as exc:  # pragma: no cover - defensive
        sys.stderr.write(f"[agy-bridge] {exc}\n")
        return 1
    if not output:
        sys.stderr.write("[agy-bridge] no output captured from agy\n")
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
