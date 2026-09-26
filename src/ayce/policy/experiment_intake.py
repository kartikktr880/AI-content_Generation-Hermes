"""Stage 13 — Experiment intake boundary (READ-ONLY derived projection).

The SMALLEST evidence-to-experiment-intake bridge between Stage 12
policy-effectiveness evidence and the EXISTING Stage 7 experimentation
subsystem::

    Stage 12 Effectiveness (verified, read-only)
        ↓  derive (THIS module; no persistence)
    ExperimentCandidate (cand-…; content-addressed)
        ↓  approve_candidate  — EXPLICIT OPERATOR APPROVAL (§12)
    existing ayce.learning.create_candidate  (Stage 7 owner, its store)
        ↓  (Stage 7, unchanged, operator-driven)
    create_experiment → approve_experiment → assign → evaluate → curate

Hard invariants:

- EVIDENCE-BOUND, NOT A DECISION (§3/§6): a candidate means ONLY "there
  is sufficient observational evidence to CONSIDER a controlled
  experiment". No causal claim, no ranking, no winner, no score, no
  promotion/rollback recommendation, no p-value, no effect estimate.
  Stage 7 owns experimentation/evaluation; Stage 8 owns the policy
  lifecycle; Stage 13 owns neither.
- DERIVED, NOT PERSISTED (§9/§15): candidates are recomputed
  deterministically from Stage 12 evidence on every query. There is no
  candidate database; changed evidence yields a NEW candidate identity
  (historical evidence is never rewritten). All reads open the canonical
  stores SQLite ``mode=ro`` — candidate generation has ZERO side effects
  on production, analytics, publishing, policy, research, RunState or
  Hermes (§12/§18).
- MAXIMUM REUSE (§11): the ONLY Stage 7 touch is ``approve_candidate``,
  which calls the EXISTING ``ayce.learning.create_candidate`` through
  the EXISTING LearningStore — no duplicate engine, evaluator, store or
  assignment logic exists here. Approval never creates an experiment:
  draft/approve/start remain separate operator actions in Stage 7.
- ELIGIBILITY FAILS CLOSED (§5/§7): only VALID Stage 12 records with a
  policy identity, a publication, collected analytics and a COMPLETE
  metric identity can yield an ``eligible`` candidate. Missing /
  unavailable data is NEVER interpreted as negative evidence — it maps
  to ``insufficient_evidence`` (or record-level exclusion reasons),
  never to a value of zero.
- Deterministic identity (§8): ``cand-<sha256(canonical evidence +
  policy + metric + scope)[:16]>`` — no timestamps in the identity
  input; equivalent evidence orderings produce the same id, different
  meaningful evidence produces a different id.
- HISTORICAL (§16): candidates describe CONSUMPTION evidence; they
  remain derivable after policy v2 activation, rollback and retirement.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .errors import PolicyError
from .lineage import _POLICY_ID_RE, _scan_runs

__all__ = [
    "INTAKE_SCHEMA_VERSION",
    "CANDIDATE_STATUS_VALUES",
    "INTAKE_NOTE",
    "CANDIDATE_ID_RE",
    "MIN_SAMPLE_SIZE",
    "candidates_for_policy",
    "candidates_for_metric",
    "show_candidate",
    "verify_intake",
    "approve_candidate",
]

#: Experiment-candidate schema version.
INTAKE_SCHEMA_VERSION = 1

#: Candidate lifecycle (§10) — deliberately minimal: derived states only.
#: ``approved`` is NOT a derived state: approval is the explicit operator
#: operation that materializes the candidate into the Stage 7 learning
#: store (a Stage 7 ``cnd-…`` candidate); it never happens as a side
#: effect of generation.
CANDIDATE_STATUS_VALUES = (
    "eligible",
    "insufficient_evidence",
    "ineligible",
)

#: Intake candidate id grammar (content-addressed, §8).
CANDIDATE_ID_RE = re.compile(r"^cand-[0-9a-f]{16}$")

#: The boundary statement carried on every response (§3/§6/§12).
INTAKE_NOTE = (
    "experiment INTAKE evidence only: verified observational evidence "
    "exists for this policy/version/scope/metric and a controlled "
    "experiment MAY be considered to test the observed difference; this "
    "is not a causal claim, not a ranking, not a winner, not a promotion "
    "or rollback recommendation, and not a started experiment"
)

#: Minimum comparable sample size for an ELIGIBLE candidate. This REUSES
#: the existing Stage 7 contract (an experiment requires >= 2 declared
#: population units; success-criterion minimums are explicit integers
#: >= 1) — a structural comparability floor, NOT an invented statistical
#: threshold and NOT a significance criterion. A real but smaller
#: population is truthfully ``insufficient_evidence`` — never negative
#: evidence, never zero (§5).
MIN_SAMPLE_SIZE = 2

#: Stage 6 availabilities that carry a REAL measured value (0 is real).
_MEASUREMENT_AVAILABLE = ("present", "zero")


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


def _validate_policy_id(policy_id) -> str:
    if not isinstance(policy_id, str) or \
            not _POLICY_ID_RE.fullmatch(policy_id.strip()):
        raise PolicyError("policy_intake_invalid",
                          f"invalid policy_id: {policy_id!r} (expected "
                          "pol-…)")
    return policy_id.strip()


def _validate_policy_version(policy_version):
    if policy_version is not None and (
            not isinstance(policy_version, int)
            or isinstance(policy_version, bool) or policy_version < 1):
        raise PolicyError("policy_intake_invalid",
                          f"invalid policy_version: {policy_version!r}")
    return policy_version


def _validate_metric(metric):
    if metric is not None and (not isinstance(metric, str)
                               or not metric.strip()):
        raise PolicyError("policy_intake_invalid",
                          f"invalid metric filter: {metric!r}")
    return metric.strip() if isinstance(metric, str) else None


def _validate_candidate_id(candidate_id) -> str:
    if not isinstance(candidate_id, str) or \
            not CANDIDATE_ID_RE.fullmatch(candidate_id.strip()):
        raise PolicyError("policy_intake_invalid",
                          f"invalid candidate_id: {candidate_id!r} "
                          "(expected cand-<16 hex>)")
    return candidate_id.strip()


# ---- Stage 12 evidence retrieval (REUSED owner; no reimplementation) --------


def _records_for_policy(paths: dict, policy_id: str, policy_version,
                        metric) -> list[dict]:
    """Fetch ONE policy's Stage 12 effectiveness records through the
    existing public Stage 12 API (§1: effectiveness.py stays the owner
    of the evidence projection)."""
    from .effectiveness import effectiveness_for_policy
    report = effectiveness_for_policy(
        paths["runs_root"], policy_id, policy_version,
        policy_db_path=paths["policy_db_path"],
        ledger_path=paths["ledger_path"],
        analytics_db_path=paths["analytics_db_path"], metric=metric)
    return report.get("records", [])


def _policy_ids(paths: dict) -> list[str]:
    """Enumerate policy identities deterministically from the Stage 11
    scan (the canonical Policy → Runs owner; no new index)."""
    scan = _scan_runs(paths["runs_root"], policy_db_path=None)
    return sorted(pid for pid in scan["policy_index"]
                  if _POLICY_ID_RE.fullmatch(pid))


# ---- candidate derivation (§4/§5/§7/§8) --------------------------------------


def _identity_of(measurement: dict) -> tuple:
    """The COMPLETE metric identity (§12 of Stage 12, reused): metric +
    source + window. Lifetime and windowed semantics never merge."""
    return (measurement["metric"], measurement["source"],
            measurement["window_start"], measurement["window_end"],
            measurement["window_timezone"])


def _identity_complete(identity: tuple) -> bool:
    """A windowed identity requires BOTH bounds and a timezone; a
    lifetime identity requires all three to be absent (§5 'incomplete
    metric identity' is ineligible — never guessed into a population)."""
    _, _, window_start, window_end, window_timezone = identity
    if window_start is None and window_end is None:
        return True  # lifetime cumulative
    return bool(window_start and window_end and window_timezone)


def _contribution_reason(record: dict, identity: tuple) -> str | None:
    """Why one record does NOT contribute usable evidence to a metric
    identity (None = it contributes). Truthful exclusion reasons reuse
    the Stage 12 taxonomy — they are NEVER negative evidence."""
    if record.get("evidence_status") != "valid":
        return "invalid_evidence"
    if record.get("publish_status") != "published":
        return "not_published"
    if record.get("analytics_status") != "available":
        return "analytics_not_collected"
    matching = [m for m in record.get("measurements", [])
                if _identity_of(m) == identity]
    if not matching:
        return "metric_absent_for_identity"
    if any(m.get("availability") in _MEASUREMENT_AVAILABLE
           and m.get("value") is not None for m in matching):
        return None  # contributing
    return "metric_missing_or_unavailable"


def _summarize(values: list[float]) -> dict:
    """Descriptive summary ONLY (§6): n/mean/median/min/max. No variance,
    no p-values, no effect estimates, no significance."""
    ordered = sorted(values)
    n = len(ordered)
    mean = sum(ordered) / n if n else None
    if n == 0:
        median = None
    elif n % 2:
        median = ordered[n // 2]
    else:
        median = (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    return {"n": n, "mean": mean, "median": median,
            "min": ordered[0] if ordered else None,
            "max": ordered[-1] if ordered else None}


def _candidate_id(*, policy_id, policy_version, scope, metric_identity,
                  effectiveness_ids, measurement_ids, run_ids) -> str:
    """Content-addressed identity (§8): canonical evidence + policy +
    metric + scope, sorted (order-insensitive), NO timestamps."""
    metric, source, window_start, window_end, window_timezone = \
        metric_identity
    basis = {
        "schema_version": INTAKE_SCHEMA_VERSION,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "scope": scope,
        "metric": {"name": metric, "source": source,
                   "window_start": window_start, "window_end": window_end,
                   "window_timezone": window_timezone},
        "effectiveness_ids": sorted(set(effectiveness_ids)),
        "measurement_ids": sorted(set(measurement_ids)),
        "run_ids": sorted(set(run_ids)),
    }
    digest = hashlib.sha256(_canonical(basis).encode("utf-8")).hexdigest()
    return "cand-" + digest[:16]


def _build_candidates(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Derive intake candidates from ONE policy's Stage 12 records.

    Returns (candidates, ineligible_records). Grouping is by exact
    (policy, version, scope, metric identity); every group becomes ONE
    candidate whose status is fail-closed (§5/§7)."""
    buckets: dict[tuple, list[tuple]] = {}
    ineligible_records = []
    for record in records:
        problems = list(record.get("invalid_reasons") or [])
        if record.get("policy_id") is None:
            problems.append("not_consumed")
        if problems or record.get("evidence_status") != "valid":
            ineligible_records.append({
                "run_id": record.get("run_id"),
                "consumption_id": record.get("consumption_id"),
                "policy_id": record.get("policy_id"),
                "reasons": sorted(set(problems)) or ["invalid_evidence"],
            })
            continue
        for measurement in record.get("measurements", []):
            key = (record["policy_id"], record["policy_version"],
                   record.get("scope"), _identity_of(measurement))
            # store (record, measurement) PAIRS — a record with several
            # same-identity measurements must be counted once per
            # measurement, never duplicated per row (§8 evidence bound)
            buckets.setdefault(key, []).append((record, measurement))

    candidates = []
    for key in sorted(buckets, key=lambda k: (str(k[0]), str(k[1]),
                                              str(k[2]))
                      + tuple(str(part) for part in k[3])):
        policy_id, policy_version, scope, identity = key
        pairs = buckets[key]
        # unique records of the group (deterministic, deduplicated)
        group_records = []
        seen_runs = set()
        for record, _measurement in pairs:
            marker = (record["run_id"], record.get("consumption_id"))
            if marker not in seen_runs:
                seen_runs.add(marker)
                group_records.append(record)
        measurements = [measurement for _record, measurement in pairs]
        usable = [m for m in measurements
                  if m.get("availability") in _MEASUREMENT_AVAILABLE
                  and m.get("value") is not None]
        complete = _identity_complete(identity)
        if not complete:
            status = "ineligible"
        elif len(usable) >= MIN_SAMPLE_SIZE:
            # the Stage 7 >= 2-unit comparability floor is met (§7) —
            # real, complete, comparable observational evidence exists
            status = "eligible"
        else:
            # a real but too-small population: insufficient evidence,
            # DISTINCT from ineligible and NEVER negative evidence (§5)
            status = "insufficient_evidence"
        contributions = []
        for record in group_records:
            reason = _contribution_reason(record, identity)
            if reason is not None:
                contributions.append({"run_id": record["run_id"],
                                      "reason": reason})
        contributing_runs = sorted({r["run_id"] for r in group_records}
                                   - {c["run_id"] for c in contributions})
        metric, source, window_start, window_end, window_timezone = identity
        effectiveness_ids = [r.get("effectiveness_id")
                             for r in group_records]
        candidate = {
            "candidate_id": _candidate_id(
                policy_id=policy_id, policy_version=policy_version,
                scope=scope, metric_identity=identity,
                effectiveness_ids=effectiveness_ids,
                measurement_ids=[m["measurement_id"] for m in measurements],
                run_ids=contributing_runs),
            "schema_version": INTAKE_SCHEMA_VERSION,
            "derived": True,  # recomputed from Stage 12 evidence; never stored
            "status": status,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "scope": scope,
            "metric": {"name": metric, "source": source,
                       "window_start": window_start,
                       "window_end": window_end,
                       "window_timezone": window_timezone,
                       "window_semantics": ("lifetime_cumulative"
                                            if window_start is None
                                            else "windowed")},
            "source_effectiveness_evidence": sorted(
                {e for e in effectiveness_ids if e}),
            "run_ids": contributing_runs,
            "runs_not_contributing": sorted(
                contributions, key=lambda c: (c["run_id"], c["reason"])),
            "measurement_count": len(measurements),
            "sample_size": len(usable),
            "min_sample_size": MIN_SAMPLE_SIZE,
            "observed_summary": _summarize(
                [float(m["value"]) for m in usable]),
            "note": INTAKE_NOTE,
        }
        candidates.append(candidate)
    candidates.sort(key=lambda c: c["candidate_id"])
    ineligible_records.sort(key=lambda r: (str(r["run_id"]),
                                           str(r["consumption_id"])))
    return candidates, ineligible_records


def _filter_candidates(candidates: list[dict], *, metric=None, source=None,
                       window_start=None, window_end=None, status=None,
                       ) -> list[dict]:
    filtered = []
    for candidate in candidates:
        m = candidate["metric"]
        if metric is not None and m["name"] != metric:
            continue
        if source is not None and m["source"] != source:
            continue
        if window_start is not None and m["window_start"] != window_start:
            continue
        if window_end is not None and m["window_end"] != window_end:
            continue
        if status is not None and candidate["status"] != status:
            continue
        filtered.append(candidate)
    return filtered


def _aggregate(candidates: list[dict]) -> dict:
    return {
        "eligible": sum(1 for c in candidates if c["status"] == "eligible"),
        "insufficient_evidence": sum(1 for c in candidates
                                     if c["status"]
                                     == "insufficient_evidence"),
        "ineligible": sum(1 for c in candidates
                          if c["status"] == "ineligible"),
        "note": INTAKE_NOTE,
    }


# ---- public read-only API (§13) ----------------------------------------------


def candidates_for_policy(runs_root, policy_id, policy_version=None, *,
                          metric=None, policy_db_path=None, ledger_path=None,
                          analytics_db_path=None) -> dict:
    """Policy direction: intake candidates derived from ONE policy's
    Stage 12 effectiveness evidence (optionally one exact version and
    one exact metric name). Read-only; nothing is persisted."""
    policy_id = _validate_policy_id(policy_id)
    policy_version = _validate_policy_version(policy_version)
    metric = _validate_metric(metric)
    paths = _paths(runs_root, policy_db_path=policy_db_path,
                   ledger_path=ledger_path,
                   analytics_db_path=analytics_db_path)
    records = _records_for_policy(paths, policy_id, policy_version, metric)
    candidates, ineligible = _build_candidates(records)
    return {
        "ok": True,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "metric_filter": metric,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "ineligible_records": ineligible,
        "aggregation": _aggregate(candidates),
    }


def candidates_for_metric(runs_root, metric, *, policy_id=None,
                          policy_version=None, source=None,
                          window_start=None, window_end=None,
                          policy_db_path=None, ledger_path=None,
                          analytics_db_path=None) -> dict:
    """Metric direction: intake candidates for ONE metric across all
    policies with Stage 12 evidence. Metric identities never merge
    across sources or windows. Read-only; nothing is persisted."""
    metric = _validate_metric(metric)
    if metric is None:
        raise PolicyError("policy_intake_invalid", "metric is required")
    if policy_id is not None:
        policy_id = _validate_policy_id(policy_id)
    policy_version = _validate_policy_version(policy_version)
    paths = _paths(runs_root, policy_db_path=policy_db_path,
                   ledger_path=ledger_path,
                   analytics_db_path=analytics_db_path)
    policy_ids = ([policy_id] if policy_id is not None
                  else _policy_ids(paths))
    candidates, ineligible = [], []
    for pid in policy_ids:
        records = _records_for_policy(paths, pid, policy_version, metric)
        built, inelig = _build_candidates(records)
        candidates.extend(built)
        ineligible.extend(inelig)
    candidates.sort(key=lambda c: c["candidate_id"])
    candidates = _filter_candidates(
        candidates, metric=metric, source=source,
        window_start=window_start, window_end=window_end)
    return {
        "ok": True,
        "metric": metric,
        "policy_filter": policy_id,
        "policy_version": policy_version,
        "source_filter": source,
        "window_filters": {"window_start": window_start,
                           "window_end": window_end},
        "candidate_count": len(candidates),
        "candidates": candidates,
        "ineligible_records": ineligible,
        "aggregation": _aggregate(candidates),
    }


def _all_candidates(paths: dict) -> tuple[list[dict], list[dict], int]:
    candidates, ineligible = [], []
    for pid in _policy_ids(paths):
        records = _records_for_policy(paths, pid, None, None)
        built, inelig = _build_candidates(records)
        candidates.extend(built)
        ineligible.extend(inelig)
    candidates.sort(key=lambda c: c["candidate_id"])
    ineligible.sort(key=lambda r: (str(r["run_id"]),
                                   str(r["consumption_id"])))
    return candidates, ineligible, len(_policy_ids(paths))


def show_candidate(runs_root, candidate_id, *, policy_db_path=None,
                   ledger_path=None, analytics_db_path=None) -> dict:
    """Show ONE derived candidate by its content-addressed id. The
    projection is recomputed (nothing is stored), so a candidate that
    no longer derives from current evidence is truthfully absent."""
    candidate_id = _validate_candidate_id(candidate_id)
    paths = _paths(runs_root, policy_db_path=policy_db_path,
                   ledger_path=ledger_path,
                   analytics_db_path=analytics_db_path)
    candidates, _ineligible, _count = _all_candidates(paths)
    for candidate in candidates:
        if candidate["candidate_id"] == candidate_id:
            return {"ok": True, "candidate": candidate,
                    "aggregation": _aggregate([candidate])}
    raise PolicyError(
        "policy_intake_invalid",
        f"no experiment candidate derives from current evidence with id "
        f"{candidate_id} (changed evidence yields a NEW id; nothing is "
        "stored)")


def verify_intake(runs_root, *, policy_db_path=None, ledger_path=None,
                  analytics_db_path=None) -> dict:
    """Integrity verification (§17): full deterministic re-derivation —
    the projection is computed TWICE and must be identical (same
    evidence → same identities), invalid/ineligible evidence is
    reported with structured reasons. Read-only; nothing is mutated."""
    paths = _paths(runs_root, policy_db_path=policy_db_path,
                   ledger_path=ledger_path,
                   analytics_db_path=analytics_db_path)
    candidates, ineligible, policies_scanned = _all_candidates(paths)
    again, _ineligible, _count = _all_candidates(paths)
    deterministic = (again == candidates)
    # the Stage 12 integrity owner reports corrupt consumption artifacts,
    # ambiguous publications and analytics lineage mismatches — intake
    # never hides evidence problems (§17: fail closed, structurally)
    from .effectiveness import verify_effectiveness
    evidence_integrity = verify_effectiveness(paths["runs_root"],
                                              **{k: paths[k] for k in
                                                 ("policy_db_path",
                                                  "ledger_path",
                                                  "analytics_db_path")})
    consistent = (deterministic
                  and evidence_integrity["consistent"]
                  and not ineligible)
    return {
        "ok": True,
        "policies_scanned": policies_scanned,
        "candidates_total": len(candidates),
        "eligible": sum(1 for c in candidates
                        if c["status"] == "eligible"),
        "insufficient_evidence": sum(1 for c in candidates
                                     if c["status"]
                                     == "insufficient_evidence"),
        "ineligible": sum(1 for c in candidates
                          if c["status"] == "ineligible"),
        "ineligible_records": ineligible,
        "runs_scanned": evidence_integrity["runs_scanned"],
        "runs_corrupt": evidence_integrity["runs_corrupt"],
        "corrupt_runs": evidence_integrity["corrupt_runs"],
        "duplicate_consumption_ids":
            evidence_integrity["duplicate_consumption_ids"],
        "policy_reference_problems":
            evidence_integrity["policy_reference_problems"],
        "invalid_effectiveness_records":
            evidence_integrity["invalid_records"],
        "deterministic": deterministic,
        "consistent": consistent,
        "note": INTAKE_NOTE,
    }


# ---- the explicit approval boundary (§12) ------------------------------------


def approve_candidate(runs_root, candidate_id, learning_store, *,
                      approved_by: str, now: str,
                      policy_db_path=None, ledger_path=None,
                      analytics_db_path=None) -> dict:
    """EXPLICIT OPERATOR APPROVAL — the ONLY mutating operation in
    Stage 13, and it mutates ONLY the existing Stage 7 learning store
    through the EXISTING ``ayce.learning.create_candidate`` owner.

    The derived candidate must CURRENTLY derive as ``eligible``
    (fail-closed: insufficient/ineligible/unknown candidates are
    refused). Approval materializes a Stage 7 learning candidate
    (``cnd-…``) whose evidence entries REFERENCE the Stage 12
    effectiveness measurements — analytics/policy evidence is never
    duplicated as a new source of truth. Approval does NOT create,
    approve or start an experiment: those remain separate explicit
    Stage 7 operator actions (create_experiment → approve_experiment
    → start_experiment)."""
    candidate_id = _validate_candidate_id(candidate_id)
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise PolicyError("policy_intake_invalid",
                          "approval requires an explicit approved_by "
                          "operator identity (auditable boundary)")
    paths = _paths(runs_root, policy_db_path=policy_db_path,
                   ledger_path=ledger_path,
                   analytics_db_path=analytics_db_path)
    candidates, _ineligible, _count = _all_candidates(paths)
    candidate = next((c for c in candidates
                      if c["candidate_id"] == candidate_id), None)
    if candidate is None:
        raise PolicyError(
            "policy_intake_invalid",
            f"no experiment candidate derives from current evidence with "
            f"id {candidate_id}; approval fails closed")
    if candidate["status"] != "eligible":
        raise PolicyError(
            "policy_intake_invalid",
            f"candidate {candidate_id} is '{candidate['status']}' — only "
            "ELIGIBLE evidence can be approved into the experiment "
            "intake (missing/unavailable data is never negative "
            "evidence)",
            details={"status": candidate["status"],
                     "sample_size": candidate["sample_size"]})

    # collect the usable measurements WITH their record context (video,
    # observed_at) — references, never duplicated analytics rows
    evidence_entries = []
    for pid_record in _records_for_policy(
            paths, candidate["policy_id"], candidate["policy_version"],
            candidate["metric"]["name"]):
        if pid_record.get("evidence_status") != "valid" or \
                pid_record.get("run_id") not in candidate["run_ids"]:
            continue
        video_id = (pid_record.get("publish") or {}).get(
            "youtube_video_id")
        for measurement in pid_record.get("measurements", []):
            if _identity_of(measurement) != (
                    candidate["metric"]["name"],
                    candidate["metric"]["source"],
                    candidate["metric"]["window_start"],
                    candidate["metric"]["window_end"],
                    candidate["metric"]["window_timezone"]):
                continue
            if measurement.get("availability") not in \
                    _MEASUREMENT_AVAILABLE or \
                    measurement.get("value") is None:
                continue
            evidence_entries.append({
                "kind": "policy_effectiveness_measurement",
                "ref": f"{pid_record['effectiveness_id']}/"
                       f"{measurement['measurement_id']}",
                "video_id": video_id,
                "observed_at": measurement.get("observed_at"),
                "value": measurement.get("value"),
            })
    evidence_entries.sort(key=lambda e: (e["ref"], str(e["video_id"])))
    if not evidence_entries:
        raise PolicyError(
            "policy_intake_invalid",
            "the candidate's evidence no longer resolves to usable "
            "measurements; approval fails closed (evidence changed — a "
            "new candidate id derives)")

    metric = candidate["metric"]
    hypothesis = (
        f"Observational evidence exists for policy "
        f"{candidate['policy_id']} v{candidate['policy_version']} "
        f"(scope {candidate['scope']}) on metric {metric['name']} via "
        f"{metric['source']} ({metric['window_semantics']}); a "
        "controlled experiment may be considered to test the observed "
        "difference")
    lineage = {
        "stage": 13,
        "boundary": "experiment_intake",
        "intake_candidate_id": candidate_id,
        "policy_id": candidate["policy_id"],
        "policy_version": candidate["policy_version"],
        "scope": candidate["scope"],
        "metric_identity": metric,
        "source_effectiveness_evidence":
            candidate["source_effectiveness_evidence"],
        "sample_size": candidate["sample_size"],
        "approved_by": approved_by.strip(),
    }
    # the EXISTING Stage 7 owner and its store (no duplicate engine)
    from ..learning import create_candidate as create_learning_candidate
    result = create_learning_candidate(
        learning_store, hypothesis=hypothesis, scope=candidate["scope"],
        evidence=evidence_entries, counterexamples=[], lineage=lineage,
        now=now)
    learning_candidate = result["candidate"]
    return {
        "ok": True,
        "candidate_id": candidate_id,
        "status": "approved",
        "stage7_candidate_id": learning_candidate["candidate_id"],
        "duplicate": bool(learning_candidate.get("duplicate")),
        "evidence_entries": len(evidence_entries),
        "boundary": "existing Stage 7 ayce.learning.create_candidate "
                    "(learning store); no experiment was created, "
                    "approved or started",
        "note": INTAKE_NOTE,
    }
