"""Stage 7 — Deterministic experiment evaluation (§19–§24).

A SMALL deterministic statistics layer — no heavyweight platform, no
third-party dependency. Evaluation:

- selects the declared metric per unit by an explicit, recorded rule
  (``latest_present_v1``: the latest observation whose availability is
  usable — zero IS usable for normalized metrics, undefined/missing
  derived metrics are NOT usable);
- reports sample sizes (control_n / treatment_n) and EXCLUDED units
  with reasons (missing metric, undefined derived metric, invalid
  lineage) — nothing is silently dropped (§22);
- reports the effect estimate (mean difference), its standard error,
  and a two-sided normal-approximation p-value (labelled as such —
  an honest approximation, not a heavyweight test);
- decides ONLY against the DECLARED criterion recorded in the
  immutable experiment definition (§20 — no magic thresholds);
- uses a deterministic development/holdout split when the definition
  configures one (§23): the criterion is evaluated on the development
  population, the holdout must agree in DIRECTION with sufficient n;
- distinguishes ``validated`` / ``rejected`` / ``insufficient_evidence``
  — insufficient evidence is NEVER a rejection (§21/§34).

Evaluation records are content-addressed (experiment + dataset
fingerprint + selection rule): re-evaluating the SAME data returns the
identical record (idempotent, §32); new data produces a NEW record —
history is never overwritten.
"""

from __future__ import annotations

import hashlib
import math

__all__ = ["evaluate_experiment"]

_METRIC_SELECTION_RULE = "latest_present_v1"


def _canonical(payload) -> str:
    import json
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _mean(values: list) -> float | None:
    return sum(values) / len(values) if values else None


def _std(values: list) -> float | None:
    """Sample standard deviation (ddof=1); None when n < 2."""
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _two_sided_p(effect: float, standard_error: float) -> float | None:
    """Two-sided p-value under a normal approximation of the effect
    (labelled honestly in the evaluation record — not a heavyweight
    exact test)."""
    if standard_error is None or standard_error <= 0:
        return None
    z = abs(effect) / standard_error
    return math.erfc(z / math.sqrt(2.0))


def _select_metric_value(analytics_store, learning_store, *, metric: str,
                         unit_id: str, is_derived: bool):
    """``latest_present_v1``: the latest USABLE value for one unit.
    Returns ``(value_or_None, reason_or_None)``."""
    if is_derived:
        row = learning_store.latest_present_derived(metric, unit_id)
        if row is not None:
            return float(row["value"]), None
        history = learning_store.derived_for_video(unit_id, formula=metric)
        if not history:
            return None, "metric never derived for this unit"
        latest = history[-1]
        if latest["availability"] == "undefined":
            return None, "derived metric undefined (zero denominator)"
        return None, "derived metric missing for this unit"
    measurements = analytics_store.measurements_for_video(unit_id)
    usable = [m for m in measurements
              if m["metric_name"] == metric
              and m["availability"] in ("present", "zero")
              and m.get("value") is not None]
    if not measurements:
        return None, "metric never measured for this unit"
    if not usable:
        return None, "no usable measurement (all missing/unavailable)"
    latest = max(usable, key=lambda m: (m["observed_at"],
                                        m["measurement_id"]))
    return float(latest["value"]), None


def _direction_ok(direction: str, effect: float, min_effect: float) -> bool:
    if direction == "treatment_greater_than_control":
        return effect >= min_effect
    return effect <= -min_effect


