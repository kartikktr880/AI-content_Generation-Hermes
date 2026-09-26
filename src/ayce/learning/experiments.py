"""Stage 7 — Learning candidates, experiments, approval, assignment.

EXPLICIT, auditable hypothesis → experiment pipeline (§10–§17). No
"try things and see": every experiment is a fully declared, immutable,
content-addressed definition. Nothing here can start itself, and
nothing here touches production.

Hard boundaries:

- A candidate NEVER becomes production knowledge automatically (§10):
  promotion happens only through the explicit curator
  (``knowledge.curate``), which requires a VALIDATED evaluation.
- Experiment definitions are content-addressed (``experiment_id`` =
  hash of the canonical definition): mutation is impossible by
  construction — a changed definition is a NEW experiment (§15).
- Assignment is deterministic (sha256 over experiment|unit|seed) and
  idempotent: an already-assigned unit is never silently reassigned
  (§14). A conflicting variant would be a construction impossibility;
  the existing assignment is returned untouched.
- The unit of experimentation is the PUBLISHED VIDEO — the only unit
  the existing lineage can reliably identify (§12).
- Approval is an explicit boundary (§17): draft experiments cannot
  assign or evaluate.
"""

from __future__ import annotations

import hashlib
import re

from .derived import FORMULA_CATALOG

__all__ = [
    "EXPERIMENT_STATUSES",
    "CANDIDATE_STATUSES",
    "UNIT_ID_RE",
    "candidate_id_for",
    "experiment_id_for",
    "assignment_method",
    "variant_for_unit",
    "create_candidate",
    "create_experiment",
    "approve_experiment",
    "start_experiment",
    "assign_units",
]

#: Experiment state machine (§16): a small explicit machine. Terminal
#: states: evaluated, cancelled, invalid.
EXPERIMENT_STATUSES = (
    "draft", "approved", "running", "evaluated", "cancelled", "invalid",
)

#: Candidate states mirror the evaluation outcome truthfully (§24/§34):
#: ``rejected`` and ``insufficient_evidence`` are DISTINCT.
CANDIDATE_STATUSES = (
    "candidate", "experimenting", "validated", "rejected",
    "insufficient_evidence",
)

#: Unit grammar: published-video ids (the experiment unit, §12).
UNIT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

#: Deterministic assignment mechanism (§14): sha256(experiment|unit|seed)
#: mod 2. Recorded on every assignment row.
ASSIGNMENT_METHOD = "sha256_mod2_v1"

#: Declared evaluation metrics: normalized Stage 6 metric names plus
#: derived-metric formula names.
_NORMALIZED_METRICS = {
    "views", "likes", "comments", "estimatedMinutesWatched",
    "averageViewDuration", "averageViewPercentage", "subscribersGained",
    "subscribersLost",
}


def _canonical(payload) -> str:
    import json
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def candidate_id_for(*, hypothesis: str, scope: str, evidence: list,
                     counterexamples: list) -> str:
    identity = {
        "hypothesis": hypothesis,
        "scope": scope,
        "evidence": evidence,
        "counterexamples": counterexamples,
    }
    return "cnd-" + hashlib.sha256(
        _canonical(identity).encode("utf-8")).hexdigest()[:24]


def experiment_id_for(definition: dict) -> str:
    """Content-addressed experiment identity (§15): the definition IS
    the identity — mutation of a definition is definitionally a new
    experiment."""
    return "exp-" + hashlib.sha256(
        _canonical(definition).encode("utf-8")).hexdigest()[:24]


def assignment_method(seed: str) -> str:
    return f"{ASSIGNMENT_METHOD}:{seed}"


def variant_for_unit(experiment_id: str, unit_id: str, seed: str) -> str:
    """Deterministic assignment: stable across re-runs, processes and
    restarts. There is no randomness and no silent reassignment."""
    digest = hashlib.sha256(
        f"{experiment_id}|{unit_id}|{seed}".encode("utf-8")).digest()
    return "control" if digest[0] % 2 == 0 else "treatment"

def _validate_evidence(entries, field: str) -> list:
    """Evidence entries must be structured references (§8/§22) — never
    opaque numbers. Allowed shapes:
    ``{kind, ref}`` or ``{kind, ref, video_id, observed_at, value}``."""
    if not isinstance(entries, list):
        from .errors import LearningError
        raise LearningError("learning_invalid_input",
                            f"{field} must be a list of reference entries")
    cleaned = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("kind") \
                or not entry.get("ref"):
            from .errors import LearningError
            raise LearningError(
                "learning_invalid_input",
                f"{field} entries must be objects with 'kind' and 'ref'",
                details={"entry": entry})
        cleaned.append({
            "kind": str(entry["kind"]),
            "ref": str(entry["ref"]),
            "video_id": entry.get("video_id"),
            "observed_at": entry.get("observed_at"),
            "value": entry.get("value"),
        })
    return cleaned


