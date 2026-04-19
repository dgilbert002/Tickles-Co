"""shared.souls.store — DB wrapper for Phase 31 souls."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.souls.protocol import (
    MODE_DETERMINISTIC,
    SoulDecision,
    SoulPersona,
    SoulPrompt,
)


def _coerce_json(val: Any) -> Dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    try:
        return json.loads(val)
    except Exception:
        return {}


def _coerce_json_list(val: Any) -> List[Any]:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


@dataclass
class SoulDecisionRow:
    id: int
    persona_id: int
    persona_name: Optional[str]
    company_id: Optional[str]
    correlation_id: str
    mode: str
    verdict: str
    confidence: float
    rationale: Optional[str]
    inputs: Dict[str, Any]
    outputs: Dict[str, Any]
    metadata: Dict[str, Any]
    decided_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "persona_id": self.persona_id,
            "persona_name": self.persona_name,
            "company_id": self.company_id,
            "correlation_id": self.correlation_id,
            "mode": self.mode,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "inputs": dict(self.inputs),
            "outputs": dict(self.outputs),
            "metadata": dict(self.metadata),
            "decided_at": self.decided_at.isoformat(),
        }


class SoulsStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # Personas
    # ------------------------------------------------------------------

    async def upsert_persona(self, persona: SoulPersona) -> int:
        sql = (
            "INSERT INTO public.agent_personas "
            "(name, role, description, default_llm, enabled) "
            "VALUES ($1,$2,$3,$4,$5) "
            "ON CONFLICT (name) DO UPDATE SET "
            "  role = EXCLUDED.role, "
            "  description = EXCLUDED.description, "
            "  default_llm = EXCLUDED.default_llm, "
            "  enabled = EXCLUDED.enabled, "
            "  updated_at = NOW() "
            "RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (persona.name, persona.role, persona.description,
             persona.default_llm, bool(persona.enabled)),
        )
        rid = int(row["id"]) if row else 0
        if rid:
            persona.id = rid
        return rid

    async def list_personas(self, *, enabled_only: bool = False) -> List[SoulPersona]:
        sql = "SELECT * FROM public.agent_personas WHERE 1=1"
        params: List[Any] = []
        if enabled_only:
            sql += " AND enabled = TRUE"
        sql += " ORDER BY name"
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [
            SoulPersona(
                id=int(r["id"]), name=r["name"], role=r["role"],
                description=r.get("description"),
                default_llm=r.get("default_llm"),
                enabled=bool(r.get("enabled", True)),
            )
            for r in rows
        ]

    async def get_persona(self, name: str) -> Optional[SoulPersona]:
        sql = "SELECT * FROM public.agent_personas WHERE name = $1"
        row = await self._pool.fetch_one(sql, (name,))
        if not row:
            return None
        return SoulPersona(
            id=int(row["id"]), name=row["name"], role=row["role"],
            description=row.get("description"),
            default_llm=row.get("default_llm"),
            enabled=bool(row.get("enabled", True)),
        )

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    async def add_prompt(self, prompt: SoulPrompt) -> int:
        sql = (
            "INSERT INTO public.agent_prompts "
            "(persona_id, version, template, variables) "
            "VALUES ($1,$2,$3,$4) "
            "ON CONFLICT (persona_id, version) DO NOTHING "
            "RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (int(prompt.persona_id), int(prompt.version), prompt.template,
             json.dumps(list(prompt.variables or []))),
        )
        return int(row["id"]) if row and "id" in row else 0

    async def list_prompts(self, persona_id: int) -> List[SoulPrompt]:
        sql = "SELECT * FROM public.agent_prompts WHERE persona_id = $1 ORDER BY version DESC"
        rows = await self._pool.fetch_all(sql, (int(persona_id),))
        return [
            SoulPrompt(
                id=int(r["id"]), persona_id=int(r["persona_id"]),
                version=int(r["version"]), template=r["template"],
                variables=_coerce_json_list(r.get("variables")),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    async def record_decision(
        self,
        persona_id: int,
        decision: SoulDecision,
        inputs: Dict[str, Any],
    ) -> int:
        sql = (
            "INSERT INTO public.agent_decisions "
            "(persona_id, company_id, correlation_id, mode, verdict, "
            " confidence, rationale, inputs, outputs, metadata) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id"
        )
        row = await self._pool.fetch_one(
            sql,
            (
                int(persona_id), decision.company_id, decision.correlation_id,
                decision.mode or MODE_DETERMINISTIC, decision.verdict,
                float(decision.confidence), decision.rationale,
                json.dumps(inputs or {}),
                json.dumps(decision.outputs or {}),
                json.dumps(decision.metadata or {}),
            ),
        )
        return int(row["id"]) if row and "id" in row else 0

    async def list_decisions(
        self,
        *,
        persona_id: Optional[int] = None,
        correlation_id: Optional[str] = None,
        company_id: Optional[str] = None,
        verdict: Optional[str] = None,
        limit: int = 100,
    ) -> List[SoulDecisionRow]:
        sql = "SELECT * FROM public.agent_decisions WHERE 1=1"
        params: List[Any] = []
        idx = 1
        for field_name, val in (
            ("persona_id", persona_id),
            ("correlation_id", correlation_id),
            ("company_id", company_id),
            ("verdict", verdict),
        ):
            if val is not None:
                sql += f" AND {field_name} = ${idx}"
                params.append(val)
                idx += 1
        sql += f" ORDER BY decided_at DESC LIMIT ${idx}"
        params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [self._decision_from_row(r) for r in rows]

    async def list_latest_per_correlation(
        self,
        *,
        persona_id: Optional[int] = None,
        company_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[SoulDecisionRow]:
        sql = "SELECT * FROM public.agent_decisions_latest WHERE 1=1"
        params: List[Any] = []
        idx = 1
        for field_name, val in (
            ("persona_id", persona_id),
            ("company_id", company_id),
        ):
            if val is not None:
                sql += f" AND {field_name} = ${idx}"
                params.append(val)
                idx += 1
        sql += f" ORDER BY decided_at DESC LIMIT ${idx}"
        params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [self._decision_from_row(r) for r in rows]

    # ------------------------------------------------------------------

    @staticmethod
    def _decision_from_row(row: Dict[str, Any]) -> SoulDecisionRow:
        return SoulDecisionRow(
            id=int(row["id"]),
            persona_id=int(row["persona_id"]),
            persona_name=row.get("persona_name"),
            company_id=row.get("company_id"),
            correlation_id=row["correlation_id"],
            mode=row["mode"],
            verdict=row["verdict"],
            confidence=float(row.get("confidence") or 0.0),
            rationale=row.get("rationale"),
            inputs=_coerce_json(row.get("inputs")),
            outputs=_coerce_json(row.get("outputs")),
            metadata=_coerce_json(row.get("metadata")),
            decided_at=row["decided_at"],
        )


__all__ = ["SoulsStore", "SoulDecisionRow"]
