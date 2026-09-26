"""AYCE Read-Only MCP server (H2 experiment) — standalone, outside src/ayce/.

Exposes exactly TEN read-only tools over the Model Context Protocol
(stdio JSON-RPC):

    list_runs                enumerate AYCE run directories (metadata only)
    get_run_state            return the persisted RunState of one run
    get_run_artifacts        return the persisted ArtifactRegistry of one run
    get_validated_knowledge  return validated learning knowledge (Stage 7;
                             deterministic scope filter; NO write path)
    get_active_policy        return the ACTIVE ProductionPolicy for one
                             scope (Stage 8; deterministic scope filter;
                             NO write path)
    get_policy_consumptions  return the policy consumption evidence of one
                             run (Stage 10; hash-verified, read-only)
    get_policy_lineage       cross-run lineage projection (Stage 11;
                             Run→Policies or Policy→Runs; derived
                             read-only index, NO write path)
    get_policy_effectiveness policy effectiveness EVIDENCE (Stage 12;
                             read-only correlation Policy → Run → Publish
                             → Video → Analytics; descriptive only; NO
                             ranking, NO causal claim, NO write path)
    get_experiment_candidates experiment INTAKE candidates (Stage 13;
                             read-only derivation from Stage 12 evidence;
                             NO ranking, NO causal claim, NO write path)
    get_experiment_definitions experiment definition PROPOSALS (Stage 14;
                             read-only composition from approved Stage 13
                             candidates; NO invented parameters; NO
                             write/start/assign authority)

SECURITY BOUNDARY (this server is deliberately minimal):

- READ-ONLY: no shell, no subprocess, no file writes, no deletion, no
  environment inspection, no credentials, no pipeline/FFmpeg/Piper
  invocation, no RunState/ArtifactRegistry mutation, no learning-state
  mutation, no POLICY mutation. The knowledge tool opens the learning
  database in SQLite READ-ONLY mode (URI ``mode=ro``); the policy tools
  open the policy database the same way, and the consumption tool only
  reads run artifacts (hash-verified). Experiment approval, curation,
  knowledge writes, and — critically — policy authoring, approval,
  promotion, activation and rollback DO NOT EXIST on this server (§2:
  Hermes executes ProductionPolicy; Hermes does not author, approve,
  mutate, or promote ProductionPolicy).
- The ONLY filesystem surface is the configured AYCE runs root
  (``AYCE_RUNS_ROOT`` env var; defaults to <repo>/data/runs) for run
  metadata, the configured learning database (``AYCE_LEARNING_DB``;
  defaults to <repo>/data/learning/learning.sqlite3) opened read-only,
  and the configured policy database (``AYCE_POLICY_DB``; defaults to
  <repo>/data/policy/policy.sqlite3) opened read-only.
- Path safety: run identifiers must match the AYCE id grammar
  (``^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$``) — this rejects absolute paths
  and any ``..`` segment by construction; the resolved directory must
  additionally stay inside the resolved runs root (symlink-escape guard).
- Failures are structured JSON (``{"ok": false, "error": {...}}``);
  stack traces and absolute host paths are never returned.
- AYCE's own readers (``RunState.load``, ``ArtifactRegistry.load``,
  ``ayce.learning.knowledge.read_validated_knowledge``,
  ``ayce.policy.lifecycle.read_active_policy``,
  ``ayce.policy.observability.read_run_consumptions``) are REUSED — no
  duplicated parsing, no modified AYCE internals.

Runtime: its own isolated virtual environment (Python 3.11 + the `mcp`
SDK) — AYCE has NO dependency on this component, and this component is
independently removable.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Make the AYCE readers importable WITHOUT making this a package or adding
# any dependency to AYCE. The readers used (ayce.state, ayce.artifacts,
# ayce.ids) are standard-library-only.
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_REPO_SRC))

from mcp.server.fastmcp import FastMCP  # noqa: E402  (import after sys.path setup)

from ayce.artifacts import ArtifactError, ArtifactRegistry  # noqa: E402
from ayce.state import RunState, StateError  # noqa: E402

#: AYCE run-id grammar — rejects absolute paths and ``..`` by construction.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Upper bound on entries returned by list_runs (bounds the response).
_MAX_LIST_RUNS = 100

mcp = FastMCP(
    "ayce-readonly",
    instructions=(
        "Read-only inspection of AYCE (Autonomous YouTube Content Engine) "
        "production runs: enumerate runs, read one run's persisted stage "
        "state, read one run's artifact registry, read validated learning "
        "knowledge, and read the active ProductionPolicy per scope. No "
        "write operations exist on this server."
    ),
)


def _runs_root() -> Path:
    """Resolve the configured AYCE runs root (the ONLY permitted surface)."""
    root = os.environ.get("AYCE_RUNS_ROOT", "").strip()
    if root:
        return Path(root).resolve()
    return (_REPO_SRC.parent / "data" / "runs").resolve()


def _learning_db() -> Path:
    """Resolve the configured learning database (opened READ-ONLY)."""
    root = os.environ.get("AYCE_LEARNING_DB", "").strip()
    if root:
        return Path(root).resolve()
    return (_REPO_SRC.parent / "data" / "learning" / "learning.sqlite3"
            ).resolve()


def _policy_db() -> Path:
    """Resolve the configured policy database (opened READ-ONLY)."""
    root = os.environ.get("AYCE_POLICY_DB", "").strip()
    if root:
        return Path(root).resolve()
    return (_REPO_SRC.parent / "data" / "policy" / "policy.sqlite3"
            ).resolve()


def _publishing_db() -> Path:
    """Resolve the configured Stage 5 publish ledger (opened READ-ONLY;
    Stage 12 effectiveness — the AUTHORITATIVE run → video mapping)."""
    root = os.environ.get("AYCE_PUBLISH_LEDGER", "").strip()
    if root:
        return Path(root).resolve()
    return (_REPO_SRC.parent / "data" / "publishing" / "ledger.sqlite3"
            ).resolve()


def _analytics_db() -> Path:
    """Resolve the configured Stage 6 analytics store (opened READ-ONLY;
    Stage 12 effectiveness — the AUTHORITATIVE measurement source)."""
    root = os.environ.get("AYCE_ANALYTICS_DB", "").strip()
    if root:
        return Path(root).resolve()
    return (_REPO_SRC.parent / "data" / "analytics" / "analytics.sqlite3"
            ).resolve()



def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}}


def _resolve_run(run_id: str) -> tuple[Path | None, dict | None]:
    """Safely resolve a run directory, or return a structured error.

    Guards: run-id grammar (rejects absolute paths / ``..``), existence,
    and a resolved-inside-root check that also defeats symlink escapes.
    """
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        return None, _error(
            "invalid_run_id",
            "run_id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ "
            "(absolute paths and traversal segments are rejected)",
        )
    root = _runs_root()
    candidate = (root / run_id).resolve()
    if candidate != root and root not in candidate.parents:
        return None, _error(
            "path_escape", "resolved run path escaped the configured runs root"
        )
    if not candidate.is_dir():
        return None, _error("unknown_run", f"no such run: {run_id}")
    return candidate, None


@mcp.tool()
def list_runs() -> dict:
    """Enumerate AYCE runs in the configured runs root (metadata only).

    Returns, per run: run_id, job_id, per-stage status summary, and the
    state checkpoint timestamp. Runs with corrupt/missing state are listed
    with a truthful ``state`` marker — metadata is never invented.
    """
    root = _runs_root()
    if not root.is_dir():
        return {"ok": True, "runs": [], "runs_root_missing": True, "truncated": False}
    runs: list[dict] = []
    truncated = False
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        if len(runs) >= _MAX_LIST_RUNS:
            truncated = True
            break
        state_path = entry / "state.json"
        if not state_path.is_file():
            continue  # only directories that look like AYCE runs are listed
        try:
            state = RunState.load(state_path)
        except StateError:
            runs.append({"run_id": entry.name, "state": "corrupt"})
            continue
        stages = state.stages
        runs.append(
            {
                "run_id": state.run_id,
                "job_id": state.job_id,
                "stage_count": len(stages),
                "stages_succeeded": sum(
                    1 for r in stages.values() if r.status.value == "succeeded"
                ),
                "stages_failed": sum(
                    1 for r in stages.values() if r.status.value == "failed"
                ),
                "updated_at": state.updated_at,
            }
        )
    return {"ok": True, "runs": runs, "truncated": truncated}


@mcp.tool()
def get_run_state(run_id: str) -> dict:
    """Return the persisted RunState of one AYCE run (read-only).

    The result contains the run identity, timestamps, and the full
    per-stage status map exactly as checkpointed by AYCE. Unknown runs
    and corrupt state files return structured errors, never exceptions.
    """
    run_dir, err = _resolve_run(run_id)
    if err is not None:
        return err
    state_path = run_dir / "state.json"
    if not state_path.is_file():
        return _error("missing_state", f"run {run_id} has no state.json")
    try:
        state = RunState.load(state_path)
    except StateError as exc:
        return _error("corrupt_state", str(exc))
    data = state.to_dict()
    return {
        "ok": True,
        "run_id": state.run_id,
        "data": {
            "run_id": data["run_id"],
            "job_id": data["job_id"],
            "created_at": data["created_at"],
            "updated_at": data["updated_at"],
            "stages": data["stages"],
        },
    }


@mcp.tool()
def get_run_artifacts(run_id: str) -> dict:
    """Return the persisted ArtifactRegistry of one AYCE run (read-only).

    Each artifact record exposes its id, producing stage, kind, run-relative
    path, creation timestamp, and metadata — exactly what AYCE checkpointed.
    File contents are never opened. Unknown runs return structured errors.
    """
    run_dir, err = _resolve_run(run_id)
    if err is not None:
        return err
    manifest_path = run_dir / "artifacts.json"
    if not manifest_path.is_file():
        return _error("missing_manifest", f"run {run_id} has no artifacts.json")
    try:
        registry = ArtifactRegistry.load(run_dir, run_id)
    except ArtifactError as exc:
        return _error("corrupt_manifest", str(exc))
    artifacts = [
        {
            "artifact_id": ref.artifact_id,
            "stage": ref.stage,
            "kind": ref.kind.value,
            "path": ref.path,
            "created_at": ref.created_at,
            "metadata": ref.metadata,
        }
        for ref in registry.all()
    ]
    return {
        "ok": True,
        "run_id": run_id,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }


@mcp.tool()
def get_validated_knowledge(scope: str = "") -> dict:
    """Return VALIDATED learning knowledge (Stage 7; read-only).

    Deterministic filtered lookup over the versioned knowledge store —
    an exact scope match when a scope is given, otherwise all active
    knowledge. Statements are factual experiment outcomes (associations
    within a population), never causal claims and never production
    policy. This tool has NO write capability: the database connection
    is opened in SQLite read-only mode.
    """
    from ayce.learning.knowledge import read_validated_knowledge

    result = read_validated_knowledge(_learning_db(),
                                      scope=scope.strip() or None)
    # bound the response (knowledge stores are small; still explicit)
    knowledge = result.get("knowledge", [])
    truncated = len(knowledge) > _MAX_LIST_RUNS
    return {
        "ok": True,
        "scope": scope.strip() or None,
        "count": len(knowledge[:_MAX_LIST_RUNS]),
        "truncated": truncated,
        "knowledge": knowledge[:_MAX_LIST_RUNS],
        **({"note": result["note"]} if result.get("note") else {}),
    }


@mcp.tool()
def get_active_policy(scope: str = "global") -> dict:
    """Return the ACTIVE ProductionPolicy for one scope (Stage 8;
    read-only).

    Deterministic exact-scope lookup over the versioned policy store.
    The returned policy is the operator-approved, explicitly activated
    ProductionPolicy — execution context only. Hermes CANNOT author,
    approve, promote, activate, roll back, or mutate policy through this
    server: the database connection is opened in SQLite read-only mode
    and no policy-write tool exists.
    """
    from ayce.policy.lifecycle import read_active_policy
    from ayce.policy.errors import PolicyError

    scope = scope.strip() or "global"
    try:
        result = read_active_policy(_policy_db(), scope)
    except PolicyError as exc:
        return _error(exc.code, exc.message)
    policy = result.get("policy")
    return {
        "ok": True,
        "scope": scope,
        "policy": policy,
        **({"note": result["note"]} if result.get("note") else {}),
    }


@mcp.tool()
def get_policy_consumptions(run_id: str, decision_id: str = "",
                            policy_id: str = "",
                            policy_version: int | None = None) -> dict:
    """Return the POLICY CONSUMPTION evidence of one run (Stage 10;
    read-only).

    Deterministic, hash-verified lookup over the run's registered
    POLICY_CONSUMPTION artifacts: which decision consumed WHICH policy
    version/scope/context — immutable historical evidence that remains
    valid after policy v2 activation, rollback, or retirement. Optional
    exact filters: decision_id, policy_id, policy_version. Corrupt or
    mutated evidence returns a structured integrity error (never silently
    filtered). NO write capability exists on this tool.
    """
    from ayce.policy.errors import PolicyError
    from ayce.policy.observability import read_run_consumptions, \
        verify_consumption_history

    run_dir, err = _resolve_run(run_id)
    if err is not None:
        return err
    try:
        result = read_run_consumptions(run_dir, run_id)
    except PolicyError as exc:
        return _error(exc.code, exc.message)
    consumptions = result.get("consumptions", [])
    # deterministic exact filters (§10) — applied AFTER reading so the
    # response ordering stays canonical
    if decision_id.strip():
        consumptions = [c for c in consumptions
                        if c.get("decision_id") == decision_id.strip()]
    if policy_id.strip():
        consumptions = [c for c in consumptions
                        if c.get("policy_id") == policy_id.strip()]
    if policy_version is not None:
        consumptions = [c for c in consumptions
                        if c.get("policy_version") == policy_version]
    # historical cross-check against the authoritative policy store
    # (§15/§16): reports current policy state without requiring active
    verifications = []
    policy_db = _policy_db()
    for event in consumptions:
        try:
            verifications.append(verify_consumption_history(event, policy_db))
        except PolicyError as exc:
            return _error(exc.code, exc.message)
    return {
        "ok": True,
        "run_id": run_id,
        "count": len(consumptions),
        "consumptions": consumptions,
        "verification": verifications,
        **({"note": result["note"]} if result.get("note") else {}),
    }


@mcp.tool()
def get_policy_lineage(run_id: str = "", policy_id: str = "",
                       policy_version: int | None = None,
                       scope: str = "") -> dict:
    """Cross-run policy lineage projection (Stage 11; read-only).

    Deterministic DERIVED index over the canonical policy-consumption
    artifacts — never authority. Exactly one direction per call:
    run_id → the policy versions that run consumed; policy_id → the
    runs that consumed that policy (optionally filtered to one exact
    policy_version and/or one exact scope). Historical evidence remains
    valid after policy v2 activation, rollback, or retirement. Corrupt
    evidence is reported and excluded (fail closed). NO write capability
    exists on this tool.
    """
    from ayce.policy.errors import PolicyError
    from ayce.policy.lineage import lineage_for_policy, lineage_for_run

    run_id = run_id.strip()
    policy_id = policy_id.strip()
    scope = scope.strip() or None
    if bool(run_id) == bool(policy_id):
        return _error("invalid_request",
                      "exactly ONE of run_id / policy_id is required "
                      "(one lineage direction per call)")
    try:
        if run_id:
            return lineage_for_run(_runs_root(), run_id,
                                   policy_db_path=_policy_db(), scope=scope)
        return lineage_for_policy(_runs_root(), policy_id,
                                  policy_version=policy_version,
                                  policy_db_path=_policy_db(), scope=scope)
    except PolicyError as exc:
        return _error(exc.code, exc.message)


@mcp.tool()
def get_policy_effectiveness(run_id: str = "", policy_id: str = "",
                             policy_version: int | None = None,
                             metric: str = "") -> dict:
    """Policy effectiveness EVIDENCE projection (Stage 12; read-only).

    Deterministic READ-ONLY correlation over the CANONICAL stores —
    Stage 11 policy lineage (Policy → Runs), Stage 5 publish ledger
    (Run → Video), Stage 6 analytics store (Video → Measurements):

        Policy Consumption → Run → Publish Ledger → Video ID
            → Analytics Observation → Measured Outcome

    Exactly ONE primary identity per call: run_id (the run's full
    evidence status) OR policy_id (aggregation over the runs that
    consumed it; optionally one exact policy_version / metric).
    Observations only: descriptive counts and summaries — NO ranking,
    NO score, NO causal claim, NO promotion/rollback recommendation.
    Unpublished runs are NOT analytics failures; missing analytics is
    NEVER zero; sources/windows are never merged. NO write capability
    exists on this tool and no store is opened outside SQLite
    read-only mode.
    """
    from ayce.policy.effectiveness import (
        effectiveness_for_policy, effectiveness_for_run,
    )
    from ayce.policy.errors import PolicyError

    run_id = run_id.strip()
    policy_id = policy_id.strip()
    metric = metric.strip() or None
    if bool(run_id) == bool(policy_id):
        return _error("invalid_request",
                      "exactly ONE of run_id / policy_id is required "
                      "(one effectiveness direction per call)")
    try:
        if run_id:
            return effectiveness_for_run(
                _runs_root(), run_id, policy_db_path=_policy_db(),
                ledger_path=_publishing_db(),
                analytics_db_path=_analytics_db())
        return effectiveness_for_policy(
            _runs_root(), policy_id, policy_version=policy_version,
            metric=metric, policy_db_path=_policy_db(),
            ledger_path=_publishing_db(),
            analytics_db_path=_analytics_db())
    except PolicyError as exc:
        return _error(exc.code, exc.message)


@mcp.tool()
def get_experiment_candidates(candidate_id: str = "", policy_id: str = "",
                              policy_version: int | None = None,
                              metric: str = "", status: str = "") -> dict:
    """Experiment INTAKE candidates derived from Stage 12 effectiveness
    evidence (Stage 13; read-only).

    A candidate means ONLY: "verified observational evidence exists for
    this policy/version/scope/metric and a controlled experiment MAY be
    considered to test the observed difference". It is NOT a causal
    claim, NOT a ranking, NOT a winner, NOT a promotion or rollback
    recommendation, and NOT a started experiment.

    Candidates are a DERIVED projection (recomputed from Stage 12
    evidence; nothing is stored; changed evidence yields a NEW
    candidate id). Requires at least ONE of candidate_id (show one) /
    policy_id (that policy's candidates) / metric (candidates across
    policies); optional exact policy_version and status
    (eligible | insufficient_evidence | ineligible) filters.
    Missing/unavailable analytics is NEVER negative evidence.
    NO write capability exists on this tool — approving a candidate
    into the existing Stage 7 learning store is an operator-only CLI
    boundary, and NO experiment-start authority is exposed here.
    """
    from ayce.policy.errors import PolicyError
    from ayce.policy.experiment_intake import (
        CANDIDATE_STATUS_VALUES, candidates_for_metric,
        candidates_for_policy, show_candidate,
    )

    candidate_id = candidate_id.strip()
    policy_id = policy_id.strip()
    metric = metric.strip()
    status = status.strip()
    if status and status not in CANDIDATE_STATUS_VALUES:
        return _error("invalid_request",
                      f"status must be one of {CANDIDATE_STATUS_VALUES}")
    if not (candidate_id or policy_id or metric):
        return _error("invalid_request",
                      "at least one of candidate_id / policy_id / metric "
                      "is required (bounded deterministic filters)")
    paths = {"policy_db_path": _policy_db(),
             "ledger_path": _publishing_db(),
             "analytics_db_path": _analytics_db()}
    try:
        if candidate_id:
            return show_candidate(_runs_root(), candidate_id, **paths)
        if policy_id:
            report = candidates_for_policy(
                _runs_root(), policy_id, policy_version=policy_version,
                metric=(metric or None), **paths)
        else:
            report = candidates_for_metric(
                _runs_root(), metric, policy_version=policy_version,
                **paths)
    except PolicyError as exc:
        return _error(exc.code, exc.message)
    if status:  # deterministic post-filter; counts stay truthful
        report["candidates"] = [c for c in report["candidates"]
                                if c["status"] == status]
        report["candidate_count"] = len(report["candidates"])
        report["status_filter"] = status
    return report


@mcp.tool()
def get_experiment_definitions(proposal_id: str = "",
                               candidate_id: str = "",
                               policy_id: str = "") -> dict:
    """Experiment DEFINITION proposals (Stage 14; read-only).

    A proposal is a deterministic, operator-reviewable draft of the
    EXISTING Stage 7 experiment inputs (metric, comparison, population,
    criteria) composed from an APPROVED Stage 13 intake candidate. It is
    NOT an experiment, NOT a start, NOT an assignment, NOT an
    evaluation, NOT a causal claim and NOT a policy recommendation.
    Unresolvable fields are reported explicitly (they are NEVER
    invented); the assignment seed is a required operator argument at
    approval time.

    Exactly ONE of proposal_id / candidate_id / policy_id is required
    (bounded deterministic lookup). NO write capability exists on this
    tool: approving a proposal into the Stage 7 DRAFT experiment is an
    operator-only CLI boundary, and NO experiment-start authority is
    exposed here.
    """
    from ayce.policy.errors import PolicyError
    from ayce.policy.experiment_definition import (
        propose_for_candidate, show_proposal,
    )

    proposal_id = proposal_id.strip()
    candidate_id = candidate_id.strip()
    policy_id = policy_id.strip()
    if bool(proposal_id) + bool(candidate_id) + bool(policy_id) != 1:
        return _error("invalid_request",
                      "exactly ONE of proposal_id / candidate_id / "
                      "policy_id is required (bounded lookup)")
    paths = {"policy_db_path": _policy_db(),
             "ledger_path": _publishing_db(),
             "analytics_db_path": _analytics_db()}
    try:
        if proposal_id:
            return show_proposal(_runs_root(), proposal_id, **paths)
        if candidate_id:
            return propose_for_candidate(_runs_root(), candidate_id,
                                         **paths)
        from ayce.policy.experiment_definition import proposals_for_policy
        return proposals_for_policy(_runs_root(), policy_id, **paths)
    except PolicyError as exc:
        return _error(exc.code, exc.message)


if __name__ == "__main__":
    # stdio JSON-RPC transport (the transport Hermes's MCP client uses for
    # `--command` servers). No server state, no persistence, no extra threads.
    mcp.run()
