"""
Timeout permutations and the output cap.

25 Oct 2026: `agy -p` is silent until its answer is ready; a web research call
was silent 218 s and the 120 s idle timer killed it even with timeout=600.
Separately, a long agy reply came back whole into the calling model's context.
"""

import sys

import pytest

from agy_headless_bridge import bridge, mcp_server


@pytest.fixture
def tmp_temp(tmp_path, monkeypatch):
    """Point cap_output's full-text files at a per-test folder."""
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


# --- run(): hard + idle timeout resolution -----------------------------------

def _run_bounds(monkeypatch, **kwargs):
    """Call run() with a stub pty; return the (timeout, idle) that reached it."""
    seen = {}
    monkeypatch.setattr(bridge, "find_agy", lambda: "agy")
    monkeypatch.setattr(
        bridge, "_pty_run",
        lambda argv, timeout, idle: seen.update(t=timeout, idle=idle) or "ok",
    )
    bridge.run("x", **kwargs)
    return seen["t"], seen["idle"]


def test_defaults_keep_short_idle(monkeypatch):
    assert _run_bounds(monkeypatch) == (bridge.DEFAULT_TIMEOUT, bridge.DEFAULT_IDLE_TIMEOUT)


@pytest.mark.parametrize("timeout", [60, 600, 1800])
def test_caller_timeout_lifts_idle(monkeypatch, timeout):
    # Below and above the default idle: the caller's budget is the bound.
    assert _run_bounds(monkeypatch, timeout=timeout) == (timeout, timeout)


def test_explicit_idle_wins(monkeypatch):
    assert _run_bounds(monkeypatch, timeout=600, idle_timeout=30) == (600, 30)
    assert _run_bounds(monkeypatch, idle_timeout=30) == (bridge.DEFAULT_TIMEOUT, 30)


def test_numeric_string_timeout_accepted(monkeypatch):
    assert _run_bounds(monkeypatch, timeout="600") == (600.0, 600.0)


@pytest.mark.parametrize("bad", [0, -5, 0.5, True, False, "soon", float("nan"),
                                 float("inf"), [600]])
