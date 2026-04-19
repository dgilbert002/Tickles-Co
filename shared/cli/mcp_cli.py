"""
shared.cli.mcp_cli - operator CLI for the Phase 37 MCP stack.

Subcommands:
  apply-migration / migration-sql   DB migration helpers.
  tools-list                        List registered tools.
  tools-call <name> [--args JSON]   Invoke a tool via the registry.
  serve-stdio                       Run JSON-RPC over stdin/stdout.
  serve-http                        Run JSON-RPC over HTTP.
  invocations                       Print recent invocations.
  demo                              End-to-end in-memory showcase.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from shared.mcp import (
    InMemoryMcpPool,
    InvocationStore,
    McpServer,
    MIGRATION_PATH,
    ToolCatalogStore,
    ToolRegistry,
    invocation_to_dict,
    read_migration_sql,
)
from shared.mcp.registry import (
    build_backtest_status_tool,
    build_backtest_submit_tool,
    build_dashboard_snapshot_tool,
    build_ping_tool,
    build_regime_current_tool,
    build_services_list_tool,
    build_strategy_intents_tool,
)
from shared.mcp.transports import run_http, run_stdio


LOG = logging.getLogger("tickles.cli.mcp")


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"unserialisable: {type(obj)}")


def _dump(data: Any) -> None:
    print(json.dumps(data, sort_keys=True, default=_default))


async def _get_pool(dsn: Optional[str], in_memory: bool) -> Any:
    if in_memory or not dsn:
        return InMemoryMcpPool()
    import asyncpg  # type: ignore

    class _AsyncpgPool:
        def __init__(self, pool: Any) -> None:
            self._pool = pool

        class _Acquire:
            def __init__(self, pool: Any) -> None:
                self._pool = pool
                self._conn: Any = None

            async def __aenter__(self) -> Any:
                self._conn = await self._pool.acquire()
                return self._conn

            async def __aexit__(self, *exc: Any) -> None:
                if self._conn is not None:
                    await self._pool.release(self._conn)
                    self._conn = None

        def acquire(self) -> Any:
            return _AsyncpgPool._Acquire(self._pool)

        async def close(self) -> None:
            await self._pool.close()

    pg_pool = await asyncpg.create_pool(dsn)
    return _AsyncpgPool(pg_pool)


# --- Subcommands -------------------------------------------------------


def cmd_migration_sql(_: argparse.Namespace) -> int:
    sys.stdout.write(read_migration_sql())
    return 0


async def _apply_migration(dsn: str) -> None:
    import asyncpg  # type: ignore

    sql = read_migration_sql()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


def cmd_apply_migration(args: argparse.Namespace) -> int:
    dsn = args.dsn or os.environ.get("TICKLES_DSN")
    if not dsn:
        print(
            "--dsn or TICKLES_DSN env required to apply migration",
            file=sys.stderr,
        )
        return 2
    asyncio.run(_apply_migration(dsn))
    print(f"[ok] applied {MIGRATION_PATH}")
    return 0


def _build_demo_registry() -> ToolRegistry:
    """Registry wired to small deterministic in-memory providers so
    `demo` / tests can exercise the end-to-end flow without Postgres
    or the full Tickles runtime.
    """
    reg = ToolRegistry()

    ping_tool, ping_handler = build_ping_tool()
    reg.register(ping_tool, ping_handler)

    services: List[Dict[str, Any]] = [
        {
            "name": "arb-scanner",
            "kind": "service",
            "phase": "33",
            "enabled_on_vps": False,
        },
        {
            "name": "dashboard",
            "kind": "api",
            "phase": "36",
            "enabled_on_vps": False,
        },
    ]

    async def services_provider() -> List[Dict[str, Any]]:
        return services

    tool, handler = build_services_list_tool(services_provider)
    reg.register(tool, handler)

    intents: List[Dict[str, Any]] = [
        {
            "id": 1,
            "kind": "arb",
            "symbol": "BTC/USDT",
            "status": "pending",
            "notional_usd": 250.0,
        },
        {
            "id": 2,
            "kind": "souls",
            "symbol": "ETH/USDT",
            "status": "approved",
            "notional_usd": 500.0,
        },
    ]

    async def intents_provider(limit: int) -> List[Dict[str, Any]]:
        return intents[:limit]

    tool, handler = build_strategy_intents_tool(intents_provider)
    reg.register(tool, handler)

    submissions: Dict[str, Dict[str, Any]] = {}

    async def submit_fn(spec: Dict[str, Any]) -> Dict[str, Any]:
        import hashlib

        h = hashlib.sha256(
            json.dumps(spec, sort_keys=True).encode("utf-8")
        ).hexdigest()
        row = submissions.setdefault(
            h,
            {
                "submission_id": len(submissions) + 1,
                "spec_hash": h,
                "status": "queued",
                "spec": spec,
            },
        )
        return {"submitted": True, "submission": row}

    tool, handler = build_backtest_submit_tool(submit_fn)
    reg.register(tool, handler)

    async def status_fn(key: str) -> Optional[Dict[str, Any]]:
        return submissions.get(key)

    tool, handler = build_backtest_status_tool(status_fn)
    reg.register(tool, handler)

    async def snapshot_fn() -> Dict[str, Any]:
        return {
            "services": services,
            "intents": intents,
            "submissions": list(submissions.values()),
            "regime": {"label": "bull", "confidence": 0.72},
            "guardrails": {"active": [], "ok": True},
        }

    tool, handler = build_dashboard_snapshot_tool(snapshot_fn)
    reg.register(tool, handler)

    async def regime_fn() -> Dict[str, Any]:
        return {
            "label": "bull",
            "confidence": 0.72,
            "ts": datetime.utcnow().isoformat() + "Z",
        }

    tool, handler = build_regime_current_tool(regime_fn)
    reg.register(tool, handler)

    return reg


async def _tools_list() -> List[Dict[str, Any]]:
    reg = _build_demo_registry()
    return [
        {
            "name": t.name,
            "description": t.description,
            "version": t.version,
            "read_only": t.read_only,
            "tags": t.tags,
        }
        for t in reg.list_tools()
    ]


def cmd_tools_list(_: argparse.Namespace) -> int:
    data = asyncio.run(_tools_list())
    _dump({"count": len(data), "tools": data})
    return 0


async def _tools_call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    reg = _build_demo_registry()
    pool = InMemoryMcpPool()
    server = McpServer(
        reg,
        invocation_store=InvocationStore(pool),
        default_caller="cli",
        default_transport="cli",
    )
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    return await server.handle(payload, caller="cli", transport="cli")  # type: ignore[return-value]


def cmd_tools_call(args: argparse.Namespace) -> int:
    raw = args.args or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"invalid --args JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(parsed, dict):
        print("--args must decode to an object", file=sys.stderr)
        return 2
    resp = asyncio.run(_tools_call(args.name, parsed))
    _dump(resp)
    return 0


async def _serve_stdio_demo() -> None:
    reg = _build_demo_registry()
    pool = InMemoryMcpPool()
    server = McpServer(
        reg,
        invocation_store=InvocationStore(pool),
        default_caller="stdio",
        default_transport="stdio",
    )
    await run_stdio(server)


def cmd_serve_stdio(_: argparse.Namespace) -> int:
    asyncio.run(_serve_stdio_demo())
    return 0


async def _serve_http_demo(
    host: str, port: int, token: Optional[str]
) -> None:
    reg = _build_demo_registry()
    pool = InMemoryMcpPool()
    server = McpServer(
        reg,
        invocation_store=InvocationStore(pool),
        default_caller="http",
        default_transport="http",
    )
    await run_http(server, host=host, port=port, auth_token=token)


def cmd_serve_http(args: argparse.Namespace) -> int:
    try:
        asyncio.run(
            _serve_http_demo(args.host, args.port, args.token)
        )
    except KeyboardInterrupt:
        pass
    return 0


async def _demo(show_invocations: bool) -> Dict[str, Any]:
    """End-to-end in-memory showcase.

    1. Registers all built-in tools against an in-memory pool.
    2. Drives a JSON-RPC sequence: initialize -> tools/list -> a few
       tools/call invocations -> invocations listing.
    3. Asserts the catalogue was upserted and the audit log grew.
    """
    pool = InMemoryMcpPool()
    catalog = ToolCatalogStore(pool)
    invocations = InvocationStore(pool)

    reg = _build_demo_registry()
    for tool in reg.list_tools():
        await catalog.upsert(tool)

    server = McpServer(
        reg,
        invocation_store=invocations,
        default_caller="demo",
        default_transport="demo",
    )

    transcript: List[Dict[str, Any]] = []

    async def rpc(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": len(transcript) + 1,
            "method": method,
            "params": params,
        }
        resp = await server.handle(
            payload, caller="demo", transport="demo"
        )
        transcript.append(
            {"request": payload, "response": resp}
        )
        return resp  # type: ignore[return-value]

    await rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "mcp-cli-demo", "version": "0"},
        },
    )
    await rpc("tools/list", {})
    await rpc("tools/call", {"name": "ping", "arguments": {}})
    await rpc(
        "tools/call",
        {"name": "services.list", "arguments": {}},
    )
    await rpc(
        "tools/call",
        {
            "name": "backtest.submit",
            "arguments": {
                "spec": {
                    "strategy": "ma_crossover",
                    "symbol": "BTC/USDT",
                    "params": {"fast": 5, "slow": 20},
                }
            },
        },
    )
    await rpc(
        "tools/call",
        {"name": "dashboard.snapshot", "arguments": {}},
    )

    recent = await invocations.list_recent(limit=10)
    listed = await catalog.list_all()

    result: Dict[str, Any] = {
        "tools_registered": [t.name for t in listed],
        "transcript_steps": len(transcript),
        "invocations_logged": len(recent),
    }
    if show_invocations:
        result["invocations"] = [
            invocation_to_dict(inv) for inv in recent
        ]
        result["transcript"] = transcript
    return result


def cmd_demo(args: argparse.Namespace) -> int:
    data = asyncio.run(_demo(show_invocations=args.verbose))
    _dump(data)
    return 0


async def _invocations(dsn: Optional[str], limit: int) -> List[Dict[str, Any]]:
    pool = await _get_pool(dsn, in_memory=dsn is None)
    store = InvocationStore(pool)
    rows = await store.list_recent(limit=limit)
    return [invocation_to_dict(r) for r in rows]


def cmd_invocations(args: argparse.Namespace) -> int:
    data = asyncio.run(_invocations(args.dsn, args.limit))
    _dump({"count": len(data), "invocations": data})
    return 0


# --- parser ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp_cli",
        description="Phase 37 MCP operator CLI.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("migration-sql")
    p.set_defaults(func=cmd_migration_sql)

    p = sub.add_parser("apply-migration")
    p.add_argument("--dsn")
    p.set_defaults(func=cmd_apply_migration)

    p = sub.add_parser("tools-list")
    p.set_defaults(func=cmd_tools_list)

    p = sub.add_parser("tools-call")
    p.add_argument("name")
    p.add_argument("--args", default="{}")
    p.set_defaults(func=cmd_tools_call)

    p = sub.add_parser("serve-stdio")
    p.set_defaults(func=cmd_serve_stdio)

    p = sub.add_parser("serve-http")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8737)
    p.add_argument("--token", default=None)
    p.set_defaults(func=cmd_serve_http)

    p = sub.add_parser("invocations")
    p.add_argument("--dsn")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_invocations)

    p = sub.add_parser("demo")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_demo)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(
        level=os.environ.get("TICKLES_LOGLEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
