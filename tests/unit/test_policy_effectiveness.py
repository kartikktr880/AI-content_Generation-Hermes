"""Stage 12 — Policy effectiveness feedback tests (deterministic fixtures).

The evidence graph is built through the REAL canonical owners — Stage
8/9/10 policy + consumption artifacts, a REAL Stage 5 PublishLedger and
a REAL Stage 6 AnalyticsStore — never fabricated at query time:

    Run A → Policy v1 → published Video A → analytics observations
    Run B → Policy v1 → published Video B → analytics observations
    Run C → Policy v2 → published Video C → analytics UNAVAILABLE
    Run D → Policy v2 → unpublished
    Run E → Policy v1 → published → analytics unavailable
    Run F → no policy → published → analytics available
    Run G → Policy v1 (after rollback) → published → analytics

Covers the §34 matrix: policy→run→publish, publish→video (authoritative,
ambiguous), video→analytics (available/unavailable/missing, windows,
sources), metric semantics (present/zero/missing/unavailable),
aggregation (counts, mean/median, explicit exclusions, deterministic
ordering), historical lifecycle (v1→v2→rollback→retirement), integrity
(lineage mismatch, corrupt artifacts/observations, unknown ids), CLI
(run/policy/metric/verify), determinism and byte-level read-only safety.
"""

import hashlib
import json
from pathlib import Path

import pytest

from ayce.analytics.store import AnalyticsStore
from ayce.policy import PolicyError, retire_policy, rollback_policy
from ayce.publishing.ledger import PublishLedger

from test_policy import NOW, compile_candidate, custom_pipeline, make_env
from test_policy_lineage import (
    consume,
    digest,
    make_active_policy,
    make_context,
    make_run_dir,
    register,
    runs_root,
)
from ayce.policy import (PolicyStore, PolicyError, activate_policy,
                         approve_candidate, build_execution_context,
                         promote_candidate, retire_policy, rollback_policy)

# ---- deterministic fixture builders (§28/§29) ---------------------------------

LEDGER_PATH_NAME = Path("data") / "publishing" / "ledger.sqlite3"
ANALYTICS_PATH_NAME = Path("data") / "analytics" / "analytics.sqlite3"


def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / LEDGER_PATH_NAME


def analytics_path(tmp_path: Path) -> Path:
    return tmp_path / ANALYTICS_PATH_NAME


def publish_run(tmp_path: Path, run_id: str, video_id: str, *,
                destination: str = "youtube",
                seal: str | None = None,
                package_id: str | None = None,
                final_status: str = "published") -> None:
    """A REAL Stage 5 ledger row for one run (created → final status)."""
    seal = seal or f"seal-{run_id[-12:]}"
    package_id = package_id or f"pkg-{run_id[-12:]}"
    ledger = PublishLedger(ledger_path(tmp_path))
    ledger.create(package_seal=seal, package_id=package_id, run_id=run_id,
                  destination=destination, privacy_status="private",
                  title="t", requested_metadata={}, now=NOW)
    if final_status != "created":
        ledger.update(seal, destination, now=NOW, status=final_status,
                      youtube_video_id=(video_id
                                        if final_status == "published"
                                        else None))
    ledger.close()


def add_observation(tmp_path: Path, video_id: str, run_id: str, obs_id: str,
                    measurements: list[dict], *, source: str = "DATA_API_V3",
                    destination: str = "youtube",
                    package_id: str = "pkg-x", package_seal: str = "seal-x",
                    window: tuple[str, str] | None = None,
                    lineage: dict | None = None,
                    raw_lineage: str | None = None) -> None:
    """A REAL Stage 6 observation + normalized measurements (deterministic
    values; mirrors what the Stage 6 collector persists)."""
    store = AnalyticsStore(analytics_path(tmp_path))
    lin = lineage or {"youtube_video_id": video_id,
                      "package_id": package_id,
                      "package_seal": package_seal,
                      "run_id": run_id,
                      "destination": destination}
    store.insert_observation(
        observation_id=obs_id, source=source, youtube_video_id=video_id,
        scope="video", observed_at=NOW,
        metrics_requested=[m["metric_name"] for m in measurements],
        source_request={}, raw_response="{}",
        lineage=lin if raw_lineage is None else raw_lineage,
        collected_at=NOW,
        window_start=window[0] if window else None,
        window_end=window[1] if window else None,
        window_timezone="UTC" if window else None)
    store.insert_measurements(
        observation_id=obs_id, source=source, youtube_video_id=video_id,
        scope="video", observed_at=NOW, lineage=lin,
        measurements=measurements, normalized_at=NOW,
        window_start=window[0] if window else None,
        window_end=window[1] if window else None)
    store.close()


