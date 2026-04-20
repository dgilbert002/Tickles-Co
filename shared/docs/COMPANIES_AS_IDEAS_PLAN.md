# Companies as Ideas — The Tickles Building Master Plan

> Deep audit and implementation plan for the "Rolls-Royce-simple company" vision.
> Audit date: Apr 10, 2026. Author: Cursor Opus 4.7 (dg).
> Supersedes: NEW_CHAPTER_AUDIT_REPORT.md (still valid; this is the plan built on top of it).

---

## Part 1 — What you actually have (so nobody forgets again)

### 1.1 Two systems, one dream

| System | Where | Role |
|--------|-------|------|
| **JarvAIs** (`C:\JarvAIs`) | Windows dev box | Original trading brain — 22 agents, mem0+Qdrant RAG, trade_dossier lifecycle, shadow trader, live CCXT/MT5 execution, 8 vector collections, prompt versioning, A/B testing, confidence calibration. Self-contained. This is the trading IP. |
| **Tickles-Co** (`C:\Tickles-Co` + VPS `/opt/tickles`) | Local + VPS | The new operating platform — OpenClaw runtime + Paperclip UI + per-company MySQL DBs + shared memory (MemU + mem0) + banker/treasury/souls/services catalog. This is the building. |

The mistake I keep making is treating them as competing. They aren't. **JarvAIs is the proven trading engine; Tickles-Co is the holding-company / multi-office chassis.** The plan below marries them.

### 1.2 Three memory tiers — actually already built

| Tier | Component | Location | Scope | What it holds |
|------|-----------|----------|-------|---------------|
| **Tier 1: Agent-private** | `mem0` (Qdrant) | VPS `qdrant@6333` | `collection=tickles_{company}`, `user_id={company}`, `agent_id={company}_{agent}` | Each agent's private lessons, observations, reasoning traces. Mandatory wrapper: `shared.utils.mem0_config.get_memory(company, agent)`. |
| **Tier 2: Company-shared** | `tickles_{company}` MySQL + `mem0` collection-level | VPS MySQL 3306 + Qdrant | Per-company | Trades, cost entries, order events, validations, balance snapshots, leverage history, agent state, strategy lifecycle, company config. Plus shared mem0 collection all company agents can read. |
| **Tier 3: Cross-company (Building-wide)** | `MemU` (Postgres + pgvector) | VPS Postgres `memu` db | Global | `insights` table keyed by `(kind, content_hash)`, vectorized (384d all-MiniLM-L6-v2), pg_notify broadcasts every insight. This is the "everyone hears what everyone learns" channel. |

All three are wired. The gap is that agents don't yet *routinely* write to Tier 3 after every trade. That's a cron+routine, not new code.

### 1.3 The banker / treasury / souls stack

| Component | File | What it does | Status |
|-----------|------|--------------|--------|
| **Banker** | `shared/trading/banker.py` | Append-only balance/equity snapshots per company+exchange+account. Says "how much capital do we have." | Built, DB-backed, tested. |
| **Capabilities** | `shared/trading/capabilities.py` | What venues/symbols/leverage/notional a company is allowed. | Built. |
| **Sizer** | `shared/trading/sizer.py` | Given intent + account + market + strategy, returns sized intent. | Built. |
| **Treasury** | `shared/trading/treasury.py` | Orchestrates capability-check → banker-read → sizer → persisted `TreasuryDecision`. | Built. This is **the gate** for every trade. |
| **Souls** | `shared/souls/{protocol,service,store}.py` + `personas/` | 7 archetypes: Apex (decision), Quant (research), Ledger (bookkeeper), Scout (discovery), Curiosity (explorer), Optimiser (tuning), RegimeWatcher (alert). Deterministic today, LLM-ready. | Built. `SoulsService` persists every decision to `agent_decisions` — audit-complete. |
| **Services Catalog** | `shared/catalog/` + `shared/services/` | Registry of runnable daemons + skills + MCP tools. | Built. |
| **MCP Server** | `shared/mcp/server.py` | JSON-RPC 2.0 surface exposing Tickles capabilities to agents. | Built. |
| **Backtest parity (Rule 1)** | `shared/backtest/parity.py` | Continuous auditor: "backtest must equal live." | Built. |
| **Execution adapters** | `shared/trading/adapters/` + `shared/connectors/{ccxt,capital}_adapter.py` | Paper, CCXT, Capital.com. | Built. Capital.com is **already there**. |
| **MemU client** | `shared/memu/client.py` | Hardened, thread-safe, deduping, broadcasting. | Built. |

