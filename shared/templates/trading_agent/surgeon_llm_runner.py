"""Twilly-faithful LLM Surgeon runner (reusable template, mem0 mode).

Every cycle:
  1. Reads SOUL.md (strategy + persona), recent trade decisions and the latest
     state snapshot from mem0, MARKET_STATE.json, MARKET_INDICATORS.json,
     (optional) EXECUTION_REALITY.md.
  2. Sends everything to an LLM via OpenRouter.
  3. LLM returns a strict JSON decision block.
  4. Python applies the decisions deterministically, persists the new state
     snapshot and per-decision entries to mem0, and updates the local
     ``.surgeon_state.json`` sidecar.

Designed to be copy-pasted per agent. Config lives in config.json next to this
script, or via CLI flags.

Why this design (Twilly-faithful):
  - The LLM does the reasoning / signal scoring / decision selection.
  - Python enforces math (fees, slippage, P&L, SL/TP hit detection) so the LLM
    cannot break accounting.

Notes:
  - No ``TRADE_STATE.md`` / ``TRADE_LOG.md`` files are written. All durable
    history lives in mem0 (Qdrant + LLM-derived facts) per company namespace.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Add /opt/tickles to sys.path to ensure shared imports work
sys.path.append("/opt/tickles")
from shared.utils.freshness import validate_freshness, StaleDataError
from shared.utils.mem0_config import ScopedMemory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s surgeon_llm %(message)s")
LOG = logging.getLogger("surgeon_llm.runner")

# ---------- config defaults (override via config.json or CLI) ----------
DEFAULT_CONFIG = {
    "agent_name": "surgeon",
    "workspace": "/root/.openclaw/workspace/surgeon",
    "mode": "PAPER_TRADING",
    "starting_balance": 10000.0,
    "leverage": 25,
    "taker_fee": 0.0005,       # 0.05% per side
    "slippage": 0.0002,        # 0.02% per side
    "max_positions": 3,
    "model_primary": "anthropic/claude-sonnet-4.5",
    "model_fallback": "openai/gpt-4.1",
    "model_budget_usd_day": 5.0,     # soft cap (warning log above this)
    "interval_sec": 300,
    "log_tail_bytes": 4000,
    "max_output_tokens": 2000,
    "temperature": 0.2,
    "openrouter_timeout_sec": 90,
}

BUDGET_FILE_NAME = ".llm_budget.json"
STATE_SIDECAR_NAME = ".surgeon_state.json"


# ---------- data model ----------
@dataclass
class Position:
    trade_id: int
    symbol: str
    side: str            # LONG | SHORT
    entry_price: float
    entry_ts: str        # iso
    margin: float
    leverage: int
    notional: float
    sl: float
    tp1: float
    tp2: float
    tp3: float
    tp1_done: bool = False
    tp2_done: bool = False
    remaining_frac: float = 1.0
    last_progress_ts: str = ""
    divergence_at_entry: float = 0.0
    funding_at_entry: float = 0.0
    reason: str = ""

    def direction(self) -> int:
        return 1 if self.side == "LONG" else -1


@dataclass
class State:
    starting_balance: float = 10000.0
    balance: float = 10000.0
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    cumulative_turnover: float = 0.0
    trade_counter: int = 0
    positions: List[Position] = field(default_factory=list)
    closed: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, starting_balance: float) -> "State":
        if not path.exists():
            s = cls(starting_balance=starting_balance, balance=starting_balance)
            return s
        try:
            d = json.loads(path.read_text())
        except Exception as exc:
            LOG.warning("State file corrupted (%s), starting fresh: %s", path, exc)
            return cls(starting_balance=starting_balance, balance=starting_balance)
        s = cls(
            starting_balance=d.get("starting_balance", starting_balance),
            balance=d.get("balance", starting_balance),
            realized_pnl=d.get("realized_pnl", 0.0),
            total_fees=d.get("total_fees", 0.0),
            cumulative_turnover=d.get("cumulative_turnover", 0.0),
            trade_counter=d.get("trade_counter", 0),
            positions=[Position(**p) for p in (d.get("positions") or [])],
            closed=list(d.get("closed") or []),
        )
        return s

    def save(self, path: Path) -> None:
        d = {
            "starting_balance": self.starting_balance,
            "balance": self.balance,
            "realized_pnl": self.realized_pnl,
            "total_fees": self.total_fees,
            "cumulative_turnover": self.cumulative_turnover,
            "trade_counter": self.trade_counter,
            "positions": [asdict(p) for p in self.positions],
            "closed": (self.closed or [])[-50:],
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        tmp.replace(path)


# ---------- helpers ----------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(ts: str) -> datetime:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        LOG.warning("Failed to parse timestamp '%s', using epoch", ts)
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def read_text_safe(p: Path, tail_bytes: int = 0) -> str:
    if not p.exists():
        return ""
    try:
        data = p.read_text()
        if tail_bytes and len(data) > tail_bytes:
            return data[-tail_bytes:]
        return data
    except Exception as exc:
        LOG.warning("read %s failed: %s", p, exc)
        return ""


def read_json_safe(p: Path) -> Optional[dict]:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception as exc:
        LOG.warning("read_json %s failed: %s", p, exc)
        return None


# ---------- budget tracking ----------
def load_budget(workspace: Path) -> dict:
    p = workspace / BUDGET_FILE_NAME
    if not p.exists():
        return {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "usd": 0.0, "calls": 0}
    try:
        d = json.loads(p.read_text())
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if d.get("date") != today:
            return {"date": today, "usd": 0.0, "calls": 0}
        return d
    except Exception:
        return {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "usd": 0.0, "calls": 0}


def save_budget(workspace: Path, b: dict) -> None:
    (workspace / BUDGET_FILE_NAME).write_text(json.dumps(b))


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    # rough per-million rates; update as needed
    rates = {
        "anthropic/claude-sonnet-4.5": (3.0, 15.0),
        "anthropic/claude-sonnet-4":   (3.0, 15.0),
        "anthropic/claude-haiku-4.5":  (1.0, 5.0),
        "openai/gpt-4.1":              (2.0, 8.0),
        "openai/gpt-4.1-mini":         (0.4, 1.6),
        "google/gemini-2.5-pro":       (1.25, 5.0),
    }
    in_rate, out_rate = rates.get(model, (3.0, 15.0))
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate


# ---------- LLM call ----------
SYSTEM_PREAMBLE = """You are about to receive an agent SOUL plus current trading state and market data.

