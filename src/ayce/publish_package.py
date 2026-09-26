"""Stage 4 — Sealed publish-package boundary (dry-run handoff, NOT a publisher).

Turns one completed, QA-verified production run into the smallest
deterministic handoff artifact a future publishing subsystem may consume::

    7-stage Golden Path (QA PASS)
        ↓  seal_publish_package()   — deterministic, QA-gated
    publish_package.json (sealed, self-describing, immutable-after-seal)
        ↓  verify_publish_package() — deterministic self-consistency check
    future publisher (Stage 5+; NOT implemented here — nothing is uploaded)

Hard boundary rules:

- The package is a MANIFEST + ATTESTATION, never a payload container: no
  video/audio/image bytes, no research payload, no full script payload.
  Media stays in its canonical artifact location inside the run directory.
- The QA gate is NOT bypassable: a run whose QA verdict is not ``PASS``
  cannot be sealed, and there is no force/ignore/skip flag.
- Sealing is IDEMPOTENT: re-sealing an unchanged run returns the identical
  package; sealing a run whose previously sealed inputs changed fails
  safely instead of overwriting.
- Deterministic: package_id and seal are content-derived (sha256 over the
  canonical JSON serialization). No randomness, no wall-clock time in the
  sealed payload (``created_at`` is the production run's own timestamp).
- NO new database, NO second registry, NO publishing API: the package is
  one JSON file inside the originating run directory, built from the
  existing RunState + ArtifactRegistry + lineage (Stage 3 field names).
- Safety: run identifiers/paths are resolved strictly inside the
  configured data root; no shell, no eval, no caller-controlled paths.

Failure codes (structured, truthful): run_not_found, run_not_successful,
lineage_invalid, qa_missing, qa_failed, render_missing, render_hash_mismatch,
artifact_missing, package_conflict, package_invalid, package_seal_mismatch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from .artifacts import ArtifactKind, ArtifactRegistry
from .config import Config
from .pipeline import PIPELINE_STAGES
from .state import RunState, StateError, StageStatus

__all__ = [
    "PACKAGE_VERSION",
    "PACKAGE_FILENAME",
    "SEAL_ALGORITHM",
    "PackageError",
    "seal_publish_package",
    "verify_publish_package",
    "resolve_run_dir",
    "load_publish_package",
]

#: Explicit package schema version (future publishers reject unsupported
#: versions deterministically; no migration framework exists by design).
PACKAGE_VERSION = 1

#: Package file name, stored inside the originating run directory.
PACKAGE_FILENAME = "publish_package.json"

#: The only seal algorithm of this stage (a bare sha256 content seal).
SEAL_ALGORITHM = "sha256"

#: AYCE run-id grammar (mirrors mcp-ayce-readonly: rejects paths/"..").
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Fields excluded from the sealed core when re-computing the seal.
_UNSEALED_FIELDS = frozenset({"package_id", "seal"})

_PACKAGE_IO_LOCK = threading.Lock()


class PackageError(RuntimeError):
    """Classified package-boundary failure (never faked into success)."""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        error = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json(payload: Any) -> bytes:
    """Canonical serialization: sorted keys, compact separators. The seal
    is defined over exactly these bytes."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ---- controlled run resolution (§29: no traversal, data-root confined) --------------


