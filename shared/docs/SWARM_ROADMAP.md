# The Swarm Vision — Tickles & Co Ultimate Roadmap

**Status:** VISION CAPTURED — 2026-04-21
**Author:** AI (dg) per CEO stream-of-consciousness
**Supersedes:** Nothing — this is additive to MCP_AND_MEMORY_PLAN.md

---

## The Big Picture

You want a **hive mind trading company**. Not one agent. Not two. A colony of specialized agents that work together, compete, learn from each other, and ultimately make money while you sleep.

This document captures that vision in concrete, buildable terms.

---

## Pillar 1: Social Alpha Mining (Discord/Telegram Collector)

**What it is:**
An agent that sits in Discord/Telegram trading channels, reads trade calls from mentors, extracts structured data (asset, direction, entry, SL, TP), and stores it.

**The Truth Engine:**
After storing a call, the system backtests it against historical data:
- Did the mentor's call hit TP1? TP2? Was it stopped out?
- Was the call even realistic given market conditions at that time?
- Did the mentor post follow-up "TP1 hit!" messages that are actually the same trade?

**Storage:**
- **MemU (Tier 3):** "Mentor_X called Long BTC @ 74k on April 20. Backtest result: Hit TP1 (+1%), SL never touched. Verdict: Valid signal."
- **Duplicate Detection:** If a mentor posts "TP1 hit!" as a new message, the agent links it to the original call instead of creating a duplicate.

**Cost:** This agent runs continuously but only acts when new messages arrive. Cheap.

---

## Pillar 2: Multi-Asset Expansion (Beyond Crypto)

**Current state:** Bybit/Binance perps (crypto only).

**Expansion targets:**
- **CFDs via Capital.com** — NASDAQ, S&P 500, TSLA, SOXL (semiconductors)
- **Apex/PipFarm** — Prop firm challenges
- **Commodities** — Oil, Gold

**New Agent Types:**
- "S&P Scout" — reads 1m/5m candles on SPX500, looks for divergence
- "SemiConductor Analyst" — tracks SOXL, NVDA, AMD patterns
- "Commodity Watcher" — Oil/Gold correlation trades

**Key requirement:** Each new asset class needs its own MCP tool group (`capital.*`, `apex.*`) so agents can discover and use them.

---

## Pillar 3: The Competition Layer

**Intra-company competition:**
- 5-6 agents in one company share memory but compete for P&L.
- Winner gets more "compute budget" (higher cron frequency) or exclusive access to the Analyst model.

**Inter-company competition:**
- Company A ("The Bulls") vs. Company B ("The Bears")
- Each has isolated mem0 (private lessons) but shared memu insights (cross-company learnings)

**Contest Rules:**
- `contest.create(name, venues, coins, duration, starting_paper_usd)`
- `contest.leaderboard(contest_id)` — live P&L ranking
- `contest.end(contest_id)` — freezes balances, auto-runs postmortem, stores learnings

**Cost model for contests:**
- Scouts: 10x Gemini Flash ($0.01/cycle) — cheap sensors
- Analysts: On-demand Kimi/Claude ($0.50/call) — expensive but rare
- CEO: 1x Kimi per hour ($0.20/hour) — strategy only

---

## Pillar 4: Backtest Integrity (The 99.9% Rule)

**The Contract:**
If a live trade diverges from the backtest by >0.1%, the system must explain why (slippage, latency, stale data, etc.).

**Validation Agent:**
- Runs for 48 hours in "paper mode"
- Compares live fills to expected fills
- Reports discrepancies with root cause analysis

**Why this matters:**
You said it yourself: "The number one rule of the system — if there's a backtest, live trades have to match 99.9%. If it doesn't, there's something wrong and I need to know what it is."

**Implementation:**
- `backtest.parity_check(live_trade_id, backtest_id)` — returns deviation % and explanation
- `backtest.calibrate(symbol, strategy)` — adjusts slippage model based on historical deviation

---

## Pillar 5: Dynamic Strategy Evolution

