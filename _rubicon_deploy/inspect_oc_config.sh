#!/bin/bash
python3 <<'PY'
import json
with open('/root/.openclaw/openclaw.json') as f:
    d = json.load(f)
agents = {a['id']: a for a in d['agents']['list']}
for i in ['rubicon_ceo', 'rubicon_surgeon', 'rubicon_surgeon2', 'tickles-n-co_cody', 'main']:
    print('===', i, '===')
    print(json.dumps(agents.get(i, {}), indent=2))

print()
print('=== defaults ===')
print(json.dumps(d['agents']['defaults'], indent=2))

print()
print('=== top-level keys ===')
print(list(d.keys()))

print()
print('=== credentials / llm ===')
for k in ['credentials', 'llm', 'providers', 'openrouter', 'env']:
    if k in d:
        print(k, ':', json.dumps(d[k], indent=2)[:500])
PY
