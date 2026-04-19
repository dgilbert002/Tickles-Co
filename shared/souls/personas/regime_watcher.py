"""
RegimeWatcher Soul — regime-transition alerter.

Given a chronological series of regime observations, it detects
transitions (e.g. sideways -> bear, bull -> crash) and produces an
``alert`` verdict when the latest observation differs from the prior
one. Output includes the from/to labels and the transition timestamp,
which callers persist in :table:`public.regime_transitions`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.souls.protocol import (
    MODE_DETERMINISTIC,
    ROLE_REGIME_WATCHER,
    SOUL_REGIME_WATCHER,
    SoulContext,
    SoulDecision,
    VERDICT_ALERT,
    VERDICT_OBSERVE,
)


@dataclass
class RegimeWatcherSoul:
    name: str = SOUL_REGIME_WATCHER
    role: str = ROLE_REGIME_WATCHER
    mode: str = MODE_DETERMINISTIC

    crash_regimes: tuple = ("crash",)

    # ------------------------------------------------------------------

    def decide(self, context: SoulContext) -> SoulDecision:
        fields = context.fields or {}
        observations: List[Dict[str, Any]] = list(fields.get("observations") or [])
        cleaned = [o for o in observations if o.get("regime")]
        cleaned.sort(key=lambda o: self._ts(o.get("ts")))

        if len(cleaned) < 2:
            return self._obs(context, "insufficient observations")

        transitions: List[Dict[str, Any]] = []
        prev = cleaned[0]
        for current in cleaned[1:]:
            pr = (prev.get("regime") or "").lower()
            cur = (current.get("regime") or "").lower()
            if cur != pr:
                transitions.append({
                    "from_regime": pr,
                    "to_regime": cur,
                    "transitioned_at": self._iso(current.get("ts")),
                    "confidence": float(current.get("confidence") or 0.0),
                    "crash": cur in self.crash_regimes,
                })
            prev = current

        if not transitions:
            return self._obs(context, "no transition detected")

        latest = transitions[-1]
        verdict = VERDICT_ALERT
        severity = "high" if latest["crash"] else (
            "medium" if latest["to_regime"] == "bear" else "low"
        )
        rationale = (
            f"transition {latest['from_regime']!r} -> {latest['to_regime']!r} "
            f"at {latest['transitioned_at']} ({len(transitions)} total)"
        )
        return SoulDecision(
            persona_name=self.name,
            verdict=verdict,
            confidence=min(1.0, latest["confidence"] or 0.8),
            rationale=rationale,
            outputs={"transitions": transitions, "latest": latest,
                     "severity": severity},
            metadata={"crash_regimes": list(self.crash_regimes)},
            mode=self.mode,
            correlation_id=context.correlation_id,
            company_id=context.company_id,
        )

    # ------------------------------------------------------------------

    def _obs(self, context: SoulContext, rationale: str) -> SoulDecision:
        return SoulDecision(
            persona_name=self.name,
            verdict=VERDICT_OBSERVE,
            confidence=1.0,
            rationale=rationale,
            outputs={"transitions": []},
            metadata={},
            mode=self.mode,
            correlation_id=context.correlation_id,
            company_id=context.company_id,
        )

    @staticmethod
    def _ts(value: Any) -> datetime:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                pass
        return datetime.min

    @staticmethod
    def _iso(value: Any) -> Optional[str]:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str):
            return value
        return None


__all__ = ["RegimeWatcherSoul"]
