"""
Module: resample_runner
Purpose: Periodic 1m → {5m,15m,30m,1h,4h,1d,1w} resample daemon.
Location: /opt/tickles/shared/candles/resample_runner.py

Runs as a systemd service (tickles-resample). Each cycle:
  1. Discovers all (instrument_id, source) pairs with 1m data.
  2. For pairs with no higher-TF rows yet → full-history resample.
  3. For pairs already resampled → incremental (last 30 min window).
  4. Sleeps RESAMPLE_INTERVAL_SECONDS between cycles.

Uses the existing resample_chain() from shared.candles.resample which
issues SQL-native INSERT...SELECT...GROUP BY with ON CONFLICT DO UPDATE.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
# Only add project root to sys.path — NOT shared/ directly.
# Adding shared/ causes shared/copy/ to shadow the stdlib copy module,
# which breaks pydantic (dataclasses -> import copy -> shared/copy/).
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from shared.candles.resample import resample_chain, resample_one
from shared.candles.schema import RESAMPLE_CHAIN
from shared.utils.db import get_shared_pool

log = logging.getLogger("tickles.resample_runner")

# ---------------------------------------------------------------------------
# Configuration (env-var overridable)
# ---------------------------------------------------------------------------
RESAMPLE_INTERVAL_SECONDS = int(os.getenv("RESAMPLE_INTERVAL_S", "300"))
INCREMENTAL_WINDOW_MINUTES = int(os.getenv("RESAMPLE_INCREMENTAL_MINS", "30"))
BATCH_DELAY_SECONDS = float(os.getenv("RESAMPLE_BATCH_DELAY_S", "0.1"))


async def _discover_instruments(pool: Any) -> List[Tuple[int, str]]:
    """Find all (instrument_id, source) pairs that have 1m candle data.

    Args:
        pool: Async DatabasePool for tickles_shared.

    Returns:
        List of (instrument_id, source) tuples.
    """
    rows = await pool.fetch_all(
        "SELECT DISTINCT instrument_id, source "
        "FROM candles WHERE timeframe = '1m' "
        "ORDER BY instrument_id, source"
    )
    return [(int(r["instrument_id"]), r["source"]) for r in rows]


async def _needs_full_resample(
    pool: Any, instrument_id: int, source: str,
) -> bool:
    """Check if an instrument/source pair has any higher-TF rows.

    Args:
        pool: Async DatabasePool.
        instrument_id: Instrument primary key.
        source: Data source identifier.

    Returns:
        True if no higher-TF rows exist (needs full resample).
    """
    row = await pool.fetch_one(
        "SELECT COUNT(*) AS n FROM candles "
        "WHERE instrument_id = $1 AND source = $2 "
        "AND timeframe != '1m'",
        (instrument_id, source),
    )
    return row is None or int(row["n"]) == 0


async def _run_full_resample(
    pool: Any, instrument_id: int, source: str,
) -> Dict[str, int]:
    """Full-history resample for one (instrument, source) pair.

    Args:
        pool: Async DatabasePool.
        instrument_id: Instrument primary key.
        source: Data source identifier.

    Returns:
        Dict mapping timeframe → rows_written count.
    """
    reports = await resample_chain(pool, instrument_id, source)
    result: Dict[str, int] = {}
    for tf_value, report in reports.items():
        result[tf_value] = report.rows_written
        if report.error:
            log.warning(
                "full resample error: iid=%s src=%s tf=%s err=%s",
                instrument_id, source, tf_value, report.error,
            )
    return result


async def _run_incremental_resample(
    pool: Any, instrument_id: int, source: str,
) -> Dict[str, int]:
    """Incremental resample for the recent window only.

    Only re-aggregates 1m bars from the last INCREMENTAL_WINDOW_MINUTES.
    This is fast because it touches a small slice of data.

    Args:
        pool: Async DatabasePool.
        instrument_id: Instrument primary key.
        source: Data source identifier.

    Returns:
        Dict mapping timeframe → rows_written count.
    """
    window_start = datetime.now(timezone.utc) - timedelta(
        minutes=INCREMENTAL_WINDOW_MINUTES
    )
    window_end = datetime.now(timezone.utc)
    result: Dict[str, int] = {}
    for tf in RESAMPLE_CHAIN:
        try:
            report = await resample_one(
                pool, instrument_id, source, tf,
                window_start=window_start,
                window_end=window_end,
            )
            result[tf.value] = report.rows_written
            if report.error:
                log.warning(
                    "incremental resample error: iid=%s src=%s tf=%s err=%s",
                    instrument_id, source, tf.value, report.error,
                )
        except Exception as exc:
            log.warning(
                "incremental resample exception: iid=%s src=%s tf=%s exc=%s",
                instrument_id, source, tf.value, exc,
            )
    return result


async def _run_cycle(pool: Any) -> Dict[str, Any]:
    """Execute one resample cycle across all instruments.

    Args:
        pool: Async DatabasePool.

    Returns:
        Summary dict with counts and timing.
    """
    cycle_start = time.time()
    pairs = await _discover_instruments(pool)
    log.info("resample cycle: %d instrument/source pairs found", len(pairs))

    full_count = 0
    incremental_count = 0
    total_rows = 0

    for instrument_id, source in pairs:
        try:
            needs_full = await _needs_full_resample(pool, instrument_id, source)
            if needs_full:
                rows = await _run_full_resample(pool, instrument_id, source)
                full_count += 1
            else:
                rows = await _run_incremental_resample(pool, instrument_id, source)
                incremental_count += 1
            total_rows += sum(rows.values())
            await asyncio.sleep(BATCH_DELAY_SECONDS)
        except Exception as exc:
            log.error(
                "resample failed: iid=%s src=%s exc=%s",
                instrument_id, source, exc, exc_info=True,
            )

    elapsed = time.time() - cycle_start
    summary = {
        "pairs": len(pairs),
        "full_resamples": full_count,
        "incremental_resamples": incremental_count,
        "total_rows_written": total_rows,
        "elapsed_seconds": round(elapsed, 2),
    }
    log.info(
        "resample cycle complete: %d full, %d incremental, "
        "%d rows written in %.1fs",
        full_count, incremental_count, total_rows, elapsed,
    )
    return summary


async def run_forever() -> None:
    """Main loop: run resample cycles with configurable interval."""
    log.info(
        "resample runner starting (interval=%ds, incremental_window=%dm)",
        RESAMPLE_INTERVAL_SECONDS, INCREMENTAL_WINDOW_MINUTES,
    )
    pool = await get_shared_pool()

    stop = asyncio.Event()

    def _signal_handler() -> None:
        log.info("received stop signal, shutting down")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # Windows doesn't support add_signal_handler

    while not stop.is_set():
        try:
            await _run_cycle(pool)
        except Exception as exc:
            log.error("resample cycle crashed: %s", exc, exc_info=True)

        try:
            await asyncio.wait_for(stop.wait(), timeout=RESAMPLE_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass  # Normal: interval elapsed, run next cycle


def main() -> int:
    """CLI entrypoint for the resample runner daemon."""
    logging.basicConfig(
        level=getattr(logging, os.getenv("TICKLES_LOGLEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
