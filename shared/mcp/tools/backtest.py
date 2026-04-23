"""
Module: backtest
Purpose: MCP tools for backtest discovery (strategies, indicators, engines, compose, sweep, top_k)
Location: /opt/tickles/shared/mcp/tools/backtest.py

Phase M5 wires the backtest discovery surface over MCP. Agents can browse
strategies, indicators, and engines, preview indicator values on live data,
compose backtest specs, plan parameter sweeps, and query top-K results from
ClickHouse.

Tools registered:
    strategy.list         — List available strategies with descriptions
    strategy.get          — Full strategy card with param schema
    indicator.list        — List indicators, optionally filtered by category
    indicator.get         — Full indicator spec with params and ranges
    indicator.compute_preview — Run an indicator on recent candles and return values
    engine.list           — List backtest engines with capabilities
    backtest.compose      — Build a validated BacktestSpec (no submission)
    backtest.plan_sweep   — Expand a parameter sweep into N specs
    backtest.top_k        — Read top-K backtest results from ClickHouse
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..protocol import McpTool
from ..registry import ToolRegistry
from .context import ToolContext
from . import db_helper

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_VENUE: str = "bybit"
_DEFAULT_TIMEFRAME: str = "1h"
_DEFAULT_STARTING_CASH: float = 10_000.0
_DEFAULT_WINDOW: int = 200
_MAX_WINDOW: int = 1000
_MAX_PREVIEW_POINTS: int = 50
_MAX_SWEEP_SPECS: int = 500
_TOP_K_MAX: int = 500
_TOP_K_DEFAULT: int = 20
_LOOKBACK_DAYS: int = 90


def _default_date_range() -> tuple[str, str]:
    """Return (from_date, to_date) as ISO date strings.

    Defaults to 90 days ago through today so agents always get a
    relevant window without specifying dates explicitly.
    """
    today = datetime.now(tz=timezone.utc).date()
    start = today - timedelta(days=_LOOKBACK_DAYS)
    return start.isoformat(), today.isoformat()


# ---------------------------------------------------------------------------
# Strategy introspection helpers
# ---------------------------------------------------------------------------


def _strategy_card(name: str, fn: Any) -> Dict[str, Any]:
    """Build a metadata card for a strategy callable.

    Args:
        name: Strategy name (e.g. 'sma_cross').
        fn: The strategy callable (df, params) -> Series.

    Returns:
        Dict with name, description, and param hints extracted from docstring.
    """
    doc = (fn.__doc__ or "").strip()
    return {
        "name": name,
        "description": doc.split("\n")[0] if doc else "",
    }


def _list_strategies() -> List[Dict[str, Any]]:
    """List all registered strategies with metadata cards.

    Returns:
        List of strategy card dicts.
    """
    from shared.backtest.strategies import STRATEGIES

    return [_strategy_card(name, fn) for name, fn in sorted(STRATEGIES.items())]


def _get_strategy(name: str) -> Dict[str, Any]:
    """Get full details for a single strategy.

    Args:
        name: Strategy name.

    Returns:
        Dict with name, description, and param hints.

    Raises:
        KeyError: If strategy name is unknown.
    """
    from shared.backtest.strategies import get as get_strategy

    fn = get_strategy(name)
    card = _strategy_card(name, fn)

    # Extract param hints from the function source if possible
    import inspect

    try:
        src = inspect.getsource(fn)
        card["sourceHint"] = src[:500]
    except (OSError, TypeError):
        pass

    return card


# ---------------------------------------------------------------------------
# Indicator introspection helpers
# ---------------------------------------------------------------------------


def _indicator_card(spec: Any) -> Dict[str, Any]:
    """Build a metadata card for an IndicatorSpec.

    Args:
        spec: An IndicatorSpec instance.

    Returns:
        Dict with name, category, direction, description, defaults, param_ranges.
    """
    return {
        "name": spec.name,
        "category": spec.category,
        "direction": spec.direction,
        "description": spec.description,
        "defaults": spec.defaults,
        "paramRanges": spec.param_ranges,
        "assetClass": spec.asset_class,
    }


def _list_indicators(category: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all registered indicators, optionally filtered by category.

    Args:
        category: Optional category filter (trend, momentum, volatility, volume, etc.).

    Returns:
        List of indicator card dicts.
    """
    from shared.backtest.indicators import INDICATORS

    cards = []
    for name in sorted(INDICATORS.keys()):
        spec = INDICATORS[name]
        if category and spec.category != category:
            continue
        cards.append(_indicator_card(spec))
    return cards


