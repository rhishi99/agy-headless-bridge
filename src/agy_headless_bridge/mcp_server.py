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
import os
import sys

from . import __version__
from .bridge import (
    AgyExitError,
    AgyNotFoundError,
    AgyQuotaError,
    AgyTimeoutError,
    force_utf8_stdio,
    resolve_add_dirs,
    run,
)

# `skip_permissions` lets agy edit files / run commands unattended. The MCP
# caller is itself a model, so the tool argument alone must not be able to
# grant that: the human who launches the server opts in with this env var.
SKIP_PERMISSIONS_ENV = "AGY_BRIDGE_ALLOW_SKIP_PERMISSIONS"

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
                "prompt": {"type": "string", "description": "The prompt to send to agy"},
                "add_dir": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Explicit directories to add to agy's workspace. "
                                   "Overrides the workspace default below.",
                },
                "workspace": {
                    "type": "string",
                    "enum": ["auto", "none"],
                    "description": "auto (default): if no add_dir given, add the "
                                   "server's cwd so agy sees the repo — needed for "
                                   "coding tasks. none: no workspace (use for "
                                   "research / Q&A that needs no repo context).",
                },
                "model": {"type": "string", "description": "agy --model (optional)"},
                "timeout": {
                    "type": "number",
                    "description": "Hard timeout in seconds (override when a task "
                                   "legitimately needs longer).",
                },
                "skip_permissions": {
                    "type": "boolean",
                    "description": "Let agy edit files / run commands without "
                                   "approval prompts (headless agy otherwise "
                                   "declines them silently). Only honoured if the "
                                   f"server was started with {SKIP_PERMISSIONS_ENV}=1.",
                },
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


def _with_output(output: str, note: str) -> str:
    return f"{output}\n\n{note}" if output else note


def _call_agy(
    prompt: str,
    add_dirs: list | None = None,
    model: str | None = None,
    timeout: float | None = None,
    skip_permissions: bool = False,
) -> tuple[str, bool]:
    """Run agy; return (text, is_error) for an MCP tool result."""
    kwargs: dict = {
        "add_dirs": add_dirs, "model": model, "skip_permissions": skip_permissions,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        out = run(prompt, **kwargs)
    except AgyNotFoundError as exc:
        return f"[agy-mcp] ERROR: {exc}", True
    except AgyTimeoutError as exc:
        # Surface partial work so the caller isn't left empty-handed; it can
        # resume the agy session with `agy -c`.
        note = f"[agy-mcp] TIMEOUT: {exc}; resume with 'agy -c'"
        return _with_output(exc.partial, note), True
    except AgyQuotaError as exc:
        when = f"; resets in ~{exc.reset_seconds}s" if exc.reset_seconds else ""
        return _with_output(exc.output, f"[agy-mcp] QUOTA: {exc}{when}"), True
    except AgyExitError as exc:
        return _with_output(exc.output, f"[agy-mcp] ERROR: {exc}"), True
    except Exception as exc:  # pragma: no cover - defensive
        return f"[agy-mcp] ERROR: {exc}", True
    if not out:
        return "[agy-mcp] agy returned no output.", True
    return out, False


def _tool_error(req_id, text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


def handle_request(req: dict) -> dict | None:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {}) or {}

    if "id" not in req:
        # A notification (initialized, cancelled, progress...). JSON-RPC
        # forbids replying to one, even with an error.
        return None

    if method == "initialize":
        # We only speak one protocol version; always report it so the client
        # can decide whether to proceed.
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agy-headless-bridge", "version": __version__},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        if name == "agy_ask":
            # Coding-shaped by default: inject cwd unless caller opts out
            # (workspace="none") or names dirs explicitly.
            skip = bool(args.get("skip_permissions"))
            if skip and os.environ.get(SKIP_PERMISSIONS_ENV) != "1":
                return _tool_error(
                    req_id,
                    "[agy-mcp] ERROR: skip_permissions refused — restart the "
                    f"server with {SKIP_PERMISSIONS_ENV}=1 to allow it.",
                )
            add_dirs = resolve_add_dirs(
                args.get("add_dir"),
                use_cwd_default=args.get("workspace", "auto") != "none",
            )
            result, is_error = _call_agy(
                args.get("prompt", ""),
                add_dirs=add_dirs,
                model=args.get("model"),
                timeout=args.get("timeout"),
                skip_permissions=skip,
            )
        elif name == "agy_research":
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                return _tool_error(req_id, "[agy-mcp] ERROR: 'query' must be a non-empty string")
            # Research never needs the repo — no workspace, keeps agy's context lean.
            result, is_error = _call_agy(f"Do deep research on: {query}")
        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {name}"},
            }
        if is_error:
            return _tool_error(req_id, result)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"content": [{"type": "text", "text": result}]},
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def main() -> int:
    # Force UTF-8 on stdio regardless of the host locale (e.g. cp936/GBK on
    # Chinese Windows, cp1252 on Western). MCP clients serialize JSON-RPC as
    # UTF-8; reading with the locale codec mangles non-ASCII prompts into
    # mojibake + surrogate escapes, which later panics pywinpty's UTF-16
    # conversion when agy's argv is built (pyo3 "assertion `left == right`
    # failed").
    force_utf8_stdio()
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
