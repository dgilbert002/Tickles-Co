"""
JarvAIs Signal Server
=====================
FastAPI server that receives trading signals from the UltimateMultiStrategy EA
via HTTP POST requests. This is the entry point for the entire trading pipeline.

The EA sends a JSON payload when it detects a high-conviction signal.
The server validates the payload, logs it to the database, and triggers
the cognitive engine for AI validation.

Endpoints:
    POST /signal          - Receive a new trading signal from the EA
    GET  /health          - Health check
    GET  /status          - Current system status (open trades, daily P&L, etc.)
    POST /manual-signal   - Manually inject a signal for testing
    GET  /signals/recent  - Recent signals for dashboard
    GET  /trades/open     - Currently open trades
    POST /webhook/test    - Test webhook notification
    GET  /queue/status    - Signal queue status

Features:
    - Duplicate signal detection (same symbol+direction within cooldown window)
    - Rate limiting (configurable max signals per minute)
    - Signal enrichment (attach market context before AI processing)
    - Async signal queue for burst handling
    - Graceful shutdown with queue drain
    - Webhook/notification support for vetoes and executions
"""

import logging
import asyncio
import json
import time
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from collections import deque
from contextlib import asynccontextmanager

from core.time_utils import utcnow

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel, Field, validator

logger = logging.getLogger("jarvais.signal_server")


# ─────────────────────────────────────────────────────────────────────
# Pydantic Models for Request/Response Validation
# ─────────────────────────────────────────────────────────────────────

class EASignalPayload(BaseModel):
    """
    The JSON payload sent by the EA via WebRequest.
    Every field maps to data available in the EA's signal generation logic.
    """
    # Required fields
    symbol: str = Field(..., description="Trading instrument, e.g., XAUUSD")
    direction: str = Field(..., description="BUY or SELL")
    votes_long: float = Field(0, description="Total weighted votes for LONG")
    votes_short: float = Field(0, description="Total weighted votes for SHORT")
    vote_ratio: float = Field(0, description="Ratio of winning votes to total")
    current_price: float = Field(..., description="Current market price at signal time")

    # Market context
    market_state: Optional[str] = Field(None, description="trending_up, trending_down, ranging, volatile")
    htf_trend: Optional[str] = Field(None, description="Higher timeframe trend direction")
    htf_context: Optional[str] = Field(None, description="H4 context direction")
    spread_points: Optional[float] = Field(None, description="Current spread in points")

    # Strategy details (which strategies voted and how)
    strategy_details: Optional[Dict[str, Any]] = Field(
        None,
        description="Breakdown of each strategy's vote"
    )

    # Indicator values snapshot
    indicator_values: Optional[Dict[str, Any]] = Field(
        None,
        description="Current indicator values: {rsi: 45.2, atr: 12.5, adx: 28.3, ...}"
    )

    # EA's calculated trade parameters
    ea_stop_loss: Optional[float] = Field(None, description="EA's calculated stop loss price")
    ea_take_profit: Optional[float] = Field(None, description="EA's calculated take profit price")
    ea_lot_size: Optional[float] = Field(None, description="EA's calculated lot size")

    # Account info from EA
    account_balance: Optional[float] = Field(None, description="Account balance at signal time")
    account_equity: Optional[float] = Field(None, description="Account equity at signal time")

    # Metadata
    ea_version: Optional[str] = Field(None, description="EA version string")
    timeframe: Optional[str] = Field("M5", description="Chart timeframe")
    timestamp: Optional[str] = Field(None, description="Signal timestamp from EA (ISO format)")
    magic_number: Optional[int] = Field(None, description="EA magic number for trade identification")

    @validator("direction")
    def validate_direction(cls, v):
        v = v.upper()
        if v not in ("BUY", "SELL"):
            raise ValueError("direction must be BUY or SELL")
        return v

    @validator("symbol")
    def validate_symbol(cls, v):
        return v.upper().strip()

    @validator("current_price")
    def validate_price(cls, v):
        if v <= 0:
            raise ValueError("current_price must be positive")
        return v


