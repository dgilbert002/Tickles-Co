# Fix-Everything — 2026-05-29 (Radar / Candles / Positions / Paper-vs-Demo)

> Plain-language roadmap for the multi-phase fix requested after the
> Signals-Radar + Paper-vs-Demo audit. Written so a non-developer (or future-me
> starting cold) can follow exactly what changed, why, and how to undo it.
>
> Phases, in order: **1** Paper-vs-Demo · **2** Signals & Radar · **3** Pending
> lifecycle · **4** Candle tracking · **5** Positions feed.

---

## Phase 1 — Paper vs Demo  ✅ (code) / ⏳ (one operational step)

### The plain-English problem
The "Paper vs Demo" tab compares each paper trade against the matching real
order placed on a demo exchange. It was broken in four ways:

1. **The comparison never lined up.** The demo bridge stamps each demo order
   with `tracked_position_id` (the signal it came from), but the dashboard was
   matching on `competition_trade_id` (which the bridge never fills in). So the
   two halves never met → the "Demo" column was always blank.
2. **The KPIs were fake.** "Demo Orders $0" and "Drift —" were hardcoded
   placeholders, not real numbers.
3. **"0 filled" forever.** Nothing ever asked the exchange "did this order
   fill?", so demo orders sat as `pending` for eternity.
4. **The "1 rejected" mystery.** One demo order was rejected and the operator
   had no idea why. Root cause below.

### Why the order was rejected (the important one)
Every signal is stored with `notional_usd = 1000` — the **whole** paper wallet.
The demo bridge mirrored that 1:1 onto the ~$1000 demo accounts and committed
`margin = notional / leverage` per order. Because it fires for **every** signal
on **every** account, those orders **stacked** until the demo accounts ran
completely out of margin (we found a BNB position at **5120 notional** and
**negative free margin**). Once an account is maxed out, Bybit rejects every new
order with `retCode 110007 "ab not enough for new order"`.

The paper agents, by contrast, only commit ~$30–72 of margin per trade (3–5%
risk). So the demo side was sizing ~20× too big.

### What changed (files)
| File | Change |
|------|--------|
| `shared/dashboard/market_routes.py` | `handle_paper_vs_demo`: join demo_orders on **`tracked_position_id`** (was `competition_trade_id`); compute real **`demo_notional`** + **`drift`** (paper−demo P&L over *matched filled* pairs only); also append **orphan** demo orders (orders for signals no paper agent traded) so the "1 rejected" is visible. |
| `shared/dashboard/static/mirror.js` | "Demo Orders $" now shows `demo_notional`; "Drift" shows the real drift (or "—" until a fill exists); orphan rows tagged `demo-only`. |
| `shared/daemons/demo_bridge.py` | **Sizing rewrite**: commit a small fixed margin per trade (`DEMO_MARGIN_USD`, default $50 ≈ 5% of $1000), then `notional = margin × leverage`, capped at the signal's own notional and shaved by `DEMO_NOTIONAL_SAFETY_PCT` (0.99) for fees. Added **`_reconcile_fills()`** that polls the exchange and promotes `pending → filled` (capturing fill price + entry slippage) or `→ cancelled`. Removed the per-insert `pool.close()` calls (they closed the shared singleton pool). Record the *effective* notional in `demo_orders`. |
| `shared/execution/ccxt_adapter.py` | `set_leverage` failure log bumped `debug → warning` (it silently let orders fall back to 1× margin). |
| `shared/migration/2026_05_29_demo_orders_table.sql` (+ ROLLBACK) | Captures the live `demo_orders` schema as a proper, idempotent migration (it had none → schema-drift risk). |
| `shared/scripts/flatten_demo_accounts.py` | One-shot maintenance tool to close the stale junk positions + cancel resting orders on the demo accounts, freeing margin. Dry-run by default; `--apply` to execute. |

### New env knobs (all optional, sane defaults)
- `DEMO_MARGIN_USD` (default `50`) — margin committed per demo trade.
- `DEMO_NOTIONAL_SAFETY_PCT` (default `0.99`) — extra fee/slippage shave.

### The one operational step left
The demo accounts are still holding the old oversized junk positions, so they
have ~$0 free margin. New (correctly-sized) orders can't fill until those clear.
Run once:
```bash
# preview:
python3 -m shared.scripts.flatten_demo_accounts
# execute:
python3 -m shared.scripts.flatten_demo_accounts --apply
```
Then restart the bridge so new sizing takes effect:
```bash
systemctl restart tickles-demo-bridge.service
```

### How to verify
- `GET /api/paper-vs-demo` returns `summary.demo_notional`, `summary.drift`,
  and orphan rows with `orphan:true`.
- After flatten + a fresh signal, a demo order should reach `status='filled'`
  with a `demo_entry` and the "Demo Entry"/"Slippage" columns populate.

### Rollback
- Code: revert the 5 files above (git).
- Migration: `2026_05_29_demo_orders_table_ROLLBACK.sql` (DESTRUCTIVE — drops the
  table; only for dev/fresh envs).
