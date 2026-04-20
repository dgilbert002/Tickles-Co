#!/usr/bin/env bash
# Phase B + C — deploy executor fixes, trimmed templates, and wizard UI.
#
# Runs on VPS after scp-ing source files into /tmp/phaseBC_staging/.
set -euo pipefail

STAGING=/tmp/phaseBC_staging
TICKLES=/opt/tickles
PAPERCLIP=/home/paperclip/paperclip

echo "== sanity =="
test -d "$STAGING" || { echo "ERR: $STAGING missing — run scp first"; exit 2; }
test -d "$TICKLES" || { echo "ERR: $TICKLES missing"; exit 2; }
test -d "$PAPERCLIP" || { echo "ERR: $PAPERCLIP missing"; exit 2; }

echo "== 1. deploy Tickles executor + templates =="
# Back up the existing shared/provisioning/executor.py and templates.py, then
# replace them with our staged versions.
STAMP=$(date +%Y%m%d-%H%M%S)
sudo cp "$TICKLES/shared/provisioning/executor.py" \
        "$TICKLES/shared/provisioning/executor.py.bak-$STAMP"
sudo cp "$TICKLES/shared/provisioning/templates.py" \
        "$TICKLES/shared/provisioning/templates.py.bak-$STAMP"
sudo cp "$STAGING/executor.py"   "$TICKLES/shared/provisioning/executor.py"
sudo cp "$STAGING/templates.py"  "$TICKLES/shared/provisioning/templates.py"
echo "[ok] executor.py + templates.py copied (backups kept with suffix .bak-$STAMP)"

echo "== 2. deploy trimmed template set =="
# Remove the old templates and replace with our 2-template set.
sudo rm -f "$TICKLES/shared/templates/companies/surgeon_co.json" \
           "$TICKLES/shared/templates/companies/polydesk.json" \
           "$TICKLES/shared/templates/companies/media.json" \
           "$TICKLES/shared/templates/companies/research.json" \
           "$TICKLES/shared/templates/companies/mentor_observer.json"
sudo cp "$STAGING/trading.json" \
        "$TICKLES/shared/templates/companies/trading.json"
sudo cp "$STAGING/blank.json" \
        "$TICKLES/shared/templates/companies/blank.json"
sudo cp "$STAGING/README.md" \
        "$TICKLES/shared/templates/companies/README.md"
echo "[ok] templates trimmed. Current list:"
ls -1 "$TICKLES/shared/templates/companies/"*.json

echo "== 3. deploy wizard UI =="
# Replace the remote OnboardingWizard.tsx with our staged version.
WIZ=/home/paperclip/paperclip/ui/src/components/OnboardingWizard.tsx
sudo cp "$WIZ" "${WIZ}.bak-$STAMP"
sudo cp "$STAGING/OnboardingWizard.tsx" "$WIZ"
sudo chown paperclip:paperclip "$WIZ"
echo "[ok] OnboardingWizard.tsx copied (backup kept)"

echo "== 4. restart tickles-mcpd (picks up new executor.py + templates) =="
sudo systemctl restart tickles-mcpd
sleep 2
systemctl is-active tickles-mcpd

echo "== 5. rebuild paperclip UI + sync to server/ui-dist =="
# Canonical rebuild (found via mem0):
sudo -u paperclip bash -c "cd $PAPERCLIP && pnpm -r build" 2>&1 | tail -20
if [[ -x "$PAPERCLIP/scripts/prepare-server-ui-dist.sh" ]]; then
  sudo -u paperclip bash -c "cd $PAPERCLIP && bash scripts/prepare-server-ui-dist.sh"
else
  echo "[warn] prepare-server-ui-dist.sh not found — falling back to rsync"
  sudo rsync -a --delete "$PAPERCLIP/ui/dist/" "$PAPERCLIP/server/ui-dist/"
fi
echo "[ok] UI rebuilt + synced"

echo "== 6. restart paperclip =="
sudo systemctl restart paperclip
sleep 4
systemctl is-active paperclip

echo "== 7. verify trimmed template list via MCP =="
MCP=http://127.0.0.1:7777
curl -sS -X POST "$MCP/mcp" \
  -H "content-type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"company.templates.list","arguments":{}}}' \
  | python3 -c '
import json, sys
d = json.load(sys.stdin)
if "error" in d:
    print("ERR:", d["error"])
    sys.exit(1)
tools = d.get("result", {}).get("content", [])
# The list tool returns text JSON. Parse.
for c in tools:
    if c.get("type") == "text":
        print(c.get("text"))'

echo "== done =="
