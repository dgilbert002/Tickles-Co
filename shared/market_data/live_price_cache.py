"""
Module: live_price_cache
Purpose: Best-effort live-price fallback for the dashboard snapshot when the
         local candle DB has no recent bar for a symbol.

         Wraps :func:`shared.market_data.live_price.fetch_live_price` with
         three behaviours the snapshot needs:

         1. **In-memory TTL cache.** A successful probe is cached for 30s
            so concurrent ``/api/snapshot`` calls during the same window
            do not hammer the exchanges.
         2. **Cross-exchange fallback chain.** If Bybit doesn't quote a
            symbol (e.g. low-cap perps live on BloFin), try the next venue
            until one resolves or the chain is exhausted.
         3. **Hard total budget.** Each ``fetch_many`` call is bounded by
            ``asyncio.wait_for`` so a slow exchange can never inflate
            snapshot latency past the 10s TTL.

         Returns a ``Dict[symbol_input -> price_float]``. Symbols that
         could not be resolved are simply absent — callers should treat
         ``dict.get(sym)`` as the only correct access pattern.

Location: /opt/tickles/shared/market_data/live_price_cache.py

Used by:
  * ``shared.dashboard.snapshot._aggregate_pending_positions`` — fills
    ``current_price`` for pending tracked_positions whose symbol isn't
    in the candle table yet.
  * The MCP ``instruments.refresh`` tool (Phase 5) — preview pass.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, Iterable, List, Tuple

from shared.market_data.live_price import (
    LivePriceError,
    UnsupportedExchangeError,
    _candidate_symbols,
    _extract_price,
    fetch_live_price,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Per-exchange probe budget. fetch_live_price has its own retry loop across
# symbol form variants, so 2.5s is generous for one venue.
_PER_EXCHANGE_TIMEOUT_S: float = float(
    os.environ.get("LIVE_PRICE_PER_EX_TIMEOUT_S", "2.5")
)

# Cache TTL — snapshot itself caches for 10s, so 30s here means a stale
# fallback price gets re-fetched roughly every 3rd snapshot.
_CACHE_TTL_S: float = float(os.environ.get("LIVE_PRICE_CACHE_TTL_S", "30"))

# Order matters: traders sign Bybit, BloFin is perps-only with great alt
# coverage, then bitget/binance/okx as additional venues. Keep <= 5 to
# bound worst-case probe count per missing symbol.
_DEFAULT_EXCHANGE_CHAIN: Tuple[str, ...] = (
    "bybit",
    "blofin",
    "bitget",
    "binance",
    "okx",
)

# Hard ceiling for a single fetch_many() call. Exchanges run in parallel,
# so total wall-clock ≈ max(per_exchange_time). Each exchange's first call
# triggers a ``load_markets()`` (slowest part — Bybit / Binance return
# multi-MB JSON), so 12s is enough for the cold path while the cache TTL
# (30s) makes every subsequent snapshot effectively free.
_TOTAL_BUDGET_S: float = float(os.environ.get("LIVE_PRICE_TOTAL_BUDGET_S", "12.0"))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
# Module-level dict: symbol -> (cached_at_monotonic_s, price_float).
_CACHE: Dict[str, Tuple[float, float]] = {}


def _cache_get(symbol: str) -> float | None:
    """Return a cached price for ``symbol`` if still fresh, else None."""
    entry = _CACHE.get(symbol)
    if entry is None:
        return None
    cached_at, price = entry
    if (time.monotonic() - cached_at) > _CACHE_TTL_S:
        return None
    return price


def _cache_put(symbol: str, price: float) -> None:
    """Store a freshly-probed price."""
    _CACHE[symbol] = (time.monotonic(), price)


def clear_cache() -> None:
    """Drop all cached entries. Test-only helper."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Shared CCXT client per fetch_many() call
