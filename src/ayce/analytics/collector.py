"""Stage 6 â€” The analytics collector: published video â†’ observations.

OBSERVE â†’ STORE â†’ NORMALIZE â†’ TRACE (never DECIDE â€” Stage 7 owns that).

Flow, enforced in order:

    video-or-run reference
        â†“ publish-ledger resolution (Stage 5 is authoritative, Â§24)
        â†“ published/reconciled validation + package seal verification (Â§7)
        â†“ lineage reconstruction: video â†’ package â†’ run â†’ script
          â†’ research â†’ objective (Â§23; references only, no payload copies)
        â†“ metric/window request validation against the OFFICIAL
          per-source catalog (Â§5/Â§10)
        â†“ official API calls via the narrow transport (Â§28) with bounded
          retries of RETRYABLE categories only (Â§21)
        â†“ durable raw observation (deterministic identity, idempotent
          Â§12/Â§13/Â§22) â€” ALL requested sources must succeed before
          anything is persisted
        â†“ derived normalized measurements (raw values never overwritten, Â§9)

Hard boundaries:

- OBSERVATION ONLY: no policy, no recommendations, no scoring, no
  ranking, no learning (Â§4/Â§16/Â§36). Reports contain factual
  measurements and source metadata exclusively.
- ``dry_run=True`` resolves the publication, validates the lineage,
  builds and validates the request â€” but NEVER contacts an API, NEVER
  acquires credentials, and NEVER persists anything (Â§27).
- Production/publication evidence is never modified (Â§25): RunState,
  sealed packages, the research store and the publish ledger are
  read-only here.
- 0 / missing / not-returned / unavailable are DISTINCT availability
  states (Â§19); errors are never converted into fabricated values.
- Historical observations are never silently overwritten (Â§12); a new
  observation timestamp or window legitimately produces a new record.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone

from ..publish_package import (
    PackageError,
    load_publish_package,
    resolve_run_dir,
    verify_publish_package,
)
from ..publishing.errors import PublishError
from ..publishing.ledger import PublishLedger
from ..publishing.oauth import TokenProvider
from ..state import RunState, StateError
from .errors import (
    RETRYABLE_CATEGORIES,
    AnalyticsError,
    categorize_exception,
    categorize_http_status,
)
from .store import AnalyticsStore
from .transport import UrllibAnalyticsTransport

__all__ = [
    "SUPPORTED_SOURCES",
    "DEFAULT_METRICS",
    "METRIC_CATALOG",
    "collect_analytics",
    "show_analytics",
    "resolve_publication",
]

#: Video-id grammar: real YouTube ids are exactly 11 chars; the range
#: covers longer ids (e.g. test fakes). Run ids are longer still and the
#: resolution always falls back to the ledger-checked run path.
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11,32}$")

#: The official measurement sources of this stage, in collection order.
#: The project's API separation is preserved (Â§5): the Data API v3 gives
#: public cumulative counts; the Analytics API v2 gives private
#: windowed metrics. Reach metrics belong to the Reporting API and are
#: OUT OF SCOPE (documented limitation, never fabricated).
SUPPORTED_SOURCES = ("DATA_API_V3", "ANALYTICS_API_V2")

#: Metric catalog per official source: name â†’ response column, value
#: type and unit. ONLY metrics actually offered by each official API.
METRIC_CATALOG = {
    "DATA_API_V3": {
        "views": {"column": "viewCount", "value_type": "integer", "unit": "count"},
        "likes": {"column": "likeCount", "value_type": "integer", "unit": "count"},
        "comments": {"column": "commentCount", "value_type": "integer", "unit": "count"},
    },
    "ANALYTICS_API_V2": {
        "views": {"column": "views", "value_type": "integer", "unit": "count"},
        "estimatedMinutesWatched": {
            "column": "estimatedMinutesWatched",
            "value_type": "float", "unit": "minutes"},
        "averageViewDuration": {
            "column": "averageViewDuration",
            "value_type": "float", "unit": "seconds"},
        "averageViewPercentage": {
            "column": "averageViewPercentage",
            "value_type": "float", "unit": "percent"},
        "subscribersGained": {
            "column": "subscribersGained", "value_type": "integer", "unit": "count"},
        "subscribersLost": {
            "column": "subscribersLost", "value_type": "integer", "unit": "count"},
        "likes": {"column": "likes", "value_type": "integer", "unit": "count"},
        "comments": {"column": "comments", "value_type": "integer", "unit": "count"},
    },
}

#: Default requested metrics per source (the stage's supported set).
DEFAULT_METRICS = {
    "DATA_API_V3": ("views", "likes", "comments"),
    "ANALYTICS_API_V2": (
        "views", "estimatedMinutesWatched", "averageViewDuration",
        "averageViewPercentage", "subscribersGained", "subscribersLost",
    ),
}
#: Metrics that are NOT available from these two sources and are
#: therefore REFUSED before any network call (Â§10/Â§32) â€” including the
#: reach metrics the accepted research places exclusively in the
#: YouTube Reporting API (VF-01/VF-02), which Stage 6 does not implement.
UNAVAILABLE_METRICS = frozenset({
    # reach metrics â€” Reporting API bulk exports only (out of scope)
    "impressions", "videoThumbnailImpressions",
    "videoThumbnailImpressionsClickRate", "clickThroughRate", "ctr",
    "cardClickRate", "cardTeaserClickRate", "cardTeaserImpressions",
    "annotationClickThroughRate", "annotationImpressions",
    # monetary metrics â€” require the monetary scope / Reporting API
    "estimatedRevenue", "estimatedAdRevenue", "grossRevenue",
    "monetizedPlaybacks", "estimatedRedMinutesWatched",
})

#: The YouTube Analytics API interprets report DATES in Pacific time
#: (official semantics; recorded as explicit conversion metadata, Â§33).
ANALYTICS_WINDOW_TIMEZONE = "America/Los_Angeles"

#: The Data API statistics part has NO window: it is a cumulative
#: lifetime snapshot at ``observed_at`` (recorded explicitly, Â§11).
DATA_API_WINDOW_SEMANTICS = "lifetime_cumulative"

#: Scope of every metric this stage collects: the published VIDEO.
#: Channel-level populations are never mixed in (Â§18).
VIDEO_SCOPE = "video"


def _utc_now() -> str:
    """Project-standard timestamp: UTC ISO-8601, millisecond precision,
    ``Z`` suffix (same convention as the Stage 5 publisher)."""
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _canonical(payload) -> str:
    """Canonical JSON text: sorted keys, compact separators."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def observation_identity(*, source: str, youtube_video_id: str, scope: str,
                         metrics, window_start, window_end,
                         observed_at: str) -> str:
    """Deterministic observation identity (Â§13): source + video + scope +
    the exact metric/window request + the observation timestamp. The
    hash IS the deduplication mechanism â€” no random ids."""
    identity = {
        "source": source,
        "youtube_video_id": youtube_video_id,
        "scope": scope,
        "metrics": sorted(metrics),
        "window_start": window_start,
        "window_end": window_end,
        "observed_at": observed_at,
    }
    return "obs-" + hashlib.sha256(
        _canonical(identity).encode("utf-8")).hexdigest()[:24]

