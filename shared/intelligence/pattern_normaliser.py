"""
Module: pattern_normaliser
Purpose: Cluster LLM-emitted pattern_tags using sentence-transformer cosine similarity.
Location: /opt/tickles/shared/intelligence/pattern_normaliser.py
"""

import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Graceful fallback if sentence-transformers is unavailable
_HAS_ST = False
try:
    from sentence_transformers import SentenceTransformer
    _HAS_ST = True
except ImportError:
    logger.warning("sentence-transformers not installed; pattern_normaliser will use identity fallback")


_MODEL_NAME = os.environ.get("PATTERN_EMBED_MODEL", "all-MiniLM-L6-v2")


def _embed_tags(tags: List[str]) -> List[List[float]]:
    """Embed tags using sentence-transformer; fallback to one-hot on import error."""
    if not _HAS_ST:
        # Identity fallback: each tag is its own cluster
        return [[float(i == j) for j in range(len(tags))] for i in range(len(tags))]
    try:
        model = SentenceTransformer(_MODEL_NAME)
        embeddings = model.encode(tags, convert_to_numpy=True)
        return [emb.tolist() for emb in embeddings]
    except Exception as exc:
        logger.warning("Embedding failed: %s; falling back to identity", exc)
        return [[float(i == j) for j in range(len(tags))] for i in range(len(tags))]


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def cluster_tags(
    tags: List[str],
    threshold: float = 0.75,
    min_cluster_size: int = 2,
) -> Dict[str, List[str]]:
    """Cluster pattern tags by cosine similarity of embeddings.

    Args:
        tags: List of pattern tag strings from LLM output.
        threshold: Minimum cosine similarity to merge into same cluster.
        min_cluster_size: Minimum tags per cluster (smaller → noise).

    Returns:
        Dict mapping cluster label (first tag in cluster) to list of tags.
    """
    if not tags:
        return {}

    embeddings = _embed_tags(tags)
    n = len(tags)
    visited = [False] * n
    clusters: Dict[str, List[str]] = {}

    for i in range(n):
        if visited[i]:
            continue
        cluster = [tags[i]]
        visited[i] = True
        for j in range(i + 1, n):
            if visited[j]:
                continue
            sim = _cosine_similarity(embeddings[i], embeddings[j])
            if sim >= threshold:
                cluster.append(tags[j])
                visited[j] = True
        if len(cluster) >= min_cluster_size:
            label = cluster[0]
            clusters[label] = cluster
        else:
            # Noise cluster — mark as visited but don't return
            pass

    return clusters


def compute_pattern_fit(
    actor_tags: List[str],
    global_clusters: Dict[str, List[str]],
) -> Optional[float]:
    """Compute pattern_fit score: mean edge of actor's cluster vs global mean.

    Args:
        actor_tags: Tags assigned to this actor's positions.
        global_clusters: Output of cluster_tags() over all actors.

    Returns:
        Float in [0, 1] or None if no match.
    """
    if not actor_tags or not global_clusters:
        return None

    matched = 0
    for tag in actor_tags:
        for label, members in global_clusters.items():
            if tag in members:
                matched += 1
                break

    if matched == 0:
        return None

    # Simple heuristic: fraction of actor tags that land in any cluster
    return matched / len(actor_tags)
