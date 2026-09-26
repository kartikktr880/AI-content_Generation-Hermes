"""H2 read-only AYCE MCP server tests (protocol-level, no Hermes, no LLM).

Drives the REAL server over the REAL MCP stdio transport (subprocess) and
asserts: exact tool surface, truthful data from the existing AYCE readers,
structured errors, path-traversal/absolute-path rejection, and — critically
— that a full session of tool calls does NOT mutate any run state or
artifact registry, and does not modify AYCE sources.

Run with the server's isolated venv:
    mcp-ayce-readonly/.venv/Scripts/python -m pytest mcp-ayce-readonly/test_server.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SERVER = HERE / "server.py"
AYCE_SRC = REPO / "src"


# ---- fixture: a deterministic AYCE runs root built from REAL AYCE structures -------


@pytest.fixture()
def runs_root(tmp_path):
    """Build a deterministic runs root using the REAL RunState/ArtifactRegistry
    classes (never touching the repo's data/ directory)."""
    import shutil

    root = tmp_path / "runs"
    root.mkdir()

    # run A: full lifecycle, 7 stages, plus an artifact registry
    run_a = root / "run-20260919T000000A-aaaa11111111"
    run_a.mkdir()
    from ayce.artifacts import ArtifactRegistry, ArtifactKind
    from ayce.ids import new_artifact_id
    from ayce.state import RunState, StageStatus

    run = RunState(run_id=run_a.name, job_id="job-20260919T000000A-aaaa11111111")
    for stage in (
        "script_to_scene", "asset_resolution", "narration_audio",
        "timeline", "captions", "production_render", "media_qa",
    ):
        run.set_stage(stage, StageStatus.RUNNING)
        run.set_stage(stage, StageStatus.SUCCEEDED)
    run.save(run_a / "state.json")

    registry = ArtifactRegistry(run_a, run_a.name)
    registry.register("script_to_scene", ArtifactKind.SCENE_MANIFEST, "scene_manifest.json",
                      {"schema_version": "1.0", "scenes": 5})
    registry.register("media_qa", ArtifactKind.QA_REPORT, "qa_report.json",
                      {"verdict": "PASS", "cues": 5})
    registry.save()

    # run B: failed narration stage, state only (no artifacts.json)
    run_b = root / "run-20260919T000000B-bbbb22222222"
    run_b.mkdir()
    failed = RunState(run_id=run_b.name, job_id="job-20260919T000000B-bbbb22222222")
    failed.set_stage("script_to_scene", StageStatus.RUNNING)
    failed.set_stage("script_to_scene", StageStatus.SUCCEEDED)
    failed.set_stage("narration_audio", StageStatus.RUNNING)
    failed.set_stage("narration_audio", StageStatus.FAILED, error="piper exited 2: boom")
    failed.save(run_b / "state.json")

    # decoy: a directory that is not a run (no state.json)
    (root / "not-a-run").mkdir()

    yield root

    shutil.rmtree(root, ignore_errors=True)


# ---- client helper: the REAL MCP stdio transport against the REAL server -----------


def make_env(runs_root: Path, learning_db: Path | None = None,
             policy_db: Path | None = None,
             publishing_db: Path | None = None,
             analytics_db: Path | None = None) -> dict:
    env = {k: v for k, v in os.environ.items()}
    env["AYCE_RUNS_ROOT"] = str(runs_root)
    env["PYTHONPATH"] = str(AYCE_SRC)
    if learning_db is not None:
        env["AYCE_LEARNING_DB"] = str(learning_db)
    if policy_db is not None:
        env["AYCE_POLICY_DB"] = str(policy_db)
    if publishing_db is not None:
        env["AYCE_PUBLISH_LEDGER"] = str(publishing_db)
    if analytics_db is not None:
        env["AYCE_ANALYTICS_DB"] = str(analytics_db)
    return env


def run_async(coro):
    return asyncio.run(coro)


def call_tool(runs_root: Path, name: str, arguments: dict | None = None,
              learning_db: Path | None = None,
              policy_db: Path | None = None,
              publishing_db: Path | None = None,
              analytics_db: Path | None = None):
    """Start the server as a subprocess, initialize, call one tool, return the
    parsed JSON result plus the initialized session's last state."""

    async def _one():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER)],
            env=make_env(runs_root, learning_db, policy_db,
                         publishing_db, analytics_db),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool(name, arguments or {})
                return tools, result

    return run_async(_one())


def call_tool_twice(runs_root: Path, name: str, arguments: dict | None = None):
    """Call a tool twice in ONE session (determinism + no-mutation probe)."""

    async def _two():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER)],
            env=make_env(runs_root),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                first = await session.call_tool(name, arguments or {})
                second = await session.call_tool(name, arguments or {})
                return first, second

    return run_async(_two())


def result_payload(result) -> dict:
    """Extract the JSON payload from an MCP tool result."""
    assert not getattr(result, "isError", False), result
    texts = [block.text for block in result.content if getattr(block, "type", "") == "text"]
    return json.loads("\n".join(texts))


# ---- 1/2: server starts; the tool surface is EXACTLY the three tools ---------------


