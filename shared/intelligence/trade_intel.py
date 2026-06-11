"""
Module: trade_intel
Purpose: Detect position-management intel in tracked-trader messages and apply
         it to their open tracked_positions.

The interpretation pipeline only copies trade ENTRIES. Traders also post
management updates -- "moving SL to BE", "closed my SOL short", "taking
partials here". This module turns those messages into actions:

  * move_sl_be   -> stop_loss = entry_price on the matching open position
  * move_sl      -> stop_loss = stated price
  * close        -> status='closed', exit_reason='trader_exit'
  * partial      -> append to partial_closes jsonb, status='partial_exit'

Every applied action also writes exit_reason_trader / raw text so the
postmortem LLM sees WHAT THE TRADER DID, not just what the candles did.

Detection is pure regex (free, no LLM). Application is conservative:
  * only positions belonging to THIS trader (trader_profiles join)
  * symbol-filtered when a symbol is present in the message
  * if no symbol and the trader has >1 open position, we do NOT guess --
    the intel is logged on all candidates' exit_reason_trader as a note
    only for single-position certainty; otherwise skipped with a log line.

Location: /opt/tickles/shared/intelligence/trade_intel.py
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tickles.intelligence.trade_intel")

# ---------------------------------------------------------------------------
# Detection patterns
# ---------------------------------------------------------------------------
# SL to breakeven: "sl to be", "moving stop to breakeven", "stops at be now"
# The bare "sl to be" form (2nd alt) excludes English continuations like
# "SL to be determined/confirmed/decided/safe/announced" via negative
# lookahead, so chatter doesn't trigger a false breakeven move.
_BE_NOT_TRADING = r"(?!\s*(?:determined|confirmed|decided|set\b|safe|announced|adjusted|updated|posted|shared|revealed|honest|fair|clear))"
_SL_BE_RE = re.compile(
    r"\b(?:mov(?:e|ed|ing)?|set|put|trail(?:ed|ing)?)\s+(?:my\s+|the\s+)?"
    r"(?:sl|stop(?:\s*loss)?|stops)\s+(?:to|at|->)?\s*"
    r"(?:be|b/e|break\s*even|breakeven|entry)\b"
    r"|\b(?:sl|stop(?:\s*loss)?)\s*(?:->|to|at)\s*(?:b/e|break\s*even|breakeven)\b"
    r"|\b(?:sl|stop(?:\s*loss)?)\s*(?:->|to|at)\s*be\b" + _BE_NOT_TRADING,
    re.IGNORECASE,
)

# SL to explicit price: "moving sl to 97.5", "raise sl to 71200".
# REQUIRES an explicit movement verb (mov/set/put/raise/lower/trail) — the
# verb-less "sl at 70800" form was removed because it matched NEW trade
# setups ("BTC long entry 71500 sl at 70800"), hijacking the existing
# position and suppressing the new signal.
_SL_PRICE_RE = re.compile(
    r"\b(?:mov(?:e|ed|ing)?|set|put|rais(?:e|ed|ing)|lower(?:ed|ing)?|trail(?:ed|ing)?|push(?:ed|ing)?|bump(?:ed|ing)?)\s+"
    r"(?:my\s+|the\s+)?(?:sl|stop(?:\s*loss)?)\s+(?:now\s+)?(?:to|at|->)\s*\$?([\d]+(?:\.[\d]+)?)\b",
    re.IGNORECASE,
)

# Close: "closed my sol short", "closing btc here", "out of eth", "fully closed".
# Excludes partial-close phrasing ("close half", "closing partially", "cut in
# half") — those are routed to _PARTIAL_RE. The negative lookahead after the
# verb prevents "cut my position in half" / "closing partially" from matching
# as a FULL close.
_CLOSE_RE = re.compile(
    r"\b(?:clos(?:e|ed|ing)|exit(?:ed|ing)?|out\s+of|killed|cut)\s+"
    r"(?:my\s+|the\s+|this\s+)?(?:[a-z0-9/]{2,12}\s+)?"
    r"(?:long|short|position|trade|it)\b"
    r"|\bfully\s+closed\b|\ball\s+out\b",
    re.IGNORECASE,
)

# Partial: "taking partials", "took 50% off", "trimming here", "closing half",
# "cut in half", "closing partially", "scaling out", "off the table".
# Checked BEFORE close in detect_trade_intel, and "half"/"partial(ly)" phrasing
# is captured here so it is not mistaken for a full close.
_PARTIAL_RE = re.compile(
    r"\b(?:tak(?:e|en|ing)|took)\s+(?:some\s+|partial?s?|(\d{1,3})\s*%)\s*(?:off|profit)?\b"
    r"|\bpartials?\b|\bpartially\b|\btrim(?:med|ming)?\b|\bscal(?:e|ed|ing)\s+out\b"
    r"|\b(?:clos(?:e|ed|ing)|cut|took|take|taking|sold|selling|trim(?:med|ming)?)\b[^.!?\n]{0,30}?\bin\s+half\b"
    r"|\b(?:clos(?:e|ed|ing)|cut|took|take|taking|sold|selling)\s+(?:[a-z0-9/]{0,12}\s+)?half\b"
    r"|\boff\s+the\s+table\b",
    re.IGNORECASE,
)

# Symbol candidates inside the message (uppercase tickers + common forms)
_INTEL_SYMBOL_RE = re.compile(r"\b([A-Z]{2,8})(?:/?USDT?)?\b")
_INTEL_SYMBOL_STOP = {
    "SL", "TP", "BE", "THE", "MY", "TO", "AT", "NOW", "OFF", "OUT", "ALL",
    "LONG", "SHORT", "STOP", "LOSS", "TAKE", "PROFIT", "AND", "FOR", "ON",
    "IT", "HERE", "THIS", "WAS", "ARE", "NOT", "BUT", "YOU", "WE", "GG",
}


def detect_trade_intel(text: str) -> Optional[Dict[str, Any]]:
    """Detect a position-management instruction in free text.

    Returns None when nothing matches, otherwise::

        {"kind": "move_sl_be" | "move_sl" | "close" | "partial",
         "price": float | None,          # for move_sl
         "pct": int | None,              # for partial (when stated)
         "symbol": "SOL" | None}         # bare base if one was mentioned

    Order matters: SL-to-BE is checked before generic SL-move (BE messages
    also contain 'sl to'), and PARTIAL is preferred over CLOSE when both match
    ('closing half' / 'cut in half' / 'closing partially' are partials).
    """
    if not text or len(text) > 1500:
        return None

    intel: Optional[Dict[str, Any]] = None

    if _SL_BE_RE.search(text):
        intel = {"kind": "move_sl_be", "price": None, "pct": None}
    else:
        m = _SL_PRICE_RE.search(text)
        if m:
            price_str = m.group(1)  # single capture group (verb-required form)
            try:
                intel = {"kind": "move_sl", "price": float(price_str), "pct": None}
            except (TypeError, ValueError):
                intel = None
    if intel is None:
        pm = _PARTIAL_RE.search(text)
        cm = _CLOSE_RE.search(text)
        # PARTIAL wins over CLOSE: "closing half my SOL" matches both, but it
        # is a partial exit, not a full close. Only treat as a full close when
        # partial phrasing is absent.
        if pm:
            pct = None
            if pm.group(1):
                try:
                    pct = int(pm.group(1))
                except ValueError:
                    pct = None
            intel = {"kind": "partial", "price": None, "pct": pct}
        elif cm:
            intel = {"kind": "close", "price": None, "pct": None}

    if intel is None:
        return None

    # Try to pull a symbol out of the message
    symbol = None
    for sm in _INTEL_SYMBOL_RE.finditer(text):
        cand = sm.group(1).upper()
        if cand not in _INTEL_SYMBOL_STOP:
            symbol = cand
            break
    intel["symbol"] = symbol
    return intel


async def apply_trade_intel(
    shared_pool: Any,
    *,
    author: str,
    text: str,
    intel: Dict[str, Any],
    news_item_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Apply detected intel to the trader's open tracked_positions.

    Returns a status dict: {"applied": bool, "kind": ..., "position_ids": [...],
    "reason": str}.  Never raises -- failures are logged and reported.
    """
    kind = intel["kind"]
    symbol = intel.get("symbol")
    try:
        async with shared_pool.acquire() as conn:
            # Open/pending positions for THIS trader (by handle)
            rows = await conn.fetch(
                """
                SELECT tp2.id, tp2.instrument_symbol, tp2.direction,
                       tp2.entry_price, tp2.stop_loss, tp2.take_profit_1,
                       tp2.current_price, tp2.notional_usd, tp2.status
                FROM public.tracked_positions tp2
                JOIN public.trader_profiles t ON t.id = tp2.trader_profile_id
                WHERE LOWER(t.handle_normalized) = LOWER($1)
                  AND tp2.status IN ('open', 'pending', 'partial_exit')
                ORDER BY tp2.signal_timestamp DESC
                LIMIT 20
                """,
                author or "",
            )
            candidates = [dict(r) for r in rows]
            if symbol:
                sym_up = symbol.upper()
                candidates = [
                    c for c in candidates
                    if str(c["instrument_symbol"]).upper().startswith(sym_up)
                ]

            if not candidates:
                return {"applied": False, "kind": kind, "position_ids": [],
                        "reason": "no matching open position"}

            # Without a symbol we only act when there is exactly ONE candidate
            if not symbol and len(candidates) > 1:
                logger.info(
                    "trade_intel: %s from %s ambiguous across %d positions -- skipped",
                    kind, author, len(candidates),
                )
                return {"applied": False, "kind": kind, "position_ids": [],
                        "reason": f"ambiguous ({len(candidates)} open positions, no symbol)"}

            applied_ids: List[int] = []
            now = datetime.now(timezone.utc)
            note = (text or "")[:500]

            for pos in candidates[:3]:  # safety cap
                pid = pos["id"]
                entry = float(pos["entry_price"] or 0)
                cur = float(pos["current_price"] or 0) or entry
                notional = float(pos["notional_usd"] or 0)
                direction = (pos["direction"] or "").lower()

                if kind == "move_sl_be" and entry > 0:
                    await conn.execute(
                        """UPDATE public.tracked_positions
                           SET stop_loss = entry_price,
                               exit_reason_trader = COALESCE(exit_reason_trader,'') ||
                                   '[' || $2 || '] SL->BE: ' || $3 || E'\n'
                           WHERE id = $1 AND status IN ('open','partial_exit')""",
                        pid, now.isoformat(), note,
                    )
                elif kind == "move_sl" and intel.get("price"):
                    await conn.execute(
                        """UPDATE public.tracked_positions
                           SET stop_loss = $2,
                               exit_reason_trader = COALESCE(exit_reason_trader,'') ||
                                   '[' || $3 || '] SL moved: ' || $4 || E'\n'
                           WHERE id = $1 AND status IN ('open','partial_exit')""",
                        pid, float(intel["price"]), now.isoformat(), note,
                    )
                elif kind == "close":
                    pnl_pct = 0.0
                    if entry > 0 and cur > 0:
                        pnl_pct = ((cur - entry) / entry) * (1 if direction == "long" else -1) * 100
                    pnl_usd = (pnl_pct / 100.0) * notional
                    await conn.execute(
                        """UPDATE public.tracked_positions
                           SET status = 'closed',
                               exit_price = $2,
                               exit_timestamp = NOW(),
                               closed_at = COALESCE(closed_at, NOW()),
                               outcome = 'manual_close',
                               exit_reason = 'trader_exit',
                               exit_reason_trader = COALESCE(exit_reason_trader,'') ||
                                   '[' || $3 || '] trader closed: ' || $4 || E'\n',
                               realized_pnl_pct = $5,
                               realized_pnl_usd = $6
                           WHERE id = $1 AND status IN ('open','partial_exit')""",
                        pid, cur if cur > 0 else None, now.isoformat(), note,
                        round(pnl_pct, 4), round(pnl_usd, 8),
                    )
                elif kind == "partial":
                    entry_json = json.dumps({
                        "at": now.isoformat(),
                        "price": cur if cur > 0 else None,
                        "pct": intel.get("pct"),
                        "note": note[:200],
                        "source": "trader_message",
                    })
                    await conn.execute(
                        """UPDATE public.tracked_positions
                           SET partial_closes = COALESCE(partial_closes, '[]'::jsonb) || $2::jsonb,
                               status = CASE WHEN status = 'open' THEN 'partial_exit' ELSE status END,
                               exit_reason_trader = COALESCE(exit_reason_trader,'') ||
                                   '[' || $3 || '] partial: ' || $4 || E'\n'
                           WHERE id = $1""",
                        pid, entry_json, now.isoformat(), note,
                    )
                else:
                    continue

                # Audit trail in position_updates (allowed source: 'manual')
                try:
                    await conn.execute(
                        """INSERT INTO public.position_updates
                           (position_id, price, unrealized_pnl_pct, unrealized_pnl_usd,
                            distance_to_entry_pct, time_in_trade_minutes, update_source)
                           VALUES ($1, $2, 0, 0, 0, 0, 'manual')""",
                        pid, cur if cur > 0 else (entry or 0),
                    )
                except Exception as exc:  # audit must never block the action
                    logger.debug("trade_intel: position_updates insert failed: %s", exc)

                applied_ids.append(pid)
                logger.info(
                    "trade_intel: applied %s to position_id=%s (%s %s) from %s%s",
                    kind, pid, pos["instrument_symbol"], direction, author,
                    f" news_item={news_item_id}" if news_item_id else "",
                )

            return {"applied": bool(applied_ids), "kind": kind,
                    "position_ids": applied_ids,
                    "reason": "ok" if applied_ids else "no rows updated"}
    except Exception as exc:
        logger.warning("trade_intel: apply failed (%s from %s): %s", kind, author, exc)
        return {"applied": False, "kind": kind, "position_ids": [], "reason": str(exc)}
