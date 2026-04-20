#!/bin/bash
cd /tmp
timeout 90 openclaw agent --agent rubicon_surgeon --message 'Reply with exactly: HELLO' --timeout 45 --json 2>&1 > /tmp/oc_test.out
python3 <<'PY'
import json
raw = open('/tmp/oc_test.out').read()
d = json.loads(raw)
print("runId:", d.get('runId'))
print("status:", d.get('status'))
print("--- keys at top level ---")
for k in d: 
    v = d[k]
    if isinstance(v, (str,int,bool,type(None))):
        print(f"{k}: {str(v)[:300]}")
    else:
        print(f"{k}: <{type(v).__name__}>")
print()
print("--- result keys ---")
r = d.get('result',{})
for k,v in r.items():
    if isinstance(v,(str,int,bool,type(None))):
        print(f"result.{k}: {str(v)[:400]}")
    else:
        print(f"result.{k}: <{type(v).__name__}> preview {str(v)[:200]}")
print()
print("--- content / reply / payloads in result ---")
for k in ['content','reply','payloads','messages','transcript','text']:
    if k in r:
        print(f"result.{k}: {str(r[k])[:800]}")
PY
