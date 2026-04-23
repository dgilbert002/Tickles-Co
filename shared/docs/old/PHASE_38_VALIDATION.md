# Phase 38 - Validation + code-analysis + docs freeze

This document is the end-of-build "audit stamp" for phases 13-37 of
the Trading House Master Plan. It records exactly what was checked,
what passed, what was knowingly left alone, and how to reproduce the
freeze. Written in plain English so any of us (or a future agent)
can audit the state of the system without reading the source tree.

## 1. Scope

Phase 38 validates the 22 phase-scoped module directories that
carry the system we built:

```
shared/backtest                shared/regime
shared/backtest_submit         shared/guardrails
shared/features                shared/alt_data
shared/services                shared/events
shared/enrichment              shared/souls
shared/services_catalog        shared/arb
shared/banker                  shared/copy
shared/execution               shared/composer
shared/indicators              shared/dashboard
shared/auditor                 shared/mcp
shared/cli                     shared/tests
```

Legacy top-level files the project inherited (pre-Phase-22) are
**not** rewritten as part of this phase, in line with the project
rule "never remove/modify functionality that wasn't part of the
current phase without explicit approval". Where those files surface
findings below we note them as `PRE-EXISTING`.

## 2. Tooling used

| Tool     | What it checks                                         |
|----------|--------------------------------------------------------|
| ruff     | Lint + style (PEP 8, unused imports, ambiguous names). |
| mypy     | Static type-checking (`--follow-imports=silent --explicit-package-bases`). |
| bandit   | Security scanning (`-ll` = medium+ severity only).     |
| pytest   | Full regression over `shared/tests`.                   |

## 3. Findings

### 3.1 ruff - CLEAN

All 22 phase module directories pass with zero findings after the
following safe rewrites:

- `shared/backtest/accessible.py` - two semicolon-joined statements
  split onto separate lines (E702).
- `shared/backtest/indicators/core.py` +
  `shared/backtest/indicators/smart_money.py` - variable name `l`
  (single-letter "low") renamed to `lo` (E741). Math is unchanged;
  the full regression (see 3.4) still passes.

### 3.2 mypy - 0 errors on the Phase 22-37 surface

After adding `# type: ignore[import-not-found]` / `[import-untyped]`
on the two legitimate missing-stub cases (asyncpg + pandas used
inside CLIs):

- `shared/cli/treasury_cli.py`, `shared/cli/services_catalog_cli.py`
  (asyncpg lazy imports).
- `shared/cli/auditor_cli.py`, `shared/cli/features_cli.py`,
  `shared/cli/engines_cli.py` (pandas).

Remaining errors: 12 in one file: `shared/services/run_all_collectors.py`.
This is a **PRE-EXISTING** legacy orchestrator that predates Phase
22's service split. Its type errors relate to dict-vs-object access
patterns in the old collector constructor flow. Out-of-scope for the
phase build; deferred.

### 3.3 bandit -ll - 0 medium/high findings

Two Low-confidence Medium-severity B608 findings were reviewed and
`# nosec B608` annotated in `shared/backtest/accessible.py`. Both
queries use parameter binding (`%(name)s`); the only interpolated
fragments are either module-local strings or values drawn from an
allow-list. Total Low findings (37) are informational (pickle,
random usage, etc.) and do not warrant changes.

### 3.4 pytest - 563 / 563 green

```
shared/tests: 563 passed in ~33s locally
            : 563 passed in ~80s on the VPS
```

Includes:
- Phase-33 arb + copy tests (real CCXT public-market snapshot).
- Phase-34 composer parity between arb -> intents and souls ->
  intents flows.
- Phase-35 backtest submission deduplication by spec hash.
- Phase-36 dashboard HTTP flow (OTP -> session token -> snapshot).
- Phase-37 MCP stack (stdio + HTTP transports, JSON-RPC errors,
  audit recording).

### 3.5 Service registry - frozen at 23 services

```
phase 13   candle-daemon           collector  vps=on
phase 14   catalog                 catalog    vps=on
phase 16   bt-workers              worker     vps=on
phase 17   md-gateway              gateway    vps=on
phase 21   auditor                 auditor    vps=off
phase 25   banker                  worker     vps=off
phase 26   executor                worker     vps=off
phase 27   regime                  worker     vps=off
phase 28   crash-protection        worker     vps=off
phase 29   altdata-ingestor        worker     vps=off
phase 30   events-calendar         worker     vps=off
phase 31   souls                   worker     vps=off
phase 33   arb-scanner             worker     vps=off
phase 33   copy-trader             worker     vps=off
phase 34   strategy-composer       worker     vps=off
phase 35   backtest-runner         worker     vps=off
phase 35   backtest-submitter      api        vps=off
phase 36   dashboard               api        vps=off
phase 37   mcp-server              api        vps=off
phase 3A   discord-collector       collector  vps=off
phase 3A   news-rss                collector  vps=off
phase 3A   telegram-collector      collector  vps=off
phase 3A   tradingview-monitor     collector  vps=off
```

See `shared/docs/SERVICES.md` for the canonical table and every
database artefact each service owns.

## 4. How to reproduce

```
# Lint
py -m ruff check shared/backtest shared/backtest_submit \
    shared/features shared/services shared/enrichment \
    shared/services_catalog shared/banker shared/execution \
    shared/regime shared/guardrails shared/alt_data \
    shared/events shared/souls shared/arb shared/copy \
    shared/composer shared/dashboard shared/mcp \
    shared/indicators shared/auditor shared/cli shared/tests

# Types
py -m mypy --follow-imports=silent --explicit-package-bases \
    shared/backtest_submit shared/services shared/enrichment \
    shared/services_catalog shared/banker shared/execution \
    shared/regime shared/guardrails shared/alt_data \
    shared/events shared/souls shared/arb shared/copy \
    shared/composer shared/dashboard shared/mcp shared/cli

# Security
py -m bandit -q -r -ll shared/backtest shared/backtest_submit \
    shared/features shared/services shared/enrichment \
    shared/services_catalog shared/banker shared/execution \
    shared/regime shared/guardrails shared/alt_data \
    shared/events shared/souls shared/arb shared/copy \
    shared/composer shared/dashboard shared/mcp \
    shared/indicators shared/auditor shared/cli

# Regression
py -m pytest shared/tests -q
```

## 5. Rollback of Phase-38 edits

Phase 38 touched only:

- `shared/backtest/accessible.py` - split semicolon lines; nosec.
- `shared/backtest/indicators/core.py` + `smart_money.py` - rename
  `l` -> `lo`.
- CLI `# type: ignore` annotations for pandas / asyncpg.
- `shared/docs/SERVICES.md` + this file (new).

To roll back: `git revert` the Phase 38 commit. No DB migration was
applied, no service registry row was added, no runtime behaviour
changed.
