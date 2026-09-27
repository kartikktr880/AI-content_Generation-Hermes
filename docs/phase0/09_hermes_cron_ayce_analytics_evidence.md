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

---

# ADDENDUM — PHASE 0.7B IMPLEMENTATION (executed, behaviorally proven)

**Starting HEAD:** `a63f10f` (working tree: only the pre-existing untracked
`docs/phase0/00–05` evidence files — preserved untouched).

## Implementation

| Item | Value |
|---|---|
| Wrapper path | `%LOCALAPPDATA%\hermes\scripts\ayce_analytics_collect.py` (INSIDE Hermes' script security boundary; created via editor, NOT hand-copied into Hermes config) |
| Exact behavior | Requires a collection target (argv[1] for manual runs, or the operator-maintained `TARGET` constant for cron mode — cron `--script --no-agent` passes no arguments; empty target → truthful exit 2, no target is ever invented); ALWAYS invokes the existing AYCE CLI with `--dry-run --json` (live collection is a later, auth-gated decision — the wrapper never contacts YouTube); spawns the pinned interpreter with argv-list (no shell, stdin=DEVNULL), `cwd` pinned to the repo (data_dir resolves from cwd), `PYTHONPATH` **replaced** with `<repo>\src` (src layout; ayce is not pip-installed — appending inherited scheduler paths broke imports, see bug-fix below); forwards child stdout/stderr verbatim and returns the child's exit code; fails with exit 3 if the interpreter or repo is missing |
| AYCE invocation | `C:\...\AI-Content-Generation\.venv\Scripts\python.exe -m ayce analytics collect <target> --dry-run --json [extra args…]` with `PYTHONPATH=<repo>\src`, `cwd=<repo>` |
| Cron job | id `19a6cce6796c`, name `ayce-analytics-collect`, schedule `every 360m` (within the research fast tier "Cron: 30m to 6h"), mode `no-agent`, deliver `local`, **PAUSED** |
| Gateway state | NOT running (unchanged, pre-existing); job is paused so nothing fires; the one-shot proof used `hermes cron run` + `hermes cron tick` (native manual mechanism) |

Bug found and fixed during proof (behavioral, not guessed): the first
cron-fired run crashed with `ModuleNotFoundError: pydantic_core._pydantic_core`
— the scheduler's inherited `PYTHONPATH` (Hermes runtime site-packages, Python
3.14) shadowed the project venv's (Python 3.12) imports. The wrapper now
REPLACES `PYTHONPATH` with `<repo>\src` (never appends) — the exact
`mcp-ayce-director` runner convention. The second cron-fired run reached the
real AYCE CLI correctly.

## Behavioral evidence

| Proof | Command | Observed |
|---|---|---|
| Wrapper dry-run SUCCESS (valid non-production target) | wrapper run under **Hermes' own python 3.14** (the cron interpreter) with `AYCE_DATA_DIR=<isolated sandbox>` + `vidFake12345678` | JSON `{"ok": true, "dry_run": true, "youtube_video_id": "vidFake12345678", "lineage": {package/pkg_seal/run/research/script/objective ids}, "would_collect": [DATA_API_V3 + ANALYTICS_API_V2 plans], "message": "dry run: … NO API call was made and NOTHING was stored"}` — **EXIT=0** |
| Production-mode truthful rejection | same wrapper, NO `AYCE_DATA_DIR` (production config), same target | `{"ok": false, "error": {"code": "analytics_lineage_invalid", "message": "no publication ledger entry for this video id; nothing is attributed"}}` — **EXIT=1** (correct: no video is published yet) |
| No-target refusal (cron-mode default) | wrapper with no args, TARGET empty | stderr "no collection target configured … Nothing was executed." — **EXIT=2** |
| Missing-interpreter safety | wrapper guard | interpreter/repo checked before spawn (exit 3 path) — present here, so not exercised |
| No external API contact | all runs above | `--dry-run` hardcoded; collector docstring §27 + observed `dry_run: true`; no credentials present anywhere |
| No unwanted DB mutation | filesystem checks | production `data\analytics` absent (never created); `data\runs` absent; `data\publishing\ledger.sqlite3` exists but is SCHEMA-ONLY — **0 rows** (verified read-only: `publish_attempts rows: 0`); the file was created by the CLI's own `PublishLedger()` instantiation during the production-mode rejection run (pre-existing CLI behavior, no records) |
| Sandbox store absence | `Test-Path …p07b_sandbox\data\analytics` → **False** | dry-run persisted nothing even where a store would exist |
| Exit-code propagation | exit codes 0 / 1 / 2 / 3 mapped (observed 0, 1, 2) | — |

