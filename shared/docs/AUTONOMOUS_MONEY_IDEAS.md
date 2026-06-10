# Autonomous Money Machine — Ideas & Roadmap
*Written 2026-06-10 after the technique-learning / trade-intel implementation round.*
*Companion to: trade_intel.py, technique_tracker.py, technique_stats table, intelligence.techniques_top MCP tool.*

## What just shipped (context for these ideas)

1. **Trade intel** — traders' "moving SL to BE / closed my SOL / taking partials"
   messages now ACT on their open tracked_positions (trade_intel.py). The
   postmortem now sees what the trader actually did, not just what candles did.
2. **Technique ledger** — the vision LLM returns up to 5 observed techniques per
   chart + a market commentary. On position close, each technique gets a win/loss
   up-vote in `technique_stats`, per trader and per symbol. Top performers are
   injected back into the chart prompt, and any agent can query them via the
   `intelligence.techniques_top` MCP tool.
3. **Recall measurement wired** (Phase B) — every mem0 recall at decision time is
   logged and linked to the resulting position, so we can finally answer "does
   memory make us money?"

The first backfill already produced signal: every technique graded so far is
net-negative. That is not a bug — it is the system telling you the current copy
rules lose money, with per-technique attribution. That attribution is the seed
of everything below.

---

## Tier 1 — Compounding what exists (weeks, not months)

### 1.1 Technique-gated sizing
`copy_trade_monitor` currently sizes by fixed % risk. Make size a function of
the technique ledger: if a setup's techniques have a graded win rate > 60% over
n >= 10, size 1.5x; if < 35%, size 0.5x or skip. This converts the ledger from
"observability" into "edge" with ~30 lines in the copier. The data for this
exists TODAY.

### 1.2 Strategy promotion daemon
A small cron that scans `technique_stats` nightly and PROMOTES any
(technique, trader, symbol) tuple with win_rate >= 65% and n >= 20 into the
existing `strategies` table with a generated description. Demote when it decays
below 45%. The MCP tool already lets any agent ask "what works" — promotion
makes it official, versioned, and dashboard-visible. This is the "80% success
ratio over 60 trades" strategy object you described, built from live data
instead of backtests.

### 1.3 Trader edge decay alarms
`trader_performance` + `technique_stats` together can detect a trader going
cold: rolling 10-trade win rate falling below their lifetime mean minus 1 sigma
=> auto-reduce copy size for that trader, write a MemU `warning` insight, ping
the dashboard. Traders' edges decay; the system should notice before the
balance does.

### 1.4 Trader-vs-us postmortem
Now that trade_intel records trader exits (`exit_reason='trader_exit'`,
timestamped notes in exit_reason_trader), the postmortem prompt should compare:
did the trader exit better than our SL/TP did? If trader exits consistently
beat our mechanical exits for a given trader, the copier should adopt
"follow trader exits" mode for that trader. This is learnable per trader from
data we now collect.

## Tier 2 — New money loops (1-2 months)

### 2.1 Funding-rate harvest agent
You already run a funding collector and have perp connectivity on multiple
exchanges. A delta-neutral agent (long spot / short perp, or cross-exchange
perp-perp) collecting funding is the most boring, most reliable money in
crypto. It needs no chart reading, no LLM, and its PnL is nearly deterministic
— a perfect "first autonomous earner" to prove the end-to-end demo->live rail
at 99.9% accuracy, because its trades are small, frequent, and hedged.
MCP tools needed: `funding.spread_scan`, `arb.execute_pair` (the arb_* tables
already exist).

### 2.2 Strategy-watcher agent (the inversion you described)
Instead of waiting for a trader to post a chart, an agent walks the PROMOTED
strategies (1.2), pulls live candles via the candle daemon, and asks "is any
proven setup forming RIGHT NOW on the coins it historically works on?" When
yes, it arms its own tracked_position with actor_id='strategy_watcher'. Same
copier, same competition, same postmortem — but the signal source is the
system's own learned knowledge. This is the moment the system stops being a
copier and starts being a trader.

