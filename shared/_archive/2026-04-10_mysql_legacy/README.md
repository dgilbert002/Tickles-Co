# 2026-04-10 — MySQL legacy variants archived

## What moved here

- `config.mysql.py` — legacy MySQL-flavoured config loader (previously `shared/utils/config.mysql.py`)
- `db.mysql.py` — legacy MySQL-flavoured connection pool helper (previously `shared/utils/db.mysql.py`)
- `seed_instruments.py.v1-mysql` — legacy MySQL seed script that populated `tickles_shared.instruments` via `pymysql` (previously `shared/migration/seed_instruments.py`). Superseded by the Phase 14 Postgres loader (`shared/assets/loader.py`).

## Why

Phase 0 (see `ROADMAP_V3.md §Phase status`) migrated the platform off MySQL
onto Postgres + ClickHouse + Redis. The `.mysql.py` variants were left on
disk as "just-in-case" references during Phase 1. Phase 13 (Foundations
cleanup) archives them here so `shared/utils/` only contains code that is
actively imported by running services.

Current production stack:

- **Postgres** (Paperclip embedded DB on `127.0.0.1:54329`, user `paperclip`)
  for instruments, candles, trades, positions, backtests, collector sources,
  MemU metadata.
- **ClickHouse** for backtest run rows and long-history analytics.
- **Redis** for online feature store and pub/sub fan-out.
- **QuestDB** (planned Phase 17) for tick/L2 data.
- **DuckDB** (planned Phase 20) for Feast offline store.

## How to roll back

If a feature discovers it needs a MySQL code path:

1. Copy the file back to `shared/utils/` — do **not** move, keep this archive
   copy intact.
2. Re-add the MySQL driver stack to `requirements.txt`
   (`mysql-connector-python`, `pymysql`).
3. Add a config switch in `shared/utils/config.py` to pick the backend.
4. Note the reactivation in `ROADMAP_V3.md` change log.

No code outside `reference/` imports these modules as of Phase 13, so
archiving is safe.

## Verification done at archive time

```powershell
# No active import under shared/ (reference/ is read-only legacy code)
rg -n "config\.mysql|db\.mysql" C:\Tickles-Co\shared --glob "!reference/**" --glob "!_archive/**"
# → no matches
```
