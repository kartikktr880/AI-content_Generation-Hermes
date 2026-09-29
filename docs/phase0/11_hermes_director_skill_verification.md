# TASK 0 — HERMES-NATIVE IMPLEMENTATION + RUNTIME VERIFICATION
## (director write boundary + project-knowledge skill)

**Scope.** Complete the two genuinely unresolved Task 0 requirements **inside
Hermes** — implement/onboard, then actually exercise them through Hermes and
record runtime evidence. No Task 1 work, no new research, no reinstall of the
already-verified MCPs, no project-wide test suite as acceptance evidence.

**Authoritative Task 0 source.** `docs/phase0/00_phase0_ledger.md` (task log §3,
open blockers/proposed continuation §5, re-verification §6, blocker status §6.1)
with the per-task evidence docs `docs/phase0/01…10`. The two items §6.1 marked
**NOT executed** are the unresolved requirements:

| # | Requirement (Task 0 source) | Status before this task | Status now |
|---|---|---|---|
| **U1** | One controlled Golden Path execution **through the director boundary**, verified independently through the read-only server (ledger §5 item 4; basis `docs/hermes_director_h4_experiment.md` §5, `docs/hermes_director_h5b_idempotency.md`, H5 "Next step (smallest)") | NOT executed | **VERIFIED** |
| **U2** | Project-knowledge skill/asset set inside Hermes' **real skill surface** so Hermes can consult the project constitution + stage contracts (ledger §5 item 5; basis Task 0.6 doc §10 recommended onboarding action) | NOT executed | **VERIFIED** |

Deliberately **not** touched: `ayce-readonly`/`ayce-director` registration
(already verified — reused, not reinstalled), the paused analytics cron job, the
`0.4 → 0.15` definitions blocker, `youtube-analytics-harvester` (BLOCKED), and
Phase 0.7B publishing/analytics (Commander/account-side blocker).

## 1. Newly established runtime fact — the LLM provider is live

Ledger §6 recorded "LLM-driven agent round trip — NOT VERIFIED (pre-existing,
credential-gated)". Re-inspection shows the blocker is **stale**: Hermes is
configured against a **local OpenAI-compatible endpoint** that is running and
reachable, so an LLM-driven Hermes session needs **no external credential**.

