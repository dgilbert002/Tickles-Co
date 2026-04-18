"""
JarvAIs Cognitive Engine
The brain of the system. Implements the 4-role cognitive architecture:

    1. THE ANALYST   — Compiles a comprehensive dossier from all data sources
    2. THE TRADER    — Makes the initial trading decision with reasoning
    3. THE COACH     — Challenges the Trader's assumptions, can approve or veto
    4. THE POST-MORTEM ANALYST — After trade closes, performs deep forensic analysis

The engine orchestrates the full signal-to-decision pipeline:
    EA Signal → Dossier Assembly → Trader Decision → Coach Challenge →
    Final Verdict → Execute or Veto → (later) Post-Mortem → Memory Storage

Usage:
    from core.cognitive_engine import get_cognitive_engine
    engine = get_cognitive_engine("DEMO_001")
    result = engine.process_signal(signal_id=1, signal_data={...})
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

from core.time_utils import utcnow

# Company Intelligence System imports
try:
    from services.trade_scorer import TradeScorer
    from services.proposal_engine import ProposalEngine
    _INTELLIGENCE_AVAILABLE = True
except ImportError:
    _INTELLIGENCE_AVAILABLE = False

logger = logging.getLogger("jarvais.cognitive_engine")


# ─────────────────────────────────────────────────────────────────────
# System Prompts for Each Cognitive Role
# ─────────────────────────────────────────────────────────────────────

ANALYST_SYSTEM_PROMPT = """You are the Analyst for JarvAIs, an autonomous AI trading system.

Your ONLY job is to compile a comprehensive, factual dossier about the current market state.
You do NOT make trading decisions. You present facts, data, and context.

You must be:
- Exhaustive: Include every relevant data point available
- Objective: No bias toward buying or selling
- Structured: Use clear sections and formatting
- Quantitative: Include specific numbers, not vague descriptions

Your output will be read by the Trader and Coach roles to make their decisions."""

TRADER_SYSTEM_PROMPT = """You are the Trader for JarvAIs, an autonomous AI trading system.

You are a world-class trader with the combined expertise of Warren Buffett's discipline,
George Soros's macro awareness, and Jim Simons's quantitative rigor.

You will receive a comprehensive dossier about a trading signal. Your job is to:
1. Analyze the signal and all supporting data
2. Make a clear APPROVE or VETO decision
3. Assign a confidence score from 1-100
4. Provide detailed reasoning for your decision
5. State what would make you WRONG (your invalidation thesis)

CRITICAL RULES:
- You must NEVER trade on emotion or FOMO
- You must NEVER ignore the risk state (daily limits, open positions)
- If the data is insufficient or contradictory, you VETO
- If the signal conflicts with the higher timeframe trend, you need VERY strong reasons to approve
- Your confidence score must be HONEST — do not inflate it
- A confidence of 65 means "slightly more likely to work than not"
- A confidence of 90+ means "overwhelming evidence in favor"

You MUST respond in this exact JSON format:
{
    "decision": "APPROVE" or "VETO",
    "confidence": 1-100,
    "reasoning": "Detailed multi-paragraph reasoning...",
    "invalidation": "What would make this trade wrong...",
    "entry_price": suggested entry or null,
    "sl_price": suggested stop loss or null,
    "tp_price": suggested take profit or null,
    "position_size_adjustment": 1.0 (normal) or 0.5 (reduce) or 0.0 (skip),
    "urgency": "immediate" or "wait_for_pullback" or "no_rush",
    "key_factors": ["factor1", "factor2", "factor3"]
}"""

COACH_SYSTEM_PROMPT = """You are the Coach for JarvAIs, an autonomous AI trading system.

You are the critical voice of reason. Your job is to CHALLENGE the Trader's decision.
You are a risk manager, a devil's advocate, and a mentor rolled into one.

You will receive:
1. The original dossier (market data, signals, context)
2. The Trader's decision and reasoning

Your job is to:
1. Find FLAWS in the Trader's reasoning
2. Identify risks the Trader may have overlooked
3. Check if the Trader is being overconfident or underconfident
4. Consider what happened in SIMILAR past situations (from memory)
5. Make a FINAL decision: CONFIRM, OVERRIDE, or CHALLENGE

CRITICAL RULES:
- You are NOT a yes-man. If the Trader is wrong, say so.
- If the Trader approved with high confidence but the evidence is weak, OVERRIDE to VETO
- If the Trader vetoed but the evidence is strong, you can OVERRIDE to APPROVE
- If you're unsure, CHALLENGE (ask the Trader to reconsider specific points)
- You must consider the FULL context: time of day, day of week, recent losses, market regime
- If the system has been losing recently, be MORE conservative
- If the system has been winning, watch for overconfidence

You MUST respond in this exact JSON format:
{
    "final_decision": "CONFIRM" or "OVERRIDE_APPROVE" or "OVERRIDE_VETO" or "CHALLENGE",
    "adjusted_confidence": 1-100,
    "coach_reasoning": "Detailed reasoning for your decision...",
    "risks_identified": ["risk1", "risk2"],
    "trader_blind_spots": ["blindspot1", "blindspot2"],
    "historical_parallels": "What happened in similar situations...",
    "recommendation": "Final recommendation text..."
}"""

POST_MORTEM_SYSTEM_PROMPT = """You are the Post-Mortem Analyst for JarvAIs, an autonomous AI trading system.

A trade has just closed. Your job is to perform a DEEP forensic analysis of what happened.
You will receive the complete trade record: entry, exit, P&L, the original dossier,
the Trader's reasoning, the Coach's assessment, and what actually happened in the market.

Your analysis must cover:
1. TECHNICAL: Did the indicators correctly predict the move? Which ones were right/wrong?
2. TIMING: Was the entry too early, too late, or perfect? What about the exit?
3. RISK: Was the position size appropriate? Was the SL too tight or too loose?
4. DECISION QUALITY: Was the AI's reasoning sound, even if the trade lost?
5. MARKET CONTEXT: Did unexpected news or events affect the outcome?
6. PATTERN RECOGNITION: Does this trade fit a pattern we've seen before?
7. LESSON: What is the ONE key lesson from this trade?

CRITICAL RULES:
- A winning trade with bad reasoning is WORSE than a losing trade with good reasoning
- Focus on PROCESS, not just OUTCOME
- Be specific: "RSI was at 72 and declining" not "RSI was overbought"
- Identify if this trade reveals a systematic weakness in our approach
- The lesson must be actionable and specific, not generic

You MUST respond in this exact JSON format:
{
    "outcome_quality": "excellent" or "good" or "neutral" or "poor" or "terrible",
    "decision_quality": "excellent" or "good" or "neutral" or "poor" or "terrible",
    "technical_analysis": "What the indicators showed and whether they were right...",
    "timing_analysis": "Entry and exit timing assessment...",
    "risk_analysis": "Position sizing and SL/TP assessment...",
    "what_worked": ["thing1", "thing2"],
    "what_failed": ["thing1", "thing2"],
    "lesson_learned": "The ONE key actionable lesson...",
    "pattern_identified": "Any recurring pattern this fits...",
    "confidence_was_calibrated": true/false,
    "would_take_again": true/false,
    "suggested_adjustment": "What to change for next time...",
    "tags": ["tag1", "tag2", "tag3"]
}"""

DAILY_REVIEW_SYSTEM_PROMPT = """You are conducting the Daily Review for JarvAIs, an autonomous AI trading system.

