"""
Postgres Candle Loader — Tickles & Co V2.0 (hardened 2026-04-17)
=================================================================

Loads OHLCV + bid/ask candles from the Postgres `candles` table for the
backtest engine and any downstream analytics.

Design notes:
  * Uses the SHARED pool (tickles_shared DB) because candles are global
    reference data. Company DBs never duplicate candle rows.
  * Works with declarative monthly partitions — always filter on `timestamp`.
  * Returns pandas DataFrames in the SAME column layout used by the
    backtest engine: snapshotTime, openPrice, highPrice, lowPrice,
    closePrice, openBid, closeAsk, lastTradedVolume, date.

  HONEST COLUMN NAMING (audit fix 2026-04-17):
    The DB only stores `open_bid` and `close_ask` — there is NO `close_bid`
    column. Previously the loader aliased `open_bid` AS `close_bid`, which
    silently fed the engine the wrong side of the spread. The loader now
    emits `openBid` (the correct name), and the engine consumes `openBid`
    when computing short-side fills at bar open.

  * All timestamps are UTC (TIMESTAMPTZ).
  * Rows with missing OHLC are DROPPED (not zero-filled) so the engine
    can trust the series is finite.
  * DISTINCT ON (timestamp) guarantees no duplicate bars reach the engine
    even if the upstream collector writes the same candle twice.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import pandas as pd


def _parse_date_bound(s, end: bool) -> datetime:
    """Return a tz-aware UTC datetime. For end dates we bump to next-day 00:00
    so the half-open window `ts < end_dt` includes the entire end date.
    """
    if isinstance(s, datetime):
        dt = s
    else:
        try:
            dt = datetime.fromisoformat(str(s))
        except ValueError:
            dt = datetime.strptime(str(s), "%Y-%m-%d")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if end:
        # If only a date was given, push to next day start.
        if (isinstance(s, str) and len(s) <= 10) or (dt.time() == datetime.min.time()):
            dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            dt = dt + timedelta(days=1)
    return dt


# Ensure `/opt/tickles` is on sys.path so `shared.utils.db` resolves.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SHARED)
for p in (_ROOT, _SHARED):
    if p not in sys.path:
        sys.path.insert(0, p)

from shared.utils.db import get_shared_pool  # noqa: E402  (path manipulated above)

log = logging.getLogger("tickles.candle_loader")

# Columns the backtest engine expects. KEEP ORDER — downstream code relies on it.
_ENGINE_COLS = [
    "snapshotTime",
    "openPrice",
    "highPrice",
    "lowPrice",
    "closePrice",
    "openBid",           # bid at bar OPEN — used for short entries
    "closeAsk",          # ask at bar CLOSE — used for long entries/exits
    "lastTradedVolume",
    "date",
]

# Validation thresholds for sanity-checking OHLC.
_MIN_VALID_PRICE = 1e-12  # anything ≤ this is treated as missing


def _assemble_df(rows) -> pd.DataFrame:
    """Build the engine-ready DataFrame from a list of row-dicts.

    Responsibilities:
      * Rename SQL aliases → engine names.
      * Coerce numerics; DROP rows with invalid close (not zero-fill).
      * Build `date` column.
      * Deduplicate by snapshotTime (defense-in-depth; SQL already DISTINCT-ON's).
    """
    if not rows:
        return pd.DataFrame(columns=_ENGINE_COLS)

    df = pd.DataFrame(rows).rename(columns={
        "snapshot_time":  "snapshotTime",
        "open_price":     "openPrice",
        "high_price":     "highPrice",
        "low_price":      "lowPrice",
        "close_price":    "closePrice",
        "open_bid":       "openBid",
        "close_ask":      "closeAsk",
        "volume":         "lastTradedVolume",
    })

    for col in ("openPrice", "highPrice", "lowPrice", "closePrice",
                "openBid", "closeAsk", "lastTradedVolume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Drop bars that failed to parse the close price or have non-positive close.
    before = len(df)
    df = df[df["closePrice"].notna() & (df["closePrice"] > _MIN_VALID_PRICE)]
    dropped = before - len(df)
    if dropped:
        log.warning("candle_loader: dropped %d bars with invalid closePrice", dropped)

    # For bid/ask proxies, fall back to close if missing. (Spread model adds slippage.)
    for col in ("openBid", "closeAsk"):
        if col in df.columns:
            df[col] = df[col].where(df[col] > _MIN_VALID_PRICE, df["closePrice"])
    df["lastTradedVolume"] = df["lastTradedVolume"].fillna(0.0)

    # Cast remaining numerics to float (after the drop so NaNs are gone).
    for col in ("openPrice", "highPrice", "lowPrice", "closePrice",
                "openBid", "closeAsk", "lastTradedVolume"):
        if col in df.columns:
            df[col] = df[col].astype(float)

    df["snapshotTime"] = pd.to_datetime(df["snapshotTime"], utc=True)

    # Final defense: remove any duplicate timestamps (keep last).
    df = df.drop_duplicates(subset=["snapshotTime"], keep="last")
    df = df.sort_values("snapshotTime").reset_index(drop=True)
    df["date"] = df["snapshotTime"].dt.date

    # Reorder to _ENGINE_COLS, adding any missing as NaN.
    for col in _ENGINE_COLS:
        if col not in df.columns:
            df[col] = pd.NA
    return df[_ENGINE_COLS]


# -------------------- SQL --------------------
# DISTINCT ON keeps the latest write if an upstream collector ever duplicates a row.
_SELECT_CANDLES_SQL = """
    SELECT DISTINCT ON ("timestamp")
        "timestamp"           AS snapshot_time,
        "open"                AS open_price,
        high                  AS high_price,
        low                   AS low_price,
        "close"               AS close_price,
        open_bid              AS open_bid,
        close_ask             AS close_ask,
        COALESCE(volume, 0)   AS volume
    FROM candles
    WHERE instrument_id = {p1}
      AND source        = {p2}
      AND timeframe     = {p3}::timeframe_t
      AND "timestamp"  >= {p4}
      AND "timestamp"   < {p5}
    ORDER BY "timestamp", created_at DESC NULLS LAST
"""


async def load_candles(
    pool,
    symbol: str,
    source: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    include_bid_ask: bool = True,
) -> pd.DataFrame:
    """Async loader. Returns engine-ready DataFrame."""
    t0 = time.perf_counter()
    log.debug(
        "load_candles(symbol=%s, source=%s, tf=%s, %s → %s, bidask=%s)",
        symbol, source, timeframe, start_date, end_date, include_bid_ask,
    )

    row = await pool.fetch_one(
        "SELECT id FROM instruments WHERE symbol = $1 AND exchange = $2 LIMIT 1",
        (symbol, source),
    )
    if row is None:
        log.warning("load_candles: unknown instrument %s@%s", symbol, source)
        return pd.DataFrame(columns=_ENGINE_COLS)

    instrument_id = row["id"]
    start_dt = _parse_date_bound(start_date, end=False)
    end_dt   = _parse_date_bound(end_date,   end=True)

    query = _SELECT_CANDLES_SQL.format(p1="$1", p2="$2", p3="$3", p4="$4", p5="$5")
    rows = await pool.fetch_all(
        query, (instrument_id, source, timeframe, start_dt, end_dt),
    )
    df = _assemble_df(rows)
    if df.empty:
        log.info(
            "load_candles: zero rows for %s@%s tf=%s %s..%s",
            symbol, source, timeframe, start_date, end_date,
        )

    if not include_bid_ask:
        df = df.drop(columns=["openBid", "closeAsk"], errors="ignore")

    elapsed = (time.perf_counter() - t0) * 1000.0
    log.info(
        "load_candles → %d rows in %.1f ms (%s@%s %s)",
        len(df), elapsed, symbol, source, timeframe,
    )
    return df


async def get_coverage(pool, symbol: str, source: str, timeframe: str) -> dict:
    """Return {first, last, count, exists} for a (symbol, src, tf)."""
    row = await pool.fetch_one(
        "SELECT id FROM instruments WHERE symbol=$1 AND exchange=$2 LIMIT 1",
        (symbol, source),
    )
    if row is None:
        return {"first": None, "last": None, "count": 0, "exists": False}
    iid = row["id"]
    stats = await pool.fetch_one(
        """
        SELECT MIN("timestamp") AS first_ts,
               MAX("timestamp") AS last_ts,
               COUNT(*)          AS n
        FROM candles
        WHERE instrument_id = $1 AND source = $2 AND timeframe = $3::timeframe_t
        """,
        (iid, source, timeframe),
    )
    return {
        "first": stats["first_ts"],
        "last":  stats["last_ts"],
        "count": int(stats["n"] or 0),
        "exists": True,
    }


# ---------------------------------------------------------------------------
# Sync wrapper — workers use psycopg2 because the asyncpg pool is bound to
# the loop that first created it, and worker multiprocessing creates new loops.
# ---------------------------------------------------------------------------
def load_candles_sync(
    symbol: str,
    source: str,
    timeframe: str,
    start_date: str,
    end_date: str,
    include_bid_ask: bool = True,
) -> pd.DataFrame:
    try:
        from shared.utils import config
        import psycopg2
        import psycopg2.extras
    except ImportError:
        log.exception("psycopg2 not installed — sync loader unavailable")
        return pd.DataFrame(columns=_ENGINE_COLS)

    t0 = time.perf_counter()
    conn = None
    try:
        conn = psycopg2.connect(
            host=config.DB_HOST, port=config.DB_PORT,
            user=config.DB_USER, password=config.DB_PASSWORD,
            dbname=config.DB_NAME_SHARED,
            connect_timeout=10,
        )
    except Exception:
        log.exception("load_candles_sync: cannot connect to Postgres")
        return pd.DataFrame(columns=_ENGINE_COLS)

    try:
        start_dt = _parse_date_bound(start_date, end=False)
        end_dt   = _parse_date_bound(end_date,   end=True)

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id FROM instruments WHERE symbol=%s AND exchange=%s LIMIT 1",
                (symbol, source),
            )
            row = cur.fetchone()
            if row is None:
                log.warning("load_candles_sync: unknown instrument %s@%s",
                            symbol, source)
                return pd.DataFrame(columns=_ENGINE_COLS)
            iid = row["id"]

            cur.execute(
                _SELECT_CANDLES_SQL.format(p1="%s", p2="%s", p3="%s", p4="%s", p5="%s"),
                (iid, source, timeframe, start_dt, end_dt),
            )
            rows = cur.fetchall()

        df = _assemble_df(rows)
        if df.empty:
            log.info("load_candles_sync: zero rows for %s@%s %s %s..%s",
                     symbol, source, timeframe, start_date, end_date)
        if not include_bid_ask:
            df = df.drop(columns=["openBid", "closeAsk"], errors="ignore")

        elapsed = (time.perf_counter() - t0) * 1000.0
        log.info("load_candles_sync → %d rows in %.1fms (%s@%s %s)",
                 len(df), elapsed, symbol, source, timeframe)
        return df
    except Exception:
        log.exception("load_candles_sync failed for %s@%s %s",
                      symbol, source, timeframe)
        return pd.DataFrame(columns=_ENGINE_COLS)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    async def _main():
        pool = await get_shared_pool()
        rows = await pool.fetch_all(
            "SELECT symbol, exchange FROM instruments "
            "ORDER BY exchange, symbol LIMIT 10"
        )
        for r in rows:
            cov = await get_coverage(pool, r["symbol"], r["exchange"], "1m")
            print(f"{r['exchange']:8s} {r['symbol']:15s} 1m  count={cov['count']:>8d} "
                  f"{cov['first']} .. {cov['last']}")
    asyncio.run(_main())
