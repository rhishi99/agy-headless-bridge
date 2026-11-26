import json
import queue
import sys
import threading
import time

import pytest

from agy_headless_bridge import bridge, mcp_server


def test_stdio_progress_and_cancel_keep_the_connection_live(monkeypatch):
    inbox = queue.Queue()
    outbox = queue.Queue()
    cancelled = threading.Event()
    monkeypatch.setattr(sys, "stdin", iter(inbox.get, None))
    monkeypatch.setattr(mcp_server, "_send", outbox.put)

    def run(prompt, *, cancel, on_progress, **kwargs):
        if prompt.startswith("next"):
            assert on_progress is None
            return bridge.AgyResult("next answer")
        on_progress(5)
        on_progress(10)
        if cancel.wait(5):
            cancelled.set()
            on_progress(15)
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
        for count in (5, 10):
            assert outbox.get(timeout=3) == {
                "jsonrpc": "2.0", "method": "notifications/progress",
                "params": {"progressToken": 0, "progress": count},
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
def test_cancel_stops_running_process_tree(monkeypatch, tmp_path, mode):
    started = tmp_path / "started"
    survived = tmp_path / "survived"
    worker = (
        "from pathlib import Path; import time; "
        f"Path({str(started)!r}).touch(); time.sleep(4); Path({str(survived)!r}).touch()"
    )
    child = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {worker!r}]); time.sleep(20)"
    )
    monkeypatch.setattr(bridge, "build_argv", lambda *args, **kwargs: [sys.executable, "-c", child])
    monkeypatch.setenv(bridge.MODE_ENV, mode)
    cancel = threading.Event()

    def cancel_when_running():
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        cancel.set()

    trigger = threading.Thread(target=cancel_when_running, daemon=True)
    trigger.start()
    with pytest.raises(RuntimeError, match="cancelled"):
        bridge.run_result("wait", timeout=15, agy_path=sys.executable, cancel=cancel)
    trigger.join(1)
    assert started.exists()
    time.sleep(4.1)
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
        deadline = time.monotonic() + 8
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
