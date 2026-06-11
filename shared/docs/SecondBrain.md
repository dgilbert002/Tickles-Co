# SecondBrain — design sketch (NOT built yet)

> Status: **proposal / thinking document.** Nothing here is implemented.
> Written 2026-05-29 so we have a clear picture of what "a real second AI brain"
> would actually mean before deciding whether to build it.

---

## 1. The one-sentence idea

A **second, independent AI trader** that looks at the same market + news + chart
feed as `chart_hacker`, but **forms its own opinion and makes its own decisions**
— so we can run two different AI minds side-by-side and see which one is
actually better, instead of one brain plus copies of it.

---

## 2. Why this is different from what we have today

Today there are really only **two kinds of thing**:

| Thing | What it is | Does it decide? | Does it learn? |
|-------|-----------|-----------------|----------------|
| `chart_hacker` | The one real AI agent. Reads charts + context, writes signals into `tracked_positions`. | **Yes** | Yes (recall + postmortems) |
| `copy_charthacker` (and `copy_spot_seq`, `copy_rose_*`, etc.) | Paper wallets that **mechanically mirror** a source's signals at different sizing/leverage rules. | **No** — they just copy | No |

So right now, every "competitor" on the leaderboard is either **chart_hacker
itself** or a **copy of someone's signals with different money math**. There is
only **one opinion** in the whole system. The copies make that one opinion look
like a competition, but they all rise and fall together.

**SecondBrain breaks that.** It is a *second opinion*. When chart_hacker says
"long BTC here", SecondBrain might say "no, I'm staying flat" or "I'd short
this". Two brains, two opinions, head-to-head.

```
                      ┌─────────────────────────────────────┐
   Feed (charts,      │  chart_hacker   →  its own signals   │
   news, candles,  ───┤                                      │
   trader posts)      │  SecondBrain    →  ITS OWN signals   │  ← the new thing
                      └─────────────────────────────────────┘
                                  │
                  copy wallets mirror EITHER brain's signals
                  (copy_charthacker, copy_secondbrain, …)
```

---

## 3. What makes a brain a "brain" (the 4 things it must own)

For SecondBrain to be a real brain and not another copier, it must do all four:

1. **Decide** — independently choose: take / skip / hold / take-profit / exit /
   resize. Not "mirror what chart_hacker did."
2. **Have a personality** — a different model, prompt, or philosophy from
   chart_hacker, so its opinions genuinely differ (otherwise it's just a slow
   duplicate).
3. **Remember** — recall its *own* past trades and lessons before each decision
   (mem0 + MemU), scoped to itself.
4. **Learn** — when its trades close, generate its *own* postmortems and feed
   those lessons back into memory.

If it skips any of these, it collapses back into being a copier or a clone.

---

## 4. Where it would plug into the existing system

Good news: the rails already exist. SecondBrain mostly **reuses** the
chart_hacker pipeline with a different identity and a different prompt.

| Capability | Existing machinery it would reuse | What's new for SecondBrain |
|-----------|-----------------------------------|----------------------------|
| Read the feed | `interpretation_service.py` (media + news + candles) | Nothing — same inputs |
| Form an opinion | LLM vision/quant tracks | **New prompt + persona** (its philosophy) |
| Write a decision | `tracked_positions` (with `actor_id`) | New `actor_id = 'jarvais_secondbrain'` |
| Recall memory | `_recall_relevant_memories()` + mem0/MemU | New mem0 scope `get_memory("jarvais","secondbrain")` |
| Score outcomes | `edge_scorer` / `coach_service` | Add SecondBrain to the agent set |
| Learn | `postmortem_service.py` | Runs per-actor — just include the new actor |
| Be copied/sized | `copy_trade_monitor.py` | Optional `copy_secondbrain` wallet |
| Show on dashboard | leaderboard reads `actor_id` / `agent_id` | Appears automatically once it writes rows |

The key design choice: **SecondBrain is a new `actor_id`, not a new codebase.**
It is "chart_hacker's pipeline run a second time with a different brain config."

