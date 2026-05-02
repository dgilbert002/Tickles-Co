"""
Module: fee_calc
Purpose: Real-exchange fee/spread/funding replication for realized P&L (D7).
Location: /opt/tickles/shared/intelligence/fee_calc.py

Computes the realized USD P&L of a closed position the way an exchange or
broker actually settles it — using the per-instrument fee policy stored in
``public.instruments`` (taker/maker fees, spread, overnight funding). All
arithmetic is performed with ``decimal.Decimal`` so we don't bleed binary
floating-point error into accounting numbers.

Directive D7 (binding, 2026-05-02): "fee policy, replicate what exchanges or
brokers do". This module is the single source of truth for that math —
position_monitor (F2 expiry close), backfill_orphan_positions (F9), and any
future close-path must call ``compute_realized_pnl`` rather than re-doing
the math inline.

Public API:
  - ``InstrumentFeeProfile``      — the per-instrument fee row
  - ``RealizedPnlBreakdown``       — line-item breakdown of the close
  - ``load_fee_profile()``         — fetch the instrument's fee row from DB
  - ``compute_realized_pnl()``     — pure function, no DB
  - ``compute_realized_pnl_db()``  — convenience wrapper (DB lookup + math)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Quantize step for USD outputs: 8 decimal places to match
# numeric(20,8) on tracked_positions.realized_pnl_usd_final.
USD_QUANTUM = Decimal("0.00000001")
# Quantize step for ratio (pct/100) outputs: 10dp.
RATIO_QUANTUM = Decimal("0.0000000001")

# Asset classes that accrue overnight funding daily (continuous markets).
# CFDs and perps swap funding nightly; spot crypto generally does not unless
# the instrument's funding row is non-NULL — we honor whatever the DB says.
_FUNDING_ASSET_CLASSES = frozenset({"crypto", "cfd", "fx", "futures", "index"})


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentFeeProfile:
    """Per-instrument fee/spread/funding policy from ``public.instruments``.

    All percentages are stored on the row as percent-units (e.g. 0.075 means
    7.5 bps = 0.00075 of notional). NULL is treated as zero.
    """

    instrument_id: int
    symbol: str
    exchange: str
    asset_class: str
    contract_multiplier: Decimal
    taker_fee_pct: Decimal
    maker_fee_pct: Decimal
    spread_pct: Decimal
    overnight_funding_long_pct: Decimal
    overnight_funding_short_pct: Decimal


@dataclass(frozen=True)
class RealizedPnlBreakdown:
    """Line-item breakdown of how the close P&L was computed.

    All amounts are USD ``Decimal``. ``net_pnl_usd`` is what gets written to
    ``tracked_positions.realized_pnl_usd_final``; the rest are audit fields.
    """

    direction: str
    entry_price: Decimal
    exit_price: Decimal
    qty: Decimal
    contract_multiplier: Decimal
    leverage: Decimal
    notional_usd_entry: Decimal
    notional_usd_exit: Decimal
    gross_pnl_usd: Decimal
    entry_fee_usd: Decimal
    exit_fee_usd: Decimal
    spread_cost_usd: Decimal
    funding_cost_usd: Decimal
    days_held: Decimal
    net_pnl_usd: Decimal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Convert any numeric-looking value (None / float / str / Decimal) to Decimal.

    NULL / missing inputs become ``default``. Non-numeric strings become
    ``default`` with a WARN log so we never silently accept garbage.

    Args:
        value: Source value (may be None, int, float, str, Decimal).
        default: Fallback when conversion fails.

    Returns:
        Decimal representation, or ``default`` on failure / NULL.
    """
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        # Pass through str() to dodge float -> Decimal precision drift
        # (Decimal(0.1) gives the binary-FP nightmare, Decimal("0.1") doesn't).
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        logger.warning("fee_calc._to_decimal: cannot convert %r: %s", value, exc)
        return default


def _percent_to_ratio(pct: Decimal) -> Decimal:
    """Convert a percent-unit value (e.g. 0.075 = 0.075%) to a ratio (0.00075).

    Args:
        pct: Percent value as stored on the instruments row.

    Returns:
        The corresponding ratio (pct / 100).
    """
    return (pct / Decimal("100"))


def _days_between(opened_at: datetime, closed_at: datetime) -> Decimal:
    """Calendar-day count between two timestamps, as a positive Decimal.

    Both timestamps must be timezone-aware (UTC). Returns ``0`` if
    ``closed_at <= opened_at`` (defensive). Funding accrual is per night
    held — fractional days count proportionally.

    Args:
        opened_at: Position open timestamp (tz-aware UTC).
        closed_at: Position close timestamp (tz-aware UTC).

    Returns:
        Days held as Decimal (e.g. ``1.5`` for 36h).
    """
    try:
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        delta = closed_at - opened_at
        if delta.total_seconds() <= 0:
            return Decimal("0")
        seconds = Decimal(str(delta.total_seconds()))
        return seconds / Decimal("86400")
    except (TypeError, ValueError) as exc:
        logger.warning("fee_calc._days_between: invalid timestamps: %s", exc)
        return Decimal("0")


