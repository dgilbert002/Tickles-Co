#!/usr/bin/env bash
python3 - <<'PY'
import json
d = json.load(open('/home/paperclip/paperclip/ui/package.json'))
print("scripts:", json.dumps(d.get('scripts', {}), indent=2))
print("name:", d.get('name'))
PY
