"""
Module: edge_scorer
Purpose: Pure, deterministic edge_score calculator with available-component normalisation.
Location: /opt/tickles/shared/intelligence/edge_scorer.py
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

FORMULA_VERSION = 1   # bump triggers backfill

# Static weights — keep here, not in SQL, so changes are reviewable in PR.
#
# PHASE_Y §6 deflated pnl_quality (0.25→0.20) and consistency (0.15→0.10) and
# introduced skill_minus_luck (0.15) — see shared/intelligence/skill_scorer.py
# for the C1–C5 composite that feeds skill_minus_luck. The new component is
# additive: when ``inputs.skill_score is None`` it is reported as
# ``available=False`` and the renormalise-on-availability path treats the
# scorer as if the component never existed (backwards-compatible deploy).
WEIGHTS = {
    "pnl_quality":            0.20,
    "consistency":            0.10,
    "discipline":             0.15,
    "reasoning_clarity":      0.10,
    "agreement_with_critic":  0.10,
    "regime_adaptability":    0.10,
    "time_to_resolution":     0.05,
    "cost_efficiency":        0.05,
    "social_signal_quality":  0.05,
    "pattern_fit":            0.05,
    "prompt_lift":            0.05,
    "skill_minus_luck":       0.15,
}

COMPONENT_CAP = 0.95   # anti-gaming: no single component can be 1.0
MIN_POSITIONS_FOR_FULL = 10
MIN_POSITIONS_FOR_DISPLAY = 3


@dataclass
class ScorerInputs:
    """Pre-fetched data for edge_score computation. No DB I/O."""

    closed_positions: list
    postmortems: list = field(default_factory=list)
    opinions: list = field(default_factory=list)
    pattern_cluster: Optional[Dict] = None
    prompt_lift_data: Optional[Dict] = None
    social_data: Optional[Dict] = None
    # PHASE_Y §6 — skill-vs-luck composite. None ⇒ component unavailable
    # (renormalisation handles that automatically). When provided, must be in
    # the unit interval [0, 1].
    skill_score: Optional[float] = None
    skill_window_days: Optional[int] = None
    skill_components: Optional[Dict] = None


@dataclass
class ScorerOutput:
    """Deterministic output. Same inputs → same output, byte-for-byte."""

    edge_score: float
    components: Dict[str, Dict]   # {name: {value, weight, available}}
    weights_used: Dict[str, float]
    confidence_low: bool
    formula_version: int = FORMULA_VERSION


def _sigmoid(x: float) -> float:
    """Sigmoid function mapping any real to (0, 1)."""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 1.0 if x > 0 else 0.0


def _pnl_quality(inputs: ScorerInputs) -> Tuple[float, bool]:
    """Sharpe-like risk-adjusted return over closed positions."""
    positions = inputs.closed_positions
    if not positions:
        return 0.0, False
    returns = []
    for p in positions:
        pnl = float(p.get("realised_pnl_pct", 0) or 0)
        returns.append(pnl)
    if not returns:
        return 0.0, False
    mean_r = sum(returns) / len(returns)
    std = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / max(len(returns), 1))
    sharpe_like = mean_r / (std + 1e-9)
    return _sigmoid(sharpe_like), True


def _consistency(inputs: ScorerInputs) -> Tuple[float, bool]:
    """1 − stddev(returns) / mean(abs(returns)), clipped to [0,1]."""
    positions = inputs.closed_positions
    if len(positions) < MIN_POSITIONS_FOR_FULL:
        return 0.0, False
    returns = [float(p.get("realised_pnl_pct", 0) or 0) for p in positions]
    mean_abs = sum(abs(r) for r in returns) / len(returns)
    if mean_abs < 1e-9:
        return 1.0, True
    std = math.sqrt(sum((r - sum(returns) / len(returns)) ** 2 for r in returns) / len(returns))
    score = max(0.0, min(1.0, 1.0 - std / mean_abs))
    return score, True


def _discipline(inputs: ScorerInputs) -> Tuple[float, bool]:
    """Fraction of trades with stop-loss respected."""
    positions = inputs.closed_positions
    if not positions:
        return 0.0, False
    with_sl = 0
    respected = 0
    for p in positions:
        sl = p.get("stop_loss")
        if sl is not None and sl != "":
            with_sl += 1
            exit_price = float(p.get("exit_price", 0) or 0)
            direction = p.get("direction", "")
            sl_val = float(sl)
            if direction == "long" and exit_price >= sl_val:
                respected += 1
            elif direction == "short" and exit_price <= sl_val:
                respected += 1
    if with_sl == 0:
        return 0.0, False
    return respected / with_sl, True


def _reasoning_clarity(inputs: ScorerInputs) -> Tuple[float, bool]:
    """Length-normalised LLM clarity score from Phase 7 postmortems."""
    postmortems = inputs.postmortems
    if not postmortems:
        return 0.0, False
    scores = []
    for pm in postmortems:
        clarity = pm.get("reasoning_clarity_score")
        if clarity is not None:
            scores.append(float(clarity))
    if not scores:
        return 0.0, False
    return min(1.0, sum(scores) / len(scores)), True


def _agreement_with_critic(inputs: ScorerInputs) -> Tuple[float, bool]:
    """Mean of would_take_trade agreement from Phase 8 agent_opinions."""
    opinions = inputs.opinions
    if not opinions:
        return 0.0, False
    agreements = []
    for op in opinions:
        wtt = op.get("would_take_trade")
        if wtt is not None:
            agreements.append(1.0 if wtt else 0.0)
    if not agreements:
        return 0.0, False
    return sum(agreements) / len(agreements), True


def _regime_adaptability(inputs: ScorerInputs) -> Tuple[float, bool]:
    """1 − variance(edge_per_regime), clipped to [0,1]."""
    postmortems = inputs.postmortems
    if not postmortems:
        return 0.0, False
    regime_edges: Dict[str, List[float]] = {}
    for pm in postmortems:
        regime = pm.get("market_regime")
        edge = pm.get("edge_score")
        if regime and edge is not None:
            regime_edges.setdefault(regime, []).append(float(edge))
    if len(regime_edges) < 2:
        return 0.0, False
    per_regime_mean = [sum(v) / len(v) for v in regime_edges.values()]
    overall_mean = sum(per_regime_mean) / len(per_regime_mean)
    variance = sum((m - overall_mean) ** 2 for m in per_regime_mean) / len(per_regime_mean)
    score = max(0.0, min(1.0, 1.0 - variance))
    return score, True


def _time_to_resolution(inputs: ScorerInputs) -> Tuple[float, bool]:
    """Exponential decay on time-to-target."""
    positions = inputs.closed_positions
    if not positions:
        return 0.0, False
    scores = []
    for p in positions:
        opened = p.get("opened_at")
        closed = p.get("closed_at")
        if opened and closed:
            try:
                from datetime import datetime, timezone
                o = opened if isinstance(opened, datetime) else datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
                c = closed if isinstance(closed, datetime) else datetime.fromisoformat(str(closed).replace("Z", "+00:00"))
                hours = (c - o).total_seconds() / 3600.0
                scores.append(math.exp(-hours / 168.0))  # 1 week half-life
            except Exception:
                pass
    if not scores:
        return 0.0, False
    return sum(scores) / len(scores), True


def _cost_efficiency(inputs: ScorerInputs) -> Tuple[float, bool]:
    """(realised_pnl − fees) / abs(realised_pnl), clipped to [0,1]."""
    positions = inputs.closed_positions
    if not positions:
        return 0.0, False
    scores = []
    for p in positions:
        pnl = float(p.get("realised_pnl_pct", 0) or 0)
        fees = float(p.get("total_fees_pct", 0) or 0)
        if abs(pnl) < 1e-9:
            continue
        eff = (pnl - fees) / abs(pnl)
        scores.append(max(0.0, min(1.0, eff)))
    if not scores:
        return 0.0, False
    return sum(scores) / len(scores), True


def _social_signal_quality(inputs: ScorerInputs) -> Tuple[float, bool]:
    """Follower-weighted historical hit-rate (Discord/Telegram only)."""
    social = inputs.social_data
    if not social:
        return 0.0, False
    hit_rate = social.get("follower_weighted_hit_rate")
    if hit_rate is None:
        return 0.0, False
    return max(0.0, min(1.0, float(hit_rate))), True


def _pattern_fit(inputs: ScorerInputs) -> Tuple[float, bool]:
    """Mean edge of actor's cluster vs global mean."""
    cluster = inputs.pattern_cluster
    if not cluster:
        return 0.0, False
    actor_edge = cluster.get("actor_cluster_mean_edge")
    global_edge = cluster.get("global_mean_edge")
    if actor_edge is None or global_edge is None:
        return 0.0, False
    diff = float(actor_edge) - float(global_edge)
    score = _sigmoid(diff * 10)  # scale to [0,1]
    return score, True


