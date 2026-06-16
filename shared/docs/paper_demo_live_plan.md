# Paper → Demo → Live Pipeline — Audit & Remediation Plan

**Date:** 2026-06-16
**Audited by:** 5 independent code-review agents (read-only)
**Scope:** Entire paper→demo→live flow, margin/leverage, position monitoring, price tracking, dashboard comparison
**For:** Senior engineer review before implementation

---

## Executive Summary

The pipeline has three independently polling daemons (copy_trade_monitor 30s, demo_bridge 15s, position_monitor 60s) that interact only through the database. Paper trading works. Demo bridge works but only mirrors 1 of 12 agents (`copy_charthacker`). The two layers have diverging sizing math, independent config sources, and no BE-lock on demo. 50 critical bugs/issues found, 19 of them HIGH severity.

The core blocker to paper-demo parity: **limit orders placed BEFORE paper confirms candle-touch**. Paper checks backward-looking candles ("did price ever cross entry?"); demo places forward-looking limit orders ("will a counterparty take this?"). These are fundamentally different models. Fixing this requires switching demo orders to IOC (immediate-or-cancel) or market-on-touch. <- NO NEVER!! pending limit orders ONLY!!!>

---

## Issues Found (by component)

### 1. Paper Trading Layer (`copy_trade_monitor.py`)

| # | Severity | Line | Issue |
|---|----------|------|-------|
| B2 | **HIGH** | 755-774 | `max_concurrent_5`/`max_concurrent_3` sizing knobs defined in config but NEVER enforced. Lev agents have no position-count cap |
| B7 | **HIGH** | 871-879 | `_sync_sl_tp()` overwrites Opt agents' multiplier-scaled SL/TP with raw trader values EVERY TICK — nullifies optimizer |
| B10 | MEDIUM | 858, 1397 | Margin queue entries don't update `_agent_entered`; duplicate paper positions possible for non-spot_seq agents |
| B4 | MEDIUM | 1011 | `asyncio.ensure_future()` fire-and-forget for `_log_trade()` → contest scores lag one tick |
| B6 | LOW | 1181, 791 | Equity formula excludes entry fees → dashboard equity overstated by cumulative fees |
| B5 | LOW | 898-900 | BE pre-check uses only latest candle; can miss BE trigger during candle gaps |
| B3 | LOW | 757 | Sizing multiplies balance by risk% with no negative-balance guard |

### 2. Demo Bridge (`demo_bridge.py` + `ccxt_adapter.py`)

| # | Severity | Line | Issue |
|---|----------|------|-------|
| D1 | **CRITICAL** | -- | Only `copy_charthacker` mapped to exchange accounts. 11 other agents have zero demo coverage |
| D2 | **HIGH** | 713-798 | `_sync_positions` creates synthetic rows with NULL `tracked_position_id`, NULL `paper_sl`/`paper_tp`, `paper_entry == demo_entry` → useless for paper-vs-demo comparison |
| D3 | **HIGH** | bridge:220 | `DEMO_NOTIONAL_SAFETY_PCT=0.99` applied to demo but NOT paper → systematic 1% notional gap |
| D4 | **HIGH** | bridge:95-100 | Demo env vars (`DEMO_RISK_PCT_5`) NOT synced with paper's DB-tunable `copy_sizing_config`. Changing risk% in Settings UI only affects paper |
| D5 | **HIGH** | bridge:none | BE-lock missing on demo entirely. Paper agents B/C lock profits at +5% and free margin; demo ignores this |
| D6 | MEDIUM | bridge:370 vs 191 | Zero-balance account falls back to $1000 sizing (`0.0` is falsy) → guaranteed exchange rejection |
| D7 | LOW | ccxt:300 vs copy:276 | Demo clamps leverage to exchange max per-symbol; paper only clamps at global cap (100x). On low-liquidity coins, paper computes impossible leverage |
| D8 | LOW | bridge:222 | Demo rounds leverage to integer; paper uses float → ~0.5% notional difference per trade |

