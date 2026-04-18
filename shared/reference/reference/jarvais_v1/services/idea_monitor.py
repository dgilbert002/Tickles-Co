"""
JarvAIs Idea Monitor Service v2.0

Tracks TradingView Ideas over time with a FLAT 3x schedule.
Uses the persistent headless browser to click PLAY on each idea,
screenshot the "played out" chart, and resubmit ALL history to AI.

Schedule Logic (FLAT 3x, not exponential):
  - Check interval = 3 × chart_timeframe
  - 5m chart  → check every 15m
  - 15m chart → check every 45m
  - 1h chart  → check every 3h
  - 4h chart  → check every 12h
  - 1D chart  → check every 3 days, max 3 weeks (7 checks)
  - 1W chart  → check every 3 weeks, max 4 weeks (~1-2 checks)

On first ingestion, AI returns an estimated end date for the idea.
Monitoring continues until that end date or the max cap is reached.

Each check:
  1. Browser navigates to idea URL → clicks PLAY → waits 5s → screenshots
  2. ALL previous snapshots + new screenshot sent to AI
  3. AI scores the idea (0-100) and says if opinion changed
  4. Author gets an accuracy score based on idea outcomes
  5. Snapshot stored with screenshot path, AI analysis, score
"""

import asyncio
import json
import logging
import os
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from core.time_utils import utcnow

logger = logging.getLogger("jarvais.idea_monitor")

# Timeframe string → minutes mapping
TF_MINUTES = {
    "1m": 1, "1": 1,
    "5m": 5, "5": 5,
    "15m": 15, "15": 15,
    "30m": 30, "30": 30,
    "1h": 60, "60": 60, "60m": 60,
    "2h": 120, "120": 120, "120m": 120,
    "3h": 180, "180": 180, "180m": 180,
    "4h": 240, "240": 240, "240m": 240,
    "1D": 1440, "D": 1440, "1d": 1440,
    "1W": 10080, "W": 10080, "1w": 10080,
    "1M": 43200, "M": 43200, "1m_monthly": 43200,
}

# Max monitoring duration caps
MAX_MONITORING = {
    # tf_minutes: max_days
    1: 1,        # 1m charts: max 1 day
    5: 2,        # 5m charts: max 2 days
    15: 3,       # 15m charts: max 3 days
    30: 5,       # 30m charts: max 5 days
    60: 7,       # 1h charts: max 7 days
    120: 10,     # 2h charts: max 10 days
    180: 14,     # 3h charts: max 14 days
    240: 14,     # 4h charts: max 14 days
    1440: 21,    # Daily charts: max 3 weeks
    10080: 28,   # Weekly charts: max 4 weeks
    43200: 60,   # Monthly charts: max 60 days
}

MULTIPLIER = 3  # Flat 3x the chart timeframe


