"""
Module: surgeon_position_reconciler
Purpose: Periodically reconcile Surgeon v1/v2 internal position tables with the
         shared ``tracked_positions`` ledger so the dashboard never diverges
         from reality if the forward-bridge missed an event.
Location: /opt/tickles/shared/intelligence/surgeon_position_reconciler.py

Slice 3, Change 4. Cadence: every 5 minutes per company. Idempotent. Safe to
re-run. Default lookback: 7 days (closed positions only get re-checked within
that window).

Reconciliation logic
--------------------
For each company under management:

1. Surgeon v2 (Postgres-native):
   * Read ``surgeon2_positions`` rows where ``closed_at IS NULL`` → upsert OPEN.
   * Read ``surgeon2_positions`` rows where ``closed_at`` is within
     ``--since`` window → upsert CLOSE (uses last matching ``surgeon2_trade_log``
     entry to derive ``exit_price`` / ``net_pnl`` / action tag).

2. Surgeon v1 (file-based, .surgeon_state.json):
   * If ``--surgeon1-state-file`` exists, parse open positions and forward
     them through the OPEN bridge. v1 has no DB-side history of closed
     positions other than ``state.closed_trades``; we walk that list and
     forward closes within the lookback window.

The bridge functions are themselves idempotent on
``(company_id, instrument_symbol_normalised, direction, status='open',
actor_type='surgeon', actor_id, metadata->>'source')`` so repeat reconciles
do not produce duplicates.

CLI
---
    python3 -m shared.intelligence.surgeon_position_reconciler \
        --company rubicon --since 7d --once
    python3 -m shared.intelligence.surgeon_position_reconciler \
        --company rubicon --interval 300
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from shared.intelligence.surgeon_position_bridge import (
    SOURCE_SURGEON_V1,
    SOURCE_SURGEON_V2,
    bridge_surgeon_position_close,
    bridge_surgeon_position_open,
)
from shared.utils.db import get_company_pool

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Constants / config
# -----------------------------------------------------------------------------
DEFAULT_INTERVAL_SEC = int(os.environ.get("SURGEON_RECON_INTERVAL_SEC", "300"))
DEFAULT_LOOKBACK = os.environ.get("SURGEON_RECON_SINCE", "7d")
DEFAULT_SURGEON1_ACTOR = os.environ.get("SURGEON1_ACTOR_ID", "surgeon1")
DEFAULT_SURGEON2_ACTOR = os.environ.get("SURGEON2_ACTOR_ID", "surgeon2")
DEFAULT_SURGEON1_STATE = os.environ.get(
    "SURGEON1_STATE_FILE", "/opt/tickles/.surgeon_state.json"
)
DEFAULT_EXCHANGE = os.environ.get("SURGEON_RECON_EXCHANGE", "bybit")

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86_400,
    "w": 604_800,
}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def parse_duration(value: str) -> timedelta:
    """Parse a duration string like ``"7d"`` / ``"30m"`` / ``"2h"`` into a timedelta.

    Args:
        value: Human-readable duration. Supported units: s, m, h, d, w.

    Returns:
        timedelta representing the duration.

    Raises:
        ValueError: If the format is unrecognised.
    """
    if not isinstance(value, str):
        raise ValueError(f"duration must be a string, got {type(value).__name__}")
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"invalid duration: {value!r} (expected e.g. '7d', '30m')")
    qty = int(match.group(1))
    unit = match.group(2).lower()
    return timedelta(seconds=qty * _DURATION_UNITS[unit])


def _utc_now() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def _coerce_ts(value: Any) -> Optional[datetime]:
    """Coerce a value into a UTC datetime, or return None on failure."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float coercion that swallows non-numeric inputs."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# -----------------------------------------------------------------------------
# Surgeon v2 reconciliation (Postgres-backed)
# -----------------------------------------------------------------------------
async def _fetch_v2_open_positions(pool, since_dt: datetime) -> List[Dict[str, Any]]:
    """Fetch all surgeon2 positions still open *or* closed inside the lookback window.

    Args:
        pool: Company asyncpg pool.
        since_dt: UTC cutoff — closed positions older than this are skipped.

    Returns:
        List of dicts (one per surgeon2 position).
    """
    query = """
        SELECT trade_id, symbol, side, entry_price, entry_ts, margin, leverage,
               notional, sl, tp1, tp2, tp3, tp1_done, tp2_done, remaining_frac,
               last_progress_ts, divergence_at_entry, funding_at_entry, reason,
               closed_at
          FROM surgeon2_positions
         WHERE closed_at IS NULL
            OR closed_at >= $1
         ORDER BY trade_id ASC
    """
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, since_dt)
    except Exception as exc:
        logger.warning("surgeon2_positions table unavailable: %s", exc)
        return []
    return [dict(r) for r in rows]