You will receive a complete summary of today's trading activity across all accounts.
Your job is to coach the system — identify what's working, what's not, and what to change.

Think of yourself as a head trader reviewing the day's performance with your team.

Your review must cover:
1. PERFORMANCE: Overall P&L, win rate, best/worst trades
2. PATTERNS: Any recurring themes in wins or losses
3. AI QUALITY: Was the AI adding value vs the raw EA signals?
4. RISK: Were risk limits respected? Any close calls?
5. MARKET: What was the market doing today? Was it a good day to trade?
6. IMPROVEMENTS: Specific, actionable changes for tomorrow
7. CONFIDENCE CALIBRATION: Are our confidence scores accurate?

You MUST respond in this exact JSON format:
{
    "overall_grade": "A" through "F",
    "summary": "One paragraph summary of the day...",
    "wins_analysis": "What drove the winning trades...",
    "losses_analysis": "What caused the losing trades...",
    "ai_value_added": "Did the AI help or hurt today?...",
    "patterns_discovered": ["pattern1", "pattern2"],
    "adjustments_for_tomorrow": ["adjustment1", "adjustment2"],
    "confidence_calibration_notes": "Are we over/under confident?...",
    "risk_assessment": "Were risk rules followed?...",
    "market_regime_assessment": "What kind of market was today?...",
    "morale_check": "How should the system feel about today?..."
}"""


# ─────────────────────────────────────────────────────────────────────
# Cognitive Engine Class
# ─────────────────────────────────────────────────────────────────────

class CognitiveEngine:
    """
    The brain of JarvAIs. Orchestrates the 4-role cognitive pipeline
    for signal validation, trade execution, and post-trade learning.
    """

    def __init__(self, account_id: str):
        self.account_id = account_id
        self._model = None
        self._db = None
        self._memory = None
        self._risk_manager = None
        self._mt5 = None
        self._data_aggregator = None

        logger.info(f"[{account_id}] Cognitive Engine initialized")

    # Lazy-loaded dependencies
    @property
    def model(self):
        if self._model is None:
            from core.model_interface import get_model_interface
            self._model = get_model_interface()
        return self._model

    @property
    def db(self):
        if self._db is None:
            from db.database import get_db
            self._db = get_db()
        return self._db

    @property
    def memory(self):
        if self._memory is None:
            from core.memory_manager import get_memory_manager
            self._memory = get_memory_manager()
        return self._memory

    @property
    def risk_manager(self):
        if self._risk_manager is None:
            from core.risk_manager import get_risk_manager
            self._risk_manager = get_risk_manager(self.account_id)
        return self._risk_manager

    @property
    def mt5(self):
        if self._mt5 is None:
            from core.mt5_executor import get_mt5_executor
            self._mt5 = get_mt5_executor(self.account_id)
        return self._mt5

    # ─────────────────────────────────────────────────────────────────
    # Prompt Loading — DB first, hardcoded fallback
    # ─────────────────────────────────────────────────────────────────

    # Map role names to their hardcoded fallback prompts
    _FALLBACK_PROMPTS = {
        "analyst": ANALYST_SYSTEM_PROMPT,
        "trader": TRADER_SYSTEM_PROMPT,
        "coach": COACH_SYSTEM_PROMPT,
        "post_mortem": POST_MORTEM_SYSTEM_PROMPT,
        "daily_review": DAILY_REVIEW_SYSTEM_PROMPT,
    }

    def _load_prompt(self, role: str) -> str:
        """
        Load the active system prompt for a role from prompt_versions table.
        Falls back to the hardcoded constant if no DB row exists.
        This ensures Prompt Engineer edits actually affect trading decisions.
        """
        try:
            row = self.db.fetch_one(
                """SELECT system_prompt FROM prompt_versions
                   WHERE role = %s AND is_active = 1
                   ORDER BY version DESC LIMIT 1""",
                (role,)
            )
            if row and row.get("system_prompt"):
                logger.debug(f"[{self.account_id}] Loaded {role} prompt from DB")
                return row["system_prompt"]
        except Exception as e:
            logger.warning(f"[{self.account_id}] Failed to load {role} prompt from DB: {e}")

        logger.debug(f"[{self.account_id}] Using hardcoded fallback prompt for {role}")
        return self._FALLBACK_PROMPTS.get(role, "")

    # ─────────────────────────────────────────────────────────────────
    # ROLE 1: THE ANALYST — Dossier Assembly
    # ─────────────────────────────────────────────────────────────────

    def build_dossier(self, signal_data: Dict[str, Any],
                      risk_mode: str = "normal",
                      is_free_trade: bool = False) -> str:
        """
        Compile a comprehensive dossier from all available data sources.
        This is the rich, 5000+ character prompt that gives the AI full context.

        The dossier includes:
        - Signal details (from EA)
        - Current candle data (M5, H4, D1)
        - Indicator values
        - Risk state
        - Recent trade history
        - Relevant memories from vector DB
        - News/sentiment (if available)
        - Open positions
        - Time/session context
        """
        symbol = signal_data.get("symbol", "UNKNOWN")
        direction = signal_data.get("direction", "UNKNOWN")

        sections = []

        # ── Section 1: Signal Overview ──
        sections.append(self._build_signal_section(signal_data))

        # ── Section 2: Market Data (Candles) ──
        sections.append(self._build_candle_section(symbol))

        # ── Section 3: Indicator Snapshot ──
        sections.append(self._build_indicator_section(signal_data))

        # ── Section 4: Risk State ──
        sections.append(self.risk_manager.get_risk_summary_for_prompt())

        # ── Section 5: Open Positions ──
        sections.append(self._build_positions_section())

        # ── Section 6: Recent Trade History ──
        sections.append(self._build_history_section(symbol))

        # ── Section 7: Relevant Memories ──
        sections.append(self._build_memory_section(signal_data))

        # ── Section 8: News & Sentiment ──
        sections.append(self._build_news_section(symbol))

        # ── Section 9: Time & Session Context ──
        sections.append(self._build_time_section())

        # ── Section 10: Alpha Intelligence (AI Summaries + Source Scores) ──
        sections.append(self._build_alpha_intelligence_section(symbol))

        # ── Section 11: Special Conditions ──
        if is_free_trade:
            sections.append("## Special Condition: FREE TRADE\n"
                            "Daily target has been reached. This is a FREE TRADE — "
                            "we are only risking today's profit. You may be slightly more "
                            "experimental, but do NOT be reckless.\n")

        if risk_mode == "cautious":
            sections.append("## Special Condition: CAUTIOUS MODE\n"
                            "Daily target reached. Position sizes are halved. "
                            "Only take very high-conviction setups.\n")

        # Combine all sections
        dossier = f"""# TRADING SIGNAL DOSSIER
