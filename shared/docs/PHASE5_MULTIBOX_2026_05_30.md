# Phase 5 — Multi-box trades, CH dedup, drawer legs (2026-05-30)

Plain-English map of what changed, why, and how to roll back.

## Problem we fixed

1. **INTERP 3370** — chart had two SHORT boxes; LLM invented a LONG. Drawer showed one consensus long for both positions.
2. **Replay API** — returned one position + wrong timeframe (`6h` chart → `1m` candles).
3. **Copy ChartHacker agent** — only mirrored `jarvais_chart_hacker` rows, not trader legs when CH agreed.
4. **Prompt** — no hard rule: N boxes → N `trader_trades`, no box → no trader leg.

## What we built

### A. Timeframes (`market_routes.py`)

- Added `3h`, `6h`, `12h` to allowed TFs.
- Aggregates from **1m** candles when native TF missing.
- **Rollback:** revert `_ALLOWED_TF`, `_AGGREGATE_TF`, `_TF_SECONDS` in `shared/dashboard/market_routes.py`.

### B. Multi-leg replay API (`/api/signal-replay`)

- Returns `legs[]` (trader + chart_hacker + orphan positions), `selected_leg`, `stats`.
- Each leg has its own `levels`, `timeframe`, `candles`, `position`.
- Query: `?leg=0` or `?position_id=14122`.
- Top-level `candles` / `position` / `signal.levels` = selected leg (backward compatible).
- **Verify:** `curl http://127.0.0.1:3101/api/signal-replay?id=3370` → `timeframe: 6h`, `legs: 3`.

### C. DB — `chart_hacker_endorsed`

- Migration: `shared/intelligence/migrations/2026_05_30_chart_hacker_endorsed.sql`
- Column on `tracked_positions`; index for endorsed rows.
- **Rollback:** `ALTER TABLE tracked_positions DROP COLUMN chart_hacker_endorsed;`

### D. Interpretation arming (`interpretation_service.py`)

- `_trades_agree()` — same direction + entry within 0.3%.
- Pass 1: arm trader legs; track `trader_armed`.
- Pass 2: if CH trade agrees → `UPDATE chart_hacker_endorsed = TRUE` on trader row, **no** duplicate CH position.
- If CH disagrees → separate `chart_hacker` position.
- `_is_explicit_trader_setup()` accepts chart `evidence` (`position_box`, etc.) without Discord text.
- Per-trade `symbol` override via `to_canonical_symbol()`.
- **Rollback:** revert Phase J dual-pass block (~line 4500) and remove `_trades_agree` / `_CHART_EVIDENCE`.

### E. Copy monitor (`copy_trade_monitor.py`)

- CH agent (`spot_seq_ch`) enters `jarvais_chart_hacker` **or** trader rows with `chart_hacker_endorsed = true`.
- **Rollback:** restore old `is_ch_pos = actor == jarvais_chart_hacker` check only.

### F. Dashboard drawer (`app.js`, `app.css`)

- Leg tabs: TR/CH, direction, entry, armed (grey dashed) vs endorsed (✓).
- Cache buster: `app.js?v=29-multi-leg`.
- **Rollback:** revert `drawReplay`, `mergeReplayLeg`, leg tab helpers; set cache buster back.

### G. Prompt v8 (`prompt_versions`)

- `2026.05.30-discord-semantic-v8` and `2026.05.30-telegram-rose-semantic-v8`.
- Insert script: `shared/scripts/insert_prompt_v8_multibox.py`.
- Newest DB prompt auto-loads for new interpretations (unless trader has pinned `prompt_id`).
- **Rollback:** `DELETE FROM prompt_versions WHERE version LIKE '2026.05.30-%v8';`

## Services to restart after deploy

```bash
systemctl restart tickles-dashboard tickles-interpretation tickles-copy-trade-monitor
```

## Prompt v9 (`2026.05.30-discord-semantic-v9`)

Path/projection boxes (orange route into a short entry) are **not** `trader_trades`.
Insert: `shared/scripts/insert_prompt_v9_path_boxes.py`

**Verified on INTERP 3370:** 2 trader shorts @ 75213 + 77565, **0 trader longs**.
Persisted 2026-05-30 via `shared/scripts/persist_reinterpret_rearm.py --id 3370`.

**Rollback:** `DELETE FROM prompt_versions WHERE version LIKE '2026.05.30-%v9';`

## MCP — `intelligence.reinterpret`

Re-read a chart with the **production** DB prompt (`prompt_versions`) and return the
**full Lens schema** (not the simplified `chart_analyze` file prompt).

```json
{
  "mediaId": 6442,
  "promptVersion": "2026.05.30-discord-semantic-v8",
  "includeQuant": true
}
```

Returns: `trader_trades[]`, `chart_hacker_trades[]`, `chart_analysis`, `setup_state`,
`instrument`, `timeframe`, market views, sentiments, `ai_agreement_with_trader`,
`reasoning`, full `parsed` object, `legacy`, optional `quant` + `consensus`, `meta`.
Does **not** persist by default.


- Re-processing INTERP 3370 (historical row stays wrong until re-run or manual fix).
- Recall measurement wiring (`record_recall`) — still Phase B.
- MemU postmortem broadcast — still Phase A.
