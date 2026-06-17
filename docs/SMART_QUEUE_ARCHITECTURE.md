# Smart Order Queue — Architecture & Phased Implementation Plan

> **Author:** Architecture Synthesis Agent
> **Date:** 2026-06-17
> **Workspace:** /opt/tickles
> **Status:** READ-ONLY Design Document (no code changes)

---

## 0. Executive Summary

The Smart Order Queue transforms the current "firehose" demo bridge (every signal is
immediately mirrored as a limit order) into a gated, merge-aware, rate-controlled
order pipeline. A new daemon `smart_queue.py` sits between `tracked_positions` and
`demo_bridge.py`, holding signals in an off-exchange queue and releasing them only
when predefined criteria are met (proximity gate, anti-flood throttle, merge windows).

Phase 1 is the **minimum viable off-exchange queue** with proximity gate — no merge,
no MCP migration. It can run alongside the existing bridge, gating it via a new
`queue_status` column on `tracked_positions`.

---

## 1. Should the smart queue be a NEW daemon or integrated?

### Recommendation: **NEW daemon** (`shared/daemons/smart_queue.py`)

**Rationale:**

| Factor | New Daemon | Integrate into existing |
|--------|-----------|------------------------|
| Code isolation | Queue logic fully independent; no risk of breaking bridge | Changes entangled with bridge's 1100-line tick loop |
| Phased rollout | Phase 1 can gate bridge via DB column; bridge unaware | Must modify bridge tick loop immediately |
| Rollback safety | Stop smart_queue process; bridge reverts to firehose | Must revert code + restart; higher risk |
| Testability | Standalone test suite (mock tracked_positions + candles) | Tests coupled to bridge's exchange mocks |
| Future merge logic | Natural home for batch/merge/cooldown logic | Bridge already 1100 lines; adding merge layer makes it unwieldy |
| MCP migration (Phase 3-4) | Queue daemon owns MCP tool calls natively | Bridge would need major restructure to add MCP dispatch |

**The queue daemon sits BETWEEN signal arrival and order placement:**

```
tracked_positions (status='pending')
        │
        ▼
   smart_queue.py          ← NEW: proximity gate, batch, merge, rate-limit
        │
        ▼ (sets queue_status='released')
   demo_bridge.py          ← EXISTING: mirrors released signals only
        │
        ▼
   CCXT exchange (limit orders)
```

---

## 2. Phase 1 — Minimum Viable Deliverable

### Scope: Off-exchange queue + proximity gate (no merge, no MCP migration)

**What it does:**
1. Polls `tracked_positions` for rows where `status='pending'` AND `queue_status IS NULL`
2. Computes distance to entry using `PriceFeedDaemon.get_latest(symbol)` (zero-cost: no API call, reads in-memory WebSocket cache)
3. If distance <= `QUEUE_PROXIMITY_THRESHOLD` (default 1.0%):
   - Sets `queue_status = 'released'`
   - Sets `queue_released_at = NOW()`
4. If distance > threshold:
   - Leaves signal in queue (queue_status stays NULL or 'queued')
   - Re-evaluates on next tick
5. Optional: sets `queue_status = 'expired'` if signal_timestamp > 7 days ago

**What bridge changes are needed for Phase 1:**
- **ONE LINE CHANGE** in `demo_bridge._get_new_signals()`: add `AND (tp.queue_status = 'released' OR tp.queue_status IS NULL)` to the WHERE clause.
  - `IS NULL` ensures backward compatibility: rows written before the migration still flow through.
- Bridge ignores `queue_status='queued'` rows entirely.

**Phase 1 configuration (env vars):**
```
SMART_QUEUE_POLL_S=15
SMART_QUEUE_PROXIMITY_THRESHOLD=0.01    # 1% default
SMART_QUEUE_MAX_AGE_HOURS=168           # 7 days — expire ancient signals
```

**Files created:**
- `shared/daemons/smart_queue.py` (~250 lines)
- `shared/migration/2026_06_17_smart_queue_columns.sql`
- `shared/migration/2026_06_17_smart_queue_columns_ROLLBACK.sql`
- `shared/tests/test_smart_queue.py`

**Files modified:**
- `shared/daemons/demo_bridge.py` — ONE LINE in `_get_new_signals()`
- `shared/services/registry.py` — register `smart-queue` service descriptor

**Feature gate:** Controlled entirely by whether the `tickles-smart-queue.service` systemd unit is running. Bridge operates normally without it (all signals flow through due to `queue_status IS NULL` fallback).

---

## 3. Phase Ordering: 1 → 2 → 3 → 4

