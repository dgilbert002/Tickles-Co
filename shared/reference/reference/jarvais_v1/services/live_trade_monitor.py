"""
live_trade_monitor.py — Background service that polls exchange accounts,
syncs order fills, handles TP/SL hits, updates P&L, and detects manual trades.

Started alongside TradingFloor in run_dashboard.py.
"""

import json
import logging
import threading
import time
from datetime import datetime, date
from typing import Dict, List, Optional, Any

logger = logging.getLogger("jarvais.live_trade_monitor")

_monitor_instance: Optional["LiveTradeMonitor"] = None
_monitor_lock = threading.Lock()


def get_live_trade_monitor() -> Optional["LiveTradeMonitor"]:
    return _monitor_instance


def start_live_trade_monitor(db, config) -> "LiveTradeMonitor":
    global _monitor_instance
    with _monitor_lock:
        if _monitor_instance and _monitor_instance._running:
            return _monitor_instance
        _monitor_instance = LiveTradeMonitor(db, config)
        _monitor_instance.start()
        return _monitor_instance


class LiveTradeMonitor:
    """Polls exchange accounts every N seconds, syncs positions/orders,
    handles TP/SL level hits, and aggregates daily P&L."""

    POLL_INTERVAL = 10  # seconds

    MAX_FETCH_RETRIES = 10  # after this many consecutive failures, mark trade for review

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_pnl_update: Dict[str, float] = {}
        self._cached_positions: Dict[str, List[Dict]] = {}
        self._cached_balances: Dict[str, Dict] = {}
        self._cache_lock = threading.Lock()
        self._fetch_retry_counts: Dict[str, int] = {}  # order_id -> consecutive failures

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop,
                                        daemon=True, name="live-trade-monitor")
        self._thread.start()
        logger.info("[LiveTradeMonitor] Started (poll every %ds)", self.POLL_INTERVAL)

    def stop(self):
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15)
        logger.info("[LiveTradeMonitor] Stopped")

    # ─────────────────────────────────────────────────────────────
    # Main polling loop
    # ─────────────────────────────────────────────────────────────

    def _poll_loop(self):
        time.sleep(5)  # let other services init first
        while self._running and not self._stop_event.is_set():
            try:
                accounts = self.db.fetch_all(
                    "SELECT * FROM trading_accounts WHERE enabled = 1 AND live_trading = 1")
                for acct in (accounts or []):
                    try:
                        self._sync_account(acct)
                    except Exception as e:
                        logger.warning(f"[LiveTradeMonitor] Sync error for "
                                       f"{acct['account_id']}: {e}")
            except Exception as e:
                logger.error(f"[LiveTradeMonitor] Poll loop error: {e}")

            self._stop_event.wait(self.POLL_INTERVAL)

    def _sync_account(self, account: dict):
        """Sync one account: fetch positions/orders, check fills, update P&L."""
        from core.ccxt_executor import get_executor

        aid = account["account_id"]
        executor = get_executor(aid, self.db)
        if not executor or not executor.connected:
            return

        # Fetch balance
        try:
            bal = executor.get_balance()
            with self._cache_lock:
                self._cached_balances[aid] = bal
            self.db.execute(
                "UPDATE trading_accounts SET cached_balance = %s, "
                "cached_balance_at = NOW() WHERE account_id = %s",
                (bal.get("total", 0), aid))
        except Exception as e:
            logger.debug(f"[LiveTradeMonitor] Balance fetch failed for {aid}: {e}")

        # Fetch positions from exchange
        positions_ok = False
        try:
            positions = executor.get_positions()
            positions_ok = True
            with self._cache_lock:
                self._cached_positions[aid] = positions
        except Exception as e:
            logger.warning(f"[LiveTradeMonitor] Positions fetch failed for {aid}: {e}")
            positions = []

        # Fetch open orders from exchange
        try:
            open_orders = executor.get_open_orders()
        except Exception as e:
            logger.debug(f"[LiveTradeMonitor] Orders fetch failed for {aid}: {e}")
            open_orders = []

        # Subscribe active trade symbols to PriceStreamer for real-time prices
        self._subscribe_trade_symbols(aid)

        # Check our pending live_trades for fills
        self._check_order_fills(aid, open_orders, positions)

        # Check position expiry on pending orders
        self._check_position_expiry(aid, account, open_orders)

        # Only check TP/SL hits and close detection when we have a reliable
        # positions snapshot.  If the fetch failed, positions=[] and every open
        # trade would be falsely detected as "position closed on exchange".
        if positions_ok:
            self._check_tp_sl_hits(aid, positions, account)
            self._close_dust_positions(aid, positions, account)
            self._sync_manual_trades(aid, positions, account.get("exchange", "unknown"))
        else:
            logger.info(f"[LiveTradeMonitor] Skipping TP/SL/close checks for {aid} "
                        f"(positions fetch failed — avoiding false closes)")

        # Retry missing exit prices on recently closed trades
        self._retry_missing_exit_prices(aid, account)

        # Update unrealised P&L (safe even with empty positions — just a no-op)
        self._update_unrealised_pnl(aid, positions)

        # Periodic daily P&L aggregation (once per minute per account)
        now = time.time()
        if now - self._last_pnl_update.get(aid, 0) > 60:
            self._update_daily_pnl(aid)
            self._last_pnl_update[aid] = now

    # ─────────────────────────────────────────────────────────────
    # WebSocket price subscription for live trade symbols
    # ─────────────────────────────────────────────────────────────

    def _subscribe_trade_symbols(self, account_id: str):
        """Subscribe active live trade symbols to the PriceStreamer for
        real-time WebSocket pricing instead of relying only on polling."""
        try:
            from services.price_streamer import get_price_streamer
            streamer = get_price_streamer()
            if not streamer:
                return
            active = self.db.fetch_all(
                "SELECT DISTINCT symbol FROM live_trades "
                "WHERE account_id = %s AND status IN ('pending','open','partial_closed')",
                (account_id,))
            if not active:
                return
            symbols = [r["symbol"] for r in active if r.get("symbol")]
            if symbols:
                streamer.subscribe(symbols)
        except Exception as e:
            logger.debug(f"[LiveTradeMonitor] PriceStreamer subscribe failed: {e}")

    # ─────────────────────────────────────────────────────────────
    # Order fill detection
    # ─────────────────────────────────────────────────────────────

    def _check_order_fills(self, account_id: str, exchange_orders: list,
                           positions: list):
        """Check if any 'pending' live_trades have been filled on the exchange."""
        pending = self.db.fetch_all(
            "SELECT * FROM live_trades WHERE account_id = %s AND status = 'pending'",
            (account_id,))
        if not pending:
            return

        import re as _re
        open_order_ids = {o["id"] for o in exchange_orders}
        position_symbols = {p["symbol"] for p in positions
                           if p.get("contracts", 0) != 0 or p.get("notional", 0) != 0}
        _norm_cache = {s: _re.sub(r'[/:\-_\s.]', '', s.upper()) for s in position_symbols}

        def _sym_in_positions(sym: str) -> bool:
            """Match symbol against position list with fuzzy normalisation
            (strips /:-_.spaces). Prevents mismatches like BTC/USDT:USDT vs BTCUSDT."""
            if sym in position_symbols:
                return True
            norm = _re.sub(r'[/:\-_\s.]', '', sym.upper())
            return any(norm == v for v in _norm_cache.values())

        for trade in pending:
            oid = trade.get("order_id", "")
            exsym = trade.get("exchange_symbol", "")

            if oid and oid not in open_order_ids and _sym_in_positions(exsym):
                # Order disappeared and we have a position — verify with
                # exchange that the order is truly filled/closed before
                # marking as open (prevents ghost fills from API hiccups).
                verified_fill = False
                try:
                    order_info = self._fetch_order_safe(
                        account_id, oid, exsym, expect="closed")
                    if order_info is None:
                        continue
                    ex_status = (order_info.get("status") or "").lower()
                    if ex_status in ("closed", "filled"):
                        verified_fill = True
                        self._fetch_retry_counts.pop(oid, None)
                    elif ex_status == "open":
                        self._fetch_retry_counts.pop(oid, None)
                        logger.debug(
                            f"[LiveTradeMonitor] Order {oid} still open "
                            f"(API glitch?) — skipping fill for "
                            f"#{trade['id']} {exsym}")
                        continue
                except Exception as exc:
                    if not self._handle_fetch_retry(oid, trade, exc):
                        continue
                    verified_fill = False
                    continue

                if verified_fill:
                    exsym_norm = _re.sub(r'[/:\-_\s.]', '', exsym.upper())
                    pos = next(
                        (p for p in positions
                         if p["symbol"] == exsym
                         or _re.sub(r'[/:\-_\s.]', '', (p.get("symbol") or "").upper()) == exsym_norm),
                        None)
                    actual_entry = (pos["entry_price"] if pos
                                    else trade["entry_price"])
                    self.db.execute(
                        "UPDATE live_trades SET status = 'open', "
                        "filled_at = NOW(), actual_entry_price = %s "
                        "WHERE id = %s",
                        (actual_entry, trade["id"]))
                    logger.info(
                        f"[LiveTradeMonitor] Order filled (verified): "
                        f"trade #{trade['id']} {exsym} @ {actual_entry}")
                    did = trade.get("dossier_id")
                    if did:
                        try:
                            from services.trading_floor import (
                                _snapshot_conditions_at_entry)
                            _snapshot_conditions_at_entry(self.db, did)
                        except Exception as e:
                            logger.debug(
                                f"[LiveTradeMonitor] Snapshot error #{did}: {e}")

            elif oid and oid not in open_order_ids and not _sym_in_positions(exsym):
                # Order gone AND no position — verify before marking cancelled
                # (exchange API throttling/maintenance can return empty lists)
                try:
                    cancel_info = self._fetch_order_safe(
                        account_id, oid, exsym, expect="open")
                    if cancel_info is None:
                        continue
                    cancel_status = (cancel_info.get("status") or "").lower()
                    if cancel_status == "open":
                        self._fetch_retry_counts.pop(oid, None)
                        logger.debug(
                            f"[LiveTradeMonitor] Order {oid} still open "
                            f"(empty list glitch) — skipping cancel for "
                            f"#{trade['id']} {exsym}")
                        continue
                    self._fetch_retry_counts.pop(oid, None)
                except Exception as cancel_exc:
                    if not self._handle_fetch_retry(oid, trade, cancel_exc):
                        continue
                    continue

                self.db.execute(
                    "UPDATE live_trades SET status = 'cancelled', closed_at = NOW(), "
                    "realised_pnl = NULL, unrealised_pnl = NULL, "
                    "realised_pnl_pct = NULL, unrealised_pnl_pct = NULL "
                    "WHERE id = %s",
                    (trade["id"],))
                logger.info(f"[LiveTradeMonitor] Order cancelled (verified): "
                            f"trade #{trade['id']} {exsym} — P&L zeroed")
                self._sync_dossier_on_cancel(trade)

    # ─────────────────────────────────────────────────────────────
    # Fetch order helpers (Bybit-safe)
    # ─────────────────────────────────────────────────────────────

    def _fetch_order_safe(self, account_id: str, order_id: str,
                          symbol: str, expect: str = "closed") -> Optional[Dict]:
        """Fetch an order using the appropriate CCXT method.

        Bybit/Bitget: uses fetchClosedOrder/fetchOpenOrder, then fetchOrder.
        Blofin (no fetchOrder support): scans fetchClosedOrders/fetchOpenOrders
        lists for matching order_id.
        Other exchanges: tries fetchOrder directly.
        """
        from core.ccxt_executor import get_executor
        executor = get_executor(account_id, self.db)
        if not executor:
            return None

        exchange = executor._exchange
        ex_id = getattr(exchange, "id", "").lower()
        has_fetch_order = exchange.has.get("fetchOrder")

        with executor._lock:
            if ex_id in ("bybit", "bitget"):
                try:
                    if expect == "closed" and hasattr(exchange, "fetch_closed_order"):
                        return exchange.fetch_closed_order(order_id, symbol)
                    elif expect == "open" and hasattr(exchange, "fetch_open_order"):
                        return exchange.fetch_open_order(order_id, symbol)
                except Exception:
                    pass
                params = {"acknowledged": True} if ex_id == "bybit" else {}
                return exchange.fetch_order(order_id, symbol, params)

            if not has_fetch_order:
                return self._fetch_order_by_list_scan(
                    exchange, order_id, symbol, expect)

            return exchange.fetch_order(order_id, symbol)

    def _fetch_order_by_list_scan(self, exchange, order_id: str,
                                  symbol: str, expect: str) -> Optional[Dict]:
        """For exchanges without fetchOrder (e.g. Blofin): scan order lists
        to find a specific order by its ID."""
        oid_str = str(order_id)

        if expect == "closed":
            scan_order = [
                ("fetch_closed_orders", "closed"),
                ("fetch_open_orders", "open"),
            ]
        else:
            scan_order = [
                ("fetch_open_orders", "open"),
                ("fetch_closed_orders", "closed"),
            ]

        for method_name, _ in scan_order:
            method = getattr(exchange, method_name, None)
            if not method:
                continue
            try:
                orders = method(symbol, limit=100)
                for o in (orders or []):
                    if str(o.get("id")) == oid_str:
                        return o
            except Exception:
                continue

        raise Exception(
            f"Order {order_id} not found in open/closed lists for {symbol}")

    def _handle_fetch_retry(self, order_id: str, trade: dict,
                            exc: Exception) -> bool:
        """Track consecutive fetch failures for an order.
        Returns True if the trade was marked stale (caller should skip),
        False if the caller should just skip this cycle normally."""
        count = self._fetch_retry_counts.get(order_id, 0) + 1
        self._fetch_retry_counts[order_id] = count

        if count >= self.MAX_FETCH_RETRIES:
            self._fetch_retry_counts.pop(order_id, None)
            self.db.execute(
                "UPDATE live_trades SET status = 'abandoned', "
                "realised_pnl = NULL, unrealised_pnl = NULL, "
                "realised_pnl_pct = NULL, unrealised_pnl_pct = NULL, "
                "notes = CONCAT(COALESCE(notes,''), %s) WHERE id = %s",
                (f"\n[auto] Marked abandoned after {count} consecutive fetch "
                 f"failures: {str(exc)[:200]}", trade["id"]))
            logger.warning(
                f"[LiveTradeMonitor] Order {order_id} marked ABANDONED after "
                f"{count} failures — trade #{trade['id']} needs manual review")
            return True

        if count <= 3:
            logger.debug(
                f"[LiveTradeMonitor] fetch_order({order_id}) attempt "
                f"{count}/{self.MAX_FETCH_RETRIES}: {exc}")
        elif count == 4:
            logger.warning(
                f"[LiveTradeMonitor] fetch_order({order_id}) failing "
                f"repeatedly ({count}x) — will mark stale after "
                f"{self.MAX_FETCH_RETRIES}: {str(exc)[:120]}")
        return False

    # ─────────────────────────────────────────────────────────────
    # Position expiry + entry threshold
    # ─────────────────────────────────────────────────────────────

    def _check_position_expiry(self, account_id: str, account: dict,
                               exchange_orders: list):
        """Cancel pending orders that have exceeded position_expiry_hours."""
        from core.ccxt_executor import get_executor

        expiry_hours = account.get("position_expiry_hours")
        if not expiry_hours or int(expiry_hours) <= 0:
            return

        expiry_secs = int(expiry_hours) * 3600
        pending = self.db.fetch_all(
            "SELECT * FROM live_trades WHERE account_id = %s AND status = 'pending'",
            (account_id,))
        if not pending:
            return

        now = time.time()
        for trade in pending:
            created = trade.get("created_at")
            if not created:
                continue
            if hasattr(created, "timestamp"):
                created_ts = created.timestamp()
            else:
                try:
                    from datetime import datetime
                    created_ts = datetime.fromisoformat(str(created)).timestamp()
                except Exception:
                    continue

            age = now - created_ts
            if age < expiry_secs:
                continue

            # Expired -- cancel order on exchange and mark as expired
            oid = trade.get("order_id", "")
            exsym = trade.get("exchange_symbol", "")
            executor = get_executor(account_id, self.db)
            if executor and oid:
                try:
                    executor.cancel_order(oid, exsym)
                except Exception as e:
                    logger.debug(f"[LiveTradeMonitor] Cancel expired order failed: {e}")

            self.db.execute(
                "UPDATE live_trades SET status = 'expired', closed_at = NOW(), "
                "close_comment = CONCAT(COALESCE(close_comment,''), ' | Expired after %sh') "
                "WHERE id = %s AND status = 'pending'",
                (expiry_hours, trade["id"]))
            logger.info(f"[LiveTradeMonitor] Trade #{trade['id']} expired after "
                        f"{expiry_hours}h (age={age/3600:.1f}h)")

            try:
                self.db.execute("""
                    INSERT INTO live_trade_audit
                    (account_id, dossier_id, duo_id, action, exchange, symbol, success, error_message)
                    VALUES (%s, %s, %s, 'expire_order', %s, %s, 1, %s)
                """, (account_id, trade.get("dossier_id"),
                      trade.get("duo_id") or trade.get("trade_source", "unknown"),
                      account.get("exchange", ""), exsym,
                      f"Expired after {expiry_hours}h"))
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────
    # TP/SL hit detection
    # ─────────────────────────────────────────────────────────────

    def _check_tp_sl_hits(self, account_id: str, positions: list,
                          account: dict):
        """Check open live trades for TP/SL level hits based on exchange P&L."""
        open_trades = self.db.fetch_all(
            "SELECT * FROM live_trades WHERE account_id = %s "
            "AND status IN ('open', 'partial_closed')",
            (account_id,))
        if not open_trades:
            return

        import re as _re
        def _find_pos(exsym, positions):
            """Fuzzy-match position by symbol, consistent with fill detection."""
            pos = next((p for p in positions if p["symbol"] == exsym), None)
            if not pos:
                norm = _re.sub(r'[/:\-_\s.]', '', exsym.upper())
                pos = next((p for p in positions
                            if _re.sub(r'[/:\-_\s.]', '', (p.get("symbol") or "").upper()) == norm),
                           None)
            return pos

        for trade in open_trades:
            exsym = trade.get("exchange_symbol", "")
            pos = _find_pos(exsym, positions)

            if not pos or pos.get("contracts", 0) == 0:
                # Position closed on exchange (SL hit or manual close)
                self._handle_position_closed(trade, account)
                continue

            mark = pos.get("mark_price", 0)
            if mark <= 0:
                continue

            direction = (trade.get("direction") or "BUY").upper()

            # Explicit SL price check — catch breaches even if exchange SL
            # order was cancelled/stacked/failed. Close position on exchange first
            # to prevent orphaned positions.
            trade_sl = float(trade.get("stop_loss") or 0)
            if trade_sl > 0:
                sl_breached = (direction == "BUY" and mark <= trade_sl) or \
                              (direction == "SELL" and mark >= trade_sl)
                if sl_breached:
                    logger.warning(f"[LiveTradeMonitor] SL BREACH detected for "
                                   f"LT#{trade['id']}: mark={mark} SL={trade_sl} "
                                   f"dir={direction} — force-closing position")
                    from core.ccxt_executor import get_executor
                    _executor = get_executor(account["account_id"], self.db)
                    if _executor:
                        try:
                            _executor.close_position(
                                trade.get("exchange_symbol"),
                                direction)
                        except Exception as _ce:
                            logger.error(f"[LiveTradeMonitor] Force-close failed "
                                         f"LT#{trade['id']}: {_ce}")
                    self._handle_position_closed(trade, account)
                    continue
            entry = float(trade.get("actual_entry_price") or trade.get("entry_price") or 0)
            tp1 = float(trade.get("take_profit_1") or 0)
            tp2 = float(trade.get("take_profit_2") or 0)
            tp3 = float(trade.get("take_profit_3") or 0)
            sl = float(trade.get("stop_loss") or 0)

            tp_progress = trade.get("tp_progress") or ""

            # Process TPs in correct order (TP1 -> TP2 -> TP3) so partial
            # close amounts are calculated on the correct remaining position.
            if direction == "BUY":
                if tp1 > 0 and mark >= tp1 and "tp1" not in tp_progress:
                    self._handle_tp1_hit(trade, account, mark)
                    tp_progress = self.db.fetch_one(
                        "SELECT tp_progress FROM live_trades WHERE id = %s",
                        (trade["id"],))
                    tp_progress = (tp_progress or {}).get("tp_progress") or ""
                if tp2 > 0 and mark >= tp2 and "tp2" not in tp_progress:
                    self._handle_tp2_hit(trade, account, mark)
                    tp_progress = self.db.fetch_one(
                        "SELECT tp_progress FROM live_trades WHERE id = %s",
                        (trade["id"],))
                    tp_progress = (tp_progress or {}).get("tp_progress") or ""
                if tp3 > 0 and mark >= tp3 and "tp3" not in tp_progress:
                    self._handle_tp3_hit(trade, account, mark)
            else:  # SELL
                if tp1 > 0 and mark <= tp1 and "tp1" not in tp_progress:
                    self._handle_tp1_hit(trade, account, mark)
                    tp_progress = self.db.fetch_one(
                        "SELECT tp_progress FROM live_trades WHERE id = %s",
                        (trade["id"],))
                    tp_progress = (tp_progress or {}).get("tp_progress") or ""
                if tp2 > 0 and mark <= tp2 and "tp2" not in tp_progress:
                    self._handle_tp2_hit(trade, account, mark)
                    tp_progress = self.db.fetch_one(
                        "SELECT tp_progress FROM live_trades WHERE id = %s",
                        (trade["id"],))
                    tp_progress = (tp_progress or {}).get("tp_progress") or ""
                if tp3 > 0 and mark <= tp3 and "tp3" not in tp_progress:
                    self._handle_tp3_hit(trade, account, mark)

    def _replace_sl_on_exchange(self, executor, exsym: str, side: str,
                                remaining_size: float, new_sl_price: float,
                                tid: int):
        """Cancel existing SL orders and place a new stop at new_sl_price."""
        try:
            open_orders = executor.get_open_orders(exsym)
            for o in (open_orders or []):
                otype = (o.get("type") or "").lower()
                if otype in ("stop", "stop_market", "stop-loss"):
                    executor.cancel_order(o["id"], exsym)
                    logger.info(f"[LiveTradeMonitor] Cancelled old SL order "
                                f"{o['id']} on #{tid}")
        except Exception as e:
            logger.debug(f"[LiveTradeMonitor] Old SL cancel attempt #{tid}: {e}")
        if remaining_size <= 0:
            try:
                positions = executor.get_positions()
                pos = next((p for p in positions
                            if p.get("symbol") == exsym
                            and abs(p.get("contracts", 0)) > 0), None)
                remaining_size = abs(pos["contracts"]) if pos else 0
            except Exception:
                pass
        if remaining_size <= 0:
            logger.warning(f"[LiveTradeMonitor] Cannot place SL: zero remaining size for #{tid}")
            return
        # Use exchange-appropriate stop order type (bybit uses "stop",
        # blofin uses "stop" with different params). CCXT unified params
        # handle the mapping via triggerPrice + reduceOnly.
        order_type = "stop"
        params = {
            "stopPrice": new_sl_price,
            "reduceOnly": True,
            "triggerPrice": new_sl_price,
            "triggerType": "mark",
        }
        if hasattr(executor, 'exchange_id') and executor.exchange_id in ("blofin", "bitget"):
            params["marginMode"] = "isolated"
        with executor._lock:
            executor._exchange.create_order(
                symbol=exsym, type=order_type,
                side=side, amount=remaining_size,
                price=new_sl_price, params=params)

    def _get_actual_remaining(self, executor, exsym: str) -> float:
        """Query the exchange for the actual remaining position size.
        Prevents dust from rounding mismatches between our DB and exchange."""
        try:
            positions = executor.get_positions()
            import re as _re
            clean = _re.sub(r'[/:\-_\s.]', '', exsym.upper())
            for p in positions:
                if abs(p.get("contracts", 0)) == 0:
                    continue
                p_clean = _re.sub(r'[/:\-_\s.]', '', (p.get("symbol") or "").upper())
                if p["symbol"] == exsym or p_clean == clean:
                    return abs(p["contracts"])
        except Exception as e:
            logger.debug(f"[LiveTradeMonitor] _get_actual_remaining failed for {exsym}: {e}")
        return 0.0

    def _handle_tp1_hit(self, trade: dict, account: dict, mark_price: float):
        """TP1 hit: partial close, move SL to breakeven, free trade mode."""
        from core.ccxt_executor import get_executor

        tid = trade["id"]
        close_pct = int(account.get("tp1_close_pct", 50) or 50)
        size = float(trade.get("position_size") or 0)
        close_amount = size * (close_pct / 100)
        exsym = trade["exchange_symbol"]

        executor = get_executor(account["account_id"], self.db)
        if executor and close_amount > 0:
            try:
                executor.close_position(exsym, trade["direction"], close_amount)
            except Exception as e:
                logger.warning(f"[LiveTradeMonitor] TP1 partial close failed #{tid}: {e}")

        actual_remaining = self._get_actual_remaining(executor, exsym) if executor else 0
        if actual_remaining <= 0:
            actual_remaining = size - close_amount
        logger.info(f"[LiveTradeMonitor] TP1 #{tid}: closed {close_pct}%, "
                    f"actual remaining on exchange: {actual_remaining}")

        # Move SL to breakeven + buffer on the exchange
        sl_be = bool(account.get("sl_to_be_enabled", True))
        entry = float(trade.get("actual_entry_price") or trade.get("entry_price") or 0)
        if sl_be and entry > 0 and executor:
            try:
                direction = (trade.get("direction") or "BUY").upper()
                side = "buy" if direction == "SELL" else "sell"
                be_buffer_pct = 0.15 / 100
                if direction == "BUY":
                    be_price = entry * (1 + be_buffer_pct)
                else:
                    be_price = entry * (1 - be_buffer_pct)
                self._replace_sl_on_exchange(
                    executor, exsym, side, actual_remaining, be_price, tid)
                logger.info(f"[LiveTradeMonitor] SL moved to BE+buffer ({be_price:.5f}) on #{tid}")
            except Exception as e:
                logger.warning(f"[LiveTradeMonitor] SL-to-BE order failed #{tid}: {e}")

        # Free trade mode: increase leverage to reduce margin but cap safely
        free_trade = bool(account.get("free_trade_mode"))
        if free_trade and executor:
            try:
                lev_limits = executor.get_leverage_limits(exsym)
                max_lev = lev_limits.get("max", 100)
                safety_buffer = int(account.get("leverage_safety_buffer", 3) or 3)
                safe_lev = max(1, max_lev - safety_buffer)
                safe_lev = min(safe_lev, 50)
                executor.set_leverage(exsym, safe_lev)
                logger.info(f"[LiveTradeMonitor] Free trade mode: set {safe_lev}x "
                            f"leverage on #{tid} {exsym} (max={max_lev}, "
                            f"buffer={safety_buffer})")
            except Exception as e:
                logger.warning(f"[LiveTradeMonitor] Free trade leverage failed #{tid}: {e}")

        # Place TP2 as a take-profit order on exchange for remaining position.
        # Skip if mark price already past TP2 (avoid double-close race with
        # the regular TP hit detection loop).
        tp2_price = float(trade.get("take_profit_2") or 0)
        if tp2_price > 0 and executor and actual_remaining > 0:
            direction = (trade.get("direction") or "BUY").upper()
            mark = executor.get_mark_price(exsym) if hasattr(executor, 'get_mark_price') else 0
            tp2_already_hit = (mark > 0 and
                               ((direction == "BUY" and mark >= tp2_price) or
                                (direction == "SELL" and mark <= tp2_price)))
            if not tp2_already_hit:
                try:
                    tp_close_side = "sell" if direction == "BUY" else "buy"
                    tp2_close_pct = int(account.get("tp2_close_pct", 25) or 25)
                    tp2_amount = actual_remaining * (tp2_close_pct / 100)
                    tp2_amount = executor.amount_to_precision(exsym, tp2_amount)
                    if tp2_amount > 0:
                        executor.place_limit_order(
                            exchange_symbol=exsym,
                            side=tp_close_side,
                            amount=tp2_amount,
                            price=tp2_price,
                            params={"reduceOnly": True})
                        logger.info(f"[LiveTradeMonitor] Placed TP2 limit order on #{tid}: "
                                    f"{tp2_amount} @ {tp2_price}")
                except Exception as e:
                    logger.warning(f"[LiveTradeMonitor] TP2 order placement failed #{tid}: {e}")

        progress = (trade.get("tp_progress") or "") + "tp1,"
        self.db.execute(
            "UPDATE live_trades SET tp_progress = %s, sl_moved_to_be = 1, "
            "status = 'partial_closed' WHERE id = %s",
            (progress, tid))
        logger.info(f"[LiveTradeMonitor] TP1 hit on #{tid}: closed {close_pct}%, "
                    f"SL→BE={sl_be}{', free trade mode' if free_trade else ''}")

    def _handle_tp2_hit(self, trade: dict, account: dict, mark_price: float):
        """TP2 hit: partial close + move SL to TP1 level."""
        from core.ccxt_executor import get_executor

        tid = trade["id"]
        tp1_close_pct = int(account.get("tp1_close_pct", 50) or 50)
        close_pct = int(account.get("tp2_close_pct", 25) or 25)
        size = float(trade.get("position_size") or 0)
        close_amount = size * (close_pct / 100)
        exsym = trade["exchange_symbol"]

        executor = get_executor(account["account_id"], self.db)
        if executor and close_amount > 0:
            try:
                executor.close_position(exsym, trade["direction"], close_amount)
            except Exception as e:
                logger.warning(f"[LiveTradeMonitor] TP2 partial close failed #{tid}: {e}")

        actual_remaining = self._get_actual_remaining(executor, exsym) if executor else 0

        # Move SL from BE to TP1 level to lock in more profit
        tp1_price = float(trade.get("take_profit_1") or 0)
        if tp1_price > 0 and executor:
            try:
                direction = (trade.get("direction") or "BUY").upper()
                side = "buy" if direction == "SELL" else "sell"
                if actual_remaining <= 0:
                    after_tp2_pct = 100 - tp1_close_pct - close_pct
                    actual_remaining = size * (max(after_tp2_pct, 0) / 100)
                self._replace_sl_on_exchange(
                    executor, exsym, side, actual_remaining, tp1_price, tid)
                logger.info(f"[LiveTradeMonitor] SL trailed to TP1 ({tp1_price}) on #{tid}, "
                            f"remaining={actual_remaining}")
            except Exception as e:
                logger.warning(f"[LiveTradeMonitor] SL trail to TP1 failed #{tid}: {e}")

        progress = (trade.get("tp_progress") or "") + "tp2,"
        self.db.execute(
            "UPDATE live_trades SET tp_progress = %s WHERE id = %s",
            (progress, tid))
        logger.info(f"[LiveTradeMonitor] TP2 hit on #{tid}: closed {close_pct}% of remaining")

    def _handle_tp3_hit(self, trade: dict, account: dict, mark_price: float):
        """TP3 hit: close remaining position, using actual fill data."""
        from core.ccxt_executor import get_executor

        tid = trade["id"]
        direction = (trade.get("direction") or "BUY").upper()
        executor = get_executor(account["account_id"], self.db)
        if executor:
            try:
                executor.close_position(trade["exchange_symbol"], direction)
            except Exception as e:
                logger.warning(f"[LiveTradeMonitor] TP3 full close failed #{tid}: {e}")

        # Use actual fill price from exchange (same as _handle_position_closed)
        exit_price = None
        total_fees = 0.0
        if executor:
            try:
                exit_price, total_fees = self._fetch_exit_price(
                    executor, trade, direction)
            except Exception as e:
                logger.debug(f"[LiveTradeMonitor] TP3 fill fetch failed #{tid}: {e}")
        if not exit_price or exit_price <= 0:
            exit_price = mark_price

        entry = float(trade.get("actual_entry_price") or trade.get("entry_price") or 0)
        leverage = int(trade.get("leverage") or 1)
        margin = float(trade.get("margin_usd") or 0)
        accrued_funding = float(trade.get("accrued_funding") or 0)

        tp_progress = (trade.get("tp_progress") or "").lower()
        tp1_close_pct = int(account.get("tp1_close_pct", 50) or 50)
        tp2_close_pct = int(account.get("tp2_close_pct", 25) or 25)
        remaining_pct = 1.0
        if "tp2" in tp_progress:
            remaining_pct = max(0.01, (100 - tp1_close_pct - tp2_close_pct) / 100)
        elif "tp1" in tp_progress:
            remaining_pct = max(0.01, (100 - tp1_close_pct) / 100)
        effective_margin = margin * remaining_pct

        rpnl = None
        rpnl_pct = None
        if entry > 0 and exit_price > 0:
            raw_pct = ((exit_price - entry) / entry * 100) if direction == "BUY" \
                else ((entry - exit_price) / entry * 100)
            if effective_margin > 0:
                rpnl = round(effective_margin * leverage * (raw_pct / 100) - total_fees - accrued_funding, 4)
                rpnl_pct = round((rpnl / margin) * 100, 4) if margin > 0 else 0
            else:
                rpnl_pct = round(raw_pct * leverage, 4)

        self.db.execute(
            "UPDATE live_trades SET status = 'closed', closed_at = NOW(), "
            "tp_progress = CONCAT(IFNULL(tp_progress,''), 'tp3,'), "
            "actual_exit_price = %s, realised_pnl_pct = %s, realised_pnl = %s "
            "WHERE id = %s",
            (exit_price, rpnl_pct, rpnl, tid))
        logger.info(f"[LiveTradeMonitor] TP3 hit on #{tid}: fully closed, "
                    f"exit={exit_price}, P&L={rpnl_pct}% (${rpnl}), "
                    f"fees={total_fees:.4f}")

        self._sync_dossier_on_close(trade, rpnl=rpnl, exit_price=exit_price)
        self._trigger_postmortem(trade)
        self._notify_shadow_reconcile(trade)

    def _handle_position_closed(self, trade: dict, account: dict):
        """Position no longer exists on exchange — SL hit or manual close.

        Uses fetch_my_trades (actual fills) to find the real exit price.
        If the close fill hasn't propagated yet (exchange delay), we mark
        the trade as closed with exit_price=NULL; the retry mechanism in
        _retry_missing_exit_prices will fill it in on the next poll.
        """
        tid = trade["id"]
        if trade["status"] in ("closed", "cancelled", "abandoned"):
            return
        # Prune funding timestamp cache for this trade
        if hasattr(self, '_funding_timestamps'):
            self._funding_timestamps.pop(f"_funding_ts_{tid}", None)

        entry = float(trade.get("actual_entry_price") or trade.get("entry_price") or 0)
        direction = (trade.get("direction") or "BUY").upper()

        rpnl = None
        rpnl_pct = None
        exit_price = None
        total_fees = 0.0
        try:
            from core.ccxt_executor import get_executor
            executor = get_executor(account["account_id"], self.db)
            if executor:
                exit_price, total_fees = self._fetch_exit_price(
                    executor, trade, direction)
        except Exception as e:
            logger.warning(f"[LiveTradeMonitor] Failed to fetch exit P&L for "
                           f"#{tid}: {e} — will retry next cycle")

        if entry > 0 and exit_price and exit_price > 0:
            margin = float(trade.get("margin_usd") or 0)
            leverage = int(trade.get("leverage") or 1)
            accrued_funding = float(trade.get("accrued_funding") or 0)
            raw_price_pct = ((exit_price - entry) / entry * 100) if direction == "BUY" \
                else ((entry - exit_price) / entry * 100)
            if margin > 0:
                rpnl = round(margin * leverage * (raw_price_pct / 100)
                             - total_fees - accrued_funding, 4)
                rpnl_pct = round((rpnl / margin) * 100, 4)
            else:
                rpnl_pct = round(raw_price_pct * leverage, 4)

        self.db.execute(
            "UPDATE live_trades SET status = 'closed', closed_at = NOW(), "
            "actual_exit_price = %s, realised_pnl = %s, realised_pnl_pct = %s, "
            "total_fees = %s "
            "WHERE id = %s AND status NOT IN ('closed','cancelled','abandoned')",
            (exit_price, rpnl, rpnl_pct,
             round(total_fees + float(trade.get("accrued_funding") or 0), 6), tid))
        logger.info(f"[LiveTradeMonitor] Position closed on exchange for #{tid} "
                    f"(exit={exit_price}, pnl={rpnl_pct}%, fees={total_fees:.4f}, "
                    f"funding={float(trade.get('accrued_funding') or 0):.6f})")

        self._sync_dossier_on_close(trade, rpnl=rpnl, exit_price=exit_price)
        self._trigger_postmortem(trade)
        self._notify_shadow_reconcile(trade)

    def _fetch_exit_price(self, executor, trade: dict,
                          direction: str) -> tuple:
        """Extract the real exit price from exchange fill/order data.

        Returns (exit_price, total_fees). Returns (None, 0) if the close
        fill hasn't propagated on the exchange yet — caller should retry.
        """
        tid = trade["id"]
        esym = trade.get("exchange_symbol")
        oid = trade.get("order_id")
        close_side = "sell" if direction == "BUY" else "buy"
        exit_price = None
        total_fees = 0.0

        # Primary: use fetch_my_trades to find actual close fills.
        # Filter by filled_at to avoid grabbing fills from other trades on same symbol.
        filled_at_ts = 0
        if trade.get("filled_at"):
            try:
                from datetime import datetime
                fa = trade["filled_at"]
                if isinstance(fa, datetime):
                    filled_at_ts = int(fa.timestamp() * 1000)
                elif isinstance(fa, (int, float)):
                    filled_at_ts = int(fa)
            except Exception:
                pass

        fills = executor.get_recent_fills(esym, limit=20)
        if fills:
            close_fills = [f for f in fills
                           if f.get("side") == close_side
                           and f.get("symbol") == esym
                           and f.get("order_id") != oid
                           and (f.get("timestamp") or 0) >= filled_at_ts]
            if close_fills:
                close_fills.sort(key=lambda f: f.get("timestamp") or 0,
                                 reverse=True)
                latest = close_fills[0]
                exit_price = latest.get("price", 0)
                total_fees = sum(f.get("fee", 0) for f in close_fills)
                logger.info(f"[LiveTradeMonitor] #{tid} exit via fills: "
                            f"price={exit_price}, fees={total_fees:.4f}")
                return (exit_price, total_fees)

        # Fallback: use closed orders — ONLY match opposite-side orders
        recent = executor.get_closed_trades(esym, limit=10)
        if recent:
            opp_orders = [o for o in recent
                          if o.get("side") == close_side
                          and o.get("symbol") == esym
                          and o.get("id") != oid]
            if opp_orders:
                opp_orders.sort(key=lambda o: o.get("timestamp") or 0,
                                reverse=True)
                exit_price = (opp_orders[0].get("average")
                              or opp_orders[0].get("price"))
                logger.info(f"[LiveTradeMonitor] #{tid} exit via closed orders: "
                            f"price={exit_price}")
                return (exit_price, total_fees)

        logger.warning(f"[LiveTradeMonitor] #{tid} close fill not yet available "
                       f"for {esym} — exit_price deferred (will retry)")
        return (None, 0.0)

    def _sync_dossier_on_close(self, trade: dict, rpnl=None, exit_price=None):
        """When a live trade closes, sync to the linked dossier:

        1. Transition dossier status to won/lost (if still live/open_order)
        2. Stamp live trade P&L onto the dossier (live_pnl, live_exit_price…)
        3. Append a LIVE OUTCOME block to the tracker_log so Tracker/Apex
           can learn from real exchange results (slippage, fees, divergence)
        """
        dossier_id = trade.get("dossier_id")
        if not dossier_id:
            return

        try:
            if rpnl is not None:
                cni_outcome = "won" if rpnl > 0 else "lost"
                self.db.execute(
                    "UPDATE compliance_note_influence "
                    "SET trade_outcome = %s WHERE dossier_id = %s",
                    (cni_outcome, dossier_id))
        except Exception as cni_err:
            logger.debug(f"[LiveTradeMonitor] Compliance influence update: "
                         f"{cni_err}")

        d = self.db.fetch_one(
            "SELECT id, status, entry_price, stop_loss, take_profit_1, "
            "realised_pnl, actual_entry_price, actual_exit_price, "
            "margin_usd, leverage, direction, symbol "
            "FROM trade_dossiers WHERE id = %s",
            (dossier_id,))
        if not d:
            return

        tid = trade.get("id")
        acct = trade.get("account_id", "?")
        live_entry = float(trade.get("actual_entry_price")
                           or trade.get("entry_price") or 0)
        live_exit = float(exit_price) if exit_price else None
        live_margin = float(trade.get("margin_usd") or 0)
        live_lev = int(trade.get("leverage") or 1)
        rpnl_pct = float(trade.get("realised_pnl_pct") or 0) if rpnl else None

        paper_entry = float(d.get("actual_entry_price")
                            or d.get("entry_price") or 0)
        paper_exit = float(d.get("actual_exit_price") or 0)
        paper_pnl = float(d["realised_pnl"]) if d.get("realised_pnl") is not None else None
        paper_margin = float(d.get("margin_usd") or 0)
        paper_lev = int(d.get("leverage") or 1)

        # ── 1. Stamp live trade P&L onto the dossier FIRST ──
        # Must happen BEFORE transition so _update_symbol_intel_on_close
        # reads the real exchange P&L, not stale paper P&L.
        try:
            self.db.execute(
                "UPDATE trade_dossiers SET "
                "live_entry_price = %s, live_exit_price = %s, "
                "live_pnl = %s, live_pnl_pct = %s, "
                "live_margin = %s, live_leverage = %s, "
                "live_account_id = %s, live_trade_id = %s, "
                "realised_pnl = %s, realised_pnl_pct = %s, "
                "actual_entry_price = COALESCE(actual_entry_price, %s), "
                "actual_exit_price = %s "
                "WHERE id = %s",
                (live_entry or None, live_exit,
                 rpnl, rpnl_pct,
                 live_margin or None, live_lev,
                 acct, tid,
                 rpnl, rpnl_pct,
                 live_entry or None, live_exit,
                 dossier_id))
        except Exception as e:
            logger.debug(f"[LiveTradeMonitor] Live P&L stamp failed "
                         f"#{dossier_id}: {e}")

        # ── 2. Transition dossier status (triggers symbol_intel update) ──
        outcome = "won" if (rpnl is not None and rpnl >= 0) else "lost"
        if d["status"] in ("live", "open_order"):
            try:
                from services.trading_floor import transition_dossier
                exit_str = f" at {live_exit}" if live_exit else ""
                pnl_str = f" (live P&L ${rpnl:.2f})" if rpnl is not None else ""
                reason = (f"Exchange position closed{exit_str}{pnl_str} "
                          f"— synced from LiveTradeMonitor")
                result = transition_dossier(self.db, dossier_id, outcome, reason)
                if result.get("success"):
                    logger.info(f"[LiveTradeMonitor] Dossier #{dossier_id} -> "
                                f"{outcome} (from LT#{tid})")
                else:
                    logger.warning(f"[LiveTradeMonitor] Dossier #{dossier_id} "
                                   f"transition failed: {result.get('error')}")
            except Exception as e:
                logger.warning(f"[LiveTradeMonitor] Dossier transition error "
                               f"#{dossier_id}: {e}")
        elif d["status"] in ("abandoned", "expired") and rpnl is not None:
            # Dossier was prematurely marked abandoned/expired but the exchange
            # trade actually closed with real P&L — correct the status
            try:
                self.db.execute(
                    "UPDATE trade_dossiers SET status = %s WHERE id = %s",
                    (outcome, dossier_id))
                logger.warning(
                    f"[LiveTradeMonitor] Dossier #{dossier_id} was '{d['status']}' "
                    f"but LT#{tid} closed with rpnl={rpnl:+.2f} — "
                    f"corrected to '{outcome}'")
            except Exception as e:
                logger.warning(f"[LiveTradeMonitor] Dossier status correction "
                               f"#{dossier_id}: {e}")

        # ── 3. Append LIVE OUTCOME to tracker_log ──
        try:
            from services.trading_floor import _append_tracker_log

            entry_slip = ""
            if live_entry and paper_entry and paper_entry > 0:
                slip = ((live_entry - paper_entry) / paper_entry) * 100
                if abs(slip) > 0.01:
                    entry_slip = f" | Entry slippage: {slip:+.3f}%"

            pnl_diverge = ""
            if rpnl is not None and paper_pnl is not None:
                live_dir = "won" if rpnl >= 0 else "lost"
                paper_dir = "won" if paper_pnl >= 0 else "lost"
                if live_dir != paper_dir:
                    pnl_diverge = (
                        f" | **DIVERGENCE**: Paper={paper_dir} "
                        f"(${paper_pnl:+.2f}) vs Live={live_dir} "
                        f"(${rpnl:+.2f})")
                else:
                    gap = rpnl - paper_pnl
                    pnl_diverge = (
                        f" | Paper P&L: ${paper_pnl:+.2f} vs "
                        f"Live P&L: ${rpnl:+.2f} (gap: ${gap:+.2f})")

            outcome_str = "WON" if (rpnl and rpnl > 0) else "LOST"
            # Build log in parts to avoid ternary truncation bug
            safe_rpnl = rpnl if rpnl is not None else 0
            safe_rpnl_pct = rpnl_pct if rpnl_pct is not None else 0
            log_msg = (
                f"── LIVE TRADE OUTCOME ──\n"
                f"  Account: {acct} | LT#{tid}\n"
                f"  Result: {outcome_str}\n"
                f"  Live entry: {live_entry} | Live exit: {live_exit}\n"
                f"  Live margin: ${live_margin:.2f} @ {live_lev}x\n"
                f"  Live P&L: ${safe_rpnl:+.4f} ({safe_rpnl_pct:+.2f}%)\n"
                f"  Paper entry: {paper_entry} | Paper exit: {paper_exit}\n"
                f"  Paper margin: ${paper_margin:.2f} @ {paper_lev}x\n")
            if paper_pnl is not None:
                log_msg += f"  Paper P&L: ${paper_pnl:+.4f}"
            else:
                log_msg += "  Paper P&L: N/A"
            log_msg += f"{entry_slip}{pnl_diverge}"

            _append_tracker_log(self.db, dossier_id, log_msg)
            logger.info(f"[LiveTradeMonitor] Live outcome logged to "
                        f"dossier #{dossier_id} tracker_log")
        except Exception as e:
            logger.debug(f"[LiveTradeMonitor] Tracker log append failed "
                         f"#{dossier_id}: {e}")

        # ── 4. Update strategy stats from live outcome ──
        try:
            strategy_id = d.get("strategy_id")
            if strategy_id and rpnl is not None:
                from services.trading_floor import _update_strategy_stats
                outcome = "won" if rpnl >= 0 else "lost"
                _update_strategy_stats(self.db, dossier_id, outcome, rpnl)
        except Exception as e:
            logger.debug(f"[LiveTradeMonitor] Strategy stats update failed "
                         f"#{dossier_id}: {e}")

    def _sync_dossier_on_cancel(self, trade: dict):
        """When a live trade is cancelled (order never filled), abandon
        the linked dossier so paper doesn't keep running a phantom trade.

        Only abandons dossiers still in open_order or live status.
        Dossiers already won/lost/abandoned/expired are untouched.
        """
        dossier_id = trade.get("dossier_id")
        if not dossier_id:
            return

        tid = trade.get("id", "?")
        had_fill = trade.get("actual_entry_price") is not None

        if had_fill:
            return

        d = self.db.fetch_one(
            "SELECT id, status FROM trade_dossiers WHERE id = %s",
            (dossier_id,))
        if not d:
            return

        if d["status"] not in ("open_order", "live"):
            return

        try:
            from services.trading_floor import transition_dossier, _append_tracker_log
            result = transition_dossier(
                self.db, dossier_id, "abandoned",
                f"Live order never filled — cancelled on exchange (LT#{tid}). "
                f"Paper dossier abandoned to match.")
            if result.get("success"):
                _append_tracker_log(
                    self.db, dossier_id,
                    f"── LIVE ORDER CANCELLED ──\n"
                    f"  LT#{tid} was never filled on exchange.\n"
                    f"  Paper dossier abandoned to prevent phantom P&L.")
                logger.info(f"[LiveTradeMonitor] Dossier #{dossier_id} -> abandoned "
                            f"(unfilled LT#{tid})")
            else:
                logger.debug(f"[LiveTradeMonitor] Dossier #{dossier_id} "
                             f"cancel-sync skipped: {result.get('error')}")
        except Exception as e:
            logger.warning(f"[LiveTradeMonitor] cancel-sync error "
                           f"D#{dossier_id}: {e}")

    def _trigger_postmortem(self, trade: dict):
        """Run post-mortem if trade is linked to a dossier.
        run_full_postmortem returns the audit_report ID (int) or None."""
        if not trade.get("dossier_id"):
            return
        if trade.get("trade_source") == "manual":
            return

        try:
            from services.auditor import run_full_postmortem
            report_id = run_full_postmortem(
                self.db, self.config, trade["dossier_id"])
            if report_id:
                self.db.execute(
                    "UPDATE live_trades SET postmortem_id = %s WHERE id = %s",
                    (report_id, trade["id"]))
                logger.info(f"[LiveTradeMonitor] Post-mortem #{report_id} "
                            f"completed for trade #{trade['id']} "
                            f"(dossier #{trade['dossier_id']})")
        except Exception as e:
            logger.warning(f"[LiveTradeMonitor] Post-mortem failed for "
                           f"#{trade['id']}: {e}")

    def _notify_shadow_reconcile(self, trade: dict):
        """If this is a shadow trade, notify the ShadowQueueManager to
        reconcile actual exchange data back to apex_shadow_trades."""
        if trade.get("trade_source") != "shadow":
            return
        shadow_id = trade.get("shadow_trade_id")
        if not shadow_id:
            return
        try:
            from services.shadow_queue import get_shadow_queue_manager
            sqm = get_shadow_queue_manager()
            if sqm:
                sqm.reconcile_single(shadow_trade_id=shadow_id,
                                     live_trade_id=trade["id"])
        except Exception as e:
            logger.debug(f"[LiveTradeMonitor] Shadow reconcile notify failed: {e}")

    # ─────────────────────────────────────────────────────────────
    # Retry missing exit prices
    # ─────────────────────────────────────────────────────────────

    def _retry_missing_exit_prices(self, account_id: str, account: dict):
        """Re-fetch exit prices for recently closed trades that have NULL exit.

        When a position closes, there's often a brief exchange delay before
        the close fill appears in fetch_my_trades. This method retries those
        trades on subsequent poll cycles until the exit price is captured.
        """
        stale = self.db.fetch_all(
            "SELECT * FROM live_trades WHERE account_id = %s "
            "AND status = 'closed' AND actual_exit_price IS NULL "
            "AND closed_at >= NOW() - INTERVAL 2 HOUR",
            (account_id,))
        if not stale:
            return

        from core.ccxt_executor import get_executor
        executor = get_executor(account_id, self.db)
        if not executor:
            return

        for trade in stale:
            tid = trade["id"]
            direction = (trade.get("direction") or "BUY").upper()
            entry = float(trade.get("actual_entry_price")
                          or trade.get("entry_price") or 0)
            try:
                exit_price, total_fees = self._fetch_exit_price(
                    executor, trade, direction)
            except Exception as e:
                logger.debug(f"[LiveTradeMonitor] Retry exit fetch failed "
                             f"#{tid}: {e}")
                continue

            if not exit_price or exit_price <= 0:
                continue

            rpnl = None
            rpnl_pct = None
            if entry > 0:
                margin = float(trade.get("margin_usd") or 0)
                leverage = int(trade.get("leverage") or 1)
                accrued_funding = float(trade.get("accrued_funding") or 0)
                raw_price_pct = ((exit_price - entry) / entry * 100) if direction == "BUY" \
                    else ((entry - exit_price) / entry * 100)
                if margin > 0:
                    rpnl = round(margin * leverage * (raw_price_pct / 100)
                                 - total_fees - accrued_funding, 4)
                    rpnl_pct = round((rpnl / margin) * 100, 4)
                else:
                    rpnl_pct = round(raw_price_pct * leverage, 4)

            self.db.execute(
                "UPDATE live_trades SET actual_exit_price = %s, "
                "realised_pnl = %s, realised_pnl_pct = %s WHERE id = %s",
                (exit_price, rpnl, rpnl_pct, tid))
            logger.info(f"[LiveTradeMonitor] Retry success #{tid}: "
                        f"exit={exit_price}, P&L=${rpnl} ({rpnl_pct}%)")

            # Only sync P&L data — skip dossier transition / postmortem
            # since _sync_dossier_on_close already ran on the initial close.
            dossier_id = trade.get("dossier_id")
            if dossier_id:
                try:
                    self.db.execute(
                        "UPDATE trade_dossiers SET "
                        "live_exit_price = %s, live_pnl = %s, "
                        "live_pnl_pct = %s, realised_pnl = %s, "
                        "realised_pnl_pct = %s, actual_exit_price = %s "
                        "WHERE id = %s",
                        (exit_price, rpnl, rpnl_pct,
                         rpnl, rpnl_pct, exit_price, dossier_id))
                except Exception as e:
                    logger.debug(f"[LiveTradeMonitor] Retry P&L stamp failed "
                                 f"#{dossier_id}: {e}")

    # ─────────────────────────────────────────────────────────────
    # Unrealised P&L update
    # ─────────────────────────────────────────────────────────────

    def _update_unrealised_pnl(self, account_id: str, positions: list):
        """Update unrealised P&L and accrue funding rates on open live trades."""
        open_trades = self.db.fetch_all(
            "SELECT id, dossier_id, exchange_symbol, direction, entry_price, "
            "actual_entry_price, margin_usd, leverage, accrued_funding "
            "FROM live_trades "
            "WHERE account_id = %s AND status IN ('open', 'partial_closed')",
            (account_id,))
        if not open_trades:
            return

        import re as _re
        pos_map = {p["symbol"]: p for p in positions}

        from core.ccxt_executor import get_executor
        executor = get_executor(account_id, self.db)

        for trade in open_trades:
            exsym = trade.get("exchange_symbol", "")
            pos = pos_map.get(exsym)
            if not pos:
                norm = _re.sub(r'[/:\-_\s.]', '', exsym.upper())
                pos = next((p for p in positions
                            if _re.sub(r'[/:\-_\s.]', '', (p.get("symbol") or "").upper()) == norm),
                           None)
            if not pos:
                continue

            upnl = pos.get("unrealised_pnl", 0)

            # Accrue funding rate cost for perpetual futures.
            # Funding is charged every 8h. We only apply it once per interval
            # by checking if the funding timestamp changed since our last read.
            funding_delta = 0.0
            if executor:
                try:
                    fr = executor.fetch_funding_rate(exsym)
                    if fr and fr.get("rate") and fr.get("timestamp"):
                        last_ts_key = f"_funding_ts_{trade['id']}"
                        last_ts = getattr(self, '_funding_timestamps', {}).get(last_ts_key, 0)
                        if fr["timestamp"] != last_ts:
                            margin = float(trade.get("margin_usd") or 0)
                            leverage = int(trade.get("leverage") or 1)
                            notional = margin * leverage
                            funding_delta = round(notional * abs(float(fr["rate"])), 6)
                            if not hasattr(self, '_funding_timestamps'):
                                self._funding_timestamps = {}
                            self._funding_timestamps[last_ts_key] = fr["timestamp"]
                except Exception:
                    pass

            accrued = float(trade.get("accrued_funding") or 0) + funding_delta
            mark_px = float(pos.get("mark_price") or pos.get("markPrice")
                           or pos.get("lastPrice") or 0)
            margin = float(trade.get("margin_usd") or 0)
            upnl_pct = round(upnl / margin * 100, 2) if margin > 0 else None
            self.db.execute(
                "UPDATE live_trades SET unrealised_pnl = %s, accrued_funding = %s, "
                "current_price = %s, current_price_at = NOW(), "
                "unrealised_pnl_pct = %s "
                "WHERE id = %s",
                (round(upnl, 4), round(accrued, 6),
                 round(mark_px, 8) if mark_px > 0 else None,
                 upnl_pct, trade["id"]))

            # Sync real exchange P&L to the linked dossier so Trading Floor
            # shows the same number as the Live Trades tab (single source of truth).
            dossier_id = trade.get("dossier_id")
            if dossier_id and upnl is not None:
                margin = float(trade.get("margin_usd") or 0)
                upnl_pct = (upnl / margin * 100) if margin else 0
                entry_px = float(trade.get("actual_entry_price")
                                 or trade.get("entry_price") or 0)
                current_px = float(pos.get("mark_price") or pos.get("markPrice")
                                   or pos.get("lastPrice") or entry_px)
                self.db.execute(
                    "UPDATE trade_dossiers SET "
                    "unrealised_pnl = %s, unrealised_pnl_pct = %s, "
                    "current_price = %s, current_price_at = NOW(), "
                    "accrued_funding = %s "
                    "WHERE id = %s AND status = 'live'",
                    (round(upnl, 4), round(upnl_pct, 4),
                     round(current_px, 6), round(accrued, 6), dossier_id))

    # ─────────────────────────────────────────────────────────────
    # Dust position cleanup
    # ─────────────────────────────────────────────────────────────

    DUST_MARGIN_THRESHOLD = 1.0

    def _close_dust_positions(self, account_id: str, positions: list,
                              account: dict):
        """Auto-close tiny residual positions left by partial close rounding.
        A position with margin < DUST_MARGIN_THRESHOLD that has no matching
        open live_trade is dust and should be closed."""
        if not positions:
            return

        known = self.db.fetch_all(
            "SELECT exchange_symbol FROM live_trades "
            "WHERE account_id = %s AND status IN ('open', 'partial_closed')",
            (account_id,))
        tracked_symbols = {r["exchange_symbol"] for r in known} if known else set()

        from core.ccxt_executor import get_executor
        executor = get_executor(account_id, self.db)
        if not executor:
            return

        for pos in positions:
            sym = pos.get("symbol", "")
            contracts = abs(pos.get("contracts", 0) or 0)
            margin = abs(pos.get("margin", 0) or 0)
            notional = abs(pos.get("notional", 0) or 0)
            if contracts == 0 and notional == 0:
                continue
            if margin >= self.DUST_MARGIN_THRESHOLD:
                continue
            if sym in tracked_symbols:
                continue

            side = (pos.get("side") or "long").upper()
            direction = "BUY" if side in ("LONG", "BUY") else "SELL"
            logger.info(
                f"[LiveTradeMonitor] Closing dust position on {account_id}: "
                f"{sym} {direction} margin=${margin:.4f} contracts={contracts}")
            try:
                executor.close_position(sym, direction)
                logger.info(f"[LiveTradeMonitor] Dust position closed: {sym} on {account_id}")
            except Exception as e:
                logger.debug(f"[LiveTradeMonitor] Dust close failed {sym}: {e}")

    # ─────────────────────────────────────────────────────────────
    # Manual trade sync
    # ─────────────────────────────────────────────────────────────

    MIN_SYNC_MARGIN_USD = 1.0

    def _sync_manual_trades(self, account_id: str, positions: list,
                            exchange: str = "unknown"):
        """Detect positions on exchange not tracked in live_trades and add them.

        Before creating a 'manual' trade, checks if a recently closed/cancelled
        trade exists for the same symbol+account — if so, re-links (reopens) it
        instead of creating a duplicate.  This prevents orphaned manual trades
        when an exchange connectivity blip causes a false close detection.

        Ignores dust positions (margin < MIN_SYNC_MARGIN_USD) and logs all
        detections for audit trail."""
        known_symbols = set()
        known = self.db.fetch_all(
            "SELECT exchange_symbol FROM live_trades "
            "WHERE account_id = %s AND status IN ('pending','open','partial_closed')",
            (account_id,))
        if known:
            known_symbols = {r["exchange_symbol"] for r in known}

        for pos in positions:
            sym = pos.get("symbol", "")
            if not sym or sym in known_symbols:
                continue
            contracts = abs(pos.get("contracts", 0) or 0)
            notional = abs(pos.get("notional", 0) or 0)
            margin = abs(pos.get("margin", 0) or 0)
            if contracts == 0 and notional == 0:
                continue

            if margin < self.MIN_SYNC_MARGIN_USD:
                logger.debug(
                    f"[LiveTradeMonitor] Ignoring dust position on {account_id}: "
                    f"{sym} margin=${margin:.4f} < ${self.MIN_SYNC_MARGIN_USD} "
                    f"(contracts={contracts}, notional={notional})")
                continue

            side = (pos.get("side") or "long").upper()
            direction = "BUY" if side in ("LONG", "BUY") else "SELL"
            base = sym.split("/")[0] if "/" in sym else sym

            # Check for a recently closed/cancelled trade for the same symbol,
            # same direction, within the last 30 min.  If found, re-link it
            # instead of creating a duplicate "manual" entry.  The time window
            # prevents stale trades from months ago being resurrected with
            # outdated TP/SL levels.  The direction check prevents a closed
            # LONG from being reopened for a new SHORT (or vice versa).
            orphan = self.db.fetch_one(
                "SELECT id, dossier_id, trade_source, mentor_source, stop_loss, "
                "take_profit_1, take_profit_2, take_profit_3 "
                "FROM live_trades "
                "WHERE account_id = %s AND exchange_symbol = %s "
                "AND direction = %s "
                "AND status IN ('closed','cancelled') "
                "AND updated_at >= NOW() - INTERVAL 30 MINUTE "
                "ORDER BY updated_at DESC LIMIT 1",
                (account_id, sym, direction))

            if orphan:
                logger.warning(
                    f"[LiveTradeMonitor] Re-linking orphaned position on {account_id}: "
                    f"{sym} {direction} — found recently closed LT#{orphan['id']} "
                    f"(src={orphan.get('trade_source')}, D#{orphan.get('dossier_id')}). "
                    f"Reopening instead of creating manual duplicate.")
                self.db.execute(
                    "UPDATE live_trades SET status = 'open', closed_at = NULL, "
                    "realised_pnl = NULL, realised_pnl_pct = NULL, "
                    "actual_exit_price = NULL, "
                    "close_comment = 'Reopened: exchange position still active after false close' "
                    "WHERE id = %s",
                    (orphan["id"],))
                # Restore linked dossier to live if it was moved to a terminal state
                did = orphan.get("dossier_id")
                if did:
                    prev = self.db.fetch_one(
                        "SELECT status FROM trade_dossiers WHERE id = %s", (did,))
                    if prev and prev["status"] in ("lost", "abandoned"):
                        self.db.execute(
                            "UPDATE trade_dossiers SET status = 'live' "
                            "WHERE id = %s AND status IN ('lost','abandoned')",
                            (did,))
                        try:
                            from services.trading_floor import (
                                _snapshot_conditions_at_entry)
                            _snapshot_conditions_at_entry(self.db, did)
                        except Exception as e:
                            logger.debug(
                                f"[LiveTradeMonitor] Snapshot error #{did}: {e}")
                continue

            # Risk check: warn if synced margin exceeds safe threshold
            max_safe_margin = 50.0
            margin_flag = ""
            if margin > max_safe_margin:
                margin_flag = (f" ** RISK WARNING: margin ${margin:.2f} exceeds "
                               f"${max_safe_margin:.2f} safety cap — "
                               f"position was opened outside JarvAIs **")

            logger.warning(
                f"[LiveTradeMonitor] Untracked position detected on {account_id}: "
                f"{sym} {direction} | margin=${margin:.2f} | contracts={contracts} | "
                f"notional={notional:.2f} | entry={pos.get('entry_price', 0)} | "
                f"leverage={pos.get('leverage', 1)}x — syncing as 'manual'"
                f"{margin_flag}")

            stop_loss = pos.get("stop_loss") or pos.get("stopLoss") or None
            take_profit = pos.get("take_profit") or pos.get("takeProfit") or None

            self.db.execute("""
                INSERT INTO live_trades
                (account_id, exchange, symbol, exchange_symbol, direction,
                 entry_price, position_size, margin_usd, leverage,
                 stop_loss, take_profit_1,
                 status, actual_entry_price, filled_at, trade_source,
                 notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        'open', %s, NOW(), 'manual', %s)
            """, (
                account_id, exchange,
                base, sym, direction,
                pos.get("entry_price", 0),
                contracts,
                margin,
                pos.get("leverage", 1),
                stop_loss, take_profit,
                pos.get("entry_price", 0),
                margin_flag.strip() if margin_flag else None,
            ))

    # ─────────────────────────────────────────────────────────────
    # Daily P&L aggregation
    # ─────────────────────────────────────────────────────────────

    def _update_daily_pnl(self, account_id: str):
        """Aggregate today's realised + unrealised P&L for the account."""
        today = date.today().isoformat()

        row = self.db.fetch_one("""
            SELECT
                COALESCE(SUM(CASE WHEN status = 'closed' AND DATE(closed_at) = %s
                         THEN realised_pnl ELSE 0 END), 0) as realised,
                COALESCE(SUM(CASE WHEN status IN ('open','partial_closed')
                         THEN unrealised_pnl ELSE 0 END), 0) as unrealised,
                COUNT(CASE WHEN DATE(created_at) = %s THEN 1 END) as trade_count,
                COALESCE(SUM(CASE WHEN trade_source = 'apex' AND status = 'closed'
                         AND DATE(closed_at) = %s THEN realised_pnl ELSE 0 END), 0) as apex_pnl,
                COALESCE(SUM(CASE WHEN trade_source = 'mentor' AND status = 'closed'
                         AND DATE(closed_at) = %s THEN realised_pnl ELSE 0 END), 0) as mentor_pnl
            FROM live_trades WHERE account_id = %s
        """, (today, today, today, today, account_id))

        with self._cache_lock:
            balance = self._cached_balances.get(account_id, {}).get("total", 0)

        self.db.execute("""
            INSERT INTO live_trade_pnl_daily
            (account_id, trade_date, realised_pnl, unrealised_pnl, balance,
             trade_count, apex_pnl, mentor_pnl)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                realised_pnl = VALUES(realised_pnl),
                unrealised_pnl = VALUES(unrealised_pnl),
                balance = VALUES(balance),
                trade_count = VALUES(trade_count),
                apex_pnl = VALUES(apex_pnl),
                mentor_pnl = VALUES(mentor_pnl)
        """, (
            account_id, today,
            round(float(row["realised"] if row else 0), 4),
            round(float(row["unrealised"] if row else 0), 4),
            round(balance, 2),
            int(row["trade_count"] if row else 0),
            round(float(row["apex_pnl"] if row else 0), 4),
            round(float(row["mentor_pnl"] if row else 0), 4),
        ))

    # ─────────────────────────────────────────────────────────────
    # Public API helpers (called by web_dashboard endpoints)
    # ─────────────────────────────────────────────────────────────

    def get_all_positions(self, account_id: str = None,
                          include_shadow: bool = False) -> List[Dict]:
        """Get all live trades enriched with current price, distance, and account name."""
        sql = """
            SELECT lt.*, ta.name as account_name, ta.exchange as account_exchange
            FROM live_trades lt
            LEFT JOIN trading_accounts ta ON lt.account_id = ta.account_id
            WHERE lt.status IN ('pending','open','partial_closed')
        """
        if not include_shadow:
            sql += " AND (lt.trade_source != 'shadow' OR lt.trade_source IS NULL)"
        params = ()
        if account_id:
            sql += " AND lt.account_id = %s"
            params = (account_id,)
        sql += " ORDER BY lt.created_at DESC"
        rows = self.db.fetch_all(sql, params) or []

        result = []
        for r in rows:
            d = dict(r)
            aid = d.get("account_id", "")
            exsym = d.get("exchange_symbol", "")
            entry = float(d.get("actual_entry_price") or d.get("entry_price") or 0)
            direction = (d.get("direction") or "BUY").upper()

            # Enrich with current mark price from cached exchange positions
            mark = 0
            with self._cache_lock:
                cached = list(self._cached_positions.get(aid, []))
            pos = next((p for p in cached if p.get("symbol") == exsym), None)
            if pos:
                mark = pos.get("mark_price", 0)
            # Fallback to PriceStreamer for pending orders (no exchange position yet)
            if mark <= 0:
                try:
                    from services.price_streamer import get_price_streamer
                    streamer = get_price_streamer()
                    if streamer:
                        sym = d.get("symbol", "")
                        pdata = streamer.get_price(sym)
                        if pdata and pdata.get("price", 0) > 0:
                            mark = pdata["price"]
                except Exception:
                    pass
            d["current_price"] = mark if mark > 0 else None

            # Distance calculation
            if mark > 0 and entry > 0:
                if d["status"] == "pending":
                    d["distance_pct"] = round(abs(mark - entry) / entry * 100, 2)
                    d["distance"] = f"{d['distance_pct']}%"
                else:
                    if direction == "BUY":
                        d["distance_pct"] = round((mark - entry) / entry * 100, 2)
                    else:
                        d["distance_pct"] = round((entry - mark) / entry * 100, 2)
                    sign = "+" if d["distance_pct"] >= 0 else ""
                    d["distance"] = f"{sign}{d['distance_pct']}%"
            else:
                d["distance"] = None
                d["distance_pct"] = None

            # P&L percentage from unrealised_pnl and margin
            margin = float(d.get("margin_usd") or 0)
            upnl = float(d.get("unrealised_pnl") or 0)
            if margin > 0:
                d["unrealised_pnl_pct"] = round(upnl / margin * 100, 2)
            else:
                d["unrealised_pnl_pct"] = None

            # Trade source label (dynamic: resolve duo display name)
            src = d.get("trade_source", "")
            mentor = d.get("mentor_source", "")
            if src == "mentor" and mentor:
                d["source_label"] = f"Mentor ({mentor})"
            elif src and src != "mentor":
                d["source_label"] = (src[0].upper() + src[1:]) if src else "Duo"
            else:
                d["source_label"] = "Manual"

            result.append(d)
        return result

    def get_trade_history(self, account_id: str = None,
                          days: int = 30,
                          include_shadow: bool = False) -> List[Dict]:
        """Get closed/cancelled trades with account name + dossier paper P&L."""
        sql = """
            SELECT lt.*, ta.name as account_name,
                   td.realised_pnl as paper_pnl,
                   td.realised_pnl_pct as paper_pnl_pct,
                   td.status as dossier_status
            FROM live_trades lt
            LEFT JOIN trading_accounts ta ON lt.account_id = ta.account_id
            LEFT JOIN trade_dossiers td ON td.id = lt.dossier_id
            WHERE lt.status IN ('closed','cancelled','abandoned')
            AND lt.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """
        if not include_shadow:
            sql += " AND (lt.trade_source != 'shadow' OR lt.trade_source IS NULL)"
        params = [days]
        if account_id:
            sql += " AND lt.account_id = %s"
            params.append(account_id)
        sql += " ORDER BY lt.closed_at DESC"
        rows = self.db.fetch_all(sql, tuple(params)) or []
        result = []
        for r in rows:
            d = dict(r)
            src = d.get("trade_source", "")
            mentor = d.get("mentor_source", "")
            if src == "mentor" and mentor:
                d["source_label"] = f"Mentor ({mentor})"
            elif src and src != "mentor":
                d["source_label"] = (src[0].upper() + src[1:]) if src else "Duo"
            else:
                d["source_label"] = "Manual"

            live_pnl = float(d.get("realised_pnl") or 0)
            paper_pnl = float(d.get("paper_pnl") or 0) if d.get("paper_pnl") is not None else None
            if paper_pnl is not None:
                d["pnl_gap"] = round(live_pnl - paper_pnl, 4)
                live_dir = "won" if live_pnl > 0 else "lost"
                paper_dir = "won" if paper_pnl > 0 else "lost"
                d["pnl_diverged"] = live_dir != paper_dir
            result.append(d)
        return result

    def get_account_summary(self, account_id: str = None,
                            refresh_balances: bool = False) -> Dict:
        """Rolled-up summary for one or all accounts."""
        if account_id:
            accounts = self.db.fetch_all(
                "SELECT * FROM trading_accounts WHERE account_id = %s",
                (account_id,)) or []
        else:
            accounts = self.db.fetch_all(
                "SELECT * FROM trading_accounts WHERE enabled = 1") or []

        if refresh_balances:
            from core.ccxt_executor import get_executor
            for a in accounts:
                aid = a["account_id"]
                if not a.get("live_trading"):
                    continue
                try:
                    ex = get_executor(aid, self.db)
                    if ex and ex.connected:
                        bal = ex.get_balance()
                        a["cached_balance"] = bal.get("total", 0)
                        with self._cache_lock:
                            self._cached_balances[aid] = bal
                        self.db.execute(
                            "UPDATE trading_accounts SET cached_balance = %s, "
                            "cached_balance_at = NOW() WHERE account_id = %s",
                            (bal.get("total", 0), aid))
                except Exception as e:
                    logger.debug(f"[LiveTradeMonitor] Balance refresh for {aid}: {e}")

        total_balance = 0
        total_pnl = 0
        total_trades = 0
        total_margin = 0
        acct_summaries = []

        for a in accounts:
            aid = a["account_id"]
            bal = float(a.get("cached_balance") or 0)
            total_balance += bal

            stats = self.db.fetch_one(
                "SELECT COUNT(*) as cnt, "
                "COALESCE(SUM(unrealised_pnl),0) as upnl, "
                "COALESCE(SUM(CASE WHEN status='closed' THEN realised_pnl ELSE 0 END),0) as rpnl, "
                "COALESCE(SUM(CASE WHEN status IN ('pending','open','partial_closed') "
                "  THEN margin_usd ELSE 0 END),0) as used_margin "
                "FROM live_trades WHERE account_id = %s "
                "AND status IN ('pending','open','partial_closed','closed') "
                "AND (trade_source != 'shadow' OR trade_source IS NULL)",
                (aid,))

            cnt = int(stats["cnt"]) if stats else 0
            upnl = float(stats["upnl"]) if stats else 0
            rpnl = float(stats["rpnl"]) if stats else 0
            margin = float(stats["used_margin"]) if stats else 0
            total_pnl += upnl + rpnl
            total_trades += cnt
            total_margin += margin
            margin_cap = float(a.get("margin_cap_pct") or 50)

            acct_summaries.append({
                "account_id": aid,
                "name": a.get("name", aid),
                "exchange": a.get("exchange", ""),
                "account_type": a.get("account_type", ""),
                "balance": bal,
                "unrealised_pnl": round(upnl, 4),
                "realised_pnl": round(rpnl, 4),
                "used_margin": round(margin, 2),
                "margin_cap_pct": margin_cap,
                "trade_count": cnt,
                "live_trading": bool(a.get("live_trading")),
                "risk_per_trade_pct": float(a.get("risk_per_trade_pct") or 1),
                "leverage_mode": a.get("leverage_mode", "max_before_sl"),
                "apex_enabled": bool(a.get("apex_enabled")),
                "mentor_enabled": bool(a.get("mentor_enabled")),
            })

        return {
            "total_balance": round(total_balance, 2),
            "total_pnl": round(total_pnl, 4),
            "total_trades": total_trades,
            "total_margin": round(total_margin, 2),
            "accounts": acct_summaries,
        }

    def get_pnl_curve(self, account_id: str = None,
                       days: int = 30) -> List[Dict]:
        """Get daily P&L curve data."""
        if account_id:
            rows = self.db.fetch_all(
                "SELECT * FROM live_trade_pnl_daily WHERE account_id = %s "
                "AND trade_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) "
                "ORDER BY trade_date ASC",
                (account_id, days))
        else:
            rows = self.db.fetch_all(
                "SELECT trade_date, "
                "SUM(realised_pnl) as realised_pnl, "
                "SUM(unrealised_pnl) as unrealised_pnl, "
                "SUM(balance) as balance, "
                "SUM(trade_count) as trade_count, "
                "SUM(apex_pnl) as apex_pnl, "
                "SUM(mentor_pnl) as mentor_pnl "
                "FROM live_trade_pnl_daily "
                "WHERE trade_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY) "
                "GROUP BY trade_date ORDER BY trade_date ASC",
                (days,))
        result = []
        for r in (rows or []):
            d = r.get("trade_date")
            result.append({
                "date": d.isoformat() if hasattr(d, "isoformat") else str(d),
                "realised_pnl": float(r.get("realised_pnl") or 0),
                "unrealised_pnl": float(r.get("unrealised_pnl") or 0),
                "balance": float(r.get("balance") or 0),
                "trade_count": int(r.get("trade_count") or 0),
                "apex_pnl": float(r.get("apex_pnl") or 0),
                "mentor_pnl": float(r.get("mentor_pnl") or 0),
            })
        return result

    def get_mentor_breakdown(self, account_id: str = None,
                             days: int = 30) -> Dict:
        """Per-mentor and Apex P&L breakdown with win/loss stats + daily series."""
        where = "WHERE lt.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)"
        params = [days]
        if account_id:
            where += " AND lt.account_id = %s"
            params.append(account_id)

        rows = self.db.fetch_all(f"""
            SELECT
                CASE
                    WHEN lt.trade_source = 'mentor' AND lt.mentor_source IS NOT NULL
                        THEN lt.mentor_source
                    WHEN lt.trade_source = 'apex' THEN 'Apex'
                    WHEN lt.trade_source = 'shadow' THEN 'Shadow Trader'
                    ELSE 'Manual'
                END as source_name,
                lt.trade_source,
                COUNT(*) as total_trades,
                SUM(CASE WHEN lt.status = 'closed' AND lt.realised_pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN lt.status = 'closed' AND lt.realised_pnl <= 0 THEN 1 ELSE 0 END) as losses,
                SUM(CASE WHEN lt.status IN ('open','partial_closed') THEN 1 ELSE 0 END) as active,
                COALESCE(SUM(CASE WHEN lt.status = 'closed' THEN lt.realised_pnl ELSE 0 END), 0) as realised_pnl,
                COALESCE(SUM(CASE WHEN lt.status IN ('open','partial_closed')
                    THEN lt.unrealised_pnl ELSE 0 END), 0) as unrealised_pnl
            FROM live_trades lt
            {where}
            GROUP BY source_name, lt.trade_source
            ORDER BY realised_pnl DESC
        """, tuple(params))

        sources = []
        for r in (rows or []):
            wins = int(r.get("wins") or 0)
            losses = int(r.get("losses") or 0)
            total_closed = wins + losses
            sources.append({
                "name": r["source_name"],
                "type": r["trade_source"],
                "total_trades": int(r["total_trades"]),
                "active": int(r.get("active") or 0),
                "wins": wins,
                "losses": losses,
                "win_pct": round(wins / total_closed * 100, 1) if total_closed > 0 else 0,
                "realised_pnl": round(float(r["realised_pnl"]), 2),
                "unrealised_pnl": round(float(r["unrealised_pnl"]), 2),
                "total_pnl": round(float(r["realised_pnl"]) + float(r["unrealised_pnl"]), 2),
            })

        # Daily series per source for chart
        daily_rows = self.db.fetch_all(f"""
            SELECT DATE(lt.closed_at) as trade_date,
                CASE WHEN lt.trade_source = 'mentor' AND lt.mentor_source IS NOT NULL
                    THEN lt.mentor_source WHEN lt.trade_source = 'apex' THEN 'Apex'
                    ELSE 'Manual' END as source_name,
                COALESCE(SUM(lt.realised_pnl), 0) as daily_pnl
            FROM live_trades lt
            {where} AND lt.status = 'closed' AND lt.closed_at IS NOT NULL
            GROUP BY trade_date, source_name
            ORDER BY trade_date ASC
        """, tuple(params))

        daily_series = {}
        for r in (daily_rows or []):
            name = r["source_name"]
            d = r["trade_date"]
            ds = d.isoformat() if hasattr(d, "isoformat") else str(d)
            if name not in daily_series:
                daily_series[name] = []
            daily_series[name].append({"date": ds, "pnl": round(float(r["daily_pnl"]), 2)})

        return {"sources": sources, "daily_series": daily_series}

    def close_trade(self, trade_id: int, comment: str = "") -> Dict:
        """Close a live trade from the dashboard (cancel order or close position)."""
        from core.ccxt_executor import get_executor

        trade = self.db.fetch_one("SELECT * FROM live_trades WHERE id = %s", (trade_id,))
        if not trade:
            return {"success": False, "error": "Trade not found"}

        executor = get_executor(trade["account_id"], self.db)
        if not executor:
            return {"success": False, "error": "Cannot connect to exchange"}

        status = trade["status"]
        exsym = trade["exchange_symbol"]

        if status == "pending":
            if trade.get("order_id"):
                result = executor.cancel_order(trade["order_id"], exsym)
                if not result.get("success"):
                    logger.warning(f"[LiveTradeMonitor] Cancel order failed: {result}")
                    return {"success": False,
                            "error": f"Exchange cancel failed: {result.get('error', 'unknown')}"}

            self.db.execute(
                "UPDATE live_trades SET status = 'cancelled', closed_at = NOW(), "
                "realised_pnl = NULL, unrealised_pnl = NULL, "
                "realised_pnl_pct = NULL, unrealised_pnl_pct = NULL, "
                "close_comment = %s WHERE id = %s",
                (comment, trade_id))
            self._sync_dossier_on_cancel(trade)

        elif status in ("open", "partial_closed"):
            result = executor.close_position(exsym, trade["direction"])
            if not result.get("success"):
                logger.warning(f"[LiveTradeMonitor] Close position failed: {result}")
                return {"success": False,
                        "error": f"Exchange close failed: {result.get('error', 'unknown')}"}

            # Fetch actual exit price and calculate P&L before triggering postmortem
            exit_price = None
            total_fees = 0.0
            direction = (trade.get("direction") or "BUY").upper()
            try:
                time.sleep(1)  # Allow exchange to process close
                exit_price, total_fees = self._fetch_exit_price(
                    executor, trade, direction)
            except Exception as ep_exc:
                logger.debug(f"[LiveTradeMonitor] Manual close fill fetch failed: {ep_exc}")

            entry = float(trade.get("actual_entry_price") or trade.get("entry_price") or 0)
            leverage = int(trade.get("leverage") or 1)
            margin = float(trade.get("margin_usd") or 0)
            accrued_funding = float(trade.get("accrued_funding") or 0)
            rpnl = None
            rpnl_pct = None
            if entry > 0 and exit_price and exit_price > 0 and margin > 0:
                raw_pct = ((exit_price - entry) / entry * 100) if direction == "BUY" \
                    else ((entry - exit_price) / entry * 100)
                rpnl = round(margin * leverage * (raw_pct / 100)
                             - total_fees - accrued_funding, 4)
                rpnl_pct = round((rpnl / margin) * 100, 4)

            self.db.execute(
                "UPDATE live_trades SET status = 'closed', closed_at = NOW(), "
                "close_comment = %s, actual_exit_price = %s, "
                "realised_pnl = %s, realised_pnl_pct = %s, "
                "total_fees = %s WHERE id = %s",
                (comment, exit_price, rpnl, rpnl_pct,
                 round(total_fees + accrued_funding, 6), trade_id))

            self._sync_dossier_on_close(trade, rpnl=rpnl, exit_price=exit_price)
            self._trigger_postmortem(trade)
        else:
            return {"success": False, "error": f"Trade already in status '{status}'"}

        logger.info(f"[LiveTradeMonitor] Trade #{trade_id} closed by user: {comment[:100]}")
        return {"success": True, "trade_id": trade_id, "new_status": "closed" if status != "pending" else "cancelled"}

    def cancel_order(self, trade_id: int, comment: str = "") -> Dict:
        """Cancel a pending order from the dashboard."""
        return self.close_trade(trade_id, comment)