Follow the SOUL instructions as your operating manual. At the end of your response,
output EXACTLY one fenced JSON code block (```json ... ```) with a decision object
matching this schema:

{
  "reasoning_summary": "1-2 sentence plain English",
  "top_candidates": [
    {"symbol": "BTCUSDT", "side": "LONG|SHORT|FLAT", "tier": "MAX|HIGH|MODERATE|NONE", "divergence_pct": -0.05, "funding": 0.0001, "reason": "..."}
  ],
  "actions": [
    {"type": "OPEN", "symbol": "BTCUSDT", "side": "LONG", "tier": "HIGH",
     "margin_pct": 0.15, "sl_pct": 0.005, "tp1_pct": 0.01, "tp2_pct": 0.02, "tp3_pct": 0.04,
     "reason": "mark<index by 0.18% + neg funding"},
    {"type": "CLOSE_PARTIAL", "trade_id": 3, "fraction": 0.25, "reason": "TP1"},
    {"type": "CLOSE_ALL", "trade_id": 2, "reason": "CONVERGENCE"},
    {"type": "ADJUST_STOP", "trade_id": 3, "new_sl_abs": 27500.0, "reason": "breakeven after TP1"}
  ]
}

Rules you MUST obey:
- "actions" can be empty if no trades.
- Tier/margin mapping: MAX=0.22, HIGH=0.15, MODERATE=0.10. Respect SOUL caps.
- Max 3 concurrent open positions. Check current state before opening more.
- Stop-loss/TP levels are percentages from entry; Python will compute abs prices.
- Never exceed leverage configured in the SOUL.
- If no valid signal, still list top_candidates (top 3 by |divergence|) and leave actions=[].
- Mode is PAPER TRADING. Do not reference live wallet balances unless EXECUTION_REALITY.md is provided.
- Output ONE JSON block at the end. No preamble JSON. Prose before is fine but the FINAL fenced JSON block is what gets parsed.
"""


def call_openrouter(api_key: str, model: str, system_prompt: str, user_content: str,
                    max_tokens: int, temperature: float, timeout: int) -> Tuple[dict, dict]:
    """Returns (parsed_response, usage_dict). Raises on HTTP errors."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://tickles.local",
            "X-Title": "rubicon-surgeon",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    usage = data.get("usage", {}) or {}
    return data, usage


def extract_json_block(text: str) -> Optional[dict]:
    # look for last ```json ... ``` block
    matches = re.findall(r"```json\s*(.*?)\s*```", text, flags=re.DOTALL)
    if not matches:
        # try bare JSON object at end
        m = re.search(r"(\{[\s\S]*\})\s*$", text.strip())
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                return None
        return None
    try:
        return json.loads(matches[-1])
    except Exception as exc:
        LOG.warning("json parse failed: %s", exc)
        return None


# ---------- applying LLM decisions ----------
def entry_price_with_slip(price: float, side: str, slippage: float) -> float:
    return price * (1 + slippage) if side == "LONG" else price * (1 - slippage)


def exit_price_with_slip(price: float, side: str, slippage: float) -> float:
    return price * (1 - slippage) if side == "LONG" else price * (1 + slippage)


