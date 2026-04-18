"""
shared.cli.gateway_cli — operator CLI for the Market Data Gateway.

Phase 13 (this file): status subcommands work against the already-running
tickles-candle-daemon and tickles-catalog services. Real WS fan-out actions
(`subscribe`, `unsubscribe`, `replay`) are stubs that report the phase where
they land.

Invocation:
    python -m shared.cli.gateway_cli status
    python -m shared.cli.gateway_cli services
    python -m shared.cli.gateway_cli subscribe --venue binance --symbol BTC/USDT
"""
from __future__ import annotations

import argparse

from ._common import (
    EXIT_OK,
    EXIT_FAIL,
    Subcommand,
    build_parser,
    emit,
    is_on_vps,
    not_yet_implemented,
    run,
    systemctl_status,
)

GATEWAY_UNITS = [
    "tickles-candle-daemon.service",
    "tickles-catalog.service",
    "tickles-bt-workers.service",
]


def cmd_status(_args: argparse.Namespace) -> int:
    """Aggregate health of every gateway-adjacent daemon."""
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
    """Static list of the services this CLI manages (no runtime call)."""
    emit({
        "ok": True,
        "services": GATEWAY_UNITS,
        "note": (
            "Phase 17 will add the CCXT-Pro WebSocket gateway and register "
            "tickles-market-gateway.service in this list."
        ),
    })
    return EXIT_OK


def _build_subscribe(p: argparse.ArgumentParser) -> None:
    p.add_argument("--venue", required=True, help="exchange id (binance, bybit, capital, ...)")
    p.add_argument("--symbol", required=True, help='market symbol, e.g. "BTC/USDT"')
    p.add_argument("--feeds", default="ticker,ohlcv", help="comma-separated feed types")


def cmd_subscribe(args: argparse.Namespace) -> int:
    """Request the WS gateway to subscribe to a market (stub until Phase 17)."""
    return not_yet_implemented(
        17,
        f"gateway subscribe venue={args.venue} symbol={args.symbol} feeds={args.feeds}",
    )


def cmd_unsubscribe(args: argparse.Namespace) -> int:
    return not_yet_implemented(
        17,
        f"gateway unsubscribe venue={args.venue} symbol={args.symbol}",
    )


def _build_unsubscribe(p: argparse.ArgumentParser) -> None:
    p.add_argument("--venue", required=True)
    p.add_argument("--symbol", required=True)


def _build_replay(p: argparse.ArgumentParser) -> None:
    p.add_argument("--venue", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--from", dest="from_ts", required=True, help="ISO 8601 start")
    p.add_argument("--to", dest="to_ts", required=True, help="ISO 8601 end")


def cmd_replay(args: argparse.Namespace) -> int:
    """Fan out historical candles from Postgres to Redis (lands with Phase 17)."""
    return not_yet_implemented(
        17,
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
                "subscribe", "subscribe the WS gateway to a venue/symbol",
                cmd_subscribe, build=_build_subscribe, lands_in_phase=17,
            ),
            Subcommand(
                "unsubscribe", "unsubscribe the WS gateway from a venue/symbol",
                cmd_unsubscribe, build=_build_unsubscribe, lands_in_phase=17,
            ),
            Subcommand(
                "replay", "replay historical candles into Redis",
                cmd_replay, build=_build_replay, lands_in_phase=17,
            ),
        ],
    )


def main() -> int:
    return run(_build())


if __name__ == "__main__":
    raise SystemExit(main())
