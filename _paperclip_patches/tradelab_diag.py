"""Diagnose mem0 scopes and re-run Banker/Learnings with companyId."""
from __future__ import annotations

import json
import urllib.request

MCP_URL = "http://127.0.0.1:7777/mcp"
PAPERCLIP = "http://127.0.0.1:3100"


def call(method, params=None):
    p = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        p["params"] = params
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(p).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def tool(name, args):
    return call("tools/call", {"name": name, "arguments": args})


def main():
    r = urllib.request.urlopen(f"{PAPERCLIP}/api/companies", timeout=10)
    companies = json.loads(r.read())
    if isinstance(companies, dict):
        companies = companies.get("companies") or companies.get("data") or []
    tradelab = next(c for c in companies if c.get("name") == "TradeLab")
    cid = tradelab["id"]
    print(f"TradeLab companyId = {cid}\n")

    print("=== banker.snapshot(companyId=...) ===")
    print(json.dumps(tool("banker.snapshot", {"companyId": cid}), indent=2)[:800])
    print()

    print("=== banker.positions(companyId=...) ===")
    print(json.dumps(tool("banker.positions", {"companyId": cid}), indent=2)[:800])
    print()

    print("=== learnings.read_last_3(companyId=..., agent_id=...) ===")
    agents = json.loads(
        urllib.request.urlopen(
            f"{PAPERCLIP}/api/companies/{cid}/agents", timeout=10
        ).read()
    )
    if isinstance(agents, dict):
        agents = agents.get("agents") or agents.get("data") or []
    aid = agents[0]["id"]
    print(f"  (ceo agent DB id = {aid})")
    print(
        json.dumps(
            tool("learnings.read_last_3", {"companyId": cid, "agent_id": aid}), indent=2
        )[:600]
    )
    print()

    print("=== memory.add with different scope shapes ===")
    for scope in (
        "tradelab_ceo",
        "tradelab",
        f"company_{cid}",
        f"agent_{aid}",
    ):
        r = tool(
            "memory.add",
            {"scope": scope, "content": f"probe scope={scope}", "metadata": {}},
        )
        err = r.get("error")
        if err:
            print(f"  scope={scope!r}  -> ERR {err.get('message')}")
        else:
            print(f"  scope={scope!r}  -> OK {json.dumps(r.get('result'))[:160]}")
    print()

    print("=== check Paperclip mem0 scope registrations ===")
    for path in (
        "/api/mem0/scopes",
        "/api/memory/scopes",
        f"/api/companies/{cid}/memory-scopes",
    ):
        try:
            u = f"{PAPERCLIP}{path}"
            body = urllib.request.urlopen(u, timeout=10).read().decode()[:400]
            print(f"  GET {path} -> ok :: {body}")
        except Exception as exc:  # noqa: BLE001
            print(f"  GET {path} -> {exc}")


if __name__ == "__main__":
    main()
