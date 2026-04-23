# Rule 1: Backtest ≡ Live — End-to-End Walkthrough

This document traces the **complete lifecycle** from "agent discovers a strategy" to
"two days later, we prove the live trade matches the backtest within 0.1%".

---

## Table of Contents

1. [Phase A: Agent Discovers a Strategy via Backtest](#phase-a)
2. [Phase B: Strategy Goes Live — The Forward-Test Shadow](#phase-b)
3. [Phase C: Agent Executes a Live Trade](#phase-c)
4. [Phase D: Two Days Pass — The Validation Engine](#phase-d)
5. [Phase E: Re-Running the Backtest to Confirm](#phase-e)
6. [Phase F: Agent Learns — Strategy Review & Autopsy](#phase-f)
7. [Why This Guarantees 99.9% Parity](#why-parity)
8. [Tables & Data Flow Diagram](#tables-diagram)

---

## Phase A: Agent Discovers a Strategy via Backtest <a name="phase-a"></a>

### Step 1: Agent lists available strategies

The agent calls the MCP tool `strategy.list`:

```json
{ "name": "strategy.list" }
```

This hits [`_handle_strategy_list()`](shared/mcp/tools/backtest.py:629) which calls
[`_list_strategies()`](shared/mcp/tools/backtest.py:88). The function iterates over
`shared/backtest/strategies/single_indicator.py`'s `STRATEGIES` dict and returns cards
like:

```json
{
  "strategies": [
    {"name": "sma_cross", "description": "SMA fast/slow crossover"},
    {"name": "rsi_reversal", "description": "RSI overbought/oversold reversal"},
    ...
  ]
}
```

### Step 2: Agent composes a backtest spec

The agent calls `backtest.compose_spec`:

```json
{
  "symbol": "BTC/USDT",
  "source": "bybit",
  "timeframe": "1h",
  "strategy": "sma_cross",
  "direction": "long",
  "params": {"fast_period": 10, "slow_period": 30},
  "risk": {
    "initial_capital": 10000,
    "position_pct": 100,
    "leverage": 1,
    "fee_bps": 5,
    "slip_bps": 2,
    "sl": 2.0,
    "tp": 4.0
  }
}
```

This flows through [`_compose_spec()`](shared/mcp/tools/backtest.py:347) which:
1. Resolves the strategy via [`get_strategy()`](shared/backtest/strategies/single_indicator.py:121)
2. Resolves the engine via [`engines.get("classic")`](shared/backtest/engines/__init__.py:54)
3. Builds a validated [`BacktestConfig`](shared/backtest/engine.py:72) dataclass
4. Returns the spec with a `param_hash` (SHA256 of all parameters)

### Step 3: Agent submits the backtest job

The agent calls `backtest.submit` with the composed spec. This enqueues a job via
[`BacktestQueue.enqueue()`](shared/backtest/queue.py:171) into Redis
(`tickles:bt:queue:pending`).

A worker process ([`worker.py`](shared/backtest/worker.py:168)) picks up the job:

```
process(job, ch):
  1. Build BacktestConfig from job payload          → line 174
  2. Check ClickHouse dedup via param_hash          → line 196
  3. Load candles from Postgres via load_candles_sync() → line 200
  4. Resolve strategy function via get_strategy()   → line 211
  5. Call run_backtest(df, strategy, cfg)           → line 221
  6. Write result to ClickHouse via ch.write_result() → line 223
```

### Step 4: The backtest engine runs — bar by bar

[`run_backtest()`](shared/backtest/engine.py:419) is the core loop. Here's exactly
what happens for each of the ~8,760 hourly bars in a year of BTC data:

```python
executor = BacktestExecutor(cfg)     # line 496 — stateful executor

for i in range(n):                   # line 499 — iterate every bar
    # 1. SL/TP check on current bar's high/low
    executor.process_intrabar(ts[i], open[i], high[i], low[i], close[i])

    # 2. Record equity
    equity_curve[i] = executor.get_equity(close[i])

    # 3. Process signal → fill at NEXT bar's open (no look-ahead!)
    if i + 1 < n:
        executor.process_signal(
            sig=raw_sig[i],           # signal from bar i
            entry_sig=entry_sig[i],
            crash_blocked=crash_mask[i],
            next_bar_ts=ts[i+1],      # filled at bar i+1
            next_bar_open=open[i+1],  # ← THIS IS THE KEY
            next_bar_bid=open_bid[i+1],
            next_bar_ask=close_ask[i+1],
        )
```

**Critical parity detail**: The signal generated at the CLOSE of bar `i` is filled at
the OPEN of bar `i+1`. This eliminates look-ahead bias and is the exact same logic
used in forward-testing (Phase B).

### Step 5: Result is written to ClickHouse

[`ClickHouseWriter.write_result()`](shared/backtest/ch_writer.py:114) writes:

| Table | What's Stored |
|-------|--------------|
| `backtest_runs` | Run metadata: param_hash, symbol, timeframe, Sharpe, P&L, etc. |
| `backtest_trades` | Every trade: entry_at, entry_px, exit_at, exit_px, direction, qty, pnl, fees, exit_reason |

The `param_hash` is deterministic — same params always produce the same hash, enabling
dedup and later re-runs to compare.

### Step 6: Agent reviews results

The agent calls `backtest.top_k` to see the best runs:

```json
{ "sort": "sharpe", "limit": 5 }
```

This reads from ClickHouse via [`_top_k()`](shared/mcp/tools/backtest.py:556) and
returns ranked results. The agent sees:

```json
{
  "results": [{
    "run_id": "uuid-...",
    "param_hash": "a3f2...",
    "symbol": "BTC/USDT",
    "sharpe": 1.87,
    "pnl_pct": 34.2,
    "total_trades": 47,
    "max_drawdown_pct": -8.3
  }]
}
```

The agent says: **"SMA(10,30) cross on BTC 1h looks good. Sharpe 1.87, 47 trades,
max DD 8.3%. I'm going live."**

---

## Phase B: Strategy Goes Live — The Forward-Test Shadow <a name="phase-b"></a>

### Step 7: Start a forward-test (shadow) run

The agent (or automated system) starts a forward-test via
[`ForwardTestEngine.start_run()`](shared/backtest/forward_test.py:38):

```python
forward_run_id = await engine.start_run(
    strategy_id="sma_cross",
    cfg=BacktestConfig(  # SAME config as the backtest!
        symbol="BTC/USDT", source="bybit", timeframe="1h",
        direction="long", initial_capital=10000,
        position_pct=100, leverage=1, fee_taker_bps=5,
        slippage_bps=2, stop_loss_pct=2.0, take_profit_pct=4.0,
        strategy_name="sma_cross",
        indicator_params={"fast_period": 10, "slow_period": 30},
        ...
    ),
    instrument_id=42
)
```

This creates:
1. A **new `BacktestExecutor`** with the exact same config as the historical backtest
2. A row in ClickHouse `backtest_forward_runs` table (status='active')
3. An empty candle buffer for this run

### Step 8: Every new candle triggers the shadow loop

When the candle collector writes a new 1h bar to Postgres, the
[`ForwardTestEngine.on_candle()`](shared/backtest/forward_test.py:60) fires:

```python
async def on_candle(self, symbol, timeframe, candle):
    # Parse the candle
    ts = pd.to_datetime(candle["snapshotTime"], utc=True)
    o, h, l, c = candle prices...

    for run_id, executor in self.executors.items():
        if cfg.symbol != symbol or cfg.timeframe != timeframe:
            continue

        # 1. SL/TP/Funding check on the bar that just closed
        executor.process_intrabar(ts, o, h, l, c)
        #    ↑ SAME BacktestExecutor.process_intrabar() as backtest!

        # 2. Persist any newly closed shadow trades to ClickHouse
        self._persist_new_trades(run_id)

        # 3. Compute signal from buffered candle history
        sig, entry_sig = self._compute_signals(run_id)

        # 4. Apply PREVIOUS bar's signal at THIS bar's open
        #    (parity with backtest: signal at bar i → fill at bar i+1 open)
        pending = getattr(executor, "_pending_signal", None)
        if pending is not None:
            executor.process_signal(
                sig=pending["sig"],
                entry_sig=pending["entry_sig"],
                crash_blocked=pending["crash_blocked"],
                next_bar_ts=ts,         # ← current bar's timestamp
                next_bar_open=o,        # ← current bar's open price
                next_bar_bid=bid,
                next_bar_ask=ask,
            )
            # ↑ SAME BacktestExecutor.process_signal() as backtest!

        # 5. Store current signal for the NEXT candle
        executor._pending_signal = {sig, entry_sig, crash_blocked}

        # 6. Update run stats in ClickHouse
        self.ch.update_forward_run_stats(run_id, equity, return_pct, trade_count)
```

**The parity guarantee**: Both the historical backtest and the forward-test call the
exact same two methods on the exact same `BacktestExecutor` class:
- `process_intrabar()` — for SL/TP/funding
- `process_signal()` — for entries/exits at next-bar open

The only difference is *where the data comes from*: the backtest reads all bars from
a DataFrame at once, while the forward-test receives them one at a time via `on_candle()`.

### Step 9: Shadow trades land in ClickHouse

When a shadow trade closes, [`_persist_new_trades()`](shared/backtest/forward_test.py:140)
writes it to `backtest_forward_trades` in ClickHouse via
[`write_forward_trade()`](shared/backtest/ch_writer.py:177):

| Column | Value |
|--------|-------|
| `forward_run_id` | UUID of the shadow run |
| `strategy_id` | "sma_cross" |
| `entry_at` | 2026-04-20 14:00:00 UTC |
| `entry_px` | 87450.12 |
| `exit_at` | 2026-04-20 22:00:00 UTC |
| `exit_px` | 90948.12 |
| `direction` | "long" |
| `exit_reason` | "tp" |
| `pnl_abs` | 3498.00 |
| `fees` | 87.45 |

---

## Phase C: Agent Executes a Live Trade <a name="phase-c"></a>

### Step 10: Treasury evaluates the trade intent

The agent calls `treasury.evaluate`:

```json
{
  "companyId": "jarvais",
  "agentId": "agent-btc-01",
  "symbol": "BTC/USDT",
  "venue": "bybit",
  "side": "buy",
  "notionalUsd": 10000,
  "strategyRef": "sma_cross"
}
```

This flows through [`_treasury_evaluate()`](shared/mcp/tools/trading.py:425):

1. **Capability check** — [`CapabilityChecker.evaluate()`](shared/trading/capabilities.py:223)
   verifies the agent is allowed to trade BTC/USDT with $10K notional
2. **Banker balance** — [`Banker.available_capital_usd()`](shared/trading/banker.py:255)
   checks the paper wallet has sufficient funds
3. **Position sizing** — [`size_intent()`](shared/trading/sizer.py:162) calculates
   the exact quantity considering fee, slippage, leverage, and risk limits
4. **Decision** — [`TreasuryDecision`](shared/trading/treasury.py:47) returns
   approved/skipped + the sized intent

### Step 11: Order submission through the execution router

The agent calls `execution.submit`:

```json
{
  "companyId": "jarvais",
  "agentId": "agent-btc-01",
  "symbol": "BTC/USDT",
  "side": "buy",
  "quantity": 0.1143,
  "venue": "bybit",
  "orderType": "market",
  "dryRun": false,
  "I_am_paper": true,
  "strategyRef": "sma_cross"
}
```

This flows through [`_execution_submit()`](shared/mcp/tools/trading.py:525):

1. Gets latest market price from Postgres
2. Constructs an [`ExecutionIntent`](shared/execution/protocol.py:100)
3. Routes through [`ExecutionRouter.submit()`](shared/execution/router.py:85)
4. The router delegates to [`PaperExecutionAdapter.submit()`](shared/execution/paper.py:133)
5. Paper adapter simulates a fill at current ask price + fee
6. The router persists the order to Postgres via [`ExecutionStore.insert_order()`](shared/execution/store.py:198)
7. When the fill comes, [`ExecutionStore.insert_fill()`](shared/execution/store.py:248) records it

### Step 12: Live trade lands in Postgres

The closed trade ends up in the `trades` table in the company database
(`tickles_jarvais`):

| Column | Value |
|--------|-------|
| `id` | 42 |
| `strategy_id` | 5 (sma_cross) |
| `symbol` | BTC/USDT |
| `direction` | long |
| `status` | closed |
| `opened_at` | 2026-04-20 14:00:03 UTC |
| `entry_price` | 87451.50 |
| `closed_at` | 2026-04-20 22:00:01 UTC |
| `exit_price` | 90947.80 |
| `exit_reason` | tp |
| `quantity` | 0.1143 |
| `net_pnl` | 3496.82 |
| `fees` | 90.12 |

**Notice the tiny differences from the shadow trade**:
- Entry: 87451.50 (live) vs 87450.12 (shadow) — $1.38 slippage
- Exit: 90947.80 (live) vs 90948.12 (shadow) — $0.32 difference
- Fees: $90.12 (live) vs $87.45 (shadow) — $2.67 difference
- P&L: $3496.82 (live) vs $3498.00 (shadow) — $1.18 delta

---

## Phase D: Two Days Pass — The Validation Engine <a name="phase-d"></a>

### Step 13: Trigger validation

Two days later, the agent (or a scheduled job) calls the MCP tool:

```json
{
  "name": "trading_trade_validate",
  "arguments": {
    "companyId": "jarvais",
    "tradeId": 42
  }
}
```

This hits [`_trade_validate()`](shared/mcp/tools/trading.py:1133) which creates a
[`ValidationEngine`](shared/trading/validation.py:17) and calls
[`validate_trade(42)`](shared/trading/validation.py:35).

### Step 14: Validation engine fetches the live trade

```python
trade = await pool.fetch_one(
    "SELECT * FROM trades WHERE id = $1 AND status = 'closed'",
    (42,)
)
```

Returns the live trade from Postgres (the one in Step 12).

### Step 15: Validation engine finds the shadow trade

[`_find_shadow_trade()`](shared/trading/validation.py:68) queries ClickHouse:

```sql
SELECT * FROM backtest_forward_trades
WHERE strategy_id = '5'
  AND entry_at <= '2026-04-20 14:00:03'     -- live entry time
  AND entry_at >= '2026-04-20 14:00:03' - INTERVAL 10 SECOND
ORDER BY entry_at DESC
LIMIT 1
```

The **10-second look-back window** accounts for execution latency: the live trade
fills a few seconds after the bar open, but the shadow trade fills exactly at the
bar open. This window reliably pairs them.

Returns the shadow trade from ClickHouse (the one in Step 9).

### Step 16: Compare and attribute drift

[`_compare_trades()`](shared/trading/validation.py:99) calculates:

```python
entry_delta  = 87451.50 - 87450.12  = 1.38    # live filled slightly higher
exit_delta   = 90947.80 - 90948.12  = -0.32   # live exited slightly lower
pnl_delta    = 3496.82 - 3498.00   = -1.18    # live made $1.18 less

notional     = 87451.50 * 0.1143    = 9,995.41
pnl_delta_pct = -1.18 / 9995.41 * 100 = -0.012%  # ← WITHIN 0.1%!
```

**Rule 1 check passes**: `|pnl_delta_pct| = 0.012% < 0.1%` ✅

### Step 17: Record the validation

[`_record_validation()`](shared/trading/validation.py:128) writes to Postgres
`trade_validations` table:

| Column | Value |
|--------|-------|
| `trade_id` | 42 |
| `strategy_id` | 5 |
| `signal_match` | true |
| `entry_price_delta` | 1.38 |
| `exit_price_delta` | -0.32 |
| `pnl_delta` | -1.18 |
| `pnl_delta_pct` | -0.012 |
| `slippage_contribution` | 0.158 |
| `fee_contribution` | 2.67 |
| `data_drift_detected` | false |
| `validated_at` | 2026-04-22 05:30:00 UTC |

---

## Phase E: Re-Running the Backtest to Confirm <a name="phase-e"></a>

### Step 18: Re-run the historical backtest with extended dates

The agent calls `backtest.submit` again with the **same param_hash** but dates that
now include the two days that have passed:

```json
{
  "symbol": "BTC/USDT",
  "source": "bybit",
  "timeframe": "1h",
  "strategy": "sma_cross",
  "direction": "long",
  "params": {"fast_period": 10, "slow_period": 30},
  "start_date": "2025-01-01",
  "end_date": "2026-04-22",     ← extended to include the live period
  "risk": { ... same as before ... }
}
```

The worker runs [`run_backtest()`](shared/backtest/engine.py:419) with the extended
candle data. Because the strategy is **deterministic** (same params → same signals),
and the execution logic is **identical** (same `BacktestExecutor`), the backtest
produces a trade at the exact same bar as the live trade:

| Field | Backtest Re-Run | Live Trade | Delta |
|-------|----------------|------------|-------|
| Entry time | 2026-04-20 14:00 | 2026-04-20 14:00:03 | 3 sec |
| Entry price | 87450.12 | 87451.50 | $1.38 |
| Exit time | 2026-04-20 22:00 | 2026-04-20 22:00:01 | 1 sec |
| Exit price | 90948.12 | 90947.80 | $0.32 |
| Exit reason | tp | tp | match ✅ |
| P&L delta | — | — | 0.012% ✅ |

**The backtest re-run confirms**: the same signal fired, the same entry/exit occurred,
and the P&L delta is within the 0.1% threshold. The strategy is consistent.

### Step 19: Dedup ensures we don't double-count

Because the `param_hash` is the same (same strategy, same params, same risk settings),
[`ClickHouseWriter.run_exists()`](shared/backtest/ch_writer.py:228) would return
`true` for the original date range. But since the end_date changed, a new `param_hash`
is generated, and the extended run is stored as a new row — allowing comparison.

---

## Why This Guarantees 99.9% Parity <a name="why-parity"></a>

The system achieves Rule 1 (Backtest ≡ Live within 0.1%) through **five structural
guarantees**:

### 1. Shared Execution Logic
Both the historical backtest ([`run_backtest()`](shared/backtest/engine.py:419)) and
the forward-test ([`ForwardTestEngine.on_candle()`](shared/backtest/forward_test.py:60))
call the **exact same methods** on the **exact same class**:

```
BacktestExecutor.process_intrabar()  → SL/TP/funding
BacktestExecutor.process_signal()    → entries/exits at next-bar open
BacktestExecutor._close_position()   → trade recording
```

There is no "live execution path" vs "backtest execution path" — there is only one
path, shared by both.

### 2. Next-Bar-Open Fill Semantics
In the backtest loop (line 510-518):
```python
executor.process_signal(
    sig=raw_sig[i],              # signal from bar i
    next_bar_open=open_px[i+1],  # fill at bar i+1's open
)
```

In the forward-test loop (line 110-120):
```python
pending = executor._pending_signal  # signal from PREVIOUS bar
executor.process_signal(
    sig=pending["sig"],
    next_bar_open=o,              # fill at CURRENT bar's open
)
```

Both fill at the **next bar's open price**. The forward-test achieves this by
buffering the signal in `_pending_signal` and only applying it when the next candle
arrives — eliminating lookahead bias.

### 3. Deterministic Strategy Functions
Strategies like [`sma_cross()`](shared/backtest/strategies/single_indicator.py:34)
are pure functions: same DataFrame + same params → same signal Series. No randomness,
no state, no external API calls. This means re-running the backtest with the same
data always produces the same signals.

### 4. Slippage & Fee Modeling
The `BacktestExecutor` applies the same slippage and fee model in both contexts:
- Entry fills at `ask * (1 + slip_rate)` for longs
- Exit fills at `bid * (1 - slip_rate)` for longs
- Fee debited at `fee_taker_bps / 10000` on notional

The live trade naturally incurs real slippage and fees. The validation engine
attributes the difference:
- `slippage_contribution`: how much of the delta came from fill price differences
- `fee_contribution`: how much came from fee differences

### 5. Data Integrity (Candle Hash)
The `candle_data_hash` column in `backtest_forward_runs` records a hash of the
candle data used. When the backtest is re-run, the same hash should appear for the
overlapping period. If it doesn't, `data_drift_detected = true` and the validation
flags a data integrity issue (e.g., a candle was corrected/replaced after the fact).

---

## Tables & Data Flow Diagram <a name="tables-diagram"></a>

```
┌─────────────────────────────────────────────────────────────────────┐
│                         POSTGRES (tickles_jarvais)                  │
│                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐  │
│  │ candles_1h   │  │   trades     │  │  trade_validations       │  │
│  │──────────────│  │──────────────│  │──────────────────────────│  │
│  │ snapshotTime │  │ id           │  │ trade_id                 │  │
│  │ openPrice    │  │ strategy_id  │  │ strategy_id              │  │
│  │ highPrice    │  │ symbol       │  │ signal_match             │  │
│  │ lowPrice     │  │ direction    │  │ entry_price_delta        │  │
│  │ closePrice   │  │ status       │  │ exit_price_delta         │  │
│  │ openBid      │  │ opened_at    │  │ pnl_delta                │  │
│  │ closeAsk     │  │ entry_price  │  │ pnl_delta_pct  ← 0.1%   │  │
│  │ volume       │  │ closed_at    │  │ slippage_contribution    │  │
│  │ instrument_id│  │ exit_price   │  │ fee_contribution         │  │
│  └──────────────┘  │ exit_reason  │  │ data_drift_detected      │  │
│        │           │ quantity     │  │ validated_at             │  │
│        │           │ net_pnl      │  └──────────────────────────┘  │
│        │           │ fees         │          ↑                      │
│        │           └──────┬───────┘          │                      │
│        │                  │                  │                      │
│        │          Step 12: Live trade        │                      │
│        │                  │                  │                      │
└────────┼──────────────────┼──────────────────┼──────────────────────┘
         │                  │                  │
    load_candles_sync()     │          _record_validation()
         │                  │                  │
         ▼                  │                  │
┌─────────────────────────────────────────────────────────────────────┐
│                         CLICKHOUSE (backtests)                      │
│                                                                     │
│  ┌──────────────────┐  ┌────────────────────┐  ┌────────────────┐  │
│  │ backtest_runs    │  │ backtest_trades    │  │ backtest_      │  │
│  │──────────────────│  │────────────────────│  │ forward_runs   │  │
│  │ run_id           │  │ run_id             │  │────────────────│  │
│  │ param_hash       │  │ entry_at           │  │ forward_run_id │  │
│  │ symbol           │  │ entry_px           │  │ strategy_id    │  │
│  │ timeframe        │  │ exit_at            │  │ param_hash     │  │
│  │ sharpe           │  │ exit_px            │  │ status         │  │
│  │ pnl_pct          │  │ direction          │  │ current_balance│  │
│  │ total_trades     │  │ qty                │  │ total_trades   │  │
│  │ max_drawdown_pct │  │ pnl_abs            │  └───────┬────────┘  │
│  └──────────────────┘  │ fees               │          │           │
│         │              │ exit_reason        │          │           │
│         │              └────────────────────┘          │           │
│         │                      │                       │           │
│  Step 5: Historical    Step 5: Historical       Step 9: Shadow     │
│  backtest result       backtest trades          run metadata       │
│                                                        │           │
│                        ┌────────────────────┐          │           │
│                        │ backtest_          │          │           │
│                        │ forward_trades     │          │           │
│                        │────────────────────│          │           │
│                        │ forward_run_id     │◄─────────┘           │
│                        │ strategy_id        │                      │
│                        │ entry_at           │                      │
│                        │ entry_px           │                      │
│                        │ exit_at            │                      │
│                        │ exit_px            │                      │
│                        │ direction          │                      │
│                        │ pnl_abs            │                      │
│                        │ fees               │                      │
│                        │ exit_reason        │                      │
│                        └────────┬───────────┘                      │
│                                 │                                  │
│                          Step 9: Shadow trades                     │
│                                 │                                  │
└─────────────────────────────────┼──────────────────────────────────┘
                                  │
                           _find_shadow_trade()
                           (10-second window match)
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │   VALIDATION RESULT      │
                    │──────────────────────────│
                    │ signal_match: true       │
                    │ entry_delta: $1.38       │
                    │ exit_delta: -$0.32       │
                    │ pnl_delta: -$1.18        │
                    │ pnl_delta_pct: -0.012%   │
                    │                          │
                    │ ✅ WITHIN 0.1% THRESHOLD │
                    └──────────────────────────┘
```

---

## Phase F: Agent Learns — Strategy Review & Autopsy <a name="phase-f"></a>

Validation tells you *whether* a trade matched. Review and autopsy tell you
*why* it matched (or didn't) and *what to do about it*.

### Step 20: Strategy Review — The Verdict

After accumulating enough validated trades, the agent calls
`trading_strategy_review`:

```json
{
  "name": "trading_strategy_review",
  "arguments": {
    "companyId": "jarvais",
    "strategyId": 5,
    "minTrades": 5
  }
}
```

This flows through [`_strategy_review()`](shared/mcp/tools/trading.py) →
[`ValidationEngine.review_strategy()`](shared/trading/validation.py:82).

The engine does three things:

**1. Aggregate all validations** via [`_aggregate_validations()`](shared/trading/validation.py):

| Metric | Value |
|--------|-------|
| `total_validated` | 47 |
| `rule1_passes` | 44 |
| `rule1_failures` | 3 |
| `rule1_pass_rate` | 93.6% |
| `avg_pnl_delta_pct` | 0.018% |
| `avg_slippage_contribution` | $0.42 |
| `avg_fee_contribution` | $1.23 |
| `worst_trade.pnl_delta_pct` | 0.38% |

**2. Compare backtest vs live profitability** via [`_compare_profitability()`](shared/trading/validation.py):

| Metric | Backtest | Live | Delta |
|--------|----------|------|-------|
| Win rate | 65% | 62% | -3% |
| Total P&L | +$3,420 | +$3,180 | -$240 |
| Primary drift source | — | — | fees |

**3. Compute a verdict** via [`_compute_verdict()`](shared/trading/validation.py):

| Condition | Verdict |
|-----------|---------|
| Rule 1 pass rate ≥ 90% AND avg delta < 0.05% | **CONTINUE** |
| Rule 1 pass rate ≥ 70% | **CAUTION** |
| Rule 1 pass rate < 70% | **STOP** |
| Fewer than `min_trades` validated | **CAUTION** |

In our example: 93.6% pass rate + 0.018% avg delta → **CONTINUE** ✅

The response includes **recommendations**:

```json
{
  "verdict": "CONTINUE",
  "recommendations": [
    "Rule 1 pass rate is excellent (>95%). Strategy is safe to scale up.",
    "Fee drift detected. Verify the exchange fee tier matches fee_taker_bps."
  ]
}
```

### Step 21: Strategy Autopsy — The Deep Dive

When the agent wants to understand *exactly where* drift is happening, it calls
`trading_strategy_autopsy`:

```json
{
  "name": "trading_strategy_autopsy",
  "arguments": {
    "companyId": "jarvais",
    "strategyId": 5,
    "limit": 20
  }
}
```

This flows through [`_strategy_autopsy()`](shared/mcp/tools/trading.py) →
[`ValidationEngine.autopsy_strategy()`](shared/trading/validation.py:117).

The autopsy produces three outputs:

**1. Per-trade comparisons** via [`_build_trade_comparisons()`](shared/trading/validation.py):

```json
{
  "trade_comparisons": [
    {
      "trade_id": 42,
      "signal_match": true,
      "entry_price_delta": 1.38,
      "exit_price_delta": -0.32,
      "pnl_delta": -1.18,
      "pnl_delta_pct": -0.012,
      "rule1_pass": true,
      "slippage_contribution": 0.158,
      "fee_contribution": 2.67
    },
    {
      "trade_id": 43,
      "signal_match": true,
      "entry_price_delta": 8.50,
      "exit_price_delta": -3.20,
      "pnl_delta": -12.40,
      "pnl_delta_pct": 0.38,
      "rule1_pass": false,
      "slippage_contribution": 0.972,
      "fee_contribution": 3.10
    }
  ]
}
```

Trade 43 failed Rule 1 — the agent can see that slippage was $0.97 and fees
were $3.10 over the shadow estimate.

**2. Drift pattern detection** via [`_detect_drift_patterns()`](shared/trading/validation.py):

The engine looks for recurring patterns across all trades:

| Pattern | Detection Logic | Severity |
|---------|----------------|----------|
| `consistent_adverse_entry_slippage` | >70% of trades have positive entry delta | high/medium |
| `consistent_adverse_exit_slippage` | >70% of trades have negative exit delta | high/medium |
| `fee_drift` | >50% of trades have fee delta > $1 | medium/low |
| `data_drift` | Any trade has `data_drift_detected=true` | high |

```json
{
  "drift_patterns": {
    "patterns_found": 2,
    "patterns": [
      {
        "name": "consistent_adverse_entry_slippage",
        "severity": "medium",
        "affected_pct": 72.3,
        "description": "34/47 trades had adverse entry slippage."
      },
      {
        "name": "fee_drift",
        "severity": "low",
        "affected_pct": 53.2,
        "description": "25/47 trades had fee drift > $1. Average fee delta: $1.23."
      }
    ]
  }
}
```

**3. Actionable learnings** via [`_generate_learnings()`](shared/trading/validation.py):

Each learning has a **category**, an **insight**, and a concrete **action**:

```json
{
  "learnings": [
    {
      "category": "slippage",
      "insight": "34/47 trades had adverse entry slippage. Live fills are consistently worse.",
      "action": "Re-run backtest with slippage_bps increased by 2-5 bps. If the strategy remains profitable, the adjusted config better reflects live conditions."
    },
    {
      "category": "fees",
      "insight": "25/47 trades had fee drift > $1. Average fee delta: $1.23.",
      "action": "Update fee_taker_bps in the backtest config to match the actual exchange fee tier. Consider using limit orders (maker fee) to reduce costs."
    }
  ]
}
```

### Step 22: The Learning Loop Closes

Armed with the autopsy learnings, the agent takes action:

**If verdict = CONTINUE:**
- Strategy is performing as expected. Scale up or add more pairs.

**If verdict = CAUTION:**
- Collect more data. Don't increase allocation yet.
- If slippage is the issue: re-backtest with `slippage_bps` increased by 2-5.
- If fees are the issue: verify exchange fee tier, consider limit orders.

**If verdict = STOP:**
- Stop live trading immediately.
- Re-run a parameter sweep with adjusted slippage/fee assumptions.
- If no parameter set passes Rule 1, the strategy is not viable in live conditions.
- Consider: different timeframe, different pair, or a completely new strategy.

**The agent re-backtests with adjusted parameters:**

```json
{
  "name": "backtest.submit",
  "arguments": {
    "symbol": "BTC/USDT",
    "strategy": "sma_cross",
    "params": {"fast_period": 10, "slow_period": 30},
    "risk": {
      "slip_bps": 7,      ← increased from 2 based on autopsy
      "fee_bps": 8,       ← increased from 5 based on autopsy
      "sl": 2.5,          ← widened from 2.0 based on exit slippage pattern
      "tp": 4.0
    }
  }
}
```

If the adjusted backtest still shows a positive Sharpe, the agent now has a
**realistic** expectation of live performance. If not, the strategy is abandoned
before wasting more capital.

---

## Summary: The Complete Loop

```
 1. Agent backtests BTC → finds SMA(10,30) with Sharpe 1.87
 2. Agent starts forward-test shadow → same BacktestExecutor, real-time candles
 3. Agent submits live trade → Treasury approves → Paper adapter fills
 4. Shadow trade fires at same bar → lands in ClickHouse
 5. Live trade fires 3 seconds later → lands in Postgres
 6. Two days pass...
 7. Validation engine pairs them (10-second window)
 8. P&L delta = 0.012% → WITHIN 0.1% → Rule 1 passes ✅
 9. Re-run backtest with extended dates → same signals, same fills
10. Strategy review → CONTINUE verdict, 93.6% Rule 1 pass rate
11. Strategy autopsy → detects slippage pattern, generates learnings
12. Agent re-backtests with adjusted slippage/fee assumptions
13. If profitable → continue with realistic expectations
14. If not → abandon strategy, try new parameters or new approach
```

The **only source of drift** is real-world market microstructure (slippage, fee
differences, execution latency) — and the validation engine attributes every
fraction of a percent to its cause. The review and autopsy tools close the loop,
turning drift data into actionable strategy improvements.
