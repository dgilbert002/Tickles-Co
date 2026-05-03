# Assessment: Proposed Plan vs. Actual Infrastructure

**Date:** 2026-04-28
**Assessor:** Roo (Architect Mode)
**Scope:** Compare the proposed "Unified Plan" against existing Tickles infrastructure

---

## Executive Summary

The proposed plan is **well-intentioned but largely redundant**. It proposes building components that **already exist** in the codebase, often under different names. The plan's core insight — unify schema, enforce single-writer rules, tiered autonomy, compounding learning — is correct. But its implementation roadmap would duplicate 18+ months of existing work.

**Key finding:** We do NOT need to build most of what the plan proposes. We need to **wire together what we already have** and fill 4-5 genuine gaps.

---

## 1. What The Plan Proposes vs. What We Already Have

### 1.1 Service Infrastructure

| Plan Proposes | We Already Have | Status |
|---|---|---|
| Generic daemon supervisor with backoff, jitter, heartbeats | `shared.services.daemon.ServiceDaemon` (228 lines) | ✅ EXISTS |
| Service registry for systemd | `shared.services.registry.SERVICE_REGISTRY` (20+ services) | ✅ EXISTS |
| Launcher for `python -m` entrypoints | `shared.services.launcher.main()` | ✅ EXISTS |
| systemd template `tickles-service@.service` | `systemd/tickles-service@.service` | ✅ EXISTS |

**Verdict:** The plan's "Service Layer" is already built and operational. The plan doesn't acknowledge this.

### 1.2 MCP Tool Surface

The plan proposes 30+ MCP tools across `intelligence.*`, `backtest.*`, `risk.*`, `memory.*`, `coach.*` namespaces.

**What we ALREADY have (40+ tools live):**

| Namespace | Tools | File | Status |
|---|---|---|---|
| `tools.*` | catalogue, suggest, request_new, usage_stats | `shared/mcp/tools/meta.py` | ✅ LIVE |
| `memory.*` | add, search, memu.broadcast, memu.search, learnings.read_last_3 | `shared/mcp/tools/memory.py` | ✅ LIVE |
| `learning.*` | autopsy.run, postmortem.run, feedback.loop, feedback.prompts | `shared/mcp/tools/learning.py` | ✅ LIVE |
| `backtest.*` | strategy.list/get, indicator.list/get/compute_preview, engine.list, backtest.compose, backtest.plan_sweep, backtest.top_k | `shared/mcp/tools/backtest.py` | ✅ LIVE |
| `intelligence.*` | chart.analyze, signal.interpret, trader.profile, trader.score, signals.recent/pending, positions.open/history, traders.leaderboard, guru.report, epic.resolve | `shared/mcp/tools/intelligence.py` | ✅ LIVE |
| `trading.*` | banker.snapshot, banker.positions, treasury.evaluate, execution.submit, execution.cancel, execution.status, wallet.paper_create | `shared/mcp/tools/trading.py` | ✅ LIVE |
| `data.*` | candles, quote, coverage, backfill | `shared/mcp/tools/data.py` | ✅ LIVE |
| `provisioning.*` | company.list/create, agent.list/create | `shared/mcp/tools/provisioning.py` | ✅ LIVE |

**What the plan proposes that we DON'T have:**
- `forward.start/stop/status` — Forward test lifecycle management
- `validation.verdict/autopsy` — Rule 1 validation as MCP tools
- `risk.evaluate_trade/status/circuit_breaker` — Risk as explicit MCP namespace
- `memory.lineage/promote` — Memory promotion gates
- `coach.propose_prompt/apply/rollback/ab_status` — Coach service

**Verdict:** ~80% of the proposed MCP surface already exists. The missing 20% is genuinely useful.

### 1.3 Memory Architecture

