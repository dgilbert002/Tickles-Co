# Step 2: Database Schema (DDL) — Implementation Plan

> **Status**: Ready for implementation  
> **Depends on**: Step 1 (complete)  
> **Blocks**: Steps 3-12  
> **Last updated**: 2026-04-12

---

## Problem Statement

CONTEXT_V3.md Section 16, Step 2 requires: "Write and execute `tickles_shared.sql` (14 tables) and `tickles_jarvais.sql` (10 tables). Test on local MySQL first, then deploy to VPS. Seed `indicator_catalog` with ~250 indicators."

The DDL files already exist at:
- `shared/migration/tickles_shared.sql` — 14 tables, partitioned candles, seed data
- `shared/migration/tickles_company.sql` — 10 tables with `COMPANY_NAME` placeholder

---

## Implementation Tasks

### Task 2a: Execute `tickles_shared.sql` on MySQL

Run the shared database DDL:
```bash
sudo mysql < /opt/tickles/shared/migration/tickles_shared.sql
```

This creates:
- Database `tickles_shared` (utf8mb4)
- 14 tables: instruments, candles (partitioned), indicator_catalog, indicators, strategies, strategy_dna_strands, strategy_windows, backtest_results, backtest_trade_details, backtest_queue, news_items, derivatives_snapshots, system_config, api_cost_log
- Seed data in `system_config` (17 rows)

**Important notes:**
- The candles table uses `PARTITION BY RANGE (TO_DAYS(timestamp))` with monthly partitions from 2024-01 through 2026-12 plus `p_future` MAXVALUE
- FKs are within the same database only (no cross-DB FKs — those are documented but not enforced)
- The `api_cost_log.company_id` is a VARCHAR not a FK — it's a soft reference to company names

### Task 2b: Execute `tickles_jarvais.sql` on MySQL

Run the company database DDL with company name substitution:
```bash
sed 's/COMPANY_NAME/jarvais/g' /opt/tickles/shared/migration/tickles_company.sql | sudo mysql
```

This creates:
- Database `tickles_jarvais` (utf8mb4)
- 10 tables: accounts, trades, trade_cost_entries, order_events, trade_validations, balance_snapshots, leverage_history, agent_state, strategy_lifecycle, company_config
- Seed data in `company_config` (9 rows with `COMPANY_NAME` → `jarvais`)

**Cross-database references (documented, NOT enforced by FK):**
- `trades.instrument_id` → `tickles_shared.instruments.id`
- `trades.strategy_id` → `tickles_shared.strategies.id`
- `trade_validations.backtest_result_id` → `tickles_shared.backtest_results.id`
- `leverage_history.instrument_id` → `tickles_shared.instruments.id`

### Task 2c: Verify all tables, indexes, partitions, FKs

After execution, run verification queries:

```sql
-- Verify tickles_shared tables
USE tickles_shared;
SHOW TABLES;
-- Expected: 14 tables

-- Verify tickles_jarvais tables
USE tickles_jarvais;
SHOW TABLES;
-- Expected: 10 tables

-- Verify candle partitions
SELECT PARTITION_NAME, TABLE_ROWS 
FROM information_schema.PARTITIONS 
WHERE TABLE_SCHEMA = 'tickles_shared' AND TABLE_NAME = 'candles'
ORDER BY PARTITION_ORDINAL_POSITION;

-- Verify seed data
SELECT COUNT(*) FROM tickles_shared.system_config;
-- Expected: 17

SELECT COUNT(*) FROM tickles_jarvais.company_config;
-- Expected: 9

-- Verify indexes exist
SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME 
FROM information_schema.STATISTICS 
WHERE TABLE_SCHEMA = 'tickles_shared'
ORDER BY TABLE_NAME, INDEX_NAME;

-- Verify FK constraints
SELECT CONSTRAINT_NAME, TABLE_NAME, REFERENCED_TABLE_NAME
FROM information_schema.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = 'tickles_shared' AND REFERENCED_TABLE_NAME IS NOT NULL;
```

