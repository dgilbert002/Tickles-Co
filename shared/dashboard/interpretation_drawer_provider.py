"""
Module: interpretation_drawer_provider
Purpose: 250ms-budgeted read-only provider for the Phase X.5 cross-tab
         Interpretation Drawer.
Location: /opt/tickles/shared/dashboard/interpretation_drawer_provider.py

Reads from the SHARED ``public.signal_interpretations`` table (joined
with ``public.news_items`` and ``public.media_items``) where the rich
LLM + quant + ChartHacker columns live (``chart_analysis``,
``trader_trades``, ``chart_hacker_trades``, ``ai_comment``,
``pattern_tags`` etc). The per-company ``signal_interpretations`` table
intentionally does NOT carry these columns — the drawer is a
shared-layer view, called from any of the four tabs.

Two query paths are exposed:

- ``fetch_by_news_item(news_item_id, *, limit)`` — returns 0..N
  interpretations attached to one news row. Used by the **News** tab
  (where the row only knows its ``news_item_id``).
- ``fetch_by_id(interp_id)`` — returns 0..1 interpretation by primary
  key. Used by the **Signals** / **Positions** / **Interpretations**
  tabs (which already carry the interpretation id on the rendered card).

Acceptance criteria (mirrored from PHASE X.4 / PHASE_Y §4.3):

- 250ms hard budget enforced via ``asyncio.wait_for``.
- Provider never raises into the dashboard request handler; on timeout
  / error it returns the documented fallback (an empty list) and logs
  a warning.
- Returns plain Python primitives ready for ``aiohttp.web.json_response``.
- All SQL is parameterised. No string interpolation of user input.
- Phase L is a strict CONSUMER — this module never writes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)

# 250ms hard budget per provider, matching PHASE_Y §4.3.
DRAWER_PROVIDER_TIMEOUT_S: float = 0.25

# Hard ceiling on per-news_item row count. A single news item rarely
# yields more than a couple of interpretations (one per model_version
# / param_hash combination), but we keep a defensive cap to bound the
# wire payload.
DRAWER_LIMIT_CAP: int = 25

# Default per-call limit for ``fetch_by_news_item``.
DRAWER_DEFAULT_LIMIT: int = 10

# Columns selected from ``signal_interpretations``. Kept as a module
# constant so the SELECT list is identical for both query paths and
# easy to audit. ``ni.*`` and ``mi.*`` aliases are appended in the
# query builder.
_INTERP_COLUMNS: str = (
    "si.id, "
    "si.news_item_id, "
    "si.media_item_id, "
    "si.trader_profile_id, "
    "si.consensus_direction, "
    "si.consensus_confidence, "
    "si.consensus_method, "
    "si.llm_direction, "
    "si.llm_confidence, "
    "si.llm_reasoning, "
    "si.llm_levels, "
    "si.quant_direction, "
    "si.quant_confidence, "
    "si.quant_indicators, "
    "si.instrument_symbol, "
    "si.instrument_exchange, "
    "si.instrument_symbol_normalised, "
    "si.timeframe, "
    "si.exchange, "
    "si.chart_analysis, "
    "si.trader_trades, "
    "si.chart_hacker_trades, "
    "si.ai_agreement_score, "
    "si.ai_comment, "
    "si.pattern_tags, "
    "si.setup_tags, "
    "si.regime_tags, "
    "si.session_tags, "
    "si.trader_stated_thesis, "
    "si.llm_inferred_thesis, "
    "si.reason_agreement_score, "
    "si.prompt_version, "
    "si.model_version, "
    "si.market_data_at, "
    "si.market_data_fresh, "
    "si.llm_cost_usd, "
    "si.quant_cost_usd, "
    "si.correlation_id, "
    "si.created_at, "
    "si.updated_at"
)


def _validate_id(value: Any, *, label: str) -> int:
    """Validate that ``value`` is a positive integer ID.

    Args:
        value: Caller-supplied identifier.
        label: Short identifier for log messages (e.g. ``"news_item_id"``).

    Returns:
        ``int(value)`` when it is a strictly positive integer.

    Raises:
        ValueError: If ``value`` is not a positive integer.
    """
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer, got {value!r}") from exc
    if n <= 0:
        raise ValueError(f"{label} must be > 0, got {n}")
    return n


def _normalise_limit(limit: Optional[int]) -> int:
    """Clamp ``limit`` to ``[1, DRAWER_LIMIT_CAP]`` with a default.

    Args:
        limit: Caller-supplied limit (may be ``None``).

    Returns:
        Integer in ``[1, DRAWER_LIMIT_CAP]``.
    """
    if limit is None:
        return DRAWER_DEFAULT_LIMIT
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return DRAWER_DEFAULT_LIMIT
    return max(1, min(DRAWER_LIMIT_CAP, n))


async def _run_with_budget(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    label: str,
    fallback: Any,
) -> Any:
    """Run an async callable under :data:`DRAWER_PROVIDER_TIMEOUT_S`.

    Args:
        coro_factory: Zero-arg callable returning a coroutine.
        label: Short identifier used in log messages.
        fallback: Value returned on timeout / cancellation / exception.

    Returns:
        The coroutine's result on success, otherwise ``fallback``.

    Raises:
        asyncio.CancelledError: Propagated so the host event loop can
            shut cleanly. All other exceptions are swallowed.
    """
    try:
        return await asyncio.wait_for(
            coro_factory(), timeout=DRAWER_PROVIDER_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.warning(
            "interpretation_drawer[%s]: exceeded %.0fms budget — "
            "returning fallback",
            label,
            DRAWER_PROVIDER_TIMEOUT_S * 1000,
        )
        return fallback
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "interpretation_drawer[%s]: provider failed: %s", label, exc
        )
        return fallback


class InterpretationDrawerProvider:
    """Reads ``signal_interpretations`` joined with ``news_items`` /
    ``media_items``.

    Output shape (per row) — see :meth:`_row_to_dict` for the full
    list. All ``Decimal`` / ``datetime`` values are coerced to JSON-
    safe primitives by the route layer's ``_jsonify``; this provider
    returns ISO-formatted timestamps and stringified decimals already
    so the route layer is a thin pass-through.

    Sort order:

    - ``fetch_by_news_item`` — ``si.created_at DESC, si.id DESC`` so
      the newest interpretation appears at the top of the drawer.
    - ``fetch_by_id`` — single row, no order needed.
    """

    async def fetch_by_news_item(
        self,
        news_item_id: int,
        *,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return all interpretations attached to one news item.

        Args:
            news_item_id: Primary key of ``public.news_items``. Must
                be a positive integer.
            limit: Maximum rows to return. Capped at
                :data:`DRAWER_LIMIT_CAP`. Default
                :data:`DRAWER_DEFAULT_LIMIT`.

        Returns:
            List of dict rows in the documented shape, newest first.
            Empty list on timeout, validation failure, or DB error.
        """
        try:
            nid = _validate_id(news_item_id, label="news_item_id")
        except ValueError as exc:
            logger.warning("InterpretationDrawerProvider: %s", exc)
            return []
        max_rows = _normalise_limit(limit)

        async def _do_fetch() -> List[Dict[str, Any]]:
            return await self._query_by_news_item(nid, max_rows)

        return await _run_with_budget(
            _do_fetch, label="by_news_item", fallback=[]
        )

    async def fetch_by_id(self, interp_id: int) -> List[Dict[str, Any]]:
        """Return a single interpretation by its primary key.

        Returns a list (always 0 or 1 row) so the route layer's
        response shape is identical regardless of which entry-point
        was hit.

        Args:
            interp_id: Primary key of ``public.signal_interpretations``.
                Must be a positive integer.

        Returns:
            One-element list on success; empty list on miss, timeout,
            validation failure, or DB error.
        """
        try:
            iid = _validate_id(interp_id, label="interp_id")
        except ValueError as exc:
            logger.warning("InterpretationDrawerProvider: %s", exc)
            return []

        async def _do_fetch() -> List[Dict[str, Any]]:
            return await self._query_by_id(iid)

        return await _run_with_budget(
            _do_fetch, label="by_id", fallback=[]
        )

    async def _query_by_news_item(
        self, news_item_id: int, limit: int
    ) -> List[Dict[str, Any]]:
        """Run the parameterised SELECT for ``fetch_by_news_item``.

        Args:
            news_item_id: Validated positive integer.
            limit: Validated row cap.

        Returns:
            List of dict rows. Never raises.
        """
        try:
            pool = await get_shared_pool()
        except Exception as exc:
            logger.warning(
                "InterpretationDrawerProvider: get_shared_pool failed: %s", exc
            )
            return []

        sql = self._build_query_by_news_item()
        try:
            async with pool.acquire() as conn:
                records = await conn.fetch(sql, news_item_id, limit)
        except Exception as exc:
            logger.warning(
                "InterpretationDrawerProvider: by_news_item query failed: %s",
                exc,
            )
            return []
        return [self._row_to_dict(r) for r in records]

    async def _query_by_id(self, interp_id: int) -> List[Dict[str, Any]]:
        """Run the parameterised SELECT for ``fetch_by_id``.

        Args:
            interp_id: Validated positive integer.

        Returns:
            List of 0 or 1 dict rows. Never raises.
        """
        try:
            pool = await get_shared_pool()
        except Exception as exc:
            logger.warning(
                "InterpretationDrawerProvider: get_shared_pool failed: %s", exc
            )
            return []

        sql = self._build_query_by_id()
        try:
            async with pool.acquire() as conn:
                records = await conn.fetch(sql, interp_id)
        except Exception as exc:
            logger.warning(
                "InterpretationDrawerProvider: by_id query failed: %s", exc
            )
            return []
        return [self._row_to_dict(r) for r in records]

    @staticmethod
    def _select_clause() -> str:
        """Return the canonical SELECT clause shared by both queries."""
        return (
            f"SELECT {_INTERP_COLUMNS}, "
            "       ni.headline       AS news_headline, "
            "       ni.source         AS news_source, "
            "       ni.collected_at   AS news_collected_at, "
            "       ni.published_at   AS news_published_at, "
            "       ni.channel_name   AS news_channel_name, "
            "       ni.author         AS news_author, "
            "       mi.local_path     AS media_local_path, "
            "       mi.thumbnail_path AS media_thumbnail_path, "
            "       mi.source_url     AS media_source_url, "
            "       mi.media_type     AS media_type "
            "FROM signal_interpretations si "
            "LEFT JOIN news_items  ni ON ni.id = si.news_item_id "
            "LEFT JOIN media_items mi ON mi.id = si.media_item_id "
        )

    def _build_query_by_news_item(self) -> str:
        """Return the parameterised SQL for ``fetch_by_news_item``.

        The query takes two positional parameters: ``$1`` = news_item_id,
        ``$2`` = limit. No caller value is interpolated into the SQL
        string.

        Returns:
            SQL string ready for ``conn.fetch(sql, news_item_id, limit)``.
        """
        return (
            self._select_clause()
            + "WHERE si.news_item_id = $1 "
            "ORDER BY si.created_at DESC, si.id DESC "
            "LIMIT $2"
        )

    def _build_query_by_id(self) -> str:
        """Return the parameterised SQL for ``fetch_by_id``.

        The query takes one positional parameter: ``$1`` = interp_id.

        Returns:
            SQL string ready for ``conn.fetch(sql, interp_id)``.
        """
        return self._select_clause() + "WHERE si.id = $1 LIMIT 1"

    def _row_to_dict(self, rec: Any) -> Dict[str, Any]:
        """Convert an asyncpg Record to the documented output dict.

        ``Decimal`` and ``datetime`` are coerced here so the route
        layer's ``_jsonify`` only has to walk nested JSONB payloads.

        Args:
            rec: asyncpg.Record (or dict-like) from the SELECT.

        Returns:
            JSON-friendly dict — primitives, ISO strings, plain lists/
            dicts. JSONB columns are passed through unchanged for the
            route's ``_jsonify`` to walk.
        """
        return {
            "id": int(rec["id"]),
            "news_item_id": _opt_int(rec["news_item_id"]),
            "media_item_id": _opt_int(rec["media_item_id"]),
            "trader_profile_id": _opt_int(rec["trader_profile_id"]),
            "consensus_direction": rec["consensus_direction"],
            "consensus_confidence": _opt_decimal(rec["consensus_confidence"]),
            "consensus_method": rec["consensus_method"],
            "llm": {
                "direction": rec["llm_direction"],
                "confidence": _opt_decimal(rec["llm_confidence"]),
                "reasoning": rec["llm_reasoning"],
                "levels": rec["llm_levels"],
                "inferred_thesis": rec["llm_inferred_thesis"],
                "cost_usd": _opt_decimal(rec["llm_cost_usd"]),
            },
            "quant": {
                "direction": rec["quant_direction"],
                "confidence": _opt_decimal(rec["quant_confidence"]),
                "indicators": rec["quant_indicators"],
                "cost_usd": _opt_decimal(rec["quant_cost_usd"]),
            },
            "instrument": {
                "symbol": rec["instrument_symbol"],
                "symbol_normalised": rec["instrument_symbol_normalised"],
                "exchange": rec["instrument_exchange"] or rec["exchange"],
                "timeframe": rec["timeframe"],
            },
            "chart_hacker": {
                "chart_analysis": rec["chart_analysis"],
                "trader_trades": rec["trader_trades"],
                "chart_hacker_trades": rec["chart_hacker_trades"],
                "ai_agreement_score": _opt_decimal(rec["ai_agreement_score"]),
                "ai_comment": rec["ai_comment"],
            },
            "tags": {
                "pattern": rec["pattern_tags"],
                "setup": rec["setup_tags"],
                "regime": rec["regime_tags"],
                "session": rec["session_tags"],
            },
            "thesis": {
                "trader_stated": rec["trader_stated_thesis"],
                "llm_inferred": rec["llm_inferred_thesis"],
                "reason_agreement_score": _opt_decimal(
                    rec["reason_agreement_score"]
                ),
            },
            "prompt_version": rec["prompt_version"],
            "model_version": rec["model_version"],
            "market_data_at": _opt_iso(rec["market_data_at"]),
            "market_data_fresh": (
                bool(rec["market_data_fresh"])
                if rec["market_data_fresh"] is not None
                else None
            ),
            "correlation_id": rec["correlation_id"],
            "created_at": _opt_iso(rec["created_at"]),
            "updated_at": _opt_iso(rec["updated_at"]),
            "news": {
                "headline": rec["news_headline"],
                "source": rec["news_source"],
                "channel_name": rec["news_channel_name"],
                "author": rec["news_author"],
                "collected_at": _opt_iso(rec["news_collected_at"]),
                "published_at": _opt_iso(rec["news_published_at"]),
            },
            "media": {
                "local_path": rec["media_local_path"],
                "thumbnail_path": rec["media_thumbnail_path"],
                "source_url": rec["media_source_url"],
                "media_type": rec["media_type"],
            },
        }


def _opt_int(value: Any) -> Optional[int]:
    """Return ``int(value)`` or ``None`` if value is ``None``.

    Args:
        value: Raw column value.

    Returns:
        Integer or ``None``.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_decimal(value: Any) -> Optional[str]:
    """Return ``str(value)`` for a Decimal/numeric, or ``None``.

    Decimals are stringified at the provider layer to avoid float
    precision loss across the JSON boundary.

    Args:
        value: Raw column value (Decimal, int, float, or None).

    Returns:
        String representation, or ``None`` when value is ``None``.
    """
    if value is None:
        return None
    return str(value)


def _opt_iso(value: Any) -> Optional[str]:
    """Return ``value.isoformat()`` or ``None``.

    Args:
        value: ``datetime`` / ``date`` / ``None``.

    Returns:
        ISO-formatted string, or ``None``.
    """
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


__all__ = [
    "DRAWER_DEFAULT_LIMIT",
    "DRAWER_LIMIT_CAP",
    "DRAWER_PROVIDER_TIMEOUT_S",
    "InterpretationDrawerProvider",
]
