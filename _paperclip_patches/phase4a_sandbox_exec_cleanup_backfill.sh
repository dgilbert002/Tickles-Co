#!/usr/bin/env bash
# Phase 4A — sandbox fix + executor deploy + cleanup + Building-backfill
#
# Idempotent. Does NOT touch Tickles n Co, Building (except their config),
# paperclip UI, or OpenClaw itself. Only:
#   1) loosens tickles-mcpd sandbox so /root/.openclaw/agents is writable
#   2) drops the updated executor.py into /opt/tickles/shared/provisioning/
#   3) deletes smoke companies (5 known ones) via Paperclip DELETE + cleans
#      their Postgres DBs, Qdrant collections, OpenClaw dirs
#   4) backfills Building's CEO/Janitor/StrategyCouncilModerator +
#      Tickles n Co agents with adapterConfig.agentId + on-disk folder.
set -euo pipefail

PAPERCLIP=${PAPERCLIP:-http://127.0.0.1:3100}
OPENCLAW_AGENTS_DIR=/root/.openclaw/agents
SMOKE_IDS=(
  b74dd27f-85ae-4960-a50a-573a61bb85f6   # smoke_jobid_1776610262
  5f45447b-4870-44c4-b949-e888d84fbab7   # smoke_jobid_1776610429
  1390f8ad-184f-46f6-aa6c-10c29850f667   # smoke_jobid_1776611273
  e5b9e9d0-75ae-4a46-bf98-e8dacb9c672d   # smoke_badtpl_1776611316
  41f05365-8a98-4836-aedf-6eff6aa4f0b8   # smoke_trading_3d167e
)
SMOKE_SLUGS=(
  smoke_jobid_1776610262
  smoke_jobid_1776610429
  smoke_jobid_1776611273
  smoke_badtpl_1776611316
  smoke_trading_3d167e
)

echo "============================================================="
echo "Phase 4A.1 — widen tickles-mcpd sandbox (/root/.openclaw rw)"
echo "============================================================="
sudo mkdir -p /etc/systemd/system/tickles-mcpd.service.d
sudo tee /etc/systemd/system/tickles-mcpd.service.d/override.conf >/dev/null <<'EOF'
# Allow the provisioning executor (runs inside tickles-mcpd) to create/modify
# agent directories under /root/.openclaw/agents/. The base unit has
# ProtectHome=read-only + ReadWritePaths=/var/lib/tickles /var/log/tickles,
# which makes /root read-only. This override adds /root/.openclaw to the
# writable set while keeping the rest of /root protected.
[Service]
ReadWritePaths=/root/.openclaw
EOF
sudo systemctl daemon-reload
echo "  -> override.conf written, daemon reloaded."

echo
echo "============================================================="
echo "Phase 4A.2 — deploy updated shared/provisioning/executor.py"
echo "============================================================="
sudo cp -v /tmp/executor.py /opt/tickles/shared/provisioning/executor.py
sudo chown root:root /opt/tickles/shared/provisioning/executor.py
sudo chmod 644 /opt/tickles/shared/provisioning/executor.py

echo
echo "  restart tickles-mcpd"
sudo systemctl restart tickles-mcpd.service
sleep 2
systemctl is-active tickles-mcpd.service || {
  echo "  !! tickles-mcpd failed to restart"
  sudo journalctl -u tickles-mcpd --no-pager -n 40
  exit 1
}
echo "  -> tickles-mcpd active"

echo
echo "============================================================="
echo "Phase 4A.3 — delete smoke companies + their Postgres/Qdrant"
echo "============================================================="
for i in "${!SMOKE_IDS[@]}"; do
  cid="${SMOKE_IDS[$i]}"
  slug="${SMOKE_SLUGS[$i]}"
  echo "  - DELETE paperclip company id=$cid (slug=$slug)"
  code=$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE "$PAPERCLIP/api/companies/$cid" 2>&1 || echo "ERR")
  echo "      http=$code"

  echo "  - DROP postgres db tickles_$slug"
  sudo -u postgres psql -d postgres -tAc "DROP DATABASE IF EXISTS \"tickles_$slug\"" 2>/dev/null || true

  echo "  - DELETE qdrant collection tickles_$slug"
  curl -sS -X DELETE "http://127.0.0.1:6333/collections/tickles_$slug" -o /dev/null -w "      http=%{http_code}\n" 2>&1 || true

  echo "  - RM openclaw dir for any <$slug>_* agents"
  sudo find "$OPENCLAW_AGENTS_DIR" -maxdepth 1 -type d -name "${slug}_*" -print -exec rm -rf {} + 2>/dev/null || true
done

echo
echo "============================================================="
echo "Phase 4A.4 — backfill Building + Tickles n Co agentIds + folders"
echo "============================================================="
sudo -E python3 /tmp/phase4a_backfill.py