def test_bad_timeout_rejected_before_spawning(monkeypatch, bad):
    monkeypatch.setattr(bridge, "find_agy", lambda: "agy")
    monkeypatch.setattr(bridge, "_pty_run", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(ValueError):
        bridge.run("x", timeout=bad)
    with pytest.raises(ValueError):
        bridge.run("x", idle_timeout=bad)


def test_non_string_prompt_rejected():
    with pytest.raises(ValueError):
        bridge.run(None)


def test_bad_env_timeout_falls_back(monkeypatch, capsys):
    name = "AGY_BRIDGE_IDLE_TIMEOUT"
    monkeypatch.setenv(name, "abc")
    assert bridge._env_seconds(name, 300.0) == 300.0
    assert "ignoring" in capsys.readouterr().err
    monkeypatch.setenv(name, "-1")
    assert bridge._env_seconds(name, 300.0) == 300.0
    monkeypatch.setenv(name, "450")
    assert bridge._env_seconds(name, 300.0) == 450.0


def test_silent_child_survives_default_idle_when_timeout_given(tmp_path, monkeypatch):
    # The real bug, through a real pty: silent longer than the idle bound, then
    # an answer. Default idle shrunk to 1 s; the caller's timeout=30 lifts it.
    stub = tmp_path / "silent_stub.py"
    stub.write_text("import time; time.sleep(3); print('ANSWER')")
    monkeypatch.setattr(bridge, "DEFAULT_IDLE_TIMEOUT", 1.0)
    monkeypatch.setattr(bridge, "find_agy", lambda: "agy")
    real_pty = bridge._pty_run
    monkeypatch.setattr(
        bridge, "_pty_run",
        lambda argv, t, idle: real_pty([sys.executable, str(stub)], t, idle),
    )
    assert "ANSWER" in bridge.run("x", timeout=30)
    with pytest.raises(bridge.AgyTimeoutError):
        bridge.run("x")  # no caller timeout: the 1 s default idle still applies


def test_cli_timeout_lifts_idle_and_bad_value_exits_2(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(bridge, "find_agy", lambda: "agy")
    monkeypatch.setattr(
        bridge, "_pty_run", lambda argv, t, idle: seen.update(t=t, idle=idle) or "ok"
    )
    assert bridge.main(["--timeout", "840", "hi"]) == 0
    assert seen == {"t": 840, "idle": 840}
    assert bridge.main(["--timeout", "0", "hi"]) == 2
    assert "timeout" in capsys.readouterr().err


# --- cap_output ----------------------------------------------------------------

def test_cap_short_text_untouched():
    assert bridge.cap_output("short", 4000) == "short"
    assert bridge.cap_output("x" * 9000, 0) == "x" * 9000  # 0 = no cap


def test_cap_returns_last_summary_and_saves_full(tmp_temp):
    text = ("working... " * 800) + "\nSUMMARY: old\n" + ("more " * 100) + \
        "\n## Summary\n- fixed bug in a.py\n- tests pass"
    out = bridge.cap_output(text, 500)
    assert out.startswith("## Summary")
    assert "old" not in out and "working" not in out
    path = out.rsplit("full text: ", 1)[1]
    assert open(path, encoding="utf-8").read() == text


@pytest.mark.parametrize("marker", ["SUMMARY:", "**Summary:**", "Summary", "> SUMMARY: done"])
def test_cap_summary_marker_shapes(tmp_temp, marker):
    out = bridge.cap_output("y" * 2000 + "\n" + marker + "\nok", 200)
    assert "summary only" in out and "ok" in out


def test_cap_no_summary_keeps_tail(tmp_temp):
    text = "a" * 5000 + "THE_ANSWER"
    out = bridge.cap_output(text, 100)
    assert out.split("\n\n[agy-bridge]")[0] == text[-100:]
    assert "last 100 chars" in out


def test_cap_summary_word_in_prose_is_not_a_marker(tmp_temp):
    text = "a" * 3000 + "\nIn summary the fix works\nSummary of changes below\nEND"
    assert "last 100 chars" in bridge.cap_output(text, 100)


def test_cap_oversized_summary_falls_back_to_tail(tmp_temp):
    assert "last 100 chars" in bridge.cap_output("SUMMARY:\n" + "z" * 3000, 100)


def test_cap_unwritable_temp_still_returns(monkeypatch):
    def boom(text):
        raise OSError("disk full")

    monkeypatch.setattr(bridge, "_save_full", boom)
    out = bridge.cap_output("b" * 500 + "\nSUMMARY: x", 50)
    assert "full text not saved (disk full)" in out and "SUMMARY: x" in out


def test_cap_keeps_unicode(tmp_temp):
    text = "₹ नमस्ते " * 500
    path = bridge.cap_output(text, 100).rsplit("full text: ", 1)[1]
    assert open(path, encoding="utf-8").read() == text


def test_cli_brief_and_max_chars(monkeypatch, capsys, tmp_temp):
    seen = {}
    monkeypatch.setattr(bridge, "find_agy", lambda: "agy")
    monkeypatch.setattr(
        bridge, "_pty_run",
        lambda argv, *a: seen.update(argv=argv) or "w" * 900 + "\nSUMMARY: done",
    )
    assert bridge.main(["--brief", "--max-chars", "100", "hi"]) == 0
    assert seen["argv"][-1].endswith(bridge.BRIEF_INSTRUCTION)
    assert capsys.readouterr().out.startswith("SUMMARY: done")


def test_cli_without_flags_prints_everything(monkeypatch, capsys):
    monkeypatch.setattr(bridge, "find_agy", lambda: "agy")
    monkeypatch.setattr(bridge, "_pty_run", lambda argv, *a: "w" * 9000)
    assert bridge.main(["hi"]) == 0
    assert capsys.readouterr().out.strip() == "w" * 9000


# --- MCP wiring ------------------------------------------------------------------

def _mcp_call(name, args, monkeypatch, reply="ok"):
    captured = {}

    def fake_run(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(mcp_server, "run", fake_run)
    resp = mcp_server.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })
    return captured, resp


def _text(resp):
    return resp["result"]["content"][0]["text"]


def test_mcp_ask_leaves_idle_to_run(monkeypatch):
    cap, _ = _mcp_call("agy_ask", {"prompt": "x", "timeout": 600}, monkeypatch)
    assert cap["timeout"] == 600 and "idle_timeout" not in cap


def test_mcp_research_passes_timeout_so_idle_lifts(monkeypatch):
    cap, _ = _mcp_call("agy_research", {"query": "q"}, monkeypatch)
    assert cap["timeout"] == bridge.DEFAULT_TIMEOUT
    cap, _ = _mcp_call("agy_research", {"query": "q", "timeout": 1200, "model": "m"},
                       monkeypatch)
    assert cap["timeout"] == 1200 and cap["model"] == "m"


def test_mcp_bad_timeout_is_a_tool_error(monkeypatch):
    monkeypatch.setattr(bridge, "find_agy", lambda: "agy")
    monkeypatch.setattr(bridge, "_pty_run", lambda *a, **k: pytest.fail("spawned"))
    for name, args in (("agy_ask", {"prompt": "x", "timeout": -1}),
                       ("agy_research", {"query": "q", "timeout": "soon"})):
        resp = mcp_server.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        })
        assert resp["result"]["isError"] is True and "timeout" in _text(resp)


