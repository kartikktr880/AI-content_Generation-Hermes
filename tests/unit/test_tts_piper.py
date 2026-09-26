"""P7 PiperNarrationProvider tests.

The process boundary is MOCKED everywhere except the opt-in live test —
the normal suite never requires Piper to be installed. Covers: truthful
health reporting, safe subprocess argv, failure modes, truthful WAV
duration/sha256, license provenance, length-scale handling, opt-in
provider selection, and a mocked-integration flow proving timeline +
captions consume the REAL synthesized duration.
"""

import hashlib
import io
import os
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from ayce.artifacts import ArtifactKind, ArtifactRegistry
from ayce.asset_resolution import FileBackedAssetProvider, run_asset_resolution_stage
from ayce.captions import run_captions_stage
from ayce.config import Config, ConfigError
from ayce.ids import new_run_id
from ayce.narration_audio import (
    AudioProvider,
    FileBackedNarrationProvider,
    NarrationAudioError,
    run_narration_audio_stage,
)
from ayce.scene_contract import Narration
from ayce.script_to_scene import load_script_input, run_script_to_scene_stage
from ayce.state import RunState
from ayce.timeline import TimelineElementKind, run_timeline_stage
from ayce.tts_piper import PiperNarrationProvider, select_narration_provider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# ---- helpers ----------------------------------------------------------------------


def wav_bytes(seconds: float, rate: int = 8000) -> bytes:
    """Build a valid PCM16 mono WAV of digital silence, exactly `seconds` long."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def make_provider(tmp_path: Path, **config_overrides) -> PiperNarrationProvider:
    """Configured provider pointing at a dummy model file in tmp_path."""
    model = tmp_path / "fake-voice-medium.onnx"
    model.write_bytes(b"dummy onnx bytes")
    return PiperNarrationProvider(Config(
        piper_python=sys.executable,  # resolvable; the process boundary is mocked
        piper_model=str(model),
        **config_overrides,
    ))


def install_fake_piper(monkeypatch, *, wav: bytes | None, returncode: int = 0,
                       stderr: bytes = b"", probe_returncode: int = 0,
                       side_effect=None):
    """Replace ayce.tts_piper.subprocess.run with a controllable fake."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        if side_effect is not None:
            raise side_effect
        if "--help" in argv:
            return subprocess.CompletedProcess(argv, probe_returncode, b"", stderr)
        if wav is not None and "-f" in argv:
            Path(argv[argv.index("-f") + 1]).write_bytes(wav)
        return subprocess.CompletedProcess(argv, returncode, b"", stderr)

    monkeypatch.setattr("ayce.tts_piper.subprocess.run", fake_run)
    return calls


def narration() -> Narration:
    return Narration(text="In 1853, the first passenger train left Bombay for Thane.")


# ---- configuration parsing ---------------------------------------------------------


def test_piper_config_from_env():
    config = Config.from_env(env={
        "AYCE_PIPER_PYTHON": "C:/piper/python.exe",
        "AYCE_PIPER_MODEL": "C:/voices/en_US-amy-medium.onnx",
        "AYCE_PIPER_CONFIG": "C:/voices/en_US-amy-medium.onnx.json",
        "AYCE_PIPER_LENGTH_SCALE": "0.9",
        "AYCE_PIPER_LICENSE": "CC-BY-4.0",
    })
    assert config.piper_python == "C:/piper/python.exe"
    assert config.piper_model == "C:/voices/en_US-amy-medium.onnx"
    assert config.piper_config == "C:/voices/en_US-amy-medium.onnx.json"
    assert config.piper_length_scale == 0.9
    assert config.piper_license == "CC-BY-4.0"


def test_piper_config_defaults_are_unset():
    config = Config.from_env(env={})
    assert config.piper_python is None
    assert config.piper_model is None
    assert config.piper_config is None
    assert config.piper_length_scale is None
    assert config.piper_license is None


def test_invalid_length_scale_rejected():
    with pytest.raises(ConfigError, match="PIPER_LENGTH_SCALE"):
        Config.from_env(env={"AYCE_PIPER_LENGTH_SCALE": "not-a-number"})
    with pytest.raises(ConfigError, match="must be > 0"):
        Config.from_env(env={"AYCE_PIPER_LENGTH_SCALE": "-1"})


# ---- truthful health ----------------------------------------------------------------


