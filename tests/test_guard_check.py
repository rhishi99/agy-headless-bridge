import subprocess
import sys
from pathlib import Path

from agy_headless_bridge import _guard_check


def test_checks_fail_when_launcher_is_unconfigured(monkeypatch, capsys):
    monkeypatch.delenv("AGY_BRIDGE_GUARD_LAUNCHER", raising=False)

    assert _guard_check.main() == 1
    assert "AGY_BRIDGE_GUARD_LAUNCHER" in capsys.readouterr().err


def test_checks_reject_a_launcher_that_allows_all_writes(monkeypatch, capsys):
    monkeypatch.setattr(_guard_check, "guarded_command", lambda *args: ((), []))

    assert _guard_check.main() == 1
    failure = capsys.readouterr().err
    for case in (".git", ".ssh", "blocked-existing.txt", "blocked-new.txt", "outside"):
        assert case in failure
    assert "writer reported an allowed prohibited write" in failure


def test_checks_require_successful_allowed_writes_by_the_child(monkeypatch, capsys):
    monkeypatch.setattr(_guard_check, "guarded_command", lambda *args: ((), []))

    def empty_probe(argv, timeout, cancel):
        result = subprocess.run([sys.executable, "-c", "pass"], timeout=timeout)
        return "", "", result.returncode

    monkeypatch.setattr(_guard_check, "_probe", empty_probe)

    assert _guard_check.main() == 1
    assert "allowed.txt" in capsys.readouterr().err


def test_checks_accept_a_preserved_result_and_allowed_write(monkeypatch, capsys):
    def guarded(roots, patterns):
        assert patterns == ["blocked-*.txt"]
        return ("fixture-launcher", str(Path(roots[0]) / "allowed.txt")), []

    def check_probe(argv, timeout, cancel):
        assert argv[0] == "fixture-launcher"
        Path(argv[1]).write_bytes(b"checked")
        return "AGY_GUARD_CHECK\n", "", 73

    monkeypatch.setattr(_guard_check, "guarded_command", guarded)
    monkeypatch.setattr(_guard_check, "_probe", check_probe)

    assert _guard_check.main() == 0
    assert "passed" in capsys.readouterr().out