## Account: {self.account_id}
## Generated: {utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC
## Signal: {direction} {symbol}

---

""" + "\n---\n\n".join(s for s in sections if s)

        logger.info(f"[{self.account_id}] Dossier compiled: {len(dossier)} characters, "
                    f"{len(sections)} sections")

        return dossier

    def _build_signal_section(self, signal_data: Dict[str, Any]) -> str:
        """Build the signal details section of the dossier."""
        sd = signal_data
        strategy_details = sd.get("strategy_details", {})

        strategy_text = ""
        if strategy_details:
            strategy_text = "\n### Strategy Votes:\n"
            for name, details in strategy_details.items():
                vote = details.get("vote", 0)
                weight = details.get("weight", 1.0)
                vote_str = "LONG" if vote > 0 else "SHORT" if vote < 0 else "NEUTRAL"
                strategy_text += f"- **{name}**: {vote_str} (weight: {weight})\n"

        return f"""## 1. SIGNAL DETAILS
- **Symbol**: {sd.get('symbol', 'N/A')}
- **Direction**: {sd.get('direction', 'N/A')}
- **Current Price**: {sd.get('current_price', 'N/A')}
- **Votes Long**: {sd.get('votes_long', 0):.2f}
- **Votes Short**: {sd.get('votes_short', 0):.2f}
- **Vote Ratio**: {sd.get('vote_ratio', 0):.2f}
- **Market State**: {sd.get('market_state', 'N/A')}
- **HTF Trend (D1)**: {sd.get('htf_trend', 'N/A')}
- **HTF Context (H4)**: {sd.get('htf_context', 'N/A')}
- **Spread**: {sd.get('spread_points', 'N/A')} points
- **EA Stop Loss**: {sd.get('ea_stop_loss', 'N/A')}
- **EA Take Profit**: {sd.get('ea_take_profit', 'N/A')}
- **EA Lot Size**: {sd.get('ea_lot_size', 'N/A')}
{strategy_text}"""

    def _build_candle_section(self, symbol: str) -> str:
        """Build the candle data section with M5, H4, and D1 data."""
        section = "## 2. MARKET DATA (Recent Candles)\n"

        for tf, count, label in [("M5", 20, "5-Minute"), ("H4", 10, "4-Hour"), ("D1", 5, "Daily")]:
            try:
                candles = self.mt5.get_candles(symbol, tf, count)
                if candles:
                    section += f"\n### {label} ({tf}) — Last {len(candles)} candles:\n"
                    section += "| Time | Open | High | Low | Close | Volume |\n"
                    section += "|------|------|------|-----|-------|--------|\n"
                    for c in candles[-min(len(candles), count):]:
                        time_str = c.time.strftime('%m-%d %H:%M') if c.time else "N/A"
                        section += (f"| {time_str} | {c.open:.2f} | {c.high:.2f} | "
                                    f"{c.low:.2f} | {c.close:.2f} | {c.tick_volume} |\n")
                else:
                    section += f"\n### {label} ({tf}): No data available\n"
            except Exception as e:
                section += f"\n### {label} ({tf}): Error fetching data: {e}\n"

        return section

    def _build_indicator_section(self, signal_data: Dict[str, Any]) -> str:
        """Build the indicator values section."""
        indicators = signal_data.get("indicator_values", {})
        if not indicators:
            return "## 3. INDICATOR SNAPSHOT\nNo indicator data provided by EA.\n"

        section = "## 3. INDICATOR SNAPSHOT\n"
        for name, value in indicators.items():
            if isinstance(value, float):
                section += f"- **{name}**: {value:.4f}\n"
            else:
                section += f"- **{name}**: {value}\n"

        return section

    def _build_positions_section(self) -> str:
        """Build the open positions section."""
        try:
            positions = self.mt5.get_open_positions()
            if not positions:
                return "## 5. OPEN POSITIONS\nNo open positions.\n"

            section = "## 5. OPEN POSITIONS\n"
            section += "| Symbol | Direction | Volume | Entry | Current | P&L | SL | TP |\n"
            section += "|--------|-----------|--------|-------|---------|-----|----|----|---|\n"
            for p in positions:
                section += (f"| {p.symbol} | {p.direction} | {p.volume} | "
                            f"{p.open_price:.2f} | {p.current_price:.2f} | "
                            f"${p.profit:.2f} | {p.sl:.2f} | {p.tp:.2f} |\n")

            total_profit = sum(p.profit for p in positions)
            section += f"\n**Total Open P&L: ${total_profit:.2f}**\n"
            return section
        except Exception as e:
            return f"## 5. OPEN POSITIONS\nError: {e}\n"

    def _build_history_section(self, symbol: str) -> str:
        """Build the recent trade history section."""
        try:
            recent_trades = self.db.get_recent_trades(self.account_id, limit=10)
            if not recent_trades:
                return "## 6. RECENT TRADE HISTORY\nNo recent trades.\n"

            section = "## 6. RECENT TRADE HISTORY (Last 10 trades)\n"
            wins = sum(1 for t in recent_trades if t.get("pnl", 0) > 0)
            losses = sum(1 for t in recent_trades if t.get("pnl", 0) < 0)
            total_pnl = sum(t.get("pnl", 0) for t in recent_trades)

            section += f"**Win Rate: {wins}/{len(recent_trades)} ({wins/len(recent_trades)*100:.0f}%) | Total P&L: ${total_pnl:.2f}**\n\n"

            for t in recent_trades:
                pnl = t.get("pnl", 0)
                emoji = "W" if pnl > 0 else "L"
                section += (f"- [{emoji}] {t.get('symbol', 'N/A')} {t.get('direction', 'N/A')} "
                            f"— P&L: ${pnl:.2f} | Confidence: {t.get('confidence', 'N/A')} | "
                            f"Lesson: {t.get('lesson', 'N/A')}\n")

            return section
        except Exception as e:
            return f"## 6. RECENT TRADE HISTORY\nError: {e}\n"

    def _build_memory_section(self, signal_data: Dict[str, Any]) -> str:
        """Build the relevant memories section from vector DB."""
        try:
            symbol = signal_data.get("symbol", "")
            direction = signal_data.get("direction", "")
            market_state = signal_data.get("market_state", "")

            # Query vector DB for similar past situations
            query = (f"{direction} signal on {symbol} in {market_state} market "
                     f"with vote ratio {signal_data.get('vote_ratio', 0):.2f}")

            memories = self.memory.search_similar(query, limit=5)

            if not memories:
                return "## 7. RELEVANT MEMORIES\nNo similar past situations found in memory.\n"

            section = "## 7. RELEVANT MEMORIES (Similar Past Situations)\n"
            for i, mem in enumerate(memories, 1):
                section += f"\n### Memory #{i} (Similarity: {mem.get('score', 0):.2f})\n"
                section += f"- **Date**: {mem.get('date', 'N/A')}\n"
                section += f"- **Situation**: {mem.get('text', 'N/A')}\n"
                section += f"- **Outcome**: {mem.get('outcome', 'N/A')}\n"
                section += f"- **Lesson**: {mem.get('lesson', 'N/A')}\n"

            return section
        except Exception as e:
            logger.warning(f"[{self.account_id}] Memory search failed: {e}")
            return "## 7. RELEVANT MEMORIES\nMemory system unavailable.\n"

    def _build_news_section(self, symbol: str) -> str:
        """Build the news and sentiment section."""
        try:
            from data.collectors import get_collector_registry
            registry = get_collector_registry()
            # Get cached/recent items from the registry (sync access)
            news = registry.get_recent_items(minutes=60) if registry else []

            if not news:
                return "## 8. NEWS & SENTIMENT\nNo recent news available.\n"

            section = "## 8. NEWS & SENTIMENT (Last 60 minutes)\n"
            for item in news[:10]:
                section += (f"- [{item.get('source', 'N/A')}] "
                            f"{item.get('headline', 'N/A')} "
                            f"(Sentiment: {item.get('sentiment', 'N/A')})\n")

            return section
        except Exception as e:
            return "## 8. NEWS & SENTIMENT\nNews aggregator not available.\n"

    def _build_time_section(self) -> str:
        """Build the time and session context section."""
        now = utcnow()
        hour = now.hour

        # Determine active sessions
        sessions = []
        if 0 <= hour < 8:
            sessions.append("Asian Session (Tokyo)")
        if 7 <= hour < 16:
            sessions.append("European Session (London)")
        if 13 <= hour < 22:
            sessions.append("American Session (New York)")
        if 7 <= hour < 9:
            sessions.append("London Open (HIGH VOLATILITY)")
        if 13 <= hour < 15:
            sessions.append("New York Open (HIGH VOLATILITY)")

        day_name = now.strftime("%A")
        is_friday = day_name == "Friday"

        section = f"""## 9. TIME & SESSION CONTEXT