# ---------------------------------------------------------------------------
# Why this exists:
#     ``shared.market_data.live_price.fetch_live_price`` creates a fresh
#     CCXT client on every call. CCXT lazy-loads markets on first
#     ``fetch_ticker``, which on Bybit means ~3400 instrument records
#     (a multi-MB JSON). When 20+ symbols probe concurrently, that's 20+
#     parallel ``load_markets`` requests — enough to blow the 6s snapshot
#     budget every time.
#
#     The batch path builds ONE client per exchange, calls ``load_markets``
#     once, then reuses the client for every ticker lookup. Clients are
#     closed in a try/finally so we don't leak aiohttp sessions across
#     ``fetch_many`` invocations.


async def _build_client(ccxt_async, exchange: str):
    """Return a CCXT async client for ``exchange``, or ``None`` if not supported."""
    # Some venues alias under CCXT (htx vs huobi, gate vs gateio).
    ccxt_id = "huobi" if exchange == "htx" else ("gateio" if exchange == "gate" else exchange)
    cls = getattr(ccxt_async, ccxt_id, None)
    if cls is None:
        return None
    return cls({"enableRateLimit": True})


async def _probe_with_client(client, symbol: str) -> float | None:
    """One-shot ticker lookup using an already-built CCXT client.

    Tries each symbol form variant against the shared client. Returns the
    first resolved price, or ``None`` if every variant failed.
    """
    try:
        from ccxt.base.errors import BadSymbol, ExchangeError, NetworkError  # type: ignore
    except ImportError:  # pragma: no cover — defensive
        BadSymbol = Exception  # type: ignore
        ExchangeError = Exception  # type: ignore
        NetworkError = Exception  # type: ignore

    for cand in _candidate_symbols(symbol):
        try:
            ticker = await asyncio.wait_for(
                client.fetch_ticker(cand), timeout=_PER_EXCHANGE_TIMEOUT_S,
            )
        except (BadSymbol, NetworkError, ExchangeError, asyncio.TimeoutError):
            continue
        except Exception:  # noqa: BLE001 — best-effort
            continue
        px = _extract_price(ticker)
        if px and px > 0:
            return float(px)
    return None


async def _probe_one_exchange_batch(
    ccxt_async, exchange: str, symbols: List[str]
) -> Dict[str, float]:
    """Resolve as many of ``symbols`` as possible on ONE exchange.

    Builds the CCXT client once, runs ``load_markets`` once, then
    probes every symbol in parallel against that single client.

    Returns:
        ``{symbol: price}`` for the symbols this exchange could quote.
    """
    out: Dict[str, float] = {}
    client = await _build_client(ccxt_async, exchange)
    if client is None:
        return out
    try:
        # Force a single market-load up-front so concurrent tickers don't
        # each race to load the same markets payload.
        try:
            await asyncio.wait_for(client.load_markets(), timeout=8.0)
        except asyncio.TimeoutError:
            logger.info("live-price: %s load_markets timed out", exchange)
            return out
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug("live-price: %s load_markets failed: %s", exchange, exc)
            return out

        async def _one(sym: str) -> Tuple[str, float | None]:
            px = await _probe_with_client(client, sym)
            return sym, px

        results = await asyncio.gather(
            *(_one(s) for s in symbols), return_exceptions=True,
        )
        for r in results:
            if isinstance(r, BaseException):
                continue
            sym, px = r
            if px is not None:
                out[sym] = px
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 — cleanup must not raise
            pass
    return out