def _criterion_report(criterion: dict, effect, p_value, control_n,
                      treatment_n) -> list:
    """The auditable criterion table (§20): declared part, observed
    value, decision — for EVERY declared component."""
    rows = []
    direction = criterion["direction"]
    rows.append({
        "criterion": direction,
        "observed": f"effect={effect if effect is not None else 'n/a'}",
        "met": (effect is not None and _direction_ok(
            direction, effect, criterion.get("min_effect", 0.0))),
    })
    rows.append({
        "criterion": "min_effect",
        "observed": criterion.get("min_effect", 0.0),
        "met": (effect is not None and abs(effect)
                >= criterion.get("min_effect", 0.0)),
    })
    rows.append({
        "criterion": "min_control_n",
        "observed": control_n,
        "met": (control_n is not None
                and control_n >= criterion["min_control_n"]),
    })
    rows.append({
        "criterion": "min_treatment_n",
        "observed": treatment_n,
        "met": (treatment_n is not None
                and treatment_n >= criterion["min_treatment_n"]),
    })
    if "max_p" in criterion:
        rows.append({
            "criterion": "max_p (declared threshold)",
            "observed": p_value,
            "met": (p_value is not None and p_value <= criterion["max_p"]),
        })
    return rows

def _variant_of(assignments, unit_id: str) -> str:
    for a in assignments:
        if a["unit_id"] == unit_id:
            return a["variant"]
    return "unknown"


def _holdout_split(experiment_id: str, holdout_spec,
                   units: list) -> tuple[set, set]:
    """Deterministic development/holdout split (§23): a unit is in the
    holdout when sha256("holdout|experiment|unit") % 100 falls below
    fraction*100. Same inputs → same split, always."""
    if not holdout_spec:
        return set(units), set()
    threshold = int(round(holdout_spec["fraction"] * 100))
    holdout, dev = set(), set()
    for unit in units:
        digest = hashlib.sha256(
            f"holdout|{experiment_id}|{unit}".encode()).digest()
        (holdout if digest[0] % 100 < threshold else dev).add(unit)
    return dev, holdout


def _direction_ok(direction: str, effect: float, min_effect: float) -> bool:
    if direction == "treatment_greater_than_control":
        return effect >= min_effect
    return effect <= -min_effect


def _criterion_report(criterion: dict, effect, p_value, control_n,
                      treatment_n) -> list:
    """The auditable criterion table (§20): declared part, observed
    value, decision — for EVERY declared component."""
    rows = []
    direction = criterion["direction"]
    rows.append({
        "criterion": direction,
        "observed": f"effect={effect if effect is not None else 'n/a'}",
        "met": (effect is not None and _direction_ok(
            direction, effect, criterion.get("min_effect", 0.0))),
    })
    rows.append({
        "criterion": "min_effect",
        "observed": criterion.get("min_effect", 0.0),
        "met": (effect is not None and abs(effect)
                >= criterion.get("min_effect", 0.0)),
    })
    rows.append({
        "criterion": "min_control_n",
        "observed": control_n,
        "met": (control_n is not None
                and control_n >= criterion["min_control_n"]),
    })
    rows.append({
        "criterion": "min_treatment_n",
        "observed": treatment_n,
        "met": (treatment_n is not None
                and treatment_n >= criterion["min_treatment_n"]),
    })
    if "max_p" in criterion:
        rows.append({
            "criterion": "max_p (declared threshold)",
            "observed": p_value,
            "met": (p_value is not None and p_value <= criterion["max_p"]),
        })
    return rows


