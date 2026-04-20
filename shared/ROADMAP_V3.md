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

## Phase 18 — Full Indicator Library (250+)

### Purpose

The Trading House needs a *big, discoverable* indicator catalog so the
scout / composer / optimiser can actually explore a meaningful search
space. Phase 13 left us with 23 hand-rolled indicators in
`shared/backtest/indicators/core.py` (+ `smart_money.py` +
`crash_protect.py`). Phase 18 takes that to **260 registered
indicators** while keeping the existing `IndicatorSpec` contract
unchanged, so no downstream code has to move.

### Strategy (why this shape)

We **don't** re-write every TA function from scratch:

1. Reuse our existing `register(name, fn, defaults, param_ranges,
   category, direction, description, asset_class)` contract — it's
   already audited, already plugged into the indicator catalog writer,
   and already used by the backtest engine.
2. Add a **pandas-ta bridge** that wraps ~70 pandas-ta functions,
   registering *one spec per output column* for multi-output
   indicators (MACD = line/signal/hist; BBANDS = lower/mid/upper/bw/%b;
   KDJ = k/d/j; etc.). pandas-ta is MIT-licensed and pure-python, so it
   inherits our "installable, no compiled deps" posture.
3. Add an **extras module** of pure-pandas indicators we want *even
   if* pandas-ta disappears later — Yang-Zhang volatility, rolling
   Sharpe/Sortino/Calmar, Hurst, VWAP session, streak counters, wick
   fractions, close-position-in-range, and so on.
4. Silent-degrade: both bridge and extras are imported in
   `shared/backtest/indicators/__init__.py` inside a try/except, so if
   `pandas_ta` ever regresses we still ship the core 70 indicators.

### Built (summary)

**Indicator modules**

* `shared/backtest/indicators/pandas_ta_bridge.py` — 190 registrations
  prefixed `pta_*`, covering:
  * 22 moving averages (DEMA, TEMA, HMA, ALMA, KAMA, ZLMA, VIDYA,
    VWMA, Fibonacci-WMA, Holt-Winters, Jurik, McGinley, Super
    Smoother, TRIMA, T3, SWMA, sine-WMA, Pascal-WMA, Wilder RMA,
    midpoint, midprice, HT Trendline).
  * Supertrend (line + direction), PSAR (long / short / AF).
  * ADX / DMP / DMN, Aroon (up / down / osc), Vortex (+/-),
    Ichimoku (5 lines).
  * 28 momentum oscillators (APO, BIAS, BRAR, CFO, CG, CMO, Coppock,
    CTI, ER, Fisher, Inertia, Momentum, PGO, PPO, PSL, PVO, QQE, ROC,
    RSX, Connors-RSI, Slope, SMI, StochRSI, TRIX, TSI, UO, Williams
    %R, Elder-Ray).
  * CCI, KDJ, KST.
  * 15 volatility (ATR-TS, Choppiness, HL2, HLC3, kurtosis, Mass
    Index, NATR, pdist, RVI, Elder Thermometer, true range, Ulcer,
    variance, entropy, z-score). Bollinger (5 cols), Keltner (3),
    Donchian (3), AccelerationBands (3).
  * 14 volume (AD, ADOSC, AOBV, CMF, EFI, EOM, KVO, NVI, PVI, PVol,
    PVR, PVT, TSV, VHM).
  * Statistical (linreg, MAD, median, quantile, skew, stdev).
  * Performance (log_return, percent_return, drawdown).
  * Candle pattern scaffolding (Doji, Inside, candle color, HA
    open/close).
  * Direction helpers (increasing / decreasing).
  * MACD (3), Stoch (3), Alligator (3), AMAT (2), AO, BOP,
    Chandelier Exit (3), CKSP (2), DPO, EBSW, HILO (3), MAMA (2),
    TTM Trend, VHF, Aberration (4), TOS-stdev bands (5), Elder-Ray
    Bear, Squeeze (3), PPO (3 cols), SMI (3 cols), QQE (4 cols),
    Fisher signal, StochRSI k/d, TSI signal, BRAR br, DM (+/-).
* `shared/backtest/indicators/extras.py` — 47 hand-rolled indicators
  prefixed `ext_*`, covering statistical (z-score, percentile rank,
  rolling skew/kurt, close/volume correlation, Hurst), volatility
  (true range, ATR%, Garman-Klass, Yang-Zhang, high-low range & %,
  close position in range), returns/performance (log return, pct
  return, cumulative, drawdown, max DD, rolling Sharpe / Sortino /
  Calmar), trend/momentum (SMMA/RMA, MA envelopes, price>SMA,
  EMA slope, close slope, gain ratio), volume (volume z-score,
  volume ratio, dollar volume, session VWAP, accumulation %), pattern
  (bullish / bearish streaks, gap %, body %, upper/lower wick %,
  inside / outside bars), and range helpers (rolling high/low, age of
  high/low, distance to high/low).
* `shared/backtest/indicators/__init__.py` — now also imports the
  bridge + extras and calls `register_all()` inside try/except so the
  system keeps working even if a dependency is missing.

**Operator CLI — `shared/cli/indicators_cli.py`**

Follows the same single-line-JSON pattern as every other Phase 13+
CLI:

* `count` — total + `{by_category}` + `{by_direction}` counts.
* `list [--category X] [--direction Y]` — enumerate specs.
* `categories` — indicator names grouped by category.
* `describe <name>` — full spec (defaults, param ranges, description,
  category, direction, asset class). Returns `EXIT_FAIL` if unknown.
* `search <substr>` — case-insensitive substring match across name +
  description.

**Tests — `shared/tests/test_indicators.py`**

Eleven new tests: registry size ≥ 250, presence of core basics,
presence of bridge + extras prefixes, synthetic OHLCV execution for
a sample of core / extras / bridge indicators, spec field sanity
(categories must be in a whitelist, every fn callable), and three
CLI smoke tests (`count`, `describe rsi`, unknown-indicator failure,
`python -m shared.cli.indicators_cli --help` exits 0).

### Success criteria (verification)

1. `pytest shared/tests/` stays green locally *and* on the VPS (130
   passing including the 11 new Phase 18 tests).
2. `ruff` + `mypy --ignore-missing-imports` both clean on the new /
   modified files.
3. `python -m shared.cli.indicators_cli count` returns `total >= 250`
   both locally and on the VPS.
4. Existing Phases 13 – 17 behaviour untouched:
   * Systemd services still `active(running)`:
     `tickles-candle-daemon`, `tickles-md-gateway`, `tickles-bt-workers`.
   * Gateway CLI (`shared.cli.gateway_cli stats`) continues to
     publish live stats.
   * Candles CLI smoke tests still pass (no schema changes).
5. `pandas_ta` installed in the VPS system Python (`/usr/bin/python3`)
   so the workers can import the bridge when a strategy asks for a
   `pta_*` indicator.

### Rollback

Phase 18 has **no** database migrations, no systemd units and no
mutable external state:

```bash
# 1. revert the commit (or git reset --hard)
cd /opt/tickles
git revert <phase-18-commit>

# 2. optionally remove pandas_ta if we want an "absolutely clean" box
sudo /usr/bin/python3 -m pip uninstall -y pandas_ta
```

No data loss, no restart sequencing: the backtest workers just drop
back to the original 23-indicator registry automatically.

---

## Phase 19 — Backtest Engine 2.0 (VectorBT + Nautilus)

### Purpose

The existing `shared/backtest/engine.py` is accurate and audited but
slow (Python for-loop per bar). Phase 19 introduces an **engine
registry** so strategies / optimisers / Scout can pick the right
backend for the job without rewriting call-sites:

  * `classic` — the existing hardened event-loop engine, source of
    truth for Rule 1.
  * `vectorbt` — numba-vectorised engine for parameter sweeps
    (thousands of combos in seconds).
  * `nautilus` — scaffolded NautilusTrader adapter; real
    implementation lands in Phase 26 when the execution layer goes
    live.

A parity harness keeps the engines honest: any new engine must
produce numbers within per-metric tolerances of the classic engine
on the same strategy + config, and CI runs this harness.

### Built

**Package — `shared/backtest/engines/`**

* `protocol.py` — `BacktestEngine` Protocol + `EngineCapabilities`
  dataclass. Every engine answers `available() -> bool` and
  `run(df, strategy, cfg) -> BacktestResult` using the existing
  config/result schema, so no downstream changes are needed.
* `classic.py` — thin wrapper around `engine.run_backtest`; advertises
  intrabar SL/TP + funding + fees + slippage; no sweep mode.
* `vectorbt_adapter.py` — wraps `vectorbt.Portfolio.from_signals`.
  Entries/exits are shifted forward by one bar to match our
  next-bar-open rule; `size_type="percent"`, `sl_stop` / `tp_stop`
  translated from percent to fraction. Trade records are mapped back
  into our `Trade` dataclass. Runs only when vectorbt is installed;
  gracefully reports `available() == False` otherwise.
* `nautilus_adapter.py` — stub that imports cleanly, reports
  capability flags, and raises `RuntimeError("…Phase 26…")` on
  `run()`. This keeps engine discovery symmetric today.

**Parity harness — `shared/backtest/parity.py`**

* `ParityTolerances` — dataclass with per-metric absolute tolerances
  (`num_trades_abs`, `pnl_pct_abs`, `sharpe_abs`, `winrate_abs`,
  `max_drawdown_abs`).
* `EngineDigest` — five-metric summary per engine plus runtime/ok
  fields.
* `parity_summary(candles_df, strategy, cfg, engines, …)` — runs
  every listed engine, digests their results, and compares each
  non-source engine against the source-of-truth within tolerance.
  Missing-dep engines are reported but never fail the harness.

**Operator CLI — `shared/cli/engines_cli.py`**

Four subcommands, JSON stdout:

* `list` — every engine with `available` + capabilities.
* `capabilities` — full capability matrix.
* `sample --engine <name>` — run a small synthetic SMA-cross backtest
  against an engine (smoke-test).
* `parity [--engines classic,vectorbt]` — run the synthetic backtest
  through the listed engines and emit the full parity report.

**Tests — `shared/tests/test_engines.py`**

11 smoke tests: registry presence, `get()` KeyError, classic runs
end-to-end on synthetic data, vectorbt runs when installed (skipped
otherwise), Nautilus stub raises `RuntimeError("…Phase 26…")`,
parity runs classic-only, parity compares classic vs vectorbt with
wide tolerances when vectorbt is present, and the four CLI
subcommands (list / sample / parity / capabilities).

### Success criteria (verification)

1. `pytest shared/tests/` green locally *and* on VPS (141 passing).
2. `ruff` + `mypy --ignore-missing-imports` clean on the new
   modules.
3. `python -m shared.cli.engines_cli list` reports the three
   engines and their availability flags.
4. `python -m shared.cli.engines_cli parity --engines classic,vectorbt`
   returns `ok: true` with both engines listed.
5. Phase 13 – 18 unchanged — indicator catalog still ≥ 250, systemd
   units still active(running), gateway/sufficiency/candles CLIs
   unaffected.

### Rollback

Phase 19 adds zero database migrations, zero systemd units, and zero
mutable external state. Rollback is:

```bash
cd /opt/tickles
git revert <phase-19-commit>
# optional cleanup: drop vectorbt from venv if we want an all-classic box
/opt/tickles/.venv/bin/pip uninstall -y vectorbt
```

Downstream code has no hard dependency on the new `engines/` package
— `engine.run_backtest` remains the primary entry point throughout
Phases 13 – 18 and was not touched.

---

## Phase 20 — Feature Store (Feast-style, Redis + DuckDB)

### Purpose

Strategies, agents, and the optimiser all need the same short list
of engineered features (rolling returns, volatility, microstructure,
…) computed the same way at train, backtest, and live time. Phase 20
adds a thin feature-store layer so we never re-derive those values
at call-site and never accidentally drift between paper and live.

We deliberately do **not** depend on the upstream `feast` package —
its install footprint is heavy and its Redis / DuckDB drivers are
more than we need. Instead we implement a tiny Feast-compatible API
on top of the infra we already run:

  * **online store** = Redis (already on the box, already used by
    Phase 17 market-data gateway).
  * **offline store** = DuckDB + parquet files on disk, one
    partition per (feature_view, entity_key).

If we ever need Feast proper, the primitives (`Entity`, `Feature`,
`FeatureView`, `FeatureStore`) already mirror its names and
semantics, so migration is a driver swap, not a rewrite.

### Built

**Package — `shared/features/`**

* `schema.py` — `Entity`, `Feature`, `FeatureView`, `FeatureDtype`
  dataclasses. `FeatureView.compute(candles, entity_key, params) ->
  DataFrame` + `validate_output` so every view is self-describing.
* `registry.py` — process-global `FEATURE_VIEWS` dict +
  `register_feature_view` / `list_feature_views` / `get_feature_view`.
* `online_store.py` — `RedisOnlineStore` (HASH-per-entity, TTL per
  view) + `InMemoryOnlineStore` test double.
* `offline_store.py` — DuckDB / parquet store. Partition layout is
  `<root>/<view>/entity=<key>/data.parquet`; `write_batch` appends +
  dedupes on timestamp, `read_range` does point-in-time queries, and
  `get_historical_features` joins multiple entity keys.
* `store.py` — `FeatureStore` façade tying online + offline
  together; `materialize`, `get_online`, `get_online_many`,
  `get_historical`.
* `feature_sets.py` — three starter views, registered on import:
  `returns_basic` (5 features), `volatility_basic` (4),
  `microstructure_basic` (4).

**Operator CLI — `shared/cli/features_cli.py`**

Six subcommands, single-line JSON stdout:

* `list` — every registered feature view + entities + features.
* `describe <name>` — full metadata for one view.
* `materialize` — pull candles from the Postgres `candles` table
  (or `--parquet PATH`) and write features to both stores.
  Flags: `--view`, `--entity`, `--symbol`, `--venue`, `--timeframe`,
  `--start`, `--end`, `--limit`, `--no-online`, `--no-offline`,
  `--in-memory`.
* `online-get --view --entity` — latest online vector.
* `historical-get --view --entities --start --end --head` — point-
  in-time range.
* `partitions --view` — list entity partitions already on disk.

**Tests — `shared/tests/test_features.py`**

18 tests covering: built-in views registered; `validate_output`
rejects bad columns + bad index; custom views register and resolve;
online-store round-trip + `read_many` + key shape + deserialise-row
edge cases; offline-store write/read + dedupe + partitions; high-
level `FeatureStore.materialize` + `get_online` + `get_historical`;
unknown view raises; CLI `list` / `describe` / `materialize` /
`online-get` / `historical-get` / `partitions`.

### Success criteria (verification)

1. `pytest shared/tests/` green locally *and* on VPS (159 passing).
2. `ruff` + `mypy --ignore-missing-imports` clean on the new
   modules.
3. `python -m shared.cli.features_cli list` reports ≥ 3 views.
4. `python -m shared.cli.features_cli describe returns_basic`
   returns a view with 5 features.
5. Materialise against a live symbol on VPS and verify both a
   Redis HASH (`tickles:fv:returns_basic:binance:BTC/USDT`) and a
   parquet file (`/opt/tickles/var/features/returns_basic/
   entity=binance_BTC_USDT/data.parquet`) exist afterwards.
6. Phases 13 – 19 unchanged — indicators still ≥ 250, engines CLI
   still lists classic/vectorbt/nautilus, systemd units still
   active(running).

### Rollback

Phase 20 is pure additive code. Rollback = `git revert` + remove
the feature-store directory if operator wants to reclaim disk:

```bash
cd /opt/tickles
git revert <phase-20-commit>
rm -rf /opt/tickles/var/features   # optional
redis-cli --scan --pattern 'tickles:fv:*' | xargs -r redis-cli del  # optional
```

No systemd units were added, no schema migrations were run.

---

## Phase 21 — Rule-1 Continuous Auditor

### Purpose

Rule 1 of the trading house is: **backtests must equal live**. If
the engine behaves one way under historical data and a different
way in production, every plan we build on top of it is wrong.
Phase 21 is the watchdog that proves, on a rolling basis, that
the invariant still holds — and that shouts immediately when it
doesn't.

The auditor has three comparators:

  1. **Parity comparator** — re-runs the same strategy on the
     same candles across the registered engines (Phase 19) and
     compares PnL / Sharpe / winrate / max-drawdown against a
     source-of-truth engine (default: `classic`).
  2. **Live-vs-backtest comparator** — takes a real executed
     trade (from Phase 26 onwards) and the backtest trade that
     "should" have been its twin and diffs entry/exit/fees/
     slippage/pnl.
  3. **Fee/slippage comparator** — checks that the assumed
     fee-bps and slippage-bps the strategies plan against match
     what the exchange actually charged.

Every audit event is persisted to a local SQLite DB and tagged
with severity (`ok` / `warn` / `breach` / `critical`). Operators
can tail events, roll them up into a rolling summary, or run the
auditor forever as a systemd service.

### Built

**Package — `shared/auditor/`**

* `schema.py` — `AuditEventType` (enum: parity_check,
  live_vs_backtest, fee_slippage, coverage, heartbeat),
  `AuditSeverity` (enum: ok/warn/breach/critical),
  `AuditRecord` (dataclass with `to_json` + `to_row`) and
  `DivergenceSummary` (rolling stats dataclass).
* `storage.py` — SQLite-backed `AuditStore`. Default path
  `/opt/tickles/var/audit/rule1.sqlite3` on Linux, override via
  `TICKLES_AUDIT_DIR` env var. Indexes on
  `(event_type, ts_unix)`, `(severity, ts_unix)`, and
  `(subject, ts_unix)` for cheap tail queries.
  Methods: `record`, `record_many`, `list_recent`, `summary`,
  `purge_older_than`, `replay`.
* `comparator.py` —
    * `ParityComparator` (wraps `shared.backtest.parity.parity_summary`),
    * `LiveVsBacktestComparator` + tolerances dataclass,
    * `FeeSlippageComparator`.
* `auditor.py` — `ContinuousAuditor` + `AuditJob` dataclass.
  Runs each job on its own cadence, emits `HEARTBEAT` every N
  seconds and a `COVERAGE` event per `tick()` so operators can
  prove the auditor is alive even during quiet markets.
  `run_forever()` is a blocking loop suitable for systemd.

**Operator CLI — `shared/cli/auditor_cli.py`**

Eight subcommands, single-line JSON stdout:

* `status` — last heartbeat + rolling summary at a glance.
* `summary [--window SEC]` — detailed rollup.
* `events [--limit N] [--severity ...] [--event-type ...]` —
  tail recent events.
* `run-parity-check --strategy-id --engines classic,vectorbt
  [--pnl-pct-abs --sharpe-abs --winrate-abs
  --max-drawdown-abs]` — one-shot parity check against a
  synthetic SMA-cross + candle feed. Tolerances configurable;
  default `sharpe_abs=8.0` because synthetic data has extreme
  Sharpe ratios.
* `simulate-live-trade` — feed a fabricated `(live, backtest)`
  trade pair through `LiveVsBacktestComparator`; used to
  smoke-test the Phase 26 integration hook before the live
  execution layer is online.
