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

# Default matches agy's own `--print-timeout` (5m). A real coding task — file
# edits + a test run — easily needs minutes; the old 180s killed the pty before
# agy finished. Override with $AGY_BRIDGE_TIMEOUT.
DEFAULT_TIMEOUT = float(os.environ.get("AGY_BRIDGE_TIMEOUT", "300"))

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


# --- platform pty runners --------------------------------------------------


def _run_windows(argv: list[str], timeout: float) -> str:
    try:
        from winpty import PtyProcess  # type: ignore
    except ImportError as exc:  # pragma: no cover - env-specific
        raise RuntimeError(
            "pywinpty is required on Windows. Install: pip install pywinpty"
        ) from exc

    # Wide cols so agy does not hard-wrap; tall rows to avoid paging.
    proc = PtyProcess.spawn(argv, dimensions=(50, 200))
    chunks: list[str] = []

    def _reader() -> None:
        try:
            while True:
                data = proc.read(4096)
                if data:
                    chunks.append(data)
                elif not proc.isalive():
                    break
        except EOFError:
            pass

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        try:
            proc.terminate(force=True)
        except Exception:
            pass
        t.join(5)
        raise TimeoutError(f"process timed out after {timeout}s")

    return clean("".join(chunks))


def _run_posix(argv: list[str], timeout: float) -> str:
    import pty
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
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() > deadline:
                proc.kill()
                raise TimeoutError(f"process timed out after {timeout}s")
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                break  # master closed: child exited
            if not data:
                break
            chunks.append(data)
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    raw = b"".join(chunks).decode("utf-8", errors="replace")
    return clean(raw)


# --- public API ------------------------------------------------------------


def _pty_run(argv: list[str], timeout: float) -> str:
    """Spawn argv attached to a fresh pty; return its cleaned stdout.

    Platform-agnostic seam: `run()` calls this with the agy command, and the
    test suite calls it with a stub command to exercise the real pty machinery
    without needing `agy` installed.
    """
    if sys.platform == "win32":
        return _run_windows(argv, timeout)
    return _run_posix(argv, timeout)


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
) -> str:
    """
    Run `agy -p <prompt>` through a fresh pty and return its cleaned stdout.

    `add_dirs` are passed as `--add-dir` so agy operates on the caller's repo
    (essential for coding delegation). `model` maps to `--model`.

    Raises AgyNotFoundError if `agy` can't be located, TimeoutError on timeout.
    Returns "" if agy genuinely emitted nothing.
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
    return _pty_run(argv, timeout)


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
        help=f"hard timeout in seconds (default {int(DEFAULT_TIMEOUT)})",
    )
    args = parser.parse_args(argv)
    prompt = " ".join(args.prompt)
    try:
        output = run(
            prompt, timeout=args.timeout,
            add_dirs=args.add_dir, model=args.model,
        )
    except AgyNotFoundError as exc:
        sys.stderr.write(f"[agy-bridge] {exc}\n")
        return 127
    except TimeoutError as exc:
        sys.stderr.write(f"[agy-bridge] {exc}\n")
        return 1
    if not output:
        sys.stderr.write("[agy-bridge] no output captured from agy\n")
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
