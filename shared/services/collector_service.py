"""
shared.services.collector_service — adapt BaseCollector to ServiceDaemon.

Existing collectors (Discord, Telegram, RSS, TradingView, …) all
subclass :class:`shared.collectors.base.BaseCollector` and expose
an async ``collect() -> List[NewsItem]`` method plus a
``write_to_db(items, db_pool)`` method.

This module converts any one of those collectors into a generic
:class:`shared.services.daemon.ServiceDaemon` tick function so the
whole zoo can reuse the Phase 22 supervisor (backoff + heartbeat
+ graceful shutdown).

No collector code is modified — the adapter is pure composition.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from shared.services.daemon import DaemonConfig, ServiceDaemon

logger = logging.getLogger("tickles.services.collector")


DbPoolFactory = Callable[[], Awaitable[Any]]


async def run_collector_once(
    collector: Any,
    db_pool: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run one collect() + write_to_db() cycle.

    Returns a JSON-serialisable summary suitable for
    :attr:`DaemonStats.last_summary`.
    """
    items = await collector.collect()
    inserted = 0
    if db_pool is not None and items:
        inserted = await collector.write_to_db(items, db_pool)
    return {
        "ok": True,
        "collector": getattr(collector, "name", type(collector).__name__),
        "items_fetched": len(items or []),
        "items_inserted": int(inserted),
        "errors": int(getattr(collector, "errors", 0)),
    }


class CollectorServiceAdapter:
    """Wrap any BaseCollector into a :class:`ServiceDaemon`.

    Parameters
    ----------
    collector:
        An instance of :class:`shared.collectors.base.BaseCollector`
        (or anything with ``collect()`` and ``write_to_db()``).
    db_pool_factory:
        Optional async factory that returns a DB pool usable by
        ``collector.write_to_db``. If None, the adapter only runs
        ``collect()`` and reports the item count (useful for dry
        runs and for collectors that write via their own path).
    config:
        Override daemon configuration. If None, sensible defaults
        are derived from ``collector.config`` when available.
    """

    def __init__(
        self,
        collector: Any,
        db_pool_factory: Optional[DbPoolFactory] = None,
        config: Optional[DaemonConfig] = None,
    ) -> None:
        self.collector = collector
        self.db_pool_factory = db_pool_factory
        self._db_pool: Optional[Any] = None
        if config is None:
            interval = getattr(
                getattr(collector, "config", None),
                "collection_interval_seconds",
                120.0,
            )
            config = DaemonConfig(
                name=f"collector:{getattr(collector, 'name', 'unnamed')}",
                interval_seconds=float(interval or 120.0),
                jitter_seconds=3.0,
            )
        self.daemon = ServiceDaemon(config=config, tick=self._tick)

    async def _tick(self) -> Dict[str, Any]:
        if self.db_pool_factory is not None and self._db_pool is None:
            self._db_pool = await self.db_pool_factory()
        return await run_collector_once(self.collector, self._db_pool)

    # convenience pass-throughs ------------------------------------------------

    async def run_once(self) -> Dict[str, Any]:
        return await self.daemon.run_once()

    async def run_forever(self) -> None:
        await self.daemon.run_forever()

    @property
    def stats(self) -> Any:
        return self.daemon.stats
