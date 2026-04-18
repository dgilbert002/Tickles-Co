"""
JarvAIs Alpha Intelligence Module
==================================
Aggregates market intelligence from multiple free sources:
1. Forex Factory Economic Calendar (JSON API)
2. TradingView Technical Analysis Scanner (POST API)
3. Google News RSS (XML feed)
4. Yahoo Finance (price data + news)
5. MQL5 Economic Calendar (HTML scrape)

All sources are FREE and require no API keys.
Future: Finnhub (free tier), TipRanks (user subscription), ForexNewsAPI (paid)
"""

import asyncio
import logging
import time
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from xml.etree import ElementTree as ET
import json
import requests

from core.time_utils import utcnow

logger = logging.getLogger("jarvais.alpha")


# ─────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────

@dataclass
class NewsArticle:
    """A single news article from any source."""
    title: str
    source: str
    url: str = ""
    published: str = ""
    symbol: str = ""
    sentiment: str = "neutral"  # bullish, bearish, neutral
    impact: str = "low"  # low, medium, high
    category: str = "news"  # news, analysis, calendar, technical
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    def summary_for_ai(self) -> str:
        return f"[{self.source}|{self.impact}|{self.sentiment}] {self.title}"


@dataclass
class EconomicEvent:
    """An economic calendar event."""
    title: str
    country: str
    date: str
    impact: str  # High, Medium, Low
    forecast: str = ""
    previous: str = ""
    actual: str = ""
    source: str = "forex_factory"
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    def summary_for_ai(self) -> str:
        parts = [f"[{self.country}|{self.impact}] {self.title} @ {self.date[:16]}"]
        if self.forecast:
            parts.append(f"Forecast: {self.forecast}")
        if self.previous:
            parts.append(f"Previous: {self.previous}")
        if self.actual:
            parts.append(f"Actual: {self.actual}")
        return " | ".join(parts)


@dataclass
class TechnicalSnapshot:
    """TradingView technical analysis snapshot."""
    symbol: str
    price: float
    change_pct: float
    recommendation: str  # Strong Buy, Buy, Neutral, Sell, Strong Sell
    rec_score: float  # -1 to 1
    ma_recommendation: str
    ma_score: float
    osc_recommendation: str
    osc_score: float
    rsi: float = 0.0
    macd: float = 0.0
    macd_signal: float = 0.0
    stoch_k: float = 0.0
    stoch_d: float = 0.0
    cci: float = 0.0
    adx: float = 0.0
    adx_plus: float = 0.0
    adx_minus: float = 0.0
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    sma_20: float = 0.0
    sma_50: float = 0.0
    sma_200: float = 0.0
    ema_20: float = 0.0
    ema_50: float = 0.0
    ema_200: float = 0.0
    atr: float = 0.0
    volatility: float = 0.0
    pivot: float = 0.0
    support_1: float = 0.0
    resistance_1: float = 0.0
    timestamp: str = ""
    source: str = "tradingview"
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    def summary_for_ai(self) -> str:
        lines = [
            f"TradingView Analysis for {self.symbol} @ ${self.price:.2f} ({self.change_pct:+.2f}%)",
            f"Overall: {self.recommendation} ({self.rec_score:.2f}) | MA: {self.ma_recommendation} ({self.ma_score:.2f}) | Osc: {self.osc_recommendation} ({self.osc_score:.2f})",
            f"RSI: {self.rsi:.1f} | MACD: {self.macd:.2f} vs Signal {self.macd_signal:.2f} | Stoch: K={self.stoch_k:.1f} D={self.stoch_d:.1f}",
            f"ADX: {self.adx:.1f} (+DI={self.adx_plus:.1f} -DI={self.adx_minus:.1f}) | CCI: {self.cci:.1f}",
            f"SMA20: {self.sma_20:.2f} | SMA50: {self.sma_50:.2f} | SMA200: {self.sma_200:.2f}",
            f"BB: {self.bb_lower:.2f} - {self.bb_upper:.2f} | ATR: {self.atr:.2f}",
            f"Pivot: {self.pivot:.2f} | S1: {self.support_1:.2f} | R1: {self.resistance_1:.2f}",
        ]
        return "\n".join(lines)


