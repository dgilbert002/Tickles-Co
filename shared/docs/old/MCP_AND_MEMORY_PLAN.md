# MCP & Memory Implementation Plan — Tickles & Co

**Status:** M0 + M0.5 + M3 + M4 DONE and VERIFIED. M5 preflight done (stdio+http MCP now both expose 41 tools). **M4.5 Step 2 executed on surgeon2** — tool allow-list mechanism proven. NEW BLOCKER: 60s MCP cold-start timeout on first `memory.*` call per agent per daemon-restart. See §Changelog / M4.5b.
**Branch:** `mcp`
**Owners:** CEO (approval) + AI (implementation)
**Last updated:** 2026-04-20 (post-M4.5 Step 2 on surgeon2)

---

## 0. Preface — what this document is, and what it is not

This is a **plan**. It's a shopping list of phases we agree on before any code is written. Every phase ends with a "rollback" note so if a change breaks something, we can undo it without panic.

I'm going to explain everything the way I'd explain it to a 21-year-old who has never built this kind of system before — no jargon for its own sake.

> The agents' job: make paper money, learn, then earn trust to do more.
> Our job: give them the cleanest possible toolbox (MCP) and the cleanest possible brain (3-tier memory).

---

## 1. What already exists (reality check, not aspiration)

I read the codebase end-to-end before writing this. Here is the honest state of what's shipped right now on `main` (@ `2c86995`):

### 1.1 MCP server — already working

- `shared/mcp/server.py` — JSON-RPC 2.0 dispatcher, audit-logs every tool call into Postgres `mcp_invocations`.
- `shared/mcp/registry.py` — the in-memory tool index. Tools have `name`, `description`, `version`, `input_schema`, `output_schema`, `read_only`, `tags`, `enabled`.
- `shared/mcp/transports/{http,stdio}.py` — two ways to reach the server.
- `shared/mcp/store.py` — the audit writer backing `mcp_tools` + `mcp_invocations` tables (Phase 37, already migrated on VPS).

**Implication:** the "massive dictionary" you want (100-400 discoverable tools) does not need a new server. The server is already there, already audits everything, already exposes `tools/list` so an agent can enumerate what it has. We just need to **register more tools** into it.

### 1.2 Tool surface today — 30 tools across 5 groups

Grouped per the CEO rule ("one file per group, not per tool"):

| File | Group | Tools | Status |
|---|---|---|---|
| `shared/mcp/tools/provisioning.py` | provisioning | 14 tools: `company.{list,get,create,delete,pause,resume,templates,provision}`, `agent.{list,get,create,delete,pause,resume}` | **All wired** to Paperclip HTTP API. `company.create` even chains into the 9-step provisioning executor. |
| `shared/mcp/tools/data.py` | data | 5 tools: `catalog.{list,get}`, `md.quote`, `md.candles`, `altdata.search` | `catalog.*` wired to Paperclip. `md.*` + `altdata.*` return `status: not_implemented` — **biggest unstub target.** |
| `shared/mcp/tools/trading.py` | trading | 6 tools: `banker.{snapshot,positions}`, `treasury.evaluate`, `execution.{submit,cancel,status}` | Only `banker.snapshot` reads live Paperclip finance data. The rest are stubs. |
| `shared/mcp/tools/memory.py` | memory | 5 tools: `memory.{add,search}`, `memu.{broadcast,search}`, `learnings.read_last_3` | `memory.*` + `learnings.*` work as "forward-to-mem0" hints (the host MCP client forwards the call to `user-mem0`). `memu.*` are stubs. |
| `shared/mcp/tools/learning.py` | learning | 4 tools: `autopsy.run`, `postmortem.run`, `feedback.loop`, `feedback.prompts` | All wired — they render Twilly's Template 01/02/03 prompts with schema-locked memory-write hints. |
| `shared/mcp/registry.py` (built-ins) | diagnostic | 7 tools: `ping`, `services.list`, `strategy.intents.recent`, `backtest.{submit,status}`, `dashboard.snapshot`, `regime.current` | All wired. `backtest.submit` already routes to the Phase-35 submission layer. |

**Total: 41 tools** (not 30 — I miscounted above; let me recount: 14 prov + 5 data + 6 trade + 5 mem + 4 learn + 7 builtins = **41**). Of those, **30 return real data** and **11 are stubs** (the 5 `md.*`/`altdata.*` in `data.py` minus `catalog.*`, plus 5 of the 6 in `trading.py`, plus 2 `memu.*` in `memory.py`).

### 1.3 Data plane — what's under the hood

| System | Where | Status |
|---|---|---|
| **MySQL `tickles_shared`** | `shared/migration/*.sql`, `candles` + `instruments` tables | Running on VPS. Candle pipeline writes here with SHA-256 drift detection. |
| **Postgres `tickles_shared_pg`** | Phase 22-37 tables | Running on VPS. Owns `mcp_tools`, `mcp_invocations`, `orders`, `fills`, `backtest_submissions`, `agent_decisions`, `strategy_intents`, etc. — 13 phases of schema. |
| **Postgres `memu`** | `shared/memu/client.py` | **Code is production-grade** (thread-safe embedder, dedup by content_hash, auto-reconnect, pg_notify broadcast). But the architecture doc says "MemU Postgres DB not created; pgvector not installed" — we need to **verify** which is true on the live VPS. |
| **Qdrant** | `localhost:6333` | Running. Used by `ScopedMemory` in `shared/utils/mem0_config.py`. Collection-per-company: `tickles_{company}`. |
| **Redis** | | Running. Used as: (a) backtest job queue, (b) heartbeat TTL for worker leases, (c) online feature store (Phase 20). |
| **ClickHouse** | `backtests` database | Running. Idempotent writes from `shared/backtest/ch_writer.py` keyed on `param_hash`. |

### 1.4 Candle pipeline — already has check-and-backfill

- `shared/market_data/candle_service.py` — `CandleService.collect_candles()` hashes every candle, writes with `ON DUPLICATE KEY UPDATE`, logs drift when hash changes.
- `shared/market_data/gap_detector.py` — `GapDetector.find_gaps()` uses MySQL `LEAD()` window function, per-timeframe thresholds (e.g. 1m gap = >2min between rows), returns typed `Gap` objects.
- `shared/market_data/retention.py` — data retention policy.
- `shared/market_data/timing_service.py` — market-hours awareness for CFDs (crypto is 24/7 so mostly passthrough).

**Implication:** your earlier ask — "if candles are missing, collectors should backfill them" — is **already the design**. The gap detector + collectors + candle service are plumbed. What's missing is the **MCP-level tool** that exposes this to agents. A single `candles.coverage(symbol, timeframe, from, to)` tool plus `candles.backfill(symbol, timeframe, from, to)` tool would turn it into a shopping-list item.

### 1.5 Backtest pipeline — also production-grade

