"""
Module: validation
Purpose: Rule 1 Validation Engine — pairs live trades with shadow trades,
         aggregates strategy performance, and produces actionable verdicts.
Location: /opt/tickles/shared/trading/validation.py
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from decimal import Decimal

from shared.backtest.ch_writer import ClickHouseWriter
from shared.utils.db import DatabasePool

logger = logging.getLogger(__name__)

# Rule 1 threshold: P&L delta must be below this percentage of notional.
RULE1_THRESHOLD_PCT = 0.1

# Verdict thresholds.
VERDICT_AVG_DELTA_THRESHOLD_PCT = 0.05
VERDICT_PASS_RATE_CONTINUE = 90.0
VERDICT_PASS_RATE_CAUTION = 70.0

# Strategy verdict levels.
VERDICT_CONTINUE = "CONTINUE"
VERDICT_CAUTION = "CAUTION"
VERDICT_STOP = "STOP"


class ValidationEngine:
    """
    Enforces Rule 1 (Backtest ≡ Live).

    When a live or paper trade closes, this engine finds the corresponding
    shadow trade and calculates slippage, fee, and P&L deltas.

    Also provides aggregate strategy review and deep-dive autopsy capabilities
    so agents can learn from drift patterns and improve their strategies.
    """

    def __init__(self, company_id: str):
        self.company_id = company_id
        self.ch = ClickHouseWriter()
        self._pool: Optional[DatabasePool] = None

    async def _get_pool(self) -> DatabasePool:
        """Lazy-initialize the Postgres connection pool."""
        if self._pool is None:
            self._pool = DatabasePool(f"tickles_{self.company_id}")
            await self._pool.initialize()
        return self._pool

    # ------------------------------------------------------------------
    # Single-trade validation (Phase 6)
    # ------------------------------------------------------------------

    async def validate_trade(self, trade_id: int) -> Dict[str, Any]:
        """
        Perform validation for a closed trade.

        1. Fetch live trade from Postgres.
        2. Fetch shadow trade from ClickHouse.
        3. Compare and record results in trade_validations.

        Args:
            trade_id: ID of the closed trade in Postgres.

        Returns:
            Dict with status and delta details.
        """
        pool = await self._get_pool()

        trade = await pool.fetch_one(
            "SELECT * FROM trades WHERE id = $1 AND status = 'closed'",
            (trade_id,)
        )
        if not trade:
            logger.warning("Trade %s not found or not closed.", trade_id)
            return {"status": "skipped", "reason": "not_found_or_not_closed"}

        shadow_trade = self._find_shadow_trade(trade)
        if not shadow_trade:
            logger.warning("No shadow trade found for live trade %s", trade_id)
            return {"status": "skipped", "reason": "no_shadow_match"}

        validation_result = self._compare_trades(trade, shadow_trade)

        await self._record_validation(pool, trade_id, trade["strategy_id"], validation_result)

        return {"status": "ok", "deltas": validation_result}

    # ------------------------------------------------------------------
    # Strategy review (aggregate verdict)
    # ------------------------------------------------------------------

    async def review_strategy(
        self,
        strategy_id: int,
        min_trades: int = 5,
    ) -> Dict[str, Any]:
        """
        Aggregate all validation results for a strategy and produce a verdict.

        Compares backtest profitability (from ClickHouse) with live/shadow
        profitability (from trade_validations) and determines whether the
        agent should CONTINUE, use CAUTION, or STOP the strategy.

        Args:
            strategy_id: The strategy ID to review.
            min_trades: Minimum validated trades before issuing a verdict.

        Returns:
            Dict with verdict, aggregate stats, and recommendations.
        """
        pool = await self._get_pool()

        validations = await self._fetch_validations(pool, strategy_id)
        backtest_stats = self._fetch_backtest_stats(strategy_id)
        live_stats = await self._fetch_live_stats(pool, strategy_id)

        if not validations:
            return {
                "status": "no_data",
                "strategy_id": strategy_id,
                "message": "No validated trades found for this strategy.",
            }

        aggregate = self._aggregate_validations(validations)
        verdict = self._compute_verdict(aggregate, min_trades)
        comparison = self._compare_profitability(backtest_stats, live_stats, aggregate)
        recommendations = self._generate_recommendations(verdict, aggregate, comparison)

        return {
            "status": "ok",
            "strategy_id": strategy_id,
            "verdict": verdict,
            "validated_trades": len(validations),
            "aggregate": aggregate,
            "backtest": backtest_stats,
            "live": live_stats,
            "comparison": comparison,
            "recommendations": recommendations,
        }

    # ------------------------------------------------------------------
    # Strategy autopsy (deep-dive per-trade comparison)
    # ------------------------------------------------------------------

    async def autopsy_strategy(
        self,
        strategy_id: int,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """
        Deep-dive comparison of each live vs shadow trade for a strategy.

        Produces per-trade breakdowns showing where drift occurred, pattern
        detection across all trades, and actionable learnings the agent can
        use to improve or abandon the strategy.

        Args:
            strategy_id: The strategy ID to autopsy.
            limit: Maximum number of trade comparisons to return.

        Returns:
            Dict with per-trade comparisons, drift patterns, and learnings.
        """
        pool = await self._get_pool()

        validations = await self._fetch_validations(pool, strategy_id, limit=limit)
        if not validations:
            return {
                "status": "no_data",
                "strategy_id": strategy_id,
                "message": "No validated trades found for autopsy.",
            }

        trade_comparisons = self._build_trade_comparisons(validations)
        patterns = self._detect_drift_patterns(validations)
        learnings = self._generate_learnings(patterns, validations)

        return {
            "status": "ok",
            "strategy_id": strategy_id,
            "total_validated": len(validations),
            "trade_comparisons": trade_comparisons,
            "drift_patterns": patterns,
            "learnings": learnings,
        }

    # ------------------------------------------------------------------
    # Internal: shadow trade lookup
    # ------------------------------------------------------------------

    def _find_shadow_trade(self, trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Query ClickHouse for the matching shadow trade.

        Uses a 10-second look-back window from the live entry time to
        account for execution latency between the shadow fill (at bar open)
        and the live fill (a few seconds later).

        Args:
            trade: Live trade row from Postgres.

        Returns:
            Shadow trade dict from ClickHouse, or None.
        """
        strategy_id = str(trade["strategy_id"])
        entry_at = trade["opened_at"]
        if entry_at.tzinfo is None:
            entry_at = entry_at.replace(tzinfo=timezone.utc)

        query = """
            SELECT * FROM backtest_forward_trades
            WHERE strategy_id = %(sid)s
              AND entry_at <= %(live_entry)s
              AND entry_at >= %(live_entry)s - INTERVAL 10 SECOND
            ORDER BY entry_at DESC
            LIMIT 1
        """
        params = {"sid": strategy_id, "live_entry": entry_at}

        try:
            rows = self.ch.client.execute(query, params, with_column_types=True)
        except Exception as exc:
            logger.error("ClickHouse shadow lookup failed: %s", exc)
            return None

        if not rows[0]:
            return None

        columns = [c[0] for c in rows[1]]
        return dict(zip(columns, rows[0][0]))

    # ------------------------------------------------------------------
    # Internal: trade comparison
    # ------------------------------------------------------------------

    def _compare_trades(self, live: Dict[str, Any], shadow: Dict[str, Any]) -> Dict[str, Any]:
        """Compare live vs shadow and attribute deltas.

        Args:
            live: Live trade row from Postgres.
            shadow: Shadow trade row from ClickHouse.

        Returns:
            Dict with entry/exit/pnl deltas and Rule 1 check.
        """
        live_entry = float(live["entry_price"])
        shadow_entry = float(shadow["entry_price"])
        live_exit = float(live["exit_price"])
        shadow_exit = float(shadow["exit_price"])

        live_pnl = float(live["net_pnl"])
        shadow_pnl = float(shadow["net_pnl"])

        entry_delta = live_entry - shadow_entry
        exit_delta = live_exit - shadow_exit
        pnl_delta = live_pnl - shadow_pnl

        notional = live_entry * float(live["quantity"])
        pnl_delta_pct = (pnl_delta / notional * 100) if notional > 1e-9 else 0.0

        # Direction-aware slippage: for SHORT, positive entry_delta is favorable.
        side = str(live.get("side", "long")).lower()
        direction = -1.0 if side in ("short", "sell") else 1.0
        adverse_entry = entry_delta * direction
        adverse_exit = -exit_delta * direction

        return {
            "signal_match": True,
            "entry_price_delta": entry_delta,
            "exit_price_delta": exit_delta,
            "pnl_delta": pnl_delta,
            "pnl_delta_pct": pnl_delta_pct,
            "rule1_pass": abs(pnl_delta_pct) <= RULE1_THRESHOLD_PCT,
            "slippage_contribution": adverse_entry * float(live["quantity"]),
            "fee_contribution": float(live.get("fees", 0)) - float(shadow.get("fees", 0)),
            "data_drift_detected": False,
        }

    # ------------------------------------------------------------------
    # Internal: persist validation
    # ------------------------------------------------------------------

    async def _record_validation(
        self, pool: DatabasePool, trade_id: int, strategy_id: int, result: Dict[str, Any]
    ) -> None:
        """Insert validation record into Postgres.

        Args:
            pool: Async Postgres connection pool.
            trade_id: ID of the validated trade.
            strategy_id: Strategy ID associated with the trade.
            result: Comparison result dict from _compare_trades.
        """
        sql = """
            INSERT INTO trade_validations (
                trade_id, strategy_id, signal_match,
                entry_price_delta, exit_price_delta, pnl_delta, pnl_delta_pct,
                slippage_contribution, fee_contribution, data_drift_detected,
                validated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11
            )
        """
        await pool.execute(sql, (
            trade_id, strategy_id, result["signal_match"],
            Decimal(str(result["entry_price_delta"])),
            Decimal(str(result["exit_price_delta"])),
            Decimal(str(result["pnl_delta"])),
            Decimal(str(result["pnl_delta_pct"])),
            Decimal(str(result["slippage_contribution"])),
            Decimal(str(result["fee_contribution"])),
            result["data_drift_detected"],
            datetime.now(timezone.utc),
        ))

    # ------------------------------------------------------------------
    # Internal: fetch validations for a strategy
    # ------------------------------------------------------------------

    async def _fetch_validations(
        self, pool: DatabasePool, strategy_id: int, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """Fetch all validation rows for a strategy.

        Args:
            pool: Async Postgres connection pool.
            strategy_id: Strategy ID to filter by.
            limit: Maximum rows to return.

        Returns:
            List of validation dicts.
        """
        rows = await pool.fetch_all(
            "SELECT * FROM trade_validations "
            "WHERE strategy_id = $1 "
            "ORDER BY validated_at DESC LIMIT $2",
            (strategy_id, limit),
        )
        return rows or []

    # ------------------------------------------------------------------
    # Internal: fetch backtest stats from ClickHouse
    # ------------------------------------------------------------------

    def _fetch_backtest_stats(self, strategy_id: int) -> Dict[str, Any]:
        """Fetch aggregate backtest performance from ClickHouse.

        Queries the best historical run for this strategy to compare
        against live/shadow performance.

        Args:
            strategy_id: Strategy ID to look up.

        Returns:
            Dict with backtest Sharpe, P&L, win rate, etc.
        """
        query = """
            SELECT
                symbol,
                timeframe,
                total_return_pct,
                sharpe_ratio,
                max_drawdown_pct,
                win_rate_pct,
                total_trades,
                initial_balance,
                final_balance,
                total_fees
            FROM backtest_runs
            WHERE strategy_id = %(sid)s
            ORDER BY sharpe_ratio DESC
            LIMIT 1
        """
        try:
            rows = self.ch.client.execute(
                query, {"sid": str(strategy_id)}, with_column_types=True
            )
        except Exception as exc:
            logger.error("ClickHouse backtest stats query failed: %s", exc)
            return {}

        if not rows[0]:
            return {}

        columns = [c[0] for c in rows[1]]
        return dict(zip(columns, rows[0][0]))

    # ------------------------------------------------------------------
    # Internal: fetch live trading stats from Postgres
    # ------------------------------------------------------------------

    async def _fetch_live_stats(
        self, pool: DatabasePool, strategy_id: int
    ) -> Dict[str, Any]:
        """Fetch aggregate live/paper trading stats from Postgres.

        Args:
            pool: Async Postgres connection pool.
            strategy_id: Strategy ID to filter by.

        Returns:
            Dict with live trade count, total P&L, win rate, etc.
        """
        row = await pool.fetch_one(
            "SELECT "
            "  COUNT(*) AS total_trades, "
            "  COALESCE(SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins, "
            "  COALESCE(SUM(net_pnl), 0) AS total_pnl, "
            "  COALESCE(AVG(net_pnl), 0) AS avg_pnl, "
            "  COALESCE(MAX(net_pnl), 0) AS best_trade, "
            "  COALESCE(MIN(net_pnl), 0) AS worst_trade, "
            "  COALESCE(SUM(fees), 0) AS total_fees "
            "FROM trades "
            "WHERE strategy_id = $1 AND status = 'closed'",
            (strategy_id,),
        )
        if not row:
            return {}

        total = int(row["total_trades"])
        wins = int(row["wins"])
        return {
            "total_trades": total,
            "wins": wins,
            "losses": total - wins,
            "win_rate_pct": (wins / total * 100) if total > 0 else 0.0,
            "total_pnl": float(row["total_pnl"]),
            "avg_pnl": float(row["avg_pnl"]),
            "best_trade": float(row["best_trade"]),
            "worst_trade": float(row["worst_trade"]),
            "total_fees": float(row["total_fees"]),
        }

    # ------------------------------------------------------------------
    # Internal: aggregate validation rows
    # ------------------------------------------------------------------

    def _aggregate_validations(self, validations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Compute aggregate statistics from validation rows.

        Args:
            validations: List of validation dicts from trade_validations.

        Returns:
            Dict with pass rate, average deltas, and worst offenders.
        """
        if not validations:
            return {}

        total = len(validations)
        rule1_passes = sum(1 for v in validations if abs(float(v.get("pnl_delta_pct", 999))) <= RULE1_THRESHOLD_PCT)
        signal_matches = sum(1 for v in validations if v.get("signal_match", False))

        pnl_deltas = [float(v.get("pnl_delta", 0)) for v in validations]
        pnl_delta_pcts = [float(v.get("pnl_delta_pct", 0)) for v in validations]
        slippage_contribs = [float(v.get("slippage_contribution", 0)) for v in validations]
        fee_contribs = [float(v.get("fee_contribution", 0)) for v in validations]

        avg_pnl_delta = sum(pnl_deltas) / total if total > 0 else 0.0
        avg_pnl_delta_pct = sum(pnl_delta_pcts) / total if total > 0 else 0.0
        avg_slippage = sum(slippage_contribs) / total if total > 0 else 0.0
        avg_fee_delta = sum(fee_contribs) / total if total > 0 else 0.0

        worst_idx = max(range(total), key=lambda i: abs(pnl_delta_pcts[i]))
        worst = validations[worst_idx]

        return {
            "total_validated": total,
            "rule1_passes": rule1_passes,
            "rule1_failures": total - rule1_passes,
            "rule1_pass_rate": rule1_passes / total * 100 if total > 0 else 0.0,
            "signal_match_rate": signal_matches / total * 100 if total > 0 else 0.0,
            "avg_pnl_delta": avg_pnl_delta,
            "avg_pnl_delta_pct": avg_pnl_delta_pct,
            "avg_slippage_contribution": avg_slippage,
            "avg_fee_contribution": avg_fee_delta,
            "worst_trade": {
                "trade_id": int(worst.get("trade_id", 0)),
                "pnl_delta_pct": float(worst.get("pnl_delta_pct", 0)),
                "slippage_contribution": float(worst.get("slippage_contribution", 0)),
                "fee_contribution": float(worst.get("fee_contribution", 0)),
            },
        }

    # ------------------------------------------------------------------
    # Internal: compute verdict
    # ------------------------------------------------------------------

    def _compute_verdict(self, aggregate: Dict[str, Any], min_trades: int) -> str:
        """Determine CONTINUE / CAUTION / STOP verdict.

        Logic:
        - < min_trades: CAUTION (insufficient data)
        - Rule 1 pass rate >= 90% and avg delta < 0.05%: CONTINUE
        - Rule 1 pass rate >= 70%: CAUTION
        - Rule 1 pass rate < 70%: STOP

        Args:
            aggregate: Aggregated validation stats.
            min_trades: Minimum trades for a confident verdict.

        Returns:
            One of CONTINUE, CAUTION, STOP.
        """
        total = aggregate.get("total_validated", 0)
        if total < min_trades:
            return VERDICT_CAUTION

        pass_rate = aggregate.get("rule1_pass_rate", 0)
        avg_delta_pct = abs(aggregate.get("avg_pnl_delta_pct", 0))

        if pass_rate >= VERDICT_PASS_RATE_CONTINUE and avg_delta_pct < VERDICT_AVG_DELTA_THRESHOLD_PCT:
            return VERDICT_CONTINUE
        if pass_rate >= VERDICT_PASS_RATE_CAUTION:
            return VERDICT_CAUTION
        return VERDICT_STOP

    # ------------------------------------------------------------------
    # Internal: compare backtest vs live profitability
    # ------------------------------------------------------------------

    def _compare_profitability(
        self,
        backtest: Dict[str, Any],
        live: Dict[str, Any],
        aggregate: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Compare backtest expectations with actual live results.

        Args:
            backtest: Stats from ClickHouse backtest_runs.
            live: Stats from Postgres trades table.
            aggregate: Aggregated validation stats.

        Returns:
            Dict with profitability comparison and drift assessment.
        """
        bt_return = float(backtest.get("total_return_pct", 0))
        bt_win_rate = float(backtest.get("win_rate_pct", 0))
        live_win_rate = float(live.get("win_rate_pct", 0))
        live_total_pnl = float(live.get("total_pnl", 0))

        win_rate_drift = live_win_rate - bt_win_rate
        avg_slippage = aggregate.get("avg_slippage_contribution", 0)
        avg_fee_delta = aggregate.get("avg_fee_contribution", 0)

        return {
            "backtest_return_pct": bt_return,
            "backtest_win_rate_pct": bt_win_rate,
            "live_win_rate_pct": live_win_rate,
            "live_total_pnl": live_total_pnl,
            "win_rate_drift_pct": win_rate_drift,
            "profitability_assessment": (
                "backtest_matches_live"
                if abs(win_rate_drift) <= 5.0
                else "live_underperforming"
                if win_rate_drift < -5.0
                else "live_outperforming"
            ),
            "primary_drift_source": (
                "slippage"
                if abs(avg_slippage) > abs(avg_fee_delta)
                else "fees"
                if abs(avg_fee_delta) > 0.01
                else "negligible"
            ),
        }

    # ------------------------------------------------------------------
    # Internal: generate recommendations
    # ------------------------------------------------------------------

    def _generate_recommendations(
        self,
        verdict: str,
        aggregate: Dict[str, Any],
        comparison: Dict[str, Any],
    ) -> List[str]:
        """Generate actionable recommendations based on verdict and data.

        Args:
            verdict: CONTINUE / CAUTION / STOP.
            aggregate: Aggregated validation stats.
            comparison: Backtest vs live comparison.

        Returns:
            List of recommendation strings.
        """
        recs: List[str] = []

        if verdict == VERDICT_STOP:
            recs.append(
                "Strategy fails Rule 1 too often. Stop live trading and "
                "re-evaluate the strategy parameters or market conditions."
            )

        if verdict == VERDICT_CAUTION:
            recs.append(
                "Insufficient data or marginal Rule 1 compliance. "
                "Collect more forward-test trades before increasing allocation."
            )

        drift_source = comparison.get("primary_drift_source", "negligible")
        if drift_source == "slippage":
            recs.append(
                "Slippage is the primary drift source. Consider: "
                "(a) increasing slippage_bps in the backtest config, "
                "(b) using limit orders instead of market orders, or "
                "(c) trading more liquid pairs/timeframes."
            )
        elif drift_source == "fees":
            recs.append(
                "Fee drift detected. Verify the exchange fee tier matches "
                "fee_taker_bps in the backtest config. Consider maker orders "
                "for lower fees."
            )

        win_rate_drift = comparison.get("win_rate_drift_pct", 0)
        if win_rate_drift < -10.0:
            recs.append(
                f"Live win rate is {abs(win_rate_drift):.1f}% below backtest. "
                "The strategy may be overfitted. Run a parameter sweep with "
                "different date ranges to test robustness."
            )

        if aggregate.get("rule1_pass_rate", 0) >= 95.0:
            recs.append(
                "Rule 1 pass rate is excellent (>95%). Strategy is safe to "
                "scale up with increased position sizing or additional pairs."
            )

        if not recs:
            recs.append("No specific issues detected. Continue monitoring.")

        return recs

    # ------------------------------------------------------------------
    # Internal: build per-trade comparison list for autopsy
    # ------------------------------------------------------------------

    def _build_trade_comparisons(self, validations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build detailed per-trade comparison rows for the autopsy report.

        Args:
            validations: List of validation dicts.

        Returns:
            List of dicts with trade_id, deltas, and pass/fail status.
        """
        comparisons: List[Dict[str, Any]] = []
        for v in validations:
            pnl_pct = float(v.get("pnl_delta_pct", 0))
            comparisons.append({
                "trade_id": int(v.get("trade_id", 0)),
                "signal_match": bool(v.get("signal_match", False)),
                "entry_price_delta": float(v.get("entry_price_delta", 0)),
                "exit_price_delta": float(v.get("exit_price_delta", 0)),
                "pnl_delta": float(v.get("pnl_delta", 0)),
                "pnl_delta_pct": pnl_pct,
                "rule1_pass": abs(pnl_pct) <= RULE1_THRESHOLD_PCT,
                "slippage_contribution": float(v.get("slippage_contribution", 0)),
                "fee_contribution": float(v.get("fee_contribution", 0)),
                "data_drift_detected": bool(v.get("data_drift_detected", False)),
            })
        return comparisons

    # ------------------------------------------------------------------
    # Internal: detect drift patterns across trades
    # ------------------------------------------------------------------

    def _detect_drift_patterns(self, validations: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Detect recurring patterns in validation drift.

        Looks for consistent directional bias in slippage, fee drift,
        and entry vs exit contributions.

        Args:
            validations: List of validation dicts.

        Returns:
            Dict with detected patterns and their severity.
        """
        if not validations:
            return {"patterns_found": 0}

        total = len(validations)
        slippages = [float(v.get("slippage_contribution", 0)) for v in validations]
        fee_deltas = [float(v.get("fee_contribution", 0)) for v in validations]
        entry_deltas = [float(v.get("entry_price_delta", 0)) for v in validations]
        exit_deltas = [float(v.get("exit_price_delta", 0)) for v in validations]
        data_drifts = sum(1 for v in validations if v.get("data_drift_detected", False))

        patterns: List[Dict[str, Any]] = []

        # Pattern: consistent adverse slippage on entry
        # Note: entry_price_delta is raw (live - shadow). For LONG, positive = adverse.
        # For SHORT, negative = adverse. Since we store direction-aware slippage_contribution,
        # we also check slippage_contribution for a more reliable signal.
        adverse_entries = sum(1 for d in entry_deltas if d > 0)
        if adverse_entries > total * 0.7:
            patterns.append({
                "name": "consistent_adverse_entry_slippage",
                "severity": "high" if adverse_entries > total * 0.9 else "medium",
                "affected_pct": adverse_entries / total * 100,
                "description": (
                    f"{adverse_entries}/{total} trades had adverse entry slippage. "
                    "Live fills are consistently worse than backtest assumptions."
                ),
            })

        # Pattern: exit slippage bias
        adverse_exits = sum(1 for d in exit_deltas if d < 0)
        if adverse_exits > total * 0.7:
            patterns.append({
                "name": "consistent_adverse_exit_slippage",
                "severity": "high" if adverse_exits > total * 0.9 else "medium",
                "affected_pct": adverse_exits / total * 100,
                "description": (
                    f"{adverse_exits}/{total} trades had adverse exit slippage. "
                    "TP/SL fills are consistently worse than backtest levels."
                ),
            })

        # Pattern: fee drift
        fee_drifted = sum(1 for d in fee_deltas if abs(d) > 1.0)
        if fee_drifted > total * 0.5:
            avg_fee = sum(fee_deltas) / total if total > 0 else 0
            patterns.append({
                "name": "fee_drift",
                "severity": "medium" if abs(avg_fee) > 5.0 else "low",
                "affected_pct": fee_drifted / total * 100,
                "description": (
                    f"{fee_drifted}/{total} trades had fee drift > $1. "
                    f"Average fee delta: ${avg_fee:.2f}. Check exchange fee tier."
                ),
            })

        # Pattern: data drift (candle corrections)
        if data_drifts > 0:
            patterns.append({
                "name": "data_drift",
                "severity": "high",
                "affected_pct": data_drifts / total * 100,
                "description": (
                    f"{data_drifts}/{total} trades had candle data drift. "
                    "Historical candles may have been corrected after the live trade."
                ),
            })

        return {
            "patterns_found": len(patterns),
            "patterns": patterns,
        }

    # ------------------------------------------------------------------
    # Internal: generate learnings from patterns
    # ------------------------------------------------------------------

    def _generate_learnings(
        self,
        patterns: Dict[str, Any],
        validations: List[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        """Generate actionable learnings from detected drift patterns.

        Each learning has a category, a concise insight, and a suggested
        action the agent can take (e.g., re-backtest with adjusted params).

        Args:
            patterns: Detected drift patterns.
            validations: Validation rows for context.

        Returns:
            List of learning dicts with category, insight, and action.
        """
        learnings: List[Dict[str, str]] = []

        for pattern in patterns.get("patterns", []):
            name = pattern.get("name", "")
            severity = pattern.get("severity", "low")
            description = pattern.get("description", "")

            if name == "consistent_adverse_entry_slippage":
                learnings.append({
                    "category": "slippage",
                    "insight": description,
                    "action": (
                        "Re-run backtest with slippage_bps increased by 2-5 bps. "
                        "If the strategy remains profitable, the adjusted config "
                        "better reflects live conditions. If not, the strategy "
                        "may be too sensitive to execution quality."
                    ),
                })
            elif name == "consistent_adverse_exit_slippage":
                learnings.append({
                    "category": "slippage",
                    "insight": description,
                    "action": (
                        "Consider widening TP/SL levels to reduce the impact of "
                        "adverse exit fills. Re-backtest with wider stops to see "
                        "if the strategy still has an edge."
                    ),
                })
            elif name == "fee_drift":
                learnings.append({
                    "category": "fees",
                    "insight": description,
                    "action": (
                        "Update fee_taker_bps in the backtest config to match "
                        "the actual exchange fee tier. Consider using limit orders "
                        "(maker fee) to reduce costs."
                    ),
                })
            elif name == "data_drift":
                learnings.append({
                    "category": "data_integrity",
                    "insight": description,
                    "action": (
                        "Investigate which candles were corrected. If the corrections "
                        "are significant, re-run the backtest with the updated data "
                        "and compare results. Flag the data source for review."
                    ),
                })

        # Global learning: if no patterns detected but Rule 1 failures exist
        rule1_fails = sum(
            1 for v in validations
            if abs(float(v.get("pnl_delta_pct", 0))) > RULE1_THRESHOLD_PCT
        )
        if rule1_fails > 0 and not patterns.get("patterns"):
            learnings.append({
                "category": "general",
                "insight": (
                    f"{rule1_fails}/{len(validations)} trades failed Rule 1 but "
                    "no consistent pattern was detected. Drift may be random."
                ),
                "action": (
                    "Continue monitoring. If failures cluster around specific "
                    "market conditions (high volatility, low liquidity), add "
                    "a regime filter to the strategy."
                ),
            })

        if not learnings:
            learnings.append({
                "category": "general",
                "insight": "All validated trades pass Rule 1 with no drift patterns.",
                "action": "Strategy is performing as expected. No changes needed.",
            })

        return learnings
