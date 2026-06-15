"""
demo_bridge.py — mirrors paper competition SIGNALS to demo exchange accounts.

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
import asyncio, logging, os, re, signal, sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/opt/tickles")
from shared.utils.db import DatabasePool
from shared.execution.ccxt_adapter import CcxtExecutionAdapter
from shared.execution.protocol import (
    ExecutionIntent, DIRECTION_LONG, DIRECTION_SHORT, ORDER_TYPE_LIMIT,
)
# Phase 1 (2026-05-29): forensic transaction log — every mirror event is
# appended to /opt/tickles/shared/logs/paper_demo.log for the dashboard's
# "Paper vs Demo vs Live" live-log viewer and shell-side grep.
from shared.daemons.demo_forensic_log import flog

LOG = logging.getLogger("demo.bridge")
POLL_INTERVAL_S = int(os.environ.get("DEMO_BRIDGE_POLL_S", "15"))

# Strip options-contract suffixes (e.g. -260531-90-P) before routing to CCXT.
_OPT_RE_DEMO = re.compile(r"-\d{6}-\d+-[PC]$")

# Phase 1 (2026-05-29) FIX — position sizing.
#
# THE BUG: every tracked_position carries notional_usd = 1000 (the full paper
# wallet). The bridge mirrored that 1:1 as the order NOTIONAL and committed
# margin = notional / leverage onto each ~1000 USD demo account. Because the
# bridge fires for EVERY signal on EVERY account, those orders STACKED until
# the account margin was completely exhausted — that is why the demo accounts
# ended up with e.g. a BNB position of 5120 notional and NEGATIVE free margin,
# after which every new order is rejected with "ab not enough" (retCode
# 110007). In other words it committed ~100% (and more) of the wallet.
#
# THE FIX: size each demo order the same way the paper agents do — commit a
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
# Phase 6 (2026-05-29) — ACCURATE per-agent demo sizing.
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
# Max concurrent OPEN demo positions per account, by mode — mirrors the paper
# agent's concurrency rule so the demo never over-places and exhausts margin.
MODE_MAX_CONCURRENT: Dict[str, int] = {
    "spot_seq": 5, "spot_seq_ch": 33, "spot_lev_3x": 3, "lev_3pct": 33, "lev_5pct": 20,
}
# Risk % / leverage knobs (env-tunable; defaults match copy_trade_monitor).
DEMO_RISK_PCT_5  = float(os.environ.get("DEMO_RISK_PCT_5",  "5.0"))
DEMO_RISK_PCT_3  = float(os.environ.get("DEMO_RISK_PCT_3",  "3.0"))
DEMO_SPOT_LEV_3X = float(os.environ.get("DEMO_SPOT_LEV_3X", "3.0"))
DEMO_LEVERAGE_CAP= float(os.environ.get("DEMO_LEVERAGE_CAP","100.0"))
# Fallback balance when the exchange balance can't be fetched (~paper wallet).
DEMO_FALLBACK_BALANCE = float(os.environ.get("DEMO_FALLBACK_BALANCE", "1000.0"))


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

        Also populates self._acct_agent — the REVERSE map keyed by
        "{exchange}/{account_name}" → agent_id — so every demo order we place
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
                    # Always update — even zero balance must be recorded
                    # so sizing sees the real free balance, not the fallback.
                    if "USDT" in bal:
                        self._acct_balance[key] = usdt
                except Exception as exc:
                    LOG.debug("balance refresh %s failed: %s", key, exc)
                self._acct_balance.setdefault(key, DEMO_FALLBACK_BALANCE)

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
        bal = balance if balance and balance > 0 else DEMO_FALLBACK_BALANCE
        # Leverage from SL distance — identical formula to the paper agents.
        if sl is not None and sl > 0 and entry > 0:
            sl_dist = abs(entry - sl) / entry
        else:
            sl_dist = 0.05
        if sl_dist < 0.005:
            sl_dist = 0.005
        # Liquidation-safe leverage — mirrors copy_trade_monitor.lev_after_buffer:
        # L <= 1 / (sl_dist * safety + mmr) keeps the forced-liquidation price
        # strictly beyond the stop (the old (1/sl_dist)*0.97 put liquidation
        # BEFORE the stop for any stop tighter than ~5%).
        _liq_safety = float(os.environ.get("DEMO_LIQ_SAFETY", "1.15"))
        _liq_mmr = float(os.environ.get("DEMO_LIQ_MMR", "0.006"))
        lev_from_sl = min(1.0 / (sl_dist * _liq_safety + _liq_mmr), DEMO_LEVERAGE_CAP)

        if mode == "spot_seq":
            allocated, leverage = bal, 1.0
        elif mode == "spot_seq_ch":
            # chart_hacker copier: 3% risk, dynamic leverage from SL distance —
            # identical to copy_trade_monitor's spot_seq_ch branch.
            allocated, leverage = bal * (DEMO_RISK_PCT_3 / 100.0), lev_from_sl
        elif mode == "spot_lev_3x":
            allocated, leverage = bal, DEMO_SPOT_LEV_3X
        elif mode == "lev_3pct":
            allocated, leverage = bal * (DEMO_RISK_PCT_3 / 100.0), lev_from_sl
        else:  # lev_5pct
            allocated, leverage = bal * (DEMO_RISK_PCT_5 / 100.0), lev_from_sl

        allocated *= DEMO_NOTIONAL_SAFETY_PCT  # headroom for fees/slippage
        notional = allocated * leverage
        lev_int = max(1, min(int(round(leverage)), int(DEMO_LEVERAGE_CAP)))
        return mode, allocated, lev_int, notional

    async def _open_demo_count(self, exchange: str, account_name: str) -> int:
        """How many demo positions/orders are currently live for this account.

        Only counts RECENT orders (last 24h) toward the concurrency cap. Older
        pending limit orders are sitting at entry prices far from current market
        — they should NOT block new signals from being placed. The paper-level
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

        Phase 1 (2026-05-29) FIX — duplicate-order stacking on restart:
        previously this only excluded positions that had a PAPER fill
        (competition_trades). A pending signal whose paper leg had not yet
        filled was NOT excluded, so EVERY daemon restart re-placed a fresh
        resting limit order for it — duplicate orders piled up on the exchange
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
            "WHERE tracked_position_id IS NOT NULL AND status IN ('pending','filled')"
        )
        self._mirrored = {r["tracked_position_id"] for r in rows}
        LOG.info("Loaded %d already-mirrored tracked positions", len(self._mirrored))

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
        """Get mirrored signals that are now cancelled — cancel their demo orders."""
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
                # — they only mirror their own source. (2026-05-29 rename: the
                # old `startswith('copy_ch_')` test no longer matches
                # 'copy_charthacker', so exclude it explicitly.)
                if agent_id != 'copy_charthacker' and not agent_id.startswith('copy_rose_'):
                    return agent_id
        return None

    async def _mirror_signal(self, signal: Dict, mappings: Dict[str, List[Dict]]):
        """Place limit order on demo accounts for a new signal."""
        tp_id = signal["id"]
        sym = signal["instrument_symbol"]
        # Strip options contract suffixes only — keep :USDT perp suffix
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
            return
        
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
        
        # Phase 6 (2026-05-29) — ACCURATE per-agent sizing.
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
            # NOTE: we deliberately do NOT cap to signal["notional_usd"] — that
            # field is always the paper WALLET (1000), not a position ceiling.
            # Capping to it would clamp spot_lev_3x (3x notional) and the
            # leveraged agents back to ~1000 and silently undo accurate sizing.
            mode, allocated, leverage, notional_eff = self._compute_demo_size(
                agent_id, balance, entry, sl)

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

            # Symbol availability gate — checked against the DEMO environment's
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
                    LOG.info("Signal #%d: %s not listed on %s (demo env) — will skip this exchange",
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
                LOG.debug("Signal #%d → %s/%s: qty=%.6f below min=%.6f for %s — skip",
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
                    ext_id = acc[0].external_order_id
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
                    # Removed the per-insert `pool.close()` — the pool is a shared
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
                    # Record error (no pool.close() — shared singleton, see above)
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
        """Phase 1 (2026-05-29) NEW — demo fill reconciliation.

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
                # raw["fees"]). Best-effort — NULL stays NULL if unavailable.
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
        etc.), we poll `fetch_order` to capture: exit_price, exit_fee, demo_pnl,
        slippage_exit. The demo_orders table has all these columns — they were
        simply never written. This makes the paper-vs-demo comparison
        statistically valid: paper's theoretical exit vs the exchange's actual fill.
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
            try:
                client = self._adapter._get_client(r["exchange"], r["account_name"])
                raw = await asyncio.to_thread(
                    client.fetch_order, r["exchange_order_id"], r["symbol"])
            except Exception as exc:
                LOG.debug("exit_reconcile fetch_order %s failed: %s",
                          r["exchange_order_id"], exc)
                continue
            status = (raw.get("status") or "").lower()
            # Only process orders that are fully closed on the exchange
            if status != "closed":
                continue
            avg = raw.get("average") or raw.get("price")
            if not avg:
                continue
            avg = float(avg)
            paper_entry = float(r["paper_entry"] or 0)
            notional = float(r["notional_usd"] or 0)
            direction = (r["direction"] or "long").lower()

            # P&L: (exit - entry) / entry * notional for long, inverse for short
            pnl = 0.0
            if paper_entry > 0 and notional > 0:
                if direction == "long":
                    pnl = (avg - paper_entry) / paper_entry * notional
                else:
                    pnl = (paper_entry - avg) / paper_entry * notional

            exit_fee = self._extract_fee(raw)
            slip_exit = ((avg - float(r.get("paper_tp") or 0)) / float(r.get("paper_tp") or 1)
                         if r.get("paper_tp") else None)

            await pool.execute(
                "UPDATE public.demo_orders SET "
                "status='closed', closed_at=NOW(), exit_price=%s, demo_pnl=%s, "
                "slippage_exit=%s, exit_fee=%s, fees_synced_at=NOW(), "
                "updated_at=NOW() WHERE id=%s AND closed_at IS NULL",
                (avg, round(pnl, 8), slip_exit, exit_fee, r["id"]))
            LOG.info("Demo order %s CLOSED @ %.6f pnl=%.4f (order %s)",
                     r["symbol"], avg, pnl, r["exchange_order_id"])
            flog("exit_reconciled", demo_order_id=r["id"],
                 exchange=r["exchange"], account=r["account_name"],
                 symbol=r["symbol"], direction=direction,
                 exit_price=avg, demo_pnl=round(pnl, 4),
                 exit_fee=exit_fee, order_id=r["exchange_order_id"])

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
        wrong — orders should track toward entry indefinitely). This only
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
            "AND tp.status IN ('open', 'closed', 'expired')",
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

    async def tick(self):
        mappings = await self._load_mappings()
        if not mappings:
            return

        # 0. Refresh real demo-account balances (drives accurate per-agent sizing)
        await self._refresh_balances(mappings)

        # 1. Place limit orders for new signals
        signals = await self._get_new_signals()
        if signals:
            LOG.info("Tick: %d new signals to mirror", len(signals))
            for s in signals:
                await self._mirror_signal(s, mappings)
                self._mirrored.add(s["id"])
                await asyncio.sleep(0.3)  # Rate limit
        
        # 1b. Reconcile pending demo orders against the exchange (mark fills)
        await self._reconcile_fills()

        # 1b2. Reconcile filled-but-open positions (capture exit data)
        await self._reconcile_exits()

        # 1c. Cancel demo orders where the paper tracked_position is already
        # open/closed/expired. These demo limits missed their entry and will
        # never fill — the paper side already moved on. (Not the same as the
        # stale-by-time cleanup — these are orphaned by paper lifecycle, which
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
                LOG.info("Signal #%d cancelled — removed %d demo orders", tp_id, len(orders))

    async def run_forever(self):
        LOG.info("DemoBridge starting — signal-driven (poll=%ds)", POLL_INTERVAL_S)
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
