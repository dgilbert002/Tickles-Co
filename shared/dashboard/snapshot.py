"""
shared.dashboard.snapshot — assembles the DashboardSnapshot JSON
structure from whatever data sources are wired in.

We keep this defensive: any one lookup can fail (e.g. the regime
service isn't wired yet, or the VPS is offline) and the snapshot
still returns with a clear note in ``snapshot.notes`` explaining
which data was missing. The dashboard UI can then render the
healthy sections and flag the gaps.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple

from shared.dashboard.db_pools import get_company_pool
from shared.dashboard.protocol import DashboardSnapshot
from shared.utils.companies import list_active_companies

LOG = logging.getLogger("tickles.dashboard.snapshot")

# Snapshot cache: (tab, company_filter, *args) -> (timestamp, data)
_SNAPSHOT_CACHE: Dict[Tuple[Any, ...], Tuple[float, Any]] = {}
_CACHE_TTL = 10.0  # 10 seconds


class ServicesProvider(Protocol):
    async def list_services(self) -> List[dict]: ...


class SubmissionsProvider(Protocol):
    async def list_active(self, limit: int) -> List[dict]: ...
    async def list_recent(self, limit: int) -> List[dict]: ...


class IntentsProvider(Protocol):
    async def latest_intents(self, limit: int) -> List[dict]: ...


class RegimeProvider(Protocol):
    async def get_current_regime(self) -> dict: ...


class GuardrailsProvider(Protocol):
    async def get_status(self) -> dict: ...


@dataclass
class SnapshotProviders:
    services: Optional[ServicesProvider] = None
    submissions: Optional[SubmissionsProvider] = None
    intents: Optional[IntentsProvider] = None
    regime: Optional[RegimeProvider] = None
    guardrails: Optional[GuardrailsProvider] = None


@dataclass
class SnapshotBuilder:
    """Assembles a :class:`DashboardSnapshot` from configured providers and SQL aggregations."""

    providers: SnapshotProviders = field(default_factory=SnapshotProviders)

    async def build(self, company_filter: str | None = None) -> DashboardSnapshot:
        """Build a full snapshot for the dashboard API.

        Args:
            company_filter: Optional company short-name to scope the result.
                ``None`` or ``"all"`` aggregates across every active company.

        Returns:
            A populated :class:`DashboardSnapshot`. Any individual data source
            failure is captured into ``snap.notes`` instead of raising.
        """
        snap = DashboardSnapshot(generated_at=datetime.now(timezone.utc))

        # Overview stats — flatten into top-level fields
        try:
            stats = await get_overview_stats(company_filter)
            for key, value in stats.items():
                if hasattr(snap, key):
                    setattr(snap, key, value)
        except Exception as e:
            LOG.error("Overview stats failed: %s", e)
            snap.notes.append(f"Overview stats failed: {e}")

        # Tab data
        try:
            snap.positions = await aggregate_open_positions(company_filter)
        except Exception as e:
            LOG.error("Positions aggregation failed: %s", e)
            snap.notes.append(f"Positions failed: {e}")

        try:
            snap.leaderboard = await aggregate_leaderboard(company_filter)
        except Exception as e:
            LOG.error("Leaderboard aggregation failed: %s", e)
            snap.notes.append(f"Leaderboard failed: {e}")

        try:
            snap.signals = await aggregate_signals(company_filter)
        except Exception as e:
            LOG.error("Signals aggregation failed: %s", e)
            snap.notes.append(f"Signals failed: {e}")

        try:
            snap.interpretations = await aggregate_interpretations(company_filter)
        except Exception as e:
            LOG.error("Interpretations aggregation failed: %s", e)
            snap.notes.append(f"Interpretations failed: {e}")

        await self._load_services(snap)
        await self._load_submissions(snap)
        await self._load_intents(snap)
        await self._load_regime(snap)
        await self._load_guardrails(snap)

        return snap

    async def _load_services(self, snap: DashboardSnapshot) -> None:
        """Populate ``snap.services`` from the optional services provider."""
        if self.providers.services is None:
            return
        try:
            snap.services = await self.providers.services.list_services()
        except Exception as e:
            snap.notes.append(f"Services failed: {e}")

    async def _load_submissions(self, snap: DashboardSnapshot) -> None:
        """Populate ``snap.submissions_recent`` and active count from the optional provider."""
        if self.providers.submissions is None:
            return
        try:
            active = await self.providers.submissions.list_active(limit=10)
            snap.submissions_active = len(active) if isinstance(active, list) else int(active or 0)
            snap.submissions_recent = await self.providers.submissions.list_recent(limit=10)
        except Exception as e:
            snap.notes.append(f"Submissions failed: {e}")

    async def _load_intents(self, snap: DashboardSnapshot) -> None:
        """Populate ``snap.latest_intents`` from the optional intents provider."""
        if self.providers.intents is None:
            return
        try:
            snap.latest_intents = await self.providers.intents.latest_intents(limit=20)
        except Exception as e:
            snap.notes.append(f"Intents failed: {e}")

    async def _load_regime(self, snap: DashboardSnapshot) -> None:
        """Populate ``snap.regime_current`` from the optional regime provider."""
        if self.providers.regime is None:
            return
        try:
            snap.regime_current = await self.providers.regime.get_current_regime()
        except Exception as e:
            snap.notes.append(f"Regime failed: {e}")

    async def _load_guardrails(self, snap: DashboardSnapshot) -> None:
        """Populate ``snap.guardrails_active`` from the optional guardrails provider."""
        if self.providers.guardrails is None:
            return
        try:
            status = await self.providers.guardrails.get_status()
            if isinstance(status, dict):
                snap.guardrails_active = status.get("active", []) or []
            elif isinstance(status, list):
                snap.guardrails_active = status
        except Exception as e:
            snap.notes.append(f"Guardrails failed: {e}")


async def get_overview_stats(company_filter: str | None = None) -> Dict[str, Any]:
    """Calculate aggregate stats for the overview strip."""
    cache_key = ("overview_stats", company_filter)
    now = time.monotonic()
    if cache_key in _SNAPSHOT_CACHE:
        ts, data = _SNAPSHOT_CACHE[cache_key]
        if now - ts < _CACHE_TTL:
            return data

    from shared.utils.db import get_shared_pool
    shared_pool = await get_shared_pool()

    # NOTE: stats labelled "_today" are intentionally rolling 24h windows
    # (NOW() - INTERVAL '24 hours'). The original CURRENT_DATE filter
    # was off-by-many-hours after midnight UTC and made the dashboard
    # appear empty for most of every day. We keep the JSON keys for
    # frontend compatibility — the strip label is "24h" in the new UI.

    # 1. Rolling-24h API cost (shared)
    cost_row = await shared_pool.fetch_one(
        "SELECT SUM(cost_usd) as total FROM api_cost_log "
        "WHERE created_at >= NOW() - INTERVAL '24 hours'"
    )
    api_cost = float(cost_row["total"] or 0.0) if cost_row else 0.0

    # 2. Open positions & Ingest depth (fan-out)
    companies = await list_active_companies()
    if company_filter and company_filter != "all":
        companies = [c for c in companies if c == company_filter]

    total_open = 0
    total_pnl = 0.0
    trader_open = 0
    agent_open = 0
    ingest_depth = 0
    signals_today = 0

    async with shared_pool.acquire() as shared_conn:
        # Open positions: prefer the positions_current view (it joins to
        # the live exchange ledger via the position monitor) and fall
        # back to tracked_positions WHERE status='open' if the view is
        # missing or empty. positions_current uses 'unrealised_pnl_usd'
        # (British spelling); tracked_positions uses the American one.
        try:
            pc_rows = await shared_conn.fetch(
                "SELECT company_id, direction, "
                "COALESCE(unrealised_pnl_usd, 0) AS pnl "
                "FROM positions_current"
            )
        except Exception:
            pc_rows = []
        if pc_rows:
            for r in pc_rows:
                if company_filter and company_filter != "all" and r["company_id"] != company_filter:
                    continue
                total_open += 1
                total_pnl += float(r["pnl"] or 0.0)
            # positions_current doesn't carry actor_type — best-effort:
            # split is captured in detailed Positions tab, not the strip.
            trader_open = total_open
            agent_open = 0
        else:
            pos_query = """
                SELECT actor_type, company_id, COUNT(*) as cnt, SUM(COALESCE(unrealized_pnl_usd, 0)) as pnl
                FROM tracked_positions
                WHERE status='open'
                GROUP BY actor_type, company_id
            """
            pos_rows = await shared_conn.fetch(pos_query)
            for r in pos_rows:
                if company_filter and company_filter != "all" and r["company_id"] != company_filter:
                    continue
                total_open += r["cnt"]
                total_pnl += float(r["pnl"] or 0.0)
                if r["actor_type"] == "trader":
                    trader_open += r["cnt"]
                else:
                    agent_open += r["cnt"]

        # Ingest depth (shared table)
        depth_query = "SELECT COUNT(*) FROM news_items WHERE enrichment_status='pending'"
        # Note: news_items doesn't have company_id, it's global ingest
        ingest_depth = await shared_conn.fetchval(depth_query) or 0

        # Signals: rolling 24h window (see NOTE above re: key naming).
        sig_query = (
            "SELECT COUNT(*) FROM signal_interpretations "
            "WHERE created_at >= NOW() - INTERVAL '24 hours'"
        )
        signals_today = await shared_conn.fetchval(sig_query) or 0

    stats = {
        "signals_today_count": signals_today,
        "api_cost_today_usd": api_cost,
        "open_positions_count": total_open,
        "open_positions_unrealized_pnl": total_pnl,
        "open_positions_trader_count": trader_open,
        "open_positions_agent_count": agent_open,
        "ingest_depth": ingest_depth,
    }
    
    _SNAPSHOT_CACHE[cache_key] = (now, stats)
    return stats


async def aggregate_leaderboard(company_filter: str | None = None) -> List[dict]:
    """Aggregate actor_leaderboard view across companies.

    When the per-company `actor_leaderboard` view is empty (postmortem
    pipeline hasn't written rows yet), derive a synthetic preview from
    `signal_interpretations` aggregated by `trader_profile_id` over
    7 days. Synthetic rows carry `_synthetic: true` so the UI can badge
    them as preview data, not real edge scores.
    """
    cache_key = ("leaderboard", company_filter)
    now = time.monotonic()
    if cache_key in _SNAPSHOT_CACHE:
        ts, data = _SNAPSHOT_CACHE[cache_key]
        if now - ts < _CACHE_TTL:
            return data

    rows: List[dict] = []
    companies = await list_active_companies()
    if company_filter and company_filter != "all":
        companies = [c for c in companies if c == company_filter]

    for company in companies:
        try:
            pool = await get_company_pool(company)
            async with pool.acquire() as conn:
                cr = await conn.fetch("SELECT * FROM actor_leaderboard ORDER BY rank ASC LIMIT 50")
                rows.extend({**dict(r), "_company": company} for r in cr)
        except Exception as e:
            LOG.error("Leaderboard failed for %s: %s", company, e)

    if not rows:
        # Synthetic fallback: rank traders by interpretation volume +
        # average consensus confidence over the last 7 days. This is a
        # preview — not a real edge score — so flag it for the UI.
        # Note: signal_interpretations rows are written into tickles_shared
        # (per-company copies are dual-written but currently empty) so we
        # query the shared DB and group by company_id when available.
        try:
            from shared.utils.db import get_shared_pool
            shared_pool = await get_shared_pool()
            async with shared_pool.acquire() as sconn:
                # company_id may not be populated on signal_interpretations —
                # fall through to ungrouped if the column doesn't exist.
                has_company_col = await sconn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='signal_interpretations' "
                    "AND column_name='company_id')"
                )
                if has_company_col and company_filter and company_filter != "all":
                    synth = await sconn.fetch(
                        "SELECT trader_profile_id, "
                        "COUNT(*) AS sample_count, "
                        "AVG(consensus_confidence)::float AS avg_confidence, "
                        "MAX(created_at) AS last_seen "
                        "FROM signal_interpretations "
                        "WHERE created_at >= NOW() - INTERVAL '7 days' "
                        "AND company_id = $1 "
                        "GROUP BY trader_profile_id "
                        "ORDER BY sample_count DESC, avg_confidence DESC NULLS LAST "
                        "LIMIT 50",
                        company_filter,
                    )
                else:
                    synth = await sconn.fetch(
                        "SELECT trader_profile_id, "
                        "COUNT(*) AS sample_count, "
                        "AVG(consensus_confidence)::float AS avg_confidence, "
                        "MAX(created_at) AS last_seen "
                        "FROM signal_interpretations "
                        "WHERE created_at >= NOW() - INTERVAL '7 days' "
                        "GROUP BY trader_profile_id "
                        "ORDER BY sample_count DESC, avg_confidence DESC NULLS LAST "
                        "LIMIT 50"
                    )
                if synth:
                    profile_ids = [r["trader_profile_id"] for r in synth]
                    profile_rows = await sconn.fetch(
                        "SELECT id, handle_normalized, display_name, "
                        "trader_type, platform "
                        "FROM trader_profiles WHERE id = ANY($1::bigint[])",
                        profile_ids,
                    )
                    profiles = {p["id"]: dict(p) for p in profile_rows}
                    # Default _company tag: the filter when set, else the
                    # first active company (best-effort attribution for the
                    # dual-write era).
                    default_company = (
                        company_filter
                        if (company_filter and company_filter != "all")
                        else (companies[0] if companies else None)
                    )
                    for rank_idx, r in enumerate(synth, start=1):
                        prof = profiles.get(r["trader_profile_id"]) or {}
                        rows.append({
                            "actor_type": prof.get("trader_type") or "trader",
                            "actor_id": prof.get("handle_normalized")
                                or (f"trader#{r['trader_profile_id']}"),
                            "display_name": prof.get("display_name"),
                            "platform": prof.get("platform"),
                            "period_start": None,
                            "period_end": None,
                            "closed_position_count": int(r["sample_count"] or 0),
                            "edge_score": float(r["avg_confidence"]) if r["avg_confidence"] is not None else None,
                            "confidence_low": True,
                            "components_jsonb": None,
                            "formula_version": None,
                            "rank": rank_idx,
                            "_company": default_company,
                            "_synthetic": True,
                            "_last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
                        })
        except Exception as e:
            LOG.warning("Synthetic leaderboard fallback failed: %s", e)

    # Sort aggregate by edge_score
    rows.sort(key=lambda x: x.get("edge_score") or 0, reverse=True)

    _SNAPSHOT_CACHE[cache_key] = (now, rows)
    return rows


async def aggregate_open_positions(company_filter: str | None = None) -> List[dict]:
    """Read-only fan-out across active companies.

    `company_filter='all' or None` aggregates every company; otherwise restricts.
    Each row is tagged with `_company` so the UI can render a per-company column.
    """
    cache_key = ("positions", company_filter)
    now = time.monotonic()
    if cache_key in _SNAPSHOT_CACHE:
        ts, data = _SNAPSHOT_CACHE[cache_key]
        if now - ts < _CACHE_TTL:
            return data

    from shared.utils.db import get_shared_pool
    shared_pool = await get_shared_pool()

    rows: List[dict] = []

    async with shared_pool.acquire() as conn:
        # Primary source: positions_current view (live exchange ledger).
        # The view doesn't carry every tracked_positions field, so we
        # synthesise the keys the frontend expects.
        try:
            pc_params: List[Any] = []
            pc_sql = (
                "SELECT id, company_id, adapter, exchange, account_id_external, "
                "symbol, direction, quantity, average_entry_price, notional_usd, "
                "unrealised_pnl_usd, realized_pnl_usd, leverage, ts, source, metadata "
                "FROM positions_current"
            )
            if company_filter and company_filter != "all":
                pc_sql += " WHERE company_id = $1"
                pc_params.append(company_filter)
            pc_sql += " ORDER BY ts DESC LIMIT 100"
            pc = await conn.fetch(pc_sql, *pc_params)
            for r in pc:
                d = dict(r)
                # Translate to the column names tracked_positions consumers
                # expect, so the frontend table schema doesn't have to fork.
                rows.append({
                    "id": d["id"],
                    "company_id": d.get("company_id"),
                    "_company": d.get("company_id"),
                    "instrument_symbol": d.get("symbol"),
                    "direction": d.get("direction"),
                    "position_size": float(d["quantity"]) if d.get("quantity") is not None else None,
                    "entry_price": float(d["average_entry_price"]) if d.get("average_entry_price") is not None else None,
                    "notional_usd": float(d["notional_usd"]) if d.get("notional_usd") is not None else None,
                    "unrealized_pnl_usd": float(d["unrealised_pnl_usd"]) if d.get("unrealised_pnl_usd") is not None else 0.0,
                    "realized_pnl_usd": float(d["realized_pnl_usd"]) if d.get("realized_pnl_usd") is not None else 0.0,
                    "leverage": d.get("leverage"),
                    "status": "open",
                    "actor_id": d.get("account_id_external"),
                    "signal_timestamp": d.get("ts"),
                    "_source": "positions_current",
                })
        except Exception as exc:
            LOG.warning("positions_current read failed: %s — falling back to tracked_positions", exc)

        # Fallback: if positions_current returns nothing, surface the
        # most recent N closed positions so the Positions tab is never
        # empty when historical data exists. The strip's
        # open_positions_count still reflects the truth (only live opens).
        if not rows:
            query = "SELECT * FROM tracked_positions"
            params: List[Any] = []
            if company_filter and company_filter != "all":
                query += " WHERE company_id = $1"
                params.append(company_filter)
            query += " ORDER BY signal_timestamp DESC NULLS LAST LIMIT 100"
            cr = await conn.fetch(query, *params)
            for r in cr:
                d = dict(r)
                d["_company"] = d.get("company_id")
                d["_source"] = "tracked_positions_recent"
                rows.append(d)

    _SNAPSHOT_CACHE[cache_key] = (now, rows)
    return rows


async def aggregate_signals(company_filter: str | None = None, limit: int = 50) -> List[dict]:
    """Fetch recent signals from the shared signal_interpretations table.

    Args:
        company_filter: Optional company short-name; ``None`` or ``"all"`` returns all rows.
        limit: Maximum number of rows to return.

    Returns:
        List of signal dicts ordered by ``created_at`` DESC, each tagged with
        ``_company`` when a ``company_id`` column is present.
    """
    cache_key = ("signals", company_filter, limit)
    now = time.monotonic()
    if cache_key in _SNAPSHOT_CACHE:
        ts, data = _SNAPSHOT_CACHE[cache_key]
        if now - ts < _CACHE_TTL:
            return data

    from shared.utils.db import get_shared_pool

    rows: List[dict] = []
    try:
        shared_pool = await get_shared_pool()
        async with shared_pool.acquire() as conn:
            base_sql = (
                "SELECT si.*, "
                "       mi.local_path AS media_local_path, "
                "       mi.thumbnail_path AS media_thumbnail_path, "
                "       mi.source_url AS media_source_url, "
                "       mi.media_type AS media_type, "
                "       ni.headline AS news_headline, "
                "       ni.source AS news_source, "
                "       ni.metadata AS news_metadata "
                "FROM signal_interpretations si "
                "LEFT JOIN media_items mi ON mi.id = si.media_item_id "
                "LEFT JOIN news_items ni ON ni.id = si.news_item_id "
            )
            if company_filter and company_filter != "all":
                cr = await conn.fetch(
                    base_sql
                    + "WHERE si.company_id = $1 "
                      "ORDER BY si.created_at DESC LIMIT $2",
                    company_filter, limit,
                )
            else:
                cr = await conn.fetch(
                    base_sql + "ORDER BY si.created_at DESC LIMIT $1",
                    limit,
                )
            for r in cr:
                d = dict(r)
                if "company_id" in d:
                    d["_company"] = d.get("company_id")
                # Build a stable media URL the front-end can <img src="..."> directly.
                # Use a relative path (no leading slash) so the URL works under both
                # the root mount ("/") and the "/dashboard/" mount.
                if d.get("media_local_path") and d.get("media_item_id") is not None:
                    d["media_url"] = f"api/media/{d['media_item_id']}"
                else:
                    d["media_url"] = None
                rows.append(d)
    except Exception as e:
        LOG.error("aggregate_signals failed: %s", e)

    _SNAPSHOT_CACHE[cache_key] = (now, rows)
    return rows


async def aggregate_interpretations(
    company_filter: str | None = None, limit: int = 50
) -> List[dict]:
    """Fetch recent interpretations for the Interpretations tab.

    This is the read-model used by ``GET /api/interpretations``. It mirrors
    :func:`aggregate_signals` but exposes a stable name and shape for the
    dashboard tab and isolates future divergence (e.g. extra joins for
    chart links, post-mortem flags, etc.).

    Args:
        company_filter: Optional company short-name; ``None`` or ``"all"`` returns all rows.
        limit: Maximum number of rows to return.

    Returns:
        List of interpretation dicts ordered by ``created_at`` DESC.
    """
    cache_key = ("interpretations", company_filter, limit)
    now = time.monotonic()
    if cache_key in _SNAPSHOT_CACHE:
        ts, data = _SNAPSHOT_CACHE[cache_key]
        if now - ts < _CACHE_TTL:
            return data

    from shared.utils.db import get_shared_pool

    rows: List[dict] = []
    try:
        shared_pool = await get_shared_pool()
        async with shared_pool.acquire() as conn:
            base_sql = (
                "SELECT si.*, "
                "       mi.local_path AS media_local_path, "
                "       mi.thumbnail_path AS media_thumbnail_path, "
                "       mi.source_url AS media_source_url, "
                "       mi.media_type AS media_type, "
                "       ni.headline AS news_headline, "
                "       ni.source AS news_source, "
                "       ni.metadata AS news_metadata "
                "FROM signal_interpretations si "
                "LEFT JOIN media_items mi ON mi.id = si.media_item_id "
                "LEFT JOIN news_items ni ON ni.id = si.news_item_id "
            )
            if company_filter and company_filter != "all":
                cr = await conn.fetch(
                    base_sql
                    + "WHERE si.company_id = $1 "
                      "ORDER BY si.created_at DESC LIMIT $2",
                    company_filter, limit,
                )
            else:
                cr = await conn.fetch(
                    base_sql + "ORDER BY si.created_at DESC LIMIT $1",
                    limit,
                )
            for r in cr:
                d = dict(r)
                if "company_id" in d:
                    d["_company"] = d.get("company_id")
                # Build a stable media URL the front-end can <img src="..."> directly.
                # Use a relative path (no leading slash) so the URL works under both
                # the root mount ("/") and the "/dashboard/" mount.
                if d.get("media_local_path") and d.get("media_item_id") is not None:
                    d["media_url"] = f"api/media/{d['media_item_id']}"
                else:
                    d["media_url"] = None
                rows.append(d)
    except Exception as e:
        LOG.error("aggregate_interpretations failed: %s", e)

    _SNAPSHOT_CACHE[cache_key] = (now, rows)
    return rows


async def get_interpretation_by_id(interp_id: int, company: str | None = None) -> Optional[dict]:
    """Fetch a single interpretation by ID, joined with its media + news rows.

    The chart_renderer needs ``local_path`` (from ``media_items``); the front-end
    needs the headline (from ``news_items``). We provide both in one query.
    """
    from shared.utils.db import get_shared_pool
    shared_pool = await get_shared_pool()

    async with shared_pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "SELECT si.*, "
                "       mi.local_path AS media_local_path, "
                "       mi.thumbnail_path AS media_thumbnail_path, "
                "       mi.source_url AS media_source_url, "
                "       mi.media_type AS media_type, "
                "       ni.headline AS news_headline, "
                "       ni.source AS news_source, "
                "       ni.metadata AS news_metadata "
                "FROM signal_interpretations si "
                "LEFT JOIN media_items mi ON mi.id = si.media_item_id "
                "LEFT JOIN news_items ni ON ni.id = si.news_item_id "
                "WHERE si.id = $1",
                interp_id,
            )
        except Exception as e:
            LOG.error("get_interpretation_by_id failed: %s", e)
            return None
        if not row:
            return None
        d = dict(row)
        # chart_renderer expects ``local_path`` — back-fill from join alias
        d["local_path"] = d.get("media_local_path")
        return d


async def get_trader_drill_data(trader_id: str, company_filter: str | None = None) -> dict:
    """Fetch performance and recent trades for a specific trader."""
    # This still needs to be per-company as trader_performance is per-company
    companies = await list_active_companies()
    if company_filter and company_filter != "all":
        companies = [c for c in companies if c == company_filter]

    perf = []
    trades = []

    for company in companies:
        try:
            pool = await get_company_pool(company)
            async with pool.acquire() as conn:
                # Performance
                p = await conn.fetchrow(
                    "SELECT * FROM trader_performance WHERE trader_id = $1 ORDER BY computed_at DESC LIMIT 1",
                    trader_id
                )
                if p:
                    perf.append({**dict(p), "_company": company})
                
                # Recent trades (from shared ledger, filtered by trader_id and company)
                from shared.utils.db import get_shared_pool
                shared_pool = await get_shared_pool()
                async with shared_pool.acquire() as s_conn:
                    t = await s_conn.fetch(
                        "SELECT * FROM tracked_positions WHERE actor_id = $1 AND company_id = $2 ORDER BY signal_timestamp DESC LIMIT 20",
                        trader_id, company
                    )
                    trades.extend({**dict(r), "_company": company} for r in t)
        except Exception as e:
            LOG.error("Trader drill failed for %s: %s", company, e)

    return {
        "trader_id": trader_id,
        "performance": perf,
        "recent_trades": sorted(trades, key=lambda x: x["signal_timestamp"], reverse=True)[:50]
    }


async def get_queue_data() -> dict:
    """Fetch real-time ingest queue status from shared tables."""
    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()
    
    async with pool.acquire() as conn:
        news = await conn.fetch(
            "SELECT id, source, headline, enrichment_status, collected_at FROM news_items ORDER BY collected_at DESC LIMIT 10"
        )
        media = await conn.fetch(
            "SELECT id, news_item_id, media_type, status, created_at FROM media_items ORDER BY created_at DESC LIMIT 10"
        )
        # Positions are also in shared
        pos = await conn.fetch(
            "SELECT id, instrument_symbol, direction, status, created_at FROM tracked_positions ORDER BY created_at DESC LIMIT 10"
        )
        
        return {
            "news": [dict(r) for r in news],
            "media": [dict(r) for r in media],
            "positions": [dict(r) for r in pos]
        }


def snapshot_to_dict(snap: DashboardSnapshot) -> dict:
    """Serialise a :class:`DashboardSnapshot` to a JSON-ready dict.

    Walks every dataclass field, converting :class:`datetime` to ISO-8601
    strings and recursively serialising nested lists/dicts. Non-serialisable
    values are coerced to ``str`` as a last-resort fallback so the response
    never raises.

    Args:
        snap: The snapshot to serialise.

    Returns:
        A flat dict suitable for ``aiohttp.web.json_response``.
    """
    from dataclasses import fields
    from decimal import Decimal

    def serialise(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, list):
            return [serialise(i) for i in v]
        if isinstance(v, dict):
            return {k: serialise(val) for k, val in v.items()}
        if isinstance(v, (str, int, float, bool)):
            return v
        try:
            return str(v)
        except Exception:
            return None

    out: Dict[str, Any] = {}
    for f in fields(snap):
        out[f.name] = serialise(getattr(snap, f.name))
    return out
