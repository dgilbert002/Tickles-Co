"""
Smoke tests for ValidationEngine including strategy review and autopsy.
Location: /opt/tickles/shared/tests/test_validation.py
"""

import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, timezone
from decimal import Decimal

from shared.trading.validation import (
    ValidationEngine,
    RULE1_THRESHOLD_PCT,
    VERDICT_CONTINUE,
    VERDICT_CAUTION,
    VERDICT_STOP,
    VERDICT_AVG_DELTA_THRESHOLD_PCT,
    VERDICT_PASS_RATE_CONTINUE,
    VERDICT_PASS_RATE_CAUTION,
)


class TestValidationEngine(unittest.IsolatedAsyncioTestCase):
    """Tests for single-trade validation."""

    async def asyncSetUp(self):
        self.engine = ValidationEngine("jarvais")
        self.engine.ch = MagicMock()
        self.engine._pool = MagicMock()
        self.engine._pool.fetch_one = AsyncMock()
        self.engine._pool.execute = AsyncMock()

    async def test_validate_trade_success(self):
        """Validate a closed trade against its shadow counterpart."""
        trade_id = 101
        live_trade = {
            "id": trade_id,
            "strategy_id": 5,
            "symbol": "BTC/USDT",
            "status": "closed",
            "opened_at": datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
            "entry_price": 50000.0,
            "exit_price": 51000.0,
            "quantity": 0.1,
            "net_pnl": 100.0,
            "fees": 5.0,
        }
        self.engine._pool.fetch_one.return_value = live_trade

        shadow_trade = {
            "entry_price": 49990.0,
            "exit_price": 51010.0,
            "net_pnl": 102.0,
            "fees": 4.0,
        }
        self.engine.ch.client.execute.return_value = (
            [(49990.0, 51010.0, 102.0, 4.0)],
            [("entry_price",), ("exit_price",), ("net_pnl",), ("fees",)],
        )

        result = await self.engine.validate_trade(trade_id)

        self.assertEqual(result["status"], "ok")
        deltas = result["deltas"]
        self.assertEqual(deltas["entry_price_delta"], 10.0)
        self.assertEqual(deltas["pnl_delta"], -2.0)
        self.assertTrue(deltas["rule1_pass"])

        self.engine._pool.execute.assert_called_once()
        args = self.engine._pool.execute.call_args[0]
        self.assertEqual(args[1][0], trade_id)
        self.assertEqual(float(args[1][3]), 10.0)

    async def test_validate_trade_not_found(self):
        """Validation skips when the trade doesn't exist."""
        self.engine._pool.fetch_one.return_value = None
        result = await self.engine.validate_trade(999)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "not_found_or_not_closed")


