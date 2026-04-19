"""Phase 37 MCP protocol dataclasses.

We implement the subset of Model Context Protocol required for Tickles
to be consumable by LLM agents (Paperclip, OpenClaw, Claude Desktop,
etc.). MCP is JSON-RPC 2.0. We intentionally keep the surface small
and well-typed so that tool authors don't have to think about JSON-RPC
plumbing.

Standard MCP methods we implement:
    initialize
    tools/list
    tools/call
    resources/list
    resources/read
    ping

Everything else returns a JSON-RPC error with code -32601 (Method not
found). This matches the spec while staying minimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional


# --- JSON-RPC ---------------------------------------------------------


# Standard JSON-RPC 2.0 error codes. The MCP spec reuses these.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Application-specific errors (above -32000 per spec guidance).
TOOL_DISABLED = -32010
TOOL_NOT_FOUND = -32011
TOOL_FAILED = -32012


@dataclass
class JsonRpcRequest:
    jsonrpc: str
    method: str
    id: Any = None
    params: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(obj: Dict[str, Any]) -> "JsonRpcRequest":
        return JsonRpcRequest(
            jsonrpc=obj.get("jsonrpc", "2.0"),
            method=str(obj.get("method", "")),
            id=obj.get("id"),
            params=obj.get("params") or {},
        )

    def is_notification(self) -> bool:
        # Notifications have no id per spec.
        return self.id is None


@dataclass
class JsonRpcResponse:
    id: Any
    result: Any = None
    error: Optional[Dict[str, Any]] = None
    jsonrpc: str = "2.0"

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error is not None:
            out["error"] = self.error
        else:
            out["result"] = self.result
        return out


def rpc_error(
    code: int, message: str, data: Any = None
) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return err


# --- Tools ------------------------------------------------------------


@dataclass
class McpTool:
    """Declarative record for a single MCP tool.

    The actual runtime dispatch is resolved via the ToolRegistry which
    keeps a callable `handler` alongside this record. The dataclass
    below is what we persist in `public.mcp_tools` and what we return
    in `tools/list`.
    """

    name: str
    description: str = ""
    version: str = "1"
    # JSON schema describing params accepted by the tool (MCP clients
    # advertise this to LLMs for function calling).
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    read_only: bool = True
    enabled: bool = True
    tags: Dict[str, Any] = field(default_factory=dict)

    def to_tool_listing(self) -> Dict[str, Any]:
        # The MCP `tools/list` response shape.
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema or {"type": "object"},
        }


ToolHandler = Callable[[Dict[str, Any]], Any]


@dataclass
class McpInvocation:
    """Audit record for a single tool call.

    `result` and `error` are mutually exclusive; `status` reflects
    which one is populated.
    """

    id: Optional[int]
    tool_name: str
    tool_version: Optional[str]
    caller: Optional[str]
    transport: Optional[str]
    params: Dict[str, Any]
    status: str
    result: Optional[Dict[str, Any]]
    error: Optional[str]
    latency_ms: Optional[int]
    started_at: datetime
    completed_at: Optional[datetime]


# --- Resources --------------------------------------------------------


@dataclass
class McpResource:
    """An MCP resource is read-only content fetchable by URI.

    We expose things like the roadmap, services catalog JSON, and
    dashboard snapshot as resources so that an LLM client can pull them
    without needing to know about our HTTP endpoints.
    """

    uri: str
    name: str
    description: str = ""
    mime_type: str = "text/plain"

    def to_listing(self) -> Dict[str, Any]:
        return {
            "uri": self.uri,
            "name": self.name,
            "description": self.description,
            "mimeType": self.mime_type,
        }


# --- Server info ------------------------------------------------------


SERVER_NAME = "tickles-mcp"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"  # MCP spec date we're targeting.


def server_info() -> Dict[str, Any]:
    return {
        "name": SERVER_NAME,
        "version": SERVER_VERSION,
    }


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)
