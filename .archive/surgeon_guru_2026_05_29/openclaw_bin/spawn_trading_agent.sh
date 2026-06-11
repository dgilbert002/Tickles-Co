#!/bin/bash
# spawn_trading_agent.sh — deploy a Twilly-style LLM trading agent in one command.
#
# Usage:
#   sudo bash spawn_trading_agent.sh <company_slug> <agent_name> [model] [starting_balance]
#
# Example:
#   sudo bash spawn_trading_agent.sh rubicon surgeon anthropic/claude-sonnet-4.5 10000
#
# What it does:
#   1. Creates /root/.openclaw/workspace/<company>_<agent>/
#   2. Renders SOUL.md / TRADE_STATE.md / TRADE_LOG.md / config.json from templates
#   3. Ensures the scanner is writing MARKET_STATE.json there (or uses the global one)
#   4. Installs a systemd service that runs surgeon_llm_runner.py every 5 min
#   5. Reloads systemd, enables + starts the service
#   6. Tails the journal so you see the first cycle

set -euo pipefail

COMPANY="${1:-}"
AGENT="${2:-}"
MODEL="${3:-anthropic/claude-sonnet-4.5}"
BALANCE="${4:-10000}"

if [[ -z "$COMPANY" || -z "$AGENT" ]]; then
  echo "Usage: $0 <company_slug> <agent_name> [model] [starting_balance]"
  exit 1
fi

COMPANY_SLUG="$(echo "$COMPANY" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')"
AGENT_NAME="$(echo "$AGENT"   | tr '[:upper:]' '[:lower:]' | tr ' ' '-')"
AGENT_ID="${COMPANY_SLUG}_${AGENT_NAME}"

TEMPLATE_DIR="/opt/tickles/shared/templates/trading_agent"
WS_DIR="/root/.openclaw/workspace/${AGENT_ID}"
SVC_NAME="tickles-trader-${AGENT_ID}.service"
SVC_PATH="/etc/systemd/system/${SVC_NAME}"
RUNNER="${TEMPLATE_DIR}/surgeon_llm_runner.py"

echo "[1/6] Creating workspace ${WS_DIR}"
mkdir -p "${WS_DIR}"

render() {
  local src="$1" dest="$2"
  sed -e "s|{{AGENT_NAME}}|${AGENT_NAME}|g" \
      -e "s|{{COMPANY_NAME}}|${COMPANY}|g" \
      -e "s|{{COMPANY_SLUG}}|${COMPANY_SLUG}|g" \
      -e "s|{{LEVERAGE}}|25|g" \
      -e "s|{{MAX_POSITIONS}}|3|g" \
      -e "s|{{MODE}}|PAPER_TRADING|g" \
      -e "s|{{STARTING_BALANCE}}|${BALANCE}|g" \
      "${src}" > "${dest}"
}

echo "[2/6] Rendering SOUL.md / TRADE_STATE.md / TRADE_LOG.md / config.json"
# SOUL.md is ALWAYS force-rendered so the agent gets the real Twilly template.
# Any prior SOUL.md is backed up next to it before overwrite.
if [[ -f "${WS_DIR}/SOUL.md" ]]; then
  cp "${WS_DIR}/SOUL.md" "${WS_DIR}/SOUL.md.prev-$(date -u +%Y%m%dT%H%M%SZ)"
fi
render "${TEMPLATE_DIR}/SOUL.template.md" "${WS_DIR}/SOUL.md"
# State + log are preserved if they already exist (don't clobber live balance).
[[ -f "${WS_DIR}/TRADE_STATE.md" ]] || render "${TEMPLATE_DIR}/TRADE_STATE.template.md" "${WS_DIR}/TRADE_STATE.md"
[[ -f "${WS_DIR}/TRADE_LOG.md"   ]] || render "${TEMPLATE_DIR}/TRADE_LOG.template.md"   "${WS_DIR}/TRADE_LOG.md"
# config always rendered so model/balance flags are current
render "${TEMPLATE_DIR}/config.template.json" "${WS_DIR}/config.json"
sed -i "s|anthropic/claude-sonnet-4.5|${MODEL}|g" "${WS_DIR}/config.json"
sed -i "s|\"starting_balance\": 10000.0|\"starting_balance\": ${BALANCE}|g" "${WS_DIR}/config.json"

echo "[3/6] Linking market data (shared scanner outputs)"
SHARED_WS="/root/.openclaw/workspace/rubicon_surgeon"
for f in MARKET_STATE.json MARKET_INDICATORS.json; do
  if [[ -f "${SHARED_WS}/${f}" && ! -e "${WS_DIR}/${f}" ]]; then
    ln -sf "${SHARED_WS}/${f}" "${WS_DIR}/${f}"
  fi
done

echo "[4/6] Writing systemd unit ${SVC_PATH}"
cat > "${SVC_PATH}" <<UNIT
[Unit]
Description=Tickles trader ${AGENT_ID} (Twilly LLM Surgeon)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/tickles
EnvironmentFile=-/opt/tickles/.env
Environment=PYTHONPATH=/opt/tickles
ExecStart=/usr/bin/python3 ${RUNNER} --config ${WS_DIR}/config.json
Restart=always
RestartSec=20
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

echo "[5/6] Reloading + starting service"
systemctl daemon-reload
systemctl enable "${SVC_NAME}"
systemctl restart "${SVC_NAME}"
sleep 3
systemctl status "${SVC_NAME}" --no-pager -l | head -20

echo ""
echo "[6/6] DONE. Tail logs with:  journalctl -u ${SVC_NAME} -f"
echo "Workspace: ${WS_DIR}"
echo "Config:    ${WS_DIR}/config.json"
echo "TRADE_STATE.md / TRADE_LOG.md / SOUL.md are under the workspace."
