"""
Module: test_coach_service
Purpose: Unit tests for CoachService variant assignment and promotion logic.
Location: /opt/tickles/shared/tests/test_coach_service.py
"""

from datetime import date

import pytest

from shared.intelligence.coach_service import assign_variant


def test_assign_variant_deterministic() -> None:
    """Same actor, day, prompt → same variant every time."""
    v = assign_variant("alice", date(2026, 5, 1), "chart_analysis", ["v1", "v2"])
    for _ in range(100):
        assert assign_variant("alice", date(2026, 5, 1), "chart_analysis", ["v1", "v2"]) == v


def test_assign_variant_different_actor() -> None:
    """Different actors may get different variants."""
    v1 = assign_variant("alice", date(2026, 5, 1), "chart_analysis", ["v1", "v2"])
    v2 = assign_variant("bob", date(2026, 5, 1), "chart_analysis", ["v1", "v2"])
    assert v1 in ("v1", "v2")
    assert v2 in ("v1", "v2")


def test_assign_variant_different_day() -> None:
    """Same actor, different day → may get different variant."""
    v1 = assign_variant("alice", date(2026, 5, 1), "chart_analysis", ["v1", "v2"])
    v2 = assign_variant("alice", date(2026, 5, 2), "chart_analysis", ["v1", "v2"])
    assert v1 in ("v1", "v2")
    assert v2 in ("v1", "v2")


def test_assign_variant_three_variants() -> None:
    """Works with more than 2 variants."""
    v = assign_variant("alice", date(2026, 5, 1), "chart_analysis", ["v1", "v2", "v3"])
    assert v in ("v1", "v2", "v3")


def test_assign_variant_single_variant() -> None:
    """Single variant always returns that variant."""
    v = assign_variant("alice", date(2026, 5, 1), "chart_analysis", ["v1"])
    assert v == "v1"
