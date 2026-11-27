# agy-headless-bridge — Multi-Agent Engineering Rules & Protocol

This document defines the operating rules for all AI coding agents (Claude Code, Antigravity/agy, Codex, and subagents) working in this repository.

---

## 1. Operating Rules & Core Invariants

1. **Repository Scope & Mission**: `agy-headless-bridge` is the headless ConPTY bridge, direct JSON runner, and MCP server enabling headless automation of Google Antigravity CLI (`agy`).
2. **Protected & Canonical Files**:
   - `src/agy_headless_bridge/`: Core package. Keep public signatures and CLI contract backwards-compatible.
   - `server.json` & `.claude-plugin/`: MCP and plugin manifests. Must stay synchronized on version bumps.
   - `CLAUDE.md` & `AGENTS.md`: Update only when system workflows or rules evolve.
3. **End Every Turn With Next Step**: State clearly what was done and provide a concrete, actionable next step.
4. **Validate Live Before Claiming Success**: Always run test suites and verify output before asserting completion. Evidence before assertions always.

---

## 2. Token Budget Discipline (MANDATORY)

Rhishi operates with strict token budget discipline across projects. Maximize work done per token:

1. **RTK Command Prefixing**: `rtk`-prefix EVERY shell command — no exceptions (e.g., `rtk git status`, `rtk pytest -q`). Applies inside chained commands too.
2. **Terse, Dense Output**: Keep responses concise and decision-dense. Avoid conversational filler or echoing back files verbatim.
3. **Batched Memory Reads**: At session/task start, read auto-memory `MEMORY.md` + relevant `obsidian-memory/` note BEFORE reading source code. Prefer targeted reads (`rtk grep`, line ranges) over whole-file dumps.
4. **Batched Memory Writes**: Write memory/obsidian ONCE at task end (batched). Never write mid-session.
5. **Filtered Test & Lint Runs**: Always use filtered runs (`rtk pytest -q`), never raw unfiltered dumps that pollute the context window.
6. **Subagent Delegation**: Delegate broad multi-step exploration, heavy documentation scans, or isolated experiments to a subagent (`senior-software-architect`, etc.) so the main thread context only processes the compressed result. Keep subagent count low (1–3 max).

---

## 3. Development Workflow & Strict TDD

1. **Strict TDD (Test-Driven Development)**:
   - For any new function, endpoint, or behavior change: write a failing test first in `tests/`.
   - Run the test to observe failure.
   - Implement the minimal code required to turn the test green.
   - Refactor cleanly while maintaining green tests.
   - No production code without a test that drove it.
2. **Ponytail Anti-Over-Engineering Protocol**:
   - Smallest working diff wins. YAGNI (You Aren't Gonna Need It) is strictly enforced.
   - Never add unsolicited abstractions, speculative helper functions, or unnecessary configuration layers.
   - Ponytail mode governs design minimalism — it does NOT relax strict TDD.
3. **Honesty Over Optimism**:
   - State plainly when a step (TDD, live validation, token rule) was NOT followed and why — never imply compliance you didn't achieve.

---

## 4. Obsidian Memory Vault Protocol (`obsidian-memory/`)

This repository uses a local-only Obsidian vault located at `obsidian-memory/` (gitignored):

- **Structure**:
  - `000-Index.md`: Central knowledge index and navigation hub.
  - `MEMORY.md`: High-speed cheat-sheet and current sprint pointers.
  - `architecture/`: Architecture notes, data flow, bridge run paths, and seams.
  - `decisions/`: Architecture Decision Records (ADRs) and trade-off rationales.
  - `logs/`: Handoff notes (`handoff-YYYY-MM-DD.md`), sprint notes, and error logs.
  - `.obsidian/`: Obsidian workspace and core plugins configuration.
- **Workflow**:
  - **Start of task**: Read `obsidian-memory/MEMORY.md` or `000-Index.md` first.
  - **End of task**: Batch-record key architectural decisions, test metrics, and handoffs in `obsidian-memory/`.

---

## 5. PR & Branching Discipline

1. **Every change goes through a branch/PR**: Never commit directly to `main`.
2. **Local test verification**: Run `rtk pytest -q` locally before proposing or merging PRs.
3. **CI Conservation**: GitHub Actions minutes are rationed. Rely on local verification; keep remote CI clean.

---

## 6. Multi-Agent Delegation & Routing

- **Claude Code**: Primary reasoning, planning, complex refactoring, and integration.
- **Senior Software Architect Subagent** (`.claude/agents/senior-software-architect.md`): Delegate high-level system design, multi-file refactoring strategy, and trade-off analysis.
- **Antigravity CLI (agy)**: For browser-based checks, live MCP tool usage, and bulk file generations. Prefer fast models (`Gemini 3.8 Flash`) for low-cost execution.
- **Note on agent-harness**: The legacy `agent-harness` is excluded; rely on native tools, direct CLI/MCP bridges, and lightweight subagent delegation.
