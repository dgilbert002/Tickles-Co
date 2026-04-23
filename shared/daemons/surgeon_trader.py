"""Twilly-faithful Surgeon paper trader (flat-file mode).

Implements the SOUL.md strategy literally:
- Reads TRADE_STATE.md (balance + open positions)
- Reads MARKET_STATE.json + MARKET_INDICATORS.json (scanner output)
- Scans for mark/index divergence + extreme funding signals
- Manages open positions (SL, TP1/2/3, convergence, time-stop, stall)
- Writes TRADE_STATE.md (overwrite) + TRADE_LOG.md (append-only)

Paper trading only. No real orders. Does NOT call any exchange API for execution.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import signal
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s surgeon %(message)s")
LOG = logging.getLogger("surgeon.trader")

STARTING_BALANCE = 10_000.0
TAKER_FEE = 0.0005     # 0.05% per side
SLIPPAGE = 0.0002      # 0.02% per side
LEVERAGE_DEFAULT = 25
MAX_POSITIONS = 3

SIG1_DIVERGENCE_ENTRY = 0.15  # % absolute
SIG1_DIVERGENCE_MAX = 0.30    # maximum conviction
SIG2_FUNDING_POS = 0.0005     # 0.05% per 8h
SIG2_FUNDING_NEG = -0.0005

SL_PCT = 0.005   # 0.5%
TP1_PCT = 0.010  # 1.0%
TP2_PCT = 0.020
TP3_PCT = 0.040
CONVERGENCE_EXIT_THRESH = 0.03  # % (abs divergence)
MAX_HOLD_SEC = 45 * 60
STALL_SEC = 15 * 60

STATE_PATH = "TRADE_STATE.md"
LOG_PATH = "TRADE_LOG.md"
STATE_JSON = ".surgeon_state.json"  # machine-readable sidecar (authoritative)


@dataclass
class Position:
    trade_id: int
    symbol: str
    side: str  # "LONG" / "SHORT"
    entry_price: float
    entry_ts: str
    margin: float
    leverage: int
    notional: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    tp1_done: bool = False
    tp2_done: bool = False
    remaining_frac: float = 1.0  # fraction still open
    last_progress_ts: str = ""
    divergence_at_entry: float = 0.0
    funding_at_entry: float = 0.0
    reason: str = ""


@dataclass
class State:
    starting_balance: float = STARTING_BALANCE
    balance: float = STARTING_BALANCE
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    cumulative_turnover: float = 0.0
    trade_counter: int = 0
    positions: List[Position] = field(default_factory=list)
    closed_trades: List[dict] = field(default_factory=list)

    @staticmethod
    def load(path: str) -> "State":
        if not os.path.exists(path):
            return State()
        with open(path, "r") as f:
            raw = json.load(f)
        s = State(
            starting_balance=raw.get("starting_balance", STARTING_BALANCE),
            balance=raw.get("balance", STARTING_BALANCE),
            realized_pnl=raw.get("realized_pnl", 0.0),
            total_fees=raw.get("total_fees", 0.0),
            cumulative_turnover=raw.get("cumulative_turnover", 0.0),
            trade_counter=raw.get("trade_counter", 0),
            positions=[Position(**p) for p in raw.get("positions", [])],
            closed_trades=raw.get("closed_trades", []),
        )
        return s

    def save(self, path: str) -> None:
        raw = {
            "starting_balance": self.starting_balance,
            "balance": self.balance,
            "realized_pnl": self.realized_pnl,
            "total_fees": self.total_fees,
            "cumulative_turnover": self.cumulative_turnover,
            "trade_counter": self.trade_counter,
            "positions": [asdict(p) for p in self.positions],
            "closed_trades": self.closed_trades[-500:],  # keep last 500
        }
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(raw, f, indent=2)
        os.replace(tmp, path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_market(ws: str) -> Tuple[Optional[dict], Optional[dict]]:
    try:
        with open(os.path.join(ws, "MARKET_STATE.json")) as f:
            ms = json.load(f)
        with open(os.path.join(ws, "MARKET_INDICATORS.json")) as f:
            mi = json.load(f)
        return ms, mi
    except Exception as exc:
        LOG.warning("market data unavailable: %s", exc)
        return None, None


def score_signal(asset: dict, ind: dict) -> Optional[Tuple[str, float, str]]:
    """Return (side, conviction, reason) or None.
    Conviction tier: 'MAX', 'HIGH', 'MODERATE'.
    """
    div = asset.get("divergencePct", 0.0)
    funding = asset.get("fundingRate", 0.0)
    rsi = ind.get("rsi14", 50.0)
    abs_div = abs(div)

    # Signal 1: divergence (primary)
    sig1 = None
    if abs_div > SIG1_DIVERGENCE_ENTRY:
        side = "SHORT" if div > 0 else "LONG"
        tier = "MAX" if abs_div > SIG1_DIVERGENCE_MAX else "HIGH"
        sig1 = (side, tier, f"div={div:+.3f}%")

    # Signal 2: extreme funding (standalone)
    sig2 = None
    if funding > SIG2_FUNDING_POS:
        sig2 = ("SHORT", "MODERATE", f"funding={funding*100:+.4f}% per 8h (longs crowded)")
    elif funding < SIG2_FUNDING_NEG:
        sig2 = ("LONG", "MODERATE", f"funding={funding*100:+.4f}% per 8h (shorts crowded)")

    # Signal 3: tech confirmation scales size
    conf_bonus = ""
    if sig1 or sig2:
        cand = sig1 or sig2
        side = cand[0]
        if side == "LONG" and rsi < 30 and funding < 0:
            conf_bonus = " + RSI oversold + neg funding"
            if cand[1] == "HIGH":
                cand = (side, "MAX", cand[2] + conf_bonus)
            elif cand[1] == "MODERATE":
                cand = (side, "HIGH", cand[2] + conf_bonus)
        elif side == "SHORT" and rsi > 70 and funding > 0:
            conf_bonus = " + RSI overbought + pos funding"
            if cand[1] == "HIGH":
                cand = (side, "MAX", cand[2] + conf_bonus)
            elif cand[1] == "MODERATE":
                cand = (side, "HIGH", cand[2] + conf_bonus)
        return cand
    return None


def margin_for_tier(tier: str, balance: float) -> float:
    if tier == "MAX":
        return balance * 0.22
    if tier == "HIGH":
        return balance * 0.15
    return balance * 0.10  # MODERATE


def entry_price_with_slip(price: float, side: str) -> float:
    # taker into market: LONG pays up, SHORT gets sold into book at down slip
    if side == "LONG":
        return price * (1 + SLIPPAGE)
    return price * (1 - SLIPPAGE)


def exit_price_with_slip(price: float, side: str) -> float:
    # closing: LONG sells into book, SHORT buys into book
    if side == "LONG":
        return price * (1 - SLIPPAGE)
    return price * (1 + SLIPPAGE)


def pnl(position: Position, exit_price: float, frac: float) -> Tuple[float, float]:
    """Return (gross_pnl, fees) for closing `frac` of the position at exit_price."""
    notional_closed = position.notional * frac
    qty = notional_closed / position.entry_price
    if position.side == "LONG":
        gross = qty * (exit_price - position.entry_price)
    else:
        gross = qty * (position.entry_price - exit_price)
    fees = notional_closed * TAKER_FEE  # one side; entry fee already counted elsewhere
    return gross, fees


def open_position(state: State, asset_sym: str, price: float, side: str,
                  tier: str, reason: str, div: float, funding: float) -> Position:
    state.trade_counter += 1
    entry = entry_price_with_slip(price, side)
    margin = margin_for_tier(tier, state.balance)
    leverage = LEVERAGE_DEFAULT
    notional = margin * leverage

    if side == "LONG":
        sl = entry * (1 - SL_PCT)
        tp1 = entry * (1 + TP1_PCT)
        tp2 = entry * (1 + TP2_PCT)
        tp3 = entry * (1 + TP3_PCT)
    else:
        sl = entry * (1 + SL_PCT)
        tp1 = entry * (1 - TP1_PCT)
        tp2 = entry * (1 - TP2_PCT)
        tp3 = entry * (1 - TP3_PCT)

    entry_fee = notional * TAKER_FEE
    state.total_fees += entry_fee
    state.balance -= entry_fee  # pay entry fee immediately
    state.cumulative_turnover += notional

    ts = now_iso()
    pos = Position(
        trade_id=state.trade_counter,
        symbol=asset_sym,
        side=side,
        entry_price=entry,
        entry_ts=ts,
        margin=margin,
        leverage=leverage,
        notional=notional,
        sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
        last_progress_ts=ts,
        divergence_at_entry=div,
        funding_at_entry=funding,
        reason=reason,
    )
    state.positions.append(pos)
    return pos


def close_partial(state: State, pos: Position, price: float, frac: float, reason: str) -> dict:
    exit_px = exit_price_with_slip(price, pos.side)
    gross, fees = pnl(pos, exit_px, frac)
    state.total_fees += fees
    net = gross - fees
    state.balance += net
    state.realized_pnl += net
    pos.remaining_frac -= frac
    pos.last_progress_ts = now_iso()
    closed_entry = {
        "ts": now_iso(),
        "trade_id": pos.trade_id,
        "symbol": pos.symbol,
        "side": pos.side,
        "frac": frac,
        "entry_price": pos.entry_price,
        "exit_price": exit_px,
        "gross_pnl": round(gross, 4),
        "fees": round(fees, 4),
        "net_pnl": round(net, 4),
        "reason": reason,
        "cumulative_net_pnl": round(state.realized_pnl, 4),
    }
    state.closed_trades.append(closed_entry)
    return closed_entry


def manage_position(state: State, pos: Position, ms_asset: dict,
                    log_lines: List[str]) -> Optional[Position]:
    """Apply stops/TPs/convergence/time-stop. Returns the position if still open, else None."""
    price = ms_asset.get("price", 0.0)
    div_now = ms_asset.get("divergencePct", 0.0)
    abs_div = abs(div_now)

    # convergence exit
    if abs_div < CONVERGENCE_EXIT_THRESH and pos.remaining_frac > 0:
        entry = close_partial(state, pos, price, pos.remaining_frac, "CONVERGENCE")
        log_lines.append(format_log_entry(pos, entry, convergence=True))
        pos.remaining_frac = 0.0
        return None

    # time stop
    entry_dt = datetime.fromisoformat(pos.entry_ts.replace("Z", "+00:00"))
    held = (datetime.now(timezone.utc) - entry_dt).total_seconds()
    if held > MAX_HOLD_SEC and pos.remaining_frac > 0:
        entry = close_partial(state, pos, price, pos.remaining_frac, "TIME_STOP")
        log_lines.append(format_log_entry(pos, entry, time_stop=True))
        pos.remaining_frac = 0.0
        return None

    # stall exit (between TPs)
    last_prog = datetime.fromisoformat(pos.last_progress_ts.replace("Z", "+00:00"))
    stalled = (datetime.now(timezone.utc) - last_prog).total_seconds()
    if pos.tp1_done and not pos.tp2_done and stalled > STALL_SEC and pos.remaining_frac > 0:
        entry = close_partial(state, pos, price, pos.remaining_frac, "STALL")
        log_lines.append(format_log_entry(pos, entry, stall=True))
        pos.remaining_frac = 0.0
        return None

    # SL hit (full close)
    stop_hit = (pos.side == "LONG" and price <= pos.sl) or (pos.side == "SHORT" and price >= pos.sl)
    if stop_hit and pos.remaining_frac > 0:
        entry = close_partial(state, pos, price, pos.remaining_frac, "SL")
        log_lines.append(format_log_entry(pos, entry, sl=True))
        pos.remaining_frac = 0.0
        return None

    # TP1
    tp1_hit = (pos.side == "LONG" and price >= pos.tp1) or (pos.side == "SHORT" and price <= pos.tp1)
    if tp1_hit and not pos.tp1_done and pos.remaining_frac > 0:
        entry = close_partial(state, pos, price, 0.25, "TP1")
        pos.tp1_done = True
        pos.sl = pos.entry_price  # breakeven stop
        log_lines.append(format_log_entry(pos, entry, tp="TP1"))
    # TP2
    tp2_hit = (pos.side == "LONG" and price >= pos.tp2) or (pos.side == "SHORT" and price <= pos.tp2)
    if tp2_hit and not pos.tp2_done and pos.remaining_frac > 0:
        entry = close_partial(state, pos, price, 0.25, "TP2")
        pos.tp2_done = True
        # trail stop at +0.5% above entry
        if pos.side == "LONG":
            pos.sl = max(pos.sl, pos.entry_price * (1 + 0.005))
        else:
            pos.sl = min(pos.sl, pos.entry_price * (1 - 0.005))
        log_lines.append(format_log_entry(pos, entry, tp="TP2"))
    # TP3 (remaining)
    tp3_hit = (pos.side == "LONG" and price >= pos.tp3) or (pos.side == "SHORT" and price <= pos.tp3)
    if tp3_hit and pos.remaining_frac > 0:
        entry = close_partial(state, pos, price, pos.remaining_frac, "TP3")
        log_lines.append(format_log_entry(pos, entry, tp="TP3"))
        pos.remaining_frac = 0.0
        return None

    return pos if pos.remaining_frac > 0 else None


def format_log_entry(pos: Position, closed: dict, *, tp: str = "", sl: bool = False,
                     convergence: bool = False, time_stop: bool = False, stall: bool = False) -> str:
    tag = "TP:" + tp if tp else ("SL" if sl else ("CONVERGENCE" if convergence else ("TIME_STOP" if time_stop else ("STALL" if stall else "EXIT"))))
    lines = [
        f"Trade #{pos.trade_id} -- {pos.symbol} {pos.side} [{tag}]",
        f"- Time: {closed['ts']} | Divergence@entry: {pos.divergence_at_entry:+.3f}% | Funding@entry: {pos.funding_at_entry*100:+.4f}%",
        f"- Entry: ${pos.entry_price:.4f} | Exit: ${closed['exit_price']:.4f} | Margin: ${pos.margin:.2f} | Leverage: {pos.leverage}x | Notional: ${pos.notional:.2f}",
        f"- Stop: ${pos.sl:.4f} | TP1/2/3: ${pos.tp1:.4f}/${pos.tp2:.4f}/${pos.tp3:.4f}",
        f"- Gross P&L: {closed['gross_pnl']:+.2f}",
        f"- Est. Fees: -{closed['fees']:.2f} ({TAKER_FEE*100:.2f}% x ${pos.notional*closed['frac']:.2f})",
        f"- Net P&L: {closed['net_pnl']:+.2f}",
        f"- Cumulative Net P&L: {closed['cumulative_net_pnl']:+.2f}",
        f"- Reason: {pos.reason}",
        "",
    ]
    return "\n".join(lines)


def format_decision_entry(ts: str, candidates: List[Tuple[str, str, str, str]]) -> str:
    """Candidate tuples: (symbol, side, conviction, reason)."""
    lines = [f"Decision @ {ts} — NO TRADE (top candidates):"]
    for sym, side, conv, reason in candidates[:3]:
        lines.append(f"  - {sym} {side} [{conv}]: {reason}")
    lines.append("")
    return "\n".join(lines)


def write_trade_state(ws: str, state: State) -> None:
    path = os.path.join(ws, STATE_PATH)
    net = state.balance - state.starting_balance
    lines = [
        "# TRADE_STATE — rubicon_surgeon",
        "",
        f"Last-updated: {now_iso()}",
        "Mode: PAPER_TRADING",
        f"Starting Balance: ${state.starting_balance:,.2f}",
        f"Current Balance:  ${state.balance:,.2f}",
        f"Realized P&L:     ${state.realized_pnl:+,.2f}",
        f"Net vs Starting:  ${net:+,.2f}",
        f"Total Estimated Fees: ${state.total_fees:,.2f}",
        f"Cumulative Turnover: ${state.cumulative_turnover:,.2f}",
        f"Trades opened: {state.trade_counter}  Closed: {len(state.closed_trades)}",
        "",
        "## Open Positions",
    ]
    if not state.positions:
        lines.append("(none)")
    else:
        for p in state.positions:
            if p.remaining_frac <= 0:
                continue
            lines.append(
                f"- #{p.trade_id} {p.symbol} {p.side} | entry ${p.entry_price:.4f} | "
                f"margin ${p.margin:.2f} | lev {p.leverage}x | rem {p.remaining_frac*100:.0f}% | "
                f"SL ${p.sl:.4f} | TP1/2/3 ${p.tp1:.4f}/${p.tp2:.4f}/${p.tp3:.4f} | "
                f"opened {p.entry_ts} | reason: {p.reason}"
            )
    lines.append("")
    lines.append("## Closed Positions (last 5)")
    if not state.closed_trades:
        lines.append("(none)")
    else:
        for c in state.closed_trades[-5:]:
            lines.append(
                f"- #{c['trade_id']} {c['symbol']} {c['side']} | {c['reason']} | "
                f"net {c['net_pnl']:+.2f} | exit ${c['exit_price']:.4f} @ {c['ts']}"
            )
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def append_trade_log(ws: str, chunks: List[str]) -> None:
    if not chunks:
        return
    path = os.path.join(ws, LOG_PATH)
    with open(path, "a") as f:
        for c in chunks:
            f.write(c)
            if not c.endswith("\n"):
                f.write("\n")


def cycle(state: State, ws: str, dry: bool = False) -> None:
    ms, mi = read_market(ws)
    if not ms or not mi:
        LOG.warning("no market data; skipping cycle")
        return
    assets_ms = ms.get("assets", {})
    assets_mi = mi.get("assets", {})
    log_chunks: List[str] = []

    # 1) Manage open positions
    still_open: List[Position] = []
    for pos in state.positions:
        if pos.remaining_frac <= 0:
            continue
        a = assets_ms.get(pos.symbol)
        if not a:
            still_open.append(pos)
            continue
        out = manage_position(state, pos, a, log_chunks)
        if out is not None:
            still_open.append(out)
    state.positions = still_open

    # 2) If slots available, scan candidates
    candidates: List[Tuple[str, str, str, str, float, float, float]] = []
    # (sym, side, tier, reason, price, div, funding)
    for sym, a in assets_ms.items():
        ind = assets_mi.get(sym, {})
        sig = score_signal(a, ind)
        if sig:
            side, tier, reason = sig
            candidates.append((sym, side, tier, reason, a.get("price", 0.0),
                               a.get("divergencePct", 0.0), a.get("fundingRate", 0.0)))

    # sort by conviction
    tier_rank = {"MAX": 3, "HIGH": 2, "MODERATE": 1}
    candidates.sort(key=lambda c: tier_rank.get(c[2], 0), reverse=True)

    open_slots = MAX_POSITIONS - len([p for p in state.positions if p.remaining_frac > 0])
    if open_slots > 0 and candidates:
        taken = 0
        for sym, side, tier, reason, price, div, funding in candidates:
            # don't re-enter an existing symbol
            if any(p.symbol == sym and p.remaining_frac > 0 for p in state.positions):
                continue
            if taken >= open_slots:
                break
            pos = open_position(state, sym, price, side, tier, reason, div, funding)
            taken += 1
            log_chunks.append(format_open_entry(pos))
    elif not candidates:
        cand_display = [(a.get("symbol", sym), "FLAT", "NO_SIGNAL",
                          f"div={a.get('divergencePct', 0):+.3f}% funding={a.get('fundingRate',0)*100:+.4f}%")
                         for sym, a in assets_ms.items()]
        log_chunks.append(format_decision_entry(now_iso(), cand_display))

    # 3) Persist state
    if not dry:
        write_trade_state(ws, state)
        append_trade_log(ws, log_chunks)
        state.save(os.path.join(ws, STATE_JSON))
        LOG.info("cycle done. balance=$%.2f open=%d closed_total=%d",
                 state.balance, len([p for p in state.positions if p.remaining_frac > 0]),
                 len(state.closed_trades))
    else:
        LOG.info("DRY cycle done")


def format_open_entry(pos: Position) -> str:
    lines = [
        f"Trade #{pos.trade_id} -- {pos.symbol} {pos.side} [OPEN]",
        f"- Time: {pos.entry_ts} | Divergence@entry: {pos.divergence_at_entry:+.3f}% | Funding@entry: {pos.funding_at_entry*100:+.4f}%",
        f"- Entry: ${pos.entry_price:.4f} | Margin: ${pos.margin:.2f} | Leverage: {pos.leverage}x | Notional: ${pos.notional:.2f}",
        f"- Stop: ${pos.sl:.4f} | TP1/TP2/TP3: ${pos.tp1:.4f}/${pos.tp2:.4f}/${pos.tp3:.4f}",
        f"- Reason: {pos.reason}",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="/root/.openclaw/workspace/rubicon_surgeon")
    parser.add_argument("--interval", type=int, default=300, help="seconds between cycles (default 5 min)")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args()

    ws = args.workspace
    os.makedirs(ws, exist_ok=True)
    state_json_path = os.path.join(ws, STATE_JSON)
    state = State.load(state_json_path)

    # seed TRADE_LOG header if missing
    log_path = os.path.join(ws, LOG_PATH)
    if not os.path.exists(log_path):
        with open(log_path, "w") as f:
            f.write("# TRADE_LOG — rubicon_surgeon\nMode: PAPER_TRADING\nAppend-only.\n\n")

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

    LOG.info("surgeon trader started. ws=%s interval=%ss", ws, args.interval)

    if args.once:
        cycle(state, ws, dry=args.dry)
        return

    while not stop["flag"]:
        try:
            cycle(state, ws, dry=args.dry)
        except Exception as exc:
            LOG.exception("cycle failed: %s", exc)
        for _ in range(args.interval):
            if stop["flag"]:
                break
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
