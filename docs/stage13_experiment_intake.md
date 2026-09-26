# Stage 13 — Evidence → Controlled Experiment Intake Boundary

Verdict: **PASS**

## A. Starting state

- Stages 1–12 present and green (full unit suite, integration suite,
  `mcp-ayce-readonly`, `mcp-ayce-director`).
- Stage 12 owns `src/ayce/policy/effectiveness.py` (derived, read-only
  Policy → Run → Publish → Video → Analytics projection, `pef-…`).
- Stage 7 owns experimentation (`src/ayce/learning/`: learning
  candidates `cnd-…`, content-addressed experiments `exp-…`, explicit
  `approve_experiment`, assignment, evaluation, curation).
- Stage 8 owns the policy lifecycle (`src/ayce/policy/`).
- Real repository state: 0 policies, 0 analytics observations,
  0 ledger rows; one real run
  `run-20260922T025706Z-358e19386030` with `policy_status=none`.
- Pre-existing modified files (before Stage 13, uncommitted):
  `.env.example`, `README.md`, `pyproject.toml`,
  `src/ayce/{artifacts,cli,config,pipeline,state}.py`,
  `tests/integration/test_cli.py`, `tests/unit/test_pipeline.py`, plus
  the untracked Stage 1–12 subtrees (`src/ayce/{analytics,learning,
  policy,research,publishing}`, `src/ayce/{lineage,captions,
  publish_package,tts_piper}.py`, `mcp-ayce-*`, docs, tests).

## B. Existing Stage 7 experiment API discovered (reused, not reimplemented)

- `ayce.learning.experiments.create_candidate(store, hypothesis, scope,
  evidence, counterexamples, lineage, now, dry_run)` → content-addressed
  `cnd-…` learning candidate, idempotent, status `candidate`, stored in
  the EXISTING learning store.
- `create_experiment / approve_experiment / start_experiment /
  assign_units / evaluate_experiment / curate_candidate` — the
  draft → approved → running → evaluated → curated operator pipeline.
- Stage 7 invariants reused verbatim: an experiment requires ≥ 2
  declared population units (`UNIT_ID_RE`); declared metrics are the
  normalized Stage 6 metrics + derived formulas; nothing starts itself.

## C. Existing Stage 8 policy API discovered (reused, untouched)

- `PolicyStore` (immutable versioned policies; `variant_preference_v1`
  rules declare the ONLY experiment comparison semantics used here:
  prefer `variant` over `comparator_variant` on `metric`).
- Lifecycle (`approve/promote/activate/rollback/retire`) — Stage 13
  never calls any of them; historical rows are read SQLite `mode=ro`.

## D. New capability

`src/ayce/policy/experiment_intake.py` — a DERIVED, READ-ONLY intake
projection (no persistence, no new store):

    Stage 12 effectiveness records (owner unchanged)
        ↓  deterministic eligibility (Stage 12 + Stage 7 contracts)
    ExperimentCandidate (cand-<sha256[:16]>; derived only)
        ↓  approve (EXPLICIT operator CLI; the ONLY mutation)
    EXISTING Stage 7 learning candidate (cnd-…) in the EXISTING store
        ↓  (Stage 7, unchanged)
    create_experiment → approve → assign → evaluate → curate

Fixes applied during this stage: bucket-deduplication bug in
`_build_candidates` (a record with N same-identity measurements was
counted N×N); `MIN_SAMPLE_SIZE` eligibility floor (reuses Stage 7's
≥ 2-unit contract; real-but-smaller populations are
`insufficient_evidence`, never negative evidence); `verify_intake` now
surfaces the Stage 12 integrity owner's corrupt-run/analytics-mismatch
reports instead of hiding them.

## E. Candidate schema (references only — no duplicated evidence)

