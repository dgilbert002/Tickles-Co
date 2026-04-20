#!/usr/bin/env python3
"""Phase 5d.1 — Create issue TRA-1 assigned to tradelab_ceo.

This is the canonical "brief an agent by opening a Paperclip issue" pattern.
The mandate (full cycle: introspect + market data + memory + trade proposal
+ autopsy) is embedded in the issue body; tradelab_ceo is expected to pick
this up on first manual run.
"""
import json, os, sys, urllib.request, urllib.error

PAPERCLIP = os.environ.get("PAPERCLIP_URL", "http://127.0.0.1:3100")

def http(m, path, body=None):
    url = f"{PAPERCLIP}{path}"
    data = None
    headers = {"content-type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=m)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw

# 1) Resolve TradeLab + CEO ids
_, companies = http("GET", "/api/companies")
tradelab = next((c for c in companies if c["name"] == "TradeLab"), None)
if not tradelab:
    print("ERROR: TradeLab not found"); sys.exit(1)
cid = tradelab["id"]
print(f"[tra1] TradeLab companyId={cid} issuePrefix={tradelab['issuePrefix']} counter={tradelab['issueCounter']}")

_, agents = http("GET", f"/api/companies/{cid}/agents")
ceo = next((a for a in agents if a.get("urlKey") == "ceo" or a.get("role") == "ceo"), None)
if not ceo:
    print("ERROR: CEO agent not found"); print(json.dumps(agents, indent=2)); sys.exit(1)
aid = ceo["id"]
print(f"[tra1] CEO agentId={aid} name={ceo['name']!r}")

# 2) Peek at existing issues (to learn schema + find existing TRA-1)
status, existing = http("GET", f"/api/companies/{cid}/issues")
print(f"[tra1] GET issues http={status} count={len(existing) if isinstance(existing, list) else existing}")
if isinstance(existing, list) and existing:
    print("[tra1] example existing issue:")
    print(json.dumps(existing[0], indent=2)[:800])
    for e in existing:
        if e.get("key") == "TRA-1" or e.get("title","").startswith("TRA-1"):
            print(f"[tra1] TRA-1 already exists (id={e['id']}). Skipping creation.")
            sys.exit(0)

# 3) Create TRA-1 — mandate body
body_md = """# First shift — full-cycle introspection & market probe

You are the CEO of TradeLab. This is your canonical first run. Execute the
cycle below in order and report back in **3 concise bullets** plus a short
risk note at the end. Store a Tier-1 `memory.add` after each step so future
agents can replay your reasoning.

## Steps

1. **Health check.** Call MCP `ping`. If it doesn't return `pong`, halt and
   tell me why.
2. **Self-introspection.** Call `agent.get` with your own agentId. Read
   back: name, role, model, budget, reportsTo. Confirm model + budget
   match `IDENTITY.md`.
3. **Company books.** Call `banker.snapshot` with your `companyId`. Report
   realised, unrealised, deposits, spend.
4. **Catalog awareness.** Call `catalog.list` filtered to `venue=bybit`,
   `kind=perp`. Confirm BTC/USDT:USDT is listed (it should be — it's the
   most-traded venue pair).
5. **Market data (stub-tolerant).** Call `md.quote` for `BTC/USDT:USDT` on
   bybit. If it returns a real price, log it Tier-1; if it returns a
   `not_implemented` stub, note it and move on — market-data gateway is
   scheduled for Phase 2.5.
6. **Treasury gate.** Call `treasury.evaluate` with a hypothetical 0.01 BTC
   long at current price, `paper` venue. Note allow/deny + reasons.
7. **Skip execution (Phase 5 constraint).** Do NOT call `execution.submit`
   yet — the live paper router is being audited in Phase 6. Just note
   what you WOULD submit if allowed.
8. **Memory write.** `memory.add` tier=`agent` with a 1-paragraph summary
   of steps 1-7 (topic=`first-shift`).
9. **Learning loop.** Read `feedback.prompts` (Twilly templates 01/02/03).
   Pick the `feedback.loop` template and render it against this shift.
10. **Autopsy (conditional).** If step 7 had any trade hypothesis,
    `autopsy.run` it with a synthetic fill. Otherwise skip and note why.

## Success criteria

Report back:
- 3 bullets summarising what you saw (P&L / catalog / market-data status).
- 1 risk bullet: anything that looked suspicious or un-testable.
- 1 ask: what you need from the CEO of Tickles n Co (the human user) to
  do your job better.

Capture your full output as a tier-1 memory with `topic="tra-1-firstshift"`.
"""

payload = {
    "title": "First shift — full-cycle introspection & market probe",
    "description": body_md,
    "priority": "high",
    "assigneeAgentId": aid,
    "status": "todo",
    "labels": ["phase5", "firstshift", "smoke"],
}
status, created = http("POST", f"/api/companies/{cid}/issues", body=payload)
print(f"[tra1] POST issues http={status}")
print(json.dumps(created, indent=2)[:1200] if not isinstance(created, str) else created[:1200])
