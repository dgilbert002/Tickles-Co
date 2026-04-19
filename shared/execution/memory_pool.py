"""
shared.execution.memory_pool — in-memory async pool for Phase 26 tests.

Implements just enough of the DatabasePool contract to let
:class:`shared.execution.store.ExecutionStore` run without Postgres.
"""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


class InMemoryExecutionPool:
    """Tiny async pool covering the execution layer's SQL surface.

    Data model:
      * ``self.orders: list[dict]``
      * ``self.order_events: list[dict]``
      * ``self.fills: list[dict]``
      * ``self.positions: list[dict]``  (append-only snapshots)
    """

    def __init__(self) -> None:
        self.orders: List[Dict[str, Any]] = []
        self.order_events: List[Dict[str, Any]] = []
        self.fills: List[Dict[str, Any]] = []
        self.positions: List[Dict[str, Any]] = []
        self._order_id_seq = itertools.count(1)
        self._event_id_seq = itertools.count(1)
        self._fill_id_seq = itertools.count(1)
        self._pos_id_seq = itertools.count(1)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _loads(value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        try:
            return json.loads(value)
        except Exception:
            return {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _find_order_idx(self, adapter: str, client_order_id: str) -> Optional[int]:
        for idx, row in enumerate(self.orders):
            if row.get("adapter") == adapter and row.get("client_order_id") == client_order_id:
                return idx
        return None

    # ------------------------------------------------------------------
    # Core async surface
    # ------------------------------------------------------------------

    async def execute(self, sql: str, params: Sequence[Any]) -> int:
        sql = sql.strip()

        if sql.startswith("INSERT INTO public.orders"):
            # Handled by fetch_one because the SQL returns a row.
            raise NotImplementedError(
                "insert_order uses fetch_one in the store; route via fetch_one"
            )

        if sql.startswith("UPDATE public.orders"):
            (
                order_id, status, filled_qty, avg_fill_price,
                fee_delta, external_id, reason,
            ) = params
            for row in self.orders:
                if row["id"] == int(order_id):
                    row["status"] = status
                    row["filled_quantity"] = float(filled_qty)
                    row["average_fill_price"] = avg_fill_price
                    row["fees_paid_usd"] = float(row.get("fees_paid_usd") or 0.0) + float(fee_delta or 0.0)
                    if external_id:
                        row["external_order_id"] = external_id
                    if reason:
                        row["reason"] = reason
                    row["updated_at"] = self._now()
                    return 1
            return 0

        if sql.startswith("INSERT INTO public.order_events"):
            order_id, event_type, severity, message, payload_json, ts = params
            self.order_events.append({
                "id": next(self._event_id_seq),
                "order_id": int(order_id),
                "event_type": event_type,
                "severity": severity,
                "message": message,
                "payload": self._loads(payload_json),
                "ts": ts or self._now(),
            })
            return 1

        raise NotImplementedError(f"InMemoryExecutionPool.execute: {sql!r}")

    async def execute_many(
        self, sql: str, rows: Sequence[Sequence[Any]]
    ) -> int:
        n = 0
        for r in rows:
            n += await self.execute(sql, r)
        return n

    # ------------------------------------------------------------------

    async def fetch_one(
        self, sql: str, params: Sequence[Any]
    ) -> Optional[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("INSERT INTO public.orders"):
            (
                company_id, strategy_id, agent_id, intent_hash, treasury_decision_id,
                adapter, exchange, account_id_external, symbol, direction, order_type,
                quantity, requested_notional, requested_price, tif,
                client_order_id, status, metadata_json,
            ) = params
            existing_idx = self._find_order_idx(adapter, client_order_id)
            if existing_idx is not None:
                self.orders[existing_idx]["updated_at"] = self._now()
                return {"id": self.orders[existing_idx]["id"]}
            row = {
                "id": next(self._order_id_seq),
                "company_id": company_id,
                "strategy_id": strategy_id,
                "agent_id": agent_id,
                "intent_hash": intent_hash,
                "treasury_decision_id": treasury_decision_id,
                "adapter": adapter,
                "exchange": exchange,
                "account_id_external": account_id_external,
                "symbol": symbol,
                "direction": direction,
                "order_type": order_type,
                "quantity": float(quantity),
                "requested_notional_usd": requested_notional,
                "requested_price": requested_price,
                "time_in_force": tif,
                "client_order_id": client_order_id,
                "external_order_id": None,
                "status": status,
                "filled_quantity": 0.0,
                "average_fill_price": None,
                "fees_paid_usd": 0.0,
                "reason": None,
                "submitted_at": self._now(),
                "updated_at": self._now(),
                "metadata": self._loads(metadata_json),
            }
            self.orders.append(row)
            return {"id": row["id"]}

        if sql.startswith("INSERT INTO public.fills"):
            (
                order_id, company_id, adapter, exchange, account_id_external,
                symbol, direction, quantity, price, notional,
                fee_usd, fee_currency, is_maker, liquidity,
                realized_pnl, external_fill_id, ts, metadata_json,
            ) = params
            row = {
                "id": next(self._fill_id_seq),
                "order_id": int(order_id),
                "company_id": company_id,
                "adapter": adapter,
                "exchange": exchange,
                "account_id_external": account_id_external,
                "symbol": symbol,
                "direction": direction,
                "quantity": float(quantity),
                "price": float(price),
                "notional_usd": float(notional),
                "fee_usd": float(fee_usd),
                "fee_currency": fee_currency,
                "is_maker": is_maker,
                "liquidity": liquidity,
                "realized_pnl_usd": realized_pnl,
                "external_fill_id": external_fill_id,
                "ts": ts or self._now(),
                "metadata": self._loads(metadata_json),
            }
            self.fills.append(row)
            return {"id": row["id"]}

        if sql.startswith("INSERT INTO public.position_snapshots"):
            (
                company_id, adapter, exchange, account_id_external, symbol,
                direction, quantity, avg_price, notional,
                unrealised, realized, leverage, ts, source, metadata_json,
            ) = params
            row = {
                "id": next(self._pos_id_seq),
                "company_id": company_id,
                "adapter": adapter,
                "exchange": exchange,
                "account_id_external": account_id_external,
                "symbol": symbol,
                "direction": direction,
                "quantity": float(quantity),
                "average_entry_price": avg_price,
                "notional_usd": notional,
                "unrealised_pnl_usd": unrealised,
                "realized_pnl_usd": float(realized),
                "leverage": int(leverage),
                "ts": ts or self._now(),
                "source": source,
                "metadata": self._loads(metadata_json),
            }
            self.positions.append(row)
            return {"id": row["id"]}

        if sql.startswith("SELECT * FROM public.orders\nWHERE adapter"):
            adapter, client_order_id = params
            idx = self._find_order_idx(adapter, client_order_id)
            return dict(self.orders[idx]) if idx is not None else None

        if sql.startswith("SELECT * FROM public.orders WHERE id"):
            (order_id,) = params
            for row in self.orders:
                if row["id"] == int(order_id):
                    return dict(row)
            return None

        raise NotImplementedError(f"InMemoryExecutionPool.fetch_one: {sql!r}")

    # ------------------------------------------------------------------

    async def fetch_all(
        self, sql: str, params: Sequence[Any]
    ) -> List[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("SELECT * FROM public.orders"):
            company_id, limit = params
            active = {"new", "accepted", "partially_filled", "pending_cancel"}
            rows = [
                dict(r) for r in self.orders
                if r["company_id"] == company_id and r["status"] in active
            ]
            rows.sort(key=lambda r: r["submitted_at"], reverse=True)
            return rows[: int(limit)]

        if sql.startswith("SELECT * FROM public.fills"):
            company_id, limit = params
            rows = [dict(r) for r in self.fills if r["company_id"] == company_id]
            rows.sort(key=lambda r: r["ts"], reverse=True)
            return rows[: int(limit)]

        if sql.startswith("SELECT * FROM public.positions_current"):
            (company_id,) = params
            latest: Dict[tuple, Dict[str, Any]] = {}
            for r in self.positions:
                if r["company_id"] != company_id:
                    continue
                key = (r["adapter"], r["exchange"], r["account_id_external"], r["symbol"])
                prev = latest.get(key)
                if prev is None or r["ts"] > prev["ts"]:
                    latest[key] = r
            out = [dict(r) for r in latest.values()]
            out.sort(key=lambda r: r["symbol"])
            return out

        raise NotImplementedError(f"InMemoryExecutionPool.fetch_all: {sql!r}")


__all__ = ["InMemoryExecutionPool"]