def create_candidate(store, *, hypothesis: str, scope: str,
                     evidence: list, counterexamples: list,
                     lineage: dict | None = None, now: str = "",
                     dry_run: bool = False) -> dict:
    """Create an EXPLICIT learning candidate (§10). Idempotent: an
    identical hypothesis/scope/evidence set returns the existing
    candidate (deterministic identity, no duplicates). The candidate
    never becomes production knowledge by itself."""
    from .errors import LearningError

    if not isinstance(hypothesis, str) or not hypothesis.strip():
        raise LearningError("learning_invalid_input",
                            "hypothesis must be a non-empty statement")
    if not isinstance(scope, str) or not scope.strip():
        raise LearningError("learning_invalid_input",
                            "scope must be a non-empty knowledge scope")
    evidence = _validate_evidence(evidence, "evidence")
    counterexamples = _validate_evidence(counterexamples,
                                         "counterexamples")
    if not evidence:
        raise LearningError("learning_invalid_input",
                            "a candidate requires at least one evidence "
                            "reference (hypotheses without evidence are "
                            "not recorded)")
    identity = candidate_id_for(
        hypothesis=hypothesis.strip(), scope=scope.strip(),
        evidence=evidence, counterexamples=counterexamples)
    candidate = {
        "candidate_id": identity,
        "hypothesis": hypothesis.strip(),
        "scope": scope.strip(),
        "evidence": evidence,
        "counterexamples": counterexamples,
        "lineage": lineage or {},
        "status": "candidate",
        "created_at": now,
        "updated_at": now,
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "candidate": candidate}
    inserted = store.insert_candidate(row=candidate)
    candidate["duplicate"] = not inserted
    return {"ok": True, "candidate": candidate}

def _validate_definition(*, candidate_id: str, hypothesis: str, metric: str,
                         control_variant: str, treatment_variant: str,
                         population: dict, evaluation_window,
                         success_criterion: dict, assignment: dict,
                         holdout) -> dict:
    """Deterministic definition validation (§11). Every field must be
    explicit; nothing is defaulted silently."""
    from .errors import LearningError

    if not isinstance(candidate_id, str) or not candidate_id:
        raise LearningError("learning_invalid_input",
                            "experiment must reference a candidate")
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        raise LearningError("learning_invalid_input",
                            "experiment hypothesis must be explicit")
    if metric not in _NORMALIZED_METRICS and metric not in FORMULA_CATALOG:
        raise LearningError(
            "learning_metric_unavailable",
            f"declared metric {metric!r} is neither a normalized Stage 6 "
            "metric nor a derived formula",
            details={"normalized": sorted(_NORMALIZED_METRICS),
                     "derived": sorted(FORMULA_CATALOG)})
    if not control_variant or not treatment_variant:
        raise LearningError("learning_invalid_input",
                            "control and treatment variants must be named")
    if control_variant == treatment_variant:
        raise LearningError("learning_experiment_invalid",
                            "control and treatment variants must differ")
    if not isinstance(population, dict) or not population.get("units"):
        raise LearningError("learning_experiment_invalid",
                            "population must explicitly list its units")
    units = population["units"]
    if not isinstance(units, list) or len(units) < 2:
        raise LearningError("learning_experiment_invalid",
                            "an experiment requires at least 2 units",
                            details={"units": units})
    for unit in units:
        if not isinstance(unit, str) or not UNIT_ID_RE.fullmatch(unit):
            raise LearningError("learning_invalid_input",
                                f"invalid unit id: {unit!r}")
    if len(set(units)) != len(units):
        raise LearningError("learning_experiment_invalid",
                            "population contains duplicate units")
    if not isinstance(assignment, dict) or not assignment.get("seed"):
        raise LearningError("learning_invalid_input",
                            "assignment must declare its seed/version")
    if evaluation_window is not None and (
            not isinstance(evaluation_window, dict)
            or not evaluation_window.get("start")
            or not evaluation_window.get("end")):
        raise LearningError("learning_invalid_input",
                            "evaluation_window must have start and end")
    if holdout is not None:
        if not isinstance(holdout, dict) or not isinstance(
                holdout.get("fraction"), (int, float)) or not (
                0 < holdout["fraction"] < 1):
            raise LearningError("learning_experiment_invalid",
                                "holdout.fraction must be in (0, 1)")
        if not isinstance(holdout.get("min_n", 1), int) or \
                holdout.get("min_n", 1) < 1:
            raise LearningError("learning_experiment_invalid",
                                "holdout.min_n must be an integer >= 1")
    return _validate_criterion(
        candidate_id=candidate_id, hypothesis=hypothesis, metric=metric,
        control_variant=control_variant,
        treatment_variant=treatment_variant, population=population,
        evaluation_window=evaluation_window,
        success_criterion=success_criterion, assignment=assignment,
        holdout=holdout)