`candidate_id` (`cand-<16 hex>`), `schema_version`, `derived: true`,
`status` (`eligible | insufficient_evidence | ineligible`),
`policy_id`, `policy_version`, `scope` (consumption scope),
`metric` `{name, source, window_start, window_end, window_timezone,
window_semantics}`, `source_effectiveness_evidence` (Stage 12 `pef-…`
ids), `run_ids` (contributing), `runs_not_contributing`
(`{run_id, reason}` using the Stage 12 taxonomy),
`measurement_count`, `sample_size`, `min_sample_size`,
`observed_summary` `{n, mean, median, min, max}`, `note`
(boundary statement). No timestamps, no values beyond the summary.

## F. Eligibility semantics (fail closed; nothing invented)

- invalid/corrupt evidence, `not_consumed`, ambiguous publication →
  record excluded with structured `ineligible_records` reasons.
- incomplete metric identity (windowed without bounds+timezone) →
  `ineligible`.
- `sample_size >= MIN_SAMPLE_SIZE (= 2, the Stage 7 ≥ 2-unit contract)`
  → `eligible`; smaller real populations → `insufficient_evidence`
  (distinct, never negative evidence); missing/unavailable metrics →
  `insufficient_evidence` with explicit exclusion reasons — NEVER zero,
  NEVER negative.

## G. Deterministic identity

`candidate_id = cand-<sha256(canonical{schema_version, policy_id,
policy_version, scope, metric identity, sorted effectiveness_ids,
sorted measurement_ids, sorted run_ids})[:16]>` — no timestamps, no
randomness. Verified: same evidence twice → same id; changed evidence
→ NEW id (the old id truthfully no longer derives); different metric /
policy version / window → different ids.

## H. Evidence lineage

Candidates reference Stage 12 `pef-…` records and measurement ids; the
approval materializes evidence ENTRIES of kind
`policy_effectiveness_measurement` whose `ref` is
`<effectiveness_id>/<measurement_id>` plus video id and observed value —
references, never duplicated analytics rows. Learning-store lineage
carries `{stage: 13, boundary: "experiment_intake", intake_candidate_id,
policy_id, policy_version, scope, metric_identity, sample_size,
approved_by}`.

## I. Approval boundary

`approve_candidate(runs_root, candidate_id, learning_store,
approved_by, now, …)`:
- ONLY mutating Stage 13 operation; writes ONLY through the EXISTING
  Stage 7 `ayce.learning.create_candidate` into the EXISTING learning
  store (idempotent: same candidate → same `cnd-…`, `duplicate: true`).
- fails closed: unknown candidate, `insufficient_evidence`,
  `ineligible`, or missing `approved_by` → `policy_intake_invalid`.
- does NOT create, approve or start an experiment
  (`store.list_experiments() == []` asserted). The Stage 7
  draft→approved boundary stays operator-owned.

## J. CLI

`ayce policy experiment-candidate {policy|metric|show|verify|approve}`,
all read directions `--json`-capable and read-only; `approve` requires
`--candidate-id` + `--approved-by` and is a SEPARATE explicit operation.
Errors follow the existing conventions (structured
`policy_intake_invalid`, rc 1). Real repo: all directions return
truthful empty/zero results.

## K. Read-only MCP

`mcp-ayce-readonly` exposes `get_experiment_candidates`
(candidate_id | policy_id | metric required; optional exact
policy_version/status filters; deterministic). NO experiment-start or
policy-mutation authority exists on either MCP server; `approve` is a
CLI-only operator boundary. Byte-read-only tests pass for the tool.

## L. Persistence decision

DERIVED projection (like Stage 11/12). No `candidate.sqlite3`. The only
persistence on the approval path is the EXISTING Stage 7 learning store
acting through its own owner function — required because Stage 7
experiments must reference a durable `cnd-…` candidate.

## M. Fixture matrix (tests/unit/test_policy_experiment_intake.py)

A → v1 eligible (n=4 lifetime views; n=2 windowed views); B →
contributes to the windowed eligible candidate; C → v2 with no
analytics → zero v2 candidates (and, with added real evidence, v2
derives its own distinct identities); D → unpublished → excluded;
E → published/analytics-not-collected → excluded; F → no policy →
never appears in any candidate; G → eligible and REMAINS interpretable
after v1 retirement. Zero-likes (n=1) → `insufficient_evidence`;
missing comments / unavailable subscribersGained →
`insufficient_evidence` with explicit exclusion reasons.