@dataclass
class AlphaDossier:
    """Complete alpha intelligence package for a symbol."""
    symbol: str
    timestamp: str
    technical: Optional[TechnicalSnapshot] = None
    news: List[NewsArticle] = field(default_factory=list)
    calendar_events: List[EconomicEvent] = field(default_factory=list)
    market_context: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "technical": self.technical.to_dict() if self.technical else None,
            "news": [n.to_dict() for n in self.news],
            "calendar_events": [e.to_dict() for e in self.calendar_events],
            "market_context": self.market_context,
        }
    
    def summary_for_ai(self) -> str:
        """Generate a concise summary for AI role consumption."""
        sections = [f"=== ALPHA DOSSIER: {self.symbol} ({self.timestamp}) ===\n"]
        
        if self.technical:
            sections.append("--- TECHNICAL ANALYSIS ---")
            sections.append(self.technical.summary_for_ai())
        
        if self.news:
            sections.append(f"\n--- NEWS ({len(self.news)} articles) ---")
            for n in self.news[:10]:
                sections.append(n.summary_for_ai())
        
        if self.calendar_events:
            high_events = [e for e in self.calendar_events if e.impact == "High"]
            sections.append(f"\n--- ECONOMIC CALENDAR ({len(self.calendar_events)} events, {len(high_events)} high-impact) ---")
            for e in high_events[:5]:
                sections.append(e.summary_for_ai())
        
        if self.market_context:
            sections.append(f"\n--- MARKET CONTEXT ---")
            for k, v in self.market_context.items():
                sections.append(f"  {k}: {v}")
        
        return "\n".join(sections)


# ─────────────────────────────────────────────
# Source Fetchers
# ─────────────────────────────────────────────

class ForexFactoryFetcher:
    """Fetches economic calendar from Forex Factory free JSON API."""
    
    URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    
    def __init__(self):
        self._cache = None
        self._cache_time = 0
        self._cache_ttl = 3600  # 1 hour cache
    
    def fetch(self) -> List[EconomicEvent]:
        """Fetch this week's economic calendar."""
        now = time.time()
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache
        
        try:
            r = requests.get(self.URL, timeout=15)
            r.raise_for_status()
            data = r.json()
            
            events = []
            for item in data:
                events.append(EconomicEvent(
                    title=item.get("title", ""),
                    country=item.get("country", ""),
                    date=item.get("date", ""),
                    impact=item.get("impact", "Low"),
                    forecast=item.get("forecast", ""),
                    previous=item.get("previous", ""),
                    actual=item.get("actual", ""),
                    source="forex_factory"
                ))
            
            self._cache = events
            self._cache_time = now
            logger.info(f"ForexFactory: fetched {len(events)} events")
            return events
            
        except Exception as e:
            logger.error(f"ForexFactory fetch error: {e}")
            return self._cache or []
    
    def get_upcoming_high_impact(self, hours_ahead: int = 4) -> List[EconomicEvent]:
        """Get high-impact events in the next N hours."""
        events = self.fetch()
        now = utcnow()
        cutoff = now + timedelta(hours=hours_ahead)
        
        upcoming = []
        for e in events:
            if e.impact != "High":
                continue
            try:
                # Parse ISO format date
                event_time = datetime.fromisoformat(e.date.replace("Z", "+00:00"))
                event_utc = event_time.replace(tzinfo=None) if event_time.tzinfo else event_time
                if now <= event_utc <= cutoff:
                    upcoming.append(e)
            except (ValueError, TypeError):
                continue
        
        return upcoming
    
    def get_events_for_currency(self, currency: str) -> List[EconomicEvent]:
        """Get all events for a specific currency (e.g., 'USD', 'EUR')."""
        events = self.fetch()
        return [e for e in events if e.country.upper() == currency.upper()]


