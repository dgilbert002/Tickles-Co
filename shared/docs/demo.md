# Demo Trading — Implementation Plan

## Goal

Bridge the paper-trading competition to demo exchange execution via the MCP
server while keeping the competition leaderboard meaningful. The end state is
12 agents competing in paper, with the top 4 also trading on Bybit testnet.

---

## System Context — What We Built (May 25-28, 2026)

This section exists so a **new context window** can understand the system
without re-reading the entire codebase. Read this before touching any code.

### 1. Interpretation Pipeline — How Charts Become Trades

```
Discord/Telegram → news_items → media_items (chart images)
    ↓
interpretation_service.py  (polls every 60s)
    ├── Text extraction: hashtag→symbol (#BTC→BTC/USDT)
    ├── Prefilter: cheap LLM gate (Gemini Flash) — "is this a chart?"
    ├── Vision LLM: reads chart image, extracts entry/SL/TP
    ├── Symbol router: exchange_router.py → unified_instruments lookup
    ├── Consensus: combines LLM + quant (candles) → direction+confidence
    └── write_signal_interpretation → signal_interpretations row
         ↓
    tracked_positions row  (status='pending')
         ↓
    position_monitor.py  (prices via candles/CCXT, checks SL/TP)
         ↓
    copy_trade_monitor.py  (paper trades for 12 agents)
         ↓
    competition_trades row
```

**Key files:**
- `shared/intelligence/interpretation_service.py` (5089 lines) — main daemon
- `shared/utils/exchange_router.py` (683 lines) — resolves LLM symbols → exchange perps
- `shared/intelligence/position_monitor.py` — 1m candle walk, SL/TP detection
- `shared/intelligence/copy_trade_monitor.py` (993 lines) — paper trade entry/exit
- `shared/utils/symbol_learner.py` (296 lines) — maps unknown symbols via LLM

**Prompts (DB: prompt_versions table, dashboard-editable):**
- `id=34, name=chart_prefilter` — concrete features, bias toward pass
- `id=23, name=chart_analysis, version=2026.05.24-DIscord` — semantic-role extraction
- `id=24, name=chart_analysis, version=2024-05-25=telegram-rose-v1` — Rose zones

### 2. Bots & Agents — How They Trade

12 agents compete. Each mirrors tracked_positions into paper positions with
their own sizing, leverage, and risk rules. All position tracking is in-memory
in `copy_trade_monitor.py`, persisted to `copy_agent_state` table.

**Agent configuration:** `shared/intelligence/copy_trade_monitor.py` lines 46-59

| Agent | Mode | Risk | Leverage | Concurrent | Description |
|-------|------|------|----------|------------|-------------|
| A: Spot Seq | spot_seq | Full balance | 1x | 1 | One trade at a time |
| B: Lev Parallel | lev_parallel | 5%/trade | SL-distance × 0.97 | 20 | Default leveraged |
| C: +BE Lock | lev_be_lock | 5%/trade | SL-distance × 0.97 → 100x | 20 | B + SL→BE at +5% |
| A+Opt | spot_seq_opt | Full balance | 1x | 1 | Optimized SL/TP multipliers |
| B+Opt | lev_parallel_opt | 5%/trade | SL-distance × 0.97 | 20 | Optimized + leveraged |
| C+Opt | lev_be_lock_opt | 5%/trade | SL-distance × 0.97 → 100x | 20 | Optimized + BE lock |
| CH: AI Vision | spot_seq_ch | Full balance | 1x | 1 | ChartHacker signals only |
| A×3 | spot_lev_3x | Full balance | 3x | 1 | Triple leverage, sequential |
| Rose A | spot_seq_rose | Full balance | 1x | 1 | Rose Telegram only |
| Rose B | lev_parallel_rose | 5%/trade | SL-distance × 0.97 | 20 | Rose + leveraged |
| Rose C | lev_be_lock_rose | 5%/trade | SL-distance × 0.97 → 100x | 20 | Rose + BE lock |
| D: 3% Lev Par | lev_parallel_3pct | 3%/trade | SL-distance × 0.97 | 33 | Smaller sizing variant |

