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

DIRECT MODE (agy >= 1.2.11)
---------------------------
Newer agy prints to a pipe, so no pty is needed. When `agy --version` reports
>= MIN_DIRECT_VERSION the bridge spawns agy with plain pipes and
`--output-format json`, which yields the reply plus status, error text, token
usage and a conversation id for resuming. Older agy keeps the pty path. Force
either with $AGY_BRIDGE_MODE=direct|pty.

Public API
----------
    from agy_headless_bridge.bridge import run, run_result
    text = run("reply with exactly: OK")
    res = run_result("...")  # .text, .conversation_id, .usage, .denied
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ._guard import add_guard_arguments, guarded_command


def check_seconds(value, name: str = "timeout") -> float:
    """A timeout as a finite float >= 1 s. Accepts numeric strings (some MCP
    clients send "600"); rejects bool, NaN/inf, zero and negatives, which used
    to turn into an instant kill or a never-firing timer."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number of seconds, got {value!r}")
    try:
        secs = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number of seconds, got {value!r}") from None
    if not math.isfinite(secs) or secs < 1:
        raise ValueError(f"{name} must be a finite number >= 1, got {value!r}")
    return secs


def _env_seconds(name: str, default: float) -> float:
    """Read a timeout env var; a bad value warns and falls back instead of
    crashing the import (and with it the whole MCP server) at startup."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return check_seconds(raw, name)
    except ValueError as exc:
        sys.stderr.write(f"[agy-bridge] ignoring {exc}; using {default:.0f}\n")
        return default


# Hard ceiling (absolute wall). A real coding task — file edits + a test run —
# can run many minutes; this is only the backstop, not the normal stop signal.
# The idle timer below is what actually ends a *stalled* run. Override with
# $AGY_BRIDGE_TIMEOUT.
DEFAULT_TIMEOUT = _env_seconds("AGY_BRIDGE_TIMEOUT", 900.0)

# Idle (inactivity) timeout: kill only after agy has emitted NOTHING for this
# many seconds. Reset on every chunk read, so a task that keeps streaming output
# stays alive regardless of total elapsed time, while a truly hung agy dies fast.
# This is the in-process "is it still printing?" check — the caller need not
# poll. Override with $AGY_BRIDGE_IDLE_TIMEOUT.
# 300, not 120: `agy -p` is silent while it thinks, and a gemini-3.8-flash web
# research answer took 218 s with no output in between (measured 25 Oct 2026).
# A caller that passes its own `timeout` (and no idle bound) gets idle = timeout,
# see run(): a longer budget is useless if the idle timer still fires first.
DEFAULT_IDLE_TIMEOUT = _env_seconds("AGY_BRIDGE_IDLE_TIMEOUT", 300.0)

# CLI exit status when agy's quota is spent (EX_TEMPFAIL: retry later), so a
# caller script can tell "wait / rotate account" apart from a real failure.
EXIT_QUOTA = 75

# Pty geometry. Very wide so agy never hard-wraps: at 200 cols a wrap landed
# mid-word ("handl e the") once callers re-joined lines. Tall to avoid paging.
PTY_COLS = 2000
PTY_ROWS = 50

# Windows: after the child exits, how long to let the reader drain before the
# pty is force-closed, and (after reader EOF) how long to wait for the child's
# exit status before it reads as unknown.
_EXIT_DRAIN_SECONDS = 2.0
_PROGRESS_INTERVAL = 5.0
_MODEL_CACHE_TTL = 3600
_MODEL_ALIASES = re.compile(r"(flash|pro)(?:-(low|medium|high|xhigh))?$")
_CATALOG_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*$")
_GEMINI_ID = re.compile(r"gemini-(\d+(?:\.\d+)*)-(flash|pro)(?:-(low|medium|high|xhigh))?$")
_model_cache: dict[tuple[str, ...], tuple[float, dict[str, str]]] = {}
_model_lock = threading.Lock()
_version_cache: dict[tuple[str, ...], tuple[int, ...]] = {}

# First agy that prints to a pipe (upstream bug #76 gone; checked on 1.2.11 and
# 1.5.0). From here on the bridge skips the pty and asks for JSON output.
MIN_DIRECT_VERSION = (1, 2, 11)
MODE_ENV = "AGY_BRIDGE_MODE"  # direct | pty; anything else = by version

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


