"""
Module: test_interpretation_drawer_provider
Purpose: Unit tests for the Phase X.5 cross-tab Interpretation Drawer
         provider. Mirrors the FakeConn / FakePool pattern used in
         ``shared/tests/test_news_provider.py``.
Location: /opt/tickles/shared/tests/test_interpretation_drawer_provider.py
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from shared.dashboard.interpretation_drawer_provider import (
    DRAWER_DEFAULT_LIMIT,
    DRAWER_LIMIT_CAP,
    DRAWER_PROVIDER_TIMEOUT_S,
    InterpretationDrawerProvider,
    _normalise_limit,
    _opt_decimal,
    _opt_int,
    _opt_iso,
    _run_with_budget,
    _validate_id,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fakes — mirror the asyncpg surface used by the provider.
# ---------------------------------------------------------------------------


class FakeConn:
    """Minimal asyncpg.Connection stand-in for the drawer provider."""

    def __init__(
        self,
        rows: Optional[List[Dict[str, Any]]] = None,
        fail: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.rows = rows or []
        self.fail = fail
        self.delay = delay
        self.queries: List[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
        self.queries.append((query, args))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("simulated DB failure")
        return list(self.rows)


class _AcquireCtx:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeConn:
        return self.conn

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self.conn)


@contextmanager
def _patch_shared_pool(pool: Optional[FakePool], *, raises: bool = False):
    """Patch ``get_shared_pool`` used inside the provider module."""
    async def _fake() -> FakePool:
        if raises:
            raise RuntimeError("pool unavailable")
        return pool  # type: ignore[return-value]

    with patch(
        "shared.dashboard.interpretation_drawer_provider.get_shared_pool",
        _fake,
    ):
        yield


# ---------------------------------------------------------------------------
# Row factory.
# ---------------------------------------------------------------------------


def _row(**overrides: Any) -> Dict[str, Any]:
    """Build a fully populated drawer row matching ``_select_clause()``."""
    base: Dict[str, Any] = {
        "id": 101,
        "news_item_id": 55,
        "media_item_id": 7,
        "trader_profile_id": 3,
        "consensus_direction": "long",
        "consensus_confidence": Decimal("0.82"),
        "consensus_method": "weighted_avg",
        "llm_direction": "long",
        "llm_confidence": Decimal("0.75"),
        "llm_reasoning": "trend up",
        "llm_levels": {"sl": "100", "tp": "120"},
        "quant_direction": "long",
        "quant_confidence": Decimal("0.90"),
        "quant_indicators": {"rsi": 55},
        "instrument_symbol": "BTCUSDT",
        "instrument_exchange": "bybit",
        "instrument_symbol_normalised": "btcusdt",
        "timeframe": "1h",
        "exchange": "bybit",
        "chart_analysis": {"pattern": "wedge"},
        "trader_trades": [{"side": "long"}],
        "chart_hacker_trades": [{"side": "long", "tp": 1}],
        "ai_agreement_score": Decimal("0.88"),
        "ai_comment": "looks aligned",
        "pattern_tags": ["wedge"],
        "setup_tags": ["breakout"],
        "regime_tags": ["bull"],
        "session_tags": ["us"],
        "trader_stated_thesis": "buy the dip",
        "llm_inferred_thesis": "trend continuation",
        "reason_agreement_score": Decimal("0.65"),
        "prompt_version": "v3",
        "model_version": "gpt-5-mini",
        "market_data_at": datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc),
        "market_data_fresh": True,
        "llm_cost_usd": Decimal("0.0021"),
        "quant_cost_usd": Decimal("0.0001"),
        "correlation_id": "abc-123",
        "created_at": datetime(2026, 5, 3, 12, 5, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 3, 12, 6, tzinfo=timezone.utc),
        "news_headline": "BTC breaks out",
        "news_source": "telegram",
        "news_collected_at": datetime(2026, 5, 3, 11, 55, tzinfo=timezone.utc),
        "news_published_at": datetime(2026, 5, 3, 11, 50, tzinfo=timezone.utc),
        "news_channel_name": "alpha-room",
        "news_author": "trader_bob",
        "media_local_path": "/m/7.png",
        "media_thumbnail_path": "/m/7_thumb.png",
        "media_source_url": "https://example.com/img/7",
        "media_type": "image",
        # --- enrichment fields added alongside Slice 1 §3.0 ---
        "instrument_resolved_from": "ocr",
        "prefilter_provider": "google",
        "prefilter_model": "gemini-2.5-flash",
        "prefilter_result": "actionable",
        "prefilter_cost_usd": Decimal("0.0001"),
        "vision_provider": "openai",
        "vision_model_requested": "gpt-5-mini",
        "vision_model_resolved": "gpt-5-mini",
        "news_content": "BTC just broke out",
        "news_metadata": {"foo": "bar"},
        "news_has_media": True,
        "news_media_count": 1,
        "media_id": 7,
        "media_mime_type": "image/png",
        "trader_handle_raw": "@alpha",
        "trader_handle_normalized": "alpha",
        "trader_display_name": "Alpha Bob",
        "trader_platform": "telegram",
        "trader_type": "scalper",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Constants & validators.
# ---------------------------------------------------------------------------


def test_constants_are_sane() -> None:
    assert DRAWER_PROVIDER_TIMEOUT_S == pytest.approx(0.25)
    assert DRAWER_DEFAULT_LIMIT == 10
    assert DRAWER_LIMIT_CAP == 25
    assert DRAWER_DEFAULT_LIMIT <= DRAWER_LIMIT_CAP


@pytest.mark.parametrize("value", [1, "1", "42", 999999])
def test_validate_id_accepts_positive_int_like(value: Any) -> None:
    assert _validate_id(value, label="x") == int(value)


@pytest.mark.parametrize("value", [0, -1, "0", "-7", "", None, "abc"])
def test_validate_id_rejects_invalid(value: Any) -> None:
    # Note: ``int(1.5)`` succeeds in Python (truncates to 1), so floats
    # are *not* rejected here — the validator only fences off non-numeric
    # strings, ``None``, empty strings, and non-positive integers.
    with pytest.raises(ValueError):
        _validate_id(value, label="x")


@pytest.mark.parametrize(
    "given,expected",
    [
        (None, DRAWER_DEFAULT_LIMIT),
        (0, 1),
        (-5, 1),
        (1, 1),
        (10, 10),
        (DRAWER_LIMIT_CAP, DRAWER_LIMIT_CAP),
        (DRAWER_LIMIT_CAP + 100, DRAWER_LIMIT_CAP),
        ("13", 13),
        ("not-a-number", DRAWER_DEFAULT_LIMIT),
    ],
)
def test_normalise_limit(given: Any, expected: int) -> None:
    assert _normalise_limit(given) == expected


# ---------------------------------------------------------------------------
# Helpers (_opt_*).
# ---------------------------------------------------------------------------


def test_opt_int_passthrough_and_none() -> None:
    assert _opt_int(None) is None
    assert _opt_int(7) == 7
    assert _opt_int("9") == 9
    assert _opt_int("not-num") is None


def test_opt_decimal_stringifies_and_passes_none() -> None:
    assert _opt_decimal(None) is None
    assert _opt_decimal(Decimal("0.250")) == "0.250"
    assert _opt_decimal(7) == "7"


def test_opt_iso_handles_datetime_and_none() -> None:
    ts = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
    assert _opt_iso(None) is None
    assert _opt_iso(ts) == ts.isoformat()
    # falls back to str() for objects without isoformat.
    assert _opt_iso("raw") == "raw"


# ---------------------------------------------------------------------------
# _run_with_budget.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_with_budget_returns_value_on_success() -> None:
    async def _ok() -> int:
        return 42

    out = await _run_with_budget(_ok, label="t", fallback=-1)
    assert out == 42


@pytest.mark.asyncio
async def test_run_with_budget_returns_fallback_on_timeout() -> None:
    async def _slow() -> int:
        await asyncio.sleep(DRAWER_PROVIDER_TIMEOUT_S * 4)
        return 1

    out = await _run_with_budget(_slow, label="t", fallback=[])
    assert out == []


@pytest.mark.asyncio
async def test_run_with_budget_returns_fallback_on_exception() -> None:
    async def _boom() -> int:
        raise RuntimeError("nope")

    out = await _run_with_budget(_boom, label="t", fallback=[])
    assert out == []


@pytest.mark.asyncio
async def test_run_with_budget_propagates_cancellation() -> None:
    async def _cancel() -> None:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _run_with_budget(_cancel, label="t", fallback=None)


# ---------------------------------------------------------------------------
# fetch_by_news_item — happy paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_by_news_item_returns_documented_shape() -> None:
    conn = FakeConn(rows=[_row()])
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        rows = await provider.fetch_by_news_item(55, limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == 101
    assert r["news_item_id"] == 55
    assert r["consensus_confidence"] == "0.82"
    assert r["llm"]["confidence"] == "0.75"
    assert r["llm"]["levels"] == {"sl": "100", "tp": "120"}
    assert r["quant"]["indicators"] == {"rsi": 55}
    assert r["instrument"]["symbol"] == "BTCUSDT"
    assert r["instrument"]["exchange"] == "bybit"
    assert r["instrument"]["timeframe"] == "1h"
    assert r["chart_hacker"]["ai_agreement_score"] == "0.88"
    assert r["chart_hacker"]["ai_comment"] == "looks aligned"
    assert r["chart_hacker"]["chart_analysis"] == {"pattern": "wedge"}
    assert r["chart_hacker"]["trader_trades"] == [{"side": "long"}]
    assert r["chart_hacker"]["chart_hacker_trades"] == [
        {"side": "long", "tp": 1}
    ]
    assert r["tags"]["pattern"] == ["wedge"]
    assert r["thesis"]["llm_inferred"] == "trend continuation"
    assert r["news"]["headline"] == "BTC breaks out"
    assert r["media"]["media_type"] == "image"
    assert r["created_at"].endswith("+00:00")
    assert r["market_data_fresh"] is True


@pytest.mark.asyncio
async def test_fetch_by_news_item_threads_id_and_limit_into_params() -> None:
    conn = FakeConn(rows=[_row()])
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        await provider.fetch_by_news_item(55, limit=7)
    assert len(conn.queries) == 1
    sql, args = conn.queries[0]
    assert "WHERE si.news_item_id = $1" in sql
    assert "LIMIT $2" in sql
    assert "ORDER BY si.created_at DESC, si.id DESC" in sql
    assert args == (55, 7)


@pytest.mark.asyncio
async def test_fetch_by_news_item_uses_default_limit_when_none() -> None:
    conn = FakeConn(rows=[])
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        await provider.fetch_by_news_item(55)
    _, args = conn.queries[0]
    assert args[1] == DRAWER_DEFAULT_LIMIT


@pytest.mark.asyncio
async def test_fetch_by_news_item_clamps_limit_to_cap() -> None:
    conn = FakeConn(rows=[])
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        await provider.fetch_by_news_item(55, limit=10_000)
    _, args = conn.queries[0]
    assert args[1] == DRAWER_LIMIT_CAP


@pytest.mark.asyncio
async def test_fetch_by_news_item_returns_empty_list_when_no_rows() -> None:
    conn = FakeConn(rows=[])
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        rows = await provider.fetch_by_news_item(55)
    assert rows == []


# ---------------------------------------------------------------------------
# fetch_by_news_item — failure modes (must never raise).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, "abc", None, ""])
async def test_fetch_by_news_item_rejects_invalid_id(bad: Any) -> None:
    provider = InterpretationDrawerProvider()
    rows = await provider.fetch_by_news_item(bad)  # type: ignore[arg-type]
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_by_news_item_returns_empty_on_pool_failure() -> None:
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(None, raises=True):
        rows = await provider.fetch_by_news_item(55)
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_by_news_item_returns_empty_on_query_failure() -> None:
    conn = FakeConn(rows=[], fail=True)
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        rows = await provider.fetch_by_news_item(55)
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_by_news_item_returns_empty_on_budget_timeout() -> None:
    conn = FakeConn(rows=[_row()], delay=DRAWER_PROVIDER_TIMEOUT_S * 5)
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        rows = await provider.fetch_by_news_item(55)
    assert rows == []


# ---------------------------------------------------------------------------
# fetch_by_id.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_by_id_returns_single_row() -> None:
    conn = FakeConn(rows=[_row(id=909)])
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        rows = await provider.fetch_by_id(909)
    assert len(rows) == 1
    assert rows[0]["id"] == 909


@pytest.mark.asyncio
async def test_fetch_by_id_threads_id_and_uses_limit_one() -> None:
    conn = FakeConn(rows=[_row()])
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        await provider.fetch_by_id(909)
    sql, args = conn.queries[0]
    assert "WHERE si.id = $1" in sql
    assert "LIMIT 1" in sql
    assert args == (909,)


@pytest.mark.asyncio
async def test_fetch_by_id_returns_empty_on_miss() -> None:
    conn = FakeConn(rows=[])
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        rows = await provider.fetch_by_id(909)
    assert rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, "abc", None])
async def test_fetch_by_id_rejects_invalid_id(bad: Any) -> None:
    provider = InterpretationDrawerProvider()
    rows = await provider.fetch_by_id(bad)  # type: ignore[arg-type]
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_by_id_returns_empty_on_query_failure() -> None:
    conn = FakeConn(rows=[], fail=True)
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        rows = await provider.fetch_by_id(909)
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_by_id_returns_empty_on_pool_failure() -> None:
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(None, raises=True):
        rows = await provider.fetch_by_id(909)
    assert rows == []


# ---------------------------------------------------------------------------
# Edge cases on _row_to_dict.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_to_dict_handles_all_null_optional_fields() -> None:
    null_fields = {
        "media_item_id": None,
        "trader_profile_id": None,
        "consensus_confidence": None,
        "llm_confidence": None,
        "llm_reasoning": None,
        "llm_levels": None,
        "quant_indicators": None,
        "ai_agreement_score": None,
        "ai_comment": None,
        "pattern_tags": None,
        "setup_tags": None,
        "regime_tags": None,
        "session_tags": None,
        "trader_stated_thesis": None,
        "llm_inferred_thesis": None,
        "reason_agreement_score": None,
        "market_data_at": None,
        "market_data_fresh": None,
        "llm_cost_usd": None,
        "quant_cost_usd": None,
        "media_local_path": None,
        "media_thumbnail_path": None,
        "media_source_url": None,
        "media_type": None,
        "news_headline": None,
        "news_source": None,
        "news_collected_at": None,
        "news_published_at": None,
        "news_channel_name": None,
        "news_author": None,
        "chart_analysis": None,
        "trader_trades": None,
        "chart_hacker_trades": None,
        "instrument_exchange": None,
        "exchange": None,
    }
    conn = FakeConn(rows=[_row(**null_fields)])
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        rows = await provider.fetch_by_news_item(55)
    r = rows[0]
    assert r["consensus_confidence"] is None
    assert r["llm"]["confidence"] is None
    assert r["llm"]["levels"] is None
    assert r["chart_hacker"]["ai_agreement_score"] is None
    assert r["instrument"]["exchange"] is None
    assert r["market_data_fresh"] is None
    assert r["news"]["headline"] is None
    assert r["media"]["media_type"] is None


@pytest.mark.asyncio
async def test_row_to_dict_prefers_instrument_exchange_over_exchange() -> None:
    conn = FakeConn(rows=[_row(instrument_exchange="okx", exchange="bybit")])
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        rows = await provider.fetch_by_news_item(55)
    assert rows[0]["instrument"]["exchange"] == "okx"


@pytest.mark.asyncio
async def test_row_to_dict_falls_back_to_exchange_when_instrument_null() -> None:
    conn = FakeConn(rows=[_row(instrument_exchange=None, exchange="bybit")])
    pool = FakePool(conn)
    provider = InterpretationDrawerProvider()
    with _patch_shared_pool(pool):
        rows = await provider.fetch_by_news_item(55)
    assert rows[0]["instrument"]["exchange"] == "bybit"


# ---------------------------------------------------------------------------
# Query-builder smoke tests.
# ---------------------------------------------------------------------------


def test_select_clause_joins_news_and_media() -> None:
    sql = InterpretationDrawerProvider._select_clause()
    # collapse runs of whitespace so the assertion is alignment-tolerant.
    flat = " ".join(sql.split())
    assert "FROM signal_interpretations si" in flat
    assert "LEFT JOIN news_items ni ON ni.id = si.news_item_id" in flat
    assert "LEFT JOIN media_items mi ON mi.id = si.media_item_id" in flat
    assert (
        "LEFT JOIN trader_profiles tp ON tp.id = si.trader_profile_id" in flat
    )
    # canonical aliases the _row_to_dict relies on
    for alias in (
        "news_headline",
        "news_content",
        "news_source",
        "news_channel_name",
        "news_author",
        "news_collected_at",
        "news_published_at",
        "news_metadata",
        "news_has_media",
        "news_media_count",
        "media_id",
        "media_local_path",
        "media_thumbnail_path",
        "media_source_url",
        "media_type",
        "media_mime_type",
        "trader_handle_raw",
        "trader_handle_normalized",
        "trader_display_name",
        "trader_platform",
        "trader_type",
    ):
        assert alias in sql, f"alias {alias!r} missing from _select_clause"


def test_build_query_by_news_item_uses_two_positional_params() -> None:
    sql = InterpretationDrawerProvider()._build_query_by_news_item()
    assert "$1" in sql and "$2" in sql
    assert "$3" not in sql
    assert "WHERE si.news_item_id = $1" in sql
    assert sql.rstrip().endswith("LIMIT $2")


def test_build_query_by_id_uses_single_positional_param() -> None:
    sql = InterpretationDrawerProvider()._build_query_by_id()
    assert "$1" in sql
    assert "$2" not in sql
    assert "WHERE si.id = $1" in sql
    assert sql.rstrip().endswith("LIMIT 1")
