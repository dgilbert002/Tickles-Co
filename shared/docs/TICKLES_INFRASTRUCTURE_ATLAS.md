# Tickles & Co — Complete Infrastructure Atlas

**Version:** 2026.04.28  
**Scope:** Every table, service, MCP tool, daemon, agent, and data flow that exists in production today.  
**Purpose:** Single source of truth for what we built, where it lives, why it exists, and how it connects.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Directory Architecture](#2-directory-architecture)
3. [Database Layer](#3-database-layer)
   - 3.1 PostgreSQL (`tickles_shared`)
   - 3.2 PostgreSQL (`tickles_company`)
   - 3.3 ClickHouse
   - 3.4 Custom Tables (To Be Migrated)
4. [Service Infrastructure](#4-service-infrastructure)
   - 4.1 ServiceDaemon (Generic Supervisor)
   - 4.2 ServiceRegistry (20+ Registered Services)
   - 4.3 Launcher & systemd
5. [MCP Tool Surface](#5-mcp-tool-surface)
   - 5.1 Meta Tools
   - 5.2 Memory Tools
   - 5.3 Learning Tools
   - 5.4 Backtest Tools
   - 5.5 Intelligence Tools
   - 5.6 Trading Tools
   - 5.7 Data Tools
   - 5.8 Provisioning Tools
6. [Intelligence Pipeline](#6-intelligence-pipeline)
   - 6.1 Ingestion (Discord/Telegram/RSS)
   - 6.2 InterpretationService (Dual-Track LLM + Quant)
   - 6.3 PositionMonitor (Live Metrics)
   - 6.4 ChartHackerGuru (Cross-Trader Reports)
   - 6.5 TextSignalExtractor
   - 6.6 TradeDedup
   - 6.7 EpicResolver
7. [Backtest & Validation Engine](#7-backtest--validation-engine)
   - 7.1 BacktestExecutor
   - 7.2 ForwardTestEngine
   - 7.3 ValidationEngine (Rule 1)
   - 7.4 Data Flow
8. [Execution & Risk Layer](#8-execution--risk-layer)
   - 8.1 ExecutionRouter
   - 8.2 Treasury
   - 8.3 CrashProtection
   - 8.4 Guardrails
9. [Memory Architecture](#9-memory-architecture)
   - 9.1 Tier 1: Agent-Private (mem0/Qdrant)
   - 9.2 Tier 2: Company-Shared (mem0/Qdrant + Postgres)
   - 9.3 Tier 3: Cross-Company (MemU)
10. [OpenClaw Agents](#10-openclaw-agents)
    - 10.1 Surgeon (Flat-File Trader)
    - 10.2 Surgeon2 (PostgreSQL Trader)
    - 10.3 ChartHacker (Vision Specialist)
11. [Assessment of Proposed Plan](#11-assessment-of-proposed-plan)
12. [Genuine Gaps](#12-genuine-gaps)
13. [Appendix: File Index](#13-appendix-file-index)

---

## 1. Executive Summary

Tickles & Co is a multi-tenant algorithmic trading platform built on a **service-oriented architecture** with **MCP (Model Context Protocol) tool exposure**, **3-tier memory**, and **deterministic backtesting**. Every component is designed for multi-company operation: shared infrastructure in `tickles_shared`, per-company data in `tickles_{company}`.

**Core principle:** Deterministic services enforce safety. LLMs contribute perception and hypothesis. The backtester proves. Memory compounds learning. Risk gates protect capital.

**What exists today:**
- **23 tables** in `tickles_shared` PostgreSQL
- **12+ tables** per company in `tickles_{company}` PostgreSQL
- **6 tables** in ClickHouse (backtest + forward-test results)
- **20+ registered services** in `ServiceRegistry`
- **40+ MCP tools** across 8 modules
- **3-tier memory** (mem0/Qdrant + MemU)
- **Full backtest→forward-test→validation→autopsy→learning loop**
- **Intelligence pipeline** (Discord → interpretation → tracked positions → monitoring)
- **Paper/live execution** with Treasury sizing and CrashProtection circuit breakers

---

## 2. Directory Architecture

```
/opt/tickles/
├── .env                          # Environment variables (DB, API keys, gateways)
├── .env.template                 # Template for new deployments
├── CLAUDE.md                     # Living architecture document
├── pytest.ini                    # Test configuration
├── requirements.txt              # Python dependencies
│
├── .roo/                         # Roo (Architect) configuration
│   ├── handoffs/                 # Session handoff documents
│   │   ├── 2026-04-25-intelligence-pipeline-handoff.md
│   │   ├── 2026-04-26-intelligence-pipeline-phase3c-handoff.md
│   │   ├── 2026-04-27-intelligence-pipeline-fixes-handoff.md
│   │   ├── 2026-04-28-intelligence-pipeline-fixes-handoff.md
│   │   └── 2026-04-28-intelligence-pipeline-verification-handoff.md
│   ├── bug-reports/
│   │   └── 2026-04-27-intelligence-pipeline.md
│   └── rules/
│       └── global.md             # Global coding standards
│
├── shared/                       # ALL production code lives here
│   ├── __init__.py
│   ├── ARCHITECTURE.md           # High-level system design
│   ├── CORE_FILES.md             # Critical file inventory
│   ├── ROADMAP_V2.md             # Phase 1-20 roadmap (completed)
│   ├── ROADMAP_V3.md             # Phase 21-40 roadmap (in progress)
│   │
│   ├── agents/                   # OpenClaw agent definitions (README only)
│   │   └── README.md             # Describes janitor, validator, optimizer, curiosity, regime_watcher
│   │
│   ├── altdata/                  # Alternative data (funding, OI, social)
│   │   ├── sources/
│   │   │   ├── ccxt_funding.py
│   │   │   ├── ccxt_open_interest.py
│   │   │   ├── manual.py
│   │   │   └── static.py
│   │   └── migrations/
│   │       └── 2026_04_19_phase29_altdata.sql
│   │
│   ├── assets/                   # Asset catalog management
│   ├── auditor/                  # Rule 1 continuous auditor (Phase 21)
│   ├── backtest/                 # Backtest engine + forward test
│   │   ├── engine.py             # BacktestExecutor (588 lines)
│   │   ├── forward_test.py       # ForwardTestEngine (197 lines)
│   │   ├── accessible.py         # ClickHouse read helpers
│   │   └── worker.py             # Redis-backed backtest worker pool
│   │
│   ├── candles/                  # Candle collection + resampling
│   ├── catalog/                  # Instrument catalog service
│   ├── catalogue/                # GUI/TUI catalogue manager
│   ├── copy_trader/              # Copy-trading engine (Phase 33)
│   ├── dashboard/              # Mobile-friendly owner dashboard (Phase 36)
│   ├── daemons/                  # Ad-hoc daemon scripts (NOT in ServiceRegistry)
│   │   ├── surgeon_scanner.py    # Twilly-style market scanner
│   │   ├── surgeon_trader.py     # Flat-file paper trader (surgeon1)
│   │   └── surgeon2_trader.py    # PostgreSQL-backed paper trader (surgeon2)
│   │
│   ├── enrichment/               # News enrichment pipeline
│   ├── events/                   # Events calendar (macro, earnings, halvings)
│   ├── execution/                # Execution layer
│   │   ├── router.py             # ExecutionRouter (routes to adapters)
│   │   ├── paper.py              # PaperExecutionAdapter
│   │   ├── ccxt_adapter.py       # CCXT live adapter
│   │   └── nautilus_adapter.py   # NautilusTrader adapter
│   │
│   ├── features/                 # Feature store (online + offline)
│   ├── gateway/                  # Market data gateway (CCXT Pro WebSocket)
│   │   ├── daemon.py             # WebSocket fan-out into Redis
│   │   ├── gateway.py            # Core gateway logic
│   │   └── redis_bus.py          # Redis pub/sub for candle distribution
│   │
│   ├── intelligence/             # Intelligence pipeline (Phase 3B/3C)
│   │   ├── interpretation_service.py   # Dual-track LLM+quant (1744 lines)
│   │   ├── chart_hacker_guru.py        # Cross-trader reports (928 lines)
│   │   ├── position_monitor.py          # Live position monitoring (682 lines)
│   │   ├── position_quant.py            # PnL/SL/TP/MAE/MFE calculations
│   │   ├── text_signal_extractor.py     # Regex + LLM text extraction
│   │   ├── trade_dedup.py               # Signal deduplication
│   │   ├── epic_resolver.py             # Symbol → Capital.com epic mapping
│   │   ├── gateway_config.py            # LLM gateway configuration
│   │   ├── llm_rate_limiter.py          # Rate limiting for vision calls
│   │   ├── performance_scorer.py        # Trader performance scoring
│   │   ├── prompts/
│   │   │   └── chart_analysis.json      # Vision prompts (chart + text)
│   │   └── migrations/
│   │       ├── 2026_04_26_phase3b_intelligence.sql
│   │       └── 2026_04_26_phase3c_collector_catalog.sql
│   │
│   ├── market_data/              # Candle service + gap detection
│   ├── memu/                     # Tier-3 cross-company memory (MemU)
│   │   └── client.py             # MemU synchronous client
│   │
│   ├── mcp/                      # MCP server + tools
│   │   ├── server.py             # JSON-RPC 2.0 MCP server
│   │   ├── registry.py           # ToolRegistry (tool registration)
│   │   ├── protocol.py           # McpTool dataclass + protocol
│   │   ├── bin/
│   │   │   ├── tickles_mcp_stdio.py    # stdio transport
│   │   │   └── tickles_mcpd.py       # daemon entrypoint
│   │   └── tools/
│   │       ├── __init__.py
│   │       ├── backtest.py       # 8 backtest tools (1110 lines)
│   │       ├── contest.py        # Contest/competition tools
│   │       ├── context.py        # ToolContext (shared state)
│   │       ├── data.py           # Candle/quote/coverage tools
│   │       ├── db_helper.py      # Sync DB query helper
│   │       ├── intelligence.py   # 12 intelligence tools (1359 lines)
│   │       ├── learning.py       # 4 learning tools (241 lines)
│   │       ├── memory.py         # 5 memory tools (589 lines)
│   │       ├── meta.py           # 4 meta tools (600 lines)
│   │       ├── provisioning.py   # Company/agent provisioning
│   │       ├── test_backtest.py  # Backtest tool tests (640 lines)
│   │       ├── test_contest.py
│   │       ├── test_data.py
│   │       ├── test_intelligence.py
│   │       ├── test_meta.py
│   │       ├── test_trading.py
│   │       └── trading.py        # 7 trading tools (1655 lines)
│   │
│   ├── provisioning/             # Company + agent provisioning
│   ├── reports/                  # Generated reports
│   │   └── chart_hacker_guru/    # Hourly status files
│   │
│   ├── services/                 # Service infrastructure
│   │   ├── daemon.py             # ServiceDaemon (228 lines)
│   │   ├── launcher.py           # Generic python -m entrypoint (68 lines)
│   │   ├── registry.py           # ServiceRegistry (444 lines, 20+ services)
│   │   ├── catalog.py            # Service catalog queries
│   │   ├── collector_service.py  # Collector orchestration
│   │   └── run_all_collectors.py # Bulk collector runner
│   │
│   ├── templates/                # OpenClaw agent templates
│   │   └── chart_hacker/
│   │       └── SOUL.template.md  # ChartHacker personality definition
│   │
│   ├── trading/                  # Trading core
│   │   ├── validation.py         # ValidationEngine (857 lines)
│   │   ├── treasury.py           # Treasury (capital allocation)
│   │   ├── capabilities.py       # TradeIntent + capability checks
│   │   └── sizer.py              # Position sizing logic
│   │
│   └── utils/                    # Shared utilities
│       ├── db.py                 # DatabasePool (async PostgreSQL)
│       ├── mem0_config.py        # ScopedMemory (mem0/Qdrant wrapper)
│       └── freshness.py          # Freshness Guard (180s staleness check)
│
├── projects/                     # Per-company configurations
│   └── rubicon/                  # Rubicon trading company
│       └── ...
│
├── local_runner/                 # Local development runners
├── systemd/                      # systemd unit templates
│   └── tickles-service@.service  # Generic service template (%i = name)
│
└── shared/docs/                  # Documentation
    ├── RULE1_END_TO_END.md       # Complete Rule 1 walkthrough (942 lines)
    └── ASSESSMENT_PROPOSED_PLAN.md  # Plan assessment
```

---

## 3. Database Layer

### 3.1 PostgreSQL — `tickles_shared` (23 Tables)

These tables hold **cross-company shared data**.

| Table | Purpose | Key Columns |
|---|---|---|
| `candles` | OHLCV candle data | `instrument_id`, `timeframe`, `timestamp`, `open`, `high`, `low`, `close`, `volume` |
| `instruments` | Trading instrument definitions | `id`, `symbol`, `exchange`, `instrument_type`, `epic_code` (JSONB) |
| `companies` | Company registry | `id`, `name`, `created_at`, `config` (JSONB) |
| `agents` | Agent registry per company | `id`, `company_id`, `agent_id`, `kind`, `config` (JSONB) |
| `agent_state` | Agent runtime state | `agent_id`, `state_data` (JSONB), `updated_at` |
| `agent_decisions` | Every agent decision audited | `id`, `agent_id`, `decision_type`, `inputs`, `outputs`, `timestamp` |
| `agent_prompts` | Versioned prompt templates | `id`, `agent_id`, `prompt_name`, `version`, `content`, `is_active` |
| `strategies` | Backtest strategy definitions | `id`, `name`, `param_hash`, `code`, `sharpe`, `sortino`, `max_dd` |
| `backtest_runs` | Backtest execution metadata | `id`, `strategy_id`, `param_hash`, `status`, `started_at`, `completed_at` |
| `backtest_submissions` | Local-to-VPS backtest queue | `id`, `strategy_id`, `param_hash`, `status`, `submitted_from` |
| `mcp_invocations` | Every MCP tool call audited | `id`, `tool_name`, `caller`, `inputs`, `outputs`, `latency_ms`, `status` |
| `mcp_tool_requests` | New tool requests for CEO review | `id`, `name`, `rationale`, `content_hash`, `status` |
| `news_items` | Raw news from collectors | `id`, `source`, `url`, `title`, `content`, `published_at` |
| `media_items` | Images/videos for interpretation | `id`, `news_item_id`, `url`, `media_type`, `status` (pending/interpreted) |
| `signal_interpretations` | LLM+quant consensus output | `id`, `media_item_id`, `trader_profile_id`, `consensus_direction`, `llm_levels` (JSONB), `quant_levels` (JSONB), `confidence` |
| `trader_profiles` | Scored trader registry | `id`, `handle`, `source`, `classification`, `win_rate`, `total_signals` |
| `trader_performance` | Rolling performance metrics | `id`, `trader_profile_id`, `period`, `win_rate`, `profit_factor`, `sharpe` |
| `tracked_positions` | Positions extracted from signals | `id`, `signal_interpretation_id`, `symbol`, `direction`, `entry_price`, `sl`, `tp`, `status` |
| `position_updates` | Live position metric snapshots | `id`, `tracked_position_id`, `current_price`, `unrealized_pnl`, `distance_to_sl`, `distance_to_tp` |
| `agent_opinions` | ChartHacker counterfactual opinions | `id`, `tracked_position_id`, `agent_id`, `would_take_trade`, `agreed_direction`, `confidence` |
| `paper_wallets` | Paper trading account balances | `id`, `company_id`, `agent_id`, `exchange`, `starting_balance_usd`, `currency` |
| `banker_balances` | Live balance snapshots | `id`, `company_id`, `exchange`, `account_id_external`, `balance`, `equity`, `free_margin` |
| `dashboard_sessions` | Telegram-OTP auth sessions | `id`, `session_hash`, `company_id`, `created_at`, `expires_at` |

**Key Design Decisions:**
- Every table has `id` (bigint unsigned auto_increment) + `created_at` (datetime(3))
- `param_hash` (SHA256) on every backtest row for deterministic dedup
- `state_data` JSONB on `agent_state` — no custom tables for agent-specific state
- `llm_levels` and `quant_levels` stored as JSONB for flexible schema evolution
- Composite unique keys prevent duplicates (e.g., `(company_id, agent_id, exchange)` on `paper_wallets`)

### 3.2 PostgreSQL — `tickles_{company}` (Per-Company, 12+ Tables)

Each company gets its own schema. Example: `tickles_rubicon`.

| Table | Purpose | Key Columns |
|---|---|---|
| `trades` | Live trade ledger | `id`, `symbol`, `direction`, `entry_price`, `exit_price`, `quantity`, `pnl_usd`, `strategy_id`, `param_hash` |
| `trade_cost_entries` | Cost basis per trade | `id`, `trade_id`, `fee_usd`, `slippage_usd`, `funding_usd` |
| `orders` | Order lifecycle | `id`, `client_order_id`, `external_order_id`, `symbol`, `direction`, `order_type`, `status`, `filled_quantity` |
| `positions_current` | Current open positions view | `symbol`, `direction`, `quantity`, `average_entry_price`, `unrealized_pnl_usd` |
| `regime_states` | Market regime classification | `id`, `symbol`, `timeframe`, `regime`, `confidence`, `features` (JSONB) |
| `crash_protection_events` | Circuit breaker triggers | `id`, `rule_name`, `triggered_at`, `severity`, `context` (JSONB) |
| `alt_data_items` | Alternative data points | `id`, `source`, `metric_name`, `value`, `timestamp` |
| `alt_data_latest` | Latest alt data per metric | `source`, `metric_name`, `value`, `timestamp` |
| `events_active` | Currently active events | `id`, `event_type`, `symbol`, `start_time`, `end_time`, `impact_score` |
| `copy_trades` | Mirrored copy trades | `id`, `source_trade_id`, `leader_source`, `mirror_ratio`, `status` |
| `arb_opportunities` | Arbitrage scan results | `id`, `symbol`, `venue_a`, `venue_b`, `spread_bps`, `net_after_fees` |
| `strategy_intents` | Composed strategy signals | `id`, `strategy_name`, `source_ref`, `symbol`, `direction`, `confidence` |
| `tracked_positions` | Unified position ledger (Phase 10) | `id`, `actor_type`, `actor_id`, `actor_instance`, `instrument_symbol`, `direction`, `entry_price`, `position_size`, `status`, `entry_reason_trader`, `entry_reason_llm`, `entry_reason_agent`, `entry_reason_frozen_at` |
| `agent_state` | Agent runtime state (Phase 10) | `id`, `agent_name`, `agent_instance`, `state_jsonb`, `updated_at` |
| `position_postmortems` | Causal LLM post-mortems (Phase 7) | `id`, `position_id`, `causal_summary`, `lessons`, `analysis_jsonb`, `postmortem_version`, `prompt_version` |
| `agent_opinions` | ChartHacker critic opinions (Phase 8) | `id`, `position_id`, `would_take_trade`, `memo_confidence`, `is_published`, `snapshot_price_bucket_pct`, `hour_bucket_utc` |
| `signal_interpretations` | Dual-track LLM+quant analysis (Phase 2+) | `id`, `media_id`, `instrument_symbol`, `direction`, `confidence`, `llm_jsonb`, `quant_jsonb`, `consensus_jsonb`, `prompt_version`, `instrument_resolved_from` |
| `news_items` | Normalised collector output (Phase 9) | `id`, `source_id`, `raw_text`, `enrichment_status`, `context_window`, `zone_filter_confidence`, `zone_filter_reason`, `image_phash`, `duplicate_of_id` |
| `actor_performance` | Platform-agnostic edge score (Phase 11) | `id`, `actor_type`, `actor_id`, `period_start`, `period_end`, `edge_score`, `components_jsonb`, `weights_used_jsonb`, `confidence_low`, `formula_version`, `computed_at` |
| `actor_leaderboard` | VIEW — ranked actors per period (Phase 11) | `actor_type`, `actor_id`, `period_start`, `period_end`, `edge_score`, `closed_position_count`, `confidence_low` |
| `edge_score_changes` | Audit log for score deltas > 0.05 (Phase 11) | `id`, `actor_type`, `actor_id`, `period_start`, `period_end`, `old_score`, `new_score`, `delta`, `note`, `computed_at` |
| `prompt_assignments` | CoachService A/B variant tracking (Phase 11) | `id`, `actor_id`, `day`, `prompt_name`, `variant`, `assigned_at` |
| `table_writers` | Writer-domain registry (Phase 10) | `id`, `table_name`, `allowed_services`, `updated_at` |
| `memu_outbox` | Durable broadcast outbox (Phase 7) | `id`, `payload_jsonb`, `processed_at`, `created_at` |
| `api_cost_log` | Universal LLM cost audit (Phase 0) | `id`, `provider`, `model`, `input_tokens`, `output_tokens`, `cost_usd`, `role`, `correlation_id`, `latency_ms`, `success`, `created_at` |

**Phase 11 Reconciliation Note:**
- `trader_performance` (legacy, pre-Phase 11) is **deprecated** as of 2026-05-03.
- `actor_performance` is the canonical table; it stores scores for **all** actor types (`trader`, `agent`, `copy_bot`, `self`).
- EdgeScorerService dual-writes Discord-actor rows to `trader_performance` for 90-day backward compat.
- After 90 days, `trader_performance` is truncated; the dual-write is removed in Phase 12.
- See [`shared/migration/tickles_company_pg.sql`](shared/migration/tickles_company_pg.sql) for full DDL.

**Key Design Decisions:**
- `trades` links to `tickles_shared.strategies` via `strategy_id`
- `param_hash` on every trade enables backtest→live traceability
- `positions_current` is a VIEW, not a table — computed from `orders`
- All monetary values use `decimal(20,8)` for prices, `decimal(30,8)` for volumes

### 3.3 ClickHouse (6 Tables)

ClickHouse stores **time-series analytical data** — backtest results, forward-test shadow trades, and validation pairs.

| Table                     | Purpose                            | Engine    |
| ---------------------------| ------------------------------------| -----------|
| `backtest_runs`           | Backtest execution metadata        | MergeTree |
| `backtest_trades`         | Individual backtest trades         | MergeTree |
| `backtest_forward_runs`   | Forward-test (shadow) run metadata | MergeTree |
| `backtest_forward_trades` | Shadow trades from forward tests   | MergeTree |
| `trade_validations`       | Rule 1 validation results          | MergeTree |
| `agent_events`            | Agent event stream                 | MergeTree |

**Why ClickHouse:**
- Columnar storage = 100x faster analytical queries than Postgres
- Backtest results are append-only, never updated
- Validation pairs (live vs shadow) are queried by time range
- `agent_events` is the event stream for the auditor

### 3.4 Custom Tables (To Be Migrated)

**`tickles_rubicon.surgeon2_state`** — Balance, realized PnL, cycle counter for surgeon2.  
**`tickles_rubicon.surgeon2_positions`** — Open positions with tp1_done/tp2_done flags.  
**`tickles_rubicon.surgeon2_trade_log`** — Trade history with entry/exit/SL/TP/reason.

**Why they exist:** Surgeon2 was built before the unified schema was finalized. It uses direct psycopg2 connections instead of the `DatabasePool` async layer.  
**Migration target:** `agent_state.state_data` (JSONB) for state, `trades` + `trade_cost_entries` for history, `tracked_positions` + `position_updates` for live positions.

---

## 4. Service Infrastructure

### 4.1 ServiceDaemon — Generic Supervisor

**File:** [`shared/services/daemon.py`](shared/services/daemon.py:100) (228 lines)

Every long-running service in Tickles uses the same supervisor loop. This is not a framework — it is **the** framework.

```python
class ServiceDaemon:
    def __init__(self, config: DaemonConfig, tick: TickFn):
        # config: name, interval_seconds, jitter_seconds, max_backoff_seconds
        # tick: async coroutine that returns a summary dict
```

**Features:**
- **Graceful shutdown** on SIGINT/SIGTERM (signal handlers installed in `run_forever()`)
- **Exponential backoff** on consecutive failures (capped at `max_backoff_seconds`)
- **Jitter** on backoff so a zoo of daemons doesn't retry in lockstep
- **Heartbeat events** written to `AuditStore` every 30 seconds
- **Rolling stats** exposed to the CLI (`total_ticks`, `total_failures`, `last_summary`)
- **Best-effort imports** — if `AuditStore` tables don't exist, logs debug and continues

**Why this design:**
- One supervisor loop = one place to fix bugs, add metrics, handle crashes
- Tests call `run_once()` directly — no signal handler stealing from pytest
- systemd units are identical — only the instance name (`%i`) changes

### 4.2 ServiceRegistry — 20+ Registered Services

**File:** [`shared/services/registry.py`](shared/services/registry.py:66) (444 lines)

The registry is a **pure-Python** dictionary of `ServiceDescriptor` objects. Every service that can run on the VPS is registered here.

**Currently registered services:**

| Name | Kind | Module | Enabled on VPS | Phase |
|---|---|---|---|---|
| `md-gateway` | gateway | `shared.gateway.daemon` | ✅ | 17 |
| `candle-daemon` | collector | `candles.daemon` | ✅ | 13 |
| `catalog` | catalog | `shared.catalog.daemon` | ✅ | 14 |
| `bt-workers` | worker | `shared.backtest.worker` | ✅ | 16 |
| `discord-collector` | collector | `shared.collectors.discord.discord_collector` | ❌ | 3A.1 |
| `news-rss` | collector | `shared.collectors.news.run_news_collection` | ❌ | 3A.1 |
| `telegram-collector` | collector | `shared.collectors.telegram.telegram_collector` | ❌ | 3A.1 |
| `tradingview-monitor` | collector | `shared.collectors.telegram.tradingview_monitor` | ❌ | 3A.1 |
| `auditor` | auditor | `shared.cli.auditor_cli` | ❌ | 21 |
| `banker` | worker | `shared.cli.treasury_cli` | ❌ | 25 |
| `executor` | worker | `shared.cli.execution_cli` | ❌ | 26 |
| `regime` | worker | `shared.cli.regime_cli` | ❌ | 27 |
| `events-calendar` | worker | `shared.cli.events_cli` | ❌ | 30 |
| `altdata-ingestor` | worker | `shared.cli.altdata_cli` | ❌ | 29 |
| `crash-protection` | worker | `shared.cli.guardrails_cli` | ❌ | 28 |
| `souls` | worker | `shared.cli.souls_cli` | ❌ | 31-32 |
| `arb-scanner` | worker | `shared.cli.arb_cli` | ❌ | 33 |
| `copy-trader` | worker | `shared.cli.copy_cli` | ❌ | 33 |
| `strategy-composer` | worker | `shared.cli.strategy_cli` | ❌ | 34 |
| `backtest-submitter` | api | `shared.cli.backtest_cli` | ❌ | 35 |
| `backtest-runner` | worker | `shared.backtest.worker` | ❌ | 35 |
| `dashboard` | api | `shared.cli.dashboard_cli` | ❌ | 36 |
| `mcp-server` | api | `shared.cli.mcp_cli` | ❌ | 37 |

**Key design decisions:**
- `enabled_on_vps` flag — operators can see mismatch between "can run" and "does run"
- `factory` field — some services return a `ServiceDaemon` object for in-process execution
- `systemd_unit` — auto-generated as `tickles-{name}.service`
- New services register in one place — same pattern as `shared.backtest.engines`

### 4.3 Launcher & systemd

**File:** [`shared/services/launcher.py`](shared/services/launcher.py:35) (68 lines)

The launcher is a generic `python -m` entrypoint. Given a service name, it:
1. Looks up the `ServiceDescriptor` in `SERVICE_REGISTRY`
2. If `factory` exists, calls it and runs `run_forever()`
3. If no factory, delegates to `python -m <module>`

**systemd template:** [`systemd/tickles-service@.service`](systemd/tickles-service@.service)

```ini
[Unit]
Description=Tickles Service (%i)

[Service]
Type=simple
ExecStart=/opt/tickles/venv/bin/python -m shared.services.launcher --name %i
Restart=on-failure
```

**Why this design:**
- One systemd unit file for ALL services — only the instance name changes
- `systemctl start tickles-service@candle-daemon` starts the candle daemon
- `systemctl start tickles-service@md-gateway` starts the market data gateway
- No per-service systemd files to maintain

---

## 5. MCP Tool Surface

The MCP server exposes **40+ tools** across 8 modules. Every tool is an `McpTool` dataclass with:
- `name` — dot-namespaced (e.g., `backtest.compose`)
- `description` — human-readable + machine-parseable
- `input_schema` — JSON Schema for validation
- `read_only` — whether the tool mutates state
- `tags` — group, phase, status

### 5.1 Meta Tools (`tools.*`)

**File:** [`shared/mcp/tools/meta.py`](shared/mcp/tools/meta.py:1) (600 lines)

| Tool | Purpose | Read-Only |
|---|---|---|
| `tools.catalogue` | List all registered tools, grouped by tag | ✅ |
| `tools.suggest` | Given a task description, suggest relevant tools (regex matching) | ✅ |
| `tools.request_new` | Request a new MCP tool (stored for CEO review, deduped by content hash) | ❌ |
| `tools.usage_stats` | Read invocation statistics from `mcp_invocations` (call count, error count, latency) | ✅ |

**How `tools.suggest` works:**
- 18 regex rules map phrases like "find divergence" → `["indicator.compute_preview", "md.candles"]`
- Rules are hardcoded in `_RAW_RULES` — no ML, deterministic
- Deduplicated, capped at 12 suggestions
- Skips disabled tools silently

**How `tools.request_new` works:**
- Computes SHA256 of `name + rationale` for dedup
- Stores in `public.mcp_tool_requests` with status `pending`
- CEO reviews via dashboard or direct DB query

### 5.2 Memory Tools (`memory.*`)

**File:** [`shared/mcp/tools/memory.py`](shared/mcp/tools/memory.py:1) (589 lines)

| Tool | Purpose | Tier |
|---|---|---|
| `memory.add` | Write to mem0 (Tier 1/2) | 1/2 |
| `memory.search` | Semantic search over mem0 | 1/2 |
| `memu.broadcast` | Cross-company broadcast (Tier 3) | 3 |
| `memu.search` | Search cross-company insights | 3 |
| `learnings.read_last_3` | Read last 3 learnings for an agent | 1/2 |

**How `memory.add` works:**
1. Resolves scope (`agent`/`company`/`building`) to Qdrant collection + user_id + agent_id
2. Calls `ScopedMemory.add()` with local sentence-transformers embeddings
3. If mem0 fails, returns `forward_to` envelope for external MCP host
4. All mem0 work runs inside `asyncio.to_thread` (non-blocking)

**How `memu.broadcast` works:**
1. Lazy-initializes `MemU` client (Postgres + pgvector + pg_notify)
2. Dedupes on `(category, content_hash)`
3. Broadcasts via `pg_notify` so all connected clients receive the insight
4. Categories: `lesson`, `warning`, `playbook`, `postmortem`

**Fallback chain:**
- If OpenRouter key missing → fallback to legacy envelope
- If Qdrant unreachable → logs warning, returns envelope
- If Postgres unreachable → `forward_to` envelope preserved

### 5.3 Learning Tools (`learning.*`)

**File:** [`shared/mcp/tools/learning.py`](shared/mcp/tools/learning.py:1) (241 lines)

| Tool | Purpose | Template |
|---|---|---|
| `autopsy.run` | Render Twilly Template 01 for a closed trade | Twilly-01 |
| `postmortem.run` | Render Twilly Template 02 for a session | Twilly-02 |
| `feedback.loop` | Render Twilly Template 03 for cycle review | Twilly-03 |
| `feedback.prompts` | Return all three raw template strings | — |

**Twilly Template 01 (Autopsy):**
```
1. What I expected to happen (pre-trade thesis)
2. What actually happened (price path + PnL)
3. Which signals confirmed or contradicted the thesis
4. What I would do differently (1-3 bullets)
5. One learning to commit to mem0 (<=80 words, actionable)
```

**Twilly Template 02 (Postmortem):**
```
1. Scorecard: wins vs losses, realised PnL
2. Two regimes the market traversed
3. Which playbook fired best? Which misfired?
4. One systemic issue (data, latency, emotional, sizing)
5. One commitment for next session (<=60 words)
```

**Twilly Template 03 (Feedback Loop):**
```
1. Read learnings.read_last_3 — call out any pattern
2. Identify 1 playbook drifting from intent
3. Suggest 1 guardrail adjustment
4. Rank curiosities for next cycle (top 3)
```

**Key design decision:** These tools are **read-only prompt renderers**. The caller (an agent) is expected to:
1. Call `autopsy.run` to get the prompt
2. Send the prompt to their LLM
3. Call `memory.add` to store the result

This decouples prompt templates from LLM execution — templates can be versioned independently.

### 5.4 Backtest Tools (`backtest.*`)

**File:** [`shared/mcp/tools/backtest.py`](shared/mcp/tools/backtest.py:1) (1110 lines)

| Tool | Purpose |
|---|---|
| `strategy.list` | List all registered strategies with docstring summaries |
| `strategy.get` | Get full strategy details (parameters, defaults, constraints) |
| `indicator.list` | List technical indicators (trend, momentum, volatility, volume) |
| `indicator.get` | Get single indicator details |
| `indicator.compute_preview` | Run indicator on recent candles, return preview values |
| `engine.list` | List available backtest engines with capabilities |
| `backtest.compose` | Build a validated `BacktestConfig` spec without submitting |
| `backtest.plan_sweep` | Expand parameter ranges into N specs (max 500) |
| `backtest.top_k` | Read top-K backtest results from ClickHouse |

**How `backtest.compose` works:**
1. Validates strategy name exists in registry
2. Validates engine is available
3. Validates direction (`long`/`short`/`both`)
4. Validates capital > 0 and not NaN
5. Builds `BacktestConfig` with deterministic `param_hash()`
6. Returns the spec — caller must submit separately

**How `backtest.plan_sweep` works:**
1. Takes a base spec + parameter ranges (e.g., `rsi_period: [10, 14, 20]`)
2. Computes Cartesian product of all ranges
3. Clamps to max 500 variants
4. Returns list of specs, each with unique `param_hash`

**How `backtest.top_k` works:**
1. Queries ClickHouse `backtest_runs` table
2. Sorts by Sharpe, Sortino, or profit factor
3. Returns top K results with full metrics

### 5.5 Intelligence Tools (`intelligence.*`)

**File:** [`shared/mcp/tools/intelligence.py`](shared/mcp/tools/intelligence.py:1) (1359 lines)

| Tool | Purpose |
|---|---|
| `chart.analyze` | Analyze a chart image with vision LLM (OpenRouter/Gemini) |
| `signal.interpret` | Trigger one cycle of InterpretationService |
| `trader.profile` | Get or create a trader profile |
| `trader.score` | Get latest performance score for a trader |
| `signals.recent` | List recent signal interpretations |
| `signals.pending` | Count media items awaiting interpretation |
| `positions.open` | List open tracked positions with live metrics |
| `positions.history` | List closed position history |
| `traders.leaderboard` | Rank traders by PnL / win rate |
| `guru.report` | Get latest ChartHacker guru report metadata |
| `epic.resolve` | Resolve trading symbol to Capital.com epic code |

**How `chart.analyze` works:**
1. Accepts `imagePath` (local file) or `imageUrl` (remote URL)
2. Converts to base64 if local
3. Calls vision LLM via `GatewayConfig.for_service("vision")`
4. Parses JSON decision block from response
5. Returns structured signal with direction, entry, SL, TP, confidence

**How `signal.interpret` works:**
1. Instantiates `InterpretationService`
2. Calls `run_cycle()` — processes one batch of pending media
3. Returns summary: items processed, interpretations created, errors

### 5.6 Trading Tools (`trading.*`)

**File:** [`shared/mcp/tools/trading.py`](shared/mcp/tools/trading.py:1) (1655 lines)

| Tool | Purpose |
|---|---|
| `banker.snapshot` | Paperclip finance/costs summary |
| `banker.positions` | Current open positions from `positions_current` view |
| `treasury.evaluate` | Evaluate trade intent against capabilities + capital |
| `execution.submit` | Submit order via paper adapter (dryRun=true default) |
| `execution.cancel` | Cancel an open order |
| `execution.status` | Get order status by order ID or client order ID |
| `wallet.paper_create` | Create a paper wallet with starting balance |

**How `treasury.evaluate` works:**
1. Constructs `TradeIntent` from parameters
2. Fetches latest market price from `candles` table
3. Constructs `MarketSnapshot` + `StrategyConfig`
4. Calls `Treasury.evaluate()` — checks:
   - Capability (can this company trade this symbol?)
   - Capital (is balance sufficient?)
   - Risk per trade (max 1% equity)
   - Daily loss cap (max 3% equity)
5. Returns `approved` boolean + `sized` position + `reasons` list

**How `execution.submit` works:**
1. `dryRun=true` (default): simulates fill, returns preview, NO DB writes
2. `dryRun=false` + `I_am_paper=true`: submits through `PaperExecutionAdapter`
3. `dryRun=false` + no `I_am_paper`: REJECTED (live not unlocked)
4. Generates deterministic `client_order_id` via SHA256
5. Returns order details or simulated fill preview

**Safety design:** Live trading requires explicit `I_am_paper=false` + human approval. No accidental live orders.

### 5.7 Data Tools (`data.*`)

**File:** [`shared/mcp/tools/data.py`

| Tool | Purpose |
|---|---|
| `md.candles` | Fetch OHLCV candles for symbol/timeframe/range |
| `md.quote` | Fetch latest bid/ask/mid for a symbol |
| `candles.coverage` | Check data coverage (missing bars, gaps) |
| `candles.backfill` | Request backfill for missing data |

### 5.8 Provisioning Tools (`provisioning.*`)

**File:** [`shared/mcp/tools/provisioning.py`

| Tool | Purpose |
|---|---|
| `company.list` | List all companies |
| `company.create` | Create a new company |
| `agent.list` | List agents for a company |
| `agent.create` | Create a new agent |

---

## 6. Intelligence Pipeline

The intelligence pipeline transforms **raw media** (Discord charts, Telegram messages, RSS news) into **structured trade signals** with consensus from LLM vision + quantitative analysis.

### 6.1 Ingestion Layer

**Collectors** (not yet enabled on VPS):
- `discord-collector` — Scrapes Discord channels for chart images + text
- `telegram-collector` — Monitors Telegram channels for signals
- `news-rss` — RSS feed aggregator
- `tradingview-monitor` — TradingView alert webhook

**Output:** `news_items` + `media_items` tables in `tickles_shared`

**Status:** Collectors are registered in `ServiceRegistry` but `enabled_on_vps=false`. They run locally during development. The Discord collector is the most mature — it auto-discovers channels and maps them to trader profiles.

### 6.2 InterpretationService — Dual-Track Analysis

**File:** [`shared/intelligence/interpretation_service.py`](shared/intelligence/interpretation_service.py:1113) (1744 lines)

The core of the intelligence pipeline. Processes one `media_item` at a time through two parallel tracks:

**Track 1: LLM Vision (`run_llm_track`)**
1. Downloads image from `media_items.url`
2. **Pre-filter** (`run_prefilter`): Gemini 2.5 Flash classifies image type
   - If not a chart (meme, screenshot, unrelated) → skip, mark `status='skipped'`
   - If chart but no clear signal → skip
   - If chart with potential signal → proceed to full analysis
3. **Full analysis**: OpenRouter vision model (Claude/GPT-4o) with structured prompt
4. Extracts: direction, entry_price, stop_loss, take_profit, confidence, pattern
5. Prompt includes **anti-hallucination rules**: "If unclear, emit null. Never guess."

**Track 2: Quantitative Analysis (`run_quant_track`)**
1. Resolves symbol from `media_items.instruments` JSONB
2. Fetches last 500 candles from `tickles_shared.candles`
3. Computes: RSI, EMA, Bollinger Bands, ATR, support/resistance
4. Generates independent signal: direction, entry, SL, TP
5. **None guard**: If symbol resolution fails → skip with error logged

**Consensus (`run_consensus`)**
- If LLM and quant agree on direction → `consensus_method='agreed'`
- If they disagree → `consensus_method='conflict'` (still stored, flagged)
- If either is null/unclear → `consensus_method='llm_only'` or `'quant_only'`

**Output:** `signal_interpretations` row with:
- `llm_levels` (JSONB): entry, sl, tp, confidence, pattern
- `quant_levels` (JSONB): entry, sl, tp, indicators
- `consensus_direction`, `consensus_method`, `confidence`

**Wiring to tracked_positions:**
- `create_tracked_position_from_interpretation()` creates `tracked_positions` row
- Skips `neutral`/`unclear` directions
- Uses `ON CONFLICT` dedup on `(signal_interpretation_id)`
- Sets `status='open'`

**Cost tracking:**
- Every LLM call logs estimated USD cost to `api_cost_log`
- Pre-filter (Gemini Flash) costs ~$0.0001 per image
- Full analysis (Claude/GPT-4o) costs ~$0.01-0.05 per image
- Target: cost per valid position < $0.50

### 6.3 PositionMonitor — Live Metrics

**File:** [`shared/intelligence/position_monitor.py`](shared/intelligence/position_monitor.py:500) (682 lines)

Monitors all `tracked_positions` with `status='open'` and computes live metrics:

**Per-position snapshot:**
- Current price from `candles` table
- Unrealized PnL (absolute + percentage)
- Distance to stop-loss (price + percentage)
- Distance to take-profit (price + percentage)
- Risk-reward ratio
- Maximum adverse excursion (MAE)
- Maximum favorable excursion (MFE)
- Time in position (hours)

**Event detection:**
- SL hit: `current_price <= sl` for longs, `>= sl` for shorts
- TP hit: `current_price >= tp` for longs, `<= tp` for shorts
- Near SL: within configurable % of stop
- Near TP: within configurable % of target

**Output:** `position_updates` table + triggers `agent_opinions` generation

### 6.4 ChartHackerGuru — Cross-Trader Reports

**File:** [`shared/intelligence/chart_hacker_guru.py`](shared/intelligence/chart_hacker_guru.py:583) (928 lines)

A specialist daemon that generates **cross-trader comparison reports** and stores learnings.

**What it does:**
1. Fetches all open `tracked_positions`
2. Groups by trader
3. Computes per-trader stats: win rate, PnL, best/worst symbol, CH agreement rate
4. Generates `CrossTraderReport` with:
   - Leaderboard (top traders by Sharpe)
   - Underperformers (traders to fade)
   - Symbol consensus (where multiple traders agree)
   - Disagreement opportunities (where traders conflict)
5. Stores learnings in Mem0 via `memory.add`

**Current status:** Writes hourly "no data" status files because `tracked_positions` was empty until today (wiring just fixed). Going forward, it will generate actual reports.

**SOUL.md:** [`shared/templates/chart_hacker/SOUL.template.md`](shared/templates/chart_hacker/SOUL.template.md)

Defines ChartHacker's personality: "I see, I score, I move on." Anti-hallucination rules, log format, confidence scoring methodology.

### 6.5 TextSignalExtractor

**File:** [`shared/intelligence/text_signal_extractor.py`](shared/intelligence/text_signal_extractor.py:369) (449 lines)

Extracts trade signals from **text-only** messages (no images).

**Two-track approach:**
1. **Regex first** (`_extract_with_regex`): Pattern matching for "BTC long 65000 SL 64000 TP 70000"
2. **LLM second** (`_extract_with_llm`): Lightweight LLM if regex fails

**Pre-filter** (`_passes_prefilter`):
- Rejects messages < 20 characters
- Rejects messages without price numbers
- Rejects known spam patterns

**Output:** Same structure as vision track — direction, entry, SL, TP, confidence

### 6.6 TradeDedup

**File:** [`shared/intelligence/trade_dedup.py`](shared/intelligence/trade_dedup.py:30) (263 lines)

Prevents duplicate signals from the same trader/symbol/direction within a time window.

**Two functions:**
- `find_duplicate_position()` — checks `tracked_positions` for near-identical open positions
- `find_duplicate_signal()` — checks `signal_interpretations` for recent same-direction signals

**Dedup logic:**
- Same trader + symbol + direction within 24 hours = duplicate
- Price levels within 1% = near-identical
- Uses `ON CONFLICT` at insert time as final guard

### 6.7 EpicResolver

**File:** [`shared/intelligence/epic_resolver.py`](shared/intelligence/epic_resolver.py:30) (229 lines)

Maps trading symbols to Capital.com epic codes for CFD trading.

**Seed map:** 60+ symbol→epic mappings (BTC → CS.D.BTCUSD.CFD.IP, ETH → CS.D.ETHUSD.CFD.IP, etc.)

**Fallback chain:**
1. Exact match in seed map
2. Case-insensitive match
3. Variant generation (BTC → BTCUSD, BTC_USD, XBTUSD)
4. Custom mapping via `add_custom_mapping()`

**Output:** Epic code string or `None` if unresolvable

---

## 7. Backtest & Validation Engine

### 7.1 BacktestExecutor

**File:** [`shared/backtest/engine.py`](shared/backtest/engine.py:230) (588 lines)

A **stateful, deterministic, bar-by-bar** simulation engine.

**Core loop (`run_backtest`):**
```python
for i, row in df.iterrows():
    # 1. Intrabar check (SL/TP) on CURRENT bar
    if executor.position:
        executor.process_intrabar(row)
    
    # 2. Apply PREVIOUS bar's signal at CURRENT bar's open
    if pending_signal:
        executor.process_signal(pending_signal, row)
        pending_signal = None
    
    # 3. Compute NEW signal for NEXT bar (no look-ahead)
    signal = strategy(df.iloc[:i+1], params)
    pending_signal = signal
```

**Key design decisions:**
- **No look-ahead**: Signal at bar `t` → fill at bar `t+1` open. Strategy sees bars `0..i`, not `i+1`.
- **Intrabar SL/TP**: Checked against high/low of current bar (simulating resting orders)
- **Deterministic param_hash**: SHA256 of rounded, sorted parameters. Re-running identical params produces byte-identical output.
- **Engine version**: "2026.04.17.hardened" — every result tagged with engine version for audit

**Metrics computed:**
- Sharpe ratio (annualized)
- Sortino ratio (downside risk only)
- Deflated Sharpe (adjusts for multiple trials)
- Max drawdown
- Profit factor
- Win rate
- Average trade PnL

### 7.2 ForwardTestEngine

**File:** [`shared/backtest/forward_test.py`](shared/backtest/forward_test.py:23) (197 lines)

**Shadow trading** — runs the exact same `BacktestExecutor` on live candles in real-time.

**How it works:**
1. `start_run(strategy_id, cfg)` — creates `BacktestExecutor` with same config as backtest
2. `on_candle(symbol, timeframe, candle)` — called for every new candle:
   - Appends candle to buffer
   - Calls `process_intrabar()` if position open
   - Persists any new trades to ClickHouse `backtest_forward_trades`
   - Computes next signal using strategy on updated buffer
   - Stores signal in `_pending_signal` for **next** candle

**Critical parity mechanism:** `_pending_signal`
- Signal computed at candle `t` is stored, not executed
- At candle `t+1`, the pending signal is applied at the open
- This maintains **perfect parity** with historical backtest

### 7.3 ValidationEngine — Rule 1

**File:** [`shared/trading/validation.py`](shared/trading/validation.py:32) (857 lines)

**Rule 1: Backtest ≡ Live**

The validation engine continuously compares live trades against their shadow counterparts.

**Threshold:** `RULE1_THRESHOLD_PCT = 0.1` (0.1% of notional)

**Process (`validate_trade`):**
1. Fetches live trade from PostgreSQL `trades`
2. `_find_shadow_trade()` — searches ClickHouse `backtest_forward_trades` within 10-second window of live entry time
3. `_compare_trades()` — computes:
   - `entry_delta` (price difference)
   - `exit_delta` (price difference)
   - `pnl_delta` (absolute P&L difference)
   - `pnl_delta_pct` (percentage of notional)
   - `rule1_pass` (boolean: `|pnl_delta_pct| < 0.1%`)
   - `slippage_contribution` (how much of delta is from entry/exit slippage)
   - `fee_contribution` (how much from fee differences)
4. Records in `trade_validations` (PostgreSQL)

**Verdict system (`review_strategy`):**
- **CONTINUE**: ≥90% pass rate AND avg P&L delta < 0.05% → keep trading
- **CAUTION**: ≥70% pass rate → reduce size 50%, investigate
- **STOP**: <70% pass rate → halt strategy, run autopsy

**Autopsy (`autopsy_strategy`):**
- `_detect_drift_patterns()`:
  - `consistent_adverse_entry_slippage` → increase `entry_slippage_bps` by 2-5
  - `fee_drift` → update `fee_bps` to match broker
  - `data_drift` → check data feed integrity
- `_generate_learnings()` → actionable recommendations stored in Mem0

### 7.4 Data Flow

```
Historical Candles → BacktestExecutor → backtest_runs (ClickHouse)
                                              ↓
Live Candles → ForwardTestEngine → backtest_forward_trades (ClickHouse)
                                              ↓
Live Trades (Postgres) → ValidationEngine → trade_validations (Postgres)
                                              ↓
                                    Autopsy → Mem0 (learning)
```

---

## 8. Execution & Risk Layer

### 8.1 ExecutionRouter

**File:** [`shared/execution/router.py`](shared/execution/router.py)

Routes orders to the appropriate adapter based on company configuration.

**Adapters:**
- `paper` — `PaperExecutionAdapter` (deterministic fills, no external calls)
- `ccxt` — `CCXTAdapter` (live exchange trading via CCXT)
- `nautilus` — `NautilusAdapter` (NautilusTrader integration)

**Flow:**
1. `ExecutionRouter.receive_order(intent)` — validates intent
2. Looks up company adapter preference
3. Delegates to adapter
4. Adapter returns `OrderResult` with fill details
5. Router persists to `orders` + `trades` tables

### 8.2 Treasury

**File:** [`shared/trading/treasury.py`](shared/trading/treasury.py)

**Capital allocation and trade approval.**

**Checks every trade intent:**
1. **Capability**: Can this company trade this symbol? (whitelist/blacklist)
2. **Capital**: Is balance sufficient? (reads `banker_balances`)
3. **Risk per trade**: Max 1% equity per trade
4. **Daily loss cap**: Max 3% equity per day
5. **Weekly loss cap**: Max 6% equity per week
6. **Correlation**: Not explicitly implemented (gap)

**Position sizing formula:**
```python
risk_per_trade = account_equity * base_risk_pct * edge_score_multiplier
```

**Output:** `TreasuryDecision` with `approved`, `sized`, `reasons`

### 8.3 CrashProtection

**File:** [`shared/cli/guardrails_cli.py`](shared/cli/guardrails_cli.py) (via `crash-protection` service)

**Circuit breakers and emergency halts.**

**Rules:**
- BTC moves >10% in 1h → halt all new entries
- Portfolio drawdown >5% in 24h → halt all activity, page on-call
- Exchange websocket latency >5s → halt
- Daily loss cap hit → halt for 24h

**Output:** `crash_protection_events` table with `rule_name`, `severity`, `context`

**Key design decision:** These are **hard-coded, non-overridable** by any agent or service. Only human intervention can disable.

### 8.4 Guardrails

**File:** [`shared/cli/guardrails_cli.py`](shared/cli/guardrails_cli.py)

**Pre-trade safety checks.**

- Freshness Guard: Reject signals older than 180 seconds
- Symbol validation: Reject unlisted symbols
- Leverage caps: Max 100x for crypto, 30x for CFDs
- Weekend trading: Reject if market closed
- Maintenance windows: Reject during exchange maintenance

---

## 9. Memory Architecture

### 9.1 Tier 1: Agent-Private (mem0 + Qdrant)

**Scope:** One agent, one company  
**Store:** mem0 with local sentence-transformers embeddings + per-company Qdrant collection (`tickles_{company}`)  
**Lifespan:** Session + persistent  
**Example:** Surgeon1's autopsy memories about BTCUSDT trades

**How it works:**
1. `ScopedMemory(company="rubicon", agent_id="surgeon")` creates a mem0 instance
2. `add()` writes to Qdrant collection `tickles_rubicon` with embedding vector
3. `search()` queries Qdrant with semantic similarity
4. All operations wrapped in `asyncio.to_thread` (non-blocking)

**Fallback:** If mem0 init fails, returns `forward_to` envelope for external MCP host

### 9.2 Tier 2: Company-Shared (mem0 + Qdrant + Postgres)

**Scope:** All agents within one company  
**Store:** Same Qdrant collection, different `agent_id`  
**Lifespan:** Permanent  
**Example:** All rubicon agents share lessons about BTC longs during high funding

**How it works:**
- Same infrastructure as Tier 1, but `agent_id="shared"`
- Postgres tables (`agent_decisions`, `trade_validations`) provide structured episodic memory
- Mem0 provides semantic search over unstructured learnings

### 9.3 Tier 3: Cross-Company (MemU)

**Scope:** All companies  
**Store:** Postgres + pgvector + pg_notify broadcast  
**Lifespan:** Permanent  
**Example:** "BTC longs during high funding perform poorly" shared across all companies

**How it works:**
1. `MemU.write_insight()` inserts into Postgres with pgvector embedding
2. `pg_notify` broadcasts to all connected clients
3. `MemU.search()` queries with semantic similarity + category filter
4. Dedup on `(category, content_hash)`

**Categories:** `lesson`, `warning`, `playbook`, `postmortem`

**Why 3 tiers:**
- Tier 1: Agent experiments without polluting shared memory
- Tier 2: Company-wide lessons (e.g., "our surgeon overtrades ETH")
- Tier 3: Universal truths (e.g., "funding >0.03% = avoid perp longs")

---

## 10. OpenClaw Agents

OpenClaw agents are **ad-hoc research/trading agents** that run outside the service infrastructure. They have their own MEMORY.md, SOUL.md, and tool-calling capabilities.

### 10.1 Surgeon (Flat-File Trader)

**File:** [`shared/daemons/surgeon_trader.py`](shared/daemons/surgeon_trader.py:566) (618 lines)

**Type:** Paper trader using flat files (`.md` trade logs)
**Company:** rubicon
**Strategy:** Twilly-style RSI + EMA + ATR + Bollinger Bands
**State:** `TRADE_STATE.md` + `TRADE_LOG.md` in `/root/.openclaw/workspace/rubicon_surgeon/`

**How it works:**
1. Reads market data from JSON files written by `surgeon_scanner.py`
2. Scores signals: RSI < 30 (oversold) + ATR expanding + price near lower Bollinger
3. Entry: Market order with 0.05% slippage
4. SL: 1.5× ATR
5. TP: 3× ATR (partial close at 50%, move SL to breakeven)
6. Position sizing: 2% risk per trade
7. Autopsy on close: writes learning to Mem0

**Performance:** $10,000 → $11,617.83 (+16.4%), 1 trade, 1 memory

**Why flat files:** Surgeon predates the PostgreSQL schema. It was built as a quick experiment. The `.md` format is human-readable and git-friendly.

### 10.2 Surgeon2 (PostgreSQL Trader)

**File:** [`shared/daemons/surgeon2_trader.py`](shared/daemons/surgeon2_trader.py:566) (620 lines)

**Type:** Paper trader using PostgreSQL
**Company:** rubicon
**Strategy:** Same Twilly-style signals as Surgeon1
**State:** `surgeon2_state`, `surgeon2_positions`, `surgeon2_trade_log` tables

**How it differs from Surgeon1:**
- Uses `DatabasePool` async connections
- Stores state in PostgreSQL instead of flat files
- Has `ScopedMemory` integration (but init failed at startup — 0 memories)
- Uses `derivatives_snapshots` for funding rate checks

**Performance:** $10,000 → $9,932.39 (-0.68%), 3 trades, 0 memories

**Problem:** Custom tables (`surgeon2_*`) need migration to unified schema.

### 10.3 ChartHacker (Vision Specialist)

**File:** [`shared/intelligence/chart_hacker_guru.py`](shared/intelligence/chart_hacker_guru.py:583) (928 lines)

**Type:** Specialist daemon (NOT an OpenClaw agent, but has SOUL.md)
**Purpose:** Generate cross-trader comparison reports
**Input:** `tracked_positions` + `position_updates` + `agent_opinions`
**Output:** Reports in `shared/reports/chart_hacker_guru/` + Mem0 learnings

**SOUL.md:** [`shared/templates/chart_hacker/SOUL.template.md`](shared/templates/chart_hacker/SOUL.template.md)

Defines:
- Identity: "ChartHacker — I see, I score, I move on"
- Core directive: Analyze charts, score signals, never execute trades
- Anti-hallucination rules: "If unclear, emit null. Never guess."
- Confidence scoring: 0.0-1.0 based on pattern clarity + level completeness

**Current status:** Hourly status files show "no data" because `tracked_positions` was empty until wiring was fixed today.

---

## 11. Assessment of Proposed Plan

A separate AI proposed a "Unified Plan" for a self-evolving multi-agent trading intelligence system. Here is how that plan maps to what we actually have.

### What The Plan Proposed vs. What Exists

| Plan Proposal | What We Have | Verdict |
|---|---|---|
| Generic daemon supervisor with backoff, jitter, heartbeats | `ServiceDaemon` (228 lines) | ✅ EXISTS — plan doesn't acknowledge |
| Service registry for systemd | `ServiceRegistry` (20+ services) | ✅ EXISTS — plan doesn't acknowledge |
| MCP tool surface (30+ tools) | 40+ tools across 8 modules | ✅ EXISTS — plan doesn't acknowledge |
| 3-tier memory | mem0/Qdrant (Tier 1/2) + MemU (Tier 3) | ✅ EXISTS — plan doesn't acknowledge |
| Backtest engine | `BacktestExecutor` (588 lines) | ✅ EXISTS — plan doesn't acknowledge |
| Forward test | `ForwardTestEngine` (197 lines) | ✅ EXISTS — plan doesn't acknowledge |
| Rule 1 validation | `ValidationEngine` (857 lines) | ✅ EXISTS — plan doesn't acknowledge |
| Intelligence pipeline | `InterpretationService` + `PositionMonitor` + `ChartHackerGuru` | ✅ EXISTS — plan doesn't acknowledge |
| Execution router | `ExecutionRouter` + `PaperExecutionAdapter` | ✅ EXISTS — plan doesn't acknowledge |
| Risk system | `Treasury` + `CrashProtection` + `Guardrails` | ✅ EXISTS — plan doesn't acknowledge |
| Learning loops | Twilly Templates 01/02/03 as MCP tools | ✅ EXISTS — plan doesn't acknowledge |
| `UnifiedPositionWriter` | `InterpretationService` + `PositionMonitor` already write to `tracked_positions` | ❌ REDUNDANT — enforce policy instead |
| `ExecutionAgent` | `ExecutionRouter` already exists | ❌ REDUNDANT — rename/document instead |
| `GuruKernel` | ChartHackerGuru is already a daemon | ❌ REDUNDANT — no abstraction needed yet |
| `RiskOfficer` | `Treasury` + `CrashProtection` + `Guardrails` exist | ❌ REDUNDANT — unify instead of rebuild |
| `MetaAgent` | `souls` service has 7 sub-agents | ❌ REDUNDANT — extend existing |
| `edge_score` formula | No unified scoring exists | ✅ GENUINE GAP |
| `CoachService` | No versioned prompt management | ✅ GENUINE GAP |
| `MemoryLibrarian` | No automated dedup/promotion/pruning | ✅ GENUINE GAP |
| Correlation-aware sizing | No portfolio VaR | ✅ GENUINE GAP |

### What The Plan Misses About Our Architecture

1. **OpenClaw agents run OUTSIDE the service registry.** Surgeon1, Surgeon2, and ChartHacker are ad-hoc research agents with their own MEMORY.md/SOUL.md. They are NOT systemd services. The plan assumes all agents are services.

2. **Phase 13-37 roadmap already exists.** The plan proposes a new "Phase 0-5" roadmap that would conflict with 18 months of existing work.

3. **MCP server (Phase 37) already exists.** The plan proposes building an MCP surface that has been operational for months.

4. **Dashboard (Phase 36) already exists.** Mobile-friendly HTML SPA with Telegram OTP auth.

5. **Souls service (Phase 31-32) already defines 7 sub-agents:** Apex, Quant, Ledger, Scout, Curiosity, Optimiser, RegimeWatcher. The plan proposes a `MetaAgent` that duplicates this.

### Correct Approach

Instead of rebuilding, we should:
1. **Map plan gaps to existing phases** — `edge_score` → Phase 34, `CoachService` → Phase 36, `MemoryLibrarian` → Phase 37
2. **Migrate surgeon2 custom tables** → 1 day
3. **Build genuine gaps** — edge_score (3 days), CoachService (5 days), MemoryLibrarian (3 days), correlation sizing (4 days)
4. **Keep OpenClaw for experimentation** — successful experiments graduate to services or MCP tools
5. **Enforce single-writer policy** — code review rule, not new component

---

## 12. Genuine Gaps

These are the real gaps that need work:

### 12.1 Schema Unification (1-2 days)
- Migrate `surgeon2_state`, `surgeon2_positions`, `surgeon2_trade_log` to unified tables
- Update `surgeon2_trader.py` to use `agent_state.state_data` + `trades` + `tracked_positions`

### 12.2 edge_score Formula (3 days)
- Define composite scoring: signal_confidence + trader_score + backtest_expectancy + forward_test_health + regime_fit + risk_reward
- Wire into `Treasury.evaluate()` for position sizing multipliers
- Version the formula (v1, v2) for A/B testing

### 12.3 CoachService (5 days)
- Versioned prompt management: `agent_prompts` table already exists, needs UI
- A/B testing: run old + new prompts in parallel on historical data
- One-call rollback: `coach.rollback` MCP tool
- Proposal queue: auto-generate prompt edits from Mem0 patterns

### 12.4 MemoryLibrarian (3 days)
- Daily daemon that:
  - Deduplicates near-identical memories (cosine similarity > 0.95)
  - Promotes episodic → semantic when ≥3 similar events
  - Prunes memories contradicted by newer evidence
  - Demotes semantic memories whose win rate decayed below threshold

### 12.5 Correlation-Aware Position Sizing (4 days)
- Rolling correlation matrix across open positions
- Portfolio VaR calculation
- BTC + ETH longs = ~90% correlated = one bet, not two
- Cap aggregate directional exposure per asset cluster

### 12.6 Validation MCP Tools (1-2 days)
- Expose `validation.verdict` and `validation.autopsy` as callable MCP tools
- Currently only daemon-triggered; OpenClaw agents can't call on demand

### 12.7 Strategy Genome Crossover (deferred)
- Genetic algorithm: combine DNA strands from top-performing strategies
- Backtest hybrids, promote winners
- Low priority — manual strategy creation works for now

---

## 13. Appendix: File Index

### Critical Files (Touch These = System Impact)

| File | Lines | Purpose |
|---|---|---|
| `shared/backtest/engine.py` | 588 | BacktestExecutor — deterministic bar-by-bar simulation |
| `shared/backtest/forward_test.py` | 197 | ForwardTestEngine — real-time shadow trading |
| `shared/trading/validation.py` | 857 | ValidationEngine — Rule 1 enforcement |
| `shared/intelligence/interpretation_service.py` | 1744 | Dual-track LLM+quant signal interpretation |
| `shared/intelligence/chart_hacker_guru.py` | 928 | Cross-trader reports + learnings |
| `shared/intelligence/position_monitor.py` | 682 | Live position monitoring + snapshots |
| `shared/mcp/tools/backtest.py` | 1110 | 8 backtest MCP tools |
| `shared/mcp/tools/intelligence.py` | 1359 | 12 intelligence MCP tools |
| `shared/mcp/tools/trading.py` | 1655 | 7 trading MCP tools |
| `shared/mcp/tools/memory.py` | 589 | 5 memory MCP tools |
| `shared/mcp/tools/meta.py` | 600 | 4 meta MCP tools |
| `shared/mcp/tools/learning.py` | 241 | 4 learning MCP tools |
| `shared/services/daemon.py` | 228 | ServiceDaemon — generic supervisor |
| `shared/services/registry.py` | 444 | ServiceRegistry — 20+ services |
| `shared/services/launcher.py` | 68 | Generic python -m entrypoint |
| `shared/execution/router.py` | — | ExecutionRouter — order routing |
| `shared/trading/treasury.py` | — | Treasury — capital allocation |
| `shared/utils/mem0_config.py` | 235 | ScopedMemory — mem0/Qdrant wrapper |
| `shared/utils/freshness.py` | — | Freshness Guard — 180s staleness check |
| `shared/docs/RULE1_END_TO_END.md` | 942 | Complete Rule 1 walkthrough |

### Configuration Files

| File | Purpose |
|---|---|
| `.env` | Database URLs, API keys, gateway endpoints |
| `pytest.ini` | Test configuration |
| `requirements.txt` | Python dependencies |
| `systemd/tickles-service@.service` | Generic systemd template |

### Migration Files

| File                                                                      | Phase | Tables Created                                                    |
| ---------------------------------------------------------------------------| -------| -------------------------------------------------------------------|
| `shared/intelligence/migrations/2026_04_26_phase3b_intelligence.sql`      | 3B    | `signal_interpretations`, `trader_profiles`, `trader_performance` |
| `shared/intelligence/migrations/2026_04_26_phase3c_collector_catalog.sql` | 3C    | `tracked_positions`, `position_updates`, `agent_opinions`         |
| `shared/altdata/migrations/2026_04_19_phase29_altdata.sql`                | 29    | `alt_data_items`, `alt_data_latest`                               |
| `shared/events/migrations/2026_04_19_phase30_events.sql`                  | 30    | `events_active`                                                   |
| `shared/execution/migrations/2026_04_19_phase26_execution.sql`            | 26    | `orders`, `trades`, `trade_cost_entries`                          |
| `shared/dashboard/migrations/2026_04_19_phase36_dashboard.sql`            | 36    | `dashboard_sessions`                                              |
| `shared/mcp/migrations/2026_04_19_phase37_mcp.sql`                        | 37    | `mcp_invocations`, `mcp_tool_requests`                            |
| `shared/mcp/migrations/2026_04_21_phase_m6_tool_requests.sql`             | M6    | Tool request schema extensions                                    |
| `shared/services/migrations/2026_04_19_phase24_services_catalog.sql`      | 24    | Service catalog tables                                            |

---

**End of Atlas**
