"""Stage 8 — Controlled policy promotion tests (deterministic fixtures).

Covers the §25 matrix using REAL Stage 6 analytics + Stage 7 learning
stores seeded with deterministic measurement fixtures — no network, no
YouTube, NO fabricated repository evidence:

candidate (deterministic compile from validated knowledge / missing /
superseded / rejected / insufficient evidence / scope grammar + mismatch
/ unsupported threshold semantics / conflicts / duplicate identity /
dry-run), approval (explicit boundary required / idempotent double
approval / rejected candidate / promoted candidate / concurrency),
promotion (immutable versioned policy / deterministic content hash /
concurrency / version increment / previous-version binding /
source-knowledge revalidation / immutability), activation (explicit +
idempotent / one-active-per-scope / conflicts / history preserved /
concurrency), diff (first policy / added / removed / changed rules /
scope change), rollback (prior version / history retained / invalid /
concurrent), Hermes read-only surface (mode=ro / no mutation / no write
tool), safety (analytics / learning / publish ledger / RunState /
research artifacts byte-identical; source knowledge row unchanged; no
automatic learning→policy path), restart, and CLI (end-to-end,
dry-run, truthful real-data absence).
"""

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from ayce.learning import (
    LearningError,
    LearningStore,
    approve_experiment,
    assign_units,
    create_candidate as learn_candidate,
    create_experiment,
    curate_candidate,
    evaluate_experiment,
)
from ayce.policy import (
    PolicyError,
    PolicyStore,
    activate_policy,
    approve_candidate,
    create_candidate,
    diff_for_activation,
    promote_candidate,
    read_active_policy,
    reject_candidate,
    retire_policy,
    rollback_policy,
    validate_rule,
    validate_scope,
)

# reuse the REAL deterministic Stage 6/7 fixture helpers (no duplication)
from test_learning import NOW, full_pipeline, make_env, pick_balanced_seed, \
    seed_video

SCOPE = "format:short_explainer"
RATIONALE = "operator decision: promote the validated variant preference"


def policy_env(tmp_path: Path) -> PolicyStore:
    return PolicyStore(tmp_path / "data" / "policy" / "policy.sqlite3")


def balanced_seed(learning, candidate, *, metric="views", units,
                  min_n=2, min_effect=1.0):
    """Deterministically choose an assignment seed against the EXACT
    definition the experiment will be created with (min_effect included,
    so the searched id matches the created id)."""
    from ayce.learning.experiments import (
        _validate_definition, experiment_id_for, variant_for_unit)
    for i in range(500):
        seed = f"seed-{i}"
        definition = _validate_definition(
            candidate_id=candidate["candidate_id"],
            hypothesis=candidate["hypothesis"], metric=metric,
            control_variant="control", treatment_variant="treatment",
            population={"units": units}, evaluation_window=None,
            success_criterion={"min_control_n": min_n,
                               "min_treatment_n": min_n,
                               "min_effect": min_effect},
            assignment={"seed": seed}, holdout=None)
        exp_id = experiment_id_for(definition)
        variants = [variant_for_unit(exp_id, u, seed) for u in sorted(units)]
        if variants.count("control") >= min_n and \
                variants.count("treatment") >= min_n:
            return seed
    raise AssertionError("no balanced deterministic seed found")


def make_validated_knowledge(tmp_path: Path, *, scope: str = SCOPE,
                             hypothesis: str | None = None,
                             metric: str = "views"):
    """REAL Stage 6 → Stage 7 pipeline → ONE validated knowledge record.

    DETERMINISTIC FIXTURE evidence — clearly distinguished from real
    repository data (the real repository may legitimately hold zero
    validated knowledge; it is never fabricated here either)."""
    _, analytics, learning = make_env(tmp_path)
    knowledge = custom_pipeline(learning, analytics, scope=scope,
                                metric=metric, hypothesis=hypothesis)
    return learning, analytics, knowledge


def compile_candidate(policy_store: PolicyStore, learning, knowledge,
                      *, scope: str | None = None, **overrides) -> dict:
    return create_candidate(
        policy_store, learning.path,
        knowledge_refs=[f"{knowledge['knowledge_id']}"
                        f"@{knowledge['knowledge_version']}"],
        scope=scope or knowledge["scope"], rationale=RATIONALE, now=NOW,
        **overrides)


def custom_pipeline(learning, analytics, *, scope: str = SCOPE,
                   treatment_variant: str = "treatment", metric="views",
                   control_value=1000.0, treatment_value=5000.0,
                   units=None, hypothesis=None, min_n: int = 2,
                   expect_validated: bool = True, seed: str | None = None) -> dict:
    """Candidate → experiment → approve → assign → measure → evaluate →
    curate, with explicit variant/metric/min-n control — used to build
    validated AND conflicting/rejected knowledge fixtures."""
    units = sorted(units) if units else [f"vid{i:03d}" for i in range(6)]
    candidate = learn_candidate(
        learning, hypothesis=hypothesis or f"units differ ({treatment_variant})",
        scope=scope, evidence=[{"kind": "measurement", "ref": "seeded",
                                "video_id": units[0]}],
        counterexamples=[], now=NOW)["candidate"]
    # an explicitly provided seed skips the balance search (used for
    # INSUFFICIENT-evidence fixtures, where the declared minimum can
    # never be met and no balanced split exists)
    seed = seed or balanced_seed(learning, candidate, metric=metric,
                                 units=units, min_n=min_n)
    experiment = create_experiment(
        learning, candidate_id=candidate["candidate_id"],
        hypothesis=candidate["hypothesis"], metric=metric,
        control_variant="control", treatment_variant=treatment_variant,
        population={"units": units}, assignment={"seed": seed},
        success_criterion={"min_control_n": min_n, "min_treatment_n": min_n,
                           "min_effect": 1.0}, now=NOW)["experiment"]
    approve_experiment(learning, experiment["experiment_id"], now=NOW)
    assignments = assign_units(learning, experiment["experiment_id"],
                               units, now=NOW)["assignments"]
    for index, entry in enumerate(
            sorted(assignments, key=lambda a: a["unit_id"])):
        base = treatment_value if entry["variant"] == treatment_variant \
            else control_value
        seed_video(analytics, entry["unit_id"],
                   **{metric: base + (index % 3) * 7.0})
    evaluation = evaluate_experiment(learning, analytics,
                                     experiment["experiment_id"],
                                     now=NOW)["evaluation"]
    if expect_validated:
        assert evaluation["status"] == "validated", evaluation["status"]
    # the Stage 7 curation boundary REFUSES rejected/insufficient
    # evaluations — a truthful structured refusal, never a fabricated
    # knowledge record
    try:
        return curate_candidate(
            learning, candidate_id=candidate["candidate_id"],
            evaluation_id=evaluation["evaluation_id"], now=NOW)["knowledge"]
    except LearningError:
        assert not expect_validated
        return None



