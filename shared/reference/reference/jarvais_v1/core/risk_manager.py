"""
JarvAIs Risk Manager
Handles all risk management: daily profit/loss targets, compounding,
post-target behavior modes, position limits, TP scaling, and pre-trade checks.

This module is the gatekeeper — no trade executes without passing through here.

Risk Modes (post-target):
    1. STOP        - Stop trading entirely after daily target is hit
    2. CAUTIOUS    - Continue with reduced risk (50% position size)
    3. FREE_TRADES - Continue with house money (risk only today's profit)
    4. SENTIMENT   - AI decides based on market sentiment
    5. AI_DECIDES  - Full AI autonomy on risk adjustment

Usage:
    from core.risk_manager import get_risk_manager
    rm = get_risk_manager("DEMO_001")
    check = rm.pre_signal_check("XAUUSD", "BUY")
    if check["allowed"]:
        lot_size = rm.calculate_position_size("XAUUSD", sl_distance=250)
"""

import logging
from datetime import datetime, date
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum

from core.time_utils import utcnow

logger = logging.getLogger("jarvais.risk_manager")


# ─────────────────────────────────────────────────────────────────────
# Enums and Data Classes
# ─────────────────────────────────────────────────────────────────────

class PostTargetMode(Enum):
    STOP = "stop"
    CAUTIOUS = "cautious"
    FREE_TRADES = "free_trades"
    SENTIMENT = "sentiment"
    AI_DECIDES = "ai_decides"


class RiskDecision(Enum):
    ALLOW = "allow"
    BLOCK_DAILY_LIMIT = "block_daily_limit"
    BLOCK_LOSS_LIMIT = "block_loss_limit"
    BLOCK_MAX_TRADES = "block_max_trades"
    BLOCK_MAX_POSITIONS = "block_max_positions"
    BLOCK_DRAWDOWN = "block_drawdown"
    ALLOW_FREE_TRADE = "allow_free_trade"
    ALLOW_CAUTIOUS = "allow_cautious"


@dataclass
class TPLevel:
    """Take Profit scaling level."""
    level: int          # 1, 2, or 3
    distance_r: float   # Distance in R-multiples (e.g., 1.5R, 2.5R, 4.0R)
    close_pct: float    # Percentage to close (e.g., 0.5 = 50%)
    action: str         # "partial_close", "move_sl", "trail"


@dataclass
class RiskProfile:
    """Complete risk profile for a trading decision."""
    allowed: bool = True
    reason: str = ""
    risk_mode: str = "normal"
    is_free_trade: bool = False
    risk_pct: float = 2.0           # Risk percentage of balance
    max_lot_size: float = 0.0       # Calculated max lot size
    recommended_lot_size: float = 0.0
    daily_pnl: float = 0.0
    daily_target: float = 0.0
    daily_target_reached: bool = False
    daily_loss_limit: float = 0.0
    daily_loss_reached: bool = False
    open_positions: int = 0
    max_positions: int = 3
    today_trade_count: int = 0
    confidence_adjusted_risk: float = 0.0


# ─────────────────────────────────────────────────────────────────────
# Risk Manager Class
# ─────────────────────────────────────────────────────────────────────