### Phase 1 — Off-Exchange Queue + Proximity Gate (NOW)
- New `smart_queue.py` daemon
- DB columns: `queue_status`, `queue_released_at`, `queue_expired_at`
- Proximity gate using PriceFeedDaemon cache (`get_latest()`)
- One-line bridge change for release gating
- Feature gate: systemd unit start/stop

### Phase 2 — Merge + Anti-Flood
- Signal deduplication within configurable merge windows
- Same symbol + same direction + overlapping entry zones → merge into one
- Configurable cooldown per symbol (e.g., "only 1 signal per BTC/USDT per 30 min")
- Queue priority scoring (closest to entry gets released first)
- New columns: `merge_group_id`, `queue_priority_score`
- Bridge becomes merge-aware (one merged release → one order)
- Dashboard columns for queue depth, merge stats

### Phase 3 — MCP Migration (Batch Operations)
- Replace bridge's individual CCXT calls with batch MCP tools
- New MCP tools:
  1. `fetchPositions` — batch position fetch across accounts
  2. `getMarketLimits` — batch market info
  3. `setLeverage` — batch leverage presets
  4. `configureAccount` — position mode, margin mode
  5. `modifySL` — batch SL modifications
  6. `fetchOrdersBatch` — batch order status checks
- Queue daemon coordinates batch windows
- Bridge simplified to "fire released signals through MCP"

### Phase 4 — Full Automation
- Queue auto-tuning (learns optimal proximity threshold from fill-rate data)
- Dynamic merge windows based on volatility
- Queue analytics dashboard (fill rate, avg wait time, slippage distribution)
- Kill-switch integration with crash-protection guardrails

---

## 4. What Existing Code Can Be Reused vs Must Be Rewritten

### Reused (zero or minor changes):

| Module | Reuse Strategy |
|--------|---------------|
| `shared/market_data/price_feed.py` | `PriceFeedDaemon.get_latest(symbol)` — already provides in-memory price cache. Zero changes. |
| `shared/intelligence/position_monitor.py` | `_entry_touched()` and `_distance_to_entry()` — same math used for proximity gate. Extract to shared util OR reimplement (1-liners). |
| `shared/utils/db.py` / `DatabasePool` | Already used by every daemon; reuse for queue reads/writes. |
| `shared/daemons/demo_bridge.py` | Reuse entirely — only gated with one WHERE clause addition. Bridge keeps all its reconciliation, sizing, exchange logic. |
| `shared/intelligence/copy_sizing_config.py` | Reused for queue cooldown/threshold config (Phase 2). |
| `shared/services/registry.py` | Reused to register smart-queue service descriptor. |
| `shared/execution/protocol.py` | Reused for ExecutionIntent (Phase 3 MCP batch). |
| `shared/execution/ccxt_adapter.py` | Reused by bridge unchanged; Phase 3 MCP layer wraps it. |

### New code required:

| Module | Lines (est.) | Purpose |
|--------|-------------|---------|
| `shared/daemons/smart_queue.py` | ~250 (Phase 1), ~500 (Phase 2+) | Queue daemon: poll, gate, release |
| `shared/daemons/queue_config.py` | ~100 | Typed config loader (env → DB → defaults) |
| `shared/mcp/tools/queue.py` | ~200 (Phase 3) | 6 MCP tools for batch operations |
| `shared/tests/test_smart_queue.py` | ~250 | Unit tests for gating logic |

---

## 5. DB Migrations Needed

### Phase 1 (2026_06_17_smart_queue_columns.sql):

```sql
-- Add queue lifecycle columns to tracked_positions
ALTER TABLE public.tracked_positions
  ADD COLUMN IF NOT EXISTS queue_status VARCHAR(16) DEFAULT NULL;
  -- Values: NULL (not queued), 'queued', 'released', 'expired', 'skipped'

ALTER TABLE public.tracked_positions
  ADD COLUMN IF NOT EXISTS queue_released_at TIMESTAMPTZ(3) DEFAULT NULL;

ALTER TABLE public.tracked_positions
  ADD COLUMN IF NOT EXISTS queue_expired_at TIMESTAMPTZ(3) DEFAULT NULL;

ALTER TABLE public.tracked_positions
  ADD COLUMN IF NOT EXISTS queue_distance_pct NUMERIC(10,4) DEFAULT NULL;
  -- Snapshot of distance-to-entry at time of last queue evaluation

-- Index for queue polling (the hot query)
CREATE INDEX IF NOT EXISTS idx_tp_queue_status
  ON public.tracked_positions (queue_status, signal_timestamp)
  WHERE status = 'pending';

-- Index for release timestamp (dashboard queries)
CREATE INDEX IF NOT EXISTS idx_tp_queue_released
  ON public.tracked_positions (queue_released_at)
  WHERE queue_status = 'released';
```

### Phase 2 (future):