def _get_indicator(name: str) -> Dict[str, Any]:
    """Get full details for a single indicator.

    Args:
        name: Indicator name.

    Returns:
        Dict with full indicator spec.

    Raises:
        KeyError: If indicator name is unknown.
    """
    from shared.backtest.indicators import get as get_indicator

    spec = get_indicator(name)
    return _indicator_card(spec)


# ---------------------------------------------------------------------------
# Indicator compute preview
# ---------------------------------------------------------------------------


def _load_candles_df(
    symbol: str, venue: str, timeframe: str, window: int,
) -> Any:
    """Load recent candles from Postgres into a pandas DataFrame.

    Uses the same column naming convention as the backtest engine
    (closePrice, highPrice, lowPrice, etc.) so indicators work directly.

    Args:
        symbol: Trading pair (e.g. 'BTC/USDT').
        venue: Exchange name (e.g. 'bybit').
        timeframe: Candle timeframe (e.g. '1h').
        window: Number of candles to load.

    Returns:
        pandas DataFrame with engine-compatible columns, or None if no data.
    """
    import pandas as pd

    iid = db_helper.resolve_instrument_id(symbol, venue)
    if iid is None:
        return None

    rows = db_helper.query(
        "SELECT open, high, low, close, volume, timestamp "
        "FROM candles WHERE instrument_id = %s AND timeframe = %s "
        "ORDER BY timestamp DESC LIMIT %s",
        (iid, timeframe, window),
    )
    if not rows:
        return None

    # Reverse to chronological order and build DataFrame
    rows.reverse()
    df = pd.DataFrame(rows)
    df.rename(columns={
        "open": "openPrice",
        "high": "highPrice",
        "low": "lowPrice",
        "close": "closePrice",
        "volume": "lastTradedVolume",
        "timestamp": "snapshotTime",
    }, inplace=True)
    df["date"] = pd.to_datetime(df["snapshotTime"], utc=True).dt.date
    # Add openBid/closeAsk approximations (engine expects them)
    df["openBid"] = df["openPrice"] * 0.9999
    df["closeAsk"] = df["closePrice"] * 1.0001
    return df


def _compute_preview(
    name: str,
    params: Dict[str, Any],
    symbol: str,
    venue: str,
    timeframe: str,
    window: int,
) -> Dict[str, Any]:
    """Run an indicator on recent candles and return the last N values.

    Args:
        name: Indicator name (e.g. 'rsi').
        params: Indicator parameters (e.g. {'period': 14}).
        symbol: Trading pair.
        venue: Exchange name.
        timeframe: Candle timeframe.
        window: Number of candles to load.

    Returns:
        Dict with indicator name, params, and preview values.
    """
    from shared.backtest.indicators import get as get_indicator

    spec = get_indicator(name)
    merged_params = {**spec.defaults, **params}

    df = _load_candles_df(symbol, venue, timeframe, window)
    if df is None:
        return {
            "status": "error",
            "message": f"no candle data for {symbol}@{venue} ({timeframe})",
        }

    series = spec.fn(df, merged_params)

    # Take the last _MAX_PREVIEW_POINTS non-NaN values
    valid = series.dropna().tail(_MAX_PREVIEW_POINTS)
    values = []
    for idx, val in valid.items():
        ts = None
        if hasattr(idx, "isoformat"):
            ts = idx.isoformat()
        elif "snapshotTime" in df.columns:
            try:
                ts = str(df.loc[idx, "snapshotTime"]) if idx in df.index else None
            except (KeyError, TypeError):
                pass
        values.append({"value": round(float(val), 6), "timestamp": ts})

    return {
        "status": "ok",
        "indicator": name,
        "params": merged_params,
        "symbol": symbol,
        "venue": venue,
        "timeframe": timeframe,
        "window": window,
        "dataPoints": len(values),
        "values": values,
    }


