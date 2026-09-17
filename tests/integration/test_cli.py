"""Integration tests: run the real CLI as a subprocess."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"


def run_cli(args, cwd, extra_env=None):
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "ayce", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
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
