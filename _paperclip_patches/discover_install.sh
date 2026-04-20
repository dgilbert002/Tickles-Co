#!/bin/bash
# READ-ONLY discovery of how Paperclip was installed.
set -u

section() { echo; echo "=========================================="; echo "$1"; echo "=========================================="; }

section "1. Paperclip service unit"
systemctl cat paperclip.service 2>/dev/null | head -40

section "2. Paperclip source dir structure"
ls -la /home/paperclip/paperclip/ 2>/dev/null | head -30

section "3. Is it a git checkout?"
if [ -d /home/paperclip/paperclip/.git ]; then
    echo "YES - git repo"
    cd /home/paperclip/paperclip && git remote -v 2>/dev/null
    cd /home/paperclip/paperclip && git log --oneline -5 2>/dev/null
    cd /home/paperclip/paperclip && git branch --show-current 2>/dev/null
else
    echo "NO - not a git repo"
fi

section "4. package.json highlights"
if [ -f /home/paperclip/paperclip/package.json ]; then
    python3 -c "
import json
with open('/home/paperclip/paperclip/package.json') as f:
    d = json.load(f)
print('name:', d.get('name'))
print('version:', d.get('version'))
print('scripts keys:', list(d.get('scripts', {}).keys())[:15])
print('deps count:', len(d.get('dependencies', {})))
" 2>&1
fi
if [ -f /home/paperclip/paperclip/server/package.json ]; then
    echo "--- server/package.json ---"
    python3 -c "
import json
with open('/home/paperclip/paperclip/server/package.json') as f:
    d = json.load(f)
print('name:', d.get('name'))
print('version:', d.get('version'))
print('scripts keys:', list(d.get('scripts', {}).keys())[:15])
" 2>&1
fi

section "5. Root package.json or workspace config"
ls -la /home/paperclip/paperclip/*.json 2>/dev/null | head -20
ls -la /home/paperclip/paperclip/*.yaml 2>/dev/null | head -20
ls -la /home/paperclip/paperclip/*.yml 2>/dev/null | head -20

section "6. Paperclip config files (no secrets)"
ls -la /home/paperclip/.paperclip/instances/default/config.json 2>/dev/null
if [ -f /home/paperclip/.paperclip/instances/default/config.json ]; then
    python3 -c "
import json
with open('/home/paperclip/.paperclip/instances/default/config.json') as f:
    d = json.load(f)
for k in sorted(d.keys()):
    v = d[k]
    if isinstance(v, str) and len(v) > 80:
        v = v[:40] + '...REDACTED...'
    elif isinstance(v, dict):
        v = '{' + ', '.join(sorted(v.keys())) + '}'
    print(k, '=', v)
" 2>&1
fi

section "7. Is there a global npm paperclip install?"
which paperclip 2>/dev/null
which paperclipai 2>/dev/null
npm list -g --depth=0 2>/dev/null | grep -i paperclip
ls /usr/local/lib/node_modules 2>/dev/null | grep -i paperclip
ls /usr/lib/node_modules 2>/dev/null | grep -i paperclip

section "8. How was paperclip user/home set up?"
getent passwd paperclip 2>/dev/null

section "9. tickles-cost-shipper service"
systemctl cat tickles-cost-shipper.service 2>/dev/null | head -30

section "10. Disk sizes (so we know what the backup will look like)"
du -sh /home/paperclip/paperclip/ 2>/dev/null
du -sh /home/paperclip/.paperclip/ 2>/dev/null
du -sh /home/paperclip/.paperclip/instances/default/ 2>/dev/null
du -sh /home/paperclip/.paperclip/instances/default/db 2>/dev/null
du -sh /home/paperclip/.paperclip/instances/default/companies 2>/dev/null
du -sh /home/paperclip/.paperclip/instances/default/data 2>/dev/null
du -sh /home/paperclip/.paperclip/instances/default/logs 2>/dev/null
du -sh /home/paperclip/.paperclip/instances/default/secrets 2>/dev/null
du -sh /home/paperclip/.paperclip/instances/default/workspaces 2>/dev/null

section "11. OpenClaw gateway token location (just confirm where it is, do NOT print it)"
grep -l "x-openclaw-token\|gatewayUrl\|openclaw_gateway" /root/.openclaw/openclaw.json 2>/dev/null
grep -l "openclaw_gateway" /home/paperclip/.paperclip/instances/default/config.json 2>/dev/null
echo "Token value will be read later during backup (kept in memory only, never printed to this output)."

section "12. Current paperclip DB summary (row counts only)"
PG_DUMP_PATH="/home/paperclip/paperclip/node_modules/.pnpm/@embedded-postgres+linux-x64@18.1.0-beta.16/node_modules/@embedded-postgres/linux-x64/native/bin"
if [ -d "$PG_DUMP_PATH" ]; then
    echo "pg tools found at: $PG_DUMP_PATH"
    PGPASSWORD=paperclip "$PG_DUMP_PATH/psql" -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -c "
    SELECT 'companies' as t, COUNT(*) FROM companies
    UNION ALL SELECT 'agents', COUNT(*) FROM agents
    UNION ALL SELECT 'issues', COUNT(*) FROM issues
    UNION ALL SELECT 'heartbeat_runs', COUNT(*) FROM heartbeat_runs
    UNION ALL SELECT 'approvals', COUNT(*) FROM approvals;
    " 2>&1 | head -20
fi

echo; echo "=== DONE (read-only discovery) ==="