class IdeaMonitorService:
    """
    Monitors tracked TradingView Ideas with a flat 3x schedule.
    Uses the persistent browser service for PLAY button screenshots.
    """

    def __init__(self):
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._tick_interval = 30  # Check every 30 seconds
        self._stats = {
            "started_at": None,
            "total_checks": 0,
            "total_snapshots": 0,
            "total_screenshots": 0,
            "total_comments_fetched": 0,
            "errors": 0,
        }

    def start(self):
        """Start the monitor background thread."""
        if self._running:
            logger.warning("Idea Monitor already running")
            return
        self._running = True
        self._stats["started_at"] = datetime.now().isoformat()
        self._ensure_tables()

        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="idea-monitor"
        )
        self._thread.start()
        logger.info("Idea Monitor Service v2.0 started (flat 3x schedule)")

    def stop(self):
        """Stop the monitor."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=15)
        logger.info("Idea Monitor Service stopped")

    def _ensure_tables(self):
        """Ensure the idea_snapshots and idea_comments tables exist."""
        try:
            from db.database import get_db
            db = get_db()

            db.execute("""
                CREATE TABLE IF NOT EXISTS idea_snapshots (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    tracking_id INT NOT NULL,
                    idea_id VARCHAR(50) NOT NULL,
                    check_number INT NOT NULL DEFAULT 0,
                    check_interval_label VARCHAR(50),
                    screenshot_path VARCHAR(500),
                    ai_analysis MEDIUMTEXT,
                    ai_score DECIMAL(5,2) DEFAULT NULL,
                    ai_opinion_changed TINYINT(1) DEFAULT 0,
                    comment_count INT DEFAULT 0,
                    new_comments_json MEDIUMTEXT,
                    status VARCHAR(20) DEFAULT 'tracking',
                    snapshot_data JSON,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_idea_id (idea_id),
                    INDEX idx_tracking_id (tracking_id)
                )
            """)

            db.execute("""
                CREATE TABLE IF NOT EXISTS idea_comments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    idea_id VARCHAR(50) NOT NULL,
                    comment_id VARCHAR(50) NOT NULL,
                    author VARCHAR(100),
                    text MEDIUMTEXT,
                    likes INT DEFAULT 0,
                    parent_id VARCHAR(50),
                    posted_at DATETIME,
                    fetched_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_comment (idea_id, comment_id),
                    INDEX idx_idea_id (idea_id)
                )
            """)

            logger.info("Idea monitor tables verified")
        except Exception as e:
            logger.error(f"Failed to ensure tables: {e}")

    def _parse_tf_minutes(self, tf_str: str) -> int:
        """Parse a timeframe string to minutes."""
        if not tf_str:
            return 60
        tf_str = tf_str.strip()
        # Direct lookup
        if tf_str in TF_MINUTES:
            return TF_MINUTES[tf_str]
        # Try numeric
        try:
            val = int(tf_str)
            # If small number, it's probably minutes already
            if val <= 240:
                return val
            return val
        except ValueError:
            pass
        return 60  # Default to 1h

    def _calc_check_interval(self, tf_minutes: int) -> int:
        """Calculate the flat check interval: 3x the chart timeframe in minutes."""
        return tf_minutes * MULTIPLIER

    def _calc_monitoring_end(self, created_at: datetime, tf_minutes: int,
                              ai_end_date: Optional[datetime] = None) -> datetime:
        """Calculate when to stop monitoring this idea."""
        # Use AI end date if available and reasonable
        if ai_end_date and ai_end_date > created_at:
            # But cap it at the max for this timeframe
            max_days = MAX_MONITORING.get(tf_minutes, 14)
            max_end = created_at + timedelta(days=max_days)
            return min(ai_end_date, max_end)

        # Default: use the max cap for this timeframe
        max_days = MAX_MONITORING.get(tf_minutes, 14)
        return created_at + timedelta(days=max_days)

    def register_new_idea(self, idea_item: Dict):
        """
        Register a newly collected idea for tracking.
        Called by the Alpha Orchestrator when a new idea is stored.
        """
        try:
            from db.database import get_db
            db = get_db()

            idea_id = idea_item.get("external_id", "")
            if not idea_id:
                return

            # Check if already tracked
            existing = db.fetch_one(
                "SELECT id FROM tradingview_ideas_tracking WHERE idea_id = %s",
                (idea_id,)
            )
            if existing:
                return

            # Parse symbols
            symbol = ""
            symbols = idea_item.get("symbols", [])
            if isinstance(symbols, list) and symbols:
                symbol = symbols[0]
            elif isinstance(symbols, str):
                try:
                    parsed = json.loads(symbols)
                    symbol = parsed[0] if parsed else ""
                except:
                    symbol = symbols

            direction = idea_item.get("direction", "")
            tf = idea_item.get("tv_timeframe", "")
            author = idea_item.get("author", "")
            chart_url = idea_item.get("chart_image_url", "")
            ai_analysis = idea_item.get("ai_analysis", "")
            idea_url = idea_item.get("url", "")

            # Calculate schedule
            tf_minutes = self._parse_tf_minutes(tf)
            check_interval = self._calc_check_interval(tf_minutes)
            now = utcnow()
            next_check = now + timedelta(minutes=check_interval)
            monitoring_end = self._calc_monitoring_end(now, tf_minutes)

            db.execute("""
                INSERT INTO tradingview_ideas_tracking
                (idea_id, symbol, direction, chart_timeframe, author,
                 initial_chart_url, ai_initial_analysis, ai_latest_analysis,
                 idea_url, check_interval_minutes, next_check_at,
                 monitoring_end_at, status, check_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'tracking', 0)
            """, (
                idea_id, symbol, direction, tf, author,
                chart_url, ai_analysis, ai_analysis,
                idea_url, check_interval,
                next_check.strftime("%Y-%m-%d %H:%M:%S"),
                monitoring_end.strftime("%Y-%m-%d %H:%M:%S"),
            ))

            logger.info(
                f"Registered idea {idea_id} ({symbol} {direction} {tf}) "
                f"check every {check_interval}m, ends {monitoring_end.strftime('%Y-%m-%d %H:%M')}"
            )

        except Exception as e:
            logger.error(f"Failed to register idea: {e}")

    def _run_loop(self):
        """Main monitor loop."""
        # Initial: register untracked ideas and populate schedules for existing ones
        self._register_untracked_ideas()
        self._populate_missing_schedules()

        while self._running:
            time.sleep(self._tick_interval)
            if not self._running:
                break

            try:
                self._check_due_ideas()
            except Exception as e:
                self._stats["errors"] += 1
                logger.error(f"Monitor loop error: {e}")

    def _register_untracked_ideas(self):
        """Register any ideas in news_items that aren't yet tracked."""
        try:
            from db.database import get_db
            db = get_db()

            rows = db.fetch_all("""
                SELECT n.id, n.external_id, n.source_detail, n.direction,
                       n.tv_timeframe, n.author, n.chart_image_url,
                       n.ai_analysis, n.symbols, n.url
                FROM news_items n
                LEFT JOIN tradingview_ideas_tracking t ON n.external_id = t.idea_id
                WHERE n.source = 'tradingview'
                  AND n.source_detail = 'ideas'
                  AND n.external_id IS NOT NULL
                  AND n.external_id != ''
                  AND t.id IS NULL
                LIMIT 100
            """)

            for row in (rows or []):
                self.register_new_idea(row)

            if rows:
                logger.info(f"Registered {len(rows)} untracked ideas")

        except Exception as e:
            logger.error(f"Failed to register untracked ideas: {e}")

    def _populate_missing_schedules(self):
        """Populate next_check_at for existing tracking records that don't have it."""
        try:
            from db.database import get_db
            db = get_db()

            rows = db.fetch_all("""
                SELECT id, chart_timeframe, created_at, check_count
                FROM tradingview_ideas_tracking
                WHERE status = 'tracking' AND next_check_at IS NULL
            """)

            for row in (rows or []):
                tf = row.get("chart_timeframe", "1h")
                tf_minutes = self._parse_tf_minutes(tf)
                check_interval = self._calc_check_interval(tf_minutes)
                created = row.get("created_at", utcnow())
                monitoring_end = self._calc_monitoring_end(created, tf_minutes)

                # For existing records, set next check to now (so they get checked soon)
                now = utcnow()
                db.execute("""
                    UPDATE tradingview_ideas_tracking
                    SET check_interval_minutes = %s,
                        next_check_at = %s,
                        monitoring_end_at = %s
                    WHERE id = %s
                """, (
                    check_interval,
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    monitoring_end.strftime("%Y-%m-%d %H:%M:%S"),
                    row["id"],
                ))

            if rows:
                logger.info(f"Populated schedules for {len(rows)} existing ideas")

        except Exception as e:
            logger.error(f"Failed to populate schedules: {e}")

    def _check_due_ideas(self):
        """Check all ideas that are due for their next monitoring check."""
        try:
            from db.database import get_db
            db = get_db()

            now = utcnow()
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")

            # Get ideas where next_check_at <= now AND monitoring hasn't ended
            ideas = db.fetch_all("""
                SELECT id, idea_id, symbol, direction, chart_timeframe,
                       author, check_count, check_interval_minutes,
                       next_check_at, monitoring_end_at, idea_url,
                       initial_chart_url, ai_initial_analysis, created_at,
                       entry_price, target_price, stop_price, news_item_id
                FROM tradingview_ideas_tracking
                WHERE status = 'tracking'
                  AND next_check_at IS NOT NULL
                  AND next_check_at <= %s
                ORDER BY next_check_at ASC
                LIMIT 5
            """, (now_str,))

            for idea in (ideas or []):
                # Check if monitoring period has ended
                end_at = idea.get("monitoring_end_at")
                if end_at and now > end_at:
                    # Mark as expired
                    db.execute("""
                        UPDATE tradingview_ideas_tracking
                        SET status = 'expired', next_check_at = NULL
                        WHERE id = %s
                    """, (idea["id"],))
                    logger.info(f"Idea {idea['idea_id']} monitoring expired")
                    continue

                # Perform the check
                self._perform_check(idea)

        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"Check due ideas error: {e}")

    def _perform_check(self, idea: Dict):
        """Perform a monitoring check on an idea: screenshot, AI analysis, store."""
        idea_id = idea.get("idea_id", "")
        tracking_id = idea.get("id")
        check_number = (idea.get("check_count", 0) or 0) + 1
        idea_url = idea.get("idea_url", "")
        check_interval = idea.get("check_interval_minutes", 180)

        try:
            from db.database import get_db
            db = get_db()

            # 1. Take PLAY screenshot via browser service
            screenshot_path = None
            if idea_url:
                try:
                    from services.browser_service import get_browser_service
                    browser = get_browser_service()
                    screenshot_path = browser.screenshot_idea_play(idea_url, idea_id)
                    if screenshot_path:
                        self._stats["total_screenshots"] += 1
                        logger.info(f"PLAY screenshot taken for idea {idea_id}")
                except Exception as e:
                    logger.warning(f"Browser screenshot failed for {idea_id}: {e}")

            # 2. Fetch comments
            comments = self._fetch_idea_comments(idea_id)

            # 3. Get ALL previous snapshots for full history resubmission
            prev_snapshots = db.fetch_all("""
                SELECT check_number, check_interval_label, ai_analysis,
                       ai_score, screenshot_path, created_at
                FROM idea_snapshots
                WHERE idea_id = %s
                ORDER BY check_number ASC
            """, (idea_id,))

            # 4. Run AI analysis with FULL history
            ai_result = self._run_ai_analysis(
                idea, check_number, comments,
                prev_snapshots or [], screenshot_path
            )

            ai_analysis = ai_result.get("analysis", "")
            ai_score = ai_result.get("score")
            opinion_changed = ai_result.get("opinion_changed", False)
            ai_end_date = ai_result.get("end_date")
            status_suggestion = ai_result.get("status", "tracking")

            # 5. Calculate interval label
            elapsed_minutes = check_number * check_interval
            if elapsed_minutes < 60:
                interval_label = f"{elapsed_minutes}m after post"
            elif elapsed_minutes < 1440:
                interval_label = f"{elapsed_minutes / 60:.1f}h after post"
            else:
                interval_label = f"{elapsed_minutes / 1440:.1f}d after post"

            # 6. Store snapshot
            snapshot_data = {
                "check_number": check_number,
                "interval_label": interval_label,
                "elapsed_minutes": elapsed_minutes,
                "comment_count": len(comments) if comments else 0,
                "screenshot_taken": screenshot_path is not None,
                "timestamp": utcnow().isoformat(),
            }

            db.execute("""
                INSERT INTO idea_snapshots
                (tracking_id, idea_id, check_number, check_interval_label,
                 screenshot_path, ai_analysis, ai_score, ai_opinion_changed,
                 comment_count, new_comments_json, status, snapshot_data)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                tracking_id, idea_id, check_number, interval_label,
                screenshot_path, ai_analysis or "", ai_score,
                1 if opinion_changed else 0,
                len(comments) if comments else 0,
                json.dumps(comments[:10]) if comments else "[]",
                status_suggestion, json.dumps(snapshot_data),
            ))

            # 7. Calculate next check time
            now = utcnow()
            next_check = now + timedelta(minutes=check_interval)

            # Update AI end date if returned
            end_date_update = ""
            end_date_params = []
            if ai_end_date:
                end_date_update = ", ai_end_date = %s, monitoring_end_at = %s"
                tf_minutes = self._parse_tf_minutes(idea.get("chart_timeframe", "1h"))
                monitoring_end = self._calc_monitoring_end(
                    idea.get("created_at", now), tf_minutes, ai_end_date
                )
                end_date_params = [
                    ai_end_date.strftime("%Y-%m-%d %H:%M:%S"),
                    monitoring_end.strftime("%Y-%m-%d %H:%M:%S"),
                ]

            # Determine final status
            final_status = status_suggestion if status_suggestion in (
                "hit_target", "hit_stop", "expired"
            ) else "tracking"

            # If status is terminal, stop monitoring
            if final_status != "tracking":
                next_check = None

            db.execute(f"""
                UPDATE tradingview_ideas_tracking
                SET check_count = %s,
                    last_checked_at = NOW(),
                    ai_latest_analysis = %s,
                    ai_score = %s,
                    latest_screenshot = %s,
                    next_check_at = %s,
                    status = %s
                    {end_date_update}
                WHERE id = %s
            """, [
                check_number,
                ai_analysis or "",
                ai_score,
                screenshot_path,
                next_check.strftime("%Y-%m-%d %H:%M:%S") if next_check else None,
                final_status,
                *end_date_params,
                tracking_id,
            ])

            self._stats["total_checks"] += 1
            self._stats["total_snapshots"] += 1
            logger.info(
                f"Check #{check_number} for idea {idea_id} ({interval_label}) "
                f"score={ai_score} opinion_changed={opinion_changed} status={final_status}"
            )

        except Exception as e:
            self._stats["errors"] += 1
            logger.error(f"Check failed for idea {idea_id}: {e}")
            # Still advance the next_check_at so we don't get stuck
            try:
                from db.database import get_db
                db = get_db()
                next_check = utcnow() + timedelta(minutes=check_interval)
                db.execute("""
                    UPDATE tradingview_ideas_tracking
                    SET next_check_at = %s, check_count = check_count + 1
                    WHERE id = %s
                """, (next_check.strftime("%Y-%m-%d %H:%M:%S"), tracking_id))
            except:
                pass

    def _run_ai_analysis(self, idea: Dict, check_number: int,
                          comments: List[Dict], prev_snapshots: List[Dict],
                          screenshot_path: Optional[str]) -> Dict:
        """
        Run AI analysis with FULL history resubmission.
        Loads model + system prompt + user prompt template from the Prompt Engineer
        (prompt_versions table, role='source_tradingview').
        Sends the PLAY screenshot as a vision image so the AI can actually see the chart.
        Returns: {analysis, score, opinion_changed, end_date, status}
        """
        try:
            from db.database import get_db
            from core.model_interface import get_model_interface
            import base64
            db = get_db()
            model_interface = get_model_interface()

            idea_id = idea.get("idea_id", "")
            symbol = idea.get("symbol", "")
            direction = idea.get("direction", "")
            tf = idea.get("chart_timeframe", "")
            author = idea.get("author", "unknown")
            initial_analysis = idea.get("ai_initial_analysis", "")

            # ── Load model from source_model_assignments first ──
            # 'tradingview_ideas' + 'image' column (screenshots are the main media)
            model_id = None
            provider = None
            try:
                sma_row = db.fetch_one(
                    "SELECT model_id, model_provider, is_enabled FROM source_model_assignments "
                    "WHERE source = 'tradingview_ideas' AND media_type = 'image'"
                )
                if sma_row and sma_row.get("model_id"):
                    model_id = sma_row["model_id"]
                    provider = sma_row["model_provider"]
                    if not sma_row.get("is_enabled"):
                        logger.info(f"[Idea Check] Image analysis disabled for tradingview_ideas in source model matrix")
            except Exception as sma_err:
                logger.debug(f"[Idea Check] source_model_assignments lookup error: {sma_err}")

            # ── Load prompts from Prompt Engineer ──
            prompt_row = db.fetch_one(
                "SELECT system_prompt, user_prompt_template, model, model_provider "
                "FROM prompt_versions WHERE role = 'source_tradingview' AND is_active = 1 "
                "ORDER BY version DESC LIMIT 1"
            )

            pe_system = ""
            pe_user_template = ""
            if prompt_row:
                pe_system = prompt_row.get("system_prompt") or ""
                pe_user_template = prompt_row.get("user_prompt_template") or ""
                # If source_model_assignments didn't have a model, fall back to PE model
                if not model_id:
                    model_id = prompt_row.get("model") or "openai/gpt-4.1-mini"
                    provider = prompt_row.get("model_provider") or "openrouter"
            else:
                logger.warning("[Idea Check] No active prompt in Prompt Engineer for role='source_tradingview'. Using defaults.")

            # Ultimate fallback
            if not model_id:
                model_id = "openai/gpt-4.1-mini"
                provider = "openrouter"

            logger.info(f"[Idea Check] Using model {provider}/{model_id} for {idea_id} check #{check_number}")

            # ── Extract trade levels (entry / SL / TP) ──
            entry_price = idea.get("entry_price")
            target_price = idea.get("target_price")
            stop_price = idea.get("stop_price")
            # If DB levels are empty, try to extract from original news item description
            if not entry_price and not target_price and not stop_price:
                news_item_id = idea.get("news_item_id")
                if news_item_id:
                    ni_row = db.fetch_one(
                        "SELECT detail FROM news_items WHERE id = %s LIMIT 1",
                        (news_item_id,)
                    )
                    desc_text = (ni_row.get("detail") or "")[:2000] if ni_row else ""
                else:
                    desc_text = ""
            else:
                desc_text = ""

            trade_levels = ""
            if entry_price or target_price or stop_price:
                trade_levels = "\nTRADE LEVELS (from author):\n"
                if entry_price:
                    trade_levels += f"- Entry: {entry_price}\n"
                if stop_price:
                    trade_levels += f"- Stop Loss: {stop_price}\n"
                if target_price:
                    trade_levels += f"- Take Profit / Target: {target_price}\n"
            elif desc_text:
                trade_levels = (
                    "\nORIGINAL IDEA DESCRIPTION (may contain Entry, Stop Loss, Take Profit):\n"
                    f"{desc_text}\n"
                )

            # ── Build history section from ALL previous snapshots ──
            history_lines = []
            prev_score = None
            for snap in prev_snapshots:
                s_num = snap.get("check_number", 0)
                s_label = snap.get("check_interval_label", "")
                s_analysis = snap.get("ai_analysis", "")[:500]
                s_score = snap.get("ai_score")
                s_date = snap.get("created_at", "")
                if isinstance(s_date, datetime):
                    s_date = s_date.strftime("%Y-%m-%d %H:%M")
                history_lines.append(
                    f"--- CHECK #{s_num} ({s_label}, {s_date}) ---\n"
                    f"Score: {s_score}/100\n"
                    f"{s_analysis}\n"
                )
                if s_score is not None:
                    prev_score = float(s_score)

            history_text = "\n".join(history_lines) if history_lines else "No previous checks."

            # Build comment summary
            comment_summary = ""
            if comments:
                top = sorted(comments, key=lambda x: x.get("likes", 0), reverse=True)[:5]
                lines = []
                for c in top:
                    a = c.get("author", "anon")
                    t = c.get("text", "")[:200]
                    l = c.get("likes", 0)
                    lines.append(f"  @{a} ({l} likes): {t}")
                comment_summary = "\n".join(lines)

            # ── Build the context data that gets injected into the prompt ──
            is_first_check = check_number == 1

            # Template variables available for {substitution} in user_prompt_template
            template_vars = {
                "check_number": check_number,
                "symbol": symbol,
                "direction": direction,
                "timeframe": tf,
                "author": author,
                "posted_at": str(idea.get("created_at", "unknown")),
                "trade_levels": trade_levels,
                "initial_analysis": (initial_analysis[:1500] if initial_analysis else "No initial analysis."),
                "history": history_text,
                "comments_count": len(comments),
                "comments": comment_summary or "No comments yet.",
                "has_screenshot": "YES — chart screenshot is attached as an image" if screenshot_path else "NO screenshot",
                "is_first_check": "true" if is_first_check else "false",
            }

            # ── Build system prompt: use Prompt Engineer's, or fallback ──
            if pe_system:
                system_prompt = pe_system
            else:
                system_prompt = (
                    "You are JarvAIs Idea Monitor. You analyze TradingView trading ideas by "
                    "examining chart screenshots and tracking their accuracy over time. "
                    "Be brutally honest about whether trades are winning or losing. "
                    "If a stop loss has been hit, score LOW. If a target has been hit, score HIGH."
                )

            # ── Build user prompt: use Prompt Engineer's template, or fallback ──
            if pe_user_template:
                try:
                    user_prompt = pe_user_template.format(**template_vars)
                except KeyError as fmt_err:
                    logger.warning(f"[Idea Check] Template format error: {fmt_err}. Using fallback prompt.")
                    user_prompt = self._build_fallback_user_prompt(template_vars, is_first_check)
            else:
                user_prompt = self._build_fallback_user_prompt(template_vars, is_first_check)

            # ── Build messages — with vision image if screenshot available ──
            if screenshot_path and os.path.exists(screenshot_path):
                with open(screenshot_path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode("utf-8")
                user_content = [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{img_b64}",
                        "detail": "high"
                    }}
                ]
                logger.info(f"[Idea Check] Sending screenshot to AI ({len(img_b64)//1024}KB base64)")
            else:
                user_content = user_prompt

            # ── Call AI via model_interface ──
            result_response = model_interface.query_with_model(
                model_id=model_id,
                provider=provider,
                role="idea_check",
                system_prompt=system_prompt,
                user_prompt=user_content,
                max_tokens=800,
                temperature=0.3,
                context="idea_check",
                source="tradingview_ideas",
                source_detail=f"idea_{idea_id}",
                author=idea.get("author"),
                news_item_id=idea.get("news_item_id"),
                media_type="image" if screenshot_path else "text",
            )

            if not result_response.success:
                logger.error(f"[Idea Check] AI call failed: {result_response.error_message}")
                return {"analysis": f"AI error: {result_response.error_message}",
                        "score": None, "opinion_changed": False, "status": "tracking"}

            analysis = result_response.content
            logger.info(f"AI analysis for {idea_id} check #{check_number}: {len(analysis)} chars "
                        f"(model={model_id}, cost=${result_response.cost_usd:.4f})")

            # Parse structured fields from the response
            result = {"analysis": analysis, "score": None, "opinion_changed": False,
                      "end_date": None, "status": "tracking"}

            for line in analysis.split("\n"):
                line = line.strip()
                if line.startswith("SCORE:"):
                    try:
                        score_str = line.replace("SCORE:", "").strip()
                        result["score"] = float(score_str.split("/")[0].strip())
                    except:
                        pass
                elif line.startswith("OPINION_CHANGED:"):
                    val = line.replace("OPINION_CHANGED:", "").strip().upper()
                    result["opinion_changed"] = val == "YES"
                elif line.startswith("STATUS:"):
                    val = line.replace("STATUS:", "").strip().upper()
                    status_map = {
                        "TRACKING": "tracking",
                        "HIT_TARGET": "hit_target",
                        "HIT_STOP": "hit_stop",
                        "EXPIRED": "expired",
                    }
                    result["status"] = status_map.get(val, "tracking")
                elif line.startswith("END_DATE:"):
                    try:
                        date_str = line.replace("END_DATE:", "").strip()
                        result["end_date"] = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
                    except:
                        try:
                            result["end_date"] = datetime.strptime(date_str[:10], "%Y-%m-%d")
                        except:
                            pass
                elif line.startswith("ENTRY:"):
                    result["entry_price"] = line.replace("ENTRY:", "").strip()
                elif line.startswith("STOP_LOSS:"):
                    result["stop_loss"] = line.replace("STOP_LOSS:", "").strip()
                elif line.startswith("TAKE_PROFIT:"):
                    result["take_profit"] = line.replace("TAKE_PROFIT:", "").strip()

            # On first check, save extracted trade levels to tracking table
            if is_first_check and (result.get("entry_price") or result.get("stop_loss") or result.get("take_profit")):
                try:
                    import re as re_mod
                    def _parse_price(s):
                        """Extract first number from a price string like '68,500-68,600' or '$69.9K'."""
                        if not s:
                            return None
                        s = s.replace(",", "").replace("$", "").replace("K", "000").replace("k", "000")
                        m = re_mod.search(r"[\d.]+", s)
                        return float(m.group()) if m else None

                    ep = _parse_price(result.get("entry_price"))
                    sl = _parse_price(result.get("stop_loss"))
                    tp = _parse_price(result.get("take_profit"))
                    updates = []
                    params = []
                    if ep:
                        updates.append("entry_price = %s")
                        params.append(ep)
                    if sl:
                        updates.append("stop_price = %s")
                        params.append(sl)
                    if tp:
                        updates.append("target_price = %s")
                        params.append(tp)
                    if updates:
                        params.append(idea_id)
                        db.execute(
                            f"UPDATE tradingview_ideas_tracking SET {', '.join(updates)} WHERE idea_id = %s",
                            tuple(params)
                        )
                        logger.info(f"[Idea Check] Saved trade levels for {idea_id}: entry={ep}, SL={sl}, TP={tp}")
                except Exception as lvl_err:
                    logger.warning(f"[Idea Check] Failed to save trade levels: {lvl_err}")

            return result

        except Exception as e:
            logger.error(f"AI analysis failed: {e}")
            return {"analysis": f"AI analysis error: {e}", "score": None,
                    "opinion_changed": False, "end_date": None, "status": "tracking"}

    def _build_fallback_user_prompt(self, v: Dict, is_first_check: bool) -> str:
        """
        Build the default user prompt when no user_prompt_template is configured
        in the Prompt Engineer for source_tradingview. This is the fallback only.
        Users should configure their own prompt in the Prompt Engineer tab.
        """
        end_date_block = ""
        if is_first_check:
            end_date_block = (
                "\nIMPORTANT - FIRST CHECK EXTRAS:\n"
                "Estimate when this idea should have played out → END_DATE: YYYY-MM-DD HH:MM\n"
                "Extract trade levels from description/chart → ENTRY: [price], STOP_LOSS: [price], TAKE_PROFIT: [price]\n"
            )

        return f"""This is CHECK #{v['check_number']} for a TradingView idea.

ORIGINAL IDEA:
- Symbol: {v['symbol']}
- Direction: {v['direction']}
- Timeframe: {v['timeframe']}
- Author: @{v['author']}
- Posted: {v['posted_at']}
{v['trade_levels']}
INITIAL AI CHART ANALYSIS:
{v['initial_analysis']}

FULL MONITORING HISTORY:
{v['history']}

COMMUNITY COMMENTS ({v['comments_count']} total):
{v['comments']}

SCREENSHOT: {v['has_screenshot']}

{end_date_block}

YOUR TASK:
1. If a screenshot is attached, LOOK AT THE CHART — compare current price to entry, stop loss, and target.
2. Price hit the stop loss → STATUS: HIT_STOP, score 0-30.
3. Price hit the target → STATUS: HIT_TARGET, score 80-100.
4. Score 0-100 based on how price is tracking vs prediction. Be HONEST.

FORMAT YOUR RESPONSE EXACTLY:
SCORE: [0-100]
OPINION_CHANGED: [YES/NO]
STATUS: [TRACKING / HIT_TARGET / HIT_STOP / EXPIRED]
{('ENTRY: [price]' if is_first_check else '')}
{('STOP_LOSS: [price]' if is_first_check else '')}
{('TAKE_PROFIT: [price]' if is_first_check else '')}
{('END_DATE: [YYYY-MM-DD HH:MM]' if is_first_check else '')}

ASSESSMENT:
[Reference the chart screenshot, mention specific price levels, 200 words max]

AUTHOR ACCURACY NOTE:
[Brief note on prediction quality]"""

    def _fetch_idea_comments(self, idea_id: str) -> List[Dict]:
        """Fetch comments for a TradingView idea."""
        import aiohttp

        HEADERS = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://www.tradingview.com/",
        }

        comments = []
        try:
            loop = asyncio.new_event_loop()
            try:
                comments = loop.run_until_complete(
                    self._async_fetch_comments(idea_id, HEADERS)
                )
            finally:
                loop.close()

            if comments:
                self._store_comments(idea_id, comments)
                self._stats["total_comments_fetched"] += len(comments)

        except Exception as e:
            logger.error(f"Failed to fetch comments for {idea_id}: {e}")

        return comments

    async def _async_fetch_comments(self, idea_id: str, headers: Dict) -> List[Dict]:
        """Async fetch comments from TradingView API."""
        import aiohttp

        url = f"https://www.tradingview.com/api/v1/ideas/{idea_id}/comments/"
        comments = []

        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=15), ssl=False
                ) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()

            results = data if isinstance(data, list) else data.get("results", [])
            for c in results:
                comments.append({
                    "comment_id": str(c.get("id", "")),
                    "author": c.get("user", {}).get("username", "unknown"),
                    "text": c.get("text", ""),
                    "likes": c.get("likes_count", 0),
                    "parent_id": str(c.get("parent_id", "")) if c.get("parent_id") else None,
                    "posted_at": c.get("created_at", ""),
                })

        except asyncio.TimeoutError:
            logger.warning(f"Comments API timeout for idea {idea_id}")
        except Exception as e:
            logger.error(f"Comments fetch error for {idea_id}: {e}")

        return comments

    def _store_comments(self, idea_id: str, comments: List[Dict]):
        """Store comments in the idea_comments table (upsert)."""
        try:
            from db.database import get_db
            db = get_db()

            for c in comments:
                comment_id = c.get("comment_id", "")
                if not comment_id:
                    continue
                try:
                    db.execute("""
                        INSERT INTO idea_comments
                        (idea_id, comment_id, author, text, likes, parent_id, posted_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            likes = VALUES(likes),
                            text = VALUES(text)
                    """, (
                        idea_id, comment_id,
                        c.get("author", ""),
                        c.get("text", ""),
                        c.get("likes", 0),
                        c.get("parent_id"),
                        c.get("posted_at") or None,
                    ))
                except Exception as e:
                    logger.debug(f"Comment store error: {e}")

        except Exception as e:
            logger.error(f"Store comments error: {e}")

    def get_idea_history(self, idea_id: str) -> Dict:
        """Get full tracking history for an idea (for the detail modal)."""
        try:
            from db.database import get_db
            db = get_db()

            tracking = db.fetch_one("""
                SELECT * FROM tradingview_ideas_tracking WHERE idea_id = %s
            """, (idea_id,))

            snapshots = db.fetch_all("""
                SELECT * FROM idea_snapshots
                WHERE idea_id = %s
                ORDER BY check_number ASC
            """, (idea_id,))

            comments = db.fetch_all("""
                SELECT * FROM idea_comments
                WHERE idea_id = %s
                ORDER BY posted_at ASC
            """, (idea_id,))

            news_item = db.fetch_one("""
                SELECT * FROM news_items
                WHERE external_id = %s AND source_detail = 'ideas'
                LIMIT 1
            """, (idea_id,))

            return {
                "tracking": tracking,
                "snapshots": snapshots or [],
                "comments": comments or [],
                "news_item": news_item,
            }

        except Exception as e:
            logger.error(f"Get idea history error: {e}")
            return {"tracking": None, "snapshots": [], "comments": [], "news_item": None}

    def get_status(self) -> Dict:
        """Get monitor service status."""
        try:
            from db.database import get_db
            db = get_db()
            counts = db.fetch_one("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status='tracking' THEN 1 ELSE 0 END) as tracking,
                    SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) as expired,
                    SUM(CASE WHEN status='hit_target' THEN 1 ELSE 0 END) as hit_target,
                    SUM(CASE WHEN status='hit_stop' THEN 1 ELSE 0 END) as hit_stop
                FROM tradingview_ideas_tracking
            """)

            # Get next due ideas
            next_due = db.fetch_all("""
                SELECT idea_id, symbol, next_check_at, check_count
                FROM tradingview_ideas_tracking
                WHERE status = 'tracking' AND next_check_at IS NOT NULL
                ORDER BY next_check_at ASC
                LIMIT 5
            """)
        except:
            counts = {}
            next_due = []

        return {
            "running": self._running,
            "stats": self._stats,
            "tracked_ideas": counts or {},
            "next_due": next_due or [],
        }


# Singleton
_monitor: Optional[IdeaMonitorService] = None


def get_idea_monitor() -> IdeaMonitorService:
    """Get or create the shared IdeaMonitorService instance."""
    global _monitor
    if _monitor is None:
        _monitor = IdeaMonitorService()
    return _monitor


def start_idea_monitor() -> IdeaMonitorService:
    """Convenience function to start the monitor."""
    monitor = get_idea_monitor()
    monitor.start()
    return monitor
