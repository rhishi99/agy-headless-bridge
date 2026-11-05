"""Direct (no-pty) mode, JSON results, heartbeat liveness, resume/mode/effort,
write-denial detection and the MCP mode gate. No agy needed: stubs stand in."""

import json
import os
import sys

import pytest

from conftest import REAL_AGY_VERSION, _as_result
from agy_headless_bridge import bridge, mcp_server

# Shape captured live from agy 1.5.0 (`--output-format json`, piped stdout).
OK = {"conversation_id": "7b43469e-9828-49e2-bc53-f91b6eb02952", "status": "SUCCESS",
      "response": "AGY_OK\n", "duration_seconds": 12.4, "num_turns": 1,
      "usage": {"input_tokens": 9004, "output_tokens": 4, "thinking_tokens": 0,
                "cache_read_tokens": 0, "total_tokens": 9008}}
# agy 1.4.11 live: a deny rule blocked the write, exit 0, refusal only in `response`.
DENY = {"conversation_id": "0", "status": "SUCCESS", "response":
        "I am unable to create `hello.txt`:\n\n```\nPermission denied for "
        "write_file(/w/hello.txt). Matches user-configured deny rule.\n```\n"}
BAD_MODEL = {"conversation_id": "", "status": "ERROR", "response": "",
             "error": "invalid model selection (--model \"x\"): model x is not recognized"}


# --- _finish: JSON result -> AgyResult or error ---------------------------------

def test_finish_success_carries_id_and_usage():
    res = bridge._finish(json.dumps(OK) + "\n", 0)
    assert res.text == "AGY_OK"  # trailing newline trimmed, as on the pty path
    assert res.conversation_id == OK["conversation_id"]
    assert res.usage["input_tokens"] == 9004
    assert res.denied == []


def test_finish_keeps_box_glyphs_in_reply():
    # clean() would strip these as TUI chrome; JSON replies must keep them.
    res = bridge._finish(json.dumps({"status": "SUCCESS", "response": "│ a │ b │"}), 0)
    assert res.text == "│ a │ b │"


def test_finish_error_status_raises_with_agy_error_text():
    with pytest.raises(bridge.AgyExitError) as e:
        bridge._finish("error: noise on stderr\n" + json.dumps(BAD_MODEL), 1)
    assert "invalid model" in e.value.output
    assert not isinstance(e.value, bridge.AgyQuotaError)


def test_finish_error_status_with_exit_0_still_fails():
    with pytest.raises(bridge.AgyExitError) as e:
        bridge._finish(json.dumps(BAD_MODEL), 0)
    assert e.value.returncode == 1


def test_finish_quota_error_in_json():
    obj = {**BAD_MODEL, "error": "You have exhausted your capacity; resets in 4h 50m"}
    with pytest.raises(bridge.AgyQuotaError) as e:
        bridge._finish(json.dumps(obj), 75)
    assert e.value.reset_seconds == 4 * 3600 + 50 * 60


def test_finish_without_json_falls_back_to_clean_text():
    assert bridge._finish("\x1b[31mplain answer\x1b[0m\n", 0).text == "plain answer"
    with pytest.raises(bridge.AgyExitError):
        bridge._finish("crashed\n", 2)


def test_denial_found_in_response_and_denied_actions():
    assert bridge._finish(json.dumps(DENY), 0).denied == ["Permission denied for write_file(/w/hello.txt)"]
    obj = {"status": "SUCCESS", "response": "done", "denied_actions": ["edit_file(/w/a.py)"]}
    assert bridge._finish(json.dumps(obj), 0).denied == ["edit_file(/w/a.py)"]


@pytest.mark.parametrize("msg", ["You have exhausted your quota", "usage limit reached",
                                 "out of credits"])
def test_new_quota_phrases_on_failed_exit(msg):
    with pytest.raises(bridge.AgyQuotaError):
        bridge._check_exit(msg, 1)


