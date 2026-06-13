# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/rhishi99/agy-headless-bridge/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/rhishi99/agy-headless-bridge/releases/tag/v1.0.1
[1.0.0]: https://github.com/rhishi99/agy-headless-bridge/releases/tag/v1.0.0
