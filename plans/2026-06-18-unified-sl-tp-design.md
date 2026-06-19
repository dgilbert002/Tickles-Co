# Unified SL/TP Design Report
# Date: 2026-06-18

## Current State Per Exchange

### BYBIT
- SL/TP passed in `create_order` params as `stopLoss`/`takeProfit` string values.
- Works for ALL order types (market + limit) because Bybit natively supports 
  conditional SL/TP on order placement.
- No post-order attachment needed.
- Status: ✅ WORKS

### BITGET
- **Market orders**: SL/TP passed in `create_order` params as `stopLossPrice`/`takeProfitPrice`. Works.
- **Limit orders**: SL/TP is STRIPPED from `create_order` params (Bitget demo rejects them on limit orders).
  Then `_bitget_set_sl_tp()` is called after order submission, but this FAILS because the
  position doesn't exist yet (order is resting, not filled).
- The post-order TPSL is called unconditionally (lines 478-483) for ALL order types,
  which is redundant for market orders (already passed in create_order) and broken for
  limit orders.
- Status: ⚠️ BUG - limit order SL/TP silently fails

### BLOFIN
- SL/TP passed in `create_order` params as `stopLossPrice`/`takeProfitPrice` strings.
- Only referenced in `submit()` params block (lines 439-443) — passes in create_order.
- No post-order attachment. No `modify_sl` branch. No `set_sl_tp` branch.
- Exists only in params block. The set_sl_tp method (line 528-533) dispatches "bybit",
  "bitget", or falls to `_generic_set_sl_tp` — BloFin is NOT handled, meaning the
  generic path would place reduce-only stop/limit orders instead.
- Status: ⚠️ UNTESTED — appears to work like Bybit (pass in create_order) but `set_sl_tp` 
  has no BloFin branch, so BE-lock and post-fill SL/TP changes would use the wrong method.

### TOOBIT
- SL/TP is ALWAYS stripped from `create_order` (Toobit rejects them).
- `_toobit_set_sl_tp()` calls `/api/v1/futures/position/trading-stop` after every order.
  This endpoint REQUIRES an existing position — fails for limit orders that haven't filled.
- The `_toobit_set_sl_tp` method itself may have param issues too (sends `side` and 
  numeric SL/TP strings — the exchange may expect different shape).
- Status: ⚠️ BUG - fails for ALL order types (market likely works if position exists 
  immediately, but limit orders always fail)

---

## Analysis: When SHOULD SL/TP Be Attached?

### The Fundamental Constraint
All three exchanges' post-order SL/TP endpoints operate on EXISTING POSITIONS:
- Bybit: `POST /v5/position/trading-stop` → needs an open position
- Bitget: `POST /api/mix/v2/order/place-tpsl-order` → needs an open position  
- Toobit: `POST /api/v1/futures/position/trading-stop` → needs an open position

A limit order that hasn't filled has NO POSITION. Calling these endpoints before
fill is guaranteed failure.

### Decision Matrix

| Order Type | Position Exists? | Attach at order time? | Attach post-fill? |
|-----------|-----------------|----------------------|-------------------|
| Market    | Immediately     | ✅ Preferred         | Would work but redundant |
| Limit     | Only after fill | ❌ Impossible        | ✅ Must |

### For limit orders that may take hours to fill
Attaching SL/TP to a non-existent position is not just "pointless" — it's a
guaranteed error from the exchange. The SL/TP MUST be deferred until the fill
is confirmed.

---

## Recommended Unified Approach

### Principle
**Strip SL/TP from create_order for ALL exchanges. Attach post-fill via _reconcile_fills.**

This single code path handles market AND limit orders uniformly:
- Market orders fill immediately → SL/TP attached on the very next tick
- Limit orders fill later → SL/TP attached when _reconcile_fills detects the fill

The only exception: Bybit can optionally keep SL/TP in create_order because it works
there. But for simplicity, even Bybit can use the post-fill path.

