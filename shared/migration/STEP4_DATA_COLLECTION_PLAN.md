# Step 4: Data Collection Services — Architecture Plan

> **Status**: Ready for implementation  
> **Depends on**: Steps 1-3 (complete)  
> **Blocks**: Steps 5-12  
> **Last updated**: 2026-04-12

---

## Problem Statement

CONTEXT_V3.md Section 16, Step 4 requires: "Port candle collector with adapter pattern + `data_hash` + retention. Port Telegram, Discord, RSS collectors to V2 schema. Port TradingView idea monitor. Port adaptive timing service. Verify: data flows into `tickles_shared.candles` and `tickles_shared.news_items`."

The legacy systems have:
- **Capital 2.0** (TypeScript): WebSocket candle streaming, REST API backfill, bid/ask candles, Capital.com-specific
- **JarvAIs V1** (Python): CCXT-based candle collection, RSS/Telegram/Discord collectors, market regime detection

V2 must unify these into exchange-agnostic Python services that write to the normalized schema.

---

## Assumptions and Constraints

1. **Python 3.12** — all services are Python, no TypeScript
2. **CCXT** is the primary exchange adapter library (Bybit primary, BloFin/Bitget secondary)
3. **Capital.com** support is deferred to Company 2 (Capital CFD Co) — not built now
4. **Crypto is 24/7** — no market close detection needed for crypto instruments
5. **CFD market hours** will be needed later — timing service must support both modes
6. **Candle data hash** (SHA-256) is mandatory on every candle write for drift detection
7. **Partitioned candles table** — writes must target the correct monthly partition
8. **Connection pooling** — max 50 MySQL connections, each service gets 5-10
9. **No bid/ask columns** in V2 schema — crypto uses mid-price; CFDs will add bid/ask later
10. **Telegram/Discord/TradingView** collectors are stubbed for now (need API keys) — RSS is fully implemented

---

## Directory Structure

```
/opt/tickles/shared/
├── connectors/                  # Exchange adapters
│   ├── __init__.py
│   ├── base.py                  # BaseExchangeAdapter ABC
│   ├── ccxt_adapter.py          # CCXT adapter (Bybit, BloFin, Bitget)
│   └── capital_adapter.py       # Capital.com adapter (STUB - future)
├── market-data/                 # Candle service + timing
│   ├── __init__.py
│   ├── candle_service.py        # Main candle collection orchestrator
│   ├── gap_detector.py          # Gap detection and backfill logic
│   ├── retention.py             # Partition management + retention policy
│   └── timing_service.py        # Adaptive timing (market hours, DST)
├── news/                        # News/social collectors
│   ├── __init__.py
│   ├── base.py                  # BaseCollector ABC (from JarvAIs V1)
│   ├── rss_collector.py         # RSS news collector (fully implemented)
│   ├── telegram_collector.py    # Telegram collector (STUB)
│   ├── discord_collector.py     # Discord collector (STUB)
│   └── tradingview_monitor.py   # TradingView ideas (STUB)
├── backtesting/                 # Empty for now (Step 6)
└── utils/                       # Shared utilities
    ├── __init__.py
    ├── db.py                    # Shared DB connection pool
    ├── config.py                # Configuration loader
    └── mem0_config.py           # Already exists
```

---

## Interface Definitions

### 1. BaseExchangeAdapter (`shared/connectors/base.py`)

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime
from enum import Enum

class MarketStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"

@dataclass
class Candle:
    """Normalized candle data — maps to tickles_shared.candles"""
    instrument_id: int
    timeframe: str          # '1m', '5m', '15m', '1h', '4h', '1d'
    timestamp: datetime     # UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0
    trades_count: int = 0
    data_source: str = "api"  # 'api', 'websocket', 'backfill'

class BaseExchangeAdapter(ABC):
    """Abstract base class for all exchange adapters.
    
    Every exchange (CCXT, Capital.com, etc.) implements this interface.
    The CandleService uses whichever adapter is configured for the instrument.
    """
    
    @abstractmethod
    async def fetch_ohlcv(
        self,
        symbol: str,           # e.g., 'BTC/USDT'
        timeframe: str,         # '1m', '5m', '15m', '1h', '4h', '1d'
        since: Optional[datetime] = None,
        limit: int = 1000
    ) -> List[Candle]:
        """Fetch historical OHLCV data from the exchange.
        
        Returns:
            List of Candle objects with instrument_id=0 (caller resolves).
            Caller must look up instrument_id from the instruments table.
        """
        ...
    
    @abstractmethod
    async def get_market_status(self, symbol: str) -> MarketStatus:
        """Check if a market is currently open for trading."""
        ...
    
    @abstractmethod
    async def get_instruments(self) -> List[dict]:
        """Get list of available instruments from the exchange.
        
        Returns:
            List of dicts with keys: symbol, base, quote, type, exchange
        """
        ...
    
    @abstractmethod
    def get_exchange_name(self) -> str:
        """Return the exchange identifier (e.g., 'bybit', 'blofin')."""
        ...