async def _fetch_v2_last_close_log(
    pool,
    trade_id: int,
) -> Optional[Dict[str, Any]]:
    """Fetch the most recent CLOSE-style trade_log row for a surgeon2 trade.

    Args:
        pool: Company asyncpg pool.
        trade_id: Surgeon v2 trade id.

    Returns:
        Dict for the most recent close action (SL/TP1/TP2/TP3/CONVERGENCE/
        TIME_STOP/STALL) for this trade, or None if no close was logged.
    """
    query = """
        SELECT ts, action, exit_price, net_pnl, reason
          FROM surgeon2_trade_log
         WHERE trade_id = $1
           AND action IN ('SL','TP1','TP2','TP3','CONVERGENCE','TIME_STOP','STALL')
         ORDER BY ts DESC
         LIMIT 1
    """
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, trade_id)
    except Exception as exc:
        logger.warning("surgeon2_trade_log unavailable for trade=%s: %s", trade_id, exc)
        return None
    return dict(row) if row else None


async def reconcile_surgeon_v2(
    company_id: str,
    *,
    actor_id: str = DEFAULT_SURGEON2_ACTOR,
    since: timedelta = timedelta(days=7),
    exchange: str = DEFAULT_EXCHANGE,
) -> Dict[str, int]:
    """Reconcile surgeon v2 positions for one company into tracked_positions.

    Args:
        company_id: Tickles company identifier (e.g. ``"rubicon"``).
        actor_id: Logical actor name to stamp on bridged rows.
        since: Lookback window for closed positions.
        exchange: Default exchange identifier for symbol normalisation.

    Returns:
        Counts dict with keys ``opens``, ``closes``, ``errors``, ``skipped``.
    """
    counts = {"opens": 0, "closes": 0, "errors": 0, "skipped": 0}
    cutoff = _utc_now() - since
    try:
        pool = await get_company_pool(company_id)
    except Exception as exc:
        logger.warning("[%s] no company pool for surgeon-v2 recon: %s", company_id, exc)
        counts["errors"] += 1
        return counts

    rows = await _fetch_v2_open_positions(pool, cutoff)
    for row in rows:
        result = await _reconcile_v2_row(
            row,
            company_id=company_id,
            actor_id=actor_id,
            exchange=exchange,
            pool=pool,
        )
        counts[result] = counts.get(result, 0) + 1
    logger.info(
        "[%s] surgeon-v2 recon: opens=%d closes=%d errors=%d skipped=%d",
        company_id, counts["opens"], counts["closes"], counts["errors"], counts["skipped"],
    )
    return counts


async def _reconcile_v2_row(
    row: Dict[str, Any],
    *,
    company_id: str,
    actor_id: str,
    exchange: str,
    pool,
) -> str:
    """Process one ``surgeon2_positions`` row through the bridge.

    Returns:
        One of ``"opens"``, ``"closes"``, ``"skipped"``, ``"errors"``.
    """
    symbol = str(row.get("symbol") or "").strip()
    side = str(row.get("side") or "").strip()
    if not symbol or not side:
        return "skipped"

    closed_at = _coerce_ts(row.get("closed_at"))
    entry_price = _safe_float(row.get("entry_price"))
    entry_ts = _coerce_ts(row.get("entry_ts")) or _utc_now()
    notional = _safe_float(row.get("notional"))
    leverage = _safe_float(row.get("leverage"))
    sl = _safe_float(row.get("sl"))
    tp1 = _safe_float(row.get("tp1"))
    tp2 = _safe_float(row.get("tp2"))
    tp3 = _safe_float(row.get("tp3"))
    reason = row.get("reason") or "twilly_v2"

    extra_metadata = {
        "tier": "auto",
        "trade_id": row.get("trade_id"),
        "divergence_at_entry": _safe_float(row.get("divergence_at_entry")),
        "funding_at_entry": _safe_float(row.get("funding_at_entry")),
        "reconciled_at": _utc_now().isoformat(),
        "remaining_frac": _safe_float(row.get("remaining_frac")),
    }

    try:
        await bridge_surgeon_position_open(
            company_id=company_id,
            source=SOURCE_SURGEON_V2,
            actor_id=actor_id,
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            entry_ts=entry_ts,
            notional_usd=notional,
            leverage=leverage,
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            reason=str(reason),
            confidence=0.6,
            extra_metadata=extra_metadata,
            exchange=exchange,
        )
    except Exception as exc:
        logger.warning(
            "[%s] surgeon-v2 OPEN bridge failed trade=%s sym=%s: %s",
            company_id, row.get("trade_id"), symbol, exc,
        )
        return "errors"

    if closed_at is None:
        return "opens"

    log_row = await _fetch_v2_last_close_log(pool, int(row["trade_id"]))
    if log_row is None:
        exit_price = _safe_float(row.get("entry_price"))
        action = "MANUAL"
        net_pnl = None
    else:
        # NOTE: explicit None check — `or entry_price` would silently replace
        # a legitimate 0.0 exit (force-liquidation, post-mortem zero) with the
        # entry price, turning a -100 % outcome into a flat 0 % PnL.
        logged_exit = _safe_float(log_row.get("exit_price"))
        exit_price = logged_exit if logged_exit is not None else entry_price
        action = str(log_row.get("action") or "MANUAL")
        net_pnl = _safe_float(log_row.get("net_pnl"))

    try:
        await bridge_surgeon_position_close(
            company_id=company_id,
            source=SOURCE_SURGEON_V2,
            actor_id=actor_id,
            symbol=symbol,
            side=side,
            exit_price=exit_price,
            exit_ts=closed_at,
            realized_pnl_usd=net_pnl,
            exit_reason=f"{action}/recon",
            exchange=exchange,
        )
    except Exception as exc:
        logger.warning(
            "[%s] surgeon-v2 CLOSE bridge failed trade=%s sym=%s: %s",
            company_id, row.get("trade_id"), symbol, exc,
        )
        return "errors"
    return "closes"


