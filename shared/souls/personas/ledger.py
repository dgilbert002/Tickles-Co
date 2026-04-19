"""
Ledger Soul — the bookkeeper.

Ledger never trades. It summarises recent fills and turns them into
structured journal entries other souls can read. Deterministic: given
the same fills + positions, it emits the same journal row. Verdict is
always ``journal``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from shared.souls.protocol import (
    MODE_DETERMINISTIC,
    ROLE_BOOKKEEPER,
    SOUL_LEDGER,
    SoulContext,
    SoulDecision,
    VERDICT_JOURNAL,
)


@dataclass
class LedgerSoul:
    name: str = SOUL_LEDGER
    role: str = ROLE_BOOKKEEPER
    mode: str = MODE_DETERMINISTIC

    # ------------------------------------------------------------------

    def decide(self, context: SoulContext) -> SoulDecision:
        fields = context.fields or {}
        fills: List[Dict[str, Any]] = list(fields.get("fills") or [])
        positions: List[Dict[str, Any]] = list(fields.get("positions") or [])

        n_fills = len(fills)
        gross_notional = sum(abs(float(f.get("notional_usd") or 0.0)) for f in fills)
        fees = sum(abs(float(f.get("fee_usd") or 0.0)) for f in fills)
        realised_pnl = sum(float(f.get("realized_pnl_usd") or 0.0) for f in fills)
        open_notional = sum(abs(float(p.get("notional_usd") or 0.0)) for p in positions)

        by_symbol: Dict[str, Dict[str, float]] = {}
        for f in fills:
            sym = f.get("symbol") or "UNK"
            entry = by_symbol.setdefault(sym, {"fills": 0, "notional": 0.0, "pnl": 0.0})
            entry["fills"] += 1
            entry["notional"] += abs(float(f.get("notional_usd") or 0.0))
            entry["pnl"] += float(f.get("realized_pnl_usd") or 0.0)

        rationale = (
            f"{n_fills} fill(s), gross=${gross_notional:,.2f}, "
            f"fees=${fees:,.2f}, pnl=${realised_pnl:,.2f}, "
            f"open=${open_notional:,.2f}"
        )
        outputs = {
            "n_fills": n_fills,
            "gross_notional_usd": round(gross_notional, 2),
            "fees_usd": round(fees, 2),
            "realised_pnl_usd": round(realised_pnl, 4),
            "open_notional_usd": round(open_notional, 2),
            "by_symbol": {
                k: {kk: round(vv, 4) for kk, vv in v.items()}
                for k, v in by_symbol.items()
            },
        }
        return SoulDecision(
            persona_name=self.name,
            verdict=VERDICT_JOURNAL,
            confidence=1.0,
            rationale=rationale,
            outputs=outputs,
            metadata={},
            mode=self.mode,
            correlation_id=context.correlation_id,
            company_id=context.company_id,
        )


__all__ = ["LedgerSoul"]