def test_server_starts_and_exposes_exactly_seven_readonly_tools(runs_root):
    async def _probe():
        params = StdioServerParameters(
            command=sys.executable, args=[str(SERVER)], env=make_env(runs_root)
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                return init, tools

    init, tools = run_async(_probe())
    assert init.serverInfo.name == "ayce-readonly"
    names = sorted(t.name for t in tools.tools)
    # Stage 14: the Stage 11–13 tool set + the read-only
    # get_experiment_definitions composition projection; NO policy-write or
    # experiment-start tool exists (approve/promote/activate/rollback and
    # experiment approve/start are operator-only CLI boundaries, never
    # Hermes tools)
    assert names == ["get_active_policy", "get_experiment_candidates",
                     "get_experiment_definitions", "get_policy_consumptions",
                     "get_policy_effectiveness", "get_policy_lineage",
                     "get_run_artifacts", "get_run_state",
                     "get_validated_knowledge", "list_runs"]
    for forbidden in ("approve", "promote", "activate", "rollback",
                      "reject", "create_policy", "set_policy",
                      "start_experiment"):
        assert not any(forbidden in name for name in names), forbidden


def test_every_tool_declares_json_schema_input(runs_root):
    async def _probe():
        params = StdioServerParameters(
            command=sys.executable, args=[str(SERVER)], env=make_env(runs_root)
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.list_tools()

    tools = run_async(_probe()).tools
    by_name = {t.name: t for t in tools}
    assert set(by_name["get_run_state"].inputSchema["properties"]) == {"run_id"}
    assert set(by_name["get_run_artifacts"].inputSchema["properties"]) == {"run_id"}
    assert set(by_name["get_validated_knowledge"]
               .inputSchema["properties"]) == {"scope"}
    # list_runs takes no arguments → empty properties object
    assert by_name["list_runs"].inputSchema["properties"] == {}


# ---- 3: list_runs -------------------------------------------------------------------


def test_list_runs_finds_both_runs_and_skips_decoy(runs_root):
    tools, result = call_tool(runs_root, "list_runs")
    payload = result_payload(result)
    assert payload["ok"] is True
    assert payload["truncated"] is False
    ids = [r["run_id"] for r in payload["runs"]]
    # exactly the two real runs; the decoy directory is not listed
    assert "run-20260919T000000A-aaaa11111111" in ids
    assert "run-20260919T000000B-bbbb22222222" in ids
    assert "not-a-run" not in ids
    run_a = next(r for r in payload["runs"] if r["run_id"].endswith("A-aaaa11111111"))
    assert run_a["stage_count"] == 7
    assert run_a["stages_succeeded"] == 7
    assert run_a["stages_failed"] == 0
    run_b = next(r for r in payload["runs"] if r["run_id"].endswith("B-bbbb22222222"))
    assert run_b["stages_failed"] == 1


# ---- 4: get_run_state valid ----------------------------------------------------------


def test_get_run_state_returns_real_runstate(runs_root):
    _, result = call_tool(runs_root, "get_run_state",
                          {"run_id": "run-20260919T000000A-aaaa11111111"})
    payload = result_payload(result)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["run_id"] == "run-20260919T000000A-aaaa11111111"
    assert data["job_id"] == "job-20260919T000000A-aaaa11111111"
    assert set(data["stages"]) == {
        "script_to_scene", "asset_resolution", "narration_audio",
        "timeline", "captions", "production_render", "media_qa",
    }
    assert all(s["status"] == "succeeded" for s in data["stages"].values())


def test_get_run_state_shows_failed_stage_truthfully(runs_root):
    _, result = call_tool(runs_root, "get_run_state",
                          {"run_id": "run-20260919T000000B-bbbb22222222"})
    payload = result_payload(result)
    assert payload["ok"] is True
    narration = payload["data"]["stages"]["narration_audio"]
    assert narration["status"] == "failed"
    assert "piper exited 2" in narration["last_error"]


# ---- 5/7: structured errors (no crashes, no stack traces) ----------------------------


def test_get_run_state_unknown_run_structured_error(runs_root):
    _, result = call_tool(runs_root, "get_run_state", {"run_id": "run-does-not-exist-000000"})
    payload = result_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unknown_run"


def test_get_run_artifacts_missing_manifest_structured_error(runs_root):
    _, result = call_tool(runs_root, "get_run_artifacts",
                          {"run_id": "run-20260919T000000B-bbbb22222222"})
    payload = result_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_manifest"


# ---- 6: get_run_artifacts valid -------------------------------------------------------


def test_get_run_artifacts_returns_real_registry(runs_root):
    _, result = call_tool(runs_root, "get_run_artifacts",
                          {"run_id": "run-20260919T000000A-aaaa11111111"})
    payload = result_payload(result)
    assert payload["ok"] is True
    assert payload["artifact_count"] == 2
    by_kind = {a["kind"]: a for a in payload["artifacts"]}
    assert by_kind["scene_manifest"]["stage"] == "script_to_scene"
    assert by_kind["scene_manifest"]["metadata"]["scenes"] == 5
    assert by_kind["qa_report"]["metadata"]["verdict"] == "PASS"
    assert by_kind["qa_report"]["path"] == "qa_report.json"


# ---- 8/9: traversal and absolute-path rejection ---------------------------------------


@pytest.mark.parametrize("bad_run_id", [
    "../run-20260919T000000B-bbbb22222222",   # traversal via ..
    "sub/../../etc",                          # nested traversal
    "C:/Windows/System32",                    # absolute posix-style
    "C:\\Windows\\System32",                  # absolute windows-style
    "/etc/passwd",                            # root-absolute
    "..",                                     # parent itself
    ".",                                      # current dir
    "run id with spaces",                     # grammar violation
    "",                                       # empty
])
def test_traversal_and_absolute_paths_rejected(runs_root, bad_run_id):
    _, result = call_tool(runs_root, "get_run_state", {"run_id": bad_run_id})
    payload = result_payload(result)
    assert payload["ok"] is False, bad_run_id
    assert payload["error"]["code"] in {"invalid_run_id", "unknown_run", "path_escape"}


def test_symlink_escape_rejected(runs_root):
    """A symlink inside the runs root pointing OUTSIDE must be rejected by
    the resolved-inside-root guard. Skipped when the OS denies symlink
    creation (Windows without developer mode)."""
    escape_target = Path(os.environ.get("TEMP", str(runs_root)))  # outside runs root
    link = runs_root / "run-escape-symlink"
    try:
        link.symlink_to(escape_target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this OS/user")
    _, result = call_tool(runs_root, "get_run_state", {"run_id": "run-escape-symlink"})
    payload = result_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "path_escape"


# ---- 10/11: NO mutation of state, registry, or AYCE sources ---------------------------


def test_no_mutation_of_runs_root_or_ayce_sources(runs_root):
    state_a = runs_root / "run-20260919T000000A-aaaa11111111" / "state.json"
    artifacts_a = runs_root / "run-20260919T000000A-aaaa11111111" / "artifacts.json"
    state_b = runs_root / "run-20260919T000000B-bbbb22222222" / "state.json"
    ayce_files = sorted((AYCE_SRC / "ayce").glob("*.py"))

    def snapshot():
        return (
            state_a.read_bytes(),
            artifacts_a.read_bytes(),
            state_b.read_bytes(),
            tuple((f.name, f.stat().st_mtime_ns) for f in ayce_files),
        )

    before = snapshot()
    call_tool(runs_root, "list_runs")
    call_tool(runs_root, "get_run_state", {"run_id": "run-20260919T000000A-aaaa11111111"})
    call_tool(runs_root, "get_run_state", {"run_id": "run-20260919T000000B-bbbb22222222"})
    call_tool(runs_root, "get_run_artifacts", {"run_id": "run-20260919T000000A-aaaa11111111"})
    call_tool(runs_root, "get_run_state", {"run_id": "../run-20260919T000000B-bbbb22222222"})
    after = snapshot()

    assert before == after  # bytes identical + no AYCE source file touched


# ---- 12: deterministic, serializable responses -----------------------------------------


def test_responses_are_deterministic_across_calls(runs_root):
    args = {"run_id": "run-20260919T000000A-aaaa11111111"}
    first, second = call_tool_twice(runs_root, "get_run_artifacts", args)
    assert result_payload(first) == result_payload(second)
    first_s, second_s = call_tool_twice(runs_root, "list_runs")
    assert result_payload(first_s) == result_payload(second_s)


def test_responses_are_json_serializable_without_host_paths(runs_root):
    _, result = call_tool(runs_root, "get_run_artifacts",
                          {"run_id": "run-20260919T000000A-aaaa11111111"})
    payload = result_payload(result)
    serialized = json.dumps(payload)  # must be plain-JSON-serializable
    assert str(runs_root) not in serialized      # no host paths leak
    assert "Traceback" not in serialized         # no stack traces


# ---- 13: server source has no dangerous capabilities ------------------------------------


def test_server_module_has_no_dangerous_capabilities():
    source = SERVER.read_text(encoding="utf-8")
    forbidden = ["import subprocess", "import socket", "import shutil",
                 "import ctypes", "os.system(", "eval(", "exec(",
                 "__import__(", "shutil.rmtree", "os.remove(", "os.unlink(",
                 ".unlink(", "open("]
    for marker in forbidden:
        assert marker not in source, f"forbidden capability in server: {marker}"
    # the only file content it ever touches is state.json / artifacts.json
    assert "state.json" in source and "artifacts.json" in source
# ---- 14: Stage 7 get_validated_knowledge (Hermes read-only proof) -------------------


@pytest.fixture()
def learning_db(tmp_path):
    """A REAL learning database (real Stage 7 store) seeded with one
    validated knowledge record — built through the REAL Stage 7 API
    over a REAL Stage 6 analytics store with deterministic
    measurements. Skips honestly if the seeded evaluation turns out
    insufficient (never fabricated as validated)."""
    import sys as _sys
    _sys.path.insert(0, str(AYCE_SRC))
    from ayce.learning import (
        LearningStore, create_candidate, create_experiment,
        approve_experiment, assign_units, evaluate_experiment,
        curate_candidate,
    )
    from ayce.analytics.store import AnalyticsStore

    db = tmp_path / "learning.sqlite3"
    store = LearningStore(db)
    analytics = AnalyticsStore(tmp_path / "analytics.sqlite3")

    units = [f"vid{i:08d}" for i in range(6)]
    NOW = "2026-09-22T12:00:00.000Z"
    for i, unit in enumerate(units):
        observation_id = f"obs-test{i:020d}"
        assert analytics.insert_observation(
            observation_id=observation_id, source="DATA_API_V3",
            youtube_video_id=unit, scope="video", observed_at=NOW,
            metrics_requested=["views"], source_request={"api": "test"},
            raw_response=json.dumps({"items": [{"statistics": {
                "viewCount": str(1000 if i % 2 == 0 else 2000)}}]}),
            lineage={"run_id": f"run-{i:03d}", "objective_id": "obj-x",
                     "research_id": "res-x", "script_id": "brief-x",
                     "package_id": "pkg-x", "package_seal": "seal-x",
                     "destination": "dest", "youtube_video_id": unit},
            collected_at=NOW)
        analytics.insert_measurements(
            observation_id=observation_id, source="DATA_API_V3",
            youtube_video_id=unit, scope="video", observed_at=NOW,
            lineage={}, measurements=[{
                "metric_name": "views",
                "raw_value": str(1000 if i % 2 == 0 else 2000),
                "value": 1000.0 if i % 2 == 0 else 2000.0,
                "value_type": "integer", "unit": "count",
                "availability": "present"}],
            normalized_at=NOW)

    candidate = create_candidate(
        store, hypothesis="Videos like these perform better",
        scope="test-scope",
        evidence=[{"kind": "measurement", "ref": "obs-test0000000000000000",
                   "video_id": units[0]}],
        counterexamples=[], now=NOW)["candidate"]
    experiment = create_experiment(
        store, candidate_id=candidate["candidate_id"],
        hypothesis=candidate["hypothesis"], metric="views",
        control_variant="control", treatment_variant="treatment",
        population={"units": units}, assignment={"seed": "test-seed"},
        success_criterion={"min_control_n": 2, "min_treatment_n": 2,
                           "min_effect": 1.0},
        evaluation_window=None, holdout=None, now=NOW)["experiment"]
    approve_experiment(store, experiment["experiment_id"], now=NOW)
    assign_units(store, experiment["experiment_id"], units, now=NOW)
    evaluation = evaluate_experiment(store, analytics,
                                     experiment["experiment_id"],
                                     now=NOW)["evaluation"]
    if evaluation["status"] != "validated":
        pytest.skip(f"seeded evaluation not validated: "
                    f"{evaluation['status']}")
    curated = curate_candidate(store, candidate_id=candidate["candidate_id"],
                               evaluation_id=evaluation["evaluation_id"],
                               now=NOW)["knowledge"]
    yield db, curated


def test_get_validated_knowledge_returns_validated_knowledge(
        runs_root, learning_db):
    db, curated = learning_db
    _, result = call_tool(runs_root, "get_validated_knowledge", {},
                          learning_db=db)
    payload = result_payload(result)
    assert payload["ok"] is True
    assert payload["count"] == 1
    record = payload["knowledge"][0]
    assert record["knowledge_id"] == curated["knowledge_id"]
    assert record["knowledge_version"] == 1
    assert record["status"] == "active"
    # the statement is a FACT about the experiment, not a policy
    assert "not a causal claim" in record["statement"]
    assert "not a production policy" in record["statement"]
    assert "always use" not in record["statement"].lower()


def test_get_validated_knowledge_scope_filter(runs_root, learning_db):
    db, curated = learning_db
    _, hit = call_tool(runs_root, "get_validated_knowledge",
                       {"scope": curated["scope"]}, learning_db=db)
    assert result_payload(hit)["count"] == 1
    _, miss = call_tool(runs_root, "get_validated_knowledge",
                        {"scope": "no-such-scope"}, learning_db=db)
    payload = result_payload(miss)
    assert payload["ok"] is True and payload["count"] == 0


def test_get_validated_knowledge_missing_db_is_truthful(runs_root):
    _, result = call_tool(runs_root, "get_validated_knowledge", {},
                          learning_db=runs_root / "no" / "such.sqlite3")
    payload = result_payload(result)
    assert payload["ok"] is True
    assert payload["knowledge"] == []
    assert "no learning database" in payload["note"]


def test_knowledge_session_does_not_mutate_learning_db(runs_root, learning_db):
    db, _ = learning_db
    before = db.read_bytes()
    call_tool(runs_root, "get_validated_knowledge", {}, learning_db=db)
    call_tool(runs_root, "get_validated_knowledge", {"scope": "x"},
              learning_db=db)
    assert before == db.read_bytes()  # READ-ONLY: not one byte changed


# ---- 15: Stage 8 get_active_policy (Hermes read-only policy surface) ------------


@pytest.fixture()
def policy_db(tmp_path):
    """A REAL policy database seeded through the REAL Stage 8 API over
    REAL Stage 6 analytics + Stage 7 learning fixtures (deterministic
    measurements, no network, no fabricated repository evidence). Runs
    the FULL controlled promotion chain: candidate ? explicit approval
    ? promotion ? activation. Skips honestly if the seeded evaluation
    turns out insufficient (never fabricated as validated)."""
    import sys as _sys
    _sys.path.insert(0, str(AYCE_SRC))
    from ayce.learning import (
        LearningStore, create_candidate, create_experiment,
        approve_experiment, assign_units, evaluate_experiment,
        curate_candidate,
    )
    from ayce.learning.experiments import (
        _validate_definition, experiment_id_for, variant_for_unit)
    from ayce.analytics.store import AnalyticsStore
    from ayce.policy import (
        PolicyStore, approve_candidate, create_candidate as create_policy_candidate,
        promote_candidate, activate_policy,
    )

    NOW = "2026-09-22T12:00:00.000Z"
    SCOPE = "format:short_explainer"
    learning_db_path = tmp_path / "learning.sqlite3"
    policy_db_path = tmp_path / "policy.sqlite3"
    learning = LearningStore(learning_db_path)
    analytics = AnalyticsStore(tmp_path / "analytics.sqlite3")

    units = [f"vid{i:08d}" for i in range(6)]
    for i, unit in enumerate(units):
        observation_id = f"obs-test{i:020d}"
        assert analytics.insert_observation(
            observation_id=observation_id, source="DATA_API_V3",
            youtube_video_id=unit, scope="video", observed_at=NOW,
            metrics_requested=["views"], source_request={"api": "test"},
            raw_response=json.dumps({"items": [{"statistics": {
                "viewCount": str(1000 if i % 2 == 0 else 2000)}}]}),
            lineage={"package_id": "pkg-x", "package_seal": "seal-x",
                     "run_id": "run-x", "destination": "dest",
                     "youtube_video_id": unit, "script_id": "brief-x",
                     "research_id": "res-x", "objective_id": "obj-x"},
            collected_at=NOW)
        assert analytics.insert_measurements(
            observation_id=observation_id, source="DATA_API_V3",
            youtube_video_id=unit, scope="video", observed_at=NOW,
            lineage={"youtube_video_id": unit},
            measurements=[{"metric_name": "views",
                           "raw_value": str(1000 if i % 2 == 0 else 2000),
                           "value": float(1000 if i % 2 == 0 else 2000),
                           "value_type": "integer", "unit": "count",
                           "availability": "present"}],
            normalized_at=NOW)

    candidate = create_candidate(
        learning, hypothesis="Videos like these perform better",
        scope=SCOPE,
        evidence=[{"kind": "measurement", "ref": "obs-test0000000000000000",
                   "video_id": units[0]}],
        counterexamples=[], now=NOW)["candidate"]
    # choose a deterministic seed that gives a balanced split for the
    # EXACT definition the experiment is created with
    seed = None
    for i in range(500):
        trial = f"seed-{i}"
        definition = _validate_definition(
            candidate_id=candidate["candidate_id"],
            hypothesis=candidate["hypothesis"], metric="views",
            control_variant="control", treatment_variant="treatment",
            population={"units": units}, evaluation_window=None,
            success_criterion={"min_control_n": 2, "min_treatment_n": 2,
                               "min_effect": 1.0},
            assignment={"seed": trial}, holdout=None)
        exp_id = experiment_id_for(definition)
        variants = [variant_for_unit(exp_id, u, trial) for u in sorted(units)]
        if variants.count("control") >= 2 and variants.count("treatment") >= 2:
            seed = trial
            break
    assert seed is not None
    experiment = create_experiment(
        learning, candidate_id=candidate["candidate_id"],
        hypothesis=candidate["hypothesis"], metric="views",
        control_variant="control", treatment_variant="treatment",
        population={"units": units}, assignment={"seed": seed},
        success_criterion={"min_control_n": 2, "min_treatment_n": 2,
                           "min_effect": 1.0},
        evaluation_window=None, holdout=None, now=NOW)["experiment"]
    approve_experiment(learning, experiment["experiment_id"], now=NOW)
    assign_units(learning, experiment["experiment_id"], units, now=NOW)
    evaluation = evaluate_experiment(learning, analytics,
                                     experiment["experiment_id"],
                                     now=NOW)["evaluation"]
    if evaluation["status"] != "validated":
        pytest.skip(f"seeded evaluation not validated: "
                    f"{evaluation['status']}")
    curated = curate_candidate(learning, candidate_id=candidate["candidate_id"],
                               evaluation_id=evaluation["evaluation_id"],
                               now=NOW)["knowledge"]
    learning.close()
    analytics.close()

    # the FULL controlled Stage 8 promotion chain (operator-driven)
    store = PolicyStore(policy_db_path)
    compiled = create_policy_candidate(
        store, learning_db_path,
        knowledge_refs=[f"{curated['knowledge_id']}@"
                        f"{curated['knowledge_version']}"],
        scope=SCOPE, rationale="operator decision (test fixture)",
        now=NOW)["candidate"]
    approve_candidate(store, candidate_id=compiled["candidate_id"],
                      approved_by="operator-test", now=NOW)
    promoted = promote_candidate(store, learning_db_path,
                                 candidate_id=compiled["candidate_id"],
                                 now=NOW)["policy"]
    activate_policy(store, policy_id=promoted["policy_id"], now=NOW)
    store.close()
    yield policy_db_path, promoted


def test_get_active_policy_returns_the_active_policy(runs_root, policy_db):
    db, promoted = policy_db
    _, result = call_tool(runs_root, "get_active_policy",
                          {"scope": "format:short_explainer"},
                          policy_db=db)
    payload = result_payload(result)
    assert payload["ok"] is True
    policy = payload["policy"]
    assert policy is not None
    assert policy["policy_id"] == promoted["policy_id"]
    assert policy["policy_version"] == 1
    assert policy["status"] == "active"
    (rule,) = policy["rules"]
    assert rule["operator"] == "prefer_variant"
    assert rule["action"] == "prefer"
    assert rule["scope"] == "format:short_explainer"


def test_get_active_policy_scope_filter_is_exact(runs_root, policy_db):
    db, _ = policy_db
    _, miss = call_tool(runs_root, "get_active_policy",
                        {"scope": "topic:other"}, policy_db=db)
    payload = result_payload(miss)
    assert payload["ok"] is True and payload["policy"] is None
    assert "no active policy" in payload["note"]


def test_get_active_policy_missing_db_is_truthful(runs_root):
    _, result = call_tool(runs_root, "get_active_policy",
                          {}, policy_db=runs_root / "no" / "such.sqlite3")
    payload = result_payload(result)
    assert payload["ok"] is True and payload["policy"] is None
    assert "no policy database" in payload["note"]


def test_policy_session_does_not_mutate_policy_db(runs_root, policy_db):
    db, _ = policy_db
    before = db.read_bytes()
    call_tool(runs_root, "get_active_policy", {"scope": "format:short_explainer"},
              policy_db=db)
    call_tool(runs_root, "get_active_policy", {"scope": "topic:other"},
              policy_db=db)
    assert before == db.read_bytes()  # READ-ONLY: not one byte changed

# ---- 16: Stage 10 get_policy_consumptions (read-only observability) ------------


@pytest.fixture()
def consumption_run(tmp_path):
    """A REAL run directory (inside a REAL runs-root layout) with a REAL
    registered policy-consumption artifact, built through the REAL Stage 8
    + Stage 10 APIs over deterministic fixture evidence."""
    import hashlib
    import sys as _sys
    _sys.path.insert(0, str(AYCE_SRC))
    _sys.path.insert(0, str(REPO / "tests" / "unit"))
    from ayce.artifacts import ArtifactRegistry
    from ayce.policy import (
        PolicyStore, append_consumption, build_execution_context,
        record_consumption_artifact,
    )
    from test_policy import NOW, SCOPE, make_env, custom_pipeline
    from ayce.policy import (approve_candidate, promote_candidate,
                             activate_policy)

    runs_root = tmp_path / "runs"
    run_id = "run-20260922T100000Z-observab00001"
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    ArtifactRegistry(run_dir, run_id).save()

    _, analytics, learning = make_env(tmp_path / "data")
    knowledge = custom_pipeline(learning, analytics,
                                scope=SCOPE)
    store = PolicyStore(tmp_path / "policy.sqlite3")
    from ayce.policy import create_candidate as create_policy_candidate
    candidate = create_policy_candidate(
        store, learning.path,
        knowledge_refs=[f"{knowledge['knowledge_id']}@"
                        f"{knowledge['knowledge_version']}"],
        scope=SCOPE, rationale="operator decision (stage 10 exercise)",
        now=NOW)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-test", now=NOW)
    promoted = promote_candidate(store, learning.path,
                                 candidate_id=candidate["candidate_id"],
                                 now=NOW)["policy"]
    activate_policy(store, policy_id=promoted["policy_id"],
                    now=NOW)
    learning.close()
    analytics.close()

    context = build_execution_context(store.path,
                                      scope_candidates=(SCOPE,))
    event = append_consumption(tmp_path / "c.jsonl", run_id=run_id,
                               decision_id="req-20260922T100000Z-obs0000001",
                               decision="trigger_golden_path",
                               context=context, now=NOW)["event"]
    record_consumption_artifact(run_dir, run_id, event)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (run_dir / "artifacts.json",
                        run_dir / "policy_consumption.json",
                        store.path) if Path(p).is_file()}
    yield runs_root, run_id, event, tmp_path / "policy.sqlite3", before


def test_get_policy_consumptions_returns_structured_evidence(
        runs_root, consumption_run):
    c_runs, run_id, event, policy_db, _before = consumption_run
    _, result = call_tool(c_runs, "get_policy_consumptions",
                          {"run_id": run_id}, policy_db=policy_db)
    payload = result_payload(result)
    assert payload["ok"] is True
    assert payload["count"] == 1
    (record,) = payload["consumptions"]
    assert record["consumption_id"] == event["consumption_id"]
    assert record["policy_id"] == event["policy_id"]
    assert record["policy_version"] == 1
    assert record["scope"] == event["scope"]
    assert record["context_hash"] == event["context_hash"]
    assert record["run_id"] == run_id
    (verification,) = payload["verification"]
    assert verification["identity"] == "verified"
    assert verification["policy_state"] == "active"
    assert verification["content_match"] is True


def test_get_policy_consumptions_filters_and_missing(runs_root,
                                                     consumption_run):
    c_runs, run_id, event, policy_db, _before = consumption_run
    # no such run ? structured error
    _, missing = call_tool(c_runs, "get_policy_consumptions",
                           {"run_id": "run-20260922T100000Z-nonexist0001"},
                           policy_db=policy_db)
    payload = result_payload(missing)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unknown_run"
    # filter with a non-matching decision_id ? empty, truthful
    _, filtered = call_tool(c_runs, "get_policy_consumptions",
                            {"run_id": run_id,
                             "decision_id": "req-nothing"},
                            policy_db=policy_db)
    fpayload = result_payload(filtered)
    assert fpayload["ok"] is True and fpayload["count"] == 0
    assert fpayload["consumptions"] == []


def test_get_policy_consumptions_is_byte_readonly(runs_root, consumption_run):
    import hashlib
    c_runs, run_id, event, policy_db, before = consumption_run
    call_tool(c_runs, "get_policy_consumptions", {"run_id": run_id},
              policy_db=policy_db)
    call_tool(c_runs, "get_policy_consumptions",
              {"run_id": run_id, "policy_id": "pol-none"},
              policy_db=policy_db)
    after = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
             for p in before}
    assert before == after  # READ-ONLY: not one byte changed


def test_get_policy_consumptions_detects_mutation(runs_root, consumption_run):
    c_runs, run_id, event, policy_db, _before = consumption_run
    artifact = c_runs / run_id / "policy_consumption.json"
    mutated = json.loads(artifact.read_text(encoding="utf-8"))
    mutated["policy_version"] = 99
    artifact.write_text(json.dumps(mutated), encoding="utf-8")
    _, result = call_tool(c_runs, "get_policy_consumptions",
                          {"run_id": run_id}, policy_db=policy_db)
    payload = result_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "policy_consumption_failed"
    assert "mutated" in payload["error"]["message"]

# ---- 17: Stage 11 get_policy_lineage (cross-run read-only projection) ----------


def test_get_policy_lineage_run_direction(runs_root, consumption_run):
    c_runs, run_id, event, policy_db, before = consumption_run
    _, result = call_tool(c_runs, "get_policy_lineage",
                          {"run_id": run_id}, policy_db=policy_db)
    payload = result_payload(result)
    assert payload["ok"] is True
    assert payload["run_found"] is True
    assert payload["evidence_status"] == "verified"
    assert payload["count"] == 1
    (record,) = payload["policies"]
    assert record["policy_id"] == event["policy_id"]
    assert record["policy_version"] == 1
    assert record["context_hash"] == event["context_hash"]


def test_get_policy_lineage_policy_direction(runs_root, consumption_run):
    c_runs, run_id, event, policy_db, before = consumption_run
    _, result = call_tool(c_runs, "get_policy_lineage",
                          {"policy_id": event["policy_id"]},
                          policy_db=policy_db)
    payload = result_payload(result)
    assert payload["ok"] is True
    assert payload["run_count"] == 1
    (record,) = payload["runs"]
    assert record["run_id"] == run_id
    assert record["consumption_id"] == event["consumption_id"]
    # version filter: wrong version ? empty, truthful
    _, v2 = call_tool(c_runs, "get_policy_lineage",
                      {"policy_id": event["policy_id"],
                       "policy_version": 2}, policy_db=policy_db)
    assert result_payload(v2)["run_count"] == 0


def test_get_policy_lineage_requires_one_direction(runs_root, policy_db=None):
    _, both = call_tool(runs_root, "get_policy_lineage",
                        {"run_id": "run-x", "policy_id": "pol-x"},
                        policy_db=policy_db)
    assert result_payload(both)["ok"] is False
    assert result_payload(both)["error"]["code"] == "invalid_request"
    _, neither = call_tool(runs_root, "get_policy_lineage", {},
                           policy_db=policy_db)
    assert result_payload(neither)["error"]["code"] == "invalid_request"


def test_get_policy_lineage_is_byte_readonly(runs_root, consumption_run):
    import hashlib
    c_runs, run_id, event, policy_db, before = consumption_run
    call_tool(c_runs, "get_policy_lineage", {"run_id": run_id},
              policy_db=policy_db)
    call_tool(c_runs, "get_policy_lineage",
              {"policy_id": event["policy_id"]}, policy_db=policy_db)
    after = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
             for p in before}
    assert before == after  # READ-ONLY: not one byte changed
