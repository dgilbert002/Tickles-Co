"""
shared.data_sufficiency — the Data Sufficiency Engine (Phase 15).

Purpose
-------
Before any strategy, backtest, forward-test, indicator fit, or regime
classifier is allowed to act on a (venue, instrument, timeframe) tuple,
this engine answers:

    "Is the data good enough to trust?"

Verdicts:

    PASS                 — profile thresholds are all met
    PASS_WITH_WARNINGS   — soft violations (still usable, but logged)
    FAIL                 — hard violations; caller MUST abort or switch TF

The engine is purely deterministic (Rule-11 infrastructure, NOT an agent).
LLM-judged reviews of the REPORTS happen later in the Rule-1 auditor (P21).

Design notes
------------
* Reads the existing `candles` Postgres table (instrument_id, timeframe,
  timestamp, OHLCV, is_fake, source). No new dependency on the future
  ClickHouse / QuestDB layer (those land in Phase 16/22).
* Profiles live in `system_config` (namespace='sufficiency.profiles') so
  operators can tune them without a code deploy. Built-ins ship as a
  safety net if the table is empty.
* Reports are cached in `data_sufficiency_reports` with a TTL so we don't
  re-scan 100k+ bars for every caller. Cache is invalidated on each
  candle-daemon sync (Phase 16 hooks into `invalidate()`).
* Integrity checks reject NULL OHLC, high<low, open/close outside
  [low,high], non-positive volume (configurable), and flagged
  `is_fake=TRUE` bars.
"""
from shared.data_sufficiency.schema import (
    CoverageStats,
    Gap,
    IntegrityIssue,
    Profile,
    SufficiencyReport,
    Verdict,
)
from shared.data_sufficiency.service import SufficiencyService

__all__ = [
    "CoverageStats",
    "Gap",
    "IntegrityIssue",
    "Profile",
    "SufficiencyReport",
    "SufficiencyService",
    "Verdict",
]