**Research Agent:**
- Writes Python strategy code based on market observations
- Uses `strategy.create(name, code, params_schema)` MCP tool

**Backtest Agent:**
- Tests new strategies against 60 days of historical data
- Uses `backtest.submit(spec)` + `backtest.status(id)` + `backtest.top_k()`

**Deploy Agent:**
- If strategy passes backtest → updates Surgeon's SOUL.md with new logic
- Uses `strategy.deploy(strategy_id, agent_id)` MCP tool

**Rollback Agent:**
- If live P&L drops after deployment → reverts to previous SOUL version
- Uses `strategy.retire(strategy_id)` + restores previous `SOUL.md` from git

**The loop:**
```
Research → Backtest → Deploy → Monitor → (If bad) Rollback → Research again
```

---

## Pillar 6: Cost-Conscious Swarm Architecture

**Three-Tier Power Stack (already established):**

| Tier | Role | Model | Cost | When Used |
|------|------|-------|------|-----------|
| **Brain** | Planning, refactoring, architecture | Claude 3.5 Sonnet / Kimi K2.6 | High | Rare — only for major decisions |
| **Executioner** | Complex end-to-end coding | Kimi K2.6 | Medium | When building new features |
| **Tactical** | Fast, cheap execution | Gemini 2.0 Flash | Very Low ($0.01/cycle) | Every 5 minutes, all day |

**Additional Cost Controls:**
- `--tools` whitelist: Agents only see MCP tools they need (prevents drowning)
- `--light-context`: Reduces bootstrap token usage
- Cron frequency scaling: Winning agents get more cycles, losing agents get fewer

---

## Pillar 7: The "Edward" Agent — Continuous Memory

**What it is:**
A persistent agent (or just a memory system) that tracks everything happening across all channels.

**Capabilities:**
- "Hey Edward, what's the latest news on Iran?" → Queries GDELT + news RSS
- "What happened 10 minutes ago in the trading channels?" → Queries Discord/Telegram collector
- "What's the current P&L of all companies?" → Queries banker snapshots
- "Show me all trades today where SL was hit" → Queries execution logs

**Why this matters:**
You want to be able to ask natural language questions about your entire operation and get answers. This is the "human interface" to the hive mind.

**Implementation:**
- Could be a dedicated agent with access to all MCP tools
- Or could be a query interface over MemU (Tier 3) + company DBs
- Probably both: agent for complex queries, direct DB for simple ones

---

## Pillar 8: Cross-Company Learning & Memory Sharing

**Shared vs. Isolated Memory:**

| Type | Scope | Use Case |
|------|-------|----------|
| **Isolated (mem0 per agent)** | Private lessons | "I learned that RSI divergence on SOL doesn't work after 22:00 UTC" |
| **Company-shared (mem0 per company)** | Team knowledge | "Our surgeon strategy works best on high-volatility days" |
| **Cross-company (MemU Tier 3)** | Global insights | "BTC mean-reversion is profitable across all companies this week" |

**Dynamic Memory Sharing:**
- Agents can "opt in" to share specific learnings
- CEO can broadcast critical insights to all companies
- Competition mode: Agents can see others' P&L but not their private strategies

---

## Pillar 9: Agent Specialization Roles

**Scout Agents (10x Gemini Flash):**
- Each watches one asset every 5 minutes
- Very cheap ($0.01/cycle)
- Reports anomalies: "BTC funding rate spiking", "Unusual volume on SOL"

**Analyst Agents (on-demand Kimi/Claude):**
- Called by scouts when something interesting is found
- Deep analysis: "Is this volume spike driven by news or manipulation?"
- Expensive but rare

**Executioner Agents (Gemini Flash + specific tools):**
- Actually places trades (paper first, then demo, then live)
- Follows surgeon strategy exactly
- Writes to TRADE_STATE.md + TRADE_LOG.md

**CEO/Orchestrator Agent (Kimi K2.6, hourly):**
- Reviews all scout reports
- Decides which analysts to activate
- Allocates compute budget
- Runs the competition leaderboard

