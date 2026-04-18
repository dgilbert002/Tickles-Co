"""
JarvAIs Performance Engine
Tracks, compares, and analyzes trading performance across multiple dimensions.

Core Comparisons:
1. EA Baseline P&L — What would have happened if we took every EA signal blindly
2. JarvAIs Actual P&L — What actually happened with AI validation
3. AI Alpha — The difference (JarvAIs P&L - EA Baseline P&L)

Key Metrics:
- Veto Accuracy: How often the AI correctly blocked losing trades
- Confidence Calibration: Is the AI's confidence score actually predictive?
- Learning Curve: Is the AI getting smarter over time?
- Correct Approvals / Mistaken Approvals / Correct Vetoes / Missed Opportunities

Usage:
    from analytics.performance_engine import get_performance_engine
    pe = get_performance_engine()
    report = pe.generate_daily_report("DEMO_001")
    comparison = pe.get_ea_vs_ai_comparison("DEMO_001", days=30)
"""

import logging
from datetime import datetime, date, timedelta
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

logger = logging.getLogger("jarvais.performance")


# ─────────────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────────────

@dataclass
class TradeClassification:
    """Classification of an AI decision relative to outcome."""
    correct_approvals: int = 0       # AI approved → trade won
    mistaken_approvals: int = 0      # AI approved → trade lost
    correct_vetoes: int = 0          # AI vetoed → EA signal would have lost
    missed_opportunities: int = 0    # AI vetoed → EA signal would have won
    correct_approval_pnl: float = 0.0
    mistaken_approval_pnl: float = 0.0
    correct_veto_saved: float = 0.0  # Money saved by vetoing losers
    missed_opportunity_cost: float = 0.0  # Money missed by vetoing winners

    @property
    def total_signals(self) -> int:
        return (self.correct_approvals + self.mistaken_approvals +
                self.correct_vetoes + self.missed_opportunities)

    @property
    def veto_accuracy(self) -> float:
        """How often vetoes were correct (blocked losers)."""
        total_vetoes = self.correct_vetoes + self.missed_opportunities
        if total_vetoes == 0:
            return 0.0
        return self.correct_vetoes / total_vetoes

    @property
    def approval_accuracy(self) -> float:
        """How often approvals were correct (winners)."""
        total_approvals = self.correct_approvals + self.mistaken_approvals
        if total_approvals == 0:
            return 0.0
        return self.correct_approvals / total_approvals

    @property
    def ai_net_value(self) -> float:
        """Net financial value added by the AI."""
        return (self.correct_approval_pnl + self.correct_veto_saved +
                self.mistaken_approval_pnl - self.missed_opportunity_cost)


@dataclass
class PerformanceSnapshot:
    """Complete performance snapshot for a period."""
    period_start: str = ""
    period_end: str = ""
    account_id: str = ""

    # EA Baseline
    ea_total_signals: int = 0
    ea_hypothetical_pnl: float = 0.0
    ea_hypothetical_wins: int = 0
    ea_hypothetical_losses: int = 0
    ea_hypothetical_win_rate: float = 0.0

    # JarvAIs Actual
    jarvais_total_trades: int = 0
    jarvais_actual_pnl: float = 0.0
    jarvais_wins: int = 0
    jarvais_losses: int = 0
    jarvais_win_rate: float = 0.0

    # AI Alpha
    ai_alpha: float = 0.0  # JarvAIs P&L - EA P&L
    ai_alpha_pct: float = 0.0

    # Decision Quality
    classification: TradeClassification = field(default_factory=TradeClassification)

    # Confidence Calibration
    avg_confidence_winners: float = 0.0
    avg_confidence_losers: float = 0.0
    confidence_spread: float = 0.0  # Winners avg - Losers avg (higher = better calibrated)

    # Risk Metrics
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    # Learning Metrics
    first_half_win_rate: float = 0.0
    second_half_win_rate: float = 0.0
    learning_improvement: float = 0.0  # second_half - first_half


