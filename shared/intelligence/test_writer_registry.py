"""Module: test_writer_registry
Purpose: Smoke tests for the writer-domain registry.
Location: /opt/tickles/shared/intelligence/test_writer_registry.py
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from shared.intelligence import writer_registry


@pytest.fixture(autouse=True)
def reset_mode():
    """Reset the module-level mode to WARNING for each test."""
    original = writer_registry._MODE
    writer_registry._MODE = "WARNING"
    yield
    writer_registry._MODE = original


@pytest.fixture
def mock_pool():
    """Return a mock asyncpg pool with a mock connection."""
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


@pytest.mark.anyio
async def test_register_writer_new_table(mock_pool):
    """Registering a writer for a new table inserts a row."""
    pool, conn = mock_pool

    with patch("shared.intelligence.writer_registry.get_shared_pool", return_value=pool):
        await writer_registry.register_writer("new_table", "test_service")

    conn.execute.assert_called_once()
    sql = conn.execute.call_args[0][0]
    assert "INSERT INTO table_writers" in sql
    assert "new_table" in conn.execute.call_args[0][1:]
    assert "test_service" in conn.execute.call_args[0][1:]


@pytest.mark.anyio
async def test_assert_authorised_allowed(mock_pool):
    """An authorised service returns True."""
    pool, conn = mock_pool
    conn.fetchrow.return_value = {
        "allowed_writer_services": ["interpretation_service", "surgeon2_trader"],
    }

    with patch("shared.intelligence.writer_registry.get_shared_pool", return_value=pool):
        result = await writer_registry.assert_authorised(
            "tracked_positions", "interpretation_service"
        )

    assert result is True


@pytest.mark.anyio
async def test_assert_authorised_not_allowed_warning_mode(mock_pool, caplog):
    """In WARNING mode, unauthorised service logs warning but returns True."""
    pool, conn = mock_pool
    conn.fetchrow.return_value = {
        "allowed_writer_services": ["interpretation_service"],
    }

    with patch("shared.intelligence.writer_registry.get_shared_pool", return_value=pool):
        result = await writer_registry.assert_authorised(
            "tracked_positions", "hacker_service"
        )

    assert result is True
    assert "hacker_service" in caplog.text
    assert "WARNING mode" in caplog.text


@pytest.mark.anyio
async def test_assert_authorised_not_allowed_enforce_mode(mock_pool):
    """In ENFORCE mode, unauthorised service raises RuntimeError."""
    writer_registry._MODE = "ENFORCE"
    pool, conn = mock_pool
    conn.fetchrow.return_value = {
        "allowed_writer_services": ["interpretation_service"],
    }

    with patch("shared.intelligence.writer_registry.get_shared_pool", return_value=pool):
        with pytest.raises(RuntimeError) as exc_info:
            await writer_registry.assert_authorised(
                "tracked_positions", "hacker_service"
            )

    assert "hacker_service" in str(exc_info.value)
    assert "NOT authorised" in str(exc_info.value)


@pytest.mark.anyio
async def test_assert_authorised_no_registry_entry_warning(mock_pool, caplog):
    """When table has no registry entry in WARNING mode, log and allow."""
    pool, conn = mock_pool
    conn.fetchrow.return_value = None

    with patch("shared.intelligence.writer_registry.get_shared_pool", return_value=pool):
        result = await writer_registry.assert_authorised(
            "unknown_table", "any_service"
        )

    assert result is True
    assert "NO registered writers" in caplog.text


@pytest.mark.anyio
async def test_assert_authorised_no_registry_entry_enforce(mock_pool):
    """When table has no registry entry in ENFORCE mode, raise."""
    writer_registry._MODE = "ENFORCE"
    pool, conn = mock_pool
    conn.fetchrow.return_value = None

    with patch("shared.intelligence.writer_registry.get_shared_pool", return_value=pool):
        with pytest.raises(RuntimeError) as exc_info:
            await writer_registry.assert_authorised(
                "unknown_table", "any_service"
            )

    assert "NO registered writers" in str(exc_info.value)


@pytest.mark.anyio
async def test_get_registry_snapshot(mock_pool):
    """Snapshot returns all table → services mappings."""
    pool, conn = mock_pool
    conn.fetch.return_value = [
        {"table_name": "t1", "allowed_writer_services": ["s1"]},
        {"table_name": "t2", "allowed_writer_services": ["s2", "s3"]},
    ]

    with patch("shared.intelligence.writer_registry.get_shared_pool", return_value=pool):
        snapshot = await writer_registry.get_registry_snapshot()

    assert snapshot == {"t1": ["s1"], "t2": ["s2", "s3"]}


@pytest.mark.anyio
async def test_register_writer_with_existing_conn(mock_pool):
    """Can pass an existing connection instead of acquiring from pool."""
    conn = AsyncMock()

    await writer_registry.register_writer("new_table", "test_service", conn=conn)

    conn.execute.assert_called_once()
    # Should NOT have called get_shared_pool
