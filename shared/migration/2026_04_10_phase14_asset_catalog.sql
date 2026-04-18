-- Phase 14 — Universal Asset Catalog
-- Run on tickles_shared (Postgres, 127.0.0.1:5432).
-- This migration is additive only: every change is an ADD COLUMN / CREATE TABLE
-- / CREATE VIEW. No data is rewritten, no column is dropped, no row is deleted.
-- Candles, backtest_results, backtest_queue continue to join on instruments.id
-- exactly as before.
--
-- Rollback: 2026_04_10_phase14_asset_catalog_rollback.sql

BEGIN;

-- --------------------------------------------------------------------------
-- 1. venues — the exchange / broker / data-provider layer
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS venues (
    id              SERIAL PRIMARY KEY,
    code            VARCHAR(32)  NOT NULL UNIQUE,        -- 'binance', 'bybit', 'capital', 'alpaca', 'yfinance'
    display_name    VARCHAR(128) NOT NULL,
    venue_type      VARCHAR(24)  NOT NULL,               -- 'crypto_cex' | 'crypto_dex' | 'broker_cfd' | 'broker_equity' | 'data_only'
    adapter         VARCHAR(32)  NOT NULL,               -- 'ccxt' | 'capital' | 'alpaca' | 'yfinance'
    ccxt_id         VARCHAR(32),                         -- matches ccxt.<id>() when adapter='ccxt'
    supports_spot   BOOLEAN      NOT NULL DEFAULT false,
    supports_perp   BOOLEAN      NOT NULL DEFAULT false,
    supports_margin BOOLEAN      NOT NULL DEFAULT false,
    api_base_url    TEXT,
    ws_base_url     TEXT,
    priority        SMALLINT     NOT NULL DEFAULT 100,    -- smaller = preferred for arbitrage tie-breaks
    is_active       BOOLEAN      NOT NULL DEFAULT true,
    notes           JSONB,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_venues_active ON venues(is_active) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_venues_adapter ON venues(adapter);

COMMENT ON TABLE venues IS
    'Phase 14: exchange / broker / data-provider registry. One row per source we can read or trade on. Loader looks up by code.';

-- --------------------------------------------------------------------------
-- 2. assets — the logical asset layer (one row per "BTC", one row per "gold")
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS assets (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(32)     NOT NULL UNIQUE,     -- canonical ticker: 'BTC', 'ETH', 'GOLD', 'SP500', 'EUR', 'AAPL'
    display_name    VARCHAR(128)    NOT NULL,
    asset_class     asset_class_t   NOT NULL,
    alias_of_id     INT REFERENCES assets(id) ON DELETE SET NULL,
    auto_seeded     BOOLEAN         NOT NULL DEFAULT true,
    curation_notes  TEXT,
    metadata        JSONB,                               -- sector, country, market_cap, cmc_id, etc.
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_assets_class      ON assets(asset_class);
CREATE INDEX IF NOT EXISTS idx_assets_alias_of   ON assets(alias_of_id) WHERE alias_of_id IS NOT NULL;

COMMENT ON TABLE assets IS
    'Phase 14: logical asset layer. Many instruments (BTC-spot on Binance, BTC-perp on Bybit, BTC-CFD on Capital) roll up to one asset row. alias_of_id supports LLM-proposed dedup without losing history.';

-- --------------------------------------------------------------------------
-- 3. instruments — add asset_id and venue_id FKs (nullable, populated by loader)
-- --------------------------------------------------------------------------
ALTER TABLE instruments
    ADD COLUMN IF NOT EXISTS asset_id INT REFERENCES assets(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS venue_id INT REFERENCES venues(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_instruments_asset ON instruments(asset_id) WHERE asset_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_instruments_venue ON instruments(venue_id) WHERE venue_id IS NOT NULL;

COMMENT ON COLUMN instruments.asset_id IS
    'Phase 14: FK to the logical asset. Nullable until backfill / loader populates. Candles keep joining on instrument_id exactly as before.';
COMMENT ON COLUMN instruments.venue_id IS
    'Phase 14: FK to the venue. Nullable until backfill / loader populates.';

-- --------------------------------------------------------------------------
-- 4. instrument_aliases — many-to-one lookup (ccxt symbol, tradingview, ISIN, FIGI)
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instrument_aliases (
    id              SERIAL PRIMARY KEY,
    instrument_id   BIGINT       NOT NULL REFERENCES instruments(id) ON DELETE CASCADE,
    alias_type      VARCHAR(24)  NOT NULL,               -- 'venue_native' | 'ccxt' | 'display' | 'tradingview' | 'isin' | 'figi'
    alias_value     VARCHAR(64)  NOT NULL,
    source          VARCHAR(24),                         -- 'loader', 'manual', 'llm'
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (alias_type, alias_value, instrument_id)
);
CREATE INDEX IF NOT EXISTS idx_aliases_value ON instrument_aliases(alias_value);
CREATE INDEX IF NOT EXISTS idx_aliases_type_value ON instrument_aliases(alias_type, alias_value);

COMMENT ON TABLE instrument_aliases IS
    'Phase 14: lookup table so "BTCUSDT", "BTC/USDT", "BTC-USDT-SWAP", "tradingview:BTCUSDT" all resolve to one instrument row.';

-- --------------------------------------------------------------------------
-- 5. v_asset_venues — arbitrage-friendly denormalised view
-- --------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_asset_venues AS
SELECT
    a.id                                    AS asset_id,
    a.symbol                                AS asset_symbol,
    a.display_name                          AS asset_name,
    a.asset_class,
    v.id                                    AS venue_id,
    v.code                                  AS venue_code,
    v.display_name                          AS venue_name,
    v.venue_type,
    v.adapter,
    v.priority                              AS venue_priority,
    i.id                                    AS instrument_id,
    i.symbol                                AS venue_symbol,
    i.min_size,
    i.size_increment,
    i.contract_multiplier,
    i.spread_pct,
    i.maker_fee_pct,
    i.taker_fee_pct,
    i.overnight_funding_long_pct,
    i.overnight_funding_short_pct,
    i.max_leverage,
    i.is_active                             AS instrument_active,
    v.is_active                             AS venue_active
FROM instruments i
JOIN assets a ON a.id = i.asset_id
JOIN venues v ON v.id = i.venue_id
WHERE i.is_active
  AND v.is_active
  AND a.alias_of_id IS NULL;

COMMENT ON VIEW v_asset_venues IS
    'Phase 14: one row per asset x venue for arbitrage price-spread analysis. Filters out inactive rows and asset aliases.';

-- --------------------------------------------------------------------------
-- 6. Seed the known venues (matches today's reality: binance, bybit, capital)
--    plus the ones we have adapters or free APIs for but have not wired yet.
-- --------------------------------------------------------------------------
INSERT INTO venues (code, display_name, venue_type, adapter, ccxt_id,
                    supports_spot, supports_perp, supports_margin, priority)
VALUES
    ('binance',   'Binance',           'crypto_cex',   'ccxt',     'binance',      true,  true,  true,  10),
    ('binanceus', 'Binance US',        'crypto_cex',   'ccxt',     'binanceus',    true,  false, false, 60),
    ('bybit',     'Bybit',             'crypto_cex',   'ccxt',     'bybit',        true,  true,  true,  20),
    ('okx',       'OKX',               'crypto_cex',   'ccxt',     'okx',          true,  true,  true,  30),
    ('coinbase',  'Coinbase Advanced', 'crypto_cex',   'ccxt',     'coinbase',     true,  false, false, 40),
    ('kraken',    'Kraken',            'crypto_cex',   'ccxt',     'kraken',       true,  true,  true,  50),
    ('capital',   'Capital.com CFDs',  'broker_cfd',   'capital',  NULL,           false, false, true,  70),
    ('alpaca',    'Alpaca US Equities','broker_equity','alpaca',   NULL,           true,  false, true,  80),
    ('yfinance',  'Yahoo Finance',     'data_only',    'yfinance', NULL,           true,  false, false, 200)
ON CONFLICT (code) DO UPDATE SET
    display_name    = EXCLUDED.display_name,
    venue_type      = EXCLUDED.venue_type,
    adapter         = EXCLUDED.adapter,
    ccxt_id         = EXCLUDED.ccxt_id,
    supports_spot   = EXCLUDED.supports_spot,
    supports_perp   = EXCLUDED.supports_perp,
    supports_margin = EXCLUDED.supports_margin,
    priority        = EXCLUDED.priority,
    updated_at      = CURRENT_TIMESTAMP;

-- --------------------------------------------------------------------------
-- 7. Backfill: create an asset row for every distinct base_currency already in
--    instruments, then wire asset_id and venue_id FKs on the 50 existing rows.
-- --------------------------------------------------------------------------

-- 7a. Seed assets from existing instruments.base_currency (crypto rows)
INSERT INTO assets (symbol, display_name, asset_class, auto_seeded)
SELECT DISTINCT
    UPPER(i.base_currency),
    UPPER(i.base_currency),
    i.asset_class,
    true
FROM instruments i
WHERE i.base_currency IS NOT NULL
  AND i.asset_class = 'crypto'
ON CONFLICT (symbol) DO NOTHING;

-- 7b. Seed assets for capital.com CFDs (symbol is already the ticker, base_currency is often null)
INSERT INTO assets (symbol, display_name, asset_class, auto_seeded, curation_notes)
SELECT DISTINCT
    UPPER(REGEXP_REPLACE(i.symbol, '[^A-Z0-9]', '', 'g')),
    i.symbol,
    i.asset_class,
    true,
    'Auto-seeded from Capital.com CFD; curate display_name via assets_cli or LLM review.'
FROM instruments i
WHERE i.exchange = 'capital'
ON CONFLICT (symbol) DO NOTHING;

-- 7c. Backfill instruments.venue_id
UPDATE instruments i
SET venue_id = v.id, updated_at = CURRENT_TIMESTAMP
FROM venues v
WHERE i.venue_id IS NULL
  AND v.code = i.exchange;

-- 7d. Backfill instruments.asset_id — crypto by base_currency
UPDATE instruments i
SET asset_id = a.id, updated_at = CURRENT_TIMESTAMP
FROM assets a
WHERE i.asset_id IS NULL
  AND i.asset_class = 'crypto'
  AND a.symbol = UPPER(i.base_currency);

-- 7e. Backfill instruments.asset_id — capital.com by symbol
UPDATE instruments i
SET asset_id = a.id, updated_at = CURRENT_TIMESTAMP
FROM assets a
WHERE i.asset_id IS NULL
  AND i.exchange = 'capital'
  AND a.symbol = UPPER(REGEXP_REPLACE(i.symbol, '[^A-Z0-9]', '', 'g'));

-- 7f. Seed instrument_aliases from existing venue-native symbols
INSERT INTO instrument_aliases (instrument_id, alias_type, alias_value, source)
SELECT i.id, 'venue_native', i.symbol, 'loader'
FROM instruments i
ON CONFLICT DO NOTHING;

COMMIT;

-- --------------------------------------------------------------------------
-- Post-migration sanity (run manually after apply)
-- --------------------------------------------------------------------------
-- SELECT 'venues'               AS table_name, COUNT(*) FROM venues
-- UNION ALL SELECT 'assets',               COUNT(*) FROM assets
-- UNION ALL SELECT 'instruments_total',    COUNT(*) FROM instruments
-- UNION ALL SELECT 'instruments_with_fk',  COUNT(*) FROM instruments WHERE asset_id IS NOT NULL AND venue_id IS NOT NULL
-- UNION ALL SELECT 'aliases',              COUNT(*) FROM instrument_aliases
-- UNION ALL SELECT 'v_asset_venues_rows',  COUNT(*) FROM v_asset_venues;
