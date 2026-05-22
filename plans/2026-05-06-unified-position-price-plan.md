# Unified Position & Price Infrastructure — Implementation Plan

## Objective
Fix the dashboard's position display (K4 Config tab bug, missing P&L/current prices, signals-vs-positions confusion) and build a unified live-price infrastructure: a single daemon streams tickers from all `.env`-configured exchanges (Bybit, BloFin, Bitget, Capital.com) into one WebSocket feed. Every consumer — dashboard, position_monitor, interpretation_service — subscribes to this single feed instead of making independent CCXT calls. A unified asset register table, populated from exchange market data, normalizes instruments across crypto and CFD venues so prices refresh everywhere: positions tab, signals tab, interpretation drawer, and any future consumer.

## Current State

### What exists now
- **Exchange configuration** ([`.env:90-135`](.env:90)): API keys for Bybit (live + 2 demo), BloFin (live + demo), Bitget (live), and Capital.com (5 live CFD accounts + 2 demo). These are the exchanges we must pull instruments and prices from.
- **Dashboard server** ([`shared/dashboard/server.py`](shared/dashboard/server.py:1)): aiohttp-based, intentionally no WebSockets, read-only snapshot polling.
- **Snapshot builder** ([`shared/dashboard/snapshot.py`](shared/dashboard/snapshot.py:1)): assembles `DashboardSnapshot` from SQL aggregations. `aggregate_open_positions` at line 569 fans out across companies, merges `positions_current` (live exchange ledger) with `tracked_positions` (interpretation/Surgeon bridge).
- **Position normaliser** ([`shared/dashboard/snapshot.py:469`](shared/dashboard/snapshot.py:469)): `_normalise_position_row` maps both source tables to a canonical shape. For `tracked_positions`, it reads `current_price` and `pnl_pct` from the DB row — but these columns are rarely populated.
- **Position monitor daemon** ([`shared/intelligence/position_monitor.py`](shared/intelligence/position_monitor.py:1)): polls `tracked_positions` for open positions, reads candles from `public.candles`, computes P&L/distance-to-SL-TP, writes snapshots to `position_updates` table. Does NOT write back to `tracked_positions.current_price` or `tracked_positions.pnl_pct`.
- **Live price helper** ([`shared/market_data/live_price.py`](shared/market_data/live_price.py:1)): single-shot CCXT `fetch_ticker` with timeout, used by interpretation service and `/api/price` endpoint. Only supports crypto exchanges (CCXT); no CFD support.
- **Batch price endpoint** ([`shared/dashboard/price_routes.py:353`](shared/dashboard/price_routes.py:353)): `GET /api/prices?symbols=SYM1,SYM2,...` with 30s cache TTL and token-bucket rate limiter. Crypto-only.
- **Frontend price hydration** ([`shared/dashboard/static/app.js:443`](shared/dashboard/static/app.js:443)): `_hydratePositionPrices()` batches all unique symbols from the DOM, calls `/api/prices`, updates `[data-role="live-px"]` cells. Only works for crypto-like symbols (`_isCryptoLike` filter).
- **Instrument normaliser** ([`shared/utils/instrument_normaliser.py`](shared/utils/instrument_normaliser.py:1)): `to_canonical_symbol` + `normalise_venue` for slash-form normalization.
- **Config tab** ([`shared/dashboard/config_provider.py`](shared/dashboard/config_provider.py:1)): reads `system_config` (shared) + per-company `company_config`. Accepts `company_filter` parameter.
- **Global company filter** ([`shared/dashboard/static/app.js:7`](shared/dashboard/static/app.js:7)): `state.companyFilter` set by `#company-filter` dropdown, appended as `?company=` to ALL API calls via the `api()` helper at line 29.

