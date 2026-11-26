"""Local OpenAI Chat Completions adapter for the AGY bridge."""

from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import sys
import time
import uuid

from .bridge import (
    AgyExitError, AgyNotFoundError, AgyQuotaError, AgyTimeoutError,
    find_agy, list_models, resolve_model, run_result,
)

API_KEY_ENV = "AGY_BRIDGE_API_KEY"
_MAX_BODY = 256 * 1024
_ROLES = {"system", "developer", "user", "assistant"}


class _Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *args):
        pass

    def _reply(self, status, payload, content_type="application/json"):
        if content_type == "application/json":
            payload = json.dumps(payload)
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message, kind="invalid_request_error"):
        self._reply(status, {"error": {"message": message, "type": kind}})

    def _body_length(self):
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            return None
        if len(lengths[0]) > len(str(_MAX_BODY)):
            return _MAX_BODY + 1
        return int(lengths[0])

    def _authorized(self):
        port = self.server.server_port
        host = self.headers.get("Host", "").lower()
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}") or "Origin" in self.headers:
            error = (403, "Use a local HTTP client on the proxy's loopback address")
        elif not hmac.compare_digest(self.headers.get("Authorization", "").encode("utf-8"),
                                     self.server.authorization):
            error = (401, f"Supply the {API_KEY_ENV} value as a Bearer token", "authentication_error")
        else:
            return True
        length = self._body_length()
        if self.command == "POST" and length is not None and 0 < length <= _MAX_BODY:
            try:
                self.rfile.read(length)
            except (TimeoutError, ConnectionError):
                return False
        self._error(*error)
        return False

    def do_GET(self):
        if self._authorized():
            self._dispatch("/v1/models", self._models)

    def do_POST(self):
        if self._authorized():
            self._dispatch("/v1/chat/completions", self._chat)

    def _dispatch(self, route, handler):
        if self.path != route:
            self._error(404, "Unknown endpoint")
            return
        try:
            path = find_agy()
            if not path:
                raise AgyNotFoundError()
            handler(path)
        except AgyTimeoutError:
            self._error(504, "AGY timed out", "upstream_error")
        except AgyQuotaError as exc:
            wait = f" Try again in about {exc.reset_seconds}s." if exc.reset_seconds else ""
            self._error(429, f"AGY quota exhausted.{wait}", "rate_limit_error")
        except AgyNotFoundError:
            self._error(503, "The AGY executable is unavailable", "upstream_error")
        except AgyExitError as exc:
            if exc.returncode == 3 and exc.output.lower().startswith("print timeout"):
                self._error(504, "AGY reached its print deadline", "upstream_error")
            else:
                self._error(502, "AGY failed. Check the model ID and AGY login.", "upstream_error")
        except ValueError as exc:
            self._error(400, str(exc))
        except ConnectionError:
            pass
        except OSError:
            self._error(502, "Could not run AGY", "upstream_error")

    def _models(self, path):
        models = list_models(path)
        self._reply(200, {"object": "list", "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "antigravity"}
            for model in models
        ]})

    def _chat(self, path):
        if "Transfer-Encoding" in self.headers or self.headers.get_content_type() != "application/json":
            self._error(415, "Send an application/json body with Content-Length")
            return
        length = self._body_length()
        if length is None:
            self._error(411, "One valid Content-Length is required")
            return
        if length > _MAX_BODY:
            self._error(413, "Request body exceeds 256 KiB")
            return
        try:
            body = json.loads(self.rfile.read(length))
        except TimeoutError:
            self._error(408, "Request body timed out")
            return
        except (ValueError, RecursionError):
            self._error(400, "Malformed JSON request body")
            return
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        model, messages = body.get("model"), body.get("messages")
        if not isinstance(model, str) or not model.strip() or model.startswith("-"):
            raise ValueError("model must be a non-empty model ID or alias")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty list")
        for message in messages:
            if (not isinstance(message, dict) or not isinstance(message.get("role"), str)
                    or message["role"] not in _ROLES or not isinstance(message.get("content"), str)
                    or "tool_calls" in message or "function_call" in message):
                raise ValueError("Only system, developer, user and assistant text messages are supported")
        if (body.get("n", 1) != 1 or body.get("tools") or body.get("functions")
                or body.get("response_format") not in (None, {"type": "text"})):
            raise ValueError("Only one text completion per request is supported")
        stream = body.get("stream", False)
        if not isinstance(stream, bool):
            raise ValueError("stream must be a boolean")
        stream_options = body.get("stream_options") or {}
        if not isinstance(stream_options, dict):
            raise ValueError("stream_options must be an object")
        transcript = [{key: message[key] for key in ("role", "content")} for message in messages]
        first = transcript[0]
        if len(transcript) == 1 and first["role"] == "user" and first["content"].strip() and "\x00" not in first["content"]:
            prompt = first["content"]
        else:
            prompt = "Respond as the assistant to this chat transcript:\n" + json.dumps(transcript, ensure_ascii=False)
        try:
            prompt.encode("utf-8")
        except UnicodeError:
            raise ValueError("Message content contains invalid Unicode") from None
        if "\x00" in model or len(model) > 256:
            raise ValueError("Invalid model ID")
        model = resolve_model(path, model)
        result = run_result(prompt, agy_path=path, model=model, mode="plan")
        if not result.text:
            self._error(502, "AGY returned no response", "upstream_error")
            return
        base = {"id": f"chatcmpl-{uuid.uuid4().hex}", "created": int(time.time()), "model": model}
        usage = {}
        raw = result.usage or {}
        if all(type(raw.get(key)) is int and raw[key] >= 0 for key in ("input_tokens", "output_tokens")):
            usage["usage"] = {
                "prompt_tokens": raw["input_tokens"], "completion_tokens": raw["output_tokens"],
                "total_tokens": raw["input_tokens"] + raw["output_tokens"],
            }
        message = {"role": "assistant", "content": result.text}
        if stream:
            base["object"] = "chat.completion.chunk"
            chunks = [
                {**base, "choices": [{"index": 0, "delta": message, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ]
            if stream_options.get("include_usage") is True and usage:
                chunks.append({**base, "choices": [], **usage})
            events = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            self._reply(200, events + "data: [DONE]\n\n", "text/event-stream")
        else:
            self._reply(200, {
                **base, "object": "chat.completion", **usage,
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            })


def make_server(port, api_key):
    server = HTTPServer(("127.0.0.1", port), _Handler)
    server.authorization = f"Bearer {api_key}".encode("utf-8")
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agy-headless-bridge serve",
                                     description="Serve an OpenAI-compatible AGY endpoint over loopback.")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    api_key = os.environ.pop(API_KEY_ENV, "")
    if not api_key or not api_key.isascii() or not api_key.isprintable() or any(c.isspace() for c in api_key):
        parser.error(f"Set {API_KEY_ENV} to a secret without spaces")
    try:
        with make_server(args.port, api_key) as server:
            print(f"AGY serving at http://127.0.0.1:{server.server_port}/v1", file=sys.stderr, flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"[agy-bridge] {exc}", file=sys.stderr)
        return 1
    return 0
