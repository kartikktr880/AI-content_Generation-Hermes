"""Stage 8 — Controlled policy promotion.

Extends the Stage 7 boundary with a deliberately controlled promotion
path from VALIDATED knowledge to a ProductionPolicy that Hermes can
consume READ-ONLY::

    Validated Knowledge (Stage 7; the ONLY legitimate evidence source)
        ↓  create_candidate (deterministic compiler; fail-closed)
    PolicyCandidate (content-addressed; references explicit knowledge)
        ↓  approve_candidate   — EXPLICIT OPERATOR APPROVAL
    approved candidate
        ↓  promote_candidate   — deterministic, immutable
    ProductionPolicy vN (content-addressed; versioned; history retained)
        ↓  activate_policy     — EXPLICIT ACTIVATION
    active policy (ONE per scope; enforced by the database)
        ↓  read_active_policy  — Hermes READ-ONLY (SQLite mode=ro)
    future content decisions

Master boundary (§2):

    Hermes executes ProductionPolicy; Hermes does not author, approve,
    mutate, or promote ProductionPolicy.

There is NO automatic analytics→policy path, NO automatic
learning→policy path, NO scheduler, NO hidden fallback. The only writes
into the policy store are the explicit operator operations in
``ayce.policy.lifecycle``. The learning and analytics stores are read
through SQLite ``mode=ro`` connections only.
"""

from .errors import ERROR_CODES, PolicyError
from .store import PolicyStore
from .compiler import (
    GLOBAL_SCOPE,
    RULE_KIND,
    RULE_OPERATOR,
    SCOPE_DIMENSIONS,
    SUPPORTED_ACTIONS,
    create_candidate,
    parse_knowledge_ref,
    validate_rule,
    validate_scope,
)
from .lifecycle import (
    CANDIDATE_STATUSES,
    POLICY_STATUSES,
    activate_policy,
    approve_candidate,
    diff_for_activation,
    promote_candidate,
    read_active_policy,
    reject_candidate,
    retire_policy,
    rollback_policy,
)
from .execution import (
    CONTEXT_SCHEMA_VERSION,
    DEFAULT_SCOPE_CANDIDATES,
    SUPPORTED_RULE_KINDS,
    append_consumption,
    apply_policy_preference,
    build_execution_context,
    consumption_id,
    preferred_variant,
    read_consumptions,
    resolve_active_policy,
    verify_active_policy,
)
from .observability import (
    CONSUMPTION_ARTIFACT_FILENAME,
    CONSUMPTION_SCHEMA_VERSION,
    CONSUMPTION_STAGE,
    read_run_consumptions,
    record_consumption_artifact,
    verify_consumption_history,
)
from .lineage import (
    lineage_for_policy,
    lineage_for_run,
    rebuild_lineage_index,
    verify_lineage_index,
)
from .effectiveness import (
    EFFECTIVENESS_SCHEMA_VERSION,
    EVIDENCE_NOTE,
    MISSING_DATA_STATUSES,
    effectiveness_for_policy,
    effectiveness_for_run,
    metric_effectiveness_summary,
    verify_effectiveness,
)
from .experiment_intake import (
    CANDIDATE_ID_RE,
    CANDIDATE_STATUS_VALUES,
    INTAKE_NOTE,
    INTAKE_SCHEMA_VERSION,
    approve_candidate as approve_experiment_candidate,
    candidates_for_metric as experiment_candidates_for_metric,
    candidates_for_policy as experiment_candidates_for_policy,
    show_candidate as show_experiment_candidate,
    verify_intake,
)
from .experiment_definition import (
    COMPOSITION_NOTE,
    COMPOSITION_SCHEMA_VERSION,
    MIN_POPULATION_UNITS,
    PROPOSAL_ID_RE,
    PROPOSAL_STATUS_VALUES,
    approve_proposal as approve_experiment_definition,
    propose_for_candidate as propose_experiment_definition,
    proposals_for_policy as experiment_definitions_for_policy,
    show_proposal as show_experiment_definition,
    verify_composition,
)

__all__ = [
    "ERROR_CODES",
    "PolicyError",
    "PolicyStore",
    "GLOBAL_SCOPE",
    "RULE_KIND",
    "RULE_OPERATOR",
    "SCOPE_DIMENSIONS",
    "SUPPORTED_ACTIONS",
    "CANDIDATE_STATUSES",
    "POLICY_STATUSES",
    "CONTEXT_SCHEMA_VERSION",
    "DEFAULT_SCOPE_CANDIDATES",
    "SUPPORTED_RULE_KINDS",
    "CONSUMPTION_ARTIFACT_FILENAME",
    "CONSUMPTION_SCHEMA_VERSION",
    "CONSUMPTION_STAGE",
    "CANDIDATE_ID_RE",
    "CANDIDATE_STATUS_VALUES",
    "COMPOSITION_NOTE",
    "COMPOSITION_SCHEMA_VERSION",
    "EFFECTIVENESS_SCHEMA_VERSION",
    "EVIDENCE_NOTE",
    "INTAKE_NOTE",
    "INTAKE_SCHEMA_VERSION",
    "MIN_POPULATION_UNITS",
    "MISSING_DATA_STATUSES",
    "PROPOSAL_ID_RE",
    "PROPOSAL_STATUS_VALUES",
    "activate_policy",
    "append_consumption",
    "apply_policy_preference",
    "approve_candidate",
    "approve_experiment_candidate",
    "approve_experiment_definition",
    "build_execution_context",
    "consumption_id",
    "create_candidate",
    "diff_for_activation",
    "effectiveness_for_policy",
    "effectiveness_for_run",
    "experiment_candidates_for_metric",
    "experiment_candidates_for_policy",
    "experiment_definitions_for_policy",
    "lineage_for_policy",
    "lineage_for_run",
    "parse_knowledge_ref",
    "preferred_variant",
    "promote_candidate",
    "read_active_policy",
    "read_consumptions",
    "read_run_consumptions",
    "record_consumption_artifact",
    "reject_candidate",
    "rebuild_lineage_index",
    "resolve_active_policy",
    "retire_policy",
    "rollback_policy",
    "metric_effectiveness_summary",
    "propose_experiment_definition",
    "show_experiment_candidate",
    "show_experiment_definition",
    "verify_composition",
    "verify_intake",
    "validate_rule",
    "validate_scope",
    "verify_active_policy",
    "verify_consumption_history",
    "verify_effectiveness",
    "verify_lineage_index",
]
