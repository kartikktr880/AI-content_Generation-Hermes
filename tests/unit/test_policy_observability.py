"""Stage 10 — Policy consumption observability & decision lineage tests.

Covers the §24 matrix over the REAL artifact registry / run-dir
conventions and the REAL Stage 8 policy store (deterministic fixtures —
never fabricated repository evidence):

artifact (deterministic canonical bytes + hash, registration, duplicate
idempotency, distinct-version events, missing run dir, mutation
detection, missing/malformed artifact, no-policy events truthful),
query (by run, empty result, deterministic created_at+consumption_id
ordering, dedupe), historical verification (verified identity after v2
activation / rollback / retirement / missing policy; corrupt identity
reported, never erased), concurrency (6 identical writers → 1 event/1
artifact), safety (policy/learning/analytics/RunState byte-identical),
no-mutation source scan, and the CLI observation surface.
"""

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.policy import (
    PolicyStore,
    PolicyError,
    activate_policy,
    append_consumption,
    approve_candidate,
    build_execution_context,
    promote_candidate,
    read_run_consumptions,
    record_consumption_artifact,
    rollback_policy,
    retire_policy,
)
from ayce.policy.observability import (
    CONSUMPTION_STAGE,
    consumption_artifact_bytes,
    verify_consumption_history,
)

from test_policy import (  # noqa: F401  (compile_candidate is a fixture helper)
    NOW, SCOPE, compile_candidate, custom_pipeline, make_env, policy_env,
)


def make_run_dir(tmp_path: Path, run_id: str) -> Path:
    """A REAL run directory with a REAL artifact registry (no RunState
    semantics touched). Lives under <tmp>/data/runs — the CLI's runs
    root convention."""
    run_dir = tmp_path / "data" / "runs" / run_id
    run_dir.mkdir(parents=True)
    registry = ArtifactRegistry(run_dir, run_id)
    registry.save()
    return run_dir


def make_event(tmp_path: Path, run_id: str, *, policy: bool = True,
               decision_id: str = "req-20260922T000000Z-obs00000001",
               now: str = NOW) -> dict:
    """A REAL Stage 9 consumption event (canonical reader output)."""
    if policy:
        store_path, _, policy_row = make_active_policy(tmp_path)
        context = build_execution_context(
            store_path, scope_candidates=(SCOPE,))
    else:
        context = build_execution_context(tmp_path / "missing.sqlite3")
    report = append_consumption(tmp_path / "consumption.jsonl",
                                run_id=run_id, decision_id=decision_id,
                                decision="trigger_golden_path",
                                context=context, now=now)
    return report["event"]


def make_active_policy(tmp_path: Path, *, scope: str = SCOPE):
    learning, _, knowledge = make_validated_knowledge(tmp_path, scope=scope)
    store = policy_env(tmp_path)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    (policy,) = [p for p in store.list_policies() if p["scope"] == scope]
    activate_policy(store, policy_id=policy["policy_id"], now=NOW)
    store.close()
    learning.close()
    return store.path, learning.path, policy


def make_validated_knowledge(tmp_path: Path, *, scope: str = SCOPE):
    _, analytics, learning = make_env(tmp_path)
    knowledge = custom_pipeline(learning, analytics, scope=scope)
    return learning, analytics, knowledge


def digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---- artifact: registration, determinism, idempotency (§5/§17/§18) ---------