class TradingViewFetcher:
    """Fetches technical analysis from TradingView Scanner API (free, no key)."""
    
    SCAN_URL = "https://scanner.tradingview.com/cfd/scan"
    
    # Map symbols to TradingView exchange prefixes
    SYMBOL_MAP = {
        "XAUUSD": "OANDA:XAUUSD",
        "XAGUSD": "OANDA:XAGUSD",
        "EURUSD": "FX:EURUSD",
        "GBPUSD": "FX:GBPUSD",
        "USDJPY": "FX:USDJPY",
        "AUDUSD": "FX:AUDUSD",
        "USDCAD": "FX:USDCAD",
        "USDCHF": "FX:USDCHF",
        "NZDUSD": "FX:NZDUSD",
        "EURJPY": "FX:EURJPY",
        "GBPJPY": "FX:GBPJPY",
        "US30": "FOREXCOM:DJI",
        "US500": "FOREXCOM:SPX500",
        "NAS100": "FOREXCOM:NSXUSD",
        "BTCUSD": "BITSTAMP:BTCUSD",
    }
    
    COLUMNS = [
        "close", "change", "change_abs", "high", "low", "volume",
        "Recommend.All", "Recommend.MA", "Recommend.Other",
        "RSI", "RSI[1]", "Stoch.K", "Stoch.D", "CCI20", "ADX", "ADX+DI", "ADX-DI",
        "MACD.macd", "MACD.signal", "BB.upper", "BB.lower",
        "SMA20", "SMA50", "SMA200", "EMA20", "EMA50", "EMA200",
        "Pivot.M.Classic.S1", "Pivot.M.Classic.R1", "Pivot.M.Classic.Middle",
        "ATR", "Volatility.D"
    ]
    
    def __init__(self):
        self._cache: Dict[str, tuple] = {}  # symbol -> (snapshot, timestamp)
        self._cache_ttl = 300  # 5 min cache
    
    @staticmethod
    def _score_to_recommendation(score: float) -> str:
        if score >= 0.5:
            return "Strong Buy"
        elif score >= 0.1:
            return "Buy"
        elif score > -0.1:
            return "Neutral"
        elif score > -0.5:
            return "Sell"
        else:
            return "Strong Sell"
    
    def fetch(self, symbol: str) -> Optional[TechnicalSnapshot]:
        """Fetch full technical analysis for a symbol."""
        now = time.time()
        if symbol in self._cache:
            cached, ts = self._cache[symbol]
            if (now - ts) < self._cache_ttl:
                return cached
        
        try:
            payload = {
                "filter": [{"left": "name", "operation": "equal", "right": symbol}],
                "symbols": {"query": {"types": []}},
                "columns": self.COLUMNS
            }
            
            r = requests.post(self.SCAN_URL, json=payload, timeout=15,
                            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})
            r.raise_for_status()
            data = r.json()
            
            if not data.get("data"):
                logger.warning(f"TradingView: no data for {symbol}")
                return None
            
            # Use the first result (usually the main exchange)
            row = data["data"][0]
            d = row["d"]
            
            def safe_float(idx: int, default: float = 0.0) -> float:
                try:
                    v = d[idx]
                    return float(v) if v is not None else default
                except (IndexError, TypeError, ValueError):
                    return default
            
            rec_score = safe_float(6)
            ma_score = safe_float(7)
            osc_score = safe_float(8)
            
            snapshot = TechnicalSnapshot(
                symbol=symbol,
                price=safe_float(0),
                change_pct=safe_float(1) * 100,  # Convert to percentage
                recommendation=self._score_to_recommendation(rec_score),
                rec_score=rec_score,
                ma_recommendation=self._score_to_recommendation(ma_score),
                ma_score=ma_score,
                osc_recommendation=self._score_to_recommendation(osc_score),
                osc_score=osc_score,
                rsi=safe_float(9),
                macd=safe_float(17),
                macd_signal=safe_float(18),
                stoch_k=safe_float(11),
                stoch_d=safe_float(12),
                cci=safe_float(13),
                adx=safe_float(14),
                adx_plus=safe_float(15),
                adx_minus=safe_float(16),
                bb_upper=safe_float(19),
                bb_lower=safe_float(20),
                sma_20=safe_float(21),
                sma_50=safe_float(22),
                sma_200=safe_float(23),
                ema_20=safe_float(24),
                ema_50=safe_float(25),
                ema_200=safe_float(26),
                atr=safe_float(30),
                volatility=safe_float(31),
                pivot=safe_float(29),
                support_1=safe_float(27),
                resistance_1=safe_float(28),
                timestamp=utcnow().isoformat(),
                source="tradingview"
            )
            
            self._cache[symbol] = (snapshot, now)
            logger.info(f"TradingView: {symbol} = {snapshot.recommendation} ({rec_score:.2f})")
            return snapshot
            
        except Exception as e:
            logger.error(f"TradingView fetch error for {symbol}: {e}")
            if symbol in self._cache:
                return self._cache[symbol][0]
            return None


