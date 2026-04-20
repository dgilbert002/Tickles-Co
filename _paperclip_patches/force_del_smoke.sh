#!/usr/bin/env bash
set -euo pipefail
CID=41f05365-8a98-4836-aedf-6eff6aa4f0b8

# Paperclip's embedded PG; DB, user = paperclip, port = 54329
sudo -u paperclip /usr/bin/psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip \
  -c "DELETE FROM budget_policies WHERE company_id = '${CID}';"

# Try again
curl -sS -o /tmp/resp.json -w 'delete-company http=%{http_code}\n' \
  -X DELETE "http://127.0.0.1:3100/api/companies/${CID}"
cat /tmp/resp.json
echo
