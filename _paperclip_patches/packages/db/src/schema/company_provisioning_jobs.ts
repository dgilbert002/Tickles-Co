import { index, jsonb, pgTable, text, timestamp, uuid } from "drizzle-orm/pg-core";
import { companies } from "./companies.js";

// Phase-3 — Tickles provisioning job tracker.
//
// One row per provisioning *run*. The latest row per `companyId` is the
// canonical "current provisioning state" — UI and services read from there.
//
// The Python executor in `shared/provisioning/` owns the lifecycle:
//   1. POST /api/companies/:cid/provisioning-jobs              → creates row (status=running)
//   2. POST /api/companies/:cid/provisioning-jobs/:id/events   → appends to `steps`, merges metadata
//   3. (final event) sets `overallStatus` + `finishedAt`
//
// `metadata` is a free-form bag of per-step decisions (mem0 scopes, MemU
// subscriptions, treasury config, pending skill installs, pending routines).
// Typed loosely on purpose so the executor can add new metadata keys in
// later phases without a schema migration.

export type ProvisioningStep = {
  step: string;
  stepIndex: number;
  status: "running" | "ok" | "skipped" | "failed";
  detail?: string | null;
  error?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
};

export type ProvisioningMetadata = {
  mem0Scopes?: {
    collection: string;
    tier1AgentIdPrefix: string;
    tier2SharedAgentId: string;
    tier3Broadcast: string;
  };
  memuSubscriptions?: string[];
  treasury?: {
    enabled: boolean;
    ruleOneMode: "advisory" | "strict" | "off";
    venues: string[];
    registeredAt: string;
  };
  pendingSkillInstalls?: string[];
  pendingRoutines?: Array<{ kind: string; trigger: string }>;
  hiredAgents?: Array<{
    urlKey: string;
    globalUrlKey?: string;
    paperclipAgentId?: string | null;
    openclawDir?: string | null;
  }>;
};

export const companyProvisioningJobs = pgTable(
  "company_provisioning_jobs",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    companyId: uuid("company_id")
      .notNull()
      .references(() => companies.id, { onDelete: "cascade" }),
    templateId: text("template_id").notNull(),
    slug: text("slug").notNull(),
    overallStatus: text("overall_status").notNull().default("running"),
    steps: jsonb("steps").$type<ProvisioningStep[]>().notNull().default([]),
    metadata: jsonb("metadata").$type<ProvisioningMetadata>().notNull().default({}),
    startedAt: timestamp("started_at", { withTimezone: true }).notNull().defaultNow(),
    finishedAt: timestamp("finished_at", { withTimezone: true }),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
    updatedAt: timestamp("updated_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (table) => ({
    companyStartedIdx: index("company_provisioning_jobs_company_started_idx").on(
      table.companyId,
      table.startedAt.desc(),
    ),
    statusIdx: index("company_provisioning_jobs_status_idx").on(table.overallStatus),
  }),
);

export type CompanyProvisioningJob = typeof companyProvisioningJobs.$inferSelect;
export type NewCompanyProvisioningJob = typeof companyProvisioningJobs.$inferInsert;
