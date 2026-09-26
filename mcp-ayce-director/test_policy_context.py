"""Stage 9 — Policy-aware director execution context tests.

Covers the §24 director-side matrix over the REAL stdio MCP transport
AND the boundary-level handle_request seam (stub runner, no real spawn):
no-policy truthfulness (policy_status none, no fabricated rules, ledger
records the policy reference), active-policy consumption (read-only
context, structured decision explanation, durable idempotent consumption
events, v1 != v2 events, rollback changes future consumption), fail-closed
corrupted policy (request rejected, NO run launched), and byte-level
immutability of the policy/learning/analytics stores plus the tool-surface
proof that NO policy-write tool exists.

Run with the director's isolated venv:
    mcp-ayce-director\\.venv\\Scripts\\python -m pytest mcp-ayce-director\\test_policy_context.py
"""

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SERVER = HERE / "server.py"

sys.path.insert(0, str(REPO / "tests" / "unit"))
sys.path.insert(0, str(HERE))

import server as director  # noqa: E402
from test_policy import NOW, SCOPE, compile_candidate, custom_pipeline  # noqa
from test_policy_execution import make_active_policy  # noqa: E402

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

_REPORT_OK = {
    "ok": True,
    "run_id": "run-20260922T000000Z-cafe00000001",
    "job_id": "job-20260920T000000Z-cafe00000001",
    "stages": [{"stage": s, "ok": True, "error": None} for s in (
        "script_to_scene", "asset_resolution", "narration_audio",
        "timeline", "captions", "production_render", "media_qa")],
    "qa_verdict": "PASS",
}


def _stub_ok(_calls=None):
    def runner(argv, cwd, env, timeout_s):
        return {"timed_out": False, "exit_code": 0,
                "stdout": json.dumps(_REPORT_OK), "stderr": ""}
    return runner


def _req(request_id="req-20260922T000000Z-policy000001",
         script_id="documentary", reason="stage 9 policy exercise"):
    return {"request_id": request_id, "command": "trigger_golden_path",
            "script_id": script_id, "reason": reason}


def _read_ledger(path: Path) -> list:
    return json.loads(Path(path).read_text(encoding="utf-8"))["requests"]


