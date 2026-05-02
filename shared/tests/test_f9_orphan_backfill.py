"""
Module: test_f9_orphan_backfill
Purpose: Smoke tests for the F9 orphan backfill script — pure-logic units that
         do not require the live database. The DB-touching paths are exercised
         by an integration dry-run against staging.
Location: /opt/tickles/shared/tests/test_f9_orphan_backfill.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import pytest

from shared.intelligence.fee_calc import RealizedPnlBreakdown
from shared.scripts import backfill_orphan_positions as f9


# -----------------------------------------------------------------------------
# parse_levels_from_text
# -----------------------------------------------------------------------------


def test_parse_levels_extracts_entry_sl_tp_from_typical_signal() -> None:
    """Verify the regex handles a typical bullet-style trader signal."""
    text = "BTC LONG entry: 78000  sl: 76500  tp1: 80000"
    entry, sl, tp = f9.parse_levels_from_text(text)
    assert entry == Decimal("78000")
    assert sl == Decimal("76500")
    assert tp == Decimal("80000")


def test_parse_levels_handles_thousands_separators_and_k_suffix() -> None:
    """Verify '78,500.5' and '78k' both parse cleanly."""
    text = "long @ 78,500.5  Stop loss: 77k  TP: 80,000"
    entry, sl, tp = f9.parse_levels_from_text(text)
    assert entry == Decimal("78500.5")
    assert sl == Decimal("77000")
    assert tp == Decimal("80000")


def test_parse_levels_returns_none_tuple_for_empty_or_garbage() -> None:
    """Missing or unrelated text yields all-None."""
    assert f9.parse_levels_from_text(None) == (None, None, None)
    assert f9.parse_levels_from_text("") == (None, None, None)
    assert f9.parse_levels_from_text("just some chatter") == (None, None, None)


# -----------------------------------------------------------------------------
# _validate_parsed_entry — sanity band guard
# -----------------------------------------------------------------------------


def test_validate_parsed_entry_accepts_within_band() -> None:
    """Entry within ±20% of candle close passes."""
    assert f9._validate_parsed_entry(Decimal("78000"), Decimal("80000")) is True
    assert f9._validate_parsed_entry(Decimal("96000"), Decimal("80000")) is True


def test_validate_parsed_entry_rejects_outside_band() -> None:
    """Entry far from candle close is rejected."""
    assert f9._validate_parsed_entry(Decimal("50000"), Decimal("80000")) is False
    assert f9._validate_parsed_entry(Decimal("200000"), Decimal("80000")) is False


def test_validate_parsed_entry_rejects_non_positive() -> None:
    """Zero or negative inputs are rejected."""
    assert f9._validate_parsed_entry(Decimal("0"), Decimal("80000")) is False
    assert f9._validate_parsed_entry(Decimal("78000"), Decimal("0")) is False


# -----------------------------------------------------------------------------
# _check_hit — SL/TP intra-candle detection
# -----------------------------------------------------------------------------


def _candle(*, ts: datetime, high: str, low: str, close: str) -> Dict[str, Any]:
    """Build a candle dict for tests."""
    return {
        "timestamp": ts,
        "open": Decimal(close),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal(close),
    }


def test_check_hit_long_sl_first_when_both_in_same_candle() -> None:
    """SL takes priority over TP in the same candle for a long (conservative)."""
    candle = _candle(
        ts=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        high="80500",
        low="76000",
        close="78000",
    )
    outcome, exit_px = f9._check_hit(
        "long", candle, sl=Decimal("76500"), tp=Decimal("80000")
    )
    assert outcome == f9.OUTCOME_SL
    assert exit_px == Decimal("76500")


def test_check_hit_long_tp_only_when_sl_not_breached() -> None:
    """TP fires when high >= tp and low > sl."""
    candle = _candle(
        ts=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        high="80500",
        low="79000",
        close="80000",
    )
    outcome, exit_px = f9._check_hit(
        "long", candle, sl=Decimal("76500"), tp=Decimal("80000")
    )
    assert outcome == f9.OUTCOME_TP1
    assert exit_px == Decimal("80000")


def test_check_hit_short_sl_when_high_breaches() -> None:
    """For shorts, SL is breached when high >= sl."""
    candle = _candle(
        ts=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        high="79500",
        low="77000",
        close="78000",
    )
    outcome, exit_px = f9._check_hit(
        "short", candle, sl=Decimal("79000"), tp=Decimal("76000")
    )
    assert outcome == f9.OUTCOME_SL
    assert exit_px == Decimal("79000")


def test_check_hit_no_levels_returns_none() -> None:
    """When both sl and tp are None, no hit is recorded."""
    candle = _candle(
        ts=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        high="80500",
        low="76000",
        close="78000",
    )
    outcome, exit_px = f9._check_hit("long", candle, sl=None, tp=None)
    assert outcome is None
    assert exit_px is None


# -----------------------------------------------------------------------------
# _walk_forward — exit-priority logic
# -----------------------------------------------------------------------------


def _seq_candles(
    start: datetime,
    triples: List[Tuple[str, str, str]],
) -> List[Dict[str, Any]]:
    """Build a list of consecutive 1m candles from (high, low, close) triples."""
    return [
        _candle(ts=start + timedelta(minutes=i), high=h, low=l, close=c)
        for i, (h, l, c) in enumerate(triples)
    ]


def test_walk_forward_stops_at_first_tp_hit() -> None:
    """Walk halts as soon as TP hits in a candle."""
    start = datetime(2026, 5, 1, 11, 0, tzinfo=timezone.utc)
    candles = _seq_candles(
        start,
        [
            ("78500", "77800", "78200"),
            ("80100", "78200", "80050"),  # TP hit here
            ("81000", "80000", "80500"),
        ],
    )
    outcome, exit_px, closed_at = f9._walk_forward(
        "long", candles, sl=Decimal("76000"), tp=Decimal("80000"), expiry_at=None
    )
    assert outcome == f9.OUTCOME_TP1
    assert exit_px == Decimal("80000")
    assert closed_at == start + timedelta(minutes=1)


def test_walk_forward_expiry_fires_before_sl_when_earlier() -> None:
    """If expiry_at falls before any SL/TP hit, expiry wins."""
    start = datetime(2026, 5, 1, 11, 0, tzinfo=timezone.utc)
    expiry = start + timedelta(minutes=2)
    candles = _seq_candles(
        start,
        [
            ("78500", "77800", "78200"),
            ("78600", "77900", "78300"),
            ("78700", "76000", "76100"),  # would have been SL — but expiry is now
        ],
    )
    outcome, exit_px, closed_at = f9._walk_forward(
        "long", candles, sl=Decimal("76500"), tp=Decimal("80000"), expiry_at=expiry
    )
    assert outcome == f9.OUTCOME_EXPIRED
    assert closed_at == expiry
    # exit_price should be candle.close at the expiry candle (index 2)
    assert exit_px == Decimal("76100")


def test_walk_forward_no_trigger_returns_manual_close_at_last() -> None:
    """When no level is hit and no expiry, returns last candle close."""
    start = datetime(2026, 5, 1, 11, 0, tzinfo=timezone.utc)
    candles = _seq_candles(
        start,
        [
            ("78500", "77800", "78200"),
            ("78600", "77900", "78300"),
            ("78700", "78000", "78400"),
        ],
    )
    outcome, exit_px, closed_at = f9._walk_forward(
        "long", candles, sl=Decimal("70000"), tp=Decimal("90000"), expiry_at=None
    )
    assert outcome == f9.OUTCOME_MANUAL
    assert exit_px == Decimal("78400")
    assert closed_at == start + timedelta(minutes=2)


# -----------------------------------------------------------------------------
# plan_one — end-to-end orchestration with a fake pool
# -----------------------------------------------------------------------------


class _FakePool:
    """Minimal in-memory pool for plan_one orchestration tests."""

    def __init__(
        self,
        *,
        instrument_id: Optional[int],
        candle_at_close: Optional[Decimal],
        walk_candles: List[Dict[str, Any]],
        breakdown: Optional[RealizedPnlBreakdown],
    ) -> None:
        self.instrument_id = instrument_id
        self.candle_at_close = candle_at_close
        self.walk_candles = walk_candles
        self.breakdown = breakdown
        self.calls: List[Tuple[str, Any]] = []

    async def fetch_one(self, sql: str, params: Any = None) -> Optional[Dict[str, Any]]:
        self.calls.append((sql, params))
        if "FROM public.instruments" in sql or "instrument_aliases" in sql:
            if self.instrument_id is None:
                return None
            return {"id": self.instrument_id}
        if "FROM public.candles" in sql and "ORDER BY timestamp DESC" in sql:
            if self.candle_at_close is None:
                return None
            return {
                "timestamp": params[2],
                "open": self.candle_at_close,
                "high": self.candle_at_close,
                "low": self.candle_at_close,
                "close": self.candle_at_close,
            }
        return None

    async def fetch_all(self, sql: str, params: Any = None) -> List[Dict[str, Any]]:
        self.calls.append((sql, params))
        if "FROM public.candles" in sql:
            return list(self.walk_candles)
        return []

    async def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))


def _make_breakdown(net: str = "10.0") -> RealizedPnlBreakdown:
    """Build a stub RealizedPnlBreakdown for tests."""
    return RealizedPnlBreakdown(
        direction="long",
        entry_price=Decimal("78000"),
        exit_price=Decimal("80000"),
        qty=Decimal("0.001"),
        contract_multiplier=Decimal("1"),
        leverage=Decimal("1"),
        notional_usd_entry=Decimal("78"),
        notional_usd_exit=Decimal("80"),
        gross_pnl_usd=Decimal("12"),
        entry_fee_usd=Decimal("0.5"),
        exit_fee_usd=Decimal("0.5"),
        spread_cost_usd=Decimal("0.2"),
        funding_cost_usd=Decimal("0.1"),
        days_held=Decimal("0.5"),
        net_pnl_usd=Decimal(net),
    )


def _orphan(**overrides: Any) -> Dict[str, Any]:
    """Build a baseline orphan row dict, with optional field overrides."""
    base = {
        "id": 81,
        "trader_profile_id": 357447,
        "signal_interpretation_id": None,
        "instrument_symbol": "BTCUSDT",
        "instrument_exchange": "bybit",
        "epic_code": None,
        "direction": "long",
        "entry_price": None,
        "position_size": None,
        "leverage": None,
        "stop_loss": None,
        "take_profit_1": None,
        "take_profit_2": None,
        "take_profit_3": None,
        "raw_signal_text": None,
        "signal_timestamp": datetime(2026, 5, 1, 11, 6, tzinfo=timezone.utc),
        "expiry_at": None,
        "notional_usd": Decimal("1000"),
        "created_at": datetime(2026, 5, 1, 11, 6, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 2, 19, 50, tzinfo=timezone.utc),
        "entry_reason_trader": "RSI oversold",
        "entry_reason_frozen_at": datetime(2026, 5, 1, 11, 6, tzinfo=timezone.utc),
        "status": "open",
    }
    base.update(overrides)
    return base


def test_plan_one_uses_candle_fallback_when_no_text(monkeypatch: Any) -> None:
    """Empty raw_signal_text → entry_price comes from candle.close."""
    start = datetime(2026, 5, 1, 11, 6, tzinfo=timezone.utc)
    walk = _seq_candles(
        start,
        [
            ("78500", "77800", "78200"),
            ("80100", "78200", "80050"),  # TP would hit, but no TP set
            ("81000", "80000", "80500"),
        ],
    )
    pool = _FakePool(
        instrument_id=3,
        candle_at_close=Decimal("78000"),
        walk_candles=walk,
        breakdown=_make_breakdown("9.99"),
    )

    async def fake_compute(*args: Any, **kwargs: Any) -> RealizedPnlBreakdown:
        return _make_breakdown("9.99")

    monkeypatch.setattr(f9, "compute_realized_pnl_db", fake_compute)

    plan, failure = asyncio.run(f9.plan_one(pool, _orphan()))

    assert failure is None
    assert plan is not None
    assert plan.position_id == 81
    assert plan.instrument_id == 3
    assert plan.entry_price == Decimal("78000")
    assert plan.entry_source == "candle"
    assert plan.outcome == f9.OUTCOME_MANUAL
    # qty = notional 1000 / entry 78000
    assert plan.qty == Decimal("1000") / Decimal("78000")
    assert plan.breakdown.net_pnl_usd == Decimal("9.99")


def test_plan_one_fails_when_instrument_unresolved(monkeypatch: Any) -> None:
    """Unresolvable instrument → BackfillFailure with reason='unresolved_instrument'."""
    pool = _FakePool(
        instrument_id=None,
        candle_at_close=None,
        walk_candles=[],
        breakdown=None,
    )
    plan, failure = asyncio.run(f9.plan_one(pool, _orphan(instrument_symbol="ZZZ")))
    assert plan is None
    assert failure is not None
    assert failure.reason == "unresolved_instrument"


def test_plan_one_uses_parsed_entry_when_within_sanity_band(monkeypatch: Any) -> None:
    """Parsed entry close to candle close is preferred over candle fallback."""
    start = datetime(2026, 5, 1, 11, 6, tzinfo=timezone.utc)
    walk = _seq_candles(start, [("78500", "77800", "78200")])
    pool = _FakePool(
        instrument_id=3,
        candle_at_close=Decimal("78000"),
        walk_candles=walk,
        breakdown=_make_breakdown(),
    )

    async def fake_compute(*args: Any, **kwargs: Any) -> RealizedPnlBreakdown:
        return _make_breakdown()

    monkeypatch.setattr(f9, "compute_realized_pnl_db", fake_compute)
    orphan = _orphan(raw_signal_text="LONG entry: 78500 sl: 76500 tp1: 81000")
    plan, failure = asyncio.run(f9.plan_one(pool, orphan))
    assert failure is None
    assert plan is not None
    assert plan.entry_source == "parsed"
    assert plan.entry_price == Decimal("78500")
    assert plan.stop_loss == Decimal("76500")
    assert plan.take_profit_1 == Decimal("81000")


def test_plan_one_falls_back_to_candle_when_parsed_entry_off_band(
    monkeypatch: Any,
) -> None:
    """Parsed entry far from market is rejected; candle fallback wins."""
    start = datetime(2026, 5, 1, 11, 6, tzinfo=timezone.utc)
    walk = _seq_candles(start, [("78500", "77800", "78200")])
    pool = _FakePool(
        instrument_id=3,
        candle_at_close=Decimal("78000"),
        walk_candles=walk,
        breakdown=_make_breakdown(),
    )

    async def fake_compute(*args: Any, **kwargs: Any) -> RealizedPnlBreakdown:
        return _make_breakdown()

    monkeypatch.setattr(f9, "compute_realized_pnl_db", fake_compute)
    # Entry 50000 vs candle 78000 = 35% off — outside 20% band
    orphan = _orphan(raw_signal_text="LONG entry: 50000 sl: 45000")
    plan, failure = asyncio.run(f9.plan_one(pool, orphan))
    assert failure is None
    assert plan is not None
    assert plan.entry_source == "candle"
    assert plan.entry_price == Decimal("78000")


def test_plan_one_fails_when_no_size_and_no_notional(monkeypatch: Any) -> None:
    """Without position_size or notional_usd, qty cannot be derived."""
    start = datetime(2026, 5, 1, 11, 6, tzinfo=timezone.utc)
    walk = _seq_candles(start, [("78500", "77800", "78200")])
    pool = _FakePool(
        instrument_id=3,
        candle_at_close=Decimal("78000"),
        walk_candles=walk,
        breakdown=None,
    )
    orphan = _orphan(notional_usd=None, position_size=None)
    plan, failure = asyncio.run(f9.plan_one(pool, orphan))
    assert plan is None
    assert failure is not None
    assert failure.reason == "no_size_no_notional"


def test_plan_one_fails_when_fee_calc_returns_none(monkeypatch: Any) -> None:
    """compute_realized_pnl_db returning None surfaces as fee_calc_failed."""
    start = datetime(2026, 5, 1, 11, 6, tzinfo=timezone.utc)
    walk = _seq_candles(start, [("78500", "77800", "78200")])
    pool = _FakePool(
        instrument_id=3,
        candle_at_close=Decimal("78000"),
        walk_candles=walk,
        breakdown=None,
    )

    async def fake_compute(*args: Any, **kwargs: Any) -> Optional[RealizedPnlBreakdown]:
        return None

    monkeypatch.setattr(f9, "compute_realized_pnl_db", fake_compute)
    plan, failure = asyncio.run(f9.plan_one(pool, _orphan()))
    assert plan is None
    assert failure is not None
    assert failure.reason == "fee_calc_failed"


# -----------------------------------------------------------------------------
# apply_plan — UPDATE SQL shape
# -----------------------------------------------------------------------------


def test_apply_plan_writes_both_pnl_columns_and_status_reason() -> None:
    """The UPDATE writes status, outcome, both PnL cols, and status_reason."""
    pool = _FakePool(
        instrument_id=3,
        candle_at_close=None,
        walk_candles=[],
        breakdown=_make_breakdown(),
    )
    plan = f9.BackfillPlan(
        position_id=81,
        instrument_id=3,
        direction="long",
        entry_price=Decimal("78000"),
        entry_source="candle",
        stop_loss=None,
        take_profit_1=None,
        qty=Decimal("0.0128"),
        opened_at=datetime(2026, 5, 1, 11, 6, tzinfo=timezone.utc),
        expiry_at=None,
        closed_at=datetime(2026, 5, 2, 18, 0, tzinfo=timezone.utc),
        exit_price=Decimal("80000"),
        outcome=f9.OUTCOME_MANUAL,
        breakdown=_make_breakdown("12.34"),
        notes="t",
    )
    asyncio.run(f9.apply_plan(pool, plan))
    update_calls = [c for c in pool.calls if "UPDATE public.tracked_positions" in c[0]]
    assert len(update_calls) == 1
    sql, params = update_calls[0]
    assert "realized_pnl_usd_final" in sql
    assert "status_reason" in sql
    assert "WHERE id = $13" in sql
    # params order matches placeholders 1..13 in apply_plan
    assert params[0] == Decimal("78000")        # entry_price
    assert params[1] == Decimal("0.0128")       # position_size
    assert params[4] == f9.STATUS_CLOSED        # status
    assert params[5] == f9.OUTCOME_MANUAL       # outcome
    assert params[8] == 12.34                   # net_pnl float
    assert params[9] == f9.STATUS_REASON
    assert params[10] == f9.ENTRY_REASON_BACKFILLED
    assert params[12] == 81                     # id


def test_apply_plan_uses_status_expired_for_expired_outcome() -> None:
    """outcome=expired ⇒ status=expired."""
    pool = _FakePool(
        instrument_id=3,
        candle_at_close=None,
        walk_candles=[],
        breakdown=_make_breakdown(),
    )
    plan = f9.BackfillPlan(
        position_id=81,
        instrument_id=3,
        direction="long",
        entry_price=Decimal("78000"),
        entry_source="candle",
        stop_loss=None,
        take_profit_1=None,
        qty=Decimal("0.0128"),
        opened_at=datetime(2026, 5, 1, 11, 6, tzinfo=timezone.utc),
        expiry_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        closed_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        exit_price=Decimal("79000"),
        outcome=f9.OUTCOME_EXPIRED,
        breakdown=_make_breakdown("5.0"),
        notes="t",
    )
    asyncio.run(f9.apply_plan(pool, plan))
    sql, params = [c for c in pool.calls if "UPDATE public.tracked_positions" in c[0]][0]
    assert params[4] == f9.STATUS_EXPIRED
    assert params[5] == f9.OUTCOME_EXPIRED


def test_apply_plan_skips_entry_reason_columns_when_reason_frozen() -> None:
    """When the orphan already has a frozen entry_reason, apply_plan must not
    touch entry_reason_* columns — the DB trigger rejects any UPDATE that does.
    The placeholder count drops from 13 → 11.
    """
    pool = _FakePool(
        instrument_id=3,
        candle_at_close=None,
        walk_candles=[],
        breakdown=_make_breakdown(),
    )
    plan = f9.BackfillPlan(
        position_id=80,
        instrument_id=3,
        direction="long",
        entry_price=Decimal("78000"),
        entry_source="candle",
        stop_loss=None,
        take_profit_1=None,
        qty=Decimal("0.0128"),
        opened_at=datetime(2026, 5, 1, 11, 6, tzinfo=timezone.utc),
        expiry_at=None,
        closed_at=datetime(2026, 5, 2, 18, 0, tzinfo=timezone.utc),
        exit_price=Decimal("80000"),
        outcome=f9.OUTCOME_MANUAL,
        breakdown=_make_breakdown("9.99"),
        notes="t",
        reason_frozen=True,
    )
    asyncio.run(f9.apply_plan(pool, plan))
    sql, params = [c for c in pool.calls if "UPDATE public.tracked_positions" in c[0]][0]
    assert "entry_reason_trader" not in sql
    assert "entry_reason_frozen_at" not in sql
    assert "WHERE id = $11" in sql
    assert len(params) == 11
    assert params[10] == 80  # id is the last placeholder
    assert params[8] == 9.99  # net_pnl_usd
    assert params[9] == f9.STATUS_REASON
