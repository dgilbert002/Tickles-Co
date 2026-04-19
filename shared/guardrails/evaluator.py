"""
shared.guardrails.evaluator — pure evaluator for Phase 28
Crash Protection rules.

The evaluator is intentionally pure: it takes a
:class:`ProtectionSnapshot` and a list of rules and returns a list
of :class:`ProtectionDecision` objects. The service layer is
responsible for persisting events, issuing alerts, and consulting
decisions from the router.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

from shared.guardrails.protocol import (
    ProtectionDecision,
    ProtectionRule,
    ProtectionSnapshot,
    RULE_DAILY_LOSS,
    RULE_EQUITY_DRAWDOWN,
    RULE_POSITION_NOTIONAL,
    RULE_REGIME_CRASH,
    RULE_STALE_DATA,
    STATUS_RESOLVED,
    STATUS_TRIGGERED,
)


REGIME_CRASH_LABELS = {"crash", "recovery"}   # recovery tagged too: still risky


def _scope_matches(rule: ProtectionRule, *, company_id: Optional[str], universe: Optional[str], exchange: Optional[str], symbol: Optional[str]) -> bool:
    if rule.company_id not in (None, company_id):
        return False
    if rule.universe not in (None, universe):
        return False
    if rule.exchange not in (None, exchange):
        return False
    if rule.symbol not in (None, symbol):
        return False
    return True


def _evaluate_regime_crash(rule: ProtectionRule, snap: ProtectionSnapshot) -> List[ProtectionDecision]:
    out: List[ProtectionDecision] = []
    matching = [
        r for r in snap.regimes
        if _scope_matches(
            rule,
            company_id=snap.company_id,
            universe=r.universe,
            exchange=r.exchange,
            symbol=r.symbol,
        )
    ]
    if not matching:
        return out
    for r in matching:
        triggered = r.regime in REGIME_CRASH_LABELS
        out.append(ProtectionDecision(
            rule=rule,
            status=STATUS_TRIGGERED if triggered else STATUS_RESOLVED,
            reason=f"regime={r.regime} on {r.universe}/{r.exchange}/{r.symbol}",
            metric=None,
            company_id=snap.company_id,
            universe=r.universe,
            exchange=r.exchange,
            symbol=r.symbol,
            metadata={
                "classifier": r.classifier,
                "timeframe": r.timeframe,
                "as_of": r.as_of.isoformat(),
            },
        ))
    return out


def _evaluate_equity_drawdown(rule: ProtectionRule, snap: ProtectionSnapshot) -> List[ProtectionDecision]:
    if rule.threshold is None or snap.equity_usd is None or snap.equity_peak_usd is None:
        return []
    if not _scope_matches(
        rule, company_id=snap.company_id, universe=snap.universe,
        exchange=None, symbol=None,
    ):
        return []
    if snap.equity_peak_usd <= 0:
        return []
    drawdown = max(0.0, (snap.equity_peak_usd - snap.equity_usd) / snap.equity_peak_usd)
    triggered = drawdown >= float(rule.threshold)
    return [ProtectionDecision(
        rule=rule,
        status=STATUS_TRIGGERED if triggered else STATUS_RESOLVED,
        reason=f"equity drawdown {drawdown:.4f} vs threshold {rule.threshold:.4f}",
        metric=float(drawdown),
        company_id=snap.company_id,
        universe=snap.universe,
        exchange=None,
        symbol=None,
        metadata={
            "equity_usd": snap.equity_usd,
            "equity_peak_usd": snap.equity_peak_usd,
        },
    )]


def _evaluate_daily_loss(rule: ProtectionRule, snap: ProtectionSnapshot) -> List[ProtectionDecision]:
    if (
        rule.threshold is None
        or snap.equity_usd is None
        or snap.equity_daily_start_usd is None
    ):
        return []
    if not _scope_matches(
        rule, company_id=snap.company_id, universe=snap.universe,
        exchange=None, symbol=None,
    ):
        return []
    if snap.equity_daily_start_usd <= 0:
        return []
    loss_frac = max(0.0, (snap.equity_daily_start_usd - snap.equity_usd) / snap.equity_daily_start_usd)
    triggered = loss_frac >= float(rule.threshold)
    return [ProtectionDecision(
        rule=rule,
        status=STATUS_TRIGGERED if triggered else STATUS_RESOLVED,
        reason=f"daily loss {loss_frac:.4f} vs threshold {rule.threshold:.4f}",
        metric=float(loss_frac),
        company_id=snap.company_id,
        universe=snap.universe,
        exchange=None,
        symbol=None,
        metadata={
            "equity_usd": snap.equity_usd,
            "equity_daily_start_usd": snap.equity_daily_start_usd,
        },
    )]


def _evaluate_position_notional(rule: ProtectionRule, snap: ProtectionSnapshot) -> List[ProtectionDecision]:
    if rule.threshold is None:
        return []
    decisions: List[ProtectionDecision] = []
    for pos in snap.positions:
        if not _scope_matches(
            rule,
            company_id=pos.company_id,
            universe=snap.universe,
            exchange=pos.exchange,
            symbol=pos.symbol,
        ):
            continue
        if pos.quantity == 0:
            continue
        triggered = abs(pos.notional_usd) >= float(rule.threshold)
        decisions.append(ProtectionDecision(
            rule=rule,
            status=STATUS_TRIGGERED if triggered else STATUS_RESOLVED,
            reason=f"position notional {pos.notional_usd:.2f} vs threshold {rule.threshold:.2f}",
            metric=float(abs(pos.notional_usd)),
            company_id=pos.company_id,
            universe=snap.universe,
            exchange=pos.exchange,
            symbol=pos.symbol,
            metadata={
                "direction": pos.direction,
                "quantity": pos.quantity,
                "unrealised_pnl_usd": pos.unrealised_pnl_usd,
            },
        ))
    return decisions


def _evaluate_stale_data(rule: ProtectionRule, snap: ProtectionSnapshot) -> List[ProtectionDecision]:
    if rule.threshold is None or snap.last_tick_at is None:
        return []
    if not _scope_matches(
        rule, company_id=snap.company_id, universe=snap.universe,
        exchange=None, symbol=None,
    ):
        return []
    now = snap.now or datetime.now(timezone.utc)
    # threshold is interpreted as MINUTES here (operator-friendly)
    age = (now - snap.last_tick_at).total_seconds() / 60.0
    triggered = age >= float(rule.threshold)
    return [ProtectionDecision(
        rule=rule,
        status=STATUS_TRIGGERED if triggered else STATUS_RESOLVED,
        reason=f"data age {age:.2f} min vs threshold {rule.threshold:.2f} min",
        metric=float(age),
        company_id=snap.company_id,
        universe=snap.universe,
        exchange=None,
        symbol=None,
        metadata={
            "last_tick_at": snap.last_tick_at.isoformat(),
            "now": now.isoformat(),
            "threshold_unit": "minutes",
        },
    )]


_EVALUATORS = {
    RULE_REGIME_CRASH: _evaluate_regime_crash,
    RULE_EQUITY_DRAWDOWN: _evaluate_equity_drawdown,
    RULE_DAILY_LOSS: _evaluate_daily_loss,
    RULE_POSITION_NOTIONAL: _evaluate_position_notional,
    RULE_STALE_DATA: _evaluate_stale_data,
}


def evaluate(
    rules: Iterable[ProtectionRule],
    snapshot: ProtectionSnapshot,
) -> List[ProtectionDecision]:
    """Evaluate every enabled rule against ``snapshot``."""
    out: List[ProtectionDecision] = []
    for rule in rules:
        if not rule.enabled:
            continue
        fn = _EVALUATORS.get(rule.rule_type)
        if fn is None:
            continue
        out.extend(fn(rule, snapshot))
    return out


def decisions_block_intent(
    decisions: Iterable[ProtectionDecision],
    *,
    company_id: str,
    universe: Optional[str],
    exchange: str,
    symbol: str,
) -> List[ProtectionDecision]:
    """Return the subset of *triggered* decisions that block a specific
    execution intent. Used by the Execution Layer to reject new orders.

    A decision blocks an intent when:

    * it is ``status='triggered'``,
    * its ``action`` is ``halt_new_orders``,
    * its scope (company/universe/exchange/symbol) either matches the
      intent exactly, or is NULL (= wildcard) for any of those
      attributes.
    """
    from shared.guardrails.protocol import ACTION_HALT_NEW_ORDERS

    blockers: List[ProtectionDecision] = []
    for d in decisions:
        if d.status != STATUS_TRIGGERED:
            continue
        if d.rule.action != ACTION_HALT_NEW_ORDERS:
            continue
        if d.company_id not in (None, company_id):
            continue
        if d.universe not in (None, universe):
            continue
        if d.exchange not in (None, exchange):
            continue
        if d.symbol not in (None, symbol):
            continue
        blockers.append(d)
    return blockers


__all__ = [
    "evaluate",
    "decisions_block_intent",
    "REGIME_CRASH_LABELS",
]


# Small helper reused by tests ------------------------------------------------

def within(duration_seconds: float, *, since: datetime, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return (now - since) <= timedelta(seconds=duration_seconds)
