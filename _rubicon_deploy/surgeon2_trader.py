"""Surgeon v2 — Twilly strategy, PostgreSQL-backed (Tickles MCP adaptation).

Same strategy as surgeon_trader.py but:
- Reads market data from Binance public endpoints (mark/index/funding) AND
  cross-references funding from tickles_shared.derivatives_snapshots
- Writes agent decisions to tickles_rubicon.agent_decisions (if table exists)
- Writes trades to tickles_rubicon.trades
- Writes balance snapshots to tickles_rubicon.balance_snapshots

No files. All state lives in Postgres so the rest of the Tickles platform can
consume it (backtester, banker, ui).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s surgeon2 %(message)s")
LOG = logging.getLogger("surgeon2.trader")

STARTING_BALANCE = 10_000.0
TAKER_FEE = 0.0005
SLIPPAGE = 0.0002
LEVERAGE_DEFAULT = 25
MAX_POSITIONS = 3

SIG1_DIVERGENCE_ENTRY = 0.15
SIG1_DIVERGENCE_MAX = 0.30
SIG2_FUNDING_POS = 0.0005
SIG2_FUNDING_NEG = -0.0005

SL_PCT = 0.005
TP1_PCT = 0.010
TP2_PCT = 0.020
TP3_PCT = 0.040
CONVERGENCE_EXIT_THRESH = 0.03
MAX_HOLD_SEC = 45 * 60
STALL_SEC = 15 * 60

ASSETS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}

BINANCE_PREM = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol={s}"
BINANCE_PRICE = "https://fapi.binance.com/fapi/v1/ticker/price?symbol={s}"
BINANCE_KLINE = "https://fapi.binance.com/fapi/v1/klines?symbol={s}&interval=1m&limit=50"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def http_json(url: str, timeout: int = 8) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "rubicon-surgeon2/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def rsi14(closes: List[float]) -> float:
    if len(closes) < 15:
        return 50.0
    gains = losses = 0.0
    for i in range(-14, 0):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses += -d
    if losses == 0:
        return 100.0
    rs = (gains / 14) / (losses / 14)
    return round(100 - (100 / (1 + rs)), 2)


def fetch_market() -> List[dict]:
    rows = []
    for asset, sym in ASSETS.items():
        try:
            prem = http_json(BINANCE_PREM.format(s=sym))
            price = float(http_json(BINANCE_PRICE.format(s=sym))["price"])
            kl = http_json(BINANCE_KLINE.format(s=sym))
            closes = [float(k[4]) for k in kl]
            mark = float(prem.get("markPrice", price))
            index = float(prem.get("indexPrice", mark))
            funding = float(prem.get("lastFundingRate", 0.0))
            div_pct = (mark - index) / index * 100.0 if index else 0.0
            rows.append({
                "symbol": sym,
                "price": price,
                "mark": mark,
                "index": index,
                "divergence_pct": round(div_pct, 4),
                "funding": funding,
                "rsi14": rsi14(closes),
            })
        except Exception as exc:
            LOG.warning("fetch %s failed: %s", sym, exc)
    return rows


# ---------- postgres helpers ----------
def connect_shared():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT", "5432")),
        user=os.environ.get("PGUSER", "admin"),
        password=os.environ.get("PGPASSWORD", ""),
        dbname="tickles_shared",
    )


def connect_company():
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT", "5432")),
        user=os.environ.get("PGUSER", "admin"),
        password=os.environ.get("PGPASSWORD", ""),
        dbname="tickles_rubicon",
    )


def ensure_schema(conn) -> None:
    """Create minimal surgeon v2 tables if they don't exist (idempotent)."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS surgeon2_state (
                id INTEGER PRIMARY KEY CHECK (id=1),
                starting_balance NUMERIC(18,6) NOT NULL,
                balance NUMERIC(18,6) NOT NULL,
                realized_pnl NUMERIC(18,6) NOT NULL DEFAULT 0,
                total_fees NUMERIC(18,6) NOT NULL DEFAULT 0,
                cumulative_turnover NUMERIC(18,6) NOT NULL DEFAULT 0,
                trade_counter INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS surgeon2_positions (
                trade_id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price NUMERIC(18,8) NOT NULL,
                entry_ts TIMESTAMPTZ NOT NULL,
                margin NUMERIC(18,6) NOT NULL,
                leverage INTEGER NOT NULL,
                notional NUMERIC(18,6) NOT NULL,
                sl NUMERIC(18,8), tp1 NUMERIC(18,8), tp2 NUMERIC(18,8), tp3 NUMERIC(18,8),
                tp1_done BOOL NOT NULL DEFAULT FALSE,
                tp2_done BOOL NOT NULL DEFAULT FALSE,
                remaining_frac NUMERIC(10,6) NOT NULL DEFAULT 1,
                last_progress_ts TIMESTAMPTZ NOT NULL,
                divergence_at_entry NUMERIC(10,6),
                funding_at_entry NUMERIC(12,8),
                reason TEXT,
                closed_at TIMESTAMPTZ
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS surgeon2_trade_log (
                id BIGSERIAL PRIMARY KEY,
                ts TIMESTAMPTZ NOT NULL DEFAULT now(),
                trade_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,  -- OPEN/SL/TP1/TP2/TP3/CONVERGENCE/TIME_STOP/STALL/DECISION
                entry_price NUMERIC(18,8),
                exit_price NUMERIC(18,8),
                frac NUMERIC(10,6),
                gross_pnl NUMERIC(18,6),
                fees NUMERIC(18,6),
                net_pnl NUMERIC(18,6),
                cumulative_net_pnl NUMERIC(18,6),
                reason TEXT,
                details JSONB
            );
        """)
        cur.execute("""
            INSERT INTO surgeon2_state (id, starting_balance, balance)
            VALUES (1, %s, %s)
            ON CONFLICT (id) DO NOTHING;
        """, (STARTING_BALANCE, STARTING_BALANCE))
        conn.commit()


# ---------- state load/save via postgres ----------
def load_state(conn) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM surgeon2_state WHERE id=1")
        row = cur.fetchone()
        cur.execute("SELECT * FROM surgeon2_positions WHERE closed_at IS NULL AND remaining_frac > 0 ORDER BY trade_id")
        pos = [dict(r) for r in cur.fetchall()]
    return {"state": dict(row) if row else {}, "positions": pos}


def save_state_row(conn, s: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE surgeon2_state SET balance=%s, realized_pnl=%s, total_fees=%s,
                cumulative_turnover=%s, trade_counter=%s, updated_at=now() WHERE id=1
        """, (s["balance"], s["realized_pnl"], s["total_fees"],
              s["cumulative_turnover"], s["trade_counter"]))


