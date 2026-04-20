#!/bin/bash
# find the correct PATCH /agents route
grep -RIn "router\.patch\|router\.put" /root/paperclip/server/src/routes/agents.ts 2>/dev/null | head -40
