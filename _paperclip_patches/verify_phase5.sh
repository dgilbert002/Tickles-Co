#!/usr/bin/env bash
set -e
echo "=== openclaw.json agents.list (should now show all 12) ==="
sudo python3 - <<'PY'
import json
d = json.load(open("/root/.openclaw/openclaw.json"))
for a in d["agents"]["list"]:
    model = a.get("model", {}).get("primary", "?")
    paperclip = a.get("paperclip", {})
    tag = f"[{paperclip.get('companySlug','original')}/{paperclip.get('role','?')}]" if paperclip else "[original]"
    print(f"- {a['id']:45s} {model:50s} {tag}")
PY
echo
echo "=== tradelab_ceo overlay files ==="
sudo ls -la /root/.openclaw/agents/tradelab_ceo/ | grep -v '^total' | head -20
echo
echo "=== backup files (rollback targets) ==="
sudo ls -la /root/.openclaw/ | grep phase5 | head -15 || echo "(none)"
echo
echo "=== sample: first 20 lines of tradelab_ceo/SOUL.md ==="
sudo head -20 /root/.openclaw/agents/tradelab_ceo/SOUL.md
echo
echo "=== sample: first 30 lines of tradelab_ceo/TOOLS.md ==="
sudo head -30 /root/.openclaw/agents/tradelab_ceo/TOOLS.md
echo
echo "=== sample: first 30 lines of tradelab_ceo/MEMORY.md ==="
sudo head -30 /root/.openclaw/agents/tradelab_ceo/MEMORY.md
