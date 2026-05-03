"""
Module: test_edge_scorer
Purpose: Unit tests for edge_scorer pure function.
Location: /opt/tickles/shared/tests/test_edge_scorer.py
"""

import pytest

from shared.intelligence.edge_scorer import (
    COMPONENT_CAP,
    FORMULA_VERSION,
    MIN_POSITIONS_FOR_DISPLAY,
    WEIGHTS,
    ScorerInputs,
    ScorerOutput,
    _skill_minus_luck,
    compute_edge_score,
)


def _make_position(realised_pnl_pct: float = 5.0, **kwargs) -> dict:
    return {
        "realised_pnl_pct": realised_pnl_pct,
        "stop_loss": kwargs.get("stop_loss", 90.0),
        "exit_price": kwargs.get("exit_price", 105.0),
        "direction": kwargs.get("direction", "long"),
        "opened_at": kwargs.get("opened_at", "2026-04-01T10:00:00+00:00"),
        "closed_at": kwargs.get("closed_at", "2026-04-01T12:00:00+00:00"),
        "total_fees_pct": kwargs.get("total_fees_pct", 0.1),
    }


def test_all_components_available() -> None:
    """12-component actor gets a score in [0, 1] (PHASE_Y §6 added skill_minus_luck)."""
    positions = [_make_position() for _ in range(15)]
    postmortems = [
        {"position_id": i, "market_regime": "trending", "edge_score": 0.7, "reasoning_clarity_score": 0.8}
        for i in range(15)
    ]
    opinions = [{"position_id": i, "would_take_trade": True} for i in range(15)]
    inputs = ScorerInputs(
        closed_positions=positions,
        postmortems=postmortems,
        opinions=opinions,
        pattern_cluster={"actor_cluster_mean_edge": 0.6, "global_mean_edge": 0.5},
        prompt_lift_data={"lift": 0.1},
        social_data={"follower_weighted_hit_rate": 0.75},
        skill_score=0.72,
        skill_window_days=14,
        skill_components={"pnl_quality": 0.7, "consistency": 0.8},
    )
    out = compute_edge_score(inputs)
    assert isinstance(out, ScorerOutput)
    assert 0.0 <= out.edge_score <= 1.0
    assert out.formula_version == FORMULA_VERSION
    assert out.confidence_low is False  # 15 >= 10
    assert len(out.components) == 12
    assert out.components["skill_minus_luck"]["available"] is True
    assert out.components["skill_minus_luck"]["value"] == 0.72


def test_few_positions_omitted() -> None:
    """Actor with < 3 closed positions should not be displayed."""
    positions = [_make_position() for _ in range(2)]
    inputs = ScorerInputs(closed_positions=positions)
    out = compute_edge_score(inputs)
    assert out.confidence_low is True


def test_renormalisation_invariant() -> None:
    """Adding a perfect 11th component to a 10-component actor never decreases score."""
    # 10-component actor (no pattern_fit, no prompt_lift)
    positions = [_make_position() for _ in range(15)]
    postmortems = [
        {"position_id": i, "market_regime": "trending", "edge_score": 0.7, "reasoning_clarity_score": 0.8}
        for i in range(15)
    ]
    opinions = [{"position_id": i, "would_take_trade": True} for i in range(15)]
    inputs_10 = ScorerInputs(
        closed_positions=positions,
        postmortems=postmortems,
        opinions=opinions,
        social_data={"follower_weighted_hit_rate": 0.75},
    )
    out_10 = compute_edge_score(inputs_10)

    # Same actor with 11th component added (perfect pattern_fit)
    inputs_11 = ScorerInputs(
        closed_positions=positions,
        postmortems=postmortems,
        opinions=opinions,
        pattern_cluster={"actor_cluster_mean_edge": 0.9, "global_mean_edge": 0.5},
        social_data={"follower_weighted_hit_rate": 0.75},
    )
    out_11 = compute_edge_score(inputs_11)

    assert out_11.edge_score >= out_10.edge_score


def test_component_cap() -> None:
    """No single component can exceed COMPONENT_CAP in the final score."""
    positions = [{"realised_pnl_pct": 1000.0} for _ in range(15)]
    inputs = ScorerInputs(closed_positions=positions)
    out = compute_edge_score(inputs)
    # Raw pnl_quality can be 1.0 (sigmoid of extreme sharpe), but the
    # capped value used in score computation is clamped to COMPONENT_CAP.
    # We verify the score is not driven above what the cap would allow.
    assert out.edge_score <= COMPONENT_CAP + 1e-6


def test_determinism() -> None:
    """Same inputs → same output across 100 runs."""
    positions = [_make_position(realised_pnl_pct=3.0) for _ in range(12)]
    inputs = ScorerInputs(closed_positions=positions)
    first = compute_edge_score(inputs)
    for _ in range(100):
        subsequent = compute_edge_score(inputs)
        assert subsequent.edge_score == first.edge_score
        assert subsequent.components == first.components


def test_empty_positions() -> None:
    """No positions → score 0, all components unavailable."""
    inputs = ScorerInputs(closed_positions=[])
    out = compute_edge_score(inputs)
    assert out.edge_score == 0.0
    assert all(not c["available"] for c in out.components.values())


