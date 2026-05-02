"""
Module: backfill_orphan_positions
Purpose: Retroactively resolve open positions with NULL entry_price/position_size
         to a real entry, walk forward through 1m candles, close at first hit
         (SL/TP/expiry/now), and persist a D7-compliant realized P&L.
Location: /opt/tickles/shared/scripts/backfill_orphan_positions.py

Implements directive D8 (2026-05-02): the 77 NULL-entry positions are real
trader signals — try to resolve P&L from candle data rather than discarding.

Design:
  1. Fetch open orphans where ``entry_price IS NULL`` OR ``position_size IS NULL``
     AND ``realized_pnl_usd_final IS NULL`` (idempotent).
  2. For each orphan:
     a. Resolve ``instrument_id`` via the shared resolver (D6 slash-form).
     b. Try to parse entry/SL/TP from ``raw_signal_text`` (regex). Sanity-check
        parsed entry against the close candle at ``signal_timestamp`` — must be
        within ±20% to be accepted (rejects garbage parses).
     c. Fallback: ``entry_price = candle.close`` at ``signal_timestamp`` (1m,
        exact match preferred, else nearest prior within 5 minutes).
     d. Derive ``qty`` from ``notional_usd / entry_price`` when missing.
     e. Walk forward 1m candles from ``signal_timestamp``. Close on first of:
        SL hit (low<=SL for long, high>=SL for short), TP hit (mirror), the
        position's ``expiry_at`` if set, or ``now()``.
     f. Compute realized P&L via fee_calc.compute_realized_pnl_db (D7 — full
        fees, spread, overnight funding).
     g. UPDATE the row with status, outcome, closed_at, exit_price,
        realized_pnl_usd, realized_pnl_usd_final, status_reason.

  CLI:
    --dry-run (default)     no DB writes, prints planned actions
    --apply                 actually write
    --limit N               cap rows processed (default: all)
    --position-id ID        single-position mode (overrides --limit)

  Acceptance per D8: ≥50/77 orphans get a non-NULL realized_pnl_usd_final.
"""

import argparse
import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from shared.intelligence.fee_calc import (
    RealizedPnlBreakdown,
    compute_realized_pnl_db,
)
from shared.intelligence.position_monitor import _resolve_instrument_id
from shared.utils.db import DatabasePool, get_shared_pool

logger = logging.getLogger(__name__)

# Constants — all caps, no hardcoded magic in function bodies
SANITY_BAND_PCT = Decimal("0.20")  # parsed entry must be within ±20% of candle
CANDLE_NEAREST_TOLERANCE_MIN = 5  # minutes to look back if exact 1m candle missing
WALK_FORWARD_TIMEFRAME = "1m"
DEFAULT_LEVERAGE = Decimal("1")
DEFAULT_FEE_TIER = "taker"
STATUS_REASON = "backfilled_retrospective"
ENTRY_REASON_BACKFILLED = "backfilled_from_candle"
ENTRY_REASON_PARSED = "backfilled_from_signal_text"

# Outcome strings — must match tracked_positions.outcome check constraint:
# 'tp1_hit' | 'tp2_hit' | 'tp3_hit' | 'sl_hit' | 'breakeven' |
# 'expired' | 'manual_close' | 'invalidated'
OUTCOME_TP1 = "tp1_hit"
OUTCOME_SL = "sl_hit"
OUTCOME_EXPIRED = "expired"
OUTCOME_MANUAL = "manual_close"  # used for "still open at now()" backfill exit

# Status strings — must match tracked_positions.status check constraint:
# 'open' | 'partial_exit' | 'closed' | 'expired' | 'invalidated' | 'cancelled'
STATUS_CLOSED = "closed"
STATUS_EXPIRED = "expired"
STATUS_INVALIDATED = "invalidated"

