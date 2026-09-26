"""Stage 7 — Bounded experimentation + learning tests (deterministic fixtures).

Covers the §38 matrix using REAL Stage 6 stores (AnalyticsStore) seeded
with deterministic measurement fixtures — no network, no YouTube:
derived metrics (valid calculation / zero denominator / missing input /
provenance / deterministic identity), candidates (valid / duplicate /
invalid / lineage / counterexamples retained), experiments (immutable
content-addressed definitions, approval boundary, deterministic
idempotent assignment, dry-run), evaluation (sufficient/insufficient
samples, effect calculation, declared criterion, holdout
agreement/contradiction/insufficiency, rejection vs insufficient),
knowledge (versioning, immutable history, curation boundary, read-only
retrieval), safety (Stage 7 cannot modify production/publication/
analytics state), restart, and concurrency.
"""

import hashlib
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from ayce.analytics.store import AnalyticsStore
from ayce.config import Config
from ayce.learning import (
    LearningError,
    LearningStore,
    approve_experiment,
    assign_units,
    create_candidate,
    create_experiment,
    curate_candidate,
    derive_metrics,
    evaluate_experiment,
    read_validated_knowledge,
    start_experiment,
)

NOW = "2026-09-22T12:00:00.000Z"
WINDOW = ("2026-09-15", "2026-09-21")  # 7 days


def make_env(tmp_path: Path):
    """A REAL Stage 6 analytics store + REAL Stage 7 learning store in
    a temporary data root (deterministic fixtures, no network)."""
    config = replace(Config.from_env(env={}), data_dir=tmp_path / "data")
    analytics = AnalyticsStore(tmp_path / "data" / "analytics" /
                               "analytics.sqlite3")
    learning = LearningStore(tmp_path / "data" / "learning" /
                             "learning.sqlite3")
    return config, analytics, learning

def seed_video(analytics: AnalyticsStore, video_id: str, *,
               views: float, likes: float = 0.0, comments: float = 0.0,
               subscribers_gained: float | None = None,
               subscribers_lost: float | None = None,
               windowed_views: float | None = None,
               observed_at: str = NOW, window=WINDOW,
               run_id: str = "run-20260922T000000Z-abc123456789",
               omit: tuple = ()) -> None:
    """Seed one video's normalized measurements through the REAL Stage 6
    store API (deterministic fixture — no YouTube)."""
    lineage = {"package_id": "pkg-x", "package_seal": "seal-x",
               "run_id": run_id, "destination": "dest",
               "youtube_video_id": video_id, "script_id": "brief-x",
               "research_id": "res-x", "objective_id": "obj-x"}
    observation_id = "obs-" + hashlib.sha256(
        f"{video_id}|{observed_at}".encode()).hexdigest()[:24]
    stats = {"viewCount": str(views), "likeCount": str(likes),
             "commentCount": str(comments)}
    data_metrics = [{"metric_name": name, "raw_value": stats[col],
                     "value": float(stats[col]), "value_type": "integer",
                     "unit": "count",
                     "availability": "missing" if name in omit else
                     ("zero" if float(stats[col]) == 0 else "present")}
                    for name, col in (("views", "viewCount"),
                                      ("likes", "likeCount"),
                                      ("comments", "commentCount"))]
    analytics.insert_observation(
        observation_id=observation_id, source="DATA_API_V3",
        youtube_video_id=video_id, scope="video", observed_at=observed_at,
        metrics_requested=["views", "likes", "comments"],
        source_request={"api": "fixture"}, raw_response=json.dumps(
            {"items": [{"statistics": stats}]}),
        lineage=lineage, collected_at=observed_at)
    analytics.insert_measurements(
        observation_id=observation_id, source="DATA_API_V3",
        youtube_video_id=video_id, scope="video", observed_at=observed_at,
        lineage=lineage, measurements=data_metrics, normalized_at=observed_at)
    if windowed_views is not None:
        win_id = observation_id + "-a"
        analytics.insert_observation(
            observation_id=win_id, source="ANALYTICS_API_V2",
            youtube_video_id=video_id, scope="video",
            observed_at=observed_at, metrics_requested=["views"],
            source_request={"api": "fixture"},
            raw_response=json.dumps({"rows": [[windowed_views]]}),
            lineage=lineage, window_start=window[0], window_end=window[1],
            window_timezone="America/Los_Angeles", collected_at=observed_at)
        analytics.insert_measurements(
            observation_id=win_id, source="ANALYTICS_API_V2",
            youtube_video_id=video_id, scope="video",
            observed_at=observed_at, lineage=lineage,
            window_start=window[0], window_end=window[1],
            measurements=[{"metric_name": "views",
                           "raw_value": str(windowed_views),
                           "value": float(windowed_views),
                           "value_type": "integer", "unit": "count",
                           "availability": "present"}],
            normalized_at=observed_at)
    if subscribers_gained is not None or subscribers_lost is not None:
        sub_id = observation_id + "-s"
        analytics.insert_observation(
            observation_id=sub_id, source="ANALYTICS_API_V2",
            youtube_video_id=video_id, scope="video",
            observed_at=observed_at + "-s",
            metrics_requested=["subscribersGained", "subscribersLost"],
            source_request={"api": "fixture"}, raw_response=json.dumps(
                {"rows": [[subscribers_gained, subscribers_lost]]}),
            lineage=lineage, window_start=window[0], window_end=window[1],
            window_timezone="America/Los_Angeles",
            collected_at=observed_at)
        analytics.insert_measurements(
            observation_id=sub_id, source="ANALYTICS_API_V2",
            youtube_video_id=video_id, scope="video",
            observed_at=observed_at + "-s", lineage=lineage,
            window_start=window[0], window_end=window[1],
            measurements=[{"metric_name": "subscribersGained",
                           "raw_value": str(subscribers_gained),
                           "value": float(subscribers_gained),
                           "value_type": "integer", "unit": "count",
                           "availability": "present"},
                          {"metric_name": "subscribersLost",
                           "raw_value": str(subscribers_lost),
                           "value": float(subscribers_lost),
                           "value_type": "integer", "unit": "count",
                           "availability": "zero"}],
            normalized_at=observed_at + "-s")


