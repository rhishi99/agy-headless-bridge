# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.1] — 2026-07-02

### Fixed
- `clean()` no longer mangles returned code: it used to `.strip()` every line
  and drop blank ones, destroying indentation and blank lines in any code agy
  returned. It now only trims lines that actually carried box-drawing/spinner
  glyphs; plain lines (including indentation and blank lines) pass through
  untouched aside from trailing whitespace.
- Windows: a child that exits with zero output could hang until the idle
  timeout fired a spurious `AgyTimeoutError`, because `pywinpty`'s reader
  thread doesn't reliably unblock on a silent exit. The poll loop now also
  breaks as soon as `proc.isalive()` is false.
- `build_argv()`'s inner `--print-timeout` had a hard floor of 30s that could
  meet or exceed a small custom `timeout`, defeating the margin meant to keep
  agy from being severed mid-write. It's now always kept strictly below the
  outer timeout.
- `from agy_headless_bridge import run, AgyNotFoundError, AgyTimeoutError`
  (as shown in the README) raised `ImportError` — `AgyTimeoutError` and
  `resolve_add_dirs` were never exported from `__init__.py`. Both are now
  exported.
- Version was hardcoded in four places (three of them stale). `__version__`
  is now read from installed package metadata (single source: `pyproject.toml`),
  and the MCP `initialize` response reports it instead of a hardcoded string.
  `server.json` bumped to match.

### Added
- MCP server now answers `ping` with an empty result instead of a
  `-32601 Method not found` error, and echoes back the client's requested
  `protocolVersion` in `initialize` when it matches ours.

## [1.2.0] — 2026-06-26

### Added
- **Intent-aware workspace.** New `resolve_add_dirs()` helper. `agy_ask` (MCP)
  and the CLI now default to adding the current directory so agy sees the repo
  without the caller having to remember `--add-dir` — but **research/Q&A calls
  do not**, since a repo only pollutes their context. Opt out of the default
  with CLI `--no-workspace` or MCP `workspace: "none"`; explicit `--add-dir`
  always wins. `agy_research` never attaches a workspace.
- **Idle timeout.** New `AGY_BRIDGE_IDLE_TIMEOUT` (default 120s) and CLI
  `--idle-timeout`. The bridge kills agy only after it has emitted nothing for
  the idle window — a task that keeps streaming output stays alive regardless of
  total elapsed time, while a genuinely hung agy dies fast. This is the
  in-process "is it still printing?" check, so the caller never has to poll.
- **Partial output on timeout.** New `AgyTimeoutError(partial=...)`. On idle or
  hard timeout the bridge now returns whatever agy produced before the kill
  instead of discarding it; CLI prints it and MCP appends it, both noting the
  session can be resumed with `agy -c`.
- MCP `agy_ask` gains `workspace` (`auto`/`none`) and `timeout` arguments — the
  MCP layer previously had no way to override the timeout at all.

### Changed
- Hard timeout is now an absolute **ceiling** (default 300s → **900s**), not the
  normal stop signal; the idle timer ends stalled runs. Both `_run_posix` and
  `_run_windows` poll in ~1s slices to enforce idle + hard bounds.

## [1.1.0] — 2026-06-26

### Added
- `run()` now accepts `add_dirs`, `model`, and `extra_args`. `add_dirs` maps to
  agy's `--add-dir` so agy operates on the **caller's repo** — without it,
  `agy -p` runs blind in its own scratch workspace and any delegated coding task
  silently does nothing. New `build_argv()` helper (testable without spawning).
- CLI gains `--add-dir` (repeatable), `--model`, and `--timeout`.
- MCP `agy_ask` tool gains optional `add_dir` (array) and `model` arguments.

### Changed
- Default timeout 180s → **300s** to match agy's own `--print-timeout` (5m); a
  real edit-plus-test task needs minutes. The bridge also passes agy an inner
  `--print-timeout` ~15s under the pty hard-kill so agy emits a clean message
  instead of being severed mid-write. Override via `$AGY_BRIDGE_TIMEOUT`.

### Fixed
- Delegated coding tasks failed because the bridge never forwarded a workspace
  dir to agy. Now fixed via `--add-dir`.

## [1.0.1] — 2026-06-13

### Added
- `server.json` + MCP-registry ownership token in the README, and a registry
  publish step in CI — lists the server on the official MCP registry. No runtime
  code change.

## [1.0.0] — 2026-06-13

First public release.

### Added
- Core pty bridge `run(prompt, timeout=180, agy_path=None)` that runs `agy -p`
  through a fresh pseudo-terminal so its stdout isn't dropped in non-TTY
  contexts (upstream bug #76).
- Cross-platform pty backends behind one API: ConPTY via `pywinpty` on Windows,
  stdlib `pty` on Linux/macOS.
- `clean()` — strips ANSI CSI/OSC escapes, collapses `\r` spinner repaints, and
  removes box-drawing / spinner TUI glyphs.
- CLI entry points: `agy-bridge` and `python -m agy_headless_bridge`.
- MCP stdio server (`python -m agy_headless_bridge.mcp_server`) exposing
  `agy_ask` and `agy_research`, with no MCP SDK dependency.
- `find_agy()` binary discovery: `$AGY_PATH` → `PATH` → OS defaults.
- Test suite (10 tests): `clean()`, arg validation, `find_agy()`, a stub-driven
  pty mechanics test (verifies the pty path on Windows ConPTY and Linux CI
  without needing `agy`), and a live `agy` round-trip that auto-skips when `agy`
  is absent.
- CI on Windows + Linux across Python 3.9 and 3.12; PyPI publish via OIDC
  Trusted Publishing.

### Verified
- Windows ConPTY path end-to-end against `agy` 1.0.6.
- POSIX pty mechanics on Linux CI (stub-driven).

### Known limitations
- The real `agy` round-trip on POSIX (Linux/macOS) is **not yet verified on
  hardware** — reports welcome.
- Model selection inside `agy` is out of scope (pair with the `antigravity-cc`
  Claude Code plugin).

[Unreleased]: https://github.com/rhishi99/agy-headless-bridge/compare/v1.2.1...HEAD
[1.2.1]: https://github.com/rhishi99/agy-headless-bridge/releases/tag/v1.2.1
[1.2.0]: https://github.com/rhishi99/agy-headless-bridge/releases/tag/v1.2.0
[1.1.0]: https://github.com/rhishi99/agy-headless-bridge/releases/tag/v1.1.0
[1.0.1]: https://github.com/rhishi99/agy-headless-bridge/releases/tag/v1.0.1
[1.0.0]: https://github.com/rhishi99/agy-headless-bridge/releases/tag/v1.0.0