class TestStrategyReview(unittest.IsolatedAsyncioTestCase):
    """Tests for the aggregate strategy review endpoint."""

    async def asyncSetUp(self):
        self.engine = ValidationEngine("jarvais")
        self.engine.ch = MagicMock()
        self.engine._pool = MagicMock()
        self.engine._pool.fetch_one = AsyncMock()
        self.engine._pool.fetch_all = AsyncMock()
        self.engine._pool.execute = AsyncMock()

    def _make_validations(self, count: int, pass_rate: float = 1.0) -> list:
        """Generate mock validation rows with a given Rule 1 pass rate.

        pass_rate controls what fraction of trades have pnl_delta_pct within
        the 0.1% threshold. Failing trades get 0.5% delta.
        """
        rows = []
        n_pass = int(count * pass_rate)
        for i in range(count):
            passes = i < n_pass
            pnl_pct = 0.01 if passes else 0.5
            rows.append({
                "trade_id": i + 1,
                "strategy_id": 5,
                "signal_match": True,
                "entry_price_delta": 1.0,
                "exit_price_delta": -0.5,
                "pnl_delta": -1.5 if not passes else 0.5,
                "pnl_delta_pct": pnl_pct,
                "slippage_contribution": 0.1,
                "fee_contribution": 0.5,
                "data_drift_detected": False,
                "validated_at": datetime(2026, 4, 21, 12, i, tzinfo=timezone.utc),
            })
        return rows

    async def test_review_no_data(self):
        """Review returns no_data when no validations exist."""
        self.engine._pool.fetch_all.return_value = []
        result = await self.engine.review_strategy(5)
        self.assertEqual(result["status"], "no_data")

    async def test_review_continue_verdict(self):
        """Strategy with high Rule 1 pass rate gets CONTINUE verdict."""
        validations = self._make_validations(10, pass_rate=1.0)
        self.engine._pool.fetch_all.return_value = validations

        # Mock backtest stats
        self.engine.ch.client.execute.return_value = (
            [("BTC/USDT", "1h", 34.2, 1.87, -8.3, 65.0, 47, 10000, 13420, 50.0)],
            [("symbol",), ("timeframe",), ("total_return_pct",),
             ("sharpe_ratio",), ("max_drawdown_pct",), ("win_rate_pct",),
             ("total_trades",), ("initial_balance",), ("final_balance",),
             ("total_fees",)],
        )

        # Mock live stats
        self.engine._pool.fetch_one.return_value = {
            "total_trades": 10,
            "wins": 6,
            "losses": 4,
            "win_rate_pct": 60.0,
            "total_pnl": 500.0,
            "avg_pnl": 50.0,
            "best_trade": 200.0,
            "worst_trade": -80.0,
            "total_fees": 25.0,
        }

        result = await self.engine.review_strategy(5, min_trades=5)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["verdict"], VERDICT_CONTINUE)
        self.assertEqual(result["validated_trades"], 10)
        self.assertIn("aggregate", result)
        self.assertIn("comparison", result)
        self.assertIn("recommendations", result)
        self.assertGreater(len(result["recommendations"]), 0)

    async def test_review_stop_verdict(self):
        """Strategy with low Rule 1 pass rate gets STOP verdict."""
        validations = self._make_validations(10, pass_rate=0.3)
        self.engine._pool.fetch_all.return_value = validations

        self.engine.ch.client.execute.return_value = ([], [])
        self.engine._pool.fetch_one.return_value = {
            "total_trades": 10,
            "wins": 2,
            "losses": 8,
            "win_rate_pct": 20.0,
            "total_pnl": -500.0,
            "avg_pnl": -50.0,
            "best_trade": 100.0,
            "worst_trade": -200.0,
            "total_fees": 25.0,
        }

        result = await self.engine.review_strategy(5, min_trades=5)

        self.assertEqual(result["verdict"], VERDICT_STOP)

    async def test_review_caution_insufficient_data(self):
        """Strategy with fewer than min_trades gets CAUTION verdict."""
        validations = self._make_validations(3, pass_rate=1.0)
        self.engine._pool.fetch_all.return_value = validations

        self.engine.ch.client.execute.return_value = ([], [])
        self.engine._pool.fetch_one.return_value = {
            "total_trades": 3,
            "wins": 2,
            "losses": 1,
            "win_rate_pct": 66.7,
            "total_pnl": 150.0,
            "avg_pnl": 50.0,
            "best_trade": 100.0,
            "worst_trade": -30.0,
            "total_fees": 10.0,
        }

        result = await self.engine.review_strategy(5, min_trades=5)

        self.assertEqual(result["verdict"], VERDICT_CAUTION)


