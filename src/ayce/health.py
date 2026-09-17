"""Baseline health / diagnostic command.

Truthful by design: a capability is reported PASS only when it actually
works in this environment right now. Unavailable media tooling (e.g.
FFmpeg) is reported WARN with a clear message, never silently ignored
and never faked as available.
"""

from __future__ import annotations

import json
import io
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

from . import __version__
from .adapters import Adapter, AdapterError, AdapterHealth, AdapterRegistry
from .artifacts import ArtifactKind, ArtifactError, ArtifactRegistry
from .config import Config, ConfigError
from .logging import StructuredLogger
from .state import InvalidTransitionError, RunState, StateError, StageStatus

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

_DISK_FREE_WARN_BYTES = 1 * 1024 * 1024 * 1024  # 1 GiB


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str = ""


def check_python_runtime() -> CheckResult:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 12):
        return CheckResult("python_runtime", WARN, f"Python {version}; 3.12+ recommended")
    return CheckResult("python_runtime", PASS, f"Python {version} ({sys.executable})")


def check_imports() -> CheckResult:
    try:
        import ayce  # noqa: F401
        from ayce import adapters, artifacts, config, health, ids, state  # noqa: F401

        return CheckResult("package_imports", PASS, f"ayce {ayce.__version__} imports cleanly")
    except Exception as exc:  # pragma: no cover - defensive
        return CheckResult("package_imports", FAIL, f"import failure: {exc}")


def check_config() -> CheckResult:
    try:
        cfg = Config.from_env()
        return CheckResult(
            "config",
            PASS,
            f"loaded (log_level={cfg.log_level}, data_dir={cfg.resolved_data_dir})",
        )
    except ConfigError as exc:
        return CheckResult("config", FAIL, str(exc))


def check_logging(config: Config, workdir: Path) -> CheckResult:
    try:
        log_file = workdir / "health" / "logging" / "probe.jsonl"
        discarded = io.StringIO()  # keep the probe out of the CLI's stderr
        logger = StructuredLogger(level=config.log_level, stream=discarded, file_path=log_file)
        logger.bind(run_id="health-probe", stage="health").info("health check probe")
        lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        record = json.loads(lines[-1])
        required = {"ts", "level", "message", "run_id", "stage"}
        missing = required - set(record)
        if missing:
            return CheckResult("logging", FAIL, f"log record missing fields: {sorted(missing)}")
        return CheckResult("logging", PASS, f"structured JSON record written to {log_file.name}")
    except Exception as exc:
        return CheckResult("logging", FAIL, f"logging failure: {exc}")


def check_state(workdir: Path) -> CheckResult:
    try:
        run = RunState(run_id="run-health-probe", job_id="job-health-probe")
        run.set_stage("probe_stage", StageStatus.RUNNING)
        run.set_stage("probe_stage", StageStatus.SUCCEEDED)
        try:
            run.set_stage("probe_stage", StageStatus.FAILED)
        except InvalidTransitionError:
            pass  # expected: succeeded is terminal
        else:
            return CheckResult("state", FAIL, "invalid transition was not rejected")
        path = workdir / "health" / "state" / "state.json"
        run.save(path)
        RunState.load(path)
        return CheckResult("state", PASS, "transitions validated; checkpoint saved + reloaded")
    except StateError as exc:
        return CheckResult("state", FAIL, f"state failure: {exc}")


def check_artifacts(workdir: Path) -> CheckResult:
    try:
        run_dir = workdir / "health" / "artifacts"
        registry = ArtifactRegistry(run_dir, run_id="run-health-probe")
        ref = registry.register("probe_stage", ArtifactKind.QA_REPORT, "qa/report.json", {"probe": True})
        reloaded = ArtifactRegistry.load(run_dir, run_id="run-health-probe").require(ref.artifact_id)
        if reloaded.path != ref.path:
            return CheckResult("artifacts", FAIL, "artifact roundtrip mismatch")
        return CheckResult("artifacts", PASS, "reference registered, persisted, reloaded")
    except ArtifactError as exc:
        return CheckResult("artifacts", FAIL, f"artifact failure: {exc}")


def check_adapters() -> CheckResult:
    registry = AdapterRegistry()
    if not registry.names():
        return CheckResult(
            "adapters", PASS, "adapter convention active; 0 providers registered (expected at P0)"
        )
    try:
        for name in registry.names():
            adapter = registry.create(name, Config.from_env())
            health: AdapterHealth = adapter.health()
            if not isinstance(health, AdapterHealth):
                return CheckResult("adapters", FAIL, f"adapter {name} returned invalid health")
        return CheckResult("adapters", PASS, f"{len(registry.names())} adapter(s) reported health")
    except AdapterError as exc:
        return CheckResult("adapters", FAIL, f"adapter failure: {exc}")


def check_ffmpeg() -> CheckResult:
    exe = shutil.which("ffmpeg")
    if not exe:
        return CheckResult(
            "ffmpeg",
            WARN,
            "ffmpeg NOT found on PATH — required later for the render/compositing stage",
        )
    try:
        proc = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10)
        first = (proc.stdout or proc.stderr).splitlines()[0] if (proc.stdout or proc.stderr) else "?"
        return CheckResult("ffmpeg", PASS, first)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("ffmpeg", WARN, f"ffmpeg found at {exe} but failed to run: {exc}")


def check_pytest() -> CheckResult:
    if find_spec("pytest") is None:
        return CheckResult("pytest", WARN, "pytest not importable — test harness unavailable")
    import pytest

    return CheckResult("pytest", PASS, f"pytest {pytest.__version__} available")


def check_disk_space(config: Config) -> CheckResult:
    try:
        # shutil.disk_usage needs an existing path; walk up to the nearest ancestor.
        target = config.resolved_data_dir
        while not target.exists() and target.parent != target:
            target = target.parent
        free = shutil.disk_usage(target).free
        free_gb = free / 1024**3
        if free < _DISK_FREE_WARN_BYTES:
            return CheckResult("disk_space", WARN, f"only {free_gb:.1f} GiB free")
        return CheckResult("disk_space", PASS, f"{free_gb:.1f} GiB free")
    except OSError as exc:
        return CheckResult("disk_space", WARN, f"could not determine free space: {exc}")


def run_checks(config: Config, workdir: Path | None = None) -> list[CheckResult]:
    """Execute all baseline checks. Failures inside a check become FAIL, never exceptions."""
    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="ayce-health-"))
    return [
        check_python_runtime(),
        check_imports(),
        check_config(),
        check_logging(config, workdir),
        check_state(workdir),
        check_artifacts(workdir),
        check_adapters(),
        check_ffmpeg(),
        check_pytest(),
        check_disk_space(config),
    ]


def summarize(results: list[CheckResult]) -> dict:
    counts = {PASS: 0, WARN: 0, FAIL: 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return {"ok": counts[FAIL] == 0, "counts": counts}


def render_text(results: list[CheckResult]) -> str:
    width = max(len(r.name) for r in results) if results else 5
    lines = [f"ayce health — {__version__}", ""]
    for r in results:
        lines.append(f"  [{r.status:^4}] {r.name.ljust(width)}  {r.detail}")
    summary = summarize(results)
    lines += [
        "",
        f"summary: {summary['counts'][PASS]} pass, {summary['counts'][WARN]} warn, "
        f"{summary['counts'][FAIL]} fail",
    ]
    return "\n".join(lines)


