import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from agy_headless_bridge import bridge, mcp_server


FIXTURES = Path(__file__).parent / "fixtures" / "delegation"


def session(root, **options):
    from agy_headless_bridge._delegation import DelegationSession

    return DelegationSession(root, **options)


def invoke(scope, name, *, structured=False, **arguments):
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": name, "arguments": arguments}}
    result = mcp_server.handle_request(req, delegation=scope, structured=structured)["result"]
    message = result["content"][0]["text"]
    payload = json.loads(message)
    if structured:
        assert result["structuredContent"] == payload
    else:
        assert "structuredContent" not in result
    return payload, len(message) * (2 if structured else 1)


@pytest.fixture
def agy_catalog(monkeypatch):
    monkeypatch.setattr(bridge, "find_agy", lambda: "fake-agy")
    monkeypatch.setattr(bridge, "_agy_version", lambda *a, **kw: (1, 5, 18))
    monkeypatch.setattr(bridge, "_probe", lambda *a: ("  --json-schema\n", "", 0))
    monkeypatch.setattr(bridge, "list_models", lambda *a, **kw: {
        "gemini-4-hydrogen-low": "Hydrogen Low", "custom-model": "Custom"})


def test_document_acceptance(agy_catalog, monkeypatch, tmp_path):
    shutil.copy(FIXTURES / "relay.md", tmp_path)
    quote = "The frame payload is capped at 65,536 bytes."

    def answer(prompt, **options):
        assert "relay.md" in prompt and "What is the frame cap?" in prompt
        assert quote not in prompt
        assert options["mode"] == "plan"
        assert "--json-schema" in options["extra_args"]
        assert "--disable-slash-commands" not in options["extra_args"]
        staged, = map(Path, options["add_dirs"])
        assert staged != tmp_path and list(staged.iterdir()) == [staged / "relay.md"]
        assert quote in (staged / "relay.md").read_text(encoding="utf-8")
        structured = {"summary": "The relay frame cap is 65,536 bytes.",
            "evidence": [{"path": (staged / "relay.md").as_posix(), "start_line": 4,
                          "end_line": 4, "quote": quote}],
            "gaps": ["Rollback is unspecified."]}
        return bridge._finish(json.dumps({"status": "SUCCESS", "response": "Draft and fenced JSON",
                                          "structured_output": structured}), 0)

    monkeypatch.setattr(bridge, "run_result", answer)
    result, size = invoke(session(tmp_path), "agy_summarize_document", structured=True,
                          paths=["relay.md"], question="What is the frame cap?")
    assert result["status"] == "ok" and size <= 2000
    assert result["summary"] == "The relay frame cap is 65,536 bytes."
    assert result["evidence"] == [{"path": "relay.md", "start_line": 4, "end_line": 4,
                                   "quote": quote}]
    assert result["gaps"] == ["Rollback is unspecified."]


def test_noisy_pytest_acceptance(monkeypatch, tmp_path):
    shutil.copy(FIXTURES / "noisy_pytest.py", tmp_path / "test_relay.py")
    monkeypatch.setattr(bridge, "run_result", lambda *a, **kw: pytest.fail("Unexpected inference"))
    scope = session(tmp_path, pytest_enabled=True, retention=True)
    result, size = invoke(scope, "agy_run_tests", recipe="pytest", max_chars=800,
                          retain=True, structured=True)
    assert result["status"] == "failed" and size <= 800
    assert result["facts"]["exit_code"] == 1
    assert result["facts"]["failed_test"] == "test_relay.py::test_relay_contract"
    assert result["facts"]["counts"] == {"tests": 1, "failures": 1, "errors": 0, "skipped": 0}
    assert "The relay rejected a valid frame" in result["excerpt"]
    artifact = result["artifact_id"]
    excerpt, size = invoke(scope, "agy_read_artifact", artifact_id=artifact,
                           start_line=20, end_line=25, max_chars=512)
    assert excerpt["status"] == "ok" and size <= 512
    assert "relay noise" in excerpt["excerpt"]
    assert invoke(session(tmp_path, retention=True), "agy_read_artifact",
                  artifact_id=artifact)[0]["status"] == "artifact_missing"