```

### 2. CCXT Adapter (`shared/connectors/ccxt_adapter.py`)

```python
class CCXTAdapter(BaseExchangeAdapter):
    """CCXT-based adapter for crypto exchanges.
    
    Supports: Bybit (primary), BloFin, Bitget, and any CCXT-supported exchange.
    Handles rate limiting, retries, and error recovery.
    """
    
    def __init__(self, exchange_id: str, config: dict):
        # exchange_id: 'bybit', 'blofin', 'bitget'
        # config: { 'apiKey': ..., 'secret': ..., 'sandbox': True/False }
        ...
    
    async def fetch_ohlcv(self, symbol, timeframe, since=None, limit=1000) -> List[Candle]:
        # Maps CCXT timeframe strings to our enum
        # Handles rate limiting (ccxt built-in)
        # Retries on network errors (3x exponential backoff)
        # Returns Candle objects with data_source='api'
        ...
    
    async def get_market_status(self, symbol) -> MarketStatus:
        # Crypto markets are always OPEN (24/7)
        # For future CFD support, check exchange status
        return MarketStatus.OPEN
    
    async def get_instruments(self) -> List[dict]:
        # Calls ccxt.load_markets() and maps to our format
        ...
```

### 3. CandleService (`shared/market-data/candle_service.py`)

```python
class CandleService:
    """Main candle collection orchestrator.
    
    Responsibilities:
    - Resolve instrument_id from exchange + symbol
    - Compute candle_data_hash (SHA-256 of OHLCV)
    - Write to tickles_shared.candles with ON DUPLICATE KEY UPDATE
    - Detect drift when existing candle hash differs from new data
    - Schedule gap detection and backfill
    - Manage retention (drop old partitions, create new ones)
    """
    
    def __init__(self, db_pool, adapters: dict[str, BaseExchangeAdapter]):
        # db_pool: shared connection pool
        # adapters: { 'bybit': CCXTAdapter('bybit'), ... }
        ...
    
    async def collect_candles(
        self,
        exchange: str,
        symbol: str,
        timeframe: str,
        since: Optional[datetime] = None,
        limit: int = 1000
    ) -> int:
        """Fetch candles from exchange and write to DB.
        
        Returns:
            Number of new candles inserted.
        
        Raises:
            ConnectionError: if DB or exchange is unreachable
            ValueError: if instrument not found in instruments table
        """
        # 1. Look up instrument_id from instruments table
        # 2. Call adapter.fetch_ohlcv()
        # 3. Compute candle_data_hash for each candle
        # 4. INSERT ... ON DUPLICATE KEY UPDATE with drift detection
        # 5. Return count of inserted/updated rows
        ...
    
    async def backfill_gaps(self, instrument_id: int, timeframe: str) -> int:
        """Detect and fill gaps in candle data.
        
        Scans for missing timestamps between expected and actual data.
        Uses gap_detector module for gap finding logic.
        """
        ...
    
    async def start_streaming(self, exchange: str, symbols: List[str]):
        """Start WebSocket streaming for real-time candles.
        
        NOTE: WebSocket streaming is deferred to Step 9 (Trading Pipeline).
        This method is a placeholder that logs a warning.
        """
        # TODO: Implement in Step 9
        ...
```

### 4. GapDetector (`shared/market-data/gap_detector.py`)

```python
class GapDetector:
    """Detects gaps in candle data and triggers backfill.
    
    For crypto (24/7 markets):
    - 1m candles: gap if >2 minutes between consecutive timestamps
    - 5m candles: gap if >10 minutes between consecutive timestamps
    - 1h candles: gap if >2 hours between consecutive timestamps
    
    For CFDs (market hours):
    - Uses timing_service to know market open/close
    - Only checks during market hours
    """
    
    def find_gaps(self, instrument_id: int, timeframe: str, 
                  start: datetime, end: datetime) -> List[Tuple[datetime, datetime]]:
        """Find gaps in the candles table for a given instrument/timeframe.
        
        Returns:
            List of (gap_start, gap_end) tuples.
        """
        ...
    
    def get_expected_timestamps(self, timeframe: str, 
                                  start: datetime, end: datetime,
                                  is_247: bool = True) -> List[datetime]:
        """Generate expected timestamp sequence for a timeframe.
        
        For 24/7 markets, generates every timestamp.
        For market-hours instruments, skips non-trading hours.
        """
        ...
