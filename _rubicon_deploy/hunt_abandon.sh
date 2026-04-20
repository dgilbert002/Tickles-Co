#!/bin/bash
echo "=== all openclaw log files ==="
find /root/.openclaw -maxdepth 4 -name '*.log' -o -name 'gateway.*' 2>/dev/null | head -20
echo ""
echo "=== journalctl openclaw-gateway (grep replay/abandon/Agent couldn't) ==="
journalctl --user -u openclaw-gateway --no-pager -n 2000 2>/dev/null | grep -iE "replay|abandon|liveness|couldn.t generate|Agent couldn|invalid" | tail -30
echo ""
echo "=== source reference: search node_modules for replayInvalid (to know what triggers it) ==="
OPENCLAW_BIN=$(which openclaw)
echo "openclaw bin: $OPENCLAW_BIN"
OPENCLAW_REAL=$(readlink -f "$OPENCLAW_BIN" 2>/dev/null)
echo "resolved: $OPENCLAW_REAL"
BASE=$(dirname "$OPENCLAW_REAL")/..
echo "base: $BASE"
grep -r -l "replayInvalid" "$BASE" 2>/dev/null | head -5
echo ""
echo "=== snippet where replayInvalid is set ==="
grep -rn -B2 -A4 "replayInvalid" "$BASE" 2>/dev/null | head -80
