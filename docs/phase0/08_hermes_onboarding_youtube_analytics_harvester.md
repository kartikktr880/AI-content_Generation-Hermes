# TASK 0.7 — HERMES SKILL ONBOARDING (youtube-analytics-harvester) — STOPPED

**Scope.** Onboard ONE research-selected skill (`youtube-analytics-harvester`)
through Hermes' native skill mechanism. Execution halted at the task's own
stop condition #9: the selected artifact cannot be located, and the cited
research evidence does not describe it. NOTHING was installed; nothing was
modified.

## 1. Selected candidate (as given)

| Field | Value |
|---|---|
| SKILL | `youtube-analytics-harvester` |
| CLAIMED SOURCE EVIDENCE | "Stage 12 Analytics and Learning Architecture research report" |
| CLAIMED COMPOSITION | `Hermes cron → youtube-analytics-harvester → YouTube Analytics/Data APIs → analytics adapter → SQLite WAL` |

## 2. Inspection performed (all read-only; no install, no force, no config change)

| # | Check | Exact command / location | Result |
|---|---|---|---|
| 1 | Repo-wide reference | regex search `youtube-analytics-harvester`, `analytics-harvester` over the workspace (195 files, incl. `docs/`, `Research_Agent/`) | **0 matches** |
| 2 | Case-insensitive sweep | `Select-String -Pattern 'harvester'` over every repo file (excluding `.venv`, `.git`, caches) | no YouTube-related occurrence anywhere |
| 3 | Already installed/bundled? | `hermes skills list --source all` | `0 hub-installed, 51 builtin, 0 local — 51 enabled, 0 disabled`; only YouTube-adjacent builtin is `youtube-content` (media category — a different capability). **Candidate NOT present.** |
| 4 | Cited Stage 12 report | `docs/stage12_policy_effectiveness.md` | contains no skill, harvester, cron, or Analytics-API-onboarding content (only `youtube_video_id`/`destination` schema fields) |
| 5 | Actual Stage 12 research material | `Research_Agent\Reports 4A-4L\YouTube Analytics Closed-Loop Architecture` | **0 occurrences of "harvester"**; the only "skill" mention is a generic note about a Curator service updating Hermes procedural skills. The report NEVER names `youtube-analytics-harvester`. |
| 6 | Registry — exact name | `hermes skills search youtube-analytics-harvester` | **"No skills found matching your query."** |
| 7 | Registry — broad | `hermes skills search "youtube analytics"` (6 hits) and `hermes skills search harvester` (13 hits) | near-name matches exist (`youtube-analytics-api`, `youtube-analytics`, `youtube-analytics-cli`, `youtube-reporting`, `youtube-performance`, `tubelab-api`) but ALL are **ClawHub `community` trust**, different names, and none is `youtube-analytics-harvester`. No "harvester"-named YouTube skill exists in any source. |
| 8 | Existing native capability | `src\ayce\analytics\` | collector.py (35 KB; YouTube **Analytics API v2** `reports.query` + **Data API v3** `videos.list`, error taxonomy incl. rate-limit/HTTP handling), transport.py (official endpoints, token never in params), store.py (`PRAGMA journal_mode=WAL`, line 116), errors.py — i.e. the exact chain the task attributes to a skill **already exists as verified project code** (Stage 6/12 implementation), reached from Hermes through the already-onboarded `ayce-director`/`ayce-readonly` MCP surfaces rather than a skill. |

## 3. Decision (per the task's own stop condition #9)

> "If the skill source cannot be located or the research report does not
> provide enough information to safely identify the installable artifact,
> STOP instead of guessing."

- The cited research report does NOT exist as described and does not name the
  skill anywhere.
- No artifact named `youtube-analytics-harvester` exists locally, bundled, or
  in any Hermes-connected registry.
- The only superficially similar registry entries are **community-trust**
  ClawHub skills with different names/purposes — installing one of them would
  (a) violate the ONE-selected-candidate hard rule, (b) violate "use a
  trusted/appropriate source", and (c) be a guess, exactly what the task
  forbids.

Therefore: **STOP — no onboarding performed.**

## 4. State separation (as required)

| State | Result |
|---|---|
| Filesystem presence | skill does NOT exist anywhere (repo, bundled, hub) |
| Hermes registration/discovery | none — `hermes skills list` unchanged (51 builtin, 0 hub, 0 local) |
| Runtime loading | none — nothing to load |
| Dashboard visibility | no change to verify: the CLI listing is authoritative for the same underlying state the dashboard renders; since nothing was onboarded, the dashboard's skills surface shows no new skill. No dashboard inspection was performed and none is meaningful for a non-existent artifact. |
| Authenticated functional execution | NOT APPLICABLE — no artifact exists to execute; no credentials were fabricated |

## 5. What WAS changed

Nothing functional. Only this evidence document. Hermes config, installed
skills (51 builtin), both MCPs (`ayce-readonly`, `ayce-director`), and the
Hermes installation are untouched (re-verified after the searches).

## 6. Rollback

Not applicable — nothing was installed. (For reference, the Phase 0.6-proven
removal mechanism for any future hub skill is `hermes skills uninstall
<name>`; project-local skills are removed by deleting the directory and
`hermes skills untrust <root>`.)

## 7. What would unblock this task (Commander decision required)

1. Provide the ACTUAL research artifact that selects the skill (path/quote), or
   re-select a concrete installable candidate with a real identifier/URL.
2. If the intent is the composition chain itself: note that it ALREADY exists
   natively (`src/ayce/analytics/` collector → transport → SQLite WAL store),
   so the honest next step may be wiring Hermes **cron** to the existing AYCE
   analytics path (via the already-onboarded director/readonly MCPs) instead
   of installing a redundant skill — per the project's REUSE-BEFORE-CREATE
   rule.
3. If a registry skill is genuinely wanted: the ClawHub community candidates
   (`youtube-analytics-api` etc.) would each need source inspection
   (`hermes skills inspect`), trust evaluation, and explicit Commander
   approval BEFORE any `hermes skills install` (community trust, network
   OAuth handling — not approvable silently).

## 8. Verification summary

| Required item | Result |
|---|---|
| Selected candidate inspected | yes — and proven non-existent as specified |
| Exact source/identifier used | none — no artifact could be safely identified |
| Already present? | no (51 builtin skills; candidate not among them) |
| Hermes onboarding command | NOT executed (stop condition) |
| Resulting skill state | unchanged: 0 hub-installed, 51 builtin, 0 local |
| Hermes verification output | `hermes skills search` → "No skills found matching your query." |
| Dashboard verification | nothing to verify (no change) |
| Runtime loading evidence | none — no artifact |
| Authentication limitation | not reached (no artifact); no credentials fabricated |
| Files/config changed | only this evidence document |
| Rollback | n/a — nothing installed |
| Final state | BLOCKED (no install, no side effects) |

## YOUTUBE_ANALYTICS_HARVESTER = BLOCKED