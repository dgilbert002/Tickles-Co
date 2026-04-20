#!/bin/bash
set -u
echo "=== rubicon_surgeon dir layout ==="
ls -la /root/.openclaw/agents/rubicon_surgeon/ 2>&1
echo "agent subdir?"
ls -la /root/.openclaw/agents/rubicon_surgeon/agent/ 2>&1 | head
echo ""
echo "=== main dir for reference ==="
ls -la /root/.openclaw/agents/main/agent/ 2>&1 | grep -E 'auth|config' | head
echo ""
echo "=== models status per agent ==="
for ag in rubicon_surgeon rubicon_surgeon2 rubicon_ceo main; do
  echo "--- $ag ---"
  openclaw models --agent "$ag" status 2>&1 | grep -E 'Agent dir|Auth store|openrouter effective|Default|Configured models' | head
done
echo ""
echo "=== direct inference test (bypasses agent runtime) ==="
openclaw infer model --help 2>&1 | head -20
echo ""
echo "=== try 'infer model list' (should show available models from provider) ==="
timeout 30 openclaw infer model list 2>&1 | head -20 || echo "failed or timed out"
