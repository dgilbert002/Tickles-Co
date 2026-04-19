"""
shared.arb.service — orchestrates the arb scanner.

``ArbService.scan_symbols()`` runs one scan tick across symbols, persists
every opportunity above the threshold, and returns them for the caller.
Designed so callers (CLI, agents, strategy composer) don't have to care
whether the quote source is live CCXT or a dict stub.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, List, Optional

from shared.arb.fetchers import QuoteFetcher
from shared.arb.protocol import ArbOpportunity, ArbVenue
from shared.arb.scanner import ArbScanner, ScannerConfig
from shared.arb.store import ArbStore

LOG = logging.getLogger("tickles.arb.service")


@dataclass
class ArbServiceConfig:
    auto_persist: bool = True


class ArbService:
    def __init__(
        self,
        store: ArbStore,
        fetcher: QuoteFetcher,
        *,
        venues: Optional[Iterable[ArbVenue]] = None,
        scanner_config: Optional[ScannerConfig] = None,
        service_config: Optional[ArbServiceConfig] = None,
    ) -> None:
        self._store = store
        self._fetcher = fetcher
        self._config = service_config or ArbServiceConfig()
        self._scanner_config = scanner_config or ScannerConfig()
        self._explicit_venues = list(venues) if venues is not None else None
        self._scanner: Optional[ArbScanner] = None

    async def _get_scanner(self) -> ArbScanner:
        if self._scanner is not None:
            return self._scanner
        if self._explicit_venues is not None:
            venues = self._explicit_venues
        else:
            venues = await self._store.list_venues(enabled_only=True)
        self._scanner = ArbScanner(
            venues=venues, fetcher=self._fetcher,
            config=self._scanner_config,
        )
        return self._scanner

    def invalidate_scanner(self) -> None:
        self._scanner = None

    async def scan_symbols(
        self,
        symbols: Iterable[str],
        *,
        correlation_id: Optional[str] = None,
        company_id: Optional[str] = None,
        persist: Optional[bool] = None,
    ) -> List[ArbOpportunity]:
        scanner = await self._get_scanner()
        opps = await scanner.scan_many(
            symbols, correlation_id=correlation_id, company_id=company_id,
        )
        if persist is None:
            persist = self._config.auto_persist
        if persist and opps:
            for o in opps:
                try:
                    new_id = await self._store.record_opportunity(o)
                    o.id = new_id or o.id
                except Exception as e:  # pragma: no cover - best effort
                    LOG.warning("persist arb opp failed: %s", e)
        return opps

    async def list_recent(
        self, *, symbol: Optional[str] = None,
        min_net_bps: Optional[float] = None, limit: int = 50,
    ) -> List[ArbOpportunity]:
        return await self._store.list_opportunities(
            symbol=symbol, min_net_bps=min_net_bps, limit=limit,
        )

    async def close(self) -> None:
        close = getattr(self._fetcher, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # pragma: no cover
                pass


__all__ = ["ArbService", "ArbServiceConfig"]
