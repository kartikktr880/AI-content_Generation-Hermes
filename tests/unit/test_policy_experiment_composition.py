"""Stage 14 — Controlled experiment composition boundary tests.

Extends the Stage 12/13 fixture graph (REAL Stage 5 ledger, REAL Stage 6
analytics, REAL Stage 8/9/10 policy+consumption) and verifies:

- the §16 matrix: approved eligible candidates → complete proposals;
  insufficient/ineligible candidates → NO proposal; no-policy/unpublished/
  analytics-unavailable evidence → nothing; historical retired v1 →
  proposals remain interpretable;
- deterministic proposal identity (same candidate + same definition →
  same expdef id; changed evidence/metric/parameter → NEW id);
- NO invented parameters (seed never proposed; unresolved fields stay
  ``needs_operator_configuration`` and approval fails closed);
- the §9/§10/§17 approval boundary: approval creates the EXISTING
  Stage 7 DRAFT experiment through its owner, idempotently, NEVER
  started, ZERO assignments; a failed approval never partially creates;
- byte-level read-only safety of every read direction (§19).
"""

import hashlib
import json
from pathlib import Path

import pytest

from ayce.learning import LearningStore
from ayce.learning.errors import LearningError
from ayce.policy import PolicyError
from ayce.policy.experiment_definition import (
    COMPOSITION_NOTE,
    MIN_POPULATION_UNITS,
    PROPOSAL_ID_RE,
    approve_proposal,
    propose_for_candidate,
    proposals_for_policy,
    show_proposal,
    verify_composition,
)

from test_policy import NOW
from test_policy_effectiveness import eff, repo as stage12_repo

FORBIDDEN_FIELDS = {"experiment_id", "started", "p_value", "effect_estimate",
                    "winner", "rank", "score", "recommendation",
                    "caused_by", "promotion"}


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


def approve_intake_candidate(repo, candidate, learning_store) -> dict:
    """The EXPLICIT Stage 13 candidate approval (separate boundary)."""
    from ayce.policy.experiment_intake import approve_candidate
    return approve_candidate(
        repo["tmp_path"] / "data" / "runs", candidate["candidate_id"],
        learning_store, approved_by="operator-a", now=NOW,
        **eff(repo["tmp_path"], repo["store_path"]))


def eligible_lifetime_views_candidate(repo):
    from ayce.policy.experiment_intake import candidates_for_policy
    report = candidates_for_policy(
        repo["tmp_path"] / "data" / "runs", repo["policy_1"]["policy_id"],
        **eff(repo["tmp_path"], repo["store_path"]))
    return next(c for c in report["candidates"]
                if c["metric"]["name"] == "views"
                and c["metric"]["source"] == "DATA_API_V3"
                and c["status"] == "eligible")


# ---- §16/§5: proposal composition and parameter resolution -----------------------


