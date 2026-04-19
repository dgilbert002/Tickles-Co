"""shared.strategies.store — async wrappers around Phase 34 tables."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.strategies.protocol import StrategyDescriptor, StrategyIntent


class StrategyStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ------------------------------------------------------------- descriptors
    async def upsert_descriptor(self, d: StrategyDescriptor) -> int:
        sql = (
            "INSERT INTO public.strategy_descriptors "
            "(company_id, name, kind, description, enabled, priority, "
            " config, updated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7, NOW()) "
            "ON CONFLICT (kind, name, COALESCE(company_id,'')) "
            "DO UPDATE SET "
            "  description = EXCLUDED.description, "
            "  enabled     = EXCLUDED.enabled, "
            "  priority    = EXCLUDED.priority, "
            "  config      = EXCLUDED.config, "
            "  updated_at  = NOW() "
            "RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (
                d.company_id, d.name, d.kind, d.description,
                bool(d.enabled), int(d.priority),
                json.dumps(d.config or {}),
            ),
        )
        return int(row["id"]) if row and "id" in row else 0

    async def list_descriptors(
        self, *, enabled_only: bool = False,
    ) -> List[StrategyDescriptor]:
        sql = "SELECT * FROM public.strategy_descriptors"
        if enabled_only:
            sql += " WHERE enabled = TRUE"
        sql += " ORDER BY priority DESC, kind, name"
        rows = await self._pool.fetch_all(sql, ())
        return [_descriptor_row(r) for r in rows]

    # ---------------------------------------------------------------- intents
    async def record_intent(self, intent: StrategyIntent) -> int:
        sql = (
            "INSERT INTO public.strategy_intents "
            "(company_id, strategy_name, strategy_kind, symbol, side, "
            " venue, size_base, notional_usd, reference_price, status, "
            " decision_reason, order_id, correlation_id, source_ref, "
            " priority_score, metadata, proposed_at, decided_at, "
            " submitted_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,"
            "        $15,$16,$17,$18,$19) "
            "ON CONFLICT (strategy_name, source_ref) "
            "WHERE source_ref IS NOT NULL DO NOTHING "
            "RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (
                intent.company_id, intent.strategy_name,
                intent.strategy_kind, intent.symbol, intent.side,
                intent.venue, float(intent.size_base),
                float(intent.notional_usd),
                None if intent.reference_price is None
                else float(intent.reference_price),
                intent.status, intent.decision_reason, intent.order_id,
                intent.correlation_id, intent.source_ref,
                float(intent.priority_score),
                json.dumps(intent.metadata or {}),
                intent.proposed_at, intent.decided_at, intent.submitted_at,
            ),
        )
        return int(row["id"]) if row and "id" in row else 0

    async def update_intent_status(
        self,
        intent_id: int,
        status: str,
        *,
        reason: Optional[str] = None,
        order_id: Optional[int] = None,
        decided_at: Optional[datetime] = None,
        submitted_at: Optional[datetime] = None,
    ) -> None:
        sql = (
            "UPDATE public.strategy_intents SET "
            "  status = $2, "
            "  decision_reason = COALESCE($3, decision_reason), "
            "  order_id = COALESCE($4, order_id), "
            "  decided_at = COALESCE($5, decided_at), "
            "  submitted_at = COALESCE($6, submitted_at) "
            "WHERE id = $1"
        )
        await self._pool.execute(
            sql,
            (
                int(intent_id), status, reason, order_id,
                decided_at, submitted_at,
            ),
        )

    async def list_intents(
        self,
        *,
        strategy_name: Optional[str] = None,
        status: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 100,
    ) -> List[StrategyIntent]:
        sql = "SELECT * FROM public.strategy_intents WHERE 1=1"
        params: List[Any] = []
        idx = 1
        if strategy_name is not None:
            sql += f" AND strategy_name = ${idx}"
            params.append(strategy_name)
            idx += 1
        if status is not None:
            sql += f" AND status = ${idx}"
            params.append(status)
            idx += 1
        if symbol is not None:
            sql += f" AND symbol = ${idx}"
            params.append(symbol)
            idx += 1
        sql += f" ORDER BY proposed_at DESC LIMIT ${idx}"
        params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [_intent_row(r) for r in rows]

    async def list_latest(
        self, *, limit: int = 50,
    ) -> List[StrategyIntent]:
        sql = (
            "SELECT * FROM public.strategy_intents_latest "
            "ORDER BY proposed_at DESC LIMIT $1"
        )
        rows = await self._pool.fetch_all(sql, (int(limit),))
        return [_intent_row(r) for r in rows]


# ---------------------------------------------------------------------------


def _descriptor_row(row: Dict[str, Any]) -> StrategyDescriptor:
    cfg = row.get("config")
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except Exception:
            cfg = {}
    return StrategyDescriptor(
        id=int(row["id"]),
        name=row["name"], kind=row["kind"],
        description=row.get("description") or "",
        enabled=bool(row.get("enabled", True)),
        priority=int(row.get("priority") or 100),
        company_id=row.get("company_id"),
        config=cfg or {},
    )


def _intent_row(row: Dict[str, Any]) -> StrategyIntent:
    md = row.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    return StrategyIntent(
        id=int(row["id"]),
        strategy_name=row["strategy_name"],
        strategy_kind=row["strategy_kind"],
        symbol=row["symbol"], side=row["side"],
        size_base=float(row.get("size_base") or 0.0),
        notional_usd=float(row.get("notional_usd") or 0.0),
        venue=row.get("venue"),
        reference_price=(
            None if row.get("reference_price") is None
            else float(row["reference_price"])
        ),
        status=row.get("status") or "pending",
        decision_reason=row.get("decision_reason"),
        order_id=(
            None if row.get("order_id") is None else int(row["order_id"])
        ),
        correlation_id=row.get("correlation_id"),
        source_ref=row.get("source_ref"),
        priority_score=float(row.get("priority_score") or 0.0),
        company_id=row.get("company_id"),
        metadata=md or {},
        proposed_at=row.get("proposed_at"),
        decided_at=row.get("decided_at"),
        submitted_at=row.get("submitted_at"),
    )


__all__ = ["StrategyStore"]
