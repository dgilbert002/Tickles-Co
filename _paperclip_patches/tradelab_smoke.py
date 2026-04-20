"""TradeLab end-to-end smoke test.

Exercises the full MCP tool surface that the TradeLab CEO agent would see:
    - ping
    - catalog.list (tradable instruments)
    - md.quote / md.candles (market data via ccxt)
    - memory.add / memory.search (mem0 at agent + company scopes)
    - banker.snapshot (per-company P&L)
    - treasury.evaluate (pre-trade check)
    - learnings.read_last_3

Writes a full JSON report to /tmp/tradelab_smoke_report.json and prints a summary.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from typing import Any

MCP_URL = "http://127.0.0.1:7777/mcp"

COMPANY_SLUG = "tradelab"
AGENT_ID = "tradelab_ceo"
TIER1_SCOPE = f"agent_{AGENT_ID}"
TIER2_SCOPE = f"company_{COMPANY_SLUG}"


def call(method: str, params: dict[str, Any] | None = None, req_id: int = 1) -> dict:
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        return {"error": {"code": -1, "message": str(exc)}}


def tool_call(name: str, args: dict[str, Any]) -> dict:
    return call("tools/call", {"name": name, "arguments": args})


def step(label: str, tool: str, args: dict[str, Any]) -> dict:
    print(f"-- {label} --")
    print(f"   tool={tool} args={args}")
    resp = tool_call(tool, args)
    result = resp.get("result") or {}
    err = resp.get("error")
    if err:
        print(f"   ERROR: {err}")
        return {"label": label, "tool": tool, "args": args, "error": err}
    # MCP normalises to result.content or result.structuredContent
    content = result.get("content") or result.get("structuredContent") or result
    summary = json.dumps(content, default=str)[:400]
    print(f"   ok: {summary}")
    print()
    return {"label": label, "tool": tool, "args": args, "result": result}


def main() -> None:
    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "company": COMPANY_SLUG,
        "agent": AGENT_ID,
        "steps": [],
    }

    print("=== TradeLab CEO smoke test ===\n")

    report["steps"].append(step(
        "1. MCP heartbeat",
        "ping",
        {},
    ))

    report["steps"].append(step(
        "2. List tradable instruments (Bybit-demo first 5)",
        "catalog.list",
        {"venue": "bybit", "limit": 5},
    ))

    report["steps"].append(step(
        "3. Latest BTC/USDT:USDT quote on Bybit",
        "md.quote",
        {"venue": "bybit", "symbol": "BTC/USDT:USDT"},
    ))

    report["steps"].append(step(
        "4. Last 5 candles 1h BTC/USDT:USDT",
        "md.candles",
        {"venue": "bybit", "symbol": "BTC/USDT:USDT", "timeframe": "1h", "limit": 5},
    ))

    report["steps"].append(step(
        "5. Write Tier-1 (agent-private) memory for tradelab_ceo",
        "memory.add",
        {
            "scope": TIER1_SCOPE,
            "content": "TradeLab CEO smoke test: Layer 1 infrastructure verified.",
            "metadata": {"test": "bootstrap", "phase": "smoke"},
        },
    ))

    report["steps"].append(step(
        "6. Search Tier-1 memory",
        "memory.search",
        {"scope": TIER1_SCOPE, "query": "smoke test", "limit": 3},
    ))

    report["steps"].append(step(
        "7. Write Tier-2 (company-shared) memory",
        "memory.add",
        {
            "scope": TIER2_SCOPE,
            "content": "TradeLab company is live. CEO agent tradelab_ceo can run MCP tools.",
            "metadata": {"test": "bootstrap", "phase": "smoke"},
        },
    ))

    report["steps"].append(step(
        "8. Banker per-company P&L snapshot",
        "banker.snapshot",
        {"company": COMPANY_SLUG},
    ))

    report["steps"].append(step(
        "9. Treasury evaluate: propose 0.001 BTC buy on Bybit-demo",
        "treasury.evaluate",
        {
            "company": COMPANY_SLUG,
            "venue": "bybit",
            "symbol": "BTC/USDT:USDT",
            "side": "buy",
            "quantity": 0.001,
            "order_type": "market",
            "mode": "demo",
        },
    ))

    report["steps"].append(step(
        "10. Learnings tier-1 read_last_3",
        "learnings.read_last_3",
        {"agent_id": AGENT_ID},
    ))

    ok = sum(1 for s in report["steps"] if "error" not in s)
    failed = len(report["steps"]) - ok
    report["summary"] = {"ok": ok, "failed": failed, "total": len(report["steps"])}
    report["finished_at"] = datetime.now(timezone.utc).isoformat()

    out = "/tmp/tradelab_smoke_report.json"
    with open(out, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n=== summary: ok={ok} failed={failed} total={len(report['steps'])} ===")
    print(f"    full report -> {out}")


if __name__ == "__main__":
    main()
