"""
shadow_queue.py — Shadow Trader Agent (Mechanical Executor)

Watches apex_shadow_trades for profitable rejected trades, filters by
confidence/model/tradability, ranks by distance-to-entry, and places
limit orders on a dedicated exchange account. Zero LLM cost.

Singleton: get_shadow_queue_manager() returns the global instance.
Lifecycle:  start() ← dashboard toggle ON  |  stop() ← toggle OFF
"""
import collections
import json
import logging
import math
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger("jarvais.shadow_queue")


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


_sq_epoch_cache: dict = {"value": None, "ts": 0}

def _get_stats_epoch(db) -> str:
    """Return stats_epoch_date from system_config, cached 60s."""
    now = time.time()
    if _sq_epoch_cache["value"] and now - _sq_epoch_cache["ts"] < 60:
        return _sq_epoch_cache["value"]
    try:
        row = db.fetch_one(
            "SELECT config_value FROM system_config WHERE config_key = 'stats_epoch_date'")
        val = (row.get("config_value") if row else None) or "2000-01-01"
    except Exception:
        val = "2000-01-01"
    _sq_epoch_cache["value"] = val
    _sq_epoch_cache["ts"] = now
    return val


def _safe_float(val, default: float = 0.0) -> float:
    try:
        v = float(val)
        return default if math.isnan(v) or math.isinf(v) else v
    except (ValueError, TypeError):
        return default


_sqm_instance: Optional["ShadowQueueManager"] = None
_sqm_lock = threading.Lock()


def get_shadow_queue_manager() -> Optional["ShadowQueueManager"]:
    return _sqm_instance


def start_shadow_queue(db, config) -> Optional["ShadowQueueManager"]:
    """Create + start the global ShadowQueueManager if shadow trading is enabled."""
    global _sqm_instance
    with _sqm_lock:
        if _sqm_instance and _sqm_instance._running:
            return _sqm_instance
        _sqm_instance = ShadowQueueManager(db, config)
        cfg_enabled = _sqm_instance._cfg.get("enabled", "false")
        if cfg_enabled.strip().lower() == "true":
            _sqm_instance.start()
        else:
            logger.info("[ShadowQ] Initialised but NOT started (enabled=false)")
        return _sqm_instance