**Leverage formula (lines 501-505):**
```python
sl_dist = abs(entry - sl) / entry
raw_lev = (1.0 / sl_dist) * 0.97   # 0.97 = 3% safety margin
leverage = min(raw_lev, 100.0)
```

**BE Lock (lines 606-616):** When a position reaches +5% profit, SL moves to
breakeven and leverage resets to 100x with proportionally reduced allocated
capital (notional stays constant).

**Agent routing (lines 890-905):** Rose agents only trade `actor_id=jarvais_rose_ch`.
CH agents only trade `actor_id=jarvais_chart_hacker`. Regular agents trade all
except Rose and CH positions.

**Files:**
- `shared/intelligence/copy_trade_monitor.py` — agent config, entry/exit, BE lock, scoring
- `shared/intelligence/position_monitor.py` — candle-based SL/TP checking

### 3. Dashboard — What the UI Shows

Single-page app at `http://127.0.0.1:3101/`, proxied through Tailscale at
`https://vmi3220412.trout-goblin.ts.net/dashboard/`.

**Tabs:**
- Trading Floor — overview: signals table, competition leaders, live positions, Discord feed
- Signals & Radar — unified view with list/card toggle, real Δ-to-entry
- Competition — 12-agent leaderboard with expandable trade history
- Positions — live + historic positions with pagination
- Trader Intel — per-trader stats (win rate, P&L, coin breakdowns)
- Discord/Telegram Feed — paginated messages (40/page, Load more button)
- AI Learning — post-mortems, memory feed, agent brains
- Ops & Cost — cost chart, service health
- Settings — AI models, sources & tracking, prompts library, app config

**Key files:**
- `shared/dashboard/web/index.html` (207 lines) — single-page layout
- `shared/dashboard/static/app.js` (1810 lines) — all rendering logic
- `shared/dashboard/static/app.css` (396 lines) — dark theme with light mode toggle
- `shared/dashboard/market_routes.py` (1312 lines) — API endpoints
- `shared/dashboard/snapshot.py` (2058 lines) — data aggregation for snapshot API

**Key APIs:**
- `/api/snapshot` — signals, positions, stats for Trading Floor
- `/api/unified-signals` — all signals with live prices and delta-to-entry
- `/api/competitions` + `/api/competition-agent` — leaderboard + agent detail
- `/api/entry-radar` — pending positions sorted by distance to entry
- `/api/traders-intel` — per-trader statistics aggregated from tracked_positions

### 4. Symbol Learning System — Unknown Coin Resolution

When the LLM reads a coin not in our database, it goes through:

```
1. Check symbol_mappings table → if found, route directly
2. If new → insert into unresolved_symbols (status='pending') → cancel trade
3. Every 12h (cron: symbol-learner): Gemini Flash maps unknowns to instruments
4. Next time that symbol appears → instant lookup, no LLM cost
```

**Files:**
- `shared/utils/symbol_learner.py` (296 lines)
- `shared/utils/symbol_learner_cron.py` (68 lines)
- `shared/mcp/tools/symbols.py` (638 lines) — 7 MCP tools for symbol management

### 5. MCP Server — Tool Registry

100 MCP tools registered across HTTP (`:7777`) and stdio. Key modules:

- `shared/mcp/tools/trading.py` — execution.submit, treasury.evaluate, banker
- `shared/mcp/tools/routing.py` — router.resolve, instruments.search, positions.*
- `shared/mcp/tools/symbols.py` — symbol.cleanup, .lookup, .mappings.*, .learn.*
- `shared/mcp/tools/intelligence.py` — chart analysis, signal interpretation

### 6. Execution Infrastructure — What Exists for Demo

**Already built:**
- `shared/execution/ccxt_adapter.py` (319 lines) — CcxtExecutionAdapter with
  sandbox mode, submit/cancel/poll. Uses credentials from `.env`.
- `shared/execution/router.py` (357 lines) — routes intents through adapters,
  persists every event. Currently paper-only at L179: `adapters={"paper": paper}`.