def test_consumption_artifact_registration_is_deterministic(tmp_path):
    run_id = "run-20260922T000000Z-observab00001"
    run_dir = make_run_dir(tmp_path, run_id)
    event = make_event(tmp_path, run_id)
    # canonical bytes are deterministic
    assert consumption_artifact_bytes(event) == \
        consumption_artifact_bytes(dict(reversed(list(event.items()))))
    report = record_consumption_artifact(run_dir, run_id, event)
    assert report["registered"] is True and report["created"] is True
    assert report["consumption_id"] == event["consumption_id"]
    expected_sha = "sha256:" + hashlib.sha256(
        consumption_artifact_bytes(event)).hexdigest()
    assert report["sha256"] == expected_sha
    # the artifact is a REAL registered ArtifactRegistry ref
    registry = ArtifactRegistry.load(run_dir, run_id)
    (ref,) = registry.for_stage(CONSUMPTION_STAGE)
    assert ref.kind is ArtifactKind.POLICY_CONSUMPTION
    assert ref.metadata["sha256"] == expected_sha
    assert ref.metadata["size"] == len(consumption_artifact_bytes(event))
    assert ref.metadata["policy_id"] == event["policy_id"]
    # reading the run returns exactly this event
    read = read_run_consumptions(run_dir, run_id)
    assert read["ok"] is True
    assert [c["consumption_id"] for c in read["consumptions"]] == \
        [event["consumption_id"]]


def test_duplicate_registration_is_idempotent(tmp_path):
    run_id = "run-20260922T000000Z-observab00002"
    run_dir = make_run_dir(tmp_path, run_id)
    event = make_event(tmp_path, run_id)
    first = record_consumption_artifact(run_dir, run_id, event)
    second = record_consumption_artifact(run_dir, run_id, event)
    assert second["registered"] is False and second["created"] is False
    assert second["artifact_id"] == first["artifact_id"]
    registry = ArtifactRegistry.load(run_dir, run_id)
    assert len(registry.for_stage(CONSUMPTION_STAGE)) == 1
    assert len(read_run_consumptions(run_dir, run_id)["consumptions"]) == 1


def test_missing_run_directory_is_truthful(tmp_path):
    event = make_event(tmp_path, "run-20260922T000000Z-observab00004")
    report = record_consumption_artifact(
        tmp_path / "runs" / "run-20260922T000000Z-observab00004",
        "run-20260922T000000Z-observab00004", event)
    assert report["registered"] is False
    assert "run directory does not exist" in report["reason"]


# ---- query semantics (§12/§14) ------------------------------------------------


def test_query_with_no_consumption_evidence_is_truthful(tmp_path):
    run_dir = make_run_dir(tmp_path, "run-20260922T000000Z-observab00005")
    read = read_run_consumptions(run_dir,
                                 "run-20260922T000000Z-observab00005")
    assert read["ok"] is True
    assert read["consumptions"] == []
    assert "no policy consumption artifact" in read["note"]


def test_query_ordering_is_deterministic(tmp_path):
    run_id = "run-20260922T000000Z-observab00006"
    run_dir = make_run_dir(tmp_path, run_id)
    store_path, _, _policy = make_active_policy(tmp_path)
    # two events with IDENTICAL timestamps → consumption_id breaks the tie
    event_a = append_consumption(tmp_path / "c.jsonl", run_id=run_id,
                                 decision_id="req-obs-aaaaaaaa0000000001",
                                 decision="trigger_golden_path",
                                 context=build_execution_context(
                                     store_path, scope_candidates=(SCOPE,)),
                                 now=NOW)["event"]
    event_b = append_consumption(tmp_path / "c.jsonl", run_id=run_id,
                                 decision_id="req-obs-bbbbbbbb0000000001",
                                 decision="trigger_golden_path",
                                 context=build_execution_context(
                                     tmp_path / "missing.sqlite3"),
                                 now=NOW)["event"]
    record_consumption_artifact(run_dir, run_id, event_b)
    record_consumption_artifact(run_dir, run_id, event_a)
    read = read_run_consumptions(run_dir, run_id)
    ids = [c["consumption_id"] for c in read["consumptions"]]
    assert ids == sorted(ids)
    # repeated reads are stable
    assert read_run_consumptions(run_dir, run_id)["consumptions"] == \
        read["consumptions"]


# ---- integrity failures (§7/§12) -------------------------------------------------


