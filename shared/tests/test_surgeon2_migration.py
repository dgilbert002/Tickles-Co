"""
Module: test_surgeon2_migration
Purpose: Smoke tests for the Phase 10 Surgeon2 migration script.
Location: /opt/tickles/shared/tests/test_surgeon2_migration.py
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.scripts.migrate_surgeon2 import _count_legacy_rows, _count_new_rows, run_migration


class FakeCursor:
    """Fake psycopg2 cursor for unit testing."""

    def __init__(self, responses: list) -> None:
        self._responses = responses
        self._idx = 0
        self.executed: list = []

    def execute(self, sql: str, params=None) -> None:
        self.executed.append((sql, params))

    def fetchone(self):
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeConn:
    """Fake psycopg2 connection for unit testing."""

    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def test_count_legacy_rows_all_exist() -> None:
    """Legacy row counts when all three tables exist and have rows."""
    responses = [
        (1,),  # surgeon2_state exists
        (5,),  # surgeon2_state count
        (1,),  # surgeon2_positions exists
        (3,),  # surgeon2_positions count
        (1,),  # surgeon2_trade_log exists
        (7,),  # surgeon2_trade_log count
    ]
    cur = FakeCursor(responses)
    conn = FakeConn(cur)
    counts = _count_legacy_rows(conn)  # type: ignore[arg-type]
    assert counts == {"surgeon2_state": 5, "surgeon2_positions": 3, "surgeon2_trade_log": 7}


def test_count_legacy_rows_missing_table() -> None:
    """Legacy row counts when a table does not exist."""
    responses = [
        (0,),  # surgeon2_state does NOT exist
        (0,),  # surgeon2_positions does NOT exist
        (0,),  # surgeon2_trade_log does NOT exist
    ]
    cur = FakeCursor(responses)
    conn = FakeConn(cur)
    counts = _count_legacy_rows(conn)  # type: ignore[arg-type]
    assert counts == {"surgeon2_state": 0, "surgeon2_positions": 0, "surgeon2_trade_log": 0}


def test_count_new_rows() -> None:
    """New row counts query canonical tables correctly."""
    responses = [
        (4,),  # tracked_positions count
        (2,),  # agent_state count
        (6,),  # position_updates count
    ]
    cur = FakeCursor(responses)
    conn = FakeConn(cur)
    counts = _count_new_rows(conn)  # type: ignore[arg-type]
    assert counts == {"tracked_positions": 4, "agent_state": 2, "position_updates": 6}


def test_run_migration_dry_run() -> None:
    """Dry-run mode prints counts but does not execute SQL."""
    responses = [
        (1,),  # surgeon2_state exists
        (5,),  # surgeon2_state count
        (1,),  # surgeon2_positions exists
        (3,),  # surgeon2_positions count
        (1,),  # surgeon2_trade_log exists
        (7,),  # surgeon2_trade_log count
    ]
    cur = FakeCursor(responses)
    conn = FakeConn(cur)

    with patch("shared.scripts.migrate_surgeon2._connect", return_value=conn):
        result = run_migration("rubicon", dry_run=True)

    assert result == {"surgeon2_state": 5, "surgeon2_positions": 3, "surgeon2_trade_log": 7}
    assert not conn.committed  # dry-run does not commit


def test_run_migration_execute_parity_pass() -> None:
    """Execute mode runs SQL and passes parity check."""
    legacy_responses = [
        (1,),  # surgeon2_state exists
        (5,),  # surgeon2_state count
        (1,),  # surgeon2_positions exists
        (3,),  # surgeon2_positions count
        (1,),  # surgeon2_trade_log exists
        (7,),  # surgeon2_trade_log count
    ]
    new_responses = [
        (3,),  # tracked_positions count (matches legacy)
        (5,),  # agent_state count
        (7,),  # position_updates count
    ]
    # First _count_legacy_rows, then after migration _count_new_rows
    all_responses = legacy_responses + new_responses
    cur = FakeCursor(all_responses)
    conn = FakeConn(cur)

    with patch("shared.scripts.migrate_surgeon2._connect", return_value=conn):
        with patch.object(Path, "read_text", return_value="SELECT 1;"):
            result = run_migration("rubicon", dry_run=False)

    assert result == {"surgeon2_state": 5, "surgeon2_positions": 3, "surgeon2_trade_log": 7}
    assert conn.committed


def test_run_migration_execute_parity_fail() -> None:
    """Execute mode logs error when parity check fails."""
    legacy_responses = [
        (1,), (5,),  # surgeon2_state
        (1,), (3,),  # surgeon2_positions
        (1,), (7,),  # surgeon2_trade_log
    ]
    new_responses = [
        (2,),  # tracked_positions count (MISMATCH: expected 3)
        (5,),  # agent_state
        (7,),  # position_updates
    ]
    all_responses = legacy_responses + new_responses
    cur = FakeCursor(all_responses)
    conn = FakeConn(cur)

    with patch("shared.scripts.migrate_surgeon2._connect", return_value=conn):
        with patch.object(Path, "read_text", return_value="SELECT 1;"):
            with patch("shared.scripts.migrate_surgeon2.logger") as mock_log:
                result = run_migration("rubicon", dry_run=False)

    assert result == {"surgeon2_state": 5, "surgeon2_positions": 3, "surgeon2_trade_log": 7}
    assert conn.committed
    # Parity mismatch should be logged as error
    error_calls = [c for c in mock_log.error.call_args_list if "PARITY MISMATCH" in str(c)]
    assert len(error_calls) == 1
