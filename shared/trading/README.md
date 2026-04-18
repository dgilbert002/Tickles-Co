# trading/

Everything related to **outbound trade actions**: position sizing, wallets,
capability checks, order management, and risk validation.

Exchange **write** adapters live in the top-level `connectors/` folder
(same place as exchange reads) because a CCXT / Capital.com adapter
naturally handles both directions. `trading/` only contains the
platform-agnostic logic that sits above those adapters.

## Scope (Phase 2 and beyond)

- `sizer.py` — **pure, deterministic** function. Computes quantity, leverage,
  SL, TP, expected spread/slippage/fees. Zero I/O. Called by both the
  backtest engine and the live OMS — that's how Rule 1 parity is enforced by
  construction.
- `treasury.py` — balance snapshots, capability checks, API-key resolution.
  The single source of truth for "can this agent trade this account right now?".
- `oms.py` — the **only** module that talks to a `connectors/*.py` write
  method. Accepts a `TradeIntent`, runs it through sizer → (risk_agent?) →
  treasury, then calls the adapter. Writes `trades` + `trade_cost_entries`
  rows.
- `risk_agent.py` — optional LLM-backed qualitative judgement layer. **OFF
  by default** per locked decision #2. Enabled per-company via config.
- `validation.py` *(Phase 6)* — Rule 1 pairing + drift attribution +
  halt-on-threshold enforcement.

## Rules

- Every public function takes `company_id`. No hardcoded company anywhere.
- `sizer.py` is **pure**. Never reads a DB, never hits an API. Unit-testable
  with frozen golden inputs/outputs. Both the backtester and the live OMS
  import this same file so numbers can't drift.
- `oms.py` is the **only** outbound-write entry point. If you find yourself
  calling an exchange order-placement method anywhere else, stop and refactor.
- Idempotency: every order carries a `client_order_id` derived from
  `(strategy_id, signal_ts_ms, symbol)` so retries never double-submit.
- No direct `connectors/*.py` imports from outside `trading/oms.py`.
