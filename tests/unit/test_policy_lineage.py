"""Stage 11 — Cross-run policy lineage index tests (deterministic
fixtures)."""

import hashlib
import json
from pathlib import Path

import pytest

from ayce.artifacts import ArtifactRegistry
from ayce.policy import (
    PolicyStore,
    PolicyError,
    activate_policy,
    append_consumption,
    approve_candidate,
    build_execution_context,
    lineage_for_policy,
    lineage_for_run,
    promote_candidate,
    rebuild_lineage_index,
    record_consumption_artifact,
    retire_policy,
    rollback_policy,
    verify_lineage_index,
)
from ayce.policy.lineage import _scan_runs
from ayce.policy.observability import CONSUMPTION_STAGE

from test_policy import NOW, SCOPE, compile_candidate, custom_pipeline, \
    make_env, policy_env


def make_run_dir(tmp_path: Path, run_id: str) -> Path:
    run_dir = tmp_path / "data" / "runs" / run_id
    run_dir.mkdir(parents=True)
    ArtifactRegistry(run_dir, run_id).save()
    return run_dir


def make_active_policy(tmp_path: Path, *, scope: str = SCOPE):
    _, analytics, learning = make_env(tmp_path / "evidence")
    knowledge = custom_pipeline(learning, analytics, scope=scope)
    store = policy_env(tmp_path / "store")
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    policy = promote_candidate(store, learning.path,
                               candidate_id=candidate["candidate_id"],
                               now=NOW)["policy"]
    activate_policy(store, policy_id=policy["policy_id"], now=NOW)
    store.close()
    learning.close()
    analytics.close()
    return store.path, policy


def make_context(store_path, *, scope=SCOPE):
    return build_execution_context(store_path, scope_candidates=(scope,))


def consume(tmp_path: Path, run_id: str, context, *, decision_id: str,
            now: str = NOW) -> dict:
    return append_consumption(tmp_path / "events.jsonl", run_id=run_id,
                              decision_id=decision_id,
                              decision="trigger_golden_path",
                              context=context, now=now)["event"]


def register(tmp_path: Path, run_id: str, event: dict) -> dict:
    return record_consumption_artifact(
        tmp_path / "data" / "runs" / run_id, run_id, event)


def digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def runs_root(tmp_path: Path) -> Path:
    return tmp_path / "data" / "runs"


# ---- index construction (§27) --------------------------------------------------


def test_index_on_empty_repository(tmp_path):
    scan = _scan_runs(runs_root(tmp_path))
    assert scan["ok"] is True and scan["runs_scanned"] == 0
    assert scan["run_index"] == {} and scan["policy_index"] == {}
    report = verify_lineage_index(runs_root(tmp_path))
    assert report["consistent"] is True and report["runs_verified"] == 0


def test_index_on_missing_runs_root(tmp_path):
    report = verify_lineage_index(tmp_path / "no" / "runs")
    assert report["ok"] is True and report["runs_scanned"] == 0
    result = lineage_for_run(tmp_path / "no" / "runs", "run-x")
    assert result["ok"] is False
    assert result["error"]["code"] == "runs_root_missing"


def test_index_one_run_multiple_and_no_policy(tmp_path):
    store_path, policy = make_active_policy(tmp_path / "fixture")
    run_a = "run-20260922T110000Z-lin0000000001"
    run_b = "run-20260922T110000Z-lin0000000002"
    run_c = "run-20260922T110000Z-lin0000000003"
    make_run_dir(tmp_path, run_a)
    make_run_dir(tmp_path, run_b)
    event_a = consume(tmp_path, run_a, make_context(store_path),
                      decision_id="req-lin-aaaaaaaa0000000001")
    register(tmp_path, run_a, event_a)
    event_b = consume(tmp_path, run_b,
                      build_execution_context(tmp_path / "missing.sqlite3"),
                      decision_id="req-lin-bbbbbbbb0000000001")
    register(tmp_path, run_b, event_b)
    make_run_dir(tmp_path, run_c)  # no consumption artifact

    scan = _scan_runs(runs_root(tmp_path), policy_db_path=store_path)
    assert scan["runs_scanned"] == 3
    statuses = {rid: rec["evidence_status"]
                for rid, rec in scan["run_index"].items()}
    assert statuses[run_a] == "verified"
    assert statuses[run_b] == "verified"
    assert statuses[run_c] == "missing"   # distinct from no-policy (§7)
    # policy→runs projection contains ONLY the real policy identity
    assert list(scan["policy_index"].keys()) == [policy["policy_id"]]
    for entries in scan["policy_index"][policy["policy_id"]].values():
        assert all(e["run_id"] != run_b for e in entries)  # §6
    assert scan["integrity"]["runs_corrupt"] == 0

    # run direction: each run resolves its own evidence truthfully
    result_a = lineage_for_run(runs_root(tmp_path), run_a,
                               policy_db_path=store_path)
    assert result_a["evidence_status"] == "verified"
    assert result_a["count"] == 1
    result_c = lineage_for_run(runs_root(tmp_path), run_c,
                               policy_db_path=store_path)
    assert result_c["evidence_status"] == "missing"
    assert result_c["count"] == 0


