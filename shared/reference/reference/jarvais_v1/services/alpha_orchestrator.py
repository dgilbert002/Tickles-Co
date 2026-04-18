"""
JarvAIs Alpha Orchestrator
Central service that manages all data collection, AI summary generation,
and alpha delivery. Runs as a single background process.

Architecture:
- One orchestrator controls all collectors via CollectorRegistry
- Each collector has its own configurable frequency
- AI summaries are generated per timeframe and stored in DB
- Summaries are also duplicated into the news_items table as alpha items
- Prompts are loaded from prompt_versions table (editable via Prompt Engineer)
- Each timeframe prompt specifies its own AI model

The orchestrator is the CEO of the news room — it decides:
- When to collect from each source
- When to generate AI summaries
- What goes into each alpha pane

Timeframes: 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w
Each generates an AI summary at its own interval using its own prompt + model.
"""

import asyncio
import hashlib
import json
import logging
import threading
import time
from core.thread_pool import DaemonThreadPoolExecutor as ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any

logger = logging.getLogger("jarvais.alpha_orchestrator")

# All 8 timeframes matching the dashboard tabs
# Maps timeframe_minutes -> config
TIMEFRAME_CONFIGS = {
    1:     {"label": "1m",  "role": "tf_1m",  "min_interval_seconds": 60},
    5:     {"label": "5m",  "role": "tf_5m",  "min_interval_seconds": 300},
    15:    {"label": "15m", "role": "tf_15m", "min_interval_seconds": 900},
    30:    {"label": "30m", "role": "tf_30m", "min_interval_seconds": 1800},
    60:    {"label": "1h",  "role": "tf_1h",  "min_interval_seconds": 3600},
    240:   {"label": "4h",  "role": "tf_4h",  "min_interval_seconds": 14400},
    1440:  {"label": "1d",  "role": "tf_1d",  "min_interval_seconds": 86400},
    10080: {"label": "1w",  "role": "tf_1w",  "min_interval_seconds": 604800},
}

# Default user prompt template when prompt_versions has no user_prompt_template
DEFAULT_USER_PROMPT = """Analyze these {news_count} news items (timeframe: {label}):

{digest}

Provide:
1. KEY EVENTS (most impactful headlines)
2. MARKET SENTIMENT (overall direction)
3. TRADING IMPLICATIONS (what this means for XAUUSD, EURUSD, GBPUSD, NAS100)
4. RISK FACTORS (what to watch out for)"""

# Fallback system prompt when no prompt_versions row exists
FALLBACK_SYSTEM_PROMPT = """You are JarvAIs Alpha Intelligence, a trading news analyst.
Analyze the following news headlines and provide a concise trading-relevant summary.
Focus on: market-moving events, sentiment shifts, key price levels mentioned,
central bank actions, geopolitical risks, and actionable trading insights.
End with an overall market sentiment assessment (Bullish/Bearish/Neutral/Mixed)."""


