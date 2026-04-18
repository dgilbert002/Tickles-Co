"""
JarvAIs Price Streamer — Universal real-time price feed.

Subscribes to live market data for all tracked symbols:
  - Crypto: CCXT Pro WebSocket (Bybit primary, Blofin fallback)
  - Stocks/Indices: Yahoo Finance REST polling (5s staggered)
  - Forex/Metals: MT5 tick polling (2s) when connected

Provides:
  - Thread-safe in-memory price cache with sub-second updates
  - Level-crossing callbacks for instant entry/SL/TP detection
  - Periodic DB write-back to live_prices table + trade_dossiers.current_price
  - Price-change notifications for WebSocket broadcast to dashboard

Usage:
    from services.price_streamer import start_price_streamer, get_price_streamer
    streamer = start_price_streamer(db, config)

    streamer.subscribe(["BTCUSDT", "XAUUSD"])
    price = streamer.get_price("BTCUSDT")
"""

import asyncio
import logging
import threading
import time
import json as _json
import urllib.request
from collections import defaultdict
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set, Any

logger = logging.getLogger("jarvais.price_streamer")

from services.candle_collector import (
    _resolve_ccxt_symbol, _is_crypto_symbol, _looks_like_crypto, YAHOO_SYMBOL_MAP,
)
from db.market_symbols import resolve_symbol, SYMBOL_ALIASES


# ═══════════════════════════════════════════════════════════════════════
# PRICE CACHE — thread-safe storage for latest prices
# ═══════════════════════════════════════════════════════════════════════

class PriceCache:
    """Thread-safe in-memory price cache with change tracking."""
    STALE_THRESHOLD = 120  # seconds; entries older than this are considered stale

    def __init__(self):
        self._lock = threading.Lock()
        self._prices: Dict[str, Dict] = {}
        self._generation: int = 0

    def update(self, symbol: str, price: float, bid: float = None,
               ask: float = None, source: str = "", ts: float = None) -> bool:
        """Update price, returns True if price actually changed."""
        if not price or price <= 0:
            return False
        now = ts or time.time()
        with self._lock:
            prev = self._prices.get(symbol)
            prev_price = prev["price"] if prev else None
            self._prices[symbol] = {
                "symbol": symbol,
                "price": float(price),
                "bid": float(bid) if bid else float(price),
                "ask": float(ask) if ask else float(price),
                "source": source,
                "ts": now,
                "prev_price": prev_price,
            }
            self._generation += 1
            return prev_price != price

    def get(self, symbol: str) -> Optional[Dict]:
        with self._lock:
            entry = self._prices.get(symbol)
            if entry is None:
                return None
            if time.time() - entry["ts"] > self.STALE_THRESHOLD:
                return {**entry, "stale": True}
            return entry

    def get_all(self) -> Dict[str, Dict]:
        with self._lock:
            return dict(self._prices)

    def get_changed_since(self, last_gen: int) -> tuple:
        """Returns (current_generation, dict of all prices if generation changed)."""
        with self._lock:
            if self._generation == last_gen:
                return last_gen, {}
            return self._generation, dict(self._prices)

    def clear_symbols(self, symbols: List[str]) -> None:
        """Remove cache entries for the given symbols (e.g. on WebSocket reconnect)."""
        with self._lock:
            for sym in symbols:
                self._prices.pop(sym, None)
            if symbols:
                self._generation += 1

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation


# ═══════════════════════════════════════════════════════════════════════
# LEVEL WATCHER — fires callbacks when price crosses entry/SL/TP
# ═══════════════════════════════════════════════════════════════════════