- `shared/execution/protocol.py` (291 lines) — ExecutionIntent, OrderUpdate types
- `shared/utils/credentials.py` — loads API keys by exchange + account name
  (supports BYBIT_DEMO_API_KEY, BYBIT_LIVE_API_KEY, etc.)

**Credentials in `.env`:**
```
BYBIT_DEMO_API_KEY, BYBIT_DEMO_API_SECRET
BYBIT_LIVE_API_KEY, BYBIT_LIVE_API_SECRET
BLOFIN_DEMO_API_KEY, BLOFIN_DEMO_API_SECRET
BLOFIN_LIVE_API_KEY, BLOFIN_LIVE_API_SECRET
BITGET_API_KEY, BITGET_API_SECRET
CAPITAL_COM_API_KEY, CAPITAL_COM_API_SECRET
```

**The gap:** CCXT adapter is built but never registered in the router (L179).
SL/TP orders are not supported in the adapter. The copy_trade_monitor has its
own paper sim and never touches the execution pipeline.

### 7. Current State (May 28, 2026)

- Competition: 343 trades across 12 agents, top agent +155%
- All services running: interpretation, position-monitor, copy-trade-monitor,
  candle-daemon, dashboard, MCP daemon
- Branch: `Signals_Rader_Merge` on GitHub (`dgilbert002/Tickles-Co`)
- Working directory: `/opt/tickles`

---

## Context

- Competition started May 20. 6 days of trading, 343 trades, 12 agents.
- Top agents: CH AI Vision (+155%), Opt Lev Parallel (+50%)
- CCXT adapter exists (319 lines). Router is paper-only. No SL/TP on adapter.
- Credentials for Bybit demo, BloFin demo, Bitget already in `.env`.

## Phase 1 — CCXT Adapter: Stop-Loss & Take-Profit (2-3 days)

**This is the gating item. Nothing else matters until stop orders work.**

Bybit supports native SL/TP as params on the create order call:
```python
client.create_order(
    symbol="SOL/USDT:USDT",
    type="market",
    side="buy",
    amount=1.0,
    params={
        "stopLoss": 80.0,      # stop market at $80
        "takeProfit": 100.0,   # limit at $100
    }
)
```

### Tasks

1. Add `stop_loss` and `take_profit` fields to `ExecutionIntent` protocol
   - File: `shared/execution/protocol.py`
   - Fields: `stop_loss: Optional[float]`, `take_profit: Optional[float]`
   
2. Wire SL/TP params into `CcxtExecutionAdapter.submit()`
   - File: `shared/execution/ccxt_adapter.py`
   - After create_order, if SL/TP are set, embed them in params
   - Handle exchange-specific differences (Bybit vs BloFin vs Bitget)
   
3. Add `set_sl_tp()` method to adapter for modifying existing positions
   - File: `shared/execution/ccxt_adapter.py`
   - Needed for: BE lock (move SL to breakeven), partial TP
   
4. Test on Bybit testnet
   - Open a position with SL/TP, verify they appear on exchange
   - Trigger a stop loss, verify fill
   - Trigger take profit, verify fill
   - Test BE lock: modify SL on open position

## Phase 2 — Router Wiring (1 day)

Wire the ccxt adapter into the router so MCP execution tools can target
demo and live exchanges, not just paper.

### Tasks

1. Register `CcxtExecutionAdapter(sandbox=True)` in `_get_router()`
   - File: `shared/mcp/tools/trading.py`
   - Key: `adapters={"paper": paper, "demo": CcxtExecutionAdapter(sandbox=True)}`

2. Expose `adapter` param on `execution.submit`
   - Already has the field, just read it from params instead of hardcoding "paper"
   
3. Map `accountName` → credentials
   - `accountName="DEMO"` → `BYBIT_DEMO_API_KEY` from credentials
   - Already supported by `Credentials.get_client()`

4. Test with MCP: `execution.submit(adapter="demo", accountName="DEMO", ...)`

## Phase 3 — Bridge Daemon (2 days)

**Do NOT modify the copy_trade_monitor.** Build a separate daemon that reads
paper trades and mirrors them to real exchange. The competition runs untouched
— the bridge is optional and independent.

