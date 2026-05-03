"""
Module: test_recall_log
Purpose: Smoke + unit tests for shared/intelligence/recall_log.py
Location: /opt/tickles/shared/tests/test_recall_log.py

Phase Y.2 — covers:
  - record_recall (success, validation rejects, truncation, JSON-fail)
  - link_recall_to_position (success, no-op, UNIQUE conflict, validation)
  - match_recall_to_outcome (no rows, full-tier match, partial matches,
    malformed metadata, None outcome, fetch failure, per-row update failure)
  - Helpers: _truncate, _normalise_outcome, _evaluate_matches,
    _coerce_metadata_list

The pytest config uses ``asyncio_mode = auto`` so coroutine tests run
without the explicit ``@pytest.mark.asyncio`` decorator.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
import pytest

from shared.intelligence.recall_log import (
    _coerce_metadata_list,
    _evaluate_matches,
    _normalise_outcome,
    _truncate,
    link_recall_to_position,
    match_recall_to_outcome,
    record_recall,
)


# ---------------------------------------------------------------------------
# FakeConn — mirrors the pattern used in test_postmortem_service.FakeConn
# ---------------------------------------------------------------------------


class FakeConn:
    """Minimal asyncpg.Connection stand-in capturing all SQL traffic."""

    def __init__(
        self,
        *,
        fetchval_returns: Optional[List[Any]] = None,
        fetchval_raises: Optional[Exception] = None,
        fetch_returns: Optional[List[Dict[str, Any]]] = None,
        fetch_raises: Optional[Exception] = None,
        execute_returns: Optional[List[str]] = None,
        execute_raises: Optional[Exception] = None,
        execute_raises_per_call: Optional[List[Optional[Exception]]] = None,
    ) -> None:
        self._fetchval_returns = list(fetchval_returns or [])
        self._fetchval_raises = fetchval_raises
        self._fetch_returns = list(fetch_returns or [])
        self._fetch_raises = fetch_raises
        self._execute_returns = list(execute_returns or [])
        self._execute_raises = execute_raises
        self._execute_raises_per_call = list(execute_raises_per_call or [])
        self.executed: List[Tuple[str, tuple]] = []
        self.fetched: List[Tuple[str, tuple]] = []
        self.fetchvals: List[Tuple[str, tuple]] = []

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.fetchvals.append((query, args))
        if self._fetchval_raises is not None:
            raise self._fetchval_raises
        if self._fetchval_returns:
            return self._fetchval_returns.pop(0)
        return None

    async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
        self.fetched.append((query, args))
        if self._fetch_raises is not None:
            raise self._fetch_raises
        if self._fetch_returns:
            return self._fetch_returns.pop(0)
        return []

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        if self._execute_raises_per_call:
            err = self._execute_raises_per_call.pop(0)
            if err is not None:
                raise err
        if self._execute_raises is not None:
            raise self._execute_raises
        if self._execute_returns:
            return self._execute_returns.pop(0)
        return "UPDATE 1"


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------


def test_truncate_returns_none_for_none() -> None:
    assert _truncate(None, 10) is None


def test_truncate_passes_through_short_string() -> None:
    assert _truncate("hello", 10) == "hello"


def test_truncate_clamps_long_string() -> None:
    out = _truncate("x" * 5000, 100)
    assert out is not None
    assert len(out) == 100


def test_truncate_coerces_non_string_input() -> None:
    assert _truncate(12345, 10) == "12345"


def test_truncate_preserves_unicode() -> None:
    out = _truncate("ümläut" * 10, 8)
    assert out is not None
    assert len(out) == 8


# ---------------------------------------------------------------------------
# _normalise_outcome
# ---------------------------------------------------------------------------


def test_normalise_outcome_lowercases_and_strips() -> None:
    assert _normalise_outcome("  WIN  ") == "win"


def test_normalise_outcome_returns_none_for_none() -> None:
    assert _normalise_outcome(None) is None


def test_normalise_outcome_returns_none_for_empty_string() -> None:
    assert _normalise_outcome("") is None
    assert _normalise_outcome("   ") is None


def test_normalise_outcome_returns_none_for_non_string() -> None:
    assert _normalise_outcome(123) is None
    assert _normalise_outcome(["win"]) is None


# ---------------------------------------------------------------------------
# _evaluate_matches
# ---------------------------------------------------------------------------


def test_evaluate_matches_full_tier_hit() -> None:
    """All three flags TRUE when one entry matches all three axes."""
    flags = _evaluate_matches(
        [{"outcome": "WIN", "dimension": "lesson", "symbol": "BTC/USDT"}],
        position_outcome="win",
        position_symbol="BTC/USDT",
        query_dimension="lesson",
    )
    assert flags == {"match_outcome": True, "match_dim": True, "match_symbol": True}


def test_evaluate_matches_or_across_top_k() -> None:
    """Different entries can satisfy different axes — OR semantics."""
    flags = _evaluate_matches(
        [
            {"outcome": "win"},
            {"dimension": "lesson"},
            {"symbol": "BTC/USDT"},
        ],
        position_outcome="win",
        position_symbol="BTC/USDT",
        query_dimension="lesson",
    )
    assert flags == {"match_outcome": True, "match_dim": True, "match_symbol": True}


def test_evaluate_matches_partial_only_outcome() -> None:
    flags = _evaluate_matches(
        [{"outcome": "win", "symbol": "ETH/USDT", "dimension": "warning"}],
        position_outcome="win",
        position_symbol="BTC/USDT",
        query_dimension="lesson",
    )
    assert flags["match_outcome"] is True
    assert flags["match_dim"] is False
    assert flags["match_symbol"] is False


def test_evaluate_matches_empty_inputs_all_false() -> None:
    """All-empty inputs still return three non-null bools (CHECK satisfied)."""
    flags = _evaluate_matches(
        [],
        position_outcome=None,
        position_symbol=None,
        query_dimension=None,
    )
    assert flags == {"match_outcome": False, "match_dim": False, "match_symbol": False}
    assert all(isinstance(v, bool) for v in flags.values())


def test_evaluate_matches_skips_non_dict_entries() -> None:
    flags = _evaluate_matches(
        ["garbage", None, 42, {"outcome": "win"}],
        position_outcome="win",
        position_symbol=None,
        query_dimension=None,
    )
    assert flags["match_outcome"] is True


def test_evaluate_matches_outcome_case_insensitive() -> None:
    flags = _evaluate_matches(
        [{"outcome": "Win"}],
        position_outcome="WIN",
        position_symbol=None,
        query_dimension=None,
    )
    assert flags["match_outcome"] is True


def test_evaluate_matches_symbol_case_sensitive() -> None:
    """Symbols are canonicalised upstream, so comparison is case-sensitive."""
    flags = _evaluate_matches(
        [{"symbol": "btc/usdt"}],
        position_outcome=None,
        position_symbol="BTC/USDT",
        query_dimension=None,
    )
    assert flags["match_symbol"] is False


def test_evaluate_matches_dimension_case_insensitive() -> None:
    flags = _evaluate_matches(
        [{"dimension": "LESSON"}],
        position_outcome=None,
        position_symbol=None,
        query_dimension="lesson",
    )
    assert flags["match_dim"] is True


def test_evaluate_matches_none_metadata_returns_all_false() -> None:
    flags = _evaluate_matches(
        None,  # type: ignore[arg-type]
        position_outcome="win",
        position_symbol="BTC/USDT",
        query_dimension="lesson",
    )
    assert flags == {"match_outcome": False, "match_dim": False, "match_symbol": False}


# ---------------------------------------------------------------------------
# _coerce_metadata_list
# ---------------------------------------------------------------------------


def test_coerce_metadata_list_passes_through_list() -> None:
    out = _coerce_metadata_list([{"a": 1}, {"b": 2}])
    assert out == [{"a": 1}, {"b": 2}]


def test_coerce_metadata_list_parses_json_string() -> None:
    out = _coerce_metadata_list('[{"a": 1}, {"b": 2}]')
    assert out == [{"a": 1}, {"b": 2}]


def test_coerce_metadata_list_returns_empty_for_none() -> None:
    assert _coerce_metadata_list(None) == []


def test_coerce_metadata_list_returns_empty_for_garbage_json() -> None:
    assert _coerce_metadata_list("not json at all {[") == []


def test_coerce_metadata_list_returns_empty_for_non_list() -> None:
    assert _coerce_metadata_list({"a": 1}) == []
    assert _coerce_metadata_list(42) == []


def test_coerce_metadata_list_drops_non_dict_elements() -> None:
    out = _coerce_metadata_list([{"a": 1}, "skip", 42, None, {"b": 2}])
    assert out == [{"a": 1}, {"b": 2}]


# ---------------------------------------------------------------------------
# record_recall
# ---------------------------------------------------------------------------


async def test_record_recall_inserts_and_returns_id() -> None:
    conn = FakeConn(fetchval_returns=[4242])
    row_id = await record_recall(
        conn,
        actor_id="charthacker",
        company_id="rubicon",
        query_summary="signal text",
        correlation_id="corr-1",
        query_dimension="lesson",
        query_symbol="BTC/USDT",
        top_k_ids=[1, 2, 3],
        top_k_metadata=[{"outcome": "win"}, {"outcome": "loss"}],
        position_id=999,
    )
    assert row_id == 4242
    assert len(conn.fetchvals) == 1
    _, args = conn.fetchvals[0]
    # Args order: actor_id, company_id, correlation_id, summary, dim,
    # symbol, returned_count, ids_json, md_json, position_id.
    assert args[0] == "charthacker"
    assert args[1] == "rubicon"
    assert args[2] == "corr-1"
    assert args[3] == "signal text"
    assert args[4] == "lesson"
    assert args[5] == "BTC/USDT"
    assert args[6] == 2  # returned_count = len(md_clean)
    assert json.loads(args[7]) == [1, 2, 3]
    assert json.loads(args[8]) == [{"outcome": "win"}, {"outcome": "loss"}]
    assert args[9] == 999


async def test_record_recall_truncates_long_summary() -> None:
    conn = FakeConn(fetchval_returns=[1])
    huge = "x" * 10_000
    await record_recall(
        conn,
        actor_id="a",
        company_id="c",
        query_summary=huge,
    )
    _, args = conn.fetchvals[0]
    assert len(args[3]) == 2000


async def test_record_recall_rejects_missing_actor_id() -> None:
    conn = FakeConn()
    row_id = await record_recall(
        conn,
        actor_id="",
        company_id="rubicon",
        query_summary="x",
    )
    assert row_id is None
    assert conn.fetchvals == []


async def test_record_recall_rejects_missing_company_id() -> None:
    conn = FakeConn()
    row_id = await record_recall(
        conn,
        actor_id="a",
        company_id="",
        query_summary="x",
    )
    assert row_id is None
    assert conn.fetchvals == []


async def test_record_recall_rejects_non_string_actor() -> None:
    conn = FakeConn()
    row_id = await record_recall(
        conn,
        actor_id=None,  # type: ignore[arg-type]
        company_id="rubicon",
        query_summary="x",
    )
    assert row_id is None


async def test_record_recall_handles_none_summary() -> None:
    """``query_summary=None`` is coerced to empty string, not crashed."""
    conn = FakeConn(fetchval_returns=[7])
    row_id = await record_recall(
        conn,
        actor_id="a",
        company_id="c",
        query_summary=None,  # type: ignore[arg-type]
    )
    assert row_id == 7
    _, args = conn.fetchvals[0]
    assert args[3] == ""


async def test_record_recall_serialises_unhashable_via_default() -> None:
    """``json.dumps(default=str)`` should swallow datetime/decimal."""
    from datetime import datetime

    conn = FakeConn(fetchval_returns=[1])
    row_id = await record_recall(
        conn,
        actor_id="a",
        company_id="c",
        query_summary="x",
        top_k_ids=[datetime(2026, 5, 5)],
        top_k_metadata=[{"when": datetime(2026, 5, 5)}],
    )
    assert row_id == 1


async def test_record_recall_returns_none_on_unserialisable_payload() -> None:
    """Truly non-stringifiable objects (e.g., recursive) → None, no INSERT."""
    conn = FakeConn(fetchval_returns=[1])

    class Boom:
        def __str__(self) -> str:  # noqa: D401
            raise RuntimeError("nope")

    # Build a self-referential structure that recurses on json.dumps.
    bad: Dict[str, Any] = {}
    bad["self"] = bad

    row_id = await record_recall(
        conn,
        actor_id="a",
        company_id="c",
        query_summary="x",
        top_k_metadata=[bad],
    )
    assert row_id is None
    assert conn.fetchvals == []


async def test_record_recall_returns_none_on_db_error() -> None:
    conn = FakeConn(fetchval_raises=asyncpg.PostgresError("db down"))
    row_id = await record_recall(
        conn,
        actor_id="a",
        company_id="c",
        query_summary="x",
    )
    assert row_id is None


async def test_record_recall_drops_non_dict_metadata() -> None:
    conn = FakeConn(fetchval_returns=[1])
    await record_recall(
        conn,
        actor_id="a",
        company_id="c",
        query_summary="x",
        top_k_metadata=[{"a": 1}, "skip", None, {"b": 2}],  # type: ignore[list-item]
    )
    _, args = conn.fetchvals[0]
    assert json.loads(args[8]) == [{"a": 1}, {"b": 2}]
    assert args[6] == 2


async def test_record_recall_coerces_invalid_position_id_to_null() -> None:
    """``position_id`` ≤ 0 / non-int / bool is coerced to NULL pre-INSERT."""
    conn = FakeConn(fetchval_returns=[1, 2, 3, 4])
    # Negative
    await record_recall(
        conn, actor_id="a", company_id="c", query_summary="x", position_id=-5
    )
    assert conn.fetchvals[-1][1][9] is None
    # Zero
    await record_recall(
        conn, actor_id="a", company_id="c", query_summary="x", position_id=0
    )
    assert conn.fetchvals[-1][1][9] is None
    # Boolean (subclass of int — must be rejected explicitly)
    await record_recall(
        conn,
        actor_id="a",
        company_id="c",
        query_summary="x",
        position_id=True,  # type: ignore[arg-type]
    )
    assert conn.fetchvals[-1][1][9] is None
    # Non-int string
    await record_recall(
        conn,
        actor_id="a",
        company_id="c",
        query_summary="x",
        position_id="42",  # type: ignore[arg-type]
    )
    assert conn.fetchvals[-1][1][9] is None


async def test_record_recall_accepts_valid_position_id() -> None:
    conn = FakeConn(fetchval_returns=[1])
    await record_recall(
        conn, actor_id="a", company_id="c", query_summary="x", position_id=42
    )
    assert conn.fetchvals[0][1][9] == 42


# ---------------------------------------------------------------------------
# link_recall_to_position
# ---------------------------------------------------------------------------


async def test_link_recall_to_position_success() -> None:
    conn = FakeConn(execute_returns=["UPDATE 1"])
    ok = await link_recall_to_position(conn, recall_id=10, position_id=20)
    assert ok is True
    assert len(conn.executed) == 1
    _, args = conn.executed[0]
    assert args == (20, 10)


async def test_link_recall_to_position_already_linked_returns_false() -> None:
    conn = FakeConn(execute_returns=["UPDATE 0"])
    ok = await link_recall_to_position(conn, recall_id=10, position_id=20)
    assert ok is False


async def test_link_recall_to_position_invalid_recall_id() -> None:
    conn = FakeConn()
    assert await link_recall_to_position(conn, recall_id=0, position_id=1) is False
    assert await link_recall_to_position(conn, recall_id=-5, position_id=1) is False
    assert await link_recall_to_position(
        conn, recall_id="abc", position_id=1  # type: ignore[arg-type]
    ) is False
    assert conn.executed == []


async def test_link_recall_to_position_invalid_position_id() -> None:
    conn = FakeConn()
    assert await link_recall_to_position(conn, recall_id=1, position_id=0) is False
    assert conn.executed == []


async def test_link_recall_to_position_handles_unique_conflict() -> None:
    conn = FakeConn(
        execute_raises=asyncpg.UniqueViolationError("dup key"),
    )
    ok = await link_recall_to_position(conn, recall_id=10, position_id=20)
    assert ok is False


async def test_link_recall_to_position_handles_postgres_error() -> None:
    conn = FakeConn(execute_raises=asyncpg.PostgresError("boom"))
    ok = await link_recall_to_position(conn, recall_id=10, position_id=20)
    assert ok is False


async def test_link_recall_to_position_handles_unparseable_status() -> None:
    """Defensive: status string that doesn't match 'UPDATE n' → False."""
    conn = FakeConn(execute_returns=["WEIRD STATUS"])
    ok = await link_recall_to_position(conn, recall_id=10, position_id=20)
    assert ok is False


