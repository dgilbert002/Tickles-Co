# Context Roadmap — Collectors, Connectors & Agent Readiness

> **Purpose**: Full audit of what's built, what's broken, what's missing, and the step-by-step
> plan to get collectors and connectors operational and product-ready so agents (OpenClaw/Paperclip)
> can command candle collection, run gap detection, backfill data, and execute backtests.
>
> **Date**: 2026-04-14
> **Status**: Post-Step 4 (Data Collection Services), pre-Step 5 (Indicator Engine)
> **Reference Code**: All analysis cross-referenced against legacy implementations in
> `shared/reference/reference/jarvais_v1/` and `shared/reference/reference/capital2/`

---

## Table of Contents

1. [Current State Audit](#1-current-state-audit)
2. [Legacy Reference Analysis](#2-legacy-reference-analysis)
3. [Smart Candle Architecture — Owner Decision](#3-smart-candle-architecture--owner-decision)
4. [Bug Report — Critical Issues](#4-bug-report--critical-issues)
5. [Bug Report — Moderate Issues](#5-bug-report--moderate-issues)
6. [Gap Analysis — What's Missing for Production](#6-gap-analysis--whats-missing-for-production)
7. [Agent Readiness Assessment](#7-agent-readiness-assessment)
8. [Roadmap — Step by Step](#8-roadmap--step-by-step)
9. [Phase Summary](#9-phase-summary)

---

## 1. Current State Audit

### 1.1 Collectors (`shared/collectors/`)

| File | Status | Verdict |
|------|--------|---------|
| [`base.py`](shared/collectors/base.py) | ✅ Solid | BaseCollector ABC, NewsItem dataclass, CollectorConfig, NewsSource enum. `write_to_db()` uses INSERT IGNORE for dedup. `compute_hash()` for content-based dedup. Clean separation — collectors fetch, services analyze. |
| [`media_extractor.py`](shared/collectors/media_extractor.py) | ✅ Solid | Unified media extraction for Discord (URL-based) and Telegram (Telethon-based). File classification, standardized naming, skip-if-exists dedup. |
| [`discord/discord_collector.py`](shared/collectors/discord/discord_collector.py) | ✅ Production-quality | Full Discord self-bot collector. High-water marks in `system_config`, message grouping with time windows, media download, mention resolution, reply context, user filtering (allow/block lists), TradingView chart URL extraction, `run_forever()` loop. ~600 lines, well-structured. |
| [`telegram/telegram_collector.py`](shared/collectors/telegram/telegram_collector.py) | ✅ Production-quality | Full Telethon collector. HWM via `system_config`, message grouping (including album detection via `grouped_id`), media download (photo/video/voice/document), user filtering. Config from JSON, env vars, or CollectorConfig. ~500 lines. |
| [`telegram/tradingview_monitor.py`](shared/collectors/telegram/tradingview_monitor.py) | ⚠️ Functional, basic | Fetches TradingView public idea API. Parses ideas into NewsItems. No HWM (relies on content hash dedup). Limited — TV API may rate-limit or change without notice. Lives in wrong folder (`telegram/` instead of `tradingview/`). |
| [`news/rss_collector.py`](shared/collectors/news/rss_collector.py) | ✅ Solid | 9 default feeds (CNBC, Yahoo, Google News, FXStreet, Investing.com). Parallel async fetching. HTML stripping. Date parsing with fallbacks. Custom feed injection. |
| [`news/run_news_collection.py`](shared/collectors/news/run_news_collection.py) | ⚠️ Functional | Runs RSS + Telegram + TradingView collectors once. No scheduling loop. No Discord (intentionally separate). Missing error recovery for individual collector failures affecting others. |
| [`tradingview/__init__.py`](shared/collectors/tradingview/__init__.py) | ⚠️ Misrouted | Re-exports from `shared.collectors.telegram.tradingview_monitor` — this is a cross-package import alias, not a real implementation. |

### 1.2 Connectors (`shared/connectors/`)

| File | Status | Verdict |
|------|--------|---------|
| [`base.py`](shared/connectors/base.py) | ✅ Solid | `BaseExchangeAdapter` ABC with `fetch_ohlcv()`, `get_market_status()`, `get_instruments()`, `close()`. `Candle` dataclass with `candle_data_hash`. `Instrument` dataclass with raw_data. |
| [`ccxt_adapter.py`](shared/connectors/ccxt_adapter.py) | ✅ Solid | Lazy-init with asyncio lock. Retry with exponential backoff (3 attempts). SHA-256 hash on every candle. Sandbox mode support. Rate limiting via CCXT built-in. Swap market default. Instrument loading with error handling per-market. |
| [`capital_adapter.py`](shared/connectors/capital_adapter.py) | ✅ Solid | Full Capital.com REST adapter with CST/X-SECURITY-TOKEN auth. Email+password+API key auth with retries. Session expiry detection (401 → re-auth). Candle hash computation. Market search across 13 search terms. Proper rate limiting between searches. |

### 1.3 Market Data (`shared/market_data/`)

| File | Status | Verdict |
|------|--------|---------|
| [`candle_service.py`](shared/market_data/candle_service.py) | ⚠️ Has bugs | Instrument ID resolution with cache. Batch insert ON DUPLICATE KEY UPDATE. Backfill with forward-walking loop. **Bug**: `collect_batch()` creates unused `tasks` list. **Bug**: Return type annotation `(int, Optional[datetime])` is not valid. |
| [`gap_detector.py`](shared/market_data/gap_detector.py) | ⚠️ Incomplete | Uses MySQL 8 `LEAD()` window function. Gap thresholds per timeframe. `get_date_range()` for coverage. **Missing**: market hours awareness, backfill trigger, agent-facing report. |
| [`retention.py`](shared/market_data/retention.py) | ⛔ Critical bug | Partition creation/drop logic. **Bug**: `_validate_partition_name()` called but never defined — crashes at runtime. Retention is hardcoded, not config-driven. |
| [`timing_service.py`](shared/market_data/timing_service.py) | ⚠️ Functional | Market type detection. Market hours for crypto/CFD/forex/commodities. Close detection from candle data. Trading window calculation. DST not dynamic yet. |
| [`run_candle_collection.py`](shared/market_data/run_candle_collection.py) | ⚠️ One-shot | Builds adapters from env config. Iterates exchanges × symbols × timeframes. Runs once and exits. No scheduling. |

### 1.4 Utils (`shared/utils/`)

| File | Status | Verdict |
|------|--------|---------|
| `__init__.py` | ⛔ **Missing** | No `__init__.py`. Imports like `from shared.utils import config` may fail. |
| [`config.py`](shared/utils/config.py) | ✅ Solid | Auto-loads `.env`. Type-safe env var parsing. DB config, exchange keys, candle settings. |
| [`db.py`](shared/utils/db.py) | ✅ Solid | Async MySQL pool (aiomysql). Thread-safe singleton. `execute`, `fetch_one`, `fetch_all`, `execute_many`. |

### 1.5 MySQL Schemas

| File | Status | Verdict |
|------|--------|---------|
| [`tickles_shared.sql`](shared/migration/tickles_shared.sql) | ✅ Production-ready | 14 tables, all indexes, partitions through 2026-12, FK constraints, DECIMAL precision per Rule 2, 18 seed rows. |
| [`tickles_company.sql`](shared/migration/tickles_company.sql) | ✅ Production-ready | 10 tables with COMPANY_NAME template. FK constraints. Optimistic concurrency on agent_state. 9 seed rows. |
| [`seed_instruments.py`](shared/migration/seed_instruments.py) | ✅ Functional | Bybit USDT perps + major spot. INSERT with ON DUPLICATE KEY UPDATE. |

### 1.6 "Preference" Folder

**Does not exist.** Gap detection config is hardcoded in [`gap_detector.py`](shared/market_data/gap_detector.py) and [`retention.py`](shared/market_data/retention.py). CONTEXT_V3.md Section 2 Rule 3 says *"User is Asked how long candles should be retained"* — not implemented.

---

## 2. Legacy Reference Analysis

### 2.1 JarvAIs V1 CandleCollector — What We Must Port

**Source**: [`shared/reference/reference/jarvais_v1/services/candle_collector.py`](shared/reference/reference/jarvais_v1/services/candle_collector.py)

The V1 `CandleCollector` is a full **daemon service** (~700 lines) with patterns our V2 version is missing:

| V1 Feature | V2 Status | Priority |
|-----------|-----------|----------|
| **Background thread with `start()`/`stop()`** — runs `_collect_cycle()` at configurable interval | ❌ V2 runs once and exits | 🔴 Critical |
| **Config from `system_config` table** — `_load_config()` reads `candle_retention_days`, `candle_fetch_interval_minutes` etc. | ❌ V2 uses hardcoded values | 🔴 Critical |
| **Multi-source priority** — `_get_candles()` tries CCXT → Yahoo for crypto, MT5 → Yahoo → CCXT for CFD | ⚠️ V2 has adapter pattern but no priority chain | 🟡 Important |
| **Coverage tracking** — `candle_coverage` table with earliest/latest/count per symbol/timeframe | ❌ Not in V2 schema | 🟡 Important |
| **Symbol resolution with caching** — `_resolve_ccxt_symbol()` with DB-backed cache, exchange market validation | ⚠️ V2 has simple `resolve_instrument_id()` but no exchange-side validation | 🟡 Important |
| **Parallel fetching** — `ThreadPoolExecutor(max_workers=8)` for symbol-level parallelism | ❌ V2 does sequential, dead-code tasks list | 🟡 Important |
| **Multi-timeframe collection** — `fetch_multi_timeframe()` for dossier builder | ❌ V2 collects one TF at a time | 🟢 Later |
| **Per-timeframe retention cleanup** — `_cleanup_old_candles()` with per-TF config | ⚠️ V2 `retention.py` exists but crashes | 🔴 Critical |
| **Exchange recon** — `run_exchange_recon()` discovers markets on Bybit/BloFin/Bitget | ❌ V2 has `seed_instruments.py` (manual) | 🟡 Important |
| **`get_status()`** for monitoring — returns running state, last run, config, stats | ❌ V2 has no status API | 🟡 Important |
| **Candle retrieval for backtesting** — `get_candles_for_signal()` reads from DB for backtests | ❌ V2 has no candle loader for backtests | 🔴 Critical |

### 2.2 JarvAIs V1 Scheduler — Patterns to Adopt

**Source**: [`shared/reference/reference/jarvais_v1/core/scheduler.py`](shared/reference/reference/jarvais_v1/core/scheduler.py)

V1's `JarvAIsScheduler` uses asyncio-based job management. Key patterns:

- **`_run_periodic(name, func, interval_hours)`** — wraps any async function in a repeating loop with error handling
- **`_run_daily(name, func, target_hour, target_minute)`** — schedules daily jobs at specific UTC times
- **`trigger_job(job_name)`** — manual trigger API for dashboard/agents
- **`get_job_status()`** — returns running state and exceptions per job
- **Jobs include**: cost aggregation (hourly), score snapshots (4h), nightly review (22:00), memory consolidation (22:30), DB maintenance (03:00), auditor CEO review (23:00)

**V2 needs**: A similar scheduler that manages candle collection intervals, retention cleanup, instrument sync, and gap detection. The V1 pattern is clean and directly portable.

### 2.3 JarvAIs V1 Launcher — Process Management

**Source**: [`shared/reference/reference/jarvais_v1/launcher.py`](shared/reference/reference/jarvais_v1/launcher.py)

V1's `ProcessManager` shows how to orchestrate multiple services:

- **Per-account subprocess spawning** with health monitoring
- **Auto-restart** with max retry count and cooldown
- **Graceful shutdown** on SIGINT/SIGTERM
- **Daily review scheduling** at configurable time

**V2 implication**: OpenClaw/Paperclip replaces the V1 launcher. Agents ARE the process managers. But we still need the health monitoring and auto-restart patterns, now as agent heartbeats and watchdog services.

### 2.4 Capital 2.0 Candle Loader — Backtest Data Pipeline

**Source**: [`shared/reference/reference/capital2/python_engine/mysql_candle_loader.py`](shared/reference/reference/capital2/python_engine/mysql_candle_loader.py)

Capital 2.0's candle loader is what agents need for backtesting:

| Feature | Description | V2 Status |
|---------|-------------|-----------|
| `load_epic_data()` | Loads candles → pandas DataFrame with OHLCV + bid/ask | ❌ Not built |
| `get_date_range()` | Returns (first_date, last_date, count) for an instrument | ⚠️ V2 `gap_detector.get_date_range()` has this |
| `check_epic_data()` | Returns `{has_data, candle_count, is_complete}` | ❌ Not built |
| Bid/ask support | `closeBid`/`closeAsk` columns for accurate spread calc | ✅ V2 schema has `open_bid`/`close_ask` columns |
| `insert_candles()` | Batch insert with ON DUPLICATE KEY UPDATE | ✅ V2 `candle_service.collect_candles()` does this |

### 2.5 Capital 2.0 Backtest Runner — What's Coming Next

**Source**: [`shared/reference/reference/capital2/python_engine/backtest_runner.py`](shared/reference/reference/capital2/python_engine/backtest_runner.py)

The backtest runner needs candle data to exist and be complete before it runs. Key dependencies:

- **`load_epic_data()`** must return a DataFrame for the requested date range
- **Spread calculation** uses `closeBid`/`closeAsk` from candle data (Rule 2: track everything)
- **Timing system** uses `adaptive_timing.py` for market close detection
- **Indicator evaluation** at specific candle indexes via `evaluate_indicator_at_index()`
- **Liquidation utils** for margin-aware position sizing

**This confirms**: Before building Step 5/6 (Indicator/Backtest engines), the candle data pipeline must be complete and agent-accessible.

### 2.6 Capital 2.0 Strategy Executor — Multi-Strand Backtesting

**Source**: [`shared/reference/reference/capital2/python_engine/strategy_executor.py`](shared/reference/reference/capital2/python_engine/strategy_executor.py)

Runs **parallel per-strand backtests** (ThreadPoolExecutor, max 8 workers), then merges window-by-window with carry-over allocation. Uses `batch_runner.py` for parameter optimization (grid search, genetic algo, Bayesian optimization).

**V2 implication**: The backtest queue table (`backtest_queue`) in the schema is ready. The strategy executor needs the candle loader, indicator engine, and gap detection pipeline as prerequisites.

---

## 3. Smart Candle Architecture — Owner Decision

> **Date**: 2026-04-14 — Owner provided strategic direction on candle storage and usage.
> This section captures the architectural decision that changes how candle data flows through the system.

### 3.1 The Decision: Store 1m Only, Roll Up Everything Else

The owner clarified that candle storage exists to serve two purposes:
1. **Real-time brain calculations** (T-60, T-4min decisions) need instant data — no download wait
2. **Backtesting** — agents need OHLCV data to evaluate strategies

The key insight: **only 1-minute candles need to be stored in the database**. All other timeframes (5m, 15m, 1h, 4h, 1d) can be **rolled up on-the-fly from 1m data**.

### 3.2 Why This Works

- A 5m candle is just 5 consecutive 1m candles aggregated: `open = first.open`, `high = max(highs)`, `low = min(lows)`, `close = last.close`, `volume = sum(volumes)`
- A 1h candle = 60 × 1m candles rolled up
- **Wicks are preserved perfectly** — the high/low of any timeframe is the max/min of its constituent 1m candles
- This is MORE accurate than exchange-provided higher-timeframe candles because it reflects exactly what happened minute-by-minute

### 3.3 Storage Strategy

| What | How Long | Why |
|------|----------|-----|
| **1m candles** | ~1 year for actively collected instruments | Sufficient for most backtesting ranges. Agents can always fetch older data on demand. |
| **Rolled-up candles** (5m, 15m, 1h, 4h, 1d) | **Not stored permanently** — cached with TTL | Generated from 1m on request. Cached for hours/days during active backtesting. |
| **On-demand historical data** | Fetched when agents need ranges beyond stored 1m | Agent requests "backtest BTC/USDT 1h over 2 years" → fetcher downloads 1m candles for the range → stored temporarily or permanently depending on age |

### 3.4 Smart Rollup Service

The core new component. A `SmartCandleService` that:

```
Agent asks for 5m candles for BTC/USDT from 2025-06-01 to 2025-12-31
  │
  ├─ Check rollup cache (memory / Redis-style TTL cache)
  │   └─ Cache hit? Return immediately
  │
  ├─ Check 1m candles exist in DB for date range
  │   ├─ Complete? Roll up 1m → 5m on-the-fly
  │   └─ Gaps? Fetch missing 1m from exchange → store → then roll up
  │
  └─ Cache the rolled-up result (TTL: hours during active backtesting)
```

Key properties:
- **One function for any timeframe**: `get_candles(instrument_id, timeframe, start, end)` handles everything
- **Transparent to the caller**: the agent doesn't know or care if data was cached, rolled up, or freshly fetched
- **Wicks preserved**: rollup uses `max(high)` / `min(low)` across constituent 1m candles
- **Fast for repeated access**: cache stays warm during backtest sessions
- **Bounded memory**: cache has max entries + TTL + LRU eviction (Rule 3)

### 3.5 When the Agent "Goes and Gets Data"

For backtesting, the flow is:
1. Agent says "find 20 profitable scenarios over the last 6 months"
2. `SmartCandleService` checks if 1m data exists for the requested range
3. If missing: fetches from exchange (CCXT/Capital.com) → stores 1m candles in DB
4. Rolls up to whatever timeframe the backtest needs (5m, 15m, 1h, etc.)
5. Caches the rollup for the duration of the backtesting session
6. Agent runs backtests at full speed against cached DataFrames

**The owner's concern**: "I don't want to sit and wait for candles to download." Solution:
- **Warm data** (last 1 year, actively collected) is instant — already in DB
- **Cold data** (older than 1 year, or new instruments) requires a one-time fetch
- **Subsequent access** is cached — multiple backtests on the same data don't re-fetch

### 3.6 Retention Policy (Revised)

Previous CONTEXT_V3.md policy stored every timeframe separately:
```
1m: 30 days, 5m: 90 days, 15m: 6 months, 1h: 2 years, 4h+: forever
```

**New policy** (per owner decision):
```
1m candles: ~1 year for actively collected instruments
All higher timeframes: NOT stored in DB — rolled up from 1m on-the-fly
Rollup cache: TTL-based, bounded memory (default 4h TTL during backtesting)
On-demand historical 1m: fetched and stored only when agents request it
```

This dramatically reduces DB size. Instead of storing 5 timeframes × N instruments, we store 1 timeframe × N instruments. The DB schema still supports all timeframes (for edge cases), but the default workflow is 1m + rollup.

### 3.7 The Librarian Concept

The owner mentioned a "librarian" agent that does housekeeping:
- Scans what 1m data exists per instrument
- Identifies stale or unused data (instruments nobody is trading or backtesting)
- Cleans up old data beyond retention
- Reports data inventory to the CEO agent
- Can be a lightweight scheduled job, not a full LLM agent

### 3.8 Impact on Schema

The `candles` table in `tickles_shared.sql` already supports this:
- `timeframe` column allows `'1m'` as the primary stored value
- Higher timeframes can still be inserted for caching if desired
- The `UNIQUE KEY (instrument_id, source, timeframe, timestamp)` handles dedup
- Partitioning by month means old 1m data can be dropped by partition (fast)

**No schema changes needed.** The change is purely in the service layer.

### 3.9 Impact on Roadmap

This simplifies some phases and adds one new component:

| Change | Effect |
|--------|--------|
| Candle daemon only collects 1m | Simpler collection logic, fewer API calls |
| `SmartCandleService` is the new core | Replaces separate per-timeframe collection |
| Retention manager only manages 1m partitions | Much simpler retention logic |
| Candle loader wraps SmartCandleService | `load_candles(instrument_id, '15m', start, end)` just calls rollup internally |
| Gap detection only checks 1m | One timeframe to verify instead of five |
| Cache layer needed | In-memory LRU or lightweight Redis for rollup results |

---

## 4. Bug Report — Critical Issues

### BUG-001: Missing `shared/utils/__init__.py`
- **Location**: `shared/utils/` directory
- **Impact**: `ModuleNotFoundError` on `from shared.utils import config`
- **Fix**: Create empty `shared/utils/__init__.py`

### BUG-002: `RetentionManager._validate_partition_name()` not defined
- **Location**: [`retention.py`](shared/market_data/retention.py) — `ensure_partitions()` and `drop_expired_partitions()`
- **Impact**: `AttributeError` crash at runtime
- **Fix**: Implement regex validation → `re.match(r'^p_\d{4}_\d{2}$', name)`

### BUG-003: CLAUDE.md directory structure out of sync
- **Location**: [`CLAUDE.md`](CLAUDE.md)
- **Details**: Says `shared/market-data/` → actual `shared/market_data/`. Says `shared/news/` → actual `shared/collectors/news/`. Doesn't mention `shared/collectors/` as the top-level package.
- **Fix**: Update CLAUDE.md directory tree

### BUG-004: `CandleService.collect_batch()` dead code
- **Location**: [`candle_service.py`](shared/market_data/candle_service.py)
- **Impact**: Creates `tasks` list never awaited, falls through to sequential loop
- **Fix**: Remove dead code, implement semaphore-bounded concurrency (port V1's `ThreadPoolExecutor(max_workers=8)` pattern as async equivalent)

---

## 5. Bug Report — Moderate Issues

| ID | Issue | Location | Fix |
|----|-------|----------|-----|
| MOD-001 | Return type `(int, Optional[datetime])` invalid | `candle_service.py` | Use `Tuple[int, Optional[datetime]]` |
| MOD-002 | TradingView monitor in `telegram/` folder | `telegram/tradingview_monitor.py` | Move to `tradingview/` |
| MOD-003 | Gap detector ignores market hours | `gap_detector.py` | Integrate `timing_service.py` |
| MOD-004 | Empty `market_data/__init__.py` | `shared/market_data/__init__.py` | Add proper exports |
| MOD-005 | Partition name SQL interpolation | `retention.py` | Guard with `_validate_partition_name()` (BUG-002) |
| MOD-006 | `seed_instruments.py` uses sync pymysql | `seed_instruments.py` | Acceptable for one-off, note in docs |

---

## 5. Gap Analysis — What's Missing for Production

### 5.1 Candle Collection is One-Shot (Not a Service)

**V1 pattern** (from [`candle_collector.py`](shared/reference/reference/jarvais_v1/services/candle_collector.py)):
```
collector.start()  →  background thread  →  _run_loop()  →  _collect_cycle() every N minutes
```

**V2 pattern** (from [`run_candle_collection.py`](shared/market_data/run_candle_collection.py)):
```
python run_candle_collection.py  →  collect once  →  exit
```

V2 needs a persistent daemon collecting **1m candles only**. V1's `CandleCollector` has exactly this with `start()`/`stop()`, health status, and configurable intervals loaded from DB. The daemon should only collect 1m — all higher timeframes are rolled up by SmartCandleService.

### 5.2 No Candle Coverage Tracking

V1 tracks coverage per symbol/timeframe in a `candle_coverage` table: earliest candle, latest candle, count, last fetched, source. V2 has no equivalent — there's no way to know "do we have data for BTC/USDT 1m from 2025-06-01 to 2025-12-31?" without scanning the candles table directly.

### 5.3 No Candle Loader for Backtesting (and No Smart Rollup)

**V1 had**: `CandleCollector.get_candles_for_signal()` — loads from DB for backtests
**Capital 2.0 had**: `mysql_candle_loader.load_epic_data()` — loads into pandas DataFrame with bid/ask

**V2 needs**: `SmartCandleService` — the single entry point for ALL candle data. Stores 1m only, rolls up any timeframe on-the-fly, caches during active backtesting. This is the core innovation that makes agents fast.

### 5.4 Gap Detection Not Wired to Anything

`gap_detector.py` finds gaps but:
- Nobody calls it
- No backfill trigger
- No market-hours filtering
- No agent-facing API
- No pre-backtest completeness check

### 5.5 No Agent API Layer

For OpenClaw/Paperclip to command collectors:
- **Start/stop collection** — currently impossible
- **Check data freshness** — no query interface
- **Run gap detection** — exists but unwired
- **Trigger backfill** — exists but manual
- **Configure retention** — hardcoded
- **Monitor health** — no heartbeat

### 5.6 No Scheduler

V1 has `JarvAIsScheduler` with periodic + daily jobs. V2 has nothing managing:
- Collection intervals per timeframe
- Retention cleanup schedule
- Instrument sync schedule
- Gap detection schedule
- Partition maintenance

### 5.7 No Retention Preference System

CONTEXT_V3.md says *"User is Asked how long candles should be retained"*. Retention is hardcoded in `retention.py`. System config seeds exist (`candles.retention_1m_days = 30`) but `RetentionManager` doesn't read them.

---

## 6. Agent Readiness Assessment

### Can OpenClaw/Paperclip currently operate these systems? **NO.**

| Capability | State | What V1/Cap2 Had | What V2 Needs |
|-----------|-------|-------------------|---------------|
| Start/stop 1m candle collection | ❌ | V1: `collector.start()`/`stop()` | Daemon service with agent control |
| Add collection targets | ❌ | V1: `_get_active_symbols()` from DB | Dynamic config via `system_config` |
| Check 1m data freshness | ❌ | V1: `_get_coverage()` per symbol | Coverage tracking service |
| Get any timeframe instantly | ❌ | V1: `fetch_multi_timeframe()` | **SmartCandleService** — roll up 1m on-the-fly |
| Run gap detection (1m only) | ❌ wired | V2 has SQL, V1 had none | Wire detector to backfill + agent API |
| Trigger backfill (1m only) | ❌ | V1: implicit in `fetch_and_store()` | Agent-callable backfill with progress |
| Load candles for backtest (any TF) | ❌ | Cap2: `load_epic_data()` → DataFrame | **SmartCandleService** → DataFrame + hash |
| Verify completeness before backtest | ❌ | Cap2: `check_epic_data()` | Pre-backtest validation (check 1m, roll up) |
| Configure 1m retention | ❌ | V1: `candle_retention_days` from DB | Config-driven from `system_config` |
| Monitor collection health | ❌ | V1: `get_status()` dict | Status/health for agents |
| Exchange market discovery | ❌ | V1: `run_exchange_recon()` | Instrument auto-sync |
| Spin up for new company | ❌ | V1: per-account launcher | Company-aware collector factory |

### What's Actually Ready

- ✅ **BaseCollector ABC** — clean pattern, any new collector plugs in
- ✅ **BaseExchangeAdapter** — works for crypto (CCXT) and CFD (Capital.com)
- ✅ **Database schema** — fully deployed, 24 tables, aligned with CONTEXT_V3.md
- ✅ **Discord/Telegram/RSS collectors** — production-quality data ingestion
- ✅ **Candle fetch + hash + write** — core data path works
- ✅ **Gap detection SQL** — efficient LEAD() window function
- ✅ **Partition management** — partitions pre-created through 2026-12

---

## 7. Roadmap — Step by Step

### Phase 1: Fix Critical Bugs (Day 1) — ✅ COMPLETE

> Goal: Make existing code runnable without crashes.
> **Status**: All 3 bugs fixed and deployed to VPS by Code Reviewer.

- ✅ `shared/utils/__init__.py` created
- ✅ `RetentionManager._validate_partition_name()` implemented (regex validation)
- ✅ `CandleService.collect_batch()` dead code removed, return type fixed
- ✅ CLAUDE.md directory structure updated

---

### Phase 2: 1m Candle Collection Daemon (Days 2-4)

> Goal: 1m candles flow continuously into the database. This is the data supply.

**Step 2.1** — Build `CandleCollectionDaemon` (1m only)
```
File: shared/market_data/candle_daemon.py
Port from: jarvais_v1/services/candle_collector.py (CandleCollector class)
```

Key features to port from V1:
- **`start()`/`stop()`** lifecycle management (V1 uses threads; V2 uses asyncio tasks)
- **`_run_loop()`** with 60-second interval for 1m candles only
- **`_collect_cycle()`** that fetches 1m candles for all active instruments
- **Config from `system_config` table** — `_load_config()` reads `candle_fetch_interval_minutes` etc.
- **Per-exchange rate limiting** with asyncio semaphores (V1 used ThreadPoolExecutor)
- **Gap detection on startup** — detect and backfill 1m gaps from last collection
- **Heartbeat to `agent_state` table** — agents can verify collection is alive
- **`get_status()`** dict for monitoring (running, last_run, symbols_fetched, candles_stored)

**Step 2.2** — Build `InstrumentSyncService`
```
File: shared/market_data/instrument_sync.py
Port from: jarvais_v1/services/candle_collector.py (run_exchange_recon function)
```

Key features to port from V1:
- Connect to each exchange via adapter, call `get_instruments()`
- Upsert into `tickles_shared.instruments`
- Deactivate removed instruments
- Run on startup + daily schedule
- V1's `run_exchange_recon()` discovered markets across Bybit/BloFin/Bitget — port this logic

**Step 2.3** — Make `RetentionManager` config-driven
```
File: shared/market_data/retention.py (modify existing)
Port from: jarvais_v1/services/candle_collector.py (_cleanup_old_candles)
```

- Read retention days from `system_config` table (`candles.retention_1m_days`, etc.)
- **Default 1m retention: 90 days** (updated from 30 per owner preference)
- Fall back to hardcoded defaults if DB unavailable
- Add `update_retention(timeframe, days)` method for agent/user preference changes
- Port V1's per-timeframe cleanup pattern

---


### Phase 4: Candle Loader + Rollup Service (Days 5-8)

> Goal: Agents can request any timeframe and get a DataFrame instantly.
> This phase is moved UP because agents need DataFrames to start backtesting ASAP.

**Step 4.1** — Build `CandleRollupService`
```
File: shared/market_data/candle_rollup.py
New — the core innovation per owner's candle strategy
```

Rolls up 1m candles into any timeframe on-the-fly:
```python
class CandleRollupService:
    """Roll up 1m candles into any timeframe with memory-bounded caching.

    Usage:
        rollup = CandleRollupService(max_memory_gb=2.0)
        df_1h = await rollup.rollup(df_1m, '1h')  # 1m → 1h
        df_4h = await rollup.rollup(df_1m, '4h')  # 1m → 4h
    """

    async def rollup(self, df_1m: pd.DataFrame, target_tf: str) -> pd.DataFrame:
        """Roll up 1m DataFrame to target timeframe.

        Args:
            df_1m: DataFrame with columns [timestamp, open, high, low, close, volume]
            target_tf: Target timeframe ('5m', '15m', '30m', '1h', '2h', '4h', '1d', '1w')

        Returns:
            DataFrame with same columns, one row per target_tf candle.

        Logic:
            - Group by time bucket (e.g., every 60 rows for 1h)
            - open = first candle's open
            - high = MAX of all highs in bucket (wicks preserved)
            - low = MIN of all lows in bucket (wicks preserved)
            - close = last candle's close
            - volume = SUM of all volumes in bucket
        """

    async def get_cached(self, cache_key: str) -> Optional[pd.DataFrame]:
        """Get a cached rolled-up DataFrame.

        Args:
            cache_key: '{instrument_id}:{timeframe}:{start}:{end}'

        Returns:
            Cached DataFrame or None if not cached/expired.
        """

    async def put_cached(self, cache_key: str, df: pd.DataFrame) -> None:
        """Cache a rolled-up DataFrame.

        Args:
            cache_key: Cache key string
            df: DataFrame to cache

        Memory management:
            - LRU eviction when max entries reached
            - TTL expiration (default 5 minutes)
            - Hard memory ceiling (default 2GB) — evicts oldest when exceeded
        """

    def get_cache_stats(self) -> dict:
        """Return cache statistics for monitoring.

        Returns:
            { entries: int, hit_rate: float, memory_mb: float, evictions: int }
        """
```

**Memory management constraints** (48GB VPS):
- **Hard ceiling**: 2GB for rollup cache (configurable via `ROLLUP_CACHE_MAX_GB` env var)
- **Max entries**: 500 DataFrames (configurable)
- **TTL**: 5 minutes default (configurable)
- **LRU eviction**: oldest/least-used DataFrames evicted first
- **Concurrent backtest limit**: semaphore to limit 3-4 simultaneous backtests (each can hold ~260K 1m candles = ~20MB)
- **Release after backtest**: DataFrames not held indefinitely

**Step 4.2** — Build `CandleLoader` (wraps rollup service)
```
File: shared/market_data/candle_loader.py
Port from: capital2/python_engine/mysql_candle_loader.py (load_epic_data)
```

Key features:
- `load_candles(instrument_id, timeframe, start, end)` → loads 1m from DB, rolls up via `CandleRollupService`
- Column mapping to match backtest runner expectations (V2 standard names)
- Include `open_bid`/`close_ask` for CFD spread calculation (Rule 2)
- Compute `candle_data_hash` as SHA-256 of the loaded dataset (Rule 1)
- Handle fake candle insertion for CFD T-60 pattern
- Validate loaded data (no gaps, no zero prices)
- Return `CandleDataset` dataclass: `{ df: DataFrame, data_hash: str, instrument_id: int, timeframe: str, start: datetime, end: datetime, candle_count: int }`

**Step 4.3** — Build `BacktestDataPrep` service
```
File: shared/services/backtest_data_prep.py
New — combines completeness_service + CandleLoader
```

Called before every backtest:
1. Verify instrument exists in instruments table
2. Call `completeness_service.check_completeness(instrument_id, '1m', start, end)` — only check 1m
3. If gaps found: trigger backfill and wait
4. Re-check after backfill (exchange may not have data for some periods)
5. Load data via `CandleLoader.load_candles(instrument_id, timeframe, start, end)` — rolls up automatically
6. Return `{ ready: bool, dataset: CandleDataset, warnings: [...] }`

---

### Phase 3: Gap Detection + Completeness (Days 9-10)

> Goal: Gap detector works on 1m data only. Wired into the CandleLoader so backtests auto-check completeness.

**Step 3.1** — Add market-hours awareness to `GapDetector`
```
File: shared/market_data/gap_detector.py (modify existing)
```
- Accept optional `TimingService`, filter out non-trading hours
- Crypto markets: no filtering (24/7)
- CFD markets: don't flag weekends/after-hours as gaps
- **Only check 1m timeframe** — higher timeframes are rolled up, so gaps in 1m are the only gaps that matter

**Step 3.2** — Build `CandleCompletenessService`
```
File: shared/market_data/completeness_service.py
Port from: capital2/python_engine/mysql_candle_loader.py (check_epic_data + get_date_range)
```

Agent-callable service that:
1. Checks 1m date range coverage (`get_date_range()` — partially exists in gap_detector)
2. Runs gap detection on 1m data (`find_gaps()`)
3. Returns structured report: `{ complete: bool, coverage_pct: float, gaps: [...], candle_count: int }`
4. Can trigger backfill for discovered gaps via `candle_service.backfill()`
5. Tracks completeness in a coverage cache (port V1's `candle_coverage` pattern)
6. **Key insight**: if 1m data is complete, ALL timeframes are complete (because they're rolled up)

**Step 3.3** — Build collection freshness monitoring
```
File: shared/market_data/collection_monitor.py
Port from: jarvais_v1/services/candle_collector.py (get_status, _get_coverage)
```

- "Last 1m candle for BTC/USDT was 3 minutes ago" → OK
- "Last 1m candle for ETH/USDT was 15 minutes ago" → STALE
- Returns structured status for agent consumption

**Step 3.4** — Add coverage tracking to schema
```
File: shared/migration/add_candle_coverage.sql
Port from: jarvais_v1 candle_coverage table concept
```

Add to `system_config` or create lightweight tracking:
- Earliest/latest 1m candle per instrument
- 1m candle count
- Last fetched timestamp
- Source used

---

### Phase 5: Agent API Layer (Days 11-13)

> Goal: OpenClaw/Paperclip agents can command all collection infrastructure.
> This phase now has real services to expose (CandleLoader, Rollup, Gap Detection).

**Step 5.1** — Build `CollectorRegistry`
```
File: shared/collectors/registry.py
```

Central registry:
- `register(collector)` / `unregister(name)` / `get_status()` → all collector statuses
- Supports dynamic collector creation from `CollectorConfig`
- Manages candle daemon, news collectors, Discord, Telegram independently

**Step 5.2** — Build `CollectorControlService`
```
File: shared/collectors/control_service.py
New — agent-facing control for Discord/Telegram/RSS collectors
```

```python
class CollectorControlService:
    """Agent-facing control for all collectors (Discord, Telegram, RSS, TradingView).

    Usage:
        ctrl = CollectorControlService()
        await ctrl.add_discord_channel('123456', 'Trading Signals', 'alerts')
        await ctrl.trigger_collection('discord')
    """

    async def add_discord_channel(
        self, channel_id: str, server_name: str, channel_name: str,
        download_media: bool = False
    ) -> bool:
        """Hot-add a Discord channel to the collector config and reload without restart.

        Args:
            channel_id: Discord channel snowflake ID
            server_name: Human-readable server name
            channel_name: Human-readable channel name
            download_media: Whether to download images/videos from this channel

        Returns:
            True if channel was added and config reloaded successfully.
        """

    async def remove_discord_channel(self, channel_id: str) -> bool:
        """Disable a Discord channel from collection."""

    async def set_user_subscription(
        self, source: str, username: str, download_media: bool = True
    ) -> bool:
        """Mark a specific user's content for download/priority."""

    async def list_active_collectors(self) -> Dict[str, Any]:
        """Returns status of Discord, Telegram, RSS, TradingView collectors."""

    async def get_collector_stats(self) -> Dict[str, Any]:
        """Returns detailed stats per collector."""

    async def trigger_collection(self, collector_name: str) -> bool:
        """Force an immediate collection cycle instead of waiting for the timer."""
```

**Step 5.3** — Build `AgentDataService`
```
File: shared/services/agent_data_service.py
Combines patterns from: V1 CandleCollector + Scheduler + Coverage + CandleRollupService
```

THE interface agents call. Every method has detailed docstrings for LLM agent consumption:
```python
class AgentDataService:
    # ── Collection Management ──
    async def list_collectors() -> Dict:
        """List all available collectors and their status."""

    async def start_candle_collection(exchange: str, symbols: List[str]) -> str:
        """Start 1m candle collection for symbols on an exchange. Returns job ID."""

    async def stop_candle_collection(exchange: str) -> bool:
        """Stop 1m candle collection for an exchange."""

    async def add_instrument(exchange: str, symbol: str) -> int:
        """Add an instrument to collection targets. Returns instrument_id."""

    # ── Data Access (via CandleRollupService) ──
    async def get_candles(
        self, instrument_id: int, timeframe: str, start: datetime, end: datetime
    ) -> CandleDataset:
        """Get candles for any timeframe. Rolls up from 1m if needed.

        Args:
            instrument_id: Instrument ID from instruments table
            timeframe: '1m', '5m', '15m', '30m', '1h', '2h', '4h', '1d', '1w'
            start: Start datetime (UTC)
            end: End datetime (UTC)

        Returns:
            CandleDataset with DataFrame, data_hash, candle_count

        Example:
            dataset = await agent.get_candles(42, '1h', start, end)
            df = dataset.df  # pandas DataFrame ready for backtesting
        """

    async def check_data_freshness(self, instrument_id: int) -> FreshnessReport:
        """Check when the last 1m candle was collected for an instrument."""

    async def check_data_completeness(
        self, instrument_id: int, start: datetime, end: datetime
    ) -> CompletenessReport:
        """Check if 1m data is complete for a date range."""

    # ── Backfill (1m only) ──
    async def backfill_data(
        self, instrument_id: int, start: datetime, end: datetime
    ) -> str:
        """Trigger backfill of 1m candles for a date range. Returns job ID."""

    async def get_backfill_status(self, job_id: str) -> BackfillProgress:
        """Get progress of a backfill job."""

    # ── Configuration ──
    async def set_retention(self, timeframe: str, days: int) -> bool:
        """Set how many days of candles to keep for a timeframe.

        Writes to system_config with namespace='candle_retention'.
        Default: 1m=90d, 5m=90d, 15m=180d, 1h=730d, 4h+=forever.

        Args:
            timeframe: '1m', '5m', '15m', '1h', '4h', '1d'
            days: Number of days to retain (0 = forever)

        Returns:
            True if retention was updated.
        """

    async def get_collection_dashboard(self) -> DashboardReport:
        """Get full collection status dashboard."""

    async def prepare_backtest_data(
        self, instrument_id: int, timeframe: str, start: datetime, end: datetime
    ) -> BacktestDataResult:
        """Prepare data for a backtest: check completeness, backfill gaps, load candles.

        This is the ONE method an agent calls before running a backtest.
        It handles everything: gap check → backfill → load → rollup → return DataFrame.
        """

    # ── Social Signal Analysis ──
    async def get_recent_signals(
        self, source: str = None, user: str = None, minutes_ago: int = 60
    ) -> List[Dict]:
        """Get recent trading signals from news_items for real-time analysis.

        Args:
            source: Filter by source ('discord', 'telegram', 'rss', 'tradingview'). None = all.
            user: Filter by author/username. None = all.
            minutes_ago: How far back to look (default 60 minutes)

        Returns:
            List of signal dicts with: headline, content, author, source, published_at,
            instruments (extracted symbols), media_path (if chart image was downloaded)

        Example:
            signals = await agent.get_recent_signals(source='discord', user='Dillon', minutes_ago=30)
            # Agent can then load candles for the instrument and run indicators
        """
```

**Step 5.4** — Build `TicklesScheduler`
```
File: shared/services/scheduler.py
Port from: jarvais_v1/core/scheduler.py (JarvAIsScheduler)
```

Port V1's scheduler pattern for V2 jobs:
- 1m candle collection (continuous, from daemon)
- Retention cleanup (daily at 03:00 UTC)
- Instrument sync (daily at 04:00 UTC)
- Partition maintenance (monthly check)
- Gap detection sweep (daily at 05:00 UTC)
- Manual trigger API for agents

### Acceptance Criteria — Real Agent Use Cases

Phase 5 is complete when these 4 scenarios work end-to-end:

**Use Case A — Mass Screening**: Owner says "Scan all coins, find the top 20 by volume + ATR + daily % decrease, and run backtests for the last 6 months across all of them."
- Agent queries instruments table for active crypto
- Fetches live stats via CCXT adapter
- Ranks/filters to top 20
- Runs 20 parallel backtests (each needs ~260K 1m candles = ~20MB)
- **Constraint**: semaphore limits concurrent backtests to 3-4 to avoid OOM on 48GB VPS
- **Constraint**: streaming/chunked loading — load 1 month at a time, evaluate, discard, move to next

**Use Case B — Targeted Strategy Search**: Owner says "Find a strategy for BTC that yields 10x profit at 10x leverage on a $500 account."
- Agent loads BTC 1m data once, caches via CandleRollupService
- Iterates through indicator combinations, reusing cached data
- This is where the rollup cache pays off — one load, many backtests

**Use Case C — Real-time Social Signal Analysis**: Owner says "Dillon from Discord posted a chart with a buy signal 5 minutes ago. What's your analysis?"
- Agent calls `get_recent_signals(source='discord', user='Dillon', minutes_ago=5)`
- Identifies instrument from signal
- Loads recent candles (last 24-48h) via `get_candles()`
- Runs relevant indicators
- Produces agree/disagree opinion
- **Speed requirement**: must return in under 5 seconds

**Use Case D — Agent Curiosity / Self-directed Research**: Agent autonomously explores whether RSI divergence on SOL/USDT 4h has been predictive in the last 3 months.
- Agent checks if data exists → backfills if needed → loads candles → computes indicators → evaluates results
- All through the same AgentDataService interface
- Docstrings must be detailed enough that an LLM agent can understand what methods are available without reading source code

### Documentation Standard for Agent-Facing Services

Every method in `AgentDataService`, `CollectorControlService`, and `CandleRollupService` must have:
- **Clear docstring** explaining what it does, parameters, and return value
- **Type hints** on all parameters and return values
- **Example usage** in the docstring (agents will read these)
- **Error cases** documented (what exceptions can be raised and why)
- Think of these docstrings as the API documentation an LLM agent reads to decide which method to call

---

### Phase 6: Cleanup & Hardening (Days 14-16)

> Goal: Everything is robust, tested, and documented.

**Step 6.1** — File organization cleanup
- Move `tradingview_monitor.py` from `telegram/` to `tradingview/`
- Add `__init__.py` to `shared/utils/`, `shared/services/`
- Update all imports

**Step 6.2** — Add circuit breaker / retry wrapper
```
File: shared/utils/retry.py
```
Reusable decorator: max_attempts, backoff_factor, specific exceptions. Used by all network calls.

**Step 6.3** — Write smoke tests
```
Files:
  - shared/market_data/test_candle_service.py
  - shared/market_data/test_gap_detector.py
  - shared/market_data/test_completeness_service.py
  - shared/market_data/test_candle_loader.py
  - shared/connectors/test_ccxt_adapter.py
  - shared/services/test_agent_data_service.py
```

**Step 6.4** — Update CLAUDE.md comprehensively
- New `shared/services/` directory
- New files created
- Updated directory tree
- Migration status: Step 4 fully complete, ready for Step 5

---

## 9. Phase Summary

| Phase | Days | What It Delivers | Key Reference Files |
|-------|------|-----------------|-------------------|
| **1: Fix Bugs** | Day 1 | ✅ Code runs without crashes (bugs fixed by Code Reviewer) | — |
| **2: 1m Daemon** | Days 2-4 | 1m-only collection daemon, instrument sync, config-driven retention (90d default) | V1 `candle_collector.py`, V1 `scheduler.py` |
| **4: Candle Loader + Rollup** | Days 5-8 | `CandleRollupService` (1m→any TF, memory-bounded cache), `CandleLoader`, `BacktestDataPrep` | Cap2 `mysql_candle_loader.py` |
| **3: Gap + Completeness** | Days 9-10 | Gap detection on 1m only, completeness checks, freshness monitoring, wired into loader | V2 `gap_detector.py` + V1 coverage pattern |
| **5: Agent API** | Days 11-13 | `AgentDataService`, `CollectorControlService`, `TicklesScheduler`, 4 use cases validated | V1 `scheduler.py`, V1 `launcher.py` |
| **6: Cleanup** | Days 14-16 | Tests, retry wrappers, file reorg, docs | — |

### After This Roadmap

With all 6 phases complete, the data infrastructure is production-ready. Next:

- **Step 5: Indicator Engine** — Port 250+ indicators from `capital2/python_engine/indicators_comprehensive.py` + JarvAIs SMC indicators from `jarvais_v1/services/data_scientist.py`
- **Step 6: Backtest Engine** — Port `capital2/python_engine/strategy_executor.py` + `backtest_runner.py`
- **Step 7: Strategy System** — DNA strands, conflict resolution from `capital2/server/orchestration/`
- **Step 8: Validation Engine** — Rule 1 enforcement (backtest-to-live parity)

### The Full Agent Loop (Post-Roadmap)

```
Agent → "Backtest RSI on BTC/USDT 1h for the last 6 months"
  │
  ├─ AgentDataService.prepare_backtest_data()
  │   ├─ CompletenessService.check_completeness(instrument_id, '1m', start, end)
  │   │   ├─ GapDetector.find_gaps() on 1m data only
  │   │   └─ CandleService.backfill() for each 1m gap
  │   └─ CandleLoader.load_candles(instrument_id, '1h', start, end)
  │       ├─ CandleRollupService: load 1m from DB
  │       ├─ Rollup: 1m → 1h (open=first, high=max, low=min, close=last)
  │       ├─ Cache result (TTL 5min, max 2GB, LRU eviction)
  │       └─ Return DataFrame + candle_data_hash
  │
  ├─ IndicatorEngine.evaluate(df, "rsi", params)
  │   └─ Returns IndicatorResult per candle
  │
  ├─ BacktestEngine.run(df, indicator_results, strategy_config)
  │   └─ Returns backtest_results row + backtest_trade_details rows
  │
  └─ Agent receives: { total_return: 42.3%, sharpe: 1.8, win_rate: 63%, ... }
```

### Priority If Time-Limited

1. **Phase 1** (bugs) — ✅ done
2. **Phase 2** (1m daemon) — data must flow continuously
3. **Phase 4** (candle loader + rollup) — agents need DataFrames ASAP
4. **Phase 3** (gap + completeness) — data must be complete
5. **Phase 5** (agent API) — agents need control
6. **Phase 6** (cleanup) — hardening

---

*This document supersedes any prior roadmap. Cross-reference with
[CONTEXT_V3.md](shared/migration/CONTEXT_V3.md) for the full architectural blueprint and
`shared/reference/reference/` for the legacy source code being ported.*