def derive_all(analytics, learning, videos):
    """Derive + persist all formulas for the given videos (helper)."""
    rows = []
    for video in videos:
        rows.extend(derive_metrics(analytics, now=NOW,
                                   youtube_video_id=video))
    inserted = 0
    for row in rows:
        if learning.insert_derived(row=row):
            inserted += 1
    return rows, inserted


def pick_balanced_seed(learning, candidate, metric, units, min_n,
                       holdout=None, holdout_balance=None):
    """Deterministically choose an assignment seed: the sha256 split
    must give ≥ min_n units per variant, and — when a holdout is
    configured and ``holdout_balance`` is set — the deterministic
    holdout split must (True) or must not (False) reach the declared
    holdout minimum. Fixture construction ONLY: the search picks which
    units land in which variant; nothing about RESULTS is chosen."""
    from ayce.learning.evaluation import _holdout_split
    from ayce.learning.experiments import (
        _validate_definition, experiment_id_for, variant_for_unit)
    for i in range(500):
        seed = f"seed-{i}"
        definition = _validate_definition(
            candidate_id=candidate["candidate_id"],
            hypothesis=candidate["hypothesis"], metric=metric,
            control_variant="control", treatment_variant="treatment",
            population={"units": units}, evaluation_window=None,
            success_criterion={"min_control_n": min_n,
                               "min_treatment_n": min_n,
                               "min_effect": 0.0},
            assignment={"seed": seed}, holdout=holdout)
        exp_id = experiment_id_for(definition)
        variants = [variant_for_unit(exp_id, u, seed)
                    for u in sorted(units)]
        if variants.count("control") < min_n or \
                variants.count("treatment") < min_n:
            continue
        if holdout is not None and holdout_balance is not None:
            dev, hold = _holdout_split(exp_id, holdout, sorted(units))
            h_control = sum(1 for u in hold
                            if variant_for_unit(exp_id, u, seed) == "control")
            h_treatment = len(hold) - h_control
            sufficient = (h_control >= holdout["min_n"]
                          and h_treatment >= holdout["min_n"])
            if sufficient is not holdout_balance:
                continue
        return seed
    raise AssertionError("no deterministic seed found for the requested "
                         "assignment shape")


def full_pipeline(learning, analytics, *, metric="views", min_effect=1.0,
                  units: list | None = None, control_value=1000.0,
                  treatment_value=5000.0, seed=None, holdout=None,
                  min_n: int = 2, now=NOW, n_units=6,
                  holdout_balance=None, hypothesis=None,
                  scope="test-scope"):
    """Candidate → experiment → approve → assign → measure → evaluate.

    Outcomes are seeded AFTER assignment (treatment units get
    ``treatment_value``, control units ``control_value``, with a tiny
    deterministic jitter so per-variant variance is non-zero) —
    mirroring a real experiment where measured outcomes follow the
    assigned variant.
    """
    units = sorted(units) if units else \
        [f"vid{i:03d}" for i in range(n_units)]
    candidate = create_candidate(
        learning, hypothesis=hypothesis or f"units differ on {metric}",
        scope=scope,
        evidence=[{"kind": "measurement", "ref": "seeded",
                   "video_id": units[0]}],
        counterexamples=[], now=now)["candidate"]
    if seed is None:
        seed = pick_balanced_seed(learning, candidate, metric, units,
                                  min_n, holdout,
                                  holdout_balance=holdout_balance)
    experiment = create_experiment(
        learning, candidate_id=candidate["candidate_id"],
        hypothesis=candidate["hypothesis"], metric=metric,
        control_variant="control", treatment_variant="treatment",
        population={"units": units}, assignment={"seed": seed},
        success_criterion={"min_control_n": min_n,
                           "min_treatment_n": min_n,
                           "min_effect": min_effect},
        holdout=holdout, now=now)["experiment"]
    approve_experiment(learning, experiment["experiment_id"], now=now)
    assignments = assign_units(learning, experiment["experiment_id"],
                               units, now=now)["assignments"]
    for index, assignment in enumerate(
            sorted(assignments, key=lambda a: a["unit_id"])):
        base = treatment_value if assignment["variant"] == "treatment" \
            else control_value
        seed_video(analytics, assignment["unit_id"],
                   views=base + (index % 3) * 7.0)
    evaluation = evaluate_experiment(learning, analytics,
                                     experiment["experiment_id"],
                                     now=now)["evaluation"]
    return {"candidate": candidate, "experiment": experiment,
            "evaluation": evaluation, "units": units}


# ---- derived metrics (§7/§8) -------------------------------------------------------


