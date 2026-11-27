#!/usr/bin/env python3
"""Lightweight state checkpoint helper for multi-step agent tasks.
Allows long executions to serialize progress and resume across sessions.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

CHECKPOINT_PATH = Path(".tmp/agent_checkpoint.json")

def save_checkpoint(phase: str, data: dict) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"phase": phase, "data": data}
    CHECKPOINT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[checkpoint] saved phase={phase}")

def load_checkpoint() -> dict | None:
    if not CHECKPOINT_PATH.exists():
        return None
    try:
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[checkpoint] error reading checkpoint: {e}", file=sys.stderr)
        return None

def clear_checkpoint() -> None:
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print("[checkpoint] cleared")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "clear":
        clear_checkpoint()
    elif len(sys.argv) > 1 and sys.argv[1] == "show":
        cp = load_checkpoint()
        print(json.dumps(cp, indent=2) if cp else "No checkpoint found")
