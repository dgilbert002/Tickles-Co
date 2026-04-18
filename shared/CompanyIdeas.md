# CompanyIdeas.md — Tickles & Co tenant backlog

> **Purpose.** The set of trading companies we plan to stand up on the
> The Platform, each with a different reason-for-being. Every entry
> follows the same template so that when Phase 5 (Provisioning) is ready, any
> of these can be spun up by filling in the blanks and running the CLI.
>
> **Reminder.** Multi-company is a first-class constraint throughout the
> codebase. Anything built in Phases 1A–12 must assume "N companies", even
> if right now we only run JarvAIs.
>
> **Audience.** Dean, CEO agent, Cody (builds), Schemy (schema), Audrey
> (review). When a new company is requested, a new entry lands here first;
> provisioning happens from the entry.

---

## Template (use this for every new entry)

```
### <company_name>

- **What.** One-line description.
- **Asset class.** crypto / cfd / spot / arb / other.
- **Primary exchange(s).**
- **Why this company.** What edge, what problem, what purpose.
- **Capital plan.** Opening size · growth rule · drawdown limit.
- **Risk profile.** max % per trade · max leverage · daily loss stop.
- **Strategy style.** e.g. SMC on 15m crypto, CFD London open-range, long-hold.
- **LLM budget.** Tokens/day across agents · preferred models.
- **Risk Agent enabled?** off / on (see locked decision #2; default off).
- **Fake-close candle?** no / yes (see locked decision #4; opt-in per strategy).
- **Approval mode.** human_all / rule_based / autonomous.
- **How to implement (when Phase 5 lands).**
  1. `tickles create-company --name <name> --asset-class <class>`
  2. `tickles bind-account --company <name> --exchange <ex> --account-alias <alias>`
  3. `tickles grant --agent <agent> --capability <cap>` × N
  4. Seed strategies from MemU `approved/` matching the company's asset class.
  5. Activate forward-test on each strategy.
  6. Set approval_mode per strategy.
- **Notes / open questions.**
```

---

## Priority order (as discussed with Dean)

1. **JarvAIs** — in flight, the platform's pilot tenant.
2. **Capital CFD Co** — CFDs on Capital.com, once Phase 2 + Phase 10 ship.
3. **Sandbox / Explorer Co** — cheap-model experimentation, always-on
   discovery.
4. **Spot-Hold Co** — long-term spot positions on IBKR.
5. **Alpaca Day-Trading Co** — US equities, PDT-aware.
6. **Arb Crypto Co** — first arbitrage tenant (Phase 11 target).

The rest are backlog.

---

## 1. JarvAIs (pilot, live/in-progress)

- **What.** Crypto perpetual futures trading using SMC (Smart Money Concepts)
  and indicator confluence.
- **Asset class.** crypto (perp futures).
- **Primary exchange(s).** Bybit (preferred), BloFin, Bitget.
- **Why this company.** The reference tenant; proves every piece of the
  platform under real-risk conditions. If it makes money, we scale; if it
  doesn't, we learn.
- **Capital plan.** Start: $1k demo → $1k live. Compound; drawdown > 20%
  → pause and root-cause.
- **Risk profile.** 1% risk per trade · max leverage 10× · daily loss stop 3%.
- **Strategy style.** SMC structure + FVG + liquidity sweeps, confluence of
  2–3 indicators, 15m and 1h timeframes.
- **LLM budget.** ~100k tokens/day CEO + agents, Sonnet 4.5 for planning,
  Haiku for housekeeping.
- **Risk Agent enabled?** off for now (can flip on any time).
- **Fake-close candle?** no — crypto is always-on.
- **Approval mode.** human_all until Phase 6 Validator proves Rule 1.
- **How to implement.** Already provisioned; finalise post-Phase-5 by
  re-running `tickles create-company` idempotently.

---

## 2. Capital CFD Co

- **What.** CFD trading on forex, indices, gold via Capital.com.
- **Asset class.** cfd.
- **Primary exchange(s).** Capital.com (optionally Pepperstone later).
- **Why this company.** Capital.com gives us spreads + leverage + session
  close. This is the tenant that actually needs the fake-close candle, and
  it's the battleground where JarvAIs V1 / Capital 2.0's lessons most
  directly apply.
- **Capital plan.** Start: demo → $2k live. Hard drawdown stop 15%.
- **Risk profile.** 0.5% risk per trade · max leverage 30× · max 3 trades
  open at once · daily loss stop 2%.
- **Strategy style.** London open range break, NY open range break, liquidity
  sweep on 5m/15m — these fire at session close, so fake candle = yes.
- **LLM budget.** Light — strategies are deterministic.
- **Risk Agent enabled?** off initially.
- **Fake-close candle?** yes (for the strategies that fire at session close).
- **Approval mode.** human_all for first 4 weeks, then rule_based if
  Validator green.
- **How to implement.** Phase 2 must ship (treasury + OMS) and Phase 4 must
  ship (sessions service with `capital_com_close`). Then:
  1. `tickles create-company --name capital_cfd --asset-class cfd --primary-exchange capitalcom`
  2. Bind demo account first; live account only after Rule 1 hits 99.9%.

---

## 3. Sandbox / Explorer Co

- **What.** Experimental always-on discovery tenant. Runs 10k random
  strategies per day in paper/backtest; any Sharpe > 2 result gets reviewed.
- **Asset class.** synthetic (paper).
- **Primary exchange(s).** none — backtest-only.
- **Why this company.** Curiosity agent's home; generates ideas for the
  other tenants without putting capital at risk.
- **Capital plan.** $0 live.
- **Risk profile.** n/a.
- **Strategy style.** random combinations of the 250 indicators, genetic
  optimisation, walk-forward validation.
