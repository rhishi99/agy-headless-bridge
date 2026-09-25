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

# CLI exit status when agy's quota is spent (EX_TEMPFAIL: retry later), so a
# caller script can tell "wait / rotate account" apart from a real failure.
EXIT_QUOTA = 75

# Pty geometry. Very wide so agy never hard-wraps: at 200 cols a wrap landed
# mid-word ("handl e the") once callers re-joined lines. Tall to avoid paging.
PTY_COLS = 2000
PTY_ROWS = 50

# Windows: once the reader hits EOF, how long to wait for the child to report
# its exit status before giving up on it (status then reads as unknown).
_EXIT_DRAIN_SECONDS = 2.0

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
    """Strip ANSI escapes, spinner repaints, and TUI chrome from agy output.

    Only lines that actually carried box-drawing/spinner glyphs are treated as
    decorative (their glyphs are removed, then the remainder is trimmed, and if
    nothing's left the line is dropped). Plain lines — including real code
    indentation and genuinely blank lines — pass through untouched aside from
    trailing whitespace, so returned code stays syntactically valid.
    """
    text = _strip_ansi(raw)
    text = _collapse_carriage_returns(text)
    # Drop remaining control chars except tab/newline.
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 0x20)
    cleaned = []
    for line in text.split("\n"):
        had_chrome = any(c in _SPINNER for c in line)
        without_chrome = "".join(c for c in line if c not in _SPINNER)
        if had_chrome:
            bare = without_chrome.strip()
            if not bare:
                continue  # pure decoration (border/spinner) — drop the line
            cleaned.append(bare)
        else:
            cleaned.append(without_chrome.rstrip())
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


class AgyExitError(RuntimeError):
    """Raised when agy exits with a non-zero status (bad --model, auth, crash).

    `.returncode` is agy's exit status; `.output` is whatever cleaned text it
    printed, which usually explains the failure.
    """

    def __init__(self, message: str, returncode: int, output: str = "") -> None:
        super().__init__(message)
        self.returncode = returncode
        self.output = output


class AgyQuotaError(AgyExitError):
    """agy failed because the account's model quota / rate limit is spent.

    `.reset_seconds` is parsed from agy's message ("resets in 4 hours and 50
    minutes") when present, else None. Retrying before then only burns calls.
    """

    def __init__(
        self, message: str, returncode: int, output: str = "",
        reset_seconds: int | None = None,
    ) -> None:
        super().__init__(message, returncode, output)
        self.reset_seconds = reset_seconds


_QUOTA_RE = re.compile(
    r"quota|rate[ _-]?limit|RESOURCE_EXHAUSTED|(?<!\d)429(?!\d)"
    r"|too many requests|exhausted",
    re.IGNORECASE,
)
# "4 hours", "28 mins", "30s", "4h50m" -> (number, first letter of unit).
_DURATION_RE = re.compile(
    r"(\d+)\s*(h|m|s)(?:ours?|rs?|inutes?|ins?|econds?|ecs?)?(?![a-z])"
)
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


def parse_reset_seconds(text: str) -> int | None:
    """Seconds until quota resets, from prose like 'resets in 4 hours and 50
    minutes' / 'retry after ~28 minutes' / 'in 30s'. None if no duration found."""
    hits: dict[str, int] = {}
    for n, unit in _DURATION_RE.findall(text.lower()):
        hits.setdefault(unit, int(n))  # first mention of each unit wins
    return sum(n * _UNIT_SECONDS[u] for u, n in hits.items()) if hits else None


def _check_exit(output: str, returncode: int | None) -> str:
    """Return output on success; raise AgyQuotaError / AgyExitError on failure.

    Quota is only inferred on a non-zero exit: matching the words on a
    successful run would misfire on any answer that merely discusses 429s.
    """
    if not returncode:  # 0, or None when the status is unknowable
        return output
    if _QUOTA_RE.search(output):
        raise AgyQuotaError(
            f"agy quota / rate limit hit (exit {returncode})",
            returncode, output, parse_reset_seconds(output),
        )
    raise AgyExitError(f"agy exited with status {returncode}", returncode, output)


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
) -> tuple[str, int | None]:
    try:
        from winpty import PtyProcess  # type: ignore
    except ImportError as exc:  # pragma: no cover - env-specific
        raise RuntimeError(
            "pywinpty is required on Windows. Install: pip install pywinpty"
        ) from exc

    # NB: ConPTY batches output and may not surface a partial (un-terminated)
    # line until the child exits, so `.partial` on a Windows timeout is
    # best-effort — it holds whatever ConPTY had already flushed, often empty.
    proc = PtyProcess.spawn(argv, dimensions=(PTY_ROWS, PTY_COLS))
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
        if not proc.isalive():
            # Child already exited. pywinpty doesn't reliably raise EOFError on
            # a silent (no-output) exit, so the reader thread's proc.read() can
            # block forever with `done` never set. Don't wait on it — read the
            # exit status, force it closed and return whatever was captured.
            rc = _exitstatus(proc)
            _terminate_windows(proc, t)
            return clean("".join(chunks)), rc
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

    # Reader hit EOF, so the child is exiting; give it a moment to report status.
    deadline = time.monotonic() + _EXIT_DRAIN_SECONDS
    while proc.isalive() and time.monotonic() < deadline:
        time.sleep(0.05)
    return clean("".join(chunks)), _exitstatus(proc)


def _exitstatus(proc) -> int | None:
    """agy's exit code, or None if the child is still alive."""
    return None if proc.isalive() else proc.exitstatus


def _terminate_windows(proc, t: threading.Thread) -> None:
    try:
        proc.terminate(force=True)
    except Exception:
        pass
    t.join(5)


