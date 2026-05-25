"""
Module: test_routing
Purpose: Smoke + unit tests for shared/mcp/tools/routing.py (Round 13).
Location: /opt/tickles/shared/mcp/tools/test_routing.py

These tests cover:
  * The pure helper ``_routed_to_dict`` (no I/O, deterministic).
  * Argument validation for every handler (no DB / network).
  * Registry plumbing — every Round 13 tool ends up registered with
    the correct ``read_only`` flag and tag set.
  * Mocked-DB happy paths for ``router.distribution`` and
    ``instruments.search`` (no live Postgres needed).
  * A live integration test (skipped if Postgres is unavailable) that
    runs ``router.resolve`` against the real ``unified_instruments``
    rows.

Run:
    python -m pytest shared/mcp/tools/test_routing.py -v
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.mcp.protocol import McpTool
from shared.mcp.registry import ToolRegistry
from shared.mcp.tools.context import ToolContext
from shared.mcp.tools.routing import (
    _build_tools,
    _handle_instruments_search,
    _handle_positions_cancel,
    _handle_positions_diagnose,
    _handle_positions_reconcile,
    _handle_router_clear_cache,
    _handle_router_distribution,
    _handle_router_resolve,
    _routed_to_dict,
    register,
)


# ---------------------------------------------------------------------------
# A tiny fake RoutedMarket dataclass so we can exercise _routed_to_dict
# without dragging in the real one.
# ---------------------------------------------------------------------------
@dataclass
class FakeRouted:
    supported: bool = True
    exchange: str = "bybit"
    exchange_symbol: str = "BTCUSDT"
    asset_class: str = "crypto"
    epic_code: Optional[str] = None
    ccxt_perp_symbol: str = "BTC/USDT:USDT"
    canonical_symbol: str = "BTC/USDT:USDT"
    unsupported_reason: Optional[str] = None
    raw_input: str = "BTC"


# ---------------------------------------------------------------------------
# A minimal fake DatabasePool that records calls and returns canned rows.
# ---------------------------------------------------------------------------
class FakePool:
    """Tracks (query, params) tuples and returns scripted responses."""

    def __init__(
        self,
        fetch_all_rows: Optional[List[Dict[str, Any]]] = None,
        fetch_one_row: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.fetch_all_rows = fetch_all_rows or []
        self.fetch_one_row = fetch_one_row
        self.fetch_all_calls: List[Tuple[str, Tuple[Any, ...]]] = []
        self.fetch_one_calls: List[Tuple[str, Tuple[Any, ...]]] = []
        self.execute_calls: List[Tuple[str, Tuple[Any, ...]]] = []
        # For multi-stage diagnose tests, allow scripted multiple fetch_one
        self._fetch_one_queue: List[Optional[Dict[str, Any]]] = []

    def queue_fetch_one(self, *rows: Optional[Dict[str, Any]]) -> None:
        self._fetch_one_queue.extend(rows)

    async def fetch_all(self, query: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
        self.fetch_all_calls.append((query, params))
        return list(self.fetch_all_rows)

    async def fetch_one(self, query: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
        self.fetch_one_calls.append((query, params))
        if self._fetch_one_queue:
            return self._fetch_one_queue.pop(0)
        return self.fetch_one_row

    async def execute(self, query: str, params: Tuple[Any, ...] = ()) -> int:
        self.execute_calls.append((query, params))
        return 1


# ===========================================================================
# Pure helper tests
# ===========================================================================
class TestRoutedToDict:
    """Tests for the _routed_to_dict serialiser including display_symbol."""

    def test_perp_canonical_yields_dotP(self) -> None:
        out = _routed_to_dict(FakeRouted(canonical_symbol="BTC/USDT:USDT"))
        assert out["display_symbol"] == "BTCUSDT.P"
        assert out["canonical_symbol"] == "BTC/USDT:USDT"
        assert out["supported"] is True

    def test_spot_canonical_yields_no_suffix(self) -> None:
        out = _routed_to_dict(
            FakeRouted(canonical_symbol="ETH/USDT", ccxt_perp_symbol="ETH/USDT:USDT")
        )
        assert out["display_symbol"] == "ETHUSDT"

    def test_unsupported_passthrough(self) -> None:
        out = _routed_to_dict(
            FakeRouted(
                supported=False,
                canonical_symbol="USDT.D",
                unsupported_reason="aggregate_index",
            )
        )
        assert out["supported"] is False
        assert out["unsupported_reason"] == "aggregate_index"
        # display_symbol falls back to canonical when not parseable
        assert out["display_symbol"] == "USDT.D"

    def test_kasusdt_perp(self) -> None:
        out = _routed_to_dict(FakeRouted(canonical_symbol="KAS/USDT:USDT"))
        assert out["display_symbol"] == "KASUSDT.P"

    def test_xau_perp(self) -> None:
        out = _routed_to_dict(FakeRouted(canonical_symbol="XAU/USDT:USDT"))
        assert out["display_symbol"] == "XAUUSDT.P"


# ===========================================================================
# Registry plumbing
# ===========================================================================
class TestRegistry:
    """Verify all 7 Round 13 tools are registered with correct metadata."""

    def test_register_adds_seven_tools(self) -> None:
        reg = ToolRegistry()
        register(reg, ToolContext())
        names = [t.name for t in reg.list_tools()]
        for expected in (
            "router.resolve",
            "router.distribution",
            "router.clear_cache",
            "instruments.search",
            "positions.diagnose",
            "positions.cancel",
            "positions.reconcile",
        ):
            assert expected in names, f"{expected} not registered"

    def test_read_only_flags(self) -> None:
        ctx = ToolContext()
        tools = {t.name: t for t, _ in _build_tools(ctx)}
        assert tools["router.resolve"].read_only is True
        assert tools["router.distribution"].read_only is True
        assert tools["instruments.search"].read_only is True
        assert tools["positions.diagnose"].read_only is True
        # Mutating tools
        assert tools["router.clear_cache"].read_only is False
        assert tools["positions.cancel"].read_only is False
        assert tools["positions.reconcile"].read_only is False

    def test_tags_are_round_13(self) -> None:
        ctx = ToolContext()
        for tool, _ in _build_tools(ctx):
            assert tool.tags.get("phase") == "13"
            assert tool.tags.get("group") == "routing"

    def test_required_input_schema(self) -> None:
        ctx = ToolContext()
        tools = {t.name: t for t, _ in _build_tools(ctx)}
        assert "symbol" in tools["router.resolve"].input_schema["required"]
        assert "positionId" in tools["positions.diagnose"].input_schema["required"]
        assert {"positionId", "reason"} <= set(
            tools["positions.cancel"].input_schema["required"]
        )


# ===========================================================================
# router.resolve — argument validation + happy path (mocked)
# ===========================================================================
class TestRouterResolve:
    """Tests for _handle_router_resolve."""

    def test_missing_symbol(self) -> None:
        result = asyncio.run(_handle_router_resolve({}))
        assert result["ok"] is False
        assert "required" in result["error"]

    def test_blank_symbol(self) -> None:
        result = asyncio.run(_handle_router_resolve({"symbol": "   "}))
        assert result["ok"] is False

    def test_happy_path_mocked(self) -> None:
        with patch(
            "shared.utils.exchange_router.resolve_market",
            new_callable=AsyncMock,
            return_value=FakeRouted(),
        ):
            result = asyncio.run(_handle_router_resolve({"symbol": "BTC"}))
        assert result["ok"] is True
        assert result["exchange"] == "bybit"
        assert result["display_symbol"] == "BTCUSDT.P"
        assert result["canonical_symbol"] == "BTC/USDT:USDT"

    def test_unsupported_path_mocked(self) -> None:
        with patch(
            "shared.utils.exchange_router.resolve_market",
            new_callable=AsyncMock,
            return_value=FakeRouted(
                supported=False,
                canonical_symbol="USDT.D",
                unsupported_reason="aggregate_index",
            ),
        ):
            result = asyncio.run(_handle_router_resolve({"symbol": "USDT.D"}))
        assert result["ok"] is True
        assert result["supported"] is False
        assert result["unsupported_reason"] == "aggregate_index"

    def test_router_exception_returned(self) -> None:
        with patch(
            "shared.utils.exchange_router.resolve_market",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ):
            result = asyncio.run(_handle_router_resolve({"symbol": "BTC"}))
        assert result["ok"] is False
        assert "boom" in result["error"]


# ===========================================================================
# router.distribution — mocked DB
# ===========================================================================
class TestRouterDistribution:
    """Tests for _handle_router_distribution."""

    def test_groups_and_totals(self) -> None:
        fake = FakePool(
            fetch_all_rows=[
                {"instrument_exchange": "bybit", "status": "open", "n": 5},
                {"instrument_exchange": "bybit", "status": "pending", "n": 2},
                {"instrument_exchange": "capital.com", "status": "cancelled", "n": 3},
            ]
        )
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            result = asyncio.run(_handle_router_distribution({"windowDays": 7}))
        assert result["ok"] is True
        assert result["totals"]["per_exchange"] == {"bybit": 7, "capital.com": 3}
        assert result["totals"]["per_status"] == {"open": 5, "pending": 2, "cancelled": 3}
        assert result["totals"]["grand_total"] == 10

    def test_zero_window_means_all_time(self) -> None:
        fake = FakePool(fetch_all_rows=[])
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            result = asyncio.run(_handle_router_distribution({"windowDays": 0}))
        assert result["ok"] is True
        # No INTERVAL clause when window=0
        query, _ = fake.fetch_all_calls[0]
        assert "INTERVAL" not in query

    def test_company_filter_applied(self) -> None:
        fake = FakePool(fetch_all_rows=[])
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            result = asyncio.run(
                _handle_router_distribution({"company": "jarvais", "windowDays": 30})
            )
        assert result["ok"] is True
        query, params = fake.fetch_all_calls[0]
        assert "company_id" in query
        assert params == ("jarvais",)


# ===========================================================================
# router.clear_cache — pure
# ===========================================================================
class TestRouterClearCache:
    """Tests for _handle_router_clear_cache."""

    def test_clears_returns_count(self) -> None:
        # Seed the real cache, then call the handler
        from shared.utils.exchange_router import _CACHE
        _CACHE.clear()
        _CACHE["sentinel"] = (0.0, FakeRouted())  # type: ignore[assignment]
        result = asyncio.run(_handle_router_clear_cache({}))
        assert result["ok"] is True
        assert result["entries_dropped"] >= 1
        # Must be empty after
        assert len(_CACHE) == 0


# ===========================================================================
# instruments.search — mocked DB
# ===========================================================================
class TestInstrumentsSearch:
    """Tests for _handle_instruments_search."""

    def test_default_filters_active_only(self) -> None:
        fake = FakePool(fetch_all_rows=[
            {
                "exchange": "bybit",
                "exchange_symbol": "BTCUSDT",
                "canonical_symbol": "BTC/USDT:USDT",
                "asset_type": "crypto",
                "is_active": True,
            }
        ])
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            result = asyncio.run(_handle_instruments_search({"query": "BTC"}))
        assert result["ok"] is True
        assert result["count"] == 1
        assert result["rows"][0]["canonical_symbol"] == "BTC/USDT:USDT"
        assert result["filters"]["active_only"] is True
        # Verify is_active = TRUE landed in WHERE
        query, _ = fake.fetch_all_calls[0]
        assert "is_active = TRUE" in query

    def test_exchange_and_asset_type_filters(self) -> None:
        fake = FakePool(fetch_all_rows=[])
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            asyncio.run(
                _handle_instruments_search(
                    {"exchange": "Bybit", "assetType": "Crypto", "query": "ETH"}
                )
            )
        _, params = fake.fetch_all_calls[0]
        # query, exchange, asset_type, limit
        assert params[0] == "%ETH%"
        assert params[1] == "bybit"
        assert params[2] == "crypto"

    def test_limit_capped(self) -> None:
        fake = FakePool(fetch_all_rows=[])
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            result = asyncio.run(_handle_instruments_search({"limit": 99999}))
        assert result["limit"] == 500


# ===========================================================================
# positions.diagnose — argument validation + mocked happy path
# ===========================================================================
class TestPositionsDiagnose:
    """Tests for _handle_positions_diagnose."""

    def test_missing_id(self) -> None:
        result = asyncio.run(_handle_positions_diagnose({}))
        assert result["ok"] is False
        assert "positionId" in result["error"]

    def test_zero_id(self) -> None:
        result = asyncio.run(_handle_positions_diagnose({"positionId": 0}))
        assert result["ok"] is False

    def test_non_integer(self) -> None:
        result = asyncio.run(_handle_positions_diagnose({"positionId": "abc"}))
        assert result["ok"] is False
        assert "integer" in result["error"]

    def test_not_found(self) -> None:
        fake = FakePool()
        fake.queue_fetch_one(None)  # main row absent
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            result = asyncio.run(_handle_positions_diagnose({"positionId": 999999}))
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_happy_path(self) -> None:
        # Main row + last_update row + router resolve_market
        main_row = {
            "id": 42,
            "instrument_symbol": "BTC/USDT",
            "instrument_exchange": "bybit",
            "instrument_symbol_normalised": "BTC/USDT",
            "epic_code": None,
            "direction": "long",
            "entry_price": "50000",
            "stop_loss": "48000",
            "take_profit_1": "52000",
            "take_profit_2": None,
            "take_profit_3": None,
            "status": "pending",
            "status_reason": None,
            "signal_source": "discord",
            "actor_type": "trader",
            "actor_id": "1",
            "trader_profile_id": 1,
            "news_item_id": None,
            "media_item_id": None,
            "signal_interpretation_id": None,
            "correlation_id": "abc",
            "current_price": None,
            "unrealized_pnl_pct": None,
            "unrealized_pnl_usd": None,
            "distance_to_entry_pct": None,
            "distance_to_sl_pct": None,
            "distance_to_tp1_pct": None,
            "time_in_trade_minutes": None,
            "created_at": None,
            "updated_at": None,
            "signal_timestamp": None,
            "activated_at": None,
            "closed_at": None,
            "closed_price": None,
            "exit_reason": None,
            "realized_pnl_usd": None,
            "realized_pnl_pct": None,
            "outcome": None,
        }
        update_row = {
            "id": 1,
            "position_id": 42,
            "price": "50100",
            "unrealized_pnl_pct": "0.2",
            "unrealized_pnl_usd": "10",
            "distance_to_entry_pct": "0.2",
            "distance_to_sl_pct": "4.0",
            "distance_to_tp1_pct": "3.8",
            "time_in_trade_minutes": 30,
            "update_source": "candle_poll",
            "timestamp": None,
            "created_at": None,
        }

        fake = FakePool()
        fake.queue_fetch_one(main_row, update_row)
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ), patch(
            "shared.utils.exchange_router.resolve_market",
            new_callable=AsyncMock,
            return_value=FakeRouted(
                canonical_symbol="BTC/USDT:USDT",  # router upgraded perp form
                exchange="bybit",
            ),
        ):
            result = asyncio.run(_handle_positions_diagnose({"positionId": 42}))
        assert result["ok"] is True
        assert result["position"]["id"] == 42
        assert result["last_update"]["price"] == 50100.0
        assert result["router_says"]["display_symbol"] == "BTCUSDT.P"
        # Drift: stored is BTC/USDT (spot canonical), router says perp form
        assert result["drift"]["symbol_changes"] is True
        assert result["drift"]["exchange_changes"] is False


# ===========================================================================
# positions.cancel — argument validation + state machine
# ===========================================================================
class TestPositionsCancel:
    """Tests for _handle_positions_cancel."""

    def test_missing_id(self) -> None:
        result = asyncio.run(_handle_positions_cancel({"reason": "x"}))
        assert result["ok"] is False

    def test_missing_reason(self) -> None:
        result = asyncio.run(_handle_positions_cancel({"positionId": 1}))
        assert result["ok"] is False

    def test_blank_reason(self) -> None:
        result = asyncio.run(
            _handle_positions_cancel({"positionId": 1, "reason": "  "})
        )
        assert result["ok"] is False

    def test_long_reason(self) -> None:
        result = asyncio.run(
            _handle_positions_cancel({"positionId": 1, "reason": "x" * 201})
        )
        assert result["ok"] is False

    def test_position_not_found(self) -> None:
        fake = FakePool(fetch_one_row=None)
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            result = asyncio.run(
                _handle_positions_cancel({"positionId": 999, "reason": "test"})
            )
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_already_cancelled_is_noop(self) -> None:
        fake = FakePool(fetch_one_row={"status": "cancelled"})
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            result = asyncio.run(
                _handle_positions_cancel({"positionId": 1, "reason": "test"})
            )
        assert result["ok"] is True
        assert result.get("no_op") is True

    def test_already_closed_refused(self) -> None:
        fake = FakePool(fetch_one_row={"status": "closed"})
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            result = asyncio.run(
                _handle_positions_cancel({"positionId": 1, "reason": "test"})
            )
        assert result["ok"] is False
        assert "closed" in result["error"]

    def test_open_blocked_without_allow_open(self) -> None:
        fake = FakePool(fetch_one_row={"status": "open"})
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            result = asyncio.run(
                _handle_positions_cancel({"positionId": 1, "reason": "test"})
            )
        assert result["ok"] is False
        assert "allowOpen" in result["error"]

    def test_open_allowed_with_flag(self) -> None:
        fake = FakePool(fetch_one_row={"status": "open"})
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            result = asyncio.run(
                _handle_positions_cancel(
                    {"positionId": 1, "reason": "operator", "allowOpen": True}
                )
            )
        assert result["ok"] is True
        assert result["new_status"] == "cancelled"
        assert "manual:operator" in result["new_status_reason"]
        # UPDATE was issued
        assert len(fake.execute_calls) == 1

    def test_pending_cancelled_normally(self) -> None:
        fake = FakePool(fetch_one_row={"status": "pending"})
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ):
            result = asyncio.run(
                _handle_positions_cancel({"positionId": 1, "reason": "junk symbol"})
            )
        assert result["ok"] is True
        assert result["new_status"] == "cancelled"
        assert "manual:junk symbol" in result["new_status_reason"]


# ===========================================================================
# positions.reconcile — dry-run safety + mocked happy path
# ===========================================================================
class TestPositionsReconcile:
    """Tests for _handle_positions_reconcile."""

    def test_default_is_dry_run(self) -> None:
        fake = FakePool()
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ), patch(
            "shared.scripts.round13_reconcile_routing.fetch_target_rows",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = asyncio.run(_handle_positions_reconcile({}))
        assert result["ok"] is True
        assert result["apply"] is False
        assert result["include_open"] is False

    def test_no_rows_returns_empty_summary(self) -> None:
        fake = FakePool()
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ), patch(
            "shared.scripts.round13_reconcile_routing.fetch_target_rows",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = asyncio.run(_handle_positions_reconcile({"limit": 10}))
        assert result["ok"] is True
        assert result["scanned"] == 0
        assert result["actions_total"] == 0

    def test_dry_run_does_not_execute(self) -> None:
        fake = FakePool()
        # One pending row that will get re-routed (different exchange)
        rows = [
            {
                "id": 1,
                "instrument_symbol": "GOLD",
                "instrument_exchange": "capital.com",
                "instrument_symbol_normalised": "GOLD",
                "epic_code": "GOLD",
                "status": "pending",
                "status_reason": None,
                "direction": "long",
                "news_item_id": None,
                "trader_profile_id": 1,
                "created_at": None,
            }
        ]
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ), patch(
            "shared.scripts.round13_reconcile_routing.fetch_target_rows",
            new_callable=AsyncMock,
            return_value=rows,
        ), patch(
            "shared.utils.exchange_router.resolve_market",
            new_callable=AsyncMock,
            return_value=FakeRouted(
                exchange="bybit",
                exchange_symbol="XAUUSDT",
                canonical_symbol="XAU/USDT:USDT",
                supported=True,
            ),
        ):
            result = asyncio.run(_handle_positions_reconcile({"apply": False}))
        assert result["ok"] is True
        assert result["scanned"] == 1
        # Action recorded
        assert result["actions_total"] == 1
        # No UPDATE issued in dry-run
        assert len(fake.execute_calls) == 0

    def test_apply_issues_update(self) -> None:
        fake = FakePool()
        rows = [
            {
                "id": 5,
                "instrument_symbol": "GOLD",
                "instrument_exchange": "capital.com",
                "instrument_symbol_normalised": "GOLD",
                "epic_code": "GOLD",
                "status": "pending",
                "status_reason": None,
                "direction": "long",
                "news_item_id": None,
                "trader_profile_id": 1,
                "created_at": None,
            }
        ]
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ), patch(
            "shared.scripts.round13_reconcile_routing.fetch_target_rows",
            new_callable=AsyncMock,
            return_value=rows,
        ), patch(
            "shared.utils.exchange_router.resolve_market",
            new_callable=AsyncMock,
            return_value=FakeRouted(
                exchange="bybit",
                exchange_symbol="XAUUSDT",
                canonical_symbol="XAU/USDT:USDT",
                supported=True,
            ),
        ):
            result = asyncio.run(_handle_positions_reconcile({"apply": True}))
        assert result["ok"] is True
        assert len(fake.execute_calls) == 1
        # First UPDATE was the "rerouted" path, params should include new_ex=bybit
        _, params = fake.execute_calls[0]
        assert params[0] == "bybit"
        assert params[2] == "XAU/USDT:USDT"
        assert params[4] == 5

    def test_unsupported_pending_cancelled_when_apply(self) -> None:
        fake = FakePool()
        rows = [
            {
                "id": 7,
                "instrument_symbol": "USDT.D",
                "instrument_exchange": "bybit",
                "instrument_symbol_normalised": "USDT.D",
                "epic_code": None,
                "status": "pending",
                "status_reason": None,
                "direction": "long",
                "news_item_id": None,
                "trader_profile_id": 1,
                "created_at": None,
            }
        ]
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ), patch(
            "shared.scripts.round13_reconcile_routing.fetch_target_rows",
            new_callable=AsyncMock,
            return_value=rows,
        ), patch(
            "shared.utils.exchange_router.resolve_market",
            new_callable=AsyncMock,
            return_value=FakeRouted(
                supported=False,
                canonical_symbol="USDT.D",
                unsupported_reason="aggregate_index",
            ),
        ):
            result = asyncio.run(_handle_positions_reconcile({"apply": True}))
        assert result["ok"] is True
        # Verify a cancel UPDATE was issued
        assert len(fake.execute_calls) == 1
        update_query, _ = fake.execute_calls[0]
        assert "status = 'cancelled'" in update_query

    def test_unsupported_open_skipped(self) -> None:
        fake = FakePool()
        rows = [
            {
                "id": 9,
                "instrument_symbol": "USDT.D",
                "instrument_exchange": "bybit",
                "instrument_symbol_normalised": "USDT.D",
                "epic_code": None,
                "status": "open",  # never auto-cancel open rows
                "status_reason": None,
                "direction": "long",
                "news_item_id": None,
                "trader_profile_id": 1,
                "created_at": None,
            }
        ]
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ), patch(
            "shared.scripts.round13_reconcile_routing.fetch_target_rows",
            new_callable=AsyncMock,
            return_value=rows,
        ), patch(
            "shared.utils.exchange_router.resolve_market",
            new_callable=AsyncMock,
            return_value=FakeRouted(
                supported=False,
                canonical_symbol="USDT.D",
                unsupported_reason="aggregate_index",
            ),
        ):
            result = asyncio.run(
                _handle_positions_reconcile({"apply": True, "includeOpen": True})
            )
        assert result["ok"] is True
        # Open row skipped, no UPDATE issued
        assert len(fake.execute_calls) == 0
        keys = [a["action"] for a in result["actions"]]
        assert keys == ["skip_open_unsupported"]

    def test_limit_capped_at_10000(self) -> None:
        fake = FakePool()
        with patch(
            "shared.mcp.tools.routing._get_pool",
            new_callable=AsyncMock,
            return_value=fake,
        ), patch(
            "shared.scripts.round13_reconcile_routing.fetch_target_rows",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_fetch:
            asyncio.run(_handle_positions_reconcile({"limit": 99999}))
            # The script's fetch was called with limit=10000
            _, kwargs = mock_fetch.call_args
            assert kwargs["limit"] == 10000


# ===========================================================================
# Live integration — skipped if Postgres unavailable
# ===========================================================================
@pytest.mark.asyncio
async def test_live_router_resolve_btc() -> None:
    """If Postgres is reachable, BTC should resolve to bybit perp form."""
    try:
        from shared.utils.db import DatabasePool
        await DatabasePool.get_instance()
    except Exception as exc:
        pytest.skip(f"Postgres unavailable for live test: {exc}")

    from shared.utils.exchange_router import clear_cache

    clear_cache()
    result = await _handle_router_resolve({"symbol": "BTC"})
    assert result["ok"] is True
    if result["supported"]:
        assert "USDT" in (result["canonical_symbol"] or "")
        assert result["display_symbol"].endswith(".P") or result["display_symbol"].endswith("USDT")
