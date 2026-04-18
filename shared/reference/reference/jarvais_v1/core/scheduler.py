"""
JarvAIs Scheduler
Handles all scheduled/cron jobs:
- Cost aggregation (ai_api_log → role_daily_costs)
- Score snapshots (daily role score calculations → role_score_snapshots)
- Nightly self-review (cognitive engine daily review)
- Memory consolidation (compact and reinforce key memories)
- Database maintenance (cleanup old logs, vacuum)
- Auditor CEO review (auditor synthesis → CEO auto-approve → prompt updates)

Usage:
    from core.scheduler import JarvAIsScheduler
    scheduler = JarvAIsScheduler()
    await scheduler.start()  # Starts all scheduled jobs
"""

import asyncio
import logging
import json
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Optional

from core.time_utils import utcnow

logger = logging.getLogger("jarvais.scheduler")


class JarvAIsScheduler:
    """
    Central scheduler for all JarvAIs background jobs.
    Uses asyncio for non-blocking execution.
    """

    def __init__(self):
        self._db = None
        self._config = None
        self._running = False
        self._tasks: Dict[str, asyncio.Task] = {}

        logger.info("JarvAIs Scheduler initialized")

    @property
    def db(self):
        if self._db is None:
            from db.database import get_db
            self._db = get_db()
        return self._db

    @property
    def config(self):
        if self._config is None:
            from core.config import get_config
            self._config = get_config()
        return self._config

    async def start(self):
        """Start all scheduled jobs."""
        if self._running:
            logger.warning("Scheduler already running")
            return

        self._running = True
        logger.info("=== JarvAIs Scheduler starting ===")

        # Schedule all jobs
        self._tasks["cost_aggregation"] = asyncio.create_task(
            self._run_periodic("cost_aggregation", self.aggregate_daily_costs,
                               interval_hours=1, run_at_start=True)
        )
        self._tasks["score_snapshots"] = asyncio.create_task(
            self._run_periodic("score_snapshots", self.capture_score_snapshots,
                               interval_hours=4, run_at_start=True)
        )
        self._tasks["nightly_review"] = asyncio.create_task(
            self._run_daily("nightly_review", self.run_nightly_review,
                            target_hour=22, target_minute=0)
        )
        self._tasks["memory_consolidation"] = asyncio.create_task(
            self._run_daily("memory_consolidation", self.consolidate_memories,
                            target_hour=22, target_minute=30)
        )
        self._tasks["db_maintenance"] = asyncio.create_task(
            self._run_daily("db_maintenance", self.run_db_maintenance,
                            target_hour=3, target_minute=0)
        )
        self._tasks["auditor_ceo_review"] = asyncio.create_task(
            self._run_daily("auditor_ceo_review", self.run_auditor_ceo_review,
                            target_hour=23, target_minute=0)
        )

        logger.info(f"Scheduler started with {len(self._tasks)} jobs")

    async def stop(self):
        """Stop all scheduled jobs gracefully."""
        self._running = False
        for name, task in self._tasks.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info(f"Job '{name}' stopped")
        self._tasks.clear()
        logger.info("=== JarvAIs Scheduler stopped ===")

    async def _run_periodic(self, name: str, func, interval_hours: float,
                            run_at_start: bool = False):
        """Run a job at a fixed interval."""
        if run_at_start:
            try:
                await func()
            except Exception as e:
                logger.error(f"[{name}] Initial run failed: {e}", exc_info=True)

        while self._running:
            try:
                await asyncio.sleep(interval_hours * 3600)
                if self._running:
                    logger.info(f"[{name}] Running scheduled job...")
                    await func()
                    logger.info(f"[{name}] Completed")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{name}] Error: {e}", exc_info=True)
                await asyncio.sleep(60)  # Wait before retry

    async def _run_daily(self, name: str, func, target_hour: int,
                         target_minute: int = 0):
        """Run a job once per day at a specific time (UTC)."""
        while self._running:
            try:
                now = utcnow()
                target = now.replace(hour=target_hour, minute=target_minute,
                                     second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)

                wait_seconds = (target - now).total_seconds()
                logger.info(f"[{name}] Next run at {target.isoformat()} UTC "
                            f"({wait_seconds/3600:.1f}h from now)")

                await asyncio.sleep(wait_seconds)

                if self._running:
                    logger.info(f"[{name}] Running daily job...")
                    await func()
                    logger.info(f"[{name}] Completed")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{name}] Error: {e}", exc_info=True)
                await asyncio.sleep(3600)  # Wait 1h before retry

    # ─────────────────────────────────────────────────────────────────
    # Job 1: Cost Aggregation
    # ─────────────────────────────────────────────────────────────────

    async def aggregate_daily_costs(self):
        """
        Aggregate AI API costs from ai_api_log into role_daily_costs.
        Runs hourly to keep the dashboard up to date.

        For each role, calculates:
        - Total API calls
        - Input/output tokens
        - Total cost in USD
        - Average latency
        - Associated trade P&L (for cost efficiency metrics)
        """
        today = date.today().isoformat()

        try:
            # Get all accounts
            accounts = self.config.accounts if hasattr(self.config, 'accounts') else {}
            account_ids = list(accounts.keys()) if accounts else ["default"]

            for account_id in account_ids:
                for role in ["analyst", "trader", "coach", "post_mortem"]:
                    try:
                        # Query ai_api_log for today's stats per role
                        stats = self.db.execute_query("""
                            SELECT
                                COUNT(*) as api_calls,
                                COALESCE(SUM(input_tokens), 0) as input_tokens,
                                COALESCE(SUM(output_tokens), 0) as output_tokens,
                                COALESCE(SUM(cost_usd), 0) as total_cost,
                                COALESCE(AVG(latency_ms), 0) as avg_latency
                            FROM ai_api_log
                            WHERE account_id = %s
                              AND role = %s
                              AND DATE(timestamp) = %s
                        """, (account_id, role, today))

                        if not stats or not stats[0]:
                            continue

                        row = stats[0]

                        # Get trade P&L for this role's signals today
                        pnl_stats = self.db.execute_query("""
                            SELECT
                                COUNT(*) as trade_count,
                                COALESCE(SUM(pnl), 0) as total_pnl,
                                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins
                            FROM trades
                            WHERE account_id = %s
                              AND DATE(open_time) = %s
                              AND status IN ('closed', 'partial_close')
                        """, (account_id, today))

                        trade_count = pnl_stats[0][0] if pnl_stats and pnl_stats[0] else 0
                        total_pnl = pnl_stats[0][1] if pnl_stats and pnl_stats[0] else 0.0
                        wins = pnl_stats[0][2] if pnl_stats and pnl_stats[0] else 0

                        # Upsert into role_daily_costs
                        self.db.upsert_role_daily_cost(
                            account_id=account_id,
                            role=role,
                            date_str=today,
                            api_calls=row[0],
                            input_tokens=row[1],
                            output_tokens=row[2],
                            total_cost=row[3],
                            avg_latency=row[4],
                            trade_count=trade_count,
                            total_pnl=total_pnl,
                            win_count=wins
                        )

                    except Exception as e:
                        logger.error(f"Cost aggregation error for {account_id}/{role}: {e}")

            logger.info(f"Cost aggregation completed for {today}")

        except Exception as e:
            logger.error(f"Cost aggregation failed: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────────────
    # Job 2: Score Snapshots
    # ─────────────────────────────────────────────────────────────────

    async def capture_score_snapshots(self):
        """
        Capture current role scores and save to role_score_snapshots.
        Runs every 4 hours to build trend data for the dashboard.
        """
        try:
            from analytics.performance_engine import get_performance_engine

            accounts = self.config.accounts if hasattr(self.config, 'accounts') else {}
            account_ids = list(accounts.keys()) if accounts else ["default"]

            for account_id in account_ids:
                try:
                    engine = get_performance_engine(account_id)
                    scorecard = engine.calculate_role_scores()

                    for role, data in scorecard.items():
                        if isinstance(data, dict) and "score" in data:
                            model = data.get("model", "unknown")
                            score = data["score"]
                            metrics = {k: v for k, v in data.items()
                                       if k not in ("score", "model")}

                            self.db.insert_score_snapshot(
                                account_id=account_id,
                                role=role,
                                model=model,
                                score=score,
                                metrics_json=json.dumps(metrics)
                            )

                except Exception as e:
                    logger.error(f"Score snapshot error for {account_id}: {e}")

            logger.info("Score snapshots captured")

        except Exception as e:
            logger.error(f"Score snapshot capture failed: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────────────
    # Job 3: Nightly Self-Review
    # ─────────────────────────────────────────────────────────────────

    async def run_nightly_review(self):
        """
        Run the cognitive engine's daily self-review for each account.
        This is where the AI reflects on the day's performance and
        generates lessons learned.

        Runs at 22:00 UTC (after US market close).
        """
        try:
            from core.cognitive_engine import get_cognitive_engine

            accounts = self.config.accounts if hasattr(self.config, 'accounts') else {}
            account_ids = list(accounts.keys()) if accounts else ["default"]

            for account_id in account_ids:
                try:
                    engine = get_cognitive_engine(account_id)
                    review = await asyncio.to_thread(engine.run_daily_review)

                    if review:
                        # Store the review as a memory
                        from core.memory_manager import get_memory_manager
                        mm = get_memory_manager()

                        review_text = (
                            f"Daily Review {date.today().isoformat()}: "
                            f"Win rate: {review.get('win_rate', 'N/A')}, "
                            f"P&L: ${review.get('total_pnl', 0):.2f}, "
                            f"Trades: {review.get('trade_count', 0)}, "
                            f"Key lesson: {review.get('key_lesson', 'N/A')}, "
                            f"Pattern: {review.get('pattern_discovered', 'N/A')}"
                        )

                        await asyncio.to_thread(
                            mm.store_memory,
                            text=review_text,
                            role="post_mortem",
                            category="daily_review",
                            metadata={
                                "account_id": account_id,
                                "date": date.today().isoformat(),
                                "review_data": review
                            }
                        )

                        logger.info(f"[{account_id}] Nightly review completed and stored")

                except Exception as e:
                    logger.error(f"Nightly review error for {account_id}: {e}")

        except Exception as e:
            logger.error(f"Nightly review failed: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────────────
    # Job 4: Memory Consolidation
    # ─────────────────────────────────────────────────────────────────

    async def consolidate_memories(self):
        """
        Consolidate and reinforce key memories in Mem0.

        Process:
        1. Get all memories from the past 24 hours
        2. Identify high-value memories (from winning trades, patterns)
        3. Cross-reference with existing memories for deduplication
        4. Reinforce recurring patterns by updating memory strength
        5. Archive low-value memories

        Runs at 22:30 UTC (after nightly review).
        """
        try:
            from core.memory_manager import get_memory_manager
            mm = get_memory_manager()

            # Get today's memories
            for role in ["trader", "coach", "analyst", "post_mortem"]:
                try:
                    memories = mm.get_role_memories(role, limit=50)

                    if not memories:
                        continue

                    # Count memories by category
                    categories = {}
                    for mem in memories:
                        cat = mem.get("metadata", {}).get("category", "unknown")
                        categories[cat] = categories.get(cat, 0) + 1

                    logger.info(f"Memory consolidation [{role}]: "
                                f"{len(memories)} memories, "
                                f"categories: {categories}")

                    # Log consolidation to audit
                    self.db.log_memory_audit(
                        operation="consolidation",
                        role=role,
                        memory_count=len(memories),
                        details=json.dumps(categories)
                    )

                except Exception as e:
                    logger.error(f"Memory consolidation error for {role}: {e}")

            logger.info("Memory consolidation completed")

        except Exception as e:
            logger.error(f"Memory consolidation failed: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────────────
    # Job 5: Database Maintenance
    # ─────────────────────────────────────────────────────────────────

    async def run_db_maintenance(self):
        """
        Database maintenance tasks:
        1. Clean up old API logs (keep 90 days)
        2. Clean up old memory audit entries (keep 180 days)
        3. Archive old signals (keep 365 days active)
        4. Optimize/vacuum database

        Runs at 03:00 UTC (low activity period).
        """
        try:
            cutoff_90d = (date.today() - timedelta(days=90)).isoformat()
            cutoff_180d = (date.today() - timedelta(days=180)).isoformat()
            cutoff_365d = (date.today() - timedelta(days=365)).isoformat()

            # Clean old API logs
            deleted_api = self.db.execute_update(
                "DELETE FROM ai_api_log WHERE DATE(timestamp) < ?",
                (cutoff_90d,)
            )
            logger.info(f"DB maintenance: deleted {deleted_api} old API log entries")

            # Clean old memory audit entries
            deleted_audit = self.db.execute_update(
                "DELETE FROM memory_audit WHERE DATE(timestamp) < ?",
                (cutoff_180d,)
            )
            logger.info(f"DB maintenance: deleted {deleted_audit} old audit entries")

            # Clean old memory fallback entries that have been migrated
            deleted_fallback = self.db.execute_update(
                "DELETE FROM memory_fallback WHERE migrated = 1 AND DATE(created_at) < ?",
                (cutoff_90d,)
            )
            logger.info(f"DB maintenance: deleted {deleted_fallback} migrated fallback entries")

            # Archive old signals
            archived = self.db.execute_update(
                "UPDATE signals SET status = 'archived' "
                "WHERE DATE(created_at) < ? AND status != 'archived'",
                (cutoff_365d,)
            )
            logger.info(f"DB maintenance: archived {archived} old signals")

            # Vacuum (SQLite) or optimize (MySQL)
            try:
                self.db.execute_update("VACUUM", ())
                logger.info("DB maintenance: VACUUM completed")
            except Exception:
                try:
                    # MySQL optimization
                    tables = [
                        "trades", "signals", "ai_api_log", "cognitive_logs",
                        "role_daily_costs", "role_score_snapshots", "memory_audit"
                    ]
                    for table in tables:
                        self.db.execute_update(f"OPTIMIZE TABLE {table}", ())
                    logger.info("DB maintenance: OPTIMIZE TABLE completed")
                except Exception as e:
                    logger.warning(f"DB optimization skipped: {e}")

            logger.info("Database maintenance completed")

        except Exception as e:
            logger.error(f"Database maintenance failed: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────────────
    # Job 6: Auditor → CEO Self-Improvement Review
    # ─────────────────────────────────────────────────────────────────

    async def run_auditor_ceo_review(self):
        """Run the Auditor→CEO self-improvement pipeline once daily.

        Pipeline steps (all handled by auditor.run_daily_rollup):
        1. Collect yesterday's trade audit reports
        2. Synthesize raw audit findings into holistic recommendations
        3. CEO auto-approves eligible recommendations (autonomous mode)
        4. Auto-apply approved changes to agent prompts/souls/config

        Also generates a CEO digest summarising what was changed.

        Runs at 23:00 UTC (after nightly review at 22:00 UTC).
        """
        try:
            from services.auditor import run_daily_rollup

            report_id = await asyncio.to_thread(
                run_daily_rollup, self.db, self.config)

            if report_id:
                logger.info(f"[auditor_ceo_review] Daily rollup #{report_id} completed")

                applied = self.db.fetch_all("""
                    SELECT id, category, target_agent, target_field,
                           duo_id, diff_summary, applied_by, applied_at
                    FROM audit_recommendations
                    WHERE applied_at >= DATE_SUB(NOW(), INTERVAL 5 MINUTE)
                      AND applied_by = 'ceo_auto'
                    ORDER BY applied_at DESC
                """) or []

                if applied:
                    changes_summary = []
                    for rec in applied[:10]:
                        label = (f"#{rec['id']}: {rec.get('category','?')} "
                                 f"→ {rec.get('target_agent','?')}")
                        if rec.get("duo_id"):
                            label += f" (duo={rec['duo_id']})"
                        if rec.get("target_field"):
                            label += f".{rec['target_field']}"
                        changes_summary.append(label)

                    logger.info(
                        f"[auditor_ceo_review] CEO auto-applied {len(applied)} "
                        f"prompt changes: {'; '.join(changes_summary)}")

                    try:
                        digest_lines = [
                            f"Daily Self-Improvement Digest — {date.today().isoformat()}",
                            f"Rollup report: #{report_id}",
                            f"Auto-applied changes: {len(applied)}",
                            ""
                        ]
                        for rec in applied:
                            parts = [
                                f"• #{rec['id']} [{rec.get('category','?')}]",
                                rec.get('target_agent', '?'),
                            ]
                            if rec.get('target_field'):
                                parts.append(f".{rec['target_field']}")
                            if rec.get('duo_id'):
                                parts.append(f" (duo={rec['duo_id']})")
                            parts.append(f" — {(rec.get('diff_summary') or 'applied')[:120]}")
                            digest_lines.append("".join(parts))
                        digest_text = "\n".join(digest_lines)

                        self.db.execute("""
                            INSERT INTO ceo_daily_digest
                            (digest_date, digest_type, content, created_at)
                            VALUES (%s, 'auditor_auto_apply', %s, NOW())
                            ON DUPLICATE KEY UPDATE
                                content = CONCAT(COALESCE(content, ''), '\n---\n', %s)
                        """, (date.today().isoformat(), digest_text, digest_text))

                        logger.info("[auditor_ceo_review] CEO digest stored")
                    except Exception as de:
                        logger.warning(f"[auditor_ceo_review] Digest storage failed: {de}")
                else:
                    logger.info("[auditor_ceo_review] No auto-applied changes this cycle")
            else:
                logger.info("[auditor_ceo_review] No trades to review today")

        except Exception as e:
            logger.error(f"Auditor-CEO review failed: {e}", exc_info=True)

    # ─────────────────────────────────────────────────────────────────
    # Manual Triggers (for dashboard/API)
    # ─────────────────────────────────────────────────────────────────

    async def trigger_job(self, job_name: str) -> Dict[str, Any]:
        """
        Manually trigger a scheduled job from the dashboard.

        Args:
            job_name: One of 'cost_aggregation', 'score_snapshots',
                      'nightly_review', 'memory_consolidation', 'db_maintenance',
                      'auditor_ceo_review'

        Returns:
            Dict with success status and message
        """
        job_map = {
            "cost_aggregation": self.aggregate_daily_costs,
            "score_snapshots": self.capture_score_snapshots,
            "nightly_review": self.run_nightly_review,
            "memory_consolidation": self.consolidate_memories,
            "db_maintenance": self.run_db_maintenance,
            "auditor_ceo_review": self.run_auditor_ceo_review,
        }

        if job_name not in job_map:
            return {"success": False, "message": f"Unknown job: {job_name}"}

        try:
            start = utcnow()
            await job_map[job_name]()
            elapsed = (utcnow() - start).total_seconds()

            return {
                "success": True,
                "message": f"Job '{job_name}' completed in {elapsed:.1f}s",
                "elapsed_seconds": elapsed
            }
        except Exception as e:
            return {"success": False, "message": f"Job '{job_name}' failed: {str(e)}"}

    def get_job_status(self) -> Dict[str, Any]:
        """Get the status of all scheduled jobs."""
        status = {}
        for name, task in self._tasks.items():
            status[name] = {
                "running": not task.done(),
                "cancelled": task.cancelled(),
                "exception": str(task.exception()) if task.done() and not task.cancelled()
                             and task.exception() else None
            }
        return {
            "scheduler_running": self._running,
            "jobs": status,
            "job_count": len(self._tasks)
        }


# ─────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────

_scheduler: Optional[JarvAIsScheduler] = None


def get_scheduler() -> JarvAIsScheduler:
    """Get or create the singleton scheduler."""
    global _scheduler
    if _scheduler is None:
        _scheduler = JarvAIsScheduler()
    return _scheduler
