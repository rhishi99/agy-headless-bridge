#!/usr/bin/env python3
"""
agy_headless_bridge.mcp_server — A minimal MCP stdio server that exposes the
Google Antigravity CLI (`agy`) as callable tools.

This lets any MCP client (Claude Code, etc.) delegate prompts to `agy`. It runs
agy through bridge.run_result (plain pipes + JSON on agy >= 1.2.11, a fresh pty
on older agy, where a plain `agy -p` silently emits nothing: upstream bug #76).

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

import argparse
import json
import os
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import __version__
from ._guard import add_guard_arguments, guarded_command
from .bridge import (
    AgyExitError,
    AgyNotFoundError,
    AgyQuotaError,
    AgyTimeoutError,
    BRIEF_INSTRUCTION,
    DEFAULT_TIMEOUT,
    cap_output,
    force_utf8_stdio,
    resolve_add_dirs,
    result_notes,
    run_result,
)

# `skip_permissions` lets agy edit files / run commands unattended. The MCP
# caller is itself a model, so the tool argument alone must not be able to
# grant that: the human who launches the server opts in with this env var.
SKIP_PERMISSIONS_ENV = "AGY_BRIDGE_ALLOW_SKIP_PERMISSIONS"

PROTOCOL_VERSION = "2024-11-05"
STRUCTURED_PROTOCOL_VERSION = "2025-06-18"

# Replies longer than this come back as their SUMMARY block (or tail) plus the
# path of a file holding the full text, so one long agy run cannot flood the
# calling model's context. 0 = no cap.
MAX_CHARS_ENV = "AGY_BRIDGE_MAX_CHARS"
DEFAULT_MAX_CHARS = 4000
MAX_PENDING_JOBS = 8
_SCOPED_READ_TOOLS = {"agy_preflight", "agy_summarize_document", "agy_read_artifact",
                      "agy_git_diff", "agy_public_research", "agy_inspect_media"}

_CAP_PROPS = {
    "brief": {
        "type": "boolean",
        "description": "default true: ask agy to end with a short SUMMARY: "
                       "block, returned alone when the reply is long.",
    },
    "max_chars": {
        "type": "integer",
        "description": "cap on returned text (default "
                       f"${MAX_CHARS_ENV} or {DEFAULT_MAX_CHARS}; 0 = no cap). "
                       "Past it the full reply is saved to a file named in the result.",
    },
}

# Shared by both tools. `mode` is gated in handle_request: accept-edits / auto
# approve agy's edits, so like skip_permissions they need the human's opt-in.
_SESSION_PROPS = {
    "effort": {
        "type": "string",
        "enum": ["low", "medium", "high", "xhigh", "max"],
        "description": "agy --effort (reasoning effort; optional)",
    },
    "conversation": {
        "type": "string",
        "description": "Resume an earlier agy conversation: pass the id a "
                       "previous result named ('conversation <id>').",
    },
}

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
                **_CAP_PROPS,
                **_SESSION_PROPS,
                "mode": {
                    "type": "string",
                    "enum": ["plan", "accept-edits", "auto"],
                    "description": "agy --mode. plan = read-only, and agy may answer "
                                   "with a plan instead of doing the task. accept-edits / "
                                   "auto approve edits, so like skip_permissions "
                                   f"they need {SKIP_PERMISSIONS_ENV}=1.",
                },
                "heartbeat": {
                    "type": "boolean",
                    "description": "Long task: agy logs each step to a progress "
                                   "file and the bridge counts that as life, so "
                                   "a stall dies in minutes while a working run "
                                   "lives to the full timeout.",
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
                "query": {"type": "string", "description": "Research query or topic"},
                "model": {"type": "string", "description": "agy --model (optional)"},
                "timeout": {
                    "type": "number",
                    "description": f"seconds (default {int(DEFAULT_TIMEOUT)}); "
                                   "agy is silent until done, so this is also "
                                   "the idle bound",
                },
                **_CAP_PROPS,
                **_SESSION_PROPS,
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


def _max_chars(value) -> int:
    """The output cap from the tool argument, else the env var, else default."""
    if value is None:
        value = os.environ.get(MAX_CHARS_ENV, DEFAULT_MAX_CHARS)
    if isinstance(value, bool):
        raise ValueError(f"max_chars must be an integer >= 0, got {value!r}")
    try:
        cap = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"max_chars must be an integer >= 0, got {value!r}") from None
    if cap < 0:
        raise ValueError(f"max_chars must be an integer >= 0, got {value!r}")
    return cap


def _call_agy(
    prompt: str,
    timeout: float | None = None,
    max_chars: int = 0,
    **kwargs,
) -> tuple[str, bool]:
    """Run agy; return (text, is_error) for an MCP tool result. agy's text is
    cut to `max_chars` (see cap_output); the bridge's own notes never are.
    Other kwargs go to run_result (add_dirs, model, mode, heartbeat...)."""
    if timeout is not None:
        kwargs["timeout"] = timeout  # run_result() lifts the idle bound to match
    try:
        res = run_result(prompt, **kwargs)
    except AgyNotFoundError as exc:
        return f"[agy-mcp] ERROR: {exc}", True
    except AgyTimeoutError as exc:
        # Surface partial work so the caller isn't left empty-handed; it can
        # resume the agy session with `agy -c`.
        note = f"[agy-mcp] TIMEOUT: {exc}; resume with 'agy -c'"
        return _with_output(cap_output(exc.partial, max_chars), note), True
    except AgyQuotaError as exc:
        when = f"; resets in ~{exc.reset_seconds}s" if exc.reset_seconds else ""
        note = f"[agy-mcp] QUOTA: {exc}{when}"
        return _with_output(cap_output(exc.output, max_chars), note), True
    except AgyExitError as exc:
        note = f"[agy-mcp] ERROR: {exc}"
        return _with_output(cap_output(exc.output, max_chars), note), True
    except ValueError as exc:  # bad timeout / prompt, caught before agy spawns
        return f"[agy-mcp] ERROR: {exc}", True
    except Exception as exc:  # pragma: no cover - defensive
        return f"[agy-mcp] ERROR: {exc}", True
    if not res.text:
        return "[agy-mcp] agy returned no output.", True
    out = cap_output(res.text, max_chars)
    for note in result_notes(res):
        out = _with_output(out, f"[agy-mcp] {note}")
    return out, False


def _tool_error(req_id, text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


def handle_request(req: dict, *, run_options=None, delegation=None, structured=False) -> dict | None:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {}) or {}

    if "id" not in req:
        # A notification (initialized, cancelled, progress...). JSON-RPC
        # forbids replying to one, even with an error.
        return None

    if method == "initialize":
        requested = params.get("protocolVersion")
        protocol = PROTOCOL_VERSION
        if delegation is not None and requested != PROTOCOL_VERSION:
            protocol = STRUCTURED_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": protocol,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agy-headless-bridge", "version": __version__},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        tools = TOOLS if delegation is None else [*TOOLS, *delegation.tools]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {})
        if delegation is not None and any(tool["name"] == name for tool in delegation.tools):
            result = delegation.call(name, args, run_options or {}, structured)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        args = args or {}
        if name in ("agy_ask", "agy_research"):
            try:
                max_chars = _max_chars(args.get("max_chars"))
            except ValueError as exc:
                return _tool_error(req_id, f"[agy-mcp] ERROR: {exc}")
            brief = BRIEF_INSTRUCTION if args.get("brief", True) is not False else ""
            session = {**(run_options or {}), **{
                k: args.get(k) for k in ("effort", "conversation")
            }}
        if name == "agy_ask":
            # Coding-shaped by default: inject cwd unless caller opts out
            # (workspace="none") or names dirs explicitly.
            skip = bool(args.get("skip_permissions"))
            mode = args.get("mode")
            for asked, what in ((skip, "skip_permissions"),
                                (mode not in (None, "plan"), f"mode={mode!r}")):
                if asked and os.environ.get(SKIP_PERMISSIONS_ENV) != "1":
                    return _tool_error(
                        req_id,
                        f"[agy-mcp] ERROR: {what} refused — restart the "
                        f"server with {SKIP_PERMISSIONS_ENV}=1 to allow it.",
                    )
            add_dirs = resolve_add_dirs(
                args.get("add_dir"),
                use_cwd_default=args.get("workspace", "auto") != "none",
            )
            prompt = args.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                return _tool_error(req_id, "[agy-mcp] ERROR: 'prompt' must be a non-empty string")
            result, is_error = _call_agy(
                prompt + brief,
                add_dirs=add_dirs,
                model=args.get("model"),
                timeout=args.get("timeout"),
                skip_permissions=skip,
                max_chars=max_chars,
                mode=mode,
                heartbeat=bool(args.get("heartbeat")),
                **session,
            )
        elif name == "agy_research":
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                return _tool_error(req_id, "[agy-mcp] ERROR: 'query' must be a non-empty string")
            # Research never needs the repo — no workspace, keeps agy's context lean.
            # An explicit timeout (default the hard wall) also lifts the idle
            # bound: research is silent for minutes before its answer.
            result, is_error = _call_agy(
                f"Do deep research on: {query}{brief}",
                model=args.get("model"),
                timeout=args.get("timeout", DEFAULT_TIMEOUT),
                max_chars=max_chars,
                **session,
            )
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


@dataclass
class _ToolJob:
    request: dict
    structured: bool
    progress_token: int | str | None
    read_only: bool
    conversation: str | None
    cancel: threading.Event = field(default_factory=threading.Event)


def _job_scope(params) -> tuple[bool, str | None]:
    if not isinstance(params, dict):
        return False, None
    name = params.get("name")
    if isinstance(name, str) and name in _SCOPED_READ_TOOLS:
        return True, None
    args = params.get("arguments")
    if name not in ("agy_ask", "agy_research") or not isinstance(args, dict):
        return False, None
    conversation = args.get("conversation")
    if not isinstance(conversation, str) or not conversation:
        conversation = None
    read_only = False
    if (name == "agy_ask" and args.get("mode") == "plan"
            and args.get("workspace") == "none" and not args.get("add_dir")
            and not args.get("skip_permissions") and not args.get("heartbeat")):
        try:
            read_only = 0 < _max_chars(args.get("max_chars")) <= DEFAULT_MAX_CHARS
        except ValueError:
            pass  # The tool handler reports invalid caps.
    return read_only, conversation


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 on stdio regardless of the host locale (e.g. cp936/GBK on
    # Chinese Windows, cp1252 on Western). MCP clients serialize JSON-RPC as
    # UTF-8; reading with the locale codec mangles non-ASCII prompts into
    # mojibake + surrogate escapes, which later panics pywinpty's UTF-16
    # conversion when agy's argv is built (pyo3 "assertion `left == right`
    # failed").
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="Serve AGY tools over MCP stdio.")
    add_guard_arguments(parser)
    parser.add_argument("--delegation-root", help="Workspace for the opt-in document and test tools")
    parser.add_argument("--enable-pytest", action="store_true", help="Approve the fixed local pytest recipe")
    parser.add_argument("--retain-delegation-artifacts", action="store_true", help="Allow temporary session artifacts")
    parser.add_argument("--enable-full-delegation-output", action="store_true", help="Enable the 8000-character output level")
    parser.add_argument("--jobs", type=int, choices=(1, 2), default=2, help="Maximum concurrent read-only jobs (default: 2)")
    parser.add_argument("--enable-git", action="store_true", help="Enable tracked Git diff inspection")
    parser.add_argument("--enable-public-research", action="store_true", help="Enable opt-in public web research")
    args = parser.parse_args(argv)
    policy = {}
    delegation = None
    if args.allowed_roots is not None or args.deny_patterns is not None:
        policy = {"allowed_roots": args.allowed_roots, "deny_patterns": args.deny_patterns}
        try:
            guarded_command(**policy)
        except ValueError as exc:
            sys.stderr.write(f"[agy-mcp] {exc}\n")
            return 2
    if args.delegation_root is not None:
        from ._delegation import DelegationSession

        if policy:
            parser.error("--delegation-root cannot be combined with a guarded launch")
        try:
            delegation = DelegationSession(args.delegation_root, pytest_enabled=args.enable_pytest,
                                           retention=args.retain_delegation_artifacts,
                                           full=args.enable_full_delegation_output, git_enabled=args.enable_git,
                                           research_enabled=args.enable_public_research)
        except (OSError, RuntimeError, ValueError):
            parser.error("--delegation-root must point to an existing directory")
        if not delegation.root.is_dir():
            parser.error("--delegation-root must point to an existing directory")
    elif (args.enable_pytest or args.retain_delegation_artifacts or args.enable_full_delegation_output
          or args.enable_git or args.enable_public_research):
        parser.error("Delegation options require --delegation-root")
    lock = threading.Condition()
    pending: dict[int | str, _ToolJob] = {}
    running: set[int | str] = set()
    stopping = initialized = False
    structured = False

    def stop():
        nonlocal stopping
        stopping = True
        for req_id in list(pending):
            if req_id in running:
                pending[req_id].cancel.set()
            else:
                del pending[req_id]
        lock.notify_all()

    def send(response):
        try:
            _send(response)
        except BrokenPipeError:
            stop()

    def send_error(req_id, code, message):
        send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    def run_tool(job):
        req_id = job.request["id"]

        def progress(elapsed, message):
            with lock:
                if not job.cancel.is_set():
                    send({
                        "jsonrpc": "2.0", "method": "notifications/progress",
                        "params": {"progressToken": job.progress_token, "progress": elapsed, "message": message},
                    })

        response = None
        try:
            if not job.cancel.is_set():
                response = handle_request(job.request, run_options={
                    **policy,
                    "cancel": job.cancel, "on_status": progress if job.progress_token is not None else None,
                }, delegation=delegation, structured=job.structured)
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(exc)}}
        finally:
            with lock:
                running.remove(req_id)
                del pending[req_id]
                lock.notify_all()
                if not job.cancel.is_set() and response is not None:
                    send(response)
                schedule()

    def schedule():
        if stopping:
            return
        active = [pending[req_id] for req_id in running]
        for req_id, job in pending.items():
            if len(active) == args.jobs or any(not current.read_only for current in active):
                break
            if req_id in running:
                continue
            if active and not job.read_only:
                break  # Let a waiting writer run before later readers.
            if job.conversation and any(job.conversation == current.conversation for current in active):
                continue
            running.add(req_id)
            active.append(job)
            worker.submit(run_tool, job)

    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    with ThreadPoolExecutor(max_workers=args.jobs) as worker:
        previous_sigterm = None
        if os.name == "posix" and threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.signal(signal.SIGTERM, terminate)
        try:
            for line in sys.stdin:
                if not line.strip():
                    continue
                with lock:
                    if stopping:
                        break
                    try:
                        req = json.loads(line)
                        if not isinstance(req, dict):
                            send_error(None, -32600, "Invalid request")
                            continue
                        req_id = req.get("id")
                        params = req.get("params") or {}
                        method = req.get("method")
                        if "id" not in req and method == "notifications/cancelled":
                            target = params.get("requestId") if isinstance(params, dict) else None
                            if type(target) in (str, int) and target in pending:
                                if target in running:
                                    pending[target].cancel.set()
                                else:
                                    del pending[target]
                                    schedule()
                        elif "id" not in req and method == "notifications/initialized":
                            initialized = True
                        elif "id" in req and method == "tools/call":
                            if type(req_id) not in (str, int) or req_id in pending:
                                send_error(req_id, -32600, "Invalid or duplicate request ID")
                                continue
                            if len(pending) >= MAX_PENDING_JOBS:
                                send(_tool_error(req_id, "[agy-mcp] AGY request queue is full"))
                                continue
                            meta = params.get("_meta") if isinstance(params, dict) else None
                            token = meta.get("progressToken") if isinstance(meta, dict) else None
                            read_only, conversation = _job_scope(params)
                            pending[req_id] = _ToolJob(req, structured, token if type(token) in (str, int) else None,
                                                       read_only, conversation)
                            schedule()
                        else:
                            response = handle_request(req, delegation=delegation)
                            if response is not None:
                                if method == "initialize":
                                    structured = response["result"]["protocolVersion"] == STRUCTURED_PROTOCOL_VERSION
                                send(response)
                    except json.JSONDecodeError:
                        send_error(None, -32700, "Parse error")
                    except Exception as exc:  # pragma: no cover - defensive
                        send_error(None, -32603, str(exc))
            with lock:
                if initialized:
                    stop()
                # Preserve piped, one-shot requests that never opened an MCP session.
                lock.wait_for(lambda: not pending)
        except BaseException:
            with lock:
                stop()
            raise
        finally:
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