def evaluate_experiment(learning_store, analytics_store,
                        experiment_id: str, *, now: str,
                        dry_run: bool = False) -> dict:
    """Evaluate one running experiment against its DECLARED criterion.

    Deterministic and idempotent: the same dataset produces the same
    evaluation record; new data produces a NEW record (history kept).
    Updates the candidate status truthfully (validated / rejected /
    insufficient_evidence) together with the evaluation insert.
    Dry-run computes and reports without persisting anything.
    """
    from .derived import FORMULA_CATALOG
    from .errors import LearningError

    experiment = learning_store.get_experiment(experiment_id)
    if experiment is None:
        raise LearningError("learning_experiment_invalid",
                            f"unknown experiment: {experiment_id}")
    definition = experiment["definition"]
    metric = definition["metric"]
    is_derived = metric in FORMULA_CATALOG
    if experiment["status"] not in ("running", "evaluated"):
        raise LearningError(
            "learning_experiment_invalid",
            f"experiment {experiment_id} is not running (current: "
            f"{experiment['status']}); approval and assignment come first")

    assignments = learning_store.assignments_for_experiment(experiment_id)
    if not assignments:
        raise LearningError("learning_evaluation_failed",
                            "experiment has no assignments to evaluate")

    # ---- per-unit metric selection (explicit rule, exclusions recorded) ----
    unit_values: dict[str, float] = {}
    excluded: list[dict] = []
    for assignment in sorted(assignments, key=lambda a: a["unit_id"]):
        unit = assignment["unit_id"]
        value, reason = _select_metric_value(
            analytics_store, learning_store, metric=metric, unit_id=unit,
            is_derived=is_derived)
        if value is None:
            excluded.append({"unit_id": unit,
                             "variant": assignment["variant"],
                             "reason": reason})
        else:
            unit_values[unit] = value

    # ---- variant split -------------------------------------------------------
    control = [unit_values[a["unit_id"]] for a in assignments
               if a["variant"] == "control" and a["unit_id"] in unit_values]
    treatment = [unit_values[a["unit_id"]] for a in assignments
                 if a["variant"] == "treatment"
                 and a["unit_id"] in unit_values]

    control_mean, treatment_mean = _mean(control), _mean(treatment)
    effect = (None if control_mean is None or treatment_mean is None
              else treatment_mean - control_mean)
    control_std, treatment_std = _std(control), _std(treatment)
    standard_error = None
    if control and treatment and control_std is not None \
            and treatment_std is not None:
        standard_error = math.sqrt(
            control_std ** 2 / len(control)
            + treatment_std ** 2 / len(treatment))
    p_value = (None if effect is None or standard_error is None
               else _two_sided_p(effect, standard_error))

    criterion = definition["success_criterion"]
    control_n, treatment_n = len(control), len(treatment)
    observed_rows = _criterion_report(criterion, effect, p_value,
                                      control_n, treatment_n)

    # ---- insufficiency FIRST (never conflated with rejection, §21) -----------
    insufficient = (control_n < criterion["min_control_n"]
                    or treatment_n < criterion["min_treatment_n"])

    dev, holdout_units = _holdout_split(experiment["experiment_id"],
                                        definition.get("holdout"),
                                        sorted(unit_values))
    holdout_report = None
    if insufficient:
        status = "insufficient_evidence"
    elif not all(row["met"] for row in observed_rows):
        status = "rejected"
    elif definition.get("holdout"):
        status, holdout_report = _evaluate_holdout(
            definition, criterion, unit_values, assignments,
            sorted(dev), sorted(holdout_units))
    else:
        status = "validated"

    return _persist_evaluation(
        learning_store, experiment=experiment, status=status,
        control=control, treatment=treatment,
        control_mean=control_mean, treatment_mean=treatment_mean,
        effect=effect, standard_error=standard_error, p_value=p_value,
        criterion=criterion, observed_rows=observed_rows,
        holdout_report=holdout_report, excluded=excluded,
        unit_values=unit_values, assignments=assignments,
        dev=sorted(dev), holdout_units=sorted(holdout_units),
        now=now, dry_run=dry_run)


def _evaluate_holdout(definition, criterion, unit_values, assignments,
                      dev, holdout_units):
    """Holdout validation (§23): the development result must be
    CONFIRMED in direction by the holdout population; an undersized
    holdout is insufficiency, not confirmation (§21)."""
    h_control = [v for u, v in unit_values.items()
                 if u in holdout_units
                 and _variant_of(assignments, u) == "control"]
    h_treatment = [v for u, v in unit_values.items()
                   if u in holdout_units
                   and _variant_of(assignments, u) == "treatment"]
    h_effect = (None if not h_control or not h_treatment
                else _mean(h_treatment) - _mean(h_control))
    min_n = definition["holdout"]["min_n"]
    if (len(h_control) < min_n or len(h_treatment) < min_n
            or h_effect is None):
        return "insufficient_evidence", {
            "validation_method": "holdout_split_v1",
            "dev_units": dev, "holdout_units": holdout_units,
            "control_n": len(h_control), "treatment_n": len(h_treatment),
            "effect": h_effect,
            "note": "holdout sample below declared minimum; the "
                    "development result cannot be confirmed",
        }
    if not _direction_ok(criterion["direction"], h_effect, 0.0):
        return "rejected", {
            "validation_method": "holdout_split_v1",
            "dev_units": dev, "holdout_units": holdout_units,
            "control_n": len(h_control), "treatment_n": len(h_treatment),
            "effect": h_effect,
            "note": "holdout direction contradicts the development result",
        }
    return "validated", {
        "validation_method": "holdout_split_v1",
        "dev_units": dev, "holdout_units": holdout_units,
        "control_n": len(h_control), "treatment_n": len(h_treatment),
        "effect": h_effect,
        "note": "holdout agrees with the development result",
    }


