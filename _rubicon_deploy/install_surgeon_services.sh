#!/bin/bash
set -e

# Install systemd services for surgeon_trader (flat files, Twilly-faithful)
# and surgeon2_trader (postgres-backed, Tickles adaptation).

cat > /etc/systemd/system/rubicon-surgeon-trader.service <<'UNIT'
[Unit]
Description=Rubicon Surgeon Paper Trader (Twilly-faithful, flat files)
After=network-online.target rubicon-surgeon-scanner.service
Wants=network-online.target rubicon-surgeon-scanner.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/tickles
EnvironmentFile=-/opt/tickles/.env
Environment=PYTHONPATH=/opt/tickles
Environment=PGHOST=127.0.0.1
Environment=PGPORT=5432
Environment=PGUSER=admin
Environment=PGPASSWORD=Tickles21!
ExecStart=/usr/bin/python3 /opt/tickles/shared/daemons/surgeon_trader.py --workspace /root/.openclaw/workspace/rubicon_surgeon --interval 300
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/rubicon-surgeon2-trader.service <<'UNIT'
[Unit]
Description=Rubicon Surgeon2 Paper Trader (PostgreSQL-backed, Tickles adaptation)
After=network-online.target postgresql.service
Wants=network-online.target postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/tickles
EnvironmentFile=-/opt/tickles/.env
Environment=PYTHONPATH=/opt/tickles
Environment=PGHOST=127.0.0.1
Environment=PGPORT=5432
Environment=PGUSER=admin
Environment=PGPASSWORD=Tickles21!
ExecStart=/usr/bin/python3 /opt/tickles/shared/daemons/surgeon2_trader.py --interval 300
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable rubicon-surgeon-trader.service
systemctl enable rubicon-surgeon2-trader.service
systemctl restart rubicon-surgeon-trader.service
systemctl restart rubicon-surgeon2-trader.service

sleep 3
echo "=== surgeon (flat files) ==="
systemctl status rubicon-surgeon-trader.service --no-pager -l | head -20
echo ""
echo "=== surgeon2 (postgres) ==="
systemctl status rubicon-surgeon2-trader.service --no-pager -l | head -20
