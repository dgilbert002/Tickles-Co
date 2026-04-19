"""
shared.regime.service — Regime Service orchestrator.

:class:`RegimeService` knows how to:

  * look up a classifier by name,
  * classify a window of candles for a symbol,
  * persist the resulting :class:`~shared.regime.protocol.RegimeSignal`
    through :class:`~shared.regime.store.RegimeStore`.

The service does *not* pull candles from Postgres itself; a thin
callable (``candles_loader``) is injected so tests can provide
synthetic series without faking the whole DB layer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from shared.regime.classifiers import (
    CompositeClassifier,
    TrendClassifier,
    VolatilityClassifier,
)
from shared.regime.protocol import (
    CLASSIFIER_COMPOSITE,
    CLASSIFIER_NAMES,
    CLASSIFIER_TREND,
    CLASSIFIER_VOLATILITY,
    Candle,
    RegimeClassifier,
    REGIME_UNKNOWN,
    RegimeSignal,
)
from shared.regime.store import RegimeStateRow, RegimeStore

logger = logging.getLogger("tickles.regime.service")


CandlesLoader = Callable[
    [str, str, str, int],
    Awaitable[List[Candle]],
]


def build_classifier(name: str, params: Optional[Dict[str, Any]] = None) -> RegimeClassifier:
    """Construct a classifier by name using ``params`` as kwargs."""
    params = dict(params or {})
    if name == CLASSIFIER_TREND:
        return TrendClassifier(**params)
    if name == CLASSIFIER_VOLATILITY:
        return VolatilityClassifier(**params)
    if name == CLASSIFIER_COMPOSITE:
        trend_params = params.pop("trend", None)
        vol_params = params.pop("volatility", None)
        crash_dd = params.pop("crash_dd", 0.10)
        trend_inst = TrendClassifier(**(trend_params or {}))
        vol_inst = VolatilityClassifier(**(vol_params or {}))
        return CompositeClassifier(
            trend=trend_inst, volatility=vol_inst, crash_dd=crash_dd,
        )
    raise ValueError(
        f"unknown classifier {name!r}; expected one of {sorted(CLASSIFIER_NAMES)}"
    )


class RegimeService:
    """Glue layer between classifiers and :class:`RegimeStore`."""

    def __init__(
        self,
        store: RegimeStore,
        *,
        candles_loader: Optional[CandlesLoader] = None,
        default_classifiers: Optional[Sequence[str]] = None,
    ) -> None:
        self._store = store
        self._candles_loader = candles_loader
        self._default_classifiers: List[str] = list(
            default_classifiers or (CLASSIFIER_COMPOSITE,)
        )

    # ------------------------------------------------------------------

    async def classify_from_candles(
        self,
        candles: Sequence[Candle],
        *,
        universe: str,
        exchange: str,
        symbol: str,
        timeframe: str,
        classifier: str = CLASSIFIER_COMPOSITE,
        params: Optional[Dict[str, Any]] = None,
        persist: bool = True,
    ) -> RegimeSignal:
        """Classify a window and optionally persist the signal."""
        clf = build_classifier(classifier, params)
        signal = clf.classify(
            candles,
            universe=universe,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
        )
        if signal.regime == REGIME_UNKNOWN:
            logger.info(
                "regime classify unknown universe=%s exchange=%s symbol=%s tf=%s classifier=%s reason=%s",
                universe, exchange, symbol, timeframe, classifier, signal.reason,
            )
        if persist:
            if signal.as_of is None:
                signal.as_of = datetime.now(timezone.utc)
            await self._store.insert_state(signal)
        return signal

    async def classify_symbol(
        self,
        *,
        universe: str,
        exchange: str,
        symbol: str,
        timeframe: str,
        classifier: str = CLASSIFIER_COMPOSITE,
        params: Optional[Dict[str, Any]] = None,
        window: int = 300,
        persist: bool = True,
    ) -> RegimeSignal:
        """Pull candles via ``candles_loader`` and classify."""
        if self._candles_loader is None:
            raise RuntimeError(
                "classify_symbol requires a candles_loader; pass one to RegimeService"
            )
        candles = await self._candles_loader(exchange, symbol, timeframe, int(window))
        return await self.classify_from_candles(
            candles,
            universe=universe,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            classifier=classifier,
            params=params,
            persist=persist,
        )

    async def tick(
        self,
        *,
        universe: Optional[str] = None,
        window: int = 300,
        persist: bool = True,
    ) -> List[RegimeSignal]:
        """Run every enabled classifier config once and persist signals."""
        if self._candles_loader is None:
            raise RuntimeError("tick requires a candles_loader; pass one to RegimeService")
        configs = await self._store.list_configs(
            universe=universe, enabled_only=True,
        )
        signals: List[RegimeSignal] = []
        for cfg in configs:
            if cfg.exchange is None or cfg.symbol is None:
                logger.debug(
                    "regime tick skipping universe-level config %s/%s (needs symbol+exchange)",
                    cfg.universe, cfg.timeframe,
                )
                continue
            try:
                sig = await self.classify_symbol(
                    universe=cfg.universe,
                    exchange=cfg.exchange,
                    symbol=cfg.symbol,
                    timeframe=cfg.timeframe,
                    classifier=cfg.classifier,
                    params=cfg.params,
                    window=window,
                    persist=persist,
                )
                signals.append(sig)
            except Exception as exc:  # pragma: no cover - logged, not rethrown
                logger.exception(
                    "regime tick failed for %s/%s/%s/%s/%s: %s",
                    cfg.universe, cfg.exchange, cfg.symbol, cfg.timeframe,
                    cfg.classifier, exc,
                )
        return signals

    # ------------------------------------------------------------------

    async def current(
        self,
        **filters: Any,
    ) -> List[RegimeStateRow]:
        return await self._store.list_current(**filters)

    async def history(
        self,
        *,
        universe: str,
        exchange: str,
        symbol: str,
        timeframe: str,
        classifier: Optional[str] = None,
        limit: int = 100,
    ) -> List[RegimeStateRow]:
        return await self._store.list_history(
            universe=universe,
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            classifier=classifier,
            limit=limit,
        )


__all__ = [
    "RegimeService",
    "CandlesLoader",
    "build_classifier",
]