# ---- 18: Stage 12 get_policy_effectiveness (read-only evidence) -----------------


@pytest.fixture()
def effectiveness_fixture(tmp_path):
    """A deterministic Stage 12 evidence graph built through the REAL
    canonical owners: Stage 8/9/10 policy + consumption, a REAL Stage 5
    PublishLedger row and a REAL Stage 6 AnalyticsStore observation
    (deterministic values; never fabricated at query time)."""
    import sys as _sys
    _sys.path.insert(0, str(AYCE_SRC))
    _sys.path.insert(0, str(REPO / "tests" / "unit"))
    from ayce.analytics.store import AnalyticsStore
    from ayce.artifacts import ArtifactRegistry
    from ayce.publishing.ledger import PublishLedger
    from ayce.policy import (
        PolicyStore, activate_policy, append_consumption, approve_candidate,
        build_execution_context, create_candidate, promote_candidate,
        record_consumption_artifact,
    )
    from test_policy import NOW, SCOPE, custom_pipeline, make_env

    runs_root = tmp_path / "runs"
    run_id = "run-20260922T120000Z-effec0000001"
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True)
    ArtifactRegistry(run_dir, run_id).save()

    _, analytics, learning = make_env(tmp_path / "evidence")
    knowledge = custom_pipeline(learning, analytics, scope=SCOPE)
    store = PolicyStore(tmp_path / "policy.sqlite3")
    candidate = create_candidate(
        store, learning.path,
        knowledge_refs=[f"{knowledge['knowledge_id']}@"
                        f"{knowledge['knowledge_version']}"],
        scope=SCOPE, rationale="operator decision (stage 12 exercise)",
        now=NOW)["candidate"]
    approve_candidate(store, candidate_id=candidate["candidate_id"],
                      approved_by="operator-test", now=NOW)
    promoted = promote_candidate(store, learning.path,
                                 candidate_id=candidate["candidate_id"],
                                 now=NOW)["policy"]
    activate_policy(store, policy_id=promoted["policy_id"], now=NOW)
    learning.close()
    analytics.close()

    context = build_execution_context(store.path, scope_candidates=(SCOPE,))
    event = append_consumption(tmp_path / "c.jsonl", run_id=run_id,
                               decision_id="req-20260922T120000Z-eff000001",
                               decision="trigger_golden_path",
                               context=context, now=NOW)["event"]
    record_consumption_artifact(run_dir, run_id, event)

    # REAL Stage 5 ledger: the run IS published with one video id
    ledger_path = tmp_path / "publishing" / "ledger.sqlite3"
    ledger = PublishLedger(ledger_path)
    ledger.create(package_seal="seal-eff-0000000001",
                  package_id="pkg-eff-0000000001", run_id=run_id,
                  destination="youtube", privacy_status="private",
                  title="t", requested_metadata={}, now=NOW)
    ledger.update("seal-eff-0000000001", "youtube", now=NOW,
                  status="published", youtube_video_id="vidEff00001")
    ledger.close()

    # REAL Stage 6 analytics store: one lifetime Data-API observation
    analytics_path = tmp_path / "analytics" / "analytics.sqlite3"
    store6 = AnalyticsStore(analytics_path)
    lineage = {"youtube_video_id": "vidEff00001",
               "package_id": "pkg-eff-0000000001",
               "package_seal": "seal-eff-0000000001", "run_id": run_id,
               "destination": "youtube"}
    store6.insert_observation(
        observation_id="obs-eff00000000000000001", source="DATA_API_V3",
        youtube_video_id="vidEff00001", scope="video", observed_at=NOW,
        metrics_requested=["views", "likes"],
        source_request={}, raw_response="{}", lineage=lineage,
        collected_at=NOW)
    store6.insert_measurements(
        observation_id="obs-eff00000000000000001", source="DATA_API_V3",
        youtube_video_id="vidEff00001", scope="video", observed_at=NOW,
        lineage=lineage,
        measurements=[
            {"metric_name": "views", "raw_value": "1234", "value": 1234.0,
             "value_type": "integer", "unit": "count",
             "availability": "present"},
            {"metric_name": "likes", "raw_value": "0", "value": 0.0,
             "value_type": "integer", "unit": "count",
             "availability": "zero"}],
        normalized_at=NOW)
    store6.close()
    store.close()

    watched = [runs_root / run_id / "policy_consumption.json",
               runs_root / run_id / "artifacts.json",
               tmp_path / "policy.sqlite3", ledger_path, analytics_path]
    import hashlib
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in watched}
    return (runs_root, run_id, event, tmp_path / "policy.sqlite3",
            ledger_path, analytics_path, before)