def stat(metric: str, value, availability: str = "present") -> dict:
    return {"metric_name": metric, "raw_value": (str(value)
                                                 if value is not None else None),
            "value": value, "value_type": "integer", "unit": "count",
            "availability": availability}


PATHS = ("policy_db_path", "ledger_path", "analytics_db_path")


def paths(tmp_path: Path, store_path) -> dict:
    return {"policy_db_path": store_path,
            "ledger_path": ledger_path(tmp_path),
            "analytics_db_path": analytics_path(tmp_path)}


@pytest.fixture()
def repo(tmp_path):
    """The full §28/§30 evidence graph: A,B,G / C,D on v2 / E / F, with
    the v1→v2→rollback→retire lifecycle applied."""
    store_path, policy_1 = make_active_policy(tmp_path / "fixture")
    store = PolicyStore(store_path)

    def promote(knowledge):
        candidate = compile_candidate(store, learning, knowledge)["candidate"]
        approve_candidate(store, candidate_id=candidate["candidate_id"],
                          approved_by="operator-a", now=NOW)
        policy = promote_candidate(store, learning.path,
                                   candidate_id=candidate["candidate_id"],
                                   now=NOW)["policy"]
        activate_policy(store, policy_id=policy["policy_id"], now=NOW)
        return policy

    runs = {}

    def make_run(run_id, context, decision_suffix, *, video_id=None,
                 final_status="published", destination="youtube"):
        make_run_dir(tmp_path, run_id)
        event = consume(tmp_path, run_id, context,
                        decision_id=f"req-eff-{decision_suffix}")
        register(tmp_path, run_id, event)
        if video_id is not None:
            publish_run(tmp_path, run_id, video_id,
                        final_status=final_status,
                        destination=destination)
        runs[run_id] = event
        return event

    # learning/analytics evidence stores for the SECOND promotion
    _, analytics, learning = make_env(tmp_path / "fixture" / "evidence2")

    # v1: runs A, B, E
    ctx_v1 = build_execution_context(store_path, scope_candidates=("format:short_explainer",))
    run_a = "run-20260922T110000Z-efa000000001"
    run_b = "run-20260922T110000Z-efb000000001"
    run_e = "run-20260922T110000Z-efe000000001"
    make_run(run_a, ctx_v1, "a0000000001", video_id="vidEffA00001")
    make_run(run_b, ctx_v1, "b0000000001", video_id="vidEffB00001")
    make_run(run_e, ctx_v1, "e0000000001", video_id="vidEffE00001")

    # v2: runs C (published), D (unpublished)
    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    policy_2 = promote(knowledge_2)
    ctx_v2 = build_execution_context(store_path, scope_candidates=("format:short_explainer",))
    run_c = "run-20260922T110000Z-efc000000001"
    run_d = "run-20260922T110000Z-efd000000001"
    make_run(run_c, ctx_v2, "c0000000001", video_id="vidEffC00001")
    make_run(run_d, ctx_v2, "d0000000001", video_id="vidEffD00001",
             final_status="upload_failed")

    # rollback → v1 active again; run G consumes v1
    rollback_policy(store, policy_id=policy_1["policy_id"], now=NOW)
    ctx_v1_again = build_execution_context(store_path, scope_candidates=("format:short_explainer",))
    run_g = "run-20260922T110000Z-efg000000001"
    make_run(run_g, ctx_v1_again, "g0000000001", video_id="vidEffG00001")

    # retire v1 — historical evidence must survive (§16/§30)
    retire_policy(store, policy_id=policy_1["policy_id"], now=NOW)

    # F: a run with NO policy at all, published, analytics available
    run_f = "run-20260922T110000Z-eff000000001"
    from ayce.policy import append_consumption
    no_ctx = build_execution_context(tmp_path / "missing.sqlite3",
                                     scope_candidates=("format:short_explainer",))
    make_run_dir(tmp_path, run_f)
    event_f = append_consumption(tmp_path / "events.jsonl", run_id=run_f,
                                 decision_id="req-eff-f0000000001",
                                 decision="trigger_golden_path",
                                 context=no_ctx, now=NOW)["event"]
    register(tmp_path, run_f, event_f)
    publish_run(tmp_path, run_f, "vidEffF00001")
    runs[run_f] = event_f

    # ---- analytics observations (§29 metric coverage) ----------------
    # A: lifetime Data-API views/likes + a MISSING metric + a windowed
    #    Analytics-API view of the SAME metric name (must NOT merge, §12)
    add_observation(tmp_path, "vidEffA00001", run_a, "obs-effa000000000001",
                    [stat("views", 1000), stat("likes", 0, availability="zero"),
                     stat("comments", None, availability="missing")])
    add_observation(tmp_path, "vidEffA00001", run_a, "obs-effa000000000002",
                    [stat("views", 50)],
                    source="ANALYTICS_API_V2", window=("2026-09-13",
                                                       "2026-09-19"),
                    package_id="pkg-a", package_seal="seal-a")
    # B: windowed Analytics-API views + an UNAVAILABLE metric
    add_observation(tmp_path, "vidEffB00001", run_b, "obs-effb000000000001",
                    [stat("views", 200)],
                    source="ANALYTICS_API_V2", window=("2026-09-13",
                                                       "2026-09-19"),
                    package_id="pkg-b", package_seal="seal-b")
    add_observation(tmp_path, "vidEffB00001", run_b, "obs-effb000000000002",
                    [stat("subscribersGained", None,
                          availability="unavailable")],
                    package_id="pkg-b", package_seal="seal-b")
    # C: published but NO observations for its video (analytics unavailable)
    # E: same (store exists, video never observed)
    # F: no policy but real analytics
    add_observation(tmp_path, "vidEffF00001", run_f, "obs-efff000000000001",
                    [stat("views", 7)])
    # G: three lifetime observations of views → median/mean are meaningful
    add_observation(tmp_path, "vidEffG00001", run_g, "obs-effg000000000001",
                    [stat("views", 10)])
    add_observation(tmp_path, "vidEffG00001", run_g, "obs-effg000000000002",
                    [stat("views", 30)])
    add_observation(tmp_path, "vidEffG00001", run_g, "obs-effg000000000003",
                    [stat("views", 20)])

    learning.close()
    analytics.close()
    store.close()
    return {"tmp_path": tmp_path, "store_path": store_path,
            "policy_1": policy_1, "policy_2": policy_2, "runs": runs,
            "run_a": run_a, "run_b": run_b, "run_c": run_c, "run_d": run_d,
            "run_e": run_e, "run_f": run_f, "run_g": run_g}


