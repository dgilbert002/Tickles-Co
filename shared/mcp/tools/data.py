"""MCP tools: Data access (market data, alt-data, catalog).

M1 (2026-04-21): Wired md.quote and md.candles to Postgres candles table.
Added candles.coverage, candles.backfill, and candles.backfill_status tools.
Read-only tools use db_helper (psycopg2); backfill uses async DatabasePool.

2026-05-22: Added ``instruments.refresh`` so an operator (or a chat command)
can re-sync the catalog from the configured exchanges and re-project
into ``public.instruments`` on demand.

Tools registered:
    catalog.list
    catalog.get
    md.quote
    md.candles
    candles.coverage
    candles.backfill
    candles.backfill_status
    altdata.search
    instruments.refresh
    positions.cleanup_retros
    positions.close_wicked
    positions.recheck
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..protocol import McpTool
from ..registry import ToolRegistry
from .context import ToolContext
from .db_helper import query, resolve_instrument_id
from shared.utils.freshness import freshness_envelope, validate_freshness

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backfill job tracking (in-memory; lost on daemon restart — acceptable for M1)
# ---------------------------------------------------------------------------
_BACKFILL_JOBS: Dict[str, Dict[str, Any]] = {}
_BACKFILL_JOB_MAX_AGE_HOURS = 24
_SHARED_POOL: Optional[Any] = None

# Timeframe → minutes mapping for default date-range calculation
_TF_MINUTES: Dict[str, int] = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440, "1w": 10080,
}


def _cleanup_old_backfill_jobs() -> None:
    """Remove completed/failed backfill jobs older than _BACKFILL_JOB_MAX_AGE_HOURS."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=_BACKFILL_JOB_MAX_AGE_HOURS)
    stale_ids = [
        jid for jid, job in _BACKFILL_JOBS.items()
        if job["status"] in ("completed", "failed")
        and job.get("created_at")
        and _parse_ts(job["created_at"]) < cutoff
    ]
    for jid in stale_ids:
        del _BACKFILL_JOBS[jid]
    if stale_ids:
        logger.debug("cleaned up %d stale backfill jobs", len(stale_ids))


async def _get_shared_pool() -> Any:
    """Lazily create and cache the asyncpg DatabasePool for backfill ops."""
    global _SHARED_POOL
    if _SHARED_POOL is not None:
        try:
            if _SHARED_POOL._pool is not None:
                return _SHARED_POOL
        except AttributeError:
            pass
    from shared.utils.db import get_shared_pool
    _SHARED_POOL = await get_shared_pool()
    return _SHARED_POOL


async def _run_backfill_job(
    job_id: str, instrument_id: int, exchange: str, symbol: str,
    timeframe: str, start_ts: datetime, end_ts: datetime,
) -> None:
    """Background coroutine: execute backfill and update job status.

    Args:
        job_id: Unique job identifier for tracking.
        instrument_id: DB primary key of the instrument.
        exchange: Exchange name (e.g. 'bybit').
        symbol: Trading pair (e.g. 'BTC/USDT').
        timeframe: Candle timeframe (e.g. '1m').
        start_ts: Backfill start timestamp (UTC).
        end_ts: Backfill end timestamp (UTC).
    """
    _BACKFILL_JOBS[job_id]["status"] = "running"
    try:
        pool = await _get_shared_pool()
        from shared.candles.backfill import backfill_instrument
        from shared.candles.schema import Timeframe
        report = await backfill_instrument(
            pool, instrument_id, start_ts, end_ts,
            timeframe=Timeframe(timeframe),
        )
        _BACKFILL_JOBS[job_id]["status"] = "completed"
        _BACKFILL_JOBS[job_id]["result"] = report.model_dump(mode="json")
    except Exception as exc:
        logger.exception("[backfill] job %s failed: %s", job_id, exc)
        _BACKFILL_JOBS[job_id]["status"] = "failed"
        _BACKFILL_JOBS[job_id]["error"] = str(exc)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _not_implemented(feature: str) -> Dict[str, Any]:
    """Return a standard not-implemented envelope for stub tools."""
    return {
        "status": "not_implemented",
        "feature": feature,
        "message": (
            f"{feature} is not yet wired to a backend. "
            "Check MCP_AND_MEMORY_PLAN.md for the roadmap."
        ),
    }


