"""
JarvAIs Candle Collector Service
Periodically fetches and stores OHLCV candle data for signal symbols.

This service:
1. Gets active signal symbols from parsed_signals table
2. Fetches candles from configured data sources (MT5, Yahoo Finance)
3. Stores candles with smart deduplication (UNIQUE constraint)
4. Tracks coverage per symbol in candle_coverage table
5. Cleans up old candles beyond retention period

Usage:
    from services.candle_collector import CandleCollector, start_candle_collector
    
    collector = CandleCollector(db, config)
    collector.start()  # Starts background thread
    
    # Or manually:
    result = collector.fetch_and_store("XAUUSD")
"""

import os
import time
import json
import logging
import threading
from concurrent.futures import as_completed
from core.thread_pool import DaemonThreadPoolExecutor as ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass

logger = logging.getLogger("jarvais.candle_collector")


def _utcnow() -> datetime:
    """Naive-UTC now — no DeprecationWarning, compatible with MySQL datetimes."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class CandleConfig:
    """Configuration for candle collection."""
    timeframe: str = "M5"
    retention_days: int = 60
    fetch_interval_minutes: int = 15
    assume_tp1_is_win: bool = True
    stale_days: int = 30
    stale_forever_days: int = 60
    entry_tolerance_pips: int = 5


# ─────────────────────────────────────────────────────────────────────
# YAHOO FINANCE EXTENDED (60 days M5 data)
# ─────────────────────────────────────────────────────────────────────

# Yahoo Finance symbol mapping (forex, metals, indices, crypto)
YAHOO_SYMBOL_MAP = {
    "XAUUSD": "GC=F",      "XAGUSD": "SI=F",
    "EURUSD": "EURUSD=X",  "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",  "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",  "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X",  "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",  "GBPJPY": "GBPJPY=X",
    "NAS100": "QQQ",        "US100": "QQQ",
    "USTEC": "QQQ",         "US30": "YM=F",
    "SPX500": "ES=F",      "USOIL": "CL=F",
    "UKOIL": "BZ=F",       "GER30": "^GDAXI",
    "UK100": "^FTSE",      "XPTUSD": "PL=F",
    # Crypto — USD variants
    "BTCUSD": "BTC-USD",   "ETHUSD": "ETH-USD",
    "XRPUSD": "XRP-USD",   "LTCUSD": "LTC-USD",
    "SOLUSD": "SOL-USD",   "DOGEUSD": "DOGE-USD",
    "ADAUSD": "ADA-USD",   "AVAXUSD": "AVAX-USD",
    "BNBUSD": "BNB-USD",   "DOTUSD": "DOT-USD",
    "MATICUSD": "MATIC-USD",
    # Crypto — USDT variants (signals often use these)
    "BTCUSDT": "BTC-USD",  "ETHUSDT": "ETH-USD",
    "XRPUSDT": "XRP-USD",  "LTCUSDT": "LTC-USD",
    "SOLUSDT": "SOL-USD",  "DOGEUSDT": "DOGE-USD",
    "ADAUSDT": "ADA-USD",  "AVAXUSDT": "AVAX-USD",
    "BNBUSDT": "BNB-USD",  "DOTUSDT": "DOT-USD",
    "SUIUSDT": "SUI20947-USD", "LINKUSDT": "LINK-USD",
    "PEPEUSDT": "PEPE24478-USD", "AAVEUSDT": "AAVE-USD",
    # Additional crypto (mentor / signal provider coins)
    "VIRTUALUSDT": "VIRTUAL-USD", "HYPEUSDT": "HYPE-USD",
    "ZECUSD": "ZEC-USD", "ZECUSDT": "ZEC-USD",
    "SHIBUSDT": "SHIB-USD", "BONKUSDT": "BONK-USD",
    "WIFUSDT": "WIF-USD", "PENDLEUSDT": "PENDLE-USD",
    # Stocks (Yahoo uses same ticker; GOOGL→GOOG for Alphabet Class C)
    "GOOG": "GOOG", "GOOGL": "GOOG", "AAPL": "AAPL", "NVDA": "NVDA",
    "MSFT": "MSFT", "AMZN": "AMZN", "META": "META", "TSLA": "TSLA",
}

_exchange_markets_cache: Dict[str, Dict] = {}
_exchange_markets_ts: Dict[str, float] = {}
_MARKETS_TTL = 14400  # 4 hours; new listings/delistings picked up faster

def _get_exchange_markets(exchange_id: str) -> Dict:
    """Load and cache exchange market listings (refreshed every 4h).
    Falls back to Google DNS if default DNS can't reach exchange APIs."""
    now = time.time()
    if (exchange_id in _exchange_markets_cache
            and now - _exchange_markets_ts.get(exchange_id, 0) < _MARKETS_TTL):
        return _exchange_markets_cache[exchange_id]
    try:
        import ccxt
        ex_cls = getattr(ccxt, exchange_id, None)
        if not ex_cls:
            return _exchange_markets_cache.get(exchange_id, {})
        ex = ex_cls({"enableRateLimit": True})
        try:
            mkts = ex.load_markets()
        except Exception:
            from core.ccxt_executor import _use_google_dns
            with _use_google_dns():
                mkts = ex.load_markets()
            logger.debug(f"[CandleCollector] {exchange_id} needed Google DNS")
        _exchange_markets_cache[exchange_id] = mkts
        _exchange_markets_ts[exchange_id] = now
        logger.info(f"[CandleCollector] Loaded {len(mkts)} markets "
                    f"from {exchange_id}")
        return mkts
    except Exception as e:
        logger.debug(f"[CandleCollector] Failed to load {exchange_id} "
                     f"markets: {e}")
        return _exchange_markets_cache.get(exchange_id, {})


def _validate_on_exchange(candidate: str, exchange_id: str) -> Optional[str]:
    """Return the candidate symbol if it exists on the exchange, else None."""
    mkts = _get_exchange_markets(exchange_id)
    if not mkts:
        return candidate
    if candidate in mkts:
        return candidate
    return None


