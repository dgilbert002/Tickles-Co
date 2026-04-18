---
name: schema-designer
description: Design MySQL database schemas following Tickles And Co standards. Use when asked to create tables, design schemas, or plan database structure.
---

# Schema Designer Skill

## When Activated
User asks to "design a schema", "create a table", "plan the database", or discusses database structure.

## Standards

### Every Table Must Have
```sql
id BIGINT AUTO_INCREMENT PRIMARY KEY,
created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
updated_at DATETIME(3) NULL ON UPDATE CURRENT_TIMESTAMP(3),
```

### Naming
- Tables: snake_case (trades, positions, candles)
- Columns: snake_case (created_at, param_hash, is_active)
- Booleans: is_ prefix (is_paper, is_active, is_closed)
- Foreign keys: [table]_id (strategy_id, exchange_id)
- Indexes: idx_[table]_[columns] (idx_trades_strategy_id)
- Unique keys: uk_[table]_[columns] (uk_candles_exchange_symbol_tf_ts)

### Data Types
- Prices: DECIMAL(20,8)
- Volumes: DECIMAL(30,8)
- Percentages: DECIMAL(10,6) for most, DECIMAL(5,2) for halt_threshold_pct
- Hashes: CHAR(64) for SHA-256 (always exactly 64 hex chars)
- Timestamps: DATETIME(3) for millisecond precision

### Multi-Tenancy
- Shared tables go in `tickles_shared` database
- Company-specific tables go in `tickles_[company]` databases
- Reference the canonical DDL files:
  - `shared/migration/tickles_shared.sql`
  - `shared/migration/tickles_company.sql`

### Partitioning
For high-volume tables like candles:
```sql
PARTITION BY RANGE (UNIX_TIMESTAMP(timestamp)) (
    PARTITION p202301 VALUES LESS THAN (UNIX_TIMESTAMP('2023-02-01')),
    PARTITION p202302 VALUES LESS THAN (UNIX_TIMESTAMP('2023-03-01')),
    PARTITION pmax VALUES LESS THAN MAXVALUE
)
```

### Indexing
- Always index foreign keys
- Consider composite indexes for common query patterns
- Use covering indexes for frequent read-only queries
