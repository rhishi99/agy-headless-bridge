"""Opt-in $AGY_BRIDGE_ON_QUOTA hook: run once on a spent quota, then retry once."""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agy_headless_bridge import bridge


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake(prompt, *args, **kwargs):
        seen.append(prompt)
        if len(seen) == 1:
            raise bridge.AgyQuotaError("quota reached", 1, "")
        return "ok"

    monkeypatch.setattr(bridge, "_run_once", fake)
    return seen


def _hook(tmp_path, code):
    marker = tmp_path / "ran"
    return marker, f'"{sys.executable}" -c "open(r\'{marker}\', \'w\'); raise SystemExit({code})"'


def test_no_hook_raises_quota(calls, monkeypatch):
    monkeypatch.delenv(bridge.ON_QUOTA_ENV, raising=False)
    with pytest.raises(bridge.AgyQuotaError):
        bridge.run_result("p")
    assert calls == ["p"]


def test_hook_success_retries_once(calls, monkeypatch, tmp_path):
    marker, cmd = _hook(tmp_path, 0)
    monkeypatch.setenv(bridge.ON_QUOTA_ENV, cmd)
    assert bridge.run_result("p") == "ok"
    assert marker.exists() and calls == ["p", "p"]


def test_hook_failure_keeps_quota_error(calls, monkeypatch, tmp_path):
    marker, cmd = _hook(tmp_path, 1)
    monkeypatch.setenv(bridge.ON_QUOTA_ENV, cmd)
    with pytest.raises(bridge.AgyQuotaError):
        bridge.run_result("p")
    assert marker.exists() and calls == ["p"]


def test_missing_hook_keeps_quota_error(calls, monkeypatch, tmp_path):
    monkeypatch.setenv(bridge.ON_QUOTA_ENV, str(tmp_path / "no-such-hook"))
    with pytest.raises(bridge.AgyQuotaError):
        bridge.run_result("p")


def test_parallel_quota_runs_hook_once(monkeypatch, tmp_path):
    # Two jobs share one account and run out together: one swap, not two.
    log = tmp_path / "hook.log"
    monkeypatch.setenv(bridge.ON_QUOTA_ENV,
                       f'"{sys.executable}" -c "open(r\'{log}\', \'a\').write(\'x\')"')
    barrier, failed = threading.Barrier(2), set()

    def fake(prompt, *args, **kwargs):
        if prompt not in failed:
            failed.add(prompt)
            barrier.wait()
            raise bridge.AgyQuotaError("quota reached", 1, "")
        return prompt

    monkeypatch.setattr(bridge, "_run_once", fake)
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(bridge.run_result, ["a", "b"])) == ["a", "b"]
    assert log.read_text() == "x"