def _prompt_lift(inputs: ScorerInputs) -> Tuple[float, bool]:
    """(variant B mean) − (variant A mean), shifted to [0,1]."""
    lift = inputs.prompt_lift_data
    if not lift:
        return 0.0, False
    raw = lift.get("lift")
    if raw is None:
        return 0.0, False
    shifted = (float(raw) + 0.5)  # shift [-0.5, 0.5] → [0, 1]
    return max(0.0, min(1.0, shifted)), True


def _skill_minus_luck(inputs: ScorerInputs) -> Tuple[float, bool]:
    """PHASE_Y §6 — pass-through for the skill-vs-luck composite.

    The skill score itself is computed in
    :func:`shared.intelligence.skill_scorer.compute_skill` from the C1–C5
    components and supplied to the scorer pre-baked. This wrapper simply
    validates the value lives in ``[0, 1]`` and reports ``available=False``
    when ``inputs.skill_score`` is ``None`` so the renormalisation path
    excludes it gracefully.

    Args:
        inputs: A populated :class:`ScorerInputs`.

    Returns:
        Tuple of ``(value, available)``. ``value`` is clipped to ``[0, 1]``;
        ``available`` is ``False`` when no skill score was supplied or the
        value is non-finite.
    """
    raw = inputs.skill_score
    if raw is None:
        return 0.0, False
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("edge_scorer: skill_score is not numeric: %r", raw)
        return 0.0, False
    if not math.isfinite(value):
        logger.warning("edge_scorer: skill_score is non-finite: %r", raw)
        return 0.0, False
    return max(0.0, min(1.0, value)), True


