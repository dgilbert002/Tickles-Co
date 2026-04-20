#!/bin/bash
source /root/rubicon.env
PC=http://127.0.0.1:3100

echo "=== cody adapterConfig ==="
# find tickles-n-co_cody id
CODY_ID=$(curl -sS "$PC/api/companies/1def5087-1267-4bfc-8c99-069685fff525/agents" 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
a=d if isinstance(d,list) else d.get('agents',[])
for x in a:
    if x.get('name','').endswith('cody'):
        print(x['id']); break
")
echo "cody_id=$CODY_ID"

curl -sS "$PC/api/agents/$CODY_ID" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(json.dumps({
  'adapterType': d.get('adapterType'),
  'adapterConfig': d.get('adapterConfig'),
  'runtimeConfig': d.get('runtimeConfig'),
  'permissions': d.get('permissions'),
}, indent=2))
"

echo ""
echo "=== rubicon_surgeon adapterConfig ==="
curl -sS "$PC/api/agents/$SURG_ID" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(json.dumps({
  'adapterType': d.get('adapterType'),
  'adapterConfig': d.get('adapterConfig'),
  'runtimeConfig': d.get('runtimeConfig'),
  'permissions': d.get('permissions'),
}, indent=2))
"
