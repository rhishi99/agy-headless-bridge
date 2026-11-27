# agy-headless-bridge — Developer & Agent Instructions

This repository provides the headless ConPTY bridge, direct JSON runner, and Model Context Protocol (MCP) server for Google's Antigravity CLI (`agy`).

---

## Token Budget Discipline (MANDATORY)

Rhishi operates under strict token budget discipline across all repositories. Maximize work done per token:

1. **RTK Command Prefixing**: `rtk`-prefix EVERY shell command — no exceptions (e.g., `rtk git status`, `rtk pytest -q`). Applies inside chained commands too.
2. **Terse, Dense Output**: Keep responses concise and decision-dense. Avoid conversational filler or echoing back files verbatim.
3. **Batched Memory Reads**: At session/task start, read auto-memory `MEMORY.md` + relevant `obsidian-memory/` note BEFORE reading source code. Prefer targeted reads (`rtk grep`, line ranges) over whole-file dumps.
4. **Batched Memory Writes**: Write memory/obsidian ONCE at task end (batched). Never write mid-session.
5. **Filtered Test & Lint Runs**: Always use filtered runs (`rtk pytest -q`), never raw unfiltered dumps that pollute the context window.
6. **Subagent Delegation**: Delegate broad multi-step exploration, heavy documentation scans, or isolated experiments to a subagent (`senior-software-architect`, etc.) so the main thread context only processes the compressed result. Keep subagent count low (1–3 max).

---

## Development Workflow Rules (MANDATORY)

1. **Strict TDD (Test-Driven Development)**:
   - For any new function, endpoint, or behavior change: write a failing test first in `tests/`.
   - Run the test to observe failure (`rtk pytest -q tests/test_<target>.py`).
   - Implement the minimal code required to turn the test green.
   - Refactor cleanly while maintaining green tests.
   - No production code without a test that drove it.
2. **Ponytail Anti-Over-Engineering Protocol**:
   - Smallest working diff wins. YAGNI (You Aren't Gonna Need It) is strictly enforced.
   - Never add unsolicited abstractions, speculative helper functions, or unnecessary configuration layers.
   - Ponytail mode governs design minimalism — it does NOT relax strict TDD.
3. **Honesty Over Optimism**:
   - State plainly when a step (TDD, live validation, token rule) was NOT followed and why — never imply compliance you didn't achieve.
4. **End Every Response With Next Step**:
   - Always conclude each turn with a concrete, actionable next step.

---

## Obsidian Memory Vault Protocol (`obsidian-memory/`)

This repository uses a local-only Obsidian vault located at `obsidian-memory/` (gitignored):

- **Start of task**: Read `obsidian-memory/MEMORY.md` or `000-Index.md` first before scanning source files.
- **End of task**: Batch-record key architectural decisions, test metrics, and handoffs in `obsidian-memory/logs/`.
- **Vault Layout**:
  - `000-Index.md`: Central index and entry point.
  - `MEMORY.md`: Pointers and quick cheat-sheet.
  - `architecture/`: Bridge run paths (direct JSON vs pty), MCP gates, and seams.
  - `decisions/`: ADRs (e.g. direct mode vs plan mode).
  - `logs/`: Handoffs (`handoff-YYYY-MM-DD.md`), sprint notes, and CLI error captures.
  - `.obsidian/`: Vault configuration and core plugins.

---

## PR & Branching Discipline

1. **Every change goes through a branch/PR**: Never commit directly to `main`.
2. **Local test verification**: Run `rtk pytest -q` locally before proposing or merging PRs.
3. **CI Conservation**: GitHub Actions minutes are rationed. Rely on local verification; keep remote CI clean.

---

## Fast Test Commands

- **Unit tests**: `rtk pytest -q`
- **Specific test file**: `rtk pytest -q tests/test_direct_runner.py`
- **Linting**: `rtk ruff check .`
