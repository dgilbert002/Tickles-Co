#!/bin/bash
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [fix-model] $*" | tee -a "$LOG"; }

# Switch to gpt-4.1 which is the working default in this OpenClaw install.
MODEL="openrouter/openai/gpt-4.1"

python3 <<PY
import json, sys
p='/root/.openclaw/openclaw.json'
with open(p) as f: d=json.load(f)
changed=0
for a in d['agents']['list']:
    if a['id'].startswith('rubicon_'):
        a['model'] = {'primary': '$MODEL', 'fallbacks': ['openrouter/google/gemini-2.5-pro']}
        a['heartbeat'] = a.get('heartbeat', {'every':'15m'})
        a['tools'] = a.get('tools', {'alsoAllow':['lcm_describe','lcm_expand','lcm_expand_query','lcm_grep','agents_list']})
        changed += 1
with open(p,'w') as f: json.dump(d,f,indent=2)
print('updated', changed, 'agents')
PY

for a in rubicon_ceo rubicon_surgeon rubicon_surgeon2; do
  meta=/root/.openclaw/agents/$a/meta.json
  python3 -c "
import json
p='$meta'
d=json.load(open(p))
d['model']='$MODEL'
json.dump(d,open(p,'w'),indent=2)
print('updated',p)
"
done

log "re-test surgeon LLM"
timeout 60 openclaw agent --agent rubicon_surgeon --message 'Reply exactly: HELLO' --timeout 45 --json 2>&1 > /tmp/oc_test.out
python3 <<'PY'
import json
d = json.loads(open('/tmp/oc_test.out').read())
print("runId:", d.get('runId'))
print("status:", d.get('status'))
r = d.get('result',{})
print("payloads:", str(r.get('payloads'))[:500])
meta = r.get('meta',{})
agmeta = meta.get('agentMeta',{})
print("model:", agmeta.get('model'), "provider:", agmeta.get('provider'))
print("lastCallUsage:", agmeta.get('lastCallUsage'))
PY
