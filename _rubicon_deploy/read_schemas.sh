#!/bin/bash
echo "=== company.ts validator ==="
sed -n '1,80p' /root/paperclip/packages/shared/src/validators/company.ts
echo ""
echo "=== agent.ts validator (createAgentSchema + createAgentHireSchema) ==="
sed -n '1,150p' /root/paperclip/packages/shared/src/validators/agent.ts
echo ""
echo "=== companies.ts route: POST / handler (260-330) ==="
sed -n '255,330p' /root/paperclip/server/src/routes/companies.ts
echo ""
echo "=== agents.ts route: POST /companies/:companyId/agent-hires (1280-1380) ==="
sed -n '1280,1400p' /root/paperclip/server/src/routes/agents.ts
echo ""
echo "=== /api/agents/available route? ==="
grep -RIn "agents/available\|availableAgents\|/available" /root/paperclip/server/src/routes 2>/dev/null | head -10
