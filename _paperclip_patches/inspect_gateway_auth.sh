#!/usr/bin/env bash
set -euo pipefail

echo "=== 1. Where the OpenClaw gateway token lives ==="
for f in /root/.openclaw/openclaw.json /home/paperclip/.openclaw/openclaw.json /etc/openclaw/openclaw.json; do
  if sudo test -f "$f"; then
    echo "FOUND: $f"
    sudo jq '.gateway.auth // "no gateway.auth key"' "$f" 2>/dev/null | head -20 || sudo head -c 500 "$f"
    echo
  fi
done
sudo find / -maxdepth 6 -name 'openclaw.json' 2>/dev/null | head

echo
echo "=== 2. Inspect existing working agent adapter_config (cody, CEO) ==="
PSQL=/home/paperclip/paperclip/node_modules/.pnpm/@embedded-postgres+linux-x64@18.1.0-beta.16/node_modules/@embedded-postgres/linux-x64/native/bin/psql
sudo -u paperclip $PSQL -h 127.0.0.1 -p 54329 -d paperclip -c \
  "SELECT id, name, adapter_type, jsonb_pretty(adapter_config) AS cfg FROM agents ORDER BY created_at DESC LIMIT 10;"

echo
echo "=== 3. Paperclip API keys per agent (counts) ==="
sudo -u paperclip $PSQL -h 127.0.0.1 -p 54329 -d paperclip -c \
  "SELECT a.name, a.adapter_type, count(k.id) AS api_keys
     FROM agents a LEFT JOIN agent_api_keys k ON k.agent_id = a.id
     GROUP BY a.id, a.name, a.adapter_type ORDER BY a.created_at DESC LIMIT 10;"

echo
echo "=== 4. grep for createAgent in the wizard path to see if it auto-creates keys ==="
grep -n 'createApiKey\|agents.*keys' /home/paperclip/paperclip/server/src/services/agents*.ts /home/paperclip/paperclip/server/src/routes/agents.ts 2>/dev/null | grep -v node_modules | head -20
