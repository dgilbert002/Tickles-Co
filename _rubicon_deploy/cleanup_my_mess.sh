#!/bin/bash
set -u
echo "=== backups available ==="
ls -la /root/.openclaw/openclaw.json.bak* 2>/dev/null

BAK=$(ls -1 /root/.openclaw/openclaw.json.bak* 2>/dev/null | head -1)
echo ""
echo "Restoring earliest backup: $BAK"
if [ -n "$BAK" ]; then
  cp "$BAK" /root/.openclaw/openclaw.json && echo "restored"
fi

echo ""
echo "=== validating restored config ==="
openclaw config validate 2>&1 | tail -3

echo ""
echo "=== rubicon_surgeon/_surgeon2 entries after restore ==="
jq '.agents.list[] | select(.id == "rubicon_surgeon" or .id == "rubicon_surgeon2") | {id, model, tools}' /root/.openclaw/openclaw.json 2>&1

echo ""
echo "=== DELETING rubicon_surgeon + rubicon_surgeon2 (clean slate for UI recreation) ==="
for a in rubicon_surgeon rubicon_surgeon2; do
  echo "--- deleting $a ---"
  openclaw agents delete "$a" --force --json 2>&1 | tail -3
done

echo ""
echo "=== remaining agents (should be main + rubicon_ceo only) ==="
openclaw agents list 2>&1 | grep -E '^- '

echo ""
echo "=== scanner daemon status ==="
systemctl status rubicon-surgeon-scanner.service --no-pager -l 2>&1 | head -10
