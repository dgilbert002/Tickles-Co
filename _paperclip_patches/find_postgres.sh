#!/bin/bash
echo "--- /opt/tickles/.env (db-related keys, values redacted) ---"
grep -iE "^(DB_|DATABASE_|POSTGRES|PG_|MYSQL|CLICKHOUSE|REDIS)" /opt/tickles/.env 2>/dev/null | sed 's/=.*/=<redacted>/'
echo
echo "--- .env.mysql.bak (legacy, for comparison) ---"
grep -iE "^(DB_|DATABASE_|POSTGRES|PG_|MYSQL)" /opt/tickles/.env.mysql.bak 2>/dev/null | sed 's/=.*/=<redacted>/'
echo
echo "--- listening ports (pg/ch/redis/mysql family) ---"
ss -lntp 2>/dev/null | grep -E ":(5432|54329|9000|9005|3306|6379)" | head -20
echo
echo "--- postgres-related processes ---"
ps -eo pid,cmd --no-headers 2>/dev/null | grep -iE "postgres|pgsql" | grep -v grep | head -20
echo
echo "--- docker ps (all) ---"
docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null | head -20
echo
echo "--- docker-compose files in /opt/tickles ---"
find /opt/tickles -maxdepth 4 -name "docker-compose*.y*ml" 2>/dev/null | head -10
echo
echo "--- grep code for DATABASE_URL / pg connection ---"
grep -RIn --include="*.py" --include="*.env*" --include="*.yml" --include="*.yaml" -l "DATABASE_URL\|psycopg\|sqlalchemy" /opt/tickles/shared 2>/dev/null | head -10
echo
echo "--- /opt/tickles/shared/db directory ---"
ls -la /opt/tickles/shared/db 2>/dev/null | head -20
echo
echo "--- /opt/tickles/shared/config ---"
ls -la /opt/tickles/shared/config 2>/dev/null | head -20
echo
echo "--- look for db.py / settings.py that defines connection ---"
find /opt/tickles/shared -maxdepth 4 -name "db.py" -o -name "settings.py" -o -name "database.py" -o -name "config.py" 2>/dev/null | head -10
echo
echo "--- scanning running service units for Tickles ---"
systemctl list-units --type=service --no-pager 2>/dev/null | grep -i tickles | head -20
echo
echo "--- cat tickles-mcpd service unit to see its env ---"
systemctl cat tickles-mcpd.service 2>/dev/null | head -30
