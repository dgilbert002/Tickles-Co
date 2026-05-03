"""
shared.cli.services_cli — Phase 22 operator CLI for long-running services.

Subcommands (all stdout is single-line JSON):

* ``list [--kind KIND]`` — show every registered service
  (name, kind, module, systemd unit, whether it's enabled on the
  VPS today).
* ``describe <name>`` — full descriptor for one service.
* ``status [--name NAME]`` — systemd status for one or all units.
  Runs ``systemctl show`` under the hood; returns ``unknown``
  fields off-box.
* ``heartbeats [--service NAME] [--limit N]`` — tail heartbeat
  events from the Rule-1 Auditor SQLite store (Phase 21). Lets
  operators confirm a collector is actually alive even if
  ``systemctl is-active`` hasn't polled yet.
* ``run-once --name NAME`` — run one tick of the named service
  using an in-process :class:`ServiceDaemon`. Only works for
  services that register a factory (Phase 22 ships with the
  framework; concrete factories land as each collector is
  migrated). Currently useful as a dry-run scaffold.
* ``systemd-units`` — list the tickles-*.service unit files
  that are installed on the current box and whether they are
  active.

Phase 22 is strictly additive: this CLI does not start, stop,
enable, or disable anything. Write operations land in a later
phase once the registry reaches full coverage.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import os
from typing import Any, Dict, List

from shared.cli._common import (
    EXIT_FAIL,
    EXIT_OK,
    Subcommand,
    build_parser,
    emit,
    is_on_vps,
    run,
    systemctl_status,
)
from shared.services import SERVICE_REGISTRY


def cmd_list(args: argparse.Namespace) -> int:
    svcs = (
        SERVICE_REGISTRY.by_kind(args.kind)
        if getattr(args, "kind", None)
        else SERVICE_REGISTRY.list_services()
    )
    emit(
        {
            "ok": True,
            "count": len(svcs),
            "services": [s.to_dict() for s in svcs],
        }
    )
    return EXIT_OK


def cmd_describe(args: argparse.Namespace) -> int:
    try:
        desc = SERVICE_REGISTRY.get(args.name)
    except KeyError:
        emit({"ok": False, "error": f"unknown service: {args.name}"})
        return EXIT_FAIL
    emit({"ok": True, "service": desc.to_dict()})
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    if args.name:
        try:
            desc = SERVICE_REGISTRY.get(args.name)
        except KeyError:
            emit({"ok": False, "error": f"unknown service: {args.name}"})
            return EXIT_FAIL
        unit = desc.systemd_unit or f"tickles-{desc.name}.service"
        state = systemctl_status(unit)
        emit({"ok": True, "on_vps": is_on_vps(), "service": desc.to_dict(), "systemd": state})
        return EXIT_OK

    rows: List[Dict[str, Any]] = []
    for desc in SERVICE_REGISTRY.list_services():
        unit = desc.systemd_unit or f"tickles-{desc.name}.service"
        rows.append({"service": desc.name, "unit": unit, "systemd": systemctl_status(unit)})
    emit({"ok": True, "on_vps": is_on_vps(), "count": len(rows), "statuses": rows})
    return EXIT_OK


def cmd_heartbeats(args: argparse.Namespace) -> int:
    try:
        from shared.auditor import AuditStore
        from shared.auditor.schema import AuditEventType
    except Exception as exc:  # noqa: BLE001
        emit({"ok": False, "error": f"auditor not available: {exc}"})
        return EXIT_FAIL

    with AuditStore() as store:
        records = store.list_recent(
            limit=int(args.limit),
            event_type=AuditEventType.HEARTBEAT,
        )
    if getattr(args, "service", None):
        subject = f"service:{args.service}"
        records = [r for r in records if r.subject == subject]

    emit(
        {
            "ok": True,
            "count": len(records),
            "events": [
                {
                    "id": r.id,
                    "ts_unix": r.ts_unix,
                    "severity": r.severity.value,
                    "subject": r.subject,
                    "passed": r.passed,
                    "details": r.details,
                }
                for r in records
            ],
        }
    )
    return EXIT_OK


def cmd_run_once(args: argparse.Namespace) -> int:
    try:
        desc = SERVICE_REGISTRY.get(args.name)
    except KeyError:
        emit({"ok": False, "error": f"unknown service: {args.name}"})
        return EXIT_FAIL
    if desc.factory is None:
        emit(
            {
                "ok": False,
                "error": (
                    f"service {args.name} has no in-process factory yet. "
                    "Register a factory in shared.services.registry to enable "
                    "run-once."
                ),
            }
        )
        return EXIT_FAIL
    obj = desc.factory()
    run_once = getattr(obj, "run_once", None)
    if run_once is None:
        emit({"ok": False, "error": "factory returned object without run_once()"})
        return EXIT_FAIL
    summary = asyncio.run(run_once())
    emit({"ok": True, "service": desc.name, "summary": summary})
    return EXIT_OK


def cmd_systemd_units(args: argparse.Namespace) -> int:
    root = "/etc/systemd/system"
    if not os.path.isdir(root):
        emit({"ok": False, "error": "not on VPS (no /etc/systemd/system)"})
        return EXIT_FAIL
    units: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(root, "tickles-*.service"))):
        unit = os.path.basename(path)
        units.append({"unit": unit, "state": systemctl_status(unit)})
    emit({"ok": True, "count": len(units), "units": units})
    return EXIT_OK


# ---------------------------------------------------------------------------
# Arg builders
# ---------------------------------------------------------------------------


def _build_list(p: argparse.ArgumentParser) -> None:
    p.add_argument("--kind", default=None, help="collector|gateway|worker|auditor|catalog|api")


def _build_describe(p: argparse.ArgumentParser) -> None:
    p.add_argument("name")


def _build_status(p: argparse.ArgumentParser) -> None:
    p.add_argument("--name", default=None, help="single service name; omit for all")


def _build_heartbeats(p: argparse.ArgumentParser) -> None:
    p.add_argument("--service", default=None, help="filter by service name")
    p.add_argument("--limit", type=int, default=20)


def _build_run_once(p: argparse.ArgumentParser) -> None:
    p.add_argument("--name", required=True)


def _build_systemd_units(p: argparse.ArgumentParser) -> None:
    del p


def main() -> int:
    subs = [
        Subcommand("list", "List registered services.", cmd_list, build=_build_list),
        Subcommand("describe", "Describe one registered service.",
                   cmd_describe, build=_build_describe),
        Subcommand("status", "Systemd status for one or all services.",
                   cmd_status, build=_build_status),
        Subcommand("heartbeats", "Tail service heartbeats from the auditor.",
                   cmd_heartbeats, build=_build_heartbeats),
        Subcommand("run-once", "Run one tick of a service (requires factory).",
                   cmd_run_once, build=_build_run_once),
        Subcommand("systemd-units", "List tickles-*.service unit files on box.",
                   cmd_systemd_units, build=_build_systemd_units),
    ]
    parser = build_parser(
        "services_cli",
        "Operator CLI for the Tickles long-running services.",
        subs,
    )
    return run(parser)


if __name__ == "__main__":
    raise SystemExit(main())
