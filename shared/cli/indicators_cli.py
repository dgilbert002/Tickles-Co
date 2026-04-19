"""
shared.cli.indicators_cli — Indicator Library operator CLI (Phase 18).

The 21-year-old version
=======================
Phase 18 expands the indicator catalog from 23 hand-rolled basics to
250+ functions by wrapping pandas_ta and adding an extras module.

This CLI is how an operator (or an agent over SSH) inspects what's
registered in the running Python process without booting the full
backtest engine:

    python -m shared.cli.indicators_cli count
    python -m shared.cli.indicators_cli list                 # JSON names
    python -m shared.cli.indicators_cli categories           # {cat: n}
    python -m shared.cli.indicators_cli describe <name>      # one spec
    python -m shared.cli.indicators_cli search <substr>      # fuzzy list

Output is always single-line JSON to stdout (pipe-friendly), so Dean
can grep / jq it from PowerShell or bash alike.
"""
from __future__ import annotations

import argparse
from typing import Any, Dict, List

from shared.cli._common import (
    EXIT_FAIL,
    EXIT_OK,
    Subcommand,
    build_parser,
    emit,
    run,
)


def _load_registry() -> Dict[str, Any]:
    """Lazy import — avoids pandas on CLI help/usage."""
    from shared.backtest.indicators import INDICATORS  # type: ignore[import-not-found]
    return INDICATORS


# ---------------------------------------------------------------------------
# count
# ---------------------------------------------------------------------------


def cmd_count(args: argparse.Namespace) -> int:
    reg = _load_registry()
    by_cat: Dict[str, int] = {}
    by_dir: Dict[str, int] = {}
    for spec in reg.values():
        by_cat[spec.category] = by_cat.get(spec.category, 0) + 1
        by_dir[spec.direction] = by_dir.get(spec.direction, 0) + 1
    emit({
        "ok": True,
        "total": len(reg),
        "by_category": by_cat,
        "by_direction": by_dir,
    })
    return EXIT_OK


# ---------------------------------------------------------------------------
# list / categories / describe / search
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    reg = _load_registry()
    items: List[Dict[str, Any]] = []
    for name in sorted(reg.keys()):
        spec = reg[name]
        if args.category and spec.category != args.category:
            continue
        if args.direction and spec.direction != args.direction:
            continue
        items.append({
            "name": name,
            "category": spec.category,
            "direction": spec.direction,
            "description": spec.description,
        })
    emit({"ok": True, "count": len(items), "indicators": items})
    return EXIT_OK


def cmd_categories(args: argparse.Namespace) -> int:
    reg = _load_registry()
    by_cat: Dict[str, List[str]] = {}
    for name, spec in reg.items():
        by_cat.setdefault(spec.category, []).append(name)
    emit({
        "ok": True,
        "categories": {cat: sorted(names) for cat, names in by_cat.items()},
        "totals": {cat: len(v) for cat, v in by_cat.items()},
    })
    return EXIT_OK


def cmd_describe(args: argparse.Namespace) -> int:
    reg = _load_registry()
    spec = reg.get(args.name)
    if spec is None:
        emit({"ok": False, "error": f"indicator '{args.name}' not registered"})
        return EXIT_FAIL
    emit({
        "ok": True,
        "name": spec.name,
        "category": spec.category,
        "direction": spec.direction,
        "description": spec.description,
        "defaults": spec.defaults,
        "param_ranges": {
            k: (list(v) if hasattr(v, "__iter__") and not isinstance(v, (str, bytes)) else v)
            for k, v in spec.param_ranges.items()
        },
        "asset_class": spec.asset_class,
    })
    return EXIT_OK


def cmd_search(args: argparse.Namespace) -> int:
    reg = _load_registry()
    q = args.query.lower()
    matches: List[Dict[str, Any]] = []
    for name, spec in reg.items():
        haystack = f"{name} {spec.description}".lower()
        if q in haystack:
            matches.append({
                "name": name,
                "category": spec.category,
                "description": spec.description,
            })
    matches.sort(key=lambda m: m["name"])
    emit({"ok": True, "query": args.query, "matches": matches, "count": len(matches)})
    return EXIT_OK


# ---------------------------------------------------------------------------
# parser wiring
# ---------------------------------------------------------------------------


def _build_list(p: argparse.ArgumentParser) -> None:
    p.add_argument("--category", help="filter by category (trend, momentum, ...)")
    p.add_argument("--direction", help="filter by direction (bullish/bearish/neutral)")


def _build_describe(p: argparse.ArgumentParser) -> None:
    p.add_argument("name", help="indicator name, e.g. rsi, pta_macd_line, ext_zscore")


def _build_search(p: argparse.ArgumentParser) -> None:
    p.add_argument("query", help="substring match against name + description")


def main(argv: List[str] | None = None) -> int:
    subs = [
        Subcommand("count", "Total indicators + counts per category.", cmd_count),
        Subcommand("list", "List indicator specs (optional filters).", cmd_list, build=_build_list),
        Subcommand("categories", "Names grouped by category.", cmd_categories),
        Subcommand("describe", "Full spec for one indicator.", cmd_describe, build=_build_describe),
        Subcommand("search", "Name/description substring search.", cmd_search, build=_build_search),
    ]
    parser = build_parser(
        prog="indicators_cli",
        description="Inspect the Tickles & Co indicator library (Phase 18+).",
        subcommands=subs,
    )
    if argv is not None:
        import sys
        sys.argv = ["indicators_cli", *argv]
    return run(parser)


if __name__ == "__main__":
    raise SystemExit(main())
