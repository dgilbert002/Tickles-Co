"""
Curiosity Soul — exploration policy.

Given a list of past experiments (each with a ``key`` and a pass/fail
outcome) and a list of untested candidates, Curiosity proposes the
next experiment to run based on a novelty score. It's a deterministic
UCB1-style exploration: candidates with fewer trials bubble to the
top; tie-breakers go to those with higher "prior" interest.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from shared.souls.protocol import (
    MODE_DETERMINISTIC,
    ROLE_EXPLORER,
    SOUL_CURIOSITY,
    SoulContext,
    SoulDecision,
    VERDICT_EXPLORE,
    VERDICT_OBSERVE,
)


@dataclass
class CuriositySoul:
    name: str = SOUL_CURIOSITY
    role: str = ROLE_EXPLORER
    mode: str = MODE_DETERMINISTIC

    max_picks: int = 5
    novelty_floor: float = 0.1

    # ------------------------------------------------------------------

    def decide(self, context: SoulContext) -> SoulDecision:
        fields = context.fields or {}
        history: List[Dict[str, Any]] = list(fields.get("history") or [])
        candidates: List[Dict[str, Any]] = list(fields.get("candidates") or [])

        trials: Dict[str, int] = {}
        passes: Dict[str, int] = {}
        for row in history:
            key = str(row.get("key") or "")
            if not key:
                continue
            trials[key] = trials.get(key, 0) + 1
            if row.get("success"):
                passes[key] = passes.get(key, 0) + 1

        scored: List[Dict[str, Any]] = []
        for cand in candidates:
            key = str(cand.get("key") or "")
            if not key:
                continue
            n = trials.get(key, 0)
            s = passes.get(key, 0)
            prior = float(cand.get("prior") or 0.5)
            novelty = 1.0 / (n + 1)
            hit_rate = (s + 1) / (n + 2)  # Laplace-smoothed
            score = 0.7 * novelty + 0.2 * hit_rate + 0.1 * prior
            scored.append({
                "key": key,
                "score": round(score, 4),
                "trials": n,
                "passes": s,
                "novelty": round(novelty, 4),
                "prior": round(prior, 4),
                "params": cand.get("params") or {},
            })
        scored.sort(key=lambda r: (-r["score"], r["key"]))
        picks = [
            r for r in scored if r["score"] >= self.novelty_floor
        ][: self.max_picks]
        verdict = VERDICT_EXPLORE if picks else VERDICT_OBSERVE
        rationale = (
            f"exploring {len(picks)} candidate(s) from {len(candidates)} "
            f"(history={len(history)})"
        ) if picks else (
            f"no candidate above novelty floor {self.novelty_floor}"
        )
        return SoulDecision(
            persona_name=self.name,
            verdict=verdict,
            confidence=round(min(1.0, 0.4 + len(picks) * 0.1), 4),
            rationale=rationale,
            outputs={"picks": picks},
            metadata={"novelty_floor": self.novelty_floor,
                      "max_picks": self.max_picks},
            mode=self.mode,
            correlation_id=context.correlation_id,
            company_id=context.company_id,
        )


__all__ = ["CuriositySoul"]
