"""
shared.data_sufficiency.schema — pydantic + enum models for Phase 15.

Pure data. No DB, no I/O.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class Verdict(str, Enum):
    """Three-tier sufficiency verdict."""

    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    FAIL = "fail"


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


# Minutes-per-bar lookup used everywhere density / gap arithmetic happens.
TIMEFRAME_MINUTES: Dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
    "1w": 10080,
}


class Profile(BaseModel):
    """A named sufficiency profile.

    Stored per-row in `system_config` (namespace='sufficiency.profiles',
    config_key=name, config_value=JSON of this model). Built-ins live in
    `shared.data_sufficiency.profiles.BUILTIN_PROFILES` as a safety net.
    """

    model_config = ConfigDict(from_attributes=True)

    name: str
    timeframe: Timeframe
    min_bars: int = Field(
        ...,
        ge=1,
        description="Absolute minimum bar count the strategy needs",
    )
    min_days: int = Field(
        ...,
        ge=1,
        description="Calendar days of history required",
    )
    max_gap_ratio: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Max (missing_bars / expected_bars); 0.01 = 1 percent tolerated",
    )
    max_gap_minutes: int = Field(
        ...,
        ge=1,
        description="Any single gap longer than this is a hard FAIL",
    )
    fresh_lag_max_minutes: int = Field(
        ...,
        ge=1,
        description="last_ts must be within this many minutes of now()",
    )
    daily_bar_target: int = Field(
        ...,
        ge=1,
        description=(
            "Expected bars per 24h window (1440 for crypto 1m; 390 for US equities 1m; "
            "etc). Used to compute density without inventing market-hour tables yet."
        ),
    )
    allow_is_fake: bool = False
    """If true, bars with is_fake=TRUE still count towards coverage."""
    allow_zero_volume: bool = False
    """If true, zero-volume bars do not raise an integrity issue."""
    notes: Optional[str] = None


class Gap(BaseModel):
    """A single missing span of bars."""

    model_config = ConfigDict(from_attributes=True)

    start_ts: datetime
    end_ts: datetime
    duration_minutes: int
    missing_bars: int


class IntegrityIssue(BaseModel):
    """A single row / stretch of integrity concern."""

    model_config = ConfigDict(from_attributes=True)

    kind: str
    count: int
    sample_ts: Optional[datetime] = None
    details: Optional[Dict[str, Any]] = None


class CoverageStats(BaseModel):
    """Raw coverage measurements, independent of any profile."""

    model_config = ConfigDict(from_attributes=True)

    instrument_id: int
    timeframe: Timeframe
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    bars: int = 0
    calendar_days: float = 0.0
    expected_bars: int = 0
    density: float = Field(
        0.0,
        ge=0.0,
        description="actual_bars / expected_bars; >1 means overlap (rare)",
    )
    fresh_lag_minutes: Optional[int] = None
    total_missing_bars: int = 0
    gap_count: int = 0
    max_gap_minutes: int = 0
    is_fake_count: int = 0
    zero_volume_count: int = 0
    null_ohlc_count: int = 0
    impossible_candle_count: int = 0

    @property
    def gap_ratio(self) -> float:
        """Missing-bar fraction; 0 if no expectation yet."""
        if self.expected_bars <= 0:
            return 0.0
        return float(self.total_missing_bars) / float(self.expected_bars)


class SufficiencyReport(BaseModel):
    """The full audit packet handed back to callers."""

    model_config = ConfigDict(from_attributes=True)

    instrument_id: int
    timeframe: Timeframe
    profile_name: str
    verdict: Verdict
    coverage: CoverageStats
    gaps: List[Gap] = Field(default_factory=list)
    integrity_issues: List[IntegrityIssue] = Field(default_factory=list)
    reasons_pass: List[str] = Field(default_factory=list)
    reasons_warn: List[str] = Field(default_factory=list)
    reasons_fail: List[str] = Field(default_factory=list)
    computed_at: datetime
    ttl_seconds: int = 300

    @property
    def ok(self) -> bool:
        """True when caller is allowed to proceed (PASS or warn)."""
        return self.verdict in {Verdict.PASS, Verdict.PASS_WITH_WARNINGS}

    def to_cache_row(self) -> Dict[str, Any]:
        """Serialise for the `data_sufficiency_reports` table."""
        return {
            "instrument_id": self.instrument_id,
            "timeframe": self.timeframe.value,
            "profile_name": self.profile_name,
            "verdict": self.verdict.value,
            "bars": self.coverage.bars,
            "first_ts": self.coverage.first_ts,
            "last_ts": self.coverage.last_ts,
            "gap_ratio": Decimal(str(round(self.coverage.gap_ratio, 6))),
            "max_gap_minutes": self.coverage.max_gap_minutes,
            "fresh_lag_minutes": self.coverage.fresh_lag_minutes,
            "report_json": self.model_dump(mode="json"),
            "computed_at": self.computed_at,
            "ttl_seconds": self.ttl_seconds,
        }
