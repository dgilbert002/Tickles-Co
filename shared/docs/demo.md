# Demo Trading — Implementation Plan

## Goal

Bridge the paper-trading competition to demo exchange execution via the MCP
server while keeping the competition leaderboard meaningful. The end state is
12 agents competing in paper, with the top 4 also trading on Bybit testnet.

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