```

### 5. RetentionManager (`shared/market-data/retention.py`)

```python
class RetentionManager:
    """Manages candle partition lifecycle and data retention.
    
    Retention policy (from CONTEXT_V3.md Section 14):
    - 1m candles: keep 30 days
    - 5m candles: keep 90 days
    - 15m candles: keep 6 months
    - 1h candles: keep 2 years
    - 4h+ candles: keep forever
    
    Partition management:
    - Auto-create next month's partition before month starts
    - Drop old partitions based on retention policy
    - Never drop p_future (MAXVALUE) partition
    """
    
    RETENTION_DAYS = {
        '1m': 30,
        '5m': 90,
        '15m': 180,
        '1h': 730,
        '4h': None,   # forever
        '1d': None,   # forever
    }
    
    async def ensure_partitions(self):
        """Create partitions for current month + next 2 months.
        
        Called on startup and via cron job on the 1st of each month.
        """
        ...
    
    async def drop_expired_partitions(self):
        """Drop partitions older than retention policy allows.
        
        Only drops complete months. Never drops p_future.
        Logs what was dropped for audit trail.
        """
        ...
```

### 6. TimingService (`shared/market-data/timing_service.py`)

```python
class TimingService:
    """Adaptive timing for market hours, DST, and early closes.
    
    Ported from Capital 2.0's adaptive_timing.py with these changes:
    - Reads from tickles_shared.candles instead of Capital.com format
    - Supports crypto 24/7 markets (no market close)
    - Supports CFD market hours (different per instrument type)
    - Uses instrument metadata from tickles_shared.instruments
    """
    
    def get_market_close(self, instrument_id: int, 
                          date: date) -> Optional[time]:
        """Get the actual market close time for a specific day.
        
        For crypto: returns None (24/7 market, no close)
        For CFDs: uses candle data to detect actual close (handles DST)
        """
        ...
    
    def is_market_open(self, instrument_id: int) -> bool:
        """Check if a market is currently open."""
        ...
    
    def get_trading_window(self, instrument_id: int, 
                            date: date) -> Optional[dict]:
        """Get entry/exit/calc times for a trading window.
        
        Returns dict with keys: entry_time, exit_time, calc_time, market_close
        Or None for 24/7 markets (crypto).
        """
        ...
```

### 7. BaseCollector + RSSCollector (`shared/news/`)

```python
# base.py
class BaseCollector(ABC):
    """Abstract base class for all data collectors.
    
    Ported from JarvAIs V1 collectors.py with these changes:
    - Writes to tickles_shared.news_items instead of JarvAIs schema
    - Uses content_hash for deduplication
    - Uses instrument_id FK instead of symbol strings
    - Removed AI enrichment (moved to Step 10)
    """
    
    @abstractmethod
    async def collect(self) -> List[NewsItem]:
        """Collect data from the source and return normalized items."""
        ...
    
    @abstractmethod
    def get_status(self) -> dict:
        """Get collector status for monitoring."""
        ...

@dataclass
class NewsItem:
    """Normalized news item — maps to tickles_shared.news_items"""
    source: str              # 'rss', 'telegram', 'discord', 'tradingview', 'api'
    source_detail: str       # Feed name, channel name, etc.
    title: str
    body: str
    url: str = ""
    author: str = ""
    content_hash: str = ""   # SHA-256 of (title + body) for dedup
    symbols: List[str] = field(default_factory=list)  # Related symbols
    relevance: str = "medium"  # 'critical', 'high', 'medium', 'low', 'noise'
    sentiment_label: str = ""   # 'bullish', 'bearish', 'neutral'
    sentiment_score: float = 0.0  # -1.0 to +1.0
    published_at: Optional[datetime] = None
    collected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