def _read_consumptions(path: Path) -> list:
    path = Path(path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def call_tool(env_extra: dict, name: str, arguments: dict | None = None,
              list_tools: bool = False):
    env = {k: v for k, v in os.environ.items()}
    env.update(env_extra)

    async def _run():
        params = StdioServerParameters(
            command=sys.executable, args=[str(SERVER)], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if list_tools:
                    return await session.list_tools()
                result = await session.call_tool(name, arguments or {})
                texts = [b.text for b in result.content
                         if getattr(b, "type", "") == "text"]
                return json.loads("\n".join(texts))

    return asyncio.run(_run())


def _active_policy_env(tmp_path):
    """A REAL Stage 8 active policy (deterministic fixture chain)."""
    return make_active_policy(tmp_path)


# ---- Exercise A: no policy (the real repository state) --------------------------


def test_no_policy_director_request_succeeds_and_is_truthful(tmp_path):
    ledger = tmp_path / "req.json"
    result = director.handle_request(
        _req(), runner=_stub_ok(), ledger_path=ledger)
    assert result["ok"] is True
    assert result["status"] == "succeeded"
    # truthful no-policy context — nothing fabricated
    assert result["policy"]["policy_status"] == "none"
    assert result["policy"]["policy_id"] is None
    assert result["policy_decision"]["reason"].startswith(
        "no active production policy")
    # the consumption evidence records the no-policy fact truthfully
    assert result["policy_consumption"]["created"] is True
    (event,) = _read_consumptions(ledger.parent / "policy_consumption.jsonl")
    assert event["policy_status"] == "none"
    assert event["policy_id"] is None
    assert event["run_id"] == _REPORT_OK["run_id"]
    # the ledger carries the policy reference for run correlation
    record = _read_ledger(ledger)[-1]
    assert record["status"] == "succeeded"
    assert record["policy"]["policy_status"] == "none"
    assert record["policy_consumption_id"] == \
        result["policy_consumption"]["consumption_id"]


def test_no_policy_stdio_flow_and_tool_surface(tmp_path):
    ledger = tmp_path / "requests.json"
    stub_result = tmp_path / "stub_result.json"
    stub_result.write_text(json.dumps(
        {"exit_code": 0, "stdout": json.dumps(_REPORT_OK), "stderr": ""}),
        encoding="utf-8")
    result = call_tool({
        "AYCE_DIRECTOR_LEDGER": str(ledger),
        "AYCE_DIRECTOR_SCRIPTS": str(HERE / "scripts.json"),
        "AYCE_POLICY_DB": str(tmp_path / "missing.sqlite3"),
        "AYCE_DIRECTOR_PYTHON": sys.executable,
        "AYCE_DIRECTOR_STUB_RUNNER": "1",
        "AYCE_DIRECTOR_STUB_RESULT": str(stub_result),
    }, "trigger_golden_path", {
        "request_id": "req-20260922T000000Z-stdio00001",
        "command": "trigger_golden_path", "script_id": "documentary",
        "reason": "no-policy stdio exercise",
    })
    assert result["ok"] is True
    assert result["policy"]["policy_status"] == "none"
    assert result["policy_decision"]["policy_status"] == "none"
    # consumption event written next to the ledger (isolated)
    events = ledger.parent / "policy_consumption.jsonl"
    assert events.is_file()
    (log,) = _read_consumptions(events)
    assert log["policy_status"] == "none"
    assert log["run_id"] == _REPORT_OK["run_id"]
    # tool surface: the three director tools and NOTHING policy-mutating
    tools = call_tool({
        "AYCE_DIRECTOR_LEDGER": str(ledger),
        "AYCE_POLICY_DB": str(tmp_path / "missing.sqlite3"),
    }, "", list_tools=True)
    names = {t.name for t in tools.tools}
    assert names == {"trigger_golden_path", "propose_script_brief",
                     "execute_research_slice"}
    for forbidden in ("approve", "promote", "activate", "rollback",
                      "reject", "candidate", "policy"):
        assert not any(forbidden in name for name in names), forbidden


# ---- Exercise B: controlled active policy ----------------------------------------


def test_active_policy_director_request_consumes_readonly(tmp_path,
                                                          monkeypatch):
    store_path, _, _policy = _active_policy_env(tmp_path)
    monkeypatch.setenv("AYCE_POLICY_DB", str(store_path))
    monkeypatch.setenv("AYCE_POLICY_SCOPE_CANDIDATES", f"{SCOPE},global")
    ledger = tmp_path / "req.json"
    result = director.handle_request(
        _req(), runner=_stub_ok(), ledger_path=ledger)
    assert result["ok"] is True
    # structured policy reference (§5): id/version/scope/hashes preserved
    assert result["policy"]["policy_status"] == "active"
    assert result["policy"]["policy_version"] == 1
    assert result["policy"]["scope"] == SCOPE
    assert result["policy"]["context_hash"].startswith("sha256:")
    assert result["policy"]["policy_content_hash"].startswith("sha256:")
    # the decision explanation is factual — no causal language (§12)
    decision = result["policy_decision"]
    assert decision["policy_status"] == "active"
    assert decision["variant_preference"]["binding"] == "preference"
    assert decision["variant_preference"]["preferred"] == "treatment"
    text = json.dumps(decision).lower()
    for banned in ("always use", "must use", "guarantee", "causally",
                   "perform better"):
        assert banned not in text
    # the consumption event exists and references the policy + run
    assert result["policy_consumption"]["created"] is True
    (event,) = _read_consumptions(ledger.parent / "policy_consumption.jsonl")
    assert event["policy_id"] == result["policy"]["policy_id"]
    assert event["policy_version"] == 1
    assert event["run_id"] == _REPORT_OK["run_id"]
    # the context the director received matches the ledger reference
    assert event["context_hash"] == result["policy"]["context_hash"]


def test_corrupted_active_policy_fails_closed(tmp_path, monkeypatch):
    store_path, _, _policy = _active_policy_env(tmp_path)
    monkeypatch.setenv("AYCE_POLICY_DB", str(store_path))
    monkeypatch.setenv("AYCE_POLICY_SCOPE_CANDIDATES", f"{SCOPE},global")
    # TAMPER with the active policy content behind the store's back
    connection = sqlite3.connect(store_path)
    with connection:
        connection.execute("UPDATE policies SET rules = '[]'")
    connection.close()
    before = Path(store_path).read_bytes()

    ledger = tmp_path / "req.json"

    def _never_runs(argv, cwd, env, timeout_s):
        raise AssertionError("a corrupted policy must NOT launch a run")

    result = director.handle_request(_req(), runner=_never_runs,
                                     ledger_path=ledger)
    assert result["ok"] is False
    assert result["error"]["code"] == "policy_context_invalid"
    # fail closed in the ledger too — and NO run was launched
    record = _read_ledger(ledger)[-1]
    assert record["status"] == "rejected"
    assert record["policy_status"] == "invalid"
    assert _read_consumptions(ledger.parent /
                              "policy_consumption.jsonl") == []
    # the tampered store was not touched by the failed decision
    assert Path(store_path).read_bytes() == before


def test_v1_v2_and_rollback_consumption_events(tmp_path, monkeypatch):
    _, analytics, learning = make_env_fixture(tmp_path / "fixture")
    from ayce.policy import (
        PolicyStore, activate_policy, approve_candidate, promote_candidate,
        rollback_policy,
    )
    store = PolicyStore(tmp_path / "policy.sqlite3")

    def promote_and_activate(knowledge):
        candidate = compile_candidate(store, learning, knowledge)["candidate"]
        approve_candidate(store, candidate_id=candidate["candidate_id"],
                          approved_by="operator-a", now=NOW)
        policy = promote_candidate(store, learning.path,
                                   candidate_id=candidate["candidate_id"],
                                   now=NOW)["policy"]
        activate_policy(store, policy_id=policy["policy_id"], now=NOW)
        return policy

    knowledge_1 = custom_pipeline(learning, analytics,
                                  hypothesis="first confirmation")
    policy_1 = promote_and_activate(knowledge_1)
    knowledge_2 = custom_pipeline(learning, analytics,
                                  units=[f"vid1{i:02d}" for i in range(6)],
                                  hypothesis="second confirmation")
    promote_and_activate(knowledge_2)
    store.close()
    learning.close()
    analytics.close()

    ledger = tmp_path / "req.json"
    monkeypatch.setenv("AYCE_POLICY_DB", str(store.path))
    monkeypatch.setenv("AYCE_POLICY_SCOPE_CANDIDATES", f"{SCOPE},global")

    # decision consumed under v2
    result_v2 = director.handle_request(
        _req("req-20260922T000000Z-v2000000001"), runner=_stub_ok(),
        ledger_path=ledger)
    assert result_v2["policy"]["policy_version"] == 2
    consumption_v2 = result_v2["policy_consumption"]["consumption_id"]

    # EXPLICIT operator rollback → future consumption references the
    # restored active version
    reopened = PolicyStore(store.path)
    rollback_policy(reopened, policy_id=policy_1["policy_id"], now=NOW)
    reopened.close()

    result_v1 = director.handle_request(
        _req("req-20260922T000000Z-v1000000001"), runner=_stub_ok(),
        ledger_path=ledger)
    assert result_v1["policy"]["policy_version"] == 1
    consumption_v1 = result_v1["policy_consumption"]["consumption_id"]
    assert consumption_v1 != consumption_v2

    log = _read_consumptions(ledger.parent / "policy_consumption.jsonl")
    assert [e["policy_version"] for e in log] == [2, 1]
    assert len({e["consumption_id"] for e in log}) == 2


def make_env_fixture(base):
    from test_policy import make_env
    return make_env(base)


def test_director_flow_does_not_mutate_policy_learning_analytics(
        tmp_path, monkeypatch):
    import hashlib
    from ayce.policy import PolicyStore

    _, analytics, learning = make_env_fixture(tmp_path / "fixture")
    store_path = policy_store_fixture(tmp_path, learning, analytics)
    monkeypatch.setenv("AYCE_POLICY_DB", str(store_path))
    monkeypatch.setenv("AYCE_POLICY_SCOPE_CANDIDATES", f"{SCOPE},global")
    ledger = tmp_path / "req.json"

    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    watched = {"policy": digest(store_path),
               "learning": digest(learning.path),
               "analytics": digest(analytics.path)}

    result = director.handle_request(_req(), runner=_stub_ok(),
                                     ledger_path=ledger)
    assert result["ok"] is True
    assert digest(store_path) == watched["policy"]
    assert digest(learning.path) == watched["learning"]
    assert digest(analytics.path) == watched["analytics"]
    reopened = PolicyStore(store_path)
    assert [p["status"] for p in reopened.list_policies()] == ["active"]
    reopened.close()


def policy_store_fixture(tmp_path, learning, analytics):
    from ayce.policy import (
        PolicyStore, activate_policy, approve_candidate, promote_candidate,
    )
    store = PolicyStore(tmp_path / "policy.sqlite3")
    knowledge = custom_pipeline(learning, analytics)
    candidate = compile_candidate(store, learning, knowledge)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-a", now=NOW)
    promote_candidate(store, learning.path,
                      candidate_id=candidate["candidate_id"], now=NOW)
    activate_policy(store,
                    policy_id=store.list_policies()[0]["policy_id"], now=NOW)
    store.close()
    return store.path



