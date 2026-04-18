"""
JarvAIs Proposal Engine — Inter-Role Intelligence Loop

This service enables roles to automatically detect patterns in trade outcomes
and propose prompt/model changes to each other. It embodies the company culture:
- Evidence-based proposals only
- Polite, respectful, constructive
- Compliment successes as well as identify failures
- NEVER at the detriment of trade quality and profit

The engine runs after each trade closes (triggered by Post-Mortem) and also
on a daily schedule (triggered by CEO).

WHO CAN PROPOSE WHAT:
┌──────────────┬─────────────────────────────────────────────────────────┐
│ Role         │ Can Propose Changes To                                  │
├──────────────┼─────────────────────────────────────────────────────────┤
│ Post-Mortem  │ Trader prompt, Coach prompt, Analyst prompt             │
│ Coach        │ Trader prompt, Analyst prompt                           │
│ Analyst      │ Own prompt (based on data quality feedback)             │
│ Trader       │ Own prompt + Coach feedback on Trader's prompt          │
│ CEO          │ All prompts (based on company-wide metrics)             │
└──────────────┴─────────────────────────────────────────────────────────┘
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from core.time_utils import utcnow

logger = logging.getLogger("jarvais.proposal_engine")


from core.config_loader import load_prompt


class ProposalEngine:
    """
    Analyzes trade outcomes and role performance to generate
    evidence-based prompt/model change proposals.
    """

    # Minimum trades before generating proposals (avoid knee-jerk reactions)
    MIN_TRADES_FOR_ANALYSIS = 5
    # Minimum consecutive losses before flagging a pattern
    MIN_CONSECUTIVE_LOSSES = 3
    # Win rate threshold below which a proposal is warranted
    WIN_RATE_THRESHOLD = 0.50
    # Minimum improvement expected to justify a change
    MIN_EXPECTED_IMPROVEMENT = 0.05  # 5%

    def __init__(self, db=None, model_interface=None, memory_manager=None):
        """
        Initialize the ProposalEngine.

        Args:
            db: Database instance for querying trades and storing proposals
            model_interface: ModelInterface for AI-generated proposal text
            memory_manager: MemoryManager for storing proposal memories
        """
        self._db = db
        self._model = model_interface
        self._memory = memory_manager

    def _get_db(self):
        if self._db is None:
            from db.database import get_db
            self._db = get_db()
        return self._db

    def _get_model(self):
        if self._model is None:
            from core.model_interface import get_model_interface
            self._model = get_model_interface()
        return self._model

    def _get_memory(self):
        if self._memory is None:
            from core.memory_manager import MemoryManager
            self._memory = MemoryManager()
        return self._memory

    # ─────────────────────────────────────────────────────────────────
    # Main Entry Points
    # ─────────────────────────────────────────────────────────────────

    def analyze_after_trade_close(self, trade_id: int, account_id: str) -> List[Dict]:
        """
        Called after a trade closes. Runs Post-Mortem analysis and
        checks if any proposals should be generated.

        Returns list of proposals created (may be empty).
        """
        proposals = []
        try:
            db = self._get_db()

            # Get the closed trade
            trade = db.fetch_one(
                "SELECT * FROM trades WHERE id = %s", (trade_id,)
            )
            if not trade:
                logger.warning(f"Trade {trade_id} not found for proposal analysis")
                return proposals

            # Get recent trade history for context
            recent_trades = db.fetch_all(
                """SELECT * FROM trades
                   WHERE account_id = %s AND status = 'closed'
                   ORDER BY close_time DESC LIMIT 20""",
                (account_id,)
            )

            if len(recent_trades) < self.MIN_TRADES_FOR_ANALYSIS:
                logger.info(
                    f"Only {len(recent_trades)} closed trades — need {self.MIN_TRADES_FOR_ANALYSIS} "
                    f"before generating proposals. Patience is a virtue."
                )
                return proposals

            # Run analysis from each role's perspective
            proposals.extend(self._postmortem_analysis(trade, recent_trades, account_id))
            proposals.extend(self._coach_analysis(trade, recent_trades, account_id))

            # Store proposal memories
            for prop in proposals:
                self._store_proposal_memory(prop)

        except Exception as e:
            logger.error(f"Error in post-trade proposal analysis: {e}")

        return proposals

    def daily_ceo_review(self, account_id: str) -> List[Dict]:
        """
        Called daily by the CEO role. Reviews overall company performance
        and generates strategic proposals.

        Returns list of proposals created (may be empty).
        """
        proposals = []
        try:
            db = self._get_db()

            # Get today's trades
            today = utcnow().date()
            today_trades = db.fetch_all(
                """SELECT * FROM trades
                   WHERE account_id = %s AND DATE(close_time) = %s AND status = 'closed'
                   ORDER BY close_time""",
                (account_id, today)
            )

            # Get 7-day trades for trend analysis
            week_ago = today - timedelta(days=7)
            week_trades = db.fetch_all(
                """SELECT * FROM trades
                   WHERE account_id = %s AND close_time >= %s AND status = 'closed'
                   ORDER BY close_time""",
                (account_id, week_ago)
            )

            if len(week_trades) < self.MIN_TRADES_FOR_ANALYSIS:
                logger.info(
                    f"Only {len(week_trades)} trades this week — CEO review deferred. "
                    f"Need {self.MIN_TRADES_FOR_ANALYSIS} for meaningful analysis."
                )
                return proposals

            # CEO can propose changes to any role
            proposals.extend(self._ceo_strategic_analysis(
                today_trades, week_trades, account_id
            ))

            for prop in proposals:
                self._store_proposal_memory(prop)

        except Exception as e:
            logger.error(f"Error in CEO daily review: {e}")

        return proposals

    def daily_pattern_scan(self) -> List[Dict]:
        """
        Called by the orchestrator daily. Scans all accounts for patterns
        and generates proposals. This is the autonomous daily intelligence scan.
        """
        all_proposals = []
        try:
            db = self._get_db()

            # Get all active accounts
            accounts = db.execute_query(
                "SELECT DISTINCT account_id FROM trades WHERE status = 'closed'"
            ) or []

            if not accounts:
                # No trades yet — check alpha source quality instead
                logger.info("No closed trades found. Checking alpha source quality...")
                return all_proposals

            for acc in accounts:
                account_id = acc.get('account_id', '')
                if not account_id:
                    continue
                try:
                    proposals = self.daily_ceo_review(account_id)
                    all_proposals.extend(proposals)
                except Exception as e:
                    logger.error(f"Daily scan for account {account_id} failed: {e}")

        except Exception as e:
            logger.error(f"Daily pattern scan error: {e}")

        return all_proposals

    # ─────────────────────────────────────────────────────────────────
    # Role-Specific Analysis Methods
    # ─────────────────────────────────────────────────────────────────

    def _postmortem_analysis(self, trade: Dict, recent_trades: List[Dict],
                             account_id: str) -> List[Dict]:
        """
        Post-Mortem Analyst perspective: analyze trade outcomes and
        propose changes to Trader, Coach, or Analyst prompts.
        """
        proposals = []
        db = self._get_db()

        # Calculate recent performance metrics
        stats = self._calculate_stats(recent_trades)

        # Check for concerning patterns
        patterns = self._detect_patterns(recent_trades)

        # Only propose if there's a real problem with evidence
        if not patterns and stats["win_rate"] >= self.WIN_RATE_THRESHOLD:
            # Things are going well — log a compliment instead
            if stats["win_rate"] >= 0.65:
                self._store_compliment(
                    "post_mortem",
                    f"Excellent performance: {stats['win_rate']:.0%} win rate over "
                    f"{stats['total_trades']} trades with ${stats['total_pnl']:.2f} P&L. "
                    f"The team is working well together.",
                    stats
                )
            return proposals

        # Generate AI-powered proposal if patterns detected
        if patterns:
            proposal = self._generate_ai_proposal(
                proposing_role="post_mortem",
                patterns=patterns,
                stats=stats,
                recent_trades=recent_trades[-10:],  # Last 10 for context
                account_id=account_id
            )
            if proposal:
                proposals.append(proposal)

        return proposals

    def _coach_analysis(self, trade: Dict, recent_trades: List[Dict],
                        account_id: str) -> List[Dict]:
        """
        Coach perspective: analyze veto patterns and coaching effectiveness.
        Can propose changes to Trader and Analyst prompts.
        """
        proposals = []
        db = self._get_db()

        # Get recent AI decisions to analyze veto patterns
        decisions = db.fetch_all(
            """SELECT * FROM ai_decisions
               WHERE account_id = %s
               ORDER BY created_at DESC LIMIT 20""",
            (account_id,)
        )

        if len(decisions) < self.MIN_TRADES_FOR_ANALYSIS:
            return proposals

        # Analyze veto effectiveness
        veto_stats = self._analyze_veto_effectiveness(decisions, recent_trades)

        if veto_stats and veto_stats.get("needs_attention"):
            proposal = self._generate_coach_proposal(
                veto_stats=veto_stats,
                recent_trades=recent_trades[-10:],
                account_id=account_id
            )
            if proposal:
                proposals.append(proposal)

        return proposals

    def _ceo_strategic_analysis(self, today_trades: List[Dict],
                                 week_trades: List[Dict],
                                 account_id: str) -> List[Dict]:
        """
        CEO perspective: company-wide strategic analysis.
        Can propose changes to any role.
        """
        proposals = []

        today_stats = self._calculate_stats(today_trades) if today_trades else None
        week_stats = self._calculate_stats(week_trades)

        # Check if the company is underperforming
        if week_stats["win_rate"] < self.WIN_RATE_THRESHOLD:
            proposal = self._generate_ceo_proposal(
                today_stats=today_stats,
                week_stats=week_stats,
                week_trades=week_trades,
                account_id=account_id
            )
            if proposal:
                proposals.append(proposal)

        # Check for model performance issues
        model_issues = self._check_model_performance(account_id)
        if model_issues:
            for issue in model_issues:
                proposals.append(issue)

        return proposals

    # ─────────────────────────────────────────────────────────────────
    # Pattern Detection
    # ─────────────────────────────────────────────────────────────────

    def _detect_patterns(self, trades: List[Dict]) -> List[Dict]:
        """
        Detect concerning patterns in recent trades.
        Returns list of pattern descriptions with evidence.
        """
        patterns = []

        if not trades:
            return patterns

        # Pattern 1: Consecutive losses
        consecutive_losses = 0
        max_consecutive = 0
        loss_streak_trades = []
        for t in sorted(trades, key=lambda x: x.get("close_time") or datetime.min):
            pnl = float(t.get("pnl_usd") or 0)
            if pnl < 0:
                consecutive_losses += 1
                loss_streak_trades.append(t.get("id"))
                max_consecutive = max(max_consecutive, consecutive_losses)
            else:
                consecutive_losses = 0
                loss_streak_trades = []

        if max_consecutive >= self.MIN_CONSECUTIVE_LOSSES:
            patterns.append({
                "type": "consecutive_losses",
                "severity": "high" if max_consecutive >= 5 else "medium",
                "description": f"{max_consecutive} consecutive losing trades detected",
                "trade_ids": loss_streak_trades[-max_consecutive:],
                "evidence": f"The last {max_consecutive} trades were all losses, "
                           f"suggesting a systematic issue rather than normal variance."
            })

        # Pattern 2: Same symbol repeated losses
        symbol_losses = {}
        for t in trades:
            pnl = float(t.get("pnl_usd") or 0)
            sym = t.get("symbol", "UNKNOWN")
            if pnl < 0:
                if sym not in symbol_losses:
                    symbol_losses[sym] = {"count": 0, "total_loss": 0, "trade_ids": []}
                symbol_losses[sym]["count"] += 1
                symbol_losses[sym]["total_loss"] += pnl
                symbol_losses[sym]["trade_ids"].append(t.get("id"))

        for sym, data in symbol_losses.items():
            if data["count"] >= 3:
                patterns.append({
                    "type": "symbol_losses",
                    "severity": "medium",
                    "description": f"{data['count']} losses on {sym} (${data['total_loss']:.2f})",
                    "trade_ids": data["trade_ids"],
                    "evidence": f"{sym} has been consistently unprofitable with "
                               f"{data['count']} losses totaling ${data['total_loss']:.2f}. "
                               f"Consider whether the analyst's data for this symbol is adequate."
                })

        # Pattern 3: Low confidence trades losing
        low_conf_losses = [
            t for t in trades
            if (t.get("ai_confidence_at_entry") or 100) < 60
            and (float(t.get("pnl_usd") or 0)) < 0
        ]
        if len(low_conf_losses) >= 2:
            patterns.append({
                "type": "low_confidence_losses",
                "severity": "medium",
                "description": f"{len(low_conf_losses)} low-confidence trades resulted in losses",
                "trade_ids": [t.get("id") for t in low_conf_losses],
                "evidence": "Trades taken with low AI confidence are losing. "
                           "The Coach may need to be more assertive in vetoing "
                           "low-confidence entries."
            })

        # Pattern 4: Stop losses hit too quickly (bad entry timing)
        quick_sl_hits = [
            t for t in trades
            if t.get("close_reason") == "stop_loss"
            and t.get("open_time") and t.get("close_time")
            and (t["close_time"] - t["open_time"]).total_seconds() < 300  # < 5 min
        ]
        if len(quick_sl_hits) >= 2:
            patterns.append({
                "type": "quick_stop_loss",
                "severity": "high",
                "description": f"{len(quick_sl_hits)} trades hit stop loss within 5 minutes",
                "trade_ids": [t.get("id") for t in quick_sl_hits],
                "evidence": "Multiple trades are hitting stop loss almost immediately after entry. "
                           "This suggests poor entry timing. The Analyst's dossier may need "
                           "better short-term momentum analysis."
            })

        return patterns

    def _analyze_veto_effectiveness(self, decisions: List[Dict],
                                     trades: List[Dict]) -> Optional[Dict]:
        """
        Analyze how effective the Coach's vetoes have been.
        """
        if not decisions:
            return None

        total_vetoes = 0
        total_approvals = 0
        approved_losses = 0
        approved_wins = 0

        trade_map = {t.get("id"): t for t in trades}

        for d in decisions:
            coach_decision = d.get("coach_decision", "")
            if "veto" in str(coach_decision).lower():
                total_vetoes += 1
            elif "approve" in str(coach_decision).lower():
                total_approvals += 1
                # Check if the approved trade was profitable
                trade_id = d.get("trade_id")
                if trade_id and trade_id in trade_map:
                    pnl = float(trade_map[trade_id].get("pnl_usd") or 0)
                    if pnl > 0:
                        approved_wins += 1
                    else:
                        approved_losses += 1

        result = {
            "total_vetoes": total_vetoes,
            "total_approvals": total_approvals,
            "approved_wins": approved_wins,
            "approved_losses": approved_losses,
            "needs_attention": False
        }

        # Coach should be vetoing more if approved trades keep losing
        if total_approvals >= 5 and approved_losses > approved_wins:
            result["needs_attention"] = True
            result["issue"] = (
                f"Coach approved {total_approvals} trades but {approved_losses} lost "
                f"vs {approved_wins} won. The Coach may need to be more selective."
            )

        # Coach may be too aggressive if vetoing too many
        if total_vetoes > 0 and total_approvals > 0:
            veto_rate = total_vetoes / (total_vetoes + total_approvals)
            if veto_rate > 0.6:
                result["needs_attention"] = True
                result["issue"] = (
                    f"Coach vetoed {veto_rate:.0%} of trades. This is unusually high. "
                    f"Either the Trader needs better signals, or the Coach is too conservative."
                )

        return result

    # ─────────────────────────────────────────────────────────────────
    # AI-Powered Proposal Generation
    # ─────────────────────────────────────────────────────────────────

    def _generate_ai_proposal(self, proposing_role: str, patterns: List[Dict],
                               stats: Dict, recent_trades: List[Dict],
                               account_id: str) -> Optional[Dict]:
        """
        Use AI to generate a thoughtful, evidence-based proposal.
        """
        try:
            model = self._get_model()
            db = self._get_db()

            # Determine target role based on patterns
            target_role = self._determine_target_role(patterns)

            # Get current prompt for the target role
            current_prompt = db.fetch_one(
                """SELECT id, role, system_prompt, model, model_provider, version
                   FROM prompt_versions
                   WHERE role = %s AND is_active = 1
                   ORDER BY version DESC LIMIT 1""",
                (target_role,)
            )

            # Build the analysis prompt
            trade_summary = self._format_trades_for_ai(recent_trades)
            pattern_summary = "\n".join([
                f"- [{p['severity'].upper()}] {p['description']}: {p['evidence']}"
                for p in patterns
            ])

            prompt = f"""You are the {proposing_role.replace('_', ' ').title()} at JarvAIs, 
