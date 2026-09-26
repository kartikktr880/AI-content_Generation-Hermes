"""Stage 7 — Derived metrics: provenance-carrying derivations over
Stage 6 observations (§7/§8).

The FIRST derivation layer above the immutable analytics store. Every
derived metric is a deterministic function of ALREADY-PERSISTED
normalized measurements, carries full provenance (formula + version,
source observation ids, lineage references), and has a deterministic
identity so re-derivation is idempotent (§32).

Semantics (§7) — the reason this layer exists:

- 0 in the NUMERATOR is a real value → the metric is ``present`` with
  value 0.
- 0 in the DENOMINATOR makes the ratio UNDEFINED (never 0, never
  silently dropped): ``availability = 'undefined'``.
- A missing operand (not returned / not applicable) makes the derived
  metric ``missing`` — distinct from undefined and from zero.
- Formulas never mix populations: each operand declares its source, and
  both operands of a ratio come from the SAME observation context
  (same video, same observed_at, same window) — Stage 6 §18 carries
  over (video-level stays video-level).
"""

from __future__ import annotations

import hashlib

__all__ = [
    "FORMULA_VERSION",
    "FORMULA_CATALOG",
    "derived_metric_id",
    "derive_metrics",
]

#: Single formula version of this layer (v1).
FORMULA_VERSION = "v1"

#: Formula catalog. Each entry declares its operands as
#: ``(source, metric_name)`` pairs so provenance and population scope
#: are explicit. ``kind`` selects the evaluation rule:
#:   ratio:           present operands required; zero denom → undefined
#:   ratio_scaled:    ratio * scale (per-1000 semantics)
#:   net_ratio_scaled:(numerator - subtrahend) / denominator * scale
#:   per_day:         numerator / window length in days (a windowless
#:                    lifetime snapshot is undefined, never divided)
FORMULA_CATALOG = {
    "likes_per_view": {
        "kind": "ratio",
        "numerator": ("DATA_API_V3", "likes"),
        "denominator": ("DATA_API_V3", "views"),
        "description": "likes / views (lifetime-cumulative snapshot)",
    },
    "comments_per_view": {
        "kind": "ratio",
        "numerator": ("DATA_API_V3", "comments"),
        "denominator": ("DATA_API_V3", "views"),
        "description": "comments / views (lifetime-cumulative snapshot)",
    },
    "subscribers_gained_per_1000_views": {
        "kind": "ratio_scaled",
        "scale": 1000.0,
        "numerator": ("ANALYTICS_API_V2", "subscribersGained"),
        "denominator": ("ANALYTICS_API_V2", "views"),
        "description": "subscribersGained / views * 1000 (same window)",
    },
    "subscribers_net_per_1000_views": {
        "kind": "net_ratio_scaled",
        "scale": 1000.0,
        "numerator": ("ANALYTICS_API_V2", "subscribersGained"),
        "subtrahend": ("ANALYTICS_API_V2", "subscribersLost"),
        "denominator": ("ANALYTICS_API_V2", "views"),
        "description": "(subscribersGained - subscribersLost) / views * 1000",
    },
    "views_per_day": {
        "kind": "per_day",
        "numerator": ("ANALYTICS_API_V2", "views"),
        "denominator": ("ANALYTICS_API_V2", "__window_days__"),
        "description": "windowed views / window length in days",
    },
}


def _canonical(payload) -> str:
    import json
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def derived_metric_id(*, formula: str, formula_version: str,
                      youtube_video_id: str, observed_at: str,
                      window_start, window_end) -> str:
    """Deterministic derived-metric identity (§32): formula + version +
    the exact observation context. Re-derivation of the same inputs can
    never produce a second record."""
    identity = {
        "formula": formula,
        "formula_version": formula_version,
        "youtube_video_id": youtube_video_id,
        "observed_at": observed_at,
        "window_start": window_start,
        "window_end": window_end,
    }
    return "dm-" + hashlib.sha256(
        _canonical(identity).encode("utf-8")).hexdigest()[:24]
def _window_days(window_start, window_end):
    """Window length in days from YYYY-MM-DD dates (inclusive), or None
    when the observation has no window (lifetime snapshot)."""
    if not window_start or not window_end:
        return None
    from datetime import date
    try:
        start = date.fromisoformat(window_start)
        end = date.fromisoformat(window_end)
    except ValueError:
        return None
    if end < start:
        return None
    return (end - start).days + 1


