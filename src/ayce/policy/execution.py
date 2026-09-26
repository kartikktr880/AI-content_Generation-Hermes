"""Stage 9 — Policy-aware Hermes execution context (read-only).

The canonical bridge between the Stage 8 ProductionPolicy store and the
Hermes director::

    Active ProductionPolicy (immutable, operator-controlled)
        ↓  build_execution_context   — READ-ONLY (SQLite mode=ro)
    Policy Execution Context (structured, deterministic, hashed)
        ↓  director decision (trigger_golden_path)
    Policy Consumption Evidence (append-only, content-addressed)

Hard invariants:

- This module is READ-ONLY over the policy store: it opens SQLite in
  ``mode=ro`` (via the Stage 8 ``read_active_policy`` boundary) and has
  NO write path, NO approval path, NO activation path.
- The director NEVER opens a writable policy connection, NEVER issues
  policy INSERT/UPDATE/DELETE, and NEVER constructs policy versions.
- ``no policy`` is a NORMAL, truthful state (``policy_status: "none"``)
  — nothing is fabricated.
- A corrupted / unverifiable active policy FAILS CLOSED
  (``policy_context_invalid``): it is never silently treated as
  "no policy".

Scope resolution precedence (explicit, deterministic — §6/§22):

1. The caller supplies an ORDERED list of scope candidates
   (most specific first); the FIRST candidate with an active policy
   wins. The director's default candidate list is ``("global",)``;
   operators may configure it (AYCE_POLICY_SCOPE_CANDIDATES).
2. A narrower scoped policy is preferred over ``global`` ONLY because
   the caller's candidate order says so — the resolution never invents
   or widens scopes, and never merges policies.
3. At most ONE policy is consumed per decision. Overlapping active
   scopes are resolved by the candidate order — never merged, never
   arbitrarily chosen. An unsupported scope (grammar violation) raises
   ``policy_scope_invalid``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from .compiler import validate_rule, validate_scope
from .errors import PolicyError
from .lifecycle import _policy_content, read_active_policy

__all__ = [
    "CONTEXT_SCHEMA_VERSION",
    "DEFAULT_SCOPE_CANDIDATES",
    "SUPPORTED_RULE_KINDS",
    "build_execution_context",
    "resolve_active_policy",
    "verify_active_policy",
    "preferred_variant",
    "apply_policy_preference",
    "consumption_id",
    "append_consumption",
    "read_consumptions",
]

#: The execution-context schema version (bumped only on a breaking change).
CONTEXT_SCHEMA_VERSION = 1

#: Stage 8's supported rule kinds — the context refuses anything else.
SUPPORTED_RULE_KINDS = ("variant_preference_v1",)

#: The director's default scope resolution order (most specific first).
DEFAULT_SCOPE_CANDIDATES = ("global",)

_CONSUMPTION_IO_LOCK = threading.Lock()


def _canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(payload) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def resolve_active_policy(policy_db_path,
                          scope_candidates=DEFAULT_SCOPE_CANDIDATES) -> dict:
    """Deterministic scope resolution (§6/§22): the FIRST candidate (in
    the caller's most-specific-first order) that has an ACTIVE policy
    wins. Returns a structured resolution record; ``policy_status`` is
    ``"none"`` when no candidate matches — a normal, truthful state."""
    if isinstance(scope_candidates, str):
        scope_candidates = (scope_candidates,)
    if not scope_candidates:
        raise PolicyError("policy_scope_unresolved",
                          "no scope candidates supplied for policy resolution")
    tried = []
    for candidate in scope_candidates:
        scope = validate_scope(candidate)  # policy_scope_invalid on garbage
        tried.append(scope)
        try:
            result = read_active_policy(policy_db_path, scope)
        except sqlite3.Error as exc:
            raise PolicyError(
                "policy_read_failed",
                f"the active policy for scope {scope!r} could not be read: "
                f"{exc}", details={"scope": scope}) from exc
        policy = result.get("policy")
        if policy is not None:
            # fail closed at the READ boundary too (§23): a corrupted or
            # unverifiable active policy is never returned as consumable
            verify_active_policy(policy)
            return {"policy_status": "active", "scope": scope,
                    "policy": policy, "scope_candidates": list(tried)}
    return {"policy_status": "none", "scope": None, "policy": None,
            "scope_candidates": list(tried)}


def verify_active_policy(policy: dict) -> None:
    """Fail-closed verification of one active policy row (§23): the
    content hash must match a recomputation over the stored content, and
    every rule must satisfy the Stage 8 schema. A corrupted or
    unverifiable policy is NEVER silently treated as 'no policy'."""
    try:
        scope = policy["scope"]
        rules = policy["rules"]
        refs = policy["source_knowledge_refs"]
        stored_hash = policy["content_hash"]
    except (KeyError, TypeError) as exc:
        raise PolicyError("policy_context_invalid",
                          "active policy row is missing required fields",
                          details={"missing": str(exc)}) from exc
    if not isinstance(rules, list) or not rules:
        raise PolicyError("policy_context_invalid",
                          "active policy has no readable rules",
                          details={"policy_id": policy.get("policy_id")})
    try:
        expected_id, expected_hash = _policy_content(scope, rules, refs)
    except (TypeError, ValueError) as exc:
        raise PolicyError("policy_context_invalid",
                          "active policy content is not canonicalizable",
                          details={"policy_id": policy.get("policy_id")}) from exc
    if expected_hash != stored_hash or expected_id != policy.get("policy_id"):
        raise PolicyError(
            "policy_context_invalid",
            "active policy content hash verification FAILED; refusing to "
            "consume an unverifiable policy (fail closed)",
            details={"policy_id": policy.get("policy_id"),
                     "expected_content_hash": expected_hash,
                     "stored_content_hash": stored_hash})
    for rule in rules:
        try:
            validate_rule(rule)
        except PolicyError as exc:
            raise PolicyError(
                "policy_context_invalid",
                f"active policy rule failed schema validation: {exc.message}",
                details={"policy_id": policy.get("policy_id"),
                         "rule_id": rule.get("rule_id")
                         if isinstance(rule, dict) else None}) from exc
        if rule["kind"] not in SUPPORTED_RULE_KINDS:
            raise PolicyError(
                "policy_context_invalid",
                f"unsupported rule kind {rule['kind']!r} in the active "
                "policy; the execution context refuses semantics it does "
                "not implement",
                details={"policy_id": policy.get("policy_id"),
                         "rule_id": rule["rule_id"]})


def build_execution_context(policy_db_path,
                            scope_candidates=DEFAULT_SCOPE_CANDIDATES) -> dict:
    """Build the STRUCTURED policy execution context supplied to the
    director (§5): only what the director needs — no database internals,
    no timestamps beyond the policy's own identity, no free-text
    knowledge statements (the rationale is deliberately EXCLUDED so it
    can never become uncontrolled prompt text, §21).

    Deterministic: the same store state + candidates produce the same
    context and the same ``context_hash``.

    Fail-closed: a corrupted/unverifiable active policy raises
    ``policy_context_invalid`` (never silently 'no policy'); an
    unreadable store raises ``policy_read_failed``.
    """
    resolution = resolve_active_policy(policy_db_path, scope_candidates)
    policy_row = resolution["policy"]
    if policy_row is not None:
        verify_active_policy(policy_row)
        context_policy = {
            "policy_id": policy_row["policy_id"],
            "policy_version": policy_row["policy_version"],
            "scope": policy_row["scope"],
            "rules": policy_row["rules"],
            "source_knowledge_refs": policy_row["source_knowledge_refs"],
            "policy_content_hash": policy_row["content_hash"],
        }
    else:
        context_policy = None
    context = {
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "policy_status": resolution["policy_status"],
        "policy": context_policy,
        "resolved": {
            "scope_candidates": resolution["scope_candidates"],
            "resolved_scope": resolution["scope"],
        },
    }
    # canonical, deterministic hash of the EXACT context Hermes receives
    context["context_hash"] = "sha256:" + _sha256_hex(context)
    return context


def preferred_variant(context: dict) -> str | None:
    """The variant preferred by the active policy's supported rules —
    the HIGHEST-priority (lowest number) ``prefer_variant`` rule — or
    None. This is a PREFERENCE SIGNAL (Stage 8 vocabulary); it is never
    a requirement, never a guarantee, and never a causal claim."""
    if not isinstance(context, dict) or \
            context.get("policy_status") != "active":
        return None
    policy = context.get("policy") or {}
    best = None
    for rule in policy.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        if rule.get("kind") not in SUPPORTED_RULE_KINDS or \
                rule.get("operator") != "prefer_variant" or \
                rule.get("action") != "prefer":
            continue
        priority = rule.get("priority")
        if not isinstance(priority, int) or isinstance(priority, bool):
            continue
        if best is None or priority < best["priority"]:
            best = rule
    return best["variant"] if best is not None else None


def apply_policy_preference(context: dict, *, variants) -> dict:
    """The ONE explicit policy-aware decision helper (§9/§12): given the
    candidate variants of a director decision, returns the policy-aware
    selection. HARD RULES:

    - ``prefer_variant`` is a PREFERENCE: when the preferred variant is
      among the candidates it is selected with a factual, policy-
      referencing explanation; when it is NOT, the decision is returned
      UNCHANGED (a preference never becomes a requirement).
    - no active policy → the decision is returned unchanged, truthfully.
    - a policy with unsupported semantics never reaches this function
      (the context builder refuses it).
    """
    if not isinstance(context, dict) or \
            context.get("policy_status") not in ("active", "none"):
        raise PolicyError("policy_decision_invalid",
                          "decision input must carry a valid policy "
                          "execution context")
    variants = list(variants or [])
    preferred = preferred_variant(context)
    policy = context.get("policy") or {}
    if context.get("policy_status") != "active" or preferred is None:
        return {
            "policy_applied": False,
            "selected": None,
            "reason": "no active production policy preference; decision "
                      "unchanged",
            "policy": None,
        }
    if preferred not in variants:
        return {
            "policy_applied": False,
            "selected": None,
            "reason": "the active policy prefers a variant that is not "
                      "among the decision candidates; preference NOT "
                      "applied (a preference is never a requirement)",
            "policy": {"policy_id": policy.get("policy_id"),
                       "policy_version": policy.get("policy_version"),
                       "scope": policy.get("scope"),
                       "rule_kind": SUPPORTED_RULE_KINDS[0]},
        }
    return {
        "policy_applied": True,
        "selected": preferred,
        "reason": "selected according to active production policy "
                  "preference",
        "policy": {"policy_id": policy.get("policy_id"),
                   "policy_version": policy.get("policy_version"),
                   "scope": policy.get("scope"),
                   "rule_kind": SUPPORTED_RULE_KINDS[0]},
    }


# ---- policy consumption evidence (§13/§15/§16) ------------------------------


def consumption_id(*, run_id, decision_id, policy_id, policy_version,
                   scope, policy_content_hash, context_hash,
                   decision) -> str:
    """Content-based, deterministic event identity (§15): the same
    decision consuming the same policy produces the SAME id (idempotent);
    a different policy version / context produces a DIFFERENT id."""
    return "cons-" + _sha256_hex({
        "run_id": run_id,
        "decision_id": decision_id,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "scope": scope,
        "policy_content_hash": policy_content_hash,
        "context_hash": context_hash,
        "decision": decision,
    })[:24]


def append_consumption(events_path, *, run_id, decision_id, decision,
                       context, now) -> dict:
    """Append ONE policy consumption event to the append-only evidence
    log (JSONL). Idempotent by content-based ``consumption_id``: a
    replayed decision does NOT create a duplicate event. A no-policy
    decision is recorded TRUTHFULLY (``policy_status: 'none'``, no
    invented policy id). This log NEVER mutates ProductionPolicy state.
    """
    if not isinstance(context, dict) or "context_hash" not in context:
        raise PolicyError("policy_decision_invalid",
                          "consumption requires a valid policy execution "
                          "context (with context_hash)")
    policy = context.get("policy") or {}
    event = {
        "consumption_id": consumption_id(
            run_id=run_id, decision_id=decision_id,
            policy_id=policy.get("policy_id"),
            policy_version=policy.get("policy_version"),
            scope=policy.get("scope"),
            policy_content_hash=policy.get("policy_content_hash"),
            context_hash=context.get("context_hash"),
            decision=decision),
        "run_id": run_id,
        "decision_id": decision_id,
        "decision": decision,
        "policy_status": context.get("policy_status"),
        "policy_id": policy.get("policy_id"),
        "policy_version": policy.get("policy_version"),
        "scope": policy.get("scope"),
        "policy_content_hash": policy.get("policy_content_hash"),
        "context_hash": context.get("context_hash"),
        "resolved_scope_candidates": (context.get("resolved") or {}).get(
            "scope_candidates"),
        "created_at": now,
    }
    path = Path(events_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _CONSUMPTION_IO_LOCK:
        existing_ids = set()
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        existing_ids.add(json.loads(line)["consumption_id"])
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue  # a corrupt line never blocks the log
            except OSError as exc:
                raise PolicyError("policy_consumption_failed",
                                  f"consumption log unreadable: {exc}") from exc
        if event["consumption_id"] in existing_ids:
            return {"ok": True, "created": False,
                    "consumption_id": event["consumption_id"],
                    "event": event}
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(_canonical(event) + "\n")
        except OSError as exc:
            raise PolicyError("policy_consumption_failed",
                              f"consumption event could not be appended: "
                              f"{exc}") from exc
    return {"ok": True, "created": True,
            "consumption_id": event["consumption_id"], "event": event}


def read_consumptions(events_path) -> list[dict]:
    """Read the append-only consumption log (oldest first). A missing
    file is a truthful empty log; corrupt lines are skipped, never
    invented."""
    path = Path(events_path)
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


