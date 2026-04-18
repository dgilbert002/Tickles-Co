"""
shared.data_sufficiency.metrics — pure functions that turn a candle stream
into CoverageStats + Gap + IntegrityIssue lists.

All inputs are simple dicts / sequences so this module is trivially
testable without Postgres. The async `service.py` wires these up against
the real DB.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from shared.data_sufficiency.schema import (
    TIMEFRAME_MINUTES,
    CoverageStats,
    Gap,
    IntegrityIssue,
    Timeframe,
)

log = logging.getLogger("tickles.sufficiency.metrics")


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------

def detect_gaps(
    timestamps: Sequence[datetime],
    timeframe: Timeframe,
    max_return: int = 50,
) -> Tuple[List[Gap], int, int]:
    """Detect gaps in a sorted timestamp stream.

    Returns (gaps_truncated_list, total_missing_bars, max_gap_minutes).
    Timestamps must be sorted ascending (the SQL always returns them
    ORDER BY ts).
    """
    if len(timestamps) < 2:
        return [], 0, 0

    minutes_per_bar = TIMEFRAME_MINUTES[timeframe.value]
    step = timedelta(minutes=minutes_per_bar)
    gaps: List[Gap] = []
    total_missing = 0
    max_gap_min = 0

    prev = timestamps[0]
    for ts in timestamps[1:]:
        delta = ts - prev
        # one-bar-step or less means contiguous. Anything >1.5*step is a gap.
        # We use >= 2 bars gap to avoid off-by-one on microsecond precision.
        missing = int(delta / step) - 1
        if missing > 0:
            minutes = int(delta.total_seconds() // 60)
            if len(gaps) < max_return:
                gaps.append(
                    Gap(
                        start_ts=prev,
                        end_ts=ts,
                        duration_minutes=minutes,
                        missing_bars=missing,
                    )
                )
            total_missing += missing
            if minutes > max_gap_min:
                max_gap_min = minutes
        prev = ts

    return gaps, total_missing, max_gap_min


# ---------------------------------------------------------------------------
# Integrity checks
# ---------------------------------------------------------------------------

def check_integrity(
    bars: Iterable[Dict[str, Any]],
    *,
    allow_zero_volume: bool = False,
) -> Tuple[List[IntegrityIssue], int, int, int, int]:
    """Scan a stream of OHLCV dicts and bucket issues by kind.

    Returns (issues, is_fake_count, zero_volume_count, null_ohlc_count,
    impossible_candle_count).
    Each bar must expose `open,high,low,close,volume,is_fake,timestamp`.
    """
    null_ohlc = 0
    impossible = 0
    zero_vol = 0
    fake = 0
    first_null_ts: Optional[datetime] = None
    first_impossible_ts: Optional[datetime] = None
    first_zerovol_ts: Optional[datetime] = None
    first_fake_ts: Optional[datetime] = None
    impossible_samples: List[Dict[str, Any]] = []

    for b in bars:
        ts = b.get("timestamp")
        if any(b.get(k) is None for k in ("open", "high", "low", "close")):
            null_ohlc += 1
            if first_null_ts is None:
                first_null_ts = ts
            continue
        try:
            o = Decimal(str(b["open"]))
            h = Decimal(str(b["high"]))
            low = Decimal(str(b["low"]))
            c = Decimal(str(b["close"]))
        except Exception:
            null_ohlc += 1
            if first_null_ts is None:
                first_null_ts = ts
            continue
        bad = False
        if h < low:
            bad = True
        if o < low or o > h:
            bad = True
        if c < low or c > h:
            bad = True
        if bad:
            impossible += 1
            if first_impossible_ts is None:
                first_impossible_ts = ts
            if len(impossible_samples) < 5:
                impossible_samples.append({"ts": ts, "o": str(o), "h": str(h), "l": str(low), "c": str(c)})
            continue
        v = b.get("volume")
        if v is not None:
            try:
                if Decimal(str(v)) <= 0 and not allow_zero_volume:
                    zero_vol += 1
                    if first_zerovol_ts is None:
                        first_zerovol_ts = ts
            except Exception:
                pass
        if b.get("is_fake"):
            fake += 1
            if first_fake_ts is None:
                first_fake_ts = ts

    issues: List[IntegrityIssue] = []
    if null_ohlc:
        issues.append(IntegrityIssue(
            kind="null_ohlc", count=null_ohlc, sample_ts=first_null_ts,
        ))
    if impossible:
        issues.append(IntegrityIssue(
            kind="impossible_candle", count=impossible,
            sample_ts=first_impossible_ts,
            details={"samples": impossible_samples} if impossible_samples else None,
        ))
    if zero_vol:
        issues.append(IntegrityIssue(
            kind="zero_or_negative_volume", count=zero_vol,
            sample_ts=first_zerovol_ts,
        ))
    if fake:
        issues.append(IntegrityIssue(
            kind="is_fake_flagged", count=fake, sample_ts=first_fake_ts,
        ))
    return issues, fake, zero_vol, null_ohlc, impossible


# ---------------------------------------------------------------------------
# Coverage aggregation
# ---------------------------------------------------------------------------

def compute_coverage(
    instrument_id: int,
    timeframe: Timeframe,
    timestamps: Sequence[datetime],
    integrity_counts: Tuple[int, int, int, int],
    *,
    now: Optional[datetime] = None,
    daily_bar_target: Optional[int] = None,
) -> CoverageStats:
    """Roll up raw candle timestamps into a CoverageStats object.

    `integrity_counts` is (is_fake, zero_volume, null_ohlc, impossible).
    """
    fake, zero_vol, null_ohlc, impossible = integrity_counts
    now = now or datetime.now(timezone.utc)

    if not timestamps:
        return CoverageStats(
            instrument_id=instrument_id,
            timeframe=timeframe,
            bars=0,
            calendar_days=0.0,
            expected_bars=0,
            density=0.0,
            is_fake_count=fake,
            zero_volume_count=zero_vol,
            null_ohlc_count=null_ohlc,
            impossible_candle_count=impossible,
        )

    first = timestamps[0]
    last = timestamps[-1]
    calendar_days = max((last - first).total_seconds() / 86400.0, 0.0)
    minutes_per_bar = TIMEFRAME_MINUTES[timeframe.value]

    if daily_bar_target is not None and daily_bar_target > 0:
        expected = int(calendar_days * daily_bar_target) + 1
    else:
        total_minutes = max(int((last - first).total_seconds() // 60), minutes_per_bar)
        expected = (total_minutes // minutes_per_bar) + 1

    gaps, total_missing, max_gap_min = detect_gaps(timestamps, timeframe)
    fresh_lag = int((now - last).total_seconds() // 60) if last.tzinfo else None
    if last.tzinfo is None:
        # interpret naive ts as UTC so fresh_lag has a value
        last_aware = last.replace(tzinfo=timezone.utc)
        fresh_lag = int((now - last_aware).total_seconds() // 60)

    density = (float(len(timestamps)) / float(expected)) if expected > 0 else 0.0

    return CoverageStats(
        instrument_id=instrument_id,
        timeframe=timeframe,
        first_ts=first,
        last_ts=last,
        bars=len(timestamps),
        calendar_days=round(calendar_days, 4),
        expected_bars=expected,
        density=round(density, 6),
        fresh_lag_minutes=fresh_lag,
        total_missing_bars=total_missing,
        gap_count=len(gaps),
        max_gap_minutes=max_gap_min,
        is_fake_count=fake,
        zero_volume_count=zero_vol,
        null_ohlc_count=null_ohlc,
        impossible_candle_count=impossible,
    )