def test_get_policy_effectiveness_run_direction(runs_root,
                                                effectiveness_fixture):
    (c_runs, run_id, event, policy_db, ledger_path, analytics_path,
     _before) = effectiveness_fixture
    _, result = call_tool(c_runs, "get_policy_effectiveness",
                          {"run_id": run_id}, policy_db=policy_db,
                          publishing_db=ledger_path,
                          analytics_db=analytics_path)
    payload = result_payload(result)
    assert payload["ok"] is True
    assert payload["evidence_status"] == "verified"
    assert payload["record_count"] == 1
    (record,) = payload["records"]
    assert record["policy_id"] == event["policy_id"]
    assert record["policy_version"] == 1
    assert record["policy_state"] == "active"
    assert record["publish_status"] == "published"
    assert record["publish"]["youtube_video_id"] == "vidEff00001"
    assert record["analytics_status"] == "available"
    assert record["missing_data_status"] == "metric_present"
    assert record["evidence_status"] == "valid"
    assert record["effectiveness_id"].startswith("pef-")
    metrics = {m["metric"]: m for m in record["measurements"]}
    assert metrics["views"]["value"] == 1234.0
    assert metrics["views"]["availability"] == "present"
    assert metrics["views"]["source"] == "DATA_API_V3"
    assert metrics["views"]["window_start"] is None  # lifetime semantics kept
    # zero is REAL data — never missing, never dropped
    assert metrics["likes"]["value"] == 0.0
    assert metrics["likes"]["availability"] == "zero"
    agg = payload["aggregation"]
    assert agg["consumed_runs"] == 1 and agg["published_runs"] == 1
    assert agg["metric_present_count"] == 2
    assert agg["metric_missing_count"] == 0
    assert agg["evidence_status"] == "descriptive_evidence"
    assert "no ranking" in agg["note"]