# ---------------------------------------------------------------------------
# Engine introspection
# ---------------------------------------------------------------------------


def _list_engines() -> List[Dict[str, Any]]:
    """List all registered backtest engines with capabilities.

    Returns:
        List of engine card dicts.
    """
    from shared.backtest.engines import capabilities, get as get_engine, list_engines

    caps = capabilities()
    result = []
    for name in list_engines():
        cap = caps[name]
        eng = get_engine(name)
        result.append({
            "name": name,
            "available": eng.available(),
            "capabilities": {
                "supportsIntrabarSlTp": cap.supports_intrabar_sl_tp,
                "supportsFunding": cap.supports_funding,
                "supportsFees": cap.supports_fees,
                "supportsSlippage": cap.supports_slippage,
                "supportsVectorisedSweep": cap.supports_vectorised_sweep,
                "supportsWalkForward": cap.supports_walk_forward,
                "notes": cap.notes,
            },
        })
    return result


# ---------------------------------------------------------------------------
# Backtest compose + sweep
# ---------------------------------------------------------------------------


def _compose_spec(p: Dict[str, Any]) -> Dict[str, Any]:
    """Build a validated BacktestConfig spec without submitting it.

    Validates that the strategy and indicator exist and that params
    are consistent. Returns a spec dict with a param_hash for dedup.

    Args:
        p: MCP tool params with symbol, strategy, indicatorParams, etc.

    Returns:
        Dict with the composed spec or an error.
    """
    try:
        from shared.backtest.engine import BacktestConfig
        from shared.backtest.strategies import get as get_strategy

        symbol = str(p["symbol"])
        strategy_name = str(p["strategy"])
        venue = str(p.get("venue", _DEFAULT_VENUE))
        timeframe = str(p.get("timeframe", _DEFAULT_TIMEFRAME))
        default_from, default_to = _default_date_range()
        start_date = str(p.get("from", default_from))
        end_date = str(p.get("to", default_to))
        direction = str(p.get("direction", "long"))
        initial_capital = float(p.get("startingCashUsd", _DEFAULT_STARTING_CASH))
        if math.isnan(initial_capital):
            return {"status": "error", "message": "startingCashUsd must be a valid number, got NaN"}
        leverage = float(p.get("leverage", 1.0))
        fee_taker_bps = float(p.get("feeTakerBps", 5.0))
        slippage_bps = float(p.get("slippageBps", 2.0))
        stop_loss_pct = float(p.get("stopLossPct", 0.0))
        take_profit_pct = float(p.get("takeProfitPct", 0.0))
        indicator_params = p.get("indicatorParams", {})
        engine = str(p.get("engine", "classic"))

        # Validate strategy exists
        try:
            get_strategy(strategy_name)
        except KeyError as exc:
            return {"status": "error", "message": str(exc)}

        # Validate engine exists
        try:
            from shared.backtest.engines import get as get_engine

            eng = get_engine(engine)
            if not eng.available():
                return {
                    "status": "error",
                    "message": f"engine '{engine}' is not available in this environment",
                }
        except KeyError as exc:
            return {"status": "error", "message": str(exc)}

        # Validate direction
        if direction not in ("long", "short", "both"):
            return {
                "status": "error",
                "message": f"direction must be 'long', 'short', or 'both', got '{direction}'",
            }

        # Validate initial_capital
        if initial_capital <= 0:
            return {"status": "error", "message": "startingCashUsd must be positive"}

        # Build config to get param_hash
        cfg = BacktestConfig(
            symbol=symbol,
            source=venue,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
            direction=direction,
            initial_capital=initial_capital,
            leverage=leverage,
            fee_taker_bps=fee_taker_bps,
            slippage_bps=slippage_bps,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            strategy_name=strategy_name,
            indicator_params=indicator_params,
        )

        return {
            "status": "ok",
            "spec": {
                "symbol": symbol,
                "venue": venue,
                "timeframe": timeframe,
                "from": start_date,
                "to": end_date,
                "direction": direction,
                "startingCashUsd": initial_capital,
                "leverage": leverage,
                "feeTakerBps": fee_taker_bps,
                "slippageBps": slippage_bps,
                "stopLossPct": stop_loss_pct,
                "takeProfitPct": take_profit_pct,
                "strategy": strategy_name,
                "indicatorParams": indicator_params,
                "engine": engine,
                "paramHash": cfg.param_hash(),
            },
            "message": "Spec composed successfully. Use backtest.submit to run it.",
        }
    except Exception as exc:
        logger.exception("backtest.compose failed")
        return {"status": "error", "message": f"spec composition failed: {exc}"}