# ---- publication resolution + lineage reconstruction (Â§7/Â§23/Â§24) ------------


def _load_verified_package(run_dir) -> dict:
    """Load a run's sealed package and verify it (seal-only, READ-ONLY â€”
    the package is never modified, Â§25)."""
    try:
        package = load_publish_package(run_dir)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise AnalyticsError("analytics_lineage_invalid",
                             f"the sealed publish package is unreadable: {exc}") from exc
    if package is None:
        raise AnalyticsError(
            "analytics_lineage_invalid",
            "the run has no sealed publish package; publishing never auto-seals")
    try:
        verification = verify_publish_package(package)
    except PackageError as exc:
        raise AnalyticsError("analytics_lineage_invalid",
                             f"package verification failed: {exc.message}") from exc
    if not verification.get("ok"):
        raise AnalyticsError(
            "analytics_lineage_invalid",
            "the sealed package failed verification; lineage cannot be trusted",
            details={"errors": verification.get("errors", [])})
    return package


def _resolve_from_record(record, package, run_dir) -> dict:
    """Cross-check the ledger record against the verified sealed package
    and the run state, then reconstruct the lineage REFERENCES (Â§23 â€”
    ids only, the lineage payload is never duplicated)."""
    seal = (package.get("seal") or {}).get("value", "")
    if package.get("package_id") != record.package_id \
            or seal != record.package_seal \
            or package.get("run_id") != record.run_id:
        raise AnalyticsError(
            "analytics_lineage_invalid",
            "the ledger record and the sealed package disagree; "
            "lineage cannot be trusted (nothing is attributed)")

    # the run state itself must agree (production evidence is read-only, Â§25)
    try:
        state = RunState.load(run_dir / "state.json")
    except StateError as exc:
        raise AnalyticsError("analytics_lineage_invalid",
                             f"run state is unreadable: {exc}") from exc
    if state.run_id != record.run_id:
        raise AnalyticsError("analytics_lineage_invalid",
                             "run state does not match the ledger record")

    # only VERIFIED publications carry analytics (Â§7)
    if record.status != "published" or not record.youtube_video_id:
        raise AnalyticsError(
            "analytics_lineage_invalid",
            "the publication is not a verified published video"
            f" (ledger status: {record.status})",
            details={"status": record.status})

    lineage = package.get("lineage") or {}
    return {
        "record": record,
        "package": package,
        "run_dir": run_dir,
        "lineage": {
            "youtube_video_id": record.youtube_video_id,
            "package_id": record.package_id,
            "package_seal": record.package_seal,
            "run_id": record.run_id,
            "destination": record.destination,
            "script_id": lineage.get("script_id"),
            "research_id": lineage.get("research_id"),
            "objective_id": lineage.get("objective_id"),
        },
    }