def test_get_policy_effectiveness_policy_direction(runs_root,
                                                   effectiveness_fixture):
    (c_runs, run_id, event, policy_db, ledger_path, analytics_path,
     _before) = effectiveness_fixture
    _, result = call_tool(c_runs, "get_policy_effectiveness",
                          {"policy_id": event["policy_id"]},
                          policy_db=policy_db, publishing_db=ledger_path,
                          analytics_db=analytics_path)
    payload = result_payload(result)
    assert payload["ok"] is True
    assert payload["aggregation"]["consumed_runs"] == 1
    groups = {g["metric"]: g
              for g in payload["aggregation"]["outcome_groups"]}
    assert groups["views"]["n"] == 1 and groups["views"]["mean"] == 1234.0
    assert groups["views"]["window_semantics"] == "lifetime_cumulative"
    assert groups["likes"]["mean"] == 0.0  # zero included as real data
    # version filter: no v2 → truthful empty + insufficient evidence
    _, v2 = call_tool(c_runs, "get_policy_effectiveness",
                      {"policy_id": event["policy_id"], "policy_version": 2},
                      policy_db=policy_db, publishing_db=ledger_path,
                      analytics_db=analytics_path)
    v2_payload = result_payload(v2)
    assert v2_payload["record_count"] == 0
    assert v2_payload["aggregation"]["evidence_status"] == \
        "insufficient_evidence"


