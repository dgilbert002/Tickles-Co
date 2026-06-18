"""
MCP tool: tickles.snapshot — single-call financial overview for cron/dashboard.
Returns: competition rankings, account balances (free+total+equity), margin health,
open positions, orphaned fills, rejection summary.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


async def _handle_snapshot(p: Dict[str, Any]) -> Dict[str, Any]:
    """Return a complete financial snapshot in one MCP call."""
    from shared.utils.db import get_shared_pool

    try:
        pool = await get_shared_pool()

        # 1. Competition rankings
        rankings_rows = await pool.fetch_all(
            """SELECT agent_id, equity_usd, realized_pnl_usd, unrealized_pnl_usd,
                      return_pct, win_rate, total_trades, open_positions, total_fees_usd
               FROM public.contest_participants
               WHERE contest_id = 'copy-trade-scenarios'
               ORDER BY equity_usd DESC""")
        rankings = [{
            "agentId": r["agent_id"],
            "equityUsd": float(r["equity_usd"] or 0),
            "realizedPnlUsd": float(r["realized_pnl_usd"] or 0),
            "unrealizedPnlUsd": float(r["unrealized_pnl_usd"] or 0),
            "returnPct": float(r["return_pct"] or 0),
            "winRate": float(r["win_rate"] or 0),
            "totalTrades": int(r["total_trades"] or 0),
            "openPositions": int(r["open_positions"] or 0),
        } for r in rankings_rows]

        # 2. Demo account balances (free + total from metadata)
        acct_rows = await pool.fetch_all(
            """SELECT exchange, account_name, account_type, last_balance, last_tested_at,
                      metadata->>'balance_total' as balance_total,
                      metadata->>'margin_mode' as margin_mode
               FROM public.exchange_accounts
               WHERE is_active = TRUE AND account_type IN ('demo', 'live')
               ORDER BY exchange, account_name""")
        accounts = []
        for r in acct_rows:
            acct = {
                "exchange": r["exchange"],
                "accountName": r["account_name"],
                "accountType": r["account_type"],
                "freeUsdt": float(r["last_balance"] or 0),
                "totalUsdt": float(r["balance_total"] or 0) if r["balance_total"] else None,
                "marginMode": r["margin_mode"] or "unknown",
                "lastTestedAt": str(r["last_tested_at"]) if r["last_tested_at"] else None,
            }
            accounts.append(acct)

        # 3. Demo order counts
        order_rows = await pool.fetch_all(
            "SELECT status, COUNT(*) as cnt FROM public.demo_orders GROUP BY status")
        orders = {r["status"]: r["cnt"] for r in order_rows}

        # 4. Orphaned filled orders
        orphan_rows = await pool.fetch_all(
            "SELECT exchange, account_name, COUNT(*) as n "
            "FROM public.demo_orders WHERE status='filled' AND closed_at IS NULL "
            "GROUP BY exchange, account_name ORDER BY n DESC")
        orphans = [{
            "exchange": r["exchange"],
            "accountName": r["account_name"],
            "count": r["n"],
        } for r in orphan_rows]

        # 5. Rejection summary (24h)
        rej_rows = await pool.fetch_all(
            "SELECT LEFT(error_message, 120) as reason, COUNT(*) as n "
            "FROM public.demo_orders WHERE status='rejected' "
            "AND ordered_at > NOW() - INTERVAL '24 hours' "
            "GROUP BY LEFT(error_message, 120) ORDER BY n DESC LIMIT 5")
        rejections = [{"reason": r["reason"], "count": r["n"]} for r in rej_rows]

        # 6. Open tracked positions
        pos_rows = await pool.fetch_all(
            "SELECT COUNT(*) as cnt FROM public.tracked_positions "
            "WHERE status = 'open'")
        open_positions = pos_rows[0]["cnt"] if pos_rows else 0

        # 7. Pending signals (tracked traders only)
        sig_rows = await pool.fetch_all(
            """SELECT COUNT(*) as cnt
               FROM public.media_items m
               JOIN public.news_items n ON n.id = m.news_item_id
               JOIN public.trader_profiles tp ON tp.handle_normalized = LOWER(n.author)
               WHERE m.processing_status = 'downloaded'
                 AND m.media_type = 'image'
                 AND tp.is_tracked = TRUE""")
        pending_tracked = sig_rows[0]["cnt"] if sig_rows else 0

        sig_all = await pool.fetch_all(
            "SELECT COUNT(*) as cnt FROM public.media_items "
            "WHERE processing_status = 'downloaded' AND media_type = 'image'")
        pending_total = sig_all[0]["cnt"] if sig_all else 0

        return {
            "ok": True,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "competition": {
                "rankings": rankings,
                "totalAgents": len(rankings),
            },
            "accounts": accounts,
            "demoOrders": orders,
            "orphanedFills": {
                "total": sum(o["count"] for o in orphans),
                "byAccount": orphans,
            },
            "rejections24h": rejections,
            "positions": {
                "open": open_positions,
            },
            "signals": {
                "pendingTracked": pending_tracked,
                "pendingTotal": pending_total,
            },
        }
    except Exception as exc:
        logger.exception("tickles.snapshot failed")
        return {"ok": False, "error": str(exc)}


def register(registry, ctx):
    """Register tickles.snapshot tool with the MCP registry."""
    from ..protocol import McpTool

    tool = McpTool(
        name="tickles.snapshot",
        description="Single-call financial snapshot: competition rankings, account balances, demo orders, orphans, rejections, positions, signals.",
        input_schema={
            "type": "object",
            "properties": {},
        },
        tags={"phase": "3", "group": "trading", "status": "live"},
    )
    registry.register(tool, _handle_snapshot)