def compute_edge_score(inputs: ScorerInputs) -> ScorerOutput:
    """Deterministic computation. Same inputs → same output, byte-for-byte.

    Args:
        inputs: Pre-fetched actor data via ScorerInputs dataclass.

    Returns:
        ScorerOutput with edge_score, components breakdown, renormalised weights,
        and confidence_low flag.
    """
    raw: Dict[str, Tuple[float, bool]] = {}
    raw["pnl_quality"]            = _pnl_quality(inputs)
    raw["consistency"]            = _consistency(inputs)
    raw["discipline"]             = _discipline(inputs)
    raw["reasoning_clarity"]      = _reasoning_clarity(inputs)
    raw["agreement_with_critic"]  = _agreement_with_critic(inputs)
    raw["regime_adaptability"]    = _regime_adaptability(inputs)
    raw["time_to_resolution"]     = _time_to_resolution(inputs)
    raw["cost_efficiency"]        = _cost_efficiency(inputs)
    raw["social_signal_quality"]  = _social_signal_quality(inputs)
    raw["pattern_fit"]            = _pattern_fit(inputs)
    raw["prompt_lift"]            = _prompt_lift(inputs)
    raw["skill_minus_luck"]       = _skill_minus_luck(inputs)

    # Renormalise weights over available components only
    available = {k: v for k, (v, ok) in raw.items() if ok}
    weights_used = {k: WEIGHTS[k] for k in available}
    weight_sum = sum(weights_used.values())
    if weight_sum == 0:
        score = 0.0
    else:
        capped = {k: min(v, COMPONENT_CAP) for k, v in available.items()}
        score = sum(weights_used[k] * capped[k] for k in capped) / weight_sum

    components = {
        name: {"value": v, "weight": WEIGHTS[name], "available": ok}
        for name, (v, ok) in raw.items()
    }
    n_closed = len(inputs.closed_positions)
    return ScorerOutput(
        edge_score=round(score, 4),
        components=components,
        weights_used={k: round(w / weight_sum, 4) for k, w in weights_used.items()} if weight_sum else {},
        confidence_low=(n_closed < MIN_POSITIONS_FOR_FULL),
    )
