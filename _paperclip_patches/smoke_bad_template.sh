#!/usr/bin/env bash
# Verify the template-load-failure path: provisioning with a nonexistent
# template should mark the job as 'failed' with a terminal event instead of
# leaving it stuck on 'running'.
set -euo pipefail

PAPERCLIP=http://127.0.0.1:3100
SLUG="smoke_badtpl_$(date +%s)"

echo "== 1. create company with template=does_not_exist"
# We have to create the company manually (with provisioning disabled) then
# kick MCP directly, because the Paperclip /api/companies POST validator
# only accepts known template names. We're deliberately going around it to
# test the MCP executor's error handling.
created="$(curl -sS "$PAPERCLIP/api/companies" \
  -H 'content-type: application/json' \
  -d "$(python3 -c "import json; print(json.dumps({'name':'$SLUG','description':'bad template smoke'}))")" \
)"
CID="$(echo "$created" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')"
echo "company_id=$CID"

echo "== 2. manually insert a provisioning-jobs row"
JID="$(curl -sS "$PAPERCLIP/api/companies/$CID/provisioning-jobs" \
  -H 'content-type: application/json' \
  -d '{"templateId":"does_not_exist","slug":"badtpl"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')"
echo "job_id=$JID"

echo "== 3. call MCP company.provision with the bad template"
curl -sS http://127.0.0.1:7777/mcp \
  -H 'content-type: application/json' \
  -d "$(python3 -c "import json; print(json.dumps({'jsonrpc':'2.0','id':'bad1','method':'tools/call','params':{'name':'company.provision','arguments':{'companyId':'$CID','jobId':'$JID','templateId':'does_not_exist','slug':'badtpl'}}}))")" \
  | python3 -m json.tool | head -30

echo
echo "== 4. final provisioning-status"
sleep 1
curl -sS "$PAPERCLIP/api/companies/$CID/provisioning-status" | python3 -c '
import sys,json
j=json.load(sys.stdin)["job"]
print("  overallStatus=",j["overallStatus"])
print("  steps=")
for s in j["steps"]:
    print("    {:<24} {:<8} {}".format(s["step"], s["status"], (s.get("error") or s.get("detail") or "")[:120]))
'
