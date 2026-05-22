"""
Module: price_routes
Purpose: Dashboard ``GET /api/price`` route returning the live last-price for
         a symbol on a supported exchange. Wraps
         :func:`shared.market_data.live_price.fetch_live_price` with a tiny
         in-process TTL cache and a per-bucket token-bucket rate limiter so
         the drawer's "live market" section does not hammer CCXT.
Location: /opt/tickles/shared/dashboard/price_routes.py

Design:
  * Cache: ``{(exchange, symbol_norm): (LivePriceResult, monotonic_at)}``,
    served back to clients while ``now - monotonic_at < _CACHE_TTL_S``.
  * Rate limit: deque of monotonic timestamps per bucket key (session
    cookie if present, else peer IP). Same shape as the media-proxy
    limiter but scoped to this module.
  * Symbol whitelist: a regex permitting the small set of forms CCXT
    accepts ("BTC/USDT", "BTCUSDT", "BTC/USDT:USDT", "BTCUSDT.P").
  * No I/O on import.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

from aiohttp import web

from shared.market_data.live_price import (
    DEFAULT_TIMEOUT_S,
    SUPPORTED_EXCHANGES,
    LivePriceError,
    LivePriceResult,
    UnsupportedExchangeError,
    fetch_live_price,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Cache TTL — drawer polls roughly once on open, so 30s is plenty.
_CACHE_TTL_S: float = 30.0

# Rate-limit policy: 60 requests / 60s per bucket (increased from 30
# to accommodate dashboard polling + drawer usage without 429s).
_RATE_LIMIT_MAX_REQUESTS: int = 60
_RATE_LIMIT_WINDOW_S: float = 60.0

# Symbol whitelist. Matches: BTC/USDT, BTCUSDT, BTC/USDT:USDT, BTCUSDT.P.
# Allows alphanumerics, slash, colon, and one trailing ".P".
_SYMBOL_RE: re.Pattern = re.compile(
    r"^[A-Z0-9]{2,15}(?:/[A-Z0-9]{2,10}(?::[A-Z0-9]{2,10})?)?(?:\.P)?$"
)

# Crypto quote currencies that CCXT exchanges actually support.
# Symbols not ending with one of these (or having a slash with one of these)
# are stocks, forex, or indices — reject early with 400.
_CRYPTO_QUOTES: frozenset = frozenset({
    "USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "DAI", "TUSD", "USDP", "FDUSD",
    "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD",
})


def _is_crypto_symbol(symbol: str) -> bool:
    """Return True if *symbol* looks like a crypto trading pair CCXT can resolve.

    Args:
        symbol: Already normalised (upper-cased, trimmed) symbol.

    Returns:
        ``True`` for crypto-like pairs, ``False`` for stocks, forex, indices.
    """
    if "/" in symbol:
        parts = symbol.split("/")
        if len(parts) == 2:
            quote = parts[1].split(":")[0]
            return quote in _CRYPTO_QUOTES
        return False
    for q in _CRYPTO_QUOTES:
        if symbol.endswith(q) and len(symbol) > len(q):
            return True
    return False

# Per-call CCXT timeout (seconds). Independent of cache.
_CCXT_TIMEOUT_S: float = DEFAULT_TIMEOUT_S


# ---------------------------------------------------------------------------
# Module state (cache + rate-limit buckets)
# ---------------------------------------------------------------------------
_CACHE: Dict[Tuple[str, str], Tuple[LivePriceResult, float]] = {}
_CACHE_LOCK: asyncio.Lock = asyncio.Lock()

_RATE_LIMIT_BUCKETS: Dict[str, Deque[float]] = {}
_RATE_LIMIT_LOCK: asyncio.Lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _err(status: int, message: str, *, retry_after: Optional[int] = None) -> web.Response:
    """Return a small JSON error response.

    Args:
        status: HTTP status code.
        message: User-facing error message.
        retry_after: Optional retry-after seconds for 429 responses.

    Returns:
        ``web.Response`` with JSON body ``{"error": <message>}``.
    """
    headers: Dict[str, str] = {}
    if retry_after is not None:
        headers["Retry-After"] = str(int(retry_after))
    return web.json_response(
        {"error": message},
        status=status,
        headers=headers,
    )


def _normalise_symbol(raw: Optional[str]) -> Optional[str]:
    """Validate and normalise a user-supplied symbol.

    Args:
        raw: The raw query-string value.

    Returns:
        The trimmed, upper-cased symbol if it matches :data:`_SYMBOL_RE`,
        otherwise ``None``.
    """
    if not raw:
        return None
    s = raw.strip().upper()
    if not s or len(s) > 30:
        return None
    if not _SYMBOL_RE.match(s):
        return None
    return s


def _normalise_exchange(raw: Optional[str]) -> Optional[str]:
    """Validate and normalise a user-supplied exchange id.

    Args:
        raw: The raw query-string value.

    Returns:
        The lower-cased exchange id if it is in
        :data:`shared.market_data.live_price.SUPPORTED_EXCHANGES`, otherwise
        ``None``.
    """
    if not raw:
        return None
    s = raw.strip().lower()
    if s not in SUPPORTED_EXCHANGES:
        return None
    return s


def _rate_limit_key(request: web.Request) -> str:
    """Return a stable identifier for the rate-limit bucket.

    Prefers the dashboard session cookie so a single browser session is one
    bucket regardless of changing client IPs. Falls back to the peer IP.

    Args:
        request: The aiohttp request object.

    Returns:
        Bucket key string.
    """
    cookie = request.cookies.get("__Host-session")
    if cookie:
        return f"sess:{cookie[:32]}"
    peer = (
        request.transport.get_extra_info("peername")
        if request.transport
        else None
    )
    return f"ip:{peer[0]}" if peer else "ip:unknown"


async def _check_rate_limit(key: str) -> Optional[int]:
    """Apply the token-bucket policy. Return retry-after on rejection.

    Args:
        key: Bucket key from :func:`_rate_limit_key`.

    Returns:
        ``None`` when allowed. Otherwise the integer retry-after seconds.
    """
    now = time.monotonic()
    cutoff = now - _RATE_LIMIT_WINDOW_S
    async with _RATE_LIMIT_LOCK:
        bucket = _RATE_LIMIT_BUCKETS.get(key)
        if bucket is None:
            bucket = deque()
            _RATE_LIMIT_BUCKETS[key] = bucket
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= _RATE_LIMIT_MAX_REQUESTS:
            oldest = bucket[0]
            retry_after = max(1, int(oldest + _RATE_LIMIT_WINDOW_S - now) + 1)
            return retry_after
        bucket.append(now)
        # Opportunistic eviction so _RATE_LIMIT_BUCKETS cannot grow
        # O(n_unique_clients) over the daemon's lifetime.
        _evict_empty_buckets(cutoff, max_evictions=8, exclude=key)
        return None


def _evict_empty_buckets(
    cutoff: float, *, max_evictions: int, exclude: str,
) -> None:
    """Prune empty rate-limit buckets so the dict cannot grow forever.

    Args:
        cutoff: Monotonic-time cutoff; entries older than this are stale.
        max_evictions: Hard cap on keys removed in one call to keep work
            bounded under the rate-limit lock.
        exclude: Key that must not be evicted (the caller's own bucket).
    """
    removed = 0
    for k, dq in list(_RATE_LIMIT_BUCKETS.items()):
        if removed >= max_evictions:
            return
        if k == exclude:
            continue
        while dq and dq[0] < cutoff:
            dq.popleft()
        if not dq:
            _RATE_LIMIT_BUCKETS.pop(k, None)
            removed += 1


async def _cache_get(exchange: str, symbol: str) -> Optional[LivePriceResult]:
    """Return a cached :class:`LivePriceResult` if still fresh.

    Args:
        exchange: Normalised exchange id.
        symbol: Normalised symbol.

    Returns:
        The cached result if still within :data:`_CACHE_TTL_S`, else ``None``.
    """
    now = time.monotonic()
    async with _CACHE_LOCK:
        entry = _CACHE.get((exchange, symbol))
        if entry is None:
            return None
        result, stored_at = entry
        if now - stored_at > _CACHE_TTL_S:
            _CACHE.pop((exchange, symbol), None)
            return None
        return result


async def _cache_set(
    exchange: str, symbol: str, result: LivePriceResult,
) -> None:
    """Store a :class:`LivePriceResult` in the cache.

    Args:
        exchange: Normalised exchange id.
        symbol: Normalised symbol (the one the user requested).
        result: Result to cache.
    """
    async with _CACHE_LOCK:
        _CACHE[(exchange, symbol)] = (result, time.monotonic())


def _reset_state() -> None:
    """Clear cache and rate-limit buckets (test-only helper)."""
    _CACHE.clear()
    _RATE_LIMIT_BUCKETS.clear()


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------
async def handle_price(request: web.Request) -> web.Response:
    """``GET /api/price?symbol=<SYM>&exchange=<EXCH>`` — live last-price.

    Query params:
      * ``symbol``   — required. e.g. ``BTC/USDT``, ``BTCUSDT``, ``BTC/USDT:USDT``.
      * ``exchange`` — optional, default ``bybit``. Must be in
        :data:`shared.market_data.live_price.SUPPORTED_EXCHANGES`.

    Response (200):
        ``{"symbol": str, "price": float, "exchange": str, "ts": int,
           "cached": bool}``

    Errors:
        * 400 — bad/missing symbol or unsupported exchange.
        * 429 — per-session rate limit exceeded (Retry-After header set).
        * 502 — CCXT probe failed.
        * 504 — CCXT probe exceeded its timeout.
    """
    symbol = _normalise_symbol(request.query.get("symbol"))
    if symbol is None:
        return _err(400, "symbol is required and must match [A-Z0-9/:.] pattern")
    if not _is_crypto_symbol(symbol):
        return _err(400, "unsupported symbol for live price")

    raw_exch = request.query.get("exchange") or "bybit"
    exchange = _normalise_exchange(raw_exch)
    if exchange is None:
        return _err(400, f"unsupported exchange: {raw_exch}")

    try:
        retry_after = await _check_rate_limit(_rate_limit_key(request))
    except Exception as exc:
        logger.exception("price route: rate-limit check failed: %s", exc)
        return _err(500, "internal error")

    if retry_after is not None:
        return _err(429, "rate limit exceeded", retry_after=retry_after)

    cached = await _cache_get(exchange, symbol)
    if cached is not None:
        return web.json_response(_payload(cached, exchange, cached_flag=True))

    try:
        result = await fetch_live_price(
            symbol, exchange, timeout_s=_CCXT_TIMEOUT_S,
        )
    except UnsupportedExchangeError as exc:
        # Should be unreachable due to whitelist, but keep belt+braces.
        logger.warning("price route: unsupported exchange %s: %s", exchange, exc)
        return _err(400, f"unsupported exchange: {exchange}")
    except asyncio.TimeoutError:
        logger.info("price route: ccxt timeout for %s@%s", symbol, exchange)
        return _err(504, "upstream timeout")
    except LivePriceError as exc:
        logger.info("price route: ccxt error for %s@%s: %s", symbol, exchange, exc)
        return _err(502, "upstream error")
    except Exception as exc:
        logger.warning(
            "price route: unexpected error for %s@%s: %s", symbol, exchange, exc,
        )
        return _err(502, "upstream error")

    await _cache_set(exchange, symbol, result)
    return web.json_response(_payload(result, exchange, cached_flag=False))


async def handle_prices(request: web.Request) -> web.Response:
    """``GET /api/prices?symbols=SYM1,SYM2,...`` — batch live prices.

    Query params:
      * ``symbols``  — required, comma-separated list. Max 20 symbols.
      * ``exchange`` — optional, default ``bybit``.

    Response (200):
        ``{"ok": true, "prices": {sym: {"price": float|null, "cached": bool}}}``

    Errors:
        * 400 — missing symbols or > 20.
        * 429 — per-session rate limit exceeded.
    """
    raw = request.query.get("symbols", "")
    symbols = [s.strip() for s in raw.split(",") if s.strip()]
    if not symbols or len(symbols) > 20:
        return _err(400, "symbols required, max 20")

    raw_exch = request.query.get("exchange") or "bybit"
    exchange = _normalise_exchange(raw_exch)
    if exchange is None:
        exchange = "bybit"

    # Rate-limit: count as one request regardless of batch size.
    try:
        retry_after = await _check_rate_limit(_rate_limit_key(request))
    except Exception as exc:
        logger.exception("prices route: rate-limit check failed: %s", exc)
        return _err(500, "internal error")
    if retry_after is not None:
        return _err(429, "rate limit exceeded", retry_after=retry_after)

    results: Dict[str, Dict[str, object]] = {}
    for sym in symbols[:20]:
        normalised = _normalise_symbol(sym)
        if normalised is None or not _is_crypto_symbol(normalised):
            results[sym] = {"price": None, "error": "unsupported symbol"}
            continue
        try:
            cached = await _cache_get(exchange, normalised)
            if cached is not None:
                results[sym] = {"price": float(cached.price), "cached": True}
            else:
                result = await fetch_live_price(
                    normalised, exchange, timeout_s=3.0,
                )
                if result is not None:
                    await _cache_set(exchange, normalised, result)
                    results[sym] = {"price": float(result.price), "cached": False}
                else:
                    results[sym] = {"price": None, "error": "unavailable"}
        except asyncio.TimeoutError:
            results[sym] = {"price": None, "error": "timeout"}
        except Exception:
            results[sym] = {"price": None, "error": "unavailable"}

    return web.json_response({"ok": True, "prices": results})


def _payload(
    result: LivePriceResult, exchange: str, *, cached_flag: bool,
) -> Dict[str, object]:
    """Shape the public JSON response body.

    Args:
        result: The live-price result.
        exchange: Exchange id requested by the client.
        cached_flag: Whether the result was served from the in-memory cache.

    Returns:
        Plain-dict JSON-serialisable payload.
    """
    return {
        "symbol": result.symbol,
        "price": float(result.price),
        "exchange": exchange,
        "ts": int(result.ts_ms or 0),
        "cached": bool(cached_flag),
    }


# ---------------------------------------------------------------------------
# Mounting
# ---------------------------------------------------------------------------
def attach_routes(app: web.Application, *, prefix: str = "") -> None:
    """Attach ``GET /api/price`` and ``GET /api/prices`` to ``app``.

    Args:
        app: aiohttp application.
        prefix: Optional URL prefix (used by tests that mount under a sub-path).
    """
    app.router.add_get(f"{prefix}/api/price", handle_price)
    app.router.add_get(f"{prefix}/api/prices", handle_prices)
