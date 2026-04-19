"""
shared.cli.gateway_cli — operator CLI for the Market Data Gateway.

Phase 17 (this file): real subscribe/unsubscribe/list/peek/stats commands
that talk to Redis.  The daemon (`tickles-md-gateway.service`) reconciles
the desired-state hash, so the CLI's job is to mutate that hash and read
back stats — no direct ccxt.pro work happens here.

Status commands continue to query systemd for the trio of long-running
gateway-adjacent units plus the new ``tickles-md-gateway.service``.

Invocation:
    python -m shared.cli.gateway_cli status
    python -m shared.cli.gateway_cli services
    python -m shared.cli.gateway_cli subscribe --venue binance --symbol BTC/USDT --channels tick,trade,l1
    python -m shared.cli.gateway_cli unsubscribe --venue binance --symbol BTC/USDT --channels tick
    python -m shared.cli.gateway_cli list
    python -m shared.cli.gateway_cli stats
    python -m shared.cli.gateway_cli peek --venue binance --symbol BTC/USDT --channel trade --count 5
    python -m shared.cli.gateway_cli replay --venue binance --symbol BTC/USDT --from 2026-04-18T00:00:00Z --to 2026-04-18T01:00:00Z
"""
from __future__ import annotations

import argparse
import asyncio
import os
from typing import List

from shared.gateway.schema import TickChannel

from ._common import (
    EXIT_FAIL,
    EXIT_OK,
    Subcommand,
    build_parser,
    emit,
    is_on_vps,
    not_yet_implemented,
    run,
    systemctl_status,
)

GATEWAY_UNITS = [
    "tickles-md-gateway.service",
    "tickles-candle-daemon.service",
    "tickles-catalog.service",
    "tickles-bt-workers.service",
]


DEFAULT_REDIS_URL = os.environ.get("TICKLES_REDIS_URL", "redis://127.0.0.1:6379/0")


def _add_redis_url(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--redis-url",
        default=DEFAULT_REDIS_URL,
        help=f"redis URL (default: {DEFAULT_REDIS_URL})",
    )


def _parse_channels(raw: str) -> List[TickChannel]:
    out: List[TickChannel] = []
    seen: set[str] = set()
    for token in raw.split(","):
        token = token.strip().lower()
        if not token or token in seen:
            continue
        seen.add(token)
        try:
            out.append(TickChannel(token))
        except ValueError as exc:
            valid = ", ".join(c.value for c in TickChannel)
            raise ValueError(f"unknown channel '{token}'; valid: {valid}") from exc
    if not out:
        raise ValueError("at least one channel is required")
    return out


def cmd_status(_args: argparse.Namespace) -> int:
    if not is_on_vps():
        emit({
            "ok": False,
            "environment": "local",
            "message": "`status` queries systemd on the VPS. Run inside /opt/tickles.",
        })
        return EXIT_FAIL
    units = [systemctl_status(u) for u in GATEWAY_UNITS]
    all_active = all(u["active"] == "active" for u in units)
    emit({
        "ok": all_active,
        "environment": "vps",
        "units": units,
    })
    return EXIT_OK if all_active else EXIT_FAIL


def cmd_services(_args: argparse.Namespace) -> int:
    emit({
        "ok": True,
        "services": GATEWAY_UNITS,
        "note": "tickles-md-gateway.service was added in Phase 17.",
    })
    return EXIT_OK


def _build_subscribe(p: argparse.ArgumentParser) -> None:
    p.add_argument("--venue", required=True, help="exchange id (binance, bybit, ...)")
    p.add_argument("--symbol", required=True, help='market symbol, e.g. "BTC/USDT"')
    p.add_argument(
        "--channels",
        default="tick",
        help="comma-separated subset of: tick,trade,l1,mark,funding",
    )
    p.add_argument("--requested-by", default="cli", help="attribution string")
    _add_redis_url(p)


def cmd_subscribe(args: argparse.Namespace) -> int:
    try:
        channels = _parse_channels(args.channels)
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)})
        return EXIT_FAIL

    from shared.gateway.redis_bus import RedisBus
    from shared.gateway.schema import SubscriptionKey

    async def _run() -> int:
        bus = RedisBus(args.redis_url)
        await bus.connect()
        try:
            results = []
            for ch in channels:
                key = SubscriptionKey(
                    exchange=args.venue.lower(),
                    symbol=args.symbol,
                    channel=ch,
                )
                added = await bus.add_desired_sub(key, args.requested_by)
                results.append({
                    "exchange": key.exchange,
                    "symbol": key.symbol,
                    "channel": key.channel.value,
                    "redis_channel": key.redis_channel,
                    "added": added,
                })
            emit({"ok": True, "subscribed": results, "note": "daemon reconciles within poll_interval"})
            return EXIT_OK
        finally:
            await bus.close()

    return asyncio.run(_run())