* `tick` — one pass of `ContinuousAuditor` against the built-in
  synthetic job. Emits at least one parity record + one coverage
  event + one heartbeat.
* `run` — blocking, suitable for systemd. Runs forever.
* `purge-older --days N` — delete events older than N days.

**Tests — `shared/tests/test_auditor.py`**

14 tests covering: `AuditRecord.to_json` roundtrip, `AuditStore`
(record, list, summary, purge, severity + event-type filters),
`ParityComparator` (happy path + breach path via tight
tolerance), `LiveVsBacktestComparator` (pass + drift fail),
`FeeSlippageComparator` (pass + fee drift + slippage drift),
`ContinuousAuditor.tick()` coverage+heartbeat, and CLI
round-trip for `status` / `summary` / `events` /
`run-parity-check` / `tick` / `purge-older`.

### Success criteria (verification)

1. `pytest shared/tests/test_auditor.py` — 14/14 green locally
   and on VPS.
2. `ruff` + `mypy --ignore-missing-imports --explicit-package-bases`
   clean on the new modules.
3. `python -m shared.cli.auditor_cli status` returns
   `{"ok": true, ...}` on a fresh box.
4. `python -m shared.cli.auditor_cli run-parity-check
   --engines classic,vectorbt` returns `"ok": true` against the
   synthetic feed under the default tolerances.
5. A regression re-run of Phase 18/19/20 test files still
   passes — Phase 21 is strictly additive.

### Rollback

Pure additive code. No schema migrations on the shared Postgres.
The SQLite DB lives under `/opt/tickles/var/audit/` and can be
deleted safely.

```bash
cd /opt/tickles
git revert <phase-21-commit>
rm -rf /opt/tickles/var/audit   # optional
```

If the auditor is run as a systemd unit in a later phase, also
`systemctl disable --now tickles-auditor.service` before
reverting.

---

## Phase 22 — Collectors-as-Services (generic service runtime)

### Purpose

Every long-lived Tickles process has the same shape: run
forever, do work on a cadence, respect signals, back off on
errors, prove you're alive. Phase 22 factors that shape into a
single `ServiceDaemon` class plus a thin adapter that wraps any
existing :class:`shared.collectors.base.BaseCollector` as a
supervised service — no collector code is modified.

It also introduces a process-global :class:`ServiceRegistry`
so the operator CLI (and later phases 24 Services Catalog /
25 Banker) can discover every long-running piece of the
platform from a single place. Heartbeats are wired into the
Phase 21 auditor so `systemctl is-active` is no longer the
only truth about service liveness.

### Built

**Package — `shared/services/`**

* `daemon.py` — `DaemonConfig`, `DaemonStats`, `ServiceDaemon`.
  Generic async supervisor loop: signal-aware `run_forever()`,
  exponential backoff with jitter on consecutive failures (cap
  = `max_backoff_seconds`), and best-effort heartbeats into
  `shared.auditor.AuditStore` every `heartbeat_every_seconds`.
* `collector_service.py` — `CollectorServiceAdapter` wraps any
  `BaseCollector` instance into a `ServiceDaemon`, plus a free
  function `run_collector_once` for use from CLIs and tests.
  Takes an optional async `db_pool_factory`; without one, only
  `collect()` runs (dry-run mode).
* `registry.py` — `ServiceDescriptor` + `ServiceRegistry` +
  `SERVICE_REGISTRY` singleton, seeded with the 9 services we
  already run (or ship) on the VPS: md-gateway,
  candle-daemon, catalog, bt-workers, discord-collector,
  news-rss, telegram-collector, tradingview-monitor, auditor.
  Each descriptor records its systemd unit name, module path,
  whether it's currently enabled on the VPS, and an optional
  factory (populated incrementally in later phases).
* `launcher.py` — `python -m shared.services.launcher
  --name NAME` entrypoint used by the systemd template. If a
  factory is registered for NAME, we call its `run_forever()`.
  Otherwise we delegate to `python -m <module>` so legacy
  collectors keep working unchanged.
* `systemd/tickles-service@.service` — systemd instance
  template so Phase 24 can stand up new services with
  `systemctl enable --now tickles-service@<name>.service`
  without writing a new unit file per service. Not enabled by
  Phase 22 — shipped as a file on disk.

**Operator CLI — `shared/cli/services_cli.py`**

Six subcommands, all single-line JSON stdout:

* `list [--kind KIND]` — every registered service
  (md-gateway, candle-daemon, catalog, bt-workers, …).
* `describe <name>` — full descriptor for one service.
* `status [--name NAME]` — systemd status for one or all units
  via `systemctl show` (returns `unknown` off-box).
* `heartbeats [--service NAME] [--limit N]` — tail heartbeat
  events from the Phase 21 auditor SQLite store; filter by
  service.
* `run-once --name NAME` — run one tick of the named service
  using its in-process factory. Returns a friendly error if
  no factory is registered yet.
* `systemd-units` — list `tickles-*.service` unit files on the
  current box.

**Tests — `shared/tests/test_services.py`**

19 tests covering: builtin registry seed, descriptor JSON,
custom registry register/get/`by_kind`; `DaemonConfig`
validation and auto-adjusted `max_backoff_seconds`;
`ServiceDaemon.run_once` happy path + exception path;
`run_forever()` graceful stop; backoff schedule; stats
JSON-serialisable; `CollectorServiceAdapter` happy paths
(with + without pool) + interval inheritance from
`CollectorConfig`; `services_cli` round-trip for `list`
(+ kind filter), `describe` known/unknown, `heartbeats`
empty, `run-once` no-factory.

### Success criteria (verification)

1. `pytest shared/tests/test_services.py` — 19/19 green
   locally and on VPS.
2. Regression: `pytest shared/tests/test_indicators.py
   shared/tests/test_engines.py shared/tests/test_features.py
   shared/tests/test_auditor.py shared/tests/test_services.py`
   — 73/73 green.
3. `ruff` + `mypy --ignore-missing-imports
   --explicit-package-bases` clean on the six Phase 22 files.
4. `python -m shared.cli.services_cli list` reports ≥ 9
   registered services on VPS.
5. `python -m shared.cli.services_cli status` reports
   `active=active` for md-gateway, candle-daemon, catalog,
   bt-workers on VPS.
6. Existing services untouched — Phase 17's gateway unit,
   Phase 13's candle daemon, Phase 16's bt-workers all remain
   `active (running)` after deployment.

### Rollback

Pure additive. No systemd unit changes applied. Rollback =
`git revert` and optionally delete
`/opt/tickles/shared/services/systemd/tickles-service@.service`
(not installed to `/etc/systemd/system` by Phase 22).

```bash
cd /opt/tickles
git revert <phase-22-commit>
```

---

## Phase 23 — Enrichment Pipeline

### Purpose

Raw news items (Discord / Telegram / RSS / TradingView) land in
``public.news_items`` with a headline + content and nothing else.
Every downstream consumer (symbols, agents, the optimiser) wants
the *enriched* view: which instruments are we talking about? is
the sentiment bullish or bearish? is this even worth reading?

Phase 23 ships a deterministic, low-dependency enrichment
pipeline that fills those fields in without requiring an LLM for
the baseline case. It's pluggable — stages share a common
interface so a later phase can swap in a transformer or route
the long-tail to OpenClaw without touching the orchestration.

### Built

**Package — `shared/enrichment/`**

* `schema.py` — `EnrichmentResult` (the accumulator that flows
  through the pipeline), `EnrichmentStage` ABC, `SymbolMatch`.
* `pipeline.py` — `Pipeline` runner with duplicate-stage-name
  guard and per-stage timing/error capture. `build_default_pipeline()`
  wires language → symbols → sentiment → relevance in that
  order.
* `stages/language.py` — ASCII-ratio + English-stopword heuristic
  returning `en | non_en | unknown`. No native deps.
* `stages/symbol_resolver.py` — three-pass regex resolver
  (pair form `BTC/USDT`, ticker form `$BTC`, bare-word `BTC`).
  Consumes a preloaded instrument list (shape mirrors the
  `instruments` table) or falls back to a compiled-in whitelist
  of 11 liquid majors. Matches are deduped per (symbol, exchange).
* `stages/sentiment.py` — keyword-based scorer with curated
  bullish/bearish vocab, simple negation handling, multi-word
  phrase support. Score is in `[-1, +1]`, label at `±0.1`.
* `stages/relevance.py` — composite score using symbol count,
  action verbs (long/short/buy/sell/TP/SL), numeric tokens,
  length, language, and sentiment polarity.
* `news_enricher.py` — `NewsEnricher` DB worker. Reads pending
  rows from `public.news_items` (via the new
  `news_items_pending_enrichment` view), runs the pipeline,
  writes back `sentiment` + `instruments` + `enrichment` jsonb
  + `enriched_at`. Uses `shared.utils.db.get_shared_pool` if
  available, else falls back to `TICKLES_SHARED_DSN`.

**DB migration — `shared/enrichment/migrations/2026_04_19_phase23_enrichment.sql`**

Adds two columns + two indices + one view. Idempotent
(`IF NOT EXISTS` throughout). Rollback is a single `ALTER
TABLE ... DROP COLUMN` pair.

**Operator CLI — `shared/cli/enrichment_cli.py`**

Six subcommands, single-line JSON stdout:

* `stages` — list every registered stage.
* `enrich-text --headline --content` — ad-hoc pipeline run, no
  DB touch. Perfect for tuning the vocab lists.
* `pending-count [--dsn]` — how many rows still need enriching?
* `enrich-batch --limit N` — fetch + enrich + write back.
* `dry-run --limit N` — fetch + enrich, do NOT write.
* `apply-migration` — print the SQL path + a ready-to-paste
  psql command.

**Tests — `shared/tests/test_enrichment.py`**

24 tests covering: schema JSON-roundtrip + empty/full summaries;
each stage (English / non-English / unknown language; pair /
ticker / bare-word / custom-instruments / no-false-positive symbol
resolution; bullish / bearish / neutral / negated sentiment;
high- and low-relevance inputs); `Pipeline` duplicate-name guard,
all-stages happy path, exception swallowing + error capture;
`enrich_text_once` convenience; CLI `stages` / `enrich-text` /
`apply-migration`.

### Success criteria (verification)

1. `pytest shared/tests/test_enrichment.py` — 24/24 green
   locally and on VPS.
2. Regression across Phases 18–23 — 97/97 green.
3. `ruff` + `mypy --ignore-missing-imports
   --explicit-package-bases` clean on all 10 Phase 23 source
   files.
4. `python -m shared.cli.enrichment_cli stages` returns 4
   registered stages in the expected order.
5. `python -m shared.cli.enrichment_cli enrich-text
   --headline "BTC long" --content "BTC/USDT pumping"` returns
   a summary with `BTC/USDT` in symbols and positive sentiment.
6. Migration applies cleanly on VPS (`psql -f
   2026_04_19_phase23_enrichment.sql`) and `\d news_items`
   shows the new `enrichment` + `enriched_at` columns.
7. Existing Phase 13–22 services untouched.

### Rollback

Pure additive. Code rollback = `git revert`. DB rollback:

```sql
BEGIN;
DROP VIEW IF EXISTS public.news_items_pending_enrichment;
DROP INDEX IF EXISTS idx_news_enrichment_gin;
DROP INDEX IF EXISTS idx_news_enriched_at;
ALTER TABLE public.news_items DROP COLUMN IF EXISTS enriched_at;
ALTER TABLE public.news_items DROP COLUMN IF EXISTS enrichment;
COMMIT;
```

No systemd units were added by Phase 23 — the Phase 22
`tickles-service@.service` template can wrap `NewsEnricher`
in a later phase once we decide the cadence.

---

## Phase 24 — Services Catalog

### Purpose

Phase 22 gave us an in-process `SERVICE_REGISTRY` listing every
long-running Tickles service. That's fine for `services_cli list`
but useless for a dashboard, a sibling process, or a human with
`psql` — they can't see what's registered, whether it's enabled on
this VPS, when it last heartbeated, and what systemd thinks of it.

Phase 24 mirrors the registry + runtime observations into
`public.services_catalog` (Postgres `tickles_shared`) so any
consumer — dashboard, alerting, downstream agent — can answer
"what services exist and are they healthy?" with one SQL query.

No behaviour of existing services changes. This is the "catalog"
the Owner Dashboard (Phase 36) is going to render from.

### Built

**DB migration — `shared/services/migrations/2026_04_19_phase24_services_catalog.sql`**

* `public.services_catalog` — one row per service. Columns: static
  descriptor fields (name PK, kind, module, description,
  systemd_unit, enabled_on_vps, has_factory, phase, tags jsonb),
  plus observed state (last_seen_at, last_systemd_state,
  last_systemd_substate, last_systemd_active_enter_ts,
  last_heartbeat_ts, last_heartbeat_severity, metadata jsonb).
  Indices on kind, enabled_on_vps, and last_heartbeat_ts DESC.
* `public.services_catalog_snapshots` — append-only history of
  state transitions so operators can see "active -> failed ->
  active" without `journalctl`.
* `public.services_catalog_current` — view that bakes in a
  `health` column (`healthy` / `warning` / `degraded` / `stale` /
  `no-heartbeat`) based on heartbeat age + severity, ready for
  dashboards.

