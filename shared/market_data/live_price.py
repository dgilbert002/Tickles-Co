"""
Module: live_price
Purpose: Shared CCXT live-price helper. Performs a single ``fetch_ticker``
         against an exchange via ``ccxt.async_support``, returning the
         normalised last/close price along with metadata. Used by the
         interpretation service (degraded quant fallback when no candles
         are in the DB) and by the dashboard ``/api/price`` endpoint.
Location: /opt/tickles/shared/market_data/live_price.py

Design:
  * One function: :func:`fetch_live_price`. Always closes the CCXT
    instance via ``try/finally`` so we never leak open aiohttp sessions.
  * Hard-bound by ``timeout_s`` (default 5s) using ``asyncio.wait_for``.
  * Tries multiple symbol forms in order: the caller's input, slash form
    (``BTCUSDT`` -> ``BTC/USDT``), and a perp-suffix form (``BTC/USDT:USDT``).
  * Module-level allow-list of supported exchanges; unknown exchanges
    raise :class:`UnsupportedExchangeError`.
  * No I/O on import. The CCXT instance is created lazily inside the
    helper.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# Supported exchange ids. Match the lowercased CCXT class names.
SUPPORTED_EXCHANGES: frozenset = frozenset({
    "binance",
    "bybit",
    "okx",
    "kucoin",
    "kraken",
    "coinbase",
    "bitget",
    "mexc",
    "gate",
    "gateio",
    "huobi",
    "htx",
})

# Default per-call timeout for the entire helper (all probes combined).
DEFAULT_TIMEOUT_S: float = 5.0


# ---------------------------------------------------------------------------
# Dataclasses + exceptions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LivePriceResult:
    """Result of a successful live-price probe."""

    symbol: str        # The symbol form that actually returned a price.
    price: float       # Last / close.
    exchange: str      # Echoed back for callers.
    ts_ms: int         # Exchange-reported timestamp (ms epoch); 0 if absent.


class UnsupportedExchangeError(ValueError):
    """Raised when ``exchange`` is not in :data:`SUPPORTED_EXCHANGES`."""


class LivePriceError(RuntimeError):
    """Raised when the probe fails for non-input reasons (network, exchange)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _candidate_symbols(symbol: str) -> List[str]:
    """Return a list of symbol forms to try, in order.

    Args:
        symbol: Caller's input symbol (e.g. ``BTC/USDT``, ``BTCUSDT``,
            ``TAOUSDT.P``).

    Returns:
        Ordered list of forms, deduped, preserving the caller's choice
        as the first element.
    """
    if not symbol:
        return []
    s = symbol.strip().upper()
    out: List[str] = [s]
    # Strip any ``.P`` perp marker to derive a base form.
    base = s[:-2] if s.endswith(".P") else s
    if base != s and base not in out:
        out.append(base)
    # Build slash form when input has none (e.g. ``BTCUSDT`` -> ``BTC/USDT``).
    if "/" not in base and len(base) > 3:
        for quote in ("USDT", "USDC", "BUSD", "USD", "EUR", "BTC", "ETH"):
            if base.endswith(quote) and len(base) > len(quote):
                slash = base[: -len(quote)] + "/" + quote
                if slash not in out:
                    out.append(slash)
                # Perp form for futures venues.
                perp = f"{slash}:{quote}"
                if perp not in out:
                    out.append(perp)
                break
    elif "/" in base:
        # Add perp form when slash present and no colon.
        if ":" not in base:
            quote = base.split("/", 1)[1]
            perp = f"{base}:{quote}"
            if perp not in out:
                out.append(perp)
    return out


def _extract_price(ticker: object) -> Optional[float]:
    """Pull a usable price from a CCXT ticker dict.

    Args:
        ticker: CCXT ``fetch_ticker`` return value.

    Returns:
        ``float(last)`` if present, else ``float(close)``, else
        ``(bid+ask)/2`` if both present, else ``None``.
    """
    if not isinstance(ticker, dict):
        return None
    for key in ("last", "close"):
        v = ticker.get(key)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    bid = ticker.get("bid")
    ask = ticker.get("ask")
    try:
        if bid is not None and ask is not None:
            mid = (float(bid) + float(ask)) / 2.0
            if mid > 0:
                return mid
    except (TypeError, ValueError):
        return None
    return None


