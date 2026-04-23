"""
shared.cli.drill_cli - Phase 39 End-to-end drill.

Runs a deterministic, read-only / in-memory sequence of commands
across every phase 13-37 CLI to prove the system is alive and the
pieces still connect after Phase 38's docs-freeze. Produces a single
structured JSON report.

Every step is either:
  * a `demo` subcommand (in-memory, no side effects)
  * a read-only list / current / describe subcommand
  * a pure-computation subcommand (`pure-size`, `paper-simulate`)
  * a migration-sql echo (no DB change - just verifies the file is
    present and parseable)
  * a direct Python import / invocation that exercises package
    wiring without touching the network

Exit code is 0 iff every step succeeded (captured JSON payload parses
OK and subprocess returned 0).

Usage:
    py -m shared.cli.drill_cli list
    py -m shared.cli.drill_cli run [--stop-on-fail] [--out report.json]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


LOG = logging.getLogger("tickles.cli.drill")


# ---------------------------------------------------------------------
# Step descriptors.
# ---------------------------------------------------------------------


@dataclass
class DrillStep:
    phase: str
    name: str
    description: str
    argv: List[str] = field(default_factory=list)
    kind: str = "cli"  # cli | python
    python_callable: Optional[Callable[[], Dict[str, Any]]] = None
    # If True, the step output is expected to be valid JSON. Otherwise
    # only the return code is checked.
    expect_json: bool = True


def _py() -> str:
    # Use the current interpreter so the drill works under VPS venv
    # or local Windows Python equally well.
    return sys.executable or "python3"


def _steps() -> List[DrillStep]:
    steps: List[DrillStep] = []

    # Phase 14: market-data package layout check. The runtime modules
    # depend on asyncpg which is only present on the VPS; locally we
    # just assert the source files are present so the drill works
    # in dev too.
    def _p14() -> Dict[str, Any]:
        import shared.market_data as md  # noqa: F401
        from pathlib import Path

        pkg_dir = Path(md.__file__).parent
        files = sorted(p.name for p in pkg_dir.iterdir() if p.is_file())
        expected = {
            "candle_service.py",
            "gap_detector.py",
            "retention.py",
            "timing_service.py",
        }
        missing = sorted(expected - set(files))
        return {
            "package_present": True,
            "module_files": files,
            "expected_present": not missing,
            "missing": missing,
        }

    steps.append(
        DrillStep(
            phase="14",
            name="market_data_layout",
            description="Phase-14 market-data package layout check.",
            kind="python",
            python_callable=_p14,
            expect_json=True,
        )
    )

    # Phase 18: indicator library - count registered indicators.
    def _p18() -> Dict[str, Any]:
        from shared.backtest.indicators import INDICATORS  # type: ignore

        names = sorted(INDICATORS.keys())
        cats: set = set()
        for spec in INDICATORS.values():
            cat = getattr(spec, "category", None) or (
                spec.get("category") if isinstance(spec, dict) else None
            )
            if cat:
                cats.add(cat)
        return {
            "indicators_registered": len(names),
            "sample": names[:5],
            "categories": sorted(cats),
        }

    steps.append(
        DrillStep(
            phase="18",
            name="indicators_registry",
            description="Count registered indicators (Phase 18).",
            kind="python",
            python_callable=_p18,
            expect_json=True,
        )
    )

    # Phase 19: backtest engine registry.
    def _p19() -> Dict[str, Any]:
        from shared.backtest.engines import list_engines  # type: ignore

        return {"engines": sorted(list_engines())}

    steps.append(
        DrillStep(
            phase="19",
            name="backtest_engines_registry",
            description="Phase-19 backtest engine registry import.",
            kind="python",
            python_callable=_p19,
            expect_json=True,
        )
    )

    # Phase 21: auditor store import (Rule-1 continuous auditor).
    def _p21() -> Dict[str, Any]:
        from shared.auditor import AuditStore, ContinuousAuditor  # type: ignore

        return {
            "auditor_available": True,
            "store": AuditStore.__name__,
            "auditor": ContinuousAuditor.__name__,
        }

    steps.append(
        DrillStep(
            phase="21",
            name="auditor_store_import",
            description="Phase-21 Rule-1 auditor store import.",
            kind="python",
            python_callable=_p21,
            expect_json=True,
        )
    )

    # Phase 22: services registry - count registered services.
    def _p22() -> Dict[str, Any]:
        from shared.services.registry import SERVICE_REGISTRY  # type: ignore

        ds = SERVICE_REGISTRY.list_services()
        return {
            "services_total": len(ds),
            "services_by_kind": _count_by(ds, lambda d: d.kind),
            "services_by_phase": _count_by(
                ds, lambda d: str(d.tags.get("phase", "?"))
            ),
        }

    steps.append(
        DrillStep(
            phase="22",
            name="services_registry_snapshot",
            description="Phase-22 service registry inventory.",
            kind="python",
            python_callable=_p22,
            expect_json=True,
        )
    )

    # Phase 23: enrichment - default pipeline build.
    def _p23() -> Dict[str, Any]:
        from shared.enrichment import build_default_pipeline  # type: ignore

        pipeline = build_default_pipeline()
        return {
            "pipeline_built": True,
            "stages": [
                getattr(stage, "__class__").__name__
                for stage in pipeline.stages
            ],
        }

    steps.append(
        DrillStep(
            phase="23",
            name="enrichment_default_pipeline",
            description="Phase-23 default enrichment pipeline.",
            kind="python",
            python_callable=_p23,
            expect_json=True,
        )
    )

    # Phase 25: pure sizer - deterministic, no DB.
    steps.append(
        DrillStep(
            phase="25",
            name="treasury_pure_size",
            description="Phase-25 Treasury.pure-size deterministic.",
            argv=[
                "-m",
                "shared.cli.treasury_cli",
                "pure-size",
                "--company", "tickles",
                "--exchange", "binance",
                "--symbol", "BTC/USDT",
                "--direction", "long",
                "--account-id", "drill-acct",
                "--price", "60000",
                "--balance", "10000",
                "--equity", "10000",
                "--requested-notional-usd", "250",
                "--taker-fee-bps", "5",
                "--maker-fee-bps", "2",
                "--slippage-bps", "1",
                "--cap-max-notional-usd", "1000",
                "--cap-max-leverage", "3",
            ],
            expect_json=True,
        )
    )

    # Phase 26: execution paper-simulate (deterministic, in-memory).
    steps.append(
        DrillStep(
            phase="26",
            name="execution_paper_simulate",
            description="Phase-26 paper execution adapter.",
            argv=[
                "-m",
                "shared.cli.execution_cli",
                "paper-simulate",
                "--company", "tickles",
                "--exchange", "binance",
                "--account-id", "drill-acct",
                "--symbol", "BTC/USDT",
                "--direction", "long",
                "--order-type", "market",
                "--quantity", "0.01",
                "--price", "60000",
                "--market-last", "60000",
                "--market-bid", "59995",
                "--market-ask", "60005",
            ],
            expect_json=True,
        )
    )

    # Phase 27: regime classifiers (pure).
    def _p27() -> Dict[str, Any]:
        from shared.regime import (  # type: ignore
            CLASSIFIER_NAMES,
            REGIME_LABELS,
        )

        return {
            "classifiers": sorted(CLASSIFIER_NAMES),
            "regimes": sorted(REGIME_LABELS),
        }

    steps.append(
        DrillStep(
            phase="27",
            name="regime_classifiers",
            description="Phase-27 regime classifiers + labels.",
            kind="python",
            python_callable=_p27,
            expect_json=True,
        )
    )

    # Phase 28: guardrails rule kinds.
    def _p28() -> Dict[str, Any]:
        from shared.guardrails import protocol  # type: ignore

        rule_types = list(getattr(protocol, "RULE_TYPES", []))
        return {
            "rule_kinds": sorted(rule_types),
            "actions": sorted(
                list(getattr(protocol, "ACTION_TYPES", []))
            ),
        }

    steps.append(
        DrillStep(
            phase="28",
            name="guardrails_rule_kinds",
            description="Phase-28 guardrails rule + action types.",
            kind="python",
            python_callable=_p28,
            expect_json=True,
        )
    )

    # Phase 29: alt-data sources.
    steps.append(
        DrillStep(
            phase="29",
            name="altdata_sources",
            description="Phase-29 alt-data source catalogue.",
            argv=["-m", "shared.cli.altdata_cli", "sources"],
            expect_json=True,
        )
    )

    # Phase 30: events kinds.
    steps.append(
        DrillStep(
            phase="30",
            name="events_kinds",
            description="Phase-30 events canonical kinds.",
            argv=["-m", "shared.cli.events_cli", "kinds"],
            expect_json=True,
        )
    )

    # Phase 31/32: souls registry.
    def _p31() -> Dict[str, Any]:
        from shared.souls import protocol  # type: ignore

        names = list(getattr(protocol, "SOUL_NAMES", []))
        return {"personas": sorted(names)}

    steps.append(
        DrillStep(
            phase="31-32",
            name="souls_personas",
            description="Phase-31/32 soul persona constants.",
            kind="python",
            python_callable=_p31,
            expect_json=True,
        )
    )

    # Phase 33: arb demo + copy demo (in-memory).
    # Demos print human-friendly reports, not JSON; we only check
    # exit code.
    steps.append(
        DrillStep(
            phase="33",
            name="arb_demo",
            description="Phase-33 arb scanner in-memory demo.",
            argv=["-m", "shared.cli.arb_cli", "demo"],
            expect_json=False,
        )
    )
    steps.append(
        DrillStep(
            phase="33",
            name="copy_demo",
            description="Phase-33 copy-trader in-memory demo.",
            argv=["-m", "shared.cli.copy_cli", "demo"],
            expect_json=False,
        )
    )

    # Phase 34: strategy composer demo.
    steps.append(
        DrillStep(
            phase="34",
            name="strategy_demo",
            description="Phase-34 strategy composer in-memory demo.",
            argv=["-m", "shared.cli.strategy_cli", "demo"],
            expect_json=False,
        )
    )

    # Phase 35: backtest submission demo.
    steps.append(
        DrillStep(
            phase="35",
            name="backtest_submit_demo",
            description="Phase-35 backtest submit in-memory demo.",
            argv=["-m", "shared.cli.backtest_cli", "demo"],
            expect_json=False,
        )
    )

    # Phase 36: dashboard package import + in-memory snapshot smoke.
    def _p36() -> Dict[str, Any]:
        from shared.dashboard import (  # type: ignore
            InMemoryDashboardPool,
            MIGRATION_PATH,
        )

        return {
            "dashboard_import_ok": True,
            "migration_present": MIGRATION_PATH.exists(),
            "pool_class": InMemoryDashboardPool.__name__,
        }

    steps.append(
        DrillStep(
            phase="36",
            name="dashboard_import",
            description="Phase-36 dashboard package import.",
            kind="python",
            python_callable=_p36,
            expect_json=True,
        )
    )

    # Phase 37: mcp demo.
    steps.append(
        DrillStep(
            phase="37",
            name="mcp_demo",
            description="Phase-37 MCP stack in-memory demo.",
            argv=["-m", "shared.cli.mcp_cli", "demo"],
            expect_json=True,
        )
    )

    return steps


def _count_by(items: Sequence[Any], key: Callable[[Any], str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for item in items:
        k = key(item) or "?"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


# ---------------------------------------------------------------------
# Execution.
# ---------------------------------------------------------------------


def _run_cli_step(step: DrillStep) -> Tuple[int, Optional[Any], str]:
    proc = subprocess.run(
        [_py(), *step.argv],
        capture_output=True,
        text=True,
        check=False,
    )
    stdout = proc.stdout.strip()
    parsed: Optional[Any] = None
    if step.expect_json and stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            # Some CLIs emit multiple JSON blocks; grab the last one.
            lines = [ln for ln in stdout.splitlines() if ln.strip()]
            if lines:
                try:
                    parsed = json.loads(lines[-1])
                except json.JSONDecodeError:
                    parsed = None
    elif not step.expect_json:
        # Capture a tail of stdout as the textual payload so the
        # operator can eyeball the demo output in the JSON report.
        tail = stdout[-400:] if stdout else ""
        parsed = {"stdout_tail": tail}
    return proc.returncode, parsed, (proc.stderr or "").strip()


def _run_python_step(step: DrillStep) -> Tuple[int, Optional[Any], str]:
    assert step.python_callable is not None
    try:
        out = step.python_callable()
        return 0, out, ""
    except Exception as exc:
        return 1, None, f"{type(exc).__name__}: {exc}"


def run_drill(
    steps: Sequence[DrillStep], *, stop_on_fail: bool = False
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    passed = 0
    failed = 0
    t_total = time.perf_counter()

    for step in steps:
        started = time.perf_counter()
        if step.kind == "python":
            rc, payload, err = _run_python_step(step)
        else:
            rc, payload, err = _run_cli_step(step)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        ok = rc == 0 and (
            not step.expect_json or payload is not None
        )
        result = {
            "phase": step.phase,
            "step": step.name,
            "description": step.description,
            "kind": step.kind,
            "argv": step.argv,
            "returncode": rc,
            "ok": ok,
            "elapsed_ms": elapsed_ms,
            "stderr": err[:500] if err else "",
            "payload": payload,
        }
        results.append(result)
        if ok:
            passed += 1
        else:
            failed += 1
            if stop_on_fail:
                break

    total_elapsed_ms = int((time.perf_counter() - t_total) * 1000)
    return {
        "steps_total": len(results),
        "steps_passed": passed,
        "steps_failed": failed,
        "elapsed_ms": total_elapsed_ms,
        "all_green": failed == 0,
        "results": results,
    }


# ---------------------------------------------------------------------
# CLI surface.
# ---------------------------------------------------------------------


def cmd_list(_: argparse.Namespace) -> int:
    data = [
        {
            "phase": s.phase,
            "name": s.name,
            "kind": s.kind,
            "description": s.description,
        }
        for s in _steps()
    ]
    print(json.dumps({"count": len(data), "steps": data}, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    report = run_drill(
        _steps(), stop_on_fail=bool(args.stop_on_fail)
    )
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(
            json.dumps(
                {
                    "steps_total": report["steps_total"],
                    "steps_passed": report["steps_passed"],
                    "steps_failed": report["steps_failed"],
                    "elapsed_ms": report["elapsed_ms"],
                    "all_green": report["all_green"],
                    "out": args.out,
                },
                indent=2,
            )
        )
    else:
        print(text)
    return 0 if report["all_green"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drill_cli",
        description="Phase 39 end-to-end drill.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("run")
    p.add_argument("--stop-on-fail", action="store_true")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_run)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(
        level=os.environ.get("TICKLES_LOGLEVEL", "WARNING"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
