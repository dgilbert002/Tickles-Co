#!/usr/bin/env bash
python3 <<'PY'
import json
d = json.load(open("/home/paperclip/paperclip/packages/db/src/migrations/meta/_journal.json"))
print("total entries:", len(d["entries"]))
print("latest:", d["entries"][-1])
PY