# rss_collector.py
class RSSCollector(BaseCollector):
    """RSS news collector — fully implemented.
    
    Ported from JarvAIs V1 NewsCollector with these changes:
    - Writes to tickles_shared.news_items
    - Uses content_hash deduplication
    - Maps detected symbols to instrument_id
    - Removed AI enrichment (moved to Step 10)
    """
    
    DEFAULT_FEEDS = [
        # Tier 1: Always available
        {"name": "CNBC World Markets", "url": "https://www.cnbc.com/id/100727362/device/rss/rss.html", ...},
        {"name": "Google News: Forex & Gold", "url": "...", ...},
        # ... (same feeds as JarvAIs V1)
    ]
    
    async def collect(self) -> List[NewsItem]:
        """Collect from all RSS feeds in parallel."""
        ...
    
    async def write_to_db(self, items: List[NewsItem], db_pool) -> int:
        """Write collected items to tickles_shared.news_items.
        
        Uses INSERT IGNORE on content_hash for deduplication.
        Returns number of new items inserted.
        """
        ...
```

### 8. DB Connection Pool (`shared/utils/db.py`)

```python
class DatabasePool:
    """Shared MySQL connection pool for all services.
    
    Uses aiomysql for async operations.
    Reads connection config from environment variables:
    - DB_HOST (default: localhost)
    - DB_PORT (default: 3306)
    - DB_USER (default: admin)
    - DB_PASSWORD (from .env)
    - DB_POOL_SIZE (default: 10)
    """
    
    _instance = None  # Singleton
    
    @classmethod
    async def get_instance(cls) -> 'DatabasePool':
        if cls._instance is None:
            cls._instance = cls()
            await cls._instance.initialize()
        return cls._instance
    
    async def initialize(self):
        """Create connection pool."""
        ...
    
    async def execute(self, query: str, params: tuple = None) -> int:
        """Execute a write query. Returns affected row count."""
        ...
    
    async def fetch_one(self, query: str, params: tuple = None) -> Optional[dict]:
        """Fetch a single row as a dict."""
        ...
    
    async def fetch_all(self, query: str, params: tuple = None) -> List[dict]:
        """Fetch all rows as list of dicts."""
        ...
    
    async def execute_many(self, query: str, params_list: List[tuple]) -> int:
        """Execute a query with multiple parameter sets. Returns total affected."""
        ...
