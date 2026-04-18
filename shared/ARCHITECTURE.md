# The Platform — ARCHITECTURE

> **Naming.** The system we're building here is **"The Platform"** (no product
> name yet). `JarvAIs`, `Capital 2.0`, `Capital Two` are **legacy companies**
> whose code we mine for reference; they are not this system.

> **Purpose.** How the system is designed. Who owns what, who talks to what,
> and what the boundaries are. Code belongs where this doc says it belongs.
> **Audience.** Dean, the CEO agent, Cody, Schemy, Audrey, and any future
> agent or developer.
> **Companion docs.** `ROADMAP_V3.md` (what we're building and when),
> `CORE_FILES.md` (what the Janitor must never touch), `CompanyIdeas.md` (the
> tenants this design serves).

---

## 1. Guiding principles

1. **Feature grouping over file-type grouping.** Everything about trading
   lives in `trading/`. Everything about market data lives in `market_data/`.
   No `models/`, no `services/` catch-all folders.
2. **Deterministic maths in services; qualitative judgement in agents.**
   A service that sizes a position is a pure function. An LLM that decides
   "do we trade this news event?" is an agent.
3. **Backtest ≡ live.** Both run through the same sizer, the same fee model,
   and the same session clock. A paired `trade_validations` row proves it
   per-trade.
4. **Multi-company from day one.** Every service accepts `company_id`. Every
   agent operates inside a `container` tag. Every capability is granted
   explicitly. One wallet ↔ one account ↔ one or more companies (many-to-one
   only via explicit grants).
5. **Memory is bounded and partitioned.** 48 GB VPS budget, hot/warm/cold
   storage, at most 50 DB connections pooled, caches TTL'd.
6. **Archive, never delete.** Every superseded file goes to `_archive/`. The
   Janitor agent never deletes, ever.

---

## 2. Physical topology

```
┌───────────────────────────────────────────────────────────────────┐
│  VPS  (48GB RAM · 16 vCPU · AMD · Ubuntu 22)                      │
│                                                                   │
│   ┌──────────────┐    ┌─────────────┐    ┌──────────────┐         │
│   │ PostgreSQL   │    │ ClickHouse  │    │ Redis        │         │
│   │ (OLTP + vec) │    │ (backtests) │    │ (queue+pub)  │         │
│   └──────────────┘    └─────────────┘    └──────────────┘         │
│           ▲                  ▲                   ▲                │
│           └───────┬──────────┴──────────┬────────┘                │
│                   │                     │                         │
│   ┌───────────────┴──┐   ┌──────────────┴─────┐                   │
│   │ Market Data      │   │ Execution Truth    │                   │
│   │ Gateway          │   │ Layer              │                   │
│   │  (WS hub)        │   │  Treasury · Sizer  │                   │
│   └──────────────────┘   │  OMS               │                   │
│           │               └────────────────────┘                  │
│           │                         │                             │
│   ┌───────┴─────────┐   ┌───────────┴────────┐                    │
│   │ Candle Daemon   │   │ Exchange Adapters  │                    │
│   │ Collectors      │   │ (CCXT · Cap.com ·  │                    │
│   │ Forward-Test    │   │  Alpaca · IBKR)    │                    │
│   └─────────────────┘   └────────────────────┘                    │
│                                                                   │
│   ┌──────────────────────────────────────────┐                    │
│   │ Agents (Janitor · Validator · Optimizer  │                    │
│   │         Curiosity · RegimeWatcher · CEO) │                    │
│   └──────────────────────────────────────────┘                    │
│                                                                   │
│   ┌──────────────┐            ┌─────────────────┐                 │
│   │ MemU         │◀──shared──▶│ Paperclip /     │                 │
│   │ (pg+pgvector)│            │  OpenClaw orch. │                 │
│   └──────────────┘            └─────────────────┘                 │
└───────────────────────────────────────────────────────────────────┘
                ▲
                │  SSH tunnel (local_runner)
                │
┌───────────────┴────────────┐
│  Dean's desktop (Windows)  │
│   · Tray UI                │
│   · Local backtest runner  │
│   · Cursor IDE             │
└────────────────────────────┘
```

Every service lives in its own systemd unit. Nothing cross-calls by Python
import boundaries except through explicit service interfaces (HTTP, Redis,
or DB).

---

## 3. File layout (post-2026-04-18 sync)

This is the **actual layout on the live VPS** (`/opt/tickles/shared/`) plus
the empty feature folders we scaffold for future phases. The local
`_vps_source/` mirror is kept identical to the VPS for every folder below.

```
_vps_source/                       ·  mirrored to /opt/tickles/shared/
├── ROADMAP_V3.md                  ·  current plan
├── ROADMAP_V2.md                  ·  history
├── ARCHITECTURE.md                ·  this file
├── MEMORY.md                      ·  CEO working memory
├── SOUL.md                        ·  agent identity
├── TOOLS.md                       ·  tool registry
├── CORE_FILES.md                  ·  Janitor allowlist
├── CompanyIdeas.md                ·  tenant backlog
├── .env                           ·  secrets (never archived, never committed)
├── .cursor/rules/*.mdc            ·  editor behaviour rules
│
├── utils/                         ·  cross-cutting helpers  [LIVE]
│   ├── __init__.py
│   ├── db.py                      (Postgres + ClickHouse + Redis pools)
│   ├── db.mysql.py                (legacy reference — explicit naming)
│   ├── config.py                  (env + company config, Postgres)
│   ├── config.mysql.py            (legacy reference — explicit naming)
│   ├── mem0_config.py             (MemU / Mem0 client config)
│   ├── mem0_test.py
│   ├── telegram_auth_setup.py
│   ├── time_sessions.py           (Phase 4 — UTC, sessions, DST)
│   └── logging.py                 (Phase 1A addition)
│
├── market_data/                   ·  inbound candles/tickers/books  [LIVE]
│   ├── __init__.py
│   ├── candle_service.py          (DB-backed OHLCV writes/reads)
│   ├── run_candle_collection.py   (systemd entrypoint)
│   ├── gap_detector.py
│   ├── retention.py
│   ├── timing_service.py          (centralised UTC clock — everyone reads here)
│   └── gateway.py                 (Phase 3 — WS hub, Redis fan-out)
│
├── connectors/                    ·  exchange read+write adapters  [LIVE]
│   ├── __init__.py
│   ├── base.py                    (BaseExchangeAdapter, Candle, Instrument)
│   ├── ccxt_adapter.py            (Bybit, BloFin, Bitget via CCXT Pro)
│   ├── capital_adapter.py         (Capital.com REST)
│   ├── alpaca_adapter.py          (Phase 10)
│   └── ibkr_adapter.py            (Phase 10)
│
├── collectors/                    ·  non-exchange inbound data  [LIVE]
│   ├── __init__.py
│   ├── base.py
│   ├── media_extractor.py
│   ├── discord/                   (data dir — media cache)
│   ├── telegram/                  (data dir — media cache)
│   ├── news/                      (data dir)
│   └── tradingview/               (data dir)
│
├── candles/                       ·  candle daemon  [LIVE]
│   ├── __init__.py
│   └── daemon.py                  (1-minute candle collector, systemd)
│
├── trading/                       ·  outbound trade logic  [SCAFFOLD]
│   ├── README.md
│   ├── sizer.py                   (Phase 2 — pure sizing function)
│   ├── treasury.py                (Phase 2 — wallets, capabilities)
│   ├── oms.py                     (Phase 2 — order manager)
│   ├── risk_agent.py              (Phase 2 — LLM hook, OFF by default)
│   └── validation.py              (Phase 6 — Rule 1 pairing)
│
│   Exchange **write** methods live in `connectors/*.py` alongside reads —
│   a CCXT / Capital.com adapter naturally handles both directions, and
│   splitting them would only duplicate boilerplate.
│
├── backtest/                      ·  historical + forward-test  [LIVE]
│   ├── __init__.py
│   ├── engine.py                  (batch + single-bar)
│   ├── worker.py
│   ├── candle_loader.py
│   ├── queue.py
│   ├── runner.py
│   ├── ch_writer.py
│   ├── accessible.py
│   ├── forward_test.py            (Phase 1D)
│   ├── walk_forward.py            (Phase 8)
│   ├── promotion_gate.py          (Phase 8)
│   ├── indicators/
│   │   ├── __init__.py
│   │   ├── core.py
│   │   ├── crash_protect.py
│   │   ├── smart_money.py
│   │   ├── comprehensive.py       (Phase 7, ~250 indicators)
│   │   ├── numba_fast.py          (Phase 7)
│   │   └── registry.py            (Phase 7)
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── single_indicator.py
│   │   └── confluence.py          (Phase 7)
│   └── optimizers/                (Phase 8)
│       ├── random.py
│       ├── grid.py
│       ├── simulated_annealing.py
│       ├── genetic.py
│       └── bayesian.py
│
├── catalog/                       ·  run_id lookup service  [LIVE]
│   ├── __init__.py
│   ├── service.py                 (REST for agents)
│   └── client.py                  (Python client for in-process callers)
│
├── memu/                          ·  shared memory client  [LIVE]
│   ├── __init__.py
│   ├── client.py
│   ├── schema.py                  (Phase 1C — enforced frontmatter)
│   └── synthesizer.py             (Phase 1C — candidates → approved)
│
├── services/                      ·  cross-cutting daemons  [LIVE]
│   ├── __init__.py
│   └── run_all_collectors.py      (orchestrates collectors/*)
│
├── agents/                        ·  standalone long-running agents  [SCAFFOLD]
│   ├── README.md
│   ├── janitor.py                 (Phase 1B)
│   ├── validator.py               (Phase 9)
│   ├── optimizer.py               (Phase 9)
│   ├── curiosity.py               (Phase 9)
│   └── regime_watcher.py          (Phase 9)
│
├── guardrails/                    ·  invariant checks, Rule-1 probes  [LIVE]
│   └── __init__.py
│
├── local_runner/                  ·  Dean's desktop tray + SSH tunnels
│                                    (this folder is LOCAL-ONLY, never synced
│                                    to the VPS).
│
├── migration/                     ·  SQL + seeds + reference docs  [LIVE]
│   ├── README.md
│   ├── clickhouse_schema.sql
│   ├── tickles_shared_pg.sql
│   ├── tickles_company_pg.sql
│   ├── tickles_shared.sql         (legacy mysql reference)
│   ├── tickles_company.sql        (legacy mysql reference)
│   ├── memclaw_update_v3.py       (MemU schema update runner)
│   ├── seed_instruments.py        (seeds the instrument catalogue)
│   ├── seed_indicator_catalog.py
│   ├── smoke_test_pg.py           (post-apply validation)
│   ├── CONTEXT_V3.md              (vision reference — historical)
│   ├── Context_roadmap.md         (historical)
│   ├── ROADMAP_V2.md              (historical)
│   ├── STEP1..4_*.md              (historical planning docs)
│   └── Collector_STATUS_*.md      (historical planning doc)
│
├── scripts/                       ·  re-runnable utilities  [LIVE]
│   ├── e2e_smoke.py
│   ├── seed_reference_data.py
│   └── tickles_cli.py             (Phase 5)
│
├── systemd/                       ·  one unit per running service
│   ├── tickles-catalog.service
│   ├── tickles-candle-daemon.service
│   ├── tickles-bt-workers.service
│   ├── tickles-market-gateway.service     (Phase 3)
│   ├── tickles-oms.service                (Phase 2)
│   ├── tickles-forward-test@.service      (Phase 1D, templated)
│   ├── tickles-validator.service          (Phase 6)
│   ├── tickles-janitor.service            (Phase 1B)
│   └── …
│
└── _archive/                      ·  supersedings only, never edited
    ├── README.md
    └── 2026-04-18_sync-from-vps/
        ├── MANIFEST.md           (successor map for every file here)
        ├── db.mysql.py            (was _vps_source/db.py)
        ├── db_pg.earlier-draft.py (was _vps_source/db_pg.py)
        ├── config.mysql.py        (was _vps_source/config.py)
        ├── …                      (30+ stale root files / one-shots)
```

This layout is **enforced by `.cursor/rules/file-structure.mdc`**. Any deviation
should prompt the agent to pause and ask Dean.

### 3.1 Live vs scaffold

- `[LIVE]` folders are active on the VPS (systemd is invoking code in them).
  Any change lands directly in production once deployed.
- `[SCAFFOLD]` folders exist as empty + README only. Code will land there
  in the phase listed in the README. Do not put code in a scaffold folder
  unless the phase is in progress.

---

## 4. Service responsibilities

### 4.1 Market Data Gateway (Phase 3)

- Owns one CCXT Pro / Capital.com / Alpaca / IBKR WebSocket **per exchange**,
  globally shared.
- Publishes to Redis channels:
  - `candles:{ex}:{sym}:{tf}` — closed candles with checksum
  - `ticker:{ex}:{sym}` — top-of-book
  - `orderbook:{ex}:{sym}` — snapshot + deltas
  - `funding:{ex}:{sym}` — funding rate updates
- Replay: on reconnect, backfills via REST using the gateway's own
  `last_seen` bookmark per stream.
- **Multi-company:** every consumer is a subscriber; the gateway knows
  nothing about companies, which is correct — rate limits are a per-exchange
  concern, not a per-company concern.

### 4.2 Execution Truth Layer — "the Banker" (Phase 2)

Split into three deterministic services plus one optional agent:

| Component | Type | Purpose |
|---|---|---|
| `trading/sizer.py` | pure fn | Compute qty, leverage, SL/TP, expected spread/slippage/fees. Zero I/O. |
| `trading/treasury.py` | service | Own balance snapshots, capability checks, key resolution. |
| `trading/oms.py` | service | The **only** caller of exchange adapters. Writes `trades` + `trade_cost_entries`. |
| `trading/risk_agent.py` | LLM hook | Optional judgement pass after sizer, before treasury. **OFF by default.** |

Data flow:

```
agent ──intent──▶ sizer ──SizedIntent──▶ (risk_agent?) ──▶ treasury
                                                           │
                                                           ▼
                                                         oms ──▶ adapter
                                                           │
                                                           ▼
                                                        trades
                                                   trade_cost_entries
```

The backtest engine's `run_bar()` calls `trading.sizer.size()` directly —
**identical code** for backtest and live. Rule 1 becomes a question of
upstream market-data fidelity, not sizing math.

### 4.3 Time / Sessions service (Phase 4)

- Everything is stored and compared in UTC milliseconds.
- Sessions are registered by name (e.g. `london_open_range`), with
  DST-aware definitions in `session_definitions`.
- Strategies reference sessions by name, never by hour literal.
- Fake 5-minute close candle is **opt-in per strategy**
  (`strategies.uses_fake_close_candle=TRUE`) — only used for strategies that
  fire at session close (Capital.com CFDs, US equity close). Crypto and
  always-on strategies never see a fake candle.

### 4.4 MemU — shared memory layer (Phase 1C)

- Category tree: `strategies/{seeds,variants,approved,archive}`, `regimes/`,
  `backtest-results/`, `positions/{active,closed}`, `review-findings/`,
  `risk-events/`.
- **Frontmatter is enforced at the client.** A write missing any required
  field is rejected.
- `container` tag scopes visibility (per-company or `shared`). Cross-company
  reads require an explicit allowlist.
- **Write discipline:** trading/backtest agents write only to `candidates/`.
  The Synthesizer is the only path to `approved/`, gated on
  `deflated_sharpe > 1.5`, `oos_sharpe > 1.0`, `num_trades > 30`, `verified_by
  is not null`.

### 4.5 Agents layer (Phases 1B, 9)

All agents live in `agents/`. Every agent:

1. Reads `MEMORY.md` first on startup.
2. Uses MemU for all cross-agent communication — **no polling file systems**.
3. Operates inside a `container` tag.
4. Has `approval_mode` (`human_all` by default) — escalates proposals to
   CEO via Telegram card before acting.
5. Never writes strategy outcomes without going through the Synthesizer.

### 4.6 Company provisioning (Phase 5)

A "company" is:

- A per-company Postgres database (`tickles_<name>`).
- An optional per-company MemU container tag.
- An entry in `companies` (shared DB).
- Zero-to-many `account_registry` rows (wallet bindings).
- A capability matrix (`capabilities` table) defining which agents can do
  what on which resources.

Provisioning via one CLI command:
```
tickles create-company --name foxtrot_cfd --asset-class cfd --primary-exchange capitalcom
tickles bind-account --company foxtrot_cfd --exchange capitalcom --account-alias foxtrot_demo
tickles grant --agent cody --capability 'read:candles:*'
tickles grant --agent cody --capability 'write:trade_intent:foxtrot_cfd'
```

No Python file is ever edited to add a company. That is the test of Phase 5.

---

## 5. Rule-1 data flow (continuous forward-test, Phase 1D + 6)

```
new candle closes
   │
   ▼
NOTIFY candle_inserted
   │
   ├──▶ forward_test.py for each active (strategy, account):
   │       │
   │       ▼
   │    engine.run_bar(cfg, bar)
   │       │
   │       ▼
   │    shadow trade → backtest_forward_trades (ClickHouse)
   │
   └──▶ strategy runtime decides to enter
           │
           ▼
         intent → sizer → treasury → oms → live trade → trades row
                                                         │
                                                         ▼
                                                      pairing by
                                                  (strategy_id,
                                                   signal_ts_ms,
                                                   symbol)
                                                         │
                                                         ▼
                                                   validation.py
                                                         │
                                                         ▼
                                               trade_validations row
                                               (drift attribution)
```

If rolling accuracy drops below `strategy.halt_threshold_pct`, validator sets
`strategies.is_active = FALSE` and tells CEO.

---

## 6. Multi-company rules (hardcoded into reviews)

1. **No hardcoded company id anywhere.** Every function that touches trading
   data takes `company_id` as a parameter.
2. **No cross-company DB queries** without going through the `authorize()`
   function in `treasury.py` (which logs the access).
3. **Per-company DB for hot data** (accounts, positions, orders, trades);
   **shared DB for catalogs** (symbols, indicators, strategies registry,
   MemU).
4. **MemU `container` scoping** on every write. Cross-company reads require
   a named allowlist row.
5. **Capability grants are per-agent, per-resource, per-action.** No
   role-based shortcut; grants are explicit.

---

## 7. Rollback philosophy

- Every SQL migration has a rollback stanza at the top of the file.
- Every service deploy is versioned by git hash; systemd rolls back by
  checkout.
- Every file move uses `git mv` so history follows; `git revert` undoes a
  restructure.
- Archived files in `_archive/` are never touched. Restoring a file is
  `git mv _archive/<date>/<file> <original_path>` — trivially reversible.
- Nothing is ever `rm`-ed. Ever.

---

## 8. Things this architecture explicitly does NOT do (yet)

- No Kubernetes, no containers beyond systemd. Single VPS, service
  boundaries, Unix permissions.
- No separate microservice mesh. HTTP + Redis + DB is the whole RPC surface.
- No heavy queue system beyond Redis lists + `NOTIFY`.
- No external vault for secrets (Phase 2 revisits).
- No multi-region replication (Phase 12+ revisits).
- No autonomous capital moves (every grant is human-gated until Validator
  has a proven track record).

If you think we need any of the above, open the question in `MEMORY.md` first
and flag it to Dean before writing code.

---

*End of ARCHITECTURE.md. Living document — update when a service boundary
changes, not when a single function changes.*