class GoogleNewsFetcher:
    """Fetches news from Google News RSS (free, no key, 100 articles per query)."""
    
    BASE_URL = "https://news.google.com/rss/search"
    
    # Map trading symbols to search queries
    SYMBOL_QUERIES = {
        "XAUUSD": "gold price XAUUSD",
        "XAGUSD": "silver price XAGUSD",
        "EURUSD": "EURUSD euro dollar forex",
        "GBPUSD": "GBPUSD pound dollar forex",
        "USDJPY": "USDJPY dollar yen forex",
        "US30": "Dow Jones US30 market",
        "US500": "S&P 500 SPX market",
        "NAS100": "Nasdaq 100 market",
        "BTCUSD": "Bitcoin BTC price",
    }
    
    def __init__(self):
        self._cache: Dict[str, tuple] = {}
        self._cache_ttl = 900  # 15 min cache
    
    def fetch(self, symbol: str, max_articles: int = 20) -> List[NewsArticle]:
        """Fetch recent news articles for a symbol."""
        now = time.time()
        if symbol in self._cache:
            cached, ts = self._cache[symbol]
            if (now - ts) < self._cache_ttl:
                return cached[:max_articles]
        
        query = self.SYMBOL_QUERIES.get(symbol, f"{symbol} forex trading")
        
        try:
            params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
            r = requests.get(self.BASE_URL, params=params, timeout=15,
                           headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            
            root = ET.fromstring(r.text)
            items = root.findall('.//item')
            
            articles = []
            for item in items[:max_articles]:
                title = item.find('title')
                link = item.find('link')
                pubdate = item.find('pubDate')
                source_elem = item.find('source')
                
                title_text = title.text if title is not None else "N/A"
                
                # Basic sentiment detection from title
                sentiment = self._detect_sentiment(title_text)
                
                articles.append(NewsArticle(
                    title=title_text,
                    source=source_elem.text if source_elem is not None else "Google News",
                    url=link.text if link is not None else "",
                    published=pubdate.text if pubdate is not None else "",
                    symbol=symbol,
                    sentiment=sentiment,
                    impact=self._estimate_impact(title_text),
                    category="news"
                ))
            
            self._cache[symbol] = (articles, now)
            logger.info(f"GoogleNews: fetched {len(articles)} articles for {symbol}")
            return articles
            
        except Exception as e:
            logger.error(f"GoogleNews fetch error for {symbol}: {e}")
            if symbol in self._cache:
                return self._cache[symbol][0][:max_articles]
            return []
    
    @staticmethod
    def _detect_sentiment(title: str) -> str:
        """Simple keyword-based sentiment detection."""
        title_lower = title.lower()
        
        bullish_words = ["rally", "surge", "soar", "jump", "gain", "rise", "bullish", 
                        "record high", "all-time high", "breakout", "upside", "strong",
                        "buy", "support holds", "recovery", "boom"]
        bearish_words = ["crash", "plunge", "drop", "fall", "decline", "bearish",
                        "sell-off", "selloff", "slump", "tumble", "downside", "weak",
                        "resistance", "correction", "fear", "panic"]
        
        bull_count = sum(1 for w in bullish_words if w in title_lower)
        bear_count = sum(1 for w in bearish_words if w in title_lower)
        
        if bull_count > bear_count:
            return "bullish"
        elif bear_count > bull_count:
            return "bearish"
        return "neutral"
    
    @staticmethod
    def _estimate_impact(title: str) -> str:
        """Estimate news impact from title keywords."""
        title_lower = title.lower()
        
        high_impact = ["fed", "fomc", "nfp", "cpi", "gdp", "interest rate", "central bank",
                      "ecb", "boj", "rba", "boe", "tariff", "sanctions", "war", "crisis",
                      "record", "all-time", "crash", "emergency"]
        medium_impact = ["forecast", "outlook", "analysis", "report", "data", "inflation",
                        "employment", "trade", "policy", "meeting"]
        
        if any(w in title_lower for w in high_impact):
            return "high"
        elif any(w in title_lower for w in medium_impact):
            return "medium"
        return "low"


class YahooFinanceFetcher:
    """Fetches price data from Yahoo Finance."""
    
    # Map trading symbols to Yahoo Finance tickers
    SYMBOL_MAP = {
        "XAUUSD": "GC=F",
        "XAGUSD": "SI=F",
        "EURUSD": "EURUSD=X",
        "GBPUSD": "GBPUSD=X",
        "USDJPY": "JPY=X",
        "AUDUSD": "AUDUSD=X",
        "USDCAD": "CAD=X",
        "USDCHF": "CHF=X",
        "US30": "YM=F",
        "US500": "ES=F",
        "NAS100": "NQ=F",
        "BTCUSD": "BTC-USD",
    }
    
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0"})
    
    def get_price(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get current price data for a symbol."""
        yahoo_ticker = self.SYMBOL_MAP.get(symbol, f"{symbol}=X")
        
        try:
            # Use Yahoo Finance v8 API
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}"
            params = {"interval": "1d", "range": "5d"}
            r = self._session.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            
            if "chart" in data and data["chart"]["result"]:
                meta = data["chart"]["result"][0]["meta"]
                return {
                    "symbol": symbol,
                    "price": meta.get("regularMarketPrice", 0),
                    "prev_close": meta.get("previousClose", 0),
                    "day_high": meta.get("regularMarketDayHigh", 0),
                    "day_low": meta.get("regularMarketDayLow", 0),
                    "volume": meta.get("regularMarketVolume", 0),
                    "change_pct": round(
                        ((meta.get("regularMarketPrice", 0) - meta.get("previousClose", 1)) 
                         / meta.get("previousClose", 1)) * 100, 2
                    ) if meta.get("previousClose") else 0,
                    "source": "yahoo_finance"
                }
            return None
            
        except Exception as e:
            logger.error(f"Yahoo Finance error for {symbol}: {e}")
            return None


# ─────────────────────────────────────────────
# Alpha Intelligence Aggregator
# ─────────────────────────────────────────────

class AlphaIntelligence:
    """
    Master aggregator that combines all alpha sources into a unified dossier.
    This is what the Analyst role consumes to build its market assessment.
    """
    
    def __init__(self, config: dict = None):
        self.config = config or {}
        
        # Initialize all fetchers
        self.forex_factory = ForexFactoryFetcher()
        self.tradingview = TradingViewFetcher()
        self.google_news = GoogleNewsFetcher()
        self.yahoo = YahooFinanceFetcher()
        
        # Source registry for dynamic management
        self._sources = {
            "forex_factory": {"fetcher": self.forex_factory, "enabled": True, "type": "calendar"},
            "tradingview": {"fetcher": self.tradingview, "enabled": True, "type": "technical"},
            "google_news": {"fetcher": self.google_news, "enabled": True, "type": "news"},
            "yahoo_finance": {"fetcher": self.yahoo, "enabled": True, "type": "price"},
        }
        
        # Fetch stats
        self._stats = {
            "total_fetches": 0,
            "errors": 0,
            "last_fetch": None,
        }
        
        logger.info(f"AlphaIntelligence initialized with {len(self._sources)} sources")
    
    def compile_dossier(self, symbol: str) -> AlphaDossier:
        """
        Compile a complete alpha intelligence dossier for a symbol.
        This is the main entry point called by the Analyst role.
        """
        start = time.time()
        self._stats["total_fetches"] += 1
        self._stats["last_fetch"] = utcnow().isoformat()
        
        dossier = AlphaDossier(
            symbol=symbol,
            timestamp=utcnow().isoformat()
        )
        
        # 1. Technical Analysis from TradingView
        if self._sources["tradingview"]["enabled"]:
            try:
                dossier.technical = self.tradingview.fetch(symbol)
            except Exception as e:
                logger.error(f"TradingView error: {e}")
                self._stats["errors"] += 1
        
        # 2. News from Google News
        if self._sources["google_news"]["enabled"]:
            try:
                dossier.news = self.google_news.fetch(symbol, max_articles=15)
            except Exception as e:
                logger.error(f"GoogleNews error: {e}")
                self._stats["errors"] += 1
        
        # 3. Economic Calendar from Forex Factory
        if self._sources["forex_factory"]["enabled"]:
            try:
                all_events = self.forex_factory.fetch()
                # Filter relevant events based on symbol's currencies
                relevant_currencies = self._get_relevant_currencies(symbol)
                dossier.calendar_events = [
                    e for e in all_events 
                    if e.country in relevant_currencies or e.impact == "High"
                ]
            except Exception as e:
                logger.error(f"ForexFactory error: {e}")
                self._stats["errors"] += 1
        
        # 4. Price data from Yahoo Finance
        if self._sources["yahoo_finance"]["enabled"]:
            try:
                price_data = self.yahoo.get_price(symbol)
                if price_data:
                    dossier.market_context["yahoo_price"] = price_data
            except Exception as e:
                logger.error(f"Yahoo error: {e}")
                self._stats["errors"] += 1
        
        # 5. Compute derived intelligence
        dossier.market_context["sentiment_summary"] = self._compute_sentiment_summary(dossier)
        dossier.market_context["risk_events_ahead"] = self._count_risk_events(dossier)
        dossier.market_context["compilation_time_ms"] = round((time.time() - start) * 1000)
        dossier.market_context["sources_used"] = [
            k for k, v in self._sources.items() if v["enabled"]
        ]
        
        elapsed = time.time() - start
        logger.info(f"Dossier compiled for {symbol} in {elapsed:.1f}s: "
                    f"{len(dossier.news)} news, {len(dossier.calendar_events)} events, "
                    f"tech={'yes' if dossier.technical else 'no'}")
        
        return dossier
    
    def get_upcoming_risk_events(self, hours_ahead: int = 4) -> List[EconomicEvent]:
        """Get high-impact economic events in the next N hours."""
        return self.forex_factory.get_upcoming_high_impact(hours_ahead)
    
    def get_news_for_symbol(self, symbol: str, max_articles: int = 10) -> List[NewsArticle]:
        """Get latest news for a specific symbol."""
        return self.google_news.fetch(symbol, max_articles)
    
    def get_technical_analysis(self, symbol: str) -> Optional[TechnicalSnapshot]:
        """Get TradingView technical analysis for a symbol."""
        return self.tradingview.fetch(symbol)
    
    def enable_source(self, source_name: str):
        """Enable a data source."""
        if source_name in self._sources:
            self._sources[source_name]["enabled"] = True
            logger.info(f"Source enabled: {source_name}")
    
    def disable_source(self, source_name: str):
        """Disable a data source."""
        if source_name in self._sources:
            self._sources[source_name]["enabled"] = False
            logger.info(f"Source disabled: {source_name}")
    
    def get_source_status(self) -> Dict[str, Any]:
        """Get status of all data sources."""
        status = {}
        for name, info in self._sources.items():
            status[name] = {
                "enabled": info["enabled"],
                "type": info["type"],
                "has_cache": hasattr(info["fetcher"], "_cache") and bool(getattr(info["fetcher"], "_cache", None)),
            }
        status["stats"] = self._stats
        return status
    
    def health_check(self) -> Dict[str, str]:
        """Quick health check of all sources."""
        results = {}
        
        # Test Forex Factory
        try:
            events = self.forex_factory.fetch()
            results["forex_factory"] = f"ok ({len(events)} events)"
        except Exception as e:
            results["forex_factory"] = f"error: {e}"
        
        # Test TradingView
        try:
            snap = self.tradingview.fetch("XAUUSD")
            results["tradingview"] = f"ok (XAUUSD={snap.recommendation})" if snap else "no data"
        except Exception as e:
            results["tradingview"] = f"error: {e}"
        
        # Test Google News
        try:
            news = self.google_news.fetch("XAUUSD", max_articles=3)
            results["google_news"] = f"ok ({len(news)} articles)"
        except Exception as e:
            results["google_news"] = f"error: {e}"
        
        # Test Yahoo Finance
        try:
            price = self.yahoo.get_price("XAUUSD")
            results["yahoo_finance"] = f"ok (${price['price']})" if price else "no data"
        except Exception as e:
            results["yahoo_finance"] = f"error: {e}"
        
        return results
    
    @staticmethod
    def _get_relevant_currencies(symbol: str) -> set:
        """Extract relevant currencies from a trading symbol."""
        currency_map = {
            "XAUUSD": {"USD"},
            "XAGUSD": {"USD"},
            "EURUSD": {"EUR", "USD"},
            "GBPUSD": {"GBP", "USD"},
            "USDJPY": {"USD", "JPY"},
            "AUDUSD": {"AUD", "USD"},
            "USDCAD": {"USD", "CAD"},
            "USDCHF": {"USD", "CHF"},
            "NZDUSD": {"NZD", "USD"},
            "EURJPY": {"EUR", "JPY"},
            "GBPJPY": {"GBP", "JPY"},
        }
        return currency_map.get(symbol, {"USD"})
    
    @staticmethod
    def _compute_sentiment_summary(dossier: AlphaDossier) -> Dict[str, Any]:
        """Compute overall sentiment from all news sources."""
        if not dossier.news:
            return {"overall": "neutral", "bullish": 0, "bearish": 0, "neutral": 0}
        
        counts = {"bullish": 0, "bearish": 0, "neutral": 0}
        for article in dossier.news:
            counts[article.sentiment] = counts.get(article.sentiment, 0) + 1
        
        total = sum(counts.values())
        if total == 0:
            return {"overall": "neutral", **counts}
        
        # Determine overall sentiment
        if counts["bullish"] > counts["bearish"] * 1.5:
            overall = "bullish"
        elif counts["bearish"] > counts["bullish"] * 1.5:
            overall = "bearish"
        else:
            overall = "mixed"
        
        return {
            "overall": overall,
            "bullish": counts["bullish"],
            "bearish": counts["bearish"],
            "neutral": counts["neutral"],
            "bullish_pct": round(counts["bullish"] / total * 100, 1),
            "bearish_pct": round(counts["bearish"] / total * 100, 1),
        }
    
    @staticmethod
    def _count_risk_events(dossier: AlphaDossier) -> Dict[str, int]:
        """Count upcoming risk events by impact level."""
        counts = {"high": 0, "medium": 0, "low": 0}
        for event in dossier.calendar_events:
            impact = event.impact.lower()
            if impact in counts:
                counts[impact] += 1
        return counts


# ─────────────────────────────────────────────
# Convenience function
# ─────────────────────────────────────────────

_instance: Optional[AlphaIntelligence] = None

def get_alpha_intelligence(config: dict = None) -> AlphaIntelligence:
    """Get or create the singleton AlphaIntelligence instance."""
    global _instance
    if _instance is None:
        _instance = AlphaIntelligence(config)
    return _instance