def test_mutated_artifact_is_detected(tmp_path):
    run_id = "run-20260922T000000Z-observab00007"
    run_dir = make_run_dir(tmp_path, run_id)
    event = make_event(tmp_path, run_id)
    record_consumption_artifact(run_dir, run_id, event)
    # TAMPER with the registered artifact bytes
    artifact = run_dir / "policy_consumption.json"
    mutated = json.loads(artifact.read_text(encoding="utf-8"))
    mutated["policy_version"] = 99
    artifact.write_text(json.dumps(mutated), encoding="utf-8")
    with pytest.raises(PolicyError) as excinfo:
        read_run_consumptions(run_dir, run_id)
    assert excinfo.value.code == "policy_consumption_failed"
    assert "mutated after registration" in excinfo.value.message
    # it is NEVER silently regenerated (§7)
    assert json.loads(artifact.read_text(encoding="utf-8"))[
        "policy_version"] == 99


def test_malformed_artifact_is_detected(tmp_path):
    run_id = "run-20260922T000000Z-observab00008"
    run_dir = make_run_dir(tmp_path, run_id)
    event = make_event(tmp_path, run_id)
    record_consumption_artifact(run_dir, run_id, event)
    (run_dir / "policy_consumption.json").write_text("not json{",
                                                     encoding="utf-8")
    with pytest.raises(PolicyError) as excinfo:
        read_run_consumptions(run_dir, run_id)
    assert excinfo.value.code == "policy_consumption_failed"


def test_missing_artifact_file_is_detected(tmp_path):
    run_id = "run-20260922T000000Z-observab00009"
    run_dir = make_run_dir(tmp_path, run_id)
    event = make_event(tmp_path, run_id)
    record_consumption_artifact(run_dir, run_id, event)
    (run_dir / "policy_consumption.json").unlink()
    with pytest.raises(PolicyError) as excinfo:
        read_run_consumptions(run_dir, run_id)
    assert excinfo.value.code == "policy_consumption_failed"
    assert "missing on disk" in excinfo.value.message


def test_tampered_event_with_same_id_fails_closed(tmp_path):
    run_id = "run-20260922T000000Z-observab00010"
    run_dir = make_run_dir(tmp_path, run_id)
    event = make_event(tmp_path, run_id)
    first = record_consumption_artifact(run_dir, run_id, event)
    assert first["registered"] is True
    # the same consumption_id but DIFFERENT canonical bytes → the
    # re-record must refuse (content-addressed identity violated);
    # tamper a NON-identity display field so the id stays identical
    tampered = dict(event, created_at="1999-01-01T00:00:00.000Z")
    assert tampered["consumption_id"] == event["consumption_id"]
    with pytest.raises(PolicyError) as excinfo:
        record_consumption_artifact(run_dir, run_id, tampered)
    assert excinfo.value.code == "policy_consumption_failed"
    # the original evidence is untouched (schema_version added by the
    # canonical artifact envelope)
    stored = read_run_consumptions(run_dir, run_id)["consumptions"][0]
    assert stored["consumption_id"] == event["consumption_id"]
    assert stored["created_at"] == event["created_at"]
    assert stored["context_hash"] == event["context_hash"]


def test_event_run_id_must_match_target_run(tmp_path):
    event = make_event(tmp_path, "run-20260922T000000Z-observab00011")
    other = make_run_dir(tmp_path, "run-20260922T000000Z-observab00012")
    with pytest.raises(PolicyError) as excinfo:
        record_consumption_artifact(other,
                                    "run-20260922T000000Z-observab00012",
                                    event)
    assert excinfo.value.code == "policy_consumption_failed"


# ---- historical verification (§15/§16) ---------------------------------------------


