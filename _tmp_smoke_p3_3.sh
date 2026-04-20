#!/usr/bin/env bash
set -euo pipefail

echo "=== 1. Create company WITHOUT provisioning ==="
R1=$(curl -s -X POST http://127.0.0.1:3100/api/companies \
  -H 'content-type: application/json' \
  -d '{"name":"Phase3 Unprovisioned","description":"no prov"}')
echo "$R1" | python3 -m json.tool
CID1=$(echo "$R1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "CID1=$CID1"

echo
echo "=== 2. Create company WITH provisioning (blank template) ==="
R2=$(curl -s -X POST http://127.0.0.1:3100/api/companies \
  -H 'content-type: application/json' \
  -d '{"name":"Phase3 Provisioned Blank","description":"blank","provisioning":{"enabled":true,"template":"blank","slug":"phase3_blank","ruleOneMode":"advisory"}}')
echo "$R2" | python3 -m json.tool
CID2=$(echo "$R2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
JID2=$(echo "$R2" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("provisioningJobId","") or "")')
echo "CID2=$CID2 JID2=$JID2"

echo
echo "=== 3. GET provisioning-status for CID2 ==="
curl -s http://127.0.0.1:3100/api/companies/$CID2/provisioning-status | python3 -m json.tool

echo
echo "=== 4. GET provisioning-jobs/latest ==="
curl -s http://127.0.0.1:3100/api/companies/$CID2/provisioning-jobs/latest | python3 -m json.tool

echo
echo "=== 5. GET provisioning-jobs list ==="
curl -s http://127.0.0.1:3100/api/companies/$CID2/provisioning-jobs | python3 -m json.tool

echo
echo "=== 6. Append a fake event to the job (simulating executor step 2 OK) ==="
curl -s -X POST http://127.0.0.1:3100/api/companies/$CID2/provisioning-jobs/$JID2/events \
  -H 'content-type: application/json' \
  -d "{\"step\":\"postgres_db\",\"stepIndex\":2,\"status\":\"ok\",\"detail\":\"tickles_phase3_blank created\",\"startedAt\":\"2026-04-10T16:30:00.000Z\",\"finishedAt\":\"2026-04-10T16:30:10.000Z\",\"metadataMerge\":{\"pendingSkillInstalls\":[]}}" | python3 -m json.tool

echo
echo "=== 7. Append terminal event (overall_status=ok) ==="
curl -s -X POST http://127.0.0.1:3100/api/companies/$CID2/provisioning-jobs/$JID2/events \
  -H 'content-type: application/json' \
  -d "{\"step\":\"complete\",\"stepIndex\":9,\"status\":\"ok\",\"overallStatus\":\"ok\",\"finishedAt\":\"2026-04-10T16:30:30.000Z\"}" | python3 -m json.tool

echo
echo "=== 8. Final GET provisioning-status ==="
curl -s http://127.0.0.1:3100/api/companies/$CID2/provisioning-status | python3 -m json.tool

echo
echo "=== 9. Status-for-unprovisioned company ==="
curl -s http://127.0.0.1:3100/api/companies/$CID1/provisioning-status | python3 -m json.tool

echo
echo "=== 10. SQL verification ==="
PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip \
  -c "SELECT id, company_id, template_id, slug, overall_status, jsonb_array_length(steps) as steps_count, finished_at FROM company_provisioning_jobs ORDER BY started_at DESC;"

echo
echo "=== 11. Cleanup ==="
curl -s -X DELETE http://127.0.0.1:3100/api/companies/$CID1 | python3 -m json.tool
curl -s -X DELETE http://127.0.0.1:3100/api/companies/$CID2 | python3 -m json.tool
PGPASSWORD=paperclip psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip \
  -c "SELECT count(*) AS remaining_jobs FROM company_provisioning_jobs;"

echo "=== DONE ==="