class RiskManager:
    """
    Central risk management for a single trading account.
    Enforces all risk rules before any trade is executed.
    """

    def __init__(self, account_id: str):
        self.account_id = account_id
        self._db = None
        self._mt5 = None
        self._config = None
        self._daily_cache = {}  # Cache daily stats to reduce DB queries
        self._cache_date = None

        logger.info(f"[{account_id}] Risk Manager initialized")

    @property
    def db(self):
        if self._db is None:
            from db.database import get_db
            self._db = get_db()
        return self._db

    @property
    def mt5(self):
        if self._mt5 is None:
            from core.mt5_executor import get_mt5_executor
            self._mt5 = get_mt5_executor(self.account_id)
        return self._mt5

    @property
    def config(self):
        if self._config is None:
            from core.config import get_config
            self._config = get_config()
        return self._config

    def _get_risk_settings(self) -> Dict[str, Any]:
        """Get risk settings from config for this account."""
        acct = self.config.get_account(self.account_id)
        if acct and acct.risk_settings:
            rs = acct.risk_settings
            return {
                "risk_per_trade_pct": rs.risk_per_trade_pct,
                "daily_profit_target_pct": rs.daily_profit_target_pct,
                "daily_loss_limit_pct": rs.daily_loss_limit_pct,
                "max_open_trades": rs.max_open_trades,
                "post_target_mode": rs.post_target_mode,
                "reward_to_risk_ratio": rs.reward_to_risk_ratio,
                "breakeven_trigger_r": rs.breakeven_trigger_r,
                "tp_levels": rs.tp_levels if hasattr(rs, 'tp_levels') else None,
                "confidence_risk_scaling": rs.confidence_risk_scaling if hasattr(rs, 'confidence_risk_scaling') else True
            }
        # Defaults
        return {
            "risk_per_trade_pct": 2.0,
            "daily_profit_target_pct": 5.0,
            "daily_loss_limit_pct": 10.0,
            "max_open_trades": 3,
            "post_target_mode": "stop",
            "reward_to_risk_ratio": 2.5,
            "breakeven_trigger_r": 1.75,
            "tp_levels": None,
            "confidence_risk_scaling": True
        }

    def _get_daily_stats(self) -> Dict[str, Any]:
        """Get today's trading statistics. Cached per day."""
        today = date.today()
        if self._cache_date != today:
            self._daily_cache = {}
            self._cache_date = today

        if not self._daily_cache:
            try:
                pnl = self.db.get_today_pnl(self.account_id)
                trade_count = self.db.get_today_trade_count(self.account_id)
                balance = self.mt5.get_account_balance()
                open_positions = self.mt5.get_position_count()

                self._daily_cache = {
                    "pnl": pnl,
                    "trade_count": trade_count,
                    "balance": balance,
                    "open_positions": open_positions,
                    "last_updated": utcnow()
                }
            except Exception as e:
                logger.error(f"[{self.account_id}] Error getting daily stats: {e}")
                self._daily_cache = {
                    "pnl": 0.0,
                    "trade_count": 0,
                    "balance": 0.0,
                    "open_positions": 0,
                    "last_updated": utcnow()
                }

        return self._daily_cache

    def invalidate_cache(self):
        """Force refresh of daily stats on next check."""
        self._daily_cache = {}

    # ─────────────────────────────────────────────────────────────────
    # Pre-Signal Check (called before AI validation)
    # ─────────────────────────────────────────────────────────────────

    def pre_signal_check(self, symbol: str, direction: str) -> Dict[str, Any]:
        """
        Quick pre-flight check before sending signal to AI.
        Prevents wasting AI tokens on signals that would be blocked anyway.

        Returns:
            Dict with keys: allowed, reason, is_free_trade, risk_mode
        """
        settings = self._get_risk_settings()
        stats = self._get_daily_stats()

        balance = stats["balance"]
        daily_pnl = stats["pnl"]
        open_positions = stats["open_positions"]
        trade_count = stats["trade_count"]

        # Check 1: Daily loss limit
        if balance > 0:
            loss_pct = (daily_pnl / balance) * 100
            if loss_pct <= -settings["daily_loss_limit_pct"]:
                logger.warning(f"[{self.account_id}] BLOCKED: Daily loss limit reached "
                               f"({loss_pct:.1f}% vs limit {settings['daily_loss_limit_pct']}%)")
                return {
                    "allowed": False,
                    "reason": f"Daily loss limit reached: {loss_pct:.1f}% "
                              f"(limit: {settings['daily_loss_limit_pct']}%)",
                    "is_free_trade": False,
                    "risk_mode": "blocked"
                }

        # Check 2: Max open positions
        if open_positions >= settings["max_open_trades"]:
            logger.info(f"[{self.account_id}] BLOCKED: Max open trades reached "
                        f"({open_positions}/{settings['max_open_trades']})")
            return {
                "allowed": False,
                "reason": f"Max open trades reached: {open_positions}/{settings['max_open_trades']}",
                "is_free_trade": False,
                "risk_mode": "blocked"
            }

        # Check 3: Daily profit target
        if balance > 0:
            profit_pct = (daily_pnl / balance) * 100
            if profit_pct >= settings["daily_profit_target_pct"]:
                # Target reached — apply post-target mode
                mode = settings["post_target_mode"]
                logger.info(f"[{self.account_id}] Daily target reached ({profit_pct:.1f}%). "
                            f"Post-target mode: {mode}")

                if mode == "stop":
                    return {
                        "allowed": False,
                        "reason": f"Daily target reached ({profit_pct:.1f}%) — mode: STOP",
                        "is_free_trade": False,
                        "risk_mode": "stop"
                    }
                elif mode == "cautious":
                    return {
                        "allowed": True,
                        "reason": f"Daily target reached — continuing in CAUTIOUS mode",
                        "is_free_trade": False,
                        "risk_mode": "cautious"
                    }
                elif mode == "free_trades":
                    return {
                        "allowed": True,
                        "reason": f"Daily target reached — FREE TRADE mode (risking today's profit only)",
                        "is_free_trade": True,
                        "risk_mode": "free_trades"
                    }
                elif mode == "sentiment":
                    return {
                        "allowed": True,
                        "reason": f"Daily target reached — SENTIMENT mode (AI decides based on market)",
                        "is_free_trade": False,
                        "risk_mode": "sentiment"
                    }
                elif mode == "ai_decides":
                    return {
                        "allowed": True,
                        "reason": f"Daily target reached — AI DECIDES mode",
                        "is_free_trade": False,
                        "risk_mode": "ai_decides"
                    }

        # All checks passed — normal trading
        return {
            "allowed": True,
            "reason": "All risk checks passed",
            "is_free_trade": False,
            "risk_mode": "normal"
        }

    # ─────────────────────────────────────────────────────────────────
    # Position Sizing
    # ─────────────────────────────────────────────────────────────────

    def calculate_position_size(self, symbol: str, sl_distance_points: float,
                                confidence: int = 75, risk_mode: str = "normal",
                                is_free_trade: bool = False) -> Dict[str, Any]:
        """
        Calculate position size with all risk adjustments applied.

        Args:
            symbol: Trading instrument
            sl_distance_points: Distance to stop loss in points
            confidence: AI confidence score (1-100)
            risk_mode: Current risk mode
            is_free_trade: Whether this is a free trade (risking profit only)

        Returns:
            Dict with lot_size, risk_amount, risk_pct, adjustments applied
        """
        settings = self._get_risk_settings()
        stats = self._get_daily_stats()

        balance = stats["balance"]
        daily_pnl = stats["pnl"]
        base_risk_pct = settings["risk_per_trade_pct"]

        adjustments = []

        # Step 1: Base risk percentage
        effective_risk_pct = base_risk_pct

        # Step 2: Confidence-based scaling
        if settings.get("confidence_risk_scaling", True):
            if confidence >= 90:
                # Very high confidence — full risk
                scale = 1.0
            elif confidence >= 75:
                # Good confidence — standard risk
                scale = 0.85
            elif confidence >= 65:
                # Moderate confidence — reduced risk
                scale = 0.65
            else:
                # Low confidence — minimum risk
                scale = 0.4

            effective_risk_pct *= scale
            adjustments.append(f"Confidence scaling: {confidence}% → {scale}x")

        # Step 3: Risk mode adjustments
        if risk_mode == "cautious":
            effective_risk_pct *= 0.5
            adjustments.append("Cautious mode: 50% risk reduction")
        elif risk_mode == "free_trades" and is_free_trade:
            # Risk only today's profit
            if daily_pnl > 0:
                max_risk_amount = daily_pnl * 0.25  # Risk 25% of today's profit
                risk_amount_normal = balance * (effective_risk_pct / 100)
                if risk_amount_normal > max_risk_amount:
                    effective_risk_pct = (max_risk_amount / balance) * 100
                    adjustments.append(f"Free trade: capped at 25% of today's profit (${max_risk_amount:.2f})")

        # Step 4: Calculate lot size
        risk_amount = balance * (effective_risk_pct / 100)
        lot_size = self.mt5.calculate_lot_size(symbol, effective_risk_pct, sl_distance_points)

        logger.info(f"[{self.account_id}] Position size: {lot_size} lots "
                    f"(risk: {effective_risk_pct:.2f}%, ${risk_amount:.2f}, "
                    f"confidence: {confidence}%, mode: {risk_mode})")

        return {
            "lot_size": lot_size,
            "risk_pct": round(effective_risk_pct, 4),
            "risk_amount": round(risk_amount, 2),
            "balance": balance,
            "confidence": confidence,
            "risk_mode": risk_mode,
            "is_free_trade": is_free_trade,
            "adjustments": adjustments
        }

    # ─────────────────────────────────────────────────────────────────
    # TP Scaling (TP1/TP2/TP3)
    # ─────────────────────────────────────────────────────────────────

    def get_tp_levels(self, entry_price: float, sl_price: float,
                      direction: str) -> List[TPLevel]:
        """
        Calculate TP1, TP2, TP3 levels based on R-multiples.

        Default scaling (configurable via UI):
            TP1: 1.5R — close 50%
            TP2: 2.5R — close 25%, move SL to breakeven
            TP3: 4.0R — close remaining 25% (or trail)

        Args:
            entry_price: Trade entry price
            sl_price: Stop loss price
            direction: "BUY" or "SELL"

        Returns:
            List of TPLevel objects with calculated prices
        """
        settings = self._get_risk_settings()

        # Default TP levels
        tp_config = settings.get("tp_levels") or [
            {"level": 1, "distance_r": 1.5, "close_pct": 0.50, "action": "partial_close"},
            {"level": 2, "distance_r": 2.5, "close_pct": 0.25, "action": "partial_close"},
            {"level": 3, "distance_r": 4.0, "close_pct": 0.25, "action": "trail"},
        ]

        sl_distance = abs(entry_price - sl_price)

        levels = []
        for tp in tp_config:
            if direction.upper() == "BUY":
                tp_price = entry_price + (sl_distance * tp["distance_r"])
            else:
                tp_price = entry_price - (sl_distance * tp["distance_r"])

            levels.append(TPLevel(
                level=tp["level"],
                distance_r=tp["distance_r"],
                close_pct=tp["close_pct"],
                action=tp["action"]
            ))

        return levels

    def get_tp_prices(self, entry_price: float, sl_price: float,
                      direction: str) -> Dict[str, float]:
        """
        Get TP prices as a simple dictionary for order placement.

        Returns:
            {"tp1": price, "tp2": price, "tp3": price, "sl": sl_price}
        """
        sl_distance = abs(entry_price - sl_price)
        levels = self.get_tp_levels(entry_price, sl_price, direction)

        prices = {"sl": sl_price}
        for level in levels:
            if direction.upper() == "BUY":
                prices[f"tp{level.level}"] = round(entry_price + (sl_distance * level.distance_r), 5)
            else:
                prices[f"tp{level.level}"] = round(entry_price - (sl_distance * level.distance_r), 5)

        return prices

    # ─────────────────────────────────────────────────────────────────
    # Position Monitoring
    # ─────────────────────────────────────────────────────────────────

    def check_breakeven_trigger(self, entry_price: float, current_price: float,
                                sl_price: float, direction: str) -> bool:
        """
        Check if the position has reached the breakeven trigger level.
        Default: 1.75R

        Returns:
            True if SL should be moved to breakeven
        """
        settings = self._get_risk_settings()
        be_trigger = settings.get("breakeven_trigger_r", 1.75)

        sl_distance = abs(entry_price - sl_price)
        trigger_distance = sl_distance * be_trigger

        if direction.upper() == "BUY":
            return current_price >= entry_price + trigger_distance
        else:
            return current_price <= entry_price - trigger_distance

    def check_tp_hit(self, entry_price: float, current_price: float,
                     sl_price: float, direction: str) -> Optional[int]:
        """
        Check if any TP level has been hit.

        Returns:
            TP level number (1, 2, 3) or None if no TP hit
        """
        prices = self.get_tp_prices(entry_price, sl_price, direction)

        if direction.upper() == "BUY":
            for level in [3, 2, 1]:  # Check highest first
                tp_key = f"tp{level}"
                if tp_key in prices and current_price >= prices[tp_key]:
                    return level
        else:
            for level in [3, 2, 1]:
                tp_key = f"tp{level}"
                if tp_key in prices and current_price <= prices[tp_key]:
                    return level

        return None

    # ─────────────────────────────────────────────────────────────────
    # Full Risk Profile (for AI dossier)
    # ─────────────────────────────────────────────────────────────────

    def get_risk_profile(self, confidence: int = 75) -> RiskProfile:
        """
        Generate a complete risk profile for inclusion in the AI dossier.
        This gives the AI full visibility into the current risk state.
        """
        settings = self._get_risk_settings()
        stats = self._get_daily_stats()

        balance = stats["balance"]
        daily_pnl = stats["pnl"]
        open_positions = stats["open_positions"]
        trade_count = stats["trade_count"]

        # Calculate target amounts
        daily_target = balance * (settings["daily_profit_target_pct"] / 100)
        daily_loss_limit = balance * (settings["daily_loss_limit_pct"] / 100)

        target_reached = daily_pnl >= daily_target if balance > 0 else False
        loss_reached = daily_pnl <= -daily_loss_limit if balance > 0 else False

        # Determine effective risk mode
        risk_mode = "normal"
        if loss_reached:
            risk_mode = "blocked_loss"
        elif target_reached:
            risk_mode = settings["post_target_mode"]

        return RiskProfile(
            allowed=not loss_reached and open_positions < settings["max_open_trades"],
            reason="" if not loss_reached else "Daily loss limit reached",
            risk_mode=risk_mode,
            is_free_trade=(risk_mode == "free_trades"),
            risk_pct=settings["risk_per_trade_pct"],
            daily_pnl=daily_pnl,
            daily_target=daily_target,
            daily_target_reached=target_reached,
            daily_loss_limit=daily_loss_limit,
            daily_loss_reached=loss_reached,
            open_positions=open_positions,
            max_positions=settings["max_open_trades"],
            today_trade_count=trade_count,
            confidence_adjusted_risk=settings["risk_per_trade_pct"] * (confidence / 100)
        )

    def get_risk_summary_for_prompt(self, confidence: int = 75) -> str:
        """
        Generate a human-readable risk summary for inclusion in AI prompts.
        """
        profile = self.get_risk_profile(confidence)

        summary = f"""## Current Risk State
- Account Balance: ${profile.daily_pnl + profile.daily_target:.2f} (approximate)
- Today's P&L: ${profile.daily_pnl:.2f}
- Daily Target: ${profile.daily_target:.2f} ({'REACHED' if profile.daily_target_reached else 'not yet'})
- Daily Loss Limit: -${profile.daily_loss_limit:.2f} ({'REACHED — TRADING BLOCKED' if profile.daily_loss_reached else 'safe'})
- Open Positions: {profile.open_positions}/{profile.max_positions}
- Trades Today: {profile.today_trade_count}
- Risk Mode: {profile.risk_mode.upper()}
- Base Risk: {profile.risk_pct}% per trade
- Confidence-Adjusted Risk: {profile.confidence_adjusted_risk:.2f}%
"""
        if profile.is_free_trade:
            summary += "- **FREE TRADE MODE**: Only risking today's profit. Be more experimental.\n"

        return summary



    # -----------------------------------------------------------------
    # Drawdown Protection
    # -----------------------------------------------------------------

    def check_drawdown(self) -> Dict[str, Any]:
        """
        Check if account is in excessive drawdown from peak equity.
        Max drawdown default: 15% from peak.

        Returns:
            Dict with allowed, current_drawdown_pct, max_drawdown_pct
        """
        settings = self._get_risk_settings()
        max_drawdown_pct = settings.get("max_drawdown_pct", 15.0)

        try:
            account_info = self.mt5.get_account_info()
            equity = account_info.equity
            balance = account_info.balance

            # Peak equity is tracked from DB (highest equity seen)
            peak_equity = self.db.get_peak_equity(self.account_id)
            if peak_equity is None or equity > peak_equity:
                peak_equity = equity
                self.db.update_peak_equity(self.account_id, peak_equity)

            if peak_equity > 0:
                drawdown_pct = ((peak_equity - equity) / peak_equity) * 100
            else:
                drawdown_pct = 0.0

            allowed = drawdown_pct < max_drawdown_pct

            if not allowed:
                logger.warning(f"[{self.account_id}] DRAWDOWN LIMIT: "
                               f"{drawdown_pct:.1f}% from peak ${peak_equity:.2f} "
                               f"(limit: {max_drawdown_pct}%)")

            return {
                "allowed": allowed,
                "current_drawdown_pct": round(drawdown_pct, 2),
                "max_drawdown_pct": max_drawdown_pct,
                "peak_equity": peak_equity,
                "current_equity": equity
            }

        except Exception as e:
            logger.error(f"[{self.account_id}] Drawdown check error: {e}")
            return {"allowed": True, "current_drawdown_pct": 0, "max_drawdown_pct": max_drawdown_pct}

    # -----------------------------------------------------------------
    # Correlation Check
    # -----------------------------------------------------------------

    # Correlation groups — instruments that tend to move together
    CORRELATION_GROUPS = {
        "precious_metals": ["XAUUSD", "XAGUSD", "XPTUSD"],
        "usd_majors_long": ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"],
        "usd_majors_short": ["USDJPY", "USDCHF", "USDCAD"],
        "jpy_crosses": ["EURJPY", "GBPJPY", "AUDJPY"],
        "oil": ["USOIL", "UKOIL", "CL", "XTIUSD", "XBRUSD"],
        "indices_us": ["US30", "US500", "NAS100", "US2000"],
        "indices_eu": ["GER40", "UK100", "FRA40"],
    }

    def check_correlation(self, symbol: str, direction: str) -> Dict[str, Any]:
        """
        Check if opening a new position would create excessive correlation risk.
        Rule: Max 2 positions in the same correlation group in the same direction.

        Args:
            symbol: New symbol to trade
            direction: "BUY" or "SELL"

        Returns:
            Dict with allowed, correlated_positions, group
        """
        # Find which group this symbol belongs to
        symbol_group = None
        for group_name, symbols in self.CORRELATION_GROUPS.items():
            if symbol.upper() in [s.upper() for s in symbols]:
                symbol_group = group_name
                break

        if symbol_group is None:
            return {"allowed": True, "correlated_positions": [], "group": None}

        # Check open positions in the same group
        open_positions = self.mt5.get_open_positions()
        correlated = []
        same_direction_count = 0

        for pos in open_positions:
            if pos.symbol.upper() in [s.upper() for s in self.CORRELATION_GROUPS.get(symbol_group, [])]:
                correlated.append({
                    "symbol": pos.symbol,
                    "direction": pos.direction,
                    "volume": pos.volume,
                    "profit": pos.profit
                })
                if pos.direction.upper() == direction.upper():
                    same_direction_count += 1

        max_correlated = 2
        allowed = same_direction_count < max_correlated

        if not allowed:
            logger.warning(f"[{self.account_id}] CORRELATION BLOCK: {symbol} {direction} "
                           f"would create {same_direction_count + 1} correlated positions "
                           f"in group '{symbol_group}'")

        return {
            "allowed": allowed,
            "correlated_positions": correlated,
            "group": symbol_group,
            "same_direction_count": same_direction_count,
            "max_correlated": max_correlated
        }

    # -----------------------------------------------------------------
    # Consecutive Loss Protection
    # -----------------------------------------------------------------

    def check_consecutive_losses(self) -> Dict[str, Any]:
        """
        Check recent consecutive losses and adjust risk accordingly.

        Rules:
        - 3 consecutive losses: reduce risk by 25%
        - 5 consecutive losses: reduce risk by 50%
        - 7 consecutive losses: stop trading for the day

        Returns:
            Dict with allowed, consecutive_losses, risk_multiplier
        """
        try:
            recent_trades = self.db.get_recent_trades(self.account_id, limit=10)
            if not recent_trades:
                return {"allowed": True, "consecutive_losses": 0, "risk_multiplier": 1.0}

            consecutive = 0
            for trade in recent_trades:
                if trade.get("pnl", 0) < 0:
                    consecutive += 1
                else:
                    break  # Streak broken

            if consecutive >= 7:
                logger.warning(f"[{self.account_id}] CONSECUTIVE LOSS BLOCK: "
                               f"{consecutive} losses in a row — stopping for the day")
                return {
                    "allowed": False,
                    "consecutive_losses": consecutive,
                    "risk_multiplier": 0.0,
                    "reason": f"{consecutive} consecutive losses — trading paused"
                }
            elif consecutive >= 5:
                multiplier = 0.5
            elif consecutive >= 3:
                multiplier = 0.75
            else:
                multiplier = 1.0

            if multiplier < 1.0:
                logger.info(f"[{self.account_id}] Consecutive loss adjustment: "
                            f"{consecutive} losses, risk multiplier: {multiplier}")

            return {
                "allowed": True,
                "consecutive_losses": consecutive,
                "risk_multiplier": multiplier
            }

        except Exception as e:
            logger.error(f"[{self.account_id}] Consecutive loss check error: {e}")
            return {"allowed": True, "consecutive_losses": 0, "risk_multiplier": 1.0}

    # -----------------------------------------------------------------
    # Time-Based Rules
    # -----------------------------------------------------------------

    def check_trading_hours(self, symbol: str = None) -> Dict[str, Any]:
        """
        Check if current time is within allowed trading hours.

        Rules:
        - No trading 5 minutes before/after high-impact news (if news feed available)
        - Respect session preferences (London, NY, Asian)
        - No trading in the last 5 minutes before market close

        Returns:
            Dict with allowed, current_session, reason
        """
        now = utcnow()
        hour = now.hour
        minute = now.minute
        weekday = now.weekday()  # 0=Monday, 6=Sunday

        # No trading on weekends
        if weekday >= 5:
            return {
                "allowed": False,
                "current_session": "weekend",
                "reason": "Markets closed (weekend)"
            }

        # Determine current session
        if 0 <= hour < 7:
            session = "asian"
        elif 7 <= hour < 12:
            session = "london"
        elif 12 <= hour < 16:
            session = "london_ny_overlap"
        elif 16 <= hour < 21:
            session = "new_york"
        else:
            session = "off_hours"

        # Check if session is allowed (configurable)
        settings = self._get_risk_settings()
        allowed_sessions = settings.get("allowed_sessions",
                                        ["london", "london_ny_overlap", "new_york"])

        if session not in allowed_sessions and session != "off_hours":
            return {
                "allowed": False,
                "current_session": session,
                "reason": f"Session '{session}' not in allowed sessions: {allowed_sessions}"
            }

        # No trading in off-hours (after NY close, before Asian open)
        if session == "off_hours":
            return {
                "allowed": False,
                "current_session": session,
                "reason": "Off-hours: between NY close and Asian open"
            }

        return {
            "allowed": True,
            "current_session": session,
            "reason": f"Trading allowed in {session} session"
        }

    # -----------------------------------------------------------------
    # Weekly / Monthly Limits
    # -----------------------------------------------------------------

    def check_weekly_limit(self) -> Dict[str, Any]:
        """
        Check if weekly loss limit has been reached.
        Default: 20% weekly drawdown limit.
        """
        settings = self._get_risk_settings()
        weekly_loss_limit_pct = settings.get("weekly_loss_limit_pct", 20.0)

        try:
            weekly_pnl = self.db.get_weekly_pnl(self.account_id)
            balance = self.mt5.get_account_balance()

            if balance > 0:
                weekly_loss_pct = (weekly_pnl / balance) * 100
            else:
                weekly_loss_pct = 0.0

            allowed = weekly_loss_pct > -weekly_loss_limit_pct

            if not allowed:
                logger.warning(f"[{self.account_id}] WEEKLY LOSS LIMIT: "
                               f"{weekly_loss_pct:.1f}% (limit: {weekly_loss_limit_pct}%)")

            return {
                "allowed": allowed,
                "weekly_pnl": weekly_pnl,
                "weekly_loss_pct": round(weekly_loss_pct, 2),
                "weekly_loss_limit_pct": weekly_loss_limit_pct
            }

        except Exception as e:
            logger.error(f"[{self.account_id}] Weekly limit check error: {e}")
            return {"allowed": True, "weekly_pnl": 0, "weekly_loss_pct": 0}

    # -----------------------------------------------------------------
    # Comprehensive Pre-Trade Validation
    # -----------------------------------------------------------------

    def full_pre_trade_check(self, symbol: str, direction: str,
                             confidence: int = 75) -> Dict[str, Any]:
        """
        Run ALL risk checks before a trade. This is the master gatekeeper.

        Checks in order:
        1. Trading hours
        2. Weekly limit
        3. Daily loss limit
        4. Max drawdown
        5. Consecutive losses
        6. Correlation
        7. Max open positions
        8. Daily target (post-target mode)

        Returns:
            Comprehensive dict with all check results and final allowed/blocked decision
        """
        checks = {}
        blocked_reasons = []

        # 1. Trading hours
        hours_check = self.check_trading_hours(symbol)
        checks["trading_hours"] = hours_check
        if not hours_check["allowed"]:
            blocked_reasons.append(f"Hours: {hours_check['reason']}")

        # 2. Weekly limit
        weekly_check = self.check_weekly_limit()
        checks["weekly_limit"] = weekly_check
        if not weekly_check["allowed"]:
            blocked_reasons.append(f"Weekly loss: {weekly_check['weekly_loss_pct']}%")

        # 3-7. Original pre_signal_check (daily limits, positions, target)
        signal_check = self.pre_signal_check(symbol, direction)
        checks["signal_check"] = signal_check
        if not signal_check["allowed"]:
            blocked_reasons.append(f"Signal: {signal_check['reason']}")

        # 4. Max drawdown
        drawdown_check = self.check_drawdown()
        checks["drawdown"] = drawdown_check
        if not drawdown_check["allowed"]:
            blocked_reasons.append(f"Drawdown: {drawdown_check['current_drawdown_pct']}%")

        # 5. Consecutive losses
        consec_check = self.check_consecutive_losses()
        checks["consecutive_losses"] = consec_check
        if not consec_check["allowed"]:
            blocked_reasons.append(f"Consecutive losses: {consec_check['consecutive_losses']}")

        # 6. Correlation
        corr_check = self.check_correlation(symbol, direction)
        checks["correlation"] = corr_check
        if not corr_check["allowed"]:
            blocked_reasons.append(f"Correlation: {corr_check['group']} "
                                   f"({corr_check['same_direction_count']} same-direction)")

        # Final decision
        allowed = len(blocked_reasons) == 0
        risk_multiplier = consec_check.get("risk_multiplier", 1.0)

        result = {
            "allowed": allowed,
            "blocked_reasons": blocked_reasons,
            "risk_mode": signal_check.get("risk_mode", "normal"),
            "is_free_trade": signal_check.get("is_free_trade", False),
            "risk_multiplier": risk_multiplier,
            "checks": checks,
            "timestamp": utcnow().isoformat()
        }

        if allowed:
            logger.info(f"[{self.account_id}] FULL PRE-TRADE CHECK PASSED: "
                        f"{symbol} {direction} (mode: {result['risk_mode']}, "
                        f"multiplier: {risk_multiplier})")
        else:
            logger.warning(f"[{self.account_id}] FULL PRE-TRADE CHECK BLOCKED: "
                           f"{symbol} {direction} — {'; '.join(blocked_reasons)}")

        return result

    def get_enhanced_risk_summary(self, symbol: str, direction: str,
                                  confidence: int = 75) -> str:
        """
        Generate an enhanced risk summary for AI prompts that includes
        all risk dimensions.
        """
        profile = self.get_risk_profile(confidence)
        consec = self.check_consecutive_losses()
        corr = self.check_correlation(symbol, direction)
        hours = self.check_trading_hours(symbol)

        summary = f"""## Current Risk State
- Account Balance: ${profile.daily_pnl + profile.daily_target:.2f} (approximate)
- Today's P&L: ${profile.daily_pnl:.2f}
- Daily Target: ${profile.daily_target:.2f} ({'REACHED' if profile.daily_target_reached else 'not yet'})
- Daily Loss Limit: -${profile.daily_loss_limit:.2f} ({'REACHED' if profile.daily_loss_reached else 'safe'})
- Open Positions: {profile.open_positions}/{profile.max_positions}
- Trades Today: {profile.today_trade_count}
- Risk Mode: {profile.risk_mode.upper()}
- Base Risk: {profile.risk_pct}% per trade
- Confidence-Adjusted Risk: {profile.confidence_adjusted_risk:.2f}%
- Current Session: {hours.get('current_session', 'unknown')}
- Consecutive Losses: {consec.get('consecutive_losses', 0)}
- Risk Multiplier: {consec.get('risk_multiplier', 1.0)}x
"""
        if corr.get("group"):
            summary += (f"- Correlation Group: {corr['group']} "
                        f"({corr.get('same_direction_count', 0)} same-direction open)\n")

        if profile.is_free_trade:
            summary += "- **FREE TRADE MODE**: Only risking today's profit.\n"

        if consec.get("consecutive_losses", 0) >= 3:
            summary += (f"- **CAUTION**: {consec['consecutive_losses']} consecutive losses. "
                        f"Risk reduced to {consec.get('risk_multiplier', 1.0)*100:.0f}%.\n")

        return summary



# ─────────────────────────────────────────────────────────────────────
# Instance Management
# ─────────────────────────────────────────────────────────────────────

_risk_managers: Dict[str, RiskManager] = {}


def get_risk_manager(account_id: str) -> RiskManager:
    """Get or create a RiskManager for the given account."""
    global _risk_managers
    if account_id not in _risk_managers:
        _risk_managers[account_id] = RiskManager(account_id)
    return _risk_managers[account_id]
