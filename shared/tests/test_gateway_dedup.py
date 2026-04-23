"""
Module: shared.tests.test_gateway_dedup
Purpose: Verify deduplication and reference counting in the Market Data Gateway.
Location: /opt/tickles/shared/tests/test_gateway_dedup.py
"""

import asyncio
import logging
import os
from shared.gateway.gateway import Gateway
from shared.gateway.redis_bus import RedisBus
from shared.gateway.schema import SubscriptionRequest, TickChannel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_gateway_deduplication():
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    bus = RedisBus(redis_url)
    gateway = Gateway(bus)

    logger.info("Starting Gateway...")
    await gateway.start()

    try:
        symbol = "BTCUSD"
        exchange = "capital"
        channel = TickChannel.CANDLE

        # 1. Agent A subscribes
        logger.info("Agent A subscribing...")
        req_a = SubscriptionRequest(
            exchange=exchange,
            symbol=symbol,
            channels=[channel],
            requested_by="agent_a"
        )
        leases_a = await gateway.subscribe(req_a)
        logger.info(f"Agent A lease: {leases_a[0].activated=}, {leases_a[0].new_count=}")
        assert leases_a[0].activated is True
        assert leases_a[0].new_count == 1

        # 2. Agent B subscribes to the SAME thing
        logger.info("Agent B subscribing to same feed...")
        req_b = SubscriptionRequest(
            exchange=exchange,
            symbol=symbol,
            channels=[channel],
            requested_by="agent_b"
        )
        leases_b = await gateway.subscribe(req_b)
        logger.info(f"Agent B lease: {leases_b[0].activated=}, {leases_b[0].new_count=}")
        assert leases_b[0].activated is False
        assert leases_b[0].new_count == 2

        # 3. Agent A unsubscribes
        logger.info("Agent A unsubscribing...")
        rel_a = await gateway.unsubscribe(req_a)
        logger.info(f"Agent A release: {rel_a[0].deactivated=}, {rel_a[0].new_count=}")
        assert rel_a[0].deactivated is False
        assert rel_a[0].new_count == 1

        # 4. Agent B unsubscribes
        logger.info("Agent B unsubscribing...")
        rel_b = await gateway.unsubscribe(req_b)
        logger.info(f"Agent B release: {rel_b[0].deactivated=}, {rel_b[0].new_count=}")
        assert rel_b[0].deactivated is True
        assert rel_b[0].new_count == 0

        logger.info("Deduplication and reference counting verified successfully!")

    finally:
        await gateway.stop()
        logger.info("Gateway stopped.")

if __name__ == "__main__":
    asyncio.run(test_gateway_deduplication())