# Regex patterns for raw_signal_text parsing.
# Matches "entry: 78000", "entry 78,000.50", "Entry@78000", "long @78k" etc.
# The number token accepts either:
#   - A plain integer/decimal: ``78000`` or ``78000.50`` (most common)
#   - A grouped form with comma/space separators: ``78,000`` or ``78 000``
#   - An optional ``k``/``m`` suffix: ``78k``, ``1.2m``
# Plain digits come first to avoid ``78000`` being captured as ``780``.
_NUMBER_TOKEN = (
    r"(?:[0-9]{1,3}(?:[, ][0-9]{3})+|[0-9]+)(?:\.[0-9]+)?[kKmM]?"
)
RE_ENTRY = re.compile(
    rf"(?:entry|enter|long|short|buy|sell)\s*[:@\s]\s*\$?\s*({_NUMBER_TOKEN})",
    re.IGNORECASE,
)
RE_SL = re.compile(
    rf"(?:sl|stop[- ]?loss|stop)\s*[:@\s]\s*\$?\s*({_NUMBER_TOKEN})",
    re.IGNORECASE,
)
RE_TP = re.compile(
    rf"(?:tp1?|take[- ]?profit|target)\s*[:@\s]\s*\$?\s*({_NUMBER_TOKEN})",
    re.IGNORECASE,
)


@dataclass
class BackfillPlan:
    """Resolved plan for a single orphan position before any DB write.

    Attributes:
        position_id: tracked_positions.id
        instrument_id: resolved instruments.id
        direction: 'long' or 'short'
        entry_price: Decimal entry (parsed or candle-derived)
        entry_source: 'parsed' or 'candle'
        stop_loss: Decimal SL or None
        take_profit_1: Decimal TP1 or None
        qty: Decimal position size in base units
        opened_at: tz-aware UTC datetime
        expiry_at: tz-aware UTC datetime or None
        closed_at: tz-aware UTC datetime (walk-forward exit moment)
        exit_price: Decimal at close
        outcome: outcome string (tp1_hit / sl_hit / expired / manual_close)
        breakdown: RealizedPnlBreakdown from D7 fee math
        notes: human-readable explanation for the operator
    """

    position_id: int
    instrument_id: int
    direction: str
    entry_price: Decimal
    entry_source: str
    stop_loss: Optional[Decimal]
    take_profit_1: Optional[Decimal]
    qty: Decimal
    opened_at: datetime
    expiry_at: Optional[datetime]
    closed_at: datetime
    exit_price: Decimal
    outcome: str
    breakdown: RealizedPnlBreakdown
    notes: str
    reason_frozen: bool = False


@dataclass
class BackfillFailure:
    """Reason an orphan could not be resolved.

    Attributes:
        position_id: tracked_positions.id
        reason: short machine-readable reason code
        detail: human-readable explanation
    """

    position_id: int
    reason: str
    detail: str