### Architecture

```
 ┌─────────────────────────┐
 │ copy_trade_monitor.py   │ ← UNCHANGED
 │ (paper sim, candles)    │
 └──────────┬──────────────┘
            │ writes paper entries
            ▼
 ┌─────────────────────────┐
 │ competition_trades (DB) │
 └──────────┬──────────────┘
            │ reads new paper entries
            ▼
 ┌─────────────────────────┐
 │ bridge_daemon.py  (NEW) │
 │                         │
 │ 1. Reads new entries    │
 │ 2. Filters whitelist    │
 │ 3. Treasury check       │
 │ 4. Submits to CCXT      │
 │ 5. Records exchange_id   │
 └──────────┬──────────────┘
            │
            ▼
 ┌─────────────────────────┐
 │ execution router        │
 │  ├── paper adapter      │
 │  └── ccxt adapter (demo)│
 └─────────────────────────┘
```

### Tasks

1. Create `shared/daemons/demo_bridge.py`
   - Polls `competition_trades` for new entries
   - Whitelist: only submit for agents in `DEMO_AGENTS` config
   - Calls `execution.submit(adapter="demo", accountName="DEMO", ...)`
   - Records `exchange_order_id` back to `competition_trades`
   
2. Agent config for demo
   ```python
   DEMO_AGENTS = {
       "copy_opt_lev_parallel": {"capital": 200, "max_positions": 10},
       "copy_opt_lev_be_lock":  {"capital": 200, "max_positions": 10},
       "copy_ch_ai_vision":     {"capital": 200, "max_positions": 5},
       "copy_spot_lev_3x":      {"capital": 200, "max_positions": 5},
   }
   ```
   Small capital ($200 each) for safety during testing.

3. Position reconciliation on startup
   - Query exchange for open positions
   - Compare with `competition_trades` entries
   - Log discrepancies, don't auto-fix

4. Health monitoring
   - Bridge daemon heartbeat to systemd
   - Alert if bridge is down for >5 min
   - Alert if exchange rejects >3 consecutive orders

## Phase 4 — Reconciliation & Trust Building

Before going live, build confidence by comparing paper vs exchange outcomes.

### Tasks

1. Add `exchange_pnl` column to `competition_trades`
   - Bridge daemon writes actual exchange P&L after close
   - Dashboard shows both: paper P&L vs exchange P&L

2. Daily reconciliation report
   - Slippage: entry price difference (paper vs actual fill)
   - Fee comparison: simulated fees vs real fees
   - Order rejections: count and reasons
   - P&L drift: cumulative difference

3. Trust gates
   - Don't increase capital until slippage < 0.5% for 7 days
   - Don't add more agents until rejection rate < 2%
   - Don't go live until paper/exchange P&L correlation > 0.95

## What We Do NOT Do

- **Do not modify the copy_trade_monitor.** The competition is the source of
  truth. The bridge is a mirror, not a replacement.
- **Do not compare paper and exchange agents on the same leaderboard.**
  Different environments, different outcomes. Separate views.
- **Do not enable live trading until Phase 4 trust gates pass.**
- **Do not run Rose agents on real money.** They're -46% in paper. Fix them first.
- **Do not attempt the "intent queue" from the other plan.** Premature
  abstraction. The bridge daemon polling model is simpler and sufficient
  for 12 agents.

## Estimated Timeline

| Phase | Duration | Depends On |
|-------|----------|------------|
| 1: SL/TP on CCXT adapter | 2-3 days | — |
| 2: Router wiring | 1 day | Phase 1 |
| 3: Bridge daemon | 2 days | Phase 2 |
| 4: Reconciliation | 2 day | Phase 3 |
| **Total** | **7-9 days** | |

## Deployment States

```
Phase 1 done → SL/TP orders work on Bybit testnet (manual test)
Phase 2 done → MCP execution.submit(adapter="demo") works
Phase 3 done → Top 4 agents executing on demo ($200 each, 24/7)
Phase 4 done → Dashboard shows paper vs exchange P&L comparison
              → Trust gates pass → ready for more capital / live
```