def _build_unsubscribe(p: argparse.ArgumentParser) -> None:
    p.add_argument("--venue", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument(
        "--channels",
        default="tick",
        help="comma-separated subset of: tick,trade,l1,mark,funding",
    )
    _add_redis_url(p)


def cmd_unsubscribe(args: argparse.Namespace) -> int:
    try:
        channels = _parse_channels(args.channels)
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)})
        return EXIT_FAIL

    from shared.gateway.redis_bus import RedisBus
    from shared.gateway.schema import SubscriptionKey

    async def _run() -> int:
        bus = RedisBus(args.redis_url)
        await bus.connect()
        try:
            results = []
            for ch in channels:
                key = SubscriptionKey(
                    exchange=args.venue.lower(),
                    symbol=args.symbol,
                    channel=ch,
                )
                removed = await bus.remove_desired_sub(key)
                results.append({
                    "exchange": key.exchange,
                    "symbol": key.symbol,
                    "channel": key.channel.value,
                    "removed": removed,
                })
            emit({"ok": True, "unsubscribed": results})
            return EXIT_OK
        finally:
            await bus.close()

    return asyncio.run(_run())


def _build_list(p: argparse.ArgumentParser) -> None:
    _add_redis_url(p)


def cmd_list(args: argparse.Namespace) -> int:
    from shared.gateway.redis_bus import RedisBus

    async def _run() -> int:
        bus = RedisBus(args.redis_url)
        await bus.connect()
        try:
            pairs = await bus.list_desired_subs()
            out = [
                {
                    "exchange": key.exchange,
                    "symbol": key.symbol,
                    "channel": key.channel.value,
                    "redis_channel": key.redis_channel,
                    "requested_by": meta.get("requested_by", "?"),
                    "since": meta.get("since"),
                }
                for key, meta in pairs
            ]
            emit({"ok": True, "count": len(out), "subscriptions": out})
            return EXIT_OK
        finally:
            await bus.close()

    return asyncio.run(_run())


def _build_stats(p: argparse.ArgumentParser) -> None:
    _add_redis_url(p)


def cmd_stats(args: argparse.Namespace) -> int:
    from shared.gateway.redis_bus import RedisBus

    async def _run() -> int:
        bus = RedisBus(args.redis_url)
        await bus.connect()
        try:
            stats = await bus.read_stats()
            lag = await bus.list_lag()
            emit({
                "ok": stats is not None,
                "stats": stats,
                "lag_keys": [{"key": k, "epoch_ms": v} for k, v in lag],
                "note": "stats null means tickles-md-gateway is not running yet",
            })
            return EXIT_OK if stats is not None else EXIT_FAIL
        finally:
            await bus.close()

    return asyncio.run(_run())


def _build_peek(p: argparse.ArgumentParser) -> None:
    p.add_argument("--venue", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument(
        "--channel",
        default="tick",
        choices=["tick", "trade", "l1", "mark", "funding"],
    )
    p.add_argument("--count", type=int, default=5, help="how many messages to print")
    p.add_argument("--timeout", type=float, default=10.0, help="seconds to wait")
    _add_redis_url(p)


def cmd_peek(args: argparse.Namespace) -> int:
    from shared.gateway.redis_bus import RedisBus
    from shared.gateway.schema import TickChannel, channel_for

    pattern = channel_for(args.venue, args.symbol, TickChannel(args.channel))

    async def _run() -> int:
        bus = RedisBus(args.redis_url)
        await bus.connect()
        seen: list[dict] = []
        try:
            async def collect() -> None:
                async for ch, payload in bus.listen_pattern(pattern):
                    seen.append({"channel": ch, "payload": payload})
                    if len(seen) >= args.count:
                        return

            try:
                await asyncio.wait_for(collect(), timeout=args.timeout)
            except asyncio.TimeoutError:
                pass

            emit({
                "ok": len(seen) > 0,
                "pattern": pattern,
                "received": len(seen),
                "messages": seen,
            })
            return EXIT_OK if seen else EXIT_FAIL
        finally:
            await bus.close()

    return asyncio.run(_run())


def _build_replay(p: argparse.ArgumentParser) -> None:
    p.add_argument("--venue", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--from", dest="from_ts", required=True, help="ISO 8601 start")
    p.add_argument("--to", dest="to_ts", required=True, help="ISO 8601 end")


def cmd_replay(args: argparse.Namespace) -> int:
    return not_yet_implemented(
        18,
        f"gateway replay venue={args.venue} symbol={args.symbol} "
        f"from={args.from_ts} to={args.to_ts}",
    )


def _build() -> argparse.ArgumentParser:
    return build_parser(
        prog="gateway_cli",
        description="Operator CLI for the Market Data Gateway.",
        subcommands=[
            Subcommand("status", "aggregate systemd status for gateway daemons", cmd_status),
            Subcommand("services", "list systemd units this CLI manages", cmd_services),
            Subcommand(
                "subscribe", "register desired ws subscriptions in Redis",
                cmd_subscribe, build=_build_subscribe,
            ),
            Subcommand(
                "unsubscribe", "drop desired ws subscriptions from Redis",
                cmd_unsubscribe, build=_build_unsubscribe,
            ),
            Subcommand(
                "list", "list desired subscriptions in Redis",
                cmd_list, build=_build_list,
            ),
            Subcommand(
                "stats", "read GatewayStats published by the daemon",
                cmd_stats, build=_build_stats,
            ),
            Subcommand(
                "peek", "tail messages from a redis md.* channel",
                cmd_peek, build=_build_peek,
            ),
            Subcommand(
                "replay", "replay historical candles into Redis (lands Phase 18)",
                cmd_replay, build=_build_replay, lands_in_phase=18,
            ),
        ],
    )


def main() -> int:
    return run(_build())


if __name__ == "__main__":
    raise SystemExit(main())
