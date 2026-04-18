"""
shared.data_sufficiency.service — async read/verdict API over the
existing `candles` table and the Phase-15 `data_sufficiency_reports`
cache.

Wired into the shared asyncpg pool via shared.utils.db.get_shared_pool();
tests substitute a FakePool exposing `fetch_all`/`fetch_one`.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shared.data_sufficiency import profiles as profile_lib
from shared.data_sufficiency.metrics import check_integrity, compute_coverage
from shared.data_sufficiency.schema import (
    CoverageStats,
    Profile,
    SufficiencyReport,
    Timeframe,
    Verdict,
)

log = logging.getLogger("tickles.sufficiency.service")


class SufficiencyService:
    """Read candles -> metrics -> verdict. Optional report cache."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        log.debug("SufficiencyService initialised with pool=%r", pool)

    # ---- profiles passthrough -----------------------------------------

    async def list_profiles(self) -> List[Profile]:
        merged = await profile_lib.all_profiles(self._pool)
        return sorted(merged.values(), key=lambda p: p.name)

    async def get_profile(self, name: str) -> Optional[Profile]:
        return await profile_lib.resolve(self._pool, name)

    # ---- public verdict path ------------------------------------------

    async def check(
        self,
        instrument_id: int,
        profile_name: str,
        *,
        use_cache: bool = True,
        now: Optional[datetime] = None,
    ) -> SufficiencyReport:
        """Grade one (instrument, profile) pair.

        Will consult `data_sufficiency_reports` first when `use_cache=True`
        (respecting `ttl_seconds`) and fall through to a fresh scan.
        """
        log.debug(
            "check(instrument_id=%d, profile_name=%s, use_cache=%s)",
            instrument_id, profile_name, use_cache,
        )
        profile = await self.get_profile(profile_name)
        if profile is None:
            raise ValueError(f"Unknown sufficiency profile: {profile_name!r}")

        now = now or datetime.now(timezone.utc)

        if use_cache:
            cached = await self._read_cache(instrument_id, profile, now)
            if cached is not None:
                return cached

        report = await self._fresh_check(instrument_id, profile, now)
        await self._write_cache(report)
        return report

    async def report_for(
        self,
        instrument_id: int,
        timeframe: Timeframe,
        profile_name: str,
    ) -> Optional[SufficiencyReport]:
        """Fetch the most recent cached report without forcing a refresh."""
        log.debug(
            "report_for(instrument_id=%d, timeframe=%s, profile_name=%s)",
            instrument_id, timeframe.value, profile_name,
        )
        row = await self._pool.fetch_one(
            """
            SELECT report_json, computed_at, ttl_seconds
              FROM data_sufficiency_reports
             WHERE instrument_id=$1 AND timeframe=$2 AND profile_name=$3
             ORDER BY computed_at DESC
             LIMIT 1
            """,
            (instrument_id, timeframe.value, profile_name),
        )
        if row is None:
            return None
        payload = row["report_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return SufficiencyReport.model_validate(payload)

    async def invalidate(
        self,
        *,
        instrument_id: Optional[int] = None,
        timeframe: Optional[Timeframe] = None,
    ) -> int:
        """Drop cached rows; called by the candle-daemon after a sync."""
        log.debug(
            "invalidate(instrument_id=%s, timeframe=%s)",
            instrument_id, timeframe,
        )
        clauses: List[str] = []
        params: List[Any] = []
        if instrument_id is not None:
            clauses.append(f"instrument_id = ${len(params) + 1}")
            params.append(instrument_id)
        if timeframe is not None:
            clauses.append(f"timeframe = ${len(params) + 1}")
            params.append(timeframe.value)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        row = await self._pool.fetch_one(
            f"WITH d AS (DELETE FROM data_sufficiency_reports {where} RETURNING 1) "
            f"SELECT COUNT(*) AS n FROM d",
            tuple(params) if params else None,
        )
        return int(row["n"]) if row else 0

    async def stats(self) -> Dict[str, int]:
        """Small overview that powers `sufficiency_cli stats`."""
        rows = await self._pool.fetch_all(
            """
            SELECT 'reports'               AS k, COUNT(*) AS v FROM data_sufficiency_reports
            UNION ALL SELECT 'pass',       COUNT(*) FROM data_sufficiency_reports WHERE verdict='pass'
            UNION ALL SELECT 'warn',       COUNT(*) FROM data_sufficiency_reports WHERE verdict='pass_with_warnings'
            UNION ALL SELECT 'fail',       COUNT(*) FROM data_sufficiency_reports WHERE verdict='fail'
            UNION ALL SELECT 'instruments_covered',
                     COUNT(DISTINCT instrument_id) FROM data_sufficiency_reports
            """
        )
        return {r["k"]: int(r["v"]) for r in rows}

    # ---- internals -----------------------------------------------------

    async def _fresh_check(
        self, instrument_id: int, profile: Profile, now: datetime,
    ) -> SufficiencyReport:
        bars = await self._fetch_bars(instrument_id, profile)
        coverage = self._roll_up(instrument_id, profile, bars, now)
        integrity_issues, _, _, _, _ = check_integrity(
            bars, allow_zero_volume=profile.allow_zero_volume,
        )
        verdict, reasons_pass, reasons_warn, reasons_fail = _grade(
            coverage, profile,
        )
        return SufficiencyReport(
            instrument_id=instrument_id,
            timeframe=profile.timeframe,
            profile_name=profile.name,
            verdict=verdict,
            coverage=coverage,
            gaps=[],  # gaps roll-up already counted; service only exports summaries
            integrity_issues=integrity_issues,
            reasons_pass=reasons_pass,
            reasons_warn=reasons_warn,
            reasons_fail=reasons_fail,
            computed_at=now,
            ttl_seconds=300,
        )

    async def _fetch_bars(
        self, instrument_id: int, profile: Profile,
    ) -> List[Dict[str, Any]]:
        """Pull every candle for (instrument, timeframe) ordered ASC.

        For very long history profiles this can be hundreds of thousands
        of rows — acceptable for Phase 15 since the daemon caches results.
        Phase 16 will add windowed scans for ultra-long profiles.
        """
        rows = await self._pool.fetch_all(
            """
            SELECT timestamp, open, high, low, close, volume, is_fake
              FROM candles
             WHERE instrument_id = $1 AND timeframe = $2
             ORDER BY timestamp ASC
            """,
            (instrument_id, profile.timeframe.value),
        )
        return [dict(r) for r in rows]

    def _roll_up(
        self,
        instrument_id: int,
        profile: Profile,
        bars: List[Dict[str, Any]],
        now: datetime,
    ) -> CoverageStats:
        timestamps = [b["timestamp"] for b in bars if b.get("timestamp") is not None]
        issues, fake, zero_vol, null_ohlc, impossible = check_integrity(
            bars, allow_zero_volume=profile.allow_zero_volume,
        )
        return compute_coverage(
            instrument_id,
            profile.timeframe,
            timestamps,
            (fake, zero_vol, null_ohlc, impossible),
            now=now,
            daily_bar_target=profile.daily_bar_target,
        )

    async def _read_cache(
        self, instrument_id: int, profile: Profile, now: datetime,
    ) -> Optional[SufficiencyReport]:
        row = await self._pool.fetch_one(
            """
            SELECT report_json, computed_at, ttl_seconds
              FROM data_sufficiency_reports
             WHERE instrument_id=$1 AND timeframe=$2 AND profile_name=$3
             ORDER BY computed_at DESC
             LIMIT 1
            """,
            (instrument_id, profile.timeframe.value, profile.name),
        )
        if row is None:
            return None
        computed_at = row["computed_at"]
        ttl = int(row["ttl_seconds"] or 0)
        if computed_at is None or ttl <= 0:
            return None
        age = (now - computed_at).total_seconds()
        if age > ttl:
            return None
        payload = row["report_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return SufficiencyReport.model_validate(payload)

    async def _write_cache(self, report: SufficiencyReport) -> None:
        cache_row = report.to_cache_row()
        await self._pool.fetch_one(
            """
            INSERT INTO data_sufficiency_reports (
                instrument_id, timeframe, profile_name, verdict, bars,
                first_ts, last_ts, gap_ratio, max_gap_minutes,
                fresh_lag_minutes, report_json, computed_at, ttl_seconds
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (instrument_id, timeframe, profile_name) DO UPDATE
              SET verdict=EXCLUDED.verdict,
                  bars=EXCLUDED.bars,
                  first_ts=EXCLUDED.first_ts,
                  last_ts=EXCLUDED.last_ts,
                  gap_ratio=EXCLUDED.gap_ratio,
                  max_gap_minutes=EXCLUDED.max_gap_minutes,
                  fresh_lag_minutes=EXCLUDED.fresh_lag_minutes,
                  report_json=EXCLUDED.report_json,
                  computed_at=EXCLUDED.computed_at,
                  ttl_seconds=EXCLUDED.ttl_seconds
            RETURNING id
            """,
            (
                cache_row["instrument_id"],
                cache_row["timeframe"],
                cache_row["profile_name"],
                cache_row["verdict"],
                cache_row["bars"],
                cache_row["first_ts"],
                cache_row["last_ts"],
                cache_row["gap_ratio"],
                cache_row["max_gap_minutes"],
                cache_row["fresh_lag_minutes"],
                json.dumps(cache_row["report_json"], default=str, sort_keys=True),
                cache_row["computed_at"],
                cache_row["ttl_seconds"],
            ),
        )


# ---------------------------------------------------------------------------
# Grading — pure function so tests can target it directly.
# ---------------------------------------------------------------------------

def _grade(
    coverage: CoverageStats, profile: Profile,
) -> tuple:
    """Return (verdict, pass_reasons, warn_reasons, fail_reasons)."""
    reasons_pass: List[str] = []
    reasons_warn: List[str] = []
    reasons_fail: List[str] = []

    # --- hard fails ---
    if coverage.bars < profile.min_bars:
        reasons_fail.append(
            f"bars={coverage.bars} < min_bars={profile.min_bars}"
        )
    if coverage.calendar_days < float(profile.min_days):
        reasons_fail.append(
            f"calendar_days={coverage.calendar_days:.2f} < min_days={profile.min_days}"
        )
    if coverage.max_gap_minutes > profile.max_gap_minutes:
        reasons_fail.append(
            f"max_gap_minutes={coverage.max_gap_minutes} "
            f"> allowed={profile.max_gap_minutes}"
        )
    if coverage.gap_ratio > profile.max_gap_ratio:
        reasons_fail.append(
            f"gap_ratio={coverage.gap_ratio:.4f} > allowed={profile.max_gap_ratio:.4f}"
        )
    if coverage.null_ohlc_count > 0:
        reasons_fail.append(f"null_ohlc_count={coverage.null_ohlc_count}")
    if coverage.impossible_candle_count > 0:
        reasons_fail.append(
            f"impossible_candle_count={coverage.impossible_candle_count}"
        )
    if (
        coverage.fresh_lag_minutes is not None
        and coverage.fresh_lag_minutes > profile.fresh_lag_max_minutes
    ):
        reasons_fail.append(
            f"fresh_lag_minutes={coverage.fresh_lag_minutes} "
            f"> allowed={profile.fresh_lag_max_minutes}"
        )

    # --- warnings (soft) ---
    if coverage.is_fake_count > 0 and not profile.allow_is_fake:
        reasons_warn.append(f"is_fake_count={coverage.is_fake_count}")
    if coverage.zero_volume_count > 0 and not profile.allow_zero_volume:
        reasons_warn.append(
            f"zero_volume_count={coverage.zero_volume_count}"
        )
    if coverage.density < 0.95 and not reasons_fail:
        reasons_warn.append(
            f"density={coverage.density:.3f} (< 0.95 expected)"
        )

    # --- pass reasons (used for logging) ---
    if not reasons_fail:
        reasons_pass.append(
            f"bars={coverage.bars} >= {profile.min_bars}, "
            f"days={coverage.calendar_days:.1f} >= {profile.min_days}, "
            f"gap_ratio={coverage.gap_ratio:.4f} <= {profile.max_gap_ratio}"
        )

    if reasons_fail:
        verdict = Verdict.FAIL
    elif reasons_warn:
        verdict = Verdict.PASS_WITH_WARNINGS
    else:
        verdict = Verdict.PASS
    return verdict, reasons_pass, reasons_warn, reasons_fail
