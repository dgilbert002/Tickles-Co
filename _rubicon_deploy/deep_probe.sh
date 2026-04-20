#!/bin/bash
set -u
echo "=== infer model run --help ==="
openclaw infer model run --help 2>&1 | head -40
echo ""
echo "=== DIRECT LLM CALL via infer (bypasses agent runtime entirely) ==="
timeout 60 openclaw infer model run --model openrouter/openai/gpt-4.1 --prompt 'Reply with just PONG.' --json 2>&1 | head -30
echo ""
echo "=== gateway journalctl around 08:10 (the recent failed turn) ==="
journalctl --user -u openclaw-gateway --since '2026-04-20 08:10:00' --until '2026-04-20 08:12:00' --no-pager 2>&1 | grep -vE 'node\.list' | tail -60
echo ""
echo "=== fire fresh turn now, capture its exact timestamp, then grep gateway log ==="
TS_BEFORE=$(date '+%F %T')
echo "TS_BEFORE=$TS_BEFORE"
sleep 1
openclaw agent --agent rubicon_surgeon --message 'Reply ONLY the word PONG.' --json 2>/dev/null > /tmp/probe_turn.json
TS_AFTER=$(date '+%F %T')
echo "TS_AFTER=$TS_AFTER"
echo ""
echo "=== gateway logs for this specific turn ==="
journalctl --user -u openclaw-gateway --since "$TS_BEFORE" --until "$TS_AFTER" --no-pager 2>&1 | grep -vE 'node\.list' | tail -80
echo ""
echo "=== turn result ==="
jq '{status, text: .result.payloads[0].text, usage: .result.meta.agentMeta.lastCallUsage, durationMs: .result.meta.durationMs, livenessState: .result.meta.livenessState}' /tmp/probe_turn.json
