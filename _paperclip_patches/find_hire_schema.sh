#!/bin/bash
set +e
echo "=== Full /agent-hires handler ==="
sed -n '1330,1430p' /home/paperclip/paperclip/server/src/routes/agents.ts
echo
echo "=== createAgentHireSchema definition ==="
grep -RIln "createAgentHireSchema" /home/paperclip/paperclip/server/src --include="*.ts" 2>/dev/null
for f in $(grep -RIl "export const createAgentHireSchema\|createAgentHireSchema\s*=" /home/paperclip/paperclip/server/src --include="*.ts" 2>/dev/null); do
  echo "--- $f ---"
  awk '/createAgentHireSchema/,/^\}\);?$/' "$f" | head -80
done
echo
echo "=== list all schema definitions in one file to know where they live ==="
ls /home/paperclip/paperclip/server/src/schemas 2>/dev/null
ls /home/paperclip/paperclip/server/src/validation 2>/dev/null
grep -RIn "export const\s\+[a-zA-Z]*Schema\s*=\s*z\." /home/paperclip/paperclip/server/src --include="*.ts" 2>/dev/null | grep -iE "agent|company|hire" | head -20
