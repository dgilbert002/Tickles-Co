# market_data/

Everything related to **inbound market data** — candles, tickers, order books,
funding, and gap/retention management.

This folder is **live on the VPS** (/opt/tickles/shared/market_data/) and is
driven by the `tickles-candle-daemon.service` systemd unit.

## What lives here today

- `candle_service.py` — wrapper around the shared DB for OHLCV writes/reads.
- `run_candle_collection.py` — entrypoint that the systemd daemon invokes.
- `gap_detector.py` — finds missing candles and triggers backfills.
- `retention.py` — trims old candle data per configured TTL.
- `timing_service.py` — **centralised UTC-aware clock** used by every
  component that cares about session opens, DST, or "is it weekend" checks.
  Everyone on the platform reads time through here.

## What belongs here in future phases

- `gateway.py` *(Phase 3)* — single WebSocket hub per exchange with Redis
  pub/sub fan-out, so every agent shares one connection instead of each
  creating their own.

## What does NOT belong here

- Discord / Telegram / news collectors → top-level `collectors/`.
- Exchange adapters (CCXT, Capital.com) → top-level `connectors/`.
- Trading / order-placement code → `trading/` (Phase 2).
- Candle schema / DB tables → `migration/`.

## Rules

- Every new function takes `company_id` in its signature (multi-company is
  first-class).
- Timestamps in UTC milliseconds; never local time. Convert at the edges
  using `timing_service.py`.
- Read from Redis pub/sub (once `gateway.py` ships) rather than opening your
  own WebSocket to an exchange.
