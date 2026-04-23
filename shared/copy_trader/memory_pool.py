"""In-memory pool for Phase 33 copy-trader tests."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


class InMemoryCopyPool:
    def __init__(self) -> None:
        self.sources: List[Dict[str, Any]] = []
        self.trades: List[Dict[str, Any]] = []
        self._s_seq = itertools.count(1)
        self._t_seq = itertools.count(1)

    @staticmethod
    def _loads(val: Any, default: Any) -> Any:
        if val is None:
            return default
        if isinstance(val, (list, dict)):
            return val
        try:
            return json.loads(val)
        except Exception:
            return default

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    async def execute(self, sql: str, params: Sequence[Any]) -> int:
        sql = sql.strip()
        if sql.startswith("UPDATE public.copy_sources SET last_checked_at"):
            (sid,) = params
            for s in self.sources:
                if s["id"] == int(sid):
                    s["last_checked_at"] = self._now()
                    s["updated_at"] = self._now()
                    return 1
            return 0
        raise NotImplementedError(f"InMemoryCopyPool.execute: {sql!r}")

    async def fetch_one(
        self, sql: str, params: Sequence[Any],
    ) -> Optional[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("INSERT INTO public.copy_sources"):
            (company_id, name, kind, venue, identifier, size_mode,
             size_value, max_notional, wl, bl, enabled, metadata,
             last_checked_at) = params
            cv = company_id or ""
            vv = venue or ""
            for s in self.sources:
                if (s["kind"] == kind and (s.get("venue") or "") == vv
                        and s["identifier"] == identifier
                        and (s.get("company_id") or "") == cv):
                    s.update({
                        "name": name,
                        "size_mode": size_mode,
                        "size_value": float(size_value or 0.0),
                        "max_notional_usd": (
                            None if max_notional is None
                            else float(max_notional)
                        ),
                        "symbol_whitelist": self._loads(wl, []),
                        "symbol_blacklist": self._loads(bl, []),
                        "enabled": bool(enabled),
                        "metadata": self._loads(metadata, {}),
                        "updated_at": self._now(),
                    })
                    return {"id": s["id"]}
            sid = next(self._s_seq)
            self.sources.append({
                "id": sid, "company_id": company_id, "name": name,
                "kind": kind, "venue": venue, "identifier": identifier,
                "size_mode": size_mode,
                "size_value": float(size_value or 0.0),
                "max_notional_usd": (
                    None if max_notional is None else float(max_notional)
                ),
                "symbol_whitelist": self._loads(wl, []),
                "symbol_blacklist": self._loads(bl, []),
                "enabled": bool(enabled),
                "metadata": self._loads(metadata, {}),
                "last_checked_at": last_checked_at,
                "created_at": self._now(),
                "updated_at": self._now(),
            })
            return {"id": sid}

        if sql.startswith("SELECT * FROM public.copy_sources WHERE id ="):
            (sid,) = params
            for s in self.sources:
                if s["id"] == int(sid):
                    return dict(s)
            return None

        if sql.startswith("INSERT INTO public.copy_trades"):
            (source_id, company_id, source_fill_id, source_trade_ts,
             symbol, side, source_price, source_qty, source_notional,
             mapped_qty, mapped_notional, status, skip_reason,
             correlation_id, metadata) = params
            for t in self.trades:
                if (t["source_id"] == int(source_id)
                        and t["source_fill_id"] == source_fill_id):
                    return None  # ON CONFLICT DO NOTHING
            tid = next(self._t_seq)
            self.trades.append({
                "id": tid, "source_id": int(source_id),
                "company_id": company_id,
                "source_fill_id": source_fill_id,
                "source_trade_ts": source_trade_ts,
                "symbol": symbol, "side": side,
                "source_price": (
                    None if source_price is None else float(source_price)
                ),
                "source_qty_base": (
                    None if source_qty is None else float(source_qty)
                ),
                "source_notional_usd": (
                    None if source_notional is None
                    else float(source_notional)
                ),
                "mapped_qty_base": float(mapped_qty),
                "mapped_notional_usd": float(mapped_notional),
                "status": status, "skip_reason": skip_reason,
                "correlation_id": correlation_id,
                "metadata": self._loads(metadata, {}),
                "created_at": self._now(), "updated_at": self._now(),
            })
            return {"id": tid}

        raise NotImplementedError(f"InMemoryCopyPool.fetch_one: {sql!r}")

    async def fetch_all(
        self, sql: str, params: Sequence[Any],
    ) -> List[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("SELECT * FROM public.copy_sources"):
            rows = list(self.sources)
            if "enabled = TRUE" in sql:
                rows = [r for r in rows if r.get("enabled", True)]
            rows.sort(key=lambda r: r["name"])
            return [dict(r) for r in rows]

        if sql.startswith("SELECT * FROM public.copy_trades"):
            rows = list(self.trades)
            p_idx = 0
            for col in ("source_id", "status", "symbol"):
                if f"{col} = $" in sql:
                    val = params[p_idx]
                    p_idx += 1
                    rows = [r for r in rows if r.get(col) == val]
            limit = int(params[p_idx])
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return [dict(r) for r in rows[:limit]]

        raise NotImplementedError(f"InMemoryCopyPool.fetch_all: {sql!r}")


__all__ = ["InMemoryCopyPool"]
