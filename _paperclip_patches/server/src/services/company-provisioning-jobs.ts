import { and, desc, eq, sql } from "drizzle-orm";
import type { Db } from "@paperclipai/db";
import {
  companies,
  companyProvisioningJobs,
  type CompanyProvisioningJob,
  type ProvisioningMetadata,
  type ProvisioningStep,
} from "@paperclipai/db";
import { notFound, unprocessable } from "../errors.js";

// Phase 3 — thin wrapper around `company_provisioning_jobs`.
//
// The Python provisioning executor drives this lifecycle:
//   create()                    → row with status=running, empty steps/metadata
//   appendEvent(jobId, event)   → pushes a step, shallow-merges metadata, may
//                                 flip overall_status + finished_at on the
//                                 terminal event
//   latestByCompany(companyId)  → what the UI polls for progress + final state
//
// Metadata merge is intentionally *shallow* so the executor can add new
// top-level keys (treasury/mem0Scopes/hiredAgents/...) without the server
// needing to understand their shape. Nested arrays get replaced wholesale —
// the executor sends the full final array when it wants to change one.

export interface AppendProvisioningEventInput {
  step: string;
  stepIndex: number;
  status: ProvisioningStep["status"];
  detail?: string | null;
  error?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  metadataMerge?: ProvisioningMetadata;
  overallStatus?: "running" | "ok" | "partial" | "failed";
}

async function assertCompanyExists(db: Db, companyId: string): Promise<void> {
  const row = await db
    .select({ id: companies.id })
    .from(companies)
    .where(eq(companies.id, companyId))
    .then((rows) => rows[0] ?? null);
  if (!row) throw notFound("Company not found");
}

async function loadJob(
  db: Db,
  companyId: string,
  jobId: string,
): Promise<CompanyProvisioningJob> {
  const row = await db
    .select()
    .from(companyProvisioningJobs)
    .where(
      and(
        eq(companyProvisioningJobs.id, jobId),
        eq(companyProvisioningJobs.companyId, companyId),
      ),
    )
    .then((rows) => rows[0] ?? null);
  if (!row) throw notFound("Provisioning job not found");
  return row as CompanyProvisioningJob;
}

export function companyProvisioningJobService(db: Db) {
  return {
    create: async (
      companyId: string,
      input: { templateId: string; slug: string },
    ): Promise<CompanyProvisioningJob> => {
      await assertCompanyExists(db, companyId);
      const [row] = await db
        .insert(companyProvisioningJobs)
        .values({
          companyId,
          templateId: input.templateId,
          slug: input.slug,
          overallStatus: "running",
          steps: [],
          metadata: {},
        })
        .returning();
      if (!row) throw unprocessable("Failed to create provisioning job");
      return row as CompanyProvisioningJob;
    },

    appendEvent: async (
      companyId: string,
      jobId: string,
      event: AppendProvisioningEventInput,
    ): Promise<CompanyProvisioningJob> => {
      const existing = await loadJob(db, companyId, jobId);

      const step: ProvisioningStep = {
        step: event.step,
        stepIndex: event.stepIndex,
        status: event.status,
        detail: event.detail ?? null,
        error: event.error ?? null,
        startedAt: event.startedAt ?? null,
        finishedAt: event.finishedAt ?? null,
      };

      const steps = [...(existing.steps ?? []), step];
      const metadata = {
        ...(existing.metadata ?? {}),
        ...(event.metadataMerge ?? {}),
      } as ProvisioningMetadata;

      const updates: Partial<typeof companyProvisioningJobs.$inferInsert> = {
        steps,
        metadata,
        updatedAt: new Date(),
      };
      if (event.overallStatus) {
        updates.overallStatus = event.overallStatus;
        if (event.overallStatus !== "running") {
          updates.finishedAt = new Date();
        }
      }

      const [row] = await db
        .update(companyProvisioningJobs)
        .set(updates)
        .where(
          and(
            eq(companyProvisioningJobs.id, jobId),
            eq(companyProvisioningJobs.companyId, companyId),
          ),
        )
        .returning();
      if (!row) throw unprocessable("Failed to append provisioning event");
      return row as CompanyProvisioningJob;
    },

    latestByCompany: async (
      companyId: string,
    ): Promise<CompanyProvisioningJob | null> => {
      const row = await db
        .select()
        .from(companyProvisioningJobs)
        .where(eq(companyProvisioningJobs.companyId, companyId))
        .orderBy(desc(companyProvisioningJobs.startedAt))
        .limit(1)
        .then((rows) => rows[0] ?? null);
      return (row as CompanyProvisioningJob) ?? null;
    },

    getById: async (
      companyId: string,
      jobId: string,
    ): Promise<CompanyProvisioningJob> => loadJob(db, companyId, jobId),

    listByCompany: async (
      companyId: string,
      limit = 20,
    ): Promise<CompanyProvisioningJob[]> => {
      const rows = await db
        .select()
        .from(companyProvisioningJobs)
        .where(eq(companyProvisioningJobs.companyId, companyId))
        .orderBy(desc(companyProvisioningJobs.startedAt))
        .limit(Math.max(1, Math.min(100, limit)));
      return rows as CompanyProvisioningJob[];
    },

    countRunning: async (companyId: string): Promise<number> => {
      const [row] = await db
        .select({ count: sql<number>`count(*)::int` })
        .from(companyProvisioningJobs)
        .where(
          and(
            eq(companyProvisioningJobs.companyId, companyId),
            eq(companyProvisioningJobs.overallStatus, "running"),
          ),
        );
      return row?.count ?? 0;
    },
  };
}

export type CompanyProvisioningJobService = ReturnType<typeof companyProvisioningJobService>;
