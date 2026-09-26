
## Token Budget Discipline (MANDATORY)

Context: Maximize work done per token to stay within budget constraints.

**Agent-enforceable rules for every session:**
1. **RTK Command Prefixing:** `rtk`-prefix EVERY shell command — no exceptions (e.g., `rtk git commit`). Applies inside `&&` chains too.
2. **Caveman Mode:** Keep Caveman full mode on (hook-driven; do not disable).
3. **Batched Memory Reads:** At session/task start, read auto-memory `MEMORY.md` + relevant `obsidian-memory/` file BEFORE reading source code. Prefer targeted reads (`rtk grep`) over whole-file exploration. Write memory/obsidian ONCE at task end (batched). Never mid-session.
4. **Graphify:** Use `graphify` only when its output is reused across the task — never one-shot.
5. **Filtered Runs:** Always use filtered runs for testing and linting (e.g., `rtk pytest`, `rtk lint`), never raw.
6. **Subagent Delegation:** Delegate broad multi-step exploration or documentation reading to a subagent (e.g., `invoke_subagent` in Antigravity) so the main thread context only processes the compressed result. You can summon Grok (via `C:\Users\HP\.grok\bin\grok.exe`) when needed for Stitch with HTML creation tasks. Keep subagent count low (1-3 max).
