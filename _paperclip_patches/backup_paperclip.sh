#!/bin/bash
# Full backup of Paperclip state BEFORE nuke.
# Non-destructive. Reads only, writes only to /root/backups/.
set -eu

TS=$(date -u +%Y-%m-%dT%H-%M-%SZ)
BACKUP_DIR="/root/backups/paperclip-nuke-${TS}"
mkdir -p "${BACKUP_DIR}"

echo "Backup dir: ${BACKUP_DIR}"
echo

# ---------- 1. find psql / pg_dump ----------
PG_BIN=$(find /home/paperclip/paperclip/node_modules -type d -name 'bin' -path '*embedded-postgres*' 2>/dev/null | head -1)
if [ -z "${PG_BIN}" ]; then
    PG_BIN=$(find /home/paperclip -type f -name 'pg_dump' 2>/dev/null | head -1 | xargs -r dirname)
fi
echo "postgres bin dir: ${PG_BIN}"
ls "${PG_BIN}" 2>/dev/null | head -20
echo

# ---------- 2. snapshot OpenClaw gateway env + systemd units ----------
echo "--- snapshotting config files ---"
mkdir -p "${BACKUP_DIR}/config"
cp -a /etc/paperclip/ "${BACKUP_DIR}/config/etc-paperclip" 2>/dev/null && echo "copied /etc/paperclip/" || echo "no /etc/paperclip/"
cp /etc/systemd/system/paperclip.service "${BACKUP_DIR}/config/" 2>/dev/null && echo "copied paperclip.service"
cp /etc/systemd/system/tickles-cost-shipper.service "${BACKUP_DIR}/config/" 2>/dev/null && echo "copied tickles-cost-shipper.service"
cp /home/paperclip/.paperclip/instances/default/config.json "${BACKUP_DIR}/config/instance-config.json" 2>/dev/null && echo "copied instance config.json"
echo

# ---------- 3. pg_dump the paperclip database ----------
echo "--- pg_dump paperclip ---"
if [ -x "${PG_BIN}/pg_dump" ]; then
    PGPASSWORD=paperclip "${PG_BIN}/pg_dump" -h 127.0.0.1 -p 54329 -U paperclip -d paperclip \
        --no-owner --no-privileges --format=plain \
        --file="${BACKUP_DIR}/paperclip-db.sql" 2>&1 | tail -10
    DUMP_SIZE=$(stat -c %s "${BACKUP_DIR}/paperclip-db.sql" 2>/dev/null || echo 0)
    echo "dump size: ${DUMP_SIZE} bytes"
    echo "dump head (first 20 lines):"
    head -20 "${BACKUP_DIR}/paperclip-db.sql"
    echo "..."
    echo "dump tail (last 5 lines):"
    tail -5 "${BACKUP_DIR}/paperclip-db.sql"
    echo
    echo "company names in dump:"
    grep -E "^COPY public.companies" "${BACKUP_DIR}/paperclip-db.sql" -A 20 | head -25
else
    echo "ERROR: pg_dump not found at ${PG_BIN}"
    exit 1
fi
echo

# ---------- 4. also do a pg_dump of ALL databases as custom format for safety ----------
echo "--- pg_dumpall (globals) ---"
if [ -x "${PG_BIN}/pg_dumpall" ]; then
    PGPASSWORD=paperclip "${PG_BIN}/pg_dumpall" -h 127.0.0.1 -p 54329 -U paperclip --globals-only \
        --file="${BACKUP_DIR}/paperclip-globals.sql" 2>&1 | tail -5
    echo "globals dump: $(stat -c %s "${BACKUP_DIR}/paperclip-globals.sql") bytes"
fi
echo

# ---------- 5. tar the whole instance directory ----------
echo "--- tar instance dir (may take a minute for 966MB) ---"
cd /home/paperclip/.paperclip/instances
tar czf "${BACKUP_DIR}/instance-default.tar.gz" default/ 2>&1 | tail -5
TAR_SIZE=$(stat -c %s "${BACKUP_DIR}/instance-default.tar.gz")
echo "tar size: ${TAR_SIZE} bytes ($(echo "scale=1; ${TAR_SIZE}/1024/1024" | bc) MB)"
echo "tar top entries:"
tar tzf "${BACKUP_DIR}/instance-default.tar.gz" 2>/dev/null | head -15
echo

# ---------- 6. backup manifest ----------
echo "--- manifest ---"
cat > "${BACKUP_DIR}/MANIFEST.md" <<EOF
# Paperclip Pre-Nuke Backup

Created: ${TS}
VPS hostname: $(hostname)

## What's here

- paperclip-db.sql : logical pg_dump of the paperclip database (restorable via psql)
- paperclip-globals.sql : pg_dumpall --globals-only (roles, tablespaces)
- instance-default.tar.gz : full tar of /home/paperclip/.paperclip/instances/default/
- config/etc-paperclip : /etc/paperclip/ (contains openclaw-gateway.env)
- config/paperclip.service : systemd unit
- config/tickles-cost-shipper.service : systemd unit
- config/instance-config.json : the instance config.json

## How to restore (emergency)

1. systemctl stop paperclip.service tickles-cost-shipper.service
2. rm -rf /home/paperclip/.paperclip/instances/default
3. cd /home/paperclip/.paperclip/instances && tar xzf ${BACKUP_DIR}/instance-default.tar.gz
4. systemctl start paperclip.service
EOF
cat "${BACKUP_DIR}/MANIFEST.md"
echo

# ---------- 7. final summary ----------
echo "=========================================="
echo "BACKUP COMPLETE"
echo "=========================================="
echo "Location: ${BACKUP_DIR}"
du -sh "${BACKUP_DIR}"
ls -la "${BACKUP_DIR}"
echo
echo "OpenClaw gateway env file preview (first 3 non-secret lines):"
if [ -f /etc/paperclip/openclaw-gateway.env ]; then
    grep -v '=.*[a-zA-Z0-9]\{30,\}' /etc/paperclip/openclaw-gateway.env | head -10 || true
    echo "(secret values redacted above)"
    echo "env var names present:"
    grep -oE '^[A-Z_]+=' /etc/paperclip/openclaw-gateway.env | tr -d '='
else
    echo "(/etc/paperclip/openclaw-gateway.env does not exist)"
fi