# -----------------------------------------------------------------------------
# Surgeon v1 reconciliation (.surgeon_state.json file-based)
# -----------------------------------------------------------------------------
def _load_surgeon1_state(state_path: Path) -> Optional[Dict[str, Any]]:
    """Load the Surgeon v1 JSON state file. Returns None if missing/invalid."""
    if not state_path.exists():
        logger.debug("surgeon-v1 state file not found at %s", state_path)
        return None
    try:
        return json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("failed to parse surgeon-v1 state %s: %s", state_path, exc)
        return None


def _v1_open_positions(state: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Yield each currently open position from a parsed v1 state dict."""
    for raw in state.get("positions", []) or []:
        if not isinstance(raw, dict):
            continue
        remaining = _safe_float(raw.get("remaining_frac"))
        if remaining is None or remaining <= 0:
            continue
        yield raw


def _v1_recent_closes(
    state: Dict[str, Any],
    cutoff: datetime,
) -> Iterable[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Yield (position, close_event) tuples for v1 closes inside the window.

    The v1 state stores each fill in ``closed_trades`` as a flat record. We
    only forward fills whose ``ts`` is within the lookback window AND that
    fully closed the parent position.  Full-close detection:

    * If the entry carries ``remaining_after`` (v2-style), use it directly.
    * Otherwise (v1-style), a close is considered final when the trade_id
      no longer appears in the current ``positions`` list.
    """
    closed = state.get("closed_trades") or []
    # Build a set of trade_ids that are still open according to current state.
    open_ids = {
        int(p["trade_id"])
        for p in (state.get("positions") or [])
        if isinstance(p, dict) and _safe_float(p.get("remaining_frac", 1)) > 0
    }
    for entry in closed:
        if not isinstance(entry, dict):
            continue
        ts = _coerce_ts(entry.get("ts"))
        if ts is None or ts < cutoff:
            continue
        # v2-style: explicit remaining_after field.
        remaining_after = _safe_float(entry.get("remaining_after"))
        if remaining_after is not None:
            if remaining_after > 1e-6:
                continue
        else:
            # v1-style: treat as fully closed only if trade_id is no longer open.
            tid = entry.get("trade_id")
            if tid is not None and int(tid) in open_ids:
                continue
        yield entry, entry


async def reconcile_surgeon_v1(
    company_id: str,
    *,
    state_path: Path,
    actor_id: str = DEFAULT_SURGEON1_ACTOR,
    since: timedelta = timedelta(days=7),
    exchange: str = DEFAULT_EXCHANGE,
) -> Dict[str, int]:
    """Reconcile surgeon v1 file-state into tracked_positions for one company.

    Args:
        company_id: Tickles company identifier.
        state_path: Path to ``.surgeon_state.json``.
        actor_id: Logical actor name.
        since: Lookback window for closed positions.
        exchange: Default exchange identifier.

    Returns:
        Counts dict with keys ``opens``, ``closes``, ``errors``, ``skipped``.
    """
    counts = {"opens": 0, "closes": 0, "errors": 0, "skipped": 0}
    state = _load_surgeon1_state(state_path)
    if state is None:
        counts["skipped"] += 1
        return counts

    cutoff = _utc_now() - since

    for pos in _v1_open_positions(state):
        try:
            await bridge_surgeon_position_open(
                company_id=company_id,
                source=SOURCE_SURGEON_V1,
                actor_id=actor_id,
                symbol=str(pos.get("symbol") or ""),
                side=str(pos.get("side") or ""),
                entry_price=_safe_float(pos.get("entry_price")),
                entry_ts=_coerce_ts(pos.get("entry_ts")) or _utc_now(),
                notional_usd=_safe_float(pos.get("notional")),
                leverage=_safe_float(pos.get("leverage")),
                sl=_safe_float(pos.get("sl")),
                tp1=_safe_float(pos.get("tp1")),
                tp2=_safe_float(pos.get("tp2")),
                tp3=_safe_float(pos.get("tp3")),
                reason=str(pos.get("reason") or "twilly_v1"),
                confidence=0.6,
                extra_metadata={
                    "tier": pos.get("tier"),
                    "trade_id": pos.get("trade_id"),
                    "divergence_at_entry": _safe_float(pos.get("divergence_at_entry")),
                    "funding_at_entry": _safe_float(pos.get("funding_at_entry")),
                    "reconciled_at": _utc_now().isoformat(),
                },
                exchange=exchange,
            )
            counts["opens"] += 1
        except Exception as exc:
            logger.warning(
                "[%s] surgeon-v1 OPEN bridge failed trade=%s: %s",
                company_id, pos.get("trade_id"), exc,
            )
            counts["errors"] += 1

    for _pos_unused, evt in _v1_recent_closes(state, cutoff):
        # Ensure the open mirror exists before closing — the forward-bridge may
        # have been broken when the position was originally opened.
        try:
            await bridge_surgeon_position_open(
                company_id=company_id,
                source=SOURCE_SURGEON_V1,
                actor_id=actor_id,
                symbol=str(evt.get("symbol") or ""),
                side=str(evt.get("side") or ""),
                entry_price=_safe_float(evt.get("entry_price")),
                entry_ts=_coerce_ts(evt.get("ts")) or _utc_now(),
                notional_usd=None,
                reason=str(evt.get("reason") or "twilly_v1"),
                confidence=0.6,
                extra_metadata={
                    "trade_id": evt.get("trade_id"),
                    "reconciled_at": _utc_now().isoformat(),
                },
                exchange=exchange,
            )
        except Exception as exc:
            logger.warning(
                "[%s] surgeon-v1 OPEN-before-close bridge failed: %s", company_id, exc,
            )
        try:
            await bridge_surgeon_position_close(
                company_id=company_id,
                source=SOURCE_SURGEON_V1,
                actor_id=actor_id,
                symbol=str(evt.get("symbol") or ""),
                side=str(evt.get("side") or ""),
                exit_price=_safe_float(evt.get("exit_price")),
                exit_ts=_coerce_ts(evt.get("ts")) or _utc_now(),
                realized_pnl_usd=_safe_float(evt.get("net_pnl")),
                exit_reason=str(evt.get("reason") or "recon"),
                exchange=exchange,
            )
            counts["closes"] += 1
        except Exception as exc:
            logger.warning(
                "[%s] surgeon-v1 CLOSE bridge failed: %s", company_id, exc,
            )
            counts["errors"] += 1

    logger.info(
        "[%s] surgeon-v1 recon: opens=%d closes=%d errors=%d skipped=%d",
        company_id, counts["opens"], counts["closes"], counts["errors"], counts["skipped"],
    )
    return counts


# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------
async def reconcile_company_once(
    company_id: str,
    *,
    since: timedelta,
    surgeon1_state: Optional[Path],
    exchange: str,
) -> Dict[str, Dict[str, int]]:
    """Run a single reconcile pass for one company across both surgeon variants.

    Args:
        company_id: Tickles company identifier.
        since: Lookback window for closed positions.
        surgeon1_state: Path to the v1 state file, or None to skip v1.
        exchange: Default exchange for symbol normalisation.

    Returns:
        Mapping from variant ("v1"/"v2") to counts dict.
    """
    summary: Dict[str, Dict[str, int]] = {}
    try:
        summary["v2"] = await reconcile_surgeon_v2(
            company_id, since=since, exchange=exchange,
        )
    except Exception as exc:
        logger.exception("[%s] surgeon-v2 reconcile crashed: %s", company_id, exc)
        summary["v2"] = {"opens": 0, "closes": 0, "errors": 1, "skipped": 0}

    if surgeon1_state is not None:
        try:
            summary["v1"] = await reconcile_surgeon_v1(
                company_id,
                state_path=surgeon1_state,
                since=since,
                exchange=exchange,
            )
        except Exception as exc:
            logger.exception("[%s] surgeon-v1 reconcile crashed: %s", company_id, exc)
            summary["v1"] = {"opens": 0, "closes": 0, "errors": 1, "skipped": 0}
    return summary


async def run_forever(
    companies: List[str],
    *,
    since: timedelta,
    interval_sec: int,
    surgeon1_state: Optional[Path],
    exchange: str,
    stop_event: asyncio.Event,
) -> None:
    """Loop reconciling each company every ``interval_sec`` until stopped.

    Args:
        companies: List of company identifiers to reconcile.
        since: Lookback window for closed positions.
        interval_sec: Seconds between reconcile passes.
        surgeon1_state: Path to surgeon v1 state file or None.
        exchange: Default exchange for symbol normalisation.
        stop_event: asyncio Event that, when set, terminates the loop.
    """
    logger.info(
        "surgeon position reconciler starting: companies=%s interval=%ds since=%s",
        companies, interval_sec, since,
    )
    while not stop_event.is_set():
        for company_id in companies:
            try:
                await reconcile_company_once(
                    company_id,
                    since=since,
                    surgeon1_state=surgeon1_state,
                    exchange=exchange,
                )
            except Exception as exc:
                logger.exception("[%s] reconcile pass failed: %s", company_id, exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
        except asyncio.TimeoutError:
            continue
    logger.info("surgeon position reconciler stopped")


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for the reconciler CLI."""
    parser = argparse.ArgumentParser(
        prog="surgeon_position_reconciler",
        description="Reconcile Surgeon v1/v2 position state into tracked_positions.",
    )
    parser.add_argument(
        "--company",
        action="append",
        default=None,
        help="Company id to reconcile. Repeatable. Defaults to $SURGEON_RECON_COMPANIES "
             "or 'rubicon'.",
    )
    parser.add_argument(
        "--since",
        default=DEFAULT_LOOKBACK,
        help=f"Lookback window for closed positions (e.g. '7d'). Default: {DEFAULT_LOOKBACK}.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SEC,
        help=f"Seconds between reconcile passes. Default: {DEFAULT_INTERVAL_SEC}.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single reconcile pass and exit.",
    )
    parser.add_argument(
        "--surgeon1-state-file",
        default=DEFAULT_SURGEON1_STATE,
        help=f"Path to surgeon v1 .surgeon_state.json. Default: {DEFAULT_SURGEON1_STATE}.",
    )
    parser.add_argument(
        "--no-surgeon1",
        action="store_true",
        help="Skip surgeon v1 reconciliation entirely.",
    )
    parser.add_argument(
        "--exchange",
        default=DEFAULT_EXCHANGE,
        help=f"Default exchange for symbol normalisation. Default: {DEFAULT_EXCHANGE}.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        help="Python logging level (default INFO).",
    )
    return parser


def _resolve_companies(arg_value: Optional[List[str]]) -> List[str]:
    """Resolve the company list from CLI arg, env var, or default."""
    if arg_value:
        return [c.strip() for c in arg_value if c and c.strip()]
    env_val = os.environ.get("SURGEON_RECON_COMPANIES", "").strip()
    if env_val:
        return [c.strip() for c in env_val.split(",") if c.strip()]
    return ["rubicon"]


async def _amain() -> int:
    """Async entrypoint shared by both ``--once`` and loop modes."""
    args = _build_arg_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    try:
        since = parse_duration(args.since)
    except ValueError as exc:
        logger.error("invalid --since: %s", exc)
        return 2

    companies = _resolve_companies(args.company)
    surgeon1_state: Optional[Path] = None
    if not args.no_surgeon1:
        candidate = Path(args.surgeon1_state_file).expanduser()
        surgeon1_state = candidate

    if args.once:
        for company_id in companies:
            await reconcile_company_once(
                company_id,
                since=since,
                surgeon1_state=surgeon1_state,
                exchange=args.exchange,
            )
        return 0

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, sig_name), stop_event.set)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main thread: best-effort signal wiring
            logger.debug("signal handler %s not installed", sig_name)

    await run_forever(
        companies,
        since=since,
        interval_sec=max(int(args.interval), 5),
        surgeon1_state=surgeon1_state,
        exchange=args.exchange,
        stop_event=stop_event,
    )
    return 0


def main() -> None:
    """Sync entrypoint for ``python -m shared.intelligence.surgeon_position_reconciler``."""
    try:
        rc = asyncio.run(_amain())
    except KeyboardInterrupt:
        rc = 130
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