```

---

## Database Schema Mapping

### candles table (tickles_shared)

| V2 Column | CCXT Source | Notes |
|-----------|-------------|-------|
| `instrument_id` | Look up from `instruments` table by exchange+symbol | FK to instruments |
| `timeframe` | Mapped from CCXT timeframe string ('1m', '5m', etc.) | ENUM |
| `timestamp` | `candle[0]` (ms timestamp → datetime UTC) | Partitioned by month |
| `open` | `candle[1]` | DECIMAL(20,8) |
| `high` | `candle[2]` | DECIMAL(20,8) |
| `low` | `candle[3]` | DECIMAL(20,8) |
| `close` | `candle[4]` | DECIMAL(20,8) |
| `volume` | `candle[5]` | DECIMAL(30,8) |
| `quote_volume` | Not available from CCXT OHLCV | Default 0 |
| `trades_count` | Not available from CCXT OHLCV | Default 0 |
| `candle_data_hash` | SHA-256 of `{timestamp}:{open}:{high}:{low}:{close}:{volume}` | CHAR(64) |
| `data_source` | 'api' for REST, 'websocket' for streaming, 'backfill' for gap fill | ENUM |

### news_items table (tickles_shared)

| V2 Column | RSS Source | Notes |
|-----------|-----------|-------|
| `source` | 'rss' | ENUM |
| `source_detail` | Feed name (e.g., "CNBC World Markets") | VARCHAR(200) |
| `title` | RSS entry title | VARCHAR(500) |
| `body` | RSS entry summary/description | TEXT |
| `url` | RSS entry link | VARCHAR(2000) |
| `author` | RSS entry author | VARCHAR(200) |
| `content_hash` | SHA-256 of (title + body[:500]) | CHAR(64), UNIQUE |
| `symbols` | JSON array of detected symbols | JSON |
| `relevance` | Auto-assessed from keywords | ENUM |
| `sentiment_label` | '' (empty until AI enrichment in Step 10) | ENUM |
| `sentiment_score` | 0.0 (until AI enrichment) | DECIMAL(10,6) |
| `published_at` | RSS entry published date | DATETIME(3) |
| `collected_at` | NOW() | DATETIME(3) |

---

## What Could Go Wrong

1. **CCXT rate limiting**: Bybit has rate limits (e.g., 120 requests/min for IP). The adapter must use CCXT's built-in rate limiter and implement exponential backoff on 429 errors.

2. **Partition writes**: Writing to a partitioned table requires that the partition for the timestamp's month exists. The `RetentionManager.ensure_partitions()` must run before any candle writes. If a partition is missing, the INSERT will fail.

3. **Instrument lookup**: Every candle write needs an `instrument_id`. If the instrument isn't in the `instruments` table, the write fails. We need a seed script to populate `instruments` with Bybit trading pairs.

4. **Timezone handling**: CCXT returns UTC timestamps. MySQL stores in UTC. But the partition function `TO_DAYS(timestamp)` uses the server's timezone. We must ensure MySQL is set to UTC (`SET time_zone = '+00:00'`).

5. **Connection pool exhaustion**: If multiple services share one pool, a slow query can block others. Each service should have its own pool connection limit (5 each, max 50 total).

6. **Gap detection performance**: Scanning for gaps in a 50M+ row partitioned table can be slow. Use indexed queries: `SELECT timestamp FROM candles WHERE instrument_id=? AND timeframe=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp`.

7. **RSS feed failures**: Some feeds may be temporarily down or block automated requests. The collector must handle timeouts (8s per feed) and HTTP errors gracefully, logging but not crashing.

8. **Deduplication race condition**: Two collectors writing the same candle simultaneously could cause duplicate key errors. Use `INSERT ... ON DUPLICATE KEY UPDATE` to handle this atomically.

9. **Candle data drift**: If an exchange retroactively adjusts a candle (e.g., after a trading halt), the `candle_data_hash` will differ. The service must log this as a drift event and update the row.

10. **Memory usage**: Loading 1000 candles at once is fine, but gap detection queries could return millions of rows. Always use `LIMIT` and cursor-based pagination.

---

## Implementation Order

| # | Task | Files | Priority |
|---|------|-------|----------|
| 1 | DB connection pool | `shared/utils/db.py`, `shared/utils/config.py` | P0 — blocks everything |
| 2 | BaseExchangeAdapter ABC | `shared/connectors/base.py` | P0 — interface definition |
| 3 | CCXT adapter | `shared/connectors/ccxt_adapter.py` | P0 — primary data source |
| 4 | Instrument seed script | `shared/migration/seed_instruments.py` | P0 — candle writes need instrument_id |
| 5 | CandleService (core) | `shared/market-data/candle_service.py` | P0 — main collection service |
| 6 | GapDetector | `shared/market-data/gap_detector.py` | P1 — backfill |
| 7 | RetentionManager | `shared/market-data/retention.py` | P1 — partition lifecycle |
| 8 | TimingService | `shared/market-data/timing_service.py` | P1 — market hours |
| 9 | BaseCollector ABC | `shared/news/base.py` | P0 — interface definition |
| 10 | RSSCollector | `shared/news/rss_collector.py` | P0 — news data |
| 11 | Collector stubs | `shared/news/telegram_collector.py`, `discord_collector.py`, `tradingview_monitor.py` | P2 — future |
| 12 | Capital.com adapter stub | `shared/connectors/capital_adapter.py` | P2 — future |
| 13 | Integration test | `tests/test_candle_service.py`, `tests/test_rss_collector.py` | P0 — verify data flows |
| 14 | Update CLAUDE.md | `CLAUDE.md` | P1 — documentation |

---

## Seed Instruments Script

Before candle writes work, the `instruments` table needs entries for Bybit trading pairs. The seed script should:

1. Call CCXT `load_markets()` on Bybit
2. Filter to USDT perpetual futures (most liquid)
3. Insert into `tickles_shared.instruments` with:
   - `exchange` = 'bybit'
   - `symbol` = CCXT unified symbol (e.g., 'BTC/USDT')
   - `base_currency` = 'BTC'
   - `quote_currency` = 'USDT'
   - `instrument_type` = 'perpetual_swap'
   - `is_active` = TRUE
4. Also add spot pairs for major coins (BTC, ETH, SOL)
5. Log count of instruments inserted

---

## Verification Criteria

Step 4 is complete when:

1. **Candle collection works**: `CandleService.collect_candles('bybit', 'BTC/USDT', '5m')` fetches data and writes to `tickles_shared.candles`
2. **Gap detection works**: `GapDetector.find_gaps()` identifies missing timestamps
3. **Retention works**: `RetentionManager.ensure_partitions()` creates future partitions
4. **RSS collection works**: `RSSCollector.collect()` fetches news and writes to `tickles_shared.news_items`
5. **No data duplication**: Running the same collection twice inserts 0 new rows (content_hash dedup)
6. **Drift detection works**: Writing a candle with different OHLCV values updates the row and logs a drift event
7. **Timing service works**: `TimingService.is_market_open()` returns correct status for crypto (always True) and CFDs (market hours)