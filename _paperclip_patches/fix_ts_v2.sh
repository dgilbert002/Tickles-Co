#!/usr/bin/env bash
# Second-pass cast fix: use Record<string, string> as the universal shape.
set -euo pipefail
F=/home/paperclip/paperclip/server/src/routes/company-provisioning-jobs.ts
sudo python3 <<'PY'
import pathlib
p = pathlib.Path("/home/paperclip/paperclip/server/src/routes/company-provisioning-jobs.ts")
src = p.read_text()
new = src
new = new.replace(
    "(req.params as { companyId: string }).companyId",
    "((req.params as unknown) as Record<string, string>).companyId",
)
new = new.replace(
    "(req.params as { jobId: string }).jobId",
    "((req.params as unknown) as Record<string, string>).jobId",
)
if new != src:
    p.write_text(new)
    print("[fix] updated; companyId casts:",
          new.count("((req.params as unknown) as Record<string, string>).companyId"),
          "jobId casts:",
          new.count("((req.params as unknown) as Record<string, string>).jobId"))
else:
    print("[skip] nothing to change")
PY
echo "== verify compile =="
sudo -u paperclip bash -c "cd /home/paperclip/paperclip/server && ./node_modules/.bin/tsc --noEmit" 2>&1 | head -20 || true
