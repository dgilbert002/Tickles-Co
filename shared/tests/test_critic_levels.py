"""Tests for critic level validation and backfill picks."""
from shared.intelligence.critic_levels import (
    is_valid_critic_sl,
    is_valid_critic_tp,
    pick_critic_levels,
)


def test_short_render_levels() -> None:
    sl, tp = pick_critic_levels(
        direction="short",
        entry=2.372,
        missing_sl=True,
        missing_tp=True,
        critic_sl=2.18,
        critic_tp=1.85,
    )
    assert sl == 2.18
    assert tp == 1.85


def test_long_breakeven_sl() -> None:
    assert is_valid_critic_sl("long", 80.62, 80.62)
    sl, tp = pick_critic_levels(
        direction="long",
        entry=80.62,
        missing_sl=True,
        missing_tp=False,
        critic_sl=80.62,
        critic_tp=None,
    )
    assert sl == 80.62
    assert tp is None


def test_rejects_wrong_side_tp() -> None:
    sl, tp = pick_critic_levels(
        direction="short",
        entry=100.0,
        missing_sl=False,
        missing_tp=True,
        critic_sl=None,
        critic_tp=105.0,
    )
    assert tp is None


def test_only_fills_missing_fields() -> None:
    sl, tp = pick_critic_levels(
        direction="short",
        entry=100.0,
        missing_sl=False,
        missing_tp=True,
        critic_sl=99.0,
        critic_tp=90.0,
    )
    assert sl is None
    assert tp == 90.0


def test_long_tp_valid() -> None:
    assert is_valid_critic_tp("long", 100.0, 120.0)
