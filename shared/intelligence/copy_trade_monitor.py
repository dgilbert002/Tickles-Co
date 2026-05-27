"""
Live Copy-Trade Monitor — watches trader positions and manages 6 competing
paper agents independently. Each agent enters when a trader enters, but
exits based on its own SL/TP rules (original or optimized).

Architecture:
  1. Poll tracked_positions for new 'open' positions every 30s
  2. For each new open, create paper entries for all 6 agents
  3. Each agent gets its own SL/TP: original or ×optimal multiplier
  4. Monitor 1m candles independently per symbol
  5. When an agent's SL/TP hits → close their paper position, update contest score
  6. When trader's position closes → close any remaining agent positions at trader's exit

Agents:
  A   = Spot Sequential (full balance, no overlap)
  B   = Leveraged Parallel (5% risk, max 20 concurrent, lev from SL)
  C   = +BE Lock 100x (same as B, SL→BE at +5%, 100x reset)
  A+Opt/B+Opt/C+Opt = Same but use optimized SL/TP multipliers
"""
import asyncio
import json
import logging
import os
import re
import signal
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/opt/tickles")

from shared.utils.db import get_shared_pool
from shared.utils.config import load_env
from shared.services.banker import update_balance as banker_update

logger = logging.getLogger("copy_trade.monitor")

POLL_INTERVAL_S = int(os.environ.get("COPY_TRADE_POLL_S", "30"))

# Optimized SL/TP multipliers — loaded from system_config table at startup
# and refreshed each tick so the auto-optimizer cron can update them live.
OPTIMAL: Dict[str, tuple] = {}

# Agent configurations
AGENTS = [
    ("A: Spot Seq",      1.0, 1.0, "spot_seq"),
    ("B: Lev Parallel",  1.0, 1.0, "lev_parallel"),
    ("C: +BE Lock",      1.0, 1.0, "lev_be_lock"),
    ("A+Opt: Spot Seq",  None, None, "spot_seq_opt"),
    ("B+Opt: Lev Par",   None, None, "lev_parallel_opt"),
    ("C+Opt: +BE Lock",  None, None, "lev_be_lock_opt"),
    ("CH: AI Vision",    1.0, 1.0, "spot_seq_ch"),
    ("A×3: Spot Lev 3x", 1.0, 1.0, "spot_lev_3x"),
    ("Rose A: Spot Seq",  1.0, 1.0, "spot_seq_rose"),
    ("Rose B: Lev Par",   1.0, 1.0, "lev_parallel_rose"),
    ("Rose C: +BE Lock",  1.0, 1.0, "lev_be_lock_rose"),
    ("D: 3% Lev Par",     1.0, 1.0, "lev_parallel_3pct"),
]

# Display-name → DB-id mapping. Round-7 persistence migration (2026-05-24):
# this mapping was duplicated in 3 places (`_update_contest_scores`,
# `_log_trade_open`, `_log_trade`). Factored to a module constant so the
# new persistence helpers `_load_state` / `_save_state` reuse the single
# source of truth and a future agent rename only needs one edit.
NAME_TO_ID = {
    "A: Spot Seq":      "copy_spot_seq",
    "B: Lev Parallel":  "copy_lev_parallel",
    "C: +BE Lock":      "copy_lev_be_lock",
    "A+Opt: Spot Seq":  "copy_opt_spot_seq",
    "B+Opt: Lev Par":   "copy_opt_lev_parallel",
    "C+Opt: +BE Lock":  "copy_opt_lev_be_lock",
    "CH: AI Vision":    "copy_ch_ai_vision",
    "A×3: Spot Lev 3x": "copy_spot_lev_3x",
    "Rose A: Spot Seq":  "copy_rose_a",
    "Rose B: Lev Par":   "copy_rose_b",
    "Rose C: +BE Lock":  "copy_rose_c",
    "D: 3% Lev Par":    "copy_lev_3pct",
}
ID_TO_NAME = {v: k for k, v in NAME_TO_ID.items()}

FEES = dict(slippage_bps=2)  # maker/taker loaded from instruments per trade

# Perp suffixes that CCXT appends for linear/inverse perpetuals.
# tracked_positions.instrument_symbol stores these (e.g. XAU/USDT:USDT)
# but the instruments table uses spot forms (e.g. XAU/USDT). When looking
# up candles or instrument fees we need to try both forms because Bybit
# only lists tokenized assets as perps, not spot.
_PERP_SUFFIX_RE = re.compile(r":(USDT|USDC|BUSD|USD)$", re.IGNORECASE)