# ---------------------------------------------------------------------------
# match_recall_to_outcome
# ---------------------------------------------------------------------------


async def test_match_recall_to_outcome_no_pending_rows() -> None:
    conn = FakeConn(fetch_returns=[[]])
    n = await match_recall_to_outcome(
        conn,
        position_id=1,
        position_outcome="win",
        position_symbol="BTC/USDT",
    )
    assert n == 0
    assert conn.executed == []


async def test_match_recall_to_outcome_full_tier_match() -> None:
    """One pending row, all-three-match → one UPDATE returning UPDATE 1."""
    pending = [
        {
            "id": 100,
            "query_dimension": "lesson",
            "top_k_metadata": json.dumps(
                [{"outcome": "win", "dimension": "lesson", "symbol": "BTC/USDT"}]
            ),
        }
    ]
    conn = FakeConn(fetch_returns=[pending], execute_returns=["UPDATE 1"])
    n = await match_recall_to_outcome(
        conn,
        position_id=1,
        position_outcome="win",
        position_symbol="BTC/USDT",
    )
    assert n == 1
    assert len(conn.executed) == 1
    _, args = conn.executed[0]
    # args: (recall_id, match_outcome, match_dim, match_symbol, matched_at)
    assert args[0] == 100
    assert args[1] is True
    assert args[2] is True
    assert args[3] is True
    # matched_at is a UTC datetime
    assert args[4].tzinfo is not None