def list_models(path: str, prefix: tuple[str, ...] = (), *,
                cancel: threading.Event | None = None) -> dict[str, str]:
    """Read the AGY model catalog, caching successful results for one hour."""
    key = (*prefix, os.path.normcase(os.path.abspath(path)))
    with _model_lock:
        cached = _model_cache.get(key)
        if cached and time.monotonic() < cached[0]:
            return cached[1].copy()
    try:
        stdout, stderr, status = _probe([*prefix, path, "models"], 20, cancel)
    except (subprocess.TimeoutExpired, AgyTimeoutError):
        raise AgyTimeoutError("agy models exceeded its 20s timeout") from None
    except OSError as exc:
        raise AgyNotFoundError(f"could not run agy models: {exc}") from exc
    _check_exit(clean(stdout + "\n" + stderr), status)
    models = {}
    for line in _strip_ansi(stdout).splitlines():
        model_id, tab, name = line.strip().partition("\t")
        if tab and name.strip() and _CATALOG_ID.fullmatch(model_id):
            models[model_id] = name.strip()
    if not models:
        raise ValueError("agy models returned an empty or unrecognized model catalog")
    with _model_lock:
        _model_cache[key] = time.monotonic() + _MODEL_CACHE_TTL, models
    return models.copy()


def resolve_model(path: str, model: str | None, prefix: tuple[str, ...] = (), *,
                  cancel: threading.Event | None = None) -> str | None:
    alias = _MODEL_ALIASES.fullmatch(model) if isinstance(model, str) else None
    if alias is None:
        return model
    models = list_models(path, prefix, cancel=cancel)
    family, tier = alias.groups()
    candidates = []
    for model_id in models:
        match = _GEMINI_ID.fullmatch(model_id)
        if match is None:
            continue
        version, model_family, model_tier = match.groups()
        if family == model_family and (model_tier == (tier or "high") or
                                       (tier is None and model_tier is None)):
            candidates.append((tuple(map(int, version.split("."))), bool(model_tier), model_id))
    if not candidates:
        raise ValueError(f"model alias {model!r} is unavailable; run 'agy models' and pass a model ID")
    return max(candidates)[2]


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
    r"|too many requests|exhausted your|usage limit|out of credits",
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


class AgyCancelledError(RuntimeError):
    """The caller cancelled the active run."""