Fully idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE OR
REPLACE VIEW`). Rollback is `DROP VIEW + DROP TABLE`.

**Module — `shared/services/catalog.py`**

* `ServicesCatalog` — DB-backed mirror of `SERVICE_REGISTRY`
  with four async methods:
    * `sync_registry(pool)` — upsert every descriptor via
      `INSERT ... ON CONFLICT (name) DO UPDATE`.
    * `snapshot_systemd(pool, names=None)` — shell out to
      `systemctl show` per service, parse `ActiveState` /
      `SubState` / `ActiveEnterTimestamp` / `LoadState`, update
      catalog row + append snapshot row.
    * `attach_heartbeats(pool, [HeartbeatMark ...])` — stamp
      `last_heartbeat_ts` / `last_heartbeat_severity` and append
      snapshot rows.
    * `list_services(pool, kind=None)` / `describe_service`.
* `SystemdState` dataclass + `parse_systemctl_show()` pure
  parser (unit-testable with fixture text, no subprocess).
* `extract_heartbeats_from_audit(store, subjects, window_seconds)`
  — reads latest `HEARTBEAT` per service from the Phase 21
  auditor SQLite store.
* `_InMemoryPool` — tiny async pool stub that implements the
  `DatabasePool` contract against Python dicts. Used by tests,
  also available for dry-run tooling.

**Operator CLI — `shared/cli/services_catalog_cli.py`**

Eight subcommands, all single-line JSON stdout:

* `apply-migration` — print SQL path + ready-to-paste psql command.
* `migration-sql` — emit raw SQL to stdout (pipe-friendly).
* `sync` — upsert registry → DB.
* `snapshot [--names a,b,c] [--no-history]` — capture systemd state.
* `attach-heartbeats [--names] [--window-seconds N]` — stamp
  latest auditor heartbeats.
* `list [--kind KIND]` / `describe <name>` — read from DB.
* `refresh` — sync + snapshot + attach-heartbeats in one shot.

Accepts `--dsn` or `TICKLES_SHARED_DSN` for local dev; otherwise
routes through the canonical `shared.utils.db.get_shared_pool()`.

**Tests — `shared/tests/test_services_catalog.py`**

20 tests covering: `systemctl show` parsing (happy / empty /
missing timestamp); `SystemdState.as_update_row` shape; migration
file presence + key DDL tokens; `_normalize_row` JSONB + datetime
coercion; `sync_registry` upserts every descriptor and is
idempotent; `snapshot_systemd` with a stubbed runner, unknown
service skip, `also_append_history=False` path; `attach_heartbeats`
round-trip + empty noop; `list_services` + kind filter;
`describe_service` hit/miss; `extract_heartbeats_from_audit` picks
the newest record per subject from a real SQLite
`AuditStore`; CLI `apply-migration` + `migration-sql` + `--help`
include every subcommand; `describe` against an unreachable DSN
returns clean JSON error.

### Success criteria (verification)

1. `pytest shared/tests/test_services_catalog.py` — 20/20 green
   locally and on VPS.
2. Regression: `pytest shared/tests/test_indicators.py
   shared/tests/test_engines.py shared/tests/test_features.py
   shared/tests/test_auditor.py shared/tests/test_services.py
   shared/tests/test_enrichment.py
   shared/tests/test_services_catalog.py` — 117/117 green.
3. `ruff` + `mypy --ignore-missing-imports
   --explicit-package-bases` clean on the Phase 24 source files.
4. Migration applies cleanly on VPS and
   `\d services_catalog` shows the 17-column table.
5. `python -m shared.cli.services_catalog_cli sync` reports
   `synced >= 9` on VPS.
6. `python -m shared.cli.services_catalog_cli snapshot` returns
   an `active_state` for each Phase 13–22 service that is
   `active` on box.
7. `python -m shared.cli.services_catalog_cli list` reflects what
   `systemctl is-active` reports for each service.
8. Existing Phase 13–23 services untouched.

### Rollback

Pure additive. Code rollback = `git revert`. DB rollback:

```sql
BEGIN;
DROP VIEW  IF EXISTS public.services_catalog_current;
DROP TABLE IF EXISTS public.services_catalog_snapshots;
DROP TABLE IF EXISTS public.services_catalog;
COMMIT;
```

No systemd units were added or modified by Phase 24.

---

## Phase 25 — Banker + Treasury + Capabilities

### What it is (21-year-old explanation)

Phase 25 gives the system a **rule-book, a cash-register, and a
deal-desk** for every trade the platform wants to place.

* **Capabilities** = the rule-book. For each *company / strategy /
  agent / venue* we store exactly what is allowed: max notional USD,
  max leverage, daily-loss cap, open-position cap, allowed /
  blocked venues, symbols, directions, order types, market hours.
  Before *any* trade leaves the box, it is checked against these
  rules. Stored in `public.capabilities`.
* **Banker** = the cash-register. Every time we learn a new account
  balance / equity / margin (from collectors, ccxt, manual input)
  we append an immutable snapshot to `public.banker_balances`. A
  view `public.banker_balances_latest` gives the newest per
  `(company, exchange, account, currency)`. Changes to leverage
  are audited in `public.leverage_history`.
* **Sizer** = the pure math of "how big should this trade be?". A
  single function `size_intent(...)` takes market snapshot +
  account snapshot + strategy config + capabilities and returns a
  deterministic `SizedIntent` (notional, qty, expected entry, fees,
  slippage, spread cost, overnight cost, SL/TP). This function is
  the **same in backtest and live** — it is the mechanical core of
  Rule 1 (backtests ≡ live).
* **Treasury** = the deal-desk that stitches it together.
  `Treasury.evaluate(intent)` runs the capability check, pulls
  the latest banker snapshot, calls the sizer, and then writes one
  row to `public.treasury_decisions` with the verdict, reasons, and
  numbers. Every trade intent from every strategy must pass through
  here first.

### What was built

**DB migration — `shared/trading/migrations/2026_04_19_phase25_banker_treasury.sql`**

Creates four tables + one view in the `tickles_shared` DB:

* `public.capabilities` — one row per `(company, scope_kind,
  scope_id)` with numeric caps, allow/deny lists, order-type list,
  trading-hours JSON, plus metadata.
* `public.banker_balances` — append-only snapshot log. Indexed by
  `(company_id, exchange, account_id_external, currency, ts DESC)`.
* `public.banker_balances_latest` — view of the newest snapshot
  per `(company, exchange, account, currency)`.
* `public.leverage_history` — audit trail of every leverage
  change request, applied value, and reason.
* `public.treasury_decisions` — immutable log of every Treasury
  verdict (approved / rejected), capability ids that matched, the
  final sized notional, and the available capital at the time.

**Core modules — `shared/trading/`**

* `capabilities.py` — `TradeIntent`, `Capability`, `CapabilityCheck`,
  `CapabilityChecker`, `CapabilityStore`, `default_capability`.
  Pure evaluation logic: match scopes, merge policies, apply
  allow/deny/numeric caps, return an auditable `CapabilityCheck`.
* `sizer.py` — `MarketSnapshot`, `AccountSnapshot`,
  `StrategyConfig`, `SizedIntent`, `size_intent(...)`. Deterministic
  function — no I/O, no randomness. Same math in live and backtest.
* `banker.py` — `BalanceSnapshot` model + `Banker` async DB wrapper
  (`record_snapshot`, `record_many`, `latest_snapshot`,
  `latest_for_company`, `list_recent`, `available_capital_usd`,
  `purge_company`).
* `treasury.py` — `TreasuryDecision` + `Treasury` class. Orchestrates
  checker + banker + sizer, persists decisions, also exposes
  `evaluate_pure(...)` for testing without a DB.
* `memory_pool.py` — in-memory duck-typed async pool used by tests
  (`InMemoryTradingPool`).
* `__init__.py` — public exports + `MIGRATION_PATH` +
  `read_migration_sql`.

**Registry**

* `shared/services/registry.py` — new `banker` service descriptor
  (`kind="worker"`, `module="shared.cli.treasury_cli"`,
  `enabled_on_vps=False` until live wiring, `tags={"phase": "25"}`).
  Phase 24 services catalog picks this up automatically.

**CLI — `shared/cli/treasury_cli.py`**

Operator commands:

* `apply-migration` / `migration-sql` — DB migration.
* `caps-list` / `caps-get` / `caps-upsert` / `caps-delete` /
  `caps-seed-default` — manage capabilities.
* `balances-record` / `balances-latest` / `balances-one` /
  `balances-history` — banker snapshots.
* `evaluate` — end-to-end `Treasury.evaluate` on a JSON intent.
* `pure-size` — offline sizing (no DB) for diagnostics.

Routed via `shared/cli/__init__.py`.

**Tests — `shared/tests/test_trading.py`**

36 tests covering: migration file presence + key DDL tokens;
`TradeIntent.hash` determinism; `Capability.applies_to` matching
and validation; `CapabilityChecker` denials (blocked symbol /
venue / direction / order type / notional / leverage / daily loss
/ open positions) and approvals with capped notional; `Sizer`
(requested notional, risk-based, fraction-of-capital, cap by
capabilities, cap by venue min, qty rounding, fees, slippage,
spread cost, SL/TP derivation, determinism); `Banker` record +
latest + history + company latest + `available_capital_usd`;
`CapabilityStore` upsert/list/delete round-trip + idempotency;
`Treasury.evaluate` happy path persists a decision, denied path
persists a rejection with reasons, no-snapshot path rejects,
`persist=False` dry-run; CLI smoke on `migration-sql` and
`--help`; and a registry check that the `banker` service is
registered.

### Success criteria (verification)

1. `pytest shared/tests/test_trading.py` — 36/36 green.
2. Full regression: `pytest shared/tests` — 272/272 green.
3. `ruff check` and `mypy --ignore-missing-imports
   --explicit-package-bases` clean on all Phase 25 source files.
4. Migration applies cleanly on VPS; `\d capabilities`,
   `\d banker_balances`, `\d treasury_decisions`,
   `\d leverage_history` all present; view
   `banker_balances_latest` resolves.
5. `python -m shared.cli.treasury_cli caps-seed-default
   --company-id tickles` creates a row visible via
   `caps-list --company-id tickles`.
6. `python -m shared.cli.treasury_cli balances-record ...` followed
   by `balances-latest` shows the newest snapshot.
7. `python -m shared.cli.treasury_cli evaluate ...` returns a
   JSON verdict and writes one row to `treasury_decisions`.
8. Phase 24 services catalog sees the `banker` service after `sync`.
9. All prior phases (13–24) untouched — no service or DB regression.

### Rollback

Pure additive. Code rollback = `git revert`. DB rollback:

```sql
BEGIN;
DROP TABLE IF EXISTS public.treasury_decisions;
DROP TABLE IF EXISTS public.leverage_history;
DROP VIEW  IF EXISTS public.banker_balances_latest;
DROP TABLE IF EXISTS public.banker_balances;
DROP TABLE IF EXISTS public.capabilities;
COMMIT;
```

No systemd units were added or modified by Phase 25; the `banker`
service is registered but `enabled_on_vps=False` until Phase 26
wires live balance collection.

---

## Phase 26 — Execution Layer (paper / ccxt / nautilus)

### What it is (21-year-old explanation)

Phase 25 gave the system a deal-desk that said "yes, place 0.014 BTC
long on Bybit". Phase 26 is what **actually places the order**.

We never want the rest of the stack to care whether an order is a
paper trade (for forward-testing), a real ccxt order, or a high-speed
NautilusTrader order. So Phase 26 introduces an abstraction:

1. **ExecutionAdapter** protocol — tiny async interface with
   `submit`, `cancel`, `poll_updates`.
2. **ExecutionRouter** — the single entry point every strategy /
   agent / CLI calls. It picks an adapter, persists everything to the
   DB, and returns an `OrderSnapshot`.
3. **Three built-in adapters**:
   * `paper` — deterministic in-memory fills (uses MarketTick input,
     respects slippage / maker-taker fees). Same math as backtest —
     critical for Rule 1 parity.
   * `ccxt` — thin wrapper over the CCXT sync client running in a
     worker thread via `asyncio.to_thread`. Works with every venue
     we already collect from.
   * `nautilus` — scaffolded stub. Returns a clean `STATUS_REJECTED`
     update until NautilusTrader is actually wired (keeps the
     catalog / CLI / router symmetric without forcing a heavyweight
     dep on operators).

Everything the adapter emits lands in Postgres (`tickles_shared`):
orders, order events, fills, and position snapshots.

### What was built

**DB migration — `shared/execution/migrations/2026_04_19_phase26_execution.sql`**

* `public.orders` — one row per submitted ExecutionIntent with the
  adapter's best-known status. `UNIQUE (adapter, client_order_id)`
  is the idempotency key for retries.
* `public.order_events` — append-only log of every adapter update
  (submitted → accepted → fill → cancel / reject).
* `public.fills` — immutable record of every filled slice with
  price, quantity, fee, liquidity.
* `public.position_snapshots` — append-only snapshots keyed by
  `(company, adapter, exchange, account, symbol)`.
* `public.positions_current` — `DISTINCT ON (...)` view that returns
  the newest position snapshot per key.

**Core modules — `shared/execution/`**

* `protocol.py` — `ExecutionAdapter` protocol, `ExecutionIntent`,
  `OrderUpdate`, `OrderSnapshot`, `MarketTick`, plus all status /
  event / order-type constants. `ensure_client_order_id(...)`
  mixes a process-local counter and `id(self)` so two intents
  constructed in the same nanosecond still produce distinct IDs.
* `paper.py` — `PaperExecutionAdapter` with deterministic
  market / limit / stop / stop-limit fill semantics and fee model.
  `touch(tick)` lets forward-testing drivers replay ticks against
  resting limit/stop orders.
* `ccxt_adapter.py` — `CcxtExecutionAdapter`. Submit translates into
  `client.create_order(...)` on a threadpool; poll_updates calls
  `fetch_order`. Defaults to sandbox mode.
* `nautilus_adapter.py` — safe stub; returns rejection updates with
  a clear message pointing to the install instructions.
* `store.py` — `ExecutionStore` (async DB wrapper) with all DDL-free
  SQL statements, plus `FillRow` and `PositionSnapshotRow`
  dataclasses.
* `router.py` — `ExecutionRouter` owns persistence; idempotent on
  `(adapter, client_order_id)`; maintains average fill price and
  cumulative fees as fills come in; writes position snapshots
  automatically on every fill.
* `memory_pool.py` — `InMemoryExecutionPool` duck-typing
  `DatabasePool` for tests and CLI `--in-memory` flag.
* `__init__.py` — public exports + `default_adapters()` factory +
  `MIGRATION_PATH` + `read_migration_sql`.

**Registry**

* `shared/services/registry.py` — registers new `executor` service
  (`kind="worker"`, `module="shared.cli.execution_cli"`,
  `enabled_on_vps=False`, `tags={"phase": "26"}`).
  Services catalog (Phase 24) picks this up automatically.

**CLI — `shared/cli/execution_cli.py`**

* `apply-migration` / `migration-sql` — DB migration helpers.
* `adapters` — list the adapter names + availability.
* `submit` — submit an ExecutionIntent via any adapter (paper by
  default). Accepts `--market-last/bid/ask` for paper fills.
* `cancel` — cancel a known client_order_id via the given adapter.
* `orders` / `fills` / `positions` — query views for a company.
* `paper-simulate` — fully offline submit → tick → fill round-trip
  using the in-memory pool; handy for docs and tests.

Routed via `shared/cli/__init__.py`.

**Tests — `shared/tests/test_execution.py`**

30 tests covering: migration file presence + key DDL tokens;
client_order_id determinism / uniqueness; PaperExecutionAdapter
market long/short at ask/bid, reject without tick, slippage math,
reject zero qty, limit-accept-then-fill-on-touch, limit immediate
fill when market already crossed, stop trigger on last-breach,
cancel idempotency + unknown rejection, submit idempotency;
NautilusExecutionAdapter stub rejection; ExecutionRouter paper
persistence, cancel path, idempotency, rejection event logging,
open-orders filter, latest-position view, fill ordering,
unknown-cancel raises; `default_adapters` returns all three;
`executor` service registration; CLI smokes on `migration-sql`,
`adapters`, `--help`, and full `paper-simulate` round-trip.

### Success criteria (verification)

1. `pytest shared/tests/test_execution.py` — 30/30 green.
2. Full regression: `pytest shared/tests` — 302/302 green.
3. `ruff` and `mypy --ignore-missing-imports
   --explicit-package-bases` clean on all Phase 26 source files.
4. Migration applies cleanly on VPS; `\d orders`, `\d fills`,
   `\d order_events`, `\d position_snapshots`, and
   `\d positions_current` all present.
5. `python -m shared.cli.execution_cli adapters` returns
   `paper / ccxt / nautilus` on VPS.
6. `python -m shared.cli.execution_cli paper-simulate ...` returns
   `order.status=filled` and at least one fill / position row.
7. Services catalog (Phase 24) sees the `executor` service after
   `services_catalog_cli sync`.
8. All Phase 13-25 services untouched.

### Rollback

Pure additive. Code rollback = `git revert`. DB rollback:

```sql
BEGIN;
DROP VIEW  IF EXISTS public.positions_current;
DROP TABLE IF EXISTS public.position_snapshots;
DROP TABLE IF EXISTS public.fills;
DROP TABLE IF EXISTS public.order_events;
DROP TABLE IF EXISTS public.orders;
COMMIT;
```

The `nautilus` adapter is wired but defaults to safe rejection; no
real nautilus dependency is introduced. The `ccxt` adapter re-uses
the already-installed `ccxt` package; no new dependency either.

---

## Phase 27 — Regime Service

### Purpose

Downstream strategies, agents, and the Treasury need a stable
*label* for the current market environment. Should I go long at
full size or should I tighten stops? Am I in a crash or a
grind-up? Phase 27 answers this deterministically.

The Regime Service looks at recent OHLCV candles for a
`(universe, exchange, symbol, timeframe)` tuple, classifies the
environment (bull / bear / sideways / crash / recovery / high_vol
/ low_vol), and records the result as an append-only
`regime_states` row. Everything else reads the `regime_current`
view.

### What was built

1. **DB migration** —
   `shared/regime/migrations/2026_04_19_phase27_regime.sql`
   * `public.regime_config`   — per universe/symbol classifier wiring
   * `public.regime_states`   — append-only classifier output
   * `public.regime_current`  — `DISTINCT ON` latest row per key
2. **Classifiers** (pure Python, no numpy/pandas) —
   * `shared/regime/classifiers/trend.py` — fast/slow SMA + slope.
   * `shared/regime/classifiers/volatility.py` — stdev of log returns
     + drawdown.
   * `shared/regime/classifiers/composite.py` — combines trend and
     volatility and adds a crash/recovery gate via drawdown.
3. **Service layer** —
   `shared/regime/service.py` wires classifiers to the store and
   supports ad-hoc classification, periodic `tick()` across
   enabled configs, and read helpers (`current`, `history`).
4. **Store + in-memory pool** —
   `shared/regime/store.py` (DB wrapper) +
   `shared/regime/memory_pool.py` (test double that exposes the
   same async contract the store expects).
5. **CLI** — `python -m shared.cli.regime_cli`
   * `apply-migration` / `migration-sql`
   * `classifiers`
   * `config-set` / `config-list`
   * `classify` (synthetic closes via `--closes` or `--closes-file`)
   * `current` / `history`
6. **Service registry** — `regime` entry (worker, phase 27,
   `enabled_on_vps=False` until a universe config is seeded in
   Phase 32).
7. **Tests** — `shared/tests/test_regime.py` (30 tests): migration
   shape, classifier behaviour on synthetic series, store
   round-trip, service tick, registry, CLI smoke.

### Success criteria

* 30/30 tests green.
* `ruff` + `mypy` clean on all new files.
* Migration applies cleanly on the VPS into `tickles_shared`.
* `regime_cli classify` produces a label for synthetic inputs.
* `regime` service visible in `services_catalog` after sync.

### Rollback

Pure additive.

```sql
BEGIN;
DROP VIEW  IF EXISTS public.regime_current;
DROP TABLE IF EXISTS public.regime_states;
DROP TABLE IF EXISTS public.regime_config;
COMMIT;
```

Then `git revert` the Phase 27 commit. The service is
`enabled_on_vps=False`, so no systemd units need to be stopped.

---

## Phase 28 - Crash Protection

### Purpose

Rule 1 says "backtests must equal live". Rule 0 says "never blow up the
account". Phase 28 is the cross-cutting **safety layer** that sits between
the Regime Service, Banker / Treasury (Phase 25), and the Execution Layer
(Phase 26), and kills / halts / alerts on dangerous conditions **before**
new orders are placed.

Concretely, a Protection Rule says things like:

* "If the composite regime for `universe=crypto` is `crash` or `high_vol`
  on any watched symbol, **halt all new orders** for `tickles`."
* "If equity drawdown from peak exceeds **15%**, halt new orders."
* "If realised daily loss is more than **5%** of start-of-day equity,
  flatten positions."
* "If notional exposure on `BTC/USDT` exceeds **$500k**, halt new BTC
  orders."
* "If the last market tick is older than **10 minutes**, alert."

Rules are persisted, events are append-only, and the latest event per
scope drives whether the Execution Layer blocks an intent.

### Built

1. **Migration** `shared/guardrails/migrations/2026_04_19_phase28_crash.sql`
   - `public.crash_protection_rules` (company/universe/exchange/symbol
     scope, rule_type, action, threshold, params jsonb, severity, enabled).
   - `public.crash_protection_events` (append-only rule evaluations with
     status in `{triggered, resolved, overridden}`).
   - `public.crash_protection_active` view: latest event per
     (rule_id, company, universe, exchange, symbol).
2. **Protocol** `shared/guardrails/protocol.py`
   - `ProtectionRule`, `ProtectionSnapshot` (inputs: equity, positions,
     regimes, staleness), `ProtectionDecision` (output: triggered /
     resolved + metric + reason), and enum-like string constants.
3. **Evaluator** `shared/guardrails/evaluator.py`
   - Pure function `evaluate(rules, snapshot)` returning decisions. Handles
     `regime_crash`, `equity_drawdown`, `daily_loss`, `position_notional`,
     and `stale_data`.
   - `decisions_block_intent(decisions, scope)` helper for the Execution
     Layer: returns only decisions whose action halts new orders.
4. **Store** `shared/guardrails/store.py` (rules CRUD, events insert,
   active view) + `InMemoryGuardrailsPool` for offline tests.
5. **Service** `shared/guardrails/service.py`
   - `GuardrailsService.tick(snapshot)` evaluates + persists decisions.
   - `is_intent_blocked(scope)` used by the Execution Router as a
     pre-flight gate.
6. **CLI** `shared/cli/guardrails_cli.py` with subcommands
   `apply-migration`, `migration-sql`, `rule-add`, `rule-list`,
   `rule-toggle`, `evaluate`, `events`, `active`, `check-intent`.
7. **Service registry** - `crash-protection` entry (worker, phase 28,
   `enabled_on_vps=False` until Phase 32 seeds rules).
8. **Tests** `shared/tests/test_guardrails.py` - 28 tests covering
   migration, evaluator per rule-type, store, service, registry, CLI.

### Success criteria

* All 28 Phase 28 tests green locally (`pytest shared/tests/test_guardrails.py`).
* `ruff` and `mypy` clean on `shared/guardrails/`.
* Migration applies cleanly on VPS `tickles_shared`.
* `guardrails_cli rule-list --in-memory` returns `{"ok": true, "count": 0}`.
* `crash-protection` visible in `services_catalog` after sync.
* No regression in 332+ existing tests.

### Rollback

Pure additive.

```sql
BEGIN;
DROP VIEW  IF EXISTS public.crash_protection_active;
DROP TABLE IF EXISTS public.crash_protection_events;
DROP TABLE IF EXISTS public.crash_protection_rules;
COMMIT;
```

Then `git revert` the Phase 28 commit. The service is
`enabled_on_vps=False`, so no systemd units need to be stopped.

---

## Phase 29 - Alt-Data Ingestion

### Purpose

Price is one input. **Alt-data** - funding rates, open interest, social
sentiment, on-chain flows, macro releases - is how strategies find
edges the average trader misses. Phase 29 builds the uniform landing
surface for all of this: a single table, a single source protocol, and
a single ingestor service.

### Built

1. **Migration** `shared/altdata/migrations/2026_04_19_phase29_altdata.sql`
   - `public.alt_data_items` with ``UNIQUE(source, provider, scope_key,
     metric, as_of)`` so re-runs never double-insert.
   - `public.alt_data_latest` view (latest row per
     `(scope_key, metric)`).
2. **Protocol** `shared/altdata/protocol.py` (`AltDataItem`,
   `AltDataSource`, canonical source/metric constants).
3. **Built-in sources** `shared/altdata/sources/`
   - `StaticAltDataSource` — fixed list, used by tests.
   - `ManualAltDataSource` — push + drain queue, used by CLI/API.
   - `CcxtFundingRateSource` — async wrapper for `ccxt.fetch_funding_rate`
     across a symbol list.
   - `CcxtOpenInterestSource` — emits both contracts and USD metrics
     from `ccxt.fetch_open_interest`.
4. **Store** `shared/altdata/store.py` — `AltDataStore` with
   `insert_item` (`ON CONFLICT DO NOTHING`), `list_items`, `list_latest`.
   `InMemoryAltDataPool` in `memory_pool.py` for offline testing.
5. **Ingestor** `shared/altdata/service.py` — `AltDataIngestor.tick()`
   fans out across sources, normalises naive datetimes to UTC, and
   returns an `IngestReport` (attempted / inserted / skipped per
   source, plus errors).
6. **CLI** `shared/cli/altdata_cli.py` with `apply-migration`,
   `migration-sql`, `sources`, `push`, `ingest`, `latest`, `items`.
7. **Service registry** - `altdata-ingestor` entry (worker, phase 29,
   `enabled_on_vps=False` until sources are seeded in Phase 32).
8. **Tests** `shared/tests/test_altdata.py` — 21 tests covering
   migration, store dedupe, latest view, ingestor, CCXT source
   adapters (with fake exchanges), CLI smoke, and registry.

### Success criteria

* All 21 Phase 29 tests green locally.
* `ruff` and `mypy` clean.
* Migration applies cleanly on VPS `tickles_shared`.
* `alt_data_items` + `alt_data_latest` visible.
* `altdata-ingestor` visible in `services_catalog`.
* No regression in 360+ existing tests.

### Rollback

Pure additive.

```sql
BEGIN;
DROP VIEW  IF EXISTS public.alt_data_latest;
DROP TABLE IF EXISTS public.alt_data_items;
COMMIT;
```

Then `git revert` the Phase 29 commit. The ingestor service is
`enabled_on_vps=False`, so no systemd units need to be stopped.

---

## Phase 30 - Events Calendar + windows

### Purpose

Some price moves are not market moves — they're **events**. CPI prints,
NFP, FOMC, earnings, exchange maintenance, funding rollovers, halvings.
Strategies and guardrails need a machine-readable calendar so they can
widen spreads, pause new entries, or fire extra protection rules while
a window is open. Phase 30 builds that calendar.

### Built

1. **Migration** `shared/events/migrations/2026_04_19_phase30_events.sql`
   - `public.events_calendar` (kind, provider, name, event_time,
     window_before / after minutes, scope: universe / exchange /
     symbol / country, importance 1-3, payload/metadata JSONB).
     Uniqueness on ``(provider, dedupe_key)`` for idempotent upserts.
   - `public.events_active` view: rows whose window contains ``NOW()``.
   - `public.events_upcoming` view: future events ordered by time.
2. **Protocol** `shared/events/protocol.py`
   - `EventRecord`, `EventWindow` dataclasses; canonical `KIND_*` and
     `IMPORTANCE_*` constants; `build_dedupe_key()` for stable upserts.
3. **Store** `shared/events/store.py`
   - `EventsStore.upsert_event` (`ON CONFLICT DO UPDATE` on mutable
     fields), `delete_event`, `list_events` (kind / provider / scope /
     importance / time range), `list_active`, `list_upcoming`.
4. **Service** `shared/events/service.py`
   - `EventsCalendarService` with `upsert` / `upsert_many` /
     `active_at` / `upcoming` / `active_windows` / `any_active` (scope
     + importance filter — used by Guardrails/Treasury as a gate).
5. **In-memory pool** `shared/events/memory_pool.py` for offline tests.
6. **CLI** `shared/cli/events_cli.py` (`apply-migration`, `migration-sql`,
   `kinds`, `add`, `list`, `active`, `upcoming`, `delete`).
7. **Service registry** — `events-calendar` entry (worker, phase 30,
   `enabled_on_vps=False` until a loader is wired in Phase 32).
8. **Tests** `shared/tests/test_events.py` — 20 tests.

### Success criteria

* All 20 Phase 30 tests green locally.
* `ruff` and `mypy` clean.
* Migration applies cleanly on VPS `tickles_shared`.
* `events_calendar`, `events_active`, `events_upcoming` visible.
* `events-calendar` visible in `services_catalog`.
* No regression.

### Rollback

```sql
BEGIN;
DROP VIEW  IF EXISTS public.events_upcoming;
DROP VIEW  IF EXISTS public.events_active;
DROP TABLE IF EXISTS public.events_calendar;
COMMIT;
```

Then `git revert` the Phase 30 commit.

---

## Phase 31 — Apex / Quant / Ledger modernised souls (2026-04-19)

### Purpose (21-year-old edition)

"Souls" are our **decision-making agents**. The platform already has
the plumbing — market data, features, backtests, treasury, regime,
guardrails, events — but somebody has to _reason_ over all of it and
say "yes / no / wait". That somebody is a soul. In this phase we build
three modernised souls, wire them to a single audit table, and make
their verdicts reproducible.

| Soul   | Role          | What it does                                                                                  |
|--------|---------------|-----------------------------------------------------------------------------------------------|
| Apex   | Decision      | Aggregates guardrails + active events + regime + treasury + proposal score → approve/reject/defer |
| Quant  | Research      | Looks at regime/trend/volatility/funding and proposes a hypothesis (direction + size bucket + invalidation) |
| Ledger | Bookkeeper    | Takes recent fills + open positions and writes a structured journal (never trades)            |

All three souls are **deterministic by default**. No LLM is required
for Phase 31 — same input JSON yields the same verdict every time,
which is exactly what Rule 1 (backtests == live) needs. Later phases
will plug OpenClaw LLM adapters in via the same `SoulPersona.default_llm`
column.

### Built in this phase

* DB migration `shared/souls/migrations/2026_04_19_phase31_souls.sql`
  creates three tables + one view under `tickles_shared`:
  * `public.agent_personas` — stable identity for each soul.
  * `public.agent_prompts`  — versioned prompt templates (`UNIQUE(persona_id, version)`).
  * `public.agent_decisions` — append-only audit log of every verdict.
  * `public.agent_decisions_latest` — `DISTINCT ON (persona_id, correlation_id)` view.
* `shared/souls/protocol.py` — `SoulPersona`, `SoulPrompt`, `SoulContext`,
  `SoulDecision` dataclasses + canonical constants (`SOUL_APEX`,
  `SOUL_QUANT`, `SOUL_LEDGER`, verdict + role + mode sets).
* `shared/souls/personas/`
  * `apex.py` — `ApexSoul.decide()`. Order of precedence:
    guardrails_blockers → high-importance active event → crash regime →
    treasury rejection → bull/bear/sideways scoring + proposal_score.
  * `quant.py` — `QuantSoul.decide()`. Scores trend + regime + funding,
    scales conviction in high-vol, emits `propose` or `observe`.
  * `ledger.py` — `LedgerSoul.decide()`. Summarises fills → fills count,
    gross notional, fees, realised pnl, open notional, and a
    by-symbol breakdown. Always verdict=`journal`.
* `shared/souls/store.py` — `SoulsStore` with
  `upsert_persona`, `add_prompt`, `list_personas`, `get_persona`,
  `list_prompts`, `record_decision`, `list_decisions`,
  `list_latest_per_correlation`.
* `shared/souls/memory_pool.py` — `InMemorySoulsPool` for offline
  tests. Mirrors the PostgreSQL view semantics (latest-per-correlation
  with id as tiebreak for identical timestamps, same pattern we used
  in Phase 28/25 in-memory pools).
* `shared/souls/service.py` — `SoulsService` orchestrator with
  `seed_personas`, `run_apex`, `run_quant`, `run_ledger`,
  `decisions`, `latest_decisions`. Each run persists the decision to
  `agent_decisions` unless the caller passes `persist=False`.
* `shared/cli/souls_cli.py` — operator CLI:
  `apply-migration`, `migration-sql`, `seed-personas`, `personas`,
  `prompts-add`, `prompts`, `run-apex`, `run-quant`, `run-ledger`,
  `decisions`, `latest`. Accepts `--fields '{...}'` or
  `--fields @path.json` so CI and operators can replay the exact same
  context a soul saw.
* `shared/services/registry.py` — registers a new `souls` service
  (`kind=worker`, phase tag `31`, `enabled_on_vps=False`). Operators
  see it alongside regime / guardrails / altdata / events.
* `shared/tests/test_souls.py` — 32 tests covering migration, each
  persona's decision logic, deterministic replay, store CRUD, service
  end-to-end, registry entry, and CLI smoke tests.

### Incidental fix: banker in-memory pool tiebreak

The Phase 25 `InMemoryTradingPool` picked the `max` balance snapshot
by `ts` only. On Windows, `datetime.now()` can return identical values
for two rapid calls, which made `test_banker_record_and_latest`
non-deterministic (observed during the Phase 31 regression). Added
`(ts, id)` as the sort/`max` key in `banker_balances_latest` fetch
helpers — same pattern used in Phase 28/31 in-memory pools — to keep
latest-snapshot semantics correct when timestamps tie.

### How to run locally

```powershell
python -m shared.cli.souls_cli apply-migration --path-only
python -m shared.cli.souls_cli seed-personas --in-memory
python -m shared.cli.souls_cli run-apex --in-memory --correlation-id demo `
  --fields "{\"regime\":\"bull\",\"treasury_decision\":{\"approved\":true},\"proposal_score\":0.4}"
python -m shared.cli.souls_cli latest --in-memory
```

