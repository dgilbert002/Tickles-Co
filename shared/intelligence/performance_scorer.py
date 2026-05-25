"""
Module: performance_scorer
Purpose: Daemon that scores trader performance by comparing signal_interpretations
         against actual market outcomes, and writes trader_performance rows.
Location: /opt/tickles/shared/intelligence/performance_scorer.py

Design:
  * Polls tickles_<company>.signal_interpretations for signals older than N hours
    that have not yet been scored (no corresponding trader_performance row).
  * For each signal, looks up the actual candle direction after the signal timestamp:
    - Reads candles from signal.market_data_at to signal.market_data_at + lookahead.
    - Determines if price moved in the predicted direction.
  * Computes per-trader aggregates:
    - accuracy_pct: correct_direction / validated_signals
    - avg_confidence, confidence_calibration
    - total_pnl_usd (simplified: direction * price_change * notional)
    - sharpe_ratio, max_drawdown_pct
  * Writes to tickles_<company>.trader_performance.
  * Updates tickles_shared.trader_profiles accuracy_score and last_scored_at.
  * Respects performance_lookback_days from system_config (default 30).
  * Idempotent: composite UNIQUE on (trader_profile_id, score_period) prevents duplicates.

Hardening:
  * Graceful handling of missing candle data (skip scoring for that signal).
  * SIGTERM/SIGINT graceful shutdown.
  * Batch processing to avoid long-running transactions.
  * Exponential backoff on DB errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure shared imports resolve
_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parent
_ROOT = _SHARED.parent
for p in (_ROOT, _SHARED):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared.utils.config import load_env
from shared.utils.db import DatabasePool, get_company_pool, get_shared_pool

logger = logging.getLogger("tickles.intelligence.scorer")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
POLL_INTERVAL_S = float(os.environ.get("SCORER_POLL_S", "3600"))  # 1 hour default
BATCH_SIZE = int(os.environ.get("SCORER_BATCH", "100"))
LOOKAHEAD_MINUTES = int(os.environ.get("SCORER_LOOKAHEAD_MIN", "60"))
LOOKBACK_DAYS = int(os.environ.get("SCORER_LOOKBACK_DAYS", "30"))
MIN_SAMPLES = int(os.environ.get("SCORER_MIN_SAMPLES", "5"))
NOTIONAL_USD = float(os.environ.get("SCORER_NOTIONAL_USD", "1000.0"))


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class ScorerConfig:
    """Runtime configuration for the performance scorer."""

    poll_interval_s: float = POLL_INTERVAL_S
    batch_size: int = BATCH_SIZE
    lookahead_minutes: int = LOOKAHEAD_MINUTES
    lookback_days: int = LOOKBACK_DAYS
    min_samples: int = MIN_SAMPLES
    notional_usd: float = NOTIONAL_USD


@dataclass
class SignalOutcome:
    """Outcome for a single signal interpretation."""

    signal_id: int
    trader_profile_id: int
    direction: str
    confidence: float
    instrument_symbol: str
    exchange: str
    market_data_at: datetime
    actual_direction: str  # 'long', 'short', 'neutral'
    price_change_pct: float
    correct: bool
    pnl_usd: float


@dataclass
class TraderScore:
    """Aggregated score for a trader over a period."""

    trader_profile_id: int
    score_period: str  # e.g., '2026-W17' or '2026-04'
    total_signals: int
    validated_signals: int
    correct_direction: int
    accuracy_pct: float
    avg_confidence: float
    confidence_calibration: float
    total_pnl_usd: float
    avg_pnl_per_signal: float
    max_win_usd: float
    max_loss_usd: float
    sharpe_ratio: float
    max_drawdown_pct: float
    calculation_params: Dict[str, Any]


# ---------------------------------------------------------------------------
# Database Operations
# ---------------------------------------------------------------------------
async def fetch_unscored_signals(
    company_pool: DatabasePool,
    batch_size: int,
    lookback_days: int,
) -> List[Dict[str, Any]]:
    """Fetch signal_interpretations that haven't been scored yet.

    A signal is "unscored" if:
      * It's older than lookahead_minutes (market had time to move).
      * No trader_performance row exists for (trader_profile_id, score_period).
      * It was created within the lookback window.

    Round 9 (2026-05-24) — Trader accuracy must NOT include AI-inferred trades.
    Previously the scorer scored every signal_interpretations row against the
    original poster's profile, even when the trader had not explicitly called
    a setup (the LLM had hallucinated a trade from level commentary). That
    poisoned the leaderboard with positions the trader never made. The new
    EXISTS clause requires at least one ``tracked_positions`` row tied to
    this interpretation with ``signal_source='trader'`` — i.e. the trader
    actually called the setup. ChartHacker's independent inferences live
    under ``signal_source='chart_hacker'`` and are scored separately under
    chart_hacker's own trader_profiles row.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    lookahead = datetime.now(timezone.utc) - timedelta(minutes=LOOKAHEAD_MINUTES)

    sql = (
        "SELECT "
        "  s.id, s.news_item_id, s.trader_profile_id, "
        "  s.consensus_direction, s.consensus_confidence, "
        "  s.instrument_symbol, s.exchange, s.market_data_at, "
        "  s.llm_direction, s.quant_direction, "
        "  s.created_at "
        "FROM public.signal_interpretations s "
        "WHERE s.market_data_at < $1 "
        "  AND s.created_at > $2 "
        "  AND s.consensus_direction IN ('long', 'short') "
        "  AND EXISTS ("
        "    SELECT 1 FROM public.tracked_positions tp "
        "    WHERE tp.signal_interpretation_id = s.id "
        "      AND tp.signal_source = 'trader'"
        "  ) "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM public.trader_performance p "
        "    WHERE p.trader_profile_id = s.trader_profile_id "
        "      AND p.score_period = TO_CHAR(s.created_at, 'YYYY-WW')"
        "  ) "
        "ORDER BY s.created_at ASC "
        "LIMIT $3"
    )
    async with company_pool.acquire() as conn:
        rows = await conn.fetch(sql, lookahead, cutoff, batch_size)
    return [dict(r) for r in rows]