@dataclass
class _RunControl:
    cancel: threading.Event | None
    on_progress: Callable[[float], None] | None
    on_status: Callable[[float, str], None] | None = None
    job: object = None
    pid: int | None = None

    def check(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise AgyCancelledError("agy run cancelled")

    def wrap(self, argv: list[str]) -> list[str]:
        if self.cancel is not None and sys.platform == "win32":
            from ._windows_job import WindowsJob

            self.job = WindowsJob()
            return self.job.wrap(argv)
        return argv

    def stop_tree(self) -> None:
        if self.job is not None:
            self.job.close()
        elif self.pid is not None:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.pid = None


# --- runners ---------------------------------------------------------------


class _Liveness:
    """When agy last showed life: output on its stdout, or (heartbeat mode) a
    change to the progress file it appends to. `agy -p` is silent until its
    answer is ready, so the file is the only signal a long run is not hung."""

    def __init__(self, progress: str | None = None, control: _RunControl | None = None) -> None:
        self.progress = progress
        self.control = control
        self.start = self.last = self.last_tick = time.monotonic()
        self.last_step = 0.0
        self._sig = self._stat()

    @property
    def poll_interval(self) -> float:
        return 0.2 if self.control else 1.0

    def _stat(self):
        if not self.progress:
            return None
        try:
            st = os.stat(self.progress)
        except OSError:
            return None
        return st.st_size, st.st_mtime_ns

    def bump(self) -> None:
        self.last = time.monotonic()

    def idle(self) -> float:
        now = time.monotonic()
        if self.control:
            self.control.check()
            if now - self.last_tick >= _PROGRESS_INTERVAL:
                if self.control.on_status:
                    message = "AGY activity observed" if self.last_step > self.last_tick else "Elapsed time"
                    self.control.on_status(now - self.start, message)
                if self.control.on_progress:
                    self.control.on_progress(now - self.start)
                self.last_tick = now
        sig = self._stat()
        if sig != self._sig:
            self._sig = sig
            self.bump()
        return time.monotonic() - self.last


_CAPTURE_LIMIT_MARKER = "\n[bridge: capture limit exceeded]\n"
_STREAM_LINE_LIMIT = 1024 * 1024
_MIN_STREAM_VERSION = (1, 5, 18)


def _stream_results(stdout, live):
    """Keep the terminal frame and observe steps without retaining their payloads."""
    received_result, error, oversized = False, "", False
    while line := stdout.readline(_STREAM_LINE_LIMIT):
        live.bump()
        complete = line.endswith(b"\n") or len(line) < _STREAM_LINE_LIMIT
        if oversized or not complete:
            oversized = not complete
            continue
        try:
            frame = json.loads(line)
        except (ValueError, RecursionError):
            if not error and line.lower().startswith(b"error:"):
                error = line[:600].decode("utf-8", errors="replace").strip()
            continue
        if not isinstance(frame, dict):
            continue
        event = frame.get("event")
        if event == "result" and isinstance(frame.get("result"), dict):
            received_result = True
            yield json.dumps(frame["result"])
        elif event == "error" and isinstance(frame.get("error"), dict):
            error = str(frame["error"].get("message", ""))[:600]
        elif event == "step_update" and isinstance(frame.get("step_update"), dict):
            step = frame["step_update"]
            if step.get("state") in ("ACTIVE", "DONE") and step.get("step_type") in ("tool", "agent_response"):
                live.last_step = time.monotonic()
    if not received_result:
        yield json.dumps({"status": "ERROR", "error": error or "AGY stream ended without a result."})


def _run_direct(
    argv: list[str], timeout: float, idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    progress: str | None = None,
    *, control: _RunControl | None = None, cwd: str | None = None,
    capture_limit: int | None = None, stream: bool = False, env: dict[str, str] | None = None,
) -> tuple[str, int | None]:
    """Spawn agy on plain pipes (agy >= MIN_DIRECT_VERSION). Returns its raw
    stdout+stderr, not clean()ed: the JSON reply may hold box-drawing glyphs
    that clean() would strip as TUI chrome."""
    group = bool(control and control.cancel is not None and sys.platform != "win32")
    proc = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=group, env=env,
        **({"cwd": cwd} if cwd is not None else {}),
    )
    if group:
        control.pid = proc.pid
    chunks: list[bytes] = []
    head, tail = bytearray(), bytearray()
    live = _Liveness(progress, control)
    streamed = ""

    def _reader() -> None:
        nonlocal streamed
        if stream:
            for streamed in _stream_results(proc.stdout, live):
                pass
            return
        for data in iter(lambda: proc.stdout.read1(4096), b""):
            if capture_limit is None:
                chunks.append(data)
            else:
                remaining = capture_limit - len(head)
                head.extend(data[:remaining])
                tail.extend(data[remaining:])
                if len(tail) > capture_limit:
                    del tail[:-capture_limit]
            live.bump()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    def text() -> str:
        if stream:
            return streamed
        if capture_limit is None:
            data = b"".join(chunks)
        elif tail:
            data = bytes(head) + _CAPTURE_LIMIT_MARKER.encode("ascii") + bytes(tail)
        else:
            data = bytes(head)
        return data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    start = time.monotonic()
    try:
        while True:
            try:
                proc.wait(live.poll_interval)
                break
            except subprocess.TimeoutExpired:
                pass
            if live.idle() > idle_timeout:
                why = f"agy idle (no output) for {idle_timeout:.0f}s"
            elif time.monotonic() - start > timeout:
                why = f"agy exceeded hard timeout {timeout:.0f}s"
            else:
                continue
            raise AgyTimeoutError(why)
    except BaseException as exc:
        if control and control.cancel is not None:
            control.stop_tree()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        t.join(5)
        if isinstance(exc, AgyTimeoutError):
            exc.partial = "" if stream else (clean(text()) if capture_limit is None else text())
        raise
    # Bounded: a grandchild agy spawned can hold the pipe open after agy exits.
    t.join(_EXIT_DRAIN_SECONDS)
    return text(), proc.returncode