def test_unconfigured_provider_reports_unavailable():
    health = PiperNarrationProvider(Config()).health()
    assert health.available is False
    assert "AYCE_PIPER_PYTHON" in health.detail


def test_missing_python_executable_unhealthy(tmp_path):
    model = tmp_path / "v.onnx"
    model.write_bytes(b"x")
    provider = PiperNarrationProvider(Config(
        piper_python="no-such-python-xyz", piper_model=str(model)
    ))
    health = provider.health()
    assert health.available is False
    assert "not found" in health.detail


def test_missing_model_unhealthy(tmp_path):
    provider = PiperNarrationProvider(Config(
        piper_python=sys.executable, piper_model=str(tmp_path / "missing.onnx")
    ))
    health = provider.health()
    assert health.available is False
    assert "piper model not found" in health.detail


def test_missing_model_config_unhealthy(tmp_path):
    model = tmp_path / "v.onnx"
    model.write_bytes(b"x")
    provider = PiperNarrationProvider(Config(
        piper_python=sys.executable, piper_model=str(model),
        piper_config=str(tmp_path / "missing.onnx.json"),
    ))
    health = provider.health()
    assert health.available is False
    assert "piper model config not found" in health.detail


def test_healthy_configured_provider(tmp_path, monkeypatch):
    install_fake_piper(monkeypatch, wav=None, probe_returncode=0)
    provider = make_provider(tmp_path)
    health = provider.health()
    assert health.available is True
    assert "piper ready" in health.detail


def test_unusable_piper_module_reported_truthfully(tmp_path, monkeypatch):
    install_fake_piper(
        monkeypatch, wav=None, probe_returncode=1,
        stderr=b"No module named piper\n",
    )
    provider = make_provider(tmp_path)
    health = provider.health()
    assert health.available is False
    assert "exit 1" in health.detail
    assert "No module named piper" in health.detail


# ---- synthesis: safe subprocess + truthful output -----------------------------------


def test_synthesis_uses_safe_explicit_argv_list(tmp_path, monkeypatch):
    calls = install_fake_piper(monkeypatch, wav=wav_bytes(0.5))
    provider = make_provider(tmp_path)

    provider.resolve_narration(narration(), "scene-001", run_dir=tmp_path / "run")

    assert len(calls) == 1
    argv, kwargs = calls[0]["argv"], calls[0]["kwargs"]
    # explicit argv list — the process boundary, never a shell string
    assert isinstance(argv, list)
    assert argv[0] == sys.executable
    assert argv[1:4] == ["-m", "piper", "-m"]
    assert argv[4].endswith("fake-voice-medium.onnx")
    assert "-i" in argv and "-f" in argv
    # shell must never be used; bounded timeout; output captured
    assert kwargs.get("shell", False) is False
    assert kwargs.get("timeout") == 300
    assert kwargs.get("capture_output") is True
    # the temp narration-input file is removed after synthesis
    run_dir = tmp_path / "run"
    leftovers = [p.name for p in (run_dir / "audio").iterdir() if p.suffix == ".txt"]
    assert leftovers == []


def test_synthesis_passes_narration_text_verbatim(tmp_path, monkeypatch):
    captured = {}

    def spying_run(argv, **kwargs):
        if "-i" in argv:
            captured["text"] = Path(argv[argv.index("-i") + 1]).read_text(encoding="utf-8")
            Path(argv[argv.index("-f") + 1]).write_bytes(wav_bytes(0.5))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("ayce.tts_piper.subprocess.run", spying_run)
    provider = make_provider(tmp_path)
    provider.resolve_narration(narration(), "scene-001", run_dir=tmp_path / "run")
    assert captured["text"] == narration().text


def test_nonzero_piper_exit_fails_with_stderr(tmp_path, monkeypatch):
    install_fake_piper(monkeypatch, wav=None, returncode=2, stderr=b"model load failed\n")
    provider = make_provider(tmp_path)
    with pytest.raises(NarrationAudioError, match="exited 2"):
        provider.resolve_narration(narration(), "scene-001", run_dir=tmp_path / "run")


def test_piper_timeout_fails(tmp_path, monkeypatch):
    install_fake_piper(
        monkeypatch, wav=None,
        side_effect=subprocess.TimeoutExpired(cmd="piper", timeout=300),
    )
    provider = make_provider(tmp_path)
    with pytest.raises(NarrationAudioError, match="timed out"):
        provider.resolve_narration(narration(), "scene-001", run_dir=tmp_path / "run")