class SignalResponse(BaseModel):
    """Response sent back to the EA after processing a signal."""
    status: str = "received"
    signal_id: int = 0
    message: str = ""
    action: str = "pending"  # pending, approved, vetoed, rejected


class SystemStatus(BaseModel):
    """Current system status for the /status endpoint."""
    account_id: str
    is_live: bool
    uptime_seconds: int
    maturity_phase: int
    open_trades: int
    today_pnl: float
    today_signals: int
    today_trades: int
    daily_target_pct: float
    daily_target_reached: bool
    risk_mode: str
    ai_model: str
    last_signal_time: Optional[str]
    queue_depth: int = 0
    signals_per_minute: float = 0.0
    health: str  # healthy, degraded, error


class QueueStatus(BaseModel):
    """Signal queue status."""
    pending: int
    processing: int
    completed_today: int
    rejected_today: int
    avg_processing_time_ms: float


# ─────────────────────────────────────────────────────────────────────
# Rate Limiter
# ─────────────────────────────────────────────────────────────────────

class RateLimiter:
    """Simple sliding-window rate limiter for signal ingestion."""

    def __init__(self, max_per_minute: int = 30, max_per_hour: int = 200):
        self.max_per_minute = max_per_minute
        self.max_per_hour = max_per_hour
        self._timestamps: deque = deque()

    def check(self) -> tuple[bool, str]:
        """Check if a new signal is allowed. Returns (allowed, reason)."""
        now = time.time()

        # Clean old entries
        while self._timestamps and self._timestamps[0] < now - 3600:
            self._timestamps.popleft()

        # Check hourly limit
        if len(self._timestamps) >= self.max_per_hour:
            return False, f"Hourly rate limit exceeded ({self.max_per_hour}/hr)"

        # Check per-minute limit
        one_min_ago = now - 60
        recent = sum(1 for ts in self._timestamps if ts >= one_min_ago)
        if recent >= self.max_per_minute:
            return False, f"Per-minute rate limit exceeded ({self.max_per_minute}/min)"

        self._timestamps.append(now)
        return True, ""

    @property
    def signals_per_minute(self) -> float:
        """Current signals per minute rate."""
        now = time.time()
        one_min_ago = now - 60
        return sum(1 for ts in self._timestamps if ts >= one_min_ago)


# ─────────────────────────────────────────────────────────────────────
# Duplicate Detector
# ─────────────────────────────────────────────────────────────────────

class DuplicateDetector:
    """
    Detects duplicate signals within a cooldown window.
    A signal is considered duplicate if the same symbol+direction arrives
    within the cooldown period (default: 60 seconds).
    """

    def __init__(self, cooldown_seconds: int = 60):
        self.cooldown_seconds = cooldown_seconds
        self._recent: Dict[str, float] = {}  # fingerprint -> timestamp

    def _fingerprint(self, symbol: str, direction: str) -> str:
        """Generate a unique fingerprint for a signal."""
        return f"{symbol}:{direction}"

    def is_duplicate(self, symbol: str, direction: str) -> tuple[bool, Optional[float]]:
        """
        Check if this signal is a duplicate.
        Returns (is_duplicate, seconds_since_last).
        """
        now = time.time()
        fp = self._fingerprint(symbol, direction)

        # Clean expired entries
        expired = [k for k, v in self._recent.items() if now - v > self.cooldown_seconds]
        for k in expired:
            del self._recent[k]

        if fp in self._recent:
            elapsed = now - self._recent[fp]
            return True, round(elapsed, 1)

        self._recent[fp] = now
        return False, None

    def reset(self, symbol: str = None, direction: str = None):
        """Reset duplicate tracking for a specific signal or all signals."""
        if symbol and direction:
            fp = self._fingerprint(symbol, direction)
            self._recent.pop(fp, None)
        else:
            self._recent.clear()


