#!/bin/bash
# Backup Paperclip state. Uses tar as primary (bit-exact DB copy) + optional pg_dump if postgresql-client available.
set -u

TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
BACKUP_DIR="/root/backups/paperclip-nuke-${TS}"
mkdir -p "${BACKUP_DIR}"

echo "Backup dir: ${BACKUP_DIR}"
echo

# ---------- 1. Try to get pg_dump via apt (best effort, non-fatal) ----------
echo "--- checking for pg_dump ---"
if ! command -v pg_dump >/dev/null 2>&1; then
    echo "pg_dump not present. Trying apt install postgresql-client (non-fatal if fails)..."
    apt-get install -y postgresql-client 2>&1 | tail -5 || echo "apt install failed, continuing without pg_dump"
fi
if command -v pg_dump >/dev/null 2>&1; then
    echo "pg_dump available: $(pg_dump --version)"
    echo "--- pg_dump paperclip ---"
    PGPASSWORD=paperclip pg_dump -h 127.0.0.1 -p 54329 -U paperclip -d paperclip \
        --no-owner --no-privileges --format=plain \
        --file="${BACKUP_DIR}/paperclip-db.sql" 2>&1 | tail -5
    if [ -s "${BACKUP_DIR}/paperclip-db.sql" ]; then
        DUMP_SIZE=$(stat -c %s "${BACKUP_DIR}/paperclip-db.sql")
        echo "logical dump size: ${DUMP_SIZE} bytes"
        echo "dump head (first 15 lines):"
        head -15 "${BACKUP_DIR}/paperclip-db.sql"
    fi
else
    echo "pg_dump not available — relying on tar of db/ directory (bit-exact, restorable)"
fi
echo

# ---------- 2. snapshot config ----------
echo "--- snapshotting config files ---"
mkdir -p "${BACKUP_DIR}/config"
cp -a /etc/paperclip/ "${BACKUP_DIR}/config/etc-paperclip" 2>/dev/null && echo "copied /etc/paperclip/" || echo "no /etc/paperclip/"
cp /etc/systemd/system/paperclip.service "${BACKUP_DIR}/config/" 2>/dev/null && echo "copied paperclip.service"
cp /etc/systemd/system/tickles-cost-shipper.service "${BACKUP_DIR}/config/" 2>/dev/null && echo "copied tickles-cost-shipper.service"
cp /home/paperclip/.paperclip/instances/default/config.json "${BACKUP_DIR}/config/instance-config.json" 2>/dev/null && echo "copied instance config.json"
echo

# ---------- 3. Quick pre-tar DB stats (via psql if we have it) ----------
if command -v psql >/dev/null 2>&1; then
    echo "--- DB row counts (sanity check) ---"
    PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -c "
    SELECT 'companies' as t, COUNT(*) FROM companies
    UNION ALL SELECT 'agents', COUNT(*) FROM agents
    UNION ALL SELECT 'issues', COUNT(*) FROM issues
    UNION ALL SELECT 'heartbeat_runs', COUNT(*) FROM heartbeat_runs;
    " 2>&1 | head -10
    echo "--- Company names ---"
    PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -c \
        "SELECT id, name, url_key FROM companies;" 2>&1 | head -15
fi
echo

# ---------- 4. tar the instance directory (primary backup) ----------
echo "--- tar instance dir (966MB — may take 1-2 min) ---"
cd /home/paperclip/.paperclip/instances
tar czf "${BACKUP_DIR}/instance-default.tar.gz" default/ 2>&1 | tail -3
if [ -s "${BACKUP_DIR}/instance-default.tar.gz" ]; then
    TAR_SIZE=$(stat -c %s "${BACKUP_DIR}/instance-default.tar.gz")
    TAR_MB=$((TAR_SIZE / 1024 / 1024))
    echo "tar size: ${TAR_SIZE} bytes (${TAR_MB} MB)"
    echo "tar entries (first 20):"
    tar tzf "${BACKUP_DIR}/instance-default.tar.gz" 2>/dev/null | head -20
    echo "tar entry count: $(tar tzf "${BACKUP_DIR}/instance-default.tar.gz" 2>/dev/null | wc -l)"
    # verify tar is readable end-to-end
    if tar tzf "${BACKUP_DIR}/instance-default.tar.gz" >/dev/null 2>&1; then
        echo "tar integrity: OK (readable end-to-end)"
    else
        echo "tar integrity: FAILED"
        exit 1
    fi
else
    echo "tar FAILED — backup file empty or missing"
    exit 1
fi
echo

# ---------- 5. manifest ----------
cat > "${BACKUP_DIR}/MANIFEST.md" <<EOF
# Paperclip Pre-Nuke Backup

Created: ${TS}
VPS hostname: $(hostname)

## What's here

- instance-default.tar.gz : full tar of /home/paperclip/.paperclip/instances/default/
  - Contains bit-exact Postgres db/ directory (restorable with same embedded-postgres version)
  - Contains all companies/, data/run-logs, workspaces/, secrets/
- paperclip-db.sql (if present) : logical pg_dump (portable, human-readable)
- config/etc-paperclip/ : /etc/paperclip/ (openclaw-gateway.env and anything else)
- config/paperclip.service : systemd unit
- config/tickles-cost-shipper.service : systemd unit
- config/instance-config.json : instance config

## How to restore from tar (emergency rollback)

    systemctl stop paperclip.service tickles-cost-shipper.service
    rm -rf /home/paperclip/.paperclip/instances/default
    cd /home/paperclip/.paperclip/instances
    tar xzf ${BACKUP_DIR}/instance-default.tar.gz
    chown -R paperclip:paperclip /home/paperclip/.paperclip/instances/default
    systemctl start paperclip.service
EOF
echo "--- MANIFEST ---"
cat "${BACKUP_DIR}/MANIFEST.md"
echo

# ---------- 6. final summary ----------
echo "=========================================="
echo "BACKUP COMPLETE"
echo "=========================================="
du -sh "${BACKUP_DIR}"
ls -la "${BACKUP_DIR}"
echo
echo "--- OpenClaw gateway env var names (values redacted) ---"
if [ -f /etc/paperclip/openclaw-gateway.env ]; then
    grep -oE '^[A-Z_]+=' /etc/paperclip/openclaw-gateway.env | tr -d '=' | sort -u
else
    echo "(no /etc/paperclip/openclaw-gateway.env)"
fi
echo
echo "BACKUP OK — SAFE TO PROCEED WITH DESTRUCTION (pending user OK)."
