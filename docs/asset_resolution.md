# P2 — Asset Resolution (file-backed provider adapter)

## What it is

`src/ayce/asset_resolution.py` is the second bounded, Hermes-compatible
production capability: it takes a **persisted Scene Manifest** (P1-A
contract), extracts each scene's `AssetRequirement`, resolves them through
an injected `AssetProvider` adapter, and persists a separate
**`asset_manifest.json`** artifact.

```text
Scene Contract (creative requirements)      ← stays untouched
      ↓ AssetRequirement (per scene)
Asset Resolution Stage
      ↓ AssetProvider (Adapter ABC)         ← injected by future Hermes
FileBackedAssetProvider (local fixtures)
      ↓
ResolvedAsset (run-local copy + provenance)
      ↓
AssetManifest artifact → ArtifactRegistry → reload + verify
```

**Separation enforced:** `Scene Contract = creative requirements`,
`Asset Resolution = finding/resolving actual media`,
`Hermes = future orchestration of the above`. Resolved-asset data is
NEVER written back into `ProductionManifest`/`Scene`/`VisualIntent`/
`AssetRequirement` (tested).

## Provider / adapter boundary

- `AssetProvider(Adapter)` extends the P0 `Adapter` ABC (`name` ClassVar +
  truthful `health() -> AdapterHealth`) with one operation:
  `resolve(requirement, scene_id, *, run_dir) -> ResolvedAsset`.
- The stage executes the single provider it is given. Provider selection
  and fallback routing are orchestration policy (future Hermes) — not
  stage logic.

## FileBackedAssetProvider

Deterministic local fixture provider. Matching rule: the asset for scene
`S` of kind `K` is the file `<fixture_dir>/<scene_id><ext>` (image →
`.png`, video → `.mp4`). Missing file → explicit `AssetResolutionError`
(never invented paths or placeholders). It copies the file into the run
directory (`assets/`) so the run is self-contained, and records a sha256
digest of the copy. Its health is truthful: **healthy only if the fixture
directory exists and is readable** — a missing directory reports
`available=False`, and the stage refuses to run with an unhealthy provider.

## ResolvedAsset / provenance

Each record: `scene_id`, `kind`, run-relative `path` of the copied file,
`sha256`, echo of the `requirement_description` brief, and provenance
(`provider`, `source="local_fixture"`, `source_ref` = fixture filename).
The `license` field exists for future real providers and stays `None` for
fixtures — **fixture provenance makes no real-world licensing claim**.

## Asset Manifest

`asset_manifest.json` (kind `asset_manifest`, schema version `1.0`):
`schema_version`, `production_id`, `provider`, run-relative
`scene_manifest` reference, `resolved_assets[]` in deterministic scene
order. Persisted/reloaded through the existing `ArtifactRegistry`
(atomic write + idempotency guard identical to P1-B).

## Behavior rules

- **Partial-resolution policy (fail-fast):** if ANY requirement fails,
  the stage fails and publishes NO asset manifest. No partial success,
  no fallback engine.
- **No requirements → empty manifest:** scenes without an
  `AssetRequirement` are skipped; a manifest with zero requirements
  resolves to an empty-but-valid asset manifest.
- **Lifecycle:** `pending → running → succeeded` / `pending → running →
  failed` via the existing P0 `RunState`; retry after failure allowed;
  rerun of a `succeeded` stage on the same `RunState` is rejected
  (terminal). Rerunning with identical input + same run directory reuses
  the existing artifact (no duplicates).

## Hermes integration boundary

```python
result = run_asset_resolution_stage(
    scene_manifest=manifest,        # loaded from the scene_manifest artifact
    run_state=run_state,
    artifact_registry=registry,
    provider=provider,              # selected/injected by the orchestrator
)
# result: ok / manifest / artifact / run_id / production_id / provider / error
```

## Explicit non-scope

No Wikimedia/Archive/Pexels/Pixabay/YouTube providers, no scraping or
download, no yt-dlp, no AI generation, no TTS, no FFmpeg/render, no
publishing/analytics, no MCP, no VPS/Docker/databases/queues, no media
storage system. P2 proves the provider adapter seam with local fixtures.