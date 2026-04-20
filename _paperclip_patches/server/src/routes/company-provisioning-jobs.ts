import { Router } from "express";
import type { Db } from "@paperclipai/db";
import {
  appendProvisioningEventSchema,
  createProvisioningJobSchema,
} from "@paperclipai/shared";
import { validate } from "../middleware/validate.js";
import {
  companyProvisioningJobService,
  logActivity,
} from "../services/index.js";
import { assertBoard, assertCompanyAccess } from "./authz.js";

// Phase 3 — Tickles provisioning job HTTP surface.
//
// These routes are mounted from `companies.ts` under
// `/api/companies/:companyId/provisioning-jobs` and are called by the Python
// executor in shared/provisioning/ (running on the VPS next to the MCP
// daemon). A light read endpoint also exists at /latest for the UI poller.

export function companyProvisioningJobRoutes(db: Db) {
  const router = Router({ mergeParams: true });
  const svc = companyProvisioningJobService(db);

  router.post(
    "/",
    validate(createProvisioningJobSchema),
    async (req, res) => {
      const companyId = req.params.companyId as string;
      assertCompanyAccess(req, companyId);
      assertBoard(req);
      const body = req.body as { templateId: string; slug: string };
      const job = await svc.create(companyId, body);
      await logActivity(db, {
        companyId,
        actorType: "user",
        actorId: req.actor.userId ?? "board",
        action: "company.provisioning.started",
        entityType: "company",
        entityId: companyId,
        details: { jobId: job.id, templateId: body.templateId, slug: body.slug },
      });
      res.status(201).json(job);
    },
  );

  router.post(
    "/:jobId/events",
    validate(appendProvisioningEventSchema),
    async (req, res) => {
      const companyId = req.params.companyId as string;
      const jobId = req.params.jobId as string;
      assertCompanyAccess(req, companyId);
      assertBoard(req);
      const job = await svc.appendEvent(companyId, jobId, req.body);
      if (req.body?.overallStatus && req.body.overallStatus !== "running") {
        await logActivity(db, {
          companyId,
          actorType: "user",
          actorId: req.actor.userId ?? "board",
          action: "company.provisioning.finished",
          entityType: "company",
          entityId: companyId,
          details: {
            jobId,
            overallStatus: req.body.overallStatus,
            lastStep: req.body.step,
          },
        });
      }
      res.json(job);
    },
  );

  router.get("/latest", async (req, res) => {
    const companyId = req.params.companyId as string;
    assertCompanyAccess(req, companyId);
    const job = await svc.latestByCompany(companyId);
    if (!job) {
      res.status(404).json({ error: "No provisioning jobs for this company" });
      return;
    }
    res.json(job);
  });

  router.get("/", async (req, res) => {
    const companyId = req.params.companyId as string;
    assertCompanyAccess(req, companyId);
    const limit = Number.parseInt(String(req.query.limit ?? "20"), 10);
    const jobs = await svc.listByCompany(
      companyId,
      Number.isFinite(limit) ? limit : 20,
    );
    res.json(jobs);
  });

  router.get("/:jobId", async (req, res) => {
    const companyId = req.params.companyId as string;
    const jobId = req.params.jobId as string;
    assertCompanyAccess(req, companyId);
    const job = await svc.getById(companyId, jobId);
    res.json(job);
  });

  return router;
}
