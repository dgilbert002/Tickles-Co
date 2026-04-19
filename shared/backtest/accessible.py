"""
Accessible Backtests — Tickles & Co V2.0 (hardened 2026-04-17)
===============================================================

Maintains a flat `backtests.txt` file rebuildable from ClickHouse, plus
lookup and top-N helpers used by the catalog service.

HARDENING (audit 2026-04-17):
  * `CH_PASSWORD` has NO default — raises on missing env.
  * `append()` is atomic: writes to a sibling temp file then cat's it into
    place via `os.replace()` of the combined output (matching `rebuild()`).
    No more partial lines on crash.
  * State file is written via temp-file + `os.replace()` (atomic).
  * Watermark boundary uses strict `>` instead of `>=`. The catch for rows
    that share a `created_at` to the second is a sub-second tie-breaker on
    `run_id` (composite cursor).
  * `top()` validates `sort` against a whitelist (unchanged — was already
    good) and uses ClickHouse `JSONExtractString` to filter by strategy
    instead of fragile `LIKE`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from clickhouse_driver import Client  # type: ignore[import-not-found]

log = logging.getLogger("tickles.accessible")

DEFAULT_PATH = Path(os.getenv(
    "TICKLES_BACKTESTS_TXT", "/opt/tickles/shared/backtests.txt"))
DEFAULT_STATE = Path(os.getenv(
    "TICKLES_BACKTESTS_STATE", "/opt/tickles/shared/.backtests_state.json"))


def _client() -> Client:
    pwd = os.getenv("CH_PASSWORD")
    if pwd is None:
        raise RuntimeError(
            "CH_PASSWORD env var not set. Accessible refuses to use a default."
        )
    return Client(
        host=os.getenv("CH_HOST", "127.0.0.1"),
        port=int(os.getenv("CH_PORT", "9000")),
        user=os.getenv("CH_USER", "admin"),
        password=pwd,
        database=os.getenv("CH_DATABASE", "backtests"),
        send_receive_timeout=int(os.getenv("CH_TIMEOUT", "60")),
    )


_BASE_COLS = (
    "run_id, param_hash, created_at, symbol, exchange, timeframe, "
    "indicator_name, params, date_from, date_to, "
    "sharpe_ratio, max_drawdown_pct, win_rate_pct, total_return_pct, "
    "total_trades, run_duration_ms, notes"
)

_QUERY_BASE = f"SELECT {_BASE_COLS} FROM backtest_runs"  # nosec B608


def _format_row(row) -> str:
    (run_id, phash, created_at, symbol, exchange, tf,
     ind_name, params_json, date_from, date_to,
     sharpe, mdd, winrate, pnl, n_trades, rt_ms, notes) = row

    try:
        params = json.loads(params_json) if params_json else {}
    except Exception:
        params = {}
    try:
        notes_d = json.loads(notes) if notes else {}
    except Exception:
        notes_d = {}

    strategy = notes_d.get("strategy_name") or ind_name or "?"
    params_str = ",".join(f"{k}={v}" for k, v in sorted(params.items()))
    ind_display = f"{ind_name}({params_str})" if ind_name else strategy

    return (
        f"{run_id} | {str(phash)[:12]} | {created_at:%Y-%m-%d %H:%M:%S} | "
        f"{symbol:<15} | {exchange:<10} | {tf:<4} | "
        f"{date_from}..{date_to} | {strategy:<22} | {ind_display:<48} | "
        f"sharpe={float(sharpe):>7.2f} | mdd={float(mdd):>7.2f}% | "
        f"wr={float(winrate):>5.1f}% | ret={float(pnl):>8.2f}% | "
        f"n={int(n_trades):>4d} | rt={int(rt_ms):>7d}ms"
    )


def _rows(client: Client, since: Optional[Tuple[datetime, str]] = None,
          limit: Optional[int] = None) -> Iterable:
    """Iterate rows in deterministic (created_at, run_id) ascending order.

    `since` is a (timestamp, run_id_str) cursor; we emit rows that are
    strictly AFTER that composite key. If `since` is None, emit all rows.
    """
    q = _QUERY_BASE
    params: dict = {}
    if since is not None:
        ts, last_run_id = since
        # Composite cursor: strictly > (created_at, run_id).
        # Pass 2 P2: if `last_run_id` is empty (first-ever cursor, or the
        # state file was truncated), the OR branch degrades to
        # `toString(run_id) > ''` which matches EVERY row at created_at=ts
        # — causing duplicates. When we have no run_id, require a strictly
        # greater created_at only.
        if last_run_id:
            q += (" WHERE created_at > %(since)s"
                  " OR (created_at = %(since)s AND toString(run_id) > %(last_run_id)s)")
            params["since"] = ts
            params["last_run_id"] = last_run_id
        else:
            q += " WHERE created_at > %(since)s"
            params["since"] = ts
    q += " ORDER BY created_at ASC, run_id ASC"
    if limit:
        q += f" LIMIT {int(limit)}"
    return client.execute_iter(q, params)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text to path atomically via temp-file + os.replace()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, dir=path.parent, prefix="." + path.name + ".",
    ) as tmp:
        tmp.write(text)
        tmp_name = tmp.name
    os.replace(tmp_name, path)


def _save_state(state: dict) -> None:
    _atomic_write_text(DEFAULT_STATE, json.dumps(state, default=str))


def _load_state() -> dict:
    if not DEFAULT_STATE.exists():
        return {}
    try:
        return json.loads(DEFAULT_STATE.read_text())
    except Exception:
        log.warning("state file corrupt, ignoring; full rebuild will run")
        return {}


def _header() -> str:
    return (
        "# Tickles backtests.txt — auto-generated, do not edit\n"
        f"# Last rebuild: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ}\n"
        "# Columns: run_id | param_hash | created_at | symbol | exchange | "
        "tf | date_from..date_to | strategy | indicator(params) | sharpe | "
        "mdd% | wr% | return% | trades | run_ms\n"
    )


def _latest_cursor(client: Client) -> Optional[Tuple[datetime, str]]:
    """Return (max_created_at, run_id_at_that_ts) or None if empty."""
    rows = client.execute(
        "SELECT created_at, toString(run_id) FROM backtest_runs "
        "ORDER BY created_at DESC, run_id DESC LIMIT 1")
    if not rows:
        return None
    return rows[0][0], rows[0][1]


def rebuild(path: Path = DEFAULT_PATH) -> int:
    """Rewrite the entire file from scratch. Atomic."""
    log.info("rebuild: %s", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    client = _client()
    n = 0
    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, dir=path.parent, prefix=".bt_",
    ) as tmp:
        tmp.write(_header())
        for row in _rows(client):
            tmp.write(_format_row(row) + "\n")
            n += 1
        tmp_name = tmp.name
    os.replace(tmp_name, path)

    cur = _latest_cursor(client)
    state = {
        "last_created_at": cur[0].isoformat() if cur else None,
        "last_run_id":     cur[1] if cur else None,
        "count":           n,
        "rebuilt_at":      datetime.now(timezone.utc).isoformat(),
    }
    _save_state(state)
    log.info("rebuild: wrote %d rows", n)
    return n


def append(path: Path = DEFAULT_PATH) -> int:
    """Append rows with a composite cursor strictly greater than last watermark.

    Atomic: builds a combined (existing file + new rows) temp file and
    os.replaces it, so a crash during append leaves the old file intact.
    """
    state = _load_state()
    since_iso = state.get("last_created_at")
    last_run_id = state.get("last_run_id", "")
    if not since_iso or not path.exists():
        return rebuild(path)
    try:
        since_ts = datetime.fromisoformat(since_iso)
    except Exception:
        return rebuild(path)

    client = _client()

    # Gather new rows into a list first so we can decide whether to touch the file.
    new_rows = list(_rows(client, since=(since_ts, last_run_id)))
    if not new_rows:
        log.info("append: +0 rows (no new backtests since %s)", since_iso)
        return 0

    # Build the combined output atomically: copy existing file, append new.
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", delete=False, dir=path.parent, prefix=".bt_",
    ) as tmp:
        # Stream existing file into tmp.
        with open(path, "r") as src:
            for chunk in iter(lambda: src.read(1024 * 1024), ""):
                tmp.write(chunk)
        # Append new rows.
        n = 0
        latest_ts: Optional[datetime] = None
        latest_id: Optional[str] = None
        for row in new_rows:
            tmp.write(_format_row(row) + "\n")
            n += 1
            created_at = row[2]
            run_id = str(row[0])
            if latest_ts is None or (created_at, run_id) > (latest_ts, latest_id or ""):
                latest_ts = created_at
                latest_id = run_id
        tmp_name = tmp.name
    os.replace(tmp_name, path)

    state["last_created_at"] = latest_ts.isoformat() if latest_ts else since_iso
    state["last_run_id"] = latest_id or last_run_id
    state["count"] = state.get("count", 0) + n
    state["appended_at"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)
    log.info("append: +%d rows", n)
    return n


def lookup(param_hash_or_run: str) -> dict:
    """Return the full row for a given param_hash (or run_id)."""
    client = _client()
    q = (
        "SELECT * FROM backtest_runs "
        "WHERE param_hash = %(h)s OR toString(run_id) = %(h)s LIMIT 1"
    )
    rows = client.execute(q, {"h": param_hash_or_run}, with_column_types=True)
    data, cols = rows if isinstance(rows, tuple) else (rows, [])
    if not data:
        return {}
    col_names = [c[0] for c in cols]
    return dict(zip(col_names, data[0]))


_SORT_ALIAS = {
    "sharpe":          "sharpe_ratio",
    "sortino":         "sortino_ratio",
    "deflated_sharpe": "deflated_sharpe",
    "pnl_pct":         "total_return_pct",
    "return_pct":      "total_return_pct",
    "winrate":         "win_rate_pct",
    "profit_factor":   "profit_factor",
}


def top(n: int = 20, sort: str = "sharpe",
        symbol: Optional[str] = None,
        strategy: Optional[str] = None,
        min_trades: int = 5) -> List[dict]:
    """Return top-N backtests by a chosen metric."""
    if sort not in _SORT_ALIAS:
        raise ValueError(f"sort must be in {sorted(_SORT_ALIAS)}")
    order_col = _SORT_ALIAS[sort]

    n = int(max(1, min(int(n or 20), 500)))
    min_trades = int(max(0, int(min_trades or 0)))

    where = [f"total_trades >= {min_trades}"]
    params: dict = {"n": n}
    if symbol:
        where.append("symbol = %(symbol)s")
        params["symbol"] = symbol
    if strategy:
        # Robust JSON extraction; doesn't care about whitespace in notes.
        where.append("JSONExtractString(notes, 'strategy_name') = %(strategy)s")
        params["strategy"] = strategy
    # where[] fragments are module-local, order_col is allow-listed, values bound via params.
    q = (
        "SELECT run_id, symbol, exchange, timeframe, indicator_name, params, "  # nosec B608
        "sharpe_ratio, sortino_ratio, deflated_sharpe, win_rate_pct, "
        "total_return_pct, max_drawdown_pct, total_trades, notes "
        f"FROM backtest_runs WHERE {' AND '.join(where)} "
        f"ORDER BY {order_col} DESC LIMIT %(n)s"
    )
    rows = _client().execute(q, params)
    keys = ["run_id", "symbol", "exchange", "timeframe", "indicator_name",
            "params", "sharpe", "sortino", "deflated_sharpe", "winrate",
            "return_pct", "max_drawdown", "num_trades", "notes"]
    return [dict(zip(keys, r)) for r in rows]


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rebuild")
    sub.add_parser("append")
    pl = sub.add_parser("lookup")
    pl.add_argument("hash_or_id")
    pt = sub.add_parser("top")
    pt.add_argument("--n", type=int, default=20)
    pt.add_argument("--sort", default="sharpe")
    pt.add_argument("--symbol", default=None)
    pt.add_argument("--strategy", default=None)
    args = ap.parse_args()

    if args.cmd == "rebuild":
        print(f"wrote {rebuild()} rows to {DEFAULT_PATH}")
    elif args.cmd == "append":
        print(f"appended {append()} rows to {DEFAULT_PATH}")
    elif args.cmd == "lookup":
        row = lookup(args.hash_or_id)
        print(json.dumps(row, default=str, indent=2) if row else "not found")
    elif args.cmd == "top":
        rows = top(n=args.n, sort=args.sort, symbol=args.symbol,
                   strategy=args.strategy)
        for r in rows:
            print(json.dumps(r, default=str))


if __name__ == "__main__":
    main()