def _plan_sweep(p: Dict[str, Any]) -> Dict[str, Any]:
    """Expand a parameter sweep from a base spec + param ranges.

    Takes a base spec (from backtest.compose) and a dict of param ranges,
    then generates the cartesian product of all parameter combinations.

    Args:
        p: MCP tool params with baseSpec and paramRanges.

    Returns:
        Dict with count and list of expanded specs.
    """
    try:
        base_spec = p.get("baseSpec", {})
        param_ranges = p.get("paramRanges", {})

        if not base_spec:
            return {"status": "error", "message": "baseSpec is required"}
        if not param_ranges:
            return {"status": "error", "message": "paramRanges is required"}

        # Validate param_ranges is a dict of lists
        range_keys = []
        range_values = []
        for key, vals in param_ranges.items():
            if not isinstance(vals, list) or len(vals) == 0:
                return {
                    "status": "error",
                    "message": f"paramRanges['{key}'] must be a non-empty list",
                }
            range_keys.append(key)
            range_values.append(vals)

        # Compute cartesian product
        combos = list(itertools.product(*range_values))
        if len(combos) > _MAX_SWEEP_SPECS:
            return {
                "status": "error",
                "message": (
                    f"sweep expands to {len(combos)} specs, "
                    f"max is {_MAX_SWEEP_SPECS}. Reduce paramRanges."
                ),
            }

        from shared.backtest.engine import BacktestConfig

        specs = []
        for combo in combos:
            variant = dict(base_spec)
            variant_indicators = dict(variant.get("indicatorParams", {}))
            for key, val in zip(range_keys, combo):
                # Check if this is a top-level spec param or indicator param
                if key in ("direction", "leverage", "feeTakerBps", "slippageBps",
                           "stopLossPct", "takeProfitPct", "startingCashUsd"):
                    variant[key] = val
                else:
                    variant_indicators[key] = val
            variant["indicatorParams"] = variant_indicators

            # Recompute param_hash for this variant
            try:
                cfg = BacktestConfig(
                    symbol=variant.get("symbol", "BTC/USDT"),
                    source=variant.get("venue", _DEFAULT_VENUE),
                    timeframe=variant.get("timeframe", _DEFAULT_TIMEFRAME),
                    start_date=variant.get("from", "2026-01-01"),
                    end_date=variant.get("to", "2026-04-21"),
                    direction=variant.get("direction", "long"),
                    initial_capital=float(variant.get("startingCashUsd", _DEFAULT_STARTING_CASH)),
                    leverage=float(variant.get("leverage", 1.0)),
                    fee_taker_bps=float(variant.get("feeTakerBps", 5.0)),
                    slippage_bps=float(variant.get("slippageBps", 2.0)),
                    stop_loss_pct=float(variant.get("stopLossPct", 0.0)),
                    take_profit_pct=float(variant.get("takeProfitPct", 0.0)),
                    strategy_name=variant.get("strategy", ""),
                    indicator_params=variant.get("indicatorParams", {}),
                )
                variant["paramHash"] = cfg.param_hash()
            except Exception as exc:
                logger.warning("plan_sweep: param_hash failed for variant %s: %s", combo, exc)

            specs.append(variant)

        return {
            "status": "ok",
            "totalSpecs": len(specs),
            "sweptParams": range_keys,
            "specs": specs,
        }
    except Exception as exc:
        logger.exception("backtest.plan_sweep failed")
        return {"status": "error", "message": f"sweep planning failed: {exc}"}


