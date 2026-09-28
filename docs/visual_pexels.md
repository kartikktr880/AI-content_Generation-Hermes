# Pexels Visual-Asset Provider (P8)

`src/ayce/visual_pexels.py` implements the production visual provider behind
the P2 `AssetProvider` adapter seam — **strictly opt-in**. It replaces the
fixture asset provider for real production runs without changing the Scene
Contract, the asset-manifest schema, or the resolver.

- **Selection**: `AYCE_PEXELS_API_KEY` configured → `PexelsVisualProvider`;
  otherwise the deterministic file-backed fixture provider remains the
  default and no network request is ever made. `select_asset_provider()`
  (the only wiring point, called by the pipeline) is orchestration policy:
  the stage still executes exactly ONE provider and never composes
  fallbacks. Pixabay is therefore deliberately NOT implemented — in-provider
  fallback would require a new fallback mechanism the architecture does not
  have.
- **API**: official Pexels REST API (`https://api.pexels.com/v1`), key sent
  only in the `Authorization` header (never in the URL, never logged).
  Photos search `/search`, videos search `/videos/search`, `per_page=15`,
  `size=large`, plus the configured `orientation`.
- **Production minimums**: landscape ≥1920×1080, portrait ≥1080×1920,
  square ≥1080×1080 (from `AYCE_PEXELS_ORIENTATION`; the Scene Contract has
  no orientation field, so orientation is provider configuration rather
  than an invented contract field). Video scenes additionally require a clip
  of at least `AYCE_PEXELS_MIN_VIDEO_DURATION_S` (default 4.0 s) and an MP4
  rendition (`video/mp4`); non-MP4 stream manifests are ignored.
- **Limitation (documented, not hidden)**: `resolve()` receives the
  requirement + scene id only, so per-scene duration adequacy cannot be
  enforced without changing the P2 contract; the configurable minimum is
  the enforced rule.
- **Selection rule**: highest pixel area, then longest duration, then the
  stable cache stem — deterministic; no candidate meeting the minimums is a
  truthful failure with the observed candidate dimensions in the message.
- **Cache**: downloads land in `AYCE_PEXELS_CACHE_DIR`
  (default `<data_dir>/assets_cache/pexels`) keyed by asset id + rendition
  id, so repeat runs reuse the same real file. The resolved copy is streamed
  into the run directory (`assets/<scene_id><ext>`), hashed (sha256 of the
  copy) and validated (non-empty + real MP4/JPEG/PNG signature) — an
  invalid download fails the stage and is never cached.
- **Provenance**: `provider=pexels-api`, `source=pexels`,
  `source_ref=<asset id>`, `license="Pexels License (https://www.pexels.com/license/)"`.
  A sidecar `assets/<scene_id>.provenance.json` records the full
  source record: asset id, rendition id, kind, query, orientation, media +
  page URLs, creator (+profile URL), license/attribution, retrieval time
  (UTC), width/height/duration, resolved path, sha256 and size. Unknown
  values stay empty — nothing is guessed.
- **Bounded network behaviour (§25)**: explicit connect/read timeouts,
  bounded retries with backoff for transient failures only (429/5xx/
  timeout/connection), immediate failure for invalid credentials
  (401/403 are permanent), and a download-size ceiling.
- **Health**: truthful and cheap — missing key ⇒ unavailable with an
  actionable message; no network call is made from `health()`.

## Configuration (all optional; see `.env.example`)

```text
AYCE_PEXELS_API_KEY=<pexels api key>              # enables the provider
AYCE_PEXELS_ORIENTATION=landscape                 # landscape|portrait|square
AYCE_PEXELS_MIN_VIDEO_DURATION_S=4.0              # video scenes only
AYCE_PEXELS_CACHE_DIR=data/assets_cache/pexels    # optional override
```

## Verification

- Mocked-transport unit tests run in the normal suite (`tests/unit/
  test_visual_pexels.py`) — no API key and no live calls are required.
- Live verification needs a real `PEXELS_API_KEY`; without it the provider
  reports itself unavailable instead of fabricating assets.