from ayce.policy.effectiveness import (  # noqa: E402
    effectiveness_for_policy,
    effectiveness_for_run,
    metric_effectiveness_summary,
    verify_effectiveness,
)


def eff(tmp_path, store_path):
    return paths(tmp_path, store_path)


def by_metric(record):
    return {m["metric"]: m for m in record["measurements"]}


# ---- Policy → Run → Publish (§5/§17) --------------------------------------------


def test_run_direction_published_run_full_chain(repo):
    tmp, run_a = repo["tmp_path"], repo["run_a"]
    report = effectiveness_for_run(tmp / "data" / "runs", run_a, **eff(tmp, repo["store_path"]))
    assert report["ok"] is True and report["evidence_status"] == "verified"
    assert report["record_count"] == 1
    record = report["records"][0]
    assert record["policy_id"] == repo["policy_1"]["policy_id"]
    assert record["policy_version"] == 1
    assert record["consumption_id"] == repo["runs"][run_a]["consumption_id"]
    assert record["publish_status"] == "published"
    # the video id comes ONLY from the ledger (§4/§5)
    assert record["publish"]["youtube_video_id"] == "vidEffA00001"
    assert record["publish"]["destination"] == "youtube"
    assert record["analytics_status"] == "available"
    assert record["missing_data_status"] == "metric_present"
    assert record["evidence_status"] == "valid"
    assert record["effectiveness_id"].startswith("pef-")
    # current state is metadata ONLY; the historical relation is intact
    assert record["policy_state"] == "retired"  # v1 was retired (§30)
    metrics = by_metric(record)
    assert metrics["views"]["value"] == 1000.0
    assert metrics["views"]["source"] == "DATA_API_V3"
    assert metrics["views"]["window_start"] is None
    # zero is REAL data — never missing, never dropped (§13/§29)
    assert metrics["likes"]["value"] == 0.0
    assert metrics["likes"]["availability"] == "zero"
    assert metrics["comments"]["availability"] == "missing"
    assert metrics["comments"]["value"] is None


