"""
Scout Soul — universe expansion / symbol discovery.

Scout takes a bundle of observations (candidate symbols with volume,
volatility, social_mentions, funding) and scores them against a
threshold. Anything above threshold is proposed with verdict
``propose``; everything else is ``observe``. Deterministic so the
same observation list always ranks identically.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from shared.souls.protocol import (
    MODE_DETERMINISTIC,
    ROLE_SCOUT,
    SOUL_SCOUT,
    SoulContext,
    SoulDecision,
    VERDICT_OBSERVE,
    VERDICT_PROPOSE,
)


@dataclass
class ScoutSoul:
    name: str = SOUL_SCOUT
    role: str = ROLE_SCOUT
    mode: str = MODE_DETERMINISTIC

    min_score: float = 0.3
    max_proposals: int = 10
    volume_weight: float = 0.4
    volatility_weight: float = 0.2
    mentions_weight: float = 0.3
    funding_weight: float = 0.1

    # ------------------------------------------------------------------

    def decide(self, context: SoulContext) -> SoulDecision:
        fields = context.fields or {}
        observations: List[Dict[str, Any]] = list(fields.get("observations") or [])
        existing = set(fields.get("existing_symbols") or [])

        scored: List[Dict[str, Any]] = []
        for obs in observations:
            symbol = obs.get("symbol")
            if not symbol or symbol in existing:
                continue
            volume = self._norm(obs.get("volume_usd"), cap=1e9)
            vol = self._norm(obs.get("volatility"), cap=0.2)
            mentions = self._norm(obs.get("social_mentions"), cap=10_000)
            funding = abs(float(obs.get("funding_rate") or 0.0)) * 100.0
            score = (
                volume * self.volume_weight
                + vol * self.volatility_weight
                + mentions * self.mentions_weight
                + min(funding, 1.0) * self.funding_weight
            )
            if score >= self.min_score:
                scored.append({
                    "symbol": symbol,
                    "exchange": obs.get("exchange"),
                    "score": round(score, 4),
                    "components": {
                        "volume": round(volume, 4),
                        "volatility": round(vol, 4),
                        "mentions": round(mentions, 4),
                        "funding_abs": round(min(funding, 1.0), 4),
                    },
                })
        scored.sort(key=lambda r: (-r["score"], r["symbol"]))
        proposals = scored[: self.max_proposals]
        verdict = VERDICT_PROPOSE if proposals else VERDICT_OBSERVE
        rationale = (
            f"{len(proposals)} candidate(s) above threshold {self.min_score}"
            if proposals else
            f"no candidate above threshold {self.min_score} "
            f"(observed={len(observations)})"
        )
        return SoulDecision(
            persona_name=self.name,
            verdict=verdict,
            confidence=round(min(1.0, 0.5 + len(proposals) / max(self.max_proposals, 1) * 0.5), 4),
            rationale=rationale,
            outputs={"proposals": proposals},
            metadata={"thresholds": {"min_score": self.min_score}},
            mode=self.mode,
            correlation_id=context.correlation_id,
            company_id=context.company_id,
        )

    @staticmethod
    def _norm(value: Any, *, cap: float) -> float:
        try:
            v = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if cap <= 0:
            return 0.0
        return max(0.0, min(1.0, v / cap))


__all__ = ["ScoutSoul"]
