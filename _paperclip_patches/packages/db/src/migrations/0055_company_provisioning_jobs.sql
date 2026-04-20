-- Phase 3 — Tickles company provisioning jobs.
-- Tracks each run of the 9-step executor so the UI can poll progress and the
-- platform has a persistent record of what was provisioned (mem0 scopes,
-- MemU subscriptions, Treasury config, hired agents, pending skill installs).

CREATE TABLE IF NOT EXISTS "company_provisioning_jobs" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
  "company_id" uuid NOT NULL REFERENCES "companies"("id") ON DELETE cascade,
  "template_id" text NOT NULL,
  "slug" text NOT NULL,
  "overall_status" text NOT NULL DEFAULT 'running',
  "steps" jsonb NOT NULL DEFAULT '[]'::jsonb,
  "metadata" jsonb NOT NULL DEFAULT '{}'::jsonb,
  "started_at" timestamptz(3) NOT NULL DEFAULT NOW(),
  "finished_at" timestamptz(3),
  "created_at" timestamptz(3) NOT NULL DEFAULT NOW(),
  "updated_at" timestamptz(3) NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS "company_provisioning_jobs_company_started_idx"
  ON "company_provisioning_jobs" ("company_id", "started_at" DESC);

CREATE INDEX IF NOT EXISTS "company_provisioning_jobs_status_idx"
  ON "company_provisioning_jobs" ("overall_status");
