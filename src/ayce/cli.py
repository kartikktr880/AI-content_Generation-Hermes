"""Command-line entry point: ``python -m ayce <command>``."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .config import Config, ConfigError
from .health import FAIL, CheckResult, render_text, run_checks


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ayce",
        description="AYCE — Autonomous YouTube Content Engine",
    )
    parser.add_argument("--version", action="version", version=f"ayce {__version__}")
    sub = parser.add_subparsers(dest="command")
    health = sub.add_parser("health", help="run baseline environment/diagnostic checks")
    health.add_argument("--json", action="store_true", help="output the report as JSON")
    run = sub.add_parser(
        "run", help="execute the Golden Path pipeline (script → QA report)"
    )
    run.add_argument("script", help="path to a script JSON input (ScriptInput contract)")
    run.add_argument(
        "--assets-dir",
        default=None,
        help="fixture directory for the file-backed asset provider (P2)",
    )
    run.add_argument(
        "--narration-dir",
        default=None,
        help="fixture directory for the file-backed narration provider (P3)",
    )
    run.add_argument("--json", action="store_true", help="output the pipeline report as JSON")
    package = sub.add_parser(
        "package",
        help="seal (or with --verify, verify) the publish package of one run",
    )
    package.add_argument("run_id", help="the production run identifier (run-…)")
    package.add_argument(
        "--verify",
        action="store_true",
        help="verify the existing sealed package instead of sealing",
    )
    package.add_argument("--json", action="store_true", help="output the report as JSON")
    publish = sub.add_parser(
        "publish",
        help="EXPLICITLY publish a sealed package to YouTube (never automatic)",
    )
    publish.add_argument(
        "package",
        help="a run id (run-…) or a publish_package.json path inside the data root",
    )
    publish.add_argument("--dry-run", action="store_true",
                         help="verify + validate + report intent; NEVER contact YouTube")
    publish.add_argument("--title", default=None, help="video title (default: scene manifest title)")
    publish.add_argument("--description", default=None, help="video description")
    publish.add_argument("--tags", default=None, help="comma-separated tags")
    publish.add_argument("--category-id", default=None, help="numeric YouTube category id")
    publish.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"],
                         help="privacy state (default: private — never auto-public)")
    publish.add_argument("--publish-at", default=None, help="ISO-8601 scheduled time (private only)")
    publish.add_argument("--json", action="store_true", help="output the report as JSON")
    publish_auth = sub.add_parser(
        "publish-auth",
        help="one-time YouTube OAuth setup: print the consent URL and exchange an authorization code",
    )
    publish_auth.add_argument("--auth-url", action="store_true",
                              help="print the Google consent URL (operator opens it in a browser)")
    publish_auth.add_argument("--auth-code", default=None,
                              help="the authorization code from the consent redirect")
    publish_auth.add_argument("--redirect-uri", default="urn:ietf:wg:oauth:2.0:oob",
                              help="the redirect URI used in the consent request")
    analytics = sub.add_parser(
        "analytics",
        help="OBSERVATION-ONLY analytics ingestion for published videos (Stage 6)",
    )
    analytics_sub = analytics.add_subparsers(dest="analytics_command")
    analytics_collect = analytics_sub.add_parser(
        "collect",
        help="collect and store one analytics observation for a published video",
    )
    analytics_collect.add_argument(
        "video_or_run",
        help="a YouTube video id (11 chars) or a run id (run-…) resolved "
             "through the publish ledger",
    )
    analytics_collect.add_argument("--window-start", default=None,
                                   help="Analytics API window start (YYYY-MM-DD)")
    analytics_collect.add_argument("--window-end", default=None,
                                   help="Analytics API window end (YYYY-MM-DD)")
    analytics_collect.add_argument(
        "--metrics", default=None,
        help="comma-separated metric names (default: the supported set per source)")
    analytics_collect.add_argument(
        "--source", default=None,
        help="restrict to one official source: DATA_API_V3 or ANALYTICS_API_V2")
    analytics_collect.add_argument(
        "--dry-run", action="store_true",
        help="resolve + validate + build the request; NO API call, NO storage")
    analytics_collect.add_argument("--json", action="store_true",
                                   help="output the report as JSON")
    analytics_show = analytics_sub.add_parser(
        "show",
        help="show the stored analytics observations of a published video",
    )
    analytics_show.add_argument(
        "video_or_run",
        help="a YouTube video id (11 chars) or a run id (run-…)")
    analytics_show.add_argument("--json", action="store_true",
                                help="output the report as JSON")
    learning = sub.add_parser(
        "learning",
        help="BOUNDED experimentation + learning over Stage 6 observations "
             "(Stage 7; explicit operator-controlled steps only)",
    )
    learning_sub = learning.add_subparsers(dest="learning_command")

    learning_derive = learning_sub.add_parser(
        "derive",
        help="derive provenance-carrying metrics from stored Stage 6 observations",
    )
    learning_derive.add_argument(
        "--video", default=None,
        help="restrict to one video id (default: all observed videos)")
    learning_derive.add_argument(
        "--formulas", default=None,
        help="comma-separated formula names (default: all supported)")
    learning_derive.add_argument("--dry-run", action="store_true",
                                 help="compute; do NOT persist")
    learning_derive.add_argument("--json", action="store_true",
                                 help="output the report as JSON")

    learning_candidate = learning_sub.add_parser(
        "candidate",
        help="register an explicit learning candidate (hypothesis + evidence)",
    )
    learning_candidate.add_argument("--hypothesis", required=True,
                                    help="the explicit hypothesis statement")
    learning_candidate.add_argument("--scope", required=True,
                                    help="knowledge scope (e.g. topic/format/hook)")
    learning_candidate.add_argument(
        "--evidence", required=True,
        help="semicolon-separated refs: kind=observation_id[,video=ID] entries")
    learning_candidate.add_argument(
        "--counterexamples", default=None,
        help="semicolon-separated refs (same format) that weaken the hypothesis")
    learning_candidate.add_argument("--json", action="store_true",
                                    help="output the report as JSON")

    learning_experiment = learning_sub.add_parser(
        "experiment",
        help="create an immutable, content-addressed experiment (draft)",
    )
    learning_experiment.add_argument("--candidate-id", required=True)
    learning_experiment.add_argument(
        "--metric", required=True,
        help="normalized Stage 6 metric or derived formula name")
    learning_experiment.add_argument(
        "--units", required=True,
        help="comma-separated unit ids (published video ids)")
    learning_experiment.add_argument(
        "--assignment-seed", required=True,
        help="explicit deterministic assignment seed")
    learning_experiment.add_argument(
        "--min-control-n", type=int, default=2,
        help="declared minimum control sample size")
    learning_experiment.add_argument(
        "--min-treatment-n", type=int, default=2,
        help="declared minimum treatment sample size")
    learning_experiment.add_argument(
        "--min-effect", type=float, default=0.0,
        help="declared minimum |effect|")
    learning_experiment.add_argument(
        "--max-p", type=float, default=None,
        help="declared p-value threshold (opt-in; never implicit)")
    learning_experiment.add_argument(
        "--holdout-fraction", type=float, default=None,
        help="deterministic holdout fraction (0,1) for holdout validation")
    learning_experiment.add_argument(
        "--evaluation-window", default=None,
        help="declared evaluation window START..END (YYYY-MM-DD)")
    learning_experiment.add_argument("--json", action="store_true",
                                     help="output the report as JSON")

    learning_approve = learning_sub.add_parser(
        "approve",
        help="EXPLICITLY approve a draft experiment (the launch boundary)",
    )
    learning_approve.add_argument("experiment_id")
    learning_approve.add_argument("--json", action="store_true",
                                  help="output the report as JSON")

    learning_start = learning_sub.add_parser(
        "start", help="mark an approved experiment as running")
    learning_start.add_argument("experiment_id")
    learning_start.add_argument("--json", action="store_true",
                                help="output the report as JSON")

    learning_assign = learning_sub.add_parser(
        "assign",
        help="deterministically assign units to control/treatment",
    )
    learning_assign.add_argument("experiment_id")
    learning_assign.add_argument(
        "--units", required=True,
        help="comma-separated unit ids (must be in the declared population)")
    learning_assign.add_argument(
        "--dry-run", action="store_true",
        help="compute the assignment; do NOT persist")
    learning_assign.add_argument("--json", action="store_true",
                                 help="output the report as JSON")

    learning_evaluate = learning_sub.add_parser(
        "evaluate",
        help="evaluate a running experiment against its declared criterion",
    )
    learning_evaluate.add_argument("experiment_id")
    learning_evaluate.add_argument("--json", action="store_true",
                                   help="output the report as JSON")

    learning_curate = learning_sub.add_parser(
        "curate",
        help="promote a VALIDATED candidate+evaluation into versioned knowledge",
    )
    learning_curate.add_argument("--candidate-id", required=True)
    learning_curate.add_argument("--evaluation-id", required=True)
    learning_curate.add_argument("--json", action="store_true",
                                 help="output the report as JSON")

    learning_knowledge = learning_sub.add_parser(
        "knowledge",
        help="read validated knowledge (the Hermes-facing read-only view)",
    )
    learning_knowledge.add_argument("--scope", default=None,
                                    help="exact scope filter (no semantic search)")
    learning_knowledge.add_argument("--json", action="store_true",
                                    help="output the report as JSON")

    learning_show = learning_sub.add_parser(
        "show", help="show the stored learning state (all layers)")
    learning_show.add_argument("--json", action="store_true",
                               help="output the report as JSON")

    policy_cmd = sub.add_parser(
        "policy",
        help="CONTROLLED policy promotion: validated knowledge → candidate "
             "→ EXPLICIT operator approval → immutable versioned "
             "ProductionPolicy → EXPLICIT activation (Stage 8; "
             "operator-only writes; Hermes is read-only)",
    )
    policy_sub = policy_cmd.add_subparsers(dest="policy_command")

    policy_candidate = policy_sub.add_parser(
        "candidate",
        help="compile an explicit policy candidate from VALIDATED knowledge "
             "references (deterministic compiler; fail-closed)",
    )
    policy_candidate.add_argument(
        "--knowledge", action="append", required=True,
        help="validated knowledge ref: knw-…[@version] (repeatable)")
    policy_candidate.add_argument(
        "--scope", required=True,
        help="policy scope: 'global' or '<dimension>:<value>' (dimension: "
             "channel/content_type/format/topic/series); must EQUAL the "
             "knowledge scope")
    policy_candidate.add_argument(
        "--rationale", required=True,
        help="the operator's explicit rationale (audited)")
    policy_candidate.add_argument(
        "--action", default="prefer",
        help="supported policy action (default: prefer)")
    policy_candidate.add_argument(
        "--priority", type=int, default=1,
        help="rule priority 1..100 (default: 1)")
    policy_candidate.add_argument(
        "--dry-run", action="store_true",
        help="compile + validate; do NOT persist the candidate")
    policy_candidate.add_argument("--json", action="store_true",
                                  help="output the report as JSON")

    policy_show = policy_sub.add_parser(
        "show", help="show the policy store state (or one candidate/policy)")
    policy_show.add_argument("--candidate-id", default=None)
    policy_show.add_argument("--policy-id", default=None)
    policy_show.add_argument("--policy-version", type=int, default=None)
    policy_show.add_argument("--json", action="store_true",
                             help="output the report as JSON")

    policy_diff = policy_sub.add_parser(
        "diff",
        help="deterministic diff of a candidate/policy against the ACTIVE "
             "policy of its scope (audited before activation)")
    policy_diff.add_argument("--candidate-id", default=None)
    policy_diff.add_argument("--policy-id", default=None)
    policy_diff.add_argument("--policy-version", type=int, default=None)
    policy_diff.add_argument("--json", action="store_true",
                             help="output the report as JSON")

    policy_approve = policy_sub.add_parser(
        "approve",
        help="EXPLICIT operator approval of a policy candidate (the "
             "promotion boundary; idempotent; never automatic)")
    policy_approve.add_argument("--candidate-id", required=True)
    policy_approve.add_argument(
        "--approved-by", required=True,
        help="operator identity recorded in the audit trail")
    policy_approve.add_argument("--json", action="store_true",
                                help="output the report as JSON")

    policy_promote = policy_sub.add_parser(
        "promote",
        help="compile an APPROVED candidate into an immutable versioned "
             "ProductionPolicy (requires prior explicit approval)")
    policy_promote.add_argument("--candidate-id", required=True)
    policy_promote.add_argument("--dry-run", action="store_true",
                                help="compile + validate; do NOT persist")
    policy_promote.add_argument("--json", action="store_true",
                                help="output the report as JSON")

    policy_activate = policy_sub.add_parser(
        "activate",
        help="EXPLICITLY activate an approved policy for its scope "
             "(one active policy per scope; idempotent)")
    policy_activate.add_argument("--policy-id", required=True)
    policy_activate.add_argument("--policy-version", type=int, default=None)
    policy_activate.add_argument("--json", action="store_true",
                                 help="output the report as JSON")

    policy_rollback = policy_sub.add_parser(
        "rollback",
        help="EXPLICITLY roll back by re-activating a previously active "
             "(superseded) immutable policy version (history retained)")
    policy_rollback.add_argument("--policy-id", required=True)
    policy_rollback.add_argument("--policy-version", type=int, default=None)
    policy_rollback.add_argument("--json", action="store_true",
                                 help="output the report as JSON")

    policy_retire = policy_sub.add_parser(
        "retire", help="EXPLICITLY retire the active policy (terminal)")
    policy_retire.add_argument("--policy-id", required=True)
    policy_retire.add_argument("--policy-version", type=int, default=None)
    policy_retire.add_argument("--json", action="store_true",
                               help="output the report as JSON")

    policy_active = policy_sub.add_parser(
        "active",
        help="read the ACTIVE policy for a scope (the Hermes-facing "
             "read-only view; SQLite mode=ro)")
    policy_active.add_argument("--scope", required=True)
    policy_active.add_argument("--json", action="store_true",
                               help="output the report as JSON")

    policy_reject = policy_sub.add_parser(
        "reject",
        help="EXPLICITLY reject a draft candidate (terminal: a rejected "
             "candidate can never be promoted)")
    policy_reject.add_argument("--candidate-id", required=True)
    policy_reject.add_argument("--reason", required=True)
    policy_reject.add_argument("--json", action="store_true",
                               help="output the report as JSON")

    policy_consumption = policy_sub.add_parser(
        "consumption",
        help="OBSERVE the policy consumption evidence of a run (Stage 10; "
             "read-only: which decision consumed which policy version, "
             "scope, and context hashes)")
    policy_consumption.add_argument("--run-id", required=True)
    policy_consumption.add_argument(
        "--decision-id", default=None,
        help="optional exact decision_id filter")
    policy_consumption.add_argument(
        "--policy-id", default=None,
        help="optional exact policy_id filter")
    policy_consumption.add_argument(
        "--policy-version", type=int, default=None,
        help="optional exact policy_version filter")
    policy_consumption.add_argument(
        "--verify", action="store_true",
        help="cross-check each event against the authoritative policy "
             "store (historical verification; never requires active)")
    policy_consumption.add_argument("--json", action="store_true",
                                    help="output the report as JSON")

    policy_lineage = policy_sub.add_parser(
        "lineage",
        help="CROSS-RUN policy lineage projection (Stage 11; derived "
             "read-only index over canonical consumption artifacts)")
    lineage_sub = policy_lineage.add_subparsers(dest="lineage_command")

    lineage_run = lineage_sub.add_parser(
        "run", help="Run → Policies: which policy versions this run consumed")
    lineage_run.add_argument("--run-id", required=True)
    lineage_run.add_argument("--scope", default=None,
                             help="optional exact scope filter")
    lineage_run.add_argument("--json", action="store_true",
                             help="output the report as JSON")

    lineage_policy = lineage_sub.add_parser(
        "policy", help="Policy → Runs: which runs consumed this policy")
    lineage_policy.add_argument("--policy-id", required=True)
    lineage_policy.add_argument("--policy-version", type=int, default=None,
                                help="optional exact version filter")
    lineage_policy.add_argument("--scope", default=None,
                                help="optional exact scope filter")
    lineage_policy.add_argument("--json", action="store_true",
                                help="output the report as JSON")

    lineage_verify = lineage_sub.add_parser(
        "verify", help="verify lineage integrity across all runs")
    lineage_verify.add_argument("--json", action="store_true",
                                help="output the report as JSON")

    lineage_rebuild = lineage_sub.add_parser(
        "rebuild",
        help="recompute the derived projection (read-only over all "
             "canonical stores; nothing persisted)")
    lineage_rebuild.add_argument("--json", action="store_true",
                                 help="output the report as JSON")

    policy_effectiveness = policy_sub.add_parser(
        "effectiveness",
        help="POLICY EFFECTIVENESS evidence projection (Stage 12; READ-ONLY "
             "correlation: Policy → Run → Publish → Video → Analytics; "
             "descriptive evidence only — no ranking, no causal claim)")
    eff_sub = policy_effectiveness.add_subparsers(
        dest="effectiveness_command")

    eff_run = eff_sub.add_parser(
        "run",
        help="Run → evidence: which policy this run consumed, its publish "
             "status (authoritative ledger) and its analytics status")
    eff_run.add_argument("--run-id", required=True)
    eff_run.add_argument("--json", action="store_true",
                         help="output the report as JSON")

    eff_policy = eff_sub.add_parser(
        "policy",
        help="Policy → evidence: runs consuming a policy version, their "
             "publish/analytics status and descriptive outcome summaries")
    eff_policy.add_argument("--policy-id", required=True)
    eff_policy.add_argument("--policy-version", type=int, default=None,
                            help="optional exact version filter")
    eff_policy.add_argument("--metric", default=None,
                            help="optional exact metric-name filter")
    eff_policy.add_argument("--video-id", default=None,
                            help="optional exact video-id filter")
    eff_policy.add_argument("--json", action="store_true",
                            help="output the report as JSON")

    eff_metric = eff_sub.add_parser(
        "metric",
        help="Metric → descriptive per-policy-version outcome summaries "
             "(labeled descriptive_comparison; groups never merge sources "
             "or windows; no ranking)")
    eff_metric.add_argument("--metric", required=True)
    eff_metric.add_argument("--policy-id", default=None,
                            help="optional exact policy_id filter")
    eff_metric.add_argument("--policy-version", type=int, default=None,
                            help="optional exact version filter")
    eff_metric.add_argument("--source", default=None,
                            help="optional exact source filter "
                                 "(DATA_API_V3 | ANALYTICS_API_V2)")
    eff_metric.add_argument("--window-start", default=None,
                            help="optional exact window_start filter")
    eff_metric.add_argument("--window-end", default=None,
                            help="optional exact window_end filter")
    eff_metric.add_argument("--json", action="store_true",
                            help="output the report as JSON")

    eff_verify = eff_sub.add_parser(
        "verify",
        help="verify effectiveness correlation integrity across all runs "
             "(fail-closed; nothing is mutated)")
    eff_verify.add_argument("--json", action="store_true",
                            help="output the report as JSON")

    # Stage 13 — experiment INTAKE candidates (derived from Stage 12
    # effectiveness evidence; approval into the existing Stage 7 learning
    # store is a SEPARATE explicit operation)
    exp_cand = policy_sub.add_parser(
        "experiment-candidate",
        help="Stage 13 experiment INTAKE candidates derived from Stage 12 "
             "effectiveness evidence (READ-ONLY projection — no causal "
             "claim, no ranking; approval into the existing Stage 7 "
             "learning store is a SEPARATE explicit operation)")
    ec_sub = exp_cand.add_subparsers(dest="experiment_candidate_command")

    ec_policy = ec_sub.add_parser(
        "policy",
        help="Policy → intake candidates derived from its Stage 12 "
             "effectiveness evidence")
    ec_policy.add_argument("--policy-id", required=True)
    ec_policy.add_argument("--policy-version", type=int, default=None)
    ec_policy.add_argument("--metric", default=None,
                           help="optional exact metric-name filter")
    ec_policy.add_argument("--json", action="store_true",
                           help="output the report as JSON")

    ec_metric = ec_sub.add_parser(
        "metric",
        help="Metric → intake candidates across all policies with "
             "evidence (identities never merge across sources/windows)")
    ec_metric.add_argument("--metric", required=True)
    ec_metric.add_argument("--policy-id", default=None)
    ec_metric.add_argument("--policy-version", type=int, default=None)
    ec_metric.add_argument("--source", default=None)
    ec_metric.add_argument("--window-start", default=None)
    ec_metric.add_argument("--window-end", default=None)
    ec_metric.add_argument("--json", action="store_true",
                           help="output the report as JSON")

    ec_show = ec_sub.add_parser(
        "show", help="show ONE derived candidate by its content-addressed id")
    ec_show.add_argument("--candidate-id", required=True)
    ec_show.add_argument("--json", action="store_true",
                         help="output the report as JSON")

    ec_verify = ec_sub.add_parser(
        "verify",
        help="verify intake derivation integrity (deterministic "
             "re-derivation; fail-closed; nothing is mutated)")
    ec_verify.add_argument("--json", action="store_true",
                           help="output the report as JSON")

    ec_approve = ec_sub.add_parser(
        "approve",
        help="EXPLICIT OPERATOR APPROVAL: materialize ONE eligible "
             "candidate into the EXISTING Stage 7 learning store (via "
             "ayce.learning.create_candidate); no experiment is created, "
             "approved or started")
    ec_approve.add_argument("--candidate-id", required=True)
    ec_approve.add_argument("--approved-by", required=True,
                            help="auditable operator identity")
    ec_approve.add_argument("--json", action="store_true",
                            help="output the report as JSON")

    # Stage 14 — controlled experiment COMPOSITION boundary (derived
    # definition proposals over APPROVED Stage 13 candidates; approval
    # creates the Stage 7 DRAFT experiment ONLY through the existing
    # Stage 7 owner; the seed is NEVER invented)
    exp_def = policy_sub.add_parser(
        "experiment-definition",
        help="Stage 14 experiment DEFINITION proposals composed from "
             "approved Stage 13 candidates (READ-ONLY derivation — "
             "nothing runs automatically; approval creates the existing "
             "Stage 7 DRAFT experiment only)")
    ed_sub = exp_def.add_subparsers(dest="experiment_definition_command")

    ed_run = ed_sub.add_parser(
        "run",
        help="compose the definition proposal for ONE approved candidate")
    ed_run.add_argument("--candidate-id", required=True)
    ed_run.add_argument("--control-variant", default=None,
                        help="optional operator-supplied control variant "
                             "(resolves an unresolved policy comparison; "
                             "requires --treatment-variant)")
    ed_run.add_argument("--treatment-variant", default=None,
                        help="optional operator-supplied treatment variant "
                             "(requires --control-variant)")
    ed_run.add_argument("--min-effect", type=float, default=None,
                        help="optional operator-declared minimum effect "
                             "(Stage 7 declares thresholds, never implicit)")
    ed_run.add_argument("--json", action="store_true",
                        help="output the report as JSON")

    ed_show = ed_sub.add_parser(
        "show", help="show ONE derived proposal by its content-addressed id")
    ed_show.add_argument("--proposal-id", required=True)
    ed_show.add_argument("--json", action="store_true",
                         help="output the report as JSON")

    ed_verify = ed_sub.add_parser(
        "verify",
        help="verify composition integrity (deterministic re-derivation; "
             "fail-closed; nothing is mutated)")
    ed_verify.add_argument("--json", action="store_true",
                           help="output the report as JSON")

    ed_approve = ed_sub.add_parser(
        "approve",
        help="EXPLICIT OPERATOR APPROVAL: create the Stage 7 DRAFT "
             "experiment for ONE ready proposal through the existing "
             "Stage 7 owner (the ONLY mutation; never started, no "
             "assignments)")
    ed_approve.add_argument("--proposal-id", required=True)
    ed_approve.add_argument("--approved-by", required=True,
                            help="auditable operator identity")
    ed_approve.add_argument("--seed", required=True,
                            help="explicit deterministic assignment seed "
                                 "(NEVER invented by Stage 14)")
    ed_approve.add_argument("--control-variant", default=None,
                            help="optional operator-supplied control "
                                 "variant (must pair with "
                                 "--treatment-variant)")
    ed_approve.add_argument("--treatment-variant", default=None,
                            help="optional operator-supplied treatment "
                                 "variant (must pair with --control-variant)")
    ed_approve.add_argument("--min-effect", type=float, default=None,
                            help="optional operator-declared minimum effect")
    ed_approve.add_argument("--dry-run", action="store_true",
                            help="compose and validate WITHOUT creating "
                                 "anything (nothing is stored)")
    ed_approve.add_argument("--json", action="store_true",
                            help="output the report as JSON")
    return parser



def _cmd_health(args: argparse.Namespace) -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        result = CheckResult("config", FAIL, str(exc))
        if getattr(args, "json", False):
            print(json.dumps({"checks": [result.__dict__], "ok": False}, indent=2))
        else:
            print(render_text([result]))
        return 1

    results = run_checks(config)
    ok = all(r.status != FAIL for r in results)
    if getattr(args, "json", False):
        print(json.dumps({"ok": ok, "checks": [r.__dict__ for r in results]}, indent=2))
    else:
        print(render_text(results))
    return 0 if ok else 1


def _cmd_run(args: argparse.Namespace) -> int:
    # Imported here so `ayce health` never pays the pipeline import cost.
    from pathlib import Path

    from .pipeline import run_pipeline

    # validate the input path BEFORE creating any run state (no partial runs)
    script = Path(args.script)
    if not script.is_file():
        print(f"ayce run: script input not found: {script}", file=sys.stderr)
        return 2

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"ayce run: configuration error: {exc}", file=sys.stderr)
        return 2

    result = run_pipeline(
        script,
        config=config,
        assets_dir=args.assets_dir,
        narration_dir=args.narration_dir,
    )

    if getattr(args, "json", False):
        print(json.dumps({
            "ok": result.ok,
            "run_id": result.run_id,
            "job_id": result.job_id,
            "run_dir": str(result.run_dir),
            "production_id": result.production_id,
            "stages": [
                {"stage": o.stage, "ok": o.ok, "error": o.error} for o in result.stages
            ],
            "qa_verdict": result.qa_verdict,
            "failed_stage": result.failed_stage,
            "error": result.error,
            # Stage 3 (additive): durable lineage references when the run
            # is research-derived; null otherwise.
            "lineage": result.lineage,
        }, indent=2))
    else:
        print("ayce run — golden path pipeline")
        for outcome in result.stages:
            status = "OK" if outcome.ok else "FAILED"
            line = f"  [{status}] {outcome.stage}"
            if outcome.error:
                line += f" — {outcome.error}"
            print(line)
        print(f"run_id: {result.run_id}")
        print(f"run_dir: {result.run_dir}")
        if result.production_id:
            print(f"production_id: {result.production_id}")
        if result.lineage:
            lineage = result.lineage
            if lineage.get("research_id"):
                print(f"research_id: {lineage['research_id']}")
            if lineage.get("objective_id"):
                print(f"objective_id: {lineage['objective_id']}")
        if result.qa_verdict is not None:
            print(f"qa_verdict: {result.qa_verdict}")
        if not result.ok:
            print(
                f"pipeline failed at stage {result.failed_stage!r}: {result.error}",
                file=sys.stderr,
            )
    return 0 if result.ok else 1


def _cmd_package(args: argparse.Namespace) -> int:
    # Imported here so `ayce health` never pays the import cost.
    from .publish_package import (
        PackageError,
        load_publish_package,
        resolve_run_dir,
        seal_publish_package,
        verify_publish_package,
    )

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"ayce package: configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        run_dir = resolve_run_dir(config, args.run_id)
        if args.verify:
            package = load_publish_package(run_dir)
            if package is None:
                print(f"ayce package: no sealed package for run {args.run_id}",
                      file=sys.stderr)
                return 1
            report = verify_publish_package(package, run_dir=run_dir)
        else:
            package = seal_publish_package(run_dir)
            report = verify_publish_package(package, run_dir=run_dir)
    except PackageError as exc:
        if getattr(args, "json", False):
            print(json.dumps(exc.to_dict(), indent=2))
        else:
            print(f"ayce package: {exc.code}: {exc.message}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
    else:
        status = "OK" if report["ok"] else "FAILED"
        depth = "content-verified" if report.get("verified_content") else "seal-only"
        print(f"ayce package — {args.run_id}")
        print(f"  [{status}] {depth}")
        print(f"package_id: {report.get('package_id')}")
        for err in report["errors"]:
            print(f"  error: {err['code']}: {err['message']}", file=sys.stderr)
    return 0 if report["ok"] else 1


def _cmd_publish(args: argparse.Namespace) -> int:
    # Imported here so `ayce health` never pays the import cost.
    from .publishing import (
        PublishError,
        PublishLedger,
        PublishRequest,
        TokenProvider,
        UrllibYouTubeTransport,
        publish_package_to_youtube,
        resolve_package,
    )

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"ayce publish: configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        # package resolution + verification happen BEFORE any credential use
        resolved = resolve_package(config, args.package)
        metadata_request = PublishRequest(
            title=args.title,
            description=args.description,
            tags=[t.strip() for t in args.tags.split(",")] if args.tags else None,
            category_id=args.category_id,
            privacy_status=args.privacy,
            publish_at=args.publish_at,
        )
        ledger = PublishLedger(config.resolved_data_dir / "publishing" / "ledger.sqlite3")
        token_provider = None
        transport = None
        if not args.dry_run:
            token_provider = TokenProvider.from_config(config)
            transport = UrllibYouTubeTransport()
        report = publish_package_to_youtube(
            args.package, metadata_request, config=config, ledger=ledger,
            token_provider=token_provider, transport=transport,
            dry_run=args.dry_run,
        )
    except PublishError as exc:
        if getattr(args, "json", False):
            print(json.dumps(exc.to_dict(), indent=2))
        else:
            print(f"ayce publish: {exc.code}: {exc.message}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        label = "DRY-RUN (no external contact)" if report.get("dry_run") \
            else f"status: {report.get('status')}"
        print(f"ayce publish — {report.get('package_id')}")
        print(f"  [{label}]")
        if report.get("youtube_video_id"):
            print(f"youtube_video_id: {report['youtube_video_id']}")
        if report.get("message"):
            print(report["message"])
    return 0 if report.get("ok") else 1


def _cmd_publish_auth(args: argparse.Namespace) -> int:
    from .config import ConfigError as _CE  # noqa: F401  (clarity)
    from .publishing import GoogleTokenClient, TokenProvider
    import json as _json

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"ayce publish-auth: configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        client_id = config.require("YT_CLIENT_ID")
        client_secret = config.require("YT_CLIENT_SECRET")
    except ConfigError as exc:
        print(f"ayce publish-auth: missing configuration: {exc}", file=sys.stderr)
        return 2
    client = GoogleTokenClient()
    if args.auth_url or not args.auth_code:
        print("1) open this URL in a browser and approve the channel access:")
        print(client.authorization_url(client_id, args.redirect_uri))
        if not args.auth_code:
            print("2) re-run: ayce publish-auth --auth-code <code> "
                  "--redirect-uri <the same redirect uri>")
        return 0
    try:
        response = client.exchange_authorization_code(
            client_id=client_id, client_secret=client_secret,
            authorization_code=args.auth_code, redirect_uri=args.redirect_uri)
    except Exception as exc:  # noqa: BLE001 — classified, truthful
        print(f"ayce publish-auth: exchange failed: {exc}", file=sys.stderr)
        return 1
    refresh_token = response.get("refresh_token")
    if not refresh_token:
        print("ayce publish-auth: no refresh_token in the response (re-run "
              "consent with prompt=consent)", file=sys.stderr)
        return 1
    print("refresh_token obtained — store it ONCE in your git-ignored .env:")
    print(f"  AYCE_YT_CLIENT_ID={client_id}")
    print("  AYCE_YT_CLIENT_SECRET=<already configured>")
    print(f"  AYCE_YT_REFRESH_TOKEN={refresh_token}")
    return 0


def _cmd_analytics(args: argparse.Namespace) -> int:
    # Imported here so `ayce health` never pays the import cost.
    from .analytics import AnalyticsError, collect_analytics, show_analytics
    from .publishing import PublishLedger

    if getattr(args, "analytics_command", None) not in ("collect", "show"):
        print("ayce analytics: choose a subcommand: collect | show", file=sys.stderr)
        return 2

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"ayce analytics: configuration error: {exc}", file=sys.stderr)
        return 2

    ledger = PublishLedger(config.resolved_data_dir / "publishing" / "ledger.sqlite3")
    try:
        if args.analytics_command == "collect":
            sources = [args.source] if getattr(args, "source", None) else None
            metrics = None
            if getattr(args, "metrics", None):
                metrics = [m.strip() for m in args.metrics.split(",") if m.strip()]
            report = collect_analytics(
                args.video_or_run, config=config, ledger=ledger,
                sources=sources, metrics=metrics,
                window_start=args.window_start, window_end=args.window_end,
                dry_run=args.dry_run)
        else:
            report = show_analytics(args.video_or_run, config=config, ledger=ledger)
    except AnalyticsError as exc:
        if getattr(args, "json", False):
            print(json.dumps(exc.to_dict(), indent=2))
        else:
            print(f"ayce analytics: {exc.code}: {exc.message}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
    elif args.analytics_command == "collect":
        label = "DRY-RUN (no external contact, nothing stored)" if report.get("dry_run") \
            else "COLLECTED"
        print(f"ayce analytics collect — {report.get('youtube_video_id')}")
        print(f"  [{label}]")
        print(f"run: {report.get('run_id')}  package: {report.get('package_id')}")
        if report.get("dry_run"):
            for plan in report.get("would_collect", []):
                window = (f"{plan['window_start']}..{plan['window_end']}"
                          if plan.get("window_start") else "lifetime cumulative")
                print(f"  would collect from {plan['source']} "
                      f"(window: {window}): {', '.join(plan['metrics'])}")
        else:
            for obs in report.get("observations", []):
                window = (f"{obs['window_start']}..{obs['window_end']}"
                          if obs.get("window_start") else "lifetime cumulative")
                dup = " [duplicate — already stored, unchanged]" if obs.get("duplicate") else ""
                print(f"  observation {obs['observation_id']} "
                      f"({obs['source']}, window: {window}, "
                      f"observed_at: {obs['observed_at']}){dup}")
                for metric in obs.get("metrics", []):
                    value = metric.get("value")
                    value = "—" if value is None else value
                    print(f"    {metric['metric_name']}: {value} "
                          f"({metric.get('unit')}) [{metric['availability']}]")
    else:
        observations = report.get("observations", [])
        measurements = report.get("measurements", [])
        print(f"ayce analytics show — {report.get('youtube_video_id')}")
        print(f"run: {report.get('run_id')}  package: {report.get('package_id')}")
        lineage = report.get("lineage") or {}
        chain = " → ".join(str(lineage.get(k)) for k in
                           ("run_id", "script_id", "research_id", "objective_id"))
        print(f"lineage: {chain}")
        print(f"{len(observations)} observation(s), {len(measurements)} measurement(s)")
        for obs in observations:
            print(f"  {obs['observation_id']} source={obs['source']} "
                  f"observed_at={obs['observed_at']} "
                  f"window={obs.get('window_start') or 'lifetime'}.."
                  f"{obs.get('window_end') or 'cumulative'}")
    return 0


def _cmd_learning(args: argparse.Namespace) -> int:
    # Imported here so `ayce health` never pays the import cost.
    from .learning import LearningError, LearningStore
    from .analytics import AnalyticsStore

    command = getattr(args, "learning_command", None)
    if command not in ("derive", "candidate", "experiment", "approve",
                       "start", "assign", "evaluate", "curate",
                       "knowledge", "show"):
        print("ayce learning: choose a subcommand: derive | candidate | "
              "experiment | approve | start | assign | evaluate | curate "
              "| knowledge | show", file=sys.stderr)
        return 2

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"ayce learning: configuration error: {exc}", file=sys.stderr)
        return 2

    learning_store = LearningStore(
        config.resolved_data_dir / "learning" / "learning.sqlite3")
    analytics_store = AnalyticsStore(
        config.resolved_data_dir / "analytics" / "analytics.sqlite3")
    now_fn = (lambda: __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc)
        .isoformat(timespec="milliseconds").replace("+00:00", "Z"))

    def _evidence(text):
        """Parse ``kind=ref[,video=ID]`` entries (deterministic refs)."""
        if not text:
            return []
        entries = []
        for part in text.split(";"):
            part = part.strip()
            if not part:
                continue
            entry = {}
            for piece in part.split(","):
                piece = piece.strip()
                if "=" not in piece:
                    print(f"ayce learning: bad evidence entry: {piece!r} "
                          "(expected kind=ref[,video=ID])", file=sys.stderr)
                    raise SystemExit(2)
                key, _, value = piece.partition("=")
                entry["video_id" if key == "video" else key] = value
            if "kind" not in entry or "ref" not in entry:
                print("ayce learning: evidence entries need kind= and ref=",
                      file=sys.stderr)
                raise SystemExit(2)
            entries.append(entry)
        return entries

    try:
        report = _learning_dispatch(args, command, learning_store,
                                    analytics_store, now_fn, _evidence)
    except LearningError as exc:
        if getattr(args, "json", False):
            print(json.dumps(exc.to_dict(), indent=2))
        else:
            print(f"ayce learning: {exc.code}: {exc.message}",
                  file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    return _print_learning(command, report)


def _print_learning(command: str, report: dict) -> int:
    if command == "derive":
        label = "DRY-RUN (nothing stored)" if report.get("dry_run") \
            else "DERIVED"
        print(f"ayce learning derive — [{label}] {report['derived']} "
              f"derived metric(s), "
              f"{report.get('new', report['derived'])} new")
        for row in report["rows"]:
            value = "—" if row["value"] is None else row["value"]
            note = f" ({row['note']})" if row.get("note") else ""
            print(f"  {row['formula']} ({row['formula_version']}) "
                  f"{row['youtube_video_id']} @ {row['observed_at']}: "
                  f"{value} [{row['availability']}]{note}")
        return 0
    if command == "candidate":
        candidate = report["candidate"]
        state = "already registered" if candidate.get("duplicate") \
            else "registered"
        print(f"ayce learning candidate — {candidate['candidate_id']}"
              f" [{state}]")
        print(f"  hypothesis: {candidate['hypothesis']}")
        print(f"  scope: {candidate['scope']}  status: "
              f"{candidate['status']}")
        print(f"  evidence: {len(candidate['evidence'])} ref(s), "
              f"counterexamples: {len(candidate['counterexamples'])} ref(s)")
        return 0
    if command == "experiment":
        experiment = report["experiment"]
        state = "already registered" if experiment.get("duplicate") \
            else "registered (draft)"
        d = experiment["definition"]
        print(f"ayce learning experiment — {experiment['experiment_id']}"
              f" [{state}]")
        print(f"  metric: {d['metric']}  unit: {d['unit']}  variants: "
              f"{d['control_variant']} vs {d['treatment_variant']}")
        print(f"  criterion: {d['success_criterion']}")
        print(f"  holdout: {d['holdout']}  assignment: {d['assignment']}")
        return 0
    if command in ("approve", "start"):
        print(f"ayce learning {command} — {report['experiment_id']} "
              f"→ {report['status']}")
        return 0
    if command == "assign":
        label = "DRY-RUN (nothing stored)" if report.get("dry_run") \
            else "ASSIGNED"
        print(f"ayce learning assign — [{label}]")
        for a in report["assignments"]:
            print(f"  {a['unit_id']}: {a['variant']}"
                  + (" (existing — unchanged)" if a["previously_assigned"]
                     else ""))
        return 0
    if command == "evaluate":
        evaluation = report["evaluation"]
        label = "DRY-RUN (nothing stored)" if report.get("dry_run") \
            else "EVALUATED"
        print(f"ayce learning evaluate — [{label}] "
              f"{evaluation['evaluation_id']}")
        print(f"  status: {evaluation['status']}  control_n: "
              f"{evaluation['control_n']}  treatment_n: "
              f"{evaluation['treatment_n']}")
        print(f"  effect: {evaluation['effect']}  "
              f"p (normal approx): {evaluation['p_value']}")
        for row in evaluation["observed"]["rows"]:
            print(f"    {row['criterion']}: observed {row['observed']} → "
                  f"{'met' if row['met'] else 'NOT met'}")
        if evaluation.get("holdout"):
            print(f"  holdout: {evaluation['holdout'].get('note')}")
        for exc in evaluation["details"].get("excluded", []):
            print(f"    excluded {exc['unit_id']}: {exc['reason']}")
        return 0
    if command == "curate":
        knowledge = report["knowledge"]
        state = "already exists" if not report.get("created") else "created"
        print(f"ayce learning curate — {knowledge['knowledge_id']} "
              f"v{knowledge['knowledge_version']} [{state}]")
        print(f"  scope: {knowledge['scope']}  status: "
              f"{knowledge['status']}")
        print(f"  statement: {knowledge['statement']}")
        return 0
    if command == "knowledge":
        items = report["knowledge"]
        print(f"ayce learning knowledge — {len(items)} active record(s)")
        for item in items:
            print(f"  {item['knowledge_id']} v{item['knowledge_version']} "
                  f"[{item['scope']}]: {item['statement']}")
        return 0
    # show
    state = report["state"]
    print("ayce learning show — "
          f"{state['derived_metrics']} derived, "
          f"{state['candidates']} candidates, "
          f"{state['experiments']} experiments, "
          f"{state['evaluations']} evaluations, "
          f"{state['knowledge']} knowledge record(s)")
    for item in report["knowledge"]:
        print(f"  {item['knowledge_id']} v{item['knowledge_version']} "
              f"[{item['scope']}]: {item['statement']}")
    return 0


def _learning_dispatch(args, command, learning_store, analytics_store,
                       now_fn, _evidence):
    from .learning import (
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
    if command == "derive":
        formulas = ([f.strip() for f in args.formulas.split(",")]
                    if args.formulas else None)
        rows = []
        if args.video:
            rows.extend(derive_metrics(
                analytics_store, now=now_fn(),
                youtube_video_id=args.video, formulas=formulas))
        else:
            rows = derive_metrics(analytics_store, now=now_fn(),
                                  formulas=formulas)
        if not args.dry_run:
            inserted = sum(1 for row in rows
                           if learning_store.insert_derived(row=row))
            return {"ok": True, "derived": len(rows), "new": inserted,
                    "dry_run": False, "rows": rows}
        return {"ok": True, "derived": len(rows), "dry_run": True,
                "rows": rows}
    if command == "candidate":
        return create_candidate(
            learning_store, hypothesis=args.hypothesis, scope=args.scope,
            evidence=_evidence(args.evidence),
            counterexamples=_evidence(args.counterexamples), now=now_fn())
    if command == "experiment":
        candidate = learning_store.get_candidate(args.candidate_id)
        if candidate is None:
            from .errors import LearningError
            raise LearningError("learning_invalid_input",
                                f"unknown candidate: {args.candidate_id}")
        window = None
        if args.evaluation_window:
            start, _, end = args.evaluation_window.partition("..")
            window = {"start": start.strip(), "end": end.strip()}
        holdout = ({"fraction": args.holdout_fraction, "min_n": 1}
                   if args.holdout_fraction is not None else None)
        return create_experiment(
            learning_store, candidate_id=args.candidate_id,
            hypothesis=candidate["hypothesis"],
            metric=args.metric,
            control_variant="control",
            treatment_variant="treatment",
            population={"units": [u.strip() for u in
                                  args.units.split(",") if u.strip()]},
            assignment={"seed": args.assignment_seed},
            success_criterion={
                "min_control_n": args.min_control_n,
                "min_treatment_n": args.min_treatment_n,
                "min_effect": args.min_effect,
                **({"max_p": args.max_p} if args.max_p else {}),
            },
            evaluation_window=window, holdout=holdout, now=now_fn())
    if command == "approve":
        return approve_experiment(learning_store, args.experiment_id,
                                  now=now_fn())
    if command == "start":
        return start_experiment(learning_store, args.experiment_id,
                                now=now_fn())
    if command == "assign":
        return assign_units(
            learning_store, args.experiment_id,
            [u.strip() for u in args.units.split(",") if u.strip()],
            now=now_fn(), dry_run=args.dry_run)
    if command == "evaluate":
        return evaluate_experiment(learning_store, analytics_store,
                                   args.experiment_id, now=now_fn())
    if command == "curate":
        return curate_candidate(learning_store,
                                candidate_id=args.candidate_id,
                                evaluation_id=args.evaluation_id,
                                now=now_fn())
    if command == "knowledge":
        return read_validated_knowledge(learning_store.path,
                                        scope=args.scope)
    # show
    return {"ok": True, "state": learning_store.list_all(),
            "knowledge": read_validated_knowledge(
                learning_store.path)["knowledge"]}


def _cmd_policy(args: argparse.Namespace) -> int:
    # Imported here so `ayce health` never pays the import cost.
    from .policy import PolicyError, PolicyStore

    command = getattr(args, "policy_command", None)
    if command not in ("candidate", "show", "diff", "approve", "promote",
                       "activate", "rollback", "retire", "active",
                       "reject", "consumption", "lineage", "effectiveness",
                       "experiment-candidate", "experiment-definition"):
        print("ayce policy: choose a subcommand: candidate | show | diff "
              "| approve | promote | activate | rollback | retire | "
              "active | reject | consumption | lineage | effectiveness "
              "| experiment-candidate | experiment-definition",
              file=sys.stderr)
        return 2

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"ayce policy: configuration error: {exc}", file=sys.stderr)
        return 2

    policy_store_path = (config.resolved_data_dir / "policy" /
                         "policy.sqlite3")
    learning_db = config.resolved_data_dir / "learning" / "learning.sqlite3"

    if command == "consumption":
        # Stage 10 — OBSERVATION ONLY: read-verified run artifacts; the
        # policy write-store is NEVER opened for this command
        report = _policy_consumption_report(config, args, policy_store_path)
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report.get("ok") else 1
        return _print_policy_consumption(report)

    if command == "lineage":
        # Stage 11 — cross-run lineage projection (derived, read-only):
        # recomputed from canonical consumption artifacts; the policy
        # write-store is NEVER opened for this command
        report = _policy_lineage_report(config, args, policy_store_path)
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report.get("ok") else 1
        return _print_policy_lineage(report)

    if command == "effectiveness":
        # Stage 12 — policy effectiveness EVIDENCE projection (derived,
        # READ-ONLY): the policy write-store is NEVER opened for this
        # command; no mutation exists on this path (§20/§25)
        report = _policy_effectiveness_report(config, args)
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report.get("ok") else 1
        return _print_policy_effectiveness(report)

    if command == "experiment-candidate":
        # Stage 13 — experiment INTAKE boundary. The read directions are
        # derived projections (no store is opened writable); the ONLY
        # mutation is explicit approval, which goes THROUGH the existing
        # Stage 7 learning-store owner.
        report = _policy_experiment_candidate_report(config, args)
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report.get("ok") else 1
        return _print_experiment_candidates(report)

    if command == "experiment-definition":
        # Stage 14 — experiment COMPOSITION boundary. Read directions
        # are derived projections (no store is opened writable); the
        # ONLY mutation is explicit approval, which goes THROUGH the
        # existing Stage 7 create_experiment owner and creates a DRAFT
        # only (never started, never assigned).
        report = _policy_experiment_definition_report(config, args)
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report.get("ok") else 1
        return _print_experiment_definitions(report)

    if command == "active":
        # the Hermes-equivalent READ-ONLY path (SQLite mode=ro); the
        # policy write-store is NEVER opened for this command
        from .policy import read_active_policy
        try:
            report = read_active_policy(policy_store_path, scope=args.scope)
        except PolicyError as exc:
            if getattr(args, "json", False):
                print(json.dumps(exc.to_dict(), indent=2))
            else:
                print(f"ayce policy: {exc.code}: {exc.message}",
                      file=sys.stderr)
            return 1
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0
        return _print_policy(command, report)

    policy_store = PolicyStore(policy_store_path)
    now_fn = (lambda: __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc)
        .isoformat(timespec="milliseconds").replace("+00:00", "Z"))

    try:
        report = _policy_dispatch(args, command, policy_store,
                                  learning_db, now_fn)
    except PolicyError as exc:
        if getattr(args, "json", False):
            print(json.dumps(exc.to_dict(), indent=2))
        else:
            print(f"ayce policy: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    finally:
        policy_store.close()

    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    return _print_policy(command, report)


def _policy_consumption_report(config, args, policy_db_path) -> dict:
    """Stage 10 read-only consumption inspection (deterministic; §13/§14)."""
    import re as _re

    from .policy.observability import read_run_consumptions, \
        verify_consumption_history

    run_id = (args.run_id or "").strip()
    if not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", run_id):
        return {"ok": False,
                "error": {"code": "invalid_run_id",
                          "message": f"invalid run id: {run_id!r}"}}
    runs_root = (config.resolved_data_dir / "runs").resolve()
    run_dir = (runs_root / run_id).resolve()
    if run_dir != runs_root and runs_root not in run_dir.parents:
        return {"ok": False,
                "error": {"code": "path_escape",
                          "message": "resolved run path escaped the runs root"}}
    if not run_dir.is_dir():
        return {"ok": False,
                "error": {"code": "run_not_found",
                          "message": f"no such run: {run_id}"}}
    try:
        report = read_run_consumptions(run_dir, run_id)
    except PolicyError as exc:
        return exc.to_dict()

    consumptions = report.get("consumptions", [])
    if getattr(args, "decision_id", None):
        consumptions = [c for c in consumptions
                        if c.get("decision_id") == args.decision_id]
    if getattr(args, "policy_id", None):
        consumptions = [c for c in consumptions
                        if c.get("policy_id") == args.policy_id]
    if getattr(args, "policy_version", None) is not None:
        consumptions = [c for c in consumptions
                        if c.get("policy_version") == args.policy_version]

    report["count"] = len(consumptions)
    report["consumptions"] = consumptions
    report["verification"] = None
    if getattr(args, "verify", False):
        verifications = []
        for event in consumptions:
            try:
                verifications.append(
                    verify_consumption_history(event, policy_db_path))
            except PolicyError as exc:
                return exc.to_dict()
        report["verification"] = verifications
    return report


def _print_policy_consumption(report: dict) -> int:
    if not report.get("ok"):
        error = report.get("error") or {}
        print(f"ayce policy consumption: {error.get('code')}: "
              f"{error.get('message')}", file=sys.stderr)
        return 1
    consumptions = report.get("consumptions", [])
    print(f"ayce policy consumption — run {report['run_id']}: "
          f"{len(consumptions)} event(s)")
    if report.get("note"):
        print(f"  note: {report['note']}")
    for event in consumptions:
        print(f"  {event['consumption_id']}  decision={event['decision_id']}"
              f"  policy={event.get('policy_id')} "
              f"v{event.get('policy_version')}  scope={event.get('scope')}")
        print(f"    status={event.get('policy_status')}  "
              f"context_hash={event.get('context_hash')}")
        print(f"    content_hash={event.get('policy_content_hash')}  "
              f"at={event.get('created_at')}")
    for verification in report.get("verification") or []:
        print(f"  verify {verification['consumption_id']}: "
              f"identity={verification['identity']} "
              f"policy_state={verification['policy_state']} "
              f"content_match={verification['content_match']} "
              f"(historical={verification['historical']})")
    return 0


def _policy_effectiveness_report(config, args) -> dict:
    """Stage 12 read-only effectiveness evidence (derived projection; §21)."""
    from .policy.effectiveness import (
        effectiveness_for_policy, effectiveness_for_run,
        metric_effectiveness_summary, verify_effectiveness,
    )
    from .policy.errors import PolicyError

    effectiveness_command = getattr(args, "effectiveness_command", None)
    if effectiveness_command not in ("run", "policy", "metric", "verify"):
        return {"ok": False,
                "error": {"code": "policy_effectiveness_invalid",
                          "message": "choose: run | policy | metric | "
                                     "verify"}}
    data_dir = config.resolved_data_dir
    paths = {
        "policy_db_path": data_dir / "policy" / "policy.sqlite3",
        "ledger_path": data_dir / "publishing" / "ledger.sqlite3",
        "analytics_db_path": data_dir / "analytics" / "analytics.sqlite3",
    }
    runs_root = data_dir / "runs"
    try:
        if effectiveness_command == "run":
            return effectiveness_for_run(runs_root, args.run_id, **paths)
        if effectiveness_command == "policy":
            return effectiveness_for_policy(
                runs_root, args.policy_id,
                policy_version=args.policy_version, metric=args.metric,
                video_id=args.video_id, **paths)
        if effectiveness_command == "metric":
            return metric_effectiveness_summary(
                runs_root, args.metric, policy_id=args.policy_id,
                policy_version=args.policy_version, source=args.source,
                window_start=args.window_start, window_end=args.window_end,
                **paths)
        return verify_effectiveness(runs_root, **paths)
    except PolicyError as exc:
        return exc.to_dict()


def _print_policy_effectiveness(report: dict) -> int:
    if not report.get("ok"):
        error = report.get("error") or {}
        print(f"ayce policy effectiveness: {error.get('code')}: "
              f"{error.get('message')}", file=sys.stderr)
        return 1
    if "runs_scanned" in report:  # verify
        print(f"ayce policy effectiveness verify — "
              f"{report['runs_scanned']} run(s) scanned, "
              f"{report['records_built']} record(s) built")
        if report["runs_corrupt"]:
            print(f"  corrupt runs: {report['runs_corrupt']}")
        for invalid in report["invalid_records"]:
            print(f"  INVALID {invalid['run_id']} "
                  f"{invalid['consumption_id']}: "
                  f"{', '.join(invalid['reasons'])}")
        print(f"  consistent: {report['consistent']}")
        print("  (evidence only — no ranking, no causal claim)")
        return 0
    if report.get("comparison") == "descriptive_comparison":  # metric
        print(f"ayce policy effectiveness metric — {report['metric']} "
              f"[{report['comparison']}]")
        for policy in report["policies"]:
            print(f"  {policy['policy_id']} v{policy['policy_version']}: "
                  f"consumed={policy['consumed_runs']} "
                  f"published={policy['published_runs']} "
                  f"unpublished={policy['unpublished_runs']} "
                  f"analytics_available="
                  f"{policy['analytics_available_runs']} "
                  f"present={policy['metric_present_count']} "
                  f"missing={policy['metric_missing_count']} "
                  f"[{policy['evidence_status']}]")
            for group in policy["outcome_groups"]:
                window = (f"{group['window_start']}..{group['window_end']}"
                          if group["window_start"] else "lifetime")
                print(f"    {group['metric']}@{group['source']} "
                      f"[{window}] n={group['n']} mean={group['mean']} "
                      f"median={group['median']} min={group['min']} "
                      f"max={group['max']}"
                      + (f" excluded={group['exclusion_counts']}"
                         if group["exclusion_counts"] else ""))
        print("  descriptive comparison ONLY — no ranking, no effect, "
              "no causal claim")
        return 0
    records = report.get("records", [])
    if "policy_id" in report:  # policy direction
        agg = report["aggregation"]
        print(f"ayce policy effectiveness policy — "
              f"{report['policy_id']} v{report['policy_version']}")
        print(f"  consumed_runs={agg['consumed_runs']} "
              f"published={agg['published_runs']} "
              f"unpublished={agg['unpublished_runs']} "
              f"analytics_available={agg['analytics_available_runs']} "
              f"metric_present={agg['metric_present_count']} "
              f"metric_missing={agg['metric_missing_count']} "
              f"[{agg['evidence_status']}]")
    else:  # run direction
        print(f"ayce policy effectiveness run — {report['run_id']} "
              f"[evidence: {report['evidence_status']}]")
    for record in records:
        publish = record.get("publish") or {}
        print(f"  {record.get('effectiveness_id') or '(invalid)'}  "
              f"policy={record.get('policy_id')} "
              f"v{record.get('policy_version')}  "
              f"state={record.get('policy_state')}")
        print(f"    publish_status={record['publish_status']}  "
              f"video={publish.get('youtube_video_id')}  "
              f"destination={publish.get('destination')}")
        print(f"    analytics_status={record['analytics_status']}  "
              f"missing_data={record['missing_data_status']}  "
              f"evidence={record['evidence_status']}"
              + (f" reasons={record['invalid_reasons']}"
                 if record["invalid_reasons"] else ""))
        for m in record["measurements"]:
            window = (f"{m['window_start']}..{m['window_end']}"
                      if m["window_start"] else "lifetime cumulative")
            value = "—" if m["value"] is None else m["value"]
            print(f"    {m['metric']}@{m['source']} [{window}] = {value} "
                  f"[{m['availability']}] obs={m['observation_id']}")
    print("  (runs consuming this policy had these OBSERVED outcomes — "
          "correlation, not causality)")
    return 0


def _policy_experiment_candidate_report(config, args) -> dict:
    """Stage 13 experiment INTAKE candidates (derived from Stage 12
    evidence; read directions are pure projections — only `approve`
    mutates, through the existing Stage 7 learning-store owner)."""
    from .policy.errors import PolicyError
    from .policy.experiment_intake import (
        approve_candidate, candidates_for_metric, candidates_for_policy,
        show_candidate, verify_intake,
    )

    command = getattr(args, "experiment_candidate_command", None)
    if command not in ("policy", "metric", "show", "verify", "approve"):
        return {"ok": False,
                "error": {"code": "policy_intake_invalid",
                          "message": "choose: policy | metric | show | "
                                     "verify | approve"}}
    data_dir = config.resolved_data_dir
    paths = {
        "policy_db_path": data_dir / "policy" / "policy.sqlite3",
        "ledger_path": data_dir / "publishing" / "ledger.sqlite3",
        "analytics_db_path": data_dir / "analytics" / "analytics.sqlite3",
    }
    runs_root = data_dir / "runs"
    try:
        if command == "policy":
            return candidates_for_policy(
                runs_root, args.policy_id,
                policy_version=args.policy_version, metric=args.metric,
                **paths)
        if command == "metric":
            return candidates_for_metric(
                runs_root, args.metric, policy_id=args.policy_id,
                policy_version=args.policy_version, source=args.source,
                window_start=args.window_start,
                window_end=args.window_end, **paths)
        if command == "show":
            return show_candidate(runs_root, args.candidate_id, **paths)
        if command == "verify":
            return verify_intake(runs_root, **paths)
        # approve — the explicit operator boundary (the ONLY mutation;
        # isolated to the existing Stage 7 learning store)
        from .learning import LearningStore
        learning_store = LearningStore(
            data_dir / "learning" / "learning.sqlite3")
        now_fn = (lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
        try:
            return approve_candidate(
                runs_root, args.candidate_id, learning_store,
                approved_by=args.approved_by, now=now_fn(), **paths)
        finally:
            learning_store.close()
    except PolicyError as exc:
        return exc.to_dict()
    except Exception as exc:  # LearningError and friends stay structured
        from .learning.errors import LearningError
        if isinstance(exc, LearningError):
            return exc.to_dict()
        raise


def _print_experiment_candidates(report: dict) -> int:
    if not report.get("ok"):
        error = report.get("error") or {}
        print(f"ayce policy experiment-candidate: {error.get('code')}: "
              f"{error.get('message')}", file=sys.stderr)
        return 1
    if "policies_scanned" in report:  # verify
        print(f"ayce policy experiment-candidate verify — "
              f"{report['policies_scanned']} policie(s) with evidence, "
              f"{report['candidates_total']} candidate(s): "
              f"{report['eligible']} eligible, "
              f"{report['insufficient_evidence']} insufficient, "
              f"{report['ineligible']} ineligible")
        for record in report["ineligible_records"]:
            print(f"  INELIGIBLE {record['run_id']} "
                  f"{record['consumption_id']}: "
                  f"{', '.join(record['reasons'])}")
        print(f"  deterministic: {report['deterministic']}  "
              f"consistent: {report['consistent']}")
        print("  (intake evidence only — no experiment starts here)")
        return 0
    if "candidate" in report:  # show / approve
        prefix = ("approved" if report.get("status") == "approved"
                  else "show")
        if report.get("status") == "approved":
            print(f"ayce policy experiment-candidate approve — "
                  f"{report['candidate_id']} → Stage 7 candidate "
                  f"{report['stage7_candidate_id']} "
                  f"(duplicate={report['duplicate']}; "
                  f"{report['evidence_entries']} evidence reference(s))")
            print(f"  boundary: {report['boundary']}")
            return 0
        candidate = report["candidate"]
        _print_one_intake_candidate(candidate)
        return 0
    # policy / metric directions
    label = (f"policy {report['policy_id']}"
             if "policy_id" in report else f"metric {report['metric']}")
    agg = report["aggregation"]
    print(f"ayce policy experiment-candidate — {label}: "
          f"{report['candidate_count']} candidate(s) "
          f"[eligible={agg['eligible']} "
          f"insufficient={agg['insufficient_evidence']} "
          f"ineligible={agg['ineligible']}]")
    for candidate in report["candidates"]:
        _print_one_intake_candidate(candidate)
    for record in report.get("ineligible_records", []):
        print(f"  INELIGIBLE {record['run_id']} "
              f"{record['consumption_id']}: "
              f"{', '.join(record['reasons'])}")
    print("  (a candidate means sufficient observational evidence to "
          "CONSIDER an experiment — no causal claim, no ranking, no "
          "started experiment)")
    return 0


def _print_one_intake_candidate(candidate: dict) -> None:
    metric = candidate["metric"]
    window = (f"{metric['window_start']}..{metric['window_end']}"
              if metric["window_start"] else "lifetime")
    summary = candidate["observed_summary"]
    print(f"  {candidate['candidate_id']}  "
          f"{candidate['policy_id']} v{candidate['policy_version']}  "
          f"scope={candidate['scope']}  [{candidate['status']}]")
    print(f"    {metric['name']}@{metric['source']} [{window}]  "
          f"n={summary['n']} mean={summary['mean']} "
          f"median={summary['median']}")
    print(f"    runs={candidate['run_ids']}  "
          f"not_contributing="
          f"{[(c['run_id'], c['reason']) for c in candidate['runs_not_contributing']]}")


def _policy_experiment_definition_report(config, args) -> dict:
    """Stage 14 experiment COMPOSITION (derived proposals; the read
    directions are pure projections — only `approve` mutates, through
    the existing Stage 7 create_experiment owner)."""
    from .policy.errors import PolicyError
    from .policy.experiment_definition import (
        approve_proposal, propose_for_candidate, show_proposal,
        verify_composition,
    )

    command = getattr(args, "experiment_definition_command", None)
    if command not in ("run", "show", "verify", "approve"):
        return {"ok": False,
                "error": {"code": "policy_composition_invalid",
                          "message": "choose: run | show | verify | "
                                     "approve"}}
    data_dir = config.resolved_data_dir
    paths = {
        "policy_db_path": data_dir / "policy" / "policy.sqlite3",
        "ledger_path": data_dir / "publishing" / "ledger.sqlite3",
        "analytics_db_path": data_dir / "analytics" / "analytics.sqlite3",
    }
    runs_root = data_dir / "runs"
    overrides = {
        "control_variant": getattr(args, "control_variant", None),
        "treatment_variant": getattr(args, "treatment_variant", None),
        "min_effect": getattr(args, "min_effect", None),
    }
    try:
        if command == "run":
            return propose_for_candidate(runs_root, args.candidate_id,
                                         **paths, **overrides)
        if command == "show":
            return show_proposal(runs_root, args.proposal_id, **paths,
                                 **overrides)
        if command == "verify":
            return verify_composition(runs_root, **paths)
        # approve — the explicit operator boundary (the ONLY mutation;
        # isolated to the existing Stage 7 learning store via its owner)
        from .learning import LearningStore
        learning_store = LearningStore(
            data_dir / "learning" / "learning.sqlite3")
        now_fn = (lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
        try:
            return approve_proposal(
                runs_root, args.proposal_id, learning_store,
                approved_by=args.approved_by, seed=args.seed,
                now=now_fn(), dry_run=args.dry_run, **paths, **overrides)
        finally:
            learning_store.close()
    except PolicyError as exc:
        return exc.to_dict()
    except Exception as exc:  # LearningError and friends stay structured
        return {"ok": False,
                "error": {"code": "policy_composition_invalid",
                          "message": str(exc)}}


def _print_one_experiment_definition(proposal: dict) -> None:
    metric = proposal["metric"]
    window = (f"{metric['window_start']}..{metric['window_end']}"
              if metric["window_start"] else "lifetime")
    comparison = proposal["comparison"]
    population = proposal["population"]
    print(f"  {proposal['proposal_id']}  "
          f"{proposal['policy_id']} v{proposal['policy_version']}  "
          f"[{proposal['status']}]")
    print(f"    candidate={proposal['candidate_id']}  "
          f"scope={proposal['scope']}")
    print(f"    {metric['name']}@{metric['source']} [{window}]  "
          f"declared_in_stage7={proposal['metric_declared_in_stage7']}")
    print(f"    comparison: control={comparison.get('control_variant')}  "
          f"treatment={comparison.get('treatment_variant')}  "
          f"direction={comparison.get('direction')}  "
          f"source={comparison.get('source')}")
    print(f"    population: {population['unit_count']} unit(s) "
          f"{population['units']}  resolved={population['resolved']}")
    if proposal["unresolved_fields"]:
        print(f"    UNRESOLVED (never invented): "
              f"{proposal['unresolved_fields']}")
    print(f"    seed: {proposal['assignment']['note']}")


def _print_experiment_definitions(report: dict) -> int:
    if not report.get("ok"):
        error = report.get("error") or {}
        print(f"ayce policy experiment-definition: {error.get('code')}: "
              f"{error.get('message')}", file=sys.stderr)
        return 1
    if "deterministic" in report and "proposals_total" in report:  # verify
        print(f"ayce policy experiment-definition verify — "
              f"{report['policies_scanned']} policy(ies), "
              f"{report['proposals_total']} proposal(s) "
              f"(ready={report['ready']}, "
              f"needs_configuration="
              f"{report['needs_operator_configuration']})")
        print(f"  deterministic={report['deterministic']}  "
              f"candidates_consistent={report['candidates_consistent']}  "
              f"evidence_consistent={report['evidence_consistent']}  "
              f"consistent={report['consistent']}")
        print("  (proposals are definitions to REVIEW — no experiment "
              "runs automatically)")
        return 0
    if report.get("experiment_id") is not None:  # approve result
        print(f"ayce policy experiment-definition approve — "
              f"{report['experiment_id']} [{report['experiment_status']}]"
              + (" [DRY RUN — nothing was created]"
                 if report.get("dry_run") else ""))
        print(f"  proposal: {report['proposal_id']}  "
              f"stage7_candidate: {report['stage7_candidate_id']}")
        print(f"  started={report['started']}  "
              f"assignments={report['assignments']}  "
              f"duplicate={report.get('duplicate')}")
        print(f"  boundary: {report['boundary']}")
        return 0
    proposal = report["proposal"]  # run / show
    print(f"ayce policy experiment-definition {proposal['status']} — "
          f"{proposal['proposal_id']}")
    _print_one_experiment_definition(proposal)
    print("  (a proposal is a definition to REVIEW — approval is a "
          "separate explicit command)")
    return 0


def _policy_lineage_report(config, args, policy_db_path) -> dict:
    """Stage 11 read-only cross-run lineage (derived projection; §15)."""
    from .policy import (
        lineage_for_policy, lineage_for_run, rebuild_lineage_index,
        verify_lineage_index,
    )
    from .policy.errors import PolicyError
    lineage_command = getattr(args, "lineage_command", None)
    if lineage_command not in ("run", "policy", "verify", "rebuild"):
        return {"ok": False,
                "error": {"code": "policy_lineage_invalid",
                          "message": "choose: run | policy | verify | "
                                     "rebuild"}}
    runs_root = config.resolved_data_dir / "runs"
    try:
        if lineage_command == "run":
            return lineage_for_run(runs_root, args.run_id,
                                   policy_db_path=policy_db_path,
                                   scope=getattr(args, "scope", None))
        if lineage_command == "policy":
            return lineage_for_policy(runs_root, args.policy_id,
                                      policy_version=args.policy_version,
                                      policy_db_path=policy_db_path,
                                      scope=getattr(args, "scope", None))
        if lineage_command == "verify":
            return verify_lineage_index(runs_root,
                                        policy_db_path=policy_db_path)
        return rebuild_lineage_index(runs_root,
                                     policy_db_path=policy_db_path)
    except PolicyError as exc:
        return exc.to_dict()


def _print_policy_lineage(report: dict) -> int:
    if not report.get("ok"):
        error = report.get("error") or {}
        print(f"ayce policy lineage: {error.get('code')}: "
              f"{error.get('message')}", file=sys.stderr)
        return 1
    if "evidence_status" in report:  # run direction
        print(f"ayce policy lineage run — {report['run_id']} "
              f"[evidence: {report['evidence_status']}]")
        for event in report.get("policies", []):
            print(f"  {event['consumption_id']}  policy="
                  f"{event.get('policy_id')} v{event.get('policy_version')}"
                  f"  scope={event.get('scope')}  "
                  f"status={event.get('policy_status')}")
            state = event.get("policy_state")
            if state is not None:
                print(f"    identity={event.get('identity')}  "
                      f"policy_state={state}  "
                      f"content_match={event.get('content_match')}")
        return 0
    if "runs" in report:  # policy direction
        version = report.get("policy_version")
        print(f"ayce policy lineage policy — {report['policy_id']}"
              + (f" v{version}" if version is not None else "")
              + f": {report['run_count']} run(s)")
        for entry in report["runs"]:
            state = entry.get("policy_state")
            print(f"  {entry['run_id']}  {entry['consumption_id']}  "
                  f"v{entry.get('policy_version')}  "
                  f"scope={entry.get('scope')}  at={entry.get('created_at')}"
                  + (f"  policy_state={state}" if state is not None else ""))
        return 0
    print(f"ayce policy lineage {report.get('derived') and 'rebuild' or 'verify'}"
          f" — scanned {report.get('runs_scanned')} run(s): "
          f"{report.get('runs_verified')} verified, "
          f"{report.get('runs_missing_evidence')} missing evidence, "
          f"{report.get('runs_corrupt')} corrupt; "
          f"policies indexed: {report.get('policies_indexed', 'n/a')}")
    print(f"  consistent: {report.get('consistent')}")
    return 0


def _policy_dispatch(args, command, policy_store, learning_db, now_fn):
    from .policy import (
        activate_policy,
        approve_candidate,
        create_candidate,
        diff_for_activation,
        promote_candidate,
        reject_candidate,
        retire_policy,
        rollback_policy,
    )
    if command == "candidate":
        return create_candidate(
            policy_store, learning_db,
            knowledge_refs=args.knowledge, scope=args.scope,
            rationale=args.rationale, action=args.action,
            priority=args.priority, now=now_fn(), dry_run=args.dry_run)
    if command == "approve":
        return approve_candidate(policy_store,
                                 candidate_id=args.candidate_id,
                                 approved_by=args.approved_by, now=now_fn())
    if command == "reject":
        return reject_candidate(policy_store,
                                candidate_id=args.candidate_id,
                                reason=args.reason, now=now_fn())
    if command == "promote":
        return promote_candidate(policy_store, learning_db,
                                 candidate_id=args.candidate_id,
                                 now=now_fn(), dry_run=args.dry_run)
    if command == "activate":
        return activate_policy(policy_store, policy_id=args.policy_id,
                               policy_version=args.policy_version,
                               now=now_fn())
    if command == "rollback":
        return rollback_policy(policy_store, policy_id=args.policy_id,
                               policy_version=args.policy_version,
                               now=now_fn())
    if command == "retire":
        return retire_policy(policy_store, policy_id=args.policy_id,
                             policy_version=args.policy_version,
                             now=now_fn())
    if command == "diff":
        if not args.candidate_id and not args.policy_id:
            from .errors import PolicyError as _PE
            raise _PE("policy_invalid_candidate",
                      "diff needs --candidate-id or --policy-id")
        return diff_for_activation(
            policy_store, candidate_id=args.candidate_id,
            policy_id=args.policy_id, policy_version=args.policy_version)
    # show
    if args.candidate_id:
        candidate = policy_store.get_candidate(args.candidate_id)
        if candidate is None:
            from .errors import PolicyError as _PE
            raise _PE("policy_invalid_candidate",
                      f"unknown candidate: {args.candidate_id}")
        return {"ok": True, "candidate": candidate,
                "approval": policy_store.get_approval(args.candidate_id)}
    if args.policy_id:
        policy = policy_store.get_policy(args.policy_id, args.policy_version)
        if policy is None:
            from .errors import PolicyError as _PE
            raise _PE("policy_activation_conflict",
                      f"unknown policy: {args.policy_id}")
        return {"ok": True, "policy": policy}
    return {"ok": True, "state": policy_store.summary(),
            "candidates": policy_store.list_candidates(),
            "policies": policy_store.list_policies()}


def _print_policy(command: str, report: dict) -> int:
    if command == "candidate":
        candidate = report["candidate"]
        state = ("DRY-RUN (nothing stored)" if report.get("dry_run") else
                 "already compiled" if report.get("duplicate") else
                 "compiled")
        print(f"ayce policy candidate — {candidate['candidate_id']} [{state}]")
        print(f"  scope: {candidate['scope']}  status: "
              f"{candidate['status']}")
        print(f"  knowledge: {len(candidate['source_knowledge_refs'])} "
              f"ref(s)  rules: {len(candidate['proposed_rules'])}")
        for rule in candidate["proposed_rules"]:
            print(f"    {rule['rule_id']}: prefer {rule['variant']} over "
                  f"{rule['comparator_variant']} on {rule['metric']} "
                  f"[{rule['scope']}] (priority {rule['priority']})")
        return 0
    if command == "approve":
        candidate = report["candidate"]
        state = "already approved" if report.get("already_approved") \
            else "approved"
        print(f"ayce policy approve — {candidate['candidate_id']} [{state}]")
        approval = report.get("approval") or {}
        print(f"  approved_by: {approval.get('approved_by')}  at: "
              f"{approval.get('approved_at')}")
        return 0
    if command == "reject":
        candidate = report["candidate"]
        print(f"ayce policy reject — {candidate['candidate_id']} "
              f"[{candidate['status']}]")
        print(f"  reason: {report['reason']}")
        return 0
    if command == "promote":
        policy = report["policy"]
        state = ("DRY-RUN (nothing stored)" if report.get("dry_run") else
                 "already promoted" if not report.get("created") else
                 "created")
        print(f"ayce policy promote — {policy['policy_id']} "
              f"v{policy['policy_version']} [{state}]")
        print(f"  scope: {policy['scope']}  status: {policy['status']}")
        print(f"  content_hash: {policy['content_hash']}")
        print(f"  previous: {policy['previous_policy_id']} "
              f"v{policy['previous_policy_version']}")
        for rule in policy["rules"]:
            print(f"    {rule['rule_id']}: prefer {rule['variant']} over "
                  f"{rule['comparator_variant']} on {rule['metric']} "
                  f"[{rule['scope']}] (priority {rule['priority']})")
        return 0
    if command in ("activate", "rollback", "retire"):
        policy = report["policy"]
        label = {"activate": "activated" if report.get("activated")
                 else "already active",
                 "rollback": "rolled back", "retire": "retired"}[command]
        print(f"ayce policy {command} — {policy['policy_id']} "
              f"v{policy['policy_version']} → {policy['status']} [{label}]")
        if report.get("activation_id"):
            print(f"  audit: {report['activation_id']}")
        return 0
    if command == "diff":
        baseline = report["baseline"]
        print(f"ayce policy diff — scope {report['scope']}")
        print(f"  target: {report['target']['kind']} "
              f"({report['target'].get('candidate_id')
                or report['target'].get('policy_id')})")
        print(f"  baseline: {baseline if baseline else '—'}")
        print(f"  {report['baseline_note']}")
        for rule in report["rules"]["added"]:
            print(f"  + rule {rule['rule_id']} ({rule['metric']}: "
                  f"prefer {rule['variant']})")
        for rule in report["rules"]["removed"]:
            print(f"  - rule {rule['rule_id']} ({rule['metric']}: "
                  f"prefer {rule['variant']})")
        for change in report["rules"]["changed"]:
            for field, delta in change["fields"].items():
                print(f"  ~ rule {change['rule_id']}.{field}: "
                      f"{delta['from']} → {delta['to']}")
        return 0
    if command == "active":
        policy = report["policy"]
        if policy is None:
            print(f"ayce policy active — scope {report['scope']}: "
                  f"no active policy ({report.get('note')})")
            return 0
        print(f"ayce policy active — {policy['policy_id']} "
              f"v{policy['policy_version']} [{policy['status']}]")
        print(f"  scope: {policy['scope']}  "
              f"content_hash: {policy['content_hash']}")
        for rule in policy["rules"]:
            print(f"    {rule['rule_id']}: prefer {rule['variant']} over "
                  f"{rule['comparator_variant']} on {rule['metric']} "
                  f"(priority {rule['priority']})")
        return 0
    # show
    state = report["state"]
    print(f"ayce policy show — {state['candidates']} candidate(s), "
          f"{state['policies']} policy version(s), "
          f"{state['activations']} activation record(s)")
    for candidate in report["candidates"]:
        print(f"  candidate {candidate['candidate_id']} "
              f"[{candidate['status']}] scope {candidate['scope']}")
    for policy in report["policies"]:
        print(f"  policy {policy['policy_id']} "
              f"v{policy['policy_version']} [{policy['status']}] "
              f"scope {policy['scope']}")
    return 0




def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command or "health"

    if command == "health":
        return _cmd_health(args)
    if command == "run":
        return _cmd_run(args)
    if command == "package":
        return _cmd_package(args)
    if command == "publish":
        return _cmd_publish(args)
    if command == "publish-auth":
        return _cmd_publish_auth(args)
    if command == "analytics":
        return _cmd_analytics(args)
    if command == "learning":
        return _cmd_learning(args)
    if command == "policy":
        return _cmd_policy(args)
    parser.error(f"unknown command: {command}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