```sql
ALTER TABLE public.tracked_positions
  ADD COLUMN IF NOT EXISTS merge_group_id BIGINT DEFAULT NULL;

ALTER TABLE public.tracked_positions
  ADD COLUMN IF NOT EXISTS queue_priority_score NUMERIC(10,6) DEFAULT NULL;

-- Merge group lookup table
CREATE TABLE IF NOT EXISTS public.queue_merge_groups (
  id BIGSERIAL PRIMARY KEY,
  symbol VARCHAR(50) NOT NULL,
  direction VARCHAR(8) NOT NULL,
  representative_tp_id BIGINT NOT NULL,
  merged_count INT NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ(3) NOT NULL DEFAULT NOW()
);
```

### Rollback (2026_06_17_smart_queue_columns_ROLLBACK.sql):

```sql
DROP INDEX IF EXISTS idx_tp_queue_status;
DROP INDEX IF EXISTS idx_tp_queue_released;
ALTER TABLE public.tracked_positions
  DROP COLUMN IF EXISTS queue_status,
  DROP COLUMN IF EXISTS queue_released_at,
  DROP COLUMN IF EXISTS queue_expired_at,
  DROP COLUMN IF EXISTS queue_distance_pct;
```

---

## 6. How to Test Phase 1 Without Breaking the Existing Bridge

### Strategy: Feature-gated by queue_status IS NULL fallback

The bridge change is:
```python
# OLD (demo_bridge._get_new_signals):
WHERE tp.status IN ('pending', 'open')
  AND tp.entry_price > 0
  AND tp.id != ALL($1::bigint[])

# NEW:
WHERE tp.status IN ('pending', 'open')
  AND tp.entry_price > 0
  AND tp.id != ALL($1::bigint[])
  AND (tp.queue_status = 'released' OR tp.queue_status IS NULL)
```

The `queue_status IS NULL` clause means:
- **Before migration runs**: all rows have `queue_status = NULL` → bridge operates EXACTLY as today → zero impact
- **After migration, before queue daemon starts**: new rows get `queue_status = 'queued'` (set by queue daemon), old rows still NULL → bridge only processes old rows → natural drain
- **Queue daemon running**: new rows get evaluated → only released rows flow → bridge gated

### Test Plan:

**Stage 1 — Dry run (no bridge impact)**
1. Deploy migration only (`ALTER TABLE ADD COLUMN` — all NULLs)
2. Verify bridge processes all signals as before (the `IS NULL` path)
3. Verify no SQL errors, no bridge restart needed

**Stage 2 — Queue daemon in OBSERVE mode**
1. Start `smart_queue.py` with `SMART_QUEUE_DRY_RUN=true`
2. Daemon evaluates proximity but does NOT set `queue_status`
3. Logs: "WOULD release signal #123 (distance=0.3%)" / "WOULD queue signal #456 (distance=4.7%)"
4. Run for 24h, observe logs, confirm gating logic is correct

**Stage 3 — Partial rollout on one agent**
1. Set `queue_status='released'` for 1 demo account's agent
2. Other agents continue firehose (bridge `IS NULL` path)
3. Compare fill rates, slippage between gated and ungated
4. Run for 48h minimum

**Stage 4 — Full rollout**
1. Start smart_queue with write enabled
2. Monitor dashboard for queue depth, release rate
3. Keep rollback path open (stop daemon → NULL path re-activates)

---

## 7. Rollback Plan

### If queue performs worse (lower fill rate, higher slippage, missed entries):

**Immediate (seconds):**
```bash
systemctl stop tickles-smart-queue
```
The bridge's `queue_status IS NULL` fallback means ALL signals flow through again.
No code deploy needed. Downtime: < 5 seconds for the daemon SIGTERM.

**Short-term (minutes):**
```bash
# Verify bridge is processing all signals again
grep "new signals to mirror" /var/log/tickles/demo-bridge.log
# Should return to pre-queue volume immediately
```

**Medium-term (hours):**
1. Run SQL to reset all `queue_status` to NULL for stuck signals:
   ```sql
   UPDATE public.tracked_positions
   SET queue_status = NULL, queue_released_at = NULL
   WHERE queue_status = 'queued'
     AND status = 'pending'
     AND signal_timestamp > NOW() - INTERVAL '7 days';
   ```
2. These signals immediately flow through bridge on next tick.

**Permanent rollback:**
1. Keep smart-queue service disabled in systemd
2. Optionally run rollback migration (drops columns) — NOT required; NULL columns have zero impact
3. Remove one-line bridge change (could also leave it — `IS NULL` path is transparent)

**Key safety property:** The queue NEVER blocks a signal permanently. The
`queue_status IS NULL` fallback + timeout-based expiry ensure no signal is
orphaned. Worst case: a signal waits in queue until expiry, then gets
released. This is strictly better than never getting released.

