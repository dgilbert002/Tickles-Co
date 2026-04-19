"""In-memory pool for Phase 31 souls tests."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple


class InMemorySoulsPool:
    def __init__(self) -> None:
        self.personas: List[Dict[str, Any]] = []
        self.prompts: List[Dict[str, Any]] = []
        self.decisions: List[Dict[str, Any]] = []
        self.scout_candidates: List[Dict[str, Any]] = []
        self.optimiser_candidates: List[Dict[str, Any]] = []
        self.regime_transitions: List[Dict[str, Any]] = []
        self._p_seq = itertools.count(1)
        self._pr_seq = itertools.count(1)
        self._d_seq = itertools.count(1)
        self._sc_seq = itertools.count(1)
        self._op_seq = itertools.count(1)
        self._rt_seq = itertools.count(1)

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

    # ------------------------------------------------------------------

    async def execute(self, sql: str, params: Sequence[Any]) -> int:
        raise NotImplementedError(f"InMemorySoulsPool.execute: {sql!r}")

    # ------------------------------------------------------------------

    async def fetch_one(
        self, sql: str, params: Sequence[Any]
    ) -> Optional[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("INSERT INTO public.agent_personas"):
            (name, role, description, default_llm, enabled) = params
            for p in self.personas:
                if p["name"] == name:
                    p.update({
                        "role": role,
                        "description": description,
                        "default_llm": default_llm,
                        "enabled": bool(enabled),
                        "updated_at": self._now(),
                    })
                    return {"id": p["id"]}
            pid = next(self._p_seq)
            self.personas.append({
                "id": pid,
                "name": name,
                "role": role,
                "description": description,
                "default_llm": default_llm,
                "enabled": bool(enabled),
                "created_at": self._now(),
                "updated_at": self._now(),
            })
            return {"id": pid}

        if sql.startswith("SELECT * FROM public.agent_personas"):
            # WHERE name = $1
            if params:
                name = params[0]
                for p in self.personas:
                    if p["name"] == name:
                        return dict(p)
            return None

        if sql.startswith("INSERT INTO public.agent_prompts"):
            (persona_id, version, template, variables) = params
            for pr in self.prompts:
                if pr["persona_id"] == int(persona_id) and pr["version"] == int(version):
                    return None
            pid = next(self._pr_seq)
            self.prompts.append({
                "id": pid,
                "persona_id": int(persona_id),
                "version": int(version),
                "template": template,
                "variables": self._loads(variables),
                "created_at": self._now(),
            })
            return {"id": pid}

        if sql.startswith("INSERT INTO public.scout_candidates"):
            (company_id, universe, exchange, symbol, score, reason,
             status, correlation_id, metadata) = params
            uv = universe or ""
            cv = company_id or ""
            for row in self.scout_candidates:
                if (row["exchange"] == exchange
                        and row["symbol"] == symbol
                        and (row.get("universe") or "") == uv
                        and (row.get("company_id") or "") == cv):
                    row.update({
                        "score": float(score), "reason": reason,
                        "status": status,
                        "correlation_id": correlation_id,
                        "metadata": self._loads(metadata),
                    })
                    return {"id": row["id"]}
            rid = next(self._sc_seq)
            self.scout_candidates.append({
                "id": rid, "company_id": company_id, "universe": universe,
                "exchange": exchange, "symbol": symbol,
                "score": float(score), "reason": reason, "status": status,
                "correlation_id": correlation_id,
                "metadata": self._loads(metadata),
                "created_at": self._now(),
            })
            return {"id": rid}

        if sql.startswith("INSERT INTO public.optimiser_candidates"):
            (strategy, company_id, params_json, score, status,
             correlation_id, metadata) = params
            rid = next(self._op_seq)
            now = self._now()
            self.optimiser_candidates.append({
                "id": rid, "strategy": strategy,
                "company_id": company_id,
                "params": self._loads(params_json),
                "score": None if score is None else float(score),
                "status": status,
                "correlation_id": correlation_id,
                "metadata": self._loads(metadata),
                "created_at": now, "updated_at": now,
            })
            return {"id": rid}

        if sql.startswith("INSERT INTO public.regime_transitions"):
            (universe, exchange, symbol, timeframe, from_regime,
             to_regime, transitioned_at, confidence, metadata) = params
            rid = next(self._rt_seq)
            self.regime_transitions.append({
                "id": rid, "universe": universe, "exchange": exchange,
                "symbol": symbol, "timeframe": timeframe,
                "from_regime": from_regime, "to_regime": to_regime,
                "transitioned_at": transitioned_at,
                "confidence": float(confidence),
                "metadata": self._loads(metadata),
                "created_at": self._now(),
            })
            return {"id": rid}

        if sql.startswith("INSERT INTO public.agent_decisions"):
            (
                persona_id, company_id, correlation_id, mode, verdict,
                confidence, rationale, inputs, outputs, metadata,
            ) = params
            did = next(self._d_seq)
            self.decisions.append({
                "id": did,
                "persona_id": int(persona_id),
                "company_id": company_id,
                "correlation_id": correlation_id,
                "mode": mode,
                "verdict": verdict,
                "confidence": float(confidence),
                "rationale": rationale,
                "inputs": self._loads(inputs),
                "outputs": self._loads(outputs),
                "metadata": self._loads(metadata),
                "decided_at": self._now(),
            })
            return {"id": did}

        raise NotImplementedError(f"InMemorySoulsPool.fetch_one: {sql!r}")

    # ------------------------------------------------------------------

    async def fetch_all(
        self, sql: str, params: Sequence[Any]
    ) -> List[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("SELECT * FROM public.agent_personas"):
            rows = list(self.personas)
            if "enabled = TRUE" in sql:
                rows = [r for r in rows if r.get("enabled", True)]
            rows.sort(key=lambda r: r["name"])
            return [dict(r) for r in rows]

        if sql.startswith("SELECT * FROM public.agent_prompts"):
            (persona_id,) = params
            rows = [p for p in self.prompts if p["persona_id"] == int(persona_id)]
            rows.sort(key=lambda r: r["version"], reverse=True)
            return [dict(r) for r in rows]

        if sql.startswith("SELECT * FROM public.agent_decisions_latest"):
            rows = list(self.decisions)
            p_idx = 0
            for field_name in ("persona_id", "company_id"):
                if f"{field_name} = $" in sql:
                    val = params[p_idx]
                    p_idx += 1
                    rows = [r for r in rows if r.get(field_name) == val]
            latest: Dict[Tuple[int, str], Dict[str, Any]] = {}
            for r in rows:
                k = (r["persona_id"], r["correlation_id"])
                prev = latest.get(k)
                if prev is None or (r["decided_at"], r["id"]) > (prev["decided_at"], prev["id"]):
                    latest[k] = r
            out = list(latest.values())
            persona_lookup = {p["id"]: p["name"] for p in self.personas}
            for r in out:
                r["persona_name"] = persona_lookup.get(r["persona_id"])
            out.sort(key=lambda r: r["decided_at"], reverse=True)
            limit = int(params[p_idx])
            return [dict(r) for r in out[:limit]]

        if sql.startswith("SELECT * FROM public.scout_candidates"):
            rows = list(self.scout_candidates)
            p_idx = 0
            if "status = $" in sql:
                val = params[p_idx]
                p_idx += 1
                rows = [r for r in rows if r.get("status") == val]
            limit = int(params[p_idx])
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return [dict(r) for r in rows[:limit]]

        if sql.startswith("SELECT * FROM public.optimiser_candidates"):
            rows = list(self.optimiser_candidates)
            p_idx = 0
            for field_name in ("strategy", "status"):
                if f"{field_name} = $" in sql:
                    val = params[p_idx]
                    p_idx += 1
                    rows = [r for r in rows if r.get(field_name) == val]
            limit = int(params[p_idx])
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return [dict(r) for r in rows[:limit]]

        if sql.startswith("SELECT * FROM public.regime_transitions"):
            rows = list(self.regime_transitions)
            p_idx = 0
            for field_name in ("exchange", "symbol"):
                if f"{field_name} = $" in sql:
                    val = params[p_idx]
                    p_idx += 1
                    rows = [r for r in rows if r.get(field_name) == val]
            limit = int(params[p_idx])
            rows.sort(key=lambda r: r["transitioned_at"], reverse=True)
            return [dict(r) for r in rows[:limit]]

        if sql.startswith("SELECT * FROM public.agent_decisions"):
            rows = list(self.decisions)
            p_idx = 0
            for field_name in ("persona_id", "correlation_id", "company_id", "verdict"):
                if f"{field_name} = $" in sql:
                    val = params[p_idx]
                    p_idx += 1
                    rows = [r for r in rows if r.get(field_name) == val]
            limit = int(params[p_idx])
            rows.sort(key=lambda r: r["decided_at"], reverse=True)
            return [dict(r) for r in rows[:limit]]

        raise NotImplementedError(f"InMemorySoulsPool.fetch_all: {sql!r}")


__all__ = ["InMemorySoulsPool"]
