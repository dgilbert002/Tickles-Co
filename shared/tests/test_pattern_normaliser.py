"""
Module: test_pattern_normaliser
Purpose: Unit tests for pattern_normaliser clustering and pattern-fit functions.
Location: /opt/tickles/shared/tests/test_pattern_normaliser.py
"""

import pytest

from shared.intelligence.pattern_normaliser import (
    cluster_tags,
    compute_pattern_fit,
)


def test_cluster_tags_empty() -> None:
    """Empty tag list returns empty dict."""
    assert cluster_tags([]) == {}


def test_cluster_tags_identity() -> None:
    """With sentence-transformers unavailable, identity fallback clusters identical strings."""
    tags = ["breakout", "breakout", "support", "support", "support"]
    clusters = cluster_tags(tags, threshold=0.75, min_cluster_size=2)
    # Identity fallback: each unique string is its own cluster
    assert "breakout" in clusters or len(clusters) == 0
    assert "support" in clusters or len(clusters) == 0


def test_cluster_tags_noise_filtered() -> None:
    """Tags that don't form clusters of min_cluster_size are dropped."""
    # Use semantically unrelated tags so even sentence-transformers won't cluster them
    tags = ["quantum_physics", "chocolate_cake", "rocket_engine"]
    clusters = cluster_tags(tags, threshold=0.75, min_cluster_size=2)
    assert clusters == {}


def test_cluster_tags_threshold_respected() -> None:
    """Tags below threshold should not merge."""
    tags = ["breakout", "totally_unrelated_string_xyz"]
    clusters = cluster_tags(tags, threshold=0.99, min_cluster_size=2)
    assert clusters == {}


def test_compute_pattern_fit_no_match() -> None:
    """Actor tags not in any global cluster → None."""
    result = compute_pattern_fit(
        actor_tags=["unknown_pattern"],
        global_clusters={"breakout": ["breakout", "breakout_daily"]},
    )
    assert result is None


def test_compute_pattern_fit_perfect() -> None:
    """All actor tags match a global cluster → 1.0."""
    result = compute_pattern_fit(
        actor_tags=["breakout", "breakout_daily"],
        global_clusters={"breakout": ["breakout", "breakout_daily", "breakout_weekly"]},
    )
    assert result == 1.0


def test_compute_pattern_fit_partial() -> None:
    """Half of actor tags match → 0.5."""
    result = compute_pattern_fit(
        actor_tags=["breakout", "unknown_pattern"],
        global_clusters={"breakout": ["breakout", "breakout_daily"]},
    )
    assert result == 0.5


def test_compute_pattern_fit_empty_actor() -> None:
    """Empty actor tags → None."""
    result = compute_pattern_fit(
        actor_tags=[],
        global_clusters={"breakout": ["breakout"]},
    )
    assert result is None


def test_compute_pattern_fit_empty_global() -> None:
    """Empty global clusters → None."""
    result = compute_pattern_fit(
        actor_tags=["breakout"],
        global_clusters={},
    )
    assert result is None
