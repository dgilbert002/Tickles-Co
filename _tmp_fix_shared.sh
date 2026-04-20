#!/usr/bin/env bash
set -euo pipefail
sudo -u paperclip python3 <<'PY'
p = "/home/paperclip/paperclip/packages/shared/src/index.ts"
with open(p) as f:
    s = f.read()
bad = "from ./validators/index.js;"
good = 'from "./validators/index.js";'
if bad in s and good not in s:
    s = s.replace(bad, good)
    with open(p, "w") as f:
        f.write(s)
    print("fixed")
else:
    print("no change needed")
PY
tail -12 /home/paperclip/paperclip/packages/shared/src/index.ts
