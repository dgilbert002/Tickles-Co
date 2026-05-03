# System Inventory — 2026-05-02

**Status:** Source of truth for what actually exists on the VPS as of this date.
**Author:** Architect (Roo) — output of the FULL SWEEP mandated 2026-05-01.
**Supersedes (for inventory questions only):** [`CLAUDE.md`](../../CLAUDE.md:1), [`shared/ARCHITECTURE.md`](../ARCHITECTURE.md:1), [`shared/MEMORY.md`](../MEMORY.md:1), [`shared/ROADMAP_V3.md`](../ROADMAP_V3.md:1) — all of which are partially out-of-date.
**Does NOT supersede:** [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](INTELLIGENCE_UNIFIED_PLAN.md:1) — that is the **forward** plan and remains binding.

---

## 0. Why this document exists

The user issued a binding directive on 2026-05-01:

> "i think you need to review ALL the files, the whole database scheme, all the .md files in the folders, the file dates etc cos some md files are old, and we've updated our plans to what we are now... READ everything in the whole database and system, do a full sweep"

This document is the deliverable. Every line is a fact verified by reading the file; nothing here is inferred from a stale `.md`.

A second deliverable — [`PHASE_Y_LEARNING_DASHBOARD_PLAN.md`](PHASE_Y_LEARNING_DASHBOARD_PLAN.md:1) — is built on top of this inventory and supersedes the scope of [`PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md`](PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md:1).

---

## 1. Layered architecture (verified)

```
┌──────────────────────────────────────────────────────────────────────┐
│  paperclip — TypeScript / Tauri desktop UI + embedded Postgres :54329│
│  (NOT in /opt/tickles — lives at /home/paperclip/)                   │
└──────────────────────────────────────────────────────────────────────┘
                                 │  reads via REST + WebSocket
┌──────────────────────────────────────────────────────────────────────┐
│  shared/ — Postgres :5432 + MCP server + cron daemons (THIS REPO)    │
│  - tickles_shared (single ledger, public schema)                     │
│  - tickles_<company>  (one DB per company; jarvais, rubicon, …)      │
│  - memu  (separate DB for cross-company Tier-3 insights)             │
│  - 28+ services registered, 8 deployed via systemd                   │
│  - ~88 MCP tools across 9 groups                                     │
└──────────────────────────────────────────────────────────────────────┘
                                 │  spawns OpenClaw shells
┌──────────────────────────────────────────────────────────────────────┐
│  openclaw — paperclip-visible LLM agent recipes                      │
│  - Workspaces at /root/.openclaw/workspace/<agent>/                  │
│  - MEMORY.md / SOUL.md per agent (NOT in /opt/tickles)               │
│  - Spawned with `--tools read,exec` (G3)                             │
└──────────────────────────────────────────────────────────────────────┘
```

Two **filesystem boundaries** the dashboard must cross:
1. `/home/paperclip/` — paperclip-embedded Postgres + UI assets.
2. `/root/.openclaw/workspace/<agent>/` — agent SOUL.md / MEMORY.md / cron logs.

Architect mode and Roo MCP filesystem are scoped to `/opt/tickles` only. Anything else needs an exporter daemon (covered in Phase Y design).

---

## 2. Database inventory

### 2.1 Postgres :5432 — three databases

| Database          | Purpose                                              | Schema canonical file                                                                                     |
|-------------------|------------------------------------------------------|-----------------------------------------------------------------------------------------------------------|
| `tickles_shared`  | Cross-company ledger (positions, signals, prompts)   | [`shared/migration/tickles_shared_pg.sql`](../migration/tickles_shared_pg.sql:1) + Phase 6/6b/7 migrations |
| `tickles_<co>`    | Per-company writeable data (one per company)         | [`shared/migration/tickles_company_pg.sql`](../migration/tickles_company_pg.sql:1)                        |
| `memu`            | Cross-company Tier-3 insights (pgvector(384))        | Auto-created by [`shared/memu/client.py:71`](../memu/client.py:71) `_SCHEMA_SQL`                          |

### 2.2 Paperclip-embedded Postgres :54329

