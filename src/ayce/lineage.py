"""Stage 3 — Objective lineage: durable references over the EXISTING state layer.

Makes one traceable production lineage::

    OBJECTIVE (content-addressed; durable carrier = the ResearchArtifact)
        ↓  research_id (data/research/res-*.json)
    RESEARCH ARTIFACT
        ↓  script_id (brief-<token>; Stage 1/2 convention) + script_sha256
    SCRIPT / BRIEF (copied byte-exact into the run as script_input.json)
        ↓  run_id
    PRODUCTION RUN (RunState.lineage + ArtifactKind.SCRIPT/RESEARCH refs)
        ↓
    ARTIFACTS → QA REPORT (existing QA hash binding, unchanged)

Design rules:

- NO new database, NO new registry, NO graph framework. This module is a
  set of small deterministic helpers over ``RunState`` and
  ``ArtifactRegistry`` (explicitly allowed: "small helper functions").
- The objective is CONTENT-ADDRESSED: ``objective_id = obj-<sha256 of the
  normalized objective text>``. The durable carrier of the objective text
  is the ResearchArtifact itself (``.objective``) — no second Objective
  store is created.
- The run retains references, not copies: the research artifact stays
  canonical in ``data/research/``; the run records a bounded REFERENCE
  record (id, sha256, objective, status, counts) plus the byte-exact
  ScriptInput that actually entered production.
- Additive and backward-compatible: ``RunState.lineage`` is optional;
  old state files (without the field) load unchanged and keep
  ``lineage=None``. Manual/fixture runs (production_id NOT a research id)
  get NO lineage artifacts at all — their artifact kind-sets are
  byte-identical to pre-Stage-3 runs.
- Derivation convention (documented Stage 1/2): a generated script uses
  ``production_id == research_id`` and ``script_id == brief-<token>``.
  Lineage is discovered from the script input alone — callers (including
  the Director and plain ``ayce run``) need no new parameters.
- Lineage decoration NEVER fails production: an unresolvable research
  artifact degrades to reference fields set to None, truthfully.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .artifacts import ArtifactKind, ArtifactRegistry
from .config import Config
from .state import RunState, StateError

if TYPE_CHECKING:  # type-hints only; keeps import cost/cycles out
    from .research.models import ResearchArtifact

__all__ = [
    "RESEARCH_ID_RE",
    "SCRIPT_INPUT_FILENAME",
    "RESEARCH_REFERENCE_FILENAME",
    "LINEAGE_STAGE_SCRIPT",
    "LINEAGE_STAGE_RESEARCH",
    "ProductionLineage",
    "objective_id_for",
    "script_id_for_research",
    "research_store_dir",
    "discover",
    "register_lineage",
    "lineage_for_run",
    "find_runs",
]

#: Research artifact identifier grammar (mirrors research.models).
RESEARCH_ID_RE = re.compile(r"^res-[A-Za-z0-9._-]{1,64}$")

#: Run-relative name of the byte-exact ScriptInput copy inside a run.
SCRIPT_INPUT_FILENAME = "script_input.json"
#: Run-relative name of the bounded research REFERENCE record.
RESEARCH_REFERENCE_FILENAME = "research_reference.json"
#: Registry stage labels for lineage inputs (plain snake_case identifiers).
LINEAGE_STAGE_SCRIPT = "script_input"
LINEAGE_STAGE_RESEARCH = "research_input"

_OBJECTIVE_TEXT_CLIP = 300


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def objective_id_for(objective_text: str) -> str:
    """Content-addressed objective identity: ``obj-<sha256[:16]>`` of the
    whitespace-normalized objective text. Deterministic and stable: the
    same objective always yields the same id, without any objective store.
    """
    normalized = " ".join(objective_text.split())
    return "obj-" + _sha256_hex(normalized.encode("utf-8"))[:16]


def script_id_for_research(research_id: str) -> str | None:
    """The Stage 1/2 convention: ``brief-<artifact token>``. Returns None
    when the token cannot form a valid identifier (no fabrication)."""
    token = research_id.split("-")[-1]
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", token):
        return None
    return f"brief-{token}"


def research_store_dir(config: Config) -> Path:
    """The controlled research artifact store (same convention as the
    research worker: ``<data_dir>/research``)."""
    return config.resolved_data_dir / "research"


@dataclass(frozen=True)
class ProductionLineage:
    """Durable lineage references for one production run.

    ``research_reference`` carries the bounded reference-record payload for
    the RESEARCH artifact; it is NOT part of the serialized state lineage.
    """

    script_sha256: str
    script_id: str | None = None
    research_id: str | None = None
    research_sha256: str | None = None
    objective_id: str | None = None
    objective_text: str | None = None
    research_reference: dict | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """The state-lineage projection: references only, Nones omitted
        (old state files without lineage stay loadable; new state files
        carry exactly the reference fields)."""
        return {
            key: value
            for key, value in (
                ("script_id", self.script_id),
                ("script_sha256", self.script_sha256),
                ("research_id", self.research_id),
                ("research_sha256", self.research_sha256),
                ("objective_id", self.objective_id),
                ("objective_text", self.objective_text),
            )
            if value is not None
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ProductionLineage | None":
        """Tolerant reload (old/foreign dicts → None fields)."""
        if not isinstance(data, dict):
            return None
        return cls(
            script_sha256=str(data.get("script_sha256") or ""),
            script_id=data.get("script_id"),
            research_id=data.get("research_id"),
            research_sha256=data.get("research_sha256"),
            objective_id=data.get("objective_id"),
            objective_text=data.get("objective_text"),
        )


def _canonical_script_bytes(script_input: Any) -> bytes:
    """Byte source for instance/dict script inputs (path-based inputs use
    the exact file bytes — see :func:`discover`)."""
    if hasattr(script_input, "model_dump_json"):
        return script_input.model_dump_json().encode("utf-8")
    return json.dumps(script_input, sort_keys=True, separators=(",", ":")).encode("utf-8")


def discover(
    script_input: Any,
    *,
    config: Config,
    script_bytes: bytes | None = None,
) -> "ProductionLineage | None":
    """Derive the production lineage from the script input.

    Returns None unless ``production_id`` is a research artifact id (the
    documented Stage 1/2 convention) — fixture/manual runs are untouched.
    Never raises: an unresolvable research artifact degrades to None
    reference fields (truthful, never fabricated).
    """
    if isinstance(script_input, dict):
        production_id = script_input.get("production_id")
    else:
        production_id = getattr(script_input, "production_id", None)
    if not isinstance(production_id, str) or not RESEARCH_ID_RE.match(production_id):
        return None

    script_bytes = (
        script_bytes if script_bytes is not None
        else _canonical_script_bytes(script_input)
    )
    research_id = production_id
    research_sha256: str | None = None
    objective_id: str | None = None
    objective_text: str | None = None
    reference: dict | None = None

    research_path = research_store_dir(config) / f"{research_id}.json"
    try:
        raw = research_path.read_bytes()
        from .research.models import ResearchArtifact  # local import: no hard dep

        artifact: ResearchArtifact = ResearchArtifact.model_validate_json(raw)
        research_sha256 = _sha256_hex(raw)
        objective_text = artifact.objective
        objective_id = objective_id_for(artifact.objective)
        reference = {
            "research_id": research_id,
            "source_store": f"data/research/{research_id}.json",
            "research_sha256": research_sha256,
            "objective_id": objective_id,
            "objective_text": artifact.objective[:_OBJECTIVE_TEXT_CLIP],
            "status": artifact.status.value,
            "candidate_count": len(artifact.candidates),
            "collected_at": artifact.collected_at,
            "worker_version": artifact.provenance.worker_version,
        }
    except (OSError, ValueError):
        # Truthful degradation: keep the id-level references, mark the
        # unresolvable artifact with None hashes — never fabricate.
        reference = None

    return ProductionLineage(
        script_sha256=_sha256_hex(script_bytes),
        script_id=script_id_for_research(research_id),
        research_id=research_id,
        research_sha256=research_sha256,
        objective_id=objective_id,
        objective_text=objective_text[:_OBJECTIVE_TEXT_CLIP] if objective_text else None,
        research_reference=reference,
    )


def _atomic_write(path: Path, data: bytes) -> None:
    """Atomic run-dir write (tmp + os.replace — the AYCE convention)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def register_lineage(
    run_state: RunState,
    registry: ArtifactRegistry,
    lineage: ProductionLineage,
    script_bytes: bytes | None = None,
) -> None:
    """Register the run's lineage INPUT artifacts (idempotent within a run).

    - ``ArtifactKind.SCRIPT``: the byte-exact ScriptInput that entered
      production, copied into the run directory (self-contained run; the
      canonical generated-script file elsewhere remains untouched).
    - ``ArtifactKind.RESEARCH``: a bounded REFERENCE record to the canonical
      research artifact (id + sha256 + objective + counts) — the research
      payload itself is NOT copied.

    Idempotency: a stage already holding a same-sha256 artifact is skipped,
    so re-invocation never duplicates registrations (the registry appends
    refs; the guard keeps that honest, mirroring the P1-B pattern).
    """
    if script_bytes is None:
        return  # nothing supplied to register — truthful no-op, never invented

    existing_script = registry.for_stage(LINEAGE_STAGE_SCRIPT)
    if not any(
        ref.metadata.get("script_sha256") == lineage.script_sha256
        for ref in existing_script
    ):
        script_path = registry.run_dir / SCRIPT_INPUT_FILENAME
        _atomic_write(script_path, script_bytes)
        registry.register(
            LINEAGE_STAGE_SCRIPT,
            ArtifactKind.SCRIPT,
            SCRIPT_INPUT_FILENAME,
            metadata={
                "production_id": lineage.research_id or lineage.script_id,
                "script_id": lineage.script_id,
                "script_sha256": lineage.script_sha256,
                "research_id": lineage.research_id,
                "objective_id": lineage.objective_id,
            },
        )

    if lineage.research_reference is not None and not registry.for_stage(
        LINEAGE_STAGE_RESEARCH
    ):
        reference_json = (
            json.dumps(lineage.research_reference, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        _atomic_write(registry.run_dir / RESEARCH_REFERENCE_FILENAME, reference_json)
        registry.register(
            LINEAGE_STAGE_RESEARCH,
            ArtifactKind.RESEARCH,
            RESEARCH_REFERENCE_FILENAME,
            metadata={
                "research_id": lineage.research_id,
                "research_sha256": lineage.research_sha256,
                "objective_id": lineage.objective_id,
                "reference_only": True,
            },
        )


# ---- reverse lookup (small deterministic helpers — no graph database) ---------------


def lineage_for_run(run_dir: str | Path) -> dict | None:
    """Reverse lineage for ONE run: run → {script_id, research_id,
    objective_id, …}. Returns None for runs without lineage (or unreadable
    state) — never invented."""
    state_path = Path(run_dir) / "state.json"
    if not state_path.is_file():
        return None
    try:
        state = RunState.load(state_path)
    except StateError:
        return None
    return state.lineage


def find_runs(
    data_dir: str | Path,
    *,
    research_id: str | None = None,
    script_id: str | None = None,
    objective_id: str | None = None,
) -> list[dict]:
    """Forward/reverse lineage scan over the EXISTING run store:
    ``<data_dir>/runs/*/state.json``. Returns compact deterministic records
    ({run_id, job_id, lineage}) for runs whose lineage matches ALL supplied
    filters. Runs without lineage never match (truthful)."""
    runs_root = Path(data_dir) / "runs"
    matches: list[dict] = []
    if not runs_root.is_dir():
        return matches
    for run_dir in sorted(runs_root.iterdir()):
        state_path = run_dir / "state.json"
        if not run_dir.is_dir() or not state_path.is_file():
            continue
        try:
            state = RunState.load(state_path)
        except StateError:
            continue  # corrupt state is never silently claimed as a match
        lineage = state.lineage
        if not isinstance(lineage, dict):
            continue
        if research_id is not None and lineage.get("research_id") != research_id:
            continue
        if script_id is not None and lineage.get("script_id") != script_id:
            continue
        if objective_id is not None and lineage.get("objective_id") != objective_id:
            continue
        matches.append({
            "run_id": state.run_id,
            "job_id": state.job_id,
            "lineage": lineage,
        })
    return matches


