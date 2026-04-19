"""
shared.souls.store_phase32 — persistence helpers for Phase 32 souls.

Thin async wrappers around the three Phase 32 tables:

* ``public.scout_candidates``
* ``public.optimiser_candidates``
* ``public.regime_transitions``

These are deliberately separate from :class:`SoulsStore` so the core
``agent_*`` tables stay focused on verdict logging. In-memory pool
support lives in :mod:`shared.souls.memory_pool` under the same
``InMemorySoulsPool``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class ScoutCandidate:
    id: Optional[int]
    exchange: str
    symbol: str
    score: float
    universe: Optional[str] = None
    company_id: Optional[str] = None
    reason: Optional[str] = None
    status: str = "proposed"
    correlation_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "exchange": self.exchange, "symbol": self.symbol,
            "score": float(self.score), "universe": self.universe,
            "company_id": self.company_id, "reason": self.reason,
            "status": self.status, "correlation_id": self.correlation_id,
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass
class OptimiserCandidate:
    id: Optional[int]
    strategy: str
    params: Dict[str, Any]
    company_id: Optional[str] = None
    score: Optional[float] = None
    status: str = "pending"
    correlation_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "strategy": self.strategy,
            "params": dict(self.params), "company_id": self.company_id,
            "score": None if self.score is None else float(self.score),
            "status": self.status, "correlation_id": self.correlation_id,
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass
class RegimeTransition:
    id: Optional[int]
    exchange: str
    symbol: str
    timeframe: str
    from_regime: Optional[str]
    to_regime: str
    transitioned_at: datetime
    universe: Optional[str] = None
    confidence: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "exchange": self.exchange, "symbol": self.symbol,
            "timeframe": self.timeframe, "from_regime": self.from_regime,
            "to_regime": self.to_regime, "universe": self.universe,
            "confidence": float(self.confidence),
            "transitioned_at": self.transitioned_at.isoformat(),
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ScoutStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def upsert(self, cand: ScoutCandidate) -> int:
        sql = (
            "INSERT INTO public.scout_candidates "
            "(company_id, universe, exchange, symbol, score, reason, "
            " status, correlation_id, metadata) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) "
            "ON CONFLICT (exchange, symbol, COALESCE(universe,''), "
            "             COALESCE(company_id,'')) "
            "DO UPDATE SET score = EXCLUDED.score, "
            "              reason = EXCLUDED.reason, "
            "              status = EXCLUDED.status, "
            "              correlation_id = EXCLUDED.correlation_id, "
            "              metadata = EXCLUDED.metadata "
            "RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (cand.company_id, cand.universe, cand.exchange, cand.symbol,
             float(cand.score), cand.reason, cand.status,
             cand.correlation_id, json.dumps(cand.metadata or {})),
        )
        return int(row["id"]) if row and "id" in row else 0

    async def list(
        self, *, status: Optional[str] = None, limit: int = 200,
    ) -> List[ScoutCandidate]:
        sql = "SELECT * FROM public.scout_candidates WHERE 1=1"
        params: List[Any] = []
        if status is not None:
            sql += " AND status = $1"
            params.append(status)
            sql += f" ORDER BY created_at DESC LIMIT ${len(params)+1}"
        else:
            sql += f" ORDER BY created_at DESC LIMIT ${len(params)+1}"
        params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [_scout_row(r) for r in rows]


class OptimiserStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def insert(self, cand: OptimiserCandidate) -> int:
        sql = (
            "INSERT INTO public.optimiser_candidates "
            "(strategy, company_id, params, score, status, "
            " correlation_id, metadata) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (cand.strategy, cand.company_id, json.dumps(cand.params or {}),
             None if cand.score is None else float(cand.score),
             cand.status, cand.correlation_id,
             json.dumps(cand.metadata or {})),
        )
        return int(row["id"]) if row and "id" in row else 0

    async def list(
        self, *, strategy: Optional[str] = None,
        status: Optional[str] = None, limit: int = 200,
    ) -> List[OptimiserCandidate]:
        sql = "SELECT * FROM public.optimiser_candidates WHERE 1=1"
        params: List[Any] = []
        idx = 1
        for field_name, val in (("strategy", strategy), ("status", status)):
            if val is not None:
                sql += f" AND {field_name} = ${idx}"
                params.append(val)
                idx += 1
        sql += f" ORDER BY created_at DESC LIMIT ${idx}"
        params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [_optim_row(r) for r in rows]


class RegimeTransitionStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def insert(self, t: RegimeTransition) -> int:
        sql = (
            "INSERT INTO public.regime_transitions "
            "(universe, exchange, symbol, timeframe, from_regime, "
            " to_regime, transitioned_at, confidence, metadata) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (t.universe, t.exchange, t.symbol, t.timeframe,
             t.from_regime, t.to_regime, t.transitioned_at,
             float(t.confidence), json.dumps(t.metadata or {})),
        )
        return int(row["id"]) if row and "id" in row else 0

    async def list(
        self, *, exchange: Optional[str] = None,
        symbol: Optional[str] = None, limit: int = 200,
    ) -> List[RegimeTransition]:
        sql = "SELECT * FROM public.regime_transitions WHERE 1=1"
        params: List[Any] = []
        idx = 1
        for field_name, val in (("exchange", exchange), ("symbol", symbol)):
            if val is not None:
                sql += f" AND {field_name} = ${idx}"
                params.append(val)
                idx += 1
        sql += f" ORDER BY transitioned_at DESC LIMIT ${idx}"
        params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [_transition_row(r) for r in rows]


# ---------------------------------------------------------------------------


def _scout_row(row: Dict[str, Any]) -> ScoutCandidate:
    md = row.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    return ScoutCandidate(
        id=int(row["id"]),
        exchange=row["exchange"], symbol=row["symbol"],
        score=float(row.get("score") or 0.0),
        universe=row.get("universe"), company_id=row.get("company_id"),
        reason=row.get("reason"),
        status=row.get("status") or "proposed",
        correlation_id=row.get("correlation_id"),
        metadata=md or {}, created_at=row.get("created_at"),
    )


def _optim_row(row: Dict[str, Any]) -> OptimiserCandidate:
    params = row.get("params")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    md = row.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    return OptimiserCandidate(
        id=int(row["id"]), strategy=row["strategy"],
        params=params or {}, company_id=row.get("company_id"),
        score=None if row.get("score") is None else float(row["score"]),
        status=row.get("status") or "pending",
        correlation_id=row.get("correlation_id"),
        metadata=md or {}, created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _transition_row(row: Dict[str, Any]) -> RegimeTransition:
    md = row.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except Exception:
            md = {}
    return RegimeTransition(
        id=int(row["id"]), exchange=row["exchange"],
        symbol=row["symbol"], timeframe=row["timeframe"],
        from_regime=row.get("from_regime"),
        to_regime=row["to_regime"],
        transitioned_at=row["transitioned_at"],
        universe=row.get("universe"),
        confidence=float(row.get("confidence") or 0.0),
        metadata=md or {}, created_at=row.get("created_at"),
    )


__all__ = [
    "ScoutCandidate",
    "OptimiserCandidate",
    "RegimeTransition",
    "ScoutStore",
    "OptimiserStore",
    "RegimeTransitionStore",
]
