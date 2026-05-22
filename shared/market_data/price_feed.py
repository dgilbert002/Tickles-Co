"""
Module: price_feed
Purpose: Unified Price Feed Daemon — standalone service that reads
         unified_instruments, opens one CCXT Pro WebSocket per exchange,
         multiplexes all symbol subscriptions, broadcasts tickers via
         internal pub/sub, and exposes a local WebSocket server on port 18790.
Location: /opt/tickles/shared/market_data/price_feed.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from aiohttp import web

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_PORT: int = 18790
DEFAULT_HOST: str = "127.0.0.1"
CFD_POLL_INTERVAL_S: float = 2.0
RECONNECT_BACKOFF_BASE: float = 2.0
RECONNECT_BACKOFF_MAX: float = 60.0
MAX_QUEUE_SIZE: int = 8

# Exchanges that use CCXT Pro WebSocket (crypto).
CRYPTO_EXCHANGES: frozenset = frozenset({
    "bybit", "binance", "okx", "kucoin", "kraken",
    "coinbase", "bitget", "mexc", "gate", "gateio",
    "huobi", "htx", "blofin",
})

# Exchanges that require REST polling (CFDs, no public WS).
POLLING_EXCHANGES: frozenset = frozenset({"capital.com", "capital"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extract_bad_symbol(error_message: str, symbols: List[str]) -> Optional[str]:
    """Try to extract and remove a bad symbol from an exchange error message.

    CCXT Pro errors often look like:
        "blofin does not have market symbol 0GUSDT/USDT"

    Args:
        error_message: The error string from CCXT.
        symbols: The current list of symbols (mutated in-place).

    Returns:
        The removed symbol, or None if no symbol could be extracted.
    """
    import re
    # Look for a symbol pattern in the error message.
    match = re.search(r"symbol\s+(\S+)", error_message)
    if match:
        bad = match.group(1)
        if bad in symbols:
            symbols.remove(bad)
            return bad
    return None


# ---------------------------------------------------------------------------
# PriceFeedDaemon
# ---------------------------------------------------------------------------
class PriceFeedDaemon:
    """Unified price feed daemon.

    Reads ``public.unified_instruments``, groups by exchange, opens one
    CCXT Pro WebSocket per crypto exchange (multiplexed watch_tickers),
    polls Capital.com REST for CFDs, and exposes a local aiohttp
    WebSocket server for consumers.

    Protocol (internal WS on port 18790):
        Client -> Server:  {"subscribe": ["BTC/USDT", "ETH/USDT"]}
        Client -> Server:  {"unsubscribe": ["BTC/USDT"]}
        Server -> Client:  {"symbol": "BTC/USDT", "price": 82400.0,
                             "exchange": "bybit", "ts": 1715000000000}
    """

    def __init__(self, port: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> None:
        self._port = port
        self._host = host
        self._running = False

        # symbol (canonical, e.g. "BTC/USDT") -> asyncio.Queue of latest tickers
        self._queues: Dict[str, asyncio.Queue[Dict[str, Any]]] = {}

        # Latest ticker cache for get_latest()
        self._latest: Dict[str, Dict[str, Any]] = {}

        # Active subscriptions per connected WS client
        self._client_subs: Dict[web.WebSocketResponse, Set[str]] = {}

        # Exchange connection tasks
        self._exchange_tasks: List[asyncio.Task] = []

        # Internal WS server
        self._ws_app: Optional[web.Application] = None
        self._ws_runner: Optional[web.AppRunner] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """Start the daemon: load instruments, connect to exchanges, start WS server."""
        if self._running:
            return
        self._running = True

        # Load instruments from DB
        exchange_symbols = await self._load_instruments()
        if not exchange_symbols:
            logger.warning("No instruments found in unified_instruments — daemon idle")
        else:
            for exchange, symbols in exchange_symbols.items():
                logger.info("Exchange %s: %d symbols", exchange, len(symbols))

        # Start exchange connections
        for exchange, symbols in exchange_symbols.items():
            if exchange.lower() in POLLING_EXCHANGES:
                task = asyncio.create_task(self._poll_exchange(exchange, symbols))
            else:
                task = asyncio.create_task(self._watch_exchange(exchange, symbols))
            self._exchange_tasks.append(task)

        # Start internal WS server
        await self._start_ws_server()
        logger.info("PriceFeedDaemon started on ws://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        """Stop the daemon gracefully."""
        self._running = False
        for task in self._exchange_tasks:
            task.cancel()
        self._exchange_tasks.clear()
        if self._ws_runner:
            await self._ws_runner.cleanup()
            self._ws_runner = None
        logger.info("PriceFeedDaemon stopped")

    async def subscribe(self, symbols: List[str]) -> AsyncIterator[Dict[str, Any]]:
        """Subscribe to ticker updates for the given symbols.

        Yields ticker dicts as they arrive. Intended for programmatic
        consumers (position_monitor, interpretation_service).

        Args:
            symbols: List of canonical symbols (e.g. ``["BTC/USDT"]``).

        Yields:
            Dict with keys ``symbol``, ``price``, ``exchange``, ``ts``.
        """
        queues: Dict[str, asyncio.Queue] = {}
        for sym in symbols:
            q = self._get_or_create_queue(sym)
            queues[sym] = q

        try:
            while self._running:
                for sym, q in queues.items():
                    try:
                        ticker = q.get_nowait()
                        yield ticker
                    except asyncio.QueueEmpty:
                        pass
                await asyncio.sleep(0.1)
        finally:
            for sym in symbols:
                await self._release_queue(sym)

    def get_latest(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return the latest cached ticker for *symbol*, or None.

        Non-blocking. Suitable for synchronous callers that want a
        best-effort current price without awaiting.

        Args:
            symbol: Canonical symbol (e.g. ``"BTC/USDT"``).

        Returns:
            Dict with ``symbol``, ``price``, ``exchange``, ``ts``, or None.
        """
        return self._latest.get(symbol)

    # ------------------------------------------------------------------
    # Instrument loading
    # ------------------------------------------------------------------
    async def _load_instruments(self) -> Dict[str, List[str]]:
        """Read unified_instruments table, group by exchange.

        Returns:
            Dict mapping exchange name -> list of canonical_symbol strings.
        """
        try:
            from shared.utils.db import get_shared_pool
            pool = await get_shared_pool()
            rows = await pool.fetch_all(
                "SELECT exchange, canonical_symbol FROM public.unified_instruments "
                "WHERE is_active = TRUE ORDER BY exchange, canonical_symbol"
            )
        except Exception as exc:
            logger.error("Failed to load unified_instruments: %s", exc)
            return {}

        result: Dict[str, List[str]] = {}
        for row in rows:
            exchange = row["exchange"]
            symbol = row["canonical_symbol"]
            if exchange not in result:
                result[exchange] = []
            if symbol not in result[exchange]:
                result[exchange].append(symbol)
        return result

    # ------------------------------------------------------------------
    # Exchange connections
    # ------------------------------------------------------------------
    async def _watch_exchange(self, exchange: str, symbols: List[str]) -> None:
        """Connect to *exchange* via CCXT Pro watch_tickers with reconnection.

        Filters out symbols that the exchange does not recognise, so a single
        bad symbol (e.g. from a stale unified_instruments row) cannot cause
        a permanent reconnect loop.
        """
        backoff = RECONNECT_BACKOFF_BASE
        working_symbols: List[str] = list(symbols)
        while self._running and working_symbols:
            try:
                import ccxt.pro as ccxtpro
                exchange_class = getattr(ccxtpro, exchange, None)
                if exchange_class is None:
                    logger.error("CCXT Pro has no class for exchange '%s'", exchange)
                    return

                instance = exchange_class({
                    "enableRateLimit": True,
                    "options": {"defaultType": "swap"},
                })
                logger.info("Connected to %s (%d symbols via watch_tickers)", exchange, len(working_symbols))

                while self._running:
                    try:
                        tickers = await instance.watch_tickers(working_symbols)
                        await self._process_tickers(exchange, tickers)
                        backoff = RECONNECT_BACKOFF_BASE  # reset on success
                    except Exception as inner_exc:
                        err_msg = str(inner_exc)
                        logger.warning("%s watch_tickers error: %s", exchange, err_msg)
                        # Try to extract the offending symbol and remove it.
                        removed = _extract_bad_symbol(err_msg, working_symbols)
                        if removed:
                            logger.info(
                                "%s: removed bad symbol '%s', retrying with %d symbols",
                                exchange, removed, len(working_symbols),
                            )
                            break  # reconnect with reduced list
                        break  # unknown error — reconnect

                await instance.close()
            except Exception as exc:
                logger.error("%s connection failed: %s", exchange, exc)

            if not self._running or not working_symbols:
                break
            logger.info("%s reconnecting in %.1fs", exchange, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)

    async def _poll_exchange(self, exchange: str, symbols: List[str]) -> None:
        """Poll *exchange* REST API for CFD prices (Capital.com).

        NOTE: CCXT 4.5.48 does not include a Capital.com adapter.
        When it becomes available, this method will be updated.
        """
        logger.warning(
            "Capital.com polling not available (CCXT %s has no capitalcom adapter). "
            "CFD prices will not stream until a REST adapter is implemented.",
            getattr(ccxt, '__version__', 'unknown') if 'ccxt' in dir() else 'unknown',
        )
        return  # graceful no-op — Capital.com adapter not available in CCXT 4.5.48

    async def _process_tickers(self, exchange: str, tickers: Dict[str, Any]) -> None:
        """Process a batch of tickers from an exchange, fan-out to queues."""
        now_ms = int(time.time() * 1000)
        for symbol, ticker in tickers.items():
            if ticker is None:
                continue
            price = (
                ticker.get("last")
                or ticker.get("close")
                or ticker.get("bid")
            )
            if price is None:
                continue
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue

            ts = ticker.get("timestamp") or now_ms
            try:
                ts = int(ts)
            except (TypeError, ValueError):
                ts = now_ms

            msg: Dict[str, Any] = {
                "symbol": symbol,
                "price": price,
                "exchange": exchange,
                "ts": ts,
            }

            # Update latest cache
            self._latest[symbol] = msg

            # Fan-out to subscriber queues
            q = self._queues.get(symbol)
            if q is not None:
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    pass

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------
    def _get_or_create_queue(self, symbol: str) -> asyncio.Queue:
        """Return the queue for *symbol*, creating it if needed."""
        if symbol not in self._queues:
            self._queues[symbol] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
        return self._queues[symbol]

    async def _release_queue(self, symbol: str) -> None:
        """Release a reference to *symbol*'s queue. Kept for future ref-counting."""
        pass

    # ------------------------------------------------------------------
    # Internal WebSocket server
    # ------------------------------------------------------------------
    async def _start_ws_server(self) -> None:
        """Start the internal aiohttp WebSocket server for consumer connections."""
        self._ws_app = web.Application()
        self._ws_app.router.add_get("/", self._handle_internal_ws)
        self._ws_runner = web.AppRunner(self._ws_app)
        await self._ws_runner.setup()
        site = web.TCPSite(self._ws_runner, self._host, self._port)
        await site.start()

    async def _handle_internal_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Handle an internal WebSocket connection from a consumer.

        Protocol:
            Receive: {"subscribe": ["BTC/USDT", ...]} or {"unsubscribe": ["BTC/USDT", ...]}
            Send: {"symbol": "BTC/USDT", "price": 82400.0, "exchange": "bybit", "ts": 1715000000000}
        """
        ws = web.WebSocketResponse(heartbeat=15.0)
        await ws.prepare(request)
        self._client_subs[ws] = set()
        logger.info("Internal WS client connected (total: %d)", len(self._client_subs))

        listen_task: Optional[asyncio.Task] = None

        async def listen_queues() -> None:
            """Read from subscribed queues and send to client."""
            while not ws.closed and self._running:
                subs = self._client_subs.get(ws, set())
                for sym in list(subs):
                    q = self._queues.get(sym)
                    if q is None:
                        continue
                    try:
                        msg = q.get_nowait()
                        await ws.send_json(msg)
                    except asyncio.QueueEmpty:
                        pass
                await asyncio.sleep(0.05)

        try:
            listen_task = asyncio.create_task(listen_queues())

            async for raw in ws:
                if raw.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(raw.data)
                    except json.JSONDecodeError:
                        continue

                    if "subscribe" in data:
                        symbols = data["subscribe"]
                        if isinstance(symbols, list):
                            for sym in symbols:
                                if isinstance(sym, str):
                                    self._get_or_create_queue(sym)
                                    self._client_subs[ws].add(sym)
                            logger.debug("Client subscribed to %s", symbols)

                    elif "unsubscribe" in data:
                        symbols = data["unsubscribe"]
                        if isinstance(symbols, list):
                            for sym in symbols:
                                if isinstance(sym, str):
                                    self._client_subs[ws].discard(sym)
                            logger.debug("Client unsubscribed from %s", symbols)

                elif raw.type == web.WSMsgType.ERROR:
                    logger.warning("Internal WS error: %s", ws.exception())
                    break
        except ConnectionResetError:
            logger.debug("Internal WS client reset")
        finally:
            if listen_task:
                listen_task.cancel()
            self._client_subs.pop(ws, None)
            logger.info("Internal WS client disconnected (total: %d)", len(self._client_subs))

        return ws


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    """Entry point for running the daemon as a standalone process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    daemon = PriceFeedDaemon()
    try:
        await daemon.start()
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())
