#!/bin/bash
# OpenRouter key MUST come from env (never inline a raw key here).
# Set it once in /root/.bashrc on the VPS or export locally before running.
KEY="${OPENROUTER_API_KEY:?OPENROUTER_API_KEY not set in env — set it before running this script}"

for MODEL in "anthropic/claude-sonnet-4" "openai/gpt-4.1" "anthropic/claude-sonnet-4.5" "google/gemini-2.5-pro"; do
  echo "=== $MODEL ==="
  curl -sS -X POST "https://openrouter.ai/api/v1/chat/completions" \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say HELLO\"}],\"max_tokens\":50}" \
    | python3 -c "
import json,sys
d=json.load(sys.stdin)
if 'error' in d:
    print('ERROR:', d['error'])
else:
    print('OK:', d.get('choices',[{}])[0].get('message',{}).get('content','')[:80])
    print('model:', d.get('model'))
"
  echo
done
