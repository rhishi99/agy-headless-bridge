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
    monkeypatch.setattr(bridge, "_pty_run", lambda argv, t: captured.setdefault("argv", argv) or "ok")
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


# --- live smoke (skipped if agy not available) ----------------------------

@pytest.mark.skipif(
    bridge.find_agy() is None,
    reason="agy binary not installed/authenticated; skipping live smoke",
)
def test_live_agy_roundtrip():
    out = bridge.run("reply with exactly: SMOKE_OK", timeout=120)
    assert "SMOKE_OK" in out
