-- shared/intelligence/migrations/2026_04_29_phase0_schema_migrations_ROLLBACK.sql
-- Phase 0 rollback: drop schema_migrations and table_writers
-- WARNING: This destroys the migration ledger. Only run during full teardown.

DROP TABLE IF EXISTS public.table_writers;
DROP TABLE IF EXISTS public.schema_migrations;
