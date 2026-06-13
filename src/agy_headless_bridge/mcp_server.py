#!/usr/bin/env python3
"""
agy_headless_bridge.mcp_server — A minimal MCP stdio server that exposes the
Google Antigravity CLI (`agy`) as callable tools.

This lets any MCP client (Claude Code, etc.) delegate prompts to `agy`. It runs
agy through the pty bridge (bridge.run), so it works in non-TTY contexts where a
plain `agy -p` would silently emit nothing (upstream bug #76).

Register with Claude Code:

    claude mcp add --transport stdio antigravity -- \
        python -m agy_headless_bridge.mcp_server

Or add to your MCP config manually:

    {
      "mcpServers": {
        "antigravity": {
          "command": "python",
          "args": ["-m", "agy_headless_bridge.mcp_server"]
        }
      }
    }

Tools exposed:
    - agy_ask(prompt: str)     -> str : one-shot prompt to Antigravity
    - agy_research(query: str) -> str : deep-research framing of a query

No third-party MCP SDK required — this speaks the JSON-RPC stdio framing
directly so the package stays dependency-light.
"""

from __future__ import annotations

import json
import sys

from .bridge import AgyNotFoundError, run

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "agy_ask",
        "description": (
            "Send a one-shot prompt to the Google Antigravity CLI (agy) and "
            "return its response. Use to delegate a focused coding, debugging, "
            "or reasoning task to Gemini via Antigravity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The prompt to send to agy"}
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "agy_research",
        "description": "Ask Antigravity to research a topic deeply.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Research query or topic"}
            },
            "required": ["query"],
        },
    },
]


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _call_agy(prompt: str) -> str:
    try:
        out = run(prompt)
    except AgyNotFoundError as exc:
        return f"[agy-mcp] ERROR: {exc}"
    except TimeoutError as exc:
        return f"[agy-mcp] ERROR: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        return f"[agy-mcp] ERROR: {exc}"
    if not out:
        return "[agy-mcp] agy returned no output."
    return out


def handle_request(req: dict) -> dict | None:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {}) or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agy-headless-bridge", "version": "1.0.1"},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        if name == "agy_ask":
            result = _call_agy(args.get("prompt", ""))
        elif name == "agy_research":
            result = _call_agy(f"Do deep research on: {args.get('query', '')}")
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": result}]},
        }

    if method == "notifications/initialized":
        return None  # notifications get no response

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            if resp is not None:
                _send(resp)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "Parse error"}})
        except Exception as exc:  # pragma: no cover - defensive
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32603, "message": str(exc)}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