**Auditor Agent (daily):**
- Checks backtest/live parity
- Flags discrepancies
- Reports to CEO

---

## Pillar 10: Live Trading Integrity

**The Gate:**
- Paper → Demo → Live graduation
- Each level requires:
  1. 30 days of profitable paper trading
  2. Backtest/live parity < 5% deviation
  3. CEO approval via Paperclip `approvals` workflow

**Risk Limits (per company):**
- Max notional per trade
- Max daily loss
- Max drawdown before auto-pause
- Venue-specific caps

**The 99.9% Rule:**
- Every live trade is compared to its backtest
- If deviation > 0.1%, system explains why
- If unexplained, trade is flagged for review

---

## Implementation Phases (Proposed)

### Phase S1: Social Alpha Mining (Week 1-2)
- Enable Discord collector (`systemctl enable tickles-discord-collector`)
- Enable Telegram collector
- Build `social.extract_trade_call` MCP tool
- Build `social.backtest_mentor_call` MCP tool
- Store results in MemU

### Phase S2: Multi-Asset Expansion (Week 2-3)
- Verify Capital.com credentials in `.env`
- Build `capital.quote`, `capital.candles`, `capital.place_order` MCP tools
- Test with paper trades on SPX500, TSLA, SOXL
- Create "S&P Scout" agent

### Phase S3: Competition Layer (Week 3-4)
- Build `contest.create`, `contest.leaderboard`, `contest.end` MCP tools
- Create 2-3 test companies
- Run first intra-company competition (paper only)
- Build leaderboard dashboard

### Phase S4: Dynamic Strategy Evolution (Week 4-5)
- Build `strategy.create`, `strategy.backtest`, `strategy.deploy` MCP tools
- Create Research Agent prototype
- Test auto-deploy + auto-rollback

### Phase S5: "Edward" Interface (Week 5-6)
- Build natural language query over MemU + company DBs
- Connect to Telegram bot for easy access
- Test: "What's my P&L today?" → accurate answer

### Phase S6: Full Swarm (Week 6+)
- 10x Scout Agents running continuously
- 2-3 Analyst Agents on standby
- 2-3 Executioner Agents per company
- 1 CEO Agent
- 1 Auditor Agent
- Full competition mode active

---

## Cost Estimates (Monthly, Full Swarm)

| Component | Count | Cost Each | Monthly Total |
|-----------|-------|-----------|---------------|
| Scout agents | 10 | $0.01/cycle × 288 cycles/day | $864 |
| Executioner agents | 5 | $0.01/cycle × 288 cycles/day | $432 |
| Analyst calls | ~50/day | $0.50/call | $750 |
| CEO agent | 1 | $0.20/hour × 24h | $144 |
| Discord/Telegram collectors | 2 | Server cost only | $0 |
| **Total** | | | **~$2,190/month** |

**Note:** This is for FULL swarm. Start with 2-3 agents and scale up.

**Cost reduction levers:**
- Reduce scout frequency (every 10 min instead of 5 min)
- Batch analyst calls (one per hour instead of on-demand)
- Use cheaper models for scouts (Gemini Flash is already cheapest)

---

## Critical Success Factors

1. **Data quality is everything** — Bad candles = bad backtests = bad strategies = lost money
2. **Start paper, stay paper until proven** — No live trading until 99.9% parity achieved
3. **Cost discipline** — Every agent must justify its compute budget with P&L
4. **Memory hygiene** — Old, unused memories must be archived or deleted
5. **Human oversight** — You approve live trading, new strategies, and major config changes

---

## Open Questions for CEO

1. **Which Discord/Telegram channels first?** (Need invite links or bot tokens)
2. **Which CFD assets first?** (SPX500, TSLA, SOXL, or others?)
3. **How many agents in first competition?** (2, 3, or 5?)
4. **What's the starting paper budget per agent?** ($500? $1,000? $10,000?)
5. **Live trading threshold?** (30 days paper + 99% parity? Or stricter?)

---

*End of Swarm Roadmap. Ready for Phase S1 upon CEO approval.*
