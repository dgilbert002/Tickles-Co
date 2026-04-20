#!/bin/bash
set +e
echo "=== createCompanySchema ==="
grep -RIn "createCompanySchema" /home/paperclip/paperclip/server/src --include="*.ts" | head -5
FILE=$(grep -RIl "export const createCompanySchema\|export.*createCompanySchema.*=.*z\." /home/paperclip/paperclip/server/src --include="*.ts" 2>/dev/null | head -1)
echo "schema file: $FILE"
[ -n "$FILE" ] && sed -n '1,80p' "$FILE" | grep -A 40 "createCompanySchema"
echo
echo "=== agents service create signature ==="
FILE=$(find /home/paperclip/paperclip/server/src/services -name "agents.ts" | head -1)
echo "agents.ts: $FILE"
[ -n "$FILE" ] && grep -n "create\s*:\|create =\|async function create\|create(companyId" "$FILE" | head -10
echo "--- first 'create' function body ---"
[ -n "$FILE" ] && awk '/create\s*[:=]/{found=1} found{print; if(/^  }/){exit}}' "$FILE" | head -80
echo
echo "=== approvals create (hire_agent) ==="
awk 'NR>=90 && NR<=200' /home/paperclip/paperclip/server/src/services/approvals.ts
echo
echo "=== createApprovalSchema ==="
grep -RIl "createApprovalSchema" /home/paperclip/paperclip/server/src --include="*.ts" 2>/dev/null
FILE=$(grep -RIl "export const createApprovalSchema\|createApprovalSchema.*=.*z\." /home/paperclip/paperclip/server/src --include="*.ts" 2>/dev/null | head -1)
[ -n "$FILE" ] && grep -B 1 -A 40 "createApprovalSchema" "$FILE" | head -80
echo
echo "=== agentsSvc create schema (find what fields agents.create expects) ==="
grep -RIn "agents.\$inferInsert\|typeof agents" /home/paperclip/paperclip/server/src --include="*.ts" | head -10
echo
echo "=== agents schema (db definition) ==="
grep -RIn "export const agents\s*=\|pgTable.*\"agents\"" /home/paperclip/paperclip/server/src --include="*.ts" | head -10
FILE=$(grep -RIl "pgTable.*\"agents\"\|pgTable(\"agents\"" /home/paperclip/paperclip/server/src --include="*.ts" 2>/dev/null | head -1)
[ -n "$FILE" ] && awk '/export const agents\s*=/,/^\}\)/' "$FILE" | head -80
