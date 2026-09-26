"""Stage 14 — Controlled experiment composition boundary.

The SMALLEST boundary between an approved Stage 13 Experiment Intake
Candidate and the EXISTING Stage 7 experimentation system::

    Stage 12 Effectiveness → Stage 13 Intake Candidate
        ↓  EXPLICIT candidate approval (Stage 13; learning store, cnd-…)
    ExperimentDefinitionProposal (expdef-…; THIS module; DERIVED)
        ↓  EXPLICIT operator approval (--seed and review; the ONLY
        ↓  mutation; through the EXISTING Stage 7 owner)
    EXISTING Stage 7 create_experiment → DRAFT experiment
        ↓  (Stage 7, unchanged, operator-driven)
    approve_experiment → start_experiment → assign_units
        → evaluate_experiment → curate_candidate
        ↓  (Stage 8, unchanged, operator-driven)
    policy lifecycle

Hard invariants:

- STAGE 7 REMAINS THE SOLE OWNER (§4): no experiment engine, no runner,
  no assignment engine, no evaluator, no knowledge store and no
  experiment schema is created or duplicated here. Approval calls the
  EXISTING ``ayce.learning.create_experiment`` and nothing else; the
  learning store is never written directly.
- PROPOSAL GENERATION HAS ZERO MUTATIONS (§3/§9/§19): deriving a
  proposal touches NOTHING — all reads open the canonical stores SQLite
  ``mode=ro``. Only explicit approval persists, and only the Stage 7
  learning store changes (through the Stage 7 owner).
- NOTHING RUNS AUTOMATICALLY (§3/§22): a proposal is never an
  experiment; approval creates a DRAFT experiment only — it never
  starts one, never assigns units, never evaluates, and never touches
  the Stage 8 policy lifecycle (no activate/promote/rollback/retire).
- NO INVENTED PARAMETERS (§6): population units are the OBSERVED
  published videos of the candidate's contributing evidence; the
  comparison (control/treatment/direction) comes ONLY from the consumed
  ProductionPolicy's own ``variant_preference_v1`` rule (or an explicit
  operator flag); the minimum sample sizes reuse the existing Stage
  7/13 contract (>= 2); ``min_effect``/``max_p``/``holdout`` are NEVER
  defaulted here (Stage 7's own explicit defaults apply, unchanged).
  The assignment seed is NEVER invented — it is a REQUIRED operator
  argument at approval time. Anything unresolvable leaves the proposal
  ``needs_operator_configuration`` and approval FAILS CLOSED.
- DETERMINISTIC IDENTITY (§7): ``expdef-<sha256(canonical proposal
  inputs)[:16]>`` — no timestamps, no randomness; the same candidate
  with the same resolved definition ALWAYS yields the same proposal id,
  and any meaningfully different definition yields a different one.
- EVIDENCE LINEAGE (§8): proposals reference (never copy) the Stage 13
  candidate, the Stage 12 ``pef-…`` evidence and the consumed policy —
  the chain proposal → candidate → effectiveness → consumption → run →
  publish → observation stays canonical end to end.
- FAIL CLOSED (§12/§18): unknown/insufficient/ineligible/corrupt
  candidates, unknown proposals, missing Stage 7 intake approval,
  unresolved configuration and Stage 7 definition failures ALL refuse
  with structured ``policy_composition_invalid`` errors — a failed
  approval never partially creates or starts an experiment.
- HERMES BOUNDARY (§22): the read-only MCP tool inspects proposals
  only; no create/approve/start/assign authority exists on any MCP
  server.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from ..learning.derived import FORMULA_CATALOG
from ..learning.errors import LearningError
from ..learning.experiments import _NORMALIZED_METRICS
from .errors import PolicyError
from .experiment_intake import (
    CANDIDATE_ID_RE,
    MIN_SAMPLE_SIZE,
    _MEASUREMENT_AVAILABLE,
    _all_candidates,
    _identity_of,
    _records_for_policy,
    show_candidate as show_intake_candidate,
)

__all__ = [
    "COMPOSITION_SCHEMA_VERSION",
    "PROPOSAL_STATUS_VALUES",
    "PROPOSAL_ID_RE",
    "COMPOSITION_NOTE",
    "MIN_POPULATION_UNITS",
    "propose_for_candidate",
    "show_proposal",
    "proposals_for_policy",
    "verify_composition",
    "approve_proposal",
]

#: Definition-proposal schema version.
COMPOSITION_SCHEMA_VERSION = 1

#: Proposal statuses (§5): ``ready`` = every field Stage 7 requires is
#: resolved from evidence/policy/operator input; anything unresolvable
#: is ``needs_operator_configuration`` — never silently invented.
PROPOSAL_STATUS_VALUES = ("ready", "needs_operator_configuration")

#: Proposal id grammar (content-addressed, §7).
PROPOSAL_ID_RE = re.compile(r"^expdef-[0-9a-f]{16}$")

#: The boundary statement carried on every proposal/response (§3/§6/§22).
COMPOSITION_NOTE = (
    "experiment DEFINITION PROPOSAL only: a deterministic, "
    "operator-reviewable draft of the existing Stage 7 experiment "
    "inputs for an APPROVED intake candidate; this is not an experiment, "
    "not a start, not an assignment, not an evaluation, not a causal "
    "claim and not a policy recommendation; the assignment seed is "
    "NEVER invented — it is a required operator argument at approval"
)

#: Minimum population size — the EXISTING Stage 7 contract (an
#: experiment requires >= 2 declared units), reused verbatim.
MIN_POPULATION_UNITS = 2

#: Operator-resolvable fields: the ONLY unresolved fields approval may
#: accept explicit operator flags for.
_OPERATOR_RESOLVABLE = ("control_variant", "treatment_variant")


def _canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _paths(runs_root, *, policy_db_path, ledger_path, analytics_db_path):
    return {
        "runs_root": Path(runs_root),
        "policy_db_path": (Path(policy_db_path)
                           if policy_db_path is not None else None),
        "ledger_path": (Path(ledger_path)
                        if ledger_path is not None else None),
        "analytics_db_path": (Path(analytics_db_path)
                              if analytics_db_path is not None else None),
    }


def _validate_proposal_id(proposal_id) -> str:
    if not isinstance(proposal_id, str) or \
            not PROPOSAL_ID_RE.fullmatch(proposal_id.strip()):
        raise PolicyError("policy_composition_invalid",
                          f"invalid proposal_id: {proposal_id!r} "
                          "(expected expdef-<16 hex>)")
    return proposal_id.strip()


def _validate_candidate_id(candidate_id) -> str:
    if not isinstance(candidate_id, str) or \
            not CANDIDATE_ID_RE.fullmatch(candidate_id.strip()):
        raise PolicyError("policy_composition_invalid",
                          f"invalid candidate_id: {candidate_id!r} "
                          "(expected cand-<16 hex>)")
    return candidate_id.strip()


def _validate_seed(seed) -> str:
    if not isinstance(seed, str) or not seed.strip():
        raise PolicyError(
            "policy_composition_invalid",
            "approval requires an explicit --seed assignment seed "
            "(Stage 14 never invents experiment parameters)")
    return seed.strip()


def _validate_approved_by(approved_by) -> str:
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise PolicyError("policy_composition_invalid",
                          "approval requires an explicit approved_by "
                          "operator identity (auditable boundary)")
    return approved_by.strip()


def _validate_variants(control_variant, treatment_variant) -> tuple:
    """Operator variant overrides: both or neither (a one-sided rename
    of a policy-declared comparison would silently redefine it)."""
    control = (control_variant.strip() if isinstance(control_variant, str)
               else "") or None
    treatment = (treatment_variant.strip()
                 if isinstance(treatment_variant, str) else "") or None
    if (control is None) != (treatment is None):
        raise PolicyError(
            "policy_composition_invalid",
            "control_variant and treatment_variant must be supplied "
            "TOGETHER (a one-sided comparison is not a valid experiment)")
    if control is not None and control == treatment:
        raise PolicyError("policy_composition_invalid",
                          "control_variant and treatment_variant must "
                          "differ (Stage 7 contract)")
    return control, treatment


def _validate_min_effect(min_effect):
    if min_effect is None:
        return None
    if isinstance(min_effect, bool) or \
            not isinstance(min_effect, (int, float)):
        raise PolicyError("policy_composition_invalid",
                          f"invalid min_effect: {min_effect!r} "
                          "(numeric or omitted)")
    return float(min_effect)


def _load_policy_rules(policy_db_path, policy_id, policy_version):
    """Read the consumed policy's immutable rules READ-ONLY (mode=ro)
    from the Stage 8 store — the ONLY owner of comparison semantics.
    Returns (rules list, policy_state) — never fabricated."""
    path = Path(policy_db_path) if policy_db_path is not None else None
    if path is None or not path.is_file():
        return None, "store_missing"
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10.0)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise PolicyError("policy_composition_invalid",
                          f"the policy store could not be opened: {exc}")
    try:
        row = connection.execute(
            "SELECT rules, status FROM policies"
            " WHERE policy_id = ? AND policy_version = ?",
            (policy_id, policy_version),
        ).fetchone()
        if row is None:
            return None, "missing"
        try:
            rules = json.loads(row["rules"])
        except (json.JSONDecodeError, TypeError):
            return None, "corrupt"
        return (rules if isinstance(rules, list) else None), row["status"]
    except sqlite3.Error as exc:
        raise PolicyError("policy_composition_invalid",
                          f"the policy store could not be read: {exc}")
    finally:
        connection.close()


def _rule_comparison(rules, metric: str) -> dict | None:
    """The variant boundary the consumed policy itself declares for this
    metric (§6): prefer ``variant`` over ``comparator_variant`` →
    Stage 7 treatment/control with direction
    ``treatment_greater_than_control``. Deterministic pick by
    (priority, rule_id) when several rules match."""
    if not isinstance(rules, list):
        return None
    matches = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("kind") != "variant_preference_v1" \
                or rule.get("action") != "prefer" \
                or rule.get("metric") != metric:
            continue
        variant, comparator = rule.get("variant"), \
            rule.get("comparator_variant")
        if not isinstance(variant, str) or not variant.strip() \
                or not isinstance(comparator, str) or not comparator.strip() \
                or variant == comparator:
            continue
        matches.append((rule.get("priority", 0),
                        str(rule.get("rule_id", "")), variant, comparator))
    if not matches:
        return None
    matches.sort()
    _priority, rule_id, variant, comparator = matches[0]
    return {"treatment_variant": variant, "control_variant": comparator,
            "direction": "treatment_greater_than_control",
            "source": "policy_rule", "source_rule_id": rule_id}


def _population_units(paths: dict, candidate: dict) -> list[str]:
    """The OBSERVED published videos behind the candidate's contributing
    evidence (§6: references, never invented) — exactly the unit type
    Stage 7 declares (the published video, §12 of Stage 7)."""
    metric = candidate["metric"]
    identity = (metric["name"], metric["source"], metric["window_start"],
                metric["window_end"], metric["window_timezone"])
    units = []
    for record in _records_for_policy(
            paths, candidate["policy_id"], candidate["policy_version"],
            metric["name"]):
        if record.get("run_id") not in candidate["run_ids"] \
                or record.get("evidence_status") != "valid" \
                or record.get("publish_status") != "published":
            continue
        usable = any(
            _identity_of(m) == identity
            and m.get("availability") in _MEASUREMENT_AVAILABLE
            and m.get("value") is not None
            for m in record.get("measurements", []))
        if not usable:
            continue
        video_id = (record.get("publish") or {}).get("youtube_video_id")
        if video_id and video_id not in units:
            units.append(video_id)
    return sorted(units)


def _metric_declared(metric_name: str) -> bool:
    """Stage 7's OWN declared-metric contract, reused verbatim: a
    proposed metric must be a normalized Stage 6 metric or a derived
    formula, or Stage 7 will refuse the definition."""
    return metric_name in _NORMALIZED_METRICS or metric_name in \
        FORMULA_CATALOG


def _proposal_for(candidate: dict, paths: dict, *, control_variant,
                  treatment_variant, min_effect) -> dict:
    """Deterministically derive ONE definition proposal from an ELIGIBLE
    Stage 13 candidate (§5). Fields Stage 7 requires are resolved from
    evidence/policy/operator input ONLY — anything else stays explicitly
    ``needs_operator_configuration`` (§6)."""
    metric = candidate["metric"]
    metric_name = metric["name"]
    declared = _metric_declared(metric_name)

    rules, policy_state = _load_policy_rules(
        paths["policy_db_path"], candidate["policy_id"],
        candidate["policy_version"])
    rule_comparison = _rule_comparison(rules, metric_name)
    operator_comparison = ({"control_variant": control_variant,
                            "treatment_variant": treatment_variant,
                            "direction": "treatment_greater_than_control",
                            "source": "operator", "source_rule_id": None}
                           if control_variant is not None else None)
    comparison = operator_comparison or rule_comparison

    units = _population_units(paths, candidate)
    population_resolved = len(units) >= MIN_POPULATION_UNITS

    unresolved = []
    if not declared:
        unresolved.append("metric_declared_in_stage7")
    if comparison is None:
        unresolved.extend(("control_variant", "treatment_variant"))
    if not population_resolved:
        unresolved.append("population_units")

    ready = not unresolved
    success_criterion = {
        "min_control_n": MIN_SAMPLE_SIZE,   # the Stage 7/13 >= 2 contract
        "min_treatment_n": MIN_SAMPLE_SIZE,
        **({"min_effect": min_effect} if min_effect is not None else {}),
    }
    definition_preview = {
        "metric": metric_name,
        "control_variant": (comparison["control_variant"]
                            if comparison else None),
        "treatment_variant": (comparison["treatment_variant"]
                              if comparison else None),
        "population": {"kind": "explicit", "units": units},
        "assignment": {"seed": None},  # NEVER invented (§6)
        "success_criterion": success_criterion,
        "evaluation_window": None,     # Stage 7's own explicit contract
        "holdout": None,               # Stage 7's own explicit contract
    }
    basis = {
        "schema_version": COMPOSITION_SCHEMA_VERSION,
        "candidate_id": candidate["candidate_id"],
        "metric": metric,
        "control_variant": definition_preview["control_variant"],
        "treatment_variant": definition_preview["treatment_variant"],
        "direction": (comparison["direction"] if comparison else None),
        "population_units": units,
        "success_criterion": success_criterion,
        "evaluation_window": None,
        "holdout": None,
    }
    digest = hashlib.sha256(_canonical(basis).encode("utf-8")).hexdigest()
    return {
        "proposal_id": "expdef-" + digest[:16],
        "schema_version": COMPOSITION_SCHEMA_VERSION,
        "derived": True,  # recomputed from approved evidence; never stored
        "status": "ready" if ready else "needs_operator_configuration",
        "candidate_id": candidate["candidate_id"],
        "candidate_status": candidate["status"],
        "policy_id": candidate["policy_id"],
        "policy_version": candidate["policy_version"],
        "policy_state": policy_state,   # CURRENT status metadata only
        "scope": candidate["scope"],
        "metric": metric,
        "metric_declared_in_stage7": declared,
        "comparison": comparison or {"resolved": False,
                                     "control_variant": None,
                                     "treatment_variant": None,
                                     "direction": None},
        "population": {"kind": "explicit_published_videos",
                       "units": units, "unit_count": len(units),
                       "resolved": population_resolved,
                       "min_units": MIN_POPULATION_UNITS},
        "assignment": {"seed": None, "resolved": False,
                       "note": "the assignment seed is NEVER invented — "
                               "it is a required operator argument at "
                               "approval (--seed)"},
        "success_criterion": success_criterion,
        "evaluation_window": None,
        "holdout": None,
        "observed_evidence": {
            "source_effectiveness_evidence":
                candidate["source_effectiveness_evidence"],
            "sample_size": candidate["sample_size"],
            "observed_summary": candidate["observed_summary"],
            "run_ids": candidate["run_ids"],
            "runs_not_contributing": candidate["runs_not_contributing"],
        },
        "unresolved_fields": sorted(set(unresolved)),
        "stage7_definition_preview": definition_preview,
        "note": COMPOSITION_NOTE,
    }


def _eligible_candidate(paths: dict, candidate_id: str) -> dict:
    """Stage 13 candidate validation (§12): unknown / ineligible /
    insufficient candidates NEVER yield a proposal (fail closed)."""
    candidate_id = _validate_candidate_id(candidate_id)
    try:
        report = show_intake_candidate(
            paths["runs_root"], candidate_id,
            policy_db_path=paths["policy_db_path"],
            ledger_path=paths["ledger_path"],
            analytics_db_path=paths["analytics_db_path"])
    except PolicyError as exc:
        raise PolicyError(
            "policy_composition_invalid",
            f"no experiment candidate derives from current evidence "
            f"with id {candidate_id}: {exc.message}") from exc
    candidate = report["candidate"]
    if candidate.get("status") != "eligible":
        raise PolicyError(
            "policy_composition_invalid",
            f"candidate {candidate_id} is '{candidate.get('status')}' — "
            "only ELIGIBLE intake candidates can be composed into an "
            "experiment definition proposal (missing/unavailable data is "
            "never negative evidence)",
            details={"status": candidate.get("status")})
    return candidate


def propose_for_candidate(runs_root, candidate_id, *, policy_db_path=None,
                          ledger_path=None, analytics_db_path=None,
                          control_variant=None, treatment_variant=None,
                          min_effect=None) -> dict:
    """Generate (derived, READ-ONLY) the experiment definition proposal
    for ONE approved eligible Stage 13 candidate. Nothing is created,
    started or assigned; nothing is persisted."""
    candidate_id = _validate_candidate_id(candidate_id)
    control, treatment = _validate_variants(control_variant,
                                            treatment_variant)
    min_effect = _validate_min_effect(min_effect)
    paths = _paths(runs_root, policy_db_path=policy_db_path,
                   ledger_path=ledger_path,
                   analytics_db_path=analytics_db_path)
    candidate = _eligible_candidate(paths, candidate_id)
    proposal = _proposal_for(candidate, paths, control_variant=control,
                             treatment_variant=treatment,
                             min_effect=min_effect)
    return {"ok": True, "proposal": proposal}


def proposals_for_policy(runs_root, policy_id, *, policy_db_path=None,
                         ledger_path=None, analytics_db_path=None,
                         control_variant=None, treatment_variant=None,
                         min_effect=None) -> dict:
    """Derived proposals for ALL eligible candidates of one policy
    (or — with ``policy_id=None`` — of every policy with evidence).
    Read-only; nothing is created or started."""
    control, treatment = _validate_variants(control_variant,
                                            treatment_variant)
    min_effect = _validate_min_effect(min_effect)
    paths = _paths(runs_root, policy_db_path=policy_db_path,
                   ledger_path=ledger_path,
                   analytics_db_path=analytics_db_path)
    candidates, _ineligible, policies_scanned = _all_candidates(paths)
    if policy_id is not None:
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise PolicyError("policy_composition_invalid",
                              f"invalid policy_id: {policy_id!r}")
        policy_id = policy_id.strip()
        candidates = [c for c in candidates
                      if c["policy_id"] == policy_id]
    proposals = []
    for candidate in candidates:
        if candidate["status"] != "eligible":
            continue  # §12: only ELIGIBLE candidates compose proposals
        proposals.append(_proposal_for(
            candidate, paths, control_variant=control,
            treatment_variant=treatment, min_effect=min_effect))
    proposals.sort(key=lambda p: p["proposal_id"])
    return {
        "ok": True,
        "policy_id": policy_id,
        "policies_scanned": policies_scanned,
        "proposal_count": len(proposals),
        "proposals": proposals,
        "aggregation": {
            "ready": sum(1 for p in proposals
                         if p["status"] == "ready"),
            "needs_operator_configuration":
                sum(1 for p in proposals if p["status"]
                    == "needs_operator_configuration"),
            "note": COMPOSITION_NOTE,
        },
    }


def show_proposal(runs_root, proposal_id, *, policy_db_path=None,
                  ledger_path=None, analytics_db_path=None,
                  control_variant=None, treatment_variant=None,
                  min_effect=None) -> dict:
    """Show ONE derived proposal by its content-addressed id. The
    projection is recomputed (nothing is stored), so a proposal whose
    evidence changed no longer derives — truthfully reported."""
    proposal_id = _validate_proposal_id(proposal_id)
    report = proposals_for_policy(
        runs_root, policy_id=None, policy_db_path=policy_db_path,
        ledger_path=ledger_path, analytics_db_path=analytics_db_path,
        control_variant=control_variant, treatment_variant=treatment_variant,
        min_effect=min_effect)
    for proposal in report["proposals"]:
        if proposal["proposal_id"] == proposal_id:
            return {"ok": True, "proposal": proposal}
    raise PolicyError(
        "policy_composition_invalid",
        f"no experiment definition proposal derives from current "
        f"approved candidates with id {proposal_id} (changed evidence "
        "yields a NEW id; nothing is stored)")


def verify_composition(runs_root, *, policy_db_path=None, ledger_path=None,
                       analytics_db_path=None) -> dict:
    """Integrity verification (§18): full deterministic re-derivation —
    the proposal projection is computed TWICE and must be identical;
    candidate/evidence integrity comes from the Stage 12/13 owners.
    Read-only; nothing is mutated."""
    paths = _paths(runs_root, policy_db_path=policy_db_path,
                   ledger_path=ledger_path,
                   analytics_db_path=analytics_db_path)
    db_paths = {k: paths[k] for k in ("policy_db_path", "ledger_path",
                                      "analytics_db_path")}
    first = proposals_for_policy(runs_root, None, **db_paths)
    second = proposals_for_policy(runs_root, None, **db_paths)
    deterministic = (first["proposals"] == second["proposals"])
    from .effectiveness import verify_effectiveness
    from .experiment_intake import verify_intake
    evidence_integrity = verify_effectiveness(
        paths["runs_root"], **db_paths)
    intake_integrity = verify_intake(paths["runs_root"], **db_paths)
    return {
        "ok": True,
        "policies_scanned": first["policies_scanned"],
        "proposals_total": first["proposal_count"],
        "ready": first["aggregation"]["ready"],
        "needs_operator_configuration":
            first["aggregation"]["needs_operator_configuration"],
        "deterministic": deterministic,
        "candidates_consistent": intake_integrity["consistent"],
        "evidence_consistent": evidence_integrity["consistent"],
        "consistent": (deterministic
                       and intake_integrity["consistent"]
                       and evidence_integrity["consistent"]),
        "note": COMPOSITION_NOTE,
    }


# ---- the explicit approval boundary (§9/§10) ----------------------------------


def _find_stage7_candidate(learning_store, candidate_id: str) -> dict:
    """The Stage 7 learning candidate materialized by the EXPLICIT
    Stage 13 candidate approval (separate boundary — never implicitly
    performed here). Fail closed when it is absent."""
    for stored in learning_store.list_candidates():
        lineage = stored.get("lineage") or {}
        if lineage.get("stage") == 13 and \
                lineage.get("intake_candidate_id") == candidate_id:
            return stored
    raise PolicyError(
        "policy_composition_invalid",
        f"the intake candidate {candidate_id} has not been approved "
        "into the Stage 7 learning store yet — run "
        "'ayce policy experiment-candidate approve' first (two explicit "
        "approval boundaries; Stage 14 never approves the candidate "
        "implicitly)")


def approve_proposal(runs_root, proposal_id, learning_store, *,
                     approved_by, seed, now,
                     policy_db_path=None, ledger_path=None,
                     analytics_db_path=None, control_variant=None,
                     treatment_variant=None, min_effect=None,
                     dry_run=False) -> dict:
    """EXPLICIT OPERATOR APPROVAL (§9/§10) — the ONLY mutating operation
    in Stage 14. It composes the proposal's definition and creates the
    Stage 7 experiment DRAFT through the EXISTING
    ``ayce.learning.create_experiment`` owner — NOTHING else is written,
    the experiment is NEVER started, and NO units are assigned.

    Fail closed when: the proposal no longer derives; configuration is
    unresolved; the assignment seed is missing (Stage 14 never invents
    it); or the Stage 13 intake candidate has not been approved yet."""
    proposal_id = _validate_proposal_id(proposal_id)
    approved_by = _validate_approved_by(approved_by)
    seed = _validate_seed(seed)
    control, treatment = _validate_variants(control_variant,
                                            treatment_variant)
    min_effect = _validate_min_effect(min_effect)
    paths = _paths(runs_root, policy_db_path=policy_db_path,
                   ledger_path=ledger_path,
                   analytics_db_path=analytics_db_path)

    # re-derive the proposal from CURRENT evidence (fail closed when the
    # candidate or its evidence no longer derives — §12)
    report = proposals_for_policy(
        paths["runs_root"], policy_id=None, **{
            k: paths[k] for k in ("policy_db_path", "ledger_path",
                                  "analytics_db_path")},
        control_variant=control, treatment_variant=treatment,
        min_effect=min_effect)
    proposal = next((p for p in report["proposals"]
                     if p["proposal_id"] == proposal_id), None)
    if proposal is None:
        raise PolicyError(
            "policy_composition_invalid",
            f"no experiment definition proposal derives from current "
            f"approved candidates with id {proposal_id}; approval fails "
            "closed")
    if proposal["status"] != "ready":
        raise PolicyError(
            "policy_composition_invalid",
            f"proposal {proposal_id} is '{proposal['status']}' — "
            f"unresolved {proposal['unresolved_fields']} cannot be "
            "silently invented; supply explicit operator configuration "
            "or fix the evidence",
            details={"unresolved_fields": proposal["unresolved_fields"]})

    # the two-approval architecture: the Stage 13 candidate approval
    # must ALREADY have happened (explicitly, separately)
    stage7 = _find_stage7_candidate(learning_store,
                                    proposal["candidate_id"])

    preview = proposal["stage7_definition_preview"]
    hypothesis = (
        f"Controlled experiment for approved intake candidate "
        f"{proposal['candidate_id']} (definition proposal "
        f"{proposal['proposal_id']}): observational evidence exists for "
        f"policy {proposal['policy_id']} "
        f"v{proposal['policy_version']} on metric "
        f"{preview['metric']}; the observed difference requires a "
        "CONTROLLED test — this is not a causal claim")
    definition_kwargs = dict(
        candidate_id=stage7["candidate_id"],
        hypothesis=hypothesis,
        metric=preview["metric"],
        control_variant=preview["control_variant"],
        treatment_variant=preview["treatment_variant"],
        population={"units": preview["population"]["units"]},
        assignment={"seed": seed},
        success_criterion=dict(preview["success_criterion"]),
        evaluation_window=preview["evaluation_window"],
        holdout=preview["holdout"],
        now=now,
        dry_run=dry_run,
    )
    # the EXISTING Stage 7 owner — the learning store is NEVER written
    # directly, and nothing beyond this call exists on the write path
    from ..learning import create_experiment as stage7_create_experiment
    try:
        result = stage7_create_experiment(learning_store,
                                          **definition_kwargs)
    except LearningError as exc:
        raise PolicyError(
            "policy_composition_invalid",
            f"the Stage 7 experiment owner refused the definition "
            f"(nothing was created): {exc.message}",
            details={"stage7_code": exc.code}) from exc
    experiment = result["experiment"]
    report = {
        "ok": True,
        "proposal_id": proposal_id,
        "candidate_id": proposal["candidate_id"],
        "stage7_candidate_id": stage7["candidate_id"],
        "experiment_id": experiment["experiment_id"],
        "experiment_status": experiment["status"],  # 'draft' — ALWAYS
        "started": False,
        "assignments": 0,
        "duplicate": bool(experiment.get("duplicate")),
        "dry_run": bool(dry_run),
        "boundary": "existing Stage 7 ayce.learning.create_experiment "
                    "(learning store); the experiment is a DRAFT — "
                    "approve/start/assign/evaluate remain separate "
                    "explicit Stage 7 operator actions",
        "note": COMPOSITION_NOTE,
    }
    if not dry_run:
        assignments = learning_store.assignments_for_experiment(
            experiment["experiment_id"])
        report["assignments"] = len(assignments)
    return report