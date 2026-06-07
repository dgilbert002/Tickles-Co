"""
Module: pipeline_watchdog
Purpose: DATA-freshness watchdog for the signal pipeline (collectors → media →
         interpretation → tracked_positions). Prevents silent stalls.
Location: /opt/tickles/shared/intelligence/pipeline_watchdog.py

WHY THIS EXISTS (2026-05-29 incident)
-------------------------------------
The existing CronCanary only watches PROCESS heartbeats — "did the service run?".
On 2026-05-29 the telegram collector AND the interpretation service were both
"active" in systemd, yet the pipeline produced ZERO fresh data for ~12.5 hours:
fresh media piled up in `downloaded` while every interpretation cycle reported
`analyzed=0`. A liveness check cannot catch that class of failure — the process
is alive, it's the DATA that stopped flowing.

This watchdog checks the actual data tables and reacts:

  1. INTERPRETATION STALL (high confidence): fresh, claimable image media
     (news < max_age, media row older than a short grace) sitting unprocessed
     means the interpreter is wedged → restart `tickles-interpretation`.

  2. COLLECTOR SILENCE (peer-relative): a collector whose last DB write is far
     older than a *peer* collector that is still writing is almost certainly
     hung (not just a quiet market) → restart that collector's unit.

  3. HEARTBEAT + ALERT: records its own heartbeat and emits a Telegram alert
     (cooldowned) so a human is told, and the dashboard can surface freshness.

SAFETY
------
  * Auto-restart is gated by WATCHDOG_AUTO_RESTART (default "1") and a per-unit
    cooldown (RESTART_COOLDOWN_S) so it can never restart-loop.
  * All restarts/alerts are best-effort and never raise into the loop.
  * Restarting these units is idempotent: the interpreter re-claims work, the
    collectors reconnect; no data is lost.

Run:  python3 -m shared.intelligence.pipeline_watchdog
Unit: tickles-pipeline-watchdog.service (User=root so systemctl works)
"""
import asyncio
import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Dict, Optional

from shared.utils.db import get_shared_pool
from shared.intelligence.heartbeat import record_heartbeat

logger = logging.getLogger("tickles.pipeline_watchdog")

# ── Tunables (env-overridable) ──────────────────────────────────────────────
CHECK_INTERVAL_S      = int(os.environ.get("WATCHDOG_CHECK_INTERVAL_S", "300"))   # 5 min
# A fresh claimable image that's been waiting longer than this with no analysis
# means the interpreter is stalled (a healthy cycle picks it up within minutes).
INTERP_STALL_GRACE_MIN = int(os.environ.get("WATCHDOG_INTERP_GRACE_MIN", "20"))
# Mirror the interpreter's freshness window so "claimable" matches reality.
INTERP_MAX_AGE_H       = float(os.environ.get("INTERPRETATION_MAX_AGE_H", "24"))
# A collector silent this long WHILE a peer is fresh = hung (not a quiet market).
COLLECTOR_SILENCE_H    = float(os.environ.get("WATCHDOG_COLLECTOR_SILENCE_H", "6"))
RESTART_COOLDOWN_S     = int(os.environ.get("WATCHDOG_RESTART_COOLDOWN_S", "1800"))  # 30 min
AUTO_RESTART           = os.environ.get("WATCHDOG_AUTO_RESTART", "1") == "1"
# Collector auto-restart is OFF by default: a low-volume feed (e.g. telegram with
# a single channel) can be legitimately quiet for >6h overnight, and restarting
# it on a peer-relative gap would churn without cause. Default = ALERT-ONLY so a
# human decides. Set WATCHDOG_RESTART_COLLECTORS=1 to let it auto-restart.
RESTART_COLLECTORS     = os.environ.get("WATCHDOG_RESTART_COLLECTORS", "0") == "1"
ALERT_COOLDOWN_S       = int(os.environ.get("WATCHDOG_ALERT_COOLDOWN_S", "1800"))

# source (news_items.source) → systemd unit that produces it
COLLECTOR_UNITS: Dict[str, str] = {
    "discord":  "tickles-discord-collector.service",
    "telegram": "tickles-telegram-collector.service",
}
INTERPRETATION_UNIT = "tickles-interpretation.service"


