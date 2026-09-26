"""Stage 7 — Curation boundary + versioned knowledge (§25/§26/§29).

The ONLY promotion path from experiment results to knowledge::

    candidate (validated by an evaluation)
        ↓ curate()          — explicit, operator-invoked, deterministic
    validated_knowledge     — versioned, factual, immutable history

Hard boundaries:

- Raw analytics NEVER call the curator; nothing in the analytics or
  experiment layers can reach ``curate`` by itself.
- Knowledge statements are FACTS about an experiment population
  ("variant B produced X% higher metric M within population P under
  window W"), NEVER policy ("always use variant B") — §29.
- Knowledge is VERSIONED: a changed statement is a new
  ``knowledge_version`` under the same ``knowledge_id``; older versions
  are retained and marked superseded — never overwritten (§25).
- ``read_validated_knowledge`` is the Hermes-facing surface: it opens
  the learning database in SQLite READ-ONLY mode and supports a
  deterministic scope filter — no write path exists on it (§27).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

__all__ = ["curate_candidate", "read_validated_knowledge"]


def _canonical(payload) -> str:
    import json
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _knowledge_id(*, statement: str, scope: str) -> str:
    return "knw-" + hashlib.sha256(
        _canonical({"statement": statement, "scope": scope})
        .encode("utf-8")).hexdigest()[:24]


def _statement_for(definition: dict, evaluation: dict) -> str:
    """Deterministic, FACTUAL knowledge statement (§29): reports what
    was observed within the experiment population — it never prescribes
    production policy."""
    criterion = evaluation["criterion"]
    window = definition.get("evaluation_window")
    window_text = (f"{window['start']}..{window['end']}" if window
                   else "no declared evaluation window")
    effect = evaluation["effect"]
    effect_text = ("n/a (no usable pairs)" if effect is None
                   else f"{effect:+.4g}")
    control_n = evaluation.get("control_n")
    treatment_n = evaluation.get("treatment_n")
    holdout = evaluation.get("holdout") or {}
    validation_method = holdout.get(
        "validation_method", definition.get("validation_method", "none"))
    return (
        f"Within experiment {evaluation['experiment_id']} "
        f"(units: control n={control_n}, treatment n={treatment_n}; "
        f"metric: {definition['metric']}), variant "
        f"'{definition['treatment_variant']}' showed {effect_text} "
        f"difference versus variant '{definition['control_variant']}' "
        f"under evaluation window {window_text}. Declared criterion: "
        f"{_canonical(criterion)}. Evaluation: {evaluation['status']} "
        f"(validation method: {validation_method}"
        + (f"; holdout effect {holdout.get('effect'):+.4g}"
           if holdout.get("effect") is not None else "")
        + "). This is an association observed within the experiment "
        "population, not a causal claim and not a production policy."
    )
def curate_candidate(store, *, candidate_id: str, evaluation_id: str,
                     now: str, dry_run: bool = False) -> dict:
    """The EXPLICIT curation boundary (§26): a VALIDATED candidate +
    its VALIDATED evaluation become versioned knowledge. Deterministic,
    auditable, idempotent (curation of the same evaluation returns the
    same knowledge record). Raw analytics cannot reach this function.
    """
    from .errors import LearningError

    candidate = store.get_candidate(candidate_id)
    if candidate is None:
        raise LearningError("learning_curation_failed",
                            f"unknown candidate: {candidate_id}")
    evaluation = store.get_evaluation(evaluation_id)
    if evaluation is None:
        raise LearningError("learning_curation_failed",
                            f"unknown evaluation: {evaluation_id}")
    experiment = store.get_experiment(evaluation["experiment_id"])
    if experiment is None or \
            experiment["candidate_id"] != candidate_id:
        raise LearningError(
            "learning_curation_failed",
            "evaluation does not belong to this candidate's experiment")
    if candidate["status"] != "validated" or \
            evaluation["status"] != "validated":
        raise LearningError(
            "learning_curation_failed",
            "only VALIDATED candidates with a VALIDATED evaluation can be "
            "curated",
            details={"candidate_status": candidate["status"],
                     "evaluation_status": evaluation["status"]})

    definition = experiment["definition"]
    statement = _statement_for(definition, evaluation)
    scope = candidate["scope"]
    knowledge_id = _knowledge_id(statement=statement, scope=scope)
    version = store.next_knowledge_version(knowledge_id)
    knowledge = {
        "knowledge_id": knowledge_id,
        "knowledge_version": version,
        "candidate_id": candidate_id,
        "experiment_id": evaluation["experiment_id"],
        "evaluation_id": evaluation_id,
        "statement": statement,
        "scope": scope,
        "evidence": candidate["evidence"],
        "validation": {
            "validation_method": definition.get("validation_method"),
            "evaluation_status": evaluation["status"],
            "control_n": evaluation.get("control_n"),
            "treatment_n": evaluation.get("treatment_n"),
            "effect": evaluation.get("effect"),
            "p_value": evaluation.get("p_value"),
            "holdout": evaluation.get("holdout"),
            "dataset_fingerprint": evaluation.get("dataset_fingerprint"),
        },
        "status": "active",
        "created_at": now,
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "knowledge": knowledge}
    # concurrent curators may race for the same version — bounded retry
    # with re-read; the UNIQUE(knowledge_id, version) constraint is the
    # arbiter (§33). An identical re-curation (same evaluation) is
    # IDEMPOTENT: it returns the existing record, never a new version.
    for _ in range(5):
        existing = store.get_knowledge(knowledge_id)
        if existing is not None:
            if existing["evaluation_id"] == evaluation_id:
                return {"ok": True, "knowledge": existing, "created": False}
            # same statement, different evaluation → genuinely a new
            # confirmation → new version (history retained, §25)
            version = store.next_knowledge_version(knowledge_id)
            knowledge = dict(knowledge, knowledge_version=version)
        if store.insert_knowledge(row=knowledge):
            if version > 1:
                store.supersede_knowledge(knowledge_id,
                                          below_version=version)
            return {"ok": True, "knowledge": knowledge, "created": True}
    from .errors import LearningError as _LE
    raise _LE("learning_version_conflict",
              "could not allocate a knowledge version after retries")


def read_validated_knowledge(db_path, scope: str | None = None) -> dict:
    """The Hermes-facing READ-ONLY query (§27/§28).

    Opens the learning database in SQLite read-only mode (URI
    ``mode=ro``) — no write path exists on this connection. Supports a
    deterministic scope filter (exact match; no semantic search).
    A missing database is reported truthfully as zero knowledge.
    """
    path = Path(db_path)
    if not path.is_file():
        return {"ok": True, "knowledge": [], "note": "no learning "
                "database exists yet"}
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        if scope is not None:
            rows = connection.execute(
                "SELECT * FROM validated_knowledge WHERE scope = ?"
                " AND status = 'active' ORDER BY knowledge_id,"
                " knowledge_version",
                (scope,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM validated_knowledge WHERE status = 'active'"
                " ORDER BY scope, knowledge_id, knowledge_version",
            ).fetchall()
        knowledge = []
        for row in rows:
            data = dict(row)
            try:
                data["evidence"] = json.loads(data["evidence"])
                data["validation"] = json.loads(data["validation"])
            except (ValueError, TypeError):
                pass
            knowledge.append(data)
        return {"ok": True, "knowledge": knowledge}
    finally:
        connection.close()


