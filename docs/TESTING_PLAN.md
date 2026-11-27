# Testing Plan & Verification Protocol

## 1. Fast Local Unit Testing (Offline)

All tests in `tests/` are fast, deterministic, and mocked against agy subprocesses.

```bash
# Full test suite
rtk pytest -q

# Specific test module
rtk pytest -q tests/test_bridge.py
rtk pytest -q tests/test_mcp_server.py
```

- `tests/conftest.py` stubs the `agy --version` probe and subprocess spawns so tests never invoke real `agy`.
- Tests must pass 100% locally before creating PRs or merging.

## 2. Live Smoke Testing (Cheapest Model, Real CLI)

To test against the live installed `agy` binary without burning expensive quota:

```bash
python -m agy_headless_bridge.bridge --model gemini-3.8-flash-low --no-workspace "reply with exactly: AGY_OK"
```

Verify output matches: `AGY_OK`.

## 3. Strict TDD Protocol

1. Write a failing test in `tests/test_<feature>.py`.
2. Run `rtk pytest -q` to confirm the test fails for the expected reason.
3. Implement the minimal diff required in `src/agy_headless_bridge/`.
4. Run `rtk pytest -q` to confirm all tests pass.
5. Refactor cleanly if necessary while keeping tests green.