def _run_windows(
    argv: list[str], timeout: float, idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    progress: str | None = None,
    *, control: _RunControl | None = None, cwd: str | None = None,
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
    proc = PtyProcess.spawn(argv, dimensions=(PTY_ROWS, PTY_COLS), cwd=cwd)
    chunks: list[str] = []
    # The reader bumps this on every chunk; the main loop polls it to detect a
    # stall without blocking on the read itself.
    live = _Liveness(progress, control)
    done = threading.Event()

    def _reader() -> None:
        try:
            while True:
                data = proc.read(4096)
                if data:
                    chunks.append(data)
                    live.bump()
                elif not proc.isalive():
                    break
        except EOFError:
            pass
        finally:
            done.set()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()

    start = time.monotonic()
    while not done.wait(live.poll_interval):
        if not proc.isalive():
            # Child already exited. Give a lagging reader a bounded window to
            # drain what's still in flight: closing the pty under it dropped
            # the answer's tail (seen under load in the test suite). Bounded,
            # because pywinpty doesn't reliably raise EOFError on a silent
            # exit, so read() can block forever with `done` never set.
            done.wait(_EXIT_DRAIN_SECONDS)
            rc = _exitstatus(proc)
            _terminate_windows(proc, t)
            return clean("".join(chunks)), rc
        now = time.monotonic()
        try:
            idle = live.idle()
        except BaseException:
            if control:
                control.stop_tree()
            _terminate_windows(proc, t)
            raise
        if idle > idle_timeout:
            if control:
                control.stop_tree()
            _terminate_windows(proc, t)
            raise AgyTimeoutError(
                f"agy idle (no output) for {idle_timeout:.0f}s", clean("".join(chunks))
            )
        if now - start > timeout:
            if control:
                control.stop_tree()
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
    argv: list[str], timeout: float, idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    progress: str | None = None,
    *, control: _RunControl | None = None, cwd: str | None = None,
) -> tuple[str, int | None]:
    import pty
    import select

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
    group = bool(control and control.cancel is not None)
    try:
        proc = subprocess.Popen(
            argv,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            env=env,
            cwd=cwd,
            start_new_session=group,
        )
        if group:
            control.pid = proc.pid
    except BaseException:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)  # parent keeps only the master end

    chunks: list[bytes] = []
    timed_out: AgyTimeoutError | None = None
    start = time.monotonic()
    live = _Liveness(progress, control)
    try:
        while True:
            now = time.monotonic()
            if live.idle() > idle_timeout:
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
            r, _, _ = select.select([master_fd], [], [], live.poll_interval)
            if not r:
                continue
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                break  # master closed: child exited
            if not data:
                break
            chunks.append(data)
            live.bump()  # progress resets the idle timer
    finally:
        if control and control.cancel is not None and control.cancel.is_set():
            control.stop_tree()
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
    argv: list[str], timeout: float, idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    progress: str | None = None,
    *, control: _RunControl | None = None, cwd: str | None = None,
) -> str:
    """Spawn argv attached to a fresh pty; return its cleaned stdout.

    Platform-agnostic seam: `run_result()` calls this on the pty path, and the
    test suite calls it with a stub command to exercise the real pty machinery
    without needing `agy` installed.

    Raises AgyTimeoutError (carrying partial output) on idle or hard timeout,
    AgyQuotaError / AgyExitError (carrying output) on a non-zero exit.
    """
    runner = _run_windows if sys.platform == "win32" else _run_posix
    kwargs = {"control": control} if control else {}
    if cwd is not None:
        kwargs["cwd"] = cwd
    return _check_exit(*runner(argv, timeout, idle_timeout, progress, **kwargs))


def _probe(argv, timeout, cancel):
    if cancel is None:
        result = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        return result.stdout, getattr(result, "stderr", "") or "", result.returncode
    control = _RunControl(cancel, None)
    control.check()
    try:
        output, status = _run_direct(control.wrap(argv), timeout, timeout, control=control)
        control.check()
        return output, "", status
    finally:
        control.stop_tree()