def test_derived_metric_valid_calculation_with_provenance(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    seed_video(analytics, "vidA1", views=1000, likes=50, comments=10,
               windowed_views=700)
    rows, _ = derive_all(analytics, learning, ["vidA1"])

    def derived(formula):
        # the derivation is per observation context; select the row
        # that actually carries the formula's operands (present)
        return next(r for r in rows if r["formula"] == formula
                    and r["availability"] == "present")

    assert derived("likes_per_view")["value"] == pytest.approx(0.05)
    assert derived("comments_per_view")["value"] == pytest.approx(0.01)
    # views_per_day = 700 windowed views / 7-day window
    assert derived("views_per_day")["value"] == pytest.approx(100.0)
    lpv = learning.get_derived(
        derived("likes_per_view")["derived_metric_id"])
    assert lpv["formula"] == "likes_per_view"
    assert lpv["formula_version"] == "v1"
    assert lpv["source_observation_ids"]      # provenance: real obs ids
    assert lpv["lineage"]["run_id"]           # provenance: lineage refs
    assert lpv["availability"] == "present"


def test_derived_metric_zero_denominator_is_undefined_not_zero(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    seed_video(analytics, "vidZero", views=0, likes=0)   # views = 0
    rows, _ = derive_all(analytics, learning, ["vidZero"])
    lpv = next(r for r in rows if r["formula"] == "likes_per_view")
    # views = 0 must NOT produce likes_per_view = 0 (§7): undefined
    assert lpv["availability"] == "undefined"
    assert lpv["value"] is None
    assert "zero denominator" in lpv["note"]
    # zero NUMERATOR stays a real zero
    stored = learning.derived_for_video("vidZero",
                                        formula="likes_per_view")[0]
    assert stored["availability"] == "undefined"


def test_derived_metric_missing_input_is_missing(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    seed_video(analytics, "vidMiss", views=100, likes=0, omit=("likes",))
    rows, _ = derive_all(analytics, learning, ["vidMiss"])
    lpv = next(r for r in rows if r["formula"] == "likes_per_view")
    assert lpv["availability"] == "missing"   # ≠ zero, ≠ undefined
    assert lpv["value"] is None
    # views_per_day is undefined (lifetime Data-API snapshot has no window)
    vpd = next(r for r in rows if r["formula"] == "views_per_day")
    assert vpd["availability"] == "undefined"
    assert "no evaluation window" in vpd["note"]


def test_derived_metric_identity_is_deterministic_and_idempotent(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    seed_video(analytics, "vidIdem", views=100, likes=10, windowed_views=50)
    rows1, inserted1 = derive_all(analytics, learning, ["vidIdem"])
    rows2, inserted2 = derive_all(analytics, learning, ["vidIdem"])
    assert inserted1 == len(rows1) and inserted2 == 0   # no duplicates
    assert {r["derived_metric_id"] for r in rows1} == \
        {r["derived_metric_id"] for r in rows2}
    assert learning.list_all()["derived_metrics"] == len(rows1)


def test_derived_metric_unknown_formula_refused(tmp_path):
    _, analytics, _ = make_env(tmp_path)
    with pytest.raises(LearningError) as excinfo:
        derive_metrics(analytics, now=NOW, youtube_video_id="vidX1",
                       formulas=["bogus_formula"])
    assert excinfo.value.code == "learning_metric_unavailable"

# ---- candidates (§10) -----------------------------------------------------------------


def test_candidate_creation_is_explicit_and_idempotent(tmp_path):
    _, _, learning = make_env(tmp_path)
    evidence = [{"kind": "measurement", "ref": "obs-1",
                 "video_id": "vidA1"}]
    first = create_candidate(
        learning, hypothesis="Hooks like H retain more viewers",
        scope="hook-family", evidence=evidence, counterexamples=[],
        now=NOW)["candidate"]
    assert first["status"] == "candidate"
    second = create_candidate(
        learning, hypothesis="Hooks like H retain more viewers",
        scope="hook-family", evidence=evidence, counterexamples=[],
        now=NOW)["candidate"]
    assert second["candidate_id"] == first["candidate_id"]
    assert second["duplicate"] is True
    assert learning.list_all()["candidates"] == 1


def test_candidate_requires_hypothesis_scope_evidence(tmp_path):
    _, _, learning = make_env(tmp_path)
    for kwargs in (
        {"hypothesis": "", "scope": "s"},
        {"hypothesis": "h", "scope": ""},
    ):
        with pytest.raises(LearningError) as excinfo:
            create_candidate(
                learning, evidence=[{"kind": "measurement", "ref": "r"}],
                counterexamples=[], now=NOW, **kwargs)
        assert excinfo.value.code == "learning_invalid_input"
    # no evidence at all → also refused
    with pytest.raises(LearningError) as excinfo:
        create_candidate(learning, hypothesis="h", scope="s",
                         evidence=[], counterexamples=[], now=NOW)
    assert excinfo.value.code == "learning_invalid_input"


def test_candidate_counterexamples_are_retained_separately(tmp_path):
    _, _, learning = make_env(tmp_path)
    evidence = [{"kind": "measurement", "ref": "obs-1", "video_id": "v1"}]
    counter = [{"kind": "measurement", "ref": "obs-2", "video_id": "v2",
                "value": 0.5}]
    candidate = create_candidate(
        learning, hypothesis="h", scope="s", evidence=evidence,
        counterexamples=counter, now=NOW)["candidate"]
    stored = learning.get_candidate(candidate["candidate_id"])
    assert stored["evidence"] != stored["counterexamples"]
    assert stored["counterexamples"][0]["ref"] == "obs-2"
    assert stored["counterexamples"][0]["value"] == 0.5  # not discarded


def test_candidate_malformed_evidence_refused(tmp_path):
    _, _, learning = make_env(tmp_path)
    with pytest.raises(LearningError) as excinfo:
        create_candidate(learning, hypothesis="h", scope="s",
                         evidence=[{"ref": "no-kind"}],
                         counterexamples=[], now=NOW)
    assert excinfo.value.code == "learning_invalid_input"


# ---- experiments (§11–§17) --------------------------------------------------------------


def _candidate_for(learning, hypothesis="h", scope="s"):
    return create_candidate(
        learning, hypothesis=hypothesis, scope=scope,
        evidence=[{"kind": "measurement", "ref": "obs-1"}],
        counterexamples=[], now=NOW)["candidate"]


def test_experiment_definition_is_immutable_content_addressed(tmp_path):
    _, _, learning = make_env(tmp_path)
    candidate = _candidate_for(learning, "Same hypothesis", "scope-a")
    definition_kwargs = dict(
        candidate_id=candidate["candidate_id"],
        hypothesis=candidate["hypothesis"], metric="views",
        control_variant="control", treatment_variant="treatment",
        population={"units": ["vidA1", "vidA2", "vidA3", "vidA4"]},
        assignment={"seed": "seed-1"},
        success_criterion={"min_control_n": 1, "min_treatment_n": 1})
    first = create_experiment(learning, now=NOW, **definition_kwargs)
    assert first["experiment"]["status"] == "draft"
    second = create_experiment(learning, now=NOW, **definition_kwargs)
    assert second["experiment"]["experiment_id"] == \
        first["experiment"]["experiment_id"]   # identical → same id
    assert second["experiment"]["duplicate"] is True
    # ANY definition change (e.g. a different metric) → a NEW experiment
    changed = create_experiment(
        learning, now=NOW, **{**definition_kwargs, "metric": "likes"})
    assert changed["experiment"]["experiment_id"] != \
        first["experiment"]["experiment_id"]
    # the original experiment's stored definition is untouched
    stored = learning.get_experiment(first["experiment"]["experiment_id"])
    assert stored["definition"]["metric"] == "views"
    assert stored["status"] == "draft"


def test_experiment_requires_valid_metric_variants_and_population(tmp_path):
    _, _, learning = make_env(tmp_path)
    candidate = _candidate_for(learning)
    base = dict(
        candidate_id=candidate["candidate_id"],
        hypothesis=candidate["hypothesis"], metric="views",
        control_variant="control", treatment_variant="treatment",
        population={"units": ["vidA1", "vidA2"]},
        assignment={"seed": "s"},
        success_criterion={"min_control_n": 1, "min_treatment_n": 1})
    with pytest.raises(LearningError) as excinfo:
        create_experiment(learning, now=NOW,
                          **{**base, "metric": "not_a_metric"})
    assert excinfo.value.code == "learning_metric_unavailable"
    with pytest.raises(LearningError) as excinfo:
        create_experiment(learning, now=NOW,
                          **{**base, "treatment_variant": "control"})
    assert excinfo.value.code == "learning_experiment_invalid"
    with pytest.raises(LearningError) as excinfo:
        create_experiment(learning, now=NOW,
                          **{**base, "population": {"units": ["only-one"]}})
    assert excinfo.value.code == "learning_experiment_invalid"
    with pytest.raises(LearningError) as excinfo:
        create_experiment(learning, now=NOW,
                          **{**base, "candidate_id": "cnd-doesnotexist"})
    assert excinfo.value.code == "learning_invalid_input"


def test_experiment_requires_explicit_approval(tmp_path):
    _, _, learning = make_env(tmp_path)
    candidate = _candidate_for(learning)
    experiment = create_experiment(
        learning, candidate_id=candidate["candidate_id"],
        hypothesis=candidate["hypothesis"], metric="views",
        control_variant="control", treatment_variant="treatment",
        population={"units": ["vidA1", "vidA2"]},
        assignment={"seed": "s"},
        success_criterion={"min_control_n": 1, "min_treatment_n": 1},
        now=NOW)["experiment"]
    exp_id = experiment["experiment_id"]
    # nothing may happen while still a draft
    with pytest.raises(LearningError) as excinfo:
        assign_units(learning, exp_id, ["vidA1", "vidA2"], now=NOW)
    assert excinfo.value.code == "learning_experiment_invalid"
    with pytest.raises(LearningError):
        evaluate_experiment(learning, learning, exp_id, now=NOW)
    # explicit approval opens the boundary
    approved = approve_experiment(learning, exp_id, now=NOW)
    assert approved["status"] == "approved"
    # double approval is refused (one-time boundary)
    with pytest.raises(LearningError):
        approve_experiment(learning, exp_id, now=NOW)


def test_assignment_is_deterministic_and_idempotent(tmp_path):
    _, _, learning = make_env(tmp_path)
    candidate = _candidate_for(learning)
    experiment = create_experiment(
        learning, candidate_id=candidate["candidate_id"],
        hypothesis=candidate["hypothesis"], metric="views",
        control_variant="control", treatment_variant="treatment",
        population={"units": ["vidA1", "vidA2", "vidA3"]},
        assignment={"seed": "seed-7"},
        success_criterion={"min_control_n": 1, "min_treatment_n": 1},
        now=NOW)["experiment"]
    exp_id = experiment["experiment_id"]
    approve_experiment(learning, exp_id, now=NOW)
    first = assign_units(learning, exp_id, ["vidA1", "vidA2", "vidA3"],
                         now=NOW)
    second = assign_units(learning, exp_id, ["vidA1", "vidA2", "vidA3"],
                          now=NOW)
    variants_first = {a["unit_id"]: a["variant"]
                      for a in first["assignments"]}
    variants_second = {a["unit_id"]: a["variant"]
                       for a in second["assignments"]}
    assert variants_first == variants_second     # no silent reassignment
    assert all(a["previously_assigned"] for a in second["assignments"])
    from ayce.learning import variant_for_unit
    for unit, variant in variants_first.items():
        assert variant_for_unit(exp_id, unit, "seed-7") == variant


def test_assignment_dry_run_does_not_persist(tmp_path):
    _, _, learning = make_env(tmp_path)
    candidate = _candidate_for(learning)
    experiment = create_experiment(
        learning, candidate_id=candidate["candidate_id"],
        hypothesis=candidate["hypothesis"], metric="views",
        control_variant="control", treatment_variant="treatment",
        population={"units": ["vidA1", "vidA2"]},
        assignment={"seed": "s"},
        success_criterion={"min_control_n": 1, "min_treatment_n": 1},
        now=NOW)["experiment"]
    exp_id = experiment["experiment_id"]
    approve_experiment(learning, exp_id, now=NOW)
    planned = assign_units(learning, exp_id, ["vidA1", "vidA2"], now=NOW,
                           dry_run=True)
    assert planned["assignments"] and planned["dry_run"] is True
    assert learning.assignments_for_experiment(exp_id) == []
    # out-of-population units are refused (definitions are immutable)
    with pytest.raises(LearningError) as excinfo:
        assign_units(learning, exp_id, ["vidOther"], now=NOW)
    assert excinfo.value.code == "learning_experiment_invalid"


# ---- evaluation (§19–§24) ----------------------------------------------------------------


def test_evaluation_validates_with_sufficient_samples(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    # treatment units measure 5000, control units 1000 (outcomes follow
    # the assigned variant, as in a real experiment)
    result = full_pipeline(learning, analytics)
    evaluation = result["evaluation"]
    assert evaluation["status"] == "validated"
    assert evaluation["control_n"] >= 1 and evaluation["treatment_n"] >= 1
    assert evaluation["effect"] == (
        evaluation["treatment_mean"] - evaluation["control_mean"])
    assert evaluation["p_value"] is not None
    candidate = learning.get_candidate(result["candidate"]["candidate_id"])
    assert candidate["status"] == "validated"
    experiment = learning.get_experiment(
        result["experiment"]["experiment_id"])
    assert experiment["status"] == "evaluated"


def test_evaluation_insufficient_sample_is_never_validated(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    # only 2 usable units but min n=3 per variant — the declared minimum
    # can never be met (truthful insufficiency, not rejection)
    result = full_pipeline(learning, analytics, units=["vidA1", "vidA2"],
                           seed="fixed-seed", min_n=3)
    evaluation = result["evaluation"]
    assert evaluation["status"] == "insufficient_evidence"
    candidate = learning.get_candidate(result["candidate"]["candidate_id"])
    assert candidate["status"] == "insufficient_evidence"  # NOT 'rejected'


def test_evaluation_missing_metric_units_are_excluded_truthfully(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    seed_video(analytics, "vidA1", views=2000)
    seed_video(analytics, "vidA2", views=2000)
    seed_video(analytics, "vidA3", views=1000)
    seed_video(analytics, "vidA4", views=1000)
    for ghost in ("vidG1", "vidG2"):
        seed_video(analytics, ghost, views=500, omit=("views",))
    candidate = _candidate_for(learning, "h", "s")
    experiment = create_experiment(
        learning, candidate_id=candidate["candidate_id"],
        hypothesis=candidate["hypothesis"], metric="views",
        control_variant="control", treatment_variant="treatment",
        population={"units": ["vidA1", "vidA2", "vidA3", "vidA4",
                              "vidG1", "vidG2"]},
        assignment={"seed": "s"},
        success_criterion={"min_control_n": 1, "min_treatment_n": 1},
        now=NOW)["experiment"]
    approve_experiment(learning, experiment["experiment_id"], now=NOW)
    assign_units(learning, experiment["experiment_id"],
                 ["vidA1", "vidA2", "vidA3", "vidA4", "vidG1", "vidG2"],
                 now=NOW)
    evaluation = evaluate_experiment(learning, analytics,
                                     experiment["experiment_id"],
                                     now=NOW)["evaluation"]
    assert evaluation["control_n"] + evaluation["treatment_n"] == 4
    excluded = evaluation["details"]["excluded"]
    assert {e["unit_id"] for e in excluded} == {"vidG1", "vidG2"}
    # the exclusion reason is truthful (missing/unusable — never a zero)
    assert all("usable" in e["reason"] or "never measured" in e["reason"]
               for e in excluded)


def test_evaluation_rejects_when_criterion_fails(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    # treatment units measure LOWER than control → declared direction
    # not met → rejected (a truthful refutation)
    result = full_pipeline(learning, analytics, control_value=2000.0,
                           treatment_value=1000.0)
    evaluation = result["evaluation"]
    assert evaluation["status"] == "rejected"
    candidate = learning.get_candidate(result["candidate"]["candidate_id"])
    assert candidate["status"] == "rejected"   # distinct from insufficient
    rows = evaluation["observed"]["rows"]
    assert any(row["criterion"] == "treatment_greater_than_control"
               and not row["met"] for row in rows)


def test_evaluation_holdout_flow_is_truthful(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    result = full_pipeline(learning, analytics, n_units=8,
                           holdout={"fraction": 0.25, "min_n": 1},
                           holdout_balance=True)
    evaluation = result["evaluation"]
    holdout = evaluation.get("holdout")
    assert holdout is not None
    assert holdout["validation_method"] == "holdout_split_v1"
    assert holdout["dev_units"] and holdout["holdout_units"]
    assert not (set(holdout["dev_units"]) & set(holdout["holdout_units"]))
    # declared dev/holdout populations stay disjoint and complete
    assert set(holdout["dev_units"]) | set(holdout["holdout_units"]) == \
        set(holdout["dev_units"]) | set(holdout["holdout_units"])
    if holdout["control_n"] >= 1 and holdout["treatment_n"] >= 1:
        assert evaluation["status"] == "validated"
        assert "agrees" in holdout["note"]
    else:
        assert evaluation["status"] == "insufficient_evidence"
        assert "below declared minimum" in holdout["note"]


def test_evaluation_holdout_below_minimum_is_insufficient(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    # the deterministic seed search finds an assignment whose holdout
    # split lands BELOW the declared holdout minimum → the development
    # result CANNOT be confirmed (truthful insufficiency, never
    # silently validated)
    result = full_pipeline(learning, analytics, n_units=4,
                           holdout={"fraction": 0.25, "min_n": 2},
                           holdout_balance=False)
    evaluation = result["evaluation"]
    if evaluation["status"] == "validated":
        # a validated outcome requires a holdout that reached the
        # declared minimum in BOTH variants
        assert evaluation["holdout"] is not None
        assert evaluation["holdout"]["control_n"] >= 2
        assert evaluation["holdout"]["treatment_n"] >= 2
    elif evaluation["holdout"] is not None:
        # the holdout split existed but was below the declared minimum
        assert evaluation["status"] == "insufficient_evidence"
        assert "below declared minimum" in evaluation["holdout"]["note"]
    else:
        # the holdout consumed so many units that the DEVELOPMENT sample
        # fell below its own declared minimum — also truthful
        # insufficiency (never silently validated)
        assert evaluation["status"] == "insufficient_evidence"


def test_evaluation_is_idempotent_per_dataset(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    result = full_pipeline(learning, analytics)
    exp_id = result["experiment"]["experiment_id"]
    first = evaluate_experiment(learning, analytics, exp_id, now=NOW)
    second = evaluate_experiment(learning, analytics, exp_id, now=NOW)
    assert first["evaluation"]["evaluation_id"] == \
        second["evaluation"]["evaluation_id"]
    assert len(learning.list_evaluations(exp_id)) == 1
    # new data (a new observation) → NEW evaluation id; history retained
    seed_video(analytics, "vid000", views=9000, observed_at=NOW + "-new")
    third = evaluate_experiment(learning, analytics, exp_id, now=NOW)
    assert third["evaluation"]["evaluation_id"] != \
        first["evaluation"]["evaluation_id"]
    assert len(learning.list_evaluations(exp_id)) == 2


# ---- knowledge / curation (§25/§26/§29) -------------------------------------------------


def validated_setup(tmp_path, **kwargs):
    _, analytics, learning = make_env(tmp_path)
    result = full_pipeline(learning, analytics, **kwargs)
    return learning, analytics, result


def test_curation_creates_versioned_factual_knowledge(tmp_path):
    learning, _, result = validated_setup(tmp_path)
    curated = curate_candidate(
        learning, candidate_id=result["candidate"]["candidate_id"],
        evaluation_id=result["evaluation"]["evaluation_id"],
        now=NOW)["knowledge"]
    assert curated["knowledge_version"] == 1
    assert curated["status"] == "active"
    assert curated["candidate_id"] == result["candidate"]["candidate_id"]
    # the statement is a FACT (knowledge), never a policy directive
    statement = curated["statement"].lower()
    assert "within experiment" in statement
    assert "not a causal claim" in statement
    assert "not a production policy" in statement
    assert "always use" not in statement
    # validation summary is auditable
    assert curated["validation"]["evaluation_status"] == "validated"
    assert curated["validation"]["control_n"] is not None


def test_curation_is_idempotent_per_evaluation(tmp_path):
    learning, _, result = validated_setup(tmp_path)
    first = curate_candidate(
        learning, candidate_id=result["candidate"]["candidate_id"],
        evaluation_id=result["evaluation"]["evaluation_id"], now=NOW)
    second = curate_candidate(
        learning, candidate_id=result["candidate"]["candidate_id"],
        evaluation_id=result["evaluation"]["evaluation_id"], now=NOW)
    assert first["created"] is True and second["created"] is False
    assert second["knowledge"]["knowledge_id"] == \
        first["knowledge"]["knowledge_id"]
    assert second["knowledge"]["knowledge_version"] == 1
    assert len(learning.query_knowledge(status=None)) == 1


def test_knowledge_new_version_never_overwrites_history(tmp_path):
    learning, _, result = validated_setup(tmp_path)
    curated = curate_candidate(
        learning, candidate_id=result["candidate"]["candidate_id"],
        evaluation_id=result["evaluation"]["evaluation_id"], now=NOW)
    knowledge_id = curated["knowledge"]["knowledge_id"]
    # a NEW confirmed evaluation of the same statement is curated to a
    # NEW VERSION (the curator supersedes the old one; history retained)
    row = dict(learning.get_knowledge(knowledge_id))
    row["knowledge_version"] = 2
    row["evaluation_id"] = "eval-other-confirmation"
    assert learning.insert_knowledge(row=row)
    assert learning.supersede_knowledge(knowledge_id, below_version=2) == 1
    history = learning.knowledge_history(knowledge_id)
    assert [r["knowledge_version"] for r in history] == [1, 2]
    assert history[0]["status"] == "superseded"   # retained, marked
    assert history[1]["status"] == "active"       # newest is active
    assert history[0]["created_at"] == NOW        # v1 untouched


def test_curation_requires_validated_candidate(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    # rejected pipeline (treatment units measure LOWER)
    result = full_pipeline(learning, analytics, control_value=2000.0,
                           treatment_value=1000.0)
    with pytest.raises(LearningError) as excinfo:
        curate_candidate(
            learning, candidate_id=result["candidate"]["candidate_id"],
            evaluation_id=result["evaluation"]["evaluation_id"], now=NOW)
    assert excinfo.value.code == "learning_curation_failed"
    assert learning.query_knowledge(status=None) == []
    # mismatched evaluation → refused
    result2 = validated_setup_2(learning, analytics)
    with pytest.raises(LearningError) as excinfo:
        curate_candidate(
            learning, candidate_id=result2["candidate"]["candidate_id"],
            evaluation_id=result["evaluation"]["evaluation_id"], now=NOW)
    assert excinfo.value.code == "learning_curation_failed"


def validated_setup_2(learning, analytics):
    # a distinct hypothesis → a distinct candidate → a distinct
    # experiment (definitions are content-addressed; identical
    # definitions would return the SAME already-evaluated experiment)
    return full_pipeline(learning, analytics,
                         hypothesis="a second, independent confirmation")


def test_read_only_knowledge_query_with_scope(tmp_path):
    learning, _, result = validated_setup(tmp_path)
    curate_candidate(
        learning, candidate_id=result["candidate"]["candidate_id"],
        evaluation_id=result["evaluation"]["evaluation_id"], now=NOW)
    scope = result["candidate"]["scope"]
    hit = read_validated_knowledge(learning.path, scope=scope)
    assert hit["ok"] is True and len(hit["knowledge"]) == 1
    miss = read_validated_knowledge(learning.path, scope="other-scope")
    assert miss["knowledge"] == []
    missing = read_validated_knowledge(learning.path.parent / "nope.sqlite3")
    assert missing["ok"] is True and missing["knowledge"] == []


# ---- safety: learning ≠ production mutation (§42) ----------------------------------------


def test_learning_cannot_modify_production_or_analytics_state(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    # seed real production-artifact stand-ins + ledger-like files
    data_dir = tmp_path / "data"
    state_path = data_dir / "runs" / "run-x" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"run_id": "run-x"}', encoding="utf-8")
    ledger_path = data_dir / "publishing" / "ledger.sqlite3"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_bytes(b"ledger-bytes")
    research_path = data_dir / "research" / "res-x.json"
    research_path.parent.mkdir(parents=True)
    research_path.write_text('{"research_id": "res-x"}', encoding="utf-8")

    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    watched = {
        "state": digest(state_path),
        "ledger": digest(ledger_path),
        "research": digest(research_path),
        "analytics_db": digest(tmp_path / "data" / "analytics" /
                               "analytics.sqlite3"),
    }
    # exercise every mutating learning operation
    result = full_pipeline(learning, analytics)
    curate_candidate(learning,
                     candidate_id=result["candidate"]["candidate_id"],
                     evaluation_id=result["evaluation"]["evaluation_id"],
                     now=NOW)
    derive_all(analytics, learning, ["vid000"])

    assert digest(state_path) == watched["state"]
    assert digest(ledger_path) == watched["ledger"]
    assert digest(research_path) == watched["research"]
    # Stage 7 only READS the analytics DB — not one byte changed
    assert digest(tmp_path / "data" / "analytics" / "analytics.sqlite3") \
        == watched["analytics_db"]


def test_knowledge_statement_is_not_a_policy_object(tmp_path):
    learning, _, result = validated_setup(tmp_path)
    curated = curate_candidate(
        learning, candidate_id=result["candidate"]["candidate_id"],
        evaluation_id=result["evaluation"]["evaluation_id"], now=NOW)
    text = json.dumps(curated["knowledge"]).lower()
    for banned in ("always use", "change_policy", "changepolicy",
                   "recommended_topic", "increase_posting_frequency"):
        assert banned not in text, banned


# ---- restart (§38) ------------------------------------------------------------------------


def test_restart_preserves_learning_history(tmp_path):
    learning, analytics, result = validated_setup(tmp_path)
    curate_candidate(learning,
                     candidate_id=result["candidate"]["candidate_id"],
                     evaluation_id=result["evaluation"]["evaluation_id"],
                     now=NOW)
    # completely NEW store objects over the same files (process restart)
    _, analytics2, learning2 = make_env(tmp_path)
    state = learning2.list_all()
    assert state["candidates"] == 1
    assert state["experiments"] == 1
    assert state["evaluations"] == 1
    assert state["knowledge"] == 1
    assert state["derived_metrics"] == 0
    experiment = learning2.get_experiment(
        result["experiment"]["experiment_id"])
    assert experiment["status"] == "evaluated"
    assert learning2.assignments_for_experiment(
        result["experiment"]["experiment_id"])
    # analytics observations equally intact (immutable, §6)
    assert len(analytics2.list_observations(result["units"][0])) == 1


# ---- concurrency (§33) ----------------------------------------------------------------------


def test_concurrent_curation_writes_are_consistent(tmp_path):
    _, analytics, learning = make_env(tmp_path)
    result = full_pipeline(learning, analytics)
    errors: list[LearningError] = []
    # each worker opens its OWN connection over the same database file —
    # the realistic multi-process shape (SQLite transactions + UNIQUE
    # constraints arbitrate; no in-memory lock, §33)
    db_path = learning.path
    candidate_id = result["candidate"]["candidate_id"]
    evaluation_id = result["evaluation"]["evaluation_id"]

    def curate_worker():
        try:
            worker_store = LearningStore(db_path)
            curate_candidate(
                worker_store, candidate_id=candidate_id,
                evaluation_id=evaluation_id, now=NOW)
            worker_store.close()
        except LearningError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=curate_worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    # the store remains consistent: exactly ONE knowledge record,
    # version 1, no duplicates, no crashes
    knowledge = learning.query_knowledge(status=None)
    assert len(knowledge) == 1
    assert knowledge[0]["knowledge_version"] == 1
    assert not errors


def test_concurrent_candidate_creation_is_idempotent(tmp_path):
    _, _, learning = make_env(tmp_path)
    outcomes: list[str] = []
    errors: list[LearningError] = []
    db_path = learning.path

    def worker():
        try:
            worker_store = LearningStore(db_path)
            report = create_candidate(
                worker_store, hypothesis="concurrent hypothesis",
                scope="s", evidence=[{"kind": "measurement",
                                      "ref": "obs-1"}],
                counterexamples=[], now=NOW)
            worker_store.close()
            outcomes.append(report["candidate"]["candidate_id"])
        except LearningError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    assert len(set(outcomes)) == 1                    # same identity
    assert learning.list_all()["candidates"] == 1     # no duplicates


# ---- CLI (§35) -------------------------------------------------------------------------------


def test_cli_learning_end_to_end(tmp_path, capsys, monkeypatch):
    from ayce import cli
    _, analytics, learning = make_env(tmp_path)
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))

    # 1) derive (nothing observed yet → zero rows, still ok)
    rc = cli.main(["learning", "derive", "--json"])
    assert rc == 0
    derived = json.loads(capsys.readouterr().out)
    assert derived["ok"] is True and derived["derived"] == 0

    # 2) candidate
    rc = cli.main(["learning", "candidate", "--json",
                   "--hypothesis", "CLI hypothesis",
                   "--scope", "cli-scope",
                   "--evidence", "kind=measurement,ref=obs-1"])
    assert rc == 0
    candidate = json.loads(capsys.readouterr().out)["candidate"]

    # 3) experiment (draft)
    rc = cli.main(["learning", "experiment", "--json",
                   "--candidate-id", candidate["candidate_id"],
                   "--metric", "views", "--units",
                   "vid000,vid001,vid002,vid003,vid004,vid005",
                   "--assignment-seed", "cli-seed",
                   "--min-control-n", "1", "--min-treatment-n", "1",
                   "--min-effect", "1"])
    assert rc == 0
    experiment = json.loads(capsys.readouterr().out)["experiment"]
    exp_id = experiment["experiment_id"]
    assert experiment["definition"]["hypothesis"] == "CLI hypothesis"

    # 4) explicit approval → 5) deterministic assignment
    rc = cli.main(["learning", "approve", exp_id, "--json"])
    assert rc == 0
    capsys.readouterr()
    rc = cli.main(["learning", "assign", exp_id, "--json", "--units",
                   "vid000,vid001,vid002,vid003,vid004,vid005"])
    assert rc == 0
    assignment = json.loads(capsys.readouterr().out)

    # 6) outcomes follow the assigned variant (as in a real experiment)
    for index, entry in enumerate(assignment["assignments"]):
        seed_video(analytics, entry["unit_id"],
                   views=5000.0 + index if entry["variant"] == "treatment"
                   else 1000.0 + index)

    # 7) evaluate
    rc = cli.main(["learning", "evaluate", exp_id, "--json"])
    assert rc == 0
    evaluation = json.loads(capsys.readouterr().out)["evaluation"]
    assert evaluation["status"] == "validated"

    # 8) curation (validated → knowledge)
    rc = cli.main(["learning", "curate", "--json",
                   "--candidate-id", candidate["candidate_id"],
                   "--evaluation-id", evaluation["evaluation_id"]])
    assert rc == 0
    knowledge = json.loads(capsys.readouterr().out)["knowledge"]

    # 9) read-only knowledge query (the Hermes-facing view)
    rc = cli.main(["learning", "knowledge", "--json",
                   "--scope", candidate["scope"]])
    assert rc == 0
    query = json.loads(capsys.readouterr().out)
    assert query["ok"] is True
    assert knowledge["knowledge_id"] in \
        {k["knowledge_id"] for k in query["knowledge"]}


def test_cli_learning_dry_run_does_not_persist(tmp_path, capsys,
                                               monkeypatch):
    from ayce import cli
    _, analytics, learning = make_env(tmp_path)
    seed_video(analytics, "vidA1", views=100, likes=5, windowed_views=70)
    monkeypatch.setenv("AYCE_DATA_DIR", str(tmp_path / "data"))
    rc = cli.main(["learning", "derive", "--dry-run", "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True and report["rows"]
    # nothing persisted
    assert LearningStore(tmp_path / "data" / "learning" /
                         "learning.sqlite3").list_all()[
        "derived_metrics"] == 0


