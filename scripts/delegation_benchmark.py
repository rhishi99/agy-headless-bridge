"""Run the native delegation benchmark in disposable synthetic workspaces."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "delegation"
TOOLS = ["agy_summarize_document", "agy_run_tests", "agy_read_artifact"]
TASKS = {
    "document": (
        "What is the frame cap, and does relay.md give a rollback procedure? "
        "Report both facts, cite file and line for each, and quote the exact source wording."
    ),
    "pytest": "Report the test process exit code and exact failed test ID.",
    "control": "Read heartbeat.txt and state the heartbeat interval with its line number.",
}
CASES = [
    ("document", "direct"), ("document", "filtered"), ("document", "bridge"),
    ("pytest", "direct"), ("pytest", "filtered"), ("pytest", "bridge"),
    ("control", "direct"), ("control", "bridge"), ("document", "subagent"),
]


def stage(workspace, document_lines=360):
    workspace.mkdir()
    relay = (FIXTURES / "relay.md").read_text(encoding="utf-8")
    details = "".join(
        f"- Synthetic relay node {i:03d} reports normal queue depth after its routine check.\n"
        for i in range(document_lines)
    )
    (workspace / "relay.md").write_text(relay + "\n## Node inventory\n" + details, encoding="utf-8")
    (workspace / "heartbeat.txt").write_text("The heartbeat interval is 15 seconds.\n", encoding="utf-8")
    shutil.copyfile(FIXTURES / "noisy_pytest.py", workspace / "test_relay.py")


def task_prompt(task, variant, python, host):
    if variant == "bridge":
        action = {
            "document": "Call agy_summarize_document with paths ['relay.md'] and max_chars 2000.",
            "pytest": "Call agy_run_tests with recipe pytest, summarize false, retain false, max_chars 800.",
            "control": "Call agy_summarize_document with paths ['heartbeat.txt'] and max_chars 1000.",
        }[task]
    elif variant == "subagent":
        action = ("Use the relay-reader Agent and give it the whole task. Wait for its result."
                  if host == "claude" else
                  "Spawn one lightweight native subagent to read the document and answer the whole task. Wait for it.")
    elif task == "pytest":
        flags = "-q --tb=line --show-capture=no" if variant == "filtered" else "-q -s"
        action = f'Run the exact command rtk proxy {json.dumps(python.as_posix())} -m pytest {flags} --color=no test_relay.py using the shell.'
    elif variant == "filtered":
        action = "Run rtk proxy rg -n -i 'frame payload|rollback' relay.md using the shell. Use those matches."
    else:
        name = {"document": "relay.md", "control": "heartbeat.txt"}[task]
        action = f"Read the entire {name} using the local file or shell tool. Preserve line numbers."
    return f"{TASKS[task]} {action} Use only this method. Keep the final answer under 80 words."


def command(host, task, variant, server, workspace, codex):
    args = ["--delegation-root", str(workspace), "--enable-pytest"]
    if host == "codex":
        cmd = [codex, "exec", "--ignore-user-config", "--skip-git-repo-check", "--json", "-m", "gpt-6.2-sol"]
        cmd += ["--sandbox", "workspace-write"]
        if os.name == "nt":
            cmd += ["-c", "windows.sandbox=unelevated"]
        if variant == "bridge":
            config = {
                "command": str(server), "args": args, "enabled_tools": TOOLS,
                "default_tools_approval_mode": "approve",
            }
            for key, value in config.items():
                cmd.extend(["-c", f"mcp_servers.delegation.{key}={json.dumps(value)}"])
        if variant == "subagent":
            cmd += ["--enable", "multi_agent"]
        return cmd
    builtin = "Bash" if task == "pytest" or variant == "filtered" else "Read"
    allowed = builtin
    cmd = ["claude", "-p", "--model", "haiku", "--output-format", "stream-json", "--verbose",
           "--setting-sources", "project", "--strict-mcp-config"]
    if variant == "bridge":
        cfg = {"mcpServers": {"delegation": {"command": str(server), "args": args}}}
        cfg_path = workspace / "mcp.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        cmd += ["--mcp-config", str(cfg_path)]
        builtin = ""
        allowed = ",".join(f"mcp__delegation__{tool}" for tool in TOOLS)
    elif variant == "subagent":
        builtin, allowed = "Agent,Read", "Agent"
        agent = {"relay-reader": {"description": "Read the relay note", "prompt": "Read relay.md and verify requested facts.",
                                  "model": "haiku", "tools": ["Read"]}}
        cmd += ["--agents", json.dumps(agent)]
    return cmd + ["--tools", builtin, "--allowedTools", allowed, "--"]


def summarize(host, rows):
    if host == "claude":
        final = next((r for r in reversed(rows) if r.get("type") == "result"), {})
        usage = final.get("usage", {})
        results = [b for r in rows if r.get("type") == "user" and not r.get("parent_tool_use_id")
                   for b in r.get("message", {}).get("content", [])
                   if b.get("type") == "tool_result"]
        calls = [b.get("name") for r in rows if r.get("type") == "assistant" and not r.get("parent_tool_use_id")
                 for b in r.get("message", {}).get("content", []) if b.get("type") == "tool_use"]
        reply = final.get("result", "")
    else:
        final = next((r for r in reversed(rows) if r.get("type") == "turn.completed"), {})
        usage = final.get("usage", {})
        items = [r["item"] for r in rows if r.get("type") == "item.completed"]
        results = [i.get("result") if i["type"] == "mcp_tool_call" else i.get("aggregated_output")
                   for i in items if i["type"] in ("mcp_tool_call", "command_execution")]
        calls = [i.get("tool", i["type"]) for i in items if i["type"].endswith(("_call", "_execution"))]
        reply = next((i["text"] for i in reversed(items) if i["type"] == "agent_message"), "")
    return {"usage": usage, "calls": calls, "tool_chars": [len(json.dumps(r, ensure_ascii=False, separators=(",", ":")))
                                                           for r in results if r is not None], "reply": reply}


def run_case(root, server, python, codex, case, document_lines=360):
    repetition, host, (task, variant) = case
    label = f"{repetition}-{host}-{task}-{variant}"
    workspace = root / label
    stage(workspace, document_lines=document_lines)
    cmd = command(host, task, variant, server, workspace, codex) + [task_prompt(task, variant, python, host)]
    started = time.monotonic()
    with (root / f"{label}.jsonl").open("wb") as stdout, (root / f"{label}.stderr").open("wb") as stderr:
        try:
            exit_code = subprocess.run(cmd, cwd=workspace, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, timeout=240).returncode
        except subprocess.TimeoutExpired:
            exit_code = None
    elapsed = round(time.monotonic() - started, 2)
    rows = []
    for line in (root / f"{label}.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"case": label, "document_lines": document_lines, "exit": exit_code, "seconds": elapsed, **summarize(host, rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new directory for workspaces and session transcripts")
    parser.add_argument("--server", type=Path, required=True, help="agy-mcp-server from the test environment")
    parser.add_argument("--python", type=Path, required=True, help="Python from that environment, with pytest")
    parser.add_argument("--codex", default="codex", help="native Codex executable")
    parser.add_argument("--host", choices=("claude", "codex", "both"), default="both")
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--document-lines", type=int, default=360)
    args = parser.parse_args()
    if args.document_lines < 0:
        parser.error("--document-lines must be >= 0")
    root = args.output.resolve()
    root.mkdir()
    hosts = ("claude", "codex") if args.host == "both" else (args.host,)
    cases = itertools.product(range(1, 1 + args.repetitions), hosts, CASES)
    with (root / "metrics.jsonl").open("w", encoding="utf-8") as metrics, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(lambda case: run_case(root, args.server.resolve(), args.python.resolve(), args.codex, case, args.document_lines), cases):
            line = json.dumps(result)
            print(line, file=metrics, flush=True)
            print(line, flush=True)


if __name__ == "__main__":
    main()
