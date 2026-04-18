"""
shared.candles.backfill — one-shot historical 1m backfill via CCXT.

Complements the live `candles.daemon` by filling history GAPS. The
daemon tails from now-1h forward; the backfill reaches back as far as
an exchange allows (often 2+ years for crypto majors on Binance).

Design
------
* Async, rate-limited via CCXT's `enableRateLimit=True`.
* Paged: fetch_ohlcv(limit=1000) per page (exchange max); advance
  `since` by `last_ts + 60_000ms`.
* Upsert matches the daemon's contract so there are no duplicate keys.
* Idempotent on re-run; backfilling an already-covered window is a
  no-op from the caller's perspective.
* Optional `dry_run` returns pages/bars counts without writing.
* After the run, `Phase 15 sufficiency_reports` for this instrument
  are invalidated so the next `sufficiency_cli check` re-grades.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from shared.candles.resample import invalidate_sufficiency_for
from shared.candles.schema import BackfillReport, Timeframe

log = logging.getLogger("tickles.candles.backfill")

DEFAULT_LIMIT = int(os.getenv("CANDLE_BACKFILL_LIMIT", "1000"))
DEFAULT_MIN_GAP_MS = 60_000  # 1 minute in ms
DEFAULT_SLEEP_ON_EMPTY = float(os.getenv("CANDLE_BACKFILL_SLEEP_S", "1.0"))


async def _get_ccxt_client(exchange: str) -> Any:
    """Lazy-import ccxt.async_support and return a configured client.

    Raises if ccxt is missing (the candle daemon already depends on it;
    the exception will surface at import time).
    """
    import ccxt.async_support as ccxt_async
    cls = getattr(ccxt_async, exchange, None)
    if cls is None:
        raise ValueError(f"unknown CCXT exchange: {exchange!r}")
    client = cls({"enableRateLimit": True})
    return client


async def _resolve_instrument(
    pool: Any, instrument_id: int,
) -> tuple:
    """Return (exchange, symbol) for instrument_id or raise."""
    row = await pool.fetch_one(
        "SELECT exchange, symbol FROM instruments WHERE id=$1", (instrument_id,),
    )
    if not row:
        raise ValueError(f"no instrument with id={instrument_id}")
    return row["exchange"], row["symbol"]


async def _upsert_page(pool: Any, rows: List[tuple]) -> int:
    """Upsert a list of (iid, tf, source, ts, o, h, l, c, v) rows."""
    if not rows:
        return 0
    sql = """
        INSERT INTO candles
            (instrument_id, timeframe, source, "timestamp",
             "open", high, low, "close", volume)
        VALUES ($1, $2::timeframe_t, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (instrument_id, source, timeframe, "timestamp") DO UPDATE
            SET "open"  = EXCLUDED."open",
                high    = EXCLUDED.high,
                low     = EXCLUDED.low,
                "close" = EXCLUDED."close",
                volume  = EXCLUDED.volume
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(sql, rows)
    return len(rows)


async def backfill_instrument(
    pool: Any,
    instrument_id: int,
    start_ts: datetime,
    end_ts: Optional[datetime] = None,
    *,
    timeframe: Timeframe = Timeframe.M1,
    source_override: Optional[str] = None,
    limit_per_page: int = DEFAULT_LIMIT,
    max_pages: Optional[int] = None,
    dry_run: bool = False,
    invalidate_sufficiency: bool = True,
) -> BackfillReport:
    """Backfill 1m candles for one instrument between [start_ts, end_ts).

    The return object records how many pages/bars came back, how many
    were inserted, any error, and the number of Phase 15 cache rows
    dropped.
    """
    log.debug(
        "backfill_instrument(instrument_id=%s, start=%s, end=%s, "
        "timeframe=%s, dry_run=%s)",
        instrument_id, start_ts, end_ts, timeframe.value, dry_run,
    )
    exchange, symbol = await _resolve_instrument(pool, instrument_id)
    source = source_override or exchange
    end_ts = end_ts or datetime.now(timezone.utc)
    if start_ts.tzinfo is None:
        start_ts = start_ts.replace(tzinfo=timezone.utc)
    if end_ts.tzinfo is None:
        end_ts = end_ts.replace(tzinfo=timezone.utc)

    report = BackfillReport(
        instrument_id=instrument_id,
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        start_ts=start_ts,
        end_ts=end_ts,
        dry_run=dry_run,
    )

    client = None
    try:
        client = await _get_ccxt_client(exchange)
        since_ms = int(start_ts.timestamp() * 1000)
        end_ms = int(end_ts.timestamp() * 1000)
        pages = 0
        total_fetched = 0
        total_inserted = 0

        while since_ms < end_ms:
            if max_pages is not None and pages >= max_pages:
                break
            try:
                ohlcv = await client.fetch_ohlcv(
                    symbol, timeframe=timeframe.value,
                    since=since_ms, limit=limit_per_page,
                )
            except Exception as exc:
                report.error = f"fetch_ohlcv failed: {exc!r}"
                log.warning(report.error)
                break
            pages += 1
            if not ohlcv:
                # Exchange has no more data in this window — advance
                # past `limit_per_page` bars to probe a future window.
                since_ms += DEFAULT_MIN_GAP_MS * limit_per_page
                await asyncio.sleep(DEFAULT_SLEEP_ON_EMPTY)
                continue
            rows: List[tuple] = []
            last_seen_ms = since_ms
            for bar in ohlcv:
                ts_ms, o, h, low_, c, v = bar
                if ts_ms < since_ms or ts_ms >= end_ms:
                    continue
                rows.append((
                    instrument_id, timeframe.value, source,
                    datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
                    float(o), float(h), float(low_), float(c),
                    float(v or 0),
                ))
                last_seen_ms = ts_ms
            total_fetched += len(rows)
            if not dry_run:
                total_inserted += await _upsert_page(pool, rows)

            if last_seen_ms <= since_ms:
                # No forward progress — skip ahead to avoid infinite loop.
                since_ms += DEFAULT_MIN_GAP_MS * limit_per_page
            else:
                since_ms = last_seen_ms + DEFAULT_MIN_GAP_MS
            # CCXT's rate limiter already throttles; extra pause keeps us
            # polite on public endpoints.
            await asyncio.sleep(0.05)

        report.pages = pages
        report.fetched_bars = total_fetched
        report.inserted_bars = total_inserted
    except Exception as exc:
        log.exception("backfill crashed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    if invalidate_sufficiency and not dry_run and report.inserted_bars > 0:
        try:
            invalidated = await invalidate_sufficiency_for(
                pool, instrument_id, [timeframe],
            )
            report.sufficiency_invalidated_rows = invalidated
        except Exception as exc:
            log.warning("sufficiency invalidate failed: %s", exc)

    return report


def minutes_between(start_ts: datetime, end_ts: datetime) -> int:
    """Minutes between two datetimes, both treated as UTC if naive."""
    if start_ts.tzinfo is None:
        start_ts = start_ts.replace(tzinfo=timezone.utc)
    if end_ts.tzinfo is None:
        end_ts = end_ts.replace(tzinfo=timezone.utc)
    return max(0, int((end_ts - start_ts).total_seconds() // 60))


def estimate_pages(start_ts: datetime, end_ts: datetime,
                   limit_per_page: int = DEFAULT_LIMIT) -> int:
    """Rough page count for a planned backfill — used by the CLI for UX."""
    mins = minutes_between(start_ts, end_ts)
    if limit_per_page <= 0:
        return 0
    # each page returns ~limit_per_page bars of 1m each
    pages = mins // limit_per_page
    if mins % limit_per_page:
        pages += 1
    return pages


def parse_window(
    start: Optional[str], end: Optional[str], default_days: int = 7,
) -> tuple:
    """Parse CLI --start / --end (ISO 8601, date-only, or '7d' relative)."""
    now = datetime.now(timezone.utc)

    def _parse(val: Optional[str], default: datetime) -> datetime:
        if val is None:
            return default
        v = val.strip()
        if v.endswith("d") and v[:-1].isdigit():
            return now - timedelta(days=int(v[:-1]))
        try:
            if "T" in v or " " in v:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            else:
                dt = datetime.fromisoformat(v + "T00:00:00+00:00")
        except ValueError as exc:
            raise ValueError(f"cannot parse timestamp {val!r}: {exc}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    start_dt = _parse(start, now - timedelta(days=default_days))
    end_dt = _parse(end, now)
    if start_dt >= end_dt:
        raise ValueError(f"start {start_dt} must be < end {end_dt}")
    return start_dt, end_dt


def _t0() -> float:
    return time.time()
