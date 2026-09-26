"""Stage 8 — Deterministic policy compiler (§9).

The ONLY transformation paths into the policy domain::

    Validated Knowledge  →  PolicyCandidate      (create_candidate)
    PolicyCandidate      →  ProductionPolicy     (lifecycle.promote_candidate,
                                                   after EXPLICIT approval)

The compiler validates EVERYTHING and fails closed:

- the source knowledge must be an ACTIVE validated record at an EXPLICIT
  version (rejected / insufficient / superseded evidence can never
  become policy — ``policy_source_not_validated``);
- the candidate scope must EQUAL the knowledge scope (a narrower
  validated scope is never silently broadened, and ``global`` is never
  derived from a scoped record — ``policy_scope_invalid``);
- the rule schema is a SMALL explicit vocabulary grounded in what Stage 7
  knowledge can legitimately support: a validated, holdout-agreeing
  POSITIVE directional effect may — through an explicit operator
  decision — become a ``prefer_variant`` rule for the experiment's
  treatment variant within the experiment's scope. Threshold semantics
  (``greater_than`` + value) are NOT supported by associational
  knowledge and are REJECTED (``policy_rule_invalid`` /
  ``policy_unsupported``);
- conflicting rules (same scope + metric, different preferred variant)
  are refused (``policy_conflict``).

Identity is content-addressed and deterministic: identical inputs
produce identical candidate ids and identical content hashes.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from .errors import PolicyError

__all__ = [
    "GLOBAL_SCOPE",
    "SCOPE_DIMENSIONS",
    "RULE_KIND",
    "RULE_OPERATOR",
    "SUPPORTED_ACTIONS",
    "validate_scope",
    "parse_knowledge_ref",
    "validate_rule",
    "build_rules",
    "create_candidate",
]

#: Explicit scope grammar (§10): ONLY dimensions the existing lineage /
#: data can reliably identify, plus the explicit global scope.
GLOBAL_SCOPE = "global"
SCOPE_DIMENSIONS = ("channel", "content_type", "format", "topic", "series")
_SCOPE_RE = re.compile(r"^([a-z][a-z0-9_]{0,31}):([A-Za-z0-9][A-Za-z0-9._-]{0,63})$")
_KNOWLEDGE_ID_RE = re.compile(r"^knw-[A-Za-z0-9._-]{1,64}$")

#: The SMALL supported policy rule vocabulary (§8/§9).
RULE_KIND = "variant_preference_v1"
RULE_OPERATOR = "prefer_variant"
SUPPORTED_ACTIONS = ("prefer",)

_RULE_KEYS = frozenset({
    "rule_id", "kind", "metric", "operator", "variant",
    "comparator_variant", "scope", "action", "priority", "source",
})
_RULE_ID_RE = re.compile(r"^rul-[0-9a-f]{16}$")


def _canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _sha256_hex(payload) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def validate_scope(scope: str) -> str:
    """Validate the explicit policy scope grammar. Returns the stripped
    scope, or raises ``policy_scope_invalid``."""
    if not isinstance(scope, str):
        raise PolicyError("policy_scope_invalid",
                          "scope must be a string",
                          details={"scope": scope})
    scope = scope.strip()
    if scope == GLOBAL_SCOPE:
        return scope
    match = _SCOPE_RE.fullmatch(scope)
    if match is None or match.group(1) not in SCOPE_DIMENSIONS:
        raise PolicyError(
            "policy_scope_invalid",
            f"invalid policy scope: {scope!r}; expected 'global' or "
            f"'<dimension>:<value>' with dimension in "
            f"{list(SCOPE_DIMENSIONS)}",
            details={"scope": scope})
    return scope


def parse_knowledge_ref(text: str) -> dict:
    """Parse one ``knw-…[@version]`` reference (explicit version is
    strongly recommended; a bare id resolves the latest ACTIVE version
    at compile time — deterministically)."""
    if not isinstance(text, str):
        raise PolicyError("policy_invalid_candidate",
                          f"knowledge reference must be a string: {text!r}")
    ref = text.strip()
    knowledge_id, _, version_part = ref.partition("@")
    knowledge_id = knowledge_id.strip()
    if not _KNOWLEDGE_ID_RE.match(knowledge_id):
        raise PolicyError("policy_invalid_candidate",
                          f"invalid knowledge id: {knowledge_id!r} "
                          "(expected knw-…)")
    if not version_part:
        return {"knowledge_id": knowledge_id, "knowledge_version": None}
    if not version_part.isdigit() or int(version_part) < 1:
        raise PolicyError("policy_invalid_candidate",
                          f"invalid knowledge version: {version_part!r}")
    return {"knowledge_id": knowledge_id,
            "knowledge_version": int(version_part)}


def validate_rule(rule: dict) -> dict:
    """Strict rule-schema validation (§9). Unknown semantics — e.g.
    threshold operators the associational knowledge cannot support —
    are REJECTED, never silently coerced."""
    if not isinstance(rule, dict):
        raise PolicyError("policy_rule_invalid", "rule must be an object")
    extra = sorted(set(rule) - _RULE_KEYS)
    missing = sorted(_RULE_KEYS - set(rule))
    if extra or missing:
        raise PolicyError("policy_rule_invalid",
                          "rule schema violation",
                          details={"unknown_fields": extra,
                                   "missing_fields": missing})
    if rule["kind"] != RULE_KIND:
        raise PolicyError("policy_rule_invalid",
                          f"unsupported rule kind: {rule['kind']!r} "
                          f"(supported: {RULE_KIND!r})")
    if rule["operator"] != RULE_OPERATOR:
        raise PolicyError(
            "policy_rule_invalid",
            f"unsupported rule operator: {rule['operator']!r}; associational "
            f"validated knowledge cannot establish thresholds — supported "
            f"operator: {RULE_OPERATOR!r}",
            details={"operator": rule["operator"]})
    if rule["action"] not in SUPPORTED_ACTIONS:
        raise PolicyError("policy_rule_invalid",
                          f"unsupported rule action: {rule['action']!r} "
                          f"(supported: {list(SUPPORTED_ACTIONS)})")
    if not isinstance(rule["metric"], str) or not rule["metric"].strip():
        raise PolicyError("policy_rule_invalid",
                          "rule metric must be a non-empty string")
    variant = rule["variant"]
    comparator = rule["comparator_variant"]
    for name in (variant, comparator):
        if not isinstance(name, str) or not name.strip():
            raise PolicyError("policy_rule_invalid",
                              "rule variants must be non-empty strings")
    if variant == comparator:
        raise PolicyError("policy_rule_invalid",
                          "rule variant and comparator_variant must differ")
    if not isinstance(rule["priority"], int) or \
            isinstance(rule["priority"], bool) or \
            not 1 <= rule["priority"] <= 100:
        raise PolicyError("policy_rule_invalid",
                          "rule priority must be an integer in 1..100")
    validate_scope(rule["scope"])
    source = rule["source"]
    if not isinstance(source, dict) or \
            not isinstance(source.get("knowledge_id"), str) or \
            not isinstance(source.get("knowledge_version"), int):
        raise PolicyError("policy_rule_invalid",
                          "rule source must reference explicit knowledge "
                          "id + version")
    if not isinstance(rule["rule_id"], str) or \
            not _RULE_ID_RE.match(rule["rule_id"]):
        raise PolicyError("policy_rule_invalid",
                          "rule_id must be a deterministic rul-… id")
    return rule


# ---- learning-store read-only access (mode=ro; NEVER a write path) ---------


def _open_learning_readonly(learning_db):
    """Open the learning database in SQLite READ-ONLY mode (URI
    ``mode=ro``) — the same proven pattern as the Hermes-facing
    ``read_validated_knowledge`` (§27). The policy layer NEVER opens a
    read-write connection to the learning store."""
    from pathlib import Path
    path = Path(learning_db)
    if not path.is_file():
        return None
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    return connection


def _load_knowledge(connection, *, knowledge_id: str,
                    version: int | None) -> dict | None:
    if connection is None:
        return None
    if version is None:
        row = connection.execute(
            "SELECT * FROM validated_knowledge WHERE knowledge_id = ?"
            " AND status = 'active' ORDER BY knowledge_version DESC"
            " LIMIT 1", (knowledge_id,),
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM validated_knowledge WHERE knowledge_id = ?"
            " AND knowledge_version = ?",
            (knowledge_id, version),
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    try:
        data["evidence"] = json.loads(data["evidence"])
        data["validation"] = json.loads(data["validation"])
    except (ValueError, TypeError):
        pass
    return data


def _load_experiment_definition(connection, experiment_id: str) -> dict | None:
    row = connection.execute(
        "SELECT definition FROM experiments WHERE experiment_id = ?",
        (experiment_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["definition"])
    except (ValueError, TypeError):
        return None


def _rule_from_knowledge(connection, knowledge: dict, *, scope: str,
                         action: str, priority: int) -> dict:
    """Deterministically compile ONE validated knowledge record into a
    ``prefer_variant`` rule — or refuse truthfully (§8/§9)."""
    validation = knowledge.get("validation") or {}
    if knowledge.get("status") != "active":
        raise PolicyError(
            "policy_source_not_validated",
            f"knowledge {knowledge['knowledge_id']} "
            f"v{knowledge['knowledge_version']} is not active "
            f"(status: {knowledge.get('status')}); only ACTIVE validated "
            "knowledge can become policy",
            details={"knowledge_id": knowledge["knowledge_id"],
                     "knowledge_version": knowledge["knowledge_version"],
                     "status": knowledge.get("status")})
    if validation.get("evaluation_status") != "validated":
        raise PolicyError(
            "policy_source_not_validated",
            "knowledge is not backed by a validated evaluation",
            details={"evaluation_status":
                     validation.get("evaluation_status")})
    if scope != knowledge.get("scope"):
        raise PolicyError(
            "policy_scope_invalid",
            "candidate scope must EQUAL the validated knowledge scope "
            "(a validated scope is never silently broadened or narrowed)",
            details={"candidate_scope": scope,
                     "knowledge_scope": knowledge.get("scope")})
    effect = validation.get("effect")
    if effect is None or effect <= 0:
        raise PolicyError(
            "policy_unsupported",
            "the validated knowledge does not support a 'prefer' rule: "
            "the observed effect is not positive",
            details={"effect": effect})
    holdout = validation.get("holdout") or {}
    holdout_effect = holdout.get("effect")
    if holdout_effect is not None and (holdout_effect > 0) != (effect > 0):
        raise PolicyError(
            "policy_unsupported",
            "the holdout validation contradicts the development effect; "
            "the knowledge cannot support a prefer rule",
            details={"effect": effect, "holdout_effect": holdout_effect})
    definition = _load_experiment_definition(
        connection, knowledge.get("experiment_id", ""))
    if definition is None:
        raise PolicyError(
            "policy_source_missing",
            "the experiment definition behind this knowledge record could "
            "not be read; refusing to invent rule semantics",
            details={"experiment_id": knowledge.get("experiment_id")})
    rule = {
        "rule_id": "rul-" + _sha256_hex({
            "kind": RULE_KIND, "scope": scope,
            "metric": definition["metric"],
            "variant": definition["treatment_variant"],
            "comparator_variant": definition["control_variant"],
            "source": {"knowledge_id": knowledge["knowledge_id"],
                       "knowledge_version": knowledge["knowledge_version"]},
            "action": action,
        })[:16],
        "kind": RULE_KIND,
        "metric": definition["metric"],
        "operator": RULE_OPERATOR,
        "variant": definition["treatment_variant"],
        "comparator_variant": definition["control_variant"],
        "scope": scope,
        "action": action,
        "priority": priority,
        "source": {"knowledge_id": knowledge["knowledge_id"],
                   "knowledge_version": knowledge["knowledge_version"]},
    }
    return validate_rule(rule)


def build_rules(connection, *, refs: list[dict], scope: str,
                action: str, priority: int) -> tuple[list[dict], list[dict]]:
    """Compile ALL referenced knowledge records into rules. Returns
    ``(rules, resolved_refs)``. Raises structured PolicyErrors on any
    missing/unvalidated/unsupported/conflicting compilation."""
    seen = set()
    rules = []
    resolved_refs = []
    for ref in refs:
        key = (ref["knowledge_id"], ref["knowledge_version"])
        if key in seen:
            raise PolicyError(
                "policy_invalid_candidate",
                f"duplicate knowledge reference: {ref['knowledge_id']}"
                f"@{ref['knowledge_version']}")
        seen.add(key)
        knowledge = _load_knowledge(
            connection, knowledge_id=ref["knowledge_id"],
            version=ref["knowledge_version"])
        if knowledge is None:
            raise PolicyError(
                "policy_source_missing",
                f"knowledge {ref['knowledge_id']}"
                f"@{ref['knowledge_version'] or 'latest-active'} does not "
                "exist (no validated knowledge record with this identity)",
                details={"knowledge_id": ref["knowledge_id"],
                         "knowledge_version": ref["knowledge_version"]})
        resolved_refs.append({
            "knowledge_id": knowledge["knowledge_id"],
            "knowledge_version": knowledge["knowledge_version"],
        })
        rules.append(_rule_from_knowledge(
            connection, knowledge, scope=scope, action=action,
            priority=priority))
    # explicit conflict detection: same scope + metric with DIFFERENT
    # preferred variants → the knowledge contradicts itself for policy
    by_metric: dict[str, dict] = {}
    for rule in rules:
        previous = by_metric.get(rule["metric"])
        if previous is not None and previous["variant"] != rule["variant"]:
            raise PolicyError(
                "policy_conflict",
                "conflicting validated knowledge: two records prefer "
                "DIFFERENT variants for the same metric and scope",
                details={"scope": scope, "metric": rule["metric"],
                         "variant_a": previous["variant"],
                         "variant_b": rule["variant"],
                         "knowledge_a": previous["source"],
                         "knowledge_b": rule["source"]})
        by_metric[rule["metric"]] = rule
    return rules, resolved_refs


def create_candidate(policy_store, learning_db, *, knowledge_refs,
                     scope: str, rationale: str, action: str = "prefer",
                     priority: int = 1, now: str,
                     dry_run: bool = False) -> dict:
    """Compile an EXPLICIT PolicyCandidate from specific validated
    knowledge (§6). The operator chooses the knowledge references, the
    scope and the rationale; the compiler produces deterministic rules
    and REFUSES anything the knowledge cannot legitimately support.

    Deterministic identity: identical inputs (same refs resolved to the
    same versions, scope, rules, rationale) produce the SAME
    candidate_id — duplicate creation is impossible by construction.
    """
    scope = validate_scope(scope)
    if action not in SUPPORTED_ACTIONS:
        raise PolicyError("policy_rule_invalid",
                          f"unsupported policy action: {action!r} "
                          f"(supported: {list(SUPPORTED_ACTIONS)})")
    if not isinstance(priority, int) or isinstance(priority, bool) or \
            not 1 <= priority <= 100:
        raise PolicyError("policy_rule_invalid",
                          "priority must be an integer in 1..100")
    if not isinstance(rationale, str) or not rationale.strip():
        raise PolicyError("policy_invalid_candidate",
                          "a non-empty operator rationale is required")
    if len(rationale) > 2000:
        raise PolicyError("policy_invalid_candidate",
                          "rationale must be at most 2000 characters")
    if not knowledge_refs:
        raise PolicyError("policy_invalid_candidate",
                          "at least one explicit knowledge reference is "
                          "required (no promotion from arbitrary text)")
    refs = [parse_knowledge_ref(ref) for ref in knowledge_refs]

    connection = _open_learning_readonly(learning_db)
    if connection is None:
        raise PolicyError(
            "policy_source_missing",
            "no learning database exists yet — there is no validated "
            "knowledge to promote (this is a truthful structured result, "
            "not an error to hide)")
    try:
        rules, resolved_refs = build_rules(
            connection, refs=refs, scope=scope, action=action,
            priority=priority)
    finally:
        connection.close()

    content = {
        "scope": scope,
        "source_knowledge_refs": sorted(
            resolved_refs, key=lambda r: (r["knowledge_id"],
                                          r["knowledge_version"])),
        "proposed_rules": sorted(rules, key=lambda r: r["rule_id"]),
        "rationale": rationale.strip(),
    }
    candidate_id = "pcand-" + _sha256_hex(content)[:24]
    content_hash = "sha256:" + _sha256_hex(content)
    candidate = {
        "candidate_id": candidate_id,
        "source_knowledge_refs": content["source_knowledge_refs"],
        "scope": scope,
        "proposed_rules": content["proposed_rules"],
        "rationale": content["rationale"],
        "content_hash": content_hash,
        "status": "draft",
        "created_at": now,
        "updated_at": now,
        "policy_id": None,
        "policy_version": None,
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "candidate": candidate}
    if policy_store.insert_candidate(row=candidate):
        return {"ok": True, "candidate": candidate, "created": True}
    existing = policy_store.get_candidate(candidate_id)
    # the UNIQUE PRIMARY KEY is the arbiter — an identical re-creation
    # returns the EXISTING candidate (idempotent, never a duplicate)
    return {"ok": True, "candidate": existing, "created": False,
            "duplicate": True}