def test_unpublished_run_is_not_an_analytics_failure(repo):
    tmp, run_d = repo["tmp_path"], repo["run_d"]
    report = effectiveness_for_run(tmp / "data" / "runs", run_d, **eff(tmp, repo["store_path"]))
    record = report["records"][0]
    assert record["publish_status"] == "unpublished"
    assert record["publish"] is None
    assert record["analytics_status"] == "not_applicable"
    assert record["missing_data_status"] == "not_published"
    assert record["measurements"] == []


def test_no_ledger_store_means_unpublished(repo):
    tmp, run_e = repo["tmp_path"], repo["run_e"]
    report = effectiveness_for_run(
        tmp / "data" / "runs", run_e, policy_db_path=repo["store_path"],
        ledger_path=tmp / "data" / "publishing" / "absent.sqlite3",
        analytics_db_path=analytics_path(tmp))
    record = report["records"][0]
    assert record["publish_status"] == "unpublished"
    assert record["ledger_missing"] is True
    assert record["missing_data_status"] == "not_published"


def test_no_policy_run_never_enters_policy_aggregation(repo):
    tmp, run_f = repo["tmp_path"], repo["run_f"]
    report = effectiveness_for_run(tmp / "data" / "runs", run_f, **eff(tmp, repo["store_path"]))
    record = report["records"][0]
    assert record["policy_id"] is None
    assert record["missing_data_status"] == "not_consumed"
    assert record["publish_status"] == "published"
    assert record["analytics_status"] == "available"
    # …and the policy-level aggregation for v1 excludes it (§28)
    agg = effectiveness_for_policy(
        tmp / "data" / "runs", repo["policy_1"]["policy_id"],
        **eff(tmp, repo["store_path"]))["aggregation"]
    assert run_f not in agg["run_ids"]


# ---- Policy direction aggregation (§10) ------------------------------------------


def test_policy_direction_counts_match_fixture(repo):
    tmp = repo["tmp_path"]
    report = effectiveness_for_policy(
        tmp / "data" / "runs", repo["policy_1"]["policy_id"],
        **eff(tmp, repo["store_path"]))
    assert report["ok"] is True
    agg = report["aggregation"]
    # v1 consumed A, B, E, G (F has no policy)
    assert agg["consumed_runs"] == 4
    assert agg["run_ids"] == sorted([repo["run_a"], repo["run_b"],
                                     repo["run_e"], repo["run_g"]])
    assert agg["published_runs"] == 4
    assert agg["unpublished_runs"] == 0
    # analytics available: A, B, G — E was never observed (§28)
    assert agg["analytics_available_run_ids"] == sorted(
        [repo["run_a"], repo["run_b"], repo["run_g"]])
    assert agg["analytics_unavailable_runs"] == 1
    assert agg["invalid_records"] == 0
    assert agg["note"] and "no ranking" in agg["note"]

    report_v2 = effectiveness_for_policy(
        tmp / "data" / "runs", repo["policy_2"]["policy_id"],
        **eff(tmp, repo["store_path"]))
    agg_v2 = report_v2["aggregation"]
    assert agg_v2["consumed_runs"] == 2  # C, D
    assert agg_v2["published_runs"] == 1  # C
    assert agg_v2["unpublished_run_ids"] == [repo["run_d"]]


def test_policy_direction_metric_and_video_filters(repo):
    tmp = repo["tmp_path"]
    report = effectiveness_for_policy(
        tmp / "data" / "runs", repo["policy_1"]["policy_id"],
        metric="views", **eff(tmp, repo["store_path"]))
    for record in report["records"]:
        assert {m["metric"] for m in record["measurements"]} <= {"views"}
    by_video = effectiveness_for_policy(
        tmp / "data" / "runs", repo["policy_1"]["policy_id"],
        video_id="vidEffA00001", **eff(tmp, repo["store_path"]))
    assert by_video["record_count"] == 1
    assert by_video["records"][0]["run_id"] == repo["run_a"]


# ---- Video → Analytics semantics (§6/§7/§12) --------------------------------------


