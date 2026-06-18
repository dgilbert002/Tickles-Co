"""
demo_bridge.py - mirrors paper competition SIGNALS to demo exchange accounts.

Watches tracked_positions for new pending/open entries and places LIMIT ORDERS
on assigned demo accounts at the signal's entry_price BEFORE the paper trade triggers.

Design:
  - Polls tracked_positions every POLL_INTERVAL_S for new pending/open positions
  - For each mapped agent's tracked_position, places a limit order on demo
  - Limit orders fill naturally when price hits entry (same as paper)
  - Cancels demo limit orders when tracked_position is cancelled/expired
  - Attaches SL/TP levels from the signal

Usage:
  python3 -m shared.daemons.demo_bridge
"""
import asyncio, logging, os, re, signal, sys, time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/opt/tickles")
from shared.utils.db import DatabasePool
from shared.execution.ccxt_adapter import CcxtExecutionAdapter
from shared.execution.protocol import (
    ExecutionIntent, DIRECTION_LONG, DIRECTION_SHORT, ORDER_TYPE_LIMIT,
)
from shared.intelligence.copy_trade_monitor import (
    _symbol_lookup_candidates, _distance_to_entry as _dist_to_entry, _entry_touched,
)
from shared.intelligence.position_monitor import fetch_latest_price as _fetch_price
# Phase 1 (2026-05-29): forensic transaction log - every mirror event is
# appended to /opt/tickles/shared/logs/paper_demo.log for the dashboard's
# "Paper vs Demo vs Live" live-log viewer and shell-side grep.
from shared.daemons.demo_forensic_log import flog

LOG = logging.getLogger("demo.bridge")
POLL_INTERVAL_S = int(os.environ.get("DEMO_BRIDGE_POLL_S", "15"))

# Strip options-contract suffixes (e.g. -260531-90-P) before routing to CCXT.
_OPT_RE_DEMO = re.compile(r"-\d{6}-\d+-[PC]$")

# Phase 1 (2026-05-29) FIX - position sizing.
#
# THE BUG: every tracked_position carries notional_usd = 1000 (the full paper
# wallet). The bridge mirrored that 1:1 as the order NOTIONAL and committed
# margin = notional / leverage onto each ~1000 USD demo account. Because the
# bridge fires for EVERY signal on EVERY account, those orders STACKED until
# the account margin was completely exhausted - that is why the demo accounts
# ended up with e.g. a BNB position of 5120 notional and NEGATIVE free margin,
# after which every new order is rejected with "ab not enough" (retCode
# 110007). In other words it committed ~100% (and more) of the wallet.
#
# THE FIX: size each demo order the same way the paper agents do - commit a
# small FIXED margin per trade (the 5%-risk paper agents allocate ~$50 of a
# $1000 wallet), then derive notional = margin * leverage. This keeps each
# order tiny relative to the wallet, leaves headroom for fees/slippage, and
# lets ~20 trades coexist instead of 2-3 maxing the account out.
#
# DEMO_MARGIN_USD  = margin committed per demo trade (default $50 ≈ 5% of $1000)
# DEMO_NOTIONAL_SAFETY_PCT = extra headroom shave applied on top (fees/slippage)
DEMO_MARGIN_USD = float(os.environ.get("DEMO_MARGIN_USD", "50"))
DEMO_NOTIONAL_SAFETY_PCT = float(os.environ.get("DEMO_NOTIONAL_SAFETY_PCT", "0.99"))

# ---------------------------------------------------------------------------
# Phase 6 (2026-05-29) - ACCURATE per-agent demo sizing.
#
# WHY: the flat DEMO_MARGIN_USD ($50) sizing above was safe but NOT faithful.
# A demo account that mirrors `copy_spot_lev_3x` (3x notional on the full
# wallet) was being sized identically to one mirroring a 5%-risk agent, so the
# paper-vs-demo slippage/fee comparison was apples-to-oranges. To be a true
# forensic mirror, each demo order must reproduce the EXACT sizing rule of the
# paper agent assigned to that demo account, computed against the demo
# account's REAL balance.
#
# Single source of truth for these rules: shared/intelligence/copy_trade_monitor.py
# (AGENTS list + the "Position sizing by mode" block). Kept in sync here.
#
# 2026-06-16: sizing knobs (risk%, leverage_cap, spot_lev_3x) now read from
# the same copy_sizing_config DB table as the paper engine.  Changes in the
# Settings UI apply to both layers simultaneously.
from shared.intelligence.copy_sizing_config import get_value as _sizing_get

# agent_id → sizing mode
AGENT_MODE: Dict[str, str] = {
    "copy_spot_seq":        "spot_seq",     # full wallet, 1x, sequential
    "copy_opt_spot_seq":    "spot_seq",
    "copy_charthacker":     "spot_seq_ch",  # mirrors chart_hacker paper agent: 3% risk, dynamic leverage from SL (NOT full wallet 1x)
    "copy_rose_a":          "spot_seq",
    "copy_spot_lev_3x":     "spot_lev_3x",  # full wallet, 3x, sequential
    "copy_lev_3pct":        "lev_3pct",     # 3% risk, lev from SL, parallel
    "copy_lev_parallel":    "lev_5pct",     # 5% risk, lev from SL, parallel
    "copy_lev_be_lock":     "lev_5pct",
    "copy_opt_lev_parallel":"lev_5pct",
    "copy_opt_lev_be_lock": "lev_5pct",
    "copy_rose_b":          "lev_5pct",
    "copy_rose_c":          "lev_5pct",
}
# Max concurrent OPEN demo positions per account, by mode - mirrors the paper
# agent's concurrency rule so the demo never over-places and exhausts margin.
MODE_MAX_CONCURRENT: Dict[str, int] = {
    "spot_seq": 1, "spot_seq_ch": 33, "spot_lev_3x": 1, "lev_3pct": 33, "lev_5pct": 20,
}
# Risk % / leverage knobs - read from copy_sizing_config (same DB table as
# the paper engine) so Settings UI changes affect both layers.  The module-level
# defaults here only apply during import before the first tick's DB refresh;
# each tick() calls _refresh_sizing() which pulls the live values.
_DEMO_SIZING_CACHE: Dict[str, Any] = {
    "risk_pct_5": 5.0, "risk_pct_3": 3.0,
    "leverage_cap": 100.0, "spot_lev_3x": 3.0,
}
# Fallback balance when the exchange balance can't be fetched (~paper wallet).
DEMO_FALLBACK_BALANCE = float(os.environ.get("DEMO_FALLBACK_BALANCE", "1000.0"))

# ── Smart Queue proximity thresholds ──
# Place when price is within this % of entry (with wick-touch verification).
SMART_QUEUE_PLACE_PCT = float(os.environ.get("SMART_QUEUE_PLACE_PCT", "5.0"))
# Close/cancel when price moves beyond this % of entry (frees margin).
SMART_QUEUE_CLOSE_PCT = float(os.environ.get("SMART_QUEUE_CLOSE_PCT", "10.0"))
# How many 1m candles to check for entry-touch verification.
SMART_QUEUE_TOUCH_CANDLES = int(os.environ.get("SMART_QUEUE_TOUCH_CANDLES", "60"))


