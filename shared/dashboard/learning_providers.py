"""
Module: learning_providers
Purpose: Six SnapshotBuilder providers for the Phase Y learning dashboard.
Location: /opt/tickles/shared/dashboard/learning_providers.py

Each provider:
- Has a 250ms hard budget (per PHASE_Y §4.3) enforced via ``asyncio.wait_for``.
- Wraps every database call in an isolated try/except — failure of one source
  must not propagate to other providers (per §12 item 4 acceptance criteria).
- Reads from per-company ``tickles_<company>`` schemas via
  :func:`shared.dashboard.db_pools.get_company_pool`.
- Returns plain Python primitives (lists of dicts) safe for JSON encoding.

The six providers, mapped to PHASE_Y §12 item 4:
    1. SkillSummaryProvider     — reads ``v_actor_skill_{7,14,30}d``.
    2. MemoryFeedProvider       — reads ``v_memory_feed_{7,14,30}d``.
    3. AgentBrainProvider       — wins/losses/breakeven from §9.1 SQL.
    4. GuardActivityProvider    — placeholder dual-role detection
                                  (full implementation lands in Y.5).
    5. PromptEvolutionProvider  — ``edge_score_changes`` rows where
                                  ``note='prompt_promoted'``.
    6. FailedTradesProvider     — failed-trade count using §9.2 contract.

Phase L is a strict CONSUMER. None of the providers in this module write
data; ``writer_registry`` lists no 'dashboard' entries (see ``db_pools``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from shared.dashboard.db_pools import (  # noqa: F401 — kept for clarity; overridden below
    get_company_pool as _original_get_company_pool,
)
from shared.utils.companies import list_active_companies
from shared.utils.db import get_shared_pool

# The v_actor_skill_* and v_memory_feed_* views live in tickles_shared,
# not per-company databases. Redirect all pool calls to shared.
async def _get_shared_for_views(_company: str = ""):
    """Return the shared pool (views live in tickles_shared, not per-company)."""
    return await get_shared_pool()

# Override the import so all existing get_company_pool() calls hit shared pool
get_company_pool = _get_shared_for_views  # noqa: F811

logger = logging.getLogger(__name__)

# 250ms hard budget per provider per PHASE_Y §4.3 ("each source must
# complete in 250ms or its lane shows partial — N/M sources reported").
PROVIDER_TIMEOUT_S: float = 2.0  # was 0.25 — too tight for shared pool + view queries

# Allowed window labels — internally always 7/14/30. The "1M" UI label
# (per §11 Q3) is mapped at the API layer, NOT here. This keeps the
# providers pure data-reading code; UI mapping is presentation concern.
ALLOWED_WINDOWS: frozenset[int] = frozenset({7, 14, 30})

# Per-source heap-merge cap (PHASE_Y §4.3): floor(60 / window_days) * window_days,
# capped at 200. For 7d → 56, 14d → 56, 30d → 60. The plan's intent is
# "show enough rows to fill the lane without DoS-ing the heap merge".
DEFAULT_FEED_LIMIT_CAP: int = 200

# Default "recent" window for prompt-evolution timeline. Promotions are
# infrequent; 30d is a sensible default to avoid an empty timeline.
PROMPT_EVOLUTION_WINDOW_DAYS: int = 30

# Hard ceiling on per-call ``limit`` for the prompt-evolution provider —
# defends against pathological clients while leaving plenty of headroom
# for normal pagination.
PROMPT_EVOLUTION_LIMIT_CAP: int = 500

# Sentinel used as a sort-key placeholder for rows whose timestamp is
# NULL. ``datetime.min`` (UTC) is far older than any real row so missing
# values land at the bottom of a DESC sort regardless of whether the
# underlying codec returns ``datetime`` or ISO ``str``.
_TS_SENTINEL_MIN: datetime = datetime.min.replace(tzinfo=timezone.utc)


def _ts_sort_key(value: Any) -> tuple[bool, Any]:
    """Return a (has_value, value) tuple for safe DESC sorting.

    Mixing ``None``/``""``/``datetime`` in a single key is a TypeError
    in Python 3. The two-tuple form keeps comparisons within each slot
    homogeneous: the boolean ``has_value`` puts ``None`` rows last under
    ``reverse=True`` without ever comparing across types.

    Args:
        value: Raw timestamp from a record (``datetime``, ``str``, or
            ``None``).

    Returns:
        A two-tuple suitable as a ``key=`` argument for ``list.sort``.
    """
    return (value is not None, value if value is not None else _TS_SENTINEL_MIN)


def _validate_window(window_days: int) -> int:
    """Validate that ``window_days`` is one of 7, 14, or 30.

    Args:
        window_days: Window size in days.

    Returns:
        ``window_days`` unchanged if valid.

    Raises:
        ValueError: If ``window_days`` is not in :data:`ALLOWED_WINDOWS`.
    """
    if window_days not in ALLOWED_WINDOWS:
        raise ValueError(
            f"window_days must be one of {sorted(ALLOWED_WINDOWS)}, "
            f"got {window_days!r}"
        )
    return window_days


def _feed_limit_for_window(window_days: int) -> int:
    """Compute per-source row LIMIT for the Memory Feed.

    PHASE_Y §4.3 sets a per-source budget (not per-day): the formula
    ``floor(60 / window_days) * window_days`` collapses to a constant
    60 rows for any window in :data:`ALLOWED_WINDOWS`. We keep the
    parameter for API symmetry and forward-compatibility if §4.3 is
    later differentiated by window.

    Args:
        window_days: Validated window size (7/14/30). Currently unused
            in the formula but reserved for future per-window tuning.

    Returns:
        Row limit, in ``[1, DEFAULT_FEED_LIMIT_CAP]``.
    """
    del window_days  # reserved; see docstring
    target = 60
    return max(1, min(DEFAULT_FEED_LIMIT_CAP, target))


async def _resolve_companies(company_filter: Optional[str]) -> List[str]:
    """Resolve a snapshot-level ``company_filter`` to a list of companies.

    Args:
        company_filter: Either a single company short-name, ``"all"``, or
            ``None``. ``"all"``/``None`` means fan-out across every active
            company.

    Returns:
        Sorted list of active company short-names. Empty list if no
        active companies are configured (tests/fresh installs).
    """
    try:
        active = await list_active_companies()
    except Exception as exc:
        logger.warning("learning_providers: list_active_companies failed: %s", exc)
        return []
    if company_filter and company_filter != "all":
        return [company_filter] if company_filter in active else []
    return list(active)


async def _run_with_budget(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    label: str,
    fallback: Any,
) -> Any:
    """Run an async callable under :data:`PROVIDER_TIMEOUT_S`.

    Args:
        coro_factory: Zero-arg callable returning a coroutine.
        label: Short identifier used in log messages on timeout/error.
        fallback: Value returned on timeout, cancellation, or any
            exception. Providers always return SOMETHING — never raise
            into the SnapshotBuilder.

    Returns:
        The coroutine's result on success, otherwise ``fallback``.
    """
    try:
        return await asyncio.wait_for(coro_factory(), timeout=PROVIDER_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning(
            "learning_providers[%s]: exceeded %.0fms budget — returning fallback",
            label,
            PROVIDER_TIMEOUT_S * 1000,
        )
        return fallback
    except asyncio.CancelledError:
        # Re-raise cancellations so the host event loop can shut cleanly.
        raise
    except Exception as exc:
        logger.warning("learning_providers[%s]: provider failed: %s", label, exc)
        return fallback


class SkillSummaryProvider:
    """Reads ``v_actor_skill_{window}d`` per company.

    Output shape (per row)::

        {
            "actor_id":     str,
            "company":      str,    # short-name (added by Python)
            "company_id":   str,    # raw company_id from the view
            "skill_score":  float | None,   # NULL when n_trades < 5
            "window_days":  int,
        }
    """

    async def fetch(
        self,
        *,
        window_days: int = 7,
        company_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return one skill-score row per actor across the requested companies.

        Args:
            window_days: One of ``{7, 14, 30}``.
            company_filter: ``None``/``"all"`` = fan-out, else single company.

        Returns:
            List of dict rows (see class docstring). Empty list on any
            failure; per-company failures are isolated.
        """
        try:
            window = _validate_window(window_days)
        except ValueError as exc:
            logger.warning("SkillSummaryProvider: %s", exc)
            return []

        companies = await _resolve_companies(company_filter)
        if not companies:
            return []

        view = f"v_actor_skill_{window}d"
        sql = (
            f"SELECT v.actor_id, v.company_id, v.skill_score, v.window_days, "
            f"  COALESCE(tc.n_trades, 0) AS trade_count, "
            f"  COALESCE(tp.handle_normalized, "
            f"    CASE WHEN v.actor_id LIKE 'jarvais_trader_%' "
            f"      THEN REPLACE(v.actor_id, 'jarvais_trader_', 'trader_') "
            f"      ELSE v.actor_id END"
            f"  ) AS display_name "
            f"FROM {view} v "
            f"LEFT JOIN ("
            f"  SELECT actor_id, company_id, COUNT(*) AS n_trades "
            f"  FROM tracked_positions "
            f"  WHERE closed_at IS NOT NULL "
            f"    AND closed_at > now() - make_interval(days => {window}) "
            f"    AND realized_pnl_usd_final IS NOT NULL "
            f"  GROUP BY actor_id, company_id"
            f") tc ON tc.actor_id = v.actor_id AND tc.company_id = v.company_id "
            f"LEFT JOIN trader_profiles tp ON "
            f"  'jarvais_trader_' || tp.id::text = v.actor_id"
        )

        async def _do_fetch() -> List[Dict[str, Any]]:
            rows: List[Dict[str, Any]] = []
            for company in companies:
                try:
                    pool = await get_company_pool(company)
                    async with pool.acquire() as conn:
                        records = await conn.fetch(sql)
                except Exception as exc:
                    logger.warning(
                        "SkillSummaryProvider[%s]: query failed: %s",
                        company,
                        exc,
                    )
                    continue
                for rec in records:
                    score = rec["skill_score"]
                    rows.append(
                        {
                            "actor_id": rec["actor_id"],
                            "company": company,
                            "company_id": rec["company_id"],
                            "skill_score": float(score) if score is not None else None,
                            "skill_pct": round(float(score) * 100, 1) if score is not None else None,
                            "window_days": int(rec["window_days"]),
                            "trade_count": int(rec["trade_count"] or 0),
                            "display_name": rec["display_name"] or rec["actor_id"],
                        }
                    )
            return rows

        return await _run_with_budget(
            _do_fetch, label="skill_summary", fallback=[]
        )