def test_analytics_unavailable_is_distinct_from_zero(repo):
    tmp, run_e = repo["tmp_path"], repo["run_e"]
    report = effectiveness_for_run(tmp / "data" / "runs", run_e, **eff(tmp, repo["store_path"]))
    record = report["records"][0]
    assert record["analytics_status"] == "unavailable"
    assert record["missing_data_status"] == "analytics_not_collected"
    assert record["measurements"] == []  # never zero (§13)


def test_incompatible_windows_and_sources_never_merge(repo):
    tmp, run_a = repo["tmp_path"], repo["run_a"]
    report = effectiveness_for_run(tmp / "data" / "runs", run_a, **eff(tmp, repo["store_path"]))
    groups = report["aggregation"]["outcome_groups"]
    views_groups = [g for g in groups if g["metric"] == "views"]
    # lifetime Data-API views and windowed Analytics-API views are
    # SEPARATE populations (§12)
    semantics = sorted(g["window_semantics"] for g in views_groups)
    assert semantics == ["lifetime_cumulative", "windowed"]
    lifetime = next(g for g in views_groups
                    if g["window_semantics"] == "lifetime_cumulative")
    windowed = next(g for g in views_groups
                    if g["window_semantics"] == "windowed")
    assert lifetime["source"] == "DATA_API_V3"
    assert lifetime["n"] == 1 and lifetime["mean"] == 1000.0
    assert windowed["source"] == "ANALYTICS_API_V2"
    assert windowed["window_start"] == "2026-09-13"
    assert windowed["window_end"] == "2026-09-19"
    assert windowed["window_timezone"] == "UTC"


def test_unavailable_metric_preserved_and_excluded_explicitly(repo):
    tmp, run_b = repo["tmp_path"], repo["run_b"]
    report = effectiveness_for_run(tmp / "data" / "runs", run_b, **eff(tmp, repo["store_path"]))
    record = report["records"][0]
    metrics = by_metric(record)
    unavailable = metrics["subscribersGained"]
    assert unavailable["availability"] == "unavailable"
    assert unavailable["value"] is None
    groups = report["aggregation"]["outcome_groups"]
    subs = next(g for g in groups if g["metric"] == "subscribersGained")
    assert subs["n"] == 0
    assert subs["exclusion_counts"] == {"unavailable": 1}
    assert subs["excluded_measurements"][0]["availability"] == "unavailable"


def test_aggregation_descriptive_statistics(repo):
    tmp, run_g = repo["tmp_path"], repo["run_g"]
    report = effectiveness_for_run(tmp / "data" / "runs", run_g, **eff(tmp, repo["store_path"]))
    groups = report["aggregation"]["outcome_groups"]
    views = next(g for g in groups if g["metric"] == "views")
    assert views["n"] == 3
    assert views["mean"] == 20.0
    assert views["median"] == 20.0
    assert views["min"] == 10.0 and views["max"] == 30.0
    assert views["exclusion_counts"] == {}
    # deterministic group ordering
    keys = [(g["metric"], g["source"]) for g in groups]
    assert keys == sorted(keys)


# ---- Metric direction (§11/§14) ------------------------------------------------------


def test_metric_summary_is_descriptive_comparison(repo):
    tmp = repo["tmp_path"]
    report = metric_effectiveness_summary(
        tmp / "data" / "runs", "views", **eff(tmp, repo["store_path"]))
    assert report["ok"] is True
    assert report["comparison"] == "descriptive_comparison"
    versions = {(p["policy_id"], p["policy_version"]): p
                for p in report["policies"]}
    v1 = versions[(repo["policy_1"]["policy_id"], 1)]
    # lifetime views observations: A=1000, G=10/30/20 → mean 265
    lifetime = next(g for g in v1["outcome_groups"]
                    if g["window_semantics"] == "lifetime_cumulative")
    assert lifetime["n"] == 4
    assert lifetime["mean"] == (1000 + 10 + 30 + 20) / 4
    assert v1["evidence_status"] == "descriptive_evidence"
    v2 = versions[(repo["policy_2"]["policy_id"], 2)]
    assert v2["consumed_runs"] == 2 and v2["published_runs"] == 1
    assert v2["outcome_groups"] == []  # C was never observed
    assert v2["evidence_status"] == "insufficient_evidence"
    # filters narrow truthfully
    scoped = metric_effectiveness_summary(
        tmp / "data" / "runs", "views", policy_id=repo["policy_1"]["policy_id"],
        policy_version=1, source="ANALYTICS_API_V2",
        **eff(tmp, repo["store_path"]))
    groups = scoped["policies"][0]["outcome_groups"]
    assert all(g["source"] == "ANALYTICS_API_V2" for g in groups)
    # A's and B's windowed views share source+window → ONE population (§12)
    assert {g["n"] for g in groups} == {2}


