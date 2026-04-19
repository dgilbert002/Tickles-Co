"""
shared.gateway — the Market Data Gateway (Phase 17).

The 21-year-old version
=======================
Phase 16 gave us *historical* candles (REST poll + SQL resample).
Phase 17 gives us *live* market data — the firehose:

  CCXT Pro WebSockets  ->  Gateway process  ->  Redis pub/sub  ->  consumers
                                                                    (agents,
                                                                     services,
                                                                     dashboards)

Why a gateway?
--------------
A naive design would let every agent open its own websocket to Binance.
That blows up rate limits, multiplies disconnect bugs, and makes audits
impossible.  Instead, we run **one** durable connection per
exchange-symbol-channel and fan out the messages on Redis.  Anybody who
wants live BTC trades subscribes to the redis channel, not to Binance.

What this package contains
--------------------------
- ``schema``: pydantic models for ticks, trades, L1 books, mark prices,
  funding rates, subscription requests, and stats.
- ``redis_bus``: a thin async wrapper around redis-py for publishing to
  channels (``md.<exchange>.<symbol>.<channel>``) and recording stats
  in well-known keys (``md:gateway:stats``, ``md:gateway:lag:<...>``).
- ``subscriptions``: an in-memory ref-counted subscription registry so
  that "100 agents want BTC L1" only opens **one** websocket.
- ``ccxt_pro_source``: per-exchange WS connection manager.  Each instance
  owns one ``ccxt.pro`` client and runs ``watch_*`` loops with
  exponential-backoff reconnect.
- ``gateway``: the orchestrator that owns the subscriptions, source
  connections, and fan-out tasks.  This is what the daemon and the
  Phase 17 CLI call.
- ``daemon``: long-running entrypoint for ``tickles-md-gateway.service``.
  Reads desired subscriptions from a redis hash so the CLI can
  add/remove subs at runtime without restarting the daemon.

Phase coupling
--------------
- Phase 14 — instruments/venues join here when we resolve adapter codes.
- Phase 15 — the freshness check on a 1m profile *should* see ticks at
  most a few seconds stale once Phase 17 is feeding the candle daemon's
  Redis input.
- Phase 16 — the live 1m candle daemon currently polls REST.  Phase 18+
  may switch it to consume the gateway directly, but Phase 17 does NOT
  touch ``shared/market_data/`` — that legacy is left running.
"""

from shared.gateway.schema import (
    GatewayStats,
    L1Book,
    MarkPrice,
    SubscriptionKey,
    SubscriptionRequest,
    Tick,
    TickChannel,
    Trade,
)

__all__ = [
    "GatewayStats",
    "L1Book",
    "MarkPrice",
    "SubscriptionKey",
    "SubscriptionRequest",
    "Tick",
    "TickChannel",
    "Trade",
]
