"""
shared.cli.forward_test_cli — operator CLI for continuous forward testing.

A forward test is the live-parallel counterpart of a backtest: once a
strategy is promoted beyond Phase-1 sanity, the same engine that ran the
backtest keeps running on every new candle and records "what would have
happened" alongside the real live fills. This is how Rule 1 (backtest ≡
live) is measured in practice.

Phase 13 (this file): status and results subcommands work against the
existing backtest tables. start/stop are stubs — the continuous forward-
test engine lands with Phase 22 (Collectors-as-Services + lifecycle) and
is tied to the backtest worker pool (Phase 19).

Invocations:
    python -m shared.cli.forward_test_cli status
    python -m shared.cli.forward_test_cli start --strategy-id 42 --symbol BTC/USDT
    python -m shared.cli.forward_test_cli results --strategy-id 42 --hours 24
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

FORWARD_TEST_UNITS = [
    "tickles-bt-workers.service",       # today: shared pool, will get a forward-test queue in Phase 22
    "tickles-forward-test.service",     # pending_cutover: planned by Phase 22
]


def cmd_status(_args: argparse.Namespace) -> int:
    """Report which supporting daemons are up and whether forward tests can run."""
    if not is_on_vps():
        emit({
            "ok": False,
            "environment": "local",
            "message": "forward_test_cli.status queries systemd on the VPS.",
        })
        return EXIT_FAIL

    units = [systemctl_status(u) for u in FORWARD_TEST_UNITS]
    # The first unit (bt-workers) must be active. The second (forward-test)
    # is allowed to be missing until Phase 22 ships.
    bt_ok = units[0]["active"] == "active"
    emit({
        "ok": bt_ok,
        "phase": 13,
        "units": units,
        "note": (
            "tickles-forward-test.service will be registered by Phase 22. "
            "Until then only the shared bt-workers pool is required."
        ),
    })
    return EXIT_OK if bt_ok else EXIT_FAIL


def _build_start(p: argparse.ArgumentParser) -> None:
    p.add_argument("--strategy-id", type=int, required=True)
    p.add_argument("--symbol", required=True, help='e.g. "BTC/USDT"')
    p.add_argument("--venue", default="binance")
    p.add_argument("--tf", default="1h", help="timeframe: 1m, 5m, 15m, 1h, 4h, 1d")


def cmd_start(args: argparse.Namespace) -> int:
    return not_yet_implemented(
        22,
        f"forward_test start strategy_id={args.strategy_id} "
        f"symbol={args.symbol} venue={args.venue} tf={args.tf}",
    )


def _build_stop(p: argparse.ArgumentParser) -> None:
    p.add_argument("--strategy-id", type=int, required=True)
    p.add_argument("--symbol", required=True)


def cmd_stop(args: argparse.Namespace) -> int:
    return not_yet_implemented(
        22,
        f"forward_test stop strategy_id={args.strategy_id} symbol={args.symbol}",
    )


def _build_results(p: argparse.ArgumentParser) -> None:
    p.add_argument("--strategy-id", type=int, required=True)
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--symbol", default=None)


def cmd_results(args: argparse.Namespace) -> int:
    return not_yet_implemented(
        22,
        f"forward_test results strategy_id={args.strategy_id} "
        f"hours={args.hours} symbol={args.symbol}",
    )


def _build() -> argparse.ArgumentParser:
    return build_parser(
        prog="forward_test_cli",
        description="Operator CLI for the continuous forward-test engine.",
        subcommands=[
            Subcommand("status", "forward-test daemon health", cmd_status),
            Subcommand(
                "start", "start a continuous forward test on a strategy+symbol",
                cmd_start, build=_build_start, lands_in_phase=22,
            ),
            Subcommand(
                "stop", "stop a running forward test",
                cmd_stop, build=_build_stop, lands_in_phase=22,
            ),
            Subcommand(
                "results", "summarise the last N hours of a forward test",
                cmd_results, build=_build_results, lands_in_phase=22,
            ),
        ],
    )


def main() -> int:
    return run(_build())


if __name__ == "__main__":
    raise SystemExit(main())
