"""
shared.trading.memory_pool — in-memory async pool stub for Phase 25.

Implements just enough of the :class:`shared.utils.db.DatabasePool`
contract for the Banker, CapabilityStore and Treasury code paths so
tests + dry-run tooling don't need a live Postgres.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


class InMemoryTradingPool:
    """Tiny async pool that backs the Phase 25 module happy paths.

    Data model:

    * ``self.capabilities[(company, scope_kind, scope_id)] -> dict``
    * ``self.balances[(company, exchange, account, currency)] -> list[dict]``
    * ``self.decisions -> list[dict]``
    * ``self.leverage -> list[dict]``
    """

    def __init__(self) -> None:
        self.capabilities: Dict[tuple, Dict[str, Any]] = {}
        self.balances: Dict[tuple, List[Dict[str, Any]]] = {}
        self.decisions: List[Dict[str, Any]] = []
        self.leverage: List[Dict[str, Any]] = []
        self._cap_id_seq = itertools.count(1)
        self._bal_id_seq = itertools.count(1)

    # ------------------------------------------------------------------
    # Inspection helpers used by tests
    # ------------------------------------------------------------------

    def all_balances(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for rows in self.balances.values():
            out.extend(rows)
        return out

    # ------------------------------------------------------------------
    # Core async surface
    # ------------------------------------------------------------------

    async def execute(self, sql: str, params: Sequence[Any]) -> int:
        sql = sql.strip()

        # ---- capabilities: upsert ----
        if sql.startswith("INSERT INTO public.capabilities"):
            (
                company_id, scope_kind, scope_id,
                max_notional, max_lev, max_daily_loss, max_open,
                allow_venues, deny_venues, allow_symbols, deny_symbols,
                allow_dirs, allow_order_types,
                active, notes, metadata_json,
            ) = params
            key = (company_id, scope_kind, scope_id)
            now = datetime.now(timezone.utc)
            existing = self.capabilities.get(key)
            metadata = json.loads(metadata_json) if metadata_json else {}
            row = existing or {
                "id": next(self._cap_id_seq),
                "created_at": now,
            }
            row.update({
                "company_id": company_id,
                "scope_kind": scope_kind,
                "scope_id": scope_id,
                "max_notional_usd": max_notional,
                "max_leverage": max_lev,
                "max_daily_loss_usd": max_daily_loss,
                "max_open_positions": max_open,
                "allow_venues": list(allow_venues or []),
                "deny_venues": list(deny_venues or []),
                "allow_symbols": list(allow_symbols or []),
                "deny_symbols": list(deny_symbols or []),
                "allow_directions": list(allow_dirs or []),
                "allow_order_types": list(allow_order_types or []),
                "active": bool(active),
                "notes": str(notes or ""),
                "metadata": metadata,
                "updated_at": now,
            })
            self.capabilities[key] = row
            return 1

        # ---- capabilities: delete ----
        if sql.startswith("DELETE FROM public.capabilities"):
            company_id, scope_kind, scope_id = params
            return 1 if self.capabilities.pop((company_id, scope_kind, scope_id), None) else 0

        # ---- banker: insert snapshot ----
        if sql.startswith("INSERT INTO public.banker_balances"):
            (
                company_id, exchange, account_id, account_type, currency,
                balance, equity, margin_used, free_margin, unrealised,
                source, ts, metadata_json,
            ) = params
            bkey = (company_id, exchange, account_id, currency)
            row = {
                "id": next(self._bal_id_seq),
                "company_id": company_id,
                "exchange": exchange,
                "account_id_external": account_id,
                "account_type": account_type,
                "currency": currency,
                "balance": float(balance),
                "equity": float(equity) if equity is not None else None,
                "margin_used": float(margin_used) if margin_used is not None else None,
                "free_margin": float(free_margin) if free_margin is not None else None,
                "unrealised_pnl": float(unrealised) if unrealised is not None else None,
                "source": source,
                "ts": ts or datetime.now(timezone.utc),
                "metadata": json.loads(metadata_json) if metadata_json else {},
            }
            self.balances.setdefault(bkey, []).append(row)
            return 1

        # ---- banker: purge company ----
        if sql.startswith("DELETE FROM public.banker_balances"):
            (company_id,) = params
            n = 0
            for key in list(self.balances.keys()):
                if key[0] == company_id:
                    n += len(self.balances[key])
                    del self.balances[key]
            return n

        # ---- treasury: insert decision ----
        if sql.startswith("INSERT INTO public.treasury_decisions"):
            (
                company_id, strategy_id, agent_id, exchange, symbol, direction,
                intent_hash, approved, reasons, cap_ids,
                requested_notional, approved_notional, available_cap,
                ts, metadata_json,
            ) = params
            self.decisions.append({
                "id": len(self.decisions) + 1,
                "company_id": company_id,
                "strategy_id": strategy_id,
                "agent_id": agent_id,
                "exchange": exchange,
                "symbol": symbol,
                "direction": direction,
                "intent_hash": intent_hash,
                "approved": bool(approved),
                "reasons": list(reasons or []),
                "capability_ids": list(cap_ids or []),
                "requested_notional_usd": requested_notional,
                "approved_notional_usd": approved_notional,
                "available_capital_usd": available_cap,
                "ts": ts or datetime.now(timezone.utc),
                "metadata": json.loads(metadata_json) if metadata_json else {},
            })
            return 1

        # ---- leverage history insert (CLI helper) ----
        if sql.startswith("INSERT INTO public.leverage_history"):
            (
                company_id, exchange, symbol, direction,
                lev_req, lev_applied, requested_by, ok, reason, ts, metadata_json,
            ) = params
            self.leverage.append({
                "id": len(self.leverage) + 1,
                "company_id": company_id,
                "exchange": exchange,
                "symbol": symbol,
                "direction": direction,
                "leverage_requested": int(lev_req),
                "leverage_applied": int(lev_applied),
                "requested_by": requested_by,
                "ok": bool(ok),
                "reason": reason,
                "ts": ts or datetime.now(timezone.utc),
                "metadata": json.loads(metadata_json) if metadata_json else {},
            })
            return 1

        raise NotImplementedError(f"InMemoryTradingPool.execute: {sql!r}")

    async def execute_many(
        self, sql: str, rows: Sequence[Sequence[Any]]
    ) -> int:
        n = 0
        for r in rows:
            n += await self.execute(sql, r)
        return n

    async def fetch_all(
        self, sql: str, params: Sequence[Any]
    ) -> List[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("SELECT * FROM public.capabilities"):
            (company_id,) = params
            rows = [
                dict(r) for (c, sk, sid), r in self.capabilities.items()
                if c == company_id
            ]
            rows.sort(key=lambda r: (r["scope_kind"], r["scope_id"]))
            return rows

        if sql.startswith("SELECT * FROM public.banker_balances_latest\nWHERE company_id"):
            (company_id,) = params
            out: List[Dict[str, Any]] = []
            for key, rows in self.balances.items():
                if key[0] != company_id or not rows:
                    continue
                latest = max(rows, key=lambda r: r["ts"])
                out.append(dict(latest))
            out.sort(key=lambda r: (r["exchange"], r["account_id_external"], r["currency"]))
            return out

        if sql.startswith("SELECT * FROM public.banker_balances\nWHERE company_id"):
            company_id, exchange, account_id, currency, limit = params
            rows = list(
                self.balances.get((company_id, exchange, account_id, currency), [])
            )
            rows.sort(key=lambda r: r["ts"], reverse=True)
            return [dict(r) for r in rows[: int(limit)]]

        raise NotImplementedError(f"InMemoryTradingPool.fetch_all: {sql!r}")

    async def fetch_one(
        self, sql: str, params: Sequence[Any]
    ) -> Optional[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("SELECT * FROM public.capabilities\nWHERE company_id"):
            company_id, scope_kind, scope_id = params
            row = self.capabilities.get((company_id, scope_kind, scope_id))
            return dict(row) if row else None

        if sql.startswith("SELECT * FROM public.banker_balances_latest\nWHERE company_id"):
            company_id, exchange, account_id, currency = params
            rows = self.balances.get((company_id, exchange, account_id, currency))
            if not rows:
                return None
            return dict(max(rows, key=lambda r: r["ts"]))

        raise NotImplementedError(f"InMemoryTradingPool.fetch_one: {sql!r}")


__all__ = ["InMemoryTradingPool"]