def test_missing_output_wav_fails(tmp_path, monkeypatch):
    install_fake_piper(monkeypatch, wav=None, returncode=0)
    provider = make_provider(tmp_path)
    with pytest.raises(NarrationAudioError, match="produced no output"):
        provider.resolve_narration(narration(), "scene-001", run_dir=tmp_path / "run")


def test_invalid_wav_output_fails(tmp_path, monkeypatch):
    install_fake_piper(monkeypatch, wav=b"this is not a wav file")
    provider = make_provider(tmp_path)
    with pytest.raises(NarrationAudioError, match="not a valid"):
        provider.resolve_narration(narration(), "scene-001", run_dir=tmp_path / "run")


def test_truthful_duration_and_sha256(tmp_path, monkeypatch):
    payload = wav_bytes(0.75)
    install_fake_piper(monkeypatch, wav=payload)
    provider = make_provider(tmp_path)

    result = provider.resolve_narration(narration(), "scene-001", run_dir=tmp_path / "run")

    assert result.duration_seconds == 0.75  # from the real WAV header
    assert result.format == "wav"
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.path == "audio/scene-001.wav"
    assert (tmp_path / "run" / "audio" / "scene-001.wav").is_file()


def test_license_provenance_configured_and_unset(tmp_path, monkeypatch):
    install_fake_piper(monkeypatch, wav=wav_bytes(0.5))

    licensed = make_provider(tmp_path, piper_license="CC-BY-4.0")
    result = licensed.resolve_narration(narration(), "scene-001", run_dir=tmp_path / "run1")
    assert result.provenance.provider == "piper-tts"
    assert result.provenance.source == "piper_tts_local"
    assert result.provenance.source_ref == "fake-voice-medium.onnx"
    assert result.provenance.license == "CC-BY-4.0"

    unlicensed = make_provider(tmp_path)  # no license configured → never guessed
    result = unlicensed.resolve_narration(narration(), "scene-001", run_dir=tmp_path / "run2")
    assert result.provenance.license is None


def test_length_scale_argument_handling(tmp_path, monkeypatch):
    calls = install_fake_piper(monkeypatch, wav=wav_bytes(0.5))
    provider = make_provider(tmp_path, piper_length_scale=0.9)
    provider.resolve_narration(narration(), "scene-001", run_dir=tmp_path / "run")
    argv = calls[-1]["argv"]
    assert argv[argv.index("--length-scale") + 1] == "0.9"

    calls_default = install_fake_piper(monkeypatch, wav=wav_bytes(0.5))
    provider = make_provider(tmp_path)  # no length scale → Piper's own default
    provider.resolve_narration(narration(), "scene-001", run_dir=tmp_path / "run")
    assert "--length-scale" not in calls_default[-1]["argv"]


def test_unconfigured_synthesis_fails_truthfully(tmp_path):
    provider = PiperNarrationProvider(Config())
    with pytest.raises(NarrationAudioError, match="not configured"):
        provider.resolve_narration(narration(), "scene-001", run_dir=tmp_path / "run")


def test_provider_satisfies_audio_provider_contract(tmp_path):
    assert isinstance(make_provider(tmp_path), AudioProvider)
    assert PiperNarrationProvider.name == "piper-tts"


# ---- opt-in provider selection ------------------------------------------------------


def test_selection_defaults_to_file_backed_provider():
    selected = select_narration_provider(Config.from_env(env={}))
    assert isinstance(selected, FileBackedNarrationProvider)
    assert not isinstance(selected, PiperNarrationProvider)


def test_selection_honors_narration_dir_for_default_provider():
    selected = select_narration_provider(
        Config.from_env(env={}), narration_dir="some/fixtures"
    )
    assert isinstance(selected, FileBackedNarrationProvider)
    assert selected.fixture_dir == Path("some/fixtures")


def test_piper_selected_only_when_explicitly_configured():
    selected = select_narration_provider(Config(piper_model="C:/voices/v.onnx"))
    assert isinstance(selected, PiperNarrationProvider)
    # piper python configured alone is NOT enough — the model enables Piper
    selected = select_narration_provider(Config(piper_python="python.exe"))
    assert isinstance(selected, FileBackedNarrationProvider)


# ---- mocked integration: real stages consume the REAL synthesized duration ----------