def _to_decimal(value: Any) -> Optional[Decimal]:
    """Coerce a value to Decimal, returning None when not parseable.

    Args:
        value: Any numeric-like input.

    Returns:
        Decimal or None.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_number_token(token: str) -> Optional[Decimal]:
    """Convert a regex-matched price token (e.g. ``'78,000.50'``, ``'78k'``) to Decimal.

    Args:
        token: Raw matched substring.

    Returns:
        Decimal value or None when un-parseable.
    """
    try:
        cleaned = token.strip().replace(",", "").replace(" ", "")
        multiplier = Decimal("1")
        if cleaned.lower().endswith("k"):
            multiplier = Decimal("1000")
            cleaned = cleaned[:-1]
        elif cleaned.lower().endswith("m"):
            multiplier = Decimal("1000000")
            cleaned = cleaned[:-1]
        return Decimal(cleaned) * multiplier
    except (InvalidOperation, ValueError, AttributeError):
        return None


def parse_levels_from_text(
    raw_text: Optional[str],
) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    """Extract (entry, stop_loss, take_profit_1) from a free-text trader signal.

    Args:
        raw_text: Free-form signal text or None.

    Returns:
        Tuple of (entry, sl, tp1), each Decimal or None.
    """
    if not raw_text:
        return (None, None, None)
    try:
        m_entry = RE_ENTRY.search(raw_text)
        m_sl = RE_SL.search(raw_text)
        m_tp = RE_TP.search(raw_text)
        entry = _parse_number_token(m_entry.group(1)) if m_entry else None
        sl = _parse_number_token(m_sl.group(1)) if m_sl else None
        tp = _parse_number_token(m_tp.group(1)) if m_tp else None
        return (entry, sl, tp)
    except (AttributeError, IndexError) as exc:
        logger.warning("parse_levels_from_text: regex error: %s", exc)
        return (None, None, None)


async def fetch_orphans(
    pool: DatabasePool,
    *,
    limit: Optional[int] = None,
    position_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fetch open positions that need backfilling.

    Idempotent: only returns rows where realized_pnl_usd_final IS NULL.

    Args:
        pool: Shared Postgres pool.
        limit: Optional cap on rows returned.
        position_id: Optional single-position filter (overrides limit).

    Returns:
        List of orphan row dicts.
    """
    where = [
        "status = 'open'",
        "(entry_price IS NULL OR position_size IS NULL)",
        "realized_pnl_usd_final IS NULL",
    ]
    params: List[Any] = []
    if position_id is not None:
        where.append(f"id = ${len(params) + 1}")
        params.append(position_id)

    sql = f"""
        SELECT id, trader_profile_id, signal_interpretation_id,
               instrument_symbol, instrument_exchange, epic_code,
               direction, entry_price, position_size, leverage,
               stop_loss, take_profit_1, take_profit_2, take_profit_3,
               raw_signal_text, signal_timestamp, expiry_at,
               notional_usd, created_at, updated_at,
               entry_reason_trader, entry_reason_frozen_at, status
        FROM public.tracked_positions
        WHERE {" AND ".join(where)}
        ORDER BY signal_timestamp ASC
    """
    if limit is not None and position_id is None:
        sql += f" LIMIT ${len(params) + 1}"
        params.append(limit)

    try:
        return await pool.fetch_all(sql, tuple(params))
    except Exception as exc:
        logger.exception("fetch_orphans: DB error: %s", exc)
        raise