def test_get_policy_effectiveness_requires_one_identity(runs_root):
    _, both = call_tool(runs_root, "get_policy_effectiveness",
                        {"run_id": "run-x", "policy_id": "pol-x"})
    assert result_payload(both)["error"]["code"] == "invalid_request"
    _, neither = call_tool(runs_root, "get_policy_effectiveness", {})
    assert result_payload(neither)["error"]["code"] == "invalid_request"


def test_get_policy_effectiveness_missing_run_is_structured(runs_root):
    _, result = call_tool(runs_root, "get_policy_effectiveness",
                          {"run_id": "run-20260922T120000Z-nonexist0001"})
    payload = result_payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "policy_effectiveness_invalid"


def test_get_policy_effectiveness_is_byte_readonly(runs_root,
                                                   effectiveness_fixture):
    import hashlib
    (c_runs, run_id, event, policy_db, ledger_path, analytics_path,
     before) = effectiveness_fixture
    call_tool(c_runs, "get_policy_effectiveness", {"run_id": run_id},
              policy_db=policy_db, publishing_db=ledger_path,
              analytics_db=analytics_path)
    call_tool(c_runs, "get_policy_effectiveness",
              {"policy_id": event["policy_id"]}, policy_db=policy_db,
              publishing_db=ledger_path, analytics_db=analytics_path)
    after = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
             for p in before}
    assert before == after  # READ-ONLY: not one byte changed in ANY store
