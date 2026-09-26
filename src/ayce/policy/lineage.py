"""Stage 11 — Cross-run policy lineage index (DERIVED projection).

A deterministic, read-oriented projection over the CANONICAL Stage 10
consumption artifacts::

    run directories
        → registered POLICY_CONSUMPTION artifacts
        → deterministic scan (fail-closed verification)
        → lineage projection
              Run → Policies
              Policy → Runs

Hard invariants (§2/§4/§12):

- DERIVED, NOT AUTHORITATIVE: the canonical sources of truth remain the
  consumption artifacts (``run_dir/policy_consumption*.json`` + the Stage
  10 artifact registry) and the immutable ProductionPolicy store. The
  index is recomputed from them on every query — no persistent index
  exists, so nothing can drift, and ``rebuild`` is by construction a
  deterministic recomputation (delete + rebuild ⇒ identical projection).
- The repository scale (tens of runs) makes a full deterministic scan
  effectively free; no persistent index is introduced (§4 — do not
  invent a scalability problem).
- OBSERVABILITY ONLY: the scanner READS run artifacts and the policy
  store (SQLite ``mode=ro``); it never writes policy/learning/analytics/
  RunState/consumption artifacts and has no lifecycle mutation calls.
- Fail closed (§14): corrupt evidence (missing/mutated/malformed
  artifact, hash/identity mismatch, run mismatch, policy-reference
  inconsistency) is REPORTED with structured integrity information and
  EXCLUDED from the associations — never silently included, never
  silently dropped.
- Missing evidence (run exists, no consumption artifact) is distinct
  from a verified ``policy_status = none`` event (§7); no-policy runs
  never enter the policy→runs projection because there is no policy
  identity to fabricate (§6).
- Historical: the index describes CONSUMPTION, not current activation —
  events remain valid after v2 activation, rollback, retirement (§9).
- Deterministic ordering everywhere: run queries by
  ``(created_at, consumption_id)``; policy queries by
  ``(created_at, run_id, consumption_id)`` (§18).
"""

from __future__ import annotations

import re
from pathlib import Path

from .errors import PolicyError
from .observability import read_run_consumptions, verify_consumption_history

__all__ = [
    "lineage_for_run",
    "lineage_for_policy",
    "verify_lineage_index",
    "rebuild_lineage_index",
]

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_POLICY_ID_RE = re.compile(r"^pol-[A-Za-z0-9._-]{1,64}$")


def _validate_run_id(run_id) -> str:
    if not isinstance(run_id, str) or \
            not _RUN_ID_RE.fullmatch(run_id.strip()):
        raise PolicyError(
            "policy_lineage_invalid",
            f"invalid run_id: {run_id!r} (expected ^[A-Za-z0-9]"
            "[A-Za-z0-9._-]{0,63}$)")
    return run_id.strip()


def _validate_policy_id(policy_id) -> str:
    if not isinstance(policy_id, str) or \
            not _POLICY_ID_RE.fullmatch(policy_id.strip()):
        raise PolicyError(
            "policy_lineage_invalid",
            f"invalid policy_id: {policy_id!r} (expected pol-…)")
    return policy_id.strip()


def _event_reference_problems(event: dict, run_id: str) -> list[str]:
    """Internal-consistency checks beyond the Stage 10 artifact hash
    verification (§8): run containment + policy-reference coherence."""
    problems = []
    if event.get("run_id") != run_id:
        problems.append("run_id_mismatch")
    if not isinstance(event.get("consumption_id"), str) or \
            not event["consumption_id"].startswith("cons-"):
        problems.append("consumption_id_invalid")
    policy_id = event.get("policy_id")
    if policy_id is None:
        # a no-policy event must not carry fabricated policy references
        if event.get("policy_status") not in (None, "none"):
            problems.append("policy_reference_inconsistent")
        if event.get("policy_version") is not None or \
                event.get("policy_content_hash") is not None:
            problems.append("policy_reference_inconsistent")
    else:
        if not isinstance(event.get("policy_version"), int) or \
                isinstance(event.get("policy_version"), bool) or \
                event["policy_version"] < 1:
            problems.append("policy_version_invalid")
        content_hash = event.get("policy_content_hash")
        if not isinstance(content_hash, str) or \
                not content_hash.startswith("sha256:"):
            problems.append("policy_content_hash_invalid")
    return problems