async def fetch_candle_at(
    pool: DatabasePool,
    instrument_id: int,
    at: datetime,
    *,
    timeframe: str = WALK_FORWARD_TIMEFRAME,
    tolerance_minutes: int = CANDLE_NEAREST_TOLERANCE_MIN,
) -> Optional[Dict[str, Any]]:
    """Fetch the candle at-or-just-before a given timestamp.

    Args:
        pool: Shared Postgres pool.
        instrument_id: Target instrument.
        at: tz-aware UTC datetime.
        timeframe: Candle timeframe enum value.
        tolerance_minutes: How far back to look if exact match missing.

    Returns:
        Candle row dict or None.
    """
    try:
        return await pool.fetch_one(
            """
            SELECT timestamp, open, high, low, close
            FROM public.candles
            WHERE instrument_id = $1
              AND timeframe = $2::timeframe_t
              AND timestamp <= $3
              AND timestamp >= $3 - ($4 || ' minutes')::interval
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (instrument_id, timeframe, at, str(tolerance_minutes)),
        )
    except Exception as exc:
        logger.exception(
            "fetch_candle_at: instrument_id=%s at=%s: %s", instrument_id, at, exc
        )
        return None


async def fetch_walk_forward_candles(
    pool: DatabasePool,
    instrument_id: int,
    start: datetime,
    end: datetime,
    *,
    timeframe: str = WALK_FORWARD_TIMEFRAME,
) -> List[Dict[str, Any]]:
    """Return all 1m candles in [start, end] for the instrument, oldest first.

    Args:
        pool: Shared Postgres pool.
        instrument_id: Target instrument.
        start: Inclusive lower bound (tz-aware).
        end: Inclusive upper bound (tz-aware).
        timeframe: Candle timeframe enum value.

    Returns:
        List of candle row dicts (may be empty).
    """
    try:
        return await pool.fetch_all(
            """
            SELECT timestamp, open, high, low, close
            FROM public.candles
            WHERE instrument_id = $1
              AND timeframe = $2::timeframe_t
              AND timestamp >= $3
              AND timestamp <= $4
            ORDER BY timestamp ASC
            """,
            (instrument_id, timeframe, start, end),
        )
    except Exception as exc:
        logger.exception(
            "fetch_walk_forward_candles: instrument_id=%s: %s", instrument_id, exc
        )
        return []


def _check_hit(
    direction: str,
    candle: Dict[str, Any],
    sl: Optional[Decimal],
    tp: Optional[Decimal],
) -> Tuple[Optional[str], Optional[Decimal]]:
    """Determine whether a candle hits SL or TP for the given direction.

    For longs: SL hit when low <= sl, TP hit when high >= tp.
    For shorts: SL hit when high >= sl, TP hit when low <= tp.
    SL takes priority over TP if both occur in the same candle (conservative).

    Args:
        direction: 'long' or 'short'.
        candle: Row with 'high' and 'low' Decimal fields.
        sl: Stop-loss price or None.
        tp: Take-profit price or None.

    Returns:
        Tuple of (outcome, exit_price) or (None, None) when neither hit.
    """
    high = _to_decimal(candle.get("high"))
    low = _to_decimal(candle.get("low"))
    if high is None or low is None:
        return (None, None)
    if direction == "long":
        if sl is not None and low <= sl:
            return (OUTCOME_SL, sl)
        if tp is not None and high >= tp:
            return (OUTCOME_TP1, tp)
    else:  # short
        if sl is not None and high >= sl:
            return (OUTCOME_SL, sl)
        if tp is not None and low <= tp:
            return (OUTCOME_TP1, tp)
    return (None, None)


def _walk_forward(
    direction: str,
    candles: List[Dict[str, Any]],
    sl: Optional[Decimal],
    tp: Optional[Decimal],
    expiry_at: Optional[datetime],
) -> Tuple[str, Decimal, datetime]:
    """Walk forward through candles to find the first close trigger.

    Trigger priority within the candle window: SL/TP intra-candle hit,
    expiry boundary, end-of-data (returns the last candle's close as
    'manual_close' marker — the position is still open at the data edge).

    Args:
        direction: 'long' or 'short'.
        candles: 1m candles ordered oldest first. Must be non-empty.
        sl: Stop-loss price or None.
        tp: Take-profit price or None.
        expiry_at: Position expiry timestamp or None.

    Returns:
        Tuple of (outcome, exit_price, closed_at).
    """
    for candle in candles:
        candle_ts: datetime = candle["timestamp"]
        if expiry_at is not None and candle_ts >= expiry_at:
            close_px = _to_decimal(candle.get("close")) or Decimal("0")
            return (OUTCOME_EXPIRED, close_px, expiry_at)
        outcome, exit_px = _check_hit(direction, candle, sl, tp)
        if outcome is not None and exit_px is not None:
            return (outcome, exit_px, candle_ts)
    # No trigger hit — close at last candle (we walked up to "now")
    last = candles[-1]
    last_close = _to_decimal(last.get("close")) or Decimal("0")
    return (OUTCOME_MANUAL, last_close, last["timestamp"])


def _validate_parsed_entry(
    parsed_entry: Decimal,
    candle_close: Decimal,
) -> bool:
    """Reject parsed entries that disagree with market price by more than ±20%.

    Args:
        parsed_entry: Entry price from regex parse.
        candle_close: Close price at signal time from candle data.

    Returns:
        True when the parsed entry is within the sanity band.
    """
    if candle_close <= 0 or parsed_entry <= 0:
        return False
    deviation = abs(parsed_entry - candle_close) / candle_close
    return deviation <= SANITY_BAND_PCT


async def plan_one(
    pool: DatabasePool,
    orphan: Dict[str, Any],
) -> Tuple[Optional[BackfillPlan], Optional[BackfillFailure]]:
    """Build a complete backfill plan for one orphan, no DB writes.

    Args:
        pool: Shared Postgres pool.
        orphan: Row dict from fetch_orphans.

    Returns:
        Tuple of (plan, failure). Exactly one is non-None.
    """
    pos_id = int(orphan["id"])
    direction = orphan["direction"]
    if direction not in ("long", "short"):
        return (
            None,
            BackfillFailure(pos_id, "bad_direction", f"direction={direction!r}"),
        )

    instrument_id = await _resolve_instrument_id(
        pool, orphan["instrument_symbol"], orphan.get("instrument_exchange")
    )
    if instrument_id is None:
        return (
            None,
            BackfillFailure(
                pos_id,
                "unresolved_instrument",
                f"symbol={orphan['instrument_symbol']!r} exchange={orphan.get('instrument_exchange')!r}",
            ),
        )

    signal_ts = orphan.get("signal_timestamp") or orphan.get(
        "entry_reason_frozen_at"
    ) or orphan["created_at"]
    if signal_ts.tzinfo is None:
        signal_ts = signal_ts.replace(tzinfo=timezone.utc)

    candle_at = await fetch_candle_at(pool, instrument_id, signal_ts)
    if candle_at is None:
        return (
            None,
            BackfillFailure(
                pos_id,
                "no_candle_at_signal",
                f"instrument_id={instrument_id} signal_ts={signal_ts.isoformat()}",
            ),
        )
    candle_close = _to_decimal(candle_at["close"])
    if candle_close is None or candle_close <= 0:
        return (
            None,
            BackfillFailure(
                pos_id, "bad_candle_close", f"close={candle_at.get('close')!r}"
            ),
        )

    # Step a: try parsing levels from raw_signal_text
    parsed_entry, parsed_sl, parsed_tp = parse_levels_from_text(
        orphan.get("raw_signal_text")
    )

    entry_price: Decimal
    entry_source: str
    if parsed_entry is not None and _validate_parsed_entry(parsed_entry, candle_close):
        entry_price = parsed_entry
        entry_source = "parsed"
    else:
        entry_price = candle_close
        entry_source = "candle"

    stop_loss = _to_decimal(orphan.get("stop_loss")) or parsed_sl
    take_profit = _to_decimal(orphan.get("take_profit_1")) or parsed_tp

    # Step d: derive qty
    pos_size = _to_decimal(orphan.get("position_size"))
    notional_usd = _to_decimal(orphan.get("notional_usd"))
    if pos_size is not None and pos_size > 0:
        qty = pos_size
    elif notional_usd is not None and notional_usd > 0:
        qty = notional_usd / entry_price
    else:
        return (
            None,
            BackfillFailure(
                pos_id,
                "no_size_no_notional",
                "neither position_size nor notional_usd set",
            ),
        )

    leverage = _to_decimal(orphan.get("leverage")) or DEFAULT_LEVERAGE

    # Step e: walk forward
    now = datetime.now(timezone.utc)
    expiry_at = orphan.get("expiry_at")
    if expiry_at is not None and expiry_at.tzinfo is None:
        expiry_at = expiry_at.replace(tzinfo=timezone.utc)
    walk_end = min(expiry_at, now) if expiry_at is not None else now

    candles = await fetch_walk_forward_candles(
        pool, instrument_id, signal_ts, walk_end
    )
    if not candles:
        return (
            None,
            BackfillFailure(
                pos_id,
                "no_walk_candles",
                f"instrument_id={instrument_id} window={signal_ts.isoformat()}..{walk_end.isoformat()}",
            ),
        )

    outcome, exit_price, closed_at = _walk_forward(
        direction, candles, stop_loss, take_profit, expiry_at
    )

    # Step f: D7 fee math
    breakdown = await compute_realized_pnl_db(
        pool,
        instrument_id=instrument_id,
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        qty=qty,
        leverage=leverage,
        opened_at=signal_ts,
        closed_at=closed_at,
        fee_tier=DEFAULT_FEE_TIER,
    )
    if breakdown is None:
        return (
            None,
            BackfillFailure(
                pos_id,
                "fee_calc_failed",
                f"instrument_id={instrument_id} (no fee profile or invalid input)",
            ),
        )

    # Detect whether the orphan already has a frozen entry_reason — the DB has a
    # trigger that rejects any UPDATE touching entry_reason_* once they are set,
    # even when the new value would be a no-op COALESCE. We must skip those
    # columns entirely on already-frozen rows.
    reason_frozen = orphan.get("entry_reason_frozen_at") is not None or bool(
        (orphan.get("entry_reason_trader") or "").strip()
    )

    notes = (
        f"entry_source={entry_source} entry={entry_price} sl={stop_loss} tp={take_profit} "
        f"qty={qty} outcome={outcome} exit={exit_price} "
        f"pnl_net={breakdown.net_pnl_usd} reason_frozen={reason_frozen}"
    )
    plan = BackfillPlan(
        position_id=pos_id,
        instrument_id=instrument_id,
        direction=direction,
        entry_price=entry_price,
        entry_source=entry_source,
        stop_loss=stop_loss,
        take_profit_1=take_profit,
        qty=qty,
        opened_at=signal_ts,
        expiry_at=expiry_at,
        closed_at=closed_at,
        exit_price=exit_price,
        outcome=outcome,
        breakdown=breakdown,
        notes=notes,
        reason_frozen=reason_frozen,
    )
    return (plan, None)


async def apply_plan(pool: DatabasePool, plan: BackfillPlan) -> None:
    """Persist a BackfillPlan to tracked_positions in a single UPDATE.

    Writes:
      - entry_price, position_size (the resolved values)
      - status (closed or expired)
      - outcome
      - exit_price, exit_timestamp, closed_at, updated_at
      - realized_pnl_usd (running tally) + realized_pnl_usd_final (canonical)
      - status_reason='backfilled_retrospective'
      - entry_reason_trader (preserved if set, else marked as backfilled)

    Args:
        pool: Shared Postgres pool.
        plan: Resolved BackfillPlan.

    Raises:
        Exception: re-raises any DB error after logging.
    """
    if plan.outcome == OUTCOME_EXPIRED:
        new_status = STATUS_EXPIRED
    else:
        new_status = STATUS_CLOSED

    entry_reason = (
        ENTRY_REASON_PARSED if plan.entry_source == "parsed" else ENTRY_REASON_BACKFILLED
    )
    net_pnl_float = float(plan.breakdown.net_pnl_usd)
    try:
        if plan.reason_frozen:
            # The orphan already has frozen entry_reason_* fields — a DB trigger
            # rejects ANY UPDATE that touches those columns once frozen. Skip
            # them entirely and only write settlement columns.
            await pool.execute(
                """
                UPDATE public.tracked_positions
                SET entry_price          = $1,
                    position_size        = $2,
                    stop_loss            = COALESCE(stop_loss, $3),
                    take_profit_1        = COALESCE(take_profit_1, $4),
                    status               = $5,
                    outcome               = $6,
                    exit_price            = $7,
                    exit_timestamp        = $8,
                    closed_at             = $8,
                    updated_at            = NOW(),
                    realized_pnl_usd      = $9,
                    realized_pnl_usd_final = $9,
                    status_reason         = $10
                WHERE id = $11
                  AND realized_pnl_usd_final IS NULL
                """,
                (
                    plan.entry_price,
                    plan.qty,
                    plan.stop_loss,
                    plan.take_profit_1,
                    new_status,
                    plan.outcome,
                    plan.exit_price,
                    plan.closed_at,
                    net_pnl_float,
                    STATUS_REASON,
                    plan.position_id,
                ),
            )
        else:
            await pool.execute(
                """
                UPDATE public.tracked_positions
                SET entry_price          = $1,
                    position_size        = $2,
                    stop_loss            = COALESCE(stop_loss, $3),
                    take_profit_1        = COALESCE(take_profit_1, $4),
                    status               = $5,
                    outcome              = $6,
                    exit_price           = $7,
                    exit_timestamp       = $8,
                    closed_at            = $8,
                    updated_at           = NOW(),
                    realized_pnl_usd     = $9,
                    realized_pnl_usd_final = $9,
                    status_reason        = $10,
                    entry_reason_trader  = COALESCE(NULLIF(entry_reason_trader, ''), $11),
                    entry_reason_frozen_at = COALESCE(entry_reason_frozen_at, $12)
                WHERE id = $13
                  AND realized_pnl_usd_final IS NULL
                """,
                (
                    plan.entry_price,
                    plan.qty,
                    plan.stop_loss,
                    plan.take_profit_1,
                    new_status,
                    plan.outcome,
                    plan.exit_price,
                    plan.closed_at,
                    net_pnl_float,
                    STATUS_REASON,
                    entry_reason,
                    plan.opened_at,
                    plan.position_id,
                ),
            )
    except Exception as exc:
        logger.exception("apply_plan: UPDATE failed for id=%s: %s", plan.position_id, exc)
        raise


async def run(
    *,
    apply: bool,
    limit: Optional[int],
    position_id: Optional[int],
) -> Dict[str, Any]:
    """Top-level entry point. Returns summary stats.

    Args:
        apply: When False, dry-run only.
        limit: Optional row cap.
        position_id: Optional single-row filter.

    Returns:
        Dict with counts and per-failure details for the operator.
    """
    pool = await get_shared_pool()
    orphans = await fetch_orphans(pool, limit=limit, position_id=position_id)
    logger.info("Found %s orphan position(s) to evaluate", len(orphans))

    plans: List[BackfillPlan] = []
    failures: List[BackfillFailure] = []
    for orphan in orphans:
        try:
            plan, failure = await plan_one(pool, orphan)
        except Exception as exc:
            logger.exception("plan_one crashed for id=%s: %s", orphan.get("id"), exc)
            failures.append(
                BackfillFailure(int(orphan["id"]), "plan_crash", repr(exc))
            )
            continue
        if plan is not None:
            plans.append(plan)
        elif failure is not None:
            failures.append(failure)

    logger.info(
        "Plans built: %s resolved, %s failed (limit=%s position_id=%s)",
        len(plans),
        len(failures),
        limit,
        position_id,
    )

    applied = 0
    if apply:
        for plan in plans:
            try:
                await apply_plan(pool, plan)
                applied += 1
                logger.info(
                    "APPLIED id=%s outcome=%s pnl_final=%s notes=%s",
                    plan.position_id,
                    plan.outcome,
                    plan.breakdown.net_pnl_usd,
                    plan.notes,
                )
            except Exception:
                # already logged in apply_plan; carry on with next
                continue
    else:
        for plan in plans:
            logger.info(
                "DRY-RUN id=%s outcome=%s pnl_final=%s notes=%s",
                plan.position_id,
                plan.outcome,
                plan.breakdown.net_pnl_usd,
                plan.notes,
            )

    for failure in failures:
        logger.warning(
            "FAIL id=%s reason=%s detail=%s",
            failure.position_id,
            failure.reason,
            failure.detail,
        )

    return {
        "evaluated": len(orphans),
        "resolved": len(plans),
        "failed": len(failures),
        "applied": applied,
        "dry_run": not apply,
        "failures": [
            {"position_id": f.position_id, "reason": f.reason, "detail": f.detail}
            for f in failures
        ],
    }


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments for the backfill script.

    Args:
        argv: Optional argv override (for tests).

    Returns:
        Parsed argparse.Namespace.
    """
    parser = argparse.ArgumentParser(
        description="Backfill realized P&L for orphan tracked_positions (D8)."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Plan only, do not write (default).",
    )
    group.add_argument(
        "--apply",
        action="store_true",
        help="Persist resolved plans to the database.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap rows processed (default: all orphans).",
    )
    parser.add_argument(
        "--position-id",
        type=int,
        default=None,
        help="Process only this single tracked_positions.id.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level.",
    )
    return parser.parse_args(argv)


async def _amain(argv: Optional[List[str]] = None) -> int:
    """Async entrypoint used by ``main``.

    Args:
        argv: Optional argv override (for tests).

    Returns:
        Process exit code.
    """
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    summary = await run(
        apply=args.apply,
        limit=args.limit,
        position_id=args.position_id,
    )
    logger.info(
        "SUMMARY evaluated=%s resolved=%s failed=%s applied=%s dry_run=%s",
        summary["evaluated"],
        summary["resolved"],
        summary["failed"],
        summary["applied"],
        summary["dry_run"],
    )
    return 0 if summary["failed"] == 0 else 1


def main() -> None:
    """Synchronous wrapper around the async entrypoint."""
    try:
        rc = asyncio.run(_amain())
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
