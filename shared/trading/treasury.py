"""
shared.trading.treasury — Phase 25 Treasury orchestration.

Treasury is the policy layer that sits between raw :class:`TradeIntent`
and an OMS. It wires three things together:

1. :class:`Banker` — "how much capital do we have?"
2. :class:`CapabilityChecker` — "is this trade allowed?"
3. :func:`size_intent` — "if it is allowed, what does it look like?"

The output is a :class:`TreasuryDecision` with three possible states:
``approved=True`` (sized, ready for OMS), ``approved=False, reasons=[...]``
(cap/risk denial), or ``skipped=True`` (sizer said the trade rounds to
zero or is below the venue minimum).

This module is deliberately dumb: it doesn't know about exchanges,
positions, or order IDs. All of that lands in Phase 26.

Every decision is persisted to ``public.treasury_decisions`` so an
auditor can replay what was approved and why.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from shared.trading.banker import Banker, BalanceSnapshot
from shared.trading.capabilities import (
    Capability,
    CapabilityCheck,
    CapabilityChecker,
    CapabilityStore,
    TradeIntent,
)
from shared.trading.sizer import (
    AccountSnapshot,
    MarketSnapshot,
    SizedIntent,
    StrategyConfig,
    size_intent,
)


@dataclass
class TreasuryDecision:
    """End-to-end verdict for one :class:`TradeIntent`."""

    intent: TradeIntent
    approved: bool
    reasons: List[str] = field(default_factory=list)
    capability_check: Optional[CapabilityCheck] = None
    sized: Optional[SizedIntent] = None
    available_capital_usd: Optional[float] = None
    skipped: bool = False
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def intent_hash(self) -> str:
        return self.intent.stable_hash()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent_hash": self.intent_hash(),
            "approved": self.approved,
            "skipped": self.skipped,
            "reasons": list(self.reasons),
            "available_capital_usd": self.available_capital_usd,
            "capability_check": (
                self.capability_check.to_dict() if self.capability_check else None
            ),
            "sized": self.sized.to_dict() if self.sized else None,
            "ts": self.ts.isoformat(),
        }


_INSERT_DECISION_SQL = """
INSERT INTO public.treasury_decisions (
    company_id, strategy_id, agent_id, exchange, symbol, direction,
    intent_hash, approved, reasons, capability_ids,
    requested_notional_usd, approved_notional_usd, available_capital_usd,
    ts, metadata
) VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8, $9::text[], $10::bigint[],
    $11, $12, $13,
    COALESCE($14, now()), $15::jsonb
)
"""


# ---------------------------------------------------------------------------
# Treasury
# ---------------------------------------------------------------------------


class Treasury:
    """Orchestrates capability + banker + sizer and persists the decision."""

    def __init__(
        self,
        capability_store: Optional[CapabilityStore] = None,
        banker: Optional[Banker] = None,
    ) -> None:
        self._caps = capability_store or CapabilityStore()
        self._banker = banker or Banker()

    # ------------------------------------------------------------------
    # Pure evaluation (no DB writes)
    # ------------------------------------------------------------------

    def evaluate_pure(
        self,
        intent: TradeIntent,
        capabilities: Sequence[Capability],
        account: AccountSnapshot,
        market: MarketSnapshot,
        strategy: StrategyConfig,
    ) -> TreasuryDecision:
        checker = CapabilityChecker(capabilities)
        cap_check = checker.evaluate(intent)

        if not cap_check.approved:
            return TreasuryDecision(
                intent=intent,
                approved=False,
                reasons=cap_check.reasons,
                capability_check=cap_check,
                available_capital_usd=account.available_capital(),
            )

        sized = size_intent(
            intent=intent,
            account=account,
            market=market,
            strategy=strategy,
            effective_max_notional_usd=cap_check.effective_max_notional_usd,
            effective_max_leverage=cap_check.effective_max_leverage,
        )

        if sized.skipped:
            return TreasuryDecision(
                intent=intent,
                approved=False,
                skipped=True,
                reasons=[sized.skipped_reason] if sized.skipped_reason else [],
                capability_check=cap_check,
                sized=sized,
                available_capital_usd=account.available_capital(),
            )

        reasons = list(sized.reasons)
        return TreasuryDecision(
            intent=intent,
            approved=True,
            reasons=reasons,
            capability_check=cap_check,
            sized=sized,
            available_capital_usd=account.available_capital(),
        )

    # ------------------------------------------------------------------
    # DB-backed evaluation
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        pool: Any,
        intent: TradeIntent,
        account_id_external: str,
        market: MarketSnapshot,
        strategy: StrategyConfig,
        currency: str = "USD",
        persist: bool = True,
    ) -> TreasuryDecision:
        caps = await self._caps.list_for_company(
            pool, intent.company_id, active_only=True
        )
        snapshot = await self._banker.latest_snapshot(
            pool,
            intent.company_id,
            intent.exchange,
            account_id_external,
            currency=currency,
        )
        if snapshot is None:
            decision = TreasuryDecision(
                intent=intent,
                approved=False,
                reasons=[
                    f"no balance snapshot for "
                    f"{intent.company_id}/{intent.exchange}/{account_id_external}"
                ],
                available_capital_usd=0.0,
            )
        else:
            account = AccountSnapshot(
                company_id=intent.company_id,
                exchange=intent.exchange,
                account_id_external=account_id_external,
                currency=currency,
                balance=snapshot.balance,
                equity=snapshot.equity,
                margin_used=snapshot.margin_used or 0.0,
                free_margin=snapshot.free_margin,
            )
            decision = self.evaluate_pure(
                intent=intent,
                capabilities=caps,
                account=account,
                market=market,
                strategy=strategy,
            )

        if persist:
            await self._persist(pool, decision)
        return decision

    async def _persist(self, pool: Any, decision: TreasuryDecision) -> None:
        intent = decision.intent
        cap_ids = (
            decision.capability_check.matched_capability_ids
            if decision.capability_check
            else []
        )
        requested = (
            intent.requested_notional_usd
            if intent.requested_notional_usd is not None
            else None
        )
        approved_notional = (
            decision.sized.notional_usd
            if decision.sized and not decision.sized.skipped
            else None
        )
        params = (
            intent.company_id,
            intent.strategy_id,
            intent.agent_id,
            intent.exchange,
            intent.symbol,
            intent.direction,
            decision.intent_hash(),
            bool(decision.approved),
            list(decision.reasons),
            list(cap_ids),
            requested,
            approved_notional,
            decision.available_capital_usd,
            decision.ts,
            json.dumps(
                {
                    "skipped": decision.skipped,
                    "sized": decision.sized.to_dict() if decision.sized else None,
                },
                sort_keys=True,
                default=str,
            ),
        )
        await pool.execute(_INSERT_DECISION_SQL, params)


# ---------------------------------------------------------------------------
# Convenience — build AccountSnapshot from BalanceSnapshot.
# ---------------------------------------------------------------------------


def account_snapshot_from_balance(snapshot: BalanceSnapshot) -> AccountSnapshot:
    return AccountSnapshot(
        company_id=snapshot.company_id,
        exchange=snapshot.exchange,
        account_id_external=snapshot.account_id_external,
        currency=snapshot.currency,
        balance=snapshot.balance,
        equity=snapshot.equity,
        margin_used=snapshot.margin_used or 0.0,
        free_margin=snapshot.free_margin,
    )


__all__ = [
    "Treasury",
    "TreasuryDecision",
    "account_snapshot_from_balance",
]
