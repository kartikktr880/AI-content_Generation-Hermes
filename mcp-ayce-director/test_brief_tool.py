"""Stage 2.5 brief tool tests — protocol-level + boundary-level, no LLM,
no network, no real pipeline runs.

Covers: tool registration, strict input validation (IDs never paths),
controlled research-artifact resolution, successful brief invocation
(stub runner) with persistence + allowlist registration, digest
idempotency, truthful Stage 1 failure pass-through, registration
conflicts, no-fixture-fallback, security argv invariants, and
non-interference with the trigger_golden_path ledger.

Run with the director's isolated venv:
    set PYTHONPATH=<repo>\\src
    mcp-ayce-director\\.venv\\Scripts\\python -m pytest mcp-ayce-director\\test_brief_tool.py
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

sys.path.insert(0, str(HERE))
import server as director  # noqa: E402


# ---- helpers -----------------------------------------------------------------------

RESEARCH_ID = "res-20260920T090318Z-e8f295ac6ec8"
SCRIPT_ID = "brief-e8f295ac6ec8"


def _candidate(video_id, *, hook=None, hook_conf=0.0, views=None, velocity=None):
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": f"Title {video_id}",
        "channel": "Chan",
        "published_at": "2026-01-01",
        "view_count": views,
        "hook_summary": hook,
        "hook_confidence": hook_conf,
        "outlier_multiplier": None,
        "velocity_proxy": velocity,
        "cluster_id": 0 if views is not None else None,
        "confidence": 0.8,
        "observed_facts": [f"view_count: {views}"] if views is not None else [],
        "provenance": {"ingested_via": "ytsearch2:test",
                       "captions_status": "auto_generated"},
    }


def _artifact_dict(candidates, research_id=RESEARCH_ID):
    return {
        "schema_version": "1.0",
        "research_id": research_id,
        "objective": "Identify outlier opening-hook formats in the AI productivity niche",
        "niche": "AI productivity tools",
        "query": "AI productivity tools",
        "collected_at": "2026-09-20T09:03:18.260Z",
        "status": "PARTIAL",
        "tiers": ["OBSERVED_FACT", "DERIVED_METRIC", "MODEL_INFERENCE", "CREATIVE_HYPOTHESIS"],
        "candidates": candidates,
        "provenance": {"worker_version": "1.0",
                       "collected_at": "2026-09-20T09:03:18.260Z",
                       "request_digest": "ab" * 32},
    }


def _script_input_stdout(research_id=RESEARCH_ID):
    """What the REAL Stage 1 worker prints on success (shape-identical)."""
    return json.dumps({
        "production_id": research_id,
        "title": "Research brief: Identify outlier opening-hook formats",
        "scenes": [
            {"narration_text": "Here is an opening hook recorded by the research worker.",
             "visual_description": "High-contrast opening title card.",
             "asset_requirement": {"kind": "image", "description": "Opening visual",
                                   "requires_provenance": True}},
            {"narration_text": "This video is built directly from research evidence.",
             "visual_description": "Clean text card naming the research objective."},
            {"narration_text": "The top-ranked candidate, \"Title aaa\", 874,065 views.",
             "visual_description": "Side-by-side comparison graphic.",
             "asset_requirement": {"kind": "image", "description": "Comparison chart",
                                   "requires_provenance": True}},
            {"narration_text": "Every claim in this brief traces to research evidence.",
             "visual_description": "Plain closing card citing the research artifact."},
        ],
    }, indent=2, ensure_ascii=False)


def _brief_stub(stdout: str = "", exit_code: int = 0, timed_out: bool = False,
                stderr: str = "", calls: list | None = None):
    def runner(argv, cwd, env, timeout_s, stdin_text):
        if calls is not None:
            calls.append({"argv": argv, "cwd": cwd, "env": env,
                          "timeout_s": timeout_s, "stdin_text": stdin_text})
        return {"timed_out": timed_out, "exit_code": exit_code,
                "stdout": stdout, "stderr": stderr,
                "started_at": "s", "finished_at": "f"}
    return runner


def _never_runs_brief(argv, cwd, env, timeout_s, stdin_text):
    raise AssertionError("boundary attempted execution on an invalid request")


@pytest.fixture()
def brief_env(tmp_path, monkeypatch):
    """Fully isolated boundary: own research store, own ledgers, own
    generated-scripts dir, own (copied) scripts allowlist."""
    data_dir = tmp_path / "data"
    store = data_dir / "research"
    store.mkdir(parents=True)
    # the operator baseline = the real allowlist MINUS generated entries
    # (a real propose_script_brief may have registered `brief-*` scripts)
    baseline = json.loads((HERE / "scripts.json").read_text(encoding="utf-8"))
    baseline = {k: v for k, v in baseline.items()
                if not k.startswith(director.GENERATED_SCRIPT_PREFIX)}
    scripts = tmp_path / "scripts.json"
    scripts.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    monkeypatch.setenv("AYCE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AYCE_DIRECTOR_BRIEF_LEDGER", str(tmp_path / "brief_requests.json"))
    monkeypatch.setenv("AYCE_DIRECTOR_SCRIPTS", str(scripts))
    monkeypatch.setenv("AYCE_DIRECTOR_GENERATED_SCRIPTS", str(tmp_path / "generated_scripts"))
    return {"tmp_path": tmp_path, "store": store, "scripts": scripts,
            "generated": tmp_path / "generated_scripts",
            "ledger": tmp_path / "brief_requests.json"}


def _write_artifact(env, payload, research_id=RESEARCH_ID):
    (env["store"] / f"{research_id}.json").write_text(
        json.dumps(payload), encoding="utf-8")


def _allowlist(env):
    return json.loads(env["scripts"].read_text(encoding="utf-8"))


def _ledger(env):
    return json.loads(env["ledger"].read_text(encoding="utf-8"))["requests"]


# ---- 1: tool registration -----------------------------------------------------------

def test_propose_script_brief_is_registered():
    async def _run():
        tools = await director.mcp.list_tools()
        return {t.name for t in tools}
    names = asyncio.run(_run())
    assert names == {"trigger_golden_path", "execute_research_slice", "propose_script_brief"}


# ---- 2: input validation (never executes) -------------------------------------------

@pytest.mark.parametrize("payload", [
    {},
    {"research_id": ""},
    {"research_id": "   "},
    {"research_id": "not-a-res-id"},
    {"research_id": "res-"},                                # empty token
    {"research_id": "data/research/res-x.json"},            # a PATH, never accepted
    {"research_id": "C:\\Windows\\System32\\cmd.exe"},      # absolute path
    {"research_id": 123},                                   # wrong type
    {"research_id": "res-x", "bogus": 1},                   # unsupported field
])
def test_invalid_brief_requests_rejected_without_execution(brief_env, payload):
    result = director.handle_brief_request(payload, runner=_never_runs_brief)
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"
    assert _ledger(brief_env)[-1]["status"] == "rejected"


def test_unknown_artifact_is_truthful(brief_env):
    result = director.handle_brief_request(
        {"research_id": "res-20990101T000000Z-doesnotexist1"}, runner=_never_runs_brief)
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_artifact"
    # no fallback: the operator's fixture allowlist was NEVER touched
    assert set(_allowlist(brief_env)) == {"_comment", "documentary"}


# ---- 3: successful invocation (stub runner): persist + register ----------------------

def test_successful_brief_persists_registers_and_returns_compact_result(brief_env):
    _write_artifact(brief_env, _artifact_dict([
        _candidate("aaa", hook="A real recorded hook.", hook_conf=0.7,
                   views=874065, velocity=3591.4),
        _candidate("bbb", views=9000),
    ]))
    calls: list = []
    result = director.handle_brief_request(
        {"research_id": RESEARCH_ID},
        runner=_brief_stub(_script_input_stdout(),
                           stderr="brief_warning: sparse baseline\n", calls=calls))
    assert result["ok"] is True
    assert result["research_id"] == RESEARCH_ID
    assert result["script_id"] == SCRIPT_ID
    assert result["production_id"] == RESEARCH_ID          # provenance link
    assert result["scene_count"] == 4
    assert result["warnings"] == ["sparse baseline"]       # truthful degradation surfaced
    assert result["script_path"] == f"mcp-ayce-director/generated_scripts/{SCRIPT_ID}.json"
    assert "trigger_golden_path" in result["next_step"]
    # byte-exact persistence of the worker output (ScriptInput, not a copy)
    saved_text = (brief_env["generated"] / f"{SCRIPT_ID}.json").read_text(encoding="utf-8")
    assert json.loads(saved_text)["production_id"] == RESEARCH_ID
    # allowlist entry with provenance and matching content digest
    entry = _allowlist(brief_env)[SCRIPT_ID]
    assert entry["script"] == f"mcp-ayce-director/generated_scripts/{SCRIPT_ID}.json"
    assert entry["generated"]["research_id"] == RESEARCH_ID
    assert entry["generated"]["script_sha256"] == result["script_sha256"]
    # fixed argv, artifact JSON on stdin, server-controlled everything
    assert len(calls) == 1
    argv = calls[0]["argv"]
    assert argv[1:3] == ["-m", "ayce.research"] and "--brief" in argv and "--json" in argv
    assert json.loads(calls[0]["stdin_text"])["research_id"] == RESEARCH_ID
    assert calls[0]["cwd"] == str(director.REPO)
    assert calls[0]["env"]["PYTHONPATH"] == str(director.AYCE_SRC)
    # ledger records the succeeded brief
    assert _ledger(brief_env)[-1]["status"] == "succeeded"


def test_brief_is_deterministic_and_idempotent(brief_env):
    import hashlib
    _write_artifact(brief_env, _artifact_dict([
        _candidate("aaa", hook="A hook.", views=1000, velocity=5.0),
        _candidate("bbb", views=2000),
    ]))
    calls: list = []
    stub = _brief_stub(_script_input_stdout(), calls=calls)
    first = director.handle_brief_request({"research_id": RESEARCH_ID}, runner=stub)
    assert first["ok"] is True
    # repeating the SAME request must not re-run the worker at all
    second = director.handle_brief_request({"research_id": RESEARCH_ID},
                                           runner=_never_runs_brief)
    assert second["ok"] is True
    assert second.get("duplicate") is True
    assert second["script_id"] == first["script_id"]
    assert second["script_sha256"] == first["script_sha256"]
    assert len(calls) == 1  # the worker ran exactly once
    # the persisted file matches the recorded digest
    content = (brief_env["generated"] / f"{SCRIPT_ID}.json").read_text(encoding="utf-8")
    assert hashlib.sha256(content.encode("utf-8")).hexdigest() == first["script_sha256"]


# ---- 4: truthful Stage 1 failure pass-through (NO fixture fallback) ------------------

def test_insufficient_evidence_passes_through_and_registers_nothing(brief_env):
    _write_artifact(brief_env, _artifact_dict([]))  # no candidates
    envelope = {"ok": False, "error": {"code": "insufficient_evidence",
                                       "message": "no candidates; cannot fabricate"}}
    result = director.handle_brief_request(
        {"research_id": RESEARCH_ID}, runner=_brief_stub(json.dumps(envelope), exit_code=9))
    assert result["ok"] is False
    assert result["error"]["code"] == "insufficient_evidence"
    assert "script_id" not in result            # nothing executable is returned
    assert not list(brief_env["generated"].glob("*.json"))   # nothing persisted
    assert set(_allowlist(brief_env)) == {"_comment", "documentary"}  # no fallback
    assert _ledger(brief_env)[-1]["status"] == "failed"


def test_malformed_artifact_passes_through_truthfully(brief_env):
    _write_artifact(brief_env, _artifact_dict([]))
    envelope = {"ok": False, "error": {"code": "artifact_validation_failed",
                                       "message": "contract rejected"}}
    result = director.handle_brief_request(
        {"research_id": RESEARCH_ID}, runner=_brief_stub(json.dumps(envelope), exit_code=7))
    assert result["ok"] is False
    assert result["error"]["code"] == "artifact_validation_failed"
    assert set(_allowlist(brief_env)) == {"_comment", "documentary"}


def test_unparseable_worker_output_never_registers(brief_env):
    _write_artifact(brief_env, _artifact_dict([_candidate("aaa", views=10)]))
    result = director.handle_brief_request(
        {"research_id": RESEARCH_ID}, runner=_brief_stub("garbage output", exit_code=0))
    assert result["ok"] is False
    assert result["error"]["code"] == "brief_failed"
    assert not (brief_env["generated"] / f"{SCRIPT_ID}.json").exists()
    assert set(_allowlist(brief_env)) == {"_comment", "documentary"}


def test_timeout_is_truthful(brief_env):
    _write_artifact(brief_env, _artifact_dict([_candidate("aaa", views=10)]))
    result = director.handle_brief_request({"research_id": RESEARCH_ID},
                                           runner=_brief_stub(timed_out=True))
    assert result["ok"] is False
    assert result["error"]["code"] == "timeout"
    assert _ledger(brief_env)[-1]["status"] == "timeout"


# ---- 5: registration conflicts (never overwrite) --------------------------------------

def test_registration_never_overwrites_conflicting_generated_script(brief_env):
    _write_artifact(brief_env, _artifact_dict([_candidate("aaa", views=10)]))
    # pre-seed a CONFLICTING generated script (different content)
    generated_dir = brief_env["generated"]
    generated_dir.mkdir(parents=True)
    generated_file = generated_dir / f"{SCRIPT_ID}.json"
    generated_file.write_text(
        '{"production_id": "DIFFERENT", "title": "t", "scenes": []}', encoding="utf-8")
    result = director.handle_brief_request(
        {"research_id": RESEARCH_ID}, runner=_brief_stub(_script_input_stdout()))
    assert result["ok"] is False
    assert result["error"]["code"] == "script_conflict"
    # the pre-existing file content is untouched and nothing was registered
    assert json.loads(generated_file.read_text(encoding="utf-8"))["production_id"] == "DIFFERENT"
    assert set(_allowlist(brief_env)) == {"_comment", "documentary"}


def test_registration_refuses_to_shadow_operator_entries(brief_env):
    _write_artifact(brief_env, _artifact_dict([_candidate("aaa", views=10)]))
    # an operator-maintained manual entry under the generated namespace with
    # different content must NEVER be overwritten
    scripts = _allowlist(brief_env)
    scripts[SCRIPT_ID] = {"script": "some/other.json",
                          "assets_dir": "a", "narration_dir": "n",
                          "description": "operator entry"}
    brief_env["scripts"].write_text(json.dumps(scripts, indent=2), encoding="utf-8")
    result = director.handle_brief_request(
        {"research_id": RESEARCH_ID}, runner=_brief_stub(_script_input_stdout()))
    assert result["ok"] is False
    assert result["error"]["code"] == "script_conflict"
    assert _allowlist(brief_env)[SCRIPT_ID]["script"] == "some/other.json"


# ---- 6: boundary isolation + security argv ---------------------------------------------

def test_golden_path_ledger_is_untouched_by_brief(brief_env):
    golden_ledger = brief_env["tmp_path"] / "requests.json"
    _write_artifact(brief_env, _artifact_dict([_candidate("aaa", views=10)]))
    director.handle_brief_request({"research_id": RESEARCH_ID},
                                  runner=_brief_stub(_script_input_stdout()))
    assert not golden_ledger.exists()  # separate ledger by construction
    assert brief_env["ledger"].exists()


def test_brief_runner_receives_artifact_on_stdin_never_in_argv(brief_env):
    calls: list = []
    _write_artifact(brief_env, _artifact_dict([_candidate("aaa", hook="Hook", views=10)]))
    director.handle_brief_request({"research_id": RESEARCH_ID},
                                  runner=_brief_stub(_script_input_stdout(), calls=calls))
    argv = calls[0]["argv"]
    assert argv[0] == director._pinned_interpreter()
    assert argv[1:3] == ["-m", "ayce.research"] and argv[3] == "--brief" and argv[4] == "--json"
    # the artifact identifier travels ONLY on stdin — nothing caller-supplied in argv
    for arg in argv:
        assert "res-20260920" not in arg
    assert json.loads(calls[0]["stdin_text"])["research_id"] == RESEARCH_ID
    assert calls[0]["cwd"] == str(director.REPO)
    assert calls[0]["env"]["PYTHONPATH"] == str(director.AYCE_SRC)

