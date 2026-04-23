"""
Module: shared.tests.test_gateway_capital
Purpose: End-to-end test for Capital.com integration into the Market Data Gateway.
Location: /opt/tickles/shared/tests/test_gateway_capital.py
"""

import asyncio
import logging
import os
from shared.gateway.gateway import Gateway
from shared.gateway.redis_bus import RedisBus
from shared.gateway.schema import SubscriptionRequest, TickChannel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_capital_gateway_streaming():
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    bus = RedisBus(redis_url)
    gateway = Gateway(bus)

    logger.info("Starting Gateway...")
    await gateway.start()

    try:
        # 1. Subscribe to a Capital.com epic
        symbol = "BTCUSD"
        logger.info(f"Subscribing to {symbol} candles on Capital.com...")
        req = SubscriptionRequest(
            exchange="capital",
            symbol=symbol,
            channels=[TickChannel.CANDLE],
            requested_by="test_script"
        )
        leases = await gateway.subscribe(req)
        logger.info(f"Subscription active: {leases}")

        # 2. Listen to Redis for the published data
        logger.info(f"Waiting for data on Redis channel: {leases[0].key.redis_channel}...")
        
        # We'll use a manual pubsub listener to verify the fan-out
        pubsub = bus.client.pubsub()
        await pubsub.subscribe(leases[0].key.redis_channel)
        
        count = 0
        async for message in pubsub.listen():
            if message["type"] == "message":
                logger.info(f"RECEIVED DATA FROM REDIS: {message['data']}")
                count += 1
                if count >= 2: # Get 2 candles then stop
                    break
        
        await pubsub.unsubscribe(leases[0].key.redis_channel)

        # 3. Unsubscribe
        logger.info(f"Unsubscribing from {symbol}...")
        releases = await gateway.unsubscribe(req)
        logger.info(f"Unsubscription complete: {releases}")

    finally:
        await gateway.stop()
        logger.info("Gateway stopped.")

if __name__ == "__main__":
    asyncio.run(test_capital_gateway_streaming())
