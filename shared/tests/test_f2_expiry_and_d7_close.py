"""
Module: test_f2_expiry_and_d7_close
Purpose: Smoke tests for F2 — expiry-based auto-closer and D7-compliant
         fee math on every close path in PositionMonitor._process_one.
Location: /opt/tickles/shared/tests/test_f2_expiry_and_d7_close.py
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from shared.intelligence import position_monitor as pm
from shared.intelligence.fee_calc import RealizedPnlBreakdown
from shared.intelligence.position_monitor import (
    PositionMonitor,
    PositionSnapshot,
    update_position_outcome,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _FakePool:
    """Minimal in-memory replacement for the asyncpg pool wrapper.

    Records every SQL call so tests can assert exactly what was written.
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, Tuple[Any, ...]]] = []

    async def execute(self, sql: str, params: Tuple[Any, ...]) -> int:
        self.calls.append((sql, params))
        return 1

    async def fetch_one(
        self, sql: str, params: Tuple[Any, ...]
    ) -> Optional[Dict[str, Any]]:
        self.calls.append((sql, params))
        return None

    async def fetch_all(
        self, sql: str, params: Tuple[Any, ...]
    ) -> List[Dict[str, Any]]:
        self.calls.append((sql, params))
        return []


def _make_breakdown(net: str = "12.34000000") -> RealizedPnlBreakdown:
    """Return a minimal RealizedPnlBreakdown stub."""
    return RealizedPnlBreakdown(
        direction="long",
        entry_price=Decimal("78000.00000000"),
        exit_price=Decimal("80000.00000000"),
        qty=Decimal("0.001"),
        contract_multiplier=Decimal("1"),
        leverage=Decimal("1"),
        notional_usd_entry=Decimal("78.00000000"),
        notional_usd_exit=Decimal("80.00000000"),
        gross_pnl_usd=Decimal("15.00000000"),
        entry_fee_usd=Decimal("1.00000000"),
        exit_fee_usd=Decimal("1.00000000"),
        spread_cost_usd=Decimal("0.50000000"),
        funding_cost_usd=Decimal("0.16000000"),
        days_held=Decimal("1"),
        net_pnl_usd=Decimal(net),
    )


def _make_snapshot(
    sl_hit: bool = False,
    tp_hit: bool = False,
) -> PositionSnapshot:
    """Build a PositionSnapshot with the SL/TP flags under test."""
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    return PositionSnapshot(
        position_id=1,
        current_price=80000.0,
        unrealized_pnl=15.0,
        pnl_pct=1.5,
        distance_to_entry_pct=2.0,
        distance_to_sl=None,
        distance_to_sl_pct=None,
        distance_to_tp=None,
        distance_to_tp_pct=None,
        hours_open=24.0,
        mae_pct=0.0,
        mfe_pct=2.0,
        rr_ratio=None,
        sl_hit=sl_hit,
        tp_hit=tp_hit,
        snapshot_time=now,
    )


def _make_position(
    expiry_at: Optional[datetime] = None,
    direction: str = "long",
) -> Dict[str, Any]:
    """Build a minimal tracked_positions row dict."""
    opened_at = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
    return {
        "id": 1,
        "trader_profile_id": 7,
        "signal_interpretation_id": 11,
        "instrument_symbol": "BTC/USDT",
        "epic_code": None,
        "instrument_exchange": "bybit",
        "direction": direction,
        "entry_price": 78000.0,
        "position_size": 0.001,
        "leverage": 1,
        "stop_loss": 77000.0,
        "take_profit_1": 80000.0,
        "status": "open",
        "created_at": opened_at,
        "updated_at": opened_at,
        "lowest_price": None,
        "highest_price": None,
        "expiry_at": expiry_at,
        "signal_timestamp": opened_at,
    }


# ---------------------------------------------------------------------------
# update_position_outcome SQL contract
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_update_position_outcome_writes_both_pnl_columns():
    """When realized_pnl_final is given, both pnl columns are updated."""
    pool = _FakePool()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    await update_position_outcome(
        pool,
        position_id=42,
        status="closed",
        outcome="tp1_hit",
        exit_price=80000.0,
        realized_pnl=12.34,
        now=now,
        realized_pnl_final=12.34,
    )
    assert len(pool.calls) == 1
    sql, params = pool.calls[0]
    assert "realized_pnl_usd = $4" in sql
    assert "realized_pnl_usd_final = $5" in sql
    assert "closed_at = $6" in sql
    assert params == ("closed", "tp1_hit", 80000.0, 12.34, 12.34, now, 42)


