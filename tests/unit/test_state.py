import pytest

from ayce.state import (
    InvalidTransitionError,
    RunState,
    StageStatus,
    StateError,
)


def test_happy_path_transitions():
    run = RunState(run_id="run-1", job_id="job-1")
    run.set_stage("research", StageStatus.RUNNING)
    run.set_stage("research", StageStatus.SUCCEEDED)
    assert run.stage("research").status is StageStatus.SUCCEEDED
    assert run.stage("research").attempts == 1


def test_retry_path_counts_attempts():
    run = RunState(run_id="run-1", job_id="job-1")
    run.set_stage("render", StageStatus.RUNNING)
    run.set_stage("render", StageStatus.FAILED, error="encode failed")
    run.set_stage("render", StageStatus.RETRYING)
    run.set_stage("render", StageStatus.RUNNING)
    run.set_stage("render", StageStatus.FAILED, error="encode failed")
    assert run.stage("render").last_error == "encode failed"
    run.set_stage("render", StageStatus.RETRYING)
    run.set_stage("render", StageStatus.RUNNING)
    run.set_stage("render", StageStatus.SUCCEEDED)
    assert run.stage("render").attempts == 3
    # success clears the last error for the stage
    assert run.stage("render").last_error is None


def test_invalid_transition_rejected():
    run = RunState(run_id="run-1", job_id="job-1")
    with pytest.raises(InvalidTransitionError, match="pending -> succeeded"):
        run.set_stage("script", StageStatus.SUCCEEDED)


def test_succeeded_is_terminal():
    run = RunState(run_id="run-1", job_id="job-1")
    run.set_stage("script", StageStatus.RUNNING)
    run.set_stage("script", StageStatus.SUCCEEDED)
    with pytest.raises(InvalidTransitionError):
        run.set_stage("script", StageStatus.FAILED)
    with pytest.raises(InvalidTransitionError):
        run.set_stage("script", StageStatus.RUNNING)
    with pytest.raises(InvalidTransitionError):
        run.set_stage("script", StageStatus.RUNNING)


def test_unknown_status_string_rejected():
    run = RunState(run_id="run-1", job_id="job-1")
    with pytest.raises(StateError, match="unknown stage status"):
        run.set_stage("script", "teleporting")


def test_invalid_stage_name_rejected():
    run = RunState(run_id="run-1", job_id="job-1")
    with pytest.raises(StateError, match="invalid stage name"):
        run.set_stage("Bad Stage!", StageStatus.RUNNING)


def test_unknown_stage_lookup_raises():
    run = RunState(run_id="run-1", job_id="job-1")
    with pytest.raises(StateError, match="unknown stage"):
        run.stage("nope")


def test_persistence_roundtrip(tmp_path):
    path = tmp_path / "runs" / "run-1" / "state.json"
    run = RunState(run_id="run-1", job_id="job-1")
    run.set_stage("research", StageStatus.RUNNING)
    run.set_stage("research", StageStatus.FAILED, error="provider down")
    run.save(path)

    loaded = RunState.load(path)
    assert loaded.run_id == "run-1"
    rec = loaded.stage("research")
    assert rec.status is StageStatus.FAILED
    assert rec.attempts == 1
    assert rec.last_error == "provider down"


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(StateError, match="not found"):
        RunState.load(tmp_path / "absent.json")


def test_load_corrupt_file_raises(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateError, match="corrupt"):
        RunState.load(path)


def test_missing_field_rejected():
    with pytest.raises(StateError, match="missing required field"):
        RunState.from_dict({"run_id": "run-1"})  # job_id missing