def _agy_version(path: str, prefix: tuple[str, ...] = (), *,
                 cancel: threading.Event | None = None) -> tuple[int, ...]:
    """agy's version as a tuple, () when unknown. Old agy prints nothing to a
    pipe (bug #76), which reads as unknown and keeps the pty path."""
    key = (*prefix, path)
    if key in _version_cache:
        return _version_cache[key]
    try:
        out, _, status = _probe([*prefix, path, "--version"], 30, cancel)
        if status:
            out = ""
    except (OSError, subprocess.SubprocessError):
        out = ""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
    version = tuple(int(n) for n in m.groups()) if m else ()
    _version_cache[key] = version
    return version


def _use_direct(path: str, prefix: tuple[str, ...] = (), *,
                cancel: threading.Event | None = None) -> bool:
    forced = os.environ.get(MODE_ENV, "").strip().lower()
    if forced in ("direct", "pty"):
        return forced == "direct"
    kwargs = {"cancel": cancel} if cancel is not None else {}
    return _agy_version(path, *([prefix] if prefix else []), **kwargs) >= MIN_DIRECT_VERSION


@dataclass
class AgyResult:
    """One agy reply. Only `text` is filled on the pty path; direct mode adds
    the rest from agy's JSON output."""

    text: str
    conversation_id: str | None = None  # pass back as conversation= to resume
    usage: dict | None = None  # input_tokens, output_tokens, thinking_tokens...
    denied: list[str] = field(default_factory=list)  # file writes agy was refused
    structured_output: dict | None = None  # populated with --json-schema on direct mode


# agy exits 0 when a permission rule blocks a write; the refusal only shows up
# in the reply text (live agy 1.4.11) or, on some builds, `denied_actions`.
_DENIED_RE = re.compile(
    r"Permission denied for (?:write_file|edit_file|replace)\([^)\n]*\)", re.I
)


def _denials(text: str, actions=None) -> list[str]:
    found = _DENIED_RE.findall(text)
    if isinstance(actions, list):
        found += [a if isinstance(a, str) else json.dumps(a) for a in actions]
    return list(dict.fromkeys(found))


def _parse_json(raw: str) -> dict | None:
    """The last line of `--output-format json` output that is agy's result."""
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and ("response" in obj or "status" in obj):
            return obj
    return None


def _finish(raw: str, returncode: int | None) -> AgyResult:
    """Turn direct-mode output into an AgyResult, or raise on a failed run."""
    obj = _parse_json(raw)
    if obj is None:  # agy printed no JSON (e.g. it died before answering)
        text = _check_exit(clean(raw), returncode)
        return AgyResult(text, denied=_denials(text))
    text = str(obj.get("response") or "").rstrip()
    if returncode or obj.get("status", "SUCCESS") != "SUCCESS":
        # agy's own error text ("invalid model ...", quota) is the useful part.
        _check_exit(str(obj.get("error") or text or raw), returncode or 1)
    return AgyResult(
        text,
        conversation_id=obj.get("conversation_id") or None,
        usage=obj.get("usage") if isinstance(obj.get("usage"), dict) else None,
        denied=_denials(text, obj.get("denied_actions")),
        structured_output=obj["structured_output"] if isinstance(obj.get("structured_output"), dict) else None,
    )


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
    mode: str | None = None,
    effort: str | None = None,
    conversation: str | None = None,
    output_json: bool = False,
) -> list[str]:
    """Assemble the agy argv. Split out so it is testable without spawning.

    `add_dirs` is the critical one: without `--add-dir`, `agy -p` runs in its
    own scratch workspace and never sees the caller's repo, so any delegated
    coding task silently does nothing. Pass the repo root to fix that.
    """
    argv = [path]
    for d in add_dirs or []:
        argv += ["--add-dir", d]
    for flag, value in (("--model", model), ("--mode", mode),
                        ("--effort", effort), ("--conversation", conversation)):
        if value:
            argv += [flag, value]
    if output_json:
        argv += ["--output-format", "json"]
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


# --- output cap: keep a caller model's context small -------------------------

