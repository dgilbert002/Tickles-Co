#!/bin/bash
# Properly test the tickles MCP stdio launcher.
set +e
LOG=/root/rubicon-deploy.log
log() { echo "$(date -u +%FT%TZ) [a2-test] $*" | tee -a "$LOG"; }

log "testing stdio mcp with proper quoting"
cd /opt/tickles

# Send initialize + tools/list. Use a heredoc to avoid escape hell.
python3 -m shared.mcp.bin.tickles_mcp_stdio 2>/tmp/mcp_stderr.log <<'EOF' | head -20 | tee -a "$LOG"
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
EOF

log "--- stderr ---"
tail -20 /tmp/mcp_stderr.log | tee -a "$LOG"

log "--- funding collector progress ---"
PGPASSWORD=$(grep '^DB_PASSWORD=' /opt/tickles/.env | cut -d= -f2-) \
  psql -h 127.0.0.1 -p 5432 -U admin -d tickles_shared -At \
  -c "SELECT COUNT(*) || ' rows, latest=' || COALESCE(MAX(snapshot_at)::text,'none') FROM derivatives_snapshots;" 2>&1 | tee -a "$LOG"
PGPASSWORD=$(grep '^DB_PASSWORD=' /opt/tickles/.env | cut -d= -f2-) \
  psql -h 127.0.0.1 -p 5432 -U admin -d tickles_shared -c "SELECT instrument_id, snapshot_at, funding_rate, source FROM derivatives_snapshots ORDER BY snapshot_at DESC LIMIT 5;" 2>&1 | tee -a "$LOG"
