# /agy:ask — Run a prompt through agy-headless-bridge

Use the bash tool to run the following command and return its output:

```bash
python -m agy_headless_bridge.bridge "$ARGUMENTS"
```

- Exit 0 = non-empty response captured.
- Custom timeout: pass `--timeout <seconds>` before prompt.
- Model override: pass `--model <alias>` (e.g. `flash-medium`).
