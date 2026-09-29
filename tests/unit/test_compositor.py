"""Stage 8 compositor tests (MUST slice).

Covers: configuration-driven renderer selection (default unchanged),
deterministic libass/ASS typography generation, filtergraph construction
(motion / transitions / typography), fail-closed behaviour when burning
captions without the verified captions artifact, and a REAL composited
production render validated with ffprobe.

Only the Stage 8 component is exercised here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ayce.asset_resolution import FileBackedAssetProvider, run_asset_resolution_stage
from ayce.artifacts import ArtifactRegistry
from ayce.captions import run_captions_stage
from ayce.compositor import (
    ASS_FILENAME,
    COMPOSITOR_RENDERER_NAME,
    CompositorOptions,
    CompositorRenderer,
    build_ass_document,
    build_renderer,
)
from ayce.config import Config
from ayce.ids import new_job_id, new_run_id
from ayce.narration_audio import FileBackedNarrationProvider, run_narration_audio_stage
from ayce.render import FFmpegRenderer, RenderError
from ayce.script_to_scene import load_script_input, run_script_to_scene_stage
from ayce.state import RunState
from ayce.timeline import run_timeline_stage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

#: Small canvas keeps the real-render test fast; production defaults are 1080p.
COMPOSITOR_ENV = {
    "AYCE_RENDERER": COMPOSITOR_RENDERER_NAME,
    "AYCE_CANVAS_WIDTH": "320",
    "AYCE_CANVAS_HEIGHT": "180",
    "AYCE_FRAME_RATE": "12",
    "AYCE_TRANSITION_SECONDS": "0.4",
}


def compositor_config(**overrides: str) -> Config:
    env = dict(COMPOSITOR_ENV)
    env.update(overrides)
    return Config.from_env(env=env)


def build_run(tmp_path: Path):
    """Execute the real P1-B -> P2 -> P3 -> P4 -> P6.5 chain in a temp run dir."""
    run_id = new_run_id()
    run_dir = tmp_path / "run"
    registry = ArtifactRegistry(run_dir, run_id)
    run = RunState(run_id=run_id, job_id=new_job_id())
    scene_result = run_script_to_scene_stage(
        load_script_input(FIXTURES / "script_to_scene" / "documentary.json"),
        run,
        registry,
    )
    assert scene_result.ok, scene_result.error
    asset_result = run_asset_resolution_stage(
        scene_result.manifest,
        run,
        registry,
        provider=FileBackedAssetProvider(Config.from_env(env={})),
    )
    assert asset_result.ok, asset_result.error
    narration_result = run_narration_audio_stage(
        scene_result.manifest,
        run,
        registry,
        provider=FileBackedNarrationProvider(Config.from_env(env={})),
    )
    assert narration_result.ok, narration_result.error
    timeline_result = run_timeline_stage(
        scene_manifest=scene_result.manifest,
        asset_manifest=asset_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert timeline_result.ok, timeline_result.error
    captions_result = run_captions_stage(
        scene_manifest=scene_result.manifest,
        timeline_manifest=timeline_result.manifest,
        narration_manifest=narration_result.manifest,
        run_state=run,
        artifact_registry=registry,
    )
    assert captions_result.ok, captions_result.error
    return timeline_result.manifest, captions_result.manifest, run_dir


# ---- configuration / selection ------------------------------------------------


def test_default_configuration_keeps_verified_renderer():
    """The verified smoke renderer stays the default (no silent behaviour change)."""
    config = Config.from_env(env={})
    assert config.renderer == "ffmpeg-smoke"
    renderer = build_renderer(config)
    assert isinstance(renderer, FFmpegRenderer)
    assert not isinstance(renderer, CompositorRenderer)
    assert renderer.name == "ffmpeg-smoke"


def test_compositor_canvas_defaults_are_production_grade():
    config = Config.from_env(env={})
    assert (config.canvas_width, config.canvas_height, config.frame_rate) == (1920, 1080, 30)
    assert config.transition == "dissolve"
    assert config.kenburns is True
    assert config.burn_captions is True


def test_compositor_selected_by_configuration():
    renderer = build_renderer(compositor_config())
    assert isinstance(renderer, CompositorRenderer)
    assert renderer.name == COMPOSITOR_RENDERER_NAME
    assert renderer.options.canvas == "320x180"


def test_unknown_transition_is_rejected():
    with pytest.raises(RenderError):
        CompositorOptions.from_config(compositor_config(AYCE_TRANSITION="sparkle"))


# ---- libass / ASS typography --------------------------------------------------


def test_ass_document_is_deterministic_and_well_formed(tmp_path):
    _timeline, captions, _run_dir = build_run(tmp_path)
    first = build_ass_document(captions, width=320, height=180)
    second = build_ass_document(captions, width=320, height=180)
    assert first == second, "ASS generation must be byte-deterministic"
    assert "PlayResX: 320" in first
    assert "PlayResY: 180" in first
    assert first.count("Dialogue: 0,") == len(captions.cues)
    assert first.startswith("[Script Info]")


def test_ass_text_is_sanitized_against_override_injection(tmp_path):
    _timeline, captions, _run_dir = build_run(tmp_path)
    hostile = captions.cues[0].model_copy(
        update={"text": "line one\n{\\pos(0,0)}line two"}
    )
    tampered = captions.model_copy(update={"cues": (hostile,)})
    document = build_ass_document(tampered, width=1920, height=1080)
    dialogue = next(line for line in document.splitlines() if line.startswith("Dialogue: 0,"))
    assert "{" not in dialogue and "}" not in dialogue
    assert "\\N" in dialogue
    assert document.count("Dialogue: 0,") == 1


def test_ass_timestamps_use_centisecond_precision(tmp_path):
    _timeline, captions, _run_dir = build_run(tmp_path)
    document = build_ass_document(captions, width=1920, height=1080)
    dialogue = next(line for line in document.splitlines() if line.startswith("Dialogue: 0,"))
    fields = dialogue.split(",")
    assert fields[1] == "0:00:00.00"
    assert len(fields[2].split(".")[1]) == 2


# ---- filtergraph construction (the Stage 8 generator) -------------------------


def test_filtergraph_contains_motion_transition_and_typography(tmp_path):
    timeline, captions, run_dir = build_run(tmp_path)
    renderer = CompositorRenderer(compositor_config())
    ass_path = renderer._write_ass(run_dir, captions)
    graph = renderer._build_filter_complex(timeline, captions_ass=ass_path)
    assert "xfade=transition=fade" in graph
    assert "zoompan=z=" in graph
    assert "pow(" in graph, "non-linear easing must be programmed"
    assert "ass=" in graph
    assert "aresample=48000" in graph
    assert "tpad=stop_mode=clone" in graph
    assert "boxblur=" in graph, "aspect-fill background blur must be present"
    assert graph.count("xfade=") == len(timeline.scenes) - 1


def test_cut_transition_concatenates_without_xfade(tmp_path):
    timeline, captions, run_dir = build_run(tmp_path)
    renderer = CompositorRenderer(compositor_config(AYCE_TRANSITION="cut"))
    graph = renderer._build_filter_complex(timeline, captions_ass=None)
    assert "xfade=" not in graph
    assert "tpad=" not in graph
    assert f"concat=n={len(timeline.scenes)}:v=1:a=0" in graph
    assert "null[vout]" in graph


# ---- real composited render ---------------------------------------------------


def test_real_composited_production_render_is_canvas_sized(tmp_path):
    timeline, _captions, run_dir = build_run(tmp_path)
    renderer = CompositorRenderer(compositor_config())
    rendered = renderer.render_production(timeline, run_dir=run_dir)

    assert (renderer.options.width, renderer.options.height) == (320, 180)
    assert (rendered.width, rendered.height) == (320, 180)
    assert rendered.has_audio is True
    assert abs(rendered.duration_seconds - timeline.total_duration_seconds) <= 0.5 + (
        0.02 * timeline.total_duration_seconds
    )
    output = run_dir / rendered.path
    assert output.is_file() and output.stat().st_size > 0
    assert (run_dir / "render" / ASS_FILENAME).is_file()


def test_burning_captions_without_artifact_fails_closed(tmp_path):
    timeline, _captions, run_dir = build_run(tmp_path)
    (run_dir / "captions.json").unlink()
    renderer = CompositorRenderer(compositor_config())
    with pytest.raises(RenderError):
        renderer.render_production(timeline, run_dir=run_dir)
