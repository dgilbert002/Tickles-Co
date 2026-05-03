"""
Module: test_memu_listener
Purpose: Smoke tests for MemuListenerService daemon.
Location: /opt/tickles/shared/tests/test_memu_listener.py
"""

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/opt/tickles")

from shared.memu.broadcast_payload import BroadcastPayload
from shared.memu.listener_service import MemuListenerService


@pytest.fixture
def service():
    return MemuListenerService()


class FakeConn:
    def __init__(self):
        self.executed = []
        self.fetched = []

    async def fetch(self, query, *args):
        self.fetched.append(("fetch", query, args))
        return []

    async def fetchrow(self, query, *args):
        self.fetched.append(("fetchrow", query, args))
        return None

    async def execute(self, query, *args):
        self.executed.append(("execute", query, args))

    async def add_listener(self, channel, callback):
        pass

    async def close(self):
        pass


def test_service_init(service):
    assert service._stop.is_set() is False


@pytest.mark.anyio
async def test_backfill_no_rows(service):
    conn = FakeConn()
    await service._backfill(conn)
    assert any("processed_at IS NULL" in str(q) for _, q, _ in conn.fetched)


@pytest.mark.anyio
async def test_process_row_skip_locked(service):
    conn = FakeConn()
    # fetchrow returns None -> row already processed or locked
    await service._process_row(conn, row_id=1)
    assert any("FOR UPDATE SKIP LOCKED" in str(q) for _, q, _ in conn.fetched)


@pytest.mark.anyio
async def test_process_row_success(service):
    conn = FakeConn()

    payload = BroadcastPayload(
        schema_version=1,
        company="testco",
        actor_type="agent",
        actor_id="chart_hacker",
        insight_kind="lesson",
        summary="Test lesson",
        body_md="# Lesson\nThis is a test.",
        correlation_id="corr-123",
        created_at_iso="2026-04-30T12:00:00Z",
    )

    conn.fetched.append(("fetchrow", "", ()))  # primed
    conn.fetched = [("fetchrow", "", ())]

    # Mock the fetchrow to return a row with payload
    row = MagicMock()
    row.__getitem__ = lambda self, key: {"id": 1, "payload": json.dumps(payload)}.get(key)

    async def mock_fetchrow(query, *args):
        conn.fetched.append(("fetchrow", query, args))
        return row

    conn.fetchrow = mock_fetchrow

    with patch("shared.memu.listener_service.get_memu") as mock_get_memu:
        mock_memu = MagicMock()
        mock_memu.write_insight = MagicMock()
        mock_get_memu.return_value = mock_memu

        await service._process_row(conn, row_id=1)

    # Should mark processed
    assert any(
        "processed_at=now()" in str(q)
        for _, q, _ in conn.executed
    )
    # Should call MemU write_insight
    mock_memu.write_insight.assert_called_once()


@pytest.mark.anyio
async def test_process_row_failure_increments_attempt(service):
    conn = FakeConn()

    row = MagicMock()
    row.__getitem__ = lambda self, key: {
        "id": 1,
        "payload": json.dumps({
            "schema_version": 1,
            "company": "testco",
            "actor_type": "agent",
            "actor_id": "chart_hacker",
            "insight_kind": "lesson",
            "summary": "Test",
            "body_md": "Test body",
            "correlation_id": "corr-123",
            "created_at_iso": "2026-04-30T12:00:00Z",
        }),
    }.get(key)

    async def mock_fetchrow(query, *args):
        conn.fetched.append(("fetchrow", query, args))
        return row

    conn.fetchrow = mock_fetchrow

    with patch("shared.memu.listener_service.get_memu") as mock_get_memu:
        mock_memu = MagicMock()
        mock_memu.write_insight.side_effect = RuntimeError("MemU down")
        mock_get_memu.return_value = mock_memu

        with pytest.raises(RuntimeError):
            await service._process_row(conn, row_id=1)

    # Should increment attempt_count and store error
    assert any(
        "attempt_count=attempt_count+1" in str(q)
        for _, q, _ in conn.executed
    )