# ─────────────────────────────────────────────────────────────────────
# Performance Engine Class
# ─────────────────────────────────────────────────────────────────────

class PerformanceEngine:
    """
    Comprehensive performance tracking and analysis engine.
    Compares EA baseline vs AI-filtered performance and measures learning.
    """

    def __init__(self):
        self._db = None
        logger.info("Performance Engine initialized")

    @property
    def db(self):
        if self._db is None:
            from db.database import get_db
            self._db = get_db()
        return self._db

    # ─────────────────────────────────────────────────────────────────
    # EA vs AI Comparison
    # ─────────────────────────────────────────────────────────────────

    def get_ea_vs_ai_comparison(self, account_id: str,
                                 days: int = 30) -> PerformanceSnapshot:
        """
        Generate a complete EA vs AI comparison for the specified period.

        This is the core analytics function that answers:
        "Is the AI adding value compared to just running the EA blindly?"
        """
        end_date = date.today()
        start_date = end_date - timedelta(days=days)

        snapshot = PerformanceSnapshot(
            period_start=start_date.isoformat(),
            period_end=end_date.isoformat(),
            account_id=account_id
        )

        try:
            # Get all EA signals in the period
            signals = self.db.get_signals_in_period(account_id, start_date, end_date)
            # Get all AI decisions in the period
            decisions = self.db.get_decisions_in_period(account_id, start_date, end_date)
            # Get all actual trades in the period
            trades = self.db.get_trades_in_period(account_id, start_date, end_date)

            # ── EA Baseline Calculation ──
            snapshot.ea_total_signals = len(signals)
            ea_wins = 0
            ea_losses = 0
            ea_pnl = 0.0

            for signal in signals:
                hyp_pnl = signal.get("ea_hypothetical_pnl", 0)
                ea_pnl += hyp_pnl
                if hyp_pnl > 0:
                    ea_wins += 1
                elif hyp_pnl < 0:
                    ea_losses += 1

            snapshot.ea_hypothetical_pnl = ea_pnl
            snapshot.ea_hypothetical_wins = ea_wins
            snapshot.ea_hypothetical_losses = ea_losses
            snapshot.ea_hypothetical_win_rate = (
                ea_wins / len(signals) if signals else 0.0
            )

            # ── JarvAIs Actual Calculation ──
            snapshot.jarvais_total_trades = len(trades)
            j_wins = 0
            j_losses = 0
            j_pnl = 0.0
            win_pnls = []
            loss_pnls = []
            confidence_winners = []
            confidence_losers = []

            for trade in trades:
                pnl = trade.get("pnl_usd", 0)
                j_pnl += pnl
                confidence = trade.get("ai_confidence_at_entry", 0)

                if pnl > 0:
                    j_wins += 1
                    win_pnls.append(pnl)
                    confidence_winners.append(confidence)
                elif pnl < 0:
                    j_losses += 1
                    loss_pnls.append(pnl)
                    confidence_losers.append(confidence)

            snapshot.jarvais_actual_pnl = j_pnl
            snapshot.jarvais_wins = j_wins
            snapshot.jarvais_losses = j_losses
            snapshot.jarvais_win_rate = (
                j_wins / len(trades) if trades else 0.0
            )

            # ── AI Alpha ──
            snapshot.ai_alpha = j_pnl - ea_pnl
            snapshot.ai_alpha_pct = (
                (snapshot.ai_alpha / abs(ea_pnl) * 100) if ea_pnl != 0 else 0.0
            )

            # ── Decision Classification ──
            snapshot.classification = self._classify_decisions(
                signals, decisions, trades
            )

            # ── Confidence Calibration ──
            snapshot.avg_confidence_winners = (
                sum(confidence_winners) / len(confidence_winners)
                if confidence_winners else 0.0
            )
            snapshot.avg_confidence_losers = (
                sum(confidence_losers) / len(confidence_losers)
                if confidence_losers else 0.0
            )
            snapshot.confidence_spread = (
                snapshot.avg_confidence_winners - snapshot.avg_confidence_losers
            )

            # ── Risk Metrics ──
            if win_pnls:
                snapshot.avg_win = sum(win_pnls) / len(win_pnls)
                snapshot.largest_win = max(win_pnls)
            if loss_pnls:
                snapshot.avg_loss = sum(loss_pnls) / len(loss_pnls)
                snapshot.largest_loss = min(loss_pnls)

            total_wins = sum(win_pnls) if win_pnls else 0
            total_losses = abs(sum(loss_pnls)) if loss_pnls else 0
            snapshot.profit_factor = (
                total_wins / total_losses if total_losses > 0 else 99.9
            )

            # Max drawdown
            snapshot.max_drawdown = self._calculate_max_drawdown(trades)

            # ── Learning Metrics ──
            if len(trades) >= 10:
                mid = len(trades) // 2
                first_half = trades[:mid]
                second_half = trades[mid:]

                fh_wins = sum(1 for t in first_half if t.get("pnl_usd", 0) > 0)
                sh_wins = sum(1 for t in second_half if t.get("pnl_usd", 0) > 0)

                snapshot.first_half_win_rate = fh_wins / len(first_half) if first_half else 0
                snapshot.second_half_win_rate = sh_wins / len(second_half) if second_half else 0
                snapshot.learning_improvement = (
                    snapshot.second_half_win_rate - snapshot.first_half_win_rate
                )

        except Exception as e:
            logger.error(f"Error generating EA vs AI comparison: {e}", exc_info=True)

        return snapshot

    def _classify_decisions(self, signals: List[Dict], decisions: List[Dict],
                            trades: List[Dict]) -> TradeClassification:
        """
        Classify every AI decision into one of four categories:
        1. Correct Approval — AI approved, trade won
        2. Mistaken Approval — AI approved, trade lost
        3. Correct Veto — AI vetoed, EA signal would have lost
        4. Missed Opportunity — AI vetoed, EA signal would have won
        """
        tc = TradeClassification()

        # Build lookup maps
        decision_map = {d.get("signal_id"): d for d in decisions}
        trade_map = {}
        for t in trades:
            sid = t.get("signal_id")
            if sid:
                trade_map[sid] = t

        for signal in signals:
            signal_id = signal.get("id")
            decision = decision_map.get(signal_id, {})
            trade = trade_map.get(signal_id)
            ea_hyp_pnl = signal.get("ea_hypothetical_pnl", 0)

            final_decision = decision.get("decision", "Veto")

            if final_decision in ("Approve",):
                # AI approved this signal
                if trade:
                    actual_pnl = trade.get("pnl_usd", 0)
                    if actual_pnl > 0:
                        tc.correct_approvals += 1
                        tc.correct_approval_pnl += actual_pnl
                    else:
                        tc.mistaken_approvals += 1
                        tc.mistaken_approval_pnl += actual_pnl
            else:
                # AI vetoed this signal
                if ea_hyp_pnl < 0:
                    tc.correct_vetoes += 1
                    tc.correct_veto_saved += abs(ea_hyp_pnl)
                elif ea_hyp_pnl > 0:
                    tc.missed_opportunities += 1
                    tc.missed_opportunity_cost += ea_hyp_pnl

        return tc

    def _calculate_max_drawdown(self, trades: List[Dict]) -> float:
        """Calculate maximum drawdown from a series of trades."""
        if not trades:
            return 0.0

        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0

        for trade in trades:
            cumulative += trade.get("pnl_usd", 0)
            if cumulative > peak:
                peak = cumulative
            drawdown = peak - cumulative
            if drawdown > max_dd:
                max_dd = drawdown

        return max_dd

    # ─────────────────────────────────────────────────────────────────
    # Confidence Analysis
    # ─────────────────────────────────────────────────────────────────

    def get_confidence_analysis(self, account_id: str,
                                 days: int = 30) -> Dict[str, Any]:
        """
        Analyze how well-calibrated the AI's confidence scores are.

        Groups trades by confidence buckets (50-60, 60-70, 70-80, 80-90, 90-100)
        and shows the actual win rate for each bucket.

        A well-calibrated AI should show:
        - Higher confidence → higher win rate
        - Monotonically increasing win rates across buckets
        """
        trades = self.db.get_trades_in_period(
            account_id,
            date.today() - timedelta(days=days),
            date.today()
        )

        buckets = {
            "50-60": {"wins": 0, "total": 0, "pnl": 0.0},
            "60-70": {"wins": 0, "total": 0, "pnl": 0.0},
            "70-80": {"wins": 0, "total": 0, "pnl": 0.0},
            "80-90": {"wins": 0, "total": 0, "pnl": 0.0},
            "90-100": {"wins": 0, "total": 0, "pnl": 0.0},
        }

        for trade in trades:
            confidence = trade.get("ai_confidence_at_entry", 0)
            pnl = trade.get("pnl_usd", 0)

            if confidence < 50:
                continue  # Should not happen (below threshold)

            if confidence < 60:
                bucket = "50-60"
            elif confidence < 70:
                bucket = "60-70"
            elif confidence < 80:
                bucket = "70-80"
            elif confidence < 90:
                bucket = "80-90"
            else:
                bucket = "90-100"

            buckets[bucket]["total"] += 1
            buckets[bucket]["pnl"] += pnl
            if pnl > 0:
                buckets[bucket]["wins"] += 1

        # Calculate win rates
        result = {}
        for bucket_name, data in buckets.items():
            result[bucket_name] = {
                "total_trades": data["total"],
                "wins": data["wins"],
                "win_rate": data["wins"] / data["total"] if data["total"] > 0 else 0.0,
                "total_pnl": round(data["pnl"], 2),
                "avg_pnl": round(data["pnl"] / data["total"], 2) if data["total"] > 0 else 0.0
            }

        # Check calibration quality
        win_rates = [result[b]["win_rate"] for b in sorted(result.keys()) if result[b]["total_trades"] > 0]
        is_monotonic = all(win_rates[i] <= win_rates[i+1] for i in range(len(win_rates)-1)) if len(win_rates) > 1 else True

        return {
            "buckets": result,
            "is_well_calibrated": is_monotonic,
            "calibration_note": (
                "Confidence scores are well-calibrated — higher confidence = higher win rate"
                if is_monotonic else
                "Confidence scores need recalibration — win rates are not monotonically increasing"
            )
        }

    # ─────────────────────────────────────────────────────────────────
    # Learning Curve
    # ─────────────────────────────────────────────────────────────────

    def get_learning_curve(self, account_id: str,
                            window_size: int = 20) -> List[Dict[str, Any]]:
        """
        Calculate the learning curve — rolling win rate over time.

        Uses a sliding window to show how the system's performance
        evolves as it accumulates more experience.

        A positive learning curve shows the system is improving.
        """
        trades = self.db.get_all_trades(account_id)

        if len(trades) < window_size:
            return []

        curve = []
        for i in range(window_size, len(trades) + 1):
            window = trades[i - window_size:i]
            wins = sum(1 for t in window if t.get("pnl_usd", 0) > 0)
            total_pnl = sum(t.get("pnl_usd", 0) for t in window)
            avg_confidence = sum(t.get("ai_confidence_at_entry", 0) for t in window) / len(window)

            curve.append({
                "trade_number": i,
                "window_win_rate": wins / window_size,
                "window_pnl": round(total_pnl, 2),
                "avg_confidence": round(avg_confidence, 1),
                "date": window[-1].get("close_time", "")
            })

        return curve

    # ─────────────────────────────────────────────────────────────────
    # Daily Report
    # ─────────────────────────────────────────────────────────────────

    def generate_daily_report(self, account_id: str) -> Dict[str, Any]:
        """
        Generate a comprehensive daily performance report.
        This feeds into the Cognitive Engine's daily review.
        """
        today = date.today()

        try:
            today_trades = self.db.get_today_trades(account_id)
            today_signals = self.db.get_today_signals(account_id)
            today_decisions = self.db.get_today_decisions(account_id)

            # Calculate today's metrics
            total_pnl = sum(t.get("pnl_usd", 0) for t in today_trades)
            wins = sum(1 for t in today_trades if t.get("pnl_usd", 0) > 0)
            losses = sum(1 for t in today_trades if t.get("pnl_usd", 0) < 0)

            # Count vetoes
            total_signals = len(today_signals)
            total_approved = sum(1 for d in today_decisions
                                 if d.get("decision") in ("Approve",))
            total_vetoed = total_signals - total_approved

            # EA hypothetical for today
            ea_hyp_pnl = sum(s.get("ea_hypothetical_pnl", 0) for s in today_signals)

            report = {
                "date": today.isoformat(),
                "account_id": account_id,

                # Signals
                "total_signals_received": total_signals,
                "signals_approved": total_approved,
                "signals_vetoed": total_vetoed,
                "approval_rate": total_approved / total_signals if total_signals > 0 else 0,

                # Trades
                "total_trades": len(today_trades),
                "wins": wins,
                "losses": losses,
                "win_rate": round(100 * wins / len(today_trades), 1) if today_trades else 0,

                # P&L
                "actual_pnl": round(total_pnl, 2),
                "ea_hypothetical_pnl": round(ea_hyp_pnl, 2),
                "ai_alpha_today": round(total_pnl - ea_hyp_pnl, 2),

                # Best/Worst
                "best_trade": max((t.get("pnl_usd", 0) for t in today_trades), default=0),
                "worst_trade": min((t.get("pnl_usd", 0) for t in today_trades), default=0),

                # Confidence
                "avg_confidence": (
                    sum(t.get("ai_confidence_at_entry", 0) for t in today_trades) / len(today_trades)
                    if today_trades else 0
                ),

                # Trade details
                "trades": today_trades
            }

            # Store daily performance in database
            self.db.insert_daily_performance({
                "account_id": account_id,
                "date": today,
                "total_signals": total_signals,
                "signals_approved": total_approved,
                "signals_vetoed": total_vetoed,
                "total_trades": len(today_trades),
                "wins": wins,
                "losses": losses,
                "win_rate": round(100 * wins / len(today_trades), 1) if today_trades else 0,
                "actual_pnl": total_pnl,
                "ea_hypothetical_pnl": ea_hyp_pnl,
                "ai_alpha": total_pnl - ea_hyp_pnl,
                "max_drawdown": self._calculate_max_drawdown(today_trades),
                "avg_confidence": report["avg_confidence"],
                "best_trade_pnl": report["best_trade"],
                "worst_trade_pnl": report["worst_trade"]
            })

            logger.info(f"[{account_id}] Daily report: {len(today_trades)} trades, "
                        f"P&L: ${total_pnl:.2f}, AI Alpha: ${total_pnl - ea_hyp_pnl:.2f}")

            return report

        except Exception as e:
            logger.error(f"Error generating daily report: {e}", exc_info=True)
            return {"error": str(e)}

    # ─────────────────────────────────────────────────────────────────
    # Cross-Account (Hive Mind) Analytics
    # ─────────────────────────────────────────────────────────────────

    def get_hive_mind_report(self, days: int = 7) -> Dict[str, Any]:
        """
        Generate a cross-account performance report.
        Shows how all accounts are performing collectively and identifies
        which account/symbol combinations are most profitable.
        """
        try:
            accounts = self.db.get_all_account_ids()
            account_reports = {}
            total_pnl = 0.0
            total_trades = 0
            total_wins = 0

            for acct_id in accounts:
                snapshot = self.get_ea_vs_ai_comparison(acct_id, days=days)
                account_reports[acct_id] = {
                    "pnl": snapshot.jarvais_actual_pnl,
                    "trades": snapshot.jarvais_total_trades,
                    "win_rate": snapshot.jarvais_win_rate,
                    "ai_alpha": snapshot.ai_alpha,
                    "learning_improvement": snapshot.learning_improvement
                }
                total_pnl += snapshot.jarvais_actual_pnl
                total_trades += snapshot.jarvais_total_trades
                total_wins += snapshot.jarvais_wins

            # Find best and worst performing accounts
            best_account = max(account_reports.items(),
                               key=lambda x: x[1]["pnl"],
                               default=("N/A", {}))
            worst_account = min(account_reports.items(),
                                key=lambda x: x[1]["pnl"],
                                default=("N/A", {}))

            return {
                "period_days": days,
                "total_accounts": len(accounts),
                "total_pnl": round(total_pnl, 2),
                "total_trades": total_trades,
                "overall_win_rate": total_wins / total_trades if total_trades > 0 else 0,
                "best_account": best_account[0],
                "best_account_pnl": best_account[1].get("pnl_usd", 0),
                "worst_account": worst_account[0],
                "worst_account_pnl": worst_account[1].get("pnl_usd", 0),
                "account_details": account_reports
            }

        except Exception as e:
            logger.error(f"Error generating hive mind report: {e}", exc_info=True)
            return {"error": str(e)}

    # ─────────────────────────────────────────────────────────────────
    # API Endpoints Data (for Dashboard)
    # ─────────────────────────────────────────────────────────────────

    def get_dashboard_data(self, account_id: str) -> Dict[str, Any]:
        """
        Get all data needed by the web dashboard in a single call.
        """
        return {
            "today": self.generate_daily_report(account_id),
            "week": self._get_period_summary(account_id, 7),
            "month": self._get_period_summary(account_id, 30),
            "all_time": self._get_period_summary(account_id, 365),
            "confidence_analysis": self.get_confidence_analysis(account_id),
            "learning_curve": self.get_learning_curve(account_id),
            "recent_trades": self.db.get_recent_trades(account_id, limit=20),
            "equity_curve": self._get_equity_curve(account_id)
        }

    def _get_period_summary(self, account_id: str, days: int) -> Dict[str, Any]:
        """Get a summary for a specific period."""
        snapshot = self.get_ea_vs_ai_comparison(account_id, days=days)
        return {
            "pnl": snapshot.jarvais_actual_pnl,
            "ea_pnl": snapshot.ea_hypothetical_pnl,
            "ai_alpha": snapshot.ai_alpha,
            "trades": snapshot.jarvais_total_trades,
            "win_rate": snapshot.jarvais_win_rate,
            "profit_factor": snapshot.profit_factor,
            "max_drawdown": snapshot.max_drawdown,
            "veto_accuracy": snapshot.classification.veto_accuracy,
            "learning_improvement": snapshot.learning_improvement
        }

    def _get_equity_curve(self, account_id: str) -> List[Dict[str, Any]]:
        """Get the equity curve data for charting."""
        try:
            daily_perf = self.db.get_all_daily_performance(account_id)
            cumulative_pnl = 0.0
            cumulative_ea_pnl = 0.0
            curve = []

            for day in daily_perf:
                cumulative_pnl += day.get("total_pnl", 0) or 0
                cumulative_ea_pnl += (day.get("ea_hypothetical_pnl", 0) or 0)
                curve.append({
                    "date": day.get("date", ""),
                    "jarvais_equity": round(cumulative_pnl, 2),
                    "ea_equity": round(cumulative_ea_pnl, 2),
                    "ai_alpha_cumulative": round(cumulative_pnl - cumulative_ea_pnl, 2)
                })

            return curve
        except Exception as e:
            logger.error(f"Error getting equity curve: {e}")
            return []


# ─────────────────────────────────────────────────────────────────────
# Singleton Instance
# ─────────────────────────────────────────────────────────────────────

_performance_engine: Optional[PerformanceEngine] = None


def get_performance_engine() -> PerformanceEngine:
    """Get or create the shared PerformanceEngine instance."""
    global _performance_engine
    if _performance_engine is None:
        _performance_engine = PerformanceEngine()
    return _performance_engine