- `shared/backtest/worker.py` — BLMOVE Redis queue (reliable claim), `reclaim_orphans()` on startup, background heartbeat thread, transient-vs-permanent error classification.
- `shared/backtest/engine.py` — `BacktestConfig` dataclass + `run_backtest()` pure function.
- `shared/backtest/engines/{classic,nautilus_adapter,vectorbt_adapter}.py` — **three pluggable engines**. You already have this for the "millions of backtests, surface the top results" feature you mentioned.
- `shared/backtest/indicators/{core,extras,pandas_ta_bridge,crash_protect,smart_money}.py` — 5 indicator modules.
- `shared/backtest/ch_writer.py` — idempotent ClickHouse writer keyed on `param_hash`.
- `shared/backtest/queue.py` — queue abstraction.
- `shared/backtest/parity.py` — engine-to-engine parity tester (compares classic vs nautilus results on same inputs).
- `shared/backtest/accessible.py` — surface layer (Phase 35 submitter).

**Implication:** the backtest engine is so feature-rich that the gap is purely **discoverability**. Agents can't see the list of indicators, strategies, engines, or compose a backtest spec conversationally — there are no MCP tools for that yet.

### 1.6 Memory infrastructure — 3-tier as specified

Per `.cursor/rules/tickles-co-architecture.mdc`:

| Tier | Purpose | Store | Keys |
|---|---|---|---|
| 1 | Agent-private lessons | Qdrant via mem0 | `collection=tickles_{company}`, `user_id={company}`, `agent_id={company}_{agent}` |
| 2 | Company-shared facts & trades | MySQL `tickles_{slug}` + Qdrant | `user_id={company}`, `agent_id=shared` |
| 3 | Cross-company insights | Postgres `memu` + pgvector + `pg_notify` | global, content-hash deduped |

**What's wired right now:**
- **Tier 1/2 (mem0)**: `memory.add` / `memory.search` MCP tools return a "forward-to-user-mem0" envelope. The calling host (Cursor/OpenClaw) must have `user-mem0` MCP registered as a client. Per the architecture gap list, **OpenClaw doesn't have the mem0 MCP registered yet** — this is Gap 6.
- **Tier 3 (MemU)**: code exists (`shared/memu/client.py`, production-quality), MCP tools (`memu.broadcast`, `memu.search`) are stubs, and architecture doc says the Postgres DB + pgvector extension may not be installed on VPS. Need verification.

### 1.7 Services still "not enabled on VPS"

From `shared/docs/SERVICES.md`, these are **shipped but inactive** (systemd units staged, not started):

- `mcp-server` (Phase 37) — the MCP daemon itself!
- `banker`, `executor`, `regime`, `crash-protection`, `altdata-ingestor`, `events-calendar`, `souls`, `arb-scanner`, `copy-trader`, `strategy-composer`, `backtest-runner`, `backtest-submitter`, `dashboard`
- All 4 collectors (`discord`, `news-rss`, `telegram`, `tradingview-monitor`)

**That's 17 services** waiting for someone to `systemctl enable --now tickles-<name>.service`.

---

## 2. Target outcome (what "done" looks like)

After the phases in Section 4 complete:

1. **The `mcp-server` systemd unit is running** on the VPS and accepts tool calls from OpenClaw agents via stdio or HTTP.
2. **The MCP tool registry has ≥ 100 tools** (I think 120-180 is achievable without inventing fantasy tools; hitting 400 needs a deliberate "category tree" exercise we'd do in Phase M9 — see open questions).
3. **Every tool in the registry is discoverable** via `tools/list`. Tools are tagged (`group`, `kind`, `status`, `destructive`, `phase`) so agents can filter — e.g. "show me all read-only data tools tagged `markets`".
4. **Agents can run real backtests over MCP**: one tool call to `candles.coverage`, a backfill if needed, submit a spec, poll status, read ClickHouse results. No Python imports, no paths, no subprocess. All over `tools/call`.
5. **Three-tier memory is live**: agents store Tier-1 lessons after every decision (required by SOUL.md), Tier-2 company facts accumulate, MemU broadcasts cross-company insights via `pg_notify`.
6. **A paper-trading competition can be declared** — an MCP tool `contest.create(venues=[...], coins=[...], duration=7d, paper_usd=10000)` wires up N agents with paper wallets, point them at `md.*` + `execution.submit(dryRun=true)`, and the dashboard shows a leaderboard.
7. **Agents can escalate** — when an agent inspects a tool's schema and decides it's missing a field, a call to `tools.request_new(description, example_use)` creates a Paperclip issue for the CEO to review.

---

## 3. Where the 100+ tools actually come from (no fantasy, all grounded)

Counting groups honestly, assuming we register every existing code capability as a first-class tool:

| Group | Today | Phase end target | Source of new tools |
|---|--:|--:|---|
| `provisioning.*` (company/agent lifecycle) | 14 | 22 | + `template.create`, `template.list`, `template.get`, `routine.{list,create,update,delete}`, `holding_wallet.{list,grant}` |
| `data.*` (market data / catalog / altdata) | 5 | 25 | Unstub `md.{quote,candles}`; add `candles.{coverage,backfill,backfill_status}`, `instrument.{list,add}`, `venue.{list,caps}`, `altdata.{search,by_source,latest}`, `news.{list,by_symbol}`, `social.{discord,telegram,twitter}.recent`, `onchain.{whale_flows,top_wallets,wallet_trades}` |
| `trading.*` (execution, treasury, banker) | 6 | 30 | Unstub `treasury.evaluate`, `execution.{submit,cancel,status}`; add `banker.{pnl_by_agent,pnl_by_strategy,equity_curve,fees_breakdown}`, `risk.{position_size,var,max_drawdown}`, `order.{list_open,history,modify}`, `position.{list,close,partial_close,hedge}`, `wallet.{paper_create,fund,balance,transfer}` |
| `backtest.*` (backtest composition / results) | 2 | 20 | Add `strategy.{list,get,params_schema}`, `indicator.{list,get,params_schema,compute_preview}`, `engine.{list,caps}`, `backtest.{compose,plan_sweep,parity_compare,top_k}`, `backtest.results.{equity_curve,trade_log,metrics,by_regime}` |
| `memory.*` + `memu.*` | 5 | 15 | Unstub `memu.{broadcast,search}`; add `memory.{list_by_type,delete_ns,namespace_stats}`, `learnings.{by_topic,by_symbol,top_k,export}`, `memu.{subscribe,unsubscribe,watch}` |
| `learning.*` (autopsy/postmortem/feedback) | 4 | 10 | Add `trade.record_outcome`, `session.{start,end,summary}`, `cycle.{start,end,report}` |
| `regime.*` + `guardrails.*` | 1 (`regime.current`) | 10 | Add `regime.{history,transitions,forecast}`, `guardrail.{list,violations,propose_change}`, `crash_protection.{status,history,trigger_test}` |
| `strategy.*` | 1 (`strategy.intents.recent`) | 8 | Add `strategy.{describe,deploy,retire,run,list_live,compare_live_vs_backtest}` |
| `services.*` + `ops.*` | 1 (`services.list`) | 10 | Add `services.{status,logs,restart,enable,disable}`, `deployment.{status,last_deploy,health}`, `cost.{summary_by_agent,summary_by_model,budget_remaining}` |
| `paperclip.*` (routines, heartbeats, issues) | 0 | 15 | Add `routine.{list,create,get,trigger,runs}`, `heartbeat.{list,recent}`, `issue.{list,create,comment,close}`, `approval.{pending,approve,reject}` |
| `diag.*` + `ping` + `dashboard.*` | 2 | 4 | Add `diag.{trace,self_test,manifest}`, `dashboard.snapshot` (already exists) |
| `contest.*` (trading competition) | 0 | 8 | Add `contest.{create,join,leaderboard,bank,end,postmortem,clone}` |
| `tools.*` (self-describing/meta) | 0 (`tools/list` is protocol-level, not a registered tool) | 5 | Add `tools.{catalogue,suggest,request_new,usage_stats,deprecations}` |

**Rough total if fully unpacked: 14+25+30+20+15+10+10+8+10+15+4+8+5 ≈ 174 tools.**

That comfortably clears your "100+" bar without inventing anything.

> **DECISION LOCKED (2026-04-20):** Fat tools (~174), **conditional on full indicator discoverability.** That means Phase M5 MUST ship: `indicator.list`, `indicator.get(name)`, `indicator.params_schema(name)`, `indicator.compute_preview(kind, params, ...)`. If during M5 testing an agent can't find an indicator by browsing these tools in ≤ 2 calls, we fall back to the **hybrid** form: keep everything else fat, but split the indicator family into thin (one tool per indicator, ~40 extra tools). Strategies and engines stay fat regardless.

---

## 4. Phased plan

Each phase is **independent and reversible**. Nothing earlier in the list requires anything later. You approve phase-by-phase.

Every phase ends with:
- **What changed** — exact files and services touched.
- **How to verify** — a one-liner command (CLI or curl) that proves it works.
- **How to roll back** — the commit to revert or the systemd unit to stop.

### Phase M0 — VPS reality baseline (no changes; 1 day) — ✅ DONE 2026-04-20

**Output:** `shared/docs/PHASE_M0_BASELINE.md`. Key findings summarized below; full evidence in that doc.

- MCP daemon is live on `:7777` as `tickles-mcpd.service`, with **35 tools** registered (not 41).
- Postgres `memu` DB + `pgvector 0.6.0` exist; `insights` table auto-creates on first write.
- OpenClaw has only `tickles` MCP registered — `user-mem0` is not registered (M4 still needed).
- Candle data is **Bybit-only**, **3 pairs**, **~11h of 1m / ~2d of 5m** — serious backfill required before M7.
- Env credentials: **Bybit + Blofin + BitGet + Capital** present. **Binance + Gates.io** absent.
- MCP endpoint is `POST /mcp`, health is `GET /healthz`.
- 6 built-in tools (`services.list`, `strategy.intents.recent`, `backtest.{submit,status}`, `dashboard.snapshot`, `regime.current`) are defined in code but never registered — **Phase M0.5** (new) picks them up for free.

---

### Phase M0.5 — Register the 6 dead-code built-in tools — ✅ DONE 2026-04-20

**Deployed:** `dcef12d+1` (commit below) → VPS `/opt/tickles/shared/mcp/bin/tickles_mcpd.py`, service restarted at 2026-04-20 15:20 UTC.

**Verified:** `POST /mcp {method:"tools/list"}` returns **41 tools**. `services.list` wired to real `SERVICE_REGISTRY` provider (reports 23 services with full descriptions). Other 5 register with safe `not_implemented` stubs — discoverable in catalogue, return readable errors when called, will be swapped for real providers in M1/M3.

**Rollback file:** `/opt/tickles/shared/mcp/bin/tickles_mcpd.py.m0_backup` (kept on VPS).

### Phase M3 — Wire MemU (Tier-3 cross-company memory) — ✅ DONE 2026-04-20

**Deployed:** commits below → VPS `/opt/tickles/shared/mcp/tools/memory.py` and `/opt/tickles/shared/memu/client.py`, plus systemd drop-in `/etc/systemd/system/tickles-mcpd.service.d/env.conf` loading `/opt/tickles/.env`.

**What changed:**

- `memu.broadcast` is now LIVE: calls `shared.memu.client.MemU.write_insight()` in an `asyncio.to_thread` wrapper (so a psycopg2 stall can't block the event loop). Returns `{status:"ok", insight_id, kind, source_agent}`.
- `memu.search` is now LIVE: calls `MemU.search(query, kind, k)`. Returns `{status:"ok", count, results:[{kind,content,metadata,created_at,distance}]}`. Uses pgvector semantic search when embeddings load; falls back to recency otherwise.
- Fixed a pre-existing bug in `shared/memu/client.py` `search()`: when both `kind` filter AND embedding are supplied, the parameter order placed the kind string where pgvector expected a vector, raising `malformed vector literal: "<kind>"`. Parameters are now slotted correctly.
- Added systemd drop-in so the MCP daemon inherits `DB_PASSWORD` + other Postgres creds (same pattern as `tickles-funding-collector`, `tickles-md-gateway`, etc.).
- Updated tool metadata: `memu.broadcast`/`memu.search` tags moved from `"status":"stub"` → `"status":"live"`, version bumped `1` → `2`, descriptions now reflect the real behavior.

**Verified** (2026-04-20 15:29-15:32 UTC, 4 insights written, all searchable):

- `memu.broadcast` (lesson, rubicon/surgeon): `insight_id=5febf960-...`
- `memu.broadcast` (playbook, tradelab/apex): `insight_id=a3f652e4-...`
- `memu.broadcast` (warning, testcorp/scout): `insight_id=6fd4f9e8-...`
- `memu.search` no-filter, `query="regime bull momentum tilt"`: returns the playbook first (0.2s, warm).
- `memu.search` filtered, `query="BTC overbought short setup", category="lesson"`: returns 2 lessons with distances 0.59 / 0.61 (0.4s, warm).
- Direct Postgres check: 4 rows in `memu.insights`, all with correct `source_agent` labels.

**Known characteristics:**

- **First call is cold (~15s)** because SentenceTransformer downloads `all-MiniLM-L6-v2` model weights from HuggingFace on first use. Subsequent searches are sub-second. Future improvement: warm-load on daemon startup (out of scope for M3).
- **Tier-1/2 memory.add/memory.search still return `forward_to: user-mem0::...` envelopes** — they will become useful once Phase M4 registers `user-mem0` with OpenClaw.

**Rollback files retained on VPS:**

- `/opt/tickles/shared/mcp/tools/memory.py.m3_backup`
- `/opt/tickles/shared/memu/client.py.m3_backup`
- `rm /etc/systemd/system/tickles-mcpd.service.d/env.conf && systemctl daemon-reload && systemctl restart tickles-mcpd.service` to revert the env drop-in.

### Phase M4 — Wire Tier-1/Tier-2 mem0 (memory.add / memory.search / learnings.read_last_3) — ✅ DONE (code) / ⚠️ BLOCKED (OpenRouter) 2026-04-20

**Deployed:** commit below → VPS `/opt/tickles/shared/mcp/tools/memory.py`.

**Architectural decision (locked):** Originally the plan called for registering a separate `user-mem0` MCP server with OpenClaw and relying on the `forward_to: user-mem0::add-memory` envelope pattern. Instead we now call `shared.utils.mem0_config.ScopedMemory` **directly** from inside the Tickles MCP daemon — one less moving part, all memory tools live in one file, no extra stdio subprocess. The `forward_to` envelope is kept as a **graceful fallback** for when mem0 cannot be initialised or a call fails, so external MCP hosts (e.g. Cursor's built-in user-mem0) can still pick up the call unchanged.

**What changed:**

- `memory.add` is now LIVE: calls `ScopedMemory(company, agent_id).add(...)` inside `asyncio.to_thread`. Per-company Qdrant collection `tickles_<company>`. Local sentence-transformers embeddings (384-dim). OpenRouter LLM for mem0's internal fact-extraction pipeline (with fallback chain Gemini → DeepSeek → auto).
- `memory.search` is now LIVE: calls `ScopedMemory.search(...)`. Returns `{status, count, results}` — each result has the memory text + metadata + relevance score. Works even when OpenRouter is down (search doesn't require the LLM, only embeddings + Qdrant).
- `learnings.read_last_3` is now LIVE: narrow-scope call to `ScopedMemory.search(topic or "recent learnings", limit=3)`. Twilly Template 03 pre-decision hook.
- ScopedMemory instances are cached per `(company, agent_id)` pair in a thread-safe dict for the lifetime of the daemon.
- All three tools fall back to the `forward_to` envelope (with a `mem0_error` field) on failure so callers always get a structured, non-crashing response.
- Tool metadata: version 1 → 2, descriptions rewritten to reflect real behaviour.

**Verified** (2026-04-20 15:38 UTC):

- Daemon restart: 41 tools registered, `services.list` real provider still reports 23 services, no errors.
- `memory.add` (rubicon/surgeon, 60-day SOL mean-reversion lesson) → returned the `forward_to` envelope with `mem0_error: RuntimeError: [mem0] add exhausted all models [...]: Error code: 403 - Key limit exceeded (total limit)` — **graceful failure, no crash, no daemon hang**.
- `memory.search` → `{status:"ok", count:0}` — search pipeline works, Qdrant collection was empty so no results.
- `learnings.read_last_3` → `{status:"ok", count:0}` — same path, same graceful behaviour.
- Direct Qdrant probe: `tickles_rubicon` collection exists, `points_count=0`.

**🚨 Pre-existing CEO BLOCKER surfaced by M4:**

The OpenRouter API key on the VPS has exhausted its $200 total limit (`usage=$200.76 / limit=$200`). This means:

- **Every** Tier-1/2 mem0 write will fail until the account is topped up or a new key is issued.
- This also blocks any agent cron that depends on LLM calls (Surgeon, Surgeon2, etc.) via OpenRouter.
- Search still works (no LLM needed), but nothing has been written yet — all agent-private & company-shared memories have been silently failing since the cap was hit.

**Action item for CEO:**
1. Visit https://openrouter.ai/settings/keys
2. Either top up the existing key's budget, raise the total limit, or generate a new key with a higher cap.
3. If a new key is generated: `ssh vps` → edit `/opt/tickles/.env`'s `OPENROUTER_API_KEY=...` → `systemctl restart tickles-mcpd.service` (and any other tickles services that use LLMs).
4. Re-run the M4 smoke test to confirm: `ssh vps` → probe `memory.add` via `POST /mcp` as in `_tmp_m4_test.sh`.

**Rollback:** `/opt/tickles/shared/mcp/tools/memory.py.m4_backup` (retained on VPS).

### Phase M0.5 (original plan)

**What:** Edit `shared/mcp/bin/tickles_mcpd.py` to call the `build_*_tool()` helpers that already exist in `shared/mcp/registry.py`. Each one pairs with a provider function we need to wire:

| Tool | Provider source | Complexity |
|---|---|---|
| `services.list` | `shared/services/registry.py` (`ServiceRegistry.snapshot_async()`) | trivial |
| `strategy.intents.recent` | Postgres `SELECT FROM strategy_intents_latest LIMIT n` | trivial |
| `backtest.submit` | `shared.backtest.submitter.submit_spec()` | medium (uses Redis queue) |
| `backtest.status` | Postgres `SELECT FROM backtest_submissions WHERE spec_hash=...` | trivial |
| `dashboard.snapshot` | `shared.dashboard.snapshot_async()` — wrap existing Phase-36 snapshot | trivial |
| `regime.current` | Postgres `SELECT FROM regime_current LIMIT 1` | trivial |

**Why:** registry grows from 35 → 41 tools at zero new functionality cost. Five of six are read-only; `backtest.submit` is the only RW one but it already has idempotency via spec_hash.

**Verify:** after restart, `POST /mcp {method:"tools/list"}` returns 41 items.

**Rollback:** revert the single commit.

### Phase M0 — (original, kept for reference)

### ~~Phase M0 — VPS reality baseline (no changes; 1 day)~~ (superseded — see above)

**What:** SSH into the VPS, run a read-only audit script that answers 9 yes/no questions:

1. Is `mcp-server` systemd unit installed + enabled + running?
2. Does Postgres `memu` database exist? Does the `vector` extension exist? Is the `insights` table present?
3. Is the `user-mem0` MCP registered with OpenClaw? (Look in `/root/.openclaw/config.json` for a `mem0` entry.)
4. How many candles do we have for each of {BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX} at {1m, 5m}? Earliest and latest timestamp per pair.
5. Are any backtest-workers running? (`systemctl status tickles-bt-workers`)
6. What venues are actually connected via CCXT Pro? (Poll `shared/gateway/*` live.)
7. Which of the 17 "shipped but inactive" services would start cleanly if we `systemctl enable --now`'d them? (Dry-run with `systemd-analyze verify` + config probe.)
8. What's the current total count of tools registered in the running MCP server? (`echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | nc -U /run/tickles/mcp.sock` or via HTTP transport.)
9. **Venue credentials audit** (added 2026-04-20): for each of {Binance, Bybit, Gates.io, Blofin, BitGet} and any other venue env vars in `/opt/tickles/.env`, report presence (yes/no) and key length (first 4 chars + `<N chars>`). **No secret values are ever printed or logged.** This tells us in advance which venues Phase M7 + M8 can actually reach. The contest scope in M7 follows this list — whatever we have keys for.

**Why:** everything downstream depends on knowing the true state — writing code based on assumptions is how we lost time earlier.

**Deliverable:** `shared/docs/PHASE_M0_BASELINE.md` — one table, 9 rows, each answered "yes/no + evidence line".

**Rollback:** none — no files changed.

---

### Phase M1 — Unstub market data + bulk backfill — 3 days (revised post-M0)

**What:**
- Wire `md.quote(symbol, venue)` → reads the last row from `candles` for `{symbol, venue, timeframe=1m}`.
- Wire `md.candles(symbol, venue, timeframe, from, to, limit)` → paginated read from `candles`.
- Add three new tools to `data.py`:
  - `candles.coverage(symbol, venue, timeframe)` → returns `{earliest, latest, count, gaps:[...]}`. Uses existing `GapDetector`.
  - `candles.backfill(symbol, venue, timeframe, from, to)` → enqueues a backfill via the existing `CandleService` (async, returns a `backfill_id`).
  - `candles.backfill_status(backfill_id)` → polls progress.
- **Run a one-time bulk backfill operation** (CLI + MCP): 60 days of 1m and 5m for `{BTC, ETH, SOL, BNB, XRP, DOGE, ADA, AVAX}/USDT` on Bybit. Math: ~16M rows, ~45 min wall-clock.
- **Diagnose candle-daemon silence.** Journal has no output since 2026-04-16 startup; latest candle in DB is 2026-04-13. Figure out whether it's silently disconnected, writing elsewhere, or idle for a config reason. Fix needed before backfill is meaningful (otherwise backfill data immediately starts going stale).

**Why:** today's candle store has **~11 hours of 1m data across 3 pairs on 1 venue**. Any curious-agent or backtest workflow requires real coverage before it's worth shipping. M7 (contest) is outright impossible without this.

**Files touched:**
- `shared/mcp/tools/data.py` (edit `_md_quote`, `_md_candles`, add 3 new tool defs).
- `shared/cli/backfill_cli.py` (new, thin wrapper around `CandleService.collect_candles()` in a loop).
- No DB schema changes — `candles` + `instruments` already exist.
- No new systemd units.

**Verify:**
```
openclaw mcp call --server tickles candles.coverage '{"symbol":"BTC/USDT","venue":"bybit","timeframe":"1m"}'
# expect: earliest ≈ 60 days ago, count ≈ 86400, gaps=[]
openclaw mcp call --server tickles md.candles '{"symbol":"BTC/USDT","venue":"bybit","timeframe":"5m","limit":10}'
# expect: 10 real OHLCV rows
```

**Rollback:**
- Revert MCP commit → tools go back to `not_implemented` stubs.
- Backfilled candle rows stay in place (deleting them is data loss, and they're correct).
- If backfill goes wrong and writes bad data: we have SHA-256 drift detection in `CandleService`, so repeat-collection will flag it.

---

### Phase M2 — Unstub trading surface (paper-only, behind `dryRun=true` default) — 3 days

**What:**
- Wire `treasury.evaluate` → calls `shared.trading.treasury.evaluate()` (already exists in-process).
- Wire `execution.submit` → always defaults `dryRun=true`; non-dry requires an explicit `I_am_paper=true` param (which only agents in companies with `is_live=false` flag can set). Live trading is **not unlocked in this phase** — that's a separate CEO decision.
- Wire `execution.cancel` + `execution.status` → read from `order_events` + `orders` tables.
- Wire `banker.positions` → read from `position_snapshots` view `positions_current`.
- Add `wallet.paper_create(companyId, agentId, starting_usd)` → creates a row in a new `paper_wallets` table (simple, no real money).

**Why:** this is how agents actually trade paper money. Without this, the Surgeon loop still works only via file writes (SOUL.md / TRADE_STATE.md), which is the Twilly pattern but not scalable to "competition between 5 agents".

**Files touched:**
- `shared/mcp/tools/trading.py` (edit 4 handlers, add 1).
- `shared/migration/<next>_paper_wallets.sql` (new table + rollback).
- No venue connectivity required — all paper.

**Verify:**
```
openclaw mcp call --server tickles treasury.evaluate '{...}'
openclaw mcp call --server tickles execution.submit '{..., "dryRun": true}'
```

**Rollback:** revert commit + drop `paper_wallets` table via rollback migration.

---

### Phase M3 — Unstub MemU tools — 1 day (prereq: Phase M0 confirms Postgres + pgvector)

**What:**
- If Phase M0 shows `memu` DB + pgvector missing: install pgvector (`apt install postgresql-16-pgvector` or build from source), create DB via the schema already at the top of `shared/memu/client.py`.
- Wire `memu.broadcast(origin, category, content, metadata)` → calls `MemU.write_insight()`.
- Wire `memu.search(query, category, limit)` → calls `MemU.search()`.
- Add `memu.subscribe(category)` → opens a `pg_notify` listener on `memu_insights` channel, forwarded to the caller via an event-stream response.

**Why:** this is Tier 3 of the memory pyramid. Without it, lessons die inside one company's agent and never help other companies.

**Files touched:**
- `shared/mcp/tools/memory.py` (2 unstubs, 1 new).
- One-time VPS task: install pgvector + create DB (idempotent).

**Verify:**
```
openclaw mcp call --server tickles memu.broadcast '{"originCompanyId":"tickles-n-co","category":"lesson","content":"Mark/Index divergence > 0.15% = reliable entry"}'
openclaw mcp call --server tickles memu.search '{"query":"divergence entry"}'
```
Should see round-trip.

**Rollback:** revert MCP changes; leave DB in place (it's just a data store).

---

### Phase M4 — Register `user-mem0` MCP with OpenClaw — 0.5 day

**What:** Add a `mem0` entry to `/root/.openclaw/config.json` pointing at the `user-mem0` MCP server. After this, any OpenClaw-cron agent can call `mem0.add-memory` / `mem0.search-memory` directly — which closes the loop that `memory.add` / `memory.search` in Tickles MCP currently defers to via "forward_to".

**Why:** fixes glue-gap #6 ("mem0 MCP not registered with OpenClaw"). Without this, agents can't use Tier 1/2 memory at all — the `forward_to` hint in Tickles MCP responses isn't actually consumed.

**Files touched:**
- VPS `openclaw.json` (one JSON edit, backed up first per standard).
- No code change.

**Verify:** create a trivial OpenClaw cron that calls `mem0 search-memory` and prints the result.

**Rollback:** restore backup of `openclaw.json` (we already do this routinely).

---

### Phase M5 — Backtest discovery tools — 3 days

**What:** the agent doesn't know what strategies, indicators, or engines exist unless we tell it. Ship:

- `strategy.list(tag?)` → `[{name, description, param_schema, supported_timeframes, ...}, ...]`
- `strategy.get(name)` → full strategy card.
- `indicator.list(kind?)` → `[{name, kind, params, window_required, category}, ...]`. Pulls from `shared/backtest/indicators/*`.
- `indicator.compute_preview(name, params, symbol, venue, timeframe, window)` → runs the indicator live on the last N candles so the agent can see what values look like before committing to a backtest.
- `engine.list()` → `[classic, nautilus, vectorbt]` with capability tags (`supports_leverage`, `supports_short`, `supports_fractional_kelly`, ...).
- `backtest.compose(symbol, strategy, indicator_params, from, to, venue, starting_cash_usd)` → returns a validated BacktestSpec (doesn't submit; lets the agent review first).
- `backtest.plan_sweep(base_spec, param_ranges)` → expands a parameter sweep (e.g. RSI period 10-20) into N specs without submitting.
- `backtest.top_k(spec_hash?, limit, sort_by='sharpe')` → reads ClickHouse, returns top K results of a sweep.

**Why:** this is the "be curious" unlock. An agent reading `strategy.list` for the first time literally sees every play in the book.

**Files touched:** `shared/mcp/tools/backtest.py` (new file, per group-by-function rule). Register it in `shared/mcp/bin/tickles_mcpd.py`.

**Verify:** `tools/list` should grow by ~10. One golden-path smoke:
```
agent> indicator.list(kind='momentum') → sees RSI, MACD, Stoch
agent> indicator.compute_preview('rsi', {'period':14}, 'BTC/USDT', 'binance', '5m', 200)
agent> strategy.list()
agent> backtest.compose(...) → spec OK
agent> backtest.submit(spec) → submission id
agent> backtest.top_k(submission_id=..., limit=5) → rows from ClickHouse
```

**Rollback:** delete the new file + revert the register call.

---

### Phase M6 — Curious-agent loop + escalation — 2 days

**What:**
- `tools.catalogue(group?, include_disabled?, include_stubs?)` → the full menu, grouped, so an agent can read the shopping list in one call.
- `tools.suggest(task_description)` → server-side: regex-based hint tool that maps a phrase to likely MCP calls (e.g. "find divergence" → suggests `md.candles` + `indicator.compute_preview('rsi')` + maybe `strategy.list(tag='divergence')`).
- `tools.request_new(name, rationale, example_input, example_output)` → creates a Paperclip issue labeled `mcp-tool-request`, assigned to the CEO.
- `tools.usage_stats(since?)` → reads `mcp_invocations` table — which tools are hot, which cold.

**Why:** this is the loop where agents get curious, check the shopping list, realize something is missing, and ask the CEO. Without it, agents silently hallucinate around gaps.

**Files touched:** `shared/mcp/tools/meta.py` (new). Uses the existing `InvocationStore` so no new tables.

**Verify:**
```
agent> tools.catalogue(group='data')                       # sees md.*, candles.*, altdata.*
agent> tools.request_new('onchain.whale_entries', ...)     # issue created in TIC project
```

**Rollback:** delete the file + unregister.

---

### Phase M7 — Paper trading contest — 3 days

**Venue scope (locked post-M0 audit):** based on actual `/opt/tickles/.env` presence + actual candle coverage after M1 backfill:

| Venue | Keys? | Candle data after M1? | Contest ready? |
|---|---|---|---|
| Bybit | yes (live + demo + demo_shadow) | yes (60d of 1m/5m, 8 pairs) | **yes — primary venue** |
| Blofin | yes (live + demo) | needs CCXT backfill in M1 follow-up | M7.1 — second wave |
| BitGet | yes | needs CCXT backfill in M1 follow-up | M7.1 — second wave |
| Capital.com | yes (email + password + api key) | no — CFD not crypto, separate schema | out of scope for M7 |
| Binance | **no keys** | n/a | blocked until CEO adds keys |
| Gate.io | **no keys** | n/a | blocked until CEO adds keys |

Initial contest runs on **Bybit only**. Blofin + BitGet join in a M7.1 sub-phase once their candle data is backfilled. Binance + Gate.io stay out until CEO provides `.env` keys.

**What:** The single highest-leverage fun feature. Ship:

- `contest.create(name, venues[], coins[], duration_days, starting_paper_usd_per_agent, agent_ids[])` → creates a row in new `contests` table, provisions a `paper_wallets` row per agent, sets `contest_id` on all their `orders` + `fills`. The `venues[]` param is validated against M0's credential set — unreachable venues return a clear error.
- `contest.join(contest_id, agent_id, strategy_ref)` → registers a late-joiner.
- `contest.leaderboard(contest_id)` → reads from `position_snapshots` + `fills`, returns ranked P&L.
- `contest.end(contest_id)` → freezes balances, auto-runs `postmortem.run` for every agent, stores learnings in MemU with `category='contest_lesson'`.

**Why:** fulfills your vision — "say to the CEO, let's have a trading competition, give them 5 agents, let them discover MCP tools and compete".

**Files touched:**
- `shared/mcp/tools/contest.py` (new).
- `shared/migration/<next>_contests.sql` + rollback.
- `shared/services/contest_service.py` (tiny reconciler that watches for contest end dates).

**Verify:** E2E: create contest with 2 agents for 5 minutes (for the smoke test!), verify leaderboard after each agent submits a paper trade, verify postmortem fires at end, verify MemU has a new row tagged `contest_lesson`.

**Rollback:** revert commits + drop `contests` + `paper_wallets` (if Phase M2 also rolled back) via rollback migrations.

---

### Phase M8 — Enable shipped-but-dormant services on the VPS — 1 day (revised post-M0)

**Reality check:** M0 showed that `SERVICES.md`'s "17 staged services" claim is wrong. Actual VPS state:
- **8 units** already `enabled + active` (paperclip, tickles-mcpd, candle-daemon, catalog, cost-shipper, funding-collector, md-gateway, bt-workers).
- **3 units** present but `disabled`: `tickles-discord-collector`, `tickles-trader-rubicon_surgeon`, `tickles-trader-rubicon_surgeon2`.
- **~12 Python modules** in `shared/services/*.py` that `SERVICES.md` calls "services" but have **no systemd unit file**: `banker`, `executor`, `regime`, `crash-protection`, `altdata-ingestor`, `events-calendar`, `souls`, `arb-scanner`, `copy-trader`, `strategy-composer`, `backtest-runner`, `auditor`.

**M8 splits:**

- **M8a (1 hour):** `systemctl enable --now` for the 3 currently-disabled units. Discord-collector only if Discord credentials are confirmed; surgeon units only if we decide cron-via-openclaw isn't enough.
- **M8b (2-3 days, after M7):** write systemd unit files for whichever of the 12 Python modules we actually need running (decided in a follow-up review, NOT blanket enabled). Each new unit is one commit, fully scoped with `Restart=always`, proper `EnvironmentFile`, `User=`, `ReadWritePaths=`, mirroring the `tickles-mcpd.service` pattern.

**Why the split:** there's no value in enabling a dormant module that was never wired. "Enable everything" becomes "enable on demand" — and the auditor decides *which* ones.

### ~~Phase M8 — Enable shipped-but-dormant services on the VPS — 1 day~~ (original, superseded)

**What:** `systemctl enable --now` the 17 services from `SERVICES.md` one at a time, in dependency order:

1. `mcp-server` (prerequisite for everything tool-related).
2. `banker`, `executor`, `regime`, `crash-protection` (trading plane).
3. `altdata-ingestor`, `events-calendar`, `souls`, `arb-scanner`, `copy-trader`, `strategy-composer` (strategy plane).
4. `backtest-runner`, `backtest-submitter`, `dashboard` (analytics plane).
5. `discord-collector`, `news-rss`, `telegram-collector`, `tradingview-monitor` (collectors — optional, credential-gated).

Each enable is a separate commit + roadmap entry.

**Why:** closes glue-gap #2 ("No Tickles MCP systemd unit; server exists but not long-lived") and all the other "code shipped, unit staged" items.

**Files touched:** `/etc/systemd/system/tickles-*.service` symlinks. No Python code.

**Verify:** `systemctl is-active` on each unit = `active`.

**Rollback:** `systemctl disable --now tickles-<name>.service`. Per-service rollback.

---

### Phase M9 (optional, later) — the final stretch to 300-400 tools

Only if, after M5-M7, you decide you want the flat-shopping-list form. This is a **breakdown phase**, not a feature phase: split `indicator.compute_preview('rsi', {...})` into `indicator.rsi.compute({...})`, split each strategy into its own `strategy.<name>.{describe,run,backtest}` triple, etc. Mechanical but tedious.

**Recommendation:** don't do M9 until you've seen M5-M7 agents actually working — context-window economics for LLMs usually win with the fat form.

---

## 5. Memory implementation (separate track, runs parallel)

### 5.1 Tier 1/2 activation (after M4)

Once `user-mem0` is registered with OpenClaw (Phase M4), every agent's SOUL.md will have a mandatory preamble:

```
Before any decision:
  1. mem0.search-memory(namespace=tickles_{company}, user_id={company}, agent_id={company}_{me}, query=<relevant topic>, limit=3)
  2. If match found: include as "last learnings" in thinking.
After any decision:
  3. mem0.add-memory(content=<outcome + lesson>, memory_types=['learning'], namespace=...)
```

This is already in the architecture doc as a hard contract — we just need it live.

### 5.2 Tier 3 (MemU) activation (after M3)

Strategy Council Moderator, per `COMPANIES_AS_IDEAS_PLAN.md`, runs the weekly board meeting and decides what gets broadcast cross-company:
- `category='lesson'` — actionable pattern (e.g. "Funding > 0.08% + price up 3% in 1h → reliable mean-reversion entry")
- `category='warning'` — trap pattern (e.g. "Binance funding data lags by 10s during volatility spikes")
- `category='playbook'` — full strategy description
- `category='postmortem'` — why something failed across multiple agents

Agents from other companies subscribe to categories they care about. An agent in SurgeonCo subscribes to `warning` and `lesson`; an agent in MentorObserver subscribes to all four.

### 5.3 Memory discipline rules (enforced by the MCP)

- **No secrets in memory.** `memory.add` refuses content matching `sk-[A-Za-z0-9]{20,}`, `ghp_`, etc. (already a rule in the OpenMemory guide — just needs to be enforced by the MCP tool layer too.)
- **Namespaces are sacred.** An agent can never write outside its `agent_id={company}_{agent}` scope. Company-level writes (`agent_id=shared`) require `scope='company'` in the tool call.
- **TTL on Tier 1.** Agent-private mem0 entries older than 90 days auto-archive to Tier 2 if they were read ≥3 times, else deleted. (Policy, implemented in a nightly cron Phase M8+.)

---

## 6. Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Tool bloat — LLM sees 170 tools and abandons the turn (the "surgeon empty-response" bug) | **HIGH** | Agent breaks | Per-cron `--tools <scoped list>` already works (see `NEW_TRADING_AGENT_HOWTO.md`). For MCP: use `tags` filtering + `tools.catalogue(group='data')` so the agent fetches only what it needs. |
| VPS pgvector install fails on Postgres 16 | MEDIUM | M3 blocks | Fallback: compile pgvector from source on the VPS; MemU already gracefully degrades to text-only search when the embedder is unavailable. |
| Candle gaps too large to backfill in a reasonable time | MEDIUM | M1 UX suffers | `candles.backfill` is async, returns `backfill_id`, CandleService already uses exchange-native historical endpoints. |
| Agent requests a tool the CEO declines repeatedly | LOW | Noise | `tools.request_new` creates Paperclip issues; dedup on description hash. |
| Real money slipping in during paper-only phases | LOW | **Catastrophic** | `execution.submit` hard-codes `dryRun=true` unless company has `is_live=true` flag; Phase M2 never sets that flag. Separate approval flow added later. |
| OpenClaw `--tools` flag regression breaking existing crons | LOW | Surgeon/Surgeon2 down | Every cron edit backs up `openclaw.json` first (already established pattern). |

---

## 7. Decisions locked (2026-04-20)

All six open questions have CEO answers. No further approval needed before Phase M0 starts — only the "start M0" command.

| # | Question | CEO decision | Rationale / constraint |
|---|---|---|---|
| 1 | Fat vs thin tools | **Fat (~174), conditional on full indicator discoverability.** If Phase M5 testing shows agents can't find an indicator in ≤2 calls, fall back to hybrid (split indicators into thin, keep everything else fat). | Protects LLM context budget while preserving the "be curious" UX. |
| 2 | Contest venues (M7) | **Whatever venues have valid API keys on VPS**, per the Phase M0 credential audit (Section 4, M0, question 9). | Avoids designing for 5 venues when only 1 has keys. Also fine: a 1-venue contest is still a contest. |
| 3 | Real-money unlock gate | **Deferred.** Don't design the gate until the moment we actually need it (post-M8, earliest). | Lets us see real paper-trading data before picking thresholds. Nothing in M0-M8 requires a gate to be defined. |
| 4 | Env credential audit in M0 | **Include in M0**, read-only, never prints secrets (only presence + length). | Tells us upfront which venues M7 + M8 can reach. |
| 5 | Phase ordering | **Default: M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7 → M8.** | Data → trade → memory → discovery → contest → prod. Bottom-up, no step depends on a later step. |
| 6 | Tool naming | **Dots (`candles.coverage`, `execution.submit`).** | Matches all 41 existing tools, the audit table, and the registry schema. Zero migration cost. |

### Items explicitly deferred

- **Real-money unlock thresholds** (Q3) — revisit when ≥1 agent has 30+ days of paper trading under M2 + M7.
- **Venues beyond the M0 credential set** — if you want to add Bybit/Gates/Blofin/BitGet keys later, drop them into `/opt/tickles/.env` and run the M0 audit again; the contest + M8 live-enable will pick them up automatically.

---

## 8. What I will NOT do without approval

- Write any code in `shared/mcp/`, `shared/backtest/`, `shared/market_data/`, `shared/memu/`, `shared/trading/`.
- Touch any VPS systemd unit.
- Install pgvector or any VPS package.
- Edit any Paperclip config or OpenClaw config file.
- Delete or archive anything.
- Start any phase before you explicitly say "start M<N>".

I will only continue to **read and document**. Writing begins when you say so.

---

## 9. How to respond

With the 6 decisions locked (Section 7), the only remaining input needed is a **start command**:

- **"Start M0"** → I run the VPS baseline audit (SSH, 9 questions, read-only), write `shared/docs/PHASE_M0_BASELINE.md`, report findings. No code changes, no service changes, ~1 day.
- **"Start M0 and M4 in parallel"** → I open two tracks (M0 audit + wiring the `user-mem0` MCP into OpenClaw's config). M4 is tiny, and it's a prerequisite for all agent memory writes.
- **"Edit the plan: <change>"** → I redraft, you re-approve.
- **"More detail on <phase>"** → I expand that section before you approve the start.

I'll update this file (`shared/docs/MCP_AND_MEMORY_PLAN.md`) as the plan evolves; every phase completion gets a "✅ Done YYYY-MM-DD, commit <sha>, rollback <how>" note appended.

---

## Changelog

| Date | Change | Commit |
|---|---|---|
| 2026-04-20 | Initial plan drafted | `0adceec` |
| 2026-04-20 | 6 open questions answered; plan status → APPROVED; M0 gains env credential audit; M7 venue scope locked to M0 audit output; Section 7 rewritten as decisions table | previous commit |
| 2026-04-20 | M0 executed; `PHASE_M0_BASELINE.md` written; plan revised: (a) status → "M0 COMPLETE", (b) Phase M0.5 added (+6 free built-in tools), (c) M1 scope expanded to include bulk backfill + candle-daemon diagnosis, (d) M7 venue scope locked to Bybit primary / Blofin+BitGet M7.1, (e) M8 split into M8a/M8b reflecting actual service fleet | *previous commit* |
| 2026-04-20 | M0.5/M3/M4 executed and verified end-to-end. OpenRouter top-up confirmed ($200→$500 cap). Surgeon1 cron recovered, 113s cycle, fresh TRADE_STATE.md. Surgeon2 cron recovered, gpt-4.1, succeeded. mem0 end-to-end works: `memory.add` extracted 3 structured memories from SOL-short insight; `memory.search` returned them w/ scores 0.57/0.24/0.10; `learnings.read_last_3` returned same 3 top-k. Qdrant `tickles_rubicon` has 3 points, status green. MemU `insights` table has 4 rows. | (local commits dcef12d/cf75cc6/01a3536/3c6053a, unpushed) |
| 2026-04-20 | **M5 preflight**: discovered stdio MCP was only exposing 35 tools while HTTP exposed 41 — the M0.5 wiring lived in `bin/tickles_mcpd.py` only. Extracted `_register_builtin_tool_providers` → `register_builtin_providers(reg, logger)` in `shared/mcp/registry.py`; both entrypoints now call the shared helper. Deployed + verified: HTTP count 41, stdio count 41, identical catalogues. This was a required prereq because OpenClaw surgeons reach the MCP via stdio spawn, not HTTP. | previous commit |
| 2026-04-20 | **M4.5 Step 0-2**: investigated OpenClaw cron tool-whitelist mechanism + applied to rubicon_surgeon2 (A/B safety). Key findings documented below. | *this session* |

---

## M4.5 findings — OpenClaw cron tool allow-list (how memory tools reach the surgeons)

### 1. Mechanism (confirmed)
- `openclaw cron add/edit --tools <csv>` → stored as `payload.toolsAllow: [...]` in `/root/.openclaw/cron/jobs.json`.
- **Without `--tools`**, the agent sees **all ~58 tools** (17 OpenClaw built-ins like `read`/`write`/`exec`/`web_search`/`web_fetch`/`cron`/`sessions_*`/`memory_get`/`memory_search`/`image`/`subagents` + all 41 `tickles__*` MCP tools). This is the "drowning" scenario that HOWTO warned about.
- **With `--tools read,write,exec`**, the menu is only those 3. Safe but blind to MCP tools.
- **With `--tools read,write,exec,tickles__memory-add,tickles__memory-search,tickles__learnings-read_last_3,tickles__memu-broadcast,tickles__memu-search`** (8 tools total), the agent sees only those 8. Proven to not trigger the `abandoned` / empty-response bug.

### 2. MCP tool naming format — dots become dashes
OpenClaw exposes MCP tools from the `tickles` server as `tickles__<tool-name>` where the original tool name's dots are replaced with dashes:
- `memory.add` → `tickles__memory-add`
- `memory.search` → `tickles__memory-search`
- `learnings.read_last_3` → `tickles__learnings-read_last_3` (underscores in the original survive; only dots → dashes)
- `memu.broadcast` → `tickles__memu-broadcast`
- `memu.search` → `tickles__memu-search`

### 3. CLI quoting gotcha
`openclaw cron edit --message "<multi-line string>"` silently fails (throws a traceback, exits non-zero, does NOT patch the job). Use single-line strings with literal `\"` for inner JSON quotes. `--tools` accepts CSV just fine.

### 4. Cold-start timeout — the real M4.5 blocker
- **Observed:** surgeon2's first `tickles__learnings-read_last_3` call timed out at exactly 60.5s with `MCP error -32001: Request timed out`.
- **Root cause:** first `ScopedMemory("rubicon","surgeon2")` instantiation per-daemon-process cold-loads the sentence-transformer embedding model (`all-MiniLM-L6-v2`) + initializes mem0's LLM client for memory extraction. Cold start takes ~60s.
- **Warm-start latency:** 6-23s on subsequent calls (still slow, but inside the 60s budget).
- **Agent behaviour on timeout:** graceful. Surgeon2 explicitly printed "Memory recovery failed (timeout from MCP). Proceeding with standard lookback only." and completed the trading cycle normally. No drowning, no crash, no session corruption.

### 5. Verdict on M4.5 Step 2
**Mechanism: ✅ proven.** Tool allow-list works, agent calls the tools with correct args, tool failures degrade gracefully.
**Usefulness: ❌ not yet.** First call per daemon restart cold-starts and dies in the 60s OpenClaw timeout. We need M4.5b before rolling to surgeon1 or writing live insights.

### 6. What M4.5 Step 2 left in place on surgeon2
- `payload.toolsAllow` extended from 3 → 8 tools (the 5 memory tools added).
- `payload.message` extended to include explicit "call learnings.read_last_3 at start, call memory.add at end" instructions.
- No SOUL.md change yet (Step 5 deferred until cold-start fix).
- Surgeon2 continues to run every 5 min; the memory calls will start succeeding once M4.5b is in place.
- Surgeon1 is **untouched**; still on the 3-tool `read,write,exec` allow-list. Stays there until M4.5b + surgeon2 proves clean for 3 consecutive cycles.

### 7. Separate latent bug (NOT fixing here)
- Surgeon2's `TRADE_STATE.md` and `TRADE_LOG.md` have not been written since **2026-04-20 09:41:40 UTC**, despite every 5-min cron run returning `status: ok` with a detailed summary. GPT-4.1 is describing file writes in prose but not issuing actual `write` tool calls. Surgeon1 (Sonnet-4.6) writes files correctly.
- This is a SOUL.md / prompt-adherence issue specific to GPT-4.1, not an MCP or memory issue. Tracked separately; do not conflate with M4.5.

---

## M4.5b — cold-start fix (new sub-phase, ~1-2 hours)

**Goal:** First `memory.*` call per daemon restart must complete in <10s, not 60s+.

**Approach options, cheapest first:**

1. **Eager warmup at mcpd startup (recommended).** In `shared/mcp/bin/tickles_mcpd.py` `main()`, after `build_registry()`, asynchronously instantiate `ScopedMemory("rubicon","surgeon")` and `ScopedMemory("rubicon","surgeon2")` so the embedding model loads once during boot. Agent calls then hit a warm cache from call #1. ~30 LoC.

2. **Shared embedder instance across ScopedMemory.** Load the sentence-transformer model ONCE at module import of `shared/utils/mem0_config.py` and pass the instance to every `ScopedMemory`. Saves repeated model loads if we ever have >2 active agents. Higher risk (mem0 config plumbing).

3. **Bump OpenClaw MCP per-call timeout from 60s to 120s.** Bandaid, not a fix. Costs real latency on every timeout path. Not recommended unless (1) and (2) both fail.

**Plan:** Do (1) first. If surgeon2 warm-latency stays >30s we revisit (2). We don't touch (3).

**Verification:**
- Restart `tickles-mcpd`.
- Force-run surgeon2.
- Session transcript must show `tickles__learnings-read_last_3` tool result with `status: ok` and latency <10s.
- Qdrant `tickles_rubicon` points_count increments by ≥1 after the `memory.add` call.
- If all green for 3 consecutive cycles → promote to surgeon1.

**Rollback:** `git revert` the warmup-block commit; restart mcpd. Memory tools go back to cold-start behaviour but nothing else breaks.

---

## Next-decision (awaiting CEO)

You asked me to advise. My recommendation: **do M4.5b now** (small, contained, unblocks the whole memory feature). Then either promote to surgeon1 for a 3-cycle soak, OR pivot to **M1 (candle backfill + md.* unstub)** because the agents have nothing to chew on while memory is being hardened.

Alternative paths:
- **A) M4.5b → surgeon1 → M1** (strict serial, safest; slower overall).
- **B) M4.5b → M1 in parallel** (two sessions; faster; some extra coordination risk).
- **C) Skip memory for now → M1 first** (candle backfill is the bigger bottleneck for backtest-driven learning; memory comes back online later). I'd only pick this if you believe the agents should focus on data-discovery before self-learning.