### Success criteria

* ✓ All 32 Phase 31 tests green locally (433 total regression).
* ✓ `ruff` + `mypy` clean on `shared/souls`, `shared/cli/souls_cli.py`,
  `shared/tests/test_souls.py`.
* ✓ `souls` service visible in `ServiceRegistry.list_services()`.
* ✓ Apex/Quant/Ledger verdicts are byte-deterministic for identical
  inputs (Rule 1).
* ✓ `agent_decisions_latest` view returns exactly one row per
  `(persona_id, correlation_id)` with the correct latest verdict.

### Rollback

```sql
BEGIN;
DROP VIEW  IF EXISTS public.agent_decisions_latest;
DROP TABLE IF EXISTS public.agent_decisions;
DROP TABLE IF EXISTS public.agent_prompts;
DROP TABLE IF EXISTS public.agent_personas;
COMMIT;
```

Then `git revert` the Phase 31 commit. The rest of the system does
not yet consume soul verdicts (that lands in Phase 32+), so rollback
is safe.

---

## Phase 32 — Scout / Curiosity / Optimiser / RegimeWatcher (2026-04-19)

### Purpose

Phase 32 adds four more deterministic "soul" personas on top of the
Phase 31 framework. Together they give the system the ability to
**expand** (Scout finds new symbols), **explore** (Curiosity picks
which experiments to try next), **tune** (Optimiser proposes parameter
sweeps), and **watch** (RegimeWatcher screams when market conditions
change). They all share the Phase 31 `agent_personas` /
`agent_decisions` tables, and add three helper tables for their
specific outputs.

### What we built

**Migration** — `shared/souls/migrations/2026_04_19_phase32_scout.sql`:

* `public.scout_candidates` — proposed new symbols; deduped by
  `UNIQUE (exchange, symbol, COALESCE(universe,''), COALESCE(company_id,''))`
  with `status` (`proposed`/`accepted`/`rejected`).
* `public.optimiser_candidates` — parameter trials per strategy, with
  `score` and `status` (`pending`/`running`/`done`/`failed`).
* `public.regime_transitions` — durable history of regime changes keyed
  by `(exchange, symbol, timeframe, transitioned_at)`.

All tables have indexes matching the dominant query patterns and a
single `BEGIN/COMMIT` rollback block in the SQL footer.

**Protocol extensions** — `shared/souls/protocol.py`:

* New canonical names `SOUL_SCOUT`, `SOUL_CURIOSITY`, `SOUL_OPTIMISER`,
  `SOUL_REGIME_WATCHER`.
* New roles `ROLE_SCOUT`, `ROLE_EXPLORER`, `ROLE_OPTIMISER`,
  `ROLE_REGIME_WATCHER`.
* New verdicts `VERDICT_EXPLORE` (curiosity), `VERDICT_ALERT`
  (regime-watcher). `SOUL_NAMES` and `VERDICTS` constants updated.

**Personas** — `shared/souls/personas/`:

* `scout.py::ScoutSoul` — normalises volume / volatility / mentions /
  funding for each candidate symbol, drops anything already in
  `existing_symbols`, sorts deterministically by `(-score, symbol)`,
  and emits `propose` if anything beats `min_score` else `observe`.
* `curiosity.py::CuriositySoul` — UCB1-ish score
  `0.7·novelty + 0.2·Laplace(hit_rate) + 0.1·prior`. Emits `explore`
  with the top picks above `novelty_floor`, else `observe`.
* `optimiser.py::OptimiserSoul` — first walks the sorted grid of
  `space` looking for the first untried combo. If every combo is
  tried, falls back to a step-±1 neighbour of the best-scoring
  history entry. Emits `propose` with `params` or `observe` when
  the budget is exhausted.
* `regime_watcher.py::RegimeWatcherSoul` — chronologically walks
  observations, records each regime change, and emits `alert` on the
  latest transition with severity `high` (crash), `medium` (bear),
  else `low`.

**Stores** — `shared/souls/store_phase32.py`:

* `ScoutCandidate`, `ScoutStore.upsert/list` — idempotent upsert
  (same row on conflict), list filter on `status`.
* `OptimiserCandidate`, `OptimiserStore.insert/list` — insert + filter
  on `strategy` / `status`.
* `RegimeTransition`, `RegimeTransitionStore.insert/list` — insert +
  filter on `exchange` / `symbol`, sorted by `transitioned_at DESC`.

**In-memory pool** — `shared/souls/memory_pool.py` was extended to
understand all three new tables with the same deduplication and sort
semantics as PostgreSQL (keyed by the composite scout uniqueness
tuple, `created_at DESC` for optimiser/scout, `transitioned_at DESC`
for regime transitions). This is what lets every test run without a
live database.

**Service wiring** — `shared/souls/service.py`:

* `SoulsConfig.enabled_personas` now lists all seven souls.
* `seed_personas()` upserts seven personas (idempotent).
* Four new methods `run_scout`, `run_curiosity`, `run_optimiser`,
  `run_regime_watcher` — each one calls the corresponding soul's
  deterministic `decide`, optionally persists to `agent_decisions`,
  and returns the dataclass decision.

**CLI** — `shared/cli/souls_cli.py`:

* `apply-migration` / `migration-sql` gain `--phase {31,32}` so
  operators can print either migration.
* Four new subcommands `run-scout`, `run-curiosity`, `run-optimiser`,
  `run-regime-watcher` mirroring the existing `run-apex/quant/ledger`
  plumbing (`--in-memory`, `--fields @file.json`, `--no-persist`).

**Registry** — `shared/services/registry.py`: the `souls` service
description was updated to mention all seven personas, and its tag is
now `phase = "31-32"`. It remains `enabled_on_vps=False` until Phase 34
(Strategy Composer) wires verdicts into the execution flow.

### Success criteria

* ✓ Migration SQL idempotent (uses `IF NOT EXISTS`); rollback block
  present at the bottom of the file.
* ✓ All four souls are pure functions — same fields ⇒ same verdict,
  bit-for-bit (tested via `_deterministic` checks for Scout and
  verified by identical dict comparison across two `decide` calls).
* ✓ Verdicts persist to `agent_decisions` through the existing
  Phase 31 store — Phase 32 does **not** add a second verdict table.
* ✓ Helper tables are *optional* sidecars — the souls run fine without
  touching them (Phase 32 store calls only happen when the strategy
  composer in a later phase decides to materialise the outputs).
* ✓ Full regression passes locally: `pytest shared/tests → 459 passed`
  (58 souls tests: 32 Phase 31 + 26 Phase 32).
* ✓ `ruff` and `mypy --explicit-package-bases` pass on all new files.
* ✓ No previously-green test breaks; `test_souls_service_is_registered`
  was updated to accept either phase tag (`31` or `31-32`).

### Rollback

```sql
BEGIN;
DROP TABLE IF EXISTS public.regime_transitions;
DROP TABLE IF EXISTS public.optimiser_candidates;
DROP TABLE IF EXISTS public.scout_candidates;
COMMIT;
```

Then `git revert` the Phase 32 commit. Phase 31 tables are untouched,
so Apex/Quant/Ledger continue to function exactly as before. The
Phase 32 souls are wired as optional — if you revert the commit the
service registry will drop back to the three-persona description and
the CLI loses its four new subcommands, but no data or downstream
feature is lost.

---

## Phase 33 — Arb + Copy-Trader

### Why this phase exists

Every strategy so far has been either an indicator- or agent-driven
"alpha" (Phases 18, 31–32). Phase 33 adds two **market-structure**
strategies that generate their own alpha without needing a price
prediction:

1. **Arbitrage scanner** — watches top-of-book quotes across N venues
   and emits opportunities when the best bid on one venue exceeds the
   best ask on another by more than ``min_net_bps`` **after fees**.
   Think "buy BTC at $75 410 on binance, simultaneously sell at
   $75 421 on coinbase, pocket the gap". In liquid majors the net
   gap after 36+ bps of fees is usually negative; in alts and
   thinner books it can be meaningful.

2. **Copy-trader** — watches a registered *source* (another account,
   a public wallet, a signal feed) for new fills and mirrors them
   onto our own book with a per-source sizing rule (ratio / fixed
   notional USD / replicate) plus whitelist/blacklist and a
   max-notional cap.

Both systems only *detect and record* opportunities in this phase.
They don't place orders — that's wired in Phase 34 when the strategy
composer promotes opportunities to intents and routes them through
the Phase 26 ExecutionRouter (which itself runs through Treasury,
Guardrails, and the Rule-1 auditor).

### What got built

**Migrations** — two new SQL files, both idempotent with explicit
rollback:

* `shared/arb/migrations/2026_04_19_phase33_arb.sql` — `public.arb_venues`
  (with a `CREATE UNIQUE INDEX` over `(name, kind, COALESCE(company_id,''))`)
  and `public.arb_opportunities` (append-only, with `CHECK (net_bps >= 0)`).
* `shared/copy/migrations/2026_04_19_phase33_copy.sql` — `public.copy_sources`
  (same coalesce-index pattern for unique tuples) and `public.copy_trades`
  with a plain `UNIQUE (source_id, source_fill_id)` so re-polling a
  source is naturally idempotent.

**Arb module** — `shared/arb/`:

* `protocol.py` — `ArbVenue`, `ArbQuote`, `ArbOpportunity` dataclasses.
* `fetchers.py` — `OfflineQuoteFetcher` (dict stub for tests), `CcxtQuoteFetcher`
  (live public tickers via `ccxt.async_support`, venue failures are
  silently dropped per scan — the scanner continues with whatever
  venues succeeded).
* `scanner.py` — pure deterministic evaluator. For each ordered
  `(buy_venue, sell_venue)` pair where `buy != sell`: compute
  `gross_bps = (sell_bid − buy_ask) / buy_ask × 10 000`, subtract
  fees (both takers by default), and emit an opportunity if
  `net_bps ≥ min_net_bps`. `size_base` is capped by the shallower
  side of the book **and** by `max_size_usd`.