def margin_for_tier(tier: str, balance: float) -> float:
    return balance * {"MAX": 0.22, "HIGH": 0.15, "MODERATE": 0.10}.get(tier.upper(), 0.10)


def apply_open(state: State, action: dict, market: dict, cfg: dict):
    """Returns the new Position on success, or an error string on failure."""
    sym = action.get("symbol")
    side = (action.get("side") or "").upper()
    tier = (action.get("tier") or "MODERATE").upper()
    if side not in ("LONG", "SHORT"):
        return f"invalid side {side}"
    if sum(1 for p in state.positions if p.remaining_frac > 0) >= cfg["max_positions"]:
        return "max positions reached"
    if any(p.symbol == sym and p.remaining_frac > 0 for p in state.positions):
        return f"already in {sym}"
    asset = market.get(sym)
    if not asset:
        return f"no market data for {sym}"
    price = float(asset.get("price") or 0)
    if price <= 0:
        return f"no price for {sym}"
    margin_pct = action.get("margin_pct")
    margin = float(margin_pct) * state.balance if margin_pct is not None else margin_for_tier(tier, state.balance)
    leverage = int(cfg["leverage"])
    notional = margin * leverage
    entry = entry_price_with_slip(price, side, cfg["slippage"])
    sl_pct = float(action.get("sl_pct", 0.005))
    tp1_pct = float(action.get("tp1_pct", 0.01))
    tp2_pct = float(action.get("tp2_pct", 0.02))
    tp3_pct = float(action.get("tp3_pct", 0.04))
    if side == "LONG":
        sl = entry * (1 - sl_pct); tp1 = entry * (1 + tp1_pct); tp2 = entry * (1 + tp2_pct); tp3 = entry * (1 + tp3_pct)
    else:
        sl = entry * (1 + sl_pct); tp1 = entry * (1 - tp1_pct); tp2 = entry * (1 - tp2_pct); tp3 = entry * (1 - tp3_pct)
    fee = notional * cfg["taker_fee"]
    state.balance -= fee
    state.total_fees += fee
    state.cumulative_turnover += notional
    state.trade_counter += 1
    pos = Position(
        trade_id=state.trade_counter, symbol=sym, side=side,
        entry_price=round(entry, 8), entry_ts=now_iso(),
        margin=round(margin, 6), leverage=leverage, notional=round(notional, 6),
        sl=round(sl, 8), tp1=round(tp1, 8), tp2=round(tp2, 8), tp3=round(tp3, 8),
        last_progress_ts=now_iso(),
        divergence_at_entry=float(asset.get("divergencePct") or 0.0),
        funding_at_entry=float(asset.get("fundingRate") or 0.0),
        reason=action.get("reason", ""),
    )
    state.positions.append(pos)
    return pos  # type: ignore[return-value]


def close_fraction(state: State, pos: Position, frac: float, market: dict,
                   cfg: dict, action_tag: str, reason: str) -> dict:
    frac = max(0.0, min(frac, pos.remaining_frac))
    asset = market.get(pos.symbol) or {}
    price = float(asset.get("price") or pos.entry_price)
    exit_px = exit_price_with_slip(price, pos.side, cfg["slippage"])
    notional_closed = pos.notional * frac
    qty = notional_closed / pos.entry_price if pos.entry_price else 0
    if pos.side == "LONG":
        gross = qty * (exit_px - pos.entry_price)
    else:
        gross = qty * (pos.entry_price - exit_px)
    fees = notional_closed * cfg["taker_fee"]
    net = gross - fees
    state.balance += net
    state.realized_pnl += net
    state.total_fees += fees
    pos.remaining_frac = round(pos.remaining_frac - frac, 8)
    pos.last_progress_ts = now_iso()
    entry = {
        "trade_id": pos.trade_id, "symbol": pos.symbol, "side": pos.side,
        "action": action_tag, "entry_price": pos.entry_price, "exit_price": round(exit_px, 8),
        "fraction": frac, "gross_pnl": round(gross, 6), "fees": round(fees, 6),
        "net_pnl": round(net, 6), "cumulative_net_pnl": round(state.realized_pnl, 6),
        "reason": reason, "ts": now_iso(),
    }
    state.closed.append(entry)
    return entry