def test_weights_sum_to_one() -> None:
    """Renormalised weights should sum to 1.0 within rounding tolerance.

    weights_used values are rounded to 4 decimals at output time, so the sum
    can drift up to 12 × 5e-5 = 6e-4 from 1.0 in the worst case.
    """
    positions = [_make_position() for _ in range(15)]
    postmortems = [
        {"position_id": i, "market_regime": "trending", "edge_score": 0.7, "reasoning_clarity_score": 0.8}
        for i in range(15)
    ]
    inputs = ScorerInputs(
        closed_positions=positions,
        postmortems=postmortems,
    )
    out = compute_edge_score(inputs)
    weight_sum = sum(out.weights_used.values())
    assert abs(weight_sum - 1.0) < 1e-3


def test_five_component_actor() -> None:
    """Actor with only 5 available components gets valid score."""
    positions = [_make_position() for _ in range(8)]
    inputs = ScorerInputs(closed_positions=positions)
    out = compute_edge_score(inputs)
    assert 0.0 <= out.edge_score <= 1.0
    available_count = sum(1 for c in out.components.values() if c["available"])
    assert available_count <= 5  # pnl_quality + discipline + time_to_resolution + cost_efficiency + maybe consistency


def test_three_component_display_omitted() -> None:
    """Actor with 3 positions has confidence_low=True."""
    positions = [_make_position() for _ in range(3)]
    inputs = ScorerInputs(closed_positions=positions)
    out = compute_edge_score(inputs)
    assert out.confidence_low is True


# ---------------------------------------------------------------------------
# PHASE_Y §6 — skill_minus_luck component
# ---------------------------------------------------------------------------


def test_skill_minus_luck_unavailable_when_score_is_none() -> None:
    """``skill_score=None`` ⇒ component reported as ``available=False``."""
    inputs = ScorerInputs(closed_positions=[], skill_score=None)
    value, available = _skill_minus_luck(inputs)
    assert available is False
    assert value == 0.0


def test_skill_minus_luck_passes_through_value_in_unit_interval() -> None:
    """Valid scores in [0, 1] are returned unchanged."""
    for s in (0.0, 0.25, 0.5, 0.75, 1.0):
        inputs = ScorerInputs(closed_positions=[], skill_score=s)
        value, available = _skill_minus_luck(inputs)
        assert available is True
        assert value == s


def test_skill_minus_luck_clips_out_of_range() -> None:
    """Out-of-range values are clipped to [0, 1] (defensive)."""
    high = ScorerInputs(closed_positions=[], skill_score=1.5)
    assert _skill_minus_luck(high) == (1.0, True)
    low = ScorerInputs(closed_positions=[], skill_score=-0.3)
    assert _skill_minus_luck(low) == (0.0, True)


def test_skill_minus_luck_rejects_non_finite() -> None:
    """NaN / inf must be marked unavailable (defensive)."""
    nan_inputs = ScorerInputs(closed_positions=[], skill_score=float("nan"))
    assert _skill_minus_luck(nan_inputs) == (0.0, False)
    inf_inputs = ScorerInputs(closed_positions=[], skill_score=float("inf"))
    assert _skill_minus_luck(inf_inputs) == (0.0, False)


def test_skill_minus_luck_rejects_non_numeric() -> None:
    """Strings or other non-numeric inputs must be marked unavailable."""
    inputs = ScorerInputs(closed_positions=[], skill_score="not-a-number")  # type: ignore[arg-type]
    value, available = _skill_minus_luck(inputs)
    assert available is False
    assert value == 0.0


def test_skill_minus_luck_present_in_default_weights() -> None:
    """The new component must be a recognised key in WEIGHTS at the canonical
    weight per PHASE_Y §6 (0.15)."""
    assert "skill_minus_luck" in WEIGHTS
    assert WEIGHTS["skill_minus_luck"] == 0.15


def test_compute_edge_score_includes_skill_when_supplied() -> None:
    """Supplying a skill_score lifts the available-component count by one."""
    positions = [_make_position() for _ in range(15)]
    base = compute_edge_score(ScorerInputs(closed_positions=positions))
    with_skill = compute_edge_score(
        ScorerInputs(closed_positions=positions, skill_score=0.6)
    )
    base_avail = sum(1 for c in base.components.values() if c["available"])
    with_avail = sum(1 for c in with_skill.components.values() if c["available"])
    assert with_avail == base_avail + 1
    assert with_skill.components["skill_minus_luck"]["available"] is True
    assert base.components["skill_minus_luck"]["available"] is False


def test_compute_edge_score_renormalises_without_skill() -> None:
    """When skill_score is None the renormalised weights must still sum to 1."""
    positions = [_make_position() for _ in range(15)]
    out = compute_edge_score(ScorerInputs(closed_positions=positions))
    assert "skill_minus_luck" in out.components
    assert out.components["skill_minus_luck"]["available"] is False
    assert "skill_minus_luck" not in out.weights_used
    weight_sum = sum(out.weights_used.values())
    assert abs(weight_sum - 1.0) < 1e-3