class TestStrategyAutopsy(unittest.IsolatedAsyncioTestCase):
    """Tests for the deep-dive strategy autopsy endpoint."""

    async def asyncSetUp(self):
        self.engine = ValidationEngine("jarvais")
        self.engine.ch = MagicMock()
        self.engine._pool = MagicMock()
        self.engine._pool.fetch_all = AsyncMock()

    async def test_autopsy_no_data(self):
        """Autopsy returns no_data when no validations exist."""
        self.engine._pool.fetch_all.return_value = []
        result = await self.engine.autopsy_strategy(5)
        self.assertEqual(result["status"], "no_data")

    async def test_autopsy_with_data(self):
        """Autopsy returns trade comparisons, patterns, and learnings."""
        validations = [
            {
                "trade_id": 1,
                "strategy_id": 5,
                "signal_match": True,
                "entry_price_delta": 5.0,
                "exit_price_delta": -2.0,
                "pnl_delta": -3.0,
                "pnl_delta_pct": 0.01,
                "slippage_contribution": 0.5,
                "fee_contribution": 1.0,
                "data_drift_detected": False,
                "validated_at": datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
            },
            {
                "trade_id": 2,
                "strategy_id": 5,
                "signal_match": True,
                "entry_price_delta": 8.0,
                "exit_price_delta": -3.0,
                "pnl_delta": -5.0,
                "pnl_delta_pct": 0.02,
                "slippage_contribution": 0.8,
                "fee_contribution": 1.5,
                "data_drift_detected": False,
                "validated_at": datetime(2026, 4, 21, 13, 0, tzinfo=timezone.utc),
            },
        ]
        self.engine._pool.fetch_all.return_value = validations

        result = await self.engine.autopsy_strategy(5)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["total_validated"], 2)
        self.assertIn("trade_comparisons", result)
        self.assertEqual(len(result["trade_comparisons"]), 2)
        self.assertIn("drift_patterns", result)
        self.assertIn("learnings", result)

    async def test_autopsy_detects_slippage_pattern(self):
        """Autopsy detects consistent adverse entry slippage pattern."""
        validations = []
        for i in range(10):
            validations.append({
                "trade_id": i + 1,
                "strategy_id": 5,
                "signal_match": True,
                "entry_price_delta": 5.0 + i,  # All positive = adverse entry
                "exit_price_delta": -1.0,
                "pnl_delta": -2.0,
                "pnl_delta_pct": 0.01,
                "slippage_contribution": 0.5 + i * 0.1,
                "fee_contribution": 1.0,
                "data_drift_detected": False,
                "validated_at": datetime(2026, 4, 21, 12, i, tzinfo=timezone.utc),
            })
        self.engine._pool.fetch_all.return_value = validations

        result = await self.engine.autopsy_strategy(5)

        patterns = result["drift_patterns"]
        self.assertGreater(patterns["patterns_found"], 0)
        pattern_names = [p["name"] for p in patterns["patterns"]]
        self.assertIn("consistent_adverse_entry_slippage", pattern_names)

        # Should have a slippage-related learning
        learnings = result["learnings"]
        categories = [l["category"] for l in learnings]
        self.assertIn("slippage", categories)


class TestVerdictComputation(unittest.TestCase):
    """Tests for the _compute_verdict method."""

    def setUp(self):
        self.engine = ValidationEngine("test")

    def test_continue_verdict(self):
        """High pass rate + low delta = CONTINUE."""
        agg = {
            "total_validated": 10,
            "rule1_pass_rate": 95.0,
            "avg_pnl_delta_pct": 0.02,
        }
        self.assertEqual(self.engine._compute_verdict(agg, 5), VERDICT_CONTINUE)

    def test_caution_verdict_marginal(self):
        """Marginal pass rate = CAUTION."""
        agg = {
            "total_validated": 10,
            "rule1_pass_rate": 75.0,
            "avg_pnl_delta_pct": 0.08,
        }
        self.assertEqual(self.engine._compute_verdict(agg, 5), VERDICT_CAUTION)

    def test_stop_verdict(self):
        """Low pass rate = STOP."""
        agg = {
            "total_validated": 10,
            "rule1_pass_rate": 50.0,
            "avg_pnl_delta_pct": 0.3,
        }
        self.assertEqual(self.engine._compute_verdict(agg, 5), VERDICT_STOP)

    def test_caution_insufficient_data(self):
        """Fewer than min_trades = CAUTION."""
        agg = {
            "total_validated": 3,
            "rule1_pass_rate": 100.0,
            "avg_pnl_delta_pct": 0.01,
        }
        self.assertEqual(self.engine._compute_verdict(agg, 5), VERDICT_CAUTION)


