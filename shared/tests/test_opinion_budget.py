"""
Module: test_opinion_budget
Purpose: Unit tests for the three-axis opinion budget token bucket.
Location: /opt/tickles/shared/tests/test_opinion_budget.py
"""

import asyncio
import os

import pytest

from shared.intelligence.opinion_budget import OpinionBudget, get_opinion_budget


@pytest.fixture
def budget(monkeypatch) -> OpinionBudget:
    """Fresh OpinionBudget with small caps for fast tests."""
    monkeypatch.setenv("OPINION_GLOBAL_BUDGET_PER_HOUR", "3")
    monkeypatch.setenv("OPINION_GLOBAL_BUDGET_USD_PER_DAY", "1.0")
    monkeypatch.setenv("OPINION_PER_POSITION_PER_HOUR", "2")
    # Re-import constants by creating a fresh instance
    b = OpinionBudget()
    # Patch internal caps directly (the env was read at import time)
    import shared.intelligence.opinion_budget as _mod

    _mod._PER_HOUR = 3
    _mod._USD_PER_DAY = 1.0
    _mod._PER_POS_PER_HOUR = 2
    return b


@pytest.mark.anyio
async def test_acquire_ok(budget: OpinionBudget) -> None:
    ok, why = await budget.try_acquire(1)
    assert ok is True
    assert why == "ok"


@pytest.mark.anyio
async def test_per_position_cap(budget: OpinionBudget) -> None:
    pos_id = 42
    # 2 allowed per position per hour
    ok1, _ = await budget.try_acquire(pos_id)
    ok2, _ = await budget.try_acquire(pos_id)
    ok3, why3 = await budget.try_acquire(pos_id)
    assert ok1 is True
    assert ok2 is True
    assert ok3 is False
    assert "per_position_budget_exhausted" in why3


@pytest.mark.anyio
async def test_global_cap(budget: OpinionBudget) -> None:
    # 3 allowed globally per hour
    for i in range(3):
        ok, _ = await budget.try_acquire(i + 100)
        assert ok is True
    ok4, why4 = await budget.try_acquire(999)
    assert ok4 is False
    assert "global_budget_exhausted" in why4


@pytest.mark.anyio
async def test_usd_cap(budget: OpinionBudget) -> None:
    # Spend $1.01 in one call
    await budget.record_cost(1.01)
    ok, why = await budget.try_acquire(1)
    assert ok is False
    assert "usd_budget_exhausted" in why


@pytest.mark.anyio
async def test_record_cost_increments_usd(budget: OpinionBudget) -> None:
    await budget.record_cost(0.3)
    await budget.record_cost(0.4)
    ok, _ = await budget.try_acquire(1)
    assert ok is True  # 0.7 < 1.0
    await budget.record_cost(0.4)
    ok2, why2 = await budget.try_acquire(2)
    assert ok2 is False
    assert "usd_budget_exhausted" in why2


@pytest.mark.anyio
async def test_get_opinion_budget_singleton() -> None:
    b1 = get_opinion_budget()
    b2 = get_opinion_budget()
    assert b1 is b2