def _fmt_ts(val: Any) -> Optional[str]:
    """Format a timestamp value to ISO 8601 string, or return None."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def _parse_ts(val: str) -> datetime:
    """Parse an ISO 8601 timestamp string to a UTC-aware datetime.

    Args:
        val: ISO 8601 timestamp string (e.g. '2026-04-21T00:00:00Z').

    Returns:
        Timezone-aware datetime in UTC.

    Raises:
        ValueError: If the string cannot be parsed.
    """
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp: {val!r}") from exc


def _decimal_to_float(val: Any) -> Optional[float]:
    """Convert Decimal or numeric types to float for JSON serialization."""
    if val is None:
        return None
    return float(val)


def _format_candle(row: Dict[str, Any]) -> Dict[str, Any]:
    """Format a single candle row dict for JSON output.

    Args:
        row: Dict from db_helper.query() with candle columns.

    Returns:
        Dict with JSON-safe values (floats, ISO timestamps).
    """
    return {
        "timestamp": _fmt_ts(row["timestamp"]),
        "open": _decimal_to_float(row["open"]),
        "high": _decimal_to_float(row["high"]),
        "low": _decimal_to_float(row["low"]),
        "close": _decimal_to_float(row["close"]),
        "volume": _decimal_to_float(row["volume"]),
        "source": row.get("source"),
    }


def _format_coverage_row(r: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Format a coverage query row for JSON output.

    Args:
        r: Dict from db_helper.query() with coverage columns.
        now: Current UTC datetime for freshness calculation.

    Returns:
        Dict with JSON-safe values including fresh_lag_minutes.
    """
    last = r["last_ts"]
    if last and isinstance(last, datetime) and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    fresh_lag: Optional[int] = None
    if last is not None:
        fresh_lag = int((now - last).total_seconds() // 60)
    return {
        "symbol": r["symbol"],
        "venue": r["exchange"],
        "source": r["source"],
        "timeframe": r["timeframe"],
        "bars": int(r["bars"]),
        "first_ts": _fmt_ts(r["first_ts"]),
        "last_ts": _fmt_ts(last),
        "fresh_lag_minutes": fresh_lag,
    }


# ---------------------------------------------------------------------------
# Handler implementations (extracted for readability and length control)
# ---------------------------------------------------------------------------

def _handle_md_quote(p: Dict[str, Any]) -> Dict[str, Any]:
    """Read the latest candle row for a symbol/venue from Postgres.

    Args:
        p: Tool parameters with 'symbol' and optional 'venue'.

    Returns:
        Dict with latest OHLCV data or error status.
    """
    symbol = p["symbol"]
    venue = p.get("venue", "bybit")
    try:
        iid = resolve_instrument_id(symbol, venue)
    except RuntimeError as exc:
        return {"status": "error", "message": f"Database error: {exc}"}
    if iid is None:
        return {"status": "error", "message": f"Instrument not found: {venue}/{symbol}"}
    try:
        rows = query(
            'SELECT "timestamp", "open", high, low, "close", volume, source '
            'FROM candles WHERE instrument_id = %s '
            'ORDER BY "timestamp" DESC LIMIT 1',
            (iid,),
        )
    except RuntimeError as exc:
        return {"status": "error", "message": f"Query failed: {exc}"}
    if not rows:
        return {"status": "no_data", "message": f"No candles for {venue}/{symbol}"}
    r = rows[0]
    res = {
        "status": "ok",
        "symbol": symbol,
        "venue": venue,
        "timestamp": _fmt_ts(r["timestamp"]),
        "open": _decimal_to_float(r["open"]),
        "high": _decimal_to_float(r["high"]),
        "low": _decimal_to_float(r["low"]),
        "close": _decimal_to_float(r["close"]),
        "volume": _decimal_to_float(r["volume"]),
        "source": r["source"],
    }
    # Apply Freshness Guard (default 180s)
    return freshness_envelope(res, context=f"md.quote:{venue}/{symbol}")


def _handle_md_candles(p: Dict[str, Any]) -> Dict[str, Any]:
    """Paginated OHLCV candle read from Postgres with optional date range.

    Args:
        p: Tool parameters with 'symbol', optional 'venue', 'timeframe',
           'from', 'to', 'limit'.

    Returns:
        Dict with candle list or error status.
    """
    symbol = p["symbol"]
    venue = p.get("venue", "bybit")
    timeframe = p.get("timeframe", "5m")
    limit = min(p.get("limit", 200), 1000)
    try:
        iid = resolve_instrument_id(symbol, venue)
    except RuntimeError as exc:
        return {"status": "error", "message": f"Database error: {exc}"}
    if iid is None:
        return {"status": "error", "message": f"Instrument not found: {venue}/{symbol}"}

    conditions = ["instrument_id = %s", "timeframe = %s"]
    params: list = [iid, timeframe]

    from_ts = p.get("from")
    to_ts = p.get("to")
    if from_ts:
        conditions.append('"timestamp" >= %s')
        params.append(_parse_ts(from_ts))
    else:
        tf_min = _TF_MINUTES.get(timeframe, 5)
        default_from = datetime.now(timezone.utc) - timedelta(minutes=tf_min * limit)
        conditions.append('"timestamp" >= %s')
        params.append(default_from)
    if to_ts:
        conditions.append('"timestamp" <= %s')
        params.append(_parse_ts(to_ts))

    where = " AND ".join(conditions)
    sql = (
        f'SELECT "timestamp", "open", high, low, "close", volume, source '
        f'FROM candles WHERE {where} '
        f'ORDER BY "timestamp" ASC LIMIT %s'
    )
    params.append(limit)
    try:
        rows = query(sql, tuple(params))
    except RuntimeError as exc:
        return {"status": "error", "message": f"Query failed: {exc}"}
    candles = [_format_candle(r) for r in rows]
    res = {
        "status": "ok",
        "symbol": symbol,
        "venue": venue,
        "timeframe": timeframe,
        "count": len(candles),
        "candles": candles,
    }
    
    # If we are fetching the most recent data (no 'to' timestamp),
    # validate that the latest candle is fresh.
    if not to_ts and candles:
        latest_ts = candles[-1]["timestamp"]
        threshold = float(p.get("freshness_threshold", 180.0))
        
        try:
            validate_freshness(latest_ts, threshold, f"md.candles:{venue}/{symbol}")
            res["freshness"] = {
                "status": "fresh",
                "checked_at": datetime.now(timezone.utc).isoformat()
            }
        except Exception as e:
            return {
                "status": "error",
                "error_code": "STALE_DATA",
                "message": str(e),
                "symbol": symbol,
                "venue": venue
            }
    
    return res


def _handle_candles_coverage(p: Dict[str, Any]) -> Dict[str, Any]:
    """Query candle data coverage summary from Postgres.

    Args:
        p: Tool parameters with optional 'symbol', 'venue', 'timeframe'.

    Returns:
        Dict with per-source coverage summaries or error status.
    """
    symbol = p.get("symbol")
    venue = p.get("venue")
    timeframe = p.get("timeframe")
    conditions: list = ["i.is_active = TRUE"]
    params: list = []

    if symbol and venue:
        try:
            iid = resolve_instrument_id(symbol, venue)
        except RuntimeError as exc:
            return {"status": "error", "message": f"Database error: {exc}"}
        if iid is None:
            return {"status": "error", "message": f"Instrument not found: {venue}/{symbol}"}
        conditions.append("c.instrument_id = %s")
        params.append(iid)
    elif symbol or venue:
        return {
            "status": "error",
            "message": "Both symbol and venue are required when filtering by instrument.",
        }
    if timeframe:
        conditions.append("c.timeframe = %s")
        params.append(timeframe)

    where = "WHERE " + " AND ".join(conditions)
    sql = (
        f'SELECT i.symbol, i.exchange, c.source, c.timeframe, '
        f'COUNT(*) as bars, MIN(c."timestamp") as first_ts, '
        f'MAX(c."timestamp") as last_ts '
        f'FROM candles c JOIN instruments i ON i.id = c.instrument_id '
        f'{where} '
        f'GROUP BY i.symbol, i.exchange, c.source, c.timeframe '
        f'ORDER BY i.exchange, i.symbol, c.timeframe '
        f'LIMIT 200'
    )
    try:
        rows = query(sql, tuple(params) if params else None)
    except RuntimeError as exc:
        return {"status": "error", "message": f"Query failed: {exc}"}
    now = datetime.now(timezone.utc)
    coverage = [_format_coverage_row(r, now) for r in rows]
    return {"status": "ok", "count": len(coverage), "coverage": coverage}


async def _handle_candles_backfill(p: Dict[str, Any]) -> Dict[str, Any]:
    """Enqueue a backfill job and return the job ID for polling.

    Args:
        p: Tool parameters with 'symbol', 'venue', 'from', optional 'to',
           'timeframe'.

    Returns:
        Dict with job_id or error status.
    """
    symbol = p["symbol"]
    venue = p["venue"]
    timeframe = p.get("timeframe", "1m")
    from_ts = p["from"]
    to_ts = p.get("to")
    try:
        iid = resolve_instrument_id(symbol, venue)
    except RuntimeError as exc:
        return {"status": "error", "message": f"Database error: {exc}"}
    if iid is None:
        return {"status": "error", "message": f"Instrument not found: {venue}/{symbol}"}
    try:
        start_dt = _parse_ts(from_ts)
        end_dt = _parse_ts(to_ts) if to_ts else datetime.now(timezone.utc)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    _cleanup_old_backfill_jobs()
    job_id = uuid.uuid4().hex[:8]
    _BACKFILL_JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "symbol": symbol,
        "venue": venue,
        "timeframe": timeframe,
        "from": _fmt_ts(start_dt),
        "to": _fmt_ts(end_dt),
        "created_at": _fmt_ts(datetime.now(timezone.utc)),
        "error": None,
        "result": None,
    }
    asyncio.create_task(
        _run_backfill_job(job_id, iid, venue, symbol, timeframe, start_dt, end_dt)
    )
    return {"status": "ok", "job_id": job_id, "message": "Backfill queued"}


