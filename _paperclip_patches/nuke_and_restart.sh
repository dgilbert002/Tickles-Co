#!/bin/bash
# NUKE + FRESH START for Paperclip.
# Prereq: backup already completed at /root/backups/paperclip-nuke-*
set -u

section() { echo; echo "=========================================="; echo "$1"; echo "=========================================="; }

section "0. Confirm backup exists"
LATEST_BACKUP=$(ls -1dt /root/backups/paperclip-nuke-* 2>/dev/null | head -1)
if [ -z "${LATEST_BACKUP}" ] || [ ! -f "${LATEST_BACKUP}/instance-default.tar.gz" ]; then
    echo "FATAL: no backup found. ABORTING."
    exit 1
fi
echo "Backup: ${LATEST_BACKUP}"
du -sh "${LATEST_BACKUP}"

section "1. Stop services"
systemctl stop tickles-cost-shipper.service 2>&1 | tail -3 || true
systemctl stop paperclip.service 2>&1 | tail -3
sleep 3
echo "paperclip.service status:"
systemctl is-active paperclip.service || echo "(stopped)"
echo "tickles-cost-shipper status:"
systemctl is-active tickles-cost-shipper.service || echo "(stopped)"
echo "any paperclip processes remaining?"
ps -eo pid,cmd --no-headers | grep -iE "paperclip|tsx src/index" | grep -v grep | head -5 || echo "(none)"

section "2. Wipe instance dir"
ls -la /home/paperclip/.paperclip/instances/default/ 2>/dev/null | head -5
echo "Deleting /home/paperclip/.paperclip/instances/default/ ..."
rm -rf /home/paperclip/.paperclip/instances/default/
sleep 1
if [ -d /home/paperclip/.paperclip/instances/default/ ]; then
    echo "FATAL: failed to delete instance dir"
    exit 1
fi
echo "OK: instance dir gone."
ls -la /home/paperclip/.paperclip/instances/ 2>/dev/null

section "3. Verify source dir untouched"
if [ -d /home/paperclip/paperclip/.git ] && [ -d /home/paperclip/paperclip/server ]; then
    echo "OK: /home/paperclip/paperclip/ git checkout intact"
    du -sh /home/paperclip/paperclip/ 2>/dev/null
else
    echo "FATAL: source dir damaged"
    exit 1
fi

section "4. Verify OpenClaw state untouched"
ls -la /root/.openclaw/workspace/ 2>/dev/null | head -5
systemctl is-active openclaw-gateway 2>/dev/null || pgrep openclaw-gateway | head -3

section "5. Start paperclip.service (fresh boot, will create new instance + run migrations)"
systemctl start paperclip.service
echo "Waiting 15s for Paperclip to initialize..."
sleep 15

section "6. Service status after start"
systemctl is-active paperclip.service
journalctl -u paperclip.service -n 40 --no-pager 2>/dev/null | tail -40

section "7. HTTP health check (with retries)"
for i in 1 2 3 4 5 6 7 8 9 10; do
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3100/api/health 2>/dev/null)
    if [ "${HTTP}" = "200" ]; then
        echo "attempt ${i}: HTTP 200 OK"
        break
    fi
    echo "attempt ${i}: HTTP=${HTTP} (waiting 3s)"
    sleep 3
done
if [ "${HTTP}" != "200" ]; then
    echo "WARN: Paperclip not responding 200 after 30s. Tail of journal:"
    journalctl -u paperclip.service -n 80 --no-pager 2>/dev/null | tail -80
fi

section "8. Verify fresh instance dir created"
ls -la /home/paperclip/.paperclip/instances/default/ 2>/dev/null | head -15
echo "Row counts in fresh DB (should all be 0 or tiny):"
sleep 2
# Use psql via node if available, otherwise curl the API
curl -s http://127.0.0.1:3100/api/companies 2>/dev/null | head -c 500
echo

section "9. Start tickles-cost-shipper"
systemctl start tickles-cost-shipper.service
sleep 3
systemctl is-active tickles-cost-shipper.service
journalctl -u tickles-cost-shipper.service -n 10 --no-pager 2>/dev/null | tail -10

section "10. OpenClaw env vars loaded?"
systemctl show paperclip.service -p Environment --no-pager 2>/dev/null | head -5
systemctl show paperclip.service -p EnvironmentFiles --no-pager 2>/dev/null | head -5

echo; echo "=== FRESH INSTALL COMPLETE ==="
