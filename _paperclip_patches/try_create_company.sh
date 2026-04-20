#!/bin/bash
set +e
API=http://127.0.0.1:3100

echo "=== 1. Find the companyPortabilityImportSchema / createCompanySchema files ==="
grep -RIln "export const createCompanySchema\|companyPortabilityImportSchema\|companyPortabilityPreviewSchema" /home/paperclip/paperclip/server/src --include="*.ts" 2>/dev/null
echo
for f in $(grep -RIl "createCompanySchema" /home/paperclip/paperclip/server/src --include="*.ts" 2>/dev/null); do
  if grep -q "createCompanySchema.*=.*z\.\|createCompanySchema.*=.*zod\." "$f"; then
    echo "--- $f ---"
    awk '/createCompanySchema/,/^}\);?$/' "$f" | head -40
  fi
done
echo
echo "=== 2. POST /api/companies with minimal body to learn required fields ==="
curl -s -X POST -H "Content-Type: application/json" -d '{}' $API/api/companies | head -c 600; echo
echo
curl -s -X POST -H "Content-Type: application/json" -d '{"name":"Rubicon Test"}' $API/api/companies | head -c 600; echo
echo
echo "=== 3. Any example company packages in the repo? ==="
find /home/paperclip/paperclip -maxdepth 5 -name "COMPANY.md" 2>/dev/null | head -10
find /home/paperclip/paperclip -type d -name "companies" 2>/dev/null | head -10
find /home/paperclip/paperclip -maxdepth 6 -name "AGENTS.md" 2>/dev/null | head -10
echo
echo "=== 4. companies routes definition (POST / handler body) ==="
sed -n '325,410p' /home/paperclip/paperclip/server/src/routes/companies.ts
echo
echo "=== 5. agents.ts routes (is there a direct agent POST route?) ==="
grep -RIn "router\.\(get\|post\|put\|patch\|delete\)" /home/paperclip/paperclip/server/src/routes/agents.ts 2>/dev/null | head -20
