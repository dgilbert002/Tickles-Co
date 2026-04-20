#!/bin/bash
# Audit the REAL Tickles Postgres (not Paperclip's embedded one).
# Conn details come from /opt/tickles/.env
set +e

set -a
source /opt/tickles/.env 2>/dev/null
set +a

: "${DB_HOST:=127.0.0.1}"
: "${DB_PORT:=5432}"
: "${DB_USER:=admin}"
: "${DB_NAME_SHARED:=tickles_shared}"
: "${DB_NAME_COMPANY:=tickles_company}"

echo "--- connection target (values masked) ---"
echo "host=${DB_HOST}  port=${DB_PORT}  user=${DB_USER}  shared_db=${DB_NAME_SHARED}  company_db=${DB_NAME_COMPANY}"
echo

export PGPASSWORD="${DB_PASSWORD}"

echo "--- databases on server ---"
psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d postgres -c "\l" 2>&1 | head -30
echo

for DB in "${DB_NAME_SHARED}" "${DB_NAME_COMPANY}"; do
  echo "=============================================="
  echo "DATABASE: ${DB}"
  echo "=============================================="
  echo "--- tables in ${DB} ---"
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB" -c "\dt" 2>&1 | head -80
  echo
  echo "--- row counts per table ---"
  psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB" -At -c "
    SELECT schemaname || '.' || relname AS tbl, n_live_tup AS rows
    FROM pg_stat_user_tables
    ORDER BY n_live_tup DESC
    LIMIT 60;" 2>&1 | head -80
  echo
done

echo
echo "--- schemas for key tables in tickles_shared (if present) ---"
for T in candles derivatives_snapshots funding_rates strategies trades orders positions executions signals market_state heartbeats runs companies agents; do
  EXISTS=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "${DB_NAME_SHARED}" -At -c "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='${T}' LIMIT 1;" 2>/dev/null)
  if [ "$EXISTS" = "1" ]; then
    echo "--- ${T} ---"
    psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "${DB_NAME_SHARED}" -c "\d ${T}" 2>&1 | head -25
    psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "${DB_NAME_SHARED}" -At -c "SELECT COUNT(*) FROM ${T};" 2>&1 | head -3 | xargs echo "rows:"
    echo
  fi
done

echo
echo "--- /opt/tickles/shared/utils/db.py (connection helper) ---"
head -80 /opt/tickles/shared/utils/db.py 2>/dev/null
echo
echo "--- /opt/tickles/shared/utils/config.py (head) ---"
head -60 /opt/tickles/shared/utils/config.py 2>/dev/null
