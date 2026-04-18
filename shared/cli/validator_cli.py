"""
shared.cli.validator_cli — operator CLI for the Rule-1 continuous auditor.

Rule 1: backtest ≡ live (99.9%). Every live fill must have a paired shadow
backtest row. This CLI exposes the four operator verbs Dean uses day-to-day:

    status   — is the validator daemon healthy?
    pair     — force-pair a single live trade with its shadow (debugging)
    check    — run a Rule-1 drift report over the last N hours
    windows  — show the validator's current sliding-window config

Phase 13 (this file): `status` and `windows` work. `pair` and `check` are
stubs that land with Phase 21 (Rule-1 continuous auditor).
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

VALIDATOR_UNIT = "tickles-validator.service"
DEFAULT_WINDOWS = {
    "short_hours": 24,
    "medium_days": 7,
    "long_days": 30,
    "drift_threshold_pct": 0.1,
}


def cmd_status(_args: argparse.Namespace) -> int:
    """Report the validator daemon health plus the drift-threshold config."""
    if not is_on_vps():
        emit({
            "ok": False,
            "environment": "local",
            "message": "validator_cli.status queries systemd on the VPS.",
        })
        return EXIT_FAIL

    unit = systemctl_status(VALIDATOR_UNIT)
    active = unit["active"] == "active"
    emit({
        "ok": active,
        "phase": 13,
        "unit": unit,
        "note": (
            "tickles-validator.service will be registered by Phase 21. Until "
            "then `active` will report 'inactive' or 'not-found' — that's "
            "expected."
        ),
    })
    return EXIT_OK if active else EXIT_FAIL


def cmd_windows(_args: argparse.Namespace) -> int:
    """Static dump of the planned Rule-1 window config."""
    emit({
        "ok": True,
        "windows": DEFAULT_WINDOWS,
        "note": "Dean edits these in /etc/tickles/validator.env once Phase 21 ships.",
    })
    return EXIT_OK


def _build_pair(p: argparse.ArgumentParser) -> None:
    p.add_argument("--trade-id", required=True, help="live trade id (positions.id)")
    p.add_argument("--force", action="store_true", help="overwrite existing pairing")


def cmd_pair(args: argparse.Namespace) -> int:
    return not_yet_implemented(
        21,
        f"validator pair trade_id={args.trade_id} force={args.force}",
    )


def _build_check(p: argparse.ArgumentParser) -> None:
    p.add_argument("--hours", type=int, default=24, help="lookback window in hours")
    p.add_argument("--company", default=None, help="limit to one company id")
    p.add_argument("--json", action="store_true", help="emit json (default) — reserved")


def cmd_check(args: argparse.Namespace) -> int:
    return not_yet_implemented(
        21,
        f"validator check hours={args.hours} company={args.company}",
    )


def _build() -> argparse.ArgumentParser:
    return build_parser(
        prog="validator_cli",
        description="Operator CLI for the Rule-1 continuous auditor.",
        subcommands=[
            Subcommand("status", "is the validator daemon alive?", cmd_status),
            Subcommand("windows", "show the Rule-1 drift window config", cmd_windows),
            Subcommand(
                "pair", "force-pair a single live trade with its shadow",
                cmd_pair, build=_build_pair, lands_in_phase=21,
            ),
            Subcommand(
                "check", "run a Rule-1 drift report over the last N hours",
                cmd_check, build=_build_check, lands_in_phase=21,
            ),
        ],
    )


def main() -> int:
    return run(_build())


if __name__ == "__main__":
    raise SystemExit(main())
