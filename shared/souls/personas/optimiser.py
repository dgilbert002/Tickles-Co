"""
Optimiser Soul — parameter-tuning proposer.

Given a parameter search space, past results, and a configured budget,
the Optimiser proposes the next parameter set to try. The algorithm
is a deterministic best-first sweep:
  1. Evaluate untried corners / centre points first.
  2. Then explore neighbours of the best-scoring known point.
Output is ``propose`` (with a params dict) or ``observe`` (done).
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any, Dict, List, Tuple

from shared.souls.protocol import (
    MODE_DETERMINISTIC,
    ROLE_OPTIMISER,
    SOUL_OPTIMISER,
    SoulContext,
    SoulDecision,
    VERDICT_OBSERVE,
    VERDICT_PROPOSE,
)


@dataclass
class OptimiserSoul:
    name: str = SOUL_OPTIMISER
    role: str = ROLE_OPTIMISER
    mode: str = MODE_DETERMINISTIC

    max_total_trials: int = 200

    # ------------------------------------------------------------------

    def decide(self, context: SoulContext) -> SoulDecision:
        fields = context.fields or {}
        strategy = str(fields.get("strategy") or "unknown")
        space: Dict[str, List[Any]] = dict(fields.get("space") or {})
        history: List[Dict[str, Any]] = list(fields.get("history") or [])

        tried = {self._key(h.get("params") or {}) for h in history}
        n = len(tried)
        if n >= self.max_total_trials:
            return self._obs(context, strategy,
                             f"budget exhausted ({n}/{self.max_total_trials})")

        combos = list(self._grid(space))
        next_params: Dict[str, Any] | None = None
        reason = ""
        for params in combos:
            if self._key(params) not in tried:
                next_params = params
                reason = f"next untried combo in grid ({n} tried, {len(combos)} total)"
                break

        if next_params is None:
            best = self._best_neighbour(history, space, tried)
            if best is not None:
                next_params = best
                reason = "neighbour of best-scoring prior result"

        if next_params is None:
            return self._obs(context, strategy,
                             f"no new candidate ({n} explored)")

        return SoulDecision(
            persona_name=self.name,
            verdict=VERDICT_PROPOSE,
            confidence=0.7,
            rationale=reason,
            outputs={
                "strategy": strategy,
                "params": next_params,
                "trial_index": n,
            },
            metadata={"space_size": len(combos), "tried": n,
                      "max_total_trials": self.max_total_trials},
            mode=self.mode,
            correlation_id=context.correlation_id,
            company_id=context.company_id,
        )

    # ------------------------------------------------------------------

    def _obs(self, context: SoulContext, strategy: str, rationale: str) -> SoulDecision:
        return SoulDecision(
            persona_name=self.name,
            verdict=VERDICT_OBSERVE,
            confidence=1.0,
            rationale=rationale,
            outputs={"strategy": strategy},
            metadata={},
            mode=self.mode,
            correlation_id=context.correlation_id,
            company_id=context.company_id,
        )

    @staticmethod
    def _grid(space: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
        if not space:
            return []
        keys = sorted(space.keys())
        values = [list(space[k]) for k in keys]
        out: List[Dict[str, Any]] = []
        for combo in product(*values):
            out.append({k: v for k, v in zip(keys, combo)})
        return out

    @staticmethod
    def _key(params: Dict[str, Any]) -> Tuple[Tuple[str, Any], ...]:
        return tuple(sorted((k, params[k]) for k in params))

    @staticmethod
    def _best_neighbour(
        history: List[Dict[str, Any]],
        space: Dict[str, List[Any]],
        tried: set,
    ) -> Dict[str, Any] | None:
        scored = [h for h in history if h.get("score") is not None]
        if not scored:
            return None
        scored.sort(key=lambda h: -float(h["score"]))
        best = scored[0].get("params") or {}
        for key, values in space.items():
            if key not in best:
                continue
            try:
                idx = values.index(best[key])
            except ValueError:
                continue
            for step in (-1, 1):
                j = idx + step
                if 0 <= j < len(values):
                    neighbour = dict(best)
                    neighbour[key] = values[j]
                    t = tuple(sorted((k, neighbour[k]) for k in neighbour))
                    if t not in tried:
                        return neighbour
        return None


__all__ = ["OptimiserSoul"]
