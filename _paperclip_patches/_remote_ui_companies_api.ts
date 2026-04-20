import type {
  Company,
  CompanyPortabilityExportRequest,
  CompanyPortabilityExportPreviewResult,
  CompanyPortabilityExportResult,
  CompanyPortabilityImportRequest,
  CompanyPortabilityImportResult,
  CompanyPortabilityPreviewRequest,
  CompanyPortabilityPreviewResult,
  UpdateCompanyBranding,
} from "@paperclipai/shared";
import { api } from "./client";

export type CompanyStats = Record<string, { agentCount: number; issueCount: number }>;

// Phase-3 — Tickles provisioning surface on top of company create.
// Shape mirrors companyProvisioningRequestSchema in shared/validators/company.ts
// and the company_provisioning_jobs row the server seeds when `enabled: true`.
export type CompanyProvisioningRequest = {
  enabled: boolean;
  template?: string;
  slug?: string;
  ruleOneMode?: "advisory" | "strict" | "off";
  memuSubscriptions?: string[];
};

export type CreateCompanyResponse = Company & {
  provisioningJobId?: string | null;
};

export type ProvisioningStep = {
  step: string;
  stepIndex: number;
  status: "running" | "ok" | "skipped" | "failed";
  detail?: string | null;
  error?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
};

export type ProvisioningJob = {
  id: string;
  companyId: string;
  templateId: string;
  slug: string;
  overallStatus: "running" | "ok" | "partial" | "failed";
  steps: ProvisioningStep[];
  metadata: Record<string, unknown>;
  startedAt: string;
  finishedAt: string | null;
  createdAt: string;
  updatedAt: string;
};

export type ProvisioningStatus =
  | { status: "not_provisioned" }
  | { status: "job"; job: ProvisioningJob };

export const companiesApi = {
  list: () => api.get<Company[]>("/companies"),
  get: (companyId: string) => api.get<Company>(`/companies/${companyId}`),
  stats: () => api.get<CompanyStats>("/companies/stats"),
  create: (data: {
    name: string;
    description?: string | null;
    budgetMonthlyCents?: number;
    provisioning?: CompanyProvisioningRequest;
  }) =>
    api.post<CreateCompanyResponse>("/companies", data),
  provisioningStatus: (companyId: string) =>
    api.get<ProvisioningStatus>(`/companies/${companyId}/provisioning-status`),
  provisioningJobs: (companyId: string) =>
    api.get<ProvisioningJob[]>(`/companies/${companyId}/provisioning-jobs`),
  update: (
    companyId: string,
    data: Partial<
      Pick<
        Company,
        | "name"
        | "description"
        | "status"
        | "budgetMonthlyCents"
        | "requireBoardApprovalForNewAgents"
        | "feedbackDataSharingEnabled"
        | "brandColor"
        | "logoAssetId"
      >
    >,
  ) => api.patch<Company>(`/companies/${companyId}`, data),
  updateBranding: (companyId: string, data: UpdateCompanyBranding) =>
    api.patch<Company>(`/companies/${companyId}/branding`, data),
  archive: (companyId: string) => api.post<Company>(`/companies/${companyId}/archive`, {}),
  remove: (companyId: string) => api.delete<{ ok: true }>(`/companies/${companyId}`),
  exportBundle: (
    companyId: string,
    data: CompanyPortabilityExportRequest,
  ) =>
    api.post<CompanyPortabilityExportResult>(`/companies/${companyId}/export`, data),
  exportPreview: (
    companyId: string,
    data: CompanyPortabilityExportRequest,
  ) =>
    api.post<CompanyPortabilityExportPreviewResult>(`/companies/${companyId}/exports/preview`, data),
  exportPackage: (
    companyId: string,
    data: CompanyPortabilityExportRequest,
  ) =>
    api.post<CompanyPortabilityExportResult>(`/companies/${companyId}/exports`, data),
  importPreview: (data: CompanyPortabilityPreviewRequest) =>
    api.post<CompanyPortabilityPreviewResult>("/companies/import/preview", data),
  importBundle: (data: CompanyPortabilityImportRequest) =>
    api.post<CompanyPortabilityImportResult>("/companies/import", data),
};