def resolve_publication(config, ledger: PublishLedger, ref: str) -> dict:
    """Resolve a video id OR run id through the Stage 5 publish ledger to
    the verified published identity and its full production lineage.

    The ledger is AUTHORITATIVE for the publication (Â§24); the sealed
    package is re-verified (seal-only) and cross-checked against the
    ledger row and the run state so lineage cannot be caller-forged.
    Any inconsistency â†’ ``analytics_lineage_invalid`` and NOTHING is
    attributed (Â§7).
    """
    if not isinstance(ref, str) or not ref.strip():
        raise AnalyticsError("analytics_lineage_invalid",
                             "reference must be a video id or run id")
    ref = ref.strip()
    runs_root = config.resolved_data_dir / "runs"

    if VIDEO_ID_RE.fullmatch(ref):
        record = ledger.find_by_video(ref)
        if record is not None:
            run_dir = runs_root / record.run_id
            if not (run_dir / "state.json").is_file():
                raise AnalyticsError(
                    "analytics_lineage_invalid",
                    "the publication's originating run directory is missing",
                    details={"run_id": record.run_id})
            return _resolve_from_record(
                record, _load_verified_package(run_dir), run_dir)
        # not a known video id: fall through to run resolution ONLY when
        # an identically named run directory exists (still ledger-checked)
        if not (runs_root / ref / "state.json").is_file():
            raise AnalyticsError(
                "analytics_lineage_invalid",
                "no publication ledger entry for this video id; "
                "nothing is attributed",
                details={"ref": ref})
        run_dir = runs_root / ref
    else:
        try:
            run_dir = resolve_run_dir(config, ref)
        except PackageError as exc:
            raise AnalyticsError(
                "analytics_lineage_invalid",
                "unresolvable reference (must be a video id or a run id): "
                f"{exc.message}",
                details={"ref": ref}) from exc

    package = _load_verified_package(run_dir)
    # cross-check ledger â†” package (lineage cannot be caller-forged, Â§29)
    candidates = [r for r in ledger.find_by_run_id(package.get("run_id", ""))
                  if r.package_id == package.get("package_id", "")
                  and r.package_seal
                  == (package.get("seal") or {}).get("value", "")]
    if not candidates:
        raise AnalyticsError(
            "analytics_lineage_invalid",
            "no publication ledger entry matches this sealed package")
    video_ids = {r.youtube_video_id for r in candidates if r.youtube_video_id}
    if len(video_ids) != 1:
        raise AnalyticsError(
            "analytics_lineage_invalid",
            "ambiguous publication: multiple ledger entries with different "
            "videos match this package")
    return _resolve_from_record(candidates[0], package, run_dir)


# ---- request validation (Â§10/Â§11/Â§27) ----------------------------------------