---

## 8. File List Summary

### Phase 1 (this initiative):

**New files:**
```
shared/daemons/smart_queue.py                  # Queue daemon (~250 lines)
shared/daemons/queue_config.py                 # Typed config (~100 lines)
shared/migration/2026_06_17_smart_queue_columns.sql
shared/migration/2026_06_17_smart_queue_columns_ROLLBACK.sql
shared/tests/test_smart_queue.py               # Unit tests (~250 lines)
docs/SMART_QUEUE_ARCHITECTURE.md               # This document
```

**Modified files:**
```
shared/daemons/demo_bridge.py                  # +1 line in _get_new_signals()
shared/services/registry.py                    # +1 ServiceDescriptor for smart-queue
```

### Phase 2 (future):

```
shared/daemons/smart_queue.py                  # +merge logic (~+250 lines)
shared/migration/2026_XX_XX_queue_merge.sql
shared/daemons/merge_engine.py                 # Signal dedup engine
```

### Phase 3 (future):

```
shared/mcp/tools/queue.py                      # 6 batch MCP tools
shared/daemons/demo_bridge.py                  # +MCP dispatch mode
```

---

## 9. Proximity Gate Design Detail

### Cost Model

| Method | Cost | Latency | Freshness |
|--------|------|---------|-----------|
| PriceFeedDaemon.get_latest() | **Zero** (in-memory dict read) | < 1 µs | Real-time (WebSocket tick) |
| Exchange REST ticker | 1 API call per symbol | ~200 ms | Snapshot |
| DB candles table | 1 SQL query per symbol | ~5 ms | Up to 60s stale (1m candles) |

**Phase 1 uses PriceFeedDaemon cache exclusively.** The price_feed daemon is
already running on port 18790, updating `self._latest[symbol]` on every tick.
The queue daemon imports and calls `feed.get_latest(symbol)` which reads a
Python dict — zero cost, sub-microsecond.

### Distance Calculation

```python
def _distance_to_entry(symbol: str, entry: float, feed: PriceFeedDaemon) -> float:
    ticker = feed.get_latest(symbol)
    if ticker is None or ticker.get("price") is None:
        return 999.0  # No price data → skip (don't release blind)
    current = float(ticker["price"])
    if entry <= 0 or current <= 0:
        return 999.0
    return abs(current - entry) / entry
```

### Activation Gate vs Proximity Gate

| | position_monitor activation gate | smart_queue proximity gate |
|---|---|---|
| Purpose | Transition pending→open (paper trade activates) | Release signal to demo bridge |
| Input | 1m candles (wick-based `_entry_touched`) | Real-time ticker (distance) |
| Threshold | Entry price must be inside candle high/low range | Current price within X% of entry |
| Default window | 24h lookback | N/A (current price only) |

These two gates are **orthogonal and complementary**:
- Queue releases the signal EARLY (within 1% of entry) → bridge places limit order
- Activation triggers LATER (price actually touches entry) → paper position activates
- The limit order sits on the exchange book in the gap between release and activation
- This is intentional: the order is on the book BEFORE price reaches entry

---

## 10. Service Descriptor

```python
SERVICE_REGISTRY.register(ServiceDescriptor(
    name="smart-queue",
    kind="worker",
    module="shared.daemons.smart_queue",
    description=(
        "Smart Order Queue (Phase 1). Gates demo order placement with "
        "proximity threshold using PriceFeedDaemon price cache. "
        "Holds signals in off-exchange queue until price is within "
        "configurable distance of entry. Phase 2+ adds merge/cooldown."
    ),
    systemd_unit="tickles-smart-queue.service",
    enabled_on_vps=False,  # Starts disabled; operator enables after validation
    tags={"phase": "smart-queue-1"},
))
```

---

## 11. Configuration Summary

| Env Var | Default | Phase | Description |
|---------|---------|-------|-------------|
| `SMART_QUEUE_POLL_S` | 15 | 1 | Tick interval |
| `SMART_QUEUE_PROXIMITY_THRESHOLD` | 0.01 | 1 | Max distance (as decimal, 0.01 = 1%) |
| `SMART_QUEUE_MAX_AGE_HOURS` | 168 | 1 | Expire signals older than this |
| `SMART_QUEUE_DRY_RUN` | false | 1 | Log decisions without writing DB |
| `SMART_QUEUE_MERGE_WINDOW_S` | 60 | 2 | Merge signals within this window |
| `SMART_QUEUE_COOLDOWN_S` | 1800 | 2 | Min time between same-symbol releases |
| `SMART_QUEUE_BATCH_SIZE` | 50 | 3 | Max signals per MCP batch |