def _extract_base(symbol: str) -> str:
    """Strip common suffixes to extract the coin base name.
    JELLYJELLYUSDT.P -> JELLYJELLY, HIPPOUSDT -> HIPPO, etc."""
    import re
    s = symbol.upper()
    s = re.sub(r'[.\-_]?P(?:ERP)?$', '', s)
    for suffix in ("USDT", "USDC", "BUSD", "USD"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[:-len(suffix)]
            break
    return s


def _resolve_ccxt_symbol(db, symbol: str, exchange_id: str = "bybit") -> Optional[str]:
    """Resolve internal symbol to exchange-specific CCXT ticker.
    Chain: alias -> DB ticker -> smart resolution against live exchange markets.
    Caches successful resolutions back to DB for future lookups.
    Returns None for symbols that don't exist on the exchange."""
    from db.market_symbols import resolve_symbol as _resolve_alias
    resolved = _resolve_alias(symbol, db)
    _ticker_col_map = {"bybit": "bybit_ticker", "blofin": "blofin_ticker", "bitget": "bitget_ticker"}
    col = _ticker_col_map.get(exchange_id, f"{exchange_id}_ticker")
    for try_sym in ([resolved, symbol] if resolved != symbol else [symbol]):
        if db:
            try:
                row = db.fetch_one(
                    f"SELECT {col} FROM market_symbols WHERE symbol = %s",
                    (try_sym,))
                if row and row.get(col):
                    validated = _validate_on_exchange(row[col], exchange_id)
                    if validated:
                        return validated
            except Exception:
                pass

    # --- Smart resolution: extract base coin, try all CCXT patterns ---
    base = _extract_base(symbol)
    if not base or len(base) < 2:
        return None
    mkts = _get_exchange_markets(exchange_id)
    if not mkts:
        logger.debug(f"[CandleCollector] No market data for {exchange_id} — "
                     f"cannot validate '{symbol}', returning None")
        return None

    candidates = [
        f"{base}/USDT:USDT",
        f"1000{base}/USDT:USDT",
        f"10000{base}/USDT:USDT",
    ]
    for c in candidates:
        if c in mkts:
            _cache_resolved_ticker(db, symbol, c, exchange_id)
            return c

    import re
    base_clean = re.sub(r'[^A-Z0-9]', '', base)
    for mkt_key in mkts:
        if not mkt_key or '/USDT' not in mkt_key:
            continue
        mkt_base = mkt_key.split('/')[0]
        if mkt_base == base_clean:
            _cache_resolved_ticker(db, symbol, mkt_key, exchange_id)
            return mkt_key
        if base_clean in mkt_base and len(base_clean) >= 3:
            if mkt_base.startswith(base_clean) or mkt_base.endswith(base_clean):
                _cache_resolved_ticker(db, symbol, mkt_key, exchange_id)
                return mkt_key

    logger.debug(f"[CandleCollector] _resolve_ccxt_symbol: no match for "
                 f"'{symbol}' (base='{base}') on {exchange_id}")
    return None


def _cache_resolved_ticker(db, symbol: str, ticker: str, exchange_id: str) -> None:
    """Write a successfully resolved CCXT ticker back to market_symbols for future lookups."""
    if not db or not ticker:
        return
    _ticker_col_map = {"bybit": "bybit_ticker", "blofin": "blofin_ticker", "bitget": "bitget_ticker"}
    col = _ticker_col_map.get(exchange_id, f"{exchange_id}_ticker")
    s = symbol.upper().strip()
    from db.market_symbols import resolve_symbol as _ra
    canonical = _ra(s, db)
    for try_sym in ([canonical, s] if canonical != s else [s]):
        try:
            row = db.fetch_one(
                f"SELECT {col} FROM market_symbols WHERE symbol = %s", (try_sym,))
            if row is not None:
                if not row.get(col) or row[col] != ticker:
                    db.execute(
                        f"UPDATE market_symbols SET {col} = %s WHERE symbol = %s",
                        (ticker, try_sym))
                    logger.info(f"[CandleCollector] Cached {exchange_id} ticker: "
                                f"{try_sym} -> {ticker}")
                return
        except Exception:
            pass


def _is_crypto_symbol(db, symbol: str) -> bool:
    """Check if symbol is cryptocurrency via market_symbols.
    Returns True if asset_class is 'cryptocurrency' OR if the symbol has
    exchange tickers (bybit_ticker/blofin_ticker) populated — that's a
    definitive signal the symbol lives on a crypto exchange."""
    if not db:
        return False
    from db.market_symbols import resolve_symbol as _resolve_alias
    for try_sym in set([symbol.upper(), _resolve_alias(symbol, db)]):
        try:
            row = db.fetch_one(
                "SELECT asset_class, bybit_ticker, blofin_ticker, bitget_ticker "
                "FROM market_symbols WHERE symbol = %s",
                (try_sym,))
            if not row:
                continue
            if row.get("asset_class") == "cryptocurrency":
                return True
            if row.get("bybit_ticker") or row.get("blofin_ticker") or row.get("bitget_ticker"):
                return True
        except Exception:
            pass
    return False

_CRYPTO_BASES = {"BTC","ETH","XRP","SOL","DOGE","ADA","AVAX","BNB","DOT",
                  "MATIC","LTC","LINK","SUI","PEPE","AAVE","ARB","OP",
                  "APT","SEI","TIA","NEAR","FTM","ATOM","UNI","FIL",
                  "INJ","HBAR","WLD","RENDER","JUP","ENA","TRX","TON",
                  "VIRTUAL","HYPE","ZEC","SHIB","BONK","WIF","PENDLE",
                  "STX","MKR","CRV","COMP","SAND","MANA","AXS","GALA",
                  "IMX","LDO","RPL","SSV","FXS","GMX","DYDX","BLUR"}

def _looks_like_crypto(symbol: str) -> bool:
    """Heuristic: recognise common crypto tickers even if DB doesn't know them."""
    s = symbol.upper()
    if s.endswith("USDT") or s.endswith("USDC") or s.endswith("BUSD"):
        return True
    if s in _CRYPTO_BASES:
        return True
    for base in _CRYPTO_BASES:
        if s.startswith(base) and s.endswith("USD"):
            return True
    return False

TIMEFRAME_LOOKBACK_DEFAULTS = {
    "D1": 365, "H4": 45, "H1": 12, "M30": 2, "M15": 3, "M5": 1,
}


def fetch_yahoo_candles(symbol: str, timeframe: str = "M5", 
                         from_date: datetime = None, to_date: datetime = None,
                         days: int = 60) -> List[Dict]:
    """
    Fetch candles from Yahoo Finance API.
    Extended to support 60 days of M5 data.
    
    Args:
        symbol: Trading symbol (e.g., "XAUUSD")
        timeframe: Timeframe string (M1, M5, M15, H1, D1)
        from_date: Start date
        to_date: End date
        days: Number of days to fetch (default 60)
        
    Returns:
        List of candle dicts with: time, open, high, low, close, volume
    """
    import requests
    
    # Map symbol to Yahoo ticker (hardcoded map, caller can also pass db_yahoo_ticker)
    yahoo_symbol = YAHOO_SYMBOL_MAP.get(symbol.upper(), symbol)
    
    # Map timeframe to Yahoo interval
    tf_to_interval = {
        "M1": "1m",
        "M5": "5m", 
        "M15": "15m",
        "M30": "30m",
        "H1": "1h",
        "H4": "1h",  # Yahoo doesn't have 4h, use 1h
        "D1": "1d",
    }
    interval = tf_to_interval.get(timeframe, "5m")
    
    # Calculate date range
    if to_date is None:
        to_date = _utcnow()
    if from_date is None:
        from_date = to_date - timedelta(days=days)
    
    # Yahoo API limits:
    # - 1m, 5m, 15m, 30m: max 60 days
    # - 1h: max 730 days
    # - 1d: max many years
    
    # Convert to timestamps
    period1 = int(from_date.timestamp())
    period2 = int(to_date.timestamp())
    
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
    params = {
        "interval": interval,
        "period1": period1,
        "period2": period2,
        "includePrePost": "false"
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code == 422:
            range_map = {"1m": "7d", "5m": "59d", "15m": "59d",
                         "30m": "59d", "1h": "60d", "1d": "1y"}
            fallback_params = {
                "interval": interval,
                "range": range_map.get(interval, "59d"),
                "includePrePost": "false",
            }
            import time
            time.sleep(1)
            resp = requests.get(url, params=fallback_params,
                                headers=headers, timeout=30)
        if resp.status_code != 200:
            logger.warning(f"[YahooCandles] {yahoo_symbol}: HTTP {resp.status_code}")
            return []
        
        data = resp.json()
        chart = data.get("chart", {}).get("result", [])
        if not chart:
            logger.warning(f"[YahooCandles] {symbol}: No chart data")
            return []
        
        result = chart[0]
        timestamps = result.get("timestamp", [])
        indicators = result.get("indicators", {})
        quote = indicators.get("quote", [{}])[0]
        
        candles = []
        for i, ts in enumerate(timestamps):
            try:
                candle_time = datetime.utcfromtimestamp(ts)
                candle = {
                    "time": candle_time,
                    "open": float(quote.get("open", [0])[i] or 0),
                    "high": float(quote.get("high", [0])[i] or 0),
                    "low": float(quote.get("low", [0])[i] or 0),
                    "close": float(quote.get("close", [0])[i] or 0),
                    "volume": int(quote.get("volume", [0])[i] or 0),
                }
                # Skip invalid candles (including Yahoo close == 0)
                if (candle["open"] > 0 and candle["high"] > 0 and candle["low"] > 0
                        and candle.get("close", 0) > 0):
                    candles.append(candle)
            except (IndexError, TypeError, ValueError):
                continue
        
        logger.info(f"[YahooCandles] Fetched {len(candles)} candles for {symbol} {timeframe}")
        return candles
        
    except Exception as e:
        logger.error(f"[YahooCandles] Error fetching {symbol}: {e}")
        return []


# ─────────────────────────────────────────────────────────────────────
# CANDLE COLLECTOR CLASS
# ─────────────────────────────────────────────────────────────────────

class CandleCollector:
    """
    Manages candle data collection and storage for signal symbols.
    """
    
    def __init__(self, db, config):
        self.db = db
        self.config = config
        
        # Load configuration from system_config
        self._load_config()
        
        # State
        self._running = False
        self._thread = None
        self._stop_event = threading.Event()
        self._last_run = None
        self._symbols_fetched = 0
        self._candles_stored = 0
        self._last_chart_cleanup = 0.0
        
    def _load_config(self):
        """Load configuration from system_config table."""
        try:
            rows = self.db.fetch_all("""
                SELECT config_key, config_value FROM system_config 
                WHERE config_key IN ('backtest_timeframe', 'candle_retention_days', 
                    'candle_fetch_interval_minutes', 'backtest_stale_days', 
                    'backtest_stale_forever_days', 'backtest_assume_tp1_is_win')
            """)
            config_dict = {r['config_key']: r['config_value'] for r in rows} if rows else {}
            
            self.cfg = CandleConfig(
                timeframe=config_dict.get('backtest_timeframe', 'M5'),
                retention_days=int(config_dict.get('candle_retention_days', 60)),
                fetch_interval_minutes=int(config_dict.get('candle_fetch_interval_minutes', 15)),
                stale_days=int(config_dict.get('backtest_stale_days', 30)),
                stale_forever_days=int(config_dict.get('backtest_stale_forever_days', 60)),
                assume_tp1_is_win=config_dict.get('backtest_assume_tp1_is_win', '1') == '1'
            )
        except Exception as e:
            logger.warning(f"[CandleCollector] Could not load config: {e}")
            self.cfg = CandleConfig()
    
    # ── Background Service ─────────────────────────────────────────
    
    def start(self):
        """Start the background candle collection thread."""
        if self._running:
            logger.warning("[CandleCollector] Already running")
            return
            
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info(f"[CandleCollector] Started (interval={self.cfg.fetch_interval_minutes}m)")
    
    def stop(self):
        """Stop the background thread."""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[CandleCollector] Stopped")
    
    def _run_loop(self):
        """Main background loop."""
        while self._running and not self._stop_event.is_set():
            try:
                self._collect_cycle()
            except Exception as e:
                logger.error(f"[CandleCollector] Cycle error: {e}", exc_info=True)

            # Generated-chart cleanup (at most once per hour)
            if time.time() - self._last_chart_cleanup >= 3600:
                try:
                    self._cleanup_generated_charts()
                except Exception as e:
                    logger.debug(f"[CandleCollector] Chart cleanup error: {e}")
                self._last_chart_cleanup = time.time()

            # Wait for next interval (minimum 60 seconds)
            wait_secs = max(60, self.cfg.fetch_interval_minutes * 60)
            self._stop_event.wait(wait_secs)
    
    def _collect_cycle(self):
        """Run one collection cycle."""
        logger.info("[CandleCollector] Starting collection cycle")
        self._last_run = _utcnow()
        
        # Get symbols that need candles
        symbols = self._get_active_symbols()
        if not symbols:
            logger.info("[CandleCollector] No active symbols to fetch")
            return
        
        logger.info(f"[CandleCollector] Fetching candles for {len(symbols)} symbols")
        
        fetched = 0
        stored = 0

        # Phase 2b: parallel symbol fetches (8 workers)
        def _fetch_one(sym):
            if self._stop_event.is_set():
                return None
            try:
                return self.fetch_and_store(sym)
            except Exception as e:
                logger.error(f"[CandleCollector] Error fetching {sym}: {e}")
                return None

        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="candle") as pool:
            futures = {pool.submit(_fetch_one, s): s for s in symbols}
            for fut in as_completed(futures):
                res = fut.result()
                if res:
                    fetched += 1
                    stored += res.get("stored", 0)

        self._symbols_fetched += fetched
        self._candles_stored += stored

        self._cleanup_old_candles()

        logger.info(f"[CandleCollector] Cycle complete: {fetched} symbols, {stored} candles stored")
    
    # ── Symbol Management ───────────────────────────────────────────
    
    def _get_active_symbols(self) -> List[str]:
        """Get all symbols needing candle data.

        Sources:
        1. Trading floor watchlist (ALWAYS collected, even without signals)
        2. Symbols with active dossiers
        3. Symbols with pending/active signals
        4. All mentor-mentioned symbols (when mentor_mode != off)
        """
        symbols = set()

        # 1. Scout watchlist
        td_cfg = {}
        if self.config:
            td_cfg = self.config.raw.get("trade_decision", {})
            watchlist = td_cfg.get("scout", {}).get("watchlist", [])
            symbols.update(watchlist)

        # 2. Symbols with active dossiers (including executed for post-mortem)
        try:
            dossier_rows = self.db.fetch_all("""
                SELECT DISTINCT symbol FROM trade_dossiers
                WHERE status IN ('proposed', 'monitoring', 'open_order', 'live')
            """)
            symbols.update(r["symbol"] for r in (dossier_rows or []))
        except Exception:
            pass

        # 3. Symbols with active signals
        try:
            rows = self.db.fetch_all("""
                SELECT DISTINCT symbol
                FROM parsed_signals
                WHERE status IN ('pending', 'active', 'entry_hit', 'tp1_hit', 'tp2_hit')
                AND parsed_at > DATE_SUB(NOW(), INTERVAL %s DAY)
            """, (self.cfg.stale_forever_days,))
            symbols.update(r['symbol'] for r in (rows or []))
        except Exception as e:
            logger.error(f"[CandleCollector] Error getting signal symbols: {e}")

        # 4. Mentor Mirror Mode: collect candles for ALL mentor-mentioned symbols
        mentor_cfg = td_cfg.get("mentor", {})
        if mentor_cfg.get("mentor_mode", "off") != "off":
            try:
                mentor_symbols = self._get_mentor_symbols()
                if mentor_symbols:
                    symbols.update(mentor_symbols)
                    logger.debug(f"[CandleCollector] Mentor Mirror: added "
                                f"{len(mentor_symbols)} mentor symbols")
            except Exception as e:
                logger.error(f"[CandleCollector] Error getting mentor symbols: {e}")

        return list(symbols)

    def _get_mentor_symbols(self) -> List[str]:
        """Get all unique symbols from recent mentor signals (past 7 days)."""
        rows = self.db.fetch_all("""
            SELECT DISTINCT ps.symbol
            FROM parsed_signals ps
            JOIN user_profile_links upl
              ON upl.source_username = ps.author AND upl.source = ps.source
            JOIN user_profiles up
              ON up.id = upl.user_profile_id AND up.is_mentor = 1
            WHERE ps.parsed_at > DATE_SUB(NOW(), INTERVAL 7 DAY)
              AND ps.symbol IS NOT NULL AND ps.symbol != ''
        """)
        return [r["symbol"] for r in (rows or [])]
    
    def _get_all_signal_symbols(self) -> List[str]:
        """Get all unique symbols from parsed_signals."""
        try:
            rows = self.db.fetch_all("SELECT DISTINCT symbol FROM parsed_signals")
            return [r['symbol'] for r in rows] if rows else []
        except:
            return []
    
    # ── Data Source Selection ───────────────────────────────────────
    
    def _get_candles_from_mt5(self, symbol: str) -> List[Dict]:
        """Fetch candles from MT5."""
        try:
            from core.mt5_manager import get_mt5_manager
            manager = get_mt5_manager()
            
            if not manager.is_connected():
                # Try to load config from system_config first
                config_rows = self.db.fetch_all("SELECT config_key, config_value FROM system_config WHERE config_key LIKE 'mt5_%'")
                config_dict = {r['config_key']: r['config_value'] for r in config_rows} if config_rows else {}
                
                # If not in system_config, use first account from config.json (ConfigManager.accounts)
                if not config_dict.get('mt5_path') and self.config:
                    try:
                        accounts_list = self.config.get_enabled_accounts() or list(self.config.accounts.values())
                        if accounts_list:
                            acc = accounts_list[0]
                            config_dict = {
                                'mt5_path': getattr(acc, 'mt5_path', '') or '',
                                'mt5_login': str(getattr(acc, 'mt5_login', '') or ''),
                                'mt5_password': getattr(acc, 'mt5_password', '') or '',
                                'mt5_server': getattr(acc, 'mt5_server', '') or '',
                                'mt5_timeout': str(getattr(acc, 'mt5_timeout', 15) or 15)
                            }
                            logger.info(f"[CandleCollector] Loaded MT5 config from config.json: path={config_dict.get('mt5_path')}, login={config_dict.get('mt5_login')}")
                    except Exception as e:
                        logger.warning(f"[CandleCollector] Could not load MT5 config from config.json: {e}")
                
                manager.load_from_config(config_dict)
                ok, msg = manager.connect()
                if not ok:
                    logger.warning(f"[CandleCollector] MT5 not connected: {msg}")
                    return []
            
            mt5_sym = self._resolve_mt5_symbol(symbol)
            candles = manager.get_candles(mt5_sym, self.cfg.timeframe, 5000)
            if not candles and mt5_sym != symbol:
                candles = manager.get_candles(symbol, self.cfg.timeframe, 5000)
            return [
                {"time": c.time, "open": c.open, "high": c.high, 
                 "low": c.low, "close": c.close, "volume": c.volume}
                for c in candles
            ]
        except Exception as e:
            logger.warning(f"[CandleCollector] MT5 fetch failed for {symbol}: {e}")
            return []
    
    def _resolve_yahoo_symbol(self, symbol: str) -> str:
        """Resolve internal symbol to Yahoo ticker via alias map, market_symbols
        table, then YAHOO_SYMBOL_MAP. Handles GOLD→XAUUSD→GC=F chains."""
        from db.market_symbols import resolve_symbol as _resolve_alias
        s = symbol.upper()
        canonical = _resolve_alias(s, self.db)

        for try_sym in ([canonical, s] if canonical != s else [s]):
            if self.db:
                try:
                    row = self.db.fetch_one(
                        "SELECT yahoo_ticker FROM market_symbols WHERE symbol = %s",
                        (try_sym,))
                    if row and row.get("yahoo_ticker"):
                        return row["yahoo_ticker"]
                except Exception:
                    pass
            if try_sym in YAHOO_SYMBOL_MAP:
                return YAHOO_SYMBOL_MAP[try_sym]

        base = canonical if canonical != s else s
        if base in _CRYPTO_BASES:
            return f"{base}-USD"
        if base.endswith("USDT") and base not in YAHOO_SYMBOL_MAP:
            return f"{base[:-4]}-USD"
        return symbol

    def _get_candles_from_yahoo(self, symbol: str) -> List[Dict]:
        """Fetch candles from Yahoo Finance with DB symbol resolution."""
        yahoo_sym = self._resolve_yahoo_symbol(symbol)
        return fetch_yahoo_candles(yahoo_sym, self.cfg.timeframe, days=self.cfg.retention_days)
    
    def _get_candles(self, symbol: str) -> Tuple[List[Dict], str]:
        """
        Get candles from best available source.
        For crypto: CCXT (Bybit) first, then Yahoo.
        For everything else: MT5 first, then Yahoo, then CCXT.
        """
        is_crypto = _is_crypto_symbol(self.db, symbol) or _looks_like_crypto(symbol)

        if is_crypto:
            candles = self._fetch_ccxt_candles(symbol, self.cfg.timeframe,
                                               TIMEFRAME_LOOKBACK_DEFAULTS.get(self.cfg.timeframe, 1))
            if candles:
                return candles, "ccxt"
            candles = self._get_candles_from_yahoo(symbol)
            if candles:
                return candles, "yahoo"
        else:
            candles = self._get_candles_from_mt5(symbol)
            if candles:
                return candles, "mt5"
            candles = self._get_candles_from_yahoo(symbol)
            if candles:
                return candles, "yahoo"
            candles = self._fetch_ccxt_candles(symbol, self.cfg.timeframe,
                                               TIMEFRAME_LOOKBACK_DEFAULTS.get(self.cfg.timeframe, 1))
            if candles:
                return candles, "ccxt"

        return [], "none"
    
    # ── Storage ─────────────────────────────────────────────────────
    
    def fetch_and_store(self, symbol: str) -> Dict[str, Any]:
        """
        Fetch and store candles for a symbol.
        Resolves aliases (GOLD→XAUUSD) and stores candles under both names.
        """
        from db.market_symbols import resolve_symbol as _resolve_alias
        canonical = _resolve_alias(symbol, self.db)
        fetch_sym = canonical if canonical != symbol else symbol

        result = {
            "symbol": symbol,
            "fetched": 0,
            "stored": 0,
            "source": "none",
            "error": None
        }
        
        try:
            coverage = self._get_coverage(fetch_sym)
            from_date = coverage.get("latest_candle") if coverage else None
            
            candles, source = self._get_candles(fetch_sym)
            result["source"] = source
            
            if not candles:
                result["error"] = f"No candles from {source}"
                return result
            
            result["fetched"] = len(candles)
            
            stored = self._store_candles(fetch_sym, candles, source)
            if fetch_sym != symbol:
                self._store_candles(symbol, candles, source)
            result["stored"] = stored
            
            self._update_coverage(fetch_sym, candles, source)
            if fetch_sym != symbol:
                self._update_coverage(symbol, candles, source)
            
            log_sym = f"{symbol}->{fetch_sym}" if fetch_sym != symbol else symbol
            logger.info(f"[CandleCollector] {log_sym}: fetched={len(candles)}, stored={stored}, source={source}")
            
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"[CandleCollector] fetch_and_store error for {symbol}: {e}")
        
        return result
    
    def _store_candles(self, symbol: str, candles: List[Dict], source: str) -> int:
        """
        Store candles in database. Uses batch INSERT IGNORE for deduplication.
        Returns count of new candles stored.
        """
        if not candles:
            return 0

        tf = self.cfg.timeframe
        return self._batch_insert_candles(symbol, tf, candles, source)

    def _batch_insert_candles(self, symbol: str, tf: str,
                              candles: List[Dict], source: str,
                              batch_size: int = 200) -> int:
        """Batch INSERT IGNORE candles in chunks to reduce DB round-trips."""
        stored = 0
        for i in range(0, len(candles), batch_size):
            batch = candles[i:i + batch_size]
            placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s, %s)"] * len(batch))
            params = []
            for c in batch:
                params.extend([
                    symbol, tf, c["time"], c["open"], c["high"],
                    c["low"], c["close"], c.get("volume", 0), source
                ])
            try:
                affected = self.db.execute(
                    f"INSERT IGNORE INTO candles "
                    f"(symbol, timeframe, candle_time, open, high, low, close, volume, source) "
                    f"VALUES {placeholders}",
                    tuple(params))
                stored += affected if affected else 0
            except Exception as e:
                logger.debug(f"[CandleCollector] Batch insert error ({len(batch)} rows): {e}")
        return stored
    
    def _get_coverage(self, symbol: str) -> Optional[Dict]:
        """Get coverage info for a symbol."""
        try:
            row = self.db.fetch_one(
                "SELECT * FROM candle_coverage WHERE symbol = %s AND timeframe = %s",
                (symbol, self.cfg.timeframe)
            )
            return row
        except:
            return None
    
    def _update_coverage(self, symbol: str, candles: List[Dict], source: str):
        """Update coverage table after fetching."""
        if not candles:
            return
        
        # Get min/max times and count from DB
        try:
            stats = self.db.fetch_one("""
                SELECT 
                    MIN(candle_time) as earliest,
                    MAX(candle_time) as latest,
                    COUNT(*) as cnt
                FROM candles 
                WHERE symbol = %s AND timeframe = %s
            """, (symbol, self.cfg.timeframe))
            
            if stats:
                self.db.execute("""
                    INSERT INTO candle_coverage 
                    (symbol, timeframe, earliest_candle, latest_candle, candle_count, last_fetched_at, fetch_source)
                    VALUES (%s, %s, %s, %s, %s, NOW(), %s)
                    ON DUPLICATE KEY UPDATE 
                        earliest_candle = %s,
                        latest_candle = %s,
                        candle_count = %s,
                        last_fetched_at = NOW(),
                        fetch_source = %s
                """, (
                    symbol, self.cfg.timeframe, 
                    stats['earliest'], stats['latest'], stats['cnt'], source,
                    stats['earliest'], stats['latest'], stats['cnt'], source
                ))
        except Exception as e:
            logger.warning(f"[CandleCollector] Could not update coverage for {symbol}: {e}")
    
    def _cleanup_old_candles(self):
        """Delete candles older than per-timeframe retention periods.
        Falls back to config.candle_retention_days or global retention_days."""
        try:
            from core.config import get_config
            cfg = get_config()
            td = cfg.raw.get("trade_decision", {}) if cfg else {}
            per_tf = td.get("candle_retention_days",
                            {"M1": 1, "M5": 3, "M15": 7,
                             "H1": 30, "H4": 90, "D1": 365})
            fallback = self.cfg.retention_days

            total_deleted = 0
            for tf, days in per_tf.items():
                cutoff = _utcnow() - timedelta(days=days)
                deleted = self.db.execute(
                    "DELETE FROM candles WHERE timeframe = %s AND candle_time < %s",
                    (tf, cutoff))
                if deleted:
                    total_deleted += deleted

            remaining_cutoff = _utcnow() - timedelta(days=fallback)
            if per_tf:
                placeholders = ",".join(["%s"] * len(per_tf))
                leftover = self.db.execute(
                    f"DELETE FROM candles WHERE candle_time < %s "
                    f"AND timeframe NOT IN ({placeholders})",
                    (remaining_cutoff, *per_tf.keys()))
                total_deleted += (leftover or 0)

            if total_deleted > 0:
                logger.info(f"[CandleCollector] Cleaned up {total_deleted} old candles")
        except Exception as e:
            logger.warning(f"[CandleCollector] Cleanup error: {e}")

    def _cleanup_generated_charts(self):
        """Remove generated charts older than 1 day to save disk space."""
        import glob
        chart_dir = os.path.join("data", "generated_charts")
        if not os.path.isdir(chart_dir):
            return
        cutoff = _utcnow() - timedelta(days=1)
        removed = 0
        for f in glob.glob(os.path.join(chart_dir, "*.png")):
            try:
                mtime = datetime.utcfromtimestamp(os.path.getmtime(f))
                if mtime < cutoff:
                    os.remove(f)
                    removed += 1
            except Exception:
                pass
        if removed:
            logger.info(f"[CandleCollector] Cleaned up {removed} generated charts older than 1 day")

    # ── Status ───────────────────────────────────────────────────────
    
    def get_status(self) -> Dict[str, Any]:
        """Get collector status for UI."""
        return {
            "running": self._running,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "config": {
                "timeframe": self.cfg.timeframe,
                "retention_days": self.cfg.retention_days,
                "fetch_interval_minutes": self.cfg.fetch_interval_minutes,
            },
            "stats": {
                "symbols_fetched": self._symbols_fetched,
                "candles_stored": self._candles_stored,
            }
        }
    
    # ── Candle Retrieval for Backtesting ─────────────────────────────
    
    def get_candles_for_signal(self, symbol: str, from_date: datetime, 
                                to_date: datetime = None) -> List[Dict]:
        """
        Get stored candles for backtesting a signal.
        
        Args:
            symbol: Trading symbol
            from_date: Start date (usually signal.parsed_at)
            to_date: End date (default: now)
            
        Returns:
            List of candle dicts ordered by time
        """
        if to_date is None:
            to_date = _utcnow()
        
        try:
            rows = self.db.fetch_all("""
                SELECT candle_time as time, open, high, low, close, volume, source
                FROM candles
                WHERE symbol = %s 
                AND timeframe = %s
                AND candle_time >= %s
                AND candle_time <= %s
                ORDER BY candle_time ASC
            """, (symbol, self.cfg.timeframe, from_date, to_date))
            
            candles = []
            for r in rows:
                candles.append({
                    "time": r['time'],
                    "open": float(r['open']),
                    "high": float(r['high']),
                    "low": float(r['low']),
                    "close": float(r['close']),
                    "volume": int(r['volume'] or 0),
                    "source": r['source']
                })
            
            return candles
            
        except Exception as e:
            logger.error(f"[CandleCollector] Error getting candles for {symbol}: {e}")
            return []
    
    def get_latest_price(self, symbol: str) -> Optional[float]:
        """Get the latest close price for a symbol."""
        try:
            row = self.db.fetch_one("""
                SELECT close FROM candles 
                WHERE symbol = %s AND timeframe = %s
                ORDER BY candle_time DESC LIMIT 1
            """, (symbol, self.cfg.timeframe))
            return float(row['close']) if row else None
        except:
            return None

    # ── Multi-Timeframe Collection for Dossier ───────────────────────

    def fetch_multi_timeframe(self, symbol: str,
                              timeframes: Dict[str, int] = None) -> Dict[str, List[Dict]]:
        """
        Fetch and store candles across multiple timeframes for a symbol.
        Used by the dossier builder for comprehensive TA.

        Args:
            symbol: Trading symbol
            timeframes: Dict mapping timeframe -> lookback_days, e.g. {"D1": 90, "H1": 7}
                        If None, uses config trade_decision.ohlcv_timeframes

        Returns:
            Dict mapping timeframe -> list of candle dicts
        """
        if timeframes is None:
            td_cfg = self.config.raw.get("trade_decision", {}) if self.config else {}
            timeframes = td_cfg.get("ohlcv_timeframes", TIMEFRAME_LOOKBACK_DEFAULTS)

        # Phase 2c: parallelize multi-timeframe fetch (5 workers, one per TF)
        all_candles = {}
        source = self._detect_source(symbol)

        def _fetch_one_tf(tf, lookback_days):
            logger.info(f"[CandleCollector] Fetching {symbol} {tf} ({lookback_days}d lookback)")
            candles = self._fetch_tf_candles(symbol, tf, lookback_days)
            if candles:
                self._store_candles_tf(symbol, tf, candles, source)
                logger.info(f"[CandleCollector] {symbol} {tf}: {len(candles)} candles")
                return tf, candles
            existing = self._get_stored_candles(symbol, tf, lookback_days)
            logger.info(f"[CandleCollector] {symbol} {tf}: {len(existing)} candles (from DB)")
            return tf, existing

        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="mtf") as pool:
            futures = {pool.submit(_fetch_one_tf, tf, days): tf
                       for tf, days in timeframes.items()}
            for fut in as_completed(futures):
                try:
                    tf, candles = fut.result()
                    all_candles[tf] = candles
                except Exception as e:
                    tf_name = futures[fut]
                    logger.error(f"[CandleCollector] MTF {symbol} {tf_name}: {e}")
                    all_candles[tf_name] = []

        return all_candles

    def _fetch_tf_candles(self, symbol: str, tf: str,
                          lookback_days: int) -> List[Dict]:
        """Fetch candles for a specific timeframe using provider priority.
        Crypto symbols use CCXT first; forex/indices use configured priority."""
        priority = ["mt5", "yahoo", "ccxt"]
        if self.config:
            td_cfg = self.config.raw.get("trade_decision", {})
            priority = td_cfg.get("candle_provider_priority", priority)

        if _is_crypto_symbol(self.db, symbol) or _looks_like_crypto(symbol):
            priority = ["ccxt", "yahoo", "mt5"]

        for provider in priority:
            try:
                if provider == "mt5":
                    candles = self._fetch_mt5_tf(symbol, tf, lookback_days)
                elif provider == "yahoo":
                    yahoo_sym = self._resolve_yahoo_symbol(symbol)
                    candles = fetch_yahoo_candles(yahoo_sym, tf, days=lookback_days)
                elif provider == "ccxt":
                    candles = self._fetch_ccxt_candles(symbol, tf, lookback_days)
                else:
                    continue
                if candles:
                    return candles
            except Exception as e:
                logger.debug(f"[CandleCollector] {provider} failed for {symbol} {tf}: {e}")
        return []

    def _resolve_mt5_symbol(self, symbol: str) -> str:
        """Resolve internal symbol to MT5 broker-specific symbol via market_symbols table."""
        if self.db:
            try:
                row = self.db.fetch_one(
                    "SELECT mt5_symbol FROM market_symbols WHERE symbol = %s",
                    (symbol,))
                if row and row.get("mt5_symbol"):
                    return row["mt5_symbol"]
            except Exception:
                pass
        return symbol

    def _fetch_mt5_tf(self, symbol: str, tf: str,
                      lookback_days: int) -> List[Dict]:
        """Fetch candles from MT5 for a specific timeframe."""
        try:
            from core.mt5_manager import get_mt5_manager
            manager = get_mt5_manager()
            if not manager.is_connected():
                return []
            mt5_sym = self._resolve_mt5_symbol(symbol)
            candles = manager.get_candles(mt5_sym, tf, 5000)
            if not candles and mt5_sym != symbol:
                candles = manager.get_candles(symbol, tf, 5000)
            return [
                {"time": c.time, "open": c.open, "high": c.high,
                 "low": c.low, "close": c.close, "volume": c.volume}
                for c in candles
            ]
        except Exception:
            return []

    def _fetch_ccxt_candles(self, symbol: str, tf: str,
                            lookback_days: int) -> List[Dict]:
        """
        Fetch candles via CCXT. Reads exchange config from config.json "ccxt" section.
        Tries preferred exchange (bybit) first, then fallback (blofin).
        Public OHLCV data does not require API keys.
        """
        try:
            import ccxt
        except ImportError:
            logger.debug("[CandleCollector] ccxt not installed")
            return []

        tf_map = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m",
                  "H1": "1h", "H4": "4h", "D1": "1d"}
        ccxt_tf = tf_map.get(tf)
        if not ccxt_tf:
            return []

        ccxt_cfg = {}
        if self.config:
            ccxt_cfg = self.config.raw.get("ccxt", {})
        preferred = ccxt_cfg.get("preferred_exchange", "bybit")
        fallback = ccxt_cfg.get("fallback_exchange", "blofin")
        exchanges = [preferred]
        if fallback != preferred:
            exchanges.append(fallback)

        since_ms = int((_utcnow() - timedelta(days=lookback_days)).timestamp() * 1000)

        for exchange_id in exchanges:
            ccxt_symbol = _resolve_ccxt_symbol(self.db, symbol, exchange_id)
            if not ccxt_symbol:
                logger.debug(f"[CandleCollector] No CCXT ticker for {symbol} on {exchange_id}")
                continue

            exchange_class = getattr(ccxt, exchange_id, None)
            if not exchange_class:
                logger.debug(f"[CandleCollector] Unknown exchange: {exchange_id}")
                continue

            try:
                exchange = exchange_class({"enableRateLimit": True})
                all_ohlcv = []
                while True:
                    batch = exchange.fetch_ohlcv(ccxt_symbol, ccxt_tf, since=since_ms, limit=1000)
                    if not batch:
                        break
                    all_ohlcv.extend(batch)
                    if len(batch) < 1000:
                        break
                    since_ms = batch[-1][0] + 1

                candles = [
                    {"time": datetime.utcfromtimestamp(row[0] / 1000),
                     "open": float(row[1]), "high": float(row[2]),
                     "low": float(row[3]), "close": float(row[4]),
                     "volume": int(row[5] or 0)}
                    for row in all_ohlcv
                ]
                if candles:
                    logger.info(f"[CandleCollector] CCXT {exchange_id}: {symbol} {tf} -> {len(candles)} candles")
                    return candles
            except Exception as e:
                logger.debug(f"[CandleCollector] CCXT {exchange_id} failed for {symbol}: {e}")

        return []

    def _detect_source(self, symbol: str) -> str:
        """Detect best source name for a symbol."""
        if _is_crypto_symbol(self.db, symbol) or _looks_like_crypto(symbol):
            return "ccxt"
        return "yahoo"

    def _store_candles_tf(self, symbol: str, tf: str,
                          candles: List[Dict], source: str) -> int:
        """Store candles for a specific timeframe using batch INSERT."""
        if not candles:
            return 0
        return self._batch_insert_candles(symbol, tf, candles, source)

    def _get_stored_candles(self, symbol: str, tf: str,
                            lookback_days: int) -> List[Dict]:
        """Retrieve already-stored candles from DB for a timeframe."""
        cutoff = _utcnow() - timedelta(days=lookback_days)
        try:
            rows = self.db.fetch_all("""
                SELECT candle_time as time, open, high, low, close, volume
                FROM candles
                WHERE symbol = %s AND timeframe = %s AND candle_time >= %s
                ORDER BY candle_time ASC
            """, (symbol, tf, cutoff))
            return [
                {"time": r["time"], "open": float(r["open"]),
                 "high": float(r["high"]), "low": float(r["low"]),
                 "close": float(r["close"]), "volume": int(r["volume"] or 0)}
                for r in (rows or [])
            ]
        except Exception:
            return []

    def get_ohlcv_for_dossier(self, symbol: str,
                               timeframes: Dict[str, int] = None) -> Dict[str, Any]:
        """
        Get OHLCV data formatted for dossier consumption.
        Returns structured data with summary stats per timeframe.
        """
        raw = self.fetch_multi_timeframe(symbol, timeframes)
        result = {}
        for tf, candles in raw.items():
            if not candles:
                result[tf] = {"count": 0, "candles": []}
                continue

            closes = [c["close"] for c in candles]
            highs = [c["high"] for c in candles]
            lows = [c["low"] for c in candles]
            result[tf] = {
                "count": len(candles),
                "latest_close": closes[-1] if closes else None,
                "high_of_range": max(highs) if highs else None,
                "low_of_range": min(lows) if lows else None,
                "first_time": candles[0]["time"].isoformat() if candles else None,
                "last_time": candles[-1]["time"].isoformat() if candles else None,
                "candles": candles,
            }
        return result


