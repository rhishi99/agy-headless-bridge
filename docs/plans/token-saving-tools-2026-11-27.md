# Plan: token-saving delegation tools (27 Nov 2026)

Goal: add operations where the caller (mainly Claude Code) pays fewer tokens by letting AGY read the bulky
input and return a short, bridge-checked answer. Benchmark basis: Claude saves on large inputs (break-even
360–2000 lines); Codex does not save through the bridge, so these tools target Claude Code first.

All work: strict TDD (red test in `tests/test_delegation_workflows.py` first), ponytail-minimal diff, reuse
`_stage`, `_preflight`, `_verified_citations`, `_present`, `_retain` in `src/agy_headless_bridge/_delegation.py`.
No new dependencies. Each task ships on its own branch + PR from its own worktree under `C:\worktrees\`.

## Tasks

| ID | Tool / change | Owner | Worktree / branch |
|---|---|---|---|
| T1 | Logs in `agy_summarize_document` | agy `gemini-4-hydrogen-medium` | `C:\worktrees\abb-t1-logs` / `feat/t1-log-documents` |
| T5 | Verified review findings in `agy_git_diff` | agy `gemini-4-hydrogen-medium` (after T1) | same worktree, new branch `feat/t5-diff-review` |
| T3 | `agy_find_code` | `sonnet-executor` subagent | `C:\worktrees\abb-t3-code` / `feat/t3-find-code` |
| T4 | More `agy_run_tests` recipes | `sonnet-executor` (after T3) | same worktree, new branch `feat/t4-recipes` |
| T2 | `agy_inspect_media` | main thread (Opus) | `C:\worktrees\abb-t2-media` / `feat/t2-media` |

### T1 — log files in the document tool (easy)
- `_stage` accepts `.log` and `.out` besides `.md/.txt/.markdown`; decode with `errors="replace"` for logs only.
- Tool description says "Markdown, text or log files".
- Tests: a `.log` fixture with a traceback is accepted and a verified quote cites the error line; `.exe` still rejected.

### T5 — verified diff review (easy-medium)
- `agy_git_diff` gains `review: bool`. When true, AGY gets the bounded excerpt with a JSON schema
  `{summary, findings:[{path, line, issue}], gaps}`.
- Bridge keeps a finding only if `path` appears in the excerpt's `+++ b/<path>` headers and `line` falls in a
  `@@ … +start,count @@` hunk for that path. Unverified → dropped, status `partial`, gap added.
- Max 8 findings, `issue` ≤ 200 chars. `review` and `summarize` are mutually exclusive (`invalid_input`).
- Tests: hunk parser accepts in-range line, drops out-of-range and unknown path; review off = old behaviour.

### T3 — `agy_find_code` (complex)
- Opt-in flag `--enable-code-search`. Args: `question` (1–2000 chars), optional `paths` (≤ 8 workspace-relative
  dirs/files, same guards as `_stage`: no `..`, `.git`, `.ssh`, absolute, `:`), `model`, `timeout`, output args.
- AGY runs `mode="plan"` with `add_dirs=[root or chosen paths]`, schema = `_DOCUMENT_SCHEMA`.
- Verification: for each cited path, resolve under root (same guards), read ≤ 4 MiB as UTF-8 (`errors="replace"`),
  then `_verified_citations`. Max 12 citations. Unverifiable → `partial` + gap.
- Tests: verified citation kept; citation outside root, in `.git`, or with a wrong quote dropped; tool hidden
  without the flag.

### T4 — more test recipes (medium)
- `recipe` enum: `pytest`, `ruff`, `npm-test`, `cargo-test`. Fixed argv only (`[sys.executable,"-m","ruff","check",
  "--output-format","concise","."]`, `["npm","test","--silent"]`, `["cargo","test","-q"]`), resolved with
  `shutil.which`; missing tool → `test_runner_error`.
- Flag `--enable-recipe NAME` (repeatable); `--enable-pytest` stays as alias for `pytest`.
- Non-pytest recipes: no junit; facts = exit code + elapsed; excerpt = first error line block (≤ cap/2).
- Tests: unapproved recipe → `unknown_recipe`; ruff failure excerpt; missing binary path.

### T2 — `agy_inspect_media` (complex, unique)
- Args: `paths` (1–4 workspace files: `.png .jpg .jpeg .webp .gif .pdf .mp3 .wav .m4a .mp4 .mov .webm`),
  `question`, `model`, `timeout`, output args. Combined cap 64 MiB; copied to a scratch dir like `_stage`.
- Schema `{summary, evidence:[{path, locator, claim}], gaps}`; locator = `page N` (PDF), `mm:ss` or `mm:ss-mm:ss`
  (audio/video), or `image`. Bridge checks path is staged and locator format matches the file kind; content
  claims are the model's (reported as `summary_source: "model"`, never "verified").
- Gate on the live probe (27 Nov): only kinds AGY actually viewed are enabled.
- Tests: suffix/size guards, locator check per kind, unstaged path dropped.

## Integration (main thread)
1. Review each PR (`/code-review low` once; harness rule), fix or dismiss findings.
2. Merge order T1 → T5 → T3 → T4 → T2; rebase each on main; changed-file tests per PR.
3. Full suite once on merged main (detached), then README + `docs/DELEGATION_BACKLOG.md` rows, bump to 1.6.0.
4. One live smoke per new tool against `gemini-4-hydrogen-low`.

## Acceptance
- `rtk pytest -q` green; `rtk ruff check .` clean.
- Each new tool: invalid input, denied path, AGY error, unverified evidence → `partial` all tested.
- No tool writes to the workspace; all AGY runs use `mode="plan"`.
