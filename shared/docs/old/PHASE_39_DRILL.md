# Phase 39 - End-to-end Drill

> Final integration phase. A single CLI runs through every phase
> 13-37 surface (CLIs, registries, in-memory pools) and emits a
> machine-readable go/no-go report. Read-only / in-memory only -
> nothing in this phase touches live exchanges, the live PostgreSQL
> schemas owned by other phases, or live Telegram traffic.

## Goal

Prove, in one command, that:

1. Every phase package still imports cleanly.
2. Every phase still exposes the same public surface (registries,
   protocol constants, CLI subcommands) we documented in Phase 38.
3. The CLI demos that were used as live evidence during phases 33-37
   still run end-to-end.

If a single step fails, the script exits non-zero. That makes it
suitable as a CI / pre-deploy smoke test.

## What it runs

| Phase    | Step                          | Kind   | What it proves                                                              |
|----------|-------------------------------|--------|------------------------------------------------------------------------------|
| 14       | `market_data_layout`          | python | Phase-14 market-data package files still on disk.                           |
| 18       | `indicators_registry`         | python | 260 indicators registered, categories intact.                               |
| 19       | `backtest_engines_registry`   | python | `classic`, `vectorbt`, `nautilus` engines registered.                       |
| 21       | `auditor_store_import`        | python | Rule-1 continuous auditor + AuditStore import.                              |
| 22       | `services_registry_snapshot`  | python | All 23 services registered; counts by kind / phase.                         |
| 23       | `enrichment_default_pipeline` | python | Default enrichment pipeline builds.                                         |
| 25       | `treasury_pure_size`          | cli    | Pure sizer is deterministic & decision payload is well-formed.              |
| 26       | `execution_paper_simulate`    | cli    | Paper execution adapter produces deterministic fills.                       |
| 27       | `regime_classifiers`          | python | All 3 classifiers + 8 regime labels available.                              |
| 28       | `guardrails_rule_kinds`       | python | Rule + action types still match the schema.                                 |
| 29       | `altdata_sources`             | cli    | Built-in alt-data sources catalogue surfaces.                               |
| 30       | `events_kinds`                | cli    | Canonical event kinds catalogue surfaces.                                   |
| 31-32    | `souls_personas`              | python | All 7 souls registered: apex/quant/ledger/scout/curiosity/optimiser/regime. |
| 33       | `arb_demo`                    | cli    | Arb scanner runs against live CCXT public quotes.                           |
| 33       | `copy_demo`                   | cli    | Copy-trader runs against live CCXT public trade tape.                       |
| 34       | `strategy_demo`               | cli    | Composer aggregates arb + copy into intents.                                |
| 35       | `backtest_submit_demo`        | cli    | Backtest submission queue + worker hook end-to-end.                         |
| 36       | `dashboard_import`            | python | Dashboard package + migration on disk.                                      |
| 37       | `mcp_demo`                    | cli    | JSON-RPC MCP server end-to-end (in-memory).                                 |

## How to run

```bash
# List the steps that will run.
python -m shared.cli.drill_cli list

# Run the drill, write a JSON report.
python -m shared.cli.drill_cli run --out shared/docs/PHASE_39_DRILL.json

# Stop on the first failure (for CI noise reduction).
python -m shared.cli.drill_cli run --stop-on-fail
```

Exit code is `0` iff every step succeeded.

## Reference reports

* `shared/docs/PHASE_39_DRILL.json` - last local run on dev workstation.
* `shared/docs/PHASE_39_DRILL_VPS.json` - last run executed on the VPS.

Both reports include, for every step:

* `phase`, `step`, `description`, `kind`, `argv`
* `returncode`, `ok`, `elapsed_ms`, `stderr`
* `payload` - structured JSON for python steps + JSON-emitting CLIs;
  the last 400 chars of stdout for human-text demos (so the operator
  can eyeball the actual demo output later).

## Last results

| Environment | Steps | Passed | Failed | Elapsed |
|-------------|-------|--------|--------|---------|
| Local Win11 | 19    | 19     | 0      | ~21s    |
| VPS         | 19    | 19     | 0      | ~31s    |

## Rollback

The drill harness is purely additive:

* New file: `shared/cli/drill_cli.py`
* New docs: `shared/docs/PHASE_39_DRILL.md`,
  `shared/docs/PHASE_39_DRILL.json`,
  `shared/docs/PHASE_39_DRILL_VPS.json`

To roll back, delete those four files. Nothing else in the codebase
references the drill module - no service registry entry, no DB
migration, no systemd unit. Other phases are unaffected.

## Why no DB migration?

Phase 39 is a meta-phase. It exercises everything that came before
without owning any new persistent state. If we ever want a
historical record of drill runs in PostgreSQL we can add a tiny
`public.drill_runs` table later, but that is intentionally out of
scope for this commit - the on-disk JSON reports are sufficient
for now.
