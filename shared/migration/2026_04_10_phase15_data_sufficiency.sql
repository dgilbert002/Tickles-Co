-- Phase 15 — Data Sufficiency Engine.
-- Additive schema: one cache table + six seed profiles in system_config.
-- Safe to re-run: every DDL is guarded with IF NOT EXISTS / ON CONFLICT.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Cache table for sufficiency reports (TTL-style invalidation).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS data_sufficiency_reports (
    id                  BIGSERIAL PRIMARY KEY,
    instrument_id       BIGINT        NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    timeframe           timeframe_t   NOT NULL,
    profile_name        VARCHAR(64)   NOT NULL,
    verdict             VARCHAR(24)   NOT NULL CHECK (verdict IN ('pass','pass_with_warnings','fail')),
    bars                INTEGER       NOT NULL DEFAULT 0,
    first_ts            TIMESTAMP(3) WITH TIME ZONE,
    last_ts             TIMESTAMP(3) WITH TIME ZONE,
    gap_ratio           NUMERIC(10,6) NOT NULL DEFAULT 0,
    max_gap_minutes     INTEGER       NOT NULL DEFAULT 0,
    fresh_lag_minutes   INTEGER,
    report_json         JSONB         NOT NULL,
    computed_at         TIMESTAMP(3) WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ttl_seconds         INTEGER       NOT NULL DEFAULT 300,
    CONSTRAINT uq_data_suff_triplet UNIQUE (instrument_id, timeframe, profile_name)
);

COMMENT ON TABLE  data_sufficiency_reports IS
    'Phase 15 cache: most recent sufficiency verdict per (instrument, timeframe, profile).';
COMMENT ON COLUMN data_sufficiency_reports.verdict IS
    'pass | pass_with_warnings | fail';
COMMENT ON COLUMN data_sufficiency_reports.report_json IS
    'Full SufficiencyReport pydantic payload (coverage stats, reasons, integrity issues).';
COMMENT ON COLUMN data_sufficiency_reports.ttl_seconds IS
    'Cache lifetime; reads older than this trigger a fresh scan.';

CREATE INDEX IF NOT EXISTS ix_data_suff_instr_tf
    ON data_sufficiency_reports(instrument_id, timeframe);
CREATE INDEX IF NOT EXISTS ix_data_suff_verdict
    ON data_sufficiency_reports(verdict);
CREATE INDEX IF NOT EXISTS ix_data_suff_computed_at
    ON data_sufficiency_reports(computed_at);

-- ---------------------------------------------------------------------------
-- 2. Seed the six built-in profiles into system_config.
--    Operators can override values without a code deploy. The Python layer
--    falls back to BUILTIN_PROFILES if a key is absent.
-- ---------------------------------------------------------------------------

INSERT INTO system_config (namespace, config_key, config_value, is_secret)
VALUES
 ('sufficiency.profiles', 'scalp_1m_crypto', $${
    "timeframe": "1m",
    "min_bars": 20160,
    "min_days": 14,
    "max_gap_ratio": 0.005,
    "max_gap_minutes": 5,
    "fresh_lag_max_minutes": 10,
    "daily_bar_target": 1440,
    "allow_is_fake": false,
    "allow_zero_volume": false,
    "notes": "Scalper profile for crypto majors; strict on freshness + tiny gaps."
 }$$, false),
 ('sufficiency.profiles', 'swing_15m_crypto', $${
    "timeframe": "15m",
    "min_bars": 17280,
    "min_days": 180,
    "max_gap_ratio": 0.01,
    "max_gap_minutes": 60,
    "fresh_lag_max_minutes": 60,
    "daily_bar_target": 96,
    "allow_is_fake": false,
    "allow_zero_volume": false,
    "notes": "Swing 15m, 6-month lookback, 1% gaps tolerated."
 }$$, false),
 ('sufficiency.profiles', 'swing_1h_crypto', $${
    "timeframe": "1h",
    "min_bars": 8760,
    "min_days": 365,
    "max_gap_ratio": 0.02,
    "max_gap_minutes": 240,
    "fresh_lag_max_minutes": 180,
    "daily_bar_target": 24,
    "allow_is_fake": false,
    "allow_zero_volume": false,
    "notes": "Swing 1h, 12-month lookback."
 }$$, false),
 ('sufficiency.profiles', 'position_4h_crypto', $${
    "timeframe": "4h",
    "min_bars": 4380,
    "min_days": 730,
    "max_gap_ratio": 0.02,
    "max_gap_minutes": 960,
    "fresh_lag_max_minutes": 720,
    "daily_bar_target": 6,
    "allow_is_fake": false,
    "allow_zero_volume": false,
    "notes": "Position 4h, 2y lookback."
 }$$, false),
 ('sufficiency.profiles', 'position_1d_crypto', $${
    "timeframe": "1d",
    "min_bars": 730,
    "min_days": 730,
    "max_gap_ratio": 0.03,
    "max_gap_minutes": 4320,
    "fresh_lag_max_minutes": 2880,
    "daily_bar_target": 1,
    "allow_is_fake": false,
    "allow_zero_volume": false,
    "notes": "Daily position. Multi-year lookback."
 }$$, false),
 ('sufficiency.profiles', 'swing_15m_equities', $${
    "timeframe": "15m",
    "min_bars": 6300,
    "min_days": 252,
    "max_gap_ratio": 0.02,
    "max_gap_minutes": 60,
    "fresh_lag_max_minutes": 60,
    "daily_bar_target": 26,
    "allow_is_fake": false,
    "allow_zero_volume": true,
    "notes": "Equities/CFD 15m. Session-aware density target."
 }$$, false)
ON CONFLICT (namespace, config_key) DO UPDATE
   SET config_value = EXCLUDED.config_value;

COMMIT;