# ─────────────────────────────────────────────────────────────────────
# Signal Enricher
# ─────────────────────────────────────────────────────────────────────

class SignalEnricher:
    """
    Enriches raw EA signals with additional market context before
    passing them to the cognitive engine.
    """

    # Pip sizes for common instruments
    PIP_SIZES = {
        "XAUUSD": 0.1, "XAGUSD": 0.01,
        "EURUSD": 0.0001, "GBPUSD": 0.0001, "AUDUSD": 0.0001,
        "NZDUSD": 0.0001, "USDCAD": 0.0001, "USDCHF": 0.0001,
        "USDJPY": 0.01, "GBPJPY": 0.01, "EURJPY": 0.01,
        "AUDJPY": 0.01, "CADJPY": 0.01,
        "US30": 1.0, "US100": 1.0, "US500": 0.1,
    }

    # Trading sessions (UTC)
    SESSIONS = {
        "sydney": (21, 6),    # 21:00 - 06:00 UTC
        "tokyo": (0, 9),      # 00:00 - 09:00 UTC
        "london": (7, 16),    # 07:00 - 16:00 UTC
        "new_york": (12, 21), # 12:00 - 21:00 UTC
    }

    @classmethod
    def enrich(cls, signal_data: Dict[str, Any]) -> Dict[str, Any]:
        """Add computed fields to the signal data."""
        enriched = dict(signal_data)

        symbol = signal_data.get("symbol", "")
        price = signal_data.get("current_price", 0)
        sl = signal_data.get("ea_stop_loss")
        tp = signal_data.get("ea_take_profit")
        direction = signal_data.get("direction", "")

        # Pip size
        pip_size = cls.PIP_SIZES.get(symbol, 0.0001)
        enriched["pip_size"] = pip_size

        # Risk-reward ratio
        if sl and tp and price and pip_size > 0:
            if direction == "BUY":
                sl_distance = abs(price - sl) / pip_size
                tp_distance = abs(tp - price) / pip_size
            else:
                sl_distance = abs(sl - price) / pip_size
                tp_distance = abs(price - tp) / pip_size

            enriched["sl_distance_pips"] = round(sl_distance, 1)
            enriched["tp_distance_pips"] = round(tp_distance, 1)
            enriched["risk_reward_ratio"] = round(tp_distance / max(sl_distance, 0.1), 2)
        else:
            enriched["sl_distance_pips"] = None
            enriched["tp_distance_pips"] = None
            enriched["risk_reward_ratio"] = None

        # Trading session
        now = utcnow()
        hour = now.hour
        active_sessions = []
        for session, (start, end) in cls.SESSIONS.items():
            if start < end:
                if start <= hour < end:
                    active_sessions.append(session)
            else:  # Wraps midnight
                if hour >= start or hour < end:
                    active_sessions.append(session)
        enriched["active_sessions"] = active_sessions
        enriched["is_session_overlap"] = len(active_sessions) > 1

        # Day of week context
        day_of_week = now.strftime("%A")
        enriched["day_of_week"] = day_of_week
        enriched["is_friday"] = day_of_week == "Friday"
        enriched["is_monday"] = day_of_week == "Monday"

        # Spread assessment
        spread = signal_data.get("spread_points", 0)
        if spread and pip_size > 0:
            spread_pips = spread * pip_size
            # High spread thresholds by instrument type
            if symbol in ("XAUUSD", "XAGUSD"):
                enriched["spread_quality"] = "tight" if spread < 20 else ("normal" if spread < 40 else "wide")
            elif "JPY" in symbol:
                enriched["spread_quality"] = "tight" if spread < 15 else ("normal" if spread < 30 else "wide")
            else:
                enriched["spread_quality"] = "tight" if spread < 10 else ("normal" if spread < 20 else "wide")
        else:
            enriched["spread_quality"] = "unknown"

        # Vote strength assessment
        votes_long = signal_data.get("votes_long", 0)
        votes_short = signal_data.get("votes_short", 0)
        total_votes = votes_long + votes_short
        if total_votes > 0:
            dominant_pct = max(votes_long, votes_short) / total_votes * 100
            enriched["vote_strength"] = "strong" if dominant_pct > 75 else ("moderate" if dominant_pct > 60 else "weak")
            enriched["vote_dominant_pct"] = round(dominant_pct, 1)
        else:
            enriched["vote_strength"] = "none"
            enriched["vote_dominant_pct"] = 0

        return enriched


