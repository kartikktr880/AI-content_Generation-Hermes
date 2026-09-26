# H1 AUDIT RECORD — Hermes Agent in an Isolated Environment

> **AUDIT RECORD ONLY — not an integration.** Hermes Agent is installed in
> an isolated environment and has NO connection to AYCE. Nothing in AYCE's
> runtime, dependencies, or pipeline references Hermes. This record captures
> what was installed, what was verified, and how to remove it.

## Identity (verified 2026-09-19)

- Project: `NousResearch/hermes-agent` — "The agent that grows with you"
- Source: https://github.com/NousResearch/hermes-agent (MIT License)
- Version: **v0.21.3** (release tag `v2026.9.14`, commit `345cd2b`) — the
  latest release at install time; pinned via the installer's `-Tag` flag
- Note: the PyPI package `hermes-agent` lags (0.19.0 as of this audit);
  v0.21.3 is only available via the official installer / GitHub tag

## Isolated environment (actual location)

- Install root: `%LOCALAPPDATA%\hermes\` (user-writable, no admin,
  independently removable) — the `/opt/hermes-agent/` proposal from the
  research report is a Linux path and does not apply on Windows
- Contents: `hermes-agent\` (git checkout @ 345cd2b), `bin\` (hermes.exe,
  uv.exe, browser tools), managed Python **3.11.16** runtime, venv,
  `skills\` (58 bundled), `sessions\`, `memories\`, `cron\`, `logs\`,
  `hooks\`, `backups\`
- Managed uv: 0.12.17
- Footprint: ≈1.73 GB on disk; **no persistent processes by default**

## Installation

```text
# installer: https://hermes-agent.nousresearch.com/install.ps1 (251,523 bytes,
# SHA256 E57D49271C45205E8FAAE964E59453A2279481D85EADAE7D8495DF2E31F91CCD)
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 ^
  -Tag v2026.9.14 -SkipSetup
```

- Attempt 1 FAILED at tag-pin: `git fetch` network disconnect (`fetch-pack:
  unexpected disconnect / early EOF`) — environmental, retried cleanly.
- Attempt 2 succeeded (~12 min total incl. downloads). Deviation to note:
  the hash-verified `uv.lock` sync failed and the installer fell back to a
  live PyPI resolve (fresh installs get latest-compatible dep versions,
  not the locked ones).
- Side effects outside the install dir: managed Node dir moved to the front
  of the **User PATH** (registry, user scope, no admin). Reversible via
  removing that PATH entry.

## Runtime verification (all VERIFIED by execution)

- `hermes --version` → `Hermes Agent v0.21.3 (2026.9.14)` · Python 3.11.16 ·
  OpenAI SDK 2.24.0 · install method: git ("3650 commits behind" main —
  expected: we pinned the release on purpose)
- `hermes --help` → full CLI (chat, model, gateway, cron, skills, plugins,
  memory, mcp, tools, computer-use, approvals, security, serve, doctor, ...)
- `hermes doctor` → clean: no security advisories, no suspicious MCP stdio
  commands, SSL bundle valid, version files consistent (0.21.3); optional
  messaging deps (telegram/discord) not installed
- Startup ≈ 1.0 s (`--version`); launcher shim ≈ 5 MB working set

## Capability audit (read-only; no LLM key configured, no agent session run)

| Capability | Result | Evidence |
|---|---|---|
| Agent loop | NOT VERIFIED (needs LLM key/interactive run) | `hermes chat`, `--yolo`, `--safe-mode`, batch_runner.py exist |
| Shell/tool execution | PARTIAL (VERIFIED policy, not execution) | `hermes approvals test "echo hello"` → verdict `allow` |
| Filesystem | OBSERVED | skills/fs tooling present; not exercised |
| MCP | PARTIAL (VERIFIED CLI surface) | `hermes mcp add/test/list` + `hermes mcp serve` (Hermes AS an MCP server); no server configured yet |
| Skills | VERIFIED (read-only) | `hermes skills list`; 58 bundled skills synced, trust/status columns |
| Browser | OBSERVED (binaries present) | browser-use.exe etc. in bin; not exercised |
| Delegation | OBSERVED | subagents/parallel documented; `peer`, `kanban` commands exist; not exercised |
| Persistence | OBSERVED | `sessions\`, `memories\`, `cron\`, FTS5 session search |
| Scheduling | PARTIAL (VERIFIED CLI) | `hermes cron list/create/pause/...` full lifecycle |
| Extensibility | OBSERVED | plugins/hooks/bundles/skills subcommands; `hermes mcp serve` |

## Security (read-only audit; nothing weakened)

- `hermes approvals test "rm -rf /"` → verdict **`hardline-deny`**
  ("matches the hardline blocklist — never bypassable, blocked even under
  `--yolo` / approvals.mode=off") — direct evidence of a layered,
  non-bypassable dangerous-command guard
- `hermes security` = on-demand supply-chain audit (OSV.dev) covering the
  venv, plugin deps, and pinned npx/uvx MCP servers
- No secrets stored or exposed during this audit; no agent session was run
  (no LLM key configured)

## Removal procedure

```text
# 1) delete the install (fully isolated):
#    %LOCALAPPDATA%\hermes\
# 2) remove the managed Node dir entry from the user PATH (optional)
# 3) delete installer logs: %TEMP%\hermes-install\
# No AYCE files, dependencies, or configuration are involved.
```