# ---------------------------------------------------------------------------
# DB lookup
# ---------------------------------------------------------------------------


async def load_fee_profile(
    pool,
    instrument_id: int,
) -> Optional[InstrumentFeeProfile]:
    """Load the fee/spread/funding profile for one instrument.

    Args:
        pool: Shared Postgres pool (asyncpg-backed via shared.utils.db).
        instrument_id: ``public.instruments.id``.

    Returns:
        ``InstrumentFeeProfile`` if found, else ``None``.
    """
    try:
        row = await pool.fetch_one(
            """
            SELECT id, symbol, exchange, asset_class::text AS asset_class,
                   contract_multiplier, taker_fee_pct, maker_fee_pct,
                   spread_pct, overnight_funding_long_pct,
                   overnight_funding_short_pct
            FROM public.instruments
            WHERE id = $1
            """,
            (instrument_id,),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception(
            "load_fee_profile: query failed instrument_id=%s: %s",
            instrument_id,
            exc,
        )
        return None

    if not row:
        return None

    return InstrumentFeeProfile(
        instrument_id=int(row["id"]),
        symbol=str(row["symbol"]),
        exchange=str(row["exchange"]),
        asset_class=str(row["asset_class"]),
        contract_multiplier=_to_decimal(row.get("contract_multiplier"), Decimal("1")),
        taker_fee_pct=_to_decimal(row.get("taker_fee_pct"), Decimal("0")),
        maker_fee_pct=_to_decimal(row.get("maker_fee_pct"), Decimal("0")),
        spread_pct=_to_decimal(row.get("spread_pct"), Decimal("0")),
        overnight_funding_long_pct=_to_decimal(
            row.get("overnight_funding_long_pct"), Decimal("0")
        ),
        overnight_funding_short_pct=_to_decimal(
            row.get("overnight_funding_short_pct"), Decimal("0")
        ),
    )


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


def compute_realized_pnl(
    *,
    direction: str,
    entry_price: Any,
    exit_price: Any,
    qty: Any,
    leverage: Any,
    profile: InstrumentFeeProfile,
    opened_at: datetime,
    closed_at: datetime,
    fee_tier: str = "taker",
) -> RealizedPnlBreakdown:
    """Compute realized USD P&L the way the venue would actually settle it.

    Replicates real-exchange behaviour (D7):
      - Gross P&L: ``(exit - entry) * qty * multiplier`` for long, mirrored
        for short.
      - Fees: charged on BOTH legs (open + close) on the notional traded.
        Default uses ``taker_fee_pct`` (market orders); pass
        ``fee_tier='maker'`` for limit-fill simulations.
      - Spread: charged once on entry notional — half-spread on each side
        is the conventional model, totaling one full ``spread_pct``.
      - Funding: per-day accrual using the side-specific funding rate,
        applied to the notional, scaled by calendar days held. Only
        applied when the asset class is in ``_FUNDING_ASSET_CLASSES``
        and the rate is non-zero (NULL on the instrument row → 0).

    All inputs are coerced to ``Decimal`` defensively. Direction must be
    one of ``'long' | 'short'`` (matching the schema check constraint);
    anything else raises ``ValueError``.

    Args:
        direction: ``'long'`` or ``'short'``.
        entry_price: Fill price at open.
        exit_price: Fill price at close.
        qty: Position size in base units.
        leverage: Position leverage (typically 1.0 for spot).
        profile: Fee profile loaded from ``instruments``.
        opened_at: Open timestamp (tz-aware UTC).
        closed_at: Close timestamp (tz-aware UTC).
        fee_tier: ``'taker'`` (default) or ``'maker'``.

    Returns:
        ``RealizedPnlBreakdown`` with ``net_pnl_usd`` quantized to 8dp.

    Raises:
        ValueError: when direction is not ``'long'`` or ``'short'``.
    """
    direction_norm = (direction or "").strip().lower()
    if direction_norm not in {"long", "short"}:
        raise ValueError(f"compute_realized_pnl: invalid direction={direction!r}")

    entry = _to_decimal(entry_price)
    exit_ = _to_decimal(exit_price)
    quantity = _to_decimal(qty)
    lev = _to_decimal(leverage, Decimal("1"))
    if lev <= 0:
        lev = Decimal("1")

    multiplier = profile.contract_multiplier or Decimal("1")
    notional_entry = entry * quantity * multiplier
    notional_exit = exit_ * quantity * multiplier

    # Gross P&L (direction-aware)
    if direction_norm == "long":
        gross_pnl = (exit_ - entry) * quantity * multiplier
    else:
        gross_pnl = (entry - exit_) * quantity * multiplier

    # Fee tier selection
    fee_pct = profile.taker_fee_pct
    if fee_tier == "maker":
        fee_pct = profile.maker_fee_pct
    fee_ratio = _percent_to_ratio(fee_pct)

    entry_fee = (notional_entry.copy_abs()) * fee_ratio
    exit_fee = (notional_exit.copy_abs()) * fee_ratio

    # Spread cost — charged once on entry notional (full round-trip spread).
    spread_ratio = _percent_to_ratio(profile.spread_pct)
    spread_cost = (notional_entry.copy_abs()) * spread_ratio

    # Funding cost — per-day, side-specific, asset-class-gated.
    days = _days_between(opened_at, closed_at)
    funding_cost = Decimal("0")
    if profile.asset_class.lower() in _FUNDING_ASSET_CLASSES:
        if direction_norm == "long":
            funding_pct = profile.overnight_funding_long_pct
        else:
            funding_pct = profile.overnight_funding_short_pct
        funding_ratio = _percent_to_ratio(funding_pct)
        # Funding accrues against the notional (a positive cost to the holder
        # when the rate is positive; negative funding = rebate).
        funding_cost = (notional_entry.copy_abs()) * funding_ratio * days

    net_pnl = gross_pnl - entry_fee - exit_fee - spread_cost - funding_cost

    return RealizedPnlBreakdown(
        direction=direction_norm,
        entry_price=entry,
        exit_price=exit_,
        qty=quantity,
        contract_multiplier=multiplier,
        leverage=lev,
        notional_usd_entry=notional_entry.quantize(USD_QUANTUM, rounding=ROUND_HALF_EVEN),
        notional_usd_exit=notional_exit.quantize(USD_QUANTUM, rounding=ROUND_HALF_EVEN),
        gross_pnl_usd=gross_pnl.quantize(USD_QUANTUM, rounding=ROUND_HALF_EVEN),
        entry_fee_usd=entry_fee.quantize(USD_QUANTUM, rounding=ROUND_HALF_EVEN),
        exit_fee_usd=exit_fee.quantize(USD_QUANTUM, rounding=ROUND_HALF_EVEN),
        spread_cost_usd=spread_cost.quantize(USD_QUANTUM, rounding=ROUND_HALF_EVEN),
        funding_cost_usd=funding_cost.quantize(USD_QUANTUM, rounding=ROUND_HALF_EVEN),
        days_held=days.quantize(RATIO_QUANTUM, rounding=ROUND_HALF_EVEN),
        net_pnl_usd=net_pnl.quantize(USD_QUANTUM, rounding=ROUND_HALF_EVEN),
    )


# ---------------------------------------------------------------------------
# DB convenience wrapper
# ---------------------------------------------------------------------------


async def compute_realized_pnl_db(
    pool,
    *,
    instrument_id: int,
    direction: str,
    entry_price: Any,
    exit_price: Any,
    qty: Any,
    leverage: Any,
    opened_at: datetime,
    closed_at: datetime,
    fee_tier: str = "taker",
) -> Optional[RealizedPnlBreakdown]:
    """Look up the fee profile and run ``compute_realized_pnl`` in one call.

    Args:
        pool: Shared Postgres pool.
        instrument_id: ``public.instruments.id`` for the closed position.
        direction: ``'long'`` or ``'short'``.
        entry_price: Fill price at open.
        exit_price: Fill price at close.
        qty: Position size in base units.
        leverage: Position leverage (1.0 for spot).
        opened_at: Open timestamp (tz-aware UTC).
        closed_at: Close timestamp (tz-aware UTC).
        fee_tier: ``'taker'`` or ``'maker'``.

    Returns:
        ``RealizedPnlBreakdown`` or ``None`` when the fee profile cannot
        be loaded (caller should treat this as an unrecoverable accounting
        gap and skip the close).
    """
    profile = await load_fee_profile(pool, instrument_id)
    if profile is None:
        logger.error(
            "compute_realized_pnl_db: no instrument profile id=%s — cannot settle",
            instrument_id,
        )
        return None

    try:
        return compute_realized_pnl(
            direction=direction,
            entry_price=entry_price,
            exit_price=exit_price,
            qty=qty,
            leverage=leverage,
            profile=profile,
            opened_at=opened_at,
            closed_at=closed_at,
            fee_tier=fee_tier,
        )
    except ValueError as exc:
        logger.error(
            "compute_realized_pnl_db: invalid input instrument_id=%s: %s",
            instrument_id,
            exc,
        )
        return None