def validate_metric_request(source: str, metrics) -> None:
    """Deterministic pre-flight metric validation. Unsupported metrics are
    refused BEFORE any network call with ``analytics_metric_unavailable``
    (Â§10/Â§21) â€” never silently dropped, never fabricated, never zeroed."""
    catalog = METRIC_CATALOG.get(source)
    if catalog is None:
        raise AnalyticsError("analytics_invalid_request",
                             f"unknown analytics source: {source}")
    requested = list(dict.fromkeys(metrics))  # dedupe, keep order
    if not requested:
        raise AnalyticsError("analytics_invalid_request",
                             f"no metrics requested from {source}")
    unsupported = [m for m in requested if m not in catalog]
    if unsupported:
        reach = [m for m in unsupported if m in UNAVAILABLE_METRICS]
        note = None
        if reach:
            note = ("this metric is not offered by the selected official "
                    "source; reach metrics (impressions/CTR) are available "
                    "exclusively via YouTube Reporting API bulk exports, "
                    "which Stage 6 does not implement")
        raise AnalyticsError(
            "analytics_metric_unavailable",
            f"unsupported metric(s) requested from {source}: "
            + ", ".join(unsupported),
            details={"unsupported": unsupported,
                     "source": source,
                     "reporting_api_only": reach or None,
                     "note": note})


def _validate_and_default_window(window_start, window_end, *, now_fn,
                                 analytics_source_requested: bool):
    """Validate/derive the Analytics API date window (Â§11/Â§33). Dates are
    plain YYYY-MM-DD calendar dates exactly as the official API takes
    them; the source's Pacific-time interpretation is recorded as
    metadata on the observation. Defaults (only when the Analytics
    source is requested): the 7 days ending yesterday UTC."""
    if window_start is None and window_end is None:
        if not analytics_source_requested:
            return None, None
        end_date = (now_fn_dt() - timedelta(days=1)).date()
        start_date = end_date - timedelta(days=6)
        return start_date.isoformat(), end_date.isoformat()
    if window_start is None or window_end is None:
        raise AnalyticsError(
            "analytics_invalid_request",
            "window_start and window_end must be supplied together")
    for name, value in (("window_start", window_start),
                        ("window_end", window_end)):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise AnalyticsError(
                "analytics_invalid_request",
                f"{name} must be a YYYY-MM-DD date (got {value!r})")
    if window_start > window_end:
        raise AnalyticsError("analytics_invalid_request",
                             "window_start must not be after window_end")
    return window_start, window_end


def now_fn_dt() -> datetime:
    """UTC wall-clock helper for window defaults (timezone-explicit)."""
    return datetime.now(timezone.utc)


# ---- transport call + classification (Â§20/Â§21) --------------------------------


def _call_with_retries(action: str, fn, *, max_retries: int, backoff_base: float):
    """Bounded deterministic retry for RETRYABLE analytics categories
    only (Â§21). Permanent categories raise immediately."""
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except AnalyticsError as exc:
            if exc.code not in RETRYABLE_CATEGORIES or attempt == max_retries:
                raise
            if backoff_base > 0:
                time.sleep(backoff_base * (2 ** attempt))
    raise AnalyticsError("analytics_unknown_error",
                         f"{action}: retries exhausted")  # pragma: no cover


def _fetch(source: str, transport, token_provider, params: dict, video_id: str):
    """One classified source call. Access tokens flow ONLY here â€” never
    into params, logs, or persisted records (Â§29)."""
    try:
        access_token = token_provider.get_access_token()
    except PublishError as exc:
        raise AnalyticsError(
            "analytics_authentication_error",
            "analytics could not authenticate (credentials missing or "
            "token refresh failed); NO request was made",
            details={"publishing_code": exc.code}) from exc
    except Exception as exc:  # classified, truthful
        raise AnalyticsError(categorize_exception(exc),
                             f"access token refresh failed: {exc}") from exc
    try:
        if source == "ANALYTICS_API_V2":
            return transport.query_report(access_token, params)
        return transport.get_video_statistics(access_token, video_id)
    except AnalyticsError:
        raise
    except Exception as exc:  # classified, truthful
        raise AnalyticsError(categorize_exception(exc),
                             f"{source} transport failure: {exc}") from exc


def _raise_for_status(source: str, response) -> None:
    if response.status != 200:
        category = categorize_http_status(
            response.status, response.body.decode("utf-8", "replace"))
        raise AnalyticsError(category, f"{source} request failed",
                             details={"http_status": response.status})


