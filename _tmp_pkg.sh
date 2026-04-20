#!/usr/bin/env bash
echo "=== shared pkg.json (exports) ==="
python3 -c "import json;d=json.load(open('/home/paperclip/paperclip/packages/shared/package.json'));print(json.dumps({k:d.get(k) for k in ['main','module','types','exports','scripts']}, indent=2))"
echo "=== db pkg.json (exports) ==="
python3 -c "import json;d=json.load(open('/home/paperclip/paperclip/packages/db/package.json'));print(json.dumps({k:d.get(k) for k in ['main','module','types','exports','scripts']}, indent=2))"
echo "=== build command hint ==="
ls /home/paperclip/paperclip/packages/shared/
ls /home/paperclip/paperclip/packages/db/ | head