async def test_match_recall_to_outcome_partial_match() -> None:
    pending = [
        {
            "id": 101,
            "query_dimension": "lesson",
            "top_k_metadata": [{"outcome": "loss", "symbol": "ETH/USDT"}],
        }
    ]
    conn = FakeConn(fetch_returns=[pending], execute_returns=["UPDATE 1"])
    n = await match_recall_to_outcome(
        conn,
        position_id=1,
        position_outcome="win",
        position_symbol="BTC/USDT",
    )
    assert n == 1
    _, args = conn.executed[0]
    assert args[1] is False  # outcome mismatch
    assert args[2] is False  # no dimension entry
    assert args[3] is False  # symbol mismatch


async def test_match_recall_to_outcome_multiple_rows_independent_evaluation() -> None:
    pending = [
        {
            "id": 200,
            "query_dimension": "lesson",
            "top_k_metadata": [{"outcome": "win"}],
        },
        {
            "id": 201,
            "query_dimension": "warning",
            "top_k_metadata": [{"symbol": "BTC/USDT"}],
        },
    ]
    conn = FakeConn(
        fetch_returns=[pending],
        execute_returns=["UPDATE 1", "UPDATE 1"],
    )
    n = await match_recall_to_outcome(
        conn,
        position_id=1,
        position_outcome="win",
        position_symbol="BTC/USDT",
    )
    assert n == 2
    # First row: outcome match only.
    args_a = conn.executed[0][1]
    assert args_a[0] == 200
    assert args_a[1] is True and args_a[2] is False and args_a[3] is False
    # Second row: symbol match only.
    args_b = conn.executed[1][1]
    assert args_b[0] == 201
    assert args_b[1] is False and args_b[2] is False and args_b[3] is True


