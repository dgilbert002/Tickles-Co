# JarvAIs V2.0 — The Definitive Build Blueprint

> **What this file is**: The ONLY document you need to build JarvAIs V2.0 from scratch. If you are
> an AI agent reading this for the first time — on a VPS, in Cursor, in Claude Code, anywhere —
> this file contains EVERYTHING: the owner's vision, the three non-negotiable rules, both legacy
> system analyses, the normalized database schema, the unified indicator architecture, the agent
> roster, the implementation plan, and every file reference with line numbers.
>
> **How this was built**: This is V3 of the context document. V1 was a summary. V2 was a 1082-line
> encyclopedia. V3 merges V2 with a comprehensive code audit of both legacy systems (JarvAIs V1 +
> Capital 2.0), a Gemini architectural review (verified against actual code), normalized table
> designs, indicator standardization, and performance engineering. Nothing was cut. Everything is here.
>
> **Memory stack**: MemClaw has the full project workspace. Mem0 has agent runtime recall. This file
> is the versioned source of truth.
>
> Last updated: 2026-04-12

---

## Table of Contents

1. [The Owner and The Vision](#1-the-owner-and-the-vision)
2. [The Three Non-Negotiable Rules](#2-the-three-non-negotiable-rules)
3. [Capital 2.0 — Deep Analysis](#3-capital-20--deep-analysis)
4. [JarvAIs V1 — Deep Analysis](#4-jarvais-v1--deep-analysis)
5. [V2 Architecture — The Synthesis](#5-v2-architecture--the-synthesis)
6. [Normalized Database Schema](#6-normalized-database-schema)
7. [Unified Indicator Architecture](#7-unified-indicator-architecture)
8. [Reusable Components Inventory](#8-reusable-components-inventory)
9. [What to Build New](#9-what-to-build-new)
10. [Communication and Development Workflow](#10-communication-and-development-workflow)
11. [Multi-Company Architecture](#11-multi-company-architecture)
12. [Self-Improvement and Path to Autonomy](#12-self-improvement-and-path-to-autonomy)
13. [Infrastructure](#13-infrastructure)
14. [Performance and Efficiency Design](#14-performance-and-efficiency-design)
15. [Gemini Architectural Review — Verified Fixes](#15-gemini-architectural-review--verified-fixes)
16. [Implementation Plan — Step by Step](#16-implementation-plan--step-by-step)
17. [Complete File Reference — What to Port](#17-complete-file-reference--what-to-port)

---

## 1. The Owner and The Vision

### Who is the owner?

The owner (referred to as "Board Operator") is NOT a developer. He understands systems at a high level
but does not write code. He wants things explained like you're talking to a 21-year-old. He prefers
"vibe coding" — talking to AI agents in natural language and having them build things, not touching
IDEs or terminals himself (except during the initial V2 build phase for infrastructure setup).

### What is the vision?

The owner has two existing trading systems (JarvAIs V1 for crypto, Capital 2.0 for CFDs) that both
have strengths and fatal weaknesses. Neither is profitable. The vision for V2 is:

1. **Start with $500 in real capital and grow it to $5 million+** through disciplined, emotionless,
   quantitatively driven AI trading.
2. **AI runs the company while the owner sleeps.** The system is self-generating, self-improving.
   Agents change their own code, swap their own LLM models, optimize their own strategies.
3. **Multiple trading companies** — crypto first, then CFDs, then exploration/sandbox — all sharing
   infrastructure but isolated in their trading data.
4. **The fundamental non-negotiable (Rule 1)**: If a backtest says a strategy makes money, and the
   live version of that same strategy doesn't make money, the system is BROKEN. Backtest-to-live
   validation is the #1 priority. Everything else is secondary.
5. **Path to full autonomy**: Human approves everything at first, then gradually hands over control
   until the AI company runs itself profitably without human intervention.
6. **Future-proof**: When Claude 5 comes out, or a better model, the system swaps without rebuilding.
   Model-agnostic, IDE-agnostic, exchange-agnostic.

### The CEO metaphor

The owner described V2 like buying a failing company. You (the AI) are the new CEO. You inherited
V1's code and Capital 2.0's code. You need to:

- Right-size the workforce (agents — some retired, some hired, some restructured)
- Fix the fundamental business model (backtest validation was missing)
- Cut costs (29 agents making LLM calls reduced to ~6 LLM agents + ~11 math services)
- Build infrastructure that scales (shared database, common services)
- Deliver profit or face "termination and bankruptcy"

The owner explicitly said: "I want to work for you. I want to be your eyes and ears. This is YOUR
company. Make it profitable."

### Company-agnostic design principle

The owner was emphatic: no bias toward JarvAIs or Capital 2.0 in naming, tables, or code. Everything
is normalized and standardized so ANY future trading company (crypto, CFD, gold, stocks, forex) can
plug in with zero schema changes. Table names, column names, field types — all neutral. If two legacy
tables do the same job, they merge into ONE normalized table.

---

## 2. The Three Non-Negotiable Rules

Every design decision in V2 must satisfy these rules. They override all other considerations.

### Rule 1: Backtest-to-Live Parity (99.9% Target)

If a backtest says $500 becomes $500,000, the live system must produce results within 0.1% of that
prediction. If it's only 80% accurate, the 20% gap can blow the account. This is the owner's #1
frustration: backtests show 60-80% win rates and $500K profit potential, but live trading never
matches. After weeks of live trading, the system barely breaks even or loses money.

**Technical requirements:**

- **Same engine for backtest and live.** Capital 2.0's brain.ts pattern (same `executeStrategy` for
  both) is the gold standard. V2 must use ONE signal evaluation path.
- **Track every variable that differs between backtest and live:**
  - `candle_data_hash` — SHA-256 of OHLCV values used at decision time
  - `decision_timestamp` — exact moment the signal was evaluated (ms precision)
  - `signal_params_hash` — paramHash of indicator config used
  - `market_snapshot` — bid/ask/mid at decision time
  - `fake_candle_used` — boolean + the fake close value + source timestamp
- **Validation on every closed trade:** re-run the signal engine with the same params against current
  candles. Compare: did the signal match? Did entry/exit match? Store the delta.
- **Configurable halt threshold** per strategy and per company (not hardcoded 80%). Store in
  `strategies.halt_threshold_pct` and `company_config`.
- **Data drift detection:** if candles changed since decision time (exchange retroactive adjustment),
  flag as "data_drift" not "strategy_failure."

**The Three Layers of Truth:**

Every trade in V2 exists in three comparable states:

**Layer 1 — Backtest**: Historical candles + indicator signals + simulated P&L. This is what
the strategy WOULD HAVE done if we ran it on past data. Uses paramHash (SHA-256 of all input
parameters) for exact reproducibility.

**Layer 2 — Paper/Demo**: Live market data + the exact same signal logic + simulated fill (no
real money). This proves the signal logic works in real-time, not just on historical data.

**Layer 3 — Live**: Real exchange + actual order fill + real fees + real slippage. This is the
truth. If Layer 3 diverges from Layer 1, we need to know by how much and why.

**How validation works (step by step):**

1. A trade closes (either paper or live).
2. The validation service wakes up.
3. It takes the trade's `strategy_id`, `signal_params_hash`, `candle_data_hash`.
4. It re-runs the backtest for that exact time window using the exact same parameters (paramHash
   guarantees identical inputs). For CFDs, the fake 5min candle at T-60 must be accounted for —
   at T-60 the last 1min candle hasn't formed/closed yet, so its close is assumed to be the
   T-0 close value. Crypto is different (24/7 markets, no fake candles needed).
5. It compares:
   - **Signal match**: Did the backtest produce the same BUY/SELL/HOLD signal?
   - **Entry price deviation**: How far was the actual fill from the backtest's predicted entry?
   - **Exit price deviation**: Same for exit.
   - **P&L deviation**: Actual P&L vs backtest predicted P&L.
   - **Slippage contribution**: How much of the delta is from slippage?
   - **Fee contribution**: How much of the delta is from fees?
6. Each comparison is stored in the `trade_validations` table (per-company database).
7. A rolling average is maintained per strategy.
8. **If accuracy drops below threshold**: Strategy is HALTED. CEO is alerted.
9. **If accuracy stays healthy**: Strategy allocation can be INCREASED.

**Hashes for reproducibility:**

- **paramHash**: SHA-256 of ALL backtest input parameters (symbol, indicator name, indicator params,
  timeframe, leverage, stop loss, take profit, date range, timing config). Sorted JSON keys before
  hashing. This is proven in Capital 2.0 where both Node.js and Python produce identical hashes.
  V2 is Python-only, eliminating cross-language risk, but test vectors must exist.
- **candle_data_hash**: SHA-256 of the candle dataset used at decision time. If candles change
  (exchange retroactively adjusts), we detect the drift.
- **signal_params_hash**: Hash of the exact indicator parameters used for this trade's signal.
- **brain_snapshot**: Full JSON of all DNA strand signals, which strand won, why it won (conflict
  resolution metric and score), whether there was a conflict at all. Stored per-trade with a
  `brain_snapshot_version` integer so schema changes don't break forensics.

### Rule 2: Execution Accuracy (Track Everything)

Every number must be tracked to precision. No guessing, no rounding, no missing fields.

- **Prices:** `DECIMAL(20,8)` everywhere — handles crypto micro-prices (0.00000001) and gold
  (2500.12345678)
- **Volumes/notional:** `DECIMAL(30,8)` — handles large BTC notional values
- **Percentages/rates:** `DECIMAL(10,6)` — funding rates, win rates, slippage percentages
- **PnL/fees:** `DECIMAL(20,8)` — must handle negative and large values
- **Ratios (Sharpe etc.):** `DECIMAL(10,4)`
- **Timestamps:** `DATETIME(3)` — millisecond precision on every event
- **Every table:** `created_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)`
- **Mutable tables:** `updated_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3)`

**Fees tracked individually in `trade_cost_entries` table (one row per fee event):**

- `maker_fee` / `taker_fee` (exchange trading fee)
- `spread` (bid-ask spread at execution)
- `overnight_funding` (per-night accrual, one row PER NIGHT for multi-day positions)
- `swap` (MT5 swap)
- `commission` (broker commission)
- `guaranteed_stop` (if applicable)
- `slippage` (expected vs actual fill price)

**Slippage tracking on the `trades` table:**

- `expected_entry_price` vs `entry_price` (actual fill) — the delta IS entry slippage
- `expected_exit_price` vs `exit_price` (actual fill) — exit slippage
- `entry_slippage_pct` / `exit_slippage_pct` — as percentages
- `signal_to_fill_ms` — time from signal generation to exchange confirmation
- `order_to_fill_ms` — time from order submission to fill

**Full chain of custody for every trade:**
trade -> strategy -> DNA strand -> backtest_result -> paramHash -> candle_data_hash

### Rule 3: Memory Efficiency and Performance

Running on a 48GB VPS with multiple companies. Every byte matters.

- **Bounded caches:** Every in-memory cache must have max entries + TTL + eviction policy.
  No unbounded Maps/dicts.
  - Indicator cache: max 200 entries, 15min TTL (port JarvAIs' `_ta_cache` pattern)
  - Candle cache: max 500 entries, 5min TTL (port Capital 2.0's `capitalCandleCache` but ADD max)
  - Session state: max entries = number of active accounts (bounded by nature)
- **Database indexes on every hot query path:**
  - Candles: composite unique on `(instrument_id, source, timeframe, timestamp)` + index on
    `(instrument_id, timeframe)` for range scans
  - Trades: index on `(account_id, status)`, `(instrument_id, opened_at)`, `(strategy_id, closed_at)`
  - Backtest results: unique on `(param_hash)`, index on `(instrument_id, indicator_name)`
- **Connection pool limits:** max 20 connections per service, total across all services capped at
  MySQL's `max_connections` minus headroom. Capital 2.0 used `queueLimit: 0` (unbounded wait
  queue) — do NOT repeat this.
- **Candle partitioning:** RANGE partition by month on 1m candles. Retention: 1m=30 days,
  5m=90 days, 15m=6 months, 1h=2 years, 4h+=forever. SMART CANDLE monitoring, if backtests need candles, they download the range specified instead of running on minimal. User is Asked how long candles should be retained
- **Avoid loading full JSON blobs** for list queries. Project only needed columns. Paginate.
- **Thread/worker limits:** aligned with VPS cores (12 vCPU). Max 6 concurrent backtest workers.
  Max 4 concurrent LLM calls. Each backtest worker gets ~4GB memory budget.

---

## 3. Capital 2.0 — Deep Analysis (C:\CapitalTwo2.0)

### What it is

CFD trading system for Capital.com. Tech stack: Node.js/TypeScript (Express, tRPC, React, Vite),
Python 3 (pandas, numpy, scipy). Drizzle ORM for database. MySQL. NO AI — purely indicator and
backtest driven. Markets close at specific times so it uses timer-based execution.

How it works: Makes leveraged trades few seconds before market closes, hoping that tomorrow the
market jumps a few percent. Some backtest strategies use as much as 10x leverage, using 99% of
available balance. Takes $500 to $500,000 in a year on backtests. Currently long-only. Example:
$500 balance, 99% margin = $495, 10x leverage = $4,950 position size with backtested stop loss.
Market opens at 5% up = $247.50 profit. Next day balance is $747.50. Repeat.

### The execution flow (how a trade actually happens)

Capital 2.0 trades CFDs which have market close times. The goal was to make compound profit per day hoping for market increase from close to open, or decrease for shorts. The system uses a countdown timer which could now be an openclaw agent:

1. **Timer starts**: `server/orchestration/timer.ts` runs a `setInterval` every 1 second.
2. **T-240 seconds** (4 minutes before close): Brain window opens.
   - `server/live_trading/brain.ts` loads the active strategy.
   - Strategy contains DNA strands (each strand = one backtest result with indicator config).
   - For each DNA strand, `dna_brain_calculator.ts` runs the indicator on the latest available
     candles and produces a signal (BUY / SELL / HOLD).
3. **T-60 seconds** (1 minute before close): Fake candle capture.
   - REST API call to Capital.com to get the current price.
   - This creates a "fake" 5-minute candle using the real-time price.
   - WHY REST not WebSocket? WebSocket prices are ~23 seconds delayed. REST is real-time.
   - The captured price is stored as `fake5minClose` in the trade record.
   - Schema has full support: `fake_5min_close`, `fake_5min_source_timestamp`,
     `fake_5min_comment`, `fake_5min_calculated_at` columns.
   - `brain.ts` stores `brainCalcPrice`, `fake5minClose`, `priceVariancePct` for traceability.
4. **T-30 seconds**: Safety close.
   - Any existing positions that should be closed are closed here.
   - Gives 30 seconds buffer before new trades.
5. **T-15 seconds**: Execute opens.
   - `dna_brain_accumulator.ts` has collected all DNA strand results.
   - If multiple strands say BUY, conflict resolution picks the winner.
   - `open_orchestrator.ts` calculates position size based on window allocation.
   - `trade_queue.ts` manages the execution queue with priority ordering
     (balance, PnL, Sharpe, win rate, or FCFS) and state machine
     (BRAIN_CALCULATING -> CLOSING_POSITION -> CHANGING_LEVERAGE -> READY_TO_BUY ->
     TRADE_FIRED -> COMPLETED / FAILED / HOLD).
   - Trades are executed 200ms apart (the "trade cannon") to avoid exchange rate limits.
   - Each trade stores `allDnaResults` JSON (the brain snapshot) for later validation.
6. **T-0**: Market closes. Timer resets for next session.

### DNA Strands (the core concept)

A DNA strand is a backtest result that has been "promoted" to active use. It contains:

- `sourceTestId` -> points to `backtestResults.id` (the specific backtest run)
- Indicator name and configuration (e.g., RSI with period 14, overbought 70, oversold 30)
- Timeframe (e.g., 5min, 15min, 1hour)
- Leverage, stop loss, take profit settings
- Performance metrics from the backtest (Sharpe, return, win rate, max drawdown)

Multiple DNA strands form a **strategy**. A strategy runs multiple backtests simultaneously, each
producing its own signal. When they conflict, conflict resolution picks the winner.

In V2, DNA strands are NORMALIZED out of JSON into the `strategy_dna_strands` junction table
so you can query "which strategies use indicator X?" without parsing JSON.

### Conflict Resolution

When multiple DNA strands in the same window produce different signals:

- `sharpe`: Winner has the highest Sharpe ratio from its backtest.
- `return`: Winner has the highest total return.
- `winRate`: Winner has the highest win rate.
- `first_signal`: First signal to arrive wins (time-based, for real-time racing).
- Configurable per-window via `conflictResolutionMetric` field in strategy config.
- Default is `sharpe`.

The brain stores the conflict resolution outcome in `allDnaResults` JSON: which strands participated,
what each signaled, which won, the winning metric value, and whether a conflict existed. In V2, key
fields (`winning_strand_id`, `conflict_exists`, `price_variance_pct`) are promoted to first-class
columns on the `trades` table. The full JSON is kept as `brain_snapshot_detail` for forensics.

### Window/Slot Allocation

Strategies are divided into windows (time slots or logical partitions):

- Each window gets `allocationPct` of total account balance.
- `carryOver: true` means if a window's DNA strands all say HOLD, the unused allocation rolls
  forward to the next window. Prevents leaving cash idle.
- `originalDailyBalance` is locked at the first initialization of the trading day. Why? Because
  after a trade uses margin, the current balance drops. If subsequent windows calculated allocation
  from the current (margin-reduced) balance, they'd get less than intended.
- `totalAccountBalancePct` caps the overall deployable balance (default 99%, keeps 1% as buffer).
- Cross-window tracking via `windowsTradedToday` Set prevents double-trading.
- `fund_tracker.ts` persists via `tradingSessionState` in DB and has `reconstructFromDatabase`
  for crash recovery. However, it lacks a version column for optimistic concurrency — V2 fixes
  this with `session_state_version INT` and compare-and-set updates.

In V2, windows are NORMALIZED out of JSON into the `strategy_windows` table.

### paramHash (SHA-256 for reproducibility)

Generated by BOTH Node.js (`server/backtest_bridge_v2.ts`) and Python (`python_engine/batch_runner.py`):

- Takes all input parameters: epic (symbol), indicator name, indicator params, timeframe, leverage,
  stop loss, take profit, date range, timing config.
- Both implementations use recursive key sorting (`sort_object_keys` / `sortObjectKeys`) before
  `JSON.stringify` / `json.dumps(separators=(',',':'))`.
- SHA-256 hex digest.
- `batch_runner.py` docstring says: "Generate the exact same hash that Node.js generates."
- V2 is Python-only, eliminating cross-language risk, but test vectors must exist.

### Strategy Executor (Python backtest engine)

`python_engine/strategy_executor.py`:

- Runs one isolated backtest per DNA strand in parallel using `ThreadPoolExecutor` (max 8 workers).
- Each strand's backtest calls `run_single_backtest` from `backtest_runner.py` which loops
  **trading days** (not every 5m bar). Intraday data is used for timing cutoffs and HMH stop
  monitoring, but indicators are evaluated once per day.
- DNA strand `timeframe` defaults to `5m`. Data loaded via `load_candles_with_fake_5min`.
- `execute_windows_with_dna` simulates the combined portfolio day-by-day:
  - For each (day, window): collect signals from all DNA strands in that window.
  - If multiple strands signal, apply conflict resolution to pick winner.
  - Calculate position size: `(available_funds * leverage) / price`, clamped to min/max increment.
  - Track running P&L including spread costs (bid/ask or fallback spread_percent) and overnight
    funding (per-night charge if position held across days).
- Carry-over: per day, `carried_over_pct` starts at 0. If a window has no trades and carryOver
  is enabled, the allocation rolls to the next window.
- Outputs: overall return, Sharpe, win rate, max drawdown, per-day breakdown, per-trade details.

### Batch Runner (optimization engine)

`python_engine/batch_runner.py`:

- Runs multiple backtests with different parameter combinations using `itertools.product`.
- 5 optimization strategies: random search, grid search, simulated annealing, genetic algorithm,
  Bayesian optimization.
- Each run generates a paramHash for deduplication — checks `backtestResults` and `testedHashes`
  tables before running, skips if hash exists.
- Parallelized with `multiprocessing.Pool` using configurable `parallel_cores`.
- Results stored in `backtest_results` table with all parameters and metrics.

### Indicator Library

`python_engine/indicators_comprehensive.py`:

- **221 unique indicators** implemented (one duplicate key `ttm_squeeze_on` — second overwrites first).
- Pure math (numpy, pandas, scipy) — no LLM costs.
- Each indicator is a `(DataFrame, index, **params) -> bool` function. True = condition fired.
- `INDICATOR_METADATA` (lines 35-2526) stores per-indicator: direction (bullish/bearish/neutral),
  category (momentum/trend/volatility/volume/smart_money/breakout/pullback/crash_protection/combination),
  description, default params, and param_ranges for optimization grids.
- Indicators are stateless given `(df, idx)` — history is whatever rows are in the DataFrame.
- Categories include: RSI, MACD, Bollinger Bands, ATR, ADX, Stochastic, Williams %R, CCI, MFI,
  OBV, Ichimoku, Supertrend, VWAP, Keltner, TTM Squeeze, Fisher, CMF, Donchian, TRIX, TSI,
  SMC order blocks, and dozens of custom combinations.
- `indicator_registry.py` provides a clean metadata layer: `get_metadata`, `get_default_params`,
  `get_param_ranges`, `get_organized_list` (nested by direction/category for UI).

### Validation Service

`server/services/validation_service.ts`:

What it got RIGHT:
- Re-runs ALL DNA strands with historical candles.
- Checks `winnerMatch`, `signalMatch`, `allSignalsMatch`.
- Stores results in `validated_trades` table.
- Has a `run-strategy-comparison` endpoint (lines ~1710-2738) that compares signal alignment
  per date/window (match, missed, unexpected, correct_hold).

What it got WRONG:
- `strategyTestResults` table disconnected from live comparison (two parallel systems).
- `validate-backtest-accuracy` uses `entryPrice` as T-60 price — BUG: should use `brainCalcPrice`.
- `trade_comparisons` table has upsert functions but nothing consistently populates it.
- No single accuracy score tracked over time (just per-trade yes/no).
- No automated nightly comparison job — manual only.
- No candle data hash — re-reads candles from DB but can't detect if candles changed.
- `validated_trades` and `trade_comparisons` use soft references (no FK constraints).

V2 fixes all of this with the `trade_validations` table (per-company, with `candle_data_hash`
and `data_drift_detected`).

### Key files in Capital 2.0

| File                                              | Purpose                                              |
| :------------------------------------------------ | :--------------------------------------------------- |
| `server/live_trading/brain.ts`                  | Main brain: loads strategy, orchestrates DNA signals, fake candle capture, `priceVariancePct` |
| `server/orchestration/timer.ts`                 | 1-second setInterval, T-minus countdown              |
| `server/orchestration/dna_brain_accumulator.ts` | Collects DNA results, final conflict resolution      |
| `server/orchestration/dna_brain_calculator.ts`  | Per-strand signal calculation                        |
| `server/orchestration/fund_tracker.ts`          | Window allocation, carryOver, originalDailyBalance, DB persistence + reconstructFromDatabase |
| `server/orchestration/open_orchestrator.ts`     | Position sizing: `(availableFunds * leverage) / midPrice`, 200ms trade cannon |
| `server/orchestration/trade_queue.ts`           | TradeQueueManager: per-window queues, state machine, priority ordering |
| `server/orchestration/leverage_checker.ts`      | Leverage cache + DB + live poll, T-5m ground truth   |
| `server/services/validation_service.ts`         | Re-runs DNA strands with historical candles          |
| `server/services/deal_confirmation_service.ts`  | Exponential backoff retry, batch with concurrency limit |
| `server/services/trade_sync_service.ts`         | Incremental sync from last timestamp, upsert by dealId |
| `server/services/market_info_service.ts`        | Syncs broker metadata (margin, hours, min sizes) to DB |
| `server/services/candle_data_service.ts`        | buildFake5MinCandle, captureT60FakeCandle, WS + history caches |
| `server/services/trading_session_state.ts`      | DB-backed JSON session state, merge-on-re-init for windows |
| `server/conflict_bridge.ts`                     | TypeScript mirror of Python conflict resolution      |
| `server/backtest_bridge_v2.ts`                  | Node.js paramHash generation, backtest orchestration |
| `python_engine/strategy_executor.py`            | Parallel per-strand backtests, window/day merging, carry-over |
| `python_engine/batch_runner.py`                 | Batch backtesting with 5 optimization strategies, paramHash generation |
| `python_engine/backtest_runner.py`              | Single backtest execution, `run_single_backtest` day loop |
| `python_engine/indicators_comprehensive.py`     | 221 technical indicators + INDICATOR_METADATA         |
| `python_engine/indicators.py`                   | IndicatorLibrary, create_indicator_library, get_conditions |
| `python_engine/unified_signal.py`               | evaluate_indicator_at_index (lines 242-357)           |
| `python_engine/batch_optimizer.py`              | ParameterSpace, itertools.product permutations        |
| `python_engine/adaptive_timing.py`              | Derives actual close times from candle data, DST handling |
| `python_engine/liquidation_utils.py`            | Closed-form liquidation price math (already Python)   |
| `python_engine/db_pool.py`                      | Pool + batch hash lookup + epic-scoped hash load      |
| `python_engine/file_logger.py`                  | Structured logging for long-running jobs              |
| `drizzle/schema.ts`                             | Full schema definition (40+ tables)                   |

### Known bugs in Capital 2.0

1. `strategyTestResults` table disconnected from live comparison (two parallel systems).
2. `validate-backtest-accuracy` uses `entryPrice` as T-60 price — BUG: should use `brainCalcPrice`.
3. `trade_comparisons` table has upsert functions but nothing populates it consistently.
4. No single accuracy score tracked over time.
5. No automated nightly comparison job — manual only.
6. Timer-based execution loses state on server restart (fund_tracker has DB recovery but no
   version column — concurrent writers could lose updates).
7. `unique_candle` index in Drizzle is `index()` not `uniqueIndex()` — verify migrations enforce
   actual uniqueness.
8. `duplicate ttm_squeeze_on` key in INDICATOR_METADATA — second overwrites first.
9. `closeActualTrade` is defined but not called in trade_poller — spreadCost/overnightCost/grossPnl
   columns may often be null on actual_trades.
10. No candle data hash anywhere — validation re-reads candles without detecting drift.

---

## 4. JarvAIs V1 — Deep Analysis (C:\JarvAIs)

### What it is

AI-driven crypto trading system. Python (FastAPI), MySQL. 29 agents with LLM-based decision making.
Has shadow/paper trading but NO real backtesting. Trades crypto perpetual futures on Bybit, BloFin,
Bitget via CCXT.

### The trading flow (how a trade actually happens in V1)

1. **Scout Agent** scans for opportunities:
   - Monitors market symbols for momentum, volume spikes, unusual activity.
   - Filters based on exchange availability across configured accounts.
   - Produces a shortlist of candidate symbols.
   - Hybrid: `_should_rescan` is pure math/DB lookups (zero LLM cost), but Desk Brief and
     optional advisor paths use `query_with_model` (LLM).

2. **Data Scientist (BillNye)** runs analysis:
   - 30+ technical indicators on each candidate including SMC (order blocks, FVG, BOS, CHoCH,
     liquidity grabs, AMD cycles), TD Sequential, divergence, volume profile, VWAP, Fibonacci.
   - Returns rich dicts per indicator per timeframe (not simple bool signals like Capital 2.0).
   - Has in-memory TTL cache: max 200 entries, 15min TTL, keyed by symbol + TF + candle state.
   - `compute_chart_tradeability_score` (lines 1540-1729) includes a `confluence` sub-score
     counting agreement among RSI/MACD/Bollinger on H1, weighted 15%.
   - Pure math — zero LLM calls. Soul says "numbers, not opinions."

3. **Trade Dossier** (the LLM decision pipeline):
   - Takes: BillNye's data, charts/screenshots, news context, market conditions.
   - Multiple LLM calls evaluate the opportunity from different angles (Stage 1 + Stage 2).
   - This is 75-85% LLM and the MAIN cost driver in V1.
   - Produces a structured trade recommendation: direction, entry, SL, TP, confidence.
   - Records full reasoning in `trade_dossiers` table (separate from trades — good pattern, keep).

4. **Trading Floor** executes the trade:
   - Receives approved dossier.
   - **State machine with optimistic concurrency**: `VALID_TRANSITIONS` dict defines allowed
     status changes. `transition_dossier()` uses `UPDATE...WHERE id = ? AND status = ?` — race-safe.
     This is the V2 concurrency standard.
   - **Waterfall routing**: `trading_accounts` ordered by `waterfall_priority` (lower = first).
   - For each account: check enabled, live_trading on, duo_allowed, max_trades_per_symbol.
   - `set_margin_mode("isolated")` — each position isolated.
   - `set_leverage(X)` — set leverage for this trade.
   - Place order via CCXT with `clientOrderId` (UUID for idempotency).
   - Fee calculation: `_calc_fee` (lines 915-927) — maker/taker % from config (default 0.02/0.06%).
   - Max retries via `waterfall_max_retries` (default 3).

5. **Live Trade Monitor** manages the position:
   - Watches all open positions continuously. Zero LLM calls.
   - Polls exchange for fills, verifies with `fetch_order`, detects ghost fills (order gone +
     position present + fetch_order confirmation to avoid false positives).
   - Fee extraction from exchange: `sum(f.get("fee", 0) for f in close_fills)` (lines 903-946).
   - Entry slippage calculation: computes entry slippage % vs paper entry (lines 1079-1083).
   - On TP1 hit: **free_trade_mode** — raise leverage, move SL to break-even.
   - Supports partial closes.
   - Accrued funding updated from funding rate (lines ~1308-1364).

6. **Auditor** runs post-mortem:
   - `run_full_postmortem()` with idempotency (skip if report exists).
   - 4 phases: evidence -> assessment -> interrogation -> verdict.
   - P&L preference for live vs paper fields.
   - LLM-heavy: 60-80% of work is LLM post-mortem analysis.

### V1 indicator details (Smart Money Concepts)

JarvAIs has ~30 indicators in `services/data_scientist.py` that Capital 2.0 does NOT have:

| Indicator | Function | Lines | What it detects |
|---|---|---|---|
| Order Blocks | `_detect_order_blocks` | 855-883 | Last 10 bullish/bearish OBs via engulfing rules |
| Fair Value Gaps | `_detect_fair_value_gaps` | 887-918 | 3-candle gaps, fill flags |
| Break of Structure | `_detect_bos` | 1375-1403 | Swing break vs prior ~10 bars |
| Change of Character | `_detect_choch` | 1343-1373 | Close through recent swing high/low |
| Liquidity Grab | `_detect_liquidity_grab` | 1457-1490 | Wicks vs body vs recent high/low |
| AMD Cycle | `_detect_amd_cycle` | 1492-1521 | Thirds of session range |
| Confluence Score | `compute_chart_tradeability_score` | 1650-1682 | RSI/MACD/Bollinger agreement, weighted 15% |

These are opt-in (only computed if requested strings appear in the `requested` parameter).

CCXT has MCP now in Roo

### V1 data sources (13+)

| Source            | What it provides                                       | Cost   |
| :---------------- | :----------------------------------------------------- | :----- |
| CCXT/CCXT Pro     | Exchange data (candles, order books, trades, balances) | Free   |
| Yahoo Finance     | Traditional market data                                | Free   |
| MT5               | MetaTrader data (for future CFD/forex)                 | Free   |
| RSS Feeds         | Crypto/finance news (multiple feeds)                   | Free   |
| Telegram Channels | Insider signals, breaking news                         | Free   |
| Discord           | Community signals, sentiment                           | Free   |
| TradingView       | Charts, technical analysis                             | MCP    |
| Alternative.me    | Fear & Greed Index                                     | Free   |
| Fair Economy      | Economic calendar                                      | Free   |
| Google News       | General news impact on markets                         | Free   |
| LLM APIs          | GPT-4, Claude for trade analysis                       | $$$    |
| Mem0/Qdrant       | Vector memory for past decisions                       | Low    |
| Various MCPs      | Gloria AI, CoinGecko, Finlight, Exa Search             | Varies |

### V1 schema overview

60+ tables. V2 replaces ALL of them with 24 normalized tables (14 shared + 10 per-company).
Key V1 tables that map to V2:

| V1 Table | V2 Replacement | Notes |
|---|---|---|
| `market_symbols` | `instruments` (shared) | Merged with Capital 2.0's `epics` + `marketInfo` |
| `candles` | `candles` (shared) | Add `data_hash`, bid/ask columns, fake candle support |
| `trading_accounts` | `accounts` (per-company) | Merged with Capital 2.0's `accounts` |
| `trades` + `live_trades` + `trade_dossiers` | `trades` (per-company) | ONE table, all trade types |
| `apex_shadow_trades` | `trades` with `trade_type='shadow'` | Shadow trades are just a type |
| `position_events` | `order_events` (per-company) | Expanded to full order lifecycle |
| `news_items` | `news_items` (shared) | Same concept, normalized columns |
| `agent_profiles` + `scheduled_tasks` | OpenClaw/Paperclip agent config | Agents managed by framework |
| `ai_api_log` | `api_cost_log` (shared) | Merged with Capital 2.0's `apiCallLogs` |
| `system_config` + `config` | `system_config` (shared) | Merged with Capital 2.0's `settings`, namespaced |
| `prompt_versions` | Moved to MemClaw/Mem0 | Prompts managed by agent framework |
| `agent_activity_log` | Port as-is (good structure) | Input/output refs JSON, costs |

### Known weaknesses in V1

1. **No backtesting at all** — LLM predictions with zero historical validation.
2. **Shadow P&L fragmented** — split across `auditor.py`, `shadow_queue.py`, `trade_scorer.py`.
3. **Signal backtester disconnected** — exists (`signal_backtester.py`, explicitly "NO AI CALLS")
   but disconnected from the dossier/live trading pipeline.
4. **No strategy concept** — each trade is a one-off LLM decision. No persistent strategy.
5. **Too many agents (29)** — massive overlap, ~$300/mo LLM costs for low-value work.
6. **P&L engine has known bugs** — scattered aggregations, not a full accounting engine.
7. **`signal_backtester.py` has NO fee handling** — backtest vs live P&L parity impossible.
8. **No outbound notification service** — collectors RECEIVE from Telegram/Discord but nothing sends.

### Valuable V1 patterns to preserve

- **State machine + compare-and-set** (`trading_floor.py` lines 6-106, 131-146) — THE concurrency standard for V2
- **Trade Conviction Score** (`trade_conviction.py`) — 5 weighted layers, hard gate (F = block), circuit breaker
- **Market Regime** (`market_regime.py`) — composite score (-100 to +100), pure math
- **Proposal Engine** (`proposal_engine.py`) — pattern detection + evidence-based prompt-change proposals
- **Memory system** (`memory_manager.py`, `rag_search.py`, `vector_store.py`) — tiered, hybrid, pluggable
- **Context Snapshots** (`context_snapshot_service.py`) — compresses long conversations for chain continuation
- **Config Manager** (`config.py` + `config_loader.py`) — file + DB two-layer pattern
- **Scheduler** (`scheduler.py`) — central job registry, candle retention, DB maintenance
- **Candle retention** (`candle_collector.py` `_cleanup_old_candles`) — per-timeframe config

---

## 5. V2 Architecture — The Synthesis

### Core principle

V2 combines both approaches:

- **Backtest-driven foundation** (from Capital 2.0): Every strategy MUST be backtested before it
  trades. No "LLM thinks this is a good trade" without historical evidence.
- **AI intelligence layer** (from JarvAIs V1): AI validates, enriches, and discovers opportunities
  that pure indicators miss. But AI is ONE call per decision (ContextSynthesizer), not a multi-LLM
  circus.
- **Unified validation** (NEW in V2): Rule 1 — every trade is tracked against its backtest
  prediction. This bridges the gap that neither V1 nor Capital 2.0 closed.

### Agent vs Service Redesign

Gemini review identified that 8 of 18 "agents" are pure computation with no LLM. Verified against
actual V1 code. V2 splits into true LLM agents and shared Python services.

**Important nuance**: OpenClaw/Paperclip agents with cron/soul/heartbeat can WRAP services. The
agent IDENTITY (soul, memory, heartbeat) is separate from whether the WORK uses LLM. A
"ValidationEngineer agent" can have a soul and heartbeat but internally call a pure-math service.

#### True LLM Agents (~6, run on OpenClaw/Paperclip)

**CEO Agent** — Top-level governance. Receives ALL requests from human via Paperclip Agent Chat
or Telegram. Delegates to divisions. Approves strategies. Manages budgets. 80-90% LLM narrative.
Heartbeat: 30 min. LLM Budget: $50/month.

**ContextSynthesizer** — THE AI decision point. Takes all data (signals, news, derivatives, regime,
indicators) and makes ONE structured LLM call. Outputs structured JSON: direction, confidence,
entry, SL, TP, reasoning. Replaces V1's multi-LLM circus. 75-85% LLM (main cost driver).
Heartbeat: triggered per trade decision. LLM Budget: $100/month.

**NewsIntel** — Collects and classifies news, sentiment, social signals. Uses MCPs (TradingView,
Gloria AI, CoinGecko, Finlight, Exa Search) + RSS + Telegram. 50-70% LLM for classification.
Heartbeat: 15 min + on-demand. LLM Budget: $10/month.

**PerformanceAuditor** — AI post-mortems on every closed trade. Runs unified P&L engine. Compares
hypothesis vs outcome. Identifies systemic issues. 60-80% LLM. Heartbeat: post-close + daily.
LLM Budget: $15/month.

**Scout** — Scans for opportunities. Momentum, volume spikes, unusual activity. Hybrid: pure math
for scanning, LLM for Desk Brief and advisor paths. Heartbeat: scheduled. LLM Budget: $10/month.

**StrategyOptimizer** — Weekly backtests, walk-forward optimization, parameter changes. LLM for
analysis/proposals, math for actual backtests. Can offload heavy compute to home server.
Heartbeat: weekly or every 20 trades. LLM Budget: $10/month.

**Total LLM budget: ~$195/month** (down from V1's ~$300+)

#### Shared Python Services (~11, no LLM)

| Service | Source | What it does |
|---|---|---|
| `candle_service` | Both | OHLCV collection with adapter pattern, `data_hash` on write, retention |
| `indicator_engine` | Both | 250+ unified indicators with `IndicatorResult` interface, confluence |
| `regime_service` | JarvAIs | Market regime detection, composite score, sizing guidance |
| `risk_service` | JarvAIs | Trade conviction scoring, hard gates, circuit breakers |
| `position_sizing` | Capital 2.0 | `(funds * leverage) / price`, window allocation, carry-over |
| `pnl_engine` | NEW | Unified P&L with fee breakdown, margin model awareness |
| `exchange_connector` | Both | CCXT execution, waterfall routing, idempotency |
| `backtest_service` | Capital 2.0 | Strategy executor + batch runner + optimizer |
| `validation_service` | NEW | Rule 1 enforcement, signal re-run, drift detection |
| `instrument_service` | Both | Universal instrument normalization |
| `notification_service` | NEW | Outbound Telegram/Discord alerts |

#### Shared Python Libraries (~6)

| Library | Source | What it does |
|---|---|---|
| `config_manager` | JarvAIs | File + DB two-layer config with namespaces |
| `memory_service` | JarvAIs | Mem0 + Qdrant + hybrid RAG search |
| `cost_tracker` | JarvAIs | API cost attribution + budget caps |
| `circuit_breaker` | NEW | Reusable tenacity-style retry/breaker for all I/O |
| `db_pool` | Capital 2.0 | Connection pool + batch operations |
| `param_hash` | Capital 2.0 | SHA-256 with recursive key sort, test vectors |

#### Observer Agents (3, run on OpenClaw/Paperclip with cheap models)

Set up during build phase to watch development:

- **CodeEngineer**: Scans codebase every 15 min, understands changes, provides feedback to CEO
- **DBSpecialist**: Scans database schemas, understands data flows, validates integrity
- **QualityAuditor**: Checks for errors, debugging, validates that code meets objectives

These use cost-effective LLM models, focused on observation and understanding, not expensive
real-time discussion. They converse asynchronously and report to the CEO.

---

## 6. Normalized Database Schema

Company-agnostic. No bias toward JarvAIs or Capital 2.0. Standard names. Fewer tables, well-purposed.
Every table serves crypto, CFD, MT5, gold, stocks — anything.

### Naming Convention

- Table names: `snake_case`, plural nouns (e.g. `instruments`, `trades`, `candles`)
- Column names: `snake_case` (e.g. `entry_price`, `created_at`)
- No system-specific prefixes (no `apex_`, no `capital_`, no `ea_`)
- Boolean columns: `is_` prefix (e.g. `is_active`, `is_fake`)
- Timestamp columns: `_at` suffix (e.g. `created_at`, `opened_at`, `closed_at`)
- Price columns: `_price` suffix (e.g. `entry_price`, `exit_price`, `stop_loss_price`)
- Percentage columns: `_pct` suffix (e.g. `win_rate_pct`, `slippage_pct`)

### Database Layout

```
tickles_shared        — market data, indicators, strategies, backtests, news, system config
tickles_jarvais       — JarvAIs Trading Co: accounts, trades, positions, validations, agent state
tickles_capital       — Capital CFD Co (future): identical structure
tickles_explorer      — Explorer/Sandbox (future): identical structure
```

### SHARED DATABASE: `tickles_shared` (14 tables)

#### instruments
Merges: JarvAIs `market_symbols` + Capital 2.0 `epics` + `marketInfo`

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `symbol` | `VARCHAR(50) NOT NULL` | Normalized: `BTC/USDT`, `AAPL`, `GOLD` |
| `exchange` | `VARCHAR(50)` | `binance`, `capital.com`, `mt5` |
| `asset_class` | `ENUM('crypto','cfd','stock','forex','commodity','index')` | |
| `base_currency` | `VARCHAR(20)` | `BTC`, `AAPL`, `XAU` |
| `quote_currency` | `VARCHAR(20)` | `USDT`, `USD`, `GBP` |
| `min_size` | `DECIMAL(20,8)` | Minimum order size |
| `max_size` | `DECIMAL(20,8)` | Maximum order size |
| `size_increment` | `DECIMAL(20,8)` | Step size |
| `contract_multiplier` | `DECIMAL(20,8) DEFAULT 1` | For CFD/futures |
| `spread_pct` | `DECIMAL(10,6)` | Typical spread |
| `maker_fee_pct` | `DECIMAL(10,6)` | |
| `taker_fee_pct` | `DECIMAL(10,6)` | |
| `overnight_funding_long_pct` | `DECIMAL(15,10)` | Daily rate |
| `overnight_funding_short_pct` | `DECIMAL(15,10)` | Daily rate |
| `margin_factor` | `DECIMAL(10,4)` | |
| `max_leverage` | `INT` | |
| `opening_hours` | `JSON` | Parsed from broker |
| `is_active` | `BOOLEAN DEFAULT TRUE` | |
| `last_synced_at` | `DATETIME(3)` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| `updated_at` | `DATETIME(3) ... ON UPDATE` | |
| **UNIQUE** | `(symbol, exchange)` | |
| **INDEX** | `(asset_class)`, `(is_active)` | |

#### candles
Merges: JarvAIs `candles` + Capital 2.0 `candles`

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `instrument_id` | `BIGINT NOT NULL` | FK -> instruments |
| `timeframe` | `ENUM('1m','5m','15m','30m','1h','4h','1d','1w')` | |
| `source` | `VARCHAR(30)` | `ccxt`, `capital_api`, `yahoo`, `mt5` |
| `timestamp` | `DATETIME(3) NOT NULL` | Candle open time |
| `open` | `DECIMAL(20,8)` | |
| `high` | `DECIMAL(20,8)` | |
| `low` | `DECIMAL(20,8)` | |
| `close` | `DECIMAL(20,8)` | |
| `volume` | `DECIMAL(30,8)` | |
| `open_bid` | `DECIMAL(20,8)` | NULL for crypto |
| `close_ask` | `DECIMAL(20,8)` | NULL for crypto |
| `is_fake` | `BOOLEAN DEFAULT FALSE` | Capital 2.0 fake 5m candle |
| `fake_source_timestamp` | `DATETIME(3)` | When fake was calculated |
| `fake_comment` | `VARCHAR(200)` | How fake was built |
| `data_hash` | `CHAR(64)` | SHA-256 of OHLCV for drift detection (Rule 1) |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **UNIQUE** | `(instrument_id, source, timeframe, timestamp)` | |
| **INDEX** | `(instrument_id, timeframe)`, `(timestamp)` | |
| **PARTITION** | `RANGE (TO_DAYS(timestamp))` monthly | |

#### indicator_catalog
Registry of all ~250 indicators from both systems.

| Column | Type | Notes |
|---|---|---|
| `id` | `INT AUTO_INCREMENT PK` | |
| `name` | `VARCHAR(100) NOT NULL UNIQUE` | `rsi_oversold`, `smc_fvg_bullish`, etc. |
| `category` | `ENUM('momentum','trend','volatility','volume','smart_money','breakout','pullback','crash_protection','combination')` | |
| `direction` | `ENUM('bullish','bearish','neutral')` | |
| `description` | `TEXT` | |
| `default_params` | `JSON` | `{"period": 14, "threshold": 30}` |
| `param_ranges` | `JSON` | `{"period": [7,14,21], "threshold": [20,25,30,35]}` |
| `source_system` | `VARCHAR(30)` | `capital2`, `jarvais`, `v2_new` |
| `is_active` | `BOOLEAN DEFAULT TRUE` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| `updated_at` | `DATETIME(3) ... ON UPDATE` | |

#### indicators
Computed indicator values, stored for reuse and audit.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `instrument_id` | `BIGINT NOT NULL` | |
| `timeframe` | `ENUM(...)` | |
| `indicator_name` | `VARCHAR(100) NOT NULL` | Standardized name |
| `params_hash` | `CHAR(64)` | SHA-256 of sorted params JSON |
| `params` | `JSON` | Full parameters used |
| `signal` | `BOOLEAN` | TRUE = condition fired |
| `value` | `DECIMAL(20,8)` | Numeric value (RSI=72.5) |
| `metadata` | `JSON` | Extra indicator-specific data |
| `calculated_at` | `DATETIME(3) NOT NULL` | |
| `candle_timestamp` | `DATETIME(3) NOT NULL` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **INDEX** | `(instrument_id, indicator_name, timeframe)`, `(params_hash)` | |

#### strategies
Merges: Capital 2.0 `savedStrategies` + `strategy_templates` + JarvAIs `trading_strategies`

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `name` | `VARCHAR(200) NOT NULL` | |
| `description` | `TEXT` | |
| `instrument_id` | `BIGINT` | NULL = applicable to any |
| `asset_class` | `ENUM(...)` | Which asset classes it works for |
| `conflict_resolution` | `ENUM('sharpe','return','win_rate','first_signal')` | |
| `halt_threshold_pct` | `DECIMAL(5,2) DEFAULT 80.00` | Rule 1: configurable per strategy |
| `is_active` | `BOOLEAN DEFAULT TRUE` | |
| `is_archived` | `BOOLEAN DEFAULT FALSE` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| `updated_at` | `DATETIME(3) ... ON UPDATE` | |
| **INDEX** | `(is_active, asset_class)` | |

#### strategy_dna_strands
Normalizes Capital 2.0's `savedStrategies.dnaStrands` JSON.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `strategy_id` | `BIGINT NOT NULL` | FK -> strategies |
| `indicator_catalog_id` | `INT NOT NULL` | FK -> indicator_catalog |
| `timeframe` | `ENUM(...)` | |
| `params` | `JSON` | Override params for this strand |
| `params_hash` | `CHAR(64)` | SHA-256 for dedup |
| `source_backtest_id` | `BIGINT` | FK -> backtest_results |
| `priority` | `INT DEFAULT 0` | Order within strategy |
| `is_active` | `BOOLEAN DEFAULT TRUE` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **UNIQUE** | `(strategy_id, indicator_catalog_id, timeframe, params_hash)` | |
| **INDEX** | `(indicator_catalog_id)` | "Which strategies use this indicator?" |

#### strategy_windows
Normalizes Capital 2.0's `savedStrategies.windowConfig` JSON.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `strategy_id` | `BIGINT NOT NULL` | FK -> strategies |
| `window_close_time` | `TIME NOT NULL` | e.g. `16:30:00` |
| `allocation_pct` | `DECIMAL(5,2)` | |
| `carry_over_enabled` | `BOOLEAN DEFAULT FALSE` | |
| `is_active` | `BOOLEAN DEFAULT TRUE` | |
| **UNIQUE** | `(strategy_id, window_close_time)` | |

#### backtest_results
Merges: Capital 2.0 `backtestResults` + `testedHashes`. Dedup by param_hash.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `instrument_id` | `BIGINT NOT NULL` | |
| `indicator_name` | `VARCHAR(100)` | |
| `param_hash` | `CHAR(64) NOT NULL UNIQUE` | SHA-256 dedup |
| `params` | `JSON` | Full parameter set |
| `timeframe` | `ENUM(...)` | |
| `date_from` | `DATE` | |
| `date_to` | `DATE` | |
| `initial_balance` | `DECIMAL(20,8)` | |
| `final_balance` | `DECIMAL(20,8)` | |
| `total_return_pct` | `DECIMAL(10,4)` | |
| `total_trades` | `INT` | |
| `win_rate_pct` | `DECIMAL(5,2)` | |
| `sharpe_ratio` | `DECIMAL(10,4)` | |
| `max_drawdown_pct` | `DECIMAL(10,4)` | |
| `profit_factor` | `DECIMAL(10,4)` | |
| `total_fees` | `DECIMAL(20,8)` | |
| `total_spread_costs` | `DECIMAL(20,8)` | |
| `total_overnight_costs` | `DECIMAL(20,8)` | |
| `candle_data_hash` | `CHAR(64)` | Rule 1: hash of candles used |
| `engine_version` | `VARCHAR(20)` | |
| `run_duration_ms` | `INT` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **INDEX** | `(instrument_id, indicator_name)`, `(total_return_pct)`, `(sharpe_ratio)` | |

#### backtest_trade_details
Normalizes Capital 2.0's `backtestResults.trades` JSON.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `backtest_result_id` | `BIGINT NOT NULL` | FK -> backtest_results |
| `trade_index` | `INT` | |
| `entry_price` | `DECIMAL(20,8)` | |
| `exit_price` | `DECIMAL(20,8)` | |
| `direction` | `ENUM('long','short')` | |
| `entry_at` | `DATETIME(3)` | |
| `exit_at` | `DATETIME(3)` | |
| `quantity` | `DECIMAL(20,8)` | |
| `gross_pnl` | `DECIMAL(20,8)` | |
| `spread_cost` | `DECIMAL(20,8)` | |
| `overnight_cost` | `DECIMAL(20,8)` | |
| `net_pnl` | `DECIMAL(20,8)` | |
| `is_winner` | `BOOLEAN` | |
| `window_close_time` | `TIME` | |
| `signal_candle_hash` | `CHAR(64)` | |
| **INDEX** | `(backtest_result_id)` | |

#### backtest_queue
Work queue for parallel backtest execution with dedup.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `param_hash` | `CHAR(64) NOT NULL UNIQUE` | |
| `instrument_id` | `BIGINT` | |
| `indicator_name` | `VARCHAR(100)` | |
| `params` | `JSON` | |
| `status` | `ENUM('pending','claimed','running','completed','failed')` | |
| `worker_id` | `VARCHAR(50)` | |
| `claimed_at` | `DATETIME(3)` | |
| `completed_at` | `DATETIME(3)` | |
| `result_id` | `BIGINT` | FK -> backtest_results |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **INDEX** | `(status, claimed_at)` | |

#### news_items [maybe call alpha_items]
All ingested news, social, TradingView ideas.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `hash_key` | `CHAR(64) NOT NULL UNIQUE` | Dedup |
| `source` | `VARCHAR(50)` | `telegram`, `discord`, `rss_yahoo`, `tradingview`, `coinglass` |
| `headline` | `TEXT` | |
| `content` | `MEDIUMTEXT` | |
| `sentiment` | `ENUM('bullish','bearish','neutral','mixed')` | |
| `instruments` | `JSON` | Array of related symbols |
| `published_at` | `DATETIME(3)` | |
| `collected_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **INDEX** | `(source, collected_at)`, `(published_at)` | |

#### derivatives_snapshots
OI, funding rates, liquidation data from CoinGlass etc.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `instrument_id` | `BIGINT NOT NULL` | |
| `snapshot_at` | `DATETIME(3) NOT NULL` | |
| `open_interest` | `DECIMAL(30,8)` | |
| `funding_rate` | `DECIMAL(15,10)` | |
| `long_short_ratio` | `DECIMAL(10,4)` | |
| `liquidation_volume_24h` | `DECIMAL(30,8)` | |
| `source` | `VARCHAR(50)` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **UNIQUE** | `(instrument_id, source, snapshot_at)` | |

#### system_config
Global key-value configuration with namespaces.

| Column | Type | Notes |
|---|---|---|
| `id` | `INT AUTO_INCREMENT PK` | |
| `namespace` | `VARCHAR(50) NOT NULL` | `global`, `backtest`, `risk`, `indicators` |
| `config_key` | `VARCHAR(100) NOT NULL` | |
| `config_value` | `TEXT` | |
| `is_secret` | `BOOLEAN DEFAULT FALSE` | |
| `updated_at` | `DATETIME(3) ... ON UPDATE` | |
| **UNIQUE** | `(namespace, config_key)` | |

#### api_cost_log
Every external API call with cost attribution.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `provider` | `VARCHAR(50)` | `openai`, `anthropic`, `capital.com`, `binance` |
| `model` | `VARCHAR(100)` | |
| `role` | `VARCHAR(50)` | Which agent/service |
| `context` | `VARCHAR(100)` | What it was for |
| `tokens_in` | `INT` | |
| `tokens_out` | `INT` | |
| `cost_usd` | `DECIMAL(10,6)` | |
| `latency_ms` | `INT` | |
| `company_id` | `VARCHAR(50)` | Which company's budget |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **INDEX** | `(company_id, created_at)`, `(role, created_at)` | |

### PER-COMPANY DATABASE: `tickles_[company]` (10 tables)

Every company gets identical table structures. Only the data differs.

#### accounts
Merges: JarvAIs `trading_accounts` + Capital 2.0 `accounts`

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `exchange` | `VARCHAR(50)` | `binance`, `capital.com`, `mt5` |
| `account_id_external` | `VARCHAR(100)` | Exchange's account ID |
| `account_type` | `ENUM('demo','live')` | |
| `api_key_ref` | `VARCHAR(100)` | Reference to secret store (NOT the key itself) |
| `balance` | `DECIMAL(20,8)` | Last known |
| `equity` | `DECIMAL(20,8)` | |
| `margin_used` | `DECIMAL(20,8)` | |
| `currency` | `VARCHAR(10)` | `USD`, `USDT`, `GBP` |
| `is_active` | `BOOLEAN DEFAULT TRUE` | |
| `session_state` | `JSON` | Runtime session (window allocations, carry-over) |
| `session_state_version` | `INT DEFAULT 0` | Optimistic concurrency |
| `last_synced_at` | `DATETIME(3)` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| `updated_at` | `DATETIME(3) ... ON UPDATE` | |
| **UNIQUE** | `(exchange, account_id_external)` | |

#### trades
THE trade table. Every real trade, paper trade, shadow trade.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `account_id` | `BIGINT NOT NULL` | FK -> accounts |
| `instrument_id` | `BIGINT NOT NULL` | FK -> tickles_shared.instruments |
| `strategy_id` | `BIGINT` | FK -> tickles_shared.strategies |
| `trade_type` | `ENUM('live','paper','shadow')` | Discriminator |
| `direction` | `ENUM('long','short')` | |
| `status` | `ENUM('pending','open','partial_close','closed','cancelled','failed')` | |
| `quantity` | `DECIMAL(20,8)` | |
| `quantity_type` | `ENUM('units','lots','contracts')` | Margin model awareness |
| `contract_size` | `DECIMAL(20,8) DEFAULT 1` | For CFD/MT5 |
| `leverage` | `INT` | At time of entry |
| `entry_price` | `DECIMAL(20,8)` | Actual fill |
| `exit_price` | `DECIMAL(20,8)` | Actual fill |
| `expected_entry_price` | `DECIMAL(20,8)` | What signal/brain predicted |
| `expected_exit_price` | `DECIMAL(20,8)` | What brain/TP predicted |
| `stop_loss_price` | `DECIMAL(20,8)` | |
| `take_profit_1` | `DECIMAL(20,8)` | |
| `take_profit_2` | `DECIMAL(20,8)` | |
| `take_profit_3` | `DECIMAL(20,8)` | |
| `gross_pnl` | `DECIMAL(20,8)` | Before fees |
| `net_pnl` | `DECIMAL(20,8)` | After all fees |
| `entry_slippage` | `DECIMAL(20,8)` | expected - actual |
| `exit_slippage` | `DECIMAL(20,8)` | |
| `entry_slippage_pct` | `DECIMAL(10,6)` | |
| `exit_slippage_pct` | `DECIMAL(10,6)` | |
| `signal_to_fill_ms` | `INT` | |
| `order_to_fill_ms` | `INT` | |
| `exchange_order_id` | `VARCHAR(100)` | |
| `exchange_deal_id` | `VARCHAR(100)` | For Capital.com |
| `winning_strand_id` | `BIGINT` | FK -> strategy_dna_strands |
| `conflict_exists` | `BOOLEAN` | |
| `brain_calc_price` | `DECIMAL(20,8)` | Price brain used |
| `fake_candle_close` | `DECIMAL(20,8)` | If fake candle used |
| `price_variance_pct` | `DECIMAL(10,6)` | Brain vs actual |
| `brain_snapshot_detail` | `JSON` | Full DNA results for forensic replay |
| `brain_snapshot_version` | `INT DEFAULT 1` | Schema version |
| `candle_data_hash` | `CHAR(64)` | Rule 1 |
| `signal_params_hash` | `CHAR(64)` | Rule 1 |
| `window_close_time` | `TIME` | |
| `signal_at` | `DATETIME(3)` | |
| `ordered_at` | `DATETIME(3)` | |
| `opened_at` | `DATETIME(3)` | |
| `closed_at` | `DATETIME(3)` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| `updated_at` | `DATETIME(3) ... ON UPDATE` | |
| **INDEX** | `(account_id, status)`, `(instrument_id, opened_at)`, `(strategy_id, closed_at)` | |
| **INDEX** | `(trade_type, status)`, `(exchange_order_id)`, `(exchange_deal_id)`, `(candle_data_hash)` | |

#### trade_cost_entries
Unified fee ledger. One row per fee event. Multi-day overnight = one row per night.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `trade_id` | `BIGINT NOT NULL` | FK -> trades |
| `cost_type` | `ENUM('maker_fee','taker_fee','spread','overnight_funding','swap','commission','guaranteed_stop','slippage','other')` | |
| `amount` | `DECIMAL(20,8)` | Positive = cost |
| `currency` | `VARCHAR(10)` | |
| `accrued_at` | `DATETIME(3)` | |
| `description` | `VARCHAR(200)` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **INDEX** | `(trade_id)`, `(cost_type, accrued_at)` | |

#### order_events
Full order lifecycle tracking.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `trade_id` | `BIGINT NOT NULL` | FK -> trades |
| `event_type` | `ENUM('submitted','accepted','partial_fill','filled','cancelled','rejected','amended','expired')` | |
| `price` | `DECIMAL(20,8)` | |
| `quantity_filled` | `DECIMAL(20,8)` | Cumulative |
| `exchange_timestamp` | `DATETIME(3)` | |
| `raw_response` | `JSON` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **INDEX** | `(trade_id, created_at)` | |

#### trade_validations
Rule 1 enforcement. Per-company (not shared — Gemini fix #2).

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `trade_id` | `BIGINT NOT NULL` | FK -> trades |
| `strategy_id` | `BIGINT NOT NULL` | |
| `backtest_result_id` | `BIGINT` | FK -> tickles_shared.backtest_results |
| `signal_match` | `BOOLEAN` | |
| `entry_price_delta` | `DECIMAL(20,8)` | |
| `exit_price_delta` | `DECIMAL(20,8)` | |
| `pnl_delta` | `DECIMAL(20,8)` | |
| `pnl_delta_pct` | `DECIMAL(10,6)` | |
| `slippage_contribution` | `DECIMAL(20,8)` | |
| `fee_contribution` | `DECIMAL(20,8)` | |
| `data_drift_detected` | `BOOLEAN DEFAULT FALSE` | |
| `original_candle_hash` | `CHAR(64)` | |
| `validation_candle_hash` | `CHAR(64)` | |
| `validated_at` | `DATETIME(3)` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **INDEX** | `(trade_id)`, `(strategy_id, validated_at)` | |

#### balance_snapshots
Periodic account balance for reconciliation.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `account_id` | `BIGINT NOT NULL` | |
| `balance` | `DECIMAL(20,8)` | |
| `equity` | `DECIMAL(20,8)` | |
| `margin_used` | `DECIMAL(20,8)` | |
| `unrealized_pnl` | `DECIMAL(20,8)` | |
| `snapshot_source` | `ENUM('exchange_api','calculated','manual')` | |
| `snapshot_at` | `DATETIME(3)` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **INDEX** | `(account_id, snapshot_at)` | |

#### leverage_history
Audit trail for leverage changes.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `account_id` | `BIGINT NOT NULL` | |
| `instrument_id` | `BIGINT NOT NULL` | |
| `old_leverage` | `INT` | |
| `new_leverage` | `INT` | |
| `changed_by` | `VARCHAR(50)` | |
| `reason` | `VARCHAR(200)` | |
| `changed_at` | `DATETIME(3)` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **INDEX** | `(account_id, changed_at)` | |

#### agent_state
Per-agent runtime state.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `agent_name` | `VARCHAR(100) NOT NULL UNIQUE` | |
| `status` | `ENUM('active','paused','error','stopped')` | |
| `last_heartbeat_at` | `DATETIME(3)` | |
| `last_error` | `TEXT` | |
| `state_data` | `JSON` | |
| `state_version` | `INT DEFAULT 0` | Optimistic concurrency |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| `updated_at` | `DATETIME(3) ... ON UPDATE` | |

#### strategy_lifecycle
Audit trail of strategy status transitions.

| Column | Type | Notes |
|---|---|---|
| `id` | `BIGINT AUTO_INCREMENT PK` | |
| `strategy_id` | `BIGINT NOT NULL` | |
| `from_status` | `VARCHAR(30)` | |
| `to_status` | `VARCHAR(30)` | `draft`, `paper`, `live`, `halted`, `archived` |
| `changed_by` | `VARCHAR(100)` | |
| `reason` | `TEXT` | |
| `changed_at` | `DATETIME(3)` | |
| `created_at` | `DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3)` | |
| **INDEX** | `(strategy_id, changed_at)` | |

#### company_config
Per-company settings.

| Column | Type | Notes |
|---|---|---|
| `id` | `INT AUTO_INCREMENT PK` | |
| `config_key` | `VARCHAR(100) NOT NULL UNIQUE` | |
| `config_value` | `TEXT` | |
| `updated_at` | `DATETIME(3) ... ON UPDATE` | |

### Table Count Summary

**Shared (`tickles_shared`):** 14 tables
**Per-company (`tickles_[company]`):** 10 tables
**Total: 24 normalized tables** replacing 80+ tables across both legacy systems.

### Margin model handling in the trades table

The `quantity_type` column determines how P&L is calculated:

**Crypto** (`quantity_type = 'units'`):
- Margin = notional / leverage
- P&L = (exit_price - entry_price) * quantity * direction_multiplier
- Leverage can change mid-trade (via set_leverage). Track in `leverage_history`.
- Fees: maker/taker as percentage of notional. Track in `trade_cost_entries`.

**CFD** (`quantity_type = 'contracts'`):
- Leverage fixed at open, cannot change mid-trade
- P&L = (exit_price - entry_price) * quantity * contract_multiplier * direction_multiplier
- Additional: spread (built into prices) + overnight funding. Track multi-day in `trade_cost_entries`.

**MetaTrader** (`quantity_type = 'lots'`):
- 1 standard lot = 100,000 units, mini = 0.1, micro = 0.01
- Leverage set at ACCOUNT level, not per-trade
- P&L = (exit_price - entry_price) * quantity * contract_size
- Swap/rollover fees. Track in `trade_cost_entries`.

---

## 7. Unified Indicator Architecture

### The Problem

Capital 2.0 has 221 indicators returning `bool`. JarvAIs has ~30 returning rich dicts. Different
names, interfaces, parameter formats. Neither supports easy confluence testing.

### The Solution

One indicator engine. One interface. One catalog. Works for crypto, CFD, gold, stocks.

### Standard Indicator Interface

Every indicator is a pure function: `(DataFrame, index, **params) -> IndicatorResult`

```python
@dataclass
class IndicatorResult:
    signal: bool           # Did this condition fire?
    value: float           # Numeric value (RSI=72.5, MACD=0.003)
    confidence: float      # 0.0-1.0 (how strong)
    metadata: dict         # Indicator-specific extras
```

### Confluence Service (NEW for V2)

Runs N indicators on the same candle data and scores agreement:

```python
def evaluate_confluence(
    instrument_id: int,
    timeframe: str,
    indicator_configs: list[dict],
    candle_data: DataFrame,
    index: int
) -> ConfluenceResult:
    # Returns:
    #   individual_signals: list of IndicatorResult per config
    #   agreement_count: how many fired
    #   agreement_pct: percentage agreement
    #   weighted_score: weighted by historical accuracy per indicator
    #   combined_confidence: aggregated confidence
```

This enables:
- **Single indicator backtests** (existing Capital 2.0 approach)
- **Multi-indicator confluence backtests** ("if RSI + MACD + SMC all agree, trade")
- **Random sweep across indicator combinations** (try all 2-of-250 combos, find best pairs)
- **Per-asset-class performance tracking** (which indicators work best for crypto vs CFD vs gold)

### Migration Path

1. Port Capital 2.0's 221 indicators as-is (already Python, `(df, idx, **kwargs) -> bool`)
2. Wrap each to return `IndicatorResult` instead of bare `bool`
3. Port JarvAIs' ~30 SMC/order flow indicators (convert dict returns to `IndicatorResult`)
4. Register all ~250 in `indicator_catalog` table
5. Build confluence service on top

### Crash/Regime Detection as Common Service

Both systems have crash detection (Capital 2.0 in `crash_protection` indicator category, JarvAIs
in market regime). V2 unifies this: the regime service computes a score per instrument/timeframe,
and ANY company's strategy can reference it. Bitcoin dropping, gold dropping, stocks dropping —
same candles, same math, same detection. Just different instruments.

---

## 8. Reusable Components Inventory

47 total components identified across both systems. Each rated GOOD, MEDIOCRE, or BAD based on
actual code review.

### From JarvAIs V1 (18 components)

| Component | File | Rating | V2 Action |
|---|---|---|---|
| State Machine + Compare-and-Set | `trading_floor.py` (lines 6-106, 131-146) | GOOD | V2 concurrency standard |
| Market Regime Detector | `market_regime.py` | GOOD | Fix duration tracking |
| Trade Conviction Score | `trade_conviction.py` | GOOD | Keep 5-layer scoring + gates |
| Shadow/Staged Order Queue | `shadow_queue.py` | GOOD | Reuse as production execution queue |
| Exchange Truth Sync | `live_trade_monitor.py` (903-946) | GOOD | Add order_events writing |
| Phased Post-Mortem Audit | `auditor.py` | GOOD | Keep 4-phase idempotent pattern |
| Proposal Engine | `proposal_engine.py` | GOOD | Keep self-improvement mechanism |
| Tiered Memory Manager | `core/memory_manager.py` | GOOD | Keep Mem0 integration |
| Hybrid RAG Search | `core/rag_search.py` | GOOD | Keep semantic + keyword + boosts |
| Vector Store Abstraction | `core/vector_store.py` | GOOD | Keep pluggable backends |
| Context Snapshot Service | `context_snapshot_service.py` | GOOD | Keep chain continuation |
| Config Manager | `core/config.py` + `config_loader.py` | GOOD | Merge, add namespaces |
| Scheduler | `core/scheduler.py` | GOOD | Add candle retention jobs |
| API Cost Attribution | `ai_api_log` + daily rollups | GOOD | Add budget caps |
| Candle Retention | `candle_collector.py` `_cleanup_old_candles` | GOOD | Port + add partitioning |
| Chart Generator | `core/chart_generator.py` | GOOD | Keep DB-driven charts |
| Trade Dedup | `trade_dedup.py` | MEDIOCRE | Add idempotency key + DB lock |
| Hallucination Auditor | `hallucination_auditor.py` | MEDIOCRE | Good governance pattern, weak detection |

### From Capital 2.0 (17 components)

| Component | File | Rating | V2 Action |
|---|---|---|---|
| DNA Strands | `savedStrategies.dnaStrands` | GOOD | Normalize to junction table |
| paramHash | `batch_runner.py` (117-179) | GOOD | Write test suite |
| Conflict Resolution | `strategy_executor.py` (784-790) | GOOD | Port pattern |
| Strategy Executor | `strategy_executor.py` | GOOD | Add candle_data_hash |
| Batch Runner | `batch_runner.py` | GOOD | Update to V2 schema |
| Brain Parity | `brain.ts` | GOOD | Same engine for backtest + live |
| Fake Candle System | `candle_data_service.ts` | GOOD | Port and document |
| Strategy Comparison | `compare_hold_strategy.py`, `detailed_trade_comparison.py` | GOOD | Invaluable for validation |
| Position Sizing | `open_orchestrator.ts` + `strategy_executor.py` | GOOD | Clear risk/execution split |
| Window Allocation + Carry-Over | `fund_tracker.ts` + `trading_session_state.ts` | GOOD | Add version column |
| Leverage Manager | `leverage_checker.ts` | GOOD | Add history table |
| Liquidation Calculator | `liquidation_utils.py` | GOOD | Already Python |
| Deal Confirmation + Retry | `deal_confirmation_service.ts` | GOOD | Port retry pattern |
| Trade Sync / Reconciliation | `trade_sync_service.ts` | GOOD | Merge with JarvAIs monitor |
| Trade Queue + Priority | `trade_queue.ts` | GOOD | Port state machine |
| Adaptive Timing | `adaptive_timing.py` | GOOD | Already Python |
| Resource Manager | `resource_manager.ts` | GOOD | Port logical core allocation |

---

## 9. What to Build New

These don't exist in either system:

1. **Unified P&L Engine** — fees, overnight funding multi-day accrual, `quantity_type` support, margin model awareness. Uses `trade_cost_entries` table.
2. **Outbound Notification Service** — Telegram bot + Discord webhook for trade alerts and system health.
3. **Generic Circuit Breaker / Retry Library** — tenacity-style wrapper for all I/O (exchange, LLM, DB).
4. **Budget Cap / Kill Switch** — per-agent LLM budget with auto-pause on threshold.
5. **Backtest-to-Live Validation Engine** — automated continuous validation with `candle_data_hash` and drift detection. Uses `trade_validations` table.
6. **Instrument Normalization Service** — universal `instruments` table as source of truth across all exchanges.
7. **Candle Data Hash Pipeline** — SHA-256 on write, compare on re-validation, detect drift vs strategy failure.
8. **Leverage History / Audit Trail** — `leverage_history` table, populated by position monitor.
9. **Order Event Lifecycle** — `order_events` table, tracking submitted through filled/cancelled.
10. **Confluence Service** — multi-indicator agreement scoring (see Section 7).

---

## 10. Communication and Development Workflow

### The Structural Dance

**Build Phase (now through Week 4)**:
- Owner uses **VS Code Server on VPS** (browser via TailScale) for infrastructure.
- Owner uses **Paperclip Agent Chat** or **Telegram** to talk to CEO for agent-managed work.
- **Mem0** stores every architectural decision. **MemClaw** has the full project workspace.
- **CONTEXT_V3.md** (this file) is the complete reference.
- **Observer agents** (CodeEngineer, DBSpecialist, QualityAuditor) scan every 15 min.

**Operations Phase (Week 5+)**:
- Owner talks ONLY to CEO via Paperclip or Telegram.
- CEO handles all code changes, strategy updates, agent management.
- VS Code Server becomes emergency-only.

### Top-down development

Human -> CEO -> IDE. Never human -> IDE directly (except during build phase). The owner explicitly
said: "I don't want to be in the backend changing code and then OpenClaw doesn't know what I've done."

### Model agnostic architecture

- Primary IDE: VS Code Server on VPS (+ Claude Code for OpenClaw/Paperclip management)
- OpenRouter for multiple LLM models. Agents can select model per task.
- Per-company model budgets. Explorer company can use cheaper models.
- When Claude 5 / Opus 5 arrives, swap the model without rebuilding.

---

## 11. Multi-Company Architecture

### Company 1: JarvAIs Trading Co (START HERE)
- Asset type: Crypto perpetual futures
- Exchanges: Bybit (primary), BloFin, Bitget
- Starting capital: $500 on demo accounts
- Database: `tickles_jarvais`

### Company 2: Capital CFD Co (WHEN READY)
- Asset type: CFDs (stocks, indices, commodities)
- Exchange: Capital.com
- Database: `tickles_capital`

### Company 3: Explorer/Sandbox (FUTURE)
- Experimental strategies, new ideas, risky bets
- Cheaper LLM models. Small capital.
- Database: `tickles_explorer`

### Cross-company: Curiosity Agent (FUTURE)
- READ access across all company databases
- Spots patterns across asset classes
- Makes recommendations to company CEOs

---

## 12. Self-Improvement and Path to Autonomy

### How agents improve

1. **StrategyOptimizer**: Weekly backtests, better parameters, proposes changes to CEO.
2. **PerformanceAuditor**: AI post-mortems identify systemic issues.
3. **ValidationEngineer**: Identifies drift between backtest and live, triggers investigation.
4. **Proposal Engine**: Pattern detection (consecutive losses, symbol clusters, quick stop-outs),
   evidence-based prompt-change proposals with human gate.

### Autonomy phases

- **Phase A (weeks 1-4)**: Human approves EVERYTHING. Paper trading only. Accuracy tracking begins.
- **Phase B (weeks 5-8)**: Human approves strategy changes only. CEO auto-approves individual
  trades within approved strategies. Live trading begins with $500.
- **Phase C (months 3-6)**: Human approves budget increases only. CEO auto-approves strategy
  changes if backtest supports them. System self-optimizes.
- **Phase D (month 6+)**: Full autonomy. CEO manages within allocated budget. Human gets
  daily/weekly reports. Can override via Telegram.

### Ultimate vision

"I want all the companies to be profitable to the point where they are making their own decisions,
even editing code. The system becomes generative, then super intelligent. No human needed. Making
millions. Because I created it, I get profit share."

---

## 13. Infrastructure

### VPS Specs
- 12 vCPU cores, 48 GB RAM, 500 GB SSD
- Ubuntu 24.04, EU Region, $27.30/month

### Home Server (heavy backtesting)
- 64 GB RAM + GPU
- Runs full optimization sweeps locally
- Pushes results to `tickles_shared` via Tailscale + SSH tunnel + batch INSERT

### Software on VPS
- OpenClaw (CEO agent framework) + Paperclip (company management)
- Claude Code (primary IDE via ACP)
- MySQL (`tickles_shared` + per-company databases)
- Mem0 (two projects: `infrastructure-migration` + `trading-agents`)
- MemClaw (full project workspace)
- Git, Python 3, VS Code Server

### Subscriptions
| Service | Cost | What it provides |
|---|---|---|
| CoinGlass | $29/mo | Liquidation heatmaps, OI, funding rates |
| OpenRouter | Variable | Multiple LLM models |
| VPS | $27.30/mo | Hosting |
| Exchange APIs | Free | CCXT trading data |

---

## 14. Performance and Efficiency Design

### Database Performance
- Candle partitioning: `RANGE (TO_DAYS(timestamp))` monthly. Auto-create next month via scheduled job.
- Candle retention: 1m=30d, 5m=90d, 15m=6mo, 1h=2yr, 4h+=forever.
- All indicator queries use composite index on `(instrument_id, timeframe, timestamp)`.
- Backtest dedup: `INSERT...ON DUPLICATE KEY UPDATE` on `param_hash`.
- Connection pool: `max_connections=50` MySQL. Each service gets 5-10. Total < 50.

### In-Memory Efficiency
- All caches bounded with max_size + TTL + LRU eviction.
- Backtest workers: max 6 concurrent (half of 12 vCPU), ~4GB each.
- LLM workers: max 4 concurrent calls, queue excess with timeout.
- Never load full JSON blobs for list queries. Project columns. Paginate.

### Monitoring
- Agent resource tracking: memory, CPU time, DB connections per heartbeat.
- MySQL slow query log (threshold 1s). DBSpecialist reviews weekly.
- Cache hit ratio alert if < 60%.

---

## 15. Gemini Architectural Review — Verified Fixes

A third-party (Gemini) reviewed CONTEXT_V2.md and found 13 issues. We verified each against
actual code. Results below. All fixes are already incorporated into the schema and architecture above.

| # | Issue | Gemini's Claim | Verification | Status |
|---|---|---|---|---|
| 1 | DB Naming | `jarvais_shared` vs `tickles_` | CONFIRMED — real split between docs and VPS | Fixed: `tickles_shared` everywhere |
| 2 | Cross-DB FK | `trade_validations` can't FK to per-company `trades` | CONFIRMED — critical | Fixed: moved to per-company DB |
| 3 | DECIMAL precision | No precision specified | CONFIRMED | Fixed: standard defined (20,8 / 30,8 / 10,6 / 10,4) |
| 4 | JSON overuse | 6 critical JSON columns | PARTIALLY RIGHT | Fixed: dna_strands + windows normalized, brain_snapshot key fields promoted |
| 5 | Missing tables | 6 tables missing | PARTIALLY RIGHT — partial equivalents exist | Fixed: 7 new tables added |
| 6 | Rule 1 gaps | Fake candles, brain_snapshot, multi-day, threshold | Fake candles WRONG (well-implemented in code), rest CONFIRMED | Fixed: documented fake candles, versioned schema, configurable threshold |
| 7 | Agents vs Services | 8 agents are pure math | MOSTLY CONFIRMED against V1 code | Fixed: redesigned to 6 agents + 11 services |
| 8 | Margin model gaps | Missing overnight, lot_size, leverage audit | PARTIALLY RIGHT (some exist under different names) | Fixed: quantity_type, contract_size, trade_cost_entries, leverage_history |
| 9 | paramHash risk | Cross-language hash mismatch | MITIGATED (sorted keys in both), V2 is Python-only | Fixed: test suite required |
| 10 | Concurrency | fund_tracker purely in-memory | PARTIALLY WRONG (has DB persistence) but lacks locking | Fixed: version columns + compare-and-set |
| 11 | Candle drift | Exchanges adjust candles retroactively | CONFIRMED — real risk, neither system detects | Fixed: candle_data_hash + data_drift_detected |
| 12 | Data retention | 50M+ rows/year, no partitioning | PARTIALLY WRONG (V1 has retention) but no partitioning | Fixed: monthly partitioning + retention policy |
| 13 | Home-to-VPS | Latency and failure modes | VALID CONCERN | Fixed: Tailscale + batch INSERT pattern |

---

## 16. Implementation Plan — Step by Step

Build bottom-up. Each step must be COMPLETE before the next starts.

### Step 1: Reconcile Naming (blocks everything)
- Update all docs, configs, and VPS files to `tickles_shared` / `tickles_[company]`
- Update `.roo/rules/`, `.roo/skills/`, `CLAUDE.md` on VPS

### Step 2: Database Schema (DDL)
- Write and execute `tickles_shared.sql` (14 tables with all indexes, partitions, constraints)
- Write and execute `tickles_jarvais.sql` (10 tables)
- Test on local MySQL first, then deploy to VPS
- Seed `indicator_catalog` with ~250 indicators

### Step 3: VPS Infrastructure
- Verify VS Code Server, OpenClaw CEO, Paperclip company
- Create MySQL databases with designed schema
- Connect Mem0 to VPS environment
- Copy `CONTEXT_V3.md`, `mem0_bridge.py`, `.env` to VPS
- Initialize Git repo for V2 codebase
- Set up observer agents (CodeEngineer, DBSpecialist, QualityAuditor)

### Step 4: Data Collection Services
- Port candle collector with adapter pattern + `data_hash` + retention
- Port Telegram, Discord, RSS collectors to V2 schema
- Port TradingView idea monitor
- Port adaptive timing service
- Verify: data flows into `tickles_shared.candles` and `tickles_shared.news_items`

### Step 5: Indicator Engine
- Port Capital 2.0's 221 indicators with `IndicatorResult` wrapper
- Port JarvAIs' ~30 SMC indicators
- Build confluence evaluation service
- Register all in `indicator_catalog`
- Verify: can evaluate any indicator on any instrument's candles

### Step 6: Backtest Engine
- Port strategy executor + batch runner + optimizer to V2 schema
- Write to `backtest_trade_details` instead of JSON
- Add `candle_data_hash` on every run
- Implement paramHash test suite
- Build backtest queue with dedup
- Verify: backtests run, results stored, no duplicate runs

### Step 7: Strategy System
- Build strategy CRUD (DNA strands normalized, windows normalized)
- Implement conflict resolution
- Implement window allocation with carry-over
- Add concurrency (version columns + compare-and-set)
- Verify: can create strategy, attach DNA strands, run combined backtest

### Step 8: Validation Engine (Rule 1)
- Build validation service: re-run signals, compare to live, detect drift
- Implement `trade_validations` table population
- Implement rolling accuracy tracking
- Implement configurable halt threshold
- Verify: closed trade triggers validation, accuracy tracked, halt works

### Step 9: Trading Pipeline
- Port CCXT execution (waterfall routing, idempotency, margin modes)
- Build P&L engine with fee breakdown and margin model awareness
- Build `trade_cost_entries` population
- Build `order_events` population
- Build slippage tracking
- Port trade conviction scorer
- Port market regime service
- Verify: can execute paper trades with full cost tracking

### Step 10: AI Decision Layer
- Build ContextSynthesizer (ONE LLM call per decision)
- Build CEO agent (OpenClaw/Paperclip soul)
- Build NewsIntel collector with LLM classification
- Build PerformanceAuditor with AI post-mortems
- Build outbound notification service (Telegram bot)
- Verify: full pipeline from signal to decision to notification

### Step 11: Paper Trading Validation
- Run complete pipeline on demo accounts
- Measure Rule 1 accuracy over 2-4 weeks
- Tune until accuracy > 80% before going live
- Budget caps active, monitoring running

### Step 12: Go Live
- Transfer $500 to live accounts
- Autonomy Phase B: CEO auto-approves within approved strategies
- Continuous validation, daily reports, weekly optimization

---

## 17. Complete File Reference — What to Port

### Data Collection Services

| File | Lines | Port as | Changes |
|---|---|---|---|
| `C:\JarvAIs\services\candle_collector.py` | ALL | `services/candle_service.py` | Add data_hash, adapter pattern, retention |
| `C:\JarvAIs\data\telegram_collector.py` | ALL | `services/collectors/telegram_collector.py` | Update schema writes |
| `C:\JarvAIs\data\discord_collector.py` | ALL | `services/collectors/discord_collector.py` | Add circuit breaker |
| `C:\JarvAIs\data\rss_collector.py` | ALL | `services/collectors/rss_collector.py` | Update schema writes |
| `C:\JarvAIs\services\idea_monitor.py` | ALL | `services/collectors/tradingview_monitor.py` | Keep vision + re-evaluation |
| `C:\CapitalTwo2.0\python_engine\adaptive_timing.py` | ALL | `services/timing_service.py` | Already Python |

### Indicator Engine

| File | Lines | Port as | Changes |
|---|---|---|---|
| `C:\CapitalTwo2.0\python_engine\indicators.py` | ALL | `engine/indicators/library.py` | Wrap as IndicatorResult |
| `C:\CapitalTwo2.0\python_engine\indicators_comprehensive.py` | 35-2526 | `engine/indicators/catalog.py` | Fix ttm_squeeze_on dup, add JarvAIs SMC |
| `C:\CapitalTwo2.0\python_engine\unified_signal.py` | 242-357 | `engine/indicators/evaluator.py` | Add confidence scoring |
| `C:\JarvAIs\services\data_scientist.py` | 855-918, 1343-1521, 1650-1682 | Merge into catalog | Convert dict -> IndicatorResult |

### Backtest Engine

| File | Lines | Port as | Changes |
|---|---|---|---|
| `C:\CapitalTwo2.0\python_engine\strategy_executor.py` | ALL | `engine/backtest/strategy_executor.py` | Add candle_data_hash, write to backtest_trade_details |
| `C:\CapitalTwo2.0\python_engine\batch_runner.py` | ALL | `engine/backtest/batch_runner.py` | V2 schema, confluence mode |
| `C:\CapitalTwo2.0\python_engine\batch_optimizer.py` | ALL | `engine/backtest/optimizer.py` | Port as-is |
| `C:\CapitalTwo2.0\python_engine\db_pool.py` | ALL | `engine/db/pool.py` | Update table names |

### Trade Lifecycle

| File | Lines | Port as | Changes |
|---|---|---|---|
| `C:\JarvAIs\services\trading_floor.py` | 6-106, 131-146 | `services/trade_lifecycle.py` | Generalize transitions |
| `C:\JarvAIs\services\live_trade_monitor.py` | 903-946, 1079-1083 | `services/position_monitor.py` | Add order_events, slippage |
| `C:\JarvAIs\services\trade_conviction.py` | ALL | `services/risk/conviction_scorer.py` | Keep 5-layer scoring |
| `C:\CapitalTwo2.0\python_engine\liquidation_utils.py` | ALL | `services/risk/liquidation.py` | Clean port |
| `C:\JarvAIs\services\market_regime.py` | ALL | `services/regime_service.py` | Fix duration tracking |

### Operations

| File | Lines | Port as | Changes |
|---|---|---|---|
| `C:\JarvAIs\core\config.py` + `config_loader.py` | ALL | `core/config.py` | Merge, add namespaces |
| `C:\JarvAIs\core\scheduler.py` | ALL | `core/scheduler.py` | Add candle retention + partitioning |
| `C:\JarvAIs\core\memory_manager.py` | ALL | `core/memory.py` | Keep Mem0 integration |
| `C:\JarvAIs\core\rag_search.py` | ALL | `core/rag.py` | Keep hybrid search |
| `C:\JarvAIs\services\proposal_engine.py` | ALL | `services/self_improvement.py` | Keep pattern detection |

### Files to copy to VPS

| File | Purpose |
|---|---|
| `CONTEXT_V3.md` | THIS FILE — complete architecture reference |
| `mem0_bridge.py` | Mem0 Python SDK interface (two projects) |
| `.env` | API keys (MEM0_API_KEY + exchange keys + others) |
| `V2_Reference_Bundle.zip` | 121 reference files from both codebases |

### Reference codebases (NOT copied, used for porting)

| Path | System | Key learnings |
|---|---|---|
| `C:\JarvAIs\` | JarvAIs V1 | AI pipeline, waterfall, multi-exchange, margin mgmt, state machine, retention |
| `C:\CapitalTwo2.0\` | Capital 2.0 | DNA strands, paramHash, conflict resolution, backtesting, validation, fake candles |

---

## Final Summary

- **3 Non-Negotiable Rules**: Backtest parity (99.9%), execution accuracy (track everything), memory efficiency (bounded, indexed, partitioned)
- **24 normalized tables** replacing 80+ legacy tables
- **~250 standardized indicators** with unified IndicatorResult interface
- **Confluence engine** for multi-indicator testing (new capability)
- **Full fee ledger** with per-entry cost tracking
- **Candle data hash** for Rule 1 drift detection
- **Optimistic concurrency** on all mutable state
- **Candle partitioning** + retention policy
- **6 LLM agents + 11 Python services + 3 observer agents**
- **Every file to port identified** with line ranges and required changes
- **13 Gemini fixes verified and incorporated**
- **12-step implementation plan** from schema to go-live

This document is the single source of truth. Any agent reading it cold can build V2.0 from scratch.