a professional AI trading company. You've identified concerning patterns in recent trades 
and need to propose constructive changes.

COMPANY VALUES: Be polite, respectful, evidence-based. Give solutions, not just problems.
Compliment what's working well. Be pragmatic — not every loss needs a change.

RECENT PERFORMANCE:
- Win Rate: {stats['win_rate']:.1%}
- Total P&L: ${stats['total_pnl']:.2f}
- Total Trades: {stats['total_trades']}
- Average P&L per trade: ${stats['avg_pnl']:.2f}

PATTERNS DETECTED:
{pattern_summary}

RECENT TRADES:
{trade_summary}

CURRENT {target_role.upper()} PROMPT (excerpt):
{(current_prompt['system_prompt'] or '')[:500] if current_prompt else 'No active prompt found'}

Based on this evidence, write a constructive proposal to improve the {target_role}'s prompt.
Include:
1. What's working well (compliment first)
2. What specific issue you've identified (with evidence)
3. What specific change you recommend to the prompt
4. Why you believe this will improve performance
5. The actual proposed prompt modification (be specific)

Keep it professional, respectful, and focused on making the company more profitable.
Format as JSON with keys: compliment, issue, recommendation, expected_improvement, proposed_prompt_changes"""

            response = model.query(
                role="analyst",
                system_prompt=load_prompt(
                    self._get_db(), "proposal_analyst_identity",
                    "You are a trading systems analyst at JarvAIs. Generate evidence-based proposals in JSON format.",
                    min_length=10),
                user_prompt=prompt,
                context="proposal_engine_generate",
                source="system",
                source_detail=f"proposal_{proposing_role}",
                media_type="text",
            )

            if not response or not response.success:
                return None

            # Parse the AI response (response.content is the string)
            response_text = response.content or ""
            try:
                json_start = response_text.find("{")
                json_end = response_text.rfind("}") + 1
                if json_start >= 0 and json_end > json_start:
                    ai_proposal = json.loads(response_text[json_start:json_end])
                else:
                    ai_proposal = {"recommendation": response_text}
            except json.JSONDecodeError:
                ai_proposal = {"recommendation": response_text}

            # Build the proposal record
            proposal = {
                "proposing_role": proposing_role,
                "proposing_model": response.model or "unknown",
                "target_role": target_role,
                "change_type": "prompt",
                "current_prompt_id": current_prompt["id"] if current_prompt else None,
                "current_model": current_prompt.get("model") if current_prompt else None,
                "proposed_prompt": ai_proposal.get("proposed_prompt_changes", ""),
                "reason": f"{ai_proposal.get('compliment', '')} {ai_proposal.get('issue', '')}".strip(),
                "evidence": json.dumps({
                    "patterns": patterns,
                    "stats": stats,
                    "ai_analysis": ai_proposal
                }, default=str),
                "expected_improvement": ai_proposal.get("expected_improvement", ""),
                "supporting_trades": json.dumps([t.get("id") for t in recent_trades if t.get("id")]),
                "supporting_stats": json.dumps(stats, default=str),
            }

            # Store in database
            proposal_id = self._store_proposal(proposal)
            proposal["id"] = proposal_id

            logger.info(
                f"[ProposalEngine] {proposing_role} proposed {proposal['change_type']} "
                f"change to {target_role}: {proposal['reason'][:100]}..."
            )

            return proposal

        except Exception as e:
            logger.error(f"Error generating AI proposal: {e}")
            return None

    def _generate_coach_proposal(self, veto_stats: Dict,
                                  recent_trades: List[Dict],
                                  account_id: str) -> Optional[Dict]:
        """Generate a proposal from the Coach's perspective about veto effectiveness."""
        try:
            model = self._get_model()
            db = self._get_db()

            prompt = f"""You are the Coach at JarvAIs, a professional AI trading company.
You've been reviewing your veto/approval patterns and found an issue.

VETO STATISTICS:
{json.dumps(veto_stats, indent=2)}

Your job is to propose a constructive change. Be polite and respectful.
Start with what's working, then address the issue.

Format as JSON with keys: compliment, issue, target_role, recommendation, expected_improvement"""

            response = model.query(
                role="analyst",
                system_prompt=load_prompt(
                    self._get_db(), "proposal_coach_identity",
                    "You are the Coach at JarvAIs. Generate constructive proposals in JSON format.",
                    min_length=10),
                user_prompt=prompt,
                context="proposal_engine_coach",
                source="system",
                source_detail="proposal_coach",
                media_type="text",
            )
            if not response or not response.success:
                return None

            response_text = response.content or ""
            try:
                json_start = response_text.find("{")
                json_end = response_text.rfind("}") + 1
                if json_start >= 0 and json_end > json_start:
                    ai_proposal = json.loads(response_text[json_start:json_end])
                else:
                    ai_proposal = {"recommendation": response_text, "target_role": "trader"}
            except json.JSONDecodeError:
                ai_proposal = {"recommendation": response_text, "target_role": "trader"}

            target = ai_proposal.get("target_role", "trader")

            proposal = {
                "proposing_role": "coach",
                "target_role": target,
                "change_type": "prompt",
                "reason": f"{ai_proposal.get('compliment', '')} {ai_proposal.get('issue', '')}".strip(),
                "evidence": json.dumps({"veto_stats": veto_stats, "ai_analysis": ai_proposal}, default=str),
                "expected_improvement": ai_proposal.get("expected_improvement", ""),
                "supporting_trades": json.dumps([t.get("id") for t in recent_trades if t.get("id")]),
                "supporting_stats": json.dumps(veto_stats, default=str),
            }

            proposal_id = self._store_proposal(proposal)
            proposal["id"] = proposal_id
            return proposal

        except Exception as e:
            logger.error(f"Error generating coach proposal: {e}")
            return None

    def _generate_ceo_proposal(self, today_stats: Optional[Dict],
                                week_stats: Dict, week_trades: List[Dict],
                                account_id: str) -> Optional[Dict]:
        """Generate a strategic proposal from the CEO's perspective."""
        try:
            model = self._get_model()

            prompt = f"""You are the CEO of JarvAIs, a professional AI trading company.
You're reviewing the company's weekly performance and need to make strategic recommendations.

WEEKLY PERFORMANCE:
- Win Rate: {week_stats['win_rate']:.1%}
- Total P&L: ${week_stats['total_pnl']:.2f}
- Total Trades: {week_stats['total_trades']}
- Average P&L: ${week_stats['avg_pnl']:.2f}

{"TODAY'S PERFORMANCE:" if today_stats else "No trades today."}
{f"- Win Rate: {today_stats['win_rate']:.1%}, P&L: ${today_stats['total_pnl']:.2f}" if today_stats else ""}

As CEO, identify the most impactful change that could improve profitability.
Be strategic, not reactive. Consider whether the issue is with:
- The Analyst (data quality, dossier completeness)
- The Trader (decision making, risk management)
- The Coach (veto effectiveness, challenge quality)
- The models being used (capability limitations)

Format as JSON with keys: compliment, strategic_issue, target_role, recommendation, expected_improvement, priority"""

            response = model.query(
                role="ceo",
                system_prompt=load_prompt(
                    self._get_db(), "proposal_ceo_identity",
                    "You are the CEO of JarvAIs. Generate strategic proposals in JSON format.",
                    min_length=10),
                user_prompt=prompt,
                context="proposal_engine_ceo",
                source="system",
                source_detail="proposal_ceo",
                media_type="text",
            )
            if not response or not response.success:
                return None

            response_text = response.content or ""
            try:
                json_start = response_text.find("{")
                json_end = response_text.rfind("}") + 1
                if json_start >= 0 and json_end > json_start:
                    ai_proposal = json.loads(response_text[json_start:json_end])
                else:
                    ai_proposal = {"recommendation": response_text, "target_role": "trader"}
            except json.JSONDecodeError:
                ai_proposal = {"recommendation": response_text, "target_role": "trader"}

            target = ai_proposal.get("target_role", "trader")

            proposal = {
                "proposing_role": "ceo",
                "target_role": target,
                "change_type": "prompt",
                "reason": f"[CEO Strategic Review] {ai_proposal.get('strategic_issue', '')}",
                "evidence": json.dumps({
                    "week_stats": week_stats,
                    "today_stats": today_stats,
                    "ai_analysis": ai_proposal
                }, default=str),
                "expected_improvement": ai_proposal.get("expected_improvement", ""),
                "supporting_trades": json.dumps([t.get("id") for t in week_trades if t.get("id")]),
                "supporting_stats": json.dumps(week_stats, default=str),
            }

            proposal_id = self._store_proposal(proposal)
            proposal["id"] = proposal_id
            return proposal

        except Exception as e:
            logger.error(f"Error generating CEO proposal: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────
    # Helper Methods
    # ─────────────────────────────────────────────────────────────────

    def _calculate_stats(self, trades: List[Dict]) -> Dict:
        """Calculate performance statistics from a list of trades."""
        if not trades:
            return {
                "total_trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "total_pnl": 0, "avg_pnl": 0,
                "max_win": 0, "max_loss": 0, "sharpe": 0
            }

        pnls = [float(t.get("pnl_usd") or 0) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        total_pnl = sum(pnls)
        avg_pnl = total_pnl / len(pnls) if pnls else 0

        # Simple Sharpe approximation
        import statistics
        if len(pnls) > 1:
            std = statistics.stdev(pnls)
            sharpe = (avg_pnl / std) if std > 0 else 0
        else:
            sharpe = 0

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) if trades else 0,
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
            "max_win": max(pnls) if pnls else 0,
            "max_loss": min(pnls) if pnls else 0,
            "sharpe": round(sharpe, 4)
        }

    def _determine_target_role(self, patterns: List[Dict]) -> str:
        """Determine which role should receive the proposal based on patterns."""
        for p in patterns:
            ptype = p.get("type", "")
            if ptype == "quick_stop_loss":
                return "analyst"  # Bad entry timing = bad data analysis
            if ptype == "low_confidence_losses":
                return "coach"  # Coach should have vetoed these
            if ptype == "symbol_losses":
                return "analyst"  # Analyst needs better symbol analysis
            if ptype == "consecutive_losses":
                return "trader"  # Trader decision making

        return "trader"  # Default target

    def _format_trades_for_ai(self, trades: List[Dict]) -> str:
        """Format trades into a readable summary for AI analysis."""
        lines = []
        for t in trades[-10:]:  # Last 10 max
            pnl = float(t.get("pnl_usd") or 0)
            symbol = t.get("symbol", "?")
            direction = t.get("direction", "?")
            confidence = t.get("ai_confidence_at_entry", "?")
            reason = t.get("close_reason", "?")
            lines.append(
                f"  {symbol} {direction} | P&L: ${pnl:.2f} | "
                f"Confidence: {confidence}% | Closed: {reason}"
            )
        return "\n".join(lines) if lines else "No recent trades"

    def _store_proposal(self, proposal: Dict) -> int:
        """Store a proposal in the database."""
        db = self._get_db()
        sql = """
            INSERT INTO prompt_change_proposals (
                proposing_role, proposing_model, target_role, change_type,
                current_prompt_id, current_model, proposed_prompt,
                proposed_model, reason, evidence, expected_improvement,
                supporting_trades, supporting_stats, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
        """
        return db.execute_returning_id(sql, (
            proposal.get("proposing_role"),
            proposal.get("proposing_model"),
            proposal.get("target_role"),
            proposal.get("change_type", "prompt"),
            proposal.get("current_prompt_id"),
            proposal.get("current_model"),
            proposal.get("proposed_prompt"),
            proposal.get("proposed_model"),
            proposal.get("reason", ""),
            proposal.get("evidence", "{}"),
            proposal.get("expected_improvement", ""),
            proposal.get("supporting_trades", "[]"),
            proposal.get("supporting_stats", "{}"),
        ))

    def _store_proposal_memory(self, proposal: Dict):
        """Store a memory about this proposal for all roles to learn from."""
        try:
            memory = self._get_memory()
            text = (
                f"Proposal from {proposal.get('proposing_role', 'unknown')}: "
                f"Suggested {proposal.get('change_type', 'prompt')} change to "
                f"{proposal.get('target_role', 'unknown')}. "
                f"Reason: {proposal.get('reason', '')[:200]}. "
                f"Expected improvement: {proposal.get('expected_improvement', '')[:200]}"
            )
            memory.store_memory(
                text=text,
                metadata={
                    "category": "proposal",
                    "proposing_role": proposal.get("proposing_role"),
                    "target_role": proposal.get("target_role"),
                    "proposal_id": proposal.get("id"),
                },
                collection="trade_lessons",
                role="all"
            )
        except Exception as e:
            logger.debug(f"Failed to store proposal memory (non-critical): {e}")

    def _store_compliment(self, from_role: str, message: str, stats: Dict):
        """Store a compliment memory — celebrating successes is important."""
        try:
            memory = self._get_memory()
            memory.store_memory(
                text=f"[Compliment from {from_role}] {message}",
                metadata={
                    "category": "compliment",
                    "from_role": from_role,
                    "win_rate": stats.get("win_rate"),
                    "total_pnl": stats.get("total_pnl"),
                },
                collection="trade_lessons",
                role="all"
            )
            logger.info(f"[ProposalEngine] Compliment from {from_role}: {message[:80]}...")
        except Exception as e:
            logger.debug(f"Failed to store compliment memory: {e}")

    def _check_model_performance(self, account_id: str) -> List[Dict]:
        """Check if any model is underperforming and suggest changes."""
        # This will be populated when model_performance table has data
        # For now, return empty — no model change proposals without evidence
        return []

    # ─────────────────────────────────────────────────────────────────
    # Proposal Lifecycle Management
    # ─────────────────────────────────────────────────────────────────

    def approve_proposal(self, proposal_id: int, reviewer: str = "owner",
                          notes: str = "") -> bool:
        """
        Approve a proposal and optionally implement it.
        """
        db = self._get_db()
        try:
            db.execute(
                """UPDATE prompt_change_proposals
                   SET status = 'approved', reviewed_by = %s,
                       review_notes = %s, reviewed_at = NOW()
                   WHERE id = %s""",
                (reviewer, notes, proposal_id)
            )
            logger.info(f"Proposal {proposal_id} approved by {reviewer}")
            return True
        except Exception as e:
            logger.error(f"Error approving proposal {proposal_id}: {e}")
            return False

    def reject_proposal(self, proposal_id: int, reviewer: str = "owner",
                         notes: str = "") -> bool:
        """Reject a proposal with optional notes."""
        db = self._get_db()
        try:
            db.execute(
                """UPDATE prompt_change_proposals
                   SET status = 'rejected', reviewed_by = %s,
                       review_notes = %s, reviewed_at = NOW()
                   WHERE id = %s""",
                (reviewer, notes, proposal_id)
            )
            logger.info(f"Proposal {proposal_id} rejected by {reviewer}")
            return True
        except Exception as e:
            logger.error(f"Error rejecting proposal {proposal_id}: {e}")
            return False

    def implement_proposal(self, proposal_id: int) -> bool:
        """
        Implement an approved proposal by creating a new prompt version.
        """
        db = self._get_db()
        try:
            proposal = db.fetch_one(
                "SELECT * FROM prompt_change_proposals WHERE id = %s",
                (proposal_id,)
            )
            if not proposal or proposal["status"] != "approved":
                logger.warning(f"Proposal {proposal_id} not found or not approved")
                return False

            if proposal.get("proposed_prompt"):
                # Create new prompt version
                current = db.fetch_one(
                    "SELECT * FROM prompt_versions WHERE id = %s",
                    (proposal["current_prompt_id"],)
                ) if proposal.get("current_prompt_id") else None

                if current:
                    new_version = (current.get("version") or 0) + 1
                    new_id = db.execute_returning_id(
                        """INSERT INTO prompt_versions
                           (role, version, prompt_name, system_prompt, model,
                            model_provider, is_active, changed_by, change_reason,
                            parent_version_id)
                           VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, %s)""",
                        (
                            current["role"],
                            new_version,
                            f"v{new_version} (Proposed by {proposal['proposing_role']})",
                            proposal["proposed_prompt"],
                            proposal.get("proposed_model") or current.get("model"),
                            current.get("model_provider"),
                            proposal["proposing_role"],
                            proposal.get("reason", "")[:500],
                            current["id"]
                        )
                    )

                    # Update proposal with the new prompt version ID
                    db.execute(
                        """UPDATE prompt_change_proposals
                           SET status = 'implemented', implemented_prompt_id = %s,
                               monitoring_start = NOW()
                           WHERE id = %s""",
                        (new_id, proposal_id)
                    )

                    logger.info(
                        f"Proposal {proposal_id} implemented as prompt version {new_id} "
                        f"for {current['role']}"
                    )
                    return True

            return False

        except Exception as e:
            logger.error(f"Error implementing proposal {proposal_id}: {e}")
            return False

    def get_pending_proposals(self) -> List[Dict]:
        """Get all pending proposals for review."""
        db = self._get_db()
        return db.fetch_all(
            """SELECT * FROM prompt_change_proposals
               WHERE status = 'pending'
               ORDER BY created_at DESC"""
        )

    def get_proposal_history(self, limit: int = 50) -> List[Dict]:
        """Get proposal history for all statuses."""
        db = self._get_db()
        return db.fetch_all(
            """SELECT * FROM prompt_change_proposals
               ORDER BY created_at DESC LIMIT %s""",
            (limit,)
        )
