"""P8 KokoroNarrationProvider tests.

The synthesis engine is replaced by an explicit TEST SEAM (a fake pipeline
factory) — the normal suite never downloads model weights and never needs
torch. Covers: missing runtime/config reporting, English and Hindi
configuration paths, espeak-ng verification for Hindi, WAV validity
(24 kHz, mono, PCM16), real duration, provenance, the content-addressed
synthesis cache, and the existing narration stage consuming the result.
"""

import builtins
import hashlib
import wave
from pathlib import Path

import pytest

from ayce.adapters import Adapter
from ayce.artifacts import ArtifactRegistry
from ayce.config import Config, ConfigError
from ayce.ids import new_job_id, new_run_id
from ayce.narration_audio import (
    FileBackedNarrationProvider,
    NarrationAudioError,
    run_narration_audio_stage,
)
from ayce.scene_contract import Narration, ProductionManifest, Scene, VisualIntent
from ayce.state import RunState
from ayce.tts_kokoro import (
    DEFAULT_REPO_ID,
    SAMPLE_RATE,
    SUPPORTED_LANG_CODES,
    KokoroNarrationProvider,
)
from ayce.tts_piper import PiperNarrationProvider, select_narration_provider

ENGLISH_VOICE = "af_heart"
HINDI_VOICE = "hf_alpha"


@pytest.fixture(autouse=True)
def isolate_engine_probe(monkeypatch):
    """Keep unit tests independent of the machine's native runtime.

    ``health()`` truthfully probes whether the engine's native libraries load
    on this machine (see ``_engine_import_probe``). Unit tests stub that probe
    so they neither require PyTorch to be loadable here nor pay its import
    cost; the probe's own behaviour is covered explicitly below and by the
    ``live`` smoke test.
    """
    from ayce import tts_kokoro

    if "result" in tts_kokoro._ENGINE_PROBE:
        monkeypatch.setitem(tts_kokoro._ENGINE_PROBE, "result", (True, "stubbed"))
    else:
        monkeypatch.setattr(tts_kokoro, "_ENGINE_PROBE", {"result": (True, "stubbed")})


def make_narration(text: str = "Welcome to the show.") -> Narration:
    return Narration(text=text)