class LevelWatcher:
    """Tracks price levels and fires when a level is breached."""

    def __init__(self):
        self._lock = threading.Lock()
        self._watches: Dict[str, List[Dict]] = defaultdict(list)
        self._triggered: Set[str] = set()

    def register(self, symbol: str, level: float, direction: str,
                 callback_id: str, meta: Dict = None):
        """
        Register a level watch.
        direction: 'above' (fires when price >= level) or 'below' (fires when price <= level)
        """
        with self._lock:
            self._watches[symbol].append({
                "level": level, "direction": direction,
                "callback_id": callback_id, "meta": meta or {},
            })

    def unregister(self, callback_id: str):
        with self._lock:
            for sym in list(self._watches):
                self._watches[sym] = [
                    w for w in self._watches[sym] if w["callback_id"] != callback_id
                ]
                if not self._watches[sym]:
                    del self._watches[sym]
            self._triggered.discard(callback_id)

    def unregister_symbol(self, symbol: str):
        with self._lock:
            removed = self._watches.pop(symbol, [])
            for w in removed:
                self._triggered.discard(w["callback_id"])

    def check(self, symbol: str, price: float) -> List[Dict]:
        """Returns list of newly triggered watches for this price tick."""
        triggered = []
        with self._lock:
            for w in self._watches.get(symbol, []):
                cid = w["callback_id"]
                if cid in self._triggered:
                    continue
                hit = False
                if w["direction"] == "above" and price >= w["level"]:
                    hit = True
                elif w["direction"] == "below" and price <= w["level"]:
                    hit = True
                if hit:
                    triggered.append(dict(w, symbol=symbol, hit_price=price))
                    self._triggered.add(cid)
        return triggered

    def get_watches_for_symbol(self, symbol: str) -> List[Dict]:
        with self._lock:
            return list(self._watches.get(symbol, []))

    def count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._watches.values())


# ═══════════════════════════════════════════════════════════════════════
# PRICE STREAMER — main service
# ═══════════════════════════════════════════════════════════════════════

