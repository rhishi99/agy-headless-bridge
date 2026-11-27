import pytest

from agy_headless_bridge import bridge, mcp_server


@pytest.fixture
def guarded(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "generated").mkdir()
    outside = tmp_path / "project-other"
    outside.mkdir()
    launcher = tmp_path / "guard.exe"
    launcher.touch()
    monkeypatch.chdir(root)
    monkeypatch.setenv("AGY_BRIDGE_GUARD_LAUNCHER", str(launcher))
    monkeypatch.setenv(bridge.MODE_ENV, "direct")
    monkeypatch.setattr(bridge, "find_agy", lambda: "fixture-agy")
    return root, outside, launcher


def test_guard_rejects_paths_before_any_agy_process(guarded, monkeypatch):
    root, outside, _ = guarded
    monkeypatch.setattr(bridge, "_use_direct", lambda *args: pytest.fail("spawned"))
    for path, message in (
        (outside, "outside allowed roots"), (root / ".git", "denied by"),
        (root / "generated", "denied by"), (root / ".." / outside.name, "outside allowed roots"),
    ):
        with pytest.raises(ValueError, match=message):
            bridge.run_result("hello", add_dirs=[str(path)], allowed_roots=[str(root)],
                              deny_patterns=["gen*"])


def test_guard_applies_cli_policy_to_real_invocation_contract(guarded, monkeypatch, capsys):
    root, _, launcher = guarded
    commands = []

    def direct(argv, *args, **kwargs):
        commands.append(argv)
        return "ok", 0

    monkeypatch.setattr(bridge, "_run_direct", direct)
    assert bridge.main(["--allowed-roots", ".", "--deny-patterns", "gen*", "hello"]) == 0
    prefix, child = commands[0][:commands[0].index("--")], commands[0][commands[0].index("--") + 1:]
    assert prefix == [
        str(launcher), "--allowed-roots", str(root), "--deny-patterns", ".git",
        "--deny-patterns", ".ssh", "--deny-patterns", "gen*",
    ]
    assert child[:3] == ["fixture-agy", "--add-dir", str(root)]
    assert "ok" in capsys.readouterr().out


def test_guarded_mcp_startup_policy_fails_closed(guarded, monkeypatch, capsys):
    root, _, _ = guarded
    monkeypatch.delenv("AGY_BRIDGE_GUARD_LAUNCHER")
    monkeypatch.setattr(bridge, "find_agy", lambda: pytest.fail("attempted unguarded run"))
    assert mcp_server.main(["--allowed-roots", str(root), "--deny-patterns", "*.pem"]) == 2
    assert "AGY_BRIDGE_GUARD_LAUNCHER" in capsys.readouterr().err


def test_guard_refuses_unconfigured_launcher(guarded, monkeypatch):
    root, _, _ = guarded
    monkeypatch.delenv("AGY_BRIDGE_GUARD_LAUNCHER")
    monkeypatch.setattr(bridge, "find_agy", lambda: pytest.fail("attempted unguarded run"))
    with pytest.raises(ValueError, match="AGY_BRIDGE_GUARD_LAUNCHER"):
        bridge.run_result("hello", allowed_roots=[str(root)])
