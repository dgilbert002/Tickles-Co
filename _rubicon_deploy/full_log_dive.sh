#!/bin/bash
set -u
RID=$(jq -r '.runId // .result.meta.agentMeta.runId' /tmp/probe_claude.json 2>/dev/null)
SID=$(jq -r '.result.meta.agentMeta.sessionId' /tmp/probe_claude.json 2>/dev/null)
echo "runId: $RID"
echo "sessionId: $SID"
echo ""
echo "=== ENTIRE log trail for session (most recent ~4 min) ==="
journalctl --user -u openclaw-gateway --since '4 minutes ago' --no-pager 2>&1 | grep -vE 'node\.list|bundle-mcp\] tool \".*\" from server \"tickles\" registered' | tail -120
