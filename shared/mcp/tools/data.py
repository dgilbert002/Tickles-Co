"""MCP tools: Data access (market data, alt-data, catalog).

M1 (2026-04-21): Wired md.quote and md.candles to Postgres candles table.
Added candles.coverage, candles.backfill, and candles.backfill_status tools.
Read-only tools use db_helper (psycopg2); backfill uses async DatabasePool.

Tools registered:
    catalog.list
    catalog.get
    md.quote
    md.candles
    candles.coverage
    candles.backfill
    candles.backfill_status
    altdata.search
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
    return {
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
    return {
        "status": "ok",
        "symbol": symbol,
        "venue": venue,
        "timeframe": timeframe,
        "count": len(candles),
        "candles": candles,
    }


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
        (t_alt_search, _alt_search),
    ]


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    """Register all data tools on the given registry."""
    for tool, handler in _build_tools(ctx):
        registry.register(tool, handler)
