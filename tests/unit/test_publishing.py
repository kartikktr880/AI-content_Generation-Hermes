"""Stage 5 — Verified YouTube publishing tests (fake transport, NO network).

Covers the §31 matrix: package gate (invalid seal / QA fail / missing
render NEVER contact YouTube), metadata validation, OAuth configuration
and refresh, idempotency (seal+destination identity), interrupted upload
recovery, unknown-outcome reconciliation, crash/process-restart recovery,
concurrency, API error normalization, reconciliation outcomes, ledger
integrity, and dry-run. Real Stage 4 sealed packages (real pipeline runs,
real FFmpeg) mirror the Stage 3/4 test conventions.
"""

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from ayce.config import Config
from ayce.lineage import objective_id_for
from ayce.pipeline import run_pipeline
from ayce.publish_package import seal_publish_package
from ayce.publishing import (
    PublishError,
    PublishLedger,
    PublishRequest,
    TokenProvider,
    YouTubeTransport,
    publish_package_to_youtube,
    validate_publish_metadata,
)
from ayce.publishing.errors import categorize_http_status

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ASSETS_DIR = FIXTURES / "asset_provider"
NARRATION_DIR = FIXTURES / "narration_fixtures"

RESEARCH_ID = "res-20260920T090318Z-e8f295ac6ec8"
SCRIPT_ID = "brief-e8f295ac6ec8"
OBJECTIVE = "Identify outlier opening-hook formats in the AI productivity tools niche"
NOW = "2026-09-20T16:00:00.000Z"


def make_config(tmp_path: Path) -> Config:
    return replace(Config.from_env(env={}), data_dir=tmp_path / "data")


class FakeTokenProvider:
    """Duck-typed TokenProvider with scripted behavior (no network)."""

    def __init__(self, *, client_id="client-123", token="fake-access-token",
                 error: PublishError | None = None):
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


