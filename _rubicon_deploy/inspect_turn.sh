#!/bin/bash
echo "=== top-level meta keys ==="
jq '.result.meta | keys' /tmp/agent_test2.json
echo ""
echo "=== meta (minus bulky) ==="
jq '.result.meta | del(.systemPromptReport, .agentMeta)' /tmp/agent_test2.json
echo ""
echo "=== any abandon/replay/fail/error paths ==="
jq 'paths(scalars) as $p | {path: ($p|join(".")), val: getpath($p)} | select(.val|tostring|test("abandon|replay|livenessState|lastError|skip|invalid";"i"))' /tmp/agent_test2.json