* `store.py` — async wrapper around the two tables: `upsert_venue`,
  `list_venues`, `record_opportunity`, `list_opportunities`.
* `memory_pool.py` — in-memory pool that mirrors PostgreSQL's
  semantics for tests.
* `service.py` — `ArbService.scan_symbols()` runs one tick across
  N symbols, sorts opportunities by `(-net_bps, symbol)`, and
  optionally persists.

**Copy module** — `shared/copy/`:

* `protocol.py` — `CopySource`, `SourceFill`, `CopyTrade` dataclasses
  + constants for source kinds (`ccxt_account` / `wallet` / `feed` /
  `static`) and sizing modes.
* `mapper.py` — `CopyMapper.map(source, fill)` → `MappingResult`.
  Enforces `enabled`, whitelist / blacklist, positive price/qty,
  sizing mode, and max-notional cap. When a fill is skipped, the
  mapper still emits a `CopyTrade(status='skipped', skip_reason=…)`
  so the audit trail captures *why* the fill was ignored.
* `sources.py` — `BaseCopySource` protocol, `StaticCopySource` (list),
  `CcxtCopySource` (wraps `fetch_my_trades` — requires API keys, so
  returns `[]` gracefully if no exchange is configured; the public
  `demo` subcommand uses a different, public-tape-based path).
* `store.py` — `upsert_source`, `list_sources`, `get_source`,
  `touch_source`, `record_trade` (idempotent on
  `(source_id, source_fill_id)`), `list_trades`.
* `memory_pool.py` — in-memory pool for tests.
* `service.py` — `CopyService.tick_one(source)` fetches fills from
  the source since its `last_checked_at`, maps each one, persists
  the result, and touches the watermark so the next tick doesn't
  re-process the same fills.

**CLIs** — `shared/cli/arb_cli.py` and `shared/cli/copy_cli.py`:

* Shared pattern with `souls_cli`: `apply-migration` / `migration-sql`
  for DB bootstrap, `venue-add`/`venues` (arb) and `source-add`/
  `sources` (copy) for config, `scan`/`tick`/`opportunities`/`trades`
  for runtime queries.
* `arb_cli demo` — human-friendly live table. Fetches public tickers
  from binance/kraken/coinbase/bybit for the requested symbols,
  prints each venue's bid/ask with a `*` on the best side, shows the
  gross `gap-vs-best-ask` in bps, then runs the scanner deterministically
  over the same snapshot and prints any opportunities. Adaptive
  decimal precision handles tiny-price coins (PEPE, SHIB, DOGE).
* `copy_cli demo` — live copy-trader illustration. Pulls Binance's
  public trade tape for a symbol (`fetch_trades`), treats each
  anonymous public trade as if it came from a registered leader,
  runs `CopyService.tick_one()` with a configurable sizing rule
  (`--size-mode ratio|fixed_notional_usd|replicate`,
  `--size-value`, `--max-notional-usd`), and prints the mirrored
  trades it *would* have produced. No orders are ever sent.

**Registry** — `shared/services/registry.py`: two new descriptors,
`arb-scanner` and `copy-trader`, both `kind="worker"`, `phase="33"`,
`enabled_on_vps=False`. They'll flip on in Phase 34 alongside the
strategy composer.

### Tangible live output from the demos

Run against real public endpoints, no keys:

```
$ py -m shared.cli.arb_cli demo --symbols BTC/USDT ETH/USDT SOL/USDT
== BTC/USDT ==
  binance   bid 75410.0000   ask 75410.0100 *  gap-vs-best-ask  -0.00bps
  kraken    bid 75415.5000   ask 75420.0000    gap-vs-best-ask  +0.73bps
  coinbase  bid 75421.6100 * ask 75432.8100    gap-vs-best-ask  +1.54bps
  bybit     bid 75412.2000   ask 75412.3000    gap-vs-best-ask  +0.29bps
```

Gross gaps are real (up to ~1.5 bps across majors right now) but
below the 36 bps of taker fees needed to clear a cross-venue round
trip, so no persisted opportunities — exactly the behaviour we want.

```
$ py -m shared.cli.copy_cli demo --symbol ETH/USDT \
    --size-mode fixed_notional_usd --size-value 50 --limit 10
== source: public-tape-leader (fixed_notional_usd, 50.0, cap=None) ==
  fills fetched : 10  trades kept : 10  trades skipped : 0
  sell 0.649000 ETH @ 2328.55 → mapped 0.021473 ETH = $50.0000
  sell 0.004300 ETH @ 2328.55 → mapped 0.021473 ETH = $50.0000
  …
```

Every real public fill — no matter whether the source traded 0.004
or 3 ETH — becomes exactly \$50 notional on our mirrored book.
Ratio mode + cap works the same way (owner tested on BTC with 5%
ratio and a \$500 cap).

### Success criteria

* ✓ Both migrations idempotent (`IF NOT EXISTS`); rollback block
  at the bottom of each file; `COALESCE`-based uniqueness done
  via `CREATE UNIQUE INDEX` (PostgreSQL won't accept that inside
  inline `UNIQUE` constraints — same lesson as Phase 32).
* ✓ Scanner is a pure function — same quotes ⇒ same opportunities,
  same order (`(-net_bps, symbol)`).
* ✓ Mapper is a pure function — every decision recorded, skipped
  ones included, with an actionable `skip_reason`.
* ✓ Source watermarks prevent re-polling the same fills; unique
  constraint on `copy_trades(source_id, source_fill_id)` gives a
  belt-and-braces layer of idempotency.
* ✓ Regression: `pytest shared/tests → 493 passed` (459 + 34 new).
* ✓ `ruff` + `mypy --explicit-package-bases` clean on all new files.
* ✓ Registry correctly lists `arb-scanner` and `copy-trader` with
  `phase=33` and `enabled_on_vps=False`.
* ✓ Live demos succeed on real endpoints (captured in phase notes).

### Rollback

```sql
BEGIN;
DROP TABLE IF EXISTS public.copy_trades;
DROP TABLE IF EXISTS public.copy_sources;
DROP TABLE IF EXISTS public.arb_opportunities;
DROP TABLE IF EXISTS public.arb_venues;
COMMIT;
```

Then `git revert` the Phase 33 commit. Nothing else in the stack
depends on these tables yet — Phase 34 will be the first consumer.
Deleting the Phase 33 registry entries just removes the descriptors
from the CLI; every other service continues unchanged.

---

## Phase 34 — Strategy Composer

### Why this phase

Phases 31-33 left us with a lot of *signals* — Apex / Quant / Ledger /
Scout / Curiosity / Optimiser / RegimeWatcher verdicts (the "souls"),
arbitrage opportunities, and copy-trade mirrored fills — but nothing
that stitches them together into one canonical trade stream. Phase 34
introduces the **Strategy Composer**: a single orchestrator that

1. pulls candidate trades from every upstream producer,
2. dedupes and ranks them,
3. persists every candidate (kept or dropped) into an audit trail,
4. optionally runs them through a **gate** (Treasury + Guardrails
   wiring lands in a later phase),
5. optionally hands survivors to a **submit** callable (the Phase 26
   `ExecutionRouter`).

Everything is audit-logged — the composer never loses a proposal,
which is critical for the Rule-1 "backtests must equal live" story.

### What got built

* **Migration** — `shared/strategies/migrations/2026_04_19_phase34_strategies.sql`
  creates three objects in `public`:
  * `strategy_descriptors` — the registry of producers (arb / copy /
    souls / custom), each with an optional priority and config JSON.
  * `strategy_intents` — append-only audit trail of every proposed
    intent, its status, decision reason, and (once submitted) the
    `order_id` it produced.
  * `strategy_intents_latest` — view that returns the latest row per
    `(strategy_name, symbol, side)` so dashboards can render a clean
    "current plan" view without hunting through history.
  * Belt-and-braces partial unique index on
    `(strategy_name, source_ref)` where `source_ref IS NOT NULL` —
    prevents accidental double-recording even if a producer re-emits.
* **Protocol** — `shared/strategies/protocol.py` defines
  `StrategyDescriptor`, `StrategyIntent`, `CompositionResult` plus
  kind / status constants (`KIND_ARB/COPY/SOULS/CUSTOM`,
  `STATUS_PENDING/APPROVED/REJECTED/SUBMITTED/FILLED/SKIPPED/DUPLICATE/FAILED`).
* **Store + in-memory pool** — `shared/strategies/store.py` wraps
  the tables with async helpers (`upsert_descriptor`,
  `list_descriptors`, `record_intent` (idempotent),
  `update_intent_status`, `list_intents`, `list_latest`) and
  `shared/strategies/memory_pool.py` mimics the same semantics for
  tests (including the partial unique index).
* **Producers** — `shared/strategies/producers/`:
  * `base.py` — the `BaseProducer` protocol.
  * `arb_producer.py` — reads `arb_opportunities` and emits a buy-leg
    + sell-leg per opportunity, correlated by a shared
    `arb_opportunities.id=…` ref.
  * `copy_producer.py` — reads pending `copy_trades` and turns each
    into an intent (notional-weighted priority).
  * `souls_producer.py` — reads approved/proposed soul verdicts and
    lifts the payload (`symbol`, `side`, `size_base`, …) into intents.
* **Composer** — `shared/strategies/composer.py`
  (`StrategyComposer` + `ComposerConfig` + `GateDecision`). A
  single `tick()` does: gather → dedupe → rank → persist → gate →
  submit → update-status. Every side-effect is optional so tests
  can pass deterministic stubs, and Phase 35+ can wire real gates.
* **Service** — `shared/strategies/service.py` is a thin wrapper
  (`StrategyComposerService`) that owns the composer and exposes
  `tick()` for a future daemon.
* **Service registry** — `shared/services/registry.py` gains a
  `strategy-composer` worker entry with `enabled_on_vps=False`.
* **CLI** — `shared/cli/strategy_cli.py` with:
  * `apply-migration` / `migration-sql` — DB helpers.
  * `descriptor-add` / `descriptors` — register producers.
  * `tick` — run one composition pass (default: arb producer).
  * `intents` / `latest` — inspect the audit trail.
  * `demo` — **live** CCXT demo that chains the Phase 33 arb scanner
    straight into the composer (public endpoints only) and prints
    every proposed / approved / submitted leg.
* **Tests** — `shared/tests/test_strategies.py` (18 tests) covers
  migration, store CRUD, partial unique dedupe, composer dedupe +
  ranking, gate approve/reject paths, submit wiring, and a CLI smoke
  test. Full `pytest shared/tests` → **511 passed** (up from 493).
* **Linters** — `ruff check` clean; `mypy --explicit-package-bases`
  clean on all new files.

### Live demo snapshot (real CCXT, no keys)

```
py -m shared.cli.strategy_cli demo --symbols BTC/USDT ETH/USDT SOL/USDT \
  DOGE/USDT --min-net-bps -50 --gate --show 8

[demo] scanning ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'DOGE/USDT'] on
       ['binance', 'kraken', 'coinbase', 'bybit'] ...
[demo] arb opportunities found: 8

== composition result ==
  proposed=16  approved=16  rejected=0  duplicate=0  submitted=0
  [approved] arb-scanner  ETH/USDT  buy  size=0.020900  notional=$48.54
                          venue=bybit    priority=-17.80
  [approved] arb-scanner  ETH/USDT  sell size=0.020900  notional=$48.56
                          venue=binance  priority=-17.80
  [approved] arb-scanner  BTC/USDT  buy  size=0.132839  notional=$10000.00
                          venue=bybit    priority=-19.12
  …
```

16 intents (8 opportunities × buy + sell legs) flowed through: live
CCXT quotes → arb scanner → producer → composer → gate stub →
`strategy_intents` audit rows.

### Success criteria

* ✓ Migration idempotent (`IF NOT EXISTS`), rollback block included,
  partial unique index via `CREATE UNIQUE INDEX …WHERE source_ref IS
  NOT NULL`.
* ✓ Composer is a pure orchestrator — all execution semantics live
  in gate/submit callables so the Rule-1 parity path stays
  deterministic.
* ✓ Dedupe at two layers: in-memory (same tick) + DB partial unique
  index (across ticks); composer turns DB conflicts into `duplicate`
  status rather than exceptions.
* ✓ Every state transition is recorded: `pending → approved →
  submitted`, `pending → rejected`, `pending → duplicate`,
  `approved → failed`.
* ✓ Regression: `pytest shared/tests → 511 passed` (493 + 18 new).
* ✓ `ruff` + `mypy --explicit-package-bases` clean on all new files.
* ✓ Registry lists `strategy-composer` with `phase=34` and
  `enabled_on_vps=False`.
* ✓ Live demo succeeds on real endpoints (captured above).

### Rollback

```sql
BEGIN;
DROP VIEW IF EXISTS public.strategy_intents_latest;
DROP TABLE IF EXISTS public.strategy_intents;
DROP TABLE IF EXISTS public.strategy_descriptors;
COMMIT;
```

Then `git revert` the Phase 34 commit and remove the
`strategy-composer` descriptor from `shared/services/registry.py`.
Phase 33 producers continue to work independently — the composer is
the only consumer of this phase's tables.

---

## Phase 35 — Local-to-VPS Backtest Submission

### Purpose (plain English)

Before Phase 35, submitting a backtest to the VPS meant pushing a
raw envelope into Redis via `shared.backtest.queue.BacktestQueue` and
*hoping*. There was no durable record that you ever asked for that
backtest, no way to look up the result from your laptop later, and
no idempotency — submit the same spec twice and you paid for two
identical runs. Phase 35 fixes all three in one layer.

The deliverable is a tiny, boring, PostgreSQL-backed **audit and
status table** (`public.backtest_submissions`) that sits in front of
the existing Redis queue. Every submission creates a row; the worker
transitions that row through `submitted → queued → running →
completed | failed | cancelled`; the local CLI can then query the
row for the result summary and Clickhouse artefact pointer without
ever touching Redis directly.

Two design choices worth calling out:

1. **The queue stays the execution transport.** We do *not* rip out
   Phase 16. The Phase-35 submitter is a thin wrapper that writes to
   Postgres first, then hands the canonical payload — annotated with
   the new `submission_id` — to the existing `BacktestQueue`. The
   worker picks the envelope up exactly as before; the only change
   is a one-line hook (`SubmissionWorkerHook.on_start / on_complete
   / on_fail`) that flips the `backtest_submissions` row's status.
2. **Hash-based idempotency is enforced in the DB, not in Python.**
   The partial unique index
   `backtest_submissions_hash_active_idx` covers
   `(spec_hash) WHERE status IN ('submitted','queued','running','completed')`,
   so two clients racing the same spec can't both win — one insert
   hits the index, the submitter reads back the existing row and
   returns it as-is. Cancelling or failing a run releases the slot
   for a clean retry.

### What was built

* **Migration** — `shared/backtest_submit/migrations/2026_04_19_phase35_submissions.sql`:
  * `public.backtest_submissions` (company_id, client_id, spec
    JSONB, spec_hash, status, queue_job_id, result_summary,
    artefacts, error, metadata, submitted/queued/started/
    completed/updated timestamps).
  * `public.backtest_submissions_active` view (status in
    submitted/queued/running).
  * Indexes: `_status_idx`, `_client_idx`, and the idempotent
    `_hash_active_idx` partial unique index.
* **Protocol** — `shared/backtest_submit/protocol.py`:
  * `BacktestSpec` dataclass with `canonical_payload()` (sorted
    symbols, sorted params) and `hash()` (SHA256 of the canonical
    JSON) — this is the contract for dedupe.
  * `BacktestSubmission` with `from_spec(...)`, `is_active`,
    `is_terminal`, `to_dict()`.
  * Status constants: `STATUS_SUBMITTED`, `STATUS_QUEUED`,
    `STATUS_RUNNING`, `STATUS_COMPLETED`, `STATUS_FAILED`,
    `STATUS_CANCELLED` and the `ACTIVE_STATUSES` /
    `TERMINAL_STATUSES` tuples.
* **Store** — `shared/backtest_submit/store.py`
  (`BacktestSubmissionStore`): async wrappers for
  `create / get / get_by_hash / mark_queued / mark_running /
  mark_completed / mark_failed / mark_cancelled / list`. JSONB
  columns are serialised with `json.dumps(..., default=str)`, and
  the `_row` helper hydrates rows back into dataclasses (handles
  both `dict` and `str` JSONB return types from different drivers).
* **In-memory pool** —
  `shared/backtest_submit/memory_pool.py`
  (`InMemoryBacktestSubmitPool`): a pure-Python test double that
  mimics the partial unique index and the status-lifecycle queries.
  Lets the CLI `demo` subcommand run without Postgres or Redis.
* **Submitter** — `shared/backtest_submit/submitter.py`
  (`BacktestSubmitter` + `QueueProtocol` + `InMemoryQueue`):
  * `submit(spec, ...)` writes the row, then (if a queue is
    provided) enqueues a canonical payload with the new
    `submission_id` baked in.
  * On queue failure, the row is marked `failed` with a clear error
    message so the audit trail still exists.
  * On hash collision, returns the existing row — idempotent, zero
    Redis traffic.
* **Worker hook** — `shared/backtest_submit/worker_hook.py`
  (`SubmissionWorkerHook`): three methods (`on_start`,
  `on_complete`, `on_fail`, `on_cancel`) that the existing
  `shared.backtest.worker` process calls at the right moments. All
  exceptions inside the hook are logged but swallowed so a Postgres
  hiccup can never kill a running worker.
* **CLI** — `shared/cli/backtest_cli.py`:
  * `apply-migration` / `migration-sql` — DB bootstrap helpers.
  * `submit --spec @file.json [--with-queue]` — the primary command.
  * `status <id>`, `list [--status X] [--client-id Y]
    [--active-only]`, `cancel <id>`, and `wait <id>` (polls until
    terminal, exit code 0 on completed / 2 on failed / 3 on
    timeout).
  * `demo` — end-to-end in-memory showcase.
* **Service registry** — two Phase-35 descriptors added to
  `shared/services/registry.py`:
  * `backtest-submitter` (kind=api) — the laptop CLI.
  * `backtest-runner` (kind=worker) — the Phase-16 worker extended
    with the submission hook. Both are `enabled_on_vps=False` until
    the worker-side integration is deployed in a follow-up.
* **Tests** — `shared/tests/test_backtest_submit.py` (16 tests):
  migration presence, canonical hashing (symbol/param ordering
  invariance, param sensitivity), store CRUD + lifecycle
  transitions, active-only filtering, submitter queue/hook behaviour
  (including dedupe skipping the second enqueue), worker hook
  success + failure paths, and CLI smoke tests
  (`migration-sql`, `apply-migration --path-only`,
  `submit --in-memory`, `demo`).

### Live demo