class FakeYouTubeTransport(YouTubeTransport):
    """Scripted fake of the official resumable-upload protocol (§30)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.initiate_responses: list = []
        self.chunk_responses: list = []
        self.status_responses: list = []
        self.video_responses: list = []
        self.received = bytearray()
        self.sessions: list[str] = []
        self.last_metadata: dict | None = None

    @staticmethod
    def _next(queue: list):
        return queue.pop(0) if queue else None

    def initiate_resumable(self, access_token, metadata, file_size):
        self.calls.append(("initiate", dict(metadata), file_size))
        self.last_metadata = metadata
        response = self._next(self.initiate_responses)
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response
        session = f"https://upload.fake/session-{len(self.sessions) + 1}"
        self.sessions.append(session)
        return _resp(200, {"Location": session})

    def put_chunk(self, session_url, data, offset, total_size):
        self.calls.append(("put_chunk", session_url, offset, len(data)))
        self.received[offset:offset + len(data)] = data
        response = self._next(self.chunk_responses)
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response
        end = offset + len(data) - 1
        if end >= total_size - 1:
            return _resp(200, {"Range": f"bytes=0-{end}"},
                         json.dumps({"id": "vidFake12345678"}).encode("utf-8"))
        return _resp(308, {"Range": f"bytes=0-{end}"})

    def upload_status(self, session_url, total_size):
        self.calls.append(("status", session_url))
        response = self._next(self.status_responses)
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response
        if self.received and len(self.received) >= total_size:
            return _resp(200, {}, json.dumps({"id": "vidFake12345678"}).encode("utf-8"))
        if self.received:
            return _resp(308, {"Range": f"bytes=0-{len(self.received) - 1}"})
        return _resp(308, {})

    def get_video(self, access_token, video_id):
        self.calls.append(("get_video", video_id))
        response = self._next(self.video_responses)
        if isinstance(response, Exception):
            raise response
        if response is not None:
            return response
        meta = self.last_metadata or {}
        return _resp(200, {}, json.dumps({"items": [{
            "id": video_id,
            "snippet": dict(meta.get("snippet") or {}),
            "status": dict(meta.get("status") or {}),
        }]}).encode("utf-8"))


def _resp(status: int, headers: dict | None = None, body: bytes = b""):
    from ayce.publishing.transport import HttpResponse
    return HttpResponse(status=status, headers=dict(headers or {}), body=body)


def _write_research_artifact(tmp_path: Path) -> str:
    artifact = {
        "schema_version": "1.0",
        "research_id": RESEARCH_ID,
        "objective": OBJECTIVE,
        "niche": "AI productivity tools",
        "query": "AI productivity tools",
        "collected_at": "2026-09-20T09:03:18.260Z",
        "status": "PARTIAL",
        "tiers": ["OBSERVED_FACT", "DERIVED_METRIC", "MODEL_INFERENCE", "CREATIVE_HYPOTHESIS"],
        "candidates": [],
        "provenance": {"worker_version": "1.0",
                       "collected_at": "2026-09-20T09:03:18.260Z",
                       "request_digest": "ab" * 32},
    }
    store = tmp_path / "data" / "research"
    store.mkdir(parents=True, exist_ok=True)
    (store / f"{RESEARCH_ID}.json").write_text(json.dumps(artifact), encoding="utf-8")
    return objective_id_for(OBJECTIVE)


def _write_generated_script(tmp_path: Path) -> Path:
    script = {
        "production_id": RESEARCH_ID,
        "title": "Research brief: Identify outlier opening-hook formats",
        "scenes": [
            {"narration_text": "Here is an opening hook recorded by the research worker.",
             "visual_description": "High-contrast opening title card."},
            {"narration_text": "This video is built directly from research evidence.",
             "visual_description": "Clean text card naming the research objective."},
        ],
    }
    path = tmp_path / "script.json"
    path.write_text(json.dumps(script), encoding="utf-8")
    return path


@pytest.fixture()
def sealed_run(tmp_path):
    """One real research-derived run, sealed into a Stage 4 package."""
    _write_research_artifact(tmp_path)
    script_path = _write_generated_script(tmp_path)
    config = make_config(tmp_path)
    result = run_pipeline(script_path, config=config,
                          assets_dir=ASSETS_DIR, narration_dir=NARRATION_DIR)
    assert result.ok and result.qa_verdict == "PASS", result.error
    package = seal_publish_package(result.run_dir)
    ledger = PublishLedger(tmp_path / "publishing" / "ledger.sqlite3")
    return {"tmp_path": tmp_path, "config": config, "result": result,
            "package": package, "run_id": result.run_id, "ledger": ledger}


def publish(sealed_run, transport, tokens, request=None, **kwargs):
    request = request or PublishRequest()
    return publish_package_to_youtube(
        sealed_run["run_id"], request,
        config=sealed_run["config"], ledger=sealed_run["ledger"],
        token_provider=tokens, transport=transport,
        backoff_base=0.0, now_fn=lambda: NOW, **kwargs)


# ---- package gate: invalid packages NEVER contact YouTube (§4) ------------------


def test_publish_success_end_to_end(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    report = publish(sealed_run, transport, tokens)
    assert report["ok"] is True and report["status"] == "published"
    assert report["youtube_video_id"] == "vidFake12345678"
    assert report["privacy_status"] == "private"
    assert [c[0] for c in transport.calls] == ["initiate", "put_chunk", "get_video"]
    initiated = transport.calls[0][1]
    assert initiated["snippet"]["title"] == \
        "Research brief: Identify outlier opening-hook formats"
    assert initiated["status"]["privacyStatus"] == "private"
    record = sealed_run["ledger"].find(report["package_seal"], report["destination"])
    assert record.status == "published"
    assert record.youtube_video_id == "vidFake12345678"
    assert record.reconciled_at == NOW


def test_invalid_seal_never_contacts_youtube(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    package = dict(sealed_run["package"])
    package["production_id"] = "res-99990101T000000Z-tamper000001"
    (sealed_run["result"].run_dir / "publish_package.json").write_text(
        json.dumps(package), encoding="utf-8")
    with pytest.raises(PublishError) as excinfo:
        publish(sealed_run, transport, tokens)
    assert excinfo.value.code == "package_verification_failed"
    assert transport.calls == []                       # NO YouTube contact
    assert tokens.calls == 0                           # no OAuth either


def test_qa_failure_never_contacts_youtube(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    qa_path = sealed_run["result"].run_dir / "qa_report.json"
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    qa["verdict"] = "FAIL"
    qa_path.write_text(json.dumps(qa), encoding="utf-8")
    with pytest.raises(PublishError) as excinfo:
        publish(sealed_run, transport, tokens)
    assert excinfo.value.code == "package_verification_failed"
    assert transport.calls == []


def test_missing_render_never_contacts_youtube(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    refs = json.loads(
        (sealed_run["result"].run_dir / "artifacts.json").read_text(encoding="utf-8"))
    render = [a for a in refs if a["kind"] == "rendered_video"][0]
    (sealed_run["result"].run_dir / render["path"]).unlink()
    with pytest.raises(PublishError) as excinfo:
        publish(sealed_run, transport, tokens)
    assert excinfo.value.code == "package_verification_failed"
    assert transport.calls == []


def test_missing_package_never_auto_seals(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    (sealed_run["result"].run_dir / "publish_package.json").unlink()
    with pytest.raises(PublishError) as excinfo:
        publish(sealed_run, transport, tokens)
    assert excinfo.value.code == "publish_package_missing"
    assert transport.calls == []


# ---- metadata validation (§27) ----------------------------------------------------


def test_metadata_validation_rejects_bad_values():
    ok = {"title": "t", "description": "d", "privacy_status": "private"}
    validate_publish_metadata(ok)  # valid baseline
    with pytest.raises(PublishError):
        validate_publish_metadata({**ok, "title": ""})
    with pytest.raises(PublishError):
        validate_publish_metadata({**ok, "title": "x" * 101})
    with pytest.raises(PublishError):
        validate_publish_metadata({**ok, "privacy_status": "public-lol"})
    with pytest.raises(PublishError):
        validate_publish_metadata({**ok, "category_id": "not-numeric"})
    with pytest.raises(PublishError):
        validate_publish_metadata({**ok, "tags": ["x" * 101]})
    with pytest.raises(PublishError):
        validate_publish_metadata({**ok, "publish_at": "not-a-date"})
    with pytest.raises(PublishError):
        validate_publish_metadata({**ok, "privacy_status": "public",
                                   "publish_at": "2026-01-01T00:00:00+00:00"})


def make_publisher(sealed_run):
    transport = FakeYouTubeTransport()
    tokens = FakeTokenProvider()
    return transport, tokens


# ---- OAuth boundary (§9-§11) --------------------------------------------------------


def test_missing_credentials_yield_auth_configuration_error(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    tokens._error = PublishError(
        "auth_configuration", "YouTube OAuth credentials are not configured")
    with pytest.raises(PublishError) as excinfo:
        publish(sealed_run, transport, tokens)
    assert excinfo.value.code == "auth_configuration"
    assert transport.calls == []                       # no upload attempt
    record = sealed_run["ledger"].find(
        sealed_run["package"]["seal"]["value"], report_destination(tokens))
    assert record.status == "auth_failed"


def report_destination(tokens) -> str:
    from ayce.publishing import destination_for_client
    return destination_for_client(tokens.client_id)


def test_invalid_refresh_token_is_authentication_error(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    tokens._error = PublishError(
        "authentication_error", "token refresh was rejected")
    with pytest.raises(PublishError) as excinfo:
        publish(sealed_run, transport, tokens)
    assert excinfo.value.code == "authentication_error"
    assert transport.calls == []
    record = sealed_run["ledger"].find(
        sealed_run["package"]["seal"]["value"], report_destination(tokens))
    assert record.status == "auth_failed"


def test_token_file_cache_round_trip(tmp_path):
    token_file = tmp_path / "publishing" / "token.json"
    tokens = TokenProvider(client_id="c", client_secret="s",
                           refresh_token="r", token_file=token_file,
                           token_client=object())  # never called in this test
    # simulate a cached access token (e.g. written by a previous run)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(json.dumps({"access_token": "cached-token"}),
                          encoding="utf-8")
    assert tokens.get_access_token() == "cached-token"
    tokens.invalidate()
    # without a working token client a refresh would fail — cache was the
    # only source; this proves the file path is honored


# ---- idempotency: seal + destination is THE identity (§15/§16) ----------------------


def test_second_identical_publish_never_reuploads(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    first = publish(sealed_run, transport, tokens)
    assert first["ok"] is True and first["status"] == "published"
    calls_after_first = list(transport.calls)
    second = publish(sealed_run, transport, tokens)
    assert second["ok"] is True and second["status"] == "published"
    assert second["duplicate"] is True
    assert second["youtube_video_id"] == first["youtube_video_id"]
    assert transport.calls == calls_after_first        # NO second upload


def test_process_restart_discovers_prior_publication(sealed_run):
    """§33: the 'new process' is a fresh ledger object over the SAME
    SQLite file — the durable row prevents duplicate publication."""
    transport, tokens = make_publisher(sealed_run)
    first = publish(sealed_run, transport, tokens)
    fresh_ledger = PublishLedger(
        sealed_run["tmp_path"] / "publishing" / "ledger.sqlite3")
    transport2 = FakeYouTubeTransport()                # a 'new' process
    report = publish_package_to_youtube(
        sealed_run["run_id"], PublishRequest(),
        config=sealed_run["config"], ledger=fresh_ledger,
        token_provider=tokens, transport=transport2,
        backoff_base=0.0, now_fn=lambda: NOW)
    assert report["ok"] is True and report["duplicate"] is True
    assert report["youtube_video_id"] == first["youtube_video_id"]
    assert transport2.calls == []                      # zero external calls


def test_concurrent_identity_insert_is_protected(sealed_run):
    """§32: the SQLite UNIQUE constraint rejects a second identity row."""
    seal = sealed_run["package"]["seal"]["value"]
    destination = report_destination(FakeTokenProvider())
    sealed_run["ledger"].create(
        package_seal=seal, package_id=sealed_run["package"]["package_id"],
        run_id=sealed_run["run_id"], destination=destination,
        privacy_status="private", title="t", requested_metadata={}, now=NOW)
    with pytest.raises(sqlite3.IntegrityError):
        sealed_run["ledger"].create(
            package_seal=seal, package_id=sealed_run["package"]["package_id"],
            run_id=sealed_run["run_id"], destination=destination,
            privacy_status="private", title="t", requested_metadata={}, now=NOW)


# ---- resumable upload / interrupted recovery (§18/§20/§33) --------------------------


class _Boom(Exception):
    """A raw transport/network exception (e.g. connection reset)."""


def test_interrupted_mid_upload_is_unknown_not_failed(sealed_run):
    """Connection lost AFTER bytes were transmitted → reconciliation_pending
    (§20: unknown outcome is never a failed upload)."""
    transport, tokens = make_publisher(sealed_run)
    transport.chunk_responses = [None, _Boom("connection reset")]  # 308, then lose it
    report = publish(sealed_run, transport, tokens, chunk_bytes=1024)
    assert report["ok"] is False
    assert report["status"] == "reconciliation_pending"
    assert report["recoverable"] is True
    record = sealed_run["ledger"].find(
        sealed_run["package"]["seal"]["value"], report_destination(tokens))
    assert record.status == "reconciliation_pending"
    assert record.upload_session_url is not None       # durable session
    assert record.bytes_sent > 0


def test_unknown_outcome_reconciles_without_duplicate_upload(sealed_run):
    """Re-run after an unknown outcome: the durable session is queried; if
    the upload actually completed, the video is reconciled — NO re-upload."""
    transport, tokens = make_publisher(sealed_run)
    transport.chunk_responses = [None, _Boom("connection reset")]
    first = publish(sealed_run, transport, tokens, chunk_bytes=1024)
    assert first["status"] == "reconciliation_pending"
    calls_before = len(transport.calls)
    # re-run: status query discovers the upload COMPLETED on YouTube
    transport.status_responses = [
        _resp(200, {}, json.dumps({"id": "vidRecovered99"}).encode("utf-8"))]
    second = publish(sealed_run, transport, tokens)
    assert second["ok"] is True and second["status"] == "published"
    assert second["youtube_video_id"] == "vidRecovered99"
    new_calls = [c[0] for c in transport.calls[calls_before:]]
    assert "initiate" not in new_calls                 # NO duplicate upload
    assert new_calls == ["status", "get_video"]


# ---- API error normalization (§22) ----------------------------------------------------


def test_api_errors_are_normalized_and_permanent_ones_not_retried(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    transport.initiate_responses = [
        _resp(403, {}, json.dumps({"error": {"message": "forbidden",
             "errors": [{"reason": "forbidden"}]}}).encode("utf-8"))]
    with pytest.raises(PublishError) as excinfo:
        publish(sealed_run, transport, tokens)
    assert excinfo.value.code == "authorization_error"
    assert excinfo.value.details["http_status"] == 403
    record = sealed_run["ledger"].find(
        sealed_run["package"]["seal"]["value"], report_destination(tokens))
    assert record.status == "upload_failed"
    assert record.error_code == "authorization_error"


def test_http_status_categorization():
    assert categorize_http_status(401) == "authentication_error"
    assert categorize_http_status(
        403, '{"error":{"errors":[{"reason":"quotaExceeded"}]}}') == "quota_error"
    assert categorize_http_status(
        403, '{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}') == "rate_limited"
    assert categorize_http_status(400) == "invalid_request"
    assert categorize_http_status(404) == "not_found"
    assert categorize_http_status(503) == "server_error"
    assert categorize_http_status(429) == "rate_limited"


def test_transient_5xx_is_retried_with_bound(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    transport.initiate_responses = [
        _resp(503, {}, b"unavailable"), _resp(503, {}, b"unavailable"),
        None]                                           # 2 transient, then success
    report = publish(sealed_run, transport, tokens)
    assert report["ok"] is True and report["status"] == "published"
    initiates = [c for c in transport.calls if c[0] == "initiate"]
    assert len(initiates) == 3                          # bounded retries happened


# ---- reconciliation outcomes (§23/§24) ------------------------------------------------


def test_reconciliation_metadata_mismatch_fails(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    transport.video_responses = [
        _resp(200, {}, json.dumps({"items": [{
            "id": "vidFake12345678",
            "snippet": {"title": "WRONG TITLE", "description": ""},
            "status": {"privacyStatus": "private"},
        }]}).encode("utf-8"))]
    report = publish(sealed_run, transport, tokens)
    assert report["ok"] is False
    assert report["status"] == "reconciliation_failed"
    assert "title" in report["mismatches"]
    record = sealed_run["ledger"].find(
        sealed_run["package"]["seal"]["value"], report_destination(tokens))
    assert record.status == "reconciliation_failed"


def test_reconciliation_temporary_failure_is_pending(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    transport.video_responses = [
        _resp(503, {}, b"backend unavailable"),
        _resp(503, {}, b"backend unavailable"),
        _resp(503, {}, b"backend unavailable"),
        _resp(503, {}, b"backend unavailable")]  # outlast bounded retries
    report = publish(sealed_run, transport, tokens)
    assert report["ok"] is False
    assert report["status"] == "reconciliation_pending"
    assert report["youtube_video_id"] == "vidFake12345678"   # upload succeeded


# ---- dry run (§29): everything except ANY external contact ----------------------------


def test_dry_run_validates_without_contacting_youtube(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    report = publish(sealed_run, transport, tokens, dry_run=True)
    assert report["ok"] is True and report["dry_run"] is True
    assert report["would_upload"] is True
    assert report["metadata"]["privacy_status"] == "private"
    assert report["metadata"]["title"].startswith("Research brief:")
    assert transport.calls == []                        # NOTHING external
    assert tokens.calls == 0                            # no OAuth either
    # the ledger was not mutated by a dry-run
    assert sealed_run["ledger"].find(
        sealed_run["package"]["seal"]["value"], report["destination"]) is None


def test_dry_run_reports_already_published(sealed_run):
    transport, tokens = make_publisher(sealed_run)
    publish(sealed_run, transport, tokens)
    calls_before = list(transport.calls)
    report = publish(sealed_run, transport, tokens, dry_run=True)
    assert report["dry_run"] is True
    assert report["would_upload"] is False
    assert report["intended_action"].startswith("no-op")
    assert transport.calls == calls_before


def test_dry_run_works_without_any_credentials(sealed_run, monkeypatch):
    """§29/§10: dry-run must not require OAuth configuration at all."""
    monkeypatch.delenv("AYCE_YT_CLIENT_ID", raising=False)
    transport, tokens = make_publisher(sealed_run)
    report = publish(sealed_run, transport, tokens, dry_run=True)
    assert report["ok"] is True and report["dry_run"] is True
    assert transport.calls == []