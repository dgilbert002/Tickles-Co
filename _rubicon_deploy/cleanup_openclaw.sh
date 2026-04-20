#!/bin/bash
# Safe cleanup of OpenClaw agents + SOUL fix for Rubicon Surgeons.
#
# - Archives orphan agent folders to /root/.openclaw/_archive/<timestamp>/
#   so nothing is lost and we can recover.
# - Force-renders Twilly's SOUL.md for rubicon_surgeon and rubicon_surgeon2
#   (keeping a backup of whatever was there).
set -euo pipefail

KEEP=(main rubicon_ceo rubicon_surgeon rubicon_surgeon2)

ARCHIVE_ROOT="/root/.openclaw/_archive/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "${ARCHIVE_ROOT}/agents" "${ARCHIVE_ROOT}/workspace"
echo "[archive] ${ARCHIVE_ROOT}"

contains() {
  local e
  for e in "${KEEP[@]}"; do
    [[ "$e" == "$1" ]] && return 0
  done
  return 1
}

echo ""
echo "[1/3] Archiving orphan ~/.openclaw/agents/* folders"
for d in /root/.openclaw/agents/*/; do
  name="$(basename "$d")"
  if contains "$name"; then
    echo "  KEEP  ${name}"
    continue
  fi
  echo "  ARCHIVE ${name}"
  mv "$d" "${ARCHIVE_ROOT}/agents/${name}"
done

echo ""
echo "[2/3] Archiving orphan ~/.openclaw/workspace/* agent folders"
# Only touch directories that look like agent workspaces (have at least one of
# SOUL.md / IDENTITY.md / AGENTS.md / config.json). Never touch main/memory/
# schema/scripts/skills/state/.clawhub/.git/.openclaw which are shared.
SHARED=(main memory schema scripts skills state .clawhub .git .openclaw)
is_shared() {
  local e
  for e in "${SHARED[@]}"; do
    [[ "$e" == "$1" ]] && return 0
  done
  return 1
}
for d in /root/.openclaw/workspace/*/; do
  name="$(basename "$d")"
  if contains "$name"; then
    echo "  KEEP  workspace/${name}"
    continue
  fi
  if is_shared "$name"; then
    echo "  SKIP (shared) workspace/${name}"
    continue
  fi
  # only move if it looks like an agent workspace
  if [[ -f "${d}/SOUL.md" || -f "${d}/IDENTITY.md" || -f "${d}/AGENTS.md" || -f "${d}/config.json" ]]; then
    echo "  ARCHIVE workspace/${name}"
    mv "$d" "${ARCHIVE_ROOT}/workspace/${name}"
  else
    echo "  SKIP  workspace/${name} (does not look like an agent workspace)"
  fi
done

echo ""
echo "[3/3] Force-rendering Twilly SOUL.md for Rubicon Surgeons"
TEMPLATE_DIR="/opt/tickles/shared/templates/trading_agent"
for agent in rubicon_surgeon rubicon_surgeon2; do
  ws="/root/.openclaw/workspace/${agent}"
  [[ -d "$ws" ]] || { echo "  (skip $agent - no workspace)"; continue; }
  if [[ -f "${ws}/SOUL.md" ]]; then
    cp "${ws}/SOUL.md" "${ARCHIVE_ROOT}/${agent}.SOUL.md.bak"
  fi
  agent_short="${agent#rubicon_}"
  sed -e "s|{{AGENT_NAME}}|${agent_short}|g" \
      -e "s|{{COMPANY_NAME}}|rubicon|g" \
      -e "s|{{COMPANY_SLUG}}|rubicon|g" \
      -e "s|{{LEVERAGE}}|25|g" \
      -e "s|{{MAX_POSITIONS}}|3|g" \
      -e "s|{{MODE}}|PAPER_TRADING|g" \
      -e "s|{{STARTING_BALANCE}}|10000|g" \
      "${TEMPLATE_DIR}/SOUL.template.md" > "${ws}/SOUL.md"
  echo "  ${agent}: SOUL.md replaced with Twilly template ($(wc -l <"${ws}/SOUL.md") lines)"
done

# Surgeon2 gets its Tickles/Postgres override appended
cat >> /root/.openclaw/workspace/rubicon_surgeon2/SOUL.md <<'APPEND'

---

## SURGEON2 OVERRIDE — TICKLES DATA SOURCES

You are Surgeon2. Your market data comes from the Tickles MCP / Postgres
stack in addition to MARKET_STATE.json. When discussing candidates, you may
reference:
  - tickles_shared.candles (1m OHLCV per exchange+symbol)
  - tickles_shared.derivatives_snapshots (funding rates from the funding collector)
  - tickles_shared.instruments (catalog)

Your trade bookkeeping lives in flat files (same TRADE_STATE.md / TRADE_LOG.md)
so the Twilly workflow stays identical. Treat Postgres as a richer read-side;
trust the same entry/exit rules.
APPEND

echo ""
echo "[4/3] Restart Surgeon services so they pick up the new SOUL on next cycle"
systemctl restart tickles-trader-rubicon_surgeon.service \
                   tickles-trader-rubicon_surgeon2.service

echo ""
echo "===================== SUMMARY ====================="
echo "Kept agents:"
for k in "${KEEP[@]}"; do
  [[ -d "/root/.openclaw/agents/$k" ]] && echo "  - $k"
done
echo ""
echo "Archive location (recover by moving back):"
echo "  ${ARCHIVE_ROOT}"
echo ""
echo "Current ~/.openclaw/agents/ contents:"
ls -1 /root/.openclaw/agents/
