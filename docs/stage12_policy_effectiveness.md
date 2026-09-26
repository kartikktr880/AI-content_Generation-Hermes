# Stage 12 — Policy Effectiveness Feedback (READ-ONLY evidence)

Status: **PASS** (2026-09-22). Final report per the Stage 12 specification.

## A. Implementation Summary

Stage 12 closes the *measurement feedback loop* while deliberately leaving the
policy decision loop operator-controlled. It is a deterministic, in-memory,
READ-ONLY projection joining the four canonical stores — no new database:

- `src/ayce/policy/effectiveness.py` — the projection core:
  `effectiveness_for_run`, `effectiveness_for_policy`,
  `metric_effectiveness_summary`, `verify_effectiveness`.
- `src/ayce/cli.py` — `ayce policy effectiveness run|policy|metric|verify`
  (all observation-only, `--json` supported, no mutation command).
- `mcp-ayce-readonly/server.py` — one generic read-only tool
  `get_policy_effectiveness` (exactly ONE of `run_id`/`policy_id` per call;
  optional `policy_version`, `metric`).
- `tests/unit/test_policy_effectiveness.py` — 25 tests over the §28–§31
  fixture matrix.
- `mcp-ayce-readonly/test_server.py` — Stage 18 section: real stdio MCP tests
  (run/policy directions, one-identity guard, structured errors, byte-level
  read-only proof).

## B. Source-of-Truth Decisions

| Capability | Owner (reused, not reimplemented) |
|---|---|
| Policy → Runs | `ayce.policy.lineage` (Stage 11 scan) |
| Consumption verification | `ayce.policy.observability` (`verify_consumption_history`) |
| Run → Video | `ayce.publishing.ledger` (Stage 5, SQLite `mode=ro`) |
| Video → Measurements | `ayce.analytics` store (Stage 6, SQLite `mode=ro`) |
| Current policy state (metadata only) | ProductionPolicy store (SQLite `mode=ro`) |

`effectiveness = f(policy artifacts, publish ledger, analytics store)` —
derived on every query (§21 preferred path at current scale). Nothing is
persisted, so no persistence concurrency mechanism exists or is needed (§32):
same evidence → same projection, rebuild ≡ recomputation.

## C. Evidence Graph

```
Policy Consumption (Stage 10 artifact, hash-verified by Stage 11 scan)
   → run_id                     [Stage 11: policy → runs]
   → Publish Ledger row         [Stage 5, authoritative]
   → youtube_video_id/destination
   → Analytics raw_observations [Stage 6, keyed by video id]
   → normalized_measurements    [joined on observation_id]
   → effectiveness record (pef-…) + descriptive aggregation
```

Every edge is explicit. Video ids are resolved ONLY through the publish
ledger — never filenames, titles, URLs, timestamps or fuzzy matching.

## D. Policy → Run Correlation

Reuses `_scan_runs` (Stage 11). The policy direction selects the exact
verified consumption events for `(policy_id[, policy_version])` from the
policy index and rebuilds records only for those events. No-policy events
(`policy_status none`) produce run-direction records with
`missing_data_status = not_consumed` but never enter any policy aggregation.

## E. Run → Publish Correlation

`_publish_resolution` reads the run's ledger rows (`mode=ro`): exactly one
distinct video id → `published` (one record per video/destination target);
none → `unpublished` (not an analytics failure); a missing ledger store →
`unpublished` with `ledger_missing = true`; multiple distinct video ids →
fail-closed `ambiguous_publication` invalid record.

## F. Publish → Analytics Correlation

`_analytics_resolution` reads the Stage 6 store for the ledger's video id
only. Store absent or video never observed → `analytics_status =
unavailable` (`analytics_not_collected`). Each measurement carries
`observation_id`, `source`, `window_start/end`, `window_timezone` (from its
observation), `observed_at`. `_analytics_crosscheck_problems` verifies
observation lineage against the publish resolution (video id, run id,
destination) — disagreements (or a corrupt lineage payload) make the record
invalid; nothing is guessed (§19).

## G. Effectiveness Schema

`schema_version`, `effectiveness_id` (valid records only), `policy_id`,
`policy_version`, `policy_status`, `policy_state` (current-state metadata),
`consumption_id`, `decision_id`, `run_id`, `publish_status`, `publish`
{youtube_video_id, destination, package_id, package_seal},
`ledger_statuses`, `ledger_missing`, `analytics_status`, `measurements[]`,
`lineage` {run_id, script_id, research_id, objective_id},
`missing_data_status`, `evidence_status`, `invalid_reasons`.

## H. Metric Semantics

Metric identity = `(metric, source, window_start, window_end,
window_timezone)`. Lifetime Data-API (`lifetime_cumulative`) and windowed
Analytics-API (`windowed`) populations are never merged; DATA_API_V3 and
ANALYTICS_API_V2 never merge. Availability values are preserved verbatim
(`present` / `zero` / `missing` / `unavailable`); zero is real data.

## I. Missing/Unavailable Data Semantics

Distinct taxonomy (§13), never collapsed into zero:
`not_consumed` / `not_published` / `analytics_not_collected` /
`metric_unavailable` / `metric_missing` / `metric_present`. Record-level
status = the furthest point the evidence chain reached. Exclusions are
reported per outcome group (`excluded_measurements`, `exclusion_counts`).

