-- Phase 14 rollback — reverses 2026_04_10_phase14_asset_catalog.sql.
-- Safe to run repeatedly. Does NOT touch the 50 existing instrument rows
-- themselves (only strips the FK columns added in forward migration).

BEGIN;

DROP VIEW IF EXISTS v_asset_venues;

ALTER TABLE instruments DROP COLUMN IF EXISTS asset_id;
ALTER TABLE instruments DROP COLUMN IF EXISTS venue_id;

DROP TABLE IF EXISTS instrument_aliases;
DROP TABLE IF EXISTS assets;
DROP TABLE IF EXISTS venues;

COMMIT;