---

## 5. What its "personality" could be (pick one to start)

The whole point is that it must *disagree* with chart_hacker sometimes. Options:

- **Different model** — e.g. chart_hacker on one vision model, SecondBrain on
  another. Cheapest to try; differences come from the model itself.
- **Different philosophy (same model)** — e.g. SecondBrain is a *skeptic*: only
  takes high-conviction setups, demands confluence, sits out chop. This is the
  most interesting because you learn whether "trade less, trade better" beats
  chart_hacker.
- **Different timeframe / style** — e.g. chart_hacker is reactive/intraday,
  SecondBrain is swing/positional and ignores noise.
- **Counter-trend / devil's advocate** — explicitly looks for reasons the
  popular setup is a trap.

Recommendation if we ever build it: start with **"skeptic, same model"** — it's
the cleanest test of whether a *different mindset* (not just a different model)
adds edge.

---

## 6. The decision loop (what one tick would look like)

```
For each new chart/news item in the feed:
  1. SecondBrain recalls its OWN relevant memories
     (past trades on this symbol, its own lessons).
  2. SecondBrain runs its OWN prompt (its philosophy) over the
     chart + quant indicators + recalled memory.
  3. It outputs ONE of: take(long/short, entry, sl, tp) | skip | (for
     open positions) hold | take-profit | exit | resize — WITH a reason.
  4. If it decides to act, write a tracked_position with
     actor_id='jarvais_secondbrain'.
  5. When that position later closes, postmortem_service grades it and
     pushes the lesson into SecondBrain's memory (mem0 + MemU).
```

Note step 3 includes **managing open positions**, not just entries — that's a
big part of what makes it a trader and not a signal generator.

---

## 7. How we'd know if it's any good

This is the real payoff. Because both brains trade the same feed:

- **Head-to-head leaderboard** — SecondBrain vs chart_hacker on the same
  contest, same period: win rate, P&L, Sharpe, drawdown (via `edge_scorer`).
- **Agreement analysis** — when do they agree vs disagree, and who's right when
  they disagree? (That's where the gold is.)
- **Skill-vs-luck** — the Phase Y skill score already exists; just include the
  new actor.

If SecondBrain consistently loses, we delete it (it's just an `actor_id`).
If it wins, we've discovered a better mindset — and we can promote it.

---

## 8. Risks & honest downsides

- **Cost.** A second brain = a second set of LLM calls per item. Roughly doubles
  the interpretation spend. Mitigated by only running it on items that pass the
  prefilter, and/or only on high-quality charts.
- **Noise / false competition.** If its personality is too close to
  chart_hacker, it's just an expensive clone. The persona must be deliberately
  different.
- **More moving parts.** A new daemon/identity to monitor, heartbeat, and keep
  from drifting. (Reuses Phase R canaries, so not free but not huge.)
- **Memory contamination.** Must be scoped to its own mem0 agent_id so it
  doesn't learn from chart_hacker's mistakes as if they were its own.
- **It might just be worse.** Most likely outcome for a first attempt. That's
  fine — it's a cheap experiment if scoped as one actor.

---

## 9. Rough effort (if we ever say go)

| Piece | Effort |
|-------|--------|
| New persona + prompt + config | ~0.5 day |
| Second interpretation pass (new actor_id, own recall scope) | ~1.5 days |
| Postmortem + memory wiring for the new actor (mostly reuse) | ~0.5 day |
| Leaderboard / agreement analysis view | ~1 day |
| Optional `copy_secondbrain` wallet | ~0.5 day |
| Testing + tuning the persona so it actually differs | ~1–2 days |
| **Total** | **~5–6 days** |

---

## 10. Decision

**Not building this now** (per owner, 2026-05-29). This file exists so that when
we revisit it, we start from a clear picture instead of from scratch.

**If/when we proceed, the first question to answer is just:**
*"What is SecondBrain's personality?"* — everything else follows from that.
