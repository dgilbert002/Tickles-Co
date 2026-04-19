-- Phase 28: Crash Protection
--
-- Crash Protection is the cross-cutting safety layer. It pulls
-- signals from the Regime Service (Phase 27), the Banker/Treasury
-- (Phase 25), and the Execution Layer (Phase 26) and emits:
--
--   * halt_new_orders  — Treasury/Router must reject new intents
--                        that fall inside the affected scope.
--   * flatten_positions — flag: strategies should exit positions.
--   * alert             — audit-only, no blocking action.
--
-- Everything is append-only. Operators and the dashboard read from
-- the active-events view.
--
-- Ownership: tickles_shared.
-- Rollback script is at the bottom of this file (commented out).

BEGIN;

-- --------------------------------------------------------------------
-- crash_protection_rules
--
-- Operator-configured trigger rules. Scope selects which positions /
-- universes the rule applies to. Any NULL column means "any".
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.crash_protection_rules (
    id                      BIGSERIAL PRIMARY KEY,
    company_id              TEXT,                      -- NULL = all companies
    universe                TEXT,                      -- NULL = any universe
    exchange                TEXT,                      -- NULL = any exchange
    symbol                  TEXT,                      -- NULL = any symbol
    rule_type               TEXT NOT NULL,             -- regime_crash / equity_drawdown / position_notional / daily_loss / stale_data
    action                  TEXT NOT NULL,             -- halt_new_orders / flatten_positions / alert
    threshold               NUMERIC(20, 8),            -- interpretation depends on rule_type
    params                  JSONB NOT NULL DEFAULT '{}'::JSONB,
    severity                TEXT NOT NULL DEFAULT 'warning',  -- info / warning / error / critical
    enabled                 BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS crash_rules_lookup_idx
    ON public.crash_protection_rules (company_id, universe, exchange, symbol, rule_type)
    WHERE enabled;
CREATE INDEX IF NOT EXISTS crash_rules_type_idx
    ON public.crash_protection_rules (rule_type, enabled);

-- --------------------------------------------------------------------
-- crash_protection_events
--
-- Append-only. One row per rule-triggered OR rule-resolved event.
-- Downstream services (Treasury, ExecutionRouter) query the derived
-- view `crash_protection_active` to check whether they should halt.
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.crash_protection_events (
    id                      BIGSERIAL PRIMARY KEY,
    rule_id                 BIGINT REFERENCES public.crash_protection_rules(id),
    company_id              TEXT,
    universe                TEXT,
    exchange                TEXT,
    symbol                  TEXT,
    rule_type               TEXT NOT NULL,
    action                  TEXT NOT NULL,
    status                  TEXT NOT NULL,              -- triggered / resolved / overridden
    severity                TEXT NOT NULL,              -- info / warning / error / critical
    reason                  TEXT,
    metric                  NUMERIC(20, 8),
    threshold               NUMERIC(20, 8),
    metadata                JSONB NOT NULL DEFAULT '{}'::JSONB,
    ts                      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS crash_events_scope_status_idx
    ON public.crash_protection_events (company_id, universe, exchange, symbol, status, ts DESC);
CREATE INDEX IF NOT EXISTS crash_events_rule_ts_idx
    ON public.crash_protection_events (rule_id, ts DESC);
CREATE INDEX IF NOT EXISTS crash_events_status_ts_idx
    ON public.crash_protection_events (status, ts DESC);

-- --------------------------------------------------------------------
-- crash_protection_active view
--
-- For every (rule_id, company_id, universe, exchange, symbol) scope
-- we return the *latest* event. Consumers treat rows with
-- status='triggered' as currently blocking; 'resolved' rows are
-- informational (the guardrail cleared itself).
-- --------------------------------------------------------------------
CREATE OR REPLACE VIEW public.crash_protection_active AS
SELECT DISTINCT ON (
    COALESCE(rule_id, 0),
    COALESCE(company_id, ''),
    COALESCE(universe, ''),
    COALESCE(exchange, ''),
    COALESCE(symbol, '')
)
    id, rule_id, company_id, universe, exchange, symbol,
    rule_type, action, status, severity, reason, metric, threshold,
    metadata, ts
FROM public.crash_protection_events
ORDER BY
    COALESCE(rule_id, 0),
    COALESCE(company_id, ''),
    COALESCE(universe, ''),
    COALESCE(exchange, ''),
    COALESCE(symbol, ''),
    ts DESC;

COMMIT;

-- ROLLBACK (manual):
-- BEGIN;
--   DROP VIEW  IF EXISTS public.crash_protection_active;
--   DROP TABLE IF EXISTS public.crash_protection_events;
--   DROP TABLE IF EXISTS public.crash_protection_rules;
-- COMMIT;
