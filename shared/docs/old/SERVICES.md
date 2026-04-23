# Tickles Services Index

Single source of truth: `shared/services/registry.py`. This document
mirrors the registry for humans; the CLI snapshots (Phase 24
`services_catalog_cli`, Phase 36 `dashboard_cli serve`) use the
registry directly.

## Frozen inventory (Phase 38)

| Service               | Kind       | Phase   | vps=on |
|-----------------------|------------|---------|--------|
| candle-daemon         | collector  | 13      | yes    |
| catalog               | catalog    | 14      | yes    |
| bt-workers            | worker     | 16      | yes    |
| md-gateway            | gateway    | 17      | yes    |
| auditor               | auditor    | 21      | no     |
| banker                | worker     | 25      | no     |
| executor              | worker     | 26      | no     |
| regime                | worker     | 27      | no     |
| crash-protection      | worker     | 28      | no     |
| altdata-ingestor      | worker     | 29      | no     |
| events-calendar       | worker     | 30      | no     |
| souls                 | worker     | 31-32   | no     |
| arb-scanner           | worker     | 33      | no     |
| copy-trader           | worker     | 33      | no     |
| strategy-composer     | worker     | 34      | no     |
| backtest-runner       | worker     | 35      | no     |
| backtest-submitter    | api        | 35      | no     |
| dashboard             | api        | 36      | no     |
| mcp-server            | api        | 37      | no     |
| discord-collector     | collector  | 3A.1    | no     |
| news-rss              | collector  | 3A.1    | no     |
| telegram-collector    | collector  | 3A.1    | no     |
| tradingview-monitor   | collector  | 3A.1    | no     |

23 services total as of the Phase 38 freeze.

## Conventions

- **enabled_on_vps=True** - service is running live on the VPS via a
  systemd unit. Any code change to these paths should be followed by
  a `systemctl restart tickles-<name>.service` during deploy.
- **enabled_on_vps=False** - code is shipped, DB schema is applied, a
  CLI-driven `demo` / tests exist. The systemd unit is intentionally
  staged but inactive until operators turn it on.
- Every service is runnable by hand via `python -m <module>` using
  the `module` attribute on its `ServiceDescriptor`.

## Databases owned

### PostgreSQL (tickles_shared)

Tables created by phases 22-37:

| Phase | Table / view |
|-------|--------------|
| 22    | `tickles_services_catalog`, `tickles_services_catalog_snapshots`, view `tickles_services_catalog_current` |
| 23    | `news_enriched` |
| 25    | `capabilities`, `banker_balances`, view `banker_balances_latest`, `leverage_history` |
| 26    | `orders`, `order_events`, `fills`, `position_snapshots`, view `positions_current` |
| 27    | `regime_config`, `regime_states`, view `regime_current` |
| 28    | `crash_protection_rules`, `crash_protection_events`, view `crash_protection_active` |
| 29    | `alt_data_items`, view `alt_data_latest` |
| 30    | `events_calendar`, views `events_active`, `events_upcoming` |
| 31    | `agent_personas`, `agent_prompts`, `agent_decisions`, view `agent_decisions_latest` |
| 32    | `scout_candidates`, `optimiser_candidates`, `regime_transitions` |
| 33    | `arb_venues`, `arb_opportunities`, `copy_sources`, `copy_trades` |
| 34    | `strategy_descriptors`, `strategy_intents`, view `strategy_intents_latest` |
| 35    | `backtest_submissions`, view `backtest_submissions_active` |
| 36    | `dashboard_users`, `dashboard_otps`, `dashboard_sessions`, view `dashboard_sessions_active` |
| 37    | `mcp_tools`, `mcp_invocations`, view `mcp_invocations_recent` |

### ClickHouse

- `backtests` database (Phase 16+) - backtest run metadata + equity curves.

### Redis

- Backtest queue (Phase 16).
- Online feature store (Phase 20).

### SQLite

- Rule-1 auditor store (Phase 21).