- **Current Time (UTC)**: {now.strftime('%Y-%m-%d %H:%M:%S')}
- **Day**: {day_name}
- **Active Sessions**: {', '.join(sessions) if sessions else 'Off-hours'}
"""
        if is_friday and hour >= 18:
            section += "- **WARNING**: Late Friday — reduced liquidity, wider spreads, weekend risk\n"

        if hour >= 21 or hour < 1:
            section += "- **NOTE**: Low liquidity period — be cautious with entries\n"

        return section

    def _build_alpha_intelligence_section(self, symbol: str) -> str:
        """Build the Alpha Intelligence section with recent AI summaries and source scores."""
        section = "## 10. ALPHA INTELLIGENCE\n"

        try:
            # Part A: Latest AI Alpha summaries (from alpha_summaries table)
            summaries = self.db.fetch_all(
                """SELECT summary_text, pane_id, timeframe_minutes, created_at
                   FROM alpha_summaries
                   WHERE created_at >= NOW() - INTERVAL 2 HOUR
                   ORDER BY created_at DESC LIMIT 3"""
            )
            if summaries:
                section += "\n### Recent AI Alpha Summaries\n"
                for s in summaries:
                    tf = s.get('timeframe_minutes', '?')
                    created = s.get('created_at', '')
                    text = (s.get('summary_text', '') or '')[:500]
                    section += f"\n**[{tf}min summary @ {created}]**\n{text}\n"
            else:
                section += "\nNo recent AI alpha summaries available.\n"

            # Part B: Alpha source leaderboard (top performing sources)
            top_sources = self.db.fetch_all(
                """SELECT source_name, sub_source, total_trades, win_rate, avg_pnl, total_pnl
                   FROM alpha_source_scores
                   WHERE total_trades >= 3
                   ORDER BY avg_pnl DESC LIMIT 5"""
            )
            if top_sources:
                section += "\n### Alpha Source Leaderboard (Top 5 by Avg PnL)\n"
                section += "| Source | Sub-Source | Trades | Win Rate | Avg PnL | Total PnL |\n"
                section += "|--------|------------|--------|----------|---------|-----------|\n"
                for src in top_sources:
                    section += (f"| {src.get('source_name', 'N/A')} "
                                f"| {src.get('sub_source', '-') or '-'} "
                                f"| {src.get('total_trades', 0)} "
                                f"| {src.get('win_rate', 0):.0f}% "
                                f"| ${src.get('avg_pnl', 0):.2f} "
                                f"| ${src.get('total_pnl', 0):.2f} |\n")

            # Part C: Worst performing sources (avoid these)
            worst_sources = self.db.fetch_all(
                """SELECT source_name, sub_source, total_trades, win_rate, avg_pnl
                   FROM alpha_source_scores
                   WHERE total_trades >= 3 AND avg_pnl < 0
                   ORDER BY avg_pnl ASC LIMIT 3"""
            )
            if worst_sources:
                section += "\n### Underperforming Sources (Caution)\n"
                for src in worst_sources:
                    section += (f"- **{src.get('source_name', 'N/A')}** "
                                f"({src.get('sub_source', '-') or '-'}): "
                                f"Avg PnL ${src.get('avg_pnl', 0):.2f}, "
                                f"Win Rate {src.get('win_rate', 0):.0f}%\n")

        except Exception as e:
            section += f"\nAlpha intelligence unavailable: {e}\n"

        return section

    # ─────────────────────────────────────────────────────────────────
    # ROLE 2 & 3: TRADER-COACH DIALOGUE
    # ─────────────────────────────────────────────────────────────────

    def _run_trader(self, dossier: str, signal_id: int,
                    source: str = None, source_detail: str = None,
                    author: str = None) -> Dict[str, Any]:
        """
        Role 2: The Trader analyzes the dossier and makes a decision.
        """
        logger.info(f"[{self.account_id}] TRADER analyzing signal #{signal_id}...")

        response = self.model.query(
            role="trader",
            system_prompt=self._load_prompt("trader"),
            user_prompt=dossier,
            account_id=self.account_id,
            signal_id=signal_id,
            model_role="primary",
            context="cognitive_trader_analysis",
            source=source,
            source_detail=source_detail,
            author=author,
            media_type="text",
        )

        if not response.success:
            logger.error(f"[{self.account_id}] Trader query failed: {response.error_message}")
            return {
                "decision": "VETO",
                "confidence": 0,
                "reasoning": f"AI query failed: {response.error_message}",
                "raw_response": response
            }

        # Parse JSON response
        try:
            result = json.loads(response.content)
            result["raw_response"] = response
            logger.info(f"[{self.account_id}] TRADER decision: {result.get('decision', 'N/A')} "
                        f"(confidence: {result.get('confidence', 0)})")
            return result
        except json.JSONDecodeError:
            # Try to extract decision from free-form text
            logger.warning(f"[{self.account_id}] Trader response not valid JSON, attempting extraction")
            content = response.content.upper()
            decision = "APPROVE" if "APPROVE" in content else "VETO"
            return {
                "decision": decision,
                "confidence": 50,
                "reasoning": response.content,
                "raw_response": response,
                "parse_warning": "Response was not valid JSON"
            }

    def _run_coach(self, dossier: str, trader_decision: Dict[str, Any],
                   signal_id: int, source: str = None,
                   source_detail: str = None, author: str = None) -> Dict[str, Any]:
        """
        Role 3: The Coach challenges the Trader's decision.
        """
        logger.info(f"[{self.account_id}] COACH reviewing Trader's decision on signal #{signal_id}...")

        # Build the Coach's prompt with both dossier and Trader's reasoning
        coach_prompt = f"""{dossier}