- **LLM budget.** High quantity, low cost — Haiku / cheap-tier models.
- **Risk Agent enabled?** off.
- **Fake-close candle?** no.
- **Approval mode.** autonomous (no capital).
- **How to implement.** Phase 5 (provisioning) + Phase 9 (Curiosity agent).
  Steps:
  1. `tickles create-company --name explorer --asset-class sandbox`
  2. Grant Curiosity write:backtest_runs + write:memu:candidates.
  3. Do NOT grant any trade:execute capability.

---

## 4. Spot-Hold Co

- **What.** Long-term spot stock holdings (buy and hold, occasional rebalance).
- **Asset class.** spot.
- **Primary exchange(s).** Interactive Brokers.
- **Why this company.** Dean wants to build a core portfolio alongside
  trading. Different time horizon, different risk, different tax profile.
- **Capital plan.** DCA schedule; no drawdown stop (long-term).
- **Risk profile.** no leverage · position sizing by conviction score.
- **Strategy style.** Fundamental + macro signals; quarterly rebalance.
- **LLM budget.** Low — decisions are infrequent.
- **Risk Agent enabled?** on (Dean wants a check before any buy/sell).
- **Fake-close candle?** no.
- **Approval mode.** human_all always (this is Dean's savings).
- **How to implement.** Phase 2 + Phase 10 (IBKR adapter). Then:
  1. `tickles create-company --name spot_hold --asset-class spot --primary-exchange ibkr`
  2. Bind live IBKR account (human confirmation required).
  3. Grant only `read:*` and `propose:trade_intent` to any agent. The
     `execute:trade` capability is reserved to Dean-with-CEO-confirmation.

---

## 5. Alpaca Day-Trading Co

- **What.** US equities day trading via Alpaca, PDT-rule aware.
- **Asset class.** equity (day trade).
- **Primary exchange(s).** Alpaca.
- **Why this company.** Alpaca is cheap, has paper accounts, good API, and
  when (if) the PDT rule is scrapped we're ready. Until then the adapter's
  PDT check blocks the 4th day-trade inside 5 days on sub-$25k accounts.
- **Capital plan.** Start $5k paper; $10k live pending PDT rule outcome.
- **Risk profile.** 1% risk per trade · no overnight holds (day-trade only).
- **Strategy style.** Opening range break, VWAP mean reversion, gap fill.
- **LLM budget.** Light.
- **Risk Agent enabled?** off initially.
- **Fake-close candle?** yes (for ORB strategies).
- **Approval mode.** human_all until Validator green.
- **How to implement.** Phase 2 + Phase 10 (Alpaca adapter with PDT rule
  baked in). Then standard provisioning.

---

## 6. Arb Crypto Co (Phase 11 target)

- **What.** Cross-exchange arbitrage on crypto spot (first) + perp (later).
- **Asset class.** arb.
- **Primary exchange(s).** Pair two of: Binance, Bybit, Bitget, BloFin.
- **Why this company.** The "latency-class=fast" tenant; validates the
  Market Data Gateway's fan-out under the tightest constraints.
- **Capital plan.** Start $2k demo per leg ($4k total) → scale on proven edge.
- **Risk profile.** 0.2% max per cycle · cap position value to 5% of
  target book · flat by end of day.
- **Strategy style.** Triangular or cross-exchange price divergence, funding
  rate arb for perps later.
- **LLM budget.** Minimal — arb is mechanical.
- **Risk Agent enabled?** off.
- **Fake-close candle?** no.
- **Approval mode.** rule_based once Validator has confirmed Rule 1 on
  simple crypto first.
- **How to implement.** Phase 3 (gateway) + Phase 2 (OMS) + Phase 11
  (arb strategy). `tickles create-company --name arb_btc_crypto
  --asset-class arb --latency-class fast`. Bind two exchange accounts.
  Grant `execute:trade` on both with tight caps.

---

## 7+ Backlog (capture now, shape later)

### gold_arb

- **What.** Arbitrage gold across CFD providers (Capital.com vs OANDA vs
  Pepperstone).
- **Why.** Different providers quote gold differently around session
  crossovers.
- **Status.** Backlog — waits on Phase 11 proving cross-exchange arb at all.

### ms_macro

- **What.** Macro-driven long-short equity / ETF book.
- **Why.** Separate vehicle from day-trading; different signal universe.
- **Status.** Backlog.

### crypto_yield

- **What.** DeFi yield + basis trade (spot long + perp short when funding
  positive).
- **Why.** Market-neutral return stream.
- **Status.** Backlog — needs custody story first.

### news_reaction

- **What.** Event-driven trading on earnings / FOMC / CPI releases.
- **Why.** Proves the Risk Agent (locked decision #2) is useful — classic
  use case for LLM qualitative gating.
- **Status.** Backlog — wait until Risk Agent is validated elsewhere.

---

## Cross-company principles (apply to every entry above)

1. Own wallet(s) or account(s) — never share credentials across companies.
2. Own Postgres DB — hot data is isolated.
3. Own MemU container tag — cross-company reads are allowlisted, not default.
4. Capabilities are explicit and minimal. `execute:trade` is the highest bar.
5. Every company starts with `approval_mode = human_all` and earns autonomy
   by demonstrating Rule 1 adherence.
6. Every company is auditable end-to-end: intent → sizing → execution →
   validation → MemU insight.

---

## How agents should use this file

- When Dean says "spin up a new company", add an entry here first with
  Dean's answers filled into the template.
- Don't write any code or schema changes until the entry exists and is
  approved.
- When Phase 5 ships, reference this file during provisioning; everything
  needed to create a tenant lives in a single entry.

---

*End of CompanyIdeas.md. Append new entries at the bottom; never rewrite an
existing entry without recording a change note.*
