#!/bin/bash
source /root/rubicon.env
PC=http://127.0.0.1:3100
curl -sS "$PC/api/companies/$COMPANY_ID/heartbeat-runs?limit=2" | python3 -c "
import json,sys
for r in json.load(sys.stdin):
    print(r.get('id'), r.get('agentId'), r.get('status'), '|', r.get('error'))
"
