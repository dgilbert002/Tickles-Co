#!/usr/bin/env bash
python3 <<'PY'
import json, pathlib
p = "/home/paperclip/paperclip/packages/db/src/migrations/meta/_journal.json"
d = json.load(open(p))
print("version:", d.get("version"))
print("dialect:", d.get("dialect"))
print("first entry:", json.dumps(d["entries"][0], indent=2))
print("last entry:", json.dumps(d["entries"][-1], indent=2))
PY