def test_index_excludes_corrupt_evidence_fail_closed(tmp_path):
    store_path, _policy = make_active_policy(tmp_path / "fixture")
    run_a = "run-20260922T110000Z-lin0000000004"
    run_dir = make_run_dir(tmp_path, run_a)
    event = consume(tmp_path, run_a, make_context(store_path),
                    decision_id="req-lin-aaaaaaaa0000000002")
    register(tmp_path, run_a, event)
    artifact = run_dir / "policy_consumption.json"
    mutated = json.loads(artifact.read_text(encoding="utf-8"))
    mutated["policy_version"] = 99
    artifact.write_text(json.dumps(mutated), encoding="utf-8")

    scan = _scan_runs(runs_root(tmp_path), policy_db_path=store_path)
    assert scan["run_index"][run_a]["evidence_status"] == "corrupt"
    assert scan["integrity"]["runs_corrupt"] == 1
    assert scan["policy_index"] == {}  # corrupt NEVER enters associations
    report = verify_lineage_index(runs_root(tmp_path),
                                  policy_db_path=store_path)
    assert report["consistent"] is False
    assert report["corrupt_runs"][0]["run_id"] == run_a


# ---- Run → Policy (§5) -----------------------------------------------------------


def test_lineage_for_run_single_and_multiple_events(tmp_path):
    store_path, policy = make_active_policy(tmp_path / "fixture")
    run_a = "run-20260922T110000Z-lin0000000005"
    make_run_dir(tmp_path, run_a)
    event_1 = consume(tmp_path, run_a, make_context(store_path),
                      decision_id="req-lin-cccccccc0000000001")
    register(tmp_path, run_a, event_1)
    event_2 = consume(tmp_path, run_a,
                      build_execution_context(tmp_path / "missing.sqlite3"),
                      decision_id="req-lin-nnnnnnnn0000000001")
    register(tmp_path, run_a, event_2)

    result = lineage_for_run(runs_root(tmp_path), run_a,
                             policy_db_path=store_path)
    assert result["ok"] is True and result["run_found"] is True
    assert result["evidence_status"] == "verified"
    assert result["count"] == 2
    ids = [p["consumption_id"] for p in result["policies"]]
    assert ids == sorted(ids)  # deterministic ordering (§18)
    policy_entry = next(p for p in result["policies"]
                        if p["policy_id"] == policy["policy_id"])
    assert policy_entry["policy_state"] == "active"
    assert policy_entry["content_match"] is True
    none_entry = next(p for p in result["policies"]
                      if p["policy_id"] is None)
    assert none_entry["policy_status"] == "none"


def test_lineage_for_run_unknown_run(tmp_path):
    store_path, _policy = make_active_policy(tmp_path / "fixture")
    make_run_dir(tmp_path, "run-20260922T110000Z-lin0000000006")
    result = lineage_for_run(runs_root(tmp_path),
                             "run-20260922T110000Z-nonexist0001",
                             policy_db_path=store_path)
    assert result["ok"] is False
    assert result["error"]["code"] == "run_not_found"


# ---- Policy → Runs + historical lifecycle (§5/§9/§25) -----------------------------