def _validate_criterion(*, candidate_id: str, hypothesis: str, metric: str,
                        control_variant: str, treatment_variant: str,
                        population: dict, evaluation_window,
                        success_criterion: dict, assignment: dict,
                        holdout) -> dict:
    """The success-criterion half of definition validation: thresholds
    are DECLARED in the definition, never implicit (§20)."""
    from .errors import LearningError

    for field in ("min_control_n", "min_treatment_n"):
        value = success_criterion.get(field)
        if not isinstance(value, int) or value < 1:
            raise LearningError(
                "learning_experiment_invalid",
                f"success_criterion.{field} must be an explicit integer >= 1")
    if "min_effect" in success_criterion and \
            not isinstance(success_criterion["min_effect"], (int, float)):
        raise LearningError("learning_experiment_invalid",
                            "success_criterion.min_effect must be numeric")
    if "max_p" in success_criterion and (
            not isinstance(success_criterion["max_p"], (int, float))
            or not 0 < success_criterion["max_p"] < 1):
        raise LearningError("learning_experiment_invalid",
                            "success_criterion.max_p must be in (0, 1) — "
                            "thresholds are DECLARED, never implicit (§20)")
    direction = success_criterion.get(
        "direction", "treatment_greater_than_control")
    if direction not in ("treatment_greater_than_control",
                         "treatment_less_than_control"):
        raise LearningError("learning_experiment_invalid",
                            f"unknown direction: {direction!r}")
    definition = {
        "candidate_id": candidate_id,
        "hypothesis": hypothesis.strip(),
        "metric": metric,
        "unit": "video",  # the only reliably identifiable unit (§12)
        "control_variant": control_variant,
        "treatment_variant": treatment_variant,
        "population": {"kind": "explicit",
                       "units": sorted(set(population["units"]))},
        "evaluation_window": evaluation_window,
        "success_criterion": {
            "direction": direction,
            "min_effect": success_criterion.get("min_effect", 0.0),
            "min_control_n": success_criterion["min_control_n"],
            "min_treatment_n": success_criterion["min_treatment_n"],
            **({"max_p": success_criterion["max_p"]}
               if "max_p" in success_criterion else {}),
        },
        "assignment": {
            "method": ASSIGNMENT_METHOD,
            "seed": str(assignment["seed"]),
        },
        "holdout": ({"fraction": holdout["fraction"],
                     "min_n": holdout.get("min_n", 1)} if holdout else None),
        "validation_method": ("holdout_split_v1" if holdout else "none"),
    }
    return definition


def create_experiment(store, *, candidate_id: str, hypothesis: str,
                      metric: str, control_variant: str,
                      treatment_variant: str, population: dict,
                      assignment: dict, success_criterion: dict,
                      evaluation_window=None, holdout=None,
                      now: str = "", dry_run: bool = False) -> dict:
    """Create an IMMUTABLE, content-addressed experiment (§11/§15).

    The candidate must already exist (experiments never appear from
    nowhere). Identical definitions return the SAME experiment
    (idempotent); ANY change to the definition yields a different
    experiment id — mutation is impossible by construction.
    """
    from .errors import LearningError

    candidate = store.get_candidate(candidate_id)
    if candidate is None:
        raise LearningError("learning_invalid_input",
                            f"unknown candidate: {candidate_id}")
    definition = _validate_definition(
        candidate_id=candidate_id, hypothesis=hypothesis, metric=metric,
        control_variant=control_variant,
        treatment_variant=treatment_variant, population=population,
        evaluation_window=evaluation_window,
        success_criterion=success_criterion, assignment=assignment,
        holdout=holdout)
    experiment_id = experiment_id_for(definition)
    experiment = {
        "experiment_id": experiment_id,
        "candidate_id": candidate_id,
        "definition": definition,
        "status": "draft",
        "created_at": now,
        "updated_at": now,
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "experiment": experiment}
    inserted = store.insert_experiment(row=experiment)
    experiment["duplicate"] = not inserted
    if inserted:
        # candidate moves to 'experimenting' (status only — the
        # hypothesis/evidence/counterexamples are NEVER rewritten)
        store.update_candidate_status(candidate_id, "experimenting", now=now)
    return {"ok": True, "experiment": experiment}