@pytest.mark.parametrize("args", [{}, {"prompt": ""}, {"prompt": 5}])
def test_mcp_ask_rejects_bad_prompt(monkeypatch, args):
    cap, resp = _mcp_call("agy_ask", args, monkeypatch)
    assert resp["result"]["isError"] is True and not cap


def test_mcp_brief_default_on_and_opt_out(monkeypatch):
    cap, _ = _mcp_call("agy_ask", {"prompt": "x"}, monkeypatch)
    assert cap["prompt"] == "x" + bridge.BRIEF_INSTRUCTION
    cap, _ = _mcp_call("agy_ask", {"prompt": "x", "brief": False}, monkeypatch)
    assert cap["prompt"] == "x"
    cap, _ = _mcp_call("agy_research", {"query": "q"}, monkeypatch)
    assert cap["prompt"].endswith(bridge.BRIEF_INSTRUCTION)


def test_mcp_long_reply_is_capped(monkeypatch, tmp_temp):
    long = "step " * 5000 + "\nSUMMARY:\n- done"
    _, resp = _mcp_call("agy_ask", {"prompt": "x"}, monkeypatch, reply=long)
    assert len(_text(resp)) < mcp_server.DEFAULT_MAX_CHARS
    assert _text(resp).startswith("SUMMARY:")
    _, resp = _mcp_call("agy_ask", {"prompt": "x", "max_chars": 0}, monkeypatch, reply=long)
    assert _text(resp) == long


def test_mcp_max_chars_env_and_bad_values(monkeypatch):
    monkeypatch.setenv(mcp_server.MAX_CHARS_ENV, "50")
    assert mcp_server._max_chars(None) == 50
    assert mcp_server._max_chars(10) == 10
    for bad in (-1, True, "lots", [1]):
        with pytest.raises(ValueError):
            mcp_server._max_chars(bad)
    cap, resp = _mcp_call("agy_ask", {"prompt": "x", "max_chars": -3}, monkeypatch)
    assert resp["result"]["isError"] is True and not cap


def test_mcp_capped_timeout_keeps_bridge_note(monkeypatch, tmp_temp):
    err = bridge.AgyTimeoutError("agy idle (no output) for 300s", partial="p" * 9000)
    _, resp = _mcp_call("agy_ask", {"prompt": "x"}, monkeypatch, reply=err)
    assert resp["result"]["isError"] is True
    assert "TIMEOUT" in _text(resp) and "agy -c" in _text(resp)
    assert len(_text(resp)) < 5000


def test_mcp_tools_list_schema_has_new_args():
    tools = {t["name"]: t["inputSchema"]["properties"] for t in mcp_server.TOOLS}
    for name in ("agy_ask", "agy_research"):
        assert {"brief", "max_chars", "timeout", "model"} <= set(tools[name])