def apply_actions(state: State, actions: List[dict], market: dict,
                  cfg: dict) -> List[dict]:
    applied: List[dict] = []
    for a in actions or []:
        t = (a.get("type") or "").upper()
        try:
            if t == "OPEN":
                result = apply_open(state, a, market, cfg)
                if isinstance(result, Position):
                    applied.append({"type": "OPEN", "ok": True, "error": None,
                                    "action": a, "position": result})
                else:
                    applied.append({"type": "OPEN", "ok": False, "error": result, "action": a})
            elif t == "CLOSE_PARTIAL":
                tid = int(a.get("trade_id"))
                frac = float(a.get("fraction", 0.25))
                pos = next((p for p in state.positions if p.trade_id == tid and p.remaining_frac > 0), None)
                if not pos:
                    applied.append({"type": t, "ok": False, "error": f"no open pos {tid}"})
                    continue
                ent = close_fraction(state, pos, frac, market, cfg, "CLOSE_PARTIAL", a.get("reason", ""))
                # update SL on TP1/TP2 if stated
                reason_up = (a.get("reason") or "").upper()
                if "TP1" in reason_up:
                    pos.tp1_done = True
                    pos.sl = pos.entry_price
                if "TP2" in reason_up:
                    pos.tp2_done = True
                    trail = pos.entry_price * (1.005 if pos.side == "LONG" else 0.995)
                    pos.sl = max(pos.sl, trail) if pos.side == "LONG" else min(pos.sl, trail)
                applied.append({"type": t, "ok": True, "entry": ent})
            elif t == "CLOSE_ALL":
                tid = int(a.get("trade_id"))
                pos = next((p for p in state.positions if p.trade_id == tid and p.remaining_frac > 0), None)
                if not pos:
                    applied.append({"type": t, "ok": False, "error": f"no open pos {tid}"})
                    continue
                ent = close_fraction(state, pos, pos.remaining_frac, market, cfg, "CLOSE_ALL", a.get("reason", ""))
                applied.append({"type": t, "ok": True, "entry": ent})
            elif t == "ADJUST_STOP":
                tid = int(a.get("trade_id"))
                pos = next((p for p in state.positions if p.trade_id == tid and p.remaining_frac > 0), None)
                if not pos:
                    applied.append({"type": t, "ok": False, "error": f"no open pos {tid}"})
                    continue
                if a.get("new_sl_abs") is not None:
                    pos.sl = float(a["new_sl_abs"])
                elif a.get("new_sl_pct") is not None:
                    pct = float(a["new_sl_pct"])
                    pos.sl = pos.entry_price * (1 - pct) if pos.side == "LONG" else pos.entry_price * (1 + pct)
                pos.last_progress_ts = now_iso()
                applied.append({"type": t, "ok": True, "sl": pos.sl})
            else:
                applied.append({"type": t, "ok": False, "error": "unknown action"})
        except Exception as exc:
            applied.append({"type": t, "ok": False, "error": f"exception: {exc}"})
    # garbage-collect fully closed positions
    state.positions = [p for p in state.positions if p.remaining_frac > 1e-8]
    return applied


# ---------- safety net: deterministic auto-exits before LLM runs ----------
def auto_manage_positions(state: State, market: dict, cfg: dict) -> List[dict]:
    """Enforce SL / TP / convergence / time / stall regardless of LLM."""
    auto: List[dict] = []
    CONVERGENCE = cfg.get("convergence_threshold", 0.03)
    MAX_HOLD = cfg.get("max_hold_seconds", 45 * 60)
    STALL = cfg.get("stall_seconds", 15 * 60)
    for pos in list(state.positions):
        if pos.remaining_frac <= 0:
            continue
        asset = market.get(pos.symbol) or {}
        price = float(asset.get("price") or 0)
        if price <= 0:
            continue
        raw_div = asset.get("divergencePct")
        if raw_div is None:
            continue  # skip convergence check if data missing
        div = abs(float(raw_div))
        now = datetime.now(timezone.utc)
        held = (now - parse_ts(pos.entry_ts)).total_seconds()
        stalled = (now - parse_ts(pos.last_progress_ts)).total_seconds()
        # SL
        stop_hit = (pos.side == "LONG" and price <= pos.sl) or (pos.side == "SHORT" and price >= pos.sl)
        if stop_hit:
            auto.append(close_fraction(state, pos, pos.remaining_frac, market, cfg, "SL", "stop hit"))
            continue
        # Convergence
        if div < CONVERGENCE:
            auto.append(close_fraction(state, pos, pos.remaining_frac, market, cfg, "CONVERGENCE", f"div={div:.3f}%"))
            continue
        # Time stop
        if held > MAX_HOLD:
            auto.append(close_fraction(state, pos, pos.remaining_frac, market, cfg, "TIME_STOP", f"held {held:.0f}s"))
            continue
        # Stall between TP1 and TP2
        if pos.tp1_done and not pos.tp2_done and stalled > STALL:
            auto.append(close_fraction(state, pos, pos.remaining_frac, market, cfg, "STALL", f"stalled {stalled:.0f}s"))
            continue
    state.positions = [p for p in state.positions if p.remaining_frac > 1e-8]
    return auto


# ---------- mem0 persistence ----------
_LOG_HEADER_RE = re.compile(
    r"Trade #(?P<trade_id>\d+) -- (?P<symbol>\S+)\s+(?P<side>\S+)"
)
_DECISION_HEADER_RE = re.compile(r"^Decision @ ", re.MULTILINE)