# ---- 19: Stage 13 get_experiment_candidates (read-only intake evidence) ---------


def test_get_experiment_candidates_policy_direction(runs_root,
                                                    effectiveness_fixture):
    (c_runs, run_id, event, policy_db, ledger_path, analytics_path,
     _before) = effectiveness_fixture
    _, result = call_tool(c_runs, "get_experiment_candidates",
                          {"policy_id": event["policy_id"]},
                          policy_db=policy_db, publishing_db=ledger_path,
                          analytics_db=analytics_path)
    payload = result_payload(result)
    assert payload["ok"] is True
    assert payload["candidate_count"] >= 1
    (views,) = [c for c in payload["candidates"]
                if c["metric"]["name"] == "views"]
    assert views["policy_id"] == event["policy_id"]
    assert views["policy_version"] == 1
    assert views["candidate_id"].startswith("cand-")
    assert views["status"] in ("eligible", "insufficient_evidence",
                               "ineligible")
    # one REAL usable views measurement → below the Stage 7 >= 2-unit
    # floor → truthfully insufficient evidence (never negative evidence)
    assert views["sample_size"] == 1
    assert views["observed_summary"]["mean"] == 1234.0
    # zero likes is REAL data — it appears with its own identity, never
    # merged into views and never interpreted as a bad outcome
    assert "not a causal claim" in views["note"]
    forbidden = {"rank", "score", "winner", "effect", "p_value",
                 "caused_by", "recommendation"}
    assert not (forbidden & set(views))


def test_get_experiment_candidates_filters_and_identity(
        runs_root, effectiveness_fixture):
    (c_runs, run_id, event, policy_db, ledger_path, analytics_path,
     _before) = effectiveness_fixture
    # metric filter narrows deterministically; the views candidate id is
    # stable across calls (same evidence → same identity)
    _, first = call_tool(c_runs, "get_experiment_candidates",
                         {"policy_id": event["policy_id"],
                          "metric": "views"},
                         policy_db=policy_db, publishing_db=ledger_path,
                         analytics_db=analytics_path)
    _, second = call_tool(c_runs, "get_experiment_candidates",
                          {"policy_id": event["policy_id"],
                           "metric": "views"},
                          policy_db=policy_db, publishing_db=ledger_path,
                          analytics_db=analytics_path)
    p1, p2 = result_payload(first), result_payload(second)
    assert p1["candidates"] == p2["candidates"]
    candidate_id = p1["candidates"][0]["candidate_id"]
    # status filter: insufficient_evidence matches, eligible does not
    _, by_status = call_tool(c_runs, "get_experiment_candidates",
                             {"policy_id": event["policy_id"],
                              "status": "eligible"},
                             policy_db=policy_db,
                             publishing_db=ledger_path,
                             analytics_db=analytics_path)
    assert result_payload(by_status)["candidate_count"] == 0
    # show ONE candidate by its content-addressed id
    _, shown = call_tool(c_runs, "get_experiment_candidates",
                         {"candidate_id": candidate_id},
                         policy_db=policy_db, publishing_db=ledger_path,
                         analytics_db=analytics_path)
    assert result_payload(shown)["candidate"]["candidate_id"] == candidate_id
    # unknown candidate id → structured refusal (nothing stored)
    _, missing = call_tool(c_runs, "get_experiment_candidates",
                           {"candidate_id": "cand-0000000000000000"},
                           policy_db=policy_db, publishing_db=ledger_path,
                           analytics_db=analytics_path)
    assert result_payload(missing)["ok"] is False
    # no primary identity → structured invalid_request
    _, neither = call_tool(c_runs, "get_experiment_candidates", {},
                           policy_db=policy_db, publishing_db=ledger_path,
                           analytics_db=analytics_path)
    assert result_payload(neither)["error"]["code"] == "invalid_request"


def test_get_experiment_candidates_is_byte_readonly(
        runs_root, effectiveness_fixture):
    import hashlib
    (c_runs, run_id, event, policy_db, ledger_path, analytics_path,
     before) = effectiveness_fixture
    call_tool(c_runs, "get_experiment_candidates",
              {"policy_id": event["policy_id"]}, policy_db=policy_db,
              publishing_db=ledger_path, analytics_db=analytics_path)
    call_tool(c_runs, "get_experiment_candidates", {"metric": "views"},
              policy_db=policy_db, publishing_db=ledger_path,
              analytics_db=analytics_path)
    after = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
             for p in before}
    assert before == after  # READ-ONLY: not one byte changed in ANY store
# ---- 20: Stage 14 get_experiment_definitions (read-only proposals) --------------