# ---- Historical lifecycle (§16/§30) ---------------------------------------------------


def test_historical_versions_remain_queryable_after_retirement(repo):
    tmp = repo["tmp_path"]
    v1 = effectiveness_for_policy(
        tmp / "data" / "runs", repo["policy_1"]["policy_id"],
        **eff(tmp, repo["store_path"]))
        # v1 → A, B, E, G — even though v1 is RETIRED today (§30)
    assert v1["aggregation"]["run_ids"] == sorted(
        [repo["run_a"], repo["run_b"], repo["run_e"], repo["run_g"]])
    states = {r["run_id"]: r["policy_state"] for r in v1["records"]}
    assert set(states.values()) == {"retired"}
    assert all(r["evidence_status"] == "valid" for r in v1["records"])
    v2 = effectiveness_for_policy(
        tmp / "data" / "runs", repo["policy_2"]["policy_id"],
        **eff(tmp, repo["store_path"]))
    assert v2["aggregation"]["run_ids"] == sorted([repo["run_c"],
                                                   repo["run_d"]])
    assert {r["policy_state"] for r in v2["records"]} == {"superseded"}


def test_unknown_policy_is_truthfully_empty(repo):
    tmp = repo["tmp_path"]
    report = effectiveness_for_policy(
        tmp / "data" / "runs", "pol-doesnotexist000000000000000",
        **eff(tmp, repo["store_path"]))
    assert report["ok"] is True and report["record_count"] == 0
    assert report["aggregation"]["evidence_status"] == "insufficient_evidence"


def test_invalid_identities_are_structured_errors(repo):
    tmp, runs = repo["tmp_path"], tmp_runs(repo)
    for kwargs in ({"run_id": "../escape"},
                   {"run_id": "run-20260922T110000Z-nonexist0001"}):
        with pytest.raises(PolicyError) as excinfo:
            effectiveness_for_run(runs, kwargs["run_id"],
                                  **eff(tmp, repo["store_path"]))
        assert excinfo.value.code == "policy_effectiveness_invalid"
    with pytest.raises(PolicyError):
        effectiveness_for_policy(runs, "not-a-policy-id",
                                 **eff(tmp, repo["store_path"]))
    with pytest.raises(PolicyError):
        metric_effectiveness_summary(runs, "   ", **eff(tmp, repo["store_path"]))


def tmp_runs(repo):
    return repo["tmp_path"] / "data" / "runs"


# ---- Determinism (§9/§32) --------------------------------------------------------------


def test_same_evidence_yields_same_identity(repo):
    tmp, run_a = repo["tmp_path"], repo["run_a"]
    args = eff(tmp, repo["store_path"])
    first = effectiveness_for_run(tmp_runs(repo), run_a, **args)
    second = effectiveness_for_run(tmp_runs(repo), run_a, **args)
    assert first == second  # whole projection identical, ids included
    assert first["records"][0]["effectiveness_id"] == \
        second["records"][0]["effectiveness_id"]


# ---- Integrity / failure injection (§19/§31) --------------------------------------------


@pytest.fixture()
def mismatch_repo(tmp_path):
    """v1 run published as vidX, but the ingested observation claims a
    DIFFERENT run — the cross-check must fail closed (§19)."""
    store_path, policy = make_active_policy(tmp_path / "fixture")
    run_id = "run-20260922T110000Z-efx000000001"
    make_run_dir(tmp_path, run_id)
    event = consume(tmp_path, run_id, make_context(store_path),
                    decision_id="req-eff-x0000000001")
    register(tmp_path, run_id, event)
    publish_run(tmp_path, run_id, "vidEffX00001")
    add_observation(tmp_path, "vidEffX00001", "run-20260922T110000Z-other001",
                    "obs-effx000000000001", [stat("views", 5)],
                    lineage={"youtube_video_id": "vidEffX00001",
                             "package_id": "pkg-x",
                             "package_seal": "seal-x",
                             "run_id": "run-20260922T110000Z-other001",
                             "destination": "youtube"})
    return tmp_path, store_path, run_id, policy


