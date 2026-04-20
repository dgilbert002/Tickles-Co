"""Tickles MCP tools — domain-grouped tool modules for the Phase 2 MCP.

Layout:
    provisioning.py   — company/agent lifecycle (via Paperclip HTTP API)
    data.py           — market data / alt-data / catalog lookups
    memory.py         — mem0 (Tier 1/2) + MemU (Tier 3) helpers
    trading.py        — execution, banker, treasury
    learning.py       — autopsy / postmortem / feedback loop (Twilly templates)

Every module exports ``register(registry, ctx)`` which registers its tools
against a ``ToolRegistry`` using a shared ``ToolContext`` carrying HTTP
clients, DB pools, and config.  Tools are grouped so the CEO rule "less
files, grouped by feature" is honoured.
"""

from __future__ import annotations

from .context import ToolContext

__all__ = ["ToolContext"]
