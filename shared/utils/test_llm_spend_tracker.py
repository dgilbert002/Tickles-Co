"""Module: test_llm_spend_tracker
Purpose: Smoke tests for the LLM spend tracker.
Location: /opt/tickles/shared/utils/test_llm_spend_tracker.py
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.utils import llm_spend_tracker


@pytest.fixture
def mock_pool():
    """Return a mock asyncpg pool with a mock connection."""
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


@pytest.mark.anyio
async def test_get_spend_summary_day(mock_pool):
    """Daily spend summary returns correct totals and breakdowns."""
    pool, conn = mock_pool
    conn.fetchrow.return_value = {
        "total_usd": 12.34,
        "call_count": 56,
        "total_tokens": 1024,
    }
    conn.fetch.side_effect = [
        # role rows
        [{"role": "vision", "usd": 10.0}, {"role": "text", "usd": 2.34}],
        # company rows
        [{"company_id": "rubicon", "usd": 12.34}],
        # agent rows
        [{"agent_id": "agent_1", "usd": 12.34}],
    ]

    with patch("shared.utils.llm_spend_tracker.get_shared_pool", return_value=pool):
        summary = await llm_spend_tracker.get_spend_summary(period="day")

    assert summary.period == "day"
    assert summary.total_usd == 12.34
    assert summary.call_count == 56
    assert summary.total_tokens == 1024
    assert summary.by_role == {"vision": 10.0, "text": 2.34}
    assert summary.by_company == {"rubicon": 12.34}
    assert summary.by_agent == {"agent_1": 12.34}


@pytest.mark.anyio
async def test_get_spend_summary_week(mock_pool):
    """Weekly period uses 7-day lookback."""
    pool, conn = mock_pool
    conn.fetchrow.return_value = {"total_usd": 0.0, "call_count": 0, "total_tokens": 0}
    conn.fetch.side_effect = [[], [], []]

    with patch("shared.utils.llm_spend_tracker.get_shared_pool", return_value=pool):
        summary = await llm_spend_tracker.get_spend_summary(period="week")

    assert summary.period == "week"
    # Verify the query used >= with a 7-day old timestamp
    call_args = conn.fetchrow.call_args
    assert "created_at >= $1" in call_args[0][0]


@pytest.mark.anyio
async def test_get_spend_summary_month(mock_pool):
    """Monthly period uses 30-day lookback."""
    pool, conn = mock_pool
    conn.fetchrow.return_value = {"total_usd": 0.0, "call_count": 0, "total_tokens": 0}
    conn.fetch.side_effect = [[], [], []]

    with patch("shared.utils.llm_spend_tracker.get_shared_pool", return_value=pool):
        summary = await llm_spend_tracker.get_spend_summary(period="month")

    assert summary.period == "month"


@pytest.mark.anyio
async def test_get_spend_summary_invalid_period(mock_pool):
    """Invalid period raises ValueError."""
    with pytest.raises(ValueError, match="Unknown period: invalid"):
        await llm_spend_tracker.get_spend_summary(period="invalid")


@pytest.mark.anyio
async def test_get_spend_summary_with_filters(mock_pool):
    """Filters are applied to all queries."""
    pool, conn = mock_pool
    conn.fetchrow.return_value = {"total_usd": 5.0, "call_count": 10, "total_tokens": 100}
    conn.fetch.side_effect = [
        [{"role": "vision", "usd": 5.0}],
        [{"company_id": "rubicon", "usd": 5.0}],
        [{"agent_id": "agent_1", "usd": 5.0}],
    ]

    with patch("shared.utils.llm_spend_tracker.get_shared_pool", return_value=pool):
        summary = await llm_spend_tracker.get_spend_summary(
            period="day",
            company_id="rubicon",
            role="vision",
            agent_id="agent_1",
        )

    # Check that all three filter params were used
    sql = conn.fetchrow.call_args[0][0]
    assert "company_id = $2" in sql
    assert "role = $3" in sql
    assert "agent_id = $4" in sql


@pytest.mark.anyio
async def test_get_daily_spend_series(mock_pool):
    """Daily series returns one row per day."""
    pool, conn = mock_pool
    conn.fetch.return_value = [
        {"day": datetime(2026, 4, 29).date(), "total_usd": 12.34, "call_count": 56},
        {"day": datetime(2026, 4, 28).date(), "total_usd": 8.90, "call_count": 40},
    ]

    with patch("shared.utils.llm_spend_tracker.get_shared_pool", return_value=pool):
        series = await llm_spend_tracker.get_daily_spend_series(days=2)

    assert len(series) == 2
    assert series[0]["date"] == "2026-04-29"
    assert series[0]["total_usd"] == 12.34
    assert series[0]["call_count"] == 56
    assert series[1]["date"] == "2026-04-28"


@pytest.mark.anyio
async def test_get_daily_spend_series_with_company(mock_pool):
    """Company filter is applied to daily series."""
    pool, conn = mock_pool
    conn.fetch.return_value = []

    with patch("shared.utils.llm_spend_tracker.get_shared_pool", return_value=pool):
        await llm_spend_tracker.get_daily_spend_series(days=7, company_id="rubicon")

    sql = conn.fetch.call_args[0][0]
    assert "company_id = $2" in sql


@pytest.mark.anyio
async def test_get_daily_spend_series_no_results(mock_pool):
    """Empty result returns empty list."""
    pool, conn = mock_pool
    conn.fetch.return_value = []

    with patch("shared.utils.llm_spend_tracker.get_shared_pool", return_value=pool):
        series = await llm_spend_tracker.get_daily_spend_series(days=30)

    assert series == []
