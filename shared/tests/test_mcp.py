"""Phase 37 MCP tests.

Covers:
    * migration presence + SQL shape
    * in-memory pool + store round trips
    * tool registry (register, enable/disable, call)
    * JSON-RPC server (initialize, tools/list, tools/call, errors)
    * stdio transport round trip
    * HTTP transport via aiohttp in-process
    * service registry registration for mcp-server
    * audit invocations are recorded
"""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any, Dict, List

from aiohttp import web  # noqa: F401 - used via TestServer
from aiohttp.test_utils import TestClient, TestServer

from shared.mcp import (
    InMemoryMcpPool,
    InvocationStore,
    McpServer,
    McpTool,
    MIGRATION_PATH,
    ToolCatalogStore,
    ToolRegistry,
    read_migration_sql,
)
from shared.mcp.protocol import (
    METHOD_NOT_FOUND,
    TOOL_NOT_FOUND,
)
from shared.mcp.registry import (
    build_backtest_submit_tool,
    build_ping_tool,
    build_services_list_tool,
)
from shared.mcp.transports.http import build_http_app
from shared.mcp.transports.stdio import run_stdio
from shared.services.registry import SERVICE_REGISTRY


# ---------- migration / package ---------------------------------------


def test_migration_file_exists() -> None:
    assert MIGRATION_PATH.exists()
    sql = read_migration_sql()
    assert "public.mcp_tools" in sql
    assert "public.mcp_invocations" in sql
    assert "public.mcp_invocations_recent" in sql


def test_service_registry_has_mcp_server() -> None:
    svc = SERVICE_REGISTRY.get("mcp-server")
    assert svc is not None
    assert svc.tags.get("phase") == "37"
    assert svc.enabled_on_vps is False


# ---------- in-memory pool + stores -----------------------------------


def test_tool_catalog_store_upsert_and_list() -> None:
    async def _run() -> None:
        pool = InMemoryMcpPool()
        store = ToolCatalogStore(pool)
        tool = McpTool(
            name="x.test",
            description="t",
            version="1",
            input_schema={"type": "object"},
            tags={"phase": "37"},
        )
        await store.upsert(tool)
        rows = await store.list_all()
        assert len(rows) == 1
        assert rows[0].name == "x.test"
        assert rows[0].tags["phase"] == "37"

        await store.set_enabled("x.test", False)
        enabled = await store.list_enabled()
        assert enabled == []
        await store.set_enabled("x.test", True)
        enabled = await store.list_enabled()
        assert len(enabled) == 1

    asyncio.run(_run())


def test_invocation_store_record_and_recent() -> None:
    async def _run() -> None:
        from datetime import datetime, timezone

        pool = InMemoryMcpPool()
        store = InvocationStore(pool)
        start = datetime.now(tz=timezone.utc)
        inv_id = await store.record(
            tool_name="ping",
            tool_version="1",
            caller="tester",
            transport="test",
            params={"a": 1},
            status="ok",
            result={"pong": True},
            error=None,
            latency_ms=3,
            started_at=start,
            completed_at=start,
        )
        assert inv_id >= 1
        recent = await store.list_recent(limit=5)
        assert len(recent) == 1
        assert recent[0].tool_name == "ping"
        assert recent[0].status == "ok"

    asyncio.run(_run())


# ---------- tool registry ---------------------------------------------


def test_registry_register_call_disable() -> None:
    async def _run() -> None:
        reg = ToolRegistry()
        tool, handler = build_ping_tool()
        reg.register(tool, handler)
        assert [t.name for t in reg.list_tools()] == ["ping"]
        out = await reg.call("ping", {})
        assert out["pong"] is True

        reg.set_enabled("ping", False)
        try:
            await reg.call("ping", {})
        except PermissionError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected PermissionError")

        try:
            await reg.call("missing", {})
        except KeyError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected KeyError")

    asyncio.run(_run())


def test_services_list_tool_uses_provider() -> None:
    async def _run() -> None:
        services: List[Dict[str, Any]] = [
            {"name": "a", "kind": "collector"},
            {"name": "b", "kind": "api"},
        ]

        async def provider() -> List[Dict[str, Any]]:
            return services

        tool, handler = build_services_list_tool(provider)
        reg = ToolRegistry()
        reg.register(tool, handler)
        out = await reg.call("services.list", {})
        assert out["count"] == 2
        assert out["services"][0]["name"] == "a"

    asyncio.run(_run())


# ---------- JSON-RPC server ------------------------------------------


