"""
shared.candles.schema — pydantic models for Phase 16 reports.

Pure data objects. No DB, no I/O.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class Timeframe(str, Enum):
    """Mirrors the Postgres `timeframe_t` enum verbatim."""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"


RESAMPLE_CHAIN: List[Timeframe] = [
    Timeframe.M5,
    Timeframe.M15,
    Timeframe.M30,
    Timeframe.H1,
    Timeframe.H4,
    Timeframe.D1,
    Timeframe.W1,
]


class CoverageSummary(BaseModel):
    """One row of per-(instrument, timeframe, source) coverage."""

    model_config = ConfigDict(from_attributes=True)

    instrument_id: int
    symbol: Optional[str] = None
    exchange: Optional[str] = None
    source: str
    timeframe: Timeframe
    bars: int = 0
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    fresh_lag_minutes: Optional[int] = None


class ResampleReport(BaseModel):
    """Result of one resample run."""

    model_config = ConfigDict(from_attributes=True)

    instrument_id: int
    source: str
    from_timeframe: Timeframe = Timeframe.M1
    to_timeframe: Timeframe
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    rows_written: int = 0
    dry_run: bool = False
    error: Optional[str] = None


class BackfillReport(BaseModel):
    """Result of one backfill run."""

    model_config = ConfigDict(from_attributes=True)

    instrument_id: int
    exchange: str
    symbol: str
    timeframe: Timeframe
    start_ts: datetime
    end_ts: datetime
    fetched_bars: int = 0
    inserted_bars: int = 0
    pages: int = 0
    dry_run: bool = False
    error: Optional[str] = None
    sufficiency_invalidated_rows: int = 0
    """Phase 15 cache rows dropped after the backfill."""