def _fetch_and_classify(source: str, transport, token_provider, params: dict,
                        video_id: str):
    """One classified source call INCLUDING the HTTP status check, so
    classified API errors (e.g. rate limits) participate in the bounded
    retry of RETRYABLE categories (§21)."""
    response = _fetch(source, transport, token_provider, params, video_id)
    _raise_for_status(source, response)
    return response


# ---- response parsing â†’ normalized measurements (Â§9/Â§19) -----------------------


def _parse_value(raw, value_type: str):
    """Parse a source value to (value, note). NEVER invents a number:
    anything unparseable stays missing (Â§19/Â§30)."""
    if raw is None:
        return None, None
    try:
        if value_type == "integer":
            if isinstance(raw, bool):
                return None, "non-numeric source value"
            if isinstance(raw, int):
                return float(raw), None
            return float(int(str(raw).strip())), None
        if isinstance(raw, (int, float)):
            return float(raw), None
        return float(str(raw).strip()), None
    except (ValueError, TypeError):
        return None, "non-numeric source value"


def _measurement(metric_name: str, raw, spec: dict, note: str | None = None) -> dict:
    """One normalized measurement: stable semantics + explicit
    availability. 0 â†’ ``zero``; absent/unparseable â†’ ``missing``
    (distinct states, Â§19). Normalization is type/unit annotation only
    â€” ``identity`` â€” so raw source values are never transformed away."""
    value, parse_note = _parse_value(raw, spec["value_type"])
    if value is None:
        availability = "missing"
    elif value == 0:
        availability = "zero"
    else:
        availability = "present"
    notes = [n for n in (note, parse_note) if n]
    return {
        "metric_name": metric_name,
        "raw_value": raw,
        "value": value,
        "value_type": spec["value_type"],
        "unit": spec["unit"],
        "availability": availability,
        "normalization_method": "identity",
        "baseline_reference": None,
        "note": "; ".join(notes) if notes else None,
    }


def _parse_response(source: str, response, metrics) -> list[dict]:
    """Parse ONE successful (200) source response into normalized
    measurements. Malformed payloads are truthful ``unknown_error``
    failures â€” never silently accepted (Â§30)."""
    body = response.json()
    if source == "ANALYTICS_API_V2":
        if not isinstance(body, dict) or not isinstance(body.get("columnHeaders"), list):
            raise AnalyticsError(
                "analytics_unknown_error",
                "malformed Analytics API response (no columnHeaders)",
                details={"http_status": response.status})
        headers = [h.get("name") for h in body["columnHeaders"] if isinstance(h, dict)]
        rows = body.get("rows")
        if rows is None:
            rows = []
        if not isinstance(rows, list) or any(not isinstance(r, list) for r in rows):
            raise AnalyticsError(
                "analytics_unknown_error",
                "malformed Analytics API response (rows)",
                details={"http_status": response.status})
        if not rows:
            # the source legitimately returned no data for this window
            # (e.g. processing latency) â€” record MISSING, never zero (Â§19/Â§32)
            return [_measurement(m, None, METRIC_CATALOG[source][m],
                                 note="the source returned no rows for this window")
                    for m in metrics]
        row = rows[0]  # TOTAL row: no dimensions are requested (Â§18)
        out = []
        for metric in metrics:
            spec = METRIC_CATALOG[source][metric]
            if spec["column"] in headers:
                idx = headers.index(spec["column"])
                raw = row[idx] if idx < len(row) else None
                out.append(_measurement(metric, raw, spec))
            else:
                out.append(_measurement(metric, None, spec,
                                        note="column absent from the response"))
        return out

    # DATA_API_V3
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise AnalyticsError(
            "analytics_unknown_error",
            "malformed Data API response (no items list)",
            details={"http_status": response.status})
    items = body["items"]
    if not items:
        raise AnalyticsError(
            "analytics_not_found",
            "the Data API returned no video for the resolved video id",
            details={"http_status": response.status})
    statistics = items[0].get("statistics") if isinstance(items[0], dict) else None
    if not isinstance(statistics, dict):
        raise AnalyticsError(
            "analytics_unknown_error",
            "malformed Data API response (no statistics part)",
            details={"http_status": response.status})
    out = []
    for metric in metrics:
        spec = METRIC_CATALOG[source][metric]
        raw = statistics.get(spec["column"])
        note = None if spec["column"] in statistics else \
            "field absent from the response"
        out.append(_measurement(metric, raw, spec, note=note))
    return out
