"""Twilly-faithful Surgeon paper trader (mem0 mode).

Implements the SOUL.md strategy literally:
- Reads .surgeon_state.json (authoritative balance + open positions)
- Reads MARKET_STATE.json + MARKET_INDICATORS.json (scanner output)
- Scans for mark/index divergence + extreme funding signals
- Manages open positions (SL, TP1/2/3, convergence, time-stop, stall)
- Persists state to .surgeon_state.json and writes trade narratives to mem0
  (type=trade_decision / type=trade_state_snapshot). NO .md output.

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

# Add /opt/tickles to sys.path to ensure shared imports work
sys.path.append("/opt/tickles")
from shared.utils.freshness import validate_freshness, StaleDataError
from shared.utils.mem0_config import ScopedMemory

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

STATE_JSON = ".surgeon_state.json"  # machine-readable state (authoritative)
DEFAULT_COMPANY = "rubicon"
DEFAULT_AGENT_ID = "surgeon1"


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
    """Read market data from JSON files with Freshness Guard."""
    try:
        ms_path = os.path.join(ws, "MARKET_STATE.json")
        mi_path = os.path.join(ws, "MARKET_INDICATORS.json")
        
        if not os.path.exists(ms_path) or not os.path.exists(mi_path):
            return None, None

        with open(ms_path) as f:
            ms = json.load(f)
        with open(mi_path) as f:
            mi = json.load(f)
            
        # Validate freshness of the scanner output (default 180s)
        # The scanner writes a 'timestamp' field in ISO format.
        ts = ms.get("timestamp")
        validate_freshness(ts, context="scanner:MARKET_STATE")
        
        return ms, mi
    except StaleDataError as e:
        LOG.error("STALE DATA from scanner: %s", e)
        return None, None
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


def close_partial(state: State, pos: Position, price: float, frac: float, reason: str,
                  memory: Optional[ScopedMemory] = None) -> dict:
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

    # --- Learning Loop: Post-Trade Autopsy ---
    if memory and pos.remaining_frac <= 0:
        try:
            autopsy = (
                f"Trade Autopsy: {pos.symbol} {pos.side} closed via {reason}. "
                f"Net PnL: {net:+.2f}. Entry: {pos.entry_price:.4f}, Exit: {exit_px:.4f}. "
                f"Divergence at entry: {pos.divergence_at_entry:.3f}%. "
                f"Funding at entry: {pos.funding_at_entry:.4f}."
            )
            memory.add(autopsy, user_id="rubicon", agent_id="surgeon1", metadata={"type": "autopsy", "symbol": pos.symbol})
            LOG.info("recorded autopsy to mem0 for trade #%s", pos.trade_id)
        except Exception as e:
            LOG.warning("failed to record autopsy: %s", e)

    return closed_entry


def manage_position(state: State, pos: Position, ms_asset: dict,
                    log_lines: List[str], memory: Optional[ScopedMemory] = None) -> Optional[Position]:
    """Apply stops/TPs/convergence/time-stop. Returns the position if still open, else None."""
    price = ms_asset.get("price", 0.0)
    div_now = ms_asset.get("divergencePct", 0.0)
    abs_div = abs(div_now)

    # convergence exit
    if abs_div < CONVERGENCE_EXIT_THRESH and pos.remaining_frac > 0:
        entry = close_partial(state, pos, price, pos.remaining_frac, "CONVERGENCE", memory=memory)
        log_lines.append(format_log_entry(pos, entry, convergence=True))
        pos.remaining_frac = 0.0
        return None

    # time stop
    entry_dt = datetime.fromisoformat(pos.entry_ts.replace("Z", "+00:00"))
    held = (datetime.now(timezone.utc) - entry_dt).total_seconds()
    if held > MAX_HOLD_SEC and pos.remaining_frac > 0:
        entry = close_partial(state, pos, price, pos.remaining_frac, "TIME_STOP", memory=memory)
        log_lines.append(format_log_entry(pos, entry, time_stop=True))
        pos.remaining_frac = 0.0
        return None

    # stall exit (between TPs)
    last_prog = datetime.fromisoformat(pos.last_progress_ts.replace("Z", "+00:00"))
    stalled = (datetime.now(timezone.utc) - last_prog).total_seconds()
    if pos.tp1_done and not pos.tp2_done and stalled > STALL_SEC and pos.remaining_frac > 0:
        entry = close_partial(state, pos, price, pos.remaining_frac, "STALL", memory=memory)
        log_lines.append(format_log_entry(pos, entry, stall=True))
        pos.remaining_frac = 0.0
        return None

    # SL hit (full close)
    stop_hit = (pos.side == "LONG" and price <= pos.sl) or (pos.side == "SHORT" and price >= pos.sl)
    if stop_hit and pos.remaining_frac > 0:
        entry = close_partial(state, pos, price, pos.remaining_frac, "SL", memory=memory)
        log_lines.append(format_log_entry(pos, entry, sl=True))
        pos.remaining_frac = 0.0
        return None

    # TP1
    tp1_hit = (pos.side == "LONG" and price >= pos.tp1) or (pos.side == "SHORT" and price <= pos.tp1)
    if tp1_hit and not pos.tp1_done and pos.remaining_frac > 0:
        entry = close_partial(state, pos, price, 0.25, "TP1", memory=memory)
        pos.tp1_done = True
        pos.sl = pos.entry_price  # breakeven stop
        log_lines.append(format_log_entry(pos, entry, tp="TP1"))
    # TP2
    tp2_hit = (pos.side == "LONG" and price >= pos.tp2) or (pos.side == "SHORT" and price <= pos.tp2)
    if tp2_hit and not pos.tp2_done and pos.remaining_frac > 0:
        entry = close_partial(state, pos, price, 0.25, "TP2", memory=memory)
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
        entry = close_partial(state, pos, price, pos.remaining_frac, "TP3", memory=memory)
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


def build_state_snapshot(state: State, company: str) -> str:
    """Render a human-readable state snapshot for mem0.

    The narrative is content-equivalent to the deprecated TRADE_STATE.md but
    is written into mem0 as a `trade_state_snapshot` entry rather than to
    disk.

    Args:
        state: Current trader state.
        company: Company slug (for the header).

    Returns:
        Multi-line string snapshot.
    """
    net = state.balance - state.starting_balance
    lines = [
        f"# TRADE_STATE — {company}_surgeon",
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
    return "\n".join(lines) + "\n"


def persist_state_snapshot(
    state: State, company: str, memory: Optional[ScopedMemory]
) -> None:
    """Write the latest state snapshot into mem0 (overwrite-latest semantics).

    Args:
        state: Current trader state.
        company: Company slug (used in the prose header).
        memory: ScopedMemory instance, or None to skip the write.
    """
    if memory is None:
        return
    snapshot = build_state_snapshot(state, company)
    try:
        memory.add(
            snapshot,
            metadata={
                "type": "trade_state_snapshot",
                "kind": "live",
                "original_timestamp": now_iso(),
                "balance": float(state.balance),
                "open_positions": len(
                    [p for p in state.positions if p.remaining_frac > 0]
                ),
            },
        )
    except Exception as exc:
        LOG.warning("failed to persist trade_state_snapshot to mem0: %s", exc)


def persist_trade_decisions(
    chunks: List[str], memory: Optional[ScopedMemory]
) -> None:
    """Write each trade-decision narrative into mem0.

    Args:
        chunks: List of pre-formatted Twilly-style entries (from
                format_log_entry / format_open_entry / format_decision_entry).
        memory: ScopedMemory instance, or None to skip the write.
    """
    if not chunks or memory is None:
        return
    for chunk in chunks:
        text = chunk.rstrip("\n")
        if not text:
            continue
        action_match = re.search(r"\[(?P<tag>[A-Z0-9_:]+)\]", text)
        action = action_match.group("tag").lower() if action_match else "decision"
        trade_match = re.search(r"Trade #(\d+)", text)
        trade_id: Optional[int] = (
            int(trade_match.group(1)) if trade_match else None
        )
        symbol_match = re.search(r"Trade #\d+ -- (\S+)", text)
        symbol: Optional[str] = (
            symbol_match.group(1) if symbol_match else None
        )
        try:
            memory.add(
                text,
                metadata={
                    "type": "trade_decision",
                    "kind": "live",
                    "action": action,
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "original_timestamp": now_iso(),
                },
            )
        except Exception as exc:
            LOG.warning("failed to persist trade_decision to mem0: %s", exc)


def cycle(
    state: State,
    ws: str,
    dry: bool = False,
    memory: Optional[ScopedMemory] = None,
    company: str = DEFAULT_COMPANY,
    agent_id: str = DEFAULT_AGENT_ID,
) -> None:
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
        out = manage_position(state, pos, a, log_chunks, memory=memory)
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

            # --- Learning Loop: Pre-Trade Memory Check ---
            if memory:
                try:
                    learnings = memory.search(
                        "recent trading learnings",
                        limit=3,
                        user_id=company,
                        agent_id=agent_id,
                    )
                    if learnings:
                        LOG.info("PRE-TRADE LEARNINGS for %s: %s", sym, json.dumps(learnings))
                except Exception as e:
                    LOG.warning("failed to query memory: %s", e)

            pos = open_position(state, sym, price, side, tier, reason, div, funding)
            taken += 1
            log_chunks.append(format_open_entry(pos))
    elif not candidates:
        cand_display = [(a.get("symbol", sym), "FLAT", "NO_SIGNAL",
                          f"div={a.get('divergencePct', 0):+.3f}% funding={a.get('fundingRate',0)*100:+.4f}%")
                         for sym, a in assets_ms.items()]
        log_chunks.append(format_decision_entry(now_iso(), cand_display))

    # 3) Persist state — JSON (authoritative on disk) + mem0 (narrative log).
    if not dry:
        state.save(os.path.join(ws, STATE_JSON))
        persist_state_snapshot(state, company, memory)
        persist_trade_decisions(log_chunks, memory)
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
    """CLI entry point for the surgeon paper trader."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="/root/.openclaw/workspace/rubicon_surgeon")
    parser.add_argument("--interval", type=int, default=900, help="seconds between cycles (default 15 min)")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--dry", action="store_true")
    parser.add_argument(
        "--company",
        default=os.environ.get("COMPANY_ID", DEFAULT_COMPANY),
        help="Company slug for mem0 scoping (default from $COMPANY_ID or rubicon).",
    )
    parser.add_argument(
        "--agent-id",
        default=os.environ.get("AGENT_ID", DEFAULT_AGENT_ID),
        help="Agent id for mem0 scoping (default from $AGENT_ID or surgeon1).",
    )
    args = parser.parse_args()

    ws = args.workspace
    os.makedirs(ws, exist_ok=True)
    state_json_path = os.path.join(ws, STATE_JSON)
    state = State.load(state_json_path)

    # Initialize ScopedMemory (Tier-1/2). Trade narratives now flow into mem0
    # exclusively — no .md writes.
    memory: Optional[ScopedMemory] = None
    try:
        memory = ScopedMemory(company=args.company, agent_id=args.agent_id)
        LOG.info(
            "ScopedMemory initialized for company=%s agent_id=%s",
            args.company,
            args.agent_id,
        )
    except Exception as e:
        LOG.warning("ScopedMemory initialization failed: %s", e)

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

    LOG.info(
        "surgeon trader started. ws=%s interval=%ss company=%s agent_id=%s",
        ws,
        args.interval,
        args.company,
        args.agent_id,
    )

    if args.once:
        cycle(
            state,
            ws,
            dry=args.dry,
            memory=memory,
            company=args.company,
            agent_id=args.agent_id,
        )
        return

    while not stop["flag"]:
        try:
            cycle(
                state,
                ws,
                dry=args.dry,
                memory=memory,
                company=args.company,
                agent_id=args.agent_id,
            )
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
