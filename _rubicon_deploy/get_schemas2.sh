#!/bin/bash
# Dump the zod schemas we need so we can craft proper POSTs.
set +e
PC_SRC=/opt/paperclip/src
# common locations to try
for P in /opt/paperclip /home/paperclip /root/paperclip; do
  if [ -d "$P" ]; then PC_SRC="$P"; fi
done
echo "=== paperclip source root: $PC_SRC ==="
echo ""
echo "=== createCompanySchema ==="
grep -RIn "createCompanySchema" "$PC_SRC" 2>/dev/null | grep -v node_modules | head -20
echo ""
echo "=== createAgentHireSchema ==="
grep -RIn "createAgentHireSchema" "$PC_SRC" 2>/dev/null | grep -v node_modules | head -20
echo ""
echo "=== UpdateCompanySchema / PATCH company ==="
grep -RIn -E "updateCompanySchema|UpdateCompany|requireBoardApproval" "$PC_SRC" 2>/dev/null | grep -v node_modules | head -20
echo ""
echo "=== agents POST handlers ==="
grep -RIn -E "\.post\(|router\.post" "$PC_SRC/apps" 2>/dev/null | grep -v node_modules | grep -iE "agent|hire" | head -20
echo ""
echo "=== list available agents (for hire) ==="
curl -s http://127.0.0.1:3100/api/agents/available 2>/dev/null | head -c 2000
echo ""
echo "=== list companies (empty start) ==="
curl -s http://127.0.0.1:3100/api/companies | head -c 2000
echo ""
echo "=== openclaw agent registry dir ==="
ls -la /root/.openclaw/agents 2>/dev/null | head -20
