# Delegation benchmark

The Windows runs on 27 November 2026 measured how delegation changes the material a caller sees and the tokens it uses through task completion. The bridge cut the document and raw pytest payloads substantially. Filtered local commands used fewer total caller input tokens on both hosts in these cases. The one-line control favored a direct read.

## Reproduce the runs

Install the bridge with its `dev` extra and make AGY, Claude Code, Codex, Git, ripgrep, and RTK available to the runner. Point `--server` and `--python` at the same environment. Use a new output directory. On Windows npm installations, `--codex` can point directly to `codex.exe`.

```powershell
rtk proxy python scripts/delegation_benchmark.py "$env:TEMP/agy-benchmark" `
  --server C:/venv/Scripts/agy-mcp-server.exe --python C:/venv/Scripts/python.exe `
  --codex C:/path/to/codex.exe --repetitions 2 --workers 2
```

The [runner](../scripts/delegation_benchmark.py) records its prompts, methods, and command settings in source. It creates a fresh workspace and native session per case and saves JSONL transcripts and `metrics.jsonl`. It extends the synthetic [relay note](../tests/fixtures/delegation/relay.md) with 360 inventory lines and preserves the evidence at lines 4 and 11. It stages the [noisy pytest fixture](../tests/fixtures/delegation/noisy_pytest.py) as `test_relay.py` and generates a one-line heartbeat control.

The recorded runs used Claude Code 2.1.332 with `claude-haiku-5-5`, Codex 0.172.0 with `gpt-6.2-sol`, AGY 1.5.18 with `gemini-4-hydrogen-low`, and an installed local preview wheel with package metadata `1.4.0`. Claude used a Haiku document-reader subagent. Codex used the native subagent tool and the host's default child model. Codex used workspace-write with the unelevated Windows sandbox for the synthetic workspaces. The first run under the default sandbox denied shell calls. Those attempts are outside the comparison. The usable runs used four workers for Claude and three for Codex. Each case started a new conversation and MCP connection. Provider prompt caches were warm or mixed as reported by each host. Subscription quota, provider latency, and local load can affect results.

## Measured results

Each cell gives the range across two repetitions. Caller input includes cache reads. Claude input is the sum of `input_tokens`, `cache_creation_input_tokens`, and `cache_read_input_tokens`. Codex reports total input and its cached subset. Payload size is the caller-facing tool results serialized in the host transcript, excluding child agent context. Codex's native subagent wait events did not expose their payload size in the captured JSONL. Claude time is its reported session duration. Codex time includes process startup.

| Host and workload | Method | Caller input tokens | Cached read tokens | Output tokens | Tool chars | Seconds | Passed |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Claude document | Full read | 26,782–26,805 | 9,656 | 477–496 | 30,943 | 6.4–7.2 | 2/2 |
| | Filtered local | 18,286 | 11,521 | 289–312 | 327 | 6.6–7.8 | 2/2 |
| | Native subagent | 20,647–20,815 | 12,980 | 586–617 | 1,120–1,451 | 11.4–12.0 | 2/2 |
| | Bridge | 29,489–29,644 | 22,531–22,532 | 533–558 | 480–965 | 34.7–94.9 | 1/2 |
| Claude pytest | Raw local | 34,940–34,948 | 11,546 | 792–793 | 20,404–20,409 | 15.8–17.6 | 2/2 |
| | Filtered local | 18,961–19,093 | 11,558 | 612–762 | 822 | 14.3–19.2 | 2/2 |
| | Bridge | 19,317 | 12,725 | 211–213 | 386–387 | 11.3–17.3 | 2/2 |
| Claude control | Direct | 16,092 | 9,638 | 135–138 | 127 | 3.9–5.1 | 2/2 |
| | Bridge | 19,777–19,779 | 12,730 | 639–681 | 664–679 | 74.0–85.9 | 0/2 |
| Codex document | Full read | 70,085–70,093 | 56,704 | 234–243 | 31,821 | 22.9–27.2 | 2/2 |
| | Filtered local | 42,021–42,024 | 20,864 | 193–194 | 114 | 15.9–22.4 | 2/2 |
| | Native subagent | 84,458–84,498 | 77,312–77,568 | 194–233 | Unavailable | 39.6–83.6 | 2/2 |
| | Bridge | 155,951–178,681 | 146,816–162,048 | 258–292 | 1,383–1,469 | 76.0–82.0 | 2/2 |
| Codex pytest | Raw local | 43,621–46,127 | 19,584–20,864 | 116–126 | 20,185 | 17.6–20.1 | 2/2 |
| | Filtered local | 42,233 | 35,584 | 128–132 | 598 | 19.2–21.6 | 2/2 |
| | Bridge | 63,569–84,721 | 56,704–70,400 | 124–149 | 637 | 17.5–19.1 | 2/2 |
| Codex control | Direct | 42,154–63,406 | 20,864–56,704 | 156 | 624–625 | 17.3–17.6 | 2/2 |
| | Bridge | 127,633–149,116 | 112,896–126,720 | 198–228 | 733–743 | 79.8–90.4 | 2/2 |

A document passed only when it gave both expected quotes and line citations. Pytest passed for an observed process exit of 1 plus `test_relay.py::test_relay_contract`. The control passed for 15 seconds at line 1 using the requested method. Both Claude bridge document cases made two summary calls. The first timed out twice and the second succeeded on retry. Both Claude control cases fell back to local `Read`, so neither passed the bridge-only check. The control rows include that extra tool result. Each Codex bridge case issued one MCP call.

The document and control bridge calls use AGY inference. The pytest calls in this benchmark used `summarize: false` and made zero AGY inference calls. The preview MCP payload exposes model ID and version for document calls. Provider-side AGY token counts per benchmark call were unavailable from these transcripts. The host token counts are telemetry and do not estimate cost or quota. The benchmark requested zero artifact reads. A separate native client smoke verified the optional artifact path with a 600-character MCP result budget. [Native smoke measurements](DELEGATION_BACKLOG.md#d06--package-native-onboarding-and-the-two-demos)

Use a local read for a one-line lookup and a deterministic filter when the relevant pattern is known. The bridge remains useful for bounded evidence or observed pytest facts when the client lacks local shell access. These measurements cover one long document size and one noisy test size, so they do not establish an automatic routing threshold.