- Sizing: set `DEMO_MARGIN_USD` back to a large value (e.g. `1000`) to restore
  old full-wallet behaviour without a code change.

---

### Flatten — DONE (2026-05-29)
All 3 Bybit demo accounts flattened (closed ~24 positions, cancelled resting
orders). Result: each account back to ~$1000 free, 0 used. Bridge restarted;
new orders now size correctly (e.g. DOT limit @ 346.5 notional = $49.5 margin ×
7× leverage, accepted as `pending`).

---

## Phase 1.5 — Agent sizing rules & tunability (audit finding)

### The 12 competition agents (what each does)
| Agent (DB id) | Rule | Margin / trade | Leverage | Max open |
|---|---|---|---|---|
| A: Spot Seq (`copy_spot_seq`) | full-wallet spot, one at a time | 100% | 1× | 1 |
| A×3: Spot Lev 3x (`copy_spot_lev_3x`) | full-wallet, one at a time | 100% | 3× | 1 |
| B: Lev Parallel (`copy_lev_parallel`) | risk-based, leverage from SL distance | **5%** | ≤100× | **20** |
| C: +BE Lock (`copy_lev_be_lock`) | like B + move SL to breakeven at +5% | **5%** | ≤100× | 20 |
| D: 3% Lev Par (`copy_lev_3pct`) | risk-based | **3%** | ≤100× | **33** |
| A+Opt (`copy_opt_spot_seq`) | A, with optimised SL/TP | 100% | 1× | 1 |
| B+Opt (`copy_opt_lev_parallel`) | B, with optimised SL/TP | 5% | ≤100× | 20 |
| C+Opt (`copy_opt_lev_be_lock`) | C, with optimised SL/TP | 5% | ≤100× | 20 |
| CH: AI Vision (`copy_ch_ai_vision`) | full-wallet spot (the SOL/USDT one) | 100% | 1× | 1 |
| Rose A (`copy_rose_a`) | spot seq | 100% | 1× | 1 |
| Rose B (`copy_rose_b`) | 5% lev parallel | 5% | ≤100× | 20 |
| Rose C (`copy_rose_c`) | 5% + BE lock | 5% | ≤100× | 20 |

### The tunability gap — FIXED (2026-05-29)
The 5% / 3% / max-concurrent (20, 33) / leverage cap (100×) / 3x were **hardcoded
constants** in `copy_trade_monitor.py`. They are now operator-tunable from the
**Settings → Agent position sizing** panel, with the same DB→env→default
resolution + 60s cache as the dedup knobs. Defaults reproduce the old behaviour
exactly (no change until edited).

| File | Change |
|------|--------|
| `shared/intelligence/copy_sizing_config.py` (NEW) | Knob registry + get/set/get_all with bounds, env override, 60s cache. Namespace `copy_sizing`. |
| `shared/intelligence/copy_trade_monitor.py` | New `SIZING` dict + `_load_sizing_knobs()` (refreshed each tick); `_enter_agent_position` reads the knobs instead of literals. |
| `shared/dashboard/settings_routes.py` | `GET/POST /api/settings/copy-sizing`. |
| `shared/dashboard/web/index.html` | "Agent position sizing" panel + `app.js?v=14-sizing`. |
| `shared/dashboard/static/app.js` | `renderSizingPanel()` — number inputs + Save per knob. |

**Knobs (env override / default):** `risk_pct_5` (`COPY_RISK_PCT_5`/5.0%),
`risk_pct_3` (3.0%), `max_concurrent_5` (20), `max_concurrent_3` (33),
`leverage_cap` (100×), `spot_lev_3x` (3×). Bounds enforced server-side.

**Verified:** GET returns all knobs; POST round-trip writes + audits; out-of-bounds
(999%) rejected; monitor restarts clean and loads knobs each tick.

**Rollback:** revert the 5 files; delete `system_config` rows where
`namespace='copy_sizing'` to drop back to defaults.

### Demo-mirror note (future)
The demo bridge currently mirrors *every* signal to *every* mapped demo account
with ONE universal sizing (`DEMO_MARGIN_USD`). To make Paper→Demo→Live a true
1:1 comparison, each demo account should eventually size like its **assigned
agent** (e.g. the account mapped to `copy_lev_parallel` sizes at 5%). Tracked as
a follow-up, not blocking.

---

## Phase 2 — Signals & Radar  ✅ (done 2026-05-29)

**Problem (plain English):** the same coin could show a *different* "distance to
entry" on the Trading Floor vs the Radar card, and sometimes "TO ENTRY: —" with
no number at all. Three different bits of code calculated the distance three
different ways:
- the candle path used the **absolute** distance (always positive, no direction),
- the live-price fallback used the **signed** distance,
- the `/api/unified-signals` endpoint used **absolute** again.
On top of that there was a dead, never-rendered Radar tab (`renderRadarPage`)
still wired to filter boxes that no longer exist in the page.

**Fix:**
1. **One distance, signed everywhere.** Every backend path now returns a
   **signed** `distance_to_entry_pct` = `(live − entry) / entry × 100`.
   `+` = price is **above** entry, `−` = **below**. Sorting "closest first" is
   done in the browser with `Math.abs()`. So the number means the same thing on
   every tab.
