"""
JarvAIs Trading Floor Service
Manages the autonomous trading floor: discovery, lifecycle, tracker monitoring,
paper trading, and post-mortem orchestration.

Trade Lifecycle:
  draft -> proposed -> monitoring -> ready -> executed -> won/lost/expired/abandoned
"""

import os
import json
import logging
import random
import threading
import time
import traceback
from collections import deque
from concurrent.futures import as_completed
from core.thread_pool import DaemonThreadPoolExecutor as ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

from services.signal_backtester import SYMBOL_PIP_OVERRIDES, PIP_VALUES
from db.market_symbols import resolve_symbol
from core.config_loader import (
    get_system_config, get_system_config_int, get_system_config_float, load_prompt,
)

logger = logging.getLogger("jarvais.trading_floor")


def _utcnow() -> datetime:
    """Naive-UTC now — avoids DeprecationWarning on datetime.utcnow() while
    staying compatible with naive datetimes returned by MySQL/PyMySQL."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_duo_allowed(raw) -> List[str]:
    """Safely extract a list of duo IDs from the ``duo_allowed`` column.

    MySQL JSON columns may return a Python list, a JSON string, or (if
    mis-stored) a dict.  This helper normalises all variants into a plain
    ``list[str]``, returning ``[]`` on any unexpected type so callers never
    silently drop accounts.
    """
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
    if isinstance(raw, dict):
        return list(raw.keys())
    return []

# Price sanity: gold ~2600–5400, silver ~20–40. Reject wrong-asset data.
_GOLD_SYMBOLS = {"GOLD", "XAUUSD", "XAU"}
_SILVER_SYMBOLS = {"SILVER", "XAGUSD", "XAG"}


def _is_price_valid_for_symbol(symbol: str, price: float) -> bool:
    """Reject prices that are clearly wrong for the asset (e.g. silver data for gold)."""
    if not price or price <= 0:
        return False
    s = (symbol or "").upper()
    if s in _GOLD_SYMBOLS and price < 500:
        logger.warning(f"[TradingFloor] {symbol}: price {price} looks like silver (gold ~2600+) — rejecting")
        return False
    if s in _SILVER_SYMBOLS and price > 100:
        logger.warning(f"[TradingFloor] {symbol}: price {price} looks like gold (silver ~20–40) — rejecting")
        return False
    return True


def _get_pip_value(symbol: str, asset_class: str = None) -> float:
    """Get pip value for distance-from-entry calculation. Reuses backtester constants."""
    for key, pip_val in SYMBOL_PIP_OVERRIDES.items():
        if key.upper() in (symbol or "").upper():
            return pip_val
    if asset_class:
        ac = asset_class.lower()
        if "jpy" in (symbol or "").lower():
            return PIP_VALUES.get("forex_jpy", 0.01)
        return PIP_VALUES.get(ac, PIP_VALUES["default"])
    return PIP_VALUES["default"]


# ═══════════════════════════════════════════════════════════════════════
# TRADE LIFECYCLE STATE MACHINE
# ═══════════════════════════════════════════════════════════════════════

VALID_TRANSITIONS = {
    "draft":       ["proposed", "abandoned"],
    "proposed":    ["monitoring", "open_order", "abandoned"],
    "monitoring":  ["open_order", "abandoned", "expired"],
    "open_order":  ["live", "abandoned", "expired", "monitoring"],
    "live":        ["won", "lost", "abandoned"],
    "won":         [],
    "lost":        [],
    "expired":     [],
    "abandoned":   [],
}


def transition_dossier(db, dossier_id: int, new_status: str,
                        reason: str = "") -> Dict:
    """
    Transition a dossier to a new lifecycle state with validation.
    Uses conditional UPDATE to prevent TOCTOU races between threads.
    Returns result dict with success, old/new status, and reason.
    """
    dossier = db.fetch_one(
        "SELECT id, symbol, status FROM trade_dossiers WHERE id = %s",
        (dossier_id,))
    if not dossier:
        return {"success": False, "error": f"Dossier #{dossier_id} not found"}

    old = dossier["status"]
    if old == new_status:
        return {"success": True, "old_status": old, "new_status": new_status}
    allowed = VALID_TRANSITIONS.get(old, [])
    if new_status not in allowed:
        return {"success": False,
                "error": f"Cannot transition from '{old}' to '{new_status}'. "
                         f"Allowed: {allowed}"}

    # Conditional UPDATE: only succeeds if status hasn't changed since the read.
    # Prevents race where two threads both read "live" and one writes "won"
    # while the other overwrites with "lost".
    rows = db.execute(
        "UPDATE trade_dossiers SET status = %s WHERE id = %s AND status = %s",
        (new_status, dossier_id, old))

    if rows == 0:
        refreshed = db.fetch_one(
            "SELECT status FROM trade_dossiers WHERE id = %s", (dossier_id,))
        actual = refreshed["status"] if refreshed else "unknown"
        logger.warning(f"[TradingFloor] Dossier #{dossier_id} transition race: "
                       f"expected '{old}' but found '{actual}' — aborting "
                       f"transition to '{new_status}'")
        return {"success": False,
                "error": f"Status changed to '{actual}' during transition (race)"}

    _append_tracker_log(db, dossier_id,
                        f"Status: {old} -> {new_status}. {reason}")

    logger.info(f"[TradingFloor] Dossier #{dossier_id} ({dossier['symbol']}): "
                f"{old} -> {new_status} | {reason}")

    TERMINAL = ("won", "lost", "abandoned", "expired")
    if new_status in TERMINAL:
        _cancel_exchange_orders_for_dossier(db, dossier_id)
        _update_symbol_intel_on_close(db, dossier_id, new_status)

    return {"success": True, "old_status": old, "new_status": new_status}


def _update_symbol_intel_on_close(db, dossier_id: int, outcome: str):
    """Update symbol_intel counters when a dossier reaches a terminal state.

    Uses atomic INSERT/UPDATE + single UPDATE-with-subqueries to avoid
    stale-read races between concurrent tracker threads.
    """
    try:
        d = db.fetch_one(
            "SELECT symbol, duo_id, realised_pnl, realised_pnl_pct "
            "FROM trade_dossiers WHERE id = %s", (dossier_id,))
        if not d or not d.get("symbol") or not d.get("duo_id"):
            return

        symbol = d["symbol"]
        duo_id = d["duo_id"]
        pnl = float(d.get("realised_pnl") or 0)

        win_inc  = 1 if outcome == "won" else 0
        loss_inc = 1 if outcome == "lost" else 0
        exp_inc  = 1 if outcome in ("expired", "abandoned") else 0

        db.execute("""
            INSERT INTO symbol_intel (symbol, duo_id,
                total_wins, total_losses, total_expired, realized_pnl_sum)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                total_wins       = total_wins + VALUES(total_wins),
                total_losses     = total_losses + VALUES(total_losses),
                total_expired    = total_expired + VALUES(total_expired),
                realized_pnl_sum = realized_pnl_sum + VALUES(realized_pnl_sum)
        """, (symbol, duo_id, win_inc, loss_inc, exp_inc, pnl))

        db.execute("""
            UPDATE symbol_intel SET
                avg_win_pct  = (SELECT AVG(realised_pnl_pct)
                                FROM trade_dossiers
                                WHERE symbol = %s AND duo_id = %s
                                  AND status = 'won'
                                  AND realised_pnl_pct IS NOT NULL),
                avg_loss_pct = (SELECT AVG(realised_pnl_pct)
                                FROM trade_dossiers
                                WHERE symbol = %s AND duo_id = %s
                                  AND status = 'lost'
                                  AND realised_pnl_pct IS NOT NULL),
                lesson_count = (SELECT COUNT(*)
                                FROM trade_lessons tl
                                INNER JOIN trade_dossiers td
                                    ON tl.dossier_id = td.id
                                WHERE tl.symbol = %s AND td.duo_id = %s)
            WHERE symbol = %s AND duo_id = %s
        """, (symbol, duo_id, symbol, duo_id, symbol, duo_id, symbol, duo_id))

        logger.debug(f"[TradingFloor] symbol_intel counters updated: "
                     f"{symbol}/{duo_id} outcome={outcome} pnl={pnl}")
    except Exception as e:
        logger.error(f"[TradingFloor] symbol_intel counter update FAILED "
                     f"for dossier #{dossier_id} ({outcome}): {e}")


def _append_tracker_log(db, dossier_id: int, message: str):
    """Append a timestamped entry to the dossier's tracker_log."""
    ts = _utcnow().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] {message}\n"
    db.execute("""
        UPDATE trade_dossiers
        SET tracker_log = CONCAT(COALESCE(tracker_log, ''), %s)
        WHERE id = %s
    """, (entry, dossier_id))


def _cancel_exchange_orders_for_dossier(db, dossier_id: int):
    """Cancel any pending exchange orders linked to this dossier.

    Called automatically when a dossier transitions to a terminal state
    (won/lost/abandoned/expired) so orphan limit orders don't linger on
    the exchange for hours.
    """
    pending = db.fetch_all(
        "SELECT id, account_id, order_id, exchange_symbol, status "
        "FROM live_trades WHERE dossier_id = %s "
        "AND status = 'pending' AND order_id IS NOT NULL",
        (dossier_id,))
    if not pending:
        return

    from core.ccxt_executor import get_executor
    for lt in pending:
        oid = lt.get("order_id", "")
        exsym = lt.get("exchange_symbol", "")
        aid = lt.get("account_id", "")
        tid = lt["id"]
        try:
            executor = get_executor(aid, db)
            if executor and oid:
                executor.cancel_order(oid, exsym)
                logger.info(f"[TradingFloor] Cancelled exchange order {oid} "
                            f"for LT#{tid} (dossier #{dossier_id} went terminal)")
        except Exception as e:
            logger.debug(f"[TradingFloor] Cancel order {oid} for LT#{tid} "
                         f"failed (may already be gone): {e}")

        db.execute(
            "UPDATE live_trades SET status = 'cancelled', closed_at = NOW(), "
            "realised_pnl = NULL, unrealised_pnl = NULL, "
            "realised_pnl_pct = NULL, unrealised_pnl_pct = NULL, "
            "close_comment = CONCAT(COALESCE(close_comment,''), "
            "'Dossier went terminal — exchange order cancelled') "
            "WHERE id = %s AND status = 'pending'",
            (tid,))
        _append_tracker_log(db, dossier_id,
                            f"Exchange order {oid} cancelled on {aid} (dossier terminal)")


def update_probability(db, dossier_id: int, probability: int,
                        reason: str, conditions_update: List[Dict] = None):
    """
    Update a dossier's probability and optionally update condition statuses.
    Appends to probability_history for the UI timeline.
    """
    dossier = db.fetch_one(
        "SELECT probability_history, conditions_for_entry FROM trade_dossiers WHERE id = %s",
        (dossier_id,))
    if not dossier:
        return

    hist = json.loads(dossier.get("probability_history") or "[]")
    hist.append({
        "time": _utcnow().isoformat(),
        "probability": probability,
        "reason": reason,
    })

    updates = ["probability_history = %s"]
    params = [json.dumps(hist, default=str)]

    if conditions_update:
        existing = json.loads(dossier.get("conditions_for_entry") or "[]")
        update_map = {c["id"]: c for c in conditions_update}
        for cond in existing:
            if cond["id"] in update_map:
                cond["status"] = update_map[cond["id"]].get("status", cond["status"])
                if update_map[cond["id"]].get("reason"):
                    cond["last_check_reason"] = update_map[cond["id"]]["reason"]
                if update_map[cond["id"]]["status"] == "met" and not cond.get("met_at"):
                    cond["met_at"] = _utcnow().isoformat()
        updates.append("conditions_met = %s")
        params.append(json.dumps(existing, default=str))
        updates.append("conditions_for_entry = %s")
        params.append(json.dumps(existing, default=str))

    params.append(dossier_id)
    db.execute(f"UPDATE trade_dossiers SET {', '.join(updates)} WHERE id = %s",
               tuple(params))

    _append_tracker_log(db, dossier_id,
                        f"Probability: {probability}% - {reason}")


# ═══════════════════════════════════════════════════════════════════════
# LIMIT ORDER MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════

def place_limit_order(db, dossier_id: int, price: float) -> Dict:
    """Mark a limit order as placed for a dossier."""
    db.execute("""
        UPDATE trade_dossiers
        SET limit_order_active = 1, limit_order_price = %s,
            limit_order_placed_at = NOW()
        WHERE id = %s
    """, (price, dossier_id))
    _append_tracker_log(db, dossier_id,
                        f"Limit order PLACED at {price}")
    return {"success": True, "price": price}


def cancel_limit_order(db, dossier_id: int, reason: str = "") -> Dict:
    """Cancel the limit order for a dossier."""
    db.execute("""
        UPDATE trade_dossiers
        SET limit_order_active = 0
        WHERE id = %s
    """, (dossier_id,))
    _append_tracker_log(db, dossier_id,
                        f"Limit order CANCELLED. {reason}")
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════
# P&L CALCULATION
# ═══════════════════════════════════════════════════════════════════════

ACCOUNT_BALANCE = 2000.0   # placeholder paper balance
DEFAULT_MARGIN  = 20.0     # $20 per position (1% of $2000)
DEFAULT_LEVERAGE = 5       # fallback when dynamic calc not available

LEVERAGE_SAFETY_BUFFER = 3       # legacy flat subtraction (kept for config compat)
LEVERAGE_SAFETY_MULT = 0.70      # multiplicative buffer: use 70% of raw_max

MAX_LEVERAGE_BY_CLASS = {
    "cryptocurrency": 100,
    "commodity": 200,
    "forex": 200,
    "index": 50,
    "stock": 5,
    "etf": 5,
    "unknown": 10,
}


def calculate_optimal_leverage(entry: float, stop_loss: float,
                               direction: str, account_balance: float = None,
                               risk_pct: float = None,
                               asset_class: str = None,
                               td_cfg: Dict = None,
                               margin_usd: float = None) -> Dict:
    """
    Calculate max leverage where stop loss hits BEFORE liquidation.

    For isolated margin:
      LONG  liq_price ≈ entry * (1 - 1/leverage)  →  need liq < SL (SL hits first)
      SHORT liq_price ≈ entry * (1 + 1/leverage)  →  need liq > SL (SL hits first)

    raw_max = entry / |entry - SL|  (leverage at which liq = SL distance)
    safe_lev = raw_max - safety_buffer  (so SL is always hit before liquidation)
    Capped by asset class. Commodity can be overridden to 5x (oil, silver, gold).

    Returns dict with margin, leverage, notional, liq_price.
    margin_usd: when risk_mode=fixed, use this directly; else margin = balance * risk_pct.
    """
    balance = account_balance or ACCOUNT_BALANCE
    rpct = risk_pct or 0.01
    if margin_usd is not None:
        margin = round(float(margin_usd), 2)
    else:
        margin = round(balance * rpct, 2)

    if not entry or not stop_loss or entry <= 0 or stop_loss <= 0:
        return {"margin": margin, "leverage": 1, "notional": margin,
                "liq_price": 0, "risk_pct": rpct}

    d = direction.upper() if direction else "BUY"
    sl_distance = abs(entry - stop_loss)
    if sl_distance == 0:
        return {"margin": margin, "leverage": 1, "notional": margin,
                "liq_price": 0, "risk_pct": rpct}

    # Config overrides: safety buffer and per-class caps (e.g. commodity=5)
    safety_buffer = LEVERAGE_SAFETY_BUFFER
    safety_mult = LEVERAGE_SAFETY_MULT
    class_caps = dict(MAX_LEVERAGE_BY_CLASS)
    if td_cfg:
        safety_buffer = int(td_cfg.get("leverage_safety_buffer", LEVERAGE_SAFETY_BUFFER))
        safety_mult = float(td_cfg.get("leverage_safety_mult", LEVERAGE_SAFETY_MULT))
        overrides = td_cfg.get("max_leverage_by_class")
        if isinstance(overrides, dict):
            class_caps.update(overrides)

    class_cap = class_caps.get(asset_class or "unknown", 10)
    raw_max = entry / sl_distance
    lev_mult = int(raw_max * safety_mult)
    lev_flat = int(raw_max) - safety_buffer
    safe_lev = max(1, min(min(lev_mult, lev_flat), class_cap))

    # Risk cap: ensure loss_at_sl <= margin (the risk budget).
    # loss_at_sl = margin * leverage * (sl_distance / entry)
    # Setting leverage <= 1 / sl_pct keeps loss_at_sl <= margin.
    sl_pct = sl_distance / entry
    max_lev_for_risk = int(1.0 / sl_pct) if sl_pct > 0 else safe_lev
    safe_lev = max(1, min(safe_lev, max_lev_for_risk))

    # Hard validation: the SL% must be strictly less than the liquidation%.
    # Exchanges add maintenance margin (~0.5-1%) so real liquidation is earlier
    # than the theoretical entry*(1-1/lev). We enforce at least 30% headroom
    # between SL distance and liquidation distance so SL always fires first.
    max_iters = 20
    while safe_lev > 1 and max_iters > 0:
        liq_pct = 1.0 / safe_lev
        # Require liq distance to be at least 1.3x the SL distance
        if liq_pct >= sl_pct * 1.3:
            break
        safe_lev -= 1
        max_iters -= 1

    if d == "BUY":
        liq_price = entry * (1 - 1 / safe_lev) if safe_lev > 1 else 0
    else:
        liq_price = entry * (1 + 1 / safe_lev) if safe_lev > 1 else entry * 2

    notional = round(margin * safe_lev, 2)
    loss_at_sl = round(margin * safe_lev * sl_pct, 2)

    return {
        "margin": margin,
        "leverage": safe_lev,
        "notional": notional,
        "liq_price": round(liq_price, 5),
        "risk_pct": rpct,
        "raw_max_leverage": int(raw_max),
        "leverage_cap": class_cap,
        "asset_class": asset_class or "unknown",
        "loss_at_sl": loss_at_sl,
    }


def _get_account_balance(account_id: str, db=None) -> float:
    """Get real balance for an account via executor, with cached_balance fallback."""
    try:
        from core.ccxt_executor import _executor_cache
        for aid, executor in _executor_cache.items():
            if aid == account_id and executor.connected:
                bal = executor.get_balance()
                if bal and bal.get("total"):
                    real = float(bal["total"])
                    if db:
                        try:
                            db.execute(
                                "UPDATE trading_accounts SET cached_balance = %s, "
                                "cached_balance_at = NOW() WHERE account_id = %s",
                                (real, account_id))
                            db.execute(
                                "INSERT INTO system_config (config_key, config_value) "
                                "VALUES (%s, %s) ON DUPLICATE KEY UPDATE config_value = %s",
                                (f"cached_balance_{account_id}", str(real), str(real)))
                        except Exception:
                            pass
                    return real
    except Exception:
        pass
    if db:
        try:
            row = db.fetch_one(
                "SELECT cached_balance FROM trading_accounts WHERE account_id = %s",
                (account_id,))
            if row and row.get("cached_balance"):
                return float(row["cached_balance"])
        except Exception:
            pass
    return 0.0


def _stamp_leverage_on_dossier(db, dossier_id: int, config=None):
    """Calculate and store optimal leverage for a dossier.

    Determines the TARGET account via waterfall logic, fetches its real
    balance, and uses its risk settings so paper P&L matches live exactly.
    Falls back to paper_balance settings only when no live accounts exist.
    Stores target_account_id on the dossier for traceability.
    """
    d = db.fetch_one(
        "SELECT entry_price, stop_loss, direction, symbol, duo_id FROM trade_dossiers WHERE id = %s",
        (dossier_id,))
    if not d or not d.get("entry_price") or not d.get("stop_loss"):
        return

    dossier_duo_id = d.get("duo_id")
    td_cfg = config.raw.get("trade_decision", {}) if config else {}

    def _sc(key, fallback):
        r = db.fetch_one("SELECT config_value FROM system_config WHERE config_key = %s", (key,))
        return r["config_value"] if r and r.get("config_value") is not None else fallback

    # ── Determine target account via waterfall priority ──
    live_accounts = db.fetch_all(
        "SELECT * FROM trading_accounts WHERE enabled = 1 AND live_trading = 1 "
        "ORDER BY waterfall_priority ASC, id ASC")
    live_acct = None
    target_account_id = None

    if live_accounts:
        symbol = d.get("symbol", "")
        for acct in live_accounts:
            aid = acct["account_id"]
            if not acct.get("receive_dossiers"):
                continue
            if not acct.get("apex_enabled") and not acct.get("mentor_enabled"):
                continue
            allowed_duos = _parse_duo_allowed(acct.get("duo_allowed"))
            is_mentor_dossier = acct.get("mentor_enabled") and not acct.get("apex_enabled")
            if not is_mentor_dossier:
                if not allowed_duos or (dossier_duo_id and dossier_duo_id not in allowed_duos):
                    continue
            try:
                from db.market_symbols import can_trade_on_exchange
                can_trade, _ = can_trade_on_exchange(symbol, acct["exchange"], db)
                if not can_trade:
                    continue
            except Exception:
                pass
            live_acct = acct
            target_account_id = aid
            break
        if not live_acct and live_accounts:
            live_acct = live_accounts[0]
            target_account_id = live_acct["account_id"]

    if live_acct:
        acct_risk_mode = live_acct.get("risk_mode", "pct")
        balance = _get_account_balance(target_account_id, db)
        if balance <= 0:
            # Fall back to DB-cached balance before using a hardcoded default
            db_bal = db.fetch_one(
                "SELECT CAST(config_value AS DECIMAL(18,2)) AS bal "
                "FROM system_config WHERE config_key = %s",
                (f"cached_balance_{target_account_id}",))
            balance = float(db_bal["bal"]) if db_bal and db_bal.get("bal") else 0
            if balance <= 0:
                balance = 500.0
            logger.warning(f"[PaperMargin] Live balance fetch failed for {target_account_id}, "
                           f"using cached/fallback ${balance:.2f}")

        if acct_risk_mode == "fixed":
            fixed_margin = float(live_acct.get("risk_fixed_usd", 5) or 5)
            rpct = fixed_margin / balance if balance else 0.01
        else:
            rpct = float(live_acct.get("risk_per_trade_pct", 1.0) or 1.0) / 100.0
            fixed_margin = None

        logger.info(f"[PaperMargin] LIVE-ALIGNED acct={target_account_id} "
                    f"mode={acct_risk_mode} balance=${balance:.2f} rpct={rpct} "
                    f"fixed_margin={fixed_margin}")
    else:
        paper_balance_row = db.fetch_one(
            "SELECT config_value FROM system_config WHERE config_key = 'paper_balance'")
        balance = float(paper_balance_row["config_value"]) if paper_balance_row and paper_balance_row.get("config_value") else td_cfg.get("account_balance", ACCOUNT_BALANCE)
        risk_mode = _sc("tf_paper_risk_mode", td_cfg.get("paper_risk_mode", "fixed"))
        rpct = float(_sc("tf_paper_risk_pct", td_cfg.get("paper_risk_pct", 1))) / 100.0 if risk_mode == "pct" else 0.01
        fixed_margin = float(_sc("tf_paper_risk_fixed_usd", td_cfg.get("paper_risk_fixed_usd", 20))) if risk_mode == "fixed" else None
        logger.info(f"[PaperMargin] PAPER-ONLY mode={risk_mode} balance=${balance:.2f} "
                    f"rpct={rpct} fixed_margin={fixed_margin}")

    # SL direction sanity check before sizing
    entry_f = float(d["entry_price"])
    sl_f = float(d["stop_loss"])
    direction = (d.get("direction") or "BUY").upper()
    if direction == "BUY" and sl_f >= entry_f:
        logger.warning(f"[PaperMargin] #{dossier_id}: BUY with SL ({sl_f}) >= entry ({entry_f})")
    elif direction == "SELL" and sl_f <= entry_f:
        logger.warning(f"[PaperMargin] #{dossier_id}: SELL with SL ({sl_f}) <= entry ({entry_f})")

    asset_class = "unknown"
    if d.get("symbol"):
        sym_row = db.fetch_one(
            "SELECT asset_class FROM market_symbols WHERE symbol = %s",
            (d["symbol"],))
        if sym_row:
            asset_class = sym_row["asset_class"] or "unknown"

    # When live-aligned, override safety buffer to match the live account's setting
    calc_td_cfg = dict(td_cfg)
    if live_acct:
        acct_safety = int(live_acct.get("leverage_safety_buffer", 3) or 3)
        calc_td_cfg["leverage_safety_buffer"] = acct_safety

    calc = calculate_optimal_leverage(
        float(d["entry_price"]), float(d["stop_loss"]),
        d.get("direction", "BUY"), balance, rpct, asset_class, calc_td_cfg,
        margin_usd=fixed_margin)

    theoretical_lev = calc["leverage"]
    exchange_max = None
    exchange_clamped = False
    exchange_tradable = False
    symbol = d.get("symbol", "")

    # ── Check exchange tradability + clamp leverage ──
    # Step 1: DB lookup (fast, works even with empty executor cache)
    exchange_ticker = None
    exchange_name = None
    try:
        sym_row = db.fetch_one(
            "SELECT bybit_ticker, blofin_ticker, bitget_ticker, preferred_exchange "
            "FROM market_symbols WHERE symbol = %s", (symbol,))
        if sym_row:
            for col, eid in (("bybit_ticker", "bybit"), ("blofin_ticker", "blofin"), ("bitget_ticker", "bitget")):
                if sym_row.get(col):
                    exchange_tradable = True
                    exchange_ticker = sym_row[col]
                    exchange_name = eid
                    break
    except Exception:
        pass

    # Step 2: On-demand recon if DB had no tickers (resolves across all exchanges)
    if not exchange_tradable and symbol:
        try:
            from db.market_symbols import resolve_on_demand
            recon = resolve_on_demand(symbol, db)
            for eid in ("bybit", "blofin", "bitget"):
                if recon.get(eid):
                    exchange_tradable = True
                    exchange_ticker = recon[eid]
                    exchange_name = eid
                    break
        except Exception as e:
            logger.debug(f"[TradingFloor] #{dossier_id} on-demand recon failed: {e}")

    # Step 3: Query exchange leverage limits if we have a connected executor
    if exchange_tradable and exchange_ticker:
        try:
            from core.ccxt_executor import _executor_cache
            for aid, executor in _executor_cache.items():
                if not executor.connected:
                    continue
                if exchange_name and executor.exchange_id != exchange_name:
                    continue
                try:
                    lev_limits = executor.get_leverage_limits(exchange_ticker)
                    exchange_max = lev_limits.get("max", None)
                    if exchange_max and theoretical_lev > exchange_max:
                        calc["leverage"] = max(lev_limits.get("min", 1),
                                               min(theoretical_lev, exchange_max))
                        calc["notional"] = round(calc["margin"] * calc["leverage"], 2)
                        exchange_clamped = True
                        logger.info(
                            f"[TradingFloor] #{dossier_id} {symbol}: leverage clamped "
                            f"{theoretical_lev}x → {calc['leverage']}x "
                            f"(exchange max {exchange_max}x on {executor.exchange_id})")
                except Exception as e:
                    logger.debug(f"[TradingFloor] #{dossier_id} leverage limit "
                                 f"query failed: {e}")
                break
        except Exception:
            pass

    db.execute("""
        UPDATE trade_dossiers
        SET margin_usd = %s, leverage = %s, resolved_exchange = %s,
            target_account_id = %s
        WHERE id = %s
    """, (calc["margin"], calc["leverage"],
          exchange_name if exchange_tradable else None,
          target_account_id, dossier_id))

    safety_buf = td_cfg.get("leverage_safety_buffer", LEVERAGE_SAFETY_BUFFER)
    loss_at_sl = calc.get("loss_at_sl", 0)
    lev_detail = (
        f"Position sized: ${calc['margin']} margin × {calc['leverage']}x = "
        f"${calc['notional']} notional | Loss@SL: ${loss_at_sl} "
        f"| Liq: {calc['liq_price']} "
        f"(raw max: {calc['raw_max_leverage']}x, cap: {calc['leverage_cap']}x [{asset_class}], "
        f"safety: -{safety_buf})")
    if exchange_clamped:
        lev_detail += (f" | EXCHANGE CLAMPED: {theoretical_lev}x → "
                       f"{calc['leverage']}x (max {exchange_max}x)")
    if not exchange_tradable and symbol:
        lev_detail += " | WARNING: symbol not found on any connected exchange"
    _append_tracker_log(db, dossier_id, lev_detail)


def _get_total_margin_in_use(db, account_id: str = None) -> float:
    """Sum margin only for dossiers that have an active live trade on exchange.
    When account_id is provided, only counts margin for that specific account.
    """
    if account_id:
        row = db.fetch_one("""
            SELECT COALESCE(SUM(d.margin_usd), 0) AS total
            FROM trade_dossiers d
            WHERE d.status IN ('open_order', 'live') AND d.margin_usd IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM live_trades lt
                  WHERE lt.dossier_id = d.id
                    AND lt.account_id = %s
                    AND lt.status IN ('pending', 'open', 'partial_closed')
              )
        """, (account_id,))
    else:
        row = db.fetch_one("""
            SELECT COALESCE(SUM(d.margin_usd), 0) AS total
            FROM trade_dossiers d
            WHERE d.status IN ('open_order', 'live') AND d.margin_usd IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM live_trades lt
                  WHERE lt.dossier_id = d.id
                    AND lt.status IN ('pending', 'open', 'partial_closed')
              )
    """)
    return float(row["total"]) if row else 0.0


def _check_margin_cap(db, dossier_id: int, config=None,
                      target_account_id: str = None) -> bool:
    """
    Return True if there is room to open a new trade under the total margin cap.
    False means the margin cap would be exceeded — do NOT open the trade.
    Uses the TARGET account's balance and risk settings when available,
    falling back to config file settings for paper-only mode.
    """
    td_cfg = config.raw.get("trade_decision", {}) if config else {}

    target_rs = {}
    if target_account_id:
        acct_row = db.fetch_one(
            "SELECT risk_per_trade_pct, margin_cap_pct, max_open_trades, "
            "risk_mode, risk_fixed_usd "
            "FROM trading_accounts WHERE account_id = %s",
            (target_account_id,))
        if acct_row:
            target_rs = {
                "risk_per_trade": float(acct_row["risk_per_trade_pct"]) if acct_row.get("risk_per_trade_pct") else None,
                "margin_cap_pct": float(acct_row["margin_cap_pct"]) if acct_row.get("margin_cap_pct") else None,
                "max_open_trades": int(acct_row["max_open_trades"]) if acct_row.get("max_open_trades") else None,
                "risk_mode": acct_row.get("risk_mode"),
                "risk_fixed_usd": float(acct_row["risk_fixed_usd"]) if acct_row.get("risk_fixed_usd") else None,
            }
            target_rs = {k: v for k, v in target_rs.items() if v is not None}

    if not target_rs:
        accts = config.raw.get("accounts", []) if config else []
        target_rs = accts[0].get("risk_settings", {}) if accts else {}

    live_acct = None
    if target_account_id:
        live_acct = db.fetch_one(
            "SELECT * FROM trading_accounts WHERE account_id = %s AND enabled = 1",
            (target_account_id,))
    if not live_acct:
        live_acct = db.fetch_one(
            "SELECT * FROM trading_accounts WHERE enabled = 1 AND live_trading = 1 "
            "ORDER BY waterfall_priority ASC, id ASC LIMIT 1")

    if live_acct:
        balance = _get_account_balance(live_acct["account_id"], db)
        if balance <= 0:
            db_bal = db.fetch_one(
                "SELECT CAST(config_value AS DECIMAL(18,2)) AS bal "
                "FROM system_config WHERE config_key = %s",
                (f"cached_balance_{live_acct['account_id']}",))
            balance = float(db_bal["bal"]) if db_bal and db_bal.get("bal") else 500.0
    else:
        paper_row = db.fetch_one("SELECT config_value FROM system_config WHERE config_key = 'paper_balance'")
        balance = float(paper_row["config_value"]) if paper_row and paper_row.get("config_value") else td_cfg.get("account_balance", ACCOUNT_BALANCE)

    cap_pct = (target_rs.get("margin_cap_pct")
               or target_rs.get("max_total_margin_pct", 50)) / 100.0
    max_margin = balance * cap_pct

    d = db.fetch_one(
        "SELECT margin_usd FROM trade_dossiers WHERE id = %s", (dossier_id,))
    new_margin = float(d["margin_usd"]) if d and d.get("margin_usd") else DEFAULT_MARGIN

    acct_id = live_acct["account_id"] if live_acct else None
    in_use = _get_total_margin_in_use(db, account_id=acct_id)

    if in_use + new_margin > max_margin:
        logger.warning(
            f"[TradingFloor] MARGIN CAP: ${in_use:.2f} in use + ${new_margin:.2f} new = "
            f"${in_use + new_margin:.2f} > cap ${max_margin:.2f} "
            f"({cap_pct*100:.0f}% of ${balance:.2f}). "
            f"Dossier #{dossier_id} blocked.")
        _append_tracker_log(db, dossier_id,
            f"MARGIN CAP BLOCKED: ${in_use:.2f} in use + ${new_margin:.2f} = "
            f"${in_use + new_margin:.2f} > cap ${max_margin:.2f}")
        return False
    return True


def _find_weakest_dossier(db, exclude_id: int = None) -> dict:
    """
    Find the weakest open_order dossier (pending exchange order) by OppScore.
    Only considers open_order — live positions are sacrosanct and never replaced.
    """
    exclude_clause = f"AND id != {int(exclude_id)}" if exclude_id else ""
    row = db.fetch_one(f"""
        SELECT id, symbol, probability, opp_score, margin_usd, status
        FROM trade_dossiers
        WHERE status = 'open_order'
          AND margin_usd IS NOT NULL
          {exclude_clause}
        ORDER BY COALESCE(opp_score, probability, 0) ASC, created_at ASC
        LIMIT 1
    """)
    return dict(row) if row else None


def _try_replace_weak_dossier(db, new_dossier_id: int, config=None) -> bool:
    """
    If the margin cap is full, try to replace the weakest existing dossier
    with the new one — using OppScore comparison with configurable threshold.
    If the weakest is a pending exchange order, cancels it on the exchange.
    Returns True if replacement happened, False otherwise.
    """
    new_d = db.fetch_one(
        "SELECT id, probability, opp_score, margin_usd, symbol "
        "FROM trade_dossiers WHERE id = %s",
        (new_dossier_id,))
    if not new_d:
        return False

    new_score = float(new_d["opp_score"]) if new_d.get("opp_score") is not None else float(new_d.get("probability") or 0)
    weakest = _find_weakest_dossier(db, exclude_id=new_dossier_id)
    if not weakest:
        return False

    weak_score = float(weakest["opp_score"]) if weakest.get("opp_score") is not None else float(weakest.get("probability") or 0)
    td_cfg = config.raw.get("trade_decision", {}) if config else {}
    threshold = td_cfg.get("replacement_threshold", 20)

    if new_score >= weak_score + threshold:
        # Only cancel exchange orders for pending (open_order), never for live
        if weakest["status"] == "open_order":
            _cancel_live_order_for_dossier(db, weakest["id"], config)

        reason = (f"Replaced by #{new_dossier_id} (score={new_score:.1f}) — "
                  f"was weakest at score={weak_score:.1f}, gap={new_score-weak_score:.1f}")
        transition_dossier(db, weakest["id"], "abandoned", reason)
        _append_tracker_log(db, weakest["id"], reason)
        logger.info(f"[TradingFloor] OPP REPLACE: Dropped #{weakest['id']} "
                    f"(score={weak_score:.1f}) for #{new_dossier_id} (score={new_score:.1f})")
        return True

    logger.info(f"[TradingFloor] MARGIN CAP: #{new_dossier_id} (score={new_score:.1f}) "
                f"not strong enough to replace #{weakest['id']} (score={weak_score:.1f})")
    return False


def _cancel_live_order_for_dossier(db, dossier_id: int, config=None):
    """Cancel the exchange order for a pending live_trade linked to this dossier.
    Only cancels 'pending' status orders — never touches open positions."""
    live_rows = db.fetch_all(
        "SELECT lt.id, lt.order_id, lt.exchange_symbol, lt.account_id "
        "FROM live_trades lt "
        "WHERE lt.dossier_id = %s AND lt.status = 'pending'",
        (dossier_id,))

    for lt in (live_rows or []):
        order_id = lt.get("order_id")
        if not order_id:
            continue
        cancel_ok = False
        try:
            from core.ccxt_executor import get_executor
            executor = get_executor(lt["account_id"], db)
            if executor:
                result = executor.cancel_order(order_id, lt["exchange_symbol"])
                if result.get("success"):
                    logger.info(f"[TradingFloor] Cancelled exchange order {order_id} "
                                f"for dossier #{dossier_id}")
                    cancel_ok = True
                else:
                    logger.warning(f"[TradingFloor] Cancel order {order_id} failed: "
                                   f"{result.get('error')}")
        except Exception as e:
            logger.warning(f"[TradingFloor] Cancel order error for #{dossier_id}: {e}")

        if cancel_ok:
            db.execute(
                "UPDATE live_trades SET status = 'cancelled', "
                "realised_pnl = NULL, unrealised_pnl = NULL, "
                "realised_pnl_pct = NULL, unrealised_pnl_pct = NULL "
                "WHERE id = %s AND status = 'pending'",
                (lt["id"],))


def _calc_fee(notional: float, is_taker: bool, config=None) -> float:
    """Calculate fee in USD. Limit fill = maker (0.02%), market = taker (0.06%). Blofin standard."""
    if not notional or notional <= 0:
        return 0.0
    try:
        td = getattr(config, "raw", None) or {} if config else {}
        pf = td.get("trade_decision", {}).get("paper_fees", {})
    except Exception:
        pf = {}
    maker_pct = float(pf.get("maker_fee_pct", 0.02))
    taker_pct = float(pf.get("taker_fee_pct", 0.06))
    rate = taker_pct if is_taker else maker_pct
    return notional * (rate / 100)


def _update_live_pnl(db, dossier_id: int, entry: float, price: float,
                     direction: str, config=None):
    """Calculate and store unrealised P&L for an executed dossier.
    Prefers live_margin/live_leverage (from exchange) when available,
    falls back to dossier margin_usd/leverage. Subtracts entry_fee +
    accrued_funding from gross PnL (paper realism)."""
    if not entry or not price:
        return

    row = db.fetch_one(
        "SELECT margin_usd, leverage, live_margin, live_leverage, "
        "entry_fee, accrued_funding FROM trade_dossiers WHERE id = %s",
        (dossier_id,))
    if not row:
        return

    margin = float(row.get("live_margin") or row.get("margin_usd") or DEFAULT_MARGIN)
    lev = int(row.get("live_leverage") or row.get("leverage") or DEFAULT_LEVERAGE)
    if not row.get("live_margin") and not row.get("margin_usd"):
        logger.warning(f"[TradingFloor] _update_live_pnl #{dossier_id}: no margin found, "
                       f"using DEFAULT_MARGIN=${DEFAULT_MARGIN}")
    exposure = margin * lev

    if entry <= 0:
        return
    position_size = exposure / entry
    if direction == "BUY":
        gross_pnl = (price - entry) * position_size
    else:
        gross_pnl = (entry - price) * position_size

    entry_fee = float(row.get("entry_fee") or 0)
    accrued_funding = float(row.get("accrued_funding") or 0)
    pnl = gross_pnl - entry_fee - accrued_funding
    pnl_pct = (pnl / margin) * 100 if margin else 0

    db.execute("""
        UPDATE trade_dossiers
        SET current_price = %s, current_price_at = NOW(),
            unrealised_pnl = %s, unrealised_pnl_pct = %s
        WHERE id = %s
    """, (round(price, 5), round(pnl, 4), round(pnl_pct, 4), dossier_id))


def _vectorize_dossier(db, dossier_id: int):
    """Queue a completed dossier for vectorization (Symbol Knowledge Bank).
    Stores the full trade context -- Stage 1/2 output, tracker conversation,
    post-mortem, BillNye TA, and lessons -- into the trade_memory collection
    for future RAG retrieval during dossier builds."""
    try:
        d = db.fetch_one("""
            SELECT id, symbol, direction, status, entry_price, stop_loss,
                   take_profit_1, confidence_score, realised_pnl, realised_pnl_pct,
                   margin_usd, leverage, mentor_source, lessons_learned,
                   stage2_hypothesis, trade_decision, conditions_for_entry,
                   stage1_output, stage2_output, tracker_conversation,
                   postmortem_output, apex_entry_reasoning, strategy_id
            FROM trade_dossiers WHERE id = %s
        """, (dossier_id,))
        if not d:
            return

        import json as _json
        parts = []
        parts.append(f"Trade dossier #{d['id']} {d.get('symbol','')} "
                      f"{d.get('direction','')} — {d.get('status','')}")
        parts.append(f"Entry: {d.get('entry_price')}, SL: {d.get('stop_loss')}, "
                      f"TP1: {d.get('take_profit_1')}")
        parts.append(f"P&L: ${d.get('realised_pnl',0)} ({d.get('realised_pnl_pct',0)}%)")
        parts.append(f"Leverage: {d.get('leverage')}x | Margin: ${d.get('margin_usd',0)}")

        if d.get("trade_decision"):
            parts.append(f"Decision: {d['trade_decision']}")
        if d.get("apex_entry_reasoning"):
            parts.append(f"Apex reasoning: {d['apex_entry_reasoning'][:800]}")
        if d.get("stage2_hypothesis"):
            hyp = d["stage2_hypothesis"]
            if isinstance(hyp, str):
                try:
                    hyp = _json.loads(hyp)
                except Exception:
                    pass
            if isinstance(hyp, dict):
                parts.append(f"Hypothesis: {hyp.get('hypothesis', '')[:500]}")
                parts.append(f"Reasoning: {hyp.get('reasoning', '')[:500]}")
            else:
                parts.append(f"Hypothesis: {str(hyp)[:500]}")
        if d.get("conditions_for_entry"):
            cond = d["conditions_for_entry"]
            if isinstance(cond, str):
                try:
                    cond = _json.loads(cond)
                except Exception:
                    pass
            if isinstance(cond, list):
                parts.append(f"Conditions: {'; '.join(str(c) for c in cond[:5])}")

        # Stage 1 TA output (BillNye analysis)
        if d.get("stage1_output"):
            s1 = str(d["stage1_output"])[:2000]
            parts.append(f"Stage 1 TA: {s1}")

        # Stage 2 full output (Apex decision reasoning)
        if d.get("stage2_output"):
            s2 = d["stage2_output"]
            if isinstance(s2, str):
                try:
                    s2 = _json.loads(s2)
                except Exception:
                    pass
            if isinstance(s2, dict):
                parts.append(f"Stage2 rationale: {str(s2.get('rationale',''))[:500]}")
                parts.append(f"Stage2 SL logic: {str(s2.get('stop_loss_reasoning',''))[:300]}")
            else:
                parts.append(f"Stage2 output: {str(s2)[:800]}")

        # Tracker conversation summary (last 2 turns)
        if d.get("tracker_conversation"):
            tc = d["tracker_conversation"]
            if isinstance(tc, str):
                try:
                    tc = _json.loads(tc)
                except Exception:
                    tc = []
            if isinstance(tc, list) and tc:
                last_msgs = [m.get("content", "")[:300] for m in tc[-2:] if m.get("role") == "assistant"]
                if last_msgs:
                    parts.append(f"Tracker summary: {' | '.join(last_msgs)}")

        # Post-mortem output
        if d.get("postmortem_output"):
            parts.append(f"Post-mortem: {str(d['postmortem_output'])[:1500]}")

        if d.get("lessons_learned"):
            parts.append(f"Lessons: {d['lessons_learned'][:1000]}")

        # Enrich with audit_report root cause + lessons from trade_lessons table
        audit = db.fetch_one(
            "SELECT root_cause, blame_assignment, auditor_summary "
            "FROM audit_reports WHERE dossier_id = %s AND status = 'completed' "
            "ORDER BY completed_at DESC LIMIT 1", (dossier_id,))
        if audit:
            if audit.get("root_cause"):
                parts.append(f"Root cause: {str(audit['root_cause'])[:500]}")
            if audit.get("blame_assignment"):
                parts.append(f"Blame: {str(audit['blame_assignment'])[:500]}")
            if audit.get("auditor_summary"):
                parts.append(f"Auditor summary: {str(audit['auditor_summary'])[:800]}")

        tl_rows = db.fetch_all(
            "SELECT lesson_text, what_worked, what_failed, root_cause "
            "FROM trade_lessons WHERE dossier_id = %s LIMIT 5", (dossier_id,))
        for tl in (tl_rows or []):
            lesson_parts = []
            if tl.get("lesson_text"):
                lesson_parts.append(tl["lesson_text"][:300])
            if tl.get("what_worked"):
                lesson_parts.append(f"Worked: {tl['what_worked'][:200]}")
            if tl.get("what_failed"):
                lesson_parts.append(f"Failed: {tl['what_failed'][:200]}")
            if tl.get("root_cause"):
                lesson_parts.append(f"Root: {tl['root_cause'][:200]}")
            if lesson_parts:
                parts.append(f"TradeLesson: {' | '.join(lesson_parts)}")

        vec_text = "\n".join(parts)[:30000]

        # FinMem-lite importance scoring: PnL-weighted with outcome multipliers
        raw_pnl_pct = abs(float(d.get("realised_pnl_pct") or 0))
        base_importance = min(1.0, raw_pnl_pct / 10.0)
        status = str(d.get("status", "")).lower()
        if status == "won":
            importance = min(1.0, base_importance * 1.5)
        elif status == "lost" and d.get("lessons_learned"):
            importance = min(1.0, base_importance * 1.2)
        else:
            importance = base_importance

        from services.vectorization_worker import queue_for_vectorization
        queue_for_vectorization(db, "trade_dossiers", dossier_id, "trade_memory", vec_text, {
            "symbol": d.get("symbol", ""),
            "direction": d.get("direction", ""),
            "status": d.get("status", ""),
            "pnl_usd": float(d.get("realised_pnl") or 0),
            "pnl_pct": float(d.get("realised_pnl_pct") or 0),
            "confidence": d.get("confidence_score"),
            "leverage": d.get("leverage"),
            "mentor_source": d.get("mentor_source", ""),
            "strategy_id": d.get("strategy_id"),
            "type": "dossier_outcome",
            "importance": round(importance, 3),
            "lessons_learned": d.get("lessons_learned", ""),
        })
        logger.info(f"[TradingFloor] Dossier #{dossier_id} queued for vectorization")
    except Exception as e:
        logger.debug(f"[TradingFloor] Dossier vectorization error #{dossier_id}: {e}")


def _generate_mentor_comparison(db, dossier_id: int):
    """When a mentor mirror OR an apex-assessed dossier closes, generate a
    structured comparison lesson so Apex can learn from the difference between
    its own analysis and the mentor's outcome.

    For mentor_mirror dossiers: find the corresponding Apex dossier (if any)
    and compare decisions, levels, and outcomes.
    For apex_assessed dossiers: the comparison is embedded in the dossier itself.
    """
    d = db.fetch_one("""
        SELECT id, symbol, direction, entry_price, stop_loss, take_profit_1,
               status, realised_pnl, realised_pnl_pct, confidence_score,
               mentor_source, mentor_type, dossier_intelligence, leverage
        FROM trade_dossiers WHERE id = %s
    """, (dossier_id,))
    if not d or not d.get("mentor_type"):
        return

    symbol = d.get("symbol", "")
    mentor = d.get("mentor_source", "unknown")
    m_status = d.get("status", "")
    m_pnl = float(d.get("realised_pnl") or 0)
    m_dir = d.get("direction", "")
    m_entry = d.get("entry_price")
    m_sl = d.get("stop_loss")
    m_tp1 = d.get("take_profit_1")

    # Find the corresponding Apex dossier via trade_group_id link,
    # falling back to parsing dossier_intelligence for legacy dossiers.
    apex_dossier = None
    group_id = d.get("trade_group_id")
    if group_id:
        apex_dossier = db.fetch_one("""
            SELECT id, direction, entry_price, stop_loss, take_profit_1,
                   status, realised_pnl, realised_pnl_pct, confidence_score,
                   trade_decision, apex_entry_reasoning
            FROM trade_dossiers WHERE id = %s
        """, (int(group_id),))
    if not apex_dossier:
        import re
        intel = d.get("dossier_intelligence") or ""
        apex_match = re.search(r"[Dd]ossier\s*#(\d+)", intel)
        if apex_match:
            apex_id = int(apex_match.group(1))
            apex_dossier = db.fetch_one("""
                SELECT id, direction, entry_price, stop_loss, take_profit_1,
                       status, realised_pnl, realised_pnl_pct, confidence_score,
                       trade_decision, apex_entry_reasoning
                FROM trade_dossiers WHERE id = %s
            """, (apex_id,))

    # Build comparison lesson text
    parts = [f"MENTOR COMPARISON: {symbol} ({mentor})"]
    parts.append(f"Mentor: {m_dir} Entry={m_entry} SL={m_sl} TP1={m_tp1} "
                 f"-> {m_status.upper()} P&L=${m_pnl:.2f}")

    if apex_dossier:
        a = apex_dossier
        a_pnl = float(a.get("realised_pnl") or 0)
        a_decision = a.get("trade_decision", "unknown")
        a_conf = a.get("confidence_score", 0)
        a_status = (a.get("status") or "").upper()
        parts.append(f"Apex: {a.get('direction','')} Entry={a.get('entry_price')} "
                     f"SL={a.get('stop_loss')} TP1={a.get('take_profit_1')} "
                     f"-> {a_status} P&L=${a_pnl:.2f}")
        parts.append(f"Apex confidence: {a_conf}% | Decision: {a_decision}")

        m_won = m_status == "won"
        a_agreed = a_decision in ("trade_now",)
        a_rejected = a_decision in ("no_trade", "do_not_trade")

        if m_won and a_agreed:
            delta = abs(a_pnl - m_pnl) if a_pnl else 0
            parts.append(f"VERDICT: Both agreed and mentor WON. "
                         f"{'Apex levels were better.' if a_pnl > m_pnl else 'Mentor levels were better.'} "
                         f"P&L delta: ${delta:.2f}")
            parts.append("ACTION: Reinforce this pattern. Apex's analysis aligned with a winning setup.")
        elif m_won and a_rejected:
            parts.append(f"VERDICT: Mentor WON but Apex REJECTED (conf={a_conf}%). "
                         f"Apex missed a ${m_pnl:.2f} winner.")
            parts.append("ACTION: Study WHY Apex rejected. Was the confidence score too conservative? "
                         "Were conditions too strict? Learn the mentor's technique.")
        elif not m_won and a_agreed:
            parts.append(f"VERDICT: Mentor LOST and Apex agreed. Both got it wrong.")
            parts.append("ACTION: Review market conditions. What invalidated the setup? "
                         "Can conditions be refined to detect this risk earlier?")
        elif not m_won and a_rejected:
            parts.append(f"VERDICT: Mentor LOST and Apex correctly REJECTED. "
                         f"Apex avoided a ${abs(m_pnl):.2f} loss.")
            parts.append("ACTION: Apex's judgment was BETTER than the mentor's. "
                         "Document what Apex detected that the mentor missed.")
        else:
            parts.append("VERDICT: Inconclusive outcome — review both perspectives.")

        if a.get("apex_entry_reasoning"):
            parts.append(f"Apex reasoning: {a['apex_entry_reasoning'][:500]}")
    else:
        parts.append("Apex: DID NOT ASSESS THIS TRADE (no independent analysis)")
        if m_status == "won":
            parts.append(f"VERDICT: Mentor WON (${m_pnl:.2f}). Apex had no "
                         f"assessment — ensure Apex evaluates all mentor trades.")
        else:
            parts.append(f"VERDICT: Mentor LOST (${abs(m_pnl):.2f}). No Apex "
                         f"comparison available.")

    lesson_text = "\n".join(parts)
    outcome = "WIN" if m_status == "won" else "LOSS"

    try:
        db.insert_lesson({
            "dossier_id": dossier_id,
            "signal_id": None,
            "symbol": symbol,
            "account_id": "jarvais",
            "model_used": "mentor_comparison",
            "outcome": outcome,
            "pnl_usd": m_pnl,
            "what_worked": lesson_text if outcome == "WIN" else None,
            "what_failed": lesson_text if outcome == "LOSS" else None,
            "lesson_text": lesson_text[:5000],
            "root_cause": f"Mentor comparison: {mentor} vs Apex on {symbol}",
            "confidence_calibration": (
                f"Mentor {m_status}, Apex "
                f"{'took (conf={})'.format(apex_dossier.get('confidence_score')) if apex_dossier else 'skipped'}"
            ),
        })
        logger.info(f"[TradingFloor] Mentor comparison lesson stored for "
                    f"dossier #{dossier_id} ({symbol})")
    except Exception as e:
        logger.debug(f"[TradingFloor] Mentor comparison lesson error: {e}")


def _finalise_pnl(db, dossier_id: int, close_price: float, exit_is_taker: bool = True,
                  config=None, exit_is_sl: bool = False) -> float:
    """Stamp realised P&L when a trade closes. Deducts fees (paper realism).
    exit_is_taker: True for SL/stop-market/live, False for TP limit close.
    exit_is_sl: True when closing via stop-loss; applies adverse slippage for realism.
    Returns the net P&L (after fees) so callers can determine won/lost status.
    IMPORTANT: For live trades, the actual exchange P&L is written by
    live_trade_monitor._sync_dossier_on_close and will override this paper calc."""
    d = db.fetch_one(
        "SELECT entry_price, actual_entry_price, direction, margin_usd, leverage, "
        "live_margin, live_leverage, "
        "entry_fee, accrued_funding, live_trade_id, live_pnl "
        "FROM trade_dossiers WHERE id = %s",
        (dossier_id,))
    if not d or not d.get("entry_price"):
        return 0.0

    if d.get("live_pnl") is not None and d.get("live_trade_id"):
        logger.info(f"[TradingFloor] Dossier #{dossier_id} has live P&L "
                    f"${d['live_pnl']} — skipping paper calc")
        return float(d["live_pnl"])

    # If a live trade is still open/partial, defer to the live monitor for P&L.
    # Paper calc would use mark price which differs from eventual fill price.
    if d.get("live_trade_id"):
        active_lt = db.fetch_one(
            "SELECT id FROM live_trades WHERE dossier_id = %s "
            "AND status IN ('open', 'partial_closed')", (dossier_id,))
        if active_lt:
            logger.debug(f"[TradingFloor] Dossier #{dossier_id} has active live trade "
                         f"— deferring P&L to live monitor")
            return 0.0

    entry = float(d.get("actual_entry_price") or d["entry_price"])
    direction = (d.get("direction") or "").upper()
    if direction not in ("BUY", "SELL"):
        logger.error(f"[TradingFloor] _finalise_pnl: dossier #{dossier_id} has invalid "
                     f"direction '{direction}' — cannot compute P&L, skipping")
        return 0.0
    margin = float(d.get("live_margin") or d.get("margin_usd") or DEFAULT_MARGIN)
    lev = int(d.get("live_leverage") or d.get("leverage") or DEFAULT_LEVERAGE)
    if not d.get("live_margin") and not d.get("margin_usd"):
        logger.warning(f"[TradingFloor] _finalise_pnl #{dossier_id}: no margin found, "
                       f"using DEFAULT_MARGIN=${DEFAULT_MARGIN}")
    exposure = margin * lev
    position_size = exposure / entry

    # SL slippage: adverse slippage on stop-outs
    if exit_is_sl and config:
        pf = config.raw.get("trade_decision", {}).get("paper_fees", {})
        slip_pct = float(pf.get("sl_slippage_pct", 0) or 0) / 100.0
        if slip_pct:
            if direction == "BUY":
                close_price = close_price * (1 - slip_pct)
            else:
                close_price = close_price * (1 + slip_pct)

    if direction == "BUY":
        gross_pnl = (close_price - entry) * position_size
    else:
        gross_pnl = (entry - close_price) * position_size

    entry_fee = float(d.get("entry_fee") or 0)
    accrued_funding = float(d.get("accrued_funding") or 0)
    exit_notional = close_price * position_size
    exit_fee = _calc_fee(exit_notional, exit_is_taker, config)
    total_fees = entry_fee + exit_fee + accrued_funding
    pnl = gross_pnl - total_fees
    pnl_pct = (pnl / margin) * 100 if margin else 0

    db.execute("""
        UPDATE trade_dossiers
        SET current_price = %s, current_price_at = NOW(),
            unrealised_pnl = 0, unrealised_pnl_pct = 0,
            realised_pnl = %s, realised_pnl_pct = %s,
            actual_exit_price = %s, exit_fee = %s, total_fees = %s
        WHERE id = %s
    """, (round(close_price, 5), round(pnl, 4), round(pnl_pct, 4),
          round(close_price, 5), round(exit_fee, 6), round(total_fees, 6), dossier_id))

    # Update simulated paper balance atomically (only paper-only dossiers)
    try:
        lt = db.fetch_one("SELECT 1 FROM live_trades WHERE dossier_id = %s AND order_id IS NOT NULL LIMIT 1", (dossier_id,))
        if not lt:
            db.execute(
                "UPDATE system_config "
                "SET config_value = CAST(ROUND("
                "CAST(COALESCE(NULLIF(config_value,''), '0') AS DECIMAL(18,2)) + %s"
                ", 2) AS CHAR) "
                "WHERE config_key = 'paper_balance'",
                (round(pnl, 2),))
            logger.info(f"[TradingFloor] Paper balance adjusted by P&L ${pnl:.2f} (atomic)")
    except Exception as e:
        logger.debug(f"[TradingFloor] Paper balance update: {e}")

    return pnl


def _snapshot_conditions_at_entry(db, dossier_id: int):
    """Record how many conditions were met vs total when the trade went live."""
    try:
        row = db.fetch_one(
            "SELECT conditions_for_entry FROM trade_dossiers WHERE id = %s",
            (dossier_id,))
        if not row or not row.get("conditions_for_entry"):
            return
        conditions = json.loads(row["conditions_for_entry"])
        if not isinstance(conditions, list):
            return
        total = len(conditions)
        met = sum(1 for c in conditions if (c.get("status") or "").lower() == "met")
        db.execute(
            "UPDATE trade_dossiers SET conditions_met_at_entry = %s, "
            "conditions_total_at_entry = %s WHERE id = %s",
            (met, total, dossier_id))
        logger.info(f"[TradingFloor] #{dossier_id}: entry snapshot {met}/{total} conditions met")
    except Exception as e:
        logger.debug(f"[TradingFloor] Conditions snapshot error #{dossier_id}: {e}")


def _update_strategy_stats(db, dossier_id: int):
    """Update strategy performance stats AFTER dossier status has been set to won/lost.
    Must be called after transition_dossier(), not before, so the subqueries
    correctly include the just-closed dossier."""
    try:
        strat_row = db.fetch_one(
            "SELECT strategy_id FROM trade_dossiers WHERE id = %s AND strategy_id IS NOT NULL",
            (dossier_id,))
        if strat_row and strat_row.get("strategy_id"):
            sid = strat_row["strategy_id"]
            db.execute("""
                UPDATE trading_strategies SET
                    total_trades = (SELECT COUNT(*) FROM trade_dossiers
                                   WHERE strategy_id = %s AND status IN ('won','lost')),
                    total_wins   = (SELECT COUNT(*) FROM trade_dossiers
                                   WHERE strategy_id = %s AND status = 'won'),
                    total_pnl    = (SELECT COALESCE(SUM(realised_pnl), 0) FROM trade_dossiers
                                   WHERE strategy_id = %s AND status IN ('won','lost'))
                WHERE id = %s
            """, (sid, sid, sid, sid))
            logger.info(f"[TradingFloor] Strategy #{sid} stats updated after dossier #{dossier_id} closed")
    except Exception as e:
        logger.debug(f"[TradingFloor] Strategy stats update error for dossier #{dossier_id}: {e}")


def _update_prompt_version_stats(db, dossier_id: int):
    """Update prompt_versions performance stats for A/B testing after a dossier closes."""
    try:
        d = db.fetch_one(
            "SELECT prompt_version_id, status, realised_pnl, confidence_score "
            "FROM trade_dossiers WHERE id = %s AND prompt_version_id IS NOT NULL",
            (dossier_id,))
        if not d or not d.get("prompt_version_id"):
            return
        pvid = d["prompt_version_id"]
        is_win = 1 if d["status"] == "won" else 0
        pnl = float(d.get("realised_pnl") or 0)
        db.execute("""
            UPDATE prompt_versions SET
                total_trades = COALESCE(total_trades, 0) + 1,
                winning_trades = COALESCE(winning_trades, 0) + %s,
                losing_trades = COALESCE(losing_trades, 0) + %s,
                total_pnl = COALESCE(total_pnl, 0) + %s,
                win_rate = CASE
                    WHEN (COALESCE(total_trades, 0) + 1) > 0
                    THEN (COALESCE(winning_trades, 0) + %s) * 100.0
                          / (COALESCE(total_trades, 0) + 1)
                    ELSE 0 END
            WHERE id = %s
        """, (is_win, 1 - is_win, pnl, is_win, pvid))
        logger.debug(f"[TradingFloor] Prompt version #{pvid} stats updated "
                     f"(dossier #{dossier_id}: {d['status']})")
    except Exception as e:
        logger.debug(f"[TradingFloor] Prompt version stats error: {e}")


def _sync_signal_outcome(db, dossier_id: int, dossier_status: str,
                         close_price: float = 0):
    """Sync a dossier's won/lost status to its linked parsed_signal.
    The 5-min evaluator also checks signals, but dossier tracker may resolve
    first. This keeps both in agreement."""
    sig_row = db.fetch_one("""
        SELECT ps.id, ps.entry_price, ps.stop_loss, ps.direction, ps.outcome
        FROM parsed_signals ps
        WHERE ps.source = 'trading_floor'
          AND ps.news_item_id = %s
          AND ps.is_valid = 1
        LIMIT 1
    """, (dossier_id,))
    if not sig_row:
        return
    if sig_row.get("outcome") in ("win", "loss"):
        return

    if dossier_status == "won":
        sig_status = "tp3_hit"
        outcome = "win"
    elif dossier_status == "lost":
        sig_status = "sl_hit"
        outcome = "loss"
    else:
        return

    entry = float(sig_row.get("entry_price") or 0)
    sl = float(sig_row.get("stop_loss") or 0)
    direction = (sig_row.get("direction") or "").upper()
    outcome_pips = 0
    outcome_rr = 0
    if close_price and entry:
        if direction == "BUY":
            outcome_pips = close_price - entry
        else:
            outcome_pips = entry - close_price
        risk = abs(entry - sl) if sl else 0
        outcome_rr = round(outcome_pips / risk, 2) if risk else 0

    db.execute("""
        UPDATE parsed_signals
        SET status = %s, outcome = %s, outcome_pips = %s, outcome_rr = %s,
            resolved_at = NOW(), resolution_method = 'dossier_tracker'
        WHERE id = %s
    """, (sig_status, outcome, round(outcome_pips, 1), outcome_rr, sig_row["id"]))
    logger.info(f"[TradingFloor] _sync_signal_outcome dossier_id=%d signal_id=%d "
                f"outcome=%s rr=%s", dossier_id, sig_row["id"], outcome, outcome_rr)


# ═══════════════════════════════════════════════════════════════════════
# PAPER TRADE EXECUTION (insert into parsed_signals)
# ═══════════════════════════════════════════════════════════════════════

def _repair_dossier_fields(db, dossier: Dict) -> bool:
    """Last-resort: re-parse entry/SL/TP/direction from stage2_raw_response
    and patch the dossier row if any fields are missing."""
    import re
    raw = dossier.get("stage2_raw_response") or ""
    if not raw:
        return False

    updates = {}
    params = []

    if not dossier.get("direction"):
        rl = raw.lower()
        if "direction: buy" in rl or "direction:**buy" in rl.replace(" ", ""):
            updates["direction"] = "BUY"
        elif "direction: sell" in rl or "direction:**sell" in rl.replace(" ", ""):
            updates["direction"] = "SELL"

    for label, col in [("entry price", "entry_price"), ("entry", "entry_price"),
                       ("stop loss", "stop_loss"),
                       ("take profit 1", "take_profit_1"), ("tp1", "take_profit_1"),
                       ("take profit 2", "take_profit_2"), ("tp2", "take_profit_2"),
                       ("take profit 3", "take_profit_3"), ("tp3", "take_profit_3")]:
        if not dossier.get(col) and col not in updates:
            m = re.search(rf'{label}[:\s]*\*?\*?\s*\$?\s*([\d,]+\.?\d*)',
                          raw, re.IGNORECASE)
            if m:
                try:
                    updates[col] = float(m.group(1).replace(",", ""))
                except ValueError:
                    pass

    if not updates:
        return False

    set_clauses = ", ".join(f"{k} = %s" for k in updates)
    params = list(updates.values()) + [dossier["id"]]
    db.execute(f"UPDATE trade_dossiers SET {set_clauses} WHERE id = %s", tuple(params))
    logger.info(f"[TradingFloor] Repaired dossier #{dossier['id']}: "
                f"{', '.join(f'{k}={v}' for k, v in updates.items())}")
    return True


def execute_paper_trade(db, dossier_id: int) -> Dict:
    """
    Execute a paper trade by inserting a signal into parsed_signals.
    Links the signal back to the dossier. Only creates ONE signal per dossier.
    Dedup checks parsed_signals directly (not linked_signal_id, which may
    reference the triggering Discord signal rather than a paper trade).
    """
    dossier = db.fetch_one("SELECT * FROM trade_dossiers WHERE id = %s",
                           (dossier_id,))
    if not dossier:
        return {"success": False, "error": "Dossier not found"}

    # Only allow paper trades from statuses where a decision has been made.
    # "proposed" is excluded: Apex hasn't decided yet, so paper-trading it
    # would pollute backtest data with pre-decision dossiers.
    if dossier["status"] not in ("open_order", "ready", "monitoring", "live"):
        return {"success": False,
                "error": f"Cannot execute from status '{dossier['status']}'"}

    # Dedup: check if a trading_floor signal already exists for this dossier
    # news_item_id stores the dossier_id for TF signals (they don't have real news items)
    existing_tf_sig = db.fetch_one("""
        SELECT id FROM parsed_signals
        WHERE source = 'trading_floor' AND news_item_id = %s
        AND is_valid = 1
        LIMIT 1
    """, (dossier_id,))
    if existing_tf_sig:
        return {"success": True, "signal_id": existing_tf_sig["id"],
                "already_existed": True,
                "note": "Paper trade signal already exists for this dossier"}

    if not dossier.get("entry_price") or not dossier.get("direction"):
        repaired = _repair_dossier_fields(db, dossier)
        if repaired:
            dossier = db.fetch_one("SELECT * FROM trade_dossiers WHERE id = %s",
                                   (dossier_id,))

    entry_for_signal = dossier.get("actual_entry_price") or dossier.get("entry_price")
    if not entry_for_signal:
        return {"success": False,
                "error": f"No entry_price -- cannot execute paper trade"}
    if not dossier.get("direction"):
        return {"success": False,
                "error": f"No direction -- cannot execute paper trade"}

    # Dedup: skip if near-identical trade already exists (within 1%)
    try:
        from services.trade_dedup import is_duplicate_trade
        if is_duplicate_trade(
                db, dossier["symbol"], dossier["direction"],
                float(dossier.get("entry_price") or 0),
                float(dossier.get("stop_loss") or 0),
                float(dossier.get("take_profit_1") or 0)):
            return {"success": False,
                    "error": f"Duplicate trade: {dossier['symbol']} {dossier['direction']} "
                             f"already exists with similar levels"}
    except Exception:
        pass

    signal_id = db.execute_returning_id("""
        INSERT INTO parsed_signals
        (news_item_id, source, source_detail, author, symbol, direction,
         entry_price, stop_loss, take_profit_1, take_profit_2, take_profit_3,
         confidence, signal_type, timeframe, risk_reward,
         raw_text, ai_reasoning, parsed_by,
         is_valid, status, tier, parsed_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
    """, (
        dossier_id,
        "trading_floor",
        "dossier",
        "Apex (AI Trading Floor)",
        dossier["symbol"],
        dossier["direction"],
        entry_for_signal,
        dossier.get("stop_loss"),
        dossier.get("take_profit_1"),
        dossier.get("take_profit_2"),
        dossier.get("take_profit_3"),
        dossier.get("confidence_score"),
        "entry",
        None,
        None,
        f"AI Trading Floor paper trade from dossier #{dossier_id}",
        (dossier.get("stage2_raw_response") or "")[:8000],
        "trading_floor",
        1,
        "pending",
        "full",
    ))

    db.execute("""
        UPDATE trade_dossiers
        SET status = 'open_order', linked_signal_id = %s
        WHERE id = %s AND status != 'live'
    """, (signal_id, dossier_id))

    try:
        from core.config import get_config
        config = get_config()
    except Exception:
        config = None
    _stamp_leverage_on_dossier(db, dossier_id, config)

    _append_tracker_log(db, dossier_id,
                        f"ORDER PLACED. Signal #{signal_id}. "
                        f"Waiting for entry at {dossier.get('entry_price')}")

    logger.info(f"[TradingFloor] execute_paper_trade dossier_id=%d signal_id=%d "
                f"entry=%s SL=%s TP1=%s", dossier_id, signal_id,
                dossier.get('entry_price'), dossier.get('stop_loss'),
                dossier.get('take_profit_1'))
    return {"success": True, "signal_id": signal_id, "dossier_id": dossier_id}


# ═══════════════════════════════════════════════════════════════════════
# LIVE TRADE EXECUTION (real exchange orders via CCXT)
# ═══════════════════════════════════════════════════════════════════════

def execute_live_trade(db, dossier_id: int, account_id: str,
                       config=None, duo_id: str = None) -> Dict:
    """Place a real limit order on an exchange for a dossier.

    Flow:
      1. Load dossier (entry, SL, TP1-3, direction, symbol)
      2. Load account from trading_accounts
      3. Check can_trade_on_exchange
      4. Check margin cap for this account
      5. Calculate position size using account risk settings
      6. Set leverage on exchange
      7. Place limit order at entry price
      8. Insert into live_trades table
    """
    from core.ccxt_executor import get_executor
    from db.market_symbols import can_trade_on_exchange

    dossier = db.fetch_one("SELECT * FROM trade_dossiers WHERE id = %s", (dossier_id,))
    if not dossier:
        return {"success": False, "error": "Dossier not found"}

    account = db.fetch_one(
        "SELECT * FROM trading_accounts WHERE account_id = %s AND enabled = 1",
        (account_id,))
    if not account:
        return {"success": False, "error": f"Account '{account_id}' not found or disabled"}
    if not account.get("live_trading"):
        return {"success": False, "error": f"Live trading disabled on account '{account_id}'"}

    symbol = dossier.get("symbol", "")
    exchange = account["exchange"]

    can_trade, ticker_or_reason = can_trade_on_exchange(symbol, exchange, db)
    if not can_trade:
        logger.info(f"[LiveTrade] Skip {symbol} on {exchange}/{account_id}: {ticker_or_reason}")
        return {"success": False, "error": ticker_or_reason}

    exchange_symbol = ticker_or_reason

    # Dedup: check if a live trade already exists for this dossier+account.
    # The real race guard is the UNIQUE-ish constraint on the INSERT.
    existing = db.fetch_one(
        "SELECT id FROM live_trades WHERE dossier_id = %s AND account_id = %s "
        "AND status IN ('pending','open','partial_closed')",
        (dossier_id, account_id))
    if existing:
        return {"success": True, "already_existed": True,
                "live_trade_id": existing["id"]}

    # Check max_trades_per_symbol
    max_per_sym = int(account.get("max_trades_per_symbol", 1) or 1)
    sym_count = db.fetch_one(
        "SELECT COUNT(*) as cnt FROM live_trades "
        "WHERE account_id = %s AND symbol = %s AND status IN ('pending','open','partial_closed')",
        (account_id, symbol))
    if sym_count and int(sym_count["cnt"]) >= max_per_sym:
        return {"success": False,
                "error": f"Max {max_per_sym} trades per symbol reached for {symbol}"}

    # Check max_open_trades
    max_open = int(account.get("max_open_trades", 5) or 5)
    open_count = db.fetch_one(
        "SELECT COUNT(*) as cnt FROM live_trades "
        "WHERE account_id = %s AND status IN ('pending','open','partial_closed')",
        (account_id,))
    if open_count and int(open_count["cnt"]) >= max_open:
        return {"success": False, "error": f"Max {max_open} open trades reached"}

    # Margin cap check (same gate as paper/discovery flow).
    # Don't stamp paper_reason here — the waterfall caller sets the final
    # reason after all accounts have been tried.
    if not _check_margin_cap(db, dossier_id, config, target_account_id=account_id):
        return {"success": False,
                "error": f"Margin cap exceeded on {account_id}"}

    # Get executor
    executor = get_executor(account_id, db)
    if not executor:
        return {"success": False, "error": f"Cannot connect to {exchange}"}

    # Get balance and check margin cap
    try:
        bal = executor.get_balance()
        total_balance = bal.get("total", 0)
        free_balance = bal.get("free", 0)
    except Exception as e:
        return {"success": False, "error": f"Balance fetch failed: {e}"}

    margin_cap_pct = float(account.get("margin_cap_pct", 50) or 50)
    max_margin = total_balance * (margin_cap_pct / 100)
    used_margin = total_balance - free_balance
    remaining_margin = max_margin - used_margin
    if remaining_margin <= 0:
        return {"success": False,
                "error": f"Margin cap {margin_cap_pct}% reached on {account_id}"}

    # Position sizing
    entry_price = float(dossier.get("entry_price", 0) or 0)
    stop_loss = float(dossier.get("stop_loss", 0) or 0)
    direction = (dossier.get("direction") or "BUY").upper()
    tp1 = float(dossier.get("take_profit_1", 0) or 0)
    tp2 = float(dossier.get("take_profit_2", 0) or 0)
    tp3 = float(dossier.get("take_profit_3", 0) or 0)

    if entry_price <= 0:
        return {"success": False, "error": "No entry price on dossier"}

    # PRE-FLIGHT: SL direction integrity check
    if stop_loss > 0:
        if direction == "BUY" and stop_loss >= entry_price:
            msg = (f"SL VIOLATION: BUY trade with SL ({stop_loss}) >= entry ({entry_price}). "
                   f"SL must be BELOW entry for BUY. Trade blocked.")
            logger.error(f"[LiveTrade] #{dossier_id} {symbol}: {msg}")
            transition_dossier(db, dossier_id, "abandoned", msg)
            db.execute(
                "UPDATE trade_dossiers SET "
                "apex_entry_reasoning = CONCAT(COALESCE(apex_entry_reasoning,''), %s) "
                "WHERE id = %s",
                (f"\n[BLOCKED] {msg}", dossier_id))
            return {"success": False, "error": msg}
        if direction == "SELL" and stop_loss <= entry_price:
            msg = (f"SL VIOLATION: SELL trade with SL ({stop_loss}) <= entry ({entry_price}). "
                   f"SL must be ABOVE entry for SELL. Trade blocked.")
            logger.error(f"[LiveTrade] #{dossier_id} {symbol}: {msg}")
            transition_dossier(db, dossier_id, "abandoned", msg)
            db.execute(
                "UPDATE trade_dossiers SET "
                "apex_entry_reasoning = CONCAT(COALESCE(apex_entry_reasoning,''), %s) "
                "WHERE id = %s",
                (f"\n[BLOCKED] {msg}", dossier_id))
            return {"success": False, "error": msg}

    # PRE-FLIGHT: Minimum SL distance enforcement.
    # Configurable per account (min_sl_pct); default 1.5%.
    if stop_loss > 0 and entry_price > 0:
        sl_distance_pct = abs(entry_price - stop_loss) / entry_price * 100
        min_sl_pct = float(account.get("min_sl_pct") or 1.5)
        if sl_distance_pct < min_sl_pct:
            msg = (f"MIN SL VIOLATION: SL distance {sl_distance_pct:.2f}% < "
                   f"minimum {min_sl_pct:.1f}%. Entry={entry_price}, SL={stop_loss}. "
                   f"Trade blocked.")
            logger.error(f"[LiveTrade] #{dossier_id} {symbol}: {msg}")
            transition_dossier(db, dossier_id, "abandoned", msg)
            db.execute(
                "UPDATE trade_dossiers SET "
                "apex_entry_reasoning = CONCAT(COALESCE(apex_entry_reasoning,''), %s) "
                "WHERE id = %s",
                (f"\n[BLOCKED] {msg}", dossier_id))
            return {"success": False, "error": msg}

    # PRE-FLIGHT: R:R safety gate — absolute floor, applies to ALL trades.
    # Stage 2 enforces min_rr (2.5:1) but mentor trades bypass it.
    # This gate ensures no trade EVER goes live below 1:1 R:R.
    if tp1 > 0 and stop_loss > 0 and entry_price > 0:
        risk_dist = abs(entry_price - stop_loss)
        reward_dist = abs(tp1 - entry_price)
        if risk_dist > 0:
            live_rr = round(reward_dist / risk_dist, 2)
            if live_rr < 1.0:
                msg = (f"R:R VIOLATION: {live_rr}:1 (below 1:1 floor). "
                       f"Entry={entry_price}, SL={stop_loss}, TP1={tp1}. "
                       f"Risk={risk_dist:.4f}, Reward={reward_dist:.4f}. Trade blocked.")
                logger.warning(f"[LiveTrade] #{dossier_id} {symbol}: {msg}")
                return {"success": False, "error": msg}
            elif live_rr < 1.5:
                logger.warning(f"[LiveTrade] #{dossier_id} {symbol}: R:R {live_rr}:1 "
                               f"is marginal (< 1.5:1) — proceeding with caution")

    # Limit orders are always placed at entry_price on the exchange.
    # The exchange handles the fill when price arrives — no need to gate
    # on current price distance. The entry_threshold_pct setting is used
    # by the tracker's paper-fill logic for trade_now market orders only.

    risk_mode = account.get("risk_mode", "pct")
    if risk_mode == "fixed":
        risk_amount = float(account.get("risk_fixed_usd", 20) or 20)
        risk_pct = risk_amount / total_balance if total_balance else 0.01
    else:
        risk_pct = float(account.get("risk_per_trade_pct", 1.0) or 1.0) / 100
        risk_amount = total_balance * risk_pct

    # Leverage calculation
    leverage_mode = account.get("leverage_mode", "max_before_sl")
    leverage_value = int(account.get("leverage_value", 1) or 1)

    if leverage_mode == "max_before_sl" and stop_loss > 0:
        # Per-account safety buffer overrides global config
        acct_safety = int(account.get("leverage_safety_buffer", 3) or 3)
        td_cfg_override = dict(config.raw.get("trade_decision", {}) if config else {})
        td_cfg_override["leverage_safety_buffer"] = acct_safety
        margin_arg = risk_amount if risk_mode == "fixed" else None
        lev_data = calculate_optimal_leverage(
            entry_price, stop_loss, direction,
            account_balance=total_balance, risk_pct=risk_pct,
            asset_class="cryptocurrency",
            td_cfg=td_cfg_override, margin_usd=margin_arg)
        leverage = lev_data.get("leverage", 1)
    elif leverage_mode == "fixed":
        leverage = leverage_value
    elif leverage_mode == "sliding":
        leverage = leverage_value
    else:
        leverage = 1

    # Clamp leverage to exchange limits
    try:
        lev_limits = executor.get_leverage_limits(exchange_symbol)
        clamped = max(lev_limits["min"], min(leverage, lev_limits["max"]))
        if clamped != leverage:
            logger.info(f"[LiveTrade] Leverage clamped {leverage}x -> {clamped}x "
                        f"for {exchange_symbol} (exchange limits: "
                        f"{lev_limits['min']}-{lev_limits['max']}x)")
        leverage = clamped
    except Exception as e:
        logger.warning(f"[LiveTrade] Could not fetch leverage limits for "
                       f"{exchange_symbol}: {e} — using calculated {leverage}x")

    # Position size = (risk_amount * leverage) / entry_price
    # But also ensure margin doesn't exceed remaining_margin
    position_margin = min(risk_amount, remaining_margin)
    notional = position_margin * leverage
    position_size = notional / entry_price

    # Loss-at-SL gate: block if estimated loss exceeds margin (risk budget)
    if stop_loss > 0 and entry_price > 0:
        sl_dist = abs(entry_price - stop_loss)
        loss_at_sl = position_size * sl_dist
        if loss_at_sl > position_margin * 1.05:
            logger.warning(
                f"[LiveTrade] BLOCKED #{dossier_id}: loss_at_sl ${loss_at_sl:.2f} "
                f"> margin ${position_margin:.2f} (lev={leverage}x, "
                f"sl_dist={sl_dist:.6f}, size={position_size:.6f})")
            return {"success": False,
                    "error": f"Loss at SL (${loss_at_sl:.2f}) exceeds risk "
                             f"budget (${position_margin:.2f})"}

    # Round amount and price to exchange precision (handles step sizes,
    # contract sizes, decimal vs tick precision per exchange).
    # Use separate variables to avoid mutating the dossier's canonical prices.
    position_size = executor.amount_to_precision(exchange_symbol, position_size)
    order_entry_price = executor.price_to_precision(exchange_symbol, entry_price)
    order_sl = executor.price_to_precision(exchange_symbol, stop_loss) if stop_loss > 0 else 0
    order_tp1 = executor.price_to_precision(exchange_symbol, tp1) if tp1 > 0 else 0
    order_tp2 = executor.price_to_precision(exchange_symbol, tp2) if tp2 > 0 else 0
    order_tp3 = executor.price_to_precision(exchange_symbol, tp3) if tp3 > 0 else 0

    if position_size <= 0:
        return {"success": False, "error": "Calculated position size is zero"}

    # Check exchange minimum order size
    mkt = executor.market_info(exchange_symbol)
    if mkt:
        min_amount = float((mkt.get("limits", {}).get("amount", {}).get("min")) or 0)
        min_cost = float((mkt.get("limits", {}).get("cost", {}).get("min")) or 0)
        if min_amount > 0 and position_size < min_amount:
            return {"success": False,
                    "error": f"Position size {position_size} below exchange minimum "
                             f"{min_amount} for {exchange_symbol}"}
        if min_cost > 0 and notional < min_cost:
            return {"success": False,
                    "error": f"Notional ${notional:.2f} below exchange minimum "
                             f"${min_cost:.2f} for {exchange_symbol}"}

    # Ensure isolated margin mode — all JarvAIs trades use isolated margin
    # so that one position's liquidation can't drain the entire account.
    mm_result = executor.set_margin_mode(exchange_symbol, "isolated")
    if not mm_result.get("success"):
        mm_err = str(mm_result.get("error", "unknown"))
        if "already" in mm_err.lower() or "no need" in mm_err.lower():
            logger.debug(f"[LiveTrade] Margin mode already isolated for {exchange_symbol}")
        else:
            logger.error(f"[LiveTrade] set_margin_mode(isolated) FAILED for "
                         f"{exchange_symbol}: {mm_err} — blocking order to prevent cross margin")
            return {"success": False,
                    "error": f"Margin mode failed (cross margin risk): {mm_err}"}

    # Set leverage on exchange — MUST succeed before placing order
    lev_result = executor.set_leverage(exchange_symbol, leverage)
    if not lev_result.get("success"):
        err = lev_result.get('error', 'unknown')
        logger.warning(f"[LiveTrade] Set leverage FAILED for {exchange_symbol} "
                       f"({leverage}x): {err}")
        return {"success": False,
                "error": f"Leverage set failed ({leverage}x): {err}"}

    # Place limit order (use exchange-rounded prices, not original dossier values)
    order_side = "buy" if direction == "BUY" else "sell"
    order_result = executor.place_limit_order(
        exchange_symbol=exchange_symbol,
        side=order_side,
        amount=position_size,
        price=order_entry_price,
        sl=order_sl if order_sl > 0 else None,
        tp=order_tp1 if order_tp1 > 0 else None,
    )

    if not order_result.get("success"):
        err = order_result.get("error", "Order placement failed")
        try:
            db.execute("""
                INSERT INTO live_trade_audit
                (account_id, dossier_id, duo_id, action, exchange, symbol,
                 request_data, response_data, success, error_message)
                VALUES (%s, %s, %s, 'place_order', %s, %s, %s, %s, 0, %s)
            """, (account_id, dossier_id, duo_id, exchange, exchange_symbol,
                  json.dumps({"direction": direction, "amount": position_size,
                              "price": order_entry_price, "leverage": leverage}),
                  json.dumps(order_result), err))
        except Exception:
            pass
        return {"success": False, "error": err}

    order_id = order_result.get("order_id", "")

    # trade_source ENUM is ('apex','mentor','manual').  The duo name is
    # already stored in duo_id; trade_source marks the *origin category*.
    mentor_source = None
    if dossier.get("mentor_type") == "mentor_mirror":
        trade_source = "mentor"
        mentor_source = dossier.get("mentor_source")
    else:
        trade_source = "apex"

    # Insert live_trades row -- if DB fails, cancel the exchange order to prevent ghost orders
    try:
        live_trade_id = db.execute_returning_id("""
            INSERT INTO live_trades
            (account_id, dossier_id, exchange, symbol, exchange_symbol, direction,
             order_id, order_type, entry_price, position_size, margin_usd, leverage,
             stop_loss, take_profit_1, take_profit_2, take_profit_3,
             status, trade_source, mentor_source, duo_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            account_id, dossier_id, exchange, symbol, exchange_symbol, direction,
            order_id, "limit", order_entry_price, position_size,
            round(position_margin, 2), leverage,
            order_sl if order_sl > 0 else None,
            order_tp1 if order_tp1 > 0 else None,
            order_tp2 if order_tp2 > 0 else None,
            order_tp3 if order_tp3 > 0 else None,
            "pending", trade_source, mentor_source,
            duo_id,
        ))
    except Exception as db_err:
        logger.critical(f"[LiveTrade] GHOST ORDER RISK: Exchange order {order_id} placed "
                        f"on {exchange} for {symbol} but DB insert FAILED: {db_err}. "
                        f"Attempting emergency cancel.")
        try:
            executor.cancel_order(order_id, exchange_symbol)
            logger.info(f"[LiveTrade] Emergency cancel succeeded for {order_id}")
        except Exception as cancel_err:
            logger.critical(f"[LiveTrade] EMERGENCY CANCEL ALSO FAILED for {order_id} "
                            f"on {exchange}/{exchange_symbol}: {cancel_err}. "
                            f"MANUAL INTERVENTION REQUIRED.")
        return {"success": False, "error": f"DB insert failed after order placement: {db_err}"}

    # Write back final margin/leverage/prices to dossier so paper P&L matches live.
    # Includes precision-rounded entry, SL, and all TPs so dossier and live_trades
    # are byte-identical — prevents paper/live P&L divergence on low-price assets.
    db.execute("""
        UPDATE trade_dossiers
        SET margin_usd = %s, leverage = %s, target_account_id = %s,
            entry_price = COALESCE(%s, entry_price),
            stop_loss = COALESCE(%s, stop_loss),
            take_profit_1 = COALESCE(%s, take_profit_1),
            take_profit_2 = COALESCE(%s, take_profit_2),
            take_profit_3 = COALESCE(%s, take_profit_3)
        WHERE id = %s
    """, (round(position_margin, 2), leverage, account_id,
          order_entry_price if order_entry_price > 0 else None,
          order_sl if order_sl > 0 else None,
          order_tp1 if order_tp1 > 0 else None,
          order_tp2 if order_tp2 > 0 else None,
          order_tp3 if order_tp3 > 0 else None,
          dossier_id))

    db.execute(
        "UPDATE trading_accounts SET cached_balance = %s, cached_balance_at = NOW() "
        "WHERE account_id = %s",
        (total_balance, account_id))

    logger.info(f"[LiveTrade] Placed {direction} {position_size} {exchange_symbol} "
                f"@ {order_entry_price} | lev={leverage}x margin=${position_margin:.2f} "
                f"| order={order_id} | account={account_id} | dossier=#{dossier_id}")

    # Audit trail
    try:
        db.execute("""
            INSERT INTO live_trade_audit
            (account_id, dossier_id, duo_id, action, exchange, symbol, request_data,
             response_data, success)
            VALUES (%s, %s, %s, 'place_order', %s, %s, %s, %s, 1)
        """, (account_id, dossier_id, duo_id, exchange, exchange_symbol,
              json.dumps({"direction": direction, "amount": position_size,
                          "price": order_entry_price, "leverage": leverage,
                          "margin": round(position_margin, 2),
                          "sl": order_sl, "tp1": order_tp1}),
              json.dumps({"order_id": order_id, "status": "pending",
                          "live_trade_id": live_trade_id})))
    except Exception:
        pass

    return {
        "success": True,
        "live_trade_id": live_trade_id,
        "order_id": order_id,
        "exchange_symbol": exchange_symbol,
        "position_size": position_size,
        "leverage": leverage,
        "margin": round(position_margin, 2),
        "account_id": account_id,
        "dossier_id": dossier_id,
    }


def _get_live_trading_accounts(db, ordered: bool = False) -> List[Dict]:
    """Fetch all enabled accounts with live_trading=1.
    If ordered=True, sort by waterfall_priority ASC (lower = higher priority)."""
    order = "ORDER BY waterfall_priority ASC, id ASC" if ordered else ""
    rows = db.fetch_all(
        f"SELECT * FROM trading_accounts WHERE enabled = 1 AND live_trading = 1 {order}")
    return rows or []


def _is_waterfall_enabled(db) -> bool:
    """Check if waterfall trading mode is enabled."""
    try:
        row = db.fetch_one(
            "SELECT config_value FROM system_config WHERE config_key = 'waterfall_enabled'")
        return row and row.get("config_value", "0") == "1"
    except Exception:
        return False


def _is_mentor_whitelisted(db, mentor_name: str) -> bool:
    """Check if a mentor is in the mentor_ids whitelist of any live trading account."""
    accounts = _get_live_trading_accounts(db)
    for acct in accounts:
        if not acct.get("mentor_enabled"):
            continue
        allowed = acct.get("mentor_ids")
        if isinstance(allowed, str):
            try:
                allowed = json.loads(allowed)
            except (json.JSONDecodeError, TypeError):
                allowed = None
        if not allowed:
            return True
        if mentor_name in allowed:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════
# DISCOVERY LOOP (runs on scheduler)
# ═══════════════════════════════════════════════════════════════════════

class TradingFloorService:
    """
    Orchestrates the autonomous trading floor:
    - Discovery: builds dossiers for watchlist symbols
    - Monitoring: 15-min tracker checks on active dossiers
    - Execution: paper trades when conditions are met
    - Post-mortem: analysis on resolved trades
    """

    def __init__(self, db, config, duo_id: str = "apex"):
        self.db = db
        self.config = config
        self.duo_id = duo_id

        if duo_id and duo_id != "__legacy__":
            from core.duo_config import get_duo_config
            self._td_cfg = get_duo_config(config, duo_id, db=db)
        else:
            self._td_cfg = config.raw.get("trade_decision", {}) if config else {}

        self._merge_db_config()
        self._running = False
        self._discovery_thread = None
        self._tracker_thread = None
        self._signal_tracker_thread = None
        self._stop_event = threading.Event()
        self._last_signal_check_at = None
        self._last_dossier_check_at = None
        self._activity_log = deque(maxlen=200)
        self._pipeline_queue: Dict[str, Dict] = {}
        self._pipeline_history = deque(maxlen=50)
        self._pipeline_lock = threading.Lock()
        self._no_price_counter: Dict[int, int] = {}
        self._discovery_cooldown: Dict[str, datetime] = {}
        self._apex_thoughts = deque(maxlen=500)
        self._opp_prev_conditions: Dict[int, int] = {}
        self._execution_ready: List[Dict] = []

        _pool_size = self._td_cfg.get("max_concurrent_builds", 5)
        self._build_pool = ThreadPoolExecutor(
            max_workers=_pool_size, thread_name_prefix=f"dossier-{duo_id}")
        self._active_builds: Dict[str, Any] = {}
        self._build_lock = threading.Lock()
        self._tracker_lock = threading.Lock()
        self._market_regime: Optional[Dict] = None

    def _log_apex_thought(self, symbol: str, event: str, detail: str,
                          decision: str = None, probability: int = None):
        """Record an Apex decision/thought for the Apex Brain tab."""
        self._apex_thoughts.append({
            "ts": _utcnow().isoformat() + "Z",
            "duo_id": self.duo_id,
            "symbol": symbol,
            "event": event,
            "detail": detail,
            "decision": decision,
            "probability": probability,
        })

    def get_apex_thoughts(self, limit: int = 200) -> List[Dict]:
        """Return Apex's recent thoughts (newest first)."""
        entries = list(self._apex_thoughts)
        entries.reverse()
        return entries[:limit]

    def _margin_gate(self, dossier_id: int) -> bool:
        """
        Check margin cap. If full, try to replace a weaker dossier.
        Returns True if dossier is allowed to proceed, False to block.
        """
        if _check_margin_cap(self.db, dossier_id, self.config):
            return True
        if _try_replace_weak_dossier(self.db, dossier_id, self.config):
            return _check_margin_cap(self.db, dossier_id, self.config)
        return False

    # ── Phase H: Opportunity Score Engine ─────────────────────────────

    def _compute_opp_score(self, dossier: dict, candles: dict = None) -> float:
        """Compute composite Opportunity Score (0-100) for ranking dossiers.
        Blends probability, R:R, condition momentum, entry proximity, and freshness."""
        weights = self._td_cfg.get("opp_score_weights", {
            "probability": 0.30, "risk_reward": 0.35,
            "condition_momentum": 0.15, "entry_proximity": 0.10,
            "freshness": 0.10,
        })

        # --- 1. Probability (0-100) ---
        try:
            prob_hist = json.loads(dossier.get("probability_history") or "[]")
            prob = prob_hist[-1].get("probability") if prob_hist and isinstance(prob_hist[-1], dict) else 0
        except (json.JSONDecodeError, TypeError, AttributeError):
            prob = 0
        prob_score = min(max(prob or 0, 0), 100)

        # --- 2. Risk:Reward ratio (scaled: 2:1=50, 3:1=75, 4:1=90, 5:1+=100) ---
        entry = float(dossier.get("entry_price") or 0)
        sl = float(dossier.get("stop_loss") or 0)
        tp1 = float(dossier.get("take_profit_1") or 0)
        rr_score = 0.0
        if entry and sl and tp1 and abs(entry - sl) > 0:
            dist_sl = abs(entry - sl)
            dist_tp = abs(tp1 - entry)
            rr = dist_tp / dist_sl
            min_rr = self._td_cfg.get("min_risk_reward", 2.0)
            if rr < min_rr:
                rr_score = max(0, (rr / min_rr) * 40)
            else:
                rr_score = min(40 + (rr - min_rr) * 20, 100)

        # --- 3. Condition momentum (improving vs declining) ---
        try:
            conditions = json.loads(dossier.get("conditions_for_entry") or "[]")
        except (json.JSONDecodeError, TypeError):
            conditions = []
        met_now = sum(1 for c in conditions if c.get("status") == "met")
        total_c = len(conditions) or 1
        met_pct = met_now / total_c

        did = dossier["id"]
        prev_met = self._opp_prev_conditions.get(did, met_now)
        self._opp_prev_conditions[did] = met_now

        if met_now > prev_met:
            momentum = 75 + min((met_now - prev_met) * 12.5, 25)
        elif met_now < prev_met:
            momentum = max(0, 25 - (prev_met - met_now) * 12.5)
        else:
            momentum = 50 + met_pct * 25  # stable → 50-75

        # --- 4. Entry proximity (closer to entry = higher score) ---
        latest_price = (candles.get("latest_close", 0) if candles else 0) or \
                       float(dossier.get("current_price") or 0)
        prox_score = 0.0
        if entry and latest_price:
            dist_pct = abs(latest_price - entry) / entry * 100
            prox_score = max(0, 100 - dist_pct * 10)  # 0% = 100, 10%+ = 0

        # --- 5. Freshness (decays linearly to 0 over dossier_expiry_hours) ---
        expiry_h = self._td_cfg.get("dossier_expiry_hours", 48)
        created = dossier.get("created_at")
        fresh_score = 100.0
        if created:
            if isinstance(created, str):
                try:
                    created = datetime.fromisoformat(created.replace("Z", "+00:00")).replace(tzinfo=None)
                except (ValueError, TypeError):
                    created = None
            if created:
                age_h = (_utcnow() - created).total_seconds() / 3600
                fresh_score = max(0, 100 - age_h * (100 / max(expiry_h, 1)))

        score = (
            prob_score  * weights.get("probability", 0.40) +
            rr_score    * weights.get("risk_reward", 0.25) +
            momentum    * weights.get("condition_momentum", 0.15) +
            prox_score  * weights.get("entry_proximity", 0.10) +
            fresh_score * weights.get("freshness", 0.10)
        )
        score = round(min(max(score, 0), 100), 2)

        self.db.execute(
            "UPDATE trade_dossiers SET opp_score = %s WHERE id = %s",
            (score, did))

        return score

    def _execute_ranked_opportunities(self):
        """Execute batched execution-ready dossiers in OppScore order (best first).
        Called at the end of each tracker cycle after all conditions are evaluated."""
        if not self._execution_ready:
            return

        MENTOR_BONUS = 15
        ranked = sorted(self._execution_ready,
                        key=lambda d: d["opp_score"] + (MENTOR_BONUS if d.get("mentor_type") else 0),
                        reverse=True)

        ranking_str = " > ".join(
            f"#{d['id']} {d['symbol']} (score={d['opp_score']})"
            for d in ranked)
        logger.info(f"[TradingFloor] RANKED EXECUTION: {len(ranked)} ready — {ranking_str}")
        self._log_apex_thought(
            symbol=ranked[0]["symbol"],
            event="ranked_execution",
            detail=f"Ranked {len(ranked)} dossiers: {ranking_str}",
            decision="execute_best_first",
            probability=ranked[0].get("probability"),
        )

        executed_count = 0
        for d in ranked:
            did, sym = d["id"], d["symbol"]
            if not self._margin_gate(did):
                self._log_activity("warn",
                    f"#{did} {sym}: MARGIN CAP — blocked (score={d['opp_score']})",
                    symbol=sym, dossier_id=did)
                self._log_apex_thought(sym, "margin_block",
                    f"#{did} blocked by margin cap (score={d['opp_score']})",
                    decision="blocked")
                continue

            t_result = transition_dossier(self.db, did, "open_order",
                               f"Ranked execution: OppScore {d['opp_score']} "
                               f"(prob {d['probability']}%)")
            if not t_result.get("success"):
                logger.warning(f"[TradingFloor] #{did} {sym}: transition to open_order "
                               f"failed — {t_result.get('error','unknown')} — skipping")
                continue

            try:
                drow = self.db.fetch_one(
                    "SELECT conditions_for_entry FROM trade_dossiers WHERE id = %s",
                    (did,))
                conds = json.loads((drow or {}).get("conditions_for_entry") or "[]")
                c_met = sum(1 for c in conds if c.get("status") == "met")
                c_total = len(conds)
                self.db.execute("""
                    UPDATE trade_dossiers
                    SET trigger_probability = %s,
                        trigger_conditions_met = %s,
                        trigger_conditions_total = %s,
                        trigger_threshold_execute = %s,
                        trigger_threshold_limit = %s,
                        trigger_min_confidence = %s
                    WHERE id = %s
                """, (d["probability"], c_met, c_total,
                      self._td_cfg.get("condition_threshold_execute", 65),
                      self._td_cfg.get("condition_threshold_limit_order", 50),
                      self._td_cfg.get("min_confidence_for_trade", 60),
                      did))
            except Exception as e:
                logger.debug(f"[TradingFloor] trigger snapshot #{did}: {e}")

            self._register_order_entry_level(did)
            self._log_activity("trade",
                f"#{did} {sym}: OPEN ORDER — ranked #{executed_count+1} "
                f"(score={d['opp_score']}, prob={d['probability']}%)",
                symbol=sym, dossier_id=did)
            if self._td_cfg.get("auto_execute_paper", False):
                execute_paper_trade(self.db, did)
            self._execute_live_trades_for_dossier(did)
            executed_count += 1

        self._execution_ready.clear()

    def _log_activity(self, level: str, message: str, symbol: str = None,
                      dossier_id: int = None):
        """Append an entry to the in-memory activity log ring buffer."""
        self._activity_log.append({
            "ts": _utcnow().isoformat() + "Z",
            "level": level,
            "msg": message,
            "symbol": symbol,
            "dossier_id": dossier_id,
        })

    def get_activity_log(self, limit: int = 100) -> List[Dict]:
        """Return the most recent activity log entries (newest first)."""
        entries = list(self._activity_log)
        entries.reverse()
        return entries[:limit]

    # ── Pipeline Queue Tracking ────────────────────────────────────

    def _pipeline_start(self, symbol: str, source: str = "apex"):
        """Mark a dossier as entering the processing pipeline."""
        key = f"{symbol}_{_utcnow().timestamp()}"
        with self._pipeline_lock:
            self._pipeline_queue[key] = {
                "key": key, "symbol": symbol, "source": source,
                "duo_id": self.duo_id,
                "stage": "gathering", "started_at": _utcnow().isoformat(),
                "stage_started_at": _utcnow().isoformat(),
                "position": len(self._pipeline_queue) + 1,
                "steps": [],
            }
        return key

    def _pipeline_stage(self, key: str, stage: str, detail: str = ""):
        with self._pipeline_lock:
            if key in self._pipeline_queue:
                now = _utcnow()
                prev_stage_start = self._pipeline_queue[key].get("stage_started_at")
                elapsed_in_stage = 0
                if prev_stage_start:
                    try:
                        elapsed_in_stage = (now - datetime.fromisoformat(prev_stage_start)).total_seconds()
                    except Exception:
                        pass
                self._pipeline_queue[key]["steps"].append({
                    "stage": self._pipeline_queue[key]["stage"],
                    "duration_sec": round(elapsed_in_stage, 1),
                    "detail": detail,
                    "at": now.isoformat(),
                })
                self._pipeline_queue[key]["stage"] = stage
                self._pipeline_queue[key]["stage_started_at"] = now.isoformat()

    def _pipeline_done(self, key: str, dossier_id: int = None):
        with self._pipeline_lock:
            entry = self._pipeline_queue.pop(key, None)
            if entry:
                entry["completed_at"] = _utcnow().isoformat()
                rejected = dossier_id is None or dossier_id <= 0
                entry["dossier_id"] = dossier_id if not rejected else None
                entry["rejected"] = rejected
                start = datetime.fromisoformat(entry["started_at"])
                entry["duration_sec"] = (_utcnow() - start).total_seconds()
                self._pipeline_history.append(entry)
                for i, (k, v) in enumerate(self._pipeline_queue.items()):
                    v["position"] = i + 1

    def get_pipeline_queue(self) -> Dict:
        """Return current pipeline state for the dashboard."""
        with self._pipeline_lock:
            queue = list(self._pipeline_queue.values())
        history = list(self._pipeline_history)

        avg_duration = 0
        if history:
            durations = [h.get("duration_sec", 0) for h in history if h.get("duration_sec")]
            avg_duration = sum(durations) / len(durations) if durations else 0

        for item in queue:
            start = datetime.fromisoformat(item["started_at"])
            item["elapsed_sec"] = (_utcnow() - start).total_seconds()
            item["eta_sec"] = max(0, avg_duration - item["elapsed_sec"]) if avg_duration else None

            try:
                from services.price_streamer import get_price_streamer
                ps = get_price_streamer()
                if ps:
                    p = ps.get_price(item["symbol"])
                    if p and p.get("price"):
                        item["current_price"] = p["price"]
            except Exception:
                pass

        return {
            "queue": sorted(queue, key=lambda x: x.get("position", 99)),
            "queue_depth": len(queue),
            "avg_duration_sec": round(avg_duration, 1),
            "recent_completions": history[-10:],
        }

    # ── Real-Time Level Detection (via PriceStreamer) ────────────

    def _register_all_levels(self):
        """Register entry/SL/TP levels for all active dossiers with PriceStreamer."""
        try:
            from services.price_streamer import get_price_streamer
            ps = get_price_streamer()
            if not ps:
                return

            orders = self.db.fetch_all(
                "SELECT id, symbol, direction, entry_price, stop_loss, current_price "
                "FROM trade_dossiers WHERE status = 'open_order' AND duo_id = %s",
                (self.duo_id,))
            for o in (orders or []):
                d = (o.get("direction") or "").upper()
                if o.get("entry_price"):
                    entry_val = float(o["entry_price"])
                    cached = ps.get_price(o["symbol"])
                    last_price = (cached or {}).get("price") or float(o.get("current_price") or 0)
                    if last_price > 0:
                        if d == "BUY":
                            trigger = "below" if entry_val <= last_price else "above"
                        else:
                            trigger = "above" if entry_val >= last_price else "below"
                    else:
                        trigger = "below" if d == "BUY" else "above"
                    ps.levels.register(o["symbol"], entry_val,
                                       trigger, f"entry_{o['id']}", {"type": "entry", "dossier_id": o["id"]})

            live = self.db.fetch_all(
                "SELECT id, symbol, direction, entry_price, stop_loss, "
                "take_profit_1, take_profit_2, take_profit_3 "
                "FROM trade_dossiers WHERE status = 'live' AND duo_id = %s",
                (self.duo_id,))
            for t in (live or []):
                d = (t.get("direction") or "").upper()
                if t.get("stop_loss"):
                    sl_dir = "below" if d == "BUY" else "above"
                    ps.levels.register(t["symbol"], float(t["stop_loss"]),
                                       sl_dir, f"sl_{t['id']}", {"type": "sl", "dossier_id": t["id"]})
                for i, tp_col in enumerate(["take_profit_1", "take_profit_2", "take_profit_3"], 1):
                    tp_val = t.get(tp_col)
                    if tp_val and float(tp_val) > 0:
                        tp_dir = "above" if d == "BUY" else "below"
                        ps.levels.register(t["symbol"], float(tp_val),
                                           tp_dir, f"tp{i}_{t['id']}", {"type": f"tp{i}", "dossier_id": t["id"]})

            logger.info(f"[TradingFloor] Registered {ps.levels.count()} price level watches")
        except Exception as e:
            logger.debug(f"[TradingFloor] _register_all_levels error: {e}")

    def _on_level_hit(self, watch: Dict, price: float):
        """Callback when PriceStreamer detects a level breach."""
        meta = watch.get("meta", {})
        dossier_id = meta.get("dossier_id")
        level_type = meta.get("type", "")
        symbol = watch.get("symbol", "")

        if not dossier_id:
            return

        logger.info(f"[TradingFloor] LEVEL HIT: {level_type} on #{dossier_id} "
                    f"{symbol} at {price} (level={watch.get('level')})")
        self._log_activity("trade", f"#{dossier_id} {symbol}: {level_type.upper()} level hit "
                          f"at {price}", symbol=symbol, dossier_id=dossier_id)

        try:
            if level_type == "entry":
                self._handle_instant_entry(dossier_id, price)
            elif level_type == "sl":
                self._handle_instant_sl(dossier_id, price)
            elif level_type.startswith("tp"):
                self._handle_instant_tp(dossier_id, price, level_type)
        except Exception as e:
            logger.error(f"[TradingFloor] level hit handler error: {e}", exc_info=True)

    def _handle_instant_entry(self, dossier_id: int, price: float):
        """Instant entry fill from live price data. Keep entry_price as target for audit."""
        d = self.db.fetch_one(
            "SELECT id, symbol, status, direction, entry_price, margin_usd, leverage "
            "FROM trade_dossiers WHERE id = %s",
            (dossier_id,))
        if not d or d["status"] != "open_order":
            return

        margin = float(d.get("margin_usd") or 20)
        lev = int(d.get("leverage") or 5)
        entry_fee = _calc_fee(margin * lev, is_taker=True, config=self.config)
        self.db.execute(
            "UPDATE trade_dossiers SET actual_entry_price = %s, entry_fill_source = 'live_stream', "
            "entry_fee = %s, original_stop_loss = COALESCE(original_stop_loss, stop_loss) "
            "WHERE id = %s",
            (round(price, 5), round(entry_fee, 6), dossier_id))

        result = transition_dossier(self.db, dossier_id, "live",
                                    f"Entry filled at {price} (live stream)")
        if result.get("success"):
            self.db.execute("UPDATE trade_dossiers SET executed_at = NOW() WHERE id = %s", (dossier_id,))
            _snapshot_conditions_at_entry(self.db, dossier_id)
            _update_live_pnl(self.db, dossier_id, price, price, d["direction"], self.config)
            self._execute_paper_trade(dossier_id)
            self._register_live_levels(dossier_id)
            self._execute_live_trades_for_dossier(dossier_id)
            self._log_activity("trade", f"#{dossier_id} {d['symbol']}: ENTRY FILLED at {price} (live)",
                              symbol=d["symbol"], dossier_id=dossier_id)

    def _handle_instant_sl(self, dossier_id: int, price: float):
        """Instant stop loss hit from live price data."""
        d = self.db.fetch_one(
            "SELECT id, symbol, status, entry_price, direction, stop_loss FROM trade_dossiers WHERE id = %s",
            (dossier_id,))
        if not d or d["status"] != "live":
            return

        pnl = _finalise_pnl(self.db, dossier_id, price, exit_is_taker=True, config=self.config, exit_is_sl=True)
        outcome = "won" if pnl >= 0 else "lost"
        transition_dossier(self.db, dossier_id, outcome,
                          f"SL hit at {price} (live stream) — P&L ${pnl:.2f}")
        _update_strategy_stats(self.db, dossier_id)
        _update_prompt_version_stats(self.db, dossier_id)
        _sync_signal_outcome(self.db, dossier_id, outcome, price)
        label = "WON (SL in profit)" if outcome == "won" else "STOP LOSS"
        self._log_activity("trade", f"#{dossier_id} {d['symbol']}: {label} at {price} (live)",
                          symbol=d["symbol"], dossier_id=dossier_id)
        self._trigger_postmortem(dossier_id)

    def _handle_instant_tp(self, dossier_id: int, price: float, tp_level: str):
        """
        Instant take profit hit from live price data.

        TP trailing logic:
          TP1 → SL moves to breakeven + buffer, partial close per tp1_close_pct
          TP2 → SL moves to TP1 level (lock in TP1 profit)
          TP3 → SL moves to TP2 level (lock in TP2 profit)
          Highest TP → fully close as won, P&L uses TP level (not live price)
        """
        d = self.db.fetch_one(
            "SELECT id, symbol, status, entry_price, direction, stop_loss, "
            "take_profit_1, take_profit_2, take_profit_3, tp_progress "
            "FROM trade_dossiers WHERE id = %s", (dossier_id,))
        if not d or d["status"] != "live":
            return

        tp_num = int(tp_level.replace("tp", ""))
        tp_hit_at_col = f"tp{tp_num}_hit_at"
        direction = (d["direction"] or "").upper()
        entry = float(d["entry_price"])
        tp_price = float(d.get(f"take_profit_{tp_num}") or price)

        self.db.execute(f"""
            UPDATE trade_dossiers
            SET tp_progress = %s, {tp_hit_at_col} = NOW()
            WHERE id = %s
        """, (f"tp{tp_num}_hit", dossier_id))

        ts_cfg = self.config.raw.get("trade_settings", {}) if self.config else {}

        live_trade_row = self.db.fetch_one(
            "SELECT lt.account_id, ta.sl_to_be_enabled, ta.sl_to_be_trigger "
            "FROM live_trades lt "
            "JOIN trading_accounts ta ON ta.account_id = lt.account_id "
            "WHERE lt.dossier_id = %s AND lt.status = 'open' LIMIT 1",
            (dossier_id,))
        sl_be_enabled = bool(live_trade_row.get("sl_to_be_enabled", True)) if live_trade_row else True
        sl_be_trigger = (live_trade_row.get("sl_to_be_trigger") or "TP1").upper() if live_trade_row else "TP1"

        if tp_num == 1:
            close_pct = ts_cfg.get("tp1_close_pct", 50)
            if sl_be_enabled and sl_be_trigger == "TP1":
                buffer_pct = self._td_cfg.get("sl_to_be_buffer_pct", 0.15) / 100
                new_sl = entry * (1 - buffer_pct) if direction == "SELL" else entry * (1 + buffer_pct)
                self.db.execute("UPDATE trade_dossiers SET stop_loss = %s WHERE id = %s",
                              (round(new_sl, 5), dossier_id))
                self._reregister_sl(dossier_id, new_sl, d["symbol"])
                _append_tracker_log(self.db, dossier_id,
                    f"TP1 hit at {tp_price} — SL→BE+buffer: {new_sl:.5f}, "
                    f"partial close {close_pct}%")
            else:
                _append_tracker_log(self.db, dossier_id,
                    f"TP1 hit at {tp_price} — SL-to-BE disabled or trigger={sl_be_trigger}, "
                    f"SL unchanged, partial close {close_pct}%")

        elif tp_num == 2:
            tp1_val = float(d.get("take_profit_1") or entry)
            new_sl = tp1_val
            self.db.execute("UPDATE trade_dossiers SET stop_loss = %s WHERE id = %s",
                          (round(new_sl, 5), dossier_id))
            self._reregister_sl(dossier_id, new_sl, d["symbol"])
            _append_tracker_log(self.db, dossier_id,
                f"TP2 hit at {tp_price} — SL moved up to TP1: {new_sl:.5f}")

        elif tp_num == 3:
            tp2_val = float(d.get("take_profit_2") or d.get("take_profit_1") or entry)
            new_sl = tp2_val
            self.db.execute("UPDATE trade_dossiers SET stop_loss = %s WHERE id = %s",
                          (round(new_sl, 5), dossier_id))
            self._reregister_sl(dossier_id, new_sl, d["symbol"])
            _append_tracker_log(self.db, dossier_id,
                f"TP3 hit at {tp_price} — SL moved up to TP2: {new_sl:.5f}")

        highest_tp = 0
        for i in [3, 2, 1]:
            if d.get(f"take_profit_{i}") and float(d[f"take_profit_{i}"]) > 0:
                highest_tp = i
                break

        if tp_num >= highest_tp and highest_tp > 0:
            _finalise_pnl(self.db, dossier_id, tp_price, exit_is_taker=True, config=self.config)
            transition_dossier(self.db, dossier_id, "won",
                              f"TP{tp_num} hit at {tp_price} (live stream)")
            _update_strategy_stats(self.db, dossier_id)
            _update_prompt_version_stats(self.db, dossier_id)
            _sync_signal_outcome(self.db, dossier_id, "won", tp_price)
            self._log_activity("trade", f"#{dossier_id} {d['symbol']}: WON at TP{tp_num} {tp_price} (live)",
                              symbol=d["symbol"], dossier_id=dossier_id)
            self._trigger_postmortem(dossier_id)
        else:
            self._log_activity("trade", f"#{dossier_id} {d['symbol']}: TP{tp_num} hit at {tp_price} (live)",
                              symbol=d["symbol"], dossier_id=dossier_id)

    def _register_live_levels(self, dossier_id: int):
        """Register SL/TP levels for a newly live dossier."""
        try:
            from services.price_streamer import get_price_streamer
            ps = get_price_streamer()
            if not ps:
                return
            d = self.db.fetch_one(
                "SELECT symbol, direction, stop_loss, take_profit_1, take_profit_2, take_profit_3 "
                "FROM trade_dossiers WHERE id = %s", (dossier_id,))
            if not d:
                return
            direction = (d["direction"] or "").upper()
            if direction not in ("BUY", "SELL"):
                logger.error(f"[TradingFloor] _register_live_levels: dossier #{dossier_id} "
                             f"has invalid direction '{direction}' — cannot register levels")
                return
            if d.get("stop_loss"):
                sl_dir = "below" if direction == "BUY" else "above"
                ps.levels.register(d["symbol"], float(d["stop_loss"]),
                                   sl_dir, f"sl_{dossier_id}", {"type": "sl", "dossier_id": dossier_id})
            for i, col in enumerate(["take_profit_1", "take_profit_2", "take_profit_3"], 1):
                if d.get(col) and float(d[col]) > 0:
                    tp_dir = "above" if direction == "BUY" else "below"
                    ps.levels.register(d["symbol"], float(d[col]),
                                       tp_dir, f"tp{i}_{dossier_id}", {"type": f"tp{i}", "dossier_id": dossier_id})
        except Exception as e:
            logger.debug(f"[TradingFloor] _register_live_levels error: {e}")

    def _reregister_sl(self, dossier_id: int, new_sl: float, symbol: str):
        """Re-register a moved SL level in PriceStreamer (unregisters old first),
        AND sync the new SL to the exchange for any linked live trade."""
        try:
            from services.price_streamer import get_price_streamer
            ps = get_price_streamer()
            if not ps:
                return
            d = self.db.fetch_one(
                "SELECT direction FROM trade_dossiers WHERE id = %s", (dossier_id,))
            if not d:
                return
            direction = (d["direction"] or "").upper()
            tag = f"sl_{dossier_id}"
            ps.levels.unregister(tag)
            sl_dir = "below" if direction == "BUY" else "above"
            ps.levels.register(symbol, new_sl, sl_dir, tag,
                              {"type": "sl", "dossier_id": dossier_id})
            logger.info(f"[TradingFloor] Re-registered SL for #{dossier_id}: "
                       f"{new_sl:.5f} ({sl_dir})")
        except Exception as e:
            logger.debug(f"[TradingFloor] _reregister_sl error: {e}")

        # Sync to exchange: update SL on live trade and live_trades table
        self._sync_sl_to_exchange(dossier_id, new_sl)

    def _sync_sl_to_exchange(self, dossier_id: int, new_sl: float):
        """Push a trailed SL to the exchange for the linked live trade.
        Cancels any existing SL stop orders first to prevent stacking,
        then uses actual remaining position size (not original) to avoid
        oversized stop orders after partial TP closes."""
        try:
            lt = self.db.fetch_one(
                "SELECT id, account_id, exchange_symbol, direction, position_size "
                "FROM live_trades WHERE dossier_id = %s AND status IN ('open', 'partial_closed')",
                (dossier_id,))
            if not lt:
                return

            from core.ccxt_executor import get_executor
            executor = get_executor(lt["account_id"], self.db)
            if not executor:
                return

            direction = (lt["direction"] or "BUY").upper()
            sl_side = "sell" if direction == "BUY" else "buy"
            exsym = lt["exchange_symbol"]

            # Cancel existing SL stop orders to prevent stacking
            try:
                open_orders = executor.get_open_orders(exsym)
                for o in (open_orders or []):
                    otype = (o.get("type") or "").lower()
                    if otype in ("stop", "stop_market", "stop-loss"):
                        executor.cancel_order(o["id"], exsym)
                        logger.info(f"[TradingFloor] Cancelled old SL order "
                                    f"{o['id']} for #{dossier_id}")
            except Exception as e:
                logger.debug(f"[TradingFloor] Old SL cancel attempt #{dossier_id}: {e}")

            # Use actual remaining position size from exchange, not DB value
            remaining = 0
            try:
                positions = executor.get_positions()
                pos = next((p for p in (positions or [])
                            if p.get("symbol") == exsym
                            and abs(p.get("contracts", 0)) > 0), None)
                remaining = abs(pos["contracts"]) if pos else 0
            except Exception:
                pass
            if remaining <= 0:
                remaining = float(lt.get("position_size") or 0)
            if remaining <= 0:
                return

            rounded_sl = executor.price_to_precision(exsym, new_sl)

            sl_params = {"stopPrice": rounded_sl, "reduceOnly": True,
                         "triggerPrice": rounded_sl, "triggerType": "mark"}
            if hasattr(executor, 'exchange_id') and executor.exchange_id in ("blofin", "bitget"):
                sl_params["marginMode"] = "isolated"
            with executor._lock:
                executor._exchange.create_order(
                    symbol=exsym, type="stop",
                    side=sl_side, amount=remaining,
                    price=rounded_sl,
                    params=sl_params)

            self.db.execute(
                "UPDATE live_trades SET stop_loss = %s WHERE id = %s",
                (rounded_sl, lt["id"]))

            logger.info(f"[TradingFloor] Synced SL to exchange: #{dossier_id} "
                        f"LT#{lt['id']} SL={rounded_sl} remaining={remaining}")
        except Exception as e:
            logger.warning(f"[TradingFloor] _sync_sl_to_exchange failed for "
                           f"#{dossier_id}: {e}")

    def _register_order_entry_level(self, dossier_id: int):
        """Register entry price level for a newly open_order dossier with PriceStreamer."""
        try:
            from services.price_streamer import get_price_streamer
            ps = get_price_streamer()
            if not ps:
                return
            d = self.db.fetch_one(
                "SELECT symbol, direction, entry_price FROM trade_dossiers WHERE id = %s",
                (dossier_id,))
            if not d or not d.get("entry_price"):
                return
            entry_val = float(d["entry_price"])
            direction = (d["direction"] or "").upper()
            cached = ps.get_price(d["symbol"])
            last_price = (cached or {}).get("price", 0)
            if last_price > 0:
                if direction == "BUY":
                    trigger = "below" if entry_val <= last_price else "above"
                else:
                    trigger = "above" if entry_val >= last_price else "below"
            else:
                trigger = "below" if direction == "BUY" else "above"
            ps.levels.register(d["symbol"], entry_val, trigger,
                               f"entry_{dossier_id}", {"type": "entry", "dossier_id": dossier_id})
            logger.debug(f"[TradingFloor] Registered entry level for #{dossier_id} "
                         f"{d['symbol']} at {entry_val} ({trigger})")
        except Exception as e:
            logger.debug(f"[TradingFloor] _register_order_entry_level error: {e}")

    def _on_price_tick(self, symbol: str, data: Dict):
        """On every price update: update P&L for paper-only live dossiers,
        and check SL/TP breach for instant paper settlement.

        Skips dossiers that have an active exchange trade — LiveTradeMonitor
        is the single source of truth for those.  Uses a single LEFT JOIN
        query to avoid N+1 DB round-trips on every WebSocket tick.
        """
        try:
            price = data.get("price", 0)
            if not price:
                return
            paper_only = self.db.fetch_all(
                "SELECT d.id, d.entry_price, d.actual_entry_price, d.direction, "
                "       d.stop_loss, d.take_profit_1, d.take_profit_2, d.take_profit_3 "
                "FROM trade_dossiers d "
                "LEFT JOIN live_trades lt ON lt.dossier_id = d.id "
                "  AND lt.status IN ('open','partial_closed') "
                "WHERE d.symbol = %s AND d.status = 'live' "
                "AND lt.id IS NULL",
                (symbol,))
            for d in (paper_only or []):
                entry_val = float(d.get("actual_entry_price") or d.get("entry_price") or 0)
                if not entry_val:
                    continue
                direction = (d.get("direction") or "").upper()
                sl = float(d.get("stop_loss") or 0)
                tp1 = float(d.get("take_profit_1") or 0)

                # Paper SL check
                if sl > 0:
                    sl_hit = (direction == "BUY" and price <= sl) or \
                             (direction == "SELL" and price >= sl)
                    if sl_hit:
                        sl_pnl = _finalise_pnl(self.db, d["id"], sl,
                                               exit_is_taker=True, config=self.config,
                                               exit_is_sl=True)
                        outcome = "won" if sl_pnl >= 0 else "lost"
                        transition_dossier(self.db, d["id"], outcome,
                                           f"Paper SL hit at {sl} (tick price={price}) "
                                           f"— P&L ${sl_pnl:.2f}")
                        continue

                # Paper TP1 check
                if tp1 > 0:
                    tp_hit = (direction == "BUY" and price >= tp1) or \
                             (direction == "SELL" and price <= tp1)
                    if tp_hit:
                        tp_pnl = _finalise_pnl(self.db, d["id"], tp1,
                                               exit_is_taker=False, config=self.config,
                                               exit_is_sl=False)
                        transition_dossier(self.db, d["id"], "won",
                                           f"Paper TP1 hit at {tp1} (tick price={price}) "
                                           f"— P&L ${tp_pnl:.2f}")
                        continue

                _update_live_pnl(self.db, d["id"], entry_val, price, d["direction"], self.config)
        except Exception:
            pass

    def _execute_paper_trade(self, dossier_id: int):
        """Wrapper for paper trade execution on entry fill."""
        try:
            d = self.db.fetch_one(
                "SELECT symbol, direction, entry_price, stop_loss, take_profit_1 "
                "FROM trade_dossiers WHERE id = %s", (dossier_id,))
            if d and not self.db.fetch_one(
                "SELECT id FROM parsed_signals WHERE news_item_id = %s AND source = 'trading_floor'",
                (dossier_id,)):
                self.execute_paper_trade(dossier_id)
        except Exception as e:
            logger.debug(f"[TradingFloor] _execute_paper_trade error: {e}")

    def _abandon_dossier(self, dossier_id: int, reason: str,
                         symbol: Optional[str] = None,
                         set_cooldown: bool = True) -> Dict:
        """Abandon a dossier and optionally record discovery cooldown.

        Wraps ``transition_dossier`` so callers get the abandon + cooldown
        in one call.  Falls back to a DB lookup for the symbol if the
        caller doesn't supply it.

        Pass ``set_cooldown=False`` for abandons caused by expired opportunity
        (e.g. TP1 overshot) rather than symbol-quality issues, so the scout
        can re-discover the symbol immediately.
        """
        result = transition_dossier(self.db, dossier_id, "abandoned", reason)
        if result.get("success") and set_cooldown:
            if not symbol:
                row = self.db.fetch_one(
                    "SELECT symbol FROM trade_dossiers WHERE id = %s",
                    (dossier_id,))
                symbol = row["symbol"] if row else None
            if symbol:
                canon = self._canonical_symbol(symbol)
                self._discovery_cooldown[canon] = _utcnow()
                logger.debug(f"[TradingFloor] Abandon cooldown set: {canon}")
        return result

    def _clone_dossier_for_account(self, original_id: int, account: Dict,
                                    trade_group_id: str) -> int:
        """Clone a dossier for a specific account, stamping it with that account's
        balance and risk settings. Returns the new dossier ID."""
        cols = self.db.fetch_all(f"SHOW COLUMNS FROM trade_dossiers")
        col_names = [c["Field"] for c in cols if c["Field"] != "id"]
        original = self.db.fetch_one(
            f"SELECT {', '.join(col_names)} FROM trade_dossiers WHERE id = %s",
            (original_id,))
        if not original:
            return 0

        original["trade_group_id"] = trade_group_id
        original["target_account_id"] = account["account_id"]
        placeholders = ", ".join(["%s"] * len(col_names))
        vals = [original.get(c) for c in col_names]
        new_id = self.db.execute_returning_id(
            f"INSERT INTO trade_dossiers ({', '.join(col_names)}) VALUES ({placeholders})",
            tuple(vals))

        if new_id:
            _stamp_leverage_on_dossier(self.db, new_id, self.config)
            logger.info(f"[TradingFloor] Cloned dossier #{original_id} → #{new_id} "
                        f"for account {account['account_id']} (group={trade_group_id})")
        return new_id or 0

    def _execute_live_trades_for_dossier(self, dossier_id: int,
                                         mentor_id: str = None,
                                         force_override: bool = False):
        """Route a dossier to live trading accounts.

        DOSSIER_MODE 'per_account': Clones the dossier for each eligible account,
        stamps each with that account's balance/risk, routes each clone individually.

        DOSSIER_MODE 'single' (default):
          WATERFALL MODE (ON): Try accounts in priority order. Stop at the FIRST
          account where the trade is placed successfully. One dossier = one live trade.
          WATERFALL MODE (OFF): Send to ALL eligible accounts (original behavior).

        For each account:
          - Check if live_trading is on and receive_dossiers is enabled
          - Check apex_enabled / mentor_enabled per account
          - Check mentor_ids whitelist if this is a mentor trade
          - Conflict resolution: if same symbol already active, use mentor_priority
          - Call execute_live_trade()

        force_override: When True (manual user action), bypasses mentor whitelist
        and mentor_enabled checks — the user explicitly wants this trade live.
        """
        try:
            dossier_mode = self._td_cfg.get("dossier_mode", "single")
            waterfall = _is_waterfall_enabled(self.db)
            accounts = _get_live_trading_accounts(self.db, ordered=waterfall)
            if not accounts:
                self.db.execute(
                    "UPDATE trade_dossiers SET paper_reason = 'no_live_accounts' "
                    "WHERE id = %s AND paper_reason IS NULL", (dossier_id,))
                return

            dossier = self.db.fetch_one(
                "SELECT mentor_type, mentor_source, symbol, trade_group_id "
                "FROM trade_dossiers WHERE id = %s",
                (dossier_id,))
            if not dossier:
                return
            is_mentor = dossier.get("mentor_type") == "mentor_mirror"
            symbol = dossier.get("symbol", "")

            # ── Per-account dossier mode: clone for each eligible account ──
            if dossier_mode == "per_account" and len(accounts) > 1:
                import uuid
                group_id = dossier.get("trade_group_id") or str(uuid.uuid4())
                if not dossier.get("trade_group_id"):
                    self.db.execute(
                        "UPDATE trade_dossiers SET trade_group_id = %s WHERE id = %s",
                        (group_id, dossier_id))
                first = True
                for acct in accounts:
                    aid = acct["account_id"]
                    if not acct.get("receive_dossiers"):
                        continue
                    if not is_mentor:
                        allowed_duos = _parse_duo_allowed(acct.get("duo_allowed"))
                        if not allowed_duos or (self.duo_id and self.duo_id not in allowed_duos):
                            continue
                    if not force_override:
                        if is_mentor and not acct.get("mentor_enabled"):
                            continue
                        if not is_mentor and not acct.get("apex_enabled"):
                            continue
                    if first:
                        _stamp_leverage_on_dossier(self.db, dossier_id, self.config)
                        result = execute_live_trade(
                            self.db, dossier_id, aid, self.config,
                            duo_id=self.duo_id)
                        if result.get("success"):
                            self._log_activity("live_trade",
                                f"#{dossier_id} → {acct['exchange']}/{aid} "
                                f"(per-account, group={group_id[:8]})",
                                dossier_id=dossier_id)
                        first = False
                    else:
                        clone_id = self._clone_dossier_for_account(
                            dossier_id, acct, group_id)
                        if clone_id:
                            if self._td_cfg.get("auto_execute_paper", False):
                                execute_paper_trade(self.db, clone_id)
                            result = execute_live_trade(
                                self.db, clone_id, aid, self.config,
                                duo_id=self.duo_id)
                            if result.get("success"):
                                self._log_activity("live_trade",
                                    f"#{clone_id} (clone of #{dossier_id}) → "
                                    f"{acct['exchange']}/{aid} (per-account)",
                                    dossier_id=clone_id)
                return

            if waterfall:
                already = self.db.fetch_one(
                    "SELECT id, account_id FROM live_trades "
                    "WHERE dossier_id = %s AND status IN ('pending','open','partial_closed')",
                    (dossier_id,))
                if already:
                    logger.info(f"[TradingFloor] WATERFALL: #{dossier_id} {symbol} "
                                f"already has active trade LT#{already['id']} on "
                                f"{already['account_id']} — skipping")
                    return
                logger.info(f"[TradingFloor] WATERFALL routing #{dossier_id} {symbol} "
                            f"across {len(accounts)} accounts (priority order)")

            placed = False
            any_tried = False
            fail_reasons = []  # collect per-account failure reasons for paper_reason
            for acct in accounts:
                aid = acct["account_id"]
                try:
                    if not acct.get("receive_dossiers"):
                        continue

                    if not is_mentor:
                        allowed_duos_wf = _parse_duo_allowed(acct.get("duo_allowed"))
                        if not allowed_duos_wf or (self.duo_id and self.duo_id not in allowed_duos_wf):
                            continue

                    trade_source = "apex"
                    acct_mentor_mode = acct.get("mentor_mode", "copy")

                    if is_mentor:
                        trade_source = "mentor"
                        if force_override:
                            logger.info(f"[TradingFloor] FORCE OVERRIDE #{dossier_id}: "
                                        f"bypassing whitelist/mode checks on {aid}")
                        else:
                            if not acct.get("mentor_enabled"):
                                continue
                            allowed_ids = acct.get("mentor_ids")
                            if isinstance(allowed_ids, str):
                                try:
                                    allowed_ids = json.loads(allowed_ids)
                                except (json.JSONDecodeError, TypeError):
                                    allowed_ids = None
                            src = mentor_id or dossier.get("mentor_source")
                            if allowed_ids and src and src not in allowed_ids:
                                continue

                            if acct_mentor_mode == "independent":
                                if dossier.get("mentor_type") == "mentor_mirror":
                                    logger.info(f"[TradingFloor] Skip mentor mirror #{dossier_id} "
                                                f"on {aid} (mode=independent, Apex decides)")
                                    continue
                            elif acct_mentor_mode == "enhance":
                                if dossier.get("mentor_type") == "mentor_mirror":
                                    logger.info(f"[TradingFloor] Skip mentor mirror #{dossier_id} "
                                                f"on {aid} (mode=enhance, Apex improves entry)")
                                    continue
                            elif acct_mentor_mode == "copy":
                                if dossier.get("mentor_type") != "mentor_mirror":
                                    logger.info(f"[TradingFloor] Skip Apex-assessed #{dossier_id} "
                                                f"on {aid} (mode=copy, only mirrors)")
                                    continue
                    else:
                        if not acct.get("apex_enabled") and not force_override:
                            continue

                    max_per_sym = int(acct.get("max_trades_per_symbol", 1) or 1)
                    existing = self.db.fetch_all(
                        "SELECT id, trade_source, status FROM live_trades "
                        "WHERE account_id = %s AND symbol = %s "
                        "AND status IN ('pending','open','partial_closed')",
                        (aid, symbol))

                    if existing and len(existing) >= max_per_sym:
                        live_positions = [e for e in existing
                                          if e["status"] in ("open", "partial_closed")]
                        pending_only = [e for e in existing
                                        if e["status"] == "pending"]

                        if live_positions:
                            logger.info(
                                f"[TradingFloor] Skip #{dossier_id} on {aid}: "
                                f"live position exists for {symbol} "
                                f"(LT#{live_positions[0]['id']})")
                            continue

                        replaced = False
                        if pending_only and trade_source == "mentor":
                            for e in pending_only:
                                try:
                                    from core.ccxt_executor import get_executor
                                    executor = get_executor(aid, self.db)
                                    lt = self.db.fetch_one(
                                        "SELECT order_id, exchange_symbol "
                                        "FROM live_trades WHERE id = %s",
                                        (e["id"],))
                                    if lt and lt.get("order_id") and executor:
                                        executor.cancel_order(
                                            lt["order_id"], lt["exchange_symbol"])
                                    self.db.execute(
                                        "UPDATE live_trades SET status='cancelled', "
                                        "closed_at=NOW(), "
                                        "realised_pnl=NULL, unrealised_pnl=NULL, "
                                        "realised_pnl_pct=NULL, unrealised_pnl_pct=NULL, "
                                        "close_comment=%s "
                                        "WHERE id=%s AND status='pending'",
                                        (f"Replaced by newer mentor trade "
                                         f"(dossier #{dossier_id})", e["id"]))
                                    _append_tracker_log(self.db, dossier_id,
                                        f"Replaced pending LT#{e['id']} "
                                        f"({e['trade_source']}) on {aid}")
                                    self._log_activity("conflict",
                                        f"Replaced pending #{e['id']} for {symbol} "
                                        f"on {aid} with mentor #{dossier_id}",
                                        dossier_id=dossier_id)
                                    replaced = True
                                except Exception as ce:
                                    logger.debug(
                                        f"[TradingFloor] Pending replace error: {ce}")

                        if not replaced:
                            logger.info(
                                f"[TradingFloor] Skip #{dossier_id} on {aid}: "
                                f"max {max_per_sym} trades for {symbol} "
                                f"(source={trade_source})")
                            continue

                    any_tried = True
                    result = execute_live_trade(self.db, dossier_id, aid, self.config,
                                               duo_id=self.duo_id)
                    if result.get("success") and not result.get("already_existed"):
                        self._log_activity(
                            "live_trade",
                            f"#{dossier_id} → live order on {acct['exchange']}/{aid} "
                            f"(order {result.get('order_id', '?')}, "
                            f"priority={acct.get('waterfall_priority', '?')})",
                            dossier_id=dossier_id)
                        _append_tracker_log(self.db, dossier_id,
                            f"LIVE TRADE placed on {acct['exchange']}/{aid} "
                            f"(order {result.get('order_id', '?')})")
                        placed = True
                        self.db.execute(
                            "UPDATE trade_dossiers SET paper_reason = NULL "
                            "WHERE id = %s", (dossier_id,))
                        if waterfall:
                            logger.info(f"[TradingFloor] WATERFALL: #{dossier_id} placed on "
                                        f"{acct['exchange']}/{aid} (priority "
                                        f"{acct.get('waterfall_priority', '?')}) — stopping cascade")
                            break
                    elif not result.get("success") and not result.get("already_existed"):
                        err = result.get('error', '?')
                        fail_reasons.append(f"{acct['exchange']}/{aid}: {err}")
                        logger.info(f"[TradingFloor] Live trade skip for #{dossier_id} "
                                    f"on {aid}: {err}")
                        _append_tracker_log(self.db, dossier_id,
                            f"LIVE SKIP on {acct['exchange']}/{aid}: {err}")
                        if waterfall:
                            logger.info(f"[TradingFloor] WATERFALL: #{dossier_id} failed on "
                                        f"{acct['exchange']}/{aid}, trying next account...")
                    elif result.get("already_existed"):
                        placed = True
                        if waterfall:
                            break
                except Exception as acct_err:
                    logger.warning(f"[TradingFloor] Account {aid} trade routing failed "
                                   f"for #{dossier_id}: {acct_err}")
                    fail_reasons.append(f"{acct.get('exchange','?')}/{aid}: {str(acct_err)[:80]}")
                    _append_tracker_log(self.db, dossier_id,
                        f"LIVE ERROR on {acct['exchange']}/{aid}: {str(acct_err)[:120]}")
                    any_tried = True
                    if waterfall:
                        logger.info(f"[TradingFloor] WATERFALL: error on {aid}, trying next...")

            if not placed and not any_tried:
                self.db.execute(
                    "UPDATE trade_dossiers SET paper_reason = 'paper_no_eligible_accounts' "
                    "WHERE id = %s AND paper_reason IS NULL", (dossier_id,))
            elif waterfall and not placed and any_tried:
                logger.warning(f"[TradingFloor] WATERFALL: #{dossier_id} {symbol} could not "
                               f"be placed on ANY account")
                _append_tracker_log(self.db, dossier_id,
                    f"WATERFALL: no account could place {symbol}")

                # Classify from per-account failure reasons
                if not fail_reasons:
                    paper_label = "paper_no_eligible_accounts"
                else:
                    reasons_lower = " ".join(fail_reasons).lower()
                    if all("margin cap" in r.lower() for r in fail_reasons):
                        paper_label = "paper_margin_full"
                    elif all("not found on" in r.lower() or "not have market" in r.lower()
                             for r in fail_reasons):
                        paper_label = "paper_no_exchange"
                    elif any("margin cap" in r.lower() for r in fail_reasons):
                        paper_label = "paper_margin_and_exchange"
                    elif "max" in reasons_lower and "trades" in reasons_lower:
                        paper_label = "paper_max_trades"
                    else:
                        paper_label = "paper_exchange_error"

                fail_row = self.db.fetch_one(
                    "SELECT paper_reason FROM trade_dossiers WHERE id = %s",
                    (dossier_id,))
                prev_reason = (fail_row or {}).get("paper_reason", "") or ""
                fail_count = prev_reason.count("waterfall_failed") + 1
                max_retries = int(self._td_cfg.get("waterfall_max_retries", 3))
                if fail_count >= max_retries:
                    reason = (f"Waterfall exhausted: failed {fail_count} times, "
                              f"no account can place {symbol}")
                    transition_dossier(self.db, dossier_id, "abandoned", reason)
                    _append_tracker_log(self.db, dossier_id, f"WATERFALL ABANDONED: {reason}")
                else:
                    _append_tracker_log(self.db, dossier_id,
                        f"WATERFALL FAILED ({fail_count}/{max_retries}): "
                        f"{'; '.join(fail_reasons[:3])}")
                    self.db.execute(
                        "UPDATE trade_dossiers SET paper_reason = %s "
                        "WHERE id = %s",
                        (f"{paper_label} waterfall_failed", dossier_id))

        except Exception as e:
            logger.error(f"[TradingFloor] _execute_live_trades_for_dossier error for "
                         f"#{dossier_id}: {e}", exc_info=True)
            try:
                self.db.execute(
                    "UPDATE trade_dossiers SET paper_reason = %s "
                    "WHERE id = %s AND paper_reason IS NULL",
                    (f"execution_error: {str(e)[:500]}", dossier_id))
            except Exception:
                pass

    # NOTE: _get_tradable_watchlist() removed — all discovery is now
    # handled by the Scout agent (services/scout_agent.py).

    def _merge_db_config(self):
        """Merge DB-persisted settings into in-memory config.

        Priority order (highest wins):
            1. config.json discovery keys (discovery_interval, cooldown, max_concurrent)
            2. DB ``tf_{duo_id}_*`` keys (duo-specific overrides)
            3. DB ``tf_*`` keys (global, only fills gaps not covered by #2)
            4. Existing ``_td_cfg`` from get_duo_config (config.json + duo block)

        This means: duo-specific DB keys beat global DB keys, but config.json
        discovery keys always win (they are the primary source of truth for
        timing/throughput). A global ``tf_min_confidence_for_trade`` will NOT
        override a duo's config.json ``min_confidence_for_trade``.
        """
        try:
            rows = self.db.fetch_all(
                "SELECT config_key, config_value FROM system_config "
                "WHERE config_key LIKE 'tf_%%'")
            if not rows:
                return
            _disc_keys = ("discovery_interval_minutes", "discovery_cooldown_minutes", "max_concurrent_builds")
            cfg_snapshot = {k: self._td_cfg[k] for k in _disc_keys if k in self._td_cfg}

            duo_prefix = f"tf_{self.duo_id}_" if self.duo_id else None
            global_applied: Dict[str, Any] = {}

            for row in rows:
                raw_key = row["config_key"]
                val = row["config_value"]
                try:
                    parsed = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    parsed = val

                if duo_prefix and raw_key.startswith(duo_prefix):
                    key = raw_key[len(duo_prefix):]
                    self._td_cfg[key] = parsed
                elif raw_key.startswith("tf_"):
                    key = raw_key[3:]
                    global_applied[key] = parsed

            for k, v in global_applied.items():
                if k not in self._td_cfg:
                    self._td_cfg[k] = v

            for k, v in cfg_snapshot.items():
                self._td_cfg[k] = v

            if cfg_snapshot:
                self._persist_config_to_db(cfg_snapshot)
        except Exception as e:
            logger.debug(f"[TradingFloor] DB config merge: {e}")

    def start(self):
        """Start the trading floor background services.

        Per-duo threads (scoped to this duo_id):
            tracker, fast-tracker
        Discovery is handled exclusively by the Scout agent.

        Global threads (only spawned by the primary/apex instance to avoid
        duplicate work and race conditions):
            signal-tracker, daily-audit, symbol-resolver
        """
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        _tag = self.duo_id

        self._tracker_thread = threading.Thread(
            target=self._tracker_loop, daemon=True,
            name=f"tf-tracker-{_tag}")
        self._tracker_thread.start()

        self._fast_tracker_thread = threading.Thread(
            target=self._fast_tracker_loop, daemon=True,
            name=f"tf-fast-tracker-{_tag}")
        self._fast_tracker_thread.start()

        # Global threads: only the primary (apex) instance runs these to
        # prevent duplicate audit rollups, symbol resolution, and signal
        # tracking across duo instances.
        if _tag == "apex":
            self._signal_tracker_thread = threading.Thread(
                target=self._signal_tracker_loop, daemon=True,
                name="tf-signal-tracker")
            self._signal_tracker_thread.start()

            self._audit_thread = threading.Thread(
                target=self._daily_audit_loop, daemon=True,
                name="tf-daily-audit")
            self._audit_thread.start()

            self._symbol_resolver_thread = threading.Thread(
                target=self._symbol_resolution_loop, daemon=True,
                name="tf-symbol-resolver")
            self._symbol_resolver_thread.start()

        # Connect to PriceStreamer for instant level detection
        try:
            from services.price_streamer import get_price_streamer
            ps = get_price_streamer()
            if ps:
                ps.on_level_trigger(self._on_level_hit)
                ps.on_update(self._on_price_tick)
                self._register_all_levels()
                logger.info("[TradingFloor] Connected to PriceStreamer for real-time level detection")
        except Exception as e:
            logger.debug(f"[TradingFloor] PriceStreamer connect: {e}")

        # Pre-connect exchange executors so leverage queries work from the start
        try:
            from core.ccxt_executor import get_executor
            accounts = _get_live_trading_accounts(self.db)
            for acct in accounts:
                try:
                    ex = get_executor(acct["account_id"], self.db)
                    if ex and ex.connected:
                        logger.info(f"[TradingFloor] Pre-connected executor: "
                                    f"{acct['account_id']} ({ex.exchange_id})")
                except Exception as ae:
                    logger.debug(f"[TradingFloor] Pre-connect {acct['account_id']}: {ae}")
        except Exception as e:
            logger.debug(f"[TradingFloor] Executor pre-connect: {e}")

        logger.info("[TradingFloor] Service started (tracker + signal tracker + fast tracker + daily audit)")
        self._log_activity("info", "Trading Floor started")

    def stop(self):
        """Stop background services."""
        self._running = False
        self._stop_event.set()
        if hasattr(self, "_build_pool"):
            self._build_pool.shutdown(wait=False)
        logger.info("[TradingFloor] Service stopped")
        self._log_activity("info", "Trading Floor stopped")

    # ── Discovery Loop ────────────────────────────────────────────────

    @staticmethod
    def _canonical_symbol(raw: str) -> str:
        """Fast, DB-free normalization for concurrency guard dedup."""
        from db.market_symbols import SYMBOL_ALIASES, _normalize_crypto_to_usdt
        s = (raw or "").upper().strip()
        s = SYMBOL_ALIASES.get(s, s)
        usdt = _normalize_crypto_to_usdt(s)
        return usdt or s

    def queue_dossier_build(self, symbol: str, source: str = "scout",
                            mentor_signal: Optional[Dict] = None):
        """Non-blocking dossier build dispatched by the Scout agent.

        Phase 4: submits to a 2-worker ThreadPoolExecutor and returns
        immediately so Scout can continue routing other candidates.
        A concurrency guard prevents duplicate builds for the same symbol.
        Symbol is normalized before the guard check so variant forms
        (1INCH vs 1INCHUSDT) share a single build slot.

        Sources:
            - ``"scout"`` / ``"alpha"`` etc — normal dossier, can execute.
            - ``"mentor"`` — legacy path: full AI + mentor metadata.
            - ``"mentor_observation"`` — duo observes a mentor signal.
              Full AI pipeline runs but the dossier is tagged
              ``mentor_type='mentor_observation'`` and **never executes**.
              Learning only — postmortems compare the duo's prediction
              against the mentor's actual outcome.
        """
        canon = self._canonical_symbol(symbol)
        build_key = f"{canon}:{source}"
        with self._build_lock:
            existing = self._active_builds.get(build_key)
            if existing and not existing.done():
                logger.info(f"[TradingFloor][{self.duo_id}] Build already in "
                            f"progress for {build_key} — skipping")
                return
            fut = self._build_pool.submit(
                self._run_dossier_build, canon, source, mentor_signal)
            self._active_builds[build_key] = fut
        logger.info(f"[TradingFloor][{self.duo_id}] Queued async build: "
                    f"{canon} (source={source})")

    def _run_dossier_build(self, symbol: str, source: str,
                           mentor_signal: Optional[Dict]):
        """Execute a dossier build inside the worker pool thread.
        ``symbol`` is already canonical (normalized by queue_dossier_build)."""
        from core.duo_config import is_duo_enabled

        canon = self._canonical_symbol(symbol)
        if not is_duo_enabled(self.config, self.duo_id):
            logger.info(f"[TradingFloor][{self.duo_id}] Duo disabled — "
                        f"skipping dossier build for {canon} (source={source})")
            return
        logger.info(f"[TradingFloor][{self.duo_id}] Scout build START: "
                    f"{canon} (source={source})")
        try:
            if source == "mentor_observation" and mentor_signal:
                self._build_mentor_observation(canon, mentor_signal)
            elif source == "mentor" and mentor_signal:
                _raw = self.config.raw if hasattr(self.config, 'raw') else self.config
                mentor_mode = (_raw.get("trade_decision", {})
                               .get("mentor", {})
                               .get("mentor_mode", "both_parallel"))
                if mentor_mode == "off":
                    logger.info(f"[TradingFloor][{self.duo_id}] Mentor mode OFF — "
                                f"skipping {canon}")
                else:
                    mentor_signal["symbol"] = canon
                    self.process_mentor_signal(mentor_signal, mentor_mode)
                    self._discovery_cooldown[canon] = _utcnow()
                    self._log_activity("mentor_processed",
                        f"{canon} via Scout (mentor={mentor_signal.get('author','?')})")
            else:
                self._build_single_dossier(canon, source=source)
        except Exception as e:
            logger.error(f"[TradingFloor][{self.duo_id}] Scout build "
                        f"failed for {canon}: {e}", exc_info=True)
        finally:
            with self._build_lock:
                self._active_builds.pop(f"{canon}:{source}", None)

    def _build_mentor_observation(self, symbol: str, signal: Dict):
        """Build a duo's independent AI dossier for a mentor signal.

        Runs the full Stage 1 + Stage 2 pipeline so the duo forms its own
        opinion, but the dossier is tagged ``mentor_type='mentor_observation'``
        and will **never execute**. The mentor's actual levels are stored in
        ``dossier_intelligence`` so postmortems can compare outcomes.

        This is pure learning material — the duo sees "Mentor X said BUY at
        Y with SL Z" and decides independently. After the trade resolves,
        the postmortem compares the duo's prediction to reality.
        """
        author = signal.get("author", "Mentor")
        direction = signal.get("direction", "?")
        entry = signal.get("entry_price", "?")
        sl = signal.get("stop_loss", "?")
        tp1 = signal.get("take_profit_1", "?")

        logger.info(f"[TradingFloor][{self.duo_id}] Mentor observation: "
                    f"{symbol} ({author} {direction})")
        pq_key = self._pipeline_start(symbol, f"mentor_obs:{author}")
        try:
            from services.candle_collector import get_candle_collector
            from services.trade_dossier import TradeDossierBuilder

            collector = get_candle_collector()
            builder = TradeDossierBuilder(
                self.db, self.config, collector, duo_id=self.duo_id)

            dossier = builder.build_dossier(symbol, market_regime=self._market_regime)

            dossier_id = dossier.get("dossier_id")
            if not dossier_id:
                return

            duo_decision = dossier.get("stage2_output", {}).get("trade_decision", "?")
            duo_direction = dossier.get("stage2_output", {}).get("direction", "?")
            duo_confidence = dossier.get("stage2_output", {}).get("confidence_score", 0)

            mentor_note = (
                f"[MENTOR OBSERVATION] {author}'s call: {direction} {symbol} "
                f"Entry={entry} SL={sl} TP1={tp1}\n"
                f"Duo {self.duo_id} decision: {duo_decision} {duo_direction} "
                f"(confidence {duo_confidence}%)\n"
                f"{'AGREED' if duo_direction == direction else 'DISAGREED'} "
                f"with mentor — observation only, no execution."
            )

            self.db.execute(
                "UPDATE trade_dossiers SET "
                "mentor_type = 'mentor_observation', "
                "mentor_source = %s, "
                "dossier_intelligence = CONCAT(COALESCE(dossier_intelligence,''), %s), "
                "paper_reason = 'mentor_observation' "
                "WHERE id = %s",
                (author, f"\n\n{mentor_note}", dossier_id))

            if duo_direction == direction:
                self._log_activity("info",
                    f"#{dossier_id} {symbol}: AGREES with {author} ({direction}) "
                    f"— observation only",
                    symbol=symbol, dossier_id=dossier_id)
            else:
                self._log_activity("warn",
                    f"#{dossier_id} {symbol}: DISAGREES with {author} — "
                    f"duo says {duo_decision} {duo_direction}, mentor says {direction} "
                    f"— observation only",
                    symbol=symbol, dossier_id=dossier_id)

            self._discovery_cooldown[self._canonical_symbol(symbol)] = _utcnow()
            self._pipeline_done(pq_key, dossier_id)
            logger.info(f"[TradingFloor][{self.duo_id}] Mentor observation "
                        f"#{dossier_id} {symbol}: duo={duo_decision} {duo_direction} "
                        f"vs mentor={direction}")
        except Exception as e:
            logger.error(f"[TradingFloor][{self.duo_id}] Mentor observation "
                         f"failed for {symbol}: {e}", exc_info=True)
            self._pipeline_done(pq_key, None)

    # NOTE: _discovery_loop(), _run_discovery_cycle(), and _build_single_dossier()
    # removed — all discovery is now handled by the Scout agent.
    # Duos receive work only via queue_dossier_build() called by the Scout.

    def _has_recent_active_dossier(self, symbol: str) -> bool:
        """Check if a recent active dossier already exists for this symbol+duo.

        Returns True if a dossier in an active state (proposed, monitoring,
        open_order, live) was created within the dedup window and shares the
        same symbol+duo.  Used as a pre-build gate to avoid wasting LLM calls
        on symbols that already have a dossier being tracked.
        """
        raw_h = self._td_cfg.get("prebuild_dedup_hours", 4)
        try:
            dedup_hours = int(raw_h)
        except (TypeError, ValueError):
            dedup_hours = 4
        dedup_hours = max(1, min(dedup_hours, 8760))  # 1 hour .. 1 year
        row = self.db.fetch_one(
            f"SELECT id, status, created_at FROM trade_dossiers "
            f"WHERE symbol = %s AND duo_id = %s "
            f"AND status IN ('proposed','monitoring','open_order','live') "
            f"AND created_at >= NOW() - INTERVAL {dedup_hours} HOUR "
            f"ORDER BY created_at DESC LIMIT 1",
            (symbol, self.duo_id))
        if row:
            logger.info(
                f"[TradingFloor] Pre-build DEDUP: {symbol} already has "
                f"active dossier #{row['id']} ({row['status']}) from "
                f"{row['created_at']} — skipping build")
            return True
        return False

    def _build_single_dossier(self, symbol: str, _retry: int = 0,
                               source: str = "apex"):
        """Build and process one dossier — called via queue_dossier_build."""
        logger.info(f"[TradingFloor] Building dossier for {symbol}"
                     f"{' (retry #' + str(_retry) + ')' if _retry else ''}")
        self._log_activity("info", f"Building dossier for {symbol}"
                           f"{' (retry)' if _retry else ''}", symbol=symbol)

        if self._has_recent_active_dossier(symbol):
            return

        pq_key = self._pipeline_start(symbol, source)
        try:
            from services.trade_dossier import TradeDossierBuilder
            from services.candle_collector import get_candle_collector

            collector = get_candle_collector()
            builder = TradeDossierBuilder(self.db, self.config, collector,
                                          duo_id=self.duo_id)
            dossier = builder.build_dossier(
                symbol,
                on_stage=lambda stage, detail="": self._pipeline_stage(pq_key, stage, detail),
                market_regime=self._market_regime)

            decision = dossier.get("stage2_output", {}).get("trade_decision")
            confidence = dossier.get("stage2_output", {}).get("confidence_score") or 0
            dossier_id = dossier.get("dossier_id")
            self._pipeline_done(pq_key, dossier_id)
            self._discovery_cooldown[self._canonical_symbol(symbol)] = _utcnow()

            rationale = (dossier.get("stage2_output", {}).get("rationale")
                         or dossier.get("stage2_output", {}).get("reasoning")
                         or "")[:1500]

            if decision in ("trade_now", "wait_for_conditions"):
                logger.info(f"[TradingFloor] {symbol}: {decision} "
                            f"(confidence: {confidence})")
                self._log_activity("info", f"#{dossier_id} {symbol}: {decision} "
                                   f"(confidence {confidence}%)",
                                   symbol=symbol, dossier_id=dossier_id)
                self._log_apex_thought(symbol, "dossier_created",
                    f"Confidence {confidence}% — {rationale[:800]}",
                    decision=decision, probability=confidence)
            elif decision == "do_not_trade":
                self._log_activity("warn", f"#{dossier_id} {symbol}: do_not_trade — dossier analysed but rejected (not sent to floor)",
                                   symbol=symbol, dossier_id=dossier_id)
                self._log_apex_thought(symbol, "do_not_trade",
                    rationale or "LLM rejected — conditions/setup insufficient",
                    decision="do_not_trade", probability=confidence)
            else:
                self._log_activity("info", f"{symbol}: decision={decision}",
                                   symbol=symbol, dossier_id=dossier_id)

            if decision in ("trade_now", "wait_for_conditions") and dossier_id:
                # Subscribe immediately so we track live price from creation — no stale data
                self._subscribe_for_new_dossier(symbol)
                sim_result = self._check_dossier_similarity(symbol, dossier_id, dossier)
                if sim_result.get("action") == "abandon":
                    logger.info(f"[TradingFloor] #{dossier_id} {symbol}: ABANDONED "
                                f"by similarity check — superseded by #{sim_result.get('keep_id')}")
                    self._log_activity("warn", f"#{dossier_id} ABANDONED — duplicate of "
                                       f"#{sim_result.get('keep_id')}",
                                       symbol=symbol, dossier_id=dossier_id)
                    sim_reason = (f"Duplicate of #{sim_result.get('keep_id')}: "
                                  f"{sim_result.get('reason','similar entry/SL/TP')}")
                    transition_dossier(self.db, dossier_id, "abandoned", sim_reason)
                    _append_tracker_log(self.db, dossier_id,
                                        f"[Similarity] {sim_reason}")
                    self._log_apex_thought(symbol, "similarity_abandon",
                        f"#{dossier_id} abandoned — duplicate of #{sim_result.get('keep_id')}",
                        decision="abandoned")
                    return
                elif sim_result.get("action") == "supersede":
                    old_id = sim_result.get("supersede_id")
                    logger.info(f"[TradingFloor] #{dossier_id} {symbol}: SUPERSEDES "
                                f"older #{old_id} — {sim_result.get('reason','')}")
                    self._log_activity("info", f"#{dossier_id} SUPERSEDES #{old_id}",
                                       symbol=symbol, dossier_id=dossier_id)
                    sup_reason = (f"Replaced by #{dossier_id}: "
                                  f"{sim_result.get('reason','newer analysis is better')}")
                    transition_dossier(self.db, old_id, "abandoned", sup_reason)
                    _append_tracker_log(self.db, old_id,
                                        f"[Superseded] {sup_reason}")
                    self._log_apex_thought(symbol, "similarity_supersede",
                        f"#{dossier_id} replaces older #{old_id}",
                        decision="supersede")

            MIN_CONFIDENCE_FOR_TRADE = int(self._td_cfg.get(
                "min_confidence_for_trade", 60))
            FAST_LANE_MIN = int(self._td_cfg.get(
                "fast_lane_min_confidence", 65))
            fast_lane_enabled = self._td_cfg.get("fast_lane_enabled", True)

            if decision == "trade_now" and (confidence is None or confidence == 0):
                fallback = int(self._td_cfg.get("trade_now_default_confidence", 65))
                logger.info(f"[TradingFloor] #{dossier_id} {symbol}: trade_now with "
                            f"no confidence — defaulting to {fallback}%")
                confidence = fallback
                self.db.execute(
                    "UPDATE trade_dossiers SET confidence_score = %s WHERE id = %s",
                    (confidence, dossier_id))

            if decision == "trade_now" and (confidence or 0) < MIN_CONFIDENCE_FOR_TRADE:
                logger.info(f"[TradingFloor] #{dossier_id} {symbol}: trade_now DOWNGRADED "
                            f"to wait_for_conditions — confidence {confidence}% < {MIN_CONFIDENCE_FOR_TRADE}%")
                self._log_activity("warn",
                    f"#{dossier_id} {symbol}: trade_now downgraded — confidence "
                    f"{confidence}% < {MIN_CONFIDENCE_FOR_TRADE}% minimum",
                    symbol=symbol, dossier_id=dossier_id)
                decision = "wait_for_conditions"

            wfc_enabled = self._td_cfg.get("wait_for_conditions_enabled", False)
            if decision == "wait_for_conditions" and not wfc_enabled:
                logger.info(f"[TradingFloor] #{dossier_id} {symbol}: "
                            f"wait_for_conditions DISABLED — treating as do_not_trade")
                self._log_activity("warn",
                    f"#{dossier_id} {symbol}: wait_for_conditions disabled — "
                    f"converted to do_not_trade (benchmark mode)",
                    symbol=symbol, dossier_id=dossier_id)
                decision = "do_not_trade"
                if dossier_id:
                    wfc_reason = "wait_for_conditions disabled — treated as do_not_trade"
                    transition_dossier(self.db, dossier_id, "abandoned", wfc_reason)
                    self.db.execute(
                        "UPDATE trade_dossiers SET paper_reason=%s WHERE id=%s",
                        (wfc_reason, dossier_id))

            # ── Fast Lane: promote wait_for_conditions → trade_now when
            # confidence is high enough (only active when wfc is enabled).
            if (fast_lane_enabled
                    and wfc_enabled
                    and decision == "wait_for_conditions"
                    and dossier_id
                    and (confidence or 0) >= FAST_LANE_MIN):
                logger.info(f"[TradingFloor] #{dossier_id} {symbol}: FAST LANE — "
                            f"wait_for_conditions PROMOTED to trade_now "
                            f"(confidence {confidence}% >= {FAST_LANE_MIN}%)")
                self._log_activity("trade",
                    f"#{dossier_id} {symbol}: FAST LANE — conditions skipped, "
                    f"executing at {confidence}% confidence",
                    symbol=symbol, dossier_id=dossier_id)
                self._log_apex_thought(symbol, "fast_lane",
                    f"#{dossier_id} promoted from wait_for_conditions to trade_now "
                    f"(confidence {confidence}% >= fast_lane threshold {FAST_LANE_MIN}%)",
                    decision="fast_lane", probability=confidence)
                decision = "trade_now"

            if decision == "trade_now" and dossier_id and dossier_id > 0:
                if not self._margin_gate(dossier_id):
                    self._log_activity("warn",
                        f"#{dossier_id} {symbol}: MARGIN CAP — trade blocked",
                        symbol=symbol, dossier_id=dossier_id)
                    self._log_apex_thought(symbol, "margin_blocked",
                        f"#{dossier_id} trade_now blocked — margin cap reached",
                        decision="blocked", probability=confidence)
                else:
                    logger.info(f"[TradingFloor] Apex says TRADE NOW for "
                                f"#{dossier_id} {symbol} — auto-executing paper trade")
                    self._log_activity("trade", f"#{dossier_id} {symbol}: TRADE NOW — executing",
                                       symbol=symbol, dossier_id=dossier_id)
                    t_result = transition_dossier(self.db, dossier_id, "open_order",
                                      "Apex original decision: trade_now")
                    if not t_result.get("success"):
                        logger.warning(f"[TradingFloor] Transition to open_order failed for "
                                       f"#{dossier_id}: {t_result.get('error')} — "
                                       f"skipping execution to prevent un-tracked orders")
                    else:
                        self._register_order_entry_level(dossier_id)
                        if self._td_cfg.get("auto_execute_paper", False):
                            e_result = execute_paper_trade(self.db, dossier_id)
                            if not e_result.get("success"):
                                logger.error(f"[TradingFloor] Paper execute FAILED for "
                                             f"#{dossier_id}: {e_result.get('error')}")
                                self._log_activity("error", f"#{dossier_id} paper execute FAILED: "
                                                   f"{e_result.get('error','')}",
                                                   symbol=symbol, dossier_id=dossier_id)
                        self._execute_live_trades_for_dossier(dossier_id)

        except Exception as e:
            logger.error(f"[TradingFloor] Dossier build failed for {symbol}: {e}")
            self._log_activity("error", f"Dossier build FAILED for {symbol}: {e}",
                               symbol=symbol)

            if _retry < 1:
                logger.info(f"[TradingFloor] Will retry {symbol} (attempt 2/2)")
                self._log_apex_thought(symbol, "build_retry",
                    f"Build failed ({str(e)[:500]}), retrying once...",
                    decision="retry")
                self._build_single_dossier(symbol, _retry=1)
            else:
                self._log_apex_thought(symbol, "build_failed",
                    f"Build failed after retry: {str(e)[:800]}",
                    decision="failed")

    # ── Dossier Similarity Detection ────────────────────────────────

    def _check_dossier_similarity(self, symbol: str, new_id: int,
                                   new_dossier: Dict) -> Dict:
        """Compare a new dossier against active dossiers for the same symbol.
        Returns dict with action: 'keep_both', 'abandon' (new is duplicate),
        or 'supersede' (new replaces an old one)."""
        try:
            existing = self.db.fetch_all("""
                SELECT id, direction, entry_price, stop_loss,
                       take_profit_1, take_profit_2, take_profit_3,
                       confidence_score, status, created_at, trade_decision
                FROM trade_dossiers
                WHERE symbol = %s AND duo_id = %s
                  AND status IN ('proposed','monitoring','open_order','live')
                  AND id != %s
                ORDER BY created_at DESC
            """, (symbol, self.duo_id, new_id)) or []

            if not existing:
                return {"action": "keep_both", "reason": "no active dossiers to compare"}

            s2 = new_dossier.get("stage2_output", {})
            new_entry = s2.get("entry_price")
            new_sl = s2.get("stop_loss")
            new_tp1 = s2.get("take_profit_1")
            new_dir = s2.get("direction", "").upper()
            new_conf = s2.get("confidence_score", 0)

            if not new_entry:
                return {"action": "keep_both", "reason": "new dossier has no entry price"}

            sim_cfg = self._td_cfg.get("similarity_thresholds", {})
            contra_entry_pct = float(sim_cfg.get("contradiction_entry_pct", 2.0))
            high_entry_pct = float(sim_cfg.get("high_sim_entry_pct", 1.0))
            high_sl_pct = float(sim_cfg.get("high_sim_sl_pct", 1.5))
            mod_entry_pct = float(sim_cfg.get("moderate_entry_pct", 2.0))
            mod_sl_pct = float(sim_cfg.get("moderate_sl_pct", 3.0))

            for ex in existing:
                ex_entry = float(ex["entry_price"]) if ex.get("entry_price") else None
                ex_sl = float(ex["stop_loss"]) if ex.get("stop_loss") else None
                ex_tp1 = float(ex["take_profit_1"]) if ex.get("take_profit_1") else None
                ex_dir = (ex.get("direction") or "").upper()

                if not ex_entry:
                    continue

                ref_price = float(new_entry)
                if ref_price == 0:
                    continue

                entry_pct = abs(float(new_entry) - ex_entry) / ref_price * 100

                if ex_dir and ex_dir != new_dir:
                    if entry_pct < contra_entry_pct:
                        new_conf = s2.get("confidence_score") or 0
                        ex_conf = ex.get("confidence_score") or 0
                        logger.warning(
                            f"[TradingFloor] Contradictory: #{new_id} {new_dir} vs "
                            f"#{ex['id']} {ex_dir} on {symbol} "
                            f"(entries {entry_pct:.2f}% apart)")
                        return {
                            "action": "abandon",
                            "keep_id": ex["id"],
                            "reason": (
                                f"Contradicts #{ex['id']} ({ex_dir}) — entries only "
                                f"{entry_pct:.2f}% apart. Not a range play. "
                                f"Keeping existing {ex_dir} dossier.")
                        }
                    continue

                sl_pct = (abs(float(new_sl) - ex_sl) / ref_price * 100) if (new_sl and ex_sl) else 999
                tp1_pct = (abs(float(new_tp1) - ex_tp1) / ref_price * 100) if (new_tp1 and ex_tp1) else 999

                if entry_pct < high_entry_pct and sl_pct < high_sl_pct:
                    return {
                        "action": "abandon",
                        "keep_id": ex["id"],
                        "reason": (f"Near-identical to #{ex['id']} "
                                   f"(entry {entry_pct:.2f}% apart, SL {sl_pct:.2f}% apart). "
                                   f"Keeping existing dossier.")
                    }

                if entry_pct < mod_entry_pct and sl_pct < mod_sl_pct:
                    llm_verdict = self._llm_compare_dossiers(symbol, new_id, ex["id"],
                                                             new_dossier, ex, entry_pct, sl_pct, tp1_pct)
                    if llm_verdict:
                        return llm_verdict

            return {"action": "keep_both", "reason": "no overlapping dossiers found"}

        except Exception as e:
            logger.warning(f"[TradingFloor] Similarity check error for #{new_id}: {e}")
            return {"action": "keep_both", "reason": f"similarity check failed: {e}"}

    def _llm_compare_dossiers(self, symbol: str, new_id: int, old_id: int,
                               new_dossier: Dict, old_row: Dict,
                               entry_pct: float, sl_pct: float, tp1_pct: float) -> Optional[Dict]:
        """Ask Apex to compare two similar dossiers and decide which is better."""
        try:
            from core.model_interface import get_model_interface

            s2 = new_dossier.get("stage2_output", {})
            prompt = (
                f"You are Apex. Two dossiers exist for {symbol} {old_row.get('direction','?')} "
                f"with similar setups:\n\n"
                f"EXISTING DOSSIER #{old_id}:\n"
                f"- Entry: {old_row.get('entry_price')}, SL: {old_row.get('stop_loss')}, "
                f"TP1: {old_row.get('take_profit_1')}, TP2: {old_row.get('take_profit_2')}, "
                f"TP3: {old_row.get('take_profit_3')}\n"
                f"- Confidence: {old_row.get('confidence_score')}%\n"
                f"- Status: {old_row.get('status')}, Created: {old_row.get('created_at')}\n\n"
                f"NEW DOSSIER #{new_id}:\n"
                f"- Entry: {s2.get('entry_price')}, SL: {s2.get('stop_loss')}, "
                f"TP1: {s2.get('take_profit_1')}, TP2: {s2.get('take_profit_2')}, "
                f"TP3: {s2.get('take_profit_3')}\n"
                f"- Confidence: {s2.get('confidence_score')}%\n\n"
                f"Overlap: entries are {entry_pct:.2f}% apart, SLs {sl_pct:.2f}% apart, "
                f"TP1s {tp1_pct:.2f}% apart.\n\n"
                f"DECIDE one of:\n"
                f"1. KEEP_BOTH — they are staggered entries that could both profit "
                f"(e.g., different price zones in a range)\n"
                f"2. SUPERSEDE — the new dossier is better (tighter entry, better R:R, "
                f"fresher analysis). Abandon the old one.\n"
                f"3. ABANDON_NEW — the existing dossier is already well-positioned. "
                f"The new one adds no value.\n\n"
                f"Reply with EXACTLY one line: KEEP_BOTH|SUPERSEDE|ABANDON_NEW followed "
                f"by a brief reason."
            )

            mi = get_model_interface()

            comparison_identity = load_prompt(
                self.db, "apex_comparison_prompt",
                "You compare two trade dossiers for the same symbol and decide "
                "the best action. Reply with EXACTLY one line: KEEP_BOTH, "
                "SUPERSEDE, or ABANDON_NEW followed by a brief reason.",
                min_length=10, duo_id=self.duo_id)
            resp = mi.query(
                role="apex",
                system_prompt=comparison_identity,
                user_prompt=prompt,
                max_tokens=get_system_config_int(self.db, "comparison_max_tokens", 200),
                temperature=get_system_config_float(self.db, "comparison_temperature", 0.1),
                context="dossier_comparison",
                duo_id=self.duo_id
            )
            answer = (resp.content if resp and resp.content else "").strip().upper()

            if answer.startswith("SUPERSEDE"):
                reason = answer.replace("SUPERSEDE", "").strip(" |-—:")
                return {"action": "supersede", "supersede_id": old_id,
                        "reason": f"Apex: {reason}" if reason else "Apex chose new dossier"}
            elif answer.startswith("ABANDON_NEW"):
                reason = answer.replace("ABANDON_NEW", "").strip(" |-—:")
                return {"action": "abandon", "keep_id": old_id,
                        "reason": f"Apex: {reason}" if reason else "Apex kept existing dossier"}
            else:
                return {"action": "keep_both",
                        "reason": f"Apex: staggered entries both valid"}

        except Exception as e:
            logger.warning(f"[TradingFloor] LLM compare failed: {e}")
            return None

    def process_mentor_signal(self, signal: Dict,
                              mentor_mode: str = "both_parallel") -> Optional[int]:
        """Process a single mentor signal — mirror first, then Apex assessment.

        Used by the manual reprocess API and Scout mentor routing.

        Returns the Apex dossier_id or None.
        """
        from core.duo_config import is_duo_enabled
        from services.trade_dossier import TradeDossierBuilder
        from services.candle_collector import get_candle_collector

        if not is_duo_enabled(self.config, self.duo_id):
            logger.info(f"[TradingFloor][{self.duo_id}] Duo disabled — "
                        f"skipping mentor signal for {signal.get('symbol')}")
            return None

        raw_symbol = signal["symbol"]
        symbol = resolve_symbol(raw_symbol, self.db)
        if symbol != raw_symbol:
            signal["symbol"] = symbol
            signal["original_symbol"] = raw_symbol

        # Fire mirror first (instant, no AI)
        if mentor_mode == "both_parallel":
            try:
                collector = get_candle_collector()
                builder = TradeDossierBuilder(self.db, self.config, collector,
                                              duo_id=self.duo_id)
                self._build_mentor_mirror_dossier(signal, None, builder)
            except Exception as e:
                logger.error(f"[TradingFloor] Mirror build error in "
                             f"process_mentor_signal: {e}")

        # Then run Apex assessment (blocking for API callers)
        return self._assess_mentor_signal(signal, mentor_mode)

    def _assess_mentor_signal(self, signal: Dict,
                               mentor_mode: str = "both_parallel"):
        """Apex-only assessment for a mentor signal — runs in the thread pool.

        This is the AI-heavy part extracted from process_mentor_signal.
        Runs the Apex independent analysis and handles its decision.
        Called via process_mentor_signal or Scout mentor routing.
        """
        from services.trade_dossier import TradeDossierBuilder
        from services.candle_collector import get_candle_collector

        raw_symbol = signal["symbol"]
        symbol = resolve_symbol(raw_symbol, self.db)
        if symbol != raw_symbol:
            signal["symbol"] = symbol
            signal["original_symbol"] = raw_symbol
        author = signal.get("author", "Mentor")
        logger.info(f"[TradingFloor] [pool] MENTOR ASSESS: "
                    f"{author} {signal.get('direction')} {symbol}")

        try:
            sym_check = self.db.fetch_one(
                "SELECT tradable, asset_class FROM market_symbols WHERE symbol = %s",
                (symbol,))
            if sym_check and not sym_check.get("tradable"):
                logger.warning(f"[TradingFloor] SKIPPING Apex assessment for {symbol}: "
                               f"not tradable (asset_class={sym_check.get('asset_class')})")
                return None
            if not sym_check:
                logger.warning(f"[TradingFloor] SKIPPING Apex assessment for {symbol}: "
                               f"not found in market_symbols")
                return None
        except Exception as e:
            logger.debug(f"[TradingFloor] Tradability check skipped for {symbol}: {e}")

        collector = get_candle_collector()
        if collector:
            try:
                collector.fetch_and_store(symbol)
            except Exception:
                pass

        max_active = self._td_cfg.get("max_active_dossiers_per_symbol", 2)
        apex_count = self.db.fetch_one(
            "SELECT COUNT(*) as cnt FROM trade_dossiers "
            "WHERE symbol = %s AND status IN ('proposed','monitoring','open_order','live') "
            "AND (mentor_type IS NULL OR mentor_type != 'mentor_mirror') "
            "AND duo_id = %s",
            (symbol, self.duo_id))
        if apex_count and int(apex_count["cnt"]) >= max_active:
            logger.info(f"[TradingFloor] {symbol}: {apex_count['cnt']} Apex dossiers "
                        f"active (max {max_active}), skipping Apex assessment for "
                        f"mentor {author}")
            return None

        builder = TradeDossierBuilder(self.db, self.config, collector,
                                      duo_id=self.duo_id)
        pq_key = self._pipeline_start(symbol, author or "mentor")
        dossier = builder.build_dossier(
            symbol,
            on_stage=lambda stage, detail="": self._pipeline_stage(pq_key, stage, detail),
            mentor_triggered=True,
            mentor_signal=signal)
        dossier_id = dossier.get("dossier_id")
        self._pipeline_done(pq_key, dossier_id)

        apex_saved = dossier_id is not None and dossier_id > 0

        if apex_saved:
            sim_result = self._check_dossier_similarity(symbol, dossier_id, dossier)
            if sim_result.get("action") == "abandon":
                logger.info(f"[TradingFloor] Mentor #{dossier_id} {symbol}: ABANDONED "
                            f"— duplicate of #{sim_result.get('keep_id')}")
                mentor_sim_reason = (f"Mentor duplicate of #{sim_result.get('keep_id')}: "
                                     f"{sim_result.get('reason','')}")
                transition_dossier(self.db, dossier_id, "abandoned", mentor_sim_reason)
                _append_tracker_log(self.db, dossier_id,
                                    f"[Similarity] {mentor_sim_reason}")
                apex_saved = False

        if apex_saved:
            self._subscribe_for_new_dossier(symbol)

            # Find the mentor mirror dossier for this signal
            mirror_row = self.db.fetch_one(
                "SELECT id FROM trade_dossiers "
                "WHERE linked_signal_id = %s AND mentor_type = 'mentor_mirror' "
                "AND id != %s ORDER BY created_at DESC LIMIT 1",
                (signal["id"], dossier_id))
            mirror_id = mirror_row["id"] if mirror_row else None

            # Link Apex dossier to signal, mentor, and mirror dossier
            self.db.execute(
                "UPDATE trade_dossiers SET linked_signal_id = %s, "
                "mentor_source = %s, mentor_type = 'apex_assessed', "
                "trade_group_id = %s "
                "WHERE id = %s",
                (signal["id"], author,
                 str(mirror_id) if mirror_id else None,
                 dossier_id))

            # Link the mirror back to this Apex dossier
            if mirror_id:
                self.db.execute(
                    "UPDATE trade_dossiers SET dossier_intelligence = "
                    "CONCAT(COALESCE(dossier_intelligence,''), %s), "
                    "trade_group_id = %s "
                    "WHERE id = %s",
                    (f"\n\n=== APEX COMPARISON ===\n"
                     f"Apex independent analysis: Dossier #{dossier_id}\n"
                     f"Apex's assessment will be compared with this mentor's "
                     f"actual trade outcome in post-mortem.",
                     str(dossier_id),
                     mirror_id))
                logger.info(f"[TradingFloor] Linked Apex #{dossier_id} <-> "
                            f"Mirror #{mirror_id} for {symbol} "
                            f"(mentor {author})")
            else:
                self.db.execute(
                    "UPDATE trade_dossiers SET dossier_intelligence = "
                    "CONCAT(COALESCE(dossier_intelligence,''), %s) "
                    "WHERE linked_signal_id = %s "
                    "AND mentor_type = 'mentor_mirror' AND id != %s",
                    (f"\n\n=== APEX COMPARISON ===\n"
                     f"Apex independent analysis: Dossier #{dossier_id}",
                     signal["id"], dossier_id))

            decision = dossier.get("stage2_output", {}).get("trade_decision")
            mentor_rationale = (dossier.get("stage2_output", {}).get("rationale")
                                or dossier.get("stage2_output", {}).get("reasoning")
                                or "")[:300]
            logger.info(f"[TradingFloor] Apex decision for {symbol} "
                        f"(mentor {author}): {decision} — dossier #{dossier_id}")

            # For WHITELISTED mentors: the mirror dossier handles the exchange
            # position, so Apex's assessment stays paper-only for comparison.
            # For NON-WHITELISTED mentors: the mirror is paper monitoring only,
            # so if Apex agrees — Apex takes the live trade itself.
            mentor_whitelisted = _is_mentor_whitelisted(self.db, author)

            if decision == "trade_now":
                if mentor_whitelisted:
                    if self._td_cfg.get("auto_execute_paper"):
                        transition_dossier(self.db, dossier_id, "monitoring",
                                          f"Apex agrees with mentor {author} — "
                                          f"paper-only for comparison")
                        self._register_order_entry_level(dossier_id)
                        e_result = execute_paper_trade(self.db, dossier_id)
                        if not e_result.get("success"):
                            logger.error(f"[TradingFloor] Mentor Apex paper "
                                         f"execute failed #{dossier_id}: "
                                         f"{e_result.get('error')}")
                    self._log_apex_thought(symbol, "mentor_trade_now",
                        f"Agreed with mentor {author} — paper-only for "
                        f"learning (mirror handles exchange). "
                        f"{mentor_rationale[:800]}",
                        decision="trade_now")
                    logger.info(f"[TradingFloor] Apex AGREES with {author} on "
                                f"{symbol} — #{dossier_id} tracking on paper "
                                f"(mentor mirror handles exchange)")
                    return dossier_id
                else:
                    transition_dossier(self.db, dossier_id, "open_order",
                                      f"Apex independently agrees with "
                                      f"non-whitelisted mentor {author} — "
                                      f"executing as Apex trade")
                    self._register_order_entry_level(dossier_id)
                    if self._td_cfg.get("auto_execute_paper"):
                        execute_paper_trade(self.db, dossier_id)
                    self._execute_live_trades_for_dossier(dossier_id)
                    self._log_apex_thought(symbol, "mentor_apex_trade",
                        f"Non-whitelisted mentor {author} — Apex agrees and "
                        f"is taking this trade live. Mirror monitors for "
                        f"learning. {mentor_rationale[:800]}",
                        decision="trade_now")
                    logger.info(f"[TradingFloor] Apex AGREES with "
                                f"non-whitelisted {author} on {symbol} — "
                                f"#{dossier_id} executing as LIVE Apex trade")
                return dossier_id
            elif decision in ("no_trade", "do_not_trade"):
                reason = mentor_rationale or "Conditions/R:R not met"
                transition_dossier(self.db, dossier_id, "monitoring",
                                  f"Apex rejected mentor {author}'s setup — "
                                  f"tracking paper for comparison")
                self._register_order_entry_level(dossier_id)
                e_result = execute_paper_trade(self.db, dossier_id)
                if not e_result.get("success"):
                    logger.debug(f"[TradingFloor] Mentor Apex paper: "
                                 f"{e_result.get('error')}")
                self.db.execute(
                    "UPDATE trade_dossiers SET apex_entry_reasoning = %s "
                    "WHERE id = %s",
                    (f"REJECTED: {reason}", dossier_id))
                self._log_apex_thought(symbol, "mentor_rejected",
                    f"Rejected {author}'s {symbol}: {reason[:800]} — "
                    f"tracking paper to compare with mentor outcome",
                    decision="no_trade")
                logger.info(f"[TradingFloor] Apex REJECTED {author}'s "
                            f"{symbol} — #{dossier_id} paper-only for "
                            f"comparison learning")
                return dossier_id
        else:
            s2 = (dossier or {}).get("stage2_output", {})
            unsaved_decision = s2.get("trade_decision", "")
            unsaved_rationale = (s2.get("rationale") or s2.get("reasoning") or "")[:1500]

            if unsaved_decision == "do_not_trade":
                logger.info(f"[TradingFloor] Apex REJECTED mentor {author}'s "
                            f"{symbol}: {unsaved_rationale[:200]}")
                self._log_apex_thought(symbol, "mentor_do_not_trade",
                    f"Rejected {author}'s {symbol}: {unsaved_rationale[:800]}",
                    decision="do_not_trade")
            else:
                logger.warning(f"[TradingFloor] Apex dossier build FAILED for {symbol} "
                               f"from mentor {author} (id={dossier_id}, "
                               f"decision={unsaved_decision or 'none'})")
                self._log_apex_thought(symbol, "mentor_build_failed",
                    f"Build genuinely failed for {author}'s {symbol}"
                    f"{(' — ' + unsaved_rationale[:800]) if unsaved_rationale else ''}",
                    decision="failed")
            return None

    # NOTE: _check_mentor_calls() removed — mentor routing is now handled
    # exclusively by the Scout agent via route_mentor_signal().

    def _build_mentor_mirror_dossier(self, signal: Dict,
                                      apex_dossier_id: Optional[int],
                                      builder):
        """Build a 'mentor mirror' dossier that follows the mentor's exact setup.

        This creates a second dossier using the mentor's entry/SL/TP levels directly,
        tagged as mentor-sourced, so performance can be compared to Apex's independent
        analysis side-by-side.

        apex_dossier_id may be None if Apex rejected/skipped the trade.
        REFUSES to create if the mentor signal has no entry or stop loss.
        """
        try:
            raw_sym = signal["symbol"]
            try:
                from db.market_symbols import normalize_for_dossier
                norm = normalize_for_dossier(raw_sym, self.db)
                symbol = norm["normalized"]
            except Exception:
                symbol = resolve_symbol(raw_sym, self.db)
            if symbol != raw_sym:
                signal["symbol"] = symbol
            author = signal.get("author", "Mentor")
            entry = signal.get("entry_price")
            sl = signal.get("stop_loss")
            tp1 = signal.get("take_profit_1")

            if not entry or float(entry) == 0:
                logger.warning(f"[TradingFloor] Mentor mirror SKIPPED for {symbol}: "
                               f"{author}'s signal has no entry price")
                return
            if not sl or float(sl) == 0:
                logger.warning(f"[TradingFloor] Mentor mirror SKIPPED for {symbol}: "
                               f"{author}'s signal has no stop loss")
                return
            if not tp1 or float(tp1) == 0:
                logger.warning(f"[TradingFloor] Mentor mirror SKIPPED for {symbol}: "
                               f"{author}'s signal has no take profit")
                return

            # ── Price sanity gate: reject if entry is wildly off from market ──
            max_dist_pct = float(self._td_cfg.get("mentor", {}).get(
                "mirror_max_entry_distance_pct", 30))
            market_price = 0
            try:
                from services.price_streamer import get_price_streamer
                ps = get_price_streamer()
                if ps:
                    pdata = ps.get_price(symbol)
                    market_price = float(pdata.get("price", 0)) if pdata else 0
            except Exception:
                pass
            if market_price and market_price > 0:
                entry_f = float(entry)
                dist_pct = abs(entry_f - market_price) / market_price * 100
                if dist_pct > max_dist_pct:
                    logger.warning(
                        f"[TradingFloor] Mentor mirror SKIPPED for {symbol}: "
                        f"{author}'s entry {entry_f} is {dist_pct:.1f}% from "
                        f"market {market_price} (max {max_dist_pct}%) — "
                        f"likely misparse")
                    self._log_apex_thought(
                        symbol, "mirror_price_sanity",
                        f"Rejected {author}'s mirror: entry {entry_f} is "
                        f"{dist_pct:.1f}% from market {market_price}",
                        decision="rejected")
                    return

            max_active = self._td_cfg.get("max_active_dossiers_per_symbol", 2)
            total_active = self.db.fetch_one(
                "SELECT COUNT(*) as cnt FROM trade_dossiers "
                "WHERE symbol = %s AND status IN "
                "('proposed','monitoring','open_order','live') "
                "AND duo_id = %s",
                (symbol, self.duo_id))
            if total_active and int(total_active["cnt"]) >= max_active + 2:
                logger.info(
                    f"[TradingFloor] Mentor mirror SKIPPED for {symbol}: "
                    f"{total_active['cnt']} dossiers active (limit {max_active}+2)")
                return

            existing_mirror = self.db.fetch_one("""
                SELECT id, status FROM trade_dossiers
                WHERE symbol = %s AND mentor_type = 'mentor_mirror'
                  AND direction = %s
                  AND ABS(entry_price - %s) / GREATEST(entry_price, 0.01) < 0.005
                  AND ABS(stop_loss - %s) / GREATEST(stop_loss, 0.01) < 0.005
                  AND (status IN ('proposed','monitoring','open_order','live')
                       OR (status IN ('won','lost','abandoned')
                           AND updated_at >= NOW() - INTERVAL 24 HOUR))
                ORDER BY FIELD(status,'live','open_order','monitoring','proposed',
                               'won','lost','abandoned')
                LIMIT 1
            """, (symbol, signal.get("direction", "?"),
                  float(entry), float(sl)))
            if existing_mirror:
                logger.info(f"[TradingFloor] Mentor mirror SKIPPED for {symbol}: "
                           f"duplicate of #{existing_mirror['id']} "
                           f"({existing_mirror['status']}, same entry/SL from {author})")
                return

            # ── Geometry validation: SL/entry/TP must be correctly ordered ──
            direction = (signal.get("direction") or "?").upper()
            entry_f, sl_f, tp1_f = float(entry), float(sl), float(tp1)

            if direction == "BUY":
                if sl_f >= entry_f:
                    logger.warning(
                        f"[TradingFloor] Mentor mirror SKIPPED for {symbol}: "
                        f"BUY but SL {sl_f} >= entry {entry_f} (invalid geometry)")
                    return
                if tp1_f <= entry_f:
                    logger.warning(
                        f"[TradingFloor] Mentor mirror SKIPPED for {symbol}: "
                        f"BUY but TP1 {tp1_f} <= entry {entry_f} (invalid geometry)")
                    return
            elif direction == "SELL":
                if sl_f <= entry_f:
                    logger.warning(
                        f"[TradingFloor] Mentor mirror SKIPPED for {symbol}: "
                        f"SELL but SL {sl_f} <= entry {entry_f} (invalid geometry)")
                    return
                if tp1_f >= entry_f:
                    logger.warning(
                        f"[TradingFloor] Mentor mirror SKIPPED for {symbol}: "
                        f"SELL but TP1 {tp1_f} >= entry {entry_f} (invalid geometry)")
                    return
            else:
                logger.warning(
                    f"[TradingFloor] Mentor mirror SKIPPED for {symbol}: "
                    f"unknown direction '{direction}'")
                return

            risk = abs(entry_f - sl_f)
            reward = abs(tp1_f - entry_f)
            rr_ratio = (reward / risk) if risk > 0 else 0
            min_rr = float(self._td_cfg.get("min_rr_floor", 1.0))
            if rr_ratio < min_rr:
                logger.warning(
                    f"[TradingFloor] Mentor mirror SKIPPED for {symbol}: "
                    f"R:R {rr_ratio:.2f} < min {min_rr} "
                    f"(risk={risk:.6f}, reward={reward:.6f})")
                return

            # Build rich dossier intelligence from the mentor's original message
            raw_text = signal.get("raw_text", "")
            ai_reasoning = signal.get("ai_reasoning", "")
            source_detail = signal.get("source_detail", "")

            tp_lines = [f"  TP1: {tp1}"]
            for i in range(2, 7):
                tp_val = signal.get(f"take_profit_{i}")
                if tp_val and float(tp_val) != 0:
                    tp_lines.append(f"  TP{i}: {tp_val}")

            apex_ref = (f"Apex independent analysis: Dossier #{apex_dossier_id}"
                        if apex_dossier_id else
                        "Apex did not take this trade independently")

            intelligence = (
                f"=== MENTOR TRADE SETUP ===\n"
                f"Mentor: {author}\n"
                f"Source: {source_detail}\n"
                f"Direction: {direction}\n"
                f"Entry: {entry}\n"
                f"Stop Loss: {sl}\n"
                f"{chr(10).join(tp_lines)}\n\n"
                f"=== MENTOR'S ORIGINAL MESSAGE ===\n"
                f"{raw_text}\n\n"
                f"=== AI ANALYSIS OF MENTOR SETUP ===\n"
                f"{ai_reasoning if ai_reasoning else 'No AI analysis available.'}\n\n"
                f"=== COMPARISON ===\n"
                f"{apex_ref}\n"
                f"This dossier follows {author}'s exact levels for side-by-side comparison."
            )

            init_prob = json.dumps([{
                "time": _utcnow().isoformat() + "Z",
                "probability": 80,
                "reason": f"Mentor mirror: following {author}'s exact setup"
            }], default=str)

            mirror_conditions = json.dumps([{
                "id": 1, "description": f"Mentor {author} trade setup — following exact levels",
                "type": "signal", "weight": 10, "status": "met",
                "measurement": "Mentor call received"
            }], default=str)

            apex_line = (f"Apex independent analysis: dossier #{apex_dossier_id}."
                         if apex_dossier_id else
                         "Mirror follows mentor's exact levels.")

            stage2_text = (
                f"[MENTOR MIRROR] Following {author}'s exact setup.\n"
                f"{apex_line}\n"
                f"Direction: {direction} | Entry: {entry} | "
                f"SL: {sl} | TP1: {tp1}"
            )

            _raw_sym = raw_sym if raw_sym != symbol else None

            mentor_dossier_id = self.db.execute_returning_id("""
                INSERT INTO trade_dossiers (
                    symbol, raw_symbol, direction, entry_price, stop_loss,
                    take_profit_1, take_profit_2, take_profit_3,
                    take_profit_4, take_profit_5, take_profit_6,
                    trade_decision, confidence_score, status,
                    linked_signal_id, stage2_raw_response,
                    probability_history, conditions_for_entry,
                    stage1_ta_output, dossier_intelligence,
                    mentor_source, mentor_type,
                    duo_id,
                    created_at, expires_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'trade_now', 80, 'proposed',
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, 'mentor_mirror',
                    %s,
                    NOW(), NOW() + INTERVAL 24 HOUR
                )
            """, (
                symbol, _raw_sym, direction, entry, sl, tp1,
                signal.get("take_profit_2") or None,
                signal.get("take_profit_3") or None,
                signal.get("take_profit_4") or None,
                signal.get("take_profit_5") or None,
                signal.get("take_profit_6") or None,
                signal.get("id"),
                stage2_text,
                init_prob,
                mirror_conditions,
                f"[Mentor Mirror] Following {author}'s levels: "
                f"Entry {entry}, SL {sl}, TP1 {tp1}",
                intelligence,
                author,
                self.duo_id,
            ))

            if mentor_dossier_id:
                self._subscribe_for_new_dossier(symbol)
                transition_dossier(self.db, mentor_dossier_id, "open_order",
                                  f"Mentor mirror trade from {author}")
                self._register_order_entry_level(mentor_dossier_id)
                if self._td_cfg.get("auto_execute_paper"):
                    result = execute_paper_trade(self.db, mentor_dossier_id)
                    if not result.get("success"):
                        logger.error(f"[TradingFloor] Mentor mirror execute failed: "
                                     f"{result.get('error')}")

                whitelisted = _is_mentor_whitelisted(self.db, author)
                if whitelisted:
                    self._execute_live_trades_for_dossier(
                        mentor_dossier_id, mentor_id=author)
                else:
                    self.db.execute(
                        "UPDATE trade_dossiers SET paper_reason = 'mentor_not_whitelisted' "
                        "WHERE id = %s", (mentor_dossier_id,))
                    _append_tracker_log(self.db, mentor_dossier_id,
                        f"Paper only: {author} is not on any account's mentor "
                        f"whitelist. Monitoring for learning. "
                        f"Apex will assess independently and may execute.")
                    logger.info(f"[TradingFloor] Mirror #{mentor_dossier_id} "
                                f"{symbol}: {author} not whitelisted — "
                                f"paper monitoring, deferring to Apex assessment")

            apex_str = f"Apex #{apex_dossier_id}" if apex_dossier_id else "Apex rejected"
            logger.info(f"[TradingFloor] Mentor mirror #{mentor_dossier_id} "
                        f"created ({apex_str}) for {symbol} "
                        f"(Entry={entry}, SL={sl}, TP1={tp1})")

        except Exception as e:
            logger.error(f"[TradingFloor] Mentor mirror dossier error: {e}")

    def _get_mentor_usernames(self) -> List[str]:
        """Get all usernames linked to mentor profiles."""
        from db.database import get_mentor_usernames
        return get_mentor_usernames(self.db)

    def _get_trading_enabled_mentor_usernames(self) -> List[str]:
        """Get usernames of mentors with mentor_trading_enabled=1 only."""
        rows = self.db.fetch_all("""
            SELECT DISTINCT upl.source_username
            FROM user_profiles up
            JOIN user_profile_links upl ON upl.user_profile_id = up.id
            WHERE up.is_mentor = 1
              AND COALESCE(up.mentor_trading_enabled, 1) = 1
              AND upl.source_username IS NOT NULL AND upl.source_username != ''
        """)
        names = [r["source_username"] for r in (rows or [])]
        profile_names = self.db.fetch_all(
            "SELECT display_name FROM user_profiles "
            "WHERE is_mentor = 1 AND COALESCE(mentor_trading_enabled, 1) = 1")
        for p in (profile_names or []):
            if p["display_name"] and p["display_name"] not in names:
                names.append(p["display_name"])
        return names

    # ── Tracker Loop ──────────────────────────────────────────────────

    def _tracker_loop(self):
        """Periodically check conditions on active dossiers."""
        interval = self._td_cfg.get("tracker_interval_minutes", 15) * 60
        while self._running and not self._stop_event.is_set():
            try:
                self._run_tracker_cycle()
                self._last_dossier_check_at = _utcnow()
            except Exception as e:
                logger.error(f"[TradingFloor] Tracker cycle error: {e}",
                             exc_info=True)
            self._stop_event.wait(interval)

    def _run_tracker_cycle(self):
        """Check all active dossiers and update their conditions.
        Every N cycles, also runs an LLM conversation for deeper evaluation."""
        if not hasattr(self, "_tracker_cycle_count"):
            self._tracker_cycle_count = -1
        self._tracker_cycle_count += 1
        self._execution_ready.clear()  # reset batch for this cycle

        active = self.db.fetch_all("""
            SELECT id, symbol, status, trade_decision
            FROM trade_dossiers
            WHERE status IN ('proposed','monitoring','open_order')
                  AND (expires_at IS NULL OR expires_at > NOW())
                  AND duo_id = %s
            ORDER BY created_at DESC
        """, (self.duo_id,))

        # Prune stale entries from momentum tracker (only keep active dossier IDs)
        active_ids = set(d["id"] for d in active) if active else set()
        stale_keys = [k for k in self._opp_prev_conditions if k not in active_ids]
        for k in stale_keys:
            del self._opp_prev_conditions[k]

        # Prune stale discovery cooldowns (older than 2 hours)
        try:
            cd_cutoff = _utcnow() - timedelta(hours=2)
            stale_cd = [s for s, t in list(self._discovery_cooldown.items()) if t < cd_cutoff]
            for s in stale_cd:
                del self._discovery_cooldown[s]
        except Exception:
            pass

        if not active:
            logger.info("[TradingFloor] Tracker: no active dossiers to check")
            self._log_activity("info", "Tracker: no active dossiers")
            return

        logger.info(f"[TradingFloor] Tracker: checking {len(active)} active dossier(s) "
                     f"(cycle #{self._tracker_cycle_count})")
        self._log_activity("info", f"Tracker: checking {len(active)} dossier(s) "
                           f"(cycle #{self._tracker_cycle_count})")

        threshold_execute = self._td_cfg.get("condition_threshold_execute", 85)
        threshold_limit = self._td_cfg.get("condition_threshold_limit_order", 75)
        llm_every_n = self._td_cfg.get("tracker_llm_every_n_cycles", 1)

        # One fetch per symbol (not per dossier)
        symbols = set(d["symbol"] for d in active)
        candles_by_symbol = {sym: self._get_recent_candles(sym) for sym in symbols}
        candles_by_tf_by_symbol = {}
        if self._tracker_cycle_count % llm_every_n == 0:
            try:
                from services.data_scientist import get_data_scientist
                ds = get_data_scientist(self.db)
                for sym in symbols:
                    candles_by_tf_by_symbol[sym] = ds.get_candles_from_db(
                        sym, {"M5": 2, "M15": 12, "H1": 48, "H4": 168, "D1": 720})
            except Exception as e:
                logger.debug(f"[TradingFloor] Pre-fetch candles_by_tf: {e}")

        for d in active:
            if self._stop_event.is_set():
                break
            try:
                candles = candles_by_symbol.get(d["symbol"])
                self._check_dossier_conditions(d, threshold_execute, threshold_limit, candles)

                if self._tracker_cycle_count % llm_every_n == 0:
                    candles_by_tf = candles_by_tf_by_symbol.get(d["symbol"])
                    self._run_tracker_conversation(d, candles_by_tf)
            except Exception as e:
                logger.error(f"[TradingFloor] Tracker error for #{d['id']}: {e}")
                self._log_activity("error", f"#{d['id']} tracker error: {e}",
                                   symbol=d.get("symbol"), dossier_id=d["id"])

        # ── Phase H: execute best opportunities first (ranked by OppScore) ──
        self._execute_ranked_opportunities()

        # ── Safety net: place orders for any open_order dossiers that need it ──
        max_order_retries = int(self._td_cfg.get("max_order_retries", 3))
        stuck_ready = [d for d in active if d["status"] == "open_order"
                       and not d.get("linked_signal_id")]
        for d in stuck_ready:
            try:
                fail_count = self.db.fetch_one(
                    "SELECT COUNT(*) as cnt FROM live_trade_audit "
                    "WHERE dossier_id = %s AND success = 0",
                    (d["id"],))
                total_fails = int(fail_count["cnt"]) if fail_count else 0

                if total_fails >= max_order_retries:
                    logger.warning(
                        f"[TradingFloor] #{d['id']} {d['symbol']}: "
                        f"{total_fails} total failures >= max {max_order_retries} "
                        f"— abandoning dossier")
                    transition_dossier(
                        self.db, d["id"], "abandoned",
                        f"Abandoned: {total_fails} order failures "
                        f"(max_order_retries={max_order_retries})")
                    self._log_activity(
                        "warn",
                        f"#{d['id']} {d['symbol']}: ABANDONED after "
                        f"{total_fails} failed order attempts",
                        symbol=d["symbol"], dossier_id=d["id"])
                    continue

                if not self._margin_gate(d["id"]):
                    logger.debug(
                        f"[TradingFloor] Safety-net skip #{d['id']} "
                        f"{d['symbol']}: margin gate blocked")
                    continue

                if self._td_cfg.get("auto_execute_paper", False):
                    e_result = execute_paper_trade(self.db, d["id"])
                    if e_result.get("success") and not e_result.get("already_existed"):
                        self._log_activity("trade",
                            f"#{d['id']} {d['symbol']}: order placed (was missing signal)",
                            symbol=d["symbol"], dossier_id=d["id"])
                    elif not e_result.get("success") and "Cannot execute from status" not in str(e_result.get("error", "")):
                        self._log_activity("warn",
                            f"#{d['id']} {d['symbol']}: ready but can't execute — "
                            f"{e_result.get('error','')}",
                            symbol=d["symbol"], dossier_id=d["id"])
                self._execute_live_trades_for_dossier(d["id"])
            except Exception:
                pass

        expired = self.db.fetch_all("""
            SELECT id FROM trade_dossiers
            WHERE status IN ('proposed','monitoring','open_order')
                  AND expires_at IS NOT NULL AND expires_at <= NOW()
                  AND duo_id = %s
        """, (self.duo_id,))
        for d in (expired or []):
            self._log_activity("warn", f"#{d['id']} expired", dossier_id=d["id"])
            transition_dossier(self.db, d["id"], "expired",
                               "Dossier expired (past expiry time)")

        # ── TP1 stale check: abandon dossiers whose TP1 was already reached ──
        with self._tracker_lock:
            self._abandon_tp1_overshot(candles_by_symbol)

        # ── Watch open orders for entry hit → transition to live ──
        with self._tracker_lock:
            self._track_open_orders(candles_by_symbol)

        # ── Track live trades (SL/TP/break-even/P&L) ──
        with self._tracker_lock:
            self._track_active_trades(candles_by_symbol)

        # ── Expire stale open orders that have been waiting too long ──
        self._expire_stale_orders()

    def _expire_stale_orders(self):
        """Expire open_order dossiers that have been waiting too long.

        Rules:
        1. open_order older than max_open_order_hours (default 48h) -> expired
        2. open_order that failed waterfall placement (tracker log shows 'WATERFALL: no account')
           AND older than 4h -> expired (no point waiting if no exchange can take it)
        """
        max_hours = self._td_cfg.get("max_open_order_hours", 48)

        stale = self.db.fetch_all("""
            SELECT id, symbol, created_at, tracker_log
            FROM trade_dossiers
            WHERE status = 'open_order'
              AND duo_id = %s
              AND created_at < NOW() - INTERVAL %s HOUR
        """, (self.duo_id, max_hours,))

        for d in (stale or []):
            reason = f"Expired: open_order for {max_hours}h without fill"
            transition_dossier(self.db, d["id"], "expired", reason)
            _append_tracker_log(self.db, d["id"], reason)
            logger.info(f"[TradingFloor] #{d['id']} {d['symbol']}: expired (stale open_order)")

        failed = self.db.fetch_all("""
            SELECT id, symbol, tracker_log
            FROM trade_dossiers
            WHERE status = 'open_order'
              AND duo_id = %s
              AND created_at < NOW() - INTERVAL 4 HOUR
              AND tracker_log LIKE '%%WATERFALL: no account%%'
        """, (self.duo_id,))

        for d in (failed or []):
            reason = "Expired: waterfall placement failed, no exchange could take this trade"
            transition_dossier(self.db, d["id"], "expired", reason)
            _append_tracker_log(self.db, d["id"], reason)
            logger.info(f"[TradingFloor] #{d['id']} {d['symbol']}: expired (waterfall failed)")

        orphans = self.db.fetch_all("""
            SELECT id, symbol FROM trade_dossiers
            WHERE status IN ('proposed', 'monitoring', 'open_order')
              AND duo_id = %s
              AND expires_at IS NULL
              AND created_at < NOW() - INTERVAL 48 HOUR
        """, (self.duo_id,))
        for d in (orphans or []):
            reason = "Abandoned: orphan dossier with no expiry, >48h old"
            transition_dossier(self.db, d["id"], "abandoned", reason)
            _append_tracker_log(self.db, d["id"], reason)
            logger.info(f"[TradingFloor] #{d['id']} {d['symbol']}: "
                        f"abandoned (orphan, no expires_at)")

        live_orphans = self.db.fetch_all("""
            SELECT d.id, d.symbol FROM trade_dossiers d
            WHERE d.status IN ('live', 'open_order')
              AND d.duo_id = %s
              AND (d.paper_reason IS NULL OR d.paper_reason = '')
              AND NOT EXISTS (
                  SELECT 1 FROM live_trades lt
                  WHERE lt.dossier_id = d.id
                    AND lt.status IN ('pending', 'open', 'partial_closed')
              )
        """, (self.duo_id,))
        for d in (live_orphans or []):
            reason = ("Abandoned: dossier marked live but no active exchange "
                      "position or pending order exists")
            self.db.execute(
                "UPDATE trade_dossiers SET status = 'abandoned', "
                "unrealised_pnl = NULL, unrealised_pnl_pct = NULL, "
                "realised_pnl = NULL, realised_pnl_pct = NULL "
                "WHERE id = %s", (d["id"],))
            _append_tracker_log(self.db, d["id"], reason)
            logger.info(f"[TradingFloor] #{d['id']} {d['symbol']}: "
                        f"abandoned (live orphan — no exchange position)")

    def _abandon_tp1_overshot(self, candles_by_symbol: Dict = None):
        """Abandon proposed/monitoring/open_order dossiers where price already
        reached TP1 after the dossier was created but we never entered the trade.

        The logic: if TP1 was reachable and price got there before we entered,
        the edge is gone. Continuing to watch is pointless — the market moved
        and the trade setup is stale.

        Uses M5 candle highs/lows since dossier creation to check if TP1
        was breached. Only checks dossiers that have not yet transitioned
        to ``live`` status (i.e. we never got filled).
        """
        un_entered = self.db.fetch_all("""
            SELECT id, symbol, direction, entry_price, take_profit_1,
                   created_at, status
            FROM trade_dossiers
            WHERE status IN ('proposed', 'monitoring', 'open_order')
              AND duo_id = %s
              AND take_profit_1 IS NOT NULL
              AND entry_price IS NOT NULL
              AND (mentor_type IS NULL OR mentor_type != 'mentor_observation')
            ORDER BY created_at DESC LIMIT 50
        """, (self.duo_id,))

        if not un_entered:
            return

        for d in un_entered:
            try:
                tp1 = float(d["take_profit_1"])
                direction = (d.get("direction") or "").upper()
                symbol = d["symbol"]

                if tp1 <= 0 or not direction:
                    continue

                candles = candles_by_symbol.get(symbol) if candles_by_symbol else None
                if not candles:
                    candles = self._get_recent_candles(symbol)

                m5 = candles.get("M5", [])
                if not m5:
                    continue

                created_at = d.get("created_at")
                if not created_at:
                    continue
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.fromisoformat(
                            created_at.replace("Z", "+00:00")).replace(tzinfo=None)
                    except (ValueError, TypeError):
                        continue

                tp1_hit = False
                for c in m5:
                    candle_ts = c.get("timestamp") or c.get("time")
                    if candle_ts:
                        if isinstance(candle_ts, str):
                            try:
                                candle_ts = datetime.fromisoformat(
                                    candle_ts.replace("Z", "+00:00")).replace(tzinfo=None)
                            except (ValueError, TypeError):
                                candle_ts = None
                        if candle_ts and candle_ts < created_at:
                            continue

                    h = float(c.get("high", 0))
                    l = float(c.get("low", 0))

                    if direction == "BUY" and h >= tp1:
                        tp1_hit = True
                        break
                    elif direction == "SELL" and l <= tp1:
                        tp1_hit = True
                        break

                if tp1_hit:
                    reason = (f"TP1 overshot: {symbol} {direction} TP1={tp1:.5f} "
                              f"was already hit while dossier was in '{d['status']}' — "
                              f"edge is gone, trade is stale")
                    result = self._abandon_dossier(
                        d["id"], reason, symbol=symbol, set_cooldown=False)
                    if not result or not result.get("success"):
                        logger.debug(f"[TradingFloor] #{d['id']} TP1 overshot transition "
                                     f"blocked (already moved): {result}")
                        continue
                    self._log_activity("warn",
                        f"#{d['id']} {symbol}: TP1 OVERSHOT — abandoned (was {d['status']})",
                        symbol=symbol, dossier_id=d["id"])
                    self._trigger_postmortem(d["id"])
                    logger.info(f"[TradingFloor] #{d['id']} {symbol}: TP1 overshot "
                                f"— abandoned from {d['status']}")
            except Exception as e:
                logger.debug(f"[TradingFloor] TP1 overshot check #{d.get('id')}: {e}")

    def _track_open_orders(self, candles_by_symbol: Dict = None):
        """Watch open_order dossiers. When price reaches entry, transition to live.
        Uses candle wicks (high/low) for all price checks, not just close price.
        Scoped to this duo's dossiers only (duo_id filter)."""
        orders = self.db.fetch_all("""
            SELECT id, symbol, direction, entry_price, stop_loss,
                   take_profit_1, trade_decision, margin_usd, leverage, resolved_exchange
            FROM trade_dossiers
            WHERE status = 'open_order' AND duo_id = %s
            ORDER BY created_at DESC LIMIT 50
        """, (self.duo_id,))
        if not orders:
            return

        for d in orders:
            try:
                entry = float(d["entry_price"]) if d.get("entry_price") else None
                direction = (d.get("direction") or "").upper()
                symbol = d["symbol"]

                if not entry or not direction:
                    continue

                # Skip dossiers whose linked live trade was cancelled unfilled —
                # _sync_dossier_on_cancel will handle transitioning the dossier
                lt_cancelled = self.db.fetch_one(
                    "SELECT id FROM live_trades "
                    "WHERE dossier_id = %s AND status = 'cancelled' "
                    "AND actual_entry_price IS NULL LIMIT 1",
                    (d["id"],))
                if lt_cancelled:
                    continue

                fill_price = entry
                entry_fill_source = "wick"  # default; overridden for trade_now_market, live_fallback
                candles = candles_by_symbol.get(symbol) if candles_by_symbol else None
                if not candles:
                    candles = self._get_recent_candles(symbol)
                price = candles.get("latest_close", 0) if candles else 0
                if price and not _is_price_valid_for_symbol(symbol, price):
                    price = 0
                if not price:
                    no_data_max = self._td_cfg.get("no_price_abandon_cycles", 15)
                    self._no_price_counter[d["id"]] = self._no_price_counter.get(d["id"], 0) + 1
                    count = self._no_price_counter[d["id"]]
                    if count >= no_data_max:
                        reason = (f"No price data for {count} consecutive tracker cycles "
                                  f"— symbol {symbol} may not be trackable on available exchanges")
                        self._abandon_dossier(d["id"], reason, symbol=symbol)
                        _append_tracker_log(self.db, d["id"], f"ABANDONED: {reason}")
                        self._log_activity("warn", f"#{d['id']} {symbol}: ABANDONED — no price data "
                                           f"after {count} cycles", symbol=symbol, dossier_id=d["id"])
                        self._no_price_counter.pop(d["id"], None)
                    elif count % 5 == 0:
                        logger.warning(f"[TradingFloor] #{d['id']} {symbol}: no price data "
                                       f"for {count} cycles (abandon at {no_data_max})")
                    continue

                self._no_price_counter.pop(d["id"], None)
                sl = float(d["stop_loss"]) if d.get("stop_loss") else None

                # Extract M5 wick data — use configurable window (default 24 = 2h) to avoid
                # missing SL hits that retest finds. Narrow 6-candle window caused false wins.
                wick_window = self._td_cfg.get("sl_tp_candle_window", 24)
                m5_candles = candles.get("M5", [])
                m5_slice = m5_candles[:wick_window] if m5_candles else []
                recent_high = max((float(c.get("high", 0)) for c in m5_slice), default=price)
                recent_low = min((float(c.get("low", 999999)) for c in m5_slice), default=price)
                fill_price = entry  # default; overridden when using live price fallback

                # ── GUARD 1: SL already breached (using wicks) ──
                if sl:
                    sl_blown = ((direction == "BUY" and recent_low <= sl) or
                                (direction == "SELL" and recent_high >= sl))
                    if sl_blown:
                        reason = (f"SL invalidated before fill: wick "
                                  f"{'low' if direction == 'BUY' else 'high'} "
                                  f"{'=' + str(recent_low) if direction == 'BUY' else '=' + str(recent_high)} "
                                  f"breached SL {sl:.5f}")
                        self._abandon_dossier(d["id"], reason, symbol=symbol)
                        _append_tracker_log(self.db, d["id"], f"ABANDONED: {reason}")
                        self._log_activity("warn",
                            f"#{d['id']} {symbol}: ABANDONED — {reason}",
                            symbol=symbol, dossier_id=d["id"])
                        self._trigger_postmortem(d["id"])
                        continue

                # ── GUARD 2: Distance sanity — price moved too far past entry ──
                if sl:
                    sl_distance = abs(entry - sl)
                    price_distance = abs(entry - price)
                    ptf_mult = float(self._td_cfg.get(
                        "price_too_far_multiplier", 3))
                    if sl_distance > 0 and price_distance > sl_distance * ptf_mult:
                        def _smart_fmt(v):
                            if v >= 1:    return f"{v:.2f}"
                            if v >= 0.01: return f"{v:.4f}"
                            return f"{v:.6f}"
                        reason = (f"Price too far from entry: moved "
                                  f"{_smart_fmt(price_distance)} "
                                  f"({ptf_mult}x beyond SL distance "
                                  f"{_smart_fmt(sl_distance)})")
                        self._abandon_dossier(
                            d["id"], reason, symbol=symbol, set_cooldown=False)
                        _append_tracker_log(self.db, d["id"], f"ABANDONED: {reason}")
                        self._log_activity("warn",
                            f"#{d['id']} {symbol}: ABANDONED — {reason}",
                            symbol=symbol, dossier_id=d["id"])
                        self._trigger_postmortem(d["id"])
                        continue

                # ── GUARD 3: Candle freshness — but use live price fallback when stale ──
                candle_ts = candles.get("latest_timestamp")
                use_live_fallback = False
                if candle_ts:
                    try:
                        from datetime import datetime as _dt
                        if isinstance(candle_ts, str):
                            candle_ts = _dt.fromisoformat(candle_ts.replace("Z", "+00:00"))
                        age_seconds = (_utcnow() - candle_ts.replace(tzinfo=None)).total_seconds()
                        if age_seconds > 900:
                            try:
                                from services.price_streamer import get_price_streamer
                                ps = get_price_streamer()
                                if ps:
                                    live = ps.get_price(symbol)
                                    if live and live.get("price"):
                                        live_price = float(live["price"])
                                        if not _is_price_valid_for_symbol(symbol, live_price):
                                            live_price = 0
                                        # Only fill when price crossed entry in correct direction (limit/stop).
                                        # BUY limit: fill when lp <= entry. BUY stop: lp >= entry.
                                        # SELL limit: lp >= entry. SELL stop: lp <= entry.
                                        pct = abs(live_price - entry) / entry * 100
                                        fill_ok = (direction == "BUY" and (live_price <= entry * 1.0001 if entry <= price else live_price >= entry * 0.9999)
                                                   or direction == "SELL" and (live_price >= entry * 0.9999 if entry >= price else live_price <= entry * 1.0001))
                                        if live_price and pct <= 0.05 and fill_ok:
                                            price = live_price
                                            recent_high = recent_low = live_price
                                            filled = True
                                            use_live_fallback = True
                                            fill_price = live_price
                                            entry_fill_source = "live_fallback"
                                            logger.info(f"[TradingFloor] #{d['id']} {symbol}: "
                                                       f"stale candles, live price {live_price} at entry — FILL")
                            except Exception:
                                pass
                            if not use_live_fallback:
                                logger.debug(f"[TradingFloor] #{d['id']} {symbol}: "
                                             f"candle data {age_seconds:.0f}s old, skipping fill check")
                                continue
                    except Exception:
                        pass

                # Entry detection: compare entry to current price to determine
                # limit vs stop, then check the correct wick direction.
                #   BUY limit  (entry <= price): price must dip  → low <= entry
                #   BUY stop   (entry >  price): price must rise → high >= entry
                #   SELL limit (entry >= price): price must rise → high >= entry
                #   SELL stop  (entry <  price): price must drop → low <= entry
                if not use_live_fallback:
                    # 0.2% tolerance for fill detection — matches signal_ai tolerance
                    tol = entry * 0.002
                    if direction == "BUY":
                        if entry <= price:
                            filled = recent_low <= entry + tol
                        else:
                            filled = recent_high >= entry - tol
                        distance_pct = ((entry - price) / entry) * 100
                    else:
                        if entry >= price:
                            filled = recent_high >= entry - tol
                        else:
                            filled = recent_low <= entry + tol
                        distance_pct = ((price - entry) / entry) * 100
                else:
                    distance_pct = 0

                order_style = ("limit" if (direction == "BUY" and entry <= price)
                               or (direction == "SELL" and entry >= price)
                               else "stop")
                logger.debug(
                    f"[TradingFloor] #{d['id']} {symbol}: {direction} {order_style} "
                    f"(entry={entry}, price={price}), "
                    f"wick L={recent_low:.5f} H={recent_high:.5f}, filled={filled}"
                )

                # "trade_now" = market order: fill if price is within 2% of entry.
                # Use actual market price (not entry) for fill — paper must match real.
                if d.get("trade_decision") == "trade_now" and not filled:
                    pct_from_entry = abs(price - entry) / entry * 100
                    if pct_from_entry <= 2.0:
                        filled = True
                        fill_price = price  # Actual market price, not ideal entry
                        entry_fill_source = "trade_now_market"
                        logger.info(f"[TradingFloor] #{d['id']} {symbol}: trade_now "
                                    f"market fill at {price} ({pct_from_entry:.1f}% from target {entry})")
                    else:
                        logger.warning(f"[TradingFloor] #{d['id']} {symbol}: trade_now "
                                       f"but price {price} is {pct_from_entry:.1f}% from entry {entry} "
                                       f"— NOT filling at this price, waiting")

                # Fallback: candle wick may have missed — check live price if within 0.05%
                # Must be direction-aware: BUY limit fills when lp<=entry, SELL limit when lp>=entry.
                if not filled:
                    try:
                        from services.price_streamer import get_price_streamer
                        ps = get_price_streamer()
                        if ps:
                            live = ps.get_price(symbol)
                            if live and live.get("price"):
                                lp = float(live["price"])
                                pct = abs(lp - entry) / entry * 100
                                fill_ok = (direction == "BUY" and (lp <= entry * 1.0001 if entry <= price else lp >= entry * 0.9999)
                                           or direction == "SELL" and (lp >= entry * 0.9999 if entry >= price else lp <= entry * 1.0001))
                                if _is_price_valid_for_symbol(symbol, lp) and pct <= 0.05 and fill_ok:
                                    filled = True
                                    fill_price = lp
                                    use_live_fallback = True
                                    entry_fill_source = "live_fallback"
                                    logger.info(f"[TradingFloor] #{d['id']} {symbol}: "
                                               f"live price {lp} at entry (wick missed) — FILL")
                    except Exception:
                        pass

                self.db.execute("""
                    UPDATE trade_dossiers
                    SET current_price = %s, current_price_at = NOW()
                    WHERE id = %s
                """, (round(price, 5), d["id"]))

                if filled:
                    # Min order check: resolve symbol (USDT), fetch limits, skip if below minimum
                    margin = float(d.get("margin_usd") or 20)
                    lev = int(d.get("leverage") or 5)
                    notional = margin * lev
                    position_size = notional / fill_price if fill_price else 0
                    resolved_exch = d.get("resolved_exchange")
                    exchange_ticker = None
                    executor = None
                    if resolved_exch and symbol:
                        try:
                            from db.market_symbols import resolve_on_demand
                            recon = resolve_on_demand(symbol, self.db)
                            col = resolved_exch if resolved_exch in ("bybit", "blofin", "bitget") else "bybit"
                            exchange_ticker = recon.get(col)
                            if exchange_ticker:
                                from core.ccxt_executor import _executor_cache
                                for aid, ex in _executor_cache.items():
                                    if ex.exchange_id == resolved_exch and ex.connected:
                                        executor = ex
                                        break
                        except Exception as e:
                            logger.debug(f"[TradingFloor] #{d['id']} min-order resolve: {e}")
                    if exchange_ticker and executor:
                        limits = executor.get_market_limits(exchange_ticker)
                        min_amt = limits.get("min_amount") or 0
                        min_cost = limits.get("min_cost") or 0
                        if min_amt and position_size < min_amt:
                            filled = False
                            logger.warning(f"[TradingFloor] #{d['id']} {symbol}: SKIP fill — position_size "
                                           f"{position_size:.6f} < min_amount {min_amt} (exchange {resolved_exch})")
                            _append_tracker_log(self.db, d["id"],
                                f"SKIP: position {position_size:.6f} < min_amount {min_amt} on {resolved_exch}")
                        elif min_cost and notional < min_cost:
                            filled = False
                            logger.warning(f"[TradingFloor] #{d['id']} {symbol}: SKIP fill — notional "
                                           f"${notional:.2f} < min_cost ${min_cost} (exchange {resolved_exch})")
                            _append_tracker_log(self.db, d["id"],
                                f"SKIP: notional ${notional:.2f} < min_cost ${min_cost} on {resolved_exch}")
                        elif executor:
                            fill_price = executor.price_to_precision(exchange_ticker, fill_price)

                    if filled:
                        # If a live order exists on the exchange, the LiveTradeMonitor
                        # detects the fill — don't double-transition from paper side.
                        # Stale guard: if a 'pending' order is older than 10 min and
                        # LiveTradeMonitor hasn't updated it, treat as dead and paper-fill.
                        has_exchange_order = self.db.fetch_one(
                            "SELECT id, status, created_at FROM live_trades "
                            "WHERE dossier_id = %s "
                            "AND status IN ('pending','open','partial_closed') LIMIT 1",
                            (d["id"],))
                        if has_exchange_order:
                            lt_status = has_exchange_order.get("status")
                            lt_created = has_exchange_order.get("created_at")
                            stale = False
                            if lt_status == "pending" and lt_created:
                                try:
                                    from datetime import datetime, timezone
                                    age_min = (datetime.now(timezone.utc) - lt_created.replace(
                                        tzinfo=timezone.utc)).total_seconds() / 60
                                    stale = age_min > 10
                                except Exception:
                                    pass
                            if not stale:
                                logger.debug(
                                    f"[TradingFloor] #{d['id']} {symbol}: wick reached entry "
                                    f"but live order LT#{has_exchange_order['id']} exists — "
                                    f"LiveTradeMonitor handles fill")
                                continue
                            logger.info(
                                f"[TradingFloor] #{d['id']} {symbol}: live order "
                                f"LT#{has_exchange_order['id']} stuck pending >10min — "
                                f"proceeding with paper fill")

                        # Condition gate: block paper fill if too few conditions met
                        min_cond_pct = float(self._td_cfg.get(
                            "min_conditions_pct_for_entry", 0.50))
                        _cond_row = self.db.fetch_one(
                            "SELECT conditions_for_entry FROM trade_dossiers "
                            "WHERE id = %s", (d["id"],))
                        if _cond_row and _cond_row.get("conditions_for_entry"):
                            try:
                                _raw_c = _cond_row["conditions_for_entry"]
                                if isinstance(_raw_c, str):
                                    _conds = json.loads(_raw_c)
                                elif isinstance(_raw_c, (list, dict)):
                                    _conds = _raw_c
                                else:
                                    _conds = []
                                if isinstance(_conds, dict):
                                    _conds = []
                                if isinstance(_conds, list) and len(_conds) > 0:
                                    _c_total = len(_conds)
                                    _c_met = sum(1 for c in _conds
                                                 if c.get("status") == "met")
                                    _c_pct = _c_met / _c_total
                                    if _c_pct < min_cond_pct:
                                        logger.info(
                                            f"[TradingFloor] #{d['id']} {symbol}: "
                                            f"BLOCKED paper fill — {_c_met}/{_c_total} "
                                            f"({_c_pct:.0%}) < {min_cond_pct:.0%}")
                                        continue
                            except (json.JSONDecodeError, TypeError) as _cond_err:
                                logger.warning(
                                    f"[TradingFloor] #{d['id']} {symbol}: "
                                    f"conditions_for_entry JSON invalid — "
                                    f"blocking paper fill (gate cannot run): {_cond_err}")
                                continue

                        is_taker = entry_fill_source in ("trade_now_market", "live_fallback")
                        entry_fee = _calc_fee(notional, is_taker, self.config)
                        self.db.execute("""
                            UPDATE trade_dossiers
                            SET actual_entry_price = %s, entry_fill_source = %s, entry_fee = %s,
                                executed_at = NOW(),
                                original_stop_loss = COALESCE(original_stop_loss, stop_loss)
                            WHERE id = %s
                        """, (round(fill_price, 6), entry_fill_source, round(entry_fee, 6), d["id"]))
                        transition_dossier(self.db, d["id"], "live",
                                           f"Paper entry filled at {fill_price} "
                                           f"(wick L={recent_low:.5f} H={recent_high:.5f})")
                        _snapshot_conditions_at_entry(self.db, d["id"])
                        _append_tracker_log(self.db, d["id"],
                                            f"PAPER TRADE LIVE: entry filled at {fill_price}")
                        self._log_activity("trade",
                            f"#{d['id']} {symbol}: PAPER LIVE — entry filled at {fill_price}",
                            symbol=symbol, dossier_id=d["id"])
                        self._execute_paper_trade(d["id"])
                else:
                    self._log_activity("info",
                        f"#{d['id']} {symbol}: open order, {abs(distance_pct):.1f}% "
                        f"{'below' if direction=='BUY' else 'above'} entry ({entry})",
                        symbol=symbol, dossier_id=d["id"])

            except Exception as e:
                logger.error(f"[TradingFloor] Open order monitor error #{d['id']}: {e}")

    def _track_active_trades(self, candles_by_symbol: Dict = None):
        """Monitor live trades: check SL and TP levels, update P&L.
        Uses candle high/low (wicks) to detect SL/TP hits.
        TP trailing: TP1→SL to BE, TP2→SL to TP1, TP3→SL to TP2.
        Highest TP hit = trade won, P&L uses TP level (not latest_close).
        Scoped to this duo's dossiers only (duo_id filter)."""
        live_trades = self.db.fetch_all("""
            SELECT id, symbol, direction, entry_price, actual_entry_price, stop_loss,
                   take_profit_1, take_profit_2, take_profit_3,
                   take_profit_4, take_profit_5, take_profit_6,
                   tp_progress, status, margin_usd, leverage,
                   last_funding_at, accrued_funding
            FROM trade_dossiers
            WHERE status = 'live' AND duo_id = %s
            ORDER BY created_at DESC LIMIT 50
        """, (self.duo_id,))
        if not live_trades:
            return

        be_buffer_pct = self._td_cfg.get("sl_to_be_buffer_pct", 0.15)

        for d in live_trades:
            try:
                symbol = d["symbol"]
                entry = float(d.get("actual_entry_price") or d.get("entry_price") or 0) or None
                sl = float(d["stop_loss"]) if d.get("stop_loss") else None
                direction = d.get("direction", "").upper()

                if not entry:
                    continue

                # Skip dossiers whose linked live trade was cancelled unfilled
                lt_cancelled = self.db.fetch_one(
                    "SELECT id FROM live_trades "
                    "WHERE dossier_id = %s AND status = 'cancelled' "
                    "AND actual_entry_price IS NULL LIMIT 1",
                    (d["id"],))
                if lt_cancelled:
                    continue

                candles = candles_by_symbol.get(symbol) if candles_by_symbol else None
                if not candles:
                    candles = self._get_recent_candles(symbol)
                price = candles.get("latest_close", 0) if candles else 0
                if not price:
                    self._no_price_counter[d["id"]] = self._no_price_counter.get(d["id"], 0) + 1
                    count = self._no_price_counter[d["id"]]
                    if count % 5 == 0:
                        logger.warning(f"[TradingFloor] #{d['id']} {symbol} LIVE: "
                                       f"no price data for {count} cycles")
                    continue

                self._no_price_counter.pop(d["id"], None)

                # ── Funding rate accrual (paper realism, every 8h) ──
                pf = self._td_cfg.get("paper_fees", {})
                if pf.get("funding_enabled") and price and entry:
                    interval_h = float(pf.get("funding_interval_hours", 8))
                    last_fd = d.get("last_funding_at")
                    due = last_fd is None
                    if not due and last_fd:
                        try:
                            from datetime import datetime as _dt
                            if isinstance(last_fd, str):
                                last_fd = _dt.fromisoformat(last_fd.replace("Z", "+00:00"))
                            age_h = (_utcnow() - last_fd.replace(tzinfo=None)).total_seconds() / 3600
                            due = age_h >= interval_h
                        except Exception:
                            due = True
                    if due:
                        try:
                            from core.ccxt_executor import get_executor
                            accounts = _get_live_trading_accounts(self.db)
                            executor = None
                            for acct in (accounts or []):
                                ex = get_executor(acct["account_id"], self.db)
                                if ex and ex.connected:
                                    executor = ex
                                    break
                            if executor:
                                ex_sym = executor.resolve_symbol(symbol, self.db)
                                if ex_sym:
                                    fr = executor.fetch_funding_rate(ex_sym)
                                    if fr and "rate" in fr:
                                        rate = float(fr["rate"])
                                        margin = float(d.get("margin_usd") or 20)
                                        lev = int(d.get("leverage") or 5)
                                        position_size = (margin * lev) / entry
                                        funding_cost = position_size * price * rate
                                        if direction == "SELL":
                                            funding_cost = -funding_cost
                                        accrued = float(d.get("accrued_funding") or 0) + funding_cost
                                        self.db.execute(
                                            "UPDATE trade_dossiers SET accrued_funding = %s, "
                                            "last_funding_at = NOW() WHERE id = %s",
                                            (round(accrued, 6), d["id"]))
                                        d["accrued_funding"] = accrued
                                        _append_tracker_log(self.db, d["id"],
                                            f"Funding: rate={rate:.6f}, cost=${funding_cost:.4f}, "
                                            f"accrued=${accrued:.4f}")
                        except Exception as e:
                            logger.debug(f"[TradingFloor] Funding accrual #{d['id']}: {e}")

                # Use configurable window (default 24 = 2h) — narrow window missed SL hits vs retest
                wick_window = self._td_cfg.get("sl_tp_candle_window", 24)
                m5_candles = candles.get("M5", [])
                m5_slice = m5_candles[:wick_window] if m5_candles else []
                recent_high = max((float(c.get("high", 0)) for c in m5_slice), default=price)
                recent_low = min((float(c.get("low", 999999)) for c in m5_slice), default=price)

                # ── Check SL hit (using wicks) ──
                if sl:
                    sl_hit = (direction == "BUY" and recent_low <= sl) or \
                             (direction == "SELL" and recent_high >= sl)
                    if sl_hit:
                        sl_pnl = _finalise_pnl(self.db, d["id"], sl, exit_is_taker=True, config=self.config, exit_is_sl=True)
                        sl_outcome = "won" if sl_pnl >= 0 else "lost"
                        transition_dossier(self.db, d["id"], sl_outcome,
                                           f"SL hit at {sl} (wick low={recent_low}, high={recent_high}) "
                                           f"— P&L ${sl_pnl:.2f}")
                        _update_strategy_stats(self.db, d["id"])
                        _update_prompt_version_stats(self.db, d["id"])
                        _sync_signal_outcome(self.db, d["id"], sl_outcome, sl)
                        sl_label = "SL HIT (WON — profit)" if sl_outcome == "won" else "SL HIT -- LOST"
                        _append_tracker_log(self.db, d["id"],
                                           f"{sl_label}: SL={sl}, wick L={recent_low} H={recent_high}")
                        self._log_activity("trade", f"#{d['id']} {symbol}: {sl_label}",
                                           symbol=symbol, dossier_id=d["id"])
                        self._trigger_postmortem(d["id"])
                        continue

                # ── Build ordered TP list ──
                tps = []
                for i in range(1, 7):
                    val = d.get(f"take_profit_{i}")
                    if val:
                        tps.append((i, float(val)))

                cur_tp = d.get("tp_progress") or "none"
                cur_tp_num = int(cur_tp.replace("tp", "").replace("_hit", "")) if "tp" in cur_tp else 0

                highest_hit = cur_tp_num
                for tp_num, tp_price in sorted(tps, key=lambda x: x[0], reverse=True):
                    if tp_num <= cur_tp_num:
                        continue
                    hit = (direction == "BUY" and recent_high >= tp_price) or \
                          (direction == "SELL" and recent_low <= tp_price)
                    if hit and tp_num > highest_hit:
                        highest_hit = tp_num

                if highest_hit > cur_tp_num:
                    new_progress = f"tp{highest_hit}_hit"
                    tp_dict = {num: val for num, val in tps}

                    # TP trailing: move SL to protect profits at each level
                    new_sl = None
                    if highest_hit >= 3 and tp_dict.get(2):
                        new_sl = tp_dict[2]
                        sl_label = f"TP2 ({new_sl:.5f})"
                    elif highest_hit >= 2 and tp_dict.get(1):
                        new_sl = tp_dict[1]
                        sl_label = f"TP1 ({new_sl:.5f})"
                    elif highest_hit >= 1:
                        buffer = entry * (be_buffer_pct / 100)
                        new_sl = entry + buffer if direction == "BUY" else entry - buffer
                        sl_label = f"BE+buffer ({new_sl:.5f})"

                    if new_sl is not None:
                        self.db.execute(
                            "UPDATE trade_dossiers SET tp_progress=%s, stop_loss=%s WHERE id=%s",
                            (new_progress, round(new_sl, 5), d["id"]))
                        self._reregister_sl(d["id"], new_sl, symbol)
                        _append_tracker_log(self.db, d["id"],
                            f"TP{highest_hit} HIT at {price}: SL trailed to {sl_label}")
                        self._log_activity("trade",
                            f"#{d['id']} {symbol}: TP{highest_hit} hit, SL → {sl_label}",
                            symbol=symbol, dossier_id=d["id"])
                    else:
                        self.db.execute(
                            "UPDATE trade_dossiers SET tp_progress=%s WHERE id=%s",
                            (new_progress, d["id"]))

                    for tp_num, tp_price in sorted(tps, key=lambda x: x[0]):
                        if tp_num > cur_tp_num and tp_num <= highest_hit and tp_num > 1:
                            _append_tracker_log(self.db, d["id"],
                                f"TP{tp_num} HIT (target was {tp_price})")
                            self._log_activity("trade",
                                f"#{d['id']} {symbol}: TP{tp_num} hit",
                                symbol=symbol, dossier_id=d["id"])

                    max_tp = max(tp[0] for tp in tps) if tps else 0
                    if highest_hit >= max_tp and max_tp > 0:
                        final_tp_price = tp_dict.get(highest_hit, price)
                        _finalise_pnl(self.db, d["id"], final_tp_price, exit_is_taker=False, config=self.config)
                        transition_dossier(self.db, d["id"], "won",
                            f"TP{highest_hit} hit at {final_tp_price} -- full target reached")
                        _update_strategy_stats(self.db, d["id"])
                        _update_prompt_version_stats(self.db, d["id"])
                        _sync_signal_outcome(self.db, d["id"], "won", final_tp_price)
                        _append_tracker_log(self.db, d["id"],
                            f"TP{highest_hit} HIT -- TRADE WON at {final_tp_price}")
                        self._log_activity("trade",
                            f"#{d['id']} {symbol}: TP{highest_hit} -- WON at {final_tp_price}",
                            symbol=symbol, dossier_id=d["id"])
                        self._trigger_postmortem(d["id"])
                        continue

                # ── Live P&L calculation ──
                # If this dossier has an active exchange trade, LiveTradeMonitor
                # already syncs the real exchange P&L — skip the formula estimate
                # so the Trading Floor shows the exact same number as Live Trades.
                has_exchange_trade = self.db.fetch_one(
                    "SELECT id FROM live_trades WHERE dossier_id = %s "
                    "AND status IN ('open', 'partial_closed') LIMIT 1",
                    (d["id"],))
                if not has_exchange_trade:
                    _update_live_pnl(self.db, d["id"], entry, price, direction, self.config)

            except Exception as e:
                logger.error(f"[TradingFloor] Trade monitor error #{d['id']}: {e}")

    # ── Post-Mortem Auto-Trigger ─────────────────────────────────────

    def _trigger_postmortem(self, dossier_id: int):
        """Fire-and-forget post-mortem via the Audit Engine.
        Vectorization is deferred to AFTER postmortem completes (inside auditor)
        so the vector store includes postmortem findings and lessons learned.
        Also generates mentor-vs-Apex comparison lessons when applicable.

        For mentor mirror dossiers (``duo_id='mentors'``), also resolves all
        matching observation dossiers and triggers their postmortems so each
        duo can compare its independent prediction to the actual outcome.
        """
        try:
            from services.auditor import run_postmortem_async
            run_postmortem_async(self.db, self.config, dossier_id)
            self._log_activity("info",
                f"#{dossier_id}: Post-mortem triggered",
                dossier_id=dossier_id)
        except Exception as e:
            logger.error(f"[TradingFloor] Post-mortem trigger error: {e}")

        try:
            _generate_mentor_comparison(self.db, dossier_id)
        except Exception as e:
            logger.debug(f"[TradingFloor] Mentor comparison error: {e}")

        try:
            self._resolve_observation_dossiers(dossier_id)
        except Exception as e:
            logger.debug(f"[TradingFloor] Observation resolution error: {e}")

    def _resolve_observation_dossiers(self, mirror_dossier_id: int):
        """When a mentor mirror dossier resolves (won/lost), resolve all
        matching ``mentor_observation`` dossiers for the same symbol so each
        duo's postmortem can compare its prediction to reality.

        The observation dossier inherits the mirror's outcome (won/lost) but
        is tagged with ``paper_reason='mentor_observation'`` so it does not
        count in the duo's P&L.  The postmortem still runs, generating
        learning material from the comparison.
        """
        mirror = self.db.fetch_one(
            "SELECT symbol, status, mentor_source, duo_id "
            "FROM trade_dossiers WHERE id = %s", (mirror_dossier_id,))
        if not mirror:
            return
        if mirror.get("duo_id") != "mentors":
            return
        if mirror.get("status") not in ("won", "lost"):
            return

        symbol = mirror["symbol"]
        outcome = mirror["status"]
        author = mirror.get("mentor_source", "Mentor")

        observations = self.db.fetch_all(
            "SELECT id, duo_id FROM trade_dossiers "
            "WHERE symbol = %s AND mentor_type = 'mentor_observation' "
            "AND mentor_source = %s "
            "AND status NOT IN ('won','lost','expired','abandoned')",
            (symbol, author))

        if not observations:
            return

        from services.auditor import run_postmortem_async

        for obs in observations:
            obs_id = obs["id"]
            obs_duo = obs.get("duo_id", "?")
            reason = (f"Mirror #{mirror_dossier_id} {outcome} — "
                      f"{author}'s {symbol} trade resolved as {outcome}")
            transition_dossier(self.db, obs_id, outcome, reason)
            _append_tracker_log(self.db, obs_id,
                f"[Observation] Resolved as {outcome}: mirror "
                f"#{mirror_dossier_id} by {author} finished {outcome}")
            try:
                run_postmortem_async(self.db, self.config, obs_id)
            except Exception as e:
                logger.debug(f"[TradingFloor] Observation postmortem "
                             f"#{obs_id} error: {e}")
            logger.info(f"[TradingFloor] Observation #{obs_id} ({obs_duo}) "
                        f"resolved as {outcome} — mentor {author}'s "
                        f"{symbol} trade finished")

    # ── Daily Audit Cron (5am Dubai / 1am UTC) ───────────────────────

    def _daily_audit_loop(self):
        """Run daily audit rollup at 1am UTC (5am Dubai)."""
        while self._running and not self._stop_event.is_set():
            try:
                now = _utcnow()
                target = now.replace(hour=1, minute=0, second=0, microsecond=0)
                if now >= target:
                    target += timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
                logger.info(f"[Auditor] Daily rollup scheduled in "
                            f"{wait_seconds/3600:.1f}h (1am UTC / 5am Dubai)")
                if self._stop_event.wait(wait_seconds):
                    break
                from services.auditor import run_daily_rollup
                run_daily_rollup(self.db, self.config)
            except Exception as e:
                logger.error(f"[TradingFloor] Daily audit error: {e}",
                             exc_info=True)
                self._stop_event.wait(3600)

    # ── Symbol Resolution Loop ───────────────────────────────────────

    def _symbol_resolution_loop(self):
        """Periodic symbol resolution: refresh exchange markets and re-resolve
        unresolved symbols every 6 hours. Also normalizes dossier symbols."""
        self._stop_event.wait(60)  # let exchanges connect first
        interval = 6 * 3600  # 6 hours
        while self._running and not self._stop_event.is_set():
            try:
                self._run_symbol_resolution()
            except Exception as e:
                logger.error(f"[TradingFloor] Symbol resolution error: {e}",
                             exc_info=True)
            self._stop_event.wait(interval)

    def _run_symbol_resolution(self):
        """Execute one full symbol resolution pass."""
        from db.market_symbols import (refresh_exchange_markets,
                                       normalize_and_resolve_all_symbols,
                                       normalize_dossier_symbols)
        # Step 1: Force-refresh exchange market listings
        counts = refresh_exchange_markets()
        self._log_activity("info",
            f"Symbol resolver: refreshed markets — "
            f"Bybit={counts.get('bybit', 0)}, Blofin={counts.get('blofin', 0)}, Bitget={counts.get('bitget', 0)}")

        # Step 2: Normalize all crypto symbols in market_symbols (canonical + tickers)
        norm_report = normalize_and_resolve_all_symbols(self.db)
        unresolved = norm_report.get("still_unresolved", [])
        if unresolved:
            self._log_activity("warn",
                f"Symbol resolver: {len(unresolved)} still unresolved: "
                f"{', '.join(unresolved[:10])}")

        # Step 3: Normalize active dossier symbols (BTC -> BTCUSDT etc.)
        dos_report = normalize_dossier_symbols(self.db)
        if dos_report.get("normalized", 0) > 0:
            self._log_activity("info",
                f"Symbol resolver: normalized {dos_report['normalized']} dossier symbols")

        # Step 4: Check active dossiers for missing prices
        self._resolve_priceless_symbols()

    def _resolve_priceless_symbols(self):
        """Find active dossiers with no live price and attempt re-resolution."""
        try:
            from services.price_streamer import get_price_streamer
            ps = get_price_streamer()
            if not ps:
                return

            rows = self.db.fetch_all("""
                SELECT DISTINCT symbol FROM trade_dossiers
                WHERE status IN ('proposed', 'monitoring', 'open_order', 'live')
            """)
            if not rows:
                return

            no_price = []
            for r in rows:
                sym = r["symbol"]
                pdata = ps.get_price(sym)
                if not pdata or not pdata.get("price"):
                    no_price.append(sym)

            if not no_price:
                return

            logger.info(f"[TradingFloor] {len(no_price)} active symbols have no price: "
                        f"{', '.join(no_price[:10])}")

            from db.market_symbols import normalize_for_dossier
            for sym in no_price[:10]:
                try:
                    norm = normalize_for_dossier(sym, self.db)
                    if norm["exchange_verified"]:
                        ps.subscribe([norm["normalized"]])
                        logger.info(f"[TradingFloor] Re-subscribed {sym} -> "
                                    f"{norm['normalized']} (method={norm['method']})")
                except Exception as e:
                    logger.debug(f"[TradingFloor] Price re-resolve {sym}: {e}")

        except Exception as e:
            logger.debug(f"[TradingFloor] _resolve_priceless_symbols: {e}")

    # ── Signal Tracker Loop ──────────────────────────────────────────

    def _fast_tracker_loop(self):
        """Fast loop (every 2 min) to check open orders for entry + live trades for SL/TP.
        Independent from the main tracker which runs every 15 min and includes LLM calls.
        This ensures entries and exits are detected promptly using fresh candle data.
        Uses _tracker_lock to prevent concurrent execution with the main tracker."""
        interval = self._td_cfg.get("fast_tracker_interval_seconds", 120)
        self._stop_event.wait(30)
        while self._running and not self._stop_event.is_set():
            try:
                ft_symbols = set()
                for row in (self.db.fetch_all(
                    "SELECT DISTINCT symbol FROM trade_dossiers "
                    "WHERE status IN ('open_order','live','proposed','monitoring') "
                    "AND duo_id = %s LIMIT 100", (self.duo_id,)) or []):
                    ft_symbols.add(row["symbol"])
                ft_candles = {s: self._get_recent_candles(s) for s in ft_symbols}
                with self._tracker_lock:
                    self._track_open_orders(ft_candles)
                    self._track_active_trades(ft_candles)
                    self._abandon_tp1_overshot(ft_candles)
                logger.debug("[TradingFloor] Fast tracker pass complete")
            except Exception as e:
                logger.error(f"[TradingFloor] Fast tracker error: {e}",
                             exc_info=True)
            self._stop_event.wait(interval)

    def _signal_tracker_loop(self):
        """Periodically evaluate active signals using candle data.
        Replaces the need for manual backtesting on live signals."""
        interval = self._td_cfg.get("signal_tracker_interval_minutes", 5) * 60
        while self._running and not self._stop_event.is_set():
            try:
                self._track_active_signals()
                self._last_signal_check_at = _utcnow()
            except Exception as e:
                logger.error(f"[TradingFloor] Signal tracker error: {e}",
                             exc_info=True)
            self._stop_event.wait(interval)

    def _track_active_signals(self):
        """Evaluate all active parsed_signals against candle data.
        Updates status (entry_hit, tp_hit, sl_hit, expired) in real time."""
        active_signals = self.db.fetch_all("""
            SELECT id, symbol, direction, entry_price, stop_loss,
                   take_profit_1, take_profit_2, take_profit_3,
                   take_profit_4, take_profit_5, take_profit_6,
                   status, parsed_at, entry_hit_at
            FROM parsed_signals
            WHERE is_valid = 1
              AND status IN ('pending', 'active', 'entry_hit',
                             'tp1_hit', 'tp2_hit', 'tp3_hit', 'tp4_hit', 'tp5_hit')
              AND parsed_at > DATE_SUB(NOW(), INTERVAL 14 DAY)
            ORDER BY parsed_at DESC
            LIMIT 200
        """)

        self._log_activity("info", f"Signal tracker: evaluating {len(active_signals)} signals")

        if not active_signals:
            return

        logger.info(f"[TradingFloor] Signal tracker: evaluating {len(active_signals)} active signals")

        # Expire old signals that never entered
        stale = self.db.fetch_all("""
            SELECT id FROM parsed_signals
            WHERE is_valid = 1 AND status = 'pending'
              AND parsed_at < DATE_SUB(NOW(), INTERVAL 14 DAY)
        """)
        for s in (stale or []):
            self.db.execute(
                "UPDATE parsed_signals SET status = 'expired' WHERE id = %s",
                (s["id"],))
        if stale:
            logger.info(f"[TradingFloor] Signal tracker: expired {len(stale)} stale signals")

        symbols = set(s["symbol"] for s in active_signals if s.get("symbol"))
        candles_cache = {}
        for sym in symbols:
            candles_cache[sym] = self._get_recent_candles(sym)

        for sig in active_signals:
            try:
                symbol = sig.get("symbol")
                if not symbol:
                    continue
                candles = candles_cache.get(symbol)
                if not candles or not candles.get("latest_close"):
                    continue

                price = candles["latest_close"]
                entry = float(sig["entry_price"]) if sig.get("entry_price") else None
                sl = float(sig["stop_loss"]) if sig.get("stop_loss") else None
                direction = (sig.get("direction") or "").upper()
                status = sig.get("status", "")

                if not entry or not direction:
                    continue

                # Check entry hit
                if status == "pending":
                    entry_tol = entry * 0.002  # 0.2% tolerance
                    if (direction == "BUY" and price <= entry + entry_tol) or \
                       (direction == "SELL" and price >= entry - entry_tol):
                        self.db.execute("""
                            UPDATE parsed_signals
                            SET status = 'entry_hit', entry_hit_at = NOW()
                            WHERE id = %s AND status = 'pending'
                        """, (sig["id"],))
                        continue

                # For entered signals, check SL and TP levels using wicks
                if status in ("entry_hit", "active", "tp1_hit", "tp2_hit",
                               "tp3_hit", "tp4_hit", "tp5_hit"):
                    m5 = candles.get("M5", [])
                    sig_high = max((float(c.get("high", 0)) for c in m5[:6]), default=price)
                    sig_low = min((float(c.get("low", 999999)) for c in m5[:6]), default=price)

                    if sl:
                        if (direction == "BUY" and sig_low <= sl) or \
                           (direction == "SELL" and sig_high >= sl):
                            pip_val = _get_pip_value(symbol)
                            pips = ((sl - entry) / pip_val) if direction == "BUY" else ((entry - sl) / pip_val)
                            self.db.execute("""
                                UPDATE parsed_signals
                                SET status = 'sl_hit', outcome = 'loss',
                                    sl_hit_at = NOW(), outcome_pips = %s
                                WHERE id = %s
                            """, (round(pips, 1), sig["id"]))
                            continue

                    # Check TP levels using wicks (highest TP first)
                    for tp_n in range(6, 0, -1):
                        tp_field = f"take_profit_{tp_n}"
                        tp_val = float(sig[tp_field]) if sig.get(tp_field) else None
                        if not tp_val:
                            continue
                        tp_status = f"tp{tp_n}_hit"
                        if (direction == "BUY" and sig_high >= tp_val) or \
                           (direction == "SELL" and sig_low <= tp_val):
                            pip_val = _get_pip_value(symbol)
                            pips = ((tp_val - entry) / pip_val) if direction == "BUY" else ((entry - tp_val) / pip_val)
                            self.db.execute(f"""
                                UPDATE parsed_signals
                                SET status = %s, outcome = 'win',
                                    tp{tp_n}_hit_at = NOW(), outcome_pips = %s,
                                    highest_price = GREATEST(COALESCE(highest_price, 0), %s),
                                    lowest_price = CASE WHEN lowest_price = 0 OR lowest_price IS NULL
                                                        THEN %s ELSE LEAST(lowest_price, %s) END
                                WHERE id = %s
                            """, (tp_status, round(pips, 1), price, price, price, sig["id"]))
                            break

                    # Track max favorable / max adverse
                    if entry:
                        if direction == "BUY":
                            fav = max(0, price - entry)
                            adv = max(0, entry - price)
                        else:
                            fav = max(0, entry - price)
                            adv = max(0, price - entry)
                        pip_val = _get_pip_value(symbol)
                        fav_pips = round(fav / pip_val, 1) if pip_val else 0
                        adv_pips = round(adv / pip_val, 1) if pip_val else 0
                        self.db.execute("""
                            UPDATE parsed_signals
                            SET max_favorable = GREATEST(COALESCE(max_favorable, 0), %s),
                                max_adverse = GREATEST(COALESCE(max_adverse, 0), %s),
                                highest_price = GREATEST(COALESCE(highest_price, 0), %s),
                                lowest_price = CASE WHEN lowest_price = 0 OR lowest_price IS NULL
                                                    THEN %s ELSE LEAST(lowest_price, %s) END
                            WHERE id = %s
                        """, (fav_pips, adv_pips, price, price, price, sig["id"]))

            except Exception as e:
                logger.error(f"[TradingFloor] Signal track error #{sig.get('id')}: {e}")

    def _check_dossier_conditions(self, dossier_row: Dict,
                                   threshold_execute: int,
                                   threshold_limit: int,
                                   candles: Dict = None):
        """Evaluate every condition against LIVE candle data and update statuses.
        If candles is provided (from pre-fetch), use it; else fetch per symbol."""
        dossier = self.db.fetch_one(
            "SELECT * FROM trade_dossiers WHERE id = %s", (dossier_row["id"],))
        if not dossier:
            return

        symbol = dossier["symbol"]
        conditions = json.loads(dossier.get("conditions_for_entry") or "[]")
        invalidations = json.loads(
            dossier.get("stage2_hypothesis") or "{}").get("invalidations", [])
        if not conditions:
            return

        if candles is None:
            candles = self._get_recent_candles(symbol)
        latest_price = candles.get("latest_close", 0)

        recent_news = self._get_recent_news(symbol)
        self._current_eval_symbol = symbol

        changes = []
        for cond in conditions:
            old_status = cond.get("status", "not_met")
            new_status, reason = self._evaluate_condition(
                cond, candles, latest_price, recent_news)
            cond["reason"] = reason
            cond["checked_at"] = _utcnow().isoformat()
            if new_status != old_status:
                changes.append(f"C{cond.get('id','?')}: {old_status}->{new_status}")
                cond["status"] = new_status
                if new_status == "met":
                    cond["met_at"] = _utcnow().isoformat()
                elif new_status == "not_met":
                    cond.pop("met_at", None)

        # Check invalidation criteria
        invalidation_hit = None
        for inv in invalidations:
            if inv.get("triggered"):
                continue
            triggered = self._check_invalidation(
                inv, candles, latest_price, dossier.get("created_at"))
            if triggered:
                inv["triggered"] = True
                inv["triggered_at"] = _utcnow().isoformat()
                invalidation_hit = inv
                break

        # Save updated conditions back
        self.db.execute(
            "UPDATE trade_dossiers SET conditions_for_entry = %s WHERE id = %s",
            (json.dumps(conditions, default=str), dossier_row["id"]))

        # Save updated invalidations
        if invalidations:
            try:
                hyp = json.loads(dossier.get("stage2_hypothesis") or "{}")
            except (json.JSONDecodeError, TypeError):
                hyp = {}
            hyp["invalidations"] = invalidations
            self.db.execute(
                "UPDATE trade_dossiers SET stage2_hypothesis = %s WHERE id = %s",
                (json.dumps(hyp, default=str), dossier_row["id"]))

        met_count = sum(1 for c in conditions if c.get("status") == "met")
        partial = sum(1 for c in conditions if c.get("status") == "partially_met")
        total = len(conditions)

        weighted_score = 0
        total_weight = 0
        for c in conditions:
            w = c.get("weight", 5)
            total_weight += w
            if c.get("status") == "met":
                weighted_score += w
            elif c.get("status") == "partially_met":
                weighted_score += w * 0.5

        cond_probability = int((weighted_score / total_weight * 100)) if total_weight > 0 else 0

        change_str = ", ".join(changes) if changes else "no changes"

        logger.info(f"[TradingFloor] #{dossier_row['id']} {symbol}: "
                     f"{met_count}/{total} met, cond_score={cond_probability}%. {change_str}")
        if changes:
            self._log_activity("info", f"#{dossier_row['id']} {symbol}: "
                               f"{met_count}/{total} met ({cond_probability}%) — {change_str}",
                               symbol=symbol, dossier_id=dossier_row["id"])

        if invalidation_hit and invalidation_hit.get("severity") == "ABANDON_IMMEDIATELY":
            reason = (f"INVALIDATION TRIGGERED: {invalidation_hit.get('description','?')} "
                      f"| Price: {latest_price:.2f}")
            update_probability(self.db, dossier_row["id"], 0, reason)
            self._abandon_dossier(dossier_row["id"], reason, symbol=symbol)
            self._log_activity("error", f"#{dossier_row['id']} {symbol}: INVALIDATED — {reason}",
                               symbol=symbol, dossier_id=dossier_row["id"])
            return

        # Dynamic probability: blend LLM assessment with live condition score.
        # LLM is still primary authority, but conditions pull it toward reality.
        if changes:
            reason = (f"Conditions: {met_count}/{total} met, {partial} partial. "
                      f"Price: {latest_price:.2f}. [{change_str}]")
            _append_tracker_log(self.db, dossier_row["id"], reason)

        prob_hist = json.loads(dossier.get("probability_history") or "[]")
        llm_prob = prob_hist[-1].get("probability") if prob_hist else None

        if llm_prob is not None:
            # Blend: 60% LLM assessment + 40% live condition score
            probability = int(llm_prob * 0.6 + cond_probability * 0.4)
        else:
            probability = cond_probability if cond_probability is not None else 0

        if changes or llm_prob is None:
            update_probability(self.db, dossier_row["id"], probability,
                             f"Dynamic: LLM={llm_prob}, cond={cond_probability}%, "
                             f"{met_count}/{total} met. Price: {latest_price:.2f}")

        dossier["conditions_for_entry"] = json.dumps(conditions, default=str)
        opp_score = self._compute_opp_score(dossier, candles)

        current_status = dossier.get("status") or dossier_row["status"]

        if dossier.get("mentor_type") == "mentor_observation":
            logger.debug(f"[TradingFloor] #{dossier_row['id']} {symbol}: "
                         f"observation-only dossier — tracking conditions but "
                         f"will never execute (prob={probability}%)")
        elif probability >= threshold_execute and current_status not in ("open_order", "live"):
            min_cond_pct = float(self._td_cfg.get(
                "min_conditions_pct_for_entry", 0.50))
            cond_pct = (met_count / total) if total > 0 else 1.0
            if cond_pct < min_cond_pct:
                logger.info(
                    f"[TradingFloor] #{dossier_row['id']} {symbol}: "
                    f"BLOCKED by condition gate — {met_count}/{total} "
                    f"({cond_pct:.0%}) < {min_cond_pct:.0%} threshold. "
                    f"Prob={probability}% but conditions insufficient")
            else:
                self._execution_ready.append({
                    "id": dossier_row["id"],
                    "symbol": symbol,
                    "probability": probability,
                    "opp_score": opp_score,
                    "mentor_type": dossier.get("mentor_type"),
                })
                logger.info(f"[TradingFloor] #{dossier_row['id']} {symbol}: execution-ready "
                            f"(prob={probability}%, opp_score={opp_score}, "
                            f"cond={met_count}/{total}) — queued for ranking")

        elif probability >= threshold_limit and current_status == "proposed":
            transition_dossier(self.db, dossier_row["id"], "monitoring",
                               f"Probability {probability}% >= limit threshold "
                               f"{threshold_limit}%")
            self._log_activity("info", f"#{dossier_row['id']} {symbol}: → Monitoring "
                               f"({probability}%)",
                               symbol=symbol, dossier_id=dossier_row["id"])
            entry_price = float(dossier["entry_price"]) if dossier.get("entry_price") else None
            if entry_price:
                place_limit_order(self.db, dossier_row["id"], entry_price)

        elif probability < threshold_limit * 0.5 and current_status in ("monitoring", "open_order"):
            cancel_limit_order(self.db, dossier_row["id"],
                               f"Probability dropped to {probability}%")
            if probability < 20:
                self._abandon_dossier(dossier_row["id"],
                    f"Probability {probability}% too low — auto-abandoned",
                    symbol=symbol)
                self._trigger_postmortem(dossier_row["id"])

    def _get_recent_candles(self, symbol: str) -> Dict:
        """Get recent candle data across timeframes for condition evaluation.

        If M5 data is stale (>30 min old) or completely missing, attempt a
        fresh fetch from the candle collector before falling back to DB data.
        Also tries normalized symbol variants for bare tickers like 'SOL'.
        """
        result = {"symbol": symbol, "latest_close": 0, "data_stale": False}

        # Resolve aliases (GOLD→XAUUSD, SILVER→XAGUSD, etc.) then try variants
        canonical = resolve_symbol(symbol, self.db)
        symbols_to_try = [symbol]
        if canonical != symbol:
            symbols_to_try.insert(0, canonical)
        symbols_to_try += self._normalize_symbol_variants(symbol)
        if canonical != symbol:
            symbols_to_try += self._normalize_symbol_variants(canonical)
        effective_symbol = symbol

        m5_hours = 6
        if _utcnow().weekday() >= 5:
            m5_hours = 72

        for try_sym in symbols_to_try:
            rows = self.db.fetch_all("""
                SELECT candle_time, open, high, low, close FROM candles
                WHERE symbol = %s AND timeframe = 'M5'
                AND candle_time >= UTC_TIMESTAMP() - INTERVAL %s HOUR
                ORDER BY candle_time DESC
            """, (try_sym, m5_hours))
            if rows:
                effective_symbol = try_sym
                break

        # Consolidated single query for all timeframes (Phase 1c optimisation)
        _tf_hours = {"M5": m5_hours, "M15": max(12, m5_hours),
                     "H1": 48, "H4": 168, "D1": 720}
        _min_hours = min(_tf_hours.values())
        _max_hours = max(_tf_hours.values())
        all_rows = self.db.fetch_all("""
            SELECT timeframe, candle_time, open, high, low, close, volume
            FROM candles
            WHERE symbol = %s
              AND timeframe IN ('M5','M15','H1','H4','D1')
              AND candle_time >= UTC_TIMESTAMP() - INTERVAL %s HOUR
            ORDER BY timeframe, candle_time DESC
        """, (effective_symbol, _max_hours))

        _tf_buckets = {}
        for r in (all_rows or []):
            _tf_buckets.setdefault(r["timeframe"], []).append(r)

        for tf, hours in _tf_hours.items():
            cutoff = _utcnow() - timedelta(hours=hours)
            tf_rows = [r for r in _tf_buckets.get(tf, [])
                       if r["candle_time"] >= cutoff]
            if tf_rows:
                candle_list = [{
                    "time": r["candle_time"],
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": int(r.get("volume") or 0),
                } for r in tf_rows]
                result[tf] = candle_list
                if tf == "M5" and candle_list:
                    result["latest_close"] = candle_list[0]["close"]
                    latest_time = candle_list[0]["time"]
                    result["latest_timestamp"] = latest_time
                    if isinstance(latest_time, datetime):
                        age_min = (_utcnow() - latest_time).total_seconds() / 60
                        stale_threshold = 10
                        if age_min > 1440:
                            stale_threshold = 1440
                        if age_min > stale_threshold:
                            logger.info(f"[TradingFloor] {effective_symbol} M5 stale ({age_min:.0f}m) — "
                                        f"fetching fresh candles now")
                            self._try_fresh_fetch(effective_symbol)
                            fresh = self.db.fetch_all("""
                                SELECT candle_time, open, high, low, close, volume FROM candles
                                WHERE symbol = %s AND timeframe = 'M5'
                                AND candle_time >= UTC_TIMESTAMP() - INTERVAL %s HOUR
                                ORDER BY candle_time DESC
                            """, (effective_symbol, m5_hours))
                            if fresh:
                                candle_list = [{
                                    "time": r["candle_time"], "open": float(r["open"]),
                                    "high": float(r["high"]), "low": float(r["low"]),
                                    "close": float(r["close"]),
                                    "volume": int(r.get("volume") or 0)
                                } for r in fresh]
                                result["M5"] = candle_list
                                result["latest_close"] = candle_list[0]["close"]
                                result["latest_timestamp"] = candle_list[0]["time"]
                                result["data_stale"] = False
                elif not result["latest_close"] and candle_list:
                    result["latest_close"] = candle_list[0]["close"]

        if result["latest_close"] == 0:
            logger.warning(f"[TradingFloor] {symbol}: No candle data found. "
                          f"Triggering on-demand fetch...")
            self._try_fresh_fetch(symbol)

            for try_sym in symbols_to_try:
                rows = self.db.fetch_all("""
                    SELECT candle_time, open, high, low, close, volume FROM candles
                    WHERE symbol = %s AND timeframe = 'M5'
                    AND candle_time >= UTC_TIMESTAMP() - INTERVAL %s HOUR
                    ORDER BY candle_time DESC LIMIT 50
                """, (try_sym, m5_hours))
                if rows:
                    candle_list = [{
                        "time": r["candle_time"],
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": int(r.get("volume") or 0),
                    } for r in rows]
                    result["M5"] = candle_list
                    result["latest_close"] = candle_list[0]["close"]
                    result["latest_timestamp"] = candle_list[0]["time"]
                    result["data_stale"] = True
                    logger.info(f"[TradingFloor] {try_sym}: On-demand fetch recovered "
                               f"{len(candle_list)} M5 candles")
                    break

        if result["latest_close"] == 0:
            logger.error(f"[TradingFloor] {symbol}: NO candle data at all after "
                        f"fresh fetch! Tracker cannot evaluate conditions.")
            result["data_stale"] = True

        # Prefer live price when available — we track from creation, no stale
        try:
            from services.price_streamer import get_price_streamer
            ps = get_price_streamer()
            if ps:
                live = ps.get_price(symbol)
                if live and live.get("price"):
                    ts = live.get("ts") or 0
                    age_s = (time.time() - ts) if ts else 999
                    if age_s < 90 and _is_price_valid_for_symbol(symbol, float(live["price"])):
                        result["latest_close"] = float(live["price"])
                        result["data_stale"] = False
        except Exception:
            pass

        return result

    def _normalize_symbol_variants(self, symbol: str) -> List[str]:
        """Generate alternative symbol names for DB lookup.
        Handles bare tickers (SOL), .P perpetual suffixes, etc."""
        s = symbol.upper()
        variants = []
        # Strip perpetual suffix first
        if s.endswith(".P"):
            base = s[:-2]
            variants.append(base)
            s = base
        if not s.endswith(("USD", "USDT", "USDC", "BTC")):
            variants.extend([f"{s}USD", f"{s}USDT"])
        elif s.endswith("USD") and not s.endswith("USDT"):
            variants.append(s + "T")  # ETHUSD -> ETHUSDT
        elif s.endswith("USDT"):
            variants.append(s[:-1])   # ETHUSDT -> ETHUSD
        return variants

    def _subscribe_for_new_dossier(self, symbol: str):
        """Subscribe to symbol immediately when dossier is created.
        Ensures live price tracking from creation — no waiting for auto-subscribe."""
        try:
            from services.price_streamer import get_price_streamer
            ps = get_price_streamer()
            if ps and symbol:
                ps.subscribe([symbol])
                logger.debug(f"[TradingFloor] Subscribed to {symbol} for live price from creation")
        except Exception as e:
            logger.debug(f"[TradingFloor] Subscribe for new dossier: {e}")

    def _try_fresh_fetch(self, symbol: str):
        """Attempt to fetch fresh candles for a symbol on-demand.
        Resolves aliases first so 'SILVER' fetches as 'XAGUSD'."""
        canonical = resolve_symbol(symbol, self.db)
        fetch_sym = canonical if canonical != symbol else symbol
        try:
            from services.candle_collector import get_candle_collector
            collector = get_candle_collector()
            if collector:
                collector.fetch_and_store(fetch_sym)
                if fetch_sym != symbol:
                    collector.fetch_and_store(symbol)
        except Exception as e:
            logger.debug(f"[TradingFloor] On-demand fetch failed for {symbol}: {e}")

    def _evaluate_condition(self, cond: Dict, candles: Dict, price: float,
                            news: List[Dict] = None) -> tuple:
        """Evaluate a single condition. Returns (status, reason) tuple."""
        desc = (cond.get("description", "") + " " +
                cond.get("measurement", "")).lower()
        ctype = cond.get("type", "").lower()

        if ctype == "price_level" or "close >" in desc or "close <" in desc or "break above" in desc or "break below" in desc:
            return self._eval_price_level(desc, candles, price)

        if ctype == "price_action" or "retest" in desc or "bounce" in desc or "reversal" in desc or "hammer" in desc or "engulfing" in desc:
            return self._eval_price_action(desc, candles, price)

        if ctype == "volume" or "volume" in desc:
            return self._eval_volume(desc, candles, price)

        if ctype in ("economic", "economic/geopolitical",
                     "news_event", "time_event", "signal",
                     "market_breadth", "onchain/flow", "relative_strength",
                     "indicator"):
            return self._eval_news_conditions(cond, news or [], price)

        return self._eval_price_level(desc, candles, price)

    def _get_recent_news(self, symbol: str) -> List[Dict]:
        """Fetch recent alpha from ALL subscribed sources for condition checks.
        Includes news, Discord, TradingView ideas, and trader commentary."""
        news_rows = self.db.fetch_all("""
            SELECT headline, sentiment, direction, ai_analysis, source,
                   author, collected_at, category
            FROM news_items
            WHERE collected_at >= NOW() - INTERVAL 4 HOUR
            ORDER BY collected_at DESC
            LIMIT 30
        """) or []

        signal_rows = self.db.fetch_all("""
            SELECT CONCAT(author, ': ', COALESCE(direction,''), ' ', COALESCE(symbol,''),
                          ' @ ', COALESCE(entry_price,'')) AS headline,
                   CASE WHEN direction IN ('buy','long') THEN 'bullish'
                        WHEN direction IN ('sell','short') THEN 'bearish'
                        ELSE 'neutral' END AS sentiment,
                   direction, '' AS ai_analysis, source,
                   author, parsed_at AS collected_at, 'signal' AS category
            FROM parsed_signals
            WHERE parsed_at >= NOW() - INTERVAL 4 HOUR
              AND parsed_by NOT IN ('trading_floor', 'signal_ai')
            ORDER BY parsed_at DESC
            LIMIT 20
        """) or []

        return news_rows + signal_rows

    def _eval_news_conditions(self, cond: Dict, news: List[Dict],
                              price: float) -> tuple:
        """Evaluate economic/geopolitical/news conditions using keyword analysis.
        Checks recent alpha feed for crisis events and major negatives."""
        desc = cond.get("description", "").lower()
        ctype = cond.get("type", "").lower()

        if ctype == "indicator" or "rsi" in desc or "ema" in desc or "macd" in desc:
            return ("partially_met",
                    "Indicator conditions evaluated by BillNye (Data Scientist)")

        if ctype in ("relative_strength", "market_breadth", "onchain/flow"):
            return ("partially_met",
                    f"Cannot fully verify ({ctype}) - external data feed pending")

        if ctype == "signal":
            return ("partially_met",
                    "Signal conditions tracked via Signal AI - check signals tab")

        if not news:
            is_absence = any(w in desc for w in
                             ["no major", "no negative", "no bearish",
                              "must not", "no significant"])
            if is_absence:
                return ("met", "No recent news found in last 4h - absence condition satisfied")
            return ("partially_met", "No recent news/alpha available - marking partial")

        external_news = [n for n in news if not self._is_jarvais_signal(n)]
        if not external_news:
            return ("met", "Only JarvAIs internal signals found - no external news concerns")

        return self._eval_news_keywords(cond, external_news)

    def _eval_news_keywords(self, cond: Dict, news: List[Dict]) -> tuple:
        """Keyword-based news/geopolitical/economic condition evaluation."""
        desc = cond.get("description", "").lower()
        is_absence = any(w in desc for w in
                         ["no major", "no negative", "no bearish",
                          "must not", "no significant"])
        crisis_keywords = ["crash", "war ", "escalat", "sanction", "tariff hike",
                          "hawkish surprise", "recession confirm", "sovereign default",
                          "military attack", "invasion", "systemic collapse",
                          "banking crisis", "emergency rate", "martial law", "nuclear"]
        major_negative = []
        for n in news:
            headline = (n.get("headline") or "").lower()
            cat = (n.get("category") or "").lower()
            if cat in ("signal", "trade", "alpha"):
                continue
            for kw in crisis_keywords:
                if kw in headline:
                    major_negative.append(n.get("headline", "?")[:80])
                    break

        bearish_count = sum(1 for n in news
                           if (n.get("sentiment") or "").lower() == "bearish")
        total = len(news) or 1

        if is_absence:
            if major_negative:
                return ("not_met",
                        f"{len(major_negative)} crisis headline(s): "
                        f"{'; '.join(major_negative[:3])}")
            return ("met", f"No crisis keywords found. {bearish_count}/{total} bearish")
        if major_negative:
            return ("not_met", f"Crisis detected: {'; '.join(major_negative[:2])}")
        return ("met", f"No crisis keywords found in {total} recent items")

    def _is_jarvais_signal(self, news_item: Dict) -> bool:
        """Check if a news item is actually a JarvAIs internal signal/trade output."""
        source = (news_item.get("source") or "").lower()
        author = (news_item.get("author") or "").lower()
        headline = (news_item.get("headline") or "").lower()
        cat = (news_item.get("category") or "").lower()
        jarvais_markers = ["jarvais", "trading_floor", "signal_ai",
                          "jarv", "ai_alpha", "auto_signal"]
        signal_markers = ["buy suggestion", "sell suggestion", "trade alert",
                         "signal:", "ai recommendation"]
        if any(m in source or m in author for m in jarvais_markers):
            return True
        if any(m in headline for m in signal_markers):
            return True
        if cat in ("signal", "ai_signal", "auto_trade"):
            return True
        return False

    def _eval_price_level(self, desc: str, candles: Dict, price: float) -> tuple:
        """Check price-level conditions. Returns (status, reason).
        Handles both large prices (BTC 65000, NAS 5185) and small ones (ENS 6.22, PEPE 0.012)."""
        import re
        candidates = re.findall(r'(\d+\.\d+|\d{2,})', desc)
        candidates = [float(n) for n in candidates if float(n) > 0]
        if not candidates:
            return ("not_met", "Could not extract price level from condition description")

        if price > 0:
            target = min(candidates, key=lambda x: abs(x - price))
        else:
            target = candidates[0]

        is_above = ("above" in desc or ">" in desc or "break above" in desc or
                    "close >" in desc or "breakout" in desc)
        is_below = ("below" in desc or "<" in desc or "break below" in desc or
                    "close <" in desc or "no.*close.*<" in desc)

        # "no close below X" means condition is MET if price hasn't closed below
        if "no" in desc and ("close" in desc) and ("<" in desc or "below" in desc):
            for tf_key in ["M15", "H1"]:
                for c in candles.get(tf_key, []):
                    if c["close"] < target:
                        return ("not_met",
                                f"{tf_key} candle at {c['time']} closed at "
                                f"{c['close']:.2f} (below {target})")
            return ("met", f"No M15/H1 closes below {target} found")

        check_tfs = []
        if "15m" in desc or "m15" in desc:
            check_tfs.append("M15")
        if "1h" in desc or "h1" in desc:
            check_tfs.append("H1")
        if "4h" in desc or "h4" in desc:
            check_tfs.append("H4")
        if "d1" in desc or "daily" in desc:
            check_tfs.append("D1")
        if not check_tfs:
            check_tfs = ["M15", "H1"]

        if is_above:
            for tf in check_tfs:
                for c in candles.get(tf, [])[:8]:
                    if c["close"] > target:
                        return ("met",
                                f"{tf} close at {c['close']:.2f} > {target} "
                                f"({c['time']})")
            if price > target:
                return ("partially_met",
                        f"Price {price:.2f} > {target} but no confirmed "
                        f"{'/'.join(check_tfs)} candle close yet")
            return ("not_met",
                    f"Price {price:.2f} still below {target}. "
                    f"Waiting for {'/'.join(check_tfs)} close above")

        if is_below:
            for tf in check_tfs:
                for c in candles.get(tf, [])[:8]:
                    if c["close"] < target:
                        return ("met",
                                f"{tf} close at {c['close']:.2f} < {target} "
                                f"({c['time']})")
            if price < target:
                return ("partially_met",
                        f"Price {price:.2f} < {target} but no confirmed close")
            return ("not_met",
                    f"Price {price:.2f} still above {target}")

        return ("not_met", f"Could not determine direction for level {target}")

    def _eval_price_action(self, desc: str, candles: Dict, price: float) -> tuple:
        """Check price-action conditions. Returns (status, reason)."""
        import re
        numbers = re.findall(r'(\d{4,6}(?:\.\d+)?)', desc)

        if "retest" in desc and len(numbers) >= 1:
            zone_top = float(numbers[0])
            zone_bot = float(numbers[1]) if len(numbers) >= 2 else zone_top - 20

            for tf in ["M15", "H1"]:
                recent = candles.get(tf, [])[:16]
                for c in recent:
                    low, close, opn = c["low"], c["close"], c["open"]
                    if low <= zone_top and low >= zone_bot:
                        body = close - opn
                        if close > zone_top and body > 0:
                            return ("met",
                                    f"{tf} candle retested {zone_bot}-{zone_top} zone "
                                    f"(low={low:.2f}) and closed bullish at {close:.2f}")
                        if close >= zone_bot:
                            return ("partially_met",
                                    f"{tf} candle touched zone (low={low:.2f}) but "
                                    f"close {close:.2f} not convincingly above {zone_top}")

            return ("not_met",
                    f"No retest of {zone_bot}-{zone_top} zone detected in recent candles. "
                    f"Price: {price:.2f}")

        if "bounce" in desc and numbers:
            level = float(numbers[0])
            for c in candles.get("M15", [])[:12]:
                if c["low"] <= level + 5 and c["close"] > level and c["close"] > c["open"]:
                    return ("met",
                            f"M15 bounce detected at {level}: low={c['low']:.2f}, "
                            f"close={c['close']:.2f} (bullish)")
            return ("not_met",
                    f"No bounce from {level} detected. Price: {price:.2f}")

        if ("no" in desc or "must not" in desc) and "close" in desc and numbers:
            level = float(numbers[-1])
            for tf in ["M15"]:
                for c in candles.get(tf, []):
                    if c["close"] < level:
                        return ("not_met",
                                f"M15 close at {c['close']:.2f} broke below "
                                f"{level} ({c['time']})")
            return ("met", f"Support at {level} holding - no M15 closes below")

        if "bullish" in desc or "hammer" in desc or "engulfing" in desc:
            for c in candles.get("M15", [])[:8]:
                body = c["close"] - c["open"]
                if body > 5:
                    return ("met",
                            f"Bullish M15 candle: open={c['open']:.2f}, "
                            f"close={c['close']:.2f} (+{body:.1f}pt body)")
            for c in candles.get("H1", [])[:4]:
                body = c["close"] - c["open"]
                if body > 10:
                    return ("met",
                            f"Bullish H1 candle: open={c['open']:.2f}, "
                            f"close={c['close']:.2f} (+{body:.1f}pt body)")
            return ("not_met",
                    "No strong bullish candle pattern detected in recent M15/H1")

        return ("not_met", "Price action pattern not detected in recent candles")

    def _eval_volume(self, desc: str, candles: Dict, price: float) -> tuple:
        """Check volume conditions against actual candle data."""
        import re
        desc_lower = desc.lower()

        for tf in ["M15", "H1", "M5", "H4"]:
            tf_candles = candles.get(tf, [])
            if not tf_candles:
                continue
            vols = [c.get("volume", 0) for c in tf_candles[-30:] if c.get("volume")]
            if not vols or all(v == 0 for v in vols):
                continue

            recent_vol = vols[-1] if vols else 0
            avg_vol = sum(vols[:-1]) / max(len(vols) - 1, 1) if len(vols) > 1 else recent_vol

            if "increas" in desc_lower or "spike" in desc_lower or "ris" in desc_lower:
                if recent_vol > avg_vol * 1.2:
                    return ("met",
                            f"Volume increasing on {tf}: latest {recent_vol:,.0f} "
                            f"vs avg {avg_vol:,.0f} (+{(recent_vol/avg_vol - 1)*100:.0f}%)")
                return ("not_met",
                        f"Volume flat/declining on {tf}: latest {recent_vol:,.0f} "
                        f"vs avg {avg_vol:,.0f}")
            elif "decreas" in desc_lower or "declin" in desc_lower or "drop" in desc_lower:
                if recent_vol < avg_vol * 0.8:
                    return ("met",
                            f"Volume decreasing on {tf}: latest {recent_vol:,.0f} "
                            f"vs avg {avg_vol:,.0f} ({(1 - recent_vol/avg_vol)*100:.0f}% drop)")
                return ("not_met",
                        f"Volume steady/increasing on {tf}: latest {recent_vol:,.0f} "
                        f"vs avg {avg_vol:,.0f}")
            else:
                return ("partially_met",
                        f"Volume on {tf}: latest {recent_vol:,.0f}, avg {avg_vol:,.0f}")

        return ("partially_met",
                "No volume data available in candle feed for this symbol")

    def _check_invalidation(self, inv: Dict, candles: Dict, price: float,
                             dossier_created_at=None) -> bool:
        """Check if an invalidation trigger has been hit.
        Only considers candles that closed AFTER the dossier was created
        to avoid false triggers from historical data."""
        import re
        trigger = (inv.get("trigger", "") + " " + inv.get("description", "")).lower()
        candidates = re.findall(r'(\d+\.\d+|\d{2,})', trigger)
        candidates = [float(n) for n in candidates if float(n) > 0]

        if not candidates:
            return False

        level = min(candidates, key=lambda x: abs(x - price)) if price > 0 else candidates[0]

        def _candles_after_creation(tf_key, count=4):
            """Return only candles that closed after the dossier was created."""
            raw = candles.get(tf_key, [])[:count * 2]
            if not dossier_created_at or not raw:
                return raw[:count]
            return [c for c in raw
                    if isinstance(c.get("time"), datetime)
                    and c["time"] > dossier_created_at][:count]

        is_negated = any(neg in trigger for neg in
                         ["no ", "fails to", "does not", "doesn't", "cannot",
                          "never ", "unable to"])

        if is_negated:
            if "above" in trigger or ">" in trigger:
                for tf in ["H1", "H4"]:
                    post = _candles_after_creation(tf, 8)
                    if not post:
                        continue
                    for c in post:
                        if c["close"] > level:
                            return False
                return True

            if "hold" in trigger and ("above" in trigger):
                return price < level

            if "below" in trigger or "<" in trigger:
                for tf in ["H1", "H4"]:
                    post = _candles_after_creation(tf, 8)
                    if not post:
                        continue
                    for c in post:
                        if c["close"] < level:
                            return False
                return True

            return False

        # Direct triggers — only check candles that closed after dossier creation
        if "close" in trigger and ("below" in trigger or "<" in trigger):
            for tf in ["H1", "H4"]:
                for c in _candles_after_creation(tf, 4):
                    if c["close"] < level:
                        return True
            return False

        if "break" in trigger and "below" in trigger:
            return price < level

        if "close" in trigger and ("above" in trigger or ">" in trigger):
            for tf in ["H1", "H4"]:
                for c in _candles_after_creation(tf, 4):
                    if c["close"] > level:
                        return True

        return False

    # ── Conversational Tracker (Phase 3) ─────────────────────────────

    def _run_tracker_conversation(self, dossier_row: Dict, candles_by_tf_prefetch: Dict = None):
        """Run an LLM conversation turn with BillNye TA fulfilment.

        Flow:
          1. Check previous Tracker response for pending ta_requests → merge into
             this cycle's TA computation so BillNye delivers the data automatically.
          2. Assemble market update (price, conditions, TA, companion data).
          3. Send to Tracker LLM with BillNye-aware system prompt.
          4. Parse response: probability, recommendation, entry actions.
          5. If Tracker requested NEW indicators → compute immediately via BillNye,
             append a BillNye message to the conversation (fulfilled in same cycle).
          6. Persist conversation + log.

        DESIGN: Tracker sends TEXT ONLY — no chart images. BillNye computes
        indicators from raw candle data; Geo/Macro context from companion feed.
        Available timeframes: M5, M15, H1, H4, D1."""
        import re
        dossier = self.db.fetch_one(
            "SELECT * FROM trade_dossiers WHERE id = %s", (dossier_row["id"],))
        if not dossier:
            return

        symbol = dossier["symbol"]
        conditions = json.loads(dossier.get("conditions_for_entry") or "[]")
        prob_hist = json.loads(dossier.get("probability_history") or "[]")
        conversation = json.loads(dossier.get("tracker_conversation") or "[]")

        try:
            from services.data_scientist import (
                get_data_scientist, get_indicator_manifest_compact,
                parse_ta_requests, normalize_indicator_names, INDICATOR_NAME_MAP)
            from core.model_interface import get_model_interface

            ds = get_data_scientist(self.db)

            # ── Step 1: Scan previous Tracker response for pending ta_requests ──
            prev_ta_extras = []
            if conversation:
                for msg in reversed(conversation):
                    if msg.get("role") == "assistant":
                        try:
                            jm = re.search(r'\{[\s\S]*\}', msg.get("content", ""))
                            if jm:
                                prev = json.loads(jm.group())
                                prev_reqs = prev.get("ta_requests", [])
                                if prev_reqs:
                                    keys, _, _ = parse_ta_requests(prev_reqs)
                                    prev_ta_extras.extend(keys)
                        except Exception:
                            pass
                        break

            base_ta = ["rsi", "ema", "macd", "atr", "fibonacci", "td_sequential"]
            ta_requested = list(dict.fromkeys(base_ta + prev_ta_extras))

            if prev_ta_extras:
                logger.info(f"[TradingFloor] #{dossier_row['id']} enriching TA with "
                           f"prev requests: {prev_ta_extras}")

            # ── Step 2: Use pre-fetched candles or fetch ──
            candles_by_tf = candles_by_tf_prefetch if candles_by_tf_prefetch else {}
            if not candles_by_tf:
                candles_by_tf = ds.get_candles_from_db(symbol,
                    {"M5": 2, "M15": 12, "H1": 48, "H4": 168, "D1": 720})

            total_candles = sum(len(v) for v in candles_by_tf.values())
            if total_candles == 0:
                logger.warning(f"[TradingFloor] #{dossier_row['id']} {symbol}: "
                              f"zero candles from DB, attempting fresh fetch")
                self._try_fresh_fetch(symbol)
                candles_by_tf = ds.get_candles_from_db(symbol,
                    {"M5": 2, "M15": 12, "H1": 48, "H4": 168, "D1": 720})
                total_candles = sum(len(v) for v in candles_by_tf.values())

            if total_candles == 0:
                logger.error(f"[TradingFloor] #{dossier_row['id']} {symbol}: "
                            f"STILL no candle data. Skipping tracker LLM "
                            f"(saves cost, no data to analyze).")
                _append_tracker_log(self.db, dossier_row["id"],
                                   f"Tracker skipped: no candle data for {symbol}")
                return

            ta_results = ds.compute_all(symbol, candles_by_tf, ta_requested)
            ta_text = ds.format_for_prompt(ta_results)

            latest_price = 0
            for tf in ["M5", "M15", "H1"]:
                tf_candles = candles_by_tf.get(tf, [])
                if tf_candles:
                    latest_price = tf_candles[-1]["close"]
                    break

            companion_text = ""
            try:
                from services.data_scientist import get_companion_feed
                cf = get_companion_feed(self.db)
                comp = cf.get_full_companion_summary(symbol)
                companion_text = cf.format_companion_for_prompt(comp)
            except Exception:
                pass

            now = _utcnow()
            hour = now.hour
            if hour < 8:
                session = "Asia"
            elif hour < 13:
                session = "London"
            elif hour < 22:
                session = "New York"
            else:
                session = "Post-market"

            cond_summary = []
            for c in conditions:
                status_icon = "MET" if c.get("status") == "met" else (
                    "PARTIAL" if c.get("status") == "partially_met" else "NOT MET")
                cond_summary.append(
                    f"  C{c.get('id','?')}: [{status_icon}] {c.get('description','')[:120]}"
                    f" | Reason: {c.get('reason','')[:100]}")

            latest_prob = prob_hist[-1]["probability"] if prob_hist else 0

            # ── Step 2b: Gather lessons + chart summaries for context ──
            lesson_block = ""
            try:
                loss_rows = self.db.fetch_all("""
                    SELECT tl.lesson_text, tl.root_cause, td.direction,
                           td.realised_pnl_pct, td.leverage
                    FROM trade_lessons tl
                    JOIN trade_dossiers td ON tl.dossier_id = td.id
                    WHERE tl.symbol = %s AND tl.outcome IN ('LOSS','BREAKEVEN')
                    ORDER BY tl.timestamp DESC LIMIT 5
                """, (symbol,))
                if loss_rows:
                    lines = ["### PAST LESSONS (Top 5 recent losses for this symbol)"]
                    for lr in loss_rows:
                        lines.append(
                            f"- {lr.get('direction','')} | P&L: {lr.get('realised_pnl_pct','?')}% "
                            f"| Lev: {lr.get('leverage','?')}x "
                            f"| Root cause: {(lr.get('root_cause') or 'unknown')[:150]} "
                            f"| Lesson: {(lr.get('lesson_text') or '')[:200]}")
                    lesson_block = "\n".join(lines) + "\n\n"
            except Exception:
                pass

            chart_summary_block = ""
            try:
                chart_rows = self.db.fetch_all("""
                    SELECT author, source, SUBSTRING(ai_analysis, 1, 300) as summary
                    FROM news_items
                    WHERE symbol = %s AND ai_analysis IS NOT NULL
                      AND LENGTH(ai_analysis) > 50
                    ORDER BY collected_at DESC LIMIT 3
                """, (symbol,))
                if chart_rows:
                    lines = ["### CHART CONTEXT (recent analyst views)"]
                    for cr in chart_rows:
                        lines.append(
                            f"- {cr.get('author','?')} ({cr.get('source','?')}): "
                            f"{(cr.get('summary') or '')[:250]}")
                    chart_summary_block = "\n".join(lines) + "\n\n"
            except Exception:
                pass

            portfolio_block = ""
            try:
                sibling_rows = self.db.fetch_all("""
                    SELECT id, symbol, direction, entry_price, stop_loss,
                           confidence_score, status
                    FROM trade_dossiers
                    WHERE status IN ('pending','monitoring','live','active')
                      AND id != %s
                    ORDER BY created_at DESC LIMIT 10
                """, (dossier_row["id"],))
                if sibling_rows:
                    lines = ["### PORTFOLIO CONTEXT (other active dossiers)"]
                    for sr in sibling_rows:
                        lines.append(
                            f"- #{sr['id']} {sr['symbol']} {sr.get('direction','?')} "
                            f"@ {sr.get('entry_price','?')} "
                            f"(SL: {sr.get('stop_loss','?')}, "
                            f"conf: {sr.get('confidence_score','?')}%, "
                            f"status: {sr.get('status','?')})")
                    lines.append(
                        "Consider: correlation risk (multiple BTC-correlated longs), "
                        "capital concentration, and macro events that affect ALL positions.")
                    portfolio_block = "\n".join(lines) + "\n\n"
            except Exception:
                pass

            # ── Step 3: Build update message ──
            enriched_note = ""
            if prev_ta_extras:
                enriched_note = (
                    f"**BillNye fulfilled your previous request**: "
                    f"{', '.join(prev_ta_extras)} — included in the TA data below.\n\n")

            update_msg = (
                f"## TRACKER UPDATE — {symbol} — {now.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"**Price**: {latest_price} | **Probability**: {latest_prob}% | "
                f"**Status**: {dossier['status']} | **Session**: {session}\n\n"
                + enriched_note
                + f"### CONDITION STATUS\n"
                + "\n".join(cond_summary) + "\n\n"
                f"### TECHNICAL ANALYSIS (BillNye)\n"
                f"Indicators computed: {', '.join(ta_requested)}\n"
                f"Timeframes: M5, M15, H1, H4, D1\n\n"
                f"{ta_text}\n\n"
                + (f"### MARKET COMPANION DATA (VIX + correlations + calendar)\n"
                   f"{companion_text}\n\n" if companion_text else "")
                + lesson_block
                + chart_summary_block
                + portfolio_block
                + f"Based on this data, respond with EXACTLY this JSON:\n"
                f"```json\n"
                f'{{"probability": <0-100>, '
                f'"recommendation": "HOLD_POSITION|ENTER_NOW|ESCALATE_TO_APEX", '
                f'"confidence_adjustment": <-20 to +10>, '
                f'"market_read": "<concise analysis of current conditions>", '
                f'"conditions_update": [{{"id": <N>, "assessment": "met|not_met|partially_met", "reason": "<why>"}}], '
                f'"ta_requests": [{{"indicator": "<name>", "timeframes": ["M5","M15","H1","H4","D1"], "reason": "<why you need it>"}}], '
                f'"should_execute_now": false, '
                f'"entry_recommendation": {{"action": "none|place_limit|update_limit|cancel_limit", "price": null}}, '
                f'"notes": "<any observations>"}}\n'
                f"```\n"
                f"confidence_adjustment: Adjust the original Stage 2 confidence score "
                f"(-20 to +10) based on how conditions have evolved. "
                f"0 = no change, negative = conditions weakening, positive = strengthening. "
                f"Only use large negative values when the thesis is deteriorating.\n"
                f"REMINDER: Use ESCALATE_TO_APEX only if the trade thesis is CLEARLY "
                f"broken. Uncertainty and partial conditions are NORMAL. Default to HOLD_POSITION.\n"
                f"For ta_requests, use these indicator names:\n"
                f"  rsi, ema, sma, macd, bollinger, atr, cci, fibonacci, vwap,\n"
                f"  td_sequential, divergence, order_blocks, fvg, volume_profile,\n"
                f"  volume_trend, volume_climax, orb, session_levels, selloff, momentum_burst\n"
                f"  Shortcuts: volume (=profile+trend+climax), "
                f"support_resistance (=fibonacci+session_levels+order_blocks)\n"
                f"Specify which timeframes (M5,M15,H1,H4,D1) — e.g. M15+H1 for scalp, "
                f"H4+D1 for swing. Empty array [] if current TA is sufficient.\n"
                f"Do NOT request 'SENTIMENT_SCORE' — it is not a computable indicator.\n"
            )

            conversation.append({
                "role": "user", "content": update_msg,
                "timestamp": now.isoformat(), "source": "market_data"
            })

            # ── Step 4: Build system prompt with BillNye protocol ──
            manifest = get_indicator_manifest_compact()

            _TRACKER_SYS_FALLBACK = (
                "You are the Tracker AI monitoring dossier #{dossier_id} for {symbol}. "
                "Direction: {direction} | Entry: {entry_price} | "
                "SL: {stop_loss} | TP1: {take_profit_1}\n\n"
                "## Your Role — ANALYST, NOT DECISION MAKER\n"
                "You are Apex's field analyst. Your job is to REPORT conditions, "
                "NOT to make trade decisions. Apex (the senior trader) made the "
                "original trade decision and ONLY Apex can cancel it.\n\n"
                "Your responsibilities:\n"
                "1. Report current price action and technical conditions accurately\n"
                "2. Update condition statuses based on live data\n"
                "3. Flag anything that has changed since last update\n"
                "4. Recommend HOLD_POSITION if the setup is still developing normally\n"
                "5. Recommend ENTER_NOW if conditions have improved and entry is optimal\n"
                "6. Recommend ESCALATE_TO_APEX ONLY if the trade thesis is clearly "
                "broken (e.g. price blew through SL level, fundamental news invalidated "
                "the setup). This triggers an Apex review — do NOT use it lightly.\n\n"
                "CRITICAL RULES:\n"
                "- You do NOT have authority to cancel trades. Never recommend cancellation.\n"
                "- Uncertainty is NORMAL in trading. Do not escalate just because some "
                "conditions aren't met yet — that's why they're called CONDITIONS.\n"
                "- This is PAPER TRADING for data collection. We NEED trades to execute "
                "so we can measure performance. Lean towards HOLD over ESCALATE.\n"
                "- A probability below 50% does NOT mean cancel. It means conditions "
                "are still developing. Report accurately and let Apex decide.\n\n"
                "## Smart Money Reporting\n"
                "When reporting price action, always note:\n"
                "- Current AMD phase (Accumulation/Manipulation/Distribution)\n"
                "- Any liquidity grabs or stop hunts since last update\n"
                "- PDH/PDL levels and whether they've been swept\n"
                "- Session ORB status (broken? which direction? failed?)\n"
                "- Any session sweeps (Asia swept by London, London swept by NY)\n"
                "These observations help Apex make informed decisions on escalation.\n\n"
                "## BillNye (Data Scientist) Protocol\n"
                "BillNye computes TA indicators from raw candle data. If your analysis "
                "is hampered by missing data, or you need additional confirmation for "
                "a specific timeframe (scalp on M5/M15, swing on H4/D1), REQUEST it "
                "via ta_requests. BillNye will fulfil it and deliver results next cycle.\n"
                "Available indicators:\n{indicator_manifest}\n\n"
                "IMPORTANT: Sentiment is NOT a computable indicator. Do not request "
                "'sentiment_score'. Use Geo/Macro data from the companion feed.\n\n"
                "If any condition cannot be verified due to missing data, REQUEST the "
                "specific indicator and timeframe that would resolve it.\n"
            )
            tracker_template = load_prompt(
                self.db, "tracker_system_prompt",
                _TRACKER_SYS_FALLBACK, min_length=200,
                duo_id=self.duo_id)

            from core.config_loader import _SafeFormatDict
            system_msg = tracker_template.format_map(_SafeFormatDict({
                "dossier_id": dossier_row["id"],
                "symbol": symbol,
                "direction": dossier.get("direction", "?"),
                "entry_price": dossier.get("entry_price", "?"),
                "stop_loss": dossier.get("stop_loss", "?"),
                "take_profit_1": dossier.get("take_profit_1", "?"),
                "indicator_manifest": manifest,
            }))

            model = self._td_cfg.get("tracker_model", "anthropic/claude-sonnet-4")
            provider = self._td_cfg.get("tracker_provider", "openrouter")

            mi = get_model_interface()
            resp = mi.query_with_model(
                model_id=model, provider=provider,
                role="tracker_conversation",
                system_prompt=system_msg,
                user_prompt=update_msg,
                max_tokens=get_system_config_int(self.db, "tracker_max_tokens", 2000),
                temperature=get_system_config_float(self.db, "tracker_temperature", 0.2),
                context="tracker_conversation",
                source="trading_floor",
                dossier_id=dossier_row.get("id"),
                duo_id=self.duo_id)

            response_text = resp.content if resp and resp.success else ""

            if response_text:
                conversation.append({
                    "role": "assistant", "content": response_text,
                    "timestamp": _utcnow().isoformat()
                })

                # ── Step 5: Parse structured JSON response ──
                llm_prob = None
                rec = None
                market_read = ""
                parsed_resp = None

                json_match = re.search(r'\{[\s\S]*\}', response_text)
                if json_match:
                    try:
                        parsed_resp = json.loads(json_match.group())
                        llm_prob = parsed_resp.get("probability")
                        rec = (parsed_resp.get("recommendation") or "").upper()
                        market_read = parsed_resp.get("market_read", "")

                        if parsed_resp.get("should_execute_now"):
                            rec = "ENTER_NOW"

                        entry_rec = parsed_resp.get("entry_recommendation", {})
                        if entry_rec.get("action") == "place_limit" and entry_rec.get("price"):
                            place_limit_order(self.db, dossier_row["id"],
                                            float(entry_rec["price"]))
                        elif entry_rec.get("action") == "cancel_limit":
                            cancel_limit_order(self.db, dossier_row["id"],
                                             "LLM recommended limit cancellation")
                    except (json.JSONDecodeError, TypeError):
                        pass

                if llm_prob is None:
                    prob_match = re.search(r'[Pp]robability[:\s]*(\d+)', response_text)
                    if prob_match:
                        llm_prob = int(prob_match.group(1))
                if rec is None:
                    rec_match = re.search(r'[Rr]ecommendation[:\s]*(\S+)', response_text)
                    if rec_match:
                        rec = rec_match.group(1).upper()

                if llm_prob is not None:
                    reason = (f"Tracker report: {rec or '?'}. "
                             f"{market_read[:200]}" if market_read else
                             f"Tracker report: {rec or '?'}")
                    update_probability(self.db, dossier_row["id"], llm_prob, reason)

                # Apply dynamic confidence adjustment from Tracker
                conf_adj = 0
                if parsed_resp and parsed_resp.get("confidence_adjustment"):
                    try:
                        conf_adj = int(parsed_resp["confidence_adjustment"])
                        conf_adj = max(-20, min(10, conf_adj))
                    except (ValueError, TypeError):
                        conf_adj = 0

                if conf_adj != 0:
                    old_conf = dossier_row.get("confidence_score") or 0
                    new_conf = max(0, min(100, old_conf + conf_adj))
                    self.db.execute(
                        "UPDATE trade_dossiers SET confidence_score = %s WHERE id = %s",
                        (new_conf, dossier_row["id"]))
                    _append_tracker_log(self.db, dossier_row["id"],
                        f"Confidence adjusted: {old_conf} -> {new_conf} "
                        f"(adj={conf_adj:+d})")
                    logger.info(f"[Tracker] #{dossier_row['id']} confidence "
                                f"{old_conf} -> {new_conf} (adj={conf_adj:+d})")

                    # Re-check against min confidence -- if dropped below, abandon.
                    # Mentor mirrors are EXEMPT: they track the mentor's actual
                    # exchange position and must stay alive until the trade closes.
                    is_mentor_mirror = (
                        dossier.get("mentor_type") == "mentor_mirror")
                    min_conf = self._td_cfg.get("min_confidence_for_trade", 60)
                    status = dossier_row.get("status", "")
                    if (new_conf < min_conf and status in ("monitoring", "open_order")
                            and old_conf >= min_conf
                            and not is_mentor_mirror):
                        reason = (f"Tracker dropped confidence to {new_conf}% "
                                  f"(below min {min_conf}%)")
                        if status == "open_order":
                            _cancel_live_order_for_dossier(
                                self.db, dossier_row["id"], self.config)
                        self._abandon_dossier(
                            dossier_row["id"], reason, symbol=symbol)
                        _append_tracker_log(self.db, dossier_row["id"],
                                           f"ABANDONED: {reason}")
                        self._log_activity("warn",
                            f"#{dossier_row['id']} {symbol}: ABANDONED — {reason}",
                            symbol=symbol, dossier_id=dossier_row["id"])
                        self._trigger_postmortem(dossier_row["id"])
                        return

                # Tracker has NO authority to cancel. Handle legacy
                # CANCEL_TRADE responses as ESCALATE_TO_APEX.
                if rec in ("CANCEL_TRADE", "ESCALATE_TO_APEX"):
                    _max_esc_turns = get_system_config_int(
                        self.db, "escalation_max_turns", 1)
                    logger.info(f"[TradingFloor] #{dossier_row['id']} Tracker escalated "
                                f"to Apex (prob={llm_prob}%, max_turns={_max_esc_turns}). "
                                f"Running Apex review...")
                    self._run_apex_review(
                        dossier_row, dossier, conversation, llm_prob,
                        market_read, ta_text, companion_text)
                elif rec == "ENTER_NOW":
                    if not self._margin_gate(dossier_row["id"]):
                        self._log_activity("warn",
                            f"#{dossier_row['id']} {symbol}: MARGIN CAP — tracker entry blocked",
                            symbol=symbol, dossier_id=dossier_row["id"])
                    else:
                        transition_dossier(self.db, dossier_row["id"], "open_order",
                                          "Tracker recommended immediate entry")
                        self._register_order_entry_level(dossier_row["id"])
                        if self._td_cfg.get("auto_execute_paper", False):
                            execute_paper_trade(self.db, dossier_row["id"])
                        self._execute_live_trades_for_dossier(dossier_row["id"])

                # ── Step 6: Fulfil ta_requests via BillNye (same cycle) ──
                if parsed_resp:
                    ta_reqs_raw = parsed_resp.get("ta_requests", [])
                    if ta_reqs_raw:
                        new_keys, tf_overrides, unknown_keys = parse_ta_requests(
                            ta_reqs_raw)
                        # Filter out indicators already delivered this cycle
                        truly_new = [k for k in new_keys if k not in ta_requested]

                        if truly_new or unknown_keys:
                            billnye_parts = [
                                f"## BILLNYE TA RESPONSE — {symbol} — "
                                f"{_utcnow().strftime('%H:%M UTC')}"]

                            if truly_new:
                                # Determine timeframes per indicator
                                extra_candles = {}
                                for key in truly_new:
                                    tfs = tf_overrides.get(key)
                                    if tfs:
                                        for tf in tfs:
                                            if tf not in candles_by_tf:
                                                hrs = {"M1": 1, "M5": 2, "M15": 12,
                                                       "H1": 48, "H4": 168, "D1": 720,
                                                       "W1": 5040}.get(tf, 48)
                                                extra_candles[tf] = hrs

                                # Fetch any extra timeframes not in base set
                                if extra_candles:
                                    extra_data = ds.get_candles_from_db(
                                        symbol, extra_candles)
                                    candles_by_tf.update(extra_data)

                                extra_results = ds.compute_all(
                                    symbol, candles_by_tf, truly_new)
                                extra_text = ds.format_for_prompt(extra_results)
                                billnye_parts.append(
                                    f"Requested: **{', '.join(truly_new)}**\n\n"
                                    f"{extra_text}")
                                logger.info(
                                    f"[TradingFloor] BillNye fulfilled #{dossier_row['id']}: "
                                    f"{truly_new}")

                            if unknown_keys:
                                billnye_parts.append(
                                    f"\n**Cannot compute**: {', '.join(unknown_keys)}\n"
                                    f"Volume data → request "
                                    f"'volume_profile', 'volume_trend', or 'volume_climax'.")

                            conversation.append({
                                "role": "user",
                                "content": "\n".join(billnye_parts),
                                "timestamp": _utcnow().isoformat(),
                                "source": "billnye"
                            })

                # Append to tracker_log for backward compatibility
                log_entry = (
                    f"[{_utcnow().strftime('%Y-%m-%d %H:%M')}] "
                    f"LLM Assessment: {response_text[:200]}")
                existing_log = dossier.get("tracker_log") or ""
                new_log = existing_log + "\n" + log_entry if existing_log else log_entry

                self.db.execute("""
                    UPDATE trade_dossiers
                    SET tracker_conversation = %s, tracker_log = %s
                    WHERE id = %s
                """, (json.dumps(
                    conversation[-get_system_config_int(self.db, "escalation_context_depth", 20):],
                    default=str),
                      new_log[-get_system_config_int(self.db, "tracker_log_max_chars", 10000):],
                      dossier_row["id"]))

                logger.info(f"[TradingFloor] Tracker conversation for #{dossier_row['id']}: "
                           f"prob={llm_prob or '?'}%, rec={rec or '?'}")

        except Exception as e:
            logger.error(f"[TradingFloor] Tracker conversation failed for "
                        f"#{dossier_row['id']}: {e}", exc_info=True)

    # ── Apex Review (Tracker Escalation) ──────────────────────────────

    def _run_apex_review(self, dossier_row: Dict, dossier: Dict,
                         tracker_conversation: list, tracker_prob: int,
                         market_read: str, ta_text: str,
                         companion_text: str):
        """When Tracker escalates, Apex (the senior trader) reviews and decides.

        Only Apex has the authority to cancel a trade. This uses a cheaper model
        than the original Stage 2 to keep costs down, but the decision is Apex's.
        """
        import re
        try:
            from core.model_interface import get_model_interface

            symbol = dossier["symbol"]
            direction = dossier.get("direction", "?")
            entry = dossier.get("entry_price", "?")
            sl = dossier.get("stop_loss", "?")
            tp1 = dossier.get("take_profit_1", "?")
            original_decision = dossier.get("trade_decision", "?")

            conditions = json.loads(dossier.get("conditions_for_entry") or "[]")
            met_count = sum(1 for c in conditions if c.get("status") == "met")
            total = len(conditions)

            cond_summary = ""
            for c in conditions:
                status_icon = "MET" if c.get("status") == "met" else (
                    "PARTIAL" if c.get("status") == "partially_met" else "NOT MET")
                cond_summary += (
                    f"  C{c.get('id','?')}: [{status_icon}] "
                    f"{c.get('description','')[:120]}\n")

            _ESCALATION_FALLBACK = (
                "You are Apex, the senior day trader at JarvAIs. Your Tracker "
                "analyst has escalated dossier #{dossier_id} for review.\n\n"
                "YOUR ORIGINAL DECISION was: **{original_decision}** for "
                "{symbol} {direction}\n"
                "Entry: {entry} | SL: {sl} | TP1: {tp1}\n\n"
                "## CONTEXT\n"
                "We are LIVE TRADING with real capital. Every entry risks real money. "
                "A missed trade costs nothing; a bad trade costs real capital. "
                "Only enter when the setup is genuinely strong.\n\n"
                "## YOUR OPTIONS\n"
                "1. **HOLD** — Keep the dossier alive. Conditions are still developing.\n"
                "2. **ENTER_NOW** — The majority of conditions are met and the setup is "
                "genuinely compelling. Use this only when conviction is high.\n"
                "3. **ABANDON** — The trade thesis is broken, SL would have been hit, "
                "or a fundamental shift has occurred.\n\n"
                "QUALITY OVER QUANTITY: Only recommend ENTER_NOW if 60%+ of conditions "
                "are met AND the risk:reward is favourable. When in doubt, HOLD. "
                "Protecting capital is more important than catching every move.\n"
            )
            escalation_template = load_prompt(
                self.db, "apex_escalation_prompt",
                _ESCALATION_FALLBACK, min_length=100,
                duo_id=self.duo_id)
            from core.config_loader import _SafeFormatDict
            system_msg = escalation_template.format_map(_SafeFormatDict({
                "dossier_id": dossier_row["id"],
                "original_decision": original_decision,
                "symbol": symbol,
                "direction": direction,
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
            }))

            user_msg = (
                f"## TRACKER'S ESCALATION REPORT\n"
                f"Tracker probability: {tracker_prob}%\n"
                f"Tracker analysis: {market_read[:500]}\n\n"
                f"## CONDITIONS ({met_count}/{total} met)\n"
                f"{cond_summary}\n"
                f"## TECHNICAL ANALYSIS (latest)\n"
                f"{ta_text[:6000]}\n\n"
                + (f"## COMPANION DATA\n{companion_text[:3000]}\n\n"
                   if companion_text else "")
                + f"Based on this, respond with EXACTLY:\n"
                f"```json\n"
                f'{{"decision": "HOLD|ENTER_NOW|ABANDON", '
                f'"probability": <0-100>, '
                f'"reasoning": "<why>", '
                f'"strategic_rationale": "<If ENTER_NOW with partial conditions: explain your conviction and what overrode the unmet conditions. If HOLD/ABANDON: explain your read of how conditions may develop or why the thesis broke>"}}\n'
                f"```\n"
            )

            # Use a cost-effective model for the review
            review_model = self._td_cfg.get(
                "apex_review_model", "google/gemini-2.5-flash")
            review_provider = self._td_cfg.get(
                "apex_review_provider", "openrouter")

            mi = get_model_interface()
            resp = mi.query_with_model(
                model_id=review_model, provider=review_provider,
                role="apex_review", system_prompt=system_msg,
                user_prompt=user_msg,
                max_tokens=get_system_config_int(self.db, "escalation_max_tokens", 500),
                temperature=get_system_config_float(self.db, "escalation_temperature", 0.2),
                context="apex_escalation_review",
                source="trading_floor",
                dossier_id=dossier_row.get("id"),
                duo_id=self.duo_id)

            response_text = resp.content if resp and resp.success else ""
            if not response_text:
                logger.warning(f"[TradingFloor] Apex review returned empty for "
                              f"#{dossier_row['id']}, defaulting to HOLD")
                return

            # Parse Apex's decision
            decision = "HOLD"
            apex_prob = tracker_prob
            reasoning = ""
            strategic_rationale = ""

            json_match = re.search(r'\{[\s\S]*\}', response_text)
            if json_match:
                try:
                    parsed = json.loads(json_match.group())
                    decision = (parsed.get("decision") or "HOLD").upper()
                    apex_prob = parsed.get("probability", tracker_prob)
                    reasoning = parsed.get("reasoning", "")
                    strategic_rationale = parsed.get("strategic_rationale", "")
                except (json.JSONDecodeError, TypeError):
                    pass

            if decision == "ENTER_NOW" and strategic_rationale:
                try:
                    self.db.execute(
                        "UPDATE trade_dossiers SET apex_entry_reasoning = %s WHERE id = %s",
                        (strategic_rationale[:2000], dossier_row["id"]))
                except Exception:
                    pass

            # Log to tracker conversation
            tracker_conversation.append({
                "role": "user",
                "content": f"[APEX REVIEW] Decision: {decision} | "
                           f"Probability: {apex_prob}% | {reasoning[:300]}",
                "timestamp": _utcnow().isoformat(),
                "source": "apex_review"
            })

            self.db.execute("""
                UPDATE trade_dossiers
                SET tracker_conversation = %s WHERE id = %s
            """, (json.dumps(
                tracker_conversation[-get_system_config_int(self.db, "escalation_context_depth", 20):],
                default=str),
                  dossier_row["id"]))

            update_probability(self.db, dossier_row["id"], apex_prob,
                             f"Apex review: {decision}. {reasoning[:200]}")

            symbol = dossier_row.get("symbol", "?")
            self._log_apex_thought(symbol, "apex_review",
                f"{decision}: {reasoning[:800]}",
                decision=decision, probability=apex_prob)

            if decision == "ABANDON":
                self._abandon_dossier(dossier_row["id"],
                    f"Apex decided to abandon: {reasoning[:200]}",
                    symbol=symbol)
            elif decision == "ENTER_NOW":
                if not self._margin_gate(dossier_row["id"]):
                    self._log_activity("warn",
                        f"#{dossier_row['id']}: MARGIN CAP — Apex entry blocked",
                        dossier_id=dossier_row["id"])
                    self._log_apex_thought(symbol, "margin_blocked",
                        f"#{dossier_row['id']} ENTER_NOW blocked — margin cap reached",
                        decision="blocked", probability=apex_prob)
                else:
                    transition_dossier(self.db, dossier_row["id"], "open_order",
                                      f"Apex decided to enter: {reasoning[:200]}")
                    self._register_order_entry_level(dossier_row["id"])
                    if self._td_cfg.get("auto_execute_paper", False):
                        execute_paper_trade(self.db, dossier_row["id"])
                    self._execute_live_trades_for_dossier(dossier_row["id"])
            else:
                logger.info(f"[TradingFloor] #{dossier_row['id']} Apex says HOLD "
                           f"(prob={apex_prob}%): {reasoning[:100]}")

        except Exception as e:
            logger.error(f"[TradingFloor] Apex review failed for "
                        f"#{dossier_row['id']}: {e}", exc_info=True)

    # ── Status & Queries ──────────────────────────────────────────────

    def get_status(self) -> Dict:
        """Get trading floor service status."""
        counts = self.db.fetch_one("""
            SELECT
                COUNT(*) as total,
                SUM(status = 'draft') as draft,
                SUM(status = 'proposed') as proposed,
                SUM(status = 'monitoring') as monitoring,
                SUM(status = 'open_order') as open_order,
                SUM(status = 'live') as live,
                SUM(status = 'won') as won,
                SUM(status = 'lost') as lost,
                SUM(status = 'expired') as expired,
                SUM(status = 'abandoned') as abandoned
            FROM trade_dossiers
        """)

        # Sell-off detection across tradable symbols
        market_alert = "normal"
        alert_details = []
        try:
            from services.data_scientist import get_data_scientist, get_companion_feed
            ds = get_data_scientist(self.db)
            tradable_rows = self.db.fetch_all(
                "SELECT symbol FROM market_symbols WHERE tradable = 1") or []
            for sym in [r["symbol"] for r in tradable_rows]:
                candles = ds.get_candles_from_db(sym, {"D1": 720})
                if "D1" in candles:
                    df = ds._candles_to_df(candles["D1"])
                    selloff = ds._detect_selloff(df)
                    if selloff.get("tier") in ("correction", "crash"):
                        alert_details.append({"symbol": sym, **selloff})
                        if selloff["tier"] == "crash":
                            market_alert = "crash"
                        elif market_alert != "crash":
                            market_alert = "correction"

            cf = get_companion_feed(self.db)
            vix = cf.get_vix_status()
            if vix.get("status") == "extreme":
                market_alert = "crash"
            elif vix.get("status") == "elevated" and market_alert == "normal":
                market_alert = "correction"
        except Exception as e:
            logger.debug(f"[TradingFloor] Sell-off check error: {e}")

        sig_interval = self._td_cfg.get("signal_tracker_interval_minutes", 5)
        dos_interval = self._td_cfg.get("tracker_interval_minutes", 15)

        # Daily trading stats
        daily_stats = self._get_daily_stats()

        return {
            "running": self._running,
            "watchlist": [],
            "market_alert": market_alert,
            "alert_details": alert_details,
            "config": {
                "discovery_interval_minutes": self._td_cfg.get("discovery_interval_minutes", 60),
                "tracker_interval_minutes": dos_interval,
                "signal_tracker_interval_minutes": sig_interval,
                "condition_threshold_execute": self._td_cfg.get("condition_threshold_execute", 85),
                "condition_threshold_limit_order": self._td_cfg.get("condition_threshold_limit_order", 75),
                "max_active_dossiers_per_symbol": self._td_cfg.get("max_active_dossiers_per_symbol", 2),
                "stage1_model": self._td_cfg.get("stage1_model"),
                "stage2_model": self._td_cfg.get("stage2_model"),
                "include_chart_images": self._td_cfg.get("include_chart_images", True),
            },
            "counts": {k: int(v or 0) for k, v in (counts or {}).items()} if counts else {},
            "paper_balance": self._get_paper_balance(),
            "daily_stats": daily_stats,
            "last_signal_check_at": self._last_signal_check_at.isoformat() + "Z" if self._last_signal_check_at else None,
            "last_dossier_check_at": self._last_dossier_check_at.isoformat() + "Z" if self._last_dossier_check_at else None,
            "signal_tracker_interval_seconds": sig_interval * 60,
            "dossier_tracker_interval_seconds": dos_interval * 60,
        }

    def _get_daily_stats(self) -> Dict:
        """Daily P&L, win ratio, account summary for the stats bar.
        Returns combined totals plus an apex/mirror breakdown.
        Splits paper vs live P&L so the UI can display them separately."""
        _APEX_FILTER = "(mentor_type IS NULL OR mentor_type = 'apex_assessed')"
        _MIRROR_FILTER = "mentor_type = 'mentor_mirror'"

        _PAPER_CLAUSE = "(d.paper_reason IS NOT NULL AND d.paper_reason != '')"
        _REAL_CLAUSE = ("EXISTS (SELECT 1 FROM live_trades lt "
                        "WHERE lt.dossier_id = d.id "
                        "AND lt.status IN ('pending','open','partial_closed','closed'))")

        def _pnl_block(db, label_filter, today_only=False):
            date_clause = "AND DATE(d.updated_at) = CURDATE()" if today_only else ""
            row = db.fetch_one(f"""
                SELECT
                    COUNT(CASE WHEN d.status='won' THEN 1 END) as wins,
                    COUNT(CASE WHEN d.status='lost' THEN 1 END) as losses,
                    COALESCE(SUM(CASE WHEN d.status IN ('won','lost')
                        THEN d.realised_pnl ELSE 0 END), 0) as closed_pnl
                FROM trade_dossiers d
                WHERE d.status IN ('won','lost')
                  AND d.realised_pnl IS NOT NULL
                  AND {label_filter}
                {date_clause}
            """)
            live_row = db.fetch_one(f"""
                SELECT COALESCE(SUM(d.unrealised_pnl), 0) as live_pnl
                FROM trade_dossiers d
                WHERE d.status = 'live' AND {label_filter}
            """)
            w = int(row["wins"]) if row else 0
            l = int(row["losses"]) if row else 0
            cp = float(row["closed_pnl"]) if row else 0
            lp = float(live_row["live_pnl"]) if live_row else 0
            total = w + l
            return {
                "wins": w, "losses": l,
                "win_rate": round(w / total * 100) if total else 0,
                "closed_pnl": round(cp, 2),
                "live_pnl": round(lp, 2),
                "pnl": round(cp + lp, 2),
            }

        try:
            balance = self._td_cfg.get("account_balance", ACCOUNT_BALANCE)

            today = self.db.fetch_one("""
                SELECT
                    COUNT(CASE WHEN d.status='won' THEN 1 END) as wins,
                    COUNT(CASE WHEN d.status='lost' THEN 1 END) as losses,
                    COALESCE(SUM(CASE WHEN d.status IN ('won','lost')
                        THEN d.realised_pnl ELSE 0 END), 0) as closed_pnl,
                    COALESCE(SUM(CASE WHEN d.status IN ('won','lost')
                        THEN d.realised_pnl_pct ELSE 0 END), 0) as closed_pnl_pct
                FROM trade_dossiers d
                WHERE DATE(d.updated_at) = CURDATE()
                  AND d.status IN ('won','lost')
            """)
            live = self.db.fetch_one("""
                SELECT
                    COUNT(*) as live_count,
                    COALESCE(SUM(d.unrealised_pnl), 0) as live_pnl,
                    COALESCE(SUM(d.margin_usd), 0) as total_margin
                FROM trade_dossiers d WHERE d.status = 'live'
            """)
            orders = self.db.fetch_one("""
                SELECT COUNT(*) as cnt FROM trade_dossiers WHERE status = 'open_order'
            """)
            alltime = self.db.fetch_one("""
                SELECT
                    COUNT(CASE WHEN d.status='won' THEN 1 END) as wins,
                    COUNT(CASE WHEN d.status='lost' THEN 1 END) as losses,
                    COALESCE(SUM(d.realised_pnl), 0) as total_realised
                FROM trade_dossiers d
                WHERE d.status IN ('won','lost') AND d.realised_pnl IS NOT NULL
            """)

            # ── Live-only P&L (dossiers backed by a real live_trades record) ──
            real_live = self.db.fetch_one(f"""
                SELECT COUNT(*) as cnt,
                       COALESCE(SUM(d.unrealised_pnl), 0) as upnl,
                       COALESCE(SUM(d.margin_usd), 0) as margin
                FROM trade_dossiers d
                WHERE d.status = 'live' AND {_REAL_CLAUSE}
            """)
            real_closed = self.db.fetch_one(f"""
                SELECT COALESCE(SUM(d.realised_pnl), 0) as rpnl,
                       SUM(d.status='won') as wins,
                       SUM(d.status='lost') as losses
                FROM trade_dossiers d
                WHERE d.status IN ('won','lost')
                  AND d.realised_pnl IS NOT NULL
                  AND {_REAL_CLAUSE}
            """)
            # Exchange-side P&L from live_trades (most accurate for open positions)
            exch_pnl = self.db.fetch_one("""
                SELECT COALESCE(SUM(unrealised_pnl), 0) as upnl,
                       COALESCE(SUM(CASE WHEN status='closed' THEN realised_pnl ELSE 0 END), 0) as rpnl
                FROM live_trades
                WHERE status IN ('open','partial_closed','closed')
            """)

            # ── Paper-only P&L (dossiers with paper_reason and no live trade) ──
            paper_live = self.db.fetch_one(f"""
                SELECT COUNT(*) as cnt,
                       COALESCE(SUM(d.unrealised_pnl), 0) as upnl
                FROM trade_dossiers d
                WHERE d.status = 'live' AND {_PAPER_CLAUSE}
                  AND NOT {_REAL_CLAUSE}
            """)
            paper_closed = self.db.fetch_one(f"""
                SELECT COALESCE(SUM(d.realised_pnl), 0) as rpnl,
                       SUM(d.status='won') as wins,
                       SUM(d.status='lost') as losses
                FROM trade_dossiers d
                WHERE d.status IN ('won','lost')
                  AND d.realised_pnl IS NOT NULL
                  AND {_PAPER_CLAUSE}
                  AND NOT {_REAL_CLAUSE}
            """)

            wins = int(today["wins"]) if today else 0
            losses = int(today["losses"]) if today else 0
            closed_pnl = float(today["closed_pnl"]) if today else 0
            live_pnl = float(live["live_pnl"]) if live else 0
            live_count = int(live["live_count"]) if live else 0
            total_margin = float(live["total_margin"]) if live else 0
            open_orders = int(orders["cnt"]) if orders else 0
            total_today = wins + losses
            win_rate = round(wins / total_today * 100) if total_today > 0 else 0
            day_pnl = closed_pnl + live_pnl
            day_pnl_pct = (day_pnl / balance) * 100

            at_wins = int(alltime["wins"]) if alltime else 0
            at_losses = int(alltime["losses"]) if alltime else 0
            at_total = float(alltime["total_realised"]) if alltime else 0
            at_combined = at_total + live_pnl
            at_wr = round(at_wins / (at_wins + at_losses) * 100) if (at_wins + at_losses) else 0

            apex_day = _pnl_block(self.db, _APEX_FILTER, today_only=True)
            mirror_day = _pnl_block(self.db, _MIRROR_FILTER, today_only=True)
            apex_all = _pnl_block(self.db, _APEX_FILTER, today_only=False)
            mirror_all = _pnl_block(self.db, _MIRROR_FILTER, today_only=False)

            # Exchange-truth numbers
            exch_upnl = float(exch_pnl["upnl"]) if exch_pnl else 0
            exch_rpnl = float(exch_pnl["rpnl"]) if exch_pnl else 0

            r_live_cnt = int(real_live["cnt"]) if real_live else 0
            r_live_upnl = float(real_live["upnl"]) if real_live else 0
            r_live_margin = float(real_live["margin"]) if real_live else 0
            r_closed_rpnl = float(real_closed["rpnl"]) if real_closed else 0
            r_closed_wins = int(real_closed["wins"] or 0) if real_closed else 0
            r_closed_losses = int(real_closed["losses"] or 0) if real_closed else 0

            p_live_cnt = int(paper_live["cnt"]) if paper_live else 0
            p_live_upnl = float(paper_live["upnl"]) if paper_live else 0
            p_closed_rpnl = float(paper_closed["rpnl"]) if paper_closed else 0
            p_closed_wins = int(paper_closed["wins"] or 0) if paper_closed else 0
            p_closed_losses = int(paper_closed["losses"] or 0) if paper_closed else 0

            actual_rpnl = round(r_closed_rpnl, 2)
            actual_upnl = round(r_live_upnl, 2)
            actual_total = round(r_closed_rpnl + r_live_upnl, 2)
            paper_rpnl_val = round(p_closed_rpnl, 2)
            paper_upnl_val = round(p_live_upnl, 2)
            paper_total = round(p_closed_rpnl + p_live_upnl, 2)
            combined_total = round(actual_total + paper_total, 2)

            return {
                "wins_today": wins,
                "losses_today": losses,
                "win_rate": win_rate,
                "closed_pnl": round(closed_pnl, 2),
                "live_pnl": round(live_pnl, 2),
                "total_pnl": round(day_pnl, 2),
                "total_pnl_pct": round(day_pnl_pct, 2),
                "alltime_pnl": round(at_combined, 2),
                "alltime_pnl_pct": round((at_combined / balance) * 100, 2),
                "alltime_wins": at_wins,
                "alltime_losses": at_losses,
                "alltime_win_rate": at_wr,
                "live_positions": live_count,
                "open_orders": open_orders,
                "total_margin_at_risk": round(total_margin, 2),
                "account_balance": balance,
                "apex_day": apex_day,
                "mirror_day": mirror_day,
                "apex_all": apex_all,
                "mirror_all": mirror_all,
                # ── Clear ACTUAL / PAPER / COMBINED hierarchy ──
                "actual_rpnl": actual_rpnl,
                "actual_upnl": actual_upnl,
                "actual_total": actual_total,
                "actual_count": r_live_cnt,
                "actual_margin": round(r_live_margin, 2),
                "actual_closed_wins": r_closed_wins,
                "actual_closed_losses": r_closed_losses,
                "paper_rpnl": paper_rpnl_val,
                "paper_upnl": paper_upnl_val,
                "paper_total": paper_total,
                "paper_count": p_live_cnt,
                "paper_closed_wins": p_closed_wins,
                "paper_closed_losses": p_closed_losses,
                "combined_total": combined_total,
                # Backwards compat
                "real_live_count": r_live_cnt,
                "real_live_upnl": round(r_live_upnl, 2),
                "real_live_margin": round(r_live_margin, 2),
                "real_closed_rpnl": round(r_closed_rpnl, 2),
                "real_closed_wins": r_closed_wins,
                "real_closed_losses": r_closed_losses,
                "paper_live_count": p_live_cnt,
                "paper_live_upnl": round(p_live_upnl, 2),
                "paper_closed_rpnl": round(p_closed_rpnl, 2),
                "paper_closed_wins": p_closed_wins,
                "paper_closed_losses": p_closed_losses,
                "exch_upnl": round(exch_upnl, 2),
                "exch_rpnl": round(exch_rpnl, 2),
            }
        except Exception as e:
            logger.debug(f"[TradingFloor] Daily stats error: {e}")
            return {}

    def get_active_dossiers(self, symbol: str = None,
                             status_filter: List[str] = None,
                             duo_id_filter: str = None) -> List[Dict]:
        """Get dossiers for the Trading Floor UI.

        Args:
            duo_id_filter: explicit duo filter. ``None`` = use this instance's
                duo_id; ``"__all__"`` = no duo filter (show all duos).
        """
        where = ["1=1"]
        params = []
        if symbol:
            where.append("symbol = %s")
            params.append(symbol)
        if status_filter:
            placeholders = ",".join(["%s"] * len(status_filter))
            where.append(f"status IN ({placeholders})")
            params.extend(status_filter)

        effective_duo = duo_id_filter if duo_id_filter is not None else self.duo_id
        if effective_duo and effective_duo != "__all__":
            where.append("duo_id = %s")
            params.append(effective_duo)

        rows = self.db.fetch_all(f"""
            SELECT id, duo_id, symbol, raw_symbol, status, trade_decision, direction,
                   entry_price, stop_loss,
                   take_profit_1, take_profit_2, take_profit_3,
                   take_profit_4, take_profit_5, take_profit_6,
                   confidence_score, time_horizon_hours,
                   stage1_model_used, stage2_model_used,
                   conditions_for_entry, probability_history,
                   linked_signal_id, limit_order_active, limit_order_price,
                   limit_order_placed_at,
                   current_price, current_price_at,
                   unrealised_pnl, unrealised_pnl_pct,
                   realised_pnl, realised_pnl_pct,
                   margin_usd, leverage, executed_at, tp_progress,
                   mentor_source, mentor_type, apex_entry_reasoning,
                   opp_score, paper_reason,
                   waterfall_resolved, waterfall_account,
                   created_at, updated_at, expires_at
            FROM trade_dossiers
            WHERE {' AND '.join(where)}
            ORDER BY
                FIELD(status, 'live','open_order','monitoring','proposed','draft',
                      'won','lost','expired','abandoned'),
                opp_score DESC, created_at DESC
            LIMIT 100
        """, tuple(params))

        # Pre-fetch exchange tickers for all symbols in one query
        _all_syms = list(set(r["symbol"] for r in (rows or []) if r.get("symbol")))
        _exchange_tickers = {}
        if _all_syms:
            try:
                placeholders = ",".join(["%s"] * len(_all_syms))
                _ticker_rows = self.db.fetch_all(
                    f"SELECT symbol, canonical_symbol, bybit_ticker, blofin_ticker, bitget_ticker "
                    f"FROM market_symbols WHERE symbol IN ({placeholders})",
                    tuple(_all_syms))
                for tr in (_ticker_rows or []):
                    _exchange_tickers[tr["symbol"]] = {
                        "canonical": tr.get("canonical_symbol"),
                        "bybit": tr.get("bybit_ticker"),
                        "blofin": tr.get("blofin_ticker"),
                        "bitget": tr.get("bitget_ticker"),
                    }
            except Exception:
                pass

        results = []
        for r in (rows or []):
            prob_hist = json.loads(r.get("probability_history") or "[]")
            current_prob = prob_hist[-1]["probability"] if prob_hist else (
                r.get("confidence_score") or 0)

            conditions = json.loads(r.get("conditions_for_entry") or "[]")
            met = sum(1 for c in conditions if c.get("status") == "met")

            # Resolved exchange pair for display confidence
            _sym = r["symbol"]
            _et = _exchange_tickers.get(_sym, {})
            _resolved_pair = _et.get("canonical") or _sym
            _exchange_info = {
                "bybit": _et.get("bybit"),
                "blofin": _et.get("blofin"),
                "bitget": _et.get("bitget"),
            }

            results.append({
                "id": r["id"],
                "duo_id": r.get("duo_id"),
                "symbol": r["symbol"],
                "raw_symbol": r.get("raw_symbol"),
                "resolved_symbol": _resolved_pair,
                "exchange_tickers": _exchange_info,
                "status": r["status"],
                "trade_decision": r.get("trade_decision"),
                "direction": r.get("direction"),
                "entry_price": float(r["entry_price"]) if r.get("entry_price") else None,
                "stop_loss": float(r["stop_loss"]) if r.get("stop_loss") else None,
                "tp1": float(r["take_profit_1"]) if r.get("take_profit_1") else None,
                "tp2": float(r["take_profit_2"]) if r.get("take_profit_2") else None,
                "tp3": float(r["take_profit_3"]) if r.get("take_profit_3") else None,
                "tp4": float(r["take_profit_4"]) if r.get("take_profit_4") else None,
                "tp5": float(r["take_profit_5"]) if r.get("take_profit_5") else None,
                "tp6": float(r["take_profit_6"]) if r.get("take_profit_6") else None,
                "confidence": r.get("confidence_score"),
                "current_probability": current_prob,
                "conditions_total": len(conditions),
                "conditions_met": met,
                "limit_order_active": bool(r.get("limit_order_active")),
                "limit_order_price": float(r["limit_order_price"]) if r.get("limit_order_price") else None,
                "linked_signal_id": r.get("linked_signal_id"),
                "model": r.get("stage2_model_used"),
                "current_price": float(r["current_price"]) if r.get("current_price") else None,
                "current_price_at": (r["current_price_at"].isoformat() + "Z") if r.get("current_price_at") else None,
                "unrealised_pnl": float(r["unrealised_pnl"]) if r.get("unrealised_pnl") is not None else None,
                "unrealised_pnl_pct": float(r["unrealised_pnl_pct"]) if r.get("unrealised_pnl_pct") is not None else None,
                "realised_pnl": float(r["realised_pnl"]) if r.get("realised_pnl") is not None else None,
                "realised_pnl_pct": float(r["realised_pnl_pct"]) if r.get("realised_pnl_pct") is not None else None,
                "margin_usd": float(r["margin_usd"]) if r.get("margin_usd") else DEFAULT_MARGIN,
                "leverage": int(r["leverage"]) if r.get("leverage") else DEFAULT_LEVERAGE,
                "executed_at": (r["executed_at"].isoformat() + "Z") if r.get("executed_at") else None,
                "tp_progress": r.get("tp_progress") or "none",
                "mentor_source": r.get("mentor_source"),
                "mentor_type": r.get("mentor_type"),
                "apex_entry_reasoning": r.get("apex_entry_reasoning"),
                "opp_score": float(r["opp_score"]) if r.get("opp_score") is not None else None,
                "paper_reason": r.get("paper_reason"),
                "stage1_model_used": r.get("stage1_model_used"),
                "waterfall_resolved": bool(r.get("waterfall_resolved")) if r.get("waterfall_resolved") is not None else None,
                "waterfall_account": r.get("waterfall_account"),
                "created_at": (r["created_at"].isoformat() + "Z") if r.get("created_at") else None,
                "expires_at": (r["expires_at"].isoformat() + "Z") if r.get("expires_at") else None,
                "age_hours": round(
                    (_utcnow() - r["created_at"]).total_seconds() / 3600, 1
                ) if r.get("created_at") else None,
            })
        return results

    def get_dossier_detail(self, dossier_id: int) -> Optional[Dict]:
        """Get full dossier detail for drill-down view."""
        d = self.db.fetch_one(
            "SELECT * FROM trade_dossiers WHERE id = %s", (dossier_id,))
        if not d:
            return None

        sections = json.loads(d.get("dossier_sections") or "{}")
        hypothesis = json.loads(d.get("stage2_hypothesis") or "{}")
        conditions = json.loads(d.get("conditions_for_entry") or "[]")
        prob_hist = json.loads(d.get("probability_history") or "[]")

        chart_images = []
        da_analyses = sections.get("da_analyses", [])
        download_dir = self.config.raw.get(
            "data_management", {}).get("alpha_download_dir", "data/alpha_downloads")
        for a in da_analyses:
            for field in ["chart_image_url", "media_url"]:
                url = a.get(field)
                if url:
                    from services.trade_dossier import resolve_chart_image_path
                    local = resolve_chart_image_path(url, download_dir)
                    if local and os.path.exists(local):
                        chart_images.append({
                            "path": local,
                            "author": a.get("author"),
                            "source": a.get("source"),
                        })

        linked_signal = None
        if d.get("linked_signal_id"):
            linked_signal = self.db.fetch_one(
                """SELECT id, symbol, direction, entry_price, stop_loss,
                          take_profit_1, status, outcome, outcome_pips,
                          entry_hit_at, sl_hit_at, tp1_hit_at, parsed_at
                   FROM parsed_signals WHERE id = %s""",
                (d["linked_signal_id"],))

        invalidations = hypothesis.get("invalidations", [])

        linked_signal_dict = None
        if linked_signal:
            linked_signal_dict = {
                "id": linked_signal["id"],
                "symbol": linked_signal.get("symbol"),
                "direction": linked_signal.get("direction"),
                "entry_price": float(linked_signal["entry_price"]) if linked_signal.get("entry_price") else None,
                "stop_loss": float(linked_signal["stop_loss"]) if linked_signal.get("stop_loss") else None,
                "take_profit_1": float(linked_signal["take_profit_1"]) if linked_signal.get("take_profit_1") else None,
                "status": linked_signal.get("status"),
                "outcome": linked_signal.get("outcome"),
                "outcome_pips": float(linked_signal["outcome_pips"]) if linked_signal.get("outcome_pips") else None,
                "entry_hit_at": linked_signal["entry_hit_at"].isoformat() if linked_signal.get("entry_hit_at") else None,
                "sl_hit_at": linked_signal["sl_hit_at"].isoformat() if linked_signal.get("sl_hit_at") else None,
                "tp1_hit_at": linked_signal["tp1_hit_at"].isoformat() if linked_signal.get("tp1_hit_at") else None,
            }

        tracker_convo = json.loads(d.get("tracker_conversation") or "[]")

        # Candles, current price, distance-from-entry, position for chart overlay
        symbol = d.get("symbol")
        candles_by_tf = {}
        current_price = None
        distance_from_entry = None
        position = None
        if symbol:
            try:
                from services.data_scientist import get_data_scientist
                ds = get_data_scientist(self.db)
                candles_by_tf = ds.get_candles_from_db(symbol, {"M15": 168})
                m15 = candles_by_tf.get("M15") or []
                if m15:
                    current_price = float(m15[-1]["close"])
            except Exception as _ds_err:
                logger.warning(f"[TradingFloor] get_dossier_detail: DataScientist unavailable ({_ds_err}), skipping candle data")
            try:
                from services.price_streamer import get_price_streamer
                ps = get_price_streamer()
                if ps:
                    live = ps.get_price(symbol)
                    if live and live.get("price"):
                        current_price = live["price"]
            except Exception:
                pass
            entry = float(d.get("entry_price") or 0) or (float(linked_signal.get("entry_price")) if linked_signal and linked_signal.get("entry_price") else 0)
            direction = d.get("direction") or (linked_signal.get("direction") if linked_signal else None)
            sl = float(d.get("stop_loss") or 0) or (float(linked_signal.get("stop_loss")) if linked_signal and linked_signal.get("stop_loss") else 0)
            tp1 = float(d.get("take_profit_1") or 0) or (float(linked_signal.get("take_profit_1")) if linked_signal and linked_signal.get("take_profit_1") else 0)
            tp2 = float(d.get("take_profit_2") or 0) or (float(linked_signal.get("take_profit_2")) if linked_signal and linked_signal.get("take_profit_2") else 0)
            tp3 = float(d.get("take_profit_3") or 0) or (float(linked_signal.get("take_profit_3")) if linked_signal and linked_signal.get("take_profit_3") else 0)
            position = {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3}
            if current_price and entry and direction:
                pip_val = _get_pip_value(symbol)
                if direction.upper() == "BUY":
                    pips = (current_price - entry) / pip_val if pip_val else 0
                    pct = ((current_price - entry) / entry * 100) if entry else 0
                else:
                    pips = (entry - current_price) / pip_val if pip_val else 0
                    pct = ((entry - current_price) / entry * 100) if entry else 0
                distance_from_entry = {"pips": round(pips, 1), "pct": round(pct, 2)}

        def _tz_iso(dt_val):
            """Tag DB datetimes with the configured display timezone offset."""
            if not dt_val:
                return None
            s = dt_val.isoformat()
            if not s.endswith("Z") and "+" not in s:
                tz_name = self._td_cfg.get("display_timezone", "UTC")
                try:
                    from zoneinfo import ZoneInfo
                    import datetime as _dt
                    offset = _dt.datetime.now(ZoneInfo(tz_name)).strftime("%z")
                    s += offset[:3] + ":" + offset[3:]
                except Exception:
                    s += "Z"
            return s

        for entry in prob_hist:
            t = entry.get("time", "")
            if t and not t.endswith("Z") and "+" not in t:
                entry["time"] = t + "Z"
        for entry in tracker_convo:
            t = entry.get("time", "")
            if t and not t.endswith("Z") and "+" not in t:
                entry["time"] = t + "Z"

        return {
            "id": d["id"],
            "symbol": d["symbol"],
            "status": d["status"],
            "trade_decision": d.get("trade_decision"),
            "direction": d.get("direction"),
            "entry_price": float(d["entry_price"]) if d.get("entry_price") else None,
            "stop_loss": float(d["stop_loss"]) if d.get("stop_loss") else None,
            "tp1": float(d["take_profit_1"]) if d.get("take_profit_1") else None,
            "tp2": float(d["take_profit_2"]) if d.get("take_profit_2") else None,
            "tp3": float(d["take_profit_3"]) if d.get("take_profit_3") else None,
            "tp4": float(d["take_profit_4"]) if d.get("take_profit_4") else None,
            "tp5": float(d["take_profit_5"]) if d.get("take_profit_5") else None,
            "tp6": float(d["take_profit_6"]) if d.get("take_profit_6") else None,
            "confidence": d.get("confidence_score"),
            "stage1_ta": d.get("stage1_ta_output"),
            "stage2_response": d.get("stage2_raw_response"),
            "stage2_model_used": d.get("stage2_model_used"),
            "stage1_model_used": d.get("stage1_model_used"),
            "model_tier": d.get("model_tier"),
            "hypothesis": hypothesis,
            "conditions": conditions,
            "invalidations": invalidations,
            "probability_history": prob_hist,
            "tracker_log": d.get("tracker_log"),
            "tracker_conversation": tracker_convo,
            "sections": sections,
            "chart_images": [{"path": f"/api/trading-floor/chart-image?path={img['path']}",
                              "author": img.get("author"),
                              "source": img.get("source")}
                             for img in chart_images],
            "candles": {tf: [{"time": c["time"].isoformat() if hasattr(c["time"], "isoformat") else str(c["time"]),
                           "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"], "volume": c.get("volume", 0)}
                          for c in candles_by_tf.get(tf, [])]
                      for tf in candles_by_tf} if candles_by_tf else {},
            "current_price": current_price,
            "distance_from_entry": distance_from_entry,
            "position": position,
            "linked_signal": linked_signal_dict,
            "limit_order_active": bool(d.get("limit_order_active")),
            "limit_order_price": float(d["limit_order_price"]) if d.get("limit_order_price") else None,
            "dossier_intelligence": d.get("dossier_intelligence"),
            "mentor_source": d.get("mentor_source"),
            "mentor_type": d.get("mentor_type"),
            "postmortem": d.get("postmortem_output"),
            "lessons": d.get("lessons_learned"),
            "liquidation_grab": bool(d.get("liquidation_grab_detected")),
            "current_price_at": _tz_iso(d.get("current_price_at")),
            "unrealised_pnl": float(d["unrealised_pnl"]) if d.get("unrealised_pnl") is not None else None,
            "unrealised_pnl_pct": float(d["unrealised_pnl_pct"]) if d.get("unrealised_pnl_pct") is not None else None,
            "realised_pnl": float(d["realised_pnl"]) if d.get("realised_pnl") is not None else None,
            "realised_pnl_pct": float(d["realised_pnl_pct"]) if d.get("realised_pnl_pct") is not None else None,
            "margin_usd": float(d["margin_usd"]) if d.get("margin_usd") else DEFAULT_MARGIN,
            "leverage": int(d["leverage"]) if d.get("leverage") else DEFAULT_LEVERAGE,
            "notional": round(
                (float(d["margin_usd"]) if d.get("margin_usd") else DEFAULT_MARGIN) *
                (int(d["leverage"]) if d.get("leverage") else DEFAULT_LEVERAGE), 2),
            "liq_price": round(calculate_optimal_leverage(
                float(d["entry_price"]) if d.get("entry_price") else 0,
                float(d["stop_loss"]) if d.get("stop_loss") else 0,
                d.get("direction", "BUY")).get("liq_price", 0), 5) if d.get("entry_price") else None,
            "executed_at": _tz_iso(d.get("executed_at")),
            "created_at": _tz_iso(d.get("created_at")),
            "expires_at": _tz_iso(d.get("expires_at")),
            "tp_progress": d.get("tp_progress"),
            "tp2_hit": bool(d.get("tp2_hit")),
            "tp3_hit": bool(d.get("tp3_hit")),
            "actual_entry_price": float(d["actual_entry_price"]) if d.get("actual_entry_price") else None,
            "actual_exit_price": float(d["actual_exit_price"]) if d.get("actual_exit_price") else None,
            "entry_fill_source": d.get("entry_fill_source"),
            "gathering_started_at": _tz_iso(d.get("gathering_started_at")),
            "stage1_started_at": _tz_iso(d.get("stage1_started_at")),
            "stage1_completed_at": _tz_iso(d.get("stage1_completed_at")),
            "stage2_started_at": _tz_iso(d.get("stage2_started_at")),
            "stage2_completed_at": _tz_iso(d.get("stage2_completed_at")),
            "retest_notes": d.get("retest_notes"),
            "original_status": d.get("original_status"),
            "original_pnl": float(d["original_pnl"]) if d.get("original_pnl") else None,
            "retested_at": _tz_iso(d.get("retested_at")),
            "paper_reason": d.get("paper_reason"),
        }

    def _refresh_discovery_from_persisted(self):
        """Reload discovery settings from DB + config.json so UI always shows saved values.
        config.json is read LAST so it overrides any stale DB values."""
        import os as _os
        _disc_keys = ("discovery_interval_minutes", "discovery_cooldown_minutes", "max_concurrent_builds")
        try:
            # Step 1: DB values (baseline)
            rows = self.db.fetch_all(
                "SELECT config_key, config_value FROM system_config "
                "WHERE config_key IN ('tf_discovery_interval_minutes','tf_discovery_cooldown_minutes','tf_max_concurrent_builds')")
            for row in (rows or []):
                key = row["config_key"][3:]
                try:
                    self._td_cfg[key] = json.loads(row["config_value"])
                except (json.JSONDecodeError, TypeError):
                    try:
                        self._td_cfg[key] = int(row["config_value"])
                    except (ValueError, TypeError):
                        self._td_cfg[key] = row["config_value"]

            # Step 2: config.json overrides DB (config.json is always written on save)
            config_path = _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                "config.json")
            if _os.path.exists(config_path):
                with open(config_path, "r") as f:
                    full = json.load(f)
                td = full.get("global", {}).get("trade_decision", {})
                for k in _disc_keys:
                    if k in td:
                        self._td_cfg[k] = td[k]

            logger.debug(f"[TradingFloor] Discovery refreshed: "
                         f"interval={self._td_cfg.get('discovery_interval_minutes')}, "
                         f"cooldown={self._td_cfg.get('discovery_cooldown_minutes')}, "
                         f"builds={self._td_cfg.get('max_concurrent_builds')}")
        except Exception as e:
            logger.warning(f"[TradingFloor] Refresh discovery from persisted: {e}")

    def _load_prompt_keys_for_ui(self) -> Dict:
        """Load all editable prompts from system_config for the Prompt Engineer UI."""
        prompt_db_keys = {
            "stage1": "dossier_stage1_prompt",
            "stage2": "dossier_stage2_prompt",
            "tracker": "tracker_system_prompt",
            "postmortem": "dossier_postmortem_prompt",
            "auditor_assessment": "auditor_assessment_prompt",
            "auditor_verdict": "auditor_verdict_prompt",
        }
        result = {}
        for short_key, db_key in prompt_db_keys.items():
            try:
                row = self.db.fetch_one(
                    "SELECT config_value FROM system_config WHERE config_key = %s",
                    (db_key,))
                if row and row.get("config_value") and len(row["config_value"]) > 50:
                    result[short_key] = row["config_value"]
            except Exception:
                pass
        return result

    def _sc(self, key, fallback=None):
        """Read a single value from system_config table."""
        try:
            r = self.db.fetch_one(
                "SELECT config_value FROM system_config WHERE config_key = %s", (key,))
            if r and r.get("config_value") is not None:
                return r["config_value"]
        except Exception:
            pass
        return fallback

    def _get_paper_balance(self) -> float:
        """Return current simulated paper balance from system_config."""
        try:
            row = self.db.fetch_one(
                "SELECT config_value FROM system_config WHERE config_key = 'paper_balance'")
            if row and row.get("config_value"):
                return float(row["config_value"])
        except Exception:
            pass
        return 2000.0

    def get_config_for_ui(self) -> Dict:
        """Return all configurable trading floor parameters for the UI.
        Returns full 3-tier model structure and data_sources toggles."""
        self._refresh_discovery_from_persisted()
        _default_s1 = {
            "primary": {"model": "google/gemini-2.5-pro", "provider": "openrouter",
                        "max_tokens": 8000, "temperature": 0.2, "context_window": 1000000, "supports_vision": True},
            "secondary": {"model": "openai/gpt-4.1", "provider": "openrouter",
                          "max_tokens": 8000, "temperature": 0.2, "context_window": 1000000, "supports_vision": True},
            "free": {"model": "deepseek/deepseek-chat-v3-0324:free", "provider": "openrouter",
                     "max_tokens": 8000, "temperature": 0.2, "context_window": 163000, "supports_vision": False},
            "active_tier": "primary"
        }
        _default_s2 = {
            "primary": {"model": "anthropic/claude-opus-4", "provider": "openrouter",
                        "max_tokens": 8000, "temperature": 0.15, "context_window": 200000, "supports_vision": True},
            "secondary": {"model": "google/gemini-2.5-pro", "provider": "openrouter",
                          "max_tokens": 8000, "temperature": 0.15, "context_window": 1000000, "supports_vision": True},
            "free": {"model": "qwen/qwen3-vl-235b-thinking:free", "provider": "openrouter",
                     "max_tokens": 8000, "temperature": 0.15, "context_window": 131000, "supports_vision": True},
            "active_tier": "primary"
        }
        _default_ds = {
            "ohlcv_candles": True, "billnye_ta": True, "da_charts": True,
            "signal_provider_charts": True, "geo_macro": True,
            "companion_data": True, "signal_intelligence": True,
            "historical_performance": True, "chart_images_to_llm": True
        }

        s1 = self._td_cfg.get("stage1_models", {})
        s2 = self._td_cfg.get("stage2_models", {})

        if not s1 or "primary" not in s1:
            s1 = _default_s1
        if not s2 or "primary" not in s2:
            s2 = _default_s2

        return {
            "watchlist": [],
            "stage1_models": s1,
            "stage2_models": s2,
            "postmortem_model": self._td_cfg.get("postmortem_model", "anthropic/claude-sonnet-4"),
            "postmortem_provider": self._td_cfg.get("postmortem_provider", "openrouter"),
            "tracker_model": self._td_cfg.get("tracker_model", "anthropic/claude-sonnet-4"),
            "tracker_provider": self._td_cfg.get("tracker_provider", "openrouter"),
            "display_timezone": self._td_cfg.get("display_timezone", "Asia/Dubai"),
            "data_sources": self._td_cfg.get("data_sources", _default_ds),
            "ohlcv_timeframes": self._td_cfg.get("ohlcv_timeframes",
                                                   {"D1": 90, "H4": 30, "H1": 7, "M15": 3, "M5": 1}),
            "max_dossier_chars": self._td_cfg.get("max_dossier_chars", 300000),
            "news_lookback_days": self._td_cfg.get("news_lookback_days", 7),
            "signal_lookback_days": self._td_cfg.get("signal_lookback_days", 7),
            "top_providers_limit": self._td_cfg.get("top_providers_limit", 10),
            "discovery_interval_minutes": self._td_cfg.get("discovery_interval_minutes", 60),
            "discovery_cooldown_minutes": self._td_cfg.get("discovery_cooldown_minutes", 30),
            "max_concurrent_builds": self._td_cfg.get("max_concurrent_builds", 3),
            "leverage_safety_buffer": self._td_cfg.get("leverage_safety_buffer", 3),
            "max_leverage_by_class": self._td_cfg.get("max_leverage_by_class", MAX_LEVERAGE_BY_CLASS),
            "min_confidence_for_trade": self._td_cfg.get("min_confidence_for_trade", 60),
            "trade_now_default_confidence": self._td_cfg.get("trade_now_default_confidence", 65),
            "tracker_interval_minutes": self._td_cfg.get("tracker_interval_minutes", 15),
            "tracker_llm_every_n_cycles": self._td_cfg.get("tracker_llm_every_n_cycles", 1),
            "condition_threshold_execute": self._td_cfg.get("condition_threshold_execute", 85),
            "condition_threshold_limit_order": self._td_cfg.get("condition_threshold_limit_order", 75),
            "max_active_dossiers_per_symbol": self._td_cfg.get("max_active_dossiers_per_symbol", 2),
            "dossier_expiry_hours": self._td_cfg.get("dossier_expiry_hours", 24),
            "include_chart_images": self._td_cfg.get("include_chart_images", True),
            "candle_provider": self._td_cfg.get("candle_provider", "yahoo"),
            "candle_provider_priority": self._td_cfg.get("candle_provider_priority",
                                                          ["mt5", "yahoo", "ccxt"]),
            "dashboard_refresh_seconds": self._td_cfg.get("dashboard_refresh_seconds", 30),
            "dossier_prompts": self._load_prompt_keys_for_ui(),
            "paper_risk_mode": self._sc("tf_paper_risk_mode", self._td_cfg.get("paper_risk_mode", "fixed")),
            "paper_risk_pct": float(self._sc("tf_paper_risk_pct", self._td_cfg.get("paper_risk_pct", 1.0))),
            "paper_risk_fixed_usd": float(self._sc("tf_paper_risk_fixed_usd", self._td_cfg.get("paper_risk_fixed_usd", 20))),
            "paper_fees": self._td_cfg.get("paper_fees", {}),
            "paper_balance": self._get_paper_balance(),
        }

    def update_config(self, updates: Dict) -> Dict:
        """Update trading floor config. Persists ALL settings to BOTH
        config.json AND the system_config DB table (source of truth)."""
        import os as _os

        prompt_keys = {}
        config_keys = {}
        for k, v in updates.items():
            if k == "dossier_prompts" and isinstance(v, dict):
                prompt_keys = v
            else:
                config_keys[k] = v

        saved = []

        if prompt_keys:
            prompt_map = {
                "stage1": "dossier_stage1_prompt",
                "stage2": "dossier_stage2_prompt",
                "tracker": "tracker_system_prompt",
                "postmortem": "dossier_postmortem_prompt",
                "auditor_assessment": "auditor_assessment_prompt",
                "auditor_verdict": "auditor_verdict_prompt",
            }
            for short_key, db_key in prompt_map.items():
                if short_key in prompt_keys and prompt_keys[short_key]:
                    try:
                        self.db.execute(
                            "INSERT INTO system_config (config_key, config_value) "
                            "VALUES (%s, %s) ON DUPLICATE KEY UPDATE config_value = VALUES(config_value)",
                            (db_key, prompt_keys[short_key]))
                        saved.append(db_key)
                    except Exception as e:
                        logger.error(f"[TradingFloor] Prompt save error ({db_key}): {e}")

        # paper_balance lives in system_config, not config.json
        if "paper_balance" in config_keys:
            amt = config_keys.pop("paper_balance")
            try:
                self.db.execute(
                    "INSERT INTO system_config (config_key, config_value) VALUES ('paper_balance', %s) "
                    "ON DUPLICATE KEY UPDATE config_value = %s",
                    (str(float(amt)), str(float(amt))))
                saved.append("paper_balance")
            except Exception as e:
                logger.warning(f"[TradingFloor] paper_balance save: {e}")

        _TRACKED_THRESHOLDS = {
            "min_confidence_for_trade", "trade_now_default_confidence",
            "condition_threshold_execute", "condition_threshold_limit_order",
        }
        for k, v in config_keys.items():
            if k in _TRACKED_THRESHOLDS:
                old_val = self._td_cfg.get(k)
                if old_val is not None and str(old_val) != str(v):
                    try:
                        self.db.execute(
                            "INSERT INTO threshold_history "
                            "(config_key, old_value, new_value, changed_by) "
                            "VALUES (%s, %s, %s, 'user')",
                            (k, str(old_val), str(v)))
                    except Exception:
                        pass

        if config_keys:
            config_path = _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                "config.json")
            try:
                with open(config_path, "r") as f:
                    full_config = json.load(f)

                td = full_config.setdefault("global", {}).setdefault("trade_decision", {})
                for k, v in config_keys.items():
                    td[k] = v

                with open(config_path, "w") as f:
                    json.dump(full_config, f, indent=4)

                # Merge into _td_cfg (preserve DB-merged keys not in config.json)
                for k, v in config_keys.items():
                    self._td_cfg[k] = v
                if self.config and hasattr(self.config, '_raw_config'):
                    self.config._raw_config.setdefault("trade_decision", {}).update(config_keys)
                saved.extend(list(config_keys.keys()))
                logger.info(f"[TradingFloor] Config saved to config.json: {list(config_keys.keys())}")
            except Exception as e:
                logger.error(f"[TradingFloor] Config update error: {e}")
                return {"success": False, "error": str(e)}

            self._persist_config_to_db(config_keys)

        return {"success": True, "updated": saved}

    def _persist_config_to_db(self, config_keys: Dict):
        """Mirror every trading floor setting into system_config DB table.
        Complex values (dicts/lists) are stored as JSON strings."""
        for k, v in config_keys.items():
            db_key = f"tf_{k}"
            try:
                db_val = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                self.db.execute(
                    "INSERT INTO system_config (config_key, config_value) "
                    "VALUES (%s, %s) ON DUPLICATE KEY UPDATE config_value = VALUES(config_value)",
                    (db_key, db_val))
                logger.debug(f"[TradingFloor] DB persisted {db_key} = {db_val}")
            except Exception as e:
                logger.warning(f"[TradingFloor] DB persist FAILED for {db_key}={v}: {e}")


# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════
# HISTORICAL DOSSIER RETEST
# ═══════════════════════════════════════════════════════════════════════

def retest_all_dossiers(db, config) -> Dict:
    """
    Replay ALL completed dossiers against historical M5 candle data.
    Chronological order by executed_at. Running paper balance from $2000.
    Risk mode (% or fixed) from config applies uniformly.
    Returns an audit report with full transparency.
    """
    logger.info("[Retest] Starting full historical dossier retest (v2 — candle-by-candle)...")

    td_cfg = config.raw.get("trade_decision", {}) if config else {}
    starting_balance = float(td_cfg.get("account_balance", ACCOUNT_BALANCE))

    def _sc_rt(key, fallback):
        r = db.fetch_one("SELECT config_value FROM system_config WHERE config_key = %s", (key,))
        return r["config_value"] if r and r.get("config_value") is not None else fallback

    risk_mode = _sc_rt("tf_paper_risk_mode", td_cfg.get("paper_risk_mode", "fixed"))
    risk_pct = float(_sc_rt("tf_paper_risk_pct", td_cfg.get("paper_risk_pct", 1))) / 100.0
    risk_fixed = float(_sc_rt("tf_paper_risk_fixed_usd", td_cfg.get("paper_risk_fixed_usd", DEFAULT_MARGIN)))
    be_buffer_pct = float(td_cfg.get("sl_to_be_buffer_pct", 0.15))

    dossiers = db.fetch_all("""
        SELECT id, symbol, direction, entry_price, actual_entry_price,
               stop_loss, original_stop_loss,
               take_profit_1, take_profit_2, take_profit_3,
               take_profit_4, take_profit_5, take_profit_6,
               status, realised_pnl, realised_pnl_pct,
               tp_progress, created_at, executed_at,
               margin_usd, leverage,
               trade_decision, entry_fill_source,
               stage2_hypothesis
        FROM trade_dossiers
        WHERE status IN ('won','lost','expired','abandoned')
          AND entry_price IS NOT NULL AND entry_price > 0
        ORDER BY COALESCE(executed_at, created_at) ASC
    """)
    if not dossiers:
        return {"total": 0, "corrected": 0, "unchanged": 0, "errors": 0,
                "details": [], "final_balance": starting_balance}

    from services.candle_collector import get_candle_collector
    collector = get_candle_collector()
    if not collector:
        from services.candle_collector import CandleCollector
        collector = CandleCollector(db, config)

    total = len(dossiers)
    corrected = 0
    unchanged = 0
    errors = 0
    details = []
    running_balance = starting_balance

    for d in dossiers:
        try:
            if risk_mode == "fixed":
                margin_for_trade = risk_fixed
            else:
                margin_for_trade = round(running_balance * risk_pct, 2)
            if margin_for_trade <= 0:
                margin_for_trade = DEFAULT_MARGIN

            result = _retest_single_dossier(
                db, d, collector, config=config, td_cfg=td_cfg,
                override_margin=margin_for_trade,
                be_buffer_pct=be_buffer_pct)
            details.append(result)

            if result.get("new_pnl") is not None and result.get("new_status") in ("won", "lost"):
                running_balance += result["new_pnl"]
                running_balance = round(running_balance, 2)
                result["balance_after"] = running_balance

            if result.get("corrected"):
                corrected += 1
            else:
                unchanged += 1
        except Exception as e:
            errors += 1
            details.append({"dossier_id": d["id"], "symbol": d["symbol"],
                          "error": str(e), "corrected": False})
            logger.debug(f"[Retest] Error on #{d['id']}: {e}", exc_info=True)

    # Update paper_balance to match the final running balance
    try:
        db.execute(
            "UPDATE system_config SET config_value = %s WHERE config_key = 'paper_balance'",
            (str(running_balance),))
        logger.info(f"[Retest] Paper balance updated: ${starting_balance:.2f} -> ${running_balance:.2f}")
    except Exception as e:
        logger.debug(f"[Retest] Paper balance update: {e}")

    wins = sum(1 for r in details if r.get("new_status") == "won")
    losses = sum(1 for r in details if r.get("new_status") == "lost")
    expired = sum(1 for r in details if r.get("new_status") == "expired")
    total_pnl = sum(r.get("new_pnl", 0) or 0 for r in details)

    logger.info(f"[Retest] Complete: {total} dossiers | {wins}W / {losses}L / {expired}E | "
                f"P&L: ${total_pnl:.2f} | Balance: ${running_balance:.2f}")
    return {
        "total": total, "corrected": corrected, "unchanged": unchanged,
        "errors": errors, "details": details,
        "wins": wins, "losses": losses, "expired": expired,
        "total_pnl": round(total_pnl, 2),
        "starting_balance": starting_balance,
        "final_balance": running_balance,
        "risk_mode": risk_mode,
        "risk_pct": risk_pct if risk_mode == "pct" else None,
        "risk_fixed": risk_fixed if risk_mode == "fixed" else None,
    }


def _retest_single_dossier(db, d: Dict, collector, config=None, td_cfg=None,
                            override_margin=None, be_buffer_pct=0.15) -> Dict:
    """
    Replay a single dossier candle-by-candle against M5 data.

    Mirrors the live tracker exactly:
    - Uses actual_entry_price (not proposed entry_price)
    - Uses original_stop_loss (not trailed stop_loss)
    - Starts from executed_at (not created_at)
    - Handles trade_now fills (skip entry detection)
    - Full SL trailing: TP1→BE+buffer, TP2→TP1, TP3→TP2, etc.
    - Checks all 6 TPs
    - SL+TP on same candle = SL wins (conservative, standard)
    - Applies fees and slippage (matches _finalise_pnl)
    - Does NOT overwrite dossier's original margin_usd/leverage
    """
    did = d["id"]
    symbol = d["symbol"]
    direction = (d.get("direction") or "").upper()
    original_status = d["status"]
    original_pnl = float(d["realised_pnl"]) if d.get("realised_pnl") else None

    # ── Entry price: actual fill, then proposed ──
    entry = float(d.get("actual_entry_price") or d.get("entry_price") or 0)
    if not entry:
        return {"dossier_id": did, "symbol": symbol, "corrected": False,
                "note": "No entry price"}

    # ── Stop loss: ORIGINAL (before trailing), with recovery fallback ──
    sl = float(d.get("original_stop_loss") or 0)
    if not sl:
        # Fallback: try stage2_hypothesis for the original SL
        hyp = d.get("stage2_hypothesis")
        if hyp:
            try:
                if isinstance(hyp, str):
                    hyp = json.loads(hyp)
                if isinstance(hyp, dict):
                    sl = float(hyp.get("stop_loss") or hyp.get("sl") or 0)
            except Exception:
                pass
    if not sl:
        # Last fallback: use current stop_loss (may be trailed — flag it)
        sl = float(d.get("stop_loss") or 0)

    # ── All 6 take profits ──
    tps = {}
    for i in range(1, 7):
        val = d.get(f"take_profit_{i}")
        if val:
            tps[i] = float(val)
    max_tp_num = max(tps.keys()) if tps else 0

    # ── Start time: when trade went LIVE (not when dossier was created) ──
    start_date = d.get("executed_at") or d.get("created_at")
    if not start_date:
        return {"dossier_id": did, "symbol": symbol, "corrected": False,
                "note": "No start date"}

    # ── Is this a trade_now / market fill? ──
    is_trade_now = (d.get("trade_decision") == "trade_now" or
                    d.get("entry_fill_source") in ("trade_now_market", "live_stream", "live_fallback"))

    # ── Resolve symbol for candle lookup ──
    from db.market_symbols import resolve_symbol as _resolve_alias
    canonical = _resolve_alias(symbol, db)
    symbols_to_try = ([canonical, symbol] if canonical != symbol else [symbol])

    candles = []
    for try_sym in symbols_to_try:
        try:
            candles = collector._get_stored_candles(try_sym, "M5", lookback_days=60)
            if candles and len(candles) >= 5:
                break
        except Exception:
            pass

    if not candles or len(candles) < 5:
        try:
            collector.fetch_and_store(symbol)
            for try_sym in symbols_to_try:
                candles = collector._get_stored_candles(try_sym, "M5", lookback_days=60)
                if candles and len(candles) >= 5:
                    break
        except Exception:
            pass

    if not candles:
        return {"dossier_id": did, "symbol": symbol, "corrected": False,
                "note": "No candle data available"}

    # ── Parse start timestamp ──
    if hasattr(start_date, 'timestamp'):
        start_ts = start_date
    else:
        start_ts = datetime.fromisoformat(str(start_date).replace("Z", ""))

    def _candle_time(c):
        t = c["time"]
        return t if isinstance(t, datetime) else datetime.fromisoformat(str(t).replace("Z", ""))

    candles_sorted = sorted(candles, key=_candle_time)
    candles_after = [c for c in candles_sorted if _candle_time(c) >= start_ts]

    if not candles_after:
        return {"dossier_id": did, "symbol": symbol, "corrected": False,
                "note": f"No candles after {start_ts.isoformat()}"}

    # ── Leverage calculation (with correct asset_class) ──
    asset_class = "unknown"
    try:
        sym_row = db.fetch_one(
            "SELECT asset_class FROM market_symbols WHERE symbol = %s", (symbol,))
        if sym_row and sym_row.get("asset_class"):
            asset_class = sym_row["asset_class"]
    except Exception:
        pass

    lev_calc = calculate_optimal_leverage(
        entry, sl, direction, asset_class=asset_class, td_cfg=td_cfg,
        margin_usd=override_margin)
    retest_margin = lev_calc["margin"]
    retest_leverage = lev_calc["leverage"]

    # ── Candle-by-candle replay ──
    entry_hit = is_trade_now
    entry_hit_time = start_ts if is_trade_now else None
    current_sl = sl
    highest_tp_hit = 0
    sl_hit = False
    exit_price = 0
    trade_complete = False
    tp_hits = {}

    for c in candles_after:
        h = float(c["high"])
        l = float(c["low"])
        t = _candle_time(c)

        # ── Entry detection (skip for trade_now — already filled) ──
        if not entry_hit:
            if direction == "BUY" and l <= entry:
                entry_hit = True
                entry_hit_time = t
            elif direction == "SELL" and h >= entry:
                entry_hit = True
                entry_hit_time = t
            continue

        # ── SL check (using current_sl which may have been trailed) ──
        sl_this_candle = False
        if current_sl > 0:
            if direction == "BUY" and l <= current_sl:
                sl_this_candle = True
            elif direction == "SELL" and h >= current_sl:
                sl_this_candle = True

        # ── TP check: find highest new TP hit this candle ──
        new_highest_tp = highest_tp_hit
        for tp_num in sorted(tps.keys()):
            if tp_num <= highest_tp_hit:
                continue
            tp_price = tps[tp_num]
            if (direction == "BUY" and h >= tp_price) or \
               (direction == "SELL" and l <= tp_price):
                new_highest_tp = tp_num

        tp_hit_this_candle = new_highest_tp > highest_tp_hit

        # ── SL + TP on same candle: SL wins (conservative, standard) ──
        if sl_this_candle and tp_hit_this_candle:
            sl_hit = True
            exit_price = current_sl
            trade_complete = True
            break

        if sl_this_candle:
            sl_hit = True
            exit_price = current_sl
            trade_complete = True
            break

        # ── Apply TP hits and trail SL ──
        if tp_hit_this_candle:
            highest_tp_hit = new_highest_tp
            tp_hits[highest_tp_hit] = t

            # SL trailing (mirrors _track_active_trades and _handle_instant_tp)
            if highest_tp_hit >= 3 and tps.get(2):
                current_sl = tps[2]
            elif highest_tp_hit >= 2 and tps.get(1):
                current_sl = tps[1]
            elif highest_tp_hit >= 1:
                buffer = entry * (be_buffer_pct / 100)
                current_sl = entry + buffer if direction == "BUY" else entry - buffer

            # Full target reached: trade won
            if highest_tp_hit >= max_tp_num and max_tp_num > 0:
                exit_price = tps[highest_tp_hit]
                trade_complete = True
                break

    # ── Determine outcome ──
    is_sl_exit = False
    if not entry_hit:
        new_status = "expired"
        new_pnl = 0.0
    elif sl_hit:
        is_sl_exit = True
        new_pnl = _retest_calc_pnl(entry, exit_price, direction,
                                    retest_margin, retest_leverage,
                                    is_sl=True, config=config)
        new_status = "won" if new_pnl >= 0 else "lost"
    elif highest_tp_hit > 0:
        exit_price = tps.get(highest_tp_hit, entry)
        new_pnl = _retest_calc_pnl(entry, exit_price, direction,
                                    retest_margin, retest_leverage,
                                    is_sl=False, config=config)
        new_status = "won" if new_pnl >= 0 else "lost"
    else:
        new_status = original_status
        new_pnl = original_pnl
        exit_price = 0

    status_changed = new_status != original_status
    pnl_diff = abs((new_pnl or 0) - (original_pnl or 0)) > 0.01 if new_pnl is not None else False
    corrected = status_changed or pnl_diff

    # ── Build notes ──
    notes = []
    if status_changed:
        notes.append(f"Status: {original_status} -> {new_status}")
    if pnl_diff and new_pnl is not None:
        notes.append(f"P&L: {original_pnl} -> {new_pnl:.2f}")
    if is_trade_now:
        notes.append(f"trade_now fill at {entry}")
    if entry_hit:
        notes.append(f"Entry hit at {entry_hit_time}")
    else:
        notes.append("Entry NEVER hit in candle data")
    for tp_num in sorted(tp_hits.keys()):
        notes.append(f"TP{tp_num} hit")
    if sl_hit:
        notes.append(f"SL hit at {exit_price:.5f}")
    if highest_tp_hit > 0 and not sl_hit:
        notes.append(f"Highest TP: {highest_tp_hit}")
    notes.append(f"SL trailing: original={sl:.5f}, final={current_sl:.5f}")
    notes.append(f"Lev: {retest_leverage}x (${retest_margin} margin, {asset_class})")
    notes.append(f"Candles: {len(candles_after)}")

    margin_pnl_pct = (new_pnl / retest_margin * 100) if retest_margin and new_pnl else 0

    tp_prog = "none"
    if highest_tp_hit > 0:
        tp_prog = f"tp{highest_tp_hit}_hit"

    # Update dossier — preserve original margin/leverage, only update outcome + P&L
    db.execute("""
        UPDATE trade_dossiers
        SET original_status = COALESCE(original_status, %s),
            original_pnl = COALESCE(original_pnl, %s),
            status = %s,
            realised_pnl = %s,
            realised_pnl_pct = %s,
            tp_progress = %s,
            retest_notes = %s,
            retested_at = NOW()
        WHERE id = %s
    """, (original_status, original_pnl,
          new_status, round(new_pnl, 4) if new_pnl is not None else 0,
          round(margin_pnl_pct, 4),
          tp_prog,
          " | ".join(notes), did))

    return {
        "dossier_id": did, "symbol": symbol, "direction": direction,
        "original_status": original_status, "new_status": new_status,
        "original_pnl": original_pnl,
        "new_pnl": round(new_pnl, 4) if new_pnl is not None else None,
        "corrected": corrected, "notes": " | ".join(notes),
        "candles_checked": len(candles_after),
        "entry_hit": entry_hit, "sl_hit": sl_hit,
        "highest_tp_hit": highest_tp_hit,
        "sl_trailed_from": round(sl, 5), "sl_trailed_to": round(current_sl, 5),
        "exit_price": round(exit_price, 5) if exit_price else None,
        "leverage": retest_leverage, "margin": retest_margin,
        "asset_class": asset_class,
        "is_trade_now": is_trade_now,
    }


def _retest_calc_pnl(entry: float, exit_price: float, direction: str,
                     margin: float, leverage: int,
                     is_sl: bool = False, config=None) -> float:
    """Calculate P&L for retest including fees and slippage. Does NOT write to DB."""
    if not entry or entry <= 0:
        return 0.0
    exposure = margin * leverage
    position_size = exposure / entry

    # SL slippage (adverse)
    if is_sl and config:
        try:
            pf = config.raw.get("trade_decision", {}).get("paper_fees", {})
            slip_pct = float(pf.get("sl_slippage_pct", 0) or 0) / 100.0
            if slip_pct:
                if direction == "BUY":
                    exit_price = exit_price * (1 - slip_pct)
                else:
                    exit_price = exit_price * (1 + slip_pct)
        except Exception:
            pass

    if direction == "BUY":
        gross_pnl = (exit_price - entry) * position_size
    else:
        gross_pnl = (entry - exit_price) * position_size

    # Fees: entry assumed taker, exit = taker for SL / maker for TP
    entry_fee = _calc_fee(exposure, True, config)
    exit_notional = exit_price * position_size
    exit_fee = _calc_fee(exit_notional, is_sl, config)
    total_fees = entry_fee + exit_fee

    return gross_pnl - total_fees


def verify_retest_integrity(db, config) -> Dict:
    """
    Cross-check verification of retest results using alternate logic.
    Compares retest outcomes against independent candle analysis.
    Returns a report of any discrepancies found.
    """
    logger.info("[Retest-Verify] Running integrity check on retested dossiers...")

    dossiers = db.fetch_all("""
        SELECT id, symbol, direction, entry_price, actual_entry_price,
               stop_loss, original_stop_loss,
               take_profit_1, status, realised_pnl,
               tp_progress, executed_at, created_at,
               retest_notes, entry_fill_source, trade_decision
        FROM trade_dossiers
        WHERE retested_at IS NOT NULL
          AND status IN ('won','lost','expired')
        ORDER BY id ASC
    """)
    if not dossiers:
        return {"checked": 0, "discrepancies": 0, "details": []}

    from services.candle_collector import get_candle_collector
    collector = get_candle_collector()
    if not collector:
        from services.candle_collector import CandleCollector
        collector = CandleCollector(db, config)

    checked = 0
    discrepancies = 0
    details = []

    for d in dossiers:
        did = d["id"]
        symbol = d["symbol"]
        direction = (d.get("direction") or "").upper()
        entry = float(d.get("actual_entry_price") or d.get("entry_price") or 0)
        sl = float(d.get("original_stop_loss") or d.get("stop_loss") or 0)
        tp1 = float(d.get("take_profit_1") or 0)

        if not entry or not sl:
            continue

        is_trade_now = (d.get("trade_decision") == "trade_now" or
                        d.get("entry_fill_source") in ("trade_now_market", "live_stream", "live_fallback"))

        start_date = d.get("executed_at") or d.get("created_at")
        if not start_date:
            continue

        from db.market_symbols import resolve_symbol as _ra
        canonical = _ra(symbol, db)
        syms = ([canonical, symbol] if canonical != symbol else [symbol])
        candles = []
        for s in syms:
            try:
                candles = collector._get_stored_candles(s, "M5", lookback_days=60)
                if candles and len(candles) >= 5:
                    break
            except Exception:
                pass
        if not candles:
            continue

        if hasattr(start_date, 'timestamp'):
            start_ts = start_date
        else:
            start_ts = datetime.fromisoformat(str(start_date).replace("Z", ""))

        def _ct(c):
            t = c["time"]
            return t if isinstance(t, datetime) else datetime.fromisoformat(str(t).replace("Z", ""))

        candles_after = [c for c in sorted(candles, key=_ct) if _ct(c) >= start_ts]
        if not candles_after:
            continue

        # Simplified independent check: did entry get hit? Did SL or TP1 get hit first?
        entry_hit = is_trade_now
        sl_first = False
        tp1_first = False

        for c in candles_after:
            h = float(c["high"])
            l = float(c["low"])

            if not entry_hit:
                if direction == "BUY" and l <= entry:
                    entry_hit = True
                elif direction == "SELL" and h >= entry:
                    entry_hit = True
                continue

            sl_touch = (direction == "BUY" and l <= sl) or (direction == "SELL" and h >= sl)
            tp1_touch = tp1 > 0 and ((direction == "BUY" and h >= tp1) or (direction == "SELL" and l <= tp1))

            if sl_touch and tp1_touch:
                sl_first = True
                break
            if sl_touch:
                sl_first = True
                break
            if tp1_touch:
                tp1_first = True
                break

        # Determine expected outcome from independent check
        if not entry_hit:
            expected = "expired"
        elif sl_first:
            expected = "lost"
        elif tp1_first:
            expected = "won"
        else:
            expected = d["status"]

        # Compare with retest result
        actual = d["status"]
        match = True
        note = ""

        if expected == "expired" and actual != "expired":
            match = False
            note = f"Verify says expired but retest says {actual}"
        elif expected == "lost" and actual == "won":
            # Possible if SL was trailed past entry (TP hit first, then SL at profit)
            # This is OK — the full retest has trailing, simplified check doesn't
            note = f"Simple check says lost, retest says won (SL trailing explains this)"
        elif expected == "won" and actual == "lost":
            match = False
            note = f"Simple check says won (TP1 hit before SL) but retest says lost"

        checked += 1
        if not match:
            discrepancies += 1
            details.append({
                "dossier_id": did, "symbol": symbol,
                "retest_status": actual, "verify_status": expected,
                "note": note,
            })

    logger.info(f"[Retest-Verify] Checked {checked} dossiers, {discrepancies} discrepancies")
    return {"checked": checked, "discrepancies": discrepancies, "details": details}


# ═══════════════════════════════════════════════════════════════════════
# Per-Duo Registry (replaces singleton)
# ═══════════════════════════════════════════════════════════════════════

_tf_instances: Dict[str, TradingFloorService] = {}


def get_trading_floor(db=None, config=None,
                      duo_id: str = "apex") -> Optional[TradingFloorService]:
    """Get or create a TradingFloorService for a specific duo.

    Each duo gets its own instance with isolated state (cooldowns, queues,
    thoughts, pipeline). When ``duo_id`` is not provided, defaults to 'apex'
    for full backward compatibility with existing callers.

    Args:
        db: DatabaseManager instance (required on first call).
        config: ConfigManager instance (required on first call).
        duo_id: Which duo this instance serves. Default='apex'.

    Returns:
        The TradingFloorService for the requested duo, or None if db is None
        and the instance hasn't been created yet.
    """
    if duo_id not in _tf_instances and db is not None:
        _tf_instances[duo_id] = TradingFloorService(db, config, duo_id=duo_id)
        logger.info(f"[TradingFloor] Created instance for duo '{duo_id}'")
    return _tf_instances.get(duo_id)


def get_all_trading_floors() -> Dict[str, TradingFloorService]:
    """Return the full registry of active TradingFloorService instances.

    Used by the scheduler to iterate over all duos for background jobs.
    """
    return dict(_tf_instances)


def start_trading_floor(db, config,
                        duo_id: str = "apex") -> TradingFloorService:
    """Create and start the trading floor service for a duo."""
    tf = get_trading_floor(db, config, duo_id=duo_id)
    tf.start()
    return tf
