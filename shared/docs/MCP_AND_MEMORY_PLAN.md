# MCP & Memory Implementation Plan — Tickles & Co

**Status:** APPROVED — 6 open questions resolved 2026-04-20 (see Section 7). Execution begins on CEO command ("start M0").
**Branch:** `mcp`
**Owners:** CEO (approval) + AI (implementation)
**Last updated:** 2026-04-20

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

### Phase M0 — VPS reality baseline (no changes; 1 day)

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

### Phase M1 — Unstub market data (`md.quote`, `md.candles`) — 2 days

**What:**
- Wire `md.quote(symbol, venue)` → reads the last row from `candles` for `{symbol, venue, timeframe=1m}`.
- Wire `md.candles(symbol, venue, timeframe, from, to, limit)` → paginated read from `candles`.
- Add two new tools to `data.py`:
  - `candles.coverage(symbol, venue, timeframe)` → returns `{earliest, latest, count, gaps: [{start,end,minutes}]}`.
  - `candles.backfill(symbol, venue, timeframe, from, to)` → enqueues a backfill via the existing `CandleService` (async, returns a `backfill_id`).
  - `candles.backfill_status(backfill_id)` → polls progress.

**Why:** this is the single biggest unlock. Every curious-agent workflow starts with "do we have data?" — the agent needs to see coverage before it proposes a strategy.

**Files touched:**
- `shared/mcp/tools/data.py` (edit `_md_quote`, `_md_candles`, add 3 new tool defs).
- No DB changes — `candles` + `instruments` already exist.
- No new systemd units.

**Verify:**
```
openclaw mcp call --server tickles md.candles '{"symbol":"BTC/USDT","venue":"binance","timeframe":"5m","limit":10}'
```
Should return 10 real OHLCV rows.

**Rollback:** `git revert` on the single commit — tools just go back to `not_implemented` stubs. No data impact.

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

**Venue scope (decided 2026-04-20):** contest uses **whatever venues we have valid API keys for**, as surfaced by the Phase M0 credential audit (question 9). No credentials = not in contest. This avoids the "design for 5 venues, only 1 works" trap.

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

### Phase M8 — Enable shipped-but-dormant services on the VPS — 1 day

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
| 2026-04-20 | 6 open questions answered; plan status → APPROVED; M0 gains env credential audit; M7 venue scope locked to M0 audit output; Section 7 rewritten as decisions table | *this commit* |