def _persist_evaluation(learning_store, *, experiment, status, control,
                        treatment, control_mean, treatment_mean, effect,
                        standard_error, p_value, criterion, observed_rows,
                        holdout_report, excluded, unit_values, assignments,
                        dev, holdout_units, now, dry_run):
    """Content-addressed, idempotent persistence (§32): the evaluation
    identity covers the experiment + the exact dataset + the selection
    rule, so re-evaluating the SAME data returns the existing record;
    changed data legitimately produces a new record. Status updates to
    the experiment and candidate happen in the SAME transaction as the
    insert."""
    fingerprint_payload = {
        "experiment_id": experiment["experiment_id"],
        "metric_selection": _METRIC_SELECTION_RULE,
        "dataset": sorted(
            (unit, _variant_of(assignments, unit), repr(value))
            for unit, value in unit_values.items()),
    }
    fingerprint = "fp-" + hashlib.sha256(
        _canonical(fingerprint_payload).encode("utf-8")).hexdigest()[:24]
    evaluation_id = "eval-" + hashlib.sha256(
        _canonical({"experiment_id": experiment["experiment_id"],
                    "dataset_fingerprint": fingerprint,
                    "metric_selection": _METRIC_SELECTION_RULE,
                    }).encode("utf-8")).hexdigest()[:24]

    details = {
        "unit_values": {u: unit_values[u] for u in sorted(unit_values)},
        "excluded": excluded,
        "control_values": control,
        "treatment_values": treatment,
        "dev_units": dev,
        "holdout_units": holdout_units,
        "effect_method": ("mean_difference_normal_approximation_v1"
                          if standard_error is not None else
                          "mean_difference_v1"),
        "units_assigned": len(assignments),
        "units_usable": len(unit_values),
        "units_excluded": len(excluded),
    }
    evaluation = {
        "evaluation_id": evaluation_id,
        "experiment_id": experiment["experiment_id"],
        "status": status,
        "control_n": len(control),
        "treatment_n": len(treatment),
        "control_mean": control_mean,
        "treatment_mean": treatment_mean,
        "effect": effect,
        "standard_error": standard_error,
        "p_value": p_value,
        "criterion": criterion,
        "observed": {"rows": observed_rows,
                     "dataset_fingerprint": fingerprint},
        "holdout": holdout_report,
        "metric_selection": {"rule": _METRIC_SELECTION_RULE,
                             "note": "latest usable observation per unit; "
                                     "normalized zeros are usable; "
                                     "undefined/missing are excluded"},
        "dataset_fingerprint": fingerprint,
        "details": details,
        "evaluated_at": now,
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "evaluation": evaluation}
    inserted = learning_store.insert_evaluation(row=evaluation)
    evaluation["duplicate"] = not inserted
    if inserted:
        learning_store.transition_experiment(
            experiment["experiment_id"], from_status="running",
            to_status="evaluated", now=now)
        candidate_status = {"validated": "validated",
                            "rejected": "rejected",
                            "insufficient_evidence":
                                "insufficient_evidence"}[status]
        learning_store.update_candidate_status(
            experiment["candidate_id"], candidate_status, now=now)
    return {"ok": True, "evaluation": evaluation}