class ShadowQueueManager:
    """Mechanical executor for profitable rejected shadow trades.
    Zero LLM cost — filters, ranks, and places orders based on data alone."""

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._cooloff_active = False
        self._cooloff_end: Optional[datetime] = None
        self._started_at: Optional[datetime] = None
        self._cycle_count = 0
        self._activity_buffer: collections.deque = collections.deque(maxlen=500)
        self._market_info: Dict[str, Dict] = {}
        self._cfg: Dict[str, str] = {}
        self._cfg_last_load = 0.0
        self._executor = None
        self._exchange_name = "bybit"
        self._executors: Dict[str, Any] = {}
        self._exchange_names: Dict[str, str] = {}
        self._waterfall_accounts: List[Dict] = []
        self._market_info_by_account: Dict[str, Dict] = {}
        self._cached_balances: Dict[str, Dict[str, float]] = {}
        self._cached_balances_at: Dict[str, float] = {}
        self._reconcile_lock = threading.Lock()
        self._cached_balance: Dict[str, float] = {}
        self._cached_balance_at: float = 0.0
        self._velocity_alert: bool = False
        self._velocity_direction: Optional[str] = None
        self._last_pulse_at: float = 0.0
        self._current_regime_score: Optional[float] = None

        self._reload_config()

    # ═══════════════════════════════════════════════════════════════════
    # LIFECYCLE
    # ═══════════════════════════════════════════════════════════════════

    def start(self):
        if self._running:
            return
        self._running = True
        self._started_at = datetime.utcnow()

        cooloff_min = _safe_int(self._cfg.get("cooloff_minutes", "5"), 5)
        self._cooloff_active = True
        self._cooloff_end = self._started_at + timedelta(minutes=cooloff_min)

        self._log_activity("START", None, "INFO",
                           f"Shadow Trader started. Cool-off for {cooloff_min} min.")

        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="shadow-queue")
        self._thread.start()
        logger.info(f"[ShadowQ] Started (cooloff={cooloff_min}m)")

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._log_activity("STOP", None, "INFO", "Shadow Trader stopped by user.")
        self._cancel_all_pending_orders()
        logger.info("[ShadowQ] Stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    # ═══════════════════════════════════════════════════════════════════
    # MAIN LOOP
    # ═══════════════════════════════════════════════════════════════════

    def _run_loop(self):
        refresh_sec = _safe_int(self._cfg.get("queue_refresh_seconds", "10"), 10)
        while self._running:
            try:
                self._run_cycle()
            except Exception as e:
                logger.error(f"[ShadowQ] Cycle error: {e}", exc_info=True)
                self._log_activity("ERROR", None, "ERROR",
                                   f"Cycle error: {e}")
            time.sleep(refresh_sec)

    def _run_cycle(self):
        self._cycle_count += 1
        self._reload_config()

        enabled_val = self._cfg.get("enabled", "false").strip().lower()
        if enabled_val != "true":
            logger.warning(f"[ShadowQ] Disabled (enabled={enabled_val!r}), pausing cycle")
            return

        if self._cooloff_active:
            if datetime.utcnow() >= self._cooloff_end:
                self._cooloff_active = False
                self._log_activity("COOLOFF", None, "INFO",
                                   "Cool-off period ended. Orders unlocked.")
                logger.info("[ShadowQ] Cool-off ended — orders unlocked")

        self._ensure_executor()
        if not self._executor and not self._executors:
            logger.warning("[ShadowQ] No executor available, skipping cycle")
            return

        # ── Fast Pulse velocity check (every ~60s) ────────────────────
        velocity_enabled = self._cfg.get("tce_velocity_enabled", "true").strip().lower() == "true"
        pulse_interval = _safe_float(self._cfg.get("tce_velocity_check_interval_sec", "60"), 60.0)
        now_ts = time.time()
        if velocity_enabled and (now_ts - self._last_pulse_at) >= pulse_interval:
            self._last_pulse_at = now_ts
            try:
                from services.market_regime import MarketRegime
                threshold = _safe_float(self._cfg.get("tce_velocity_threshold", "2.0"), 2.0)
                active_positions = self._get_active_positions_for_pulse()
                pulse = MarketRegime().fast_pulse(
                    "BTCUSDT", self.db, executor=self._executor,
                    threshold_pct=threshold, active_positions=active_positions)
                if pulse.get("velocity_alert"):
                    self._velocity_alert = True
                    self._velocity_direction = pulse.get("alert_direction", "drop")
                    self._log_activity("VELOCITY_ALERT", None, "WARNING",
                                       f"BTC {self._velocity_direction}: "
                                       f"5m={pulse.get('velocity_5m', 0):.2f}%, "
                                       f"15m={pulse.get('velocity_15m', 0):.2f}%")
                elif self._velocity_alert:
                    self._velocity_alert = False
                    self._velocity_direction = None
                    logger.info("[ShadowQ] Velocity alert cleared")
                    self._log_activity("VELOCITY_CLEAR", None, "INFO",
                                       "Velocity alert cleared — normal conditions restored")
            except Exception as e:
                logger.debug(f"[ShadowQ] Fast pulse failed (non-fatal): {e}")

        # Load latest full regime score for protection gates
        try:
            regime_row = self.db.fetch_one(
                "SELECT regime_score FROM market_regime_history "
                "WHERE source='full' ORDER BY created_at DESC LIMIT 1")
            if regime_row and regime_row.get("regime_score") is not None:
                self._current_regime_score = float(regime_row["regime_score"])
        except Exception:
            pass

        primary_aid_ref = self._cfg.get("account_id", "ShadowDemo")
        if self._executor:
            try:
                bal = self._executor.get_balance()
                self._cached_balance = bal
                self._cached_balance_at = time.time()
                self._cached_balances[primary_aid_ref] = bal
                self._cached_balances_at[primary_aid_ref] = time.time()
            except Exception as e:
                logger.debug(f"[ShadowQ] Balance cache refresh failed: {e}")

        for wf_aid, wf_ex in self._executors.items():
            if wf_aid == primary_aid_ref:
                continue
            try:
                wf_bal = wf_ex.get_balance()
                self._cached_balances[wf_aid] = wf_bal
                self._cached_balances_at[wf_aid] = time.time()
            except Exception as e:
                logger.debug(f"[ShadowQ] Balance cache for {wf_aid}: {e}")

        self._poll_new_shadows()
        self._update_distances()
        self._rank_queue()

        self._detect_fills()
        self._expire_tp1_overshot()

        if not self._cooloff_active:
            self._place_next_orders()
            self._rotate_exchange_orders()
            self._expire_stale_orders()

        self._reconcile_closed_trades()

    def _get_active_positions_for_pulse(self) -> list:
        """Fetch active shadow positions (placed/filled) for portfolio-aware velocity check."""
        try:
            rows = self.db.fetch_all(
                "SELECT direction, entry_price, current_price "
                "FROM shadow_queue WHERE queue_status IN ('placed', 'filled') "
                "AND current_price IS NOT NULL AND entry_price > 0")
            return rows or []
        except Exception:
            return []

    # ═══════════════════════════════════════════════════════════════════
    # EXECUTOR & MARKET DATA
    # ═══════════════════════════════════════════════════════════════════

    def _is_waterfall_enabled(self) -> bool:
        return self._cfg.get("shadow_waterfall_enabled", "false").strip().lower() == "true"

    @staticmethod
    def _parse_duo_allowed(raw) -> List[str]:
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                return []
        return []

    def _load_waterfall_accounts(self):
        """Load eligible waterfall accounts from trading_accounts, sorted by priority.
        Only includes accounts where the 'shadow' duo is explicitly in duo_allowed.
        Empty/NULL duo_allowed means NO duos are allowed."""
        try:
            rows = self.db.fetch_all(
                "SELECT * FROM trading_accounts "
                "WHERE enabled = 1 AND live_trading = 1 AND receive_dossiers = 1 "
                "ORDER BY waterfall_priority ASC, id ASC")
            eligible = []
            for r in (rows or []):
                allowed = self._parse_duo_allowed(r.get("duo_allowed"))
                if not allowed or "shadow" not in allowed:
                    continue
                eligible.append(r)
            self._waterfall_accounts = eligible
            valid_aids = {a["account_id"] for a in eligible}
            valid_aids.add(self._cfg.get("account_id", "ShadowDemo"))
            stale = [k for k in self._executors if k not in valid_aids]
            for k in stale:
                self._executors.pop(k, None)
                self._exchange_names.pop(k, None)
                self._market_info_by_account.pop(k, None)
        except Exception as e:
            logger.warning(f"[ShadowQ] Failed to load waterfall accounts: {e}")
            self._waterfall_accounts = []

    def _ensure_executor(self):
        account_id = self._cfg.get("account_id", "ShadowDemo")
        if not self._executor and account_id:
            try:
                acct = self.db.fetch_one(
                    "SELECT exchange FROM trading_accounts WHERE account_id = %s",
                    (account_id,))
                if acct:
                    self._exchange_name = acct.get("exchange", "bybit")
                from core.ccxt_executor import get_executor
                self._executor = get_executor(account_id, self.db)
                if self._executor:
                    self._cache_market_info()
                    self._executors[account_id] = self._executor
                    self._exchange_names[account_id] = self._exchange_name
                    logger.info(f"[ShadowQ] Primary executor connected: {account_id} "
                                f"({self._exchange_name})")
                else:
                    logger.warning(f"[ShadowQ] Could not create executor for {account_id}")
            except Exception as e:
                logger.error(f"[ShadowQ] Executor init failed: {e}")

        if self._is_waterfall_enabled():
            self._load_waterfall_accounts()
            for wf_acct in self._waterfall_accounts:
                aid = wf_acct["account_id"]
                if aid in self._executors:
                    continue
                try:
                    from core.ccxt_executor import get_executor
                    ex = get_executor(aid, self.db)
                    if ex:
                        self._executors[aid] = ex
                        self._exchange_names[aid] = wf_acct.get("exchange", "unknown")
                        self._cache_market_info_for(aid, ex)
                        logger.info(f"[ShadowQ] Waterfall executor connected: {aid} "
                                    f"({wf_acct.get('exchange', '?')})")
                except Exception as e:
                    logger.debug(f"[ShadowQ] Waterfall executor init for {aid}: {e}")
        else:
            primary = self._cfg.get("account_id", "ShadowDemo")
            stale = [k for k in self._executors if k != primary]
            for k in stale:
                self._executors.pop(k, None)
                self._exchange_names.pop(k, None)
                self._market_info_by_account.pop(k, None)
            self._waterfall_accounts = []

    def _cache_market_info_for(self, account_id: str, executor):
        """Cache market info for a specific executor/account."""
        try:
            with executor._lock:
                markets = executor._exchange.markets
                if not markets:
                    executor._exchange.load_markets()
                    markets = executor._exchange.markets
            info = {}
            for sym, mkt in (markets or {}).items():
                limits = mkt.get("limits", {})
                amount_limits = limits.get("amount", {})
                cost_limits = limits.get("cost", {})
                lev_limits = limits.get("leverage", {})
                precision = mkt.get("precision", {})
                info[sym] = {
                    "min_amount": float(amount_limits.get("min") or 0),
                    "min_cost": float(cost_limits.get("min") or 0),
                    "max_leverage": int(lev_limits.get("max") or 100),
                    "price_precision": precision.get("price"),
                    "amount_precision": precision.get("amount"),
                }
            self._market_info_by_account[account_id] = info
            if account_id == self._cfg.get("account_id", "ShadowDemo"):
                self._market_info = info
            logger.info(f"[ShadowQ] Cached market info for {account_id}: "
                        f"{len(info)} symbols")
        except Exception as e:
            logger.warning(f"[ShadowQ] Market info cache for {account_id} failed: {e}")

    def _get_executor_for(self, account_id: str):
        """Get executor for a specific account, falling back to primary."""
        return self._executors.get(account_id, self._executor)

    def _get_market_info_for(self, account_id: str) -> Dict:
        return self._market_info_by_account.get(account_id, self._market_info)

    def _cache_market_info(self):
        if not self._executor or self._market_info:
            return
        try:
            with self._executor._lock:
                markets = self._executor._exchange.markets
                if not markets:
                    self._executor._exchange.load_markets()
                    markets = self._executor._exchange.markets
            for sym, mkt in (markets or {}).items():
                limits = mkt.get("limits", {})
                amount_limits = limits.get("amount", {})
                cost_limits = limits.get("cost", {})
                lev_limits = limits.get("leverage", {})
                precision = mkt.get("precision", {})
                self._market_info[sym] = {
                    "min_amount": float(amount_limits.get("min") or 0),
                    "min_cost": float(cost_limits.get("min") or 0),
                    "max_leverage": int(lev_limits.get("max") or 100),
                    "price_precision": precision.get("price"),
                    "amount_precision": precision.get("amount"),
                }
            logger.info(f"[ShadowQ] Cached market info for {len(self._market_info)} symbols")
        except Exception as e:
            logger.warning(f"[ShadowQ] Market info cache failed: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # POLLING
    # ═══════════════════════════════════════════════════════════════════

    def _poll_new_shadows(self):
        try:
            lookback = self._started_at - timedelta(minutes=5) if self._started_at else None
            rows = self.db.fetch_all("""
                SELECT s.id, s.symbol, s.direction, s.entry_price, s.stop_loss,
                       s.take_profit_1, s.take_profit_2, s.take_profit_3,
                       s.confidence_score, s.model_used, s.asset_class,
                       s.duo_id, s.rejected_at,
                       s.regime_sizing_multiplier
                FROM apex_shadow_trades s
                WHERE s.shadow_status = 'pending'
                  AND s.entry_price IS NOT NULL
                  AND s.stop_loss IS NOT NULL
                  AND s.direction IS NOT NULL
                  AND s.rejected_at >= %s
                  AND NOT EXISTS (SELECT 1 FROM shadow_queue sq
                                  WHERE sq.shadow_trade_id = s.id)
                ORDER BY s.rejected_at DESC
                LIMIT 100
            """, (lookback,))
        except Exception as e:
            logger.error(f"[ShadowQ] Poll failed: {e}")
            return

        if not rows:
            if self._cycle_count % 30 == 0:
                logger.debug(f"[ShadowQ] Cycle #{self._cycle_count}: no new shadows to poll")
            return

        added = 0
        replaced = 0
        for r in rows:
            passes, reason = self._passes_filter(r)
            if not passes:
                self._enqueue(r, status="skipped", skip_reason=reason)
                continue
            victim = self._should_replace(r)
            if victim:
                try:
                    if victim.get("order_id"):
                        v_ex = self._get_executor_for(victim.get("account_id")) or self._executor
                        if v_ex:
                            v_ex.cancel_order(
                                victim["order_id"],
                                victim.get("exchange_symbol", ""))
                    if victim.get("live_trade_id"):
                        self.db.execute(
                            "UPDATE live_trades SET status = 'cancelled', "
                            "close_comment = 'ShadowQ replaced by better candidate' "
                            "WHERE id = %s AND status = 'pending'",
                            (victim["live_trade_id"],))
                    if victim.get("shadow_trade_id"):
                        self.db.execute(
                            "UPDATE apex_shadow_trades SET placed_on_exchange = 0, "
                            "exchange_order_id = NULL, exchange_status = 'not_placed' "
                            "WHERE id = %s", (victim["shadow_trade_id"],))
                    self._update_queue_status(victim["id"], "replaced")
                    replaced += 1
                    self._log_activity("REPLACE", r.get("symbol"), "INFO",
                                       f"Replaced shadow #{victim['shadow_trade_id']} "
                                       f"with #{r['id']} (higher conf/R:R)")
                except Exception as e:
                    logger.debug(f"[ShadowQ] Replace cleanup error: {e}")
                    self._enqueue(r, status="skipped",
                                  skip_reason="replace cleanup failed, existing still active")
                    continue
            elif self._has_queued_for_symbol_direction(r):
                self._enqueue(r, status="skipped",
                              skip_reason="same symbol+direction already queued/placed")
                continue
            self._enqueue(r, status="queued")
            added += 1

        if added:
            msg = f"Polled {len(rows)} new shadows, {added} passed filters"
            if replaced:
                msg += f", {replaced} replacements"
            self._log_activity("POLL", None, "INFO", msg)
            logger.info(f"[ShadowQ] {msg}")

    def _passes_filter(self, row: dict) -> Tuple[bool, str]:
        min_conf = _safe_int(self._cfg.get("min_confidence", "70"), 70)
        conf = int(row.get("confidence_score") or 0)
        if conf < min_conf:
            return False, f"confidence {conf} < {min_conf}"

        allowed_models_raw = self._cfg.get("allowed_models", "[]")
        try:
            allowed = json.loads(allowed_models_raw)
        except Exception:
            allowed = []
        model = (row.get("model_used") or "").lower()
        if allowed:
            if not any(model.startswith(prefix.lower()) for prefix in allowed):
                return False, f"model '{model}' not in allowed list"

        direction = (row.get("direction") or "").upper()
        if direction not in ("BUY", "SELL"):
            return False, f"direction '{direction}' is invalid (expected BUY or SELL)"
        dir_filter = self._cfg.get("direction_filter", "both")
        if dir_filter == "long" and direction != "BUY":
            return False, f"direction {direction} filtered (long only)"
        if dir_filter == "short" and direction != "SELL":
            return False, f"direction {direction} filtered (short only)"

        blacklist_raw = self._cfg.get("blacklist", "[]")
        try:
            blacklist = [s.upper() for s in json.loads(blacklist_raw)]
        except Exception:
            blacklist = []
        symbol = (row.get("symbol") or "").upper()
        if symbol in blacklist:
            return False, f"{symbol} is blacklisted"

        # Smart Queue: mentor blacklist (safety net — Scout also filters)
        mentor_bl_raw = self._cfg.get("shadow_mentor_blacklist", "[]")
        try:
            mentor_bl = [m.lower() for m in json.loads(mentor_bl_raw)]
        except Exception:
            mentor_bl = []
        if mentor_bl:
            author = (row.get("author") or row.get("raw_author") or "").lower()
            if author in mentor_bl:
                return False, f"mentor '{author}' is blacklisted"

        # Smart Queue: minimum R:R gate
        min_rr = _safe_float(self._cfg.get("shadow_min_rr", "0"), 0)
        if min_rr > 0:
            entry = _safe_float(row.get("entry_price") or row.get("entry"), 0)
            sl = _safe_float(row.get("stop_loss") or row.get("sl"), 0)
            tp = _safe_float(row.get("take_profit_1") or row.get("tp1"), 0)
            if entry > 0 and sl > 0 and tp > 0:
                risk = abs(entry - sl)
                reward = abs(tp - entry)
                rr = reward / risk if risk > 0 else 0
                if rr < min_rr:
                    return False, f"R:R {rr:.1f} < min {min_rr}"

        return True, ""

    def _get_coin_reputation(self, symbol: str) -> float:
        """Dynamic coin reputation based on recent shadow trade results.
        New/unknown coins start at 0.5 (pessimistic). Winning coins rise, losing coins fall.
        Uses exponential decay so recent results matter more."""
        try:
            memory = _safe_int(self._cfg.get("shadow_coin_memory", "8"), 8)
            rows = self.db.fetch_all("""
                SELECT sq.queue_status,
                       CASE WHEN COALESCE(lt.realised_pnl, 0) > 0 THEN 1 ELSE 0 END as is_win,
                       COALESCE(lt.realised_pnl, 0) as pnl
                FROM shadow_queue sq
                INNER JOIN live_trades lt ON lt.id = sq.live_trade_id
                WHERE sq.symbol = %s
                  AND sq.queue_status = 'closed'
                  AND lt.status = 'closed'
                ORDER BY sq.added_at DESC
                LIMIT %s
            """, (symbol, memory))
            if not rows or len(rows) < 3:
                return 0.5
            n = len(rows)
            total_weight = 0
            weighted_sum = 0
            wins = 0
            for i, r in enumerate(rows):
                w = 1.3 ** (n - 1 - i)  # recent trades get MORE weight
                total_weight += w
                pnl_val = float(r.get("pnl") or 0)
                weighted_sum += pnl_val * w
                if r.get("is_win"):
                    wins += 1
            avg_pnl = weighted_sum / total_weight if total_weight > 0 else 0
            wr = wins / n
            rep = max(0.2, 0.5 + avg_pnl / 8.0 + (wr - 0.35) * 1.5)
            return min(rep, 2.5)
        except Exception as e:
            logger.debug(f"[ShadowQ] Coin reputation for {symbol}: {e}")
            return 0.5

    def _has_queued_for_symbol_direction(self, row: dict) -> bool:
        """Check if an active (queued/placed/filled) queue entry exists for same symbol+direction."""
        symbol = (row.get("symbol") or "").upper()
        direction = (row.get("direction") or "").upper()
        try:
            existing = self.db.fetch_one("""
                SELECT 1 FROM shadow_queue
                WHERE symbol = %s AND direction = %s
                  AND queue_status IN ('queued', 'placed', 'filled')
                LIMIT 1
            """, (symbol, direction))
            return existing is not None
        except Exception:
            return False

    def _should_replace(self, new_row: dict) -> Optional[dict]:
        """Check if a queued/placed shadow for same symbol+direction should be replaced.
        Uses dossier-provided entry/SL/TP for R:R — no hardcoded floors.
        Returns the existing queue row to replace, or None.
        Two paths (per spec):
          A) Higher confidence AND better-or-equal R:R
          B) Significantly higher confidence (>=5 pts) AND fresher dossier"""
        symbol = (new_row.get("symbol") or "").upper()
        direction = (new_row.get("direction") or "").upper()
        try:
            existing = self.db.fetch_one("""
                SELECT sq.id, sq.shadow_trade_id, sq.order_id, sq.exchange_symbol,
                       sq.queue_status, sq.live_trade_id, sq.account_id,
                       ast.confidence_score AS ex_conf,
                       ast.entry_price AS ex_entry, ast.stop_loss AS ex_sl,
                       ast.take_profit_1 AS ex_tp1, ast.rejected_at AS ex_age
                FROM shadow_queue sq
                JOIN apex_shadow_trades ast ON ast.id = sq.shadow_trade_id
                WHERE sq.symbol = %s AND sq.direction = %s
                  AND sq.queue_status IN ('queued','placed')
                LIMIT 1
            """, (symbol, direction))
        except Exception:
            return None
        if not existing:
            return None
        new_conf = int(new_row.get("confidence_score") or 0)
        ex_conf = int(existing.get("ex_conf") or 0)
        new_entry = float(new_row.get("entry_price") or 0)
        new_sl = float(new_row.get("stop_loss") or 0)
        new_tp = float(new_row.get("take_profit_1") or 0)
        ex_entry = float(existing.get("ex_entry") or 0)
        ex_sl = float(existing.get("ex_sl") or 0)
        ex_tp = float(existing.get("ex_tp1") or 0)
        new_rr = abs(new_tp - new_entry) / abs(new_entry - new_sl) if abs(new_entry - new_sl) > 0 else 0
        ex_rr = abs(ex_tp - ex_entry) / abs(ex_entry - ex_sl) if abs(ex_entry - ex_sl) > 0 else 0
        if new_conf > ex_conf and new_rr >= ex_rr:
            return dict(existing)
        if new_conf >= (ex_conf + 5):
            new_age = new_row.get("rejected_at")
            ex_age = existing.get("ex_age")
            if new_age and ex_age and new_age > ex_age:
                return dict(existing)
        return None

    def _resolve_waterfall_account(self, symbol: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Try each waterfall account in priority order to find one that can trade this symbol.
        Returns (account_id, exchange_name, exchange_symbol) or (None, None, None)."""
        from db.market_symbols import can_trade_on_exchange

        if self._is_waterfall_enabled() and self._waterfall_accounts:
            for acct in self._waterfall_accounts:
                aid = acct["account_id"]
                if aid not in self._executors:
                    continue
                exch = acct.get("exchange", "bybit")
                try:
                    can_trade, ticker_or_reason = can_trade_on_exchange(
                        symbol, exch, self.db)
                    if can_trade and ticker_or_reason and ":" in ticker_or_reason:
                        return aid, exch, ticker_or_reason
                except Exception:
                    continue

        fallback_aid = self._cfg.get("account_id", "ShadowDemo")
        fallback_exch = self._exchange_name
        try:
            can_trade, ticker_or_reason = can_trade_on_exchange(
                symbol, fallback_exch, self.db)
            if can_trade and ticker_or_reason and ":" in ticker_or_reason:
                return fallback_aid, fallback_exch, ticker_or_reason
        except Exception:
            pass
        return None, None, None

    def _enqueue(self, row: dict, status: str = "queued",
                 skip_reason: str = None):
        symbol = (row.get("symbol") or "").upper()

        if status != "queued":
            target_aid, target_exch, exsym = None, None, None
            tradable = False
        else:
            target_aid, target_exch, exsym = self._resolve_waterfall_account(symbol)
            tradable = target_aid is not None and exsym is not None

        try:
            self.db.execute("""
                INSERT INTO shadow_queue
                (shadow_trade_id, symbol, exchange_symbol, account_id, exchange_name,
                 direction,
                 entry_price, stop_loss, take_profit_1, take_profit_2, take_profit_3,
                 confidence_score, model_used, tradable_on_exchange,
                 queue_status, skip_reason, regime_sizing_multiplier)
                VALUES (%s, %s, %s, %s, %s,
                        %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s)
            """, (
                row["id"], symbol, exsym, target_aid, target_exch,
                row.get("direction"),
                row.get("entry_price"), row.get("stop_loss"),
                row.get("take_profit_1"), row.get("take_profit_2"),
                row.get("take_profit_3"),
                int(row.get("confidence_score") or 0), row.get("model_used"),
                1 if tradable else 0, status, skip_reason,
                float(row.get("regime_sizing_multiplier") or 1.0),
            ))
            if target_aid and target_aid != self._cfg.get("account_id", "ShadowDemo"):
                logger.info(f"[ShadowQ] Waterfall: {symbol} → {target_aid} ({target_exch})")
        except Exception as e:
            logger.debug(f"[ShadowQ] Enqueue failed for shadow #{row['id']}: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # RANKING & DISTANCE
    # ═══════════════════════════════════════════════════════════════════

    def _update_distances(self):
        try:
            queued = self.db.fetch_all("""
                SELECT id, symbol, entry_price, stop_loss, direction
                FROM shadow_queue
                WHERE queue_status IN ('queued', 'placed')
            """)
        except Exception:
            return
        if not queued:
            return

        from services.price_streamer import get_price_streamer
        ps = get_price_streamer()

        for q in queued:
            entry = float(q.get("entry_price") or 0)
            sl = float(q.get("stop_loss") or 0)
            direction = (q.get("direction") or "").upper()
            if entry <= 0:
                continue
            price_data = ps.get_price(q["symbol"]) if ps else None
            if not price_data:
                continue
            current = float(price_data.get("price") or price_data.get("last") or 0)
            if current <= 0:
                continue
            dist = abs(current - entry) / entry * 100
            try:
                self.db.execute(
                    "UPDATE shadow_queue SET distance_pct = %s, current_price = %s WHERE id = %s",
                    (round(dist, 4), current, q["id"]))
            except Exception as e:
                logger.warning(f"[ShadowQ] Distance update failed for queue #{q['id']}: {e}")

            if self._cooloff_active and q.get("queue_status") == "queued":
                entry_passed = False
                if direction == "BUY" and sl > 0 and current < entry:
                    if current <= sl or (current < entry and current < entry * 0.99):
                        entry_passed = True
                elif direction == "SELL" and sl > 0 and current > entry:
                    if current >= sl or (current > entry and current > entry * 1.01):
                        entry_passed = True
                if entry_passed:
                    self._update_queue_status(
                        q["id"], "missed",
                        skip_reason="entry_passed_during_cooloff")
                    self._log_activity("MISSED", q["symbol"], "INFO",
                                       f"Entry passed during cool-off "
                                       f"(entry={entry}, current={current})")

    def _expire_tp1_overshot(self):
        """Remove queued trades whose TP1 has already been reached.

        If price already hit TP1 before we placed the order, the setup edge
        is gone and holding the queue slot is wasteful.

        Two checks per item:
        1. Fast: current snapshot price (from ``_update_distances()``)
        2. Wick: recent M5 candle highs/lows from the ``candles`` table,
           which catches intra-bar wicks that pulled back before the
           next snapshot.
        """
        try:
            rows = self.db.fetch_all("""
                SELECT id, symbol, direction, entry_price,
                       take_profit_1, current_price, added_at
                FROM shadow_queue
                WHERE queue_status = 'queued'
                  AND take_profit_1 IS NOT NULL
                  AND take_profit_1 > 0
            """)
        except Exception:
            return
        if not rows:
            return

        sym_wicks = {}
        symbols = set(r["symbol"] for r in rows if r.get("symbol"))
        for sym in symbols:
            try:
                wicks = self.db.fetch_all("""
                    SELECT high, low FROM candles
                    WHERE symbol = %s AND timeframe = 'M5'
                    ORDER BY candle_time DESC LIMIT 6
                """, (sym,))
                if wicks:
                    sym_wicks[sym] = {
                        "high": max(float(w["high"]) for w in wicks),
                        "low": min(float(w["low"]) for w in wicks),
                    }
            except Exception:
                pass

        expired = 0
        for r in rows:
            tp1 = float(r["take_profit_1"])
            cur = float(r.get("current_price") or 0)
            direction = (r.get("direction") or "").upper()
            sym = r.get("symbol", "")
            if not direction:
                continue

            hit = False
            price_used = cur
            wicks = sym_wicks.get(sym)

            if direction == "BUY":
                wick_high = wicks["high"] if wicks else 0
                if (cur > 0 and cur >= tp1) or (wick_high > 0 and wick_high >= tp1):
                    hit = True
                    price_used = max(cur, wick_high) if wick_high > 0 else cur
            elif direction == "SELL":
                wick_low = wicks["low"] if wicks else float("inf")
                if (cur > 0 and cur <= tp1) or (wicks and wick_low <= tp1):
                    hit = True
                    price_used = min(cur, wick_low) if wicks else cur

            if hit:
                self._update_queue_status(
                    r["id"], "missed",
                    skip_reason=f"tp1_overshot (TP1={tp1:.5f}, price={price_used:.5f})")
                self._log_activity(
                    "MISSED", sym, "INFO",
                    f"TP1 overshot: price {price_used:.5f} already past "
                    f"TP1 {tp1:.5f} ({direction}) — removed from queue")
                expired += 1
        if expired:
            logger.info(f"[ShadowQ] TP1 overshot: expired {expired} queued trades")

    def _rank_queue(self):
        tce_enabled = self._cfg.get("tce_enabled", "true").strip().lower() == "true"
        if tce_enabled:
            self._rank_queue_tce()
        else:
            self._rank_queue_legacy()

    def _rank_queue_legacy(self):
        """Original ranking: distance + confidence + freshness."""
        try:
            queued = self.db.fetch_all("""
                SELECT id, tradable_on_exchange, distance_pct,
                       confidence_score, added_at
                FROM shadow_queue WHERE queue_status = 'queued'
            """)
        except Exception:
            return
        if not queued:
            return

        now = datetime.utcnow()
        batch = []
        for q in queued:
            tradable = int(q.get("tradable_on_exchange") or 0)
            dist = float(q.get("distance_pct") or 100)
            conf = float(q.get("confidence_score") or 0)
            added = q.get("added_at")
            age_hours = 1.0
            if added:
                try:
                    age_hours = max(0.1, (now - added).total_seconds() / 3600)
                except Exception:
                    pass

            dist_score = max(0.0, min(1.0, 1.0 - (dist / 10.0)))
            conf_score = max(0.0, min(1.0, conf / 100.0))
            fresh_score = max(0.0, min(1.0, 1.0 - (age_hours / 48.0)))

            if not tradable:
                batch.append((0.0, q["id"]))
                continue

            priority = round(dist_score * 0.50
                             + conf_score * 0.30
                             + fresh_score * 0.20, 4)
            batch.append((priority, q["id"]))

        if batch:
            for score, qid in batch:
                try:
                    self.db.execute(
                        "UPDATE shadow_queue SET priority_score = %s WHERE id = %s",
                        (score, qid))
                except Exception as e:
                    logger.warning(f"[ShadowQ] Priority score update failed for queue #{qid}: {e}")

    def _rank_queue_tce(self):
        """TCE-powered ranking: 5-layer conviction scoring."""
        try:
            queued = self.db.fetch_all("""
                SELECT id, symbol, direction, entry_price, stop_loss, take_profit_1,
                       confidence_score, tradable_on_exchange, distance_pct,
                       exchange_symbol, added_at
                FROM shadow_queue WHERE queue_status = 'queued'
                ORDER BY added_at DESC
                LIMIT 200
            """)
        except Exception:
            return
        if not queued:
            return

        from services.trade_conviction import TradeConvictionEngine
        tce = TradeConvictionEngine()

        regime_score = None
        regime_label = None
        regime_enrichment = None
        manus_data = None
        try:
            row = self.db.fetch_one(
                "SELECT regime_score, regime_label, components FROM market_regime_history "
                "WHERE source='full' ORDER BY created_at DESC LIMIT 1")
            if row:
                regime_score = float(row.get("regime_score") or 0)
                regime_label = row.get("regime_label")
                import json
                comp = row.get("components")
                if comp and isinstance(comp, str):
                    comp = json.loads(comp)
                regime_enrichment = comp if isinstance(comp, dict) else None
        except Exception as e:
            logger.debug(f"[ShadowQ] Regime fetch for TCE failed: {e}")

        # Load Manus market intelligence (if available, fresh, and enabled)
        enrichment_data = {"funding_rate": None, "long_short_ratio": None}
        manus_cfg_on = (self._cfg.get("manus_intel_enabled", "false")
                        .strip().lower() == "true")
        if manus_cfg_on:
            try:
                max_age = int(self._cfg.get("manus_intel_max_age_minutes", "120"))
                manus_row = self.db.fetch_one(
                    "SELECT * FROM market_regime_intel "
                    "WHERE timestamp >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s MINUTE) "
                    "ORDER BY timestamp DESC LIMIT 1", (max_age,))
                if manus_row:
                    enrichment_data["funding_rate"] = manus_row.get(
                        "btc_oi_weighted_funding_rate")
                    ls = manus_row.get("hyperliquid_ls_ratio")
                    if ls is not None:
                        enrichment_data["long_short_ratio"] = float(ls)
                    manus_data = {"market_context": {
                        "average_crypto_rsi": manus_row.get("average_crypto_rsi"),
                        "percent_overbought": manus_row.get("percent_overbought"),
                        "percent_oversold": manus_row.get("percent_oversold"),
                        "btc_oi_weighted_funding_rate": manus_row.get(
                            "btc_oi_weighted_funding_rate"),
                        "hyperliquid_long_traders": manus_row.get(
                            "hyperliquid_long_traders"),
                        "hyperliquid_short_traders": manus_row.get(
                            "hyperliquid_short_traders"),
                        "hyperliquid_ls_ratio": ls,
                        "btc_liquidation_cluster_bias": manus_row.get(
                            "btc_liquidation_cluster_bias"),
                    }}
            except Exception as e:
                logger.debug(f"[ShadowQ] Manus intel fetch for TCE: {e}")

        recent_trades_cache = {}
        sym_intel_cache = {}
        min_score = _safe_float(self._cfg.get("tce_min_score_to_place", "25"), 25.0)
        batch = []
        import json as _json

        # Pre-load symbol intel for all queued symbols (avoids N+1 queries)
        try:
            all_syms = list({q["symbol"] for q in queued
                            if int(q.get("tradable_on_exchange") or 0)})
            if all_syms:
                placeholders = ",".join(["%s"] * len(all_syms))
                _si_rows = self.db.fetch_all(
                    f"SELECT symbol, llm_consensus, manus_oi_change_24h, "
                    f"manus_funding_rate_binance, grok_direction, grok_confidence "
                    f"FROM symbol_intel WHERE symbol IN ({placeholders}) "
                    f"AND duo_id = '_grok_setup'", tuple(all_syms))
                sym_intel_cache = {r["symbol"]: r for r in (_si_rows or [])}
        except Exception:
            pass

        for q in queued:
            tradable = int(q.get("tradable_on_exchange") or 0)
            if not tradable:
                batch.append((0.0, None, q["id"], q.get("symbol", "")))
                continue

            sym = q["symbol"]
            direction = q["direction"]
            entry = float(q.get("entry_price") or 0)
            sl = float(q.get("stop_loss") or 0)
            tp1 = float(q.get("take_profit_1") or 0)
            conf = int(q.get("confidence_score") or 0)

            if sym not in recent_trades_cache:
                try:
                    recent_trades_cache[sym] = self.db.fetch_all(
                        "SELECT sq.direction, lt.realised_pnl "
                        "FROM shadow_queue sq "
                        "LEFT JOIN live_trades lt ON lt.id = sq.live_trade_id "
                        "WHERE sq.symbol = %s AND sq.queue_status = 'closed' "
                        "AND sq.live_trade_id IS NOT NULL "
                        "ORDER BY sq.cancelled_at DESC LIMIT 20", (sym,))
                except Exception:
                    recent_trades_cache[sym] = []

            sym_intel = sym_intel_cache.get(sym)

            result = tce.evaluate(
                symbol=sym, direction=direction,
                entry_price=entry, stop_loss=sl, take_profit_1=tp1,
                confidence=conf,
                market_score=regime_score, market_label=regime_label,
                enrichment=enrichment_data,
                manus_data=manus_data,
                symbol_intel=sym_intel,
                recent_trades=recent_trades_cache.get(sym, []),
            )

            tcs = result.get("tcs", 0)
            components = result.get("sub_scores", {})
            if result.get("circuit_breaker"):
                components["_circuit_breaker"] = True
                components["_gates_failed"] = result.get("gates_failed", [])
                components["_verdict"] = result.get("verdict", "blocked")
            batch.append((tcs, _json.dumps(components), q["id"], sym))

        if batch:
            coin_rep_enabled = self._cfg.get("shadow_coin_rep_enabled", "true").lower() == "true"
            coin_rep_cache = {}
            for score, components_json, qid, batch_sym in batch:
                try:
                    final_score = score
                    if coin_rep_enabled and batch_sym:
                        if batch_sym not in coin_rep_cache:
                            coin_rep_cache[batch_sym] = self._get_coin_reputation(batch_sym)
                        coin_mult = coin_rep_cache[batch_sym]
                        final_score = score * coin_mult
                    if components_json:
                        self.db.execute(
                            "UPDATE shadow_queue SET priority_score = %s, tcs_score = %s, "
                            "tcs_components = %s WHERE id = %s",
                            (final_score, score, components_json, qid))
                    else:
                        self.db.execute(
                            "UPDATE shadow_queue SET priority_score = %s, tcs_score = %s WHERE id = %s",
                            (final_score, score, qid))
                except Exception as e:
                    logger.warning(f"[ShadowQ] TCE score update failed for queue #{qid}: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # ORDER PLACEMENT
    # ═══════════════════════════════════════════════════════════════════

    def _get_balance_for(self, account_id: str) -> dict:
        """Get balance for a specific account, with caching."""
        now = time.time()
        cached = self._cached_balances.get(account_id)
        cached_at = self._cached_balances_at.get(account_id, 0)
        if cached and (now - cached_at) < 30:
            return cached
        executor = self._get_executor_for(account_id)
        if not executor:
            return {}
        try:
            bal = executor.get_balance()
            self._cached_balances[account_id] = bal
            self._cached_balances_at[account_id] = now
            return bal
        except Exception:
            return cached or {}

    def _get_margin_used_for(self, account_id: str) -> float:
        """Get margin used for a specific account from DB."""
        try:
            row = self.db.fetch_one("""
                SELECT COALESCE(SUM(margin_allocated), 0) AS total
                FROM shadow_queue
                WHERE queue_status IN ('placed', 'filled')
                  AND account_id = %s
            """, (account_id,))
            return float(row["total"]) if row else 0.0
        except Exception:
            return 0.0

    def _place_next_orders(self):
        if not self._executor and not self._executors:
            return

        max_per_cycle = _safe_int(self._cfg.get("max_orders_per_cycle", "1"), 1)
        margin_cap_pct = _safe_float(self._cfg.get("margin_cap_pct", "33"), 33.0)

        primary_aid = self._cfg.get("account_id", "ShadowDemo")
        waterfall_on = self._is_waterfall_enabled()

        if not waterfall_on:
            balance = self._cached_balance if self._cached_balance else {}
            total_bal = balance.get("total", 0)
            if total_bal <= 0 and self._executor:
                try:
                    balance = self._executor.get_balance()
                    total_bal = balance.get("total", 0)
                except Exception as e:
                    logger.warning(f"[ShadowQ] Balance fetch failed: {e}")
                    total_bal = 0

            max_margin = total_bal * margin_cap_pct / 100.0 if total_bal > 0 else 0
            used_margin = self._get_margin_used_for(primary_aid)
            available = max_margin - used_margin if total_bal > 0 else 0

            if available < 0 and total_bal > 0:
                self._enforce_margin_cap(max_margin, used_margin, primary_aid)
                used_margin = self._get_margin_used_for(primary_aid)
                available = max_margin - used_margin

        if waterfall_on:
            for wf_acct in self._waterfall_accounts:
                wf_aid = wf_acct["account_id"]
                try:
                    wf_bal = self._get_balance_for(wf_aid)
                    wf_total = wf_bal.get("total", 0)
                    wf_cap = float(wf_acct.get("margin_cap_pct") or margin_cap_pct)
                    wf_max = wf_total * wf_cap / 100.0
                    wf_used = self._get_margin_used_for(wf_aid)
                    if wf_used > wf_max and wf_total > 0:
                        self._enforce_margin_cap(wf_max, wf_used, wf_aid)
                except Exception as e:
                    logger.debug(f"[ShadowQ] Per-account margin check for {wf_aid}: {e}")

        try:
            promoted = self.db.execute("""
                UPDATE shadow_queue SET queue_status = 'queued', skip_reason = NULL,
                       account_id = NULL, exchange_name = NULL, exchange_symbol = NULL,
                       tradable_on_exchange = 0
                WHERE queue_status = 'margin_wait'
            """)
            if promoted:
                logger.info(f"[ShadowQ] Re-promoted {promoted} margin_wait -> queued"
                            f"{' (cleared accounts for re-routing)' if waterfall_on else ''}")
        except Exception as e:
            logger.debug(f"[ShadowQ] margin_wait re-promote: {e}")

        try:
            if waterfall_on:
                candidates = self.db.fetch_all("""
                    SELECT * FROM shadow_queue
                    WHERE queue_status = 'queued'
                      AND ( (tradable_on_exchange = 1 AND exchange_symbol IS NOT NULL)
                            OR account_id IS NULL )
                    ORDER BY priority_score DESC
                    LIMIT 20
                """)
            else:
                candidates = self.db.fetch_all("""
                    SELECT * FROM shadow_queue
                    WHERE queue_status = 'queued'
                      AND tradable_on_exchange = 1
                      AND exchange_symbol IS NOT NULL
                    ORDER BY priority_score DESC
                    LIMIT 20
                """)
        except Exception:
            return
        if not candidates:
            return

        placed = 0
        per_account_available: Dict[str, float] = {}

        for c in candidates:
            if placed >= max_per_cycle:
                break

            symbol = c["symbol"]
            direction = c["direction"]

            if self._velocity_alert:
                if self._velocity_direction == "drop" and direction == "BUY":
                    logger.info(f"[ShadowQ] Blocked BUY {symbol} — velocity drop alert active")
                    continue
                if self._velocity_direction == "pump" and direction == "SELL":
                    logger.info(f"[ShadowQ] Blocked SELL {symbol} — velocity pump alert active")
                    continue

            protection_on = self._cfg.get("protection_circuits_enabled", "true").strip().lower() == "true"
            if protection_on and self._current_regime_score is not None:
                min_regime_buy = _safe_float(self._cfg.get("tce_min_regime_for_buy", "-30"), -30.0)
                max_regime_sell = _safe_float(self._cfg.get("tce_max_regime_for_sell", "30"), 30.0)
                if direction == "BUY" and self._current_regime_score < min_regime_buy:
                    logger.info(f"[ShadowQ] Blocked BUY {symbol} — regime {self._current_regime_score:.0f} "
                                f"< min {min_regime_buy}")
                    continue
                if direction == "SELL" and self._current_regime_score > max_regime_sell:
                    logger.info(f"[ShadowQ] Blocked SELL {symbol} — regime {self._current_regime_score:.0f} "
                                f"> max {max_regime_sell}")
                    continue

            tce_enabled = self._cfg.get("tce_enabled", "true").strip().lower() == "true"
            if tce_enabled:
                tcs = float(c.get("tcs_score") or c.get("priority_score") or 0)
                min_tcs = _safe_float(self._cfg.get("tce_min_score_to_place", "25"), 25.0)
                if tcs < min_tcs:
                    self._update_queue_status(
                        c["id"], "skipped",
                        skip_reason=f"TCS {tcs:.1f} below minimum {min_tcs}")
                    continue

            if self._has_existing_position(symbol):
                continue

            target_aid = c.get("account_id")
            target_exch = c.get("exchange_name")
            esym_resolved = c.get("exchange_symbol")

            if not target_aid and waterfall_on:
                re_aid, re_exch, re_esym = self._resolve_waterfall_account(symbol)
                if re_aid:
                    target_aid = re_aid
                    target_exch = re_exch
                    esym_resolved = re_esym
                    try:
                        self.db.execute(
                            "UPDATE shadow_queue SET account_id = %s, exchange_name = %s, "
                            "exchange_symbol = %s, tradable_on_exchange = 1 WHERE id = %s",
                            (re_aid, re_exch, re_esym, c["id"]))
                    except Exception:
                        pass

            target_aid = target_aid or primary_aid
            target_exch = target_exch or self._exchange_name
            executor = self._get_executor_for(target_aid)
            if not executor:
                logger.debug(f"[ShadowQ] No executor for account {target_aid}, skipping {symbol}")
                continue

            if target_aid not in per_account_available:
                acct_bal = self._get_balance_for(target_aid)
                acct_total = acct_bal.get("total", 0)
                if waterfall_on:
                    acct_cap = margin_cap_pct
                    try:
                        acct_row = self.db.fetch_one(
                            "SELECT margin_cap_pct FROM trading_accounts WHERE account_id = %s",
                            (target_aid,))
                        if acct_row and acct_row.get("margin_cap_pct"):
                            acct_cap = float(acct_row["margin_cap_pct"])
                    except Exception:
                        pass
                else:
                    acct_cap = margin_cap_pct
                acct_max_margin = acct_total * acct_cap / 100.0
                acct_used = self._get_margin_used_for(target_aid)
                per_account_available[target_aid] = acct_max_margin - acct_used

            acct_avail = per_account_available.get(target_aid, 0)

            mkt_info = self._get_market_info_for(target_aid)
            acct_bal_data = self._get_balance_for(target_aid)
            acct_total_bal = acct_bal_data.get("total", 0)
            if acct_total_bal <= 0:
                continue

            esym_for_sizing = esym_resolved or c["exchange_symbol"]
            sizing_mult = float(c.get("regime_sizing_multiplier") or 1.0)
            sizing = self._compute_position_size(
                float(c["entry_price"]), float(c["stop_loss"]),
                esym_for_sizing, acct_total_bal,
                market_info_override=mkt_info,
                executor_override=executor,
                regime_sizing_multiplier=sizing_mult)
            if not sizing:
                self._update_queue_status(
                    c["id"], "skipped",
                    skip_reason="below exchange minimum or sizing failed")
                self._log_activity("SKIP", symbol, "WARN",
                                   "Below exchange minimum or sizing failed")
                continue

            pos_size, margin, leverage = sizing
            if margin > acct_avail:
                continue

            esym = esym_resolved or c["exchange_symbol"]
            if not esym:
                self._update_queue_status(
                    c["id"], "skipped",
                    skip_reason="no exchange can trade this symbol")
                continue
            try:
                executor.set_margin_mode(esym)
            except Exception as e:
                logger.debug(f"[ShadowQ] Set margin mode for {esym} on {target_aid}: {e}")
            try:
                executor.set_leverage(esym, leverage)
            except Exception as e:
                logger.warning(f"[ShadowQ] Set leverage failed for {esym} on {target_aid}: {e}")

            side = direction.lower()
            sl_price = float(c["stop_loss"])
            tp_price = float(c["take_profit_1"]) if c.get("take_profit_1") else None
            entry_price = executor.price_to_precision(esym, float(c["entry_price"]))
            sl_price = executor.price_to_precision(esym, sl_price)
            if tp_price:
                tp_price = executor.price_to_precision(esym, tp_price)
            result = executor.place_limit_order(
                esym, side, pos_size, entry_price,
                sl=sl_price, tp=tp_price)

            if result.get("success"):
                order_id = result.get("order_id") or result.get("id", "")
                shadow_trade_id = c["shadow_trade_id"]

                try:
                    lt_id = self.db.execute_returning_id("""
                        INSERT INTO live_trades
                        (account_id, duo_id, dossier_id, shadow_trade_id, exchange,
                         symbol, exchange_symbol, direction, order_id, order_type,
                         entry_price, position_size, margin_usd, leverage,
                         stop_loss, take_profit_1, take_profit_2, take_profit_3,
                         status, trade_source)
                        VALUES (%s, 'shadow', NULL, %s, %s,
                                %s, %s, %s, %s, 'limit',
                                %s, %s, %s, %s,
                                %s, %s, %s, %s,
                                'pending', 'shadow')
                    """, (
                        target_aid,
                        shadow_trade_id, target_exch,
                        symbol, esym, direction, order_id,
                        float(c["entry_price"]), pos_size, margin, leverage,
                        float(c["stop_loss"]),
                        float(c["take_profit_1"]) if c.get("take_profit_1") else None,
                        float(c["take_profit_2"]) if c.get("take_profit_2") else None,
                        float(c["take_profit_3"]) if c.get("take_profit_3") else None,
                    ))
                except Exception as e:
                    logger.error(f"[ShadowQ] live_trades INSERT failed: {e}")
                    lt_id = None

                if lt_id is None:
                    try:
                        executor.cancel_order(order_id, esym)
                    except Exception:
                        pass
                    self._log_activity("ERROR", symbol, "ERROR",
                                       "live_trades INSERT failed — order cancelled")
                    continue

                try:
                    self.db.execute("""
                        UPDATE shadow_queue SET queue_status = 'placed',
                               order_id = %s, live_trade_id = %s,
                               margin_allocated = %s, leverage = %s,
                               position_size = %s, placed_at = NOW(),
                               account_id = %s, exchange_name = %s
                        WHERE id = %s
                    """, (order_id, lt_id, margin, leverage, pos_size,
                          target_aid, target_exch, c["id"]))

                    self.db.execute("""
                        UPDATE apex_shadow_trades
                        SET placed_on_exchange = 1, exchange_order_id = %s,
                            exchange_account_id = %s, exchange_status = 'pending'
                        WHERE id = %s
                    """, (order_id, target_aid, shadow_trade_id))
                except Exception as upd_err:
                    logger.error(f"[ShadowQ] Post-placement UPDATE failed for "
                                 f"{symbol}: {upd_err} — cancelling order")
                    try:
                        executor.cancel_order(order_id, esym)
                        self.db.execute(
                            "UPDATE live_trades SET status = 'cancelled' WHERE id = %s",
                            (lt_id,))
                    except Exception:
                        pass
                    self._log_activity("ERROR", symbol, "ERROR",
                                       f"Post-placement DB update failed: {upd_err}")
                    continue

                per_account_available[target_aid] = acct_avail - margin
                placed += 1
                acct_label = f"{target_aid}/{target_exch}" if target_aid != primary_aid else ""
                self._log_activity("PLACE", symbol, "INFO",
                                   f"{direction} limit @ {c['entry_price']} "
                                   f"(conf={c['confidence_score']}%, "
                                   f"margin=${margin:.2f}"
                                   f"{' on ' + acct_label if acct_label else ''})")
                logger.info(f"[ShadowQ] Placed {direction} {symbol} @ "
                            f"{c['entry_price']} on {target_aid} (order={order_id})")
            else:
                err = result.get("error", "unknown")
                err_lower = str(err).lower()
                if "insufficient balance" in err_lower or "margin" in err_lower:
                    self._update_queue_status(c["id"], "margin_wait",
                                             skip_reason=f"insufficient margin on {target_aid}")
                    self._log_activity("MARGIN", symbol, "WARN",
                                       f"Insufficient margin on {target_aid}, parked")
                    logger.warning(f"[ShadowQ] {symbol}: insufficient margin on {target_aid}")
                    continue
                permanent_errors = ("badsymbol", "invalid symbol", "symbol not found",
                                    "not found", "delisted", "not supported",
                                    "maximum sell price", "maximum buy price",
                                    "low-liquidity pair", "risk control restrictions",
                                    "pair is temporarily unavailable",
                                    "trading pair is not available",
                                    "order price exceeds", "102125", "102127")
                if any(pe in err_lower for pe in permanent_errors):
                    self._update_queue_status(c["id"], "skipped",
                                             skip_reason=f"permanent error: {err[:100]}")
                    self._log_activity("SKIP", symbol, "WARN",
                                       f"Skipped (permanent error on {target_aid}): {err[:80]}")
                else:
                    fail_key = f"{c['id']}_place_fail"
                    self._place_fail_counts = getattr(self, '_place_fail_counts', {})
                    self._place_fail_counts[fail_key] = self._place_fail_counts.get(fail_key, 0) + 1
                    if self._place_fail_counts[fail_key] >= 5:
                        self._place_fail_counts.pop(fail_key, None)
                        self._update_queue_status(c["id"], "skipped",
                                                 skip_reason=f"failed 5x: {err[:100]}")
                        self._log_activity("SKIP", symbol, "WARN",
                                           f"Skipped after 5 consecutive failures on {target_aid}: {err[:80]}")
                    else:
                        self._log_activity("ERROR", symbol, "ERROR",
                                           f"Order placement failed on {target_aid}: {err}")
                logger.warning(f"[ShadowQ] Order failed for {symbol}: {err}")

    def _compute_position_size(self, entry: float, sl: float,
                               exchange_symbol: str,
                               balance: float,
                               market_info_override: Dict = None,
                               executor_override=None,
                               regime_sizing_multiplier: float = 1.0) -> Optional[Tuple[float, float, int]]:
        risk_pct = _safe_float(self._cfg.get("risk_per_trade_pct", "0.5"), 0.5)

        # Smart Queue: dynamic margin scaling based on recent streak
        if self._cfg.get("shadow_dynamic_margin_enabled", "false").lower() == "true":
            base_mp = _safe_float(self._cfg.get("shadow_base_margin_pct", "4.0"), 4.0)
            max_mp = _safe_float(self._cfg.get("shadow_max_margin_pct", "8.0"), 8.0)
            risk_pct = base_mp
            try:
                recent = self.db.fetch_all("""
                    SELECT lt.realised_pnl as pnl FROM shadow_queue sq
                    JOIN live_trades lt ON lt.id = sq.live_trade_id
                    WHERE sq.queue_status = 'closed' AND lt.status = 'closed'
                    ORDER BY sq.added_at DESC LIMIT 8
                """)
                consec_wins = 0
                for r in (recent or []):
                    if float(r.get("pnl") or 0) > 0:
                        consec_wins += 1
                    else:
                        break
                if consec_wins >= 4:
                    risk_pct = min(max_mp, base_mp * 1.75)
                elif consec_wins >= 2:
                    risk_pct = min(max_mp, base_mp * 1.4)
                logger.debug(f"[ShadowQ] Dynamic margin: {consec_wins} consec wins -> {risk_pct:.1f}%")
            except Exception as e:
                logger.debug(f"[ShadowQ] Dynamic margin fallback: {e}")

        risk_pct = risk_pct * max(0.25, min(1.5, regime_sizing_multiplier))

        risk_usd = balance * risk_pct / 100.0

        sl_distance_abs = abs(entry - sl)
        sl_pct = sl_distance_abs / entry if entry > 0 else 1
        if sl_pct <= 0:
            return None

        src_info = market_info_override or self._market_info
        mkt = src_info.get(exchange_symbol, {})
        max_lev = mkt.get("max_leverage", 100)

        raw_lev = 1.0 / sl_pct
        safety_mult = 0.70
        safety_buffer = 3
        lev_mult = int(raw_lev * safety_mult)
        lev_flat = int(raw_lev) - safety_buffer
        leverage = max(1, min(min(lev_mult, lev_flat), max_lev))

        max_lev_for_risk = int(1.0 / sl_pct) if sl_pct > 0 else leverage
        leverage = max(1, min(leverage, max_lev_for_risk))

        max_iters = 20
        while leverage > 1 and max_iters > 0:
            liq_pct = 1.0 / leverage
            if liq_pct >= sl_pct * 1.3:
                break
            leverage -= 1
            max_iters -= 1

        notional = risk_usd * leverage
        pos_size = notional / entry if entry > 0 else 0

        min_amount = mkt.get("min_amount", 0)
        min_cost = mkt.get("min_cost", 0)
        if pos_size < min_amount or notional < min_cost:
            return None

        _ex = executor_override or self._executor
        if _ex and exchange_symbol:
            pos_size = _ex.amount_to_precision(exchange_symbol, pos_size)
        else:
            amt_prec = mkt.get("amount_precision")
            if amt_prec is not None and isinstance(amt_prec, (int, float)):
                factor = 10 ** int(amt_prec)
                pos_size = math.floor(pos_size * factor) / factor

        if pos_size <= 0:
            return None

        margin = notional / leverage if leverage > 0 else notional
        return (pos_size, round(margin, 2), leverage)

    # ═══════════════════════════════════════════════════════════════════
    # ONE-SYMBOL-ONE-DIRECTION
    # ═══════════════════════════════════════════════════════════════════

    def _has_existing_position(self, symbol: str) -> bool:
        try:
            row = self.db.fetch_one("""
                SELECT id FROM shadow_queue
                WHERE symbol = %s AND queue_status IN ('placed', 'filled')
                LIMIT 1
            """, (symbol,))
            if row:
                return True
            lt = self.db.fetch_one("""
                SELECT id FROM live_trades
                WHERE symbol = %s AND trade_source = 'shadow'
                  AND status IN ('pending', 'open', 'partial_closed')
                LIMIT 1
            """, (symbol,))
            return lt is not None
        except Exception:
            return True

    # ═══════════════════════════════════════════════════════════════════
    # ORDER EXPIRY (cancel unfilled orders after X hours)
    # ═══════════════════════════════════════════════════════════════════

    def _expire_stale_orders(self):
        expiry_hours = _safe_int(self._cfg.get("order_expiry_hours", "0"), 0)
        if expiry_hours <= 0:
            return
        try:
            stale = self.db.fetch_all("""
                SELECT sq.id, sq.order_id, sq.exchange_symbol, sq.symbol,
                       sq.live_trade_id, sq.shadow_trade_id, sq.account_id
                FROM shadow_queue sq
                WHERE sq.queue_status = 'placed'
                  AND sq.placed_at IS NOT NULL
                  AND TIMESTAMPDIFF(HOUR, sq.placed_at, NOW()) >= %s
            """, (expiry_hours,))
        except Exception:
            return
        for s in (stale or []):
            try:
                if s.get("order_id"):
                    ex = self._get_executor_for(s.get("account_id")) or self._executor
                    if ex:
                        ex.cancel_order(
                            s["order_id"], s.get("exchange_symbol", ""))
                if s.get("live_trade_id"):
                    self.db.execute(
                        "UPDATE live_trades SET status = 'cancelled', "
                        "close_comment = 'ShadowQ order expired' WHERE id = %s "
                        "AND status = 'pending'",
                        (s["live_trade_id"],))
                self._update_queue_status(s["id"], "expired")
                if s.get("shadow_trade_id"):
                    self.db.execute(
                        "UPDATE apex_shadow_trades SET placed_on_exchange = 0, "
                        "exchange_order_id = NULL, exchange_status = 'not_placed' "
                        "WHERE id = %s", (s["shadow_trade_id"],))
                self._log_activity("EXPIRE", s.get("symbol"), "INFO",
                                   f"Order expired after {expiry_hours}h unfilled")
            except Exception as e:
                logger.debug(f"[ShadowQ] Expire error for {s.get('symbol')}: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # FILL DETECTION (placed → filled transition)
    # ═══════════════════════════════════════════════════════════════════

    def _detect_fills(self):
        try:
            placed = self.db.fetch_all("""
                SELECT sq.id AS sq_id, sq.shadow_trade_id, sq.live_trade_id
                FROM shadow_queue sq
                JOIN live_trades lt ON lt.id = sq.live_trade_id
                WHERE sq.queue_status = 'placed'
                  AND lt.status IN ('open', 'partial_closed')
            """)
        except Exception:
            return
        for p in (placed or []):
            try:
                self.db.execute(
                    "UPDATE shadow_queue SET queue_status = 'filled', "
                    "filled_at = NOW() WHERE id = %s",
                    (p["sq_id"],))
                self.db.execute(
                    "UPDATE apex_shadow_trades SET exchange_status = 'open' WHERE id = %s",
                    (p["shadow_trade_id"],))
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════════════
    # EXCHANGE ORDER ROTATION
    # ═══════════════════════════════════════════════════════════════════

    def _rotate_exchange_orders(self):
        refresh_hours = _safe_int(self._cfg.get("exchange_order_refresh_hours", "4"), 4)
        try:
            stale = self.db.fetch_all("""
                SELECT sq.*, TIMESTAMPDIFF(HOUR, sq.placed_at, NOW()) AS hours_placed
                FROM shadow_queue sq
                WHERE sq.queue_status = 'placed'
                  AND sq.placed_at IS NOT NULL
                  AND TIMESTAMPDIFF(HOUR, sq.placed_at, NOW()) >= %s
            """, (refresh_hours,))
        except Exception:
            return
        if not stale:
            return

        for s in stale:
            try:
                better = self.db.fetch_one("""
                    SELECT id, priority_score FROM shadow_queue
                    WHERE symbol = %s AND direction = %s
                      AND queue_status = 'queued'
                      AND tradable_on_exchange = 1
                      AND priority_score > %s
                    ORDER BY priority_score DESC LIMIT 1
                """, (s["symbol"], s.get("direction", ""), float(s.get("priority_score") or 0)))

                if better:
                    if s.get("order_id"):
                        rot_ex = self._get_executor_for(s.get("account_id")) or self._executor
                        if rot_ex:
                            rot_ex.cancel_order(
                                s["order_id"], s.get("exchange_symbol", ""))
                    if s.get("live_trade_id"):
                        try:
                            self.db.execute(
                                "UPDATE live_trades SET status = 'cancelled', "
                                "close_comment = 'Rotated by ShadowQ' "
                                "WHERE id = %s AND status = 'pending'",
                                (s["live_trade_id"],))
                        except Exception:
                            pass
                    self._update_queue_status(s["id"], "queued")
                    if self._is_waterfall_enabled():
                        self.db.execute(
                            "UPDATE shadow_queue SET order_id = NULL, placed_at = NULL, "
                            "margin_allocated = NULL, live_trade_id = NULL, "
                            "account_id = NULL, exchange_name = NULL, exchange_symbol = NULL, "
                            "tradable_on_exchange = 0 WHERE id = %s",
                            (s["id"],))
                    else:
                        self.db.execute(
                            "UPDATE shadow_queue SET order_id = NULL, placed_at = NULL, "
                            "margin_allocated = NULL, live_trade_id = NULL WHERE id = %s",
                            (s["id"],))
                    if s.get("shadow_trade_id"):
                        try:
                            self.db.execute(
                                "UPDATE apex_shadow_trades "
                                "SET placed_on_exchange = 0, exchange_order_id = NULL, "
                                "exchange_status = 'not_placed' WHERE id = %s",
                                (s["shadow_trade_id"],))
                        except Exception:
                            pass
                    self._log_activity("REPLACE", s["symbol"], "INFO",
                                       f"Rotated stale order (was placed {s.get('hours_placed')}h ago)")
            except Exception as e:
                logger.debug(f"[ShadowQ] Rotate error for {s.get('symbol')}: {e}")

    def _enforce_margin_cap(self, max_margin: float, used_margin: float,
                            target_account_id: str = None):
        """When margin cap is reduced, cancel weakest ORDERS (not filled trades)
        until used margin fits under the new cap. Filled positions are left alone.
        If target_account_id is provided, only enforces for that account."""
        excess = used_margin - max_margin
        if excess <= 0:
            return

        try:
            if target_account_id:
                placed = self.db.fetch_all("""
                    SELECT id, symbol, order_id, exchange_symbol, margin_allocated,
                           priority_score, shadow_trade_id, live_trade_id, account_id
                    FROM shadow_queue
                    WHERE queue_status = 'placed'
                      AND margin_allocated > 0
                      AND account_id = %s
                    ORDER BY priority_score ASC
                """, (target_account_id,))
            else:
                placed = self.db.fetch_all("""
                    SELECT id, symbol, order_id, exchange_symbol, margin_allocated,
                           priority_score, shadow_trade_id, live_trade_id, account_id
                    FROM shadow_queue
                    WHERE queue_status = 'placed'
                      AND margin_allocated > 0
                    ORDER BY priority_score ASC
                """)
        except Exception:
            return
        if not placed:
            logger.info(f"[ShadowQ] Over margin cap by ${excess:.2f} but only filled "
                        "positions remain - waiting for trades to close")
            self._log_activity("MARGIN", None, "WARN",
                               f"Over cap by ${excess:.2f}, waiting for trades to close")
            return

        freed = 0.0
        for p in placed:
            if freed >= excess:
                break
            margin = _safe_float(p.get("margin_allocated"), 0)
            oid = p.get("order_id")
            esym = p.get("exchange_symbol", "")
            if oid:
                cap_ex = self._get_executor_for(p.get("account_id")) or self._executor
                if cap_ex:
                    try:
                        cap_ex.cancel_order(oid, esym)
                    except Exception as e:
                        logger.debug(f"[ShadowQ] Cancel order {oid} for cap enforcement: {e}")
            ltid = p.get("live_trade_id")
            if ltid:
                try:
                    self.db.execute(
                        "UPDATE live_trades SET status = 'cancelled', "
                        "close_comment = 'Margin cap reduced' "
                        "WHERE id = %s AND status = 'pending'", (ltid,))
                except Exception:
                    pass
            self._update_queue_status(p["id"], "margin_wait",
                                      skip_reason="margin cap reduced")
            stid = p.get("shadow_trade_id")
            if stid:
                try:
                    self.db.execute(
                        "UPDATE apex_shadow_trades SET placed_on_exchange = 0, "
                        "exchange_order_id = NULL, exchange_status = 'not_placed' "
                        "WHERE id = %s", (stid,))
                except Exception:
                    pass
            freed += margin
            self._log_activity("MARGIN_CAP", p["symbol"], "WARN",
                               f"Cancelled order to reduce margin (freed ${margin:.2f})")
            logger.info(f"[ShadowQ] Cancelled {p['symbol']} order to enforce margin cap "
                        f"(freed ${margin:.2f}, excess was ${excess:.2f})")

    # ═══════════════════════════════════════════════════════════════════
    # RECONCILIATION (THE CRITICAL PART)
    # ═══════════════════════════════════════════════════════════════════

    def _reconcile_closed_trades(self, target_shadow_id: int = None):
        if not self._reconcile_lock.acquire(blocking=False):
            logger.debug("[ShadowQ] Reconciliation already in progress, skipping")
            return
        try:
            base_sql = """
                SELECT lt.id AS lt_id, lt.shadow_trade_id, lt.actual_entry_price,
                       lt.actual_exit_price, lt.realised_pnl, lt.realised_pnl_pct,
                       lt.margin_usd, lt.filled_at, lt.closed_at,
                       lt.order_id, lt.exchange_symbol, lt.account_id,
                       lt.accrued_funding,
                       sq.id AS sq_id, sq.margin_allocated
                FROM live_trades lt
                JOIN shadow_queue sq ON sq.shadow_trade_id = lt.shadow_trade_id
                WHERE lt.trade_source = 'shadow'
                  AND lt.status = 'closed'
                  AND sq.queue_status IN ('placed', 'filled')
            """
            if target_shadow_id:
                closed = self.db.fetch_all(
                    base_sql + " AND lt.shadow_trade_id = %s",
                    (target_shadow_id,))
            else:
                closed = self.db.fetch_all(base_sql)
            if not closed:
                return

            for c in closed:
                shadow_id = c["shadow_trade_id"]
                try:
                    total_fees = 0.0
                    accrued_funding = float(c.get("accrued_funding") or 0)
                    target_order = c.get("order_id")
                    recon_ex = self._get_executor_for(c.get("account_id")) or self._executor
                    if recon_ex and c.get("exchange_symbol") and target_order:
                        try:
                            fills = recon_ex.get_recent_fills(
                                c["exchange_symbol"], limit=30)
                            if fills:
                                for f in fills:
                                    fill_oid = f.get("order") or f.get("order_id")
                                    if fill_oid != target_order:
                                        continue
                                    fee = f.get("fee", {})
                                    if isinstance(fee, dict):
                                        total_fees += float(fee.get("cost", 0) or 0)
                                    elif isinstance(fee, (int, float)):
                                        total_fees += float(fee)
                        except Exception as e:
                            logger.debug(f"[ShadowQ] Fee fetch failed: {e}")

                    margin = float(c.get("margin_allocated")
                                   or c.get("margin_usd") or 1)
                    forecast_pnl_pct = None
                    try:
                        shadow_row = self.db.fetch_one(
                            "SELECT counterfactual_pnl_pct FROM apex_shadow_trades WHERE id = %s",
                            (shadow_id,))
                        if shadow_row:
                            forecast_pnl_pct = shadow_row.get("counterfactual_pnl_pct")
                    except Exception:
                        pass

                    forecast_pnl_usd = None
                    if forecast_pnl_pct is not None:
                        forecast_pnl_usd = round(margin * float(forecast_pnl_pct) / 100, 4)

                    self.db.execute("""
                        UPDATE apex_shadow_trades SET
                            actual_entry_price = %s, actual_exit_price = %s,
                            actual_fees = %s, actual_funding = %s,
                            actual_pnl = %s, actual_pnl_pct = %s,
                            actual_entry_at = %s, actual_exit_at = %s,
                            exchange_status = 'closed',
                            counterfactual_pnl = COALESCE(counterfactual_pnl, %s)
                        WHERE id = %s
                    """, (
                        c.get("actual_entry_price"), c.get("actual_exit_price"),
                        round(total_fees, 6), round(accrued_funding, 6),
                        c.get("realised_pnl"), c.get("realised_pnl_pct"),
                        c.get("filled_at"), c.get("closed_at"),
                        forecast_pnl_usd,
                        shadow_id,
                    ))

                    self._update_queue_status(c["sq_id"], "closed")

                    self._log_activity("RECONCILE", None, "INFO",
                                       f"Shadow #{shadow_id} closed. "
                                       f"Actual P&L: {c.get('realised_pnl')}, "
                                       f"Fees: {total_fees:.4f}")
                    logger.info(f"[ShadowQ] Reconciled shadow #{shadow_id}: "
                                f"pnl={c.get('realised_pnl')}, fees={total_fees:.4f}")
                except Exception as e:
                    logger.error(f"[ShadowQ] Reconcile failed for shadow #{shadow_id}: {e}")
        finally:
            self._reconcile_lock.release()

    def notify_new_shadow(self, shadow_id: int):
        """Called by trade_dossier when a new shadow trade is saved.
        The next poll cycle will pick it up — this is just a log trigger."""
        logger.debug(f"[ShadowQ] Notified of new shadow #{shadow_id}")

    def reconcile_single(self, shadow_trade_id: int, live_trade_id: int):
        """Called by LiveTradeMonitor on shadow trade close — triggers
        immediate reconciliation for a specific trade."""
        self._reconcile_closed_trades(target_shadow_id=shadow_trade_id)

    # ═══════════════════════════════════════════════════════════════════
    # CONFIG HOT-RELOAD
    # ═══════════════════════════════════════════════════════════════════

    def _reload_config(self):
        now = time.time()
        if now - self._cfg_last_load < 5:
            return
        self._cfg_last_load = now
        old_cfg = dict(self._cfg)
        try:
            rows = self.db.fetch_all("SELECT config_key, config_value FROM shadow_config")
            if rows:
                self._cfg = {r["config_key"]: r["config_value"] for r in rows}
        except Exception as e:
            logger.debug(f"[ShadowQ] Config reload failed: {e}")
            return

        changed_keys = {k for k in self._cfg
                        if self._cfg.get(k) != old_cfg.get(k)}
        if not changed_keys:
            return

        if "margin_cap_pct" in changed_keys:
            old_cap = old_cfg.get("margin_cap_pct", "33")
            new_cap = self._cfg.get("margin_cap_pct", "33")
            logger.info(f"[ShadowQ] Margin cap changed: {old_cap}% -> {new_cap}%")
            self._log_activity("CONFIG", None, "INFO",
                               f"Margin cap changed from {old_cap}% to {new_cap}%")

        if "shadow_waterfall_enabled" in changed_keys:
            new_wf = self._cfg.get("shadow_waterfall_enabled", "false").strip().lower()
            if new_wf != "true":
                primary = self._cfg.get("account_id", "ShadowDemo")
                try:
                    rerouted = self.db.execute("""
                        UPDATE shadow_queue
                        SET account_id = %s, exchange_name = NULL,
                            exchange_symbol = NULL, tradable_on_exchange = 0
                        WHERE queue_status IN ('queued', 'margin_wait')
                          AND account_id IS NOT NULL AND account_id != %s
                    """, (primary, primary))
                    if rerouted:
                        logger.info(f"[ShadowQ] Waterfall OFF: re-routed {rerouted} "
                                    f"queue items to primary account {primary}")
                        self._log_activity("CONFIG", None, "WARN",
                                           f"Waterfall disabled: {rerouted} items re-routed to {primary}")
                except Exception as e:
                    logger.warning(f"[ShadowQ] Waterfall OFF re-route failed: {e}")

        refilter_keys = {"min_confidence", "allowed_models", "direction_filter", "blacklist",
                         "shadow_mentor_blacklist", "shadow_min_rr"}
        if changed_keys & refilter_keys:
            self._refilter_queue_on_config_change()

    def _refilter_queue_on_config_change(self):
        """Re-check queued/placed items against new config; cancel those that fail."""
        try:
            rows = self.db.fetch_all("""
                SELECT sq.id, sq.shadow_trade_id, sq.order_id, sq.exchange_symbol,
                       sq.queue_status, sq.live_trade_id, sq.symbol, sq.account_id,
                       ast.confidence_score, ast.model_used, ast.direction, ast.asset_class,
                       sq.entry_price, sq.stop_loss, sq.take_profit_1
                FROM shadow_queue sq
                JOIN apex_shadow_trades ast ON ast.id = sq.shadow_trade_id
                WHERE sq.queue_status IN ('queued', 'placed')
            """)
        except Exception:
            return
        cancelled = 0
        for r in (rows or []):
            passes, reason = self._passes_filter(r)
            if passes:
                continue
            try:
                if r.get("queue_status") == "placed":
                    if r.get("live_trade_id"):
                        lt_row = self.db.fetch_one(
                            "SELECT status FROM live_trades WHERE id = %s",
                            (r["live_trade_id"],))
                        lt_status = (lt_row or {}).get("status", "")
                        if lt_status in ("open", "partial_closed"):
                            logger.warning(
                                f"[ShadowQ] Refilter: skipping {r.get('symbol')} — "
                                f"live trade #{r['live_trade_id']} already {lt_status}")
                            continue
                    if r.get("order_id"):
                        rf_ex = self._get_executor_for(r.get("account_id")) or self._executor
                        if rf_ex:
                            rf_ex.cancel_order(
                                r["order_id"], r.get("exchange_symbol", ""))
                if r.get("live_trade_id"):
                    self.db.execute(
                        "UPDATE live_trades SET status = 'cancelled', "
                        "close_comment = 'Config change: no longer passes filter' "
                        "WHERE id = %s AND status = 'pending'",
                        (r["live_trade_id"],))
                if r.get("shadow_trade_id"):
                    self.db.execute(
                        "UPDATE apex_shadow_trades SET placed_on_exchange = 0, "
                        "exchange_order_id = NULL, exchange_status = 'not_placed' "
                        "WHERE id = %s AND exchange_status != 'closed'",
                        (r["shadow_trade_id"],))
                self._update_queue_status(r["id"], "cancelled",
                                          skip_reason=f"config_change: {reason}")
                cancelled += 1
            except Exception as e:
                logger.debug(f"[ShadowQ] Re-filter cancel error: {e}")
        if cancelled:
            self._log_activity("REFILTER", None, "WARN",
                               f"Config change: cancelled {cancelled} queue items")

    def update_blacklist_from_ev(self, lookback_days: int = 30,
                                 min_trades: int = 5,
                                 ev_threshold: float = -50.0) -> List[str]:
        cur = self.db
        rows = cur.fetch_all("""
            SELECT UPPER(symbol) AS sym,
                   COUNT(*) AS n,
                   SUM(CASE WHEN pnl >= 0 THEN 1 ELSE 0 END) AS wins,
                   COALESCE(SUM(pnl), 0) AS total_pnl
            FROM apex_shadow_trades
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND pnl IS NOT NULL
            GROUP BY UPPER(symbol)
            HAVING n >= %s
        """, (lookback_days, min_trades))
        if not rows:
            return []
        new_blacklist = []
        for r in rows:
            avg_pnl = float(r["total_pnl"] or 0) / int(r["n"])
            if avg_pnl < ev_threshold:
                new_blacklist.append(r["sym"])
        if not new_blacklist:
            return []
        blacklist_raw = self._cfg.get("blacklist", "[]")
        try:
            existing = [s.upper() for s in json.loads(blacklist_raw)]
        except Exception:
            existing = []
        merged = sorted(set(existing + new_blacklist))
        merged_json = json.dumps(merged)
        cur.execute(
            "UPDATE shadow_config SET config_value = %s WHERE config_key = 'blacklist'",
            (merged_json,))
        self._cfg["blacklist"] = merged_json
        self._log_activity("BLACKLIST_EV", None, "INFO",
                           f"Auto-blacklisted {new_blacklist} (EV < {ev_threshold})")
        return new_blacklist

    # ═══════════════════════════════════════════════════════════════════
    # MARGIN TRACKING
    # ═══════════════════════════════════════════════════════════════════

    def _get_margin_used(self, allow_exchange_call: bool = True) -> float:
        now = time.time()
        if self._cached_balance and now - self._cached_balance_at < 60:
            used = float(self._cached_balance.get("used", 0) or 0)
            if used > 0:
                return used
        if allow_exchange_call and self._executor:
            try:
                bal = self._executor.get_balance()
                self._cached_balance = bal
                self._cached_balance_at = time.time()
                used = float(bal.get("used", 0) or 0)
                if used > 0:
                    return used
            except Exception:
                pass
        try:
            row = self.db.fetch_one("""
                SELECT COALESCE(SUM(margin_allocated), 0) AS total
                FROM shadow_queue
                WHERE queue_status IN ('placed', 'filled')
            """)
            return float(row["total"]) if row else 0.0
        except Exception:
            return 0.0

    # ═══════════════════════════════════════════════════════════════════
    # ACTIVITY LOG
    # ═══════════════════════════════════════════════════════════════════

    def _log_activity(self, event_type: str, symbol: Optional[str],
                      severity: str, message: str, details: dict = None):
        entry = {
            "event_type": event_type, "symbol": symbol,
            "severity": severity, "message": message,
            "created_at": datetime.utcnow().isoformat(),
        }
        self._activity_buffer.append(entry)

        try:
            details_json = json.dumps(details, default=str) if details else None
            self.db.execute("""
                INSERT INTO shadow_activity_log
                (event_type, severity, symbol, message, details)
                VALUES (%s, %s, %s, %s, %s)
            """, (event_type, severity, symbol, message, details_json))
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════════════

    def _update_queue_status(self, queue_id: int, status: str,
                             skip_reason: str = None):
        try:
            if status == "cancelled":
                self.db.execute(
                    "UPDATE shadow_queue SET queue_status = %s, cancelled_at = NOW() WHERE id = %s",
                    (status, queue_id))
            elif status == "expired":
                self.db.execute(
                    "UPDATE shadow_queue SET queue_status = %s, expired_at = NOW() WHERE id = %s",
                    (status, queue_id))
            elif status in ("skipped", "missed", "margin_wait"):
                self.db.execute(
                    "UPDATE shadow_queue SET queue_status = %s, skip_reason = %s WHERE id = %s",
                    (status, skip_reason, queue_id))
            else:
                self.db.execute(
                    "UPDATE shadow_queue SET queue_status = %s WHERE id = %s",
                    (status, queue_id))
        except Exception as e:
            logger.debug(f"[ShadowQ] Status update failed for #{queue_id}: {e}")

    def _cancel_all_pending_orders(self):
        if not self._executor and not self._executors:
            return
        try:
            placed = self.db.fetch_all("""
                SELECT id, order_id, exchange_symbol, symbol, live_trade_id,
                       queue_status, account_id
                FROM shadow_queue WHERE queue_status IN ('placed', 'filled')
            """)
            for p in (placed or []):
                if p.get("queue_status") == "filled":
                    self._log_activity("OPEN_POSITION", p.get("symbol"), "WARN",
                                       "Open position left on exchange (not cancelled on stop)")
                    continue
                if p.get("order_id"):
                    try:
                        ex = self._get_executor_for(p.get("account_id")) or self._executor
                        if ex:
                            ex.cancel_order(
                                p["order_id"], p.get("exchange_symbol", ""))
                    except Exception:
                        pass
                self._update_queue_status(p["id"], "cancelled")
                if p.get("live_trade_id"):
                    try:
                        self.db.execute(
                            "UPDATE live_trades SET status = 'cancelled', "
                            "close_comment = 'ShadowQ shutdown' WHERE id = %s "
                            "AND status = 'pending'",
                            (p["live_trade_id"],))
                    except Exception:
                        pass
                self._log_activity("CANCEL", p.get("symbol"), "INFO",
                                   "Cancelled pending order on shutdown")
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════════
    # STATS FOR DASHBOARD API
    # ═══════════════════════════════════════════════════════════════════

    def get_queue_stats(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {
            "running": self._running,
            "cooloff_active": self._cooloff_active,
            "cooloff_remaining_seconds": 0,
            "cycle_count": self._cycle_count,
            "queue_size": 0, "active_orders": 0, "open_positions": 0,
            "completed_trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "forecast_pnl": 0, "actual_pnl": 0, "net_pnl": 0, "variance": 0,
            "total_fees": 0, "total_funding": 0, "fee_drag_pct": 0,
            "unrealised_pnl": 0, "equity": 0,
            "available_margin": 0, "margin_used": 0, "margin_cap_usd": 0,
            "margin_over_cap": False, "account_balance": 0,
        }
        epoch = _get_stats_epoch(self.db)

        if self._cooloff_active and self._cooloff_end:
            remaining = (self._cooloff_end - datetime.utcnow()).total_seconds()
            stats["cooloff_remaining_seconds"] = max(0, int(remaining))
            stats["cooloff_total_seconds"] = _safe_int(self._cfg.get("cooloff_minutes", "5"), 5) * 60

        try:
            counts = self.db.fetch_one("""
                SELECT
                    SUM(queue_status = 'queued') AS queue_size,
                    SUM(queue_status = 'placed') AS active_orders,
                    SUM(queue_status = 'filled') AS open_positions,
                    SUM(queue_status = 'missed') AS missed_count,
                    SUM(queue_status = 'margin_wait') AS margin_wait_count
                FROM shadow_queue
            """)
            if counts:
                stats["queue_size"] = int(counts.get("queue_size") or 0)
                stats["active_orders"] = int(counts.get("active_orders") or 0)
                stats["open_positions"] = int(counts.get("open_positions") or 0)
                stats["missed_count"] = int(counts.get("missed_count") or 0)
                stats["margin_wait_count"] = int(counts.get("margin_wait_count") or 0)
        except Exception:
            pass

        try:
            pnl_row = self.db.fetch_one("""
                SELECT
                    COUNT(*) AS completed,
                    SUM(CASE WHEN actual_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN actual_pnl < 0 THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(counterfactual_pnl), 0) AS forecast_pnl,
                    COALESCE(SUM(actual_pnl), 0) AS actual_pnl,
                    COALESCE(SUM(actual_fees), 0) AS total_fees,
                    COALESCE(SUM(actual_funding), 0) AS total_funding
                FROM apex_shadow_trades
                WHERE placed_on_exchange = 1 AND exchange_status = 'closed'
                  AND rejected_at >= %s
            """, (epoch,))
            if pnl_row:
                stats["completed_trades"] = int(pnl_row.get("completed") or 0)
                stats["wins"] = int(pnl_row.get("wins") or 0)
                stats["losses"] = int(pnl_row.get("losses") or 0)
                w = stats["wins"]
                total = w + stats["losses"]
                stats["win_rate"] = round(w / total * 100, 1) if total > 0 else 0
                stats["forecast_pnl"] = float(pnl_row.get("forecast_pnl") or 0)
                stats["actual_pnl"] = float(pnl_row.get("actual_pnl") or 0)
                stats["variance"] = round(stats["actual_pnl"] - stats["forecast_pnl"], 2)
                stats["total_fees"] = float(pnl_row.get("total_fees") or 0)
                stats["total_funding"] = float(pnl_row.get("total_funding") or 0)
                stats["net_pnl"] = round(
                    stats["actual_pnl"] - stats["total_fees"] - stats["total_funding"], 2)
                actual = stats["actual_pnl"]
                stats["fee_drag_pct"] = round(
                    stats["total_fees"] / abs(actual) * 100, 1) if actual else 0
        except Exception:
            pass

        try:
            fc_row = self.db.fetch_one("""
                SELECT
                    COUNT(*) AS total,
                    SUM(shadow_status = 'shadow_won') AS won,
                    SUM(shadow_status = 'shadow_lost') AS lost,
                    COALESCE(AVG(counterfactual_pnl_pct), 0) AS avg_pnl
                FROM apex_shadow_trades
                WHERE shadow_status IN ('shadow_won','shadow_lost')
                  AND placed_on_exchange = 0
                  AND rejected_at >= %s
            """, (epoch,))
            if fc_row:
                fc_w = int(fc_row.get("won") or 0)
                fc_l = int(fc_row.get("lost") or 0)
                fc_total = fc_w + fc_l
                stats["forecast_win_rate"] = round(
                    fc_w / fc_total * 100, 1) if fc_total > 0 else 0
                stats["forecast_wins"] = fc_w
                stats["forecast_losses"] = fc_l
                stats["forecast_total"] = fc_total
                stats["forecast_avg_pnl_pct"] = round(
                    float(fc_row.get("avg_pnl") or 0), 2)
        except Exception:
            pass

        waterfall_on = self._is_waterfall_enabled()

        if waterfall_on and self._cached_balances:
            total_eq = 0.0
            total_free = 0.0
            for wf_aid, wf_bal in dict(self._cached_balances).items():
                total_eq += float(wf_bal.get("total", 0) or 0)
                total_free += float(wf_bal.get("free", 0) or 0)
            stats["equity"] = round(total_eq, 2)
            stats["account_balance"] = round(total_eq, 2)
            stats["free_balance"] = round(total_free, 2)
        elif self._cached_balance_at > 0:
            stats["equity"] = self._cached_balance.get("total", 0)
            stats["account_balance"] = self._cached_balance.get("total", 0)
            stats["free_balance"] = self._cached_balance.get("free", 0)
        else:
            try:
                acc_row = self.db.fetch_one("""
                    SELECT cached_balance FROM trading_accounts
                    WHERE account_id = %s
                """, (self._cfg.get("account_id", "ShadowDemo"),))
                if acc_row and acc_row.get("cached_balance"):
                    stats["account_balance"] = float(acc_row["cached_balance"])
                    stats["equity"] = stats["account_balance"]
            except Exception:
                pass

        try:
            upnl_row = self.db.fetch_one("""
                SELECT COALESCE(SUM(lt.unrealised_pnl), 0) AS upnl
                FROM live_trades lt
                WHERE lt.trade_source = 'shadow'
                  AND lt.status IN ('open', 'partial_closed')
            """)
            if upnl_row:
                stats["unrealised_pnl"] = round(float(upnl_row["upnl"] or 0), 2)
        except Exception:
            pass

        if waterfall_on:
            total_used = 0.0
            total_max = 0.0
            margin_cap_default = _safe_float(self._cfg.get("margin_cap_pct", "33"), 33.0)
            for wf_acct in list(self._waterfall_accounts):
                wf_aid = wf_acct["account_id"]
                wf_bal = self._cached_balances.get(wf_aid, {})
                wf_total = float(wf_bal.get("total", 0) or 0)
                wf_cap = float(wf_acct.get("margin_cap_pct") or margin_cap_default)
                total_used += self._get_margin_used_for(wf_aid)
                total_max += wf_total * wf_cap / 100.0
            stats["margin_used"] = round(total_used, 2)
            stats["margin_cap_usd"] = round(total_max, 2)
            stats["available_margin"] = round(total_max - total_used, 2)
            stats["margin_over_cap"] = total_used > total_max
        else:
            primary_for_stats = self._cfg.get("account_id", "ShadowDemo")
            stats["margin_used"] = self._get_margin_used_for(primary_for_stats)
            margin_cap = _safe_float(self._cfg.get("margin_cap_pct", "33"), 33.0)
            max_margin = stats["account_balance"] * margin_cap / 100.0
            stats["available_margin"] = round(max_margin - stats["margin_used"], 2)
            stats["margin_cap_usd"] = round(max_margin, 2)
            stats["margin_over_cap"] = stats["margin_used"] > max_margin

        stats["by_model"] = {}
        stats["by_direction"] = {}
        stats["by_confidence_band"] = {}
        try:
            bd_rows = self.db.fetch_all("""
                SELECT
                    model_used,
                    direction,
                    CASE
                        WHEN confidence_score >= 80 THEN '80+'
                        WHEN confidence_score >= 70 THEN '70-79'
                        WHEN confidence_score >= 60 THEN '60-69'
                        ELSE '<60'
                    END AS conf_band,
                    COUNT(*) AS total,
                    SUM(CASE WHEN actual_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                    COALESCE(SUM(actual_pnl), 0) AS pnl,
                    COALESCE(SUM(actual_fees), 0) AS fees
                FROM apex_shadow_trades
                WHERE placed_on_exchange = 1 AND exchange_status = 'closed'
                  AND rejected_at >= %s
                GROUP BY model_used, direction, conf_band
            """, (epoch,))
            by_model: Dict[str, dict] = {}
            by_dir: Dict[str, dict] = {}
            by_band: Dict[str, dict] = {}
            for r in (bd_rows or []):
                m = r.get("model_used") or "unknown"
                d = r.get("direction") or "?"
                b = r.get("conf_band") or "?"
                total = int(r.get("total") or 0)
                wins = int(r.get("wins") or 0)
                pnl = float(r.get("pnl") or 0)
                fees = float(r.get("fees") or 0)
                for grp, key in [(by_model, m), (by_dir, d), (by_band, b)]:
                    if key not in grp:
                        grp[key] = {"total": 0, "wins": 0, "pnl": 0, "fees": 0}
                    grp[key]["total"] += total
                    grp[key]["wins"] += wins
                    grp[key]["pnl"] = round(grp[key]["pnl"] + pnl, 2)
                    grp[key]["fees"] = round(grp[key]["fees"] + fees, 4)
            for grp in (by_model, by_dir, by_band):
                for v in grp.values():
                    v["win_rate"] = round(v["wins"] / v["total"] * 100, 1) if v["total"] else 0
            stats["by_model"] = by_model
            stats["by_direction"] = by_dir
            stats["by_confidence_band"] = by_band
        except Exception as e:
            logger.debug(f"[ShadowQ] Breakdown stats error: {e}")

        return stats

    def get_config(self) -> Dict[str, str]:
        self._reload_config()
        return dict(self._cfg)

    def get_activity_log(self, limit: int = 100, epoch: str = None) -> List[Dict]:
        limit = max(1, min(limit, 500))
        ep = epoch or _get_stats_epoch(self.db)
        try:
            rows = self.db.fetch_all("""
                SELECT event_type, severity, symbol, message, created_at
                FROM shadow_activity_log
                WHERE created_at >= %s
                ORDER BY created_at DESC LIMIT %s
            """, (ep, limit))
            return rows or []
        except Exception:
            return list(self._activity_buffer)[-limit:]
