# migration/

Numbered SQL migration files. **Immutable once applied** to any live
database.

## Naming

`NNN_purpose.sql` where `NNN` is a zero-padded monotonic sequence.

## Rules

1. One concern per migration (add a column, create a table, etc.). Never mix
   destructive and additive operations in a single file.
2. Every file starts with a comment block documenting:
   - What it does
   - Why
   - How to roll back (explicit SQL or "see `NNN_revert_purpose.sql`")
   - Target database(s): `shared`, `company:<name>`, or `clickhouse`.
3. Once applied on any environment (local, VPS, production), **never edit**.
   Create a new migration instead.
4. Sequence is global across all databases. The `NNN` number tells you the
   order, regardless of which DB each file targets.

## Current numbered slots (reserved by ROADMAP)

| NNN | Purpose | DB | Phase |
|---|---|---|---|
| 001 | Postgres shared DDL | shared | 0 (done) |
| 002 | Postgres company DDL | company | 0 (done) |
| 003 | ClickHouse backtests schema | clickhouse | 0 (done) |
| 004 | MemU container + parent + verified_by columns | shared | 1C |
| 005 | ClickHouse forward-test tables | clickhouse | 1D |
| 006 | Capabilities + account_registry + leverage_history | shared | 2 |
| 007 | Sessions + session_definitions | shared | 4 |
| 008 | Companies registry | shared | 5 |
| 009 | trade_validations | company | 6 |

When an already-applied migration is wrong, do **not** edit it — add a
follow-up migration that corrects the situation and document why in the
comment block.
