"""Phase 16 — tests for Candle Hub helpers.

Three concerns:
    1. Pydantic schema invariants (Timeframe enum, RESAMPLE_CHAIN).
    2. Resampler SQL builders (bucket_floor_sql, build_resample_sql).
    3. Backfill helpers (minutes_between, estimate_pages, parse_window).

Async DB round-trip tests would need a live Postgres; those live in
shared/tests/integration/ and are skipped outside the VPS.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shared.candles.backfill import (
    estimate_pages,
    minutes_between,
    parse_window,
)
from shared.candles.resample import (
    _BUCKET_FLOOR_SQL,
    bucket_floor_sql,
    build_resample_sql,
)
from shared.candles.schema import (
    RESAMPLE_CHAIN,
    BackfillReport,
    CoverageSummary,
    ResampleReport,
    Timeframe,
)


UTC = timezone.utc


# ---------------------------------------------------------------------------
# 1. Schema
# ---------------------------------------------------------------------------

def test_timeframe_enum_exact_values() -> None:
    assert {t.value for t in Timeframe} == {
        "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w",
    }


def test_resample_chain_excludes_1m_and_is_ordered() -> None:
    vals = [t.value for t in RESAMPLE_CHAIN]
    assert "1m" not in vals
    assert vals == ["5m", "15m", "30m", "1h", "4h", "1d", "1w"]


def test_resample_report_roundtrip() -> None:
    r = ResampleReport(
        instrument_id=42, source="binance",
        to_timeframe=Timeframe.M5, rows_written=12, dry_run=False,
    )
    dumped = r.model_dump(mode="json")
    assert dumped["to_timeframe"] == "5m"
    assert dumped["rows_written"] == 12


def test_coverage_summary_roundtrip() -> None:
    cov = CoverageSummary(
        instrument_id=1, symbol="BTC/USDT", exchange="binance",
        source="binance", timeframe=Timeframe.M1, bars=3000,
        first_ts=datetime(2026, 4, 1, tzinfo=UTC),
        last_ts=datetime(2026, 4, 19, tzinfo=UTC),
        fresh_lag_minutes=2,
    )
    dumped = cov.model_dump(mode="json")
    assert dumped["timeframe"] == "1m"
    assert dumped["bars"] == 3000


def test_backfill_report_defaults() -> None:
    rep = BackfillReport(
        instrument_id=7,
        exchange="binance",
        symbol="BTC/USDT",
        timeframe=Timeframe.M1,
        start_ts=datetime(2026, 4, 1, tzinfo=UTC),
        end_ts=datetime(2026, 4, 10, tzinfo=UTC),
    )
    assert rep.fetched_bars == 0
    assert rep.inserted_bars == 0
    assert rep.sufficiency_invalidated_rows == 0
    assert rep.dry_run is False


# ---------------------------------------------------------------------------
# 2. Resampler SQL builders
# ---------------------------------------------------------------------------

def test_bucket_floor_sql_every_target() -> None:
    for tf in RESAMPLE_CHAIN:
        sql = bucket_floor_sql(tf)
        assert sql.startswith("date_trunc("), f"{tf.value}: {sql!r}"
        assert sql == _BUCKET_FLOOR_SQL[tf.value]


def test_bucket_floor_sql_1m_rejected() -> None:
    with pytest.raises(ValueError):
        bucket_floor_sql(Timeframe.M1)


def test_build_resample_sql_has_insert_and_conflict() -> None:
    sql = build_resample_sql(Timeframe.M5)
    low = sql.lower()
    assert "insert into candles" in low
    assert "'5m'::timeframe_t" in low
    assert "on conflict" in low
    assert "group by" in low
    assert "returning 1" in low


def test_build_resample_sql_uses_target_specific_floor() -> None:
    assert "5" in build_resample_sql(Timeframe.M5)
    assert "15" in build_resample_sql(Timeframe.M15)
    assert "'1h'::timeframe_t" in build_resample_sql(Timeframe.H1)
    assert "'1w'::timeframe_t" in build_resample_sql(Timeframe.W1)


def test_build_resample_sql_parameter_positions() -> None:
    sql = build_resample_sql(Timeframe.H4)
    # $1..$4 must all appear: instrument_id, source, window_start, window_end
    for p in ("$1", "$2", "$3", "$4"):
        assert p in sql, f"missing placeholder {p} in SQL"


# ---------------------------------------------------------------------------
# 3. Backfill helpers
# ---------------------------------------------------------------------------

def test_minutes_between_aware_and_naive() -> None:
    a = datetime(2026, 4, 1, 10, 0)
    b = datetime(2026, 4, 1, 10, 30)
    assert minutes_between(a, b) == 30


def test_minutes_between_reversed_returns_zero() -> None:
    a = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    b = datetime(2026, 4, 1, 11, 0, tzinfo=UTC)
    assert minutes_between(a, b) == 0


def test_estimate_pages_rounds_up() -> None:
    start = datetime(2026, 4, 1, tzinfo=UTC)
    end = start + timedelta(minutes=2500)
    assert estimate_pages(start, end, limit_per_page=1000) == 3


def test_estimate_pages_zero_limit() -> None:
    assert estimate_pages(
        datetime.now(UTC), datetime.now(UTC) + timedelta(minutes=10),
        limit_per_page=0,
    ) == 0


def test_parse_window_iso_dates() -> None:
    start_dt, end_dt = parse_window("2026-04-01", "2026-04-05")
    assert start_dt.tzinfo is not None
    assert (end_dt - start_dt).days == 4


def test_parse_window_relative_days() -> None:
    start_dt, end_dt = parse_window("7d", None)
    span = end_dt - start_dt
    assert 6.9 <= span.total_seconds() / 86400 <= 7.1


def test_parse_window_defaults_to_7d() -> None:
    start_dt, end_dt = parse_window(None, None, default_days=7)
    span_days = (end_dt - start_dt).total_seconds() / 86400
    assert 6.9 <= span_days <= 7.1


def test_parse_window_iso_with_tz() -> None:
    start_dt, end_dt = parse_window("2026-04-01T00:00:00Z", "2026-04-02T12:00:00Z")
    assert (end_dt - start_dt) == timedelta(days=1, hours=12)


def test_parse_window_invalid_raises() -> None:
    with pytest.raises(ValueError):
        parse_window("not-a-date", None)


def test_parse_window_swapped_raises() -> None:
    with pytest.raises(ValueError):
        parse_window("2026-04-05", "2026-04-01")