### Task 2d: Seed `indicator_catalog` with ~250 indicators

Write a Python script at `shared/migration/seed_indicator_catalog.py` that:

1. **Reads Capital 2.0 indicators** from `shared/reference/reference/capital2/python_engine/indicators_comprehensive.py`:
   - The `INDICATOR_METADATA` dict (lines 35-2526) contains ~221 entries
   - Each entry has: direction, category, description, params, param_ranges
   - **Known bug**: duplicate key `ttm_squeeze_on` — second overwrites first. The script should log this and keep the last definition.

2. **Reads JarvAIs V1 SMC indicators** from `shared/reference/reference/jarvais_v1/services/data_scientist.py`:
   - ~30 indicators not in Capital 2.0: order blocks, FVG, BOS, CHoCH, liquidity grabs, AMD cycle, confluence score
   - These return rich dicts, not bools — but for the catalog we just need name, category, direction, description, default_params
   - Map V1 category names to V2 enum values

3. **Inserts into `indicator_catalog`** table with columns:
   - `name` — standardized snake_case name (unique key)
   - `category` — ENUM('momentum','trend','volatility','volume','smart_money','breakout','pullback','crash_protection','combination')
   - `direction` — ENUM('bullish','bearish','neutral')
   - `description` — TEXT
   - `default_params` — JSON
   - `param_ranges` — JSON
   - `source_system` — 'capital2' or 'jarvais' or 'v2_new'
   - `is_active` — TRUE

4. **Deduplication**: Use `INSERT IGNORE` or `ON DUPLICATE KEY UPDATE` on the unique `name` key. If both systems define the same indicator name, keep the Capital 2.0 version (more comprehensive param_ranges) and log the conflict.

5. **Expected result**: ~250 rows in `indicator_catalog`

The script should be runnable standalone:
```bash
sudo python3 /opt/tickles/shared/migration/seed_indicator_catalog.py
```

### Task 2e: Update `CLAUDE.md` with database creation status

Add to the "V2 Migration" section:
- `tickles_shared` database: created, 14 tables, 17 config rows
- `tickles_jarvais` database: created, 10 tables, 9 config rows
- `indicator_catalog`: seeded with ~250 indicators
- Candle partitions: 2024-01 through 2026-12 + future

---

## What Could Go Wrong

1. **MySQL not running**: Check `sudo systemctl status mysql` before executing DDL
2. **Existing databases**: If `tickles_shared` or `tickles_jarvais` already exist, `CREATE DATABASE IF NOT EXISTS` will skip but `CREATE TABLE` will fail if tables exist. Check first with `sudo mysql -e "SHOW DATABASES"`
3. **Partition maintenance**: The candle partitions end at 2026-12 + MAXVALUE. A scheduled job must add new partitions before 2027. Document this as a TODO.
4. **Cross-DB FKs**: MySQL doesn't support cross-database foreign keys. The DDL correctly omits them for cross-DB references. Application code must enforce referential integrity.
5. **Indicator script import path**: The seed script needs to import from `indicators_comprehensive.py` which has dependencies (numpy, pandas, scipy). If these aren't installed, the script should parse the file as text rather than importing it.
6. **Character set**: Both databases use `utf8mb4_unicode_ci`. Ensure MySQL server has `character_set_server=utf8mb4` in my.cnf.

---

## Implementation Order

| # | Task | Mode | Risk |
|---|------|------|------|
| 1 | Check MySQL is running, check for existing databases | code | Low |
| 2 | Execute `tickles_shared.sql` | code | Medium (irreversible) |
| 3 | Execute `tickles_jarvais.sql` via sed | code | Medium (irreversible) |
| 4 | Run verification queries | code | Low |
| 5 | Write and run `seed_indicator_catalog.py` | code | Medium (import issues) |
| 6 | Update `CLAUDE.md` | code | Low |