def build_state_snapshot_text(state: State, cfg: dict, last_reasoning: str) -> str:
    """Render the latest state snapshot as a Twilly-style narrative for mem0.

    The output mirrors what the legacy ``TRADE_STATE.md`` file used to contain
    so downstream consumers (LLM prompt, dashboards) get the same context.

    Args:
        state: Current ``State`` instance.
        cfg: Agent config dict (uses ``agent_name`` and ``mode``).
        last_reasoning: Most recent LLM reasoning summary string.

    Returns:
        A multi-line string suitable for ``mem.add(text=...)``.
    """
    lines = [
        f"# TRADE_STATE — {cfg['agent_name']}",
        "",
        f"Last-updated: {now_iso()}",
        f"Mode: {cfg['mode']}",
        f"Starting Balance: ${state.starting_balance:,.2f}",
        f"Current Balance:  ${state.balance:,.2f}",
        f"Realized P&L:     ${state.realized_pnl:+,.2f}",
        f"Net vs Starting:  ${(state.balance - state.starting_balance):+,.2f}",
        f"Total Estimated Fees: ${state.total_fees:,.2f}",
        f"Cumulative Turnover: ${state.cumulative_turnover:,.2f}",
        f"Trades opened: {state.trade_counter}  Closed entries: {len(state.closed)}",
        "",
        "## Last LLM Reasoning",
        last_reasoning or "(none)",
        "",
        "## Open Positions",
    ]
    open_pos = [p for p in state.positions if p.remaining_frac > 0]
    if not open_pos:
        lines.append("(none)")
    else:
        for p in open_pos:
            lines.append(
                f"- Trade #{p.trade_id} {p.symbol} {p.side} entry=${p.entry_price:.4f} "
                f"margin=${p.margin:.2f} notional=${p.notional:.2f} SL=${p.sl:.4f} "
                f"TP1/TP2/TP3=${p.tp1:.4f}/${p.tp2:.4f}/${p.tp3:.4f} "
                f"rem={p.remaining_frac:.2%} tp1={p.tp1_done} tp2={p.tp2_done} since={p.entry_ts}"
            )
    lines.append("")
    lines.append("## Closed (last 5)")
    for e in state.closed[-5:]:
        lines.append(
            f"- #{e['trade_id']} {e['symbol']} {e['side']} {e['action']} "
            f"entry=${e['entry_price']:.4f} exit=${e['exit_price']:.4f} "
            f"frac={e['fraction']:.0%} net=${e['net_pnl']:+,.2f} @ {e['ts']}"
        )
    return "\n".join(lines) + "\n"


def persist_state_snapshot(
    state: State,
    cfg: dict,
    last_reasoning: str,
    memory: Optional[ScopedMemory],
) -> None:
    """Write the latest state snapshot to mem0 (overwrite-latest semantics).

    Args:
        state: Current ``State`` instance.
        cfg: Agent config dict.
        last_reasoning: Latest LLM reasoning summary.
        memory: ``ScopedMemory`` instance, or ``None`` to skip persistence.
    """
    if memory is None:
        return
    text = build_state_snapshot_text(state, cfg, last_reasoning)
    metadata = {
        "type": "trade_state_snapshot",
        "kind": "live",
        "agent_name": cfg.get("agent_name", "surgeon"),
        "mode": cfg.get("mode", "PAPER_TRADING"),
        "balance": round(state.balance, 6),
        "realized_pnl": round(state.realized_pnl, 6),
        "open_positions": sum(1 for p in state.positions if p.remaining_frac > 0),
        "original_timestamp": now_iso(),
    }
    try:
        memory.add(text, metadata=metadata)
    except Exception as exc:
        LOG.warning("persist_state_snapshot failed: %s", exc)


def persist_trade_decisions(
    entries: List[str],
    memory: Optional[ScopedMemory],
) -> None:
    """Write each formatted log entry to mem0 as a discrete trade_decision memory.

    The original Twilly entry text is preserved verbatim. Best-effort metadata
    (``trade_id``, ``symbol``, ``side``) is parsed from the header so future
    ``memory.search`` queries can filter by trade or asset.

    Args:
        entries: List of pre-formatted entry strings (open/close/decision).
        memory: ``ScopedMemory`` instance, or ``None`` to skip persistence.
    """
    if memory is None or not entries:
        return
    for raw in entries:
        text = raw.rstrip("\n")
        if not text:
            continue
        metadata: Dict[str, Any] = {
            "type": "trade_decision",
            "kind": "live",
            "original_timestamp": now_iso(),
        }
        if _DECISION_HEADER_RE.search(text):
            metadata["action"] = "DECISION"
        else:
            match = _LOG_HEADER_RE.search(text)
            if match:
                metadata["trade_id"] = int(match.group("trade_id"))
                metadata["symbol"] = match.group("symbol")
                metadata["side"] = match.group("side")
                if "[CLOSE" in text or "Action: CLOSE" in text:
                    metadata["action"] = "CLOSE"
                elif "Action: OPEN" in text:
                    metadata["action"] = "OPEN"
        try:
            memory.add(text, metadata=metadata)
        except Exception as exc:
            LOG.warning(
                "persist_trade_decisions: failed to add entry (trade_id=%s): %s",
                metadata.get("trade_id"),
                exc,
            )


