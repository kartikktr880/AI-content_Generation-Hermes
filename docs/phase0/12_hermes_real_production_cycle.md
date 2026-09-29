# TASK 1 — HERMES-DIRECTED REAL PRODUCTION CYCLE (research → brief → produce → verify)

**Task identification.** Derived from the approved project sources, not invented:
the Master Project Description / engineering north star (Commander store
`paste_2_125644.txt`, §1 ultimate loop, §35 "Hermes is the intended high-level
director", §41 production job model, final mantra `DISCOVER → UNDERSTAND → DECIDE
→ CREATE → PRODUCE → VERIFY → PUBLISH → MEASURE → LEARN`) plus the already-approved
director design and H6/H7/H8 experiment records (`mcp-ayce-director/h6…h8` prompts,
`docs/stage2_research_intelligence.md`, `docs/hermes_director_h3_design.md`).

**Why this is the next approved work item.** Task 0 proved Hermes operating the
Golden Path over the **allowlisted documentary fixture** only. The real
research→brief→production chain exists as approved, integrated capability
(`execute_research_slice`, `propose_script_brief`, `trigger_golden_path`) but had
never been operated by Hermes on this machine state — the 2026-09-27 chain was run
directly through the CLI. The next executable item in the approved loop is
therefore: **Hermes directs one real research-derived production cycle and
verifies it through the read-only surface.** No new resource, no new code, no new
install — pure composition of already-selected capability through Hermes.

**Boundary honoured:** the cycle stops before PUBLISH. Publishing remains blocked
account-side (`403 authenticatedUserAccountSuspended`, Phase 0.7B) and requires
explicit Commander authorization.

## 1. What was executed (exact Hermes runtime chain)

```text
Hermes one-shot agent session (gemini-3.6-flash, per-session -m; no config change)
  → execute_research_slice   (MCP ayce-director → real yt-dlp ingestion worker)
  → propose_script_brief     (MCP ayce-director → deterministic Stage 1 bridge, no LLM)
  → trigger_golden_path      (MCP ayce-director → AYCE Golden Path engines)
  → get_run_state / get_run_artifacts (MCP ayce-readonly, observation surface)
```

Two Hermes sessions, mirroring the approved H6/H7/H8 single-purpose session
pattern (launched through Hermes' own interpreter/entry point, cwd = repo, Hermes
FFmpeg fronted on PATH for the render/QA stages):

| Session | Prompt file | Tool calls observed |
|---|---|---|
| `20260929_002646_*` (research) | `mcp-ayce-director/h10_research_slice_prompt.txt` | `tool_describe` → `mcp__ayce_director__execute_research_slice` |
| `20260929_002924_2bbd54` (direct) | `mcp-ayce-director/h10_direct_prompt.txt` | `tool_describe` → `propose_script_brief` → `trigger_golden_path` → (batched readonly call rejected by Hermes → model split it) → `get_run_state` → `get_run_artifacts` |

`NON-AYCE tools used`: only Hermes' built-in `tool_describe`/`tool_call`
dispatchers — **zero shell/terminal/file/browser/web tools** in both sessions.

## 2. Real results (verbatim from the Hermes tool results)

**Research slice** (`execute_research_slice`, real network ingestion):

| Field | Value |
|---|---|
| `research_id` | `res-20260929T072643Z-77ca29a092d7` |
| `status` / `candidate_count` | `SUCCESS` / 6 |
| request | objective `Identify outlier opening-hook formats in the AI productivity tools niche`; niche `AI productivity tools`; query `AI productivity tools workflow automation`; `max_outliers 2`, `max_videos 4` |
| outliers (2) | `4uvX6dxD6QA` "5 AI for Work Tips and Tricks" (Kevin Stratvert, 221,597 views, velocity 260.0/day); `FwOTs4UxQS4` "AI Agents, Clearly Explained" (Jeff Su, 5,201,695 views, velocity 9645.1/day) |
| provenance | worker 1.0, yt-dlp `2026.08.19`, clustering `lexical_cosine_dbscan` (`fallback_lexical`, fastembed not installed) |
| artifact | `data/research/res-20260929T072643Z-77ca29a092d7.json` |

**Brief conversion** (`propose_script_brief`, deterministic, no LLM):

| Field | Value |
|---|---|
| `ok` / duplicate | `true` / `false` (newly generated) |
| `script_id` | `brief-77ca29a092d7` |
| `script_sha256` | `f02f227b8e93d3e6d13cce899fcb0d20a9f6cbbaa43ac7eb4a6d3322afdae188` |
| registered | appended to `mcp-ayce-director/scripts.json` (never overwriting operator entries); persisted at `mcp-ayce-director/generated_scripts/brief-77ca29a092d7.json` |

**Production** (`trigger_golden_path`, `request_id req-20260929-h10-real-prod-01`):

| Field | Value |
|---|---|
| `ok` / `status` / `exit_code` | `true` / `succeeded` / `0` |
| `run_id` / `job_id` | `run-20260929T073007Z-01adeb428f9c` / `job-20260929T073007Z-55fd291324d3` |
| `qa_verdict` | `PASS` |
| stages | 7/7 succeeded (script_to_scene, asset_resolution, narration_audio, timeline, captions, production_render, media_qa) |
| script used | the **generated brief** `brief-77ca29a092d7` — NOT `documentary` (stated explicitly by Hermes and proven by the artifacts) |

## 3. Independent verification (outside Hermes + inside Hermes)

**Inside Hermes** (session `20260929_002924_2bbd54`, read-only MCP tools):
7/7 stages `succeeded` (attempts 1), **10** artifacts, `qa_report verdict PASS,
total_checks 15, failed_checks 0`.

**Outside Hermes** (`mcp-ayce-readonly\.venv\Scripts\python.exe
mcp-ayce-director\verify_via_readonly.py run-20260929T073007Z-01adeb428f9c` — real
read-only MCP stdio transport):

| Check | Observed |
|---|---|
| `get_run_state` | 7/7 `succeeded`, attempts 1, `last_error: null`, job_id matches |
| artifact count | **10** |
| `script_input` metadata | `research_id: res-20260929T072643Z-77ca29a092d7`, `script_id: brief-77ca29a092d7`, `objective_id: obj-5969bacf4a332431` → **real research lineage** |
| `research_input` (`research_reference.json`) | `research_sha256: 1351740dbc1a23972a7ee41fc477578d430df644d5b9837a764a7bc2317f3e9e`, `reference_only: true` |
| scene manifest | **4 scenes** (the documentary fixture run produces 5) |
| timeline | 4 scenes, `total_duration_seconds 69.2` (fixture run: 34.0) |
| `rendered_video` | `render/res-20260929T072643Z-77ca29a092d7.mp4`, 4 scenes, **69.328 s**, 64×64, sha256 `a0a89c2cd67899cc22a47f368500abef391e3022b38a2bc54905eca7a26007af` |
| `qa_report` | `PASS`, 15 checks, 0 failed, `render_sha256` equals the rendered_video sha256 |
| `policy_consumption` | `cons-32e80b7374e37e3888b9edc9`, `decision_id req-20260929-h10-real-prod-01` |

**Content provenance (the production is research-derived, not fixture):** the
generated brief's narration quotes the ingested evidence verbatim — e.g. scene 1
quotes the recorded opening hook *"We're going to look at five practical ways you
can start using AI in your work or at school right now…"*, scene 3 reports the
observed candidate metrics ("5 AI for Work Tips and Tricks", Kevin Stratvert,
221,597 views, velocity proxy 260 views/day) — and the run's `production_id` is
the research id itself.

## 4. State deltas (all produced by the runs, nothing hand-edited)

| Counter | Before | After |
|---|---|---|
| `data/research/*.json` | 1 | **2** (`+ res-20260929T072643Z-77ca29a092d7.json`) |
| `mcp-ayce-director/research_requests.json` records | 2 | **3** |
| `mcp-ayce-director/brief_requests.json` records | 4 | **5** |
| `mcp-ayce-director/requests.json` records | 9 | **10** |
| `mcp-ayce-director/scripts.json` entries | documentary, brief-e8f295ac6ec8, brief-215a22d36c19 | **+ brief-77ca29a092d7** |
| `data/runs/` directories | 3 | **4** |

## 5. What this task does NOT claim

* **Not published.** No upload, no publish ledger row, no YouTube action — the
  cycle intentionally ends at QA-verified output. Publishing stays behind the
  Phase 0.7B account-side blocker and requires explicit Commander authorization.
* **Not new capability.** Zero code/dependency/config change in AYCE, the MCP
  components or Hermes; every step is an already-approved, already-integrated
  capability composed through Hermes.
* **Not a fixture run.** The research, brief and scene/narration content derive
  from real ingested YouTube evidence (provenance recorded in the artifact).

## 6. Files changed by this task

| Path | Change |
|---|---|
| `mcp-ayce-director/h10_research_slice_prompt.txt` | NEW — research-slice session prompt |
| `mcp-ayce-director/h10_direct_prompt.txt` | NEW — brief→produce→observe session prompt |
| `mcp-ayce-director/research_requests.json` | +1 record (by the run) |
| `mcp-ayce-director/brief_requests.json` | +1 record (by the run) |
| `mcp-ayce-director/requests.json` | +1 record (by the run) |
| `mcp-ayce-director/scripts.json` | +1 generated brief entry (by the run) |
| `docs/phase0/12_hermes_real_production_cycle.md` | NEW — this evidence record |
| `docs/phase0/00_phase0_ledger.md` | appended §10 |
| `data/research/res-20260929T072643Z-77ca29a092d7.json`, `data/runs/run-20260929T073007Z-01adeb428f9c/**` | produced by the runs (git-ignored) |
| Hermes config, `src/ayce/**`, `tests/**`, `mcp-ayce-readonly/**`, `.env` | **untouched** |

## 7. Acceptance

| # | Criterion | Verdict |
|---|---|---|
| A | Hermes directs the DISCOVER/RESEARCH leg (real ingestion) | PASS — `execute_research_slice` through Hermes, real yt-dlp evidence |
| B | Hermes directs the CREATE leg (research → validated brief) | PASS — new `brief-77ca29a092d7` registered, `duplicate: false` |
| C | Hermes directs the PRODUCE leg (Golden Path on the brief) | PASS — 7/7 stages, QA PASS, exit 0 |
| D | Hermes verifies through the read-only surface | PASS — `get_run_state`/`get_run_artifacts` in-session + independent probe |
| E | Production is research-derived (not fixture) | PASS — lineage in `script_input`/`research_input`, 4 scenes/69.3 s vs 5/34.0 s, verbatim hooks |
| F | MCP-only boundary held | PASS — transcripts show only `tool_describe`/`tool_call` + AYCE MCP tools |
| G | No new resource/code/config | PASS — zero changes outside the run-produced ledgers and the prompt/evidence docs |
| H | Cycle stops before PUBLISH | PASS — nothing published |

**TASK 1 (Hermes-directed real production cycle) = VERIFIED.**

## 8. PACKAGE task — sealed publish package for the Task 1 run (VERIFIED)

**Capability used (no new tool, no code change):** the already-verified Stage 4
packaging boundary `python -m ayce package <run_id>` (seal) /
`--verify` (`src/ayce/publish_package.py`: deterministic, QA-gated,
immutable-after-seal, content-derived `package_id` + `seal`).

**Operated through Hermes** — one-shot session `20260929_004650_02f3e1`
(`gemini-3.6-flash`), tool used: `terminal` (only). The agent executed exactly
the two approved commands (chained in one shell invocation) from the repository
root and reported every output verbatim:

```text
.venv\Scripts\python.exe -m ayce package run-20260929T073007Z-01adeb428f9c --json           # seal
.venv\Scripts\python.exe -m ayce package run-20260929T073007Z-01adeb428f9c --verify --json  # verify
```

Hermes terminal result (verbatim JSON, exit code 0 for both):

```json
{"ok": true, "package_id": "pkg-6c0d9cc96879e3f0",
 "run_id": "run-20260929T073007Z-01adeb428f9c",
 "verified_content": true, "errors": []}
```

**Sealed package** (`data/runs/run-20260929T073007Z-01adeb428f9c/publish_package.json`):

| Field | Value |
|---|---|
| `package_version` | 1 |
| `package_id` | `pkg-6c0d9cc96879e3f0` |
| `seal` | `algorithm: sha256`, `value: 1f8cb27b01af785efe24a8e6298446e910c21d6e89f5349d34eba269723c51e7` |
| `run_id` / `job_id` | `run-20260929T073007Z-01adeb428f9c` / `job-20260929T073007Z-55fd291324d3` |
| `production_id` | `res-20260929T072643Z-77ca29a092d7` |
| `lineage` | `objective_id obj-5969bacf4a332431`, `research_id res-20260929T072643Z-77ca29a092d7`, `script_id brief-77ca29a092d7`, `script_sha256 9531ced8…`, `research_sha256 1351740d…`, `objective_text "Identify outlier opening-hook formats in the AI productivity tools niche"` |
| `render` | `art-20260929T073017Z-06f02221cd1c`, `render/res-20260929T072643Z-77ca29a092d7.mp4`, sha256 `a0a89c2c…`, 28,902 B |
| `qa` | `verdict PASS`, `total_checks 15`, `failed_checks 0`, `render_sha256` equal to the render sha |
| `artifact_manifest` | **10 entries** — asset_manifest, audio, captions, qa_report, policy_consumption, rendered_video, research, scene_manifest, script, timeline |

**Verification (existing approved surfaces only):**

| Criterion | Evidence |
|---|---|
| exact real run packaged | `run_id` inside the sealed package == `run-20260929T073007Z-01adeb428f9c` |
| package created successfully | `ok: true`, `publish_package.json` present in the run directory, exit 0 |
| package sealed/verified | content seal `1f8cb27b…`; `ayce package --verify` → `verified_content: true`, `errors: []` (run **through Hermes**) |
| lineage points to the correct run | `lineage.research_id`/`script_id`/`objective_id` + `production_id` == the Task 1 research, brief and run; `render.sha256` / `qa.render_sha256` match the run's rendered_video artifact |
| expected artifacts present | 10-entry `artifact_manifest` covering every Golden Path artifact of the run |
| no corruption / missing artifact | `errors: []`, `verified_content: true` (the verifier recomputes artifact sizes/sha256 against the run directory) |
| metadata consistent with the source run | `created_at` = the run's own timestamp; `qa` block equals the run's `qa_report` metadata |
| **not published** | publish ledger `publish_attempts` unchanged at **1 row** (the old blocked 2026-09-27 attempt) — no publish attempt, no upload, no YouTube action |

**Files changed:** `data/runs/run-20260929T073007Z-01adeb428f9c/publish_package.json`
(produced by the sealing), `mcp-ayce-director/h11_package_prompt.txt` (session
prompt), this section, ledger §11. **No `src/`, `tests/`, Hermes config, MCP
config, cron or dependency change.**

**PACKAGE task = VERIFIED.**
