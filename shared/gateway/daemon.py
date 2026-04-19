"""
shared.gateway.daemon — long-running entrypoint for tickles-md-gateway.

The 21-year-old version
-----------------------
This is the script systemd runs forever.

  Loop:
    1. Make sure the Gateway is started.
    2. Read desired subscriptions from Redis (md:subscriptions).
    3. Reconcile: subscribe what is missing, unsubscribe what was
       deleted via the CLI.
    4. Publish stats to md:gateway:stats every few seconds (the
       Gateway's own background task already does this).
    5. Sleep ``poll_interval`` seconds and repeat.

We use a *desired-state* model rather than RPC.  The CLI just writes the
hash; the daemon reconciles.  This keeps the daemon idempotent and
lets the operator inspect intent (``HGETALL md:subscriptions``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from typing import Optional, Set

from shared.gateway.gateway import Gateway
from shared.gateway.redis_bus import RedisBus
from shared.gateway.schema import SubscriptionKey, SubscriptionRequest

logger = logging.getLogger("tickles.md_gateway")

DEFAULT_REDIS_URL = os.environ.get("TICKLES_REDIS_URL", "redis://127.0.0.1:6379/0")
DEFAULT_POLL_INTERVAL = float(os.environ.get("TICKLES_MD_GATEWAY_POLL_INTERVAL", "5"))


async def reconcile_once(gateway: Gateway, bus: RedisBus, current: Set[SubscriptionKey]) -> Set[SubscriptionKey]:
    desired_pairs = await bus.list_desired_subs()
    desired: Set[SubscriptionKey] = {key for key, _ in desired_pairs}
    requesters: dict[SubscriptionKey, str] = {
        key: str(meta.get("requested_by", "daemon")) for key, meta in desired_pairs
    }

    to_add = desired - current
    to_remove = current - desired

    for key in to_add:
        try:
            await gateway.subscribe(
                SubscriptionRequest(
                    exchange=key.exchange,
                    symbol=key.symbol,
                    channels=[key.channel],
                    requested_by=requesters.get(key, "daemon"),
                )
            )
            logger.info("subscribed %s", key.redis_channel)
        except Exception:  # noqa: BLE001
            logger.exception("failed to subscribe %s", key.redis_channel)

    for key in to_remove:
        try:
            await gateway.unsubscribe(
                SubscriptionRequest(
                    exchange=key.exchange,
                    symbol=key.symbol,
                    channels=[key.channel],
                    requested_by="daemon",
                )
            )
            logger.info("unsubscribed %s", key.redis_channel)
        except Exception:  # noqa: BLE001
            logger.exception("failed to unsubscribe %s", key.redis_channel)

    return desired


async def run(redis_url: str, poll_interval: float) -> None:
    bus = RedisBus(redis_url)
    gateway = Gateway(bus)
    await gateway.start()

    stop_event = asyncio.Event()

    def _shutdown() -> None:
        logger.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    current: Set[SubscriptionKey] = set()
    try:
        while not stop_event.is_set():
            try:
                current = await reconcile_once(gateway, bus, current)
            except Exception:  # noqa: BLE001
                logger.exception("reconcile_once failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await gateway.stop()


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tickles-md-gateway")
    p.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    p.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run(args.redis_url, args.poll_interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
