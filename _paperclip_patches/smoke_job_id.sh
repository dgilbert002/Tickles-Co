#!/usr/bin/env bash
# Phase-3 smoke: verify job_id flows from Paperclip -> MCP -> executor ->
# back into Paperclip's provisioning-jobs/:id/events endpoint.
set -euo pipefail

PAPERCLIP=http://127.0.0.1:3100

SLUG="smoke_jobid_$(date +%s)"
echo "== 1. create company $SLUG (provisioning enabled, blank template)"
created="$(curl -sS "$PAPERCLIP/api/companies" \
  -H 'content-type: application/json' \
  -d "$(python3 -c "import json; print(json.dumps({'name':'$SLUG','description':'job_id smoke','provisioning':{'enabled':True,'template':'blank','slug':'$SLUG','ruleOneMode':'advisory'}}))")" \
)"
echo "$created" | python3 -m json.tool | head -30
CID="$(echo "$created" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("id") or d.get("company",{}).get("id",""))')"
JID="$(echo "$created" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("provisioningJobId","") or "")')"
echo "company_id=$CID  job_id=$JID"

echo
echo "== 2. poll provisioning-status until overall != running (60s max)"
for i in $(seq 1 60); do
  sleep 1
  resp="$(curl -sS "$PAPERCLIP/api/companies/$CID/provisioning-status")"
  status="$(echo "$resp" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))')"
  if [[ "$status" == "job" ]]; then
    overall="$(echo "$resp" | python3 -c 'import sys,json;print(json.load(sys.stdin)["job"]["overallStatus"])')"
    steps="$(echo "$resp" | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["job"]["steps"]))')"
    echo "  t=${i}s overall=$overall steps=$steps"
    if [[ "$overall" != "running" ]]; then
      echo
      echo "== 3. TERMINAL ==  job summary:"
      echo "$resp" | python3 -c '
import sys,json
j=json.load(sys.stdin)["job"]
print("  id=",j["id"])
print("  overallStatus=",j["overallStatus"])
print("  startedAt=",j["startedAt"])
print("  finishedAt=",j.get("finishedAt"))
print("  steps=")
for s in j["steps"]:
    print("    {:2d} {:<24} {:<8} {}".format(s["stepIndex"], s["step"], s["status"], (s.get("detail") or s.get("error") or "")[:80]))
print("  metadata keys:", list((j.get("metadata") or {}).keys()))
'
      exit 0
    fi
  else
    echo "  t=${i}s status=$status (not_provisioned)"
  fi
done
echo "== timeout — final status:"
curl -sS "$PAPERCLIP/api/companies/$CID/provisioning-status" | python3 -m json.tool
exit 1