# --- argv + mode selection ------------------------------------------------------

def test_build_argv_session_flags():
    argv = bridge.build_argv("agy", "p", mode="plan", effort="high",
                             conversation="abc", output_json=True)
    for pair in (["--mode", "plan"], ["--effort", "high"], ["--conversation", "abc"],
                 ["--output-format", "json"]):
        i = argv.index(pair[0])
        assert argv[i:i + 2] == pair
    assert argv[-2:] == ["-p", "p"]
    assert "--mode" not in bridge.build_argv("agy", "p")


def test_use_direct_by_version_and_env(monkeypatch):
    monkeypatch.setattr(bridge, "_agy_version", lambda p: (1, 5, 0))
    assert bridge._use_direct("agy")
    monkeypatch.setattr(bridge, "_agy_version", lambda p: (1, 2, 10))
    assert not bridge._use_direct("agy")
    monkeypatch.setattr(bridge, "_agy_version", lambda p: ())
    assert not bridge._use_direct("agy")  # unknown -> pty
    monkeypatch.setenv(bridge.MODE_ENV, "direct")
    assert bridge._use_direct("agy")
    monkeypatch.setattr(bridge, "_agy_version", lambda p: (9, 0, 0))
    monkeypatch.setenv(bridge.MODE_ENV, "pty")
    assert not bridge._use_direct("agy")


def test_agy_version_parses_and_survives_missing_binary(tmp_path):
    real = REAL_AGY_VERSION.__wrapped__  # skip the cache
    stub = tmp_path / "v.py"
    stub.write_text("print('agy 1.5.0')")
    if sys.platform == "win32":
        exe = tmp_path / "agyv.cmd"
        exe.write_text(f'@"{sys.executable}" "{stub}"\n')
    else:
        exe = tmp_path / "agyv"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}"\n')
        exe.chmod(0o755)
    assert real(str(exe)) == (1, 5, 0)
    assert real(str(tmp_path / "missing")) == ()


# --- direct runner with a stub agy ----------------------------------------------

def _stub(tmp_path, body):
    f = tmp_path / "stub.py"
    f.write_text(body, encoding="utf-8")
    return [sys.executable, str(f)]


def test_direct_run_end_to_end(tmp_path, monkeypatch):
    stub = _stub(tmp_path, f"import sys; sys.stdout.write({json.dumps(json.dumps(OK))} + '\\n')")
    monkeypatch.setenv(bridge.MODE_ENV, "direct")
    monkeypatch.setattr(bridge, "build_argv", lambda *a, **k: stub)
    res = bridge.run_result("x", agy_path="agy", timeout=30)
    assert res.text == "AGY_OK" and res.conversation_id == OK["conversation_id"]


def test_direct_build_asks_for_json(monkeypatch):
    seen = {}
    monkeypatch.setenv(bridge.MODE_ENV, "direct")
    monkeypatch.setattr(bridge, "_run_direct",
                        lambda argv, *a: seen.update(argv=argv) or (json.dumps(OK), 0))
    bridge.run_result("x", agy_path="agy")
    assert "--output-format" in seen["argv"]


def test_direct_idle_timeout_kills_silent_child(tmp_path):
    argv = _stub(tmp_path, "import time; time.sleep(30)")
    with pytest.raises(bridge.AgyTimeoutError, match="idle"):
        bridge._run_direct(argv, timeout=60, idle_timeout=1.5)


def test_heartbeat_file_keeps_silent_child_alive(tmp_path):
    hb = tmp_path / "progress.log"
    hb.write_text("")
    body = (f"import time\nfor i in range(6):\n    time.sleep(0.5)\n"
            f"    open({str(hb)!r}, 'a').write('step %d\\n' % i)\nprint('{{\"status\":\"SUCCESS\",\"response\":\"done\"}}')")
    out, rc = bridge._run_direct(_stub(tmp_path, body), timeout=60, idle_timeout=1.5,
                                 progress=str(hb))
    assert rc == 0 and bridge._finish(out, rc).text == "done"
    # Same child without the heartbeat file being watched: silent 3 s > 1.5 s idle.
    with pytest.raises(bridge.AgyTimeoutError):
        bridge._run_direct(_stub(tmp_path, body), timeout=60, idle_timeout=1.5)