# Appended to the prompt by `brief=True` callers, so the answer ends in a block
# cap_output() can return alone when the full reply is long.
BRIEF_INSTRUCTION = (
    "\n\nEnd your reply with a line that says exactly 'SUMMARY:' followed by at "
    "most 10 short lines. State the findings or answer themselves (the facts, "
    "numbers, names; not a description of your work), then files changed and "
    "anything left open. Put nothing after the summary."
)

# Last line that starts the summary block ("SUMMARY:", "## Summary", "**SUMMARY**").
_SUMMARY_RE = re.compile(r"(?im)^[ \t#*>_]*summary[*_]*[ \t]*(?::|$)")


def _save_full(text: str) -> str:
    """Write the full reply to a temp file; return its path."""
    folder = os.path.join(tempfile.gettempdir(), "agy-bridge")
    os.makedirs(folder, exist_ok=True)
    fd, path = tempfile.mkstemp(
        prefix=time.strftime("agy-%Y%m%d-%H%M%S-"), suffix=".txt", dir=folder
    )
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def cap_output(text: str, max_chars: int) -> str:
    """Return `text` whole when it fits in `max_chars` (0 = no cap). Otherwise
    return its last SUMMARY block, or failing that its tail (the answer comes
    last), cut to `max_chars`, plus a note naming the file with the full text."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    hits = list(_SUMMARY_RE.finditer(text))
    kept = text[hits[-1].start():].strip() if hits else ""
    how = "summary only"
    if not kept or len(kept) > max_chars:
        kept = text[-max_chars:]
        how = f"last {max_chars} chars"
    try:
        where = f"full text: {_save_full(text)}"
    except OSError as exc:
        where = f"full text not saved ({exc})"
    return f"{kept}\n\n[agy-bridge] reply was {len(text)} chars; showing {how}; {where}"


# Appended to the prompt by heartbeat=True: agy logs each step to a file the
# bridge watches, so a long silent run is told apart from a hung one.
HEARTBEAT_INSTRUCTION = (
    "\n\nWhile you work, append one short line to {path} after every step you "
    "finish (e.g. 'HH:MM done: <what> / next: <what>'). Write the first line "
    "within 2 minutes. Only append; never overwrite the file. It is how the "
    "caller knows you are still working."
)


def _tail(path: str | None, n: int = 5) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "\n".join(f.read().splitlines()[-n:])
    except (OSError, TypeError):
        return ""


def run_result(
    prompt: str,
    timeout: float | None = None,
    agy_path: str | None = None,
    *,
    add_dirs: list[str] | None = None,
    model: str | None = None,
    extra_args: list[str] | None = None,
    idle_timeout: float | None = None,
    skip_permissions: bool = False,
    mode: str | None = None,
    effort: str | None = None,
    conversation: str | None = None,
    heartbeat: bool = False,
    cancel: threading.Event | None = None,
    on_progress: Callable[[float], None] | None = None,
    on_status: Callable[[float, str], None] | None = None,
    cwd: str | None = None,
    allowed_roots: list[str] | None = None,
    deny_patterns: list[str] | None = None,
) -> AgyResult:
    """
    Run `agy -p <prompt>` headlessly and return an AgyResult.

    `add_dirs` are passed as `--add-dir` so agy operates on the caller's repo
    (essential for coding delegation). `model`, `mode` (plan | accept-edits |
    auto), `effort` (low ... max) and `conversation` (resume a conversation by
    the id a previous result returned) map to the agy flags of the same name.

    `timeout` is the hard wall (absolute ceiling, default DEFAULT_TIMEOUT);
    `idle_timeout` ends a run that has gone silent. Left unset, the idle bound is
    DEFAULT_IDLE_TIMEOUT, or `timeout` itself when the caller set one: `agy -p`
    prints nothing until its answer is ready, so a caller's longer budget must
    not still die at the default idle bound. Both must be finite and >= 1 s.

    `heartbeat` asks agy to append a line per step to a temp progress file and
    counts each change as life, so the idle bound stays DEFAULT_IDLE_TIMEOUT
    even with a long `timeout`: a stall dies in minutes, a working run lives to
    the hard wall. On timeout the last progress lines are added to `.partial`.

    Set `cancel` to stop the local process tree and raise AgyCancelledError.
    `on_progress` reports elapsed seconds independently of the idle timer.
    `on_status` adds a quiet activity message when a supported AGY emits steps.
    `cwd` sets the child working directory without changing the caller's process.
    `allowed_roots` and `deny_patterns` require a host isolation launcher.

    `skip_permissions` passes `--dangerously-skip-permissions`, which headless
    agy needs to actually edit files or run commands (it cannot answer approval
    prompts). It lets agy act unattended in `add_dirs` — opt in deliberately.

    Raises AgyNotFoundError if `agy` can't be located, AgyTimeoutError (with
    `.partial`) on timeout, AgyQuotaError (with `.reset_seconds`) when the
    quota is spent, AgyExitError (with `.returncode`, `.output`) on any other
    failure. `.text` is "" if agy genuinely emitted nothing.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    prefix, add_dirs = guarded_command(allowed_roots, deny_patterns, add_dirs)
    if prefix and heartbeat:
        raise ValueError("guarded execution cannot attach the heartbeat scratch directory")
    if heartbeat and mode == "plan":
        # Measured on agy 1.5.0: plan mode writes nothing, so the progress file
        # would stay empty and the short idle bound would kill a healthy run.
        raise ValueError("heartbeat needs file writes, which plan mode blocks")
    control = _RunControl(cancel, on_progress, on_status) if cancel is not None or on_progress or on_status else None
    if control:
        control.check()
    hard = DEFAULT_TIMEOUT if timeout is None else check_seconds(timeout)
    if idle_timeout is not None:
        idle = check_seconds(idle_timeout, "idle_timeout")
    else:
        idle = DEFAULT_IDLE_TIMEOUT if timeout is None or heartbeat else hard

    path = agy_path or find_agy()
    if not path:
        raise AgyNotFoundError(
            "agy binary not found. Set $AGY_PATH or install the Antigravity CLI: "
            "https://antigravity.google/cli"
        )

    model = resolve_model(path, model, prefix, cancel=cancel)
    probe_options = {"cancel": cancel} if cancel is not None else {}
    direct = _use_direct(path, *([prefix] if prefix else []), **probe_options)
    stream = bool(on_status and direct and not any(
        arg.split("=", 1)[0] == "--output-format" for arg in extra_args or []
    ) and _agy_version(path, *([prefix] if prefix else []), **probe_options) >= _MIN_STREAM_VERSION)
    if control:
        control.check()
    folder = progress = None
    if heartbeat:
        folder = tempfile.mkdtemp(prefix="agy-heartbeat-")
        progress = os.path.join(folder, "progress.log")
        open(progress, "w").close()
        add_dirs = [*(add_dirs or []), folder]
        prompt += HEARTBEAT_INSTRUCTION.format(path=progress.replace("\\", "/"))
    argv = [*prefix, *build_argv(
        path, prompt, add_dirs=add_dirs, model=model,
        timeout=hard, extra_args=["--output-format", "stream-json", *(extra_args or [])] if stream else extra_args,
        skip_permissions=skip_permissions, mode=mode, effort=effort,
        conversation=conversation, output_json=direct and not stream,
    )]
    try:
        runner_kwargs = {"control": control} if control else {}
        if cwd is not None:
            runner_kwargs["cwd"] = cwd
        if control:
            argv = control.wrap(argv)
        if direct:
            if stream:
                runner_kwargs["stream"] = True
            res = _finish(*_run_direct(argv, hard, idle, progress, **runner_kwargs))
        else:
            # progress only when set: keeps the 3-argument seam tests patch.
            if progress:
                runner_kwargs["progress"] = progress
            text = _pty_run(argv, hard, idle, **runner_kwargs)
            res = AgyResult(text, denied=_denials(text))
        if control:
            control.check()
    except AgyTimeoutError as exc:
        tail = _tail(progress)
        if tail:
            exc.partial = f"{exc.partial}\n\n[agy-bridge] last progress:\n{tail}".strip()
        raise
    finally:
        if control:
            control.stop_tree()
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
    return res


