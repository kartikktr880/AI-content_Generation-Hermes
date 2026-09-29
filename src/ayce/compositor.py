"""Stage 8 (MUST slice) — Procedural FFmpeg compositor + libass typography.

This module is the *Procedural FFmpeg Filtergraph Generator* of the approved
Stage 8 architecture ("Programmable Motion, Timeline Orchestration, and Media
Compositing"). The research verdict for Stage 8 selects a **Decoupled Hybrid
Rendering Pipeline**: the foundational media pipeline (decode, Ken Burns
motion, layering, audio multiplexing, codec multiplexing) is executed by the
C-based FFmpeg binaries, and pre-timed typography is rasterized by **libass**
through FFmpeg's ``ass`` filter — no browser runtime is required for the
MUST-scope overlay set. Remotion remains a SHOULD/OPTIONAL extension for
kinetic typography and is deliberately NOT implemented here.

REUSE FIRST (per the Stage 8 verdict): this renderer *wraps* the verified
P4.5/P5 renderer instead of replacing it — it subclasses
:class:`ayce.render.FFmpegRenderer` and reuses its executable resolution,
health gate, ``RenderedOutput`` contract, ffprobe validation and artifact
policy. The verified ``ffmpeg-smoke`` renderer remains the pipeline default;
this renderer is opt-in through configuration (``AYCE_RENDERER``), so no
previously verified behaviour changes.

MUST capabilities implemented here:

1. multi-track frame-accurate composition per timeline scene;
2. aspect-ratio fitting with background blur for mixed-aspect assets;
3. programmable Ken Burns pan/zoom with non-linear (smooth-step) easing;
4. standard transitions (``cut`` plus FFmpeg ``xfade`` transitions) that
   preserve the timeline's declared total duration exactly;
5. pre-timed SSA/ASS subtitle rasterization via libass (deterministic ASS
   generated from the verified ``captions.json`` artifact);
6. 48 kHz audio normalization and multiplexing.

Truthfulness: the filtergraph is built from typed timeline data only; no
caller-supplied path, argv or shell string is ever executed (the FFmpeg CLI is
invoked with an explicit argument list, never a shell). A failure at any step
raises — success is never claimed without an ffprobe-validated output.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .captions import CAPTIONS_FILENAME, CaptionsManifest, load_captions_manifest
from .config import Config
from .render import (
    DURATION_TOLERANCE_ABS_SECONDS,
    DURATION_TOLERANCE_RELATIVE,
    RENDER_DIRNAME,
    FFmpegRenderer,
    RenderedOutput,
    RenderError,
    _run_tool,
)
from .timeline import TimelineElementKind, TimelineManifest

__all__ = [
    "ASS_FILENAME",
    "AUDIO_SAMPLE_RATE",
    "COMPOSITOR_RENDERER_NAME",
    "TRANSITION_NAMES",
    "CompositorOptions",
    "build_ass_document",
    "build_renderer",
    "CompositorRenderer",
]

#: Renderer identifier recorded on the rendered_video artifact.
COMPOSITOR_RENDERER_NAME = "ffmpeg-compositor"

#: Deterministic ASS document written beside the production render.
ASS_FILENAME = "captions.ass"

#: Audio sample rate mandated by the Stage 8 MUST matrix (prevents sample-clock
#: drift and pitch distortion during multi-track multiplexing).
AUDIO_SAMPLE_RATE = 48000

#: FFmpeg ``xfade`` transitions exposed to configuration (MUST-level
#: "standard cuts, fades, dissolves, wipes"). ``cut`` is handled natively
#: (stream concatenation) because it is not a cross-fade.
TRANSITION_NAMES = frozenset(
    {
        "cut",
        "fade",
        "dissolve",
        "wipeleft",
        "wiperight",
        "wipeup",
        "wipedown",
        "slideleft",
        "slideright",
        "slideup",
        "slidedown",
        "smoothleft",
        "smoothright",
        "circleopen",
        "circleclose",
    }
)

#: ``xfade`` receives the FFmpeg transition name; ``dissolve`` is FFmpeg's ``fade``.
_XFADE_ALIASES = {"dissolve": "fade"}

#: Blur strength of the aspect-fill background plate (chroma radius must be < 8).
_BACKGROUND_LUMA_RADIUS = 6
_BACKGROUND_CHROMA_RADIUS = 2

#: Video codec settings — deterministic, broadly decodable production output.
_VIDEO_CODEC_ARGS = ("-c:v", "libx264", "-preset", "veryfast", "-crf", "20")
_AUDIO_CODEC_ARGS = ("-c:a", "aac", "-b:a", "128k", "-ar", str(AUDIO_SAMPLE_RATE))

#: Image inputs are looped at the compositor frame rate.
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm"})

#: Ken Burns easing denominator floor (never divide by zero on short scenes).
_MIN_FRAMES_PER_SCENE = 2


@dataclass(frozen=True)
class CompositorOptions:
    """Validated compositor configuration (config/operator controlled only)."""

    width: int
    height: int
    frame_rate: int
    transition: str
    transition_seconds: float
    kenburns: bool
    kenburns_strength: float
    burn_captions: bool

    @property
    def canvas(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def xfade_transition(self) -> str | None:
        """The FFmpeg transition name, or ``None`` for a hard cut."""
        if self.transition == "cut":
            return None
        return _XFADE_ALIASES.get(self.transition, self.transition)

    def validate(self) -> None:
        if self.width < 16 or self.height < 16:
            raise RenderError(f"compositor canvas must be at least 16x16 (got {self.canvas})")
        if self.frame_rate < 1:
            raise RenderError(f"compositor frame rate must be >= 1 (got {self.frame_rate})")
        if self.transition not in TRANSITION_NAMES:
            raise RenderError(
                f"unknown transition {self.transition!r}; "
                f"supported: {', '.join(sorted(TRANSITION_NAMES))}"
            )
        if not 0.0 <= self.transition_seconds <= 5.0:
            raise RenderError(
                f"transition_seconds must be within 0.0..5.0 (got {self.transition_seconds})"
            )
        if not 0.0 <= self.kenburns_strength <= 0.9:
            raise RenderError(
                f"kenburns_strength must be within 0.0..0.9 (got {self.kenburns_strength})"
            )

    @classmethod
    def from_config(cls, config: Config) -> "CompositorOptions":
        options = cls(
            width=config.canvas_width,
            height=config.canvas_height,
            frame_rate=config.frame_rate,
            transition=config.transition,
            transition_seconds=config.transition_seconds,
            kenburns=config.kenburns,
            kenburns_strength=config.kenburns_strength,
            burn_captions=config.burn_captions,
        )
        options.validate()
        return options


def build_renderer(config: Config) -> FFmpegRenderer:
    """Select the renderer for a run (configuration-driven; default unchanged).

    ``AYCE_RENDERER=ffmpeg-compositor`` selects the Stage 8 compositor; any
    other value keeps the verified ``ffmpeg-smoke`` production renderer.
    """
    if (config.renderer or "").strip().lower() == COMPOSITOR_RENDERER_NAME:
        return CompositorRenderer(config)
    return FFmpegRenderer(config)


# ---- libass typography (pre-timed SSA/ASS) ------------------------------------


def _ass_timestamp(seconds: float) -> str:
    """ASS timestamp ``H:MM:SS.cc`` (centisecond precision, deterministic)."""
    total_centis = int(round(max(seconds, 0.0) * 100))
    centis = total_centis % 100
    total_seconds = total_centis // 100
    return (
        f"{total_seconds // 3600}:{(total_seconds // 60) % 60:02d}:"
        f"{total_seconds % 60:02d}.{centis:02d}"
    )


def _sanitize_ass_text(text: str) -> str:
    """Escape caption text for ASS: no override blocks, no raw newlines.

    Caption text is untrusted structured input; ``{``/``}`` would open an ASS
    override block and a newline would break the ``Dialogue:`` record.
    """
    cleaned = text.replace("{", "(").replace("}", ")").replace("\r", "")
    return cleaned.replace("\n", "\\N").strip()


def build_ass_document(
    captions: CaptionsManifest,
    *,
    width: int,
    height: int,
    font_size: int | None = None,
) -> str:
    """Build a deterministic ASS document from the verified captions artifact."""
    size = font_size if font_size is not None else max(12, int(round(height * 0.045)))
    margin_v = max(12, int(round(height * 0.055)))
    margin_h = max(12, int(round(width * 0.05)))
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,"
        f"{size},&H00FFFFFF,&H000000FF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,"
        f"1,3,1,2,{margin_h},{margin_h},{margin_v},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    lines = []
    for cue in sorted(captions.cues, key=lambda c: (c.start_seconds, c.sequence)):
        lines.append(
            "Dialogue: 0,"
            f"{_ass_timestamp(cue.start_seconds)},{_ass_timestamp(cue.end_seconds)},"
            f"Default,,0,0,0,,{_sanitize_ass_text(cue.text)}"
        )
    return header + "\n".join(lines) + "\n"


def _quote_filter_value(path: Path) -> str:
    """Encode a filesystem path as an FFmpeg filtergraph option value.

    VERIFIED on Windows with the project's FFmpeg build: the ``ass`` filter
    splits an unquoted ``C:/...`` value at the colon and mis-reads the tail as
    the ``original_size`` option ("No option name near '/Users/...'"). The form
    that works when the filtergraph is passed directly through argv (no shell)
    is a single-quoted value with the drive colon backslash-escaped, i.e.
    ``filename='C\\:/path/file.ass'``. The relative form also works, but the
    absolute form is unambiguous regardless of the process working directory.
    """
    text = str(path).replace("\\", "/").replace("'", "\\'")
    text = text.replace(":", "\\:")
    return f"'{text}'"


#: Production encode bound (a real 1080p composited master is not a smoke render).
_COMPOSITOR_TIMEOUT_SECONDS = 480

#: The background plate is blurred at reduced resolution (deterministic + cheap).
_BACKGROUND_DIVISOR = 6


class CompositorRenderer(FFmpegRenderer):
    """Stage 8 compositing renderer: motion, transitions, typography, 48 kHz audio.

    Reuses the verified FFmpeg renderer's executable resolution, health gate
    and ffprobe validation (``_validate_output``); only the filtergraph
    generation differs, which is exactly the Stage 8 capability boundary.
    """

    name: ClassVar[str] = COMPOSITOR_RENDERER_NAME

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self.options = CompositorOptions.from_config(config)

    # ---- input plan --------------------------------------------------------

    def _scene_input_args(self, scene, run_dir: Path, *, pad_seconds: float) -> list[str]:
        """Input arguments for one scene's visual stream (input index ``i``)."""
        visual = next(
            (e for e in scene.elements if e.kind is TimelineElementKind.VISUAL), None
        )
        duration = scene.duration_seconds + pad_seconds
        if visual is None:
            # documented timeline policy: narration-only scenes render as filler
            return [
                "-f", "lavfi", "-i",
                f"color=c=black:s={self.options.canvas}:r={self.options.frame_rate}"
                f":d={duration}",
            ]
        source = run_dir / visual.source
        if not source.is_file():
            raise RenderError(
                f"resolved source missing for scene {scene.scene_id!r}: "
                f"{visual.source!r} does not exist in the run directory"
            )
        if source.suffix.lower() in _VIDEO_SUFFIXES:
            return ["-stream_loop", "-1", "-i", str(source)]
        return ["-loop", "1", "-framerate", str(self.options.frame_rate), "-i", str(source)]

    def _scene_audio_args(self, scene, run_dir: Path) -> list[str]:
        """Input arguments for one scene's audio stream (input index ``N + i``)."""
        narration = next(
            (e for e in scene.elements if e.kind is TimelineElementKind.NARRATION), None
        )
        if narration is None:
            return [
                "-f", "lavfi", "-i",
                f"anullsrc=r={AUDIO_SAMPLE_RATE}:cl=stereo:d={scene.duration_seconds}",
            ]
        source = run_dir / narration.source
        if not source.is_file():
            raise RenderError(
                f"resolved narration missing for scene {scene.scene_id!r}: "
                f"{narration.source!r} does not exist in the run directory"
            )
        return ["-i", str(source)]

    # ---- filtergraph generation (the Stage 8 procedural generator) ---------

    def _kenburns_filter(self, scene, index: int) -> str:
        """Programmable Ken Burns with non-linear (smooth-step) easing.

        Even scenes push in, odd scenes pull out; easing is the cubic
        smooth-step ``3t^2 - 2t^3`` evaluated per output frame so the motion
        decelerates organically instead of moving mechanically.
        """
        if not self.options.kenburns or self.options.kenburns_strength <= 0.0:
            return ""
        frames = max(
            _MIN_FRAMES_PER_SCENE,
            int(round(scene.duration_seconds * self.options.frame_rate)),
        )
        progress = f"min(on/{frames},1)"
        eased = f"(3*pow({progress},2)-2*pow({progress},3))"
        strength = self.options.kenburns_strength
        zoom = f"1+{strength}*{eased}" if index % 2 == 0 else f"1+{strength}*(1-{eased})"
        return (
            f",zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={self.options.canvas}:fps={self.options.frame_rate}"
        )

    def _scene_video_chain(self, scene, index: int, *, pad_seconds: float) -> str:
        """Per-scene composition chain: aspect fit + blurred plate + motion."""
        width, height = self.options.width, self.options.height
        fps = self.options.frame_rate
        small_w = max(32, width // _BACKGROUND_DIVISOR)
        small_h = max(32, height // _BACKGROUND_DIVISOR)
        # Named options: the positional form would bind the second value to
        # luma_power and leave the chroma radius at an invalid value.
        blur = (
            f"boxblur=luma_radius={_BACKGROUND_LUMA_RADIUS}:luma_power=2"
            f":chroma_radius={_BACKGROUND_CHROMA_RADIUS}:chroma_power=2"
        )
        chain = (
            f"[{index}:v]fps={fps},scale={width}:{height}"
            f":force_original_aspect_ratio=decrease[fg{index}];"
            f"[{index}:v]fps={fps},scale={width}:{height}"
            f":force_original_aspect_ratio=increase,crop={width}:{height},"
            f"scale={small_w}:{small_h},{blur},"
            f"scale={width}:{height}[bg{index}];"
            f"[bg{index}][fg{index}]overlay=(W-w)/2:(H-h)/2,setsar=1"
            f"{self._kenburns_filter(scene, index)},"
            f"trim=duration={scene.duration_seconds},setpts=PTS-STARTPTS"
        )
        if pad_seconds > 0.0:
            chain += f",tpad=stop_mode=clone:stop_duration={pad_seconds}"
        return chain + f"[v{index}];"

    def _scene_audio_chain(self, scene, index: int, *, audio_index: int) -> str:
        """Per-scene audio chain: 48 kHz normalization + exact scene interval."""
        return (
            f"[{audio_index}:a]aresample={AUDIO_SAMPLE_RATE},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"apad,atrim=0:{scene.duration_seconds},asetpts=N/SR/TB[a{index}];"
        )

    def _build_filter_complex(
        self, timeline: TimelineManifest, *, captions_ass: Path | None
    ) -> str:
        """The complete deterministic composition graph for one timeline."""
        options = self.options
        scenes = timeline.scenes
        count = len(scenes)
        xfade = options.xfade_transition
        pad = options.transition_seconds if xfade is not None else 0.0
        total = timeline.total_duration_seconds

        steps: list[str] = []
        for index, scene in enumerate(scenes):
            steps.append(self._scene_video_chain(scene, index, pad_seconds=pad))
            steps.append(self._scene_audio_chain(scene, index, audio_index=count + index))

        if count == 1:
            steps.append(f"[v0]trim=duration={total},setpts=PTS-STARTPTS[vcat];")
        elif xfade is None:
            # hard cut: plain stream concatenation, duration exactly preserved
            joined = "".join(f"[v{index}]" for index in range(count))
            steps.append(f"{joined}concat=n={count}:v=1:a=0[vcat];")
        else:
            # cross-fade chain: each scene's tail is padded by the transition
            # duration, so the overlaps are absorbed and the composited total
            # still equals the declared timeline total exactly.
            previous = "v0"
            for index in range(1, count):
                label = "vraw" if index == count - 1 else f"x{index}"
                steps.append(
                    f"[{previous}][v{index}]xfade=transition={xfade}"
                    f":duration={pad}:offset={scenes[index].start_seconds}[{label}];"
                )
                previous = label
            steps.append(f"[vraw]trim=duration={total},setpts=PTS-STARTPTS[vcat];")

        if captions_ass is not None:
            steps.append(
                f"[vcat]ass=filename={_quote_filter_value(captions_ass)}[vout];"
            )
        else:
            steps.append("[vcat]null[vout];")

        audio_joined = "".join(f"[a{index}]" for index in range(count))
        steps.append(f"{audio_joined}concat=n={count}:v=0:a=1[acat]")
        return "".join(steps)

    # ---- captions / typography --------------------------------------------

    def _load_captions(self, run_dir: Path) -> CaptionsManifest | None:
        """Load the verified captions artifact when burning is enabled.

        Fails closed: when burning is configured but the verified captions
        artifact is absent, the render refuses instead of silently producing a
        master without its typography layer.
        """
        if not self.options.burn_captions:
            return None
        path = Path(run_dir) / CAPTIONS_FILENAME
        if not path.is_file():
            raise RenderError(
                "burn_captions is enabled but the verified captions artifact is "
                f"missing ({CAPTIONS_FILENAME} not found in the run directory)"
            )
        return load_captions_manifest(path)

    def _write_ass(self, run_dir: Path, captions: CaptionsManifest | None) -> Path:
        """Materialize the deterministic ASS document for libass rasterization."""
        if captions is None:
            raise RenderError(
                "burn_captions is enabled but no verified captions artifact is "
                f"available ({CAPTIONS_FILENAME} missing in the run directory)"
            )
        path = Path(run_dir) / RENDER_DIRNAME / ASS_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            build_ass_document(
                captions, width=self.options.width, height=self.options.height
            ),
            encoding="utf-8",
        )
        return path

    # ---- rendering ---------------------------------------------------------

    def _render_timeline(
        self,
        timeline: TimelineManifest,
        run_dir: Path,
        output_path: Path,
        *,
        captions: CaptionsManifest | None,
    ) -> RenderedOutput:
        ffmpeg = self._resolve_executable(self.config.ffmpeg_path, "ffmpeg")
        ffprobe = self._resolve_executable(self.config.ffprobe_path, "ffprobe")
        if ffmpeg is None or ffprobe is None:
            raise RenderError("ffmpeg/ffprobe executables not found")

        run_dir = Path(run_dir)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pad = (
            self.options.transition_seconds
            if self.options.xfade_transition is not None
            else 0.0
        )
        captions_ass = self._write_ass(run_dir, captions) if captions is not None else None

        args: list[str] = ["-y"]
        for scene in timeline.scenes:
            args += self._scene_input_args(scene, run_dir, pad_seconds=pad)
        for scene in timeline.scenes:
            args += self._scene_audio_args(scene, run_dir)
        args += [
            "-filter_complex",
            self._build_filter_complex(timeline, captions_ass=captions_ass),
            "-map", "[vout]", "-map", "[acat]",
            *_VIDEO_CODEC_ARGS, "-pix_fmt", "yuv420p", "-r", str(self.options.frame_rate),
            *_AUDIO_CODEC_ARGS, "-movflags", "+faststart",
            str(output_path),
        ]

        proc = _run_tool(ffmpeg, args, timeout=_COMPOSITOR_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()
            detail = tail[-1] if tail else "no output"
            raise RenderError(f"compositor ffmpeg exited {proc.returncode}: {detail}")
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RenderError(
                f"ffmpeg reported success but the output is missing or empty: {output_path}"
            )

        rendered = self._validate_output(ffprobe, output_path)
        expected = timeline.total_duration_seconds
        tolerance = DURATION_TOLERANCE_ABS_SECONDS + DURATION_TOLERANCE_RELATIVE * expected
        if abs((rendered.duration_seconds or 0.0) - expected) > tolerance:
            raise RenderError(
                f"composited duration {rendered.duration_seconds}s deviates from the "
                f"timeline total {expected}s beyond the documented tolerance "
                f"(+/-{tolerance:.2f}s) - scene coverage cannot be confirmed"
            )
        return rendered

    def render(
        self, timeline: TimelineManifest, scene_id: str, *, run_dir: Path
    ) -> RenderedOutput:
        """Compose ONE timeline scene through the same Stage 8 pipeline."""
        scene = next((s for s in timeline.scenes if s.scene_id == scene_id), None)
        if scene is None:
            raise RenderError(f"scene {scene_id!r} not found in the timeline")
        subset = timeline.model_copy(
            update={"scenes": (scene,), "total_duration_seconds": scene.duration_seconds}
        )
        captions = self._load_captions(Path(run_dir))
        if captions is not None:
            shifted = tuple(
                cue.model_copy(
                    update={
                        "start_seconds": max(0.0, cue.start_seconds - scene.start_seconds),
                        "end_seconds": max(0.0, cue.end_seconds - scene.start_seconds),
                    }
                )
                for cue in captions.cues
                if cue.scene_id == scene_id
            )
            captions = captions.model_copy(update={"cues": shifted})
        return self._render_timeline(
            subset,
            Path(run_dir),
            Path(run_dir) / RENDER_DIRNAME / f"{scene_id}.mp4",
            captions=captions,
        )

    def render_production(
        self, timeline: TimelineManifest, *, run_dir: Path
    ) -> RenderedOutput:
        """Compose the COMPLETE timeline into one production master."""
        return self._render_timeline(
            timeline,
            Path(run_dir),
            Path(run_dir) / RENDER_DIRNAME / f"{timeline.production_id}.mp4",
            captions=self._load_captions(Path(run_dir)),
        )

        """Load the verified captions artifact when burning is enabled."""
        if not self.options.burn_captions:
            return None
        path = Path(run_dir) / CAPTIONS_FILENAME
        if not path.is_file():
            return None
        return load_captions_manifest(path)

    def _write_ass(self, run_dir: Path, captions: CaptionsManifest | None) -> Path:
        """Materialize the deterministic ASS document for libass rasterization."""
        if captions is None:
            raise RenderError(
                "burn_captions is enabled but no verified captions artifact is "
                f"available ({CAPTIONS_FILENAME} missing in the run directory)"
            )
        path = Path(run_dir) / RENDER_DIRNAME / ASS_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            build_ass_document(
                captions, width=self.options.width, height=self.options.height
            ),
            encoding="utf-8",
        )
        return path