def test_proposal_composes_complete_stage7_definition(stage12_repo):
    repo = stage12_repo
    candidate = eligible_lifetime_views_candidate(repo)
    report = propose_for_candidate(
        repo["tmp_path"] / "data" / "runs", candidate["candidate_id"],
        **eff(repo["tmp_path"], repo["store_path"]))
    assert report["ok"] is True
    proposal = report["proposal"]
    assert PROPOSAL_ID_RE.fullmatch(proposal["proposal_id"])
    assert proposal["status"] == "ready"
    assert proposal["candidate_id"] == candidate["candidate_id"]
    assert proposal["policy_id"] == repo["policy_1"]["policy_id"]
    assert proposal["policy_version"] == 1
    assert proposal["policy_state"] == "retired"  # historical, §16-G
    assert proposal["scope"] == "format:short_explainer"
    # comparison resolved from the CONSUMED POLICY's own rule — never
    # invented (§6)
    comparison = proposal["comparison"]
    assert comparison["source"] == "policy_rule"
    assert comparison["control_variant"] == "control"
    assert comparison["treatment_variant"] == "treatment"
    assert comparison["direction"] == "treatment_greater_than_control"
    # population = the OBSERVED published videos of the evidence (A + G)
    assert proposal["population"]["units"] == ["vidEffA00001",
                                               "vidEffG00001"]
    assert proposal["population"]["unit_count"] == 2
    assert proposal["population"]["resolved"] is True
    assert proposal["population"]["min_units"] == MIN_POPULATION_UNITS
    # the seed is NEVER invented — it is an approval-time operator arg
    assert proposal["assignment"]["seed"] is None
    assert proposal["assignment"]["resolved"] is False
    assert "--seed" in proposal["assignment"]["note"]
    # success criterion reuses the Stage 7/13 >= 2 contract; min_effect
    # is NOT silently defaulted (Stage 7's own contract applies)
    criterion = proposal["success_criterion"]
    assert criterion["min_control_n"] == 2
    assert criterion["min_treatment_n"] == 2
    assert "min_effect" not in criterion
    assert proposal["metric"]["name"] == "views"
    assert proposal["metric_declared_in_stage7"] is True
    assert proposal["unresolved_fields"] == []
    # the preview is exactly what the Stage 7 owner will receive
    preview = proposal["stage7_definition_preview"]
    assert preview["metric"] == "views"
    assert preview["assignment"]["seed"] is None
    assert preview["population"]["units"] == proposal["population"]["units"]
    # evidence lineage: references to Stage 12 pef- ids, never copies
    assert proposal["observed_evidence"]["sample_size"] == 4
    assert all(eid.startswith("pef-") for eid in
               proposal["observed_evidence"]["source_effectiveness_evidence"])
    # a proposal is NOT an experiment — no Stage 7 fields exist at all
    assert not (FORBIDDEN_FIELDS & set(proposal))
    assert proposal["note"] == COMPOSITION_NOTE


