"""
Module: test_orphan_postmortem_check
Purpose: Smoke tests for shared/jobs/orphan_postmortem_check.py
Location: /opt/tickles/shared/jobs/test_orphan_postmortem_check.py
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from shared.jobs.orphan_postmortem_check import _find_orphans_for_company, run_orphan_check


class FakeRow:
    """Minimal asyncpg.Record stand-in."""

    def __init__(self, **kwargs):
        self._data = kwargs

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)


@pytest.mark.anyio
async def test_find_orphans_no_postmortems():
    """When a company has no postmortems, return empty list."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    shared_pool = AsyncMock()

    orphans = await _find_orphans_for_company("rubicon", conn, shared_pool)
    assert orphans == []
    conn.fetch.assert_awaited_once()


@pytest.mark.anyio
async def test_find_orphans_all_valid():
    """When all position_ids exist in shared ledger, return empty list."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        FakeRow(id=1, position_id=100, created_at="2026-04-30T00:00:00"),
        FakeRow(id=2, position_id=101, created_at="2026-04-30T00:00:00"),
    ])

    shared_conn = AsyncMock()
    shared_conn.fetch = AsyncMock(return_value=[
        FakeRow(id=100),
        FakeRow(id=101),
    ])
    shared_pool = AsyncMock()
    shared_pool.acquire = MagicMock()
    shared_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=shared_conn)
    shared_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    orphans = await _find_orphans_for_company("rubicon", conn, shared_pool)
    assert orphans == []


@pytest.mark.anyio
async def test_find_orphans_some_missing():
    """When some position_ids are missing from shared ledger, return orphans."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        FakeRow(id=1, position_id=100, created_at="2026-04-30T00:00:00"),
        FakeRow(id=2, position_id=999, created_at="2026-04-30T00:00:00"),
    ])

    shared_conn = AsyncMock()
    shared_conn.fetch = AsyncMock(return_value=[FakeRow(id=100)])
    shared_pool = AsyncMock()
    shared_pool.acquire = MagicMock()
    shared_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=shared_conn)
    shared_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    orphans = await _find_orphans_for_company("rubicon", conn, shared_pool)
    assert len(orphans) == 1
    assert orphans[0]["position_id"] == 999


@pytest.mark.anyio
async def test_find_orphans_db_error_graceful():
    """If the per-company DB query fails, log warning and return empty list."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=Exception("connection lost"))
    shared_pool = AsyncMock()

    orphans = await _find_orphans_for_company("rubicon", conn, shared_pool)
    assert orphans == []


@pytest.mark.anyio
async def test_run_orphan_check_with_explicit_companies():
    """run_orphan_check can target specific companies."""
    with patch(
        "shared.jobs.orphan_postmortem_check.get_shared_pool",
        new_callable=AsyncMock,
    ) as mock_shared_pool:
        mock_shared_pool.return_value = AsyncMock()
        # Mock the per-company connection
        with patch(
            "shared.jobs.orphan_postmortem_check.asyncpg",
            autospec=True,
        ) as mock_asyncpg:
            mock_conn = AsyncMock()
            mock_conn.fetch = AsyncMock(return_value=[])
            mock_asyncpg.connect = AsyncMock(return_value=mock_conn)

            result = await run_orphan_check(
                dry_run=True,
                companies=["rubicon"],
            )

    assert result["total_orphans"] == 0
    assert result["errors"] == []