def _format_memory_hits(hits: Any) -> str:
    """Render a list of mem0 search hits as a plain-text block for the LLM.

    Args:
        hits: Whatever ``memory.search`` returned (list, dict, str, ...).

    Returns:
        Newline-joined text of each hit's memory body, or ``""`` if empty.
    """
    if not hits:
        return ""
    items: List[Any]
    if isinstance(hits, dict) and "results" in hits:
        items = list(hits.get("results") or [])
    elif isinstance(hits, list):
        items = hits
    else:
        return str(hits)
    chunks: List[str] = []
    for item in items:
        if isinstance(item, dict):
            chunks.append(str(item.get("memory") or item.get("text") or item))
        else:
            chunks.append(str(item))
    return "\n\n".join(c for c in chunks if c)


def format_open_entry(state: State, pos: Position, reason: str) -> str:
    return (
        f"Trade #{pos.trade_id} -- {pos.symbol} {pos.side}\n"
        f"- Time: {pos.entry_ts} | Divergence: {pos.divergence_at_entry:+.3f}% | "
        f"Funding: {pos.funding_at_entry*100:+.4f}%\n"
        f"- Entry: ${pos.entry_price:.4f} | Margin: ${pos.margin:.2f} | "
        f"Leverage: {pos.leverage}x | Notional: ${pos.notional:.2f}\n"
        f"- Stop: ${pos.sl:.4f} | TP1/TP2/TP3: ${pos.tp1:.4f}/${pos.tp2:.4f}/${pos.tp3:.4f}\n"
        f"- Action: OPEN | Reason: {reason}\n"
        f"- Cumulative Net P&L: ${state.realized_pnl:+,.2f}\n"
    )


def format_close_entry(entry: dict, state: State) -> str:
    return (
        f"Trade #{entry['trade_id']} -- {entry['symbol']} {entry['side']} "
        f"[{entry['action']} {entry['fraction']:.0%}]\n"
        f"- Time: {entry['ts']}\n"
        f"- Entry: ${entry['entry_price']:.4f} | Exit: ${entry['exit_price']:.4f}\n"
        f"- Gross P&L: ${entry['gross_pnl']:+,.2f}\n"
        f"- Est. Fees: ${entry['fees']:,.2f}\n"
        f"- Net P&L: ${entry['net_pnl']:+,.2f}\n"
        f"- Cumulative Net P&L: ${entry['cumulative_net_pnl']:+,.2f}\n"
        f"- Reason: {entry['reason']}\n"
    )


def format_decision_line(cands: List[dict], reasoning: str) -> str:
    head = f"\nDecision @ {now_iso()} — NO TRADE. {reasoning}"
    body = "\n".join(
        f"  - {c.get('symbol','?')} {c.get('side','FLAT')} [{c.get('tier','NONE')}]: "
        f"div={float(c.get('divergence_pct') or 0):+.3f}% "
        f"funding={float(c.get('funding') or 0)*100:+.4f}% :: {c.get('reason','')}"
        for c in (cands or [])[:3]
    )
    return head + ("\n" + body if body else "")


