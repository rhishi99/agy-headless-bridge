from types import SimpleNamespace

import pytest

from agy_headless_bridge import bridge


def test_aliases_follow_live_catalog_and_refresh_after_an_hour(monkeypatch):
    now = [100]
    calls = []

    def models(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "Fetching available models...\n"
                "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n"
                f"gemini-{len(calls) + 7}.2-flash-high\tGemini Flash (High)\n"
                "gemini-5.1-flash-low\tGemini 5.1 Flash (Low)\n"
                "gemini-5.1-pro-high\tGemini 5.1 Pro (High)\n"
            ),
        )

    monkeypatch.setattr(bridge.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(bridge.subprocess, "run", models)
    monkeypatch.setattr(bridge, "_run_direct", lambda argv, *args: (
        argv[argv.index("--model") + 1], 0
    ))
    monkeypatch.setenv(bridge.MODE_ENV, "direct")

    def run(model, path="fixture-agy"):
        return bridge.run_result("hello", agy_path=path, model=model).text

    assert run("custom-model") == "custom-model"
    assert not calls
    assert run("flash") == "gemini-8.2-flash-high"
    assert run("flash-high") == "gemini-8.2-flash-high"
    assert run("flash-low") == "gemini-5.1-flash-low"
    assert run("pro") == "gemini-5.1-pro-high"
    assert len(calls) == 1
    assert run("flash", "different-agy") == "gemini-9.2-flash-high"
    now[0] += 3600
    assert run("flash") == "gemini-10.2-flash-high"
    assert all(argv[1:] == ["models"] and 0 < timeout < 60 for argv, timeout in calls)


def test_unavailable_alias_does_not_silently_change_family(monkeypatch):
    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="gemini-4-hydrogen-high\tGemini 4 Hydrogen (High)\n",
    ))
    monkeypatch.setattr(bridge, "_pty_run", lambda *args, **kwargs: pytest.fail("spawned"))
    with pytest.raises(ValueError, match="alias 'pro'.*unavailable"):
        bridge.run_result("hello", agy_path="fixture-no-pro", model="pro")
