"""
Tickles cost shipper v2 — streams LLM usage from api_cost_log into Paperclip cost_events.
Replaces the OpenClaw-dependent v1. Reads from our own Postgres, not OpenClaw jsonl files.
"""
import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone

import aiohttp

sys.path.insert(0, "/opt/tickles")
from shared.utils.db import get_shared_pool
from shared.utils.config import load_env

logger = logging.getLogger("cost_shipper.v2")

PAPERCLIP_URL = "http://127.0.0.1:3100/api"
POLL_INTERVAL_S = int(os.environ.get("COST_SHIPPER_POLL_S", "60"))
CURSOR_FILE = "/var/lib/tickles/cost_shipper_cursor.txt"


async def get_last_shipped_ts():
    try:
        with open(CURSOR_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


async def save_last_shipped_ts(ts: str):
    os.makedirs(os.path.dirname(CURSOR_FILE), exist_ok=True)
    with open(CURSOR_FILE, "w") as f:
        f.write(ts)


async def ship_costs():
    pool = await get_shared_pool()
    last_ts = await get_last_shipped_ts()

    # Fetch new cost rows since last shipment
    query = """
        SELECT id, role, provider, model, tokens_in, tokens_out, cost_usd,
               latency_ms, created_at, correlation_id, company_id, agent_id
        FROM api_cost_log
        WHERE cost_usd > 0
    """
    params = []
    if last_ts:
        query += " AND created_at > $1"
        params.append(last_ts)
    query += " ORDER BY created_at ASC LIMIT 200"

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *params)

    if not rows:
        return 0

    shipped = 0
    latest_ts = last_ts

    async with aiohttp.ClientSession() as session:
        for r in rows:
            payload = {
                "provider": r["provider"] or "openrouter",
                "model": r["model"] or "unknown",
                "tokensIn": int(r["tokens_in"] or 0),
                "tokensOut": int(r["tokens_out"] or 0),
                "costUsd": float(r["cost_usd"]),
                "latencyMs": int(r["latency_ms"] or 0),
                "operation": r["role"] or "llm_call",
                "correlationId": r["correlation_id"] or "",
                "companyId": r["company_id"] or "jarvais",
                "agentId": r["agent_id"] or "hermes",
                "timestamp": r["created_at"].isoformat(),
            }
            try:
                async with session.post(
                    f"{PAPERCLIP_URL}/cost-events",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status in (200, 201):
                        shipped += 1
                    else:
                        logger.warning("cost-event POST returned %d", resp.status)
            except Exception as exc:
                logger.warning("cost-event POST failed: %s", exc)
                return shipped  # stop on first failure, retry next poll

            latest_ts = r["created_at"].isoformat()

    if shipped:
        await save_last_shipped_ts(latest_ts)
        logger.info("Shipped %d cost events to Paperclip", shipped)

    return shipped


async def run_forever():
    load_env()
    logger.info("Cost shipper v2 starting (poll=%ds, target=%s)", POLL_INTERVAL_S, PAPERCLIP_URL)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    while not stop.is_set():
        try:
            await ship_costs()
        except Exception as exc:
            logger.exception("Ship cycle failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
        except asyncio.TimeoutError:
            pass

    logger.info("Cost shipper v2 stopped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_forever())