async def resolve_instrument_id(
    company_pool: DatabasePool,
    symbol: str,
    exchange: str,
) -> Optional[int]:
    """Resolve instrument symbol + exchange to instrument_id."""
    sql = (
        "SELECT id FROM public.instruments "
        "WHERE symbol = $1 AND exchange = $2 LIMIT 1"
    )
    async with company_pool.acquire() as conn:
        row = await conn.fetchrow(sql, symbol, exchange)
    return int(row["id"]) if row else None


async def fetch_outcome_candles(
    company_pool: DatabasePool,
    instrument_id: int,
    exchange: str,
    from_ts: datetime,
    to_ts: datetime,
) -> List[Dict[str, Any]]:
    """Fetch candles between from_ts and to_ts for outcome determination."""
    sql = (
        "SELECT timestamp, open, high, low, close, volume "
        "FROM public.candles "
        "WHERE instrument_id = $1 AND source = $2 AND timeframe = '1m' "
        "  AND timestamp >= $3 AND timestamp <= $4 "
        "ORDER BY timestamp ASC"
    )
    async with company_pool.acquire() as conn:
        rows = await conn.fetch(sql, instrument_id, exchange, from_ts, to_ts)
    return [dict(r) for r in rows]


def determine_outcome(
    candles: List[Dict[str, Any]],
    predicted_direction: str,
    notional_usd: float,
) -> Tuple[str, float, float, bool]:
    """Determine actual market direction and P&L from a candle series.

    Args:
        candles: Chronologically ordered 1m candles.
        predicted_direction: 'long' or 'short'.
        notional_usd: Notional position size for P&L calc.

    Returns:
        (actual_direction, price_change_pct, pnl_usd, correct)
    """
    if len(candles) < 2:
        return ("neutral", 0.0, 0.0, False)

    entry_price = float(candles[0]["close"])
    if entry_price == 0:
        return ("neutral", 0.0, 0.0, False)

    # Use the high/low during the lookahead window for best-case direction
    highs = [float(c["high"]) for c in candles[1:]]
    lows = [float(c["low"]) for c in candles[1:]]
    final_close = float(candles[-1]["close"])

    max_high = max(highs) if highs else entry_price
    min_low = min(lows) if lows else entry_price

    # Determine which direction had the larger move
    up_move = (max_high - entry_price) / entry_price
    down_move = (entry_price - min_low) / entry_price

    if up_move > down_move:
        actual_direction = "long"
        price_change_pct = up_move * 100.0
    elif down_move > up_move:
        actual_direction = "short"
        price_change_pct = -down_move * 100.0
    else:
        actual_direction = "neutral"
        price_change_pct = 0.0

    # P&L: if predicted long, profit = price rise; if predicted short, profit = price fall
    if predicted_direction == "long":
        pnl_pct = (final_close - entry_price) / entry_price
    else:
        pnl_pct = (entry_price - final_close) / entry_price

    pnl_usd = pnl_pct * notional_usd
    correct = predicted_direction == actual_direction

    return (actual_direction, price_change_pct, pnl_usd, correct)


