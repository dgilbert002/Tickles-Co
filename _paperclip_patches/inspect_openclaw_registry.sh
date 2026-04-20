#!/usr/bin/env bash
set -euo pipefail

echo "=== openclaw.json 'agents' key shape ==="
sudo python3 <<'PY'
import json
d = json.load(open("/root/.openclaw/openclaw.json"))
a = d.get("agents")
print("type:", type(a).__name__)
if isinstance(a, dict):
    print("keys:", list(a.keys())[:20])
    for k, v in list(a.items())[:5]:
        print(f"\n--- {k} ({type(v).__name__}) ---")
        if isinstance(v, dict):
            for k2 in list(v.keys())[:12]:
                val = v[k2]
                preview = str(val)[:100] if not isinstance(val, (list, dict)) else f"{type(val).__name__}({len(val)})"
                print(f"  {k2} = {preview}")
elif isinstance(a, list):
    print("len:", len(a))
    if a:
        print("first:", json.dumps(a[0], indent=2)[:800])
else:
    print("value:", repr(a))
PY
echo
echo "=== session.currentAgent key ==="
sudo python3 <<'PY'
import json
d = json.load(open("/root/.openclaw/openclaw.json"))
s = d.get("session") or {}
print("session keys:", list(s.keys())[:20])
for k in ("currentAgent", "activeAgent", "agent", "selectedAgent"):
    if k in s:
        print(f"  session[{k}] =", s[k])
PY
echo
echo "=== gateway keys ==="
sudo python3 <<'PY'
import json
d = json.load(open("/root/.openclaw/openclaw.json"))
g = d.get("gateway") or {}
print("gateway keys:", list(g.keys()))
for k in ("mode","enabled","wsUrl","port","bindHost","auth"):
    if k in g:
        v = g[k]
        if isinstance(v, dict):
            print(f"  gateway.{k} keys:", list(v.keys()))
        else:
            print(f"  gateway.{k} =", v if k != "auth" else "<redacted>")
PY
echo
echo "=== openclaw HTTP surface probe ==="
# Gateway is ws://, but there may be a local HTTP endpoint too for the agent
# registry / admin API. Check common ports.
for p in 7777 18789 8181 8088 3400 3101 3200; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$p/" 2>/dev/null || echo "--")
  echo "  port $p -> HTTP $code"
done
echo
echo "=== all openclaw-related systemd services ==="
systemctl list-units --type=service --all | grep -iE 'openclaw|ticklescore|tickles' || echo "(none matched)"