def test_lineage_for_policy_v1_v2_rollback_lifecycle(tmp_path):
    _, analytics, learning = make_env(tmp_path / "evidence")
    store = policy_env(tmp_path / "store")

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
    run_a = "run-20260922T110000Z-lin0000000007"
    make_run_dir(tmp_path, run_a)
    event_a = consume(tmp_path, run_a,
                      build_execution_context(store.path,
                                              scope_candidates=(SCOPE,)),
                      decision_id="req-lin-aaaaaaaa0000000003")
    register(tmp_path, run_a, event_a)

    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    policy_2 = promote_and_activate(knowledge_2)
    run_b = "run-20260922T110000Z-lin0000000008"
    make_run_dir(tmp_path, run_b)
    event_b = consume(tmp_path, run_b,
                      build_execution_context(store.path,
                                              scope_candidates=(SCOPE,)),
                      decision_id="req-lin-bbbbbbbb0000000003")
    register(tmp_path, run_b, event_b)

    # rollback → v1 active; run C consumes the restored v1
    rollback_policy(store, policy_id=policy_1["policy_id"], now=NOW)
    run_c = "run-20260922T110000Z-lin0000000009"
    make_run_dir(tmp_path, run_c)
    event_c = consume(tmp_path, run_c,
                      build_execution_context(store.path,
                                              scope_candidates=(SCOPE,)),
                      decision_id="req-lin-cccccccc0000000003")
    register(tmp_path, run_c, event_c)
    learning.close()
    analytics.close()

    # v1 → Runs A, C; v2 → Run B (§9/§25) — regardless of current state
    result_1 = lineage_for_policy(runs_root(tmp_path), policy_1["policy_id"],
                                  policy_db_path=store.path)
    assert result_1["run_count"] == 2
    assert {e["run_id"] for e in result_1["runs"]} == {run_a, run_c}
    assert all(e["policy_version"] == 1 for e in result_1["runs"])
    result_2 = lineage_for_policy(runs_root(tmp_path), policy_2["policy_id"],
                                  policy_db_path=store.path)
    assert result_2["run_count"] == 1
    assert result_2["runs"][0]["run_id"] == run_b
    assert result_2["runs"][0]["policy_version"] == 2
    # deterministic ordering: (created_at, run_id, consumption_id)
    assert [e["run_id"] for e in result_1["runs"]] == \
        sorted(e["run_id"] for e in result_1["runs"])
    # version filter
    result_1_v1 = lineage_for_policy(runs_root(tmp_path),
                                     policy_1["policy_id"], policy_version=1,
                                     policy_db_path=store.path)
    assert result_1_v1["run_count"] == 2
    result_1_v2 = lineage_for_policy(runs_root(tmp_path),
                                     policy_1["policy_id"], policy_version=2,
                                     policy_db_path=store.path)
    assert result_1_v2["run_count"] == 0
    # historical verification: policy_state reflects CURRENT state
    # (after v1→v2→rollback, v1 is active again and v2 is superseded —
    # but the HISTORICAL events remain verified either way, §16)
    states = {e["run_id"]: e["policy_state"] for e in result_1["runs"]}
    assert states[run_a] == "active"
    assert states[run_c] == "active"
    result_2_check = lineage_for_policy(runs_root(tmp_path),
                                        policy_2["policy_id"],
                                        policy_db_path=store.path)
    assert result_2_check["runs"][0]["policy_state"] == "superseded"
    # unknown policy → truthful empty (no fabricated association)
    result_none = lineage_for_policy(runs_root(tmp_path),
                                     "pol-doesnotexist0000000000000000",
                                     policy_db_path=store.path)
    assert result_none["ok"] is True and result_none["run_count"] == 0


# ---- rebuild determinism (§12) ------------------------------------------------------


def test_rebuild_is_deterministic_and_idempotent(tmp_path):
    store_path, policy = make_active_policy(tmp_path / "fixture")
    run_a = "run-20260922T110000Z-lin0000000010"
    make_run_dir(tmp_path, run_a)
    event = consume(tmp_path, run_a, make_context(store_path),
                    decision_id="req-lin-eeeeeeee0000000001")
    register(tmp_path, run_a, event)

    first = rebuild_lineage_index(runs_root(tmp_path),
                                  policy_db_path=store_path)
    second = rebuild_lineage_index(runs_root(tmp_path),
                                   policy_db_path=store_path)
    assert first == second  # delete + rebuild ⇒ identical projection
    assert first["derived"] is True and first["consistent"] is True
    # the derived projection is a pure function of the canonical artifacts
    scan_before = _scan_runs(runs_root(tmp_path))
    scan_after = _scan_runs(runs_root(tmp_path))
    assert scan_before["run_index"] == scan_after["run_index"]
    assert scan_before["policy_index"] == scan_after["policy_index"]


# ---- safety (§22) ----------------------------------------------------------------------


