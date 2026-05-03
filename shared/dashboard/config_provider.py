"""
Module: config_provider
Purpose: 250ms-budgeted SnapshotBuilder provider for the Phase X.6 Config tab.
Location: /opt/tickles/shared/dashboard/config_provider.py

Reads from two distinct config tables:

1. **Shared** ``tickles_shared.public.system_config`` — rows of
   ``(namespace, config_key, config_value, is_secret, updated_at)``
   covering global pipeline knobs (candles retention, intelligence
   thresholds, db pool sizes, etc.).

2. **Per-company** ``tickles_<short_name>.public.company_config`` —
   rows of ``(config_key, config_value, updated_at)`` covering a
   company's trading rails (capital, risk, drawdown caps).

The frozen-legacy ``jarvais`` company is intentionally excluded by
:func:`shared.utils.companies.list_active_companies`, so it never
appears under the per-company subset even though its DB exists.

Acceptance criteria (mirrored from PHASE_Y §4.3):

- 250ms hard budget enforced via ``asyncio.wait_for``.
- Provider never raises into the SnapshotBuilder; on timeout / error
  it returns the documented fallback (an empty snapshot) and logs a
  warning.
- Returns plain Python primitives ready for ``aiohttp.web.json_response``.
- All SQL is parameterised. No string interpolation of user input.
- Phase L is a strict CONSUMER — this module never writes.
- ``is_secret=true`` rows have their ``config_value`` redacted to
  the literal string ``"***"`` BEFORE leaving the provider, so the
  HTTP layer can never accidentally leak secrets.

Output shape::

    {
        "shared":    {namespace: [ {key, value, is_secret, updated_at}, ... ], ...},
        "companies": {short_name: [ {key, value, updated_at}, ... ], ...},
    }

Both maps are dicts keyed lexicographically; row lists are sorted by
``config_key`` for deterministic UI rendering.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from shared.utils.companies import list_active_companies
from shared.utils.db import get_company_pool, get_shared_pool

logger = logging.getLogger(__name__)

# 250ms hard budget per provider, matching PHASE_Y §4.3.
CONFIG_PROVIDER_TIMEOUT_S: float = 0.25

# Sentinel string returned in place of ``config_value`` whenever
# ``is_secret`` is true. The provider redacts at the source so the
# raw value never crosses the asyncio boundary into the HTTP handler.
SECRET_REDACTION: str = "***"


def _normalise_company_filter(company: Optional[str]) -> Optional[str]:
    """Strip & lowercase a caller-supplied company filter.

    ``None`` / empty / ``"all"`` (any case) all mean "no narrowing".
    Short-names are lower-cased to match
    :func:`shared.utils.companies.list_active_companies`, which always
    returns lower-case identifiers — without this, ``?company=RUBICON``
    would silently fall through and return an empty per-company subset.

    Args:
        company: Raw caller value.

    Returns:
        Trimmed lower-case short-name, or ``None`` to disable filtering.
    """
    if company is None:
        return None
    cleaned = company.strip().lower()
    if not cleaned:
        return None
    if cleaned == "all":
        return None
    return cleaned


async def _run_with_budget(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    label: str,
    fallback: Any,
) -> Any:
    """Run an async callable under :data:`CONFIG_PROVIDER_TIMEOUT_S`.

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
            coro_factory(), timeout=CONFIG_PROVIDER_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.warning(
            "config_provider[%s]: exceeded %.0fms budget — returning fallback",
            label,
            CONFIG_PROVIDER_TIMEOUT_S * 1000,
        )
        return fallback
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("config_provider[%s]: provider failed: %s", label, exc)
        return fallback