**Sandbox fixture (for the "valid non-production test invocation"):** built in
`.tmp\p07b_sandbox\` using the project's OWN test harness
(`tests/unit/test_analytics.py` helpers loaded by path): real pipeline run
(`run-20260927T145357Z-7f08f94db73f`, QA PASS) → real seal → REAL Stage 5
publisher with the FAKE transport → published record `vidFake12345678` in the
sandbox ledger. No network, no real credentials, production `data/` untouched.
Hermes-managed FFmpeg (`…\hermes\tools\ffmpeg-9.0.1-win32-x64\bin`) was added
to PATH for the fixture run (same toolchain the H4/H5 verified runs used).

## Hermes evidence (actual outputs)

- `hermes cron create "every 6h" --name ayce-analytics-collect --script
  ayce_analytics_collect.py --no-agent --paused --paused-reason "…" --deliver
  local` → `Created job: 19a6cce6796c … Created PAUSED — resume to schedule,
  or explicitly run now.` (EXIT=0)
- `hermes cron list` → exactly ONE job: `19a6cce6796c [paused]`,
  `ayce-analytics-collect`, `every 360m`, `Deliver: local`,
  `Script: ayce_analytics_collect.py`, `Mode: no-agent` — **no duplicates, no
  unrelated jobs**.
- `hermes cron status` → "No gateway is running on this host — cron jobs will
  NOT fire. Scheduler last ticked…" (pre-existing operational state).
- **Tick proof (one-shot):** `hermes cron resume` → `hermes cron run` →
  `hermes cron tick` → job executed by the scheduler. First attempt exposed
  the PYTHONPATH bug (see above); after the fix the durable record shows the
  full chain — `hermes cron runs 19a6cce6796c`:
  `263425df… failed … Script exited with code 1` with the AYCE CLI's
  structured JSON (`analytics_lineage_invalid — no publication ledger entry
  for this video id; nothing is attributed`) on **stdout** — i.e. Hermes cron
  → wrapper script → pinned interpreter → EXISTING AYCE CLI → structured
  truthful result, exit code propagated end-to-end.
- Final safe state: TARGET reset to `""` (the fired-run failure above is the
  correct production behavior for a not-yet-published video); job re-paused
  (`hermes cron pause` → "Paused job: ayce-analytics-collect"); wrapper
  TARGET is empty so any accidental resume truthfully exits 2 instead of
  inventing a target.
- Post-fix wrapper dry-run re-verified from a CLEAN environment
  (no inherited PYTHONPATH): `ok: true, dry_run: true`, **EXIT=0**.
- Dashboard visibility: the dashboard renders the same jobs store this CLI
  reads; the Commander should see ONE paused job `ayce-analytics-collect`
  (script mode, every 360m). Independent check: `hermes cron list`.

## Authentication

`AUTHENTICATED_COLLECTION = BLOCKED` — no YouTube OAuth credentials are
configured (production publish ledger has 0 rows and was only created
schema-only by the CLI; `data/analytics` and `data/runs` do not exist). No
credentials were fabricated. The wrapper is hard-wired to `--dry-run`, so even
an accidental resume performs NO API call and NO storage.

## Files changed

| Path | Change |
|---|---|
| `%LOCALAPPDATA%\hermes\scripts\ayce_analytics_collect.py` | NEW (repo-external, Hermes scripts boundary) — the only functional artifact |
| `docs/phase0/09_hermes_cron_ayce_analytics_evidence.md` | this addendum |
| Hermes cron store | ONE paused job created via native CLI (job `19a6cce6796c`); two durable tick-proof run records (documented above) |
| `src/ayce/**`, `ayce-readonly`, `ayce-director`, Hermes config | UNTOUCHED |

Repository-side: only this evidence document is committed (the wrapper lives
inside Hermes' scripts directory by design and is covered by the rollback
below, not by git).

## Rollback

```text
hermes cron remove 19a6cce6796c
Remove-Item %LOCALAPPDATA%\hermes\scripts\ayce_analytics_collect.py
```
Nothing else to revert (no repo source/config/test changes; `.tmp/` sandbox
and diagnostics deleted after evidence capture).

## Known limitations

- `Authenticated collection = BLOCKED` (no OAuth configured; wrapper is
  dry-run-only by design for now).
- The paused job will truthfully fail with "no collection target configured"
  if resumed before an operator sets TARGET to a real published video — by
  design (no target is ever invented).
- Production-level tri-tier scheduling (30m–6h fast tier, daily harvest,
  Day 7/14/30 backfill; Reporting-API bulk CSV) remains future work — this
  phase proves the SINGLE integration path with the smallest safe cadence.
- The Hermes gateway is not installed/running (pre-existing); enabling
  scheduled firing is an explicit operational decision recorded in
  `hermes cron status`.

## Verification summary (STEP 9)

| Check | Result |
|---|---|
| Wrapper exists in Hermes scripts boundary | yes |
| Wrapper invokes the pinned `.venv` interpreter (absolute path, PYTHONPATH=repro src) | yes — proven by sandbox dry-run success + cron-fired run reaching `src\ayce\cli.py` |
| Dry-run succeeds | yes — `ok:true, dry_run:true`, EXIT=0 (sandbox; also re-verified after the PYTHONPATH fix under a clean env) |
| Exit-code propagation | yes — 0/1/2/3 mapped; observed 0, 1, 2 |
| No external API contact | yes — `--dry-run` hardcoded; collector §27/§28; no credentials present |
| No unwanted DB mutation | production `data\publishing` 0 rows, `data\analytics` absent, `data\runs` absent; sandbox `data\analytics` absent after dry-runs |
| Hermes sees the cron job | yes — `19a6cce6796c`, paused, every 360m, exact script path, single job |
| Existing AYCE tests | `tests/unit/test_analytics.py` — 49 passed, EXIT=0 (no repo code changed) |

## PHASE_0_7B_IMPLEMENTATION = complete

## HERMES_CRON_AYCE_ANALYTICS = VERIFIED

---

# ADDENDUM 2 — PHASE 0.7B TARGET RESOLUTION FIX

**Starting HEAD:** `6789b5a`. Issue: the cron-fired wrapper exited 2 with
"no collection target configured" — cron mode had no authoritative target
source. Task: make cron obtain `<video_id|run_id>` from EXISTING project
state without inventing or hard-coding one.

## Authoritative target source discovered

| Item | Evidence |
|---|---|
| Source of truth | **The Stage 5 publish ledger** `data\publishing\ledger.sqlite3` (`publish_attempts` table). Already treated as authoritative by the project itself: `mcp-ayce-readonly/server.py:129-136` resolves exactly this file as "the AUTHORITATIVE run → video mapping" (opened READ-ONLY, `AYCE_PUBLISH_LEDGER` env override honored) and the analytics collector docstring §24 states "The ledger is AUTHORITATIVE for the publication". |
| Eligibility rule (existing, not invented) | `src/ayce/analytics/collector.py:233` `_resolve_from_record`: only `status == "published"` records WITH a `youtube_video_id` carry analytics. The wrapper query mirrors exactly this rule: `SELECT youtube_video_id FROM publish_attempts WHERE status='published' AND youtube_video_id IS NOT NULL`. |
| Existing selection rule for multiple eligible records | NONE exists in the project (each video has its own tri-tier cadence). Therefore the wrapper does NOT invent ranking: 0 eligible → truthful refusal; 1 eligible → collect it; >1 eligible → refusal requiring operator disambiguation via TARGET. |
| Real current state | Production ledger exists, **0 rows** (verified read-only before AND after every run) — no eligible published target exists. |

## Wrapper change (surgical, same file)

`%LOCALAPPDATA%\hermes\scripts\ayce_analytics_collect.py`:
- NEW `_resolve_cron_target()`: opens the ledger **read-only** (`file:…?mode=ro` — the file is never created or mutated by the wrapper; missing file → "no video has ever been published" refusal), applies the collector's own eligibility rule, returns the single eligible `youtube_video_id`, or a truthful refusal (0 eligible / >1 eligible requiring operator disambiguation).
- Cron mode (no argv): `TARGET` operator override first (unchanged semantics), else ledger auto-resolution. Manual mode (`argv[1]`) UNCHANGED (regression-verified: same structured `analytics_lineage_invalid` output, exit 1).
- `sqlite3` is stdlib — no dependency added. No new state file/table/DB.

## Verification (real existing state only — no fabricated ID)

| Proof | Command | Observed |
|---|---|---|
| Cron-mode ledger resolution (real empty ledger) | wrapper, no args, under Hermes' own python 3.14 | stderr `publish ledger contains no published video - nothing to collect (no target is ever invented)` — **EXIT=2** |
| Ledger untouched by resolution | read-only row count after | `publish_attempts rows: 0` (mode=ro; file never written) |
| Manual mode regression | wrapper `vidFake12345678` | same structured `analytics_lineage_invalid` JSON, **EXIT=1** (unchanged) |
| Cron-fired end-to-end | `hermes cron resume` → `hermes cron run` → `hermes cron tick` → `hermes cron runs` | durable record `a818d36b…` (`source=direct`): "Script exited with code 2" + the ledger-based refusal message on stderr — i.e. Hermes cron → wrapper → authoritative ledger read → truthful no-target decision, captured by Hermes |
| Job safety | `hermes cron pause` → `hermes cron list` (checked twice) | `19a6cce6796c [paused]` — persists; gateway still NOT running (nothing can fire) |

## What is still missing (exact blocker)

**At least one real published record** in the production publish ledger
(`publish_attempts` row with `status='published'` and a real
`youtube_video_id`). Producing one requires a REAL Stage 5 publication
(OAuth + operator-approved publish) — explicitly out of scope and not
fabricated. The moment such a record exists (and only one is eligible), the
paused cron job will resolve it automatically; with multiple eligible records
the operator must set TARGET (no invented ranking).

`AUTHENTICATED_COLLECTION = BLOCKED` (unchanged).

## Files changed

| Path | Change |
|---|---|
| `%LOCALAPPDATA%\hermes\scripts\ayce_analytics_collect.py` | target-resolution change only (ledger read-only resolution; manual mode untouched) |
| `docs/phase0/09_hermes_cron_ayce_analytics_evidence.md` | this addendum |
| Hermes cron store | job `19a6cce6796c` remains PAUSED; one new durable run record (`a818d36b…`) from the verification tick |
| `src/ayce/**`, MCPs, Hermes config, dependencies | UNTOUCHED |

Rollback (unchanged mechanism): `hermes cron remove 19a6cce6796c` + delete the
wrapper script.

## TARGET_RESOLUTION = BLOCKED
(blocker: zero eligible published records in the authoritative publish
ledger — mechanism implemented, no-target path proven through the real cron
path; no fabricated target)

## PHASE_0_7B_TARGET_RESOLUTION = BLOCKED