def _symbol_lookup_candidates(symbol: str) -> list:
    """Return candidate symbol forms to try when looking up instruments/candles.

    Order: stripped form first (matches instruments table), then original
    (matches tracked_positions exactly), then slash/no-slash variants.
    """
    if not symbol:
        return []
    out = []
    s = symbol.strip()
    # Strip :USDT / :USDC / :BUSD / :USD perp suffix
    stripped = _PERP_SUFFIX_RE.sub("", s)
    if stripped != s:
        out.append(stripped)
    if s not in out:
        out.append(s)
    # Try slash ↔ no-slash variants
    if "/" not in stripped and len(stripped) > 3:
        for quote in ("USDT", "USDC", "BUSD", "USD"):
            if stripped.endswith(quote) and len(stripped) > len(quote):
                slash = stripped[:-len(quote)] + "/" + quote
                if slash not in out:
                    out.append(slash)
                break
    elif "/" in stripped:
        noslash = stripped.replace("/", "")
        if noslash not in out:
            out.append(noslash)
    return out


def get_sl_tp(entry: float, direction: str, orig_sl: float, orig_tp: float,
              symbol: str, use_optimal: bool):
    """Compute agent-specific SL and TP."""
    if not use_optimal:
        return orig_sl, orig_tp

    sl_m, tp_m = OPTIMAL.get(symbol, (1.0, 1.0))
    sl_dist = abs(entry - orig_sl) / entry
    tp_dist = abs(orig_tp - entry) / entry
    if direction == "long":
        return entry * (1 - sl_dist * sl_m), entry * (1 + tp_dist * tp_m)
    else:
        return entry * (1 + sl_dist * sl_m), entry * (1 - tp_dist * tp_m)


def compute_pnl(entry: float, exit_px: float, direction: str, notional: float):
    """Simple P&L: notional × (exit/entry - 1) for long, notional × (1 - exit/entry) for short."""
    if entry <= 0:
        return 0.0
    if direction == "long":
        return notional * (exit_px / entry - 1.0)
    else:
        return notional * (1.0 - exit_px / entry)


