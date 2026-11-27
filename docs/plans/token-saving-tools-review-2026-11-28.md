# Token-saving delegation tools review

28 November 2026 · Ponytail Ultra

Reviewed the [T1–T5 plan](token-saving-tools-2026-11-27.md) at `902c75a` and five implementation branches. The tools extend scoped document reads, Git review, code search, local test recipes, and media inspection. Fix findings 1–5 before merging.

| Task | Branch | PR | Reviewed tip |
| --- | --- | --- | --- |
| T1 · Log documents | `feat/t1-log-documents` | #19 | `1726a35` |
| T5 · Git diff review | `feat/t5-diff-review` | #22 | `32b43b3` |
| T3 · Code search | `feat/t3-find-code` | #20 | `794db93` |
| T4 · Test recipes | `feat/t4-recipes` | #23 | `c980c42` |
| T2 · Media inspection | `feat/t2-media` | #21 | `1efdf86` |

## Must fix

1. **T3 · Scope code search and citation verification.** A request for `paths=["pkg"]` still starts AGY at the workspace root. The verifier accepts a valid quote from any file under that root, including a sibling of `pkg/`. Run AGY from a scratch directory with the selected attachments and check each resolved citation against the selected files or directories. Add a regression test that rejects a valid sibling quote and checks the launch options. Preserve whole-workspace search when `paths` is omitted. [Launch and verification](https://github.com/rhishi99/agy-headless-bridge/blob/794db93df5643363f562e216993b92ef939a8d3a/src/agy_headless_bridge/_delegation.py#L365-L409)
2. **T5 · Verify lines AGY actually received.** The plan diff declared 69 additions in its hunk header. The bounded excerpt showed only 23 addition markers and ended mid-line, yet the verifier accepted line 69. Verify complete added lines in the sent excerpt and flag incomplete coverage. Test a clipped hunk with findings on a fully shown line and on a line beyond the excerpt. [Hunk parser](https://github.com/rhishi99/agy-headless-bridge/blob/32b43b3108c0bf06cdb60787c6dce2f3a013c845/src/agy_headless_bridge/_delegation.py#L195-L210)
3. **T5 · Preserve findings within the response budget.** Eight allowed findings at the default structured limit yielded only “Result exceeds the character budget” with `status: ok`. Trim findings to fit and signal omissions. Test the default structured response with several long findings and assert it retains findings within the cap. [Response budgeting](https://github.com/rhishi99/agy-headless-bridge/blob/32b43b3108c0bf06cdb60787c6dce2f3a013c845/src/agy_headless_bridge/_delegation.py#L238-L278)
4. **T4 · Handle missing Ruff and keep the tests portable.** CI installs `.[dev]`, which declares pytest. The new Ruff test invokes the real module. In a clean environment, the recipe returns `failed` with `No module named ruff`. Stub the Ruff runner in the unit test and return `test_runner_error` when Ruff is unavailable. Test both a lint finding and an unavailable runner. [Ruff test](https://github.com/rhishi99/agy-headless-bridge/blob/c980c42200dfbf7887d83b25bef9c43b90acbfc1/tests/test_delegation_workflows.py#L430-L441) · [Dev dependencies](https://github.com/rhishi99/agy-headless-bridge/blob/c980c42200dfbf7887d83b25bef9c43b90acbfc1/pyproject.toml#L24-L25)
5. **T2 · Gate media formats on live viewing.** The recorded probe succeeded for PNG and MP4. The implementation also enables PDF, audio, and other formats. Limit the accepted formats and user-facing descriptions to PNG and MP4, or record successful live viewing before enabling additional formats. Test that an unproven suffix is rejected. [Plan](token-saving-tools-2026-11-27.md#t2--agy_inspect_media-complex-unique) · [Media formats](https://github.com/rhishi99/agy-headless-bridge/blob/1efdf86b6394542671db3efc64464ae60125b4ac/src/agy_headless_bridge/_delegation.py#L72-L75)

## Should fix

6. **T1 · Stage the normalized log.** The verifier replaces invalid UTF-8 bytes while the attachment retains them. Decode log bytes once and stage the decoded text as UTF-8 so the attachment and verifier see the same content. Make the regression test read the staged attachment. [Log staging](https://github.com/rhishi99/agy-headless-bridge/blob/1726a3595d032ac9b774554e091733eed1b2f56d/src/agy_headless_bridge/_delegation.py#L340-L344)
7. **T2 and T4 · Validate types before lookup.** A list in a media citation’s `path` or a test call’s `recipe` raises `TypeError`. Check both values are strings. Test that malformed media evidence yields `partial` while retaining valid evidence and that a malformed recipe returns a bounded input error. [Media evidence](https://github.com/rhishi99/agy-headless-bridge/blob/1efdf86b6394542671db3efc64464ae60125b4ac/src/agy_headless_bridge/_delegation.py#L454) · [Recipe validation](https://github.com/rhishi99/agy-headless-bridge/blob/c980c42200dfbf7887d83b25bef9c43b90acbfc1/src/agy_headless_bridge/_delegation.py#L597)
8. **T4 · Preserve Cargo’s failure details.** A failing Rust test returned only Cargo’s rerun hint. Keep the failing test name and assertion in the bounded excerpt. Test representative Rust failure output ending in the rerun hint. [Excerpt selection](https://github.com/rhishi99/agy-headless-bridge/blob/c980c42200dfbf7887d83b25bef9c43b90acbfc1/src/agy_headless_bridge/_delegation.py#L637)

## Verification and handoff

- Main passed 175 tests. Focused suites passed 1 on T1, 37 on T5, 43 on stacked T3/T4, and 42 on T2. Ruff reported 43 existing findings on main.
- The T2 format gate uses the recorded PNG and MP4 probe. This review used local tests and fixtures for the feature branches.
- Test from each feature worktree with that worktree’s `src` first on `PYTHONPATH`. The local editable install otherwise points at main. T4 is stacked on T3.
- Use strict TDD for behavior changes. Run `rtk pytest -q` and check the changed Python files with Ruff before handing off the PRs.

## One-line implementation prompt

Read `E:/vibe-code-projects/agy-headless-bridge/docs/plans/token-saving-tools-review-2026-11-28.md` and use strict TDD to fix findings 1–8 on the existing feature branches, starting with 1–5, then report the test results for each PR.
