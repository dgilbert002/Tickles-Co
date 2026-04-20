#!/bin/bash
cd /tmp
timeout 90 openclaw agent --agent rubicon_surgeon --message 'Reply HELLO' --timeout 45 --json 2>&1 > /tmp/oc_test.out
python3 <<'PY'
import json
with open('/tmp/oc_test.out') as f:
    raw = f.read()
try:
    d = json.loads(raw)
except Exception as e:
    print("NOT JSON:", e)
    print(raw[-2000:])
    raise SystemExit
out = {}
for k in ['status','error','errorMessage','content','reply','runId']:
    if k in d:
        out[k] = d[k]
meta = d.get('metadata') or {}
out['livenessState'] = meta.get('livenessState')
out['stopReason'] = meta.get('stopReason')
out['replayInvalid'] = meta.get('replayInvalid')
# scan for any error-ish keys
def walk(x, path=''):
    if isinstance(x, dict):
        for k, v in x.items():
            if any(s in k.lower() for s in ('error','fail','abort','warning')):
                yield path+'.'+k, v
            yield from walk(v, path+'.'+k)
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from walk(v, path+f'[{i}]')
print(json.dumps(out, indent=2, default=str))
print("--- error-like keys ---")
for p, v in list(walk(d))[:40]:
    print(p, '=', (str(v)[:200]))
PY
