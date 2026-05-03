"""
Module: test_dashboard_foundation
Purpose: Smoke test for Phase L.1 Foundation (db_pools and snapshot aggregation).
Location: /opt/tickles/shared/tests/test_dashboard_foundation.py
"""

import asyncio
import logging
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from shared.dashboard.db_pools import get_company_pool, close_all_dashboard_pools
from shared.dashboard import snapshot as snapshot_module
from shared.dashboard.snapshot import aggregate_open_positions

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_db_pools_caching():
    """Verify that get_company_pool caches pools."""
    with patch("shared.dashboard.db_pools._open", new_callable=AsyncMock) as mock_open:
        mock_pool = MagicMock()
        mock_pool.close = AsyncMock()
        mock_open.return_value = mock_pool

        # First call
        pool1 = await get_company_pool("rubicon")
        assert pool1 == mock_pool
        assert mock_open.call_count == 1

        # Second call (should be cached)
        pool2 = await get_company_pool("rubicon")
        assert pool2 == mock_pool
        assert mock_open.call_count == 1

        await close_all_dashboard_pools()


@pytest.mark.asyncio
async def test_aggregate_open_positions_shared_ledger():
    """Verify that aggregate_open_positions reads from the shared ledger.

    `tracked_positions` lives in `tickles_shared` (single source of truth);
    rows are tagged with `_company` from the per-row `company_id` column.
    """
    # Reset cache to force a fresh read
    snapshot_module._SNAPSHOT_CACHE.clear()

    mock_pool = MagicMock()
    mock_conn = MagicMock()

    # Mock async context manager for pool.acquire()
    acquire_ctx = MagicMock()
    acquire_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
    acquire_ctx.__aexit__ = AsyncMock(return_value=None)
    mock_pool.acquire = MagicMock(return_value=acquire_ctx)

    mock_conn.fetch = AsyncMock(
        return_value=[
            {"id": 1, "status": "open", "company_id": "rubicon"},
            {"id": 2, "status": "open", "company_id": "testco"},
        ]
    )

    with patch(
        "shared.utils.db.get_shared_pool", new_callable=AsyncMock
    ) as mock_get_shared:
        mock_get_shared.return_value = mock_pool

        rows = await aggregate_open_positions()

        assert len(rows) == 2
        assert rows[0]["_company"] == "rubicon"
        assert rows[1]["_company"] == "testco"
        assert mock_get_shared.call_count == 1


if __name__ == "__main__":
    asyncio.run(test_db_pools_caching())
    asyncio.run(test_aggregate_open_positions_shared_ledger())
    print("Foundation smoke tests passed!")