def test_heartbeat_refused_in_plan_mode(monkeypatch):
    monkeypatch.setattr(bridge, "_pty_run", lambda *a, **k: pytest.fail("spawned"))
    with pytest.raises(ValueError, match="plan"):
        bridge.run_result("x", agy_path="agy", heartbeat=True, mode="plan")


def test_heartbeat_run_wires_prompt_dir_idle_and_cleans_up(monkeypatch):
    seen = {}

    def fake_pty(argv, t, idle, progress=None):
        seen.update(argv=argv, t=t, idle=idle, progress=progress,
                    existed=os.path.exists(progress))
        with open(progress, "a") as f:
            f.write("10:00 done: read files / next: edit\n")
        raise bridge.AgyTimeoutError("agy idle (no output) for 300s", "")

    monkeypatch.setattr(bridge, "_pty_run", fake_pty)
    with pytest.raises(bridge.AgyTimeoutError) as e:
        bridge.run_result("do it", agy_path="agy", timeout=3600, heartbeat=True)
    assert seen["existed"] and not os.path.exists(seen["progress"])  # removed after
    assert (seen["t"], seen["idle"]) == (3600, bridge.DEFAULT_IDLE_TIMEOUT)
    assert os.path.dirname(seen["progress"]) in seen["argv"]  # --add-dir'd
    assert seen["progress"].replace("\\", "/") in seen["argv"][-1]  # named in prompt
    assert "next: edit" in e.value.partial  # last progress surfaced


# --- MCP: mode gate, session params, notes --------------------------------------

def _call(name, arguments):
    return mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": arguments}})


@pytest.mark.parametrize("mode", ["auto", "accept-edits", "Auto", "yolo"])
def test_mcp_mode_that_approves_edits_needs_opt_in(monkeypatch, mode):
    monkeypatch.delenv(mcp_server.SKIP_PERMISSIONS_ENV, raising=False)
    monkeypatch.setattr(mcp_server, "run_result", lambda *a, **k: pytest.fail("ran agy"))
    r = _call("agy_ask", {"prompt": "x", "mode": mode})
    assert r["result"]["isError"] and "refused" in r["result"]["content"][0]["text"]


def test_mcp_passes_session_params_and_appends_notes(monkeypatch):
    seen = {}

    def fake(prompt, **k):
        seen.update(k)
        return bridge.AgyResult("answer", conversation_id="c-1",
                                denied=["Permission denied for write_file(/a)"])

    monkeypatch.setenv(mcp_server.SKIP_PERMISSIONS_ENV, "1")
    monkeypatch.setattr(mcp_server, "run_result", fake)
    r = _call("agy_ask", {"prompt": "x", "mode": "auto", "effort": "high",
                          "conversation": "c-0", "heartbeat": True})
    text = r["result"]["content"][0]["text"]
    assert (seen["mode"], seen["effort"], seen["conversation"], seen["heartbeat"]) == \
        ("auto", "high", "c-0", True)
    assert text.startswith("answer") and "conversation c-1" in text and "refused file writes" in text


def test_mcp_research_plan_mode_allowed_without_opt_in(monkeypatch):
    monkeypatch.delenv(mcp_server.SKIP_PERMISSIONS_ENV, raising=False)
    monkeypatch.setattr(mcp_server, "run_result", _as_result(lambda p, **k: "ok"))
    assert "isError" not in _call("agy_ask", {"prompt": "x", "mode": "plan"})["result"]
    assert "isError" not in _call("agy_research", {"query": "q"})["result"]
