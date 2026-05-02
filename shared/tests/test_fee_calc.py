"""
Module: test_fee_calc
Purpose: Smoke tests for shared.intelligence.fee_calc (F10 / D7).
Location: /opt/tickles/shared/tests/test_fee_calc.py

Verifies the realized-P&L math replicates real-exchange settlement:
fees on both legs, spread once on entry notional, side-specific funding
gated on asset class, defensive Decimal coercion, and direction validation.

Tests are pure — no DB connection required. Profiles are constructed
in-memory so the test suite remains hermetic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from shared.intelligence.fee_calc import (
    InstrumentFeeProfile,
    RealizedPnlBreakdown,
    USD_QUANTUM,
    _days_between,
    _percent_to_ratio,
    _to_decimal,
    compute_realized_pnl,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _btc_perp_profile() -> InstrumentFeeProfile:
    """Realistic-ish BTC/USDT perp profile on a major venue.

    Numbers are illustrative for the math test, not pulled from DB.
    """
    return InstrumentFeeProfile(
        instrument_id=3,
        symbol="BTC/USDT",
        exchange="bybit",
        asset_class="crypto",
        contract_multiplier=Decimal("1"),
        taker_fee_pct=Decimal("0.06"),    # 6 bps
        maker_fee_pct=Decimal("0.01"),    # 1 bp
        spread_pct=Decimal("0.02"),       # 2 bps round-trip
        overnight_funding_long_pct=Decimal("0.01"),   # 1 bp/day to longs
        overnight_funding_short_pct=Decimal("-0.01"), # -1 bp/day (rebate)
    )


def _spot_no_funding_profile() -> InstrumentFeeProfile:
    """Spot equity profile — funding gated off via asset_class."""
    return InstrumentFeeProfile(
        instrument_id=999,
        symbol="AAPL",
        exchange="ibkr",
        asset_class="equity",  # NOT in _FUNDING_ASSET_CLASSES
        contract_multiplier=Decimal("1"),
        taker_fee_pct=Decimal("0.05"),
        maker_fee_pct=Decimal("0.0"),
        spread_pct=Decimal("0.0"),
        overnight_funding_long_pct=Decimal("0.05"),   # would charge if gate let it
        overnight_funding_short_pct=Decimal("0.05"),
    )


# ---------------------------------------------------------------------------
# _to_decimal
# ---------------------------------------------------------------------------


def test_to_decimal_handles_none() -> None:
    assert _to_decimal(None) == Decimal("0")
    assert _to_decimal(None, default=Decimal("7")) == Decimal("7")


def test_to_decimal_handles_float_without_drift() -> None:
    # Decimal(0.1) is the binary FP nightmare; via str() it's clean.
    assert _to_decimal(0.1) == Decimal("0.1")


def test_to_decimal_passes_through_decimal() -> None:
    d = Decimal("123.456")
    assert _to_decimal(d) is d


def test_to_decimal_handles_garbage() -> None:
    assert _to_decimal("not-a-number") == Decimal("0")
    assert _to_decimal("not-a-number", default=Decimal("-1")) == Decimal("-1")


# ---------------------------------------------------------------------------
# _percent_to_ratio
# ---------------------------------------------------------------------------


def test_percent_to_ratio() -> None:
    assert _percent_to_ratio(Decimal("100")) == Decimal("1")
    assert _percent_to_ratio(Decimal("0.075")) == Decimal("0.00075")


# ---------------------------------------------------------------------------
# _days_between
# ---------------------------------------------------------------------------


def test_days_between_one_day() -> None:
    a = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    b = a + timedelta(days=1)
    assert _days_between(a, b) == Decimal("1")


def test_days_between_fractional() -> None:
    a = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    b = a + timedelta(hours=12)
    assert _days_between(a, b) == Decimal("0.5")


def test_days_between_negative_clamped_to_zero() -> None:
    a = datetime(2026, 5, 2, 0, 0, 0, tzinfo=timezone.utc)
    b = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert _days_between(a, b) == Decimal("0")


def test_days_between_naive_coerced_to_utc() -> None:
    a = datetime(2026, 5, 1, 0, 0, 0)
    b = datetime(2026, 5, 1, 12, 0, 0)
    assert _days_between(a, b) == Decimal("0.5")


# ---------------------------------------------------------------------------
# compute_realized_pnl — happy path
# ---------------------------------------------------------------------------


def test_long_winning_trade_math() -> None:
    """Long BTC trade, +0.50 USD gross before costs, 24h hold.

    Hand calculation with the BTC perp profile above:
      qty = 0.001, entry = 78000, exit = 78500, mult = 1
      notional_entry = 78.00 USD
      notional_exit  = 78.50 USD
      gross_pnl      = (78500 - 78000) * 0.001 = 0.50 USD
      taker_fee_ratio = 0.06 / 100 = 0.0006
      entry_fee = 78.00 * 0.0006 = 0.0468
      exit_fee  = 78.50 * 0.0006 = 0.04710
      spread_ratio = 0.02 / 100 = 0.0002
      spread_cost  = 78.00 * 0.0002 = 0.01560
      funding_ratio (long) = 0.01 / 100 = 0.0001
      funding_cost  = 78.00 * 0.0001 * 1 day = 0.00780
      net = 0.50 - 0.0468 - 0.0471 - 0.0156 - 0.0078 = 0.38270
    """
    profile = _btc_perp_profile()
    opened = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(days=1)

    result = compute_realized_pnl(
        direction="long",
        entry_price=78000,
        exit_price=78500,
        qty=Decimal("0.001"),
        leverage=1,
        profile=profile,
        opened_at=opened,
        closed_at=closed,
    )

    assert isinstance(result, RealizedPnlBreakdown)
    assert result.direction == "long"
    assert result.notional_usd_entry == Decimal("78.00000000")
    assert result.notional_usd_exit == Decimal("78.50000000")
    assert result.gross_pnl_usd == Decimal("0.50000000")
    assert result.entry_fee_usd == Decimal("0.04680000")
    assert result.exit_fee_usd == Decimal("0.04710000")
    assert result.spread_cost_usd == Decimal("0.01560000")
    assert result.funding_cost_usd == Decimal("0.00780000")
    assert result.days_held == Decimal("1.0000000000")
    # Net = 0.50 - 0.0468 - 0.0471 - 0.0156 - 0.0078 = 0.3827
    assert result.net_pnl_usd == Decimal("0.38270000")


def test_short_winning_trade_math() -> None:
    """Short BTC: price drops from 80000 -> 79500, +0.50 USD gross."""
    profile = _btc_perp_profile()
    opened = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(hours=12)  # half day

    result = compute_realized_pnl(
        direction="short",
        entry_price=80000,
        exit_price=79500,
        qty=Decimal("0.001"),
        leverage=1,
        profile=profile,
        opened_at=opened,
        closed_at=closed,
    )

    assert result.direction == "short"
    # Gross = (80000 - 79500) * 0.001 = 0.50
    assert result.gross_pnl_usd == Decimal("0.50000000")
    # Short funding rate is -0.01 -> rebate -> negative funding cost
    # funding = 80.00 * -0.0001 * 0.5 = -0.004
    assert result.funding_cost_usd == Decimal("-0.00400000")
    assert result.days_held == Decimal("0.5000000000")


def test_maker_fee_tier_uses_maker_rate() -> None:
    profile = _btc_perp_profile()
    opened = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(days=1)

    taker = compute_realized_pnl(
        direction="long",
        entry_price=78000,
        exit_price=78500,
        qty=Decimal("0.001"),
        leverage=1,
        profile=profile,
        opened_at=opened,
        closed_at=closed,
        fee_tier="taker",
    )
    maker = compute_realized_pnl(
        direction="long",
        entry_price=78000,
        exit_price=78500,
        qty=Decimal("0.001"),
        leverage=1,
        profile=profile,
        opened_at=opened,
        closed_at=closed,
        fee_tier="maker",
    )

    # Maker fee (1bp) is 6x cheaper than taker (6bp), so maker net > taker net
    assert maker.net_pnl_usd > taker.net_pnl_usd
    # Sanity: maker entry_fee = 78 * 0.0001 = 0.00780000
    assert maker.entry_fee_usd == Decimal("0.00780000")


# ---------------------------------------------------------------------------
# Asset class gating for funding
# ---------------------------------------------------------------------------


def test_equity_asset_class_skips_funding() -> None:
    """asset_class='equity' is NOT in _FUNDING_ASSET_CLASSES; expect 0 funding."""
    profile = _spot_no_funding_profile()
    opened = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(days=10)  # long hold — funding would be huge if applied

    result = compute_realized_pnl(
        direction="long",
        entry_price=Decimal("200"),
        exit_price=Decimal("210"),
        qty=Decimal("100"),
        leverage=1,
        profile=profile,
        opened_at=opened,
        closed_at=closed,
    )

    assert result.funding_cost_usd == Decimal("0.00000000")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_invalid_direction_raises() -> None:
    profile = _btc_perp_profile()
    opened = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(hours=1)

    with pytest.raises(ValueError, match="invalid direction"):
        compute_realized_pnl(
            direction="banana",
            entry_price=78000,
            exit_price=78500,
            qty=Decimal("0.001"),
            leverage=1,
            profile=profile,
            opened_at=opened,
            closed_at=closed,
        )


def test_direction_normalisation_is_case_insensitive() -> None:
    profile = _btc_perp_profile()
    opened = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(hours=1)

    result = compute_realized_pnl(
        direction="LONG",
        entry_price=78000,
        exit_price=78500,
        qty=Decimal("0.001"),
        leverage=1,
        profile=profile,
        opened_at=opened,
        closed_at=closed,
    )
    assert result.direction == "long"


def test_zero_leverage_clamped_to_one() -> None:
    """Leverage <= 0 is treated as 1 to avoid div-by-zero downstream."""
    profile = _btc_perp_profile()
    opened = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(hours=1)

    result = compute_realized_pnl(
        direction="long",
        entry_price=78000,
        exit_price=78500,
        qty=Decimal("0.001"),
        leverage=0,
        profile=profile,
        opened_at=opened,
        closed_at=closed,
    )
    assert result.leverage == Decimal("1")


def test_none_inputs_coerced_to_zero() -> None:
    """Defensive: None entry/exit yields zeros, not exceptions."""
    profile = _btc_perp_profile()
    opened = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(hours=1)

    result = compute_realized_pnl(
        direction="long",
        entry_price=None,
        exit_price=None,
        qty=None,
        leverage=None,
        profile=profile,
        opened_at=opened,
        closed_at=closed,
    )
    assert result.entry_price == Decimal("0")
    assert result.exit_price == Decimal("0")
    assert result.qty == Decimal("0")
    assert result.gross_pnl_usd == Decimal("0.00000000")
    assert result.net_pnl_usd == Decimal("0.00000000")


# ---------------------------------------------------------------------------
# Output quantization
# ---------------------------------------------------------------------------


def test_outputs_quantized_to_8dp() -> None:
    """All USD fields must be quantized to USD_QUANTUM (8dp = numeric(20,8))."""
    profile = _btc_perp_profile()
    opened = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(days=1, hours=3, minutes=17)

    result = compute_realized_pnl(
        direction="long",
        entry_price=Decimal("12345.6789"),
        exit_price=Decimal("12400.123456789"),
        qty=Decimal("0.07654321"),
        leverage=1,
        profile=profile,
        opened_at=opened,
        closed_at=closed,
    )

    for field in (
        "notional_usd_entry",
        "notional_usd_exit",
        "gross_pnl_usd",
        "entry_fee_usd",
        "exit_fee_usd",
        "spread_cost_usd",
        "funding_cost_usd",
        "net_pnl_usd",
    ):
        value = getattr(result, field)
        # The exponent of a properly-quantized 8dp Decimal is -8.
        assert value.as_tuple().exponent == USD_QUANTUM.as_tuple().exponent, (
            f"{field}={value!r} not quantized to 8dp"
        )
