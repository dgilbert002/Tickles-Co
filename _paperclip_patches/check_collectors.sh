#!/bin/bash
# What funding/derivatives collectors exist in the tickles repo?
set +e

echo "--- all systemd tickles units ---"
systemctl list-unit-files --no-pager 2>/dev/null | grep -i tickles | head -30
echo
echo "--- services with 'funding' or 'deriv' in their ExecStart ---"
for u in $(systemctl list-unit-files --no-pager 2>/dev/null | awk '/tickles/{print $1}'); do
  OUT=$(systemctl cat "$u" 2>/dev/null | grep -iE "funding|deriv|snapshot")
  if [ -n "$OUT" ]; then
    echo "### $u"
    echo "$OUT"
  fi
done
echo
echo "--- source: any files named *funding* or *deriv* ---"
find /opt/tickles -maxdepth 6 -type f \( -iname "*funding*" -o -iname "*deriv*" -o -iname "*snapshot*" \) 2>/dev/null | grep -v __pycache__ | head -30
echo
echo "--- where derivatives_snapshots is INSERT-ed into ---"
grep -RIn --include="*.py" "derivatives_snapshots\|INSERT INTO derivatives" /opt/tickles 2>/dev/null | head -20
echo
echo "--- tickles-candle-daemon status + last logs ---"
systemctl is-active tickles-candle-daemon.service
journalctl -u tickles-candle-daemon.service -n 10 --no-pager 2>/dev/null | tail -10
echo
echo "--- tickles-md-gateway status ---"
systemctl is-active tickles-md-gateway.service
journalctl -u tickles-md-gateway.service -n 10 --no-pager 2>/dev/null | tail -10
echo
echo "--- openclaw CLI available + mcp subcommand? ---"
which openclaw 2>/dev/null
openclaw --help 2>&1 | head -30 | grep -iE "mcp|help"
echo
echo "--- existing MCP registrations in openclaw.json ---"
python3 -c "
import json
with open('/root/.openclaw/openclaw.json') as f:
    d = json.load(f)
mcp = d.get('mcp', {})
print('mcp keys:', sorted(mcp.keys()))
servers = mcp.get('servers', {})
print('servers:', sorted(servers.keys()))
for k,v in (servers or {}).items():
    print(' -', k, ':', {kk:vv for kk,vv in v.items() if kk != 'token'})
" 2>&1