| Item | Observed |
|---|---|
| Configured model config (`config.yaml`) | `provider: openai`, `default: gemini-3.1-pro`, `base_url: http://127.0.0.1:8081/v1`, `api_key: <local dummy key>` (a dummy value for a **local** proxy — no external credential exists or is needed) |
| Endpoint liveness | `GET http://127.0.0.1:8081/v1/models` → HTTP 200, gemini-* model list; listener PID 5080 |
| Real one-shot agent session | `hermes -z "Reply with exactly: PONG"` → `PONG` (42 s) |
| Default model `gemini-3.1-pro` | **unreliable** on this proxy (the endpoint's own model listing notes it "requires cookie for real routing"); it answered AYCE prompts with an apology instead of acting |
| Working model override | `-m gemini-3.7-flash` / `-m gemini-3.6-flash` → tool-calling sessions complete normally |
| Rate limiting | `gemini-flash-lite` / occasionally `gemini-3.7-flash` return the proxy's "getting a lot of requests right now" message — the same behaviour documented in `docs/hermes_director_h5_composition.md` |

**No Hermes configuration was changed** (default model, provider, MCP, cron and
skills config untouched except the project-skill trust entry — §6). The model was
overridden per session with the top-level `-m` flag, exactly the pattern H5 used.

## 2. U1 — Controlled Golden Path through the director boundary (VERIFIED)

Chain actually executed:

```text
Hermes one-shot agent session (gemini-3.6-flash)
  → Hermes tool dispatcher `tool_call`
    → MCP stdio `ayce-director` :: trigger_golden_path     (the ONLY write tool)
      → fixed argv: <repo>\.venv python -m ayce run <allowlisted documentary fixture> --json
        → AYCE Golden Path: script_to_scene → asset_resolution → narration_audio
          → timeline → captions → production_render → media_qa   (existing engines)
  → MCP stdio `ayce-readonly` :: get_run_state               (observation surface)
  → MCP stdio `ayce-readonly` :: get_run_artifacts           (independent verification)
```

**Exact invocation** (single-line one-shot prompt; `-z` carries the whole line):

```pwsh
hermes -m gemini-3.6-flash -z "<line 2 of mcp-ayce-director/h9b_director_execute_prompt.txt>"
```

Launcher note (reproducibility): `%LOCALAPPDATA%\hermes\bin\hermes.exe` no longer
exists in v0.21.5 (only `hermes.cmd`), and `Start-Process` cannot redirect a
`.cmd`. The sessions were therefore launched through Hermes' **own** interpreter
and entry point — byte-for-byte what `hermes.cmd` runs
(`%LOCALAPPDATA%\hermes\tools\python-3.14.7+20260901-win32-x64\python.exe` +
`sys.path.insert(<hermes-agent>)` + `hermes_cli.main:main`) — with the same
`-m <model> -z "<prompt>"` arguments. The cwd was the repository root (required
for the project-local skill to load) and Hermes' own FFmpeg directory
(`%LOCALAPPDATA%\hermes\tools\ffmpeg-9.0.1-win32-x64\bin`) was fronted on `PATH`
for the render/QA stages.

**Real result (verbatim from the Hermes tool result):**

| Field | Value |
|---|---|
| `ok` / `status` | `true` / `succeeded` |
| `request_id` | `req-20260928-h9-golden-compose-01` |
| `run_id` | `run-20260929T064556Z-3b422d61fce8` |
| `job_id` | `job-20260929T064556Z-4bff2a419bd7` |
| `qa_verdict` / `exit_code` | `PASS` / `0` |
| `stages` | 7/7 `ok: true` (script_to_scene, asset_resolution, narration_audio, timeline, captions, production_render, media_qa) |
| policy context | `policy_status: none`; decision `trigger_golden_path`; consumption `cons-e9a5189766ba95c3faeb659f` |

**Hermes session transcript** (`20260928_234452_4cc22a`, read-only dump via
`mcp-ayce-director/dump_session_h5.py`):

```text
[115] assistant TOOL_CALLS=[{"function":{"name":"mcp__ayce_director__trigger_golden_path",
        "arguments":"{\"request_id\":\"req-20260928-h9-golden-compose-01\",\"command\":
        \"trigger_golden_path\",\"script_id\":\"documentary\",\"reason\":\"H9 Task 0 …\"}"}}]
[117] assistant TOOL_CALLS=[tool_call calls=[mcp__ayce_director__trigger_golden_path]]
[118] tool tool=mcp__ayce_director__trigger_golden_path
        <untrusted_tool_result …> {"ok": true, "status": "succeeded", "run_id":
        "run-20260929T064556Z-3b422d61fce8", "qa_verdict": "PASS", "exit_code": 0, …}
[119] assistant TOOL_CALLS=[tool_call calls=[mcp__ayce_readonly__get_run_state
        {"run_id":"run-20260929T064556Z-3b422d61fce8"}]]
[120] tool tool=mcp__ayce_readonly__get_run_state   <untrusted_tool_result …> {"ok": true …}
NON-AYCE tools used in this session: ['mcp__ayce_director__trigger_golden_path',
                                     'mcp__ayce_readonly__get_run_state']
```

**Independent verification (outside Hermes, via the project's existing read-only
probe on the real read-only MCP server):**

```pwsh
mcp-ayce-readonly\.venv\Scripts\python.exe mcp-ayce-director\verify_via_readonly.py run-20260929T064556Z-3b422d61fce8
```

- `get_run_state` → 7/7 stages `succeeded`, `attempts: 1`, `last_error: null`,
  `job_id: job-20260929T064556Z-4bff2a419bd7` (matches the director result).
- `get_run_artifacts` → **8** artifacts: scene_manifest (5 scenes),
  asset_manifest (4 assets, `file-backed-fixtures`), audio (5,
  `file-backed-narration-fixtures`), timeline (5 scenes, 34.0 s), captions
  (5 cues + `captions.srt`), rendered_video
  (`render/job-20260917T131500Z-1a2b3c4d5e6f.mp4`, sha256
  `df90f4863f6e91aa483b0341e3927b5b5b4bbf8fb43cfa6814db84d355e97ef5`), qa_report
  (`verdict: PASS`, `total_checks: 16`, `failed_checks: 0`, `render_sha256`
  equal to the rendered_video sha256), policy_consumption.
  (The probe's shell exit code 1 is PowerShell treating the MCP server's
  informational stderr lines as a native-command error; the tool results above
  are complete and `ok: true`.)

**Idempotency proof through Hermes** (`h9c_duplicate_prompt.txt`, session
`20260928_234741_93137d`, model `gemini-3.6-flash`) — the exact same four field
values re-issued:

```json
{"ok": false, "request_id": "req-20260928-h9-golden-compose-01",
 "error": {"code": "duplicate_request",
           "message": "request_id already processed; no second run launched. original status: succeeded"},
 "original": {"status": "succeeded", "run_id": "run-20260929T064556Z-3b422d61fce8",
              "job_id": "job-20260929T064556Z-4bff2a419bd7", "qa_verdict": "PASS", "exit_code": 0}}
```

| No-second-execution evidence | Before duplicate | After duplicate |
|---|---|---|
| Run directories in `data/runs` | 3 | **3** |
| Director ledger `requests` records | 9 | **9** |
| New run directory file count | — | 20 (unchanged) |

## 3. U2 — Project-knowledge skill in Hermes' real skill surface (VERIFIED)

**Mechanism** (the one documented in Task 0.6 §10): a **project-local skill**
plus Hermes' own project-skill trust mechanism — no registry install, no network,
no invented skill.

| Step | Exact action | Observed |
|---|---|---|
| Author | created `.hermes/skills/ayce-project-knowledge/SKILL.md` in the repository (frontmatter + constitution, Golden Path, stage-contract map, Hermes operating surfaces, director-run rules, verification, pitfalls) | file present; `.gitignore` does **not** ignore `.hermes` (`git check-ignore` → exit 1) |
| Trust | `hermes skills trust C:\Users\Administrator\AI-Content-Generation` | `Trusted: …` + "1 project skill(s) will load in sessions started inside this repo" |
| Registry state | `hermes skills list --source local` | `ayce-project-knowledge  |  | local  | local  | enabled` → `0 hub-installed, 0 builtin, 1 local — 1 enabled` |
| Total skill surface | `hermes skills list --source all` | `0 hub-installed, 51 builtin, 1 local — 52 enabled, 0 disabled` (was 51 builtin / 0 local) |
| Runtime prompt surface | `hermes prompt-size --json` | `skills_index` 5076 → **5203** bytes; `system_prompt` 20153 → **20275** bytes; skill count 51 → **52**; the breakdown lists `ayce-project-knowledge` with its repo `SKILL.md` path |
| Prompt actually built for a session | read-only dump of Hermes' stored system prompt for session `20260928_230816_c6114f` | contains `  ayce-project-knowledge:` / `    - ayce-project-knowledge: [project] AYCE constitution, Golden Path and stage contracts for He…` → the skill is on the **live runtime prompt** of a repo session |
| **Hermes retrieves the knowledge** | one-shot session `20260928_235021_5c7864` (`-m gemini-3.6-flash`) instructed to open the skill | transcript `[133] TOOL_CALLS=[skill_view {"name":"ayce-project-knowledge"}]` → `[134] tool tool=skill_view` → `{"success": true, "name": "ayce-project-knowledge", "description": "AYCE constitution, Golden Path and stage contracts for Hermes.", "content": "---\nname: ayce-project-knowledge…"}` → `[135]` answer copied verbatim: `script_to_scene, asset_resolution, narration_audio, timeline, captions, production_render, media_qa` / `request_id, command, script_id, reason` / `mcp__ayce_readonly` |

Control observation (recorded for honesty): the first, non-directive attempt
(session `20260928_233734_14f76b`, same model, no mandatory tool call) answered
**from the index line alone with zero tool calls** and produced a hallucinated
stage list. Only the tool-forcing prompt produced faithful content — i.e. the
skill *is* loadable and *is* used, but the model must be directed to open it.

## 4. Verification summary (Hermes state after the operation)

| Check | Command | Observed |
|---|---|---|
| MCP registry | `hermes mcp list` | `ayce-readonly … ✓ enabled`, `ayce-director … ✓ enabled` (unchanged) |
| Read-only connect | `hermes mcp test ayce-readonly` | `✓ Connected`, 10 tools (unchanged) |
| Director connect | `hermes mcp test ayce-director` | `✓ Connected (8763ms)`, `✓ Tools discovered: 3` (trigger_golden_path, execute_research_slice, propose_script_brief) |
| Skill surface | `hermes skills list …` | 51 builtin + 1 local, all enabled |
| Security/health | `hermes doctor` | `No active security advisories`; `No suspicious MCP stdio commands`; `Config version up to date (v46)`; only the pre-existing agent-browser npm advisory |
| Cron | `hermes cron list` | `19a6cce6796c [paused]` (`ayce-analytics-collect`) — unchanged |
| No unrelated Hermes resources | config diff | only `skills.trusted_project_dirs` added; no new MCP, no new cron job, no model/provider change, no registry skill installed |

## 5. Blocker / limitation boundary (unchanged, not worked around)

1. `0.4 → 0.15` task definitions still do not exist in any accessible artifact →
   the Commander's authoritative list is still required (ledger §5 blocker 1).
2. YouTube publishing remains blocked **account-side** (`403`,
   `authenticatedUserAccountSuspended`) — Commander/account action, not code
   (Phase 0.7B).
3. YouTube Analytics authenticated collection remains blocked (no eligible
   published record) — unchanged.
4. Model availability is external: the configured default `gemini-3.1-pro` is
   unreliable on the local proxy and the proxy rate-limits under load, so the
   verification sessions pin `-m gemini-3.6-flash` / `gemini-3.7-flash`. This is
   an environment characteristic, not a Hermes or AYCE defect.

## 6. Files changed by this task

| Path | Change |
|---|---|
| `.hermes/skills/ayce-project-knowledge/SKILL.md` | NEW — project knowledge skill (Hermes skill mechanism) |
| `mcp-ayce-director/h9_golden_compose_prompt.txt` | NEW — full composition prompt (kept for record; the verification used the split prompts below) |
| `mcp-ayce-director/h9b_director_execute_prompt.txt` | NEW — controlled execution + read-only observation prompt |
| `mcp-ayce-director/h9c_duplicate_prompt.txt` | NEW — idempotency (duplicate) prompt |
| `mcp-ayce-director/requests.json` | modified BY the run — one new ledger record (`req-20260928-h9-golden-compose-01`, `succeeded`) |
| `mcp-ayce-director/policy_consumption.jsonl` | modified BY the run — one new Stage 9 policy-consumption record |
| `docs/phase0/11_hermes_director_skill_verification.md` | NEW — this evidence record |
| `docs/phase0/00_phase0_ledger.md` | appended §7 (state update) |
| Hermes `%LOCALAPPDATA%\hermes\config.yaml` | `skills.trusted_project_dirs: [<repo>]` (Hermes-native trust; reversible with `hermes skills untrust`) |
| `src/ayce/**`, `tests/**`, `pyproject.toml`, `mcp-ayce-readonly/**`, `.env` | **untouched** |

Evidence artifacts on disk (not committed because `data/` is git-ignored):
`data/runs/run-20260929T064556Z-3b422d61fce8/` (20 files: 7 Golden Path artifacts
+ the rendered MP4 + `policy_consumption.json`). Hermes-side evidence lives in
`%LOCALAPPDATA%\hermes\state.db` (session transcripts + stored system prompts).

Rollback: `hermes skills untrust C:\Users\Administrator\AI-Content-Generation`
and delete `.hermes/skills/ayce-project-knowledge/`. The director run stays a
truthful on-disk run and its ledger record is the audit trail. No publish, no
OAuth, no paid service, no new dependency.

## 7. Acceptance

| # | Acceptance question | Answer |
|---|---|---|
| 1 | Capability registered in Hermes? | YES — skill `local`/`enabled`; both MCP servers `enabled` |
| 2 | Enabled/available? | YES — 52/52 skills enabled; `mcp test` connects to both servers |
| 3 | Can Hermes actually invoke it? | YES — real LLM-driven MCP tool calls executed in live sessions (transcripts above) |
| 4 | Expected real result? | YES — Golden Path `succeeded`, QA `PASS`, 7/7 stages, 8 artifacts; duplicate → `duplicate_request`, no second run; skill content retrieved verbatim |
| 5 | Matches Task 0 acceptance criteria? | YES — ledger §5 items 4 and 5 satisfied through Hermes' own surfaces |
| 6 | Hermes still reports the capability correctly afterwards? | YES — `mcp list` / `mcp test` / `skills list` / `doctor` re-checked after the operation |

**TASK 0 (ledger §5 items 4 & 5) = VERIFIED — Hermes runtime behaviour
demonstrated, not merely configured.**
