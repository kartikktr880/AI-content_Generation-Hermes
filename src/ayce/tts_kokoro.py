"""Kokoro-82M narration provider (P8) — real local TTS behind the P3 seam.

Implements :class:`ayce.narration_audio.AudioProvider` with the official
``kokoro`` inference package (Kokoro-82M, Apache-2.0 weights downloaded from
the official model repository ``hexgrad/Kokoro-82M``). Execution is LOCAL:
no paid API, no network at synthesis time beyond the package's own model
cache.

Selection is strictly opt-in via ``AYCE_KOKORO_VOICE`` (see
``ayce.tts_piper.select_narration_provider``); when unset, the deterministic
file-backed fixture provider remains the default and Kokoro is never
imported.

Boundary rules:

- ``narration.text`` is synthesized VERBATIM — no rewording, SSML, trimming,
  padding, resampling, or duration fabrication.
- Output is 24 kHz mono 16-bit PCM WAV, written with the standard library
  ``wave`` module (the package's own sample rate; measured, never assumed).
  Duration/format are read back from the produced file with stdlib ``wave``
  (the P3 truthful-metadata convention); unreadable/empty output is an
  explicit :class:`NarrationAudioError`.
- The scene language/voice semantics of the existing narration request are
  used as-is: the Scene Contract carries only narration text, so the voice
  (and therefore the language, per Kokoro's own ``<lang><gender>_<name>``
  voice convention) comes from configuration. Nothing is added to the
  contract.
- Hindi (voice prefix ``h``) requires espeak-ng for G2P. Availability is
  verified BEFORE synthesis and a missing runtime raises a precise,
  actionable :class:`NarrationAudioError`; AYCE never installs system
  software. English (``a``/``b``) uses misaki's English G2P, where espeak-ng
  is only an out-of-dictionary fallback.
- The Windows native prerequisite is verified too: the engine import probe
  makes ``health()`` truthful about the Microsoft Visual C++ Redistributable
  (x64), which PyTorch's DLLs need. A missing runtime is reported with the
  official download link — never installed automatically.
- Deterministic file-backed output: synthesis results are cached under a
  content-addressed key (model + voice + speed + text), so identical
  requests reuse the same real WAV instead of re-synthesizing. The run copy
  always lands at the conventional ``audio/<scene_id>.wav`` path, so the
  timeline integration is unchanged.
- Any failure raises :class:`NarrationAudioError`; there is NO silent
  fallback to another TTS provider (the project forbids silent fallback —
  provider choice, including Piper, is configuration/orchestration policy).
- The ``pipeline_factory`` constructor argument is an explicit TEST seam:
  the production path always uses the official ``kokoro`` package.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import wave
from pathlib import Path
from typing import Callable, ClassVar

from .adapters import AdapterHealth
from .config import Config
from .narration_audio import (
    AUDIO_DIRNAME,
    AudioProvider,
    NarrationAudioError,
    NarrationAudioProvenance,
    ResolvedNarrationAudio,
)
from .scene_contract import Narration

__all__ = [
    "KokoroNarrationProvider",
]

#: Kokoro's fixed output sample rate (documented by the official package).
SAMPLE_RATE = 24000
#: Official model repository (Apache-2.0 weights). Never an unofficial mirror.
DEFAULT_REPO_ID = "hexgrad/Kokoro-82M"

#: Voice-name language prefixes enabled by this integration. Kokoro encodes
#: the language in the voice name (``af_heart`` = American English female,
#: ``hf_alpha`` = Hindi female), so the language is derived from the
#: configured voice rather than from a new contract field.
SUPPORTED_LANG_CODES: dict[str, str] = {
    "a": "English (US)",
    "b": "English (GB)",
    "h": "Hindi",
}
HINDI_LANG_CODE = "h"

#: Kokoro splits long texts on this pattern (its documented default) — kept
#: explicit so chunking is deterministic and visible.
SPLIT_PATTERN = r"\n+"

#: Cache location for synthesized WAVs (content-addressed file names).
DEFAULT_CACHE_SUBDIR = Path("narration_cache") / "kokoro"

#: Packages required for in-process synthesis (truthful health checks).
REQUIRED_PACKAGES = ("kokoro", "torch", "numpy")


def _engine_load_error_message(exc: OSError) -> str:
    """Turn a native-library load failure into an actionable description.

    The Windows Kokoro runtime imports PyTorch, whose DLLs require the
    Microsoft Visual C++ Redistributable (x64). AYCE never installs system
    software, so the exact prerequisite is reported instead.
    """
    detail = str(exc)
    hint = ""
    if "dll" in detail.lower() or "winerror" in detail.lower():
        hint = (
            " On Windows, PyTorch needs the Microsoft Visual C++ Redistributable "
            "(x64) — install it from the official Microsoft download "
            "(https://aka.ms/vs/17/release/vc_redist.x64.exe) and retry; AYCE does "
            "not install system software automatically."
        )
    return (
        f"the Kokoro runtime is installed but its native libraries failed to load: "
        f"{detail}.{hint}"
    )


#: Memoized engine-import probe result (importing the engine loads native
#: libraries — exactly the step that fails when a system runtime is missing).
_ENGINE_PROBE: dict[str, tuple[bool, str]] = {}


def _engine_import_probe() -> tuple[bool, str]:
    """Import the engine once per process and report truthfully (memoized).

    No model weights are loaded here — this only proves that the runtime can
    actually be imported on this machine, so ``health()`` never claims a
    usable engine when the native libraries cannot load.
    """
    if "result" in _ENGINE_PROBE:
        return _ENGINE_PROBE["result"]
    try:
        from kokoro import KPipeline  # noqa: F401 — import probe only
    except ImportError as exc:
        result = (False, f"the Kokoro runtime is not importable: {exc}")
    except OSError as exc:
        result = (False, _engine_load_error_message(exc))
    else:
        result = (True, "kokoro/torch native libraries loaded")
    _ENGINE_PROBE["result"] = result
    return result


def _espeak_runtime_status() -> tuple[bool, str]:
    """Truthful espeak-ng availability for languages that need it.

    Two legitimate local runtimes are recognized: the pip-provided
    ``espeakng-loader`` bundle (installed with ``misaki[en]``) and a system
    espeak-ng binary on PATH. Nothing is installed here.
    """
    if importlib.util.find_spec("espeakng_loader") is not None:
        try:
            import espeakng_loader

            library = Path(espeakng_loader.get_library_path())
            data = Path(espeakng_loader.get_data_path())
        except Exception as exc:  # noqa: BLE001 — report the real reason
            return False, f"espeakng-loader is installed but unusable: {exc}"
        if library.is_file() and data.is_dir():
            return True, f"espeakng-loader runtime usable ({library.name})"
        return False, (
            f"espeakng-loader is installed but its library/data paths are missing "
            f"({library}, {data})"
        )
    system = shutil.which("espeak-ng") or shutil.which("espeak")
    if system:
        return True, f"system espeak-ng found at {system}"
    return False, (
        "no espeak-ng runtime found: install the pip runtime "
        "(`pip install espeakng-loader`, shipped with `misaki[en]`, which the "
        "Kokoro runtime already depends on) or a system espeak-ng build"
    )


# ---- WAV plumbing (stdlib wave; mirror of the Piper provider's approach) ----


def _to_mono_samples(audio) -> "object":
    """Normalize one Kokoro audio chunk to a 1-D float array (never guessed).

    Kokoro yields either torch tensors or numpy arrays depending on version;
    anything that is not mono after squeezing is an explicit failure rather
    than a silently mangled channel layout.
    """
    import numpy as np

    if hasattr(audio, "detach"):  # torch tensor
        audio = audio.detach().cpu().numpy()
    samples = np.asarray(audio, dtype=np.float32).squeeze()
    if samples.ndim != 1:
        raise NarrationAudioError(
            f"unexpected audio shape {samples.shape} from kokoro; expected mono audio"
        )
    return samples


def _write_wav_24k_mono(path: Path, samples) -> None:
    """Write 24 kHz mono 16-bit PCM WAV (no resampling, no gain change)."""
    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).round().astype("<i2")
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(SAMPLE_RATE)
        writer.writeframes(pcm.tobytes())


def _read_wav_duration(data: bytes) -> float | None:
    """Read the REAL duration from WAV bytes (never estimated)."""
    import io

    try:
        with wave.open(io.BytesIO(data), "rb") as reader:
            frames = reader.getnframes()
            framerate = reader.getframerate()
            channels = reader.getnchannels()
            width = reader.getsampwidth()
    except (wave.Error, EOFError, OSError):
        return None
    if framerate <= 0 or frames <= 0 or channels <= 0 or width <= 0:
        return None
    return round(frames / framerate, 3)


def _verify_wav_format(path: Path) -> tuple[float, int, int]:
    """Verify a produced/cached WAV really is 24 kHz mono PCM.

    Returns (duration_seconds, sample_rate, channels); raises when the file
    is not usable production narration audio.
    """
    try:
        with wave.open(str(path), "rb") as reader:
            frames = reader.getnframes()
            framerate = reader.getframerate()
            channels = reader.getnchannels()
            width = reader.getsampwidth()
    except (wave.Error, EOFError, OSError) as exc:
        raise NarrationAudioError(f"unreadable WAV produced by kokoro: {path}: {exc}") from None
    if frames <= 0 or framerate <= 0 or channels <= 0 or width <= 0:
        raise NarrationAudioError(f"empty or malformed WAV produced by kokoro: {path}")
    if framerate != SAMPLE_RATE:
        raise NarrationAudioError(
            f"kokoro WAV sample rate is {framerate} Hz, expected {SAMPLE_RATE} Hz: {path}"
        )
    if channels != 1:
        raise NarrationAudioError(
            f"kokoro WAV has {channels} channels, expected mono: {path}"
        )
    return round(frames / framerate, 3), framerate, channels


# ---- provider ---------------------------------------------------------------


class KokoroNarrationProvider(AudioProvider):
    """Synthesize narration locally with the official Kokoro-82M runtime (P8).

    Configuration (all via ``AYCE_*`` through :class:`ayce.config.Config`):
    ``AYCE_KOKORO_VOICE`` (required to enable), ``AYCE_KOKORO_SPEED``,
    ``AYCE_KOKORO_REPO_ID``, ``AYCE_KOKORO_LICENSE``,
    ``AYCE_KOKORO_CACHE_DIR``.
    """

    name: ClassVar[str] = "kokoro-82m"

    def __init__(
        self,
        config: Config,
        *,
        cache_dir: str | Path | None = None,
        pipeline_factory: Callable[[str], Callable] | None = None,
    ) -> None:
        super().__init__(config)
        self.voice: str | None = config.kokoro_voice
        self.speed: float = config.kokoro_speed
        self.repo_id: str = config.kokoro_repo_id or DEFAULT_REPO_ID
        self.model_license: str | None = config.kokoro_license
        if cache_dir is not None:
            self.cache_dir = Path(cache_dir)
        elif config.kokoro_cache_dir:
            self.cache_dir = Path(config.kokoro_cache_dir)
        else:
            self.cache_dir = config.resolved_data_dir / DEFAULT_CACHE_SUBDIR
        #: TEST SEAM — the production path loads the official package.
        self._pipeline_factory = pipeline_factory or self._load_pipeline
        self._pipeline: Callable | None = None

    # ---- language/voice semantics (Kokoro's own naming convention) ----------

    @property
    def lang_code(self) -> str | None:
        """Language code implied by the configured voice (``af_heart`` → ``a``)."""
        if not self.voice:
            return None
        return self.voice[0].lower()

    # ---- truthful health ---------------------------------------------------

    def health(self) -> AdapterHealth:
        """Cheap, truthful readiness report (never synthesizes)."""
        if not self.voice:
            return AdapterHealth(
                name=self.name,
                available=False,
                detail=(
                    "AYCE_KOKORO_VOICE is not configured: the Kokoro narration "
                    "provider is opt-in (e.g. AYCE_KOKORO_VOICE=af_heart for "
                    "English, hf_alpha for Hindi)"
                ),
            )
        lang_code = self.lang_code or ""
        if lang_code not in SUPPORTED_LANG_CODES:
            return AdapterHealth(
                name=self.name,
                available=False,
                detail=(
                    f"voice {self.voice!r} implies language code {lang_code!r}; this "
                    f"integration enables "
                    f"{', '.join(sorted(SUPPORTED_LANG_CODES))} only"
                ),
            )
        missing = [p for p in REQUIRED_PACKAGES if importlib.util.find_spec(p) is None]
        if missing:
            return AdapterHealth(
                name=self.name,
                available=False,
                detail=(
                    f"missing Python package(s): {', '.join(missing)}; install the "
                    f"Kokoro runtime with `pip install 'kokoro>=0.9.4'` (pyproject "
                    f"extra 'tts-kokoro')"
                ),
            )
        loadable, load_detail = _engine_import_probe()
        if not loadable:
            return AdapterHealth(name=self.name, available=False, detail=load_detail)
        if lang_code == HINDI_LANG_CODE:
            espeak_ok, espeak_detail = _espeak_runtime_status()
            if not espeak_ok:
                return AdapterHealth(
                    name=self.name,
                    available=False,
                    detail=f"Hindi narration requires espeak-ng: {espeak_detail}",
                )
        return AdapterHealth(
            name=self.name,
            available=True,
            detail=(
                f"kokoro ready (voice={self.voice}, "
                f"language={SUPPORTED_LANG_CODES[lang_code]}, {SAMPLE_RATE} Hz mono); "
                f"model weights come from {self.repo_id} (downloaded on first "
                f"synthesis)"
            ),
        )

    # ---- synthesis ---------------------------------------------------------

    def _load_pipeline(self, lang_code: str) -> Callable:
        """Load the official Kokoro pipeline (lazy import; real local runtime)."""
        try:
            from kokoro import KPipeline
        except ImportError as exc:  # noqa: PERF203 — actionable boundary error
            raise NarrationAudioError(
                f"the Kokoro runtime is not installed ({exc}); install it with "
                f"`pip install 'kokoro>=0.9.4'` (pyproject extra 'tts-kokoro')"
            ) from None
        except OSError as exc:
            raise NarrationAudioError(_engine_load_error_message(exc)) from None
        return KPipeline(lang_code=lang_code, repo_id=self.repo_id)

    def _pipeline_for(self, lang_code: str) -> Callable:
        if self._pipeline is None:
            self._pipeline = self._pipeline_factory(lang_code)
        return self._pipeline

    def _synthesize(self, text: str):
        """Synthesize ``text`` verbatim and return 1-D mono float samples."""
        lang_code = self.lang_code or ""
        pipeline = self._pipeline_for(lang_code)
        chunks = []
        for _graphemes, _phonemes, audio in pipeline(
            text, voice=self.voice, speed=self.speed, split_pattern=SPLIT_PATTERN
        ):
            chunks.append(_to_mono_samples(audio))
        if not chunks:
            raise NarrationAudioError(
                f"kokoro produced no audio for {text[:60]!r} (voice={self.voice})"
            )
        if len(chunks) == 1:
            return chunks[0]
        import numpy as np

        return np.concatenate(chunks)

    def _cache_key(self, text: str) -> str:
        """Content-addressed synthesis key (model + voice + speed + text)."""
        material = f"{self.repo_id}|{self.voice}|{self.speed}|{text}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _ensure_synthesized(self, text: str) -> Path:
        """Return the cached WAV for ``text``, synthesizing it when needed."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / f"{self._cache_key(text)}.wav"
        if target.is_file() and target.stat().st_size > 0:
            _verify_wav_format(target)
            return target
        samples = self._synthesize(text)
        if getattr(samples, "size", 0) <= 0:
            raise NarrationAudioError(
                f"kokoro produced empty audio for {text[:60]!r} (voice={self.voice})"
            )
        partial = target.with_name(target.name + ".part")
        try:
            _write_wav_24k_mono(partial, samples)
            _verify_wav_format(partial)
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return target

    # ---- provider operation -------------------------------------------------

    def resolve_narration(
        self,
        narration: Narration,
        scene_id: str,
        *,
        run_dir: Path,
    ) -> ResolvedNarrationAudio:
        """Synthesize one scene's narration locally (24 kHz mono WAV)."""
        if not self.voice:
            raise NarrationAudioError(
                "kokoro narration provider is not configured: set "
                "AYCE_KOKORO_VOICE (e.g. af_heart for English, hf_alpha for Hindi)"
            )
        lang_code = self.lang_code or ""
        if lang_code not in SUPPORTED_LANG_CODES:
            raise NarrationAudioError(
                f"voice {self.voice!r} implies language code {lang_code!r}; this "
                f"integration enables {', '.join(sorted(SUPPORTED_LANG_CODES))} only"
            )
        if lang_code == HINDI_LANG_CODE:
            espeak_ok, espeak_detail = _espeak_runtime_status()
            if not espeak_ok:
                raise NarrationAudioError(
                    f"Kokoro Hindi narration (voice {self.voice!r}) needs espeak-ng "
                    f"for grapheme-to-phoneme conversion: {espeak_detail}. AYCE does "
                    f"not install system software automatically; install the runtime "
                    f"above and retry."
                )

        cached = self._ensure_synthesized(narration.text)

        audio_dir = Path(run_dir) / AUDIO_DIRNAME
        audio_dir.mkdir(parents=True, exist_ok=True)
        destination = audio_dir / f"{scene_id}.wav"
        shutil.copyfile(cached, destination)
        content = destination.read_bytes()
        duration = _read_wav_duration(content)
        if duration is None or duration <= 0:
            raise NarrationAudioError(
                f"kokoro output for scene {scene_id!r} is not a valid, non-empty WAV: "
                f"{destination}"
            )
        _verify_wav_format(destination)

        return ResolvedNarrationAudio(
            scene_id=scene_id,
            path=f"{AUDIO_DIRNAME}/{destination.name}",
            sha256=hashlib.sha256(content).hexdigest(),
            duration_seconds=duration,
            format="wav",
            provenance=NarrationAudioProvenance(
                provider=self.name,
                source="kokoro_local",
                source_ref=f"{self.repo_id}:{self.voice}",
                license=self.model_license,
            ),
        )