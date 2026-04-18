-- Rollback for Phase 15 — Data Sufficiency Engine.
-- Drops the cache table and removes the seeded profile rows.

BEGIN;

DROP TABLE IF EXISTS data_sufficiency_reports;

DELETE FROM system_config
 WHERE namespace = 'sufficiency.profiles'
   AND config_key IN (
       'scalp_1m_crypto',
       'swing_15m_crypto',
       'swing_1h_crypto',
       'position_4h_crypto',
       'position_1d_crypto',
       'swing_15m_equities'
   );

COMMIT;
