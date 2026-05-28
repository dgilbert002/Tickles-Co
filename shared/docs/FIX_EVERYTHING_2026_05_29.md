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
## Phase 5 — Trading Floor  ⏳ (pending)

_(Filled in as each phase ships.)_