def _evaluate_formula(formula: str, spec: dict, measurements: dict) -> dict:
    """Evaluate one formula against the measurement map of ONE
    observation context. Returns ``{value, availability, note}`` —
    never invents a number (§7/§39)."""
    def operand(ref):
        if ref[1] == "__window_days__":
            days = _window_days(measurements.get("__window_start__"),
                                measurements.get("__window_end__"))
            if days is None:
                return None, "missing", "observation has no evaluation window"
            return float(days), "present", None
        m = measurements.get(ref)
        if m is None:
            return None, "missing", None
        if m.get("availability") == "zero":
            return 0.0, "zero", None
        if m.get("availability") != "present" or m.get("value") is None:
            return None, "missing", None
        return float(m["value"]), "present", None

    num_value, num_avail, num_note = operand(spec["numerator"])

    if spec["kind"] == "per_day":
        # the WINDOW (denominator) is checked first: a windowless
        # lifetime snapshot is UNDEFINED, never "missing" — the
        # documented per-day semantics (§7)
        days, _d_avail, d_note = operand(spec["denominator"])
        if days is None:
            return {"value": None, "availability": "undefined",
                    "note": d_note or "denominator undefined"}
        if num_avail == "missing":
            return {"value": None, "availability": "missing",
                    "note": num_note or "numerator missing"}
        return {"value": num_value / days, "availability": "present",
                "note": None}

    if spec["kind"] == "net_ratio_scaled":
        sub_value, sub_avail, sub_note = operand(spec["subtrahend"])
        if sub_avail == "missing":
            return {"value": None, "availability": "missing",
                    "note": sub_note or "subtrahend missing"}
        num_value = num_value - sub_value

    den_value, den_avail, den_note = operand(spec["denominator"])
    if num_avail == "missing":
        return {"value": None, "availability": "missing",
                "note": num_note or "numerator missing"}
    if den_avail == "missing":
        return {"value": None, "availability": "missing",
                "note": den_note or "denominator missing"}
    if den_value == 0:
        # zero denominator is UNDEFINED, never 0 and never missing (§7)
        return {"value": None, "availability": "undefined",
                "note": "zero denominator"}
    value = num_value / den_value
    if spec["kind"] == "ratio_scaled":
        value = value * spec["scale"]
    return {"value": value, "availability": "present", "note": None}

def derive_metrics(analytics_store, *, now: str,
                   youtube_video_id: str | None = None,
                   formulas=None) -> list[dict]:
    """Derive metrics from ALREADY-PERSISTED Stage 6 normalized
    measurements. Reads the analytics store (never writes it — §6);
    returns rows with deterministic identities and full provenance
    (formula + version, source observation ids, lineage). ``now`` is
    the caller-controlled creation timestamp (deterministic in tests).
    The CALLER persists them via LearningStore.insert_derived
    (idempotent — §32); dry-run simply does not persist.
    """
    from .errors import LearningError

    if formulas is None:
        formulas = sorted(FORMULA_CATALOG)
    for formula in formulas:
        if formula not in FORMULA_CATALOG:
            raise LearningError(
                "learning_metric_unavailable",
                f"unknown derived-metric formula: {formula}",
                details={"supported": sorted(FORMULA_CATALOG)})

    # normalized measurements grouped by observation context
    # (video, observed_at, source) — one group per observation
    if youtube_video_id is None:
        video_ids = sorted({o["youtube_video_id"] for o in
                            analytics_store.list_observations()})
    else:
        video_ids = [youtube_video_id]
    measurements: list[dict] = []
    for video_id in video_ids:
        measurements.extend(
            analytics_store.measurements_for_video(video_id))
    contexts: dict[tuple, dict] = {}
    for metric in measurements:
        key = (metric["youtube_video_id"], metric["observed_at"],
               metric["source"], metric.get("window_start"),
               metric.get("window_end"))
        context = contexts.setdefault(key, {})
        context[(metric["source"], metric["metric_name"])] = metric
        context.setdefault("__observation_ids__", set())
        context["__observation_ids__"].add(metric["observation_id"])

    rows = []
    for (video_id, observed_at, _source, window_start, window_end), \
            context in sorted(contexts.items()):
        context = dict(context)
        context["__window_start__"] = window_start
        context["__window_end__"] = window_end
        lineage = {}
        for (source, name), metric in context.items():
            if isinstance(metric, dict) and metric.get("lineage"):
                lineage = metric["lineage"]
                break
        for formula in formulas:
            spec = FORMULA_CATALOG[formula]
            result = _evaluate_formula(formula, spec, context)
            rows.append({
                "derived_metric_id": derived_metric_id(
                    formula=formula, formula_version=FORMULA_VERSION,
                    youtube_video_id=video_id,
                    observed_at=observed_at,
                    window_start=window_start, window_end=window_end),
                "formula": formula,
                "formula_version": FORMULA_VERSION,
                "youtube_video_id": video_id,
                "observed_at": observed_at,
                "window_start": window_start,
                "window_end": window_end,
                "value": result["value"],
                "availability": result["availability"],
                "note": result["note"],
                "source_observation_ids": sorted(
                    context["__observation_ids__"]),
                "lineage": lineage,
                "created_at": now,
            })
    rows.sort(key=lambda r: (r["youtube_video_id"], r["observed_at"],
                             r["formula"]))
    return rows

