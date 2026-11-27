import json
import re
import queue
import sys
import threading
import time

import pytest

from agy_headless_bridge import bridge, mcp_server


@pytest.fixture
def stdio(monkeypatch):
    inbox = queue.Queue()
    outbox = queue.Queue()
    monkeypatch.setattr(sys, "stdin", iter(inbox.get, None))
    monkeypatch.setattr(mcp_server, "_send", outbox.put)
    server = threading.Thread(target=mcp_server.main, args=([],), daemon=True)
    server.start()

    def send(method, request_id=None, **params):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        inbox.put(json.dumps(message))

    yield send, outbox, inbox, server
    if server.is_alive():
        inbox.put(None)
        server.join(8)
    assert not server.is_alive()


def test_stdio_schedules_independent_jobs_and_bounds_admission(stdio, monkeypatch):
    send, outbox, _, _ = stdio
    started = queue.Queue()
    release = threading.Event()

    def run(prompt, *, cancel, **kwargs):
        started.put(prompt)
        if prompt == "first":
            assert cancel.wait(6)
        elif prompt == "independent":
            assert release.wait(6)
        return bridge.AgyResult(prompt)

    monkeypatch.setattr(mcp_server, "run_result", run)

    def ask(request_id, prompt, conversation):
        send("tools/call", request_id, name="agy_ask",
             arguments={"prompt": prompt, "conversation": conversation, "mode": "plan",
                        "workspace": "none", "brief": False})

    ask(1, "first", "shared")
    assert started.get(timeout=3) == "first"
    ask(2, "queued cancellation", "shared")
    ask(3, "independent", "different")
    assert started.get(timeout=3) == "independent"
    for request_id in range(4, 9):
        ask(request_id, "queued", "shared")
    ask(9, "busy", "different")
    busy = outbox.get(timeout=3)
    assert busy["id"] == 9 and busy["result"]["isError"]
    assert "queue is full" in busy["result"]["content"][0]["text"]
    for request_id in (2, 4, 5, 6, 7, 8):
        send("notifications/cancelled", requestId=request_id)
    ask(2, "replacement", "shared")
    send("ping", 10)
    assert outbox.get(timeout=3)["id"] == 10
    assert started.empty()
    send("notifications/cancelled", requestId=1)
    assert started.get(timeout=3) == "replacement"
    assert outbox.get(timeout=3)["id"] == 2
    release.set()
    assert outbox.get(timeout=3)["id"] == 3
    assert outbox.empty()


@pytest.mark.parametrize("writer_args", [{}, {"mode": "plan"},
                                         {"mode": "plan", "workspace": "none", "add_dir": ["."]}])
def test_stdio_serializes_potential_writers_and_cancels_on_shutdown(stdio, monkeypatch, writer_args):
    send, outbox, inbox, server = stdio
    started = queue.Queue()
    cancelled = queue.Queue()
    release = threading.Event()

    def run(prompt, *, cancel, **kwargs):
        started.put(prompt)
        if prompt == "reader":
            assert release.wait(6)
        elif prompt == "writer" and cancel.wait(6):
            cancelled.put(prompt)
        return bridge.AgyResult(prompt)

    monkeypatch.setattr(mcp_server, "run_result", run)
    send("notifications/initialized")
    send("tools/call", 1, name="agy_ask",
         arguments={"prompt": "reader", "mode": "plan", "workspace": "none", "brief": False})
    assert started.get(timeout=3) == "reader"
    send("tools/call", 2, name="agy_ask", arguments={"prompt": "writer", "brief": False, **writer_args})
    send("tools/call", 3, name="agy_ask",
         arguments={"prompt": "queued reader", "mode": "plan", "workspace": "none", "brief": False})
    send("ping", 4)
    assert outbox.get(timeout=3)["id"] == 4
    assert started.empty()
    release.set()
    assert outbox.get(timeout=3)["id"] == 1
    assert started.get(timeout=3) == "writer"
    inbox.put(None)
    assert cancelled.get(timeout=3) == "writer"
    server.join(3)
    assert not server.is_alive()
    assert started.empty() and outbox.empty()


