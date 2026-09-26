"""Stage 13 — Experiment intake boundary tests (deterministic fixtures).

Extends the Stage 12 evidence graph (REAL Stage 5 ledger + REAL Stage 6
analytics + REAL Stage 8/9/10 policy/consumption) and verifies:

- the §16 fixture matrix (A/B/C/D/E/F/G eligibility landscape);
- deterministic content-addressed identity (same evidence → same id;
  changed evidence / metric / policy version → NEW id);
- fail-closed failure injection (§17): invalid/corrupt/mismatched
  evidence NEVER becomes a candidate; missing/unavailable data is NEVER
  negative evidence;
- the explicit approval boundary (§12): approval materializes the
  candidate into the EXISTING Stage 7 learning store ONLY (no experiment
  is created, approved or started); insufficient/ineligible/unknown
  candidates are refused;
- byte-level read-only safety of every read direction.
"""

import hashlib
import json
from pathlib import Path

import pytest

from ayce.analytics.store import AnalyticsStore
from ayce.learning import LearningStore
from ayce.policy import PolicyError
from ayce.policy.experiment_intake import (
    CANDIDATE_ID_RE,
    INTAKE_NOTE,
    MIN_SAMPLE_SIZE,
    approve_candidate,
    candidates_for_metric,
    candidates_for_policy,
    show_candidate,
    verify_intake,
)

from test_policy import NOW
from test_policy_effectiveness import (
    add_observation,
    eff,
    repo as stage12_repo,  # the full Stage 12 evidence graph (A–G)
    stat,
)


def digests(paths) -> dict:
    return {str(p): hashlib.sha256(Path(p).read_bytes()).hexdigest()
            for p in paths if Path(p).is_file()}


def store_sources(repo):
    tmp = repo["tmp_path"]
    return [tmp / "data" / "publishing" / "ledger.sqlite3",
            tmp / "data" / "analytics" / "analytics.sqlite3",
            repo["store_path"]]


def run_sources(repo):
    sources = []
    for run_dir in sorted((repo["tmp_path"] / "data" / "runs").iterdir()):
        for name in ("state.json", "artifacts.json",
                     "policy_consumption.json"):
            candidate = run_dir / name
            if candidate.is_file():
                sources.append(candidate)
    return sources


def by_key(candidates):
    """Map candidates by (policy_version, metric name, source, window)."""
    return {(c["policy_version"], c["metric"]["name"], c["metric"]["source"],
             c["metric"]["window_start"]): c for c in candidates}


# ---- §16: the eligibility landscape --------------------------------------------


def test_policy_direction_eligibility_landscape(stage12_repo):
    repo = stage12_repo
    report = candidates_for_policy(
        repo["tmp_path"] / "data" / "runs", repo["policy_1"]["policy_id"],
        **eff(repo["tmp_path"], repo["store_path"]))
    assert report["ok"] is True
    assert report["aggregation"]["eligible"] == 2
    assert report["aggregation"]["insufficient_evidence"] == 3
    assert report["aggregation"]["ineligible"] == 0
    landscape = by_key(report["candidates"])
    # A (1000) + G (10, 30, 20): REAL lifetime Data-API views → eligible
    lifetime = landscape[(1, "views", "DATA_API_V3", None)]
    assert lifetime["status"] == "eligible"
    assert lifetime["sample_size"] == 4
    assert lifetime["observed_summary"]["min"] == 10.0
    assert lifetime["observed_summary"]["max"] == 1000.0
    assert lifetime["observed_summary"]["median"] == 25.0
    # A (50) + B (200): REAL windowed Analytics-API views → eligible
    windowed = landscape[(1, "views", "ANALYTICS_API_V2", "2026-09-13")]
    assert windowed["status"] == "eligible"
    assert windowed["sample_size"] == 2
    assert windowed["metric"]["window_semantics"] == "windowed"
    # zero is REAL data but ONE observation is below the Stage 7 >= 2-unit
    # floor → insufficient evidence (NEVER ineligible, NEVER negative)
    likes = landscape[(1, "likes", "DATA_API_V3", None)]
    assert likes["status"] == "insufficient_evidence"
    assert likes["sample_size"] == 1 and likes["observed_summary"]["n"] == 1
    # missing / unavailable metrics contribute NOTHING usable → still
    # insufficient evidence with explicit exclusion reasons (never zero)
    comments = landscape[(1, "comments", "DATA_API_V3", None)]
    assert comments["status"] == "insufficient_evidence"
    assert comments["sample_size"] == 0
    assert comments["runs_not_contributing"][0]["reason"] == \
        "metric_missing_or_unavailable"
    subs = landscape[(1, "subscribersGained", "DATA_API_V3", None)]
    assert subs["status"] == "insufficient_evidence"
    assert subs["sample_size"] == 0
    # the boundary statement rides on every candidate
    assert all(c["note"] == INTAKE_NOTE for c in report["candidates"])
    # no ranking / winner / score / causal field exists anywhere
    for candidate in report["candidates"]:
        assert not ({"rank", "score", "winner", "effect", "p_value",
                     "caused_by", "recommendation"} & set(candidate))