| Plan Proposes | We Already Have | Status |
|---|---|---|
| Tier 1: Redis (working/session) | Redis used for candle buffers, pending signals | ✅ EXISTS |
| Tier 2: Postgres (episodic) | `agent_decisions`, `trade_validations`, `mcp_invocations` | ✅ EXISTS |
| Tier 3: Mem0+Qdrant (semantic) | `shared.utils.mem0_config.ScopedMemory` | ✅ EXISTS |
| Tier 4: Procedural (strategies+prompts) | `strategies`, `agent_prompts`, `system_config` | ✅ EXISTS |
| MemoryRouter (single entry point) | `shared.mcp.tools.memory` handles all tiers | ✅ EXISTS |
| MemoryLibrarian (dedupe, promote, prune) | **NOT IMPLEMENTED** | ❌ GAP |

**Verdict:** 4-tier memory exists and is wired. The Librarian is a genuine gap.

### 1.4 Backtest & Validation

| Plan Proposes | We Already Have | Status |
|---|---|---|
| Deterministic bar-by-bar backtester | `BacktestExecutor` in `shared/backtest/engine.py` | ✅ EXISTS |
| Forward test (shadow trading) | `ForwardTestEngine` in `shared/backtest/forward_test.py` | ✅ EXISTS |
| Rule 1 validation (0.1% threshold) | `ValidationEngine` in `shared/trading/validation.py` | ✅ EXISTS |
| Verdict system (CONTINUE/CAUTION/STOP) | `_compute_verdict()` with 90%/70% thresholds | ✅ EXISTS |
| Autopsy + drift detection | `_detect_drift_patterns()` + `_generate_learnings()` | ✅ EXISTS |
| param_hash for dedup | `BacktestConfig.param_hash()` SHA256 | ✅ EXISTS |
| Candle hash for data integrity | Hash verification in backtest pipeline | ✅ EXISTS |

**Verdict:** The ENTIRE backtest→forward→validation→autopsy→learning loop already exists. The plan proposes it as if it's new.

### 1.5 Risk & Execution

| Plan Proposes | We Already Have | Status |
|---|---|---|
| RiskOfficer (hard limits, position sizing) | `Treasury` + `CrashProtection` + `Guardrails` | ✅ EXISTS |
| ExecutionAgent (sole trades writer) | `ExecutionRouter` + `PaperExecutionAdapter` | ✅ EXISTS |
| Paper wallet creation | `wallet.paper_create` MCP tool | ✅ EXISTS |
| Position sizing formula | `Treasury.evaluate()` with risk_per_trade | ✅ EXISTS |
| Circuit breakers | `crash_protection_events` + `guardrails_cli` | ✅ EXISTS |
| Correlation caps | Not explicitly implemented | ❌ GAP |

**Verdict:** Risk system exists but is fragmented across Treasury, CrashProtection, and Guardrails. Needs unification.

### 1.6 Intelligence Pipeline

| Plan Proposes | We Already Have | Status |
|---|---|---|
| InterpretationService (LLM+quant consensus) | `shared.intelligence.interpretation_service.py` | ✅ EXISTS |
| ChartHacker (vision analysis) | `shared.intelligence.chart_hacker_guru.py` | ✅ EXISTS |
| TextSignalExtractor | `shared.intelligence.text_signal_extractor.py` | ✅ EXISTS |
| PositionMonitor | `shared.intelligence.position_monitor.py` | ✅ EXISTS |
| Tracked positions | `tracked_positions` table + wiring (just fixed) | ✅ EXISTS |
| Agent opinions | `agent_opinions` table + ChartHacker writes | ✅ EXISTS |
| Trader scoring | `trader_performance` table + rolling metrics | ✅ EXISTS |
| Dedup logic | `shared.intelligence.trade_dedup.py` | ✅ EXISTS |

**Verdict:** The entire intelligence pipeline exists and is operational. The plan doesn't acknowledge the Phase 3B/3C work.

### 1.7 Learning & Coach

