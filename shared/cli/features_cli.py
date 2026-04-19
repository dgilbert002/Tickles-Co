"""
shared.cli.features_cli — Feature Store operator CLI (Phase 20).

Subcommands:

* ``list``            — every registered feature view (+ entity + features).
* ``describe <name>`` — full metadata for one feature view.
* ``materialize``     — pull candles from the ``candles`` table (or a
                        supplied parquet file) and write features to
                        the online + offline stores.
* ``online-get``      — fetch the latest online vector for an entity.
* ``historical-get``  — fetch a historical range for one or more
                        entities (printed as JSON records).
* ``partitions``      — list entity partitions that exist on disk
                        for a view.

All stdout is single-line JSON.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import asdict
from typing import Any, List, Optional

import pandas as pd

from shared.cli._common import (
    EXIT_FAIL,
    EXIT_OK,
    Subcommand,
    build_parser,
    emit,
    run,
)
from shared.features import (
    FeatureStore,
    feature_sets,
    get_feature_view,
    list_feature_views,
)

log = logging.getLogger("tickles.features.cli")

_ = feature_sets  # keep import for registration side-effect


# -------------------------------------------------------------- helpers

def _store_from_args(args: argparse.Namespace) -> FeatureStore:
    use_mem = bool(getattr(args, "in_memory", False))
    return FeatureStore(use_in_memory_online=use_mem)


def _load_candles_parquet(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if "ts" in df.columns:
        df = df.set_index(pd.DatetimeIndex(df["ts"], name="ts")).drop(columns=["ts"])
    elif not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("candles parquet must have a 'ts' column or DatetimeIndex")
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"candles missing columns {sorted(missing)}")
    return df


def _load_candles_from_db(
    symbol: str,
    venue: str,
    timeframe: str,
    start: Optional[str],
    end: Optional[str],
    limit: int,
) -> pd.DataFrame:
    """Best-effort load from the existing ``tickles_shared.candles`` table.

    Uses the project's own ``DatabasePool`` (``shared.utils.db``) so we
    pick up the standard ``DB_HOST / DB_PORT / DB_USER / DB_PASSWORD``
    env config that every other service already uses. Falls back
    cleanly if the DB isn't reachable — the operator can pass
    ``--parquet PATH`` instead.

    Falls back to raw asyncpg + ``TICKLES_CANDLES_DSN`` if the project
    db utilities aren't importable (e.g. running from an unrelated
    venv).
    """
    import asyncio
    import os

    try:
        from shared.utils import db as _dbmod  # type: ignore[import-not-found]
    except Exception:
        _dbmod = None  # type: ignore[assignment]

    async def _fetch_via_pool() -> List[Any]:
        pool = await _dbmod.get_shared_pool()  # type: ignore[union-attr]
        where = ["symbol = $1", "venue = $2", "timeframe = $3"]
        params: List[Any] = [symbol, venue, timeframe]
        idx = 4
        if start:
            where.append(f"ts >= ${idx}")
            params.append(pd.Timestamp(start).to_pydatetime())
            idx += 1
        if end:
            where.append(f"ts <= ${idx}")
            params.append(pd.Timestamp(end).to_pydatetime())
            idx += 1
        sql = (
            "SELECT ts, open, high, low, close, volume "
            "FROM candles WHERE " + " AND ".join(where) + f" ORDER BY ts DESC LIMIT ${idx}"
        )
        params.append(limit)
        return list(await pool.fetch_all(sql, tuple(params)))

    async def _fetch_via_dsn() -> List[Any]:
        import asyncpg  # type: ignore[import-not-found]

        dsn = os.environ.get("TICKLES_CANDLES_DSN")
        if not dsn:
            raise RuntimeError(
                "TICKLES_CANDLES_DSN not set and shared.utils.db not importable"
            )
        where = ["symbol = $1", "venue = $2", "timeframe = $3"]
        params: List[Any] = [symbol, venue, timeframe]
        idx = 4
        if start:
            where.append(f"ts >= ${idx}")
            params.append(pd.Timestamp(start).to_pydatetime())
            idx += 1
        if end:
            where.append(f"ts <= ${idx}")
            params.append(pd.Timestamp(end).to_pydatetime())
            idx += 1
        sql = (
            "SELECT ts, open, high, low, close, volume "
            "FROM candles WHERE " + " AND ".join(where) + f" ORDER BY ts DESC LIMIT ${idx}"
        )
        params.append(limit)
        conn = await asyncpg.connect(dsn)
        try:
            return list(await conn.fetch(sql, *params))
        finally:
            await conn.close()

    rows: List[Any]
    if _dbmod is not None:
        rows = asyncio.run(_fetch_via_pool())
    else:  # pragma: no cover
        rows = asyncio.run(_fetch_via_dsn())

    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame([dict(r) for r in rows])
    df = df.sort_values("ts")
    df = df.set_index(pd.DatetimeIndex(df["ts"], name="ts")).drop(columns=["ts"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df


# -------------------------------------------------------------- commands

def cmd_list(_: argparse.Namespace) -> int:
    views = list_feature_views()
    emit(
        {
            "ok": True,
            "count": len(views),
            "views": [
                {
                    "name": fv.name,
                    "entities": [e.name for e in fv.entities],
                    "num_features": len(fv.features),
                    "features": [f.name for f in fv.features],
                    "ttl_seconds": fv.ttl_seconds,
                    "description": fv.description,
                    "tags": fv.tags,
                }
                for fv in views
            ],
        }
    )
    return EXIT_OK


def cmd_describe(args: argparse.Namespace) -> int:
    fv = get_feature_view(args.name)
    emit(
        {
            "ok": True,
            "view": {
                "name": fv.name,
                "description": fv.description,
                "source": fv.source,
                "ttl_seconds": fv.ttl_seconds,
                "tags": fv.tags,
                "entities": [asdict(e) for e in fv.entities],
                "features": [
                    {"name": f.name, "dtype": f.dtype.value, "description": f.description}
                    for f in fv.features
                ],
            },
        }
    )
    return EXIT_OK


def cmd_materialize(args: argparse.Namespace) -> int:
    store = _store_from_args(args)

    if args.parquet:
        candles = _load_candles_parquet(args.parquet)
    else:
        candles = _load_candles_from_db(
            symbol=args.symbol,
            venue=args.venue,
            timeframe=args.timeframe,
            start=args.start,
            end=args.end,
            limit=args.limit,
        )

    if candles.empty:
        emit({"ok": False, "error": "no candles", "view": args.view, "entity": args.entity})
        return EXIT_FAIL

    summary = store.materialize(
        view_name=args.view,
        entity_key=args.entity,
        candles_df=candles,
        write_online=not args.no_online,
        write_offline=not args.no_offline,
    )
    emit(
        {
            "ok": True,
            "summary": summary,
            "candles_rows": int(len(candles)),
            "candles_first": str(candles.index[0]),
            "candles_last": str(candles.index[-1]),
        }
    )
    return EXIT_OK


def cmd_online_get(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    vec = store.get_online(args.view, args.entity)
    emit({"ok": True, "view": args.view, "entity": args.entity, "values": vec})
    return EXIT_OK if vec is not None else EXIT_FAIL


def cmd_historical_get(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    df = store.get_historical(
        args.view,
        entity_keys=args.entities.split(","),
        start=pd.Timestamp(args.start) if args.start else None,
        end=pd.Timestamp(args.end) if args.end else None,
    )
    df2 = df.copy()
    if "ts" in df2.columns:
        df2["ts"] = df2["ts"].astype(str)
    emit(
        {
            "ok": True,
            "view": args.view,
            "entities": args.entities.split(","),
            "rows": int(len(df2)),
            "records": df2.head(args.head).to_dict(orient="records") if not df2.empty else [],
        }
    )
    return EXIT_OK


def cmd_partitions(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    fv = get_feature_view(args.view)
    parts = store.offline.partitions(fv)
    emit({"ok": True, "view": args.view, "count": len(parts), "partitions": parts})
    return EXIT_OK


# -------------------------------------------------------------- wiring

def _build_describe(p: argparse.ArgumentParser) -> None:
    p.add_argument("name", help="feature view name")


def _build_materialize(p: argparse.ArgumentParser) -> None:
    p.add_argument("--view", required=True)
    p.add_argument("--entity", required=True, help="entity key, e.g. binance:BTC/USDT")
    p.add_argument("--parquet", help="load candles from a parquet file instead of the DB")
    p.add_argument("--symbol", default="BTC/USDT")
    p.add_argument("--venue", default="binance")
    p.add_argument("--timeframe", default="1m")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--limit", type=int, default=10_000)
    p.add_argument("--no-online", action="store_true")
    p.add_argument("--no-offline", action="store_true")
    p.add_argument("--in-memory", action="store_true", help="use in-memory online store (tests)")


def _build_online_get(p: argparse.ArgumentParser) -> None:
    p.add_argument("--view", required=True)
    p.add_argument("--entity", required=True)
    p.add_argument("--in-memory", action="store_true")


def _build_historical_get(p: argparse.ArgumentParser) -> None:
    p.add_argument("--view", required=True)
    p.add_argument("--entities", required=True, help="comma-separated entity keys")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--head", type=int, default=5)
    p.add_argument("--in-memory", action="store_true")


def _build_partitions(p: argparse.ArgumentParser) -> None:
    p.add_argument("--view", required=True)
    p.add_argument("--in-memory", action="store_true")


def main(argv: Optional[List[str]] = None) -> int:
    subs = [
        Subcommand("list", "List registered feature views.", cmd_list),
        Subcommand("describe", "Describe one feature view.", cmd_describe, build=_build_describe),
        Subcommand(
            "materialize",
            "Compute + write features for an entity.",
            cmd_materialize,
            build=_build_materialize,
        ),
        Subcommand(
            "online-get",
            "Read the latest online vector for an entity.",
            cmd_online_get,
            build=_build_online_get,
        ),
        Subcommand(
            "historical-get",
            "Read a historical range.",
            cmd_historical_get,
            build=_build_historical_get,
        ),
        Subcommand(
            "partitions",
            "List entity partitions stored on disk for a view.",
            cmd_partitions,
            build=_build_partitions,
        ),
    ]
    parser = build_parser(
        prog="features_cli",
        description="Tickles Feature Store — operator CLI (Phase 20).",
        subcommands=subs,
    )
    if argv is not None:
        import sys
        sys.argv = ["features_cli", *argv]
    return run(parser)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