2. **Live-price fallback added to `/api/unified-signals`.** Symbols with no local
   1-minute candles (exotic / brand-new perps) used to show "—". They now get a
   bounded CCXT live-price probe — the *same* helper the Radar uses — so the
   distance is real and identical across tabs.
3. **Floor sorts by closeness too.** The Trading Floor signal list is now sorted
   closest-to-entry, matching the Radar's order.
4. **Dead code retired.** `renderRadarPage()` is marked LEGACY/UNUSED and its
   filter wiring is commented out (kept for rollback). The live tab routes
   through `renderUnifiedPage()` as it has for months.

**Files touched:**

| File | What changed |
|------|--------------|
| `shared/dashboard/market_routes.py` | `_classify_signal_path` distance abs→signed; `/api/unified-signals` distance abs→signed **and** calls `_enrich_radar_with_live_prices(result, max_probes=40)`; helper now reads `exchange` or `instrument_exchange` and takes an optional `max_probes` cap. |
| `shared/dashboard/static/app.js` | New `signalDistanceSigned()` (single source of truth); `signalDistance()` = its abs (sorting only); `radarCard` "to entry" now shows the signed, colour-coded value via `pct()`; Floor signal list sorted by distance; dead radar wiring commented out; `renderRadarPage` flagged legacy. |
| `shared/dashboard/web/index.html` | `app.js?v=15-radar-distance` cache-buster. |

**Verified:** `/api/unified-signals` now returns signed distances (51 neg / 16 pos
in a live sample), live-fallback runs without error, no dashboard log errors after
restart. `/api/entry-radar` returns the same signed contract (currently 0 rows
because there are **0 pending positions** — that supply problem is Phase 3, not a
Phase 2 regression).

**Rollback:** revert the three files above. The legacy `renderRadarPage()` and its
commented wiring are preserved verbatim if the old standalone Radar tab is ever
restored.

## Phase 3 — Pending lifecycle + radar supply  ✅ (done 2026-05-29)

**Finding (plain English):** the pending→open→close lifecycle is NOT broken.
Every open position activated *live* and sat in `pending` for >60 min until a
real 1-minute candle actually crossed the entry. The radar looked empty because
(a) real setups are sparse (~10–15/day) and each only waits in `pending` for ~1h,
and (b) a lot is correctly filtered out (154 no-setup AI charts, ~60 dupes, ~42
unsupported symbols).

**ROOT CAUSE found while verifying:** `/api/unified-signals` ordered its `LIMIT`
by `si.id ASC`, so it returned the OLDEST 120 interpretations (long-closed) and
never the recent ones — the radar was permanently stale regardless of filters.
Fixed to `ORDER BY si.id DESC, tp.id DESC`.

**Shipped:**
1. New `tracked_positions.activated_at` column (migration
   `2026_05_29_tracked_positions_activated_at.sql` + ROLLBACK), set once at the
   pending→open transition in `position_monitor._activate_pending_positions`.
2. `/api/unified-signals` now returns a signed distance (Phase 2), a live-price
   fallback, a `radar_meta` block (pending / filled_24h / cancelled_24h /
   no_setup_24h / dupes_24h / unsupported_24h / just_filled_30m), `activated_at`
   per row, and **newest-first ordering**.
3. Frontend (A): honest empty-state — "No setups waiting — last 24h: X filled,
   Y cancelled (Z no-setup, W dupes)" instead of "No signals".
4. Frontend (B): `JUST FILLED` badge + green accent on positions activated in the
   last 30 min, via `isJustFilled()` / `isPreEntry()` helpers.

**Verified:** newest-first now returns 14 open + recent signals (was returning
oldest/closed); `radar_meta` populated; `activated_at` column live.

## Phase 3B — Signals & Radar = "approaching only"  ✅ (done 2026-05-29)

- Stripped history/closed out of the Signals & Radar tab. Status dropdown is now
  "Approaching entry" (default = pre-entry + just-filled) and "All active
  signals". History lives in Positions.
- Subtitle rewritten to explain the approaching→position flow.
- Card branch now calls `wireRows()` (radar cards were previously **not
  clickable** — a dead-click bug) so every card opens the unified drawer.

## Unified accurate drawer  ✅ (done 2026-05-29)

**Problem:** the drawer showed entry/SL/TP = `0.000`. Root cause: it read levels
ONLY from `signal_interpretations.*`, which are frequently NULL for AI-inferred /
entry-only signals, even though the real `tracked_positions.*` row carried the
true entry.

**Fix:** `/api/signal-replay` now coalesces si-level → tp-level (prefers a
non-null / non-zero signal value, falls back to the position). `levelCards()`
already filters out 0/null so absent levels render "—", not a fake `0.000`. Same
drawer (chart replay + levels + trader/LLM theses + trade journey) is reachable
from the radar cards, the signals list, and the positions tables.

**Verified:** `/api/signal-replay?id=2900` (IMX) now returns `entry: 0.1628`
(from the position) instead of `0.000`; SL/TP correctly `null` (none were set).

