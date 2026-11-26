---
name: agy-delegation
description: Run a scoped Markdown or text summary with verified quotes, or an approved local pytest recipe with compact results. Requires the bridge's opt-in MCP tools.
---

Use the scoped `antigravity-delegation` MCP server registered in the [delegation guide](../../docs/DELEGATION.md). Call its tools directly in the current session.

- For a document, call `agy_preflight` with the requested model or the default. If it succeeds, call `agy_summarize_document` with the workspace-relative paths and the user's question. Report the answer, verified citations, and gaps. `partial` means the bridge could not verify every reference.
- For tests, call `agy_run_tests` with recipe `pytest` only when the user asked to run it. Report the observed exit code, counts when available, and failed test ID. Set `summarize: true` only when the user wants model synthesis of the failure excerpt.
- Use the default `summary` level for the first call. Request retention when a follow-up log excerpt is likely and the user has enabled session artifacts. Use `agy_read_artifact` for a specific range.
- If a tool is absent, ask the user to check the scoped MCP registration. If preflight fails, report the status and the corrective action from the tool.