### Implementation Plan

#### 1. ccxt_adapter.py — Strip SL/TP from create_order (simplify submit())

Remove all SL/TP param logic from `submit()`. The `params` dict becomes empty.
Remove the post-order Bitget and Toobit SL/TP calls.

Current code (lines 419-492) becomes:
```python
# ── Place order (SL/TP never passed; attached post-fill) ──
params: Dict[str, Any] = {}
```

The `_known_orders` tracking still stores `sl`/`tp` for reference, but they are
no longer acted upon at order time.

#### 2. ccxt_adapter.py — Ensure set_sl_tp() handles ALL exchanges

Add a `blofin` branch to `set_sl_tp()` (line 528-533) and add a `_blofin_set_sl_tp()`.
Also rename/keep `_toobit_set_sl_tp()` and `_bitget_set_sl_tp()`.

The public method `set_sl_tp()` already dispatches:
```python
if ex == "bybit":     → _bybit_set_sl_tp()     ✅
elif ex == "bitget":  → _bitget_set_sl_tp()    ✅  
else:                 → _generic_set_sl_tp()   ❌ (BloFin, Toobit get wrong method)
```

Fix: Add dedicated branches for `blofin` and `toobit`:
```python
elif ex == "blofin":  → _blofin_set_sl_tp()
elif ex == "toobit":  → _toobit_set_sl_tp()
else:                 → _generic_set_sl_tp()  # fallback only
```

For BloFin, research the correct endpoint (likely similar to Bybit's `trading-stop`).

#### 3. demo_bridge.py — Extend _reconcile_fills() to attach SL/TP

Modify `_reconcile_fills()` to:
a) Add `paper_sl` and `paper_tp` to the SELECT query (line 797-801)
b) After detecting a fill (line 814: `status == "closed"`), call `set_sl_tp()` on the adapter
c) Track which orders have already had SL/TP attached (avoid duplicate calls)

Modified SELECT:
```sql
SELECT id, exchange, account_name, exchange_order_id, symbol,
       paper_entry, paper_sl, paper_tp, agent_id, direction
FROM public.demo_orders
WHERE status = 'pending' AND exchange_order_id IS NOT NULL
ORDER BY ordered_at ASC LIMIT 50
```

Post-fill SL/TP attachment block (inserted after line 831, before the `elif status in (...)`):
```python
# Attach SL/TP now that position exists
if (r.get("paper_sl") or r.get("paper_tp")):
    try:
        await self._adapter.set_sl_tp(
            symbol=r["symbol"],
            stop_loss=float(r["paper_sl"]) if r.get("paper_sl") else None,
            take_profit=float(r["paper_tp"]) if r.get("paper_tp") else None,
            exchange=r["exchange"],
            account_name=r["account_name"],
            direction=r["direction"],
        )
        LOG.info("SL/TP attached post-fill for %s/%s %s: SL=%s TP=%s",
                 r["exchange"], r["account_name"], r["symbol"],
                 r.get("paper_sl"), r.get("paper_tp"))
    except Exception as exc:
        LOG.warning("SL/TP attach failed for %s: %s", r["exchange_order_id"], exc)
```

#### 4. demo_bridge.py — _sync_positions() as safety net

`_sync_positions()` already handles BE-lock. It can also serve as a backup
for initial SL/TP attachment if `_reconcile_fills` missed it (e.g., bridge restart).

When `_sync_positions` discovers a NEW position (no existing `demo_orders` row)
and links it to a tracked_position with paper_sl/paper_tp, it should also call
`set_sl_tp()`. This is a secondary path — the primary is _reconcile_fills.

---

## Per-Exchange Summary

