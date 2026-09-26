"""Stage 10 — Policy consumption observability & decision lineage.

Makes Stage 9's consumption evidence a FIRST-CLASS, queryable, verified
run artifact::

    Director decision
        ↓  append_consumption (Stage 9 JSONL log — unchanged)
    Consumption Event (content-addressed cons-…)
        ↓  record_consumption_artifact (THIS module; idempotent)
    Run Artifact (ArtifactKind.POLICY_CONSUMPTION, hash-pinned)
        ↓  read_run_consumptions / CLI / readonly MCP
    Historical Audit (immutable, survives policy v2/rollback/retirement)

Hard invariants:

- OBSERVABILITY ONLY: this module has NO policy write path — it never
  calls approve/promote/activate/rollback/retire/reject and never
  mutates ProductionPolicy, learning, analytics, or RunState.
- Consumption artifacts are IMMUTABLE after registration: canonical
  bytes, sha256-pinned in the artifact metadata; mutation is detected,
  never silently regenerated.
- Consumption identity is the Stage 9 content-addressed
  ``consumption_id`` — the same event re-recorded produces the same
  artifact bytes and no duplicate logical event.
- HISTORICAL evidence stays readable and verifiable even when the
  referenced policy is later superseded, retired, or deleted; the
  verifier distinguishes historical validity from current policy state
  and never erases evidence.
- ``no consumption evidence`` (no artifact) is distinct from
  ``policy_status none`` (an event whose policy was absent) and from
  ``corrupt evidence`` (hash/identity failure) — nothing is fabricated.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path

from ..artifacts import ArtifactError, ArtifactRegistry
from .errors import PolicyError

__all__ = [
    "CONSUMPTION_ARTIFACT_FILENAME",
    "CONSUMPTION_SCHEMA_VERSION",
    "CONSUMPTION_STAGE",
    "consumption_artifact_bytes",
    "record_consumption_artifact",
    "read_run_consumptions",
    "verify_consumption_history",
]

#: Run-relative filename of the consumption artifact.
CONSUMPTION_ARTIFACT_FILENAME = "policy_consumption.json"
#: Registry stage label (validated by state.validate_stage_name).
CONSUMPTION_STAGE = "policy_consumption"
#: Artifact schema version.
CONSUMPTION_SCHEMA_VERSION = 1

_EVENT_REQUIRED_FIELDS = (
    "consumption_id", "run_id", "decision_id", "decision",
    "policy_status", "policy_id", "policy_version", "scope",
    "policy_content_hash", "context_hash", "created_at",
)

#: Serializes the load-check-write-register sequence per process; the
#: content-addressed identity + reader-side dedupe remain the arbiters
#: across processes (same philosophy as the director's ledger lock).
_RECORD_LOCK = threading.Lock()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def consumption_artifact_bytes(event: dict) -> bytes:
    """Deterministic canonical artifact bytes for one consumption event
    (§6): the event + schema version, canonical JSON + newline."""
    payload = {"schema_version": CONSUMPTION_SCHEMA_VERSION, **event}
    return (_canonical(payload) + "\n").encode("utf-8")


def _validate_event(event: dict, run_id: str) -> None:
    if not isinstance(event, dict):
        raise PolicyError("policy_consumption_failed",
                          "consumption event must be an object")
    missing = [f for f in _EVENT_REQUIRED_FIELDS if f not in event]
    if missing:
        raise PolicyError("policy_consumption_failed",
                          "consumption event is missing required fields",
                          details={"missing": missing})
    if event["run_id"] != run_id:
        raise PolicyError(
            "policy_consumption_failed",
            "consumption event run_id does not match the target run",
            details={"event_run_id": event["run_id"], "run_id": run_id})
    if not isinstance(event["consumption_id"], str) or \
            not event["consumption_id"].startswith("cons-"):
        raise PolicyError("policy_consumption_failed",
                          "consumption event has no content-addressed id",
                          details={"consumption_id":
                                  event.get("consumption_id")})


def record_consumption_artifact(run_dir, run_id: str, event: dict) -> dict:
    """Register one consumption event as a run artifact (§5/§17/§18).

    Idempotent by ``consumption_id``: the same event re-recorded finds
    the existing artifact (same canonical bytes ⇒ same sha256) and
    registers nothing. A DIFFERENT event (e.g. a different policy
    version) is a NEW artifact ref on the SAME run. The artifact bytes
    are canonical and sha256-pinned in the ref metadata (the project's
    hashing convention — cf. lineage's ``script_sha256``).

    The run directory must already exist (created by the production
    pipeline) — evidence is never fabricated for a non-existent run.
    """
    run_dir = Path(run_dir)
    run_id = str(run_id)
    _validate_event(event, run_id)
    if not run_dir.is_dir():
        return {"ok": True, "registered": False,
                "reason": "run directory does not exist; consumption "
                          "evidence remains in the director log only",
                "run_id": run_id,
                "consumption_id": event["consumption_id"]}
    with _RECORD_LOCK:
        return _record_locked(run_dir, run_id, event)


def _record_locked(run_dir: Path, run_id: str, event: dict) -> dict:
    artifact_bytes = consumption_artifact_bytes(event)
    sha256 = _sha256_bytes(artifact_bytes)
    try:
        registry = ArtifactRegistry.load(run_dir, run_id)
    except ArtifactError as exc:
        raise PolicyError("policy_consumption_failed",
                          f"run artifact manifest unreadable: {exc}") from exc

    # idempotency: an existing registration with the SAME consumption_id
    for ref in registry.for_stage(CONSUMPTION_STAGE):
        if ref.metadata.get("consumption_id") == event["consumption_id"]:
            existing = run_dir / ref.path
            if existing.is_file() and \
                    _sha256_bytes(existing.read_bytes()) == sha256:
                return {"ok": True, "registered": False, "created": False,
                        "run_id": run_id,
                        "consumption_id": event["consumption_id"],
                        "artifact_id": ref.artifact_id,
                        "sha256": sha256}
            raise PolicyError(
                "policy_consumption_failed",
                "a consumption artifact with this id exists but its bytes "
                "differ; refusing to overwrite (fail closed)",
                details={"consumption_id": event["consumption_id"],
                         "artifact_id": ref.artifact_id})

    artifact_path = run_dir / CONSUMPTION_ARTIFACT_FILENAME
    # multiple consumption events per run (v1 → v2 → rollback) must all
    # be observable: when the default filename already holds a DIFFERENT
    # event, write a per-event filename instead of overwriting the
    # earlier immutable evidence
    if artifact_path.is_file():
        try:
            existing_event = json.loads(
                artifact_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            existing_event = None
        if existing_event is not None and \
                existing_event.get("consumption_id") != \
                event["consumption_id"]:
            artifact_path = run_dir / (
                "policy_consumption." + event["consumption_id"] + ".json")
    _atomic_write(artifact_path, artifact_bytes)

    metadata = {
        "consumption_id": event["consumption_id"],
        "run_id": run_id,
        "decision_id": event["decision_id"],
        "decision": event["decision"],
        "policy_status": event["policy_status"],
        "policy_id": event["policy_id"],
        "policy_version": event["policy_version"],
        "scope": event["scope"],
        "policy_content_hash": event["policy_content_hash"],
        "context_hash": event["context_hash"],
        "sha256": "sha256:" + sha256,
        "size": len(artifact_bytes),
        "schema_version": CONSUMPTION_SCHEMA_VERSION,
    }
    try:
        ref = registry.register(CONSUMPTION_STAGE, "policy_consumption",
                                artifact_path.relative_to(run_dir),
                                metadata=metadata)
    except (ArtifactError, ValueError) as exc:
        raise PolicyError("policy_consumption_failed",
                          f"consumption artifact could not be registered: "
                          f"{exc}") from exc
    return {"ok": True, "registered": True, "created": True,
            "run_id": run_id,
            "consumption_id": event["consumption_id"],
            "artifact_id": ref.artifact_id,
            "path": ref.path, "sha256": "sha256:" + sha256,
            "size": len(artifact_bytes)}


def _read_event_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PolicyError(
            "policy_consumption_failed",
            f"consumption artifact is malformed JSON: {path.name}",
            details={"artifact": path.name, "reason": str(exc)}) from exc
    if not isinstance(data, dict) or "consumption_id" not in data:
        raise PolicyError(
            "policy_consumption_failed",
            f"consumption artifact is not a consumption event: {path.name}")
    return data


def read_run_consumptions(run_dir, run_id: str, *, verify: bool = True) -> dict:
    """Deterministically read a run's policy consumption evidence (§14).

    - no registered consumption artifact → ``consumptions: []`` plus a
      truthful note (evidence absent ≠ policy absent);
    - hash/identity mismatch or malformed bytes → structured integrity
      failure (fail closed — corrupt evidence is never silently returned
      as if it were intact);
    - ordering: ``created_at`` then ``consumption_id`` (never filesystem
      ordering); duplicate ids (defense in depth) are deduplicated.
    """
    run_dir = Path(run_dir)
    run_id = str(run_id)
    try:
        registry = ArtifactRegistry.load(run_dir, run_id)
    except ArtifactError as exc:
        raise PolicyError("policy_consumption_failed",
                          f"run artifact manifest unreadable: {exc}") from exc
    refs = registry.for_stage(CONSUMPTION_STAGE)
    if not refs:
        return {"ok": True, "run_id": run_id, "consumptions": [],
                "note": "no policy consumption artifact is registered for "
                        "this run"}
    events = []
    for ref in sorted(refs, key=lambda r: (r.created_at, r.artifact_id)):
        artifact_path = run_dir / ref.path
        if not artifact_path.is_file():
            raise PolicyError(
                "policy_consumption_failed",
                "registered consumption artifact is missing on disk",
                details={"artifact_id": ref.artifact_id, "path": ref.path})
        raw = artifact_path.read_bytes()
        registered_hash = ref.metadata.get("sha256")
        if isinstance(registered_hash, str) and registered_hash:
            expected = registered_hash.split(":", 1)[-1]
            if _sha256_bytes(raw) != expected:
                raise PolicyError(
                    "policy_consumption_failed",
                    "consumption artifact BYTES do not match the registered "
                    "hash; the evidence was mutated after registration",
                    details={"consumption_id":
                             ref.metadata.get("consumption_id"),
                             "artifact_id": ref.artifact_id,
                             "expected_sha256": registered_hash,
                             "actual_sha256":
                             "sha256:" + _sha256_bytes(raw)})
        try:
            event = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PolicyError(
                "policy_consumption_failed",
                f"consumption artifact is malformed JSON: {ref.path}",
                details={"artifact_id": ref.artifact_id,
                         "reason": str(exc)}) from exc
        if not isinstance(event, dict) or not event.get("consumption_id"):
            raise PolicyError(
                "policy_consumption_failed",
                f"consumption artifact is not a consumption event: "
                f"{ref.path}")
        if verify:
            expected_bytes = consumption_artifact_bytes(event)
            if _sha256_bytes(expected_bytes) != _sha256_bytes(raw):
                raise PolicyError(
                    "policy_consumption_failed",
                    "consumption artifact bytes are not canonical for its "
                    "consumption_id (non-deterministic or tampered)",
                    details={"consumption_id": event["consumption_id"],
                             "artifact_id": ref.artifact_id})
        events.append(event)
    # dedupe by content-addressed id (defense in depth), then order
    # deterministically: created_at, then consumption_id (§14)
    unique: dict[str, dict] = {}
    for event in events:
        unique.setdefault(event["consumption_id"], event)
    ordered = sorted(unique.values(),
                     key=lambda e: (str(e.get("created_at") or ""),
                                    e["consumption_id"]))
    return {"ok": True, "run_id": run_id, "consumptions": ordered}


def _policy_row(policy_db_path, policy_id: str, policy_version) -> dict | None:
    """Read one exact (policy_id, version) row — READ-ONLY (mode=ro).
    Returns None when the store or the row does not exist."""
    if not policy_id or not Path(policy_db_path).is_file():
        return None
    connection = sqlite3.connect(
        f"file:{Path(policy_db_path).as_posix()}?mode=ro", uri=True,
        timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT policy_id, policy_version, scope, status, content_hash"
            " FROM policies WHERE policy_id = ? AND policy_version = ?",
            (policy_id, policy_version),
        ).fetchone()
        return dict(row) if row is not None else None
    except sqlite3.Error as exc:
        raise PolicyError("policy_read_failed",
                          f"the policy store could not be read: {exc}") from exc
    finally:
        connection.close()


def verify_consumption_history(event: dict, policy_db_path) -> dict:
    """Cross-check one consumption event against the authoritative policy
    store (§15/§16) WITHOUT requiring the policy to still be active.

    Returns a truthful verification record:

    - ``identity``: ``verified`` when the content-addressed
      consumption_id recomputes exactly from the event's own fields
      (the evidence is what it claims); ``corrupt`` otherwise — the
      event is REPORTED, never erased;
    - ``policy_state``: the CURRENT store state of the referenced
      (policy_id, version) — ``active`` / ``superseded`` / ``retired`` /
      ``missing``; ``None`` for no-policy events;
    - ``content_match``: whether the event's policy_content_hash equals
      the store's content hash for that exact version (``None`` when the
      policy is missing or the event consumed no policy).
    """
    from .execution import consumption_id
    recomputed = consumption_id(
        run_id=event.get("run_id"), decision_id=event.get("decision_id"),
        policy_id=event.get("policy_id"),
        policy_version=event.get("policy_version"),
        scope=event.get("scope"),
        policy_content_hash=event.get("policy_content_hash"),
        context_hash=event.get("context_hash"),
        decision=event.get("decision"))
    identity = ("verified" if recomputed == event.get("consumption_id")
                else "corrupt")
    policy_id = event.get("policy_id")
    policy_state = None
    content_match = None
    if policy_id is not None:
        row = _policy_row(policy_db_path, policy_id,
                          event.get("policy_version"))
        if row is None:
            policy_state = "missing"
            content_match = False
        else:
            policy_state = row["status"]  # active / superseded / retired
            content_match = (row["content_hash"] ==
                             event.get("policy_content_hash"))
    return {
        "consumption_id": event.get("consumption_id"),
        "identity": identity,
        "policy_state": policy_state,
        "content_match": content_match,
        "historical": True,  # evidence remains valid regardless of state
    }



