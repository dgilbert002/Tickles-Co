"""In-memory pool for Phase 33 arb tests (mirrors PostgreSQL semantics)."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


class InMemoryArbPool:
    def __init__(self) -> None:
        self.venues: List[Dict[str, Any]] = []
        self.opportunities: List[Dict[str, Any]] = []
        self._v_seq = itertools.count(1)
        self._o_seq = itertools.count(1)

    @staticmethod
    def _loads(val: Any) -> Any:
        if val is None:
            return {}
        if isinstance(val, (dict, list)):
            return val
        try:
            return json.loads(val)
        except Exception:
            return {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    async def execute(self, sql: str, params: Sequence[Any]) -> int:
        raise NotImplementedError(f"InMemoryArbPool.execute: {sql!r}")

    async def fetch_one(
        self, sql: str, params: Sequence[Any],
    ) -> Optional[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("INSERT INTO public.arb_venues"):
            (company_id, name, kind, taker, maker, enabled, metadata) = params
            cv = company_id or ""
            for v in self.venues:
                if (v["name"] == name and v["kind"] == kind
                        and (v.get("company_id") or "") == cv):
                    v.update({
                        "taker_fee_bps": float(taker),
                        "maker_fee_bps": float(maker),
                        "enabled": bool(enabled),
                        "metadata": self._loads(metadata),
                        "updated_at": self._now(),
                    })
                    return {"id": v["id"]}
            vid = next(self._v_seq)
            self.venues.append({
                "id": vid, "company_id": company_id, "name": name,
                "kind": kind, "taker_fee_bps": float(taker),
                "maker_fee_bps": float(maker), "enabled": bool(enabled),
                "metadata": self._loads(metadata),
                "created_at": self._now(), "updated_at": self._now(),
            })
            return {"id": vid}

        if sql.startswith("INSERT INTO public.arb_opportunities"):
            (company_id, symbol, buy_v, sell_v, buy_ask, sell_bid,
             size_base, gross_bps, net_bps, est_profit_usd, fees_bps,
             correlation_id, metadata, observed_at) = params
            oid = next(self._o_seq)
            self.opportunities.append({
                "id": oid, "company_id": company_id, "symbol": symbol,
                "buy_venue": buy_v, "sell_venue": sell_v,
                "buy_ask": float(buy_ask), "sell_bid": float(sell_bid),
                "size_base": float(size_base),
                "gross_bps": float(gross_bps), "net_bps": float(net_bps),
                "est_profit_usd": float(est_profit_usd),
                "fees_bps": float(fees_bps),
                "correlation_id": correlation_id,
                "metadata": self._loads(metadata),
                "observed_at": observed_at or self._now(),
            })
            return {"id": oid}

        raise NotImplementedError(f"InMemoryArbPool.fetch_one: {sql!r}")

    async def fetch_all(
        self, sql: str, params: Sequence[Any],
    ) -> List[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("SELECT * FROM public.arb_venues"):
            rows = list(self.venues)
            if "enabled = TRUE" in sql:
                rows = [r for r in rows if r.get("enabled", True)]
            rows.sort(key=lambda r: (r["name"], r["kind"]))
            return [dict(r) for r in rows]

        if sql.startswith("SELECT * FROM public.arb_opportunities"):
            rows = list(self.opportunities)
            p_idx = 0
            if "symbol = $" in sql:
                rows = [r for r in rows if r["symbol"] == params[p_idx]]
                p_idx += 1
            if "net_bps >= $" in sql:
                threshold = float(params[p_idx])
                rows = [r for r in rows if float(r["net_bps"]) >= threshold]
                p_idx += 1
            limit = int(params[p_idx])
            rows.sort(key=lambda r: r["observed_at"], reverse=True)
            return [dict(r) for r in rows[:limit]]

        raise NotImplementedError(f"InMemoryArbPool.fetch_all: {sql!r}")


__all__ = ["InMemoryArbPool"]
