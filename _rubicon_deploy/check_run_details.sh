#!/bin/bash
source /root/rubicon.env
PC=http://127.0.0.1:3100

echo "=== issues (RUB-1, RUB-2) ==="
curl -sS "$PC/api/companies/$COMPANY_ID/issues" | python3 -c "
import json,sys
for i in json.load(sys.stdin):
    print(i.get('identifier'), i.get('status'), '|', i.get('title'))
"

echo ""
echo "=== RUB-1 comments ==="
curl -sS "$PC/api/issues/88e44840-b1bf-4778-bb34-5618b000ee03/comments" | python3 -c "
import json,sys
for c in json.load(sys.stdin):
    print('-', c.get('content', '')[:200])
"

echo ""
echo "=== RUB-2 comments ==="
curl -sS "$PC/api/issues/65229e46-3be2-4366-bba5-8e29f5258e6b/comments" | python3 -c "
import json,sys
for c in json.load(sys.stdin):
    print('-', c.get('content', '')[:200])
"

echo ""
echo "=== run transcript surgeon v1 ==="
RUN1=cc0d0c30-224f-4898-bd8e-a9b48532a9cd
curl -sS "$PC/api/heartbeat-runs/$RUN1/log" | head -c 4000
echo ""

echo ""
echo "=== paperclip workspace surgeon ==="
SURG_ID=166a0626-d7d3-4b92-9b33-a74b6da2ba0b
ls -la /home/paperclip/.paperclip/instances/default/workspaces/$SURG_ID/ 2>/dev/null | head -20