def resolve_run_dir(config: Config, run_id: str) -> Path:
    """Resolve a caller-supplied run identifier to a directory strictly
    inside the configured runs root. Grammar + containment — a path can
    never escape the data root, by construction and by check."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise PackageError("run_not_found", "run_id must be a plain run identifier")
    runs_root = (config.resolved_data_dir / "runs").resolve()
    candidate = (runs_root / run_id).resolve()
    if candidate != runs_root and runs_root not in candidate.parents:
        raise PackageError("run_not_found", "resolved run path escaped the data root")
    if not candidate.is_dir():
        raise PackageError("run_not_found", f"no such run directory: {run_id}")
    return candidate


def load_publish_package(run_dir: str | Path) -> dict | None:
    """Load the sealed package of one run (None when absent)."""
    path = Path(run_dir) / PACKAGE_FILENAME
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---- seal helpers ----------------------------------------------------------------------


def _seal_core(core: dict) -> str:
    return _sha256_hex(_canonical_json(core))


def _artifact_entry(run_dir: Path, ref) -> dict:
    """One manifest entry: reference + content digest + size (no payload)."""
    file_path = run_dir / ref.path
    if not file_path.is_file():
        raise PackageError(
            "artifact_missing",
            f"registered artifact file is missing: {ref.path}",
            details={"artifact_id": ref.artifact_id, "path": ref.path},
        )
    data = file_path.read_bytes()
    return {
        "artifact_id": ref.artifact_id,
        "kind": ref.kind.value,
        "stage": ref.stage,
        "path": ref.path,
        "sha256": _sha256_hex(data),
        "size": len(data),
    }


def _require_single_ref(registry: ArtifactRegistry, kind: ArtifactKind,
                        missing_code: str, missing_message: str):
    refs = [r for r in registry.all() if r.kind is kind]
    if not refs:
        raise PackageError(missing_code, missing_message)
    return refs[-1]  # the registry appends; the latest ref is authoritative


def _production_id_from(state: RunState, registry: ArtifactRegistry, run_dir: Path) -> str | None:
    """The production id of the run: the Scene Contract's production_id,
    read from the persisted scene manifest (fallback: lineage)."""
    for ref in registry.all():
        if ref.kind is ArtifactKind.SCENE_MANIFEST:
            manifest_path = Path(run_dir) / ref.path
            if manifest_path.is_file():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                production_id = manifest.get("production_id")
                if isinstance(production_id, str) and production_id:
                    return production_id
    if isinstance(state.lineage, dict):
        return state.lineage.get("production_id")
    return None


def build_package_payload(run_dir: Path) -> dict:
    """Deterministically build the SEALED-CORE payload for one run, with
    every acceptance gate enforced. Raises :class:`PackageError` on any
    gate failure — gates are ordered cheapest-first and are NOT bypassable.
    """
    state_path = run_dir / "state.json"
    if not state_path.is_file():
        raise PackageError("run_not_found", f"run state not found: {run_dir.name}")
    try:
        state = RunState.load(state_path)
        registry = ArtifactRegistry.load(run_dir, state.run_id)
    except StateError as exc:
        raise PackageError("run_not_found", f"run state unreadable: {exc}") from exc

    # Gate 1: the WHOLE pipeline succeeded (all 7 stages, terminal succeeded).
    not_succeeded = [
        stage for stage in PIPELINE_STAGES
        if stage not in state.stages
        or state.stage(stage).status is not StageStatus.SUCCEEDED
    ]
    if not_succeeded:
        raise PackageError(
            "run_not_successful",
            "run has stages that did not succeed; a publish package requires "
            "a fully succeeded production run",
            details={"stages_not_succeeded": not_succeeded},
        )

    # Gate 2: Stage 3 lineage must be present and complete — an untraceable
    # video is not publishable.
    lineage = state.lineage
    required_lineage = ("objective_id", "research_id", "script_id", "script_sha256")
    if not isinstance(lineage, dict) or any(not lineage.get(k) for k in required_lineage):
        raise PackageError(
            "lineage_invalid",
            "run lineage is missing or incomplete; a publish package must be "
            "traceable to objective, research and script",
            details={"lineage": lineage if isinstance(lineage, dict) else None},
        )

    # Gate 3: QA artifact present and verdict PASS (no bypass exists).
    qa_ref = _require_single_ref(registry, ArtifactKind.QA_REPORT, "qa_missing",
                                 "run has no QA report artifact")
    render_ref = _require_single_ref(registry, ArtifactKind.RENDERED_VIDEO,
                                     "render_missing", "run has no rendered video artifact")
    qa_file = run_dir / qa_ref.path
    if not qa_file.is_file():
        raise PackageError("qa_missing",
                           f"QA report artifact file is missing: {qa_ref.path}",
                           details={"artifact_id": qa_ref.artifact_id})
    qa_report = json.loads(qa_file.read_text(encoding="utf-8"))
    if qa_report.get("verdict") != "PASS":
        raise PackageError(
            "qa_failed",
            f"QA verdict is {qa_report.get('verdict')!r}; only PASS runs can be "
            "sealed as publish-ready",
            details={"verdict": qa_report.get("verdict")},
        )

    # Gate 4: content digests — the render FILE must still match its
    # production-time hash, and QA must attest exactly that hash.
    render_entry = _artifact_entry(run_dir, render_ref)
    recorded_render_sha = render_ref.metadata.get("sha256")
    if recorded_render_sha and render_entry["sha256"] != recorded_render_sha:
        raise PackageError(
            "render_hash_mismatch",
            "rendered video file no longer matches its production-time hash",
            details={"file_sha256": render_entry["sha256"],
                     "recorded_sha256": recorded_render_sha},
        )
    qa_render_sha = qa_report.get("render_sha256") or qa_ref.metadata.get("render_sha256")
    if qa_render_sha and qa_render_sha != render_entry["sha256"]:
        raise PackageError(
            "render_hash_mismatch",
            "QA attests a different render hash than the packaged video",
            details={"qa_render_sha256": qa_render_sha,
                     "render_sha256": render_entry["sha256"]},
        )

    # Manifest: EVERY registered artifact of the run, content-digested
    # (the run is self-describing; the manifest carries references only).
    manifest = [
        _artifact_entry(run_dir, ref)
        for ref in sorted(registry.all(), key=lambda r: (r.stage, r.artifact_id))
    ]

    summary = qa_report.get("summary") or {}
    return {
        "package_version": PACKAGE_VERSION,
        "run_id": state.run_id,
        "job_id": state.job_id,
        "production_id": _production_id_from(state, registry, run_dir),
        "created_at": state.created_at,  # the production run's own timestamp
        "lineage": {
            key: lineage[key] for key in (
                "objective_id", "research_id", "script_id",
                "script_sha256", "research_sha256", "objective_text",
            ) if lineage.get(key) is not None
        },
        "render": {
            "artifact_id": render_ref.artifact_id,
            "kind": render_ref.kind.value,
            "path": render_ref.path,
            "sha256": render_entry["sha256"],
            "size": render_entry["size"],
        },
        "qa": {
            "artifact_id": qa_ref.artifact_id,
            "kind": qa_ref.kind.value,
            "path": qa_ref.path,
            "verdict": qa_report["verdict"],
            "render_sha256": qa_render_sha,
            "total_checks": summary.get("total_checks"),
            "failed_checks": summary.get("failed"),
        },
        "artifact_manifest": manifest,
    }


def seal_publish_package(run_dir: str | Path) -> dict:
    """Seal one completed, QA-PASS, lineage-complete run into an immutable
    publish package (``<run_dir>/publish_package.json``).

    Sealing model:
        payload core (canonical JSON) → sha256 → package_id ("pkg-<prefix>")
        + package_id added to the core → sha256 → seal

    Idempotency: sealing an unchanged run again returns the EXISTING
    package byte-for-byte (no second seal, no overwrite). If the sealed
    inputs changed since the previous seal, the operation FAILS with
    ``package_conflict`` instead of silently overwriting.
    """
    run_dir = Path(run_dir)
    core = build_package_payload(run_dir)  # all gates enforced here

    # Deterministic rebuild: the SAME sealed state always produces the SAME
    # package bytes (package_id and seal are content-derived).
    seal_value = _seal_core(core)
    package_id = "pkg-" + seal_value[:16]
    sealed_core = dict(core)
    sealed_core["package_id"] = package_id
    package = {
        **sealed_core,
        "seal": {"algorithm": SEAL_ALGORITHM, "value": _seal_core(sealed_core)},
    }

    with _PACKAGE_IO_LOCK:
        existing_path = run_dir / PACKAGE_FILENAME
        if existing_path.is_file():
            try:
                existing = json.loads(existing_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise PackageError(
                    "package_invalid",
                    f"existing publish package is corrupt: {exc}") from exc
            if existing == package:
                return existing  # byte-identical: sealed before, reuse it
            # A difference means the previously sealed state changed (or the
            # file is corrupt) — never overwrite silently.
            raise PackageError(
                "package_conflict",
                "a different publish package already exists for this run; "
                "refusing to overwrite (the previously sealed state changed)",
                details={"existing_package_id": existing.get("package_id"),
                         "new_package_id": package_id},
            )

        _atomic_write(
            existing_path,
            (json.dumps(package, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
        return package


def verify_publish_package(package: Any, *, run_dir: str | Path | None = None) -> dict:
    """Deterministic self-consistency verification (never repairs anything).

    Without ``run_dir``: structural + seal verification only. With
    ``run_dir``: FULL content verification — every manifest entry is
    re-hashed against the run directory, lineage is compared against the
    persisted RunState, and the QA attestation is re-checked against the
    render. Returns
    ``{"ok", "package_id", "run_id", "verified_content", "errors"}``.
    """
    errors: list[dict] = []

    def _fail(code: str, message: str, **details: Any) -> None:
        entry = {"code": code, "message": message}
        if details:
            entry["details"] = details
        errors.append(entry)

    if not isinstance(package, dict):
        return {"ok": False, "package_id": None, "run_id": None,
                "verified_content": False,
                "errors": [{"code": "package_invalid",
                            "message": "package must be a JSON object"}]}

    if package.get("package_version") != PACKAGE_VERSION:
        _fail("package_invalid",
              f"unsupported package_version {package.get('package_version')!r}; "
              f"this verifier understands version {PACKAGE_VERSION}")

    seal = package.get("seal")
    if not isinstance(seal, dict) or seal.get("algorithm") != SEAL_ALGORITHM \
            or not isinstance(seal.get("value"), str):
        _fail("package_seal_mismatch", "missing or unsupported seal")
    else:
        # The stored seal covers the package WITHOUT the "seal" field
        # (package_id INCLUDED — exactly what seal_publish_package hashed).
        core = {k: v for k, v in package.items() if k != "seal"}
        recomputed = _seal_core(core)
        if recomputed != seal["value"]:
            _fail("package_seal_mismatch",
                  "package content does not match its seal",
                  expected=recomputed, found=seal["value"])
        # package_id derives from the core BEFORE package_id was added.
        core_no_pid = {k: v for k, v in core.items() if k != "package_id"}
        derived_pid = "pkg-" + _seal_core(core_no_pid)[:16]
        if package.get("package_id") != derived_pid:
            _fail("package_seal_mismatch",
                  "package_id does not match the sealed content",
                  expected=derived_pid, found=package.get("package_id"))

    run_id = package.get("run_id")
    lineage = package.get("lineage")
    if not isinstance(lineage, dict) or not lineage.get("objective_id") \
            or not lineage.get("research_id") or not lineage.get("script_id"):
        _fail("lineage_invalid", "package lineage is missing required references")
    if not isinstance(package.get("render"), dict) or not package["render"].get("sha256"):
        _fail("package_invalid", "package has no render identity")
    if not isinstance(package.get("qa"), dict) or package["qa"].get("verdict") != "PASS":
        _fail("qa_failed", "package QA attestation is not a PASS verdict")
    if not isinstance(package.get("artifact_manifest"), list) \
            or not package["artifact_manifest"]:
        _fail("package_invalid", "package artifact manifest is missing or empty")

    verified_content = False
    if run_dir is not None and not errors:
        # FULL content verification against the originating run directory.
        run_dir = Path(run_dir)
        try:
            state = RunState.load(run_dir / "state.json")
        except StateError as exc:
            _fail("run_not_found", f"run state unreadable: {exc}")
            state = None
        if state is not None:
            if state.run_id != run_id:
                _fail("lineage_invalid", "package run_id does not match the run state",
                      package=run_id, state=state.run_id)
            if state.lineage != lineage:
                _fail("lineage_invalid",
                      "package lineage does not match the persisted run lineage")
        for entry in package.get("artifact_manifest", []):
            if not isinstance(entry, dict):
                _fail("package_invalid", "malformed manifest entry")
                continue
            artifact_path = run_dir / entry.get("path", "")
            if not artifact_path.is_file():
                _fail("artifact_missing",
                      f"packaged artifact file is missing: {entry.get('path')}",
                      artifact_id=entry.get("artifact_id"))
                continue
            actual = _sha256_hex(artifact_path.read_bytes())
            if actual != entry.get("sha256"):
                _fail("artifact_hash_mismatch",
                      f"packaged artifact content changed after sealing: "
                      f"{entry.get('path')}",
                      artifact_id=entry.get("artifact_id"),
                      expected=entry.get("sha256"), found=actual)
        render = package.get("render") or {}
        qa = package.get("qa") or {}
        if render.get("sha256") and qa.get("render_sha256") \
                and render["sha256"] != qa["render_sha256"]:
            _fail("render_hash_mismatch",
                  "QA attestation does not match the packaged render")
        verified_content = not errors

    return {
        "ok": not errors,
        "package_id": package.get("package_id"),
        "run_id": run_id,
        "verified_content": verified_content,
        "errors": errors,
    }