# ─────────────────────────────────────────────────────────────────────
# Exchange Reconnaissance (CCXT market discovery)
# ─────────────────────────────────────────────────────────────────────

def run_exchange_recon(db, config=None) -> Dict[str, Any]:
    """
    Connect to Bybit, Blofin, and Bitget via CCXT, discover all available
    markets, and reconcile them with the market_symbols table.

    Updates bybit_ticker, blofin_ticker, bitget_ticker, and
    preferred_exchange for every crypto symbol that can be matched to an
    exchange market.

    Returns a report dict with match stats.
    """
    try:
        import ccxt
    except ImportError:
        return {"error": "ccxt not installed", "matched": 0}

    _ALL_EXCHANGES = ("bybit", "blofin", "bitget")
    report = {
        "symbols_checked": 0, "matched": 0, "unmatched": 0,
        "updated": 0, "details": [],
    }
    for eid in _ALL_EXCHANGES:
        report[f"{eid}_markets"] = 0
        report[f"{eid}_only"] = 0
    report["multi_exchange"] = 0

    exchange_defs = {
        "bybit": {
            "apiKey": os.environ.get("BYBIT_API_KEY", ""),
            "secret": os.environ.get("BYBIT_SECRET", ""),
            "options": {"defaultType": "swap"},
        },
        "blofin": {
            "apiKey": os.environ.get("BLOFIN_API_KEY", ""),
            "secret": os.environ.get("BLOFIN_API_SECRET", ""),
            "options": {"defaultType": "swap"},
        },
        "bitget": {
            "apiKey": os.environ.get("BITGET_API_KEY", ""),
            "secret": os.environ.get("BITGET_API_SECRET", ""),
            "password": os.environ.get("BITGET_API_PHASE", ""),
            "options": {"defaultType": "swap", "defaultSubType": "linear"},
        },
    }

    exchanges = {}
    for eid, opts in exchange_defs.items():
        cls = getattr(ccxt, eid, None)
        if not cls:
            logger.warning(f"[Recon] Exchange class '{eid}' not found in ccxt")
            continue
        try:
            ex = cls({"enableRateLimit": True, "timeout": 15000, **opts})
            ex.load_markets()
            exchanges[eid] = ex
            logger.info(f"[Recon] {eid}: loaded {len(ex.markets)} markets")
        except Exception as e:
            logger.error(f"[Recon] Failed to load markets for {eid}: {e}")

    if not exchanges:
        return {**report, "error": "No exchanges could be loaded"}

    for eid in _ALL_EXCHANGES:
        if eid in exchanges:
            report[f"{eid}_markets"] = len(exchanges[eid].markets or {})

    def _build_lookup(ex) -> Dict[str, str]:
        """Map normalised base coin (e.g. 'BTC') to the best CCXT symbol on this exchange."""
        lookup = {}
        for ccxt_sym, mkt in ex.markets.items():
            if not mkt.get("active", True):
                continue
            base = (mkt.get("base") or "").upper()
            quote = (mkt.get("quote") or "").upper()
            mtype = mkt.get("type", "")
            if quote != "USDT":
                continue
            key = base
            if key not in lookup or mtype == "swap":
                lookup[key] = ccxt_sym
        return lookup

    lookups = {eid: _build_lookup(ex) for eid, ex in exchanges.items()}

    crypto_syms = db.fetch_all(
        "SELECT symbol, bybit_ticker, blofin_ticker, bitget_ticker, preferred_exchange "
        "FROM market_symbols WHERE asset_class = 'cryptocurrency'")
    if not crypto_syms:
        crypto_syms = []

    report["symbols_checked"] = len(crypto_syms)

    for row in crypto_syms:
        sym = row["symbol"].upper()
        base = sym
        for suffix in ("USDT", "USD", "USDC", "BUSD", "PERP"):
            if sym.endswith(suffix) and len(sym) > len(suffix):
                base = sym[:-len(suffix)]
                break

        matches = {eid: lookups.get(eid, {}).get(base) for eid in _ALL_EXCHANGES}
        detail = {"symbol": row["symbol"], "base": base}
        for eid in _ALL_EXCHANGES:
            detail[eid] = matches[eid]
            detail[f"prev_{eid}"] = row.get(f"{eid}_ticker")

        hit_count = sum(1 for v in matches.values() if v)
        if hit_count:
            report["matched"] += 1
            if hit_count > 1:
                report["multi_exchange"] += 1
            else:
                for eid in _ALL_EXCHANGES:
                    if matches[eid]:
                        report[f"{eid}_only"] += 1

            pref = next((eid for eid in _ALL_EXCHANGES if matches[eid]), _ALL_EXCHANGES[0])
            fallback = next((eid for eid in _ALL_EXCHANGES if matches[eid] and eid != pref), pref)

            sets, params = [], []
            for eid in _ALL_EXCHANGES:
                col = f"{eid}_ticker"
                if matches[eid] and matches[eid] != row.get(col):
                    sets.append(f"{col} = %s")
                    params.append(matches[eid])
            if pref != row.get("preferred_exchange"):
                sets.append("preferred_exchange = %s")
                params.append(pref)
                sets.append("fallback_exchange = %s")
                params.append(fallback)

            if sets:
                params.append(row["symbol"])
                db.execute(
                    f"UPDATE market_symbols SET {', '.join(sets)} WHERE symbol = %s",
                    tuple(params))
                report["updated"] += 1
                detail["action"] = "updated"
            else:
                detail["action"] = "unchanged"
        else:
            report["unmatched"] += 1
            detail["action"] = "no_match"

        report["details"].append(detail)

    logger.info(f"[Recon] Complete: {report['matched']} matched, "
                f"{report['unmatched']} unmatched, {report['updated']} updated | "
                + " ".join(f"{eid}={report[f'{eid}_markets']}" for eid in _ALL_EXCHANGES))

    return report


# ─────────────────────────────────────────────────────────────────────
# Singleton and Startup
# ─────────────────────────────────────────────────────────────────────

_collector_instance: Optional[CandleCollector] = None


def get_candle_collector(db=None, config=None) -> CandleCollector:
    """Get the singleton CandleCollector instance."""
    global _collector_instance
    if _collector_instance is None and db is not None:
        _collector_instance = CandleCollector(db, config)
    return _collector_instance


def start_candle_collector(db, config) -> CandleCollector:
    """Create and start the candle collector service."""
    collector = get_candle_collector(db, config)
    collector.start()
    return collector


def stop_candle_collector():
    """Stop the candle collector service."""
    global _collector_instance
    if _collector_instance:
        _collector_instance.stop()
