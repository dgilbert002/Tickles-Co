"""
demo_forensic_log.py — append-only forensic transaction log for the
paper → demo → live mirroring pipeline.

WHY THIS EXISTS (plain English):
The owner wants a "forensic" audit trail of EVERYTHING that happens between a
paper trade and the demo (and later live) order that mirrors it: when a limit
order was placed, on which exchange/account, for which agent, at what price,
whether it was accepted/rejected (and WHY), when it filled, the fill price,
the slippage vs. the paper entry, fees, and finally how it closed. The
demo_orders TABLE stores the latest *state* of each order, but a state table
can't tell you the *story* (order placed → rejected → re-placed → filled →
closed). This module writes that story as one JSON object per line to a real
.log file so it can be tailed in the dashboard AND grepped from a shell.

DESIGN:
  - One JSON object per line (JSON-lines / .jsonl style) so it's both
    human-readable and machine-parseable.
  - Rotating file handler (5 MB x 5 files) so it never fills the disk.
  - `flog(event, **fields)` — call this anywhere in the mirror pipeline.
  - `read_tail(n)` — used by the dashboard /api/paper-demo/log endpoint.

This is a pure utility — it never raises into the caller (logging must never
break trading). All failures are swallowed and best-effort.

Usage:
    from shared.daemons.demo_forensic_log import flog, read_tail
    flog("mirror_placed", tp_id=123, exchange="bybit", account="TicklesCo",
         agent="copy_charthacker", symbol="BTC/USDT:USDT", direction="long",
         paper_entry=72000, qty=0.01, leverage=20, order_id="abc")
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Log file location
# ---------------------------------------------------------------------------
LOG_DIR = os.environ.get("TICKLES_LOG_DIR", "/opt/tickles/shared/logs")
FORENSIC_LOG_PATH = os.path.join(LOG_DIR, "paper_demo.log")

_logger: Optional[logging.Logger] = None


def _get_logger() -> logging.Logger:
    """Lazily build a dedicated rotating-file logger (process-local)."""
    global _logger
    if _logger is not None:
        return _logger

    lg = logging.getLogger("tickles.demo.forensic")
    lg.setLevel(logging.INFO)
    lg.propagate = False  # don't spam the root/journal logger with JSON

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = RotatingFileHandler(
            FORENSIC_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5,
            encoding="utf-8",
        )
        # We pre-format the message as JSON, so the handler just prints it raw.
        handler.setFormatter(logging.Formatter("%(message)s"))
        # Avoid attaching duplicate handlers if called repeatedly.
        if not lg.handlers:
            lg.addHandler(handler)
    except Exception:
        # If the file can't be opened (perms, disk), fall back to no handler —
        # flog() will simply no-op rather than crash the daemon.
        pass

    _logger = lg
    return lg


def flog(event: str, **fields: Any) -> None:
    """Append one forensic event line. Never raises.

    Args:
        event: short machine event name, e.g. "mirror_placed",
               "mirror_rejected", "fill", "cancel", "close".
        **fields: arbitrary JSON-serialisable context (ids, prices, etc).
    """
    try:
        record: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
        }
        for k, v in fields.items():
            # Decimals / datetimes → str so json.dumps never blows up.
            try:
                json.dumps(v)
                record[k] = v
            except (TypeError, ValueError):
                record[k] = str(v)
        _get_logger().info(json.dumps(record, separators=(",", ":")))
    except Exception:
        pass  # logging must never break the trading pipeline


def read_tail(n: int = 200, event_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return the last `n` forensic events (newest first), best-effort.

    Args:
        n: how many lines to return (capped at 2000).
        event_filter: if set, only return events whose "event" matches.
    """
    n = max(1, min(int(n or 200), 2000))
    path = FORENSIC_LOG_PATH
    if not os.path.exists(path):
        return []
    try:
        # Read whole file then slice; the file is size-capped (5 MB) so this
        # is bounded. For very hot logs we read only the tail bytes.
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            # read at most the last ~2 MB (plenty for a few thousand lines)
            read_bytes = min(size, 2 * 1024 * 1024)
            f.seek(size - read_bytes)
            chunk = f.read().decode("utf-8", errors="replace")
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        out: List[Dict[str, Any]] = []
        for ln in reversed(lines):
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if event_filter and rec.get("event") != event_filter:
                continue
            out.append(rec)
            if len(out) >= n:
                break
        return out
    except Exception:
        return []


__all__ = ["flog", "read_tail", "FORENSIC_LOG_PATH"]