# ---------------------------------------------------------------------------
# Tool definitions and registration
# ---------------------------------------------------------------------------

def _build_tools(ctx: ToolContext) -> list[tuple[McpTool, Any]]:
    """Build all data-access MCP tools bound to the given context."""

    # -- catalog.list -------------------------------------------------------
    t_catalog_list = McpTool(
        name="catalog.list",
        description=(
            "List tradable instruments from the Tickles asset catalog. "
            "Optional filters: venue, asset_class, symbol_contains."
        ),
        version="2",
        input_schema={
            "type": "object",
            "properties": {
                "venue": {"type": "string"},
                "assetClass": {"type": "string"},
                "symbolContains": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            },
        },
        read_only=True,
        tags={"phase": "2", "group": "data"},
    )

    async def _catalog_list(p: Dict[str, Any]) -> Dict[str, Any]:
        qp = {
            "venue": p.get("venue"),
            "assetClass": p.get("assetClass"),
            "symbol": p.get("symbolContains"),
            "limit": p.get("limit", 50),
        }
        try:
            rows = ctx.paperclip("GET", "/api/catalog/instruments", query=qp) or []
            return {"count": len(rows), "instruments": rows}
        except RuntimeError as err:
            if "HTTP 404" in str(err):
                return {
                    "count": 0,
                    "instruments": [],
                    "note": "Paperclip catalog endpoint not yet exposed.",
                }
            raise

    # -- catalog.get --------------------------------------------------------
    t_catalog_get = McpTool(
        name="catalog.get",
        description="Fetch a single asset catalog row by symbol (venue optional).",
        version="2",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "venue": {"type": "string"},
            },
            "required": ["symbol"],
        },
        read_only=True,
        tags={"phase": "2", "group": "data"},
    )

    async def _catalog_get(p: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return ctx.paperclip(
                "GET",
                "/api/catalog/instruments/by-symbol",
                query={"symbol": p["symbol"], "venue": p.get("venue")},
            )
        except RuntimeError as err:
            if "HTTP 404" in str(err):
                return _not_implemented("catalog.get")
            raise

    # -- md.quote -----------------------------------------------------------
    t_md_quote = McpTool(
        name="md.quote",
        description=(
            "Latest traded price from the most recent candle for a "
            "symbol/venue. Reads from the Postgres candles table."
        ),
        version="2",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Trading pair, e.g. BTC/USDT"},
                "venue": {"type": "string", "description": "Exchange name, e.g. bybit"},
            },
            "required": ["symbol"],
        },
        read_only=True,
        tags={"phase": "2", "group": "data"},
    )

    async def _md_quote(p: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(_handle_md_quote, p)

    # -- md.candles ---------------------------------------------------------
    t_md_candles = McpTool(
        name="md.candles",
        description=(
            "OHLCV candles for a symbol/venue/timeframe with optional date "
            "range. Reads from the Postgres candles table. Defaults to the "
            "last N bars if 'from' is omitted."
        ),
        version="2",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Trading pair, e.g. BTC/USDT"},
                "venue": {"type": "string", "description": "Exchange name, e.g. bybit"},
                "timeframe": {
                    "type": "string",
                    "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"],
                    "default": "5m",
                },
                "from": {
                    "type": "string",
                    "description": "Start timestamp (ISO 8601). Defaults to a window based on timeframe × limit.",
                },
                "to": {
                    "type": "string",
                    "description": "End timestamp (ISO 8601). Defaults to now.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            },
            "required": ["symbol"],
        },
        read_only=True,
        tags={"phase": "2", "group": "data"},
    )

    async def _md_candles(p: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(_handle_md_candles, p)

    # -- candles.coverage ---------------------------------------------------
    t_candles_coverage = McpTool(
        name="candles.coverage",
        description=(
            "Show candle data coverage: bar count, first/last timestamp, "
            "and freshness for each (symbol, venue, timeframe, source). "
            "Omit symbol/venue to list all instruments."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Trading pair (optional)"},
                "venue": {"type": "string", "description": "Exchange name (optional)"},
                "timeframe": {
                    "type": "string",
                    "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"],
                },
            },
        },
        read_only=True,
        tags={"phase": "2", "group": "data"},
    )

    async def _candles_coverage(p: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(_handle_candles_coverage, p)

    # -- candles.backfill ---------------------------------------------------
    t_candles_backfill = McpTool(
        name="candles.backfill",
        description=(
            "Enqueue a historical candle backfill for a symbol/venue. "
            "Returns a job_id; poll candles.backfill_status for progress."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Trading pair, e.g. BTC/USDT"},
                "venue": {"type": "string", "description": "Exchange name, e.g. bybit"},
                "timeframe": {
                    "type": "string",
                    "enum": ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"],
                    "default": "1m",
                },
                "from": {
                    "type": "string",
                    "description": "Start timestamp (ISO 8601). Required.",
                },
                "to": {
                    "type": "string",
                    "description": "End timestamp (ISO 8601). Defaults to now.",
                },
            },
            "required": ["symbol", "venue", "from"],
        },
        read_only=False,
        tags={"phase": "2", "group": "data"},
    )

    async def _candles_backfill(p: Dict[str, Any]) -> Dict[str, Any]:
        return await _handle_candles_backfill(p)

    # -- candles.backfill_status --------------------------------------------
    t_candles_backfill_status = McpTool(
        name="candles.backfill_status",
        description="Check the status of a previously submitted backfill job.",
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "backfill_id": {
                    "type": "string",
                    "description": "Job ID returned by candles.backfill",
                },
            },
            "required": ["backfill_id"],
        },
        read_only=True,
        tags={"phase": "2", "group": "data"},
    )

    async def _candles_backfill_status(p: Dict[str, Any]) -> Dict[str, Any]:
        job_id = p["backfill_id"]
        job = _BACKFILL_JOBS.get(job_id)
        if job is None:
            return {"status": "error", "message": f"Backfill job not found: {job_id}"}
        return {"status": "ok", "job": job}

    # -- instruments.refresh ------------------------------------------------
    # 2026-05-22 — Operator/agent tool that syncs market metadata from every
    # configured exchange and re-projects it into public.instruments so the
    # dashboard snapshot can resolve newly-listed symbols. Idempotent.
    t_instruments_refresh = McpTool(
        name="instruments.refresh",
        description=(
            "Refresh the instrument catalog. Runs the unified_instruments "
            "sync (Bybit, BloFin, Bitget, Capital.com) and then projects the "
            "result into public.instruments so the dashboard / candle daemon "
            "see new symbols. Idempotent — safe to call repeatedly. Use "
            "skipSync=true to project only (no exchange calls)."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "skipSync": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "If true, skip the network sync and only re-project "
                        "the existing unified_instruments rows into instruments."
                    ),
                },
                "dryRun": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, compute the plan but write nothing.",
                },
            },
        },
        read_only=False,
        tags={"phase": "5", "group": "data", "status": "live"},
    )

    async def _instruments_refresh(p: Dict[str, Any]) -> Dict[str, Any]:
        """Run the catalog refresh + projection.

        Step 1 (optional): ``shared.market_data.instrument_sync.sync_instruments``
                           — populates public.unified_instruments via CCXT.
        Step 2:           ``shared.jobs.project_unified_to_instruments.project_unified_to_instruments``
                           — projects into public.instruments using the
                           preference chain and 90-day feed activation.
        """
        skip_sync = bool(p.get("skipSync", False))
        dry_run = bool(p.get("dryRun", False))
        result: Dict[str, Any] = {"status": "ok"}

        try:
            if not skip_sync:
                from shared.market_data.instrument_sync import sync_instruments
                # sync_instruments writes to unified_instruments; it is
                # safe to run alongside other writers (uses UPSERT).
                sync_counts = await sync_instruments()
                result["unified_sync"] = sync_counts
            else:
                result["unified_sync"] = "skipped"

            from shared.jobs.project_unified_to_instruments import (
                project_unified_to_instruments,
            )
            proj_counts = await project_unified_to_instruments(dry_run=dry_run)
            result["projection"] = proj_counts
            result["dry_run"] = dry_run
        except Exception as exc:
            logger.exception("instruments.refresh failed")
            return {"status": "error", "message": str(exc)}

        return result

    # -- positions.cleanup_retros -------------------------------------------
    # 2026-05-22 — Detects and expires tracked_positions that were
    # retro-activated by the old position_monitor bug (single-close
    # comparison instead of candle [low,high] range check). Idempotent.
    t_positions_cleanup_retros = McpTool(
        name="positions.cleanup_retros",
        description=(
            "Sweep stale rows out of the live trading set. Two passes: "
            "(1) expire tracked_positions currently status='open' whose "
            "entry was never actually touched by a 1m candle between "
            "created_at and updated_at (and delete any still-open "
            "competition_trades pointing at them); (2) delete still-open "
            "competition_trades whose tracked_position_id is NULL (orphan "
            "shadow trades from an earlier copy_trade_monitor version). "
            "Both passes are idempotent. Use dryRun=true for a no-write "
            "preview, includeOrphans=false to skip pass (2)."
        ),
        version="2",
        input_schema={
            "type": "object",
            "properties": {
                "lookbackHours": {
                    "type": "integer", "minimum": 1, "maximum": 720, "default": 48,
                    "description": "Only inspect positions created in the last N hours.",
                },
                "dryRun": {
                    "type": "boolean", "default": False,
                    "description": "If true, report what would be cleaned without writing.",
                },
                "includeOrphans": {
                    "type": "boolean", "default": True,
                    "description": (
                        "If true (default), also delete any still-open "
                        "competition_trades whose tracked_position_id is "
                        "NULL. Set false to run only the retro-activation "
                        "sweep."
                    ),
                },
            },
        },
        read_only=False,
        tags={"phase": "5", "group": "data", "status": "live"},
    )

    async def _positions_cleanup_retros(p: Dict[str, Any]) -> Dict[str, Any]:
        from shared.intelligence.position_housekeeping import cleanup_retro_activations
        try:
            return {
                "status": "ok",
                **await cleanup_retro_activations(
                    lookback_hours=int(p.get("lookbackHours", 48)),
                    dry_run=bool(p.get("dryRun", False)),
                    include_orphan_competition_trades=bool(p.get("includeOrphans", True)),
                ),
            }
        except Exception as exc:
            logger.exception("positions.cleanup_retros failed")
            return {"status": "error", "message": str(exc)}

    # -- positions.close_wicked ---------------------------------------------
    # 2026-05-22 — Catches positions whose SL or TP was already wicked
    # through by a 1m candle. The live position_monitor catches new wicks
    # in real time after the 2026-05-22 fix; this tool exists for backfill
    # of historical leftovers AND as an on-demand safety-net for agents.
    t_positions_close_wicked = McpTool(
        name="positions.close_wicked",
        description=(
            "Close any currently-open tracked_positions whose SL or TP "
            "was wicked through by a 1m candle since activation. Uses the "
            "same predicate the live monitor uses; closes route through "
            "the same _settle_close path so backfilled closes are "
            "indistinguishable from live closes. SL is preferred over TP "
            "on same-candle ambiguity (conservative trader convention). "
            "Idempotent — safe to call repeatedly. Use dryRun=true for a "
            "no-write preview."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "dryRun": {
                    "type": "boolean", "default": False,
                    "description": "If true, report what would be closed without writing.",
                },
            },
        },
        read_only=False,
        tags={"phase": "5", "group": "data", "status": "live"},
    )

    async def _positions_close_wicked(p: Dict[str, Any]) -> Dict[str, Any]:
        from shared.intelligence.position_housekeeping import close_wicked_positions
        try:
            return {
                "status": "ok",
                **await close_wicked_positions(dry_run=bool(p.get("dryRun", False))),
            }
        except Exception as exc:
            logger.exception("positions.close_wicked failed")
            return {"status": "error", "message": str(exc)}

    # -- positions.recheck --------------------------------------------------
    # 2026-05-22 — Trigger one immediate pending-activation cycle so an
    # agent doesn't need to wait for the 60s daemon poll. Honours the same
    # recency bound the daemon uses; safe to call repeatedly.
    t_positions_recheck = McpTool(
        name="positions.recheck",
        description=(
            "Run one pending-activation cycle now (out-of-band). Honours "
            "the same recency bound the position_monitor daemon enforces, "
            "so repeated calls cannot retro-activate stale touches. "
            "Returns the per-cycle counts (activated / expired / no_price / "
            "error). Safe and idempotent."
        ),
        version="1",
        input_schema={"type": "object", "properties": {}},
        read_only=False,
        tags={"phase": "5", "group": "data", "status": "live"},
    )

    async def _positions_recheck(_p: Dict[str, Any]) -> Dict[str, Any]:
        from shared.intelligence.position_housekeeping import recheck_pending_activations
        try:
            return await recheck_pending_activations()
        except Exception as exc:
            logger.exception("positions.recheck failed")
            return {"status": "error", "message": str(exc)}

    # -- altdata.search (stub) ----------------------------------------------
    t_alt_search = McpTool(
        name="altdata.search",
        description=(
            "Search across alt-data sources (Discord signals, whales, "
            "news, social) — stub; will be wired to shared.altdata."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["discord", "whales", "news", "social"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
            },
            "required": ["query"],
        },
        read_only=True,
        tags={"phase": "2", "group": "data", "status": "stub"},
    )

    async def _alt_search(p: Dict[str, Any]) -> Dict[str, Any]:
        out = _not_implemented("altdata.search")
        out["echo"] = {"query": p.get("query"), "kind": p.get("kind")}
        return out

    return [
        (t_catalog_list, _catalog_list),
        (t_catalog_get, _catalog_get),
        (t_md_quote, _md_quote),
        (t_md_candles, _md_candles),
        (t_candles_coverage, _candles_coverage),
        (t_candles_backfill, _candles_backfill),
        (t_candles_backfill_status, _candles_backfill_status),
        (t_instruments_refresh, _instruments_refresh),
        (t_positions_cleanup_retros, _positions_cleanup_retros),
        (t_positions_close_wicked, _positions_close_wicked),
        (t_positions_recheck, _positions_recheck),
        (t_alt_search, _alt_search),
    ]


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    """Register all data tools on the given registry."""
    for tool, handler in _build_tools(ctx):
        registry.register(tool, handler)