## Phase 4 — Positions restructure  ✅ (done 2026-05-29)

The Live/Historic sub-tabs + lazy historic + drawer already existed (Round 11).
What was missing — now fixed:

1. **Paper agents were invisible.** The 9 open paper-agent copies live in
   `competition_trades` (`exited_at IS NULL`), which `aggregate_live_positions`
   never read. Now unioned in as **one row per agent** (no de-dupe), with live
   `current_price` joined from the linked `tracked_position`, accurate price-move
   %, and a `$` P&L from `notional = allocated × leverage` (verified vs the
   monitor's own figure — ch_ai_vision SOL short ≈ +1027 vs monitor +1037).
   Live feed went from 25 → **34 rows (24 signal + 9 paper-agent + 1 broker)**.
2. **Origin column + tags** (`paper` / `broker` / `signal`) so multi-agent rows
   are distinguishable.
3. **Per-agent ↔ Grouped toggle** on the Live sub-tab. Grouped = one row per
   coin+direction with holder count, aggregate P&L/notional, and the list of
   holders.
4. **Competition-style flash pulse** — P&L cells flash green/red when the value
   changes on the 5s tick (`flashLivePnl()` + `flash-up`/`flash-down` CSS).
5. Drawer reachable from every live + historic row (now accurate, see above).

**Files:** `shared/dashboard/snapshot.py` (competition_trades source + `origin`
in `_normalise_position_row`, union + enrichment in `aggregate_live_positions`),
`shared/dashboard/static/app.js` (origin column, grouped view, `setLiveView`,
flash), `app.css` (origin tags + flash), `index.html` (toggle + cache-busters).

**Rollback:** revert those four files; the competition_trades read is additive
(remove the `ct_rows` block in `aggregate_live_positions`).

## Phase 5 — Trading Floor  ✅ (done 2026-05-29)

1. **Open P&L KPI** was a stub (`+$0.00 · 1 open position`) because the snapshot
   counted only `positions_current` (one demo fill). Now it sums the same
   `aggregate_live_positions` union → **+$1025 · 34 open positions**.
2. **Best Trader/Agent KPI** now ranks by **live equity** (balance + unrealized)
   instead of `return_pct` → `copy_ch_ai_vision · $2108 equity`.
3. **Competition leaders** mini: removed the top-6 cap → shows **all 12 agents,
   scrollable**, ranked by live equity, each line shows **closed + unrealized +
   equity**.
4. **Signals approaching entry** = the radar approaching list (closest-first); if
   none, shows "Last to hit: SYM dir → exchange (time)".
5. **Live positions** = the all-agents union (`/api/positions/live`) with the
   **Origin** column — paper agents (`copy_*` pink tags), broker, signal — and
   the flash-on-change pulse.
6. **Perf:** floor signals + positions now render **independently/parallel** with
   "Loading…" placeholders (no more 6 s blank), and the heavy signal-enrichment
   fetch runs every 4th pulse (~20 s) instead of every 5 s.

**Also fixed (found during browser validation):** `_unifiedView` was declared
`const` but `setUnifiedView` reassigned it → the Signals & Radar **Cards/radar
toggle threw and was dead**. Changed to `let`. Radar cards now render + click
through to the drawer.

**Files:** `shared/dashboard/snapshot.py` (Open P&L union + best-agent equity),
`shared/dashboard/static/app.js` (renderStats label, renderCompetitionMini,
renderFloor split + floorPosRows, pulse, `_unifiedView` fix), `app.css` (leader
scroll), `index.html` (cache-busters).

**All five phases (1–5) complete and browser-validated.**

## Phase 6 — Forensic "Paper vs Demo vs Live" + copy-path repair  ✅ (done 2026-05-29)

This is the phase the owner cared about most: a **forensic, line-by-line audit**
of every paper signal vs. the demo order that mirrors it (and a structural lane
for live), so nothing — not $0.01 of fees, not a one-candle timing difference —
slips through the cracks.

### 6a. Copy-path repair (the demo orders themselves)

Plain English: the demo bridge takes a paper signal and places a matching limit
order on the mapped Bybit/Bitget demo account. It mostly worked for Bybit but
had four problems. All fixed:

1. **Bitget rejected EVERY order (`40774` "unilateral position")** — the bridge
   never set Bitget's position mode, so our order params didn't match the
   account. `ccxt_adapter._ensure_one_way_mode()` now forces Bitget one-way mode
   (`set_position_mode(hedged=False)`) the same way it already did for Bybit.
   Result: `40774` is **gone**.
2. **No agent attribution** — `demo_orders.agent_id` was blank, so you couldn't
   tell which scenario an order belonged to. The bridge now resolves the
   account→agent map (1 demo account = 1 agent) and stamps `agent_id` on every
   order. The forensic API also back-fills it at read time for old rows.
3. **Duplicate-order stacking on restart** — `_load_mirrored()` only excluded
   signals that had a PAPER fill, so every daemon restart re-placed a fresh
   resting order for not-yet-filled signals. They piled up and ate the account
   margin → "ab not enough" (Bybit `110007`). Now it ALSO excludes any signal
   that already has a pending/filled demo order. Rejected ones can still retry.
   We flattened all demo accounts once (`flatten_demo_accounts.py --apply`) and
   marked stale `pending` rows `cancelled`; the bridge then re-placed **one
   clean order per account** (verified: zero duplicates).
4. **No fee capture** — `_reconcile_fills()` now records the execution
   `entry_fee` (and stamps `fees_synced_at`) when a demo limit order fills, so
   the forensic view can account for fees.

**Remaining (NOT a code bug):** the Bitget demo account `TicklesCo_BG_1` has
**$0 virtual USDT** (verified via `fetch_balance` → `0.0`). It rejects with
`40754 "balance not enough"` until you claim/fund virtual USDT in the Bitget
demo UI. The 3 Bybit demo accounts are funded (~$1000) and place orders fine.

**Per-agent sizing fidelity — ✅ FIXED 2026-05-29 (see 6g below).** The bridge
no longer uses a flat `DEMO_MARGIN_USD` ($50); each demo order now reproduces the
EXACT sizing rule of the paper agent mapped to that account.

### 6b. Forensic transaction log (the "check the log" surface)

- NEW `shared/daemons/demo_forensic_log.py` — append-only **JSON-lines** log at
  `/opt/tickles/shared/logs/paper_demo.log` (rotating, 5 MB × 5). `flog(event,
  **fields)` is called by the bridge on every `mirror_placed`,
  `mirror_rejected`, `mirror_error`, `fill`, and `cancel`. Never raises into the
  trading path.
- NEW route `GET /api/paper-demo/log?lines=N&event=…` tails it for the UI.
- You can also just `tail -f shared/logs/paper_demo.log` from a shell.

### 6c. Forensic backend API

- `GET /api/paper-vs-demo` rebuilt: per-order **line items** (paper entry, demo
  entry, slippage %, fees, paper P&L, demo P&L, leverage, notional, ordered/fill
  timestamps), agent + exchange + account on every row, full reject message,
  plus a **`reject_summary`** (errors classified into human causes and counted)
  and an **`accuracy`** block (entry-accuracy = 100 − mean |slippage|, fill rate,
  matched-filled count). Live-lane fields (`live_*`) are present and zero until a
  live account is mapped — no schema change needed to turn live on.
- NEW route `GET /api/paper-demo/exchange-state` — **on-demand** live pull from
  each demo/live account: wallet balance, open positions, resting orders straight
  from the exchange (button-triggered, not auto-refreshed, because it hits the
  network).
- Migration `2026_05_29_demo_orders_fees.sql` adds `entry_fee/exit_fee/funding_fee/
  fees_synced_at` (rollback provided).

### 6d. Forensic UI — "Paper vs Demo vs Live" tab

Three sub-views inside the tab:
- **Comparison** — the line-item table (demo-linked rows sorted to the top),
  accuracy badge (honest empty-state until a demo order fills), KPI strip
  (paper P&L, demo orders, rejected, demo fees, drift, live), and **reject-cause
  cards** (e.g. "10 Insufficient margin", "4 Position-mode mismatch").
- **Exchange state (live)** — a card per demo/live account showing live balance,
  open positions and resting orders, tagged with the mapped agent. This is the
  "what's ACTUALLY on the exchange" reconciliation view.
- **Transaction log** — the live, color-coded forensic log viewer.

### 6e. Trading Floor — Competition ↔ Demo accounts toggle

The "Competition leaders" panel got a **Competition / Demo accts** toggle. "Demo
accts" lists the mapped demo accounts ranked by balance, each showing its agent
(scenario) and P&L vs the $1000 start. (Balances come from the last account
sync; the Exchange-state view has the live figures.)

**Files:** `shared/daemons/demo_bridge.py`, `shared/daemons/demo_forensic_log.py`
(NEW), `shared/execution/ccxt_adapter.py` (bitget one-way), `shared/dashboard/
market_routes.py` (forensic API + 2 routes + `_classify_reject`/`_account_agent_map`),
`shared/dashboard/static/mirror.js` (forensic UI), `shared/dashboard/static/app.js`
(leaders toggle + title), `app.css`, `index.html` (tab markup + cache-busters),
`shared/migration/2026_05_29_demo_orders_fees.sql` (+ rollback).

**Rollback:** revert those files; drop the fee columns with the rollback SQL. The
copy-path logic changes are additive/guarded (the dedup UNION, agent stamping,
forensic logging) and safe to revert independently.

**Browser-validated 2026-05-29:** all three forensic sub-views render with live
data; leaders toggle flips correctly; bitget `40774` gone; no duplicate orders.

### 6g. Accurate per-agent demo sizing  ✅ (done 2026-05-29)

**Plain English (explain-like-21):** every paper agent has its own way of
deciding how big a trade is. Some go "all-in" with the whole wallet (the spot
agents and the AI Vision brain), one goes all-in but at 3× leverage
(`copy_spot_lev_3x`), and the rest risk a small slice — 5% or 3% of the wallet —
and lever it up based on how tight the stop-loss is. The demo bridge used to
ignore all that and just bet a flat $50 of margin on EVERY order no matter which
agent's account it was. So the "paper vs demo" comparison wasn't fair: a 3×
agent's demo order looked the same size as a 5% agent's. We fixed it so the demo
order is sized **exactly** like the paper agent would size it, using that demo
account's **real** balance.

**What changed (`shared/daemons/demo_bridge.py`):**
- New `AGENT_MODE` map (agent_id → sizing mode) and `MODE_MAX_CONCURRENT` map —
  kept in sync with the single source of truth,
  `shared/intelligence/copy_trade_monitor.py` (the `AGENTS` list + sizing block).
- New `_compute_demo_size(agent_id, balance, entry, sl)` — reproduces the paper
  formula **per agent**:
  - `spot_seq` (Spot Seq, AI Vision, Rose A): margin = full balance, **1×**.
  - `spot_lev_3x`: margin = full balance, **3×** → 3× notional.
  - `lev_3pct`: margin = **3%** of balance, leverage = `1/sl_dist` (cap 100×).
  - `lev_5pct` (everything else / default): margin = **5%**, same leverage rule.
  - Margin is shaved by `DEMO_NOTIONAL_SAFETY_PCT` (0.99) for fee/slippage room.
- New `_refresh_balances()` — pulls each mapped account's **real USDT balance**
  once per tick (cached), so sizing tracks the actual money, not a guess. Falls
  back to `DEMO_FALLBACK_BALANCE` ($1000) only if the balance call fails.
- New `_open_demo_count()` + per-account **concurrency cap** — sequential agents
  (spot / 3×) get **1** live demo order at a time; the 5%/3% parallel agents get
  up to **20 / 33** (matching the paper caps), so the demo can't over-place and
  exhaust margin. Over-cap signals are skipped and logged as `mirror_skipped_cap`.
- Sizing now runs **inside** the per-account loop (it depends on the account's
  agent + balance), and the old `min(notional, signal_notional)` clamp was
  REMOVED — `signal.notional_usd` is always the $1000 paper wallet, not a
  position ceiling, so clamping to it would have silently undone 3× and the
  leveraged agents.

**New env knobs (all optional, defaults match the paper agents):**
`DEMO_RISK_PCT_5=5.0`, `DEMO_RISK_PCT_3=3.0`, `DEMO_SPOT_LEV_3X=3.0`,
`DEMO_LEVERAGE_CAP=100.0`, `DEMO_FALLBACK_BALANCE=1000.0`. `DEMO_MARGIN_USD` is
no longer used for sizing (left defined for backwards-compat).

**Verified 2026-05-29** (live balances + sizing on the 4 mapped accounts):
| Account | Agent | Mode | Margin | Lev | Notional |
|---|---|---|---|---|---|
| bybit/TicklesCo3 | copy_ch_ai_vision | spot_seq | $993.94 (full) | 1× | $993.94 |
| bybit/TicklesCo | copy_opt_lev_parallel | lev_5pct | $49.30 (5%) | 29× | $1,434.77 |
| bybit/TicklesCo2 | copy_opt_lev_be_lock | lev_5pct | $49.24 (5%) | 29× | $1,433.00 |
| bitget/TicklesCo_BG_1 | copy_spot_lev_3x | spot_lev_3x | $990 (full) | 3× | $2,970.00 |

(Bitget shows the $1000 fallback because that demo account is still unfunded —
the known funding issue, unchanged by this fix.)

**Rollback:** the change is contained to `demo_bridge.py`. To revert to the flat
$50 behaviour, restore the previous `_mirror_signal` sizing block (compute one
`leverage`/`margin_usd = DEMO_MARGIN_USD * safety`/`notional_eff` before the
account loop, capped to `signal.notional_usd`) and remove the `AGENT_MODE` /
`MODE_MAX_CONCURRENT` maps, `_compute_demo_size`, `_refresh_balances`,
`_open_demo_count`, the `self._acct_balance` init, and the `_refresh_balances`
call in `tick()`. No DB/schema changes were made.

### 6f. ChartHacker (copy_ch_ai_vision) autonomy — PROPOSALS ONLY
See [`COPY_CH_AI_VISION_AUTONOMY_PROPOSALS.md`](COPY_CH_AI_VISION_AUTONOMY_PROPOSALS.md) —
three options for letting the AI vision agent manage its OWN open trades
(partial TP, trailing SL, early close) using cron + MCP with the LLM woken only
on event triggers, logging its reasoning to mem0/MemU. No code shipped; the
owner reviews and picks one.

---

## Phase 7 — Pipeline stall fix + Radar hygiene + Freshness watchdog (2026-05-29)

**Why:** the dashboard looked "broken" — no new positions arming, and the radar
showed 2-day-old "COMP"/"rose" signals as "approaching entry." Audit found
multiple issues; all fixed below. Plain English first, then the code + rollback.

### 7a. ROOT CAUSE — the vision pipeline was crashing on every chart

**Plain English:** since 05:16 UTC, *every* chart the interpreter read crashed
and got marked `failed`. A recent edit referenced a variable
(`prefilter_version_label`) that was never created, so the code threw `NameError`
the instant it tried to record which prompt version it used. No charts analysed →
no signals → no positions → the radar kept showing only the old stale signals.

**Fix (`shared/intelligence/interpretation_service.py`, ~line 980):** initialise
`prefilter_version_label = "builtin-fallback"` before the prompt load; set it to
`"db:chart_prefilter"` on a DB hit. Requeued the 4 failed charts (6386–6389) →
all analysed cleanly next cycle (`media(analyzed=4)`). `prompt_version` audit
field now reads `prefilter:builtin-fallback` / `prefilter:db:chart_prefilter`
(free text, no downstream enum — safe).

**Rollback:** delete the two `prefilter_version_label = …` lines (don't — without
them the pipeline is dead).

### 7b. Arming gate — confirmed CORRECT, no change

COMP (interp 2793) and the recent ETH/ZEC/HYPE reads sit at confidence 0.0–0.20.
`create_tracked_position_from_interpretation()` skips anything below **0.4**
(or missing entry price, or symbol UNKNOWN). That is working as designed —
low-conviction/unclear reads should NOT arm. Threshold left untouched; lowering
it would flood the system with junk. "Nothing armed on a quiet market" is the
honest, correct state.

### 7c. Radar hygiene — "Approaching entry" is now ARMED-ONLY

Previously *any* read signal (incl. low-conviction `position_status='signal'`)
counted as "approaching," so the radar looked full of setups that would never
fire. Now "Approaching entry" shows **only armed setups** (a real `pending`
order is placed and waiting). Read-but-not-armed signals live under **"All active
signals."** Empty-state is honest: how long since the last read + how many read
in 24h. (The Trading-Floor "Signals approaching entry" panel shares `isPreEntry`,
so it's consistent automatically.)

**Files:** `shared/dashboard/market_routes.py` (`armed` bool +
`last_signal_min`/`signals_24h` in `radar_meta`); `shared/dashboard/static/app.js`
(`isPreEntry` now armed/`pending`-only; richer `radarEmptyState`; list-view empty
-state); `index.html` cache-buster `app.js?v=25-radar-armed-svc-health`.
**Rollback:** restore old `isPreEntry`, drop `armed` + the 2 meta keys.

### 7d. PREVENTION — data-freshness watchdog (`pipeline-watchdog`)

The old `cron_canary` only checks "is the process running?" — useless here
(services were "active" but producing no data). The new **PipelineWatchdog**
watches the DATA and reacts:
1. **Interpreter stalled** (fresh charts unprocessed >20 min) → **auto-restarts**
   `tickles-interpretation` (idempotent; 30-min per-unit cooldown).
2. **Vision failing** (≥3 `failed`, 0 `analyzed` in last hour — the exact 7a
   signature) → **alerts only** (a restart can't fix a code bug).
3. **Collector hung** (a feed silent ≥6 h *while a peer is live*) → **alert-only
   by default** (`WATCHDOG_RESTART_COLLECTORS=1` to auto-restart). Alert-only
   because a 1-channel feed like telegram can be legitimately quiet overnight.
Records its own heartbeat (so the dashboard + canary watch the watcher).

**Files (NEW):** `shared/intelligence/pipeline_watchdog.py`,
`systemd/tickles-pipeline-watchdog.service` (User=root for `systemctl`),
registered in `shared/services/registry.py`. Installed + enabled on the VPS.
**Tunables (env):** `WATCHDOG_CHECK_INTERVAL_S=300`, `WATCHDOG_INTERP_GRACE_MIN=20`,
`WATCHDOG_COLLECTOR_SILENCE_H=6`, `WATCHDOG_RESTART_COOLDOWN_S=1800`,
`WATCHDOG_AUTO_RESTART=1`, `WATCHDOG_RESTART_COLLECTORS=0`.
**Rollback:** `systemctl disable --now tickles-pipeline-watchdog.service`; remove
the unit + daemon-reload; delete `pipeline_watchdog.py` and the registry block.

> **ACTION REQUIRED for Telegram alerts:** `TICKLES_ADMIN_CHAT_ID` and
> `TICKLES_TELEGRAM_BOT_TOKEN` are currently **unset**, so the watchdog (and
> cron_canary) only LOG alerts — they don't push to Telegram. Set both in
> `/opt/tickles/.env` to enable push alerts. Until then, the dashboard **Service
> health** panel is the visibility path (see 7f).

### 7e. Queue hygiene + schema-gate alignment — `skipped_stale`

47 day-old images were stuck in `downloaded` forever (the interpreter only reads
news <24 h old). Now they age out to a terminal `skipped_stale` status, both as a
one-time sweep and a recurring hygiene step.
**Files:** `cleanup_stale_downloaded_backlog()` in `interpretation_service.py`
(wired into Slice-4 hygiene); migration `2026_05_29_media_skipped_stale_status.sql`
(+ `_ROLLBACK`) widens the `media_items_processing_status_check` constraint.
**Schema-drift gate:** the migration uses the `processing_status IN (...)` form
(NOT an explicit ARRAY cast) so `pg_dump` reproduces the canonical
`::character varying` form; the snapshot
(`shared/scripts/snapshots/tickles_shared.snapshot.sql`) was updated to match the
live DB **byte-for-byte** (also folding in the 3 statuses from the May-04
migration that had never been snapshotted — a pre-existing drift, now resolved).
**Rollback:** run `_ROLLBACK.sql`, remove the function + hygiene call, revert the
snapshot constraint line.

### 7f. Audit fixes (downstream effects)

- **`cron_canary` latent bug fixed** (`shared/intelligence/cron_canary.py`): it
  queried non-existent columns `last_heartbeat_at`/`status` (real columns are
  `last_run_at`/`last_status`), so it raised every cycle and never alerted.
  Aliased correctly + skip rows with null ts/interval. Left **disabled** (it's
  redundant with the dashboard staleness view until a Telegram token is set).
- **Service-health visibility** (`server.py` + `app.js` + `app.css`): the panel
  ignored heartbeat `status`, so a `partial` (problem-detected) watchdog still
  showed green "live." Now it surfaces **`degraded`** (amber) with the heartbeat
  message as a tooltip, and `stale` is amber too. This is the primary "I can SEE
  a problem" path while Telegram is unconfigured.

**Verification (2026-05-29):** 12 targeted tests pass (feed-hygiene, schema-diff,
cron-canary, master-sync, writer-registry, grep-guard); live constraint ==
snapshot (IDENTICAL); 4 charts reprocessed → analysed; radar API armed=0 →
honest empty-state; watchdog heartbeat `ok`/"pipeline fresh" and visible in
`/api/services`.

### Telegram collector
Restarted — it had only 1 (quiet) channel and reconnected cleanly; not actually
hung. Now covered by 7d's collector check.

---

## Rename: `copy_ch_ai_vision` → `copy_charthacker` (2026-05-29)

### Why (plain English)
The copy-agent id `copy_ch_ai_vision` (display "CH: AI Vision") *looked* like a
second, independent AI brain. It is **not**. It is just a paper wallet that
mechanically mirrors `chart_hacker`'s signals (1x, full wallet, sequential).
The dashboard shows the raw `agent_id`, so the misleading name was visible.
Renamed to `copy_charthacker` so it reads honestly as "the chart_hacker copier".

### What changed
**Code**
- `shared/intelligence/copy_trade_monitor.py` — `AGENTS` display "CH: AI Vision"
  → "Copy: ChartHacker"; `NAME_TO_ID` value → `copy_charthacker`. **Routing
  decoupled from the display name**: position routing + the CH log now key off
  the stable `mode == "spot_seq_ch"` instead of `agent_name.startswith("CH:")`,
  so the label can change without misrouting trades.
- `shared/daemons/demo_bridge.py` — `AGENT_MODE` key + `_agent_for_actor` return
  → `copy_charthacker`. The trader-signal exclusion `startswith('copy_ch_')`
  would NOT match `copy_charthacker` (char 7 is `a`, not `_`), so it was changed
  to an explicit `agent_id != 'copy_charthacker'` check. Without this fix the
  copier would wrongly start taking *trader* signals.
- `shared/dashboard/static/mirror.js` — agent id list.
- `shared/scripts/backfill_copy_agent_state.py`, `shared/daemons/demo_forensic_log.py` — id refs.
- `shared/intelligence/migrations/2026_05_24_copy_agent_state.sql` — seed row (fresh installs).
- `shared/tests/test_copy_trade_persistence.py` — 2 expectations.

**Database** (`tickles_shared`, agent_id has no FK; ran with both services stopped)
- Migration: `shared/intelligence/migrations/2026_05_29_rename_copy_charthacker.sql`
- Rows renamed: copy_agent_state(1), competition_trades(9), contest_participants(1),
  competition_agent_exchanges(2), demo_orders(4).

### Verification
- 22/22 `test_copy_trade_persistence.py` pass; both files compile.
- Post-restart, `copy_agent_state` has `copy_charthacker` (bal $1071, 8 trades,
  updated AFTER restart) and **zero** `copy_ch_ai_vision` rows recreated —
  proves the live service loaded the new id and persists to it.
- Both `tickles-copy-trade-monitor` and `tickles-demo-bridge` active, no errors.

### Rollback
1. `systemctl stop tickles-copy-trade-monitor tickles-demo-bridge`
2. `psql ... -f shared/intelligence/migrations/2026_05_29_rename_copy_charthacker_ROLLBACK.sql`
3. `git checkout` the code files listed above (revert to `copy_ch_ai_vision` /
   "CH: AI Vision" / `startswith` routing).
4. `systemctl start tickles-copy-trade-monitor tickles-demo-bridge`

### NOT done (still a copier, not a brain)
This was a **rename only**. `copy_charthacker` still mechanically mirrors
chart_hacker — it does not make its own decisions or learn. Building a real
independent "second brain" (Phase C, Option B) remains future work.