class FakePipeline:
    """Stand-in for ``kokoro.KPipeline`` (records calls, yields shaped audio)."""

    def __init__(self, *, seconds: float = 0.5, chunks: int = 1, shape=None, empty=False):
        self.seconds = seconds
        self.chunks = chunks
        self.shape = shape
        self.empty = empty
        self.calls: list[dict] = []

    def __call__(self, text, voice=None, speed=1, split_pattern=None):
        self.calls.append(
            {"text": text, "voice": voice, "speed": speed, "split_pattern": split_pattern}
        )
        if self.empty:
            return
        np = pytest.importorskip("numpy")
        frames = int(SAMPLE_RATE * self.seconds)
        per_chunk = max(frames // self.chunks, 1)
        shape = self.shape if self.shape is not None else (per_chunk,)
        for _index in range(self.chunks):
            data = np.zeros(shape, dtype=np.float32)
            yield text, "phonemes", data


def make_provider(tmp_path: Path, pipeline: FakePipeline | None = None, **overrides):
    """Provider wired to the fake engine (no torch, no model download)."""
    engine = pipeline if pipeline is not None else FakePipeline()
    overrides.setdefault("kokoro_voice", ENGLISH_VOICE)
    config = Config(data_dir=tmp_path / "data", **overrides)
    provider = KokoroNarrationProvider(
        config,
        cache_dir=tmp_path / "cache",
        pipeline_factory=lambda lang_code: engine,
    )
    provider.engine = engine
    return provider


def wav_params(path: Path) -> tuple[int, int, int, float]:
    with wave.open(str(path), "rb") as reader:
        frames = reader.getnframes()
        rate = reader.getframerate()
        channels = reader.getnchannels()
        width = reader.getsampwidth()
    return rate, channels, width, round(frames / rate, 3)


# ---- configuration + truthful health ---------------------------------------


def test_unconfigured_provider_is_unavailable(tmp_path):
    provider = make_provider(tmp_path, kokoro_voice=None)
    health = provider.health()
    assert health.available is False
    assert "AYCE_KOKORO_VOICE" in health.detail
    with pytest.raises(NarrationAudioError, match="not configured"):
        provider.resolve_narration(make_narration(), "scene-001", run_dir=tmp_path / "run")


def test_missing_runtime_reports_actionable_install(tmp_path, monkeypatch):
    from ayce import tts_kokoro

    monkeypatch.setattr(tts_kokoro.importlib.util, "find_spec", lambda name: None)
    health = make_provider(tmp_path).health()
    assert health.available is False
    assert "kokoro" in health.detail and "torch" in health.detail
    assert "kokoro>=0.9.4" in health.detail


def test_load_pipeline_without_runtime_is_actionable(tmp_path, monkeypatch):
    from ayce import tts_kokoro

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "kokoro":
            raise ImportError("No module named 'kokoro'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    provider = tts_kokoro.KokoroNarrationProvider(Config(kokoro_voice=ENGLISH_VOICE))
    with pytest.raises(NarrationAudioError, match="kokoro>=0.9.4"):
        provider._load_pipeline("a")


def test_native_library_failure_is_reported_truthfully(tmp_path, monkeypatch):
    """A missing native runtime must never be reported as usable."""
    from ayce import tts_kokoro

    message = (
        "the Kokoro runtime is installed but its native libraries failed to load: "
        '[WinError 126] Error loading "c10.dll"... On Windows, PyTorch needs the '
        "Microsoft Visual C++ Redistributable (x64)"
    )
    monkeypatch.setitem(tts_kokoro._ENGINE_PROBE, "result", (False, message))
    health = make_provider(tmp_path).health()
    assert health.available is False
    assert "Visual C++ Redistributable" in health.detail


def test_load_pipeline_native_failure_is_actionable(tmp_path, monkeypatch):
    from ayce import tts_kokoro

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name in ("torch", "kokoro"):
            raise OSError(
                '[WinError 126] The specified module could not be found. Error '
                'loading "torch\\lib\\c10.dll" or one of its dependencies.'
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    provider = tts_kokoro.KokoroNarrationProvider(Config(kokoro_voice=ENGLISH_VOICE))
    with pytest.raises(NarrationAudioError) as excinfo:
        provider._load_pipeline("a")
    message = str(excinfo.value)
    assert "native libraries failed to load" in message
    assert "Visual C++ Redistributable" in message
    assert "does not install system software" in message


def test_unsupported_language_voice_is_refused(tmp_path):
    provider = make_provider(tmp_path, kokoro_voice="zf_xiaobei")
    health = provider.health()
    assert health.available is False
    assert set(SUPPORTED_LANG_CODES) == {"a", "b", "h"}
    with pytest.raises(NarrationAudioError, match="language code 'z'"):
        provider.resolve_narration(make_narration(), "scene-001", run_dir=tmp_path / "run")


def test_language_is_derived_from_the_voice(tmp_path):
    assert make_provider(tmp_path, kokoro_voice=ENGLISH_VOICE).lang_code == "a"
    assert make_provider(tmp_path, kokoro_voice="bm_george").lang_code == "b"
    assert make_provider(tmp_path, kokoro_voice=HINDI_VOICE).lang_code == "h"


def test_provider_configuration_is_validated():
    with pytest.raises(ConfigError, match="AYCE_KOKORO_SPEED"):
        Config.from_env(env={"AYCE_KOKORO_SPEED": "-1"})
    with pytest.raises(ConfigError, match="AYCE_KOKORO_SPEED"):
        Config.from_env(env={"AYCE_KOKORO_SPEED": "fast"})

    configured = Config.from_env(env={
        "AYCE_KOKORO_VOICE": "hf_alpha",
        "AYCE_KOKORO_SPEED": "0.9",
        "AYCE_KOKORO_LICENSE": "Apache-2.0",
    })
    assert configured.kokoro_voice == "hf_alpha"
    assert configured.kokoro_speed == 0.9
    assert configured.kokoro_license == "Apache-2.0"
    assert configured.kokoro_repo_id == DEFAULT_REPO_ID


# ---- English synthesis ------------------------------------------------------


def test_english_synthesis_produces_valid_wav(tmp_path):
    provider = make_provider(tmp_path)
    asset = provider.resolve_narration(
        make_narration("Hello from Kokoro."), "scene-001", run_dir=tmp_path / "run"
    )

    wav_path = tmp_path / "run" / "audio" / "scene-001.wav"
    rate, channels, width, duration = wav_params(wav_path)
    assert rate == SAMPLE_RATE == 24000
    assert channels == 1
    assert width == 2                      # 16-bit PCM
    assert duration == pytest.approx(0.5, abs=0.01)

    assert asset.scene_id == "scene-001"
    assert asset.path == "audio/scene-001.wav"
    assert asset.format == "wav"
    assert asset.duration_seconds == pytest.approx(0.5, abs=0.01)
    assert asset.sha256 == hashlib.sha256(wav_path.read_bytes()).hexdigest()
    assert asset.provenance.provider == "kokoro-82m"
    assert asset.provenance.source == "kokoro_local"
    assert asset.provenance.source_ref == f"{DEFAULT_REPO_ID}:{ENGLISH_VOICE}"
    assert asset.provenance.license is None   # never guessed when unconfigured

    call = provider.engine.calls[0]
    assert call["text"] == "Hello from Kokoro."   # verbatim
    assert call["voice"] == ENGLISH_VOICE
    assert call["speed"] == 1.0
    assert call["split_pattern"] == r"\n+"


def test_license_recorded_only_when_configured(tmp_path):
    provider = make_provider(tmp_path, kokoro_license="Apache-2.0")
    asset = provider.resolve_narration(make_narration(), "scene-002", run_dir=tmp_path / "run")
    assert asset.provenance.license == "Apache-2.0"


def test_multi_chunk_output_is_concatenated(tmp_path):
    provider = make_provider(tmp_path, FakePipeline(seconds=0.6, chunks=3))
    asset = provider.resolve_narration(
        make_narration("Line one.\nLine two.\nLine three."), "scene-003",
        run_dir=tmp_path / "run",
    )
    assert len(provider.engine.calls) == 1
    assert asset.duration_seconds == pytest.approx(0.6, abs=0.05)


def test_tensor_like_audio_is_normalized(tmp_path):
    """Kokoro may yield torch tensors; the provider accepts that shape."""

    class FakeTensor:
        def __init__(self, array):
            self._array = array

        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self._array

    class TensorPipeline(FakePipeline):
        def __call__(self, text, voice=None, speed=1, split_pattern=None):
            np = pytest.importorskip("numpy")
            self.calls.append({"voice": voice})
            yield text, "phonemes", FakeTensor(
                np.zeros((SAMPLE_RATE // 4,), dtype=np.float32)
            )

    provider = make_provider(tmp_path, TensorPipeline())
    asset = provider.resolve_narration(make_narration(), "scene-004", run_dir=tmp_path / "run")
    assert asset.duration_seconds == pytest.approx(0.25, abs=0.01)


def test_non_mono_audio_is_rejected(tmp_path):
    provider = make_provider(tmp_path, FakePipeline(shape=(2, 100)))
    with pytest.raises(NarrationAudioError, match="expected mono audio"):
        provider.resolve_narration(make_narration(), "scene-005", run_dir=tmp_path / "run")


def test_empty_synthesis_is_rejected(tmp_path):
    provider = make_provider(tmp_path, FakePipeline(empty=True))
    with pytest.raises(NarrationAudioError, match="produced no audio"):
        provider.resolve_narration(make_narration(), "scene-006", run_dir=tmp_path / "run")
    assert not (tmp_path / "run" / "audio" / "scene-006.wav").exists()


# ---- Hindi path (espeak-ng prerequisite) ------------------------------------


def test_hindi_requires_espeak_ng(tmp_path, monkeypatch):
    from ayce import tts_kokoro

    monkeypatch.setattr(
        tts_kokoro,
        "_espeak_runtime_status",
        lambda: (False, "no espeak-ng runtime found: install `pip install espeakng-loader`"),
    )
    provider = make_provider(tmp_path, kokoro_voice=HINDI_VOICE)

    health = provider.health()
    assert health.available is False
    assert "espeak-ng" in health.detail

    with pytest.raises(NarrationAudioError) as excinfo:
        provider.resolve_narration(make_narration("नमस्ते"), "scene-007", run_dir=tmp_path / "run")
    message = str(excinfo.value)
    assert "espeak-ng" in message
    assert "does not install system software" in message
    assert not (tmp_path / "run" / "audio").exists()   # nothing was synthesized
    assert provider.engine.calls == []


def test_hindi_synthesis_when_espeak_is_available(tmp_path, monkeypatch):
    from ayce import tts_kokoro

    monkeypatch.setattr(
        tts_kokoro,
        "_espeak_runtime_status",
        lambda: (True, "espeakng-loader runtime usable (libespeak-ng.dll)"),
    )
    provider = make_provider(tmp_path, kokoro_voice=HINDI_VOICE)
    assert provider.health().available is True

    asset = provider.resolve_narration(
        make_narration("नमस्ते दुनिया"), "scene-008", run_dir=tmp_path / "run"
    )
    rate, channels, _width, _duration = wav_params(tmp_path / "run" / "audio" / "scene-008.wav")
    assert (rate, channels) == (SAMPLE_RATE, 1)
    assert asset.provenance.source_ref == f"{DEFAULT_REPO_ID}:{HINDI_VOICE}"
    assert provider.engine.calls[0]["text"] == "नमस्ते दुनिया"


# ---- content-addressed synthesis cache --------------------------------------


def test_cache_avoids_resynthesis_and_is_content_addressed(tmp_path):
    provider = make_provider(tmp_path)
    provider.resolve_narration(make_narration("Same line."), "scene-009",
                              run_dir=tmp_path / "run-a")
    provider.resolve_narration(make_narration("Same line."), "scene-010",
                              run_dir=tmp_path / "run-b")
    assert len(provider.engine.calls) == 1   # identical request reused the cache
    assert (
        (tmp_path / "run-a" / "audio" / "scene-009.wav").read_bytes()
        == (tmp_path / "run-b" / "audio" / "scene-010.wav").read_bytes()
    )

    provider.resolve_narration(make_narration("Different line."), "scene-011",
                              run_dir=tmp_path / "run-c")
    assert len(provider.engine.calls) == 2
    assert len(list((tmp_path / "cache").glob("*.wav"))) == 2
    assert list((tmp_path / "cache").glob("*.part")) == []


def test_voice_and_speed_change_the_cache_key(tmp_path):
    first = make_provider(tmp_path, kokoro_voice=ENGLISH_VOICE)
    second = make_provider(tmp_path, kokoro_voice="am_michael")
    faster = make_provider(tmp_path, kokoro_voice=ENGLISH_VOICE, kokoro_speed=1.25)
    assert first._cache_key("Hello.") != second._cache_key("Hello.")
    assert first._cache_key("Hello.") != faster._cache_key("Hello.")


# ---- selection + stage integration ------------------------------------------


def test_kokoro_is_selected_only_when_configured():
    assert isinstance(
        select_narration_provider(Config.from_env(env={})), FileBackedNarrationProvider
    )
    assert isinstance(
        select_narration_provider(Config(kokoro_voice=ENGLISH_VOICE)),
        KokoroNarrationProvider,
    )
    assert isinstance(
        select_narration_provider(Config(piper_model="voice.onnx")),
        PiperNarrationProvider,
    )
    # documented priority: Kokoro (primary) wins when both are configured
    both = select_narration_provider(
        Config(kokoro_voice=ENGLISH_VOICE, piper_model="voice.onnx")
    )
    assert isinstance(both, KokoroNarrationProvider)
    # a speed alone is NOT enough — the voice enables the real provider
    assert isinstance(
        select_narration_provider(Config(kokoro_speed=1.2)), FileBackedNarrationProvider
    )


def test_provider_fits_the_adapter_convention(tmp_path):
    provider = make_provider(tmp_path)
    assert isinstance(provider, Adapter)
    assert provider.name == "kokoro-82m"
    assert provider.health().name == "kokoro-82m"


def test_existing_narration_stage_consumes_the_real_provider(tmp_path):
    """The untouched P3 stage runs against the Kokoro provider end to end."""
    provider = make_provider(tmp_path)
    manifest = ProductionManifest(
        production_id=new_job_id(),
        title="Kokoro stage integration",
        scenes=(
            Scene(
                scene_id="scene-001",
                sequence=1,
                duration_seconds=3.0,
                narration=Narration(text="First line of narration."),
                visual=VisualIntent(description="a wide landscape"),
            ),
            Scene(
                scene_id="scene-002",
                sequence=2,
                duration_seconds=3.0,
                narration=Narration(text="Second line of narration."),
                visual=VisualIntent(description="a close-up"),
            ),
        ),
    )
    run_id = new_run_id()
    run = RunState(run_id=run_id, job_id=new_job_id())
    registry = ArtifactRegistry(tmp_path / "run", run_id)

    result = run_narration_audio_stage(manifest, run, registry, provider)

    assert result.ok, result.error
    assert result.manifest.provider == "kokoro-82m"
    durations = [record.duration_seconds for record in result.manifest.narration_audio]
    assert durations == [pytest.approx(0.5, abs=0.01), pytest.approx(0.5, abs=0.01)]
    assert (tmp_path / "run" / "audio" / "scene-001.wav").is_file()
    assert (tmp_path / "run" / "audio" / "scene-002.wav").is_file()


# ---- opt-in live smoke test (real local runtime; never in the normal suite) --

@pytest.mark.live
def test_live_kokoro_synthesis(tmp_path):
    """Real local Kokoro synthesis (opt-in; needs the tts-kokoro extra).

    Runs only when a voice is configured in the real environment
    (``AYCE_KOKORO_VOICE``). Verifies the ACTUAL produced file properties —
    no weight download happens in the normal suite.
    """
    config = Config.from_env()
    if not config.kokoro_voice:
        pytest.skip("AYCE_KOKORO_VOICE is not configured")
    provider = KokoroNarrationProvider(config, cache_dir=tmp_path / "cache")
    health = provider.health()
    assert health.available, health.detail

    asset = provider.resolve_narration(
        make_narration("Local narration smoke test."),
        "scene-live-001",
        run_dir=tmp_path / "run",
    )
    rate, channels, width, duration = wav_params(
        tmp_path / "run" / "audio" / "scene-live-001.wav"
    )
    assert (rate, channels, width) == (SAMPLE_RATE, 1, 2)
    assert duration > 0.2
    assert asset.duration_seconds == pytest.approx(duration, abs=0.001)
    assert asset.provenance.provider == "kokoro-82m"