```
$ py -m shared.cli.backtest_cli demo
[demo] submitting three backtests ...
  submitted id=1 hash=2565bae1b5a1 status=queued queue_job=mem-000001
  submitted id=2 hash=b1e6d4321621 status=queued queue_job=mem-000002
  submitted id=3 hash=a8bedce0bd04 status=queued queue_job=mem-000003

[demo] resubmit of same spec returns id=1 status=queued (idempotent)

[demo] fake worker processing queue ...
  worker finished id=1  pnl=$  73.45  trades=23   sharpe=0.7
  worker finished id=2  pnl=$ 196.90  trades=26   sharpe=1.4
  worker finished id=3  pnl=$  70.35  trades=29   sharpe=2.1

[demo] final statuses:
  id=1 status=completed strategy=rsi_crossover      pnl=$  73.45 trades=23 sharpe=0.7
  id=2 status=completed strategy=donchian_breakout  pnl=$ 196.90 trades=26 sharpe=1.4
  id=3 status=completed strategy=ma_revert          pnl=$  70.35 trades=29 sharpe=2.1
```

The "resubmit is idempotent" line is the Phase-35 promise in one
sentence: operators can't accidentally duplicate a run by
re-pressing enter.

### Success criteria

* 16/16 Phase-35 tests green.
* Full regression 527/527 green (previous 511 + 16 new).
* `ruff`, `mypy --explicit-package-bases`, `bandit` all clean on
  the new code.
* Service registry reports 21 services, including
  `backtest-submitter` and `backtest-runner` tagged `phase=35`.
* `py -m shared.cli.backtest_cli demo` runs end-to-end without
  Redis or Postgres.

### Rollback

```sql
BEGIN;
DROP VIEW IF EXISTS public.backtest_submissions_active;
DROP TABLE IF EXISTS public.backtest_submissions;
COMMIT;
```

Then `git revert` the Phase 35 commit and remove the
`backtest-submitter` / `backtest-runner` descriptors from
`shared/services/registry.py`. The Phase-16 `BacktestQueue` is
untouched, so the existing Redis-only flow keeps working.

---

## Phase 36 — Owner Dashboard + Telegram OTP + Mobile

### Purpose (plain English)

Before Phase 36 there was no way to look at the system from a phone
or laptop without opening an SSH tunnel and running CLIs. Phase 36
gives the owner a tiny, mobile-friendly **read-only** dashboard —
services, backtest submissions, strategy intents, regime,
guardrails — guarded by a Telegram-OTP login flow.

The design is boring on purpose:

1. **Telegram OTP, not passwords.** Passwords on a single-user
   owner-facing dashboard are strictly worse than "prove you can
   read this Telegram chat". An allowlisted `chat_id` requests an
   OTP, the Telegram Bot API delivers a 6-digit code, the owner
   echoes it back, and a session token is issued.
2. **Hashes in the database, raw secrets only in-flight.** Every
   OTP is stored as a SHA256 hex; every session token is stored the
   same way. If the DB is ever dumped, nobody can replay the
   codes/tokens. The raw OTP is only in the outgoing Telegram
   message; the raw session token is only in the response to
   `verify-otp` and in the browser's `localStorage`.
3. **Read-only API.** The dashboard cannot submit a backtest, place
   a trade, or change a rule. All mutating operations remain CLI-
   only. That keeps the blast radius of a stolen session token
   small.

### What was built

* **Migration** — `shared/dashboard/migrations/2026_04_19_phase36_dashboard.sql`:
  * `public.dashboard_users` — allowlisted chat IDs.
  * `public.dashboard_otps` — hashed codes with expiry + single-use
    marker.
  * `public.dashboard_sessions` + `public.dashboard_sessions_active`
    view — hashed session tokens with expiry and revocation.
* **Protocol** — `shared/dashboard/protocol.py`:
  `DashboardUser`, `DashboardOtp`, `DashboardSession`,
  `OtpIssueResult`, `SessionIssueResult`, `DashboardSnapshot`.
* **Store** — `shared/dashboard/store.py`:
  `DashboardUserStore`, `DashboardOtpStore`,
  `DashboardSessionStore`, `hash_secret()`, plus an `InMemoryDashboardPool`
  in `memory_pool.py` that mirrors the partial-unique-index and
  expiry semantics for tests.
* **Telegram transport** — `shared/dashboard/telegram.py`:
  `TelegramSender` protocol, `NullTelegramSender` (writes codes to
  a local JSONL log — dev only), `TelegramBotSender` (uses
  `https://api.telegram.org/bot{token}/sendMessage` via stdlib
  `urllib`), `sender_from_env()` auto-picks based on
  `TICKLES_TELEGRAM_BOT_TOKEN`.
* **Auth** — `shared/dashboard/auth.py`:
  * `DashboardAuth.issue_otp(chat_id)` generates a `secrets.randbelow`-
    based 6-digit code, hashes it, stores the row, delivers the
    raw code through the transport, and returns the raw code + TTL
    to the caller **once**.
  * `DashboardAuth.verify_otp(chat_id, code)` looks up the matching
    un-consumed, un-expired row, consumes it, and issues a
    `secrets.token_urlsafe(32)` session token.
  * `DashboardAuth.authenticate_token(token)` resolves the token to
    an active session + user, touches `last_seen_at`.
  * Explicit exception classes with `http_status` attributes:
    `UnknownChat (404)`, `DisabledUser (403)`, `InvalidOtp (401)`,
    `InvalidSession (401)`, `OtpDeliveryFailed (502)`.
* **Snapshot** — `shared/dashboard/snapshot.py` + `providers.py`:
  `SnapshotBuilder` aggregates data from `ServicesProvider`,
  `SubmissionsProvider`, `IntentsProvider`, `RegimeProvider`,
  `GuardrailsProvider`. Any provider can be missing; the snapshot
  returns partial data + a `notes` array explaining which sources
  were unavailable. Concrete providers: `RegistryServicesProvider`
  (wraps `SERVICE_REGISTRY`), `SubmissionsStoreProvider` (wraps the
  Phase-35 store), `IntentsSqlProvider` (queries
  `strategy_intents_latest`).
* **HTTP server** — `shared/dashboard/server.py` (aiohttp 3.x):
  * Middleware enforces bearer-token auth on every `/api/*` path
    except `/api/auth/request-otp` and `/api/auth/verify-otp`.
  * Endpoints: `GET /` (static SPA), `GET /healthz`,
    `POST /api/auth/request-otp`, `POST /api/auth/verify-otp`,
    `POST /api/auth/logout`, `GET /api/snapshot`,
    `GET /api/services`.
  * Dev flag `expose_otp=True` echoes the raw OTP in the response
    (used by the demo / tests, never on the VPS).
* **Static SPA** — `shared/dashboard/web/index.html`:
  Single self-contained HTML+CSS+JS page. Dark theme, mobile-first
  (`max-width: 1200px`, 1-column on phones, 2-column ≥ 720 px).
  Login screen → chat_id + OTP input; main screen → services KPI
  card, submissions KPI card, latest intents table, regime +
  guardrails status. Polls `/api/snapshot` every 15 s. Session
  token persisted in `localStorage`; automatic logout on 401.
* **CLI** — `shared/cli/dashboard_cli.py`:
  `apply-migration`, `migration-sql`, `user-add`, `user-list`,
  `user-disable`, `request-otp`, `verify-otp`, `sessions`,
  `revoke-sessions`, `serve`, `demo`.
* **Service registry** — `dashboard` descriptor (kind=api,
  phase=36, enabled_on_vps=False).
* **Tests** — `shared/tests/test_dashboard.py` (20 tests): migration
  presence, hashing stability, user-store CRUD + set_enabled, OTP
  lifecycle (happy path, unknown chat, disabled user, wrong code,
  consumed code, expired code), session revocation (individual +
  all-for-chat), snapshot builder with mixed providers (services
  only, services + submissions, with notes), HTTP auth middleware
  (valid bearer, missing bearer, logout revokes, healthz public,
  unknown-chat returns 404), and CLI smoke tests
  (`migration-sql`, `demo`).

### Live demo

```
$ py -m shared.cli.dashboard_cli demo
[demo] enrolled user chat_id=12345 (display=Owner Demo)
[demo] issued OTP code=497025 expires_at=2026-04-19T07:46:51+00:00
[demo] telegram transport delivered=True (NullTelegramSender)
[demo] verified OTP, issued session id=1 token=xSTIp2s28uVFvk...
[demo] token authenticates user=12345 role=owner session_id=1

[demo] snapshot summary:
  services registered : 22
  submissions active  : 0
  latest intents      : 0
  notes               : ['intents: provider not wired']

[demo] first 5 services from the registry:
  - backtest-submitter     kind=api        phase=35  vps=False
  - dashboard              kind=api        phase=36  vps=False
  - auditor                kind=auditor    phase=21  vps=False
  - catalog                kind=catalog    phase=14  vps=True
  - candle-daemon          kind=collector  phase=13  vps=True
```

### Success criteria

* 20/20 Phase-36 tests green.
* Full regression 547/547 green (previous 527 + 20 new).
* `ruff`, `mypy --explicit-package-bases --follow-imports=silent`,
  `bandit` all clean (Telegram HTTPS call has a scoped
  `# nosec B310`).
* Service registry reports 22 services, including the new
  `dashboard` descriptor tagged `phase=36`.
* `py -m shared.cli.dashboard_cli demo` runs end-to-end without
  Telegram or Postgres.

### Rollback

```sql
BEGIN;
DROP VIEW IF EXISTS public.dashboard_sessions_active;
DROP TABLE IF EXISTS public.dashboard_sessions;
DROP TABLE IF EXISTS public.dashboard_otps;
DROP TABLE IF EXISTS public.dashboard_users;
COMMIT;
```

Then `git revert` the Phase 36 commit and remove the `dashboard`
descriptor from `shared/services/registry.py`. All other phases are
untouched — the dashboard is strictly additive.

---

## Phase 37 — MCP stack

### What we built

A Model Context Protocol (MCP) server that exposes Tickles
capabilities as standardised tools to LLM-native agents (Paperclip,
OpenClaw, Claude Desktop, etc.). MCP is JSON-RPC 2.0; we implemented
the subset we actually need and kept the surface small and
well-typed so new tools are trivial to add.

Think of it like a "USB-C port for the trading house": any agent that
speaks MCP can now call `services.list`, `backtest.submit`,
`dashboard.snapshot`, etc., without having to know anything about our
internal modules or databases.

### Deliverables

* `shared/mcp/migrations/2026_04_19_phase37_mcp.sql` creates:
  * `public.mcp_tools` — declarative catalogue of exposed tools with
    `input_schema`, `read_only`, `enabled` flags. Operators can toggle
    tools on/off without redeploy.
  * `public.mcp_invocations` — append-only audit log: every tool call
    records caller, transport, params, result/error, latency_ms.
  * `public.mcp_invocations_recent` view (last 500).
* `shared/mcp/protocol.py` — JSON-RPC 2.0 dataclasses (`JsonRpcRequest`,
  `JsonRpcResponse`, `rpc_error`), `McpTool`, `McpResource`,
  `McpInvocation`. Defines standard error codes
  (`METHOD_NOT_FOUND`, `TOOL_NOT_FOUND`, `TOOL_DISABLED`,
  `TOOL_FAILED`). Protocol version pinned to `2024-11-05`.
* `shared/mcp/store.py` — async `ToolCatalogStore` (upsert, list,
  enable/disable) and `InvocationStore` (append-only record, recent
  query). Accepts any asyncpg-shaped pool.
* `shared/mcp/memory_pool.py` — `InMemoryMcpPool` so tests run
  without Postgres.
* `shared/mcp/registry.py` — in-process `ToolRegistry` plus factory
  builders for seven built-in tools:
    - `ping` — heartbeat.
    - `services.list` — wraps the in-process ServiceRegistry.
    - `strategy.intents.recent` — wraps Phase-34 intents.
    - `backtest.submit` — wraps Phase-35 submitter.
    - `backtest.status` — wraps Phase-35 store.
    - `dashboard.snapshot` — wraps Phase-36 snapshot builder.
    - `regime.current` — wraps Phase-27 regime service.
* `shared/mcp/server.py` — `McpServer` JSON-RPC dispatcher. Handles
  `initialize`, `ping`, `tools/list`, `tools/call`, `resources/list`,
  `resources/read`. Wraps every `tools/call` in try/finally so the
  invocation store always gets a row (success or failure).
* `shared/mcp/transports/stdio.py` — `run_stdio` reads newline-
  delimited JSON from stdin and writes responses to stdout. Matches
  how Claude Desktop spawns local MCP servers.
* `shared/mcp/transports/http.py` — `build_http_app` creates an
  `aiohttp` app with optional bearer-token auth middleware. Single
  endpoint `POST /mcp` plus `GET /healthz`.
* `shared/cli/mcp_cli.py` — subcommands:
    - `apply-migration` / `migration-sql`
    - `tools-list`
    - `tools-call <name> --args JSON`
    - `serve-stdio`
    - `serve-http --host --port --token`
    - `invocations --dsn --limit`
    - `demo [--verbose]`
* `shared/services/registry.py` — new `mcp-server` descriptor
  (`kind=api`, `tags={phase: 37}`, `enabled_on_vps=False`).
* `shared/tests/test_mcp.py` — 16 tests covering migration, stores,
  registry, JSON-RPC dispatcher (success / error / notification /
  unknown tool / method not found / invalid params), stdio
  round trip, HTTP transport with and without bearer auth, CLI
  `demo` smoke test.

### Live demo

```
$ py -m shared.cli.mcp_cli tools-list
{"count": 7, "tools": [
  "backtest.status", "backtest.submit",
  "dashboard.snapshot", "ping", "regime.current",
  "services.list", "strategy.intents.recent"
]}

$ py -m shared.cli.mcp_cli demo
{
  "transcript_steps": 6,
  "invocations_logged": 4,
  "tools_registered": [
    "backtest.status", "backtest.submit",
    "dashboard.snapshot", "ping", "regime.current",
    "services.list", "strategy.intents.recent"
  ]
}
```

The `demo` runs the full JSON-RPC sequence against an in-memory pool:
`initialize` -> `tools/list` -> four `tools/call` invocations. Every
call writes a row into `mcp_invocations`.

### Success criteria

* 16/16 Phase-37 tests green.
* Full regression 563/563 green (previous 547 + 16 new).
* `ruff`, `mypy --follow-imports=silent --explicit-package-bases`,
  `bandit` all clean on Phase-37 surface.
* Service registry reports 23 services, including `mcp-server`
  tagged `phase=37`.
* `py -m shared.cli.mcp_cli demo` runs end-to-end without Postgres.

### Rollback

```sql
BEGIN;
DROP VIEW IF EXISTS public.mcp_invocations_recent;
DROP TABLE IF EXISTS public.mcp_invocations;
DROP TABLE IF EXISTS public.mcp_tools;
COMMIT;
```

Then `git revert` the Phase 37 commit and remove the `mcp-server`
descriptor from `shared/services/registry.py`. Phase 37 is strictly
additive — no earlier phase depends on it.

---

## Phase 38 - Validation + code-analysis + docs freeze

### Purpose

Phase 38 is the audit stamp. It does not ship new features or DB
migrations; it proves that the system we built in phases 13-37 is
lint-clean, type-clean, security-clean, and that the full regression
is green both locally and on the VPS. It also freezes the service
index into a human-readable document so future operators can reason
about the system without reading source.

### Deliverables

* `shared/docs/SERVICES.md` - canonical table of the 23 registered
  services plus every database artefact each phase owns.
* `shared/docs/PHASE_38_VALIDATION.md` - detailed audit report
  (tooling, findings, reproduction recipe, rollback).
* Safe hygiene fixes on pre-existing Phase-16/18 code:
  - `shared/backtest/accessible.py` - split two semicolon-joined
    statements (E702); annotated two B608 false-positives with
    `# nosec B608`.
  - `shared/backtest/indicators/core.py` +
    `smart_money.py` - renamed single-letter variable `l` to `lo`
    for "low" (E741). Math is byte-for-byte identical; regression
    unchanged.
  - CLI `# type: ignore` annotations for asyncpg / pandas lazy
    imports (`treasury_cli`, `services_catalog_cli`, `auditor_cli`,
    `features_cli`, `engines_cli`).

### Audit results

```
ruff    : 0 findings on 22 phase module directories.
mypy    : 0 errors on Phase 22-37 surface; 12 pre-existing errors
          in shared/services/run_all_collectors.py (legacy
          orchestrator, out of scope, documented).
bandit  : 0 medium/high findings (-ll). Low-severity findings are
          informational and unchanged.
pytest  : 563 / 563 green locally (33s).
VPS     : 563 / 563 green on VPS (80s).
```

### Service registry frozen

23 services total. 4 are live on the VPS (`candle-daemon`,
`catalog`, `bt-workers`, `md-gateway`); the other 19 are staged
with migrations applied but systemd units intentionally off until
operators switch them on.

### Rollback

Phase 38 changed no runtime behaviour. `git revert` of the Phase-38
commit removes the two new docs and reverts the five hygiene fixes;
tests and deploy state are unaffected.

---

## Phase 39 - End-to-end drill

> Single-command go/no-go for the entire Phase 13-37 system.

### Why

After Phase 38 froze the docs, we needed one repeatable command an
operator (or CI) can run that exercises every phase end-to-end and
returns a single pass / fail. Without that, regressions can hide
inside one CLI for weeks before someone notices. The drill is read-
only, in-memory, and intentionally does NOT touch live exchanges,
live PostgreSQL schemas owned by other phases, or live Telegram
traffic.

### What was built

* `shared/cli/drill_cli.py` - the harness. Two subcommands:
  * `list` - prints the 19 steps the drill will run.
  * `run [--out file.json] [--stop-on-fail]` - executes them and
    emits a JSON report.
* `shared/docs/PHASE_39_DRILL.md` - the operator's guide (table of
  steps, how to run, rollback).
* `shared/docs/PHASE_39_DRILL.json` - last local report.
* `shared/docs/PHASE_39_DRILL_VPS.json` - last VPS report.

### Coverage (19 steps)

| Phase | Step                          | Kind   |
|-------|-------------------------------|--------|
| 14    | market_data_layout            | python |
| 18    | indicators_registry           | python |
| 19    | backtest_engines_registry     | python |
| 21    | auditor_store_import          | python |
| 22    | services_registry_snapshot    | python |
| 23    | enrichment_default_pipeline   | python |
| 25    | treasury_pure_size            | cli    |
| 26    | execution_paper_simulate      | cli    |
| 27    | regime_classifiers            | python |
| 28    | guardrails_rule_kinds         | python |
| 29    | altdata_sources               | cli    |
| 30    | events_kinds                  | cli    |
| 31-32 | souls_personas                | python |
| 33    | arb_demo                      | cli    |
| 33    | copy_demo                     | cli    |
| 34    | strategy_demo                 | cli    |
| 35    | backtest_submit_demo          | cli    |
| 36    | dashboard_import              | python |
| 37    | mcp_demo                      | cli    |

### Live results

```
Local Win11 : 19 / 19 passed in ~21s
VPS         : 19 / 19 passed in ~31s
```

Tangibles surfaced by the drill on the live system:

* Phase 18 - 260 indicators registered, categories `crash_protection,
  momentum, pattern, performance, smart_money, statistical, trend,
  volatility, volume`.
* Phase 19 - engines `classic, vectorbt, nautilus`.
* Phase 22 - 23 services registered across 18 phases; 4 enabled on
  VPS (`candle-daemon, catalog, bt-workers, md-gateway`).
* Phase 27 - classifiers `composite, trend, volatility`; regimes
  `bear, bull, crash, high_vol, low_vol, recovery, sideways,
  unknown`.
* Phase 28 - rule kinds `daily_loss, equity_drawdown,
  position_notional, regime_crash, stale_data`; actions `alert,
  flatten_positions, halt_new_orders`.
* Phase 31-32 - all 7 souls present.
* Phase 33 - arb scanner streams live CCXT quotes from
  binance / coinbase / kraken / bybit / bitfinex.
* Phase 35 - submission queue + worker hook completes 3 in-memory
  backtests with PnL / trades / sharpe.
* Phase 37 - JSON-RPC MCP server logs 4 invocations across 7 tools.

### How to run

```bash
python -m shared.cli.drill_cli list
python -m shared.cli.drill_cli run --out shared/docs/PHASE_39_DRILL.json
```

Exit code is `0` iff every step succeeded - safe for use as a
pre-deploy gate.

### Rollback

Phase 39 is purely additive - no DB migration, no service registry
entry, no systemd unit. Delete the four new files (`drill_cli.py`
plus the three docs) and the system is back to the Phase-38 baseline.

---

*End of original ROADMAP_V3.md (Phases 13-39). Below are the "Building" master-plan phases (Phases 0-10) that layer on top of the frozen platform to turn it into a tenant-hostable, autonomous trading building.*

---

## Building Phase 3 — Paperclip Company Create Wizard (landed 2026-04-19)

> Lets the user open a company from the UI and (optionally) tick a single
> checkbox to get a fully-provisioned workspace: per-company Postgres DB,
> Qdrant collection, mem0 scopes, MemU subscriptions, and — for trading
> templates — Treasury registration + venue allow-list. Before this phase
> the wizard only created a Paperclip row; no underlying infra existed.

### Why

The master-plan vision is "open a company from the UI → agents trade" with
zero manual Ops work. The previous flow only inserted a `companies` row;
every tenant then required manual `psql` + Qdrant + mem0 + Treasury work.
Phase 3 closes that gap by wiring the existing MCP `company.create` executor
(Building Phase 3.2) into the wizard, and by giving the platform a persistent
record of every provisioning run so the UI can poll progress and the owner
has an audit trail.

### Design pivot (important for future phases)

Initial attempt: store provisioning state as `metadata` JSONB on the
`companies` table (with a `PATCH /api/companies/:id` per step).

We discovered Paperclip's `companies` Drizzle schema has no `metadata`
column, so Zod silently stripped the field → every PATCH was a no-op.
Adding a column to the core `companies` table would have touched many
surfaces (portability export, tests, CLI, agent permissions, etc.).

**Decision**: introduce a dedicated `company_provisioning_jobs` table
instead. This isolates provisioning history in its own surface, makes
cascade-delete trivial, and gives us a stable API shape (one row per run,
latest row = "current state") for UI polling without polluting the core
companies schema.

### What shipped

**Templates (Building Phase 3.1, landed earlier in the session)**

* `shared/templates/companies/*.json` — 6 templates: `blank`, `media`,
  `research`, `surgeon_co`, `polydesk`, `mentor_observer`. Each declares
  `layer2_trading`, `rule_one_mode`, `memu_subscriptions`, `venues`,
  `skills`, `agents`, and `routines`. Loaded at runtime — no restart
  needed to add templates.
* `shared/templates/companies/README.md` — schema + the 9 provisioning
  steps documented in order.

**Provisioning executor (Building Phase 3.2)**

* `shared/provisioning/templates.py` — loader + validator for template
  JSON files.
* `shared/provisioning/jobs.py` — stateless fire-and-forget emitter that
  POSTs step events to Paperclip.
* `shared/provisioning/executor.py` — the 9-step atomic orchestrator.
  Layer-1 steps always run when provisioning is enabled (Postgres DB,
  Qdrant collection, mem0 scopes, MemU subscriptions). Layer-2 and
  skills/agents/routines only run when the template enables them.
  Each step is idempotent and auto-rolls back on failure.
* `shared/mcp/tools/provisioning.py` — added `company.templates` and
  `company.provision` MCP tools. `company.create` now chains into the
  executor when `provisioning.enabled=true`.

**Paperclip provisioning-jobs table (Building Phase 3.3, landed 2026-04-19)**

* `packages/db/src/schema/company_provisioning_jobs.ts` — Drizzle schema
  for the new table.
* `packages/db/src/migrations/0055_company_provisioning_jobs.sql` — SQL
  migration creating the table + 2 indexes. Auto-applied by
  `PAPERCLIP_MIGRATION_AUTO_APPLY=true` on systemd restart.
* `packages/db/src/migrations/meta/_journal.json` — entry `idx=55`
  `tag=0055_company_provisioning_jobs`.
* `server/src/services/company-provisioning-jobs.ts` — service exposing
  `create/appendEvent/latestByCompany/getById/listByCompany/countRunning`.
  Metadata is shallow-merged per event so the executor can add new
  top-level keys without a schema migration.
* `server/src/routes/company-provisioning-jobs.ts` — mounted at
  `/api/companies/:companyId/provisioning-jobs` with 5 endpoints:
  `POST /`, `POST /:jobId/events`, `GET /latest`, `GET /`, `GET /:jobId`.
* `server/src/routes/companies.ts` —
  1. Destructures optional `provisioning` block from the POST body.
  2. If `provisioning.enabled=true`, seeds a job row, returns its id on
     the create response as `provisioningJobId`, and fire-and-forgets a
     `company.provision` MCP call (5 s timeout, failures only logged).
  3. Adds `GET /api/companies/:companyId/provisioning-status` as a
     UI-friendly alias returning `{status:"not_provisioned"}` or
     `{status:"job", job}` so the wizard poller has one code path.

**Validators (Building Phase 3.4)**

* `packages/shared/src/validators/provisioning.ts` — step/metadata/event/
  create schemas (new).
* `packages/shared/src/validators/company.ts` — adds optional
  `provisioning` block to `createCompanySchema`:
  `{enabled, template, slug, ruleOneMode, memuSubscriptions}`.
* `packages/shared/src/validators/index.ts` + `packages/shared/src/index.ts`
  — re-exports so the server and UI can import them.

**UI wizard (Building Phase 3.6)**

* `ui/src/api/companies.ts` — extends `companiesApi.create` signature,
  adds `provisioningStatus` / `provisioningJobs` getters and TypeScript
  types (`CreateCompanyResponse`, `ProvisioningJob`, `ProvisioningStep`,
  `ProvisioningStatus`).
* `ui/src/components/OnboardingWizard.tsx`:
  * New Step-1 panel: checkbox "Provision company workspace" + template
    dropdown (Blank default) + Rule-1 mode selector.
  * Extends the `handleStep1Next` call to include the `provisioning`
    block when the checkbox is ticked.
  * Polls `GET /provisioning-status` every 1.5 s while a job is running
    and renders a live progress banner at the top of Step 2 (last 4
    steps, coloured by status, auto-stops on terminal state).

### Success criteria (all green)

* `psql … -c '\d company_provisioning_jobs'` lists the table with
  `company_id` FK, jsonb `steps`/`metadata`, indexes on
  `(company_id, started_at DESC)` and `overall_status`.
* `POST /api/companies` with no `provisioning` block returns a normal
  company, `provisioningJobId: null`, no row in
  `company_provisioning_jobs`.
* `POST /api/companies` with `provisioning.enabled=true` returns
  `provisioningJobId` set and seeds a row with `overall_status='running'`,
  empty `steps[]`, empty `metadata{}`.
* `POST /provisioning-jobs/:jobId/events` with a step appends to `steps`
  and shallow-merges `metadata`. Terminal event (`overallStatus !=
  running`) sets `finished_at`.
* `GET /provisioning-status` returns `{status:"not_provisioned"}` before
  any job, then `{status:"job", job:{…}}` reflecting latest run.
* `DELETE /api/companies/:id` cascades and removes all provisioning rows
  for that company.
* UI wizard build (`pnpm run build` in `ui/`) passes typecheck and
  produces a new bundle; restarting `paperclip.service` serves it on
  `http://127.0.0.1:3100`.

### Files added/edited

* Added (on VPS):
  `packages/db/src/schema/company_provisioning_jobs.ts`,
  `packages/db/src/migrations/0055_company_provisioning_jobs.sql`,
  `packages/shared/src/validators/provisioning.ts`,
  `server/src/services/company-provisioning-jobs.ts`,
  `server/src/routes/company-provisioning-jobs.ts`.
* Edited (on VPS):
  `packages/db/src/schema/index.ts` (re-export),
  `packages/db/src/migrations/meta/_journal.json` (idx=55 entry),
  `packages/shared/src/validators/company.ts` (provisioning block),
  `packages/shared/src/validators/index.ts` (re-exports),
  `packages/shared/src/index.ts` (re-exports),
  `server/src/services/index.ts` (re-export),
  `server/src/routes/companies.ts` (kickOffProvisioning helper,
  mounted sub-router, /provisioning-status alias, POST handler extension),
  `ui/src/api/companies.ts` (types + new methods),
  `ui/src/components/OnboardingWizard.tsx` (state, step-1 UI, poller,
  step-2 banner).
* Added (in repo, mirror of VPS):
  `shared/templates/companies/*.json` (6 templates + README),
  `shared/provisioning/{__init__,templates,jobs,executor}.py`,
  `shared/provisioning/README.md`.

### Rollback

To undo Building Phase 3 entirely (back to "Paperclip row only" behaviour):

1. **UI**: revert `ui/src/components/OnboardingWizard.tsx` and
   `ui/src/api/companies.ts` to their pre-phase versions, run
   `cd ui && pnpm run build`.
2. **Server**: revert `server/src/routes/companies.ts` (drop the
   `kickOffProvisioning` helper, the sub-router mount, the
   `/provisioning-status` alias, and the POST handler extension) and
   remove `server/src/routes/company-provisioning-jobs.ts` +
   `server/src/services/company-provisioning-jobs.ts`. Remove the
   `companyProvisioningJobService` re-export from
   `server/src/services/index.ts`.
3. **Validators**: revert `packages/shared/src/validators/company.ts`
   and remove `packages/shared/src/validators/provisioning.ts`. Remove
   the new re-exports from `packages/shared/src/validators/index.ts`
   and `packages/shared/src/index.ts`.
4. **DB**: drop the `company_provisioning_jobs` table, remove the
   journal entry `idx=55`, delete
   `packages/db/src/migrations/0055_company_provisioning_jobs.sql` and
   `packages/db/src/schema/company_provisioning_jobs.ts`, remove the
   re-export from `packages/db/src/schema/index.ts`.
5. Restart `paperclip.service`.

The Tickles MCP side (`company.create` / `company.provision` /
`company.templates` tools + `shared/provisioning/` executor) can stay
in place or be rolled back independently by reverting
`shared/mcp/tools/provisioning.py` and removing the
`shared/provisioning/` package. They never break anything if the UI no
longer calls them.

Partial rollback (keep infra, disable UI only): set
`provisioningEnabled` default `false` in the wizard and the checkbox
just never shows/ticks — everything else is inert.

### Post-landing patch — job_id propagation fix (2026-04-19 late)

The first Phase-3 smoke run surfaced one sneaky bug that is worth
calling out because it will bite anybody who extends the executor
later. Symptoms in `paperclip` logs:

```
WARN: POST /companies/<cid>/provisioning-events 404 "API route not found"  (x9 steps)
INFO: POST /01ae.../events 200                                             (terminal only)
```

All nine step events were hitting the legacy un-jobbed URL, so the UI
poller never saw them — only the final "overallStatus=ok" event
persisted. Root cause: `shared/provisioning/executor.py` was threading
`job_id` through via a `ContextVar` (`_CURRENT_JOB_ID`), but every step
ran inside `loop.run_in_executor(None, fn, *args)` which **does not**
copy contextvars into the worker thread. The final `_emit_terminal`
ran on the event-loop thread and saw the var just fine, which is why
only that one event got the right URL.

Fix (single small change): replace all `loop.run_in_executor(None,
fn, *args)` calls inside `run()` with `asyncio.to_thread(fn, *args)`.
`asyncio.to_thread` explicitly does `ctx = contextvars.copy_context()`
before dispatching, so `_CURRENT_JOB_ID` now propagates into every
step thread and `_emit()` can read it.

Also added in the same round-trip:

* `jobs.JobEvent` gained `overall_status` + `metadata_merge` fields
  so the terminal event can carry those server-side (matches the
  `appendProvisioningEventSchema`).
* `jobs.emit` routes to the per-job events URL when a `job_id` is set,
  and to the legacy fan-out URL otherwise.
* `shared/mcp/tools/provisioning.py` accepts `jobId` on both
  `company.create.provisioning` and `company.provision`, and forwards
  it into `executor.run(..., job_id=...)`.
* `server/src/routes/companies.ts` sends `jobId` (camelCase) in the
  MCP JSON-RPC call; previously it was sending `job_id` (snake_case)
  which the MCP schema silently ignored.

Verification (Paperclip-local smoke):

```
$ bash /tmp/smoke_job_id.sh
== 1. create company smoke_jobid_... (provisioning enabled, blank template)
  provisioningJobId: 57e06062-679d-...
== 2. poll provisioning-status
  t=1s overall=running steps=3
  t=2s overall=running steps=16
  t=3s overall=ok      steps=19
== 3. TERMINAL ==
  19 events (9 × running + 9 × ok/skipped + 1 × terminal) all posted
  to /provisioning-jobs/<job_id>/events
```

Rollback for this patch only: revert the `asyncio.to_thread` change
in `shared/provisioning/executor.py::run()` back to
`loop.run_in_executor(None, ...)`; revert the camelCase rename in
`server/src/routes/companies.ts::kickOffProvisioning`. The executor
still functions — UI just stops seeing per-step progress and the
final row shows only the terminal event.

### Post-landing patch — one-click agent auth + template trim (2026-04-19 later)

The first user-driven end-to-end wizard run surfaced *three* bugs which
together meant a freshly-provisioned trading company's agents couldn't
actually run anything:

1. **Gateway token not auto-injected on agent create.** Every
   `openclaw_gateway` agent needs both `adapterConfig.url`
   (e.g. `ws://127.0.0.1:18789`) and
   `adapterConfig.headers["x-openclaw-token"]` on its first invocation,
   otherwise the adapter says `unauthorized: gateway token missing`.
   Paperclip's POST `/companies/:id/agents` handler had no defaulting
   logic for these, so the wizard's "create CEO" path produced a broken
   agent — the user had to open the agent's Config panel and paste in
   an API key manually.
2. **Adapter-type name mismatch.** `shared/provisioning/executor.py`
   hardcoded `adapterType: "openclaw-gateway"` (hyphen) but the
   canonical Paperclip enum is `openclaw_gateway` (underscore). Every
   template-hired agent therefore failed with HTTP 422
   `Unknown adapter type`.
3. **Role enum mismatch.** Templates used friendly role names
   (`analyst`, `observer`, `member`, `quant`, `ledger`) which are not
   in Paperclip's fixed `AGENT_ROLES` enum, producing 400 Zod errors.

And separately the user asked to **trim six company templates down to
two** — "blank" (Layer-1 kit only) and "trading" (Layer-1 + Layer-2
trading add-ons + 1 pre-hired CEO) — because the media/research/
mentor_observer/polydesk/surgeon_co verticals were over-fitting the
wizard. Feature verticals are now expressed by installing skills
after-the-fact, not by template choice.

#### Fix A — Paperclip auto-defaults for OpenClaw Gateway agents

* `server/src/routes/agents.ts` gains
  `ensureOpenClawGatewayUrlAndToken(adapterType, adapterConfig)`. It
  reads `process.env.OPENCLAW_GATEWAY_URL` and
  `process.env.OPENCLAW_GATEWAY_TOKEN`, and fills in any missing
  `.url` or `.headers["x-openclaw-token"]` on the adapter config.
  Explicit values in the incoming POST are *never* overwritten.
* It is **chained inside** `applyCreateDefaultsByAdapterType` at all
  three existing callsites (codex_local branch, gemini_local branch,
  and the catch-all), so every path — POST `/companies/:id/agents`,
  POST `/companies/:id/agent-hires`, and the runtime
  `effectiveAdapterConfig` resolver — picks it up with zero extra
  wiring.
* **Env file**: `/etc/paperclip/openclaw-gateway.env` (mode 640,
  `root:paperclip`) contains `OPENCLAW_GATEWAY_URL=ws://127.0.0.1:18789`
  and `OPENCLAW_GATEWAY_TOKEN=<mirror of gateway.auth.token from
  /root/.openclaw/openclaw.json>`. It is loaded via
  `EnvironmentFile=-/etc/paperclip/openclaw-gateway.env` in
  `/etc/systemd/system/paperclip.service`. A deploy script
  (`_paperclip_patches/phaseA_deploy.sh`) extracts the token from the
  root-only `openclaw.json` once, writes the env file, patches the
  unit, and restarts.
* **Backfill**: `_paperclip_patches/phaseA4_backfill.py` PATCHes every
  existing `openclaw_gateway` agent that is missing either value. Out
  of 8 existing agents on the box, 7 were already wired manually and 1
  (`CEO_TEST`) was fixed by the backfill.

#### Fix B — Executor adapter type + role mapping

* `shared/provisioning/executor.py` now hardcodes `"openclaw_gateway"`
  (underscore) in the hire body.
* New `_ROLE_MAP` dict + `_map_role_for_paperclip(raw)` helper
  translates `analyst → researcher`, `quant → researcher`,
  `observer → general`, `ledger → general`, `member → general`.
  Original role is preserved under `metadata.templateRole`.
* Same helper runs regardless of template, so future templates can use
  either canonical or friendly role names. `shared/provisioning/
  templates.py::VALID_AGENT_ROLES` was expanded to accept the full
  superset at load time.
* As belt-and-braces, the executor *also* injects
  `adapterConfig.url`/`headers["x-openclaw-token"]` from its own env
  vars (if set) — makes the executor portable to a Paperclip that
  hasn't taken Fix A yet.

#### Fix C — Template trim + wizard UI

* Deleted: `media.json`, `research.json`, `polydesk.json`,
  `mentor_observer.json`, `surgeon_co.json`.
* Kept: `blank.json`, `trading.json` (Bybit-demo, 1 pre-hired CEO on
  `openrouter/anthropic/claude-sonnet-4`, autopsy/post-mortem/
  feedback routines).
* Wizard (`ui/src/components/OnboardingWizard.tsx`) dropdown now has
  exactly two options and the Rule-1 parity mode selector only shows
  when Trading is picked.

#### Verification (2026-04-19, fresh company via Paperclip HTTP API)