class TestAggregateValidations(unittest.TestCase):
    """Tests for the _aggregate_validations method."""

    def setUp(self):
        self.engine = ValidationEngine("test")

    def test_empty_validations(self):
        """Empty list returns empty dict."""
        result = self.engine._aggregate_validations([])
        self.assertEqual(result, {})

    def test_aggregate_computes_pass_rate(self):
        """Aggregate correctly computes Rule 1 pass rate."""
        validations = [
            {"trade_id": 1, "pnl_delta_pct": 0.01, "signal_match": True,
             "pnl_delta": 1.0, "slippage_contribution": 0.1, "fee_contribution": 0.5},
            {"trade_id": 2, "pnl_delta_pct": 0.02, "signal_match": True,
             "pnl_delta": 2.0, "slippage_contribution": 0.2, "fee_contribution": 0.6},
            {"trade_id": 3, "pnl_delta_pct": 0.5, "signal_match": True,
             "pnl_delta": -5.0, "slippage_contribution": 0.3, "fee_contribution": 0.7},
        ]
        result = self.engine._aggregate_validations(validations)

        self.assertEqual(result["total_validated"], 3)
        self.assertEqual(result["rule1_passes"], 2)
        self.assertEqual(result["rule1_failures"], 1)
        self.assertAlmostEqual(result["rule1_pass_rate"], 66.67, places=1)

    def test_negative_pnl_delta_pct_uses_abs(self):
        """Negative pnl_delta_pct with large magnitude should fail Rule 1.

        Regression test: _aggregate_validations must use abs() when checking
        against RULE1_THRESHOLD_PCT, otherwise a delta of -0.5% would
        incorrectly pass (since -0.5 <= 0.1 is True without abs).
        """
        validations = [
            {"trade_id": 1, "pnl_delta_pct": -0.5, "signal_match": True,
             "pnl_delta": -5.0, "slippage_contribution": 0.1, "fee_contribution": 0.5},
            {"trade_id": 2, "pnl_delta_pct": 0.01, "signal_match": True,
             "pnl_delta": 0.5, "slippage_contribution": 0.1, "fee_contribution": 0.5},
        ]
        result = self.engine._aggregate_validations(validations)

        self.assertEqual(result["rule1_passes"], 1)
        self.assertEqual(result["rule1_failures"], 1)


class TestCompareProfitability(unittest.TestCase):
    """Tests for the _compare_profitability method."""

    def setUp(self):
        self.engine = ValidationEngine("test")

    def test_backtest_matches_live(self):
        """Small win rate drift = backtest_matches_live."""
        backtest = {"total_return_pct": 34.2, "win_rate_pct": 65.0}
        live = {"win_rate_pct": 63.0, "total_pnl": 500.0}
        aggregate = {"avg_slippage_contribution": 0.1, "avg_fee_contribution": 0.5}

        result = self.engine._compare_profitability(backtest, live, aggregate)
        self.assertEqual(result["profitability_assessment"], "backtest_matches_live")
        self.assertEqual(result["primary_drift_source"], "fees")

    def test_live_underperforming(self):
        """Large negative win rate drift = live_underperforming."""
        backtest = {"total_return_pct": 34.2, "win_rate_pct": 65.0}
        live = {"win_rate_pct": 50.0, "total_pnl": -200.0}
        aggregate = {"avg_slippage_contribution": 2.0, "avg_fee_contribution": 0.5}

        result = self.engine._compare_profitability(backtest, live, aggregate)
        self.assertEqual(result["profitability_assessment"], "live_underperforming")
        self.assertEqual(result["primary_drift_source"], "slippage")


