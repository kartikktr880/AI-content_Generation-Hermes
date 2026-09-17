from ayce.config import Config
from ayce.health import FAIL, run_checks, summarize


def test_health_checks_all_pass_or_warn(tmp_path):
    config = Config.from_env(env={})
    results = run_checks(config, workdir=tmp_path)
    names = [r.name for r in results]
    assert {
        "python_runtime",
        "package_imports",
        "config",
        "logging",
        "state",
        "artifacts",
        "adapters",
        "ffmpeg",
        "pytest",
        "disk_space",
    } <= set(names)
    # ffmpeg may legitimately be WARN (not installed here), but nothing may FAIL
    failing = [r for r in results if r.status == FAIL]
    assert failing == []


def test_summary_counts(tmp_path):
    config = Config.from_env(env={})
    results = run_checks(config, workdir=tmp_path)
    summary = summarize(results)
    assert summary["ok"] is True
    assert summary["counts"]["FAIL"] == 0


def test_core_mechanisms_report_pass(tmp_path):
    config = Config.from_env(env={})
    results = {r.name: r for r in run_checks(config, workdir=tmp_path)}
    for name in ("package_imports", "config", "logging", "state", "artifacts", "adapters"):
        assert results[name].status == "PASS", f"{name}: {results[name].detail}"