# ---------------------------------------------------------------------------
# Top-K from ClickHouse
# ---------------------------------------------------------------------------


def _top_k(p: Dict[str, Any]) -> Dict[str, Any]:
    """Read top-K backtest results from ClickHouse.

    Falls back to an empty list if ClickHouse is unavailable.

    Args:
        p: MCP tool params with limit, sortBy, symbol, strategy, minTrades.

    Returns:
        Dict with results list or error.
    """
    try:
        from shared.backtest.accessible import top

        limit = int(p.get("limit", _TOP_K_DEFAULT))
        sort_by = str(p.get("sortBy", "sharpe"))
        symbol = p.get("symbol")
        strategy = p.get("strategy")
        min_trades = int(p.get("minTrades", 5))

        # Clamp limit
        limit = max(1, min(limit, _TOP_K_MAX))

        rows = top(
            n=limit,
            sort=sort_by,
            symbol=symbol,
            strategy=strategy,
            min_trades=min_trades,
        )

        # Format rows for MCP response
        results = []
        for r in rows:
            results.append({
                "runId": str(r.get("run_id", "")),
                "symbol": r.get("symbol", ""),
                "exchange": r.get("exchange", ""),
                "timeframe": r.get("timeframe", ""),
                "indicatorName": r.get("indicator_name", ""),
                "params": r.get("params", {}),
                "sharpe": r.get("sharpe"),
                "sortino": r.get("sortino"),
                "deflatedSharpe": r.get("deflated_sharpe"),
                "winRatePct": r.get("winrate"),
                "returnPct": r.get("return_pct"),
                "maxDrawdownPct": r.get("max_drawdown"),
                "totalTrades": r.get("num_trades"),
            })

        return {
            "status": "ok",
            "count": len(results),
            "sortBy": sort_by,
            "results": results,
        }
    except RuntimeError as exc:
        # CH_PASSWORD not set or ClickHouse unreachable
        return {
            "status": "unavailable",
            "message": f"ClickHouse not available: {exc}",
            "results": [],
        }
    except Exception as exc:
        logger.exception("backtest.top_k failed")
        return {"status": "error", "message": f"top_k query failed: {exc}"}


# ---------------------------------------------------------------------------
# Sync handler wrappers
# ---------------------------------------------------------------------------


def _handle_strategy_list(p: Dict[str, Any]) -> Dict[str, Any]:
    """List all strategies.

    Args:
        p: MCP tool params (no required fields).

    Returns:
        Dict with strategies list.
    """
    try:
        strategies = _list_strategies()
        return {"status": "ok", "strategies": strategies, "count": len(strategies)}
    except Exception as exc:
        logger.exception("strategy.list failed")
        return {"status": "error", "message": f"failed to list strategies: {exc}"}


def _handle_strategy_get(p: Dict[str, Any]) -> Dict[str, Any]:
    """Get full details for a strategy.

    Args:
        p: MCP tool params with 'name'.

    Returns:
        Dict with strategy details.
    """
    try:
        name = p.get("name")
        if not name:
            return {"status": "error", "message": "missing required param: 'name'"}
        name = str(name)
        card = _get_strategy(name)
        return {"status": "ok", "strategy": card}
    except KeyError as exc:
        return {"status": "error", "message": f"unknown strategy: {exc}"}
    except Exception as exc:
        logger.exception("strategy.get failed")
        return {"status": "error", "message": f"failed to get strategy: {exc}"}


def _handle_indicator_list(p: Dict[str, Any]) -> Dict[str, Any]:
    """List all indicators, optionally filtered by category.

    Args:
        p: MCP tool params with optional 'category'.

    Returns:
        Dict with indicators list.
    """
    try:
        category = p.get("category")
        indicators = _list_indicators(category)
        return {"status": "ok", "indicators": indicators, "count": len(indicators)}
    except Exception as exc:
        logger.exception("indicator.list failed")
        return {"status": "error", "message": f"failed to list indicators: {exc}"}