| Exchange | Order time | Post-order (market) | Post-fill (limit) | BE-lock modify_sl |
|----------|-----------|---------------------|-------------------|-------------------|
| **Bybit** | Supported but stripped in unified approach | N/A | Via `_bybit_set_sl_tp` | Already works via `modify_sl` |
| **Bitget** | Supported for market, broken for limit | Keep for market (optional) | Via `_bitget_set_sl_tp` | Already works via `modify_sl` |
| **BloFin** | Currently passed in create_order | N/A (unified strips) | Via new `_blofin_set_sl_tp` | Needs `modify_sl` for BE-lock |
| **Toobit** | Rejected by exchange | Via `_toobit_set_sl_tp` | Via `_toobit_set_sl_tp` | Needs `modify_sl` for BE-lock |

---

## Code Changes Required

### File: shared/execution/ccxt_adapter.py

1. **submit()** (lines 418-492): Remove all SL/TP param logic and post-order attachment.
   Keep only order placement. SL/TP values stored in `_known_orders` for audit only.

2. **set_sl_tp()** (lines 507-546): Add `blofin` and `toobit` branches.

3. **New method `_blofin_set_sl_tp()`**: Research BloFin's trading-stop endpoint.
   Likely similar to Bybit's pattern: POST to a position/trading-stop endpoint.
   (If BloFin doesn't support it, fall back to `_generic_set_sl_tp` which places
   reduce-only stop/limit orders.)

4. **modify_sl()** (lines 250-329): Add `blofin` and `toobit` branches for BE-lock.
   Currently only handles bybit and bitget.

### File: shared/daemons/demo_bridge.py

1. **_reconcile_fills()** (lines 785-845): 
   - Add `paper_sl`, `paper_tp` to SELECT
   - Attach SL/TP after fill detection using `self._adapter.set_sl_tp()`
   - Track which orders already got SL/TP (in `demo_orders.metadata` or a set)

2. **_sync_positions()** (lines 950-1236): 
   - When inserting a NEW `demo_orders` row for a discovered position,
     if paper_sl/paper_tp are known, call `self._adapter.set_sl_tp()` as safety net.

---

## Risks & Open Questions

1. **BloFin's trading-stop endpoint**: Not yet researched. May not support the same
   pattern as Bybit/Bitget. If it doesn't, the `_generic_set_sl_tp` fallback
   (placing reduce-only stop/limit orders) works but is less robust than native
   trading-stop endpoints.

2. **Toobit's trading-stop endpoint**: Currently calls `/api/v1/futures/position/trading-stop`
   with `side` and `stopLoss`/`takeProfit` params. Need to verify this is the correct
   shape — the current code may have the params wrong, which is the other half of why
   it's failing.

3. **Race condition**: Between a market order fill and the next tick (~15s), the
   position has no SL/TP. For market orders, the original approach of attaching
   immediately after order submission is safer. Recommendation: keep the immediate
   post-order attachment for market orders on Bitget and Toobit, and use the
   post-fill path only for limit orders.

4. **Bybit optional optimization**: Since Bybit supports SL/TP in create_order,
   we could keep that path for Bybit only. This avoids the 15s gap.

## Revised Recommendation (balancing safety vs. simplicity)

**Tiered approach:**

1. **Bybit**: Keep passing SL/TP in `create_order` (works, battle-tested). No changes.

2. **BloFin**: Keep passing SL/TP in `create_order` (appears to work like Bybit).
   No changes. Add `modify_sl` and `set_sl_tp` branches for BE-lock support.

3. **Bitget**: 
   - Market orders: pass in `create_order` (works). No changes to order flow.
   - Limit orders: strip from `create_order`, attach post-fill via `_reconcile_fills`.

4. **Toobit**:
   - Market orders: strip from `create_order`, attach immediately after order in
     `submit()` (position exists immediately for market orders).
   - Limit orders: strip from `create_order`, attach post-fill via `_reconcile_fills`.

This avoids breaking what already works (Bybit, BloFin, Bitget market) while
fixing the two broken paths (Bitget limit, Toobit all).
