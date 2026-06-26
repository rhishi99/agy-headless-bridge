"""
Smoke + unit tests for agy-headless-bridge.

The unit tests (clean / find_agy / arg validation) always run. The live smoke
test that actually invokes `agy` is skipped automatically when the binary is
not installed/authenticated, so CI without Antigravity still passes.
"""

import os
import sys
import textwrap

import pytest

from agy_headless_bridge import bridge


# --- pure-unit: output cleaning -------------------------------------------

def test_clean_strips_ansi_color():
    raw = "\x1b[32mhello\x1b[0m world"
    assert bridge.clean(raw) == "hello world"


def test_clean_collapses_spinner_repaint():
    # A spinner repaints the same line via \r; only the final paint is real.
    raw = "loading...\rloading done\n"
    assert bridge.clean(raw) == "loading done"


def test_clean_drops_tui_chrome_glyphs():
    raw = "╭─────╮\n│ answer: 42 │\n╰─────╯"
    assert bridge.clean(raw) == "answer: 42"


def test_clean_empty_returns_empty():
    assert bridge.clean("\x1b[0m\n  \n") == ""


# --- pure-unit: arg validation --------------------------------------------

def test_run_rejects_empty_prompt():
    with pytest.raises(ValueError):
        bridge.run("   ")


def test_find_agy_honors_explicit_env(tmp_path, monkeypatch):
    fake = tmp_path / ("agy.exe" if os.name == "nt" else "agy")
    fake.write_text("")
    monkeypatch.setenv("AGY_PATH", str(fake))
    assert bridge.find_agy() == str(fake)


def test_run_raises_when_agy_missing(monkeypatch):
    monkeypatch.setenv("AGY_PATH", "/nonexistent/path/to/agy")
    monkeypatch.setattr(bridge, "find_agy", lambda: None)
    with pytest.raises(bridge.AgyNotFoundError):
        bridge.run("hello")


# --- argv construction (the workspace-blindness bug had no coverage) ------

def test_build_argv_passes_add_dir_and_model():
    argv = bridge.build_argv(
        "agy", "do the thing",
        add_dirs=["/repo/a", "/repo/b"], model="pro", timeout=300,
    )
    # repo dirs must reach agy, else it runs blind in its scratch workspace
    assert argv.count("--add-dir") == 2
    assert "/repo/a" in argv and "/repo/b" in argv
    assert argv[argv.index("--model") + 1] == "pro"
    # inner print-timeout fires before our pty hard-kill
    assert "--print-timeout" in argv
    assert argv[argv.index("--print-timeout") + 1] == "285s"
    # prompt is last, behind -p
    assert argv[-2:] == ["-p", "do the thing"]


def test_run_forwards_add_dirs_to_pty(monkeypatch):
    captured = {}
    monkeypatch.setattr(bridge, "find_agy", lambda: "agy")
    monkeypatch.setattr(bridge, "_pty_run", lambda argv, *a, **k: captured.setdefault("argv", argv) or "ok")
    bridge.run("hello", add_dirs=["/my/repo"])
    assert "--add-dir" in captured["argv"]
    assert "/my/repo" in captured["argv"]


# --- pty mechanics (no agy needed; runs on every CI runner) ---------------

# A stand-in for `agy`: it prints its payload ONLY when stdout is a real tty —
# exactly the isatty() gate that makes agy go silent in a pipe (bug #76). If the
# bridge gives it a working pseudo-terminal, we get the payload back; if the pty
# machinery is broken on this platform, we get "".
_STUB = textwrap.dedent(
    """
    import os, sys
    if os.isatty(sys.stdout.fileno()):
        sys.stdout.write("STUB_OK")
        sys.stdout.flush()
    """
)


def test_pty_mechanics_with_isatty_stub(tmp_path):
    """Exercise the real pty path (ConPTY on Windows, os.openpty on POSIX).

    This is the platform-verification test: it proves the bridge hands the
    child a stdout that passes isatty(), and that we capture + clean the output
    — without requiring agy to be installed or authenticated.
    """
    stub = tmp_path / "isatty_stub.py"
    stub.write_text(_STUB)
    out = bridge._pty_run([sys.executable, str(stub)], timeout=60)
    assert "STUB_OK" in out


def test_pty_returns_empty_when_child_emits_nothing(tmp_path):
    stub = tmp_path / "silent_stub.py"
    stub.write_text("import sys; sys.exit(0)")
    out = bridge._pty_run([sys.executable, str(stub)], timeout=60)
    assert out == ""


# --- workspace policy (intent-aware: smart default + opt-out) -------------

def test_resolve_add_dirs_explicit_wins():
    # caller-named dirs are honoured verbatim, default ignored
    assert bridge.resolve_add_dirs(["/x", "/y"], use_cwd_default=True) == ["/x", "/y"]
    assert bridge.resolve_add_dirs(["/x"], use_cwd_default=False) == ["/x"]


def test_resolve_add_dirs_defaults_to_cwd_for_coding():
    assert bridge.resolve_add_dirs(None, use_cwd_default=True) == [os.getcwd()]
    assert bridge.resolve_add_dirs([], use_cwd_default=True) == [os.getcwd()]


def test_resolve_add_dirs_no_workspace_for_research():
    # research / Q&A get NO repo — feeding it only pollutes agy's context
    assert bridge.resolve_add_dirs(None, use_cwd_default=False) == []