# ---- candidate (§6/§9) ------------------------------------------------------------------


def test_candidate_compiles_deterministically_from_validated_knowledge(
        tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    first = compile_candidate(store, learning, knowledge)
    assert first["created"] is True
    candidate = first["candidate"]
    assert candidate["candidate_id"].startswith("pcand-")
    assert candidate["status"] == "draft"
    assert candidate["scope"] == SCOPE
    assert candidate["source_knowledge_refs"] == [
        {"knowledge_id": knowledge["knowledge_id"],
         "knowledge_version": knowledge["knowledge_version"]}]
    (rule,) = candidate["proposed_rules"]
    assert rule["kind"] == "variant_preference_v1"
    assert rule["operator"] == "prefer_variant"
    assert rule["action"] == "prefer"
    assert rule["variant"] == "treatment"
    assert rule["comparator_variant"] == "control"
    assert rule["metric"] == "views"
    assert rule["scope"] == SCOPE
    assert rule["source"] == {"knowledge_id": knowledge["knowledge_id"],
                              "knowledge_version": 1}
    assert candidate["content_hash"].startswith("sha256:")
    # deterministic identity: the SAME inputs → the SAME candidate
    second = compile_candidate(store, learning, knowledge)
    assert second["created"] is False and second["duplicate"] is True
    assert second["candidate"]["candidate_id"] == candidate["candidate_id"]
    assert second["candidate"]["content_hash"] == candidate["content_hash"]
    assert len(store.list_candidates()) == 1  # no duplicates


def test_candidate_missing_knowledge_refused(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    with pytest.raises(PolicyError) as excinfo:
        create_candidate(store, learning.path,
                         knowledge_refs=["knw-doesnotexist@1"],
                         scope=SCOPE, rationale=RATIONALE, now=NOW)
    assert excinfo.value.code == "policy_source_missing"
    # a missing learning database is a truthful structured result
    with pytest.raises(PolicyError) as excinfo:
        create_candidate(store, tmp_path / "data" / "learning" /
                         "nope.sqlite3", knowledge_refs=["knw-x@1"],
                         scope=SCOPE, rationale=RATIONALE, now=NOW)
    assert excinfo.value.code == "policy_source_missing"


def test_candidate_superseded_knowledge_refused(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    # a newer knowledge version supersedes v1 (history retained, Stage 7 §25)
    row = dict(learning.get_knowledge(knowledge["knowledge_id"]))
    row["knowledge_version"] = 2
    row["evaluation_id"] = "eval-other-confirmation"
    assert learning.insert_knowledge(row=row)
    assert learning.supersede_knowledge(knowledge["knowledge_id"],
                                        below_version=2) == 1
    store = policy_env(tmp_path)
    with pytest.raises(PolicyError) as excinfo:
        compile_candidate(store, learning, knowledge)  # references v1
    assert excinfo.value.code == "policy_source_not_validated"


def test_candidate_from_rejected_or_insufficient_evidence_refused(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    # REJECTED pipeline (treatment measures LOWER)
    custom_pipeline(learning, analytics, control_value=5000.0,
                    treatment_value=1000.0, expect_validated=False)
    assert learning.query_knowledge(status=None) == []  # never curated
    store = policy_env(tmp_path)
    # with NO validated knowledge in existence, NOTHING can be referenced
    # → the policy compiler fails CLOSED
    with pytest.raises(PolicyError) as excinfo:
        create_candidate(store, learning.path,
                         knowledge_refs=["knw-any@1"], scope=SCOPE,
                         rationale=RATIONALE, now=NOW)
    assert excinfo.value.code == "policy_source_missing"
    # INSUFFICIENT evidence: equally never curated → equally unreferenceable
    _, analytics2, learning2 = make_env(tmp_path / "insufficient")
    custom_pipeline(learning2, analytics2, min_n=5, expect_validated=False,
                    seed="seed-0")  # min_n=5 is unreachable with 6 units
    assert learning2.query_knowledge(status=None) == []


def test_candidate_scope_mismatch_never_broadens(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    # a scoped knowledge record must NEVER silently become a global policy
    with pytest.raises(PolicyError) as excinfo:
        compile_candidate(store, learning, knowledge, scope="global")
    assert excinfo.value.code == "policy_scope_invalid"
    with pytest.raises(PolicyError) as excinfo:
        compile_candidate(store, learning, knowledge, scope="topic:other")
    assert excinfo.value.code == "policy_scope_invalid"
    assert store.list_candidates() == []


def test_candidate_scope_grammar_enforced():
    with pytest.raises(PolicyError) as excinfo:
        validate_scope("all_content")
    assert excinfo.value.code == "policy_scope_invalid"
    with pytest.raises(PolicyError):
        validate_scope("dimension:invented")
    with pytest.raises(PolicyError):
        validate_scope("format:")
    assert validate_scope("global") == "global"
    assert validate_scope("format:short_explainer") == "format:short_explainer"
    assert validate_scope("topic:history") == "topic:history"


def test_candidate_threshold_semantics_are_unsupported(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    # associational knowledge cannot establish thresholds — a
    # greater_than rule is REJECTED, never silently coerced
    threshold_rule = {
        "rule_id": "rul-" + "0" * 16, "kind": "variant_preference_v1",
        "metric": "views", "operator": "greater_than", "value": 1000,
        "scope": SCOPE, "action": "prefer", "priority": 1,
        "source": {"knowledge_id": knowledge["knowledge_id"],
                   "knowledge_version": 1}}
    with pytest.raises(PolicyError) as excinfo:
        validate_rule(threshold_rule)
    assert excinfo.value.code == "policy_rule_invalid"
    with pytest.raises(PolicyError) as excinfo:
        create_candidate(store, learning.path,
                         knowledge_refs=[f"{knowledge['knowledge_id']}@1"],
                         scope=SCOPE, rationale=RATIONALE,
                         action="always_use", now=NOW)
    assert excinfo.value.code == "policy_rule_invalid"


def test_candidate_non_positive_effect_is_unsupported(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    # a fixture knowledge row whose validated effect is NOT positive:
    # it must NOT become a prefer rule (no invented causal conclusions)
    row = {
        "knowledge_id": "knw-fixture-negative-effect",
        "knowledge_version": 1, "candidate_id": "cand-fixture",
        "experiment_id": "exp-fixture", "evaluation_id": "eval-fixture",
        "statement": "fixture statement", "scope": SCOPE, "evidence": [],
        "validation": {"evaluation_status": "validated", "effect": -5.0,
                       "control_n": 2, "treatment_n": 2, "holdout": None,
                       "dataset_fingerprint": "fp-fixture"},
        "status": "active", "created_at": NOW}
    assert learning.insert_knowledge(row=row)
    store = policy_env(tmp_path)
    with pytest.raises(PolicyError) as excinfo:
        create_candidate(store, learning.path,
                         knowledge_refs=["knw-fixture-negative-effect@1"],
                         scope=SCOPE, rationale=RATIONALE, now=NOW)
    assert excinfo.value.code == "policy_unsupported"


def test_candidate_conflicting_knowledge_refused(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    store = policy_env(tmp_path)
    knowledge_b = custom_pipeline(learning, analytics,
                                  hypothesis="first confirmation")
    # a controlled FIXTURE knowledge record that prefers a DIFFERENT
    # variant for the same metric + scope. The real Stage 7 evaluator
    # hardcodes the control/treatment vocabulary, so the conflicting
    # record is inserted through the REAL LearningStore API (same
    # schema, same immutability rules) — clearly marked fixture evidence.
    definition = {
        "candidate_id": "cand-fixture-variant-c",
        "hypothesis": "fixture: prefer variant_c",
        "metric": "views", "unit": "video",
        "control_variant": "control", "treatment_variant": "variant_c",
        "population": {"kind": "explicit", "units": ["vidF1", "vidF2",
                                                     "vidF3", "vidF4"]},
        "evaluation_window": None,
        "success_criterion": {"direction": "treatment_greater_than_control",
                              "min_effect": 1.0, "min_control_n": 2,
                              "min_treatment_n": 2},
        "assignment": {"method": "sha256_unit_hash_v1", "seed": "fixture"},
        "holdout": None, "validation_method": "none",
    }
    assert learning.insert_experiment(row={
        "experiment_id": "exp-fixture-variant-c",
        "candidate_id": "cand-fixture-variant-c",
        "definition": definition, "status": "evaluated",
        "created_at": NOW, "updated_at": NOW})
    assert learning.insert_knowledge(row={
        "knowledge_id": "knw-fixture-variant-c", "knowledge_version": 1,
        "candidate_id": "cand-fixture-variant-c",
        "experiment_id": "exp-fixture-variant-c",
        "evaluation_id": "eval-fixture-variant-c",
        "statement": "fixture statement (variant_c preferred)",
        "scope": SCOPE, "evidence": [],
        "validation": {"evaluation_status": "validated", "effect": 12.0,
                       "control_n": 2, "treatment_n": 2, "holdout": None,
                       "dataset_fingerprint": "fp-fixture-c"},
        "status": "active", "created_at": NOW})
    refs = [f"{knowledge_b['knowledge_id']}@1", "knw-fixture-variant-c@1"]
    with pytest.raises(PolicyError) as excinfo:
        create_candidate(store, learning.path, knowledge_refs=refs,
                         scope=SCOPE, rationale=RATIONALE, now=NOW)
    assert excinfo.value.code == "policy_conflict"
    assert store.list_candidates() == []


def test_candidate_dry_run_does_not_persist(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    report = compile_candidate(store, learning, knowledge, dry_run=True)
    assert report["dry_run"] is True
    compiled = report["candidate"]
    assert store.list_candidates() == []
    # the SAME compile persisted afterwards matches the dry-run identity
    stored = compile_candidate(store, learning, knowledge)["candidate"]
    assert stored["candidate_id"] == compiled["candidate_id"]
    assert stored["content_hash"] == compiled["content_hash"]


def test_duplicate_candidate_creation_is_concurrency_safe(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    errors: list[PolicyError] = []
    outcomes = []

    def worker():
        try:
            worker_store = PolicyStore(store.path)
            result = compile_candidate(worker_store, learning, knowledge)
            outcomes.append(result["candidate"]["candidate_id"])
            worker_store.close()
        except PolicyError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    assert len(set(outcomes)) == 1                    # same identity
    assert len(store.list_candidates()) == 1          # no duplicates


# ---- approval (§7) ------------------------------------------------------------------------


def test_promotion_requires_explicit_approval(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    # NO automatic approval, NO hidden fallback: a draft candidate
    # cannot be promoted
    with pytest.raises(PolicyError) as excinfo:
        promote_candidate(store, learning.path,
                          candidate_id=candidate["candidate_id"], now=NOW)
    assert excinfo.value.code == "policy_approval_required"
    assert store.list_policies() == []


def test_double_approval_is_idempotent(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    first = approve_candidate(store, candidate_id=candidate["candidate_id"],
                              approved_by="operator-a", now=NOW)
    assert first["created"] is True
    assert first["candidate"]["status"] == "approved"
    second = approve_candidate(store, candidate_id=candidate["candidate_id"],
                               approved_by="operator-b", now=NOW)
    assert second["created"] is False
    assert second["already_approved"] is True
    # exactly ONE durable approval record; a double approval never
    # duplicates anything
    assert len(store.list_candidates()) == 1
    assert store.get_approval(candidate["candidate_id"])["approved_by"] == \
        "operator-a"
    assert store.summary()["approvals"] == 1


def test_approval_records_operator_action_and_time(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    report = approve_candidate(store,
                               candidate_id=candidate["candidate_id"],
                               approved_by="operator-a", now=NOW)
    approval = report["approval"]
    # exactly WHICH candidate, WHICH knowledge, WHICH operator, WHEN
    assert approval["candidate_id"] == candidate["candidate_id"]
    assert approval["decision"] == "approved"
    assert approval["approved_by"] == "operator-a"
    assert approval["approved_at"] == NOW
    assert candidate["source_knowledge_refs"][0]["knowledge_id"] == \
        knowledge["knowledge_id"]


def test_approval_of_rejected_candidate_refused(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    reject_candidate(store, candidate_id=candidate["candidate_id"],
                     reason="evidence too weak", now=NOW)
    with pytest.raises(PolicyError) as excinfo:
        approve_candidate(store, candidate_id=candidate["candidate_id"],
                          approved_by="operator-a", now=NOW)
    assert excinfo.value.code == "policy_invalid_candidate"
    # terminal: a rejected candidate can never be promoted
    with pytest.raises(PolicyError) as excinfo:
        promote_candidate(store, learning.path,
                          candidate_id=candidate["candidate_id"], now=NOW)
    assert excinfo.value.code == "policy_invalid_candidate"
    assert store.list_policies() == []


def test_approval_of_promoted_candidate_refused(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    with pytest.raises(PolicyError) as excinfo:
        approve_candidate(store, candidate_id=candidate["candidate_id"],
                          approved_by="operator-b", now=NOW)
    assert excinfo.value.code == "policy_already_approved"


def test_concurrent_approval_creates_single_record(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    errors: list[PolicyError] = []

    def worker():
        try:
            worker_store = PolicyStore(store.path)
            approve_candidate(worker_store,
                              candidate_id=candidate["candidate_id"],
                              approved_by="operator", now=NOW)
            worker_store.close()
        except PolicyError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    assert store.summary()["approvals"] == 1
    assert store.get_candidate(candidate["candidate_id"])["status"] == \
        "approved"


# ---- promotion (§5/§9/§11) ----------------------------------------------------------------


def test_promotion_creates_immutable_versioned_policy(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    report = promote_candidate(store, learning.path,
                               candidate_id=candidate["candidate_id"],
                               now=NOW)
    assert report["created"] is True
    policy = report["policy"]
    assert policy["policy_id"].startswith("pol-")
    assert policy["policy_version"] == 1
    assert policy["status"] == "approved"
    assert policy["scope"] == SCOPE
    assert policy["candidate_id"] == candidate["candidate_id"]
    assert policy["content_hash"].startswith("sha256:")
    assert policy["previous_policy_id"] is None
    assert policy["previous_policy_version"] is None
    assert policy["activated_at"] is None
    assert policy["source_knowledge_refs"] == candidate[
        "source_knowledge_refs"]
    # deterministic content hash: an independent recompile of the SAME
    # content produces the SAME identity
    recompiled = promote_candidate(
        store, learning.path, candidate_id=candidate["candidate_id"],
        now=NOW)["policy"]
    assert recompiled["policy_id"] == policy["policy_id"]
    assert recompiled["content_hash"] == policy["content_hash"]
    assert len(store.list_policies()) == 1


def test_promotion_dry_run_does_not_persist(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    report = promote_candidate(store, learning.path,
                               candidate_id=candidate["candidate_id"],
                               now=NOW, dry_run=True)
    assert report["dry_run"] is True
    assert store.list_policies() == []
    assert store.get_candidate(candidate["candidate_id"])["status"] == \
        "approved"
    # the persisted promotion afterwards matches the dry-run identity
    stored = promote_candidate(store, learning.path,
                               candidate_id=candidate["candidate_id"],
                               now=NOW)["policy"]
    assert stored["policy_id"] == report["policy"]["policy_id"]
    assert stored["content_hash"] == report["policy"]["content_hash"]


def test_concurrent_promotion_single_identity(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    errors: list[PolicyError] = []
    outcomes = []

    def worker():
        try:
            worker_store = PolicyStore(store.path)
            report = promote_candidate(worker_store, learning.path,
                                       candidate_id=candidate["candidate_id"],
                                       now=NOW)
            outcomes.append((report["policy"]["policy_id"],
                             report["policy"]["policy_version"]))
            worker_store.close()
        except PolicyError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    assert len(set(outcomes)) == 1                    # ONE durable identity
    assert len(store.list_policies()) == 1            # ONE policy row
    assert store.list_policies()[0]["policy_version"] == 1


def test_version_increment_and_previous_binding(tmp_path):
    config, analytics, learning = make_env(tmp_path)
    store = policy_env(tmp_path)
    # a second, independent confirmation of a DIFFERENT experiment in
    # the same scope → different rule provenance → a new policy version
    knowledge_1 = custom_pipeline(learning, analytics,
                                  hypothesis="first confirmation")
    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    assert knowledge_1["knowledge_id"] != knowledge_2["knowledge_id"]

    def promote_for(knowledge):
        candidate = compile_candidate(store, learning, knowledge)["candidate"]
        approve_candidate(store, candidate_id=candidate["candidate_id"],
                          approved_by="operator-a", now=NOW)
        return promote_candidate(store, learning.path,
                                 candidate_id=candidate["candidate_id"],
                                 now=NOW)["policy"]

    policy_1 = promote_for(knowledge_1)
    policy_2 = promote_for(knowledge_2)
    assert policy_1["policy_version"] == 1
    assert policy_2["policy_version"] == 2
    assert policy_2["policy_id"] != policy_1["policy_id"]  # new content
    assert policy_2["previous_policy_id"] == policy_1["policy_id"]
    assert policy_2["previous_policy_version"] == 1
    # history retained: BOTH versions still exist
    assert len(store.list_policies()) == 2


def test_promotion_revalidates_source_knowledge(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    # the knowledge gets superseded AFTER approval → promotion must
    # fail CLOSED (it re-validates the exact recorded version)
    row = dict(learning.get_knowledge(knowledge["knowledge_id"]))
    row["knowledge_version"] = 2
    row["evaluation_id"] = "eval-newer-confirmation"
    assert learning.insert_knowledge(row=row)
    assert learning.supersede_knowledge(knowledge["knowledge_id"],
                                        below_version=2) == 1
    with pytest.raises(PolicyError) as excinfo:
        promote_candidate(store, learning.path,
                          candidate_id=candidate["candidate_id"], now=NOW)
    assert excinfo.value.code == "policy_source_not_validated"
    assert store.list_policies() == []


def test_policy_content_is_immutable_after_creation(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    before = dict(store.list_policies()[0])
    # activate + retire: statuses change, content NEVER does
    activate_policy(store, policy_id=before["policy_id"], now=NOW)
    retire_policy(store, policy_id=before["policy_id"], now=NOW)
    after = dict(store.list_policies()[0])
    for field in ("policy_id", "policy_version", "scope", "rules",
                  "source_knowledge_refs", "rationale", "candidate_id",
                  "content_hash", "created_at"):
        assert after[field] == before[field], field
    assert after["status"] == "retired"


# ---- activation (§12) ---------------------------------------------------------------------


def test_activation_is_explicit_and_idempotent(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    (policy,) = store.list_policies()
    assert policy["status"] == "approved"  # existing ≠ active (§12)
    report = activate_policy(store, policy_id=policy["policy_id"], now=NOW)
    assert report["activated"] is True
    assert report["policy"]["status"] == "active"
    assert report["policy"]["activated_at"] == NOW
    # duplicate activation is deterministic (idempotent)
    again = activate_policy(store, policy_id=policy["policy_id"], now=NOW)
    assert again["activated"] is False
    assert again["policy"]["policy_id"] == policy["policy_id"]
    assert len(store.list_activations()) == 1


def test_only_one_active_policy_per_scope(tmp_path):
    config, analytics, learning = make_env(tmp_path)
    store = policy_env(tmp_path)

    def promote_for(knowledge):
        candidate = compile_candidate(store, learning, knowledge)["candidate"]
        approve_candidate(store, candidate_id=candidate["candidate_id"],
                          approved_by="operator-a", now=NOW)
        return promote_candidate(store, learning.path,
                                 candidate_id=candidate["candidate_id"],
                                 now=NOW)["policy"]

    knowledge_1 = custom_pipeline(learning, analytics,
                                  hypothesis="first confirmation")
    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    policy_1 = promote_for(knowledge_1)
    policy_2 = promote_for(knowledge_2)
    activate_policy(store, policy_id=policy_1["policy_id"], now=NOW)
    activate_policy(store, policy_id=policy_2["policy_id"], now=NOW)
    active = [p for p in store.list_policies() if p["status"] == "active"]
    assert len(active) == 1 and active[0]["policy_id"] == \
        policy_2["policy_id"]
    superseded = [p for p in store.list_policies()
                  if p["status"] == "superseded"]
    assert len(superseded) == 1 and superseded[0]["policy_id"] == \
        policy_1["policy_id"]
    # historical policies are NEVER deleted
    assert len(store.list_policies()) == 2


def test_activation_conflicts_fail_closed(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    # unknown policy
    with pytest.raises(PolicyError) as excinfo:
        activate_policy(store, policy_id="pol-doesnotexist", now=NOW)
    assert excinfo.value.code == "policy_activation_conflict"
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    (policy,) = store.list_policies()
    # a nonexistent version of an existing policy
    with pytest.raises(PolicyError) as excinfo:
        activate_policy(store, policy_id=policy["policy_id"],
                        policy_version=9, now=NOW)
    assert excinfo.value.code == "policy_version_conflict"
    activate_policy(store, policy_id=policy["policy_id"], now=NOW)
    retire_policy(store, policy_id=policy["policy_id"], now=NOW)
    # a retired policy can never be re-activated
    with pytest.raises(PolicyError) as excinfo:
        activate_policy(store, policy_id=policy["policy_id"], now=NOW)
    assert excinfo.value.code == "policy_activation_conflict"


def test_concurrent_activation_leaves_one_active_policy(tmp_path):
    config, analytics, learning = make_env(tmp_path)
    store = policy_env(tmp_path)

    def promote_for(knowledge):
        candidate = compile_candidate(store, learning, knowledge)["candidate"]
        approve_candidate(store, candidate_id=candidate["candidate_id"],
                          approved_by="operator-a", now=NOW)
        return promote_candidate(store, learning.path,
                                 candidate_id=candidate["candidate_id"],
                                 now=NOW)["policy"]

    knowledge_1 = custom_pipeline(learning, analytics,
                                  hypothesis="first confirmation")
    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    policy_1 = promote_for(knowledge_1)
    policy_2 = promote_for(knowledge_2)
    barrier = threading.Barrier(2)
    errors: list[PolicyError] = []

    def worker(policy_id):
        try:
            worker_store = PolicyStore(store.path)
            barrier.wait(30)
            activate_policy(worker_store, policy_id=policy_id, now=NOW)
            worker_store.close()
        except PolicyError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(p["policy_id"],))
               for p in (policy_1, policy_2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    active = [p for p in store.list_policies() if p["status"] == "active"]
    assert len(active) == 1  # the DATABASE constraint is the arbiter


# ---- diff (§13) ----------------------------------------------------------------------------


def test_diff_first_policy_is_explicit(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    report = diff_for_activation(store,
                                 candidate_id=candidate["candidate_id"])
    assert report["ok"] is True
    assert report["baseline"] is None
    assert "FIRST policy" in report["baseline_note"]
    assert report["material_change"] is True
    assert len(report["rules"]["added"]) == 1


def test_diff_added_removed_and_changed_rules(tmp_path):
    config, analytics, learning = make_env(tmp_path)
    store = policy_env(tmp_path)

    def candidate_for(knowledge, **overrides):
        compiled = compile_candidate(store, learning, knowledge,
                                     **overrides)["candidate"]
        approve_candidate(store, candidate_id=compiled["candidate_id"],
                          approved_by="operator-a", now=NOW)
        promote_candidate(store, learning.path,
                          candidate_id=compiled["candidate_id"], now=NOW)
        return compiled

    knowledge_1 = custom_pipeline(learning, analytics,
                                  hypothesis="first confirmation")
    candidate_1 = candidate_for(knowledge_1)
    activate_policy(store, policy_id=store.list_policies()[0]["policy_id"],
                    now=NOW)
    # unchanged candidate → no material change
    same = diff_for_activation(store,
                               candidate_id=candidate_1["candidate_id"])
    assert same["material_change"] is False
    assert same["rules"]["added"] == [] and same["rules"]["removed"] == []
    # a second candidate → the old rule is removed, the new one added
    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    candidate_2 = candidate_for(knowledge_2, priority=2)
    report = diff_for_activation(store,
                                 candidate_id=candidate_2["candidate_id"])
    assert report["baseline"]["policy_id"] == \
        store.active_policy(SCOPE)["policy_id"]
    assert len(report["rules"]["added"]) == 1
    assert len(report["rules"]["removed"]) == 1
    # source-knowledge change is surfaced explicitly
    assert report["source_knowledge"]["added"] == [
        {"knowledge_id": knowledge_2["knowledge_id"], "knowledge_version": 1}]
    assert report["source_knowledge"]["removed"] == [
        {"knowledge_id": knowledge_1["knowledge_id"], "knowledge_version": 1}]


def test_diff_scope_change_gets_its_own_baseline(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    activate_policy(store, policy_id=store.list_policies()[0]["policy_id"],
                    now=NOW)
    # a candidate in a DIFFERENT scope (from its own separate fixture
    # learning store) diffs against THAT scope's baseline — never
    # against another scope's policy
    learning_t, _, knowledge_t = make_validated_knowledge(
        tmp_path / "other-scope", scope="topic:history")
    candidate_t = create_candidate(
        store, learning_t.path,
        knowledge_refs=[f"{knowledge_t['knowledge_id']}@1"],
        scope="topic:history", rationale=RATIONALE, now=NOW)["candidate"]
    report = diff_for_activation(store,
                                 candidate_id=candidate_t["candidate_id"])
    assert report["scope"] == "topic:history"
    assert report["baseline"] is None
    assert "FIRST policy" in report["baseline_note"]
    # the OTHER scope's active policy is untouched
    assert store.active_policy(SCOPE)["status"] == "active"
    assert store.active_policy("topic:history") is None


# ---- rollback (§12) ------------------------------------------------------------------------


def test_rollback_to_prior_version_preserves_history(tmp_path):
    config, analytics, learning = make_env(tmp_path)
    store = policy_env(tmp_path)

    def promote_and_activate(knowledge):
        candidate = compile_candidate(store, learning, knowledge)["candidate"]
        approve_candidate(store, candidate_id=candidate["candidate_id"],
                          approved_by="operator-a", now=NOW)
        policy = promote_candidate(
            store, learning.path,
            candidate_id=candidate["candidate_id"], now=NOW)["policy"]
        activate_policy(store, policy_id=policy["policy_id"], now=NOW)
        return policy

    knowledge_1 = custom_pipeline(learning, analytics,
                                  hypothesis="first confirmation")
    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    policy_1 = promote_and_activate(knowledge_1)
    policy_2 = promote_and_activate(knowledge_2)
    assert store.active_policy(SCOPE)["policy_id"] == policy_2["policy_id"]

    report = rollback_policy(store, policy_id=policy_1["policy_id"],
                             policy_version=1, now=NOW)
    assert report["action"] == "rollback"
    assert report["policy"]["status"] == "active"
    assert store.active_policy(SCOPE)["policy_id"] == policy_1["policy_id"]
    assert store.get_policy(policy_2["policy_id"])["status"] == "superseded"
    # history retained: both versions + the full audit trail
    assert len(store.list_policies()) == 2
    actions = [a["action"] for a in store.list_activations()]
    assert actions.count("activate") == 2 and actions.count("rollback") == 1


def test_rollback_invalid_cases(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    with pytest.raises(PolicyError) as excinfo:
        rollback_policy(store, policy_id="pol-doesnotexist", now=NOW)
    assert excinfo.value.code == "policy_rollback_invalid"
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    (policy,) = store.list_policies()
    # an APPROVED (never active) policy is not a rollback target
    with pytest.raises(PolicyError) as excinfo:
        rollback_policy(store, policy_id=policy["policy_id"], now=NOW)
    assert excinfo.value.code == "policy_rollback_invalid"
    activate_policy(store, policy_id=policy["policy_id"], now=NOW)
    # the ACTIVE policy cannot be rolled back to itself
    with pytest.raises(PolicyError) as excinfo:
        rollback_policy(store, policy_id=policy["policy_id"], now=NOW)
    assert excinfo.value.code == "policy_rollback_invalid"


def test_concurrent_rollback_and_activation_stay_consistent(tmp_path):
    config, analytics, learning = make_env(tmp_path)
    store = policy_env(tmp_path)

    def promote_and_activate(knowledge):
        candidate = compile_candidate(store, learning, knowledge)["candidate"]
        approve_candidate(store, candidate_id=candidate["candidate_id"],
                          approved_by="operator-a", now=NOW)
        policy = promote_candidate(
            store, learning.path,
            candidate_id=candidate["candidate_id"], now=NOW)["policy"]
        activate_policy(store, policy_id=policy["policy_id"], now=NOW)
        return policy

    knowledge_1 = custom_pipeline(learning, analytics,
                                  hypothesis="first confirmation")
    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    policy_1 = promote_and_activate(knowledge_1)
    policy_2 = promote_and_activate(knowledge_2)
    barrier = threading.Barrier(2)

    def activator():
        worker_store = PolicyStore(store.path)
        barrier.wait(30)
        activate_policy(worker_store, policy_id=policy_2["policy_id"],
                        now=NOW)
        worker_store.close()

    def rollbacker():
        worker_store = PolicyStore(store.path)
        barrier.wait(30)
        rollback_policy(worker_store, policy_id=policy_1["policy_id"],
                        now=NOW)
        worker_store.close()

    threads = [threading.Thread(target=activator),
               threading.Thread(target=rollbacker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    # whatever the interleaving: exactly ONE active policy, full history
    active = [p for p in store.list_policies() if p["status"] == "active"]
    assert len(active) == 1
    assert len(store.list_policies()) == 2


# ---- Hermes read-only surface (§14) ----------------------------------------------------------


def test_read_active_policy_returns_the_active_policy(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    (policy,) = store.list_policies()
    store.close()

    # BEFORE activation: nothing active yet
    report = read_active_policy(store.path, SCOPE)
    assert report["ok"] is True and report["policy"] is None
    assert "no active policy" in report["note"]

    store = PolicyStore(store.path)
    activate_policy(store, policy_id=policy["policy_id"], now=NOW)
    store.close()

    report = read_active_policy(store.path, SCOPE)
    assert report["ok"] is True
    active = report["policy"]
    assert active["policy_id"] == policy["policy_id"]
    assert active["policy_version"] == 1
    assert active["status"] == "active"
    assert active["rules"][0]["rule_id"] == policy["rules"][0]["rule_id"]
    # deterministic scope filter: other scopes see nothing
    miss = read_active_policy(store.path, "topic:other")
    assert miss["ok"] is True and miss["policy"] is None


def test_read_active_policy_missing_database_is_truthful(tmp_path):
    report = read_active_policy(tmp_path / "no" / "such.sqlite3", "global")
    assert report["ok"] is True
    assert report["policy"] is None
    assert "no policy database" in report["note"]


def test_hermes_read_does_not_mutate_the_policy_db(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    activate_policy(store, policy_id=store.list_policies()[0]["policy_id"],
                    now=NOW)
    store.close()  # writers closed: WAL checkpointed into the main file

    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    before = digest(store.path)
    read_active_policy(store.path, SCOPE)
    read_active_policy(store.path, "topic:other")
    assert digest(store.path) == before  # READ-ONLY: not one byte changed


def test_readonly_connection_cannot_write_policy(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    store.close()
    connection = sqlite3.connect(
        f"file:{store.path.as_posix()}?mode=ro", uri=True, timeout=10.0)
    try:
        # the OS/database-level boundary: even RAW SQL cannot mutate
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "UPDATE policies SET status = 'active'").fetchall()
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "INSERT INTO policies (policy_id, policy_version, scope,"
                " rules, source_knowledge_refs, rationale, candidate_id,"
                " content_hash, status, created_at)"
                " VALUES ('pol-h', 1, 's', '[]', '[]', 'r', 'c', 'h',"
                " 'active', 'now')").fetchall()
    finally:
        connection.close()


# ---- safety invariants (§22/§23) --------------------------------------------------------------


def test_policy_operations_never_mutate_source_stores(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    knowledge_1 = custom_pipeline(learning, analytics,
                                  hypothesis="first confirmation")
    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    # production-artifact stand-ins (the §22 watched set)
    data_dir = tmp_path / "data"
    state_path = data_dir / "runs" / "run-x" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text('{"run_id": "run-x"}', encoding="utf-8")
    ledger_path = data_dir / "publishing" / "ledger.sqlite3"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_bytes(b"ledger-bytes")
    research_path = data_dir / "research" / "res-x.json"
    research_path.parent.mkdir(parents=True, exist_ok=True)
    research_path.write_text('{"research_id": "res-x"}', encoding="utf-8")

    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    # close the fixture writers → WAL checkpointed; byte digests are
    # then a complete immutability proof
    learning.close()
    analytics.close()
    watched = {
        "state": digest(state_path),
        "ledger": digest(ledger_path),
        "research": digest(research_path),
        "analytics_db": digest(data_dir / "analytics" / "analytics.sqlite3"),
        "learning_db": digest(data_dir / "learning" / "learning.sqlite3"),
    }

    # the FULL policy lifecycle (all mutating policy operations)
    store = policy_env(tmp_path)

    def lifecycle(knowledge):
        candidate = compile_candidate(store, learning, knowledge)["candidate"]
        approve_candidate(store, candidate_id=candidate["candidate_id"],
                          approved_by="operator-a", now=NOW)
        return promote_candidate(store, learning.path,
                                 candidate_id=candidate["candidate_id"],
                                 now=NOW)["policy"]

    policy_1 = lifecycle(knowledge_1)
    policy_2 = lifecycle(knowledge_2)
    activate_policy(store, policy_id=policy_1["policy_id"], now=NOW)
    activate_policy(store, policy_id=policy_2["policy_id"], now=NOW)
    rollback_policy(store, policy_id=policy_1["policy_id"], now=NOW)
    read_active_policy(store.path, SCOPE)  # the Hermes-equivalent read
    store.close()

    assert digest(state_path) == watched["state"]
    assert digest(ledger_path) == watched["ledger"]
    assert digest(research_path) == watched["research"]
    # the policy layer reads learning/analytics ONLY through mode=ro
    # connections — not one byte changed
    assert digest(data_dir / "analytics" / "analytics.sqlite3") == \
        watched["analytics_db"]
    assert digest(data_dir / "learning" / "learning.sqlite3") == \
        watched["learning_db"]


def test_policy_promotion_does_not_mutate_source_knowledge(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    before = learning.get_knowledge(knowledge["knowledge_id"])
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    activate_policy(store, policy_id=store.list_policies()[0]["policy_id"],
                    now=NOW)
    after = learning.get_knowledge(knowledge["knowledge_id"])
    assert after == before  # validation/activation never touch knowledge


def test_no_automatic_policy_path_exists():
    """§23: the analytics and learning layers can NEVER reach the policy
    domain — no import, no call, no callback (fail closed by
    construction)."""
    layers = [
        Path("src/ayce/analytics"),
        Path("src/ayce/learning"),
        Path("src/ayce/publishing"),
    ]
    for directory in layers:
        for module in directory.glob("*.py"):
            source = module.read_text(encoding="utf-8")
            assert "ayce.policy" not in source, module
            assert "PolicyStore" not in source, module
            assert "promote_candidate" not in source, module
            assert "activate_policy" not in source, module


# ---- restart (§21) -----------------------------------------------------------------------


def test_restart_preserves_policy_history(tmp_path):
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    (policy,) = store.list_policies()
    activate_policy(store, policy_id=policy["policy_id"], now=NOW)
    store.close()
    # completely NEW store objects over the same files (process restart)
    store2 = PolicyStore(store.path)
    summary = store2.summary()
    assert summary["candidates"] == 1
    assert summary["approvals"] == 1
    assert summary["policies"] == 1
    assert summary["activations"] == 1
    assert store2.get_candidate(candidate["candidate_id"])["status"] == \
        "promoted"
    assert store2.get_approval(candidate["candidate_id"])["approved_by"] == \
        "operator-a"
    assert store2.active_policy(SCOPE)["policy_id"] == policy["policy_id"]
    store2.close()
    # the read-only Hermes view survives the restart too
    assert read_active_policy(store.path, SCOPE)["policy"]["policy_id"] == \
        policy["policy_id"]


# ---- CLI (§19) ------------------------------------------------------------------------------


def test_cli_policy_end_to_end(tmp_path, monkeypatch, capsys):
    from ayce import cli
    learning, _, knowledge = make_validated_knowledge(tmp_path)
    learning.close()
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))
    ref = f"{knowledge['knowledge_id']}@{knowledge['knowledge_version']}"

    # 1) dry-run candidate (validation without persistence)
    rc = cli.main(["policy", "candidate", "--json", "--knowledge", ref,
                   "--scope", SCOPE, "--rationale", RATIONALE, "--dry-run"])
    assert rc == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    policy_dir = tmp_path / "data" / "policy"
    assert not policy_dir.exists() or \
        len(PolicyStore(policy_dir / "policy.sqlite3").list_candidates()) == 0

    # 2) candidate
    rc = cli.main(["policy", "candidate", "--json", "--knowledge", ref,
                   "--scope", SCOPE, "--rationale", RATIONALE])
    assert rc == 0
    candidate = json.loads(capsys.readouterr().out)["candidate"]

    # 3) show
    rc = cli.main(["policy", "show", "--json",
                   "--candidate-id", candidate["candidate_id"]])
    assert rc == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["candidate"]["candidate_id"] == candidate["candidate_id"]

    # 4) diff (first policy → explicit no-baseline report)
    rc = cli.main(["policy", "diff", "--json",
                   "--candidate-id", candidate["candidate_id"]])
    assert rc == 0
    diff = json.loads(capsys.readouterr().out)
    assert diff["baseline"] is None and diff["material_change"] is True

    # 5) promote WITHOUT approval → structured refusal (rc 1)
    rc = cli.main(["policy", "promote", "--json",
                   "--candidate-id", candidate["candidate_id"]])
    assert rc == 1
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["error"]["code"] == "policy_approval_required"

    # 6) EXPLICIT approval → 7) promote
    rc = cli.main(["policy", "approve", "--json",
                   "--candidate-id", candidate["candidate_id"],
                   "--approved-by", "operator-a"])
    assert rc == 0
    capsys.readouterr()
    rc = cli.main(["policy", "promote", "--json",
                   "--candidate-id", candidate["candidate_id"]])
    assert rc == 0
    policy = json.loads(capsys.readouterr().out)["policy"]
    assert policy["policy_version"] == 1

    # 8) activate → 9) the Hermes-equivalent read-only view
    rc = cli.main(["policy", "activate", "--json",
                   "--policy-id", policy["policy_id"]])
    assert rc == 0
    capsys.readouterr()
    rc = cli.main(["policy", "active", "--json", "--scope", SCOPE])
    assert rc == 0
    active = json.loads(capsys.readouterr().out)
    assert active["policy"]["policy_id"] == policy["policy_id"]
    assert active["policy"]["status"] == "active"



def test_cli_policy_full_cycle(tmp_path, monkeypatch, capsys):
    """The §28 controlled acceptance exercise over the CLI surface:
    candidate → inspect → approve → promote → inspect → diff → activate
    → read-only retrieval → second version → activate → rollback →
    verify history."""
    from ayce import cli
    config, analytics, learning = make_env(tmp_path)
    knowledge_1 = custom_pipeline(learning, analytics,
                                  hypothesis="first confirmation")
    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    learning.close()
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))
    store = policy_env(tmp_path)

    def promote_cli(knowledge):
        ref = f"{knowledge['knowledge_id']}@1"
        rc = cli.main(["policy", "candidate", "--json", "--knowledge", ref,
                       "--scope", SCOPE, "--rationale", RATIONALE])
        assert rc == 0
        candidate = json.loads(capsys.readouterr().out)["candidate"]
        rc = cli.main(["policy", "approve", "--json",
                       "--candidate-id", candidate["candidate_id"],
                       "--approved-by", "operator-a"])
        assert rc == 0
        capsys.readouterr()
        rc = cli.main(["policy", "promote", "--json",
                       "--candidate-id", candidate["candidate_id"]])
        assert rc == 0
        return json.loads(capsys.readouterr().out)["policy"]

    policy_1 = promote_cli(knowledge_1)
    rc = cli.main(["policy", "activate", "--json",
                   "--policy-id", policy_1["policy_id"]])
    assert rc == 0
    capsys.readouterr()
    policy_2 = promote_cli(knowledge_2)
    rc = cli.main(["policy", "activate", "--json",
                   "--policy-id", policy_2["policy_id"]])
    assert rc == 0
    capsys.readouterr()
    # rollback to v1 through the CLI; history retained
    rc = cli.main(["policy", "rollback", "--json",
                   "--policy-id", policy_1["policy_id"],
                   "--policy-version", "1"])
    assert rc == 0
    rolled = json.loads(capsys.readouterr().out)
    assert rolled["action"] == "rollback"
    assert rolled["policy"]["status"] == "active"
    assert len(store.list_policies()) == 2
    assert store.active_policy(SCOPE)["policy_id"] == policy_1["policy_id"]


def test_cli_policy_active_without_database_is_truthful(tmp_path, monkeypatch,
                                                        capsys):
    from ayce import cli
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))
    rc = cli.main(["policy", "active", "--json", "--scope", "global"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["policy"] is None
    assert "no policy database" in report["note"]
    # a candidate attempt with no knowledge at all fails truthfully
    rc = cli.main(["policy", "candidate", "--json", "--knowledge",
                   "knw-any@1", "--scope", "global",
                   "--rationale", RATIONALE])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["code"] == "policy_source_missing"














