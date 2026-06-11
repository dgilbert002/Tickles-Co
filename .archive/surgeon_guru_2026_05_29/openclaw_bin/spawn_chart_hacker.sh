#!/bin/bash
# spawn_chart_hacker.sh — deploy ChartHacker as an OpenClaw-native vision agent.
#
# Usage:
#   sudo bash spawn_chart_hacker.sh <company_slug> <agent_name> [model]
#
# Example:
#   sudo bash spawn_chart_hacker.sh rubicon chart_hacker openrouter/anthropic/claude-sonnet-4
#
# What it does:
#   1. Creates /root/.openclaw/workspace/<company>_<agent>/
#   2. Renders SOUL.md / config.json from templates
#   3. Registers the agent with OpenClaw
#   4. Registers the agent with Paperclip (Org Chart visibility)
#   5. Smoke-tests a single turn
#   6. Registers a 5-minute cron job with --tools read,write,exec
#   7. Prints summary and tail commands

set -euo pipefail

COMPANY="${1:-}"
AGENT="${2:-}"
MODEL="${3:-openrouter/anthropic/claude-sonnet-4}"

if [[ -z "$COMPANY" || -z "$AGENT" ]]; then
  echo "Usage: $0 <company_slug> <agent_name> [model]"
  exit 1
fi

COMPANY_SLUG="$(echo "$COMPANY" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')"
AGENT_NAME="$(echo "$AGENT"   | tr '[:upper:]' '[:lower:]' | tr ' ' '-')"
AGENT_ID="${COMPANY_SLUG}_${AGENT_NAME}"

TEMPLATE_DIR="/opt/tickles/shared/templates/chart_hacker"
WS_DIR="/root/.openclaw/workspace/${AGENT_ID}"

echo "[1/6] Creating workspace ${WS_DIR}"
mkdir -p "${WS_DIR}"

render() {
  local src="$1" dest="$2"
  sed -e "s|{{AGENT_NAME}}|${AGENT_NAME}|g" \
      -e "s|{{AGENT_ID}}|${AGENT_ID}|g" \
      -e "s|{{COMPANY_NAME}}|${COMPANY}|g" \
      -e "s|{{COMPANY_SLUG}}|${COMPANY_SLUG}|g" \
      -e "s|{{MODEL}}|${MODEL}|g" \
      "${src}" > "${dest}"
}

echo "[2/6] Rendering SOUL.md and config.json"
if [[ -f "${WS_DIR}/SOUL.md" ]]; then
  cp "${WS_DIR}/SOUL.md" "${WS_DIR}/SOUL.md.prev-$(date -u +%Y%m%dT%H%M%SZ)"
fi
render "${TEMPLATE_DIR}/SOUL.template.md" "${WS_DIR}/SOUL.md"
render "${TEMPLATE_DIR}/config.template.json" "${WS_DIR}/config.json"

# Start with empty log if not present
[[ -f "${WS_DIR}/CHART_LOG.md" ]] || : > "${WS_DIR}/CHART_LOG.md"

echo "[3/6] Registering agent with OpenClaw"
openclaw agents add "${AGENT_ID}" \
  --workspace "${WS_DIR}" \
  --model "${MODEL}" \
  --non-interactive --json

echo "[4/6] Registering agent with Paperclip (Org Chart visibility)"
# Look up company UUID by slug, then CEO agent id, then POST the new agent
COMPANY_UUID=$(curl -s "http://127.0.0.1:3100/api/companies" | \
  python3 -c "import sys,json; [print(c['id']) for c in json.load(sys.stdin) if c.get('name','').lower().replace(' ','-')=='${COMPANY_SLUG}']")
if [[ -n "${COMPANY_UUID}" ]]; then
  CEO_AGENT_ID=$(curl -s "http://127.0.0.1:3100/api/companies/${COMPANY_UUID}/agents" | \
    python3 -c "import sys,json; [print(a['id']) for a in json.load(sys.stdin) if a.get('role')=='ceo']")
  curl -s -X POST "http://127.0.0.1:3100/api/companies/${COMPANY_UUID}/agents" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${AGENT_ID}\",\"role\":\"general\",\"title\":\"ChartHacker — Vision Analyst\",\"reportsTo\":\"${CEO_AGENT_ID}\",\"capabilities\":\"Analyzes chart images and screenshots shared by traders. Reads media_items, writes signal_interpretations. Does not trade.\",\"adapterType\":\"openclaw_gateway\",\"budgetMonthlyCents\":500}" | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print('Paperclip agent id:',d.get('id','ERROR'),d.get('name',''))"
else
  echo "WARNING: Could not find Paperclip company for slug '${COMPANY_SLUG}'. Agent will NOT appear in Org Chart."
  echo "         Register manually via: curl -X POST http://127.0.0.1:3100/api/companies/<uuid>/agents"
fi

echo "[5/6] Smoke-testing a single turn"
openclaw agent --agent "${AGENT_ID}" \
  -m 'PING. Reply with the single word PONG and nothing else.' \
  --json --timeout 120 | tail -20

echo "[6/6] Registering 5-minute cron job"
openclaw cron add \
  --agent "${AGENT_ID}" \
  --name "${AGENT_ID}_cycle" \
  --description 'ChartHacker 5-min chart analysis cycle' \
  --cron '*/5 * * * *' --tz UTC \
  --session isolated \
  --tools read,write,exec \
  --thinking low --timeout-seconds 180 \
  --no-deliver \
  --message 'Heartbeat. Execute ChartHacker analysis cycle per SOUL.md: (1) check workspace for any new chart images (CHART_IMAGE.png, CHART_IMAGE.jpg, or paths listed in CHART_QUEUE.json); (2) analyze each chart image visually; (3) output structured JSON interpretation per SOUL.md schema to CHART_INTERPRETATION.json; (4) append one-line summary to CHART_LOG.md.' \
  --json > /tmp/chart_hacker_cron.json
  cat /tmp/chart_hacker_cron.json | tail -30

echo "[7/7] DONE."
echo "Workspace: ${WS_DIR}"
echo "SOUL:      ${WS_DIR}/SOUL.md"
echo "Config:    ${WS_DIR}/config.json"
echo "Log:       ${WS_DIR}/CHART_LOG.md"
echo ""
echo "To force-run now:  openclaw cron run \$(openclaw cron list | awk -v name='${AGENT_ID}_cycle' '\$2==name{print \$1}')"
echo "To tail logs:        journalctl -u openclaw-gateway -f | grep -i chart"
