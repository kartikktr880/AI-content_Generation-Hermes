"""Stage 9 — Policy-aware Hermes execution context tests (deterministic
fixtures).

Covers the §24 matrix over the REAL Stage 8 policy store fed by REAL
Stage 6/7 fixture pipelines (no network, no fabricated repository
evidence): no-policy truthfulness (missing/empty store, deterministic
context hash, no fabricated rules), active-policy context (structure,
hashing, scope resolution precedence incl. overlapping scopes and
unsupported scopes), fail-closed verification (tampered content hash,
malformed rules, unreadable store), decision behavior (prefer_variant is
a preference, never a requirement; factual language only), consumption
observability (content-addressed idempotent events, version-distinct
events, rollback changes future consumption, no-policy events truthful),
immutability (policy/learning/analytics byte-identical), and the
no-policy-bypass source scan over the director server.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from ayce.policy import (
    PolicyStore,
    PolicyError,
    activate_policy,
    approve_candidate,
    build_execution_context,
    append_consumption,
    apply_policy_preference,
    consumption_id,
    preferred_variant,
    promote_candidate,
    read_consumptions,
    resolve_active_policy,
)

# reuse the REAL deterministic Stage 6/7/8 fixture helpers
from test_policy import (
    NOW,
    RATIONALE,
    SCOPE,
    compile_candidate,
    custom_pipeline,
    make_env,
    make_validated_knowledge,
    policy_env,
)

CAUSAL_BANNED = ("perform better", "always use", "must use", "guarantee",
                 "causally", "guaranteed", "will perform")


def make_active_policy(tmp_path: Path, *, scope: str = SCOPE,
                       store: PolicyStore | None = None):
    """Full Stage 8 chain → ONE active policy. Returns
    (policy_db_path, learning_db_path, policy_row)."""
    learning, _, knowledge = make_validated_knowledge(tmp_path, scope=scope)
    owns_store = store is None
    store = store or policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    (policy,) = [p for p in store.list_policies() if p["scope"] == scope]
    activate_policy(store, policy_id=policy["policy_id"], now=NOW)
    if owns_store:
        store.close()
    learning.close()
    return store.path, learning.path, policy


def digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---- no policy is a valid, truthful state (§7) -------------------------------


def test_context_without_policy_database_is_truthful_none(tmp_path):
    missing = tmp_path / "no" / "such.sqlite3"
    first = build_execution_context(missing)
    assert first["policy_status"] == "none"
    assert first["policy"] is None
    assert first["resolved"]["resolved_scope"] is None
    assert first["context_hash"].startswith("sha256:")
    # deterministic: the same state → the same context hash
    second = build_execution_context(missing)
    assert second["context_hash"] == first["context_hash"]
    assert second == first  # fully deterministic context, nothing fabricated


def test_context_with_empty_policy_store_is_none(tmp_path):
    store = policy_env(tmp_path)
    store.close()
    context = build_execution_context(store.path, scope_candidates=(SCOPE,))
    assert context["policy_status"] == "none"
    assert context["policy"] is None
    # the none-context hash differs from an active context hash (tested
    # below) and is stable for the same candidates
    again = build_execution_context(store.path, scope_candidates=(SCOPE,))
    assert again["context_hash"] == context["context_hash"]


def test_context_hash_tracks_the_exact_context(tmp_path):
    store_path, learning_path, _ = make_active_policy(tmp_path)
    active = build_execution_context(store_path, scope_candidates=(SCOPE,))
    assert active["policy_status"] == "active"
    # a DIFFERENT resolved state (no policy) → a DIFFERENT context hash
    none_context = build_execution_context(tmp_path / "missing.sqlite3")
    assert none_context["context_hash"] != active["context_hash"]
    # different candidate lists → different resolved context → different hash
    scoped = build_execution_context(store_path,
                                     scope_candidates=(SCOPE,))
    both = build_execution_context(store_path,
                                   scope_candidates=(SCOPE, "global"))
    assert scoped["context_hash"] == both["context_hash"]  # same resolution


# ---- active policy: structured context (§5) ------------------------------------


def test_context_with_active_policy_is_structured_and_hashed(tmp_path):
    store_path, learning_path, policy = make_active_policy(tmp_path)
    context = build_execution_context(store_path, scope_candidates=(SCOPE,))
    assert context["policy_status"] == "active"
    assert context["context_schema_version"] == 1
    consumed = context["policy"]
    assert consumed["policy_id"] == policy["policy_id"]
    assert consumed["policy_version"] == policy["policy_version"]
    assert consumed["scope"] == SCOPE
    assert consumed["policy_content_hash"] == policy["content_hash"]
    assert consumed["source_knowledge_refs"] == policy[
        "source_knowledge_refs"]
    (rule,) = consumed["rules"]
    assert rule["kind"] == "variant_preference_v1"
    assert rule["operator"] == "prefer_variant"
    assert rule["action"] == "prefer"
    assert rule["variant"] == "treatment"
    assert rule["priority"] == 1
    # the operator rationale / free-text knowledge is EXCLUDED (§21: policy
    # context is structured configuration, never prompt-injectable text)
    assert "rationale" not in consumed
    assert "statement" not in json.dumps(consumed)
    assert "created_at" not in consumed
    assert "context_hash" in context


# ---- scope resolution (§6/§22) --------------------------------------------------


def test_scope_resolution_prefers_specific_over_global(tmp_path):
    store = policy_env(tmp_path)
    # an active SCOPED policy and an active GLOBAL policy coexist
    make_active_policy(tmp_path / "scoped", scope=SCOPE, store=store)
    make_active_policy(tmp_path / "glob", scope="global", store=store)
    # explicit precedence: the caller's most-specific-first order decides
    specific = resolve_active_policy(store.path, (SCOPE, "global"))
    assert specific["policy_status"] == "active"
    assert specific["scope"] == SCOPE
    global_only = resolve_active_policy(store.path, ("global",))
    assert global_only["policy_status"] == "active"
    assert global_only["scope"] == "global"
    assert global_only["policy"]["policy_id"] != \
        specific["policy"]["policy_id"]
    # through the full context builder, same behavior
    context = build_execution_context(store.path,
                                      scope_candidates=(SCOPE, "global"))
    assert context["policy"]["scope"] == SCOPE
    context_g = build_execution_context(store.path,
                                        scope_candidates=("global",))
    assert context_g["policy"]["scope"] == "global"
    assert context_g["context_hash"] != context["context_hash"]


def test_scope_resolution_for_overlapping_scopes_is_deterministic(tmp_path):
    store = policy_env(tmp_path)
    make_active_policy(tmp_path / "fmt", scope=SCOPE, store=store)
    make_active_policy(tmp_path / "topic", scope="topic:history",
                       store=store)
    # both remain ACTIVE (overlapping scopes are never merged or modified)
    opened = PolicyStore(store.path)
    assert opened.active_policy(SCOPE)["status"] == "active"
    assert opened.active_policy("topic:history")["status"] == "active"
    opened.close()
    # the FIRST candidate in the explicit order wins — deterministically
    first = resolve_active_policy(store.path, (SCOPE, "topic:history"))
    third = resolve_active_policy(store.path, ("topic:history", SCOPE))
    assert first["scope"] == SCOPE
    assert third["scope"] == "topic:history"
    # repeated resolution is stable
    again = resolve_active_policy(store.path, (SCOPE, "topic:history"))
    assert again["scope"] == SCOPE
    assert again["policy"]["policy_id"] == first["policy"]["policy_id"]


def test_scope_resolution_rejects_unsupported_scope(tmp_path):
    with pytest.raises(PolicyError) as excinfo:
        resolve_active_policy(tmp_path / "missing.sqlite3", ("all_content",))
    assert excinfo.value.code == "policy_scope_invalid"
    with pytest.raises(PolicyError) as excinfo:
        resolve_active_policy(tmp_path / "missing.sqlite3", ())
    assert excinfo.value.code == "policy_scope_unresolved"


# ---- fail-closed verification (§23) -----------------------------------------------


def test_corrupted_active_policy_fails_closed(tmp_path):
    store_path, _, policy = make_active_policy(tmp_path)
    # TAMPER with the immutable policy content behind the store's back
    connection = sqlite3.connect(store_path)
    with connection:
        connection.execute(
            "UPDATE policies SET rules = ? WHERE policy_id = ?",
            (json.dumps([dict(policy["rules"][0], priority=9)]),
             policy["policy_id"]))
    connection.close()
    with pytest.raises(PolicyError) as excinfo:
        build_execution_context(store_path, scope_candidates=(SCOPE,))
    assert excinfo.value.code == "policy_context_invalid"
    # and it is NEVER silently treated as 'no policy'
    with pytest.raises(PolicyError):
        resolve_active_policy(store_path, (SCOPE,))


def test_malformed_rules_fail_closed(tmp_path):
    store_path, _, _policy = make_active_policy(tmp_path)
    connection = sqlite3.connect(store_path)
    with connection:
        connection.execute("UPDATE policies SET rules = 'not-json'")
    connection.close()
    with pytest.raises(PolicyError) as excinfo:
        build_execution_context(store_path, scope_candidates=(SCOPE,))
    assert excinfo.value.code == "policy_context_invalid"


def test_empty_rules_fail_closed(tmp_path):
    store_path, _, _policy = make_active_policy(tmp_path)
    connection = sqlite3.connect(store_path)
    with connection:
        connection.execute("UPDATE policies SET rules = '[]'")
    connection.close()
    with pytest.raises(PolicyError) as excinfo:
        build_execution_context(store_path, scope_candidates=(SCOPE,))
    assert excinfo.value.code == "policy_context_invalid"


def test_unreadable_policy_store_fails_closed(tmp_path):
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"this is not a sqlite database at all")
    with pytest.raises(PolicyError) as excinfo:
        build_execution_context(corrupt)
    assert excinfo.value.code == "policy_read_failed"


# ---- decision behavior: preference, never requirement (§10/§12) ----------------


def test_prefer_variant_is_a_preference_not_a_requirement(tmp_path):
    store_path, _, _policy = make_active_policy(tmp_path)
    context = build_execution_context(store_path, scope_candidates=(SCOPE,))
    # the preferred variant IS among the candidates → selected with a
    # factual, policy-referencing explanation
    applied = apply_policy_preference(context,
                                      variants=["control", "treatment"])
    assert applied["policy_applied"] is True
    assert applied["selected"] == "treatment"
    assert applied["policy"]["policy_id"]
    for banned in CAUSAL_BANNED:
        assert banned not in applied["reason"].lower()
    # the preferred variant is NOT among the candidates → the decision is
    # returned UNCHANGED (a preference never becomes a requirement)
    not_applied = apply_policy_preference(context, variants=["control"])
    assert not_applied["policy_applied"] is False
    assert not_applied["selected"] is None
    for banned in CAUSAL_BANNED:
        assert banned not in not_applied["reason"].lower()
    # no active policy → decision unchanged, truthfully
    none_context = build_execution_context(tmp_path / "missing.sqlite3")
    unchanged = apply_policy_preference(none_context,
                                        variants=["control", "treatment"])
    assert unchanged["policy_applied"] is False
    assert unchanged["selected"] is None
    assert unchanged["policy"] is None


def test_preferred_variant_selection_rules(tmp_path):
    store_path, _, _policy = make_active_policy(tmp_path)
    context = build_execution_context(store_path, scope_candidates=(SCOPE,))
    assert preferred_variant(context) == "treatment"
    assert preferred_variant({"policy_status": "none"}) is None


def test_decision_input_must_carry_valid_context():
    with pytest.raises(PolicyError) as excinfo:
        apply_policy_preference({"policy_status": "bogus"}, variants=["a"])
    assert excinfo.value.code == "policy_decision_invalid"
    with pytest.raises(PolicyError):
        apply_policy_preference("not-a-context", variants=["a"])
    assert excinfo.value.code == "policy_decision_invalid"


# ---- consumption observability (§13/§15/§16) ------------------------------------


def test_consumption_event_created_and_idempotent(tmp_path):
    store_path, _, policy = make_active_policy(tmp_path)
    context = build_execution_context(store_path, scope_candidates=(SCOPE,))
    events = tmp_path / "consumption.jsonl"
    first = append_consumption(events, run_id="run-20260922T000000Z-abc123",
                               decision_id="req-test0001",
                               decision="trigger_golden_path",
                               context=context, now=NOW)
    assert first["created"] is True
    event = first["event"]
    assert event["consumption_id"].startswith("cons-")
    assert event["run_id"] == "run-20260922T000000Z-abc123"
    assert event["decision_id"] == "req-test0001"
    assert event["policy_id"] == policy["policy_id"]
    assert event["policy_version"] == 1
    assert event["scope"] == SCOPE
    assert event["policy_content_hash"] == policy["content_hash"]
    assert event["context_hash"] == context["context_hash"]
    assert event["created_at"] == NOW
    # deterministic identity: recomputing it by hand matches
    assert event["consumption_id"] == consumption_id(
        run_id=event["run_id"], decision_id=event["decision_id"],
        policy_id=event["policy_id"], policy_version=event["policy_version"],
        scope=event["scope"],
        policy_content_hash=event["policy_content_hash"],
        context_hash=event["context_hash"], decision=event["decision"])
    # the SAME decision replayed → idempotent, NO duplicate event
    retry = append_consumption(events, run_id=event["run_id"],
                               decision_id=event["decision_id"],
                               decision="trigger_golden_path",
                               context=context, now=NOW)
    assert retry["created"] is False
    assert retry["consumption_id"] == first["consumption_id"]
    assert len(read_consumptions(events)) == 1


def test_different_policy_version_produces_distinct_event(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    store = policy_env(tmp_path)

    def promote_and_activate(knowledge):
        candidate = compile_candidate(store, learning, knowledge)["candidate"]
        approve_candidate(store, candidate_id=candidate["candidate_id"],
                          approved_by="operator-a", now=NOW)
        policy = promote_candidate(store, learning.path,
                                   candidate_id=candidate["candidate_id"],
                                   now=NOW)["policy"]
        activate_policy(store, policy_id=policy["policy_id"], now=NOW)
        return policy

    knowledge_1 = custom_pipeline(learning, analytics,
                                  hypothesis="first confirmation")
    promote_and_activate(knowledge_1)
    context_v1 = build_execution_context(store.path, scope_candidates=(SCOPE,))
    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    promote_and_activate(knowledge_2)
    context_v2 = build_execution_context(store.path, scope_candidates=(SCOPE,))
    assert context_v1["policy"]["policy_version"] == 1
    assert context_v2["policy"]["policy_version"] == 2
    assert context_v1["context_hash"] != context_v2["context_hash"]

    events = tmp_path / "consumption.jsonl"
    event_1 = append_consumption(events, run_id="run-a", decision_id="req-1",
                                 decision="trigger_golden_path",
                                 context=context_v1, now=NOW)
    event_2 = append_consumption(events, run_id="run-b", decision_id="req-2",
                                 decision="trigger_golden_path",
                                 context=context_v2, now=NOW)
    assert event_1["consumption_id"] != event_2["consumption_id"]
    log = read_consumptions(events)
    assert [e["policy_version"] for e in log] == [1, 2]
    assert len({e["consumption_id"] for e in log}) == 2


def test_no_policy_consumption_event_is_truthful(tmp_path):
    context = build_execution_context(tmp_path / "missing.sqlite3")
    events = tmp_path / "consumption.jsonl"
    report = append_consumption(events, run_id="run-x",
                                decision_id="req-none",
                                decision="trigger_golden_path",
                                context=context, now=NOW)
    event = report["event"]
    # truthful: the fact that NO policy existed is recorded; NO policy id
    # is invented
    assert event["policy_status"] == "none"
    assert event["policy_id"] is None
    assert event["policy_version"] is None
    assert event["scope"] is None
    assert event["policy_content_hash"] is None
    assert event["context_hash"] == context["context_hash"]
    assert len(read_consumptions(events)) == 1


def test_rollback_changes_future_consumption_to_restored_version(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    store = policy_env(tmp_path)

    def promote_and_activate(knowledge):
        candidate = compile_candidate(store, learning, knowledge)["candidate"]
        approve_candidate(store, candidate_id=candidate["candidate_id"],
                          approved_by="operator-a", now=NOW)
        policy = promote_candidate(store, learning.path,
                                   candidate_id=candidate["candidate_id"],
                                   now=NOW)["policy"]
        activate_policy(store, policy_id=policy["policy_id"], now=NOW)
        return policy

    knowledge_1 = custom_pipeline(learning, analytics,
                                  hypothesis="first confirmation")
    policy_1 = promote_and_activate(knowledge_1)
    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    promote_and_activate(knowledge_2)

    events = tmp_path / "consumption.jsonl"
    context_v2 = build_execution_context(store.path, scope_candidates=(SCOPE,))
    append_consumption(events, run_id="run-v2", decision_id="req-v2",
                       decision="trigger_golden_path", context=context_v2,
                       now=NOW)
    # EXPLICIT rollback to v1 → future consumption references the
    # restored active version
    from ayce.policy import rollback_policy
    rollback_policy(store, policy_id=policy_1["policy_id"], now=NOW)
    context_v1 = build_execution_context(store.path, scope_candidates=(SCOPE,))
    assert context_v1["policy"]["policy_version"] == 1
    event_restored = append_consumption(
        events, run_id="run-restored", decision_id="req-restored",
        decision="trigger_golden_path", context=context_v1, now=NOW)
    log = read_consumptions(events)
    assert [e["policy_version"] for e in log] == [2, 1]
    assert event_restored["consumption_id"] != log[0]["consumption_id"]


# ---- immutability + no policy bypass (§27) ----------------------------------------


def test_execution_context_consumption_does_not_mutate_any_source(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    knowledge = custom_pipeline(learning, analytics)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    activate_policy(store, policy_id=store.list_policies()[0]["policy_id"],
                    now=NOW)
    store.close()
    learning.close()
    analytics.close()

    opened = PolicyStore(store.path)
    policy_rows_before = opened.list_policies()
    opened.close()
    watched = {
        "policy_db": digest(store.path),
        "learning_db": digest(learning.path),
        "analytics_db": digest(analytics.path),
    }

    # the full Stage 9 consumption surface
    context = build_execution_context(store.path, scope_candidates=(SCOPE,))
    append_consumption(tmp_path / "consumption.jsonl", run_id="run-i",
                       decision_id="req-i", decision="trigger_golden_path",
                       context=context, now=NOW)
    apply_policy_preference(context, variants=["control", "treatment"])

    # policy/learning/analytics state: byte-identical; policy rows unchanged
    assert digest(store.path) == watched["policy_db"]
    assert digest(learning.path) == watched["learning_db"]
    assert digest(analytics.path) == watched["analytics_db"]
    reopened = PolicyStore(store.path)
    assert reopened.list_policies() == policy_rows_before
    reopened.close()


def test_director_source_has_no_policy_write_capability():
    """§27: the director may ONLY consume canonical policy state. Its
    source must not open policy SQLite writable, issue policy
    INSERT/UPDATE/DELETE, invoke any Stage 8 lifecycle mutation, or
    construct policy rows itself."""
    here = Path(__file__).resolve().parent.parent.parent
    source = (here / "mcp-ayce-director" / "server.py").read_text(
        encoding="utf-8")
    for forbidden in ("INSERT INTO policies", "UPDATE policies",
                      "DELETE FROM policies", "PolicyStore(",
                      "approve_candidate", "promote_candidate",
                      "activate_policy", "rollback_policy",
                      "reject_candidate", "retire_policy",
                      "journal_mode", "mode=rw", "create_candidate"):
        assert forbidden not in source, forbidden
    # the ONLY policy capability is the canonical read-only consumption
    for required in ("build_execution_context", "append_consumption",
                     "AYCE_POLICY_DB"):
        assert required in source, required






