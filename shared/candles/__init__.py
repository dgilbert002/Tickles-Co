"""
shared.candles — candle plumbing.

Phase 13 shipped the live 1m daemon (`shared.candles.daemon`). Phase 16
adds three ADDITIVE modules:

    resample.py   — deterministic 1m -> 5m/15m/30m/1h/4h/1d/1w rollups
                     executed entirely in Postgres (INSERT ... SELECT
                     ... GROUP BY date_trunc) so nothing has to stream
                     through Python.
    backfill.py   — historical fetch loop (CCXT async) with rate-limiter
                     and retry; upserts into `candles` with the same
                     uniqueness contract the daemon uses.
    coverage.py   — read-only coverage queries for the Phase 16 CLI and
                     Phase 15 cache invalidation.

The daemon is deliberately untouched. The CLI surface is
`python -m shared.cli.candles_cli`.
"""

__all__ = ["backfill", "coverage", "daemon", "resample"]
