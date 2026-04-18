"""
ccxt_executor.py — Exchange order execution via CCXT (Blofin, Bybit, etc.)

Provides CCXTExecutor for placing limit orders, managing positions,
fetching balances, and resolving symbols on crypto perpetual futures exchanges.

Thread-safe: one executor instance per account, cached in _executor_cache.

Usage:
    from core.ccxt_executor import get_executor
    ex = get_executor("blofin_comp", db)
    result = ex.place_limit_order("BTC/USDT:USDT", "buy", 0.001, 95000)
"""

import logging
import socket
import struct
import threading
import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Any

logger = logging.getLogger("jarvais.ccxt_executor")

_executor_cache: Dict[str, "CCXTExecutor"] = {}
_cache_lock = threading.Lock()
_dns_lock = threading.Lock()


# ── Google DNS fallback (pure Python, no extra dependencies) ──────────

def _google_dns_resolve(host: str) -> Optional[str]:
    """Resolve a hostname to an IPv4 address via Google Public DNS (8.8.8.8).
    Uses a raw UDP DNS query — zero external dependencies."""
    try:
        qid = struct.pack(">H", int(time.time()) & 0xFFFF)
        flags = struct.pack(">H", 0x0100)
        counts = struct.pack(">HHHH", 1, 0, 0, 0)
        question = b""
        for part in host.encode().split(b"."):
            question += bytes([len(part)]) + part
        question += b"\x00" + struct.pack(">HH", 1, 1)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        try:
            sock.sendto(qid + flags + counts + question, ("8.8.8.8", 53))
            data, _ = sock.recvfrom(1024)
        finally:
            sock.close()

        ans_count = struct.unpack(">H", data[6:8])[0]
        if ans_count == 0:
            return None

        offset = 12
        while data[offset] != 0:
            offset += data[offset] + 1
        offset += 5

        for _ in range(ans_count):
            if data[offset] & 0xC0 == 0xC0:
                offset += 2
            else:
                while data[offset] != 0:
                    offset += data[offset] + 1
                offset += 1
            rtype, _, _, rdlen = struct.unpack(">HHIH", data[offset:offset + 10])
            offset += 10
            if rtype == 1 and rdlen == 4:
                return ".".join(str(b) for b in data[offset:offset + 4])
            offset += rdlen
        return None
    except Exception:
        return None


@contextmanager
def _use_google_dns():
    """Temporarily patch socket.getaddrinfo to resolve via Google DNS (8.8.8.8).
    Falls through to system DNS if Google DNS can't resolve.
    Thread-safe: uses _dns_lock to prevent concurrent monkey-patch corruption."""
    _cache: Dict[str, str] = {}

    with _dns_lock:
        original = socket.getaddrinfo

        def _patched(host, port, *args, **kwargs):
            if isinstance(host, str) and not host.replace(".", "").isdigit():
                if host not in _cache:
                    ip = _google_dns_resolve(host)
                    if ip:
                        _cache[host] = ip
                        logger.debug(f"[GoogleDNS] Resolved {host} -> {ip}")
                if host in _cache:
                    return original(_cache[host], port, *args, **kwargs)
            return original(host, port, *args, **kwargs)

        socket.getaddrinfo = _patched
        try:
            yield
        finally:
            socket.getaddrinfo = original


def get_executor(account_id: str, db=None) -> Optional["CCXTExecutor"]:
    """Get or create a cached CCXTExecutor for a trading_accounts row.
    Thread-safe: holds _cache_lock for the entire creation path to prevent
    two threads from both creating executors for the same account."""
    with _cache_lock:
        if account_id in _executor_cache:
            return _executor_cache[account_id]

        if not db:
            return None
        row = db.fetch_one(
            "SELECT * FROM trading_accounts WHERE account_id = %s AND enabled = 1",
            (account_id,))
        if not row:
            logger.warning(f"[CCXTExec] Account '{account_id}' not found or disabled")
            return None

        ex = CCXTExecutor(row)
        if ex.connect():
            _executor_cache[account_id] = ex
            return ex
        return None


def invalidate_executor(account_id: str):
    """Remove a cached executor (e.g. after credential change)."""
    with _cache_lock:
        _executor_cache.pop(account_id, None)


