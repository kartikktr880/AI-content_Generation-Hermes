"""Stage 6 — Analytics ingestion tests (fake transport, NO network).

Covers the §29 matrix: lineage resolution (published package → video →
run → script → research → objective; missing/inconsistent ledger →
failure), source ingestion (success / empty / partial / unsupported
metrics / source timestamps), snapshot semantics (idempotency, new
observation timestamp, distinct windows), metric semantics (zero vs
missing vs unavailable), persistence (raw + normalized, restart),
API failure normalization (auth, permission, quota, rate limit,
transient, not found, server error), security (no secrets, no arbitrary
paths, lineage cannot be caller-forged), scope, policy isolation and
dry-run. Real Stage 3/4/5 sealed packages and real pipeline runs mirror
the Stage 5 test conventions.
"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ayce.config import Config
from ayce.lineage import objective_id_for
from ayce.pipeline import run_pipeline
from ayce.publish_package import seal_publish_package
from ayce.publishing import PublishError, PublishLedger, PublishRequest
from ayce.publishing.transport import HttpResponse
from ayce.analytics import (
    AnalyticsError,
    AnalyticsStore,
    collect_analytics,
    resolve_publication,
    show_analytics,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ASSETS_DIR = FIXTURES / "asset_provider"
NARRATION_DIR = FIXTURES / "narration_fixtures"

RESEARCH_ID = "res-20260920T090318Z-e8f295ac6ec8"
SCRIPT_ID = "brief-e8f295ac6ec8"
OBJECTIVE = "Identify outlier opening-hook formats in the AI productivity tools niche"
VIDEO_ID = "vidFake12345678"
NOW = "2026-09-20T16:00:00.000Z"
NOW2 = "2026-09-21T16:00:00.000Z"
WINDOW = ("2026-09-13", "2026-09-19")


def make_config(tmp_path: Path) -> Config:
    return replace(Config.from_env(env={}), data_dir=tmp_path / "data")


def _body(status: int, payload) -> HttpResponse:
    return HttpResponse(status=status, headers={},
                        body=json.dumps(payload).encode("utf-8"))


class FakeTokenProvider:
    """Duck-typed TokenProvider with scripted behavior (no network)."""

    def __init__(self, *, client_id="client-123", token="fake-access-token",
                 error: Exception | None = None):
        self._client_id = client_id
        self._token = token
        self._error = error
        self.calls = 0

    @property
    def client_id(self) -> str:
        return self._client_id

    def get_access_token(self) -> str:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._token


class FakeAnalyticsTransport:
    """Scripted fake of the official read-only analytics protocol (§28).

    Queues are consumed FIFO; ``None`` produces the default success
    response. Records every call; the access token is never stored.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.report_responses: list = []
        self.stats_responses: list = []
        self.statistics: dict = {"viewCount": "1234", "likeCount": "56",
                                 "commentCount": "7"}

    def query_report(self, access_token, params):
        self.calls.append(("query_report", dict(params)))
        response = self._next(self.report_responses)
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response
        return _body(200, {
            "columnHeaders": [
                {"name": m} for m in [
                    "views", "estimatedMinutesWatched", "averageViewDuration",
                    "averageViewPercentage", "subscribersGained",
                    "subscribersLost",
                ]
            ],
            "rows": [[100, 42.5, 95.0, 38.2, 3, 1]],
        })

    def get_video_statistics(self, access_token, video_id):
        self.calls.append(("get_video_statistics", video_id))
        response = self._next(self.stats_responses)
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response
        return _body(200, {"items": [{"id": video_id,
                                      "statistics": dict(self.statistics)}]})

    @staticmethod
    def _next(queue: list):
        return queue.pop(0) if queue else None
# ---- fixtures: one real published run (full Golden Path + ledger) -------------


def _write_research_artifact(tmp_path: Path) -> str:
    artifact = {
        "schema_version": "1.0",
        "research_id": RESEARCH_ID,
        "objective": OBJECTIVE,
        "niche": "AI productivity tools",
        "query": "AI productivity tools",
        "collected_at": "2026-09-20T09:03:18.260Z",
        "status": "PARTIAL",
        "tiers": ["OBSERVED_FACT", "DERIVED_METRIC", "MODEL_INFERENCE",
                  "CREATIVE_HYPOTHESIS"],
        "candidates": [],
        "provenance": {"worker_version": "1.0",
                       "collected_at": "2026-09-20T09:03:18.260Z",
                       "request_digest": "ab" * 32},
    }
    store = tmp_path / "data" / "research"
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{RESEARCH_ID}.json").write_text(json.dumps(artifact),
                                               encoding="utf-8")
    return objective_id_for(OBJECTIVE)


