import pytest

from agy_headless_bridge import bridge

REAL_AGY_VERSION = bridge._agy_version  # before the autouse patch below


@pytest.fixture(autouse=True)
def _never_probe_real_agy(monkeypatch):
    """Tests stub find_agy() to "agy", which may be a real install: keep the
    version probe from spawning it and pin the pty path the stubs patch.
    Direct-mode tests opt in with AGY_BRIDGE_MODE=direct."""
    monkeypatch.setattr(bridge, "_agy_version", lambda path: ())
    monkeypatch.delenv(bridge.MODE_ENV, raising=False)


def _as_result(fn):
    """Wrap a str-returning fake `run` as a fake `run_result`."""
    return lambda *a, **k: bridge.AgyResult(fn(*a, **k))
