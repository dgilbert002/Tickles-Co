#!/bin/bash
set -u
echo "=== openclaw infer --help ==="
openclaw infer --help 2>&1 | head -40
echo ""
echo "=== openclaw capability --help ==="
openclaw capability --help 2>&1 | head -40
echo ""
echo "=== openclaw models status ==="
openclaw models status 2>&1 | head -40
echo ""
echo "=== openclaw models list ==="
openclaw models list 2>&1 | head -30
echo ""
echo "=== models auth list ==="
openclaw models auth --help 2>&1 | head -20
openclaw models auth list 2>&1 | head -20
echo ""
echo "=== OPENROUTER env? ==="
env | grep -iE 'openrouter|openai|anthropic' | sed 's/=.*/=REDACTED/'
echo ""
echo "=== /root/.openclaw/openclaw.json providers section ==="
jq '.providers // .models // {error: "no providers key"}' /root/.openclaw/openclaw.json 2>&1 | head -60
echo ""
echo "=== secrets files ==="
ls -la /root/.openclaw/secrets* /root/.openclaw/credentials* 2>/dev/null || true
cat /root/.openclaw/openclaw.json | jq 'keys' 2>&1 | head -20
