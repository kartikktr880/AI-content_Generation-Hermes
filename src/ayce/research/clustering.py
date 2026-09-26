"""Stage 2 — Local semantic clustering of candidate titles (deterministic).

Zero required dependencies (the project stays pydantic-only):

- Embedding path: FastEmbed (local ONNX) is used ONLY when the optional
  package is already importable AND model initialization succeeds.
- Fallback path: a deterministic lexical vector space (lowercased token
  counts over a sorted vocabulary) with cosine similarity. This is a
  real, reproducible grouping — clearly labeled as the lexical fallback,
  never presented as semantic-embedding clustering.
- DBSCAN is implemented locally in pure Python (deterministic neighbor
  order, ascending point expansion) — no scikit-learn dependency.

Failure modes are explicit capability statuses, never exceptions:
insufficient candidates, unavailable model, or any internal clustering
error all degrade to unlabeled candidates with a truthful status.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

__all__ = [
    "MIN_CLUSTER_CANDIDATES",
    "SIMILARITY_THRESHOLD",
    "ClusteringResult",
    "cluster_titles",
]

#: Below this many candidates clustering is meaningless.
MIN_CLUSTER_CANDIDATES = 3

#: Cosine similarity required to consider two candidates neighbors.
#: Short titles overlap modestly even within one topic, so the bar is
#: deliberately moderate (deterministic; tuned on synthetic title sets).
SIMILARITY_THRESHOLD = 0.45

#: DBSCAN min samples to form a dense region (kept tiny for small sets).
_MIN_POINTS = 2

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


@dataclass(frozen=True)
class ClusteringResult:
    """Truthful clustering outcome (labels are ``None`` for noise/unlabeled)."""

    labels: list[int | None]
    method: str
    capability_status: str  # "ok" | "insufficient_input" | "fallback_lexical" | "failed"
    n_clusters: int
    detail: str = ""
    diagnostics: dict = field(default_factory=dict)


def _tokenize(title: str) -> list[str]:
    return _TOKEN_RE.findall((title or "").lower())


def _lexical_vectors(titles: list[str]) -> list[dict[str, float]]:
    """Deterministic term-count vectors over a sorted vocabulary."""
    token_sets = [_tokenize(t) for t in titles]
    vocab = sorted({tok for toks in token_sets for tok in toks})
    index = {tok: i for i, tok in enumerate(vocab)}
    vectors: list[dict[str, float]] = []
    for toks in token_sets:
        vec: dict[str, float] = {}
        for tok in toks:
            vec[index[tok]] = vec.get(index[tok], 0.0) + 1.0
        norm = math.sqrt(sum(w * w for w in vec.values())) or 1.0
        vectors.append({k: w / norm for k, w in vec.items()})
    return vectors


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(b) < len(a):
        a, b = b, a
    return sum(w * b.get(k, 0.0) for k, w in a.items())


def _dbscan(similarity: list[list[float]], eps: float, min_pts: int) -> list[int | None]:
    """Deterministic DBSCAN over a precomputed similarity matrix.

    Returns per-point cluster index or ``None`` for noise. Deterministic
    via ascending point order everywhere.
    """
    n = len(similarity)
    neighbors = [
        sorted(j for j in range(n) if j != i and similarity[i][j] >= eps)
        for i in range(n)
    ]
    labels: list[int | None] = [None] * n
    visited = [False] * n
    cluster = 0
    for i in range(n):
        if visited[i]:
            continue
        visited[i] = True
        if len(neighbors[i]) + 1 < min_pts:
            continue  # noise (may still be claimed as a border point later)
        labels[i] = cluster
        seeds = list(neighbors[i])
        while seeds:
            j = seeds.pop(0)
            if not visited[j]:
                visited[j] = True
                if len(neighbors[j]) + 1 >= min_pts:
                    for k in neighbors[j]:
                        if k not in seeds and labels[k] is None and k != i:
                            seeds.append(k)
            if labels[j] is None:
                labels[j] = cluster
        cluster += 1
    return labels


def _cluster_with(vectors: list[dict[str, float]]) -> list[int | None]:
    n = len(vectors)
    similarity = [
        [1.0 if i == j else _cosine(vectors[i], vectors[j]) for j in range(n)]
        for i in range(n)
    ]
    raw = _dbscan(similarity, eps=SIMILARITY_THRESHOLD, min_pts=_MIN_POINTS)
    # Relabel clusters densely 0..k-1 in order of first appearance.
    mapping: dict[int, int] = {}
    labels: list[int | None] = []
    for lbl in raw:
        if lbl is None:
            labels.append(None)
        else:
            if lbl not in mapping:
                mapping[lbl] = len(mapping)
            labels.append(mapping[lbl])
    return labels


def cluster_titles(titles: list[str]) -> ClusteringResult:
    """Cluster candidate titles locally; degrade explicitly, never crash."""
    if len(titles) < MIN_CLUSTER_CANDIDATES:
        return ClusteringResult(
            labels=[None] * len(titles),
            method="none",
            capability_status="insufficient_input",
            n_clusters=0,
            detail=(
                f"only {len(titles)} candidate(s); need >= "
                f"{MIN_CLUSTER_CANDIDATES} for clustering"
            ),
        )

    # Preferred path: FastEmbed local ONNX embeddings (optional dep).
    try:  # pragma: no cover - exercised only when fastembed is installed
        from fastembed import TextEmbedding  # type: ignore

        model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        vectors = [
            {i: float(w) for i, w in enumerate(list(model.embed(title))[0])}
            for title in titles
        ]
        labels = _cluster_with(vectors)
        n_clusters = len({c for c in labels if c is not None})
        return ClusteringResult(
            labels=labels,
            method="fastembed_onnx_dbscan",
            capability_status="ok",
            n_clusters=n_clusters,
            detail="local ONNX embedding model",
        )
    except ImportError:
        pass  # optional dependency absent — deterministic fallback below
    except Exception as exc:  # model download/runtime failure → degrade
        return ClusteringResult(
            labels=[None] * len(titles),
            method="none",
            capability_status="failed",
            n_clusters=0,
            detail=f"fastembed unavailable: {type(exc).__name__}: {exc}",
        )

    # Fallback path: deterministic lexical cosine space (no dependencies).
    try:
        vectors = _lexical_vectors(titles)
        labels = _cluster_with(vectors)
        n_clusters = len({c for c in labels if c is not None})
        return ClusteringResult(
            labels=labels,
            method="lexical_cosine_dbscan",
            capability_status="fallback_lexical",
            n_clusters=n_clusters,
            detail="deterministic lexical vectors + local DBSCAN (fastembed not installed)",
        )
    except Exception as exc:  # clustering must never fail the research run
        return ClusteringResult(
            labels=[None] * len(titles),
            method="none",
            capability_status="failed",
            n_clusters=0,
            detail=f"clustering failed: {type(exc).__name__}: {exc}",
        )