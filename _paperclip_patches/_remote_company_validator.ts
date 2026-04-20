import { z } from "zod";
import { COMPANY_STATUSES } from "../constants.js";

const logoAssetIdSchema = z.string().uuid().nullable().optional();
const brandColorSchema = z.string().regex(/^#[0-9a-fA-F]{6}$/).nullable().optional();
const feedbackDataSharingTermsVersionSchema = z.string().min(1).nullable().optional();

// Phase 3 — Tickles provisioning opt-in block.
//
// When `provisioning.enabled` is true the POST /api/companies handler will
// insert a `company_provisioning_jobs` row and fire-and-forget the MCP
// `company.provision` call that runs the 9-step executor. Defaults here
// match the UI wizard defaults: Blank template, advisory Rule-1, no MemU
// subscriptions beyond the template's own defaults.
export const companyProvisioningRequestSchema = z
  .object({
    enabled: z.boolean().default(false),
    template: z.string().min(1).default("blank"),
    slug: z
      .string()
      .regex(/^[a-z0-9][a-z0-9_]*$/, "slug must be lowercase alphanumeric/underscore")
      .optional(),
    ruleOneMode: z.enum(["advisory", "strict", "off"]).default("advisory"),
    memuSubscriptions: z.array(z.string().min(1)).optional(),
  })
  .optional();

export type CompanyProvisioningRequest = z.infer<
  typeof companyProvisioningRequestSchema
>;

export const createCompanySchema = z.object({
  name: z.string().min(1),
  description: z.string().optional().nullable(),
  budgetMonthlyCents: z.number().int().nonnegative().optional().default(0),
  provisioning: companyProvisioningRequestSchema,
});

export type CreateCompany = z.infer<typeof createCompanySchema>;

export const updateCompanySchema = createCompanySchema
  .partial()
  .extend({
    status: z.enum(COMPANY_STATUSES).optional(),
    spentMonthlyCents: z.number().int().nonnegative().optional(),
    requireBoardApprovalForNewAgents: z.boolean().optional(),
    feedbackDataSharingEnabled: z.boolean().optional(),
    feedbackDataSharingConsentAt: z.coerce.date().nullable().optional(),
    feedbackDataSharingConsentByUserId: z.string().min(1).nullable().optional(),
    feedbackDataSharingTermsVersion: feedbackDataSharingTermsVersionSchema,
    brandColor: brandColorSchema,
    logoAssetId: logoAssetIdSchema,
  });

export type UpdateCompany = z.infer<typeof updateCompanySchema>;

export const updateCompanyBrandingSchema = z
  .object({
    name: z.string().min(1).optional(),
    description: z.string().nullable().optional(),
    brandColor: brandColorSchema,
    logoAssetId: logoAssetIdSchema,
  })
  .strict()
  .refine(
    (value) =>
      value.name !== undefined
      || value.description !== undefined
      || value.brandColor !== undefined
      || value.logoAssetId !== undefined,
    "At least one branding field must be provided",
  );

export type UpdateCompanyBranding = z.infer<typeof updateCompanyBrandingSchema>;
