#!/bin/bash
cd /tmp
timeout 60 openclaw agent --agent main --message 'Reply exactly HELLO' --timeout 45 --json 2>&1 > /tmp/oc_main.out
python3 <<'PY'
import json
try:
    d = json.loads(open('/tmp/oc_main.out').read())
except Exception as e:
    print("ERR:", e)
    print(open('/tmp/oc_main.out').read()[-800:])
    raise SystemExit
r = d.get('result',{})
print("runId:", d.get('runId'))
print("payloads:", str(r.get('payloads'))[:300])
m = r.get('meta',{}).get('agentMeta',{})
print("model:", m.get('model'))
print("usage:", m.get('lastCallUsage'))
PY
