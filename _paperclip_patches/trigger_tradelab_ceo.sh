#!/usr/bin/env bash
# Try to invoke tradelab_ceo through the OpenClaw gateway (simulating what
# Paperclip's openclaw-gateway adapter does on a wake).
set -e
CID="25c28438-1208-4593-82fc-d86b460a4a1e"
AID="0aff984d-e3a4-4f69-8636-ac29546ed5a0"
ISSUE_ID="403250f7-aea8-415f-b0c7-97362f80ffe5"

echo "=== probe Paperclip agent run endpoint ==="
# Look for a "wake" / "run" endpoint — Paperclip exposes one per agent.
for path in \
    "/api/agents/${AID}/wake" \
    "/api/agents/${AID}/run" \
    "/api/agents/${AID}/heartbeat" \
    "/api/agents/${AID}/trigger" \
    "/api/companies/${CID}/agents/${AID}/wake" \
    "/api/companies/${CID}/agents/${AID}/run" ; do
  code=$(curl -sS -o /tmp/_r.json -w '%{http_code}' -X POST "http://127.0.0.1:3100${path}" -H 'content-type: application/json' -d '{"reason":"tra1-manual-trigger"}')
  echo "POST ${path} -> ${code}"
  if [ "${code}" = "200" ] || [ "${code}" = "201" ] || [ "${code}" = "202" ]; then
      echo "--- body:"
      head -c 1500 /tmp/_r.json
      echo
      break
  fi
done

echo
echo "=== probe OpenClaw gateway directly (does it accept agent messages?) ==="
TOKEN=$(sudo awk -F= '/OPENCLAW_GATEWAY_TOKEN/{print $2}' /etc/paperclip/openclaw-gateway.env 2>/dev/null | tr -d '"')
echo "token_len=${#TOKEN}"
for path in \
    "/agents/tradelab_ceo" \
    "/agents/tradelab_ceo/run" \
    "/agents/tradelab_ceo/wake" \
    "/v1/agents/tradelab_ceo/run" \
    "/session" ; do
  code=$(curl -sS -o /tmp/_r.json -w '%{http_code}' -H "x-openclaw-token: ${TOKEN}" "http://127.0.0.1:18789${path}")
  echo "GET ${path} -> ${code}"
done

echo
echo "=== probe OpenClaw control UI /agents page for tradelab_ceo HTML hint ==="
curl -sS "http://127.0.0.1:18789/agents/tradelab_ceo" -H "x-openclaw-token: ${TOKEN}" 2>/dev/null | grep -iE 'tradelab_ceo|soul\.md|identity\.md|memory\.md' | head -10 || echo "(no text matches)"

echo
echo "=== peek at a known-good agent's files render via gateway (if it has an API) ==="
curl -sS -m 5 "http://127.0.0.1:18789/api/agents" -H "x-openclaw-token: ${TOKEN}" 2>/dev/null | head -c 1200 || true
echo
echo "=== tickles-mcpd journal (last 20 lines — did the probes hit any daemon?) ==="
sudo journalctl -u tickles-mcpd.service -n 20 --no-pager 2>&1 | tail -20 || true
