# Token Budget Rules & Operating Discipline

## Core Token Budget Discipline (MANDATORY)

Rhishi operates with strict token budget discipline across projects. Maximize work done per token:

1. **`rtk`-prefix EVERY shell command** — no exceptions (e.g. `rtk git status`, `rtk pytest -q`). Applies inside chained commands too.
2. **Terse, Dense Output** — Drop conversational preamble, filler, and echoing back user input. State decisions and results directly. Code and security explanations remain complete.
3. **Batched Memory Reads** — At session/task start, read auto-memory `MEMORY.md` + relevant `obsidian-memory/` note BEFORE reading source code. Prefer targeted reads (`rtk grep`, line ranges) over whole-file dumps.
4. **Batched Memory Writes** — Write memory/obsidian ONCE at task end (batched). Never write mid-session.
5. **Filtered Test & Lint Runs** — Always use filtered runs (`rtk pytest -q`), never raw dumps that flood context.
6. **Subagent Delegation** — Delegate broad exploration, large file reading, or multi-step analysis to subagents (`senior-software-architect`, etc.) so the main thread context only processes the compressed result. Keep subagent count low (1–3 max).

## Model Routing Table

- **Claude Sonnet / gpt-5.5 / Gemini Flash**: Preferred for rapid execution, tests, minor edits, doc/memory updates, and bulk tasks.
- **Opus / High Reasoning Models**: Reserved exclusively for high-judgment architectural planning, multi-file refactoring, and critical security reviews.
- **Antigravity CLI (agy)**: Route via Gemini 3.8 Flash (`flash-medium` by default). Do not route to retired Claude/GPT models on agy.