def full_upstream_chain(tmp_path, provider):
    """P1-B → P2 → narration(with the given provider) → P4 → P6.5 captions."""
    run_id = new_run_id()
    run_dir = tmp_path / "run"
    registry = ArtifactRegistry(run_dir, run_id)
    run = RunState(run_id=run_id, job_id=f"job-{run_id}")
    config = Config.from_env(env={})

    scene_result = run_script_to_scene_stage(
        load_script_input(FIXTURES / "script_to_scene" / "documentary.json"),
        run, registry,
    )
    assert scene_result.ok, scene_result.error
    asset_result = run_asset_resolution_stage(
        scene_result.manifest, run, registry,
        provider=FileBackedAssetProvider(config),
    )
    assert asset_result.ok, asset_result.error
    narration_result = run_narration_audio_stage(
        scene_result.manifest, run, registry, provider=provider,
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
        scene_result.manifest, timeline_result.manifest,
        narration_result.manifest, run, registry,
    )
    assert captions_result.ok, captions_result.error
    return timeline_result.manifest, captions_result.manifest, narration_result.manifest


def test_piper_duration_flows_through_timeline_and_captions(tmp_path, monkeypatch):
    """The REAL synthesized duration (0.6s per scene) drives the timeline
    narration elements AND the P6.5 caption intervals — with ZERO changes
    to timeline or captions code."""
    install_fake_piper(monkeypatch, wav=wav_bytes(0.6))
    provider = make_provider(tmp_path, piper_license="CC-BY-4.0")

    timeline, captions, narration_manifest = full_upstream_chain(tmp_path, provider)

    # the narration manifest records the real provider + real durations
    assert narration_manifest.provider == "piper-tts"
    for record in narration_manifest.narration_audio:
        assert record.duration_seconds == 0.6
        assert record.provenance.license == "CC-BY-4.0"

    # timeline narration elements use the actual audio duration
    for scene in timeline.scenes:
        element = next(
            e for e in scene.elements if e.kind is TimelineElementKind.NARRATION
        )
        assert element.duration_seconds == 0.6

    # P6.5 captions inherit the actual narration timing, unchanged
    for cue in captions.cues:
        assert cue.end_seconds == pytest.approx(cue.start_seconds + 0.6)


def test_unhealthy_piper_fails_the_narration_stage(tmp_path):
    """Configured-but-broken Piper → stage fails truthfully, no artifact,
    no silent fallback to the fixture provider."""
    run_id = new_run_id()
    run_dir = tmp_path / "run"
    registry = ArtifactRegistry(run_dir, run_id)
    run = RunState(run_id=run_id, job_id=f"job-{run_id}")

    scene_result = run_script_to_scene_stage(
        load_script_input(FIXTURES / "script_to_scene" / "documentary.json"),
        run, registry,
    )
    assert scene_result.ok

    # provider configured with a MISSING model → unhealthy → stage fails
    provider = PiperNarrationProvider(Config(
        piper_python="fake-python-exe",
        piper_model=str(tmp_path / "missing.onnx"),
    ))
    result = run_narration_audio_stage(scene_result.manifest, run, registry, provider=provider)

    assert result.ok is False
    assert "unhealthy" in result.error
    assert run.stage("narration_audio").status.value == "failed"
    assert ArtifactKind.AUDIO not in [a.kind for a in registry.all()]


# ---- opt-in live test (skipped unless Piper is configured via AYCE_* env) ----------


def _piper_live_configured() -> bool:
    return bool(os.environ.get("AYCE_PIPER_PYTHON")) and bool(os.environ.get("AYCE_PIPER_MODEL"))


@pytest.mark.live
@pytest.mark.skipif(
    not _piper_live_configured(),
    reason="AYCE_PIPER_PYTHON / AYCE_PIPER_MODEL not configured (opt-in live test)",
)
def test_live_piper_synthesis(tmp_path):
    """Real synthesis against the configured Piper environment. Never runs
    in the normal suite; never downloads a model."""
    config = Config.from_env()  # real environment
    provider = PiperNarrationProvider(config)

    health = provider.health()
    assert health.available, health.detail

    result = provider.resolve_narration(
        Narration(text="Live Piper verification."), "scene-live",
        run_dir=tmp_path / "run",
    )
    wav_path = tmp_path / "run" / result.path
    assert wav_path.is_file()
    assert result.duration_seconds is not None and result.duration_seconds > 0
    assert result.sha256 == hashlib.sha256(wav_path.read_bytes()).hexdigest()
    assert result.provenance.provider == "piper-tts"
    assert result.provenance.license == config.piper_license  # truthful, possibly None

