"""
Quant Soul — research / hypothesis generator.

Given a bundle of market state (regime, funding, recent features), it
proposes a trade hypothesis: direction, conviction, suggested size
bucket, and an invalidation condition. The output is ``propose`` (so
Apex can decide) or ``observe`` (when no opportunity).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from shared.souls.protocol import (
    MODE_DETERMINISTIC,
    ROLE_RESEARCH,
    SOUL_QUANT,
    SoulContext,
    SoulDecision,
    VERDICT_OBSERVE,
    VERDICT_PROPOSE,
)


@dataclass
class QuantSoul:
    name: str = SOUL_QUANT
    role: str = ROLE_RESEARCH
    mode: str = MODE_DETERMINISTIC

    # Tunables
    trend_threshold: float = 0.2         # abs(trend_score) above this = strong
    volatility_threshold: float = 0.04   # above = volatile

    # ------------------------------------------------------------------

    def decide(self, context: SoulContext) -> SoulDecision:
        fields = context.fields or {}
        regime = (fields.get("regime") or "unknown").lower()
        trend = float(fields.get("trend_score") or 0.0)
        vol = float(fields.get("volatility") or 0.0)
        funding = float(fields.get("funding_rate") or 0.0)

        reasons: List[str] = []
        direction: Optional[str] = None
        conviction = 0.0

        if abs(trend) >= self.trend_threshold:
            direction = "long" if trend > 0 else "short"
            conviction += min(abs(trend), 1.0) * 0.6
            reasons.append(f"trend_score={trend:+.3f}")
        if regime == "bull":
            conviction += 0.2
            direction = direction or "long"
            reasons.append("regime bull")
        elif regime == "bear":
            conviction += 0.2
            direction = direction or "short"
            reasons.append("regime bear")
        elif regime == "crash":
            return self._obs(context, "regime crash, stepping back", conviction=0.1)

        # Funding signal: positive funding rewards shorts, negative rewards longs.
        if direction == "long" and funding < -0.0005:
            conviction += 0.1
            reasons.append(f"funding_rate={funding:+.4f} favourable for long")
        elif direction == "short" and funding > 0.0005:
            conviction += 0.1
            reasons.append(f"funding_rate={funding:+.4f} favourable for short")

        if vol >= self.volatility_threshold:
            conviction *= 0.7  # penalise in high-vol
            reasons.append(f"volatility={vol:.4f} high (conviction scaled)")

        if direction is None or conviction < 0.2:
            return self._obs(context, "insufficient edge", conviction=conviction)

        bucket = (
            "small" if conviction < 0.4
            else "medium" if conviction < 0.7
            else "large"
        )
        outputs: Dict[str, Any] = {
            "direction": direction,
            "size_bucket": bucket,
            "invalidate_if": {
                "regime_in": ["crash"],
                "volatility_above": self.volatility_threshold * 1.5,
            },
        }
        return SoulDecision(
            persona_name=self.name,
            verdict=VERDICT_PROPOSE,
            confidence=round(min(conviction, 1.0), 4),
            rationale=";".join(reasons),
            outputs=outputs,
            metadata={"thresholds": {
                "trend": self.trend_threshold,
                "volatility": self.volatility_threshold,
            }},
            mode=self.mode,
            correlation_id=context.correlation_id,
            company_id=context.company_id,
        )

    # ------------------------------------------------------------------

    def _obs(self, context: SoulContext, rationale: str, conviction: float) -> SoulDecision:
        return SoulDecision(
            persona_name=self.name,
            verdict=VERDICT_OBSERVE,
            confidence=round(min(conviction, 1.0), 4),
            rationale=rationale,
            outputs={},
            metadata={},
            mode=self.mode,
            correlation_id=context.correlation_id,
            company_id=context.company_id,
        )


__all__ = ["QuantSoul"]
