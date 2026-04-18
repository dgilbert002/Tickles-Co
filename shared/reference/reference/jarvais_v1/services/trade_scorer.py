"""
JarvAIs Trade Scorer — Three-Axis Scoring System

Scores three independent dimensions after each trade:
1. Model Score — How well does this AI model perform in this role?
2. Prompt Score — How well does this prompt version perform?
3. Role Score — How well is this role performing overall?

Also tracks alpha source scoring:
- Source → Sub-source → Sub-sub-source (3 levels)
- Trader names the alpha source per trade for attribution

Scoring is evidence-based and statistical. No emotional reactions.
"""

import json
import logging
import statistics
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from decimal import Decimal
from core.time_utils import utcnow

logger = logging.getLogger("jarvais.trade_scorer")


class TradeScorer:
    """
    Updates scoring tables after each trade closes.
    Maintains running statistics for prompts, models, and roles.
    """

    def __init__(self, db=None):
        self._db = db

    def _get_db(self):
        if self._db is None:
            from db.database import get_db
            self._db = get_db()
        return self._db

    # ─────────────────────────────────────────────────────────────────
    # Main Entry Point
    # ─────────────────────────────────────────────────────────────────

    def score_trade(self, trade_id: int, account_id: str,
                    alpha_source: str = None,
                    alpha_sub_source: str = None,
                    alpha_sub_sub_source: str = None,
                    alpha_sources: List[Dict] = None) -> Dict[str, Any]:
        """
        Called after a trade closes. Updates all three scoring axes.

        Args:
            trade_id: The closed trade's ID
            account_id: The trading account
            alpha_source: Primary alpha source (e.g., "tradingview_ideas") [legacy]
            alpha_sub_source: Sub-source (e.g., "XAUUSD") [legacy]
            alpha_sub_sub_source: Sub-sub-source (e.g., "TraderJoe123") [legacy]
            alpha_sources: List of alpha source dicts from AI response:
                [{"source": "...", "sub_source": "...", "sub_sub_source": "..."}]

        Returns:
            Dict with updated scores for all three axes
        """
        db = self._get_db()
        result = {
            "trade_id": trade_id,
            "prompt_scores": {},
            "model_scores": {},
            "role_scores": {},
            "alpha_source_scores": []
        }

        try:
            # Get the trade
            trade = db.fetch_one(
                "SELECT * FROM trades WHERE id = %s", (trade_id,)
            )
            if not trade or trade.get("status") != "closed":
                logger.warning(f"Trade {trade_id} not found or not closed")
                return result

            pnl = float(trade.get("pnl_usd") or 0)
            is_win = pnl > 0

            # 1. Update Prompt Scores
            result["prompt_scores"] = self._update_prompt_scores(
                trade, account_id
            )

            # 2. Update Model Scores
            result["model_scores"] = self._update_model_scores(
                trade, account_id
            )

            # 3. Update Role Scores
            result["role_scores"] = self._update_role_scores(
                trade, account_id
            )

            # 4. Update Alpha Source Scores — supports both legacy single and new list format
            scored_sources = []
            if alpha_sources and isinstance(alpha_sources, list):
                for src in alpha_sources:
                    if isinstance(src, dict) and src.get("source"):
                        score = self._update_alpha_source_score(
                            trade,
                            src.get("source", ""),
                            src.get("sub_source"),
                            src.get("sub_sub_source"),
                            account_id
                        )
                        scored_sources.append(score)
            elif alpha_source:
                score = self._update_alpha_source_score(
                    trade, alpha_source, alpha_sub_source,
                    alpha_sub_sub_source, account_id
                )
                scored_sources.append(score)
            result["alpha_source_scores"] = scored_sources

            logger.info(
                f"[TradeScorer] Scored trade {trade_id}: "
                f"PnL=${pnl:.2f}, Win={is_win}"
            )

        except Exception as e:
            logger.error(f"Error scoring trade {trade_id}: {e}")

        return result

    # ─────────────────────────────────────────────────────────────────
    # Axis 1: Prompt Scoring
    # ─────────────────────────────────────────────────────────────────

    def _update_prompt_scores(self, trade: Dict, account_id: str) -> Dict:
        """
        Update prompt_versions scoring columns based on trade outcome.
        Finds which prompt versions were active when the trade was opened.
        """
        db = self._get_db()
        scores = {}

        try:
            open_time = trade.get("open_time")
            pnl = float(trade.get("pnl_usd") or 0)
            is_win = pnl > 0

            # Find active prompt versions at trade open time
            # (the prompts that influenced this trade decision)
            active_prompts = db.fetch_all(
                """SELECT pv.id, pv.role, pv.model, pv.version,
                          pv.winning_trades, pv.losing_trades, pv.win_rate,
                          pv.avg_pnl, pv.sharpe_ratio, pv.max_drawdown, pv.score
                   FROM prompt_versions pv
                   WHERE pv.is_active = 1
                   AND pv.role IN ('analyst', 'trader', 'coach', 'post_mortem')"""
            )

            for pv in active_prompts:
                pv_id = pv["id"]
                role = pv["role"]

                # Get current stats
                wins = int(pv.get("winning_trades") or 0)
                losses = int(pv.get("losing_trades") or 0)
                old_avg = float(pv.get("avg_pnl") or 0)
                total_trades = wins + losses

                # Update stats
                if is_win:
                    wins += 1
                else:
                    losses += 1

                new_total = wins + losses
                new_win_rate = (wins / new_total * 100) if new_total > 0 else 0
                new_total_pnl = (old_avg * total_trades) + pnl
                new_avg_pnl = new_total_pnl / new_total if new_total > 0 else pnl

                # Calculate Sharpe ratio from recent trades
                sharpe = self._calculate_prompt_sharpe(pv_id, pnl)

                # Calculate max drawdown
                max_dd = self._calculate_prompt_max_drawdown(pv_id, pnl)

                # Composite score (weighted: 40% win_rate, 30% avg_pnl normalized, 30% sharpe)
                score = self._calculate_composite_score(new_win_rate, new_avg_pnl, sharpe)

                # Update the prompt version
                db.execute(
                    """UPDATE prompt_versions
                       SET winning_trades = %s, losing_trades = %s,
                           total_trades = %s, total_pnl = %s,
                           win_rate = %s, avg_pnl = %s,
                           sharpe_ratio = %s, max_drawdown = %s, score = %s
                       WHERE id = %s""",
                    (wins, losses, new_total, round(new_total_pnl, 2),
                     round(new_win_rate, 2), round(new_avg_pnl, 2),
                     round(sharpe, 4), round(max_dd, 2), round(score, 2), pv_id)
                )

                scores[role] = {
                    "prompt_version_id": pv_id,
                    "win_rate": round(new_win_rate, 2),
                    "avg_pnl": round(new_avg_pnl, 2),
                    "sharpe": round(sharpe, 4),
                    "score": round(score, 2),
                    "total_trades": new_total
                }

        except Exception as e:
            logger.error(f"Error updating prompt scores: {e}")

        return scores

    # ─────────────────────────────────────────────────────────────────
    # Axis 2: Model Scoring
    # ─────────────────────────────────────────────────────────────────

    def _update_model_scores(self, trade: Dict, account_id: str) -> Dict:
        """
        Update model_performance table based on trade outcome.
        Tracks how each model performs in each role.
        """
        db = self._get_db()
        scores = {}

        try:
            pnl = float(trade.get("pnl_usd") or 0)
            is_win = pnl > 0
            today = utcnow().date()

            # Get active models per role
            active_prompts = db.fetch_all(
                """SELECT role, model, model_provider
                   FROM prompt_versions
                   WHERE is_active = 1
                   AND role IN ('analyst', 'trader', 'coach', 'post_mortem')"""
            )

            for pv in active_prompts:
                role = pv["role"]
                model = pv.get("model") or "unknown"
                provider = pv.get("model_provider") or "unknown"

                # Upsert into model_performance for today
                existing = db.fetch_one(
                    """SELECT id, total_trades, winning_trades, losing_trades,
                              total_pnl, avg_latency_ms, total_cost_usd
                       FROM model_performance
                       WHERE model = %s AND role = %s
                       AND period_start = %s""",
                    (model, role, today)
                )

                if existing:
                    new_total = int(existing["total_trades"] or 0) + 1
                    new_wins = int(existing["winning_trades"] or 0) + (1 if is_win else 0)
                    new_losses = int(existing["losing_trades"] or 0) + (0 if is_win else 1)
                    new_pnl = float(existing["total_pnl"] or 0) + pnl
                    new_wr = (new_wins / new_total * 100) if new_total > 0 else 0

                    db.execute(
                        """UPDATE model_performance
                           SET total_trades = %s, winning_trades = %s,
                               losing_trades = %s, total_pnl = %s,
                               win_rate = %s
                           WHERE id = %s""",
                        (new_total, new_wins, new_losses, round(new_pnl, 2),
                         round(new_wr, 2), existing["id"])
                    )
                else:
                    db.execute(
                        """INSERT INTO model_performance
                           (model, provider, role, period_start,
                            period_end, total_trades, winning_trades, losing_trades,
                            total_pnl, win_rate)
                           VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s)""",
                        (model, provider, role, today, today,
                         1 if is_win else 0, 0 if is_win else 1,
                         round(pnl, 2), 100 if is_win else 0)
                    )

                scores[role] = {
                    "model": model,
                    "provider": provider,
                    "trade_pnl": round(pnl, 2)
                }

        except Exception as e:
            logger.error(f"Error updating model scores: {e}")

        return scores

    # ─────────────────────────────────────────────────────────────────
    # Axis 3: Role Scoring
    # ─────────────────────────────────────────────────────────────────

    def _update_role_scores(self, trade: Dict, account_id: str) -> Dict:
        """
        Update role_score_snapshots with latest performance data.
        """
        db = self._get_db()
        scores = {}

        try:
            today = utcnow().date()

            # Get all closed trades for today
            today_trades = db.fetch_all(
                """SELECT pnl_usd FROM trades
                   WHERE account_id = %s AND DATE(close_time) = %s
                   AND status = 'closed'""",
                (account_id, today)
            )

            pnls = [float(t.get("pnl_usd") or 0) for t in today_trades]
            total_trades = len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            total_pnl = sum(pnls)
            win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

            # Calculate individual role scores based on their contribution
            # For now, all roles share the same trade outcome
            # In future, we can weight based on decision quality
            for role in ['analyst', 'trader', 'coach', 'postmortem']:
                score = self._calculate_role_score(role, pnls)
                scores[role] = {"score": round(score, 2)}

            # Upsert daily snapshot
            existing = db.fetch_one(
                """SELECT id FROM role_score_snapshots
                   WHERE account_id = %s AND date = %s""",
                (account_id, today)
            )

            if existing:
                db.execute(
                    """UPDATE role_score_snapshots
                       SET trader_score = %s, coach_score = %s,
                           analyst_score = %s, postmortem_score = %s,
                           total_trades = %s, total_pnl = %s, win_rate = %s
                       WHERE id = %s""",
                    (scores.get("trader", {}).get("score", 0),
                     scores.get("coach", {}).get("score", 0),
                     scores.get("analyst", {}).get("score", 0),
                     scores.get("postmortem", {}).get("score", 0),
                     total_trades, round(total_pnl, 2), round(win_rate, 2),
                     existing["id"])
                )
            else:
                db.execute(
                    """INSERT INTO role_score_snapshots
                       (account_id, date, trader_score, coach_score,
                        analyst_score, postmortem_score,
                        total_trades, total_pnl, win_rate)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (account_id, today,
                     scores.get("trader", {}).get("score", 0),
                     scores.get("coach", {}).get("score", 0),
                     scores.get("analyst", {}).get("score", 0),
                     scores.get("postmortem", {}).get("score", 0),
                     total_trades, round(total_pnl, 2), round(win_rate, 2))
                )

        except Exception as e:
            logger.error(f"Error updating role scores: {e}")

        return scores

    # ─────────────────────────────────────────────────────────────────
    # Alpha Source Scoring
    # ─────────────────────────────────────────────────────────────────

    def _update_alpha_source_score(self, trade: Dict, source: str,
                                    sub_source: str, sub_sub_source: str,
                                    account_id: str) -> Dict:
        """
        Update alpha_source_scores with trade outcome attribution.
        3-level hierarchy: Source → Sub-source → Sub-sub-source
        """
        db = self._get_db()

        try:
            pnl = float(trade.get("pnl_usd") or 0)
            is_win = pnl > 0

            # Upsert at each level
            for level_source, level_sub, level_subsub in [
                (source, None, None),                    # Level 1: Source only
                (source, sub_source, None),              # Level 2: Source + Sub
                (source, sub_source, sub_sub_source),    # Level 3: Full path
            ]:
                if level_source is None:
                    continue

                existing = db.fetch_one(
                    """SELECT id, total_trades, winning_trades, losing_trades,
                              total_pnl, avg_pnl
                       FROM alpha_source_scores
                       WHERE source_name = %s
                       AND (sub_source = %s OR (sub_source IS NULL AND %s IS NULL))
                       AND (sub_sub_source = %s OR (sub_sub_source IS NULL AND %s IS NULL))
                       AND account_id = %s""",
                    (level_source, level_sub, level_sub,
                     level_subsub, level_subsub, account_id)
                )

                if existing:
                    new_total = int(existing["total_trades"] or 0) + 1
                    new_wins = int(existing["winning_trades"] or 0) + (1 if is_win else 0)
                    new_losses = int(existing["losing_trades"] or 0) + (0 if is_win else 1)
                    new_pnl = float(existing["total_pnl"] or 0) + pnl
                    new_avg = new_pnl / new_total if new_total > 0 else 0
                    new_wr = (new_wins / new_total * 100) if new_total > 0 else 0

                    db.execute(
                        """UPDATE alpha_source_scores
                           SET total_trades = %s, winning_trades = %s,
                               losing_trades = %s, total_pnl = %s,
                               avg_pnl = %s, win_rate = %s,
                               last_trade_at = NOW()
                           WHERE id = %s""",
                        (new_total, new_wins, new_losses,
                         round(new_pnl, 2), round(new_avg, 2),
                         round(new_wr, 2), existing["id"])
                    )
                else:
                    db.execute(
                        """INSERT INTO alpha_source_scores
                           (account_id, source_name, sub_source, sub_sub_source,
                            total_trades, winning_trades, losing_trades,
                            total_pnl, avg_pnl, win_rate, last_trade_at)
                           VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s, %s, NOW())""",
                        (account_id, level_source, level_sub, level_subsub,
                         1 if is_win else 0, 0 if is_win else 1,
                         round(pnl, 2), round(pnl, 2),
                         100 if is_win else 0)
                    )

            return {
                "source": source,
                "sub_source": sub_source,
                "sub_sub_source": sub_sub_source,
                "pnl": round(pnl, 2),
                "win": is_win
            }

        except Exception as e:
            logger.error(f"Error updating alpha source score: {e}")
            return {}

    # ─────────────────────────────────────────────────────────────────
    # Calculation Helpers
    # ─────────────────────────────────────────────────────────────────

    def _calculate_prompt_sharpe(self, prompt_version_id: int, latest_pnl: float) -> float:
        """Calculate Sharpe ratio for a prompt version based on its trade history."""
        db = self._get_db()
        try:
            # Get PnLs from trades that used this prompt version
            # For now, use all recent trades since prompt was activated
            pv = db.fetch_one(
                "SELECT activated_at FROM prompt_versions WHERE id = %s",
                (prompt_version_id,)
            )
            if not pv or not pv.get("activated_at"):
                return 0

            trades = db.fetch_all(
                """SELECT pnl_usd FROM trades
                   WHERE status = 'closed' AND close_time >= %s
                   ORDER BY close_time""",
                (pv["activated_at"],)
            )

            pnls = [float(t.get("pnl_usd") or 0) for t in trades]
            pnls.append(latest_pnl)

            if len(pnls) < 2:
                return 0

            avg = statistics.mean(pnls)
            std = statistics.stdev(pnls)
            return (avg / std) if std > 0 else 0

        except Exception as e:
            logger.debug(f"Sharpe calculation error: {e}")
            return 0

    def _calculate_prompt_max_drawdown(self, prompt_version_id: int,
                                        latest_pnl: float) -> float:
        """Calculate max drawdown for a prompt version."""
        db = self._get_db()
        try:
            pv = db.fetch_one(
                "SELECT activated_at FROM prompt_versions WHERE id = %s",
                (prompt_version_id,)
            )
            if not pv or not pv.get("activated_at"):
                return 0

            trades = db.fetch_all(
                """SELECT pnl_usd FROM trades
                   WHERE status = 'closed' AND close_time >= %s
                   ORDER BY close_time""",
                (pv["activated_at"],)
            )

            pnls = [float(t.get("pnl_usd") or 0) for t in trades]
            pnls.append(latest_pnl)

            if not pnls:
                return 0

            # Calculate cumulative equity curve
            cumulative = 0
            peak = 0
            max_dd = 0
            for p in pnls:
                cumulative += p
                if cumulative > peak:
                    peak = cumulative
                dd = peak - cumulative
                if dd > max_dd:
                    max_dd = dd

            return max_dd

        except Exception as e:
            logger.debug(f"Max drawdown calculation error: {e}")
            return 0

    def _calculate_composite_score(self, win_rate: float, avg_pnl: float,
                                    sharpe: float) -> float:
        """
        Calculate composite score from multiple metrics.
        Score range: 0-100

        Weights:
        - Win rate: 40% (normalized to 0-100)
        - Avg PnL: 30% (normalized using sigmoid)
        - Sharpe ratio: 30% (normalized to 0-100)
        """
        import math

        # Win rate component (already 0-100)
        wr_score = min(max(win_rate, 0), 100)

        # Avg PnL component (sigmoid normalization)
        # Maps any PnL to 0-100 range, with 50 at PnL=0
        pnl_score = 100 / (1 + math.exp(-avg_pnl / 50))

        # Sharpe component (normalize: 0=0, 1=50, 2=75, 3+=90+)
        sharpe_score = min(100, max(0, 50 * (1 + math.tanh(sharpe - 1))))

        composite = (wr_score * 0.4) + (pnl_score * 0.3) + (sharpe_score * 0.3)
        return round(composite, 2)

    def _calculate_role_score(self, role: str, pnls: List[float]) -> float:
        """
        Calculate a role's daily score based on trade outcomes.
        Different roles have different scoring emphasis.
        """
        if not pnls:
            return 50.0  # Neutral score when no data

        total = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / total if total > 0 else 0
        avg_pnl = sum(pnls) / total if total > 0 else 0

        if role == "analyst":
            # Analyst scored on data quality → reflected in win rate
            return min(100, max(0, win_rate * 100))
        elif role == "trader":
            # Trader scored on P&L and risk management
            import math
            pnl_component = 100 / (1 + math.exp(-avg_pnl / 30))
            return min(100, max(0, (win_rate * 50) + (pnl_component * 0.5)))
        elif role == "coach":
            # Coach scored on veto quality (higher win rate = good coaching)
            return min(100, max(0, win_rate * 110))  # Slight boost for high win rates
        elif role == "postmortem":
            # Post-mortem scored on learning (improving trend)
            if len(pnls) >= 3:
                recent = pnls[-3:]
                improving = all(recent[i] >= recent[i-1] for i in range(1, len(recent)))
                return 75.0 if improving else 50.0
            return 50.0

        return 50.0  # Default neutral

    # ─────────────────────────────────────────────────────────────────
    # Reporting
    # ─────────────────────────────────────────────────────────────────

    def get_prompt_leaderboard(self, role_name: str = None) -> List[Dict]:
        """Get prompt versions ranked by score."""
        db = self._get_db()
        sql = """
            SELECT id, role, version, prompt_name, model, model_provider,
                   winning_trades, losing_trades, win_rate, avg_pnl,
                   sharpe_ratio, max_drawdown, score, is_active,
                   (winning_trades + losing_trades) as total_trades
            FROM prompt_versions
            WHERE (winning_trades + losing_trades) > 0
        """
        params = []
        if role_name:
            sql += " AND role = %s"
            params.append(role_name)
        sql += " ORDER BY score DESC"

        return db.fetch_all(sql, tuple(params)) if params else db.fetch_all(sql)

    def get_model_leaderboard(self, role_name: str = None) -> List[Dict]:
        """Get models ranked by performance."""
        db = self._get_db()
        sql = """
            SELECT model, provider, role,
                   SUM(total_trades) as total_trades,
                   SUM(winning_trades) as total_wins,
                   SUM(total_pnl) as total_pnl,
                   AVG(win_rate) as avg_win_rate
            FROM model_performance
            WHERE total_trades > 0
        """
        params = []
        if role_name:
            sql += " AND role = %s"
            params.append(role_name)
        sql += " GROUP BY model, provider, role ORDER BY total_pnl DESC"

        return db.fetch_all(sql, tuple(params)) if params else db.fetch_all(sql)

    def get_alpha_source_leaderboard(self, account_id: str) -> List[Dict]:
        """Get alpha sources ranked by profitability."""
        db = self._get_db()
        return db.fetch_all(
            """SELECT source_name, sub_source, sub_sub_source,
                      total_trades, winning_trades, losing_trades,
                      total_pnl, avg_pnl, win_rate
               FROM alpha_source_scores
               WHERE account_id = %s AND total_trades >= 3
               ORDER BY avg_pnl DESC""",
            (account_id,)
        )

    def repair_prompt_stats(self) -> Dict[str, int]:
        """
        One-time repair: recalculate total_trades and total_pnl for all
        prompt_versions rows.  These columns were never written by
        _update_prompt_scores() prior to the Wave 1 fix.

        Derivation:
          total_trades = winning_trades + losing_trades
          total_pnl    = avg_pnl * total_trades   (when total_trades > 0)

        Returns dict with counts: {repaired, skipped, errors}.
        """
        db = self._get_db()
        result = {"repaired": 0, "skipped": 0, "errors": 0}

        rows = db.fetch_all(
            "SELECT id, winning_trades, losing_trades, avg_pnl, "
            "total_trades, total_pnl FROM prompt_versions"
        )

        for row in rows:
            pv_id = row["id"]
            wins = int(row.get("winning_trades") or 0)
            losses = int(row.get("losing_trades") or 0)
            old_total = int(row.get("total_trades") or 0)
            old_pnl = float(row.get("total_pnl") or 0)
            avg_pnl = float(row.get("avg_pnl") or 0)

            correct_total = wins + losses
            correct_pnl = avg_pnl * correct_total if correct_total > 0 else 0.0

            if old_total == correct_total and abs(old_pnl - correct_pnl) < 0.01:
                result["skipped"] += 1
                continue

            try:
                db.execute(
                    "UPDATE prompt_versions SET total_trades = %s, total_pnl = %s "
                    "WHERE id = %s",
                    (correct_total, round(correct_pnl, 2), pv_id)
                )
                result["repaired"] += 1
            except Exception as e:
                logger.error(f"repair_prompt_stats id={pv_id}: {e}")
                result["errors"] += 1

        logger.info(
            f"[TradeScorer] repair_prompt_stats complete: "
            f"{result['repaired']} repaired, {result['skipped']} skipped, "
            f"{result['errors']} errors"
        )
        return result
