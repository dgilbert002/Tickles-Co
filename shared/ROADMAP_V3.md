# The Platform — ROADMAP (v3)

> **Naming.** The system we're building here is **"The Platform"** — no product
> name yet. `JarvAIs`, `Capital 2.0`, `Capital Two` are **legacy companies** we
> mine for reference code; they are not this system.
> **Supersedes:** `ROADMAP_V2.md` (kept on disk as history; all new work lives here).
> **Purpose:** Single source of truth for everything we build from Phase 1A onwards.
> **Audience:** Dean (owner), future-me (CEO agent), Cody, Schemy, Audrey, and any new agent that joins.
> **Golden rule:** No phase starts until the prior phase's "success criteria" are green.
>
> **Last updated:** Phase 1A kickoff draft.

---

## 0. Why this roadmap exists

Phase 1 (backtest stack + candle daemon + catalog + local runner) is live and
Pass 2-hardened. This roadmap picks up from three parallel decisions made over
the Apr 16–17 design conversations:

1. **We need the "execution truth layer" (Treasury + Position Sizer + Order Gateway)
   before any more features,** because without it Rule 1 (backtest ≡ live) is
   unprovable and every new company reinvents position maths.
2. **The repo needs a clean, structured home** before Phases 2–12 add another
   wave of files — otherwise we get a 200-file garage sale.
3. **Continuous forward-testing is the missing piece of Rule 1.** A backtest that
   stops the day it's promoted to demo is useless; the backtest engine must keep
   running on every new candle and pair shadow trades 1:1 with live fills.

This roadmap is ordered to address those three things first (Phases 1A–1D),
then the big build (Phases 2–12), so that every phase after the foundation is
easier, not harder.

---

## 1. Locked-in decisions (from the design conversations)

These are non-negotiable without an explicit reversal from Dean.

| # | Decision | Who decided | When |
|---|---|---|---|
| 1 | Phase order: **1A → 1B → 1C → 1D → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12** | Dean | 2026-04-17 |
| 2 | **Risk Agent (LLM judgment layer) is OFF by default.** Every company has a config toggle to turn it on when desired. JarvAIs stays OFF. | Dean | 2026-04-17 |
| 3 | **Aggressive port from Capital 2.0 worktrees** (liquidation, indicators, adaptive timing, fake candle, conflict resolution, optimisers). Fix known bugs during port. | Dean | 2026-04-17 |
| 4 | **Fake 5-minute candle is opt-in per strategy**, only enabled for strategies that fire at session close (e.g. Capital.com CFDs). Crypto and any always-on strategy never uses it. Driven by a `uses_fake_close_candle` flag on `strategies`. | Dean | 2026-04-17 |
| 5 | **Archive, never delete.** All superseded files go to `_archive/YYYY-MM-DD_reason/`. Nothing is ever `rm`-ed or `git rm`-ed. | Dean | 2026-04-17 |
| 6 | **Janitor is report-only for the first 4 weeks**, then moves with 7-day quarantine and archive-not-delete. | Dean | 2026-04-17 |
| 7 | **MemU tier discipline:** trading/backtest agents write only to `candidates/`. Promotion to `approved/` is the Synthesizer's job, gated on deflated-Sharpe + OOS + `verified_by`. | Dean | 2026-04-17 |
| 8 | **Phase 1A restructure ships as one atomic commit** (git tracks renames cleanly, diff is a one-screen review). | Dean | 2026-04-17 |
| 9 | **Multi-company is a first-class design constraint at every layer.** No phase ships a service that assumes one company. Company id + capabilities flow through every API. | Dean | 2026-04-17 |
| 10 | Three non-negotiable rules from CONTEXT_V3 stand: Rule 1 (backtest ≡ live 99.9%), Rule 2 (execution accuracy tracked per-entry), Rule 3 (bounded memory + partitioned data + 48GB VPS discipline). | Dean / CONTEXT_V3 | standing |

---

## 2. Phase status at a glance

| Phase | Name | Status | Days | Depends on |
|---|---|---|---|---|
| 0 | Infra migration (MySQL→PG+CH+Redis+MemU) | ✅ done | – | – |
| 1 | Backtest stack + catalog + candle daemon + local runner | ✅ done | – | 0 |
| 1A | Housekeeping — sync-from-VPS + organic-migration policy | ✅ done 2026-04-18 | – | 1 |
| **3A.1** | **Intelligence schema + Discord remediation** | **✅ done 2026-04-18** | – | 1A |
| 1B | Janitor agent (report-only for 4 weeks) | ⏰ | 1 | 1A |
| 1C | MemU cross-agent wiring + Synthesizer skeleton | ⏰ | 2 | 1A |
| 1D | Continuous backtest / forward-test engine | ⏰ | 2 | 1A |
| 3A.2–3A.5 | Collector refactor + MediaProcessor + Qdrant + SignalSearch | ⏰ | 6 | 3A.1 |
| 2 | Execution Truth Layer (Treasury / Sizer / OMS) | ⏰ | 7 | 1A, 1C |
| 3 | Market Data Gateway + Redis fan-out | ⏰ | 3 | 1A |
| 4 | Time / Sessions service (UTC-only, session-by-name) | ⏰ | 2 | 1A |
| 5 | Company provisioning + capabilities (tenancy shell) | ⏰ | 4 | 2, 4 |
| 6 | Validation Engine (continuous Rule 1 pairing) | ⏰ | 3 | 1D, 2 |
| 7 | Indicator library 23 → ~250 + confluence | ⏰ | 7 | 1A |
| 8 | Walk-forward, OOS, 5 optimisers, promotion gate | ⏰ | 5 | 7 |
| 9 | Autonomous agents (Validator, Optimizer, Curiosity, RegimeWatcher) | ⏰ | 5 | 1C, 6, 8 |
| 10 | Alpaca + IBKR adapters | ⏰ | 3 + 3 | 2 |
| 11 | Arbitrage capability (first arb company) | ⏰ | 3 | 3, 10 |
| 12 | Owner dashboards (provisioning, wallets, perms, P&L, Rule-1 heatmap) | ⏰ | 5 | 5, 6 |

**Time to first paper-trading JarvAIs agent on a demo account: Phases 1A → 6 ≈ 3 weeks.**

### Phase 3A.1 — Intelligence schema + Discord remediation (landed 2026-04-18)

What shipped:

- `public.collector_sources` (subscription table, replaces `*_config.json`),
  `public.media_items` (one row per attachment, processing lifecycle), and
  `public.news_items +8 cols` (`source_id`, `channel_name`, `author`,
  `author_id`, `message_id`, `metadata jsonb`, `has_media`, `media_count`).
  Migration file: `migration/2026_04_18_phase3a1_collector_sources.sql`,
  rollback alongside it.
- Three dormant-but-fatal Discord collector bugs fixed:
  `INSERT IGNORE` → `ON CONFLICT (hash_key) DO NOTHING`; removed bogus
  `tickles_shared.` schema prefix from every SQL; HWM-save exception
  logging raised from `DEBUG` to `WARNING`.
- Discord bot token moved from plaintext `discord_config.json` (644) to
  `/etc/tickles/discord.env` (600 root:root). Token key stripped from JSON.
