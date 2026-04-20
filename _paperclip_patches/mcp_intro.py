"""Introspect MCP tools + skills catalog from the TradeLab company's POV.

Runs on the VPS. Speaks JSON-RPC 2.0 to http://127.0.0.1:7777/mcp.
"""
from __future__ import annotations

import json
import urllib.request

MCP_URL = "http://127.0.0.1:7777/mcp"


def call(method: str, params: dict | None = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> None:
    print("=== MCP tools/list ===")
    resp = call("tools/list")
    tools = (resp.get("result") or {}).get("tools") or []
    print(f"Total: {len(tools)}")
    for t in tools:
        print(f"  - {t['name']}  :: {(t.get('description') or '').splitlines()[0][:80]}")

    print()
    print("=== MCP tools/call skills_list (TradeLab perspective) ===")
    try:
        resp = call(
            "tools/call",
            {
                "name": "skills_list",
                "arguments": {"company": "tradelab"},
            },
        )
        result = resp.get("result") or resp.get("error") or resp
        print(json.dumps(result, indent=2)[:3000])
    except Exception as exc:  # noqa: BLE001
        print(f"skills_list err: {exc}")

    print()
    print("=== MCP tools/call company_overview for tradelab ===")
    try:
        resp = call(
            "tools/call",
            {
                "name": "company_overview",
                "arguments": {"company": "tradelab"},
            },
        )
        result = resp.get("result") or resp.get("error") or resp
        print(json.dumps(result, indent=2)[:3000])
    except Exception as exc:  # noqa: BLE001
        print(f"company_overview err: {exc}")


if __name__ == "__main__":
    main()
