"""
Module: cron_canary
Purpose: Monitor cron heartbeats and alert on staleness.
Location: /opt/tickles/shared/intelligence/cron_canary.py
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from shared.dashboard.telegram import sender_from_env
from shared.utils.db import DatabasePool

logger = logging.getLogger(__name__)

# Constants
CHECK_INTERVAL_SECONDS = 60
STALENESS_THRESHOLD_MULTIPLIER = 2.5  # Alert if missed > 2.5x expected interval

class CronCanary:
    """
    Daemon that monitors the public.cron_heartbeats table.
    If an agent hasn't checked in within its expected interval, sends a Telegram alert.
    """

    def __init__(self) -> None:
        self.telegram = sender_from_env()
        self.admin_chat_id = os.environ.get("TICKLES_ADMIN_CHAT_ID")
        self._last_alerts: Dict[str, datetime] = {}  # agent_id -> last_alert_ts
        self._alert_cooldown = timedelta(hours=1)

    async def run_forever(self) -> None:
        """Main loop for the canary daemon."""
        logger.info("CronCanary starting...")
        if not self.admin_chat_id:
            logger.warning("TICKLES_ADMIN_CHAT_ID not set. Alerts will be logged but not sent.")

        while True:
            try:
                await self.check_heartbeats()
            except Exception as e:
                logger.error(f"Error in CronCanary check loop: {e}", exc_info=True)
            
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def check_heartbeats(self) -> None:
        """Query heartbeats and identify stale agents."""
        pool = await DatabasePool.get_instance()
        
        # Fetch all heartbeats
        rows = await pool.fetch_all(
            "SELECT agent_id, last_heartbeat_at, status, expected_interval_seconds, consecutive_failures "
            "FROM public.cron_heartbeats"
        )

        now = datetime.now(timezone.utc)
        stale_agents: List[dict] = []

        for row in rows:
            agent_id = row["agent_id"]
            last_ts = row["last_heartbeat_at"]
            interval = row["expected_interval_seconds"]
            
            # Ensure last_ts is timezone-aware
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)

            # Calculate staleness
            seconds_since = (now - last_ts).total_seconds()
            threshold = interval * STALENESS_THRESHOLD_MULTIPLIER

            if seconds_since > threshold:
                stale_agents.append({
                    "agent_id": agent_id,
                    "seconds_since": seconds_since,
                    "expected": interval,
                    "status": row["status"]
                })

        if stale_agents:
            await self._process_stale_agents(stale_agents)

    async def _process_stale_agents(self, stale_agents: List[dict]) -> None:
        """Send alerts for stale agents with cooldown logic."""
        now = datetime.now(timezone.utc)
        
        for agent in stale_agents:
            agent_id = agent["agent_id"]
            last_alert = self._last_alerts.get(agent_id)

            if last_alert is None or (now - last_alert) > self._alert_cooldown:
                msg = (
                    f"💀 <b>STALE AGENT: {agent_id}</b>\n"
                    f"Last seen: {int(agent['seconds_since'] / 60)}m ago\n"
                    f"Expected every: {agent['expected']}s\n"
                    f"Last status: {agent['status']}"
                )
                
                logger.warning(f"Alerting for stale agent {agent_id}")
                
                if self.admin_chat_id:
                    try:
                        await self.telegram.send_alert(self.admin_chat_id, msg)
                        self._last_alerts[agent_id] = now
                    except Exception as e:
                        logger.error(f"Failed to send Telegram alert for {agent_id}: {e}")
                else:
                    logger.info(f"DRY RUN ALERT: {msg}")
                    self._last_alerts[agent_id] = now

if __name__ == "__main__":
    # Simple entry point for manual testing
    logging.basicConfig(level=logging.INFO)
    canary = CronCanary()
    asyncio.run(canary.run_forever())
