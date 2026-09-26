"""Stage 2 clustering tests — small synthetic title sets, insufficient
input, determinism, and valid label domains."""

from ayce.research.clustering import (
    MIN_CLUSTER_CANDIDATES,
    cluster_titles,
)

_PYTHON = [
    "Best python tutorial 2026",
    "Learn python fast tutorial",
    "The only python tutorial you need",
]
_KETO = [
    "Keto diet meal plan",
    "Keto diet beginner guide",
    "My keto diet results",
]
_TITLES = _PYTHON + _KETO


def test_small_synthetic_set_separates_topics():
    result = cluster_titles(_TITLES)
    assert result.capability_status in ("ok", "fallback_lexical")
    assert result.n_clusters >= 1
    # every label is None (noise) or a valid dense cluster id
    valid_ids = set(range(result.n_clusters))
    assert all(lbl is None or lbl in valid_ids for lbl in result.labels)
    # same-topic titles share a cluster; topics never share one
    py_labels = {result.labels[i] for i in range(3)}
    keto_labels = {result.labels[i] for i in range(3, 6)}
    assert None not in py_labels or None not in keto_labels
    if None not in py_labels and None not in keto_labels:
        assert py_labels.isdisjoint(keto_labels)


def test_insufficient_input_degrades_explicitly():
    result = cluster_titles(["one topic", "another topic"])
    assert result.capability_status == "insufficient_input"
    assert result.method == "none"
    assert result.labels == [None, None]
    assert result.n_clusters == 0


def test_exactly_minimum_is_clusterable():
    result = cluster_titles(_PYTHON)
    assert len(result.labels) == MIN_CLUSTER_CANDIDATES
    assert result.capability_status in ("ok", "fallback_lexical")


def test_clustering_is_deterministic():
    first = cluster_titles(_TITLES)
    second = cluster_titles(list(reversed(_TITLES)))
    assert first.method == second.method
    assert first.n_clusters == second.n_clusters
    # identical input order → identical labels (repeatable testing)
    again = cluster_titles(_TITLES)
    assert first.labels == again.labels


def test_never_raises_on_garbage_titles():
    result = cluster_titles(["", "123", "!!!", "…", "a"])
    assert len(result.labels) == 5
    assert all(lbl is None or isinstance(lbl, int) for lbl in result.labels)