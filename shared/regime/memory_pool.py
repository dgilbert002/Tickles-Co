"""
shared.regime.memory_pool — in-memory async pool for Phase 27 tests.

Implements just enough of the DatabasePool contract to let
:class:`shared.regime.store.RegimeStore` and
:class:`shared.regime.service.RegimeService` run without Postgres.
"""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple


class InMemoryRegimePool:
    """Tiny async pool covering the regime layer's SQL surface."""

    def __init__(self) -> None:
        self.configs: List[Dict[str, Any]] = []
        self.states: List[Dict[str, Any]] = []
        self._config_seq = itertools.count(1)
        self._state_seq = itertools.count(1)

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

    # ------------------------------------------------------------------

    async def execute(self, sql: str, params: Sequence[Any]) -> int:
        raise NotImplementedError(f"InMemoryRegimePool.execute: {sql!r}")

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

        if sql.startswith("INSERT INTO public.regime_config"):
            universe, exchange, symbol, timeframe, classifier, params_json, enabled = params
            key: Tuple[str, Optional[str], Optional[str], str, str] = (
                universe, exchange, symbol, timeframe, classifier,
            )
            for row in self.configs:
                existing_key = (
                    row["universe"], row.get("exchange"), row.get("symbol"),
                    row["timeframe"], row["classifier"],
                )
                if existing_key == key:
                    row["params"] = self._loads(params_json)
                    row["enabled"] = bool(enabled)
                    row["updated_at"] = self._now()
                    return {"id": row["id"]}
            row_id = next(self._config_seq)
            self.configs.append({
                "id": row_id,
                "universe": universe,
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "classifier": classifier,
                "params": self._loads(params_json),
                "enabled": bool(enabled),
                "created_at": self._now(),
                "updated_at": self._now(),
            })
            return {"id": row_id}

        if sql.startswith("INSERT INTO public.regime_states"):
            (
                universe, exchange, symbol, timeframe, classifier, regime,
                confidence, trend_score, volatility, drawdown, features_json,
                sample_size, as_of, reason, metadata_json,
            ) = params
            row_id = next(self._state_seq)
            self.states.append({
                "id": row_id,
                "universe": universe,
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "classifier": classifier,
                "regime": regime,
                "confidence": float(confidence),
                "trend_score": None if trend_score is None else float(trend_score),
                "volatility": None if volatility is None else float(volatility),
                "drawdown": None if drawdown is None else float(drawdown),
                "features": self._loads(features_json),
                "sample_size": int(sample_size or 0),
                "as_of": as_of,
                "recorded_at": self._now(),
                "reason": reason,
                "metadata": self._loads(metadata_json),
            })
            return {"id": row_id}

        raise NotImplementedError(f"InMemoryRegimePool.fetch_one: {sql!r}")

    # ------------------------------------------------------------------

    async def fetch_all(
        self, sql: str, params: Sequence[Any]
    ) -> List[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("SELECT * FROM public.regime_config"):
            universe = None
            enabled_only = "enabled = TRUE" in sql
            if "universe = $1" in sql and params:
                universe = params[0]
            rows = list(self.configs)
            if universe is not None:
                rows = [r for r in rows if r["universe"] == universe]
            if enabled_only:
                rows = [r for r in rows if r["enabled"]]
            rows.sort(key=lambda r: (
                r["universe"],
                r.get("exchange") or "",
                r.get("symbol") or "",
                r["timeframe"],
                r["classifier"],
            ))
            return [dict(r) for r in rows]

        if sql.startswith("SELECT * FROM public.regime_current"):
            filters: Dict[str, Any] = {}
            keys = ["universe", "exchange", "symbol", "timeframe", "classifier"]
            idx = 0
            for key in keys:
                if f"{key} = $" in sql:
                    filters[key] = params[idx]
                    idx += 1
            latest: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
            for r in self.states:
                ok = all(r.get(k) == v for k, v in filters.items())
                if not ok:
                    continue
                k = (r["universe"], r["exchange"], r["symbol"], r["timeframe"], r["classifier"])
                prev = latest.get(k)
                if prev is None or r["as_of"] > prev["as_of"]:
                    latest[k] = r
            out = [dict(r) for r in latest.values()]
            out.sort(key=lambda r: (
                r["universe"], r["exchange"], r["symbol"], r["timeframe"], r["classifier"],
            ))
            return out

        if sql.startswith("SELECT * FROM public.regime_states"):
            universe, exchange, symbol, timeframe = params[0], params[1], params[2], params[3]
            classifier = None
            limit_idx = 4
            if "classifier = $5" in sql:
                classifier = params[4]
                limit_idx = 5
            limit = int(params[limit_idx])
            rows = [
                r for r in self.states
                if r["universe"] == universe
                and r["exchange"] == exchange
                and r["symbol"] == symbol
                and r["timeframe"] == timeframe
                and (classifier is None or r["classifier"] == classifier)
            ]
            rows.sort(key=lambda r: r["as_of"], reverse=True)
            return [dict(r) for r in rows[:limit]]

        raise NotImplementedError(f"InMemoryRegimePool.fetch_all: {sql!r}")


__all__ = ["InMemoryRegimePool"]