class CCXTExecutor:
    """Exchange connectivity and order execution via CCXT unified API."""

    _ENV_KEY_MAP = {
        "bybit":  ("BYBIT_API_KEY",  "BYBIT_SECRET",      "BYBIT_PASSPHRASE"),
        "blofin": ("BLOFIN_API_KEY", "BLOFIN_API_SECRET",  "BLOFIN_API_PHRASE"),
        "bitget": ("BITGET_API_KEY", "BITGET_API_SECRET",  "BITGET_API_PHASE"),
    }
    _ENV_KEY_MAP_DEMO = {
        "bybit":  ("BYBIT_DEMO_API_KEY",  "BYBIT_DEMO_API_SECRET", "BYBIT_DEMO_PASSPHRASE"),
        "blofin": ("BLOFIN_DEMO_API_KEY", "BLOFIN_DEMO_API_SECRET", "BLOFIN_DEMO_API_PHRASE"),
        "bitget": ("BITGET_API_KEY",      "BITGET_API_SECRET",      "BITGET_API_PHASE"),
    }

    def __init__(self, account_config: dict):
        import os
        self.account_id = account_config["account_id"]
        self.exchange_id = (account_config.get("exchange") or "").strip().lower()
        self.name = account_config.get("name", self.account_id)
        self.last_error = None

        acct_type = (account_config.get("account_type") or "live").lower()
        env_map = (self._ENV_KEY_MAP_DEMO if acct_type == "demo"
                   else self._ENV_KEY_MAP)
        env_vars = env_map.get(self.exchange_id, ("", "", ""))
        self._api_key = account_config.get("api_key", "") or os.getenv(env_vars[0], "")
        self._api_secret = account_config.get("api_secret", "") or os.getenv(env_vars[1], "")
        self._passphrase = account_config.get("api_passphrase", "") or os.getenv(env_vars[2], "")

        if not account_config.get("api_key") and self._api_key:
            logger.info(f"[CCXTExec] {self.account_id}: Using .env keys for {self.exchange_id}")

        self._testnet = bool(account_config.get("testnet", 0))
        self._account_type = (account_config.get("account_type") or "live").lower()
        self._exchange = None
        self._markets_loaded = False
        self._lock = threading.Lock()
        self._funding_cache: Dict[str, Any] = {}

        # Simple token-bucket rate limiter: max N calls per second
        self._rate_limit_calls = 5   # max calls per window
        self._rate_limit_window = 1.0  # seconds
        self._call_timestamps: list = []

    def connect(self) -> bool:
        """Initialize the CCXT exchange instance and load markets.

        Connection modes (determined by account_type and testnet):
          - account_type='demo' → exchange-specific demo handling
            - Bybit demo: api-demo.bybit.com (NOT testnet, NOT sandbox)
            - BloFin demo: sandbox mode + brokerId="" (CCXT's hardcoded
              brokerId is rejected by BloFin's demo endpoint)
          - testnet=1 (and not demo) → standard CCXT sandbox mode
          - Otherwise → live/mainnet
        """
        try:
            import ccxt
        except ImportError:
            logger.error("[CCXTExec] ccxt package not installed")
            return False

        if not self.exchange_id:
            self.last_error = "Exchange not set"
            logger.error(f"[CCXTExec] No exchange configured for {self.account_id}")
            return False
        cls = getattr(ccxt, self.exchange_id, None)
        if not cls:
            self.last_error = f"Unknown exchange: '{self.exchange_id}'"
            logger.error(f"[CCXTExec] Unknown exchange: {self.exchange_id}")
            return False

        is_demo = self._account_type == "demo"
        is_testnet = self._testnet and not is_demo

        opts = {
            "apiKey": self._api_key,
            "secret": self._api_secret,
            "enableRateLimit": True,
            "timeout": 15000,
            "options": {"defaultType": "swap"},
        }
        if self._passphrase:
            opts["password"] = self._passphrase

        if self.exchange_id == "bitget":
            opts["options"]["defaultType"] = "swap"
            opts["options"]["defaultSubType"] = "linear"

        try:
            # ── BYBIT DEMO ──────────────────────────────────────
            # Bybit demo keys only work with api-demo.bybit.com.
            # set_sandbox_mode() would point to api-testnet.bybit.com
            # which is a completely separate environment (error 10003).
            # The demo endpoint has no public market data, so we
            # load markets from mainnet first and copy them over.
            if is_demo and self.exchange_id == "bybit":
                public = cls({"enableRateLimit": True,
                              "options": {"defaultType": "swap"}})
                public.load_markets()

                self._exchange = cls(opts)
                self._exchange.markets = public.markets
                self._exchange.markets_by_id = public.markets_by_id
                self._exchange.currencies = public.currencies
                self._exchange.currencies_by_id = public.currencies_by_id

                demo_base = "https://api-demo.bybit.com"
                for api_type in list(self._exchange.urls["api"].keys()):
                    url = self._exchange.urls["api"][api_type]
                    if isinstance(url, str):
                        self._exchange.urls["api"][api_type] = demo_base

                self._markets_loaded = True
                logger.info(f"[CCXTExec] Connected to bybit (DEMO) "
                            f"for '{self.account_id}' — "
                            f"{len(self._exchange.markets)} markets")
                return True

            # ── BLOFIN DEMO ─────────────────────────────────────
            # CCXT hardcodes brokerId ec6dd3a7dd982d0b for BloFin.
            # The demo endpoint rejects requests with a brokerId
            # (error 152401 "Access key does not exist").
            elif is_demo and self.exchange_id == "blofin":
                opts["options"]["brokerId"] = ""
                self._exchange = cls(opts)
                self._exchange.set_sandbox_mode(True)
                self._exchange.load_markets()
                self._markets_loaded = True
                logger.info(f"[CCXTExec] Connected to blofin (DEMO) "
                            f"for '{self.account_id}' — "
                            f"{len(self._exchange.markets)} markets")
                return True

            # ── BITGET (no demo/testnet endpoint, live only) ─────
            elif self.exchange_id == "bitget":
                self._exchange = cls(opts)
                self._exchange.load_markets()
                self._markets_loaded = True
                logger.info(f"[CCXTExec] Connected to bitget "
                            f"(live) for '{self.account_id}' — "
                            f"{len(self._exchange.markets)} markets")
                return True

            # ── TESTNET (any other exchange) ──────────────────────
            elif is_testnet:
                opts["sandbox"] = True
                self._exchange = cls(opts)
                self._exchange.set_sandbox_mode(True)
                self._exchange.load_markets()
                self._markets_loaded = True
                logger.info(f"[CCXTExec] Connected to {self.exchange_id} "
                            f"(testnet) for '{self.account_id}' — "
                            f"{len(self._exchange.markets)} markets")
                return True

            # ── LIVE ─────────────────────────────────────────────
            else:
                self._exchange = cls(opts)
                self._exchange.load_markets()
                self._markets_loaded = True
                logger.info(f"[CCXTExec] Connected to {self.exchange_id} "
                            f"(live) for '{self.account_id}' — "
                            f"{len(self._exchange.markets)} markets")
                return True

        except Exception as e:
            self.last_error = str(e)
            self._exchange = None

            # Retry once with Google DNS fallback
            dns_hint = ("dns" in str(e).lower() or "connect" in str(e).lower()
                        or "network" in str(e).lower() or "timeout" in str(e).lower())
            if dns_hint:
                logger.info(f"[CCXTExec] Retrying {self.exchange_id} with Google DNS "
                            f"for '{self.account_id}' ...")
                try:
                    with _use_google_dns():
                        return self._connect_inner(cls, opts, is_demo, is_testnet)
                except Exception as e2:
                    self.last_error = str(e2)
                    logger.error(f"[CCXTExec] Google DNS retry also failed: {e2}")
                    self._exchange = None
                    return False

            logger.error(f"[CCXTExec] Connect failed for {self.account_id}: {e}")
            return False

    def _connect_inner(self, cls, opts: dict, is_demo: bool, is_testnet: bool) -> bool:
        """Shared connection logic used by connect() and its DNS retry."""
        if is_demo and self.exchange_id == "bybit":
            public = cls({"enableRateLimit": True,
                          "options": {"defaultType": "swap"}})
            public.load_markets()
            self._exchange = cls(opts)
            self._exchange.markets = public.markets
            self._exchange.markets_by_id = public.markets_by_id
            self._exchange.currencies = public.currencies
            self._exchange.currencies_by_id = public.currencies_by_id
            demo_base = "https://api-demo.bybit.com"
            for api_type in list(self._exchange.urls["api"].keys()):
                url = self._exchange.urls["api"][api_type]
                if isinstance(url, str):
                    self._exchange.urls["api"][api_type] = demo_base
            self._markets_loaded = True
            logger.info(f"[CCXTExec] Connected to bybit (DEMO+GoogleDNS) "
                        f"for '{self.account_id}' — "
                        f"{len(self._exchange.markets)} markets")
            return True
        elif is_demo and self.exchange_id == "blofin":
            opts["options"]["brokerId"] = ""
            self._exchange = cls(opts)
            self._exchange.set_sandbox_mode(True)
            self._exchange.load_markets()
            self._markets_loaded = True
            logger.info(f"[CCXTExec] Connected to blofin (DEMO+GoogleDNS) "
                        f"for '{self.account_id}' — "
                        f"{len(self._exchange.markets)} markets")
            return True
        elif self.exchange_id == "bitget":
            self._exchange = cls(opts)
            self._exchange.load_markets()
            self._markets_loaded = True
            logger.info(f"[CCXTExec] Connected to bitget "
                        f"(live+GoogleDNS) for '{self.account_id}' — "
                        f"{len(self._exchange.markets)} markets")
            return True
        elif is_testnet:
            opts["sandbox"] = True
            self._exchange = cls(opts)
            self._exchange.set_sandbox_mode(True)
            self._exchange.load_markets()
            self._markets_loaded = True
            logger.info(f"[CCXTExec] Connected to {self.exchange_id} "
                        f"(testnet+GoogleDNS) for '{self.account_id}' — "
                        f"{len(self._exchange.markets)} markets")
            return True
        else:
            self._exchange = cls(opts)
            self._exchange.load_markets()
            self._markets_loaded = True
            logger.info(f"[CCXTExec] Connected to {self.exchange_id} "
                        f"(live+GoogleDNS) for '{self.account_id}' — "
                        f"{len(self._exchange.markets)} markets")
            return True

    @property
    def connected(self) -> bool:
        return self._exchange is not None and self._markets_loaded

    def test_connection(self) -> Dict[str, Any]:
        """Test exchange connectivity. Returns balance on success."""
        if not self.connected:
            ok = self.connect()
            if not ok:
                return {"success": False, "error": "Connection failed"}
        try:
            bal = self.get_balance()
            return {
                "success": True,
                "exchange": self.exchange_id,
                "testnet": self._testnet,
                "balance": bal,
                "markets": len(self._exchange.markets),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ─────────────────────────────────────────────────────────────
    # Balance & Account Info
    # ─────────────────────────────────────────────────────────────

    def get_balance(self) -> Dict[str, float]:
        """Fetch account balance. Returns {total, free, used} in USDT."""
        self._ensure_connected()
        with self._lock:
            bal = self._exchange.fetch_balance({"type": "swap"})
        # CCXT returns {"USDT": {"total":X,"free":Y,"used":Z}, "total":{"USDT":X,...}}
        # Try the per-currency nested dict first (has total/free/used keys)
        usdt_nested = bal.get("USDT")
        if (isinstance(usdt_nested, dict)
                and ("total" in usdt_nested or "free" in usdt_nested)):
            return {
                "total": float(usdt_nested.get("total", 0) or 0),
                "free": float(usdt_nested.get("free", 0) or 0),
                "used": float(usdt_nested.get("used", 0) or 0),
            }
        # Fallback: read from the flat currency dicts
        total = float(bal.get("total", {}).get("USDT", 0) or 0)
        free = float(bal.get("free", {}).get("USDT", 0) or 0)
        used = float(bal.get("used", {}).get("USDT", 0) or 0)
        if total == 0 and free == 0:
            for key in ("USDT", "USD", "BUSD", "USDC"):
                t = float(bal.get("total", {}).get(key, 0) or 0)
                if t > 0:
                    total = t
                    free = float(bal.get("free", {}).get(key, 0) or 0)
                    used = float(bal.get("used", {}).get(key, 0) or 0)
                    break
        return {"total": total, "free": free, "used": used or (total - free)}

    # ─────────────────────────────────────────────────────────────
    # Positions & Orders
    # ─────────────────────────────────────────────────────────────

    def get_positions(self) -> List[Dict]:
        """Fetch all open positions."""
        self._rate_limit_wait()
        self._ensure_connected()
        with self._lock:
            positions = self._exchange.fetch_positions()
        result = []
        for p in positions:
            size = float(p.get("contracts", 0) or 0)
            if size == 0:
                notional = float(p.get("notional", 0) or 0)
                if notional == 0:
                    continue
            result.append({
                "symbol": p.get("symbol", ""),
                "side": p.get("side", ""),
                "contracts": float(p.get("contracts", 0) or 0),
                "notional": float(p.get("notional", 0) or 0),
                "entry_price": float(p.get("entryPrice", 0) or 0),
                "mark_price": float(p.get("markPrice", 0) or 0),
                "liquidation_price": float(p.get("liquidationPrice", 0) or 0),
                "unrealised_pnl": float(p.get("unrealizedPnl", 0) or 0),
                "leverage": int(p.get("leverage", 1) or 1),
                "margin": float(p.get("initialMargin", 0) or p.get("collateral", 0) or 0),
                "percentage": float(p.get("percentage", 0) or 0),
            })
        return result

    def get_open_orders(self, symbol: str = None) -> List[Dict]:
        """Fetch all open limit orders, optionally filtered by symbol."""
        self._ensure_connected()
        with self._lock:
            orders = self._exchange.fetch_open_orders(symbol)
        return [self._normalize_order(o) for o in orders]

    def get_closed_trades(self, symbol: str = None, since_ms: int = None,
                          limit: int = 50) -> List[Dict]:
        """Fetch recent closed/filled orders."""
        self._ensure_connected()
        with self._lock:
            orders = self._exchange.fetch_closed_orders(symbol, since=since_ms, limit=limit)
        return [self._normalize_order(o) for o in orders]

    def get_recent_fills(self, symbol: str = None, since_ms: int = None,
                         limit: int = 20) -> List[Dict]:
        """Fetch actual trade fills (not orders) — used for accurate exit price.

        Unlike fetch_closed_orders (which returns the entry order), fetch_my_trades
        returns each individual fill with the real execution price and fees.
        """
        self._ensure_connected()
        try:
            with self._lock:
                trades = self._exchange.fetch_my_trades(symbol, since=since_ms, limit=limit)
            result = []
            for t in trades:
                fee_info = t.get("fee") or {}
                result.append({
                    "id": t.get("id", ""),
                    "order_id": t.get("order", ""),
                    "symbol": t.get("symbol", ""),
                    "side": t.get("side", ""),
                    "amount": float(t.get("amount", 0) or 0),
                    "price": float(t.get("price", 0) or 0),
                    "cost": float(t.get("cost", 0) or 0),
                    "fee": float(fee_info.get("cost", 0) or 0),
                    "fee_currency": fee_info.get("currency", ""),
                    "timestamp": t.get("timestamp"),
                })
            return result
        except Exception as e:
            logger.warning(f"[CCXTExec] get_recent_fills failed for {symbol}: {e}")
            return []

    def fetch_funding_rate(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch current funding rate for a perpetual symbol. Cached 30 min.
        Returns {"rate": float, "timestamp": int} or None on failure."""
        cache_key = f"{self.exchange_id}:{symbol}"
        now_ms = int(time.time() * 1000)
        if cache_key in self._funding_cache:
            cached = self._funding_cache[cache_key]
            if now_ms - cached.get("_fetched_at", 0) < 30 * 60 * 1000:
                return {"rate": cached.get("rate", 0), "timestamp": cached.get("timestamp", now_ms)}
        self._rate_limit_wait()
        self._ensure_connected()
        try:
            with self._lock:
                data = self._exchange.fetch_funding_rate(symbol)
            rate = float(data.get("fundingRate", 0) or 0)
            ts = int(data.get("timestamp", now_ms) or now_ms)
            with self._lock:
                self._funding_cache[cache_key] = {"rate": rate, "timestamp": ts, "_fetched_at": now_ms}
            return {"rate": rate, "timestamp": ts}
        except Exception as e:
            logger.debug(f"[CCXTExec] fetch_funding_rate failed for {symbol}: {e}")
            return None

    def fetch_open_interest(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch current open interest for a perpetual symbol. Cached 15 min.
        Returns {"oi_base": float, "oi_value": float|None, "timestamp": int} or None."""
        cache_key = f"oi:{self.exchange_id}:{symbol}"
        now_ms = int(time.time() * 1000)
        if cache_key in self._funding_cache:
            cached = self._funding_cache[cache_key]
            if now_ms - cached.get("_fetched_at", 0) < 15 * 60 * 1000:
                return {k: v for k, v in cached.items() if k != "_fetched_at"}
        self._rate_limit_wait()
        self._ensure_connected()
        try:
            with self._lock:
                data = self._exchange.fetch_open_interest(symbol)
            oi_base = float(data.get("openInterestAmount", 0) or 0)
            oi_value = data.get("openInterestValue")
            if oi_value is not None:
                oi_value = float(oi_value)
            ts = int(data.get("timestamp", now_ms) or now_ms)
            result = {"oi_base": oi_base, "oi_value": oi_value, "timestamp": ts}
            with self._lock:
                self._funding_cache[cache_key] = {**result, "_fetched_at": now_ms}
            return result
        except Exception as e:
            err_str = str(e).lower()
            if "not supported" in err_str or "contract" in err_str:
                logger.debug(f"[CCXTExec] OI not supported for {symbol} on {self.exchange_id}")
            else:
                logger.debug(f"[CCXTExec] fetch_open_interest failed for {symbol}: {e}")
            return None

    def fetch_long_short_ratio(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch current long/short ratio for a perpetual symbol. Cached 15 min.
        Returns {"ratio": float, "timestamp": int} or None. ratio >1 = more longs."""
        cache_key = f"ls:{self.exchange_id}:{symbol}"
        now_ms = int(time.time() * 1000)
        if cache_key in self._funding_cache:
            cached = self._funding_cache[cache_key]
            if now_ms - cached.get("_fetched_at", 0) < 15 * 60 * 1000:
                return {k: v for k, v in cached.items() if k != "_fetched_at"}
        self._rate_limit_wait()
        self._ensure_connected()
        try:
            with self._lock:
                data = self._exchange.fetch_long_short_ratio_history(symbol, limit=1)
            if not data:
                return None
            entry = data[0]
            ratio = float(entry.get("longShortRatio", 1.0) or 1.0)
            ts = int(entry.get("timestamp", now_ms) or now_ms)
            result = {"ratio": ratio, "timestamp": ts}
            with self._lock:
                self._funding_cache[cache_key] = {**result, "_fetched_at": now_ms}
            return result
        except Exception as e:
            err_str = str(e).lower()
            if "not supported" in err_str or "linear" in err_str:
                logger.debug(f"[CCXTExec] L/S ratio not supported for {symbol} on {self.exchange_id}")
            else:
                logger.debug(f"[CCXTExec] fetch_long_short_ratio failed for {symbol}: {e}")
            return None

    # ─────────────────────────────────────────────────────────────
    # Order Execution
    # ─────────────────────────────────────────────────────────────

    def _rate_limit_wait(self):
        """Token-bucket rate limiter. Sleeps outside the lock to avoid
        blocking other threads during the wait period."""
        sleep_for = 0
        with self._lock:
            now = time.time()
            self._call_timestamps = [t for t in self._call_timestamps
                                     if now - t < self._rate_limit_window]
            if len(self._call_timestamps) >= self._rate_limit_calls:
                sleep_for = self._rate_limit_window - (now - self._call_timestamps[0])
            if len(self._funding_cache) > 500:
                cutoff = int(now * 1000) - 30 * 60 * 1000
                self._funding_cache = {k: v for k, v in self._funding_cache.items()
                                       if v.get("_fetched_at", 0) > cutoff}
        if sleep_for > 0:
            time.sleep(sleep_for)
        with self._lock:
            self._call_timestamps.append(time.time())

    def place_limit_order(self, exchange_symbol: str, side: str,
                          amount: float, price: float,
                          sl: float = None, tp: float = None,
                          params: dict = None) -> Dict[str, Any]:
        """Place a limit order. Returns order info dict or error."""
        self._rate_limit_wait()
        self._ensure_connected()

        if exchange_symbol and ":" not in exchange_symbol and "/" in exchange_symbol:
            logger.error(f"[CCXTExec] BLOCKED: '{exchange_symbol}' looks like a "
                         f"SPOT symbol (no settle currency). Only perpetual "
                         f"futures (e.g. BTC/USDT:USDT) are allowed.")
            return {"success": False,
                    "error": f"Spot symbol rejected: {exchange_symbol}"}

        side = side.lower()
        if side not in ("buy", "sell"):
            return {"success": False, "error": f"Invalid side: {side}"}
        if amount <= 0:
            return {"success": False, "error": f"Invalid amount: {amount}"}
        if price <= 0:
            return {"success": False, "error": f"Invalid price: {price}"}

        order_params = params or {}

        # NOTE: CCXT 4.4.100+ ships a valid brokerId for Blofin.
        # Do NOT override it — error 152012 "brokerId is required".
        # (Old override `brokerId=None` removed 2026-03-04.)

        # Force isolated margin on the order itself — set_margin_mode can
        # silently fail on some exchanges (BloFin demo in particular).
        if "marginMode" not in order_params:
            order_params["marginMode"] = "isolated"

        # Bitget one-way (unilateral) mode: error 40774 occurs when the
        # account is set to one-way position mode but the order doesn't
        # declare it.  Setting oneWayMode tells CCXT to use the correct
        # tradeSide / productType so Bitget accepts the order.
        if self.exchange_id == "bitget":
            order_params.setdefault("oneWayMode", True)

        if sl and sl > 0:
            order_params["stopLoss"] = {"triggerPrice": sl, "type": "market"}
        if tp and tp > 0:
            order_params["takeProfit"] = {"triggerPrice": tp, "type": "market"}

        logger.info(f"[CCXTExec] Placing LIMIT {side.upper()} {amount} "
                    f"{exchange_symbol} @ {price} | SL={sl} TP={tp} "
                    f"| account={self.account_id}")

        last_err = None
        for attempt in range(2):
            try:
                if attempt == 1:
                    logger.info(f"[CCXTExec] Retrying order with Google DNS ...")
                ctx = _use_google_dns() if attempt == 1 else contextmanager(lambda: (yield))()
                with ctx:
                    with self._lock:
                        result = self._exchange.create_order(
                            symbol=exchange_symbol,
                            type="limit",
                            side=side,
                            amount=amount,
                            price=price,
                            params=order_params,
                        )
                order_id = result.get("id", "")
                logger.info(f"[CCXTExec] Order placed: {order_id} on {self.exchange_id}"
                            f"{' (via GoogleDNS)' if attempt == 1 else ''}")
                return {
                    "success": True,
                    "order_id": order_id,
                    "exchange": self.exchange_id,
                    "symbol": exchange_symbol,
                    "side": side,
                    "amount": amount,
                    "price": price,
                    "status": result.get("status", "open"),
                    "raw": result,
                }
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                is_network = any(k in err_str for k in
                                 ("dns", "connect", "network", "timeout", "unreachable"))
                if attempt == 0 and is_network:
                    time.sleep(1)
                    self._ensure_connected()
                    continue
                break

        logger.error(f"[CCXTExec] Order failed: {last_err}")
        return {"success": False, "error": str(last_err)}

    def cancel_order(self, order_id: str, exchange_symbol: str) -> Dict[str, Any]:
        """Cancel an open order. Retries with Google DNS on network failure."""
        self._rate_limit_wait()
        self._ensure_connected()
        last_err = None
        for attempt in range(2):
            try:
                ctx = _use_google_dns() if attempt == 1 else contextmanager(lambda: (yield))()
                with ctx:
                    with self._lock:
                        result = self._exchange.cancel_order(order_id, exchange_symbol)
                logger.info(f"[CCXTExec] Cancelled order {order_id}")
                return {"success": True, "order_id": order_id, "raw": result}
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                if attempt == 0 and any(k in err_str for k in
                                        ("dns", "connect", "network", "timeout")):
                    time.sleep(1)
                    continue
                break
        logger.error(f"[CCXTExec] Cancel failed for {order_id}: {last_err}")
        return {"success": False, "error": str(last_err)}

    def close_position(self, exchange_symbol: str, side: str,
                       amount: float = None) -> Dict[str, Any]:
        """Close an open position (full or partial) with a market order.
        Retries with Google DNS on network failure."""
        self._rate_limit_wait()
        self._ensure_connected()
        close_side = "sell" if side.lower() in ("buy", "long") else "buy"

        if not amount or amount <= 0:
            try:
                positions = self.get_positions()
                import re as _re
                clean = _re.sub(r'[/:\-_\s.]', '', exchange_symbol.upper())
                pos = None
                for p in positions:
                    if abs(p.get("contracts", 0)) == 0 and abs(p.get("notional", 0)) == 0:
                        continue
                    p_clean = _re.sub(r'[/:\-_\s.]', '', (p.get("symbol") or "").upper())
                    if p["symbol"] == exchange_symbol or p_clean == clean:
                        pos = p
                        break
                if not pos:
                    return {"success": False,
                            "error": f"No open position for {exchange_symbol}"}
                amount = abs(pos.get("contracts", 0))
                if amount <= 0:
                    amount = abs(pos.get("notional", 0))
                if amount <= 0:
                    return {"success": False, "error": "Position size is zero"}
            except Exception as e:
                return {"success": False, "error": f"Position lookup failed: {e}"}

        last_err = None
        for attempt in range(2):
            try:
                ctx = _use_google_dns() if attempt == 1 else contextmanager(lambda: (yield))()
                with ctx:
                    close_params = {"reduceOnly": True}
                    if self.exchange_id in ("blofin", "bitget"):
                        close_params["marginMode"] = "isolated"
                    if self.exchange_id == "bitget":
                        close_params["oneWayMode"] = True
                    with self._lock:
                        result = self._exchange.create_order(
                            symbol=exchange_symbol, type="market",
                            side=close_side, amount=amount,
                            params=close_params)
                logger.info(f"[CCXTExec] Closed position on {exchange_symbol} "
                            f"({close_side} {amount})")

                # Verify no residual dust remains after close
                time.sleep(0.5)
                try:
                    import re as _re2
                    clean = _re2.sub(r'[/:\-_\s.]', '', exchange_symbol.upper())
                    for p in self.get_positions():
                        p_clean = _re2.sub(
                            r'[/:\-_\s.]', '',
                            (p.get("symbol") or "").upper())
                        if (p["symbol"] == exchange_symbol or p_clean == clean):
                            residual = abs(p.get("contracts", 0))
                            # Only sweep genuine dust (< 10% of original).
                            # If residual == full amount, the exchange hasn't
                            # processed the close yet — don't double-close.
                            if 0 < residual < (amount * 0.10 if amount else float('inf')):
                                logger.warning(
                                    f"[CCXTExec] Residual dust after close: "
                                    f"{exchange_symbol} {residual} contracts "
                                    f"— closing remainder")
                                with self._lock:
                                    self._exchange.create_order(
                                        symbol=exchange_symbol, type="market",
                                        side=close_side, amount=residual,
                                        params={"reduceOnly": True})
                except Exception as dust_e:
                    logger.debug(f"[CCXTExec] Dust sweep check: {dust_e}")

                return {"success": True, "raw": result}
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                if attempt == 0 and any(k in err_str for k in
                                        ("dns", "connect", "network", "timeout")):
                    logger.info(f"[CCXTExec] Retrying close_position with Google DNS ...")
                    time.sleep(1)
                    continue
                break
        logger.error(f"[CCXTExec] Close position failed: {last_err}")
        return {"success": False, "error": str(last_err)}

    # ─────────────────────────────────────────────────────────────
    # Leverage
    # ─────────────────────────────────────────────────────────────

    def get_leverage_limits(self, exchange_symbol: str) -> Dict[str, Any]:
        """Get min/max leverage for a symbol."""
        self._ensure_connected()
        with self._lock:
            mkt = self._exchange.market(exchange_symbol)
        if not mkt:
            return {"min": 1, "max": 1}
        limits = mkt.get("limits", {}).get("leverage", {})
        return {
            "min": int(limits.get("min", 1) or 1),
            "max": int(limits.get("max", 100) or 100),
        }

    def get_ticker(self, exchange_symbol: str) -> Dict[str, Any]:
        """Fetch current ticker (last price, bid, ask) for a symbol."""
        self._ensure_connected()
        with self._lock:
            return self._exchange.fetch_ticker(exchange_symbol)

    def set_margin_mode(self, exchange_symbol: str,
                        mode: str = "isolated") -> Dict[str, Any]:
        """Set margin mode (isolated or cross) for a symbol.
        Should be called before set_leverage / placing orders."""
        self._ensure_connected()

        # Bitget: ensure one-way position mode is set before margin/leverage.
        # Without this, order placement fails with error 40774.
        if self.exchange_id == "bitget":
            try:
                with self._lock:
                    self._exchange.set_position_mode(False, exchange_symbol)
            except Exception as e:
                err_s = str(e).lower()
                if not any(k in err_s for k in ("already", "same", "no need",
                                                 "not modified", "not change")):
                    logger.debug(f"[CCXTExec] set_position_mode(one-way) note "
                                 f"for {exchange_symbol}: {e}")

        try:
            with self._lock:
                params = {}
                if self.exchange_id in ("blofin", "bitget"):
                    params["marginMode"] = mode
                result = self._exchange.set_margin_mode(
                    mode, exchange_symbol, params=params)
            logger.info(f"[CCXTExec] Set margin mode '{mode}' on "
                        f"{exchange_symbol} ({self.exchange_id})")
            return {"success": True, "mode": mode, "raw": result}
        except Exception as e:
            err = str(e).lower()
            if any(k in err for k in ("already", "same", "no need", "not modified",
                                       "margin mode is not")):
                return {"success": True, "mode": mode, "note": "already set"}
            logger.warning(f"[CCXTExec] Set margin mode failed for "
                           f"{exchange_symbol} ({self.exchange_id}): {e}")
            return {"success": False, "error": str(e)}

    def set_leverage(self, exchange_symbol: str, leverage: int,
                     margin_mode: str = "isolated") -> Dict[str, Any]:
        """Set leverage for a symbol. Must be called before placing orders."""
        self._ensure_connected()
        try:
            params = {}
            if self.exchange_id in ("blofin", "bitget"):
                params["marginMode"] = margin_mode
            with self._lock:
                result = self._exchange.set_leverage(
                    leverage, exchange_symbol, params=params)
            logger.info(f"[CCXTExec] Set leverage {leverage}x on "
                        f"{exchange_symbol} ({margin_mode})")
            return {"success": True, "leverage": leverage, "raw": result}
        except Exception as e:
            err_s = str(e).lower()
            if any(k in err_s for k in ("already", "same", "not modified",
                                         "no need", "not change")):
                return {"success": True, "leverage": leverage, "note": "already set"}
            # Blofin: "pending isolated orders" means leverage can't change
            # while orders exist — the current leverage is usable, proceed.
            if "pending" in err_s and "order" in err_s:
                logger.info(f"[CCXTExec] Leverage {leverage}x on {exchange_symbol}: "
                            f"pending orders exist, using current leverage")
                return {"success": True, "leverage": leverage,
                        "note": "pending orders, kept current"}
            logger.warning(f"[CCXTExec] Set leverage failed for {exchange_symbol}: {e}")
            return {"success": False, "error": str(e)}

    # ─────────────────────────────────────────────────────────────
    # Symbol Resolution
    # ─────────────────────────────────────────────────────────────

    def resolve_symbol(self, symbol: str, db=None) -> Optional[str]:
        """Resolve JarvAIs internal symbol to the exchange's CCXT ticker.

        Delegates to the unified resolve_for_exchange() in market_symbols.py,
        passing our exchange's loaded markets for the direct-match step.
        """
        from db.market_symbols import resolve_for_exchange

        exchange_markets = {}
        if self._markets_loaded and self._exchange:
            with self._lock:
                exchange_markets = dict(self._exchange.markets) if self._exchange else {}

        result = resolve_for_exchange(
            symbol, self.exchange_id, db=db,
            exchange_markets=exchange_markets)
        if not result:
            logger.warning(f"[CCXTExec] resolve_symbol: no match for '{symbol}' "
                           f"on {self.exchange_id}")
        return result

    def get_available_symbols(self) -> List[str]:
        """Return all active USDT perpetual futures on this exchange."""
        self._ensure_connected()
        with self._lock:
            markets = dict(self._exchange.markets)
        result = []
        for sym, mkt in markets.items():
            if not mkt.get("active", True):
                continue
            if mkt.get("type") == "swap" and mkt.get("quote", "").upper() == "USDT":
                result.append(sym)
        return sorted(result)

    def get_market_limits(self, exchange_symbol: str) -> Dict[str, float]:
        """Return min amount and min cost (notional) for a symbol.
        Used to validate paper fills and live orders against exchange minimums."""
        self._ensure_connected()
        with self._lock:
            mkt = self._exchange.markets.get(exchange_symbol) if exchange_symbol else None
        if not mkt:
            return {"min_amount": 0.0, "min_cost": 0.0}
        limits = mkt.get("limits", {}) or {}
        amount_lim = limits.get("amount", {}) or {}
        cost_lim = limits.get("cost", {}) or {}
        min_amount = float(amount_lim.get("min", 0) or 0)
        min_cost = float(cost_lim.get("min", 0) or 0)
        return {"min_amount": min_amount, "min_cost": min_cost}

    def price_to_precision(self, exchange_symbol: str, price: float) -> float:
        """Round price to exchange precision for the symbol."""
        self._ensure_connected()
        with self._lock:
            if exchange_symbol not in self._exchange.markets:
                return round(price, 8)
            try:
                return float(self._exchange.price_to_precision(exchange_symbol, price))
            except Exception:
                return round(price, 8)

    def amount_to_precision(self, exchange_symbol: str, amount: float) -> float:
        """Round amount/quantity to exchange precision for the symbol.
        Thread-safe — use this instead of accessing _exchange directly."""
        self._ensure_connected()
        with self._lock:
            try:
                return float(self._exchange.amount_to_precision(exchange_symbol, amount))
            except Exception:
                return round(amount, 8)

    def market_info(self, exchange_symbol: str) -> Optional[Dict[str, Any]]:
        """Get exchange market metadata for a symbol.
        Thread-safe — use this instead of accessing _exchange.market() directly."""
        self._ensure_connected()
        with self._lock:
            try:
                return self._exchange.market(exchange_symbol)
            except Exception:
                return None

    # ─────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────

    def _ensure_connected(self):
        """Reconnect if the exchange instance is missing or has no markets.
        Thread-safe: holds the lock during reconnection to prevent other
        threads from hitting a None exchange reference."""
        if self.connected and self._exchange and self._exchange.markets:
            return
        with self._lock:
            # Double-check under lock (another thread may have reconnected)
            if self.connected and self._exchange and self._exchange.markets:
                return
            self._markets_loaded = False
            self._exchange = None
            if not self.connect():
                raise ConnectionError(
                    f"Cannot connect to {self.exchange_id} for account {self.account_id}")

    @staticmethod
    def _normalize_order(o: dict) -> Dict[str, Any]:
        return {
            "id": o.get("id", ""),
            "symbol": o.get("symbol", ""),
            "type": o.get("type", ""),
            "side": o.get("side", ""),
            "amount": float(o.get("amount", 0) or 0),
            "price": float(o.get("price", 0) or 0),
            "filled": float(o.get("filled", 0) or 0),
            "remaining": float(o.get("remaining", 0) or 0),
            "status": o.get("status", ""),
            "timestamp": o.get("timestamp"),
            "cost": float(o.get("cost", 0) or 0),
            "average": float(o.get("average", 0) or 0),
        }

    def __repr__(self):
        return (f"CCXTExecutor({self.exchange_id}, account={self.account_id}, "
                f"connected={self.connected})")
