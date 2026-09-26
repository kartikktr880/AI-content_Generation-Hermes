"""Stage 7 — Bounded experimentation + learning.

Consumes Stage 6 observations and produces VALIDATED, VERSIONED
knowledge through an explicit, auditable pipeline::

    Observation (Stage 6, immutable)
        → Derived Metric (provenance-carrying formulas)
        → Learning Candidate (explicit hypothesis + evidence +
          counterexamples, kept separate)
        → Experiment (immutable content-addressed definition;
          deterministic assignment; EXPLICIT approval boundary)
        → Evaluation (declared criterion; sample sizes; effect;
          uncertainty; holdout; insufficient ≠ rejected)
        → Validated Knowledge (versioned via explicit curation)
        → Hermes READ-ONLY consumption (mode=ro SQLite; scope filter)

Master boundary: the engine can learn from measured outcomes but
cannot silently rewrite itself. There is NO path from analytics or
experiment results to automatic prompt/policy modification; curation
is an explicit operator boundary; the Hermes interface is read-only.
"""

from .errors import ERROR_CODES, LearningError
from .store import LearningStore
from .derived import (
    FORMULA_CATALOG,
    FORMULA_VERSION,
    derive_metrics,
    derived_metric_id,
)
from .experiments import (
    ASSIGNMENT_METHOD,
    CANDIDATE_STATUSES,
    EXPERIMENT_STATUSES,
    approve_experiment,
    assign_units,
    candidate_id_for,
    create_candidate,
    create_experiment,
    experiment_id_for,
    start_experiment,
    variant_for_unit,
)
from .evaluation import evaluate_experiment
from .knowledge import curate_candidate, read_validated_knowledge

__all__ = [
    "ERROR_CODES",
    "LearningError",
    "LearningStore",
    "FORMULA_CATALOG",
    "FORMULA_VERSION",
    "ASSIGNMENT_METHOD",
    "CANDIDATE_STATUSES",
    "EXPERIMENT_STATUSES",
    "approve_experiment",
    "assign_units",
    "candidate_id_for",
    "create_candidate",
    "create_experiment",
    "curate_candidate",
    "derive_metrics",
    "derived_metric_id",
    "evaluate_experiment",
    "experiment_id_for",
    "read_validated_knowledge",
    "start_experiment",
    "variant_for_unit",
]
