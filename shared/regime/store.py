"""
shared.regime.store — async DB wrapper for Phase 27 tables.

:class:`RegimeStore` owns the SQL for ``public.regime_config`` and
``public.regime_states`` plus the ``public.regime_current`` view.
All writes are append-only. The store is intentionally thin so the
:class:`shared.regime.service.RegimeService` can be tested in
isolation.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from shared.regime.protocol import RegimeSignal


@dataclass
class RegimeConfigRow:
    id: int
    universe: str
    exchange: Optional[str]
    symbol: Optional[str]
    timeframe: str
    classifier: str
    params: Dict[str, Any]
    enabled: bool
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass
class RegimeStateRow:
    id: int
    universe: str
    exchange: str
    symbol: str
    timeframe: str
    classifier: str
    regime: str
    confidence: float
    trend_score: Optional[float]
    volatility: Optional[float]
    drawdown: Optional[float]
    features: Dict[str, Any]
    sample_size: int
    as_of: datetime
    recorded_at: datetime
    reason: Optional[str]
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            "as_of": self.as_of.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
        }


def _coerce_json(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    return float(value)


class RegimeStore:
    """Async DB wrapper used by the Regime Service and CLI."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # regime_config
    # ------------------------------------------------------------------

    async def upsert_config(
        self,
        *,
        universe: str,
        exchange: Optional[str],
        symbol: Optional[str],
        timeframe: str,
        classifier: str,
        params: Optional[Dict[str, Any]] = None,
        enabled: bool = True,
    ) -> int:
        sql = (
            "INSERT INTO public.regime_config "
            "(universe, exchange, symbol, timeframe, classifier, params, enabled) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) "
            "ON CONFLICT (universe, exchange, symbol, timeframe, classifier) "
            "DO UPDATE SET params = EXCLUDED.params, enabled = EXCLUDED.enabled, "
            "updated_at = NOW() "
            "RETURNING id"
        )
        params_json = json.dumps(params or {})
        row = await self._pool.fetch_one(
            sql,
            (universe, exchange, symbol, timeframe, classifier, params_json, enabled),
        )
        return int(row["id"]) if row else 0

    async def list_configs(
        self,
        *,
        universe: Optional[str] = None,
        enabled_only: bool = True,
    ) -> List[RegimeConfigRow]:
        sql = "SELECT * FROM public.regime_config WHERE 1=1"
        params: List[Any] = []
        idx = 1
        if universe is not None:
            sql += f" AND universe = ${idx}"
            params.append(universe)
            idx += 1
        if enabled_only:
            sql += " AND enabled = TRUE"
        sql += " ORDER BY universe, exchange NULLS FIRST, symbol NULLS FIRST, timeframe, classifier"
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [self._config_from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # regime_states
    # ------------------------------------------------------------------

    async def insert_state(self, signal: RegimeSignal) -> int:
        sql = (
            "INSERT INTO public.regime_states "
            "(universe, exchange, symbol, timeframe, classifier, regime, "
            " confidence, trend_score, volatility, drawdown, features, "
            " sample_size, as_of, reason, metadata) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15) "
            "RETURNING id"
        )
        features_json = json.dumps(signal.features or {})
        metadata_json = json.dumps(signal.metadata or {})
        if signal.as_of is None:
            raise ValueError("RegimeSignal.as_of is required before insert")
        row = await self._pool.fetch_one(
            sql,
            (
                signal.universe,
                signal.exchange,
                signal.symbol,
                signal.timeframe,
                signal.classifier,
                signal.regime,
                float(signal.confidence),
                signal.trend_score,
                signal.volatility,
                signal.drawdown,
                features_json,
                int(signal.sample_size),
                signal.as_of,
                signal.reason,
                metadata_json,
            ),
        )
        return int(row["id"]) if row else 0

    async def list_current(
        self,
        *,
        universe: Optional[str] = None,
        exchange: Optional[str] = None,
        symbol: Optional[str] = None,
        timeframe: Optional[str] = None,
        classifier: Optional[str] = None,
    ) -> List[RegimeStateRow]:
        sql = "SELECT * FROM public.regime_current WHERE 1=1"
        params: List[Any] = []
        idx = 1
        for col, val in (
            ("universe", universe),
            ("exchange", exchange),
            ("symbol", symbol),
            ("timeframe", timeframe),
            ("classifier", classifier),
        ):
            if val is not None:
                sql += f" AND {col} = ${idx}"
                params.append(val)
                idx += 1
        sql += " ORDER BY universe, exchange, symbol, timeframe, classifier"
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [self._state_from_row(r) for r in rows]

    async def list_history(
        self,
        *,
        universe: str,
        exchange: str,
        symbol: str,
        timeframe: str,
        classifier: Optional[str] = None,
        limit: int = 100,
    ) -> List[RegimeStateRow]:
        sql = (
            "SELECT * FROM public.regime_states "
            "WHERE universe = $1 AND exchange = $2 AND symbol = $3 AND timeframe = $4"
        )
        params: List[Any] = [universe, exchange, symbol, timeframe]
        if classifier is not None:
            sql += " AND classifier = $5"
            params.append(classifier)
            sql += " ORDER BY as_of DESC LIMIT $6"
        else:
            sql += " ORDER BY as_of DESC LIMIT $5"
        params.append(int(limit))
        rows = await self._pool.fetch_all(sql, tuple(params))
        return [self._state_from_row(r) for r in rows]

    # ------------------------------------------------------------------

    @staticmethod
    def _config_from_row(row: Dict[str, Any]) -> RegimeConfigRow:
        return RegimeConfigRow(
            id=int(row["id"]),
            universe=row["universe"],
            exchange=row.get("exchange"),
            symbol=row.get("symbol"),
            timeframe=row["timeframe"],
            classifier=row["classifier"],
            params=_coerce_json(row.get("params")),
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _state_from_row(row: Dict[str, Any]) -> RegimeStateRow:
        return RegimeStateRow(
            id=int(row["id"]),
            universe=row["universe"],
            exchange=row["exchange"],
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            classifier=row["classifier"],
            regime=row["regime"],
            confidence=float(row["confidence"]),
            trend_score=_coerce_float(row.get("trend_score")),
            volatility=_coerce_float(row.get("volatility")),
            drawdown=_coerce_float(row.get("drawdown")),
            features=_coerce_json(row.get("features")),
            sample_size=int(row.get("sample_size") or 0),
            as_of=row["as_of"],
            recorded_at=row["recorded_at"],
            reason=row.get("reason"),
            metadata=_coerce_json(row.get("metadata")),
        )


__all__ = [
    "RegimeStore",
    "RegimeConfigRow",
    "RegimeStateRow",
]


def _sequence_guard(values: Sequence[Any]) -> Sequence[Any]:
    return values
