"""
shared.guardrails.store — DB wrapper for Phase 28 crash protection.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.guardrails.protocol import (
    ProtectionDecision,
    ProtectionRule,
    SEVERITY_WARNING,
    STATUS_TRIGGERED,
)


@dataclass
class ProtectionEventRow:
    id: int
    rule_id: Optional[int]
    company_id: Optional[str]
    universe: Optional[str]
    exchange: Optional[str]
    symbol: Optional[str]
    rule_type: str
    action: str
    status: str
    severity: str
    reason: Optional[str]
    metric: Optional[float]
    threshold: Optional[float]
    metadata: Dict[str, Any]
    ts: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "rule_id": self.rule_id,
            "company_id": self.company_id,
            "universe": self.universe,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "rule_type": self.rule_type,
            "action": self.action,
            "status": self.status,
            "severity": self.severity,
            "reason": self.reason,
            "metric": self.metric,
            "threshold": self.threshold,
            "metadata": dict(self.metadata),
            "ts": self.ts.isoformat(),
        }


def _coerce_json(val: Any) -> Dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    try:
        return json.loads(val)
    except Exception:
        return {}


def _coerce_float(val: Any) -> Optional[float]:
    return None if val is None else float(val)


class GuardrailsStore:
    """Async DB wrapper."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # rules
    # ------------------------------------------------------------------

    async def insert_rule(self, rule: ProtectionRule) -> int:
        sql = (
            "INSERT INTO public.crash_protection_rules "
            "(company_id, universe, exchange, symbol, rule_type, action, "
            " threshold, params, severity, enabled) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id"
        )
        params_json = json.dumps(rule.params or {})
        row = await self._pool.fetch_one(
            sql,
            (
                rule.company_id, rule.universe, rule.exchange, rule.symbol,
                rule.rule_type, rule.action, rule.threshold, params_json,
                rule.severity or SEVERITY_WARNING, bool(rule.enabled),
            ),
        )
        return int(row["id"]) if row else 0

    async def list_rules(
        self,
        *,
        company_id: Optional[str] = None,
        rule_type: Optional[str] = None,
        enabled_only: bool = True,
    ) -> List[ProtectionRule]:
        sql = "SELECT * FROM public.crash_protection_rules WHERE 1=1"
        params: List[Any] = []
        idx = 1
        if company_id is not None:
            sql += f" AND (company_id = ${idx} OR company_id IS NULL)"
            params.append(company_id)
            idx += 1
        if rule_type is not None:
            sql += f" AND rule_type = ${idx}"
            params.append(rule_type)
            idx += 1
        if enabled_only:
            sql += " AND enabled = TRUE"
        sql += " ORDER BY id"
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [self._rule_from_row(r) for r in rows]

    async def set_enabled(self, rule_id: int, enabled: bool) -> int:
        sql = "UPDATE public.crash_protection_rules SET enabled = $1, updated_at = NOW() WHERE id = $2"
        return await self._pool.execute(sql, (bool(enabled), int(rule_id)))

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    async def insert_event(self, decision: ProtectionDecision) -> int:
        sql = (
            "INSERT INTO public.crash_protection_events "
            "(rule_id, company_id, universe, exchange, symbol, rule_type, "
            " action, status, severity, reason, metric, threshold, metadata) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING id"
        )
        metadata_json = json.dumps(decision.metadata or {})
        row = await self._pool.fetch_one(
            sql,
            (
                decision.rule.id,
                decision.company_id,
                decision.universe,
                decision.exchange,
                decision.symbol,
                decision.rule.rule_type,
                decision.rule.action,
                decision.status,
                decision.rule.severity,
                decision.reason,
                decision.metric,
                decision.rule.threshold,
                metadata_json,
            ),
        )
        return int(row["id"]) if row else 0

    async def list_events(
        self,
        *,
        company_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> List[ProtectionEventRow]:
        sql = "SELECT * FROM public.crash_protection_events WHERE 1=1"
        params: List[Any] = []
        idx = 1
        if company_id is not None:
            sql += f" AND (company_id = ${idx} OR company_id IS NULL)"
            params.append(company_id)
            idx += 1
        if status is not None:
            sql += f" AND status = ${idx}"
            params.append(status)
            idx += 1
        sql += f" ORDER BY ts DESC LIMIT ${idx}"
        params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [self._event_from_row(r) for r in rows]

    async def list_active(
        self,
        *,
        company_id: Optional[str] = None,
        triggered_only: bool = True,
    ) -> List[ProtectionEventRow]:
        sql = "SELECT * FROM public.crash_protection_active WHERE 1=1"
        params: List[Any] = []
        idx = 1
        if company_id is not None:
            sql += f" AND (company_id = ${idx} OR company_id IS NULL)"
            params.append(company_id)
            idx += 1
        if triggered_only:
            sql += f" AND status = ${idx}"
            params.append(STATUS_TRIGGERED)
            idx += 1
        sql += " ORDER BY ts DESC"
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [self._event_from_row(r) for r in rows]

    # ------------------------------------------------------------------

    @staticmethod
    def _rule_from_row(row: Dict[str, Any]) -> ProtectionRule:
        return ProtectionRule(
            id=int(row["id"]),
            company_id=row.get("company_id"),
            universe=row.get("universe"),
            exchange=row.get("exchange"),
            symbol=row.get("symbol"),
            rule_type=row["rule_type"],
            action=row["action"],
            threshold=_coerce_float(row.get("threshold")),
            params=_coerce_json(row.get("params")),
            severity=row.get("severity") or SEVERITY_WARNING,
            enabled=bool(row.get("enabled", True)),
        )

    @staticmethod
    def _event_from_row(row: Dict[str, Any]) -> ProtectionEventRow:
        return ProtectionEventRow(
            id=int(row["id"]),
            rule_id=None if row.get("rule_id") is None else int(row["rule_id"]),
            company_id=row.get("company_id"),
            universe=row.get("universe"),
            exchange=row.get("exchange"),
            symbol=row.get("symbol"),
            rule_type=row["rule_type"],
            action=row["action"],
            status=row["status"],
            severity=row["severity"],
            reason=row.get("reason"),
            metric=_coerce_float(row.get("metric")),
            threshold=_coerce_float(row.get("threshold")),
            metadata=_coerce_json(row.get("metadata")),
            ts=row["ts"],
        )


__all__ = [
    "GuardrailsStore",
    "ProtectionEventRow",
]
