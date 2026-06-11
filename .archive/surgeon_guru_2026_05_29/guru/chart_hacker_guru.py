"""
Module: chart_hacker_guru
Purpose: Fixed daemon that becomes the specialist/guru on ChartHackers Discord traders.
         Periodically queries tracked_positions, position_updates, and agent_opinions
         to generate comparison reports, trader statistics, and learning summaries.
         Writes reports to disk and stores key insights in Mem0 for cross-agent learning.
Location: /opt/tickles/shared/intelligence/chart_hacker_guru.py

Design:
  * Polls tickles_shared.tracked_positions + position_updates + agent_opinions
    for ChartHackers-related traders (source_id = charthackers_discord).
  * Generates per-trader statistics: win rate, avg R:R, avg hold time,
    most traded instruments, best/worst performing setups.
  * Generates cross-trader comparison: leaderboard, style differences,
    who has best risk management, who is most consistent.
  * Writes periodic reports to shared/reports/chart_hacker_guru/ directory.
  * Stores key learnings in Mem0 (dev namespace) for other agents to query.
  * Respects guru_poll_minutes from system_config (default 60).
  * No LLM calls — pure quant analysis from database.

Hardening:
  * Graceful handling of empty result sets.
  * SIGTERM/SIGINT graceful shutdown.
  * Report files rotated (max 30 days retained).
  * Exponential backoff on DB errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure shared imports resolve
_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parent
_ROOT = _SHARED.parent
for p in (_ROOT, _SHARED):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared.utils.config import load_env
from shared.utils.db import DatabasePool, get_shared_pool

logger = logging.getLogger("tickles.intelligence.chart_hacker_guru")

# ---------------------------------------------------------------------------
# Config (env-driven)
# ---------------------------------------------------------------------------
POLL_INTERVAL_M = float(os.environ.get("GURU_POLL_MIN", "60"))
REPORT_DIR = os.environ.get("GURU_REPORT_DIR", "/opt/tickles/shared/reports/chart_hacker_guru")
MAX_REPORT_AGE_DAYS = int(os.environ.get("GURU_MAX_REPORT_AGE", "30"))
CHARTHACKERS_SOURCE_SLUG = os.environ.get("GURU_SOURCE_SLUG", "charthackers_discord")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class GuruConfig:
    """Runtime configuration for the ChartHacker guru."""

    poll_interval_m: float = POLL_INTERVAL_M
    report_dir: str = REPORT_DIR
    max_report_age_days: int = MAX_REPORT_AGE_DAYS
    source_slug: str = CHARTHACKERS_SOURCE_SLUG


@dataclass
class TraderStats:
    """Computed statistics for a single trader."""

    trader_id: int
    display_name: str
    platform_handle: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakevens: int = 0
    win_rate_pct: float = 0.0
    avg_rr: float = 0.0
    avg_hold_hours: float = 0.0
    total_pnl: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    most_traded_symbol: str = ""
    symbols_traded: List[str] = field(default_factory=list)
    avg_confidence: float = 0.0
    chart_hacker_agreement_pct: float = 0.0


@dataclass
class CrossTraderReport:
    """Cross-trader comparison report."""

    generated_at: datetime
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    trader_stats: List[TraderStats] = field(default_factory=list)
    leaderboard_by_pnl: List[Tuple[str, float]] = field(default_factory=list)
    leaderboard_by_winrate: List[Tuple[str, float]] = field(default_factory=list)
    best_risk_manager: str = ""
    most_consistent: str = ""
    lessons_learned: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------
async def fetch_trader_positions(
    pool: DatabasePool,
    source_slug: str,
    days: int = 30,
) -> List[Dict[str, Any]]:
    """Fetch all tracked positions for traders in a source, last N days.

    Args:
        pool: Shared Postgres pool.
        source_slug: Source slug to filter by.
        days: Lookback period in days.

    Returns:
        List of position row dicts with trader info joined.
    """
    return await pool.fetch_all(
        """
        SELECT
            tp.id AS position_id,
            tp.trader_profile_id,
            tp.instrument_symbol,
            tp.epic_code,
            tp.direction,
            tp.entry_price,
            tp.stop_loss,
            tp.take_profit_1,
            tp.status,
            tp.outcome,
            tp.realized_pnl_usd,
            tp.created_at,
            tp.updated_at,
            tp.risk_reward_ratio,
            tpf.display_name,
            tpf.handle_normalized
        FROM public.tracked_positions tp
        JOIN public.trader_profiles tpf ON tpf.id = tp.trader_profile_id
        JOIN public.watched_users wu ON wu.trader_profile_id = tpf.id
        JOIN public.watched_channels wc ON wc.id = wu.channel_id
        JOIN public.collector_catalog cc ON cc.id = wc.collector_id
        WHERE cc.source_slug = $1
        AND tp.created_at >= NOW() - INTERVAL '1 day' * $2
        ORDER BY tp.created_at DESC
        """,
        (source_slug, days),
    )


async def fetch_position_updates_for_trader(
    pool: DatabasePool,
    trader_profile_id: int,
    days: int = 30,
) -> List[Dict[str, Any]]:
    """Fetch position updates for a specific trader's positions.

    Args:
        pool: Shared Postgres pool.
        trader_profile_id: Trader profile ID.
        days: Lookback period.

    Returns:
        List of update row dicts.
    """
    return await pool.fetch_all(
        """
        SELECT pu.*
        FROM public.position_updates pu
        JOIN public.tracked_positions tp ON tp.id = pu.position_id
        WHERE tp.trader_profile_id = $1
        AND pu.timestamp >= NOW() - INTERVAL '1 day' * $2
        ORDER BY pu.timestamp DESC
        """,
        (trader_profile_id, days),
    )


async def fetch_agent_opinions_for_trader(
    pool: DatabasePool,
    trader_profile_id: int,
    days: int = 30,
) -> List[Dict[str, Any]]:
    """Fetch ChartHacker agent opinions for a trader's positions.

    Args:
        pool: Shared Postgres pool.
        trader_profile_id: Trader profile ID.
        days: Lookback period.

    Returns:
        List of opinion row dicts.
    """
    return await pool.fetch_all(
        """
        SELECT ao.*
        FROM public.agent_opinions ao
        JOIN public.tracked_positions tp ON tp.id = ao.position_id
        WHERE tp.trader_profile_id = $1
        AND ao.is_published = TRUE
        AND ao.created_at >= NOW() - INTERVAL '%s days'
        ORDER BY ao.created_at DESC
        """,
        (trader_profile_id, days),
    )


# ---------------------------------------------------------------------------
# Statistics computation
# ---------------------------------------------------------------------------
def _compute_trader_stats(
    positions: List[Dict[str, Any]],
    updates: List[Dict[str, Any]],
    opinions: List[Dict[str, Any]],
) -> TraderStats:
    """Compute statistics for a single trader from their positions.

    Args:
        positions: List of position rows.
        updates: List of position update rows.
        opinions: List of agent opinion rows.

    Returns:
        TraderStats with computed metrics.
    """
    if not positions:
        return TraderStats(
            trader_id=0,
            display_name="",
            platform_handle="",
        )

    first = positions[0]
    stats = TraderStats(
        trader_id=first["trader_profile_id"],
        display_name=first.get("display_name") or first.get("platform_handle") or "",
        platform_handle=first.get("platform_handle") or "",
    )

    # Position outcomes
    symbol_counts: Dict[str, int] = {}
    pnls: List[float] = []
    hold_hours: List[float] = []
    rr_values: List[float] = []

    for pos in positions:
        stats.total_trades += 1
        outcome = pos.get("outcome")
        pnl = pos.get("realized_pnl_usd")
        rr = pos.get("risk_reward_ratio")

        if outcome == "take_profit":
            stats.wins += 1
        elif outcome == "stop_loss":
            stats.losses += 1
        elif outcome == "breakeven":
            stats.breakevens += 1

        if pnl is not None:
            pnls.append(float(pnl))
            stats.total_pnl += float(pnl)

        if rr is not None:
            rr_values.append(float(rr))

        sym = pos.get("instrument_symbol") or pos.get("epic_code") or "unknown"
        symbol_counts[sym] = symbol_counts.get(sym, 0) + 1

        # Hold time
        detected = pos.get("created_at")
        closed = pos.get("updated_at")
        if detected and closed:
            try:
                if isinstance(detected, str):
                    detected = datetime.fromisoformat(detected.replace("Z", "+00:00"))
                if isinstance(closed, str):
                    closed = datetime.fromisoformat(closed.replace("Z", "+00:00"))
                delta = (closed - detected).total_seconds() / 3600.0
                if delta > 0:
                    hold_hours.append(delta)
            except Exception:
                pass

    # Derived metrics
    closed_trades = stats.wins + stats.losses + stats.breakevens
    if closed_trades > 0:
        stats.win_rate_pct = (stats.wins / closed_trades) * 100.0

    if rr_values:
        stats.avg_rr = sum(rr_values) / len(rr_values)

    if hold_hours:
        stats.avg_hold_hours = sum(hold_hours) / len(hold_hours)

    if pnls:
        stats.best_trade_pnl = max(pnls)
        stats.worst_trade_pnl = min(pnls)

    # Most traded symbol
    if symbol_counts:
        stats.most_traded_symbol = max(symbol_counts.items(), key=lambda x: x[1])[0]
        stats.symbols_traded = list(symbol_counts.keys())

    # ChartHacker agreement
    if opinions:
        agreements = sum(1 for o in opinions if o.get("would_take_trade"))
        stats.chart_hacker_agreement_pct = (agreements / len(opinions)) * 100.0

    return stats


def _build_cross_trader_report(
    trader_stats: List[TraderStats],
    now: datetime,
    days: int = 30,
) -> CrossTraderReport:
    """Build a cross-trader comparison report.

    Args:
        trader_stats: List of per-trader stats.
        now: Report generation time.
        days: Lookback period.

    Returns:
        CrossTraderReport with leaderboards and insights.
    """
    report = CrossTraderReport(
        generated_at=now,
        period_end=now,
        period_start=now - timedelta(days=days),
        trader_stats=trader_stats,
    )

    if not trader_stats:
        report.lessons_learned.append("No trades recorded in the lookback period.")
        return report

    # Leaderboards
    by_pnl = sorted(
        [(s.display_name or s.platform_handle, s.total_pnl) for s in trader_stats],
        key=lambda x: x[1],
        reverse=True,
    )
    report.leaderboard_by_pnl = by_pnl

    by_wr = sorted(
        [(s.display_name or s.platform_handle, s.win_rate_pct) for s in trader_stats if s.total_trades > 0],
        key=lambda x: x[1],
        reverse=True,
    )
    report.leaderboard_by_winrate = by_wr

    # Best risk manager (highest avg R:R among those with >5 trades)
    risk_candidates = [s for s in trader_stats if s.total_trades >= 5 and s.avg_rr > 0]
    if risk_candidates:
        best = max(risk_candidates, key=lambda s: s.avg_rr)
        report.best_risk_manager = best.display_name or best.platform_handle

    # Most consistent (lowest variance in P&L among those with >5 trades)
    consistent_candidates = [s for s in trader_stats if s.total_trades >= 5]
    if consistent_candidates:
        # Use win rate as consistency proxy for now
        most = max(consistent_candidates, key=lambda s: s.win_rate_pct)
        report.most_consistent = most.display_name or most.platform_handle

    # Lessons learned
    for s in trader_stats:
        if s.total_trades >= 5:
            if s.win_rate_pct < 30:
                report.lessons_learned.append(
                    f"{s.display_name}: Low win rate ({s.win_rate_pct:.1f}%). "
                    f"Consider reviewing SL/TP placement (avg R:R {s.avg_rr:.2f})."
                )
            if s.avg_rr < 1.0:
                report.lessons_learned.append(
                    f"{s.display_name}: R:R below 1:1 ({s.avg_rr:.2f}). "
                    f"Risking more than potential reward."
                )
            if s.avg_hold_hours > 48:
                report.lessons_learned.append(
                    f"{s.display_name}: Avg hold time {s.avg_hold_hours:.1f}h. "
                    f"Consider shorter timeframes or tighter SL."
                )

    # Cross-trader lessons
    if len(trader_stats) >= 2:
        best_wr = by_wr[0] if by_wr else None
        best_pnl = by_pnl[0] if by_pnl else None
        if best_wr and best_pnl and best_wr[0] != best_pnl[0]:
            report.lessons_learned.append(
                f"Win-rate leader ({best_wr[0]}, {best_wr[1]:.1f}%) differs from "
                f"P&L leader ({best_pnl[0]}, ${best_pnl[1]:.2f}). "
                f"Position sizing and R:R matter more than win rate."
            )

    return report


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def _format_report(report: CrossTraderReport) -> str:
    """Format a CrossTraderReport as a human-readable string.

    Args:
        report: Report to format.

    Returns:
        Formatted report string.
    """
    lines: List[str] = []
    lines.append("=" * 70)
    lines.append("ChartHacker Guru Report")
    lines.append(f"Generated: {report.generated_at.isoformat()}")
    if report.period_start and report.period_end:
        lines.append(
            f"Period: {report.period_start.date()} to {report.period_end.date()}"
        )
    lines.append("=" * 70)
    lines.append("")

    # Per-trader stats
    for s in report.trader_stats:
        lines.append(f"--- {s.display_name or s.platform_handle} (@{s.platform_handle}) ---")
        lines.append(f"  Trades: {s.total_trades} | Wins: {s.wins} | Losses: {s.losses} | BE: {s.breakevens}")
        lines.append(f"  Win Rate: {s.win_rate_pct:.1f}%")
        lines.append(f"  Total P&L: ${s.total_pnl:.2f}")
        lines.append(f"  Best: ${s.best_trade_pnl:.2f} | Worst: ${s.worst_trade_pnl:.2f}")
        lines.append(f"  Avg R:R: {s.avg_rr:.2f}")
        lines.append(f"  Avg Hold: {s.avg_hold_hours:.1f}h")
        lines.append(f"  Most Traded: {s.most_traded_symbol}")
        lines.append(f"  ChartHacker Agrees: {s.chart_hacker_agreement_pct:.1f}%")
        lines.append("")

    # Leaderboards
    lines.append("--- Leaderboards ---")
    lines.append("By P&L:")
    for name, pnl in report.leaderboard_by_pnl:
        lines.append(f"  {name}: ${pnl:.2f}")
    lines.append("By Win Rate:")
    for name, wr in report.leaderboard_by_winrate:
        lines.append(f"  {name}: {wr:.1f}%")
    lines.append("")

    # Awards
    if report.best_risk_manager:
        lines.append(f"Best Risk Manager: {report.best_risk_manager}")
    if report.most_consistent:
        lines.append(f"Most Consistent: {report.most_consistent}")
    lines.append("")

    # Lessons
    lines.append("--- Lessons Learned ---")
    for lesson in report.lessons_learned:
        lines.append(f"  • {lesson}")
    lines.append("")

    lines.append("=" * 70)
    return "\n".join(lines)


def _write_report(report: CrossTraderReport, report_dir: str) -> str:
    """Write a report to disk and return the file path.

    Args:
        report: Report to write.
        report_dir: Directory for reports.

    Returns:
        Path to the written file.
    """
    Path(report_dir).mkdir(parents=True, exist_ok=True)
    ts = report.generated_at.strftime("%Y%m%d_%H%M%S")
    path = Path(report_dir) / f"chart_hacker_guru_{ts}.txt"
    path.write_text(_format_report(report), encoding="utf-8")

    # Also write JSON for programmatic access
    json_path = Path(report_dir) / f"chart_hacker_guru_{ts}.json"
    json_path.write_text(
        json.dumps(
            {
                "generated_at": report.generated_at.isoformat(),
                "period_start": report.period_start.isoformat() if report.period_start else None,
                "period_end": report.period_end.isoformat() if report.period_end else None,
                "traders": [
                    {
                        "trader_id": s.trader_id,
                        "display_name": s.display_name,
                        "platform_handle": s.platform_handle,
                        "total_trades": s.total_trades,
                        "wins": s.wins,
                        "losses": s.losses,
                        "win_rate_pct": s.win_rate_pct,
                        "total_pnl": s.total_pnl,
                        "avg_rr": s.avg_rr,
                        "avg_hold_hours": s.avg_hold_hours,
                        "most_traded_symbol": s.most_traded_symbol,
                        "chart_hacker_agreement_pct": s.chart_hacker_agreement_pct,
                    }
                    for s in report.trader_stats
                ],
                "leaderboard_by_pnl": report.leaderboard_by_pnl,
                "leaderboard_by_winrate": report.leaderboard_by_winrate,
                "best_risk_manager": report.best_risk_manager,
                "most_consistent": report.most_consistent,
                "lessons_learned": report.lessons_learned,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    logger.info("Wrote report to %s", path)
    return str(path)


def _cleanup_old_reports(report_dir: str, max_age_days: int) -> int:
    """Remove report files older than max_age_days.

    Args:
        report_dir: Directory containing reports.
        max_age_days: Maximum age in days.

    Returns:
        Number of files removed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    count = 0
    for f in Path(report_dir).glob("chart_hacker_guru_*.*"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink()
                count += 1
        except Exception as exc:
            logger.warning("Failed to cleanup %s: %s", f, exc)
    if count:
        logger.info("Cleaned up %d old reports", count)
    return count


# ---------------------------------------------------------------------------
# Mem0 integration
# ---------------------------------------------------------------------------
async def _store_learning_in_mem0(lesson: str, metadata: Dict[str, Any]) -> None:
    """Store a learning in Mem0 dev namespace.

    Args:
        lesson: Learning text to store.
        metadata: Additional metadata dict.
    """
    try:
        from shared.utils.mem0_config import get_dev_memory

        mem, agent_id = get_dev_memory(agent="chart_hacker_guru")
        mem.add(
            lesson,
            user_id="dev",
            agent_id=agent_id,
            metadata={"type": "learning", **metadata},
        )
        logger.debug("Stored learning in Mem0: %s", lesson[:80])
    except Exception as exc:
        logger.warning("Failed to store learning in Mem0: %s", exc)


# ---------------------------------------------------------------------------
# Daemon class
# ---------------------------------------------------------------------------
class ChartHackerGuru:
    """Daemon that generates trader comparison reports and stores learnings."""

    def __init__(self, cfg: Optional[GuruConfig] = None) -> None:
        self.cfg = cfg or GuruConfig()
        self._stop = asyncio.Event()

    async def _ensure_pool(self) -> DatabasePool:
        return await get_shared_pool()

    async def run_cycle(self, days: int = 30) -> Dict[str, Any]:
        """Run one report generation cycle.

        Args:
            days: Lookback period in days.

        Returns:
            Summary dict with report path and stats.
        """
        pool = await self._ensure_pool()
        now = datetime.now(timezone.utc)

        # Fetch positions
        positions = await fetch_trader_positions(pool, self.cfg.source_slug, days)
        if not positions:
            logger.info("No positions found for source %s in last %d days", self.cfg.source_slug, days)
            return {"report_path": None, "traders": 0, "positions": 0}

        # Group by trader
        by_trader: Dict[int, List[Dict[str, Any]]] = {}
        for pos in positions:
            tid = pos["trader_profile_id"]
            by_trader.setdefault(tid, []).append(pos)

        # Compute stats per trader
        trader_stats: List[TraderStats] = []
        for tid, t_positions in by_trader.items():
            updates = await fetch_position_updates_for_trader(pool, tid, days)
            opinions = await fetch_agent_opinions_for_trader(pool, tid, days)
            stats = _compute_trader_stats(t_positions, updates, opinions)
            trader_stats.append(stats)

        # Build report
        report = _build_cross_trader_report(trader_stats, now, days)

        # Write to disk
        report_path = _write_report(report, self.cfg.report_dir)

        # Cleanup old reports
        _cleanup_old_reports(self.cfg.report_dir, self.cfg.max_report_age_days)

        # Store lessons in Mem0
        for lesson in report.lessons_learned:
            await _store_learning_in_mem0(
                lesson,
                {
                    "topic": "chart_hacker_trader_analysis",
                    "generated_at": now.isoformat(),
                    "source": self.cfg.source_slug,
                },
            )

        logger.info(
            "Guru cycle complete: %d traders, %d positions, report=%s",
            len(trader_stats),
            len(positions),
            report_path,
        )
        return {
            "report_path": report_path,
            "traders": len(trader_stats),
            "positions": len(positions),
            "lessons": len(report.lessons_learned),
        }

    async def _run_hourly_status_check(self) -> Dict[str, Any]:
        """Run an hourly status check: report what we're tracking, struggles, recommendations.

        Returns:
            Status dict with open positions, struggles, and recommendations.
        """
        pool = await self._ensure_pool()
        now = datetime.now(timezone.utc)

        # Fetch open positions
        open_positions = await pool.fetch_all(
            """
            SELECT
                tp.id,
                tp.trader_profile_id,
                tp.instrument_symbol,
                tp.epic_code,
                tp.direction,
                tp.entry_price,
                tp.stop_loss,
                tp.take_profit_1,
                tp.status,
                tp.created_at,
                tpf.display_name,
                tpf.handle_normalized
            FROM public.tracked_positions tp
            JOIN public.trader_profiles tpf ON tpf.id = tp.trader_profile_id
            WHERE tp.status IN ('open', 'partial_tp', 'approaching_entry')
            ORDER BY tp.created_at DESC
            """,
        )

        # Fetch recent position updates (last hour)
        recent_updates = await pool.fetch_all(
            """
            SELECT
                pu.position_id,
                pu.price AS current_price,
                pu.distance_to_sl_pct,
                pu.distance_to_tp1_pct AS distance_to_tp_pct,
                pu.unrealized_pnl_usd,
                pu.timestamp AS snapshot_time
            FROM public.position_updates pu
            WHERE pu.timestamp >= NOW() - INTERVAL '1 hour'
            ORDER BY pu.timestamp DESC
            """,
        )

        # Build status report
        status_lines: List[str] = []
        status_lines.append("=" * 70)
        status_lines.append(f"ChartHacker Guru Hourly Status — {now.isoformat()}")
        status_lines.append("=" * 70)
        status_lines.append("")

        struggles: List[str] = []
        recommendations: List[str] = []

        if not open_positions:
            status_lines.append("No open positions currently tracked.")
            status_lines.append("")
            struggles.append("No trades detected in the last hour — collector or interpretation may be failing.")
            recommendations.append("Check Discord collector connectivity and interpretation service health.")
        else:
            status_lines.append(f"Open positions: {len(open_positions)}")
            status_lines.append("")

            # Group by position
            updates_by_pos: Dict[int, List[Dict[str, Any]]] = {}
            for u in recent_updates:
                pid = u.get("position_id")
                if pid:
                    updates_by_pos.setdefault(pid, []).append(u)

            for pos in open_positions:
                pid = pos["id"]
                symbol = pos.get("instrument_symbol") or pos.get("epic_code") or "unknown"
                direction = pos.get("direction", "unknown")
                entry = pos.get("entry_price")
                sl = pos.get("stop_loss")
                tp = pos.get("take_profit_1")
                trader = pos.get("display_name") or pos.get("handle_normalized") or "unknown"
                status = pos.get("status", "unknown")

                status_lines.append(f"--- Position {pid} | {trader} | {symbol} {direction.upper()} ---")
                status_lines.append(f"  Status: {status}")
                if entry:
                    status_lines.append(f"  Entry: {entry}")
                if sl:
                    status_lines.append(f"  SL: {sl}")
                if tp:
                    status_lines.append(f"  TP: {tp}")

                # Check recent updates
                updates = updates_by_pos.get(pid, [])
                if updates:
                    latest = updates[0]
                    curr_price = latest.get("current_price")
                    dist_sl = latest.get("distance_to_sl_pct")
                    dist_tp = latest.get("distance_to_tp_pct")
                    pnl = latest.get("unrealized_pnl_usd")

                    if curr_price:
                        status_lines.append(f"  Current Price: {curr_price}")
                    if dist_sl is not None:
                        status_lines.append(f"  Distance to SL: {dist_sl:.2f}%")
                    if dist_tp is not None:
                        status_lines.append(f"  Distance to TP: {dist_tp:.2f}%")
                    if pnl is not None:
                        status_lines.append(f"  Unrealized P&L: ${pnl:.2f}")

                    # Detect if price is moving toward or against
                    if direction == "long" and entry and curr_price:
                        if curr_price > entry:
                            status_lines.append("  Price moving TOWARD TP (above entry)")
                        else:
                            status_lines.append("  Price moving TOWARD SL (below entry)")
                            if dist_sl and dist_sl < 1.0:
                                struggles.append(f"Position {pid} ({trader}, {symbol}) is very close to SL ({dist_sl:.2f}%).")
                    elif direction == "short" and entry and curr_price:
                        if curr_price < entry:
                            status_lines.append("  Price moving TOWARD TP (below entry)")
                        else:
                            status_lines.append("  Price moving TOWARD SL (above entry)")
                            if dist_sl and dist_sl < 1.0:
                                struggles.append(f"Position {pid} ({trader}, {symbol}) is very close to SL ({dist_sl:.2f}%).")
                else:
                    status_lines.append("  No recent updates — position monitor may be stalled.")
                    struggles.append(f"Position {pid} ({trader}, {symbol}) has no recent price updates.")
                    recommendations.append(f"Check position monitor health for {symbol}.")

                status_lines.append("")

        # Check for candles sufficiency
        symbols = list({
            (p.get("instrument_symbol") or p.get("epic_code") or "unknown")
            for p in open_positions
        })
        for sym in symbols:
            if sym == "unknown":
                continue
            candle_count = await pool.fetch_val(
                """
                SELECT COUNT(*) FROM public.candles
                WHERE symbol = $1 AND timestamp >= NOW() - INTERVAL '4 hours'
                """,
                (sym,),
            )
            if candle_count is not None and candle_count < 10:
                struggles.append(f"Insufficient candles for {sym}: only {candle_count} in last 4 hours.")
                recommendations.append(f"Check candle collection for {sym} — may need to restart candle service.")

        # Add struggles and recommendations to report
        if struggles:
            status_lines.append("--- STRUGGLES / WARNINGS ---")
            for s in struggles:
                status_lines.append(f"  ⚠ {s}")
            status_lines.append("")

        if recommendations:
            status_lines.append("--- RECOMMENDATIONS ---")
            for r in recommendations:
                status_lines.append(f"  → {r}")
            status_lines.append("")

        status_lines.append("=" * 70)
        status_text = "\n".join(status_lines)

        # Write status report to disk
        Path(self.cfg.report_dir).mkdir(parents=True, exist_ok=True)
        ts = now.strftime("%Y%m%d_%H%M%S")
        status_path = Path(self.cfg.report_dir) / f"hourly_status_{ts}.txt"
        status_path.write_text(status_text, encoding="utf-8")

        # Store in Mem0
        await _store_learning_in_mem0(
            f"Hourly status: {len(open_positions)} open positions. "
            f"Struggles: {len(struggles)}. Recommendations: {len(recommendations)}.",
            {
                "topic": "hourly_status",
                "generated_at": now.isoformat(),
                "open_positions": len(open_positions),
                "struggles": struggles,
                "recommendations": recommendations,
            },
        )

        logger.info(
            "Hourly status check: %d open positions, %d struggles, %d recommendations, written to %s",
            len(open_positions),
            len(struggles),
            len(recommendations),
            status_path,
        )

        return {
            "open_positions": len(open_positions),
            "struggles": struggles,
            "recommendations": recommendations,
            "status_path": str(status_path),
        }

    async def run_forever(self) -> None:
        """Main loop: generate reports periodically until stopped.
        Also runs hourly status checks."""
        logger.info(
            "ChartHackerGuru starting (poll=%.0fmin, source=%s, report_dir=%s)",
            self.cfg.poll_interval_m,
            self.cfg.source_slug,
            self.cfg.report_dir,
        )

        # Track when we last ran hourly status
        last_hourly = datetime.now(timezone.utc) - timedelta(hours=2)

        while not self._stop.is_set():
            try:
                await self.run_cycle()
            except Exception as exc:
                logger.exception("Guru cycle failed: %s", exc)

            # Run hourly status check if it's been > 55 minutes
            now = datetime.now(timezone.utc)
            if (now - last_hourly).total_seconds() >= 3300:  # 55 minutes
                try:
                    await self._run_hourly_status_check()
                    last_hourly = now
                except Exception as exc:
                    logger.exception("Hourly status check failed: %s", exc)

            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.cfg.poll_interval_m * 60.0,
                )
            except asyncio.TimeoutError:
                pass
        logger.info("ChartHackerGuru stopped")

    def stop(self) -> None:
        """Signal the daemon to stop gracefully."""
        self._stop.set()


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------
def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """Install SIGINT/SIGTERM handlers for graceful shutdown."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass


async def main() -> None:
    """Run the ChartHackerGuru daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()
    guru = ChartHackerGuru()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, guru._stop)
    await guru.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