### 2.3 The indicator-discovery loop
The LLM already names techniques it sees. Next step: when a technique reaches
n >= 30 with stable win rate, spawn a one-shot agent task: "write a Python
indicator that detects <technique> from OHLCV; backtest it on our candle
partitions; report precision vs the LLM's visual detection." Winners get added
to the quant track. Over time the quant snapshot grows organs the vision model
taught it. The agent literally builds its own indicators from observed edge.

### 2.4 Regime-conditional memory
mem0 lessons are currently regime-blind. Stamp each lesson with the
regime_current at write time, and filter recall by current regime. "SL too
tight on SOL" learned in a chop regime is noise in a trend regime. The
regime_* tables exist; this is a metadata field plus a recall filter.

## Tier 3 — The crazy stuff (3-12 months)

### 3.1 The trader clone
Per tracked trader, accumulate: techniques, timeframes, session times, coins,
hold durations, exit behavior (now captured via trade_intel). After ~100
trades you have a behavioral fingerprint. Fine-tune nothing — just build a
system prompt: "You are a clone of <trader>. Here is how they trade: ..."
and let the clone trade the competition WITHOUT waiting for the trader to
post. Measure clone-vs-original correlation. A clone with 0.8 correlation to
a profitable trader is a money printer that never sleeps and never stops
posting.

### 3.2 The ensemble desk
12 competition agents currently copy the same signals with different sizing.
Replace half with genuinely different brains: strategy-watcher (2.2), funding
harvester (2.1), trader clones (3.1), a contrarian that fades the lowest-graded
techniques, and chart_hacker itself. Then a DESK agent allocates capital
weekly across them based on rolling Sharpe from competition_trades — a fund of
funds where every PM is an agent and the allocator is an agent. The dashboard
competition becomes a real capital allocation mechanism.

### 3.3 Self-funding via tool licensing
The MCP server itself is a product. `intelligence.techniques_top`,
`traders.leaderboard`, signal feeds — these are exactly what trading Discord
communities pay for. A read-only, rate-limited, paid API key tier on the MCP
HTTP daemon (it already has auth and rate-limit hooks) turns the system's
learning exhaust into subscription revenue that funds the trading capital.
The agents' job: keep the data good. Your job: collect.

### 3.4 The capital ladder (paper -> demo -> live, automated)
Codify promotion gates as a daemon, not a decision:
- paper agent with 90d Sharpe > 1.5 and max DD < 15% => gets demo account
- demo agent tracking paper within 1% slippage for 30d => gets live with $100
- live agent doubling its stake => stake doubles (up to a cap)
- any agent breaching DD limit => demoted one rung, postmortem written to MemU
This is the "set them free" mechanism with survival pressure built in. Agents
earn their capital; the ladder is the natural selection.

### 3.5 Cross-company swarm memory
MemU is multi-tenant by design (company_id). When you spin up company #2 with
its own agents, the `playbook` and `warning` insight kinds should be shared
across companies while `lesson` stays private. Hard-won knowledge ("bitget
demo rejects qty < 0.01 on SOL") propagates to every future agent on the
server instantly. New agents are born knowing what every dead agent died of.

## The one metric that matters

Every idea above feeds one number the dashboard should show at the top:
**autonomous PnL per day, by source** (copied / strategy-watcher / funding /
clones). When that line is reliably positive across 90 days with the capital
ladder enforcing risk, you scale by adding capital, not code.

## Implementation order (my recommendation)

1. 1.1 technique-gated sizing (days — data already flowing)
2. 1.2 strategy promotion daemon (the strategies table is waiting)
3. 2.1 funding harvester (first genuinely autonomous earner, lowest risk)
4. 1.4 trader-vs-us exits (data started accruing today via trade_intel)
5. 2.2 strategy-watcher (the inversion — the system's first self-sourced trade)
6. 3.4 capital ladder (formalize before any live money)
