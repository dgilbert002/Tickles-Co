#!/bin/bash
set -u
ORPHANS=(
  audrey cody schemy
  tickles-n-co_main tickles-n-co_audrey tickles-n-co_cody tickles-n-co_schemy
  building_ceo building_janitor building_strategy-council-moderator
  tradelab_ceo
)
for a in "${ORPHANS[@]}"; do
  echo "--- deleting $a ---"
  openclaw agents delete "$a" --force --json 2>&1 | tail -3 || true
done
echo ""
echo "=== remaining agents ==="
openclaw agents list 2>&1 | grep -E '^- ' || true
