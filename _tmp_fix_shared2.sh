#!/usr/bin/env bash
set -euo pipefail
sudo -u paperclip python3 <<'PY'
import re
p = "/home/paperclip/paperclip/packages/shared/src/index.ts"
with open(p) as f:
    s = f.read()
# Replace any dangling "from ./validators/index.js;" (unquoted path) with quoted.
new = re.sub(r'from\s+\./validators/index\.js\s*;', 'from "./validators/index.js";', s)
if new != s:
    with open(p, "w") as f:
        f.write(new)
    print("fixed")
else:
    print("no change needed")
PY
tail -12 /home/paperclip/paperclip/packages/shared/src/index.ts
