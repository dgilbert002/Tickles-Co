"""
shared.candles.resample — SQL-native 1m -> Nm/Nh/Nd rollups.

We do NOT stream candles through Python. Instead we issue one
`INSERT ... SELECT ... GROUP BY date_trunc(...) ON CONFLICT DO UPDATE`
per (instrument, source, target_timeframe). Postgres handles the grouping.

The bucket math is intentionally explicit (no TimescaleDB dependency):

    5m, 15m, 30m  ->  minute_floor = ts - INTERVAL '1 minute' *
                       ((minute(ts) % N) + 1) + INTERVAL '1 minute'
                      (cheaper to write as date_trunc('minute', ts) -
                       INTERVAL '1 minute' * (minute % N))
    1h            ->  date_trunc('hour', ts)
    4h            ->  date_trunc('hour', ts) -
                       INTERVAL '1 hour' * (hour(ts) % 4)
    1d            ->  date_trunc('day', ts)
    1w            ->  date_trunc('week', ts)   -- ISO week

For OHLC aggregation we use:

    open  = (array_agg(open  ORDER BY ts ASC))[1]
    close = (array_agg(close ORDER BY ts DESC))[1]
    high  = MAX(high)
    low   = MIN(low)
    volume= SUM(volume)

This is portable across vanilla Postgres; timescale's `first()`/`last()`
would be faster but we're not committing to that dependency yet.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from shared.candles.schema import RESAMPLE_CHAIN, ResampleReport, Timeframe

log = logging.getLogger("tickles.candles.resample")

# Bucket-floor SQL per target timeframe. $ts$ is a placeholder for the
# base column reference; we substitute it so the same template serves
# both live and backtest code paths.
_BUCKET_FLOOR_SQL: Dict[str, str] = {
    Timeframe.M5.value:  (
        "date_trunc('minute', \"timestamp\") "
        "- INTERVAL '1 minute' * (EXTRACT(MINUTE FROM \"timestamp\")::int % 5)"
    ),
    Timeframe.M15.value: (
        "date_trunc('minute', \"timestamp\") "
        "- INTERVAL '1 minute' * (EXTRACT(MINUTE FROM \"timestamp\")::int % 15)"
    ),
    Timeframe.M30.value: (
        "date_trunc('minute', \"timestamp\") "
        "- INTERVAL '1 minute' * (EXTRACT(MINUTE FROM \"timestamp\")::int % 30)"
    ),
    Timeframe.H1.value:  "date_trunc('hour', \"timestamp\")",
    Timeframe.H4.value:  (
        "date_trunc('hour', \"timestamp\") "
        "- INTERVAL '1 hour' * (EXTRACT(HOUR FROM \"timestamp\")::int % 4)"
    ),
    Timeframe.D1.value:  "date_trunc('day', \"timestamp\")",
    Timeframe.W1.value:  "date_trunc('week', \"timestamp\")",
}


def bucket_floor_sql(target: Timeframe) -> str:
    """Return the Postgres expression that floors a 1m `timestamp` into
    the bucket for `target`. Raises ValueError if target is unresolvable."""
    if target.value == Timeframe.M1.value:
        raise ValueError("target timeframe must be coarser than 1m")
    sql = _BUCKET_FLOOR_SQL.get(target.value)
    if sql is None:
        raise ValueError(f"unsupported resample target: {target.value}")
    return sql


def build_resample_sql(target: Timeframe) -> str:
    """Return a reusable SQL template for one instrument+source+window
    resample. Parameters (in order): instrument_id, source,
    window_start, window_end."""
    bucket = bucket_floor_sql(target)
    return f"""
    INSERT INTO candles (
        instrument_id, source, timeframe, "timestamp",
        "open", high, low, "close", volume
    )
    SELECT
        instrument_id,
        source,
        '{target.value}'::timeframe_t AS timeframe,
        {bucket} AS bucket_ts,
        (array_agg("open"  ORDER BY "timestamp" ASC))[1]  AS "open",
        MAX(high) AS high,
        MIN(low)  AS low,
        (array_agg("close" ORDER BY "timestamp" DESC))[1] AS "close",
        SUM(COALESCE(volume, 0)) AS volume
    FROM candles
    WHERE instrument_id = $1
      AND source        = $2
      AND timeframe     = '1m'
      AND "timestamp"   >= $3
      AND "timestamp"   <  $4
    GROUP BY instrument_id, source, {bucket}
    ON CONFLICT (instrument_id, source, timeframe, "timestamp") DO UPDATE
       SET "open"  = EXCLUDED."open",
           high    = EXCLUDED.high,
           low     = EXCLUDED.low,
           "close" = EXCLUDED."close",
           volume  = EXCLUDED.volume
    RETURNING 1
    """.strip()


async def resample_one(
    pool: Any,
    instrument_id: int,
    source: str,
    target: Timeframe,
    *,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    dry_run: bool = False,
) -> ResampleReport:
    """Resample 1m -> target for (instrument, source) within a window.

    * `window_start`/`window_end`: inclusive / exclusive bounds. Defaults
      to the full 1m history of that (instrument, source).
    * `dry_run=True` skips the INSERT and only returns the expected row
      count.
    """
    log.debug(
        "resample_one(instrument_id=%s, source=%s, target=%s, "
        "window_start=%s, window_end=%s, dry_run=%s)",
        instrument_id, source, target.value, window_start, window_end, dry_run,
    )

    if window_start is None or window_end is None:
        # Auto-detect: grab MIN/MAX 1m timestamps.
        row = await pool.fetch_one(
            """
            SELECT MIN("timestamp") AS lo, MAX("timestamp") AS hi
              FROM candles
             WHERE instrument_id=$1 AND source=$2 AND timeframe='1m'
            """,
            (instrument_id, source),
        )
        if not row or row["lo"] is None:
            return ResampleReport(
                instrument_id=instrument_id,
                source=source,
                to_timeframe=target,
                rows_written=0,
                dry_run=dry_run,
                error="no 1m bars available to resample",
            )
        window_start = window_start or row["lo"]
        # +1us so hi is INCLUDED by the half-open interval
        window_end = window_end or (row["hi"].replace(microsecond=(row["hi"].microsecond + 1) % 1_000_000))

    if dry_run:
        floor = bucket_floor_sql(target)
        count_sql = f"""
            SELECT COUNT(DISTINCT {floor}) AS n
              FROM candles
             WHERE instrument_id=$1 AND source=$2 AND timeframe='1m'
               AND "timestamp" >= $3 AND "timestamp" < $4
        """
        row = await pool.fetch_one(
            count_sql, (instrument_id, source, window_start, window_end),
        )
        return ResampleReport(
            instrument_id=instrument_id, source=source,
            to_timeframe=target,
            window_start=window_start, window_end=window_end,
            rows_written=int(row["n"]) if row and row["n"] is not None else 0,
            dry_run=True,
        )

    sql = build_resample_sql(target)
    rows = await pool.fetch_all(
        sql, (instrument_id, source, window_start, window_end),
    )
    return ResampleReport(
        instrument_id=instrument_id, source=source,
        to_timeframe=target,
        window_start=window_start, window_end=window_end,
        rows_written=len(rows),
        dry_run=False,
    )


async def resample_chain(
    pool: Any,
    instrument_id: int,
    source: str,
    *,
    window_start: Optional[datetime] = None,
    window_end: Optional[datetime] = None,
    dry_run: bool = False,
    targets: Optional[list] = None,
) -> Dict[str, ResampleReport]:
    """Run the full 1m -> {5m,15m,30m,1h,4h,1d,1w} chain for one pair."""
    chain = targets or RESAMPLE_CHAIN
    out: Dict[str, ResampleReport] = {}
    for tf in chain:
        report = await resample_one(
            pool, instrument_id, source, tf,
            window_start=window_start, window_end=window_end,
            dry_run=dry_run,
        )
        out[tf.value] = report
    return out


async def invalidate_sufficiency_for(
    pool: Any,
    instrument_id: int,
    touched_timeframes: list,
) -> int:
    """Drop Phase 15 sufficiency cache rows affected by a resample/backfill.

    Runs one DELETE so it is atomic even if the SufficiencyService
    module isn't loaded yet.
    """
    if not touched_timeframes:
        return 0
    rows = await pool.fetch_all(
        """
        WITH d AS (
            DELETE FROM data_sufficiency_reports
             WHERE instrument_id = $1
               AND timeframe = ANY($2::timeframe_t[])
            RETURNING 1
        )
        SELECT COUNT(*) AS n FROM d
        """,
        (instrument_id, [tf.value if isinstance(tf, Timeframe) else tf
                         for tf in touched_timeframes]),
    )
    return int(rows[0]["n"]) if rows else 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
