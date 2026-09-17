from ayce.ids import new_artifact_id, new_job_id, new_run_id

import re


def test_run_id_format():
    run_id = new_run_id()
    assert re.fullmatch(r"run-\d{8}T\d{6}Z-[0-9a-f]{12}", run_id)


def test_job_id_and_artifact_id_prefixes():
    assert new_job_id().startswith("job-")
    assert new_artifact_id().startswith("art-")


def test_ids_are_unique():
    seen = {new_run_id() for _ in range(200)}
    assert len(seen) == 200
