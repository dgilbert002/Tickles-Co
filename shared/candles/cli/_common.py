"""
shared.cli._common — shared helpers for the operator CLIs.

Kept tiny deliberately: we want every CLI to be runnable without dragging
in service-layer imports when the backend is off. Heavy imports are
deferred inside subcommand handlers.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NOT_YET = 2


def setup_logging(verbose: bool) -> logging.Logger:
    """Uniform log format across every CLI so Dean can grep one pattern."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return logging.getLogger("tickles.cli")


def emit(payload: Dict) -> None:
    """Single-line JSON to stdout so the output is pipe-friendly."""
    sys.stdout.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    sys.stdout.flush()


def systemctl_status(unit: str) -> Dict[str, str]:
    """Return {active, sub, enabled} for a systemd unit. Works on VPS only."""
    def _one(prop: str) -> str:
        try:
            out = subprocess.check_output(
                ["systemctl", "show", "-p", prop, "--value", unit],
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return out.decode().strip()
        except Exception:
            return "unknown"

    return {
        "unit": unit,
        "active": _one("ActiveState"),
        "sub": _one("SubState"),
        "enabled": _one("UnitFileState"),
    }


@dataclass
class Subcommand:
    """One subcommand on a CLI. `handler` returns an exit code."""
    name: str
    help: str
    handler: Callable[[argparse.Namespace], int]
    build: Optional[Callable[[argparse.ArgumentParser], None]] = None
    lands_in_phase: Optional[int] = None  # when set, handler is a stub


def not_yet_implemented(lands_in_phase: int, what: str) -> int:
    emit({
        "ok": False,
        "status": "not_yet_implemented",
        "lands_in_phase": lands_in_phase,
        "what": what,
        "message": (
            f"This subcommand is scaffolded in Phase 13; the real body lands "
            f"in Phase {lands_in_phase}. Running the stub is a no-op."
        ),
    })
    return EXIT_NOT_YET


def build_parser(prog: str, description: str, subcommands: List[Subcommand]) -> argparse.ArgumentParser:
    """Common parser builder — gives every CLI --verbose and the same help look."""
    p = argparse.ArgumentParser(prog=prog, description=description)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    sub.required = True
    for sc in subcommands:
        child = sub.add_parser(sc.name, help=sc.help, description=sc.help)
        if sc.build is not None:
            sc.build(child)
        child.set_defaults(_handler=sc.handler, _lands_in=sc.lands_in_phase)
    return p


def run(parser: argparse.ArgumentParser) -> int:
    args = parser.parse_args()
    setup_logging(getattr(args, "verbose", False))
    handler = getattr(args, "_handler", None)
    if handler is None:
        parser.print_help()
        return EXIT_FAIL
    try:
        return int(handler(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logging.getLogger("tickles.cli").exception("handler failed: %s", exc)
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL


def is_on_vps() -> bool:
    """Cheap runtime detector so status commands are truthful off-box."""
    return os.path.isdir("/opt/tickles") and os.path.isdir("/etc/systemd/system")
