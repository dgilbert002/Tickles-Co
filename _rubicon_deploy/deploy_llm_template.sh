#!/bin/bash
# Run on the VPS. Installs the reusable trading-agent template and migrates
# rubicon_surgeon to the LLM-driven runner.
set -euo pipefail

TEMPLATE_DIR="/opt/tickles/shared/templates/trading_agent"

echo "[1/4] Installing template into ${TEMPLATE_DIR}"
mkdir -p "${TEMPLATE_DIR}"
cp -f /tmp/surgeon_llm_runner.py      "${TEMPLATE_DIR}/surgeon_llm_runner.py"
cp -f /tmp/SOUL.template.md           "${TEMPLATE_DIR}/SOUL.template.md"
cp -f /tmp/TRADE_STATE.template.md    "${TEMPLATE_DIR}/TRADE_STATE.template.md"
cp -f /tmp/TRADE_LOG.template.md      "${TEMPLATE_DIR}/TRADE_LOG.template.md"
cp -f /tmp/config.template.json       "${TEMPLATE_DIR}/config.template.json"
cp -f /tmp/spawn_trading_agent.sh     "${TEMPLATE_DIR}/spawn_trading_agent.sh"
chmod +x "${TEMPLATE_DIR}/spawn_trading_agent.sh"
sed -i 's/\r$//' "${TEMPLATE_DIR}/spawn_trading_agent.sh" "${TEMPLATE_DIR}/surgeon_llm_runner.py"

echo "[2/4] Stopping old deterministic Surgeon services"
systemctl stop rubicon-surgeon-trader.service   2>/dev/null || true
systemctl disable rubicon-surgeon-trader.service 2>/dev/null || true
systemctl stop rubicon-surgeon2-trader.service   2>/dev/null || true
systemctl disable rubicon-surgeon2-trader.service 2>/dev/null || true

echo "[3/4] Spawning rubicon_surgeon (LLM, Twilly-faithful, flat files)"
bash "${TEMPLATE_DIR}/spawn_trading_agent.sh" rubicon surgeon anthropic/claude-sonnet-4.5 10000

echo "[4/4] Spawning rubicon_surgeon2 (LLM, Tickles/Postgres-backed variant)"
# Surgeon2 reads/writes to Postgres via a SOUL addendum; uses same runner but
# a different workspace + SOUL emphasising MCP/DB data.
bash "${TEMPLATE_DIR}/spawn_trading_agent.sh" rubicon surgeon2 openai/gpt-4.1 10000

# Overlay a Surgeon2-specific note into SOUL.md (append only, doesn't overwrite)
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
echo "DONE. Active services:"
systemctl list-units --all --type=service | grep -E 'tickles-trader-' || true
echo ""
echo "Tail logs with:"
echo "  journalctl -u tickles-trader-rubicon_surgeon.service -f"
echo "  journalctl -u tickles-trader-rubicon_surgeon2.service -f"
