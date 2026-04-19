"""In-memory pool for Phase 34 strategies tests."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


class InMemoryStrategyPool:
    def __init__(self) -> None:
        self.descriptors: List[Dict[str, Any]] = []
        self.intents: List[Dict[str, Any]] = []
        self._d_seq = itertools.count(1)
        self._i_seq = itertools.count(1)

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
        sql = sql.strip()
        if sql.startswith("UPDATE public.strategy_intents"):
            (
                iid, status, reason, order_id, decided_at, submitted_at,
            ) = params
            for row in self.intents:
                if row["id"] == int(iid):
                    row["status"] = status
                    if reason is not None:
                        row["decision_reason"] = reason
                    if order_id is not None:
                        row["order_id"] = int(order_id)
                    if decided_at is not None:
                        row["decided_at"] = decided_at
                    if submitted_at is not None:
                        row["submitted_at"] = submitted_at
                    return 1
            return 0
        raise NotImplementedError(f"InMemoryStrategyPool.execute: {sql!r}")

    async def fetch_one(
        self, sql: str, params: Sequence[Any],
    ) -> Optional[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("INSERT INTO public.strategy_descriptors"):
            (
                company_id, name, kind, description, enabled,
                priority, config,
            ) = params
            cv = company_id or ""
            for d in self.descriptors:
                if (
                    d["kind"] == kind and d["name"] == name
                    and (d.get("company_id") or "") == cv
                ):
                    d.update({
                        "description": description,
                        "enabled": bool(enabled),
                        "priority": int(priority),
                        "config": self._loads(config),
                        "updated_at": self._now(),
                    })
                    return {"id": d["id"]}
            did = next(self._d_seq)
            self.descriptors.append({
                "id": did, "company_id": company_id, "name": name,
                "kind": kind, "description": description,
                "enabled": bool(enabled), "priority": int(priority),
                "config": self._loads(config),
                "created_at": self._now(), "updated_at": self._now(),
            })
            return {"id": did}

        if sql.startswith("INSERT INTO public.strategy_intents"):
            (
                company_id, strategy_name, strategy_kind, symbol, side,
                venue, size_base, notional_usd, reference_price, status,
                decision_reason, order_id, correlation_id, source_ref,
                priority_score, metadata, proposed_at, decided_at,
                submitted_at,
            ) = params
            # dedupe check for partial unique index
            if source_ref is not None:
                for row in self.intents:
                    if (
                        row["strategy_name"] == strategy_name
                        and row.get("source_ref") == source_ref
                    ):
                        return None
            iid = next(self._i_seq)
            self.intents.append({
                "id": iid, "company_id": company_id,
                "strategy_name": strategy_name,
                "strategy_kind": strategy_kind,
                "symbol": symbol, "side": side, "venue": venue,
                "size_base": float(size_base),
                "notional_usd": float(notional_usd),
                "reference_price": (
                    None if reference_price is None
                    else float(reference_price)
                ),
                "status": status,
                "decision_reason": decision_reason,
                "order_id": order_id,
                "correlation_id": correlation_id,
                "source_ref": source_ref,
                "priority_score": float(priority_score),
                "metadata": self._loads(metadata),
                "proposed_at": proposed_at or self._now(),
                "decided_at": decided_at,
                "submitted_at": submitted_at,
            })
            return {"id": iid}

        raise NotImplementedError(f"InMemoryStrategyPool.fetch_one: {sql!r}")

    async def fetch_all(
        self, sql: str, params: Sequence[Any],
    ) -> List[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("SELECT * FROM public.strategy_descriptors"):
            rows = list(self.descriptors)
            if "enabled = TRUE" in sql:
                rows = [r for r in rows if r.get("enabled", True)]
            rows.sort(
                key=lambda r: (-int(r.get("priority") or 0),
                               r["kind"], r["name"]),
            )
            return [dict(r) for r in rows]

        if sql.startswith("SELECT * FROM public.strategy_intents_latest"):
            rows = sorted(
                list(self.intents),
                key=lambda r: r.get("proposed_at") or self._now(),
                reverse=True,
            )
            seen = set()
            out = []
            for r in rows:
                key = (r["strategy_name"], r["symbol"], r["side"])
                if key in seen:
                    continue
                seen.add(key)
                out.append(dict(r))
            limit = int(params[0]) if params else len(out)
            out.sort(
                key=lambda r: r.get("proposed_at") or self._now(),
                reverse=True,
            )
            return out[:limit]

        if sql.startswith("SELECT * FROM public.strategy_intents"):
            rows = list(self.intents)
            p_idx = 0
            if "strategy_name = $" in sql:
                rows = [
                    r for r in rows if r["strategy_name"] == params[p_idx]
                ]
                p_idx += 1
            if "status = $" in sql:
                rows = [r for r in rows if r["status"] == params[p_idx]]
                p_idx += 1
            if "symbol = $" in sql:
                rows = [r for r in rows if r["symbol"] == params[p_idx]]
                p_idx += 1
            limit = int(params[p_idx])
            rows.sort(
                key=lambda r: r.get("proposed_at") or self._now(),
                reverse=True,
            )
            return [dict(r) for r in rows[:limit]]

        raise NotImplementedError(f"InMemoryStrategyPool.fetch_all: {sql!r}")


__all__ = ["InMemoryStrategyPool"]
