"""In-memory async pool used by Phase 28 tests and CLI simulation."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple


class InMemoryGuardrailsPool:
    def __init__(self) -> None:
        self.rules: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self._rule_seq = itertools.count(1)
        self._event_seq = itertools.count(1)

    @staticmethod
    def _loads(val: Any) -> Dict[str, Any]:
        if val is None:
            return {}
        if isinstance(val, dict):
            return val
        try:
            return json.loads(val)
        except Exception:
            return {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    # ------------------------------------------------------------------

    async def execute(self, sql: str, params: Sequence[Any]) -> int:
        sql = sql.strip()
        if sql.startswith("UPDATE public.crash_protection_rules"):
            enabled, rule_id = params
            for r in self.rules:
                if r["id"] == int(rule_id):
                    r["enabled"] = bool(enabled)
                    r["updated_at"] = self._now()
                    return 1
            return 0
        raise NotImplementedError(f"InMemoryGuardrailsPool.execute: {sql!r}")

    async def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        n = 0
        for r in rows:
            n += await self.execute(sql, r)
        return n

    # ------------------------------------------------------------------

    async def fetch_one(
        self, sql: str, params: Sequence[Any]
    ) -> Optional[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("INSERT INTO public.crash_protection_rules"):
            (
                company_id, universe, exchange, symbol, rule_type, action,
                threshold, params_json, severity, enabled,
            ) = params
            rid = next(self._rule_seq)
            self.rules.append({
                "id": rid,
                "company_id": company_id,
                "universe": universe,
                "exchange": exchange,
                "symbol": symbol,
                "rule_type": rule_type,
                "action": action,
                "threshold": None if threshold is None else float(threshold),
                "params": self._loads(params_json),
                "severity": severity,
                "enabled": bool(enabled),
                "created_at": self._now(),
                "updated_at": self._now(),
            })
            return {"id": rid}

        if sql.startswith("INSERT INTO public.crash_protection_events"):
            (
                rule_id, company_id, universe, exchange, symbol, rule_type,
                action, status, severity, reason, metric, threshold, metadata_json,
            ) = params
            eid = next(self._event_seq)
            self.events.append({
                "id": eid,
                "rule_id": None if rule_id is None else int(rule_id),
                "company_id": company_id,
                "universe": universe,
                "exchange": exchange,
                "symbol": symbol,
                "rule_type": rule_type,
                "action": action,
                "status": status,
                "severity": severity,
                "reason": reason,
                "metric": None if metric is None else float(metric),
                "threshold": None if threshold is None else float(threshold),
                "metadata": self._loads(metadata_json),
                "ts": self._now(),
            })
            return {"id": eid}

        raise NotImplementedError(f"InMemoryGuardrailsPool.fetch_one: {sql!r}")

    # ------------------------------------------------------------------

    async def fetch_all(
        self, sql: str, params: Sequence[Any]
    ) -> List[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("SELECT * FROM public.crash_protection_rules"):
            rows = list(self.rules)
            p_idx = 0
            if "company_id = $" in sql:
                comp = params[p_idx]
                p_idx += 1
                rows = [r for r in rows if r["company_id"] in (None, comp)]
            if "rule_type = $" in sql:
                rt = params[p_idx]
                p_idx += 1
                rows = [r for r in rows if r["rule_type"] == rt]
            if "enabled = TRUE" in sql:
                rows = [r for r in rows if r["enabled"]]
            rows.sort(key=lambda r: r["id"])
            return [dict(r) for r in rows]

        if sql.startswith("SELECT * FROM public.crash_protection_events"):
            rows = list(self.events)
            p_idx = 0
            if "company_id = $" in sql:
                comp = params[p_idx]
                p_idx += 1
                rows = [r for r in rows if r["company_id"] in (None, comp)]
            if "status = $" in sql:
                status = params[p_idx]
                p_idx += 1
                rows = [r for r in rows if r["status"] == status]
            limit = int(params[p_idx])
            rows.sort(key=lambda r: r["ts"], reverse=True)
            return [dict(r) for r in rows[:limit]]

        if sql.startswith("SELECT * FROM public.crash_protection_active"):
            rows = list(self.events)
            p_idx = 0
            if "company_id = $" in sql:
                comp = params[p_idx]
                p_idx += 1
                rows = [r for r in rows if r["company_id"] in (None, comp)]
            latest: Dict[Tuple[int, str, str, str, str], Dict[str, Any]] = {}
            for r in rows:
                key = (
                    r.get("rule_id") or 0,
                    r.get("company_id") or "",
                    r.get("universe") or "",
                    r.get("exchange") or "",
                    r.get("symbol") or "",
                )
                prev = latest.get(key)
                if prev is None or (r["ts"], r["id"]) > (prev["ts"], prev["id"]):
                    latest[key] = r
            out = list(latest.values())
            if "status = $" in sql:
                status = params[p_idx]
                p_idx += 1
                out = [r for r in out if r["status"] == status]
            out.sort(key=lambda r: r["ts"], reverse=True)
            return [dict(r) for r in out]

        raise NotImplementedError(f"InMemoryGuardrailsPool.fetch_all: {sql!r}")


__all__ = ["InMemoryGuardrailsPool"]
