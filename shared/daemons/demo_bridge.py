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
import asyncio, logging, os, signal, sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/opt/tickles")
from shared.utils.db import DatabasePool
from shared.execution.ccxt_adapter import CcxtExecutionAdapter
from shared.execution.protocol import (
    ExecutionIntent, DIRECTION_LONG, DIRECTION_SHORT, ORDER_TYPE_LIMIT,
)

LOG = logging.getLogger("demo.bridge")
POLL_INTERVAL_S = int(os.environ.get("DEMO_BRIDGE_POLL_S", "15"))

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


class DemoBridge:
    def __init__(self):
        self._pool = None
        self._adapter = CcxtExecutionAdapter(demo_trading=True)
        self._stop = asyncio.Event()
        self._mirrored: set = set()        # tracked_position IDs already mirrored
        self._orders: Dict[int, Dict] = {} # tracked_position_id → {account: order_id}

    async def _ensure_pool(self):
        if self._pool is None:
            self._pool = await DatabasePool.get_instance()
        return self._pool

    async def _load_mappings(self) -> Dict[str, List[Dict]]:
        """Load agent→account mappings: {agent_id: [{exchange, account_name}, ...]}"""
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
        for r in rows:
            mappings.setdefault(r["agent_id"], []).append({
                "exchange": r["exchange"], "account_name": r["account_name"],
            })
        return mappings

    async def _load_mirrored(self):
        """Load already-mirrored tracked_position IDs from persistent tracking."""
        pool = await self._ensure_pool()
        rows = await pool.fetch_all(
            "SELECT DISTINCT tracked_position_id FROM public.competition_trades "
            "WHERE contest_id = 'copy-trade-scenarios' AND tracked_position_id IS NOT NULL"
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
            "AND tp.signal_timestamp >= NOW() - INTERVAL '7 days' "
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
        # ChartHacker → copy_ch_ai_vision
        if actor_id == 'jarvais_chart_hacker':
            return 'copy_ch_ai_vision'
        # Rose → copy_rose_a (primary rose agent)
        if actor_id == 'jarvais_rose_ch':
            return 'copy_rose_a'
        # Traders → all regular agents (return the first mapped one, or pick specifically)
        # For bridge purposes, we need to know WHICH agent is assigned to mirror
        # We check all mapped agents to see if they trade this signal type
        for agent_id in mappings:
            if actor_id.startswith('jarvais_trader_'):
                # All regular agents trade trader signals
                if not agent_id.startswith('copy_ch_') and not agent_id.startswith('copy_rose_'):
                    return agent_id
        return None

    async def _mirror_signal(self, signal: Dict, mappings: Dict[str, List[Dict]]):
        """Place limit order on demo accounts for a new signal."""
        tp_id = signal["id"]
        sym = signal["instrument_symbol"]
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
        
        # Determine leverage from SL distance (must come BEFORE sizing now).
        leverage = 1
        if sl and sl > 0:
            sl_dist = abs(entry - sl) / entry
            if sl_dist > 0.005:
                leverage = min(int((1.0 / sl_dist) * 0.97), 100)

        # Phase 1 (2026-05-29) sizing — margin-based, NOT full-wallet.
        # Commit a small fixed margin per trade (matches the paper risk model)
        # then derive notional = margin * leverage. See DEMO_MARGIN_USD note at
        # the top of the file. Capped so it never exceeds the signal's own
        # notional (when the signal is genuinely small) and shaved by the safety
        # pct for fee/slippage headroom.
        margin_usd = DEMO_MARGIN_USD * DEMO_NOTIONAL_SAFETY_PCT
        notional_eff = margin_usd * leverage
        if notional > 0:
            notional_eff = min(notional_eff, notional)  # never upsize beyond the signal

        # Calculate quantity from notional / entry
        qty = notional_eff / entry if notional_eff > 0 and entry > 0 else 0.001
        if entry >= 1000:
            qty = round(qty, 3)
        elif entry >= 1:
            qty = round(qty, 1)
        else:
            qty = max(round(qty, 6), 0.001)
        
        for acct in unique_accounts:
            try:
                intent = ExecutionIntent(
                    company_id="jarvais", strategy_id=None, agent_id="demo_bridge",
                    exchange=acct["exchange"],
                    account_id_external=f"demo_signal_{tp_id}",
                    symbol=sym, direction=direction, order_type=ORDER_TYPE_LIMIT,
                    quantity=qty, requested_price=entry,
                    stop_loss=sl if sl > 0 else None,
                    take_profit=tp if tp > 0 else None,
                    leverage=leverage if leverage > 1 else None,
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
                    self._orders.setdefault(tp_id, {})[acct["account_name"]] = ext_id
                    LOG.info("Signal #%d → %s/%s: LIMIT %s %s qty=%.4f @ %.4f SL=%s TP=%s lev=%dx (order %s)",
                             tp_id, acct["exchange"], acct["account_name"],
                             direction, sym, qty, entry, sl, tp, leverage,
                             ext_id or "?")
                    # Record in demo_orders table for comparison tracking.
                    # Phase 1 (2026-05-29): record the EFFECTIVE notional we
                    # actually sized to (after the safety factor), not the raw
                    # 1000, so the dashboard "Demo Orders $" KPI is honest.
                    # Removed the per-insert `pool.close()` — the pool is a shared
                    # singleton; closing it here broke subsequent ticks/daemons.
                    try:
                        pool = await self._ensure_pool()
                        await pool.execute(
                            "INSERT INTO public.demo_orders "
                            "(tracked_position_id, exchange, account_name, exchange_order_id, "
                            "symbol, direction, paper_entry, paper_sl, paper_tp, leverage, quantity, "
                            "notional_usd, status, ordered_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',NOW()) "
                            "ON CONFLICT DO NOTHING",
                            (tp_id, acct["exchange"], acct["account_name"], ext_id,
                             sym, direction, entry, sl, tp, leverage, qty, notional_eff)
                        )
                    except Exception as exc:
                        LOG.debug("demo_orders insert (accepted) failed: %s", exc)
                else:
                    rej = [u for u in updates if u.status == "rejected"]
                    LOG.warning("Signal #%d → %s/%s FAILED: %s",
                                tp_id, acct["exchange"], acct["account_name"],
                                rej[0].message if rej else "unknown")
                    # Record error (no pool.close() — shared singleton, see above)
                    try:
                        pool = await self._ensure_pool()
                        await pool.execute(
                            "INSERT INTO public.demo_orders "
                            "(tracked_position_id, exchange, account_name, symbol, direction, "
                            "paper_entry, paper_sl, paper_tp, leverage, quantity, notional_usd, "
                            "status, error_message, ordered_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'rejected',%s,NOW())",
                            (tp_id, acct["exchange"], acct["account_name"], sym, direction,
                             entry, sl, tp, leverage, qty, notional_eff,
                             rej[0].message[:500] if rej else "unknown")
                        )
                    except Exception as exc:
                        LOG.debug("demo_orders insert (rejected) failed: %s", exc)
            except Exception as exc:
                LOG.error("Signal #%d → %s/%s ERROR: %s",
                          tp_id, acct["exchange"], acct["account_name"], exc)

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
            "       paper_entry "
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
                await pool.execute(
                    "UPDATE public.demo_orders SET status='filled', demo_entry=%s, "
                    "filled_at=NOW(), slippage_entry=%s, updated_at=NOW() WHERE id=%s",
                    (avg, slip, r["id"]))
                LOG.info("Demo order %s FILLED @ %.6f (slip=%s)",
                         r["exchange_order_id"], avg,
                         f"{slip:.4%}" if slip is not None else "n/a")
            elif status in ("canceled", "cancelled", "rejected", "expired"):
                await pool.execute(
                    "UPDATE public.demo_orders SET status='cancelled', updated_at=NOW() "
                    "WHERE id=%s", (r["id"],))

    async def tick(self):
        mappings = await self._load_mappings()
        if not mappings:
            return
        
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

        # 2. Cancel orders for cancelled/expired signals
        cancelled = await self._get_cancelled_signals()
        for c in cancelled:
            tp_id = c["id"]
            orders = self._orders.pop(tp_id, {})
            for acct_name, order_id in orders.items():
                if order_id:
                    await self._cancel_demo_order(tp_id, acct_name, order_id)
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
