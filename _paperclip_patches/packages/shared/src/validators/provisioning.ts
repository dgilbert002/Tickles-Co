import { z } from "zod";

// Phase 3 — Tickles provisioning job validators.
// Mirrors ProvisioningStep / ProvisioningMetadata in the Drizzle schema.

export const provisioningStepSchema = z.object({
  step: z.string().min(1),
  stepIndex: z.number().int().min(1).max(9),
  status: z.enum(["running", "ok", "skipped", "failed"]),
  detail: z.string().nullable().optional(),
  error: z.string().nullable().optional(),
  startedAt: z.string().datetime().nullable().optional(),
  finishedAt: z.string().datetime().nullable().optional(),
});

export const provisioningMetadataSchema = z
  .object({
    mem0Scopes: z
      .object({
        collection: z.string(),
        tier1AgentIdPrefix: z.string(),
        tier2SharedAgentId: z.string(),
        tier3Broadcast: z.string(),
      })
      .optional(),
    memuSubscriptions: z.array(z.string()).optional(),
    treasury: z
      .object({
        enabled: z.boolean(),
        ruleOneMode: z.enum(["advisory", "strict", "off"]),
        venues: z.array(z.string()),
        registeredAt: z.string(),
      })
      .optional(),
    pendingSkillInstalls: z.array(z.string()).optional(),
    pendingRoutines: z
      .array(z.object({ kind: z.string(), trigger: z.string() }))
      .optional(),
    hiredAgents: z
      .array(
        z.object({
          urlKey: z.string(),
          globalUrlKey: z.string().optional(),
          paperclipAgentId: z.string().nullable().optional(),
          openclawDir: z.string().nullable().optional(),
        }),
      )
      .optional(),
  })
  .passthrough();

export const createProvisioningJobSchema = z.object({
  templateId: z.string().min(1),
  slug: z.string().min(1),
});

export const appendProvisioningEventSchema = z.object({
  step: z.string().min(1),
  stepIndex: z.number().int().min(1).max(9),
  status: z.enum(["running", "ok", "skipped", "failed"]),
  detail: z.string().nullable().optional(),
  error: z.string().nullable().optional(),
  startedAt: z.string().datetime().nullable().optional(),
  finishedAt: z.string().datetime().nullable().optional(),
  metadataMerge: provisioningMetadataSchema.optional(),
  overallStatus: z.enum(["running", "ok", "partial", "failed"]).optional(),
});

export type CreateProvisioningJob = z.infer<typeof createProvisioningJobSchema>;
export type AppendProvisioningEvent = z.infer<typeof appendProvisioningEventSchema>;