# ---------------------------------------------------------------------------
# Batch API (what snapshot.py calls)
# ---------------------------------------------------------------------------
async def fetch_many(
    symbols: Iterable[str],
    *,
    exchanges: Iterable[str] | None = None,
    total_budget_s: float | None = None,
) -> Dict[str, float]:
    """Resolve a batch of symbols to live prices, best-effort.

    Strategy:
      1. **Cache-first.** Anything fresh in ``_CACHE`` returns immediately.
      2. **Per-exchange concurrency.** Each exchange in ``chain`` becomes
         its own task; each task builds ONE CCXT client, calls
         ``load_markets`` once, then probes every pending symbol against
         that single client. Far cheaper than spawning a client per
         (exchange, symbol) pair.
      3. **All exchanges race in parallel.** Wall-clock ≈ ``max`` of the
         per-exchange times, not their sum.
      4. **Exchange preference breaks ties on merge.** If two venues both
         quote a symbol, the venue earlier in ``chain`` wins. This keeps
         Bybit's price authoritative when present (matches trader-feed
         attribution).
      5. **Hard wall-clock budget.** The whole call is bounded by
         ``total_budget_s`` so the snapshot can never block longer than
         that. Tasks still running at deadline are cancelled.

    Args:
        symbols: Symbol strings the caller wants prices for.
        exchanges: Override the default exchange chain. Useful for the
            MCP tool's "bybit-only" mode.
        total_budget_s: Override the total wall-clock budget.

    Returns:
        ``{symbol: price}`` — only contains entries that resolved.
        Callers MUST treat the absence of a key as "no live price".
    """
    sym_list: List[str] = [s for s in (symbols or []) if s]
    if not sym_list:
        return {}

    chain = tuple(exchanges) if exchanges else _DEFAULT_EXCHANGE_CHAIN
    budget = total_budget_s if total_budget_s is not None else _TOTAL_BUDGET_S

    out: Dict[str, float] = {}
    pending: List[str] = []
    for s in sym_list:
        cached = _cache_get(s)
        if cached is not None:
            out[s] = cached
        else:
            pending.append(s)

    if not pending:
        return out

    try:
        import ccxt.async_support as ccxt_async  # type: ignore
    except ImportError:
        logger.debug("ccxt.async_support not importable; skipping fallback")
        return out

    # Run all exchanges in parallel. Each exchange task builds its own client,
    # loads markets once, and probes every pending symbol against it. Wall-
    # clock time is bounded by ``max(per_exchange_time)``, not their sum.
    # Symbols that resolve on multiple exchanges use the exchange-chain
    # preference order: the FIRST exchange in ``chain`` to produce a price
    # wins (so trader-feed Bybit prices remain authoritative when available).
    async def _run_one(exchange: str) -> Dict[str, float]:
        try:
            return await _probe_one_exchange_batch(ccxt_async, exchange, pending)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("live-price %s probe raised: %s", exchange, exc)
            return {}

    tasks = {ex: asyncio.create_task(_run_one(ex)) for ex in chain}

    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks.values(), return_exceptions=True),
            timeout=budget,
        )
    except asyncio.TimeoutError:
        for t in tasks.values():
            if not t.done():
                t.cancel()
        # Let cancellations settle; swallow exceptions.
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        logger.info(
            "live_price_cache.fetch_many: budget %.2fs exceeded — using "
            "whatever resolved within budget",
            budget,
        )

    # Merge results respecting exchange-chain preference. Earlier in ``chain``
    # wins ties so Bybit's price beats BloFin's for the same symbol.
    per_exchange_counts: Dict[str, int] = {}
    for ex in chain:
        task = tasks.get(ex)
        if task is None or not task.done() or task.cancelled():
            continue
        try:
            resolved = task.result() or {}
        except Exception:  # noqa: BLE001 — best-effort
            resolved = {}
        per_exchange_counts[ex] = len(resolved)
        for sym, px in resolved.items():
            if sym in out:
                continue  # earlier-preferred exchange already filled this
            out[sym] = px
            _cache_put(sym, px)

    if per_exchange_counts:
        logger.info(
            "live-price parallel resolve: %s (total resolved: %d of %d)",
            per_exchange_counts, sum(1 for s in pending if s in out), len(pending),
        )

    return out


__all__ = [
    "fetch_many",
    "clear_cache",
]