def test_analytics_lineage_mismatch_is_invalid_not_guessed(mismatch_repo):
    tmp, store_path, run_id, policy = mismatch_repo
    report = effectiveness_for_run(tmp / "data" / "runs", run_id,
                                   policy_db_path=store_path,
                                   ledger_path=ledger_path(tmp),
                                   analytics_db_path=analytics_path(tmp))
    record = report["records"][0]
    assert record["evidence_status"] == "invalid"
    assert any("analytics_run_id_mismatch" in reason
               for reason in record["invalid_reasons"])
    assert "effectiveness_id" not in record  # no identity for bad evidence
    verify = verify_effectiveness(tmp / "data" / "runs",
                                  policy_db_path=store_path,
                                  ledger_path=ledger_path(tmp),
                                  analytics_db_path=analytics_path(tmp))
    assert verify["consistent"] is False
    assert verify["invalid_records"][0]["reasons"]


def test_corrupt_analytics_lineage_is_reported(mismatch_repo):
    tmp, store_path, run_id, _policy = mismatch_repo
    add_observation(tmp, "vidEffX00001", run_id, "obs-effx000000000002",
                    [stat("views", 6)], raw_lineage="not-json{")
    report = effectiveness_for_run(tmp / "data" / "runs", run_id,
                                   policy_db_path=store_path,
                                   ledger_path=ledger_path(tmp),
                                   analytics_db_path=analytics_path(tmp))
    bad = [r for r in report["records"]
           if any("analytics_lineage_corrupt" in reason
                  for reason in r["invalid_reasons"])]
    assert bad, "corrupt observation lineage must be reported, not dropped"


def test_ambiguous_publication_fails_closed(tmp_path):
    store_path, policy = make_active_policy(tmp_path / "fixture")
    run_id = "run-20260922T110000Z-efy000000001"
    make_run_dir(tmp_path, run_id)
    event = consume(tmp_path, run_id, make_context(store_path),
                    decision_id="req-eff-y0000000001")
    register(tmp_path, run_id, event)
    publish_run(tmp_path, run_id, "vidEffY00001", seal="seal-y-1",
                package_id="pkg-y-1")
    publish_run(tmp_path, run_id, "vidEffY99999", seal="seal-y-2",
                package_id="pkg-y-2")
    report = effectiveness_for_run(
        tmp_path / "data" / "runs", run_id, policy_db_path=store_path,
        ledger_path=ledger_path(tmp_path),
        analytics_db_path=analytics_path(tmp_path))
    record = report["records"][0]
    assert record["evidence_status"] == "invalid"
    assert record["invalid_reasons"] == ["ambiguous_publication"]
    assert record["measurements"] == []


def test_corrupt_consumption_artifact_is_excluded(tmp_path):
    store_path, _policy = make_active_policy(tmp_path / "fixture")
    run_id = "run-20260922T110000Z-efz000000001"
    run_dir = make_run_dir(tmp_path, run_id)
    event = consume(tmp_path, run_id, make_context(store_path),
                    decision_id="req-eff-z0000000001")
    register(tmp_path, run_id, event)
    artifact = run_dir / "policy_consumption.json"
    mutated = json.loads(artifact.read_text(encoding="utf-8"))
    mutated["policy_version"] = 99
    artifact.write_text(json.dumps(mutated), encoding="utf-8")
    publish_run(tmp_path, run_id, "vidEffZ00001")
    report = effectiveness_for_run(
        tmp_path / "data" / "runs", run_id, policy_db_path=store_path,
        ledger_path=ledger_path(tmp_path),
        analytics_db_path=analytics_path(tmp_path))
    assert report["evidence_status"] == "corrupt"
    assert report["records"] == []
    verify = verify_effectiveness(tmp_path / "data" / "runs",
                                  policy_db_path=store_path,
                                  ledger_path=ledger_path(tmp_path),
                                  analytics_db_path=analytics_path(tmp_path))
    assert verify["consistent"] is False
    assert verify["runs_corrupt"] == 1


def test_run_without_consumption_artifact_has_no_records(tmp_path):
    store_path, _policy = make_active_policy(tmp_path / "fixture")
    make_run_dir(tmp_path, "run-20260922T110000Z-efn000000001")
    report = effectiveness_for_run(
        tmp_path / "data" / "runs", "run-20260922T110000Z-efn000000001",
        policy_db_path=store_path, ledger_path=ledger_path(tmp_path),
        analytics_db_path=analytics_path(tmp_path))
    assert report["ok"] is True
    assert report["evidence_status"] == "missing"
    assert report["record_count"] == 0