def _handle_indicator_get(p: Dict[str, Any]) -> Dict[str, Any]:
    """Get full details for an indicator.

    Args:
        p: MCP tool params with 'name'.

    Returns:
        Dict with indicator spec.
    """
    try:
        name = p.get("name")
        if not name:
            return {"status": "error", "message": "missing required param: 'name'"}
        name = str(name)
        card = _get_indicator(name)
        return {"status": "ok", "indicator": card}
    except KeyError as exc:
        return {"status": "error", "message": f"unknown indicator: {exc}"}
    except Exception as exc:
        logger.exception("indicator.get failed")
        return {"status": "error", "message": f"failed to get indicator: {exc}"}


def _handle_indicator_compute_preview(p: Dict[str, Any]) -> Dict[str, Any]:
    """Run an indicator on recent candles and return preview values.

    Args:
        p: MCP tool params with name, params, symbol, venue, timeframe, window.

    Returns:
        Dict with indicator preview values.
    """
    try:
        name = p.get("name")
        if not name:
            return {"status": "error", "message": "missing required param: 'name'"}
        name = str(name)
        params = p.get("params", {})
        symbol = p.get("symbol")
        if not symbol:
            return {"status": "error", "message": "missing required param: 'symbol'"}
        symbol = str(symbol)
        venue = str(p.get("venue", _DEFAULT_VENUE))
        timeframe = str(p.get("timeframe", _DEFAULT_TIMEFRAME))
        window = int(p.get("window", _DEFAULT_WINDOW))

        if window <= 0:
            return {"status": "error", "message": "window must be positive"}
        if window > _MAX_WINDOW:
            window = _MAX_WINDOW

        return _compute_preview(name, params, symbol, venue, timeframe, window)
    except KeyError as exc:
        return {"status": "error", "message": f"missing required param: {exc}"}
    except Exception as exc:
        logger.exception("indicator.compute_preview failed")
        return {"status": "error", "message": f"indicator preview failed: {exc}"}


def _handle_engine_list(p: Dict[str, Any]) -> Dict[str, Any]:
    """List all backtest engines with capabilities.

    Args:
        p: MCP tool params (no required fields).

    Returns:
        Dict with engines list.
    """
    try:
        engines = _list_engines()
        return {"status": "ok", "engines": engines, "count": len(engines)}
    except Exception as exc:
        logger.exception("engine.list failed")
        return {"status": "error", "message": f"failed to list engines: {exc}"}


# ---------------------------------------------------------------------------
# Tool definitions + registration
# ---------------------------------------------------------------------------


