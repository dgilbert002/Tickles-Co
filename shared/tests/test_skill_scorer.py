"""
Test: skill_scorer
Purpose: Smoke tests for the Phase Y.1 Python skill-score mirror.
Location: /opt/tickles/shared/tests/test_skill_scorer.py
"""

from __future__ import annotations

import math

import pytest

from shared.intelligence.skill_scorer import WEIGHTS, compute_skill, explain


# ---------------------------------------------------------------------------
# WEIGHTS invariants
# ---------------------------------------------------------------------------
def test_weights_sum_to_one() -> None:
    assert math.isclose(sum(WEIGHTS.values()), 1.0, abs_tol=1e-9)


def test_weights_are_canonical_five_components() -> None:
    assert set(WEIGHTS) == {
        "clarity", "consistency", "reason_corr", "recall_hit", "prompt_lift"
    }


def test_weights_match_plan_values() -> None:
    # Per PHASE_Y_LEARNING_DASHBOARD_PLAN.md §3.1
    assert WEIGHTS["clarity"]     == 0.30
    assert WEIGHTS["consistency"] == 0.25
    assert WEIGHTS["reason_corr"] == 0.20
    assert WEIGHTS["recall_hit"]  == 0.15
    assert WEIGHTS["prompt_lift"] == 0.10


# ---------------------------------------------------------------------------
# compute_skill — happy paths
# ---------------------------------------------------------------------------
def test_all_zeros_yields_zero() -> None:
    score, dropped = compute_skill({k: 0.0 for k in WEIGHTS})
    assert score == 0.0
    assert dropped == []


def test_all_ones_yields_one() -> None:
    score, dropped = compute_skill({k: 1.0 for k in WEIGHTS})
    assert math.isclose(score, 1.0, abs_tol=1e-9)
    assert dropped == []


def test_weighted_sum_no_dropouts() -> None:
    components = {
        "clarity":     1.0,
        "consistency": 0.0,
        "reason_corr": 1.0,
        "recall_hit":  0.0,
        "prompt_lift": 1.0,
    }
    # Expected: 0.30*1 + 0.25*0 + 0.20*1 + 0.15*0 + 0.10*1 = 0.60
    score, dropped = compute_skill(components)
    assert math.isclose(score, 0.60, abs_tol=1e-9)
    assert dropped == []


# ---------------------------------------------------------------------------
# compute_skill — dropout / renormalisation
# ---------------------------------------------------------------------------
def test_one_dropout_renormalises() -> None:
    # Drop recall_hit; the remaining four weights sum to 0.85; renormalise.
    components = {
        "clarity":     1.0,
        "consistency": 1.0,
        "reason_corr": 1.0,
        "recall_hit":  None,
        "prompt_lift": 1.0,
    }
    score, dropped = compute_skill(components)
    # After renormalisation all four contribute fully; score should be 1.0.
    assert math.isclose(score, 1.0, abs_tol=1e-9)
    assert dropped == ["recall_hit"]


def test_two_dropouts_renormalise_correctly() -> None:
    components = {
        "clarity":     0.5,
        "consistency": 1.0,
        "reason_corr": None,
        "recall_hit":  None,
        "prompt_lift": 0.0,
    }
    # Active weights: 0.30 + 0.25 + 0.10 = 0.65
    # Weighted sum:  0.30*0.5 + 0.25*1.0 + 0.10*0.0 = 0.40
    # Score: 0.40 / 0.65 = 0.61538...
    score, dropped = compute_skill(components)
    assert math.isclose(score, 0.40 / 0.65, abs_tol=1e-9)
    # dropped is sorted alphabetically: 'reason_corr' < 'recall_hit'
    # ('rea' < 'rec' because 'a' < 'c').
    assert dropped == sorted(["recall_hit", "reason_corr"])


def test_all_none_yields_zero_score() -> None:
    components = {k: None for k in WEIGHTS}
    score, dropped = compute_skill(components)
    assert score == 0.0
    assert sorted(dropped) == sorted(WEIGHTS)


def test_missing_key_treated_as_dropout() -> None:
    # Don't even pass recall_hit & prompt_lift.
    components = {"clarity": 1.0, "consistency": 1.0, "reason_corr": 1.0}
    score, dropped = compute_skill(components)
    assert math.isclose(score, 1.0, abs_tol=1e-9)
    assert dropped == ["prompt_lift", "recall_hit"]


