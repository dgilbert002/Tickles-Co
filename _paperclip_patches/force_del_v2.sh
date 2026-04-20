#!/usr/bin/env bash
set -euo pipefail
CID=41f05365-8a98-4836-aedf-6eff6aa4f0b8

export PGPASSWORD=paperclip
DB_URL="postgresql://paperclip@127.0.0.1:54329/paperclip?sslmode=disable"

sudo -u paperclip -E /usr/bin/psql "$DB_URL" <<SQL
DELETE FROM budget_policies WHERE company_id = '${CID}';
DELETE FROM company_provisioning_jobs WHERE company_id = '${CID}';
SQL

curl -sS -o /tmp/resp.json -w 'delete-company http=%{http_code}\n' \
  -X DELETE "http://127.0.0.1:3100/api/companies/${CID}"
cat /tmp/resp.json; echo

echo '--- remaining companies ---'
curl -sS http://127.0.0.1:3100/api/companies | python3 -c 'import sys,json; d=json.load(sys.stdin); rows=d if isinstance(d,list) else d.get("companies",[]); [print("  ", c["name"], c["id"]) for c in rows]'
