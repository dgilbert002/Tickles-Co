"""
Module: test_writer_registry
Purpose: Unit tests for Phase 10 writer-domain registry.
Location: /opt/tickles/shared/tests/test_writer_registry.py
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.intelligence.writer_registry import assert_authorised, register_writer


@pytest.mark.anyio
async def test_register_writer_idempotent() -> None:
    """register_writer appends service to allowed list idempotently."""
    mock_conn = MagicMock()
    mock_conn.execute = AsyncMock()

    await register_writer("tracked_positions", "surgeon2_trader", conn=mock_conn)

    mock_conn.execute.assert_awaited_once()
    sql = mock_conn.execute.await_args[0][0]
    assert "INSERT INTO table_writers" in sql
    assert "ON CONFLICT (table_name)" in sql


@pytest.mark.anyio
async def test_assert_authorised_allows_registered() -> None:
    """assert_authorised returns True for registered service."""
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={"allowed_writer_services": ["surgeon2_trader", "interpretation_service"]}
    )

    result = await assert_authorised("tracked_positions", "surgeon2_trader", conn=mock_conn)
    assert result is True


@pytest.mark.anyio
async def test_assert_authorised_warns_on_unregistered(monkeypatch) -> None:
    """In WARNING mode, unauthorised writes are logged but allowed."""
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={"allowed_writer_services": ["interpretation_service"]}
    )

    import shared.intelligence.writer_registry as wr
    monkeypatch.setattr(wr, "_MODE", "WARNING")
    result = await assert_authorised("tracked_positions", "rogue_service", conn=mock_conn)
    assert result is True


@pytest.mark.anyio
async def test_assert_authorised_enforces_when_enforce_mode(monkeypatch) -> None:
    """In ENFORCE mode, unauthorised writes raise RuntimeError."""
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={"allowed_writer_services": ["interpretation_service"]}
    )

    import shared.intelligence.writer_registry as wr
    monkeypatch.setattr(wr, "_MODE", "ENFORCE")
    with pytest.raises(RuntimeError, match="WRITER-REGISTRY"):
        await assert_authorised("tracked_positions", "rogue_service", conn=mock_conn)


@pytest.mark.anyio
async def test_assert_authorised_auto_registers_missing_table(monkeypatch) -> None:
    """No registry entry for table → auto-registers and allows."""
    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)
    mock_conn.execute = AsyncMock()

    import shared.intelligence.writer_registry as wr
    monkeypatch.setattr(wr, "_MODE", "WARNING")
    result = await assert_authorised("new_table", "some_service", conn=mock_conn)
    assert result is True
