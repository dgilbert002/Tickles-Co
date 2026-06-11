# `copy_ch_ai_vision` — Autonomous Open-Trade Management Proposals

> **What this document is.** A read-only investigation of how the
> `copy_ch_ai_vision` paper-trading agent works **today**, an inventory of the
> tools and machinery it could reuse, and **three concrete proposals** for letting
> it *manage* its open trades on its own (take partial profit, trail a stop, exit
> early when conditions change) — **without** the owner hand-coding profit/loss
> rules. The agent should *learn* when to act.
>
> Written for a non-developer. Every claim is backed by a real file path and line
> number so a developer can verify it. **No code was changed to produce this doc.**

---

## 1. How it works today (the facts)

### 1.1 What `copy_ch_ai_vision` actually is

There are two different "ChartHacker" things, and it's easy to confuse them:

1. **The ChartHacker *brain*** — an AI vision agent (`actor_id = jarvais_chart_hacker`)
   that looks at chart images and *decides to open a trade*. When it decides, a row
   is written into the `tracked_positions` table (entry price, direction, stop-loss,
   take-profit). This is the part that "decides for itself."
2. **The `copy_ch_ai_vision` *paper wallet*** — one of **12 competing paper agents**
   run by the daemon [`shared/intelligence/copy_trade_monitor.py`](shared/intelligence/copy_trade_monitor.py:1).
   This is the $1,000-starting-balance contestant that currently holds the big
   SOL/USDT short. It does **not** look at charts itself — it *mirrors* whatever the
   ChartHacker brain opens.

In the competition daemon, the agent is defined here:

```65:65:shared/intelligence/copy_trade_monitor.py
    ("CH: AI Vision",    1.0, 1.0, "spot_seq_ch"),
```

and mapped to its database id here:

```85:85:shared/intelligence/copy_trade_monitor.py
    "CH: AI Vision":    "copy_ch_ai_vision",
```

Its trading style is `spot_seq_ch` = **full-wallet, spot, one trade at a time, 1×
leverage** (confirmed in the agent table of [`shared/docs/FIX_EVERYTHING_2026_05_29.md`](shared/docs/FIX_EVERYTHING_2026_05_29.md:106)).

### 1.2 How it opens a position

Every 30 seconds (`POLL_INTERVAL_S`, [line 39](shared/intelligence/copy_trade_monitor.py:39)) the daemon runs one `tick()`:

1. It reads brand-new open rows from `tracked_positions` written by the trader
   brains, including the ChartHacker brain
   ([`_get_new_open_positions`](shared/intelligence/copy_trade_monitor.py:385), the
   `actor_id = 'jarvais_chart_hacker'` filter is on [line 428](shared/intelligence/copy_trade_monitor.py:428)).
2. It routes each new position to the right agent. The `CH: AI Vision` agent only
   takes ChartHacker-brain positions:

```946:948:shared/intelligence/copy_trade_monitor.py
                    # CH agent only takes chart_hacker positions
                    if is_ch_agent and not is_ch_pos:
                        continue
```

3. It sizes the trade. For `spot_seq` (which includes `spot_seq_ch`), it bets the
   **whole balance** and refuses a second trade while one is open:

```496:501:shared/intelligence/copy_trade_monitor.py
        if "spot_seq" in mode:
            # Full balance, sequential
            if agent["open_positions"]:
                return  # Already in a trade
            allocated = agent["balance"]
            leverage = 1.0
```