def test_lineage_never_mutates_any_canonical_store(tmp_path):
    _, analytics, learning = make_env(tmp_path / "evidence")
    store_path, _policy = make_active_policy(tmp_path / "fixture")
    run_a = "run-20260922T110000Z-lin0000000011"
    make_run_dir(tmp_path, run_a)
    event = consume(tmp_path, run_a, make_context(store_path),
                    decision_id="req-lin-ffffffff0000000001")
    register(tmp_path, run_a, event)
    learning.close()
    analytics.close()

    watched = {"policy": digest(store_path),
               "learning": digest(learning.path),
               "artifact": digest(runs_root(tmp_path) / run_a /
                                  "policy_consumption.json"),
               "manifest": digest(runs_root(tmp_path) / run_a /
                                  "artifacts.json")}

    lineage_for_run(runs_root(tmp_path), run_a, policy_db_path=store_path)
    lineage_for_policy(runs_root(tmp_path), event["policy_id"],
                       policy_db_path=store_path)
    verify_lineage_index(runs_root(tmp_path), policy_db_path=store_path)
    rebuild_lineage_index(runs_root(tmp_path), policy_db_path=store_path)

    assert digest(store_path) == watched["policy"]
    assert digest(learning.path) == watched["learning"]
    assert digest(runs_root(tmp_path) / run_a /
                  "policy_consumption.json") == watched["artifact"]
    assert digest(runs_root(tmp_path) / run_a /
                  "artifacts.json") == watched["manifest"]


def test_lineage_source_has_no_policy_mutation_capability():
    here = Path(__file__).resolve().parent
    source = (here.parent.parent / "src" / "ayce" / "policy" /
              "lineage.py").read_text(encoding="utf-8")
    for forbidden in ("approve_candidate", "promote_candidate",
                      "activate_policy", "rollback_policy",
                      "retire_policy", "reject_candidate",
                      "INSERT INTO policies", "UPDATE policies",
                      "DELETE FROM policies", "create_candidate",
                      "PolicyStore("):
        assert forbidden not in source, forbidden


# ---- CLI (§15) ------------------------------------------------------------------------


def test_cli_lineage_run_and_policy(tmp_path, monkeypatch, capsys):
    from ayce import cli
    store_path, policy = make_active_policy(tmp_path / "fixture")
    run_a = "run-20260922T110000Z-lin0000000012"
    make_run_dir(tmp_path, run_a)
    event = consume(tmp_path, run_a, make_context(store_path),
                    decision_id="req-lin-gggggggg0000000001")
    register(tmp_path, run_a, event)
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AYCE_POLICY_DB", str(store_path))

    rc = cli.main(["policy", "lineage", "run", "--json", "--run-id", run_a])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["run_found"] is True and report["count"] == 1
    assert report["policies"][0]["policy_id"] == policy["policy_id"]

    rc = cli.main(["policy", "lineage", "policy", "--json",
                   "--policy-id", policy["policy_id"]])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["run_count"] == 1
    assert report["runs"][0]["run_id"] == run_a

    rc = cli.main(["policy", "lineage", "policy", "--json",
                   "--policy-id", policy["policy_id"],
                   "--policy-version", "2"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["run_count"] == 0


def test_cli_lineage_verify_and_rebuild(tmp_path, monkeypatch, capsys):
    from ayce import cli
    store_path, _policy = make_active_policy(tmp_path / "fixture")
    run_a = "run-20260922T110000Z-lin0000000013"
    make_run_dir(tmp_path, run_a)
    event = consume(tmp_path, run_a, make_context(store_path),
                    decision_id="req-lin-hhhhhhhh0000000001")
    register(tmp_path, run_a, event)
    make_run_dir(tmp_path, "run-20260922T110000Z-lin0000000014")
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AYCE_POLICY_DB", str(store_path))

    rc = cli.main(["policy", "lineage", "verify", "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["consistent"] is True
    assert report["runs_verified"] == 1
    assert report["runs_missing_evidence"] == 1

    rc = cli.main(["policy", "lineage", "rebuild", "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["derived"] is True and report["consistent"] is True
    assert report["runs_scanned"] == 2


def test_cli_lineage_missing_run(tmp_path, monkeypatch, capsys):
    from ayce import cli
    store_path, _policy = make_active_policy(tmp_path / "fixture")
    make_run_dir(tmp_path, "run-20260922T110000Z-lin0000000014")
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AYCE_POLICY_DB", str(store_path))
    rc = cli.main(["policy", "lineage", "run", "--json", "--run-id",
                   "run-20260922T110000Z-nonexist0001"])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["code"] == "run_not_found"


def test_cli_lineage_rejects_bad_policy_id(tmp_path, monkeypatch, capsys):
    from ayce import cli
    store_path, _policy = make_active_policy(tmp_path / "fixture")
    make_run_dir(tmp_path, "run-20260922T110000Z-lin0000000015")
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AYCE_POLICY_DB", str(store_path))
    rc = cli.main(["policy", "lineage", "policy", "--json",
                   "--policy-id", "not-a-policy-id"])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["code"] == "policy_lineage_invalid"





