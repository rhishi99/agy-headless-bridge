# AGY Headless Bridge — Architecture & Competitive Analysis for Claude Code and Codex

This analysis evaluates delegation from Claude Code and Codex to Google Antigravity CLI (`agy`). Other local MCP clients can use the same execution layer. Findings were reviewed against the v1.4.0 implementation and installed AGY 1.5.18 on November 26, 2026. The [delegation backlog](DELEGATION_BACKLOG.md) covers implementation order and acceptance criteria.

## 1. Problem statement and token economics

Claude Code and Codex can delegate high-volume work to Antigravity while retaining the task plan and final review. Good candidates include long-document summaries, broad repository reads, public-source research, and noisy test or Git output. The caller sends a focused question and source paths before ingesting the material, then receives the few facts it needs to act. Follow-up retrieval should bring back an individual section or excerpt.

Measure the total work to deliver a correct answer. A small response can still cost more if the parent already read the document, has to open the full artifact, or repeats the task. Report caller input and output, cache usage where available, AGY usage, elapsed time, and retries. Quotas depend on the Antigravity account and plan, and credit overages can apply. Token counts alone cannot establish a subscription cost or quota reduction. [Antigravity plans and overages](https://antigravity.google/docs/plans)

## 2. Competitive comparison

| Approach | What it offers | What to compare |
| --- | --- | --- |
| Direct caller with filtered commands or a native lightweight subagent | Low setup effort, existing tool approvals, and concise output for routine commands | Use filtered local tests and Git as the baseline. Include a native lightweight subagent for equivalent long-document or research work. [RTK](https://github.com/rtk-ai/rtk) |
| Claude Code AGY plugins | Explicit delegation and reviews within Claude Code. Some offer background jobs, bounded digests, and published cost exercises. | Compare onboarding, task selection, evidence quality, and the frequency of follow-up reads. Yuting's self-reported implementation benchmark observed savings on larger handoffs and losses on small tasks. [simplybychris plugin](https://github.com/simplybychris/antigravity-plugin-cc) · [yuting0624 plugin](https://github.com/yuting0624/antigravity-for-claude-code) |
| AGY or multi-agent MCP servers | Local model and tool delegation. Existing projects advertise background jobs, Git reporting, or progress and usage telemetry. | Compare the same workloads on Claude Code and Codex, including cancellation and recovery. [agy-mcp](https://github.com/josephjerryrhule/agy-mcp) · [mcp-agents](https://github.com/thomaswitt/mcp-agents) |
| Direct model API integration | Direct model access, structured output, usage reporting, and an application-controlled tool runtime | Include where it answers the same task. Workspace, shell, and browser access depend on the host integration. [Gemini API function calling](https://ai.google.dev/gemini-api/docs/function-calling) |
| `agy-headless-bridge` | CLI, Python API, and stdio MCP with native AGY JSON on compatible versions and Windows ConPTY/POSIX PTY fallback | Demonstrate a bounded, verifiable result on both clients. Compare current direct AGY headless execution as well, since AGY now supports machine-readable headless output. [AGY headless docs](https://antigravity.google/docs/cli/headless/) |

This is a capability comparison. The previously suggested 500 versus 10,000+ token figure and blanket claims about Windows failures or quota advantages lack a controlled benchmark. Test pinned versions and comparable configurations before publishing a head-to-head result.

## 3. Verified architecture findings

| Area | Current behavior | Product implication |
| --- | --- | --- |
| Subprocess transport | The bridge selects direct JSON by default on AGY 1.2.11 and newer and retains a PTY path. AGY 1.5.18 exposes `--json-schema` and `--output-format stream-json`. | Test the native schema and event stream before extending the existing file heartbeat. Preserve legacy transport coverage. [Bridge implementation](../src/agy_headless_bridge/bridge.py) · [AGY headless docs](https://antigravity.google/docs/cli/headless/) |
| Model selection | The bridge forwards `--model` per invocation. The local `agy models` listing included Gemini 4 Hydrogen and Gemini 3.8 Flash. Google lists Gemini 3.8 Flash removal for November 30, 2026. | Start with a configurable Hydrogen preset and a preflight that reports available IDs. Add aliases when actual routing needs justify the policy. [Google model catalog](https://antigravity.google/docs/models) |
| Response size and artifacts | `brief` adds a prompt instruction. `cap_output()` returns a fitting reply whole and only looks for a summary when truncation is needed. The default MCP cap is 4,000 characters. The caller can override `max_chars` or use `0`. Spill notes and other diagnostics sit outside the text cap. The spill is a local path to retained reply text. | Give the new workflow a server-controlled ceiling over the entire model-visible response and bounded follow-up retrieval. Preserve the existing contract for legacy callers. [Bridge](../src/agy_headless_bridge/bridge.py) · [MCP server](../src/agy_headless_bridge/mcp_server.py) |
| Result integrity | Direct mode parses usage and write denials. The bridge can raise `AgyQuotaError`, mapped to CLI exit 75. MCP flattens results into text, and denial notes can accompany a successful tool result. | Keep machine-readable status and verified execution metadata distinct from model-written interpretation. |
| Parallel execution | The stdio loop waits for the current `handle_request()` before reading the next message. Inbound notifications are ignored. Separate subprocesses or temporary directories alone establish neither shared-workspace isolation nor usable in-session parallelism. | Keep the connection responsive during runs. Add bounded dispatch, cancellation, timeout, and process cleanup as one lifecycle. [MCP server](../src/agy_headless_bridge/mcp_server.py) |
| Progress | The current heartbeat relies on AGY writing after steps. Explicit plan mode rejects this heartbeat, and `agy_research` lacks a heartbeat argument. MCP progress is requested by a client token and client timeout policies still apply. | Use observed AGY events as progress when supported. Label elapsed-time messages accurately and verify host behavior before promoting long jobs. [MCP progress](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress) · [Cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation) |
| Permissions | `--add-dir` controls workspace visibility. The existing environment gate can enable broad unattended permissions. | Scope the initial workflows to deliberate read access and configured local commands. Validate effective permissions on the supported AGY/OS combination before calling a workflow read-only. Worktrees isolate changes between jobs. Runtime policy must enforce any containment claim. [Bridge security policy](../SECURITY.md) · [AGY terminal sandbox](https://antigravity.google/docs/sandbox?tab=cli) |
| Onboarding | Claude Code has a plugin and a native registration example. The Codex README section shows generic MCP JSON. | Provide version-pinned native install instructions for both hosts, a preflight, and a first useful task. A client skill can call MCP directly for the initial workflows. [Current Codex instructions](../README.md#wire-into-codex-or-any-mcp-client) · [Codex MCP](https://developers.openai.com/codex/mcp) · [Claude Code plugin components](https://code.claude.com/docs/en/plugins-reference) |

## 4. Recommended routing

| Workload | Execution | Compact handoff |
| --- | --- | --- |
| Long documents and repository reads | AGY reads scoped source paths and answers a focused question. | Findings with line, section, or page references and coverage gaps. |
| Tests and read-only Git | Execute project-approved local commands. Use AGY where synthesis of large outputs helps. | Exit status, verifiable counts, commit or diff scope, failing identifiers, and a small excerpt. |
| Public web research | AGY gathers sources for a bounded question under an explicit network policy. | Findings with source URLs, dates where material, and unresolved questions. |
| Bulk edits | Later, run an explicitly approved job with an isolated worktree and enforceable permissions. | Changed files, compact diff summary, and verified test results for caller review. |

Short commands and small questions commonly fit the caller's normal workflow. Delegate when the task is likely to avoid substantial reads, logs, or research in the caller. Preserve the user's choice of when to delegate and what workspace material Antigravity may process.

## 5. Release direction and evidence

1. Ship bounded, evidence-backed handoffs for document summaries and noisy failing tests. Pair them with a native setup and one reproducible demo in each host.
2. Prove two independent jobs can overlap on one MCP connection, including cancellation, cleanup, and quiet progress. Add read-only Git and public web recipes once their scope is verified.
3. Publish matched benchmarks with accuracy and latency alongside caller token usage. Promote optional routing and new model profiles based on these measurements. Consider unattended write workflows and additional transports after their requirements are concrete.

PR #7 is merged and v1.4.0 is published. The review verified 120 local tests and an MCP initialization and tool discovery smoke using the published package. Client end-to-end workflows, live AGY summaries, and savings estimates await the backlog's acceptance runs. The separate pytest-worker throughput plan covers repository test execution. [v1.4.0 release](https://github.com/rhishi99/agy-headless-bridge/releases/tag/v1.4.0) · [PyPI](https://pypi.org/project/agy-headless-bridge/1.4.0/)
