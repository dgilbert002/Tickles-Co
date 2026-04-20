#!/usr/bin/env bash
# Read-only investigation of OpenClaw's /agents screen:
#  - where does the dropdown list come from?
#  - what file set does the UI expect per agent (tabs: AGENTS/SOUL/TOOLS/IDENTITY/USER/HEARTBEAT/BOOTSTRAP/MEMORY)?
#  - how do we register a new agent so it shows up in the dropdown?
set -euo pipefail

echo "=============================================================="
echo "1. OpenClaw install layout"
echo "=============================================================="
sudo ls -la /root/.openclaw/ 2>&1 | head -30
echo
echo "--- top-level files:"
sudo find /root/.openclaw -maxdepth 2 -type f 2>&1 | grep -v '/sessions/' | head -30

echo
echo "=============================================================="
echo "2. cody (reference/default agent) full file tree"
echo "=============================================================="
sudo find /root/.openclaw/agents/cody -maxdepth 4 -type f -printf '%p  (%s bytes)\n' 2>&1 | head -40

echo
echo "=============================================================="
echo "3. main (default agent) full file tree"
echo "=============================================================="
sudo find /root/.openclaw/agents/main -maxdepth 4 -type f -printf '%p  (%s bytes)\n' 2>&1 | head -40

echo
echo "=============================================================="
echo "4. tradelab_ceo (what we created) full file tree"
echo "=============================================================="
sudo find /root/.openclaw/agents/tradelab_ceo -maxdepth 4 -type f -printf '%p  (%s bytes)\n' 2>&1 | head -40

echo
echo "=============================================================="
echo "5. OpenClaw config / registry files"
echo "=============================================================="
for candidate in \
    /root/.openclaw/openclaw.json \
    /root/.openclaw/config.json \
    /root/.openclaw/settings.json \
    /root/.openclaw/registry.json \
    /root/.openclaw/agents.json \
    /root/.openclaw/control.json \
    /etc/openclaw/config.json \
    /etc/openclaw/openclaw.json ; do
    if sudo test -f "$candidate"; then
        echo ">>> $candidate"
        sudo cat "$candidate" 2>&1 | head -80
        echo
    fi
done

echo
echo "=============================================================="
echo "6. OpenClaw systemd unit / install path"
echo "=============================================================="
sudo systemctl cat openclaw 2>/dev/null | head -40 || true
sudo systemctl cat openclaw-gateway 2>/dev/null | head -40 || true
sudo systemctl cat openclaw-control 2>/dev/null | head -40 || true
echo "--- any openclaw-named units:"
sudo systemctl list-unit-files 2>/dev/null | grep -i open | head -10 || true

echo
echo "=============================================================="
echo "7. OpenClaw binary / source path (look for the server binary)"
echo "=============================================================="
for p in /opt/openclaw /usr/local/openclaw /root/openclaw /opt/openclaw-gateway /usr/bin/openclaw ; do
    if sudo test -e "$p"; then
        echo ">>> $p exists"
        sudo ls -la "$p" 2>&1 | head -10
    fi
done
echo "--- `which openclaw*`:"
which openclaw openclawd openclaw-gateway openclaw-control 2>&1 || true

echo
echo "=============================================================="
echo "8. Gateway /agents JSON endpoint (the UI must call something)"
echo "=============================================================="
TOKEN=$(sudo awk -F= '/OPENCLAW_GATEWAY_TOKEN/{print $2}' /etc/paperclip/openclaw-gateway.env 2>/dev/null | tr -d '"')
echo "token_len=${#TOKEN}"
for path in /api/agents /agents/list /v1/agents /control/agents /registry/agents ; do
    body=$(curl -sS -m 5 -H "x-openclaw-token: ${TOKEN}" "http://127.0.0.1:18789${path}" -w '\n---http=%{http_code}' 2>&1 | head -5)
    echo "GET ${path}: ${body}"
    echo
done

echo
echo "=============================================================="
echo "9. Look for 'agents' logic in OpenClaw server JS/TS source"
echo "=============================================================="
for base in /opt/openclaw /root/openclaw /usr/local/openclaw ; do
    if sudo test -d "$base"; then
        echo ">>> grep in $base"
        sudo grep -rIln --include='*.js' --include='*.ts' --include='*.mjs' -E '"(main|audrey|cody|schemy)"|agents/list|registerAgent|loadAgents|readAgentDir' "$base" 2>/dev/null | head -10
    fi
done

echo
echo "=============================================================="
echo "10. HTTP fetch of the /agents page HTML for any embedded list"
echo "=============================================================="
curl -sS -m 5 "http://127.0.0.1:18789/agents" 2>&1 | grep -iE 'main|cody|audrey|schemy|tradelab|building' | head -20 || true

echo
echo "=============================================================="
echo "Done."
echo "=============================================================="