def _write_generated_script(tmp_path: Path) -> Path:
    script = {
        "production_id": RESEARCH_ID,
        "title": "Research brief: Identify outlier opening-hook formats",
        "scenes": [
            {"narration_text": "Here is an opening hook recorded by the "
                               "research worker.",
             "visual_description": "High-contrast opening title card."},
            {"narration_text": "This video is built directly from research "
                               "evidence.",
             "visual_description": "Clean text card naming the research "
                                   "objective."},
        ],
    }
    path = tmp_path / "script.json"
    path.write_text(json.dumps(script), encoding="utf-8")
    return path


class _FakePublishTransport:
    """Minimal upload fake to get a VERIFIED publication into the real
    ledger (mirrors the Stage 5 fake protocol)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._metadata: dict = {}

    def initiate_resumable(self, access_token, metadata, file_size):
        self.calls.append(("initiate", dict(metadata), file_size))
        self._metadata = metadata
        return HttpResponse(status=200, headers={"Location": "https://fake/session"})

    def put_chunk(self, session_url, data, offset, total_size):
        self.calls.append(("put_chunk", session_url, offset, len(data)))
        end = offset + len(data) - 1
        if end >= total_size - 1:
            return HttpResponse(status=200, headers={"Range": f"bytes=0-{end}"},
                                body=json.dumps({"id": VIDEO_ID}).encode("utf-8"))
        return HttpResponse(status=308, headers={"Range": f"bytes=0-{end}"})

    def upload_status(self, session_url, total_size):
        self.calls.append(("status", session_url))
        return HttpResponse(status=200, headers={"Range": "bytes=0-0"})

    def get_video(self, access_token, video_id):
        self.calls.append(("get_video", video_id))
        meta = self._metadata
        return _body(200, {"items": [{"id": video_id,
                                      "snippet": dict(meta.get("snippet") or {}),
                                      "status": dict(meta.get("status") or {})}]})


@pytest.fixture()
def published_run(tmp_path):
    """One real research-derived run, sealed AND published through the
    real Stage 5 publisher against a fake upload transport."""
    from ayce.publishing import publish_package_to_youtube
    _write_research_artifact(tmp_path)
    script_path = _write_generated_script(tmp_path)
    config = make_config(tmp_path)
    result = run_pipeline(script_path, config=config,
                          assets_dir=ASSETS_DIR, narration_dir=NARRATION_DIR)
    assert result.ok and result.qa_verdict == "PASS", result.error
    package = seal_publish_package(result.run_dir)
    ledger = PublishLedger(tmp_path / "data" / "publishing" / "ledger.sqlite3")
    transport = _FakePublishTransport()
    report = publish_package_to_youtube(
        result.run_id, PublishRequest(), config=config, ledger=ledger,
        token_provider=FakeTokenProvider(), transport=transport,
        backoff_base=0.0, now_fn=lambda: NOW)
    assert report["ok"] is True and report["status"] == "published", report
    return {"tmp_path": tmp_path, "config": config, "result": result,
            "package": package, "run_id": result.run_id, "ledger": ledger,
            "publish_report": report}


def make_analytics_env(run):
    """Analytics collector wired to the fake transport + fake tokens."""
    store = AnalyticsStore(run["tmp_path"] / "data" / "analytics" /
                           "analytics.sqlite3")
    tokens = FakeTokenProvider()
    transport = FakeAnalyticsTransport()
    clock = {"value": NOW}

    def now_fn():
        return clock["value"]

    def collect(**kwargs):
        # default window applies only when the Analytics API v2 source is
        # requested and the caller did not supply an explicit window
        sources = kwargs.get("sources")
        if kwargs.get("window_start") is None and \
                kwargs.get("window_end") is None and \
                (sources is None or "ANALYTICS_API_V2" in sources):
            kwargs["window_start"] = WINDOW[0]
            kwargs["window_end"] = WINDOW[1]
        return collect_analytics(
            run["run_id"], config=run["config"], ledger=run["ledger"],
            token_provider=tokens, transport=transport, store=store,
            backoff_base=0.0, now_fn=now_fn, **kwargs)

    return {"store": store, "tokens": tokens, "transport": transport,
            "clock": clock, "collect": collect}

# ---- lineage (§7/§23/§24) ------------------------------------------------------


def test_collect_resolves_full_lineage(published_run):
    env = make_analytics_env(published_run)
    report = env["collect"]()
    assert report["ok"] is True
    lineage = report["lineage"]
    assert lineage["youtube_video_id"] == VIDEO_ID
    assert lineage["run_id"] == published_run["run_id"]
    assert lineage["package_id"] == published_run["package"]["package_id"]
    assert lineage["package_seal"] == \
        published_run["package"]["seal"]["value"]
    assert lineage["script_id"] == SCRIPT_ID
    assert lineage["research_id"] == RESEARCH_ID
    assert lineage["objective_id"] == objective_id_for(OBJECTIVE)
    assert lineage["destination"]  # from the ledger record


def test_collect_by_video_id_resolves_same_lineage(published_run):
    env = make_analytics_env(published_run)
    by_video = collect_analytics(
        VIDEO_ID, config=published_run["config"],
        ledger=published_run["ledger"], token_provider=env["tokens"],
        transport=env["transport"], store=env["store"],
        window_start=WINDOW[0], window_end=WINDOW[1],
        backoff_base=0.0, now_fn=lambda: NOW)
    assert by_video["ok"] is True
    assert by_video["lineage"]["run_id"] == published_run["run_id"]
    assert by_video["lineage"]["youtube_video_id"] == VIDEO_ID


def test_missing_publication_ledger_fails(published_run):
    # a sealed run that was NEVER published: no ledger entry exists
    env = make_analytics_env(published_run)
    result2 = run_pipeline(
        _write_generated_script(published_run["tmp_path"]),
        config=published_run["config"], assets_dir=ASSETS_DIR,
        narration_dir=NARRATION_DIR)
    assert result2.ok
    seal_publish_package(result2.run_dir)
    with pytest.raises(AnalyticsError) as excinfo:
        collect_analytics(result2.run_id, config=published_run["config"],
                          ledger=published_run["ledger"],
                          token_provider=env["tokens"],
                          transport=env["transport"], store=env["store"],
                          backoff_base=0.0, now_fn=lambda: NOW)
    assert excinfo.value.code == "analytics_lineage_invalid"
    assert env["store"].list_observations() == []   # NOTHING attributed


def test_unknown_video_id_fails_without_attribution(published_run):
    env = make_analytics_env(published_run)
    with pytest.raises(AnalyticsError) as excinfo:
        collect_analytics("zzzzzzzzzzz",  # 11 chars, no ledger entry
                          config=published_run["config"],
                          ledger=published_run["ledger"],
                          token_provider=env["tokens"],
                          transport=env["transport"], store=env["store"],
                          backoff_base=0.0, now_fn=lambda: NOW)
    assert excinfo.value.code == "analytics_lineage_invalid"


def test_unpublished_status_fails(published_run):
    # a ledger row that never reached ``published`` carries no analytics
    env = make_analytics_env(published_run)
    record = published_run["ledger"].find_by_run_id(published_run["run_id"])[0]
    published_run["ledger"].update(record.package_seal, record.destination,
                                   status="reconciliation_pending",
                                   now=NOW)
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"]()
    assert excinfo.value.code == "analytics_lineage_invalid"
    assert env["store"].list_observations() == []


def test_inconsistent_ledger_package_fails(published_run):
    # tamper with the sealed package AFTER publication → seal mismatch
    env = make_analytics_env(published_run)
    package_path = published_run["result"].run_dir / "publish_package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["production_id"] = "res-99990101T000000Z-tamper000001"
    package_path.write_text(json.dumps(package), encoding="utf-8")
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"]()
    assert excinfo.value.code == "analytics_lineage_invalid"
    assert env["store"].list_observations() == []


def test_forged_package_fails_against_ledger(published_run):
    # replace the package with a DIFFERENT VALID sealed package (another
    # run): seal-valid but the ledger record disagrees → no attribution
    env = make_analytics_env(published_run)
    result2 = run_pipeline(
        _write_generated_script(published_run["tmp_path"]),
        config=published_run["config"], assets_dir=ASSETS_DIR,
        narration_dir=NARRATION_DIR)
    assert result2.ok
    forged = seal_publish_package(result2.run_dir)
    (published_run["result"].run_dir / "publish_package.json").write_text(
        json.dumps(forged), encoding="utf-8")
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"]()
    assert excinfo.value.code == "analytics_lineage_invalid"

# ---- source ingestion (§5/§8) ---------------------------------------------------


def test_successful_collection_persists_raw_and_normalized(published_run):
    env = make_analytics_env(published_run)
    report = env["collect"]()
    assert report["ok"] is True and len(report["observations"]) == 2
    by_source = {o["source"]: o for o in report["observations"]}
    stats = by_source["DATA_API_V3"]
    assert stats["scope"] == "video"
    stats_metrics = {m["metric_name"]: m for m in stats["metrics"]}
    assert stats_metrics["views"]["value"] == 1234.0
    assert stats_metrics["views"]["raw_value"] == "1234"  # verbatim
    assert stats_metrics["views"]["availability"] == "present"
    assert stats_metrics["views"]["unit"] == "count"
    analytics = by_source["ANALYTICS_API_V2"]
    assert analytics["window_start"] == WINDOW[0]
    assert analytics["window_end"] == WINDOW[1]
    analytics_metrics = {m["metric_name"]: m for m in analytics["metrics"]}
    assert analytics_metrics["estimatedMinutesWatched"]["value"] == 42.5
    assert analytics_metrics["estimatedMinutesWatched"]["unit"] == "minutes"
    assert analytics_metrics["subscribersGained"]["value"] == 3.0
    # raw layer preserved verbatim
    stored = env["store"].get_observation(stats["observation_id"])
    assert json.loads(stored["raw_response"])["items"][0]["statistics"][
        "viewCount"] == "1234"
    stored_a = env["store"].get_observation(analytics["observation_id"])
    assert json.loads(stored_a["raw_response"])["rows"] == \
        [[100, 42.5, 95.0, 38.2, 3, 1]]
    # request recorded without secrets
    assert "access_token" not in json.dumps(stored_a["source_request"])
    # normalized layer persisted with lineage
    measurements = env["store"].measurements_for_video(VIDEO_ID)
    assert len(measurements) == 9  # 3 stats + 6 analytics
    assert all(m["lineage"]["run_id"] == published_run["run_id"]
               for m in measurements)


def test_empty_analytics_response_records_missing(published_run):
    env = make_analytics_env(published_run)
    env["transport"].report_responses.append(
        _body(200, {"columnHeaders": [{"name": "views"}], "rows": []}))
    report = env["collect"]()
    analytics = [o for o in report["observations"]
                 if o["source"] == "ANALYTICS_API_V2"][0]
    assert all(m["availability"] == "missing" for m in analytics["metrics"])
    assert all(m["value"] is None for m in analytics["metrics"])
    assert env["store"].get_observation(analytics["observation_id"]) is not None


def test_partial_response_missing_column(published_run):
    env = make_analytics_env(published_run)
    env["transport"].report_responses.append(_body(200, {
        "columnHeaders": [{"name": "views"}],
        "rows": [[50]],
    }))
    report = env["collect"](sources=["ANALYTICS_API_V2"])
    metrics = {m["metric_name"]: m for m in report["observations"][0]["metrics"]}
    assert metrics["views"]["availability"] == "present"
    assert metrics["views"]["value"] == 50.0
    assert metrics["estimatedMinutesWatched"]["availability"] == "missing"
    assert metrics["estimatedMinutesWatched"]["value"] is None


def test_unsupported_metric_refused_before_network(published_run):
    env = make_analytics_env(published_run)
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](metrics=["impressions"])
    assert excinfo.value.code == "analytics_metric_unavailable"
    assert env["transport"].calls == []            # NO external contact
    assert env["tokens"].calls == 0                # NO credential use
    assert env["store"].list_observations() == []  # NOTHING stored


def test_unknown_metric_name_refused(published_run):
    env = make_analytics_env(published_run)
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](metrics=["totally_bogus_metric"])
    assert excinfo.value.code == "analytics_metric_unavailable"
    assert env["transport"].calls == []


def test_source_timestamp_and_request_recorded(published_run):
    env = make_analytics_env(published_run)
    report = env["collect"]()
    for obs in report["observations"]:
        assert obs["observed_at"] == NOW  # explicit, caller-controlled
        stored = env["store"].get_observation(obs["observation_id"])
        assert stored["observed_at"] == NOW
        assert stored["collected_at"] == NOW
        request = stored["source_request"]
        if obs["source"] == "ANALYTICS_API_V2":
            assert request["params"]["filters"] == f"video=={VIDEO_ID}"
            assert request["params"]["startDate"] == WINDOW[0]
            assert request["params"]["endDate"] == WINDOW[1]
            assert request["endpoint"] == "reports.query"
        else:
            assert request["endpoint"] == "videos.list"

# ---- snapshot semantics (§11/§12/§13/§22) ----------------------------------------


def test_repeated_identical_ingestion_is_idempotent(published_run):
    env = make_analytics_env(published_run)
    first = env["collect"]()
    before = env["store"].list_observations()
    measurements_before = env["store"].measurements_for_video(VIDEO_ID)
    second = env["collect"]()  # same clock → identical observation identity
    after = env["store"].list_observations()
    assert len(second["observations"]) == 2
    assert all(o["duplicate"] for o in second["observations"])
    assert after == before                        # NOTHING new persisted
    assert env["store"].measurements_for_video(VIDEO_ID) == measurements_before
    # the deterministic identity is the dedup mechanism (no random ids)
    first_ids = sorted(o["observation_id"] for o in first["observations"])
    second_ids = sorted(o["observation_id"] for o in second["observations"])
    assert first_ids == second_ids


def test_new_observation_timestamp_creates_new_record(published_run):
    env = make_analytics_env(published_run)
    first = env["collect"]()
    env["clock"]["value"] = NOW2
    second = env["collect"]()
    ids1 = {o["observation_id"] for o in first["observations"]}
    ids2 = {o["observation_id"] for o in second["observations"]}
    assert not ids1 & ids2                        # distinct identities
    assert all(not o["duplicate"] for o in second["observations"])
    assert len(env["store"].list_observations()) == 4   # both retained
    # the first observation is NOT overwritten — history reconstructable
    first_stats = [o for o in first["observations"]
                   if o["source"] == "DATA_API_V3"][0]
    first_stored = env["store"].get_observation(first_stats["observation_id"])
    assert first_stored["observed_at"] == NOW
    assert json.loads(first_stored["raw_response"])["items"][0][
        "statistics"]["viewCount"] == "1234"


def test_different_window_is_a_distinct_observation(published_run):
    env = make_analytics_env(published_run)
    first = env["collect"]()
    second = env["collect"](window_start="2026-09-01",
                            window_end="2026-09-07")
    ids1 = {o["observation_id"] for o in first["observations"]
            if o["source"] == "ANALYTICS_API_V2"}
    ids2 = {o["observation_id"] for o in second["observations"]
            if o["source"] == "ANALYTICS_API_V2"}
    assert not ids1 & ids2
    analytics_obs = [o for o in second["observations"]
                     if o["source"] == "ANALYTICS_API_V2"][0]
    assert analytics_obs["window_start"] == "2026-09-01"
    assert analytics_obs["window_end"] == "2026-09-07"
    # the repeated lifetime snapshot dedupes; the new window is a new record
    assert len(env["store"].list_observations()) == 3
    for obs in second["observations"]:
        expected_dup = obs["source"] == "DATA_API_V3"
        assert obs["duplicate"] is expected_dup


def test_observation_identity_is_deterministic(published_run):
    from ayce.analytics.collector import observation_identity
    a = observation_identity(source="DATA_API_V3",
                             youtube_video_id=VIDEO_ID, scope="video",
                             metrics=["views", "likes"],
                             window_start=None, window_end=None,
                             observed_at=NOW)
    b = observation_identity(source="DATA_API_V3",
                             youtube_video_id=VIDEO_ID, scope="video",
                             metrics=["likes", "views"],
                             window_start=None, window_end=None,
                             observed_at=NOW)
    assert a == b                                  # metric-order independent
    c = observation_identity(source="DATA_API_V3",
                             youtube_video_id=VIDEO_ID, scope="video",
                             metrics=["views"], window_start=None,
                             window_end=None, observed_at=NOW2)
    assert a != c                                  # timestamp in identity
    d = observation_identity(source="ANALYTICS_API_V2",
                             youtube_video_id=VIDEO_ID, scope="video",
                             metrics=["views"], window_start=WINDOW[0],
                             window_end=WINDOW[1], observed_at=NOW)
    e = observation_identity(source="ANALYTICS_API_V2",
                             youtube_video_id=VIDEO_ID, scope="video",
                             metrics=["views"], window_start="2026-09-01",
                             window_end="2026-09-07", observed_at=NOW)
    assert d != e                                  # window in identity

# ---- metric semantics (§10/§19) ---------------------------------------------------


def test_zero_is_retained_as_zero_not_missing(published_run):
    env = make_analytics_env(published_run)
    env["transport"].statistics = {"viewCount": "0", "likeCount": "0",
                                   "commentCount": "0"}
    report = env["collect"](sources=["DATA_API_V3"])
    metrics = {m["metric_name"]: m
               for m in report["observations"][0]["metrics"]}
    assert metrics["views"]["availability"] == "zero"
    assert metrics["views"]["value"] == 0.0
    assert metrics["views"]["raw_value"] == "0"
    assert metrics["likes"]["availability"] == "zero"
    assert metrics["comments"]["availability"] == "zero"
    stored = env["store"].get_observation(
        report["observations"][0]["observation_id"])
    assert stored is not None


def test_missing_is_distinct_from_zero_and_unavailable(published_run):
    env = make_analytics_env(published_run)
    # missing: the source returned NO rows for the window
    env["transport"].report_responses.append(
        _body(200, {"columnHeaders": [{"name": "views"}], "rows": []}))
    report = env["collect"](sources=["ANALYTICS_API_V2"])
    missing = {m["metric_name"]: m
               for m in report["observations"][0]["metrics"]}
    assert missing["views"]["availability"] == "missing"
    assert missing["views"]["value"] is None
    assert missing["views"]["raw_value"] is None
    # zero stays zero on the other source (0 ≠ missing ≠ unavailable)
    env2 = make_analytics_env(published_run)
    env2["transport"].statistics = {"viewCount": "0"}
    report2 = env2["collect"](sources=["DATA_API_V3"])
    zero = {m["metric_name"]: m
            for m in report2["observations"][0]["metrics"]}
    assert zero["views"]["availability"] == "zero"
    assert zero["views"]["value"] == 0.0


def test_unavailable_metric_is_rejected_not_zeroed(published_run):
    env = make_analytics_env(published_run)
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](metrics=["views", "clickThroughRate"])
    assert excinfo.value.code == "analytics_metric_unavailable"
    assert excinfo.value.details["unsupported"] == ["clickThroughRate"]
    assert excinfo.value.details["reporting_api_only"] == ["clickThroughRate"]
    assert env["store"].list_observations() == []


# ---- persistence (§12/§14/§30) ------------------------------------------------------


def test_process_restart_preserves_history(published_run):
    env = make_analytics_env(published_run)
    env["collect"]()
    env["clock"]["value"] = NOW2
    env["collect"]()
    # a COMPLETELY NEW store object over the same file (process restart)
    store2 = AnalyticsStore(published_run["tmp_path"] / "data" /
                            "analytics" / "analytics.sqlite3")
    observations = store2.list_observations()
    assert len(observations) == 4
    assert {o["observed_at"] for o in observations} == {NOW, NOW2}
    assert len(store2.measurements_for_video(VIDEO_ID)) == 18
    raw = json.loads(observations[0]["raw_response"])
    assert isinstance(raw, dict)   # raw responses survive the restart


def test_reopening_a_stored_observation_changes_nothing(published_run):
    env = make_analytics_env(published_run)
    report = env["collect"]()
    stored_before = env["store"].get_observation(
        report["observations"][0]["observation_id"])
    env["collect"]()  # repeat → idempotent
    stored_after = env["store"].get_observation(
        report["observations"][0]["observation_id"])
    assert stored_before == stored_after          # §12 immutability

# ---- API failure normalization + bounded retries (§20/§21/§30) ------------------


@pytest.mark.parametrize("status,payload,expected", [
    (401, {}, "analytics_authentication_error"),
    (403, {}, "analytics_authorization_error"),
    (403, {"error": {"errors": [{"reason": "quotaExceeded"}]}},
     "analytics_quota_error"),
    (429, {}, "analytics_rate_limited"),
    (404, {}, "analytics_not_found"),
    (500, {}, "analytics_server_error"),
    (400, {}, "analytics_invalid_request"),
])
def test_api_errors_are_normalized(published_run, status, payload, expected):
    env = make_analytics_env(published_run)
    env["transport"].report_responses.append(_body(status, payload))
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](sources=["ANALYTICS_API_V2"], max_retries=0)
    assert excinfo.value.code == expected
    assert env["store"].list_observations() == []  # nothing fabricated


def test_transient_network_error_is_classified(published_run):
    import urllib.error
    env = make_analytics_env(published_run)
    env["transport"].report_responses.append(
        urllib.error.URLError("connection reset"))
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](sources=["ANALYTICS_API_V2"], max_retries=0)
    assert excinfo.value.code == "analytics_transient_network_error"
    assert env["store"].list_observations() == []


def test_rate_limit_is_retried_bounded_then_succeeds(published_run):
    env = make_analytics_env(published_run)
    env["transport"].report_responses.append(_body(429, {}))   # first try
    report = env["collect"](sources=["ANALYTICS_API_V2"])      # retry wins
    assert report["ok"] is True
    report_calls = [c for c in env["transport"].calls
                    if c[0] == "query_report"]
    assert len(report_calls) == 2                    # exactly one retry


def test_rate_limit_retry_exhaustion_is_truthful(published_run):
    env = make_analytics_env(published_run)
    env["transport"].report_responses.append(_body(429, {}))
    env["transport"].report_responses.append(_body(429, {}))
    env["transport"].report_responses.append(_body(429, {}))
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](sources=["ANALYTICS_API_V2"], max_retries=2)
    assert excinfo.value.code == "analytics_rate_limited"
    assert len([c for c in env["transport"].calls
                if c[0] == "query_report"]) == 3     # bounded, no infinite loop


def test_permanent_errors_are_never_retried(published_run):
    env = make_analytics_env(published_run)
    env["transport"].report_responses.append(_body(401, {}))
    env["transport"].report_responses.append(_body(200, {"rows": []}))
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](sources=["ANALYTICS_API_V2"])
    assert excinfo.value.code == "analytics_authentication_error"
    assert len([c for c in env["transport"].calls
                if c[0] == "query_report"]) == 1     # NO retry


def test_timeout_is_transient(published_run):
    env = make_analytics_env(published_run)
    env["transport"].report_responses.append(TimeoutError("timed out"))
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](sources=["ANALYTICS_API_V2"], max_retries=0)
    assert excinfo.value.code == "analytics_transient_network_error"


def test_malformed_response_is_unknown_error(published_run):
    env = make_analytics_env(published_run)
    from ayce.publishing.transport import HttpResponse as _HR
    env["transport"].report_responses.append(
        _HR(status=200, headers={}, body=b"<html>not json</html>"))
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](sources=["ANALYTICS_API_V2"])
    assert excinfo.value.code == "analytics_unknown_error"
    assert env["store"].list_observations() == []


def test_data_api_empty_items_is_not_found(published_run):
    env = make_analytics_env(published_run)
    env["transport"].stats_responses.append(_body(200, {"items": []}))
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](sources=["DATA_API_V3"])
    assert excinfo.value.code == "analytics_not_found"
    assert env["store"].list_observations() == []


def test_partial_failure_persists_nothing(published_run):
    # all-or-nothing: the Data API succeeds but Analytics fails →
    # NO observation of ANY source is persisted
    env = make_analytics_env(published_run)
    env["transport"].report_responses.append(_body(500, {}))
    with pytest.raises(AnalyticsError):
        env["collect"](max_retries=0)
    assert env["store"].list_observations() == []
    assert env["store"].measurements_for_video(VIDEO_ID) == []


# ---- security (§29) -----------------------------------------------------------------


def test_no_secrets_persisted_or_reported(published_run):
    env = make_analytics_env(published_run)
    report = env["collect"]()
    secret = "fake-access-token"
    assert secret not in json.dumps(report)
    db_bytes = (published_run["tmp_path"] / "data" / "analytics" /
                "analytics.sqlite3").read_bytes()
    assert secret.encode("utf-8") not in db_bytes
    for obs in env["store"].list_observations():
        assert secret not in json.dumps(obs["source_request"])


def test_no_secrets_in_logs(published_run, capsys):
    env = make_analytics_env(published_run)
    env["collect"]()
    captured = capsys.readouterr()
    assert "fake-access-token" not in captured.out + captured.err


def test_arbitrary_paths_are_rejected(published_run):
    env = make_analytics_env(published_run)
    for bad in ("../../etc", "..\\..\\windows", "run-..\\x", "a" * 70):
        with pytest.raises(AnalyticsError) as excinfo:
            resolve_publication(published_run["config"],
                                published_run["ledger"], bad)
        assert excinfo.value.code == "analytics_lineage_invalid"

# ---- scope (§18) ----------------------------------------------------------------------


def test_scope_and_windows_are_explicit(published_run):
    env = make_analytics_env(published_run)
    report = env["collect"]()
    for obs in report["observations"]:
        assert obs["scope"] == "video"          # video-level stays video-level
    analytics = [o for o in report["observations"]
                 if o["source"] == "ANALYTICS_API_V2"][0]
    assert analytics["window_start"] == WINDOW[0]
    assert analytics["window_end"] == WINDOW[1]
    assert analytics["window_timezone"] == "America/Los_Angeles"
    stats = [o for o in report["observations"]
             if o["source"] == "DATA_API_V3"][0]
    assert stats["window_start"] is None and stats["window_end"] is None
    stored = env["store"].measurements_for_video(VIDEO_ID)
    assert all(m["scope"] == "video" for m in stored)
    assert all(m["window_start"] == (WINDOW[0] if m["source"] ==
                                     "ANALYTICS_API_V2" else None)
               for m in stored)


def test_window_validation(published_run):
    env = make_analytics_env(published_run)
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](window_start="2026-09-13", window_end=None)
    assert excinfo.value.code == "analytics_invalid_request"
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](window_start="2026-09-19", window_end="2026-09-13")
    assert excinfo.value.code == "analytics_invalid_request"
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](window_start="not-a-date", window_end="2026-09-19")
    assert excinfo.value.code == "analytics_invalid_request"
    # windows make no sense for the lifetime snapshot alone
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](sources=["DATA_API_V3"], window_start="2026-09-13",
                       window_end="2026-09-19")
    assert excinfo.value.code == "analytics_invalid_request"
    assert env["transport"].calls == []

# ---- policy isolation (§4/§16/§25/§36) --------------------------------------------------


def test_collection_never_modifies_production_or_publication_state(
        published_run):
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    run_dir = published_run["result"].run_dir
    watched = {
        "state.json": digest(run_dir / "state.json"),
        "publish_package.json": digest(run_dir / "publish_package.json"),
        "artifacts.json": digest(run_dir / "artifacts.json"),
        "qa_report.json": digest(run_dir / "qa_report.json"),
        "research": digest(published_run["tmp_path"] / "data" / "research" /
                           f"{RESEARCH_ID}.json"),
    }
    record_before = published_run["ledger"].find_by_run_id(
        published_run["run_id"])[0].to_dict()

    env = make_analytics_env(published_run)
    env["collect"]()
    env["collect"](window_start="2026-09-01", window_end="2026-09-07")

    assert {k: digest(run_dir / k) for k in
            ("state.json", "publish_package.json", "artifacts.json",
             "qa_report.json")} == \
        {k: watched[k] for k in ("state.json", "publish_package.json",
                                 "artifacts.json", "qa_report.json")}
    assert digest(published_run["tmp_path"] / "data" / "research" /
                  f"{RESEARCH_ID}.json") == watched["research"]
    record_after = published_run["ledger"].find_by_run_id(
        published_run["run_id"])[0].to_dict()
    assert record_after == record_before         # ledger untouched


def test_report_contains_no_policy_fields(published_run):
    env = make_analytics_env(published_run)
    report = env["collect"]()
    text = json.dumps(report)
    for banned in ("best_video", "winner", "loser", "recommended_topic",
                   "recommended_hook", "increase_posting_frequency",
                   "change_policy", "recommendation", "score", "rank"):
        assert banned not in text, banned
    stored = json.dumps(env["store"].list_observations()) + json.dumps(
        env["store"].measurements_for_video(VIDEO_ID))
    for banned in ("best_video", "winner", "recommended_", "policy"):
        assert banned not in stored, banned


# ---- dry run (§27) ----------------------------------------------------------------------


def test_dry_run_resolves_and_validates_but_never_persists(published_run):
    from ayce.analytics.collector import DEFAULT_METRICS
    env = make_analytics_env(published_run)
    report = env["collect"](dry_run=True)
    assert report["ok"] is True and report["dry_run"] is True
    assert report["lineage"]["run_id"] == published_run["run_id"]
    assert report["youtube_video_id"] == VIDEO_ID
    plans = report["would_collect"]
    assert {p["source"] for p in plans} == {"DATA_API_V3",
                                            "ANALYTICS_API_V2"}
    analytics_plan = [p for p in plans
                      if p["source"] == "ANALYTICS_API_V2"][0]
    assert analytics_plan["window_timezone"] == "America/Los_Angeles"
    assert analytics_plan["metrics"] == list(
        DEFAULT_METRICS["ANALYTICS_API_V2"])
    stats_plan = [p for p in plans if p["source"] == "DATA_API_V3"][0]
    assert stats_plan["window_semantics"] == "lifetime_cumulative"
    # NO external contact, NO credentials, NO storage
    assert env["transport"].calls == []
    assert env["tokens"].calls == 0
    assert env["store"].list_observations() == []


def test_dry_run_still_rejects_bad_lineage_and_metrics(published_run):
    env = make_analytics_env(published_run)
    with pytest.raises(AnalyticsError) as excinfo:
        env["collect"](dry_run=True, metrics=["impressions"])
    assert excinfo.value.code == "analytics_metric_unavailable"
    with pytest.raises(AnalyticsError) as excinfo:
        resolve_publication(published_run["config"],
                            published_run["ledger"], "zzzzzzzzzzz")
    assert excinfo.value.code == "analytics_lineage_invalid"

# ---- show (observation read-back) + CLI --------------------------------------------------


def test_show_returns_stored_observations_only(published_run):
    env = make_analytics_env(published_run)
    before = show_analytics(published_run["run_id"],
                            config=published_run["config"],
                            ledger=published_run["ledger"],
                            store=env["store"])
    assert before["ok"] is True
    assert before["observations"] == [] and before["measurements"] == []
    env["collect"]()
    after = show_analytics(VIDEO_ID, config=published_run["config"],
                           ledger=published_run["ledger"],
                           store=env["store"])
    assert len(after["observations"]) == 2
    assert len(after["measurements"]) == 9
    assert after["lineage"]["objective_id"] == objective_id_for(OBJECTIVE)


def _store_observations(run) -> list:
    store = AnalyticsStore(run["tmp_path"] / "data" / "analytics" /
                           "analytics.sqlite3")
    return store.list_observations()


def test_cli_analytics_collect_dry_run_and_show(published_run, capsys,
                                                monkeypatch):
    from ayce import cli
    monkeypatch.setenv("AYCE_DATA_DIR",
                       str(published_run["tmp_path"] / "data"))
    rc = cli.main(["analytics", "collect", published_run["run_id"],
                   "--dry-run", "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["youtube_video_id"] == VIDEO_ID
    assert _store_observations(published_run) == []   # nothing stored

    rc = cli.main(["analytics", "show", published_run["run_id"], "--json"])
    assert rc == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["ok"] is True and shown["observations"] == []

    # non-dry-run without credentials → deterministic auth error, no storage
    monkeypatch.delenv("AYCE_YT_CLIENT_ID", raising=False)
    monkeypatch.delenv("AYCE_YT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("AYCE_YT_REFRESH_TOKEN", raising=False)
    rc = cli.main(["analytics", "collect", published_run["run_id"], "--json"])
    assert rc == 1
    error = json.loads(capsys.readouterr().out)
    assert error["error"]["code"] == "analytics_authentication_error"
    assert _store_observations(published_run) == []


def test_cli_analytics_human_output_dry_run(published_run, capsys,
                                            monkeypatch):
    from ayce import cli
    monkeypatch.setenv("AYCE_DATA_DIR",
                       str(published_run["tmp_path"] / "data"))
    rc = cli.main(["analytics", "collect", published_run["run_id"],
                   "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "would collect" in out
    assert VIDEO_ID in out