---

## THE TRADER'S DECISION

The Trader has made the following decision. Your job is to CHALLENGE it.

**Decision**: {trader_decision.get('decision', 'N/A')}
**Confidence**: {trader_decision.get('confidence', 0)}/100
**Reasoning**: {trader_decision.get('reasoning', 'N/A')}
**Invalidation Thesis**: {trader_decision.get('invalidation', 'N/A')}
**Key Factors**: {json.dumps(trader_decision.get('key_factors', []))}

---

Now, as the Coach, critically evaluate this decision. Find flaws, identify risks,
and make your FINAL determination: CONFIRM, OVERRIDE_APPROVE, OVERRIDE_VETO, or CHALLENGE.
"""

        response = self.model.query(
            role="coach",
            system_prompt=self._load_prompt("coach"),
            user_prompt=coach_prompt,
            account_id=self.account_id,
            signal_id=signal_id,
            model_role="primary",
            context="cognitive_coach_review",
            source=source,
            source_detail=source_detail,
            author=author,
            media_type="text",
        )

        if not response.success:
            logger.error(f"[{self.account_id}] Coach query failed: {response.error_message}")
            # If Coach fails, fall back to Trader's decision
            return {
                "final_decision": "CONFIRM" if trader_decision.get("decision") == "APPROVE" else "OVERRIDE_VETO",
                "adjusted_confidence": trader_decision.get("confidence", 50),
                "coach_reasoning": f"Coach unavailable: {response.error_message}. Defaulting to Trader's decision.",
                "raw_response": response
            }

        try:
            result = json.loads(response.content)
            result["raw_response"] = response
            logger.info(f"[{self.account_id}] COACH decision: {result.get('final_decision', 'N/A')} "
                        f"(adjusted confidence: {result.get('adjusted_confidence', 0)})")
            return result
        except json.JSONDecodeError:
            logger.warning(f"[{self.account_id}] Coach response not valid JSON")
            return {
                "final_decision": "CONFIRM",
                "adjusted_confidence": trader_decision.get("confidence", 50),
                "coach_reasoning": response.content,
                "raw_response": response,
                "parse_warning": "Response was not valid JSON"
            }

    # ─────────────────────────────────────────────────────────────────
    # MAIN PIPELINE: Process Signal
    # ─────────────────────────────────────────────────────────────────

    def process_signal(self, signal_id: int, signal_data: Dict[str, Any],
                       is_free_trade: bool = False,
                       risk_mode: str = "normal") -> Dict[str, Any]:
        """
        Main entry point: Process a signal through the full cognitive pipeline.

        Steps:
        1. Build comprehensive dossier (Analyst role)
        2. Get Trader's decision
        3. Get Coach's review (with Challenge loop if needed)
        4. Apply confidence calibration from recent accuracy
        5. Resolve final verdict
        6. Execute or veto
        7. Log everything

        Returns:
            Dict with action ("execute" or "veto"), confidence, reasoning, etc.
        """
        logger.info(f"[{self.account_id}] === COGNITIVE PIPELINE START: Signal #{signal_id} ===")

        try:
            # Extract source info from signal_data for granular cost tracking
            sig_source = signal_data.get("source", "")
            sig_source_detail = signal_data.get("source_detail", "")
            sig_author = signal_data.get("author", "")

            # Step 1: Build the dossier (Analyst role)
            dossier = self.build_dossier(signal_data, risk_mode, is_free_trade)

            # Step 2: Trader's decision
            trader_result = self._run_trader(dossier, signal_id,
                                            source=sig_source, source_detail=sig_source_detail,
                                            author=sig_author)

            # Step 3: Coach's review
            coach_result = self._run_coach(dossier, trader_result, signal_id,
                                          source=sig_source, source_detail=sig_source_detail,
                                          author=sig_author)

            # Step 3b: Challenge loop - if Coach says CHALLENGE, give Trader
            # one chance to reconsider with the Coach's feedback
            challenge_round = 0
            max_challenges = 1
            while (coach_result.get("final_decision") == "CHALLENGE"
                   and challenge_round < max_challenges):
                challenge_round += 1
                logger.info(f"[{self.account_id}] CHALLENGE ROUND {challenge_round}: "
                            f"Coach challenged Trader, re-running with feedback")

                coach_risks = json.dumps(coach_result.get("risks_identified", []))
                coach_blindspots = json.dumps(coach_result.get("trader_blind_spots", []))
                challenge_feedback = (
                    f"\n\n---\n\n## COACH CHALLENGE (Round {challenge_round})\n\n"
                    f"The Coach has CHALLENGED your decision. Their feedback:\n\n"
                    f"**Coach Reasoning**: {coach_result.get('coach_reasoning', 'N/A')}\n"
                    f"**Risks Identified**: {coach_risks}\n"
                    f"**Blind Spots**: {coach_blindspots}\n\n"
                    f"Reconsider your decision. You may maintain it if you have strong "
                    f"reasons, but you must address each concern explicitly."
                )
                revised_dossier = dossier + challenge_feedback
                trader_result = self._run_trader(revised_dossier, signal_id,
                                                source=sig_source, source_detail=sig_source_detail,
                                                author=sig_author)
                coach_result = self._run_coach(dossier, trader_result, signal_id,
                                              source=sig_source, source_detail=sig_source_detail,
                                              author=sig_author)

            # Step 4: Apply confidence calibration based on recent accuracy
            calibrated = self._calibrate_confidence(
                coach_result.get("adjusted_confidence",
                                 trader_result.get("confidence", 50)),
                signal_data.get("symbol", "")
            )
            coach_result["adjusted_confidence"] = calibrated

            # Step 5: Resolve final verdict
            final_decision = self._resolve_verdict(trader_result, coach_result)

            # Extract alpha sources from Trader's response for attribution
            alpha_sources_used = trader_result.get("alpha_sources_used", [])
            if alpha_sources_used:
                logger.info(f"[{self.account_id}] Trader cited {len(alpha_sources_used)} alpha source(s)")

            # Step 5: Log the AI decision to database
            ai_decision_data = {
                "signal_id": signal_id,
                "account_id": self.account_id,
                "trader_decision": trader_result.get("decision", "VETO"),
                "trader_confidence": trader_result.get("confidence", 0),
                "trader_reasoning": trader_result.get("reasoning", ""),
                "coach_decision": coach_result.get("final_decision", "CONFIRM"),
                "coach_confidence": coach_result.get("adjusted_confidence", 0),
                "coach_reasoning": coach_result.get("coach_reasoning", ""),
                "final_decision": final_decision["action"].upper(),
                "final_confidence": final_decision["confidence"],
                "risks_identified": json.dumps(coach_result.get("risks_identified", [])),
                "dossier_length": len(dossier),
                "total_cost_usd": self._get_total_cost(trader_result, coach_result),
                "is_free_trade": is_free_trade,
                "risk_mode": risk_mode
            }
            self.db.insert_ai_decision(ai_decision_data)

            # Attach alpha sources to final_decision so _execute_trade can store them
            final_decision["alpha_sources_used"] = alpha_sources_used

            # Step 6: Execute or veto
            if final_decision["action"] == "execute":
                self._execute_trade(signal_id, signal_data, final_decision, risk_mode, is_free_trade)

            # Also track what the EA would have done (hypothetical)
            self._track_ea_hypothetical(signal_id, signal_data)

            final_decision["challenge_rounds"] = challenge_round

            logger.info(f"[{self.account_id}] === COGNITIVE PIPELINE END: "
                        f"{final_decision['action'].upper()} (confidence: {final_decision['confidence']}) "
                        f"[{challenge_round} challenge(s)] ===")

            return final_decision

        except Exception as e:
            logger.error(f"[{self.account_id}] Cognitive pipeline error: {e}", exc_info=True)
            return {
                "action": "veto",
                "confidence": 0,
                "reason": f"Pipeline error: {str(e)}",
                "challenge_rounds": 0
            }

    def _resolve_verdict(self, trader_result: Dict[str, Any],
                         coach_result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Resolve the final verdict from Trader and Coach decisions.

        Logic:
        - CONFIRM: Use Trader's decision with Coach's adjusted confidence
        - OVERRIDE_APPROVE: Execute even if Trader said VETO
        - OVERRIDE_VETO: Veto even if Trader said APPROVE
        - CHALLENGE: Default to VETO (conservative)
        """
        from core.config import get_config
        config = get_config()
        acct = config.get_account(self.account_id)
        min_confidence = 65  # Default
        if acct and acct.risk_settings:
            min_confidence = getattr(acct.risk_settings, 'min_confidence', 65)

        coach_decision = coach_result.get("final_decision", "CONFIRM")
        confidence = coach_result.get("adjusted_confidence",
                                      trader_result.get("confidence", 50))

        if coach_decision == "CONFIRM":
            if trader_result.get("decision") == "APPROVE" and confidence >= min_confidence:
                action = "execute"
                reason = "Trader APPROVED, Coach CONFIRMED"
            else:
                action = "veto"
                reason = (f"Trader {'APPROVED' if trader_result.get('decision') == 'APPROVE' else 'VETOED'}, "
                          f"Coach CONFIRMED, but confidence ({confidence}) below threshold ({min_confidence})")

        elif coach_decision == "OVERRIDE_APPROVE":
            if confidence >= min_confidence:
                action = "execute"
                reason = "Coach OVERRODE Trader's veto — strong evidence for trade"
            else:
                action = "veto"
                reason = f"Coach wanted to override, but confidence ({confidence}) below threshold"

        elif coach_decision == "OVERRIDE_VETO":
            action = "veto"
            reason = "Coach VETOED — identified critical risks"

        elif coach_decision == "CHALLENGE":
            action = "veto"
            reason = "Coach CHALLENGED — insufficient consensus, defaulting to conservative veto"

        else:
            action = "veto"
            reason = f"Unknown coach decision: {coach_decision}"

        logger.info(f"[{self.account_id}] VERDICT: {action.upper()} | "
                    f"Confidence: {confidence} | Reason: {reason}")

        return {
            "action": action,
            "confidence": confidence,
            "reason": reason,
            "trader_decision": trader_result.get("decision"),
            "coach_decision": coach_decision,
            "entry_price": trader_result.get("entry_price"),
            "sl_price": trader_result.get("sl_price"),
            "tp_price": trader_result.get("tp_price"),
            "key_factors": trader_result.get("key_factors", []),
            "risks": coach_result.get("risks_identified", [])
        }

    def _execute_trade(self, signal_id: int, signal_data: Dict[str, Any],
                       decision: Dict[str, Any], risk_mode: str,
                       is_free_trade: bool):
        """Execute the trade via MT5."""
        symbol = signal_data["symbol"]
        direction = signal_data["direction"]

        # Get SL/TP from EA or AI
        sl_price = decision.get("sl_price") or signal_data.get("ea_stop_loss", 0)
        tp_price = decision.get("tp_price") or signal_data.get("ea_take_profit", 0)
        current_price = signal_data.get("current_price", 0)

        if not sl_price or not current_price:
            logger.error(f"[{self.account_id}] Cannot execute: missing SL or price")
            return

        # Calculate SL distance
        sl_distance = abs(current_price - sl_price)

        # Calculate position size through risk manager
        sizing = self.risk_manager.calculate_position_size(
            symbol=symbol,
            sl_distance_points=sl_distance,
            confidence=decision["confidence"],
            risk_mode=risk_mode,
            is_free_trade=is_free_trade
        )

        lot_size = sizing["lot_size"]
        magic_number = self.mt5._generate_magic_number()

        # Get TP levels
        tp_prices = self.risk_manager.get_tp_prices(current_price, sl_price, direction)

        # Place the order (use TP1 as initial TP)
        initial_tp = tp_prices.get("tp1", tp_price)

        result = self.mt5.place_order(
            symbol=symbol,
            direction=direction,
            volume=lot_size,
            sl=sl_price,
            tp=initial_tp,
            comment=f"JarvAIs_s{signal_id}",
            magic_number=magic_number
        )

        if result.success:
            # Log the trade to database
            alpha_sources_json = None
            if decision.get("alpha_sources_used"):
                alpha_sources_json = json.dumps(decision["alpha_sources_used"])

            trade_data = {
                "signal_id": signal_id,
                "account_id": self.account_id,
                "mt5_ticket": result.order_ticket,
                "magic_number": magic_number,
                "symbol": symbol,
                "direction": direction,
                "lot_size": lot_size,
                "entry_price": result.price,
                "stop_loss": sl_price,
                "take_profit_1": tp_prices.get("tp1", 0),
                "take_profit_2": tp_prices.get("tp2", 0),
                "take_profit_3": tp_prices.get("tp3", 0),
                "ai_confidence_at_entry": decision["confidence"],
                "risk_percent_used": sizing["risk_pct"],
                "balance_at_entry": sizing.get("balance", 0),
                "is_free_trade": is_free_trade,
                "is_live": True,
                "alpha_sources": alpha_sources_json
            }
            self.db.insert_trade(trade_data)
            self.risk_manager.invalidate_cache()

            logger.info(f"[{self.account_id}] TRADE EXECUTED: {direction} {lot_size} {symbol} "
                        f"@ {result.price} | SL={sl_price} | TP1={tp_prices.get('tp1', 0)}")
        else:
            logger.error(f"[{self.account_id}] TRADE EXECUTION FAILED: {result.error_message}")

    def _track_ea_hypothetical(self, signal_id: int, signal_data: Dict[str, Any]):
        """
        Track what would have happened if we took the EA signal blindly.
        This is essential for measuring AI Alpha.
        """
        try:
            self.db.update_signal_ea_hypothetical(signal_id, {
                "ea_entry_price": signal_data.get("current_price", 0),
                "ea_sl": signal_data.get("ea_stop_loss", 0),
                "ea_tp": signal_data.get("ea_take_profit", 0),
                "ea_lot_size": signal_data.get("ea_lot_size", 0)
            })
        except Exception as e:
            logger.warning(f"[{self.account_id}] Failed to track EA hypothetical: {e}")

    def _get_total_cost(self, trader_result: Dict, coach_result: Dict) -> float:
        """Calculate total AI cost for this decision."""
        cost = 0.0
        for result in [trader_result, coach_result]:
            raw = result.get("raw_response")
            if raw and hasattr(raw, "cost_usd"):
                cost += raw.cost_usd
        return cost

    def _format_trade_alpha_sources(self, trade: Dict) -> str:
        """Format alpha sources from a trade record for display in prompts."""
        try:
            raw = trade.get("alpha_sources")
            if not raw:
                return "No alpha sources were recorded for this trade."

            sources = raw if isinstance(raw, list) else json.loads(raw)
            if not sources:
                return "No alpha sources were recorded for this trade."

            lines = []
            for i, src in enumerate(sources, 1):
                source = src.get("source", "unknown")
                sub = src.get("sub_source", "")
                subsub = src.get("sub_sub_source", "")
                influence = src.get("influence", "unknown")
                path = " → ".join(filter(None, [source, sub, subsub]))
                lines.append(f"- **Source {i}**: {path} (influence: {influence})")

            return "\n".join(lines)
        except Exception:
            return "Alpha source data could not be parsed."

    # ─────────────────────────────────────────────────────────────────
    # ROLE 4: POST-MORTEM ANALYSIS
    # ─────────────────────────────────────────────────────────────────

    def run_post_mortem(self, trade_id: int) -> Dict[str, Any]:
        """
        Run post-mortem analysis on a closed trade.
        Called when a trade is closed (by TP, SL, or manual close).
        """
        logger.info(f"[{self.account_id}] Running post-mortem on trade #{trade_id}")

        try:
            # Get the full trade record
            trade = self.db.get_trade(trade_id)
            if not trade:
                logger.error(f"Trade #{trade_id} not found")
                return {}

            # Get the original signal and AI decision
            signal = self.db.get_signal(trade.get("signal_id", 0))
            ai_decision = self.db.get_ai_decision_by_signal(trade.get("signal_id", 0))

            # Build the post-mortem prompt
            pm_prompt = f"""## TRADE RECORD FOR POST-MORTEM ANALYSIS

### Trade Details
- **Trade ID**: {trade_id}
- **Symbol**: {trade.get('symbol', 'N/A')}
- **Direction**: {trade.get('direction', 'N/A')}
- **Entry Price**: {trade.get('entry_price', 0)}
- **Exit Price**: {trade.get('exit_price', 0)}
- **Volume**: {trade.get('volume', 0)}
- **P&L**: ${trade.get('pnl', 0):.2f}
- **Duration**: {trade.get('duration_minutes', 0)} minutes
- **SL**: {trade.get('sl_price', 0)}
- **TP1**: {trade.get('tp1_price', 0)}
- **Result**: {'WIN' if trade.get('pnl', 0) > 0 else 'LOSS'}

### Original AI Decision
- **Trader Decision**: {ai_decision.get('trader_decision', 'N/A') if ai_decision else 'N/A'}
- **Trader Confidence**: {ai_decision.get('trader_confidence', 0) if ai_decision else 0}
- **Trader Reasoning**: {ai_decision.get('trader_reasoning', 'N/A') if ai_decision else 'N/A'}
- **Coach Decision**: {ai_decision.get('coach_decision', 'N/A') if ai_decision else 'N/A'}
- **Coach Reasoning**: {ai_decision.get('coach_reasoning', 'N/A') if ai_decision else 'N/A'}
- **Risks Identified**: {ai_decision.get('risks_identified', '[]') if ai_decision else '[]'}

### Market Context at Entry
- **Market State**: {signal.get('market_state', 'N/A') if signal else 'N/A'}
- **HTF Trend**: {signal.get('htf_trend', 'N/A') if signal else 'N/A'}
- **Vote Ratio**: {signal.get('vote_ratio', 0) if signal else 0}

### Alpha Sources Cited by Trader
{self._format_trade_alpha_sources(trade)}

Now perform your deep forensic analysis across ALL dimensions.
Pay special attention to which alpha sources helped or hurt this trade.
"""

            # Extract source info from the original signal for cost tracking
            pm_source = signal.get("source", "") if signal else ""
            pm_source_detail = signal.get("source_detail", "") if signal else ""
            pm_author = signal.get("author", "") if signal else ""

            response = self.model.query(
                role="post_mortem",
                system_prompt=self._load_prompt("post_mortem"),
                user_prompt=pm_prompt,
                account_id=self.account_id,
                trade_id=trade_id,
                model_role="primary",
                context="cognitive_post_mortem",
                source=pm_source,
                source_detail=pm_source_detail,
                author=pm_author,
                media_type="text",
            )

            if not response.success:
                logger.error(f"[{self.account_id}] Post-mortem query failed: {response.error_message}")
                return {}

            try:
                result = json.loads(response.content)
            except json.JSONDecodeError:
                result = {"lesson_learned": response.content, "parse_warning": "Not valid JSON"}

            # Store the lesson in the database
            lesson_data = {
                "trade_id": trade_id,
                "account_id": self.account_id,
                "outcome_quality": result.get("outcome_quality", "neutral"),
                "decision_quality": result.get("decision_quality", "neutral"),
                "lesson_learned": result.get("lesson_learned", ""),
                "what_worked": json.dumps(result.get("what_worked", [])),
                "what_failed": json.dumps(result.get("what_failed", [])),
                "pattern_identified": result.get("pattern_identified", ""),
                "would_take_again": result.get("would_take_again", False),
                "suggested_adjustment": result.get("suggested_adjustment", ""),
                "tags": json.dumps(result.get("tags", []))
            }
            self.db.insert_trade_lesson(lesson_data)

            # Store in vector memory for future retrieval
            memory_text = (f"Trade on {trade.get('symbol')} ({trade.get('direction')}): "
                           f"{'WIN' if trade.get('pnl', 0) > 0 else 'LOSS'} ${trade.get('pnl', 0):.2f}. "
                           f"Lesson: {result.get('lesson_learned', 'N/A')}. "
                           f"Pattern: {result.get('pattern_identified', 'N/A')}.")

            self.memory.store_memory(
                text=memory_text,
                metadata={
                    "type": "trade_lesson",
                    "trade_id": trade_id,
                    "symbol": trade.get("symbol"),
                    "direction": trade.get("direction"),
                    "pnl": trade.get("pnl", 0),
                    "confidence": ai_decision.get("final_confidence", 0) if ai_decision else 0,
                    "outcome": "win" if trade.get("pnl", 0) > 0 else "loss",
                    "account_id": self.account_id,
                    "timestamp": utcnow().isoformat()
                }
            )

            logger.info(f"[{self.account_id}] Post-mortem complete for trade #{trade_id}: "
                        f"Quality={result.get('decision_quality', 'N/A')}, "
                        f"Lesson={result.get('lesson_learned', 'N/A')[:80]}...")

            # ── Company Intelligence: Score trade and check for proposals ──
            if _INTELLIGENCE_AVAILABLE:
                try:
                    # Score the trade across all 3 axes (prompt, model, alpha source)
                    scorer = TradeScorer(self.db)

                    # Merge alpha sources: from post-mortem AI + from trade record
                    pm_alpha_sources = result.get("alpha_sources_used", [])
                    trade_alpha_sources = []
                    try:
                        trade_alpha_raw = trade.get("alpha_sources")
                        if trade_alpha_raw:
                            if isinstance(trade_alpha_raw, str):
                                trade_alpha_sources = json.loads(trade_alpha_raw)
                            elif isinstance(trade_alpha_raw, list):
                                trade_alpha_sources = trade_alpha_raw
                    except (json.JSONDecodeError, TypeError):
                        pass

                    # Use post-mortem sources if available (more forensic), else trade record
                    alpha_sources = pm_alpha_sources if pm_alpha_sources else trade_alpha_sources
                    if alpha_sources:
                        logger.info(f"[{self.account_id}] Scoring {len(alpha_sources)} alpha source(s) for trade #{trade_id}")

                    score_result = scorer.score_trade(
                        trade_id=trade_id,
                        account_id=self.account_id,
                        alpha_sources=alpha_sources
                    )
                    logger.info(f"[{self.account_id}] Trade #{trade_id} scored: "
                                f"prompt={score_result.get('prompt_scores', {})}, "
                                f"alpha_sources={len(score_result.get('alpha_source_scores', []))} scored")

                    # Run proposal engine to check for patterns and generate proposals
                    proposal_engine = ProposalEngine(self.db, self.model)
                    proposals = proposal_engine.analyze_after_trade_close(
                        trade_id=trade_id,
                        account_id=self.account_id
                    )
                    if proposals:
                        logger.info(f"[{self.account_id}] {len(proposals)} proposal(s) generated after trade #{trade_id}")
                    else:
                        logger.debug(f"[{self.account_id}] No proposals generated after trade #{trade_id}")

                except Exception as intel_err:
                    logger.warning(f"[{self.account_id}] Intelligence scoring/proposals error (non-fatal): {intel_err}")

            return result

        except Exception as e:
            logger.error(f"[{self.account_id}] Post-mortem error: {e}", exc_info=True)
            return {}

    # ─────────────────────────────────────────────────────────────────
    # DAILY REVIEW
    # ─────────────────────────────────────────────────────────────────

    def run_daily_review(self) -> Dict[str, Any]:
        """
        Run the daily self-coaching review.
        Analyzes all trades from today, identifies patterns, and generates adjustments.
        """
        logger.info(f"[{self.account_id}] === DAILY REVIEW START ===")

        try:
            # Gather today's data
            today_trades = self.db.get_today_trades(self.account_id)
            today_signals = self.db.get_today_signals_count(self.account_id)
            today_pnl = self.db.get_today_pnl(self.account_id)
            recent_lessons = self.db.get_recent_lessons(self.account_id, days=7)

            # Build the review prompt
            review_prompt = f"""## DAILY TRADING REVIEW
### Date: {utcnow().strftime('%Y-%m-%d')}
### Account: {self.account_id}

### Today's Summary
- **Total Signals Received**: {sum(today_signals.values()) if isinstance(today_signals, dict) else today_signals}
- **Trades Executed**: {len(today_trades)}
- **Total P&L**: ${today_pnl:.2f}

### Today's Trades
"""
            if today_trades:
                for t in today_trades:
                    pnl = t.get("pnl", 0)
                    review_prompt += (f"- {t.get('symbol')} {t.get('direction')} | "
                                      f"P&L: ${pnl:.2f} | Confidence: {t.get('confidence', 'N/A')} | "
                                      f"{'WIN' if pnl > 0 else 'LOSS'}\n")
            else:
                review_prompt += "No trades executed today.\n"

            review_prompt += f"\n### Recent Lessons (Last 7 days)\n"
            if recent_lessons:
                for lesson in recent_lessons[:10]:
                    review_prompt += f"- {lesson.get('lesson_learned', 'N/A')}\n"
            else:
                review_prompt += "No recent lessons.\n"

            review_prompt += "\nNow conduct your thorough daily review."

            response = self.model.query(
                role="daily_review",
                system_prompt=self._load_prompt("daily_review"),
                user_prompt=review_prompt,
                account_id=self.account_id,
                model_role="primary",
                context="cognitive_daily_review",
                source="system",
                source_detail="daily_review",
                media_type="text",
            )

            if not response.success:
                logger.error(f"[{self.account_id}] Daily review query failed")
                return {}

            try:
                result = json.loads(response.content)
            except json.JSONDecodeError:
                result = {"summary": response.content}

            # Store the review in the database
            self.db.insert_daily_review({
                "account_id": self.account_id,
                "date": utcnow().date(),
                "grade": result.get("overall_grade", "N/A"),
                "summary": result.get("summary", ""),
                "adjustments": json.dumps(result.get("adjustments_for_tomorrow", [])),
                "patterns": json.dumps(result.get("patterns_discovered", [])),
                "ai_value_assessment": result.get("ai_value_added", ""),
                "total_pnl": today_pnl,
                "trade_count": len(today_trades)
            })

            # Store review as memory
            self.memory.store_memory(
                text=f"Daily review {utcnow().strftime('%Y-%m-%d')}: "
                     f"Grade {result.get('overall_grade', 'N/A')}. "
                     f"P&L: ${today_pnl:.2f}. "
                     f"{result.get('summary', '')}",
                metadata={
                    "type": "daily_review",
                    "date": utcnow().isoformat(),
                    "grade": result.get("overall_grade", "N/A"),
                    "pnl": today_pnl,
                    "account_id": self.account_id
                }
            )

            logger.info(f"[{self.account_id}] === DAILY REVIEW END: "
                        f"Grade={result.get('overall_grade', 'N/A')} ===")

            return result

        except Exception as e:
            logger.error(f"[{self.account_id}] Daily review error: {e}", exc_info=True)
            return {}


# ─────────────────────────────────────────────────────────────────────
# Instance Management
# ─────────────────────────────────────────────────────────────────────

_engines: Dict[str, CognitiveEngine] = {}


def get_cognitive_engine(account_id: str) -> CognitiveEngine:
    """Get or create a CognitiveEngine for the given account."""
    global _engines
    if account_id not in _engines:
        _engines[account_id] = CognitiveEngine(account_id)
    return _engines[account_id]
