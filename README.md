<!-- mcp-name: io.github.rhishi99/agy-headless-bridge -->

<div align="center">

# agy-headless-bridge

### Call the Google **Antigravity CLI** (`agy`) headlessly — and actually get output back.

Codename **PtyGravity** · pty + antiGravity

[![PyPI](https://img.shields.io/pypi/v/agy-headless-bridge.svg?color=7c5cff)](https://pypi.org/project/agy-headless-bridge/)
[![PyPI downloads](https://img.shields.io/pypi/dm/agy-headless-bridge.svg?color=22d3ee)](https://pypi.org/project/agy-headless-bridge/)
[![MCP Registry](https://img.shields.io/badge/MCP_Registry-listed-7c5cff)](https://registry.modelcontextprotocol.io/v0/servers?search=agy-headless-bridge)
[![tests](https://github.com/rhishi99/agy-headless-bridge/actions/workflows/test.yml/badge.svg)](https://github.com/rhishi99/agy-headless-bridge/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-7c5cff.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-22d3ee.svg)](https://www.python.org)
[![Platform](https://img.shields.io/badge/platform-Windows%20%E2%9C%93%20%C2%B7%20POSIX%20%28beta%29-34d399.svg)]()

📖 **[Architecture & docs → rhishi99.github.io/agy-headless-bridge](https://rhishi99.github.io/agy-headless-bridge/)**

</div>

---

## TL;DR — the problem, before & after

`agy -p "<prompt>"` prints **nothing** when its stdout is not a real terminal.
So calling it from a subprocess, an MCP server, CI, or another coding agent
(Claude Code, Codex, …) returns an empty string and exit `0` — silently. This
package gives `agy` a fresh pseudo-terminal so it emits normally, then cleans
the output.

```mermaid
flowchart TB
    subgraph B["❌ BEFORE — agy -p from any non-TTY caller"]
        direction TB
        a1["subprocess · MCP · CI · agent"] --> a2["agy -p &quot;prompt&quot;"]
        a2 --> a3["stdout gated by isatty()"]
        a3 --> a4["(empty string)<br/>exit 0 · no error · no output"]
    end
    subgraph A["✅ AFTER — through agy-headless-bridge"]
        direction TB
        b1["subprocess · MCP · CI · agent"] --> b2["run(prompt)"]
        b2 --> b3["allocate fresh pseudo-terminal"]
        b3 --> b4["agy -p &quot;prompt&quot;<br/>isatty() == True"]
        b4 --> b5["clean() strips ANSI/TUI"]
        b5 --> b6["clean text ✓"]
    end

    classDef bad fill:#2a1313,stroke:#f87171,color:#ffd9d9;
    classDef good fill:#0f2a1e,stroke:#34d399,color:#d7ffe9;
    class a1,a2,a3,a4 bad;
    class b1,b2,b3,b4,b5,b6 good;
```

```python
# ❌ The problem — plain subprocess
import subprocess
r = subprocess.run(["agy", "-p", "say hi"], capture_output=True, text=True)
print(r.stdout)          # '' — prints nothing, exit 0

# ✅ The fix
from agy_headless_bridge import run
print(run("say hi"))     # 'Hi! How can I help?'
```

Three entry points around one core:

| Entry point | Invoke | Use for |
|---|---|---|
| **Library** | `from agy_headless_bridge import run` | embedding agy in Python |
| **CLI** | `agy-bridge "prompt"` | shell scripts, quick calls |
| **MCP server** | `python -m agy_headless_bridge.mcp_server` | letting an agent call agy as a tool |

---

## The problem in detail — upstream bug [#76]

`agy` gates its stdout on `isatty()`. The instant stdout isn't a terminal, it
goes silent — no output, no error, exit `0`:

```console
$ agy -p "say hi" | cat
$            # empty. exit 0. nothing.
```

The common `winpty agy -p "..."` workaround needs a terminal that **already
exists**, so it still fails from any automated / non-TTY caller.

## The fix — give agy a tty it didn't ask for

Allocate a **brand-new** pseudo-terminal (one that needs no parent tty) and
attach `agy` to it. Same code path on every OS — only the pty allocator differs.

```mermaid
flowchart TD
    A["Caller — non-TTY<br/>Claude Code · MCP · subprocess · CI"] -->|"prompt"| B{{"run(prompt)"}}
    B --> C["find_agy()<br/>$AGY_PATH → PATH → OS defaults"]
    C --> D{"sys.platform?"}
    D -->|"win32"| E["pywinpty<br/>PtyProcess.spawn"]
    D -->|"posix"| F["stdlib pty<br/>os.openpty + Popen"]
    E --> G(["fresh pseudo-terminal"])
    F --> G
    G --> H["agy -p prompt<br/>isatty == True → emits"]
    H -->|"raw bytes + ANSI/TUI chrome"| I["clean()<br/>strip CSI/OSC · collapse \r repaints · drop spinner glyphs"]
    I -->|"clean text"| A
```

| Platform | pty backend | Status |
|---|---|---|
| **Windows** | ConPTY via [`pywinpty`] (`PtyProcess`) | ✅ verified (agy 1.0.6) |
| **Linux / macOS** | stdlib [`pty`] (`os.openpty` + `subprocess.Popen`) | ✅ verified on macOS hardware |

> [!TIP]
> **Linux users wanted.** The pty mechanics are verified on Linux CI and macOS hardware, but the real `agy` round-trip on bare-metal Linux hasn't been run.
> If you're on Linux: `pip install agy-headless-bridge`, try it, and
> [tell us how it went](https://github.com/rhishi99/agy-headless-bridge/issues/new/choose) — pass or fail. PRs welcome.

> **Why not just the existing `agy` Claude Code plugins?** They wrap `agy` for
> *triggering* (slash commands, model selection) but still call `agy -p`
> directly — so in any headless context they hit this exact empty-output bug.
> This package fixes the I/O layer they're missing. **Use both together.**

---

## Prerequisites

Before installing this bridge you need:

1. **Python 3.9+** — `python --version`.
2. **The Antigravity CLI (`agy`)**, installed and **authenticated**:
   - Install: <https://antigravity.google/cli>
   - Authenticate once interactively (`agy` opens a browser OAuth flow), or set
     `ANTIGRAVITY_API_KEY` in your environment if you use an API key.
   - Verify it runs *in a real terminal*: `agy -p "say hi"` should print a reply.
     (From a pipe it won't — that's the very bug this package fixes.)
3. **Windows only:** `pywinpty` (installed automatically as a dependency).
   POSIX uses the stdlib `pty` module — nothing extra.

> This package does **not** install or authenticate `agy`, and does not bundle
> any credentials. It only spawns the `agy` already on your machine.

---

## Install

Requires **Python 3.9+**.

```bash
pip install agy-headless-bridge          # pywinpty auto-installs on Windows only
```

From source:

```bash
git clone https://github.com/rhishi99/agy-headless-bridge
cd agy-headless-bridge
pip install -e .
```

The bridge locates the binary via, in order: `$AGY_PATH` → `agy` on `PATH` →
OS default install paths.

---

## Usage

### Library

```python
from agy_headless_bridge import run, AgyNotFoundError

try:
    print(run("reply with exactly: OK", timeout=60))
except AgyNotFoundError:
    print("install agy first")
```

`run(prompt, timeout=180, agy_path=None) -> str` — raises `AgyNotFoundError` if
the binary is missing, `TimeoutError` on timeout, `ValueError` on empty prompt.
Returns `""` only if agy genuinely emitted nothing.

### CLI

```bash
agy-bridge "reply with exactly: OK"
python -m agy_headless_bridge "reply with exactly: OK"   # equivalent
```

### MCP server

```bash
claude mcp add --transport stdio antigravity -- \
    python -m agy_headless_bridge.mcp_server
```

The server speaks JSON-RPC stdio directly (no MCP SDK dependency) and routes
every call through the pty bridge.

**Tool schema** (what an agent — or you, integrating manually — sees):

| Tool | Argument | Type | Required | Description |
|---|---|---|---|---|
| `agy_ask` | `prompt` | string | ✅ | one-shot prompt sent to agy |
| `agy_research` | `query` | string | ✅ | wrapped as a deep-research prompt for agy |

**Response shape** — a standard MCP `tools/call` result; the answer is the text
content:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": { "content": [ { "type": "text", "text": "<agy's cleaned answer>" } ] }
}
```

On failure the `text` is an `[agy-mcp] ERROR: ...` string (agy missing, timeout,
etc.) rather than a JSON-RPC error, so the agent always gets a readable reply.

---

## Use cases & wiring it into your AI coding tools

The whole point: let **one** AI coding tool delegate work to **Gemini via
Antigravity**, headlessly. Common setups:

| Use case | How |
|---|---|
| Claude Code asks Gemini for a second opinion / diff review | MCP server → `agy_ask` tool |
| A CI step runs an `agy` prompt and captures the answer | `agy-bridge "..."` in the workflow |
| A Python pipeline fans work out to agy | `from agy_headless_bridge import run` |
| Codex / any MCP-capable agent delegates to agy | register the same MCP server |
| Cron / scheduled job summarizes logs via agy | `agy-bridge` in the script |

### Wire into Claude Code

Register the MCP server, then prompt Claude to use it:

```bash
claude mcp add --transport stdio antigravity -- \
    python -m agy_headless_bridge.mcp_server
```

> **Prompt to Claude Code:**
> *"Use the `agy_ask` tool to ask Antigravity to review this function for edge
> cases, then summarize its findings for me."*

If you also want slash-command triggering and model selection, pair this bridge
with the community `antigravity-cc` Claude Code plugin — that handles the
`/agy:*` commands and Gemini/Claude model swap; this handles the headless I/O.

### Wire into Codex (or any MCP client)

Add the server to the client's MCP config:

```json
{
  "mcpServers": {
    "antigravity": {
      "command": "python",
      "args": ["-m", "agy_headless_bridge.mcp_server"]
    }
  }
}
```

> **Prompt to the agent:**
> *"Call `agy_research` with the query 'idiomatic error handling in Rust' and
> turn the result into a checklist."*

### Use from a shell / CI script

```bash
ANSWER="$(agy-bridge 'Summarize the key risk in this diff in one sentence.')"
echo "$ANSWER"
```

---

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `AGY_PATH` | auto-detect | Absolute path to the `agy` binary |
| `AGY_BRIDGE_TIMEOUT` | `180` | Seconds before a call is killed |

---

## How `clean()` works

`agy`'s pty output is a TUI stream, not plain text. `clean()` removes **ANSI
escapes** (CSI/OSC — colors, cursor moves), **`\r` repaints** (a spinner
overwrites one line; only the final paint is kept), and **box-drawing / spinner
glyphs** (`╭─╮ │ ⠋⠙⠹`) — leaving just the model's answer.

What comes off the pty vs. what you get back:

```text
RAW (off the pty)                          CLEANED (returned to you)
─────────────────────────────────────     ─────────────────────────
⠋ thinking…\r⠙ thinking…\r\x1b[2K          A closure is a function that
\x1b[32m╭─────────────╮\x1b[0m              captures variables from the
\x1b[32m│\x1b[0m A closure is a function     scope where it was defined.
that captures variables from the
scope where it was defined.
\x1b[32m╰─────────────╯\x1b[0m
```

---

## Troubleshooting / FAQ

**`pip install` fails on Windows building `pywinpty`** — `pywinpty` is a native
extension. If pip tries to build from source and errors with a compiler/`cl.exe`
message, install the **Microsoft C++ Build Tools** (or use a Python where a
prebuilt `pywinpty` wheel exists — recent CPython on Windows has them). Upgrade
pip first: `python -m pip install -U pip`.

**`AgyNotFoundError`** — the bridge can't find `agy`. Set `AGY_PATH` to the
absolute path of the binary, or make sure `agy` is on your `PATH`
(`agy --version` should work in your shell).

**Empty string returned** — agy produced no output. Confirm it works in a real
terminal first: `agy -p "say hi"`. If that's also empty, the problem is agy/auth,
not the bridge. Re-authenticate (`agy` interactively) or check
`ANTIGRAVITY_API_KEY`.

**`TimeoutError`** — the call exceeded `AGY_BRIDGE_TIMEOUT` (default 180s). Raise
it for long prompts: `AGY_BRIDGE_TIMEOUT=600 agy-bridge "..."` or
`run(prompt, timeout=600)`.

**Pseudo-terminal allocation fails** — rare. On Windows it means `pywinpty`
isn't importable (reinstall it). On POSIX it means the system is out of pty
slots or `pty.openpty()` is denied (containers with no `/dev/pts`); run with a
real pty available.

**Garbled / partial output** — open an
[issue](https://github.com/rhishi99/agy-headless-bridge/issues/new) with the OS,
Python + agy version, and the raw output; `clean()` may need another glyph rule.

## Development & CI

```bash
pip install -e ".[dev]"
pytest
```

Unit tests (cleaning, arg validation, binary discovery) always run. The live
`agy` round-trip test **auto-skips** when `agy` isn't installed — so CI runners
(which don't have `agy`) stay green and never need credentials. CI runs on
Windows + Linux across Python 3.9 and 3.12.

---

## Scope, non-goals & disclaimer

- **Model selection** (Gemini Pro / Flash / Claude inside agy) is *not* handled
  here — it's an `agy` `settings.json` concern, covered by the `antigravity-cc`
  plugin. Pair the two.
- Does **not** install or authenticate `agy`, and ships **no credentials**.
- Automating any vendor CLI may interact with that vendor's terms / rate limits.
  You are responsible for using `agy` within Google's terms of service. This
  project only changes *how stdout is captured* — it does not bypass auth,
  quotas, or any access control.
- Not affiliated with Google. *Antigravity* and *agy* are Google products.

## License

[MIT](LICENSE).

[#76]: https://antigravity.google/cli
[`pywinpty`]: https://github.com/andfoy/pywinpty
[`pty`]: https://docs.python.org/3/library/pty.html
