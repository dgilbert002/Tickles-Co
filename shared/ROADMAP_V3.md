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

*End of ROADMAP_V3.md. Phase 27 (Regime Service) is next.*
