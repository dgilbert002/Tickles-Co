"""
shared.cli.enrichment_cli — Phase 23 operator CLI.

Subcommands (single-line JSON stdout):

* ``stages``                 — list every enrichment stage.
* ``enrich-text "..."``      — run the pipeline against ad-hoc
  text (no DB). Useful for smoke tests + tuning.
* ``pending-count``          — how many rows are still unenriched?
* ``enrich-batch --limit N`` — fetch ``N`` pending rows and enrich
  them (DB write). Honours ``TICKLES_SHARED_DSN``.
* ``dry-run --limit N``      — like ``enrich-batch`` but does NOT
  write back; prints summaries.
* ``apply-migration``        — print the migration SQL path + a
  helper command to apply it via ``psql``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any, Dict, List

from shared.cli._common import (
    EXIT_FAIL,
    EXIT_OK,
    Subcommand,
    build_parser,
    emit,
    run,
)
from shared.enrichment import build_default_pipeline
from shared.enrichment.news_enricher import (
    EnricherConfig,
    NewsEnricher,
    enrich_text_once,
)


def cmd_stages(args: argparse.Namespace) -> int:
    del args
    pipe = build_default_pipeline()
    emit(
        {
            "ok": True,
            "count": len(pipe.stages),
            "stages": [
                {"name": s.name, "class": type(s).__name__}
                for s in pipe.stages
            ],
        }
    )
    return EXIT_OK


def cmd_enrich_text(args: argparse.Namespace) -> int:
    summary = enrich_text_once(args.headline or "", args.content or "")
    emit({"ok": True, "summary": summary})
    return EXIT_OK


def _make_enricher(args: argparse.Namespace) -> NewsEnricher:
    dsn = (
        getattr(args, "dsn", None)
        or os.environ.get("TICKLES_SHARED_DSN")
    )
    cfg = EnricherConfig(dsn=dsn)
    return NewsEnricher(config=cfg)


def cmd_pending_count(args: argparse.Namespace) -> int:
    enricher = _make_enricher(args)
    try:
        n = asyncio.run(enricher.pending_count())
    except Exception as exc:  # noqa: BLE001
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit({"ok": True, "pending": int(n)})
    return EXIT_OK


def cmd_enrich_batch(args: argparse.Namespace) -> int:
    enricher = _make_enricher(args)
    enricher.config.max_rows_per_run = int(args.limit)
    try:
        summary = asyncio.run(enricher.process_pending(int(args.limit)))
    except Exception as exc:  # noqa: BLE001
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit({"ok": True, "result": summary})
    return EXIT_OK


def cmd_dry_run(args: argparse.Namespace) -> int:
    enricher = _make_enricher(args)
    try:
        rows = asyncio.run(enricher.fetch_pending(int(args.limit)))
        summaries: List[Dict[str, Any]] = asyncio.run(enricher.enrich_batch(rows))
    except Exception as exc:  # noqa: BLE001
        emit({"ok": False, "error": str(exc), "type": type(exc).__name__})
        return EXIT_FAIL
    emit(
        {
            "ok": True,
            "fetched": len(rows),
            "summaries": summaries,
        }
    )
    return EXIT_OK


def cmd_apply_migration(args: argparse.Namespace) -> int:
    del args
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, os.pardir, os.pardir))
    path = os.path.join(
        repo_root,
        "shared",
        "enrichment",
        "migrations",
        "2026_04_19_phase23_enrichment.sql",
    )
    emit(
        {
            "ok": True,
            "migration_path": path,
            "apply_example": (
                f"psql -h 127.0.0.1 -U admin -d tickles_shared -f {path}"
            ),
        }
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_enrich_text(p: argparse.ArgumentParser) -> None:
    p.add_argument("--headline", default="")
    p.add_argument("--content", default="")


def _build_enrich_batch(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--dsn", default=None)


def _build_dry_run(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--dsn", default=None)


def _build_pending_count(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=None)


def _build_stages(p: argparse.ArgumentParser) -> None:
    del p


def _build_apply_migration(p: argparse.ArgumentParser) -> None:
    del p


def main() -> int:
    subs = [
        Subcommand("stages", "List enrichment stages.", cmd_stages, build=_build_stages),
        Subcommand("enrich-text", "Enrich ad-hoc text (no DB).",
                   cmd_enrich_text, build=_build_enrich_text),
        Subcommand("pending-count", "Count rows not yet enriched.",
                   cmd_pending_count, build=_build_pending_count),
        Subcommand("enrich-batch", "Enrich up to N pending rows (DB write).",
                   cmd_enrich_batch, build=_build_enrich_batch),
        Subcommand("dry-run", "Fetch + enrich without writing back.",
                   cmd_dry_run, build=_build_dry_run),
        Subcommand("apply-migration", "Print migration path + psql command.",
                   cmd_apply_migration, build=_build_apply_migration),
    ]
    parser = build_parser(
        "enrichment_cli",
        "Operator CLI for the enrichment pipeline.",
        subs,
    )
    return run(parser)


if __name__ == "__main__":
    raise SystemExit(main())
