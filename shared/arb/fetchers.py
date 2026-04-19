"""
shared.arb.fetchers — sources of top-of-book quotes for the arbitrage
scanner.

* :class:`OfflineQuoteFetcher` — deterministic, no network. Used by
  tests and by ``arb_cli demo``.
* :class:`CcxtQuoteFetcher`  — live orderbooks via :mod:`ccxt.async_support`.
  Used by ``arb_cli scan --live``. Handles venue timeouts gracefully
  (any venue that fails just drops out of the scan, never crashes it).

The fetcher only needs to return a mapping of ``venue_name -> ArbQuote``
for one symbol at a time. That keeps the scanner pure and easy to test.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Protocol

from shared.arb.protocol import ArbQuote

LOG = logging.getLogger("tickles.arb.fetchers")


class QuoteFetcher(Protocol):
    async def fetch_top_of_book(
        self, symbol: str, venues: Iterable[str],
    ) -> Dict[str, ArbQuote]:
        ...


class OfflineQuoteFetcher:
    """Dict-backed fetcher: ``{venue: {symbol: ArbQuote}}``."""

    def __init__(
        self, book: Optional[Mapping[str, Mapping[str, ArbQuote]]] = None,
    ) -> None:
        self._book: Dict[str, Dict[str, ArbQuote]] = {
            v: dict(quotes) for v, quotes in (book or {}).items()
        }

    def set(self, venue: str, symbol: str, quote: ArbQuote) -> None:
        self._book.setdefault(venue, {})[symbol] = quote

    def clear(self) -> None:
        self._book.clear()

    async def fetch_top_of_book(
        self, symbol: str, venues: Iterable[str],
    ) -> Dict[str, ArbQuote]:
        out: Dict[str, ArbQuote] = {}
        for v in venues:
            q = self._book.get(v, {}).get(symbol)
            if q is not None:
                out[v] = q
        return out


class CcxtQuoteFetcher:
    """Live CCXT-backed fetcher.

    Uses public endpoints only (no API keys). Every call runs
    ``fetch_ticker`` in parallel across venues with a shared timeout.
    Any venue whose call raises is silently dropped from the result
    for that scan — the scanner just works with whatever succeeded.
    """

    def __init__(
        self,
        *,
        timeout_s: float = 5.0,
        exchanges: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._timeout_s = float(timeout_s)
        self._ex: Dict[str, Any] = dict(exchanges or {})

    async def _get_exchange(self, name: str) -> Optional[Any]:
        if name in self._ex:
            return self._ex[name]
        try:
            import ccxt.async_support as ccxt  # type: ignore
        except ImportError:
            LOG.error("ccxt not installed")
            return None
        klass = getattr(ccxt, name, None)
        if klass is None:
            LOG.warning("unknown venue %r", name)
            return None
        ex = klass({"enableRateLimit": True, "timeout": int(self._timeout_s * 1000)})
        self._ex[name] = ex
        return ex

    async def close(self) -> None:
        for ex in list(self._ex.values()):
            try:
                await ex.close()
            except Exception:
                pass
        self._ex.clear()

    async def fetch_top_of_book(
        self, symbol: str, venues: Iterable[str],
    ) -> Dict[str, ArbQuote]:
        names: List[str] = [v for v in venues]

        async def _one(v: str) -> Optional[ArbQuote]:
            ex = await self._get_exchange(v)
            if ex is None:
                return None
            try:
                ticker = await asyncio.wait_for(
                    ex.fetch_ticker(symbol), timeout=self._timeout_s,
                )
            except Exception as e:
                LOG.debug("fetch_ticker %s@%s failed: %s", symbol, v, e)
                return None
            bid = float(ticker.get("bid") or 0.0)
            ask = float(ticker.get("ask") or 0.0)
            if bid <= 0 or ask <= 0 or ask < bid:
                return None
            info = ticker.get("info") or {}
            bid_size = float(ticker.get("bidVolume") or info.get("bidQty") or 0.0)
            ask_size = float(ticker.get("askVolume") or info.get("askQty") or 0.0)
            return ArbQuote(
                venue=v, symbol=symbol, bid=bid, ask=ask,
                bid_size=bid_size, ask_size=ask_size,
                observed_at=datetime.now(timezone.utc),
            )

        results = await asyncio.gather(*(_one(v) for v in names),
                                        return_exceptions=False)
        out: Dict[str, ArbQuote] = {}
        for v, q in zip(names, results):
            if q is not None:
                out[v] = q
        return out


__all__ = [
    "CcxtQuoteFetcher",
    "OfflineQuoteFetcher",
    "QuoteFetcher",
]