def test_unpublished_and_no_policy_runs_yield_nothing(stage12_repo):
    repo = stage12_repo
    # policy v2: C published but analytics NOT collected, D unpublished —
    # no usable observational evidence → zero candidates (truthful)
    v2 = candidates_for_policy(
        repo["tmp_path"] / "data" / "runs", repo["policy_2"]["policy_id"],
        **eff(repo["tmp_path"], repo["store_path"]))
    assert v2["ok"] is True and v2["candidate_count"] == 0
    # the no-policy run F appears nowhere in any candidate
    everything = candidates_for_metric(
        repo["tmp_path"] / "data" / "runs", "views",
        **eff(repo["tmp_path"], repo["store_path"]))
    for candidate in everything["candidates"]:
        assert repo["run_f"] not in candidate["run_ids"]
        assert candidate["policy_id"] in (repo["policy_1"]["policy_id"],
                                          repo["policy_2"]["policy_id"])


# ---- §8: deterministic, evidence-bound identity ---------------------------------


def test_same_evidence_yields_same_candidate_identity(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    first = candidates_for_policy(runs_root, repo["policy_1"]["policy_id"],
                                  **paths)
    second = candidates_for_policy(runs_root, repo["policy_1"]["policy_id"],
                                   **paths)
    assert [c["candidate_id"] for c in first["candidates"]] == \
        [c["candidate_id"] for c in second["candidates"]]
    assert all(CANDIDATE_ID_RE.fullmatch(c["candidate_id"])
               for c in first["candidates"])
    verification = verify_intake(runs_root, **paths)
    assert verification["ok"] is True
    assert verification["deterministic"] is True
    assert verification["consistent"] is True


def test_changed_evidence_yields_new_identity(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    before = by_key(candidates_for_policy(
        runs_root, repo["policy_1"]["policy_id"], **paths)["candidates"])
    # NEW observational evidence for run G's video (a real new Stage 6
    # measurement row) → the lifetime-views candidate is a NEW identity
    add_observation(repo["tmp_path"], "vidEffG00001", repo["run_g"],
                    "obs-effg000000000004", [stat("views", 40)])
    after = by_key(candidates_for_policy(
        runs_root, repo["policy_1"]["policy_id"], **paths)["candidates"])
    assert before[(1, "views", "DATA_API_V3", None)]["candidate_id"] != \
        after[(1, "views", "DATA_API_V3", None)]["candidate_id"]
    assert after[(1, "views", "DATA_API_V3", None)]["sample_size"] == 5
    # the OLD id no longer derives — nothing is stored, history is not
    # rewritten, and show_candidate says so truthfully (fail closed)
    with pytest.raises(PolicyError) as excinfo:
        show_candidate(runs_root,
                       before[(1, "views", "DATA_API_V3",
                               None)]["candidate_id"], **paths)
    assert excinfo.value.code == "policy_intake_invalid"


def test_different_metric_and_policy_version_yield_new_identities(
        stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    # give policy v2 REAL usable evidence: windowed observations for run
    # C's video (run C consumed v2 and is published)
    add_observation(repo["tmp_path"], "vidEffC00001", repo["run_c"],
                    "obs-effc000000000001", [stat("views", 500)],
                    source="ANALYTICS_API_V2",
                    window=("2026-09-13", "2026-09-19"))
    add_observation(repo["tmp_path"], "vidEffC00001", repo["run_c"],
                    "obs-effc000000000002", [stat("views", 700)],
                    source="ANALYTICS_API_V2",
                    window=("2026-09-20", "2026-09-26"))
    v1 = by_key(candidates_for_policy(
        runs_root, repo["policy_1"]["policy_id"], **paths)["candidates"])
    v2 = by_key(candidates_for_policy(
        runs_root, repo["policy_2"]["policy_id"], **paths)["candidates"])
    # v2 now derives candidates from C's real evidence — one per window
    # (identities never merge across windows)
    v2_windowed = [c for c in v2.values()
                   if c["metric"]["source"] == "ANALYTICS_API_V2"]
    assert len(v2_windowed) == 2
    assert all(c["status"] == "insufficient_evidence"
               for c in v2_windowed)  # one observation per window
    # different policy version → different candidate identity space
    v1_ids = {c["candidate_id"] for c in v1.values()}
    v2_ids = {c["candidate_id"] for c in v2.values()}
    assert not (v1_ids & v2_ids)
    # different metric → different identity within the same policy
    metric_report = candidates_for_policy(
        runs_root, repo["policy_1"]["policy_id"], **paths)
    ids = {c["candidate_id"] for c in metric_report["candidates"]}
    assert len(ids) == len(metric_report["candidates"])


# ---- §17: failure injection (fail closed, never negative evidence) --------------


def test_incomplete_metric_identity_is_ineligible(stage12_repo):
    repo = stage12_repo
    # a windowed observation MISSING its timezone: the metric identity is
    # incomplete → the candidate is structurally INELIGIBLE (never guessed
    # into a population, never treated as insufficient data)
    store = AnalyticsStore(repo["tmp_path"] / "data" / "analytics" /
                           "analytics.sqlite3")
    lineage = {"youtube_video_id": "vidEffA00001", "run_id": repo["run_a"],
               "package_id": "pkg-x", "destination": "youtube"}
    store.insert_observation(
        observation_id="obs-effa000000000009", source="ANALYTICS_API_V2",
        youtube_video_id="vidEffA00001", scope="video", observed_at=NOW,
        metrics_requested=["views"], source_request={}, raw_response="{}",
        lineage=lineage, collected_at=NOW,
        window_start="2026-09-01", window_end="2026-09-07",
        window_timezone=None)
    store.insert_measurements(
        observation_id="obs-effa000000000009", source="ANALYTICS_API_V2",
        youtube_video_id="vidEffA00001", scope="video", observed_at=NOW,
        lineage=lineage, measurements=[stat("views", 99)],
        normalized_at=NOW, window_start="2026-09-01",
        window_end="2026-09-07")
    store.close()
    report = candidates_for_policy(
        repo["tmp_path"] / "data" / "runs", repo["policy_1"]["policy_id"],
        **eff(repo["tmp_path"], repo["store_path"]))
    broken = [c for c in report["candidates"]
              if c["metric"]["window_start"] == "2026-09-01"]
    assert len(broken) == 1
    assert broken[0]["status"] == "ineligible"


def test_corrupt_consumption_artifact_fails_closed(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    # mutate run G's hash-pinned consumption artifact (corrupt evidence)
    artifact = runs_root / repo["run_g"] / "policy_consumption.json"
    event = json.loads(artifact.read_text(encoding="utf-8"))
    event["policy_version"] = 99
    artifact.write_text(json.dumps(event), encoding="utf-8")
    verification = verify_intake(runs_root, **paths)
    assert verification["runs_corrupt"] >= 1
    assert any(r["run_id"] == repo["run_g"]
               for r in verification["corrupt_runs"])
    # the corrupt run's evidence is EXCLUDED — the lifetime-views candidate
    # loses G and drops below the comparability floor (fail closed; the
    # remaining real evidence is reported truthfully as insufficient)
    report = candidates_for_policy(runs_root, repo["policy_1"]["policy_id"],
                                   **paths)
    lifetime = by_key(report["candidates"])[(1, "views", "DATA_API_V3", None)]
    assert lifetime["status"] == "insufficient_evidence"
    assert repo["run_g"] not in lifetime["run_ids"]


def test_analytics_lineage_mismatch_is_ineligible_not_guessed(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    # an observation whose lineage claims a DIFFERENT run: the Stage 12
    # cross-check invalidates the record; intake refuses the evidence
    add_observation(
        repo["tmp_path"], "vidEffA00001", repo["run_a"],
        "obs-effa000000000008", [stat("views", 12345)],
        lineage={"youtube_video_id": "vidEffA00001", "run_id": repo["run_b"],
                 "package_id": "pkg-x", "destination": "youtube"})
    report = candidates_for_policy(runs_root, repo["policy_1"]["policy_id"],
                                   **paths)
    mismatched = [r for r in report["ineligible_records"]
                  if r["run_id"] == repo["run_a"]]
    assert mismatched and any(
        any("analytics_run_id_mismatch" in reason for reason in r["reasons"])
        for r in mismatched)
    lifetime = by_key(report["candidates"])[(1, "views", "DATA_API_V3", None)]
    assert repo["run_a"] not in lifetime["run_ids"]


def test_unknown_policy_and_invalid_inputs_are_structured(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    empty = candidates_for_policy(runs_root, "pol-20260922nopo00000001",
                                  **paths)
    assert empty["ok"] is True and empty["candidate_count"] == 0
    with pytest.raises(PolicyError) as excinfo:
        candidates_for_policy(runs_root, "not-a-policy-id", **paths)
    assert excinfo.value.code == "policy_intake_invalid"
    with pytest.raises(PolicyError):
        candidates_for_policy(runs_root, repo["policy_1"]["policy_id"],
                              policy_version=0, **paths)
    with pytest.raises(PolicyError):
        candidates_for_metric(runs_root, "   ", **paths)
    with pytest.raises(PolicyError):
        show_candidate(runs_root, "cand-deadbeefdeadbeef", **paths)


# ---- §12: the explicit approval boundary ----------------------------------------


def test_approval_materializes_existing_stage7_candidate_only(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    report = candidates_for_policy(runs_root, repo["policy_1"]["policy_id"],
                                   **paths)
    eligible = next(c for c in report["candidates"]
                    if c["status"] == "eligible")
    learning_path = repo["tmp_path"] / "data" / "learning" / "learning.sqlite3"
    before = digests(store_sources(repo) + run_sources(repo))

    result = approve_candidate(runs_root, eligible["candidate_id"],
                               LearningStore(learning_path),
                               approved_by="operator-a", now=NOW, **paths)

    assert result["ok"] is True and result["status"] == "approved"
    assert result["stage7_candidate_id"].startswith("cnd-")
    assert "no experiment was created" in result["boundary"]
    # the ONLY mutation is the existing Stage 7 learning store (its owner)
    assert digests(store_sources(repo) + run_sources(repo)) == before
    store = LearningStore(learning_path)
    stored = store.get_candidate(result["stage7_candidate_id"])
    assert stored["status"] == "candidate"  # Stage 7 state, untouched flow
    assert stored["lineage"]["stage"] == 13
    assert stored["lineage"]["intake_candidate_id"] == \
        eligible["candidate_id"]
    assert stored["lineage"]["policy_id"] == eligible["policy_id"]
    assert stored["lineage"]["approved_by"] == "operator-a"
    # evidence entries REFERENCE the Stage 12 measurements (no duplication)
    assert all(e["kind"] == "policy_effectiveness_measurement"
               for e in stored["evidence"])
    # NO experiment was created, approved or started
    assert store.list_experiments() == []
    store.close()
    # idempotent: approving the SAME candidate again is a no-op duplicate
    again = approve_candidate(runs_root, eligible["candidate_id"],
                              LearningStore(learning_path),
                              approved_by="operator-a", now=NOW, **paths)
    assert again["stage7_candidate_id"] == result["stage7_candidate_id"]
    assert again["duplicate"] is True


def test_approval_refuses_non_eligible_and_unknown(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    learning_path = repo["tmp_path"] / "data" / "learning2" / "l.sqlite3"
    report = candidates_for_policy(runs_root, repo["policy_1"]["policy_id"],
                                   **paths)
    insufficient = next(c for c in report["candidates"]
                        if c["status"] == "insufficient_evidence")
    with pytest.raises(PolicyError) as excinfo:
        approve_candidate(runs_root, insufficient["candidate_id"],
                          LearningStore(learning_path),
                          approved_by="operator-a", now=NOW, **paths)
    assert excinfo.value.code == "policy_intake_invalid"
    assert "insufficient_evidence" in excinfo.value.message
    with pytest.raises(PolicyError):
        approve_candidate(runs_root, "cand-0000000000000000",
                          LearningStore(learning_path),
                          approved_by="operator-a", now=NOW, **paths)
    with pytest.raises(PolicyError):
        approve_candidate(runs_root, insufficient["candidate_id"],
                          LearningStore(learning_path),
                          approved_by="   ", now=NOW, **paths)
    # the refused approvals wrote NOTHING (the empty store file is the
    # existing Stage 7 owner's open behavior — zero candidate/experiment
    # rows exist and no intake data was materialized)
    store = LearningStore(learning_path)
    assert store.list_candidates() == []
    assert store.list_experiments() == []
    store.close()


# ---- §18: byte-level read-only safety of the read directions ---------------------


def test_read_directions_never_mutate_any_canonical_store(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    watched = store_sources(repo) + run_sources(repo)
    before = digests(watched)
    candidates_for_policy(runs_root, repo["policy_1"]["policy_id"], **paths)
    candidates_for_metric(runs_root, "views", **paths)
    report = candidates_for_policy(runs_root, repo["policy_1"]["policy_id"],
                                   **paths)
    show_candidate(runs_root, report["candidates"][0]["candidate_id"],
                   **paths)
    verify_intake(runs_root, **paths)
    assert digests(watched) == before  # not one byte changed


def test_metric_direction_is_descriptive_and_never_merges(stage12_repo):
    repo = stage12_repo
    report = candidates_for_metric(
        repo["tmp_path"] / "data" / "runs", "views",
        **eff(repo["tmp_path"], repo["store_path"]))
    assert report["ok"] is True
    identities = [(c["policy_id"], c["metric"]["source"],
                   c["metric"]["window_start"])
                  for c in report["candidates"]]
    assert len(identities) == len(set(identities))  # no merged groups
    assert report["aggregation"]["note"] == INTAKE_NOTE


def test_min_sample_size_reuses_stage7_contract():
    # the floor is the Stage 7 >= 2-unit comparability contract, not an
    # invented statistical threshold
    from ayce.learning.experiments import _validate_definition  # noqa: F401
    assert MIN_SAMPLE_SIZE == 2


# ---- §13: CLI (policy | metric | show | verify | approve) ------------------------


def test_cli_intake_read_directions_and_approval(stage12_repo, monkeypatch,
                                                 capsys):
    from ayce import cli
    repo = stage12_repo
    monkeypatch.setenv("AYCE_DATA_DIR", str(repo["tmp_path"] / "data"))
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])

    # policy direction (read-only projection; --json)
    assert cli.main(["policy", "experiment-candidate", "policy", "--json",
                     "--policy-id", repo["policy_1"]["policy_id"]]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["candidate_count"] >= 1
    eligible = next(c for c in report["candidates"]
                    if c["status"] == "eligible")

    # metric direction (--json)
    assert cli.main(["policy", "experiment-candidate", "metric", "--json",
                     "--metric", "views"]) == 0
    metric_report = json.loads(capsys.readouterr().out)
    assert metric_report["ok"] is True
    assert any(c["candidate_id"] == eligible["candidate_id"]
               for c in metric_report["candidates"])

    # show (--json)
    assert cli.main(["policy", "experiment-candidate", "show", "--json",
                     "--candidate-id", eligible["candidate_id"]]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["candidate"]["candidate_id"] == eligible["candidate_id"]

    # verify (--json)
    assert cli.main(["policy", "experiment-candidate", "verify",
                     "--json"]) == 0
    verification = json.loads(capsys.readouterr().out)
    assert verification["ok"] is True
    assert verification["deterministic"] is True

    # text output works (no --json)
    assert cli.main(["policy", "experiment-candidate", "policy",
                     "--policy-id", repo["policy_1"]["policy_id"]]) == 0
    text = capsys.readouterr().out
    assert eligible["candidate_id"] in text
    assert "no causal claim" in text

    # approve — the ONLY mutating operation (isolated to the Stage 7
    # learning store); watch every other canonical store byte-for-byte
    watched = store_sources(repo) + run_sources(repo)
    before = digests(watched)
    assert cli.main(["policy", "experiment-candidate", "approve", "--json",
                     "--candidate-id", eligible["candidate_id"],
                     "--approved-by", "operator-cli"]) == 0
    approval = json.loads(capsys.readouterr().out)
    assert approval["ok"] is True and approval["status"] == "approved"
    assert approval["stage7_candidate_id"].startswith("cnd-")
    assert "no experiment was created" in approval["boundary"]
    assert digests(watched) == before  # not one byte changed elsewhere

    # the Stage 7 learning store now holds the materialized candidate —
    # and still NO experiment (approval never starts anything)
    store = LearningStore(repo["tmp_path"] / "data" / "learning" /
                          "learning.sqlite3")
    assert store.get_candidate(approval["stage7_candidate_id"]) is not None
    assert store.list_experiments() == []
    store.close()

    # unknown candidate → structured CLI error (rc 1)
    assert cli.main(["policy", "experiment-candidate", "show", "--json",
                     "--candidate-id", "cand-0000000000000000"]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error["error"]["code"] == "policy_intake_invalid"
    # missing subcommand → structured usage error (the repo's convention:
    # policy_*_report errors are rc 1 with a code, printed to stderr)
    assert cli.main(["policy", "experiment-candidate"]) == 1
    assert capsys.readouterr().err.strip().startswith(
        "ayce policy experiment-candidate: policy_intake_invalid:")