# ─────────────────────────────────────────────────────────────────────
# Signal Server Class
# ─────────────────────────────────────────────────────────────────────

class SignalServer:
    """
    Manages the FastAPI application and signal processing pipeline.
    Each MT5 account instance runs its own SignalServer on a unique port.
    """

    def __init__(self, account_id: str, port: int,
                 rate_limit_per_min: int = 30,
                 duplicate_cooldown_sec: int = 60):
        self.account_id = account_id
        self.port = port
        self.start_time = utcnow()
        self._db = None
        self._cognitive_engine = None
        self._risk_manager = None
        self._memory_manager = None
        self._last_signal_time = None
        self._signal_count = 0
        self._today_rejected = 0
        self._processing_times: deque = deque(maxlen=100)
        self._shutdown_event = asyncio.Event()

        # Components
        self.rate_limiter = RateLimiter(max_per_minute=rate_limit_per_min)
        self.duplicate_detector = DuplicateDetector(cooldown_seconds=duplicate_cooldown_sec)
        self.enricher = SignalEnricher()

        # Signal processing queue
        self._signal_queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._queue_workers: List[asyncio.Task] = []
        self._num_workers = 2  # Concurrent signal processors

        # Create FastAPI app with lifespan
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            # Startup: launch queue workers
            for i in range(self._num_workers):
                task = asyncio.create_task(self._queue_worker(i))
                self._queue_workers.append(task)
            logger.info(f"[{self.account_id}] Started {self._num_workers} queue workers")
            yield
            # Shutdown: drain queue and stop workers
            await self._graceful_shutdown()

        self.app = FastAPI(
            title=f"JarvAIs Signal Server [{account_id}]",
            description="Receives trading signals from the UltimateMultiStrategy EA",
            version="5.1",
            lifespan=lifespan
        )
        self._register_routes()
        logger.info(f"[{account_id}] Signal server initialized on port {port}")

    # ── Lazy-loaded dependencies ──

    @property
    def db(self):
        """Lazy-load database manager."""
        if self._db is None:
            from db.database import get_db
            self._db = get_db()
        return self._db

    @property
    def cognitive_engine(self):
        """Lazy-load cognitive engine."""
        if self._cognitive_engine is None:
            from core.cognitive_engine import get_cognitive_engine
            self._cognitive_engine = get_cognitive_engine(self.account_id)
        return self._cognitive_engine

    @property
    def risk_manager(self):
        """Lazy-load risk manager."""
        if self._risk_manager is None:
            from core.risk_manager import get_risk_manager
            self._risk_manager = get_risk_manager(self.account_id)
        return self._risk_manager

    @property
    def memory_manager(self):
        """Lazy-load memory manager."""
        if self._memory_manager is None:
            from core.memory_manager import MemoryManager
            self._memory_manager = MemoryManager()
        return self._memory_manager

    # ── Queue Workers ──

    async def _queue_worker(self, worker_id: int):
        """Process signals from the queue sequentially."""
        logger.info(f"[{self.account_id}] Queue worker {worker_id} started")
        while not self._shutdown_event.is_set():
            try:
                # Wait for a signal with timeout (allows checking shutdown)
                try:
                    item = await asyncio.wait_for(
                        self._signal_queue.get(), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    continue

                signal_id = item["signal_id"]
                signal_data = item["signal_data"]
                is_free_trade = item["is_free_trade"]
                risk_mode = item["risk_mode"]

                start_time = time.time()
                logger.info(f"[{self.account_id}] Worker {worker_id} processing signal #{signal_id}")

                try:
                    await self._process_signal(
                        signal_id=signal_id,
                        signal_data=signal_data,
                        is_free_trade=is_free_trade,
                        risk_mode=risk_mode
                    )
                except Exception as e:
                    logger.error(f"[{self.account_id}] Worker {worker_id} error on signal #{signal_id}: {e}",
                                 exc_info=True)
                    try:
                        self.db.update_signal_status(signal_id, "error")
                    except Exception:
                        pass

                elapsed_ms = (time.time() - start_time) * 1000
                self._processing_times.append(elapsed_ms)
                self._signal_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{self.account_id}] Worker {worker_id} unexpected error: {e}", exc_info=True)
                await asyncio.sleep(1)

        logger.info(f"[{self.account_id}] Queue worker {worker_id} stopped")

    async def _graceful_shutdown(self):
        """Drain the signal queue and stop workers gracefully."""
        logger.info(f"[{self.account_id}] Initiating graceful shutdown...")
        self._shutdown_event.set()

        # Wait for queue to drain (max 8s -- must finish before launcher's
        # 10s process.wait timeout, otherwise launcher force-kills us)
        if not self._signal_queue.empty():
            logger.info(f"[{self.account_id}] Draining {self._signal_queue.qsize()} queued signals...")
            try:
                await asyncio.wait_for(self._signal_queue.join(), timeout=8.0)
            except asyncio.TimeoutError:
                logger.warning(f"[{self.account_id}] Queue drain timed out, "
                               f"{self._signal_queue.qsize()} signals abandoned")

        # Cancel workers
        for task in self._queue_workers:
            task.cancel()
        await asyncio.gather(*self._queue_workers, return_exceptions=True)
        logger.info(f"[{self.account_id}] Graceful shutdown complete")

    # ── Signal Processing ──

    async def _process_signal(self, signal_id: int, signal_data: Dict[str, Any],
                               is_free_trade: bool, risk_mode: str):
        """
        Process a signal through the full cognitive pipeline.
        Called by queue workers.
        """
        logger.info(f"[{self.account_id}] Starting AI validation for signal #{signal_id}")

        # Step 1: Retrieve relevant memories for the dossier
        symbol = signal_data.get("symbol", "")
        direction = signal_data.get("direction", "")
        market_state = signal_data.get("market_state", "")

        memories = []
        try:
            memory_query = f"{symbol} {direction} {market_state} trade lesson"
            memories = self.memory_manager.search_similar(
                query=memory_query,
                collection="trade_lessons",
                limit=5
            )
            if memories:
                logger.info(f"[{self.account_id}] Found {len(memories)} relevant memories for signal #{signal_id}")
        except Exception as e:
            logger.warning(f"[{self.account_id}] Memory search failed (non-fatal): {e}")

        # Attach memories to signal data for the cognitive engine
        signal_data["relevant_memories"] = memories

        # Step 2: Invoke the cognitive engine's full pipeline
        result = await asyncio.to_thread(
            self.cognitive_engine.process_signal,
            signal_id=signal_id,
            signal_data=signal_data,
            is_free_trade=is_free_trade,
            risk_mode=risk_mode
        )

        # Step 3: Handle the result
        if result["action"] == "execute":
            logger.info(f"[{self.account_id}] Signal #{signal_id} APPROVED "
                        f"(confidence: {result['confidence']}). Executing trade...")
            self.db.update_signal_status(signal_id, "executed")

            # Notify via webhook
            await self._send_notification(
                event="trade_approved",
                data={
                    "signal_id": signal_id,
                    "symbol": symbol,
                    "direction": direction,
                    "confidence": result["confidence"],
                    "risk_mode": risk_mode,
                }
            )

        elif result["action"] == "veto":
            logger.info(f"[{self.account_id}] Signal #{signal_id} VETOED "
                        f"(confidence: {result['confidence']}). Reason: {result.get('reason', 'N/A')}")
            self.db.update_signal_status(signal_id, "vetoed")

            # Track hypothetical outcome for vetoed signals
            self.db.update_signal_hypothetical(signal_id, {
                "hypothetical_entry": signal_data.get("current_price"),
            })

            await self._send_notification(
                event="trade_vetoed",
                data={
                    "signal_id": signal_id,
                    "symbol": symbol,
                    "direction": direction,
                    "confidence": result["confidence"],
                    "reason": result.get("reason", "N/A"),
                }
            )
        else:
            logger.warning(f"[{self.account_id}] Signal #{signal_id} unknown action: {result['action']}")

    async def _send_notification(self, event: str, data: Dict[str, Any]):
        """Send a notification via configured webhook (if any)."""
        try:
            from core.config import get_config
            config = get_config()
            webhook_url = config.raw.get("notifications", {}).get("webhook_url")
            if not webhook_url:
                return

            import aiohttp
            payload = {
                "event": event,
                "account_id": self.account_id,
                "timestamp": utcnow().isoformat(),
                "data": data
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(webhook_url, json=payload, timeout=5) as resp:
                    if resp.status != 200:
                        logger.warning(f"Webhook notification failed: HTTP {resp.status}")
        except ImportError:
            pass  # aiohttp not installed, skip
        except Exception as e:
            logger.warning(f"Webhook notification error: {e}")

    # ── Route Registration ──

    def _register_routes(self):
        """Register all API endpoints."""

        @self.app.post("/signal", response_model=SignalResponse)
        async def receive_signal(payload: EASignalPayload):
            """
            Main endpoint: receives a signal from the EA.
            Validates, enriches, logs, and queues for AI validation.
            """
            logger.info(f"[{self.account_id}] Signal received: {payload.direction} {payload.symbol} "
                        f"@ {payload.current_price} (votes: L={payload.votes_long:.2f} S={payload.votes_short:.2f})")

            try:
                # Gate 1: Rate limiting
                allowed, reason = self.rate_limiter.check()
                if not allowed:
                    logger.warning(f"[{self.account_id}] Signal rate-limited: {reason}")
                    self._today_rejected += 1
                    return SignalResponse(
                        status="rejected",
                        signal_id=0,
                        message=f"Rate limited: {reason}",
                        action="rejected"
                    )

                # Gate 2: Duplicate detection
                is_dup, elapsed = self.duplicate_detector.is_duplicate(
                    payload.symbol, payload.direction
                )
                if is_dup:
                    logger.info(f"[{self.account_id}] Duplicate signal ignored: "
                                f"{payload.direction} {payload.symbol} ({elapsed}s since last)")
                    return SignalResponse(
                        status="duplicate",
                        signal_id=0,
                        message=f"Duplicate signal (same {payload.symbol} {payload.direction} "
                                f"received {elapsed}s ago, cooldown: {self.duplicate_detector.cooldown_seconds}s)",
                        action="rejected"
                    )

                # Parse timestamp
                signal_ts = utcnow()
                if payload.timestamp:
                    try:
                        signal_ts = datetime.fromisoformat(payload.timestamp)
                    except (ValueError, TypeError):
                        pass

                # Build signal data for database
                signal_data = {
                    "account_id": self.account_id,
                    "timestamp": signal_ts,
                    "symbol": payload.symbol,
                    "direction": payload.direction,
                    "votes_long": payload.votes_long,
                    "votes_short": payload.votes_short,
                    "vote_ratio": payload.vote_ratio,
                    "market_state": payload.market_state,
                    "htf_trend": payload.htf_trend,
                    "htf_context": payload.htf_context,
                    "strategy_details": payload.strategy_details,
                    "indicator_values": payload.indicator_values,
                    "ea_stop_loss": payload.ea_stop_loss,
                    "ea_take_profit": payload.ea_take_profit,
                    "ea_lot_size": payload.ea_lot_size,
                    "current_price": payload.current_price,
                    "spread_points": payload.spread_points,
                    "account_balance": payload.account_balance,
                    "account_equity": payload.account_equity,
                    "magic_number": payload.magic_number,
                    "ea_version": payload.ea_version,
                    "timeframe": payload.timeframe,
                }

                # Step 1: Enrich the signal with computed fields
                signal_data = self.enricher.enrich(signal_data)

                # Step 2: Log the signal to the database
                signal_id = self.db.insert_signal(signal_data)
                self._last_signal_time = signal_ts
                self._signal_count += 1

                # Step 3: Pre-flight risk check (before wasting AI tokens)
                risk_check = self.risk_manager.pre_signal_check(
                    symbol=payload.symbol,
                    direction=payload.direction
                )

                if not risk_check["allowed"]:
                    self.db.update_signal_status(signal_id, "rejected_risk")
                    self._today_rejected += 1
                    logger.info(f"[{self.account_id}] Signal #{signal_id} rejected by risk manager: "
                                f"{risk_check['reason']}")
                    return SignalResponse(
                        status="rejected",
                        signal_id=signal_id,
                        message=risk_check["reason"],
                        action="rejected"
                    )

                # Step 4: Queue for AI processing
                queue_item = {
                    "signal_id": signal_id,
                    "signal_data": signal_data,
                    "is_free_trade": risk_check.get("is_free_trade", False),
                    "risk_mode": risk_check.get("risk_mode", "normal"),
                }

                try:
                    self._signal_queue.put_nowait(queue_item)
                except asyncio.QueueFull:
                    logger.warning(f"[{self.account_id}] Signal queue full, processing inline")
                    # Fallback: process inline if queue is full
                    asyncio.create_task(self._process_signal(
                        signal_id=signal_id,
                        signal_data=signal_data,
                        is_free_trade=risk_check.get("is_free_trade", False),
                        risk_mode=risk_check.get("risk_mode", "normal")
                    ))

                return SignalResponse(
                    status="received",
                    signal_id=signal_id,
                    message="Signal received, AI validation in progress",
                    action="pending"
                )

            except Exception as e:
                logger.error(f"[{self.account_id}] Error processing signal: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/health")
        async def health_check():
            """Simple health check endpoint."""
            uptime = (utcnow() - self.start_time).total_seconds()
            return {
                "status": "healthy",
                "account_id": self.account_id,
                "uptime_seconds": int(uptime),
                "signals_received": self._signal_count,
                "queue_depth": self._signal_queue.qsize(),
                "workers_active": sum(1 for t in self._queue_workers if not t.done()),
            }

        @self.app.get("/status", response_model=SystemStatus)
        async def system_status():
            """Detailed system status for the dashboard."""
            try:
                from core.config import get_config
                config = get_config()
                acct_config = config.get_account(self.account_id)

                open_trades = self.db.get_open_trades(self.account_id)
                today_pnl = self.db.get_today_pnl(self.account_id)
                today_signals = self.db.get_today_signals_count(self.account_id)
                today_trades = self.db.get_today_trade_count(self.account_id)

                risk_settings = config.get_risk_settings(self.account_id)
                daily_target = risk_settings.daily_profit_target_pct if risk_settings else 5.0

                balance = 0
                try:
                    from core.mt5_executor import get_mt5_executor
                    executor = get_mt5_executor(self.account_id)
                    balance = executor.get_account_balance()
                except Exception:
                    pass

                target_reached = False
                if balance > 0:
                    target_reached = (today_pnl / balance * 100) >= daily_target

                ai_model_config = config.get_ai_model(account_id=self.account_id)

                return SystemStatus(
                    account_id=self.account_id,
                    is_live=acct_config.is_live if acct_config else False,
                    uptime_seconds=int((utcnow() - self.start_time).total_seconds()),
                    maturity_phase=1,
                    open_trades=len(open_trades),
                    today_pnl=today_pnl,
                    today_signals=sum(today_signals.values()) if isinstance(today_signals, dict) else today_signals,
                    today_trades=today_trades,
                    daily_target_pct=daily_target,
                    daily_target_reached=target_reached,
                    risk_mode=risk_settings.post_target_mode if risk_settings else "normal",
                    ai_model=f"{ai_model_config.provider}/{ai_model_config.model}",
                    last_signal_time=self._last_signal_time.isoformat() if self._last_signal_time else None,
                    queue_depth=self._signal_queue.qsize(),
                    signals_per_minute=self.rate_limiter.signals_per_minute,
                    health="healthy"
                )
            except Exception as e:
                logger.error(f"Error getting status: {e}")
                return SystemStatus(
                    account_id=self.account_id,
                    is_live=False,
                    uptime_seconds=int((utcnow() - self.start_time).total_seconds()),
                    maturity_phase=1,
                    open_trades=0,
                    today_pnl=0,
                    today_signals=0,
                    today_trades=0,
                    daily_target_pct=5.0,
                    daily_target_reached=False,
                    risk_mode="unknown",
                    ai_model="unknown",
                    last_signal_time=None,
                    queue_depth=self._signal_queue.qsize(),
                    signals_per_minute=0,
                    health="error"
                )

        @self.app.post("/manual-signal", response_model=SignalResponse)
        async def manual_signal(payload: EASignalPayload):
            """
            Manually inject a signal for testing purposes.
            Bypasses duplicate detection but still goes through rate limiting and risk checks.
            """
            logger.info(f"[{self.account_id}] MANUAL signal injected: "
                        f"{payload.direction} {payload.symbol}")
            # Reset duplicate detector for this symbol so manual signals always go through
            self.duplicate_detector.reset(payload.symbol, payload.direction)
            return await receive_signal(payload)

        @self.app.get("/signals/recent")
        async def recent_signals(limit: int = 20):
            """Get recent signals for the dashboard."""
            signals = self.db.get_recent_signals(self.account_id, limit)
            return {"signals": signals}

        @self.app.get("/trades/open")
        async def open_trades():
            """Get currently open trades."""
            trades = self.db.get_open_trades(self.account_id)
            return {"trades": trades}

        @self.app.get("/queue/status", response_model=QueueStatus)
        async def queue_status():
            """Get signal queue status."""
            avg_time = 0.0
            if self._processing_times:
                avg_time = sum(self._processing_times) / len(self._processing_times)
            return QueueStatus(
                pending=self._signal_queue.qsize(),
                processing=sum(1 for t in self._queue_workers if not t.done()),
                completed_today=self._signal_count,
                rejected_today=self._today_rejected,
                avg_processing_time_ms=round(avg_time, 1)
            )

        @self.app.post("/webhook/test")
        async def test_webhook():
            """Test the webhook notification system."""
            await self._send_notification(
                event="test",
                data={"message": "Webhook test from JarvAIs signal server"}
            )
            return {"status": "sent"}

        @self.app.post("/duplicate-detector/reset")
        async def reset_duplicate_detector():
            """Reset the duplicate detector (useful after EA restart)."""
            self.duplicate_detector.reset()
            return {"status": "reset", "message": "Duplicate detector cleared"}

    def run(self):
        """Start the FastAPI server (blocking)."""
        import uvicorn
        logger.info(f"[{self.account_id}] Starting signal server on port {self.port}")
        uvicorn.run(
            self.app,
            host="127.0.0.1",  # Localhost only — EA and server on same machine
            port=self.port,
            log_level="warning",
            access_log=False
        )


# ─────────────────────────────────────────────────────────────────────
# Factory function
# ─────────────────────────────────────────────────────────────────────

def create_signal_server(account_id: str, port: int,
                          rate_limit_per_min: int = 30,
                          duplicate_cooldown_sec: int = 60) -> SignalServer:
    """Create a new SignalServer instance for an account."""
    return SignalServer(
        account_id=account_id,
        port=port,
        rate_limit_per_min=rate_limit_per_min,
        duplicate_cooldown_sec=duplicate_cooldown_sec
    )