def _scan_runs(runs_root, *, policy_db_path=None) -> dict:
    """Deterministic full scan: every candidate run directory →
    hash-verified consumption events → per-run records + the
    policy→runs projection + integrity report. Corrupt evidence is
    reported and EXCLUDED from associations (fail closed, §14)."""
    runs_root = Path(runs_root)
    scan = {
        "ok": True,
        "runs_root_missing": not runs_root.is_dir(),
        "runs_scanned": 0,
        "run_index": {},       # run_id → record (sorted keys by caller)
        "policy_index": {},    # policy_id → {version_str → [entries]}
        "integrity": {
            "runs_verified": 0,
            "runs_missing_evidence": 0,
            "runs_corrupt": 0,
            "duplicate_consumption_ids": [],
            "run_mismatches": [],
            "policy_reference_problems": [],
        },
        "derived_from": "canonical policy consumption artifacts "
                        "(Stage 10); index is a projection, not authority",
    }
    if scan["runs_root_missing"]:
        return scan

    run_index: dict[str, dict] = {}
    seen_consumption: dict[str, str] = {}  # consumption_id → run_id
    policy_buckets: dict[str, dict[str, list]] = {}
    for run_dir in sorted((p for p in runs_root.iterdir() if p.is_dir()),
                          key=lambda p: p.name):
        run_id = run_dir.name
        if not _RUN_ID_RE.fullmatch(run_id):
            continue
        scan["runs_scanned"] += 1
        record = {"run_id": run_id, "evidence_status": "missing",
                  "consumptions": [], "integrity": None}
        manifest = run_dir / "artifacts.json"
        if manifest.is_file():
            try:
                read = read_run_consumptions(run_dir, run_id, verify=True)
            except PolicyError as exc:
                record["evidence_status"] = "corrupt"
                record["integrity"] = exc.to_dict()["error"]
            else:
                events = read["consumptions"]
                if events:
                    record["evidence_status"] = "verified"
                    record["consumptions"] = events
                else:
                    record["note"] = read.get("note")
        if record["evidence_status"] == "verified":
            scan["integrity"]["runs_verified"] += 1
            for event in record["consumptions"]:
                problems = _event_reference_problems(event, run_id)
                if problems:
                    record["evidence_status"] = "corrupt"
                    record["integrity"] = {
                        "code": "policy_consumption_failed",
                        "message": "consumption event failed internal "
                                   "consistency verification",
                        "details": {"consumption_id":
                                    event.get("consumption_id"),
                                    "problems": problems}}
                    scan["integrity"]["policy_reference_problems"].append(
                        {"run_id": run_id,
                         "consumption_id": event.get("consumption_id"),
                         "problems": problems})
                    break
        elif record["evidence_status"] == "missing":
            scan["integrity"]["runs_missing_evidence"] += 1
        else:
            scan["integrity"]["runs_corrupt"] += 1
        # cross-run duplicate consumption detection (a consumption_id
        # embeds run_id; appearing under two runs = integrity violation)
        if record["evidence_status"] == "verified":
            for event in record["consumptions"]:
                cid = event["consumption_id"]
                if cid in seen_consumption and \
                        seen_consumption[cid] != run_id:
                    scan["integrity"]["duplicate_consumption_ids"].append(
                        {"consumption_id": cid,
                         "runs": sorted({seen_consumption[cid], run_id})})
                seen_consumption[cid] = run_id
        run_index[run_id] = record

    # policy→runs projection: ONLY verified events with a policy identity
    for run_id in sorted(run_index):
        record = run_index[run_id]
        if record["evidence_status"] != "verified":
            continue
        for event in record["consumptions"]:
            policy_id = event.get("policy_id")
            if policy_id is None:
                continue  # no fabricated policy identity (§6)
            version_key = str(event.get("policy_version"))
            entry = dict(event, run_id=run_id,
                         identity="verified")
            policy_buckets.setdefault(policy_id, {}).setdefault(
                version_key, []).append(entry)
    for policy_id in sorted(policy_buckets):
        versions = policy_buckets[policy_id]
        for version_key in sorted(versions, key=lambda v: (len(v), v)):
            versions[version_key].sort(
                key=lambda e: (str(e.get("created_at") or ""), e["run_id"],
                               e["consumption_id"]))
    scan["policy_index"] = policy_buckets
    scan["run_index"] = run_index
    return scan


def _entry_identity(entry: dict, policy_db_path) -> dict:
    """Attach historical verification (§10/§15/§16) — identity + current
    policy state + content match, WITHOUT requiring the policy to still
    be active."""
    verification = verify_consumption_history(entry, policy_db_path)
    return dict(entry, policy_state=verification["policy_state"],
                content_match=verification["content_match"],
                identity=verification["identity"])