def _iso(ts: Any) -> Optional[str]:
    """Return ``ts.isoformat()`` if ``ts`` looks like a datetime, else ``None``."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.isoformat()
    # Defensive: asyncpg always hands us datetime, but custom backends
    # could pass strings — preserve them rather than crashing.
    try:
        return str(ts)
    except Exception:
        return None


def _redact(value: Any, is_secret: bool) -> Optional[str]:
    """Redact ``value`` to :data:`SECRET_REDACTION` when ``is_secret``.

    NULL values stay ``None`` regardless of the secret flag — there's
    nothing to leak and the UI can render "—" naturally.

    Args:
        value: Raw config value (text or NULL from the DB).
        is_secret: Whether the row is flagged as secret.

    Returns:
        ``None``, the redaction sentinel, or the stringified value.
    """
    if value is None:
        return None
    if is_secret:
        return SECRET_REDACTION
    return str(value)


def _shared_row_to_dict(rec: Any) -> Dict[str, Any]:
    """Convert one ``system_config`` asyncpg.Record to the output shape."""
    is_secret = bool(rec["is_secret"])
    return {
        "key": rec["config_key"],
        "value": _redact(rec["config_value"], is_secret),
        "is_secret": is_secret,
        "updated_at": _iso(rec["updated_at"]),
    }


def _company_row_to_dict(rec: Any) -> Dict[str, Any]:
    """Convert one ``company_config`` asyncpg.Record to the output shape.

    Per-company rows do NOT carry an ``is_secret`` flag — the schema
    is intentionally simpler. We surface the value verbatim and let
    the UI render it as text.
    """
    return {
        "key": rec["config_key"],
        "value": None if rec["config_value"] is None else str(rec["config_value"]),
        "updated_at": _iso(rec["updated_at"]),
    }


def _group_by_namespace(rows: List[Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Bucket shared rows by ``namespace`` and sort each bucket by ``key``.

    Args:
        rows: asyncpg.Record list from ``SELECT … FROM system_config``.

    Returns:
        Mapping ``namespace -> [row, …]`` with rows sorted by ``key``
        and outer keys sorted lexicographically. Determinism makes
        UI rendering stable across reloads.
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for rec in rows:
        ns = rec["namespace"] or ""
        buckets.setdefault(ns, []).append(_shared_row_to_dict(rec))
    for ns in buckets:
        buckets[ns].sort(key=lambda r: r["key"] or "")
    # Re-build the dict in sorted order so JSON serialisation is stable.
    return {ns: buckets[ns] for ns in sorted(buckets)}


class ConfigSnapshotProvider:
    """Reads ``system_config`` + per-company ``company_config``.

    Output shape (see module docstring)::

        {
            "shared":    {namespace: [{key, value, is_secret, updated_at}, ...]},
            "companies": {short_name: [{key, value, updated_at}, ...]},
        }

    Errors at any level degrade to empty maps for that level so a
    single broken company DB never poisons the whole snapshot.
    """

    async def fetch(
        self,
        *,
        company_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return the combined config snapshot.

        Args:
            company_filter: ``None`` / ``"all"`` for every active
                company, else a short-name to narrow to one.

        Returns:
            Dict with ``"shared"`` and ``"companies"`` sub-maps. Empty
            sub-maps on timeout / DB error — never raises.
        """
        company = _normalise_company_filter(company_filter)

        async def _do_fetch() -> Dict[str, Any]:
            shared_rows = await self._fetch_shared()
            company_rows = await self._fetch_companies(company)
            return {
                "shared": _group_by_namespace(shared_rows),
                "companies": company_rows,
            }

        return await _run_with_budget(
            _do_fetch,
            label="config_snapshot",
            fallback={"shared": {}, "companies": {}},
        )

    # ------------------------------------------------------------------
    # Shared subset
    # ------------------------------------------------------------------

    async def _fetch_shared(self) -> List[Any]:
        """Read every row from ``tickles_shared.public.system_config``.

        Returns the asyncpg records verbatim — grouping happens in the
        public ``fetch`` so it can be unit-tested independently.

        Returns:
            List of asyncpg.Record (may be empty on any failure).
        """
        try:
            pool = await get_shared_pool()
        except Exception as exc:
            logger.warning(
                "ConfigSnapshotProvider: get_shared_pool failed: %s", exc
            )
            return []

        sql = self._build_shared_query()
        try:
            async with pool.acquire() as conn:
                return list(await conn.fetch(sql))
        except Exception as exc:
            logger.warning(
                "ConfigSnapshotProvider: system_config query failed: %s", exc
            )
            return []

    def _build_shared_query(self) -> str:
        """Return the SELECT for ``system_config``.

        No parameters — this is a full-table read so PG planner picks
        a SeqScan regardless. We return a plain string for symmetry
        with the per-company query that DOES take params.

        ``discord_hwm`` rows are excluded — they're operational
        high-water-mark cursors rewritten by the ingest pipeline, not
        human-tunable config, and they pollute the UI with hundreds
        of opaque IDs that change minute-by-minute.
        """
        return (
            "SELECT namespace, config_key, config_value, is_secret, updated_at "
            "FROM system_config "
            "WHERE namespace <> 'discord_hwm' "
            "ORDER BY namespace ASC, config_key ASC"
        )

    # ------------------------------------------------------------------
    # Per-company subset
    # ------------------------------------------------------------------

    async def _fetch_companies(
        self,
        company_filter: Optional[str],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Fan out across active companies and collect their config rows.

        Args:
            company_filter: ``None`` for every active company, else a
                short-name. Unknown short-names degrade to an empty
                dict (they're not in the active list).

        Returns:
            Mapping ``short_name -> [row, …]``. Companies with read
            failures are simply omitted — never raise.
        """
        try:
            active = await list_active_companies()
        except Exception as exc:
            logger.warning(
                "ConfigSnapshotProvider: list_active_companies failed: %s",
                exc,
            )
            return {}

        if company_filter is not None:
            if company_filter not in active:
                logger.debug(
                    "config_provider: company=%r not in active list — "
                    "returning empty per-company subset",
                    company_filter,
                )
                return {}
            targets: List[str] = [company_filter]
        else:
            targets = list(active)

        if not targets:
            return {}

        # Run each company read concurrently — they hit different
        # databases and benefit from overlap. asyncio.gather keeps
        # the budget envelope tight.
        results = await asyncio.gather(
            *[self._fetch_one_company(name) for name in targets],
            return_exceptions=True,
        )
        out: Dict[str, List[Dict[str, Any]]] = {}
        for name, res in zip(targets, results):
            if isinstance(res, BaseException):
                # Warning, not debug: a company silently disappearing
                # from the Config tab is the kind of thing operators
                # want to spot in routine log scans.
                logger.warning(
                    "config_provider: %s read raised %s — skipping",
                    name,
                    type(res).__name__,
                )
                continue
            out[name] = res
        # Sort outer dict so JSON output is deterministic.
        return {k: out[k] for k in sorted(out)}

    async def _fetch_one_company(
        self, company: str
    ) -> List[Dict[str, Any]]:
        """Read ``company_config`` for one company's database.

        Failure semantics: this method **re-raises** infrastructure
        problems (pool unavailable, query errors, schema drift) so the
        outer ``asyncio.gather(..., return_exceptions=True)`` in
        :meth:`_fetch_companies` can omit the company from the snapshot.
        A successful query that returned zero rows still yields an
        empty list — that's a meaningfully different state and the UI
        is welcome to show "no rows" rather than hide the company.

        Args:
            company: Validated short-name from the active list.

        Returns:
            Sorted list of row dicts. Empty list when the table really
            is empty.

        Raises:
            Exception: Re-raises any underlying DB failure so the
                caller can drop this company from the snapshot.
        """
        pool = await get_company_pool(company)
        sql = (
            "SELECT config_key, config_value, updated_at "
            "FROM company_config "
            "ORDER BY config_key ASC"
        )
        async with pool.acquire() as conn:
            records = await conn.fetch(sql)
        return [_company_row_to_dict(r) for r in records]


__all__ = [
    "CONFIG_PROVIDER_TIMEOUT_S",
    "SECRET_REDACTION",
    "ConfigSnapshotProvider",
]
