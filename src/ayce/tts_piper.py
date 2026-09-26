"""Piper TTS narration provider (P7) — real speech behind the P3 adapter seam.

Implements :class:`ayce.narration_audio.AudioProvider` by invoking the
externally installed Piper TTS engine (``piper-tts``, piper1-gpl) as a
**CLI subprocess in its own Python environment**.

GPL isolation (P6.6 decision): Piper is GPL-3.0 — NEVER imported into
AYCE and NOT an AYCE dependency. The adapter shells out to a configured
interpreter (``AYCE_PIPER_PYTHON``) with an explicit argv list (never a
shell), like the verified FFmpeg render boundary. The file-backed fixture
provider remains the default; Piper is strictly opt-in via configuration.

Boundary rules:
- ``narration.text`` is synthesized VERBATIM — no rewording, SSML,
  trimming, padding, resampling, or duration fabrication.
- Duration/format are read from the produced WAV via the stdlib ``wave``
  module (P3 truthful-metadata convention); unreadable/empty output is
  an explicit :class:`NarrationAudioError` — never invented metadata.
- Health is truthful and cheap (config presence, path existence, fast
  ``-m piper --help`` probe; NO synthesis). An unconfigured Piper is
  *unavailable*, not a crash.
- provenance license is recorded ONLY when explicitly configured
  (``AYCE_PIPER_LICENSE``) — never guessed.
- Any failure raises :class:`NarrationAudioError`; there is NO silent
  fallback to another TTS provider.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import wave
from pathlib import Path
from typing import ClassVar

from .adapters import AdapterHealth
from .config import Config
from .narration_audio import (
    AUDIO_DIRNAME,
    AudioProvider,
    FileBackedNarrationProvider,
    NarrationAudioError,
    NarrationAudioProvenance,
    ResolvedNarrationAudio,
)
from .scene_contract import Narration

__all__ = [
    "PiperNarrationProvider",
    "select_narration_provider",
]

#: Bounded synthesis runtime (neural TTS on CPU is fast; a hang-guard,
#: not a performance bound).
SYNTHESIS_TIMEOUT_SECONDS = 300
#: Cheap availability-probe runtime (``-m piper --help``).
HEALTH_TIMEOUT_SECONDS = 30
#: How much of a failed process's stderr is attached to error messages.
_STDERR_TAIL_LINES = 5


class PiperNarrationProvider(AudioProvider):
    """Synthesize narration with an externally installed Piper TTS engine.

    Configuration (all via ``AYCE_*`` through :class:`ayce.config.Config`):
    ``AYCE_PIPER_PYTHON`` (python executable with piper-tts installed),
    ``AYCE_PIPER_MODEL`` (.onnx voice model; required to enable Piper),
    ``AYCE_PIPER_CONFIG`` (optional .onnx.json), ``AYCE_PIPER_LENGTH_SCALE``
    (optional rate control, >0), ``AYCE_PIPER_LICENSE`` (explicit
    voice-model license for provenance).
    """

    name: ClassVar[str] = "piper-tts"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.python_executable: str | None = config.piper_python
        self.model_path: Path | None = (
            Path(config.piper_model) if config.piper_model else None
        )
        self.model_config_path: Path | None = (
            Path(config.piper_config) if config.piper_config else None
        )
        self.length_scale: float | None = config.piper_length_scale
        self.model_license: str | None = config.piper_license

    # ---- truthful health ------------------------------------------------------------

    def health(self) -> AdapterHealth:
        """Truthful availability report. Cheap: no synthesis is performed."""
        if not self.python_executable:
            return self._unhealthy("AYCE_PIPER_PYTHON is not configured")
        if not self.model_path:
            return self._unhealthy("AYCE_PIPER_MODEL is not configured")

        resolved_exe = shutil.which(self.python_executable)
        if resolved_exe is None and not Path(self.python_executable).is_file():
            return self._unhealthy(
                f"piper python executable not found: {self.python_executable!r}"
            )
        executable = resolved_exe or self.python_executable

        if not self.model_path.is_file():
            return self._unhealthy(f"piper model not found: {self.model_path}")
        if self.model_config_path is not None and not self.model_config_path.is_file():
            return self._unhealthy(f"piper model config not found: {self.model_config_path}")

        # cheap availability probe: the configured environment must actually
        # be able to run the piper module (catches "python exists but
        # piper-tts is not installed there")
        try:
            proc = subprocess.run(
                [str(executable), "-m", "piper", "--help"],
                capture_output=True,
                timeout=HEALTH_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            return self._unhealthy(f"piper probe failed to start: {exc}")
        except subprocess.TimeoutExpired:
            return self._unhealthy(f"piper probe timed out after {HEALTH_TIMEOUT_SECONDS}s")
        if proc.returncode != 0:
            return self._unhealthy(
                "piper module is not usable in the configured environment "
                f"(exit {proc.returncode}): {self._stderr_tail(proc.stderr)}"
            )
        return AdapterHealth(
            name=self.name,
            available=True,
            detail=f"piper ready (model: {self.model_path.name})",
        )

    def _unhealthy(self, detail: str) -> AdapterHealth:
        return AdapterHealth(name=self.name, available=False, detail=detail)

    def _stderr_tail(self, stderr: bytes | None) -> str:
        text = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return " | ".join(text[-_STDERR_TAIL_LINES:]) or "no stderr output"

    # ---- synthesis ------------------------------------------------------------------

    def resolve_narration(
        self,
        narration: Narration,
        scene_id: str,
        *,
        run_dir: Path,
    ) -> ResolvedNarrationAudio:
        """Synthesize one scene's narration text into a truthful WAV record."""
        if not self.python_executable or self.model_path is None:
            raise NarrationAudioError(
                "piper provider is not configured: set AYCE_PIPER_PYTHON and "
                "AYCE_PIPER_MODEL (and check its health first)"
            )

        run_dir = Path(run_dir)
        audio_dir = run_dir / AUDIO_DIRNAME
        audio_dir.mkdir(parents=True, exist_ok=True)
        output_path = audio_dir / f"{scene_id}.wav"
        input_path = audio_dir / f".{scene_id}.piper-input.txt"
        input_path.write_text(narration.text, encoding="utf-8")

        try:
            # explicit argv list — never a shell, never string concatenation
            argv = [str(self.python_executable), "-m", "piper", "-m", str(self.model_path)]
            if self.model_config_path is not None:
                argv += ["-c", str(self.model_config_path)]
            argv += ["-i", str(input_path), "-f", str(output_path)]
            if self.length_scale is not None:
                argv += ["--length-scale", str(self.length_scale)]

            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    timeout=SYNTHESIS_TIMEOUT_SECONDS,
                )
            except FileNotFoundError as exc:
                raise NarrationAudioError(
                    f"piper python executable not found: {self.python_executable!r}"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise NarrationAudioError(
                    f"piper synthesis timed out after {SYNTHESIS_TIMEOUT_SECONDS}s "
                    f"for scene {scene_id!r}"
                ) from exc
            if proc.returncode != 0:
                raise NarrationAudioError(
                    f"piper exited {proc.returncode} for scene {scene_id!r}: "
                    f"{self._stderr_tail(proc.stderr)}"
                )
        finally:
            input_path.unlink(missing_ok=True)

        if not output_path.is_file():
            raise NarrationAudioError(
                f"piper reported success but produced no output for scene {scene_id!r}: "
                f"expected {output_path}"
            )
        content = output_path.read_bytes()
        duration = _read_wav_duration(content)
        if duration is None or duration <= 0:
            raise NarrationAudioError(
                f"piper output for scene {scene_id!r} is not a valid, non-empty WAV: "
                f"{output_path}"
            )

        return ResolvedNarrationAudio(
            scene_id=scene_id,
            path=f"{AUDIO_DIRNAME}/{output_path.name}",
            sha256=hashlib.sha256(content).hexdigest(),
            duration_seconds=duration,
            format="wav",
            provenance=NarrationAudioProvenance(
                provider=self.name,
                source="piper_tts_local",
                source_ref=self.model_path.name,
                license=self.model_license,
            ),
        )


def _read_wav_duration(data: bytes) -> float | None:
    """Read the REAL duration from WAV bytes (stdlib wave — never estimated).

    Returns None when the bytes are not a parseable, non-empty WAV.
    """
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


def select_narration_provider(config: Config, narration_dir: str | Path | None = None):
    """Opt-in provider selection (the ONLY wiring point in the pipeline).

    - Piper is selected ONLY when ``AYCE_PIPER_MODEL`` is explicitly
      configured.
    - Otherwise the deterministic file-backed fixture provider is
      returned — the verified default behavior is untouched.
    """
    if config.piper_model:
        return PiperNarrationProvider(config)
    if narration_dir is not None:
        return FileBackedNarrationProvider(config, narration_dir)
    return FileBackedNarrationProvider(config)