async def test_match_recall_to_outcome_malformed_metadata_falls_through() -> None:
    """Garbage JSON in metadata → match flags all-False, but UPDATE still fires."""
    pending = [
        {
            "id": 300,
            "query_dimension": "lesson",
            "top_k_metadata": "not valid json {[",
        }
    ]
    conn = FakeConn(fetch_returns=[pending], execute_returns=["UPDATE 1"])
    n = await match_recall_to_outcome(
        conn,
        position_id=1,
        position_outcome="win",
        position_symbol="BTC/USDT",
    )
    assert n == 1
    args = conn.executed[0][1]
    assert args[1] is False and args[2] is False and args[3] is False


async def test_match_recall_to_outcome_none_outcome() -> None:
    """None outcome → match_outcome must still be a non-null bool (False)."""
    pending = [
        {
            "id": 400,
            "query_dimension": None,
            "top_k_metadata": [{"outcome": "win", "symbol": "BTC/USDT"}],
        }
    ]
    conn = FakeConn(fetch_returns=[pending], execute_returns=["UPDATE 1"])
    n = await match_recall_to_outcome(
        conn,
        position_id=1,
        position_outcome=None,
        position_symbol="BTC/USDT",
    )
    assert n == 1
    args = conn.executed[0][1]
    assert args[1] is False  # no outcome to compare
    assert args[2] is False  # no dimension to compare
    assert args[3] is True   # symbol still matches
    # All three are real bools, never None — CHECK constraint stays satisfied.
    assert all(isinstance(v, bool) for v in args[1:4])


