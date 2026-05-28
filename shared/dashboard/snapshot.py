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

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple
from urllib.parse import quote

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

        async def task_overview():
            try:
                stats = await get_overview_stats(company_filter)
                for key, value in stats.items():
                    if hasattr(snap, key):
                        setattr(snap, key, value)
            except Exception as e:
                LOG.error("Overview stats failed: %s", e)
                snap.notes.append(f"Overview stats failed: {e}")

        async def task_positions():
            # Round 12 (2026-05-24): switched from aggregate_open_positions
            # to aggregate_live_positions. Floor mini-table only renders
            # open + partial_exit anyway; pulling the 7d closed tail every
            # snapshot was wasted I/O. Historic context lives on the
            # /api/positions/historic endpoint, used by the Positions tab.
            try:
                snap.positions = await aggregate_live_positions(company_filter, limit=200)
            except Exception as e:
                LOG.error("Positions aggregation failed: %s", e)
                snap.notes.append(f"Positions failed: {e}")

        async def task_leaderboard():
            try:
                snap.leaderboard = await aggregate_leaderboard(company_filter)
            except Exception as e:
                LOG.error("Leaderboard aggregation failed: %s", e)
                snap.notes.append(f"Leaderboard failed: {e}")

        async def task_signals():
            try:
                snap.signals = await aggregate_signals(company_filter)
            except Exception as e:
                LOG.error("Signals aggregation failed: %s", e)
                snap.notes.append(f"Signals failed: {e}")

        async def task_interpretations():
            try:
                snap.interpretations = await aggregate_interpretations(company_filter)
            except Exception as e:
                LOG.error("Interpretations aggregation failed: %s", e)
                snap.notes.append(f"Interpretations failed: {e}")

        async def task_services():
            await self._load_services(snap)

        async def task_submissions():
            await self._load_submissions(snap)

        async def task_intents():
            await self._load_intents(snap)

        async def task_regime():
            await self._load_regime(snap)

        async def task_guardrails():
            await self._load_guardrails(snap)

        await asyncio.gather(
            task_overview(),
            task_positions(),
            task_leaderboard(),
            task_signals(),
            task_interpretations(),
            task_services(),
            task_submissions(),
            task_intents(),
            task_regime(),
            task_guardrails(),
        )

        return snap

    async def _load_services(self, snap: DashboardSnapshot) -> None:
        """Populate ``snap.services`` and the healthy/total counts.

        Liveness rules (Phase R, heartbeat-aware):
          * ``heartbeat`` row exists and ``status=='ok'`` and not stale → healthy
          * ``heartbeat`` row exists but stale or status!='ok'           → unhealthy
          * no ``heartbeat`` row + ``enabled_on_vps==True``              → healthy
            (the service is configured to run but does not yet emit
            heartbeats — treat as healthy until heartbeats are wired in)
          * no ``heartbeat`` row + ``enabled_on_vps==False``             → unhealthy

        ``services_total_count`` is the number of registered services.
        ``services_healthy_count`` follows the rules above.
        """
        if self.providers.services is None:
            return
        try:
            services = await self.providers.services.list_services()
            services = list(services or [])
        except Exception as e:
            LOG.error("Services aggregation failed: %s", e)
            snap.notes.append(f"Services failed: {e}")
            return

        # Best-effort heartbeat enrichment — failure must NOT poison
        # the snapshot; we degrade to enabled_on_vps as the liveness proxy.
        hb_map: Dict[str, Dict[str, Any]] = {}
        try:
            from shared.utils.db import DatabasePool

            pool = await DatabasePool.get_instance()
            heartbeats = await pool.fetch_all(
                "SELECT agent_id, last_run_at, last_status, "
                "expected_interval_seconds FROM public.cron_heartbeats"
            )
            now = datetime.now(timezone.utc)
            for hb in heartbeats:
                last_ts = hb.get("last_run_at")
                interval = hb.get("expected_interval_seconds") or 0
                entry: Dict[str, Any] = {
                    "status": hb.get("last_status"),
                    "last_seen_seconds": None,
                    "is_stale": True,
                }
                if last_ts is not None:
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=timezone.utc)
                    seconds_since = (now - last_ts).total_seconds()
                    is_stale = bool(interval) and seconds_since > (interval * 2.5)
                    entry = {
                        "status": hb.get("last_status"),
                        "last_seen_seconds": int(seconds_since),
                        "is_stale": is_stale,
                    }
                hb_map[hb["agent_id"]] = entry
        except Exception as e:
            LOG.warning("Services heartbeat enrichment skipped: %s", e)

        enriched: list = []
        healthy = 0
        for s in services:
            s_dict = dict(s) if not isinstance(s, dict) else dict(s)
            hb = hb_map.get(s_dict.get("name"))
            if hb is not None:
                s_dict["heartbeat"] = hb
                if not hb["is_stale"] and hb["status"] == "ok":
                    healthy += 1
            else:
                # No heartbeat row — fall back to the registry config flag.
                if bool(s_dict.get("enabled_on_vps")):
                    healthy += 1
            enriched.append(s_dict)

        snap.services = enriched
        snap.services_total_count = len(enriched)
        snap.services_healthy_count = healthy

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

    # Open P&L fix (2026-05-29): the strip used to count ONLY positions_current
    # (a single demo broker fill), so it showed "1 open position · +$0.00". Now
    # it sums the SAME live union the Positions tab + Floor show
    # (aggregate_live_positions = trader signals + paper-agent copies + broker
    # fills), so Open P&L reflects everything actually in play.
    try:
        live_rows = await aggregate_live_positions(company_filter, limit=500)
        total_open = len(live_rows)
        total_pnl = sum(float(r.get("unrealized_pnl_usd") or 0.0) for r in live_rows)
        agent_open = sum(1 for r in live_rows if r.get("_source") == "competition_trades")
        trader_open = total_open - agent_open
    except Exception as exc:
        LOG.warning("[stats] live aggregation failed, falling back: %s", exc)

    async with shared_pool.acquire() as shared_conn:
        if total_open == 0:
            # Fallback to the legacy positions_current count if the live
            # aggregation returned nothing (e.g. cold pool).
            try:
                pc_rows = await shared_conn.fetch(
                    "SELECT company_id, COALESCE(unrealised_pnl_usd, 0) AS pnl FROM positions_current"
                )
                for r in pc_rows:
                    if company_filter and company_filter != "all" and r["company_id"] != company_filter:
                        continue
                    total_open += 1
                    total_pnl += float(r["pnl"] or 0.0)
                    trader_open = total_open
            except Exception:
                pass

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

        # Phase 8 — dedup counter: how many positions were deduped in last 24h
        dedup_query = (
            "SELECT COUNT(*) FROM tracked_positions "
            "WHERE deduped_at >= NOW() - INTERVAL '24 hours'"
        )
        deduped_24h = await shared_conn.fetchval(dedup_query) or 0

        # Best Trader/Agent (2026-05-29): rank by live EQUITY (closed balance +
        # unrealized P&L), not return_pct — the user wants "highest equity".
        top_actor_name = None
        top_actor_score = 0.0
        try:
            top_row = await shared_conn.fetchrow(
                "SELECT agent_id, "
                "(COALESCE(equity_usd,0) + COALESCE(unrealized_pnl_usd,0)) AS live_equity "
                "FROM contest_participants "
                "ORDER BY live_equity DESC NULLS LAST LIMIT 1"
            )
            if top_row:
                top_actor_name = top_row["agent_id"]
                top_actor_score = float(top_row["live_equity"] or 0)
        except Exception:
            pass

    stats = {
        "signals_today_count": signals_today,
        "api_cost_today_usd": api_cost,
        "budget_limit_usd": 100.0,  # global daily budget (env-overridable)
        "budget_remaining_usd": round(max(0, 100.0 - api_cost), 2),
        "open_positions_count": total_open,
        "open_positions_unrealized_pnl": total_pnl,
        "open_positions_trader_count": trader_open,
        "open_positions_agent_count": agent_open,
        "ingest_depth": ingest_depth,
        "deduped_24h": deduped_24h,
        "top_actor_name": top_actor_name,
        "top_actor_score": top_actor_score,
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


def _normalise_position_row(d: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Normalise a position row into the dashboard's canonical shape.

    Slice 3 / Change 6 — every row served by aggregate_open_positions must
    carry the same set of keys regardless of whether it came from
    ``positions_current`` (live exchange ledger) or
    ``tracked_positions`` (interpretation/Surgeon ledger). Missing keys
    default to ``None`` (or sensible 0.0 for P&L).

    Args:
        d: Raw row mapping from the source table/view.
        source: Origin tag, e.g. ``positions_current``,
            ``tracked_positions_open``, ``tracked_positions_closed``.

    Returns:
        A new dict with the canonical keys populated.
    """
    def _f(v: Any) -> Optional[float]:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    if source == "positions_current":
        symbol = d.get("symbol")
        entry = _f(d.get("average_entry_price"))
        size = _f(d.get("quantity"))
        upnl = _f(d.get("unrealised_pnl_usd"))
        rpnl = _f(d.get("realized_pnl_usd"))
        rpnl_final = None
        opened_at = d.get("ts")
        closed_at = None
        status = "open"
        actor_id = d.get("account_id_external")
        signal_source = d.get("source")
        detection_method = None
        entry_price_source = None
        signal_interpretation_id = None
        trader_profile_id = None
        current_price = None
        pnl_pct = None
        stop_loss = None
        take_profit_1 = None
        take_profit_2 = None
        take_profit_3 = None
        distance_to_sl_pct = None
        distance_to_tp1_pct = None
        time_in_trade_minutes = None
        outcome = None
        exit_price = None
        exit_reason = None
        status_reason = None
        news_item_id = None
        price_updated_at = None
    elif source == "competition_trades":
        # Phase 4 (2026-05-29): paper-agent positions. One row per agent's open
        # copy. agent_id is the actor; sl_price/tp_price/allocated map onto the
        # canonical level/notional fields. current_price + unrealized P&L are
        # enriched downstream from the live price map.
        symbol = d.get("symbol")
        entry = _f(d.get("entry_price"))
        size = None
        upnl = _f(d.get("unrealized_pnl_usd"))  # filled by enrichment pass
        rpnl = None
        rpnl_final = None
        opened_at = d.get("entered_at")
        closed_at = d.get("exited_at")
        status = "open" if d.get("exited_at") is None else "closed"
        actor_id = d.get("agent_id")
        signal_source = "paper_agent"
        detection_method = None
        entry_price_source = None
        signal_interpretation_id = d.get("signal_interpretation_id")
        trader_profile_id = None
        current_price = _f(d.get("current_price"))
        pnl_pct = _f(d.get("pnl_pct"))
        stop_loss = _f(d.get("sl_price"))
        take_profit_1 = _f(d.get("tp_price"))
        take_profit_2 = None
        take_profit_3 = None
        distance_to_sl_pct = None
        distance_to_tp1_pct = None
        time_in_trade_minutes = None
        outcome = None
        exit_price = _f(d.get("exit_price"))
        exit_reason = d.get("exit_reason")
        status_reason = None
        news_item_id = None
        price_updated_at = d.get("price_updated_at")
    else:
        symbol = d.get("instrument_symbol_normalised") or d.get("instrument_symbol")
        entry = _f(d.get("entry_price"))
        size = _f(d.get("position_size") or d.get("quantity"))
        upnl = _f(d.get("unrealized_pnl_usd") or d.get("unrealised_pnl_usd"))
        rpnl = _f(d.get("realized_pnl_usd"))
        rpnl_final = _f(d.get("realized_pnl_usd_final"))
        opened_at = d.get("signal_timestamp") or d.get("created_at") or d.get("opened_at")
        closed_at = d.get("closed_at")
        status = d.get("status") or ("closed" if closed_at else "open")
        actor_id = d.get("actor_id")
        signal_source = d.get("signal_source")
        detection_method = d.get("detection_method")
        entry_price_source = d.get("entry_price_source")
        signal_interpretation_id = d.get("signal_interpretation_id")
        trader_profile_id = d.get("trader_profile_id")
        current_price = _f(d.get("current_price"))
        pnl_pct = _f(d.get("unrealized_pnl_pct")) or _f(d.get("realized_pnl_pct"))
        # Round 11 (2026-05-24): Live tab needs SL/TP + distances; Historic
        # tab needs outcome + exit. Expose them all on the canonical row so
        # the frontend can branch on status without extra API calls.
        stop_loss = _f(d.get("stop_loss"))
        take_profit_1 = _f(d.get("take_profit_1"))
        take_profit_2 = _f(d.get("take_profit_2"))
        take_profit_3 = _f(d.get("take_profit_3"))
        distance_to_sl_pct = _f(d.get("distance_to_sl_pct"))
        distance_to_tp1_pct = _f(d.get("distance_to_tp1_pct"))
        time_in_trade_minutes = _f(d.get("time_in_trade_minutes"))
        outcome = d.get("outcome")
        exit_price = _f(d.get("exit_price"))
        exit_reason = d.get("exit_reason")
        status_reason = d.get("status_reason")
        news_item_id = d.get("news_item_id")
        price_updated_at = d.get("price_updated_at")

    pnl_usd = upnl if upnl is not None else (rpnl if rpnl is not None else 0.0)

    # Phase 4 (2026-05-29): origin label so the Live tab can show WHICH actor /
    # account each row belongs to (paper agent, broker account, or signal).
    _adapter = d.get("adapter") or d.get("exchange") or d.get("exchange_name")
    _account = d.get("account_id_external") or d.get("exchange_account")
    if source == "competition_trades":
        origin = d.get("agent_id") or "paper-agent"
        origin_kind = "paper"
    elif source == "positions_current":
        origin = (f"{_adapter or 'broker'} · {_account}" if _account else (_adapter or "broker"))
        origin_kind = "broker"
    else:
        origin = "signal"
        origin_kind = "signal"

    return {
        "id": d.get("id"),
        "origin": origin,
        "origin_kind": origin_kind,
        "exchange": _adapter,
        "company_id": d.get("company_id"),
        "_company": d.get("company_id"),
        "instrument_symbol": symbol,
        "direction": d.get("direction"),
        "position_size": size,
        "entry_price": entry,
        "current_price": current_price,
        "stop_loss": stop_loss,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "take_profit_3": take_profit_3,
        "distance_to_sl_pct": distance_to_sl_pct,
        "distance_to_tp1_pct": distance_to_tp1_pct,
        "time_in_trade_minutes": time_in_trade_minutes,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "outcome": outcome,
        "notional_usd": _f(d.get("notional_usd")),
        "unrealized_pnl_usd": upnl if upnl is not None else 0.0,
        "realized_pnl_usd": rpnl if rpnl is not None else 0.0,
        "realized_pnl_usd_final": rpnl_final,
        "pnl_usd": pnl_usd if pnl_usd is not None else 0.0,
        "pnl_pct": pnl_pct,
        "leverage": d.get("leverage"),
        "status": status,
        "status_reason": status_reason,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "price_updated_at": price_updated_at,
        "signal_timestamp": opened_at,
        "actor_id": actor_id,
        "signal_source": signal_source,
        "detection_method": detection_method,
        "entry_price_source": entry_price_source,
        "signal_interpretation_id": signal_interpretation_id,
        "trader_profile_id": trader_profile_id,
        "news_item_id": news_item_id,
        "actor_display": None,  # populated by aggregate_open_positions after batch lookup
        "actor_handle": None,   # populated by aggregate_open_positions after batch lookup
        "_source": source,
    }


def _position_dedupe_key(row: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    """Build the de-dupe tuple ``(company_id, symbol, direction)`` for a row."""
    sym = row.get("instrument_symbol")
    if isinstance(sym, str):
        sym = sym.strip().upper()
    direction = row.get("direction")
    if isinstance(direction, str):
        direction = direction.strip().lower()
    return (row.get("company_id"), sym, direction)


async def aggregate_open_positions(company_filter: str | None = None) -> List[dict]:
    """**DEPRECATED (Round 12, 2026-05-24).**

    Use ``aggregate_live_positions`` for live-only data (open + partial_exit +
    broker fills) or ``aggregate_historic_positions`` for closed/expired/
    cancelled/invalidated rows with pagination. The legacy mixed payload
    (live + 7d closed tail) is kept here only for backward compatibility
    with out-of-tree consumers and unit tests still mocking this name.

    The dashboard snapshot builder (``shared/dashboard/snapshot.py:91``)
    has been switched to call ``aggregate_live_positions`` directly so
    the Floor mini-table no longer pays the cost of the closed tail it
    never showed anyway.

    Slice 3 / Change 2 — always queries BOTH ``positions_current`` (live
    exchange ledger) AND ``tracked_positions`` (interpretation + Surgeon
    forward bridge). Rows are tagged with ``_source`` then de-duped on
    ``(company_id, symbol, direction)`` preferring ``positions_current``
    but merging in ``signal_interpretation_id`` / ``trader_profile_id``
    from the matching tracked_positions row when available.

    Also includes recently-closed (≤7 days) tracked_positions so the UI
    can show context. Sort: open first, then opened_at DESC. Cap 200.

    ``company_filter='all' or None`` aggregates every company; otherwise
    restricts.
    """
    cache_key = ("positions", company_filter)
    now_mono = time.monotonic()
    if cache_key in _SNAPSHOT_CACHE:
        ts, data = _SNAPSHOT_CACHE[cache_key]
        if now_mono - ts < _CACHE_TTL:
            return data

    from shared.utils.db import get_shared_pool
    shared_pool = await get_shared_pool()

    pc_rows: List[Dict[str, Any]] = []
    tp_open_rows: List[Dict[str, Any]] = []
    tp_closed_rows: List[Dict[str, Any]] = []

    async with shared_pool.acquire() as conn:
        # ---- positions_current (live exchange ledger) ------------------
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
            pc_sql += " ORDER BY ts DESC LIMIT 200"
            pc = await conn.fetch(pc_sql, *pc_params)
            for r in pc:
                pc_rows.append(_normalise_position_row(dict(r), "positions_current"))
        except Exception as exc:
            LOG.warning("positions_current read failed: %s", exc)

        # ---- tracked_positions: open ----------------------------------
        try:
            tp_open_params: List[Any] = []
            tp_open_sql = (
                "SELECT * FROM tracked_positions WHERE status IN ('open', 'partial_exit')"
            )
            if company_filter and company_filter != "all":
                tp_open_sql += " AND company_id = $1"
                tp_open_params.append(company_filter)
            tp_open_sql += " ORDER BY signal_timestamp DESC NULLS LAST LIMIT 200"
            tpo = await conn.fetch(tp_open_sql, *tp_open_params)
            for r in tpo:
                tp_open_rows.append(_normalise_position_row(dict(r), "tracked_positions_open"))
        except Exception as exc:
            LOG.warning("tracked_positions open read failed: %s", exc)

        # ---- tracked_positions: closed (last 7 days) ------------------
        # Round 11 (2026-05-24): explicit terminal-status enum instead of the
        # NOT IN ('open','pending') sieve which was leaking partial_exit rows
        # into the closed bucket and causing double-listing alongside the
        # open bucket. partial_exit is a LIVE state and belongs in tp_open_rows.
        try:
            tp_closed_params: List[Any] = []
            tp_closed_sql = (
                "SELECT * FROM tracked_positions "
                "WHERE status IN ('closed', 'expired', 'cancelled', 'invalidated') "
                "  AND COALESCE(closed_at, updated_at, created_at) "
                "      >= (NOW() AT TIME ZONE 'UTC' - INTERVAL '7 days')"
            )
            if company_filter and company_filter != "all":
                tp_closed_sql += " AND company_id = $1"
                tp_closed_params.append(company_filter)
            tp_closed_sql += (
                " ORDER BY COALESCE(closed_at, updated_at, created_at) DESC LIMIT 200"
            )
            tpc = await conn.fetch(tp_closed_sql, *tp_closed_params)
            for r in tpc:
                tp_closed_rows.append(_normalise_position_row(dict(r), "tracked_positions_closed"))
        except Exception as exc:
            LOG.warning("tracked_positions closed read failed: %s", exc)

    # ---- De-dupe (company, symbol, direction) preferring positions_current
    by_key: Dict[Tuple[Any, Any, Any], Dict[str, Any]] = {}
    for row in pc_rows:
        by_key[_position_dedupe_key(row)] = row

    for row in tp_open_rows:
        key = _position_dedupe_key(row)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = row
            continue
        # Merge tracked_positions provenance into the live row.
        for fld in (
            "signal_interpretation_id",
            "trader_profile_id",
            "detection_method",
            "entry_price_source",
            "signal_source",
        ):
            if not existing.get(fld) and row.get(fld) is not None:
                existing[fld] = row.get(fld)

    # Closed rows: never collide with open rows on the same key (status
    # differs), but de-dupe among themselves on (company, symbol, direction,
    # closed_at) by simple insertion order (already sorted by recency).
    seen_closed: Dict[Tuple[Any, Any, Any, Any], None] = {}
    closed_unique: List[Dict[str, Any]] = []
    for row in tp_closed_rows:
        ck = (
            row.get("company_id"),
            (row.get("instrument_symbol") or "").upper() if isinstance(row.get("instrument_symbol"), str) else row.get("instrument_symbol"),
            (row.get("direction") or "").lower() if isinstance(row.get("direction"), str) else row.get("direction"),
            row.get("closed_at"),
        )
        if ck in seen_closed:
            continue
        seen_closed[ck] = None
        closed_unique.append(row)

    # ---- Sort: open first, then opened_at DESC ----------------------
    open_rows = list(by_key.values())

    def _opened_key(r: Dict[str, Any]) -> Tuple[int, float]:
        ts = r.get("opened_at")
        if isinstance(ts, datetime):
            try:
                return (0, -ts.timestamp())
            except Exception:
                return (0, 0.0)
        return (1, 0.0)

    open_rows.sort(key=_opened_key)
    closed_unique.sort(key=_opened_key)

    rows: List[Dict[str, Any]] = (open_rows + closed_unique)[:200]

    # ---- Enrich with trader_profiles display/handle -----------------
    tp_ids = [r["trader_profile_id"] for r in rows if r.get("trader_profile_id")]
    if tp_ids:
        try:
            async with shared_pool.acquire() as conn2:
                profile_rows = await conn2.fetch(
                    "SELECT id, handle_normalized, display_name "
                    "FROM trader_profiles WHERE id = ANY($1::bigint[])",
                    tp_ids,
                )
                profiles = {p["id"]: dict(p) for p in profile_rows}
                for row in rows:
                    tp_id = row.get("trader_profile_id")
                    if tp_id and tp_id in profiles:
                        prof = profiles[tp_id]
                        row["actor_display"] = prof.get("display_name")
                        row["actor_handle"] = prof.get("handle_normalized")
        except Exception as exc:
            LOG.warning("trader_profiles batch lookup failed: %s", exc)

    _SNAPSHOT_CACHE[cache_key] = (now_mono, rows)
    return rows


# ---------------------------------------------------------------------------
# Round 11 (2026-05-24): Live / Historic position split
# ---------------------------------------------------------------------------
# aggregate_open_positions above is kept for backward compatibility with any
# existing callers (and the legacy /api/positions endpoint). The new
# aggregators below produce CLEAN buckets:
#
#   aggregate_live_positions      — open + partial_exit + positions_current
#                                    (broker fills). No historic mix-in.
#   aggregate_historic_positions  — closed + expired + cancelled +
#                                    invalidated. Keyset-paginated. Default
#                                    window 30 days; configurable.
#
# Together they replace the all-in-one stream so the dashboard can render
# Live vs Historic as two distinct sub-tabs with different column logic.
# ---------------------------------------------------------------------------

# Status enums grouped by lifecycle phase. Single source of truth — keep in
# sync with shared/migration/tickles_shared_pg.sql.
LIVE_TRACKED_STATUSES: Tuple[str, ...] = ("open", "partial_exit")
HISTORIC_TRACKED_STATUSES: Tuple[str, ...] = (
    "closed", "expired", "cancelled", "invalidated",
)


async def aggregate_live_positions(
    company_filter: str | None = None,
    limit: int = 200,
) -> List[dict]:
    """Return all currently-LIVE positions (open + partial_exit + broker fills).

    Live = anything actively in market. Pending rows belong on Signals
    Watch (they have no fill yet); closed/expired/cancelled rows belong
    on the Historic tab. Broker fills from ``positions_current`` are
    always included so the user sees real exchange exposure even when
    we have no upstream signal.

    De-dup logic: ``positions_current`` wins on ``(company, symbol,
    direction)``; matching ``tracked_positions`` provenance fields are
    merged in.

    Args:
        company_filter: ``"all"``/``None`` for cross-company; otherwise
            short_name like ``"rubicon"``.
        limit: Total row cap before enrichment. 200 is generous for
            currently-realistic position counts.

    Returns:
        List of normalized position dicts sorted by ``opened_at DESC``.
    """
    from shared.utils.db import get_shared_pool
    shared_pool = await get_shared_pool()

    pc_rows: List[Dict[str, Any]] = []
    tp_open_rows: List[Dict[str, Any]] = []
    ct_rows: List[Dict[str, Any]] = []

    async with shared_pool.acquire() as conn:
        # ---- positions_current ------------------------------------------
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
            pc_sql += " ORDER BY ts DESC LIMIT 200"
            pc = await conn.fetch(pc_sql, *pc_params)
            for r in pc:
                pc_rows.append(_normalise_position_row(dict(r), "positions_current"))
        except Exception as exc:
            LOG.warning("[live] positions_current read failed: %s", exc)

        # ---- competition_trades: open paper-agent positions -------------
        # Phase 4 (2026-05-29): the 7 paper agents' open copies live HERE, not in
        # tracked_positions / positions_current. exited_at IS NULL == open. We
        # join tracked_positions for a live current_price (the monitor keeps it
        # fresh) and surface one row PER AGENT (no dedupe) so "everything in play
        # across all agents" is literally true.
        try:
            ct_params: List[Any] = []
            ct_sql = (
                "SELECT ct.id, ct.contest_id, ct.agent_id, ct.company_id, ct.symbol, "
                "ct.direction, ct.entry_price, ct.exit_price, ct.sl_price, ct.tp_price, "
                "ct.allocated AS notional_usd, ct.leverage, ct.pnl, ct.fees, ct.exit_reason, "
                "ct.entered_at, ct.exited_at, ct.tracked_position_id, ct.signal_interpretation_id, "
                "ct.exchange_name, ct.exchange_account, "
                "tp.current_price, tp.price_updated_at "
                "FROM competition_trades ct "
                "LEFT JOIN tracked_positions tp ON tp.id = ct.tracked_position_id "
                "WHERE ct.exited_at IS NULL"
            )
            if company_filter and company_filter != "all":
                ct_sql += " AND ct.company_id = $1"
                ct_params.append(company_filter)
            ct_sql += " ORDER BY ct.entered_at DESC NULLS LAST LIMIT 300"
            ct = await conn.fetch(ct_sql, *ct_params)
            for r in ct:
                ct_rows.append(_normalise_position_row(dict(r), "competition_trades"))
        except Exception as exc:
            LOG.warning("[live] competition_trades read failed: %s", exc)

        # ---- tracked_positions: open + partial_exit ---------------------
        try:
            tp_params: List[Any] = []
            tp_sql = (
                "SELECT * FROM tracked_positions "
                "WHERE status = ANY($1::text[])"
            )
            tp_params.append(list(LIVE_TRACKED_STATUSES))
            if company_filter and company_filter != "all":
                tp_sql += " AND company_id = $2"
                tp_params.append(company_filter)
            tp_sql += " ORDER BY signal_timestamp DESC NULLS LAST LIMIT 200"
            tpo = await conn.fetch(tp_sql, *tp_params)
            for r in tpo:
                tp_open_rows.append(_normalise_position_row(dict(r), "tracked_positions_open"))
        except Exception as exc:
            LOG.warning("[live] tracked_positions read failed: %s", exc)

    # De-dupe: positions_current wins on (company, symbol, direction)
    by_key: Dict[Tuple[Any, Any, Any], Dict[str, Any]] = {}
    for row in pc_rows:
        by_key[_position_dedupe_key(row)] = row

    for row in tp_open_rows:
        key = _position_dedupe_key(row)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = row
            continue
        # Merge provenance from the shadow row into the live row so the
        # broker-fill view still shows trader handle + signal id.
        for fld in (
            "signal_interpretation_id", "trader_profile_id", "detection_method",
            "entry_price_source", "signal_source", "stop_loss", "take_profit_1",
            "take_profit_2", "take_profit_3", "news_item_id",
        ):
            if not existing.get(fld) and row.get(fld) is not None:
                existing[fld] = row.get(fld)

    # Phase 4 (2026-05-29): paper-agent rows are NOT de-duped — every agent's
    # open copy is its own row. Enrich move% + $ P&L from entry/current.
    for row in ct_rows:
        if not row.get("actor_display"):
            row["actor_display"] = row.get("actor_id")
        ent = row.get("entry_price")
        cur = row.get("current_price")
        lev = float(row.get("leverage") or 1.0)
        alloc = row.get("notional_usd")  # = competition_trades.allocated
        if ent and cur and ent != 0:
            sign = 1.0 if str(row.get("direction") or "").lower() == "long" else -1.0
            move = (cur - ent) / ent * sign
            row["pnl_pct"] = move
            if alloc:
                # notional = allocated × leverage matches the copy-trade monitor's
                # own per-agent unrealized figure (verified vs contest_participants).
                notional = float(alloc) * lev
                row["notional_usd"] = notional
                row["unrealized_pnl_usd"] = move * notional
                row["pnl_usd"] = row["unrealized_pnl_usd"]

    rows = list(by_key.values()) + ct_rows

    def _opened_key(r: Dict[str, Any]) -> Tuple[int, float]:
        ts = r.get("opened_at")
        if isinstance(ts, datetime):
            try:
                return (0, -ts.timestamp())
            except Exception:
                return (0, 0.0)
        return (1, 0.0)

    rows.sort(key=_opened_key)
    rows = rows[:limit]

    # Enrich with trader_profiles
    tp_ids = [r["trader_profile_id"] for r in rows if r.get("trader_profile_id")]
    if tp_ids:
        try:
            async with shared_pool.acquire() as conn2:
                profile_rows = await conn2.fetch(
                    "SELECT id, handle_normalized, display_name "
                    "FROM trader_profiles WHERE id = ANY($1::bigint[])",
                    tp_ids,
                )
                profiles = {p["id"]: dict(p) for p in profile_rows}
                for row in rows:
                    tp_id = row.get("trader_profile_id")
                    if tp_id and tp_id in profiles:
                        prof = profiles[tp_id]
                        row["actor_display"] = prof.get("display_name")
                        row["actor_handle"] = prof.get("handle_normalized")
        except Exception as exc:
            LOG.warning("[live] trader_profiles batch lookup failed: %s", exc)

    return rows


async def aggregate_historic_positions(
    company_filter: str | None = None,
    since_days: int = 30,
    cursor_closed_at: Optional[datetime] = None,
    cursor_id: Optional[int] = None,
    limit: int = 50,
    status_filter: Optional[str] = None,
    outcome_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one paginated page of historic (terminal) tracked_positions.

    Keyset pagination on ``(close_ts DESC, id DESC)`` for stable infinite
    scroll. Default window 30 days; pass ``since_days=0`` to disable
    (return all history).

    Args:
        company_filter: ``"all"``/``None`` for cross-company.
        since_days: Look-back window in days. ``0`` disables the filter.
        cursor_closed_at: Page cursor timestamp; pass the ``next_cursor``
            from the previous response to fetch the next page.
        cursor_id: Page cursor id (paired with cursor_closed_at).
        limit: Page size; capped at 200.
        status_filter: Optional single-status filter (e.g. ``"closed"``,
            ``"cancelled"``). When ``None`` all 4 terminal statuses are
            returned.
        outcome_filter: Optional outcome filter (``"tp1_hit"``, ``"sl_hit"``,
            ``"expired"``).

    Returns:
        ``{"rows": [...], "next_cursor": {"closed_at": str, "id": int}|None,
          "has_more": bool, "page_size": int}``
    """
    from shared.utils.db import get_shared_pool

    page_size = max(1, min(int(limit), 200))
    fetch_size = page_size + 1  # one extra for has_more detection

    shared_pool = await get_shared_pool()

    where_parts = ["status = ANY($1::text[])"]
    params: List[Any] = []
    statuses = (
        [status_filter] if status_filter and status_filter in HISTORIC_TRACKED_STATUSES
        else list(HISTORIC_TRACKED_STATUSES)
    )
    params.append(statuses)
    arg_idx = 2

    if company_filter and company_filter != "all":
        where_parts.append(f"company_id = ${arg_idx}")
        params.append(company_filter)
        arg_idx += 1

    if since_days and since_days > 0:
        where_parts.append(
            "COALESCE(closed_at, updated_at, created_at) "
            f">= (NOW() AT TIME ZONE 'UTC' - INTERVAL '{int(since_days)} days')"
        )

    if cursor_closed_at is not None and cursor_id is not None:
        where_parts.append(
            f"(COALESCE(closed_at, updated_at, created_at), id) "
            f"< (${arg_idx}, ${arg_idx + 1})"
        )
        params.extend([cursor_closed_at, int(cursor_id)])
        arg_idx += 2

    if outcome_filter:
        where_parts.append(f"outcome = ${arg_idx}")
        params.append(outcome_filter)
        arg_idx += 1

    sql = (
        "SELECT * FROM tracked_positions "
        f"WHERE {' AND '.join(where_parts)} "
        "ORDER BY COALESCE(closed_at, updated_at, created_at) DESC, id DESC "
        f"LIMIT {fetch_size}"
    )

    rows: List[Dict[str, Any]] = []
    raw: List[Any] = []
    try:
        async with shared_pool.acquire() as conn:
            raw = await conn.fetch(sql, *params)
    except Exception as exc:
        LOG.warning("[historic] read failed: %s", exc)
        return {"rows": [], "next_cursor": None, "has_more": False, "page_size": page_size}

    has_more = len(raw) > page_size
    raw = raw[:page_size]

    for r in raw:
        rows.append(_normalise_position_row(dict(r), "tracked_positions_closed"))

    # Enrich with trader_profiles
    tp_ids = [r["trader_profile_id"] for r in rows if r.get("trader_profile_id")]
    if tp_ids:
        try:
            async with shared_pool.acquire() as conn2:
                profile_rows = await conn2.fetch(
                    "SELECT id, handle_normalized, display_name "
                    "FROM trader_profiles WHERE id = ANY($1::bigint[])",
                    tp_ids,
                )
                profiles = {p["id"]: dict(p) for p in profile_rows}
                for row in rows:
                    tp_id = row.get("trader_profile_id")
                    if tp_id and tp_id in profiles:
                        prof = profiles[tp_id]
                        row["actor_display"] = prof.get("display_name")
                        row["actor_handle"] = prof.get("handle_normalized")
        except Exception as exc:
            LOG.warning("[historic] trader_profiles batch lookup failed: %s", exc)

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        cursor_ts = last.get("closed_at") or last.get("opened_at")
        next_cursor = {
            "closed_at": cursor_ts.isoformat() if isinstance(cursor_ts, datetime) else None,
            "id": last.get("id"),
        }

    return {
        "rows": rows,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "page_size": page_size,
    }


def _coerce_signal_level_value(value: Any) -> Optional[float]:
    """Coerce a heterogeneous level value to a finite positive float.

    Accepts ``Decimal``, ``int``, ``float``, or string. Strings have
    non-numeric characters stripped before parsing. Returns ``None``
    when the value cannot be expressed as a finite positive number.

    Args:
        value: Raw value from a ``signal_interpretations`` column or
            from the ``llm_levels`` JSONB.

    Returns:
        ``float`` or ``None``.
    """
    try:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            num = float(value)
        elif isinstance(value, str):
            # Try float() first — handles scientific notation ("9.966e-05")
            try:
                num = float(value)
            except (ValueError, TypeError):
                import re as _re
                cleaned = _re.sub(r"[^0-9.\-]", "", value)
                if not cleaned or cleaned in ("-", ".", "-."):
                    return None
                try:
                    num = float(cleaned)
                except (ValueError, TypeError):
                    return None
        else:
            try:
                num = float(value)
            except (TypeError, ValueError):
                return None
        if num != num or num in (float("inf"), float("-inf")):
            return None
        if num <= 0:
            return None
        return num
    except (TypeError, ValueError):
        return None


def _coalesce_signal_levels(row: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Coalesce dedicated level columns with the ``llm_levels`` JSONB fallback.

    The Phase Z migration added denormalised NUMERIC columns
    (``entry_price``, ``stop_loss``, ``take_profit_1`` .. ``take_profit_6``)
    on ``signal_interpretations`` for fast Signals-tab rendering and
    live-vs-signal tracking. Old rows that pre-date the backfill may
    still rely on ``llm_levels`` JSONB. This helper prefers the
    dedicated column and falls back to JSONB so the Signals tab works
    on both old and new rows.

    Args:
        row: Raw row dict from ``aggregate_signals`` with ``si.*``
            columns plus the JSONB ``llm_levels``.

    Returns:
        A dict with stable keys (``entry``, ``stop_loss``,
        ``take_profit_1``..``take_profit_6``) — each ``Optional[float]``.
    """
    raw_jsonb = row.get("llm_levels")
    parsed: Dict[str, Any] = {}
    if isinstance(raw_jsonb, dict):
        parsed = raw_jsonb
    elif isinstance(raw_jsonb, str) and raw_jsonb:
        try:
            import json as _json
            decoded = _json.loads(raw_jsonb)
            if isinstance(decoded, dict):
                parsed = decoded
        except (ValueError, TypeError):
            parsed = {}

    out: Dict[str, Optional[float]] = {
        "entry": _coerce_signal_level_value(row.get("entry_price"))
                 or _coerce_signal_level_value(parsed.get("entry")),
        "stop_loss": _coerce_signal_level_value(row.get("stop_loss"))
                     or _coerce_signal_level_value(parsed.get("stop_loss")),
    }
    for n in range(1, 7):
        col_key = f"take_profit_{n}"
        val = _coerce_signal_level_value(row.get(col_key)) or _coerce_signal_level_value(parsed.get(col_key))
        if n == 1 and val is None:
            val = _coerce_signal_level_value(parsed.get("take_profit"))
        out[col_key] = val
    return out


async def _aggregate_signal_rows(
    cache_key_prefix: str,
    company_filter: str | None = None,
    limit: int = 50,
) -> List[dict]:
    """Shared implementation for aggregate_signals and aggregate_interpretations.

    Both functions share identical SQL, row processing, caching, and level
    coalescing logic. This helper eliminates the duplication.

    Args:
        cache_key_prefix: Distinct cache key prefix ("signals" or "interpretations").
        company_filter: Optional company short-name; ``None`` or ``"all"`` returns all rows.
        limit: Maximum number of rows to return.

    Returns:
        List of signal/interpretation dicts ordered by ``created_at`` DESC.
    """
    cache_key = (cache_key_prefix, company_filter, limit)
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
                "       ni.content AS news_content, "
                "       ni.source AS news_source, "
                "       ni.author AS news_author, "
                "       ni.channel_name AS news_channel_name, "
                "       ni.metadata AS news_metadata, "
                "       tp.handle_raw AS trader_handle_raw, "
                "       tp.handle_normalized AS trader_handle_normalized, "
                "       tp.display_name AS trader_display_name, "
                "       tp.platform AS trader_platform, "
                "       tp.trader_type AS trader_type "
                "FROM signal_interpretations si "
                "LEFT JOIN media_items mi ON mi.id = si.media_item_id "
                "LEFT JOIN news_items ni ON ni.id = si.news_item_id "
                "LEFT JOIN trader_profiles tp ON tp.id = si.trader_profile_id "
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
                # Synthesise actor_id for the Signals/Interpretations tables:
                # prefer trader handle_raw, fall back to display_name, else None.
                d["actor_id"] = (
                    d.get("trader_handle_raw")
                    or d.get("trader_display_name")
                    or d.get("news_author")
                )
                d["actor_platform"] = d.get("trader_platform")
                d["actor_type"] = d.get("trader_type")
                # Build a stable media URL the front-end can <img src="..."> directly.
                # Use a relative path (no leading slash) so the URL works under both
                # the root mount ("/") and the "/dashboard/" mount.
                if d.get("media_local_path") and d.get("media_item_id") is not None:
                    d["media_url"] = f"api/media/{d['media_item_id']}"
                elif d.get("media_source_url"):
                    d["media_url"] = f"api/media/proxy?url={quote(d['media_source_url'], safe='')}"
                else:
                    d["media_url"] = None
                # Phase Z — coalesce dedicated NUMERIC columns with the
                # canonical ``llm_levels`` JSONB. Old rows written before
                # the Phase Z migration backfill ran may have NULL in the
                # dedicated columns but populated JSONB; new rows (and
                # backfilled rows) have both. The coalesced ``levels``
                # block is what the Signals tab consumes directly.
                d["levels"] = _coalesce_signal_levels(d)
                rows.append(d)
    except Exception as e:
        LOG.error("%s aggregation failed: %s", cache_key_prefix, e)

    _SNAPSHOT_CACHE[cache_key] = (now, rows)
    return rows


async def aggregate_signals(company_filter: str | None = None, limit: int = 50) -> List[dict]:
    """Fetch recent signals from shared signal_interpretations + pending tracked_positions.

    Phase 3 — pending tracked_positions (status='pending') are normalized into the
    same shape as interpreted signals and merged into the result set. Pending positions
    are tagged ``_source='pending_position'`` and sorted before interpreted signals
    by ``distance_to_entry_pct ASC NULLS LAST``.

    Live prices are NOT computed in this phase — ``distance_to_entry_pct`` is a
    placeholder that will be populated in Phase 4/7.

    Args:
        company_filter: Optional company short-name; ``None`` or ``"all"`` returns all rows.
        limit: Maximum number of rows to return.

    Returns:
        List of signal dicts (interpreted + pending) ordered by proximity to entry.
    """
    # Fetch interpreted signals from signal_interpretations
    interpreted = await _aggregate_signal_rows("signals", company_filter, limit)

    # Fetch pending tracked_positions and normalize into signals shape
    pending = await _aggregate_pending_positions(company_filter, limit)

    # Merge: pending positions first (distance_to_entry_pct=0.0), then
    # interpreted signals (no distance, i.e. NULL → sorted last).
    combined = pending + interpreted

    # Round 11 (2026-05-24): closest-to-entry must use ABSOLUTE distance, not
    # signed. Previously a short trading 16% above entry sorted ahead of a long
    # trading 1% below entry because -16 < -1 numerically — opposite of what
    # "closest" means. Recency tiebreaker switched to signal_timestamp DESC,
    # then created_at DESC, to match handle_entry_radar.
    def _sort_key(row: dict) -> tuple:
        dist = row.get("distance_to_entry_pct")
        if dist is not None:
            try:
                return (0, abs(float(dist)))
            except (TypeError, ValueError):
                pass
        # Interpreted signals (no distance) sort by recency, newest first.
        ts = row.get("signal_timestamp") or row.get("created_at")
        if isinstance(ts, datetime):
            return (1, -ts.timestamp())
        return (1, 0)

    combined.sort(key=_sort_key)
    return combined[:limit]


async def _aggregate_pending_positions(
    company_filter: str | None = None, limit: int = 50
) -> List[dict]:
    """Fetch pending tracked_positions and normalize into a signals-compatible shape.

    Queries ``public.tracked_positions WHERE status='pending'``, joins
    ``trader_profiles`` for display-name attribution, and normalizes each
    row into the same dict shape as interpreted signals so that
    ``renderSignals()`` in the frontend handles both identically.

    The ``levels`` dict uses: entry=entry_price, stop_loss, take_profit_1
    from the tracked_positions row (coerced via ``_coalesce_signal_levels``).

    ``distance_to_entry_pct`` is set to 0.0 as a placeholder — it will be
    replaced with a live-price-derived value in Phase 4/7.

    Args:
        company_filter: Optional company short-name; ``None`` or ``"all"``
            returns rows across all companies.
        limit: Maximum number of rows to return.

    Returns:
        List of normalized dicts, each tagged ``_source='pending_position'``.
    """
    from shared.utils.db import get_shared_pool

    rows: List[dict] = []
    try:
        shared_pool = await get_shared_pool()
        async with shared_pool.acquire() as conn:
            base_sql = (
                "SELECT tp.*, "
                "       tprof.handle_raw AS trader_handle_raw, "
                "       tprof.handle_normalized AS trader_handle_normalized, "
                "       tprof.display_name AS trader_display_name, "
                "       tprof.platform AS trader_platform, "
                "       tprof.trader_type AS trader_type "
                "FROM public.tracked_positions tp "
                "LEFT JOIN public.trader_profiles tprof ON tprof.id = tp.trader_profile_id "
                "WHERE tp.status = 'pending' "
            )
            if company_filter and company_filter != "all":
                cr = await conn.fetch(
                    base_sql + "AND tp.company_id = $1 "
                               "ORDER BY tp.entry_price ASC NULLS LAST LIMIT $2",
                    company_filter, limit,
                )
            else:
                cr = await conn.fetch(
                    base_sql + "ORDER BY tp.entry_price ASC NULLS LAST LIMIT $1",
                    limit,
                )
            # Phase 8 — batch-fetch latest candle prices for all pending symbols
            symbols = list({r.get("instrument_symbol") for r in cr if r.get("entry_price") and not r.get("current_price")})
            price_map: dict[str, float] = {}
            if symbols:
                try:
                    px_rows = await conn.fetch(
                        """
                        SELECT DISTINCT ON (i.symbol) i.symbol, c.close
                        FROM public.candles c
                        JOIN public.instruments i ON i.id = c.instrument_id
                        WHERE i.symbol = ANY($1)
                          AND i.is_active = TRUE
                          AND c.timeframe = '1m'
                        ORDER BY i.symbol, c.timestamp DESC
                        """,
                        symbols,
                    )
                    for px in px_rows:
                        price_map[px["symbol"]] = float(px["close"])
                except Exception as exc:
                    logger.debug("Batch candle lookup for pending signals failed: %s", exc)

                # 2026-05-22 — Live-CCXT fallback for symbols the candle DB
                # doesn't have yet (e.g. freshly-projected instruments whose
                # candle daemon catch-up is still in flight, or symbols on
                # exchanges not currently being collected). Probes the cross-
                # exchange chain (bybit / blofin / bitget / binance / okx) in
                # PARALLEL with one shared CCXT client per exchange, a 30s
                # in-memory TTL cache, and a 12s total wall-clock budget.
                # First subsequent call within 30s is satisfied from cache.
                #
                # See: shared/market_data/live_price_cache.py
                # Rollback: comment out this block to restore the pre-fix
                # behaviour of "no fallback → +0.00% on missing symbols".
                missing = [s for s in symbols if s and s not in price_map]
                if missing:
                    try:
                        from shared.market_data.live_price_cache import fetch_many
                        LOG.info(
                            "Live-price fallback: probing %d missing symbol(s) "
                            "(candle DB had no recent bar)", len(missing),
                        )
                        live_prices = await fetch_many(missing)
                        for sym, px in live_prices.items():
                            price_map[sym] = float(px)
                        if live_prices:
                            LOG.info(
                                "Live-price fallback resolved %d of %d missing symbols",
                                len(live_prices), len(missing),
                            )
                    except Exception as exc:
                        # Never let a fallback failure break the snapshot.
                        LOG.warning("Live-price fallback failed: %s", exc)

            for r in cr:
                d = dict(r)
                if "company_id" in d:
                    d["_company"] = d.get("company_id")

                # Normalize instrument symbol — prefer the normalised form
                sym = d.get("instrument_symbol_normalised") or d.get("instrument_symbol")
                d["instrument_symbol"] = sym

                # Synthesize actor_id — prefer trader handle, fall back to raw actor_id
                d["actor_id"] = (
                    d.get("trader_handle_raw")
                    or d.get("trader_display_name")
                    or d.get("actor_id")
                )
                d["actor_platform"] = d.get("trader_platform")
                d["actor_type"] = d.get("trader_type")

                # Build levels dict using the same coalescer as interpreted signals
                d["levels"] = _coalesce_signal_levels(d)

                # Fields that exist on signal_interpretations but not on
                # tracked_positions — default to None so renderSignals()
                # shows '—' placeholders.
                d["consensus_direction"] = d.get("direction")
                d["consensus_confidence"] = None
                d["consensus_method"] = None
                d["news_headline"] = None
                d["news_content"] = None
                d["media_url"] = None
                d["news_item_id"] = None
                d["media_item_id"] = None

                # Phase 8 — compute distance_to_entry_pct from current_price
                # (open positions) or batch-fetched candle price (pending positions).
                # Round 11 (2026-05-24): when we can't compute, leave it None
                # rather than 0.0. The frontend already shows '—' for null, and
                # the sort key pushes None rows to the second tier instead of
                # treating "no data" as "exactly at entry, closest possible".
                entry_price = d.get("entry_price")
                current_price = d.get("current_price") or price_map.get(sym)
                if entry_price and current_price and float(entry_price) != 0:
                    d["distance_to_entry_pct"] = ((float(current_price) - float(entry_price)) / float(entry_price)) * 100
                    d["current_price"] = float(current_price)
                else:
                    d["distance_to_entry_pct"] = None

                # Ensure created_at exists for sort consistency
                d["created_at"] = d.get("signal_timestamp") or d.get("created_at")

                d["_source"] = "pending_position"
                rows.append(d)
    except Exception as e:
        LOG.error("Pending positions aggregation failed: %s", e)

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
    return await _aggregate_signal_rows("interpretations", company_filter, limit)


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
        # Build a stable media URL for the front-end; fall back to proxy for external-only media.
        if d.get("media_local_path") and d.get("media_item_id") is not None:
            d["media_url"] = f"api/media/{d['media_item_id']}"
        elif d.get("media_source_url"):
            d["media_url"] = f"api/media/proxy?url={quote(d['media_source_url'], safe='')}"
        else:
            d["media_url"] = None
        return d


async def get_trader_drill_data(trader_id: str, company_filter: str | None = None) -> dict:
    """Fetch performance, all trades, and AI learnings for a specific trader.

    Matches ``trader_id`` against both ``tracked_positions.actor_id`` AND
    ``trader_profiles.handle_normalized`` so the dashboard can pass Discord
    handles (e.g. ``kingofsocks``) from the leaderboard directly.

    Returns:
        Dict with keys: trader_id, stats, trades_active, trades_history,
        ai_learnings.
    """
    companies = await list_active_companies()
    if company_filter and company_filter != "all":
        companies = [c for c in companies if c == company_filter]

    from shared.utils.db import get_shared_pool
    pool = await get_shared_pool()

    # Unified query that matches by actor_id OR handle_normalized
    _MATCH_WHERE = """(
        tp.actor_id = $1
        OR tp.trader_profile_id IN (
            SELECT id FROM public.trader_profiles WHERE handle_normalized = $1
        )
    )"""

    stats: dict = {}
    trades_active: list = []
    trades_history: list = []
    ai_learnings: list = []

    try:
        async with pool.acquire() as conn:
            # ── Stats: aggregate from tracked_positions ──
            agg = await conn.fetchrow(
                "SELECT "
                "  COUNT(*) AS total_trades, "
                "  COUNT(*) FILTER (WHERE tp.status NOT IN ('open', 'pending')) AS closed_trades, "
                "  COUNT(*) FILTER (WHERE tp.realized_pnl_usd > 0) AS wins, "
                "  COUNT(*) FILTER (WHERE tp.realized_pnl_usd < 0) AS losses, "
                "  COUNT(*) FILTER (WHERE tp.realized_pnl_usd = 0 AND tp.status NOT IN ('open', 'pending')) AS breakeven, "
                "  SUM(COALESCE(tp.realized_pnl_usd, 0)) AS total_pnl, "
                "  AVG(COALESCE(tp.realized_pnl_usd, 0)) FILTER (WHERE tp.realized_pnl_usd > 0) AS avg_win, "
                "  AVG(COALESCE(tp.realized_pnl_usd, 0)) FILTER (WHERE tp.realized_pnl_usd < 0) AS avg_loss, "
                "  MAX(COALESCE(tp.realized_pnl_usd, 0)) AS best_trade, "
                "  MIN(COALESCE(tp.realized_pnl_usd, 0)) AS worst_trade, "
                "  COUNT(DISTINCT tp.instrument_symbol) AS unique_coins "
                "FROM public.tracked_positions tp "
                "WHERE " + _MATCH_WHERE,
                trader_id,
            )
            if agg and agg["total_trades"]:
                total = int(agg["total_trades"] or 0)
                w = int(agg["wins"] or 0)
                l_ = int(agg["losses"] or 0)
                stats = {
                    "total_trades": total,
                    "closed_trades": int(agg["closed_trades"] or 0),
                    "wins": w,
                    "losses": l_,
                    "breakeven": int(agg["breakeven"] or 0),
                    "win_rate": round(w / total * 100, 1) if total > 0 else 0.0,
                    "total_pnl": float(agg["total_pnl"] or 0),
                    "avg_win": float(agg["avg_win"] or 0),
                    "avg_loss": float(agg["avg_loss"] or 0),
                    "best_trade": float(agg["best_trade"] or 0),
                    "worst_trade": float(agg["worst_trade"] or 0),
                    "unique_coins": int(agg["unique_coins"] or 0),
                }

            # ── Coin breakdown ──
            coin_rows = await conn.fetch(
                "SELECT "
                "  tp.instrument_symbol, "
                "  COUNT(*) AS trades, "
                "  COUNT(*) FILTER (WHERE tp.realized_pnl_usd > 0) AS coin_wins, "
                "  SUM(COALESCE(tp.realized_pnl_usd, 0)) AS coin_pnl "
                "FROM public.tracked_positions tp "
                "WHERE " + _MATCH_WHERE + " "
                "  AND tp.instrument_symbol IS NOT NULL "
                "GROUP BY tp.instrument_symbol "
                "ORDER BY trades DESC "
                "LIMIT 25",
                trader_id,
            )
            coin_breakdown = []
            for cr in coin_rows:
                sym = cr["instrument_symbol"] or ""
                ct = int(cr["trades"] or 0)
                cw = int(cr["coin_wins"] or 0)
                coin_breakdown.append({
                    "symbol": sym,
                    "trades": ct,
                    "wins": cw,
                    "win_rate": round(cw / ct * 100, 1) if ct > 0 else 0.0,
                    "pnl": float(cr["coin_pnl"] or 0),
                })
            stats["coin_breakdown"] = coin_breakdown
            stats["most_traded"] = sorted(coin_breakdown, key=lambda x: x["trades"], reverse=True)[:5]
            stats["most_profitable"] = sorted(coin_breakdown, key=lambda x: x["pnl"], reverse=True)[:5]

            # ── Active trades ──
            active_rows = await conn.fetch(
                "SELECT tp.*, tr.handle_normalized, tr.display_name, tr.platform, tr.trader_type "
                "FROM public.tracked_positions tp "
                "LEFT JOIN public.trader_profiles tr ON tr.id = tp.trader_profile_id "
                "WHERE tp.status IN ('open', 'pending', 'partial_exit') "
                "  AND " + _MATCH_WHERE + " "
                "ORDER BY tp.signal_timestamp DESC NULLS LAST "
                "LIMIT 200",
                trader_id,
            )
            trades_active = [dict(r) for r in active_rows]

            # ── History trades ──
            history_rows = await conn.fetch(
                "SELECT tp.*, tr.handle_normalized, tr.display_name, tr.platform, tr.trader_type "
                "FROM public.tracked_positions tp "
                "LEFT JOIN public.trader_profiles tr ON tr.id = tp.trader_profile_id "
                "WHERE tp.status NOT IN ('open', 'pending', 'partial_exit') "
                "  AND " + _MATCH_WHERE + " "
                "ORDER BY COALESCE(tp.closed_at, tp.updated_at, tp.signal_timestamp) DESC NULLS LAST "
                "LIMIT 500",
                trader_id,
            )
            trades_history = [dict(r) for r in history_rows]

            # ── AI learnings from agent_decisions ──
            learn_rows = await conn.fetch(
                "SELECT ad.id, ad.mode, ad.verdict, ad.confidence, ad.rationale, "
                "ad.decided_at, ad.inputs, ad.outputs, "
                "ap.name AS persona_name, ap.role AS persona_role "
                "FROM public.agent_decisions ad "
                "JOIN public.agent_personas ap ON ap.id = ad.persona_id "
                "WHERE ad.rationale IS NOT NULL AND ad.rationale <> '' "
                "  AND (ad.inputs::text ILIKE '%' || $1 || '%' "
                "       OR ad.outputs::text ILIKE '%' || $1 || '%') "
                "ORDER BY ad.decided_at DESC "
                "LIMIT 80",
                trader_id,
            )
            for lr in learn_rows:
                ai_learnings.append({
                    "id": lr["id"],
                    "mode": lr["mode"],
                    "verdict": lr["verdict"],
                    "confidence": float(lr["confidence"] or 0),
                    "rationale": lr["rationale"],
                    "decided_at": lr["decided_at"].isoformat() if lr["decided_at"] else None,
                    "persona_name": lr["persona_name"],
                    "persona_role": lr["persona_role"],
                })

            # ── AI learnings from position_postmortems (join via trader) ──
            pm_rows = await conn.fetch(
                "SELECT pm.lessons_for_actor, pm.what_happened, pm.why_it_worked, pm.why_it_failed, "
                "pm.created_at, pm.position_id, "
                "tp.instrument_symbol, tp.direction "
                "FROM public.position_postmortems pm "
                "JOIN public.tracked_positions tp ON tp.id = pm.position_id "
                "WHERE pm.lessons_for_actor IS NOT NULL AND pm.lessons_for_actor <> '' "
                "  AND " + _MATCH_WHERE + " "
                "ORDER BY pm.created_at DESC "
                "LIMIT 60",
                trader_id,
            )
            for pr in pm_rows:
                ai_learnings.append({
                    "source": "postmortem",
                    "position_id": pr["position_id"],
                    "symbol": pr["instrument_symbol"],
                    "direction": pr["direction"],
                    "what_happened": pr["what_happened"],
                    "why_it_worked": pr["why_it_worked"],
                    "why_it_failed": pr["why_it_failed"],
                    "lesson": pr["lessons_for_actor"],
                    "created_at": pr["created_at"].isoformat() if pr["created_at"] else None,
                })

            # Sort learnings by date
            ai_learnings.sort(
                key=lambda x: x.get("decided_at") or x.get("created_at") or "",
                reverse=True,
            )

    except Exception as e:
        LOG.error("Trader drill failed: %s", e)

    return {
        "trader_id": trader_id,
        "stats": stats,
        "trades_active": trades_active,
        "trades_history": trades_history,
        "ai_learnings": ai_learnings[:100],
    }


async def get_queue_data() -> dict:
    """Fetch real-time ingest queue status from shared tables.

    Returns the most recent ``pending`` rows for the news + media pipelines
    plus the latest open positions. Keys match what the frontend
    ``renderQueue`` consumer expects (``news_pending``, ``media_pending``,
    ``positions_open``). Counts are included for the queue header strip.
    """
    from shared.utils.db import get_shared_pool

    try:
        pool = await get_shared_pool()
    except Exception as exc:
        LOG.exception("get_queue_data: shared pool unavailable: %s", exc)
        return {
            "news_pending": [],
            "media_pending": [],
            "positions_open": [],
            "counts": {"news_pending": 0, "media_pending": 0, "positions_open": 0},
        }

    async with pool.acquire() as conn:
        try:
            news = await conn.fetch(
                """
                SELECT id, source, headline, enrichment_status, collected_at
                FROM news_items
                WHERE enrichment_status = 'pending'
                ORDER BY collected_at DESC
                LIMIT 25
                """
            )
            media = await conn.fetch(
                """
                SELECT id, news_item_id, media_type, processing_status, created_at
                FROM media_items
                WHERE processing_status = 'pending'
                ORDER BY created_at DESC
                LIMIT 25
                """
            )
            pos = await conn.fetch(
                """
                SELECT id, instrument_symbol, direction, status, created_at
                FROM tracked_positions
                WHERE status = 'open'
                ORDER BY created_at DESC
                LIMIT 25
                """
            )
            news_count = await conn.fetchval(
                "SELECT COUNT(*) FROM news_items WHERE enrichment_status = 'pending'"
            )
            media_count = await conn.fetchval(
                "SELECT COUNT(*) FROM media_items WHERE processing_status = 'pending'"
            )
            pos_count = await conn.fetchval(
                "SELECT COUNT(*) FROM tracked_positions WHERE status = 'open'"
            )
        except Exception as exc:
            LOG.exception("get_queue_data: query failed: %s", exc)
            return {
                "news_pending": [],
                "media_pending": [],
                "positions_open": [],
                "counts": {"news_pending": 0, "media_pending": 0, "positions_open": 0},
            }

    return {
        "news_pending": [dict(r) for r in news],
        "media_pending": [dict(r) for r in media],
        "positions_open": [dict(r) for r in pos],
        "counts": {
            "news_pending": int(news_count or 0),
            "media_pending": int(media_count or 0),
            "positions_open": int(pos_count or 0),
        },
    }


async def aggregate_agent_decisions(
    company_filter: str | None = None, limit: int = 50
) -> List[dict]:
    """Fetch recent agent decisions joined with persona info.

    Queries ``agent_decisions`` and ``agent_personas`` in the shared DB,
    returning a timeline of agent decisions with persona name, role, mode,
    verdict, confidence, and rationale.

    Args:
        company_filter: Optional company short-name; ``None`` or ``"all"`` returns all rows.
        limit: Maximum number of rows to return.

    Returns:
        List of decision dicts ordered by ``decided_at`` DESC.
    """
    from shared.utils.db import get_shared_pool

    rows: List[dict] = []
    try:
        pool = await get_shared_pool()
        async with pool.acquire() as conn:
            base_sql = (
                "SELECT ad.id, ad.company_id, ad.mode, ad.verdict, "
                "ad.confidence, ad.rationale, ad.decided_at, "
                "ad.inputs, ad.outputs, ad.metadata, "
                "ap.name AS persona_name, ap.role AS persona_role, "
                "ap.description AS persona_description "
                "FROM public.agent_decisions ad "
                "JOIN public.agent_personas ap ON ap.id = ad.persona_id "
            )
            if company_filter and company_filter != "all":
                cr = await conn.fetch(
                    base_sql
                    + "WHERE ad.company_id = $1 "
                      "ORDER BY ad.decided_at DESC LIMIT $2",
                    company_filter, limit,
                )
            else:
                cr = await conn.fetch(
                    base_sql + "ORDER BY ad.decided_at DESC LIMIT $1",
                    limit,
                )
            for r in cr:
                d = dict(r)
                d["_company"] = d.get("company_id")
                rows.append(d)
    except Exception as exc:
        LOG.error("aggregate_agent_decisions failed: %s", exc)

    return rows


async def aggregate_agent_performance(
    company_filter: str | None = None,
) -> List[dict]:
    """Aggregate per-agent P&L and trade stats from tracked_positions.

    Groups closed positions by actor_id and company, computing win rate,
    total PnL, and trade count. Falls back to agent_decisions volume when
    tracked_positions has no data.

    Also enriches each row with ``handle_normalized`` and ``display_name``
    from ``trader_profiles`` (via ``trader_profile_id``) so the dashboard
    can cross-reference performance rows with the leaderboard which uses
    Discord handles as ``actor_id``.

    Args:
        company_filter: Optional company short-name; ``None`` or ``"all"``
            aggregates across every active company.

    Returns:
        List of performance dicts sorted by total_pnl DESC.
    """
    from shared.utils.db import get_shared_pool

    rows: List[dict] = []
    try:
        pool = await get_shared_pool()
        async with pool.acquire() as conn:
            base_sql = (
                "SELECT "
                "  coalesce(tp.actor_id, 'unknown') AS actor_id, "
                "  tp.company_id, "
                "  COUNT(*) AS total_trades, "
                "  COUNT(*) FILTER (WHERE tp.realized_pnl_usd > 0) AS wins, "
                "  COUNT(*) FILTER (WHERE tp.realized_pnl_usd < 0) AS losses, "
                "  SUM(COALESCE(tp.realized_pnl_usd, 0)) AS total_pnl, "
                "  AVG(COALESCE(tp.realized_pnl_usd, 0)) AS avg_pnl, "
                "  MAX(COALESCE(tp.realized_pnl_usd, 0)) AS best_trade, "
                "  MIN(COALESCE(tp.realized_pnl_usd, 0)) AS worst_trade, "
                "  MIN(tr.handle_normalized) AS handle_normalized, "
                "  MIN(tr.display_name) AS display_name "
                "FROM public.tracked_positions tp "
                "LEFT JOIN public.trader_profiles tr ON tr.id = tp.trader_profile_id "
                "WHERE tp.status <> 'open' "
                "  AND tp.realized_pnl_usd IS NOT NULL "
                "  AND tp.actor_id IS NOT NULL "
            )
            limit_sql = " GROUP BY tp.actor_id, tp.company_id ORDER BY total_pnl DESC LIMIT 50"
            if company_filter and company_filter != "all":
                cr = await conn.fetch(
                    base_sql
                    + "AND tp.company_id = $1 "
                    + limit_sql,
                    company_filter,
                )
            else:
                cr = await conn.fetch(base_sql + limit_sql)
            for r in cr:
                d = dict(r)
                total = int(d.get("total_trades") or 0)
                w = int(d.get("wins") or 0)
                d["win_rate"] = round(w / total * 100, 1) if total > 0 else 0.0
                d["_company"] = d.get("company_id")
                # Map agent persona name from agent_personas if the actor_id
                # matches a chart_hacker or surgeon naming convention.
                rows.append(d)
    except Exception as exc:
        LOG.error("aggregate_agent_performance failed: %s", exc)

    # Enrich with persona names from agent_personas (actor_id may match persona name)
    try:
        pool = await get_shared_pool()
        async with pool.acquire() as conn:
            personas = await conn.fetch(
                "SELECT id, name, role FROM public.agent_personas"
            )
            persona_map: Dict[str, Dict[str, str]] = {}
            for p in personas:
                pn = p["name"]
                persona_map[pn] = {"name": pn, "role": p["role"]}
            for row in rows:
                aid = row.get("actor_id", "")
                if aid in persona_map:
                    row["persona_name"] = persona_map[aid]["name"]
                    row["persona_role"] = persona_map[aid]["role"]
                elif aid.lower() in persona_map:
                    row["persona_name"] = persona_map[aid.lower()]["name"]
                    row["persona_role"] = persona_map[aid.lower()]["role"]
    except Exception as exc:
        LOG.warning("agent_performance persona enrichment failed: %s", exc)

    return rows


async def aggregate_agent_achievements(company_filter: str | None = None) -> List[dict]:
    """Fetch all agent achievements grouped by agent_id.

    Reads from public.agent_achievements and returns a list of
    achievement dicts sorted by most recent first.

    Args:
        company_filter: Optional company short-name (unused, achievements
            are agent-scoped not company-scoped).

    Returns:
        List of achievement dicts with agent_id, achievement_type,
        achieved_at, and metadata.
    """
    cache_key = ("agent_achievements", company_filter)
    now_mono = time.monotonic()
    if cache_key in _SNAPSHOT_CACHE:
        ts, data = _SNAPSHOT_CACHE[cache_key]
        if now_mono - ts < _CACHE_TTL:
            return data

    from shared.utils.db import get_shared_pool
    shared_pool = await get_shared_pool()

    result: List[dict] = []
    try:
        async with shared_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, agent_id, achievement_type, achieved_at, metadata "
                "FROM public.agent_achievements ORDER BY achieved_at DESC LIMIT 500"
            )
            for r in rows:
                d = dict(r)
                meta = d.get("metadata") or {}
                if isinstance(meta, str):
                    import json as _json
                    try:
                        meta = _json.loads(meta)
                    except Exception:
                        meta = {}
                d["metadata"] = meta
                d["label"] = meta.get("label") or d.get("achievement_type", "")
                result.append(d)
    except Exception as exc:
        LOG.error("aggregate_agent_achievements failed: %s", exc)

    _SNAPSHOT_CACHE[cache_key] = (now_mono, result)
    return result


async def aggregate_competitions(company_filter: str | None = None) -> List[dict]:
    """Aggregate active contests with participant scores for the dashboard.

    Reads from the contests and contest_participants tables, enriching
    with computed P&L scores when available. Returns a list of contest
    dicts each containing a ``participants`` sub-list ranked by equity.

    Args:
        company_filter: Optional company short-name to scope results.

    Returns:
        List of contest dicts with nested participant rankings.
    """
    cache_key = ("competitions", company_filter)
    now_mono = time.monotonic()
    if cache_key in _SNAPSHOT_CACHE:
        ts, data = _SNAPSHOT_CACHE[cache_key]
        if now_mono - ts < _CACHE_TTL:
            return data

    from shared.utils.db import get_shared_pool
    shared_pool = await get_shared_pool()

    result: List[dict] = []

    try:
        async with shared_pool.acquire() as conn:
            contest_rows = await conn.fetch(
                "SELECT id, name, venues, coins, starting_balance_usd, "
                "status, created_at, ends_at, metadata "
                "FROM contests ORDER BY created_at DESC"
            )

            for cr in contest_rows:
                contest = dict(cr)
                participant_rows = await conn.fetch(
                    "SELECT contest_id, company_id, agent_id, "
                    "strategy_ref, joined_at, "
                    "COALESCE(scores, '{}'::jsonb) AS scores, "
                    "COALESCE(metadata, '{}'::jsonb) AS metadata, "
                    "equity_usd, realized_pnl_usd, unrealized_pnl_usd, "
                    "return_pct, win_rate, total_trades, open_positions, "
                    "total_fees_usd, persona_id "
                    "FROM contest_participants WHERE contest_id = $1 "
                    "ORDER BY joined_at",
                    contest["id"],
                )

                participants: List[dict] = []
                for pr in participant_rows:
                    p = dict(pr)
                    scores = p.get("scores") or {}
                    if isinstance(scores, str):
                        import json as _json
                        try:
                            scores = _json.loads(scores)
                        except Exception:
                            scores = {}
                    p["scores"] = scores
                    metadata = p.get("metadata") or {}
                    if isinstance(metadata, str):
                        try:
                            metadata = _json.loads(metadata)
                        except Exception:
                            metadata = {}
                    p["metadata"] = metadata
                    # Prefer dedicated columns over JSONB scores
                    if p.get("equity_usd") is not None:
                        scores["equity"] = float(p["equity_usd"])
                        scores["total_realized_pnl_usd"] = float(p["realized_pnl_usd"] or 0)
                        scores["unrealized_pnl_usd"] = float(p["unrealized_pnl_usd"] or 0)
                        scores["return_pct"] = float(p["return_pct"] or 0)
                        scores["win_rate"] = float(p["win_rate"] or 0)
                        scores["total_trades"] = int(p["total_trades"] or 0)
                        scores["open_positions"] = int(p["open_positions"] or 0)
                        scores["total_fees"] = float(p["total_fees_usd"] or 0)
                    p["scores"] = scores
                    if company_filter and company_filter != "all":
                        if p.get("company_id") != company_filter:
                            continue
                    participants.append(p)

                participants.sort(
                    key=lambda x: float(
                        (x.get("scores") or {}).get("equity", 0) or 0
                    ),
                    reverse=True,
                )

                for rank_idx, p in enumerate(participants, start=1):
                    p["rank"] = rank_idx

                contest["participants"] = participants
                result.append(contest)

    except Exception as exc:
        LOG.error("aggregate_competitions failed: %s", exc)

    _SNAPSHOT_CACHE[cache_key] = (now_mono, result)
    return result


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