def _make_server() -> tuple[McpServer, InMemoryMcpPool, ToolRegistry]:
    pool = InMemoryMcpPool()
    reg = ToolRegistry()
    reg.register(*build_ping_tool())

    async def services_provider() -> List[Dict[str, Any]]:
        return [{"name": "svc-a"}]

    reg.register(*build_services_list_tool(services_provider))

    async def submit_fn(spec: Dict[str, Any]) -> Dict[str, Any]:
        return {"submitted": True, "spec": spec}

    reg.register(*build_backtest_submit_tool(submit_fn))

    server = McpServer(
        reg,
        invocation_store=InvocationStore(pool),
        default_caller="test",
        default_transport="test",
    )
    return server, pool, reg


def test_server_initialize_and_list() -> None:
    async def _run() -> None:
        server, _, _ = _make_server()

        resp = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "clientInfo": {"name": "t", "version": "0"},
                },
            }
        )
        assert resp is not None
        assert resp["result"]["protocolVersion"] == "2024-11-05"

        resp = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )
        assert resp is not None
        names = [t["name"] for t in resp["result"]["tools"]]
        assert "ping" in names
        assert "services.list" in names

    asyncio.run(_run())


def test_server_tools_call_success_and_audit() -> None:
    async def _run() -> None:
        server, pool, _ = _make_server()
        store = InvocationStore(pool)

        resp = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "backtest.submit",
                    "arguments": {
                        "spec": {
                            "strategy": "ma",
                            "symbol": "BTC/USDT",
                        }
                    },
                },
            },
            caller="tester",
            transport="test",
        )
        assert resp is not None
        assert "error" not in resp
        assert resp["result"]["ok"] is True
        assert resp["result"]["result"]["submitted"] is True

        recent = await store.list_recent(limit=10)
        assert len(recent) == 1
        assert recent[0].tool_name == "backtest.submit"
        assert recent[0].status == "ok"
        assert recent[0].caller == "tester"

    asyncio.run(_run())


def test_server_tools_call_not_found() -> None:
    async def _run() -> None:
        server, _, _ = _make_server()
        resp = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "nope", "arguments": {}},
            }
        )
        assert resp is not None
        assert resp["error"]["code"] == TOOL_NOT_FOUND

    asyncio.run(_run())


def test_server_method_not_found() -> None:
    async def _run() -> None:
        server, _, _ = _make_server()
        resp = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "does.not.exist",
                "params": {},
            }
        )
        assert resp is not None
        assert resp["error"]["code"] == METHOD_NOT_FOUND

    asyncio.run(_run())


def test_server_notification_returns_none() -> None:
    async def _run() -> None:
        server, _, _ = _make_server()
        resp = await server.handle(
            {"jsonrpc": "2.0", "method": "ping"}
        )
        assert resp is None

    asyncio.run(_run())


def test_server_invalid_params_on_tools_call() -> None:
    async def _run() -> None:
        server, _, _ = _make_server()
        resp = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {},
            }
        )
        assert resp is not None
        assert resp["error"]["code"] < 0


    asyncio.run(_run())


# ---------- stdio transport -------------------------------------------


def test_stdio_roundtrip() -> None:
    async def _run() -> None:
        server, _, _ = _make_server()
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "ping", "arguments": {}},
            },
        ]
        in_stream = io.StringIO(
            "\n".join(json.dumps(r) for r in requests) + "\n"
        )
        out_stream = io.StringIO()
        await run_stdio(
            server, stdin=in_stream, stdout=out_stream
        )
        lines = [
            ln for ln in out_stream.getvalue().splitlines() if ln
        ]
        assert len(lines) == 3
        decoded = [json.loads(ln) for ln in lines]
        assert decoded[0]["result"]["serverInfo"]["name"] == "tickles-mcp"
        assert decoded[2]["result"]["result"]["pong"] is True

    asyncio.run(_run())


# ---------- HTTP transport --------------------------------------------


def test_http_transport_with_bearer() -> None:
    async def _run() -> None:
        server, _, _ = _make_server()
        app = build_http_app(server, auth_token="secret-123")

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/healthz")
            assert resp.status == 200

            resp = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "ping",
                        "arguments": {},
                    },
                },
            )
            assert resp.status == 401

            resp = await client.post(
                "/mcp",
                headers={"Authorization": "Bearer secret-123"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "ping",
                        "arguments": {},
                    },
                },
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["result"]["ok"] is True

    asyncio.run(_run())


def test_http_transport_without_token_allows_all() -> None:
    async def _run() -> None:
        server, _, _ = _make_server()
        app = build_http_app(server, auth_token=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "ping",
                    "params": {},
                },
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["result"]["ok"] is True

    asyncio.run(_run())


# ---------- CLI smoke -------------------------------------------------


def test_cli_demo_runs() -> None:
    from shared.cli.mcp_cli import _demo

    data = asyncio.run(_demo(show_invocations=False))
    assert data["transcript_steps"] == 6
    assert data["invocations_logged"] >= 4
    assert "ping" in data["tools_registered"]
    assert "services.list" in data["tools_registered"]
    assert "backtest.submit" in data["tools_registered"]