| Plan Proposes | We Already Have | Status |
|---|---|---|
| Autopsy prompts (Twilly Template 01) | `_AUTOPSY_PROMPT` in `shared/mcp/tools/learning.py` | ✅ EXISTS |
| Postmortem prompts (Twilly Template 02) | `_POSTMORTEM_PROMPT` | ✅ EXISTS |
| Feedback loop (Twilly Template 03) | `_FEEDBACK_LOOP_PROMPT` | ✅ EXISTS |
| Mem0 writes with evidence IDs | `memory.add` + metadata templates | ✅ EXISTS |
| CoachService (propose/apply/rollback) | **NOT IMPLEMENTED** | ❌ GAP |
| Strategy genome crossover | **NOT IMPLEMENTED** | ❌ GAP |

**Verdict:** Learning loop scaffolding exists. Coach and crossover are genuine gaps.

---

## 2. What The Plan Gets Wrong About Our Architecture

### 2.1 OpenClaw vs. Services

The plan assumes all agents are "services" running under systemd. It misses that:

- **OpenClaw agents** (surgeon, surgeon2, ChartHacker) run as **separate processes** with their own MEMORY.md and SOUL.md
- They communicate via **Mem0/Qdrant**, not through the service registry
- They are **NOT** in `SERVICE_REGISTRY` because they predate the registry system
- They are **ad-hoc research/trading agents**, not long-running infrastructure services

**Implication:** The plan's "Agent Roster" table is wrong. We don't need 15 new agents. We need to decide:
1. Which ad-hoc agents should become **services** (reusable, multi-company)
2. Which should stay as **OpenClaw one-offs** (research, experimentation)
3. Which should become **MCP tools** (callable on demand, no daemon needed)

### 2.2 Phase-Based Roadmap

The plan ignores our existing **Phase 13-37 roadmap**:

- Phase 13: Candle collection ✅
- Phase 14: Catalog ✅
- Phase 16: Backtest workers ✅
- Phase 17: Market data gateway ✅
- Phase 21: Auditor ✅
- Phase 24: Services catalog ✅
- Phase 25: Treasury ✅
- Phase 26: Execution ✅
- Phase 27: Regime ✅
- Phase 28: Crash protection ✅
- Phase 29: Alt data ✅
- Phase 30: Events calendar ✅
- Phase 31-32: Souls (7 sub-agents) ✅
- Phase 33: Copy trader + Arb scanner ✅
- Phase 34: Strategy composer ✅
- Phase 35: Backtest submission ✅
- Phase 36: Dashboard ✅
- Phase 37: MCP server ✅

**Implication:** The plan's "Phase 0-5" roadmap would conflict with existing phases. We should map plan proposals to existing phases, not create a parallel roadmap.

### 2.3 Custom Tables

The plan correctly identifies `surgeon2_*` custom tables as a problem. But it misses:

- `surgeon_trader.py` uses **flat files** (`.md` trade logs), not custom tables
- The `souls` service (Phase 31-32) already defines **7 deterministic sub-agents** that use unified tables
- The `copy_trader` and `arb_scanner` services also use unified tables

**Implication:** The migration is smaller than the plan suggests. Only `surgeon2_*` needs migration. surgeon1 flat files can stay as export format.

### 2.4 "One Writer Per Critical Table"

The plan proposes `UnifiedPositionWriter` and `ExecutionAgent` as sole writers. We already have:

- `ExecutionRouter` in `shared/execution/router.py` — routes to paper/ccxt/nautilus adapters
- `PaperExecutionAdapter` in `shared/execution/paper.py` — deterministic paper fills
- `shared.intelligence.interpretation_service.py` — writes to `signal_interpretations`
- `shared.intelligence.position_monitor.py` — writes to `position_updates`

**Implication:** We don't need new "Writer" classes. We need to **enforce** that existing services are the only writers. This is a policy + code review change, not a new component.

---

## 3. Genuine Gaps (What The Plan Correctly Identifies)

### 3.1 Schema Unification

