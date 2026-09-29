# STAGE 8 — MOTION & COMPOSITING (MUST slice) — Hermes-operated, VERIFIED

**Task identification (research-report-first).** `Phase 0 Research/Stage 8 Motion
and Compositing Architecture Deep R___` — *"Programmable Motion, Timeline
Orchestration, and Media Compositing: System Architecture and Infrastructure
Blueprint for Stage 8"*. Its Executive Verdict selects a **Decoupled Hybrid
Rendering Pipeline** and states the reuse mandate explicitly:

> "an audit of the existing codebase confirms that legacy production components
> (specifically the P4 multiplexer, P4.5 filtergraph generator, and P5.5 audio
> ducking engine) already provide robust, operational media manipulation logic.
> These modules must be adapted and wrapped rather than retired, restricting
> custom engineering strictly to four thin architectural components."

**Selected candidates (explicitly named by the report: FFmpeg + libass).**

| Candidate | Report role | State on this machine | Action |
|---|---|---|---|
| FFmpeg (C-based binaries; xfade/zoompan/boxblur/overlay/scale/aresample/concat) | foundational media pipeline | **already installed** — Hermes-bundled FFmpeg 9.0.1 (`%LOCALAPPDATA%\hermes\tools\ffmpeg-9.0.1-win32-x64`) | **REUSED, not installed** |
| libass (via FFmpeg's `ass` filter) | MUST: "pre-timed SSA/ASS subtitle rasterization … via libass without spinning up browser runtimes" | build flag verified: `--enable-libass --enable-fontconfig --enable-libharfbuzz --enable-libfreetype --enable-libfribidi`; `ass`/`subtitles` filters present | **REUSED** |
| Remotion / libass-compiled kinetic typography | SHOULD (word bounce) / OPTIONAL (GL transitions) | not required by the MUST slice | **NOT implemented (out of scope)** |

No candidate was installed; the report's own "reuse and wrap" instruction was
followed instead.

## 1. Implementation (smallest correct MUST slice)

New `src/ayce/compositor.py` — the report's *Procedural FFmpeg Filtergraph
Generator* + libass typography — implemented by **subclassing the verified
`FFmpegRenderer`** (so executable resolution, the health gate, the
`RenderedOutput` contract and `ffprobe` validation are reused, never duplicated):

| MUST capability (Stage 8 matrix) | Implementation |
|---|---|
| multi-track frame-accurate composition | one composition graph per timeline; per-scene `fps`-normalized chains |
| aspect-ratio fitting + **background blur** | `scale=…:force_original_aspect_ratio=increase,crop`, downscaled `boxblur` plate + centred overlay of the aspect-fit foreground |
| **programmable Ken Burns** pan/zoom | `zoompan` with a programmed zoom expression, alternating push-in/pull-out per scene |
| **non-linear easing (cubic bezier)** | smooth-step `3t²−2t³` evaluated per output frame (`pow(min(on/N,1),2)`) |
| **standard cuts, fades, dissolves, wipes** | `concat` for `cut`; `xfade` for fade/dissolve/wipe/slide/smooth/circle — **duration-preserving** (per-scene tail padding absorbs the overlap, then `trim` to the declared total) |
| **pre-timed SSA/ASS rasterization via libass** | deterministic `captions.ass` generated from the verified `captions.json` (`PlayResX/Y` = canvas, one `Dialogue:` per cue) applied with `ass=filename='…'` |
| **48 kHz audio multiplex** | per-scene `aresample=48000` + `aformat=fltp:stereo`, exact scene intervals, `concat` |

Two verified FFmpeg/Windows findings are recorded in code comments: (a) `ass`
mis-parses an unquoted `C:/…` value ("No option name near '/Users/…'") — the
working form is `filename='C\:/…'`; (b) `boxblur` must use **named** options
because the positional second value is `luma_power`, not the chroma radius
(`chroma_radius` must be < 8).

**Contract preservation:** `AYCE_RENDERER` defaults to `ffmpeg-smoke`, so the
verified renderer is still what every existing run uses; the compositor is
opt-in. `pipeline.py` now resolves the renderer through
`compositor.build_renderer(config)` (an explicitly injected renderer still wins).

New configuration (all optional, validated in `config.py`): `AYCE_RENDERER`,
`AYCE_CANVAS_WIDTH` (1920), `AYCE_CANVAS_HEIGHT` (1080), `AYCE_FRAME_RATE` (30),
`AYCE_TRANSITION` (dissolve), `AYCE_TRANSITION_SECONDS` (0.5), `AYCE_KENBURNS`,
`AYCE_KENBURNS_STRENGTH` (0.08), `AYCE_BURN_CAPTIONS`.

## 2. Hermes runtime execution (primary evidence)

**Session A — Hermes executes the composited production** (one-shot
`gemini-3.7-flash`, tool used: `terminal` with `timeout=600`; prompt
`mcp-ayce-director/h11a_compositor_execute_prompt.txt`):

```text
Set-Location 'C:\Users\Administrator\AI-Content-Generation';
$env:PYTHONPATH='src'; $env:AYCE_RENDERER='ffmpeg-compositor';
$env:Path='%LOCALAPPDATA%\hermes\tools\ffmpeg-9.0.1-win32-x64\bin;'+$env:Path;
.\.venv\Scripts\python.exe -m ayce run tests\fixtures\script_to_scene\documentary.json `
  --assets-dir tests\fixtures\asset_provider --narration-dir tests\fixtures\narration_fixtures --json
```

Hermes-observed result (verbatim):

```json
{"ok": true, "run_id": "run-20260929T101927Z-e32756f8d6a5",
 "job_id": "job-20260929T101927Z-110b7f534ad6",
 "production_id": "job-20260917T131500Z-1a2b3c4d5e6f",
 "stages": [ …7 stages, all ok: true… ], "qa_verdict": "PASS",
 "failed_stage": null, "error": null}
```

**Session B — Hermes verifies the result through its own read-only MCP surface**
(`h11b_compositor_verify_prompt.txt`, `mcp__ayce_readonly__get_run_state` +
`get_run_artifacts`):

| Reported by Hermes | Value |
|---|---|
| stages | 7/7 `succeeded`, attempts 1 |
| `artifact_count` | 7 |
| `rendered_video.renderer` | **`ffmpeg-compositor`** |
| `rendered_video.width × height` | **1920 × 1080** |
| `rendered_video.duration_seconds` | 34.0 |
| `rendered_video.sha256` | `f1a054143431b04a40af64b05027d08bf532db00591d1fc8e343a522456989b5` |
| `qa_report` | `verdict PASS`, `total_checks 16`, `failed_checks 0`, `render_sha256` equal to the render sha |

## 3. Independent verification (physical media + artifact inspection)

`ffprobe` on `data/runs/run-20260929T101927Z-e32756f8d6a5/render/job-20260917T131500Z-1a2b3c4d5e6f.mp4`:

```text
video: h264  1920x1080  r_frame_rate=30/1
audio: aac   sample_rate=48000  channels=2
format: duration=34.000000  size=467343
```

`render/captions.ass` present in the run directory with `PlayResX: 1920` and one
`Dialogue:` record per caption cue (pre-timed typography actually rasterized by
libass during the encode). The composited master is additionally
**byte-deterministic** for identical inputs: the earlier local validation run
(`run-20260929T101400Z-d771fe469a25`) produced the identical sha256.

## 4. Focused regression (directly affected components only)

`pytest tests/unit/test_compositor.py tests/unit/test_render.py
test_production_render.py test_captions.py test_pipeline.py test_config.py` →
**62 passed, 0 failed** (includes the Stage 8 suite: renderer selection default
unchanged, ASS determinism + injection sanitisation, timestamps, filtergraph
motion/transition/typography/48 kHz assertions, cut-vs-dissolve, a real
composited render, and fail-closed caption burning).

No project-wide suite was run; no unrelated module was touched.

## 5. Files changed

| Path | Change |
|---|---|
| `src/ayce/compositor.py` | NEW — Stage 8 procedural filtergraph generator, libass typography, `CompositorRenderer`, `build_renderer` |
| `src/ayce/config.py` | +9 optional compositor keys (defaults preserve current behaviour) |
| `src/ayce/pipeline.py` | renderer resolved via `build_renderer(config)` (injected renderer still wins) |
| `tests/unit/test_compositor.py` | NEW — 11 focused tests |
| `mcp-ayce-director/h11a_compositor_execute_prompt.txt`, `h11b_compositor_verify_prompt.txt` | NEW — Hermes session prompts (the combined `h11_compositor_prompt.txt` is kept as a record) |
| `docs/phase0/13_stage8_compositor_must_slice.md`, `docs/phase0/00_phase0_ledger.md` | NEW this record + ledger §12 |
| Hermes configuration, MCP configs, cron, `pyproject.toml`, `.env`, Task 0/1 evidence | **untouched** |

## 6. Acceptance

| # | Criterion | Verdict |
|---|---|---|
| A | Next approved task identified from the research report (not invented) | PASS — Stage 8 blueprint, MUST matrix |
| B | Only the report's explicitly selected candidates used | PASS — FFmpeg + libass reused; Remotion (SHOULD/OPTIONAL) excluded |
| C | No installation performed (candidates already present) | PASS |
| D | Implemented by wrapping/reusing the verified renderer, not replacing it | PASS — subclass; `ffmpeg-smoke` remains default |
| E | Executed through Hermes | PASS — Hermes ran the real production command (session A) |
| F | Verified through Hermes | PASS — read-only MCP observation (session B) |
| G | Real media, real behavior | PASS — 1920×1080 h264/aac 48 kHz, 34.000 s, QA PASS 16/0 |
| H | No regression of verified components | PASS — 62 focused tests |

**STAGE 8 (MOTION & COMPOSITING, MUST SLICE) = VERIFIED.**

PUBLISH remains **PENDING — `403 authenticatedUserAccountSuspended`** (external
account blocker; untouched by this task).
