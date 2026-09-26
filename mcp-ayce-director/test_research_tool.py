"""Stage 2 research tool tests — protocol-level + boundary-level, no LLM.

Covers: tool registration, input validation, idempotent digest reuse,
successful research invocation (stub runner), structured artifact
return, controlled errors, timeout, unparseable output, and
non-interference with the trigger_golden_path ledger.

NO test performs a real YouTube request: execution is exercised through
the explicit `runner=` seam of handle_research_request. The ONE real
controlled execution is performed separately by research_roundtrip.py.

Run with the director's isolated venv:
    set PYTHONPATH=<repo>\\src
    mcp-ayce-director\\.venv\\Scripts\\python -m pytest mcp-ayce-director\\test_research_tool.py
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SERVER = HERE / "server.py"

sys.path.insert(0, str(HERE))
import server as director  # noqa: E402


# ---- helpers -----------------------------------------------------------------------

def _req(**overrides):
    payload = {
        "objective": "identify outlier video formats in the AI tools niche",
        "query": "ai tools",
        "max_outliers": 3,
    }
    payload.update(overrides)
    return {k: v for k, v in payload.items() if v is not ...}


def _envelope(research_id="res-20260920T000000Z-cafe00000001"):
    return {
        "ok": True,
        "research_id": research_id,
        "status": "SUCCESS",
        "candidate_count": 3,
        "artifact_path": "data/research/res-20260920T000000Z-cafe00000001.json",
        "artifact": {
            "schema_version": "1.0",
            "research_id": research_id,
            "objective": "identify outlier video formats",
            "status": "SUCCESS",
            "tiers": ["OBSERVED_FACT", "DERIVED_METRIC", "MODEL_INFERENCE",
                      "CREATIVE_HYPOTHESIS"],
            "candidate_count": 3,
            "outliers": [{"video_id": "abc12345678", "title": "A title"}],
            "provenance": {"worker_version": "1.0"},
        },
    }


def _research_stub(stdout: str = "", exit_code: int = 0, timed_out: bool = False,
                   calls: list | None = None):
    def runner(argv, cwd, env, timeout_s, stdin_text):
        if calls is not None:
            calls.append({"argv": argv, "cwd": cwd, "env": env,
                          "timeout_s": timeout_s, "stdin_text": stdin_text})
        return {"timed_out": timed_out, "exit_code": exit_code,
                "stdout": stdout, "stderr": "", "started_at": "s", "finished_at": "f"}
    return runner


@pytest.fixture()
def ledger(tmp_path):
    return tmp_path / "research_requests.json"


# ---- 1: tool registration ----------------------------------------------------------

def test_execute_research_slice_is_registered():
    async def _run():
        tools = await director.mcp.list_tools()
        return {t.name for t in tools}
    names = asyncio.run(_run())
    assert "execute_research_slice" in names
    assert "trigger_golden_path" in names  # H4 write tool still present


# ---- 2: input validation (never executes) ------------------------------------------

def _never_runs_research(argv, cwd, env, timeout_s, stdin_text):
    raise AssertionError("boundary attempted execution on an invalid request")


@pytest.mark.parametrize("payload", [
    {},                                        # missing objective
    {"objective": "   "},                      # blank objective
    {"objective": "x" * 2001},                 # objective too long
    {"objective": "x", "bogus": 1},            # unsupported field
    {"objective": "x", "max_outliers": 0},     # below range
    {"objective": "x", "max_outliers": 11},    # above range
    {"objective": "x", "max_outliers": "5"},   # wrong type
    {"objective": "x", "max_videos": 13},      # above range
    {"objective": "x", "niche": "y" * 301},    # too long
    {"objective": "x", "target_channels": ["ok", "; rm -rf"]},   # metacharacter
    {"objective": "x", "target_channels": ["a"] * 6},            # too many
])
def test_invalid_research_requests_rejected_without_execution(ledger, payload):
    result = director.handle_research_request(
        payload, runner=_never_runs_research, ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"
    records = json.loads(ledger.read_text(encoding="utf-8"))["requests"]
    assert records[-1]["status"] == "rejected"


def test_validation_normalizes_defaults(ledger):
    normalized, error = director.validate_research_request(_req())
    assert error is None
    assert normalized["max_outliers"] == 3
    assert normalized["max_videos"] == 6          # default
    assert normalized["target_channels"] == []    # default
    assert normalized["niche"] is None


# ---- 3: successful invocation (stub runner) ----------------------------------------

def test_successful_research_invocation_returns_structured_artifact(ledger):
    calls: list = []
    result = director.handle_research_request(
        _req(), runner=_research_stub(json.dumps(_envelope()), calls=calls),
        ledger_path=ledger)
    assert result["ok"] is True
    assert result["research_id"] == "res-20260920T000000Z-cafe00000001"
    assert result["status"] == "SUCCESS"
    assert result["candidate_count"] == 3
    assert result["artifact"]["tiers"][0] == "OBSERVED_FACT"
    # fixed argv, request JSON on stdin, server-controlled everything
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[1:3] == ["-m", "ayce.research"] and argv[3] == "--json"
    assert json.loads(calls[0]["stdin_text"])["objective"].startswith("identify outlier")
    assert calls[0]["cwd"] == str(director.REPO)
    assert calls[0]["env"]["PYTHONPATH"] == str(director.AYCE_SRC)
    # ledger records the succeeded slice
    records = json.loads(ledger.read_text(encoding="utf-8"))["requests"]
    assert records[-1]["status"] == "succeeded"
    assert records[-1]["research_id"] == result["research_id"]


def test_idempotent_digest_reuse_avoids_second_run(ledger):
    calls: list = []
    stub = _research_stub(json.dumps(_envelope()), calls=calls)
    first = director.handle_research_request(_req(), runner=stub, ledger_path=ledger)
    second = director.handle_research_request(_req(), runner=stub, ledger_path=ledger)
    assert first["ok"] and second["ok"]
    assert second.get("duplicate") is True
    assert second["research_id"] == first["research_id"]
    assert len(calls) == 1  # the worker ran exactly once
    # a DIFFERENT payload is not a duplicate
    third = director.handle_research_request(
        _req(max_outliers=4), runner=stub, ledger_path=ledger)
    assert third.get("duplicate") is not True
    assert len(calls) == 2


# ---- 4: controlled errors -----------------------------------------------------------

def test_worker_error_envelope_is_classified(ledger):
    error = {"ok": False, "error": {"code": "rate_limited", "message": "429"}}
    result = director.handle_research_request(
        _req(), runner=_research_stub(json.dumps(error), exit_code=4),
        ledger_path=ledger)
    assert result == {"ok": False, "error": {"code": "rate_limited", "message": "429"}}
    records = json.loads(ledger.read_text(encoding="utf-8"))["requests"]
    assert records[-1]["status"] == "failed"


def test_unparseable_worker_output_is_extraction_failed(ledger):
    result = director.handle_research_request(
        _req(), runner=_research_stub("not json at all", exit_code=0),
        ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "extraction_failed"


def test_timeout_returns_truthful_timeout(ledger):
    result = director.handle_research_request(
        _req(), runner=_research_stub(timed_out=True), ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "timeout"
    records = json.loads(ledger.read_text(encoding="utf-8"))["requests"]
    assert records[-1]["status"] == "timeout"


# ---- 5: boundary isolation ----------------------------------------------------------

def test_golden_path_ledger_is_untouched_by_research(tmp_path):
    golden_ledger = tmp_path / "requests.json"
    research_ledger = tmp_path / "research_requests.json"
    director.handle_research_request(
        _req(), runner=_research_stub(json.dumps(_envelope())),
        ledger_path=research_ledger)
    assert not golden_ledger.exists()  # separate ledger by construction
    assert research_ledger.exists()


def test_research_runner_never_receives_caller_controlled_executables(ledger):
    calls: list = []
    director.handle_research_request(
        _req(), runner=_research_stub(json.dumps(_envelope()), calls=calls),
        ledger_path=ledger)
    argv = calls[0]["argv"]
    # caller input appears ONLY on stdin, never in argv
    for arg in argv:
        assert "identify outlier" not in arg