**Gap:** `surgeon2_state`, `surgeon2_positions`, `surgeon2_trade_log` in `tickles_rubicon`

**Impact:** Low. Only one agent uses custom tables. surgeon1 uses flat files.

**Effort:** 1-2 days to migrate + update `surgeon2_trader.py`

### 3.2 edge_score Formula

**Gap:** No unified scoring formula for trade eligibility

**Impact:** High. Currently eligibility is ad-hoc gate stacking.

**Effort:** 1 day to define + 2 days to wire into Treasury/Execution

### 3.3 CoachService

**Gap:** No versioned prompt management, A/B testing, or rollback

**Impact:** Medium. Currently prompt changes are manual edits.

**Effort:** 3-5 days (new service + MCP tools + UI)

### 3.4 MemoryLibrarian

**Gap:** No automated dedup, promotion, or pruning of Mem0 memories

**Impact:** Medium. Memories will accumulate indefinitely.

**Effort:** 2-3 days (daemon + Qdrant queries)

### 3.5 Strategy Genome Crossover

**Gap:** No automated hybrid strategy generation from top performers

**Impact:** Low-Medium. Manual strategy creation works for now.

**Effort:** 5-7 days (genetic algorithm + backtest validation)

### 3.6 Correlation-Aware Position Sizing

**Gap:** `RiskOfficer` doesn't model portfolio VaR with correlation matrix

**Impact:** High. BTC+ETH longs count as one bet, not two.

**Effort:** 3-4 days (rolling correlation + VaR calculation)

### 3.7 MCP Tools for Validation

**Gap:** Rule 1 validation is daemon-only, not exposed as MCP tools

**Impact:** Medium. Can't trigger autopsy on demand from OpenClaw.

**Effort:** 1-2 days (wrap ValidationEngine methods as MCP tools)

---

## 4. What We Should NOT Build (Redundant)

| Plan Proposal | Why Redundant | What To Do Instead |
|---|---|---|
| `UnifiedPositionWriter` | `InterpretationService` + `PositionMonitor` already write to tracked_positions | Enforce single-writer policy in code review |
| `ExecutionAgent` | `ExecutionRouter` already exists with paper/ccxt/nautilus adapters | Rename/document existing router |
| `GuruKernel` | ChartHackerGuru is already a daemon; no need for abstraction | Keep ChartHacker as-is; add new gurus by copying pattern |
| `MemoryRouter` | `shared.mcp.tools.memory` already handles all tiers | Add `memory.lineage` + `memory.promote` to existing module |
| `RiskOfficer` | `Treasury` + `CrashProtection` + `Guardrails` already exist | Unify into single `RiskEngine` class |
| `MetaAgent` | `souls` service already has 7 sub-agents (Apex, Quant, Ledger, Scout, Curiosity, Optimiser, RegimeWatcher) | Extend existing souls with "Coach" sub-agent |
| New daemon framework | `ServiceDaemon` already has backoff, jitter, heartbeats, SIGINT handling | Use existing framework for any new daemons |
| New systemd templates | `tickles-service@.service` already supports all services via `%i` | Register new services in `SERVICE_REGISTRY` |

---

## 5. Recommended Reconciliation

Instead of the plan's 5-phase roadmap, I recommend **3 tracks** that map to our existing phases:

### Track A: Unify (1-2 weeks)
- [ ] Migrate `surgeon2_*` → unified tables (1 day)
- [ ] Enforce single-writer policy: document + lint rule (1 day)
- [ ] Merge `Treasury` + `CrashProtection` + `Guardrails` → `RiskEngine` (3-4 days)
- [ ] Add `risk.*` MCP namespace (1 day)

### Track B: Measure (2-3 weeks)
- [ ] Implement `edge_score` formula + wire into Treasury (3 days)
- [ ] Add correlation-aware position sizing (3-4 days)
- [ ] Expose `validation.verdict` + `validation.autopsy` as MCP tools (2 days)
- [ ] Wire `trader_score` into `InterpretationService` consensus weights (2 days)

