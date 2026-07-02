# Security Policy

## Design stance

`agy-headless-bridge` handles **no credentials** by design. It does not read,
store, or transmit any auth token, and it does not bypass any access control. It
only spawns the `agy` binary already installed and authenticated on your machine,
inside a pseudo-terminal, and returns the cleaned stdout. Authentication and
quotas are entirely `agy`'s concern.

It does execute a subprocess (`agy`, or — in tests — a stub you provide). Treat
the `prompt` you pass like any other input you'd hand to a CLI.

**MCP server callers, note:** the `agy_ask` tool accepts caller-supplied
`add_dir` paths and forwards them to agy as `--add-dir`. Any MCP client that
can reach this server can therefore ask agy to read/operate on any directory
that client names — the bridge does not sandbox or allowlist `add_dir`. If you
expose this server to untrusted MCP clients, restrict who can call it at the
transport/host level; this package enforces no path restrictions of its own.

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.x     | ✅ |
| < 1.0   | ❌ |

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Use GitHub's **[private vulnerability reporting](https://github.com/rhishi99/agy-headless-bridge/security/advisories/new)**
(Security tab → "Report a vulnerability"). Include reproduction steps, affected
version, and platform.

Examples worth reporting privately: a way to make the bridge leak environment
data, execute something other than the requested command, or write outside its
intended scope.

You can expect an initial response within a few days. Thanks for helping keep it
safe.