def test_preflight_checks_preset_explicit_model_and_auth(agy_catalog, monkeypatch, tmp_path):
    scope = session(tmp_path)
    assert invoke(scope, "agy_preflight")[0]["facts"]["model"] == "gemini-4-hydrogen-low"
    assert invoke(scope, "agy_preflight", model="custom-model")[0]["status"] == "ok"
    missing, _ = invoke(scope, "agy_preflight", model="unknown")
    assert missing["status"] == "model_unavailable" and missing["facts"]["model"] == "unknown"
    monkeypatch.setattr(bridge, "_probe", lambda *a: ("", "", 0))
    assert invoke(scope, "agy_preflight")[0]["status"] == "incompatible"
    monkeypatch.setattr(bridge, "_probe", lambda *a: ("  --json-schema\n", "", 0))

    def denied(*args, **kwargs):
        raise bridge.AgyExitError("failed", 1, "Authentication required. " + "x" * 20000)

    monkeypatch.setattr(bridge, "list_models", denied)
    result, size = invoke(scope, "agy_preflight", max_chars=512)
    assert result["status"] == "auth_required" and size <= 512
    assert result["facts"]["model"] == "gemini-4-hydrogen-low"


def test_document_refuses_outside_paths_and_unverifiable_quotes(agy_catalog, monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    shutil.copy(FIXTURES / "relay.md", root)
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    scope = session(root)
    monkeypatch.setattr(bridge, "run_result", lambda *a, **k: pytest.fail("Unexpected inference"))
    for path in ("../outside.md", str(outside), ".git/config.txt"):
        assert invoke(scope, "agy_summarize_document", paths=[path], question="q")[0]["status"] == "denied"
    monkeypatch.setattr(bridge, "run_result", lambda *a, **k: bridge.AgyResult(json.dumps({
        "summary": "α😀" * 3000, "gaps": ["missing"],
        "evidence": [{"path": "relay.md", "start_line": 4, "end_line": 4, "quote": "made up"}]})))
    result, size = invoke(scope, "agy_summarize_document", paths=["relay.md"], question="q",
                          max_chars=512, structured=True)
    assert result["status"] == "partial" and size <= 512
    assert not result.get("evidence")


@pytest.mark.parametrize("raw", ["", "not JSON", "[]", '{"summary": "unverified"}'])
def test_bad_model_output_is_compact(agy_catalog, monkeypatch, tmp_path, raw):
    shutil.copy(FIXTURES / "relay.md", tmp_path)
    monkeypatch.setattr(bridge, "run_result", lambda *a, **k: bridge.AgyResult(raw))
    result, size = invoke(session(tmp_path), "agy_summarize_document", paths=["relay.md"],
                          question="q", max_chars=512)
    assert result["status"] == "unusable_model_output" and size <= 512


def test_pytest_pass_unknown_timeout_and_optional_summary(agy_catalog, monkeypatch, tmp_path):
    test_file = tmp_path / "test_example.py"
    test_file.write_text("def test_pass():\n    assert True\n", encoding="utf-8")
    scope = session(tmp_path, pytest_enabled=True)
    assert invoke(scope, "agy_run_tests", recipe="custom")[0]["status"] == "unknown_recipe"
    monkeypatch.setattr(bridge, "run_result", lambda *a, **k: pytest.fail("Unexpected inference"))
    passed, _ = invoke(scope, "agy_run_tests", recipe="pytest", retain=True)
    assert passed["status"] == "ok" and passed["facts"]["counts"]["tests"] == 1
    assert passed["artifact_status"] == "retention_disabled"
    test_file.write_text("import time\ndef test_slow():\n    time.sleep(30)\n", encoding="utf-8")
    timed, _ = invoke(scope, "agy_run_tests", recipe="pytest", timeout=1)
    assert timed["status"] == "timeout" and timed["facts"]["exit_code"] is None
    assert timed["facts"]["counts"] is None
    test_file.write_text("def test_fails():\n    assert False, 'helpful failure'\n", encoding="utf-8")

    def quota(*args, **kwargs):
        raise bridge.AgyQuotaError("quota reached", 1, "diagnostic overflow " * 1000)

    monkeypatch.setattr(bridge, "run_result", quota)
    failed, _ = invoke(scope, "agy_run_tests", recipe="pytest", summarize=True)
    assert failed["status"] == "failed" and failed["facts"]["exit_code"] == 1
    assert "helpful failure" in failed["excerpt"] and failed["model_status"] == "quota"


def test_artifacts_expire_reject_large_logs_and_require_full_opt_in(agy_catalog, monkeypatch, tmp_path):
    from agy_headless_bridge import _delegation

    shutil.copy(FIXTURES / "relay.md", tmp_path)
    clock = [100]
    monkeypatch.setattr(_delegation, "time", type("Clock", (), {"monotonic": staticmethod(lambda: clock[0])}))
    scope = session(tmp_path, retention=True)
    monkeypatch.setattr(bridge, "run_result", lambda *a, **k: bridge.AgyResult("diagnostic line\n" * 10))
    options = {"paths": ["relay.md"], "question": "q", "retain": True}
    result, _ = invoke(scope, "agy_summarize_document", **options)
    clock[0] += _delegation.ARTIFACT_TTL
    assert invoke(scope, "agy_read_artifact", artifact_id=result["artifact_id"])[0]["status"] == "artifact_expired"
    assert invoke(scope, "agy_preflight", output="full")[0]["status"] == "full_disabled"
    assert invoke(session(tmp_path, full=True), "agy_preflight", output="full")[0]["status"] == "ok"
    monkeypatch.setattr(bridge, "run_result", lambda *a, **k: bridge.AgyResult("x" * 70000))
    result, _ = invoke(scope, "agy_summarize_document", **options)
    assert result["artifact_status"] == "artifact_oversized" and "artifact_id" not in result


def test_lowest_budget_preserves_test_exit_and_identifier_with_optional_fields(monkeypatch, tmp_path):
    from agy_headless_bridge import _delegation

    test_id = "test_cases.py::test_" + "x" * 80
    partial = "FAILED misleading output\n=== short test summary info ===\nFAILED " + test_id
    monkeypatch.setattr(bridge, "_run_direct", lambda *a, **kw: (partial, 1))
    monkeypatch.setattr(_delegation, "_junit_counts", lambda report: {
        "tests": 123456, "failures": 1, "errors": 0, "skipped": 0})
    monkeypatch.setattr(_delegation, "_preflight", lambda *a: ({"status": "quota"}, None))
    result, size = invoke(session(tmp_path, pytest_enabled=True, retention=True), "agy_run_tests",
                          recipe="pytest", retain=True, summarize=True, model="x" * 64,
                          max_chars=512, structured=True)
    assert size <= 512 and result["status"] == "failed"
    assert result["facts"]["exit_code"] == 1 and result["facts"]["failed_test"] == test_id


def test_stdio_negotiates_structured_delegation_and_keeps_legacy_content(tmp_path):
    requests = [
        {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list"},
        {"id": 3, "method": "tools/call", "params": {"name": "agy_run_tests",
                                                      "arguments": {"recipe": "unknown"}}},
        {"id": 4, "method": "tools/call", "params": {"name": "agy_ask", "arguments": {}}},
    ]
    raw = "".join(json.dumps({"jsonrpc": "2.0", **request}) + "\n" for request in requests)
    proc = subprocess.run([sys.executable, "-m", "agy_headless_bridge.mcp_server",
                           "--delegation-root", str(tmp_path), "--enable-pytest"],
                          input=raw, capture_output=True, text=True, timeout=20, check=True)
    results = {item["id"]: item["result"] for item in map(json.loads, proc.stdout.splitlines())}
    assert results[1]["protocolVersion"] == "2025-06-18"
    assert "agy_run_tests" in {tool["name"] for tool in results[2]["tools"]}
    assert results[3]["structuredContent"]["status"] == "unknown_recipe"
    assert results[4]["isError"] and "structuredContent" not in results[4]