class DemoBridge:
    def __init__(self):
        self._pool = None
        self._adapter = CcxtExecutionAdapter(demo_trading=True, default_type="swap")
        self._stop = asyncio.Event()

        self._mirrored: set = set()        # tracked_position IDs already mirrored
        self._orders: Dict[int, Dict] = {} # tracked_position_id → {account: order_id}
        self._acct_balance: Dict[str, float] = {}  # "{exchange}/{account}" → USDT balance (refreshed each tick)


    async def _ensure_pool(self):
        if self._pool is None:
            self._pool = await DatabasePool.get_instance()
        return self._pool

    async def _load_mappings(self) -> Dict[str, List[Dict]]:
        """Load agent→account mappings: {agent_id: [{exchange, account_name}, ...]}

        Also populates self._acct_agent - the REVERSE map keyed by
        "{exchange}/{account_name}" → agent_id - so every demo order we place
        can be stamped with the agent it belongs to (the demo account follows
        that agent's scenario). Mappings are currently 1 account ↔ 1 agent.
        """
        pool = await self._ensure_pool()
        rows = await pool.fetch_all("""
            SELECT cae.agent_id, ea.exchange, ea.account_name
            FROM public.competition_agent_exchanges cae
            JOIN public.exchange_accounts ea ON ea.id = cae.exchange_account_id
            WHERE cae.is_active = TRUE AND ea.is_active = TRUE
              AND cae.competition_id = 'copy-trade-scenarios'
            ORDER BY cae.priority
        """)
        mappings: Dict[str, List[Dict]] = {}
        self._acct_agent = {}
        for r in rows:
            mappings.setdefault(r["agent_id"], []).append({
                "exchange": r["exchange"], "account_name": r["account_name"],
            })
            self._acct_agent[f"{r['exchange']}/{r['account_name']}"] = r["agent_id"]
        return mappings

    def _agent_for_account(self, exchange: str, account_name: str) -> Optional[str]:
        """Which competition agent is this demo account mirroring? (reverse map)"""
        return getattr(self, "_acct_agent", {}).get(f"{exchange}/{account_name}")

    async def _refresh_balances(self, mappings: Dict[str, List[Dict]]):
        """Pull the REAL USDT balance for every mapped demo account once per tick.

        The accurate sizing model derives each order's notional from the demo
        account's actual balance (so a demo order reproduces what the paper
        agent would do with the same money). We cache it per tick to avoid one
        balance call per signal. On failure we keep the last value, falling back
        to DEMO_FALLBACK_BALANCE the first time.
        """
        seen = set()
        for accts in mappings.values():
            for a in accts:
                key = f"{a['exchange']}/{a['account_name']}"
                if key in seen:
                    continue
                seen.add(key)
                try:
                    bal = await self._adapter.fetch_balance(
                        exchange=a["exchange"], account_name=a["account_name"])
                    usdt = float(bal.get("USDT") or 0.0)
                    # Always update - even zero balance must be recorded
                    # so sizing sees the real free balance, not the fallback.
                    if "USDT" in bal:
                        self._acct_balance[key] = usdt
                    # Persist to DB so dashboard/cron see live balance
                    pool = await self._ensure_pool()
                    await pool.execute(
                        "UPDATE public.exchange_accounts "
                        "SET last_balance = $1, last_tested_at = NOW() "
                        "WHERE exchange = $2 AND account_name = $3",
                        (round(usdt, 2), a["exchange"], a["account_name"]))
                except Exception as exc:
                    LOG.debug("balance refresh %s failed: %s", key, exc)
                self._acct_balance.setdefault(key, DEMO_FALLBACK_BALANCE)

    @staticmethod
    def _demo_sizing(key: str, default: float) -> float:
        """Read a sizing knob from the DB-refreshed cache (same source as paper)."""
        return float(_DEMO_SIZING_CACHE.get(key, default))

    def _compute_demo_size(self, agent_id: Optional[str], balance: float,
                           entry: float, sl: Optional[float]):
        """Reproduce the mapped paper agent's sizing rule against `balance`.

        Mirrors shared/intelligence/copy_trade_monitor.py exactly:
          - spot_seq      : allocated = full balance,        leverage = 1
          - spot_lev_3x   : allocated = full balance,        leverage = 3 (cfg)
          - lev_3pct      : allocated = 3% of balance,       leverage = 1/sl_dist (cap)
          - lev_5pct      : allocated = 5% of balance,       leverage = 1/sl_dist (cap)
        Notional = allocated * leverage. `allocated` is shaved by the safety pct
        so margin stays just under the free balance (fee/slippage headroom).
        Returns (mode, allocated, leverage_int, notional).
        """
        mode = AGENT_MODE.get(agent_id or "", "lev_5pct")
        bal = balance if balance is not None and balance > 0 else DEMO_FALLBACK_BALANCE
        # Leverage from SL distance - identical formula to the paper agents.
        if sl is not None and sl > 0 and entry > 0:
            sl_dist = abs(entry - sl) / entry
        else:
            sl_dist = 0.05
        if sl_dist < 0.005:
            sl_dist = 0.005
        # Liquidation-safe leverage - mirrors copy_trade_monitor.lev_after_buffer:
        # L <= 1 / (sl_dist * safety + mmr) keeps the forced-liquidation price
        # strictly beyond the stop (the old (1/sl_dist)*0.97 put liquidation
        # BEFORE the stop for any stop tighter than ~5%).
        _liq_safety = self._demo_sizing("liq_safety", 1.15)
        _liq_mmr = self._demo_sizing("liq_mmr", 0.006)
        _lev_cap = self._demo_sizing("leverage_cap", 100.0)
        lev_from_sl = min(1.0 / (sl_dist * _liq_safety + _liq_mmr), _lev_cap)

        _risk3 = self._demo_sizing("risk_pct_3", 3.0) / 100.0
        _risk5 = self._demo_sizing("risk_pct_5", 5.0) / 100.0
        _spot3x = self._demo_sizing("spot_lev_3x", 3.0)

        if mode == "spot_seq":
            allocated, leverage = bal, 1.0
        elif mode == "spot_seq_ch":
            # chart_hacker copier: 3% risk, dynamic leverage from SL distance -
            # identical to copy_trade_monitor's spot_seq_ch branch.
            allocated, leverage = bal * _risk3, lev_from_sl
        elif mode == "spot_lev_3x":
            allocated, leverage = bal, _spot3x
        elif mode == "lev_3pct":
            allocated, leverage = bal * _risk3, lev_from_sl
        else:  # lev_5pct
            allocated, leverage = bal * _risk5, lev_from_sl

        allocated *= DEMO_NOTIONAL_SAFETY_PCT  # headroom for fees/slippage
        notional = allocated * leverage
        lev_int = max(1, min(leverage, _lev_cap))
        return mode, allocated, lev_int, notional

    async def _open_demo_count(self, exchange: str, account_name: str) -> int:
        """How many demo positions/orders are currently live for this account.

        Only counts RECENT orders (last 24h) toward the concurrency cap. Older
        pending limit orders are sitting at entry prices far from current market
        - they should NOT block new signals from being placed. The paper-level
        tracked_position timeout (7 days) handles stale-paper cleanup; the bridge
        cancels the corresponding demo order when the paper position expires.
        """
        pool = await self._ensure_pool()
        row = await pool.fetch_one(
            "SELECT COUNT(*) AS n FROM public.demo_orders "
            "WHERE exchange=%s AND account_name=%s "
            "AND (status='pending' AND ordered_at > NOW() - INTERVAL '24 hours' "
            "     OR (status='filled' AND closed_at IS NULL))",
            (exchange, account_name))
        return int(row["n"] if row else 0)

    async def _load_mirrored(self):
        """Load already-mirrored tracked_position IDs from persistent tracking.

        Phase 1 (2026-05-29) FIX - duplicate-order stacking on restart:
        previously this only excluded positions that had a PAPER fill
        (competition_trades). A pending signal whose paper leg had not yet
        filled was NOT excluded, so EVERY daemon restart re-placed a fresh
        resting limit order for it - duplicate orders piled up on the exchange
        and ate the account margin ("ab not enough" / "balance not enough").
        We now ALSO exclude any tracked_position that already has a
        pending/filled demo order, so restarts never duplicate. Rejected
        orders are intentionally NOT excluded so they can be retried after the
        underlying cause (balance/position-mode) is fixed.
        """
        pool = await self._ensure_pool()
        rows = await pool.fetch_all(
            "SELECT DISTINCT tracked_position_id FROM public.competition_trades "
            "WHERE contest_id = 'copy-trade-scenarios' AND tracked_position_id IS NOT NULL "
            "UNION "
            "SELECT DISTINCT tracked_position_id FROM public.demo_orders "
            "WHERE tracked_position_id IS NOT NULL AND status IN ('pending','filled','queued','cancelled','rejected')"
        )
        self._mirrored = {r["tracked_position_id"] for r in rows}
        LOG.info("Loaded %d already-mirrored tracked positions", len(self._mirrored))

    # ── Smart Queue helpers (delegate to paper's implementations) ──

    async def _queue_price(self, symbol: str):
        """Current price via paper's fetch_latest_price."""
        try:
            pool = await self._ensure_pool()
            return await _fetch_price(pool, symbol, None, "1m")
        except Exception as exc:
            LOG.debug("_queue_price failed for %s: %s", symbol, exc)
            return None

    async def _queue_candles(self, symbol: str, n: int = 60) -> list:
        """Recent candles via paper's _symbol_lookup_candidates."""
        try:
            pool = await self._ensure_pool()
            for cand_sym in _symbol_lookup_candidates(symbol):
                rows = await pool.fetch_all(
                    "SELECT c.timestamp, c.open, c.high, c.low, c.close "
                    "FROM public.candles c "
                    "JOIN public.instruments i ON i.id = c.instrument_id "
                    "WHERE i.symbol = $1 AND c.timeframe = '1m' "
                    "ORDER BY c.timestamp DESC LIMIT $2",
                    (cand_sym, n),
                )
                if rows:
                    return [dict(r) for r in reversed(rows)]
        except Exception as exc:
            LOG.debug("_queue_candles failed for %s: %s", symbol, exc)
        return []

    async def _get_new_signals(self) -> List[Dict]:
        """Get tracked_positions with pending/open status that haven't been mirrored."""
        pool = await self._ensure_pool()
        exclude = list(self._mirrored) if self._mirrored else [-1]
        rows = await pool.fetch_all(
            "SELECT tp.id, tp.actor_id, tp.instrument_symbol, tp.direction, "
            "tp.entry_price, tp.stop_loss, tp.take_profit_1, tp.status, "
            "tp.signal_timestamp, tp.notional_usd "
            "FROM tracked_positions tp "
            "WHERE tp.status IN ('pending', 'open') "
            "AND tp.entry_price > 0 "
            "AND tp.id != ALL($1::bigint[]) "
            "AND tp.signal_timestamp >= NOW() - INTERVAL '24 hours' "
            "ORDER BY tp.signal_timestamp ASC "
            "LIMIT 50",
            (exclude,)
        )
        return [dict(r) for r in rows]

    async def _get_cancelled_signals(self) -> List[Dict]:
        """Get mirrored signals that are now cancelled - cancel their demo orders."""
        if not self._orders:
            return []
        pool = await self._ensure_pool()
        ids = list(self._orders.keys())
        if not ids:
            return []
        rows = await pool.fetch_all(
            "SELECT id, status FROM tracked_positions "
            "WHERE id = ANY($1::bigint[]) AND status IN ('cancelled', 'expired', 'closed')",
            (ids,),
        )
        return [dict(r) for r in rows]

    async def _agent_for_actor(self, actor_id: str, mappings: Dict[str, List[Dict]]) -> Optional[str]:
        """Map tracked_position actor_id to competition agent_id."""
        # ChartHacker → copy_charthacker
        if actor_id == 'jarvais_chart_hacker':
            return 'copy_charthacker'
        # Rose → copy_rose_a (primary rose agent)
        if actor_id == 'jarvais_rose_ch':
            return 'copy_rose_a'
        # Traders → all regular agents (return the first mapped one, or pick specifically)
        # For bridge purposes, we need to know WHICH agent is assigned to mirror
        # We check all mapped agents to see if they trade this signal type
        for agent_id in mappings:
            if actor_id.startswith('jarvais_trader_'):
                # All regular agents trade trader signals. Exclude the
                # chart_hacker copier (copy_charthacker) and the rose copiers
                # - they only mirror their own source. (2026-05-29 rename: the
                # old `startswith('copy_ch_')` test no longer matches
                # 'copy_charthacker', so exclude it explicitly.)
                if agent_id != 'copy_charthacker' and not agent_id.startswith('copy_rose_'):
                    return agent_id
        return None

    async def _mirror_signal(self, signal: Dict, mappings: Dict[str, List[Dict]]):
        """Place limit order on demo accounts for a new signal."""
        tp_id = signal["id"]
        sym = signal["instrument_symbol"]
        # Strip options contract suffixes only - keep :USDT perp suffix
        # (all accounts use swap/perps, never spot).
        import re as _re2
        _opt_re = _re2.compile(r"-\d{6}-\d+-[PC]$")
        sym = _opt_re.sub("", sym)
        direction = signal["direction"]
        entry = float(signal["entry_price"] or 0)
        sl = float(signal["stop_loss"] or 0) if signal.get("stop_loss") else None
        tp = float(signal["take_profit_1"] or 0) if signal.get("take_profit_1") else None
        notional = float(signal["notional_usd"] or 0)
        
        if entry <= 0:
            return False

        # ── Smart Queue proximity gate ──
        # Don't place orders when price is far from entry.  Queued orders
        # wait until price approaches (promoted by _promote_queued).
        current_price = await self._queue_price(sym)
        if current_price is None or current_price <= 0:
            # No price data - queue, don't place blindly (skip if recently queued)
            pool = await self._ensure_pool()
            recent = await pool.fetch_one(
                "SELECT id FROM public.demo_orders "
                "WHERE tracked_position_id = $1 AND exchange = 'queue' "
                "AND ordered_at > NOW() - INTERVAL '5 minutes' LIMIT 1",
                (tp_id,))
            if recent:
                return False
            await pool.execute(
                "INSERT INTO public.demo_orders "
                "(exchange, account_name, symbol, direction, paper_entry, "
                "paper_sl, paper_tp, tracked_position_id, status, ordered_at) "
                "VALUES ('queue', 'queue', %s, %s, %s, %s, %s, %s, 'queued', NOW()) "
                "ON CONFLICT DO NOTHING",
                (sym, direction, entry, sl, tp, tp_id))
            LOG.debug("Queued signal #%d %s %s (no price data)", tp_id, sym, direction)
            return False

        dist_pct = _dist_to_entry(current_price, entry) * 100.0
        if dist_pct > SMART_QUEUE_PLACE_PCT:
            # Beyond threshold - queue, don't place (skip if recently queued)
            pool = await self._ensure_pool()
            recent = await pool.fetch_one(
                "SELECT id FROM public.demo_orders "
                "WHERE tracked_position_id = $1 AND exchange = 'queue' "
                "AND ordered_at > NOW() - INTERVAL '5 minutes' LIMIT 1",
                (tp_id,))
            if recent:
                return False
            await pool.execute(
                "INSERT INTO public.demo_orders "
                "(exchange, account_name, symbol, direction, paper_entry, "
                "paper_sl, paper_tp, tracked_position_id, status, ordered_at) "
                "VALUES ('queue', 'queue', %s, %s, %s, %s, %s, %s, 'queued', NOW()) "
                "ON CONFLICT DO NOTHING",
                (sym, direction, entry, sl, tp, tp_id))
            LOG.debug("Queued signal #%d %s %s @%.4f (dist=%.1f%%)",
                      tp_id, sym, direction, entry, dist_pct)
            return False
        # Within threshold - verify wick touch before placing
        candles = await self._queue_candles(
            sym, SMART_QUEUE_TOUCH_CANDLES)
        if not _entry_touched(candles, entry):
            # Close but no wick yet - queue anyway (skip if recently queued)
            pool = await self._ensure_pool()
            recent = await pool.fetch_one(
                "SELECT id FROM public.demo_orders "
                "WHERE tracked_position_id = $1 AND exchange = 'queue' "
                "AND ordered_at > NOW() - INTERVAL '5 minutes' LIMIT 1",
                (tp_id,))
            if recent:
                return False
            await pool.execute(
                "INSERT INTO public.demo_orders "
                "(exchange, account_name, symbol, direction, paper_entry, "
                "paper_sl, paper_tp, tracked_position_id, status, ordered_at) "
                "VALUES ('queue', 'queue', %s, %s, %s, %s, %s, %s, 'queued', NOW()) "
                "ON CONFLICT DO NOTHING",
                (sym, direction, entry, sl, tp, tp_id))
            LOG.debug("Queued signal #%d %s %s (within %.1f%% but no wick touch)",
                      tp_id, sym, direction, dist_pct)
            return False

        # Map the actor to agent, then find accounts
        actor_id = signal.get("actor_id", "")
        
        # For now: mirror ALL trader signals to ALL mapped agents
        # This places limit orders for every tracked_position on every mapped account
        accounts = []
        for agent_id, accts in mappings.items():
            accounts.extend(accts)
        
        # Deduplicate accounts
        seen = set()
        unique_accounts = []
        for a in accounts:
            key = f"{a['exchange']}/{a['account_name']}"
            if key not in seen:
                seen.add(key)
                unique_accounts.append(a)
        
        placed_any = False
        
        # Phase 6 (2026-05-29) - ACCURATE per-agent sizing.
        # The OLD flat DEMO_MARGIN_USD logic is removed: each account is now
        # sized by the EXACT rule of the paper agent it mirrors (see
        # _compute_demo_size), computed against the account's real balance.
        # Leverage, allocated margin, notional and the concurrency cap are all
        # resolved per-account INSIDE the loop below.
        for acct in unique_accounts:
            # Which agent does this demo account mirror?
            agent_id = self._agent_for_account(acct["exchange"], acct["account_name"])
            acct_key = f"{acct['exchange']}/{acct['account_name']}"
            balance = self._acct_balance.get(acct_key, DEMO_FALLBACK_BALANCE)

            # Reproduce the mapped agent's sizing rule against the real balance.
            # NOTE: we deliberately do NOT cap to signal["notional_usd"] - that
            # field is always the paper WALLET (1000), not a position ceiling.
            # Capping to it would clamp spot_lev_3x (3x notional) and the
            # leveraged agents back to ~1000 and silently undo accurate sizing.
            mode, allocated, leverage, notional_eff = self._compute_demo_size(
                agent_id, balance, entry, sl)
            # Clamp to exchange per-symbol max (matches paper engine)
            if leverage and leverage > 1.0:
                try:
                    from shared.market_data.exchange_limits import get_max_leverage
                    sym_max = await get_max_leverage(sym)
                    leverage = min(leverage, sym_max)
                except Exception:
                    pass

            # Pre-flight balance check: skip if free balance can't cover allocation.
            # Saves API calls that would be rejected with "ab not enough."
            if allocated > balance * 0.98:  # 2% headroom for concurrent orders
                LOG.debug("Signal #%d → %s/%s: SKIP (allocated=%.2f > free=%.2f)",
                          tp_id, acct["exchange"], acct["account_name"],
                          allocated, balance)
                continue

            # Respect the agent's max-concurrent rule on the demo side so we
            # don't over-place and exhaust margin (sequential agents = 1 at a
            # time; 5%/3% parallel agents = 20/33).
            cap = MODE_MAX_CONCURRENT.get(mode, 20)
            open_n = await self._open_demo_count(acct["exchange"], acct["account_name"])
            if open_n >= cap:
                LOG.info("Signal #%d → %s/%s: SKIP (agent %s mode %s at cap %d/%d)",
                         tp_id, acct["exchange"], acct["account_name"],
                         agent_id or "?", mode, open_n, cap)
                flog("mirror_skipped_cap", tp_id=tp_id, agent=agent_id,
                     exchange=acct["exchange"], account=acct["account_name"],
                     mode=mode, open_count=open_n, cap=cap)
                continue

            # ── Keep current: replace stale orders with updated trader data ──
            # If a newer signal arrives for the same symbol+direction+account
            # within 2% of an existing order's entry, cancel the old and place
            # the new - the trader adjusted their entry/SL/TP.  Beyond 2% they're
            # separate trades.  Older signals defer to the existing order.
            dpool = await self._ensure_pool()
            existing = await dpool.fetch_one(
                "SELECT id, paper_entry, status, exchange_order_id, ordered_at "
                "FROM public.demo_orders "
                "WHERE symbol = $1 AND direction = $2 "
                "AND exchange = $3 AND account_name = $4 "
                "AND status IN ('queued', 'pending') "
                "ORDER BY ordered_at DESC LIMIT 1",
                (sym, direction, acct["exchange"], acct["account_name"]))
            if existing:
                old_entry = float(existing["paper_entry"] or 0)
                # Guard: zero or negative old_entry means corrupted row - treat as different trade
                if old_entry <= 0:
                    LOG.debug("Signal #%d: existing order #%d has paper_entry=%.4f - treating as different trade",
                              tp_id, existing["id"], old_entry)
                else:
                    entry_diff = abs(entry - old_entry) / old_entry
                    # Use signal_timestamp from tracked_position; fall back to NOW()
                    new_ts = signal.get("signal_timestamp") or datetime.now(timezone.utc)
                    old_ts = existing.get("ordered_at")

                    if entry_diff <= 0.02:  # within 2% - same trade, updated
                        if new_ts > old_ts:
                            # Newer - place new first, cancel old after
                            LOG.info("Signal #%d: replacing order #%d %s/%s %s (entry %.4f→%.4f)",
                                     tp_id, existing["id"], acct["exchange"],
                                     acct["account_name"], sym, old_entry, entry)
                            # Fall through - place new order below
                        else:
                            # Older or same age - keep existing
                            LOG.debug("Signal #%d: existing order #%d is current (same trade) - skip",
                                      tp_id, existing["id"])
                            continue
                    else:
                        # Different trade - both valid
                        LOG.debug("Signal #%d: entry %.4f differs >2%% from existing %.4f - both valid",
                                  tp_id, entry, old_entry)
                        existing = None  # prevent cancel-after-placement from destroying unrelated trade

            # Skip when sizing produces zero notional (free balance exhausted).
            if notional_eff <= 0 or allocated <= 0:
                LOG.debug("Signal #%d → %s/%s: SKIP (notional=%.2f allocated=%.2f balance=%.2f)",
                          tp_id, acct["exchange"], acct["account_name"],
                          notional_eff, allocated, balance)
                continue

            # Quantity from the per-account notional, rounded to exchange
            # precision and checked against the symbol's minimum order size.
            qty = notional_eff / entry if notional_eff > 0 and entry > 0 else 0.0
            if qty <= 0:
                continue

            # Symbol availability gate - checked against the DEMO environment's
            # actual market list (bitget PAPTRADING has only ~29 swaps; bybit
            # demo has ~678). Skips guaranteed rejections, logs once per
            # exchange/symbol pair so the gap is visible, not silent.
            avail_cache = getattr(self, "_symbol_avail", {})
            avail_key = f"{acct['exchange']}/{sym}"
            if avail_key not in avail_cache:
                try:
                    avail_cache[avail_key] = await self._adapter.has_market(
                        exchange=acct["exchange"], symbol=sym)
                except Exception:
                    avail_cache[avail_key] = True  # fail open
                self._symbol_avail = avail_cache
                if not avail_cache[avail_key]:
                    LOG.info("Signal #%d: %s not listed on %s (demo env) - will skip this exchange",
                             tp_id, sym, acct["exchange"])
            if not avail_cache[avail_key]:
                flog("mirror_skipped_no_market", tp_id=tp_id, agent=agent_id,
                     exchange=acct["exchange"], account=acct["account_name"],
                     symbol=sym)
                continue

            # Fetch exchange limits for this symbol (cached per bridge instance).
            limits_cache = getattr(self, "_market_limits", {})
            cache_key = f"{acct['exchange']}/{sym}"
            if cache_key not in limits_cache:
                try:
                    limits_cache[cache_key] = await self._adapter.get_market_limits(
                        exchange=acct["exchange"], symbol=sym)
                except Exception:
                    limits_cache[cache_key] = {"min_amount": 0.001, "amount_precision": 3}
                self._market_limits = limits_cache
            limits = limits_cache[cache_key]
            min_amount = limits["min_amount"]
            prec = limits["amount_precision"]
            qty = round(qty, prec)
            if qty < min_amount:
                LOG.debug("Signal #%d → %s/%s: qty=%.6f below min=%.6f for %s - skip",
                          tp_id, acct["exchange"], acct["account_name"],
                          qty, min_amount, sym)
                continue

            try:
                intent = ExecutionIntent(
                    company_id="jarvais", strategy_id=None, agent_id="demo_bridge",
                    exchange=acct["exchange"],
                    account_id_external=f"demo_signal_{tp_id}",
                    symbol=sym, direction=direction, order_type=ORDER_TYPE_LIMIT,
                    quantity=qty, requested_price=entry,
                    stop_loss=sl if sl is not None and sl > 0 else None,
                    take_profit=tp if tp is not None and tp > 0 else None,
                    leverage=leverage if leverage is not None and leverage > 1 else None,
                    metadata={
                        "source": "demo_bridge_signal",
                        "accountName": acct["account_name"],
                        "tracked_position_id": tp_id,
                    },
                )
                updates = await self._adapter.submit(intent)
                acc = [u for u in updates if u.status == "accepted"]
                
                if acc:
                    placed_any = True
                    ext_id = acc[0].external_order_id
                    if existing and existing.get("status") == "pending" and existing.get("exchange_order_id"):
                        try:
                            await self._cancel_demo_order(tp_id, acct["account_name"], existing["exchange_order_id"], exchange=acct["exchange"])
                            await dpool.execute("UPDATE public.demo_orders SET status = 'cancelled', error_message = 'replaced by newer signal', updated_at = NOW() WHERE id = $1", (existing["id"],))
                        except Exception:
                            pass
                    self._orders.setdefault(tp_id, {})[acct["account_name"]] = {
                        "order_id": ext_id,
                        "exchange": acct["exchange"],
                    }
                    LOG.info("Signal #%d [%s] → %s/%s: LIMIT %s %s qty=%.4f @ %.4f SL=%s TP=%s lev=%dx (order %s)",
                             tp_id, agent_id or "?", acct["exchange"], acct["account_name"],
                             direction, sym, qty, entry, sl, tp, leverage,
                             ext_id or "?")
                    flog("mirror_placed", tp_id=tp_id, agent=agent_id,
                         exchange=acct["exchange"], account=acct["account_name"],
                         symbol=sym, direction=direction, paper_entry=entry,
                         sl=sl, tp=tp, qty=qty, leverage=leverage,
                         notional=round(notional_eff, 2), order_id=ext_id)
                    # Record in demo_orders table for comparison tracking.
                    # Phase 1 (2026-05-29): record the EFFECTIVE notional we
                    # actually sized to (after the safety factor), not the raw
                    # 1000, so the dashboard "Demo Orders $" KPI is honest.
                    # Now also stamps agent_id (account↔agent attribution).
                    # Removed the per-insert `pool.close()` - the pool is a shared
                    # singleton; closing it here broke subsequent ticks/daemons.
                    try:
                        pool = await self._ensure_pool()
                        await pool.execute(
                            "INSERT INTO public.demo_orders "
                            "(tracked_position_id, exchange, account_name, exchange_order_id, "
                            "agent_id, symbol, direction, paper_entry, demo_entry, paper_sl, paper_tp, "
                            "leverage, quantity, notional_usd, status, ordered_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',NOW()) "
                            "ON CONFLICT DO NOTHING",
                            (tp_id, acct["exchange"], acct["account_name"], ext_id,
                             agent_id, sym, direction, entry, entry, sl, tp, leverage, qty, notional_eff)
                        )
                    except Exception as exc:
                        LOG.debug("demo_orders insert (accepted) failed: %s", exc)
                else:
                    rej = [u for u in updates if u.status == "rejected"]
                    rej_msg = rej[0].message if rej else "unknown"
                    LOG.warning("Signal #%d [%s] → %s/%s FAILED: %s",
                                tp_id, agent_id or "?", acct["exchange"],
                                acct["account_name"], rej_msg)
                    flog("mirror_rejected", tp_id=tp_id, agent=agent_id,
                         exchange=acct["exchange"], account=acct["account_name"],
                         symbol=sym, direction=direction, paper_entry=entry,
                         qty=qty, leverage=leverage,
                         reason=(rej_msg or "")[:300])
                    # Record error (no pool.close() - shared singleton, see above)
                    try:
                        pool = await self._ensure_pool()
                        await pool.execute(
                            "INSERT INTO public.demo_orders "
                            "(tracked_position_id, exchange, account_name, agent_id, symbol, direction, "
                            "paper_entry, paper_sl, paper_tp, leverage, quantity, notional_usd, "
                            "status, error_message, ordered_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'rejected',%s,NOW())",
                            (tp_id, acct["exchange"], acct["account_name"], agent_id, sym, direction,
                             entry, sl, tp, leverage, qty, notional_eff,
                             (rej_msg or "unknown")[:500])
                        )
                    except Exception as exc:
                        LOG.debug("demo_orders insert (rejected) failed: %s", exc)
            except Exception as exc:
                LOG.error("Signal #%d → %s/%s ERROR: %s",
                          tp_id, acct["exchange"], acct["account_name"], exc)
                flog("mirror_error", tp_id=tp_id, agent=agent_id,
                     exchange=acct["exchange"], account=acct["account_name"],
                     symbol=sym, error=str(exc)[:300])

        return placed_any

    async def _cancel_demo_order(self, tp_id: int, acct_name: str, order_id: str,
                                  exchange: str = "bybit"):
        """Cancel a limit order on the demo exchange."""
        try:
            client = self._adapter._get_client(exchange, acct_name)
            await asyncio.to_thread(client.cancel_order, order_id, "")
            LOG.info("Cancelled demo order %s for signal #%d (%s/%s)",
                     order_id, tp_id, exchange, acct_name)
        except Exception as exc:
            LOG.debug("Cancel order %s: %s", order_id, exc)

    async def _reconcile_fills(self):
        """Phase 1 (2026-05-29) NEW - demo fill reconciliation.

        Previously nothing ever marked a demo limit order as 'filled', so the
        dashboard always showed "0 filled" no matter what happened on the
        exchange. This polls every still-pending demo order, asks the exchange
        for its current status, and promotes it to 'filled' (capturing the real
        fill price + entry slippage) or 'cancelled' when appropriate. Orders
        that are still resting at the exchange stay 'pending'.
        """
        pool = await self._ensure_pool()
        rows = await pool.fetch_all(
            "SELECT id, exchange, account_name, exchange_order_id, symbol, "
            "       paper_entry, agent_id, direction "
            "FROM public.demo_orders "
            "WHERE status = 'pending' AND exchange_order_id IS NOT NULL "
            "ORDER BY ordered_at ASC LIMIT 50"
        )
        for r in rows:
            try:
                client = self._adapter._get_client(r["exchange"], r["account_name"])
                raw = await asyncio.to_thread(
                    client.fetch_order, r["exchange_order_id"], r["symbol"])
            except Exception as exc:
                LOG.debug("fetch_order %s failed: %s", r["exchange_order_id"], exc)
                continue
            status = (raw.get("status") or "").lower()
            filled = float(raw.get("filled") or 0)
            avg = raw.get("average") or raw.get("price")
            if status == "closed" and filled > 0 and avg:
                avg = float(avg)
                paper_entry = float(r["paper_entry"] or 0)
                slip = ((avg - paper_entry) / paper_entry) if paper_entry > 0 else None
                # Phase 1 (2026-05-29): capture the execution fee on the entry
                # fill so the forensic audit accounts for every cent. CCXT
                # normalises this into raw["fee"]["cost"] (or a list in
                # raw["fees"]). Best-effort - NULL stays NULL if unavailable.
                entry_fee = self._extract_fee(raw)
                await pool.execute(
                    "UPDATE public.demo_orders SET status='filled', demo_entry=%s, "
                    "filled_at=NOW(), slippage_entry=%s, entry_fee=%s, "
                    "fees_synced_at=NOW(), updated_at=NOW() WHERE id=%s",
                    (avg, slip, entry_fee, r["id"]))
                LOG.info("Demo order %s FILLED @ %.6f (slip=%s fee=%s)",
                         r["exchange_order_id"], avg,
                         f"{slip:.4%}" if slip is not None else "n/a",
                         entry_fee)
                flog("fill", demo_order_id=r["id"], agent=r["agent_id"],
                     exchange=r["exchange"], account=r["account_name"],
                     symbol=r["symbol"], direction=r["direction"],
                     paper_entry=float(r["paper_entry"] or 0), demo_entry=avg,
                     slippage_pct=(round(slip * 100, 4) if slip is not None else None),
                     entry_fee=entry_fee, order_id=r["exchange_order_id"])
            elif status in ("canceled", "cancelled", "rejected", "expired"):
                await pool.execute(
                    "UPDATE public.demo_orders SET status='cancelled', updated_at=NOW() "
                    "WHERE id=%s", (r["id"],))
                flog("cancel", demo_order_id=r["id"], agent=r["agent_id"],
                     exchange=r["exchange"], account=r["account_name"],
                     symbol=r["symbol"], exchange_status=status,
                     order_id=r["exchange_order_id"])

    async def _reconcile_exits(self):
        """Capture exchange-side close data for filled positions.

        When a demo position closes on the exchange (SL/TP hit, manual close,
        etc.), we poll fetch_positions (for open) and fetch_closed_orders
        (for closed) to capture: exit_price, exit_fee, demo_pnl, slippage_exit.
        fetch_order only returns LIMIT-ORDER fill data (same price for entry
        and exit), which always produces zero P&L. The position and closed-order
        APIs carry the real entry-vs-exit prices and P&L.
        """
        pool = await self._ensure_pool()
        rows = await pool.fetch_all(
            "SELECT id, exchange, account_name, exchange_order_id, symbol, "
            "       paper_entry, paper_sl, paper_tp, direction, notional_usd "
            "FROM public.demo_orders "
            "WHERE status = 'filled' AND closed_at IS NULL "
            "  AND exchange_order_id IS NOT NULL "
            "ORDER BY filled_at ASC LIMIT 20"
        )
        for r in rows:
            ex = r["exchange"]
            acct = r["account_name"]
            sym = r["symbol"]
            oid = r["exchange_order_id"]
            direction = (r["direction"] or "long").lower()
            paper_entry = float(r["paper_entry"] or 0)
            notional = float(r["notional_usd"] or 0)

            try:
                client = self._adapter._get_client(ex, acct)
                if not client.markets:
                    await asyncio.to_thread(client.load_markets)
            except Exception as exc:
                LOG.debug("exit_reconcile client error %s/%s: %s", ex, acct, exc)
                continue

            exit_price = None
            pnl = None
            exit_fee = None

            # Try 1: fetch_closed_orders - returns closed positions with
            # realised P&L as reported by the exchange.
            try:
                since_ms = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp() * 1000)
                closed = await asyncio.to_thread(
                    lambda: client.fetch_closed_orders(sym, since_ms, 50))
                if isinstance(closed, list):
                    for co in closed:
                        if co.get("id") == oid:
                            co_info = co.get("info", {}) if isinstance(co.get("info"), dict) else {}
                            exit_price = float(co.get("average") or 0) or None
                            # Realised P&L from exchange (Bybit: cum_realised_pnl)
                            pnl = float(co.get("info", {}).get("cumRealisedPnl") or 0) or None
                            if pnl is None:
                                pnl = float(co.get("profit") or 0) or None
                            exit_fee = self._extract_fee(co)
                            break
            except Exception as exc:
                LOG.debug("exit_reconcile fetch_closed_orders %s failed: %s", oid, exc)

            # Try 2: fetch_positions (for still-open positions that have P&L)
            if exit_price is None:
                try:
                    positions = await asyncio.to_thread(
                        lambda: client.fetch_positions([sym]) if hasattr(client, "fetch_positions")
                        else [])
                    for pos in (positions or []):
                        if pos.get("info", {}).get("orderId") == oid or pos.get("symbol") == sym:
                            # If the position is open, capture unrealized P&L
                            if float(pos.get("contracts") or 0) > 0:
                                exit_price = float(pos.get("markPrice") or 0) or None
                                pnl = float(pos.get("unrealizedPnl") or 0) or None
                            break
                except Exception as exc:
                    LOG.debug("exit_reconcile fetch_positions %s failed: %s", oid, exc)

            if exit_price is None:
                continue  # still open, no close data yet

            if pnl is None and paper_entry > 0 and notional > 0:
                if direction == "long":
                    pnl = (exit_price - paper_entry) / paper_entry * notional
                else:
                    pnl = (paper_entry - exit_price) / paper_entry * notional

            slip_exit = (
                ((exit_price - float(r["paper_tp"] or 0)) / float(r["paper_tp"] or 1))
                if r.get("paper_tp") else None
            )

            await pool.execute(
                "UPDATE public.demo_orders SET "
                "status='closed', closed_at=NOW(), exit_price=%s, demo_pnl=%s, "
                "slippage_exit=%s, exit_fee=%s, fees_synced_at=NOW(), "
                "updated_at=NOW() WHERE id=%s AND closed_at IS NULL",
                (round(exit_price, 8), round(float(pnl or 0), 8), slip_exit, exit_fee, r["id"]))
            LOG.info("Demo order %s CLOSED @ %.6f pnl=%.4f (order %s)",
                     sym, exit_price, float(pnl or 0), oid)
            flog("exit_reconciled", demo_order_id=r["id"],
                 exchange=ex, account=acct, symbol=sym, direction=direction,
                 exit_price=round(exit_price, 8), demo_pnl=round(float(pnl or 0), 4),
                 exit_fee=exit_fee, order_id=oid)

    async def _sync_positions(self, mappings: Dict[str, List[Dict]]):
        """Sync live exchange positions into demo_orders.

        The bridge places limit orders; once filled, positions live on the
        exchange independently. If demo_orders rows were cleaned up (cancelled
        during orphan processing), the position still exists with real P&L
        that our dashboard can't see. This polls fetch_positions on every
        mapped account and creates demo_orders rows for untracked positions,
        or updates existing rows with current unrealized P&L.
        """
        pool = await self._ensure_pool()
        # Flatten mapping dict: {agent: [{exchange, account_name}, ...]} → unique accounts
        seen = set()
        accounts = []
        for accts in mappings.values():
            for a in accts:
                key = f"{a['exchange']}/{a['account_name']}"
                if key not in seen:
                    seen.add(key)
                    accounts.append(a)
        for acct in accounts:
            ex = acct["exchange"]
            acct_name = acct["account_name"]
            agent_id = self._agent_for_account(ex, acct_name) or "?"
            sym_set = set()
            try:
                client = self._adapter._get_client(ex, acct_name)
                positions = await asyncio.to_thread(
                    lambda: client.fetch_positions() if hasattr(client, "fetch_positions")
                    else [])
            except Exception as exc:
                LOG.debug("_sync_positions fetch_positions %s/%s failed: %s",
                          ex, acct_name, exc)
                continue
            for pos in (positions or []):
                sym = pos.get("symbol", "")
                if not sym:
                    continue
                sym_set.add(sym)
                contracts = float(pos.get("contracts") or 0)
                if contracts <= 0:
                    continue
                entry = float(pos.get("entryPrice") or 0)
                mark = float(pos.get("markPrice") or 0)
                upnl = float(pos.get("unrealizedPnl") or 0)
                notional = float(pos.get("notional") or 0) or (entry * abs(contracts))
                # Bitget demo CCXT adapter reports side='short' for long
                # positions - contracts is the numeric truth.  When both
                # signals disagree, trust contracts over side.
                raw_side = str(pos.get("side", "")).lower()
                sign_dir = "long" if contracts > 0 else "short"
                if raw_side in ("long", "short") and raw_side != sign_dir:
                    direction = sign_dir  # override CCXT side with numeric truth
                elif raw_side in ("long", "short"):
                    direction = raw_side
                else:
                    direction = sign_dir
                pnl_pct = ((mark - entry) / entry * 100) if entry > 0 else 0
                if direction == "short":
                    pnl_pct = -pnl_pct

                # BE-lock check FIRST (runs before the continue, for both
                # existing and newly-discovered positions).
                be_existing = False
                already_locked = False
                if "be_lock" in (agent_id or "") and entry > 0 and mark > 0:
                    # Quick check: is this position already BE-locked?
                    be_row = await pool.fetch_one(
                        "SELECT id, metadata FROM public.demo_orders "
                        "WHERE exchange = $1 AND account_name = $2 "
                        "AND symbol = $3 AND status IN ('filled','pending') "
                        "AND closed_at IS NULL ORDER BY created_at DESC LIMIT 1",
                        (ex, acct_name, sym))
                    be_existing = bool(be_row)
                    already_locked = bool((be_row["metadata"] or {}).get("be_locked")) if be_row else False
                    if not already_locked:
                        pnl_pct = ((mark - entry) / entry) * 100.0
                        if direction == "short":
                            pnl_pct = -pnl_pct
                        threshold = self._demo_sizing("demo_be_lock_threshold_pct", 5.0)
                        offset   = self._demo_sizing("demo_be_lock_offset_pct", 0.002)
                        if pnl_pct >= threshold:
                            new_sl = entry * (1.0 + offset) if direction == "long" else entry * (1.0 - offset)
                            ok = await self._adapter.modify_sl(
                                exchange=ex, account_name=acct_name,
                                symbol=sym, sl_price=new_sl, direction=direction,
                                size=abs(contracts))
                            if ok and be_row:
                                await pool.execute(
                                    "UPDATE public.demo_orders SET "
                                    "paper_sl = $1, "
                                    "metadata = jsonb_set(COALESCE(metadata,'{}'::jsonb), "
                                    "  '{be_locked}', 'true'::jsonb), "
                                    "updated_at = NOW() WHERE id = $2",
                                    (round(new_sl, 8), be_row["id"]))
                            LOG.info("BE-LOCK %s/%s %s %s @+%.1f%% -> SL=%.6g (%s)",
                                     ex, acct_name, sym, direction, pnl_pct,
                                     new_sl, "ok" if ok else "failed")

                # Check if we already have a demo_orders row for this position.
                # Prefer filled rows (the real position) over pending ones
                # which are newer limit orders from signal mirroring and may
                # never fill.  This ensures the _sync_positions backfill always
                # targets the row that represents actual exchange state.
                existing = await pool.fetch_one(
                    "SELECT id FROM public.demo_orders "
                    "WHERE exchange=%s AND account_name=%s AND symbol=%s "
                    "AND status IN ('filled','closed','pending') "
                    "AND (closed_at IS NULL OR created_at > NOW() - INTERVAL '7 days') "
                    "ORDER BY CASE WHEN status='filled' THEN 0 ELSE 1 END, created_at DESC LIMIT 1",
                    (ex, acct_name, sym))
                if existing:
                    # Update unrealized P&L on the existing row, and backfill
                    # paper tracking columns if they're missing (first sync after
                    # this fix won't have them for rows created before 2026-06-16).
                    tp_id_backfill = None
                    _pe_backfill = None
                    _sl_backfill = None
                    _tp_backfill = None
                    _slip_backfill = None
                    try:
                        tp_row = await pool.fetch_one(
                            "SELECT id, entry_price, stop_loss, take_profit_1 "
                            "FROM public.tracked_positions "
                            "WHERE company_id = 'jarvais' "
                            "AND instrument_symbol ILIKE $1 "
                            "AND direction = $2 "
                            "AND status IN ('open','closed') "
                            "AND entry_price BETWEEN $3 * 0.95 AND $3 * 1.05 "
                            "ORDER BY signal_timestamp DESC LIMIT 1",
                            (sym, direction, entry))
                        if tp_row:
                            tp_id_backfill = tp_row["id"]
                            _pe_backfill = float(tp_row["entry_price"] or 0) or None
                            _sl_backfill = float(tp_row["stop_loss"] or 0) or None
                            _tp_backfill = float(tp_row["take_profit_1"] or 0) or None
                            if _pe_backfill and _pe_backfill > 0 and entry > 0:
                                _slip_backfill = round((entry - _pe_backfill) / _pe_backfill, 6)
                    except Exception:
                        pass
                    await pool.execute(
                        "UPDATE public.demo_orders SET "
                        "demo_pnl=%s, demo_entry=COALESCE(demo_entry,%s), "
                        "notional_usd=%s, updated_at=NOW(), "
                        "direction=%s, "
                        "tracked_position_id=COALESCE(tracked_position_id,%s), "
                        "paper_entry=%s, "
                        "paper_sl=COALESCE(paper_sl,%s), "
                        "paper_tp=COALESCE(paper_tp,%s), "
                        "slippage_entry=%s "
                        "WHERE id=%s",
                        (round(upnl, 8), entry, round(notional, 2),
                         direction,
                         tp_id_backfill, _pe_backfill,
                         _sl_backfill, _tp_backfill, _slip_backfill,
                         existing["id"]))
                    continue

                # ── BE-lock check (runs for BOTH existing and new positions) ──
                if "be_lock" in (agent_id or "") and entry > 0 and mark > 0:
                    be_existing = bool(existing)
                    already_locked = False
                    if be_existing:
                        meta_row = await pool.fetch_one(
                            "SELECT metadata FROM public.demo_orders WHERE id=%s",
                            (existing["id"],))
                        if meta_row and meta_row.get("metadata"):
                            already_locked = bool((meta_row["metadata"] or {}).get("be_locked"))
                    if not already_locked:
                        pnl_pct = ((mark - entry) / entry) * 100.0
                        if direction == "short":
                            pnl_pct = -pnl_pct
                        threshold = self._demo_sizing("demo_be_lock_threshold_pct", 5.0)
                        offset   = self._demo_sizing("demo_be_lock_offset_pct", 0.002)
                        if pnl_pct >= threshold:
                            new_sl = entry * (1.0 + offset) if direction == "long" else entry * (1.0 - offset)
                            ok = await self._adapter.modify_sl(
                                exchange=ex, account_name=acct_name,
                                symbol=sym, sl_price=new_sl, direction=direction,
                                size=abs(contracts))
                            if ok:
                                target_id = existing["id"] if be_existing else None
                                if target_id:
                                    await pool.execute(
                                        "UPDATE public.demo_orders SET "
                                        "paper_sl=%s, "
                                        "metadata = jsonb_set(COALESCE(metadata,'{}'::jsonb), "
                                        "  '{be_locked}', 'true''::jsonb), "
                                        "updated_at=NOW() WHERE id=%s",
                                        (round(new_sl, 8), target_id))
                                LOG.info("BE-LOCK %s/%s %s %s @+%.1f%% → SL=%.6g (%s)",
                                         ex, acct_name, sym, direction, pnl_pct,
                                         new_sl, "ok" if ok else "failed")

                # New position - try to link it to a tracked_position so the
                # dashboard can do paper-vs-demo comparison (drift, slip, etc.).
                tp_id = None
                _paper_entry = None
                _paper_sl = None
                _paper_tp = None
                try:
                    # Match by symbol + direction, preferring the most recent
                    # open position whose entry_price is within ±5% of the
                    # exchange's entry (symbol-name collisions across exchanges
                    # can produce very different prices - 5% tightens it).
                    tp_row = await pool.fetch_one(
                        "SELECT id, entry_price, stop_loss, take_profit_1 "
                        "FROM public.tracked_positions "
                        "WHERE company_id = 'jarvais' "
                        "AND instrument_symbol ILIKE $1 "
                        "AND direction = $2 "
                        "AND status IN ('open','closed') "
                        "AND entry_price BETWEEN $3 * 0.95 AND $3 * 1.05 "
                        "ORDER BY signal_timestamp DESC "
                        "LIMIT 1",
                        (sym, direction, entry))
                    if tp_row:
                        tp_id = tp_row["id"]
                        _paper_entry = float(tp_row["entry_price"] or 0) or None
                        _paper_sl = float(tp_row["stop_loss"] or 0) or None
                        _paper_tp = float(tp_row["take_profit_1"] or 0) or None
                except Exception:
                    pass  # best-effort linkage; row still useful without it

                # Now compute slippage: (demo_fill − paper_target) / paper_target.
                _slip = None
                if _paper_entry and _paper_entry > 0 and entry > 0:
                    _slip = round((entry - _paper_entry) / _paper_entry, 6)

                try:
                    await pool.execute(
                        "INSERT INTO public.demo_orders "
                        "(exchange, account_name, agent_id, symbol, direction, "
                        "tracked_position_id, paper_entry, demo_entry, paper_sl, paper_tp, "
                        "notional_usd, demo_pnl, slippage_entry, "
                        "status, ordered_at, filled_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'filled',NOW(),NOW()) "
                        "ON CONFLICT DO NOTHING",
                        (ex, acct_name, agent_id, sym, direction,
                         tp_id, _paper_entry, round(entry, 8), _paper_sl, _paper_tp,
                         round(notional, 2), round(upnl, 8), _slip))
                except Exception as exc:
                    LOG.debug("_sync_positions insert failed: %s", exc)



            if sym_set:
                LOG.info("_sync_positions %s/%s: %d positions synced",
                         ex, acct_name, len(sym_set))

                # Detect closed positions: filled rows on this account
                # whose symbol was NOT seen in current fetch_positions.
                # The position was closed on the exchange since last tick.
                closed_ids = await pool.fetch_all(
                    "SELECT id, symbol, direction, demo_entry, demo_pnl, "
                    "paper_tp, slippage_exit "
                    "FROM public.demo_orders "
                    "WHERE exchange = $1 AND account_name = $2 "
                    "AND status = 'filled' AND closed_at IS NULL "
                    "AND NOT (symbol = ANY($3::text[]))",
                    (ex, acct_name, list(sym_set)))
                for cr in closed_ids:
                    # Try to get exit price from closed orders
                    exit_px = cr.get("demo_entry")
                    try:
                        co = await asyncio.to_thread(
                            lambda: client.fetch_closed_orders(
                                cr["symbol"], limit=1))
                        if co:
                            exit_px = float(co[0].get("average") or co[0].get("price") or exit_px)
                    except Exception:
                        pass
                    await pool.execute(
                        "UPDATE public.demo_orders SET "
                        "status = 'closed', closed_at = NOW(), "
                        "exit_price = $1, demo_pnl = COALESCE(demo_pnl, 0) "
                        "WHERE id = $2",
                        (round(exit_px, 8) if exit_px else None, cr["id"]))
                if closed_ids:
                    LOG.info("_sync_positions %s/%s: %d positions detected as closed",
                             ex, acct_name, len(closed_ids))

    @staticmethod
    def _extract_fee(raw: Dict) -> Optional[float]:
        """Best-effort total execution fee (USDT) from a CCXT order dict."""
        try:
            fee = raw.get("fee")
            if isinstance(fee, dict) and fee.get("cost") is not None:
                return abs(float(fee["cost"]))
            fees = raw.get("fees")
            if isinstance(fees, list) and fees:
                total = sum(abs(float(f.get("cost") or 0)) for f in fees
                            if isinstance(f, dict))
                return total or None
        except (TypeError, ValueError):
            pass
        return None

    async def _cancel_orphan_orders(self):
        """Cancel pending demo orders whose paper tracked_position is done.

        The paper side is authoritative. When paper status moves from 'pending'
        to 'open' (filled), 'closed' (trade complete), or 'expired' (timed out),
        the corresponding demo limit order missed its entry window and will
        never fill. Cancel it so it doesn't count against the cap.

        Different from the old _cancel_stale_orders (time-based, which was
        wrong - orders should track toward entry indefinitely). This only
        cancels orders whose paper lifecycle has moved past 'pending'.

        Exchange-side cancellation is best-effort; the DB update is the
        canonical cleanup.
        """
        pool = await self._ensure_pool()
        rows = await pool.fetch_all(
            "SELECT d.id, d.exchange, d.account_name, d.exchange_order_id "
            "FROM public.demo_orders d "
            "JOIN public.tracked_positions tp ON tp.id = d.tracked_position_id "
            "WHERE d.status = 'pending' "
            "AND tp.status IN ('closed', 'expired')",
        )
        if not rows:
            return
        LOG.info("Orphan cleanup: cancelling %d demo orders (paper already open/closed/expired)",
                 len(rows))
        for r in rows:
            oid = r["exchange_order_id"]
            if oid:
                try:
                    await self._cancel_demo_order(
                        r["id"], r["account_name"], oid,
                        exchange=r["exchange"],
                    )
                except Exception:
                    pass
            await pool.execute(
                "UPDATE public.demo_orders SET status='cancelled', "
                "error_message='orphan: paper position no longer pending' "
                "WHERE id=%s", (r["id"],),
            )

    async def _refresh_sizing(self) -> None:
        """Pull live sizing knobs from copy_sizing_config (same DB table as paper).

        Called at the start of every tick so changes made via the Settings UI
        apply to both the paper engine and this demo bridge within one cycle.
        """
        try:
            from shared.intelligence.copy_sizing_config import get_all
            pool = await self._ensure_pool()
            knobs = await get_all(pool=pool)
            for k, meta in knobs.items():
                _DEMO_SIZING_CACHE[k] = meta["value"]
        except Exception as exc:
            LOG.debug("_refresh_sizing failed (%s); using prior/defaults", exc)

    # ═══════════════════════════════════════════════════════════════════
    # Smart Queue - promote / expire
    # ═══════════════════════════════════════════════════════════════════

    async def _promote_queued(self):
        """Promote queued orders to pending when price is within place threshold.

        Checks current price + candle wick-touch for every order with
        status='queued'.  If price is within SMART_QUEUE_PLACE_PCT AND a
        recent 1m candle's [low,high] range contains the entry price, the
        order is promoted - status → 'pending', and the next _mirror_signal
        call will place it on the exchange.
        """
        pool = await self._ensure_pool()
        rows = await pool.fetch_all(
            "SELECT id, symbol, direction, paper_entry, tracked_position_id "
            "FROM public.demo_orders "
            "WHERE status = 'queued' "
            "ORDER BY created_at ASC LIMIT 100"
        )
        if not rows:
            return
        LOG.debug("_promote_queued: checking %d queued orders", len(rows))

        promoted = 0
        for r in rows:
            entry = float(r["paper_entry"] or 0)
            if entry <= 0:
                continue
            sym = r["symbol"]

            # Get current price + candles
            price = await self._queue_price(sym)
            if price is None:
                continue

            dist = _dist_to_entry(price, entry)
            dist_pct = dist * 100.0

            if dist_pct > SMART_QUEUE_PLACE_PCT:
                continue  # still too far

            # Within threshold - verify wick touch
            candles = await self._queue_candles(
                sym, SMART_QUEUE_TOUCH_CANDLES)
            if not _entry_touched(candles, entry):
                continue  # within range but no wick kiss yet

            # Promote: status queued → pending so _mirror_signal picks it up
            await pool.execute(
                "UPDATE public.demo_orders SET status = 'pending', "
                "updated_at = NOW() WHERE id = $1",
                (r["id"],))
            if r.get("tracked_position_id"):
                self._mirrored.discard(r["tracked_position_id"])
            promoted += 1
            LOG.info("Promoted queued→pending #%d %s %s @%.2f (dist=%.1f%%)",
                     r["id"], sym, r["direction"], entry, dist_pct)

        if promoted:
            LOG.info("_promote_queued: %d orders promoted", promoted)

    async def _expire_distant(self):
        """Close/cancel orders where price has drifted past the close threshold.

        Queued orders (never placed): status → 'expired', no exchange call.
        Pending orders (on exchange): cancel on exchange + status → 'cancelled'.
        """
        pool = await self._ensure_pool()
        rows = await pool.fetch_all(
            "SELECT id, symbol, direction, paper_entry, status, "
            "exchange_order_id, exchange, account_name "
            "FROM public.demo_orders "
            "WHERE status IN ('queued', 'pending') "
            "ORDER BY created_at ASC LIMIT 100"
        )
        if not rows:
            return

        expired_queued = 0
        cancelled_pending = 0
        for r in rows:
            entry = float(r["paper_entry"] or 0)
            if entry <= 0:
                continue

            price = await self._queue_price(r["symbol"])
            if price is None:
                continue

            dist_pct = _dist_to_entry(price, entry) * 100.0

            # Stale pending: order is on exchange but price crossed entry
            # hours ago and never filled.  The fill window is closed.
            if (r["status"] == "pending" and r.get("ordered_at") and
                r.get("exchange_order_id")):
                age_h = (datetime.now(timezone.utc) - r["ordered_at"]).total_seconds() / 3600
                past_entry = (
                    (r["direction"] == "long" and price > entry * 1.01) or
                    (r["direction"] == "short" and price < entry * 0.99)
                )
                if age_h > 1 and past_entry:
                    try:
                        await self._cancel_demo_order(
                            r.get("tracked_position_id", 0),
                            r["account_name"], r["exchange_order_id"],
                            exchange=r.get("exchange", "bybit"))
                    except Exception:
                        pass
                    await pool.execute(
                        "UPDATE public.demo_orders SET status = 'cancelled', "
                        "error_message = 'stale: fill window closed (%.1fh, price past entry)', "
                        "updated_at = NOW() WHERE id = $1",
                        (r["id"],))
                    cancelled_pending += 1
                    LOG.info("Cancelled stale pending #%d %s %s (age=%.1fh, dist=%.1f%%)",
                             r["id"], r["symbol"], r["direction"], age_h, dist_pct)
                    continue

            if dist_pct <= SMART_QUEUE_CLOSE_PCT:
                continue  # still within holding zone

            if r["status"] == "queued":
                # Never placed on exchange - just mark expired locally
                await pool.execute(
                    "UPDATE public.demo_orders SET status = 'expired', "
                    "updated_at = NOW() WHERE id = $1",
                    (r["id"],))
                expired_queued += 1
                LOG.info("Expired queued #%d %s %s (dist=%.1f%%)",
                         r["id"], r["symbol"], r["direction"], dist_pct)

            elif r["status"] == "pending":
                # Guard: promoted-but-never-placed orders have no exchange_order_id
                if not r.get("exchange_order_id"):
                    await pool.execute(
                        "UPDATE public.demo_orders SET status = 'expired', "
                        "error_message = 'promoted but never placed', "
                        "updated_at = NOW() WHERE id = $1",
                        (r["id"],))
                    expired_queued += 1
                    LOG.info("Expired unplaced pending #%d %s %s (dist=%.1f%%)",
                             r["id"], r["symbol"], r["direction"], dist_pct)
                    continue
                # On exchange - cancel, then mark
                try:
                    await self._cancel_demo_order(
                        r.get("tracked_position_id", 0),
                        r["account_name"],
                        r["exchange_order_id"],
                        exchange=r.get("exchange", "bybit"),
                    )
                    await pool.execute(
                        "UPDATE public.demo_orders SET status = 'cancelled', "
                        "updated_at = NOW() WHERE id = $1",
                        (r["id"],))
                    cancelled_pending += 1
                    LOG.info("Cancelled distant #%d %s %s (dist=%.1f%%)",
                             r["id"], r["symbol"], r["direction"], dist_pct)
                except Exception as exc:
                    LOG.debug("_expire_distant cancel failed #%d: %s", r["id"], exc)

        if expired_queued or cancelled_pending:
            LOG.info("_expire_distant: %d expired (queued) + %d cancelled (pending)",
                     expired_queued, cancelled_pending)

    async def tick(self):
        mappings = await self._load_mappings()
        if not mappings:
            return

        # 0. Refresh sizing knobs from the same DB table the paper engine uses.
        await self._refresh_sizing()

        # 0. Refresh real demo-account balances (drives accurate per-agent sizing)
        await self._refresh_balances(mappings)

        # Refresh exchange leverage limits cache (every 5 min)
        if not hasattr(self, "_last_limits_refresh"):
            self._last_limits_refresh = 0.0
        if time.monotonic() - self._last_limits_refresh > 300:
            try:
                from shared.market_data.exchange_limits import clear_cache
                clear_cache()
            except Exception:
                pass
            self._last_limits_refresh = time.monotonic()

        # Persist margin mode to DB (one-time per account, first tick only)
        if not hasattr(self, "_margin_persisted"):
            self._margin_persisted = set()
        for acct_key in self._acct_balance:
            if acct_key not in self._margin_persisted:
                ex, name = acct_key.split("/", 1)
                try:
                    pool = await self._ensure_pool()
                    await pool.execute(
                        "UPDATE public.exchange_accounts "
                        "SET metadata = jsonb_set(COALESCE(metadata,'{}'::jsonb), "
                        "  '{margin_mode}', '\"cross\"'::jsonb) "
                        "WHERE exchange = $1 AND account_name = $2",
                        (ex, name))
                    self._margin_persisted.add(acct_key)
                except Exception:
                    pass

        # ── Smart Queue ──
        # 0a. Expire queued/pending orders that drifted past close threshold
        await self._expire_distant()
        # 0b. Promote queued orders whose price is now within place threshold
        await self._promote_queued()

        # 1. Place limit orders for new signals
        signals = await self._get_new_signals()
        if signals:
            LOG.info("Tick: %d new signals to mirror", len(signals))
            for s in signals:
                was_placed = await self._mirror_signal(s, mappings)
                self._mirrored.add(s["id"])
                await asyncio.sleep(0.3)  # Rate limit
        
        # 1b. Reconcile pending demo orders against the exchange (mark fills)
        await self._reconcile_fills()

        # 1b2. Reconcile filled-but-open positions (capture exit data)
        await self._reconcile_exits()

        # 1b3. Sync exchange-side positions into demo_orders.
        # The bridge places orders; once filled, the position lives on the
        # exchange independently. If demo_orders rows were cleaned up (cancelled
        # during orphan processing), the position still exists and has real P&L
        # that our dashboard can't see. This step polls fetch_positions on
        # every account and creates/updates demo_orders rows for any position
        # not already tracked.
        await self._sync_positions(mappings)

        # 1c. Cancel demo orders where the paper tracked_position is already
        # open/closed/expired. These demo limits missed their entry and will
        # never fill - the paper side already moved on. (Not the same as the
        # stale-by-time cleanup - these are orphaned by paper lifecycle, which
        # is the authoritative signal.)
        await self._cancel_orphan_orders()

        # 2. Cancel orders for cancelled/expired signals
        cancelled = await self._get_cancelled_signals()
        for c in cancelled:
            tp_id = c["id"]
            orders = self._orders.pop(tp_id, {})
            for acct_name, info in orders.items():
                if isinstance(info, dict):
                    oid = info.get("order_id")
                    exch = info.get("exchange", "bybit")
                else:
                    oid = info  # backward compat with old string-format entries
                    exch = "bybit"
                if oid:
                    await self._cancel_demo_order(tp_id, acct_name, oid, exchange=exch)
            if orders:
                LOG.info("Signal #%d cancelled - removed %d demo orders", tp_id, len(orders))

    async def run_forever(self):
        LOG.info("DemoBridge starting - signal-driven (poll=%ds)", POLL_INTERVAL_S)
        await self._load_mirrored()
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                LOG.exception("Tick failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
        LOG.info("DemoBridge stopped")

    def stop(self):
        self._stop.set()


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bridge = DemoBridge()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bridge.stop)
    await bridge.run_forever()

if __name__ == "__main__":
    asyncio.run(main())