# ---- per-source collection + persistence (§12/§13/§22) -------------------------


def _persist_observation(*, pub, source, metrics, window, observed_at,
                         raw_response, measurements, store, now_fn):
    """Persist one raw observation + its derived measurements. The
    deterministic identity makes repetition idempotent (§22): a duplicate
    inserts NOTHING new and the already-stored raw record is returned
    untouched (§12)."""
    video_id = pub["lineage"]["youtube_video_id"]
    if source == "ANALYTICS_API_V2":
        params = {
            "ids": "channel==MINE",
            "startDate": window["start"],
            "endDate": window["end"],
            "metrics": ",".join(metrics),
            "filters": f"video=={video_id}",
        }
        request_meta = {
            "api": "YouTube Analytics API v2",
            "endpoint": "reports.query",
            "params": params,
        }
        window_start, window_end = window["start"], window["end"]
        window_timezone = ANALYTICS_WINDOW_TIMEZONE
    else:
        request_meta = {
            "api": "YouTube Data API v3",
            "endpoint": "videos.list",
            "part": "statistics",
            "id": video_id,
        }
        window_start = window_end = None
        window_timezone = None

    observation_id = observation_identity(
        source=source, youtube_video_id=video_id, scope=VIDEO_SCOPE,
        metrics=metrics, window_start=window_start, window_end=window_end,
        observed_at=observed_at)

    inserted = store.insert_observation(
        observation_id=observation_id, source=source,
        youtube_video_id=video_id, scope=VIDEO_SCOPE,
        window_start=window_start, window_end=window_end,
        window_timezone=window_timezone, observed_at=observed_at,
        metrics_requested=list(metrics), source_request=request_meta,
        raw_response=raw_response, lineage=pub["lineage"],
        collected_at=now_fn())
    duplicate = not inserted
    store.insert_measurements(
        observation_id=observation_id, source=source,
        youtube_video_id=video_id, scope=VIDEO_SCOPE,
        window_start=window_start, window_end=window_end,
        observed_at=observed_at, lineage=pub["lineage"],
        measurements=measurements, normalized_at=now_fn())
    return {
        "observation_id": observation_id,
        "source": source,
        "scope": VIDEO_SCOPE,
        "window_start": window_start,
        "window_end": window_end,
        "window_timezone": window_timezone,
        "observed_at": observed_at,
        "duplicate": duplicate,
        "metrics": measurements,
    }


