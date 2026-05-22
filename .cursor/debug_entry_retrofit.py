"""
Forensic diagnostic — answer one question:
    Are pending→open activations happening BEFORE a real 1m candle touched
    the entry price, or AFTER?

This script is READ-ONLY. It does not modify dashboard / position_monitor /
copy_trade_monitor / candle daemon code. All findings are written as NDJSON
lines to /opt/tickles/.cursor/debug-c8d268.log so they can be reviewed
hypothesis-by-hypothesis.

For each recently-activated tracked_positions row (status='open' in the last
48h) we capture:
  * created_at, updated_at, signal_timestamp
  * direction, entry_price, stop_loss, take_profit_1
  * (updated_at - created_at) wall-clock gap (a sub-60s gap is suspicious)
  * The 1m candle range that contained ``updated_at`` — did its
    [low, high] window actually include entry?
  * The 1m candle BEFORE that — did entry first cross there?
  * Whether the trader's signal was a "limit" entry (entry on the opposite
    side of current price at signal time) or a "breakout" entry.

For competition_trades:
  * entered_at, entry_price, signal_timestamp of the linked tracked_position
  * Whether entered_at is suspiciously close to signal_timestamp.

Read-only. Re-runnable. No production code touched.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG_PATH = Path("/opt/tickles/.cursor/debug-c8d268.log")
SESSION_ID = "c8d268"
RUN_ID = f"retrofit-{int(time.time())}"


def log(location: str, message: str, data: Dict[str, Any] | None = None,
        *, hypothesis: str = "") -> None:
    payload = {
        "sessionId": SESSION_ID, "runId": RUN_ID, "hypothesisId": hypothesis,
        "location": location, "message": message, "data": data or {},
        "timestamp": int(time.time() * 1000),
    }
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=str) + "\n")


async def main() -> None:
    sys.path.insert(0, "/opt/tickles")
    from shared.utils.db import get_shared_pool

    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        # --- (A) recently-activated tracked_positions -----------------------
        rows = await conn.fetch(
            """
            SELECT id, instrument_symbol, instrument_exchange, direction,
                   entry_price, stop_loss, take_profit_1, signal_timestamp,
                   created_at, updated_at, status, current_price,
                   actor_id, signal_source
            FROM public.tracked_positions
            WHERE status = 'open'
              AND created_at > now() - interval '48 hours'
            ORDER BY created_at DESC
            LIMIT 200
            """
        )
        log("tp:opened_count", "open positions in last 48h",
            {"n": len(rows)}, hypothesis="H5")

        suspicious = 0
        confirmed_retro = 0
        for r in rows:
            d = dict(r)
            created = d["created_at"]
            updated = d["updated_at"]
            gap_s = None
            try:
                gap_s = (updated - created).total_seconds()
            except Exception:
                pass
            d["gap_s_created_to_updated"] = gap_s

            # Was entry inside any 1m candle's [low, high] window BETWEEN
            # created_at and updated_at? If not → retro-fit.
            candle_check = await _candle_touched_entry(
                conn,
                symbol=d["instrument_symbol"],
                exchange=d["instrument_exchange"],
                entry=float(d["entry_price"]) if d["entry_price"] is not None else None,
                start_ts=created,
                end_ts=updated,
            )
            d["entry_touched_by_candle"] = candle_check
            if gap_s is not None and gap_s < 60:
                suspicious += 1
                if candle_check.get("ok") and not candle_check.get("touched"):
                    confirmed_retro += 1

            # Was it a limit (entry on opposite side of price at signal time)
            # or breakout (entry beyond price at signal time)? Look up the
            # 1m candle that contains signal_timestamp.
            entry_class = await _classify_entry(
                conn,
                symbol=d["instrument_symbol"],
                exchange=d["instrument_exchange"],
                direction=d["direction"],
                entry=float(d["entry_price"]) if d["entry_price"] is not None else None,
                signal_ts=d["signal_timestamp"] or created,
            )
            d["entry_classification"] = entry_class

            log(
                "tp:row",
                "activation evidence for one position",
                {
                    "id": d["id"], "sym": d["instrument_symbol"],
                    "dir": d["direction"], "entry": str(d["entry_price"]),
                    "current": str(d.get("current_price")),
                    "signal_ts": str(d.get("signal_timestamp")),
                    "created_at": str(created),
                    "updated_at": str(updated),
                    "gap_s": gap_s,
                    "candle_check": candle_check,
                    "entry_classification": entry_class,
                    "actor_id": d.get("actor_id"),
                },
                hypothesis="H1+H2+H5",
            )

        log(
            "tp:summary",
            "activation summary",
            {
                "total_opened_48h": len(rows),
                "sub_60s_activation_gap": suspicious,
                "confirmed_retro_no_candle_touch": confirmed_retro,
            },
            hypothesis="H1+H2+H5",
        )

        # --- (B) competition_trades — how did the bots enter --------------
        trade_rows = await conn.fetch(
            """
            SELECT ct.id, ct.contest_id, ct.agent_id, ct.symbol,
                   ct.direction, ct.entry_price, ct.sl_price, ct.tp_price,
                   ct.entered_at, ct.tracked_position_id,
                   tp.signal_timestamp, tp.entry_price AS trader_entry,
                   tp.created_at AS tp_created_at,
                   tp.updated_at AS tp_updated_at,
                   tp.actor_id AS trader_actor
            FROM public.competition_trades ct
            LEFT JOIN public.tracked_positions tp
              ON tp.id = ct.tracked_position_id
            WHERE ct.entered_at > now() - interval '48 hours'
            ORDER BY ct.entered_at DESC
            LIMIT 100
            """
        )
        log("ct:opened_count", "competition_trades in last 48h",
            {"n": len(trade_rows)}, hypothesis="H4")

        ct_retro = 0
        for r in trade_rows:
            d = dict(r)
            entered = d["entered_at"]
            tp_created = d.get("tp_created_at")
            sig_ts = d.get("signal_timestamp")
            entry_eq = (
                d.get("entry_price") == d.get("trader_entry")
                if d.get("entry_price") is not None and d.get("trader_entry") is not None
                else None
            )
            d["bot_entry_eq_trader_entry"] = entry_eq

            # Check whether a 1m candle between tp_created_at and entered_at
            # actually contained the entry price.
            candle_check = await _candle_touched_entry(
                conn,
                symbol=d["symbol"],
                exchange=None,
                entry=float(d["entry_price"]) if d["entry_price"] is not None else None,
                start_ts=tp_created,
                end_ts=entered,
            )
            d["entry_touched_by_candle"] = candle_check
            if candle_check.get("ok") and not candle_check.get("touched"):
                ct_retro += 1

            log(
                "ct:row",
                "competition_trade entry forensics",
                {
                    "id": d["id"], "agent": d["agent_id"], "sym": d["symbol"],
                    "dir": d["direction"],
                    "bot_entry": str(d.get("entry_price")),
                    "trader_entry": str(d.get("trader_entry")),
                    "bot_eq_trader_entry": entry_eq,
                    "tp_created_at": str(tp_created),
                    "entered_at": str(entered),
                    "candle_check": candle_check,
                    "tracked_position_id": d.get("tracked_position_id"),
                },
                hypothesis="H4",
            )

        log(
            "ct:summary",
            "competition_trades retro-fit summary",
            {
                "rows": len(trade_rows),
                "confirmed_retro_no_candle_touch": ct_retro,
            },
            hypothesis="H4",
        )


async def _candle_touched_entry(
    conn, *, symbol: Optional[str], exchange: Optional[str],
    entry: Optional[float], start_ts, end_ts,
) -> Dict[str, Any]:
    """Check whether any 1m candle between start_ts and end_ts had a
    [low, high] range that contained ``entry``. Returns a dict with:
        ok: bool — whether we could perform the check at all
        touched: bool — whether ANY candle crossed entry
        n_candles: int — number of candles inspected
        first_touching: dict | None — the first candle's metadata
        latest_close: float | None
    """
    if not symbol or entry is None or start_ts is None or end_ts is None:
        return {"ok": False, "reason": "missing-inputs"}
    try:
        rows = await conn.fetch(
            """
            SELECT c.timestamp, c.open, c.high, c.low, c.close
            FROM public.candles c
            JOIN public.instruments i ON i.id = c.instrument_id
            WHERE i.symbol = $1
              AND c.timeframe = '1m'
              AND c.timestamp >= $2
              AND c.timestamp <= $3
            ORDER BY c.timestamp ASC
            LIMIT 500
            """,
            symbol, start_ts, end_ts,
        )
    except Exception as exc:
        return {"ok": False, "reason": "query-failed", "err": repr(exc)}
    n = len(rows)
    if n == 0:
        return {"ok": False, "reason": "no-candles-in-window", "n_candles": 0}
    first_touch = None
    for r in rows:
        lo = float(r["low"]); hi = float(r["high"])
        if lo <= entry <= hi:
            first_touch = {
                "timestamp": str(r["timestamp"]),
                "low": lo, "high": hi, "close": float(r["close"]),
            }
            break
    return {
        "ok": True,
        "touched": first_touch is not None,
        "n_candles": n,
        "first_touching": first_touch,
        "latest_close": float(rows[-1]["close"]),
    }


async def _classify_entry(
    conn, *, symbol: str, exchange: Optional[str],
    direction: Optional[str], entry: Optional[float], signal_ts,
) -> Dict[str, Any]:
    """Determine if entry is LIMIT (opposite side of current at signal time)
    or BREAKOUT (same side).

    Returns:
        {kind: 'limit'|'breakout'|'unknown', price_at_signal: float|None}
    """
    if not symbol or entry is None or direction is None or signal_ts is None:
        return {"kind": "unknown", "reason": "missing-inputs"}
    row = await conn.fetchrow(
        """
        SELECT c.close
        FROM public.candles c
        JOIN public.instruments i ON i.id = c.instrument_id
        WHERE i.symbol = $1
          AND c.timeframe = '1m'
          AND c.timestamp <= $2
        ORDER BY c.timestamp DESC
        LIMIT 1
        """,
        symbol, signal_ts,
    )
    if row is None:
        return {"kind": "unknown", "reason": "no-candle-at-signal"}
    price_at_signal = float(row["close"])
    direction = direction.lower() if direction else None
    if direction == "long":
        # LIMIT-buy: entry < price (waiting to dip)
        # BREAKOUT-buy: entry > price (waiting to break up)
        kind = "limit_below" if entry < price_at_signal else (
            "breakout_above" if entry > price_at_signal else "at_market"
        )
    elif direction == "short":
        kind = "limit_above" if entry > price_at_signal else (
            "breakout_below" if entry < price_at_signal else "at_market"
        )
    else:
        kind = "unknown"
    return {"kind": kind, "price_at_signal": price_at_signal}


if __name__ == "__main__":
    asyncio.run(main())