## N. Failure injection (all fail closed)

Corrupt consumption artifact → run excluded by the Stage 12 integrity
owner and surfaced by `verify_intake` (`runs_corrupt`); analytics
lineage run-mismatch → record invalid, evidence refused; incomplete
metric identity → `ineligible`; unknown policy/invalid ids → structured
`policy_intake_invalid`; unknown/insufficient candidates on approval →
refused, nothing written; changed evidence → old id no longer derives.

## O. Real repository evidence

`run-20260922T025706Z-358e19386030`:
`policy_status=none, missing_data_status=not_consumed,
publish_status=unpublished, analytics_status=not_applicable` → no
candidate fabricated. `experiment-candidate metric --metric views` →
`candidate_count: 0`; `verify` → `runs_scanned: 15, runs_corrupt: 0,
deterministic: true, consistent: true`; approval of an unknown
candidate → rc 1 `policy_intake_invalid` (fail closed).

## P. Safety/hash proof

SHA-256 of `policy.sqlite3`, `learning.sqlite3`, `analytics.sqlite3`,
`ledger.sqlite3` and the real run's `state.json`/`artifacts.json`/
`policy_consumption.json` captured BEFORE and AFTER the full read +
refusal exercise: **byte-identical**. Unit test
`test_read_directions_never_mutate_any_canonical_store` proves the same
for every read direction on the fixture graph; approval tests prove the
ONLY mutation is the Stage 7 learning store via its own owner.

## Q. Full regression

- `tests/unit` — pass (1 pre-existing skip), exit 0
- `tests/integration` — 6 passed (Golden Path included)
- `mcp-ayce-readonly` — 46 passed + 1 pre-existing skip
- `mcp-ayce-director` — 121 passed
- Stage 7/8/10/11/12 test files all green inside `tests/unit`.

## R. Golden Path regression

`tests/integration` (Golden Path CLI end-to-end) — 6 passed, exit 0.

## S. Hermes/director safety

`mcp-ayce-director` gained no tools (121 tests unchanged/passing);
`mcp-ayce-readonly` gained only the read-only
`get_experiment_candidates`; no experiment-start authority exists
anywhere; candidate generation performs no writes and never calls the
director or any Stage 8 lifecycle mutation.

## T. Git status

Nothing committed (working tree was already dirty before Stage 13).
Stage 13 changes: NEW `src/ayce/policy/experiment_intake.py`,
NEW `tests/unit/test_policy_experiment_intake.py`, NEW
`docs/stage13_experiment_intake.md`; MODIFIED `src/ayce/policy/
__init__.py` (exports), `src/ayce/policy/errors.py`
(`policy_intake_invalid`), `src/ayce/cli.py` (Stage 13 subcommand),
`mcp-ayce-readonly/server.py` + `test_server.py` (Stage 13 tool),
`src/ayce/policy/effectiveness.py` (additive `scope` field on records).
Pre-existing modified/untracked files listed in §A were not touched.

## U. Limitations

- The eligible-candidate population floor (2) is structural (Stage 7
  unit contract), not statistical; no power/significance reasoning
  exists here by design.
- Candidates are per (policy, version, scope, metric identity); the
  only rule kind in the current schema is `variant_preference_v1`, so
  the intake comparison semantics always come from the consumed policy.
- Approval materializes the candidate but deliberately does NOT draft
  the Stage 7 experiment; the operator composes population/seed/criterion
  in Stage 7 (`create_experiment`) — keeping Stage 13 free of experiment
  definition semantics.
- MCP exposes candidates read-only; approval is intentionally CLI-only.

## V. Next stage

Not implemented. Natural continuation (future stage): operator-driven
composition of the Stage 7 experiment definition FROM an approved
intake candidate (population/seed/criterion), still fully
operator-controlled.


