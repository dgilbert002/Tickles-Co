#!/usr/bin/env bash
set -euo pipefail

# Paperclip stores its DB credentials in the service env. Try discovering:
ENVFILE=$(systemctl show paperclip -p EnvironmentFiles --value | awk '{print $1}' | sed 's/^-//' )
echo "ENVFILE=$ENVFILE"
if [[ -n "$ENVFILE" && -f "$ENVFILE" ]]; then
  sudo cat "$ENVFILE" | grep -E 'PG|POSTGRES|DB_' | head -10
fi

# Embedded postgres typically: user=paperclip, db=paperclip, no password (trust for localhost), port=54329
for pw in "" "paperclip" "postgres"; do
  echo "--- trying PGPASSWORD='$pw' ---"
  PGPASSWORD="$pw" psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -tAc "select 1" 2>&1 | head -2
done

echo
echo "=== agents (latest 10) ==="
psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -c \
  "select left(id::text,8) id, name, adapter_type, adapter_config from agents order by created_at desc limit 10;"

echo
echo "=== agent_api_keys counts ==="
psql -h 127.0.0.1 -p 54329 -U paperclip -d paperclip -c \
  "select a.name, a.adapter_type, count(k.id) keys
   from agents a left join agent_api_keys k on k.agent_id=a.id
   group by a.id, a.name, a.adapter_type
   order by a.created_at desc limit 12;"