# ---------------------------------------------------------------------------
# Clipping
# ---------------------------------------------------------------------------
def test_values_clipped_to_unit_interval() -> None:
    components = {
        "clarity":     -5.0,   # clip to 0
        "consistency": 99.0,   # clip to 1
        "reason_corr": 0.5,
        "recall_hit":  0.5,
        "prompt_lift": 0.5,
    }
    # 0.30*0 + 0.25*1 + 0.20*0.5 + 0.15*0.5 + 0.10*0.5 = 0.475
    score, dropped = compute_skill(components)
    assert math.isclose(score, 0.475, abs_tol=1e-9)
    assert dropped == []


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
def test_non_mapping_raises_typeerror() -> None:
    with pytest.raises(TypeError):
        compute_skill([("clarity", 1.0)])  # type: ignore[arg-type]


def test_non_finite_raises_valueerror() -> None:
    with pytest.raises(ValueError):
        compute_skill({"clarity": float("inf"),
                       "consistency": 0, "reason_corr": 0,
                       "recall_hit": 0, "prompt_lift": 0})

    with pytest.raises(ValueError):
        compute_skill({"clarity": float("nan"),
                       "consistency": 0, "reason_corr": 0,
                       "recall_hit": 0, "prompt_lift": 0})


def test_unparseable_value_raises_valueerror() -> None:
    with pytest.raises(ValueError):
        compute_skill({"clarity": "banana",  # type: ignore[dict-item]
                       "consistency": 0, "reason_corr": 0,
                       "recall_hit": 0, "prompt_lift": 0})


def test_bool_value_rejected_explicitly() -> None:
    """bool is a subclass of int; float(True)==1.0 would silently coerce a
    caller bug into a max-confidence component. Must raise instead."""
    with pytest.raises(ValueError, match="bool"):
        compute_skill({"clarity": True,  # type: ignore[dict-item]
                       "consistency": 0.5, "reason_corr": 0.5,
                       "recall_hit": 0.5, "prompt_lift": 0.5})

    with pytest.raises(ValueError, match="bool"):
        compute_skill({"clarity": 0.5, "consistency": False,  # type: ignore[dict-item]
                       "reason_corr": 0.5,
                       "recall_hit": 0.5, "prompt_lift": 0.5})


def test_unknown_key_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        score, dropped = compute_skill({
            "clarity": 1.0, "consistency": 1.0, "reason_corr": 1.0,
            "recall_hit": 1.0, "prompt_lift": 1.0,
            "bogus_component": 0.5,
        })
    assert math.isclose(score, 1.0, abs_tol=1e-9)
    assert "bogus_component" in caplog.text


# ---------------------------------------------------------------------------
# explain()
# ---------------------------------------------------------------------------
def test_explain_returns_canonical_keys() -> None:
    out = explain({k: 0.5 for k in WEIGHTS})
    assert set(out) == {"score", "dropped", "weights_used", "components"}


def test_explain_renormalised_weights_sum_to_one() -> None:
    out = explain({"clarity": 1.0, "consistency": 1.0, "reason_corr": None,
                   "recall_hit": None, "prompt_lift": 1.0})
    weights_used = out["weights_used"]
    assert isinstance(weights_used, dict)
    assert math.isclose(sum(weights_used.values()), 1.0, abs_tol=1e-9)  # type: ignore[arg-type]
    # Dropped components must NOT appear in weights_used
    assert "reason_corr" not in weights_used
    assert "recall_hit"  not in weights_used


def test_explain_all_none_has_empty_weights_used() -> None:
    out = explain({k: None for k in WEIGHTS})
    assert out["score"] == 0.0
    assert out["weights_used"] == {}
    assert sorted(out["dropped"]) == sorted(WEIGHTS)  # type: ignore[arg-type]


def test_explain_components_clipped() -> None:
    out = explain({"clarity": -1.0, "consistency": 5.0,
                   "reason_corr": 0.7, "recall_hit": 0.3, "prompt_lift": 0.1})
    components = out["components"]
    assert isinstance(components, dict)
    assert components["clarity"]     == 0.0  # type: ignore[index]
    assert components["consistency"] == 1.0  # type: ignore[index]