@pytest.fixture()
def composition_fixture(runs_root, effectiveness_fixture):
    """The Stage 14 chain on top of the Stage 12/13 fixture: the intake
    candidate is APPROVED through the REAL Stage 13 boundary (existing
    learning store), yielding an approved candidate whose definition
    proposal can be composed read-only."""
    (c_runs, run_id, event, policy_db, ledger_path, analytics_path,
     before) = effectiveness_fixture
    from ayce.learning import LearningStore
    from ayce.policy.experiment_intake import (
        approve_candidate, candidates_for_policy,
    )
    learning_path = c_runs.parent / "learning" / "learning.sqlite3"
    now = "2026-09-22T12:00:00.000Z"
    # a SECOND real Stage 6 views observation for the same video: with
    # two real measurements the views candidate reaches the Stage 13
    # comparability floor and derives as ELIGIBLE
    from ayce.analytics.store import AnalyticsStore as _AnalyticsStore
    store6 = _AnalyticsStore(analytics_path)
    lineage2 = {"youtube_video_id": "vidEff00001",
                "package_id": "pkg-eff-0000000001",
                "package_seal": "seal-eff-0000000001", "run_id": run_id,
                "destination": "youtube"}
    store6.insert_observation(
        observation_id="obs-eff00000000000000002", source="DATA_API_V3",
        youtube_video_id="vidEff00001", scope="video",
        observed_at="2026-09-22T12:00:01.000Z",
        metrics_requested=["views"], source_request={}, raw_response="{}",
        lineage=lineage2, collected_at="2026-09-22T12:00:01.000Z")
    store6.insert_measurements(
        observation_id="obs-eff00000000000000002", source="DATA_API_V3",
        youtube_video_id="vidEff00001", scope="video",
        observed_at="2026-09-22T12:00:01.000Z", lineage=lineage2,
        measurements=[{"metric_name": "views", "raw_value": "2000",
                       "value": 2000.0, "value_type": "integer",
                       "unit": "count", "availability": "present"}],
        normalized_at="2026-09-22T12:00:01.000Z")
    store6.close()
    store = LearningStore(learning_path)
    candidates = candidates_for_policy(
        c_runs, event["policy_id"], policy_db_path=policy_db,
        ledger_path=ledger_path, analytics_db_path=analytics_path)
    views = next(c for c in candidates["candidates"]
                 if c["metric"]["name"] == "views")
    approval = approve_candidate(c_runs, views["candidate_id"], store,
                                 approved_by="operator-test", now=now,
                                 policy_db_path=policy_db,
                                 ledger_path=ledger_path,
                                 analytics_db_path=analytics_path)
    store.close()
    return (c_runs, run_id, event, policy_db, ledger_path, analytics_path,
            learning_path, views["candidate_id"], approval, before)
def test_get_experiment_definitions_candidate_direction(
        runs_root, composition_fixture):
    (c_runs, run_id, event, policy_db, ledger_path, analytics_path,
     learning_path, candidate_id, approval, _before) = composition_fixture
    _, result = call_tool(c_runs, "get_experiment_definitions",
                          {"candidate_id": candidate_id},
                          policy_db=policy_db, publishing_db=ledger_path,
                          analytics_db=analytics_path)
    payload = result_payload(result)
    assert payload["ok"] is True
    proposal = payload["proposal"]
    assert proposal["proposal_id"].startswith("expdef-")
    assert proposal["candidate_id"] == candidate_id
    assert proposal["policy_id"] == event["policy_id"]
    assert proposal["policy_version"] == 1
    assert proposal["candidate_status"] == "eligible"
    # the comparison comes from the CONSUMED POLICY's own rule — never
    # invented
    comparison = proposal["comparison"]
    assert comparison["source"] == "policy_rule"
    assert comparison["direction"] == "treatment_greater_than_control"
    assert comparison["treatment_variant"] != \
        comparison["control_variant"]
    # population = the OBSERVED published videos of the evidence — one
    # published video observed so far, BELOW the Stage 7 >= 2-unit
    # contract → the proposal is truthfully needs_operator_configuration
    # (the population is NEVER padded or invented)
    assert proposal["population"]["kind"] == "explicit_published_videos"
    assert proposal["population"]["unit_count"] == 1
    assert proposal["population"]["resolved"] is False
    assert "vidEff00001" in proposal["population"]["units"]
    assert proposal["status"] == "needs_operator_configuration"
    assert proposal["unresolved_fields"] == ["population_units"]
    # the assignment seed is NEVER invented
    assert proposal["assignment"]["seed"] is None
    assert proposal["assignment"]["resolved"] is False
    # a proposal is not an experiment — nothing exists in Stage 7
def test_get_experiment_definitions_show_and_policy_direction(
        runs_root, composition_fixture):
    (c_runs, run_id, event, policy_db, ledger_path, analytics_path,
     learning_path, candidate_id, approval, _before) = composition_fixture
    _, by_candidate = call_tool(c_runs, "get_experiment_definitions",
                                {"candidate_id": candidate_id},
                                policy_db=policy_db,
                                publishing_db=ledger_path,
                                analytics_db=analytics_path)
    proposal_id = result_payload(by_candidate)["proposal"]["proposal_id"]
    # show by proposal id → identical derived proposal (deterministic id)
    _, shown = call_tool(c_runs, "get_experiment_definitions",
                         {"proposal_id": proposal_id},
                         policy_db=policy_db, publishing_db=ledger_path,
                         analytics_db=analytics_path)
    assert result_payload(shown)["proposal"]["proposal_id"] == proposal_id
    # policy direction finds it too
    _, by_policy = call_tool(c_runs, "get_experiment_definitions",
                             {"policy_id": event["policy_id"]},
                             policy_db=policy_db, publishing_db=ledger_path,
                             analytics_db=analytics_path)
    payload = result_payload(by_policy)
    assert any(p["proposal_id"] == proposal_id
               for p in payload["proposals"])
    # unknown proposal id → structured refusal (nothing stored)
    _, missing = call_tool(c_runs, "get_experiment_definitions",
                           {"proposal_id": "expdef-0000000000000000"},
                           policy_db=policy_db, publishing_db=ledger_path,
                           analytics_db=analytics_path)
    assert result_payload(missing)["ok"] is False
    # no identity → structured invalid_request
    _, neither = call_tool(c_runs, "get_experiment_definitions", {},
                           policy_db=policy_db, publishing_db=ledger_path,
                           analytics_db=analytics_path)
    assert result_payload(neither)["error"]["code"] == "invalid_request"


def test_get_experiment_definitions_is_byte_readonly(
        runs_root, composition_fixture):
    import hashlib
    (c_runs, run_id, event, policy_db, ledger_path, analytics_path,
     learning_path, candidate_id, approval, _pre) = composition_fixture
    # baseline captured AFTER fixture setup (which legitimately wrote the
    # fixture's own evidence); the tool calls themselves must change
    # NOTHING
    watched = [c_runs / run_id / "policy_consumption.json",
               c_runs / run_id / "artifacts.json",
               policy_db, ledger_path, analytics_path, learning_path]
    before = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
              for p in watched if Path(p).is_file()}
    call_tool(c_runs, "get_experiment_definitions",
              {"candidate_id": candidate_id}, policy_db=policy_db,
              publishing_db=ledger_path, analytics_db=analytics_path)
    call_tool(c_runs, "get_experiment_definitions",
              {"policy_id": event["policy_id"]}, policy_db=policy_db,
              publishing_db=ledger_path, analytics_db=analytics_path)
    after = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
             for p in before}
    assert before == after  # READ-ONLY: not one byte changed in ANY store