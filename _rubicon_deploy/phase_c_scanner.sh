#!/bin/bash
# Install + start the Surgeon scanner daemon
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [scanner] $*" | tee -a "$LOG"; }

mkdir -p /opt/tickles/shared/daemons

cat > /etc/systemd/system/rubicon-surgeon-scanner.service <<'UNIT'
[Unit]
Description=Rubicon Surgeon Multi-Exchange Scanner (writes MARKET_STATE.json/MARKET_INDICATORS.json)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/tickles/shared/daemons/surgeon_scanner.py --output /root/.openclaw/workspace/rubicon_surgeon --interval 60
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable rubicon-surgeon-scanner.service 2>&1 | tee -a "$LOG"
systemctl restart rubicon-surgeon-scanner.service
sleep 8
log "status:"
systemctl status rubicon-surgeon-scanner.service --no-pager | head -15 | tee -a "$LOG"
log "files in workspace:"
ls -la /root/.openclaw/workspace/rubicon_surgeon/ | tee -a "$LOG"
log "MARKET_STATE preview:"
head -40 /root/.openclaw/workspace/rubicon_surgeon/MARKET_STATE.json 2>/dev/null | tee -a "$LOG"
