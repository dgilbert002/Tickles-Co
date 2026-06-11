"""
Module: test_chart_hacker_opinion_service
Purpose: Smoke tests for ChartHackerOpinionService.
Location: /opt/tickles/shared/tests/test_chart_hacker_opinion_service.py
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.intelligence.chart_hacker_opinion_service import ChartHackerOpinionService


@pytest.fixture
def service() -> ChartHackerOpinionService:
    return ChartHackerOpinionService(company_id="testco")


class _FakeRecord:
    """Dict-like mock for asyncpg.Record."""

    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key: str):
        return self._data[key]

    def get(self, key: str, default=None):
        return self._data.get(key, default)


@pytest.mark.anyio
async def test_should_fire_initial(service: ChartHackerOpinionService) -> None:
    row = _FakeRecord({"last_opinion_at": None})
    assert service._should_fire(row) is True


@pytest.mark.anyio
async def test_should_fire_sl_tp_change(service: ChartHackerOpinionService) -> None:
    last_op = datetime.now(timezone.utc) - timedelta(hours=2)
    price_at = last_op + timedelta(minutes=30)
    plan_at = last_op + timedelta(hours=1)
    row = _FakeRecord(
        {
            "last_opinion_at": last_op,
            "position_updated_at": plan_at,
            "price_updated_at": price_at,
        }
    )
    assert service._should_fire(row) is True


@pytest.mark.anyio
async def test_should_fire_no_trigger_after_price_tick(service: ChartHackerOpinionService) -> None:
    last_op = datetime.now(timezone.utc) - timedelta(hours=1)
    tick_at = last_op + timedelta(minutes=5)
    row = _FakeRecord(
        {
            "last_opinion_at": last_op,
            "position_updated_at": tick_at,
            "price_updated_at": tick_at,
        }
    )
    assert service._should_fire(row) is False


@pytest.mark.anyio
async def test_should_fire_no_trigger_stable(service: ChartHackerOpinionService) -> None:
    last_op = datetime.now(timezone.utc)
    row = _FakeRecord(
        {
            "last_opinion_at": last_op,
            "position_updated_at": last_op,
            "price_updated_at": last_op,
        }
    )
    assert service._should_fire(row) is False


@pytest.mark.anyio
async def test_tick_no_pool(service: ChartHackerOpinionService) -> None:
    """Tick with no eligible positions returns zero processed."""
    mock_conn = MagicMock()
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.fetchval = AsyncMock(return_value=True)  # lock acquired
    mock_conn.execute = AsyncMock()

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)

    service._pool = mock_pool
    service.prompt_version = "abc123"

    with patch(
        "shared.intelligence.chart_hacker_opinion_service.get_shared_pool",
        return_value=mock_pool,
    ):
        stats = await service.tick()

    assert stats["processed"] == 0
    assert stats["eligible"] == 0
    assert "skipped_lock" not in stats


@pytest.mark.anyio
async def test_tick_lock_held(service: ChartHackerOpinionService) -> None:
    mock_conn = MagicMock()
    mock_conn.fetchval = AsyncMock(return_value=False)  # lock NOT acquired
    mock_conn.execute = AsyncMock()

    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)

    service._pool = mock_pool
    service.prompt_version = "abc123"

    with patch(
        "shared.intelligence.chart_hacker_opinion_service.get_shared_pool",
        return_value=mock_pool,
    ):
        stats = await service.tick()

    assert stats["skipped_lock"] is True
    assert stats["processed"] == 0