class PriceStreamer:
    """
    Universal real-time price feed.
    Crypto → CCXT Pro WebSocket | Non-crypto → Yahoo REST | Forex → MT5 tick
    """

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self.cache = PriceCache()
        self.levels = LevelWatcher()

        self._subscribed: Set[str] = set()
        self._crypto_syms: Set[str] = set()
        self._yahoo_syms: Set[str] = set()

        self._alias_to_canonical: Dict[str, str] = {}
        self._canonical_to_aliases: Dict[str, Set[str]] = defaultdict(set)
        self._unresolved: Set[str] = set()
        self._failed_symbols: Dict[str, float] = {}
        self._FAILED_COOLDOWN = 3600
        self._symbol_corrections: Dict[str, str] = {}

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sub_lock = threading.Lock()

        self._on_update_cbs: List[Callable] = []
        self._on_level_cbs: List[Callable] = []

        ccxt_cfg = config.raw.get("ccxt", {}) if config else {}
        self._preferred_ex = ccxt_cfg.get("preferred_exchange", "bybit")
        self._fallback_ex = ccxt_cfg.get("fallback_exchange", "blofin")

        ps_cfg = config.raw.get("price_streamer", {}) if config else {}
        self._yahoo_interval = ps_cfg.get("yahoo_poll_seconds", 5)
        self._mt5_interval = ps_cfg.get("mt5_poll_seconds", 2)
        self._db_wb_interval = ps_cfg.get("db_writeback_seconds", 10)
        self._auto_sub_interval = ps_cfg.get("auto_subscribe_seconds", 30)

        logger.info(f"[PriceStreamer] init | preferred={self._preferred_ex} "
                    f"fallback={self._fallback_ex}")

    # ── Public API ─────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="price-streamer")
        self._thread.start()
        logger.info("[PriceStreamer] Started background thread")

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        logger.info("[PriceStreamer] Stop requested")

    def subscribe(self, symbols: List[str]):
        """Add symbols to subscription. Resolves aliases automatically."""
        with self._sub_lock:
            new = set(s.upper() for s in symbols) - self._subscribed
            if not new:
                return
            for original in new:
                self._subscribed.add(original)
                canonical = resolve_symbol(original, self.db)
                if canonical != original:
                    self._alias_to_canonical[original] = canonical
                    self._canonical_to_aliases[canonical].add(original)
                    logger.info(f"[PriceStreamer] Alias: {original} -> {canonical}")
                    if canonical not in self._subscribed:
                        self._subscribed.add(canonical)
                    sym = canonical
                else:
                    sym = original
                if _is_crypto_symbol(self.db, sym) or _looks_like_crypto(sym):
                    self._crypto_syms.add(sym)
                else:
                    self._yahoo_syms.add(sym)
            logger.info(f"[PriceStreamer] +{len(new)} symbols "
                        f"(crypto={len(self._crypto_syms)} yahoo={len(self._yahoo_syms)} "
                        f"total={len(self._subscribed)})")

    def unsubscribe(self, symbols: List[str]):
        with self._sub_lock:
            for sym in symbols:
                s = sym.upper()
                self._subscribed.discard(s)
                self._crypto_syms.discard(s)
                self._yahoo_syms.discard(s)

    def get_price(self, symbol: str) -> Optional[Dict]:
        """Get live price for symbol. Resolves aliases (GOLD→XAUUSD, TAO→TAOUSDT).
        If no price cached, subscribes on-demand to trigger fetching.
        Returns data with symbol=requested so frontend matches dossier rows."""
        s = symbol.upper()
        result = self.cache.get(s)
        if result:
            return result
        canonical = self._alias_to_canonical.get(s) or resolve_symbol(s, self.db)
        if canonical != s:
            result = self.cache.get(canonical)
            if result:
                return {**result, "symbol": s}
        if not result and s not in self._subscribed:
            self.subscribe([s])
        return result

    def get_all_prices(self) -> Dict[str, Dict]:
        return self.cache.get_all()

    def on_update(self, cb: Callable):
        """Register callback(symbol, data_dict) for every price update."""
        self._on_update_cbs.append(cb)

    def on_level_trigger(self, cb: Callable):
        """Register callback(watch_dict, price) when a watched level is breached."""
        self._on_level_cbs.append(cb)

    def get_status(self) -> Dict:
        return {
            "running": self._running,
            "subscribed": len(self._subscribed),
            "crypto": len(self._crypto_syms),
            "yahoo": len(self._yahoo_syms),
            "cached_prices": len(self.cache.get_all()),
            "level_watches": self.levels.count(),
        }

    # ── Event loop ─────────────────────────────────────────────────────

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        _default_handler = self._loop.get_exception_handler()

        def _suppress_dup_sub(loop, context):
            """Handle orphaned Futures from CCXT Pro WebSocket.
            CCXT Pro creates internal Futures per subscription. When the
            WebSocket drops (ping-pong timeout, already subscribed, etc.),
            all those Futures fail simultaneously and spam
            'Future exception was never retrieved'. Suppress known
            recoverable cases; delegate everything else."""
            exc = context.get("exception")
            if exc:
                msg = str(exc).lower()
                if ("already subscribed" in msg
                        or "ping-pong keepalive" in msg
                        or "requesttimeout" in type(exc).__name__.lower()
                        or "timed out" in msg):
                    logger.debug("[PriceStreamer] suppressed orphan Future: "
                                 f"{type(exc).__name__}")
                    return
            if _default_handler:
                _default_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        self._loop.set_exception_handler(_suppress_dup_sub)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            logger.error(f"[PriceStreamer] Event loop crashed: {e}", exc_info=True)
        finally:
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except Exception:
                pass
            self._loop.close()
            logger.info("[PriceStreamer] Event loop closed")

    async def _main(self):
        tasks = [
            asyncio.create_task(self._crypto_stream()),
            asyncio.create_task(self._yahoo_poll()),
            asyncio.create_task(self._mt5_poll()),
            asyncio.create_task(self._db_writeback()),
            asyncio.create_task(self._auto_subscribe()),
            asyncio.create_task(self._resolve_unknown_symbols()),
        ]
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"[PriceStreamer] _main error: {e}")

    # ── Auto-subscribe: discover tracked symbols ───────────────────────

    async def _auto_subscribe(self):
        while self._running:
            try:
                syms = self._get_tracked_symbols()
                if syms:
                    self.subscribe(syms)
            except Exception as e:
                logger.debug(f"[PriceStreamer] auto-subscribe error: {e}")
            await asyncio.sleep(self._auto_sub_interval)

    def _get_tracked_symbols(self) -> List[str]:
        symbols = set()
        try:
            rows = self.db.fetch_all(
                "SELECT DISTINCT symbol FROM trade_dossiers "
                "WHERE status IN ('proposed','monitoring','open_order','live')")
            for r in (rows or []):
                if r.get("symbol"):
                    symbols.add(r["symbol"])
        except Exception:
            pass
        try:
            if self.config:
                wl = (self.config.raw.get("trade_decision", {})
                      .get("scout", {}).get("watchlist", []))
                symbols.update(wl)
        except Exception:
            pass
        try:
            rows = self.db.fetch_all(
                "SELECT DISTINCT symbol FROM parsed_signals "
                "WHERE status IN ('pending','active','entry_hit') "
                "AND created_at > DATE_SUB(NOW(), INTERVAL 7 DAY)")
            for r in (rows or []):
                if r.get("symbol"):
                    symbols.add(r["symbol"])
        except Exception:
            pass
        return list(symbols)

    # ── Background LLM resolver for symbols without price data ─────────

    async def _resolve_unknown_symbols(self):
        """Periodically check for subscribed symbols with no price data.
        Tries static/DB resolution first; if still no price, uses LLM to resolve."""
        await asyncio.sleep(30)
        while self._running:
            try:
                no_price = []
                with self._sub_lock:
                    for sym in self._subscribed:
                        if sym in self._alias_to_canonical:
                            continue
                        if not self.cache.get(sym):
                            no_price.append(sym)

                for sym in no_price[:5]:
                    if sym in self._unresolved:
                        continue
                    try:
                        from db.market_symbols import resolve_symbol_with_llm
                        loop = asyncio.get_event_loop()
                        canonical = await loop.run_in_executor(
                            None, resolve_symbol_with_llm, sym, self.db)
                        if canonical and canonical != sym:
                            with self._sub_lock:
                                self._alias_to_canonical[sym] = canonical
                                self._canonical_to_aliases[canonical].add(sym)
                                if canonical not in self._subscribed:
                                    self._subscribed.add(canonical)
                                if (_is_crypto_symbol(self.db, canonical)
                                        or _looks_like_crypto(canonical)):
                                    self._crypto_syms.add(canonical)
                                else:
                                    self._yahoo_syms.add(canonical)
                            logger.info(f"[PriceStreamer] LLM resolved "
                                        f"{sym} -> {canonical}")
                        else:
                            self._unresolved.add(sym)
                            logger.debug(f"[PriceStreamer] Could not resolve: {sym}")
                    except Exception as e:
                        logger.debug(f"[PriceStreamer] LLM resolve error "
                                     f"for {sym}: {e}")
                        self._unresolved.add(sym)
            except Exception as e:
                logger.debug(f"[PriceStreamer] resolve loop error: {e}")
            await asyncio.sleep(45)

    def _update_db_ticker(self, internal_sym: str, ccxt_sym: str,
                          exchange_id: str):
        """Persist a corrected exchange ticker to market_symbols so it
        survives restarts and benefits the whole app."""
        if not self.db or not internal_sym:
            return
        _ticker_col_map = {"bybit": "bybit_ticker", "blofin": "blofin_ticker", "bitget": "bitget_ticker"}
        col = _ticker_col_map.get(exchange_id, f"{exchange_id}_ticker")
        try:
            row = self.db.fetch_one(
                "SELECT id FROM market_symbols WHERE symbol = %s",
                (internal_sym.upper(),))
            if row:
                self.db.execute(
                    f"UPDATE market_symbols SET {col} = %s WHERE symbol = %s",
                    (ccxt_sym, internal_sym.upper()))
                logger.info(f"[PriceStreamer] Updated DB {col}={ccxt_sym} "
                            f"for {internal_sym}")
            else:
                logger.debug(f"[PriceStreamer] {internal_sym} not in "
                             f"market_symbols — correction cached in memory")
        except Exception as e:
            logger.debug(f"[PriceStreamer] DB ticker update failed: {e}")

    # ── Crypto: CCXT Pro WebSocket ─────────────────────────────────────

    @staticmethod
    def _patch_exchange_session(exchange):
        """Replace the exchange's aiohttp session with one using ThreadedResolver.
        aiohttp's default AsyncResolver (aiodns) can fail when the system's
        DNS configuration uses IPv6 or non-standard resolvers.  ThreadedResolver
        falls back to socket.getaddrinfo which is more reliable on Windows."""
        try:
            import aiohttp
            connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
            exchange.session = aiohttp.ClientSession(connector=connector)
            logger.debug("[PriceStreamer] Patched exchange session with "
                         "ThreadedResolver")
        except Exception as e:
            logger.debug(f"[PriceStreamer] Could not patch session: {e}")

    async def _crypto_stream(self):
        """Stream crypto prices via CCXT Pro WebSocket with auto-reconnect.
        Uses aiohttp.ThreadedResolver to bypass aiodns issues on Windows."""
        ccxtpro = None
        try:
            import ccxt.pro as _ccxtpro
            ccxtpro = _ccxtpro
        except ImportError:
            logger.warning("[PriceStreamer] ccxt.pro not available — "
                           "falling back to REST polling for crypto")
            await self._crypto_rest_fallback()
            return

        exchange = None
        active_ex = self._preferred_ex
        _fallback_since = 0.0
        _consecutive_failures = 0

        _EX_OPTIONS = {
            "bybit": {"defaultType": "swap", "fetchMarkets": ["linear"]},
            "blofin": {"defaultType": "swap"},
            "bitget": {"defaultType": "swap", "defaultSubType": "linear"},
        }

        while self._running:
            try:
                with self._sub_lock:
                    symbols = list(self._crypto_syms)
                if not symbols:
                    await asyncio.sleep(2)
                    continue

                now = time.time()
                ccxt_symbols = []
                sym_map: Dict[str, str] = {}
                for sym in symbols:
                    if sym in self._failed_symbols:
                        if now - self._failed_symbols[sym] < self._FAILED_COOLDOWN:
                            continue
                        del self._failed_symbols[sym]
                    corr_key = f"{sym}:{active_ex}"
                    if corr_key in self._symbol_corrections:
                        cs = self._symbol_corrections[corr_key]
                    else:
                        cs = _resolve_ccxt_symbol(self.db, sym, active_ex)
                    if cs:
                        if cs in self._failed_symbols:
                            if now - self._failed_symbols[cs] < self._FAILED_COOLDOWN:
                                continue
                            del self._failed_symbols[cs]
                        ccxt_symbols.append(cs)
                        sym_map[cs] = sym
                if not ccxt_symbols:
                    await asyncio.sleep(5)
                    continue

                if (active_ex != self._preferred_ex and _fallback_since
                        and time.time() - _fallback_since > 300):
                    logger.info(f"[PriceStreamer] retrying preferred exchange: "
                                f"{self._preferred_ex}")
                    if exchange:
                        try:
                            await exchange.close()
                        except Exception:
                            pass
                        exchange = None
                        self.cache.clear_symbols(symbols)
                        await asyncio.sleep(0)
                    active_ex = self._preferred_ex
                    _fallback_since = 0.0

                if exchange is None:
                    ex_cls = getattr(ccxtpro, active_ex, None)
                    if not ex_cls:
                        logger.error(f"[PriceStreamer] {active_ex} not in "
                                     f"ccxt.pro")
                        await asyncio.sleep(30)
                        continue
                    ex_opts = {"enableRateLimit": True}
                    if active_ex in _EX_OPTIONS:
                        ex_opts["options"] = _EX_OPTIONS[active_ex]
                    exchange = ex_cls(ex_opts)
                    self._patch_exchange_session(exchange)
                    try:
                        await exchange.load_markets()
                        logger.info(f"[PriceStreamer] CCXT Pro: connected to "
                                    f"{active_ex} "
                                    f"({len(exchange.markets)} markets)")
                        _consecutive_failures = 0
                    except Exception as lm_err:
                        _consecutive_failures += 1
                        backoff = min(10 * (2 ** (_consecutive_failures - 1)),
                                      120)
                        logger.warning(
                            f"[PriceStreamer] {active_ex} loadMarkets failed "
                            f"(attempt {_consecutive_failures}, "
                            f"retry in {backoff}s): {lm_err}")
                        try:
                            await exchange.close()
                        except Exception:
                            pass
                        exchange = None
                        self.cache.clear_symbols(symbols)
                        await asyncio.sleep(0)
                        if (active_ex == self._preferred_ex
                                and self._fallback_ex):
                            active_ex = self._fallback_ex
                            _fallback_since = time.time()
                            logger.info(f"[PriceStreamer] switching to "
                                        f"fallback: {active_ex}")
                        else:
                            active_ex = self._preferred_ex
                            _fallback_since = 0.0
                        await asyncio.sleep(backoff)
                        continue

                tickers = await exchange.watch_tickers(ccxt_symbols)
                for csym, tk in tickers.items():
                    our = sym_map.get(csym)
                    if our and tk:
                        ts = ((tk.get("timestamp") or
                               (time.time() * 1000)) / 1000.0)
                        self._handle_update(
                            our,
                            price=tk.get("last") or tk.get("close", 0),
                            bid=tk.get("bid"), ask=tk.get("ask"),
                            source=active_ex, ts=ts,
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                err_str = str(e)
                import re as _re
                # Bybit "already subscribed" on reconnect — close and reconnect for clean slate
                if "already subscribed" in err_str.lower():
                    logger.debug(
                        f"[PriceStreamer] Bybit already subscribed — "
                        f"closing and reconnecting")
                    if exchange:
                        try:
                            await exchange.close()
                        except Exception:
                            pass
                        exchange = None
                        self.cache.clear_symbols(symbols)
                    await asyncio.sleep(2)
                    continue
                bad_sym_match = _re.search(
                    r'does not have market symbol (\S+)', err_str)
                if bad_sym_match:
                    bad_sym = bad_sym_match.group(1)
                    our_sym = sym_map.get(bad_sym)

                    corrected = None
                    if our_sym:
                        from services.candle_collector import (
                            _get_exchange_markets as _gem,
                            _exchange_markets_ts as _emts)
                        _emts[active_ex] = 0
                        _gem(active_ex)

                        new_sym = _resolve_ccxt_symbol(
                            self.db, our_sym, active_ex)
                        if new_sym and new_sym != bad_sym:
                            corrected = new_sym
                            logger.info(
                                f"[PriceStreamer] Re-resolved {our_sym}: "
                                f"{bad_sym} -> {corrected} on {active_ex}")
                        else:
                            alt_ex = (self._fallback_ex
                                      if active_ex == self._preferred_ex
                                      else self._preferred_ex)
                            if alt_ex:
                                _emts[alt_ex] = 0
                                _gem(alt_ex)
                                alt_sym = _resolve_ccxt_symbol(
                                    self.db, our_sym, alt_ex)
                                if alt_sym:
                                    logger.info(
                                        f"[PriceStreamer] {our_sym} not on "
                                        f"{active_ex}, found {alt_sym} on "
                                        f"{alt_ex}")

                    if corrected:
                        corr_key = f"{our_sym}:{active_ex}"
                        self._symbol_corrections[corr_key] = corrected
                        self._failed_symbols.pop(bad_sym, None)
                        if our_sym:
                            self._failed_symbols.pop(our_sym, None)
                        self._update_db_ticker(our_sym, corrected, active_ex)
                    else:
                        if bad_sym not in self._failed_symbols:
                            logger.warning(
                                f"[PriceStreamer] {bad_sym} does not exist on "
                                f"{active_ex} (no correction found for "
                                f"'{our_sym or '?'}') — quarantined for "
                                f"{self._FAILED_COOLDOWN}s")
                        self._failed_symbols[bad_sym] = time.time()
                        if our_sym:
                            self._failed_symbols[our_sym] = time.time()

                    if exchange:
                        try:
                            await exchange.close()
                        except Exception:
                            pass
                        exchange = None
                        self.cache.clear_symbols(symbols)
                        await asyncio.sleep(0)
                    await asyncio.sleep(2)
                    continue

                _consecutive_failures += 1
                backoff = min(10 * (2 ** (_consecutive_failures - 1)), 120)
                logger.warning(
                    f"[PriceStreamer] crypto stream error ({active_ex}, "
                    f"attempt {_consecutive_failures}, retry in {backoff}s): {e}")
                if exchange:
                    try:
                        await exchange.close()
                    except Exception:
                        pass
                    exchange = None
                    self.cache.clear_symbols(symbols)
                    await asyncio.sleep(0)

                if active_ex == self._preferred_ex and self._fallback_ex:
                    active_ex = self._fallback_ex
                    _fallback_since = time.time()
                    logger.info(
                        f"[PriceStreamer] switching to fallback: {active_ex}")
                else:
                    active_ex = self._preferred_ex
                    _fallback_since = 0.0

                await asyncio.sleep(backoff)

        if exchange:
            try:
                await exchange.close()
            except Exception:
                pass

    async def _crypto_rest_fallback(self):
        """Fallback when ccxt.pro unavailable: poll crypto prices via REST.
        Sync CCXT uses socket.getaddrinfo which works with system DNS."""
        try:
            import ccxt
        except ImportError:
            logger.error("[PriceStreamer] ccxt not installed — no crypto prices")
            return

        while self._running:
            try:
                with self._sub_lock:
                    symbols = list(self._crypto_syms)
                if not symbols:
                    await asyncio.sleep(2)
                    continue

                ex = getattr(ccxt, self._preferred_ex)(
                    {"enableRateLimit": True})
                ex.load_markets()
                for sym in symbols:
                    if not self._running:
                        break
                    try:
                        cs = _resolve_ccxt_symbol(
                            self.db, sym, self._preferred_ex)
                        if cs:
                            tk = ex.fetch_ticker(cs)
                            if tk:
                                self._handle_update(
                                    sym, price=tk.get("last", 0),
                                    bid=tk.get("bid"), ask=tk.get("ask"),
                                    source=self._preferred_ex,
                                )
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)

                await asyncio.sleep(3)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[PriceStreamer] crypto REST fallback error: {e}")
                await asyncio.sleep(10)

    # ── Yahoo Finance: REST poll ───────────────────────────────────────

    async def _yahoo_poll(self):
        """Poll Yahoo Finance chart API for non-crypto symbols."""
        while self._running:
            try:
                with self._sub_lock:
                    symbols = list(self._yahoo_syms)
                if not symbols:
                    await asyncio.sleep(2)
                    continue

                loop = asyncio.get_event_loop()
                for sym in symbols:
                    if not self._running:
                        break
                    try:
                        yahoo_sym = YAHOO_SYMBOL_MAP.get(sym)
                        if not yahoo_sym and self.db:
                            row = self.db.fetch_one(
                                "SELECT yahoo_ticker FROM market_symbols "
                                "WHERE symbol = %s", (sym,))
                            if row and row.get("yahoo_ticker"):
                                yahoo_sym = row["yahoo_ticker"]
                        if not yahoo_sym:
                            continue

                        price = await loop.run_in_executor(
                            None, self._yahoo_fetch_price, yahoo_sym)
                        if price and price > 0:
                            self._handle_update(sym, price=price, source="yahoo")
                    except Exception as e:
                        logger.debug(f"[PriceStreamer] yahoo error {sym}: {e}")
                    await asyncio.sleep(1)

                remaining = max(0.0, self._yahoo_interval - len(symbols))
                await asyncio.sleep(remaining)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[PriceStreamer] yahoo poll loop error: {e}")
                await asyncio.sleep(10)

    @staticmethod
    def _yahoo_fetch_price(yahoo_sym: str) -> float:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}"
               f"?range=1d&interval=1m")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=8)
        data = _json.loads(resp.read())
        meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
        return float(meta.get("regularMarketPrice", 0))

    # ── MT5: tick poll ─────────────────────────────────────────────────

    async def _mt5_poll(self):
        """Poll MT5 for forex/metal tick prices (only if MT5 connected)."""
        while self._running:
            try:
                from core.mt5_manager import get_mt5_manager
                mgr = get_mt5_manager()
                if not mgr or not mgr.is_connected():
                    await asyncio.sleep(15)
                    continue

                with self._sub_lock:
                    yahoo = list(self._yahoo_syms)
                mt5_syms = [s for s in yahoo if not _looks_like_crypto(s)]
                if not mt5_syms:
                    await asyncio.sleep(5)
                    continue

                loop = asyncio.get_event_loop()
                for sym in mt5_syms:
                    if not self._running:
                        break
                    try:
                        tick = await loop.run_in_executor(
                            None, mgr.get_current_price, sym)
                        if tick and hasattr(tick, "bid") and tick.bid:
                            mid = (tick.bid + tick.ask) / 2
                            self._handle_update(
                                sym, price=mid, bid=tick.bid,
                                ask=tick.ask, source="mt5")
                    except Exception:
                        pass

                await asyncio.sleep(self._mt5_interval)
            except ImportError:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(10)

    # ── DB write-back ──────────────────────────────────────────────────

    async def _db_writeback(self):
        """Batch-write cached prices to live_prices table and dossier current_price."""
        while self._running:
            await asyncio.sleep(self._db_wb_interval)
            try:
                prices = self.cache.get_all()
                if not prices:
                    continue

                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._do_db_writeback, prices)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[PriceStreamer] DB writeback error: {e}")

    def _do_db_writeback(self, prices: Dict[str, Dict]):
        for sym, d in prices.items():
            try:
                self.db.execute("""
                    INSERT INTO live_prices (symbol, price, bid, ask, source, updated_at)
                    VALUES (%s, %s, %s, %s, %s, NOW(3))
                    ON DUPLICATE KEY UPDATE
                        price = VALUES(price), bid = VALUES(bid),
                        ask = VALUES(ask), source = VALUES(source),
                        updated_at = NOW(3)
                """, (sym, d["price"], d.get("bid"), d.get("ask"),
                      d.get("source", "")))
            except Exception:
                pass

        for sym, d in prices.items():
            try:
                self.db.execute("""
                    UPDATE trade_dossiers
                    SET current_price = %s, current_price_at = NOW()
                    WHERE symbol = %s
                      AND status IN ('proposed','monitoring','open_order','live')
                """, (round(d["price"], 5), sym))
            except Exception:
                pass

    # ── Common price handler ───────────────────────────────────────────

    def _handle_update(self, symbol: str, price: float, bid: float = None,
                       ask: float = None, source: str = "", ts: float = None):
        if not price or price <= 0:
            return

        changed = self.cache.update(symbol, price, bid, ask, source, ts)

        triggered = self.levels.check(symbol, price)
        for w in triggered:
            for cb in self._on_level_cbs:
                try:
                    cb(w, price)
                except Exception as e:
                    logger.debug(f"[PriceStreamer] level callback error: {e}")

        if changed:
            data = self.cache.get(symbol)
            for cb in self._on_update_cbs:
                try:
                    cb(symbol, data)
                except Exception as e:
                    logger.debug(f"[PriceStreamer] update callback error: {e}")

        aliases = self._canonical_to_aliases.get(symbol, set())
        for alias in aliases:
            self.cache.update(alias, price, bid, ask, source, ts)
            self.levels.check(alias, price)
            if changed:
                for cb in self._on_update_cbs:
                    try:
                        cb(alias, data)
                    except Exception:
                        pass


# ═══════════════════════════════════════════════════════════════════════
# SINGLETON
# ═══════════════════════════════════════════════════════════════════════

_instance: Optional[PriceStreamer] = None


def get_price_streamer() -> Optional[PriceStreamer]:
    return _instance


def start_price_streamer(db, config) -> PriceStreamer:
    global _instance
    if _instance:
        return _instance
    _instance = PriceStreamer(db, config)
    _instance.start()
    return _instance
