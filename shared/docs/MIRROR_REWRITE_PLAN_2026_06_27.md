# Mirror Rewrite Plan — 100% copy_lev_3pct → TOOBIT replication

> Author: pairing session 2026-06-27. Objective: the live exchange (TOOBIT, via
> `copy_lev_3pct` mapping) must faithfully mirror exactly what the paper agent
> `copy_lev_3pct` enters — no duplicates, no forced/chased entries, smart-queue
> margin gate preserved, full DB recording for paper-vs-real comparison.

## Verified architecture (source of truth)

```
Discord chart → Interpreter (LLM) → tracked_positions (entry/SL/TP/dir/symbol)
                                          ↓ (paper engine polls 30s)
              copy_trade_monitor → copy_lev_3pct decides size/lev/dedup/chase
                  → INSERT competition_trades (exit_price=NULL)   ← SOURCE OF TRUTH
                                          ↓
              demo_bridge mirrors competition_trades onto TOOBIT (this rewrite)
```

`competition_trades` rows where `agent_id='copy_lev_3pct' AND exit_price IS NULL`
ARE the open paper positions. Mapping `copy_lev_3pct → toobit/ToobitCOLive` lives
in `competition_agent_exchanges` (competition_id='copy-trade-scenarios').

## Root causes found (evidence)

1. **Bridge reads the wrong table.** `_get_new_signals()` reads `tracked_positions`
   and `_agent_for_actor` mirrors EVERY trader signal to "all regular agents."
   It re-decides entries with its own candle/chase/sizing logic. This is the core
   DeepSeek mistake → duplicates + divergence from the paper agent.
2. **Paper-engine double-entry.** `_load_state` rehydrates per-agent `_agent_entered`
   ONLY from `copy_agent_state.open_positions` JSON, NOT from `competition_trades`.
   If the JSON snapshot lags/fails (crash between INSERT and `_save_agent`), a
   restart re-enters the same tracked_position → duplicate competition_trades rows.
   Evidence: tp=14664 (MSTR) → ct 4987+5074; tp=14547 (ETH) → ct 4768+5161.
3. **Broken paper↔exchange link.** 0/46 demo_orders have `competition_trade_id`;
   all 42 open competition_trades have NULL `exchange_order_id`/`exchange_name`.
   Paper-vs-real comparison is impossible today.

## Fix A — Paper engine double-entry guard (copy_trade_monitor.py)

In `_load_state`, after rebuilding the global `_entered_positions` from
`competition_trades`, ALSO rebuild per-agent `_agent_entered` from open
`competition_trades` rows (agent_id + tracked_position_id where exit_price IS NULL).
This makes the per-agent dedup survive a snapshot-lag restart.

## Fix B — Bridge mirrors competition_trades (demo_bridge.py)

Replace `_get_new_signals()` source with a new `_get_agent_open_trades()`:

```sql
SELECT ct.id AS competition_trade_id, ct.tracked_position_id, ct.agent_id,
       ct.symbol, ct.direction, ct.entry_price, ct.sl_price, ct.tp_price,
       ct.allocated, ct.leverage, ct.entered_at, ct.exchange_order_id,
       tp.current_price
FROM competition_trades ct
JOIN tracked_positions tp ON tp.id = ct.tracked_position_id
WHERE ct.contest_id = 'copy-trade-scenarios'
  AND ct.exit_price IS NULL                 -- still open in paper
  AND ct.agent_id IN (<mapped agents>)      -- e.g. copy_lev_3pct
ORDER BY ct.entered_at ASC
```

Mirror rules:
- **One exchange order per `competition_trade_id`** (the unique key). Track via
  `demo_orders.competition_trade_id`.
- **Sizing/leverage come FROM the ct row** (already decided by paper). Do NOT
  recompute. Keep the per-symbol max-leverage clamp and min-order-size check only.
- **Smart queue = margin gate (unchanged intent):**
  - dist% = |current_price - entry| / entry
  - within `SMART_QUEUE_PLACE_PCT` (5%) → place limit order on exchange
  - 5–10% → holding pattern, no action (don't churn)
  - > `SMART_QUEUE_CANCEL_PCT` (10%) → cancel the exchange order (price moving away)
  - prioritize closest-to-entry first when margin-capped (margin_usage_pct)
- **Same symbol+direction+account 2% dedup retained** (closest entry wins; >2%
  apart are legitimately separate trades) — this also absorbs the rare paper dupe.
- **Per (symbol,direction,exchange,account) asyncio.Lock** retained (race guard).
- **On paper close** (ct.exit_price set) → cancel pending / close filled exchange
  order for that competition_trade_id.
- **Record the link both ways:** demo_orders.competition_trade_id set on insert;
  write back exchange_order_id + exchange_name + exchange_account onto the
  competition_trades row so the dashboard can compare paper vs real.

## Fix C — DB integrity

- Add UNIQUE index on demo_orders(competition_trade_id) WHERE competition_trade_id
  IS NOT NULL so ON CONFLICT actually fires and one ct → one demo order.
- Keep existing partial unique on (tracked_position_id, exchange, account_name).

## Bugs to fix in passing (from subagent hunt — keep scope tight)

- `_place_locks` init (DONE).
- `_open_demo_count` SQL precedence: wrap the OR in parens.
- `_expire_distant` must pass `symbol` to `_cancel_demo_order` (Toobit needs it).
- `qty` referenced before assignment in margin-cap rejection log.

## What we KEEP (do not remove — there for a reason)

- Position sizing rules, leverage-from-SL, max-concurrent caps, 2% dedup,
  5%/10% smart-queue thresholds, holding-pattern band, margin_usage_pct cap.

## Verification

1. After deploy, every open copy_lev_3pct competition_trade within 5% of entry has
   exactly ONE TOOBIT limit order; >10% away has none; 5–10% unchanged.
2. demo_orders.competition_trade_id populated; competition_trades.exchange_order_id
   populated. No duplicate (symbol,direction) within 2% on TOOBIT.
3. Live MCP cross-check: fetchOpenOrders count == open ct within-5% count.