### 1.4 Paperclip database (already there)

**69 tables.** Key ones for our model:
- `companies` — 2 today (`Tickles n Co`, `Building`). Supports arbitrary more.
- `agents` — 7 today, all using `openclaw_gateway` adapter. `adapter_config` JSON stores per-agent model/keys.
- `routines` — scheduled/triggered work (cron equivalent, native).
- `plugins` — what capabilities a company/agent has.
- `budget_policies` — spend caps per company (hello, OpenRouter cost tracking).
- `finance_events` — every cost/revenue event, per company.
- `issues` + `approvals` — the "what needs human OK" channel.
- `heartbeat_runs` — agent liveness.
- `goals` — company purpose (Building's goal is already a holding-company goal statement).

### 1.5 The two companies that exist today

| Company | Purpose | Agents | Why it matters to the plan |
|---------|---------|--------|----------------------------|
| **Tickles n Co** | Holding / HQ | Main (broken IP), Cody, Schemy, Audrey | This is **your** HQ. CEO lives here. Cody/Schemy/Audrey maintain the codebase. |
| **Building** | Infrastructure / platform | CEO, Janitor, Strategy Council Moderator | This is the **services/MCP/memory layer**. Strategy Council Moderator is already here and is exactly what you asked for: the chair of cross-company lessons. |

Together they form the **holding + platform** model. Every new company = a new "office" renting services from Building.

---

## Part 2 — The operating model ("Rolls-Royce-simple companies")

### 2.1 The picture

```
                    ┌────────────────────────────┐
                    │      Tickles n Co          │   ← holding co, you are CEO
                    │   CEO · Banker · Treasury  │      (owns the keys)
                    └─────┬──────────────┬───────┘
                          │              │
               sets caps/limits    records P&L
                          │              │
                    ┌─────▼──────────────▼───────┐
                    │        Building             │   ← platform / MCP
                    │  Services · Skills · MemU  │     Strategy Council Moderator
                    │  Cody · Schemy · Audrey    │     keeps the codebase sane
                    │  Janitor · StrategyCouncil │
                    └─────┬──────────────┬───────┘
                          │              │
                services + insights shared
                          │              │
     ┌────────────────────┼──────────────┼────────────────────┐
     │                    │              │                    │
┌────▼────┐         ┌─────▼────┐   ┌─────▼────┐        ┌──────▼─────┐
│ SurgeonCo│         │ PolyDesk │   │ LPCopyCo │        │ Capital2Co │
│ (crypto  │         │ (predict │   │ (Meteora │        │ (CFD on    │
│  perps,  │         │  markets)│   │  wallet  │        │  Capital.  │
│ Binance) │         │          │   │  copy)   │        │  com)      │
└──────────┘         └──────────┘   └──────────┘        └────────────┘
    each one is a company = an idea = 1-20 agents with a soul, skills, MCP tools
```

### 2.2 What "opening a company" does under the hood

Today you type in Paperclip "new company = PolyDesk." One click should trigger this chain:

1. **Paperclip** — insert into `companies` (already works).
2. **Tickles MySQL** — run `new-project.sh polydesk` to create `tickles_polydesk` DB (already exists; needs Paperclip hook).
3. **mem0 / Qdrant** — `collection=tickles_polydesk` created on first `get_memory()` call (automatic).
4. **Tickles capabilities** — seed default capability rows (e.g., "can trade on Polymarket via Simmer skill, max $500 notional, max 5% daily loss"). From a preset template.
5. **Banker** — register at least one account (paper account by default, live/demo on promotion).
6. **Souls** — seed the 7 persona rows into per-company `agent_personas` (auto via `SoulsService.seed_personas`).
7. **MemU** — no-op at create time; company's agents start writing insights as they work.
8. **Strategy Council Moderator** — subscribed to `pg_notify('memu_insights')` so every company's learnings surface at the next board meeting.
9. **Routines** — install the 3 standard routines on every agent:
   - **Heartbeat** (every 5 min; keeps agent alive)
   - **Post-trade Autopsy** (every closed trade; writes `kind=autopsy` insight to MemU + Tier-1 mem0)
   - **Nightly Post-Mortem** (once/day; writes `kind=postmortem` insight summarizing day)
   - **Weekly Feedback Loop** (once/week; proposes prompt/soul edits via `approvals` workflow)
10. **Plugins** — user picks which skills/MCP tools this company can use (e.g., `polymarket-live-bet`, `polymarket-analysis`, `xurl`, `rss-aggregator`). Written to `plugins` + surfaced to the agent's context.
11. **Budget policy** — default `llm_budget_monthly_usd=50`, `max_drawdown_pct=25`, etc. Editable in UI.

**Closing a company**: reverse the chain. `delete-project.sh polydesk` drops the MySQL DB. mem0 Qdrant collection is deleted. Paperclip company archived. MemU insights from that company are *kept* (flagged `archived_company=true`) so the lessons don't die with the company.

### 2.3 The universal trade ledger — already there

`tickles_{company}.trades` already supports every venue you named because it's schema-neutral:
- `exchange` VARCHAR — "binance", "bybit", "aster", "capital.com", "alpaca", "polymarket", "solana_meteora"
- `trade_type` ENUM — `live | paper | shadow`
- `quantity_type` ENUM — `units | lots | contracts` (covers crypto, CFD, stocks, options)
- `contract_size` DECIMAL — critical for CFDs (Capital.com uses contracts, not units)
- `direction` ENUM — `long | short` (for LP positions we extend to `long | short | lp` — 1 enum add)
- `trade_cost_entries` — 9 cost types already: `maker_fee, taker_fee, spread, overnight_funding, swap, commission, guaranteed_stop, slippage, other`. Covers everything.

Extensions needed (tiny):
- Add `wallet_address` VARCHAR to `trades` (for DEX trades + Solana LP copy)
- Add `mirror_wallet` VARCHAR (who we're copying, for copy-trade companies)
- Add `prediction_market` BOOLEAN + `outcome_token_id` VARCHAR (for Polymarket)
- Add `'lp'` to `direction` enum (for Meteora DLMM positions)

### 2.4 Rule 1 (backtest = live), per-company

Already has `trade_validations` table and `shared/backtest/parity.py` auditor. Make it **per-company opt-in** via `company_config.rule_one_mode`:
- `strict` — no live trade without backtest parity <5% deviation (current Tickles default)
- `advisory` — backtest runs alongside, agent warned, no block
- `off` — human-authorized live-only company (e.g., "just copy this wallet")

### 2.5 Paper → Demo → Live graduation

The Banker already tracks `account_type` = `demo | live | paper`. Graduation is a **Treasury capability**, not new code:

| Stage | Banker account_type | Treasury cap | Who authorizes |
|-------|---------------------|--------------|----------------|
| Paper | paper | unlimited (within company budget) | Company CEO agent |
| Demo | demo | cap per venue (e.g., $500 total on Bybit demo) | Holding-company Treasurer (you or your CEO) |
| Live | live | cap per venue + per strategy + daily loss limit | You only (via approvals queue) |

A company's agents can request graduation via an `approvals` row; the approver sees 30-day paper P&L + backtest parity + risk profile + proposes graduation caps.

---

## Part 3 — The glue gap (what we build next)

Everything below is **glue**, not new capability. The capability is already there.

### Gap 1: Paperclip "New Company" wizard

**Today:** User creates a Paperclip company → nothing happens on the Tickles side.
**Target:** User picks a company template (`CryptoDesk`, `CFDDesk`, `PolyDesk`, `LPCopyDesk`, `Custom`), Paperclip calls a MCP tool `tickles.bootstrap_company(slug, template)` which runs steps 1-11 from §2.2 atomically. Idempotent — re-running heals partial state.

**Build:** 1 MCP tool + 1 UI form + template JSON files. ~300 LOC total.

### Gap 2: Paperclip "New Agent" wizard per company

**Today:** Agents are created via SQL INSERT; soul is a file path; skills are added manually.
**Target:** In Paperclip → company → "+ Agent" → pick soul preset (Apex/Quant/Ledger/Scout/Surgeon/SylvanShort/MeteoraCopy/custom) → pick skills (multiselect from Building's plugin catalog) → pick model (OpenRouter dropdown with live cost per 1M tokens) → write or inherit AGENTS.md → Deploy.

**Build:** 1 UI form + 1 MCP tool `tickles.register_agent(company, name, soul, skills, model)` + `SoulsService.seed_personas(company_id)` hook. ~400 LOC.

### Gap 3: OpenRouter cost tracking per agent

**Today:** OpenClaw logs per-call cost somewhere in `~/.openclaw/logs/`; Paperclip's `finance_events` table can hold it; nothing bridges them.
**Target:** Hook OpenClaw's response logger → emit `finance_event(company_id, agent_id, kind='llm_call', amount_usd, model, tokens_in, tokens_out)` for every call. Paperclip shareholder dashboard rolls up by company and by agent.

**Build:** 1 OpenClaw plugin (~100 LOC) + 1 SQL materialised view for daily rollup + 1 dashboard card. Also enforce `company_config.llm_budget_monthly_usd` — when exceeded, agent is paused (Treasury capability check).

### Gap 4: Mandatory autopsy/post-mortem/feedback routines

**Today:** Twilly's 3-prompt framework exists in a doc. JarvAIs has a postmortem pipeline. Tickles has `Ledger` soul.
**Target:** Every new agent gets 3 scheduled routines auto-installed:
- **Autopsy** — trigger: `trade_closed` event. Runs cheap model (`glm-5-turbo` or `gemini-2.5-flash`) over the trade's data + dossier. Output: `{lesson, root_cause, what_next}` → `mem0.add(agent_id=...)` AND `memu.write_insight(kind='autopsy', source_agent=...)`.
- **Post-mortem** — trigger: `cron 23:00 daily`. Runs over the day's closed trades. Output → `mem0` company-scoped + `memu(kind='daily_postmortem')`.
- **Feedback loop** — trigger: `cron sunday 09:00`. Reads last 7 days of autopsies, proposes soul/prompt/threshold changes → creates a Paperclip `approval` row for human sign-off.

**Build:** 3 routine templates (SOUL.md equivalent) + 1 OpenClaw skill `tickles-learning-loop` + wiring into `SoulsService`. ~500 LOC.

### Gap 5: Shareholder dashboard

**Today:** Paperclip has per-company views, no cross-company rollup.
**Target:** New Paperclip view `/shareholder`:
- **Top band:** Total P&L across companies · Active companies · Active agents · Total LLM spend MTD · Free capital · Deployed capital.
- **Company grid:** card per company (name, purpose, 7-day P&L sparkline, total trades, win rate, current drawdown, agents status, LLM spend, rule-1 parity score). Click drills into company.
- **Leaderboards:** best strategy across companies, most expensive agent, most profitable agent, biggest mover today.
- **Cross-company lessons feed:** last 20 MemU insights with `kind in (autopsy, daily_postmortem, feedback)`.

**Build:** 1 Paperclip page + 5 aggregate SQL queries. ~600 LOC.

### Gap 6: Capability gating at the holding level

**Today:** Capabilities are defined per-company in `shared/trading/capabilities.py` but the API key storage is scattered.
**Target:** `holding_wallets` table in `tickles_shared`:

```sql
CREATE TABLE holding_wallets (
  id BIGINT PK,
  wallet_name VARCHAR(100),        -- "Bybit Demo", "Capital.com Demo", "Solana Treasury"
  venue VARCHAR(50),               -- "bybit", "capital.com", "solana_meteora"
  account_type ENUM('demo','live','paper'),
  api_key_ref VARCHAR(100),        -- ref, not key itself
  allowed_companies JSON,          -- ["polydesk","surgeoncо"] — NULL=all
  max_notional_usd DECIMAL,        -- per-company cap this wallet enforces
  max_daily_loss_usd DECIMAL,
  is_active BOOLEAN
);
```

Treasury reads this at decision time. A company's Treasury evaluates against the intersection of the company's capabilities AND the holding's wallet allow-list. **Result:** you can safely hand $500 to LPCopyCo on a Solana demo wallet without it ever touching your Bybit live keys.

**Build:** 1 table + 1 MCP tool `holding.grant_wallet(wallet_id, company_slug, cap)` + UI in Tickles n Co dashboard. ~200 LOC.

### Gap 7: Agent curiosity (skills/services discovery)

**Today:** ClawHub has 100+ skills installed but agents don't systematically browse them.
**Target:** Curiosity soul already exists in `shared/souls/personas/curiosity_soul.py`. Give it an MCP tool `building.list_skills(category, for_company)` that returns the plugin catalog. Curiosity soul runs hourly, ranks "interesting unseen skills" by the company's goal, and writes a `kind=skill_suggestion` insight to MemU. Human approves → skill activated for that company.

**Build:** 1 MCP tool + 1 Curiosity soul hourly routine + 1 Paperclip review UI. ~250 LOC.

### Gap 8: Help ME (Cursor) stop forgetting

This is serious — I forgot the memory architecture even while the user was explaining it. Two fixes:

1. **New Cursor rule** `.cursor/rules/tickles-architecture.mdc` — auto-loaded on every session, contains the 3-tier memory map, where banker/treasury/souls/MemU live, the mem0 scoping rules, and "ALWAYS read `C:\Tickles-Co\CLAUDE.md` + `C:\JarvAIs\openmemory.md` before planning anything."
2. **OpenMemory project fact** — store this plan's key architectural decisions as `project_info` memories in OpenMemory (project_id `dgilbert002/JarvAIs` or a new `dgilbert002/Tickles-Co`) so even without the rule I recall them on memory search.
3. **MCP access to MemU from here** — add a tiny Cursor MCP shim that calls the VPS MemU's `search()` over Tailscale. Then in Cursor I can `search-memu("banker treasury")` and pull live insights from the operating system. Optional but excellent.

**Build:** 1 rule file + 4 project-info memory writes + 1 MCP shim. ~150 LOC.

---

## Part 4 — Phased implementation (cash-to-market path)

Theme: **first live paper P&L visible in Paperclip within 10 days.**

### Phase 40 — Wiring (week 1)

1. **Fix Main agent** on VPS (Tailscale IP repair) so the holding CEO chat works.
2. **Gap 1** — Paperclip "New Company" wizard + MCP tool + 4 templates (`CryptoDesk`, `PolyDesk`, `CFDDesk`, `LPCopyDesk`).
3. **Gap 2** — Paperclip "New Agent" wizard + MCP tool + 7 soul presets.
4. **Gap 3** — OpenRouter cost → `finance_events` plugin + shareholder cost card.
5. **Deploy `SurgeonCo`** (first test company) with a single `surgeon` agent, paper mode on Bybit demo. Start writing autopsies. **First tangible P&L visible here.**

### Phase 41 — Learning loops (week 2)

6. **Gap 4** — 3 mandatory routines installed on every agent (autopsy, post-mortem, feedback loop).
7. **Gap 5** — Shareholder dashboard v1 (read-only).
8. **Gap 8** — Cursor rule + MemU memory writes + MCP shim.

### Phase 42 — Curiosity + capital gating (week 3)

9. **Gap 6** — `holding_wallets` + Treasury gating.
10. **Gap 7** — Curiosity soul skill-discovery routine.
11. Deploy **PolyDesk** (Polymarket company) using the `polymarket-weather-trader` + `polymarket-analysis` ClawHub skills. One agent, paper-only, claim from Simmer.

### Phase 43 — Production (week 4)

12. Deploy **LPCopyDesk** (Meteora DLMM copy) — requires the Solana skill we identified as needing a custom build (Helius RPC + Meteora SDK).
13. Deploy **Capital2Co** (CFD on Capital.com) — already have `capital_adapter.py`.
14. **Paper→Demo graduation** for SurgeonCo (approvals workflow).
15. Strategy Council Moderator **first board meeting** — synthesises insights across the 4 companies into a weekly report.

### Phase 44+ — scale (open-ended)

- Agent competitions (same company, different souls, same symbol — best P&L wins the strategy)
- Cross-company strategy promotion (SurgeonCo's winning setups become templates available to any new CryptoDesk)
- External mentor ingestion (Discord channel → mentor signals → mentor company observes every other company)
- Live graduation for proven companies

---

## Part 5 — First concrete deployment (what I want to do first)

**Surgeon agent in SurgeonCo, paper mode, Bybit demo. Start writing autopsies into MemU.**

Why first:
- Uses only software already on the VPS (OpenClaw, Paperclip, Binance/Bybit keys in .env, mem0+Qdrant, Treasury, Banker)
- Gives you visible numbers in Paperclip within a day of deployment
- Exercises every tier of the memory stack (mem0, company DB, MemU)
- Validates the autopsy/post-mortem/feedback loop on a real live trade flow
- Makes Phase 40 worth doing because the wizard is now reusable

Things I still need you to decide (4 questions):

1. **Holding structure confirmed?** — Tickles n Co = you as CEO. Building = platform/MCP/Strategy Council. Every idea = new company. Banker+Treasury at holding level. **Yes/No.**
2. **Banker+Treasury = holding, capabilities per company?** — You grant each company access to a subset of holding wallets with caps, Treasury enforces. **Yes/No.**
3. **First company = SurgeonCo on Bybit demo paper?** Or do you want a different first office? (Possibilities: SurgeonCo, PolyDesk, LPCopyDesk, Capital2Co — I'd pick Surgeon because it's the most self-contained and we have all components ready.)
4. **Rule 1 default for new companies — `strict`, `advisory`, or `off`?** My recommendation: `advisory` (backtest runs, never blocks, but the parity score is on the dashboard). You can flip individual companies to `strict` later.

---

## Part 6 — Things I will stop forgetting

(This section exists for me, not you.)

- **Two trading systems, not one:** JarvAIs (trading IP) + Tickles-Co (platform). Before planning, read `C:\JarvAIs\openmemory.md` AND `C:\Tickles-Co\CLAUDE.md`.
- **Memory has 3 tiers:** mem0 (per-agent, `get_memory(company, agent)`), per-company MySQL (`tickles_{company}`), MemU (cross-company pgvector insights). Never write "memory.md" as the only target.
- **Per-company MySQL DBs exist** via `new-project.sh` — Paperclip companies must trigger this, else the company has no trade ledger.
- **Banker, Treasury, Capabilities, Sizer are already built** and in `shared/trading/`. Don't re-implement.
- **Souls are already built** — 7 of them, persisted, auditable. Adding a new one = persona row + class inherit. Don't design a new framework.
- **Services catalog + MCP server are built** — if a tool exists, expose it, don't re-build it.
- **ClawHub has ~100 installed skills** (polymarket-*, arbitrage-*, copytrade-*, rss-*, discord-*). Check this first before building anything new.
- **OpenClaw + Paperclip are running in production** on the VPS — they're not abstractions to replace.
- **Capital.com adapter exists** in `shared/connectors/capital_adapter.py`.
- **Rule 1 parity** is a shared service already written.
- **The user pays for OpenRouter.** Every design choice should be weighed against inference cost. `glm-5-turbo`, `gemini-2.5-flash`, `deepseek-chat` are the cheap defaults; premium models only when the decision warrants it.
- **Companies are ideas.** One company = one idea = 1-20 agents. Open, run, measure, close. Dynamic.

---

*End of plan. Pending your sign-off on the 4 questions in §5 before any code changes.*
