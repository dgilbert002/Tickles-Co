#!/bin/bash
# Query the REAL state of agents in Paperclip + OpenClaw.
# Paperclip uses an embedded Postgres on 127.0.0.1:54329, user paperclip.

echo '=========================================================='
echo 'A. Paperclip Postgres creds'
echo '=========================================================='
sudo cat /etc/paperclip/paperclip.env 2>/dev/null | grep -iE "^(DATABASE_URL|POSTGRES|PG|PAPERCLIP_DB)" | sed 's/\(password\|PASSWORD\)[^@]*/***REDACTED***/' | head -5
echo
sudo cat /home/paperclip/.paperclip/config/*.env 2>/dev/null | grep -iE "DATABASE_URL|POSTGRES|PG" | sed 's/:[^:@]*@/:***@/' | head -5
echo
sudo -u paperclip bash -c 'env | grep -iE "DATABASE|POSTGRES|PG_" | sed "s/:[^:@]*@/:***@/"' 2>/dev/null | head -5

echo
echo '=========================================================='
echo 'B. Locate Paperclip Postgres socket / port'
echo '=========================================================='
sudo ss -tlnp 2>/dev/null | grep -E "543(29|30|32|40)" | head -5
sudo ls /run/postgresql/ 2>/dev/null
echo
# Paperclip usually runs its own postgres — check the instance dir
sudo ls /home/paperclip/.paperclip/instances/default/ 2>/dev/null | head -10

echo
echo '=========================================================='
echo 'C. Paperclip agents — current adapter_config per agent'
echo '=========================================================='
sudo -u paperclip psql -p 54329 -d paperclip -A -F '|' -c "\\dt" 2>/dev/null | head -5
sudo -u paperclip psql -p 54329 -d paperclip -A -F '|' -c "
SELECT
  c.name AS company,
  a.id AS paperclip_agent_id,
  a.name AS agent_name,
  a.url_key,
  a.adapter_type,
  a.adapter_config->>'agentId' AS openclaw_agent_id,
  a.created_at
FROM agents a
LEFT JOIN companies c ON c.id = a.company_id
ORDER BY c.name, a.created_at;
" 2>&1 | head -30

echo
echo '=========================================================='
echo 'D. Paperclip soul / instructions path on disk'
echo '=========================================================='
sudo ls /home/paperclip/.paperclip/instances/default/companies/ 2>/dev/null | head -5
# Per audit: /home/paperclip/.paperclip/instances/default/companies/{cid}/agents/{aid}/instructions/AGENTS.md
for cdir in /home/paperclip/.paperclip/instances/default/companies/*/; do
  cid=$(basename "$cdir")
  echo "  company $cid:"
  for adir in "$cdir"agents/*/; do
    [ -d "$adir" ] || continue
    aid=$(basename "$adir")
    if [ -f "$adir/instructions/AGENTS.md" ]; then
      sz=$(sudo stat -c%s "$adir/instructions/AGENTS.md")
      echo "    agent $aid  AGENTS.md=${sz}b"
    else
      echo "    agent $aid  (no instructions/AGENTS.md)"
    fi
  done
done
