"""
JarvAIs Market Data Fetcher v1.0
Fetches candle data from multiple sources:
- Yahoo Finance API (primary, works on Linux)
- MT5 direct (when running on Windows with MT5)
Provides OHLCV data for any symbol in multiple timeframes.
"""

import os
import sys
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

# Manus sandbox runtime (Linux only); skip on Windows for cross-environment compatibility
_manus_path = "/opt/.manus/.sandbox-runtime"
if os.path.exists(_manus_path):
    sys.path.append(_manus_path)

logger = logging.getLogger("jarvais.market_data")


@dataclass
class Candle:
    """Single OHLCV candle."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    
    def to_dict(self):
        d = asdict(self)
        d['timestamp'] = self.timestamp.isoformat()
        return d


@dataclass
class MarketSnapshot:
    """Current market state for a symbol."""
    symbol: str
    price: float
    bid: float
    ask: float
    spread: float
    day_high: float
    day_low: float
    day_open: float
    prev_close: float
    volume: int
    change_pct: float
    timestamp: datetime
    source: str
    
    def to_dict(self):
        d = asdict(self)
        d['timestamp'] = self.timestamp.isoformat()
        return d


# Symbol mapping: MT5 symbol -> Yahoo Finance symbol
SYMBOL_MAP = {
    "XAUUSD": "GC=F",       # Gold futures
    "XAGUSD": "SI=F",       # Silver futures
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
    "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X",
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "US30":   "YM=F",       # Dow Jones futures
    "US500":  "ES=F",       # S&P 500 futures
    "USTEC":  "NQ=F",       # Nasdaq futures
    "USOIL":  "CL=F",       # Crude oil futures
    "BTCUSD": "BTC-USD",
    "ETHUSD": "ETH-USD",
}

# Timeframe mapping: MT5 timeframe -> Yahoo interval
TIMEFRAME_MAP = {
    "M1":  "1m",
    "M5":  "5m",
    "M15": "15m",
    "M30": "30m",
    "H1":  "60m",
    "H4":  "60m",   # Yahoo doesn't have 4h, use 1h and aggregate
    "D1":  "1d",
    "W1":  "1wk",
    "MN":  "1mo",
}

# Range mapping for each timeframe
RANGE_MAP = {
    "1m":  "1d",
    "5m":  "5d",
    "15m": "5d",
    "30m": "1mo",
    "60m": "1mo",
    "1d":  "6mo",
    "1wk": "2y",
    "1mo": "5y",
}


class MarketDataFetcher:
    """Fetches market data from Yahoo Finance and MT5."""
    
    def __init__(self, config: dict = None):
        self.config = config or {}
        self._cache = {}
        self._cache_ttl = 60  # seconds
        self._api_client = None
        self._mt5_available = False
        
        # Check if MT5 is available (Windows only)
        try:
            import MetaTrader5 as mt5
            self._mt5_available = True
            logger.info("MT5 Python package available — will use for candle data")
        except ImportError:
            logger.info("MT5 not available (Linux) — using Yahoo Finance for candle data")
        
        # Initialize Yahoo Finance API client
        try:
            from data_api import ApiClient
            self._api_client = ApiClient()
            logger.info("Yahoo Finance API client initialized")
        except ImportError:
            logger.warning("Yahoo Finance API client not available")
    
    def _get_yahoo_symbol(self, mt5_symbol: str) -> str:
        """Convert MT5 symbol to Yahoo Finance symbol."""
        return SYMBOL_MAP.get(mt5_symbol, mt5_symbol)
    
    def _get_yahoo_interval(self, timeframe: str) -> str:
        """Convert MT5 timeframe to Yahoo interval."""
        return TIMEFRAME_MAP.get(timeframe, "1d")
    
    def _cache_key(self, symbol: str, timeframe: str) -> str:
        return f"{symbol}_{timeframe}"
    
    def _is_cache_valid(self, key: str) -> bool:
        if key not in self._cache:
            return False
        cached_time = self._cache[key].get("_cached_at", 0)
        return (time.time() - cached_time) < self._cache_ttl
    
    def get_candles(self, symbol: str, timeframe: str = "H1", 
                    count: int = 100) -> List[Candle]:
        """
        Fetch OHLCV candles for a symbol.
        
        Args:
            symbol: MT5 symbol (e.g., "XAUUSD")
            timeframe: MT5 timeframe (e.g., "M5", "H1", "D1")
            count: Number of candles to return
            
        Returns:
            List of Candle objects, newest last
        """
        cache_key = self._cache_key(symbol, timeframe)
        if self._is_cache_valid(cache_key):
            candles = self._cache[cache_key]["candles"]
            return candles[-count:] if len(candles) > count else candles
        
        # Try MT5 first (Windows), then Yahoo Finance
        if self._mt5_available:
            candles = self._fetch_mt5_candles(symbol, timeframe, count)
            if candles:
                self._cache[cache_key] = {"candles": candles, "_cached_at": time.time()}
                return candles
        
        # Yahoo Finance fallback
        candles = self._fetch_yahoo_candles(symbol, timeframe, count)
        if candles:
            self._cache[cache_key] = {"candles": candles, "_cached_at": time.time()}
        return candles
    
    def _fetch_yahoo_candles(self, symbol: str, timeframe: str, 
                              count: int) -> List[Candle]:
        """Fetch candles from Yahoo Finance API."""
        if not self._api_client:
            logger.error("Yahoo Finance API client not available")
            return []
        
        yahoo_symbol = self._get_yahoo_symbol(symbol)
        interval = self._get_yahoo_interval(timeframe)
        range_val = RANGE_MAP.get(interval, "1mo")
        
        try:
            response = self._api_client.call_api('YahooFinance/get_stock_chart', query={
                'symbol': yahoo_symbol,
                'interval': interval,
                'range': range_val,
                'includeAdjustedClose': True,
            })
            
            if not response or 'chart' not in response:
                logger.error(f"No data from Yahoo Finance for {yahoo_symbol}")
                return []
            
            result = response['chart']['result'][0]
            timestamps = result.get('timestamp', [])
            quotes = result['indicators']['quote'][0]
            
            candles = []
            for i in range(len(timestamps)):
                if quotes['open'][i] is None:
                    continue
                candles.append(Candle(
                    timestamp=datetime.fromtimestamp(timestamps[i]),
                    open=float(quotes['open'][i]),
                    high=float(quotes['high'][i]),
                    low=float(quotes['low'][i]),
                    close=float(quotes['close'][i]),
                    volume=float(quotes['volume'][i] or 0),
                ))
            
            logger.info(f"Fetched {len(candles)} candles for {symbol} ({timeframe}) from Yahoo Finance")
            return candles[-count:] if len(candles) > count else candles
            
        except Exception as e:
            logger.error(f"Yahoo Finance API error for {symbol}: {e}")
            return []
    
    def _fetch_mt5_candles(self, symbol: str, timeframe: str, 
                            count: int) -> List[Candle]:
        """Fetch candles from MT5 (Windows only)."""
        try:
            import MetaTrader5 as mt5
            
            tf_map = {
                "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
                "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
                "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
                "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1,
                "MN": mt5.TIMEFRAME_MN1,
            }
            
            mt5_tf = tf_map.get(timeframe, mt5.TIMEFRAME_H1)
            rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, count)
            
            if rates is None or len(rates) == 0:
                return []
            
            candles = []
            for r in rates:
                candles.append(Candle(
                    timestamp=datetime.fromtimestamp(r['time']),
                    open=float(r['open']),
                    high=float(r['high']),
                    low=float(r['low']),
                    close=float(r['close']),
                    volume=float(r['tick_volume']),
                ))
            
            return candles
            
        except Exception as e:
            logger.error(f"MT5 candle fetch error: {e}")
            return []
    
    def get_market_snapshot(self, symbol: str) -> Optional[MarketSnapshot]:
        """Get current market state for a symbol."""
        yahoo_symbol = self._get_yahoo_symbol(symbol)
        
        if not self._api_client:
            return None
        
        try:
            response = self._api_client.call_api('YahooFinance/get_stock_chart', query={
                'symbol': yahoo_symbol,
                'interval': '1d',
                'range': '2d',
                'includeAdjustedClose': True,
            })
            
            if not response or 'chart' not in response:
                return None
            
            meta = response['chart']['result'][0]['meta']
            price = meta.get('regularMarketPrice', 0)
            prev_close = meta.get('chartPreviousClose', meta.get('previousClose', price))
            day_high = meta.get('regularMarketDayHigh', price)
            day_low = meta.get('regularMarketDayLow', price)
            
            change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
            
            return MarketSnapshot(
                symbol=symbol,
                price=price,
                bid=price - 0.5,  # Approximate — real spread from MT5
                ask=price + 0.5,
                spread=1.0,
                day_high=day_high,
                day_low=day_low,
                day_open=meta.get('regularMarketOpen', price),
                prev_close=prev_close,
                volume=meta.get('regularMarketVolume', 0),
                change_pct=round(change_pct, 2),
                timestamp=datetime.now(),
                source="yahoo_finance"
            )
            
        except Exception as e:
            logger.error(f"Market snapshot error for {symbol}: {e}")
            return None
    
    def get_technical_context(self, symbol: str, timeframe: str = "H1") -> Dict:
        """
        Build technical analysis context for AI prompts.
        Returns key levels, trend direction, and recent price action.
        """
        candles = self.get_candles(symbol, timeframe, count=100)
        if not candles or len(candles) < 20:
            return {"error": "Insufficient candle data"}
        
        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        
        # Simple Moving Averages
        sma_20 = sum(closes[-20:]) / 20
        sma_50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else sma_20
        
        # Current price vs SMAs
        current = closes[-1]
        trend = "bullish" if current > sma_20 > sma_50 else (
            "bearish" if current < sma_20 < sma_50 else "ranging"
        )
        
        # Recent high/low (20 candles)
        recent_high = max(highs[-20:])
        recent_low = min(lows[-20:])
        
        # ATR (14-period)
        atr_values = []
        for i in range(1, min(15, len(candles))):
            tr = max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i-1].close),
                abs(candles[i].low - candles[i-1].close)
            )
            atr_values.append(tr)
        atr = sum(atr_values) / len(atr_values) if atr_values else 0
        
        # RSI (14-period)
        gains, losses = [], []
        for i in range(1, min(15, len(closes))):
            diff = closes[-i] - closes[-i-1]
            if diff > 0:
                gains.append(diff)
            else:
                losses.append(abs(diff))
        avg_gain = sum(gains) / 14 if gains else 0
        avg_loss = sum(losses) / 14 if losses else 0.001
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        # Last 5 candles summary
        last_5 = []
        for c in candles[-5:]:
            body = c.close - c.open
            direction = "bullish" if body > 0 else "bearish"
            last_5.append({
                "time": c.timestamp.strftime("%Y-%m-%d %H:%M"),
                "direction": direction,
                "body_size": round(abs(body), 2),
                "upper_wick": round(c.high - max(c.open, c.close), 2),
                "lower_wick": round(min(c.open, c.close) - c.low, 2),
            })
        
        # Support/Resistance (simple pivot points)
        pivot = (candles[-1].high + candles[-1].low + candles[-1].close) / 3
        r1 = 2 * pivot - candles[-1].low
        s1 = 2 * pivot - candles[-1].high
        r2 = pivot + (candles[-1].high - candles[-1].low)
        s2 = pivot - (candles[-1].high - candles[-1].low)
        
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "current_price": round(current, 2),
            "trend": trend,
            "sma_20": round(sma_20, 2),
            "sma_50": round(sma_50, 2),
            "rsi_14": round(rsi, 1),
            "atr_14": round(atr, 2),
            "recent_high_20": round(recent_high, 2),
            "recent_low_20": round(recent_low, 2),
            "pivot": round(pivot, 2),
            "resistance_1": round(r1, 2),
            "resistance_2": round(r2, 2),
            "support_1": round(s1, 2),
            "support_2": round(s2, 2),
            "last_5_candles": last_5,
            "candle_count": len(candles),
            "data_source": "mt5" if self._mt5_available else "yahoo_finance",
        }
    
    def get_multi_timeframe_context(self, symbol: str) -> Dict:
        """Get technical context across multiple timeframes for AI."""
        contexts = {}
        for tf in ["M15", "H1", "H4", "D1"]:
            ctx = self.get_technical_context(symbol, tf)
            if "error" not in ctx:
                contexts[tf] = ctx
        
        # Determine overall bias
        trends = [ctx.get("trend", "ranging") for ctx in contexts.values()]
        bullish_count = trends.count("bullish")
        bearish_count = trends.count("bearish")
        
        if bullish_count >= 3:
            overall_bias = "strong_bullish"
        elif bullish_count >= 2:
            overall_bias = "bullish"
        elif bearish_count >= 3:
            overall_bias = "strong_bearish"
        elif bearish_count >= 2:
            overall_bias = "bearish"
        else:
            overall_bias = "mixed"
        
        return {
            "symbol": symbol,
            "overall_bias": overall_bias,
            "timeframes": contexts,
            "timestamp": datetime.now().isoformat(),
        }


# Singleton
_fetcher = None

def get_market_data(config: dict = None) -> MarketDataFetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = MarketDataFetcher(config)
    return _fetcher
