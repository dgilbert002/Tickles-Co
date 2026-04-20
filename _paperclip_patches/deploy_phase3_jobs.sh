#!/usr/bin/env bash
# Phase-3 deployment: company_provisioning_jobs table + routes + validators.
# Idempotent — safe to re-run.
set -euo pipefail

PAPERCLIP="/home/paperclip/paperclip"
STAGE="/tmp/phase3_jobs"

echo "[1/8] Staged files present at ${STAGE}:"
ls -la "${STAGE}"

echo "[2/8] Installing schema + migration files…"
sudo -u paperclip cp "${STAGE}/company_provisioning_jobs.ts" \
  "${PAPERCLIP}/packages/db/src/schema/company_provisioning_jobs.ts"
sudo -u paperclip cp "${STAGE}/0055_company_provisioning_jobs.sql" \
  "${PAPERCLIP}/packages/db/src/migrations/0055_company_provisioning_jobs.sql"

echo "[3/8] Appending schema export (idempotent)…"
SCHEMA_IDX="${PAPERCLIP}/packages/db/src/schema/index.ts"
if ! grep -q 'company_provisioning_jobs.js' "${SCHEMA_IDX}"; then
  sudo -u paperclip tee -a "${SCHEMA_IDX}" >/dev/null <<'EOT'
export {
  companyProvisioningJobs,
  type CompanyProvisioningJob,
  type NewCompanyProvisioningJob,
  type ProvisioningStep,
  type ProvisioningMetadata,
} from "./company_provisioning_jobs.js";
EOT
  echo "    appended"
else
  echo "    already present"
fi

echo "[4/8] Updating _journal.json (idempotent)…"
JOURNAL="${PAPERCLIP}/packages/db/src/migrations/meta/_journal.json"
sudo -u paperclip python3 - "$JOURNAL" <<'PY'
import json, sys
p = sys.argv[1]
with open(p) as f:
    d = json.load(f)
tag = "0055_company_provisioning_jobs"
if any(e.get("tag") == tag for e in d["entries"]):
    print("    already present")
    sys.exit(0)
max_idx = max(e["idx"] for e in d["entries"])
# 2026-04-10 23:00:00 UTC
d["entries"].append({
    "idx": max_idx + 1,
    "version": "7",
    "when": 1776078000000,
    "tag": tag,
    "breakpoints": True,
})
with open(p, "w") as f:
    json.dump(d, f, indent=2)
print(f"    appended idx={max_idx+1}")
PY

echo "[5/8] Installing validators (shared/provisioning.ts + updated company.ts)…"
sudo -u paperclip cp "${STAGE}/provisioning_validator.ts" \
  "${PAPERCLIP}/packages/shared/src/validators/provisioning.ts"
sudo -u paperclip cp "${STAGE}/company_validator.ts" \
  "${PAPERCLIP}/packages/shared/src/validators/company.ts"

VAL_IDX="${PAPERCLIP}/packages/shared/src/validators/index.ts"
if ! grep -q 'createProvisioningJobSchema' "${VAL_IDX}"; then
  sudo -u paperclip tee -a "${VAL_IDX}" >/dev/null <<'EOT'

export {
  provisioningStepSchema,
  provisioningMetadataSchema,
  createProvisioningJobSchema,
  appendProvisioningEventSchema,
  type CreateProvisioningJob,
  type AppendProvisioningEvent,
} from "./provisioning.js";

export {
  companyProvisioningRequestSchema,
  type CompanyProvisioningRequest,
} from "./company.js";
EOT
  echo "    appended validator exports"
else
  echo "    validator exports already present"
fi

echo "[6/8] Installing server service + routes…"
sudo -u paperclip cp "${STAGE}/company-provisioning-jobs-service.ts" \
  "${PAPERCLIP}/server/src/services/company-provisioning-jobs.ts"
sudo -u paperclip cp "${STAGE}/company-provisioning-jobs-routes.ts" \
  "${PAPERCLIP}/server/src/routes/company-provisioning-jobs.ts"
sudo -u paperclip cp "${STAGE}/companies-routes.ts" \
  "${PAPERCLIP}/server/src/routes/companies.ts"

SVC_IDX="${PAPERCLIP}/server/src/services/index.ts"
if ! grep -q 'companyProvisioningJobService' "${SVC_IDX}"; then
  sudo -u paperclip tee -a "${SVC_IDX}" >/dev/null <<'EOT'
export { companyProvisioningJobService } from "./company-provisioning-jobs.js";
EOT
  echo "    appended service export"
else
  echo "    service export already present"
fi

echo "[7/8] Restarting paperclip.service (auto-applies migration)…"
sudo systemctl restart paperclip
sleep 4
sudo journalctl -u paperclip --since '30 seconds ago' --no-pager | tail -40

echo "[8/8] Verifying table exists…"
sudo -u paperclip /home/paperclip/paperclip/node_modules/.pnpm/@embedded-postgres+linux-x64@18.1.0-beta.16/node_modules/@embedded-postgres/linux-x64/native/bin/psql \
  -h 127.0.0.1 -p 54329 -U paperclip -d paperclip \
  -c "\d company_provisioning_jobs" 2>&1 | head -30 || true

echo "=== DONE ==="