def collect_analytics(
    ref: str,
    *,
    config,
    ledger: PublishLedger,
    token_provider=None,
    transport=None,
    store: AnalyticsStore | None = None,
    sources=None,
    metrics=None,
    window_start: str | None = None,
    window_end: str | None = None,
    dry_run: bool = False,
    max_retries: int = 2,
    backoff_base: float = 1.0,
    now_fn=None,
) -> dict:
    """Collect ONE observation set for a published video (§22).

    Every requested source must respond successfully before ANYTHING is
    persisted (all-or-nothing per collect call). Returns a structured,
    factual report — NEVER policy, recommendations, or rankings (§16).
    Raises :class:`AnalyticsError` for classified failures.

    ``dry_run=True`` performs resolution, lineage validation and
    request construction only — no credentials, no API contact, no
    persistence (§27).
    """
    now_fn = now_fn or _utc_now
    pub = resolve_publication(config, ledger, ref)

    sources = tuple(sources) if sources else SUPPORTED_SOURCES
    if not sources:
        raise AnalyticsError("analytics_invalid_request",
                             "no analytics sources requested")
    for source in sources:
        if source not in SUPPORTED_SOURCES:
            raise AnalyticsError(
                "analytics_invalid_request",
                f"unknown analytics source: {source}",
                details={"supported": list(SUPPORTED_SOURCES)})

    requested: dict[str, tuple] = {}
    for source in sources:
        req = tuple(metrics) if metrics else DEFAULT_METRICS[source]
        validate_metric_request(source, req)
        requested[source] = req

    if (window_start or window_end) and "ANALYTICS_API_V2" not in sources:
        raise AnalyticsError(
            "analytics_invalid_request",
            "windows apply only to ANALYTICS_API_V2; the Data API v3 "
            "statistics part is a cumulative lifetime snapshot")
    win_start, win_end = _validate_and_default_window(
        window_start, window_end, now_fn=now_fn,
        analytics_source_requested="ANALYTICS_API_V2" in sources)
    window = {"start": win_start, "end": win_end}


    identity = pub["lineage"]
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "youtube_video_id": identity["youtube_video_id"],
            "package_id": identity["package_id"],
            "package_seal": identity["package_seal"],
            "run_id": identity["run_id"],
            "lineage": identity,
            "would_collect": [
                {
                    "source": source,
                    "metrics": list(requested[source]),
                    "window_start": (window["start"]
                                     if source == "ANALYTICS_API_V2" else None),
                    "window_end": (window["end"]
                                   if source == "ANALYTICS_API_V2" else None),
                    "window_timezone": (ANALYTICS_WINDOW_TIMEZONE
                                        if source == "ANALYTICS_API_V2" else None),
                    "window_semantics": (None if source == "ANALYTICS_API_V2"
                                         else DATA_API_WINDOW_SEMANTICS),
                }
                for source in sources
            ],
            "message": ("dry run: publication resolved, lineage verified and "
                        "request validated; NO API call was made and NOTHING "
                        "was stored"),
        }

    if token_provider is None:
        try:
            token_provider = TokenProvider.from_config(config)
        except PublishError as exc:
            raise AnalyticsError(
                "analytics_authentication_error",
                "analytics credentials are not configured; NO request "
                "was made (set AYCE_YT_CLIENT_ID / AYCE_YT_CLIENT_SECRET / "
                "AYCE_YT_REFRESH_TOKEN)",
                details={"publishing_code": exc.code}) from exc
    if transport is None:
        transport = UrllibAnalyticsTransport()
    if store is None:
        store = AnalyticsStore(
            config.resolved_data_dir / "analytics" / "analytics.sqlite3")

    # fetch + parse EVERY requested source first (all-or-nothing, §22)
    fetched = []
    for source in sources:
        observed_at = now_fn()
        params = {
            "ids": "channel==MINE",
            "startDate": window["start"],
            "endDate": window["end"],
            "metrics": ",".join(requested[source]),
            "filters": f"video=={identity['youtube_video_id']}",
        } if source == "ANALYTICS_API_V2" else {}
        response = _call_with_retries(
            source,
            lambda s=source, p=params: _fetch_and_classify(
                s, transport, token_provider, p,
                identity["youtube_video_id"]),
            max_retries=max_retries, backoff_base=backoff_base)
        parsed = _parse_response(source, response, requested[source])
        fetched.append((source, observed_at, response, parsed))

    # everything succeeded → persist (idempotently)
    observations = []
    for source, observed_at, response, parsed in fetched:
        raw_response = response.body.decode("utf-8", "replace")
        observations.append(_persist_observation(
            pub=pub, source=source, metrics=requested[source], window=window,
            observed_at=observed_at, raw_response=raw_response,
            measurements=parsed, store=store, now_fn=now_fn))

    return {
        "ok": True,
        "youtube_video_id": identity["youtube_video_id"],
        "package_id": identity["package_id"],
        "package_seal": identity["package_seal"],
        "run_id": identity["run_id"],
        "destination": identity["destination"],
        "lineage": identity,
        "observations": observations,
    }


def show_analytics(ref: str, *, config, ledger: PublishLedger,
                   store: AnalyticsStore | None = None) -> dict:
    """Factual read-back of the stored observations + normalized
    measurements for one published video (observation ONLY — §16)."""
    pub = resolve_publication(config, ledger, ref)
    if store is None:
        store = AnalyticsStore(
            config.resolved_data_dir / "analytics" / "analytics.sqlite3")
    video_id = pub["lineage"]["youtube_video_id"]
    return {
        "ok": True,
        "youtube_video_id": video_id,
        "package_id": pub["lineage"]["package_id"],
        "package_seal": pub["lineage"]["package_seal"],
        "run_id": pub["lineage"]["run_id"],
        "lineage": pub["lineage"],
        "observations": store.list_observations(video_id),
        "measurements": store.measurements_for_video(video_id),
    }