4. It records the open paper position (entry, SL, TP copied straight from the
   ChartHacker brain's `tracked_positions` row) and writes a row into
   `competition_trades` so the dashboard can see it
   ([`_enter_agent_position`](shared/intelligence/copy_trade_monitor.py:478),
   [`_log_trade_open`](shared/intelligence/copy_trade_monitor.py:678)).

### 1.3 What happens **after** entry — the gap

**Nothing intelligent.** After a position is open, the only thing that ever touches
it is the mechanical SL/TP candle check in
[`_check_agent_positions`](shared/intelligence/copy_trade_monitor.py:579). On each
tick it scans 1-minute candles and:

- closes at the **stop-loss** if a candle wicks through it,
- closes at the **take-profit** if a candle wicks through it,
- for the `+BE Lock` agents only, moves the stop to break-even after +5%
  ([lines 618-632](shared/intelligence/copy_trade_monitor.py:618)). **`copy_ch_ai_vision`
  is `spot_seq_ch`, so it does not even get the break-even move** — it has *no*
  post-entry rule of any kind.

The SL/TP values themselves are just re-copied from the underlying ChartHacker-brain
`tracked_positions` row every tick
([`_sync_sl_tp`](shared/intelligence/copy_trade_monitor.py:564)). The agent never
decides to move them, never takes partial profit, never exits early.

There **is** a separate AI critic, the
[`ChartHackerOpinionService`](shared/intelligence/chart_hacker_opinion_service.py:1),
which *does* wake an LLM on events (new position, ≥1% move, SL/TP changed, hourly —
see [`_should_fire`](shared/intelligence/chart_hacker_opinion_service.py:186)) and
writes an opinion (`would_take_trade`, `suggested_sl`, `suggested_tp`) plus a
mem0 note. **But by design it is advisory only** and is wired to *human* traders,
not to the copy agents:

```4:6:shared/intelligence/chart_hacker_opinion_service.py
Purpose: Phase 8 ChartHackerOpinionService daemon — critic role watching open
         tracked_positions, calling vision LLM, writing agent_opinions.
         Never opens/closes positions. Never writes tracked_positions.
```

> **Bottom line:** `copy_ch_ai_vision` is "fire-and-forget." Its open trade is steered
> only by the stop-loss / take-profit it inherited at entry. There is no logic — and
> no existing path — for it to actively manage the trade in flight. The pieces to
> *observe* a position and to *think about* it exist (opinion critic, guru reports,
> position monitor) but none of them are allowed to *act* on this agent's trade.

---

## 2. Tools the agent could use to manage an open trade

All tools live behind the MCP server
[`shared/mcp/bin/tickles_mcpd.py`](shared/mcp/bin/tickles_mcpd.py:1) (HTTP on
`:7777`) and the matching stdio entry for OpenClaw Desktop. There are ~90 tools
across many groups; below are the ones that actually matter for *managing a live
trade*. (The "100 tools" the owner remembers is roughly right — but most are
provisioning/backtest/symbols housekeeping, not trade-management.)

### 2.1 Reading price & the position

| Tool | File | What it does |
|------|------|--------------|
| `market_ticker` | [`trading.py:1314`](shared/mcp/tools/trading.py:1314) | Live bid/ask/last for a symbol from the exchange (via CCXT). |
| `md.quote` / `md.candles` | [`data.py:529`](shared/mcp/tools/data.py:529) | Latest quote / recent candles from the local DB. |
| `market_funding` | [`trading.py:1354`](shared/mcp/tools/trading.py:1354) | Funding / overnight holding cost for a perp. |
| `intelligence.positions.open` | [`intelligence.py:860`](shared/mcp/tools/intelligence.py:860) | Open tracked positions with live metrics (distance-to-SL/TP, P&L). |
| `intelligence.positions.history` | [`intelligence.py:885`](shared/mcp/tools/intelligence.py:885) | Closed-position history + summary stats. |
| `agent.open_positions` | [`agent_self.py:187`](shared/mcp/tools/agent_self.py:187) | **The agent's own** open paper positions (entry/SL/TP/allocation) from `competition_trades`. |
| `agent.performance` | [`agent_self.py:175`](shared/mcp/tools/agent_self.py:175) | The agent's own win-rate, P&L, return %, equity. |
| `agent.trade_history` | [`agent_self.py:199`](shared/mcp/tools/agent_self.py:199) | The agent's own closed trades with P&L + exit reason. |
| `agent.wallet` | [`agent_self.py:163`](shared/mcp/tools/agent_self.py:163) | The agent's balance / equity / free margin. |

### 2.2 Acting on a trade (what exists, and the gap)

| Tool | File | What it does | Useful here? |
|------|------|--------------|--------------|
| `execution.submit` | [`trading.py:1023`](shared/mcp/tools/trading.py:1023) | Place an order on the **paper** or **demo** execution adapter; supports `stopLoss`, `takeProfit`, `leverage`. | Partially — it writes to the `orders` table / paper-execution engine, **not** to the `competition_trades` rows the copy daemon owns. |
| `execution.cancel` / `execution.status` | [`trading.py:1059`](shared/mcp/tools/trading.py:1061) | Cancel / check an order. | Same caveat as above. |
| `positions.close_wicked` | [`data.py:812`](shared/mcp/tools/data.py:813) | Force-close any `tracked_positions` whose SL/TP already wicked. | Safety-net only, not discretionary management. |
| `positions.recheck` | [`data.py:853`](shared/mcp/tools/data.py:854) | Run one pending-activation cycle now. | Lifecycle plumbing, not management. |
| `positions.cancel` / `positions.reconcile` / `positions.diagnose` | [`routing.py:874`](shared/mcp/tools/routing.py:874) | Cancel / reconcile / diagnose routed positions. | Plumbing. |
| `treasury.evaluate` | [`trading.py:975`](shared/mcp/tools/trading.py:975) | Pre-trade risk/capital check. | Useful before a *re-entry*, not for managing the current trade. |

> **Important gap to flag up-front:** there is currently **no MCP tool that says
> "move the stop-loss on my open copy-agent position to X" or "close half of my
> `copy_ch_ai_vision` position now"**. The copy agent's trade lives in the
> daemon's in-memory state + the `competition_trades` table, and the only writer is
> the daemon itself. *Every* proposal below therefore needs **one small new
> "manage my copy position" capability** (either a new MCP tool, or a control-row
> the daemon reads). This is the single most important new piece of plumbing.

### 2.3 Memory & learning tools (so it can record *why*)

| Tool | File | What it does |
|------|------|--------------|
| `memory.add` | [`memory.py:229`](shared/mcp/tools/memory.py:229) | Write a memory to mem0 (agent-private or company-shared). |
| `memory.search` | [`memory.py:296`](shared/mcp/tools/memory.py:296) | Semantic search of the agent's own memories. |
| `learnings.read_last_3` | [`memory.py:512`](shared/mcp/tools/memory.py:512) | Read the agent's last 3 lessons before deciding (the standard "look before you leap" call). |
| `memu.broadcast` / `memu.search` | [`memory.py:368`](shared/mcp/tools/memory.py:369) | Share / read **cross-company** lessons via MemU (Tier-3). |
| `autopsy.run` | [`learning.py:73`](shared/mcp/tools/learning.py:73) | Render the per-trade reflection prompt; output is meant to be saved via `memory.add`. |
| `postmortem.run` | [`learning.py:118`](shared/mcp/tools/learning.py:118) | Per-session review prompt. |
| `feedback.loop` | [`learning.py:160`](shared/mcp/tools/learning.py:160) | Cycle-level "have I been drifting?" review prompt. |

---

## 3. Existing cron / heartbeat / memory machinery it can reuse

The system already has a clean "wake an agent, do work, go back to sleep, and prove
you're alive" pattern. Proposals should build on it rather than invent a new one.

### 3.1 Heartbeats + a watchdog (already built)

- [`shared/intelligence/heartbeat.py`](shared/intelligence/heartbeat.py:16) —
  `record_heartbeat(agent_id, status, expected_interval_seconds, message)` upserts a
  row into the `cron_heartbeats` table. Every existing daemon calls it (see the copy
  daemon at [line 974](shared/intelligence/copy_trade_monitor.py:974), the position
  monitor at [line 2221](shared/intelligence/position_monitor.py:2221)).
- [`shared/intelligence/cron_canary.py`](shared/intelligence/cron_canary.py:22) —
  `CronCanary` reads `cron_heartbeats` every 60s and sends a Telegram alert if an
  agent goes quiet for >2.5× its expected interval. **So if we add a new wake-up job,
  it gets free "is it dead?" monitoring just by calling `record_heartbeat`.**

### 3.2 The "cron wakes an LLM agent" pattern (already used for the ChartHacker brain)

The ChartHacker vision brain is deployed as an **OpenClaw-native agent woken by
cron**, in [`shared/templates/chart_hacker/spawn_chart_hacker.sh`](shared/templates/chart_hacker/spawn_chart_hacker.sh:87):

```87:97:shared/templates/chart_hacker/spawn_chart_hacker.sh
echo "[6/6] Registering 5-minute cron job"
openclaw cron add \
  --agent "${AGENT_ID}" \
  --name "${AGENT_ID}_cycle" \
  --description 'ChartHacker 5-min chart analysis cycle' \
  --cron '*/5 * * * *' --tz UTC \
  --session isolated \
  --tools read,write,exec \
  --thinking low --timeout-seconds 180 \
```

This is exactly the shape the owner wants: a scheduled wake-up that runs an LLM
agent with tool access, then exits. A management agent can be deployed the same way.

### 3.3 The "non-LLM job prepares a report, then maybe wakes the LLM" pattern (already used twice)

- [`ChartHackerGuru`](shared/intelligence/chart_hacker_guru.py:584) — a **pure-quant,
  no-LLM** daemon ([line 19](shared/intelligence/chart_hacker_guru.py:19)) that builds
  trader statistics + an **hourly status report** that explicitly flags positions
  "very close to SL"
  ([`_run_hourly_status_check`](shared/intelligence/chart_hacker_guru.py:659), the
  near-SL detection is at [lines 776-784](shared/intelligence/chart_hacker_guru.py:776))
  and writes lessons to mem0.
- [`ChartHackerOpinionService._should_fire`](shared/intelligence/chart_hacker_opinion_service.py:186)
  — the canonical **event-trigger gate**: only call the LLM when something
  *changed* (new position, ≥1% price move, SL/TP edited, or 1 hour elapsed). This is
  the exact "don't burn LLM calls on nothing" logic the owner wants, ready to copy.

### 3.4 Memory for "lessons learned" (already wired)

- [`shared/utils/mem0_config.py`](shared/utils/mem0_config.py:224) —
  `get_memory(company, agent)` for per-agent private memory and
  `get_dev_memory(agent)` for the build namespace. **Always use these, never raw
  `Memory()`** (workspace rule).
- The opinion critic already writes per-position notes to the `chart_hacker` mem0
  namespace ([`_push_opinion_to_mem0`](shared/intelligence/chart_hacker_opinion_service.py:42)),
  and the guru writes lessons to dev mem0
  ([`_store_learning_in_mem0`](shared/intelligence/chart_hacker_guru.py:559)).
- The `position_updates` table is a ready-made time-series of every position's
  price / P&L / distance-to-SL/TP / MAE / MFE, written every cycle by
  [`position_monitor.write_position_update`](shared/intelligence/position_monitor.py:1229).
  A management agent can read this instead of recomputing anything.

---

## 4. Three proposals

All three obey the owner's hard rules:

- **No human-set profit/loss thresholds.** Where a number is needed (e.g. "is this a
  big enough move to wake up?"), it is either (a) a *cheap, mechanical* wake-up gate
  that does **not** decide the trade, or (b) a value the **agent learns from its own
  memory**, not a rule the owner types in.
- **Favour cron + MCP; wake the LLM only on events; let the agent set its own next
  wake time.**
- **Every action is logged with reasoning to mem0/MemU.**

A shared prerequisite for all three (the gap from §2.2):

> **P0 — "Manage my copy position" capability (small, shared by all proposals).**
> Add a way for an agent to *act* on its own `copy_ch_ai_vision` paper position.
> Concretely: a new control table `copy_agent_actions` (rows like
> `{agent_id, tracked_position_id, action: 'move_sl'|'partial_close'|'close_now'|'set_next_wake', value, reason, status}`)
> that [`copy_trade_monitor.tick()`](shared/intelligence/copy_trade_monitor.py:924)
> drains at the **start** of each 30s tick — applying `move_sl` to the in-memory
> `pos["sl"]`, doing a `partial_close`/`close_now` via the existing
> [`_close_agent_position`](shared/intelligence/copy_trade_monitor.py:637) path, and
> persisting via the existing [`_save_agent`](shared/intelligence/copy_trade_monitor.py:216).
> Optionally expose the same thing as MCP tools (`copyagent.move_sl`,
> `copyagent.partial_close`, `copyagent.close`) so the LLM can call them directly.
> This keeps the copy daemon the *single writer* of its own state (no race), while
> giving the agent a safe lever to pull. Effort: ~1–1.5 days.

---

### Proposal A — "Quiet Watcher" (cron + non-LLM report, LLM woken only on events) ★ owner's preference

**Core idea.** A cheap, scheduled, **non-LLM** "watcher" job does the looking. It
only wakes the expensive LLM when something genuinely changed about the open
`copy_ch_ai_vision` trade. The LLM then reviews its history + the watcher's report,
decides one action via the P0 tools, writes its reasoning to mem0/MemU, and **tells
the watcher when to wake it next** (adaptive heartbeat).

**Architecture / data flow.**

1. **Watcher (non-LLM, systemd timer or cron, ~60s):** a new small daemon, e.g.
   `shared/intelligence/copy_ch_manager_watcher.py`. Each run it:
   - reads the agent's open position (`agent.open_positions` / the in-memory state),
   - reads the latest `position_updates` row (price, P&L, distance-to-SL/TP, MAE/MFE
     — all already computed by the position monitor),
   - pulls live price via `market_ticker` and funding via `market_funding`,
   - writes a compact **report row** to a new `copy_ch_manager_reports` table,
   - calls `record_heartbeat("copy-ch-manager-watcher", ...)` for free watchdog cover.
2. **Event gate (non-LLM, copied from
   [`_should_fire`](shared/intelligence/chart_hacker_opinion_service.py:186)):** the
   watcher decides whether to **wake the LLM**. Wake triggers are *mechanical and
   cheap*, not P/L rules: e.g. price moved ≥1 "bucket" since last look, distance to
   SL/TP shrank past the last-observed band, funding flipped sign, a new opinion or
   guru warning landed, or the agent's own self-set `next_wake_at` arrived. If
   nothing changed, it does nothing — **zero LLM cost.**
3. **LLM decision (only when woken):** the watcher triggers the agent via
   `openclaw cron run` / a one-shot OpenClaw turn (same mechanism as
   [`spawn_chart_hacker.sh`](shared/templates/chart_hacker/spawn_chart_hacker.sh:87)).
   The agent prompt instructs it to: call `learnings.read_last_3` →
   `agent.trade_history` → `agent.open_positions` → read the latest watcher report →
   decide **one** of {hold, move_sl, partial_close, close_now} → execute via the P0
   tool → write reasoning via `memory.add` (and `memu.broadcast` if it's a
   generalisable lesson) → **set its own `next_wake_at`** in the report/control table.
4. **Self-scheduling:** the agent writes `next_wake_at` (e.g. "nothing to do for 2h,
   wake me at 14:00 or sooner if price moves 2%"). The watcher honours it as one of
   its wake triggers. That's the adaptive heartbeat.

**New code / files / tables.**
- New daemon `copy_ch_manager_watcher.py` (non-LLM).
- New tables: `copy_ch_manager_reports`, `copy_agent_actions` (P0).
- New OpenClaw agent `jarvais_copy_ch_manager` + SOUL prompt (modelled on the
  ChartHacker SOUL template).
- The P0 capability.
- One systemd timer/unit (mirrors the existing daemon units).

**How it logs reasoning.** Every LLM wake writes a mem0 note via `memory.add`
(scope=agent, `jarvais/copy_ch_manager`) and, for portable lessons,
`memu.broadcast`. The non-LLM watcher writes structured report rows so there's a
full audit even on ticks where the LLM never ran.

**Pros.** Cheapest by far (LLM only fires on real events); reuses the exact patterns
already proven by the guru + opinion services; self-scheduling matches the owner's
vision precisely; non-LLM watcher means the system keeps "watching" continuously
even if the LLM budget is exhausted.
**Cons.** Most moving parts (watcher + agent + control table + report table); the
"what counts as an event" gate needs care so it neither spams nor sleeps through a
fast move; two-process design is a bit more to operate.
**Risk:** **Low–Medium** (additive, single-writer daemon stays the only mutator).
**Effort:** ~3–4 days (incl. P0).

---

### Proposal B — "Smart Heartbeat Agent" (one self-scheduling LLM cron job)

**Core idea.** Skip the separate watcher process. Run a **single** LLM agent on a
cron, but make it *cheap by being lazy*: most of the time it just looks at numbers
and goes straight back to sleep, only "thinking hard" when its own learned criteria
say it's worth it. It manages its own wake schedule.

**Architecture / data flow.**

1. **One OpenClaw cron agent** `jarvais_copy_ch_manager`, registered exactly like the
   ChartHacker brain ([`spawn_chart_hacker.sh:87`](shared/templates/chart_hacker/spawn_chart_hacker.sh:87)),
   but with a **dynamic schedule**: it re-arms its own next run each time (via
   `openclaw cron` update, or a `next_wake_at` it re-reads).
2. **Two-stage thinking inside one wake (to save tokens):**
   - **Stage 1 (cheap / "low thinking"):** call `agent.open_positions`,
     `market_ticker`, and the latest `position_updates` row. Compare against the
     `last_seen` snapshot it stored in mem0. If essentially nothing changed → write a
     one-line "no-op" memory, set a longer `next_wake_at`, exit. This is the
     `--thinking low` path the template already uses.
   - **Stage 2 (full reasoning, only if Stage 1 says "something changed"):** call
     `learnings.read_last_3` + `agent.trade_history`, decide one action, execute via
     the P0 tool, log reasoning, set a shorter `next_wake_at`.
3. **Self-scheduling:** the agent decides its own cadence ("trade is calm and far
   from SL → check again in 3h; trade is +8% and accelerating → check in 10m") and
   writes it to its schedule. Heartbeats via `record_heartbeat` give it watchdog
   cover.

**New code / files / tables.**
- New OpenClaw agent + SOUL prompt (the SOUL *is* the management policy in plain
  English; no thresholds hard-coded in Python).
- The P0 capability.
- A tiny `copy_ch_manager_state` table (or a mem0 record) for `last_seen` +
  `next_wake_at`.
- No separate watcher daemon.

**How it logs reasoning.** Same as A — `memory.add` per wake (including no-ops so the
schedule logic is auditable), `memu.broadcast` for shareable lessons.

**Pros.** Simplest to deploy (one agent, reuses the existing spawn pattern verbatim);
fewest new tables; the management policy lives in editable English (SOUL.md), so the
owner can shape behaviour without code; genuinely self-scheduling.
**Cons.** Even a "cheap" Stage-1 wake still spins up an LLM turn, so it costs more
than A's pure-Python watcher; if the agent mis-sets a long `next_wake_at`, it could
sleep through a fast reversal (mitigated by a hard "minimum check cadence" the
watchdog enforces); token cost scales with how chatty Stage 1 is.
**Risk:** **Medium** (LLM is in the loop on every wake; relies on the agent's own
schedule discipline).
**Effort:** ~2–3 days (incl. P0).

---

### Proposal C — "Promote the Critic to Co-Pilot" (reuse the existing opinion service as the trigger)

**Core idea.** Don't build a new watcher at all — the
[`ChartHackerOpinionService`](shared/intelligence/chart_hacker_opinion_service.py:1)
*already* wakes an LLM on the right events and already produces `suggested_sl` /
`suggested_tp` / `would_take_trade` + confidence for open positions. Today that
output is **advisory and ignored**. Proposal C turns it into the *trigger* for a
thin, separate **execution step** that acts on the `copy_ch_ai_vision` trade.

**Architecture / data flow.**

1. **Extend the opinion critic's reach** so it also evaluates the ChartHacker-brain
   position that `copy_ch_ai_vision` is mirroring (it already runs on
   `tracked_positions` with the exact event gate
   [`_should_fire`](shared/intelligence/chart_hacker_opinion_service.py:186): new /
   ≥1% move / SL-TP edited / hourly). No new scheduler needed — this is already a
   cron-style, event-gated LLM.
2. **A thin, non-LLM "executor"** (a few lines added to the copy daemon's tick, or a
   tiny sidecar) reads the **latest published `agent_opinions` row** for the mirrored
   position. When the critic's confidence clears a bar that the **agent itself
   learned** (see step 3) and the opinion implies an action (e.g. a tighter
   `suggested_sl`, or `would_take_trade=false` ⇒ "I'd exit now"), the executor
   applies it through the P0 path. The executor makes **no judgement of its own** — it
   only carries out the critic's already-reasoned opinion.
3. **Learned thresholds, not human ones.** The "how confident must the critic be
   before I act" bar is **not** an owner-set constant. A small periodic job (reuse the
   guru / edge-scorer pattern) back-tests past opinions vs. realised outcomes and
   writes the agent's *current* acted-on-confidence to mem0; the executor reads it
   from there. So the agent tunes its own trust in the critic over time.

**New code / files / tables.**
- Allow the opinion critic to cover the copy agent's mirrored position (small filter
  change + reuse).
- New thin executor (in the copy daemon tick or a sidecar) + the P0 capability.
- A small "learned trust" record in mem0 + a periodic calibration job (can piggyback
  on the existing edge-scorer / guru machinery).

**How it logs reasoning.** The critic *already* writes its reasoning to
`agent_opinions` and mem0
([`_push_opinion_to_mem0`](shared/intelligence/chart_hacker_opinion_service.py:42));
the executor appends an action note ("acted on opinion #N because learned-trust ≥ X")
via `memory.add`, and `memu.broadcast` for cross-company calibration lessons.

**Pros.** Reuses the *most* existing machinery (the event-gated LLM critic already
exists and runs); least net-new LLM cost (one critic call already happening); clean
separation — the thinking part is the critic, the acting part is mechanical;
naturally produces a paper-trail because opinions are already persisted.
**Cons.** The critic was built for human traders and "never acts" by design
([lines 4-6](shared/intelligence/chart_hacker_opinion_service.py:4)) — repurposing it
blurs that boundary and could affect how its opinions are used elsewhere; the action
vocabulary is limited to what the opinion expresses (SL/TP/exit), so "take 30%
partial profit" needs the opinion schema extended; weakest on the "agent reviews its
*own* trade history before each decision" goal unless that's added to the executor.
**Risk:** **Medium** (changes the semantics of a shared service; needs care so other
consumers of `agent_opinions` aren't surprised).
**Effort:** ~2.5–3.5 days (incl. P0 + opinion-schema/coverage changes).

---

## 5. Recommendation

| | A — Quiet Watcher | B — Smart Heartbeat Agent | C — Critic Co-Pilot |
|---|---|---|---|
| Matches owner's "cron+MCP, LLM only on events, self-scheduling" | **Best** | Good | Good |
| LLM cost | **Lowest** (pure-Python watcher gates everything) | Medium | Low (reuses existing call) |
| Reuses existing machinery | High (guru + opinion patterns) | Medium (spawn pattern) | **Highest** (the critic already exists) |
| Agent reviews its own history before deciding | Yes | Yes | Needs adding |
| "Learns its own thresholds, no human rules" | Yes | Yes | Yes (learned-trust) |
| New moving parts | Most | Fewest | Medium |
| Risk | Low–Medium | Medium | Medium |
| Effort | ~3–4 d | ~2–3 d | ~2.5–3.5 d |

**Recommendation: build Proposal A ("Quiet Watcher"), and steal the best bits of the
other two.** A is the truest fit for the owner's stated vision: a cheap, always-on
**non-LLM** job does the watching and only **wakes the LLM on real events**, the LLM
reviews its memory + history + a prepared report, acts through MCP tools, logs *why*,
and **sets its own next wake time**. To de-risk and speed it up:

- Borrow **B's two-stage "cheap-look, then think-hard"** prompt structure so even the
  woken LLM stays frugal, and B's "policy lives in an editable SOUL.md" idea so the
  owner can shape behaviour without code.
- Borrow **C's event gate verbatim** — it already exists in
  [`_should_fire`](shared/intelligence/chart_hacker_opinion_service.py:186) — and feed
  the critic's existing `agent_opinions` into the watcher's report as one more signal,
  so the LLM gets a second opinion for free.

Whichever path is chosen, **P0 (the "manage my copy position" capability) must land
first** — it is the one genuinely missing piece, and all three proposals depend on
it. Start there, keeping the copy daemon as the single writer of its own state to
avoid races.