### Track C: Learn (3-4 weeks)
- [ ] Build `MemoryLibrarian` daemon (3 days)
- [ ] Build `CoachService` with versioned prompts (5 days)
- [ ] Add `memory.lineage` + `memory.promote` MCP tools (2 days)
- [ ] Strategy genome crossover (deferred — low priority)

---

## 6. Critical Architectural Decision

The plan and our infrastructure disagree on **how agents should be structured**:

| Approach | Plan's View | Our Reality |
|---|---|---|
| **Services** | Long-running daemons, reusable, multi-company | `ServiceDaemon` + `SERVICE_REGISTRY` — 20+ services |
| **OpenClaw Agents** | Not mentioned | surgeon, surgeon2, ChartHacker — ad-hoc, single-company |
| **MCP Tools** | Control surface for everything | 40+ tools, but not all actions are tool-callable |
| **Souls** | Not mentioned | 7 deterministic sub-agents in `souls` service |

**Recommendation:** Adopt a **3-tier agent model**:

1. **Services** (`ServiceDaemon`) — infrastructure, multi-company, always-on
   - Examples: gateway, candle-daemon, catalog, backtest-workers, position-monitor
   - Registered in `SERVICE_REGISTRY`, managed by systemd

2. **Tools** (MCP) — on-demand capabilities, no state, callable by anyone
   - Examples: chart.analyze, backtest.compose, memory.add, autopsy.run
   - Registered in `ToolRegistry`, discoverable via `tools.catalogue`

3. **Agents** (OpenClaw) — research/experimentation, single-company, ephemeral
   - Examples: surgeon (researching Twilly strategy), ChartHacker (testing vision pipeline)
   - Spawned manually, write to Mem0, may be promoted to services if successful

**Rule:** If an agent does something useful, it should become a **service** or an **MCP tool**, not remain an OpenClaw one-off. OpenClaw is for experimentation only.

---

## 7. What The Plan Misses Entirely

1. **OpenClaw integration** — The plan doesn't account for how OpenClaw agents (with MEMORY.md, SOUL.md, tool-calling) fit into the service architecture

2. **Phase 31-32 Souls** — The 7 sub-agents (Apex, Quant, Ledger, Scout, Curiosity, Optimiser, RegimeWatcher) are already defined in `shared/services/registry.py` but not mentioned in the plan

3. **Dashboard (Phase 36)** — Mobile-friendly HTML SPA with Telegram OTP auth — already exists but disabled on VPS

4. **MCP Server (Phase 37)** — JSON-RPC 2.0 server exposing tools to LLM clients — already exists but disabled on VPS

5. **Copy Trader + Arb Scanner (Phase 33)** — Already implemented, disabled on VPS until Phase 34 wiring

6. **Freshness Guard** — 180-second staleness check on all signals — already implemented in `shared/utils/freshness.py`

7. **3-tier memory with fallback** — If mem0 fails, falls back to `forward_to` envelope for external MCP host — already implemented

---

## 8. Conclusion

The proposed plan is **a good description of what we already have**, with 4-5 genuine gaps identified. Implementing the plan as written would:

- **Waste 2-3 months** rebuilding existing components under new names
- **Create confusion** with parallel roadmaps (plan's Phase 0-5 vs. our Phase 13-37)
- **Miss the real work**: wiring existing pieces together, not building new ones

**Correct approach:**
1. Acknowledge that 80% of the plan already exists
2. Map the remaining 20% (gaps) to our existing phase roadmap
3. Focus on **wiring** (tracked_positions → ChartHacker → agent_opinions → validation → memory)
4. Defer abstractions (GuruKernel, UnifiedPositionWriter) until we have 3+ concrete use cases
5. Keep OpenClaw for experimentation, but require successful experiments to graduate to services or MCP tools

The flywheel is already spinning. We don't need to rebuild the engine. We need to **connect the gears**.