def lineage_for_run(runs_root, run_id, *, policy_db_path=None,
                    scope: str | None = None) -> dict:
    """Run → Policies (§5, run direction). Returns the run's consumption
    evidence with its truthful status; no-policy events are included
    (policy_status none) and never receive a fabricated identity."""
    run_id = _validate_run_id(run_id)
    scan = _scan_runs(runs_root, policy_db_path=policy_db_path)
    if scan["runs_root_missing"]:
        return {"ok": False, "run_found": False, "run_id": run_id,
                "error": {"code": "runs_root_missing",
                          "message": "the runs root does not exist"}}
    record = scan["run_index"].get(run_id)
    if record is None:
        return {"ok": False, "run_found": False, "run_id": run_id,
                "error": {"code": "run_not_found",
                          "message": f"no such run: {run_id}"}}
    policies = []
    for event in record["consumptions"]:
        if scope is not None and event.get("scope") != scope:
            continue
        if policy_db_path is not None:
            policies.append(_entry_identity(event, policy_db_path))
        else:
            policies.append(dict(event))
    return {
        "ok": True,
        "run_found": True,
        "run_id": run_id,
        "evidence_status": record["evidence_status"],
        "count": len(policies),
        "policies": policies,  # ordered (created_at, consumption_id)
        "integrity": record.get("integrity"),
        **({"note": record.get("note")} if record.get("note") else {}),
    }


def lineage_for_policy(runs_root, policy_id, policy_version=None, *,
                       policy_db_path=None, scope: str | None = None) -> dict:
    """Policy → Runs (§5, policy direction). Historical consumption
    only: results are independent of the policy's CURRENT activation
    state; optional exact ``policy_version`` / ``scope`` filters."""
    policy_id = _validate_policy_id(policy_id)
    if policy_version is not None and (
            not isinstance(policy_version, int)
            or isinstance(policy_version, bool) or policy_version < 1):
        raise PolicyError("policy_lineage_invalid",
                          f"invalid policy_version: {policy_version!r}")
    scan = _scan_runs(runs_root, policy_db_path=policy_db_path)
    if scan["runs_root_missing"]:
        return {"ok": False, "error": {"code": "runs_root_missing",
                                       "message": "the runs root does not "
                                                  "exist"}}
    versions = scan["policy_index"].get(policy_id, {})
    matched = []
    for version_key in sorted(versions, key=lambda v: (len(v), v)):
        if policy_version is not None and str(policy_version) != version_key:
            continue
        for entry in versions[version_key]:
            if scope is not None and entry.get("scope") != scope:
                continue
            if policy_db_path is not None:
                matched.append(_entry_identity(entry, policy_db_path))
            else:
                matched.append(dict(entry))
    matched.sort(key=lambda e: (str(e.get("created_at") or ""), e["run_id"],
                                e["consumption_id"]))
    return {
        "ok": True,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "run_count": len(matched),
        "runs": matched,  # ordered (created_at, run_id, consumption_id)
    }


def verify_lineage_index(runs_root, *, policy_db_path=None) -> dict:
    """Full integrity verification (§14): every run's consumption
    artifacts hash/identity-checked, cross-run duplicates and reference
    problems reported. Fail closed: nothing silently dropped — corrupt
    runs appear with structured integrity information."""
    scan = _scan_runs(runs_root, policy_db_path=policy_db_path)
    corrupt_runs = [
        {"run_id": run_id, "integrity": record["integrity"]}
        for run_id, record in sorted(scan["run_index"].items())
        if record["evidence_status"] == "corrupt"
    ]
    return {
        "ok": True,
        "runs_scanned": scan["runs_scanned"],
        "runs_verified": scan["integrity"]["runs_verified"],
        "runs_missing_evidence": scan["integrity"]["runs_missing_evidence"],
        "runs_corrupt": scan["integrity"]["runs_corrupt"],
        "corrupt_runs": corrupt_runs,
        "duplicate_consumption_ids":
            scan["integrity"]["duplicate_consumption_ids"],
        "policy_reference_problems":
            scan["integrity"]["policy_reference_problems"],
        "consistent": (not corrupt_runs
                       and not scan["integrity"]["duplicate_consumption_ids"]
                       and not scan["integrity"]["policy_reference_problems"]),
    }


def rebuild_lineage_index(runs_root, *, policy_db_path=None) -> dict:
    """Recompute the projection from canonical artifacts (§12). The index
    is DERIVED (nothing persisted), so rebuild = deterministic rescan:
    delete + rebuild ⇒ identical projection, repeated rebuilds are
    idempotent, and only this projection — never policy/learning/
    analytics/RunState/artifacts — is produced."""
    scan = _scan_runs(runs_root, policy_db_path=policy_db_path)
    return {
        "ok": True,
        "derived": True,
        "runs_scanned": scan["runs_scanned"],
        "runs_verified": scan["integrity"]["runs_verified"],
        "runs_missing_evidence": scan["integrity"]["runs_missing_evidence"],
        "runs_corrupt": scan["integrity"]["runs_corrupt"],
        "policies_indexed": len(scan["policy_index"]),
        "consistent": (not scan["integrity"]["runs_corrupt"]
                       and not scan["integrity"]["duplicate_consumption_ids"]
                       and not scan["integrity"]["policy_reference_problems"]),
        "note": "index is a derived projection; recomputed deterministically "
                "from canonical consumption artifacts",
    }



