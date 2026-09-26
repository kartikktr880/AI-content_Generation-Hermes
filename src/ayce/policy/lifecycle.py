"""Stage 8 — Controlled policy lifecycle (§7/§11/§12/§13).

The EXPLICIT, operator-only boundaries::

    draft candidate  --approve-->  approved        (EXPLICIT operator)
    approved         --promote-->  ProductionPolicy vN (immutable)
    approved policy  --activate--> active          (EXPLICIT operator)
    superseded       --rollback--> active          (EXPLICIT operator)
    active           --retire-->   retired         (EXPLICIT operator)

Hard invariants:

- Promotion REQUIRES a recorded explicit operator approval; a draft
  candidate fails CLOSED with ``policy_approval_required``. There is NO
  automatic, scheduled, background, or Hermes-triggered path into this
  module's write operations.
- A ProductionPolicy is IMMUTABLE after creation: content columns are
  never updated; changed content is a NEW version with
  ``previous_policy_id`` / ``previous_policy_version`` bound.
- Activation is EXPLICIT and separate from existence: at most ONE active
  policy per scope (enforced by a partial UNIQUE index in the database).
- Rollback activates a previous immutable version — history is NEVER
  deleted.
- ``read_active_policy`` is the Hermes-facing surface: it opens the
  policy database in SQLite READ-ONLY mode; no write tool exists on it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .compiler import _canonical, _open_learning_readonly, build_rules, \
    validate_scope
from .errors import PolicyError

__all__ = [
    "CANDIDATE_STATUSES",
    "POLICY_STATUSES",
    "approve_candidate",
    "reject_candidate",
    "promote_candidate",
    "activate_policy",
    "rollback_policy",
    "retire_policy",
    "diff_for_activation",
    "read_active_policy",
]

CANDIDATE_STATUSES = ("draft", "approved", "promoted", "rejected")
POLICY_STATUSES = ("approved", "active", "superseded", "retired")

_MAX_VERSION_RETRIES = 5


def _policy_content(scope: str, rules, refs) -> tuple[str, str]:
    payload = {
        "scope": scope,
        "rules": sorted(rules, key=lambda r: r["rule_id"]),
        "source_knowledge_refs": sorted(
            refs, key=lambda r: (r["knowledge_id"], r["knowledge_version"])),
    }
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return "pol-" + digest[:24], "sha256:" + digest


def approve_candidate(policy_store, *, candidate_id: str,
                      approved_by: str, now: str) -> dict:
    """The EXPLICIT operator approval boundary (§7). Records WHO approved
    WHAT and WHEN; idempotent for an already-approved candidate (a
    double approval never creates duplicate records or policy versions);
    refuses rejected candidates; ``policy_already_approved`` for a
    candidate already promoted past this boundary."""
    candidate = policy_store.get_candidate(candidate_id)
    if candidate is None:
        raise PolicyError("policy_invalid_candidate",
                          f"unknown candidate: {candidate_id}")
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise PolicyError("policy_invalid_candidate",
                          "approved_by must identify the operator")
    approved_by = approved_by.strip()
    if candidate["status"] == "promoted":
        raise PolicyError(
            "policy_already_approved",
            "candidate is already promoted past the approval boundary",
            details={"candidate_id": candidate_id,
                     "policy_id": candidate.get("policy_id"),
                     "policy_version": candidate.get("policy_version")})
    if candidate["status"] == "rejected":
        raise PolicyError("policy_invalid_candidate",
                          "candidate was rejected; rejected candidates can "
                          "never be approved or promoted")
    if candidate["status"] == "approved":
        approval = policy_store.get_approval(candidate_id)
        # idempotent: the same approval boundary, re-crossed → same record
        return {"ok": True, "created": False, "already_approved": True,
                "approval": approval, "candidate": candidate}
    if not policy_store.record_approval(
            candidate_id=candidate_id, decision="approved",
            approved_by=approved_by, approved_at=now,
            candidate_status_before=candidate["status"]):
        # lost an insert race — the winner's record is authoritative
        return {"ok": True, "created": False, "already_approved": True,
                "approval": policy_store.get_approval(candidate_id),
                "candidate": policy_store.get_candidate(candidate_id)}
    if not policy_store.transition_candidate(
            candidate_id, from_status="draft", to_status="approved",
            now=now):
        # concurrent approver flipped it first — converge idempotently
        return {"ok": True, "created": False, "already_approved": True,
                "approval": policy_store.get_approval(candidate_id),
                "candidate": policy_store.get_candidate(candidate_id)}
    return {"ok": True, "created": True, "candidate":
            policy_store.get_candidate(candidate_id),
            "approval": policy_store.get_approval(candidate_id)}


def reject_candidate(policy_store, *, candidate_id: str, reason: str,
                     now: str) -> dict:
    """EXPLICIT terminal rejection of a draft candidate (a rejected
    candidate can never be approved, promoted, or activated)."""
    candidate = policy_store.get_candidate(candidate_id)
    if candidate is None:
        raise PolicyError("policy_invalid_candidate",
                          f"unknown candidate: {candidate_id}")
    if candidate["status"] != "draft":
        raise PolicyError(
            "policy_invalid_candidate",
            f"candidate is {candidate['status']!r}; only draft candidates "
            "can be rejected")
    if not policy_store.transition_candidate(
            candidate_id, from_status="draft", to_status="rejected",
            now=now):
        candidate = policy_store.get_candidate(candidate_id)
    return {"ok": True, "candidate": policy_store.get_candidate(candidate_id),
            "reason": (reason or "").strip()}


def promote_candidate(policy_store, learning_db, *, candidate_id: str,
                      now: str, dry_run: bool = False) -> dict:
    """The compile step AFTER explicit approval (§7/§11): an APPROVED
    candidate becomes an IMMUTABLE, versioned ProductionPolicy.

    - unapproved (draft) candidates fail CLOSED
      (``policy_approval_required``); rejected candidates are refused;
    - the source knowledge is RE-VALIDATED at its exact recorded version
      through a READ-ONLY learning connection before anything is written
      (superseded/changed evidence → ``policy_source_not_validated``);
    - identical content returns the SAME durable (policy_id, version) —
      concurrent promotion is arbitrated by UNIQUE constraints, and the
      version allocation retries with re-reads before reporting a
      truthful ``policy_version_conflict``.
    """
    candidate = policy_store.get_candidate(candidate_id)
    if candidate is None:
        raise PolicyError("policy_invalid_candidate",
                          f"unknown candidate: {candidate_id}")
    if candidate["status"] == "promoted":
        # idempotent: the same candidate → the SAME policy identity
        policy = policy_store.get_policy(candidate["policy_id"],
                                         candidate["policy_version"])
        return {"ok": True, "created": False, "candidate": candidate,
                "policy": policy}
    if candidate["status"] == "rejected":
        raise PolicyError("policy_invalid_candidate",
                          "rejected candidates can never be promoted")
    if candidate["status"] == "draft":
        raise PolicyError(
            "policy_approval_required",
            "candidate is NOT approved; promotion requires the explicit "
            "operator approval boundary",
            details={"candidate_id": candidate_id})

    scope = validate_scope(candidate["scope"])
    refs = candidate["source_knowledge_refs"]
    connection = _open_learning_readonly(learning_db)
    try:
        # re-compile from the CURRENT knowledge store: the exact recorded
        # versions must still be ACTIVE validated records (fail closed)
        first_rule = candidate["proposed_rules"][0]
        rules, resolved_refs = build_rules(
            connection, refs=refs, scope=scope,
            action=first_rule["action"], priority=first_rule["priority"])
    finally:
        if connection is not None:
            connection.close()
    if (sorted(rules, key=lambda r: r["rule_id"])
            != sorted(candidate["proposed_rules"],
                      key=lambda r: r["rule_id"])
            or sorted(resolved_refs, key=lambda r: (r["knowledge_id"],
                                                    r["knowledge_version"]))
            != sorted(refs, key=lambda r: (r["knowledge_id"],
                                           r["knowledge_version"]))):
        raise PolicyError(
            "policy_source_not_validated",
            "the source knowledge changed after approval; the approved "
            "candidate no longer compiles — author a new candidate",
            details={"candidate_id": candidate_id})

    policy_id, content_hash = _policy_content(scope, rules, resolved_refs)
    if dry_run:
        return _dry_run_promotion(policy_store, candidate, scope, rules,
                                  resolved_refs, policy_id, content_hash, now)
    return _insert_policy_with_retry(policy_store, candidate, scope, rules,
                                     resolved_refs, policy_id, content_hash,
                                     now)


def _dry_run_promotion(policy_store, candidate, scope, rules, refs,
                       policy_id, content_hash, now) -> dict:
    existing = policy_store.get_policy_by_content(content_hash)
    version = (existing["policy_version"] if existing is not None
               else policy_store.next_policy_version(scope))
    previous = policy_store.latest_policy(scope)
    return {"ok": True, "dry_run": True, "candidate": candidate,
            "policy": {
                "policy_id": policy_id, "policy_version": version,
                "scope": scope, "rules": rules,
                "source_knowledge_refs": refs,
                "rationale": candidate["rationale"],
                "candidate_id": candidate["candidate_id"],
                "content_hash": content_hash, "status": "approved",
                "previous_policy_id": (previous["policy_id"]
                                       if previous else None),
                "previous_policy_version": (previous["policy_version"]
                                            if previous else None),
                "created_at": now, "activated_at": None}}


def _insert_policy_with_retry(policy_store, candidate, scope, rules, refs,
                              policy_id, content_hash, now) -> dict:
    candidate_id = candidate["candidate_id"]

    def bind_existing(existing):
        policy_store.transition_candidate(
            candidate_id, from_status="approved", to_status="promoted",
            now=now, policy_id=existing["policy_id"],
            policy_version=existing["policy_version"])
        return {"ok": True, "created": False,
                "candidate": policy_store.get_candidate(candidate_id),
                "policy": existing}

    # bounded retry: concurrent promotions of DIFFERENT content in the
    # same scope race for the next version — UNIQUE(scope, policy_version)
    # is the arbiter; the loser re-reads and retries
    for _ in range(_MAX_VERSION_RETRIES):
        existing = policy_store.get_policy_by_content(content_hash)
        if existing is not None:
            return bind_existing(existing)
        version = policy_store.next_policy_version(scope)
        previous = policy_store.latest_policy(scope)
        row = {
            "policy_id": policy_id,
            "policy_version": version,
            "scope": scope,
            "rules": rules,
            "source_knowledge_refs": refs,
            "rationale": candidate["rationale"],
            "candidate_id": candidate_id,
            "content_hash": content_hash,
            "status": "approved",
            "previous_policy_id": (previous["policy_id"]
                                   if previous else None),
            "previous_policy_version": (previous["policy_version"]
                                        if previous else None),
            "created_at": now,
            "activated_at": None,
        }
        if policy_store.insert_policy(row=row):
            policy_store.transition_candidate(
                candidate_id, from_status="approved", to_status="promoted",
                now=now, policy_id=policy_id, policy_version=version)
            return {"ok": True, "created": True,
                    "candidate": policy_store.get_candidate(candidate_id),
                    "policy": policy_store.get_policy(policy_id, version)}
    raise PolicyError(
        "policy_version_conflict",
        "could not allocate a policy version after retries",
        details={"scope": scope, "policy_id": policy_id})


def _set_active(policy_store, policy, *, action: str, now: str) -> dict:
    """Shared explicit activation machinery: ONE atomic swap (supersede
    the currently active policy of the scope + activate the target) and
    an appended audit record. The partial UNIQUE index guarantees at
    most ONE active policy per scope at the database level."""
    if not policy_store.activate_swap(
            policy_id=policy["policy_id"],
            policy_version=policy["policy_version"],
            scope=policy["scope"], now=now):
        # re-read: a concurrent operator already moved this policy
        policy = policy_store.get_policy(policy["policy_id"],
                                         policy["policy_version"])
        if policy["status"] == "active":
            return {"ok": True, "activated": False, "action": action,
                    "policy": policy}
        raise PolicyError(
            "policy_activation_conflict",
            f"policy is {policy['status']!r} and cannot be activated",
            details={"policy_id": policy["policy_id"],
                     "policy_version": policy["policy_version"]})
    stored = policy_store.get_policy(policy["policy_id"],
                                     policy["policy_version"])
    activation = {
        "activation_id": "act-" + hashlib.sha256(_canonical({
            "policy_id": policy["policy_id"],
            "policy_version": policy["policy_version"],
            "action": action, "performed_at": now,
        }).encode("utf-8")).hexdigest()[:24],
        "policy_id": policy["policy_id"],
        "policy_version": policy["policy_version"],
        "action": action,
        "previous_policy_id": stored.get("previous_policy_id"),
        "previous_policy_version": stored.get("previous_policy_version"),
        "performed_at": now,
    }
    policy_store.record_activation(row=activation)
    return {"ok": True, "activated": True, "action": action,
            "activation_id": activation["activation_id"],
            "policy": policy_store.get_policy(policy["policy_id"],
                                              policy["policy_version"])}


def activate_policy(policy_store, *, policy_id: str,
                    policy_version: int | None = None, now: str) -> dict:
    """EXPLICIT activation (§12): an APPROVED policy becomes THE active
    policy of its scope. Idempotent for an already-active policy.
    Superseded versions must be re-activated through the explicit
    rollback boundary; retired policies can never be activated."""
    policy = policy_store.get_policy(policy_id, policy_version)
    if policy is None:
        if policy_version is not None and \
                policy_store.get_policy(policy_id) is not None:
            raise PolicyError("policy_version_conflict",
                              f"policy {policy_id} has no version "
                              f"{policy_version}",
                              details={"policy_id": policy_id,
                                       "policy_version": policy_version})
        raise PolicyError("policy_activation_conflict",
                          f"unknown policy: {policy_id}"
                          + (f" v{policy_version}" if policy_version else ""),
                          details={"policy_id": policy_id,
                                   "policy_version": policy_version})
    if policy["status"] == "active":
        return {"ok": True, "activated": False, "action": "activate",
                "policy": policy}
    if policy["status"] == "superseded":
        raise PolicyError(
            "policy_activation_conflict",
            "policy is superseded; re-activating a previously active "
            "version is the explicit ROLLBACK boundary",
            details={"policy_id": policy_id,
                     "policy_version": policy["policy_version"]})
    if policy["status"] != "approved":
        raise PolicyError("policy_activation_conflict",
                          f"policy is {policy['status']!r} and cannot be "
                          "activated",
                          details={"policy_id": policy_id,
                                   "policy_version":
                                   policy["policy_version"]})
    return _set_active(policy_store, policy, action="activate", now=now)


def rollback_policy(policy_store, *, policy_id: str,
                    policy_version: int | None = None, now: str) -> dict:
    """EXPLICIT rollback (§12): re-activate a previously-active
    (superseded) immutable version. History is NEVER deleted; the
    rollback itself is an audited activation record."""
    policy = policy_store.get_policy(policy_id, policy_version)
    if policy is None:
        raise PolicyError("policy_rollback_invalid",
                          f"unknown policy: {policy_id}"
                          + (f" v{policy_version}" if policy_version else ""),
                          details={"policy_id": policy_id,
                                   "policy_version": policy_version})
    if policy["status"] == "active":
        raise PolicyError("policy_rollback_invalid",
                          "policy is already active; there is nothing to "
                          "roll back to",
                          details={"policy_id": policy_id,
                                   "policy_version":
                                   policy["policy_version"]})
    if policy["status"] != "superseded":
        raise PolicyError(
            "policy_rollback_invalid",
            f"policy is {policy['status']!r}; only a previously-ACTIVE "
            "(superseded) version can be rolled back to",
            details={"policy_id": policy_id,
                     "policy_version": policy["policy_version"]})
    return _set_active(policy_store, policy, action="rollback", now=now)


def retire_policy(policy_store, *, policy_id: str,
                  policy_version: int | None = None, now: str) -> dict:
    """EXPLICIT retirement of the ACTIVE policy (terminal: a retired
    policy can never be re-activated)."""
    policy = policy_store.get_policy(policy_id, policy_version)
    if policy is None:
        raise PolicyError("policy_activation_conflict",
                          f"unknown policy: {policy_id}",
                          details={"policy_id": policy_id})
    if policy["status"] != "active":
        raise PolicyError("policy_not_active",
                          f"policy is {policy['status']!r}; only the ACTIVE "
                          "policy can be retired",
                          details={"policy_id": policy_id,
                                   "policy_version":
                                   policy["policy_version"]})
    if not policy_store.transition_policy(
            policy["policy_id"], policy["policy_version"],
            from_statuses=("active",), to_status="retired"):
        policy = policy_store.get_policy(policy["policy_id"],
                                         policy["policy_version"])
    return {"ok": True, "policy": policy_store.get_policy(
        policy["policy_id"], policy["policy_version"])}


def _diff_rules(baseline_rules, target_rules) -> dict:
    """Deterministic rule-set diff (added / removed / changed / unchanged),
    ordered by rule_id (§13)."""
    baseline = {r["rule_id"]: r for r in baseline_rules}
    target = {r["rule_id"]: r for r in target_rules}
    added = [target[k] for k in sorted(set(target) - set(baseline))]
    removed = [baseline[k] for k in sorted(set(baseline) - set(target))]
    changed = []
    for key in sorted(set(baseline) & set(target)):
        fields = {
            field: {"from": baseline[key].get(field),
                    "to": target[key].get(field)}
            for field in sorted(set(baseline[key]) | set(target[key]))
            if baseline[key].get(field) != target[key].get(field)
        }
        if fields:
            changed.append({"rule_id": key, "fields": fields})
    changed_ids = {c["rule_id"] for c in changed}
    unchanged = sorted(set(baseline) & set(target) - changed_ids)
    return {"added": added, "removed": removed, "changed": changed,
            "unchanged_rule_ids": unchanged}


def _diff_refs(baseline_refs, target_refs) -> dict:
    def key(ref):
        return (ref["knowledge_id"], ref["knowledge_version"])

    baseline = {key(r) for r in baseline_refs}
    target = {key(r) for r in target_refs}
    return {
        "added": [{"knowledge_id": k, "knowledge_version": v}
                  for k, v in sorted(target - baseline)],
        "removed": [{"knowledge_id": k, "knowledge_version": v}
                    for k, v in sorted(baseline - target)],
        "unchanged": [{"knowledge_id": k, "knowledge_version": v}
                      for k, v in sorted(baseline & target)],
    }


def diff_for_activation(policy_store, *, candidate_id: str | None = None,
                        policy_id: str | None = None,
                        policy_version: int | None = None) -> dict:
    """Deterministic, auditable policy diff BEFORE activation (§13):
    the target (candidate or policy) versus the CURRENTLY ACTIVE policy
    of its scope. An absent active policy is reported EXPLICITLY."""
    if candidate_id is not None:
        target = policy_store.get_candidate(candidate_id)
        if target is None:
            raise PolicyError("policy_invalid_candidate",
                              f"unknown candidate: {candidate_id}")
        rules = target["proposed_rules"]
        refs = target["source_knowledge_refs"]
        scope = target["scope"]
        target_desc = {"kind": "candidate", "candidate_id": candidate_id,
                       "status": target["status"],
                       "content_hash": target["content_hash"]}
    elif policy_id is not None:
        target = policy_store.get_policy(policy_id, policy_version)
        if target is None:
            raise PolicyError("policy_activation_conflict",
                              f"unknown policy: {policy_id}")
        rules = target["rules"]
        refs = target["source_knowledge_refs"]
        scope = target["scope"]
        target_desc = {"kind": "policy", "policy_id": policy_id,
                       "policy_version": target["policy_version"],
                       "status": target["status"],
                       "content_hash": target["content_hash"]}
    else:
        raise PolicyError("policy_invalid_candidate",
                          "diff needs --candidate-id or --policy-id")

    baseline = policy_store.active_policy(scope)
    diff = _diff_rules(baseline["rules"] if baseline else [], rules)
    refs_diff = _diff_refs(baseline["source_knowledge_refs"] if baseline
                           else [], refs)
    return {
        "ok": True,
        "scope": scope,
        "target": target_desc,
        "baseline": ({"policy_id": baseline["policy_id"],
                      "policy_version": baseline["policy_version"],
                      "content_hash": baseline["content_hash"],
                      "status": baseline["status"],
                      "activated_at": baseline["activated_at"]}
                     if baseline else None),
        "baseline_note": ("no active policy in this scope — activating "
                          "would be the FIRST policy"
                          if baseline is None else
                          "diff against the currently active policy"),
        "rules": diff,
        "source_knowledge": refs_diff,
        "material_change": bool(diff["added"] or diff["removed"]
                                or diff["changed"]
                                or refs_diff["added"]
                                or refs_diff["removed"]),
    }


# ---- Hermes-facing READ-ONLY surface (§14) ---------------------------------


def read_active_policy(policy_db_path, scope: str) -> dict:
    """The Hermes-facing READ-ONLY query (§14): the ACTIVE
    ProductionPolicy for one exact scope.

    Opens the policy database in SQLite read-only mode (URI
    ``mode=ro``) — no write path exists on this connection. A missing
    database or an inactive scope is reported truthfully.
    """
    scope = validate_scope(scope)
    path = Path(policy_db_path)
    if not path.is_file():
        return {"ok": True, "scope": scope, "policy": None,
                "note": "no policy database exists yet"}
    import sqlite3
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM policies WHERE scope = ? AND status = 'active'",
            (scope,),
        ).fetchone()
        if row is None:
            return {"ok": True, "scope": scope, "policy": None,
                    "note": f"no active policy for scope {scope!r}"}
        data = dict(row)
        try:
            data["rules"] = json.loads(data["rules"])
            data["source_knowledge_refs"] = json.loads(
                data["source_knowledge_refs"])
        except (ValueError, TypeError):
            pass
        return {"ok": True, "scope": scope, "policy": data}
    finally:
        connection.close()