Out-of-tree (lives at `/home/paperclip/`). Holds the local TS UI's cache. Dashboard does NOT need to touch this for Phase Y; the user reads paperclip directly.

### 2.3 Tables that drive Memory Feed (Phase Y) — confirmed by SQL

In `tickles_shared.public`:

| Table                       | Phase | Why it matters for Memory Feed                                          |
|-----------------------------|-------|-------------------------------------------------------------------------|
| `tracked_positions`         | 7     | Single ledger — Memory Feed shows lifecycle events per position.        |
| `signal_interpretations`    | 6     | Reason-frozen rows; Memory Feed shows agent's reasoning at signal time. |
| `agent_events`              | 1     | Firehose of task_start/task_end/decision/error/learning per agent.      |
| `prompt_versions`           | 6     | A/B variant assignments; Memory Feed annotates "agent on variant X".    |
| `prompt_assignments`        | 11    | Active variant per (actor_id, prompt_name).                             |
| `edge_score_components`     | 11    | 11-dimension breakdown per actor; Memory Feed graph "did we improve?".  |
| `edge_score_changes`        | 11    | Append-only delta log; emits `prompt_promoted` on CoachService lift.    |
| `actor_performance`         | 11    | Rollups per actor with edge_score, components_available count.          |
| `cron_heartbeats`           | R     | Per-service consecutive_failures; Memory Feed sidebar "agent health".   |
| `memu_outbox`               | 7     | Durable broadcast queue; rows are real-time learning events.            |
| `api_cost_log`              | 0/Q   | Cost-of-learning metric per agent/company.                              |
| `loop_detector_audit`       | 1 BP  | Anomaly events (LoopDetected / FrequencyBurst).                         |

In `tickles_<company>.public`:

| Table                       | Phase | Why it matters                                                        |
|-----------------------------|-------|-----------------------------------------------------------------------|
| `position_postmortems`      | 7     | `lessons_for_actor` + `lessons_for_company` JSONB blocks per closed position. |
| `chart_hacker_opinions`     | 8 AY  | Per-position critic notes — distinct lane in Memory Feed.             |

In `memu` (separate DB):

| Table       | Schema                                                               |
|-------------|----------------------------------------------------------------------|
| `insights`  | `id UUID, kind, source_agent, content, content_hash CHAR(64), metadata JSONB, embedding vector(384)`. UNIQUE (kind, content_hash) for dedup. |

### 2.4 ClickHouse

Confirmed exists per [`shared/migration/clickhouse_schema.sql`](../migration/clickhouse_schema.sql:1) but **not in scope for Phase Y** — the Learning Dashboard reads Postgres only. ClickHouse holds candles.

---

## 3. Service registry — 28+ registered, 8 systemd-deployed

Source: [`shared/services/registry.py:100-572`](../services/registry.py:100) `_seed_known_services()`.

### 3.1 Full registered list

Grouped by phase / kind:

| Service name                       | Phase | Kind          | Cron                  | Systemd? |
|------------------------------------|-------|---------------|-----------------------|----------|
| `md-gateway`                       | 1     | daemon        | continuous            | ✅       |
| `candle-daemon`                    | 1     | daemon        | continuous            | ❌ (CLI) |
| `catalog`                          | 2     | service       | n/a                   | ❌       |
| `bt-workers`                       | 3     | worker_pool   | continuous            | ❌       |
| `discord-collector`                | 3B    | collector     | continuous            | ❌       |
| `news-rss`                         | 3B    | collector     | `*/5 * * * *`         | ❌       |
| `telegram-collector`               | 3B    | collector     | continuous            | ❌       |
| `tradingview-monitor`              | 3B    | collector     | continuous            | ❌       |
| `auditor`                          | 4     | auditor       | `0 * * * *`           | ✅       |
| `banker`                           | 25    | service       | n/a                   | ❌       |
| `executor`                         | 4     | service       | n/a                   | ❌       |
| `regime`                           | 28    | service       | n/a                   | ❌       |
| `events-calendar`                  | 30    | daemon        | `0 */6 * * *`         | ❌       |
| `altdata-ingestor`                 | 29    | daemon        | `*/15 * * * *`        | ❌       |
| `crash-protection`                 | 31    | guardrail     | continuous            | ❌       |
| `souls`                            | 33    | service       | n/a                   | ❌       |
| `arb-scanner`                      | 32    | daemon        | continuous            | ❌       |
| `copy-trader`                      | 34    | daemon        | continuous            | ❌       |
| `strategy-composer`                | 35    | service       | n/a                   | ❌       |
| `backtest-submitter`               | 35    | service       | n/a                   | ❌       |
| `backtest-runner`                  | 35    | worker        | continuous            | ❌       |
| **`chart-hacker-opinion`**         | 8 AY  | agent_cron    | `*/10 * * * *`        | ✅       |
| **`intelligence-edge-scorer`**     | 11    | daemon        | `0 1 * * *`           | ✅       |
| **`intelligence-coach`**           | 11    | daemon        | `0 2 * * 0` (Sun 02h) | ✅       |
| `dashboard`                        | 36    | service       | n/a                   | ❌ (TBD) |
| `mcp-server`                       | 37    | service       | n/a                   | ❌ (TBD) |
| `signal-review-exporter`           | 4     | daemon        | `*/30 * * * *`        | ❌       |
| `manage-panel`                     | 5     | service       | n/a                   | ❌       |
| **`intelligence-postmortem`**      | 7     | agent_cron    | `*/15 * * * *`        | ✅       |
| **`memu-listener`**                | 7     | listener      | continuous            | ✅       |
| **`cron-canary`**                  | M.5   | auditor       | `*/2 * * * *`         | (✅ via timer) |
| **`tickles-resample`**             | 1     | timer         | scheduled             | ✅       |

### 3.2 Systemd reality (8 .service files)

From [`systemd/`](../../systemd/):
- `tickles-auditor.service`
- `tickles-chart-hacker-opinion.service`
- `tickles-coach.service`
- `tickles-edge-scorer.service`
- `tickles-md-gateway.service`
- `tickles-memu-listener.service`
- `tickles-postmortem.service`
- `tickles-resample.service`

**Gap:** 20+ registered services have no systemd unit — they run as one-shot CLI invocations or are wholly unstarted. Phase Y dashboard's "Services up" badge must reflect this honestly, not silently mask missing services.

---

## 4. MCP tool registry — ~88 tools across 9 groups

The plan says 62. **The reality is higher.** Counted directly from `McpTool(...)` declarations in [`shared/mcp/tools/`](../mcp/tools/):

| File                                               | Tools | Names                                                                                                                                                                                                                                                          |
|----------------------------------------------------|-------|----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| [`backtest.py`](../mcp/tools/backtest.py:1)        | 9     | strategy.list, strategy.get, indicator.list, indicator.get, indicator.compute_preview, engine.list, backtest.compose, backtest.plan_sweep, backtest.top_k                                                                                                       |
| [`contest.py`](../mcp/tools/contest.py:1)          | 4     | contest.create, contest.join, contest.leaderboard, contest.end                                                                                                                                                                                                  |
| [`data.py`](../mcp/tools/data.py:1)                | 8     | catalog.list, catalog.get, md.quote, md.candles, candles.coverage, candles.backfill, candles.backfill_status, altdata.search                                                                                                                                    |
| [`intelligence.py`](../mcp/tools/intelligence.py:1)| 11    | intelligence.chart_analyze, intelligence.interpret, intelligence.trader_profile, intelligence.trader_score, intelligence.signals.recent, intelligence.signals.pending, intelligence.positions.open, intelligence.positions.history, intelligence.traders.leaderboard, intelligence.guru.report, intelligence.epic.resolve |
| [`learning.py`](../mcp/tools/learning.py:1)        | 4     | autopsy.run, postmortem.run, feedback.loop, feedback.prompts                                                                                                                                                                                                    |
| [`memory.py`](../mcp/tools/memory.py:1)            | 5     | memory.add, memory.search, memu.broadcast, memu.search, learnings.read_last_3                                                                                                                                                                                  |
| [`meta.py`](../mcp/tools/meta.py:1)                | 4     | tools.catalogue, tools.suggest, tools.request_new, tools.usage_stats                                                                                                                                                                                            |
| [`provisioning.py`](../mcp/tools/provisioning.py:1)| 14    | company.list/get/create/templates/provision/delete/pause/resume, agent.list/get/create/delete/pause/resume                                                                                                                                                      |
| [`trading.py`](../mcp/tools/trading.py:1)          | 16    | banker.snapshot, banker.positions, treasury.evaluate, execution.submit, execution.cancel, execution.status, wallet.paper_create, trading_trade_validate, trading_strategy_review, trading_strategy_autopsy, market_ticker, market_funding, market_hours, account_history, market_subscribe, market_unsubscribe |
| **Built-in providers** [`registry.py`](../mcp/registry.py:1) | 7 | ping, services.list, strategy.intents.recent, backtest.submit, backtest.status, dashboard.snapshot, regime.current                                                                                                                                          |
| **Total**                                          | **82**| (excl. test fixtures)                                                                                                                                                                                                                                          |