# ---- Read-only safety (§20/§33) -----------------------------------------------------------


def test_effectiveness_never_mutates_any_canonical_store(repo):
    tmp = repo["tmp_path"]
    run_a = repo["run_a"]
    run_dir = tmp / "data" / "runs" / run_a
    watched = {
        "policy": digest(repo["store_path"]),
        "ledger": digest(ledger_path(tmp)),
        "analytics": digest(analytics_path(tmp)),
        "consumption": digest(run_dir / "policy_consumption.json"),
        "manifest": digest(run_dir / "artifacts.json"),
    }
    args = eff(tmp, repo["store_path"])
    runs = tmp_runs(repo)
    effectiveness_for_run(runs, run_a, **args)
    effectiveness_for_policy(runs, repo["policy_1"]["policy_id"], **args)
    metric_effectiveness_summary(runs, "views", **args)
    verify_effectiveness(runs, **args)
    assert digest(repo["store_path"]) == watched["policy"]
    assert digest(ledger_path(tmp)) == watched["ledger"]
    assert digest(analytics_path(tmp)) == watched["analytics"]
    assert digest(run_dir / "policy_consumption.json") == \
        watched["consumption"]
    assert digest(run_dir / "artifacts.json") == watched["manifest"]


def test_effectiveness_source_has_no_write_capability():
    here = Path(__file__).resolve().parent
    source = (here.parent.parent / "src" / "ayce" / "policy" /
              "effectiveness.py").read_text(encoding="utf-8")
    for forbidden in ("INSERT INTO", "UPDATE policies", "DELETE FROM",
                      "approve_candidate", "promote_candidate",
                      "activate_policy", "rollback_policy",
                      "retire_policy", "PolicyStore(",
                      "insert_observation", "insert_measurements"):
        assert forbidden not in source, forbidden


# ---- CLI (§22) -----------------------------------------------------------------------------


def test_cli_effectiveness_run_policy_metric_verify(repo, monkeypatch, capsys):
    from ayce import cli
    tmp = repo["tmp_path"]
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp / "data"))
    monkeypatch.setenv("AYCE_POLICY_DB", str(repo["store_path"]))

    rc = cli.main(["policy", "effectiveness", "run", "--json",
                   "--run-id", repo["run_a"]])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["record_count"] == 1
    assert report["records"][0]["publish"]["youtube_video_id"] == \
        "vidEffA00001"

    rc = cli.main(["policy", "effectiveness", "policy", "--json",
                   "--policy-id", repo["policy_1"]["policy_id"]])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["aggregation"]["consumed_runs"] == 4

    rc = cli.main(["policy", "effectiveness", "metric", "--json",
                   "--metric", "views"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["comparison"] == "descriptive_comparison"
    assert report["policy_count"] == 2

    rc = cli.main(["policy", "effectiveness", "verify", "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["consistent"] is True
    assert report["records_built"] == 7  # A,B,C,D,E,F,G


def test_cli_effectiveness_text_output(repo, monkeypatch, capsys):
    from ayce import cli
    tmp = repo["tmp_path"]
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp / "data"))
    monkeypatch.setenv("AYCE_POLICY_DB", str(repo["store_path"]))
    rc = cli.main(["policy", "effectiveness", "run",
                   "--run-id", repo["run_a"]])
    assert rc == 0
    out = capsys.readouterr().out
    assert "publish_status=published" in out
    assert "missing_data=metric_present" in out

    rc = cli.main(["policy", "effectiveness", "metric", "--metric", "views"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "descriptive comparison ONLY" in out
    assert "no ranking" in out

    rc = cli.main(["policy", "effectiveness", "verify"])
    assert rc == 0
    assert "consistent: True" in capsys.readouterr().out


def test_cli_effectiveness_structured_errors(repo, monkeypatch, capsys):
    from ayce import cli
    tmp = repo["tmp_path"]
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp / "data"))
    monkeypatch.setenv("AYCE_POLICY_DB", str(repo["store_path"]))
    rc = cli.main(["policy", "effectiveness", "run", "--json",
                   "--run-id", "run-20260922T110000Z-nonexist0001"])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["code"] == "policy_effectiveness_invalid"

    rc = cli.main(["policy", "effectiveness", "policy", "--json",
                   "--policy-id", "bad-id"])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["code"] == "policy_effectiveness_invalid"