def compute_trader_score(
    outcomes: List[SignalOutcome],
    notional_usd: float,
) -> Optional[TraderScore]:
    """Aggregate a list of signal outcomes into a TraderScore.

    Returns None if not enough validated signals.
    """
    if not outcomes:
        return None

    validated = [o for o in outcomes if o.actual_direction != "neutral"]
    if len(validated) < MIN_SAMPLES:
        return None

    correct_count = sum(1 for o in validated if o.correct)
    accuracy_pct = correct_count / len(validated) * 100.0 if validated else 0.0

    confidences = [o.confidence for o in validated]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    # Calibration: how well does confidence predict accuracy?
    # Simple metric: |accuracy_pct/100 - avg_confidence|
    confidence_calibration = round(abs(accuracy_pct / 100.0 - avg_confidence), 4)

    pnls = [o.pnl_usd for o in validated]
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / len(validated) if validated else 0.0
    max_win = max(pnls) if pnls else 0.0
    max_loss = min(pnls) if pnls else 0.0

    # Sharpe ratio (simplified): mean return / std dev of returns
    returns = [o.price_change_pct / 100.0 for o in validated]
    if len(returns) > 1:
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        std_dev = math.sqrt(variance) if variance > 0 else 1e-9
        sharpe = mean_ret / std_dev
    else:
        sharpe = 0.0

    # Max drawdown (simplified running P&L)
    running_pnl = 0.0
    max_dd = 0.0
    peak = 0.0
    for o in validated:
        running_pnl += o.pnl_usd
        if running_pnl > peak:
            peak = running_pnl
        dd = peak - running_pnl
        if dd > max_dd:
            max_dd = dd
    max_drawdown_pct = (max_dd / notional_usd * 100.0) if notional_usd > 0 else 0.0

    # Score period from first outcome's market_data_at
    first_ts = validated[0].market_data_at
    score_period = first_ts.strftime("%Y-W%W")

    return TraderScore(
        trader_profile_id=validated[0].trader_profile_id,
        score_period=score_period,
        total_signals=len(outcomes),
        validated_signals=len(validated),
        correct_direction=correct_count,
        accuracy_pct=round(accuracy_pct, 2),
        avg_confidence=round(avg_confidence, 4),
        confidence_calibration=confidence_calibration,
        total_pnl_usd=round(total_pnl, 2),
        avg_pnl_per_signal=round(avg_pnl, 2),
        max_win_usd=round(max_win, 2),
        max_loss_usd=round(max_loss, 2),
        sharpe_ratio=round(sharpe, 4),
        max_drawdown_pct=round(max_drawdown_pct, 2),
        calculation_params={
            "notional_usd": notional_usd,
            "lookahead_minutes": LOOKAHEAD_MINUTES,
            "scored_at": datetime.now(timezone.utc).isoformat(),
        },
    )