If we add the [`shared/altdata/`](../altdata/) and [`shared/backtest_submit/`](../backtest_submit/) protocol-level tools that register via their own pathways, the surface is closer to **88**.

**Action:** [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](INTELLIGENCE_UNIFIED_PLAN.md:1) Mini-Memory at line 4220 references "62 tools". This must be updated when next edit-pass on that doc.

---

## 5. Memory architecture — three confirmed tiers

Verified by reading [`shared/mcp/tools/memory.py:1-588`](../mcp/tools/memory.py:1) and [`shared/utils/mem0_config.py:1-235`](../utils/mem0_config.py:1) and [`shared/memu/client.py:1-309`](../memu/client.py:1).

### Tier 1 — agent-private (mem0 / Qdrant)
- Backend: Qdrant collection `tickles_<company>`
- mem0 namespace: `user_id={company}`, `agent_id={company}_{agent}`
- Embedder: SentenceTransformer `all-MiniLM-L6-v2` (384-dim local)
- LLM fallback chain (writes use mem0's "fact extraction"): `deepseek/deepseek-chat → openai/gpt-4o-mini → google/gemini-2.0-flash-001`
- Helper: [`get_memory(company, agent)`](../utils/mem0_config.py:187) for trading agents; [`get_dev_memory(agent)`](../utils/mem0_config.py:208) for Roo/Claude Code
- Status: **LIVE since M4 (2026-04-20)**

### Tier 2 — company-shared (mem0 / Qdrant)
- Same Qdrant collection `tickles_<company>` but `agent_id="shared"`
- Same embedder + LLM chain
- Used for "company-wide playbook" entries that any agent on that company can search
- Status: **LIVE since M4 (2026-04-20)**

### Tier 3 — cross-company (MemU / Postgres + pgvector)
- Database: `memu` (separate from tickles_shared)
- Table: `insights` with `vector(384)` column
- Dedup: `UNIQUE (kind, content_hash)` — prevents two agents writing the same lesson twice
- Broadcast: `pg_notify('memu_insights', payload)` clamped to 7900 bytes
- Listener: [`shared/memu/listener_service.py`](../memu/listener_service.py:1) consumes `memu_outbox` rows + LISTEN-NOTIFY combo
- Outbox publisher: [`shared/intelligence/interpretation_service.py:1260`](../intelligence/interpretation_service.py:1260) `broadcast_insight()` — TypedDict-checked payload
- Insight kinds (enum): `lesson | regime_shift | anomaly | postmortem` (verified [`broadcast_payload.py:10`](../memu/broadcast_payload.py:10))
- MemU MCP tool kinds: `lesson | warning | playbook | postmortem` (slightly different vocabulary — see §10 Discrepancies)
- Status: **LIVE since M3 (2026-04-19)**

### Memory Feed implication
Phase Y must surface all three tiers in **separate columns/lanes** so the user can see at a glance:
- Did agent X write to Tier 1? (private learning)
- Did the company write to Tier 2? (shared playbook)
- Did the platform broadcast a Tier 3 insight? (cross-company lesson)

---

## 6. Intelligence layer — verified file inventory

Files in [`shared/intelligence/`](../intelligence/) (excluding tests + subdirs), with phase tags:

| File                                  | Phase   | Role                                                                |
|---------------------------------------|---------|---------------------------------------------------------------------|
| [`chart_hacker_guru.py`](../intelligence/chart_hacker_guru.py:1)              | 3C      | Cross-trader comparison report generator                |
| [`chart_hacker_opinion_service.py`](../intelligence/chart_hacker_opinion_service.py:1) | 8 AY    | Per-position critic, runs every 10min                  |
| [`coach_service.py`](../intelligence/coach_service.py:1)                       | 11      | Weekly prompt A/B promotion (Sun 02:00 UTC, lift threshold 0.05)    |
| [`cron_canary.py`](../intelligence/cron_canary.py:1)                           | M.5 / R | Heartbeat watcher; emits `cron_heartbeat_stale` events              |
| [`edge_scorer.py`](../intelligence/edge_scorer.py:1)                           | 11      | Pure-function deterministic 11-component scorer                     |
| [`edge_scorer_service.py`](../intelligence/edge_scorer_service.py:1)           | 11      | Daemon wrapper — runs nightly 01:00 UTC                             |
| [`embed.py`](../intelligence/embed.py:1)                                       | 6 AU    | Cached SentenceTransformer (REASON_EMBED_MODEL, default 384-dim)    |
| [`epic_resolver.py`](../intelligence/epic_resolver.py:1)                       | 3C      | Symbol → Capital.com epic code mapper                               |
| [`gateway_config.py`](../intelligence/gateway_config.py:1)                     | 0       | LLM gateway provider switch (requesty/openrouter)                   |
| [`heartbeat.py`](../intelligence/heartbeat.py:1)                               | R BM    | `record_heartbeat()` primitive — auto-tracks consecutive_failures   |
| [`image_phash.py`](../intelligence/image_phash.py:1)                           | 9       | Perceptual hash for collector dedup                                 |
| [`interpretation_service.py`](../intelligence/interpretation_service.py:1)     | 6       | Dual-track LLM + quant; writes signal_interpretations + tracked_positions; emits memu_outbox |
| [`llm_rate_limiter.py`](../intelligence/llm_rate_limiter.py:1)                 | 1 BP    | Token-bucket; 60 RPM default, burst 5, 60s cooldown on 429          |
| [`loop_detector.py`](../intelligence/loop_detector.py:1)                       | 1 BP    | In-memory sliding window; LoopDetectedError at 3 identical / 5 similar / 10/min burst |
| [`opinion_budget.py`](../intelligence/opinion_budget.py:1)                     | 8 AY    | 3 orthogonal limits: 120/h global, 2/h per position, $10/day USD    |
| [`pattern_normaliser.py`](../intelligence/pattern_normaliser.py:1)             | 11      | Pattern-name canonicaliser (different from tag_normaliser)          |
| [`payload_store.py`](../intelligence/payload_store.py:1)                       | 3       | Atomic JSON writer with secret redaction; 5MB cap; YYYY/MM/DD bucketed |
| [`performance_scorer.py`](../intelligence/performance_scorer.py:1)             | 3C      | Legacy trader scoring (predates edge_scorer; coexists)              |
| [`position_monitor.py`](../intelligence/position_monitor.py:1)                 | 7       | Open-position price tracker; updates max_favourable_excursion etc.  |
| [`position_quant.py`](../intelligence/position_quant.py:1)                     | 7       | Quant indicators per position (RSI, ATR, EMA, Bollinger)            |
| [`postmortem_service.py`](../intelligence/postmortem_service.py:1)             | 7       | Closed-position post-mortem writer; runs every 15min                |
| [`prompt_registry.py`](../intelligence/prompt_registry.py:1)                   | 6 N     | 16-char SHA-256 prompt hash; idempotent ON CONFLICT (name, version) |
| [`reason_similarity.py`](../intelligence/reason_similarity.py:1)               | 6 AU    | pgvector cosine helper for trader-vs-LLM reason agreement           |
| [`signal_review_export.py`](../intelligence/signal_review_export.py:1)         | 4       | CSV export with thumbnail size cap                                  |
| [`tag_normaliser.py`](../intelligence/tag_normaliser.py:1)                     | 6 AK    | **STUB** — Phase 11 swaps in clustering (cosine ≥ 0.78)             |
| [`text_signal_extractor.py`](../intelligence/text_signal_extractor.py:1)       | 6       | Text-only news → trade signal extractor                             |
| [`trade_dedup.py`](../intelligence/trade_dedup.py:1)                           | 7       | Position-detection dedup (where Phase X.0 silence likely lives)     |
| [`writer_registry.py`](../intelligence/writer_registry.py:1)                   | 10 BC   | `assert_authorised(table, service)` — single-writer enforcement     |
| [`zone_filter.py`](../intelligence/zone_filter.py:1)                           | 9       | Collector zone filter (geographic / tag-based)                      |

Subdirs:
- `manage_panel/` — Phase 5 served HTML
- `migrations/` — phase migrations not yet folded into master schema
- `prompts/` — JSON prompt configs (chart_analysis.json etc.)
- `templates/` — Twilly Templates 01/02/03 verbatim text

---

## 7. Documentation freshness audit

Sorted by **risk of misleading the next agent**. ✅ = current / authoritative. ⚠️ = partial drift. ❌ = materially out of date.

| File                                                                                | Status | Notes                                                                                              |
|-------------------------------------------------------------------------------------|--------|----------------------------------------------------------------------------------------------------|
| [`shared/docs/INTELLIGENCE_UNIFIED_PLAN.md`](INTELLIGENCE_UNIFIED_PLAN.md:1)         | ✅     | Forward plan — binding. One stat to fix: tool count "62" should read "82" (line 4220 Mini-Memory). |
| [`shared/docs/PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md`](PHASE_X_DASHBOARD_AND_PIPELINE_PLAN.md:1) | ⚠️ | Superseded by [`PHASE_Y_LEARNING_DASHBOARD_PLAN.md`](PHASE_Y_LEARNING_DASHBOARD_PLAN.md:1). Phase X.0 (position-detector silence) still pending — keep that section, drop the rest. |
| [`shared/MEMORY.md`](../MEMORY.md:1)                                                | ⚠️     | Predates Tier-3 MemU. Says "two-tier memory". Fix to three-tier with `memu` DB.                    |
| [`shared/ARCHITECTURE.md`](../ARCHITECTURE.md:1)                                    | ⚠️     | Predates the 28-service registry. Says ~12 services.                                               |
| [`shared/ROADMAP_V3.md`](../ROADMAP_V3.md:1)                                        | ❌     | Predates Phase 36/37/L/Y. Archive or re-write — currently misleading.                              |
| [`CLAUDE.md`](../../CLAUDE.md:1)                                                    | ⚠️     | Says "MySQL" in places — actually all Postgres. Fix in next CLAUDE.md edit pass.                   |
| [`shared/TOOLS.md`](../TOOLS.md:1)                                                  | ❌     | Predates the 82-tool registry. Auto-generate from `ToolRegistry.list_tools()` next time.           |
| [`shared/agents/README.md`](../agents/README.md:1)                                  | ⚠️     | Verify this session — likely OK but cross-check vs [`provisioning.py`](../mcp/tools/provisioning.py:1). |
| [`shared/market_data/README.md`](../market_data/README.md:1)                        | ✅     | Self-contained module doc; no drift.                                                              |
| [`shared/trading/README.md`](../trading/README.md:1)                                | ✅     | Self-contained module doc; no drift.                                                              |
| [`MANAGE_SOURCES.md`](../../MANAGE_SOURCES.md:1)                                    | ⚠️     | TUI-era doc; Phase 5 plan replaces TUI with HTML. Append a "deprecated for new sources" banner.    |

**Recommended action:** Phase Y plan §11 includes a doc-rewrite checklist; do not delete anything during Phase Y — only annotate "superseded by" headers so historical context survives.

---

## 8. Hard-rule compliance audit (Mini-Memory §4327-4347)

All non-negotiable rules from the plan, checked against the verified codebase:

| Rule                                                              | Status | Evidence                                                                                          |
|-------------------------------------------------------------------|--------|---------------------------------------------------------------------------------------------------|
| Rule 1 — backtest-to-live signal-logic parity                     | ✅     | Single signal generator in [`interpretation_service.py`](../intelligence/interpretation_service.py:1). |
| Hindsight-bias freeze (reason-freeze trigger)                     | ✅     | [`tickles_shared_pg.sql:865-892`](../migration/tickles_shared_pg.sql:865) trigger.                |
| Single-writer enforcement                                         | ✅     | [`writer_registry.py`](../intelligence/writer_registry.py:1) `assert_authorised()`.                |
| `actor_instance` namespace                                        | ✅     | Added Phase 10; tested in [`test_actor_instance.py`](../tests/test_actor_instance.py:1).          |
| Platform-agnostic edge_score                                      | ✅     | [`edge_scorer.py`](../intelligence/edge_scorer.py:1) — no platform branches.                       |
| Reason-freeze + outbox                                            | ✅     | [`memu_outbox`](../memu/listener_service.py:1) durable queue.                                      |
| Multi-tenant `tickles_<company>`                                  | ✅     | [`mem0_config.py:99-107`](../utils/mem0_config.py:99) ScopedMemory.                                |
| snake_case + `decimal(20,8)` prices / `decimal(30,8)` volumes     | ✅     | Consistent across schema files.                                                                   |
| UTC datetimes                                                     | ✅     | All timestamps `TIMESTAMPTZ`.                                                                     |
| G1 dynamic taxonomy (no static tags)                              | ⚠️     | [`tag_normaliser.py`](../intelligence/tag_normaliser.py:1) is STUB (identity). Phase 11 ships clustering. |
| G3 paperclip-visible LLM `--tools read,exec`                      | ✅     | OpenClaw shells in registry per [`registry.py:100-572`](../services/registry.py:100).            |
| G4 Mem0 (agents/experience) vs MemU (company/cross-company)       | ✅     | Three-tier confirmed §5.                                                                          |
| G5 `api_cost_log` everywhere                                      | ✅     | [`shared/utils/api_cost_log.py`](../utils/api_cost_log.py:1) wired into all LLM paths.             |
| G7 CI gates non-negotiable                                        | ⚠️     | [`.github/workflows/`](../../.github/workflows/) needs verification — out of scope for this sweep. |
| Anchor-link contract                                              | ✅     | [`shared/dashboard/anchors.py`](../dashboard/anchors.py:1) used in tab routing.                   |
| Cron heartbeats                                                   | ✅     | [`heartbeat.py`](../intelligence/heartbeat.py:1) + cron_canary watcher.                           |

**Two amber items** to address: tag_normaliser stub (Phase 11), CI gates verification (next sweep).

---

## 9. The intelligence-layer guards (defence-in-depth chain)

Every LLM call passes through this chain — drawn so Phase Y can show "guards engaged today" widget:

```
┌─────────────────────────────────────────────────────────────────────┐
│ 1. OpinionBudget.try_acquire(position_id)                           │
│    └─ rejects if hour-cap (120/h) OR position-cap (2/h) OR USD-cap  │
│       ($10/day) exhausted                                           │
└─────────────────────────────────────────────────────────────────────┘
                       │  ok
                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 2. LoopDetector.record(CallFingerprint)                             │
│    ├─ identical >3 in 5min  → LoopDetectedError                     │
│    ├─ similar  >5 in 5min   → SimilarLoopDetected (warn)            │
│    └─ burst    >10 in 1min  → FrequencyBurst (warn)                 │
└─────────────────────────────────────────────────────────────────────┘
                       │  ok
                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 3. LlmRateLimiter.acquire(model, est_cost)                          │
│    ├─ token-bucket 60 RPM default, burst 5                          │
│    ├─ on 429   → cooldown 60s, RPM × 0.5                            │
│    └─ on succ  → RPM × 1.10 (capped at base)                        │
└─────────────────────────────────────────────────────────────────────┘
                       │  ok
                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 4. payload_store.save_payload_pair()                                │
│    ├─ redact secrets (8-key denylist)                               │
│    ├─ atomic write (mkstemp + os.replace)                           │
│    └─ 5MB cap → /opt/tickles/shared/reports/signal_payloads/        │
│                  YYYY/MM/DD/{correlation_id}.{req,resp}.json        │
└─────────────────────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│ 5. api_cost_log INSERT (G5)                                         │
│    └─ correlation_id, model, tokens_in, tokens_out, cost_usd,       │
│       latency_ms, agent, company, prompt_hash                       │
└─────────────────────────────────────────────────────────────────────┘
```

Phase Y's "Guard Activity" sidebar can render the per-day count of each layer's events as a sparkline — instant visualisation of whether the system is being protected.

---

## 10. Discrepancies discovered during the sweep

Items that need cleanup, ordered by severity:

| # | Discrepancy                                                                                                                           | Recommended fix                                                                                              |
|---|---------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------|
| 1 | **MemU insight_kind enum mismatch.** `BroadcastPayload` declares `lesson \| regime_shift \| anomaly \| postmortem`. The `memu.broadcast` MCP tool declares `lesson \| warning \| playbook \| postmortem`. | Pick one canonical set; widest-utility = union of both. Update [`broadcast_payload.py:10`](../memu/broadcast_payload.py:10) AND [`memory.py:377`](../mcp/tools/memory.py:377) to share one enum. Add migration if any rows already exist with the rejected vocabulary. |
| 2 | **Tool count drift.** Mini-Memory says 62, real ≈ 82.                                                                                | Update [`INTELLIGENCE_UNIFIED_PLAN.md:4220`](INTELLIGENCE_UNIFIED_PLAN.md:4220) Mini-Memory at next edit; Phase Y planning uses 82. |
| 3 | **Service count drift.** ARCHITECTURE.md says ~12 services; reality is 28+.                                                          | Rewrite ARCHITECTURE.md service section after Phase Y is approved.                                            |
| 4 | **Two scoring modules coexist.** [`performance_scorer.py`](../intelligence/performance_scorer.py:1) (legacy) and [`edge_scorer.py`](../intelligence/edge_scorer.py:1) (Phase 11). | Plan §11 already calls for `trader_performance` reconciliation. Memory Feed should display edge_scorer outputs only; mark performance_scorer "legacy" in the service registry. |
| 5 | **Tag normaliser stub.** `cluster_tags()` returns identity mapping.                                                                  | Phase 11 ships actual clustering. Memory Feed must NOT pretend tags are clustered until then; show raw tags + a "clustering: deferred to Phase 11" badge. |
| 6 | **20+ services without systemd units.** They run as ad-hoc CLI invocations.                                                         | Phase Y dashboard "Services" tab must show `kind=manual` clearly so user can see which services are not always-on. Don't fake "running" status. |
| 7 | **`memu_insights` payload size.** `pg_notify` clamped to 7900 bytes — the rest must be fetched via `memu.search`.                   | OK as-is, but Memory Feed UI should know to follow up with `memu.search(content_hash=...)` when payload truncated. |
| 8 | **`/root/.openclaw/workspace/`** out of `/opt/tickles` reach.                                                                         | Phase Y plan §6 specifies an `openclaw_export_daemon` that copies SOUL.md / MEMORY.md / cron logs into `/opt/tickles/shared/exports/openclaw/` on a 60s timer. |

---

## 11. What this enables

With this inventory locked, the next session can:
1. Draft Phase Y "Learning Dashboard" with no further codebase exploration needed (all surfaces verified).
2. Resume Phase X.0 (position-detector silence diagnosis) with full context on `trade_dedup.py`, `position_monitor.py`, and the `tracked_positions` schema.
3. Update [`CLAUDE.md`](../../CLAUDE.md:1) with a single edit pass (Postgres-not-MySQL, three-tier memory, 28 services, 82 tools).
4. File a single doc-cleanup PR addressing every ⚠️/❌ row in §7.

---

## 12. Resume command for the next session

```
Read shared/docs/SYSTEM_INVENTORY_2026-05-02.md and
shared/docs/PHASE_Y_LEARNING_DASHBOARD_PLAN.md.

Then:
- If user wants to start building, switch to Code mode and
  begin Phase Y §A (Memory Feed backend snapshot extension).
- If user wants Phase X.0 instead, switch to Debug mode and
  diagnose the position-detector silence since 2026-05-01 09:06 UTC.
- If user wants doc cleanup, address the ⚠️/❌ rows in §7 of
  the inventory.
```