def insert_position(conn, pos: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO surgeon2_positions
            (trade_id, symbol, side, entry_price, entry_ts, margin, leverage,
             notional, sl, tp1, tp2, tp3, last_progress_ts,
             divergence_at_entry, funding_at_entry, reason)
            VALUES (%(trade_id)s,%(symbol)s,%(side)s,%(entry_price)s,%(entry_ts)s,
                    %(margin)s,%(leverage)s,%(notional)s,%(sl)s,%(tp1)s,%(tp2)s,%(tp3)s,
                    %(last_progress_ts)s,%(divergence_at_entry)s,%(funding_at_entry)s,%(reason)s)
        """, pos)


def update_position(conn, pos: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE surgeon2_positions SET
                sl=%(sl)s, tp1_done=%(tp1_done)s, tp2_done=%(tp2_done)s,
                remaining_frac=%(remaining_frac)s, last_progress_ts=%(last_progress_ts)s,
                closed_at=%(closed_at)s
            WHERE trade_id=%(trade_id)s
        """, pos)


def insert_log(conn, entry: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO surgeon2_trade_log
            (ts, trade_id, symbol, side, action, entry_price, exit_price, frac,
             gross_pnl, fees, net_pnl, cumulative_net_pnl, reason, details)
            VALUES (%(ts)s,%(trade_id)s,%(symbol)s,%(side)s,%(action)s,
                    %(entry_price)s,%(exit_price)s,%(frac)s,%(gross_pnl)s,%(fees)s,
                    %(net_pnl)s,%(cumulative_net_pnl)s,%(reason)s,%(details)s)
        """, entry)


# ---------- strategy ----------
def entry_price_with_slip(price: float, side: str) -> float:
    return price * (1 + SLIPPAGE) if side == "LONG" else price * (1 - SLIPPAGE)


def exit_price_with_slip(price: float, side: str) -> float:
    return price * (1 - SLIPPAGE) if side == "LONG" else price * (1 + SLIPPAGE)


def score(asset: dict) -> Optional[Tuple[str, str, str]]:
    div = asset["divergence_pct"]
    funding = asset["funding"]
    rsi = asset["rsi14"]
    abs_div = abs(div)
    sig1 = None
    if abs_div > SIG1_DIVERGENCE_ENTRY:
        side = "SHORT" if div > 0 else "LONG"
        tier = "MAX" if abs_div > SIG1_DIVERGENCE_MAX else "HIGH"
        sig1 = (side, tier, f"div={div:+.3f}%")
    sig2 = None
    if funding > SIG2_FUNDING_POS:
        sig2 = ("SHORT", "MODERATE", f"funding={funding*100:+.4f}%")
    elif funding < SIG2_FUNDING_NEG:
        sig2 = ("LONG", "MODERATE", f"funding={funding*100:+.4f}%")
    cand = sig1 or sig2
    if cand and cand[0] == "LONG" and rsi < 30 and funding < 0:
        # RSI oversold + neg funding confirms
        return (cand[0], "MAX" if cand[1] in ("HIGH", "MAX") else "HIGH", cand[2] + " + RSI<30")
    if cand and cand[0] == "SHORT" and rsi > 70 and funding > 0:
        return (cand[0], "MAX" if cand[1] in ("HIGH", "MAX") else "HIGH", cand[2] + " + RSI>70")
    return cand


def margin_for(tier: str, balance: float) -> float:
    return balance * {"MAX": 0.22, "HIGH": 0.15, "MODERATE": 0.10}[tier]


def cycle(conn) -> None:
    state_load = load_state(conn)
    s = state_load["state"]
    positions = state_load["positions"]
    market = fetch_market()
    market_by_sym = {m["symbol"]: m for m in market}

    balance = float(s["balance"])
    realized_pnl = float(s["realized_pnl"])
    total_fees = float(s["total_fees"])
    turnover = float(s["cumulative_turnover"])
    trade_counter = int(s["trade_counter"])
    cumulative_net = realized_pnl

    # manage existing
    for pos in positions:
        if pos["remaining_frac"] is None or float(pos["remaining_frac"]) <= 0:
            continue
        sym = pos["symbol"]
        a = market_by_sym.get(sym)
        if not a:
            continue
        price = a["price"]
        div_now = a["divergence_pct"]
        abs_div = abs(div_now)
        side = pos["side"]
        held = (now_utc() - pos["entry_ts"]).total_seconds()
        stalled = (now_utc() - pos["last_progress_ts"]).total_seconds()

        def close_partial(frac: float, action: str, reason: str):
            nonlocal balance, realized_pnl, total_fees, cumulative_net
            ep = float(pos["entry_price"])
            notional_closed = float(pos["notional"]) * frac
            qty = notional_closed / ep
            exit_px = exit_price_with_slip(price, side)
            if side == "LONG":
                gross = qty * (exit_px - ep)
            else:
                gross = qty * (ep - exit_px)
            fees = notional_closed * TAKER_FEE
            net = gross - fees
            balance += net
            realized_pnl += net
            total_fees += fees
            cumulative_net = realized_pnl
            pos["remaining_frac"] = float(pos["remaining_frac"]) - frac
            pos["last_progress_ts"] = now_utc()
            if pos["remaining_frac"] <= 1e-6:
                pos["remaining_frac"] = 0
                pos["closed_at"] = now_utc()
            update_position(conn, {
                "trade_id": pos["trade_id"], "sl": pos["sl"],
                "tp1_done": pos["tp1_done"], "tp2_done": pos["tp2_done"],
                "remaining_frac": pos["remaining_frac"],
                "last_progress_ts": pos["last_progress_ts"],
                "closed_at": pos.get("closed_at"),
            })
            insert_log(conn, {
                "ts": now_utc(), "trade_id": pos["trade_id"], "symbol": sym,
                "side": side, "action": action, "entry_price": ep,
                "exit_price": exit_px, "frac": frac,
                "gross_pnl": round(gross, 6), "fees": round(fees, 6),
                "net_pnl": round(net, 6), "cumulative_net_pnl": round(cumulative_net, 6),
                "reason": reason, "details": json.dumps({"div_now": div_now}),
            })
            LOG.info("close %s %s %.0f%% %s net=%+.2f cum=%+.2f",
                     sym, side, frac*100, action, net, cumulative_net)

        # convergence
        if abs_div < CONVERGENCE_EXIT_THRESH and float(pos["remaining_frac"]) > 0:
            close_partial(float(pos["remaining_frac"]), "CONVERGENCE", "divergence closed")
            continue
        # time stop
        if held > MAX_HOLD_SEC and float(pos["remaining_frac"]) > 0:
            close_partial(float(pos["remaining_frac"]), "TIME_STOP", f"held {held:.0f}s")
            continue
        # stall
        if pos["tp1_done"] and not pos["tp2_done"] and stalled > STALL_SEC and float(pos["remaining_frac"]) > 0:
            close_partial(float(pos["remaining_frac"]), "STALL", f"stalled {stalled:.0f}s")
            continue
        # SL
        stop_hit = (side == "LONG" and price <= float(pos["sl"])) or (side == "SHORT" and price >= float(pos["sl"]))
        if stop_hit and float(pos["remaining_frac"]) > 0:
            close_partial(float(pos["remaining_frac"]), "SL", "stop hit")
            continue
        # TP1
        tp1_hit = (side == "LONG" and price >= float(pos["tp1"])) or (side == "SHORT" and price <= float(pos["tp1"]))
        if tp1_hit and not pos["tp1_done"]:
            close_partial(0.25, "TP1", "TP1")
            pos["tp1_done"] = True
            pos["sl"] = float(pos["entry_price"])
            update_position(conn, {
                "trade_id": pos["trade_id"], "sl": pos["sl"],
                "tp1_done": True, "tp2_done": pos["tp2_done"],
                "remaining_frac": pos["remaining_frac"],
                "last_progress_ts": pos["last_progress_ts"],
                "closed_at": None,
            })
        # TP2
        tp2_hit = (side == "LONG" and price >= float(pos["tp2"])) or (side == "SHORT" and price <= float(pos["tp2"]))
        if tp2_hit and not pos["tp2_done"]:
            close_partial(0.25, "TP2", "TP2")
            pos["tp2_done"] = True
            trail = float(pos["entry_price"]) * (1.005 if side == "LONG" else 0.995)
            pos["sl"] = max(pos["sl"], trail) if side == "LONG" else min(pos["sl"], trail)
            update_position(conn, {
                "trade_id": pos["trade_id"], "sl": pos["sl"],
                "tp1_done": pos["tp1_done"], "tp2_done": True,
                "remaining_frac": pos["remaining_frac"],
                "last_progress_ts": pos["last_progress_ts"],
                "closed_at": None,
            })
        # TP3
        tp3_hit = (side == "LONG" and price >= float(pos["tp3"])) or (side == "SHORT" and price <= float(pos["tp3"]))
        if tp3_hit and float(pos["remaining_frac"]) > 0:
            close_partial(float(pos["remaining_frac"]), "TP3", "TP3")
            continue

    # scan new
    open_count = sum(1 for p in positions if float(p["remaining_frac"]) > 0)
    open_slots = MAX_POSITIONS - open_count
    candidates = []
    for m in market:
        sig = score(m)
        if sig:
            candidates.append((m, sig))
    tier_rank = {"MAX": 3, "HIGH": 2, "MODERATE": 1}
    candidates.sort(key=lambda x: tier_rank.get(x[1][1], 0), reverse=True)

    if open_slots > 0 and candidates:
        for m, (side, tier, reason) in candidates:
            if open_slots <= 0:
                break
            sym = m["symbol"]
            if any(p["symbol"] == sym and float(p["remaining_frac"]) > 0 for p in positions):
                continue
            price = m["price"]
            entry = entry_price_with_slip(price, side)
            margin = margin_for(tier, balance)
            notional = margin * LEVERAGE_DEFAULT
            if side == "LONG":
                sl = entry * (1 - SL_PCT); tp1 = entry*(1+TP1_PCT); tp2 = entry*(1+TP2_PCT); tp3 = entry*(1+TP3_PCT)
            else:
                sl = entry * (1 + SL_PCT); tp1 = entry*(1-TP1_PCT); tp2 = entry*(1-TP2_PCT); tp3 = entry*(1-TP3_PCT)
            fee = notional * TAKER_FEE
            total_fees += fee
            balance -= fee
            turnover += notional
            trade_counter += 1
            ts = now_utc()
            pos = {
                "trade_id": trade_counter, "symbol": sym, "side": side,
                "entry_price": entry, "entry_ts": ts,
                "margin": margin, "leverage": LEVERAGE_DEFAULT, "notional": notional,
                "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "last_progress_ts": ts,
                "divergence_at_entry": m["divergence_pct"],
                "funding_at_entry": m["funding"],
                "reason": reason,
            }
            insert_position(conn, pos)
            insert_log(conn, {
                "ts": ts, "trade_id": trade_counter, "symbol": sym, "side": side,
                "action": "OPEN", "entry_price": entry, "exit_price": None,
                "frac": 1.0, "gross_pnl": None, "fees": fee, "net_pnl": None,
                "cumulative_net_pnl": realized_pnl, "reason": reason,
                "details": json.dumps({"tier": tier, "margin": margin, "notional": notional,
                                       "divergence_pct": m["divergence_pct"],
                                       "funding": m["funding"]}),
            })
            open_slots -= 1
            LOG.info("OPEN %s %s [%s] %s margin=%.2f notional=%.2f entry=%.4f",
                     sym, side, tier, reason, margin, notional, entry)
    elif not candidates:
        insert_log(conn, {
            "ts": now_utc(), "trade_id": 0, "symbol": "-", "side": "-",
            "action": "DECISION", "entry_price": None, "exit_price": None,
            "frac": None, "gross_pnl": None, "fees": None, "net_pnl": None,
            "cumulative_net_pnl": realized_pnl, "reason": "no signal",
            "details": json.dumps({"market": market}),
        })
        LOG.info("no signal. candidates=%s", [(m['symbol'], m['divergence_pct'], m['funding']) for m in market])

    save_state_row(conn, {
        "balance": balance, "realized_pnl": realized_pnl,
        "total_fees": total_fees, "cumulative_turnover": turnover,
        "trade_counter": trade_counter,
    })
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    conn = connect_company()
    ensure_schema(conn)

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

    LOG.info("surgeon2 trader started. interval=%ss", args.interval)
    if args.once:
        cycle(conn)
        return

    while not stop["flag"]:
        try:
            cycle(conn)
        except Exception as exc:
            LOG.exception("cycle failed: %s", exc)
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            conn = connect_company()
            ensure_schema(conn)
        for _ in range(args.interval):
            if stop["flag"]:
                break
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