def approve_experiment(store, experiment_id: str, *, now: str) -> dict:
    """The EXPLICIT approval boundary (§17): draft → approved. Nothing
    launches automatically; this is an operator decision."""
    from .errors import LearningError

    experiment = store.get_experiment(experiment_id)
    if experiment is None:
        raise LearningError("learning_experiment_invalid",
                            f"unknown experiment: {experiment_id}")
    if not store.transition_experiment(
            experiment_id, from_status="draft", to_status="approved",
            now=now):
        raise LearningError(
            "learning_experiment_invalid",
            f"experiment {experiment_id} is not in 'draft' (current: "
            f"{experiment['status']}); approval is a one-time boundary")
    return {"ok": True, "experiment_id": experiment_id, "status": "approved"}

def start_experiment(store, experiment_id: str, *, now: str) -> dict:
    """approved → running (assignment/evaluation become meaningful)."""
    from .errors import LearningError

    experiment = store.get_experiment(experiment_id)
    if experiment is None:
        raise LearningError("learning_experiment_invalid",
                            f"unknown experiment: {experiment_id}")
    if not store.transition_experiment(
            experiment_id, from_status="approved", to_status="running",
            now=now):
        raise LearningError(
            "learning_experiment_invalid",
            f"experiment {experiment_id} is not in 'approved' (current: "
            f"{experiment['status']})")
    return {"ok": True, "experiment_id": experiment_id, "status": "running"}


def assign_units(store, experiment_id: str, units: list, *, now: str,
                 dry_run: bool = False) -> dict:
    """Deterministic, idempotent unit → variant assignment (§14).

    Re-assignment returns the EXISTING variant unchanged; the stored
    assignment is authoritative. Dry-run computes the assignment
    without persisting anything."""
    from .errors import LearningError

    experiment = store.get_experiment(experiment_id)
    if experiment is None:
        raise LearningError("learning_experiment_invalid",
                            f"unknown experiment: {experiment_id}")
    if experiment["status"] not in ("approved", "running"):
        raise LearningError(
            "learning_experiment_invalid",
            f"experiment {experiment_id} is not approved (current: "
            f"{experiment['status']}); assignment requires the explicit "
            "approval boundary (§17)")
    if not isinstance(units, list) or not units:
        raise LearningError("learning_invalid_input",
                            "units must be a non-empty list")
    for unit in units:
        if not isinstance(unit, str) or not UNIT_ID_RE.fullmatch(unit):
            raise LearningError("learning_invalid_input",
                                f"invalid unit id: {unit!r}")
    definition = experiment["definition"]
    seed = definition["assignment"]["seed"]
    population = set(definition["population"]["units"])
    for unit in units:
        if unit not in population:
            raise LearningError(
                "learning_experiment_invalid",
                f"unit {unit!r} is not in the declared population; "
                "definitions are immutable — create a new experiment",
                details={"population": sorted(population)})

    planned, persisted = [], []
    for unit in sorted(set(units)):
        variant = variant_for_unit(experiment_id, unit, seed)
        existing = store.get_assignment(experiment_id, unit)
        if existing is not None:
            planned.append({"unit_id": unit,
                            "variant": existing["variant"],
                            "previously_assigned": True})
            persisted.append(existing)
            continue
        if dry_run:
            planned.append({"unit_id": unit, "variant": variant,
                            "previously_assigned": False})
            continue
        inserted = store.insert_assignment(row={
            "experiment_id": experiment_id, "unit_id": unit,
            "variant": variant,
            "assignment_method": definition["assignment"]["method"],
            "assignment_seed": seed, "assigned_at": now,
        })
        if not inserted:  # lost a race — read back the winner's row
            existing = store.get_assignment(experiment_id, unit)
            variant = existing["variant"] if existing else variant
        planned.append({"unit_id": unit, "variant": variant,
                        "previously_assigned": existing is not None})
        persisted.append(store.get_assignment(experiment_id, unit))

    if not dry_run and experiment["status"] == "approved":
        # first persisted assignment flips approved → running
        store.transition_experiment(experiment_id, from_status="approved",
                                    to_status="running", now=now)
    return {
        "ok": True,
        "experiment_id": experiment_id,
        "assignments": planned,
        "control": [a["unit_id"] for a in persisted
                    if a and a["variant"] == "control"],
        "treatment": [a["unit_id"] for a in persisted
                      if a and a["variant"] == "treatment"],
        "dry_run": dry_run,
    }