def run(prompt: str, timeout: float | None = None, agy_path: str | None = None,
        **kwargs) -> str:
    """run_result(...).text: the reply alone. Same arguments and errors."""
    return run_result(prompt, timeout, agy_path, **kwargs).text


def main(argv: list[str] | None = None) -> int:
    import argparse

    argv = argv if argv is not None else sys.argv[1:]
    force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="agy-bridge",
        description="Call the Antigravity CLI (agy) headlessly.",
    )
    parser.add_argument("prompt", nargs="+", help="prompt to send to agy")
    parser.add_argument(
        "--add-dir", action="append", default=[], metavar="DIR",
        help="add a directory to agy's workspace (repeatable). Pass your repo "
             "root here for coding tasks, else agy can't see your files.",
    )
    parser.add_argument("--model", default=None, help="agy --model to use")
    add_guard_arguments(parser)
    parser.add_argument(
        "--timeout", type=float, default=None,
        help=f"hard timeout (absolute ceiling) in seconds "
             f"(default {int(DEFAULT_TIMEOUT)})",
    )
    parser.add_argument(
        "--idle-timeout", type=float, default=None,
        help=f"kill agy after this many seconds of no output (default "
             f"{int(DEFAULT_IDLE_TIMEOUT)}, or --timeout when that is given)",
    )
    parser.add_argument(
        "--brief", action="store_true",
        help="ask agy to end with a SUMMARY: block; with --max-chars, print "
             "only that block when the reply is long",
    )
    parser.add_argument(
        "--max-chars", type=int, default=0, metavar="N",
        help="cap printed output at N chars; the full reply goes to a temp "
             "file named on the last line (default 0 = no cap)",
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
    parser.add_argument("--mode", choices=["plan", "accept-edits", "auto"],
                        default=None, help="agy --mode (plan = read-only)")
    parser.add_argument("--effort", default=None,
                        help="agy --effort: low|medium|high|xhigh|max")
    parser.add_argument(
        "--conversation", default=None, metavar="ID",
        help="resume a conversation by the id a previous run printed",
    )
    parser.add_argument(
        "--heartbeat", action="store_true",
        help="have agy log each step to a progress file; idle timeout then "
             "watches that file, so a long --timeout no longer hides a stall",
    )
    args = parser.parse_args(argv)
    prompt = " ".join(args.prompt)
    if args.brief:
        prompt += BRIEF_INSTRUCTION
    # Coding-shaped by default: inject cwd unless explicitly opted out or the
    # caller already named dirs. resolve_add_dirs keeps the policy in one place.
    add_dirs = resolve_add_dirs(args.add_dir, use_cwd_default=not args.no_workspace)
    try:
        res = run_result(
            prompt, timeout=args.timeout, idle_timeout=args.idle_timeout,
            add_dirs=add_dirs, model=args.model,
            skip_permissions=args.skip_permissions, mode=args.mode,
            effort=args.effort, conversation=args.conversation,
            heartbeat=args.heartbeat,
            **({"allowed_roots": args.allowed_roots, "deny_patterns": args.deny_patterns}
               if args.allowed_roots is not None or args.deny_patterns is not None else {}),
        )
    except AgyNotFoundError as exc:
        sys.stderr.write(f"[agy-bridge] {exc}\n")
        return 127
    except ValueError as exc:  # bad --timeout / --idle-timeout
        sys.stderr.write(f"[agy-bridge] {exc}\n")
        return 2
    except AgyExitError as exc:
        if exc.output:
            print(cap_output(exc.output, args.max_chars))
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
            print(cap_output(exc.partial, args.max_chars))  # keep partial work
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
    for note in result_notes(res):
        sys.stderr.write(f"[agy-bridge] {note}\n")
    if not res.text:
        sys.stderr.write("[agy-bridge] no output captured from agy\n")
        return 1
    print(cap_output(res.text, args.max_chars))
    return 0


def result_notes(res: AgyResult) -> list[str]:
    """Caller-facing notes on a successful run: refused writes, resume id."""
    notes = []
    if res.denied:
        notes.append(
            "WARNING: agy was refused file writes, so its edits may be missing: "
            + "; ".join(res.denied[:5])
        )
    if res.conversation_id:
        notes.append(f"conversation {res.conversation_id} (pass as conversation "
                     "/ --conversation to continue it)")
    return notes


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