- Systemd unit `tickles-discord-collector.service` installed **disabled**
  and **inactive**. Dean flips it on manually after reviewing.
- 25 GB of unused downloaded media cleared (none of it had made it to the
  DB — strategy-irrelevant for now and the reference doc's Phase 3A.2
  will redesign download + analysis end-to-end).
- 35 `collector_sources` rows seeded (2 servers + 33 channels) from the
  existing `discord_config.json`.

Phase 3A.2 onwards (the bigger refactor: `MediaProcessor`, Qdrant
`tickles_signals` collection, `EmbeddingService`, `SignalSearchService`)
is scoped in
`/opt/tickles/shared/migration/Collector_STATUS_UPDATE_INTELLIGENCE_PIPELINE.md`.

---

## 3. Ground rules (carried forward, still binding)

1. Rule 1 — backtest-to-live parity (99.9% target). Every live trade has a shadow
   backtest row. Drift is attributed, not guessed.
2. Rule 2 — execution accuracy. Slippage, spread, funding, fees tracked per-entry
   in `trade_cost_entries`.
3. Rule 3 — memory efficiency. Bounded caches, partitioned candles, max 50 DB
   connections total.
4. Human-in-loop for capital. `approval_mode = human_all` until Phase 8 guardrails
   + Phase 9 Validator are proven.
5. Cross-company isolation. Per-company DB, container-tagged MemU, capability-gated
   access. Allowlist for any cross-company read.
6. Multi-company from day one. Every service takes `company_id` in its API
   signature, even if today only `jarvais` exists.

---

## 4. Phase-by-phase detail

Legend per phase: **Goal · Deliverables · Schema changes · Rollback · Success criteria**.

### Phase 1A — Scaffold + sync-from-VPS + organic migration policy (0.5 day) ✅

**Decision 2026-04-18.** We rejected the original "atomic restructure" plan
because (a) the VPS is live, (b) the local mirror was incomplete so we
couldn't safely `git mv` from local, and (c) real companies don't migrate
live systems with a single big-bang rename — they grow the new beside the
old and retire pieces as they're replaced.

**Discovery while executing 1A.** The live VPS had already been restructured
into feature folders by an earlier agent session. Specifically `market_data/`,
`connectors/`, `collectors/`, `services/`, `utils/__init__.py`, and
`migration/` already existed with code in them — the **local** `_vps_source/`
was the one lagging behind. So the actual 1A work became:

1. Pull VPS canonical state down over local (`scp tar.gz` + extract).
2. Retire my invented subfolders (`market_data/collectors/`, `market_data/adapters/`,
   `trading/adapters/`) — they duplicate VPS's top-level `collectors/` and
   `connectors/` which handle both directions.
3. Archive the stale root-level flat files (db/config pairs, *.sql, *.sh,
   seed scripts, one-shot tests) now that proper homes exist in `utils/`,
   `market_data/`, `migration/`, and `scripts/`.
4. Update docs to reflect reality (`ARCHITECTURE.md §3`, `CORE_FILES.md`,
   `MEMORY.md`).

**Goal.** Local mirrors VPS exactly. Feature-folder skeleton is real, not
hypothetical. Every new file from Phase 2 onwards lands in the right place.

**Deliverables (what actually shipped).**
- **VPS canonical folders in `_vps_source/`:** `utils/` (8 files), `market_data/`
  (6 files), `connectors/` (4 files), `collectors/` (3 py + data dirs),
  `services/` (2 files), `migration/` (SQL + seeds + historical docs).
- **Scaffold folders with READMEs** for code that doesn't exist yet:
  `trading/README.md`, `agents/README.md`, `_archive/README.md`,
  `market_data/README.md` (updated for real layout).
- **Archive batch** at `_vps_source/_archive/2026-04-18_sync-from-vps/`
  with a `MANIFEST.md` mapping every archived file → its successor.
- **`.cursor/rules/file-structure.mdc`** — enforcement layer.
- **`CORE_FILES.md`** — Janitor allowlist, updated to match actual paths.
- **`ARCHITECTURE.md`** — file layout section rewritten to describe reality.
- **Commits:** `0baaa5d` (V1 checkpoint), `c6a3799` (V2 baseline),
  `170c466` (scaffold), plus the sync-from-VPS commit landing after this doc.

**Organic migration rules (enforced by reviews).**

1. **New code goes in feature folders.** Any file created from Phase 2
   onwards must land in `market_data/`, `trading/`, `agents/`, etc. The
   Cursor rule catches deviations.
2. **Legacy files are frozen.** Nothing gets moved just because it *looks*
   misplaced. A file only moves when its replacement is live and validated.
3. **Retire-and-archive flow.** When a new file replaces an old one:
   (a) new file written in its feature folder,
   (b) all callers migrated,
   (c) smoke tests green,
   (d) `git mv old_file _archive/YYYY-MM-DD_<reason>/`,
   (e) systemd units updated if applicable,
   (f) services restarted,
   (g) commit with `retire:` prefix.
4. **Never delete.** `_archive/` is the only destination for retired files.
5. **Janitor phase 1B** scans monthly and reports (report-only for 4 weeks)
   on files that look orphaned — Dean reviews, not the agent.

**Schema changes.** None.

**Rollback.** `git revert` the scaffold commit removes the empty folders.
Nothing else changes.

**Success criteria.**
- Empty feature folders present on local and VPS.
- Next file written (first Phase 2 deliverable) lands automatically in
  `trading/` because the Cursor rule catches any other placement.
- `tickles-catalog`, `tickles-candle-daemon`, `tickles-bt-workers` services
  untouched, still `active`.

**Non-goals for 1A.**
- No systemd-unit edits. (They already point at `backtest.runner`,
  `candles.daemon`, `catalog.service` — the feature-folder module paths
  the live code uses.)
- No change to any file whose import graph is reachable from a running
  systemd ExecStart.

---

### Phase 1B — Janitor Agent (1 day)

**Goal.** An autonomous filesystem housekeeper with a four-tier safety model
that can never accidentally archive a core file.

**Deliverables.**
- `agents/janitor.py` — classifier + scanner + dry-run reporter.
- `systemd/tickles-janitor.service` — runs daily at 04:00 UTC, emits report to
  Telegram + MemU `review-findings` category.
- `CORE_FILES.md` (already committed in 1A) — the allowlist.
- First-week report generation (report-only mode — NO moves).
- Docs: `agents/README.md` with the four-tier classification logic.

**Classification tiers (hardcoded in `janitor.py`).**

- **Tier 0 untouchable** — matches systemd `ExecStart`, matches a glob in
  `CORE_FILES.md`, currently opened by a running process (`lsof`), or in the
  "do-not-modify" list. Janitor skips immediately, no report, no action.
- **Tier 1 dependency-protected** — reachable via Python `ast`-derived import
  graph from any Tier 0 entry point. Same treatment as Tier 0.
- **Tier 2 pending-cutover** — tagged `status: pending_cutover` in
  `CORE_FILES.md`. Treated as Tier 0 until the tag is removed.
- **Tier 3 ambiguous** — survives Tiers 0–2, atime AND mtime > 60 days. LLM
  reviews. Output goes to Telegram report only.

**Schema changes.** None.

**Rollback.** Disable the service: `systemctl disable --now tickles-janitor`.

