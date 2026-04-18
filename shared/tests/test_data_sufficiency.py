"""Phase 15 — tests for the Data Sufficiency Engine.

Three concerns:
    1. Pure metric helpers (detect_gaps, check_integrity, compute_coverage).
    2. Grading function (`_grade`) with synthetic CoverageStats.
    3. SufficiencyService end-to-end against a FakePool emulating asyncpg.

Phase 21's Rule-1 auditor will add live-DB integration tests.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from shared.data_sufficiency import (
    CoverageStats,
    Profile,
    SufficiencyReport,
    SufficiencyService,
    Verdict,
)
from shared.data_sufficiency.metrics import (
    check_integrity,
    compute_coverage,
    detect_gaps,
)
from shared.data_sufficiency.profiles import (
    BUILTIN_PROFILES,
    NAMESPACE,
    all_profiles,
    get_builtin,
    list_builtin_names,
    resolve,
)
from shared.data_sufficiency.schema import TIMEFRAME_MINUTES, Timeframe
from shared.data_sufficiency.service import _grade


UTC = timezone.utc


def _ts(mins: int) -> datetime:
    """Deterministic timestamp helper: minutes since 2026-01-01T00:00Z."""
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=mins)


# ---------------------------------------------------------------------------
# 1. Metric helpers
# ---------------------------------------------------------------------------

def test_timeframe_minutes_complete() -> None:
    assert set(TIMEFRAME_MINUTES.keys()) == {t.value for t in Timeframe}


def test_detect_gaps_contiguous() -> None:
    ts = [_ts(i) for i in range(10)]
    gaps, missing, max_gap = detect_gaps(ts, Timeframe.M1)
    assert gaps == []
    assert missing == 0
    assert max_gap == 0


def test_detect_gaps_single_gap() -> None:
    ts = [_ts(0), _ts(1), _ts(2), _ts(10), _ts(11)]  # gap 2->10 = 7 missing bars
    gaps, missing, max_gap = detect_gaps(ts, Timeframe.M1)
    assert len(gaps) == 1
    assert gaps[0].missing_bars == 7
    assert gaps[0].duration_minutes == 8
    assert missing == 7
    assert max_gap == 8


def test_detect_gaps_truncates_list() -> None:
    ts = [_ts(0)]
    for i in range(60):
        ts.append(_ts(2 + i * 2))  # 60 gaps of 1 bar each
    gaps, missing, _ = detect_gaps(ts, Timeframe.M1, max_return=5)
    assert len(gaps) == 5  # list capped
    assert missing == 60   # but total count is honest


def test_detect_gaps_4h_timeframe() -> None:
    ts = [_ts(0), _ts(240), _ts(720)]  # one 480-min gap = 1 missing 4h bar
    gaps, missing, max_gap = detect_gaps(ts, Timeframe.H4)
    assert len(gaps) == 1
    assert missing == 1
    assert max_gap == 480


def test_check_integrity_clean() -> None:
    bars = [
        {"timestamp": _ts(i), "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 10, "is_fake": False}
        for i in range(5)
    ]
    issues, fake, zero_vol, null_ohlc, impossible = check_integrity(bars)
    assert issues == []
    assert (fake, zero_vol, null_ohlc, impossible) == (0, 0, 0, 0)


def test_check_integrity_null_ohlc() -> None:
    bars = [
        {"timestamp": _ts(0), "open": None, "high": 1, "low": 0, "close": 0.5, "volume": 1},
        {"timestamp": _ts(1), "open": 1, "high": 2, "low": 0, "close": 1.5, "volume": 1},
    ]
    issues, _, _, null_ohlc, _ = check_integrity(bars)
    assert null_ohlc == 1
    kinds = {i.kind for i in issues}
    assert "null_ohlc" in kinds


def test_check_integrity_impossible_candle() -> None:
    bars = [
        {"timestamp": _ts(0), "open": 5, "high": 2, "low": 1, "close": 1.5, "volume": 1},  # h<low is impossible
    ]
    issues, _, _, _, impossible = check_integrity(bars)
    assert impossible == 1
    kinds = {i.kind for i in issues}
    assert "impossible_candle" in kinds


def test_check_integrity_zero_volume_and_allow() -> None:
    bars = [
        {"timestamp": _ts(0), "open": 1, "high": 2, "low": 0, "close": 1, "volume": 0, "is_fake": False},
    ]
    issues_strict, _, zv_strict, _, _ = check_integrity(bars, allow_zero_volume=False)
    issues_lax, _, zv_lax, _, _ = check_integrity(bars, allow_zero_volume=True)
    assert zv_strict == 1
    assert zv_lax == 0
    assert any(i.kind == "zero_or_negative_volume" for i in issues_strict)
    assert not any(i.kind == "zero_or_negative_volume" for i in issues_lax)


def test_check_integrity_is_fake_flagged() -> None:
    bars = [
        {"timestamp": _ts(0), "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1, "is_fake": True},
        {"timestamp": _ts(1), "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1, "is_fake": False},
    ]
    issues, fake, _, _, _ = check_integrity(bars)
    assert fake == 1
    assert any(i.kind == "is_fake_flagged" for i in issues)


def test_compute_coverage_empty() -> None:
    cov = compute_coverage(
        instrument_id=1,
        timeframe=Timeframe.M1,
        timestamps=[],
        integrity_counts=(0, 0, 0, 0),
    )
    assert cov.bars == 0
    assert cov.density == 0.0
    assert cov.fresh_lag_minutes is None


def test_compute_coverage_density_and_freshness() -> None:
    # 1 hour of 1m bars = 60 bars, density should be ~1.0
    ts = [_ts(i) for i in range(60)]
    now = ts[-1] + timedelta(minutes=2)  # last bar is 2 minutes old
    cov = compute_coverage(
        instrument_id=42,
        timeframe=Timeframe.M1,
        timestamps=ts,
        integrity_counts=(0, 0, 0, 0),
        now=now,
        daily_bar_target=1440,
    )
    assert cov.bars == 60
    assert cov.fresh_lag_minutes == 2
    assert cov.calendar_days == pytest.approx(59 / 1440, rel=1e-3)
    assert 0.0 < cov.density <= 1.5


def test_compute_coverage_gap_ratio() -> None:
    # 10 bars with a 4-bar gap (6 actual + 4 missing = 10 expected in span)
    ts = [_ts(0), _ts(1), _ts(2), _ts(3), _ts(4), _ts(5), _ts(10)]
    cov = compute_coverage(
        instrument_id=1,
        timeframe=Timeframe.M1,
        timestamps=ts,
        integrity_counts=(0, 0, 0, 0),
        now=ts[-1] + timedelta(minutes=1),
        daily_bar_target=1440,
    )
    assert cov.total_missing_bars == 4
    assert cov.max_gap_minutes == 5
    assert cov.gap_ratio > 0


# ---------------------------------------------------------------------------
# 2. Built-in profiles
# ---------------------------------------------------------------------------

def test_builtin_profiles_cover_every_timeframe_family() -> None:
    tfs = {p.timeframe for p in BUILTIN_PROFILES.values()}
    # We intentionally ship 1m, 15m, 1h, 4h, 1d across crypto + 15m equities.
    assert Timeframe.M1 in tfs
    assert Timeframe.M15 in tfs
    assert Timeframe.H1 in tfs
    assert Timeframe.H4 in tfs
    assert Timeframe.D1 in tfs


def test_list_builtin_names_sorted() -> None:
    names = list_builtin_names()
    assert names == sorted(names)
    assert "scalp_1m_crypto" in names


def test_get_builtin_unknown_returns_none() -> None:
    assert get_builtin("does-not-exist") is None


def test_profile_json_schema_roundtrip() -> None:
    p = BUILTIN_PROFILES["scalp_1m_crypto"]
    as_json = p.model_dump(mode="json")
    reloaded = Profile.model_validate(as_json)
    assert reloaded.name == p.name
    assert reloaded.timeframe == p.timeframe


# ---------------------------------------------------------------------------
# 3. Grading
# ---------------------------------------------------------------------------

def _profile_min() -> Profile:
    """Minimal profile for grading tests; extremely permissive so we can
    isolate the single threshold we want to test."""
    return Profile(
        name="test",
        timeframe=Timeframe.M1,
        min_bars=10,
        min_days=1,
        max_gap_ratio=0.1,
        max_gap_minutes=30,
        fresh_lag_max_minutes=120,
        daily_bar_target=1440,
    )


def _coverage(**overrides: Any) -> CoverageStats:
    base = CoverageStats(
        instrument_id=1,
        timeframe=Timeframe.M1,
        first_ts=_ts(0),
        last_ts=_ts(1440),
        bars=1440,
        calendar_days=1.0,
        expected_bars=1440,
        density=1.0,
        fresh_lag_minutes=5,
        total_missing_bars=0,
        gap_count=0,
        max_gap_minutes=0,
        is_fake_count=0,
        zero_volume_count=0,
        null_ohlc_count=0,
        impossible_candle_count=0,
    )
    data = base.model_dump()
    data.update(overrides)
    return CoverageStats.model_validate(data)


def test_grade_pass_clean() -> None:
    verdict, _, warn, fail = _grade(_coverage(), _profile_min())
    assert verdict == Verdict.PASS
    assert fail == []
    assert warn == []


def test_grade_fail_bars_too_few() -> None:
    verdict, _, _, fail = _grade(_coverage(bars=5), _profile_min())
    assert verdict == Verdict.FAIL
    assert any("min_bars" in r for r in fail)


def test_grade_fail_stale() -> None:
    verdict, _, _, fail = _grade(
        _coverage(fresh_lag_minutes=500), _profile_min(),
    )
    assert verdict == Verdict.FAIL
    assert any("fresh_lag_minutes" in r for r in fail)


def test_grade_fail_gap_ratio() -> None:
    verdict, _, _, fail = _grade(
        _coverage(total_missing_bars=300, expected_bars=1000),
        _profile_min(),
    )
    assert verdict == Verdict.FAIL
    assert any("gap_ratio" in r for r in fail)


def test_grade_fail_max_gap() -> None:
    verdict, _, _, fail = _grade(
        _coverage(max_gap_minutes=999), _profile_min(),
    )
    assert verdict == Verdict.FAIL
    assert any("max_gap_minutes" in r for r in fail)


def test_grade_fail_integrity() -> None:
    verdict, _, _, fail = _grade(
        _coverage(null_ohlc_count=3), _profile_min(),
    )
    assert verdict == Verdict.FAIL
    assert any("null_ohlc_count" in r for r in fail)

    verdict2, _, _, fail2 = _grade(
        _coverage(impossible_candle_count=1), _profile_min(),
    )
    assert verdict2 == Verdict.FAIL
    assert any("impossible_candle_count" in r for r in fail2)


def test_grade_warn_fake_bars() -> None:
    verdict, _, warn, fail = _grade(
        _coverage(is_fake_count=2), _profile_min(),
    )
    assert verdict == Verdict.PASS_WITH_WARNINGS
    assert fail == []
    assert any("is_fake_count" in r for r in warn)


def test_grade_warn_density_low() -> None:
    verdict, _, warn, _ = _grade(
        _coverage(density=0.80), _profile_min(),
    )
    assert verdict == Verdict.PASS_WITH_WARNINGS
    assert any("density" in r for r in warn)


def test_grade_warn_zero_volume_allowed() -> None:
    prof = _profile_min().model_copy(update={"allow_zero_volume": True})
    verdict, _, warn, _ = _grade(
        _coverage(zero_volume_count=5), prof,
    )
    # zero volume was tolerated -> no warning, no fail
    assert verdict == Verdict.PASS
    assert not any("zero_volume_count" in r for r in warn)


# ---------------------------------------------------------------------------
# 4. SufficiencyService against a FakePool
# ---------------------------------------------------------------------------

class FakePool:
    """Async-pool stand-in driven by per-query route tables."""

    def __init__(self) -> None:
        self.candle_bars: List[Dict[str, Any]] = []
        self.cache_rows: List[Dict[str, Any]] = []
        self.system_config_rows: List[Dict[str, Any]] = []
        self.calls: List[Tuple[str, Optional[Tuple[Any, ...]]]] = []
        self.insert_count = 0
        self.invalidate_delete_count = 7  # arbitrary return

    async def fetch_all(
        self, sql: str, params: Optional[Sequence[Any]] = None,
    ) -> List[Dict[str, Any]]:
        self.calls.append((sql, tuple(params) if params else None))
        low = sql.lower()
        if "from candles" in low:
            return list(self.candle_bars)
        if "from system_config" in low:
            return list(self.system_config_rows)
        if "from data_sufficiency_reports" in low:
            return list(self.cache_rows)
        return []

    async def fetch_one(
        self, sql: str, params: Optional[Sequence[Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        self.calls.append((sql, tuple(params) if params else None))
        low = sql.lower()
        if "insert into data_sufficiency_reports" in low:
            self.insert_count += 1
            return {"id": self.insert_count}
        if "delete from data_sufficiency_reports" in low:
            return {"n": self.invalidate_delete_count}
        if "from data_sufficiency_reports" in low:
            return self.cache_rows[0] if self.cache_rows else None
        if "from system_config" in low:
            return self.system_config_rows[0] if self.system_config_rows else None
        return None


def test_service_unknown_profile_raises() -> None:
    pool = FakePool()
    svc = SufficiencyService(pool)
    with pytest.raises(ValueError):
        asyncio.run(svc.check(instrument_id=1, profile_name="bogus"))


def test_service_fresh_check_pass() -> None:
    pool = FakePool()
    now = datetime(2026, 4, 19, 0, 0, tzinfo=UTC)

    # 2 days 1 hour of 1m bars so we're past min_days=1 easily.
    bars: List[Dict[str, Any]] = []
    total = 2 * 1440 + 60
    for i in range(total):
        ts_i = now - timedelta(minutes=(total - 1) - i)
        bars.append({
            "timestamp": ts_i,
            "open": Decimal("100"), "high": Decimal("101"),
            "low": Decimal("99"),  "close": Decimal("100"),
            "volume": Decimal("10"), "is_fake": False,
        })
    pool.candle_bars = bars

    profile = Profile(
        name="unit_test_1m",
        timeframe=Timeframe.M1,
        min_bars=30,
        min_days=1,
        max_gap_ratio=0.1,
        max_gap_minutes=5,
        fresh_lag_max_minutes=10,
        daily_bar_target=1440,
    )
    # Inject override into system_config route so resolve() picks it up.
    pool.system_config_rows = [{
        "config_key": profile.name,
        "config_value": json.dumps(profile.model_dump(mode="json")),
    }]

    report = asyncio.run(svc_check(pool, 42, profile.name, now))
    assert report.verdict == Verdict.PASS
    assert report.coverage.bars == total
    assert pool.insert_count == 1


async def svc_check(pool: FakePool, instrument_id: int, profile_name: str, now: datetime) -> SufficiencyReport:
    svc = SufficiencyService(pool)
    return await svc.check(instrument_id, profile_name, use_cache=False, now=now)


def test_service_report_for_returns_cached() -> None:
    pool = FakePool()
    cached_report = SufficiencyReport(
        instrument_id=42,
        timeframe=Timeframe.M1,
        profile_name="scalp_1m_crypto",
        verdict=Verdict.PASS,
        coverage=_coverage(),
        integrity_issues=[],
        reasons_pass=["ok"],
        reasons_warn=[],
        reasons_fail=[],
        computed_at=datetime(2026, 4, 19, tzinfo=UTC),
    )
    pool.cache_rows = [{
        "report_json": cached_report.model_dump(mode="json"),
        "computed_at": cached_report.computed_at,
        "ttl_seconds": 300,
    }]
    svc = SufficiencyService(pool)
    out = asyncio.run(svc.report_for(42, Timeframe.M1, "scalp_1m_crypto"))
    assert out is not None
    assert out.verdict == Verdict.PASS


def test_service_invalidate_executes() -> None:
    pool = FakePool()
    svc = SufficiencyService(pool)
    deleted = asyncio.run(svc.invalidate(instrument_id=42))
    assert deleted == pool.invalidate_delete_count


def test_service_cache_hit_skips_scan() -> None:
    pool = FakePool()
    now = datetime(2026, 4, 19, tzinfo=UTC)
    prior = now - timedelta(seconds=100)
    cached_report = SufficiencyReport(
        instrument_id=42,
        timeframe=Timeframe.M1,
        profile_name="scalp_1m_crypto",
        verdict=Verdict.PASS_WITH_WARNINGS,
        coverage=_coverage(),
        integrity_issues=[],
        reasons_pass=[],
        reasons_warn=["density=0.80"],
        reasons_fail=[],
        computed_at=prior,
        ttl_seconds=300,
    )
    pool.cache_rows = [{
        "report_json": cached_report.model_dump(mode="json"),
        "computed_at": prior,
        "ttl_seconds": 300,
    }]
    svc = SufficiencyService(pool)
    result = asyncio.run(svc.check(42, "scalp_1m_crypto", use_cache=True, now=now))
    assert result.verdict == Verdict.PASS_WITH_WARNINGS
    assert pool.insert_count == 0  # no write because it was a cache hit


# ---------------------------------------------------------------------------
# 5. Profiles DB loader
# ---------------------------------------------------------------------------

class ProfilePool:
    """Tiny helper: only implements fetch_all for the system_config route."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch_all(self, sql: str, params: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        return list(self._rows)


