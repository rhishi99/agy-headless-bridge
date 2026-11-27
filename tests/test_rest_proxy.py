import http.client
import json
import threading

import pytest

from agy_headless_bridge import bridge


@pytest.fixture
def proxy(monkeypatch):
    from agy_headless_bridge import rest_proxy

    observed = []
    monkeypatch.setattr(rest_proxy, "find_agy", lambda: "fixture-agy")
    monkeypatch.setattr(rest_proxy, "list_models", lambda path: {"gemini-3.8-flash-high": "Flash"})
    monkeypatch.setattr(rest_proxy, "resolve_model", lambda path, model: "gemini-3.8-flash-high")

    def run(prompt, **kwargs):
        observed.append((prompt, kwargs))
        return bridge.AgyResult("Hello ₹", usage={"input_tokens": 7, "output_tokens": 2})

    monkeypatch.setattr(rest_proxy, "run_result", run)
    with rest_proxy.make_server(0, "secret") as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port, observed
        finally:
            server.shutdown()
            thread.join(3)


def request(port, body=None, **headers):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("POST" if body is not None else "GET",
                           "/v1/chat/completions" if body is not None else "/v1/models",
                           body=json.dumps(body) if body is not None else None,
                           headers={"Content-Type": "application/json", **headers})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_chat_and_models_use_the_bridge(proxy):
    port, observed = proxy
    auth = {"Authorization": "Bearer secret"}
    status, raw = request(port, **auth)
    assert status == 200
    assert json.loads(raw)["data"][0]["id"] == "gemini-3.8-flash-high"
    status, raw = request(port, {
        "model": "flash", "messages": [{"role": "system", "content": "Be concise"},
                                       {"role": "user", "content": "Say hello"}],
    }, **auth)
    assert status == 200
    reply = json.loads(raw)
    assert reply["object"] == "chat.completion"
    assert reply["model"] == "gemini-3.8-flash-high"
    assert reply["choices"][0]["message"] == {"role": "assistant", "content": "Hello ₹"}
    assert reply["usage"] == {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
    prompt, kwargs = observed[0]
    assert "Be concise" in prompt and "Say hello" in prompt
    assert kwargs == {"agy_path": "fixture-agy", "model": "gemini-3.8-flash-high", "mode": "plan"}


def test_http_validation_does_not_run_agy(proxy):
    port, observed = proxy
    body = {"model": "flash", "messages": [{"role": "user", "content": "Hi"}]}
    auth = {"Authorization": "Bearer secret"}
    assert request(port, body)[0] == 401
    assert request(port, body, Origin="https://example.test", **auth)[0] == 403
    assert request(port, body, Host="example.test", **auth)[0] == 403
    assert request(port, {**body, "messages": [{"role": "tool", "content": "Hi"}]}, **auth)[0] == 400
    assert request(port, {**body, "tools": [{}]}, **auth)[0] == 400
    assert not observed


def test_stream_uses_chat_chunks_and_done_marker(proxy):
    port, observed = proxy
    status, raw = request(port, {
        "model": "flash", "messages": [{"role": "user", "content": "Say hello"}],
        "stream": True,
    }, Authorization="Bearer secret")
    assert status == 200
    events = raw.decode().splitlines()
    assert "data: [DONE]" in events
    chunks = [json.loads(line[6:]) for line in events if line.startswith("data: {")]
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert chunks[0]["choices"][0]["delta"]["content"] == "Hello ₹"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert observed[0][0] == "Say hello"


def test_agy_print_deadline_maps_to_gateway_timeout(proxy, monkeypatch):
    from agy_headless_bridge import rest_proxy

    def timeout(*args, **kwargs):
        raise bridge.AgyExitError("agy exited with status 3", 3, "print timeout after 55s")

    monkeypatch.setattr(rest_proxy, "run_result", timeout)
    port, _ = proxy
    status, raw = request(port, {
        "model": "flash", "messages": [{"role": "user", "content": "Say hello"}],
    }, Authorization="Bearer secret")
    assert status == 504
    assert json.loads(raw)["error"]["type"] == "upstream_error"


def test_serve_subcommand_dispatches_without_changing_normal_prompt(monkeypatch):
    from agy_headless_bridge import __main__, rest_proxy

    monkeypatch.setattr(rest_proxy, "main", lambda argv: 37 if argv == ["--port", "8080"] else 1)
    assert __main__.main(["serve", "--port", "8080"]) == 37
    monkeypatch.setattr(bridge, "run_result", lambda prompt, **kwargs: bridge.AgyResult(prompt))
    assert bridge.main(["serve"]) == 0