class PipelineWatchdog:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._last_restart: Dict[str, datetime] = {}   # unit → ts
        self._last_alert: Dict[str, datetime] = {}      # key → ts
        try:
            from shared.dashboard.telegram import sender_from_env
            self._telegram = sender_from_env()
        except Exception:
            self._telegram = None
        self._admin_chat = os.environ.get("TICKLES_ADMIN_CHAT_ID")

    # ── helpers ──────────────────────────────────────────────────────────
    def _cooldown_ok(self, store: Dict[str, datetime], key: str, window_s: int) -> bool:
        now = datetime.now(timezone.utc)
        last = store.get(key)
        if last is None or (now - last).total_seconds() > window_s:
            store[key] = now
            return True
        return False

    async def _alert(self, key: str, message: str) -> None:
        """Best-effort cooldowned Telegram alert (also always logged)."""
        logger.warning("WATCHDOG: %s", message)
        if not self._cooldown_ok(self._last_alert, key, ALERT_COOLDOWN_S):
            return
        if self._telegram and self._admin_chat:
            try:
                await self._telegram.send_alert(self._admin_chat, message)
            except Exception as exc:  # pragma: no cover - network
                logger.error("watchdog alert send failed: %s", exc)

    def _restart_unit(self, unit: str) -> bool:
        """Restart a systemd unit, honouring the per-unit cooldown. Returns True
        if a restart was actually issued."""
        if not AUTO_RESTART:
            logger.info("WATCHDOG: auto-restart disabled; would have restarted %s", unit)
            return False
        if not self._cooldown_ok(self._last_restart, unit, RESTART_COOLDOWN_S):
            logger.info("WATCHDOG: %s within restart cooldown — skipping", unit)
            return False
        try:
            subprocess.run(["systemctl", "restart", unit], check=True, timeout=60)
            logger.warning("WATCHDOG: restarted %s", unit)
            return True
        except Exception as exc:
            logger.error("WATCHDOG: failed to restart %s: %s", unit, exc)
            return False

    # ── checks ───────────────────────────────────────────────────────────
    async def _check_interpretation_stall(self, pool) -> Optional[str]:
        """Return a problem description if the interpreter is wedged, else None."""
        row = await pool.fetch_one(
            """
            SELECT count(*) AS n
            FROM public.media_items m
            JOIN public.news_items n ON n.id = m.news_item_id
            WHERE m.processing_status IN ('downloaded','pending')
              AND m.media_type = 'image'
              AND n.collected_at > now() - ($1 || ' hours')::interval
              AND m.created_at  < now() - ($2 || ' minutes')::interval
            """,
            (str(INTERP_MAX_AGE_H), str(INTERP_STALL_GRACE_MIN)),
        )
        n = int(row["n"]) if row else 0
        if n > 0:
            return (f"{n} fresh chart(s) have been waiting >"
                    f"{INTERP_STALL_GRACE_MIN}m unprocessed — interpreter stalled")
        return None

    async def _check_failure_rate(self, pool) -> Optional[str]:
        """Detect the 'everything fails' mode (e.g. a code/model bug marks every
        chart 'failed'). A restart won't fix a NameError, so this ALERTS only.

        Fires when recent media is failing AND essentially nothing is analysing —
        exactly the 2026-05-29 `prefilter_version_label` NameError signature.
        """
        row = await pool.fetch_one(
            """
            SELECT
                count(*) FILTER (WHERE processing_status = 'failed')   AS failed,
                count(*) FILTER (WHERE processing_status = 'analyzed')  AS analyzed
            FROM public.media_items
            WHERE processed_at > now() - interval '60 minutes'
            """
        )
        failed = int(row["failed"]) if row else 0
        analyzed = int(row["analyzed"]) if row else 0
        if failed >= 3 and analyzed == 0:
            return (f"{failed} media failed and 0 analysed in the last hour — "
                    f"vision step is erroring (likely code/model bug)")
        return None

    async def _check_collectors(self, pool) -> Dict[str, float]:
        """Return {source: hours_since_last_write} for known collector sources."""
        rows = await pool.fetch_all(
            """
            SELECT source, EXTRACT(EPOCH FROM (now() - max(collected_at)))/3600.0 AS age_h
            FROM public.news_items
            WHERE source = ANY($1)
            GROUP BY source
            """,
            (list(COLLECTOR_UNITS.keys()),),
        )
        return {r["source"]: float(r["age_h"]) for r in rows if r["age_h"] is not None}

    async def check_once(self) -> None:
        pool = await get_shared_pool()
        problems = []

        # 1. Interpretation stall — high-confidence, auto-restart.
        try:
            stall = await self._check_interpretation_stall(pool)
            if stall:
                problems.append(stall)
                restarted = self._restart_unit(INTERPRETATION_UNIT)
                await self._alert(
                    "interp_stall",
                    f"🔧 Interpretation pipeline stalled: {stall}. "
                    f"{'Auto-restarted.' if restarted else 'Restart skipped (cooldown/disabled).'}",
                )
        except Exception as exc:
            logger.error("interpretation-stall check failed: %s", exc)

        # 1b. Failure-rate — alert-only (a restart can't fix a code/model bug).
        try:
            failing = await self._check_failure_rate(pool)
            if failing:
                problems.append(failing)
                await self._alert(
                    "failure_rate",
                    f"❌ Vision pipeline failing: {failing}. "
                    f"NOT auto-restarted (a restart won't fix a code/model bug) — "
                    f"check /var/log/tickles/interpretation.log.",
                )
        except Exception as exc:
            logger.error("failure-rate check failed: %s", exc)

        # 2. Collector silence — peer-relative. Only act when at least one peer
        #    is fresh (so a globally quiet market never trips it).
        try:
            ages = await self._check_collectors(pool)
            if ages:
                freshest = min(ages.values())
                peer_is_fresh = freshest < COLLECTOR_SILENCE_H
                for source, age_h in ages.items():
                    if age_h >= COLLECTOR_SILENCE_H and peer_is_fresh:
                        unit = COLLECTOR_UNITS[source]
                        problems.append(
                            f"{source} silent {age_h:.1f}h while a peer is fresh")
                        restarted = self._restart_unit(unit) if RESTART_COLLECTORS else False
                        if restarted:
                            tail = "Auto-restarted."
                        elif RESTART_COLLECTORS:
                            tail = "Restart skipped (cooldown)."
                        else:
                            tail = "Alert-only (set WATCHDOG_RESTART_COLLECTORS=1 to auto-restart)."
                        await self._alert(
                            f"collector_{source}",
                            f"🔇 Collector '{source}' silent {age_h:.1f}h while a "
                            f"peer feed is live — likely hung. {tail}",
                        )
        except Exception as exc:
            logger.error("collector check failed: %s", exc)

        # 3. Heartbeat (so the watcher itself is watched + dashboard sees it).
        status = "ok" if not problems else "partial"
        msg = "; ".join(problems) if problems else "pipeline fresh"
        try:
            await record_heartbeat(
                "pipeline-watchdog", status,
                expected_interval_seconds=CHECK_INTERVAL_S, message=msg[:500],
            )
        except Exception as exc:
            logger.error("watchdog heartbeat failed: %s", exc)

        logger.info("WATCHDOG cycle: %s", msg)

    async def run_forever(self) -> None:
        logger.info(
            "PipelineWatchdog starting (interval=%ds, grace=%dm, collector_silence=%.1fh, "
            "auto_restart=%s)",
            CHECK_INTERVAL_S, INTERP_STALL_GRACE_MIN, COLLECTOR_SILENCE_H, AUTO_RESTART,
        )
        while not self._stop.is_set():
            try:
                await self.check_once()
            except Exception as exc:
                logger.exception("watchdog cycle failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=CHECK_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
        logger.info("PipelineWatchdog stopped")

    def stop(self) -> None:
        self._stop.set()


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    wd = PipelineWatchdog()
    import signal as _signal
    loop = asyncio.get_running_loop()
    for s in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(s, wd.stop)
        except NotImplementedError:  # pragma: no cover
            pass
    await wd.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
