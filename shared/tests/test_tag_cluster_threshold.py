"""
Module: test_tag_cluster_threshold
Purpose: Verify tag normaliser stub exports threshold env vars.
Location: /opt/tickles/shared/tests/test_tag_cluster_threshold.py
"""

import os
import importlib
from shared.intelligence import tag_normaliser


def test_threshold_default() -> None:
    """Default threshold is 0.78."""
    assert tag_normaliser.TAG_CLUSTER_COSINE_THRESHOLD == 0.78


def test_borderline_band_default() -> None:
    """Default borderline band is 0.05."""
    assert tag_normaliser.TAG_BORDERLINE_BAND == 0.05


def test_env_override_changes_threshold(monkeypatch) -> None:
    """[AK] Env override changes the stub's exported threshold."""
    monkeypatch.setenv("TAG_CLUSTER_COSINE_THRESHOLD", "0.85")
    reloaded = importlib.reload(tag_normaliser)
    assert reloaded.TAG_CLUSTER_COSINE_THRESHOLD == 0.85


def test_cluster_tags_identity_mapping() -> None:
    """Phase 6 stub returns identity mapping."""
    tags = ["breakout", "ascending_triangle"]
    result = tag_normaliser.cluster_tags(tags)
    assert result == {"breakout": ["breakout"], "ascending_triangle": ["ascending_triangle"]}