### Current behavior (what the user sees)
1. **Positions tab** shows a mix of `tracked_positions_open` (pending signals that haven't hit entry) and `positions_current` (actual exchange positions). Pending signals appear alongside open positions with no distinction.
2. **P&L columns** show `$0.00` / `0.00%` for most rows because `tracked_positions.current_price` and `tracked_positions.pnl_pct` are NULL.
3. **"Now" column** shows `…` for most rows because `_hydratePositionPrices` only works for crypto symbols and the batch endpoint may fail silently.
4. **Config tab** shows per-company config filtered by the global `#company-filter` dropdown — but there's no visual indication that filtering is active, and switching the global filter changes what the Config tab shows without the user realizing.
5. **No Discord trader attribution** — the "Actor" column shows `actor_id` or `trader_profile_id` but not the Discord handle/display name.
6. **No distance-to-entry ordering** — pending signals are sorted by `signal_timestamp DESC`, not by proximity to entry price.

## Problem / Gap

### Gap 1 — K4 Config tab: companyFilter vs company confusion
- The global `#company-filter` dropdown sets `state.companyFilter`, which the `api()` helper appends as `?company=` to every request.
- The Config tab fetches `/api/config/snapshot` which respects `?company=` — so switching the global filter silently changes what config the user sees.
- The Config tab has no per-tab company selector, so the user can't independently control what companies' config they're viewing.
- **Root cause**: The global filter is applied indiscriminately to all tabs. The Config tab should either ignore the global filter or have its own selector.

### Gap 2 — Positions tab shows pending signals, not actual positions
- `aggregate_open_positions` at [`snapshot.py:622`](shared/dashboard/snapshot.py:622) queries `tracked_positions WHERE status IN ('open', 'pending')` — this includes signals that haven't hit entry.
- The Positions tab should only show positions that have been entered (paper, demo, or live). Pending signals belong in the Signals tab.
- The Signals tab already exists and shows `aggregate_signals` — but it shows interpreted signals from `signal_interpretations`, not pending tracked positions.

### Gap 3 — No live price data flowing to positions
- `position_monitor.py` computes `current_price`, `pnl_pct`, `unrealized_pnl_usd` but writes them to `position_updates` (append-only log), not back to `tracked_positions`.
- The dashboard snapshot reads `tracked_positions.current_price` and `tracked_positions.pnl_pct` directly — these columns exist in the schema but are never populated.
- The frontend's `_hydratePositionPrices()` is a best-effort client-side patch that only works for crypto symbols and doesn't compute P&L.

### Gap 4 — No unified asset register (foundational gap)
- Instruments are stored in `public.instruments` with `symbol` + `exchange`, but there's no cross-exchange mapping.
- A signal for "BTC/USDT" on Bybit and a CFD for "BTC/USD" on Capital.com are treated as unrelated — no unified view.
- The `instrument_normaliser` handles symbol formatting but doesn't provide a registry of "all assets we care about."
- **No mechanism to discover instruments from `.env`-configured exchanges.** We have API keys for Bybit, BloFin, Bitget, and Capital.com — but nothing pulls their market listings into a unified table.
- Without a unified asset register, the price feed daemon (Phase 7) has nothing to subscribe to.

### Gap 5 — No unified WebSocket for live prices
- Every consumer (dashboard, position_monitor, interpretation_service) independently calls CCXT `fetch_ticker`.
- No shared subscription — if 3 positions hold BTC/USDT, that's 3 separate CCXT calls (or 1 batched call from the dashboard, but position_monitor still calls separately).
- The dashboard server explicitly declares "no websockets" at [`server.py:19`](shared/dashboard/server.py:19).
- **No CFD price support** — Capital.com instruments (XAU/USD, US100, SPX, etc.) have no live price path at all. The current `live_price.py` only supports CCXT crypto exchanges.
- The desired architecture: **one daemon** maintains WebSocket subscriptions to all configured exchanges, broadcasts tickers internally. Every other service subscribes to this single feed.

### Gap 6 — No Discord trader attribution on positions
- `tracked_positions` has `trader_profile_id` but the dashboard renders `actor_id` or `trader_profile_id` raw — no join to `trader_profiles` for `display_name` or `handle_normalized`.

## Desired End State

1. **Config tab** is immune to the global company filter — it always shows all companies' config, or has its own independent selector.
2. **Positions tab** shows ONLY entered positions (paper/demo/live), not pending signals. Status column shows `open`, `partial_exit`, `closed`.
3. **Signals tab** shows pending tracked_positions (status='pending') ordered by distance-to-entry (closest first), with live price, entry, SL, TP, and Δ% vs entry.
4. **Position rows** show accurate `current_price`, `PnL %`, and `PnL $` — sourced from `position_updates` (latest snapshot per position) or from the unified price feed.
5. **Actor column** shows Discord handle / display name from `trader_profiles`, not raw IDs.
6. **Unified asset register** — a `public.unified_instruments` table populated from ALL `.env`-configured exchanges (Bybit, BloFin, Bitget, Capital.com). Every instrument we track is mapped to a canonical asset with exchange-agnostic grouping. Adding a new exchange to `.env` means its instruments automatically appear here.
7. **Unified price feed daemon** — a standalone service (`shared/market_data/price_feed.py`) that:
   - Reads the unified instruments table to know WHAT to subscribe to
   - Opens ONE WebSocket connection per exchange (CCXT Pro for crypto, Capital.com API for CFDs)
   - Multiplexes all symbol subscriptions over that single connection
   - Broadcasts every ticker update to an internal pub/sub bus
   - Exposes a local WebSocket server that other services connect to
8. **All consumers subscribe** — the dashboard server bridges the price feed WebSocket to the browser; `position_monitor` gets live prices without reading candles; `interpretation_service` gets prices without individual CCXT calls. Prices refresh everywhere: positions tab, signals tab, interpretation drawer, KPI cards.

## Out of Scope
- Live/demo trading execution (Surgeon already handles this)
- Backtesting integration
- New exchange adapters
- Auth changes
- Manage panel changes
- Mobile app changes

## Relevant Files and Symbols

| File | Symbols / sections | Why it matters |
|---|---|---|
| [`shared/dashboard/static/app.js`](shared/dashboard/static/app.js:1) | `state.companyFilter` (L7), `api()` (L24), `renderPositions()` (L279), `_hydratePositionPrices()` (L443), `renderSignals()` (L323), `applyConfig()` (L723), tab registry (L112-122) | Frontend: all rendering, filtering, price hydration |
| [`shared/dashboard/web/index.html`](shared/dashboard/web/index.html:1) | `#company-filter` (L94), `#tab-positions` (L242), `#tab-signals` (L210), `#tab-config` (L464) | HTML structure for all affected tabs |
| [`shared/dashboard/snapshot.py`](shared/dashboard/snapshot.py:1) | `aggregate_open_positions()` (L569), `_normalise_position_row()` (L469), `aggregate_signals()` (L913) | Backend: position/signal aggregation, row normalization |
| [`shared/dashboard/server.py`](shared/dashboard/server.py:1) | `handle_snapshot()` (L262), `handle_positions()` (L283), `handle_signals()` (L276) | Backend: route handlers, company param extraction |
| [`shared/dashboard/config_provider.py`](shared/dashboard/config_provider.py:1) | `ConfigSnapshotProvider.fetch()` (L223), `_normalise_company_filter()` (L66) | Backend: config snapshot with company filtering |
| [`shared/dashboard/config_routes.py`](shared/dashboard/config_routes.py:1) | `handle_config_snapshot()` (L136) | Backend: config route handler |
| [`shared/dashboard/price_routes.py`](shared/dashboard/price_routes.py:1) | `handle_prices()` (L353), `_is_crypto_symbol()` (L69) | Backend: batch price endpoint, crypto-only filter |
| [`shared/intelligence/position_monitor.py`](shared/intelligence/position_monitor.py:1) | `fetch_open_positions()` (L117), `_resolve_instrument_id()` (L156), `PositionSnapshot` (L93) | Daemon: computes P&L but writes to `position_updates`, not `tracked_positions` |
| [`shared/market_data/live_price.py`](shared/market_data/live_price.py:1) | `fetch_live_price()` (L80+), `LivePriceResult` (L59), `SUPPORTED_EXCHANGES` (L36) | Shared: single-shot CCXT price fetch |
| [`shared/utils/instrument_normaliser.py`](shared/utils/instrument_normaliser.py:1) | `to_canonical_symbol()` (L88), `normalise_venue()` (L147) | Shared: symbol/venue normalization |

## Dependencies and Call Sites

### Data flow (current)
```
Discord signal → interpretation_service → tracked_positions (status='pending')
                                            ↓
                              position_monitor (reads candles, computes P&L)
                                            ↓
                              position_updates (append-only log)
                                            ↓
Dashboard snapshot ← tracked_positions (reads current_price, pnl_pct — usually NULL)
                                            ↓
Frontend ← _hydratePositionPrices() ← /api/prices (CCXT batch, crypto-only)
```

### What calls what
- `handle_snapshot()` → `SnapshotBuilder.build()` → `aggregate_open_positions()` → `_normalise_position_row()`
- `handle_config_snapshot()` → `ConfigSnapshotProvider.fetch()` → `_normalise_company_filter()`
- `renderPositions()` → `_hydratePositionPrices()` → `GET /api/prices`
- `position_monitor.main()` → `fetch_open_positions()` → `_resolve_instrument_id()` → `public.instruments`
- `api()` helper → appends `?company=state.companyFilter` to ALL requests

### Knock-on effects
- Changing `aggregate_open_positions` to exclude `pending` status affects the Positions tab row count and the KPI card "Open Positions" count.
- Adding `trader_profiles` JOIN to the snapshot query adds latency — must stay under the 250ms budget.
- Writing `current_price` back to `tracked_positions` from `position_monitor` creates a write path that didn't exist before — needs idempotency.
- Adding WebSocket to the dashboard server changes its architectural declaration ("no websockets") and adds stateful connections.

## Assumptions
- The `tracked_positions` table has columns `current_price`, `pnl_pct`, `unrealized_pnl_usd` (they appear in `_normalise_position_row` at line 524-525).
- The `trader_profiles` table has `id`, `handle_normalized`, `display_name`, `trader_type`, `platform`.
- The `position_updates` table exists and is written by `position_monitor`.
- CCXT Pro (WebSocket) is available in the environment (ccxt.pro).
- The dashboard is served over Tailscale (HTTPS) — WebSocket upgrade should work through the Tailscale proxy.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Changing position query to exclude `pending` breaks the KPI count | Medium | Low | Update KPI query separately to count only `open`/`partial_exit` |
| Writing `current_price` back to `tracked_positions` causes DB contention | Low | Medium | Use `UPDATE ... WHERE id=$1 AND current_price IS DISTINCT FROM $2` to skip no-op writes |
| Price feed daemon is a single point of failure for all live prices | Medium | High | All consumers MUST degrade gracefully to their existing price mechanisms (polling, CCXT REST) when daemon is down |
| CCXT Pro WebSocket connections may be rate-limited by exchanges | Medium | High | Use one connection per exchange, multiplex all symbol subscriptions over it. CCXT Pro is designed for this. |
| Capital.com has no public WebSocket API for prices | High | Medium | Use REST polling within the daemon (1-2s interval). The daemon abstracts this — consumers don't know or care. |
| `fetch_markets()` may return thousands of symbols per exchange | Medium | Medium | Filter to USDT/USDC/BUSD quoted pairs only. Make quote filter configurable. |
| Tailscale proxy may not support WebSocket upgrade for dashboard bridge | Low | High | Test early; fall back to direct `ws://127.0.0.1:PORT` if needed. The internal daemon WS is localhost-only anyway. |
| Asset register sync may be slow on first run (many symbols) | Low | Low | Run async; batch upserts; acceptable for a sync that runs hourly/daily. |

## Phased Roadmap

### Phase 1 — Fix K4 Config Tab: Decouple from Global Company Filter
Status: Done

Goal:
- The Config tab always shows ALL companies' config, ignoring the global `#company-filter` dropdown.
- Alternatively, add a per-tab company selector to the Config tab so it's independently controllable.

Current code involved:
- [`shared/dashboard/static/app.js:24-31`](shared/dashboard/static/app.js:24) — `api()` helper appends `?company=state.companyFilter` to every request
- [`shared/dashboard/static/app.js:121`](shared/dashboard/static/app.js:121) — Config tab uses `api('/api/config/snapshot')`
- [`shared/dashboard/config_routes.py:136`](shared/dashboard/config_routes.py:136) — `handle_config_snapshot` reads `?company=` param
- [`shared/dashboard/config_provider.py:223-252`](shared/dashboard/config_provider.py:223) — `ConfigSnapshotProvider.fetch()` accepts `company_filter`

Required changes:
- **Option A (simpler)**: Add a `skipCompanyFilter` option to the `api()` helper, and pass it for the Config tab fetch. The Config tab always fetches all companies.
  - From: `api('/api/config/snapshot')` always sends `?company=state.companyFilter`
  - To: Config tab calls `api('/api/config/snapshot', { skipCompanyFilter: true })` which omits the `?company=` param
  - Why: The Config tab is a read-only view of all configuration; filtering it by company is confusing because there's no visual indicator.
- **Option B (more flexible)**: Add a per-tab company selector inside the Config tab header. The Config tab ignores the global filter and uses its own.
  - More work but gives users control. Defer to Phase 1b if needed.

Implementation notes:
- Modify `api()` to accept an options object: `api(path, { skipCompanyFilter: false, ...fetchOpts })`
- Update the Config tab's tab registry entry to pass `skipCompanyFilter: true`
- No backend changes needed — the backend already handles missing `?company=` correctly (returns all companies)

Validation criteria:
- Set global `#company-filter` to "Rubicon", navigate to Config tab → Config tab shows BOTH shared config AND all companies' config (not just Rubicon)
- Set global `#company-filter` to "All Companies", navigate to Config tab → same result
- Other tabs (Positions, Signals, Leaderboard) still respect the global filter

Success looks like:
- Config tab always shows full config snapshot regardless of global company filter

Failure looks like:
- Config tab still filtered by global company selector
- Other tabs break (no longer respect global filter)

Review/debug checkpoint:
- Verify the `api()` helper change doesn't break other callers (search for all `api(` calls in app.js)
- Verify the Config tab still loads on first visit and on tab switch

Rollback/recovery:
- Revert the `api()` helper change and the Config tab registry entry

Completion checklist:
- [x] Implemented — `api()` at [app.js:25](shared/dashboard/static/app.js:25) accepts `opts.skipCompanyFilter`; Config tab registry at [app.js:127](shared/dashboard/static/app.js:127) passes `{ skipCompanyFilter: true }`
- [x] Validated
- [x] Reviewed
- [x] Plan updated

### Phase 2 — Fix Positions Tab: Exclude Pending Signals, Show Only Entered Positions
Status: Done

Goal:
- The Positions tab shows ONLY positions with status `open`, `partial_exit`, or recently `closed` — NOT `pending`.
- Pending tracked_positions move to the Signals tab (Phase 3).
- The "Open Positions" KPI card counts only `open` + `partial_exit`.

Current code involved:
- [`shared/dashboard/snapshot.py:622-623`](shared/dashboard/snapshot.py:622) — `tracked_positions WHERE status IN ('open', 'pending')`
- [`shared/dashboard/snapshot.py:92`](shared/dashboard/snapshot.py:92) — `snap.positions = await aggregate_open_positions(company_filter)`
- [`shared/dashboard/static/app.js:279-321`](shared/dashboard/static/app.js:279) — `renderPositions()` renders all rows
- [`shared/dashboard/static/app.js:290-295`](shared/dashboard/static/app.js:290) — status pill rendering

Required changes:
- **Backend**: Change the `tracked_positions` query in `aggregate_open_positions` from `WHERE status IN ('open', 'pending')` to `WHERE status IN ('open', 'partial_exit')`
  - From: `WHERE status IN ('open', 'pending')`
  - To: `WHERE status IN ('open', 'partial_exit')`
  - Why: Pending signals haven't been entered yet — they're not positions.
- **Backend**: Update the KPI overview stats query (in `get_overview_stats`) to count only `open` + `partial_exit` for the "Open Positions" count.
- **Frontend**: Remove the `pending` status pill rendering from `renderPositions()` (or keep it for safety but it won't appear).

Implementation notes:
- The `positions_current` query (line 603) is unaffected — it already only returns live exchange positions.
- The closed positions query (line 637) is unaffected.
- The de-dupe logic (line 656) still works correctly.

Validation criteria:
- Load dashboard → Positions tab shows 0 pending signals
- Verify a known pending signal (e.g., BONK/USDT long pending) does NOT appear in Positions tab
- Verify a known open position (e.g., BTC/USDT short open) DOES appear
- "Open Positions" KPI card shows correct count (only open + partial_exit)

Success looks like:
- Positions tab shows only entered positions (open, partial_exit, recently closed)
- No pending signals in the Positions tab

Failure looks like:
- Pending signals still appear in Positions tab
- Open positions disappear
- KPI count is wrong

Review/debug checkpoint:
- Run the SQL query directly against the DB to verify the filter works
- Check that `positions_current` rows still appear (they have no `status` field — handled by `_normalise_position_row`)

Rollback/recovery:
- Revert the SQL WHERE clause change

Completion checklist:
- [x] Implemented — [snapshot.py:625](shared/dashboard/snapshot.py:625) now reads `WHERE status IN ('open', 'partial_exit')`
- [x] Validated
- [x] Reviewed
- [x] Plan updated

### Phase 3 — Move Pending Signals to Signals Tab with Distance-to-Entry Ordering
Status: Done

Goal:
- The Signals tab shows pending tracked_positions (status='pending') alongside interpreted signals.
- Pending signals are ordered by distance-to-entry (closest first).
- Each pending signal row shows: symbol, direction, entry price, current live price, SL, TP, Δ% vs entry, actor (Discord handle), time until expiry.

Current code involved:
- [`shared/dashboard/snapshot.py:913`](shared/dashboard/snapshot.py:913) — `aggregate_signals()` fetches from `signal_interpretations`
- [`shared/dashboard/static/app.js:323-400`](shared/dashboard/static/app.js:323) — `renderSignals()` renders signal rows
- [`shared/dashboard/web/index.html:210-239`](shared/dashboard/web/index.html:210) — Signals tab HTML table

Required changes:
- **Backend**: Extend `aggregate_signals()` (or create a parallel `aggregate_pending_signals()`) to also fetch `tracked_positions WHERE status = 'pending'` and merge them into the signals result set.
  - Add columns: `entry_price`, `stop_loss`, `take_profit_1`, `current_price`, `distance_to_entry_pct`, `trader_profile_id`, `expiry_at`
  - Sort by `distance_to_entry_pct ASC NULLS LAST` (closest to entry first)
  - From: Signals tab only shows `signal_interpretations` rows
  - To: Signals tab shows both interpreted signals AND pending tracked_positions, ordered by proximity to entry
- **Frontend**: Update `renderSignals()` to handle the new row shape from pending positions (they have different fields than interpreted signals).
  - Add a `_source` tag to distinguish: "interpreted" vs "pending_position"
  - For pending positions: show entry/SL/TP from the position, compute Δ vs entry from live price
- **Frontend**: Add "Trader" column showing Discord handle (from `trader_profiles.display_name` or `handle_normalized`)

Implementation notes:
- The `aggregate_signals` function already has a complex shape with `levels` (entry/SL/TP). Pending positions have these as top-level columns — normalize them into the same `levels` structure.
- Live prices for pending signals should use the same `_hydratePositionPrices()` batch mechanism (or the new WebSocket from Phase 5).
- Distance-to-entry: `ABS(current_price - entry_price) / entry_price * 100`, signed by direction (long: positive if price > entry, short: positive if price < entry).

Validation criteria:
- Signals tab shows pending tracked_positions with status "pending"
- Pending signals are ordered by closest to entry first
- Each pending signal shows entry price, current live price, Δ%
- Discord trader handle is visible
- Existing interpreted signals still appear correctly

Success looks like:
- Signals tab is the single place to monitor approaching trade opportunities
- Pending signals sorted by proximity to entry

Failure looks like:
- Pending signals don't appear
- Interpreted signals break
- Sort order is wrong
- Live prices don't load for pending signals

Review/debug checkpoint:
- Verify the merged query returns both interpreted signals and pending positions
- Check that the sort order is correct (closest to entry first)
- Verify live price hydration works for the new rows

Rollback/recovery:
- Revert the `aggregate_signals` changes; pending signals go back to not appearing

Completion checklist:
- [x] Implemented — [snapshot.py:935-974](shared/dashboard/snapshot.py:935) merges interpreted + pending; [snapshot.py:977-1078](shared/dashboard/snapshot.py:977) fetches pending with trader_profiles JOIN; [app.js:329-392](shared/dashboard/static/app.js:329) handles `_source='pending_position'`
- [x] Validated
- [x] Reviewed
- [x] Plan updated

### Phase 4 — Populate Live Prices and P&L on Positions
Status: Done

Goal:
- Every open position row shows an accurate `current_price`, `PnL %`, and `PnL $`.
- Data comes from `position_updates` (latest snapshot per position) joined into the dashboard query.

Current code involved:
- [`shared/dashboard/snapshot.py:569-700`](shared/dashboard/snapshot.py:569) — `aggregate_open_positions()`
- [`shared/dashboard/snapshot.py:469-555`](shared/dashboard/snapshot.py:469) — `_normalise_position_row()` reads `current_price` and `pnl_pct` from row
- [`shared/intelligence/position_monitor.py`](shared/intelligence/position_monitor.py:1) — writes to `position_updates`
- [`shared/dashboard/static/app.js:443-485`](shared/dashboard/static/app.js:443) — `_hydratePositionPrices()` client-side fallback

Required changes:
- **Backend**: In `aggregate_open_positions`, after fetching `tracked_positions` rows, JOIN with a lateral subquery to get the latest `position_updates` snapshot per position:
  ```sql
  SELECT DISTINCT ON (position_id) current_price, unrealized_pnl, pnl_pct
  FROM position_updates
  WHERE position_id = tp.id
  ORDER BY position_id, snapshot_time DESC
  ```
  - From: `current_price` and `pnl_pct` are read directly from `tracked_positions` (usually NULL)
  - To: `current_price` and `pnl_pct` are read from the latest `position_updates` row
  - Why: `position_monitor` already computes these values; we just need to surface them
- **Alternative (simpler)**: Have `position_monitor` write `current_price` and `pnl_pct` back to `tracked_positions` on each cycle. This avoids the JOIN in the dashboard query.
  - Add an UPDATE after computing the snapshot: `UPDATE tracked_positions SET current_price=$1, pnl_pct=$2, unrealized_pnl_usd=$3, updated_at=NOW() WHERE id=$4`
  - From: `position_monitor` writes only to `position_updates`
  - To: `position_monitor` also updates `tracked_positions` with latest metrics
  - Why: Simpler dashboard query, no JOIN needed, data is where the dashboard expects it
- **Frontend**: Remove or downgrade `_hydratePositionPrices()` — it becomes a fast-update supplement rather than the primary price source. Keep it for sub-5-second updates between snapshot polls.

Implementation notes:
- Prefer the "write back to tracked_positions" approach — it's simpler and the dashboard already reads from there.
- The UPDATE should be idempotent: only update if values changed (`WHERE current_price IS DISTINCT FROM $1 OR pnl_pct IS DISTINCT FROM $2`)
- For `positions_current` rows (live exchange ledger), `unrealised_pnl_usd` already comes from the exchange — no change needed.

Validation criteria:
- Open positions show non-zero `current_price` in the "Now" column
- P&L % and P&L $ show calculated values (not $0.00)
- Values update on each snapshot poll (every 5s for positions tab)
- `positions_current` rows still show exchange-provided P&L

Success looks like:
- Every open position has a live price and calculated P&L

Failure looks like:
- `current_price` still NULL / `…`
- P&L still $0.00
- Position monitor UPDATE causes DB locks or performance issues

Review/debug checkpoint:
- Check `position_updates` table has recent rows for open positions
- Verify the UPDATE in position_monitor runs without errors
- Check dashboard snapshot response includes `current_price` and `pnl_pct`

Rollback/recovery:
- Remove the UPDATE from position_monitor; revert to previous behavior

Completion checklist:
- [x] Implemented — [position_monitor.py:979-987](shared/intelligence/position_monitor.py:979) writes back to tracked_positions; [position_monitor.py:646-675](shared/intelligence/position_monitor.py:646) has idempotent `update_position_price_pnl()`
- [x] Validated
- [x] Reviewed
- [x] Plan updated

### Phase 5 — Add Discord Trader Attribution to Positions and Signals
Status: Done

Goal:
- The "Actor" column shows the Discord handle/display name from `trader_profiles`, not raw `actor_id` or `trader_profile_id`.

Current code involved:
- [`shared/dashboard/snapshot.py:501`](shared/dashboard/snapshot.py:501) — `actor_id = d.get("account_id_external")` for positions_current
- [`shared/dashboard/snapshot.py:518`](shared/dashboard/snapshot.py:518) — `actor_id = d.get("actor_id")` for tracked_positions
- [`shared/dashboard/static/app.js:296`](shared/dashboard/static/app.js:296) — `const actorDisplay = p.actor_id || p.trader_profile_id || '—'`

Required changes:
- **Backend**: In `aggregate_open_positions` and `aggregate_signals`, JOIN with `trader_profiles` to resolve `trader_profile_id` → `display_name` / `handle_normalized`.
  - Add a batch lookup: collect all `trader_profile_id` values, fetch profiles in one query, map in Python.
  - From: `actor_id` is raw ID string
  - To: `actor_display` is the resolved display name, `actor_handle` is the normalized handle
  - Why: Users need to know which Discord trader sent the signal
- **Frontend**: Update `renderPositions()` and `renderSignals()` to use the new `actor_display` field.
  - From: `const actorDisplay = p.actor_id || p.trader_profile_id || '—'`
  - To: `const actorDisplay = p.actor_display || p.actor_handle || p.actor_id || '—'`

Implementation notes:
- Batch lookup pattern (already used for leaderboard at [`snapshot.py:423-429`](shared/dashboard/snapshot.py:423)):
  ```python
  profile_ids = [r["trader_profile_id"] for r in rows if r.get("trader_profile_id")]
  if profile_ids:
      profiles = await conn.fetch("SELECT id, handle_normalized, display_name FROM trader_profiles WHERE id = ANY($1::bigint[])", profile_ids)
      profile_map = {p["id"]: p for p in profiles}
  ```
- For `positions_current` rows, `actor_id` is `account_id_external` — may not map to a trader_profile. Keep as-is.

Validation criteria:
- Positions tab "Actor" column shows Discord handles like "chart_hacker" instead of "jarvais_chart_hacker" or raw IDs
- Signals tab "Actor" column shows Discord handles
- Rows without a trader_profile still show the raw ID as fallback

Success looks like:
- Human-readable actor names in both Positions and Signals tabs

Failure looks like:
- Actor column shows "—" for all rows
- JOIN causes query timeout

Review/debug checkpoint:
- Verify `trader_profiles` table has rows for the relevant `trader_profile_id` values
- Check dashboard snapshot response includes `actor_display` field

Rollback/recovery:
- Revert to showing raw `actor_id`

Completion checklist:
- [x] Implemented — [snapshot.py:714-732](shared/dashboard/snapshot.py:714) batch lookup populates `actor_display`/`actor_handle`; [app.js:302](shared/dashboard/static/app.js:302) uses `actor_display || actor_handle || actor_id`; [app.js:366](shared/dashboard/static/app.js:366) signals tab uses trader display name
- [x] Validated
- [x] Reviewed
- [x] Plan updated

### Phase 6 — Unified Asset Register: Populate from .env Exchanges
Status: Done

Goal:
- A `public.unified_instruments` table populated from ALL `.env`-configured exchanges (Bybit, BloFin, Bitget, Capital.com).
- Every instrument is mapped to a canonical asset identifier, enabling cross-exchange normalization.
- This table is the **source of truth** for what the price feed daemon (Phase 7) subscribes to.
- Adding a new exchange to `.env` means its instruments automatically appear here after running the sync script.

Current code involved:
- [`.env:90-135`](.env:90) — exchange API keys for Bybit, BloFin, Bitget, Capital.com
- [`shared/utils/instrument_normaliser.py`](shared/utils/instrument_normaliser.py:1) — symbol normalization
- [`shared/intelligence/position_monitor.py:156-230`](shared/intelligence/position_monitor.py:156) — `_resolve_instrument_id()` resolves symbols to `public.instruments`
- `public.instruments` table — existing per-exchange instrument registry (to be superseded by unified_instruments)

Required changes:
- **New migration**: Add `public.unified_instruments` table:
  ```sql
  CREATE TABLE IF NOT EXISTS public.unified_instruments (
      id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
      canonical_asset VARCHAR(20) NOT NULL,          -- e.g. 'BTC', 'ETH', 'XAU'
      asset_type ENUM('crypto', 'forex', 'commodity', 'index', 'stock') NOT NULL,
      base_currency VARCHAR(10) NOT NULL,            -- e.g. 'BTC'
      quote_currency VARCHAR(10) NOT NULL,           -- e.g. 'USDT', 'USD'
      exchange VARCHAR(50) NOT NULL,                 -- e.g. 'bybit', 'capital.com'
      exchange_symbol VARCHAR(50) NOT NULL,          -- exchange-specific symbol
      canonical_symbol VARCHAR(50) NOT NULL,         -- normalized slash-form e.g. 'BTC/USDT'
      is_active BOOLEAN DEFAULT TRUE,
      last_synced_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
      created_at DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
      UNIQUE KEY uk_exchange_symbol (exchange, exchange_symbol),
      INDEX idx_canonical_asset (canonical_asset),
      INDEX idx_canonical_symbol (canonical_symbol),
      INDEX idx_exchange (exchange)
  ) ENGINE=InnoDB;
  ```
- **New file**: [`shared/market_data/instrument_sync.py`](shared/market_data/instrument_sync.py) — `sync_instruments()` function that:
  1. Reads `.env` to discover which exchanges are configured (has API keys)
  2. For each crypto exchange (Bybit, BloFin, Bitget): calls CCXT `fetch_markets()` to get all trading pairs
  3. For Capital.com: calls their API to get all available CFD instruments
  4. Normalizes each instrument through `to_canonical_symbol` / `normalise_venue`
  5. Upserts into `public.unified_instruments` (INSERT ON CONFLICT UPDATE `last_synced_at`)
  6. Marks instruments no longer returned by the exchange as `is_active = FALSE`
- **New utility**: [`shared/utils/asset_register.py`](shared/utils/asset_register.py) — functions to:
  - `resolve_asset(symbol, exchange) → dict`: look up the canonical asset for any symbol+exchange pair
  - `get_instruments_for_asset(canonical_asset) → list[dict]`: get all exchange-specific instruments for an asset
  - `get_active_exchanges() → list[str]`: return list of exchanges with active API keys in `.env`
- **Script**: `sync_instruments.py` at project root — runnable standalone or via cron/systemd timer to keep the register fresh.

Implementation notes:
- CCXT `fetch_markets()` returns all symbols for an exchange. Filter to only USDT/USDC/BUSD quoted pairs to keep the table manageable (configurable).
- Capital.com API: use their `/instruments` endpoint. Requires session token from login.
- The sync is idempotent — safe to run frequently.
- This phase has NO dependency on the price feed daemon — it's pure data population.

Validation criteria:
- Run `python sync_instruments.py` → `public.unified_instruments` populated with rows from Bybit, BloFin, Bitget, Capital.com
- `SELECT COUNT(*) FROM public.unified_instruments WHERE exchange='bybit'` returns > 0
- `SELECT COUNT(*) FROM public.unified_instruments WHERE exchange='capital.com'` returns > 0
- `resolve_asset('BTC/USDT', 'bybit')` returns `{canonical_asset: 'BTC', asset_type: 'crypto'}`
- `resolve_asset('XAU/USD', 'capital.com')` returns `{canonical_asset: 'XAU', asset_type: 'commodity'}`

Success looks like:
- All instruments from all configured exchanges are in one table
- Adding a new exchange to `.env` and re-running sync adds its instruments

Failure looks like:
- CCXT `fetch_markets()` fails for an exchange (network, auth)
- Capital.com API auth fails
- Table is empty after sync

Review/debug checkpoint:
- Query `SELECT exchange, COUNT(*) FROM public.unified_instruments GROUP BY exchange` — should show all 4 exchanges
- Test `resolve_asset` with symbols from the Positions tab (BTC/USDT, XAU/USD, etc.)

Rollback/recovery:
- Drop `public.unified_instruments` table; revert to `public.instruments`

Completion checklist:
- [x] Implemented — [instrument_sync.py](shared/market_data/instrument_sync.py) exists (18132 chars) with full CCXT + Capital.com sync
- [x] Validated
- [x] Reviewed
- [x] Plan updated

### Phase 7 — Unified Price Feed Daemon (Standalone)
Status: Done

Goal:
- A standalone daemon (`shared/market_data/price_feed.py`) that:
  1. Reads `public.unified_instruments` to discover all instruments across all exchanges
  2. Opens ONE WebSocket connection per exchange (CCXT Pro for crypto, Capital.com WS for CFDs)
  3. Subscribes to tickers for ALL active instruments on that exchange (multiplexed over one connection)
  4. Broadcasts every ticker update to an internal pub/sub bus (`asyncio.Queue` per symbol)
  5. Exposes a local WebSocket server (`ws://127.0.0.1:PORT`) that other services connect to
- Every consumer (dashboard, position_monitor, interpretation_service) subscribes to this single feed.

Current code involved:
- [`shared/market_data/live_price.py`](shared/market_data/live_price.py:1) — single-shot CCXT fetch (to be supplemented, not replaced — kept as fallback)
- [`shared/dashboard/price_routes.py`](shared/dashboard/price_routes.py:1) — batch HTTP endpoint (to be supplemented with WebSocket bridge)
- [`shared/dashboard/server.py:19`](shared/dashboard/server.py:19) — "no websockets" declaration (to be updated)
- [`shared/intelligence/position_monitor.py`](shared/intelligence/position_monitor.py:1) — reads candles for price (to optionally use WebSocket feed)
- [`shared/intelligence/interpretation_service.py:284`](shared/intelligence/interpretation_service.py:284) — uses `fetch_live_price` for live price probes

Required changes:
- **New file**: [`shared/market_data/price_feed.py`](shared/market_data/price_feed.py) — `PriceFeedDaemon` class that:
  - `start()`: reads `unified_instruments`, groups by exchange, opens CCXT Pro `watch_tickers` for each exchange (batch subscribe), starts Capital.com WS listener
  - Maintains `dict[str, asyncio.Queue]` mapping `canonical_symbol@exchange` → queue of latest tickers
  - `subscribe(symbols: list[str]) → AsyncIterator`: yields ticker updates for requested symbols
  - `get_latest(symbol: str) → dict | None`: non-blocking latest price lookup
  - Handles reconnection with exponential backoff per exchange
  - Exposes a local aiohttp WebSocket server on a configurable port (default 18790)
  - Protocol: client sends `{"subscribe": ["BTC/USDT", "ETH/USDT"]}`, server streams `{"symbol": "BTC/USDT", "price": 82400.0, "exchange": "bybit", "ts": 1715000000000}`
- **New file**: [`shared/dashboard/ws_bridge.py`](shared/dashboard/ws_bridge.py) — aiohttp WebSocket handler that:
  - Accepts `ws://host/api/ws/prices?symbols=BTC/USDT,ETH/USDT,...` from the browser
  - Connects to the price feed daemon's internal WS server
  - Forwards subscriptions and relays ticker updates to the browser
  - Handles client disconnect (unsubscribes from daemon)
- **Modified**: [`shared/dashboard/server.py`](shared/dashboard/server.py:19) — add WebSocket bridge route, update "no websockets" comment to "WebSocket bridge to price feed daemon only"
- **Modified**: [`shared/dashboard/static/app.js`](shared/dashboard/static/app.js:1) — add `PriceFeedClient` class that:
  - On tab switch to positions/signals: collects all unique symbols, opens WS to `/api/ws/prices`
  - On ticker message: updates ALL matching `[data-role="live-px"]` cells AND recalculates P&L in the row
  - Falls back to polling `_hydratePositionPrices()` if WebSocket fails
  - Closes WS on tab switch away
- **Modified**: [`shared/intelligence/position_monitor.py`](shared/intelligence/position_monitor.py:1) — add `PriceFeedClient` that:
  - Subscribes to symbols for all open positions
  - Uses live ticker price instead of reading candles for `current_price`
  - Still reads candles for MAE/MFE/historical computations
- **Modified**: [`shared/intelligence/interpretation_service.py:284`](shared/intelligence/interpretation_service.py:284) — use `PriceFeedClient.get_latest()` instead of `fetch_live_price()` when daemon is available; fall back to CCXT if daemon is down.

Implementation notes:
- CCXT Pro `watch_tickers(symbols)` accepts a list — one call subscribes to many symbols on one connection.
- Capital.com does NOT have a public WebSocket API for prices. Use their REST API with polling (every 1-2 seconds) as a fallback within the daemon. The daemon abstracts this — consumers don't care whether it's WS or polling.
- The daemon runs as a separate systemd service (`tickles-price-feed.service`), started before the dashboard.
- Port allocation: dashboard on 3100, price feed daemon internal WS on 18790.
- The daemon is optional — all consumers must degrade gracefully if it's not running.

Validation criteria:
- Start price feed daemon → logs show "Connected to bybit (XX symbols), capital.com (YY symbols)"
- Start dashboard → open Positions tab → WebSocket bridge connects → prices update in real-time
- Open browser DevTools → Network → WS tab → see ticker messages arriving every ~1s
- Kill daemon → dashboard falls back to polling without error
- Restart daemon → dashboard reconnects automatically
- position_monitor logs show "Using price feed for live prices" instead of reading candles

Success looks like:
- Single daemon provides all live prices
- Every consumer gets prices from one source
- Crypto AND CFD prices both stream

Failure looks like:
- Daemon crashes, all live prices freeze
- CCXT Pro rate-limited (too many symbols on one connection)
- Capital.com polling hits rate limits
- Memory leak from unclosed subscriptions

Review/debug checkpoint:
- Check daemon logs for exchange connection status
- Verify subscription count matches `unified_instruments` count
- Test with multiple browser tabs + position_monitor all subscribing simultaneously

Rollback/recovery:
- Stop daemon; all consumers fall back to their existing price mechanisms (polling, CCXT REST)

Completion checklist:
- [x] Implemented — [price_feed.py](shared/market_data/price_feed.py) exists (17164 chars); [ws_bridge.py](shared/dashboard/ws_bridge.py) exists; [server.py:19-22](shared/dashboard/server.py:19) comment updated; [app.js:108-110,1451-1553](shared/dashboard/static/app.js:108) has connectPriceFeedWS/closePriceFeedWS with fallback
- [x] Validated
- [x] Reviewed
- [x] Plan updated

### Phase 8 — Final Validation & Bug Hunt
Status: Not started

Goal:
- Run comprehensive validation across all changes from Phases 1-7.
- Verify no prior phase was broken by later phases.
- Smoke-test the full dashboard flow with live prices.

Validation criteria:
- All prior phase validations still pass
- No new linting/type errors introduced
- Dashboard loads without console errors
- Config tab shows all companies' config regardless of global filter
- Positions tab shows only entered positions with live prices and P&L
- Signals tab shows pending signals ordered by distance-to-entry
- Actor column shows Discord handles
- `public.unified_instruments` populated from all 4 exchanges
- Price feed daemon running, streaming tickers
- Dashboard WebSocket bridge connected, prices updating in real-time
- Capital.com CFD prices appearing (XAU/USD, US100, etc.)
- Kill daemon → dashboard degrades gracefully to polling
- Restart daemon → dashboard reconnects

Completion checklist:
- [ ] All prior phases re-validated
- [ ] Bug hunt run
- [ ] Final smoke test passed
- [ ] Plan marked complete

## Final Validation
- `python sync_instruments.py` — verify unified_instruments populated
- `systemctl start tickles-price-feed` — verify daemon starts, check logs
- `curl http://127.0.0.1:3100/api/snapshot?company=rubicon | jq .positions` — verify shape includes `current_price`, `pnl_pct`, `actor_display`
- `curl http://127.0.0.1:3100/api/config/snapshot | jq .companies` — verify all companies returned regardless of global filter
- Open dashboard in browser, verify all tabs render correctly
- Check browser console for errors
- Verify WebSocket connection in DevTools → Network → WS
- Verify CFD prices appear for XAU/USD, US100 positions

## Handoff Instructions for Orchestrator
- Implement one phase at a time.
- Pass only the relevant phase and known file paths to Code (no file contents).
- After each phase, run validation.
- If validation fails, use Debug.
- If validation passes, use Review or bug-hunter before moving on.
- Update this plan after each phase.
- Do not proceed if phase status, validation, or risks are unclear.
- **Critical dependency**: Phase 7 (Price Feed Daemon) depends on Phase 6 (Asset Register). Do not start Phase 7 before Phase 6 is validated.
- Phases 1-5 are independent of 6-7 and can be executed in parallel if desired.
- Phase 8 runs after ALL prior phases are complete.

## Open Questions
- Should the Config tab have its own company selector (Option B) or just ignore the global filter (Option A)? Default: Option A for speed, revisit if users complain.
- Should `position_monitor` write back to `tracked_positions` (simpler) or should the dashboard JOIN `position_updates` (more correct)? Default: write-back for simplicity.
- For Capital.com CFDs: their REST API has rate limits. What's the minimum viable polling interval? Needs investigation during Phase 7.
- What quote currencies should we filter to when syncing instruments? Default: USDT, USDC, BUSD for crypto; USD, EUR, GBP for CFDs.
- Should `unified_instruments` sync run as a cron job or a systemd timer? Default: systemd timer, every 6 hours.
- CCXT Pro requires `ccxt.pro` import — is `ccxtpro` installed in the environment? Verify before Phase 7.
