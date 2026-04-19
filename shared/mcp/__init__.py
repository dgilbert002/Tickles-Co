"""Phase 37 MCP stack.

Exposes:
    - protocol : JSON-RPC + tool / resource dataclasses.
    - registry : in-process tool registry with built-in tool builders.
    - server   : JSON-RPC dispatcher with audit hooks.
    - transports.stdio / transports.http.
    - store    : ToolCatalogStore + InvocationStore.
    - memory_pool : InMemoryMcpPool for offline tests.
"""

from __future__ import annotations

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
MIGRATION_PATH = (
    PACKAGE_ROOT / "migrations" / "2026_04_19_phase37_mcp.sql"
)


def read_migration_sql() -> str:
    return MIGRATION_PATH.read_text(encoding="utf-8")


from .protocol import (  # noqa: E402
    PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    McpInvocation,
    McpResource,
    McpTool,
)
from .registry import ToolRegistry  # noqa: E402
from .server import McpServer  # noqa: E402
from .store import (  # noqa: E402
    InvocationStore,
    ToolCatalogStore,
    invocation_to_dict,
)
from .memory_pool import InMemoryMcpPool  # noqa: E402


__all__ = [
    "MIGRATION_PATH",
    "read_migration_sql",
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    "McpInvocation",
    "McpResource",
    "McpTool",
    "ToolRegistry",
    "McpServer",
    "InvocationStore",
    "ToolCatalogStore",
    "invocation_to_dict",
    "InMemoryMcpPool",
]
