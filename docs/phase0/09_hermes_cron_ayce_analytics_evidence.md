# TASK 0.7B — HERMES CRON → EXISTING AYCE ANALYTICS (evidence gate, NO implementation)

**Scope.** Evidence-only. Determine whether repository + research evidence
supports wiring Hermes' native scheduler to the EXISTING AYCE analytics
pipeline. Nothing was installed, created, configured, scheduled, or modified.
No skill, no MCP change, no cron job, no source change, no dependency.

## 1. Current verified state

| Item | Value |
|---|---|
| Repo HEAD | `f45247d` (main, synced with origin/main) |
| Hermes | `v0.21.5+2244.g35ad70c (2026.9.24)`, install `git` |
| Hermes skills | `0 hub-installed, 51 builtin, 0 local — 51 enabled, 0 disabled` (unchanged; no analytics skill installed — Phase 0.7 candidate does not exist) |
| MCPs | `ayce-readonly ✓ enabled`, `ayce-director ✓ enabled` — untouched (`hermes mcp list` re-verified) |
| AYCE analytics implementation | PRESENT and previously verified: `src/ayce/analytics/{collector,transport,store,errors}.py` |

## 2. Hermes scheduling/cron capability evidence (native, source + CLI verified)

`hermes cron --help` → full native scheduler subsystem:

```text
{list, create(add), edit, pause, resume, run, remove, status, runs(history),
 incidents, notepad, doctor, tick}
Manage scheduled tasks
```

Key facts (from `hermes cron create --help` and source):

- **Job = schedule + prompt OR script.** `hermes cron create <schedule>
  [prompt]` with schedule grammar `'30m', 'every 2h', '0 9 * * *'`.
- **Pure-script jobs (no LLM):** `--script <path> --no-agent` — help text:
  "With --no-agent: the script IS the job and its stdout is delivered
  verbatim. … Classic watchdog pattern." `.sh/.bash` run via bash, everything
  else via Python.