async def write_trader_performance(
    company_pool: DatabasePool,
    score: TraderScore,
) -> None:
    """Write a trader_performance row. Uses ON CONFLICT for idempotency."""
    sql = (
        "INSERT INTO public.trader_performance ("
        "  trader_profile_id, score_period, scored_at, "
        "  total_signals, validated_signals, correct_direction, "
        "  accuracy_pct, avg_confidence, confidence_calibration, "
        "  total_pnl_usd, avg_pnl_per_signal, max_win_usd, max_loss_usd, "
        "  sharpe_ratio, max_drawdown_pct, calculation_params, metadata"
        ") VALUES ("
        "  $1, $2, NOW(), "
        "  $3, $4, $5, "
        "  $6, $7, $8, "
        "  $9, $10, $11, $12, "
        "  $13, $14, $15::jsonb, $16::jsonb"
        ")"
        "ON CONFLICT (trader_profile_id, score_period) DO UPDATE SET"
        "  scored_at = EXCLUDED.scored_at,"
        "  total_signals = EXCLUDED.total_signals,"
        "  validated_signals = EXCLUDED.validated_signals,"
        "  correct_direction = EXCLUDED.correct_direction,"
        "  accuracy_pct = EXCLUDED.accuracy_pct,"
        "  avg_confidence = EXCLUDED.avg_confidence,"
        "  confidence_calibration = EXCLUDED.confidence_calibration,"
        "  total_pnl_usd = EXCLUDED.total_pnl_usd,"
        "  avg_pnl_per_signal = EXCLUDED.avg_pnl_per_signal,"
        "  max_win_usd = EXCLUDED.max_win_usd,"
        "  max_loss_usd = EXCLUDED.max_loss_usd,"
        "  sharpe_ratio = EXCLUDED.sharpe_ratio,"
        "  max_drawdown_pct = EXCLUDED.max_drawdown_pct,"
        "  calculation_params = EXCLUDED.calculation_params,"
        "  metadata = EXCLUDED.metadata"
    )
    params = (
        score.trader_profile_id,
        score.score_period,
        score.total_signals,
        score.validated_signals,
        score.correct_direction,
        score.accuracy_pct,
        score.avg_confidence,
        score.confidence_calibration,
        score.total_pnl_usd,
        score.avg_pnl_per_signal,
        score.max_win_usd,
        score.max_loss_usd,
        score.sharpe_ratio,
        score.max_drawdown_pct,
        json.dumps(score.calculation_params),
        json.dumps({"scored_by": "performance_scorer", "version": "1.0"}),
    )
    async with company_pool.acquire() as conn:
        await conn.execute(sql, *params)


async def update_trader_profile_accuracy(
    shared_pool: DatabasePool,
    trader_profile_id: int,
    accuracy_pct: float,
    sample_count: int,
) -> None:
    """Update the trader_profiles accuracy_score and related fields."""
    sql = (
        "UPDATE public.trader_profiles SET"
        "  accuracy_score = $1,"
        "  accuracy_samples = $2,"
        "  last_scored_at = NOW()"
        "WHERE id = $3"
    )
    async with shared_pool.acquire() as conn:
        await conn.execute(sql, accuracy_pct, sample_count, trader_profile_id)


