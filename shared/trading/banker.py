"""
shared.trading.banker — Phase 25 balance/equity snapshot recorder.

The Banker is the accounting book. It records *what we observed*
about each account from the exchange (or a paper feed) and answers
"how much capital does this company have available right now?".

Design rules
------------
* Append-only. Every snapshot is a new row — never UPDATE an existing
  balance. Makes audit and time-travel trivial.
* No exchange calls here. That belongs in the OMS / connector layer
  (Phase 26). Banker accepts already-fetched values and persists them.
* Consumers ask for the *latest* snapshot via
  :meth:`Banker.latest_snapshot`; the SQL view
  ``public.banker_balances_latest`` is the single source of truth.
* Paper / forward-test accounts are first-class. ``account_type``
  distinguishes them without a separate schema.

This module leans on the duck-typed async pool contract used by
:mod:`shared.services.catalog`, so tests can pass an in-memory fake.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class BalanceSnapshot:
    """One observed account balance/equity at a point in time."""

    company_id: str
    exchange: str
    account_id_external: str
    balance: float
    id: Optional[int] = None
    account_type: str = "demo"       # demo | live | paper
    currency: str = "USD"
    equity: Optional[float] = None
    margin_used: Optional[float] = None
    free_margin: Optional[float] = None
    unrealised_pnl: Optional[float] = None
    source: str = "ccxt"
    ts: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def available_capital(self) -> float:
        if self.free_margin is not None:
            return float(self.free_margin)
        if self.equity is not None and self.margin_used is not None:
            return max(float(self.equity) - float(self.margin_used), 0.0)
        margin = float(self.margin_used or 0.0)
        return max(float(self.balance) - margin, 0.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "company_id": self.company_id,
            "exchange": self.exchange,
            "account_id_external": self.account_id_external,
            "account_type": self.account_type,
            "currency": self.currency,
            "balance": float(self.balance),
            "equity": float(self.equity) if self.equity is not None else None,
            "margin_used": (
                float(self.margin_used) if self.margin_used is not None else None
            ),
            "free_margin": (
                float(self.free_margin) if self.free_margin is not None else None
            ),
            "unrealised_pnl": (
                float(self.unrealised_pnl) if self.unrealised_pnl is not None else None
            ),
            "source": self.source,
            "ts": self.ts.isoformat() if isinstance(self.ts, datetime) else self.ts,
            "metadata": dict(self.metadata),
            "available_capital": self.available_capital(),
        }


# ---------------------------------------------------------------------------
# DB SQL
# ---------------------------------------------------------------------------

_INSERT_BALANCE_SQL = """
INSERT INTO public.banker_balances (
    company_id, exchange, account_id_external, account_type, currency,
    balance, equity, margin_used, free_margin, unrealised_pnl,
    source, ts, metadata
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9, $10,
    $11, COALESCE($12, now()), $13::jsonb
)
"""

_SELECT_LATEST_SQL = """
SELECT * FROM public.banker_balances_latest
WHERE company_id = $1
  AND exchange = $2
  AND account_id_external = $3
  AND currency = $4
"""

_SELECT_LATEST_COMPANY_SQL = """
SELECT * FROM public.banker_balances_latest
WHERE company_id = $1
ORDER BY exchange, account_id_external, currency
"""

_SELECT_RECENT_SQL = """
SELECT * FROM public.banker_balances
WHERE company_id = $1
  AND exchange = $2
  AND account_id_external = $3
  AND currency = $4
ORDER BY ts DESC
LIMIT $5
"""

_DELETE_COMPANY_SQL = """
DELETE FROM public.banker_balances
WHERE company_id = $1
"""


# ---------------------------------------------------------------------------
# Deserialization helpers
# ---------------------------------------------------------------------------


def _row_to_snapshot(row: Dict[str, Any]) -> BalanceSnapshot:
    def _num(v: Any) -> Optional[float]:
        return float(v) if v is not None else None

    md = row.get("metadata")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except json.JSONDecodeError:
            md = {}
    if md is None:
        md = {}

    ts = row.get("ts")
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            ts = None

    return BalanceSnapshot(
        id=int(row["id"]) if row.get("id") is not None else None,
        company_id=str(row["company_id"]),
        exchange=str(row["exchange"]),
        account_id_external=str(row["account_id_external"]),
        account_type=str(row.get("account_type") or "demo"),
        currency=str(row.get("currency") or "USD"),
        balance=float(row["balance"]),
        equity=_num(row.get("equity")),
        margin_used=_num(row.get("margin_used")),
        free_margin=_num(row.get("free_margin")),
        unrealised_pnl=_num(row.get("unrealised_pnl")),
        source=str(row.get("source") or "ccxt"),
        ts=ts if isinstance(ts, datetime) else None,
        metadata=dict(md) if isinstance(md, dict) else {},
    )


# ---------------------------------------------------------------------------
# Banker
# ---------------------------------------------------------------------------


class Banker:
    """Async facade over ``public.banker_balances``."""

    async def record_snapshot(
        self,
        pool: Any,
        snapshot: BalanceSnapshot,
    ) -> int:
        ts = snapshot.ts
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        params = (
            snapshot.company_id,
            snapshot.exchange,
            snapshot.account_id_external,
            snapshot.account_type,
            snapshot.currency,
            float(snapshot.balance),
            None if snapshot.equity is None else float(snapshot.equity),
            None if snapshot.margin_used is None else float(snapshot.margin_used),
            None if snapshot.free_margin is None else float(snapshot.free_margin),
            None if snapshot.unrealised_pnl is None else float(snapshot.unrealised_pnl),
            snapshot.source,
            ts,
            json.dumps(snapshot.metadata or {}, sort_keys=True, default=str),
        )
        return await pool.execute(_INSERT_BALANCE_SQL, params)

    async def record_many(
        self, pool: Any, snapshots: List[BalanceSnapshot]
    ) -> int:
        n = 0
        for snap in snapshots:
            n += await self.record_snapshot(pool, snap)
        return n

    async def latest_snapshot(
        self,
        pool: Any,
        company_id: str,
        exchange: str,
        account_id_external: str,
        currency: str = "USD",
    ) -> Optional[BalanceSnapshot]:
        row = await pool.fetch_one(
            _SELECT_LATEST_SQL,
            (company_id, exchange, account_id_external, currency),
        )
        return _row_to_snapshot(row) if row else None

    async def latest_for_company(
        self, pool: Any, company_id: str
    ) -> List[BalanceSnapshot]:
        rows = await pool.fetch_all(_SELECT_LATEST_COMPANY_SQL, (company_id,))
        return [_row_to_snapshot(r) for r in rows]

    async def list_recent(
        self,
        pool: Any,
        company_id: str,
        exchange: str,
        account_id_external: str,
        currency: str = "USD",
        limit: int = 50,
    ) -> List[BalanceSnapshot]:
        rows = await pool.fetch_all(
            _SELECT_RECENT_SQL,
            (company_id, exchange, account_id_external, currency, int(limit)),
        )
        return [_row_to_snapshot(r) for r in rows]

    async def available_capital_usd(
        self,
        pool: Any,
        company_id: str,
        exchange: str,
        account_id_external: str,
    ) -> float:
        snap = await self.latest_snapshot(
            pool, company_id, exchange, account_id_external, currency="USD"
        )
        if snap is None:
            return 0.0
        return float(snap.available_capital())

    async def purge_company(self, pool: Any, company_id: str) -> int:
        return await pool.execute(_DELETE_COMPANY_SQL, (company_id,))


__all__ = [
    "BalanceSnapshot",
    "Banker",
]
