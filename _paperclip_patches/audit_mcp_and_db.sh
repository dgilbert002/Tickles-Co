#!/bin/bash
# Audit: what MCP server / DB tables / services do we already have for Tickles?
set -u

section() { echo; echo "=========================================="; echo "$1"; echo "=========================================="; }

section "1. Tickles source on VPS"
ls -la /opt/tickles/ 2>/dev/null | head -30
echo "---"
du -sh /opt/tickles/* 2>/dev/null | head -20

section "2. Tickles services (systemd)"
systemctl list-units --type=service --all --no-pager 2>/dev/null | grep -i tickles | head -20

section "3. Tickles MCP server (file structure)"
ls -la /opt/tickles/shared/mcp_server/ 2>/dev/null | head -30
ls -la /opt/tickles/mcp/ 2>/dev/null | head -30
find /opt/tickles -type d -name "mcp*" 2>/dev/null | head -10
find /opt/tickles -name "*.py" -path "*/mcp*" 2>/dev/null | head -20

section "4. Any listening MCP ports"
ss -lntp 2>/dev/null | head -20

section "5. Tickles databases (MySQL or Postgres)"
# MySQL
if command -v mysql >/dev/null 2>&1; then
    echo "--- mysql ---"
    mysql -u root -e "SHOW DATABASES;" 2>&1 | head -20 || echo "no mysql access"
    for db in tickles_shared tickles_company tickles; do
        echo "--- tables in ${db} ---"
        mysql -u root -e "USE ${db}; SHOW TABLES;" 2>&1 | head -30 || echo "db ${db} not accessible"
    done
fi
# MariaDB
which mariadb 2>/dev/null
# Docker databases
docker ps 2>/dev/null | head -20

section "6. Docker compose / running containers"
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null | head -20
ls /opt/tickles/*compose* 2>/dev/null
find /opt/tickles -name "docker-compose*.yml" 2>/dev/null | head -10

section "7. Env / config files"
ls /etc/tickles/ 2>/dev/null
ls /opt/tickles/.env* 2>/dev/null
find /opt/tickles -name ".env*" 2>/dev/null | head -10

section "8. Existing CCXT / exchange wrappers"
find /opt/tickles -name "*.py" 2>/dev/null | xargs grep -l "ccxt" 2>/dev/null | head -10
find /opt/tickles -name "*.py" 2>/dev/null | xargs grep -l "aster\|bybit\|binance" 2>/dev/null | head -10

section "9. Banker / Treasury / Souls - the 'platform' services"
find /opt/tickles -type d -name "banker" 2>/dev/null
find /opt/tickles -type d -name "treasury" 2>/dev/null
find /opt/tickles -type d -name "souls" 2>/dev/null
find /opt/tickles -type f -name "*.py" -path "*banker*" 2>/dev/null | head -5
find /opt/tickles -type f -name "*.py" -path "*treasury*" 2>/dev/null | head -5

section "10. Health checks"
for port in 3100 7008 8080 5000 18789; do
    curl -s -o /dev/null -w "port ${port}: %{http_code}\n" --max-time 2 "http://127.0.0.1:${port}/" 2>/dev/null
done
for port in 3100 7008 8080 5000; do
    curl -s -o /dev/null -w "port ${port}/health: %{http_code}\n" --max-time 2 "http://127.0.0.1:${port}/health" 2>/dev/null
done

section "11. Python packages (ccxt, pymysql, etc)"
pip3 list 2>/dev/null | grep -iE "ccxt|pymysql|asyncio|mcp" | head -10
python3 -c "import ccxt; print('ccxt', ccxt.__version__)" 2>&1 | head -3
python3 -c "import pymysql; print('pymysql ok')" 2>&1 | head -3
python3 -c "import mcp; print('mcp ok')" 2>&1 | head -3

echo; echo "=== AUDIT DONE ==="