class AlphaOrchestrator:
    """
    Central orchestrator for all alpha collection and AI summary generation.
    Runs as a single background service with configurable intervals.
    """

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._tick_interval = 10  # Check every 10 seconds what needs to run

        # Collector schedules: {collector_name: interval_seconds}
        self._collector_intervals: Dict[str, int] = {
            "news": 60,                # RSS feeds every 60 seconds
            "telegram": 30,            # Telegram every 30 seconds (when enabled)
            "discord": 60,             # Discord every 60 seconds (when enabled)
            "tradingview_minds": 60,   # TV Minds every 60 seconds
            "tradingview_ideas": 60,   # TV Ideas every 60 seconds
        }
        self._last_collector_run: Dict[str, float] = {}

        # Per-collector health tracking (Phase 3: health dashboard)
        self._collector_health: Dict[str, Dict] = {}
        for _cname in self._collector_intervals:
            self._collector_health[_cname] = {
                "state": "idle",        # idle | running | failed | stuck
                "last_success": 0.0,
                "last_duration_ms": 0,
                "last_error": None,
                "consecutive_failures": 0,
                "items_collected": 0,
                "total_runs": 0,
            }

        # Persistent event loop for async collector execution
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Summary schedules per timeframe (keyed by minutes)
        # Each timeframe generates an AI summary at its own interval
        self._tf_configs: Dict[int, Dict] = {}
        for tf_min, cfg in TIMEFRAME_CONFIGS.items():
            self._tf_configs[tf_min] = {
                "timeframe_minutes": tf_min,
                "label": cfg["label"],
                "role": cfg["role"],
                "interval_seconds": cfg["min_interval_seconds"],
                "last_summary": 0.0,
            }

        # Legacy pane mapping for backward compatibility
        self._pane_configs: Dict[str, Dict] = {
            "left":   {"timeframe_minutes": 1,   "last_summary": 0.0},
            "center": {"timeframe_minutes": 15,  "last_summary": 0.0},
            "right":  {"timeframe_minutes": 240, "last_summary": 0.0},
        }

        # Prompt cache: {role: {prompt_row}} — refreshed periodically
        self._prompt_cache: Dict[str, Dict] = {}
        self._prompt_cache_time: float = 0.0
        self._prompt_cache_ttl: float = 120.0  # Refresh cache every 2 minutes

        # CEO daily report scheduler
        self._ceo_report_hour_utc = 19  # 7 PM UTC = 11 PM Dubai (UTC+4)
        self._ceo_report_minute_utc = 59
        self._last_ceo_report_date: Optional[str] = None  # Track which date we last generated for

        # Proposal engine daily run (check patterns across all trades)
        self._last_proposal_scan_date: Optional[str] = None

        # Signal AI integration
        self._signal_ai = None  # Lazy-loaded
        self._signal_ai_backfill_done = False

        # Stats
        self._stats = {
            "started_at": None,
            "total_collections": 0,
            "total_summaries": 0,
            "total_news_stored": 0,
            "errors": 0,
        }

    def start(self):
        """Start the orchestrator background thread + media analyzer."""
        if self._running:
            logger.warning("Alpha Orchestrator already running")
            return
        self._running = True
        self._stats["started_at"] = datetime.now().isoformat()

        # Start the media analyzer (Whisper voice + video processing)
        try:
            from services.media_analyzer import get_media_analyzer
            get_media_analyzer().start()
        except Exception as e:
            logger.warning(f"Media analyzer start failed (non-critical): {e}")

        # Start the alpha analyzer (multi-modal alpha analysis pipeline)
        try:
            from services.alpha_analysis import get_alpha_analyzer
            get_alpha_analyzer().start()
        except Exception as e:
            logger.warning(f"Alpha analyzer start failed (non-critical): {e}")

        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="alpha-orchestrator"
        )
        self._thread.start()
        logger.info("Alpha Orchestrator started")

    def stop(self):
        """Stop the orchestrator and clean up the event loop."""
        self._running = False
        if hasattr(self, "_sync_pool"):
            self._sync_pool.shutdown(wait=False)
        if self._loop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=15)
        if self._loop and not self._loop.is_closed():
            self._loop.close()
        self._loop = None
        logger.info("Alpha Orchestrator stopped")

    def update_pane_timeframe(self, pane_id: str, timeframe_minutes: int):
        """Update a pane's timeframe and persist it (legacy compatibility)."""
        if pane_id in self._pane_configs:
            self._pane_configs[pane_id]["timeframe_minutes"] = timeframe_minutes
            try:
                from db.database import get_db
                db = get_db()
                key = f"alpha_tf_{pane_id}"
                db.execute(
                    """INSERT INTO config (account_id, config_key, config_value, updated_by)
                       VALUES ('global', %s, %s, 'system')
                       ON DUPLICATE KEY UPDATE config_value=%s, updated_at=NOW()""",
                    (key, str(timeframe_minutes), str(timeframe_minutes))
                )
            except Exception as e:
                logger.error(f"Failed to save preference: {e}")

    # ─────────────────────────────────────────────────────────────────
    # Prompt Loading from prompt_versions
    # ─────────────────────────────────────────────────────────────────

    def _refresh_prompt_cache(self):
        """Load active prompts from prompt_versions table into cache."""
        now = time.time()
        if now - self._prompt_cache_time < self._prompt_cache_ttl:
            return  # Cache is still fresh

        try:
            from db.database import get_db
            db = get_db()
            rows = db.fetch_all(
                """SELECT id, role, category, system_prompt, user_prompt_template,
                          model, model_provider, prompt_name
                   FROM prompt_versions
                   WHERE is_active = 1
                   ORDER BY role, version DESC"""
            )
            new_cache = {}
            for row in rows:
                role = row.get("role", "")
                if role and role not in new_cache:
                    new_cache[role] = row
            self._prompt_cache = new_cache
            self._prompt_cache_time = now
            logger.debug(f"Prompt cache refreshed: {len(new_cache)} active prompts loaded")
        except Exception as e:
            logger.error(f"Failed to refresh prompt cache: {e}")

    def _get_prompt_for_tf(self, tf_minutes: int) -> Dict:
        """
        Get the active prompt for a timeframe from the cache.
        Returns a dict with: system_prompt, user_prompt_template, model, model_provider, prompt_version_id
        Falls back to hardcoded defaults if no prompt exists.
        """
        self._refresh_prompt_cache()

        config = self._tf_configs.get(tf_minutes, {})
        role = config.get("role", f"tf_{config.get('label', '')}")

        cached = self._prompt_cache.get(role)
        if cached:
            return {
                "system_prompt": cached.get("system_prompt", FALLBACK_SYSTEM_PROMPT),
                "user_prompt_template": cached.get("user_prompt_template") or DEFAULT_USER_PROMPT,
                "model": cached.get("model"),
                "model_provider": cached.get("model_provider"),
                "prompt_version_id": cached.get("id"),
                "prompt_name": cached.get("prompt_name", ""),
            }

        # No prompt in DB — use fallback
        label = config.get("label", f"{tf_minutes}m")
        return {
            "system_prompt": FALLBACK_SYSTEM_PROMPT,
            "user_prompt_template": DEFAULT_USER_PROMPT,
            "model": None,
            "model_provider": None,
            "prompt_version_id": None,
            "prompt_name": f"Fallback ({label})",
        }

    # ─────────────────────────────────────────────────────────────────
    # Main Loop
    # ─────────────────────────────────────────────────────────────────

    def _run_loop(self):
        """Main orchestrator loop — async-parallel collectors, 10-second tick.

        Architecture (Phase 4 async refactor):
        - Creates a persistent asyncio event loop for this thread
        - Collectors that are due run in PARALLEL via asyncio.gather()
        - Summaries, Signal AI, CEO report, proposals stay sync (periodic tasks)
        - Wrapped in top-level try/except to prevent thread death
        """
        # Create a persistent event loop for this thread
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            # Run initial collection (all collectors in parallel)
            try:
                all_names = list(self._collector_intervals.keys())
                logger.info(f"Initial parallel collection: {all_names}")
                self._loop.run_until_complete(self._async_run_collectors(all_names))
            except Exception as e:
                logger.error(f"Initial collection failed: {e}")

            # Generate initial summaries for all timeframes
            try:
                self._generate_all_summaries()
            except Exception as e:
                logger.error(f"Initial summary generation failed: {e}")

            consecutive_errors = 0
            while self._running:
                try:
                    time.sleep(self._tick_interval)
                    if not self._running:
                        break

                    now = time.time()

                    # Gather collectors that are due to run
                    due = [
                        name for name, interval in self._collector_intervals.items()
                        if now - self._last_collector_run.get(name, 0) >= interval
                    ]

                    # Run all due collectors in PARALLEL
                    if due:
                        self._loop.run_until_complete(self._async_run_collectors(due))

                    # Phase 5: offload heavy sync tasks to a worker pool
                    # so the main loop stays responsive for collector ticks.
                    _sync_tasks = []

                    for tf_min, config in self._tf_configs.items():
                        interval = config["interval_seconds"]
                        last = config["last_summary"]
                        if now - last >= interval:
                            config["last_summary"] = now  # optimistic update to prevent duplicate dispatch
                            _sync_tasks.append(
                                ("summary", tf_min, self._generate_summary_for_tf, (tf_min,)))

                    _sync_tasks.append(
                        ("signal_ai", None, self._run_signal_ai, ()))
                    _sync_tasks.append(
                        ("ceo_report", None, self._check_ceo_daily_report, ()))
                    _sync_tasks.append(
                        ("proposal_scan", None, self._check_daily_proposal_scan, ()))

                    if not hasattr(self, "_sync_pool"):
                        self._sync_pool = ThreadPoolExecutor(
                            max_workers=2, thread_name_prefix="orch-sync")

                    _sync_futures = {}
                    for label, detail, fn, args in _sync_tasks:
                        _sync_futures[self._sync_pool.submit(fn, *args)] = (label, detail)

                    for fut in _sync_futures:
                        try:
                            fut.result(timeout=120)
                        except Exception as e:
                            label, detail = _sync_futures[fut]
                            tag = f"{label}({detail})" if detail else label
                            logger.error(f"{tag} error: {e}")
                            self._stats["errors"] += 1

                    consecutive_errors = 0  # Reset on successful tick

                except Exception as e:
                    consecutive_errors += 1
                    logger.error(f"Orchestrator tick error #{consecutive_errors}: {e}")
                    if consecutive_errors > 10:
                        logger.critical("Too many consecutive errors, sleeping 60s")
                        time.sleep(60)
                        consecutive_errors = 0
                    else:
                        time.sleep(5)  # Brief pause before retry

        except Exception as e:
            logger.critical(f"Orchestrator loop FATAL error: {e}")
            time.sleep(30)
            if self._running:
                logger.info("Attempting orchestrator loop restart...")
                self._run_loop()  # Recursive restart
        finally:
            if self._loop and not self._loop.is_closed():
                self._loop.close()
                self._loop = None

    # ─────────────────────────────────────────────────────────────────
    # Collectors
    # ─────────────────────────────────────────────────────────────────

    def _run_collectors(self):
        """Run all enabled collectors in parallel via the async event loop."""
        if self._loop and not self._loop.is_closed():
            self._loop.run_until_complete(
                self._async_run_collectors(list(self._collector_intervals.keys()))
            )
        else:
            for name in self._collector_intervals:
                self._run_collector(name)
        self._stats["total_collections"] += 1

    # ─────────────────────────────────────────────────────────────────
    # Async Parallel Collection (Phase 4)
    # ─────────────────────────────────────────────────────────────────

    async def _async_run_collectors(self, names: list):
        """Run multiple collectors in PARALLEL via asyncio.gather.
        Each collector runs concurrently — Discord no longer blocks Telegram, etc."""
        tasks = [self._async_run_collector(name) for name in names]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                logger.error(f"Collector '{name}' async error: {result}")
                self._stats["errors"] += 1
                health = self._collector_health.get(name, {})
                health["state"] = "failed"
                health["last_error"] = str(result)
                health["consecutive_failures"] = health.get("consecutive_failures", 0) + 1

    async def _async_run_collector(self, name: str):
        """Run a single collector asynchronously with health tracking.
        Called by _async_run_collectors via gather — multiple instances run at once."""
        health = self._collector_health.get(name, {})
        health["state"] = "running"
        start_time = time.time()

        try:
            from data.collectors import get_collector_registry
            registry = get_collector_registry()
            collector = registry.get_collector(name)

            if not collector or not collector.enabled:
                health["state"] = "idle"
                return

            # Timeout per collector: 3x their interval, max 180s
            max_timeout = min(self._collector_intervals.get(name, 60) * 3, 180)
            items = await asyncio.wait_for(collector.collect(), timeout=max_timeout)

            # Process items synchronously (DB inserts are fast, queues are non-blocking)
            self._process_collected_items(name, items)

            # Update health on success
            elapsed_ms = int((time.time() - start_time) * 1000)
            health["state"] = "idle"
            health["last_success"] = time.time()
            health["last_duration_ms"] = elapsed_ms
            health["consecutive_failures"] = 0
            health["items_collected"] = health.get("items_collected", 0) + len(items or [])
            health["total_runs"] = health.get("total_runs", 0) + 1
            logger.debug(f"Collector '{name}' completed in {elapsed_ms}ms ({len(items or [])} items)")

        except asyncio.TimeoutError:
            elapsed_ms = int((time.time() - start_time) * 1000)
            health["state"] = "stuck"
            health["last_error"] = f"Timed out after {elapsed_ms}ms"
            health["consecutive_failures"] = health.get("consecutive_failures", 0) + 1
            logger.warning(f"Collector '{name}' timed out after {elapsed_ms}ms")
            self._stats["errors"] += 1

        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            health["state"] = "failed"
            health["last_error"] = str(e)[:200]
            health["consecutive_failures"] = health.get("consecutive_failures", 0) + 1
            logger.error(f"Collector '{name}' failed after {elapsed_ms}ms: {e}")
            self._stats["errors"] += 1

    def _run_collector(self, name: str):
        """Run a specific collector (sync fallback — used only if no event loop).
        The primary path is _async_run_collector via _async_run_collectors."""
        try:
            from data.collectors import get_collector_registry
            from db.database import get_db

            db = get_db()
            registry = get_collector_registry()
            collector = registry.get_collector(name)

            if not collector or not collector.enabled:
                return

            loop = asyncio.new_event_loop()
            try:
                items = loop.run_until_complete(collector.collect())
            finally:
                loop.close()

            self._process_collected_items(name, items)

        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"Collector '{name}' error: {e}")

    def _process_collected_items(self, name: str, items):
        """Process and store collected items to DB, queue for AI analysis.
        Extracted from _run_collector so both sync and async paths share this logic."""
        try:
            from db.database import get_db
            db = get_db()

            # Phase 1e: cache per-item lookups at batch start
            _alpha_enabled_cache = True
            try:
                _cfg = db.fetch_one(
                    "SELECT config_value FROM config "
                    "WHERE config_key = 'alpha_analysis_enabled'")
                if _cfg and _cfg.get("config_value", "true").lower() in (
                        "false", "0", "no", "off"):
                    _alpha_enabled_cache = False
            except Exception:
                pass

            _alpha_prefs_cache = {}
            try:
                _prefs = db.fetch_all(
                    "SELECT source, source_detail, author "
                    "FROM user_follow_preferences WHERE track_alpha = 1")
                for _p in (_prefs or []):
                    key = (_p["source"] or "", _p.get("source_detail") or "",
                           _p["author"] or "")
                    _alpha_prefs_cache[key] = True
            except Exception:
                pass

            _mentor_names_cache = set()
            try:
                _m_rows = db.fetch_all("""
                    SELECT LOWER(upl.source_username) AS n
                    FROM user_profile_links upl
                    JOIN user_profiles up ON upl.user_profile_id = up.id
                    WHERE up.is_mentor = 1
                """)
                _mentor_names_cache = {r["n"] for r in (_m_rows or []) if r.get("n")}
            except Exception:
                pass

            new_count = 0
            skipped_stale = 0
            # ── 7-DAY MAX AGE GATE ──────────────────────────────────
            # On first run (virgin DB) we only want 7 days of data.
            # On subsequent runs, incremental mode handles freshness.
            # This gate applies universally to ALL sources.
            MAX_AGE_DAYS = 7
            age_cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

            for item in (items or []):
                title = getattr(item, 'title', '') or ''
                body = getattr(item, 'body', '') or getattr(item, 'summary', '') or ''
                url = getattr(item, 'url', '') or ''
                source = getattr(item, 'source', 'unknown') or 'unknown'
                source_detail = getattr(item, 'source_detail', '') or ''
                author = getattr(item, 'author', '') or ''
                symbols = getattr(item, 'symbols', []) or []
                sentiment_score = getattr(item, 'sentiment_score', 0.0) or 0.0
                raw_data = getattr(item, 'raw_data', '') or ''

                # ── Drop items older than MAX_AGE_DAYS ──
                item_ts = getattr(item, 'timestamp', None)
                if item_ts:
                    try:
                        if isinstance(item_ts, str):
                            # Handle ISO format with or without T separator
                            ts_clean = item_ts.replace('T', ' ')[:19]
                            item_dt = datetime.strptime(ts_clean, '%Y-%m-%d %H:%M:%S')
                        elif isinstance(item_ts, datetime):
                            item_dt = item_ts
                        else:
                            item_dt = None
                        if item_dt and item_dt < age_cutoff:
                            skipped_stale += 1
                            continue  # Skip stale item
                    except (ValueError, TypeError):
                        pass  # Can't parse date — allow it through

                if sentiment_score > 0.2:
                    sentiment = 'bullish'
                elif sentiment_score < -0.2:
                    sentiment = 'bearish'
                else:
                    sentiment = 'neutral'

                category = self._classify_category(item)
                relevance = getattr(item, 'relevance', 'low') or 'low'

                # Build hash_key matching store_news_item's SHA256 logic
                ext_id = ''  # Will be set below for TradingView/Telegram
                _hash_title = title[:500] if title else ''
                _hash_url = url[:500] if url else ''

                db_item = {
                    "source": source,
                    "headline": title[:500] if title else "No title",
                    "detail": body,
                    "url": url,
                    "category": category,
                    "relevance": getattr(item, 'relevance', 'low') or 'low',
                    "sentiment": sentiment,
                    "symbols": symbols if isinstance(symbols, list) else [],
                    "published_at": getattr(item, 'timestamp', None),
                    "author": author if author else None,
                    "source_detail": source_detail if source_detail else None,
                }

                # Add TradingView-specific fields
                if source == 'tradingview' and raw_data:
                    try:
                        raw = json.loads(raw_data)
                        db_item["author_badge"] = raw.get('badge_label', '')
                        db_item["external_id"] = raw.get('id', '') or raw.get('idea_id', '')
                        ext_id = db_item["external_id"]
                        db_item["boosts"] = raw.get('likes', 0) or raw.get('boosts', 0)
                        db_item["comments_count"] = raw.get('comments', 0)
                        if source_detail == 'ideas':
                            db_item["chart_image_url"] = raw.get('chart_image_big', '')
                            db_item["ai_analysis"] = raw.get('ai_analysis', '')
                            db_item["direction"] = raw.get('direction', '')
                            db_item["tv_timeframe"] = raw.get('chart_timeframe', '')
                        elif source_detail == 'minds':
                            db_item["direction"] = raw.get('direction', '')
                    except (json.JSONDecodeError, KeyError):
                        pass

                # Add Telegram-specific fields
                if source == 'telegram' and raw_data:
                    try:
                        raw = json.loads(raw_data)
                        db_item["external_id"] = f"tg_{raw.get('channel_id', '')}_{raw.get('first_msg_id', '')}"
                        ext_id = db_item["external_id"]
                        # Detect direction from signals if present
                        signals = raw.get('signals', [])
                        if signals:
                            for sig in signals:
                                direction = sig.get('direction', '')
                                if direction:
                                    db_item["direction"] = direction
                                    break
                        # Store media file paths for downloaded media
                        media_items = raw.get('media', [])
                        downloaded_media = [m for m in media_items if m.get('path')]
                        if downloaded_media:
                            # Store first media path and type
                            db_item["media_url"] = downloaded_media[0].get('path', '')
                            db_item["media_type"] = downloaded_media[0].get('type', '')
                    except (json.JSONDecodeError, KeyError):
                        pass

                # Add Discord-specific fields
                if source == 'discord' and raw_data:
                    try:
                        raw = json.loads(raw_data)
                        db_item["external_id"] = f"dc_{raw.get('channel_id', '')}_{raw.get('first_msg_id', '')}_{raw.get('last_msg_id', '')}"
                        ext_id = db_item["external_id"]
                        # Detect direction from signals if present
                        signals = raw.get('signals', [])
                        if signals:
                            for sig in signals:
                                direction = sig.get('direction', '')
                                if direction:
                                    db_item["direction"] = direction
                                    break
                        # Store chart image URL (TV charts, embed images, attachment images)
                        chart_img = raw.get('chart_image_url', '')
                        if chart_img:
                            db_item["chart_image_url"] = chart_img
                        # Store media file paths for downloaded media
                        media_items = raw.get('media', [])
                        downloaded_media = [m for m in media_items if m.get('path')]
                        if downloaded_media:
                            db_item["media_url"] = downloaded_media[0].get('path', '')
                            db_item["media_type"] = downloaded_media[0].get('type', '')
                        # Fallback: if no downloaded media but we have image URLs, store the first one
                        elif not chart_img:
                            img_urls = raw.get('image_urls', [])
                            if img_urls:
                                db_item["chart_image_url"] = img_urls[0]
                    except (json.JSONDecodeError, KeyError):
                        pass

                # Build the SAME SHA256 hash that store_news_item() uses
                # so we can look up the stored ID after insertion
                if ext_id:
                    _hash_input = f"{ext_id}:{_hash_title}:{_hash_url}"
                elif source == 'news':
                    # News RSS: hash on title + source_detail only (not URL)
                    # Google News URLs change on every fetch for the same article
                    _hash_input = f"{_hash_title}:{source_detail or ''}"
                else:
                    _hash_input = f"{_hash_title}:{_hash_url}"
                hash_key = hashlib.sha256(_hash_input.encode()).hexdigest()[:64]

                if db.store_news_item(db_item):
                    new_count += 1

                    # Source-agnostic: get the stored news_item_id immediately after INSERT
                    _stored_id = None
                    try:
                        _stored_row = db.fetch_one(
                            "SELECT id FROM news_items WHERE hash_key = %s", (hash_key,))
                        if _stored_row:
                            _stored_id = _stored_row['id']
                        elif hash_key:
                            _stored_row = db.fetch_one(
                                "SELECT id FROM news_items WHERE hash_key = %s", (hash_key,))
                            if _stored_row:
                                _stored_id = _stored_row['id']
                    except Exception:
                        pass

                    # Register new ideas for tracking
                    if source == 'tradingview' and source_detail == 'ideas':
                        try:
                            from services.idea_monitor import get_idea_monitor
                            monitor = get_idea_monitor()
                            monitor.register_new_idea(db_item)
                        except Exception as e:
                            logger.debug(f"Idea registration: {e}")

                    # Queue media analysis for items with downloaded media files
                    if source in ('telegram', 'discord') and raw_data and _stored_id:
                        try:
                            raw_parsed = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
                            media_list = raw_parsed.get('media', [])
                            has_downloaded = any(m.get('path') for m in media_list)
                            if has_downloaded:
                                from services.media_analyzer import get_media_analyzer
                                analyzer = get_media_analyzer()
                                analyzer.queue_item(
                                    news_item_id=_stored_id,
                                    media_list=media_list,
                                    context={
                                        "source": source,
                                        "source_detail": source_detail,
                                        "author": author,
                                        "headline": title[:200] if title else "",
                                        "news_item_id": _stored_id,
                                    }
                                )
                        except Exception as e:
                            logger.debug(f"Media analysis queue: {e}")

                    # Detect video URLs in message body and queue for yt-dlp download
                    # Gated by media_process_video system config toggle
                    _video_enabled = True
                    try:
                        _vr = db.fetch_one("SELECT config_value FROM system_config WHERE config_key = 'media_process_video'")
                        if _vr and _vr.get("config_value") == "false":
                            _video_enabled = False
                    except Exception:
                        pass
                    if _video_enabled and body and source in ('telegram', 'discord', 'twitter'):
                        try:
                            from services.media_analyzer import get_media_analyzer
                            analyzer = get_media_analyzer()
                            video_urls = analyzer.detect_video_urls(body)
                            if video_urls:
                                stored_for_video = db.fetch_one(
                                    "SELECT id FROM news_items WHERE hash_key = %s",
                                    (hash_key,)
                                )
                                vid_item_id = stored_for_video['id'] if stored_for_video else None
                                full_msg = ((title or "") + "\n" + (body or ""))[:500]
                                for vurl in video_urls[:2]:
                                    analyzer.queue_video_url(
                                        video_url=vurl,
                                        news_item_id=vid_item_id,
                                        context={
                                            "source": source,
                                            "source_detail": source_detail,
                                            "author": author,
                                            "headline": full_msg,
                                            "news_item_id": vid_item_id,
                                        }
                                    )
                        except Exception as e:
                            logger.debug(f"Video URL detection: {e}")

                    # Phase 1e: use cached lookups instead of per-item DB queries
                    is_alpha_tracked = False
                    try:
                        if source == "tradingview" and source_detail == "ideas":
                            is_alpha_tracked = True
                        else:
                            _key_exact = (source, source_detail, author)
                            _key_no_det = (source, "", author)
                            _key_no_auth = (source, source_detail, "")
                            _key_bare = (source, "", "")
                            is_alpha_tracked = (_key_exact in _alpha_prefs_cache
                                                or _key_no_det in _alpha_prefs_cache
                                                or _key_no_auth in _alpha_prefs_cache
                                                or _key_bare in _alpha_prefs_cache)
                    except Exception:
                        pass

                    if not is_alpha_tracked and author:
                        try:
                            if author.strip().lower() in _mentor_names_cache:
                                is_alpha_tracked = True
                                logger.info(
                                    f"[MentorMode] Forced alpha-track for "
                                        f"mentor {author} ({source}/{source_detail})")
                        except Exception:
                            pass

                    # ── Signal AI: text-only signal detection for NON-ALPHA feed items ──
                    # Alpha items are handled by Alpha Analysis v2.0 (avoids duplicate AI calls)
                    if not is_alpha_tracked and _stored_id:
                        try:
                            signal_ai = self._get_signal_ai()
                            if signal_ai:
                                queue_item = dict(db_item)
                                queue_item['id'] = _stored_id
                                signal_ai.queue_for_parsing(queue_item)
                        except Exception as e:
                            logger.debug(f"Signal AI queue: {e}")

                    # ── ALPHA ANALYSIS PIPELINE (Phase 1: Trigger and Routing) ──
                    # Alpha-tracked items get the full multi-modal v2.0 analysis
                    # (text + images + voice + video + dual human/JarvAIs trades)
                    try:
                        alpha_enabled = _alpha_enabled_cache

                        if not alpha_enabled:
                            continue  # Skip alpha analysis

                        # Check if item has media (image, voice, or video)
                        has_media = False
                        media_types = []
                        if raw_data:
                            try:
                                raw_parsed = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
                                media_list = raw_parsed.get("media", [])
                                for m in media_list:
                                    m_type = m.get("type", "")
                                    if m_type in ("photo", "voice", "video", "video_note"):
                                        has_media = True
                                        media_types.append(m_type)
                                # Also check for chart/image URLs (TV ideas use chart_image_big)
                                if (raw_parsed.get("chart_image_url") or raw_parsed.get("chart_image_big")
                                        or raw_parsed.get("image_urls")):
                                    has_media = True
                                    if "photo" not in media_types:
                                        media_types.append("photo")
                                # Check for video URLs in body
                                if body and any(x in body.lower() for x in ["youtube.com", "youtu.be", "vimeo.com"]):
                                    has_media = True
                                    if "video" not in media_types:
                                        media_types.append("video")
                            except Exception:
                                pass

                        # v2.0: Analyze ALL alpha-tracked items (source-agnostic, uses cached _stored_id)
                        if is_alpha_tracked and _stored_id:
                            from services.alpha_analysis import get_alpha_analyzer
                            analyzer = get_alpha_analyzer()
                            analyzer.queue_item(
                                news_item_id=_stored_id,
                                source=source,
                                source_detail=source_detail,
                                author=author,
                                headline=title[:200] if title else "",
                                body=body,
                                media_types=media_types,
                                raw_data=raw_data,
                            )
                            logger.info(f"[Alpha v2.0] Queued item {_stored_id} from {source}/{source_detail} | media: {media_types or ['text-only']}")
                        elif is_alpha_tracked and not _stored_id:
                            logger.warning(f"[Alpha v2.0] Skipped item (no stored ID): {source}/{source_detail} hash={hash_key[:12]}")
                    except Exception as e:
                        logger.debug(f"Alpha Analysis queue: {e}")

            self._last_collector_run[name] = time.time()
            self._stats["total_news_stored"] += new_count

            if new_count > 0 or skipped_stale > 0:
                msg = f"Collector '{name}': {new_count} new items"
                if skipped_stale > 0:
                    msg += f" ({skipped_stale} stale items dropped, >{MAX_AGE_DAYS}d old)"
                logger.info(msg)

        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"_process_collected_items '{name}' error: {e}")

    # ─────────────────────────────────────────────────────────────────
    # Per-Item AI Chart/Image Analysis (uses Prompt Engineer per source)
    # ─────────────────────────────────────────────────────────────────

    def _get_model_for_source(self, source: str, media_type: str, db=None) -> dict:
        """
        Look up the AI model to use for a given source + media_type from the
        source_model_assignments table. Falls back to the Prompt Engineer config
        for the matching role, then to gpt-4.1-mini as a last resort.

        Returns dict: {model_id, model_provider, is_enabled}
        """
        if db is None:
            from db.database import get_db
            db = get_db()

        try:
            row = db.fetch_one(
                "SELECT model_id, model_provider, is_enabled FROM source_model_assignments "
                "WHERE source = %s AND media_type = %s",
                (source, media_type)
            )
            if row and row.get("model_id"):
                return {
                    "model_id": row["model_id"],
                    "model_provider": row["model_provider"],
                    "is_enabled": bool(row.get("is_enabled", 1)),
                }
        except Exception as e:
            logger.debug(f"[ModelLookup] source_model_assignments query error: {e}")

        # Fallback: derive PE role from source name
        pe_role = f"source_{source.split('_')[0]}"  # e.g. 'discord' -> 'source_discord'
        try:
            pe_row = db.fetch_one(
                "SELECT model, model_provider FROM prompt_versions "
                "WHERE role = %s AND is_active = 1 ORDER BY version DESC LIMIT 1",
                (pe_role,)
            )
            if pe_row and pe_row.get("model"):
                return {
                    "model_id": pe_row["model"],
                    "model_provider": pe_row["model_provider"] or "openrouter",
                    "is_enabled": True,
                }
        except Exception:
            pass

        return {"model_id": "openai/gpt-4.1-mini", "model_provider": "openrouter", "is_enabled": True}

    async def _analyze_source_images(self, items: list, pe_role: str) -> list:
        """
        For items that contain chart images, run AI vision analysis using
        the model from source_model_assignments (image column) with prompts
        from the Prompt Engineer.

        Args:
            items: List of StandardizedItem objects
            pe_role: Prompt Engineer role to load (e.g. 'source_discord', 'source_telegram')

        Returns:
            Updated items list with ai_analysis injected into raw_data.
        """
        import json as json_mod
        from db.database import get_db
        from core.model_interface import get_model_interface

        db = get_db()
        mi = get_model_interface()

        # Derive source name from pe_role (e.g. 'source_discord' -> 'discord')
        source_name = pe_role.replace("source_", "")

        # Get model from source_model_assignments (image column)
        model_cfg = self._get_model_for_source(source_name, "image", db)
        if not model_cfg.get("is_enabled"):
            logger.debug(f"[SourceImageAI] Image analysis disabled for source '{source_name}'")
            return items

        model_id = model_cfg["model_id"]
        provider = model_cfg["model_provider"]

        # Load prompt config from Prompt Engineer (for system_prompt + user template)
        row = db.fetch_one(
            "SELECT system_prompt, user_prompt_template "
            "FROM prompt_versions WHERE role = %s AND is_active = 1 "
            "ORDER BY version DESC LIMIT 1",
            (pe_role,)
        )

        system_prompt = (row.get("system_prompt") if row else "") or ""
        user_template = (row.get("user_prompt_template") if row else "") or ""

        analyzed_count = 0
        for item in items:
            try:
                raw = json_mod.loads(item.raw_data) if item.raw_data else {}
            except (json_mod.JSONDecodeError, AttributeError):
                continue

            # Find chart image URL from the item's raw data
            chart_url = raw.get("chart_image_url") or ""
            if not chart_url:
                # Check image_urls list
                img_list = raw.get("image_urls") or []
                if img_list:
                    chart_url = img_list[0]
            if not chart_url:
                continue  # No image to analyze

            # Skip if already analyzed
            if raw.get("ai_chart_analysis"):
                continue

            # Build user prompt from template or use raw text as context
            item_text = (getattr(item, 'body', '') or getattr(item, 'title', '') or '')[:2000]
            symbols_str = ", ".join(item.symbols) if item.symbols else "unknown"
            author = getattr(item, 'author', '') or 'unknown'

            template_vars = {
                "text": item_text,
                "symbols": symbols_str,
                "author": author,
                "source": getattr(item, 'source', ''),
                "source_detail": getattr(item, 'source_detail', ''),
            }

            if user_template:
                try:
                    user_prompt = user_template.format(**template_vars)
                except KeyError:
                    # Template has vars we don't have — just use the raw text
                    user_prompt = f"Analyze this chart image. Context from {author}:\n{item_text}"
            else:
                user_prompt = f"Analyze this chart image. Context from @{author}:\n\n{item_text}"

            # Build vision content
            user_content = [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": chart_url, "detail": "high"}},
            ]

            try:
                result = mi.query_with_model(
                    model_id=model_id,
                    provider=provider,
                    role=pe_role.replace("source_", "") + "_chart",
                    system_prompt=system_prompt,
                    user_prompt=user_content,
                    max_tokens=1000,
                    temperature=0.3,
                    context=f"{pe_role}_chart_analysis",
                    source=source_name,
                    source_detail=getattr(item, 'source_detail', None),
                    author=author,
                    news_item_id=getattr(item, 'db_id', None),
                    media_type="image", duo_id=None,
                )
                if result.success and result.content:
                    raw["ai_chart_analysis"] = result.content
                    raw["ai_chart_model"] = model_id
                    item.raw_data = json_mod.dumps(raw)
                    analyzed_count += 1
                    logger.info(f"[SourceImageAI] {pe_role} chart analysis for {item.id[:20]} "
                                f"({len(result.content)} chars, ${result.cost_usd:.4f})")
                else:
                    logger.warning(f"[SourceImageAI] AI failed for {item.id[:20]}: {result.error_message}")
            except Exception as ai_err:
                logger.warning(f"[SourceImageAI] Error analyzing {item.id[:20]}: {ai_err}")

        if analyzed_count > 0:
            logger.info(f"[SourceImageAI] {pe_role}: analyzed {analyzed_count} chart images with {model_id}")
        return items

    # ─────────────────────────────────────────────────────────────────
    # AI Summary Generation (loads prompts from prompt_versions)
    # ─────────────────────────────────────────────────────────────────

    def _generate_all_summaries(self):
        """Generate AI summaries for all timeframes."""
        for tf_min in self._tf_configs:
            self._generate_summary_for_tf(tf_min)

    def _ai_submit_tree_to_exclusions(self, tree: dict):
        """
        Convert AI Submission tree (same shape as display filter) into exclusion lists.
        Unchecked nodes (false) are excluded; only checked sources/subsources/authors go to AI.
        Returns (exclude_sources, exclude_details, exclude_authors).
        """
        exclude_sources = []
        exclude_details = []   # list of (source, detail)
        exclude_authors = []   # list of (source, detail, author)
        if not isinstance(tree, dict):
            return exclude_sources, exclude_details, exclude_authors
        for source, source_val in tree.items():
            if source == "__all" or not isinstance(source_val, dict):
                continue
            if source_val.get("__all") is False:
                exclude_sources.append(source)
                continue
            for detail, detail_val in source_val.items():
                if detail == "__all" or not isinstance(detail_val, dict):
                    continue
                if detail_val.get("__all") is False:
                    exclude_details.append((source, detail))
                    continue
                for author, checked in detail_val.items():
                    if author == "__all":
                        continue
                    if checked is False:
                        exclude_authors.append((source, detail, author))
        return exclude_sources, exclude_details, exclude_authors

    def _generate_summary_for_tf(self, tf_minutes: int):
        """
        Generate an AI summary for a specific timeframe and store it.
        
        Loads the prompt from prompt_versions table so that changes made
        in the Prompt Engineer tab are immediately reflected in the next
        summary generation cycle.
        
        Uses the model specified on the prompt version row. If no model
        is specified, falls back to the primary model from config.json.
        """
        try:
            from db.database import get_db
            from core.model_interface import get_model_interface

            db = get_db()
            model_interface = get_model_interface()
            config = self._tf_configs[tf_minutes]
            label = config["label"]

            # ── Load prompt from prompt_versions ──
            prompt_config = self._get_prompt_for_tf(tf_minutes)
            system_prompt = prompt_config["system_prompt"]
            user_template = prompt_config["user_prompt_template"]
            prompt_model = prompt_config["model"]
            prompt_provider = prompt_config["model_provider"]
            prompt_version_id = prompt_config["prompt_version_id"]
            prompt_name = prompt_config["prompt_name"]

            # ── Fetch news items for this timeframe window ──
            # For short timeframes (1m, 5m), use a wider window to ensure content
            query_minutes = max(tf_minutes, 60)  # Minimum 1-hour lookback
            news_items = db.fetch_all(
                """SELECT headline, detail, source, source_detail, author, symbols, sentiment, published_at
                   FROM news_items
                   WHERE published_at >= DATE_SUB(NOW(), INTERVAL %s MINUTE)
                   ORDER BY published_at DESC
                   LIMIT 50""",
                (query_minutes,)
            )

            if not news_items:
                config["last_summary"] = time.time()
                return

            # ── Load mentor usernames (cached, refreshed every 5 min) ──
            mentor_usernames = set()
            mentor_bypass_enabled = False
            try:
                from core.config import get_config as _gc
                _mcfg = _gc().raw.get("trade_decision", {}).get("mentor", {})
                mentor_bypass_enabled = _mcfg.get("track_all_content", True)
            except Exception:
                mentor_bypass_enabled = True

            if mentor_bypass_enabled:
                _now = time.time()
                if not hasattr(self, '_mentor_cache') or _now - getattr(self, '_mentor_cache_ts', 0) > 300:
                    try:
                        rows = db.fetch_all("""
                            SELECT LOWER(up.display_name) as n FROM user_profiles up WHERE up.is_mentor = 1
                            UNION
                            SELECT LOWER(upl.source_username) FROM user_profile_links upl
                            JOIN user_profiles up2 ON upl.user_profile_id = up2.id WHERE up2.is_mentor = 1
                        """)
                        self._mentor_cache = set(r['n'] for r in (rows or []) if r.get('n'))
                        self._mentor_cache_ts = _now
                    except Exception as e:
                        logger.debug(f"Failed to load mentor usernames: {e}")
                        self._mentor_cache = set()
                        self._mentor_cache_ts = _now
                mentor_usernames = getattr(self, '_mentor_cache', set())

            def _is_mentor_item(item):
                if not mentor_usernames:
                    return False
                auth = (item.get("author") or "").strip().lower()
                return auth in mentor_usernames

            # ── Company Policy Gate: enforce market_symbols.submit_to_ai ──
            # Only items tagged with symbols that have submit_to_ai=1 go into AI digests.
            # Items with no symbols (general news, chat) pass through.
            # Mentor-authored items ALWAYS pass (so Apex can learn from all their content).
            ai_symbols = set()
            try:
                ai_sym_rows = db.fetch_all(
                    "SELECT symbol FROM market_symbols WHERE submit_to_ai = 1"
                )
                ai_symbols = set(r['symbol'].upper() for r in (ai_sym_rows or []) if r.get('symbol'))
                logger.debug(f"AI submit symbols (company policy): {len(ai_symbols)} symbols enabled")
            except Exception as e:
                logger.warning(f"Failed to load submit_to_ai symbols: {e}")

            if ai_symbols:
                filtered = []
                for item in news_items:
                    if _is_mentor_item(item):
                        filtered.append(item)
                        continue
                    syms = item.get("symbols", "")
                    if isinstance(syms, str):
                        try:
                            syms = json.loads(syms) if syms else []
                        except Exception:
                            syms = []
                    item_syms = set((s or "").upper() for s in (syms if isinstance(syms, list) else []))
                    if not item_syms or (item_syms & ai_symbols):
                        filtered.append(item)
                if filtered:
                    news_items = filtered

            # Filter by Command Center: only include items from users with track_alpha=1
            # Mentor-authored items always pass this gate too.
            try:
                alpha_users = db.fetch_all(
                    "SELECT source, source_detail, author FROM user_follow_preferences WHERE track_alpha = 1"
                )
                if alpha_users:
                    alpha_set = set(
                        (r['source'], r.get('source_detail', ''), r['author'])
                        for r in alpha_users
                    )
                    filtered = []
                    for item in news_items:
                        if _is_mentor_item(item):
                            filtered.append(item)
                            continue
                        src = (item.get("source") or "").strip()
                        det = (item.get("source_detail") or "").strip()
                        auth = (item.get("author") or "").strip()
                        if src in ('news', 'tradingview'):
                            filtered.append(item)
                        elif (src, det, auth) in alpha_set:
                            filtered.append(item)
                    if filtered:
                        news_items = filtered
            except Exception as e:
                logger.warning(f"Failed to apply Command Center alpha filter: {e}")

            # ── Build the news digest for AI ──
            digest_lines = []
            for i, item in enumerate(news_items[:30]):  # Limit to 30 for cost
                headline = item.get("headline", "")
                source = item.get("source", "")
                sentiment = item.get("sentiment", "")
                symbols = item.get("symbols", "")
                if isinstance(symbols, str):
                    try:
                        symbols = json.loads(symbols)
                    except Exception:
                        symbols = []
                sym_str = ", ".join(symbols) if symbols else ""
                digest_lines.append(
                    f"[{i+1}] {headline} | Source: {source} | Sentiment: {sentiment} | Symbols: {sym_str}"
                )

            digest = "\n".join(digest_lines)

            # ── Build the user prompt from template ──
            user_prompt = user_template.format(
                news_count=len(news_items),
                label=label,
                digest=digest,
                timeframe_minutes=tf_minutes,
            )
            if ai_symbols:
                user_prompt += (
                    "\n\nIMPORTANT: Restrict your analysis and recommendations to ONLY these symbols: "
                    + ", ".join(sorted(ai_symbols))
                    + ". Do not discuss or mention other assets."
                )

            # ── Query the AI model ──
            # If the prompt specifies a model, use query_with_model for that specific model
            # Otherwise, use the default primary model
            if prompt_model and prompt_provider:
                result = model_interface.query_with_model(
                    model_id=prompt_model,
                    provider=prompt_provider,
                    role="analyst",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.3,
                    context="timeframe_summary",
                    source="system",
                    source_detail=label,
                    media_type="text", duo_id=None,
                )
            else:
                result = model_interface.query(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    role="analyst",
                    temperature=0.3,
                    context="timeframe_summary",
                    source="system",
                    source_detail=label,
                    media_type="text", duo_id=None,
                )

            if not result.success:
                logger.error(f"AI query failed for {label}: {result.error_message}")
                config["last_summary"] = time.time()
                return

            summary_text = result.content if hasattr(result, 'content') else str(result)
            cost = result.cost_usd if hasattr(result, 'cost_usd') else 0.0
            model_used = result.model if hasattr(result, 'model') else "unknown"
            model_provider_used = result.provider if hasattr(result, 'provider') else ""

            # ── Extract symbols mentioned and sentiment from the summary ──
            symbols_mentioned = []
            sentiment_overall = "neutral"
            summary_lower = summary_text.lower()
            if "bullish" in summary_lower:
                sentiment_overall = "bullish"
            elif "bearish" in summary_lower:
                sentiment_overall = "bearish"
            elif "mixed" in summary_lower:
                sentiment_overall = "mixed"

            # Check for common symbols
            for sym in ["XAUUSD", "EURUSD", "GBPUSD", "NAS100", "USDJPY", "BTCUSD", "SPX500"]:
                if sym.lower() in summary_lower or sym in summary_text:
                    symbols_mentioned.append(sym)

            # ── Store the summary in alpha_summaries table ──
            pane_id = f"tf_{tf_minutes}"
            db.execute(
                """INSERT INTO alpha_summaries
                   (account_id, pane_id, timeframe_minutes, summary_text, news_count,
                    symbols_mentioned, sentiment_overall, model_used, prompt_version_id,
                    model_provider, token_count, cost_usd)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    "default", pane_id, tf_minutes, summary_text, len(news_items),
                    json.dumps(symbols_mentioned),
                    sentiment_overall,
                    model_used,
                    prompt_version_id,
                    model_provider_used,
                    (result.token_count_input or 0) + (result.token_count_output or 0),
                    cost
                )
            )

            # Vectorize the alpha summary + record source lineage
            try:
                from services.vectorization_worker import queue_for_vectorization, record_lineage
                summary_row = db.fetch_one("SELECT LAST_INSERT_ID() as lid")
                summary_id = summary_row["lid"] if summary_row else 0
                if summary_id:
                    queue_for_vectorization(db, "alpha_summaries", summary_id, "alpha_timeframes", summary_text[:30000], {
                        "timeframe_minutes": tf_minutes,
                        "pane_id": pane_id,
                        "symbols": symbols_mentioned,
                        "sentiment": sentiment_overall,
                    })
                    # Lineage: this summary was built from these news items
                    news_ids = [n.get("id") or n.get("news_id") for n in news_items if n.get("id") or n.get("news_id")]
                    if news_ids:
                        record_lineage(db, "alpha_summaries", summary_id,
                                       [("news_items", nid) for nid in news_ids], "analyzed_from")
            except Exception as vec_err:
                logger.debug(f"Summary vectorization error: {vec_err}")

            # ── Also store as a news_item so it appears in the alpha feed timeline ──
            hash_key = hashlib.md5(
                f"alpha_summary_{pane_id}_{tf_minutes}_{datetime.now().isoformat()}".encode()
            ).hexdigest()

            source_label = f"ai_alpha_{label}"
            headline = f"AI Alpha Summary ({label}) - {datetime.now().strftime('%H:%M')}"
            if prompt_name:
                headline = f"AI Alpha: {prompt_name} ({label}) - {datetime.now().strftime('%H:%M')}"

            db.execute(
                """INSERT INTO news_items
                   (hash_key, source, headline, detail, category, relevance, sentiment, symbols, published_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())""",
                (
                    hash_key,
                    source_label,
                    headline,
                    summary_text,
                    "analysis",
                    "high",
                    sentiment_overall,
                    json.dumps(symbols_mentioned),
                )
            )

            config["last_summary"] = time.time()
            self._stats["total_summaries"] += 1
            logger.info(
                f"Generated AI summary for {label} ({tf_minutes}m) "
                f"using {model_used} (prompt v{prompt_version_id or 'fallback'}, "
                f"{len(news_items)} items, ${cost:.4f})"
            )

            # Update legacy pane configs if they match
            for pid, pcfg in self._pane_configs.items():
                if pcfg["timeframe_minutes"] == tf_minutes:
                    pcfg["last_summary"] = time.time()

        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"Summary generation error for {tf_minutes}m: {e}")

    # ─────────────────────────────────────────────────────────────────
    # Utility Methods
    # ─────────────────────────────────────────────────────────────────

    def _classify_category(self, item) -> str:
        """Classify a StandardizedItem into a category.
        Priority: 1) managed_sources DB category  2) item.category  3) keyword matching
        
        Categories:
          - news: general news (Bloomberg, CNBC, etc.) — goes to Alpha feed
          - alpha: trading alpha/analysis — goes to Alpha feed + Signal AI
          - signals: explicit trading signals — goes to Alpha feed + Signal AI
          - signals_alpha: both signals and alpha — goes to Alpha feed + Signal AI
          - politics: geopolitical news — goes to Alpha feed
          - trading_signals: legacy category for signals — goes to Signal AI
          - tv_mind, tv_idea: TradingView specific — goes to Signal AI
        """
        title = (getattr(item, 'title', '') or '').lower()
        source = (getattr(item, 'source', '') or '').lower()
        source_detail = (getattr(item, 'source_detail', '') or '').lower()
        category = (getattr(item, 'category', '') or '').lower()

        # First: look up managed_sources for a user-assigned category
        try:
            from db.database import get_db
            _db = get_db()
            ms_cat = _db.fetch_one(
                'SELECT category FROM managed_sources WHERE enabled=1 AND '
                '(name=%s OR LOWER(name) LIKE %s) LIMIT 1',
                (getattr(item, 'source_detail', '') or getattr(item, 'title', ''),
                 f'%{source_detail}%' if source_detail else '%')
            )
            if ms_cat and ms_cat.get('category') and ms_cat['category'] != 'general':
                return ms_cat['category']
        except Exception:
            pass  # Fall through to other methods

        if category and category != 'unknown':
            return category
        # TradingView-specific categories
        if source == 'tradingview':
            if source_detail == 'minds':
                return 'tv_mind'
            elif source_detail == 'ideas':
                return 'tv_idea'
            return 'social'
        if any(k in title for k in ["fed", "rate", "inflation", "cpi", "nfp", "gdp", "fomc"]):
            return "macro"
        if any(k in title for k in ["gold", "xau", "silver", "commodity"]):
            return "commodity"
        if any(k in title for k in ["forex", "eur", "gbp", "usd", "jpy", "currency"]):
            return "forex"
        if any(k in title for k in ["stock", "nasdaq", "s&p", "dow", "equity"]):
            return "equity"
        if any(k in title for k in ["crypto", "bitcoin", "btc", "eth"]):
            return "crypto"
        return "news"

    def get_status(self) -> Dict[str, Any]:
        """Get orchestrator status for the dashboard, including per-collector health."""
        # Build collector health with human-readable timestamps
        collector_health = {}
        for cname, health in self._collector_health.items():
            ch = dict(health)
            if ch.get("last_success") and ch["last_success"] > 0:
                ch["last_success_iso"] = datetime.fromtimestamp(ch["last_success"]).isoformat()
                ch["seconds_since_success"] = int(time.time() - ch["last_success"])
            else:
                ch["last_success_iso"] = None
                ch["seconds_since_success"] = None
            # Watchdog: mark as stuck if no success in 5x interval
            interval = self._collector_intervals.get(cname, 60)
            if (ch.get("last_success", 0) > 0
                    and time.time() - ch["last_success"] > interval * 5
                    and ch.get("state") != "running"):
                ch["state"] = "stuck"
            collector_health[cname] = ch

        return {
            "running": self._running,
            "stats": self._stats,
            "timeframe_configs": {
                str(tf_min): {
                    "label": config["label"],
                    "role": config["role"],
                    "interval_seconds": config["interval_seconds"],
                    "last_summary": datetime.fromtimestamp(config["last_summary"]).isoformat()
                    if config["last_summary"] > 0 else None,
                }
                for tf_min, config in self._tf_configs.items()
            },
            "pane_configs": {
                pane_id: {
                    "timeframe_minutes": config["timeframe_minutes"],
                    "last_summary": datetime.fromtimestamp(config["last_summary"]).isoformat()
                    if config["last_summary"] > 0 else None,
                }
                for pane_id, config in self._pane_configs.items()
            },
            "collector_intervals": self._collector_intervals,
            "last_collector_runs": {
                name: datetime.fromtimestamp(ts).isoformat()
                for name, ts in self._last_collector_run.items()
                if ts > 0
            },
            "collector_health": collector_health,
            "signal_ai": self._signal_ai.get_status() if self._signal_ai else {"status": "not_initialized"},
        }

    def get_latest_summary(self, pane_id: str) -> Optional[Dict]:
        """Get the latest AI summary for a pane (legacy) or timeframe."""
        try:
            from db.database import get_db
            db = get_db()
            # Support both legacy pane IDs and new tf_ IDs
            row = db.fetch_one(
                """SELECT summary_text, news_count, model_used, prompt_version_id,
                          model_provider, cost_usd, created_at
                   FROM alpha_summaries
                   WHERE pane_id=%s
                   ORDER BY created_at DESC LIMIT 1""",
                (pane_id,)
            )
            if row:
                return {
                    "summary": row.get("summary_text", ""),
                    "news_count": row.get("news_count", 0),
                    "model": row.get("model_used", ""),
                    "model_provider": row.get("model_provider", ""),
                    "prompt_version_id": row.get("prompt_version_id"),
                    "cost": float(row.get("cost_usd", 0)),
                    "generated_at": (row.get("created_at").isoformat() + 'Z') if row.get("created_at") else None,
                }
        except Exception as e:
            logger.error(f"Error fetching summary: {e}")
        return None

    def get_latest_summary_for_tf(self, timeframe_minutes: int) -> Optional[Dict]:
        """Get the latest AI summary for a specific timeframe."""
        pane_id = f"tf_{timeframe_minutes}"
        return self.get_latest_summary(pane_id)

    def get_summary_history(self, pane_id: str, limit: int = 20) -> List[Dict]:
        """Get historical AI summaries for a pane."""
        try:
            from db.database import get_db
            db = get_db()
            rows = db.fetch_all(
                """SELECT summary_text, news_count, timeframe_minutes, model_used,
                          prompt_version_id, model_provider, cost_usd, created_at
                   FROM alpha_summaries
                   WHERE pane_id=%s
                   ORDER BY created_at DESC LIMIT %s""",
                (pane_id, limit)
            )
            return [
                {
                    "summary": r.get("summary_text", ""),
                    "news_count": r.get("news_count", 0),
                    "timeframe": r.get("timeframe_minutes", 0),
                    "model": r.get("model_used", ""),
                    "model_provider": r.get("model_provider", ""),
                    "prompt_version_id": r.get("prompt_version_id"),
                    "cost": float(r.get("cost_usd", 0)),
                    "generated_at": (r.get("created_at").isoformat() + 'Z') if r.get("created_at") else None,
                }
                for r in rows
            ]
        except Exception as e:
            logger.error(f"Error fetching summary history: {e}")
            return []

    # ─────────────────────────────────────────────────────────────────
    # CEO Daily Report & Proposal Scan
    # ─────────────────────────────────────────────────────────────────

    def _check_ceo_daily_report(self):
        """Check if it's time to generate the CEO daily report.
        Schedule is loaded from system_config (set via CEO tab slider)."""
        now = datetime.now(timezone.utc)
        today_str = now.strftime('%Y-%m-%d')

        # Skip if already generated today
        if self._last_ceo_report_date == today_str:
            return

        # Load schedule from system_config
        try:
            from db.database import get_db
            db = get_db()
            utc_h_row = db.fetch_one("SELECT config_value FROM system_config WHERE config_key = 'ceo_report_utc_hour'")
            utc_m_row = db.fetch_one("SELECT config_value FROM system_config WHERE config_key = 'ceo_report_utc_minute'")
            if utc_h_row:
                self._ceo_report_hour_utc = int(utc_h_row.get('config_value', 19))
            if utc_m_row:
                self._ceo_report_minute_utc = int(utc_m_row.get('config_value', 59))
        except Exception:
            pass  # Use existing defaults

        # Only trigger at or after the scheduled time
        if now.hour < self._ceo_report_hour_utc:
            return
        if now.hour == self._ceo_report_hour_utc and now.minute < self._ceo_report_minute_utc:
            return

        logger.info("=== CEO DAILY REPORT: Generating end-of-day report ===")

        # Run comprehensive model sync first (check for new/removed models, price changes)
        try:
            from services.model_sync import run_model_sync
            result = run_model_sync()
            logger.info(f"Model sync before CEO report: {result.get('total_models', 0)} models, +{result.get('added', 0)} new, {result.get('price_changed', 0)} price changes")
        except Exception as e:
            logger.warning(f"Model sync failed (non-blocking): {e}")

        try:
            self._generate_ceo_report(report_type="full")
            self._last_ceo_report_date = today_str  # Only mark done on success
            logger.info("CEO daily report generated successfully")
        except Exception as e:
            logger.error(f"CEO daily report generation failed: {e}")

    def _generate_ceo_report(self, report_type: str = "day_so_far"):
        """Generate a CEO report using AI analysis of the day's data."""
        from db.database import get_db
        from core.model_interface import get_model_interface

        db = get_db()
        model = get_model_interface()

        # Gather all data for the report
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')

        # Get trade data
        trades_today = db.execute_query(
            "SELECT * FROM trades WHERE DATE(created_at) = %s ORDER BY created_at",
            (today,)
        ) or []

        total_pnl = sum(float(t.get('pnl_usd', 0) or 0) for t in trades_today)
        wins = sum(1 for t in trades_today if float(t.get('pnl_usd', 0) or 0) > 0)
        losses = sum(1 for t in trades_today if float(t.get('pnl_usd', 0) or 0) < 0)
        win_rate = (wins / len(trades_today) * 100) if trades_today else 0

        # Get role performance — role_score_snapshots has per-role score columns
        role_snapshot = db.execute_query(
            "SELECT trader_score, coach_score, analyst_score, postmortem_score "
            "FROM role_score_snapshots WHERE date = %s ORDER BY created_at DESC LIMIT 1",
            (today,)
        )
        role_data = []
        if role_snapshot:
            snap = role_snapshot[0]
            for role_name in ['trader', 'coach', 'analyst', 'postmortem']:
                score = snap.get(f'{role_name}_score')
                # Get model from role_model_history
                model_row = db.execute_query(
                    "SELECT model FROM role_model_history WHERE role = %s ORDER BY assigned_at DESC LIMIT 1",
                    (role_name,)
                )
                model_name = model_row[0].get('model', 'N/A') if model_row else 'N/A'
                role_data.append({'role': role_name, 'score': score, 'model': model_name})

        # Get pending proposals
        proposals = db.execute_query(
            "SELECT * FROM prompt_change_proposals WHERE status = 'pending' ORDER BY created_at DESC"
        ) or []

        # Get alpha summaries generated today
        summaries_today = db.execute_query(
            "SELECT COUNT(*) as cnt, SUM(cost_usd) as total_cost FROM alpha_summaries "
            "WHERE DATE(created_at) = %s",
            (today,)
        ) or [{}]
        summary_count = summaries_today[0].get('cnt', 0) if summaries_today else 0
        summary_cost = float(summaries_today[0].get('total_cost', 0) or 0) if summaries_today else 0

        # Get recent lessons — trade_lessons uses lesson_text, outcome, decision_quality(json)
        lessons = db.execute_query(
            "SELECT lesson_text, outcome, what_worked, what_failed FROM trade_lessons "
            "WHERE DATE(timestamp) = %s ORDER BY timestamp DESC LIMIT 10",
            (today,)
        ) or []

        # Build the CEO prompt
        report_label = "END OF DAY REPORT" if report_type == "full" else "DAY SO FAR REPORT"
        ceo_prompt = f"""## CEO {report_label} — {today}

### Trading Performance
- **Total Trades**: {len(trades_today)}
- **Wins**: {wins} | **Losses**: {losses} | **Win Rate**: {win_rate:.1f}%
- **Total P&L**: ${total_pnl:.2f}
- **AI Summaries Generated**: {summary_count} (cost: ${summary_cost:.4f})

### Trade Details
"""
        if trades_today:
            for t in trades_today:
                pnl = float(t.get('pnl_usd', 0) or 0)
                ceo_prompt += (f"- {t.get('symbol', 'N/A')} {t.get('direction', 'N/A')} | "
                               f"P&L: ${pnl:.2f} | {'WIN' if pnl > 0 else 'LOSS'}\n")
        else:
            ceo_prompt += "No trades executed today.\n"

        ceo_prompt += f"\n### Role Performance\n"
        if role_data:
            for r in role_data:
                ceo_prompt += (f"- **{r.get('role', 'N/A')}**: Score {r.get('score', 'N/A')}, "
                               f"Model: {r.get('model', 'N/A')}\n")
        else:
            ceo_prompt += "No role snapshots today.\n"

        ceo_prompt += f"\n### Pending Proposals: {len(proposals)}\n"
        for p in proposals[:5]:
            ceo_prompt += (f"- [{p.get('proposing_role', 'N/A')} \u2192 {p.get('target_role', 'N/A')}] "
                           f"{p.get('change_type', 'N/A')}: {(p.get('reason', '') or 'N/A')[:100]}\n")

        ceo_prompt += f"\n### Lessons Learned Today\n"
        if lessons:
            for l in lessons:
                ceo_prompt += f"- [{l.get('outcome', 'N/A')}] {(l.get('lesson_text', '') or 'N/A')[:100]}\n"
        else:
            ceo_prompt += "No lessons today.\n"

        ceo_prompt += "\nProvide your CEO assessment as a JSON object with keys: "
        ceo_prompt += "executive_summary, trade_analysis, role_performance, recommendations, risk_alerts, overall_grade (A-F)"

        # Get CEO model from system_config
        ceo_model = "openai/gpt-4.1-mini"
        ceo_provider = "openrouter"
        try:
            config_row = db.execute_query(
                "SELECT config_value FROM system_config WHERE config_key = 'ceo_model'"
            )
            if config_row:
                ceo_model = config_row[0].get('config_value', ceo_model)
            provider_row = db.execute_query(
                "SELECT config_value FROM system_config WHERE config_key = 'ceo_model_provider'"
            )
            if provider_row:
                ceo_provider = provider_row[0].get('config_value', ceo_provider)
        except Exception:
            pass

        ceo_system = ("You are the CEO of JarvAIs, an AI-powered trading company. "
                      "You are professional, data-driven, and focused on profitability. "
                      "You review the company's daily performance and provide actionable insights. "
                      "Be frank but respectful. Compliment successes, identify areas for improvement. "
                      "Grade the day A-F based on profitability, discipline, and execution quality.")

        # Query the AI
        response = model.query_with_model(
            model_id=ceo_model,
            provider=ceo_provider,
            system_prompt=ceo_system,
            user_prompt=ceo_prompt,
            role="ceo",
            context="ceo_daily_report",
            source="system",
            source_detail="ceo_daily_report",
            media_type="text", duo_id=None,
        )

        if not response.success:
            logger.error(f"CEO report AI query failed: {response.error_message}")
            return

        content = response.content

        # Try to parse as JSON
        try:
            import json
            report_data = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            report_data = {"executive_summary": content}

        # Store in ceo_daily_reports table
        # Table columns: report_date, report_type, model_used, executive_summary,
        #   pnl_summary, trade_analysis, role_performance, mistake_analysis,
        #   alpha_quality, pending_proposals, recommendations,
        #   total_trades, total_pnl, win_rate, total_api_cost, sharpe_ratio,
        #   mistakes_identified, recurring_mistakes, token_count, cost_usd
        report_type_enum = "daily" if report_type == "full" else "day_so_far"
        db.execute(
            """INSERT INTO ceo_daily_reports 
            (report_date, report_type, model_used, executive_summary,
             pnl_summary, trade_analysis, role_performance, mistake_analysis,
             pending_proposals, recommendations,
             total_trades, total_pnl, win_rate, total_api_cost,
             cost_usd)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                today,
                report_type_enum,
                ceo_model,
                report_data.get("executive_summary", content[:500]),
                f"Total P&L: ${total_pnl:.2f} | Trades: {len(trades_today)} | Win Rate: {win_rate:.1f}%",
                report_data.get("trade_analysis", ""),
                report_data.get("role_performance", ""),
                report_data.get("mistake_analysis", ""),
                f"{len(proposals)} pending" if proposals else "None",
                report_data.get("recommendations", ""),
                len(trades_today),
                total_pnl,
                win_rate,
                summary_cost,
                response.cost_usd
            )
        )

        logger.info(f"CEO {report_type} report stored successfully.")

        # Store CEO report in Mem0 for institutional memory
        try:
            from core.memory_manager import get_memory_manager
            mem = get_memory_manager()
            report_summary = content[:500] if len(content) > 500 else content
            mem.store_memory(
                text=f"CEO {report_type} Report ({today}): {report_summary}",
                metadata={
                    'type': 'ceo_report',
                    'report_type': report_type,
                    'date': today,
                    'model': ceo_model,
                    'trades': len(trades_today),
                    'pnl': total_pnl,
                    'win_rate': win_rate
                },
                collection='ceo_reports',
                role='ceo'
            )
            logger.info(f"CEO report stored in Mem0 for {today}")
        except Exception as mem_err:
            logger.warning(f"Failed to store CEO report in Mem0: {mem_err}")

    def _check_daily_proposal_scan(self):
        """Run the proposal engine daily scan at 8 PM UTC (midnight Dubai).
        Checks patterns across all recent trades and generates proposals."""
        now = datetime.now(timezone.utc)
        today_str = now.strftime('%Y-%m-%d')

        # Skip if already scanned today
        if self._last_proposal_scan_date == today_str:
            return

        # Only trigger at or after 8 PM UTC
        if now.hour < 20:
            return

        logger.info("=== DAILY PROPOSAL SCAN: Checking for patterns ===")
        self._last_proposal_scan_date = today_str

        try:
            from db.database import get_db
            from core.model_interface import get_model_interface
            from services.proposal_engine import ProposalEngine

            db = get_db()
            model = get_model_interface()
            engine = ProposalEngine(db, model)

            # Run the daily pattern detection
            proposals = engine.daily_pattern_scan()
            if proposals:
                logger.info(f"Daily scan generated {len(proposals)} proposal(s)")
            else:
                logger.info("Daily scan: No proposals generated (no concerning patterns detected)")

        except Exception as e:
            logger.error(f"Daily proposal scan error: {e}")

    # ─────────────────────────────────────────────────────────────────
    # Signal AI Integration
    # ─────────────────────────────────────────────────────────────────

    def _get_signal_ai(self):
        """Lazy-load the Signal AI service."""
        if self._signal_ai is None:
            try:
                from services.signal_ai import get_signal_ai
                self._signal_ai = get_signal_ai()
                logger.info("Signal AI initialized and integrated with orchestrator")
            except Exception as e:
                logger.error(f"Failed to initialize Signal AI: {e}")
        return self._signal_ai

    def _run_signal_ai(self):
        """Run the Signal AI pipeline: parse queue → track signals → score resolved."""
        signal_ai = self._get_signal_ai()
        if not signal_ai:
            logger.warning("Signal AI not available")
            return

        # On first run, do a SMALL backfill to avoid blocking summaries
        # The catch-up will gradually process more items over time
        if not self._signal_ai_backfill_done:
            try:
                logger.info("Signal AI: starting initial backfill (small batch)...")
                parsed = signal_ai.parse_existing_signals(limit=10)  # Small batch to avoid blocking
                self._signal_ai_backfill_done = True
                logger.info(f"Signal AI backfill: parsed {parsed} signals (catch-up will process more)")
            except Exception as e:
                logger.error(f"Signal AI backfill error: {e}")
                import traceback
                logger.error(traceback.format_exc())
                self._signal_ai_backfill_done = True  # Don't retry forever

        # Run all 3 stages
        logger.debug("Signal AI: running all stages (parse queue, track, score)")
        signal_ai.run_all_stages()


# Singleton
_orchestrator: Optional[AlphaOrchestrator] = None


def get_alpha_orchestrator() -> AlphaOrchestrator:
    """Get or create the shared AlphaOrchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AlphaOrchestrator()
    return _orchestrator


def start_alpha_orchestrator() -> AlphaOrchestrator:
    """Convenience function to start the orchestrator."""
    orch = get_alpha_orchestrator()
    orch.start()
    return orch
