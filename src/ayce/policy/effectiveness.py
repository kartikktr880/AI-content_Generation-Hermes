"""Stage 12 — Policy effectiveness feedback (READ-ONLY evidence projection).

A deterministic, in-memory, READ-ONLY projection joining FOUR canonical
existing stores (one capability → one primary owner — no new database)::

    ProductionPolicy store (Stage 8, mode=ro)     → current policy_state
    Policy consumption artifacts (Stage 10/11)    → Policy → Runs
    Publish ledger (Stage 5, mode=ro)             → Run → Video
    Analytics observation store (Stage 6, mode=ro) → Video → Measurements
          ↓
    Effectiveness evidence records (pef-…) + descriptive aggregation

The correlation chain is EXPLICIT at every edge (§4):

    Policy Consumption → Run ID → Publish Ledger → Video ID
        → Analytics Observation → Normalized Measurement

Hard invariants:

- EVIDENCE, NOT DECISION (§1/§25): the output answers "runs consuming
  policy vN had these OBSERVED outcomes". There is NO causal claim, NO
  ranking, NO score, NO winner, NO promotion/rollback recommendation,
  and NO autonomous path from analytics back into ProductionPolicy.
- DERIVED, NOT AUTHORITATIVE (§21): nothing is persisted; the projection
  is recomputed deterministically on every query, so repeated queries
  and rebuilds return identical projections and no persistence
  concurrency mechanism is required (§32). All source stores are opened
  SQLite ``mode=ro`` (§20) — policy.sqlite3, the publish ledger,
  analytics.sqlite3, RunState and consumption artifacts are
  byte-identical after any query.
- AUTHORITATIVE EDGES ONLY (§4): the video id comes ONLY from the
  Stage 5 publish ledger (``run → publish attempt → destination →
  youtube_video_id``); never from filenames, titles, URLs, timestamps
  or fuzzy matching. Analytics come ONLY from already-ingested Stage 6
  observations — no scraping, no external API calls, no fabrication.
- MISSING-DATA TAXONOMY (§13): ``not_consumed`` / ``not_published`` /
  ``analytics_not_collected`` / ``metric_unavailable`` / ``metric_missing``
  / ``metric_present`` are DISTINCT states. Missing analytics is never
  converted into zero; unavailable metrics are never converted into zero.
- TIME-WINDOW INTEGRITY (§7): every measurement retains its Stage 6
  ``source`` / ``window_start`` / ``window_end`` / ``window_timezone`` /
  ``observed_at`` / ``observation_id``. Lifetime Data-API metrics and
  windowed Analytics-API metrics are NEVER aggregated together — the
  metric identity is (metric, source, window) (§12).
- CROSS-CHECK FAIL-CLOSED (§19): consumption.run_id == publish.run_id and
  analytics.video_id == publish.video_id are verified; any disagreement
  (or an ambiguous publication) produces ``policy_effectiveness_invalid``
  evidence with structured reasons — never a guessed attribution.
- HISTORICAL INTEGRITY (§16): records describe CONSUMPTION; they remain
  valid after v2 activation, rollback and retirement. Current policy
  state is exposed ONLY as metadata (``policy_state``).
- DESCRIPTIVE STATISTICS ONLY (§26): n / mean / median / min / max over
  measurements that are actually present; exclusions are reported, never
  silently dropped; no p-values, no significance, no experimentation
  machinery (Stage 7 owns that boundary).
- Deterministic ordering everywhere; random-free identities (§9).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .errors import PolicyError
from .lineage import (
    _POLICY_ID_RE,
    _RUN_ID_RE,
    _event_reference_problems,
    _scan_runs,
)
from .observability import verify_consumption_history

__all__ = [
    "EFFECTIVENESS_SCHEMA_VERSION",
    "MISSING_DATA_STATUSES",
    "EVIDENCE_NOTE",
    "effectiveness_for_run",
    "effectiveness_for_policy",
    "metric_effectiveness_summary",
    "verify_effectiveness",
]

#: Effectiveness-record schema version.
EFFECTIVENESS_SCHEMA_VERSION = 1

#: The missing/unavailable-data taxonomy (§13) — distinct, never collapsed
#: into a zero and never silently dropped.
MISSING_DATA_STATUSES = (
    "not_consumed",
    "not_published",
    "analytics_not_collected",
    "metric_unavailable",
    "metric_missing",
    "metric_present",
)

#: The evidence boundary statement carried on every aggregate (§1/§14/§26).
EVIDENCE_NOTE = (
    "descriptive evidence only: observed outcomes of runs CONSUMING each "
    "policy version; correlation/association, not causality; no ranking, "
    "no score, no winner, no promotion or rollback recommendation"
)

#: Stage 6 availabilities that carry a REAL measured value (0 is real data).
_MEASUREMENT_AVAILABLE = ("present", "zero")


# ---- read-only IO over the canonical stores (§20/§24/§28) -------------------


def _canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _open_ro(path) -> sqlite3.Connection:
    """Open one canonical SQLite store STRICTLY read-only (mode=ro)."""
    connection = sqlite3.connect(
        f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    return connection


def _load_json_object(text):
    """Parse a stored JSON object; returns ``None`` for corrupt payloads
    (the corruption is reported by the caller — never repaired, never
    fabricated)."""
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _ledger_rows(ledger_path, run_id: str | None = None) -> list[dict] | None:
    """Read the Stage 5 publish ledger READ-ONLY (mode=ro). Returns
    ``None`` when the store does not exist yet (a truthful absence —
    never treated as an analytics failure, §5)."""
    path = Path(ledger_path)
    if not path.is_file():
        return None
    sql = ("SELECT package_seal, package_id, run_id, destination, status,"
           " youtube_video_id, created_at, updated_at FROM publish_attempts")
    params: tuple = ()
    if run_id is not None:
        sql += " WHERE run_id = ?"
        params = (run_id,)
    sql += " ORDER BY id"
    try:
        connection = _open_ro(path)
    except sqlite3.Error as exc:
        raise PolicyError("policy_effectiveness_invalid",
                          f"the publish ledger could not be opened: {exc}")
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    except sqlite3.Error as exc:
        raise PolicyError("policy_effectiveness_invalid",
                          f"the publish ledger could not be read: {exc}")
    finally:
        connection.close()


def _analytics_snapshot(analytics_db_path, video_id: str) -> dict:
    """Read the Stage 6 analytics store READ-ONLY (mode=ro) for ONE video:
    raw observations + their normalized measurements, joined, in
    deterministic order. Corrupt (unparseable) observation lineage is
    REPORTED via ``lineage_corrupt`` — never silently dropped (§31)."""
    path = Path(analytics_db_path)
    if not path.is_file():
        return {"store_present": False, "observations": [],
                "measurements": []}
    try:
        connection = _open_ro(path)
    except sqlite3.Error as exc:
        raise PolicyError("policy_effectiveness_invalid",
                          f"the analytics store could not be opened: {exc}")
    try:
        observations = []
        for row in connection.execute(
            "SELECT observation_id, source, scope, window_start, window_end,"
            " window_timezone, observed_at, lineage FROM raw_observations"
            " WHERE youtube_video_id = ? ORDER BY observed_at,"
            " observation_id", (video_id,),
        ):
            row = dict(row)
            lineage = _load_json_object(row.pop("lineage"))
            row["lineage"] = lineage
            row["lineage_corrupt"] = lineage is None
            observations.append(row)
        measurements = []
        for row in connection.execute(
            "SELECT measurement_id, observation_id, source, metric_name,"
            " value, value_type, unit, availability, window_start,"
            " window_end, observed_at FROM normalized_measurements"
            " WHERE youtube_video_id = ?"
            " ORDER BY observed_at, source, metric_name, measurement_id",
            (video_id,),
        ):
            measurements.append(dict(row))
        return {"store_present": True,
                "observations": observations, "measurements": measurements}
    except sqlite3.Error as exc:
        raise PolicyError("policy_effectiveness_invalid",
                          f"the analytics store could not be read: {exc}")
    finally:
        connection.close()


def _analytics_resolution(analytics_db_path, target: dict) -> dict:
    """Video → Analytics (§6, AUTHORITATIVE Stage 6 edge) for ONE publish
    target. Distinct statuses, never collapsed (§13):

    - store absent / no observation for THIS video →
      ``analytics_status = "unavailable"`` (published but analytics not
      collected — NOT an error, NOT zero);
    - otherwise ``"available"`` with the already-ingested measurements
      normalized to the evidence shape; the observation window's
      ``window_timezone`` is carried onto each measurement (§7 — the
      window semantics of the observation the metric came from).
    """
    snapshot = _analytics_snapshot(analytics_db_path,
                                   target["youtube_video_id"])
    if not snapshot["store_present"] or not snapshot["observations"]:
        return {"analytics_status": "unavailable",
                "measurements": [],
                "observations": snapshot["observations"]}
    window_timezone_by_observation = {
        obs["observation_id"]: obs.get("window_timezone")
        for obs in snapshot["observations"]}
    measurements = []
    for row in snapshot["measurements"]:
        measurements.append({
            "measurement_id": row["measurement_id"],
            "observation_id": row["observation_id"],
            "metric": row["metric_name"],
            "value": row["value"],
            "value_type": row["value_type"],
            "unit": row["unit"],
            "availability": row["availability"],
            "source": row["source"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "window_timezone": window_timezone_by_observation.get(
                row["observation_id"]),
            "observed_at": row["observed_at"],
        })
    return {"analytics_status": "available", "measurements": measurements,
            "observations": snapshot["observations"]}


def _analytics_crosscheck_problems(observations: list[dict], run_id: str,
                                   target: dict) -> list[str]:
    """Analytics-lineage cross-check (§19): the ingested observations
    must agree with the AUTHORITATIVE publish resolution —
    ``analytics.video_id == publish.video_id`` and, where the Stage 6
    lineage carries run/package references, those must match too.
    Any disagreement is a structured problem; nothing is guessed."""
    problems = []
    for obs in observations:
        label = f"obs:{obs.get('observation_id')}"
        if obs.get("lineage_corrupt"):
            problems.append(f"analytics_lineage_corrupt:{label}")
            continue
        lineage = obs.get("lineage") or {}
        if lineage.get("youtube_video_id") != target["youtube_video_id"]:
            problems.append(f"analytics_video_id_mismatch:{label}")
        if lineage.get("run_id") != run_id:
            problems.append(f"analytics_run_id_mismatch:{label}")
        if lineage.get("destination") != target["destination"]:
            problems.append(f"analytics_destination_mismatch:{label}")
    return sorted(set(problems))


# ---- explicit graph edges (§4/§5/§6/§19) -------------------------------------


def _publish_resolution(run_id: str, rows: list[dict] | None) -> dict:
    """Run → Publish (§5, AUTHORITATIVE Stage 5 edge). Deterministic:

    - no ledger store / no rows / no verified published row with a video
      id → ``publish_status = "unpublished"`` (NOT an analytics failure);
    - exactly one distinct video id → one publish target per
      (video_id, destination), in deterministic order;
    - multiple DISTINCT video ids for one run → ``ambiguous`` (reported,
      never guessed — §31).
    """
    if rows is None:
        return {"publish_status": "unpublished", "targets": [],
                "ledger_statuses": [], "ledger_missing": True,
                "ambiguous": False}
    published = [r for r in rows
                 if r.get("youtube_video_id")
                 and r.get("status") == "published"]
    targets = [{"youtube_video_id": r["youtube_video_id"],
                "destination": r["destination"],
                "package_id": r["package_id"],
                "package_seal": r["package_seal"],
                "ledger_status": r["status"]}
               for r in published]
    targets.sort(key=lambda t: (t["youtube_video_id"], t["destination"]))
    video_ids = sorted({t["youtube_video_id"] for t in targets})
    return {
        "publish_status": "published" if targets else "unpublished",
        "targets": targets,
        "ledger_statuses": sorted({r["status"] for r in rows}),
        "ledger_missing": False,
        "ambiguous": len(video_ids) > 1,
    }


def _missing_data_status(record: dict) -> str:
    """Record-level taxonomy (§13): the FURTHEST point the evidence
    chain reached — distinct states, never collapsed into zero."""
    if record["policy_id"] is None:
        return "not_consumed"
    if record["publish_status"] != "published":
        return "not_published"
    if record["analytics_status"] != "available":
        return "analytics_not_collected"
    availabilities = {m["availability"] for m in record["measurements"]}
    if any(a in _MEASUREMENT_AVAILABLE for a in availabilities):
        return "metric_present"
    if "unavailable" in availabilities:
        return "metric_unavailable"
    return "metric_missing"


def _effectiveness_identity(*, policy_id, policy_version, consumption_id,
                            run_id, video_id, destination,
                            measurements) -> str:
    """Deterministic content identity (§9): the same underlying evidence
    always yields the same ``pef-…`` id; random ids are impossible."""
    basis = {
        "schema_version": EFFECTIVENESS_SCHEMA_VERSION,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "consumption_id": consumption_id,
        "run_id": run_id,
        "video_id": video_id,
        "destination": destination,
        "observation_ids": sorted({m["observation_id"]
                                   for m in measurements}),
        "measurement_ids": sorted({m["measurement_id"]
                                   for m in measurements}),
    }
    digest = hashlib.sha256(_canonical(basis).encode("utf-8")).hexdigest()
    return "pef-" + digest[:24]


def _record_for_event(event: dict, run_id: str, *, policy_db_path,
                      ledger_rows, analytics_db_path, run_lineage) -> dict:
    """Build ONE effectiveness record for one verified consumption event
    (§8). This is the full evidence chain: consumption → publish →
    analytics → measurements, with fail-closed cross-checks (§19)."""
    problems = [p for p in _event_reference_problems(event, run_id)]
    policy_id = event.get("policy_id")
    record = {
        "schema_version": EFFECTIVENESS_SCHEMA_VERSION,
        "policy_id": policy_id,
        "policy_version": event.get("policy_version"),
        "policy_status": event.get("policy_status"),
        "scope": event.get("scope"),
        "policy_state": None,
        "consumption_id": event.get("consumption_id"),
        "decision_id": event.get("decision_id"),
        "scope": event.get("scope"),
        "run_id": run_id,
        "publish_status": "unpublished",
        "publish": None,
        "ledger_statuses": [],
        "ledger_missing": False,
        "analytics_status": "not_applicable",
        "measurements": [],
        "lineage": {key: (run_lineage or {}).get(key)
                    for key in ("run_id", "script_id", "research_id",
                                "objective_id")},
        "evidence_status": "valid",
        "invalid_reasons": [],
    }
    if policy_db_path is not None and Path(policy_db_path).is_file():
        try:
            verification = verify_consumption_history(event, policy_db_path)
        except PolicyError as exc:
            problems.append(f"policy_verification_failed:{exc.code}")
        else:
            record["policy_state"] = verification["policy_state"]
            if verification["identity"] == "corrupt":
                problems.append("consumption_identity_corrupt")
    if problems:
        # fail closed (§19/§31): report, never guess an attribution
        record["evidence_status"] = "invalid"
        record["invalid_reasons"] = sorted(set(problems))
        record["missing_data_status"] = "not_consumed" if policy_id is None \
            else "not_published"
        return [record]

    publish = _publish_resolution(run_id, ledger_rows)
    if publish["ambiguous"]:
        record["evidence_status"] = "invalid"
        record["invalid_reasons"] = ["ambiguous_publication"]
        record["ledger_statuses"] = publish["ledger_statuses"]
        record["missing_data_status"] = "not_published"
        return [record]

    if publish["publish_status"] != "published":
        record["ledger_statuses"] = publish["ledger_statuses"]
        record["ledger_missing"] = publish["ledger_missing"]
        record["missing_data_status"] = _missing_data_status(record)
        return [record]

    # one deterministic record per (video_id, destination) target
    records = []
    for target in publish["targets"]:
        entry = dict(record)
        entry["publish_status"] = "published"
        entry["publish"] = target
        entry["ledger_statuses"] = publish["ledger_statuses"]
        entry["ledger_missing"] = False
        resolution = _analytics_resolution(analytics_db_path, target)
        entry["analytics_status"] = resolution["analytics_status"]
        entry["measurements"] = resolution["measurements"]
        problems = _analytics_crosscheck_problems(
            resolution["observations"], run_id, target)
        if problems:
            entry["evidence_status"] = "invalid"
            entry["invalid_reasons"] = problems
        entry["missing_data_status"] = _missing_data_status(entry)
        if not problems:
            # an identity is earned by VALID evidence only (§9): invalid
            # records carry their structured reasons instead (§19/§31)
            entry["effectiveness_id"] = _effectiveness_identity(
                policy_id=entry["policy_id"],
                policy_version=entry["policy_version"],
                consumption_id=entry["consumption_id"],
                run_id=run_id,
                video_id=target["youtube_video_id"],
                destination=target["destination"],
                measurements=entry["measurements"])
        records.append(entry)
    return records


# ---- descriptive aggregation (§10/§11/§12/§26) --------------------------------


def _metric_identity(measurement: dict) -> tuple:
    """Metric identity (§12): (metric, source, window) — lifetime Data-API
    semantics and windowed Analytics-API semantics NEVER merge."""
    return (measurement["metric"], measurement["source"],
            measurement["window_start"], measurement["window_end"],
            measurement["window_timezone"])


def _summarize(values: list[float]) -> dict:
    """Descriptive summary ONLY (§26): n/mean/median/min/max. No variance
    bands, no p-values, no significance, no ranking."""
    ordered = sorted(values)
    n = len(ordered)
    mean = sum(ordered) / n if n else None
    if n == 0:
        median = None
    elif n % 2:
        median = ordered[n // 2]
    else:
        median = (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    return {
        "n": n,
        "mean": mean,
        "median": median,
        "min": ordered[0] if ordered else None,
        "max": ordered[-1] if ordered else None,
    }


def _outcome_groups(measurements: list[dict]) -> list[dict]:
    """Group measurements by EXACT metric identity; summarize per group;
    exclusions are reported explicitly (§11/§12) — never silently
    dropped, never merged across sources/windows."""
    buckets: dict[tuple, list[dict]] = {}
    for measurement in measurements:
        buckets.setdefault(_metric_identity(measurement), []).append(
            measurement)
    groups = []
    for identity in sorted(buckets, key=lambda i: (str(i[0]), str(i[1]),
                                                   str(i[2]), str(i[3]),
                                                   str(i[4]))):
        metric, source, window_start, window_end, window_timezone = identity
        members = buckets[identity]
        usable = [m for m in members
                  if m["availability"] in _MEASUREMENT_AVAILABLE
                  and m.get("value") is not None]
        excluded = [m for m in members if m not in usable]
        exclusion_counts: dict[str, int] = {}
        for m in excluded:
            exclusion_counts[m["availability"]] = \
                exclusion_counts.get(m["availability"], 0) + 1
        summary = _summarize([float(m["value"]) for m in usable])
        groups.append({
            "metric": metric,
            "source": source,
            "window_start": window_start,
            "window_end": window_end,
            "window_timezone": window_timezone,
            "window_semantics": ("lifetime_cumulative"
                                 if window_start is None else "windowed"),
            "sample_size": summary["n"],
            **summary,
            "excluded_measurements": [
                {"measurement_id": m["measurement_id"],
                 "metric": m["metric"],
                 "availability": m["availability"]}
                for m in excluded],
            "exclusion_counts": dict(sorted(exclusion_counts.items())),
        })
    return groups


def _aggregate_records(records: list[dict]) -> dict:
    """Policy-level aggregation (§10): COUNTS + descriptive outcome
    groups. No winner, no ranking, no score, no promotion signal."""
    valid = [r for r in records
             if r.get("evidence_status") == "valid"
             and r.get("policy_id") is not None]
    invalid = [r for r in records if r.get("evidence_status") != "valid"]
    consumed_runs = sorted({r["run_id"] for r in valid})
    published_runs = sorted({r["run_id"] for r in valid
                             if r["publish_status"] == "published"})
    analytics_available = sorted({r["run_id"] for r in valid
                                  if r["analytics_status"] == "available"})
    measurements = [m for r in valid for m in r["measurements"]]
    metric_present_count = sum(
        1 for m in measurements if m["availability"] in _MEASUREMENT_AVAILABLE)
    metric_missing_count = sum(
        1 for m in measurements
        if m["availability"] not in _MEASUREMENT_AVAILABLE)
    groups = _outcome_groups(measurements)
    return {
        "consumed_runs": len(consumed_runs),
        "run_ids": consumed_runs,
        "published_runs": len(published_runs),
        "published_run_ids": published_runs,
        "unpublished_runs": len(consumed_runs) - len(published_runs),
        "unpublished_run_ids": sorted(set(consumed_runs)
                                      - set(published_runs)),
        "analytics_available_runs": len(analytics_available),
        "analytics_available_run_ids": analytics_available,
        "analytics_unavailable_runs": (len(consumed_runs)
                                       - len(analytics_available)),
        "metric_present_count": metric_present_count,
        "metric_missing_count": metric_missing_count,
        "metric_present_count_by_status": {
            "present": sum(1 for m in measurements
                           if m["availability"] == "present"),
            "zero": sum(1 for m in measurements
                        if m["availability"] == "zero"),
        },
        "outcome_groups": groups,
        "invalid_records": len(invalid),
        "evidence_status": ("descriptive_evidence" if groups
                            else "insufficient_evidence"),
        "note": EVIDENCE_NOTE,
    }


# ---- public read-only API (§22/§23) ------------------------------------------


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


def _run_lineage_refs(run_dir: Path) -> dict | None:
    """Production lineage references (§18) from the run's own state —
    loaded defensively; unreadable state yields ``None`` (never invented)."""
    state_path = run_dir / "state.json"
    if not state_path.is_file():
        return None
    try:
        payload = _load_json_object(
            state_path.read_text(encoding="utf-8"))
    except OSError:
        return None
    lineage = payload.get("lineage") if payload else None
    return lineage if isinstance(lineage, dict) else None


def _build_all_records(scan: dict, *, paths: dict) -> list[dict]:
    """Deterministic full projection over the Stage 11 scan: every
    verified consumption event of every run → effectiveness record(s)."""
    ledger_missing = (paths["ledger_path"] is None
                      or not paths["ledger_path"].is_file())
    records = []
    for run_id in sorted(scan["run_index"]):
        run_record = scan["run_index"][run_id]
        if run_record["evidence_status"] != "verified":
            continue  # missing/corrupt evidence is reported at envelope level
        ledger_rows = (None if ledger_missing
                       else _ledger_rows(paths["ledger_path"], run_id))
        run_lineage = _run_lineage_refs(paths["runs_root"] / run_id)
        for event in run_record["consumptions"]:
            records.extend(_record_for_event(
                event, run_id, policy_db_path=paths["policy_db_path"],
                ledger_rows=ledger_rows,
                analytics_db_path=paths["analytics_db_path"],
                run_lineage=run_lineage))
    records.sort(key=lambda r: (r["run_id"], str(r["consumption_id"]),
                                str(r.get("effectiveness_id") or "")))
    return records


def effectiveness_for_run(runs_root, run_id, *, policy_db_path=None,
                          ledger_path=None, analytics_db_path=None) -> dict:
    """Run direction (§22): the FULL evidence status of one run — which
    policy it consumed, whether it was published (authoritative ledger),
    and what analytics exists. Read-only; nothing is persisted."""
    if not isinstance(run_id, str) or \
            not _RUN_ID_RE.fullmatch(run_id.strip()):
        raise PolicyError("policy_effectiveness_invalid",
                          f"invalid run_id: {run_id!r}")
    run_id = run_id.strip()
    paths = _paths(runs_root, policy_db_path=policy_db_path,
                   ledger_path=ledger_path,
                   analytics_db_path=analytics_db_path)
    scan = _scan_runs(paths["runs_root"], policy_db_path=None)
    if scan["runs_root_missing"]:
        raise PolicyError("policy_effectiveness_invalid",
                          "the runs root does not exist")
    run_record = scan["run_index"].get(run_id)
    if run_record is None:
        raise PolicyError("policy_effectiveness_invalid",
                          f"no such run: {run_id}")
    envelope = {
        "ok": True,
        "run_id": run_id,
        "evidence_status": run_record["evidence_status"],
        **({"integrity": run_record["integrity"]}
           if run_record.get("integrity") else {}),
        **({"note": run_record["note"]}
           if run_record.get("note") else {}),
    }
    if run_record["evidence_status"] != "verified":
        envelope.update({"records": [], "record_count": 0})
        return envelope
    records = _build_all_records(
        {"run_index": {run_id: run_record}}, paths=paths)
    envelope.update({
        "record_count": len(records),
        "records": records,
        "aggregation": _aggregate_records(records),
    })
    return envelope


def effectiveness_for_policy(runs_root, policy_id, policy_version=None, *,
                             policy_db_path=None, ledger_path=None,
                             analytics_db_path=None, metric=None,
                             video_id=None) -> dict:
    """Policy direction (§10): read-only aggregation over all runs that
    consumed a policy version — counts + descriptive outcome groups.
    Historical consumption only: independent of the policy's CURRENT
    state (§16). No winner, no ranking, no causal claim."""
    if not isinstance(policy_id, str) or \
            not _POLICY_ID_RE.fullmatch(policy_id.strip()):
        raise PolicyError("policy_effectiveness_invalid",
                          f"invalid policy_id: {policy_id!r} (expected pol-…)")
    policy_id = policy_id.strip()
    if policy_version is not None and (
            not isinstance(policy_version, int)
            or isinstance(policy_version, bool) or policy_version < 1):
        raise PolicyError("policy_effectiveness_invalid",
                          f"invalid policy_version: {policy_version!r}")
    if metric is not None and (not isinstance(metric, str)
                               or not metric.strip()):
        raise PolicyError("policy_effectiveness_invalid",
                          f"invalid metric filter: {metric!r}")
    metric = metric.strip() if isinstance(metric, str) else None
    paths = _paths(runs_root, policy_db_path=policy_db_path,
                   ledger_path=ledger_path,
                   analytics_db_path=analytics_db_path)
    scan = _scan_runs(paths["runs_root"], policy_db_path=None)
    if scan["runs_root_missing"]:
        raise PolicyError("policy_effectiveness_invalid",
                          "the runs root does not exist")
    versions = scan["policy_index"].get(policy_id, {})
    if policy_version is not None:
        versions = {str(policy_version):
                    versions.get(str(policy_version), [])}
    matched_events = [entry for version_key in
                      sorted(versions, key=lambda v: (len(v), v))
                      for entry in versions[version_key]]
    # reuse the run-direction builder for EXACTLY these events (§17:
    # Stage 11 owns Policy → Runs; the ledger + analytics own the rest)
    by_run: dict[str, set[str]] = {}
    for entry in matched_events:
        by_run.setdefault(entry["run_id"], set()).add(entry["consumption_id"])
    records = []
    for run_id in sorted(by_run):
        run_record = scan["run_index"].get(run_id)
        if run_record is None or \
                run_record["evidence_status"] != "verified":
            continue
        events = [e for e in run_record["consumptions"]
                  if e.get("consumption_id") in by_run[run_id]]
        subset = dict(run_record, consumptions=events)
        records.extend(_build_all_records(
            {"run_index": {run_id: subset}}, paths=paths))
    if metric is not None:
        for record in records:
            record["measurements"] = [
                m for m in record["measurements"]
                if m["metric"] == metric]
    if video_id is not None:
        records = [r for r in records
                   if (r.get("publish") or {}).get("youtube_video_id")
                   == video_id]
    return {
        "ok": True,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "metric_filter": metric,
        "video_id_filter": video_id,
        "record_count": len(records),
        "records": records,
        "aggregation": _aggregate_records(records),
    }


def metric_effectiveness_summary(runs_root, metric, *, policy_id=None,
                                 policy_version=None, source=None,
                                 window_start=None, window_end=None,
                                 policy_db_path=None, ledger_path=None,
                                 analytics_db_path=None) -> dict:
    """Metric direction (§11/§14): descriptive per-policy-version outcome
    summaries for ONE metric, labeled ``descriptive_comparison``.
    Groups NEVER merge sources or windows (§12); no ranking is computed
    and no policy is declared better (§14/§26)."""
    if not isinstance(metric, str) or not metric.strip():
        raise PolicyError("policy_effectiveness_invalid",
                          f"invalid metric: {metric!r}")
    metric = metric.strip()
    paths = _paths(runs_root, policy_db_path=policy_db_path,
                   ledger_path=ledger_path,
                   analytics_db_path=analytics_db_path)
    scan = _scan_runs(paths["runs_root"], policy_db_path=None)
    if scan["runs_root_missing"]:
        raise PolicyError("policy_effectiveness_invalid",
                          "the runs root does not exist")
    records = _build_all_records(scan, paths=paths)
    records = [r for r in records if r.get("policy_id") is not None
               and r.get("evidence_status") == "valid"]
    if policy_id is not None:
        if not isinstance(policy_id, str) or \
                not _POLICY_ID_RE.fullmatch(policy_id.strip()):
            raise PolicyError("policy_effectiveness_invalid",
                              f"invalid policy_id: {policy_id!r}")
        records = [r for r in records if r["policy_id"] == policy_id.strip()]
    if policy_version is not None:
        records = [r for r in records
                   if r["policy_version"] == policy_version]

    buckets: dict[tuple, list[dict]] = {}
    for record in records:
        buckets.setdefault((record["policy_id"], record["policy_version"]),
                           []).append(record)
    policies = []
    for key in sorted(buckets, key=lambda k: (str(k[0]), str(k[1]))):
        members = buckets[key]
        measurements = []
        for record in members:
            for m in record["measurements"]:
                if m["metric"] != metric:
                    continue
                if source is not None and m["source"] != source:
                    continue
                if window_start is not None and \
                        m["window_start"] != window_start:
                    continue
                if window_end is not None and \
                        m["window_end"] != window_end:
                    continue
                measurements.append(m)
        groups = _outcome_groups(measurements)
        counts = _aggregate_records(members)
        policies.append({
            "policy_id": key[0],
            "policy_version": key[1],
            "consumed_run_ids": sorted({r["run_id"] for r in members}),
            "consumed_runs": counts["consumed_runs"],
            "published_runs": counts["published_runs"],
            "unpublished_runs": counts["unpublished_runs"],
            "analytics_available_runs": counts["analytics_available_runs"],
            "analytics_unavailable_runs":
                counts["analytics_unavailable_runs"],
            "metric_present_count": sum(
                1 for m in measurements
                if m["availability"] in _MEASUREMENT_AVAILABLE),
            "metric_missing_count": sum(
                1 for m in measurements
                if m["availability"] not in _MEASUREMENT_AVAILABLE),
            "outcome_groups": groups,
            "evidence_status": ("descriptive_evidence" if groups
                                else "insufficient_evidence"),
            "note": EVIDENCE_NOTE,
        })
    return {
        "ok": True,
        "metric": metric,
        "comparison": "descriptive_comparison",
        "filters": {"policy_id": policy_id, "policy_version": policy_version,
                    "source": source, "window_start": window_start,
                    "window_end": window_end},
        "policy_count": len(policies),
        "policies": policies,
        "note": ("descriptive comparison of OBSERVED outcomes across policy "
                 "versions; groups are never merged across sources or "
                 "windows; no ranking, no effect estimate, no causal claim, "
                 "no promotion recommendation"),
    }


def verify_effectiveness(runs_root, *, policy_db_path=None, ledger_path=None,
                         analytics_db_path=None) -> dict:
    """Integrity verification (§19/§31): full fail-closed scan — corrupt
    consumption artifacts, ambiguous publications, analytics lineage
    mismatches and invalid records are REPORTED with structured reasons.
    Read-only; nothing is mutated."""
    paths = _paths(runs_root, policy_db_path=policy_db_path,
                   ledger_path=ledger_path,
                   analytics_db_path=analytics_db_path)
    scan = _scan_runs(paths["runs_root"], policy_db_path=None)
    records = _build_all_records(scan, paths=paths)
    invalid_records = [{"effectiveness_id": r.get("effectiveness_id"),
                        "run_id": r["run_id"],
                        "consumption_id": r["consumption_id"],
                        "reasons": r["invalid_reasons"]}
                       for r in records if r["evidence_status"] != "valid"]
    return {
        "ok": True,
        "runs_scanned": scan["runs_scanned"],
        "runs_verified": scan["integrity"]["runs_verified"],
        "runs_missing_evidence": scan["integrity"]["runs_missing_evidence"],
        "runs_corrupt": scan["integrity"]["runs_corrupt"],
        "corrupt_runs": [{"run_id": run_id,
                          "integrity": scan["run_index"][run_id]["integrity"]}
                         for run_id in sorted(scan["run_index"])
                         if scan["run_index"][run_id]["evidence_status"]
                         == "corrupt"],
        "duplicate_consumption_ids":
            scan["integrity"]["duplicate_consumption_ids"],
        "policy_reference_problems":
            scan["integrity"]["policy_reference_problems"],
        "records_built": len(records),
        "invalid_records": invalid_records,
        "consistent": (not scan["integrity"]["runs_corrupt"]
                       and not scan["integrity"]["duplicate_consumption_ids"]
                       and not scan["integrity"]["policy_reference_problems"]
                       and not invalid_records),
    }