class TestDriftPatternDetection(unittest.TestCase):
    """Tests for the _detect_drift_patterns method."""

    def setUp(self):
        self.engine = ValidationEngine("test")

    def test_no_patterns(self):
        """Clean validations with non-adverse deltas produce no patterns."""
        validations = [
            {"entry_price_delta": -0.1, "exit_price_delta": 0.05,
             "slippage_contribution": 0.01, "fee_contribution": 0.1,
             "data_drift_detected": False},
            {"entry_price_delta": -0.2, "exit_price_delta": 0.03,
             "slippage_contribution": 0.02, "fee_contribution": 0.2,
             "data_drift_detected": False},
            {"entry_price_delta": 0.05, "exit_price_delta": -0.01,
             "slippage_contribution": 0.01, "fee_contribution": 0.1,
             "data_drift_detected": False},
        ]
        result = self.engine._detect_drift_patterns(validations)
        self.assertEqual(result["patterns_found"], 0)

    def test_detects_adverse_entry_slippage(self):
        """Consistently positive entry deltas trigger adverse entry pattern."""
        validations = []
        for i in range(8):
            validations.append({
                "entry_price_delta": 5.0 + i,
                "exit_price_delta": -1.0,
                "slippage_contribution": 0.5,
                "fee_contribution": 0.5,
                "data_drift_detected": False,
            })
        result = self.engine._detect_drift_patterns(validations)
        self.assertGreater(result["patterns_found"], 0)
        names = [p["name"] for p in result["patterns"]]
        self.assertIn("consistent_adverse_entry_slippage", names)

    def test_detects_data_drift(self):
        """Data drift flags are detected."""
        validations = [
            {"entry_price_delta": 0.1, "exit_price_delta": -0.05,
             "slippage_contribution": 0.01, "fee_contribution": 0.1,
             "data_drift_detected": True},
        ]
        result = self.engine._detect_drift_patterns(validations)
        names = [p["name"] for p in result["patterns"]]
        self.assertIn("data_drift", names)


class TestGenerateLearnings(unittest.TestCase):
    """Tests for the _generate_learnings method."""

    def setUp(self):
        self.engine = ValidationEngine("test")

    def test_no_patterns_stable(self):
        """No patterns + all Rule 1 passes = stable learning."""
        patterns = {"patterns_found": 0, "patterns": []}
        validations = [
            {"pnl_delta_pct": 0.01},
            {"pnl_delta_pct": 0.02},
        ]
        learnings = self.engine._generate_learnings(patterns, validations)
        self.assertEqual(len(learnings), 1)
        self.assertEqual(learnings[0]["category"], "general")
        self.assertIn("No changes needed", learnings[0]["action"])

    def test_slippage_learning(self):
        """Adverse entry slippage pattern generates slippage learning."""
        patterns = {
            "patterns_found": 1,
            "patterns": [{
                "name": "consistent_adverse_entry_slippage",
                "severity": "high",
                "description": "8/8 trades had adverse entry slippage.",
            }],
        }
        validations = [{"pnl_delta_pct": 0.01}]
        learnings = self.engine._generate_learnings(patterns, validations)
        categories = [l["category"] for l in learnings]
        self.assertIn("slippage", categories)

    def test_fee_drift_learning(self):
        """Fee drift pattern generates fee learning."""
        patterns = {
            "patterns_found": 1,
            "patterns": [{
                "name": "fee_drift",
                "severity": "medium",
                "description": "5/8 trades had fee drift > $1.",
            }],
        }
        validations = [{"pnl_delta_pct": 0.01}]
        learnings = self.engine._generate_learnings(patterns, validations)
        categories = [l["category"] for l in learnings]
        self.assertIn("fees", categories)


if __name__ == "__main__":
    unittest.main()
