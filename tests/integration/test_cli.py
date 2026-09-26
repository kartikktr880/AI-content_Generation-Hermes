"""Integration tests: run the real CLI as a subprocess."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"


def run_cli(args, cwd, extra_env=None, timeout=60):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "ayce", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.integration
def test_health_command_exits_zero(tmp_path):
    # run from tmp cwd so the default data/ dir is created there, not in the repo
    proc = run_cli(["health"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ayce health" in proc.stdout
    assert "[FAIL]" not in proc.stdout
    assert "summary:" in proc.stdout


@pytest.mark.integration
def test_health_command_json_output(tmp_path):
    proc = run_cli(["health", "--json"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert any(c["name"] == "ffmpeg" for c in report["checks"])


@pytest.mark.integration
def test_version_flag(tmp_path):
    proc = run_cli(["--version"], cwd=tmp_path)
    assert proc.returncode == 0
    assert proc.stdout.startswith("ayce")


# ---- P6: `ayce run` — the Golden Path pipeline via the real CLI ------------------


def _fixture(name: str) -> Path:
    return PROJECT_ROOT / "tests" / "fixtures" / name


@pytest.mark.integration
def test_run_command_golden_path(tmp_path):
    script = _fixture("script_to_scene/documentary.json")
    assets = _fixture("asset_provider")
    narration = _fixture("narration_fixtures")
    proc = run_cli(
        [
            "run", str(script),
            "--assets-dir", str(assets),
            "--narration-dir", str(narration),
        ],
        cwd=tmp_path,
        extra_env={"AYCE_DATA_DIR": str(tmp_path / "data")},
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[OK] media_qa" in proc.stdout

    # the run directory is reported and contains the full artifact set
    run_dir_line = next(
        line for line in proc.stdout.splitlines() if line.startswith("run_dir: ")
    )
    run_dir = Path(run_dir_line.removeprefix("run_dir: ").strip())
    assert run_dir.is_dir()
    for name in (
        "state.json", "artifacts.json", "scene_manifest.json",
        "asset_manifest.json", "narration_manifest.json", "timeline.json",
        "captions.json", "captions.srt", "qa_report.json",
    ):
        assert (run_dir / name).is_file(), name

    # the rendered MP4 exists (registered rendered_video artifact)
    registry_payload = json.loads((run_dir / "artifacts.json").read_text(encoding="utf-8"))
    render_refs = [r for r in registry_payload if r["kind"] == "rendered_video"]
    assert len(render_refs) == 1
    assert (run_dir / render_refs[0]["path"]).is_file()

    # the persisted QA report reloads with a PASS verdict
    report = json.loads((run_dir / "qa_report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "PASS"

    # the durable state checkpoint shows every stage succeeded
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert {s["status"] for s in state["stages"].values()} == {"succeeded"}
    assert set(state["stages"]) == {
        "script_to_scene", "asset_resolution", "narration_audio",
        "timeline", "captions", "production_render", "media_qa",
    }

    # the SRT serialization exists and its cue text matches the scene contract
    srt_text = (run_dir / "captions.srt").read_text(encoding="utf-8")
    scene_manifest = json.loads((run_dir / "scene_manifest.json").read_text(encoding="utf-8"))
    for scene in scene_manifest["scenes"]:
        assert scene["narration"]["text"] in srt_text

    assert "qa_verdict: PASS" in proc.stdout


@pytest.mark.integration
def test_run_command_json_output(tmp_path):
    script = _fixture("script_to_scene/documentary.json")
    proc = run_cli(
        [
            "run", str(script),
            "--assets-dir", str(_fixture("asset_provider")),
            "--narration-dir", str(_fixture("narration_fixtures")),
            "--json",
        ],
        cwd=tmp_path,
        extra_env={"AYCE_DATA_DIR": str(tmp_path / "data")},
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    assert report["qa_verdict"] == "PASS"
    assert report["failed_stage"] is None
    assert [s["stage"] for s in report["stages"]] == [
        "script_to_scene", "asset_resolution", "narration_audio",
        "timeline", "captions", "production_render", "media_qa",
    ]


@pytest.mark.integration
def test_run_command_invalid_script_path(tmp_path):
    data_dir = tmp_path / "data"
    proc = run_cli(
        ["run", str(tmp_path / "does-not-exist.json")],
        cwd=tmp_path,
        extra_env={"AYCE_DATA_DIR": str(data_dir)},
    )
    assert proc.returncode != 0
    assert "not found" in (proc.stdout + proc.stderr)
    # no partially-created production run directory
    runs_dir = data_dir / "runs"
    assert not runs_dir.exists() or not any(runs_dir.iterdir())