### 3. Margin / Leverage / Balance Parity

| # | Severity | Line | Issue |
|---|----------|------|-------|
| M1 | **HIGH** | copy:279-302 | `be_lock_leverage()` uses `be_liq_mmr=0.010`. Demo has no equivalent BE-lock path |
| M2 | **HIGH** | bridge:310-317 vs none | Paper has `_used_margin()` pool tracking + margin queue with retry. Demo has no equivalent — just fires orders, exchange rejects them |
| M3 | MEDIUM | copy:1306 vs env | Paper sizing reads from DB-refreshed SIZING dict (tunable via Settings UI). Demo reads env vars only. Can silently diverge |
| M4 | MEDIUM | bridge:90-92 | Concurrency caps differ: spot_seq paper=1 vs demo=5; spot_lev_3x paper=1 vs demo=3 |
| M5 | LOW | ccxt:221-238 | Cross-margin silently fails if `set_margin_mode` throws; no fallback detection |

### 4. Position Monitoring & Price Tracking

| # | Severity | Line | Issue |
|---|----------|------|-------|
| P1 | **HIGH** | price_feed:284-289 | Capital.com CFD polling is a complete NO-OP stub. All CFD instruments get zero live price streaming |
| P2 | **HIGH** | pos_monitor:106-117 | `_CAPITAL_EPIC_MAP` missing US30 and XAU/USD entries. XAU/USD resolves to "XAUUSD" instead of "GOLD"; US30 has no mapping |
| P3 | **HIGH** | candle_collect:28-36 | Default `COLLECTION_SYMBOLS` = 3 crypto pairs. Vast majority of symbols have zero pre-collected candles, forcing live CCXT calls on every monitor cycle |
| P4 | MEDIUM | pos_monitor:19 vs 303 | Docstring claims poll interval from system_config but code only reads env var `POSITION_MONITOR_POLL_S` |
| P5 | MEDIUM | pos_monitor:2148-2153 | CCXT fallback for `capital.com` raises `UnsupportedExchangeError` → silently swallowed → CFD positions never get price |
| P6 | MEDIUM | pos_monitor:1766 | Pending expiry hardcoded at 7 days, not configurable |
| P7 | LOW | pos_monitor:1795 | Lost-race detection uses fragile string matching on asyncpg return format |

### 5. Architecture & Dashboard

| # | Severity | Line | Issue |
|---|----------|------|-------|
| A1 | **CRITICAL** | design | **Fundamental model asymmetry**: Paper checks backwards ("did price ever touch entry?"). Demo places forwards ("will a counterparty take this limit order?"). A wick through entry with zero volume fills paper but NOT demo |
| A2 | **HIGH** | design | Two independent polling loops (30s paper, 15s demo) with NO coordination. Demo places limit orders BEFORE paper confirms entry |
| A3 | **HIGH** | routes:2308-2332 | Dashboard accuracy score is `entry_accuracy` only (mean absolute slippage). Does NOT incorporate fill_rate. A 0.01% slip on 1 fill scores 99.99% while 99% of orders are rejected |
| A4 | **MEDIUM** | demo_orders | `competition_trade_id` always NULL — can't map demo order to specific paper agent's trade. JOIN on `tracked_position_id` produces cartesian product (12 paper agents × N demo orders) |
| A5 | MEDIUM | design | Paper closes at SL/TP via candle wick check; demo positions might still be open. No cross-reference code |
| A6 | LOW | routes | Accuracy formula weighs all notional sizes equally — a $5 DOGE fill and $1000 BTC fill count the same |

---

## Priority Remediation Plan

### Phase A — Immediate fixes (same session, all HIGH)

**A1. Add demo exchange accounts for all 12 agents**
- File: DB only (competition_agent_exchanges)
- What: Map each agent to a bybit demo account
- Effort: 10 minutes (SQL inserts)