def test_historical_consumption_survives_v2_rollback_and_retirement(tmp_path):
    _, analytics, learning = make_env(tmp_path / "fixture")
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
    context_v1 = build_execution_context(store.path,
                                         scope_candidates=(SCOPE,))
    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    promote_and_activate(knowledge_2)
    context_v2 = build_execution_context(store.path,
                                         scope_candidates=(SCOPE,))
    learning.close()
    analytics.close()

    # A → v1, B → v2 (§9) — both remain distinct forever
    record_a = verify_consumption_history(
        append_consumption(tmp_path / "c.jsonl", run_id="run-a",
                           decision_id="req-hist-aaaaaaaa00000001",
                           decision="trigger_golden_path",
                           context=context_v1, now=NOW)["event"],
        store.path)
    record_b = verify_consumption_history(
        append_consumption(tmp_path / "c.jsonl", run_id="run-b",
                           decision_id="req-hist-bbbbbbbb00000001",
                           decision="trigger_golden_path",
                           context=context_v2, now=NOW)["event"],
        store.path)
    assert record_a["identity"] == "verified"
    # policy_state reflects the CURRENT store state: v1 was already
    # superseded by v2 at verification time — the HISTORICAL event is
    # still verified (§16)
    assert record_a["policy_state"] == "superseded"
    assert record_a["content_match"] is True
    assert record_b["identity"] == "verified"
    assert record_b["policy_state"] == "active"
    assert record_b["content_match"] is True

    # rollback, then retire: the HISTORICAL v1 evidence must remain valid
    rollback_policy(store, policy_id=policy_1["policy_id"], now=NOW)
    verify_v1 = verify_consumption_history(
        append_consumption(tmp_path / "c.jsonl", run_id="run-c",
                           decision_id="req-hist-cccccccc00000001",
                           decision="trigger_golden_path",
                           context=build_execution_context(
                               store.path, scope_candidates=(SCOPE,)),
                           now=NOW)["event"],
        store.path)
    assert verify_v1["identity"] == "verified"
    assert verify_v1["policy_state"] == "active"
    retire_policy(store, policy_id=policy_1["policy_id"], now=NOW)
    after_retire = verify_consumption_history(
        append_consumption(tmp_path / "c.jsonl", run_id="run-a",
                           decision_id="req-hist-aaaaaaaa00000001",
                           decision="trigger_golden_path",
                           context=context_v1, now=NOW)["event"],
        store.path)
    assert after_retire["identity"] == "verified"
    assert after_retire["policy_state"] == "retired"
    assert after_retire["content_match"] is True
    assert after_retire["historical"] is True


def test_historical_verification_reports_missing_policy(tmp_path):
    event = make_event(tmp_path, "run-20260922T000000Z-observab00013")
    verification = verify_consumption_history(
        event, tmp_path / "missing" / "policy.sqlite3")
    assert verification["identity"] == "verified"
    assert verification["policy_state"] == "missing"
    assert verification["content_match"] is False
    assert verification["historical"] is True


def test_no_policy_event_verification_is_truthful(tmp_path):
    event = make_event(tmp_path, "run-20260922T000000Z-observab00014",
                       policy=False)
    verification = verify_consumption_history(event, tmp_path / "x.sqlite3")
    assert verification["identity"] == "verified"
    assert verification["policy_state"] is None
    assert verification["content_match"] is None


def test_corrupted_event_identity_is_reported_not_erased(tmp_path):
    event = make_event(tmp_path, "run-20260922T000000Z-observab00015")
    tampered = dict(event, policy_version=99)
    verification = verify_consumption_history(tampered, tmp_path / "x.sqlite3")
    assert verification["identity"] == "corrupt"
    assert verification["consumption_id"] == event["consumption_id"]


# ---- concurrency (§19) ----------------------------------------------------------


def test_concurrent_identical_registration_single_event(tmp_path):
    run_id = "run-20260922T000000Z-observab00016"
    run_dir = make_run_dir(tmp_path, run_id)
    event = make_event(tmp_path, run_id)
    errors: list[PolicyError] = []
    outcomes = []

    def worker():
        try:
            outcomes.append(record_consumption_artifact(run_dir, run_id,
                                                        event))
        except PolicyError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    assert len({o["artifact_id"] for o in outcomes}) == 1
    registry = ArtifactRegistry.load(run_dir, run_id)
    assert len(registry.for_stage(CONSUMPTION_STAGE)) == 1
    assert len(read_run_consumptions(run_dir, run_id)["consumptions"]) == 1


