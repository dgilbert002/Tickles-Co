"""Re-customize TradeLab CEO AGENT.md with corrected memory-tier doc."""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import sys
sys.path.insert(0, "/tmp")
sys.path.insert(0, "/opt/tickles")

PAPERCLIP = "http://127.0.0.1:3100"


def get_tradelab_ceo():
    companies = json.loads(
        urllib.request.urlopen(f"{PAPERCLIP}/api/companies", timeout=10).read()
    )
    if isinstance(companies, dict):
        companies = companies.get("companies") or companies.get("data") or []
    tradelab = next(c for c in companies if c.get("name") == "TradeLab")
    cid = tradelab["id"]
    slug = tradelab.get("slug") or "tradelab"
    ags = json.loads(
        urllib.request.urlopen(
            f"{PAPERCLIP}/api/companies/{cid}/agents", timeout=10
        ).read()
    )
    if isinstance(ags, dict):
        ags = ags.get("agents") or ags.get("data") or []
    ceo = ags[0]
    cfg = ceo.get("adapterConfig") or {}
    return {
        "companyId": cid,
        "slug": slug,
        "agentName": ceo["name"],
        "role": ceo.get("role") or "ceo",
        "agentId": cfg.get("agentId") or f"{slug}_{ceo.get('urlKey')}",
        "model": cfg.get("model") or "openrouter/anthropic/claude-sonnet-4",
        "soul": (ceo.get("runtimeConfig") or {}).get("soul") or "apex",
        "skills": ["ccxt-pro", "indicator-library", "backtest-submit"],
    }


def main():
    info = get_tradelab_ceo()
    print(json.dumps(info, indent=2))
    dst = Path(f"/root/.openclaw/agents/{info['agentId']}")
    if not dst.exists():
        print(f"!! missing {dst}")
        return
    skills = info["skills"]
    agent_md = (
        f"# {info['agentName']} — {info['role']}\n\n"
        f"You are **{info['agentName']}**, the {info['role']} of company "
        f"`{info['slug']}` (paperclip company_id=`{info['companyId']}`). "
        f"Your OpenClaw agent id is `{info['agentId']}`.\n\n"
        f"## Identity / Soul\n\n{info['soul']}\n\n"
        f"## Model\n\n`{info['model']}` (set in adapterConfig.model).\n\n"
        f"## Skills available to you\n\n"
        + "\n".join(f"- {s}" for s in skills)
        + "\n\n## Workspace\n\n"
        f"- Paperclip company DB: `tickles_{info['slug']}` on 127.0.0.1:5432\n"
        f"- Qdrant collection: `tickles_{info['slug']}`\n"
        f"- MCP control-plane: http://127.0.0.1:7777/mcp (JSON-RPC 2.0)\n\n"
        "## Memory (three-tier mem0)\n\n"
        "MCP `memory.*` tools take a tier literal, not a namespace name:\n\n"
        "```\n"
        "memory.add  { scope: 'agent',    companyId, agentId, content }  # Tier-1 private\n"
        "memory.add  { scope: 'company',  companyId,          content }  # Tier-2 shared\n"
        "memory.search { scope: 'building',                   query   }  # Tier-3 wide\n"
        "```\n\n"
        f"Your `companyId` = `{info['companyId']}`\n"
        f"Your `agentId`   = `{info['agentId']}`\n"
    )
    (dst / "AGENT.md").write_text(agent_md, encoding="utf-8")
    print(f"* wrote {dst / 'AGENT.md'}")


if __name__ == "__main__":
    main()