- **Script security boundary (source):** `cron/scheduler_script.py:271-314`
  `_resolve_script_path` — scripts MUST resolve inside
  `HERMES_HOME/scripts/` (`C:\Users\Administrator\AppData\Local\hermes\scripts\`,
  exists, currently empty); traversal/absolute-path/symlink escapes are
  blocked. `.py` scripts run under `sys.executable` (Hermes' own interpreter,
  `scheduler_script.py:317-330`) — so a wrapper must invoke the AYCE venv
  python by absolute path (the same pattern `mcp-ayce-director/server.py`
  `_pinned_interpreter` already uses, lines 123-135).
- Windows script execution explicitly supported
  (`_windows_cron_python_invocation`, `scheduler_script.py:117`).
- Operational features: `--deliver`/`--failure-deliver` targets, `--repeat`,
  `--workdir` (absolute cwd for the job), `--paused` creation, durable
  `runs`/`incidents`/`notepad`, `hermes cron doctor`, `hermes cron tick`
  ("Run due jobs once and exit").
- **Scheduler liveness (observed, read-only):** `hermes cron status` →
  "✗ No gateway is running on this host — cron jobs will NOT fire. Scheduler
  last ticked 1d 20h ago." `hermes cron list` → "No scheduled jobs." The
  runtime state exists (`%LOCALAPPDATA%\hermes\cron\executions.db`,
  `ticker_heartbeat`, `ticker_last_success`) but the gateway service is not
  currently running. This is an OPERATIONAL prerequisite for scheduled firing
  (documented; not a blocker for this evidence gate).

## 3. Stage 12 research evidence (report present in repo)

`Research_Agent\Reports 4A-4L\YouTube Analytics Closed-Loop Architecture`
(extracted verbatim from the report text):

- "The analytics collector executes a **tri-tier scheduled ingestion process**
  designed to accommodate official platform delays and protect project API
  quotas."
- Tier cadences: "Hours 0 to 48 (**Cron: 30m to 6h**)"; "**Scheduled cron** at
  Day 7, 14, and 30 post-publish"; "A **daily cron job** inspects Reporting API
  job outputs (channel_reach_basic_a1 and channel_reach_combined_a1). New CSV
  reports are downloaded from Google Cloud Storage signed URLs, decompressed
  locally, parsed…"
- Component matrix row: "**Cron / Task Scheduler — EXTEND** — Extend existing
  scheduler to orchestrate the tri-tier collection intervals (fast feedback,
  daily harvest, mature backfill)."

Verdict on question 4: the research EXPLICITLY supports a scheduler →
analytics-collection flow and even specifies the cadences. (It does not name
`youtube-analytics-harvester` as a skill — Phase 0.7 record stands; the
research's own component is "the analytics collector", which is the existing
`src/ayce/analytics/` code.)

## 4. Existing AYCE analytics execution boundary (read-only inspection)

- **CLI entry (the callable boundary):** `src/ayce/cli.py:77-104` defines
  `ayce analytics collect <video_id|run_id> [--window-start YYYY-MM-DD]
  [--window-end YYYY-MM-DD] [--sources …] [--metrics …] [--dry-run] [--json]`
  ("OBSERVATION-ONLY analytics ingestion for published videos (Stage 6)") and
  `ayce analytics show <video_id|run_id>`.
- Handler `_cmd_analytics` (`cli.py:855-933`) calls
  `collect_analytics(video_or_run, config, ledger, sources, metrics,
  window_start, window_end, dry_run)` from `.analytics` (`cli.py:857,877-881`);
  errors surface as structured `AnalyticsError.to_dict()`.
- **Interpreter:** `python -m ayce` (`src/ayce/__main__.py` present) under the
  AYCE project venv `C:\...\AI-Content-Generation\.venv\Scripts\python.exe`
  — **verified present** (`Test-Path` → True); identical to the interpreter the
  director already pins (`mcp-ayce-director/server.py:123-135`).
- **Persistence:** `store.py:116` `PRAGMA journal_mode=WAL` (SQLite WAL, as
  researched). Ledger source: `PublishLedger(config.resolved_data_dir /
  "publishing" / "ledger.sqlite3")` (`cli.py:870`).
- **Auth:** live collection requires YouTube credentials; AYCE has a one-time
  OAuth setup subcommand (`cli.py:69` "one-time YouTube OAuth setup: print the
  consent URL and exchange an authorization code"; TokenProvider pattern at
  `cli.py:757,784,814`). **`--dry-run` performs NO external contact and stores
  nothing** (`cli.py:894` "DRY-RUN (no external contact, nothing stored)") —
  the safe verification lever for the future implementation phase.
- Files NOT modified: `collector.py`, `transport.py`, `store.py`, `errors.py`,
  `cli.py` — read-only inspection only.

## 5. Supported invocation path (what the evidence supports)

```text
Hermes native cron (hermes cron create <schedule>
    --script <HERMES_HOME/scripts/<wrapper>.py> --no-agent [--name] [--deliver])
  → wrapper script (inside %LOCALAPPDATA%\hermes\scripts\, Hermes' security boundary)
      → C:\...\AI-Content-Generation\.venv\Scripts\python.exe -m ayce analytics collect … --json
        → src/ayce/analytics/collector.py (YouTube Analytics API v2 / Data API v3; auth-gated)
          → src/ayce/analytics/store.py (SQLite WAL)
```

- No skill required (question 3: YES — `--no-agent` script jobs invoke existing
  capabilities directly; the skill layer is irrelevant to this flow).
- No MCP change required; no Hermes config change required to CREATE a job
  (job creation is a Hermes-native operation, deferred to implementation).
- Alternative supported path (not recommended): prompt-driven cron job with
  `--workdir <repo>` letting the agent drive the CLI via terminal tools —
  works, but spends LLM tokens and is non-deterministic; the `--no-agent`
  script pattern is the exact "classic watchdog" use-case Hermes documents.

## 6. Unsupported / unknown paths (no guessing)

- **Gateway not running** (observed): scheduled firing requires the Hermes
  gateway service (`hermes --profile default gateway install|run` per
  `hermes cron status` output) or an equivalent tick driver. Starting it is an
  implementation-phase operational step, out of scope here.
- **Tri-tier orchestration shape is NOT yet defined by evidence:** the CLI
  collects ONE video/run per invocation; how the three research cadences
  (30m–6h fast tier, daily harvest, Day 7/14/30 backfill) map onto N cron jobs
  vs one wrapper that iterates the publish ledger is an implementation decision
  that needs its own design step (the wrapper script content itself).
- Exact credential provisioning for the cron execution context (env vs AYCE
  config file) is not yet observed — AUTH-BLOCKED until the one-time OAuth
  setup is actually performed.
- Reporting-API bulk CSV harvest (research tier: "daily cron job inspects
  Reporting API job outputs") is NOT part of the current `analytics collect`
  CLI surface — the research's full tri-tier design exceeds the existing CLI;
  treat as a known scope gap for the implementation phase, not silently assumed.

## 7. Is implementation justified?

YES — all four legs of the intended chain are evidenced:
1. Hermes native scheduler exists with a no-LLM script-job mechanism designed
   exactly for this (§2).
2. The research explicitly prescribes scheduler-driven tri-tier analytics
   ingestion and says EXTEND the existing scheduler (§3).
3. The existing AYCE analytics CLI is a real, parameterized, JSON-reporting,
   dry-run-capable execution boundary on a verified venv (§4).
4. Reuse rule satisfied: zero new capability is invented; only a thin wrapper
   script inside Hermes' own scripts boundary plus cron job definitions.

## 8. Exact next implementation boundary (NOT executed in this task)

1. Create ONE wrapper script under `%LOCALAPPDATA%\hermes\scripts\`
   (e.g. `ayce_analytics_collect.py`) that invokes
   `C:\...\AI-Content-Generation\.venv\Scripts\python.exe -m ayce analytics
   collect … --json` with explicit absolute paths and truthful exit codes.
2. Manually execute the wrapper once with `--dry-run` and verify JSON output
   (safe, credential-free) BEFORE creating any cron job.
3. `hermes cron create` the job(s) `--script … --no-agent --paused`, verify
   with `hermes cron list`, then unpause; start/verify the gateway per
   `hermes cron status` guidance; `hermes cron tick` for an immediate proof.
4. Live (authenticated) collection remains **AUTH-BLOCKED** until the one-time
   AYCE YouTube OAuth setup is performed — never fabricated.

## PHASE_0_7B = evidence gate complete; no implementation performed

## HERMES_CRON_AYCE_ANALYTICS = READY_FOR_IMPLEMENTATION