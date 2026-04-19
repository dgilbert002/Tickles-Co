"""Phase 37 MCP server.

Dispatches JSON-RPC 2.0 requests to the tool registry and writes audit
records (when a store is configured). Transport-agnostic: this module
doesn't read from stdio or listen on HTTP; it just translates a
request dict into a response dict.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    TOOL_DISABLED,
    TOOL_FAILED,
    TOOL_NOT_FOUND,
    JsonRpcRequest,
    JsonRpcResponse,
    McpResource,
    rpc_error,
    server_info,
    utcnow,
)
from .registry import ToolRegistry
from .store import InvocationStore


log = logging.getLogger("tickles.mcp.server")


# Callable that loads a resource body by URI. Returns (mime_type,
# body_bytes_or_text) or raises KeyError when missing.
ResourceLoader = Callable[[str], Awaitable[tuple[str, str]]]


class McpServer:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        invocation_store: Optional[InvocationStore] = None,
        resources: Optional[List[McpResource]] = None,
        resource_loader: Optional[ResourceLoader] = None,
        default_caller: str = "unknown",
        default_transport: str = "stdio",
    ) -> None:
        self._registry = registry
        self._invocations = invocation_store
        self._resources = list(resources or [])
        self._resource_loader = resource_loader
        self._default_caller = default_caller
        self._default_transport = default_transport

    # ---- public entry point -----------------------------------------

    async def handle(
        self,
        payload: Dict[str, Any],
        *,
        caller: Optional[str] = None,
        transport: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Handles a single JSON-RPC request dict.

        Returns the response dict, or None if the request was a
        notification (no id).
        """
        try:
            req = JsonRpcRequest.from_dict(payload)
        except Exception as exc:  # pragma: no cover - defensive
            return JsonRpcResponse(
                id=payload.get("id"),
                error=rpc_error(INVALID_REQUEST, str(exc)),
            ).to_dict()

        if req.jsonrpc != "2.0" or not req.method:
            resp = JsonRpcResponse(
                id=req.id,
                error=rpc_error(
                    INVALID_REQUEST, "malformed JSON-RPC envelope"
                ),
            )
            return None if req.is_notification() else resp.to_dict()

        caller = caller or self._default_caller
        transport = transport or self._default_transport

        try:
            result = await self._dispatch(req, caller, transport)
        except _RpcException as exc:
            resp = JsonRpcResponse(id=req.id, error=exc.as_error())
            return None if req.is_notification() else resp.to_dict()
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("unhandled mcp error: %s", exc)
            resp = JsonRpcResponse(
                id=req.id,
                error=rpc_error(INTERNAL_ERROR, str(exc)),
            )
            return None if req.is_notification() else resp.to_dict()

        if req.is_notification():
            return None
        return JsonRpcResponse(id=req.id, result=result).to_dict()

    # ---- dispatch ---------------------------------------------------

    async def _dispatch(
        self,
        req: JsonRpcRequest,
        caller: str,
        transport: str,
    ) -> Any:
        method = req.method
        params = req.params or {}

        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {"ok": True, "ts": utcnow().isoformat()}
        if method == "tools/list":
            return {"tools": self._list_tools()}
        if method == "tools/call":
            return await self._call_tool(params, caller, transport)
        if method == "resources/list":
            return {
                "resources": [r.to_listing() for r in self._resources]
            }
        if method == "resources/read":
            return await self._read_resource(params)

        raise _RpcException(
            METHOD_NOT_FOUND, f"method not supported: {method}"
        )

    # ---- individual methods -----------------------------------------

    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # MCP `initialize` handshake: client tells us what protocol
        # version / capabilities it supports; we echo ours back.
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": server_info(),
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False, "subscribe": False},
            },
            "clientRequested": {
                "protocolVersion": params.get("protocolVersion"),
                "clientInfo": params.get("clientInfo"),
            },
        }

    def _list_tools(self) -> List[Dict[str, Any]]:
        return [t.to_tool_listing() for t in self._registry.list_tools()]

    async def _call_tool(
        self,
        params: Dict[str, Any],
        caller: str,
        transport: str,
    ) -> Dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            raise _RpcException(
                INVALID_PARAMS, "name is required"
            )
        if not isinstance(arguments, dict):
            raise _RpcException(
                INVALID_PARAMS, "arguments must be an object"
            )

        tool = self._registry.get(name)
        if tool is None:
            raise _RpcException(
                TOOL_NOT_FOUND, f"tool not registered: {name}"
            )
        if not tool.enabled:
            raise _RpcException(
                TOOL_DISABLED, f"tool disabled: {name}"
            )

        started_at = utcnow()
        t0 = time.perf_counter()
        status = "ok"
        error_text: Optional[str] = None
        result_payload: Any = None

        try:
            result_payload = await self._registry.call(
                name, arguments
            )
            envelope: Dict[str, Any] = {
                "tool": name,
                "ok": True,
                "result": result_payload,
            }
            return envelope
        except _RpcException:
            raise
        except Exception as exc:
            status = "error"
            error_text = str(exc)
            raise _RpcException(TOOL_FAILED, error_text) from exc
        finally:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            completed_at = utcnow()
            await self._record_invocation(
                tool_name=name,
                tool_version=tool.version,
                caller=caller,
                transport=transport,
                params=arguments,
                status=status,
                result={"result": result_payload}
                if status == "ok"
                else None,
                error=error_text,
                latency_ms=latency_ms,
                started_at=started_at,
                completed_at=completed_at,
            )

    async def _read_resource(
        self, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            raise _RpcException(INVALID_PARAMS, "uri is required")
        if self._resource_loader is None:
            raise _RpcException(
                METHOD_NOT_FOUND, "no resource loader wired"
            )
        try:
            mime, body = await self._resource_loader(uri)
        except KeyError as exc:
            raise _RpcException(
                INVALID_PARAMS, f"resource not found: {uri}"
            ) from exc
        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": mime,
                    "text": body,
                }
            ]
        }

    # ---- audit ------------------------------------------------------

    async def _record_invocation(self, **fields: Any) -> None:
        if self._invocations is None:
            return
        try:
            await self._invocations.record(**fields)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("failed to record mcp invocation: %s", exc)


class _RpcException(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def as_error(self) -> Dict[str, Any]:
        return rpc_error(self.code, self.message, self.data)