def test_cli_no_workspace_suppresses_cwd(monkeypatch):
    captured = {}
    monkeypatch.setattr(bridge, "find_agy", lambda: "agy")
    monkeypatch.setattr(
        bridge, "_pty_run",
        lambda argv, *a, **k: captured.setdefault("argv", argv) or "out",
    )
    bridge.main(["--no-workspace", "what is 2+2"])
    assert "--add-dir" not in captured["argv"]


def test_cli_defaults_to_cwd_workspace(monkeypatch):
    captured = {}
    monkeypatch.setattr(bridge, "find_agy", lambda: "agy")
    monkeypatch.setattr(
        bridge, "_pty_run",
        lambda argv, *a, **k: captured.setdefault("argv", argv) or "out",
    )
    bridge.main(["list the files"])
    assert "--add-dir" in captured["argv"]
    assert os.getcwd() in captured["argv"]


# --- idle / hard timeout + partial output ---------------------------------

# Newline-terminated output: Windows ConPTY only flushes a row to the read pipe
# once the line is committed, so mid-run partial capture needs the "\n".
_PARTIAL_THEN_STALL = textwrap.dedent(
    """
    import os, sys, time
    if os.isatty(sys.stdout.fileno()):
        sys.stdout.write("PARTIAL\\n"); sys.stdout.flush()
    time.sleep(30)
    """
)

_STREAM_FOREVER = textwrap.dedent(
    """
    import os, sys, time
    if os.isatty(sys.stdout.fileno()):
        while True:
            sys.stdout.write("x\\n"); sys.stdout.flush(); time.sleep(0.05)
    else:
        time.sleep(30)
    """
)


# Mid-run partial capture is reliable on POSIX (os.openpty delivers promptly).
# Windows ConPTY batches output and may only flush at process exit, so on
# Windows we assert the timeout fires and carries a (possibly empty) `.partial`
# string, but only assert partial *content* on POSIX.
_POSIX = sys.platform != "win32"


def test_idle_timeout_fires_and_keeps_partial(tmp_path):
    stub = tmp_path / "stall_stub.py"
    stub.write_text(_PARTIAL_THEN_STALL)
    with pytest.raises(bridge.AgyTimeoutError) as ei:
        bridge._pty_run([sys.executable, str(stub)], timeout=60, idle_timeout=2)
    assert "idle" in str(ei.value)
    assert isinstance(ei.value.partial, str)
    if _POSIX:
        assert "PARTIAL" in ei.value.partial  # pre-stall output is preserved


def test_hard_timeout_fires_and_keeps_partial(tmp_path):
    stub = tmp_path / "stream_stub.py"
    stub.write_text(_STREAM_FOREVER)
    # idle never trips (constant output); the hard ceiling must.
    with pytest.raises(bridge.AgyTimeoutError) as ei:
        bridge._pty_run([sys.executable, str(stub)], timeout=2, idle_timeout=30)
    assert "hard" in str(ei.value)
    assert isinstance(ei.value.partial, str)
    if _POSIX:
        assert "x" in ei.value.partial


# --- live smoke (skipped if agy not available) ----------------------------

@pytest.mark.skipif(
    bridge.find_agy() is None,
    reason="agy binary not installed/authenticated; skipping live smoke",
)
def test_live_agy_roundtrip():
    out = bridge.run("reply with exactly: SMOKE_OK", timeout=120)
    assert "SMOKE_OK" in out


# --- MCP server: workspace + timeout plumbing -----------------------------

from agy_headless_bridge import mcp_server


def _agy_ask(args: dict, monkeypatch) -> dict:
    """Drive a tools/call for agy_ask, capturing the kwargs that reach run()."""
    captured = {}

    def fake_run(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(mcp_server, "run", fake_run)
    mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "agy_ask", "arguments": args},
    })
    return captured


def test_mcp_agy_ask_defaults_to_cwd(monkeypatch):
    cap = _agy_ask({"prompt": "fix the bug"}, monkeypatch)
    assert cap["add_dirs"] == [os.getcwd()]


def test_mcp_agy_ask_workspace_none_suppresses_cwd(monkeypatch):
    cap = _agy_ask({"prompt": "what is 2+2", "workspace": "none"}, monkeypatch)
    assert cap["add_dirs"] == []


def test_mcp_agy_ask_timeout_reaches_run(monkeypatch):
    cap = _agy_ask({"prompt": "big refactor", "timeout": 1800}, monkeypatch)
    assert cap["timeout"] == 1800


def test_mcp_agy_research_gets_no_workspace(monkeypatch):
    captured = {}

    def fake_run(prompt, **kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(mcp_server, "run", fake_run)
    mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "agy_research", "arguments": {"query": "transformers"}},
    })
    # research must never attach the repo
    assert captured["add_dirs"] is None or captured["add_dirs"] == []


def test_mcp_agy_ask_timeout_surfaces_partial(monkeypatch):
    def fake_run(prompt, **kwargs):
        raise bridge.AgyTimeoutError("agy idle for 120s", partial="HALF DONE")

    monkeypatch.setattr(mcp_server, "run", fake_run)
    out = mcp_server._call_agy("do it", add_dirs=[os.getcwd()])
    assert "HALF DONE" in out
    assert "agy -c" in out  # tells caller how to resume