@pytest.mark.asyncio
async def test_update_position_outcome_omits_final_when_none():
    """Without realized_pnl_final, the final column is left untouched."""
    pool = _FakePool()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    await update_position_outcome(
        pool,
        position_id=99,
        status="closed",
        outcome="manual_close",
        exit_price=80000.0,
        realized_pnl=5.0,
        now=now,
    )
    sql, params = pool.calls[0]
    assert "realized_pnl_usd_final" not in sql
    assert "realized_pnl_usd = $4" in sql
    assert "closed_at = $5" in sql
    assert params == ("closed", "manual_close", 80000.0, 5.0, now, 99)


# ---------------------------------------------------------------------------
# fetch_open_positions SQL contract
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_open_positions_uses_partial_exit_and_includes_expiry():
    """Status filter must use 'partial_exit' (not 'partial_close')."""
    pool = _FakePool()
    await pm.fetch_open_positions(pool, batch_size=50)
    assert len(pool.calls) == 1
    sql, params = pool.calls[0]
    assert "'partial_exit'" in sql
    assert "'partial_close'" not in sql
    assert "expiry_at" in sql
    assert "signal_timestamp" in sql
    assert params == (50,)


# ---------------------------------------------------------------------------
# _process_one branches
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_expired_position_settles_via_expired_branch(caplog):
    """expiry_at past now → outcome 'expired', net_pnl from D7 breakdown."""
    monitor = PositionMonitor()
    pool = _FakePool()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    expired = now - timedelta(hours=1)
    position = _make_position(expiry_at=expired)
    snapshot = _make_snapshot()  # no SL/TP hit — expiry should still fire
    breakdown = _make_breakdown(net="9.99000000")

    with patch.object(pm, "fetch_latest_price", return_value=80000.0), \
         patch.object(pm, "_build_snapshot", return_value=snapshot), \
         patch.object(pm, "write_position_update", return_value=101), \
         patch.object(pm, "update_position_extremes", return_value=1), \
         patch.object(pm, "_resolve_instrument_id", return_value=55), \
         patch.object(pm, "compute_realized_pnl_db", return_value=breakdown):
        with caplog.at_level(logging.INFO, logger="tickles.intelligence.position_monitor"):
            result = await monitor._process_one(pool, position, now)

    assert result["status"] == "closed"
    assert result["outcome"] == "expired"
    assert result["realized_pnl"] == pytest.approx(9.99)
    # update_position_outcome should have been called with both pnl columns
    update_calls = [c for c in pool.calls if "tracked_positions" in c[0]]
    assert len(update_calls) == 1
    sql, params = update_calls[0]
    assert params[0] == "closed"
    assert params[1] == "expired"
    assert params[4] == pytest.approx(9.99)  # realized_pnl_usd_final
    assert any("expired" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_expiry_runs_before_sl_tp():
    """Even with SL/TP hit, expiry settles via 'expired' (priority order)."""
    monitor = PositionMonitor()
    pool = _FakePool()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    position = _make_position(expiry_at=now - timedelta(seconds=1))
    snapshot = _make_snapshot(tp_hit=True)  # would otherwise settle as tp1_hit
    breakdown = _make_breakdown()

    with patch.object(pm, "fetch_latest_price", return_value=80000.0), \
         patch.object(pm, "_build_snapshot", return_value=snapshot), \
         patch.object(pm, "write_position_update", return_value=1), \
         patch.object(pm, "update_position_extremes", return_value=1), \
         patch.object(pm, "_resolve_instrument_id", return_value=55), \
         patch.object(pm, "compute_realized_pnl_db", return_value=breakdown):
        result = await monitor._process_one(pool, position, now)

    assert result["outcome"] == "expired"


@pytest.mark.asyncio
async def test_tp_hit_uses_constraint_allowed_outcome():
    """TP hit emits 'tp1_hit' (not the legacy invalid 'take_profit')."""
    monitor = PositionMonitor()
    pool = _FakePool()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    position = _make_position(expiry_at=None)
    snapshot = _make_snapshot(tp_hit=True)
    breakdown = _make_breakdown()

    with patch.object(pm, "fetch_latest_price", return_value=80000.0), \
         patch.object(pm, "_build_snapshot", return_value=snapshot), \
         patch.object(pm, "write_position_update", return_value=1), \
         patch.object(pm, "update_position_extremes", return_value=1), \
         patch.object(pm, "_resolve_instrument_id", return_value=55), \
         patch.object(pm, "compute_realized_pnl_db", return_value=breakdown):
        result = await monitor._process_one(pool, position, now)

    assert result["outcome"] == "tp1_hit"
    update_calls = [c for c in pool.calls if "tracked_positions" in c[0]]
    assert update_calls[0][1][1] == "tp1_hit"


@pytest.mark.asyncio
async def test_sl_hit_uses_constraint_allowed_outcome():
    """SL hit emits 'sl_hit' (not the legacy invalid 'stop_loss')."""
    monitor = PositionMonitor()
    pool = _FakePool()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    position = _make_position(expiry_at=None)
    snapshot = _make_snapshot(sl_hit=True)
    breakdown = _make_breakdown(net="-7.50000000")

    with patch.object(pm, "fetch_latest_price", return_value=77000.0), \
         patch.object(pm, "_build_snapshot", return_value=snapshot), \
         patch.object(pm, "write_position_update", return_value=1), \
         patch.object(pm, "update_position_extremes", return_value=1), \
         patch.object(pm, "_resolve_instrument_id", return_value=55), \
         patch.object(pm, "compute_realized_pnl_db", return_value=breakdown):
        result = await monitor._process_one(pool, position, now)

    assert result["outcome"] == "sl_hit"
    assert result["realized_pnl"] == pytest.approx(-7.5)


@pytest.mark.asyncio
async def test_settle_deferred_when_no_fee_profile(caplog):
    """No fee profile → no UPDATE; status=settle_deferred returned."""
    monitor = PositionMonitor()
    pool = _FakePool()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    position = _make_position(expiry_at=now - timedelta(seconds=1))
    snapshot = _make_snapshot()

    with patch.object(pm, "fetch_latest_price", return_value=80000.0), \
         patch.object(pm, "_build_snapshot", return_value=snapshot), \
         patch.object(pm, "write_position_update", return_value=1), \
         patch.object(pm, "update_position_extremes", return_value=1), \
         patch.object(pm, "_resolve_instrument_id", return_value=55), \
         patch.object(pm, "compute_realized_pnl_db", return_value=None):
        with caplog.at_level(logging.ERROR, logger="tickles.intelligence.position_monitor"):
            result = await monitor._process_one(pool, position, now)

    assert result["status"] == "settle_deferred"
    assert result["reason"] == "expired_no_profile"
    # No UPDATE on tracked_positions should have happened
    update_calls = [c for c in pool.calls if "UPDATE public.tracked_positions" in c[0]]
    assert update_calls == []
    assert any("no fee profile" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_settle_deferred_when_instrument_unresolved(caplog):
    """Unresolved instrument → no UPDATE; structured ERROR logged."""
    monitor = PositionMonitor()
    pool = _FakePool()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    position = _make_position(expiry_at=now - timedelta(seconds=1))
    snapshot = _make_snapshot()

    with patch.object(pm, "fetch_latest_price", return_value=80000.0), \
         patch.object(pm, "_build_snapshot", return_value=snapshot), \
         patch.object(pm, "write_position_update", return_value=1), \
         patch.object(pm, "update_position_extremes", return_value=1), \
         patch.object(pm, "_resolve_instrument_id", return_value=None):
        with caplog.at_level(logging.ERROR, logger="tickles.intelligence.position_monitor"):
            result = await monitor._process_one(pool, position, now)

    assert result["status"] == "settle_deferred"
    update_calls = [c for c in pool.calls if "UPDATE public.tracked_positions" in c[0]]
    assert update_calls == []
    assert any("instrument unresolved" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_no_expiry_no_sltp_returns_monitored():
    """Without expiry or SL/TP hit, position stays open and is monitored."""
    monitor = PositionMonitor()
    pool = _FakePool()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    # expiry far in future
    position = _make_position(expiry_at=now + timedelta(days=7))
    snapshot = _make_snapshot()  # no SL/TP hit

    with patch.object(pm, "fetch_latest_price", return_value=79000.0), \
         patch.object(pm, "_build_snapshot", return_value=snapshot), \
         patch.object(pm, "write_position_update", return_value=42), \
         patch.object(pm, "update_position_extremes", return_value=1):
        result = await monitor._process_one(pool, position, now)

    assert result["status"] == "monitored"
    assert result["update_id"] == 42
    update_calls = [c for c in pool.calls if "UPDATE public.tracked_positions" in c[0]]
    assert update_calls == []