def test_profile_loader_merges_overrides() -> None:
    override = {
        "timeframe": "1m",
        "min_bars": 5,
        "min_days": 1,
        "max_gap_ratio": 0.0,
        "max_gap_minutes": 1,
        "fresh_lag_max_minutes": 1,
        "daily_bar_target": 1440,
    }
    pool = ProfilePool([{
        "config_key": "scalp_1m_crypto",
        "config_value": json.dumps(override),
    }])
    effective = asyncio.run(all_profiles(pool))
    assert effective["scalp_1m_crypto"].min_bars == 5


def test_profile_loader_skips_invalid_rows() -> None:
    pool = ProfilePool([
        {"config_key": "broken", "config_value": json.dumps({"timeframe": "WRONG"})},
        {"config_key": "scalp_1m_crypto", "config_value": json.dumps({
            "timeframe": "1m", "min_bars": 100, "min_days": 1,
            "max_gap_ratio": 0.01, "max_gap_minutes": 5,
            "fresh_lag_max_minutes": 10, "daily_bar_target": 1440,
        })},
    ])
    effective = asyncio.run(all_profiles(pool))
    assert "broken" not in effective
    assert effective["scalp_1m_crypto"].min_bars == 100


def test_profile_resolve_prefers_override() -> None:
    pool = ProfilePool([{
        "config_key": "scalp_1m_crypto",
        "config_value": json.dumps({
            "timeframe": "1m", "min_bars": 1, "min_days": 1,
            "max_gap_ratio": 0.01, "max_gap_minutes": 5,
            "fresh_lag_max_minutes": 10, "daily_bar_target": 1440,
        }),
    }])
    resolved = asyncio.run(resolve(pool, "scalp_1m_crypto"))
    assert resolved is not None
    assert resolved.min_bars == 1  # override wins, not built-in 20160


def test_profile_namespace_constant() -> None:
    assert NAMESPACE == "sufficiency.profiles"
