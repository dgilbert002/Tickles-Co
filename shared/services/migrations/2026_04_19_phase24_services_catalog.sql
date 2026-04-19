-- Phase 24: Services Catalog
--
-- Persistent, queryable catalog of every long-running Tickles service.
-- Joins the in-process Phase 22 SERVICE_REGISTRY with observed runtime
-- state (systemd active/sub state) and the Phase 21 auditor heartbeat
-- stream so a dashboard (or a human with psql) can answer:
--
--   * what services *can* we run?
--   * what services are enabled on this VPS?
--   * which ones last heartbeated, when, and at what severity?
--
-- Fully idempotent — safe to re-run.
BEGIN;

CREATE TABLE IF NOT EXISTS public.services_catalog (
    name                         text        PRIMARY KEY,
    kind                         text        NOT NULL,
    module                       text        NOT NULL,
    description                  text        NOT NULL DEFAULT '',
    systemd_unit                 text        NOT NULL,
    enabled_on_vps               boolean     NOT NULL DEFAULT false,
    has_factory                  boolean     NOT NULL DEFAULT false,
    phase                        text,
    tags                         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    first_registered_at          timestamptz NOT NULL DEFAULT now(),
    last_seen_at                 timestamptz NOT NULL DEFAULT now(),
    last_systemd_state           text,
    last_systemd_substate        text,
    last_systemd_active_enter_ts timestamptz,
    last_heartbeat_ts            timestamptz,
    last_heartbeat_severity      text,
    metadata                     jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_services_catalog_kind
    ON public.services_catalog (kind);

CREATE INDEX IF NOT EXISTS idx_services_catalog_enabled
    ON public.services_catalog (enabled_on_vps);

CREATE INDEX IF NOT EXISTS idx_services_catalog_last_heartbeat
    ON public.services_catalog (last_heartbeat_ts DESC NULLS LAST);

-- Append-only snapshot history so operators can see state transitions
-- (e.g. active -> failed -> active) without scraping journalctl.
CREATE TABLE IF NOT EXISTS public.services_catalog_snapshots (
    id                        bigserial PRIMARY KEY,
    name                      text        NOT NULL,
    ts                        timestamptz NOT NULL DEFAULT now(),
    systemd_state             text,
    systemd_substate          text,
    systemd_active_enter_ts   timestamptz,
    last_heartbeat_ts         timestamptz,
    last_heartbeat_severity   text,
    metadata                  jsonb       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_services_catalog_snapshots_name_ts
    ON public.services_catalog_snapshots (name, ts DESC);

-- Convenient "current state" view for dashboards.
CREATE OR REPLACE VIEW public.services_catalog_current AS
SELECT
    name,
    kind,
    module,
    description,
    systemd_unit,
    enabled_on_vps,
    has_factory,
    phase,
    tags,
    last_seen_at,
    last_systemd_state,
    last_systemd_substate,
    last_systemd_active_enter_ts,
    last_heartbeat_ts,
    last_heartbeat_severity,
    CASE
        WHEN last_heartbeat_ts IS NULL THEN 'no-heartbeat'
        WHEN last_heartbeat_ts < now() - interval '5 minutes' THEN 'stale'
        WHEN last_heartbeat_severity IN ('breach','critical') THEN 'degraded'
        WHEN last_heartbeat_severity = 'warning' THEN 'warning'
        ELSE 'healthy'
    END AS health
FROM public.services_catalog;

COMMIT;