def test_proposal_identity_is_deterministic_and_parameter_bound(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    candidate = eligible_lifetime_views_candidate(repo)
    first = propose_for_candidate(runs_root, candidate["candidate_id"],
                                  **paths)["proposal"]
    second = propose_for_candidate(runs_root, candidate["candidate_id"],
                                   **paths)["proposal"]
    assert first["proposal_id"] == second["proposal_id"]
    # a meaningfully different EXPERIMENT PARAMETER (operator-declared
    # min_effect) yields a DIFFERENT proposal id
    with_min_effect = propose_for_candidate(
        runs_root, candidate["candidate_id"], min_effect=1.5,
        **paths)["proposal"]
    assert with_min_effect["proposal_id"] != first["proposal_id"]
    assert with_min_effect["success_criterion"]["min_effect"] == 1.5
    # an explicit operator comparison yields a DIFFERENT proposal id and
    # is marked as operator-sourced (explicit choice, not invented)
    with_variants = propose_for_candidate(
        runs_root, candidate["candidate_id"],
        control_variant="baseline", treatment_variant="bold",
        **paths)["proposal"]
    assert with_variants["proposal_id"] != first["proposal_id"]
    assert with_variants["comparison"]["source"] == "operator"
    assert with_variants["comparison"]["control_variant"] == "baseline"
    # a DIFFERENT candidate (windowed Analytics-API views, A+B) yields a
    # different proposal id — identities never merge across windows
    from ayce.policy.experiment_intake import candidates_for_policy
    all_candidates = candidates_for_policy(
        runs_root, repo["policy_1"]["policy_id"], **paths)["candidates"]
    windowed = next(c for c in all_candidates
                    if c["metric"]["source"] == "ANALYTICS_API_V2")
    other = propose_for_candidate(runs_root, windowed["candidate_id"],
                                  **paths)["proposal"]
    assert other["proposal_id"] != first["proposal_id"]
    assert other["metric"]["window_semantics"] == "windowed"
    # show by id returns the identical derived proposal
    shown = show_proposal(runs_root, first["proposal_id"],
                          **paths)["proposal"]
    assert shown == first


# ---- §12: candidate validation fails closed --------------------------------------


def test_invalid_candidates_never_yield_proposals(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    from ayce.policy.experiment_intake import candidates_for_policy
    report = candidates_for_policy(
        runs_root, repo["policy_1"]["policy_id"], **paths)
    insufficient = next(c for c in report["candidates"]
                        if c["status"] == "insufficient_evidence")
    # insufficient evidence → NO proposal (fail closed; never negative
    # evidence, never a fabricated experiment design)
    with pytest.raises(PolicyError) as excinfo:
        propose_for_candidate(runs_root, insufficient["candidate_id"],
                              **paths)
    assert excinfo.value.code == "policy_composition_invalid"
    assert "insufficient_evidence" in excinfo.value.message
    # unknown candidate → structured refusal
    with pytest.raises(PolicyError):
        propose_for_candidate(runs_root, "cand-0000000000000000", **paths)
    # invalid id grammar → structured refusal
    with pytest.raises(PolicyError):
        propose_for_candidate(runs_root, "not-a-candidate", **paths)
    # no-policy evidence (run F) never yields a proposal at all
    everything = proposals_for_policy(runs_root, None, **paths)
    for proposal in everything["proposals"]:
        assert proposal["policy_id"] in (repo["policy_1"]["policy_id"],
                                         repo["policy_2"]["policy_id"])
        assert repo["run_f"] not in proposal["observed_evidence"]["run_ids"]


def test_unresolved_configuration_fails_closed(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    # corrupt run A's hash-pinned consumption artifact → the lifetime
    # views candidate loses A and derives from G ONLY: still eligible
    # evidence (n=3) but ONE observed unit — BELOW the Stage 7 >= 2-unit
    # contract → the proposal is truthfully needs_operator_configuration
    artifact = runs_root / repo["run_a"] / "policy_consumption.json"
    event = json.loads(artifact.read_text(encoding="utf-8"))
    event["policy_version"] = 99
    artifact.write_text(json.dumps(event), encoding="utf-8")
    from ayce.policy.experiment_intake import candidates_for_policy
    report = candidates_for_policy(
        runs_root, repo["policy_1"]["policy_id"], **paths)
    degraded = next(c for c in report["candidates"]
                    if c["metric"]["name"] == "views"
                    and c["metric"]["source"] == "DATA_API_V3")
    assert degraded["status"] == "eligible"  # real evidence remains
    proposal = propose_for_candidate(runs_root, degraded["candidate_id"],
                                     **paths)["proposal"]
    assert proposal["status"] == "needs_operator_configuration"
    assert proposal["unresolved_fields"] == ["population_units"]
    assert proposal["population"]["units"] == ["vidEffG00001"]
    # approval FAILS CLOSED — a structural gap cannot be fixed by
    # operator flags, and no experiment is created
    learning_path = repo["tmp_path"] / "data" / "learning" / "l.sqlite3"
    store = LearningStore(learning_path)
    approve_intake_candidate(repo, degraded, store)
    with pytest.raises(PolicyError) as excinfo:
        approve_proposal(runs_root, proposal["proposal_id"], store,
                         approved_by="operator-a", seed="seed-x", now=NOW,
                         **paths)
    assert excinfo.value.code == "policy_composition_invalid"
    assert "population_units" in str(excinfo.value.details)
    assert store.list_experiments() == []  # nothing was created
    store.close()


# ---- §9/§10/§17: the explicit approval boundary -----------------------------------


def test_approval_creates_draft_experiment_idempotently(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    candidate = eligible_lifetime_views_candidate(repo)
    proposal = propose_for_candidate(runs_root, candidate["candidate_id"],
                                     **paths)["proposal"]
    learning_path = (repo["tmp_path"] / "data" / "learning" /
                     "learning.sqlite3")
    # the candidate approval is a SEPARATE explicit boundary — without
    # it, Stage 14 approval fails closed
    fresh = LearningStore(repo["tmp_path"] / "data" / "learning2" /
                          "l.sqlite3")
    with pytest.raises(PolicyError) as excinfo:
        approve_proposal(runs_root, proposal["proposal_id"], fresh,
                         approved_by="operator-a", seed="seed-1", now=NOW,
                         **paths)
    assert "has not been approved" in excinfo.value.message
    assert fresh.list_experiments() == []
    fresh.close()

    learning_store = LearningStore(learning_path)
    approve_intake_candidate(repo, candidate, learning_store)

    # explicit proposal approval → the EXISTING Stage 7 DRAFT experiment
    watched = store_sources(repo) + run_sources(repo)
    before = digests(watched)
    result = approve_proposal(runs_root, proposal["proposal_id"],
                              learning_store, approved_by="operator-a",
                              seed="seed-det-001", now=NOW, **paths)
    assert result["ok"] is True
    assert result["experiment_id"].startswith("exp-")
    assert result["experiment_status"] == "draft"   # NEVER started
    assert result["started"] is False
    assert result["assignments"] == 0               # ZERO assignments
    assert "the experiment is a DRAFT" in result["boundary"]
    # the ONLY mutation is the Stage 7 learning store via its owner
    assert digests(watched) == before

    # the experiment exists EXACTLY once, unstarted, unassigned
    experiments = learning_store.list_experiments()
    assert len(experiments) == 1
    experiment = learning_store.get_experiment(result["experiment_id"])
    assert experiment["status"] == "draft"
    assert experiment["definition"]["candidate_id"] == \
        result["stage7_candidate_id"]
    assert experiment["definition"]["metric"] == "views"
    assert experiment["definition"]["control_variant"] == "control"
    assert experiment["definition"]["treatment_variant"] == "treatment"
    assert experiment["definition"]["population"]["units"] == \
        ["vidEffA00001", "vidEffG00001"]
    assert experiment["definition"]["assignment"]["seed"] == "seed-det-001"
    # the proposal id is embedded in the definition hypothesis (audit)
    assert proposal["proposal_id"] in experiment["definition"]["hypothesis"]
    assert learning_store.assignments_for_experiment(
        result["experiment_id"]) == []

    # idempotency: approving the SAME proposal again → the SAME existing
    # experiment, no duplicate
    again = approve_proposal(runs_root, proposal["proposal_id"],
                             learning_store, approved_by="operator-a",
                             seed="seed-det-001", now=NOW, **paths)
    assert again["experiment_id"] == result["experiment_id"]
    assert again["duplicate"] is True
    assert len(learning_store.list_experiments()) == 1

    # a DIFFERENT seed is a meaningfully different definition → a NEW
    # experiment (Stage 7 mutation-immutability contract, unchanged)
    different = approve_proposal(runs_root, proposal["proposal_id"],
                                 learning_store, approved_by="operator-a",
                                 seed="seed-other", now=NOW, **paths)
    assert different["experiment_id"] != result["experiment_id"]
    assert len(learning_store.list_experiments()) == 2
    # STILL zero assignments anywhere — approval never assigns
    for stored in learning_store.list_experiments():
        assert learning_store.assignments_for_experiment(
            stored["experiment_id"]) == []
    learning_store.close()


def test_approval_requires_seed_and_validates_input(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    candidate = eligible_lifetime_views_candidate(repo)
    proposal = propose_for_candidate(runs_root, candidate["candidate_id"],
                                     **paths)["proposal"]
    learning_store = LearningStore(repo["tmp_path"] / "data" / "learning3"
                                   / "l.sqlite3")
    approve_intake_candidate(repo, candidate, learning_store)
    # the seed is NEVER invented — missing seed fails closed
    with pytest.raises(PolicyError) as excinfo:
        approve_proposal(runs_root, proposal["proposal_id"],
                         learning_store, approved_by="operator-a",
                         seed="   ", now=NOW, **paths)
    assert "--seed" in excinfo.value.message
    # missing operator identity fails closed
    with pytest.raises(PolicyError):
        approve_proposal(runs_root, proposal["proposal_id"],
                         learning_store, approved_by="  ", seed="s",
                         now=NOW, **paths)
    # unknown proposal id fails closed
    with pytest.raises(PolicyError):
        approve_proposal(runs_root, "expdef-0000000000000000",
                         learning_store, approved_by="operator-a",
                         seed="s", now=NOW, **paths)
    # one-sided variant override fails closed
    with pytest.raises(PolicyError):
        approve_proposal(runs_root, proposal["proposal_id"],
                         learning_store, approved_by="operator-a",
                         seed="s", now=NOW, control_variant="solo",
                         **paths)
    # nothing was created by ANY refused approval
    assert learning_store.list_experiments() == []
    learning_store.close()


def test_stage7_refusal_wraps_structured_and_creates_nothing(
        stage12_repo, monkeypatch):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    candidate = eligible_lifetime_views_candidate(repo)
    proposal = propose_for_candidate(runs_root, candidate["candidate_id"],
                                     **paths)["proposal"]
    learning_store = LearningStore(repo["tmp_path"] / "data" / "learning4"
                                   / "l.sqlite3")
    approve_intake_candidate(repo, candidate, learning_store)
    # the Stage 7 owner refuses → Stage 14 wraps it structurally and
    # NOTHING is created (no partial experiment)
    def refusing_owner(*args, **kwargs):
        raise LearningError("learning_experiment_invalid",
                            "definition refused by the Stage 7 owner")
    import ayce.learning
    monkeypatch.setattr(ayce.learning, "create_experiment",
                        refusing_owner)
    with pytest.raises(PolicyError) as excinfo:
        approve_proposal(runs_root, proposal["proposal_id"],
                         learning_store, approved_by="operator-a",
                         seed="seed-x", now=NOW, **paths)
    assert excinfo.value.code == "policy_composition_invalid"
    assert "nothing was created" in excinfo.value.message
    assert excinfo.value.details["stage7_code"] == \
        "learning_experiment_invalid"
    assert learning_store.list_experiments() == []
    learning_store.close()


def test_dry_run_approval_creates_nothing(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    candidate = eligible_lifetime_views_candidate(repo)
    proposal = propose_for_candidate(runs_root, candidate["candidate_id"],
                                     **paths)["proposal"]
    learning_store = LearningStore(repo["tmp_path"] / "data" / "learning5"
                                   / "l.sqlite3")
    approve_intake_candidate(repo, candidate, learning_store)
    watched = store_sources(repo) + run_sources(repo)
    before = digests(watched)
    result = approve_proposal(runs_root, proposal["proposal_id"],
                              learning_store, approved_by="operator-a",
                              seed="seed-dry", now=NOW, dry_run=True,
                              **paths)
    assert result["ok"] is True and result["dry_run"] is True
    assert result["experiment_id"].startswith("exp-")
    assert result["experiment_status"] == "draft"
    # NOTHING was persisted anywhere — not even the learning store
    assert digests(watched) == before
    assert learning_store.list_experiments() == []
    learning_store.close()


# ---- §19: byte-level read-only safety + integrity ---------------------------------


def test_read_directions_never_mutate_any_canonical_store(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    watched = store_sources(repo) + run_sources(repo)
    before = digests(watched)
    candidate = eligible_lifetime_views_candidate(repo)
    propose_for_candidate(runs_root, candidate["candidate_id"], **paths)
    proposals_for_policy(runs_root, None, **paths)
    proposal = propose_for_candidate(runs_root, candidate["candidate_id"],
                                     **paths)["proposal"]
    show_proposal(runs_root, proposal["proposal_id"], **paths)
    verify_composition(runs_root, **paths)
    assert digests(watched) == before  # not one byte changed


def test_verify_composition_reports_integrity(stage12_repo):
    repo = stage12_repo
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    verification = verify_composition(runs_root, **paths)
    assert verification["ok"] is True
    assert verification["deterministic"] is True
    assert verification["candidates_consistent"] is True
    assert verification["evidence_consistent"] is True
    assert verification["consistent"] is True
    assert verification["proposals_total"] >= 1
    assert verification["note"] == COMPOSITION_NOTE


# ---- §13: CLI (run | show | verify | approve) --------------------------------------


def test_cli_definition_read_directions_and_approval(stage12_repo,
                                                     monkeypatch, capsys,
                                                     tmp_path):
    import shutil
    from ayce import cli
    repo = stage12_repo
    monkeypatch.setenv("AYCE_DATA_DIR", str(repo["tmp_path"] / "data"))
    # the fixture's policy store lives outside the CLI's AYCE_DATA_DIR
    # layout; mirror it into the CLI's read-only policy location (the
    # CLI never writes it — verified by the byte checks below)
    policy_dir = repo["tmp_path"] / "data" / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repo["store_path"], policy_dir / "policy.sqlite3")
    runs_root = repo["tmp_path"] / "data" / "runs"
    paths = eff(repo["tmp_path"], repo["store_path"])
    candidate = eligible_lifetime_views_candidate(repo)

    # the Stage 13 candidate approval happens FIRST (separate boundary)
    learning_path = repo["tmp_path"] / "data" / "learning" / "learning.sqlite3"
    store = LearningStore(learning_path)
    approve_intake_candidate(repo, candidate, store)
    store.close()

    # run — the derived proposal (read-only; --json)
    assert cli.main(["policy", "experiment-definition", "run", "--json",
                     "--candidate-id", candidate["candidate_id"]]) == 0
    report = json.loads(capsys.readouterr().out)
    proposal = report["proposal"]
    assert proposal["status"] == "ready"
    assert proposal["candidate_id"] == candidate["candidate_id"]
    assert proposal["assignment"]["seed"] is None  # never invented

    # show — by content-addressed id (--json)
    assert cli.main(["policy", "experiment-definition", "show", "--json",
                     "--proposal-id", proposal["proposal_id"]]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["proposal"]["proposal_id"] == proposal["proposal_id"]

    # verify — deterministic re-derivation (--json)
    assert cli.main(["policy", "experiment-definition", "verify",
                     "--json"]) == 0
    verification = json.loads(capsys.readouterr().out)
    assert verification["deterministic"] is True
    assert verification["consistent"] is True

    # text output
    assert cli.main(["policy", "experiment-definition", "run",
                     "--candidate-id", candidate["candidate_id"]]) == 0
    text = capsys.readouterr().out
    assert proposal["proposal_id"] in text
    assert "approval is a separate explicit command" in text

    # DRY-RUN approval — validates and previews WITHOUT creating anything
    watched = store_sources(repo) + run_sources(repo)
    before = digests(watched)
    assert cli.main(["policy", "experiment-definition", "approve", "--json",
                     "--proposal-id", proposal["proposal_id"],
                     "--approved-by", "operator-cli",
                     "--seed", "seed-cli-001", "--dry-run"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert digests(watched) == before
    store = LearningStore(learning_path)
    assert store.list_experiments() == []
    store.close()

    # EXPLICIT approval — creates the Stage 7 DRAFT through its owner;
    # every OTHER canonical store stays byte-identical
    assert cli.main(["policy", "experiment-definition", "approve", "--json",
                     "--proposal-id", proposal["proposal_id"],
                     "--approved-by", "operator-cli",
                     "--seed", "seed-cli-001"]) == 0
    approval = json.loads(capsys.readouterr().out)
    assert approval["ok"] is True
    assert approval["experiment_id"].startswith("exp-")
    assert approval["experiment_status"] == "draft"
    assert approval["started"] is False and approval["assignments"] == 0
    assert digests(watched) == before  # not one byte changed elsewhere

    # the Stage 7 learning store now holds exactly ONE draft experiment
    store = LearningStore(learning_path)
    assert len(store.list_experiments()) == 1
    experiment = store.get_experiment(approval["experiment_id"])
    assert experiment["status"] == "draft"
    assert proposal["proposal_id"] in experiment["definition"]["hypothesis"]
    store.close()

    # unknown proposal → structured CLI error (rc 1)
    assert cli.main(["policy", "experiment-definition", "show", "--json",
                     "--proposal-id", "expdef-0000000000000000"]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error["error"]["code"] == "policy_composition_invalid"
    # missing subcommand → structured usage error (rc 1, stderr)
    assert cli.main(["policy", "experiment-definition"]) == 1
    assert capsys.readouterr().err.strip().startswith(
        "ayce policy experiment-definition: policy_composition_invalid:")