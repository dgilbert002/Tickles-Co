#!/bin/bash
# Phase 5 audit v3 — find OpenClaw's overlay-reader code and Paperclip's DB.
set -u

echo '=========================================================='
echo 'A. OpenClaw npm package layout'
echo '=========================================================='
sudo ls -la /usr/lib/node_modules/openclaw/ 2>/dev/null | head -15

echo
echo '=========================================================='
echo 'B. Search for "MISSING" badge text in OpenClaw'
echo '=========================================================='
sudo grep -rn "MISSING" /usr/lib/node_modules/openclaw/ 2>/dev/null | grep -v ".map$" | head -10 || echo '(no match)'

echo
echo '=========================================================='
echo 'C. Search for the overlay reader — look for AGENT.md/TOOLS.md refs'
echo '=========================================================='
sudo grep -rn "AGENT.md\|TOOLS.md\|IDENTITY.md" /usr/lib/node_modules/openclaw/ 2>/dev/null | grep -v ".map" | head -20 || echo '(no match)'

echo
echo '=========================================================='
echo 'D. Search for the 8 overlay names in one query'
echo '=========================================================='
sudo grep -rln "'AGENT'\|\"AGENT\"\|overlayFile\|overlayFiles" /usr/lib/node_modules/openclaw/ 2>/dev/null | grep -v ".map" | head -10

echo
echo '=========================================================='
echo 'E. Paperclip — find its DB via the env file'
echo '=========================================================='
sudo cat /etc/paperclip/paperclip.env 2>/dev/null | grep -E "^(DATABASE_URL|POSTGRES|PG|PAPERCLIP_DB)" | sed 's/\(PASSWORD[^=]*=\).*/\1***REDACTED***/;s/\(password=\)[^&@]*/\1***REDACTED***/' | head -10

echo
sudo -u postgres psql -l 2>/dev/null | head -30
