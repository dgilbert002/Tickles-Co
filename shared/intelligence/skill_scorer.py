"""
Module: skill_scorer
Purpose: Python mirror of the Phase Y.1 compute_skill_score() SQL function with
         component-dropout / weight-renormalisation logic.
Location: /opt/tickles/shared/intelligence/skill_scorer.py
Plan ref: shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md §3.3

The SQL function in 2026_05_05_phase_y_skill_views.sql treats missing components
as zero (penalising the actor). For early operation, where C4 mem0_recall_log
or C5 prompt-lift data may be entirely absent, callers can pass per-component
None values to this Python mirror to drop those components and renormalise the
remaining weights so the score stays in [0, 1].

The dashboard tooltip should display the dropped-components list returned here
("computed without C4 — recall log empty for this window") so the headline
number is never misleading.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

# Canonical weights — MUST sum to 1.0. Keep in sync with the SQL function in
# shared/intelligence/migrations/2026_05_05_phase_y_skill_views.sql.
WEIGHTS: Dict[str, float] = {
    "clarity":     0.30,
    "consistency": 0.25,
    "reason_corr": 0.20,
    "recall_hit":  0.15,
    "prompt_lift": 0.10,
}

# Tolerance for the weight-sum invariant.
_WEIGHT_SUM_TOL = 1e-9

# Defensive: verify at import time that weights sum to 1.0.
_weight_sum = sum(WEIGHTS.values())
if abs(_weight_sum - 1.0) > _WEIGHT_SUM_TOL:
    raise RuntimeError(
        f"skill_scorer.WEIGHTS must sum to 1.0; got {_weight_sum!r}. "
        "This is a code defect, not a runtime error."
    )


def compute_skill(
    components: Mapping[str, Optional[float]],
) -> Tuple[float, List[str]]:
    """Compute a renormalised skill score given per-component values.

    Components with ``None`` value are treated as "no data" and dropped; the
    weights of the remaining components are renormalised so they sum to 1.0.
    All input values are clipped to [0, 1] before weighting.

    Args:
        components: Mapping from component name (must be a key of ``WEIGHTS``)
            to a float in [0, 1] or ``None`` for "no data". Unknown keys are
            ignored with a warning. Missing keys are treated as ``None``.

    Returns:
        A tuple ``(score, dropped)`` where:

        * ``score`` is a float in [0, 1]. If every component is ``None`` the
          score is 0.0 (consistent with "no signal").
        * ``dropped`` is a sorted list of component names that were dropped
          because their value was ``None`` or missing.

    Raises:
        TypeError: if ``components`` is not a mapping.
        ValueError: if any non-None value is not finite.
    """
    if not isinstance(components, Mapping):
        raise TypeError(f"components must be a Mapping, got {type(components).__name__}")

    # Detect unknown keys (caller bug — log but don't raise).
    unknown = set(components) - set(WEIGHTS)
    if unknown:
        logger.warning(
            "skill_scorer.compute_skill: ignoring unknown component(s): %s",
            sorted(unknown),
        )

    # Build the active subset and the dropped list, considering ALL canonical
    # components (so a missing key counts as dropped).
    active: Dict[str, float] = {}
    dropped: List[str] = []
    for name in WEIGHTS:
        raw = components.get(name)
        if raw is None:
            dropped.append(name)
            continue
        # Reject bool explicitly — bool is a subclass of int, so float(True)==1.0
        # would silently coerce a caller bug into a max-confidence component.
        if isinstance(raw, bool):
            raise ValueError(
                f"component {name!r} value must be a real number or None; "
                f"got bool {raw!r}"
            )
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"component {name!r} value must be a real number or None; got {raw!r}"
            ) from exc
        # Reject NaN / inf — they would silently corrupt the score.
        if not math.isfinite(value):
            raise ValueError(
                f"component {name!r} value must be finite; got {raw!r}"
            )
        # Clip to [0, 1] — the SQL component formulas already clip but defend
        # against caller-supplied raw values.
        active[name] = max(0.0, min(1.0, value))

    if not active:
        return 0.0, sorted(dropped)

    total_w = sum(WEIGHTS[name] for name in active)
    # total_w is strictly positive because active is non-empty and all WEIGHTS > 0.
    weighted_sum = sum(WEIGHTS[name] * value for name, value in active.items())
    score = weighted_sum / total_w
    # Final clip for safety against floating-point drift.
    score = max(0.0, min(1.0, score))
    return score, sorted(dropped)


def explain(
    components: Mapping[str, Optional[float]],
) -> Dict[str, object]:
    """Produce a JSON-serialisable explanation of a skill computation.

    Useful for the dashboard tooltip and for the api_cost_log audit trail.

    Args:
        components: Same shape as ``compute_skill``.

    Returns:
        Dict with keys:

        * ``score``      — the float score in [0, 1].
        * ``dropped``    — sorted list of component names that were None.
        * ``weights_used`` — the renormalised weights for the active
          components, as a dict (sums to 1.0 if any component was active).
        * ``components`` — the (clipped) values used per active component.

    Raises:
        Same as ``compute_skill``.
    """
    score, dropped = compute_skill(components)

    # Recompute the renormalised weights for transparency.
    active_names = [n for n in WEIGHTS if n not in dropped]
    if active_names:
        total_w = sum(WEIGHTS[n] for n in active_names)
        weights_used = {n: WEIGHTS[n] / total_w for n in active_names}
    else:
        weights_used = {}

    clipped: Dict[str, float] = {}
    for name in active_names:
        try:
            raw = float(components[name])  # type: ignore[arg-type]
        except (TypeError, ValueError):
            # Already validated by compute_skill — defensive only.
            continue
        clipped[name] = max(0.0, min(1.0, raw))

    return {
        "score": score,
        "dropped": dropped,
        "weights_used": weights_used,
        "components": clipped,
    }
