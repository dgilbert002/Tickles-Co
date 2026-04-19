"""
shared.trading.sizer — Phase 25 pure deterministic position sizer.

The sizer is the beating heart of Rule 1: **both backtest and live
call this exact function** so the numbers can't drift.

Design rules
------------
* Pure. No I/O. No clock. Only inputs in, outputs out.
* Integer/decimal safety: we work in Python floats but round to
  venue-specific precision (``lot_step``, ``price_step``,
  ``min_notional``) at the *end*, so golden tests freeze the result.
* Fee/slippage/overnight-cost math all in one place. Callers never
  bake it into a strategy.
* No leverage inference — the caller states ``requested_leverage``.
  If it exceeds the capability cap the checker already denied the
  intent before the sizer runs.

The sizer doesn't know about capabilities; Treasury does the gating.
This module only does math.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from shared.trading.capabilities import TradeIntent


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketSnapshot:
    """Minimal view of market state needed by the sizer."""

    price: float                       # last traded / mark
    bid: Optional[float] = None
    ask: Optional[float] = None
    contract_size: float = 1.0         # USD-per-contract for perps, 1 for spot
    lot_step: float = 0.0              # quantity increment, 0 = continuous
    min_notional_usd: float = 0.0      # venue minimum order notional
    taker_fee_bps: float = 0.0         # basis points (10 = 0.10%)
    maker_fee_bps: float = 0.0
    spread_bps_estimate: float = 0.0   # half-spread in bps, for expected slip
    slippage_bps_estimate: float = 0.0 # additional expected slippage
    overnight_bps_per_day: float = 0.0 # swap/funding in bps/day

    def mid(self) -> float:
        if self.bid is not None and self.ask is not None:
            return 0.5 * (float(self.bid) + float(self.ask))
        return float(self.price)


@dataclass(frozen=True)
class AccountSnapshot:
    """Balance + margin view passed into the sizer by Treasury."""

    company_id: str
    exchange: str
    account_id_external: str
    currency: str = "USD"
    balance: float = 0.0
    equity: Optional[float] = None
    margin_used: float = 0.0
    free_margin: Optional[float] = None

    def available_capital(self) -> float:
        if self.free_margin is not None:
            return float(self.free_margin)
        if self.equity is not None:
            return max(float(self.equity) - float(self.margin_used), 0.0)
        return max(float(self.balance) - float(self.margin_used), 0.0)


@dataclass(frozen=True)
class StrategyConfig:
    """Strategy-side inputs that affect sizing."""

    name: str
    risk_per_trade_pct: float = 0.01     # fraction of equity (1% default)
    max_notional_pct: float = 1.0        # cap as fraction of available cap
    sl_distance_pct: Optional[float] = None  # from entry; used for risk-based sizing
    tp_distance_pct: Optional[float] = None
    prefer_maker: bool = False


@dataclass
class SizedIntent:
    """Output of the sizer — fully deterministic, venue-ready."""

    intent: TradeIntent
    quantity: float = 0.0
    notional_usd: float = 0.0
    leverage: int = 1
    entry_price: float = 0.0
    expected_entry_price: float = 0.0
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    expected_fee_usd: float = 0.0
    expected_spread_usd: float = 0.0
    expected_slippage_usd: float = 0.0
    expected_overnight_usd_per_day: float = 0.0
    fee_rate_bps: float = 0.0
    reasons: list = field(default_factory=list)
    skipped: bool = False
    skipped_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent_hash": self.intent.stable_hash(),
            "symbol": self.intent.symbol,
            "exchange": self.intent.exchange,
            "direction": self.intent.direction,
            "quantity": self.quantity,
            "notional_usd": self.notional_usd,
            "leverage": self.leverage,
            "entry_price": self.entry_price,
            "expected_entry_price": self.expected_entry_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "expected_fee_usd": self.expected_fee_usd,
            "expected_spread_usd": self.expected_spread_usd,
            "expected_slippage_usd": self.expected_slippage_usd,
            "expected_overnight_usd_per_day": self.expected_overnight_usd_per_day,
            "fee_rate_bps": self.fee_rate_bps,
            "reasons": list(self.reasons),
            "skipped": self.skipped,
            "skipped_reason": self.skipped_reason,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _round_to_step(value: float, step: float) -> float:
    if step is None or step <= 0:
        return float(value)
    # Floor so we never exceed caps.
    return math.floor(float(value) / float(step)) * float(step)


def _clamp(value: float, lo: Optional[float], hi: Optional[float]) -> float:
    if lo is not None and value < lo:
        value = lo
    if hi is not None and value > hi:
        value = hi
    return value


# ---------------------------------------------------------------------------
# Public sizer
# ---------------------------------------------------------------------------


def size_intent(
    intent: TradeIntent,
    account: AccountSnapshot,
    market: MarketSnapshot,
    strategy: StrategyConfig,
    effective_max_notional_usd: Optional[float] = None,
    effective_max_leverage: Optional[int] = None,
) -> SizedIntent:
    """Compute quantity, SL/TP, expected costs.

    Preference order for notional sizing:

    1. ``intent.requested_notional_usd`` (explicit ask), capped by
       ``effective_max_notional_usd``.
    2. Strategy risk-based sizing if ``strategy.sl_distance_pct`` is
       provided and non-zero.
    3. ``strategy.max_notional_pct`` × ``account.available_capital()``.

    The final notional is capped by both the venue's ``min_notional_usd``
    (floor) and the capability layer's ``effective_max_notional_usd``
    (ceiling). If the result is below the venue minimum, the sizer
    returns ``skipped=True`` rather than an invalid trade.
    """
    reasons: list = []

    available = account.available_capital()
    if available <= 0.0:
        return SizedIntent(
            intent=intent,
            skipped=True,
            skipped_reason=f"no available capital (available={available})",
            entry_price=market.mid(),
            reasons=["no available capital"],
        )

    leverage = int(
        intent.requested_leverage
        or effective_max_leverage
        or 1
    )
    if effective_max_leverage is not None:
        leverage = min(leverage, int(effective_max_leverage))
    leverage = max(1, leverage)

    price = float(market.mid())
    if price <= 0.0:
        return SizedIntent(
            intent=intent,
            skipped=True,
            skipped_reason=f"invalid market price {price}",
            entry_price=price,
            reasons=["invalid market price"],
        )

    # ------------------------------------------------------------------
    # Notional selection
    # ------------------------------------------------------------------
    notional_candidates: list = []
    if intent.requested_notional_usd is not None:
        notional_candidates.append(float(intent.requested_notional_usd))
        reasons.append(f"requested_notional={intent.requested_notional_usd}")

    if (
        strategy.sl_distance_pct is not None
        and strategy.sl_distance_pct > 0.0
        and strategy.risk_per_trade_pct > 0.0
    ):
        risk_usd = available * float(strategy.risk_per_trade_pct) * float(leverage)
        # risk_usd = notional * sl_distance_pct  =>  notional = risk_usd / sl_distance_pct
        risk_based_notional = risk_usd / float(strategy.sl_distance_pct)
        notional_candidates.append(risk_based_notional)
        reasons.append(
            f"risk_based_notional={risk_based_notional:.4f}"
        )

    cap_frac = max(0.0, float(strategy.max_notional_pct))
    frac_notional = available * cap_frac * float(leverage)
    notional_candidates.append(frac_notional)
    reasons.append(f"fraction_notional={frac_notional:.4f} (frac={cap_frac})")

    target_notional = min(notional_candidates)

    # Apply capability cap
    if effective_max_notional_usd is not None:
        target_notional = min(target_notional, float(effective_max_notional_usd))
        reasons.append(
            f"cap_max_notional={effective_max_notional_usd:.4f}"
        )

    # Cap by available capital * leverage
    max_available_notional = available * float(leverage)
    target_notional = min(target_notional, max_available_notional)

    target_notional = _clamp(target_notional, 0.0, None)

    # Venue minimum notional
    if (
        market.min_notional_usd is not None
        and market.min_notional_usd > 0.0
        and target_notional < market.min_notional_usd
    ):
        return SizedIntent(
            intent=intent,
            entry_price=price,
            expected_entry_price=price,
            skipped=True,
            skipped_reason=(
                f"notional {target_notional:.4f} below venue minimum "
                f"{market.min_notional_usd:.4f}"
            ),
            reasons=reasons,
        )

    # ------------------------------------------------------------------
    # Quantity
    # ------------------------------------------------------------------
    contract_size = float(market.contract_size or 1.0)
    raw_qty = target_notional / (price * contract_size)
    qty = _round_to_step(raw_qty, market.lot_step)
    if qty <= 0.0:
        return SizedIntent(
            intent=intent,
            entry_price=price,
            expected_entry_price=price,
            skipped=True,
            skipped_reason=f"rounded quantity is zero (raw={raw_qty}, lot_step={market.lot_step})",
            reasons=reasons,
        )

    final_notional = qty * price * contract_size

    # ------------------------------------------------------------------
    # Expected entry price + slippage + spread
    # ------------------------------------------------------------------
    direction_sign = 1.0 if intent.direction == "long" else -1.0
    spread_bps = float(market.spread_bps_estimate)
    slip_bps = float(market.slippage_bps_estimate)

    # Longs pay the ask, shorts hit the bid — we approximate that by
    # adding half-spread in the adverse direction.
    expected_entry = price * (1.0 + direction_sign * (spread_bps + slip_bps) / 10_000.0)

    expected_spread_usd = abs(final_notional) * spread_bps / 10_000.0
    expected_slippage_usd = abs(final_notional) * slip_bps / 10_000.0

    fee_bps = float(
        market.maker_fee_bps if strategy.prefer_maker else market.taker_fee_bps
    )
    fee_usd = abs(final_notional) * fee_bps / 10_000.0
    overnight_usd = abs(final_notional) * float(market.overnight_bps_per_day) / 10_000.0

    # ------------------------------------------------------------------
    # SL / TP
    # ------------------------------------------------------------------
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    if strategy.sl_distance_pct is not None and strategy.sl_distance_pct > 0.0:
        sl_price = expected_entry * (1.0 - direction_sign * float(strategy.sl_distance_pct))
    if strategy.tp_distance_pct is not None and strategy.tp_distance_pct > 0.0:
        tp_price = expected_entry * (1.0 + direction_sign * float(strategy.tp_distance_pct))

    return SizedIntent(
        intent=intent,
        quantity=qty,
        notional_usd=final_notional,
        leverage=leverage,
        entry_price=price,
        expected_entry_price=expected_entry,
        stop_loss_price=sl_price,
        take_profit_price=tp_price,
        expected_fee_usd=fee_usd,
        expected_spread_usd=expected_spread_usd,
        expected_slippage_usd=expected_slippage_usd,
        expected_overnight_usd_per_day=overnight_usd,
        fee_rate_bps=fee_bps,
        reasons=reasons,
    )


__all__ = [
    "MarketSnapshot",
    "AccountSnapshot",
    "StrategyConfig",
    "SizedIntent",
    "size_intent",
]