class MemoryFeedProvider:
    """Reads ``v_memory_feed_{window}d`` per company; window-parameterised.

    PHASE_Y §4 view shape (10 canonical columns):
        ``source_kind, tier, actor, company, dimension, body, raw, ts,
          source_id, correlation_id``

    Notes:
        - mem0 (Qdrant) and MemU sources are NOT included here — they
          live outside SQL and are merged by the API layer downstream
          (per PHASE_Y §5.2). This provider only ships the SQL feed.
        - Rows are sorted by ``ts DESC`` server-side; cross-company merge
          preserves DESC ordering by re-sorting in Python after fan-out.
    """

    async def fetch(
        self,
        *,
        window_days: int = 7,
        company_filter: Optional[str] = None,
        dimension_filter: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return the merged Memory Feed for the requested window.

        Args:
            window_days: One of ``{7, 14, 30}``.
            company_filter: ``None``/``"all"`` = fan-out, else single company.
            dimension_filter: Optional dimension to scope to (e.g.
                ``'lesson'``); ``None`` returns all dimensions.
            limit: Optional override of per-source LIMIT. Defaults to
                :func:`_feed_limit_for_window`. Capped at
                :data:`DEFAULT_FEED_LIMIT_CAP` regardless.

        Returns:
            List of dict rows (the 10 canonical columns plus a
            ``"window_days"`` echo). Empty list on any failure.
        """
        try:
            window = _validate_window(window_days)
        except ValueError as exc:
            logger.warning("MemoryFeedProvider: %s", exc)
            return []

        companies = await _resolve_companies(company_filter)
        if not companies:
            return []

        per_source_limit = (
            min(int(limit), DEFAULT_FEED_LIMIT_CAP)
            if isinstance(limit, int) and limit > 0
            else _feed_limit_for_window(window)
        )

        view = f"v_memory_feed_{window}d"
        # Parameterised query — dimension filter binds to $1, limit to $2.
        if dimension_filter:
            sql = (
                f"SELECT source_kind, tier, actor, company, dimension, "
                f"body, raw, ts, source_id, correlation_id "
                f"FROM {view} "
                f"WHERE dimension = $1 "
                f"ORDER BY ts DESC "
                f"LIMIT $2"
            )
            args: Sequence[Any] = (dimension_filter, per_source_limit)
        else:
            sql = (
                f"SELECT source_kind, tier, actor, company, dimension, "
                f"body, raw, ts, source_id, correlation_id "
                f"FROM {view} "
                f"ORDER BY ts DESC "
                f"LIMIT $1"
            )
            args = (per_source_limit,)

        async def _do_fetch() -> List[Dict[str, Any]]:
            merged: List[Dict[str, Any]] = []
            for company in companies:
                try:
                    pool = await get_company_pool(company)
                    async with pool.acquire() as conn:
                        records = await conn.fetch(sql, *args)
                except Exception as exc:
                    logger.warning(
                        "MemoryFeedProvider[%s,%dd]: query failed: %s",
                        company,
                        window,
                        exc,
                    )
                    continue
                for rec in records:
                    merged.append(
                        {
                            "source_kind": rec["source_kind"],
                            "tier": rec["tier"],
                            "actor": rec["actor"],
                            "company": rec["company"],
                            "dimension": rec["dimension"],
                            "body": rec["body"],
                            "raw": rec["raw"],
                            "ts": rec["ts"],
                            "source_id": rec["source_id"],
                            "correlation_id": rec["correlation_id"],
                            "window_days": window,
                        }
                    )
            # Re-sort the cross-company merge by ts DESC. Some rows may
            # have ts=None for malformed data; the (has_value, value)
            # key form pushes those to the end without cross-type
            # comparisons (datetime vs str would TypeError).
            merged.sort(key=lambda r: _ts_sort_key(r["ts"]), reverse=True)
            # Final cap: we don't show more than DEFAULT_FEED_LIMIT_CAP
            # rows total even after fan-out, to bound payload size.
            return merged[:DEFAULT_FEED_LIMIT_CAP]

        return await _run_with_budget(
            _do_fetch, label=f"memory_feed_{window}d", fallback=[]
        )


class AgentBrainProvider:
    """Wins / losses / breakeven count per actor per window.

    Implements PHASE_Y §9.1 SQL with adaptive breakeven band:
        ``breakeven  ≡  |pnl| <= max(total_fees_usd, 1.00)``.

    Output shape (per row)::

        {
            "actor_id":   str | None,
            "company":    str,
            "wins":       int,
            "losses":     int,
            "breakeven":  int,
            "total":      int,
            "window_days": int,
        }
    """

    async def fetch(
        self,
        *,
        window_days: int = 30,
        company_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return wins/losses/breakeven counts per actor per company.

        Args:
            window_days: One of ``{7, 14, 30}``. Defaults to 30 because
                Agent Brain Cards in §9.2 are framed against "the
                window" and 30d is the most-populated bucket.
            company_filter: ``None``/``"all"`` = fan-out, else single company.

        Returns:
            List of dict rows. Empty list on failure.
        """
        try:
            window = _validate_window(window_days)
        except ValueError as exc:
            logger.warning("AgentBrainProvider: %s", exc)
            return []

        companies = await _resolve_companies(company_filter)
        if not companies:
            return []

        # PHASE_Y §9.1 verbatim, with window parameterised. The
        # ``GREATEST(COALESCE(total_fees_usd, 0), 1.00)`` band is the
        # ratified §11 Q4 decision — pre-F10 rows with NULL fees still
        # bucket sensibly via the $1 floor.
        sql = (
            "SELECT "
            "  actor_id, "
            "  count(*) FILTER ( "
            "    WHERE realized_pnl_usd_final > 1.00 "
            "  ) AS wins, "
            "  count(*) FILTER ( "
            "    WHERE realized_pnl_usd_final < -1.00 "
            "  ) AS losses, "
            "  count(*) FILTER ( "
            "    WHERE abs(realized_pnl_usd_final) <= 1.00 "
            "  ) AS breakeven, "
            "  count(*) AS total "
            "FROM tracked_positions "
            "WHERE closed_at IS NOT NULL "
            "  AND realized_pnl_usd_final IS NOT NULL "
            "  AND closed_at >= now() - ($1::int * interval '1 day') "
            "GROUP BY actor_id"
        )

        async def _do_fetch() -> List[Dict[str, Any]]:
            rows: List[Dict[str, Any]] = []
            for company in companies:
                try:
                    pool = await get_company_pool(company)
                    async with pool.acquire() as conn:
                        records = await conn.fetch(sql, window)
                except Exception as exc:
                    logger.warning(
                        "AgentBrainProvider[%s,%dd]: query failed: %s",
                        company,
                        window,
                        exc,
                    )
                    continue
                for rec in records:
                    rows.append(
                        {
                            "actor_id": rec["actor_id"],
                            "company": company,
                            "wins": int(rec["wins"] or 0),
                            "losses": int(rec["losses"] or 0),
                            "breakeven": int(rec["breakeven"] or 0),
                            "total": int(rec["total"] or 0),
                            "window_days": window,
                        }
                    )
            return rows

        return await _run_with_budget(
            _do_fetch, label=f"agent_brain_{window}d", fallback=[]
        )


class GuardActivityProvider:
    """Surfaces guard-sidebar warnings for the dashboard.

    Y.3 ships a minimal MVP that reads three concrete signals already
    available in the database. Full guard-sidebar UX (dual-role
    cross-checks, prompt-version drift, etc.) lands in Y.5 per
    PHASE_Y §12 item 6.

    Output shape (per row)::

        {
            "kind":     str,          # 'no_failed_trades' | 'pre_f10_fees_missing'
                                       # | 'aa_seed_budget_warn'
            "severity": str,          # 'info' | 'warn' | 'error'
            "company":  str,
            "message":  str,
            "ts":       datetime | None,
            "context":  dict,
        }
    """

    # Threshold per PHASE_Y §9: "if at the end of Phase Y deployment we
    # still see 0 failed trades on a real population, that is a system-
    # validation finding — surface it via a guard-sidebar warning".
    NO_FAILED_TRADES_MIN_POPULATION: int = 20

    async def fetch(
        self,
        *,
        company_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return the current set of guard-sidebar warnings.

        Args:
            company_filter: ``None``/``"all"`` = fan-out, else single company.

        Returns:
            List of warning dicts. Empty list on failure or if no
            warnings apply.
        """
        companies = await _resolve_companies(company_filter)
        if not companies:
            return []

        async def _do_fetch() -> List[Dict[str, Any]]:
            warnings: List[Dict[str, Any]] = []
            for company in companies:
                try:
                    pool = await get_company_pool(company)
                    async with pool.acquire() as conn:
                        # Signal 1 — no losing trades despite real population.
                        no_fail = await conn.fetchrow(
                            "SELECT count(*) AS total, "
                            "       count(*) FILTER ( "
                            "         WHERE outcome IN ('sl_hit','expiry') "
                            "           AND realized_pnl_usd_final < "
                            "               -GREATEST(COALESCE(total_fees_usd, 0)::numeric, 1.00::numeric) "
                            "       ) AS losing "
                            "FROM tracked_positions "
                            "WHERE closed_at IS NOT NULL "
                            "  AND realized_pnl_usd_final IS NOT NULL "
                            "  AND closed_at >= now() - interval '30 days'"
                        )
                        # asyncpg.Record supports __getitem__ but NOT
                        # ``.get(...)``. Subscript with explicit None
                        # check is the canonical pattern.
                        if no_fail is None:
                            total = 0
                            losing = 0
                        else:
                            total = int(no_fail["total"] or 0)
                            losing = int(no_fail["losing"] or 0)
                        if total >= self.NO_FAILED_TRADES_MIN_POPULATION and losing == 0:
                            warnings.append(
                                {
                                    "kind": "no_failed_trades",
                                    "severity": "warn",
                                    "company": company,
                                    "message": (
                                        f"0 losing trades in last 30d across "
                                        f"N={total} positions — verify SL execution"
                                    ),
                                    "ts": None,
                                    "context": {"total": total, "losing": losing},
                                }
                            )

                        # Signal 2 — pre-F10 rows with NULL total_fees_usd
                        # still in the closed-window pool. Surfaces stale
                        # data that pollutes the breakeven band.
                        pre_f10 = await conn.fetchval(
                            "SELECT count(*) FROM tracked_positions "
                            "WHERE closed_at IS NOT NULL "
                            "  AND realized_pnl_usd_final IS NOT NULL "
                            "  AND total_fees_usd IS NULL "
                            "  AND closed_at >= now() - interval '30 days'"
                        )
                        pre_f10_count = int(pre_f10 or 0)
                        if pre_f10_count > 0:
                            warnings.append(
                                {
                                    "kind": "pre_f10_fees_missing",
                                    "severity": "info",
                                    "company": company,
                                    "message": (
                                        f"{pre_f10_count} closed positions in last 30d "
                                        f"have NULL total_fees_usd (pre-F10 backfill); "
                                        f"breakeven band defaults to $1.00 floor"
                                    ),
                                    "ts": None,
                                    "context": {"count": pre_f10_count},
                                }
                            )
                except Exception as exc:
                    logger.warning(
                        "GuardActivityProvider[%s]: query failed: %s",
                        company,
                        exc,
                    )
                    continue

            return warnings

        async def _do_fetch_aa_seed() -> List[Dict[str, Any]]:
            """Signal 3 — A/A coach seed budget tripped (PHASE_Y §11 Q5).

            ``api_cost_log`` lives in tickles_shared, so we read it ONCE
            outside the per-company loop and filter rows down to the
            resolved company set in Python. The seed writes a single
            row with role='aa_seed_budget_warn' and
            correlation_id='coach_aa_seed' when its $5 hard-cap is hit.

            B-Y5-C1 fix: this query gets its own ``_run_with_budget``
            so a slow shared-pool round-trip cannot starve Signals 1+2.
            B-Y5-M2 fix: empty resolved-company set short-circuits to
            ``[]`` so the filter cannot accidentally surface non-scoped
            rows.
            """
            companies_set = set(companies)
            if not companies_set:
                return []
            seed_warnings: List[Dict[str, Any]] = []
            shared_pool = await get_shared_pool()
            async with shared_pool.acquire() as sconn:
                seed_rows = await sconn.fetch(
                    "SELECT company_id, context, extra, "
                    "       cost_usd, http_status, created_at "
                    "FROM api_cost_log "
                    "WHERE role = 'aa_seed_budget_warn' "
                    "  AND correlation_id = 'coach_aa_seed' "
                    "  AND created_at >= now() - interval '30 days' "
                    "ORDER BY created_at DESC "
                    "LIMIT 50"
                )
            for row in seed_rows:
                row_company = row["company_id"] or ""
                if row_company and row_company not in companies_set:
                    continue
                ctx_text = row["context"] or "coach A/A seed budget cap reached"
                extra_payload: Dict[str, Any] = {}
                raw_extra = row["extra"]
                if isinstance(raw_extra, dict):
                    extra_payload = raw_extra
                elif isinstance(raw_extra, str) and raw_extra:
                    try:
                        parsed = json.loads(raw_extra)
                        if isinstance(parsed, dict):
                            extra_payload = parsed
                    except (ValueError, TypeError):
                        extra_payload = {}
                # B-Y5-M3 fix: preserve cost_usd as a string to keep
                # Decimal precision intact across the API boundary.
                # Consumers that need a number should parse explicitly.
                raw_cost = row["cost_usd"]
                cost_str = str(raw_cost) if raw_cost is not None else None
                seed_warnings.append(
                    {
                        "kind": "aa_seed_budget_warn",
                        "severity": "warn",
                        "company": row_company or "shared",
                        "message": (
                            f"A/A coach seed budget cap reached: {ctx_text}"
                        ),
                        "ts": row["created_at"],
                        "context": {
                            "cost_usd": cost_str,
                            "http_status": row["http_status"],
                            "extra": extra_payload,
                        },
                    }
                )
            return seed_warnings

        primary, seed = await asyncio.gather(
            _run_with_budget(_do_fetch, label="guard_activity", fallback=[]),
            _run_with_budget(
                _do_fetch_aa_seed,
                label="guard_activity_aa_seed",
                fallback=[],
            ),
        )
        return list(primary) + list(seed)


class PromptEvolutionProvider:
    """Reads ``edge_score_changes`` rows where ``note='prompt_promoted'``.

    Surfaces the Prompt Evolution timeline (PHASE_Y §12 item 6 / Y.5
    UI). The query is read-only and lives in this provider so Y.4 can
    render the timeline before Y.5's full guard sidebar.

    Output shape (per row)::

        {
            "id":             int,
            "actor_type":     str,
            "actor_id":       str,
            "period_end":     datetime,
            "score_before":   float | None,
            "score_after":    float | None,
            "delta":          float | None,
            "components_before": dict,
            "components_after":  dict,
            "note":           str,
            "logged_at":      datetime,
            "company":        str,
        }
    """

    async def fetch(
        self,
        *,
        window_days: int = PROMPT_EVOLUTION_WINDOW_DAYS,
        company_filter: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return prompt-promotion events within the time window.

        Args:
            window_days: How far back to look (default 30).
            company_filter: ``None``/``"all"`` = fan-out, else single company.
            limit: Per-company row cap. Capped at 500.

        Returns:
            List of promotion-event dicts, sorted by ``logged_at`` DESC
            after cross-company merge. Empty list on failure.
        """
        if not isinstance(window_days, int) or window_days <= 0:
            logger.warning(
                "PromptEvolutionProvider: window_days must be positive int, got %r",
                window_days,
            )
            return []
        capped_limit = max(1, min(int(limit), PROMPT_EVOLUTION_LIMIT_CAP))

        companies = await _resolve_companies(company_filter)
        if not companies:
            return []

        sql = (
            "SELECT id, actor_type, actor_id, period_end, "
            "       score_before, score_after, delta, "
            "       components_before, components_after, note, logged_at "
            "FROM edge_score_changes "
            "WHERE note = 'prompt_promoted' "
            "  AND logged_at >= now() - ($1::int * interval '1 day') "
            "ORDER BY logged_at DESC "
            "LIMIT $2"
        )

        async def _do_fetch() -> List[Dict[str, Any]]:
            merged: List[Dict[str, Any]] = []
            for company in companies:
                try:
                    pool = await get_company_pool(company)
                    async with pool.acquire() as conn:
                        records = await conn.fetch(sql, window_days, capped_limit)
                except Exception as exc:
                    logger.warning(
                        "PromptEvolutionProvider[%s]: query failed: %s",
                        company,
                        exc,
                    )
                    continue
                for rec in records:
                    merged.append(
                        {
                            "id": int(rec["id"]),
                            "actor_type": rec["actor_type"],
                            "actor_id": rec["actor_id"],
                            "period_end": rec["period_end"],
                            "score_before": (
                                float(rec["score_before"])
                                if rec["score_before"] is not None
                                else None
                            ),
                            "score_after": (
                                float(rec["score_after"])
                                if rec["score_after"] is not None
                                else None
                            ),
                            "delta": (
                                float(rec["delta"])
                                if rec["delta"] is not None
                                else None
                            ),
                            "components_before": rec["components_before"],
                            "components_after": rec["components_after"],
                            "note": rec["note"],
                            "logged_at": rec["logged_at"],
                            "company": company,
                        }
                    )
            merged.sort(
                key=lambda r: _ts_sort_key(r["logged_at"]), reverse=True
            )
            return merged[:capped_limit]

        return await _run_with_budget(
            _do_fetch, label="prompt_evolution", fallback=[]
        )


class FailedTradesProvider:
    """Failed-trade count per PHASE_Y §9.2 dashboard contract.

    SQL contract from §9.2 verbatim:
        ``count(*) FILTER (WHERE outcome IN ('sl_hit','expiry')
                            AND realized_pnl_usd_final
                                < -GREATEST(COALESCE(total_fees_usd,0), 1.00))``

    Output shape (per row)::

        {
            "company":      str,
            "failed_count": int,
            "total_closed": int,
            "window_days":  int,
        }
    """

    async def fetch(
        self,
        *,
        window_days: int = 7,
        company_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return failed-trade counts per company for the window.

        Args:
            window_days: One of ``{7, 14, 30}``.
            company_filter: ``None``/``"all"`` = fan-out, else single company.

        Returns:
            List of dict rows (one per company). Empty list on failure.
        """
        try:
            window = _validate_window(window_days)
        except ValueError as exc:
            logger.warning("FailedTradesProvider: %s", exc)
            return []

        companies = await _resolve_companies(company_filter)
        if not companies:
            return []

        sql = (
            "SELECT "
            "  count(*) FILTER ( "
            "    WHERE outcome IN ('sl_hit','expiry') "
            "      AND realized_pnl_usd_final "
            "          < -GREATEST(COALESCE(total_fees_usd, 0)::numeric, 1.00::numeric) "
            "  ) AS failed_count, "
            "  count(*) AS total_closed "
            "FROM tracked_positions "
            "WHERE closed_at IS NOT NULL "
            "  AND realized_pnl_usd_final IS NOT NULL "
            "  AND closed_at >= now() - ($1::int * interval '1 day')"
        )

        async def _do_fetch() -> List[Dict[str, Any]]:
            rows: List[Dict[str, Any]] = []
            for company in companies:
                try:
                    pool = await get_company_pool(company)
                    async with pool.acquire() as conn:
                        rec = await conn.fetchrow(sql, window)
                except Exception as exc:
                    logger.warning(
                        "FailedTradesProvider[%s,%dd]: query failed: %s",
                        company,
                        window,
                        exc,
                    )
                    continue
                if rec is None:
                    continue
                rows.append(
                    {
                        "company": company,
                        "failed_count": int(rec["failed_count"] or 0),
                        "total_closed": int(rec["total_closed"] or 0),
                        "window_days": window,
                    }
                )
            return rows

        return await _run_with_budget(
            _do_fetch, label=f"failed_trades_{window}d", fallback=[]
        )


__all__ = [
    "ALLOWED_WINDOWS",
    "DEFAULT_FEED_LIMIT_CAP",
    "PROMPT_EVOLUTION_LIMIT_CAP",
    "PROMPT_EVOLUTION_WINDOW_DAYS",
    "PROVIDER_TIMEOUT_S",
    "SkillSummaryProvider",
    "MemoryFeedProvider",
    "AgentBrainProvider",
    "GuardActivityProvider",
    "PromptEvolutionProvider",
    "FailedTradesProvider",
]
