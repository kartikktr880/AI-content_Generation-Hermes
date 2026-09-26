"""Stage 5 — The publisher: sealed package → verified YouTube publication.

The orchestrator of the publishing boundary. The flow is FIXED and every
step is enforced in code (§4)::

    PublishPackage (must ALREADY exist — never auto-sealed, §36)
        ↓ verify_publish_package()  (FULL content verification)
        ↓ publish metadata validation (§27)
        ↓ ledger idempotency lookup (package_seal + destination, §15)
        ↓ OAuth access token (user/channel OAuth, §9)
        ↓ resumable upload (official YouTube Data API v3, §18)
        ↓ reconciliation (official videos.list, §23)
        ↓ durable ledger state (SQLite, worker-internal, §12)

Hard rules enforced here:

- Any verification/validation failure returns BEFORE any external
  contact — YouTube is never touched by an unverified package.
- ``dry_run=True`` performs everything except ANY external call (the
  transport is never invoked, tokens are never refreshed).
- An interrupted/ambiguous upload is NEVER assumed failed: the resumable
  session and byte offset are durable, unknown outcomes become
  ``reconciliation_pending`` and are reconciled (§20).
- The same (package seal, destination) can only ever upload ONCE (§16).
- The sealed package is never modified (§26); the YouTube video id lives
  only in the ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..publish_package import verify_publish_package
from .errors import (
    RETRYABLE_CATEGORIES,
    PublishError,
    categorize_exception,
    categorize_http_status,
)
from .ledger import PublishLedger, PublishRecord
from .oauth import TokenProvider
from .transport import YouTubeTransport

__all__ = [
    "VALID_PRIVACY_STATES",
    "PUBLISH_METADATA_LIMITS",
    "PublishRequest",
    "resolve_package",
    "build_publish_metadata",
    "validate_publish_metadata",
    "destination_for_client",
    "publish_package_to_youtube",
]

VALID_PRIVACY_STATES = ("private", "unlisted", "public")

#: Explicit metadata limits (YouTube Data API v3; §27 — never truncated).
PUBLISH_METADATA_LIMITS = {
    "title_max": 100,
    "description_max": 5000,
    "tag_max": 100,
    "tags_max": 30,
}

_DEFAULT_CHUNK_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class PublishRequest:
    """The EXPLICIT publish intent (§7). Everything is optional and
    resolved from authoritative existing metadata where absent."""

    title: str | None = None
    description: str | None = None
    tags: tuple[str, ...] | list[str] | None = None
    category_id: str | None = None
    privacy_status: str = "private"          # NEVER auto-public (§8)
    publish_at: str | None = None            # ISO-8601; requires private


@dataclass(frozen=True)
class ResolvedPackage:
    package: dict
    run_dir: Path
    package_path: Path


def resolve_package(config, ref: str) -> ResolvedPackage:
    """Resolve a run id OR a package path to the sealed Stage 4 package.

    Containment: any path must resolve inside the configured data root;
    run ids use the grammar-checked runs-root resolution. A missing
    package is ``publish_package_missing`` — publishing NEVER auto-seals
    (§35/§36).
    """
    data_root = config.resolved_data_dir.resolve()
    runs_root = data_root / "runs"
    package_path: Path | None = None
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", ref):
        candidate = runs_root / ref / "publish_package.json"
        if candidate.is_file():
            package_path = candidate
    if package_path is None:
        candidate = Path(ref)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.resolve()
        if data_root != candidate and data_root not in candidate.parents:
            raise PublishError(
                "publish_package_missing",
                "package path escapes the configured data root",
                details={"ref": ref},
            )
        package_path = candidate
    if not package_path.is_file():
        raise PublishError(
            "publish_package_missing",
            "no sealed publish package found; publishing never auto-seals — "
            "run `ayce package <run_id>` first",
            details={"ref": ref},
        )
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PublishError(
            "publish_package_missing", f"package file unreadable: {exc}") from exc
    run_id = package.get("run_id") if isinstance(package, dict) else None
    if not isinstance(run_id, str) or not run_id:
        raise PublishError("publish_package_missing", "package has no run_id")
    run_dir = runs_root / run_id
    if not (run_dir / "state.json").is_file():
        raise PublishError(
            "publish_package_missing",
            "the package's originating run directory is missing",
            details={"run_id": run_id},
        )
    return ResolvedPackage(package=package, run_dir=run_dir,
                           package_path=package_path)


def build_publish_metadata(package: dict, request: PublishRequest,
                           run_dir: Path) -> dict:
    """Resolve the FINAL publish metadata: explicit request values win;
    defaults come from AUTHORITATIVE existing artifacts (the scene
    manifest's title; a deterministic lineage-based description). No
    invention, no silent truncation — validation rejects over-limit."""
    title = request.title
    if title is None:
        manifest_path = run_dir / "scene_manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                title = manifest.get("title")
            except (json.JSONDecodeError, OSError):
                title = None
    description = request.description
    if description is None:
        lineage = package.get("lineage") or {}
        description = (
            f"AYCE autonomous production {package.get('production_id')}. "
            f"Objective: {lineage.get('objective_text', '')} "
            f"(research {lineage.get('research_id', '')})."
        )
    metadata = {
        "title": title,
        "description": description,
        "tags": list(request.tags) if request.tags else None,
        "category_id": request.category_id,
        "privacy_status": request.privacy_status,
        "publish_at": request.publish_at,
    }
    return {k: v for k, v in metadata.items() if v is not None}


def validate_publish_metadata(metadata: dict) -> None:
    """Deterministic pre-flight validation (§27). Raises PublishError
    (``invalid_metadata``) — nothing is truncated or normalized silently."""
    limits = PUBLISH_METADATA_LIMITS
    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip():
        raise PublishError("invalid_metadata", "title is required and must be non-empty")
    if len(title) > limits["title_max"]:
        raise PublishError(
            "invalid_metadata",
            f"title exceeds the YouTube limit of {limits['title_max']} characters",
            details={"length": len(title)},
        )
    description = metadata.get("description")
    if not isinstance(description, str):
        raise PublishError("invalid_metadata", "description must be a string")
    if len(description) > limits["description_max"]:
        raise PublishError(
            "invalid_metadata",
            f"description exceeds {limits['description_max']} characters",
            details={"length": len(description)},
        )
    tags = metadata.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or len(tags) > limits["tags_max"] or any(
            not isinstance(t, str) or not t.strip() or len(t) > limits["tag_max"]
            for t in tags
        ):
            raise PublishError(
                "invalid_metadata",
                f"tags must be at most {limits['tags_max']} non-empty strings "
                f"of at most {limits['tag_max']} characters",
            )
    category_id = metadata.get("category_id")
    if category_id is not None and (
        not isinstance(category_id, str) or not category_id.isdigit()
    ):
        raise PublishError("invalid_metadata",
                           "category_id must be a numeric YouTube category id")
    privacy = metadata.get("privacy_status")
    if privacy not in VALID_PRIVACY_STATES:
        raise PublishError(
            "invalid_metadata",
            f"privacy_status must be one of {list(VALID_PRIVACY_STATES)}",
            details={"found": privacy},
        )
    publish_at = metadata.get("publish_at")
    if publish_at is not None:
        if privacy != "private":
            raise PublishError(
                "invalid_metadata",
                "publish_at requires privacy_status='private' (YouTube rule)",
            )
        try:
            datetime.fromisoformat(str(publish_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise PublishError(
                "invalid_metadata",
                "publish_at must be an ISO-8601 timestamp",
                details={"found": publish_at},
            ) from exc


def destination_for_client(client_id: str) -> str:
    """The publish destination identity: a NON-SENSITIVE deterministic
    digest of the OAuth client id (never the raw id, never a secret)."""
    return "yt-" + hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]


def _destination_identity(token_provider, config) -> str:
    """Dry-runs must work WITHOUT credentials (§29): fall back to the
    configured client id (or a stable placeholder) instead of failing."""
    import os

    if token_provider is not None:
        return destination_for_client(token_provider.client_id)
    client_id = os.environ.get("AYCE_YT_CLIENT_ID", "").strip()
    return destination_for_client(client_id or "unconfigured")


def _classify_transport_response(response, action: str) -> PublishError:
    """Normalize a non-success transport response into a classified error
    (§22) — the raw HTTP response is diagnostic detail only."""
    category = categorize_http_status(
        response.status, response.body.decode("utf-8", "replace"))
    body = response.json()
    message = "YouTube API error"
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or message)
    return PublishError(category, f"{action}: {message}",
                        details={"http_status": response.status})


def _with_retries(action: str, fn, *, max_retries: int, backoff_base: float):
    """Bounded deterministic retry for RETRYABLE categories only (§21).
    Permanent categories raise immediately."""
    last_error: PublishError | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except PublishError as exc:
            if exc.code not in RETRYABLE_CATEGORIES or attempt == max_retries:
                raise
            last_error = exc
            if backoff_base > 0:
                time.sleep(backoff_base * (2 ** attempt))
    raise last_error  # pragma: no cover — defensive


def _claim_lost(ledger: PublishLedger, package_seal: str, destination: str,
                exc: Exception) -> PublishRecord:
    """Another process won the identity INSERT (§32). Re-read its row; if
    it vanished (should not happen), fail safely."""
    record = ledger.find(package_seal, destination)
    if record is None:
        raise PublishError(
            "unknown_error",
            "concurrent publish identity claim failed and no record exists",
            details={"cause": str(exc)},
        ) from exc
    return record


def publish_package_to_youtube(
    package_ref: str,
    request: PublishRequest,
    *,
    config,
    ledger: PublishLedger,
    token_provider: TokenProvider | None = None,
    transport: YouTubeTransport | None = None,
    dry_run: bool = False,
    chunk_bytes: int = _DEFAULT_CHUNK_BYTES,
    max_retries: int = 3,
    backoff_base: float = 1.0,
    now_fn=None,
) -> dict:
    """Publish ONE sealed, verified package to the authenticated channel.

    Enforces the full boundary in order; returns a structured result.
    Raises :class:`PublishError` for classified failures (the ledger
    records them truthfully). ``dry_run=True`` NEVER contacts YouTube.
    """
    now_fn = now_fn or (lambda: datetime.now(timezone.utc)
                        .isoformat(timespec="milliseconds").replace("+00:00", "Z"))

    def now() -> str:
        return now_fn()

    # ---- 1) resolve the SEALED package (never auto-seal) --------------------
    resolved = resolve_package(config, package_ref)
    package = resolved.package
    run_dir = resolved.run_dir
    package_seal = (package.get("seal") or {}).get("value", "")
    package_id = package.get("package_id", "")

    # ---- 2) FULL Stage 4 verification BEFORE any external contact -----------
    verification = verify_publish_package(package, run_dir=run_dir)
    if not verification["ok"]:
        raise PublishError(
            "package_verification_failed",
            "the sealed package failed Stage 4 verification; YouTube was "
            "NOT contacted",
            details={"errors": verification["errors"]},
        )

    # ---- 3) publish metadata: resolve + validate (§27) -----------------------
    metadata = build_publish_metadata(package, request, run_dir)
    validate_publish_metadata(metadata)

    # ---- 4) destination + idempotency lookup (seal + destination, §15) -------
    destination = _destination_identity(token_provider, config)
    existing = ledger.find(package_seal, destination)

    # ---- 5) DRY RUN: everything above, NOTHING external (§29) ----------------
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "package_id": package_id,
            "package_seal": package_seal,
            "run_id": package["run_id"], "destination": destination,
            "production_id": package.get("production_id"),
            "destination": destination,
            "metadata": metadata,
            "render": package.get("render"),
            "privacy_status": metadata.get("privacy_status"),
            "ledger_state": existing.to_dict() if existing else None,
            "would_upload": not (existing and existing.status == "published"),
            "intended_action": (
                "no-op (already published to this destination)"
                if existing and existing.status == "published"
                else "initiate resumable upload to YouTube Data API v3"
            ),
        }

    # ---- 6) idempotency: never upload the same successful package twice -----
    if existing is not None and existing.status == "published":
        return {
            "ok": True,
            "status": "published",
            "duplicate": True,
            "package_id": package_id,
            "package_seal": package_seal,
            "run_id": package["run_id"], "destination": destination,
            "youtube_video_id": existing.youtube_video_id,
            "privacy_status": existing.privacy_status,
            "attempt_count": existing.attempt_count,
            "message": "this exact package was already published to this "
                       "destination; NO second upload was made",
        }

    # ---- 7) create/claim the ledger identity (SQLite UNIQUE = §32) ----------
    if existing is None:
        try:
            record = ledger.create(
                package_seal=package_seal, package_id=package_id,
                run_id=package["run_id"], destination=destination,
                privacy_status=metadata.get("privacy_status", "private"),
                title=metadata.get("title", ""),
                requested_metadata=metadata, now=now(),
            )
        except Exception as exc:  # concurrent INSERT lost the race (§32)
            record = _claim_lost(ledger, package_seal, destination, exc)
    else:
        record = existing

    if record.status == "published":
        # a concurrent process finished while we validated (§32)
        return {
            "ok": True, "status": "published", "duplicate": True,
            "package_id": package_id, "package_seal": package_seal,
            "run_id": package["run_id"], "destination": destination,
            "youtube_video_id": record.youtube_video_id,
            "privacy_status": record.privacy_status,
            "attempt_count": record.attempt_count,
        }

    if record.status not in ("upload_in_progress", "reconciliation_pending",
                             "uploaded", "reconciling"):
        # advance the machine WITHOUT destroying durable resumable state
        # (an interrupted upload must keep its session for recovery, §33)
        record = ledger.update(
            package_seal, destination, status="validated", now=now())

    return _execute_upload(
        package=package, metadata=metadata, record=record,
        run_dir=run_dir, ledger=ledger, token_provider=token_provider,
        transport=transport, destination=destination,
        chunk_bytes=chunk_bytes, max_retries=max_retries,
        backoff_base=backoff_base, now=now,
    )


def _execute_upload(*, package: dict, metadata: dict, record: PublishRecord,
                    run_dir: Path, ledger: PublishLedger,
                    token_provider: TokenProvider | None,
                    transport: YouTubeTransport | None, destination: str,
                    chunk_bytes: int, max_retries: int, backoff_base: float,
                    now) -> dict:
    """Auth → session resume/initiate → chunked transfer → reconciliation."""
    seal = record.package_seal
    dest = record.destination
    render_path = run_dir / package["render"]["path"]
    video_data = render_path.read_bytes()
    total = len(video_data)

    # ---- auth (§9/§10): failure is truthful and terminal for this attempt --
    try:
        access_token = token_provider.get_access_token()
    except PublishError as exc:
        ledger.update(seal, dest, status="auth_failed", now=now(),
                      error_code=exc.code, error_message=exc.message)
        raise
    ledger.update(seal, dest, status="auth_ready", now=now(), clear_error=True)

    youtube_video_id = record.youtube_video_id
    completed_via_session_query = False

    # ---- resume an existing session (§18/§33): never blind re-upload --------
    if record.upload_session_url and record.status in (
            "upload_in_progress", "reconciliation_pending"):
        session = record.upload_session_url

        def _status():
            response = transport.upload_status(session, total)
            if response.status not in (200, 308) and response.status in (404, 410):
                return response
            if response.status in (200, 308):
                return response
            raise _classify_transport_response(response, "upload_status")

        try:
            response = _with_retries("upload_status", _status,
                                     max_retries=max_retries,
                                     backoff_base=backoff_base)
        except PublishError as exc:
            ledger.update(seal, dest, status="upload_failed", now=now(),
                          error_code=exc.code, error_message=exc.message)
            raise
        if response.status == 200:
            body = response.json() or {}
            youtube_video_id = body.get("id")
            completed_via_session_query = True   # the session query proves it finished
            ledger.update(seal, dest, status="uploaded", now=now(),
                          youtube_video_id=youtube_video_id, bytes_sent=total)
        elif response.status == 308:
            received = (response.range_end or -1) + 1
            ledger.update(seal, dest, status="upload_in_progress", now=now(),
                          bytes_sent=received)
        else:  # 404/410: session gone — the resumable protocol guarantees
            # the upload did NOT complete; clear and start fresh (bounded).
            ledger.update(seal, dest, clear_session=True, now=now(),
                          error_code="upload_session_error",
                          error_message="resumable session expired; a new "
                                        "session will be initiated")

    if completed_via_session_query:
        # the session query already proved the upload finished — go straight
        # to reconciliation; NEVER re-transfer (§20/§16)
        return _reconcile(
            package=package, seal=seal, destination=dest, metadata=metadata,
            youtube_video_id=youtube_video_id, ledger=ledger,
            token_provider=token_provider, transport=transport,
            max_retries=max_retries, backoff_base=backoff_base, now=now,
        )

    record = ledger.find(seal, dest)  # refresh after resume handling

    # ---- initiate a session if none is usable --------------------------------
    if youtube_video_id is None and not (
            record.upload_session_url and record.status == "upload_in_progress"):
        api_metadata = _api_metadata(metadata)

        def _initiate():
            response = transport.initiate_resumable(access_token, api_metadata, total)
            if response.status != 200 or not response.location:
                raise _classify_transport_response(response, "initiate_resumable")
            return response.location

        try:
            session_url = _with_retries("initiate_resumable", _initiate,
                                        max_retries=max_retries,
                                        backoff_base=backoff_base)
        except PublishError as exc:
            ledger.update(seal, dest, status="upload_failed", now=now(),
                          error_code=exc.code, error_message=exc.message)
            raise
        ledger.update(seal, dest, status="upload_in_progress", now=now(),
                      upload_session_url=session_url, bytes_sent=0,
                      clear_error=True)
        offset = 0
    else:
        session_url = record.upload_session_url
        offset = record.bytes_sent if record.status == "upload_in_progress" else 0
        offset = min(offset, total)

    outcome, youtube_video_id = _transfer_chunks(
        transport=transport, session_url=session_url, video_data=video_data,
        offset=offset, total=total, ledger=ledger, seal=seal, dest=dest,
        chunk_bytes=chunk_bytes, now=now,
        max_retries=max_retries, backoff_base=backoff_base,
        youtube_video_id=youtube_video_id,
    )
    if outcome == "reconciliation_pending":
        return {
            "ok": False, "status": "reconciliation_pending", "recoverable": True,
            "package_id": package["package_id"], "package_seal": seal,
            "run_id": package["run_id"], "destination": destination,
            "message": "upload outcome unknown; re-run publish to "
                       "reconcile/resume — NO duplicate upload will be made",
        }

    # ---- reconciliation (§23/§24) ---------------------------------------------
    return _reconcile(
        package=package, seal=seal, destination=dest, metadata=metadata,
        youtube_video_id=youtube_video_id, ledger=ledger,
        token_provider=token_provider, transport=transport,
        max_retries=max_retries, backoff_base=backoff_base, now=now,
    )


def _api_metadata(metadata: dict) -> dict:
    """The YouTube Data API v3 resource body (snippet/status only)."""
    snippet = {
        "title": metadata["title"],
        "description": metadata.get("description", ""),
    }
    if metadata.get("tags"):
        snippet["tags"] = metadata["tags"]
    if metadata.get("category_id"):
        snippet["categoryId"] = metadata["category_id"]
    status = {"privacyStatus": metadata["privacy_status"]}
    if metadata.get("publish_at"):
        status["publishAt"] = metadata["publish_at"]
    return {"snippet": snippet, "status": status}


def _transfer_chunks(*, transport, session_url: str, video_data: bytes,
                     offset: int, total: int, ledger: PublishLedger,
                     seal: str, dest: str, chunk_bytes: int, now,
                     max_retries: int, backoff_base: float,
                     youtube_video_id: str | None) -> tuple[str, str | None]:
    """Chunked resumable transfer. Returns (outcome, video_id) where
    outcome is ``uploaded`` or ``reconciliation_pending`` (§20: an unknown
    outcome is NEVER treated as a failed upload). Raises PublishError for
    hard, classified failures."""
    transient_failures = 0
    offset = min(offset, total)
    while offset < total:
        chunk = video_data[offset:offset + chunk_bytes]
        try:
            response = transport.put_chunk(session_url, chunk, offset, total)
        except Exception as exc:  # noqa: BLE001 — network/transport failure
            category = categorize_exception(exc)
            if offset > 0:
                # Bytes were transmitted: the outcome is UNKNOWN, never a
                # failed upload (§20) — persist the session and reconcile.
                ledger.update(seal, dest, status="reconciliation_pending",
                              now=now(), bytes_sent=offset,
                              error_code=category,
                              error_message=f"connection lost after {offset} "
                                            "bytes; outcome unknown")
                return "reconciliation_pending", None
            transient_failures += 1
            if category not in RETRYABLE_CATEGORIES \
                    or transient_failures > max_retries:
                ledger.update(seal, dest, status="upload_failed", now=now(),
                              error_code=category,
                              error_message=f"chunk upload failed before any "
                                            f"bytes: {exc}")
                raise PublishError(category,
                                   f"chunk upload failed before any bytes: {exc}"
                                   ) from exc
            if backoff_base > 0:
                time.sleep(backoff_base * (2 ** (transient_failures - 1)))
            continue
        if response.status in (200, 201):
            body = response.json() or {}
            ledger.update(seal, dest, status="uploaded", now=now(),
                          youtube_video_id=body.get("id"), bytes_sent=total)
            return "uploaded", body.get("id")
        if response.status == 308:
            offset = (response.range_end or (offset + len(chunk) - 1)) + 1
            ledger.update(seal, dest, status="upload_in_progress", now=now(),
                          bytes_sent=offset)
            continue
        error = _classify_transport_response(response, "upload_chunk")
        if error.code in RETRYABLE_CATEGORIES and offset == 0:
            transient_failures += 1
            if transient_failures > max_retries:
                ledger.update(seal, dest, status="upload_failed", now=now(),
                              error_code=error.code, error_message=error.message)
                raise error
            if backoff_base > 0:
                time.sleep(backoff_base * (2 ** (transient_failures - 1)))
            continue
        ledger.update(seal, dest, status="upload_failed", now=now(),
                      error_code=error.code, error_message=error.message)
        raise error
    # loop ended without a video id (e.g. everything already transferred
    # but the final response was lost): truthful unknown outcome
    ledger.update(seal, dest, status="reconciliation_pending", now=now(),
                  error_code="transient_network_error",
                  error_message="transfer loop ended without a final response; "
                                "outcome unknown")
    return "reconciliation_pending", None


def _reconcile(*, package: dict, seal: str, destination: str,
               metadata: dict, youtube_video_id: str | None,
               ledger: PublishLedger, token_provider: TokenProvider,
               transport, max_retries: int, backoff_base: float, now) -> dict:
    """Post-upload reconciliation via the official API (§23): the video
    must exist and match the requested metadata before ``published``.
    A temporary lookup failure → ``reconciliation_pending`` (§24)."""
    if not youtube_video_id:
        ledger.update(seal, destination, status="reconciliation_pending",
                      now=now(), error_code="reconciliation_pending",
                      error_message="no video id to reconcile")
        return {
            "ok": False, "status": "reconciliation_pending", "recoverable": True,
            "package_id": package["package_id"], "package_seal": seal,
            "run_id": package["run_id"], "destination": destination,
            "message": "no YouTube video id is known; re-run publish to "
                       "reconcile (the ledger prevents duplicate uploads)",
        }
    ledger.update(seal, destination, status="reconciling", now=now(),
                  youtube_video_id=youtube_video_id)

    for attempt in range(max_retries + 1):
        try:
            access_token = token_provider.get_access_token()
        except PublishError as exc:
            ledger.update(seal, destination, status="reconciliation_pending",
                          now=now(), error_code=exc.code,
                          error_message=exc.message)
            return {
                "ok": False, "status": "reconciliation_pending",
                "recoverable": True,
                "package_id": package["package_id"], "package_seal": seal,
                "run_id": package["run_id"], "destination": destination,
                "youtube_video_id": youtube_video_id,
                "message": "upload accepted but reconciliation could not "
                           "authenticate; re-run publish to reconcile",
            }
        response = transport.get_video(access_token, youtube_video_id)
        if response.status == 200:
            return _reconcile_video(
                package=package, seal=seal, destination=destination,
                metadata=metadata, youtube_video_id=youtube_video_id,
                response=response, ledger=ledger, now=now)
        category = categorize_http_status(
            response.status, response.body.decode("utf-8", "replace"))
        if category in RETRYABLE_CATEGORIES and attempt < max_retries:
            if backoff_base > 0:
                time.sleep(backoff_base * (2 ** attempt))
            continue
        # temporary lookup failure → reconciliation_pending (NOT failed, §24)
        ledger.update(seal, destination, status="reconciliation_pending",
                      now=now(), error_code=category,
                      error_message=f"reconciliation lookup failed "
                                    f"(HTTP {response.status})")
        return {
            "ok": False, "status": "reconciliation_pending", "recoverable": True,
            "package_id": package["package_id"], "package_seal": seal,
            "run_id": package["run_id"], "destination": destination, "youtube_video_id": youtube_video_id,
            "message": "upload succeeded but reconciliation is temporarily "
                       "unavailable; re-run publish to reconcile",
        }
    # defensive: exhausted retries with no classified response
    ledger.update(seal, destination, status="reconciliation_pending", now=now(),
                  error_code="transient_network_error",
                  error_message="reconciliation retries exhausted")
    return {
        "ok": False, "status": "reconciliation_pending", "recoverable": True,
        "package_id": package["package_id"], "package_seal": seal,
        "run_id": package["run_id"], "destination": destination, "youtube_video_id": youtube_video_id,
        "message": "reconciliation retries exhausted; re-run publish",
    }


def _reconcile_video(*, package: dict, seal: str, destination: str,
                     metadata: dict, youtube_video_id: str, response,
                     ledger: PublishLedger, now) -> dict:
    """Compare the ACTUAL published video resource against the request
    (§23). Mismatch → reconciliation_failed (operator review)."""
    body = response.json() or {}
    items = body.get("items") or []
    if not items:
        # not (yet) visible via the API: processing — pending, NOT failed
        ledger.update(seal, destination, status="reconciliation_pending",
                      now=now(), error_code="reconciliation_pending",
                      error_message="video not yet returned by the API")
        return {
            "ok": False, "status": "reconciliation_pending",
            "recoverable": True,
            "package_id": package["package_id"], "package_seal": seal,
            "run_id": package["run_id"], "destination": destination,
            "youtube_video_id": youtube_video_id,
            "message": "the video exists but is not yet reconcilable "
                       "(processing); re-run publish to reconcile",
        }
    video = items[0]
    snippet = video.get("snippet") or {}
    status = video.get("status") or {}
    mismatches = []
    if snippet.get("title") != metadata["title"]:
        mismatches.append("title")
    if snippet.get("description", "") != metadata.get("description", ""):
        mismatches.append("description")
    if status.get("privacyStatus") != metadata["privacy_status"]:
        mismatches.append("privacy_status")
    if mismatches:
        ledger.update(seal, destination, status="reconciliation_failed",
                      now=now(), error_code="reconciliation_failed",
                      error_message="mismatched: " + ", ".join(mismatches))
        return {
            "ok": False, "status": "reconciliation_failed",
            "recoverable": False,
            "package_id": package["package_id"], "package_seal": seal,
            "run_id": package["run_id"], "destination": destination,
            "youtube_video_id": youtube_video_id,
            "mismatches": mismatches,
            "message": "the published video's metadata does not match the "
                       "request; operator review required",
        }
    record = ledger.update(seal, destination, status="published", now=now(),
                           reconciled_at=now(), clear_error=True)
    return {
        "ok": True, "status": "published",
        "package_id": package["package_id"], "package_seal": seal,
        "run_id": package["run_id"], "destination": destination,
        "youtube_video_id": youtube_video_id,
        "privacy_status": record.privacy_status,
        "reconciled_at": record.reconciled_at,
        "attempt_count": record.attempt_count,
    }