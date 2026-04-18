"""
shared.candles.coverage — async read-only coverage queries.

Used by the CLI (`candles_cli coverage`) and by Phase 15's cache
invalidation hooks.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shared.candles.schema import CoverageSummary, Timeframe

log = logging.getLogger("tickles.candles.coverage")


async def list_coverage(
    pool: Any,
    *,
    instrument_id: Optional[int] = None,
    timeframe: Optional[Timeframe] = None,
    source: Optional[str] = None,
    active_only: bool = True,
    include_empty: bool = False,
) -> List[CoverageSummary]:
    """Per-(instrument, source, timeframe) coverage summary.

    The result is a thin roll-up of the `candles` table joined to
    `instruments` to carry the symbol / exchange for display.
    """
    log.debug(
        "list_coverage(instrument_id=%s, timeframe=%s, source=%s, active_only=%s)",
        instrument_id, timeframe, source, active_only,
    )

    clauses: List[str] = []
    params: List[Any] = []
    if instrument_id is not None:
        params.append(instrument_id)
        clauses.append(f"c.instrument_id = ${len(params)}")
    if timeframe is not None:
        params.append(timeframe.value)
        clauses.append(f"c.timeframe = ${len(params)}::timeframe_t")
    if source is not None:
        params.append(source)
        clauses.append(f"c.source = ${len(params)}")
    if active_only:
        clauses.append("i.is_active = TRUE")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    sql = f"""
        SELECT c.instrument_id,
               i.symbol,
               i.exchange,
               c.source,
               c.timeframe,
               COUNT(*) AS bars,
               MIN(c."timestamp") AS first_ts,
               MAX(c."timestamp") AS last_ts
          FROM candles c
          JOIN instruments i ON i.id = c.instrument_id
          {where}
         GROUP BY c.instrument_id, i.symbol, i.exchange, c.source, c.timeframe
         ORDER BY i.exchange, i.symbol, c.timeframe, c.source
    """
    rows = await pool.fetch_all(sql, tuple(params) if params else None)
    now = datetime.now(timezone.utc)
    out: List[CoverageSummary] = []
    for r in rows:
        last = r["last_ts"]
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        fresh_lag: Optional[int] = None
        if last is not None:
            fresh_lag = int((now - last).total_seconds() // 60)
        out.append(CoverageSummary(
            instrument_id=int(r["instrument_id"]),
            symbol=r.get("symbol") if hasattr(r, "get") else r["symbol"],
            exchange=r.get("exchange") if hasattr(r, "get") else r["exchange"],
            source=r["source"],
            timeframe=Timeframe(r["timeframe"]),
            bars=int(r["bars"]),
            first_ts=r["first_ts"],
            last_ts=last,
            fresh_lag_minutes=fresh_lag,
        ))

    if include_empty and instrument_id is not None and timeframe is not None:
        # Make sure the caller gets at least one row per requested tuple,
        # zero-filled when the table has no candles yet.
        if not out:
            row = await pool.fetch_one(
                "SELECT symbol, exchange FROM instruments WHERE id=$1",
                (instrument_id,),
            )
            if row:
                out.append(CoverageSummary(
                    instrument_id=instrument_id,
                    symbol=row["symbol"], exchange=row["exchange"],
                    source=source or "unknown",
                    timeframe=timeframe, bars=0,
                    first_ts=None, last_ts=None, fresh_lag_minutes=None,
                ))
    return out


async def coverage_stats(pool: Any) -> Dict[str, int]:
    """High-level counts for the CLI `candles_cli stats` command."""
    log.debug("coverage_stats()")
    rows = await pool.fetch_all(
        """
        SELECT 'candles_total' AS k, COUNT(*) AS v FROM candles
         UNION ALL SELECT 'candles_1m',   COUNT(*) FROM candles WHERE timeframe='1m'
         UNION ALL SELECT 'candles_5m',   COUNT(*) FROM candles WHERE timeframe='5m'
         UNION ALL SELECT 'candles_15m',  COUNT(*) FROM candles WHERE timeframe='15m'
         UNION ALL SELECT 'candles_30m',  COUNT(*) FROM candles WHERE timeframe='30m'
         UNION ALL SELECT 'candles_1h',   COUNT(*) FROM candles WHERE timeframe='1h'
         UNION ALL SELECT 'candles_4h',   COUNT(*) FROM candles WHERE timeframe='4h'
         UNION ALL SELECT 'candles_1d',   COUNT(*) FROM candles WHERE timeframe='1d'
         UNION ALL SELECT 'candles_1w',   COUNT(*) FROM candles WHERE timeframe='1w'
         UNION ALL SELECT 'instruments_with_candles',
                  COUNT(DISTINCT instrument_id) FROM candles
         UNION ALL SELECT 'sources_used',
                  COUNT(DISTINCT source) FROM candles
        """
    )
    return {r["k"]: int(r["v"]) for r in rows}