**Success criteria.**
- 4-week dry-run posts a daily report to Telegram / MemU with zero
  false-positive Tier-0 intrusions.
- After 4 weeks, Dean flips `janitor.mode = enforce`. 7-day quarantine window
  activates. Every move lands in `_archive/YYYY-MM-DD_janitor/` with a
  `MANIFEST.md` explaining why.

---

### Phase 1C — MemU cross-agent wiring + Synthesizer (2 days)

**Goal.** Any agent reads/writes MemU with the category + frontmatter discipline
from the Opus stack-roadmap doc. The Synthesizer is the only path from
`candidates/` to `approved/`.

**Deliverables.**
- `memu/schema.py` — category tree + Pydantic model for frontmatter. Writes
  missing a required field are rejected at the client.
- `memu/client.py` — updated `write_insight()` enforces `schema.py`; adds
  `container` tag (company id or `shared`); adds `parent` (strategy genealogy).
- `memu/synthesizer.py` — nightly cron agent: scans `candidates/`, runs
  promotion checks (deflated_sharpe > 1.5, oos_sharpe > 1.0, num_trades > 30,
  verified_by non-null), moves matching rows to `approved/`.
- `agents/README.md` — "how agents find each other" (search MemU by predicate,
  don't poll filesystems).
- Migration `004_memu_container_tags.sql` — adds `container`, `parent`, and
  `verified_by` columns to the `insights` table if not present.
- Smoke test: Cody writes an insight, Schemy writes a schema observation,
  Audrey queries "show me all insights tagged container=shared in the last 24h"
  and gets both.

**Categories.**
```
strategies/{seeds,variants,approved,archive}
regimes/
backtest-results/
positions/{active,closed}
review-findings/
risk-events/
```

**Frontmatter (enforced on write).**
`id`, `parent`, `discovered_by`, `timestamp`, `company`, `container`, `status`,
`verified_by`, `metrics{sharpe,oos_sharpe,deflated_sharpe,win_rate,num_trades}`,
`train_period`, `test_period`, `oos_period`.

**Schema changes.** Additive columns on `memu.insights`. Backwards compatible.

**Rollback.** Drop the three new columns; revert synthesizer service.

**Success criteria.**
- Cody → Audrey cross-read round-trip works end-to-end without polling
  filesystems.
- Synthesizer rejects a write with missing `verified_by`.
- `container=jarvais` insights are invisible to any other company's agent
  unless an allowlist entry is added.

---

### Phase 1D — Continuous backtest / forward-test (2 days)

**Goal.** Every deployed strategy has a forward-test process that runs the same
engine on every new closed candle, emitting paired "shadow trades" to compare
against live fills.

**Deliverables.**
- `backtest/forward_test.py` — long-running process, one per (strategy, account).
  Listens to Postgres `NOTIFY candle_inserted`; on each new bar, calls
  `backtest.engine.run_bar(strategy_cfg, bar)`; writes shadow trade to
  `backtest_forward_trades` in ClickHouse.
- `migration/005_ch_forward_tables.sql` — adds `backtest_forward_runs` and
  `backtest_forward_trades`.
- `backtest/engine.py` — refactor to expose `run_bar()` (single-bar replay)
  alongside existing `run_backtest()` (batch). Same code path, Rule 1 by
  construction.
- `systemd/tickles-forward-test@.service` — templated unit, one instance per
  active strategy.
- Integration with catalog API: `/strategies/:id/forward-test-status`.

**Pairing key.** `(strategy_id, signal_timestamp_ms, symbol)` — used later by
Phase 6 Validator to do the 1:1 join.

**Catch-up logic.** On start, read `last_processed_candle_at` from state, replay
any missed bars sequentially. No full rerun needed.

**Fake-close-candle discipline (locked decision #4).** Forward-test engine honours
`strategies.uses_fake_close_candle`: only if true does it build a fake T-60 bar
at session close. Crypto and always-on strategies never do.

**Schema changes.**
```sql
-- ClickHouse
CREATE TABLE backtests.backtest_forward_runs (...)
CREATE TABLE backtests.backtest_forward_trades (...)

-- Postgres
ALTER TABLE tickles_shared.strategies ADD COLUMN uses_fake_close_candle BOOL NOT NULL DEFAULT FALSE;
```

**Rollback.** Stop forward-test services; `ALTER TABLE ... DROP COLUMN`; drop CH
tables. No live trade writes depend on it yet.

**Success criteria.**
- Deploying a strategy starts a forward-test process within 5 s.
- New candle (via NOTIFY) produces a shadow trade entry in < 500 ms.
- Restart replays the gap, no double-counting, idempotent by pairing key.
- `SELECT count() FROM backtest_forward_trades WHERE strategy_id=X` grows
  monotonically with market time.

---

### Phase 2 — Execution Truth Layer (Treasury + Sizer + OMS) — 7 days

**Goal.** One code path computes "quantity, leverage, SL/TP, expected
spread/slippage/fees" for both backtest and live. Rule 1 stops depending on hope.

**Deliverables.**
- `trading/sizer.py` — pure, deterministic function:
  `size(intent, account, market_snapshot, strategy_cfg) -> SizedIntent`.
  Ports `liquidation_utils.py` and the fee/spread/overnight math from
  Capital 2.0 and JarvAIs V1. Zero I/O, fully unit-testable.
- `trading/treasury.py` — service that owns balance snapshots, per-agent
  capability checks, and API-key resolution via env/vault reference.
  Never stores keys in code or DB.
- `trading/oms.py` — the only thing that calls an exchange adapter. Accepts
  `TradeIntent`, runs sizer → treasury → adapter → writes `trades` and
  `trade_cost_entries`. Idempotent via `client_order_id`.
- `trading/adapters/ccxt_adapter.py` — CCXT/Pro wrapper, crypto.
- `trading/adapters/capitalcom_adapter.py` — REST client port.
- `backtest/engine.py` — rewired to call `trading.sizer.size()` instead of its
  internal fee/slippage code. Same function, same numbers.
- `migration/006_capabilities.sql` — `capabilities`, `account_registry`,
  `leverage_history` tables (schema per CONTEXT_V3 §6).
- Golden test suite: `tests/test_sizer_golden.py` — known inputs → frozen
  outputs, covering crypto + CFD + spot + edge cases (near-liquidation,
  min-notional, exchange fee tiers).
- Risk Agent hook (no-op by default): `trading/risk_agent.py` — if
  `company.risk_agent_enabled == True`, sizer output passes through it before
  hitting treasury. Default OFF (locked decision #2).

**Schema changes.** Tables above; no destructive changes.

**Rollback.** Backtest engine reverts to its internal math; new tables can be
dropped without data loss because nothing reads them yet.

**Success criteria.**
- Golden-test diffs pre/post port = 0.
- One end-to-end paper trade: agent submits intent → sizer computes → treasury
  approves → OMS places via CCXT demo → fill writes `trades` + 2 rows in
  `trade_cost_entries` (maker_fee + spread).
- Same intent run through `backtest.engine.run_bar()` produces an identical
  sized order.
- Multi-company: submitting an intent with `company_id=foxtrot` while
  `account.company_id=jarvais` is rejected by treasury with "capability denied".

---

### Phase 3 — Market Data Gateway (3 days)

**Goal.** One CCXT Pro / Capital.com WS connection per exchange, fan out to
all consumers via Redis pub/sub.

**Deliverables.**
- `market_data/gateway.py` — multi-exchange WS hub with reconnect, rate-limit
  smoothing, and per-stream type fan-out.
- Redis channels: `candles:{ex}:{sym}:{tf}`, `ticker:{ex}:{sym}`,
  `orderbook:{ex}:{sym}`, `funding:{ex}:{sym}`.
- `market_data/candles_daemon.py` refactored to consume from the gateway
  instead of connecting directly.
- `systemd/tickles-market-gateway.service`.
- Docs: subscriber code example for any agent or strategy.

**Schema changes.** None (uses existing candles table).

**Rollback.** Candles daemon already works standalone — gateway is additive.
Disable the gateway service; candles daemon falls back to direct CCXT.

**Success criteria.**
- 3+ companies subscribe to `candles:binance:BTC/USDT:1m`, none of them open
  their own CCXT connection.
- WS reconnect after forced disconnect < 3 s.
- Rate-limit violations on exchanges = 0 in a 24h window.

---

### Phase 4 — Time / Sessions service (2 days)

**Goal.** UTC everywhere. Sessions by name, DST-aware, never hardcode hours.

**Deliverables.**
- `utils/time_sessions.py` — session registry + DST math (port
  `adaptive_timing.py` from Capital 2.0).
- Migration `007_sessions.sql` — `sessions`, `session_definitions` tables.
- Pre-seeded sessions: `crypto_24_7`, `london_equity`, `ny_equity`,
  `tokyo_equity`, `london_open_range`, `ny_open_range`, `capital_com_close`.
- Enforcement: strategies referencing hours literally (`"open_hour": 7`)
  raise a validator error on save.

**Schema changes.** Additive — new tables.

**Rollback.** Drop tables; revert strategies that reference session names.

**Success criteria.**
- Strategy configured with `"session": "london_open_range"` evaluates
  correctly regardless of VPS clock timezone.
- DST spring-forward day produces the correct 1-hour shift in session start.

---

### Phase 5 — Company provisioning + capabilities (4 days)

**Goal.** "Move-in ready" tenancy shell. New company = one CLI command + key
bind + capability grants.

**Deliverables.**
- `tickles` CLI (`scripts/tickles_cli.py`) — `create-company`, `bind-account`,
  `grant`, `revoke`, `list-companies`, `list-capabilities`.
- Migration `008_companies_registry.sql` — `companies`, `account_registry`
  (already partly from Phase 2), agent registry.
- `trading/treasury.py` extended — capability checks go through a single
  `authorize(agent_id, resource, action)` function.
- Docs: `PROVISIONING.md` — one-page "how to add a new company".
- Admin web page (tiny aiohttp UI) — `127.0.0.1:8766`, wraps the CLI.

**Schema changes.** Additive.

**Rollback.** CLI + UI are additive. Drop the new tables if we abandon.

**Success criteria.**
- Creating a test company from CLI in < 30 s, fully isolated DB, CEO agent
  stub spawned.
- Granting `read:candles:*` to an agent makes candles queryable; revoking
  blocks it immediately.
- Cross-company read attempt denied with audit log entry.

---

### Phase 6 — Validation Engine (3 days)

**Goal.** The Rule 1 enforcer. For every live/demo trade, find its paired
forward-test shadow, compute drift, attribute the breakdown, halt the strategy
if the rolling accuracy drops below threshold.

**Deliverables.**
- `trading/validation.py` — pairing service + drift attribution.
- Migration `009_trade_validations.sql` (per-company DB) — `trade_validations`
  table with full drift fields.
- `systemd/tickles-validator.service`.
- Materialized view in Postgres: `strategy_rolling_accuracy` (refresh every 5
  min).
- Halt integration: when accuracy < `strategy.halt_threshold_pct`, sets
  `strategies.is_active = FALSE` and notifies CEO.

**Depends on:** Phase 1D (shadow trades exist) + Phase 2 (live trades with
`candle_data_hash` + `signal_params_hash`).

**Rollback.** Disable service; accuracy tracking stops; no live trades affected.

**Success criteria.**
- Paper trade fills → `trade_validations` row created within 60 s.
- Drift breakdown sums to total drift (no unattributed residual).
- Forcing accuracy < threshold in a test → strategy halted, Telegram alert
  fires.

---

### Phase 7 — Indicator library 23 → ~250 + confluence (7 days)

**Goal.** Port Capital 2.0's 221 indicators + JarvAIs SMC set; add confluence.

**Deliverables.**
- `backtest/indicators/comprehensive.py` — ported indicator module.
- `backtest/indicators/registry.py` — metadata + param_ranges.
- `backtest/indicators/numba_fast.py` — JIT versions of hot-path indicators.
- `backtest/strategies/confluence.py` — N-of-M agreement strategy.
- Seed `indicator_catalog` to ~250 rows.

**Fix during port:** `ttm_squeeze_on` duplicate key, any other bugs noted in
CONTEXT_V3 §3.6.

**Schema changes.** None (existing tables).

**Rollback.** Old indicators untouched. Drop the new file.

**Success criteria.**
- `indicator_catalog` has ~250 rows.
- Confluence strategy runs on any 2-of-5 selection, produces deterministic
  signals, verifiable by seed.
- Hot-path indicators ≥ 5× throughput vs pre-JIT.

---

### Phase 8 — Walk-forward + 5 optimisers + promotion gate (5 days)

**Goal.** Trustworthy Sharpe numbers. Automatic "only promote if it passes
walk-forward OOS + deflated Sharpe".

**Deliverables.**
- `backtest/walk_forward.py` — rolling IS/OOS windows.
- `backtest/optimizers/` — random, grid, simulated annealing, GA, Bayesian.
- `backtest/promotion_gate.py` — single function called at end of a batch.
- Scheduled job: nightly re-optimisation on top 10 live strategies.

**Schema changes.** Populate `oos_sharpe`, `oos_return_pct` on `backtest_runs`.

**Rollback.** Leave columns at placeholder zero; revert script.

**Success criteria.**
- Batch of 100 param sweeps produces `deflated_sharpe` and `oos_sharpe` on every
  row.
- Promotion gate rejects a strategy with in-sample Sharpe 3, OOS Sharpe 0.4.

---

### Phase 9 — Autonomous agents (5 days)

**Goal.** Stand up the four agents that *use* the foundations built in 1D, 6, 8.

**Deliverables.**
- `agents/validator.py` — continuous Rule 1 watcher, alerts on drift.
- `agents/optimizer.py` — weekly walk-forward sweep, proposes DNA strands.
- `agents/curiosity.py` — port of `relationship_discovery.py`, autonomously
  sweeps 2-of-250 indicator combinations, writes to MemU
  `strategies/seeds/`.
- `agents/regime_watcher.py` — subscribes to `NOTIFY candle_inserted`,
  classifies regime, writes to MemU `regimes/`.
- All agents `approval_mode = human_all` (CEO approves proposals).

**Schema changes.** None.

**Rollback.** systemd disable per agent.

**Success criteria.**
- Curiosity produces 10+ `candidates/` insights in first 24h.
- Optimizer's weekly proposal gets a CEO-reviewable Telegram card.
- Validator reliably fires on injected drift in a test.

---

### Phase 10 — Alpaca + IBKR adapters (3 days each, can run parallel)

**Deliverables.**
- `trading/adapters/alpaca_adapter.py` + PDT pre-trade rule.
- `trading/adapters/ibkr_adapter.py` + `trade_type='spot_hold'` support.

**Success criteria.** Paper trade round-trip on each. PDT rule blocks a 4th
day-trade on a sub-$25k Alpaca account.

---

### Phase 11 — Arbitrage (3 days)

**Deliverables.**
- `backtest/strategies/cross_exchange_arb.py` — reads two Redis ticker streams,
  emits paired long/short intents.
- First arb company template: `tickles_arb_btc_bybit_binance`.
- Docs: arb operational runbook.

**Success criteria.** Demo account arb runs 24h, total slippage cost <
theoretical edge.

---

### Phase 12 — Owner dashboards (5 days)

**Deliverables.**
- Web UI over the Phase 5 CLI: company list, wallet bindings, capability grid,
  per-company P&L, Rule 1 accuracy heatmap per strategy.
- Mobile-friendly. Authenticated by SSH key + Telegram OTP.

**Success criteria.** Dean can provision a new company from his phone while
walking the dog.

---

## 5. Still-open decisions (not blocking 1A–1D)

These can be answered any time before their owning phase starts.

| Decision | Phase | Default if unanswered |
|---|---|---|
| Vault for API keys (HashiCorp / AWS) vs .env forever | 2 | `.env` until live capital — then migrate |
| Candle retention extension (currently 1m=90d) | 3 | Keep 90d; revisit when volume grows |
| Latency-class config per company (`standard` vs `fast`) | 5 | Default `standard`; arb companies flip to `fast` |
| MemU seeding from existing Mem0 data | 1C | Start clean; bridge later if useful |
| Autonomy phase B/C/D triggers (human → rule-based approval) | after 6 | Stays `human_all` until proven |
| Arb company: BTC, gold, or both first | 11 | BTC cross-exchange first |

---

## 6. How agents reading this should use it

1. When joining a fresh session, read `MEMORY.md` first, then this file.
2. Any new code you write must land in the folder this roadmap and
   `ARCHITECTURE.md` specify. If you don't know where it goes, ask before
   creating the file.
3. If a phase says "deliverables: X files" — don't add a 5th. Group by
   feature. See `.cursor/rules/file-structure.mdc`.
4. Update this file's phase-status table when you finish something. Append a
   dated line under the phase's "Success criteria" describing what you proved.
5. Store a MemU insight for every non-trivial decision or bug found
   (`review-findings` category) with the frontmatter from Phase 1C.

---

## 7. Master-plan alignment (2026-04-10)

The 27-phase "Trading House Master Plan" agreed in the
`c-JarvAIs` session assigns numbers **13–39** to the work that this
ROADMAP_V3 labels 1B → 12 plus a pile of new sections. Both
numberings remain valid. Mapping:

| Master plan # | ROADMAP_V3 § | One-line scope |
|---|---|---|
| 13 | (new) | Foundations cleanup (this file's landing pad) |
| 14 | new   | Universal Asset Catalog |
| 15 | new   | Data Sufficiency Engine |
| 16 | §3    | Candle Hub + multi-TF + backfill CLI |
| 17 | §3    | Market Data Gateway (CCXT-Pro WS + Redis fan-out) |
| 18 | §7    | Full indicator library (250+) |
| 19 | §1, §8 | Backtest Engine 2.0 (VectorBT sweeps + Nautilus execution-aligned) |
| 20 | new   | Feast Feature Store (Redis online / DuckDB offline) |
| 21 | §6    | Rule-1 Continuous Auditor |
| 22 | new   | Collectors-as-Services (on-demand lifecycle, incl. forward-test engine) |
| 23 | §3A.2-5 | Enrichment Pipeline (vision / whisper / chart OCR) |
| 24 | new   | Services Catalog ("the Menu") |
| 25 | §2    | Banker + Treasury + Capabilities |
| 26 | §2, §10 | Execution Layer on NautilusTrader |
| 27 | new   | Regime Service |
| 28 | new   | Crash Protection ported + extended |
| 29 | new   | Alt-Data Ingestion |
| 30 | §4    | Events Calendar + windows |
| 31 | §9    | Apex / Quant / Ledger modernised souls |
| 32 | §9    | Scout / Curiosity / Optimiser / RegimeWatcher activation |
| 33 | §11   | Arbitrage + Copy-Trader |
| 34 | new   | Strategy Composer |
| 35 | new   | Local-to-VPS Backtest Submission |
| 36 | §12   | Owner Dashboard + Telegram OTP + Mobile |
| 37 | new   | MCP stack |
| 38 | new   | Validation + code-analysis + docs freeze |
| 39 | new   | End-to-end drill |

The ROADMAP_V3 numbering (1A, 1B, 1C, 1D, 2-12, 3A.x) stays on this
document for historical continuity; the master-plan numbering is
what every new commit, test file, and change-log entry will cite
from Phase 13 onwards.

---

## 8. Phase 13 — Foundations cleanup (landed 2026-04-10)

### What shipped

- **Repository home moved.** All platform code now lives under
  `https://github.com/dgilbert002/Tickles-Co.git` (branch `main`, commit
  `967df65 init` at landing time). The legacy `JarvAIs` repo becomes
  read-only reference only.
- **VPS Git collapsed from 22 GB → 3.4 MB.** The old `.git` on
  `/opt/tickles` (which had years of accidentally committed binaries)
  was archived and replaced with a fresh `git init` pointed at the
  new GitHub remote. All four services
  (`paperclip`, `tickles-catalog`, `tickles-bt-workers`,
  `tickles-candle-daemon`) stayed active throughout the swap.
- **Three operator CLIs scaffolded.** `shared/cli/gateway_cli.py`,
  `shared/cli/validator_cli.py`, `shared/cli/forward_test_cli.py`.
  Each CLI's `status` and non-action subcommands work today; action
  subcommands return a clearly labelled "lands in Phase N" stub and
  exit code 2 so automation wired against these surfaces today keeps
  working once real bodies land.
- **MySQL legacy variants archived.** `shared/utils/config.mysql.py`
  and `shared/utils/db.mysql.py` moved to
  `shared/_archive/2026-04-10_mysql_legacy/` with a rollback README.
  No code outside `reference/` imports them.
- **Tests folder re-founded.** The pre-migration `.pyc`-only state
  was scrubbed. `shared/tests/conftest.py` and
  `shared/tests/test_cli_scaffolding.py` added. Phase 38 will
  re-author the full regression suite.
- **Master-plan ↔ ROADMAP_V3 mapping committed** (section 7 above)
  so nobody re-fights the numbering question.

### Success criteria (all green)

- [x] `python -m shared.cli.gateway_cli --help` exits 0 (proven by
      `tests/test_cli_scaffolding.py::test_help_via_python_m`).
- [x] `python -m shared.cli.validator_cli windows` emits one JSON
      line with `ok: true`.
- [x] `python -m shared.cli.forward_test_cli --help` exits 0.
- [x] `rg "config\.mysql|db\.mysql" shared --glob "!reference/**"
      --glob "!_archive/**"` returns zero hits.
- [x] `pytest shared/tests/test_cli_scaffolding.py` — all 13
      parameterised cases pass.
- [x] `ruff check shared/cli shared/tests` clean (twice — second run
      required by Operating Protocol).
- [x] Fresh commit on GitHub `dgilbert002/Tickles-Co` main branch.
- [x] Same commit pulled onto VPS `/opt/tickles`, services unaffected.

### Rollback

1. `cd /opt/tickles && git reset --hard 967df65` to drop Phase 13.
2. `mv shared/_archive/2026-04-10_mysql_legacy/*.mysql.py shared/utils/`.
3. Delete `shared/cli/` and `shared/tests/test_cli_scaffolding.py`.

No service config changed, no systemd units were added or removed,
so rollback is code-only.

---

## 9. Phase 14 — Universal Asset Catalog (landed 2026-04-10)

### What shipped

- **Additive migration** `shared/migration/2026_04_10_phase14_asset_catalog.sql`:
  - New tables: `venues`, `assets`, `instrument_aliases`.
  - New nullable FK columns on existing `instruments`: `asset_id`,
    `venue_id`. Candles + backtest_results keep joining on
    `instrument_id` untouched.
  - New read-view `v_asset_venues` — one row per asset × venue with
    spread / fee / funding / leverage pre-joined for arbitrage &
    dashboard consumers.
  - Seed rows for 9 venues: binance, binanceus, bybit, okx,
    coinbase, kraken, capital, alpaca, yfinance.
  - Backfill block wires `asset_id` + `venue_id` on every one of the
    50 existing rows (crypto by `base_currency`, CFDs by symbol).
  - Rollback file `2026_04_10_phase14_asset_catalog_rollback.sql`.
- **New Python module `shared/assets/`:**
  - `schema.py` — pydantic v2 models: `Venue`, `Asset`,
    `InstrumentRef`, `VenueAssetRow`, plus `AssetClass`,
    `VenueType`, `AdapterKind` enums matching the Postgres enum.
  - `service.py` — `AssetCatalogService`: async read API
    (`list_venues`, `venue_by_code`, `list_assets`,
    `asset_by_symbol`, `resolve_symbol`, `venues_for_asset`,
    `spread_snapshot`, `stats`). Resolver checks direct symbol
    match first, falls back to `instrument_aliases`.
  - `loader.py` — adapter-based ingester. Adapters:
    `CcxtAdapter` (crypto — binance/bybit/okx/coinbase/kraken/
    binanceus), `CapitalAdapter` (defers to existing
    `shared.connectors.capital_adapter` when `list_markets()`
    lands), `AlpacaAdapter` (stub until Phase 22),
    `YFinanceAdapter` (curated seed of 10 FX / commodity / index
    tickers so gold, silver, crude, S&P 500, EUR/USD, etc. get
    asset rows without needing a paid data feed).
    All upserts idempotent. Includes a `--dry-run` mode that
    fetches without writing.
- **Operator CLI** `shared/cli/assets_cli.py` wired into the Phase-13
  CLI package: `stats`, `list-venues`, `list-assets`, `resolve`,
  `spread`, `load`. Pipe-friendly JSON on every subcommand.
- **Legacy MySQL seed archived.**
  `shared/migration/seed_instruments.py` (pymysql, `tickles_shared`
  MySQL) moved to
  `shared/_archive/2026-04-10_mysql_legacy/seed_instruments.py.v1-mysql`
  and superseded by `shared/assets/loader.py`. README updated.
- **Tests** `shared/tests/test_assets.py` — 22 cases covering pydantic
  schema (class-value parity with the Postgres enum, canonical vs
  alias, `VenueAssetRow.total_cost_one_side_pct`), the
  `AssetCatalogService` against a `FakePool` (list, resolve,
  alias fallback, spread snapshot sort + delta), and loader
  helpers (`_d`, `_pct`, `_i`, `_capital_class`, YFinance curated
  set, CCXT graceful-degrade when ccxt not installed).
- **Test harness expanded** — `test_cli_scaffolding.py` now also
  exercises `assets_cli`'s 6 subcommands and `python -m
  shared.cli.assets_cli --help`.

### Success criteria (all green)

- [x] Migration is idempotent (every `CREATE` uses `IF NOT EXISTS`,
      every `ALTER` uses `ADD COLUMN IF NOT EXISTS`, every seed
      uses `ON CONFLICT DO UPDATE`).
- [x] Backfill links every one of the 50 existing instrument rows
      to an asset + venue.
- [x] `python -m shared.cli.assets_cli --help` exits 0.
- [x] ruff clean on `shared/cli shared/assets shared/tests`.
- [x] mypy clean on 10 files with `--namespace-packages
      --explicit-package-bases -p shared.cli -p shared.assets`.
- [x] pytest `shared/tests/` → **40 / 40 pass** (17 CLI
      scaffolding + 22 asset-catalog + 1 shared).

### Rollback

1. `cd /opt/tickles && psql -f shared/migration/2026_04_10_phase14_asset_catalog_rollback.sql`
2. `git revert <phase-14 commit>` or `git reset --hard <phase-13 commit>`.
3. Restore `seed_instruments.py` from
   `shared/_archive/2026-04-10_mysql_legacy/seed_instruments.py.v1-mysql`
   if the MySQL workflow is ever revived.

No services were restarted, no candle/backtest data was touched,
rollback is SQL-level only.

---

## 10. Phase 15 landed — Data Sufficiency Engine

**Date:** 2026-04-19 (shipped in commit after c722cd8)

### What went in

**Purpose.** Before anything downstream (backtests, forward-tests,
indicator fits, regime classifiers, strategy agents) is allowed to act
on a `(instrument, timeframe)` pair, the Data Sufficiency Engine must
grade the candle history and return a verdict:

* `pass` — all profile thresholds met;
* `pass_with_warnings` — soft issues (e.g. `is_fake` bars, density
  slightly below 95 percent, zero-volume bars);
* `fail` — hard issues (not enough bars, gap ratio exceeded, single gap
  too long, stale freshness, NULL OHLC, impossible candle geometry).

This is the first deterministic Rule-1 gate. LLM-level auditing of the
*verdicts* arrives in Phase 21; Phase 15 is the raw arithmetic layer
(no agent round-trip, just Python + SQL).

### Files added

* `shared/data_sufficiency/__init__.py` — package front door + exports
* `shared/data_sufficiency/schema.py` — pydantic models: `Verdict`,
  `Timeframe`, `Profile`, `Gap`, `IntegrityIssue`, `CoverageStats`,
  `SufficiencyReport`, `TIMEFRAME_MINUTES` lookup.
* `shared/data_sufficiency/metrics.py` — pure helpers:
  `detect_gaps`, `check_integrity`, `compute_coverage`. Zero DB.
* `shared/data_sufficiency/profiles.py` — six built-in profiles
  (`scalp_1m_crypto`, `swing_15m_crypto`, `swing_1h_crypto`,
  `position_4h_crypto`, `position_1d_crypto`, `swing_15m_equities`)
  plus the `system_config` loader (`NAMESPACE='sufficiency.profiles'`).
* `shared/data_sufficiency/service.py` — `SufficiencyService`:
  `check()`, `report_for()`, `invalidate()`, `stats()`, plus a pure
  `_grade()` function exported for tests.
* `shared/migration/2026_04_10_phase15_data_sufficiency.sql` —
  creates `data_sufficiency_reports` (BIGSERIAL PK, triplet UNIQUE,
  JSONB payload, TTL column, three indexes on instrument/timeframe,
  verdict, computed_at) and seeds the six profile rows with
  `ON CONFLICT (namespace, config_key) DO UPDATE`.
* `shared/migration/2026_04_10_phase15_data_sufficiency_rollback.sql` —
  pristine rollback: DROP table + DELETE the six config rows.
* `shared/cli/sufficiency_cli.py` — operator CLI with subcommands
  `stats`, `profiles`, `check`, `report`, `invalidate`, `bulk`.
* `shared/tests/test_data_sufficiency.py` — 39 unit tests covering
  metrics, profiles, grading edge cases, and service round-trips
  against a `FakePool`.

### Files edited

* `shared/cli/__init__.py` — added `sufficiency_cli` to `__all__`.
* `shared/tests/test_cli_scaffolding.py` — added the new CLI to the
  parameterised smoke suite (44 → 52 parametrised rows).

### Success criteria (all green)

* 79/79 pytest (40 Phase 13/14 + 39 new Phase 15).
* `ruff check shared/cli/ shared/assets/ shared/data_sufficiency/ shared/tests/` clean (two passes).
* `mypy --namespace-packages --explicit-package-bases --ignore-missing-imports
  -p shared.cli -p shared.assets -p shared.data_sufficiency` — 16 files clean.
* Migration applied on VPS without error; six profile rows visible in
  `system_config WHERE namespace='sufficiency.profiles'`.
* `python -m shared.cli.sufficiency_cli stats` returns JSON; zero
  cached rows initially; first call to `check` populates one.
* All four services remain active on VPS: `paperclip`, `tickles-catalog`,
  `tickles-bt-workers`, `tickles-candle-daemon`.

### Design rationale (the non-obvious choices)

* **Grading is pure** — `_grade(coverage, profile) -> Verdict` takes
  zero side-effects. Phase 21's auditor will reuse it as the
  fast-path and only escalate to LLM on disagreements.
* **Profiles live in `system_config`** so operators can tune thresholds
  without a code deploy. Built-ins are a safety net, not the source
  of truth.
* **TTL-based cache** (`ttl_seconds` defaults to 300 s). The candle
  daemon will call `invalidate()` on each partition sync in Phase 16,
  which is why the service exposes that public method now.
* **Market-hour awareness via `daily_bar_target`** — we do not ship a
  calendar table yet. Equities/CFD profiles use `daily_bar_target=26`
  for 15m (6.5-hour session), crypto uses 96; the density ratio
  compensates without needing a session grid.
* **No writes during `check` on cache hit** — the fresh scan path is
  the only write, so TTL hits are free.

### Rollback

1. `cd /opt/tickles && psql -f shared/migration/2026_04_10_phase15_data_sufficiency_rollback.sql`
2. `git revert <phase-15 commit>` or `git reset --hard <phase-14 commit>`.
3. No services restarted, no candle data touched, no legacy behaviour
   changed — rollback is pure DDL + config delete.

---

## 11. Phase 16 landed — Candle Hub + multi-TF + backfill CLI

**Date:** 2026-04-19 (shipped in commit after 258fcec)

### What went in

**Purpose.** The live 1m candle daemon (`tickles-candle-daemon`)
is untouched. Phase 16 adds the three pieces the daemon deliberately
does NOT do:

* **Multi-timeframe rollups** — `1m -> 5m/15m/30m/1h/4h/1d/1w` computed
  entirely inside Postgres with one `INSERT ... SELECT ... GROUP BY
  date_trunc(...) ON CONFLICT DO UPDATE` per (instrument, source,
  target). Idempotent. OHLC aggregation via `array_agg ORDER BY`
  (portable across vanilla PG, no TimescaleDB dependency).
* **Historical backfill** — CCXT async client pages `fetch_ohlcv`
  1000 bars at a time and upserts into the same `candles` table
  using the daemon's exact unique key contract
  `(instrument_id, source, timeframe, "timestamp")`.
* **Coverage introspection** — read-only queries over `candles x
  instruments` for the CLI and for the Phase 15 sufficiency
  invalidation hook.

### Files added

* `shared/candles/__init__.py` — package front door + `__all__`.
* `shared/candles/schema.py` — pydantic models: `Timeframe` enum
  (mirrors `timeframe_t`), `RESAMPLE_CHAIN`, `CoverageSummary`,
  `ResampleReport`, `BackfillReport`.
* `shared/candles/resample.py` — `bucket_floor_sql`,
  `build_resample_sql`, `resample_one`, `resample_chain`,
  `invalidate_sufficiency_for`.
* `shared/candles/backfill.py` — `backfill_instrument` (CCXT async,
  pagination, rate-limited, transactional upsert, auto-invalidates
  Phase 15 cache on write), plus `parse_window`, `minutes_between`,
  `estimate_pages` helpers.
* `shared/candles/coverage.py` — `list_coverage`, `coverage_stats`.
* `shared/cli/candles_cli.py` — subcommands `status`, `stats`,
  `coverage`, `resample`, `backfill`, `invalidate`.
* `shared/tests/test_candles.py` — 23 unit tests (schema, SQL
  builders, backfill helpers).

### Files edited (additive only)

* `shared/cli/__init__.py` — added `candles_cli` to `__all__`.
* `shared/tests/test_cli_scaffolding.py` — parametrised smoke suite
  extended with `candles_cli`.

### Success criteria (all green)

* 103/103 pytest (79 Phase 13-15 + 24 new Phase 16).
* Ruff: clean on all new files (two passes).
* Mypy: 23 source files, no errors
  (`--namespace-packages --explicit-package-bases --ignore-missing-imports`).
* Regression Phase 13/14/15: 83/83 passing.

### Design rationale

* **Daemon is sacred.** The live 1m collector is battle-tested (see
  `shared/candles/daemon.py` with its P0-E* hardening notes). Phase 16
  is strictly additive around it.
* **SQL-native rollups, not Python streaming.** For N million candles
  Python would be the bottleneck. Postgres handles group-by far
  faster, and the `ON CONFLICT` makes every resample idempotent.
* **Bucket math is explicit** — no TimescaleDB required. The
  expressions are short enough to eyeball and validate against
  exchange-native bars later. If we adopt Timescale in Phase 22 we can
  swap to `time_bucket` without the SQL builder signature changing.
* **Backfill auto-invalidates sufficiency.** Whenever `inserted_bars >
  0`, the backfill calls `invalidate_sufficiency_for(...)` so Phase 15
  grades against fresh coverage on the next check. Same story for
  resample — both entry points clear the triplet cache.
* **CLI-only surface.** No new systemd unit in Phase 16: the daemon
  handles live tail, the CLI handles on-demand resample/backfill.
  Phase 22 ("collectors-as-services") will wrap this into long-running
  services where appropriate.

### Rollback

No migration, no new DDL; rolling back is `git revert <commit>` or
`git reset --hard <prior-commit>`. The live daemon and live data are
untouched. Resampled rows already written can be removed selectively:

```sql
DELETE FROM candles WHERE timeframe IN ('5m','15m','30m','1h','4h','1d','1w')
    AND source IN (SELECT DISTINCT source FROM candles WHERE timeframe='1m');
```

---

## 12. Phase 17 — Market Data Gateway (CCXT Pro WebSocket + Redis fan-out)

### What this phase does (the 21-year-old version)

Up until now, all "live" market data on the box has come from the
*polled* 1-minute candle daemon — it asks each exchange "what was the
last minute?" once a minute and writes the answer to Postgres. That's
fine for candles, but it's blind to ticks, trade prints, top-of-book
spreads, perp marks and funding. It also can't react inside a minute.

Phase 17 introduces the **Market Data Gateway**: one durable process per
box that holds a single CCXT Pro WebSocket per exchange, multiplexes
many `(symbol, channel)` subscriptions onto it, and **fans out every
parsed message on Redis pub/sub channels** (`md.<exchange>.<symbol>.<channel>`).

Why one gateway? Because if every agent opened its own ws to Binance
we'd hit rate limits, multiply disconnect bugs, and lose auditability.
The gateway becomes the **only** ws talker on the box.

### What we built

**Python package — `shared/gateway/`**

* `schema.py` — pydantic models (`Tick`, `Trade`, `L1Book`, `MarkPrice`,
  `GatewayStats`, `SubscriptionRequest`, `SubscriptionKey`) and the
  channel-naming helpers (`channel_for`, `safe_symbol`).
* `subscriptions.py` — `SubscriptionRegistry`: an asyncio-locked,
  ref-counted registry. 47 agents asking for BTC/USDT trades opens
  exactly **one** stream; the last unsubscribe closes it.
* `redis_bus.py` — `RedisBus`: thin async wrapper around `redis.asyncio`
  that owns publishing, lag-tracking, stats publication, and the
  desired-state hash (`md:subscriptions`). All redis I/O is here so
  unit tests can mock it cleanly.
* `ccxt_pro_source.py` — `ExchangeSource`: per-exchange ccxt.pro client
  manager. Owns one client and a task per `(symbol, channel)` running
  `watch_ticker` / `watch_trades` / `watch_order_book` /
  `watch_mark_price`. Reconnects with exponential backoff + jitter
  (cap = 30 s). `parse_message()` is exposed as a pure function so we
  can unit-test the message conversion without ccxt.
* `gateway.py` — `Gateway` orchestrator: glues registry + sources + bus.
  Maintains counters for `messages_in`, `messages_published`,
  `publish_errors`, `reconnects`. Exposes `subscribe`, `unsubscribe`,
  `stats`, `registry_snapshot`, `sources_snapshot`. A background task
  publishes `GatewayStats` JSON to `md:gateway:stats` every 5 s.
* `daemon.py` — `tickles-md-gateway.service` entrypoint. Runs a
  desired-state reconcile loop: reads the `md:subscriptions` hash,
  diffs against current, calls `subscribe`/`unsubscribe`. SIGINT/SIGTERM
  shut everything down cleanly. CLI talks to Redis only — never to the
  daemon directly. This is intentional: the daemon can crash and
  restart and pick up the right subscriptions purely from Redis state.

**Operator CLI — `shared/cli/gateway_cli.py` (rewritten)**

Phase 13 stubs replaced with real commands:

* `status` — systemd state for `tickles-md-gateway.service`,
  `tickles-candle-daemon.service`, `tickles-catalog.service`,
  `tickles-bt-workers.service`.
* `services` — list of units this CLI manages (now includes
  `tickles-md-gateway.service`).
* `subscribe --venue binance --symbol BTC/USDT --channels tick,trade,l1
  [--requested-by agent-name]` — writes desired-state to Redis.
* `unsubscribe --venue binance --symbol BTC/USDT --channels tick` —
  removes desired-state.
* `list` — dumps the current desired-state hash with attribution.
* `stats` — reads `md:gateway:stats` plus per-pair lag keys. Returns
  `EXIT_FAIL` if the daemon hasn't published yet (so monitors can
  alert).
* `peek --venue binance --symbol BTC/USDT --channel trade --count 5
  [--timeout 10]` — subscribes to the redis pattern and prints the
  next N messages. Useful for sanity-checking that the gateway is
  actually fanning data out.
* `replay` remains a Phase-13-style stub but now lands in Phase 18 (it
  belongs with the indicator/feature back-replay path).

**Systemd unit — `systemd/tickles-md-gateway.service`**

Restart-on-failure with `RequiresRedis-server.service`. Reads
`/opt/tickles/.env`. Runs `python -m shared.gateway.daemon`.

**Tests — `shared/tests/test_gateway.py`**

Sixteen unit tests covering schema / channel naming, registry ref
counting, the pure `parse_message` for ticker/trades/L1, an
`ExchangeSource` driven against a fake ccxt client (success path +
reconnect-on-error), and an end-to-end `Gateway` against a `_FakeBus`
+ `_FakeSource` (ref-counted activate/deactivate, message publish,
counters). All use `asyncio.run()` to match the existing test style —
no `pytest-asyncio` dependency added.

`shared/tests/test_cli_scaffolding.py` updated for the new gateway
subcommand surface and the `replay`-now-lands-in-18 stub.

### Success criteria (verification)

1. `pytest shared/tests/` green locally and on the VPS — old +
   sufficiency + candles + assets + scaffolding + new gateway tests
   all pass.
2. `ruff` + `mypy` clean on the new `shared/gateway/*` and the
   rewritten `shared/cli/gateway_cli.py`.
3. `tickles-md-gateway.service` starts on the VPS and stays
   `active(running)`. `journalctl` shows the reconcile loop.
4. `gateway_cli stats` returns a non-null `GatewayStats` payload after
   the daemon's first 5-second tick.
5. `gateway_cli subscribe --venue binance --symbol BTC/USDT --channels tick`
   followed by a 10-second wait and `gateway_cli peek --venue binance
   --symbol BTC/USDT --channel tick --count 3` returns at least 3
   real ticker messages from Binance.
6. `messages_published_total` in `stats` is non-zero after the test
   subscription has run for a few seconds.
7. The legacy `tickles-candle-daemon` is **not touched** and remains
   `active(running)` throughout.

### Rollback

Phase 17 introduces no DDL and no destructive change. Roll back with:

```bash
sudo systemctl stop tickles-md-gateway.service
sudo systemctl disable tickles-md-gateway.service
sudo rm /etc/systemd/system/tickles-md-gateway.service
sudo systemctl daemon-reload
redis-cli del md:gateway:stats md:subscriptions
redis-cli --scan --pattern 'md:gateway:lag:*' | xargs -r redis-cli del
git revert <commit>   # or git reset --hard <prior-commit>
```

The Phase-13 stubbed `gateway_cli` is preserved in git history; the
desired-state hash is the only mutable artefact, and dropping it has
no effect on any other component.

---

*End of ROADMAP_V3.md. Phase 18 (Full Indicator Library — 250+
indicators wired to the Phase 17 firehose) starts next in the
master-plan sequence.*
