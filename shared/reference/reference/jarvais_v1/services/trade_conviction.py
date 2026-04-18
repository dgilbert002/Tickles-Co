"""
Trade Conviction Engine (TCE) — 5-layer holistic scoring for shadow trades.

TCS = (L0 * 0.25) + (L1 * 0.30) + (L2 * 0.20) + (L3 * 0.15) + (L4 * 0.10)

Each layer returns 0-100. Layer 0 has a hard gate: grade F = TCS forced to 0.
Stateless: designed for use in shadow_queue ranking and live trade evaluation.
"""

import logging
import math
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trade_conviction")


class TradeConvictionEngine:
    """Five-layer additive weighted scoring engine for trade quality assessment."""

    WEIGHTS = {
        "symbol_character": 0.25,
        "setup_quality": 0.30,
        "market_context": 0.20,
        "track_record": 0.15,
        "external_validation": 0.10,
    }

    def evaluate(self, symbol: str, direction: str,
                 entry_price: float, stop_loss: float, take_profit_1: float,
                 confidence: int = 0, model_used: str = None,
                 symbol_intel: dict = None, candle_data: dict = None,
                 market_score: float = None, market_label: str = None,
                 enrichment: dict = None,
                 manus_data: dict = None,
                 recent_trades: list = None,
                 mentor_corroboration: bool = False,
                 cts_score: float = None, cts_grade: str = None,
                 config: dict = None) -> Dict[str, Any]:
        """Evaluate a trade setup and return Trade Conviction Score (TCS).

        Returns dict with: tcs (0-100), verdict, sub_scores, reasoning,
        gates_failed, circuit_breaker (bool)
        """
        cfg = config or {}
        direction = (direction or "").upper()
        sub_scores = {}
        reasoning = []
        gates_failed = []

        # ── Layer 0: Symbol Character (25%) ───────────────────────────
        l0 = self._layer_0_symbol_character(
            cts_score=cts_score, cts_grade=cts_grade,
            symbol_intel=symbol_intel)
        sub_scores["symbol_character"] = l0

        if (cts_grade or "").upper() == "F":
            gates_failed.append("L0_HARD_GATE: CTS grade F")
            reasoning.append("HARD GATE: Symbol has grade F chart tradeability — TCS forced to 0")
            return {
                "tcs": 0.0,
                "verdict": "reject",
                "sub_scores": sub_scores,
                "reasoning": reasoning,
                "gates_failed": gates_failed,
            }

        # ── Layer 1: Setup Quality (30%) ──────────────────────────────
        l1 = self._layer_1_setup_quality(
            entry_price=entry_price, stop_loss=stop_loss,
            take_profit_1=take_profit_1, confidence=confidence,
            direction=direction)
        sub_scores["setup_quality"] = l1

        # ── Layer 2: Market Context (20%) ─────────────────────────────
        l2 = self._layer_2_market_context(
            direction=direction, market_score=market_score,
            market_label=market_label, enrichment=enrichment,
            manus_data=manus_data)
        sub_scores["market_context"] = l2

        # ── Layer 3: Track Record (15%) ───────────────────────────────
        l3 = self._layer_3_track_record(
            symbol=symbol, direction=direction,
            recent_trades=recent_trades)
        sub_scores["track_record"] = l3

        # ── Layer 4: External Validation (10%) ────────────────────────
        l4 = self._layer_4_external_validation(
            mentor_corroboration=mentor_corroboration,
            symbol_intel=symbol_intel, direction=direction,
            manus_data=manus_data)
        sub_scores["external_validation"] = l4

        # ── Circuit Breaker (Layer 5 protection) ──────────────────────
        circuit_breaker = False
        if manus_data:
            mc = manus_data.get("market_context", {})
            ls_ratio = mc.get("hyperliquid_ls_ratio")
            if ls_ratio is not None:
                try:
                    ls = float(ls_ratio)
                    if ls > 3.0 and direction == "BUY":
                        circuit_breaker = True
                        gates_failed.append(
                            f"MANUS_SQUEEZE: L/S ratio {ls:.2f} > 3.0 — "
                            f"blocking BUY (extreme long crowding)")
                    elif ls < 0.33 and direction == "SELL":
                        circuit_breaker = True
                        gates_failed.append(
                            f"MANUS_SQUEEZE: L/S ratio {ls:.2f} < 0.33 — "
                            f"blocking SELL (extreme short crowding)")
                except (TypeError, ValueError):
                    pass

        # ── Weighted Composite ────────────────────────────────────────
        if circuit_breaker:
            tcs = 0.0
            verdict = "blocked"
            reasoning.append("CIRCUIT BREAKER: Trade blocked by Manus squeeze protection")
        else:
            tcs = sum(sub_scores.get(k, 50) * w for k, w in self.WEIGHTS.items())
            if math.isnan(tcs) or math.isinf(tcs):
                tcs = 0.0
            tcs = round(max(0.0, min(100.0, tcs)), 2)

            if tcs >= 75:
                verdict = "strong"
            elif tcs >= 50:
                verdict = "moderate"
            elif tcs >= 25:
                verdict = "weak"
            else:
                verdict = "reject"

        reasoning.append(f"TCS={tcs} ({verdict}): "
                         f"L0={l0:.0f} L1={l1:.0f} L2={l2:.0f} L3={l3:.0f} L4={l4:.0f}")

        return {
            "tcs": tcs,
            "verdict": verdict,
            "sub_scores": sub_scores,
            "reasoning": reasoning,
            "gates_failed": gates_failed,
            "circuit_breaker": circuit_breaker,
        }

    # ── Layer Implementations ─────────────────────────────────────────

    def _layer_0_symbol_character(self, cts_score=None, cts_grade=None,
                                   symbol_intel=None) -> float:
        """CTS chart quality + historical P&L for this symbol."""
        score = 50.0

        if cts_score is not None:
            score = float(cts_score)
        elif symbol_intel and symbol_intel.get("cts_score"):
            try:
                score = float(symbol_intel["cts_score"])
            except (TypeError, ValueError):
                pass

        if symbol_intel:
            win_rate = symbol_intel.get("shadow_win_rate")
            if win_rate is not None:
                try:
                    wr = float(win_rate)
                    if wr >= 60:
                        score = min(100, score + 10)
                    elif wr <= 30:
                        score = max(0, score - 15)
                except (TypeError, ValueError):
                    pass

        return max(0.0, min(100.0, score))

    def _layer_1_setup_quality(self, entry_price=0, stop_loss=0,
                                take_profit_1=0, confidence=0,
                                direction="BUY") -> float:
        """R:R ratio, confidence, entry quality."""
        score = 0.0

        # R:R calculation
        risk = abs(entry_price - stop_loss) if entry_price and stop_loss else 0
        reward = abs(take_profit_1 - entry_price) if take_profit_1 and entry_price else 0

        if risk > 0:
            rr = reward / risk
            if rr >= 4.0:
                score += 40
            elif rr >= 3.0:
                score += 35
            elif rr >= 2.0:
                score += 25
            elif rr >= 1.5:
                score += 15
            else:
                score += 5
        else:
            score += 10

        # Confidence score
        conf = max(0, min(100, confidence or 0))
        if conf >= 80:
            score += 35
        elif conf >= 65:
            score += 25
        elif conf >= 50:
            score += 15
        else:
            score += 5

        # Direction validity
        if direction in ("BUY", "SELL"):
            score += 10

        # SL sanity (not too tight, not too wide)
        if risk > 0 and entry_price > 0:
            sl_pct = (risk / entry_price) * 100
            if 0.5 <= sl_pct <= 5.0:
                score += 15
            elif 0.2 <= sl_pct <= 8.0:
                score += 8

        return max(0.0, min(100.0, score))

    def _layer_2_market_context(self, direction="BUY", market_score=None,
                                 market_label=None, enrichment=None,
                                 manus_data=None) -> float:
        """Regime alignment, funding rate, OI, L/S ratio, Manus crowd positioning."""
        score = 50.0

        if market_score is not None:
            try:
                ms = float(market_score)
                if math.isnan(ms):
                    ms = 0.0
            except (ValueError, TypeError):
                ms = 0.0

            if direction == "BUY":
                if ms >= 30:
                    score += 25
                elif ms >= 0:
                    score += 10
                elif ms >= -30:
                    score -= 10
                else:
                    score -= 30
            elif direction == "SELL":
                if ms <= -30:
                    score += 25
                elif ms <= 0:
                    score += 10
                elif ms <= 30:
                    score -= 10
                else:
                    score -= 30

        if enrichment:
            fr = enrichment.get("funding_rate")
            if fr is not None:
                try:
                    fr_val = float(fr)
                    if direction == "BUY" and fr_val > 0.0001:
                        score -= 10
                    elif direction == "SELL" and fr_val < -0.0001:
                        score -= 10
                    elif direction == "BUY" and fr_val < -0.0001:
                        score += 5
                    elif direction == "SELL" and fr_val > 0.0001:
                        score += 5
                except (TypeError, ValueError):
                    pass

            ls_ratio = enrichment.get("long_short_ratio")
            if ls_ratio is not None:
                try:
                    ls = float(ls_ratio)
                    if direction == "BUY" and ls > 2.5:
                        score -= 15
                    elif direction == "BUY" and ls > 2.0:
                        score -= 10
                    elif direction == "SELL" and ls < 0.4:
                        score -= 15
                    elif direction == "SELL" and ls < 0.5:
                        score -= 10
                except (TypeError, ValueError):
                    pass

        # Manus crowd positioning signals
        if manus_data:
            mc = manus_data.get("market_context", {})

            avg_rsi = mc.get("average_crypto_rsi")
            if avg_rsi is not None:
                try:
                    rsi = float(avg_rsi)
                    if rsi < 30 and direction == "BUY":
                        score += 8  # Oversold market, potential bounce
                    elif rsi > 70 and direction == "SELL":
                        score += 8  # Overbought market, potential drop
                    elif rsi < 30 and direction == "SELL":
                        score -= 5  # Shorting into oversold
                    elif rsi > 70 and direction == "BUY":
                        score -= 5  # Buying into overbought
                except (TypeError, ValueError):
                    pass

            liq_bias = mc.get("btc_liquidation_cluster_bias")
            if liq_bias:
                if liq_bias == "below" and direction == "BUY":
                    score -= 5  # Liquidation magnet is down
                elif liq_bias == "above" and direction == "SELL":
                    score -= 5  # Liquidation magnet is up
                elif liq_bias == "below" and direction == "SELL":
                    score += 3  # Aligned with liquidation magnet
                elif liq_bias == "above" and direction == "BUY":
                    score += 3

        return max(0.0, min(100.0, score))

    def _layer_3_track_record(self, symbol="", direction="",
                               recent_trades=None) -> float:
        """Recent trade performance for this symbol and direction."""
        if not recent_trades:
            return 50.0

        score = 50.0
        wins = 0
        losses = 0
        recent_pnl = 0.0
        direction_wins = 0
        direction_trades = 0
        consecutive_losses = 0
        max_consecutive_losses = 0

        for t in recent_trades:
            pnl = float(t.get("realised_pnl") or t.get("pnl") or 0)
            t_dir = (t.get("direction") or "").upper()

            if pnl > 0:
                wins += 1
                consecutive_losses = 0
            elif pnl < 0:
                losses += 1
                consecutive_losses += 1
                max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)

            recent_pnl += pnl

            if t_dir == direction:
                direction_trades += 1
                if pnl > 0:
                    direction_wins += 1

        total = wins + losses
        if total > 0:
            win_rate = wins / total
            if win_rate >= 0.6:
                score += 15
            elif win_rate >= 0.4:
                score += 5
            else:
                score -= 15

        if direction_trades >= 3:
            dir_wr = direction_wins / direction_trades
            if dir_wr >= 0.6:
                score += 10
            elif dir_wr < 0.3:
                score -= 15

        if max_consecutive_losses >= 5:
            score -= 20
        elif max_consecutive_losses >= 3:
            score -= 10

        if recent_pnl < 0:
            score -= 5

        return max(0.0, min(100.0, score))

    def _layer_4_external_validation(self, mentor_corroboration=False,
                                      symbol_intel=None, direction="BUY",
                                      manus_data=None) -> float:
        """Mentor agreement, Grok/Manus consensus, per-coin Manus intel."""
        score = 50.0

        if mentor_corroboration:
            score += 30

        if symbol_intel:
            grok_dir = (symbol_intel.get("grok_direction") or "").upper()
            try:
                grok_conf = int(symbol_intel.get("grok_confidence") or 0)
            except (ValueError, TypeError):
                grok_conf = 0
            if grok_dir and grok_conf >= 70:
                score += 15
            elif grok_dir and grok_conf >= 50:
                score += 5

            # LLM consensus bonus (Grok + Manus agree)
            consensus = symbol_intel.get("llm_consensus")
            if consensus:
                score += 10

            # Manus per-coin OI and funding rate checks
            oi_change = symbol_intel.get("manus_oi_change_24h")
            fr_binance = symbol_intel.get("manus_funding_rate_binance")

            if oi_change is not None and fr_binance is not None:
                try:
                    oi = float(oi_change)
                    fr = float(fr_binance)
                    # BUY into high OI surge + positive funding = squeeze risk
                    if direction == "BUY" and oi > 20 and fr > 0.01:
                        score -= 20
                    # SELL into OI dump + negative funding = squeeze risk
                    elif direction == "SELL" and oi < -20 and fr < -0.01:
                        score -= 20
                    elif oi > 30:
                        score -= 10  # Extreme OI surge is risky either way
                except (TypeError, ValueError):
                    pass

        return max(0.0, min(100.0, score))