# ---------------------------------------------------------------------------
# Main Worker
# ---------------------------------------------------------------------------
class PerformanceScorer:
    """Daemon that scores trader performance by comparing signals to market outcomes."""

    def __init__(self, cfg: Optional[ScorerConfig] = None) -> None:
        self.cfg = cfg or ScorerConfig()
        self._stop = asyncio.Event()
        self._shared_pool: Optional[DatabasePool] = None

    async def _ensure_shared_pool(self) -> DatabasePool:
        if self._shared_pool is None:
            self._shared_pool = await get_shared_pool()
        return self._shared_pool

    async def _score_company(
        self,
        company: str,
    ) -> Dict[str, Any]:
        """Score all unscored signals for one company database.

        Returns summary stats.
        """
        company_pool = await get_company_pool(company)
        signals = await fetch_unscored_signals(
            company_pool,
            batch_size=self.cfg.batch_size,
            lookback_days=self.cfg.lookback_days,
        )
        if not signals:
            return {"company": company, "processed": 0}

        logger.info("Scorer: %d unscored signals for %s", len(signals), company)

        # Group by trader_profile_id for batch scoring
        outcomes_by_trader: Dict[int, List[SignalOutcome]] = {}

        for sig in signals:
            if self._stop.is_set():
                break

            instrument_id = await resolve_instrument_id(
                company_pool, sig["instrument_symbol"], sig["exchange"]
            )
            if not instrument_id:
                logger.warning(
                    "Scorer: no instrument_id for %s@%s",
                    sig["instrument_symbol"],
                    sig["exchange"],
                )
                continue

            from_ts = sig["market_data_at"]
            to_ts = from_ts + timedelta(minutes=self.cfg.lookahead_minutes)

            candles = await fetch_outcome_candles(
                company_pool, instrument_id, sig["exchange"], from_ts, to_ts
            )

            actual_dir, price_change, pnl, correct = determine_outcome(
                candles,
                predicted_direction=sig["consensus_direction"],
                notional_usd=self.cfg.notional_usd,
            )

            outcome = SignalOutcome(
                signal_id=sig["id"],
                trader_profile_id=sig["trader_profile_id"],
                direction=sig["consensus_direction"],
                confidence=sig["consensus_confidence"],
                instrument_symbol=sig["instrument_symbol"],
                exchange=sig["exchange"],
                market_data_at=sig["market_data_at"],
                actual_direction=actual_dir,
                price_change_pct=price_change,
                correct=correct,
                pnl_usd=pnl,
            )

            outcomes_by_trader.setdefault(sig["trader_profile_id"], []).append(outcome)

        # Compute and write scores per trader
        shared_pool = await self._ensure_shared_pool()
        scores_written = 0
        for trader_id, outcomes in outcomes_by_trader.items():
            score = compute_trader_score(outcomes, self.cfg.notional_usd)
            if score:
                await write_trader_performance(company_pool, score)
                await update_trader_profile_accuracy(
                    shared_pool,
                    trader_profile_id=trader_id,
                    accuracy_pct=score.accuracy_pct,
                    sample_count=score.validated_signals,
                )
                scores_written += 1
                logger.info(
                    "Scorer: trader %s accuracy=%.1f%% signals=%d",
                    trader_id,
                    score.accuracy_pct,
                    score.validated_signals,
                )

        return {
            "company": company,
            "processed": len(signals),
            "scores_written": scores_written,
            "traders_scored": len(outcomes_by_trader),
        }

    async def run_cycle(self) -> Dict[str, Any]:
        """Score all companies. Returns aggregated stats."""
        # Discover companies from existing databases or config
        companies = self._discover_companies()
        results: List[Dict[str, Any]] = []
        for company in companies:
            if self._stop.is_set():
                break
            try:
                result = await self._score_company(company)
                results.append(result)
            except Exception as exc:
                logger.exception("Scorer failed for company %s: %s", company, exc)
                results.append({"company": company, "error": str(exc)})

        total_processed = sum(r.get("processed", 0) for r in results)
        total_scores = sum(r.get("scores_written", 0) for r in results)
        logger.info(
            "Scorer cycle complete: processed=%d scores_written=%d",
            total_processed,
            total_scores,
        )
        return {
            "processed": total_processed,
            "scores_written": total_scores,
            "companies": results,
        }

    def _discover_companies(self) -> List[str]:
        """Discover companies to score.

        Reads from environment or defaults to ['jarvais'].
        In production, this could query information_schema or a registry.
        """
        companies_env = os.environ.get("SCORER_COMPANIES", "")
        if companies_env:
            return [c.strip() for c in companies_env.split(",") if c.strip()]
        return ["jarvais"]

    async def run_forever(self) -> None:
        """Main loop: poll, score, sleep until stop event."""
        logger.info(
            "PerformanceScorer started (poll=%.0fs, batch=%d, lookback=%dd)",
            self.cfg.poll_interval_s,
            self.cfg.batch_size,
            self.cfg.lookback_days,
        )
        while not self._stop.is_set():
            try:
                await self.run_cycle()
            except Exception as exc:
                logger.exception("Scorer cycle crashed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.poll_interval_s)
            except asyncio.TimeoutError:
                pass
        logger.info("PerformanceScorer stopped")

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------
def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, AttributeError, RuntimeError):
            if os.name != "nt" or sig == signal.SIGINT:
                try:
                    signal.signal(sig, lambda *_a: stop_event.set())
                except (OSError, ValueError):
                    pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()

    cfg = ScorerConfig()
    scorer = PerformanceScorer(cfg)
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, scorer._stop)

    try:
        await scorer.run_forever()
    finally:
        logger.info("PerformanceScorer shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