```
company: smoke_trading_3d167e
overallStatus = ok
templateId    = trading
steps         = 19 (every step running + ok, 1-9 present)
   [8] hire_agents  ok  hired 1/1 agents

agents: 1
  - CEO  role=ceo  adapter=openclaw_gateway  url=y  token=y
         templateAgent=True  templateRole=ceo
```

All six assertions in `_paperclip_patches/phaseC3_verify.py` passed.

#### Rollback

For Fix A: revert `server/src/routes/agents.ts` (backup at
`agents.ts.bak-<stamp>`) and remove the `EnvironmentFile=` line from
`/etc/systemd/system/paperclip.service`. Agents created before the
patch keep working; agents created after may revert to needing a
manual API key paste.

For Fix B: revert `shared/provisioning/executor.py` and
`templates.py` to the `.bak-<stamp>` siblings under
`/opt/tickles/shared/provisioning/`. Templates hit the old 422/400
errors again but older-style agents created by hand still work.

For Fix C: restore the five deleted template JSONs from their local
copies in `_paperclip_patches/` or from git history; revert the
wizard `OnboardingWizard.tsx.bak-<stamp>`.

---

## Building Phase 5 — OpenClaw visibility + full 8-file overlay + Services-vs-Agents (landed 2026-04-19)

**Why this phase exists.** Phase 4A got us to "agent rows exist in Paperclip
and a folder exists under `/root/.openclaw/agents/`", but the OpenClaw
control-UI dropdown at `http://100.71.74.12:18789/agents/` still only
listed the four originals (`main`, `cody`, `schemy`, `audrey`). From the
user's perspective: "I created TradeLab, my CEO exists, but OpenClaw acts
like he doesn't. Also — how do I tell this agent what to do, and what's
the clean line between an always-on watcher and a reasoning agent?"

### 5.1 Root-cause — OpenClaw has TWO registries, we populated one

| Registry | Path | Drives | Populated by Phase 4A? |
|---|---|---|---|
| on-disk folders | `/root/.openclaw/agents/<id>/` | gateway chat route `/agents/<id>/` | ✅ yes |
| GUI dropdown list | `/root/.openclaw/openclaw.json` → `agents.list[]` | OpenClaw control-UI `/agents` selector | ❌ no |

So the folder existed, the gateway would let the user deep-link to it, but
the dropdown couldn't enumerate it because the JSON registry was not
upserted. The GUI's 8 tabs (AGENTS/SOUL/TOOLS/IDENTITY/USER/HEARTBEAT/
BOOTSTRAP/MEMORY) read optional markdown overlay files per-agent; Phase 4A
only wrote two of them (`AGENT.md`, `HEARTBEAT.md`).

### 5.2 What Phase 5 added

1. **`_openclaw_register_in_registry(...)`** in `shared/provisioning/
   executor.py` reads `openclaw.json`, writes a timestamped backup
   (`openclaw.json.bak.phase5-<ISO>`), and upserts an entry into
   `agents.list[]` keyed by `global_url_key` (e.g. `tradelab_ceo`).
   Entry shape matches the existing four agents:

   ```json
   { "id": "tradelab_ceo",
     "model": {"primary": "openrouter/anthropic/claude-sonnet-4",
                "fallbacks": []},
     "tools": {"alsoAllow": ["lcm_describe","lcm_expand","lcm_grep",
                               "agents_list"]},
     "paperclip": {"companySlug":"tradelab","role":"ceo","urlKey":"ceo"} }
   ```

   `heartbeat` is intentionally omitted on create — see section 5.5.

2. **`_openclaw_customize(...)` now writes 8 overlay markdown files** +
   `meta.json`, each with a `<!-- generated-by: shared/provisioning/
   executor.py / phase5 -->` header:

   | File | Purpose |
   |---|---|
   | `AGENT.md` | High-level identity + read-order pointer |
   | `SOUL.md` | Persona / voice rules |
   | `IDENTITY.md` | companyId, agentId, slug, model, budget, reports-to |
   | `TOOLS.md` | MCP tool catalogue (grouped) + declared skills |
   | `USER.md` | Who the human user is + how to address them |
   | `HEARTBEAT.md` | 7-step per-tick checklist |
   | `BOOTSTRAP.md` | 5-step first-run checklist |
   | `MEMORY.md` | 3-tier mem0 contract + runnable examples |
   | `meta.json` | Machine-readable wiring (companyId/agentId/etc.) |

   `_write_overlay_if_allowed(...)` preserves files that DON'T have our
   header — so hand-edits in the OpenClaw UI survive backfills. Passing
   `force_overwrite=True` only regenerates files we own.

3. **`_paperclip_patches/phase4a_backfill.py` rewritten** to reuse the
   canonical executor helpers (no duplicate logic). Runs against every
   `openclaw_gateway` agent — confirmed against 8 live agents on 2026-04-19
   (4 × Tickles n Co, 3 × Building, 1 × TradeLab). Result: 12 entries now
   in the GUI dropdown (4 original + 8 ours).

### 5.3 TRA-1 mandate + CEO first-shift artifact

**Issue:** `TRA-1` "First shift — full-cycle introspection & market
probe" was created against `companyId=25c28438-.../TradeLab` and assigned
to `agentId=0aff984d-.../CEO`. Mandate = 10 mandated tool calls + 3-bullet
success summary + risk note + ask.

**Artifact:** `shared/artifacts/tradelab_ceo_firstrun.md` captures the
MCP-surface simulation — i.e. what the CEO will see when it runs TRA-1.
Real findings:

| Tool | Result | Notes |
|---|---|---|
| `ping` | ✅ real | pong + ts |
| `agent.get` | ❌ 404 | tool hits `/api/companies/{cid}/agents/{aid}` which doesn't exist; correct route is `/api/agents/{aid}`. Queued for Phase 6. |
| `banker.snapshot` | ✅ real | cost / finance / byAgent present, zero (expected — day-0 company) |
| `catalog.list` | stub | `count=0`, honest Phase-2.5 message |
| `md.quote` | stub | Phase-2.5 market-data gateway pending |
| `treasury.evaluate` | stub | echoes request, default-deny posture documented |
| `execution.submit` | skipped | mandate forbids live submission in Phase 5 |
| `memory.add` tier=agent | ✅ real | forwards to user-mem0, namespace + ids round-trip |
| `feedback.prompts` | ✅ real | Twilly 01/02/03 templates render in full |
| `autopsy.run` | ⚠️ schema mismatch | expects `tradeId`, not inline trade object; mandate adjustment queued |
| `memory.search` tier=agent | ✅ real | forward_to payload echoes the same ids |

**Honest summary:** ~50% of the CEO's tool surface is real today; the rest
are honest stubs pointing at Phase 2.5. Backtest + 15-min parity check
(TRA-1.b) not executable in Phase 5 because `backtest.submit` does not
exist — queued as a Phase 6 line item.

### 5.4 Architectural one-pager — Services vs Agents

**The mental model.**

| Layer | What it is | Always-on? | Uses LLM? | Cost / tick | Example |
|---|---|---|---|---|---|
| **Service** | Deterministic background daemon (systemd unit under `shared/services/<name>/`) | Yes | No | ~$0 | Market watcher, Rule-1 auditor |
| **Agent** | LLM reasoner under Paperclip's `agents` table, with an on-disk OpenClaw overlay dir | No — triggered | Yes | Tokens | CEO, Strategist, Scout, Janitor |

**Why you (almost) never want an always-on LLM agent.** Ticking every N
seconds wastes tokens on nothing-changed ticks, adds 8-30s of latency
when something DOES change, and gives the model no fresh info most of the
time. An LLM agent is a **decision engine** — it should only be woken by
an event, a heartbeat, or a user message. The continuous observation goes
in a service.

**Services today:**

| Service | Unit | Purpose |
|---|---|---|
| `tickles-mcpd` | systemd | MCP control-plane (35 tools live on :7777) |
| `tickles-cost-shipper` | systemd | Paperclip LLM-cost events → `shared.cost` finance_events |

**Services planned (Phase 6 and later):**

| Service | Trigger | Fires event | Consumer agent(s) |
|---|---|---|---|
| `market-watcher` | WebSocket tick | `md.alert.{symbol}.{kind}` | TradeLab CEO, scouts |
| `rule1-auditor` | every new candle | `rule1.violation.{strategy}` | TradeLab CTO, Strategy Council |
| `regime-classifier` | every 5m candle | `regime.shift.{venue}` | All CEOs subscribed to the venue |
| `crash-guardrail` | on drawdown | `crash.tripwire.{company}` | Every CEO (halt all new positions) |
| `janitor-sweeper` | cron 24h | `janitor.report.daily` | Building Janitor agent |

**Event-bus protocol** (minimum viable, compatible with existing stack):

1. A service detects a condition (e.g. BTC -2% in 1h).
2. It POSTs a webhook to Paperclip at
   `/api/events/publish` with `{topic, payload, subscribers: [agentId]}`.
   (This endpoint lands in Phase 6.1 — currently does not exist; services
   will `INSERT INTO paperclip_events(topic, payload)` directly via DB as
   the interim bridge.)
3. Paperclip's subscriber index finds all agents subscribed to `topic`
   (agents declare `metadata.subscriptions: [...]`), and for each one it
   wakes the OpenClaw gateway with `{reason: "event", event}` as the
   initial chat message.
4. OpenClaw runs the agent's LLM loop, which reads AGENT.md + SOUL.md +
   the incoming event, decides, acts, and returns.

**Why this is the right shape.** Services are cheap, fast, deterministic,
and easy to unit-test. Agents are expensive, slow, non-deterministic, and
invaluable when you actually need judgement. Keep the line sharp: if a
task can be expressed as "on X, emit Y", it's a service. If it's "given
X and the last 3 learnings, decide whether to do Y", it's an agent.

### 5.5 Default agent cadence on create

Per the CEO's Apr 19 guidance: newly-created agents start with
`runtimeConfig.heartbeat.enabled=false` in Paperclip AND no `heartbeat`
key in `openclaw.json`. That means an agent only runs when (a) the user
clicks "Run" in the OpenClaw UI, (b) Paperclip assigns a new issue (and
Phase 6's event-bus fires a wake), or (c) a service emits a subscribed
event. The per-agent settings tab in the OpenClaw UI lets the owner flip
on periodic heartbeats after the fact if they want "wake up and check
things every 30 min".

For "watch this thing constantly" (BTC price, whale wallets, news feed),
the answer is a service under `shared/services/`, not a tight-loop
agent. The service fires events; the agent reasons about the events.

### 5.6 Rollback

For each piece of the phase:

1. **`openclaw.json` registry entries** — `cp
   /root/.openclaw/openclaw.json.bak.phase5-<first-ISO>
   /root/.openclaw/openclaw.json`. Backups are ordered — the earliest
   `.bak.phase5-*` is the pre-Phase-5 state. Eight backups were written
   on 2026-04-19 (one per upsert).
2. **Overlay markdown files** — files carry the generated header, so the
   simplest undo is `sudo rm /root/.openclaw/agents/<id>/{AGENT,SOUL,
   IDENTITY,TOOLS,USER,HEARTBEAT,BOOTSTRAP,MEMORY}.md` for the agents
   you want to reset. Nothing else references them.
3. **Executor code** — prior version archived on the VPS at
   `/opt/tickles/_archive/executor.py.phase4.20260419T232822Z`. To
   roll back: `sudo cp /opt/tickles/_archive/executor.py.phase4.
   20260419T232822Z /opt/tickles/shared/provisioning/executor.py &&
   sudo systemctl restart tickles-mcpd.service`.
4. **TRA-1 issue** — harmless, but if you want it gone:
   `DELETE FROM issues WHERE id='403250f7-aea8-415f-b0c7-97362f80ffe5'`.

### 5.7 Phase 6 follow-ups (queued)

1. Fix `agent.get` MCP tool — it calls the non-existent Paperclip route
   `/api/companies/{cid}/agents/{aid}`; should call `/api/agents/{aid}`.
2. Clarify `autopsy.run` contract — it wants `tradeId` (a row lookup),
   not an inline trade object. Either (a) add an inline-trade shortcut or
   (b) document the "store-then-autopsy" flow clearly in `TOOLS.md`.
3. Wire CCXT-Pro market-data gateway into `md.quote`/`md.candles` (Phase
   2.5 / Phase 17 — see section 12).
4. Add `backtest.submit` MCP tool + live 15-min paper-parity harness.
5. Event-bus protocol from section 5.4 → `/api/events/publish` endpoint +
   subscriber index + gateway-wake bridge.
6. `_undo_step8` rollback should also strip the agent's id from
   `openclaw.json` `agents.list[]` + `tickles-meta-map.json` side-file
   (today it only deletes the on-disk `/root/.openclaw/agents/<id>/`
   dir + paperclip row, leaving orphan registry entries).
7. Structured entry/exit debug logs on the provisioning helpers per
   project rules (`[module.function] params=... -> result=...`).
8. Backfill should resolve soul preset-keys (`apex`/`quant`/etc.) to
   their full text before writing `SOUL.md` — today the backfilled SOUL
   literally reads `apex` because Paperclip stores only the preset key.

### 5.8 Post-deploy hotfix — OpenClaw `openclaw.json` is schema-strict (2026-04-19)

**What broke.** Right after Phase 5c landed, the `openclaw-gateway.service`
crash-looped. The zod schema for `agents.list[]` entries only allows:
`id`, `model`, `heartbeat`, `tools`. My first cut of
`_openclaw_register_in_registry` also wrote a `paperclip` sidecar key
(`{companySlug, role, urlKey}`) onto every entry — which the schema
rejected with `Unrecognized key: "paperclip"`.

**How we fixed it.** Two moves, both idempotent:

1. **Sanitiser on the VPS** — `_paperclip_patches/
   fix_openclaw_json_strip_paperclip.py` reads every entry, pops any
   `paperclip` key into a side-file at
   `/root/.openclaw/tickles-meta-map.json`, writes a timestamped backup
   (`openclaw.json.bak.phase5-<iso>`), and re-writes a clean
   `openclaw.json`. Ran once, gateway booted cleanly (`http=200`,
   `ready (7 plugins)`).
2. **Executor stopped writing the bad key** — `executor.py`'s
   `_openclaw_register_in_registry` now ONLY writes the 4 schema-allowed
   keys into `openclaw.json`, and writes the `{companySlug, role, urlKey}`
   mapping to the side-file `tickles-meta-map.json` (same dir).
   That way new agents never re-introduce the crash.

**Why a side-file instead of changing OpenClaw's schema.** OpenClaw is a
third-party product (v2026.4.9); patching its zod schema would fork it
and break upgrades. The mapping is only useful to *us* (mapping
`tickles-n-co_main` → Paperclip company `tickles-n-co`), so it lives in
*our* file in *our* folder.

**Verification.**
- `sudo python3 /tmp/list_openclaw_agents.py` → 12 agents listed:
  4 legacy (`main`, `cody`, `schemy`, `audrey`) + 4 Tickles n Co + 3
  Building + 1 TradeLab — all schema-clean.
- `curl http://127.0.0.1:18789/agents` → HTTP 200 (after ~15s cold
  start including channels/sidecars).
- `sudo cat /root/.openclaw/tickles-meta-map.json` → 8 entries with
  correct `{companySlug, role, urlKey, updatedAt}` for the Paperclip-
  owned agents (the 4 legacy ones are not listed since they pre-date
  our registry logic).

**Rollback path.** `sudo cp /root/.openclaw/openclaw.json.bak.phase5-
2026-04-19T19-29-17.718Z /root/.openclaw/openclaw.json && sudo -u root
XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart openclaw-gateway.
service`. That restores the registry to its pre-phase-5 shape (4 legacy
agents only). The `tickles-meta-map.json` side-file is safe to leave.

### 5.8.1 Code-review follow-through (2026-04-19, same session)

After 5.8 shipped, I ran a focused code-review pass on `executor.py` +
the two patch scripts. Applied the P0 fix + two P1 quick wins inline
before hand-off:

- **P0 — schema-sanitising merge** (`_openclaw_register_in_registry`).
  The previous version did `{**existing, **entry}` on upsert, which
  preserved any stray non-schema keys on the existing record (e.g. a
  leftover `paperclip` from a pre-fix run). New version filters the
  existing record through an explicit allow-list `{"id", "model",
  "heartbeat", "tools"}` BEFORE overlaying our fresh entry, so the
  openclaw.json schema invariant is self-healing. Verified by re-running
  `phase4a_backfill.py` — 12/12 agents still schema-clean, gateway stays
  at `http=200`, `ready (7 plugins)`.
- **P1 — side-file rollback symmetry.** `tickles-meta-map.json` now
  also gets a `*.bak.phase5-<iso>` backup before mutation, matching the
  `openclaw.json` backup policy so rollback covers both files.
- **P3 — `asyncio.get_event_loop()` → `get_running_loop()`** in
  `rollback()` (deprecated since Python 3.10; already inside an `async
  def`, so the swap is safe and silences the DeprecationWarning).

Review findings left for Phase 6 (non-blocking): (F5) `_undo_step8`
should also remove the agent's id from `openclaw.json` + side-map on
rollback (today it only deletes the on-disk dir + paperclip row); (F7)
sprinkle entry/exit debug logs with params + return values per the
project rules; (F9) patch scripts still use `datetime.utcnow()`
(deprecated 3.12 — cosmetic, already-ran scripts); (F10) three
short-lived patch scripts use `open()` without a context manager;
(F12) `phase4a_backfill.http()` catches `HTTPError` but not `URLError`.

### 5.9 Final Phase-5 deliverables

- [x] **12 agents visible in `/agents` dropdown** (4 legacy + 8 new).
- [x] **All 8 overlay files + `meta.json` present** on disk for every
  new agent (verified by `ls -la /root/.openclaw/agents/tradelab_ceo/`
  and `building_ceo/`).
- [x] **`openclaw.json` schema-clean** (gateway `ready`, `http=200`).
- [x] **`tickles-meta-map.json` side-file** carries the Paperclip
  mapping (8 entries).
- [x] **`TRA-1` mandate created + simulated end-to-end** — full
  transcript in `shared/artifacts/tradelab_ceo_firstrun.md` (11/11
  mandated MCP tools exercised; 7 work, 2 stubs with clear Phase-2.5
  plan, 2 real tool bugs queued into 5.7).
- [x] **Services vs Agents architecture doc + event-bus protocol**
  landed in section 5.4.
- [x] **Rollback steps** documented per changed artifact.
- [ ] Phase 5f **human-in-the-loop** — take a screenshot of the
  `/agents` page after logging into OpenClaw Control UI and drop it into
  `shared/artifacts/openclaw_agents_dropdown.png` so future you has a
  visual receipt. (Scripting a Playwright login through OpenClaw's
  device-pair auth is in-scope for Phase 6.)

---

*End of ROADMAP_V3.md. Platform Phases 13-39 frozen; Building Phase 3 landed 2026-04-19 and unblocks Building Phases 4-9 (skills install, three-tier memory, learning loop, holding wallets, shareholder dashboard, first live tenant). Building Phase 5 landed 2026-04-19 (incl. 5.8 schema hotfix + 5.9 deliverables); Building Phase 6 follow-ups queued in section 5.7.*