async def test_match_recall_to_outcome_invalid_position_id() -> None:
    conn = FakeConn()
    assert await match_recall_to_outcome(
        conn, position_id=0, position_outcome="win", position_symbol="BTC/USDT"
    ) == 0
    assert await match_recall_to_outcome(
        conn,
        position_id="abc",  # type: ignore[arg-type]
        position_outcome="win",
        position_symbol="BTC/USDT",
    ) == 0
    assert conn.fetched == []


async def test_match_recall_to_outcome_fetch_error_swallowed() -> None:
    conn = FakeConn(fetch_raises=asyncpg.PostgresError("boom"))
    n = await match_recall_to_outcome(
        conn,
        position_id=1,
        position_outcome="win",
        position_symbol="BTC/USDT",
    )
    assert n == 0
    assert conn.executed == []


async def test_match_recall_to_outcome_per_row_update_failure_continues() -> None:
    """One bad row must not block the others from being matched."""
    pending = [
        {"id": 500, "query_dimension": "lesson", "top_k_metadata": [{"outcome": "win"}]},
        {"id": 501, "query_dimension": "warning", "top_k_metadata": [{"outcome": "win"}]},
        {"id": 502, "query_dimension": "lesson", "top_k_metadata": [{"outcome": "win"}]},
    ]
    conn = FakeConn(
        fetch_returns=[pending],
        execute_returns=["UPDATE 1", "UPDATE 1"],
        execute_raises_per_call=[
            None,  # first row succeeds
            asyncpg.PostgresError("transient"),  # second row fails
            None,  # third row succeeds
        ],
    )
    n = await match_recall_to_outcome(
        conn,
        position_id=1,
        position_outcome="win",
        position_symbol="BTC/USDT",
    )
    assert n == 2  # first + third counted, second skipped
    assert len(conn.executed) == 3


async def test_match_recall_to_outcome_unparseable_status_not_counted() -> None:
    pending = [
        {"id": 600, "query_dimension": "lesson", "top_k_metadata": [{"outcome": "win"}]},
    ]
    conn = FakeConn(fetch_returns=[pending], execute_returns=["WEIRD"])
    n = await match_recall_to_outcome(
        conn,
        position_id=1,
        position_outcome="win",
        position_symbol="BTC/USDT",
    )
    assert n == 0