def _ticker_ts_ms(ticker: object) -> int:
    """Extract a millisecond timestamp from a CCXT ticker, or ``0``."""
    if not isinstance(ticker, dict):
        return 0
    raw = ticker.get("timestamp")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def fetch_live_price(
    symbol: str,
    exchange: str = "binance",
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> LivePriceResult:
    """Probe ``exchange`` for the live price of ``symbol``.

    Tries multiple symbol forms (slash, no-slash, perp) until one of
    them resolves. Always closes the CCXT instance.

    Args:
        symbol: Trading pair (any common form).
        exchange: Lower-cased CCXT id (e.g. ``binance``).
        timeout_s: Hard deadline for the entire call, including all
            symbol-form retries.

    Returns:
        :class:`LivePriceResult` on success.

    Raises:
        ValueError: If ``symbol`` is empty.
        UnsupportedExchangeError: If ``exchange`` is not in
            :data:`SUPPORTED_EXCHANGES`.
        LivePriceError: On timeout or when no symbol form returned a
            usable price.
    """
    if not symbol or not symbol.strip():
        raise ValueError("symbol must be non-empty")
    ex_id = (exchange or "").strip().lower()
    if ex_id not in SUPPORTED_EXCHANGES:
        raise UnsupportedExchangeError(
            f"exchange not supported: {exchange!r}"
        )
    # Some venues alias under CCXT (htx vs huobi, gate vs gateio).
    ccxt_id = "huobi" if ex_id == "htx" else ("gateio" if ex_id == "gate" else ex_id)

    try:
        return await asyncio.wait_for(
            _probe(symbol, ex_id, ccxt_id), timeout=timeout_s,
        )
    except asyncio.TimeoutError as exc:
        raise LivePriceError(
            f"live-price probe timed out after {timeout_s}s for {symbol}@{ex_id}"
        ) from exc


async def _probe(symbol: str, exchange: str, ccxt_id: str) -> LivePriceResult:
    """Inner probe — kept under 50 lines and timeout-bounded by caller."""
    try:
        import ccxt.async_support as ccxt_async  # type: ignore
    except ImportError as exc:
        raise LivePriceError("ccxt async_support not available") from exc
    try:
        from ccxt.base.errors import (  # type: ignore
            BadSymbol, ExchangeError, NetworkError,
        )
    except ImportError:  # pragma: no cover — defensive fallback
        BadSymbol = Exception  # type: ignore
        ExchangeError = Exception  # type: ignore
        NetworkError = Exception  # type: ignore

    cls = getattr(ccxt_async, ccxt_id, None)
    if cls is None:
        raise UnsupportedExchangeError(
            f"ccxt has no async_support class: {ccxt_id!r}"
        )
    client = cls({"enableRateLimit": True})
    try:
        last_err: Optional[BaseException] = None
        for cand in _candidate_symbols(symbol):
            try:
                ticker = await client.fetch_ticker(cand)
            except BadSymbol as exc:
                last_err = exc
                continue
            except (NetworkError, ExchangeError) as exc:
                last_err = exc
                continue
            except Exception as exc:  # noqa: BLE001 - probe is best-effort
                last_err = exc
                continue
            price = _extract_price(ticker)
            if price is None:
                continue
            return LivePriceResult(
                symbol=cand,
                price=price,
                exchange=exchange,
                ts_ms=_ticker_ts_ms(ticker),
            )
        raise LivePriceError(
            f"no usable ticker for {symbol!r} on {exchange}: {last_err!r}"
        )
    finally:
        try:
            await client.close()
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            logger.debug("ccxt close failed for %s: %s", ccxt_id, exc)


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "LivePriceError",
    "LivePriceResult",
    "SUPPORTED_EXCHANGES",
    "UnsupportedExchangeError",
    "fetch_live_price",
]
