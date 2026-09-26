# Stage 2 — Research & Content Intelligence (First Vertical Slice)

Evidence record. Starting commit: `a84c355a1860dae99b9e9b0782af6865ad99a1ba`
(branch `master`; pre-existing uncommitted work in the tree was left untouched).

## Existing capabilities found (inspection before editing)

- **No** prior research / yt-dlp / transcript / embedding / clustering module
  existed in `src/ayce/` (searched the whole repo; only doc references).
- `yt-dlp` was already installed as an external CLI on the machine
  (`C:\Python314\Scripts\yt-dlp.EXE`, version `2026.08.19`) — the quota-free
  ingestion path. The project already has the external-CLI convention
  (Piper in `tts_piper.py`, FFmpeg in `render.py`): subprocess with an
  explicit argv list, never a shell, never imported.
- `mcp-ayce-director/` (H4): one write tool `trigger_golden_path`, fixed
  argv, no shell, request ledger with atomic writes, stub-runner test seam.
- Artifact/ID conventions: `src/ayce/ids.py`
  (`run-<utcstamp>-<token>`), pydantic V2 strict contracts
  (`extra="forbid"`), atomic writes (`tmp + os.replace`).
- Dependencies: `pydantic` only. **No new dependencies were added.**

## New components

| Path | Role |
|---|---|
| `src/ayce/research/models.py` | Four-tier Research Artifact contract (pydantic V2, `extra="forbid"`); `compact()` bounded Hermes envelope |
| `src/ayce/research/ingestion.py` | yt-dlp subprocess ingestion: `ytsearchN:` discovery, `@channel/videos` flat playlists, per-video metadata, normalization to `VideoRecord`, rate-limit detection |
| `src/ayce/research/transcripts.py` | Caption fetch (`--write-subs --write-auto-subs --sub-format json3/vtt`), json3+VTT parsers, 45s opening-hook extraction, deterministic no-caption mode |
| `src/ayce/research/analytics.py` | `channel_median_views`, `outlier_multiplier` (views/median, 2dp), `velocity_proxy` (views/day, labeled temporal proxy); edge cases return `(None, reason)` |
| `src/ayce/research/clustering.py` | Local clustering: optional FastEmbed (ONNX) when installed, else deterministic lexical-cosine vectors + pure-Python DBSCAN; explicit capability statuses |
| `src/ayce/research/worker.py` | Orchestrator: request validation → ingestion → hooks (bounded) → analytics → clustering → validated artifact → persist full JSON to `data/research/<research_id>.json` → compact envelope |
| `src/ayce/research/__main__.py` | stdin→stdout JSON worker CLI; classified exit codes (2 invalid_request, 3 source_unavailable, 4 rate_limited, 5 extraction_failed, 6 analysis_failed, 7 artifact_validation_failed, 8 internal_error); forced UTF-8 stdio |
| `mcp-ayce-director/server.py` (+tool) | Second tool `execute_research_slice` on the SAME server: validation, digest-keyed research ledger (`research_requests.json`, idempotent reuse), fixed argv `<pinned python> -m ayce.research --json`, request JSON on stdin, UTF-8 child pipe, timeout (`AYCE_RESEARCH_TIMEOUT_S`, default 300s) |

## Research MCP

Tool: `execute_research_slice(objective, niche="", query="",
target_channels=[], max_outliers=5)` on the existing `ayce-director`
server. `trigger_golden_path` remains the ONLY write tool; the H4
ledger/locks are not shared. Caller input never enters argv — it
travels as validated JSON on the worker's stdin.

## Research Artifact

`schema_version 1.0`; four epistemic tiers enforced in canonical order
(`OBSERVED_FACT, DERIVED_METRIC, MODEL_INFERENCE, CREATIVE_HYPOTHESIS`);
per-candidate tier lists, metrics, `cluster_id`, confidence, and
per-candidate + artifact-level provenance; `compact(max_outliers)`
produces the ≤2,500-token envelope (measured: ~3–5 KB JSON for 5
outliers). Full artifact persisted locally; never shipped to Hermes.

## Real execution (bounded: 1 query, ≤6 videos, 2 outliers)

1. Direct worker run (`python -m ayce.research --json`, stdin JSON):
   `SUCCESS`, 3 candidates, real hooks extracted (e.g. video `ZLYKh5HOjZw`,
   "Business Training Media", 12,550 views, velocity 4.4/day).
2. Full MCP roundtrip (`mcp-ayce-director/research_roundtrip.py`, real
   stdio transport → real subprocess worker → real yt-dlp):
   `ok: true`, `research_id res-20260920T090318Z-e8f295ac6ec8`, 6
   candidates, clustering `lexical_cosine_dbscan` (1 cluster),
   PARTIAL with one truthful warning: a single subtitle fetch hit
   `HTTP Error 429` and degraded that candidate only (no caption text was
   fabricated). A repeat invocation returned the recorded slice with
   `"duplicate": true` — idempotent reuse across separate server
   processes. Evidence: `mcp-ayce-director/research_roundtrip_output.json`,
   `data/research/res-*.json`, `mcp-ayce-director/research_requests.json`.
3. Truthful degradation observed: candidates from different channels give
   1-sample baselines → `outlier_multiplier: null` +
   `insufficient_baseline` (never a guessed number).

## Tests (exact commands)

- `python -m pytest -q` → all pass (1 pre-existing opt-in skip);
  includes 44 new research unit tests
  (`tests/unit/test_research_{analytics,clustering,transcripts,ingestion,artifact,worker}.py`)
- `mcp-ayce-director\.venv\Scripts\python -m pytest
  mcp-ayce-director\test_server.py mcp-ayce-director\test_research_tool.py -q`
  (PYTHONPATH=src) → 94 pass (20 new research boundary tests)
- `mcp-ayce-readonly\.venv\Scripts\python -m pytest
  mcp-ayce-readonly\test_server.py -q` (PYTHONPATH=src) → pass

## Dependency changes

None. `pyproject.toml` untouched (`pydantic>=2.12,<3` only). FastEmbed is
an optional runtime enhancement (auto-detected, degrades explicitly).

## Regression / protected surfaces

- H4 golden-path boundary: validation, ledger, single-flight, execution
  semantics untouched (only two test *expectations* about the tool
  surface were updated to acknowledge the second tool; the write
  boundary guarantees are preserved and re-asserted).
- H5/H5-B composition & idempotency: untouched (`pipeline.py`,
  `captions.py`, `state.py`, `artifacts.py` unmodified by this stage).
- Read-only server: untouched, suite green.

## Known limitations

- `outlier_multiplier` needs ≥3 same-channel samples; mixed-channel
  searches yield `insufficient_baseline` (by design).
- Velocity is a labeled temporal proxy (publication age + current
  views), not real-time velocity.
- Clustering uses lexical vectors unless FastEmbed is installed; small
  title sets can produce noise (`cluster_id: null`).
- yt-dlp scrapes YouTube's public web endpoints: bot-checks/429s occur;
  handled as RATE_LIMITED / per-video degradation, never retried
  aggressively.
- Research worker is not wired into RunState/ArtifactRegistry (it is a
  capability beside the Golden Path, not a stage of it).

## Next smallest capability

Wire `execute_research_slice` output into Hermes-facing script ideation:
a deterministic "objective + top outliers → script brief" formatter
behind the existing Director MCP, before any trend engine or
multi-platform work.