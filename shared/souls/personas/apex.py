"""
Apex Soul — the senior decision agent.

Apex aggregates:
  * active guardrails (crash protection status)
  * active events (macro windows, maintenance)
  * regime label (bull / bear / crash / ...)
  * treasury decision (approve / reject from capabilities + sizing)
  * proposal from downstream souls / strategies

and produces ``approve`` / ``reject`` / ``defer`` verdicts with a
plain-English rationale. The logic is deterministic so backtests and
live flows produce byte-identical verdicts for identical inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from shared.souls.protocol import (
    MODE_DETERMINISTIC,
    ROLE_DECISION,
    SOUL_APEX,
    SoulContext,
    SoulDecision,
    VERDICT_APPROVE,
    VERDICT_DEFER,
    VERDICT_REJECT,
)


@dataclass
class ApexSoul:
    name: str = SOUL_APEX
    role: str = ROLE_DECISION
    mode: str = MODE_DETERMINISTIC

    # Thresholds; exposed so tests / operators can override.
    min_confidence_for_approve: float = 0.3
    reject_regimes: tuple = ("crash",)

    # ------------------------------------------------------------------

    def decide(self, context: SoulContext) -> SoulDecision:
        fields = context.fields or {}
        reasons: List[str] = []
        score = 0.0

        guardrails_blockers: List[Dict[str, Any]] = fields.get("guardrails_blockers") or []
        if guardrails_blockers:
            reasons.append(
                f"guardrails blocking ({len(guardrails_blockers)} rule(s) triggered)"
            )
            return self._build_decision(
                context, VERDICT_REJECT,
                confidence=1.0, rationale=";".join(reasons),
            )

        active_events: List[Dict[str, Any]] = fields.get("active_events") or []
        high_importance = [e for e in active_events if int(e.get("importance", 0)) >= 3]
        if high_importance:
            reasons.append(
                f"high-importance event active: "
                f"{high_importance[0].get('name', '?')}"
            )
            return self._build_decision(
                context, VERDICT_DEFER,
                confidence=0.9, rationale=";".join(reasons),
            )

        regime = (fields.get("regime") or "unknown").lower()
        if regime in self.reject_regimes:
            reasons.append(f"regime is '{regime}'")
            return self._build_decision(
                context, VERDICT_REJECT,
                confidence=0.95, rationale=";".join(reasons),
            )
        if regime == "bull":
            score += 0.5
            reasons.append("bull regime")
        elif regime == "bear":
            score -= 0.2
            reasons.append("bear regime (penalty)")
        elif regime == "sideways":
            score += 0.1
            reasons.append("sideways regime")
        else:
            reasons.append(f"regime '{regime}' neutral")

        treasury = fields.get("treasury_decision") or {}
        if treasury.get("approved") is False:
            msg = treasury.get("reason") or "treasury rejected"
            reasons.append(f"treasury: {msg}")
            return self._build_decision(
                context, VERDICT_REJECT,
                confidence=0.9, rationale=";".join(reasons),
            )
        if treasury.get("approved") is True:
            score += 0.3
            reasons.append("treasury approved")

        prop_score = float(fields.get("proposal_score") or 0.0)
        if prop_score:
            score += prop_score
            reasons.append(f"proposal_score={prop_score:.2f}")

        verdict = (
            VERDICT_APPROVE if score >= self.min_confidence_for_approve
            else VERDICT_DEFER
        )
        return self._build_decision(
            context, verdict,
            confidence=round(min(max(score, 0.0), 1.0), 4),
            rationale=";".join(reasons) or "no strong signals",
        )

    # ------------------------------------------------------------------

    def _build_decision(
        self, context: SoulContext, verdict: str,
        *, confidence: float, rationale: str,
        outputs: Optional[Dict[str, Any]] = None,
    ) -> SoulDecision:
        return SoulDecision(
            persona_name=self.name,
            verdict=verdict,
            confidence=float(confidence),
            rationale=rationale,
            outputs=outputs or {},
            metadata={"thresholds": {
                "min_confidence_for_approve": self.min_confidence_for_approve,
                "reject_regimes": list(self.reject_regimes),
            }},
            mode=self.mode,
            correlation_id=context.correlation_id,
            company_id=context.company_id,
        )


__all__ = ["ApexSoul"]