def test_stdio_drops_requests_after_broken_output_pipe(stdio, monkeypatch):
    send, _, inbox, server = stdio
    attempted = threading.Event()
    calls = []
    writes = []

    def broken_pipe(response):
        writes.append(response["id"])
        attempted.set()
        raise BrokenPipeError

    def run(prompt, **kwargs):
        calls.append(prompt)
        return bridge.AgyResult(prompt)

    monkeypatch.setattr(mcp_server, "_send", broken_pipe)
    monkeypatch.setattr(mcp_server, "run_result", run)
    send("ping", 1)
    assert attempted.wait(3)
    send("tools/call", 2, name="agy_ask", arguments={"prompt": "after broken pipe"})
    send("ping", 3)
    send("notifications/initialized")
    inbox.put(None)
    server.join(2)
    assert not server.is_alive()
    assert writes == [1]
    assert not calls


def test_stdio_progress_and_cancel_keep_the_connection_live(monkeypatch):
    inbox = queue.Queue()
    outbox = queue.Queue()
    cancelled = threading.Event()
    monkeypatch.setattr(sys, "stdin", iter(inbox.get, None))
    monkeypatch.setattr(mcp_server, "_send", outbox.put)

    def run(prompt, *, cancel, on_status, **kwargs):
        if prompt.startswith("next"):
            assert on_status is None
            return bridge.AgyResult("next answer")
        on_status(5, "Elapsed time")
        on_status(10, "AGY activity observed")
        if cancel.wait(5):
            cancelled.set()
            on_status(15, "Elapsed time")
        return bridge.AgyResult("late answer")

    monkeypatch.setattr(mcp_server, "run_result", run)

    def send(method, request_id=None, **params):
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            message["id"] = request_id
        inbox.put(json.dumps(message))

    server = threading.Thread(target=mcp_server.main, args=([],), daemon=True)
    server.start()
    try:
        send("tools/call", 1, name="agy_ask", arguments={"prompt": "first"},
             _meta={"progressToken": 0})
        for count, message in ((5, "Elapsed time"), (10, "AGY activity observed")):
            assert outbox.get(timeout=3) == {
                "jsonrpc": "2.0", "method": "notifications/progress",
                "params": {"progressToken": 0, "progress": count, "message": message},
            }
        send("ping", 2)
        assert outbox.get(timeout=3)["id"] == 2
        send("notifications/cancelled", requestId=1)
        assert cancelled.wait(3)
        send("tools/call", 3, name="agy_ask", arguments={"prompt": "next"})
        reply = outbox.get(timeout=3)
        assert reply["id"] == 3
        assert reply["result"]["content"][0]["text"] == "next answer"
        assert outbox.empty()
    finally:
        inbox.put(None)
        server.join(6)
    assert not server.is_alive()


@pytest.mark.parametrize("mode", ["direct", "pty"])
@pytest.mark.parametrize("reason", ["cancelled", "hard timeout", "idle"])
def test_terminal_states_stop_running_process_tree(monkeypatch, tmp_path, mode, reason):
    started = tmp_path / "started"
    survived = tmp_path / "survived"
    probe = tmp_path / "probe"
    worker = (
        "from pathlib import Path; import time; "
        f"Path({str(started)!r}).touch(); start = time.monotonic(); probe = Path({str(probe)!r}); "
        "exec('while not probe.exists() and time.monotonic() - start < 30:\\n time.sleep(.02)'); "
        f"probe.exists() and Path({str(survived)!r}).touch()"
    )
    child = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {worker!r}]); "
        + ("exec('while True:\\n print(\"pulse\", flush=True)\\n time.sleep(.2)')"
           if reason == "hard timeout" else "time.sleep(30)")
    )
    monkeypatch.setattr(bridge, "build_argv", lambda *args, **kwargs: [sys.executable, "-c", child])
    monkeypatch.setenv(bridge.MODE_ENV, mode)
    cancel = threading.Event()

    def cancel_when_running():
        deadline = time.monotonic() + 20
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        cancel.set()

    timeout, idle = {"hard timeout": (12, 20), "idle": (25, 12), "cancelled": (25, 25)}[reason]
    if reason == "cancelled":
        threading.Thread(target=cancel_when_running, daemon=True).start()
    error = bridge.AgyCancelledError if reason == "cancelled" else bridge.AgyTimeoutError
    with pytest.raises(error, match=reason) as caught:
        bridge.run_result("wait", timeout=timeout, idle_timeout=idle, agy_path=sys.executable, cancel=cancel)
    assert started.exists()
    if reason == "hard timeout":
        assert "pulse" in caught.value.partial
    probe.touch()
    time.sleep(0.6)
    assert not survived.exists()