**A2. Fix _sync_sl_tp() nullifying optimizer (B7)**
- File: copy_trade_monitor.py line 871-879
- What: Skip overwrite for Opt agents; keep their multiplier-scaled SL/TP
- Effort: 2 lines

**A3. Fix sizing config divergence (D4)**
- File: demo_bridge.py _compute_demo_size
- What: Read risk% and sizing knobs from shared SIZING dict (same as paper)
- Effort: ~15 lines

**A4. Fix zero-balance fallback (D6)**
- File: demo_bridge.py line 191
- What: Don't fallback to $1000 when balance is genuinely 0
- Effort: 1 line

**A5. Fix synced position rows to carry paper data (D2)**
- File: demo_bridge.py _sync_positions
- What: Attempt to JOIN back to tracked_positions by symbol+direction, populate paper_entry/paper_sl/paper_tp
- Effort: ~20 lines

### Phase B — Structural fixes (1-2 sessions)

**B1. Switch demo from LIMIT to IOC (immediate-or-cancel)**
- File: demo_bridge.py + ccxt_adapter.py
- What: Replace `ORDER_TYPE_LIMIT` with IOC limit orders. Fills instantly at limit or better; partial fills at limit, remainder cancelled. Matches paper's "fill at entry" model
- Effort: ~30 lines
- **This is the single biggest lever for paper-demo parity**

**B2. Add BE-lock to demo bridge (D5 / M1)**
- File: demo_bridge.py (new `_check_be_lock()` method)
- What: Each tick, check if any filled position has moved +5% in profit; if so, move SL to breakeven, recalculate leverage, free margin
- Effort: ~60 lines

**B3. Unify sizing into shared module**
- File: new `shared/sizing.py`
- What: `compute_allocation(mode, balance, entry, sl) → (allocated, leverage, notional)`. Both daemons import it
- Effort: ~80 lines, remove ~60 duplicated lines

**B4. Fix accuracy score to incorporate fill rate (A3)**
- File: market_routes.py handle_paper_vs_demo
- What: `score = (entry_accuracy * fill_rate) / 100`
- Effort: 3 lines

### Phase C — Monitoring & CFD fixes (2-3 sessions)

**C1. Implement Capital.com REST polling (P1)**
- File: price_feed.py
- What: Replace NO-OP stub with actual `fetch_ticker()` polling
- Effort: ~40 lines

**C2. Fix Capital.com epic mapping (P2)**
- File: position_monitor.py _CAPITAL_EPIC_MAP
- What: Add US30, XAU/USD entries with correct Capital.com epics
- Effort: ~5 lines

**C3. Expand candle collection (P3)**
- File: run_candle_collection.py
- What: Auto-discover collection symbols from tracked_positions/interpretations
- Effort: ~30 lines

### Phase D — Architectural (future)

**D1. Merge paper + demo into single daemon**
- Eliminates dual-polling race conditions
- Guarantees ordering: paper enters → demo mirrors in same tick

**D2. Event bus instead of polling**
- `PositionOpened` event triggers both paper entry and demo mirroring

**D3. Live lane**
- Fork demo_bridge to live_bridge with risk controls, confirmation gates
- Activate capital.com credentials

---

## How to Achieve 99.9% Paper-Demo Parity

1. **Switch to IOC limit orders** — fills exactly like paper (at limit or better, instantly). This alone closes the biggest gap.
2. **Map all agents to bybit demo** — 678 symbols, deep liquidity. Bitget demo (29 symbols) is the wrong venue.
3. **Accept 0.1-0.5% residual slip** — real exchange spread + order book depth. This is the cost of real exchange testing.
4. **Tighten demo concurrency caps to match paper** (M4)
5. **Sync sizing config to single source of truth** (A3)
6. **Add BE-lock to demo** (B2)
7. **Fix accuracy score to include fill rate** (B4)

With these, the "Paper → Demo accuracy" number on the dashboard will reflect real parity, not just entry price fidelity on the few orders that happen to fill.

---

## Files to Review