def test_concurrent_distinct_versions_produce_distinct_artifacts(tmp_path):
    run_id = "run-20260922T000000Z-observab00017"
    run_dir = make_run_dir(tmp_path, run_id)
    store_path, _, _policy = make_active_policy(tmp_path)
    event_1 = append_consumption(tmp_path / "c.jsonl", run_id=run_id,
                                 decision_id="req-obs-v100000000002",
                                 decision="trigger_golden_path",
                                 context=build_execution_context(
                                     store_path, scope_candidates=(SCOPE,)),
                                 now=NOW)["event"]
    event_2 = append_consumption(tmp_path / "c.jsonl", run_id=run_id,
                                 decision_id="req-obs-none000000000002",
                                 decision="trigger_golden_path",
                                 context=build_execution_context(
                                     tmp_path / "missing.sqlite3"),
                                 now=NOW)["event"]
    barrier = threading.Barrier(2)

    def worker(event):
        barrier.wait(30)
        record_consumption_artifact(run_dir, run_id, event)

    threads = [threading.Thread(target=worker, args=(e,))
               for e in (event_1, event_2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    read = read_run_consumptions(run_dir, run_id)
    statuses = sorted(str(c["policy_status"]) for c in read["consumptions"])
    assert statuses == ["active", "none"]
    assert len({c["consumption_id"] for c in read["consumptions"]}) == 2


# ---- safety invariants (§20/§25) ---------------------------------------------------


def test_observability_never_mutates_policy_learning_analytics(tmp_path):
    store_path, _, _policy = make_active_policy(tmp_path / "fixture")
    learning, _, knowledge = make_validated_knowledge(
        tmp_path / "evidence")
    analytics = None
    # the evidence event is built against the SAME policy store
    run_id = "run-20260922T000000Z-observab00018"
    run_dir = make_run_dir(tmp_path, run_id)
    context = build_execution_context(store_path, scope_candidates=(SCOPE,))
    event = append_consumption(tmp_path / "consumption.jsonl", run_id=run_id,
                               decision_id="req-20260922T000000Z-obs00000001",
                               decision="trigger_golden_path",
                               context=context, now=NOW)["event"]
    learning.close()
    if analytics is not None:
        analytics.close()
    # close fixture writers → WAL checkpointed; byte digests are complete
    watched = {"policy": digest(store_path),
               "learning": digest(learning.path)}

    opened = PolicyStore(store_path)
    rows_before = opened.list_policies()
    opened.close()

    record_consumption_artifact(run_dir, run_id, event)
    read_run_consumptions(run_dir, run_id)
    verification = verify_consumption_history(event, store_path)
    assert verification["identity"] == "verified"

    assert digest(store_path) == watched["policy"]
    assert digest(learning.path) == watched["learning"]
    reopened = PolicyStore(store_path)
    assert reopened.list_policies() == rows_before
    reopened.close()


def test_runstate_is_never_touched_by_observability(tmp_path):
    run_id = "run-20260922T000000Z-observab00019"
    run_dir = make_run_dir(tmp_path, run_id)
    state_path = run_dir / "state.json"
    state_path.write_text(json.dumps({"run_id": run_id,
                                      "job_id": "job-20260922T000000Z-obs0000"
                                      }), encoding="utf-8")
    before = digest(state_path)
    event = make_event(tmp_path, run_id)
    record_consumption_artifact(run_dir, run_id, event)
    read_run_consumptions(run_dir, run_id)
    # RunState semantics intact: same bytes, still loads
    assert digest(state_path) == before
    from ayce.state import RunState
    state = RunState.load(state_path)
    assert state.run_id == run_id
    assert state.lineage is None


def test_stage10_source_has_no_policy_mutation_capability():
    """§20: the Stage 10 implementation is read/record/inspect only —
    no lifecycle mutation call may appear in the observability module."""
    here = Path(__file__).resolve().parent
    source = (here.parent.parent / "src" / "ayce" / "policy" /
              "observability.py").read_text(encoding="utf-8")
    for forbidden in ("approve_candidate", "promote_candidate",
                      "activate_policy", "rollback_policy",
                      "retire_policy", "reject_candidate",
                      "INSERT INTO policies", "UPDATE policies",
                      "DELETE FROM policies", "create_candidate"):
        assert forbidden not in source, forbidden


# ---- CLI observation surface (§13) ----------------------------------------------


def test_cli_consumption_json_roundtrip(tmp_path, monkeypatch, capsys):
    from ayce import cli
    run_id = "run-20260922T000000Z-observab00020"
    run_dir = make_run_dir(tmp_path, run_id)
    event = make_event(tmp_path, run_id)
    record_consumption_artifact(run_dir, run_id, event)
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))

    rc = cli.main(["policy", "consumption", "--json", "--run-id", run_id])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["count"] == 1
    (record,) = report["consumptions"]
    assert record["consumption_id"] == event["consumption_id"]
    assert record["policy_id"] == event["policy_id"]
    assert record["policy_version"] == 1
    assert record["context_hash"] == event["context_hash"]