# ---------- cycle ----------
def cycle(cfg: dict, openrouter_key: str, memory: Optional[ScopedMemory] = None) -> None:
    """
    Execute one iteration of the LLM Surgeon loop.
    
    Args:
        cfg: Configuration dictionary.
        openrouter_key: API key for OpenRouter.
        memory: Optional ScopedMemory instance for the Learning Loop.
    """
    ws = Path(cfg["workspace"])
    ws.mkdir(parents=True, exist_ok=True)
    state_path = ws / STATE_SIDECAR_NAME
    state = State.load(state_path, cfg["starting_balance"])

    soul_md = read_text_safe(ws / "SOUL.md")

    # Pull recent state snapshot + decision tail from mem0 (replaces TRADE_STATE.md / TRADE_LOG.md)
    trade_state_md = ""
    trade_log_tail = ""
    if memory is not None:
        try:
            state_hits = memory.search(
                query="latest trade state snapshot",
                metadata={"type": "trade_state_snapshot"},
                limit=1,
            )
            trade_state_md = _format_memory_hits(state_hits)
        except Exception as exc:
            LOG.warning("mem0 search (trade_state_snapshot) failed: %s", exc)
        try:
            log_hits = memory.search(
                query="recent trade decisions",
                metadata={"type": "trade_decision"},
                limit=5,
            )
            trade_log_tail = _format_memory_hits(log_hits)
        except Exception as exc:
            LOG.warning("mem0 search (trade_decision) failed: %s", exc)
    
    # Read market data with Freshness Guard
    market_state_path = ws / "MARKET_STATE.json"
    market_ind_path = ws / "MARKET_INDICATORS.json"
    
    market_state = read_json_safe(market_state_path) or {}
    market_ind = read_json_safe(market_ind_path) or {}
    
    # Validate freshness of market data
    if market_state:
        try:
            validate_freshness(market_state.get("timestamp"), context="MARKET_STATE.json")
        except ValueError as exc:
            raise StaleDataError(str(exc), lag_seconds=float("inf"), threshold_seconds=180.0)
    if market_ind:
        try:
            validate_freshness(market_ind.get("timestamp"), context="MARKET_INDICATORS.json")
        except ValueError as exc:
            raise StaleDataError(str(exc), lag_seconds=float("inf"), threshold_seconds=180.0)
        
    exec_reality = read_text_safe(ws / "EXECUTION_REALITY.md")

    assets = (market_state.get("assets") or {})

    # 1) auto-manage existing positions BEFORE asking LLM (math is sacred)
    auto_closed = auto_manage_positions(state, assets, cfg)
    auto_log_lines = [format_close_entry(e, state) for e in auto_closed]
    
    # Record autopsies for auto-closed trades
    if memory and auto_closed:
        for entry in auto_closed:
            try:
                autopsy = (
                    f"AUTO-CLOSED Trade #{entry['trade_id']} {entry['symbol']} {entry['side']} "
                    f"via {entry['action']}. Net PnL: ${entry['net_pnl']:.2f}. Reason: {entry['reason']}"
                )
                memory.add(autopsy, metadata={"type": "autopsy", "trade_id": entry["trade_id"], "auto": True})
                LOG.info("Recorded auto-close autopsy for trade #%d", entry["trade_id"])
            except Exception as e:
                LOG.warning("Failed to record auto-close autopsy: %s", e)

    # 2) Query Memory for relevant experiences
    memories = []
    if memory:
        try:
            # Query for recent performance or similar market conditions
            query_str = f"recent performance {cfg['agent_name']} {list(assets.keys())[:3]}"
            memories = memory.search(query_str, limit=5)
            LOG.info("Retrieved %d memories for context", len(memories))
        except Exception as e:
            LOG.warning("Memory search failed: %s", e)

    # 3) build LLM prompt
    # Ensure memories are JSON-serializable
    safe_memories = []
    for m in (memories or []):
        if isinstance(m, dict):
            safe_memories.append(m)
        elif hasattr(m, "__dict__"):
            safe_memories.append(m.__dict__)
        else:
            safe_memories.append(str(m))
    user_payload = {
        "now_utc": now_iso(),
        "memories": safe_memories,
        "TRADE_STATE_md": trade_state_md,
        "TRADE_LOG_tail": trade_log_tail,
        "MARKET_STATE": market_state,
        "MARKET_INDICATORS": market_ind,
        "EXECUTION_REALITY_md": exec_reality,
        "OPEN_POSITIONS": [asdict(p) for p in state.positions if p.remaining_frac > 0],
        "BALANCE": round(state.balance, 6),
        "REALIZED_PNL": round(state.realized_pnl, 6),
        "CONFIG": {
            "mode": cfg["mode"], "leverage": cfg["leverage"],
            "max_positions": cfg["max_positions"],
            "taker_fee": cfg["taker_fee"], "slippage": cfg["slippage"],
        },
        "AUTO_CLOSED_THIS_CYCLE": auto_closed,
    }

    system_prompt = SYSTEM_PREAMBLE + "\n\n--- AGENT SOUL (follow this as your manual) ---\n\n" + (soul_md or "(missing SOUL.md)")

    # 3) budget check
    ws_budget = load_budget(ws)
    if ws_budget["usd"] >= cfg["model_budget_usd_day"]:
        LOG.warning("daily LLM budget exhausted ($%.2f). Skipping LLM call, auto-management only.",
                    ws_budget["usd"])
        llm_decision: Dict[str, Any] = {"reasoning_summary": "(budget cap hit)", "top_candidates": [], "actions": []}
        reasoning_summary = "LLM skipped — daily budget cap reached."
    else:
        # 4) call LLM (primary then fallback)
        llm_decision = None
        reasoning_summary = ""
        last_err = None
        for model in [cfg["model_primary"], cfg["model_fallback"]]:
            try:
                resp, usage = call_openrouter(
                    openrouter_key, model, system_prompt, json.dumps(user_payload),
                    cfg["max_output_tokens"], cfg["temperature"], cfg["openrouter_timeout_sec"]
                )
                content = resp["choices"][0]["message"]["content"]
                parsed = extract_json_block(content)
                if not parsed:
                    last_err = f"{model}: no JSON block parsed"
                    LOG.warning(last_err)
                    continue
                cost = estimate_cost_usd(model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
                ws_budget["usd"] = round(ws_budget["usd"] + cost, 6)
                ws_budget["calls"] += 1
                save_budget(ws, ws_budget)
                LOG.info("LLM %s in=%d out=%d est=$%.4f budget=$%.4f",
                         model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                         cost, ws_budget["usd"])
                llm_decision = parsed
                reasoning_summary = parsed.get("reasoning_summary", "")
                break
            except Exception as exc:
                last_err = f"{model}: {exc}"
                LOG.warning(last_err)
        if llm_decision is None:
            LOG.error("all LLM attempts failed: %s", last_err)
            llm_decision = {"reasoning_summary": f"LLM failed: {last_err}", "top_candidates": [], "actions": []}
            reasoning_summary = llm_decision["reasoning_summary"]

    # 5) apply LLM actions
    applied = apply_actions(state, llm_decision.get("actions", []), assets, cfg)

    # 6) build log entries
    log_entries: List[str] = []
    log_entries.extend(auto_log_lines)  # auto-exits first
    for a in applied:
        if a["type"] == "OPEN" and a["ok"]:
            pos = a.get("position")
            if pos is not None:
                log_entries.append(format_open_entry(state, pos, a["action"].get("reason", "")))
        elif a["type"] in ("CLOSE_PARTIAL", "CLOSE_ALL") and a["ok"]:
            log_entries.append(format_close_entry(a["entry"], state))
            # Record autopsy for manually closed trades
            if memory:
                try:
                    entry = a["entry"]
                    autopsy = (
                        f"CLOSED Trade #{entry['trade_id']} {entry['symbol']} {entry['side']} "
                        f"via {entry['action']}. Net PnL: ${entry['net_pnl']:.2f}. Reason: {entry['reason']}"
                    )
                    memory.add(autopsy, metadata={"type": "autopsy", "trade_id": entry["trade_id"]})
                    LOG.info("Recorded manual-close autopsy for trade #%d", entry["trade_id"])
                except Exception as e:
                    LOG.warning("Failed to record manual-close autopsy: %s", e)

    if not applied or not any(a.get("ok") for a in applied):
        if not auto_closed:
            log_entries.append(format_decision_line(llm_decision.get("top_candidates", []), reasoning_summary))

    persist_trade_decisions(log_entries, memory)
    persist_state_snapshot(state, cfg, reasoning_summary, memory)
    state.save(state_path)

    open_n = sum(1 for p in state.positions if p.remaining_frac > 0)
    LOG.info("cycle done. balance=$%.2f open=%d realized=$%.2f applied=%d auto=%d",
             state.balance, open_n, state.realized_pnl, len(applied), len(auto_closed))


def load_config(path: Path, overrides: dict) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path and path.exists():
        try:
            cfg.update(json.loads(path.read_text()))
        except Exception as exc:
            LOG.warning("config %s parse failed: %s", path, exc)
    for k, v in overrides.items():
        if v is not None:
            cfg[k] = v
    return cfg


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=str, default="")
    ap.add_argument("--workspace", type=str, default="")
    ap.add_argument("--agent-name", type=str, default="")
    ap.add_argument("--interval", type=int, default=None)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    overrides = {}
    if args.workspace:
        overrides["workspace"] = args.workspace
    if args.agent_name:
        overrides["agent_name"] = args.agent_name
    if args.interval is not None:
        overrides["interval_sec"] = args.interval

    cfg_path = Path(args.config) if args.config else None
    cfg = load_config(cfg_path, overrides)

    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not openrouter_key:
        LOG.error("OPENROUTER_API_KEY missing; cannot run LLM cycles.")
        sys.exit(2)

    # Initialize ScopedMemory if configured
    memory = None
    company_id = os.environ.get("COMPANY_ID", "tickles")
    agent_id = cfg["agent_name"]
    try:
        memory = ScopedMemory(company=company_id, agent_id=agent_id)
        LOG.info("ScopedMemory initialized for %s/%s", company_id, agent_id)
    except Exception as e:
        LOG.warning("ScopedMemory initialization failed (Learning Loop disabled): %s", e)

    LOG.info("surgeon_llm starting. agent=%s ws=%s interval=%ss model=%s",
             cfg["agent_name"], cfg["workspace"], cfg["interval_sec"], cfg["model_primary"])

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))

    if args.once:
        try:
            cycle(cfg, openrouter_key, memory=memory)
        except StaleDataError as e:
            LOG.error("STALE DATA: %s. Stopping.", e)
            sys.exit(1)
        return

    interval = max(int(cfg.get("interval_sec", 300)), 1)
    while not stop["flag"]:
        try:
            cycle(cfg, openrouter_key, memory=memory)
        except StaleDataError as e:
            LOG.error("STALE DATA: %s. Waiting for fresh data...", e)
        except Exception as exc:
            LOG.exception("cycle failed: %s", exc)
        for _ in range(interval):
            if stop["flag"]:
                break
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