def test_progress_ticks_without_resetting_an_idle_timeout(monkeypatch):
    monkeypatch.setenv(bridge.MODE_ENV, "direct")
    monkeypatch.setattr(bridge, "_PROGRESS_INTERVAL", 0.2, raising=False)
    monkeypatch.setattr(bridge, "build_argv", lambda *a, **k: [
        sys.executable, "-c", "import time; time.sleep(10)",
    ])
    ticks = []
    with pytest.raises(bridge.AgyTimeoutError, match="idle"):
        bridge.run_result("wait", timeout=5, idle_timeout=1, agy_path=sys.executable,
                          on_progress=ticks.append)
    assert ticks and ticks == sorted(set(ticks))


def test_stream_progress_uses_observed_steps_and_keeps_final_result(monkeypatch):
    monkeypatch.setenv(bridge.MODE_ENV, "direct")
    monkeypatch.setattr(bridge, "_agy_version", lambda *a, **k: (1, 5, 18))
    monkeypatch.setattr(bridge, "_PROGRESS_INTERVAL", 0.05)
    build_argv = bridge.build_argv

    def command(*args, **kwargs):
        argv = build_argv(*args, **kwargs)
        assert argv[argv.index("--output-format") + 1] == "stream-json"
        frames = [
            {"event": "step_update", "step_update": {
                "state": "ACTIVE", "step_type": "tool", "text_delta": "private step",
            }},
            {"event": "result", "result": {
                "status": "SUCCESS", "response": "final answer", "structured_output": {"value": 4},
            }},
        ]
        first, last = (json.dumps(frame) for frame in frames)
        return [sys.executable, "-u", "-c", f"import time; print({first!r}); time.sleep(.6); print({last!r})"]

    monkeypatch.setattr(bridge, "build_argv", command)
    progress = []
    result = bridge.run_result("wait", timeout=5, agy_path=sys.executable, cancel=threading.Event(),
                               on_status=lambda elapsed, message: progress.append((elapsed, message)))
    assert result.text == "final answer" and result.structured_output == {"value": 4}
    messages = [message for _, message in progress]
    assert sum(m.startswith("AGY activity observed") for m in messages) == 1
    assert all(re.search(r"\d+ s", m) for m in messages)  # hosts show the text, not the number


@pytest.mark.parametrize("frame, message", [
    ('{"event":"step_update","step_update":{"text_delta":"unfinished"}}', "stream ended without a result"),
    ('{"event":"error","error":{"message":"authentication required"}}', "authentication required"),
])
def test_stream_progress_reports_failure_without_model_deltas(monkeypatch, frame, message):
    monkeypatch.setenv(bridge.MODE_ENV, "direct")
    monkeypatch.setattr(bridge, "_agy_version", lambda *a, **k: (1, 5, 18))
    monkeypatch.setattr(bridge, "build_argv", lambda *a, **k: [sys.executable, "-c", f"print({frame!r})"])
    with pytest.raises(bridge.AgyExitError) as raised:
        bridge.run_result("wait", agy_path=sys.executable, on_status=lambda *_: None)
    assert message in raised.value.output
    assert "unfinished" not in raised.value.output


def test_cancel_terminates_an_in_flight_catalog_probe(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "models").write_text(
        "from pathlib import Path\n"
        "import time\n"
        "Path('probe-started').touch()\n"
        "time.sleep(3)\n"
        "Path('probe-finished').touch()\n"
        "print('gemini-3.8-flash-high\\tFlash High')\n",
        encoding="utf-8",
    )
    cancel = threading.Event()

    def cancel_probe():
        deadline = time.monotonic() + 20
        while not (tmp_path / "probe-started").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        cancel.set()

    trigger = threading.Thread(target=cancel_probe, daemon=True)
    trigger.start()
    with pytest.raises(RuntimeError, match="cancelled"):
        bridge.run_result("wait", model="flash", agy_path=sys.executable, cancel=cancel)
    trigger.join(1)
    assert (tmp_path / "probe-started").exists()
    assert not (tmp_path / "probe-finished").exists()