## J. Aggregation

Counts: `consumed_runs`, `published_runs`, `unpublished_runs`,
`analytics_available_runs`, `analytics_unavailable_runs`,
`metric_present_count`, `metric_missing_count` (+ per-status split).
Descriptive summaries per metric identity: n/mean/median/min/max with
`sample_size`, source, window, availability, explicit exclusions.
Aggregates carry `evidence_status` (`descriptive_evidence` |
`insufficient_evidence`) and the evidence-boundary note. NO winner, NO
ranking, NO score, NO promotion signal.

## K. Historical Policy Lifecycle

Records describe CONSUMPTION. The v1 → v2 → rollback → run G → retire v1
exercise keeps `v1 → {A,B,E,G}`, `v2 → {C,D}` queryable after retirement;
`policy_state` (`retired`/`superseded`) is current-state metadata only.

## L. CLI

```
ayce policy effectiveness run     --run-id X [--json]
ayce policy effectiveness policy  --policy-id P [--policy-version N]
                                  [--metric M] [--video-id V] [--json]
ayce policy effectiveness metric  --metric M [--policy-id P]
                                  [--policy-version N] [--source S]
                                  [--window-start WS] [--window-end WE]
ayce policy effectiveness verify  [--json]
```

Read-only; errors are structured (`policy_effectiveness_invalid`, exit 1).

## M. Read-only MCP

`get_policy_effectiveness(run_id?, policy_id?, policy_version?, metric?)` in
`mcp-ayce-readonly` — exactly one primary identity per call; all stores are
opened SQLite `mode=ro`; no SQL, no write path, no raw analytics DB exposed.
Hermes receives NOTHING new: no effectiveness surface was added to
`mcp-ayce-director`, and Stage 9's active-policy boundary is unchanged (§24).

## N. Integrity / Failure Injection

Covered by tests: missing consumption / unknown run / unknown policy /
missing ledger / missing video id / missing analytics / missing metric /
unavailable metric / incompatible windows / analytics lineage mismatch
(run + video + destination) / corrupt consumption artifact / corrupt
analytics observation lineage / ambiguous publication. All fail closed with
structured reasons; invalid records earn no `effectiveness_id`.

## O. Safety Proof

Real repository: sha256 of `policy.sqlite3`, `learning.sqlite3`,
`analytics.sqlite3`, `publishing/ledger.sqlite3` captured before and after
all four CLI directions — **byte-identical**. Unit tests additionally
assert digest equality for policy/ledger/analytics stores plus run
consumption artifact and artifacts manifest, and a source scan proves
`effectiveness.py` contains no write capability.

## P. Test Results

- `tests/unit` (incl. 25 new Stage 12 tests): all pass.
- `tests/integration` (incl. Golden Path CLI test): all pass.
- `mcp-ayce-readonly/test_server.py` (incl. Stage 12 MCP section): all pass.

## Q. Real Repository Evidence

`run-20260922T025706Z-358e19386030`: `evidence_status=verified`, one valid
record with `policy_status=none` (`missing_data_status=not_consumed`),
`publish_status=unpublished` (ledger has no row), `analytics_status =
not_applicable`. `verify`: 15 runs scanned, 1 verified (the Stage 9/10
event), 14 missing evidence, 0 corrupt, `consistent=true`. Zero real
analytics observations exist; nothing was fabricated.

## R. Controlled Fixture Evidence

Full §28 graph (runs A–G, two policy versions, rollback + retirement) built
through the real Stage 5 ledger API and real Stage 6 store API; §29 metric
coverage (views/likes/comments, present/zero/missing/unavailable, lifetime
vs windowed, both sources).

## S. Golden Path Regression

`tests/integration/test_cli.py::test_run_command_golden_path` — green in the
regression run; no Stage 1–11 behavior changed.

## T. Git Status

Stage 12 changes (this stage): `src/ayce/policy/effectiveness.py`
(completed: analytics resolution + cross-checks, read-only row factory,
run-direction return path, identity-on-valid-evidence only),
`tests/unit/test_policy_effectiveness.py` (new),
`mcp-ayce-readonly/test_server.py` Stage 12 section, `docs/this file`.
Pre-existing uncommitted work (Stages 1–11, research, publishing,
analytics, learning, MCP servers, etc.) was already in the working tree and
is untouched.

## U. Known Limitations

- Descriptive statistics only; no variance, no effect estimates, no
  p-values (Stage 7 owns experimentation).
- The projection rescans all runs per query — fine at tens of runs; a
  materialized cache would need justification (§21) and none is warranted.
- `metric_effectiveness_summary` groups by exact metric identity; metrics
  reported under inconsistent names by future sources will form separate
  groups (truthful, not merged).
- The real repository has zero analytics observations, so real-data
  aggregation paths are exercised by fixtures only.

## V. Next Stage

Not implemented (per instruction). Stage 12 ends at observed evidence; any
operator-facing policy-decision support (e.g. controlled experiments over
policy versions with causal designs) would be a new, explicitly specified
stage.