def _build_tools(ctx: ToolContext) -> list[tuple[McpTool, Any]]:
    """Build all backtest discovery MCP tools bound to the given context.

    Args:
        ctx: Shared dependency container (unused by backtest tools but required by interface).

    Returns:
        List of (McpTool, handler) tuples for registration.
    """

    # --- strategy.list ---
    t_strategy_list = McpTool(
        name="strategy.list",
        description=(
            "List all available backtest strategies. Each strategy is a "
            "callable that generates entry/exit signals from candle data. "
            "Use strategy.get(name) for full details."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {},
        },
        read_only=True,
        tags={"phase": "5", "group": "backtest", "status": "live"},
    )

    async def _strategy_list(p: Dict[str, Any]) -> Dict[str, Any]:
        """List strategies via sync helper in thread."""
        return await asyncio.to_thread(_handle_strategy_list, p)

    # --- strategy.get ---
    t_strategy_get = McpTool(
        name="strategy.get",
        description=(
            "Get full details for a strategy including description and "
            "parameter hints. Returns the strategy card with source hint."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Strategy name (e.g. 'sma_cross', 'rsi_reversal')",
                },
            },
            "required": ["name"],
        },
        read_only=True,
        tags={"phase": "5", "group": "backtest", "status": "live"},
    )

    async def _strategy_get(p: Dict[str, Any]) -> Dict[str, Any]:
        """Get strategy details via sync helper in thread."""
        return await asyncio.to_thread(_handle_strategy_get, p)

    # --- indicator.list ---
    t_indicator_list = McpTool(
        name="indicator.list",
        description=(
            "List all available technical indicators. Optionally filter by "
            "category (trend, momentum, volatility, volume, smart_money, crash). "
            "Each indicator has defaults and param_ranges for sweep planning."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Filter by category: trend, momentum, volatility, volume, smart_money, crash",
                },
            },
        },
        read_only=True,
        tags={"phase": "5", "group": "backtest", "status": "live"},
    )

    async def _indicator_list(p: Dict[str, Any]) -> Dict[str, Any]:
        """List indicators via sync helper in thread."""
        return await asyncio.to_thread(_handle_indicator_list, p)

    # --- indicator.get ---
    t_indicator_get = McpTool(
        name="indicator.get",
        description=(
            "Get full details for an indicator including defaults, param "
            "ranges, category, and description."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Indicator name (e.g. 'rsi', 'sma', 'macd_line')",
                },
            },
            "required": ["name"],
        },
        read_only=True,
        tags={"phase": "5", "group": "backtest", "status": "live"},
    )

    async def _indicator_get(p: Dict[str, Any]) -> Dict[str, Any]:
        """Get indicator details via sync helper in thread."""
        return await asyncio.to_thread(_handle_indicator_get, p)

    # --- indicator.compute_preview ---
    t_indicator_compute_preview = McpTool(
        name="indicator.compute_preview",
        description=(
            "Run an indicator on recent candle data and return the last N "
            "values. Useful for previewing what an indicator looks like "
            "before committing to a full backtest. Loads candles from the "
            "Postgres candles table."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Indicator name (e.g. 'rsi', 'sma')",
                },
                "params": {
                    "type": "object",
                    "description": "Indicator parameters (overrides defaults)",
                },
                "symbol": {"type": "string"},
                "venue": {
                    "type": "string",
                    "default": "bybit",
                },
                "timeframe": {
                    "type": "string",
                    "default": "1h",
                    "description": "Candle timeframe (1m, 5m, 15m, 1h, 4h, 1d)",
                },
                "window": {
                    "type": "integer",
                    "default": 200,
                    "minimum": 10,
                    "maximum": 1000,
                    "description": "Number of candles to load",
                },
            },
            "required": ["name", "symbol"],
        },
        read_only=True,
        tags={"phase": "5", "group": "backtest", "status": "live"},
    )

    async def _indicator_compute_preview_async(p: Dict[str, Any]) -> Dict[str, Any]:
        """Compute indicator preview via sync helper in thread."""
        return await asyncio.to_thread(_handle_indicator_compute_preview, p)

    # --- engine.list ---
    t_engine_list = McpTool(
        name="engine.list",
        description=(
            "List all available backtest engines with their capabilities. "
            "Each engine has different features (intrabar SL/TP, funding, "
            "vectorised sweeps, walk-forward analysis)."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {},
        },
        read_only=True,
        tags={"phase": "5", "group": "backtest", "status": "live"},
    )

    async def _engine_list(p: Dict[str, Any]) -> Dict[str, Any]:
        """List engines via sync helper in thread."""
        return await asyncio.to_thread(_handle_engine_list, p)

    # --- backtest.compose ---
    t_backtest_compose = McpTool(
        name="backtest.compose",
        description=(
            "Build a validated BacktestSpec without submitting it. Validates "
            "that the strategy and engine exist, and returns a spec dict "
            "with a paramHash for dedup. Use backtest.plan_sweep to expand "
            "parameter ranges, or backtest.submit to run it."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "strategy": {
                    "type": "string",
                    "description": "Strategy name from strategy.list",
                },
                "indicatorParams": {
                    "type": "object",
                    "description": "Parameters passed to the strategy/indicator",
                },
                "venue": {"type": "string", "default": "bybit"},
                "timeframe": {"type": "string", "default": "1h"},
                "from": {
                    "type": "string",
                    "description": "Start date (YYYY-MM-DD)",
                    "default": "2026-01-01",
                },
                "to": {
                    "type": "string",
                    "description": "End date (YYYY-MM-DD)",
                    "default": "2026-04-21",
                },
                "direction": {
                    "type": "string",
                    "enum": ["long", "short", "both"],
                    "default": "long",
                },
                "startingCashUsd": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "default": 10000,
                },
                "leverage": {"type": "number", "minimum": 1, "default": 1},
                "feeTakerBps": {"type": "number", "default": 5.0},
                "slippageBps": {"type": "number", "default": 2.0},
                "stopLossPct": {"type": "number", "default": 0},
                "takeProfitPct": {"type": "number", "default": 0},
                "engine": {
                    "type": "string",
                    "default": "classic",
                    "description": "Backtest engine from engine.list",
                },
            },
            "required": ["symbol", "strategy"],
        },
        read_only=True,
        tags={"phase": "5", "group": "backtest", "status": "live"},
    )

    async def _backtest_compose(p: Dict[str, Any]) -> Dict[str, Any]:
        """Compose backtest spec via sync helper in thread."""
        return await asyncio.to_thread(_compose_spec, p)

    # --- backtest.plan_sweep ---
    t_backtest_plan_sweep = McpTool(
        name="backtest.plan_sweep",
        description=(
            "Expand a parameter sweep from a base spec + param ranges. "
            "Generates the cartesian product of all parameter combinations. "
            "Max 500 specs per sweep. Use backtest.compose first to build "
            "the base spec, then add paramRanges to sweep over."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "baseSpec": {
                    "type": "object",
                    "description": "Base spec from backtest.compose",
                },
                "paramRanges": {
                    "type": "object",
                    "description": (
                        "Dict of param name → list of values to sweep. "
                        "e.g. {'period': [10, 14, 20], 'stddev': [1.5, 2.0]}"
                    ),
                },
            },
            "required": ["baseSpec", "paramRanges"],
        },
        read_only=True,
        tags={"phase": "5", "group": "backtest", "status": "live"},
    )

    async def _backtest_plan_sweep(p: Dict[str, Any]) -> Dict[str, Any]:
        """Plan parameter sweep via sync helper in thread."""
        return await asyncio.to_thread(_plan_sweep, p)

    # --- backtest.top_k ---
    t_backtest_top_k = McpTool(
        name="backtest.top_k",
        description=(
            "Read top-K backtest results from ClickHouse, sorted by a "
            "chosen metric (sharpe, sortino, deflated_sharpe, pnl_pct, "
            "winrate, profit_factor). Optionally filter by symbol and "
            "strategy. Returns 'unavailable' if ClickHouse is not configured."
        ),
        version="1",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 20,
                },
                "sortBy": {
                    "type": "string",
                    "default": "sharpe",
                    "description": "Metric to sort by: sharpe, sortino, deflated_sharpe, pnl_pct, winrate, profit_factor",
                },
                "symbol": {"type": "string"},
                "strategy": {"type": "string"},
                "minTrades": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 5,
                },
            },
        },
        read_only=True,
        tags={"phase": "5", "group": "backtest", "status": "live"},
    )

    async def _backtest_top_k(p: Dict[str, Any]) -> Dict[str, Any]:
        """Read top-K results via sync helper in thread."""
        return await asyncio.to_thread(_top_k, p)

    return [
        (t_strategy_list, _strategy_list),
        (t_strategy_get, _strategy_get),
        (t_indicator_list, _indicator_list),
        (t_indicator_get, _indicator_get),
        (t_indicator_compute_preview, _indicator_compute_preview_async),
        (t_engine_list, _engine_list),
        (t_backtest_compose, _backtest_compose),
        (t_backtest_plan_sweep, _backtest_plan_sweep),
        (t_backtest_top_k, _backtest_top_k),
    ]


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    """Register all backtest discovery tools with the MCP registry.

    Args:
        registry: The tool registry to register tools with.
        ctx: Shared dependency container for the MCP tools.
    """
    for tool, handler in _build_tools(ctx):
        registry.register(tool, handler)