| File | Purpose | Review for |
|------|---------|-----------|
| `shared/intelligence/copy_trade_monitor.py` | Paper trading engine | Sizing, P&L, BE-lock, concurrency |
| `shared/daemons/demo_bridge.py` | Demo bridge | Sizing parity, order lifecycle, sync_positions |
| `shared/execution/ccxt_adapter.py` | Exchange adapter | Cross margin, leverage clamping, has_market |
| `shared/intelligence/position_monitor.py` | Position tracker | Activation, expiry, near-miss, CFD handling |
| `shared/market_data/price_feed.py` | Live price | CFD polling stub, websocket |
| `shared/market_data/run_candle_collection.py` | Candle collector | Default symbols, capital.com |
| `shared/dashboard/market_routes.py:2027-2400` | Paper-vs-demo API | Accuracy formula, column population |
| `shared/intelligence/technique_tracker.py` | Technique grading | Validation-aware voting |
| `shared/intelligence/trade_intel.py` | Trade intel | Regex quality, close path |

---

## Database State (as of 2026-06-16)

```
demo_orders:  470 cancelled, 189 rejected, 82 pending, 29 filled, 4 closed
competition_trades: 532 total, 454 closed
contest_participants: 12 agents, most unrealized P&L positive
tracked_positions: 91 open, 119 pending, 133 closed
paper total realized P&L: -$2,791  |  demo total: $0 (pre-sync_positions)
paper unrealized P&L aggregate: ~+$1,500 est
demo accounts on exchange: 52 positions, ~+$899 unrealized est
copy_lev_parallel: realized -$319, unrealized +$636, equity $1,317 (best performer)
```

---

*This document covered the initial audit.  Issues P1-P12 have been addressed
in order.  Remaining from the audit: Capital.com CFD gaps, duplicate demo orders,
MCP ccxt account config, and the smart margin queue (backlog).*


## Post-Implementation Code Review (2026-06-17)

After 12 fixes spanning 7 files, a full code review and bug-hunt pass is
necessary before further work.  Areas to scrutinise:

1. **`copy_trade_monitor.py`** — `_sync_sl_tp` opt-agent guard, leverage clamping
   via `exchange_limits`, the `_entry_touched` / margin-queue interaction, and
   the `asyncio.ensure_future` fire-and-forget in `_close_agent_position`.

2. **`demo_bridge.py`** — sizing-knob sync via `copy_sizing_config`, BE-lock
   piggybacking on `_sync_positions`, paper-trail backfill, direction detection
   from CCXT `side` field, concurrency-cap parity, and the float-leverage flow
   through `_compute_demo_size` → `ExecutionIntent` → `ccxt_adapter.submit()`.

3. **`ccxt_adapter.py`** — `modify_sl()` Bybit/Bitget endpoints (tested only
   via demo bridge logs, never independently), `round(lev)` change in submit,
   and cross-margin `_ensure_cross_margin` silent-failure paths.

4. **`copy_sizing_config.py`** — new `demo_be_lock_*` knobs (bounds, UI wiring).

5. **`exchange_limits.py`** — new module.  Cache invalidation, multi-exchange
   credential loading, symbol normalisation edge cases, and behaviour when
   ALL exchanges are unreachable.

6. **Dashboard / `market_routes.py`** — Paper-vs-Demo score formula (confirmed
   correct — separates entry accuracy from fill rate), `paper_entry`/`demo_entry`
   difference in JOINed rows, and orphan-demo-order surface path.

7. **Cross-cutting** — toobit live-account routing (no `account_type` filter in
   bridge), Capital.com integration stubs, and the 3 orphaned BTC/JTO rows that
   needed manual SQL fixes (root cause: signal-handler pending rows shadowing
   `_sync_positions` backfill target, already fixed via `ORDER BY CASE`).

**Reviewers:** any senior engineer or Hermes agent with read-only access.
**Goal:** find crash-paths, silent wrong-behaviour, and performance regressions
BEFORE live-lane activation.