class LiveCopyTradeMonitor:
    """Daemon that watches trader positions and manages 6 competing paper agents."""

    def __init__(self):
        self._stop = asyncio.Event()
        self._pool = None
        self._started_at = datetime.now(timezone.utc)
        # Track which trader positions we've already entered
        self._entered_positions: set = set()
        # Agent state: agent_id → {balance, open_positions: [{trader_id, entry, sl, tp, dir, symbol, allocated, lev}]}
        self._agents: Dict[str, Dict] = {}
        for name, _, _, mode in AGENTS:
            self._agents[name] = {
                "balance": 1000.0,
                "starting": 1000.0,
                "total_pnl": 0.0,
                "total_fees": 0.0,
                "wins": 0,
                "losses": 0,
                "trades": 0,
                "open_positions": [],
                "mode": mode,
            }

    async def _ensure_pool(self):
        if self._pool is None:
            self._pool = await get_shared_pool()
        return self._pool

    # --------------------------------------------------------------------
    # Round-7 persistence layer (Phase 1 of the copy-agent state fix,
    # 2026-05-24).
    # --------------------------------------------------------------------
    # Why this exists:
    #   Before this layer landed, every agent's running balance, win/loss
    #   counters, total P&L, and open-paper-position list lived ONLY in
    #   the in-memory `self._agents` dict. Any service restart wiped
    #   everything and re-initialised every agent to balance=$1000,
    #   trades=0, open_positions=[]. The dashboard's `contest_participants`
    #   row then froze at the first `_update_contest_scores` push after
    #   restart — which (because nothing had happened yet) was always the
    #   round-defaults. End result: 7 agents permanently stuck at $1000 /
    #   $0 / 0 trades on the dashboard.
    #
    # Design:
    #   - Single JSONB-bearing row per agent in public.copy_agent_state.
    #   - Per-agent UPSERT: `_save_agent(name)` is the write hot path,
    #     called after every position open/close so a crash mid-cycle
    #     loses at most one in-flight close.
    #   - Bulk save `_save_state()` is called on every contest score push
    #     so even if individual close paths skip a save, we eventually
    #     reconcile.
    #   - `_load_state()` runs ONCE at startup (top of `run_forever`),
    #     before the first tick. Falls back to in-memory defaults if the
    #     row is missing — same behaviour as today on a clean install.

    async def _save_agent(self, agent_name: str) -> None:
        """Persist a single agent's state. Called after every open/close.

        Per-agent UPSERT keeps the write path cheap and atomic — only the
        agent that just changed gets a round-trip, not all 7. UPSERT means
        a missing seed row is auto-created (defensive against a future
        operator dropping the seed migration).
        """
        agent_id = NAME_TO_ID.get(agent_name)
        if not agent_id:
            return
        agent = self._agents.get(agent_name)
        if agent is None:
            return
        # Sanitise open_positions for JSONB: datetime must be isoformat.
        ops = []
        for pos in agent.get("open_positions", []):
            p = dict(pos)
            ts = p.get("entered_at")
            if hasattr(ts, "isoformat"):
                p["entered_at"] = ts.isoformat()
            ops.append(p)
        entered_ids = sorted({
            int(p["trader_id"]) for p in agent.get("open_positions", [])
            if p.get("trader_id") is not None
        })
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO public.copy_agent_state
                  (agent_id, balance, starting_balance, total_pnl, total_fees,
                   wins, losses, trades, open_positions,
                   entered_position_ids, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, now())
                ON CONFLICT (agent_id) DO UPDATE SET
                  balance              = EXCLUDED.balance,
                  starting_balance     = EXCLUDED.starting_balance,
                  total_pnl            = EXCLUDED.total_pnl,
                  total_fees           = EXCLUDED.total_fees,
                  wins                 = EXCLUDED.wins,
                  losses               = EXCLUDED.losses,
                  trades               = EXCLUDED.trades,
                  open_positions       = EXCLUDED.open_positions,
                  entered_position_ids = EXCLUDED.entered_position_ids,
                  updated_at           = now()
                """,
                agent_id,
                float(agent["balance"]),
                float(agent["starting"]),
                float(agent["total_pnl"]),
                float(agent["total_fees"]),
                int(agent["wins"]),
                int(agent["losses"]),
                int(agent["trades"]),
                json.dumps(ops),
                json.dumps(entered_ids),
            )

    async def _save_state(self) -> None:
        """Persist all agents in one pass. Belt-and-suspenders backstop
        called from `_update_contest_scores` so even if a single
        `_save_agent` was skipped (exception, early return), we eventually
        reconcile."""
        for agent_name in list(self._agents.keys()):
            try:
                await self._save_agent(agent_name)
            except Exception as exc:
                logger.warning(
                    "copy_agent_state save failed for %s: %s", agent_name, exc,
                )

    async def _load_state(self) -> None:
        """Hydrate self._agents from persisted state on startup.

        Behaviour:
          - For every persisted row whose agent_id maps to a known agent
            display-name, copy the columns into the in-memory dict.
          - Open paper-positions are re-attached to the agent.
          - The global `_entered_positions` set is rebuilt from the union
            of all loaded open_positions (so we don't double-enter on the
            first tick after restart) AND from the closed `competition_trades`
            rows for this contest (so we don't reopen positions we've
            already paper-closed).
          - Missing rows are NOT errors — the seed migration ensures the
            7 known agents exist, but a fresh install or new agent simply
            falls back to the in-memory defaults from __init__.
        """
        pool = await self._ensure_pool()
        loaded = 0
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT agent_id, balance, starting_balance, total_pnl,
                       total_fees, wins, losses, trades, open_positions,
                       entered_position_ids
                FROM public.copy_agent_state
                """
            )
            # Pre-fetch every trader_id this contest has ever paper-traded
            # against, regardless of whether it's still open. This is the
            # canonical source for "have we entered this trader_id before"
            # — superior to a per-agent JSONB column because it's persisted
            # on every trade entry/close path that already exists.
            try:
                done_rows = await conn.fetch(
                    """
                    SELECT DISTINCT tracked_position_id
                    FROM competition_trades
                    WHERE contest_id = 'copy-trade-scenarios'
                      AND tracked_position_id IS NOT NULL
                    """
                )
                for r in done_rows:
                    tid = r.get("tracked_position_id")
                    if tid is not None:
                        self._entered_positions.add(int(tid))
            except Exception as exc:
                # competition_trades table may not exist on a fresh install
                logger.debug("competition_trades scan skipped: %s", exc)

        for r in rows:
            agent_name = ID_TO_NAME.get(r["agent_id"])
            if not agent_name or agent_name not in self._agents:
                continue
            agent = self._agents[agent_name]
            agent["balance"] = float(r["balance"])
            agent["starting"] = float(r["starting_balance"])
            agent["total_pnl"] = float(r["total_pnl"])
            agent["total_fees"] = float(r["total_fees"])
            agent["wins"] = int(r["wins"])
            agent["losses"] = int(r["losses"])
            agent["trades"] = int(r["trades"])

            ops_raw = r["open_positions"] or []
            if isinstance(ops_raw, str):
                ops_raw = json.loads(ops_raw)
            # Convert isoformat strings back to datetime where _check_exits
            # expects a comparable timestamp.
            for pos in ops_raw:
                ts = pos.get("entered_at")
                if isinstance(ts, str):
                    try:
                        pos["entered_at"] = datetime.fromisoformat(ts)
                    except ValueError:
                        pass
            agent["open_positions"] = ops_raw

            for pos in ops_raw:
                tid = pos.get("trader_id")
                if tid is not None:
                    self._entered_positions.add(int(tid))
            loaded += 1

        logger.info(
            "copy_agent_state: loaded persisted state for %d agents "
            "(%d trader_ids in entered set)",
            loaded, len(self._entered_positions),
        )

    def _agent_uses_optimal(self, name: str) -> bool:
        return "Opt" in name

    def _agent_mode(self, name: str) -> str:
        for an, _, _, mode in AGENTS:
            if an == name:
                return mode
        return "spot_seq"

    async def _get_new_open_positions(self):
        """Fetch trader positions that just opened and we haven't entered yet.

        Round-7 persistence (Phase 2 of the copy-agent state fix,
        2026-05-24): the previous version filtered
        ``WHERE signal_timestamp >= self._started_at`` — the intent was
        "don't back-fill stale signals into the competition", but the side
        effect was that **every restart orphaned every currently-open
        trader position**: the daemon couldn't see them, so it never
        entered them into any agent, so the dashboard showed 9 LIVE
        positions in the trader feed but 0 across every copy agent.

        New behaviour (safe because Phase 1 persists `entered_position_ids`
        in `copy_agent_state` and `competition_trades` is the canonical
        record of "have we paper-traded this trader_id before"):

          * Drop the `_started_at` filter entirely. Any currently-open
            trader position is fair game.
          * Cap the lookback at 7 days so a one-time orphan reattach
            doesn't suddenly resurrect ancient stale rows that the
            trader closed manually outside our system.
          * `_load_state()` rebuilds `self._entered_positions` from BOTH
            persisted open paper-positions AND historical
            `competition_trades`, so the final filter
            (``id not in self._entered_positions``) prevents double-entry.

        Net effect: after a restart, the next tick paper-enters every
        currently-open trader position into all 7 agents. After Phase 3
        also runs (one-shot historical backfill), realised P&L from the
        past 4 days is reconstructed too.
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT tp.id, tp.instrument_symbol, tp.direction,
                       tp.entry_price, tp.stop_loss, tp.take_profit_1,
                       tp.signal_timestamp, tp.actor_id, tp.notional_usd
                FROM tracked_positions tp
                WHERE tp.status = 'open'
                  AND tp.entry_price > 0


                  AND tp.signal_timestamp >= NOW() - INTERVAL '7 days'
                  AND (tp.actor_id LIKE 'jarvais_trader_%' OR tp.actor_id = 'jarvais_chart_hacker' OR tp.actor_id = 'jarvais_rose_ch')
                ORDER BY tp.signal_timestamp DESC
                LIMIT 200
            """)
        return [dict(r) for r in rows if r["id"] not in self._entered_positions]

    async def _get_closed_positions(self):
        """Fetch trader positions that closed, where we still have open paper positions."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            trader_ids = set()
            for agent in self._agents.values():
                for pos in agent["open_positions"]:
                    trader_ids.add(pos["trader_id"])
            if not trader_ids:
                return {}
            rows = await conn.fetch("""
                SELECT id, exit_price, outcome, closed_at
                FROM tracked_positions
                WHERE status = 'closed' AND id = ANY($1::int[])
            """, list(trader_ids))
        return {r["id"]: dict(r) for r in rows}

    async def _get_candles(self, symbol: str, since: datetime):
        """Get recent 1m candles for a symbol.

        Tries multiple symbol forms (with/without :USDT perp suffix,
        slash/no-slash) because tracked_positions stores perp-suffixed
        forms like XAU/USDT:USDT while the instruments table uses spot
        forms like XAU/USDT.
        """
        candidates = _symbol_lookup_candidates(symbol)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            for cand_sym in candidates:
                rows = await conn.fetch("""
                    SELECT c.timestamp, c.high, c.low, c.close
                    FROM candles c JOIN instruments i ON i.id = c.instrument_id
                    WHERE i.symbol = $1 AND c.timeframe = '1m'
                      AND c.timestamp >= $2
                    ORDER BY c.timestamp
                    LIMIT 10000
                """, cand_sym, since)
                if rows:
                    logger.debug("candles found for %s (tried %s)", cand_sym, symbol)
                    return [{"ts": r["timestamp"], "high": float(r["high"]),
                             "low": float(r["low"]), "close": float(r["close"])} for r in rows]
            logger.debug("no candles for %s (tried %s)", symbol, candidates)
        return []

    async def _enter_agent_position(self, agent_name: str, trader_pos: dict):
        """Create a paper position for one agent, sizing by their mode rules."""
        agent = self._agents[agent_name]
        mode = self._agent_mode(agent_name)
        use_opt = self._agent_uses_optimal(agent_name)
        sym = trader_pos["instrument_symbol"]
        entry = float(trader_pos["entry_price"])
        direction = trader_pos["direction"]
        orig_sl = float(trader_pos.get("stop_loss") or 0)
        orig_tp = float(trader_pos.get("take_profit_1") or 0)
        # Default SL/TP if not provided by trader
        if orig_sl <= 0:
            orig_sl = entry * 0.95 if direction == "long" else entry * 1.05
        if orig_tp <= 0:
            orig_tp = entry * 1.05 if direction == "long" else entry * 0.95
        sl, tp = get_sl_tp(entry, direction, orig_sl, orig_tp, sym, use_opt)

        # Position sizing by mode
        if "spot_seq" in mode:
            # Full balance, sequential
            if agent["open_positions"]:
                return  # Already in a trade
            allocated = agent["balance"]
            leverage = 1.0
        elif mode == "spot_lev_3x":
            # Full balance, sequential, 3x leverage
            if agent["open_positions"]:
                return
            allocated = agent["balance"]
            leverage = 3.0
        elif mode == "lev_parallel_3pct":
            # 3% risk per trade, max 33 concurrent
            if len(agent["open_positions"]) >= 33:
                return
            allocated = agent["balance"] * 0.03
            sl_dist = abs(entry - sl) / entry if entry > 0 else 0.05
            if sl_dist < 0.005:
                sl_dist = 0.005
            raw_lev = (1.0 / sl_dist) * 0.97
            leverage = min(raw_lev, 100.0)
        else:
            # Leveraged: 5% risk per trade, max 20 concurrent
            if len(agent["open_positions"]) >= 20:
                return
            allocated = agent["balance"] * 0.05
            sl_dist = abs(entry - sl) / entry if entry > 0 else 0.05
            if sl_dist < 0.005:
                sl_dist = 0.005
            raw_lev = (1.0 / sl_dist) * 0.97
            leverage = min(raw_lev, 100.0)

        if agent_name.startswith("CH:"):
            logger.info("CH entering %s dir=%s entry=%s sl=%s tp=%s", sym, direction, entry, sl, tp)
                # Entry fee — load from instruments table per symbol
        fees = await self._load_instrument_fees(sym)
        entry_fee = allocated * leverage * fees["maker_bps"] / 10000.0
        agent["balance"] -= entry_fee
        agent["total_fees"] += entry_fee

        agent["open_positions"].append({
            "trader_id": trader_pos["id"],
            "symbol": sym,
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "allocated": allocated,
            "leverage": leverage,
            "be_locked": False,
            "be_price": entry * 1.05 if direction == "long" else entry * 0.95,
            "entered_at": datetime.now(timezone.utc),
        })

        # Insert open position into competition_trades so the dashboard can see it
        try:
            await self._log_trade_open(agent_name, agent["open_positions"][-1], sym)
        except Exception:
            pass

        # Round-7 persistence: save the agent's new open-position list so a
        # crash before the next contest score push doesn't lose this entry.
        try:
            await self._save_agent(agent_name)
        except Exception as exc:
            logger.warning("copy_agent_state save (open) failed for %s: %s", agent_name, exc)

    async def _sync_sl_tp(self):
        """Re-read SL/TP from tracked_positions for all in-memory positions."""
        pool = await self._ensure_pool()
        for agent_name, agent in self._agents.items():
            for pos in agent["open_positions"]:
                row = await pool.fetch_one(
                    "SELECT stop_loss, take_profit_1 FROM tracked_positions WHERE id = $1",
                    (pos["trader_id"],),
                )
                if row:
                    if row["stop_loss"] and float(row["stop_loss"] or 0) > 0:
                        pos["sl"] = float(row["stop_loss"])
                    if row["take_profit_1"] and float(row["take_profit_1"] or 0) > 0:
                        pos["tp"] = float(row["take_profit_1"])

    async def _check_agent_positions(self, agent_name: str):
        """Check all open positions for one agent against recent candles."""
        agent = self._agents[agent_name]
        if not agent["open_positions"]:
            return

        # Group by symbol for candle fetching
        by_symbol = defaultdict(list)
        for pos in agent["open_positions"]:
            by_symbol[pos["symbol"]].append(pos)

        for sym, positions in by_symbol.items():
            since = min(p["entered_at"] for p in positions)
            candles = await self._get_candles(sym, since)
            if not candles:
                continue

            to_close = []
            for pos in positions:
                for c in candles:
                    hi, lo = c["high"], c["low"]
                    # Check SL
                    if pos["direction"] == "long" and lo <= pos["sl"]:
                        exit_px = pos["sl"] * (1 - FEES["slippage_bps"] / 10000.0)
                        to_close.append((pos, exit_px, "sl", c["ts"]))
                        break
                    elif pos["direction"] == "short" and hi >= pos["sl"]:
                        exit_px = pos["sl"] * (1 + FEES["slippage_bps"] / 10000.0)
                        to_close.append((pos, exit_px, "sl", c["ts"]))
                        break
                    # Check TP
                    if pos["direction"] == "long" and hi >= pos["tp"]:
                        exit_px = pos["tp"] * (1 - FEES["slippage_bps"] / 10000.0)
                        to_close.append((pos, exit_px, "tp", c["ts"]))
                        break
                    elif pos["direction"] == "short" and lo <= pos["tp"]:
                        exit_px = pos["tp"] * (1 + FEES["slippage_bps"] / 10000.0)
                        to_close.append((pos, exit_px, "tp", c["ts"]))
                        break
                    # Check BE lock threshold (+5%)
                    if not pos["be_locked"]:
                        if pos["direction"] == "long" and hi >= pos["be_price"]:
                            pos["be_locked"] = True
                            pos["sl"] = pos["entry"] * 1.001  # SL→BE+fees
                            pos["leverage"] = 100.0
                        elif pos["direction"] == "short" and lo <= pos["be_price"]:
                            pos["be_locked"] = True
                            pos["sl"] = pos["entry"] * 0.999
                            pos["leverage"] = 100.0

            for pos, exit_px, reason, ts in to_close:
                await self._close_agent_position(agent_name, pos, exit_px, reason)

    async def _close_agent_position(self, agent_name: str, pos: dict, exit_px: float, reason: str):
        """Close one paper position and update agent balance."""
        agent = self._agents[agent_name]
        if pos not in agent["open_positions"]:
            return

        agent["open_positions"].remove(pos)

        # Gross PnL
        gross = compute_pnl(pos["entry"], exit_px, pos["direction"],
                            pos["allocated"] * pos["leverage"])
        # Exit fee — load from instruments per symbol
        fees = await self._load_instrument_fees(pos["symbol"])
        fee_rate = fees["taker_bps"] if reason == "sl" else fees["maker_bps"]
        exit_fee = pos["allocated"] * pos["leverage"] * fee_rate / 10000.0
        net = gross - exit_fee

        agent["balance"] += net
        agent["total_pnl"] += net
        agent["total_fees"] += exit_fee
        agent["trades"] += 1
        if net > 0:
            agent["wins"] += 1
        else:
            agent["losses"] += 1

        # Log trade to competition_trades table
        asyncio.ensure_future(self._log_trade(agent_name, pos, exit_px, net, reason))

        logger.info("%s: closed %s %s @ %.4f (reason=%s) PnL=$%.2f balance=$%.2f",
                     agent_name, pos["symbol"], pos["direction"],
                     exit_px, reason, net, agent["balance"])

        # Round-7 persistence: save the agent's updated balance / wins /
        # losses / total_pnl / open_positions so a crash before the next
        # contest score push doesn't roll back this close.
        try:
            await self._save_agent(agent_name)
        except Exception as exc:
            logger.warning("copy_agent_state save (close) failed for %s: %s", agent_name, exc)

    async def _log_trade_open(self, agent_name, pos, sym):
        """Insert an open position into competition_trades (exit_price=NULL)."""
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO competition_trades
                    (contest_id, agent_id, symbol, direction, entry_price,
                     sl_price, tp_price, allocated, leverage, pnl, fees, exit_reason,
                     entered_at, tracked_position_id)
                    VALUES ('copy-trade-scenarios', $1, $2, $3, $4, $5, $6, $7, $8, 0, 0, 'open',
                            $9, $10)
                """, NAME_TO_ID.get(agent_name, agent_name), sym, pos["direction"],
                    pos["entry"], pos["sl"], pos["tp"], pos["allocated"], pos["leverage"],
                    pos["entered_at"], pos.get("trader_id"))
        except Exception:
            pass

    async def _log_trade(self, agent_name, pos, exit_px, pnl, reason):
        """Write closed trade to competition_trades table."""
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                # Update the open trade row that was created on entry
                # Also copy over signal_interpretation_id now that we know it
                result = await conn.execute("""
                    UPDATE competition_trades
                    SET exit_price = $1, pnl = $2, exit_reason = $3, exited_at = NOW()
                    WHERE contest_id = 'copy-trade-scenarios'
                      AND agent_id = $4
                      AND symbol = $5
                      AND direction = $6
                      AND entry_price = $7
                      AND exit_price IS NULL
                """, exit_px, pnl, reason,
                    NAME_TO_ID.get(agent_name, agent_name), pos["symbol"],
                    pos["direction"], pos["entry"])
                # If no row matched (legacy), fall back to INSERT
                if result == "UPDATE 0":
                    await conn.execute("""
                        INSERT INTO competition_trades
                        (contest_id, agent_id, symbol, direction, entry_price, exit_price,
                         sl_price, tp_price, allocated, leverage, pnl, fees, exit_reason, entered_at)
                        VALUES ('copy-trade-scenarios', $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    """, NAME_TO_ID.get(agent_name, agent_name), pos["symbol"], pos["direction"],
                        pos["entry"], exit_px, pos["sl"], pos["tp"],
                        pos["allocated"], pos["leverage"], pnl, 0.0, reason, pos["entered_at"])
        except Exception:
            pass

    async def _close_trader_exits(self, closed: dict):
        """Close agent positions whose trader exited."""
        for agent_name in self._agents:
            agent = self._agents[agent_name]
            for pos in list(agent["open_positions"]):
                tid = pos["trader_id"]
                if tid in closed:
                    exit_info = closed[tid]
                    await self._close_agent_position(
                        agent_name, pos,
                        float(exit_info["exit_price"] or pos["entry"]),
                        exit_info.get("outcome", "closed") or "closed"
                    )

    async def _update_contest_scores(self):
        """Push current agent balances to contest_participants."""
        # Compute unrealized P&L from open positions using latest candle prices
        unrealized = await self._compute_unrealized_pnl()
        
        pool = await self._ensure_pool()
        for agent_name, agent in self._agents.items():
            agent_id = NAME_TO_ID.get(agent_name)
            if not agent_id:
                continue
            wc = agent["wins"]
            lc = agent["losses"]
            tc = agent["trades"]
            upnl = unrealized.get(agent_name, 0.0)
            score = {
                "equity": round(agent["balance"], 2),
                "total_realized_pnl_usd": round(agent["total_pnl"], 2),
                "unrealized_pnl_usd": round(upnl, 2),
                "return_pct": round((agent["balance"] / agent["starting"] - 1) * 100, 1),
                "total_trades": tc,
                "winning_trades": wc,
                "losing_trades": lc,
                "win_rate": round(wc / max(tc, 1), 3),
                "open_positions": len(agent["open_positions"]),
                "total_fees": round(agent["total_fees"], 2),
                "starting_balance_usd": agent["starting"],
            }
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE contest_participants
                    SET scores = $1::jsonb,
                        equity_usd = $2,
                        realized_pnl_usd = $3,
                        unrealized_pnl_usd = $4,
                        return_pct = $5,
                        win_rate = $6,
                        total_trades = $7,
                        open_positions = $8,
                        total_fees_usd = $9
                    WHERE contest_id = 'copy-trade-scenarios' AND agent_id = $10
                """, json.dumps(score), score["equity"], score["total_realized_pnl_usd"],
                    score.get("unrealized_pnl_usd", 0), score["return_pct"],
                    score["win_rate"], score["total_trades"],
                    score["open_positions"], score["total_fees"], agent_id)

            # Also update banker balance for this agent's wallet
            try:
                await banker_update(agent_id, "jarvais", score["equity"],
                                    score["equity"], 0, upnl)
            except Exception:
                pass

        # Round-7 persistence: bulk save as a backstop. The hot path
        # (`_save_agent` after open/close) is where most writes happen,
        # but if a path raised before reaching `_save_agent` we'll still
        # reconcile here on the next 30-second tick.
        try:
            await self._save_state()
        except Exception as exc:
            logger.warning("copy_agent_state bulk save failed: %s", exc)

    async def _compute_unrealized_pnl(self) -> Dict[str, float]:
        """Compute unrealized P&L for each agent's open positions using latest candle prices."""
        result: Dict[str, float] = {}
        # Collect unique symbols across all agents
        symbols = set()
        for agent in self._agents.values():
            for pos in agent["open_positions"]:
                symbols.add(pos["symbol"])
        if not symbols:
            return result
        
        # Fetch latest candle close for each symbol
        pool = await self._ensure_pool()
        prices: Dict[str, float] = {}
        try:
            async with pool.acquire() as conn:
                for sym in symbols:
                    for cand_sym in _symbol_lookup_candidates(sym):
                        row = await conn.fetchrow("""
                            SELECT c.close FROM candles c
                            JOIN instruments i ON i.id = c.instrument_id
                            WHERE i.symbol = $1 AND c.timeframe = '1m'
                            ORDER BY c.timestamp DESC LIMIT 1
                        """, cand_sym)
                        if row:
                            prices[sym] = float(row["close"])
                            break
        except Exception:
            pass
        
        # Fallback: for symbols with no candles, use tracked_positions.current_price
        unpriced = [s for s in symbols if s not in prices]
        if unpriced:
            try:
                async with pool.acquire() as conn:
                    for sym in unpriced:
                        row = await conn.fetchrow(
                            "SELECT current_price FROM tracked_positions WHERE instrument_symbol = $1 AND current_price > 0 ORDER BY updated_at DESC LIMIT 1",
                            sym,
                        )
                        if row:
                            prices[sym] = float(row["current_price"])
            except Exception:
                pass
        
        if not prices:
            return result
        
        for agent_name, agent in self._agents.items():
            total = 0.0
            for pos in agent["open_positions"]:
                px = prices.get(pos["symbol"])
                if px and pos["entry"] > 0:
                    if pos["direction"] == "long":
                        total += pos["allocated"] * pos["leverage"] * (px / pos["entry"] - 1.0)
                    else:
                        total += pos["allocated"] * pos["leverage"] * (1.0 - px / pos["entry"])
            result[agent_name] = total
        return result

    async def _load_optimal_multipliers(self):
        """Refresh optimal SL/TP multipliers from system_config."""
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT config_key, config_value FROM system_config WHERE namespace = 'auto_opt'"
                )
            for r in rows:
                v = r["config_value"]
                if isinstance(v, str):
                    v = json.loads(v)
                OPTIMAL[r["config_key"]] = (float(v["sl_mult"]), float(v["tp_mult"]))
        except Exception:
            pass  # use whatever was loaded previously

    async def _load_instrument_fees(self, symbol: str) -> dict:
        """Load maker/taker fees and funding rates for a symbol from instruments table.

        Tries multiple symbol forms (with/without :USDT perp suffix) because
        the instruments table may use a different form than tracked_positions.
        """
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                found = False
                for fee_sym in _symbol_lookup_candidates(symbol):
                    row = await conn.fetchrow("""
                        SELECT maker_fee_pct, taker_fee_pct, spread_pct,
                               overnight_funding_long_pct, overnight_funding_short_pct
                        FROM instruments WHERE symbol = $1 AND is_active = TRUE
                        LIMIT 1
                    """, fee_sym)
                    if row:
                        return {
                            "maker_bps": int(float(row["maker_fee_pct"] or 0) * 10000),
                            "taker_bps": int(float(row["taker_fee_pct"] or 0) * 10000),
                            "spread_bps": int(float(row["spread_pct"] or 0) * 10000),
                            "funding_long_bps": int(float(row["overnight_funding_long_pct"] or 0) * 10000),
                            "funding_short_bps": int(float(row["overnight_funding_short_pct"] or 0) * 10000),
                        }
            if row is None:
                logger.debug("no instrument fees for %s (tried %s)", symbol,
                             _symbol_lookup_candidates(symbol))
        except Exception:
            pass
        return {"maker_bps": 1, "taker_bps": 5}  # Bybit default fallback

    async def tick(self):
        """Single monitoring cycle."""
        logger.info("Tick start")
        # 0. Refresh optimal multipliers from DB
        await self._load_optimal_multipliers()
        # 0.5 Sync SL/TP from tracked_positions (may have been updated by LLM re-extraction)
        await self._sync_sl_tp()
        # 1. Enter new trader positions
        new_positions = await self._get_new_open_positions()
        for pos in new_positions:
            for agent_name in self._agents:
                try:
                    # Agent → position source routing
                    is_ch_pos = (pos.get("actor_id") == "jarvais_chart_hacker")
                    is_ch_agent = agent_name.startswith("CH:")
                    is_rose_pos = (pos.get("actor_id") == "jarvais_rose_ch")
                    is_rose_agent = agent_name.startswith("Rose")
                    # Regular agents skip ChartHacker and Rose positions
                    if not is_ch_agent and not is_rose_agent and (is_ch_pos or is_rose_pos):
                        continue
                    # CH agent only takes chart_hacker positions
                    if is_ch_agent and not is_ch_pos:
                        continue
                    # Rose agent only takes rose_ch positions
                    if is_rose_agent and not is_rose_pos:
                        continue
                    if pos["entry_price"] and float(pos["entry_price"]) > 0:
                        await self._enter_agent_position(agent_name, pos)
                except Exception as exc:
                    logger.warning("Agent %s failed to enter pos %s: %s", agent_name, pos.get("id"), exc)
            self._entered_positions.add(pos["id"])

        if new_positions:
            names = [a for a in self._agents.keys()]; logger.info("Entered %d new positions across agents: %s", len(new_positions), names)

        # 2. Check each agent's SL/TP against candles
        for agent_name in self._agents:
            await self._check_agent_positions(agent_name)

        # 3. Close positions where trader exited
        closed = await self._get_closed_positions()
        if closed:
            await self._close_trader_exits(closed)

        # 4. Update contest scores
        await self._update_contest_scores()

        # 5. Heartbeat for cron-canary watchdog
        try:
            from shared.intelligence.heartbeat import record_heartbeat
            await record_heartbeat(
                agent_id="copy-trade-monitor",
                status="ok",
                expected_interval_seconds=POLL_INTERVAL_S,
            )
        except Exception:
            pass

        logger.info("Tick complete")
    async def run_forever(self):
        logger.info("LiveCopyTradeMonitor starting (poll=%ds, %d agents)", POLL_INTERVAL_S, len(self._agents))
        # Round-7 persistence: hydrate in-memory agent state from
        # public.copy_agent_state BEFORE the first tick. Without this,
        # every restart wipes balance / wins / losses / open_positions to
        # round-defaults — which is what produced the "all 7 agents stuck
        # at $1000 / $0 / 0 trades" dashboard symptom.
        try:
            await self._load_state()
        except Exception as exc:
            logger.exception(
                "copy_agent_state: load failed (%s) — continuing with "
                "in-memory defaults; running balances may be lost.", exc,
            )
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                logger.exception("Tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
        logger.info("LiveCopyTradeMonitor stopped")

    def stop(self):
        self._stop.set()


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env()
    monitor = LiveCopyTradeMonitor()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, monitor.stop)
    await monitor.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
