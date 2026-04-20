"""TradeLab smoke test v2 — correct camelCase + correct scope literals.

Exercises every MCP tool the agent will use, using the actual tool schemas:
    scope in {agent, company, building}; companyId + agentId as separate args.

Writes a JSON report to /tmp/tradelab_smoke_report_v2.json.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

MCP_URL = "http://127.0.0.1:7777/mcp"
PAPERCLIP = "http://127.0.0.1:3100"


def rpc(method, params=None, req_id=1):
    p = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        p["params"] = params
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(p).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        return {"error": {"code": -1, "message": str(exc)}}


def tool(name, args):
    return rpc("tools/call", {"name": name, "arguments": args})


def step(steps, label, name, args):
    print(f"-- {label} --")
    r = tool(name, args)
    err = r.get("error") or (r.get("result") or {}).get("error")
    result = r.get("result") or {}
    if err:
        print(f"   ERROR: {err}")
        steps.append({"label": label, "tool": name, "args": args, "error": err})
        return None
    content = result.get("result") or result.get("content") or result
    print(f"   ok: {json.dumps(content, default=str)[:250]}")
    steps.append({"label": label, "tool": name, "args": args, "result": result})
    print()
    return result


def main():
    r = urllib.request.urlopen(f"{PAPERCLIP}/api/companies", timeout=10)
    companies = json.loads(r.read())
    if isinstance(companies, dict):
        companies = companies.get("companies") or companies.get("data") or []
    tradelab = next(c for c in companies if c.get("name") == "TradeLab")
    cid = tradelab["id"]
    ags = json.loads(
        urllib.request.urlopen(
            f"{PAPERCLIP}/api/companies/{cid}/agents", timeout=10
        ).read()
    )
    if isinstance(ags, dict):
        ags = ags.get("agents") or ags.get("data") or []
    ceo = ags[0]
    aid = ceo["id"]

    print(f"TradeLab companyId = {cid}")
    print(f"         ceo aid   = {aid}\n")

    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "company": "TradeLab",
        "companyId": cid,
        "ceoAgentId": aid,
        "openclawAgentId": (ceo.get("adapterConfig") or {}).get("agentId"),
        "steps": [],
    }
    steps = report["steps"]

    step(steps, "1. ping (MCP heartbeat)", "ping", {})

    step(steps, "2. catalog.list (bybit, 5)", "catalog.list",
         {"venue": "bybit", "limit": 5})

    step(steps, "3. md.quote stub", "md.quote",
         {"venue": "bybit", "symbol": "BTC/USDT:USDT"})

    step(steps, "4. md.candles stub", "md.candles",
         {"venue": "bybit", "symbol": "BTC/USDT:USDT", "timeframe": "1h", "limit": 5})

    step(steps, "5. memory.add scope=agent (Tier-1)", "memory.add", {
        "scope": "agent",
        "companyId": cid,
        "agentId": aid,
        "content": "TradeLab CEO smoke-v2: Layer 1 infra verified; Banker DB-backed.",
        "metadata": {"test": "smoke-v2", "tier": 1},
    })

    step(steps, "6. memory.search scope=agent", "memory.search", {
        "scope": "agent", "companyId": cid, "agentId": aid,
        "query": "Layer 1 infra", "limit": 3,
    })

    step(steps, "7. memory.add scope=company (Tier-2)", "memory.add", {
        "scope": "company", "companyId": cid,
        "content": "TradeLab is live. Rule-1 advisory. Template=trading.",
        "metadata": {"test": "smoke-v2", "tier": 2},
    })

    step(steps, "8. memory.search scope=building (Tier-3 building shared)",
         "memory.search",
         {"scope": "building", "query": "welcome", "limit": 3})

    step(steps, "9. banker.snapshot", "banker.snapshot", {"companyId": cid})

    step(steps, "10. treasury.evaluate", "treasury.evaluate", {
        "companyId": cid, "venue": "bybit", "symbol": "BTC/USDT:USDT",
        "side": "buy", "quantity": 0.001, "order_type": "market", "mode": "demo",
    })

    step(steps, "11. learnings.read_last_3", "learnings.read_last_3",
         {"companyId": cid, "agentId": aid})

    step(steps, "12. feedback.prompts (Twilly templates)", "feedback.prompts", {})

    ok = sum(1 for s in steps if "error" not in s)
    failed = len(steps) - ok
    real_ok = sum(
        1 for s in steps
        if "error" not in s
        and (s.get("result") or {}).get("result", {}).get("status") != "not_implemented"
    )
    report["summary"] = {
        "ok_total": ok,
        "failed_total": failed,
        "ok_real_data": real_ok,
        "ok_stubbed": ok - real_ok,
        "total_steps": len(steps),
    }
    report["finished_at"] = datetime.now(timezone.utc).isoformat()

    with open("/tmp/tradelab_smoke_report_v2.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n=== summary: ok_total={ok} failed={failed} (real_data={real_ok}, stubbed={ok-real_ok}) ===")
    print("    full report -> /tmp/tradelab_smoke_report_v2.json")


if __name__ == "__main__":
    main()
