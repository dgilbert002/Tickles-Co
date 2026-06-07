"""
Module: critic_audit_log
Purpose: CSV audit trail for chart_hacker critic opinions — one row per
         position reviewed, with request/response context, timestamps, IDs.
Location: /opt/tickles/shared/intelligence/critic_audit_log.py

Env:
  CRITIC_AUDIT_CSV — path to CSV file (default shared/logs/critic_audit.csv)
  CRITIC_AUDIT_ENABLED — "true"/"1" (default true)

File format every 10 minutes, writes to disk so a crashed daemon doesn't
lose all rows.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CSV_PATH = os.environ.get(
    "CRITIC_AUDIT_CSV",
    str(Path("/opt/tickles/shared/logs/critic_audit.csv")),
)
_ENABLED = os.environ.get("CRITIC_AUDIT_ENABLED", "true").lower() in ("1", "true", "yes")

CSV_HEADER = [
    "ts_utc",
    "position_id",
    "signal_interpretation_id",
    "symbol",
    "direction",
    "entry",
    "sl",
    "tp",
    "current_price",
    "trader_handle",
    "trigger_reason",
    "enrichment_present",
    "model",
    "would_take_trade",
    "memo_confidence",
    "memo_preview",
    "suggested_sl",
    "suggested_tp",
    "cost_usd",
    "tokens_in",
    "tokens_out",
    "latency_ms",
    "status",
    "error",
]

_lock = threading.Lock()
_rows: list[dict] = []  # buffer before flush


def _ensure_header() -> None:
    p = Path(_CSV_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_HEADER)
            w.writeheader()


def _flush() -> None:
    global _rows
    if not _rows:
        return
    rows_snapshot: list[dict] = []
    _rows, rows_snapshot = [], _rows
    p = Path(_CSV_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_HEADER)
            for r in rows_snapshot:
                w.writerow(r)
    except Exception as exc:
        logger.warning("critic_audit: flush failed: %s", exc)


def _schedule_flush_later() -> None:
    """Schedule an async flush ~10 s from now (fire-and-forget)."""
    try:
        import asyncio as _asyncio

        async def _delayed():
            await _asyncio.sleep(10)
            with _lock:
                _flush()
        try:
            loop = _asyncio.get_running_loop()
            loop.create_task(_delayed())
        except RuntimeError:
            pass  # no running event loop — flush will happen on next append
    except Exception:
        pass


def log_critic_opinion(
    *,
    position_id: int,
    signal_interpretation_id: Optional[int] = None,
    symbol: str = "",
    direction: str = "",
    entry: Optional[float] = None,
    sl: Optional[float] = None,
    tp: Optional[float] = None,
    current_price: Optional[float] = None,
    trader_handle: str = "",
    trigger_reason: str = "",
    enrichment_present: str = "",
    model: str = "",
    would_take_trade: Optional[bool] = None,
    memo_confidence: Optional[float] = None,
    memo_preview: str = "",
    suggested_sl: Optional[float] = None,
    suggested_tp: Optional[float] = None,
    cost_usd: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int = 0,
    status: str = "ok",
    error: str = "",
) -> None:
    """Append one row to the critic audit CSV.

    Thread-safe. Writes are buffered and flushed every ~10 s or on process
    exit to avoid per-tick disk I/O while keeping rows safe.
    """
    if not _ENABLED:
        return

    try:
        with _lock:
            _ensure_header()
    except Exception as exc:
        logger.warning("critic_audit: header ensure failed: %s", exc)
        return

    row: Dict[str, Any] = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "position_id": position_id,
        "signal_interpretation_id": signal_interpretation_id or "",
        "symbol": symbol or "",
        "direction": direction or "",
        "entry": entry if entry is not None else "",
        "sl": sl if sl is not None else "",
        "tp": tp if tp is not None else "",
        "current_price": current_price if current_price is not None else "",
        "trader_handle": trader_handle or "",
        "trigger_reason": trigger_reason or "",
        "enrichment_present": enrichment_present or "",
        "model": model or "",
        "would_take_trade": _bool_str(would_take_trade),
        "memo_confidence": round(memo_confidence, 4) if memo_confidence is not None else "",
        "memo_preview": str(memo_preview or "")[:200],
        "suggested_sl": suggested_sl if suggested_sl is not None else "",
        "suggested_tp": suggested_tp if suggested_tp is not None else "",
        "cost_usd": round(cost_usd, 6) if cost_usd else "",
        "tokens_in": tokens_in or "",
        "tokens_out": tokens_out or "",
        "latency_ms": latency_ms or "",
        "status": status or "ok",
        "error": str(error or "")[:500],
    }

    with _lock:
        _rows.append(row)
        if len(_rows) >= 5:
            _flush()
        elif len(_rows) == 1:
            _schedule_flush_later()


def _bool_str(val: Optional[bool]) -> str:
    if val is None:
        return ""
    return "true" if val else "false"


def enrichment_summary(enrichment: Optional[Dict[str, Any]]) -> str:
    """Compact summary of which enrichment sections are present."""
    if not enrichment:
        return ""
    tags: list[str] = []
    qn = enrichment.get("quant_now") or {}
    if isinstance(qn, dict) and qn.get("indicators", {}).get("rsi14") is not None:
        tags.append("quant_now")
    if enrichment.get("at_entry"):
        tags.append("at_entry")
    if enrichment.get("funding"):
        tags.append("funding")
    trsi = enrichment.get("timeframe_rsi") or {}
    if isinstance(trsi, dict) and trsi.get("rsi14") is not None:
        tags.append("timeframe_rsi")
    if enrichment.get("trader_stats_30d"):
        tags.append("trader_stats")
    return ",".join(tags)


def trigger_reason_for(row: Any) -> str:
    """Derive human-readable trigger reason from a position row dict or Record."""
    try:
        last_opinion_at = row.get("last_opinion_at") if hasattr(row, "get") else row.last_opinion_at if hasattr(row, "last_opinion_at") else None
    except Exception:
        last_opinion_at = None
    if last_opinion_at is None:
        return "first_opinion"
    return "position_edit"


def flush_and_close() -> None:
    with _lock:
        _flush()