def test_cli_consumption_missing_run(tmp_path, monkeypatch, capsys):
    from ayce import cli
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))
    rc = cli.main(["policy", "consumption", "--json", "--run-id",
                   "run-20260922T000000Z-nonexist0001"])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["code"] == "run_not_found"


def test_cli_consumption_run_without_evidence(tmp_path, monkeypatch, capsys):
    from ayce import cli
    run_id = "run-20260922T000000Z-observab00021"
    make_run_dir(tmp_path, run_id)
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))
    rc = cli.main(["policy", "consumption", "--json", "--run-id", run_id])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["consumptions"] == []
    assert "no policy consumption artifact" in report["note"]


def test_cli_consumption_multiple_events_and_verify(tmp_path, monkeypatch,
                                                    capsys):
    from ayce import cli
    run_id = "run-20260922T000000Z-observab00022"
    run_dir = make_run_dir(tmp_path, run_id)
    store_path, _, _policy = make_active_policy(tmp_path)
    event_1 = append_consumption(tmp_path / "c.jsonl", run_id=run_id,
                                 decision_id="req-obs-cccccccc0000000001",
                                 decision="trigger_golden_path",
                                 context=build_execution_context(
                                     store_path, scope_candidates=(SCOPE,)),
                                 now=NOW)["event"]
    event_2 = append_consumption(tmp_path / "c.jsonl", run_id=run_id,
                                 decision_id="req-obs-nnnnnnnn0000000001",
                                 decision="trigger_golden_path",
                                 context=build_execution_context(
                                     tmp_path / "missing.sqlite3"),
                                 now=NOW)["event"]
    record_consumption_artifact(run_dir, run_id, event_1)
    record_consumption_artifact(run_dir, run_id, event_2)
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AYCE_POLICY_DB", str(store_path))

    rc = cli.main(["policy", "consumption", "--json", "--run-id", run_id,
                   "--verify"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["count"] == 2
    assert [v["identity"] for v in report["verification"]] == \
        ["verified", "verified"]
    assert report["verification"][0]["policy_state"] == "active"
    assert report["verification"][1]["policy_state"] is None
    # exact filters work
    rc = cli.main(["policy", "consumption", "--json", "--run-id", run_id,
                   "--policy-id", event_1["policy_id"]])
    assert rc == 0
    filtered = json.loads(capsys.readouterr().out)
    assert filtered["count"] == 1
    assert filtered["consumptions"][0]["policy_id"] == event_1["policy_id"]


def test_cli_consumption_rejects_path_traversal(tmp_path, monkeypatch,
                                                capsys):
    from ayce import cli
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))
    rc = cli.main(["policy", "consumption", "--json", "--run-id", "../escape"])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["code"] in ("invalid_run_id", "path_escape")






