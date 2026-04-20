#!/usr/bin/env bash
# Fix TS2339 errors in company-provisioning-jobs.ts by casting req.params
# explicitly. Idempotent.
set -euo pipefail

F=/home/paperclip/paperclip/server/src/routes/company-provisioning-jobs.ts
sudo cp "$F" "${F}.bak-$(date +%Y%m%d-%H%M%S)"

sudo python3 <<'PY'
import pathlib, re
p = pathlib.Path("/home/paperclip/paperclip/server/src/routes/company-provisioning-jobs.ts")
src = p.read_text()
# Replace `req.params.companyId as string` with a typed cast.
new = re.sub(
    r"req\.params\.companyId as string",
    "(req.params as { companyId: string }).companyId",
    src,
)
# Replace `req.params.jobId as string` with a typed cast (route has :jobId).
new = re.sub(
    r"req\.params\.jobId as string",
    "(req.params as { jobId: string }).jobId",
    new,
)
changes = (src != new)
if changes:
    p.write_text(new)
    # count occurrences in new file
    print("[fix] req.params.companyId casts:", new.count("(req.params as { companyId: string }).companyId"))
    print("[fix] req.params.jobId casts:",    new.count("(req.params as { jobId: string }).jobId"))
else:
    print("[skip] already patched")
PY
echo "== verify compile =="
sudo -u paperclip bash -c "cd /home/paperclip/paperclip/server && ./node_modules/.bin/tsc --noEmit 2>&1 | head -30 || true"