def _run_posix(
    argv: list[str], timeout: float, idle_timeout: float = DEFAULT_IDLE_TIMEOUT
) -> tuple[str, int | None]:
    import pty
    import select
    import subprocess

    master_fd, slave_fd = pty.openpty()
    # Set a wide window on the pty itself (what tty-aware code queries) and
    # mirror it in env, so agy doesn't hard-wrap its answer.
    import fcntl
    import struct
    import termios

    fcntl.ioctl(
        slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0)
    )
    env = {
        **os.environ, "COLUMNS": str(PTY_COLS), "LINES": str(PTY_ROWS),
        "TERM": "xterm-256color",
    }
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
    return cleaned, proc.returncode


# --- public API ------------------------------------------------------------


def _pty_run(
    argv: list[str], timeout: float, idle_timeout: float = DEFAULT_IDLE_TIMEOUT
) -> str:
    """Spawn argv attached to a fresh pty; return its cleaned stdout.

    Platform-agnostic seam: `run()` calls this with the agy command, and the
    test suite calls it with a stub command to exercise the real pty machinery
    without needing `agy` installed.

    Raises AgyTimeoutError (carrying partial output) on idle or hard timeout,
    AgyQuotaError / AgyExitError (carrying output) on a non-zero exit.
    """
    runner = _run_windows if sys.platform == "win32" else _run_posix
    output, returncode = runner(argv, timeout, idle_timeout)
    return _check_exit(output, returncode)


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
    skip_permissions: bool = False,
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
    if skip_permissions:
        # Headless agy can't answer tool-permission prompts: without this it
        # silently declines every file write / command and still exits 0.
        argv.append("--dangerously-skip-permissions")
    # Tell agy to give up ~15s before our pty hard-kills it, so it can emit a
    # clean message instead of being severed mid-write. Must stay strictly
    # below `timeout`, or agy's own deadline can equal/exceed ours and the
    # hard-kill severs it mid-write anyway — the exact thing this margin
    # exists to prevent (bites callers who pass a small custom --timeout).
    inner = max(5, int(timeout) - 15)
    inner = max(1, min(inner, int(timeout) - 1))
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
    skip_permissions: bool = False,
) -> str:
    """
    Run `agy -p <prompt>` through a fresh pty and return its cleaned stdout.

    `add_dirs` are passed as `--add-dir` so agy operates on the caller's repo
    (essential for coding delegation). `model` maps to `--model`.

    `timeout` is the hard wall (absolute ceiling); `idle_timeout` ends a run that
    has gone silent. A long-but-active task survives the wall; a stalled one dies
    at the idle bound.

    `skip_permissions` passes `--dangerously-skip-permissions`, which headless
    agy needs to actually edit files or run commands (it cannot answer approval
    prompts). It lets agy act unattended in `add_dirs` — opt in deliberately.

    Raises AgyNotFoundError if `agy` can't be located, AgyTimeoutError (with
    `.partial`) on timeout, AgyQuotaError (with `.reset_seconds`) when the
    quota is spent, AgyExitError (with `.returncode`, `.output`) on any other
    non-zero exit. Returns "" if agy genuinely emitted nothing.
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
        skip_permissions=skip_permissions,
    )
    return _pty_run(argv, timeout, idle_timeout)


def main(argv: list[str] | None = None) -> int:
    import argparse

    argv = argv if argv is not None else sys.argv[1:]
    force_utf8_stdio()
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
    parser.add_argument(
        "--skip-permissions", action="store_true",
        help="pass --dangerously-skip-permissions so agy can edit files / run "
             "commands unattended (headless agy otherwise declines silently)",
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
            skip_permissions=args.skip_permissions,
        )
    except AgyNotFoundError as exc:
        sys.stderr.write(f"[agy-bridge] {exc}\n")
        return 127
    except AgyExitError as exc:
        if exc.output:
            print(exc.output)
        if isinstance(exc, AgyQuotaError):
            when = f"; resets in ~{exc.reset_seconds}s" if exc.reset_seconds else ""
            sys.stderr.write(f"[agy-bridge] {exc}{when}\n")
            return EXIT_QUOTA
        sys.stderr.write(f"[agy-bridge] {exc}\n")
        # Pass agy's status through; a signal death (negative on POSIX) or an
        # out-of-range Windows code collapses to a generic 1.
        return exc.returncode if 0 < exc.returncode < 256 else 1
    except AgyTimeoutError as exc:
        if exc.partial:
            print(exc.partial)  # surface partial work instead of dropping it
            note = "partial output above"
        else:
            # Common on Windows: ConPTY batches output and flushes nothing
            # before the kill, so there is no partial to show. Don't claim there
            # is one.
            note = "no partial output captured (none flushed before kill)"
        sys.stderr.write(f"[agy-bridge] {exc}; {note}; resume with 'agy -c'\n")
        return 1
    except TimeoutError as exc:  # pragma: no cover - defensive
        sys.stderr.write(f"[agy-bridge] {exc}\n")
        return 1
    if not output:
        sys.stderr.write("[agy-bridge] no output captured from agy\n")
        return 1
    print(output)
    return 0


def force_utf8_stdio() -> None:
    """Windows pipes default to the ANSI codepage (cp1252): printing an answer
    containing e.g. '₹' or an emoji raised UnicodeEncodeError and lost the
    whole (possibly many-minute) result. Force UTF-8 on all three std streams
    (stdin too, for the MCP server, which reads UTF-8 JSON-RPC)."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover - replaced stream
            pass


if __name__ == "__main__":
    raise SystemExit(main())
