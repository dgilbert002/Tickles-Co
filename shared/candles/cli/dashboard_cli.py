"""
shared.cli.dashboard_cli — operator CLI for the Phase 36 owner
dashboard.

Subcommands:
  apply-migration / migration-sql     DB migration helpers.
  user-add / user-list / user-disable OTP allowlist management.
  request-otp / verify-otp            OTP flow (exposes raw values).
  sessions                            List active sessions.
  revoke-sessions                     Kill all sessions for a chat_id.
  serve                               Run the aiohttp server.
  demo                                End-to-end in-memory showcase.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Optional, Sequence

from shared.dashboard import (
    DashboardUser,
    InMemoryDashboardPool,
    MIGRATION_PATH,
    RegistryServicesProvider,
    SnapshotBuilder,
    SnapshotProviders,
    SubmissionsStoreProvider,
    build_auth_from_pool,
    read_migration_sql,
    sender_from_env,
    snapshot_to_dict,
)
from shared.dashboard.store import (
    DashboardUserStore,
    DashboardSessionStore,
)
from shared.dashboard.telegram import NullTelegramSender

LOG = logging.getLogger("tickles.cli.dashboard")


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    raise TypeError(f"unserialisable: {type(obj)}")


def _dump(data: Any) -> None:
    print(json.dumps(data, sort_keys=True, default=_default))


async def _get_pool(dsn: Optional[str], in_memory: bool) -> Any:
    if in_memory or not dsn:
        return InMemoryDashboardPool()
    import asyncpg  # type: ignore

    class _AsyncpgPool:
        def __init__(self, pool: Any) -> None:
            self._pool = pool

        async def execute(self, sql: str, params: Sequence[Any]) -> int:
            async with self._pool.acquire() as conn:
                r = await conn.execute(sql, *params)
            return int(r.split()[-1]) if r and r[-1].isdigit() else 0

        async def fetch_one(self, sql: str, params: Sequence[Any]):
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(sql, *params)
            return dict(row) if row else None

        async def fetch_all(self, sql: str, params: Sequence[Any]):
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
            return [dict(r) for r in rows]

    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)
    return _AsyncpgPool(pool)


def _handle_migration_sql(_: argparse.Namespace) -> int:
    print(read_migration_sql())
    return 0


def _handle_apply_migration(args: argparse.Namespace) -> int:
    if args.path_only:
        print(str(MIGRATION_PATH))
        return 0
    _dump({
        "migration_path": str(MIGRATION_PATH),
        "apply_with": (
            "psql -h 127.0.0.1 -U admin -d tickles_shared "
            f"-f {MIGRATION_PATH}"
        ),
        "ok": True,
    })
    return 0


async def _handle_user_add_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    users = DashboardUserStore(pool)
    uid = await users.upsert(DashboardUser(
        id=None, chat_id=args.chat_id,
        display_name=args.display_name,
        role=args.role, enabled=not args.disabled,
    ))
    _dump({"ok": True, "id": uid, "chat_id": args.chat_id})
    return 0


async def _handle_user_list_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    users = DashboardUserStore(pool)
    rows = await users.list(enabled_only=args.enabled_only)
    _dump({
        "ok": True, "count": len(rows),
        "users": [
            {
                "id": u.id, "chat_id": u.chat_id,
                "display_name": u.display_name, "role": u.role,
                "enabled": u.enabled,
            }
            for u in rows
        ],
    })
    return 0


async def _handle_user_disable_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    users = DashboardUserStore(pool)
    await users.set_enabled(args.chat_id, False)
    _dump({"ok": True, "chat_id": args.chat_id, "enabled": False})
    return 0


async def _handle_request_otp_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    sender = NullTelegramSender() if args.null_sender else sender_from_env()
    auth = build_auth_from_pool(pool, sender=sender)
    result = await auth.issue_otp(args.chat_id)
    _dump({
        "ok": True, "chat_id": args.chat_id,
        "code": result.code if args.show_code else "<sent>",
        "expires_at": result.expires_at.isoformat(),
        "delivered": result.delivery_ok,
        "delivery_error": result.delivery_error,
    })
    return 0


async def _handle_verify_otp_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    auth = build_auth_from_pool(pool, sender=NullTelegramSender())
    result = await auth.verify_otp(args.chat_id, args.code)
    _dump({
        "ok": True, "chat_id": args.chat_id,
        "token": result.token,
        "expires_at": result.expires_at.isoformat(),
        "session_id": result.session_id,
    })
    return 0


async def _handle_sessions_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = DashboardSessionStore(pool)
    rows = await store.list_active(
        chat_id=args.chat_id, limit=args.limit,
    )
    _dump({
        "ok": True, "count": len(rows),
        "sessions": [
            {
                "id": s.id, "chat_id": s.chat_id,
                "expires_at": s.expires_at,
                "last_seen_at": s.last_seen_at,
                "user_agent": s.user_agent,
            }
            for s in rows
        ],
    })
    return 0


async def _handle_revoke_sessions_async(args: argparse.Namespace) -> int:
    pool = await _get_pool(args.dsn, args.in_memory)
    store = DashboardSessionStore(pool)
    n = await store.revoke_all_for(args.chat_id)
    _dump({"ok": True, "chat_id": args.chat_id, "revoked": n})
    return 0


async def _handle_serve_async(args: argparse.Namespace) -> int:
    from shared.dashboard.server import build_app, run_server
    pool = await _get_pool(args.dsn, args.in_memory)
    auth = build_auth_from_pool(pool, sender=sender_from_env())
    providers = _build_providers(args)
    app = build_app(auth, providers, expose_otp=args.expose_otp)
    await run_server(app, args.host, args.port)
    return 0


def _build_providers(_args: argparse.Namespace) -> SnapshotProviders:
    providers = SnapshotProviders()
    try:
        providers.services = RegistryServicesProvider()
    except Exception as e:  # pragma: no cover - defensive
        LOG.warning("services provider unavailable: %s", e)
    return providers


async def _handle_demo_async(args: argparse.Namespace) -> int:
    pool = InMemoryDashboardPool()
    users = DashboardUserStore(pool)
    await users.upsert(DashboardUser(
        id=None, chat_id="12345",
        display_name="Owner Demo", role="owner", enabled=True,
    ))
    sender = NullTelegramSender()
    auth = build_auth_from_pool(pool, sender=sender)

    print("[demo] enrolled user chat_id=12345 (display=Owner Demo)")

    issued = await auth.issue_otp("12345", client_ip="127.0.0.1")
    print(
        f"[demo] issued OTP code={issued.code} "
        f"expires_at={issued.expires_at.isoformat()}"
    )
    print(
        f"[demo] telegram transport delivered={issued.delivery_ok} "
        f"(NullTelegramSender - appended to log)"
    )

    session = await auth.verify_otp(
        "12345", issued.code, user_agent="tickles-demo-cli",
    )
    print(
        f"[demo] verified OTP, issued session id={session.session_id} "
        f"token={session.token[:14]}..."
    )

    u, s = await auth.authenticate_token(session.token)
    print(
        f"[demo] token authenticates user={u.chat_id} role={u.role} "
        f"session_id={s.id}"
    )

    providers = _build_providers(args)
    from shared.backtest_submit import BacktestSubmissionStore
    from shared.backtest_submit.memory_pool import (
        InMemoryBacktestSubmitPool,
    )
    bt_pool = InMemoryBacktestSubmitPool()
    submissions_store = BacktestSubmissionStore(bt_pool)
    providers.submissions = SubmissionsStoreProvider(submissions_store)

    builder = SnapshotBuilder(providers=providers)
    snap = await builder.build()
    data = snapshot_to_dict(snap)

    print()
    print("[demo] snapshot summary:")
    print(f"  services registered : {data['services_total']}")
    print(f"  submissions active  : {data['submissions_active']}")
    print(f"  latest intents      : {len(data['latest_intents'])}")
    print(f"  notes               : {data['notes']}")

    print()
    print("[demo] first 5 services from the registry:")
    for svc in data["services"][:5]:
        print(
            f"  - {svc['name']:22s} kind={svc['kind']:10s} "
            f"phase={svc['phase']:<3s} vps={svc['enabled_on_vps']}"
        )
    return 0


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=os.environ.get("TICKLES_BT_DSN"))
    p.add_argument("--in-memory", action="store_true")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dashboard_cli",
        description="Phase 36 Owner Dashboard + Telegram OTP.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="SUBCOMMAND")

    sp = sub.add_parser("apply-migration")
    sp.add_argument("--path-only", action="store_true")
    sub.add_parser("migration-sql")

    sp = sub.add_parser("user-add")
    sp.add_argument("chat_id")
    sp.add_argument("--display-name", default=None)
    sp.add_argument("--role", default="owner")
    sp.add_argument("--disabled", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("user-list")
    sp.add_argument("--enabled-only", action="store_true")
    _add_common(sp)

    sp = sub.add_parser("user-disable")
    sp.add_argument("chat_id")
    _add_common(sp)

    sp = sub.add_parser("request-otp")
    sp.add_argument("chat_id")
    sp.add_argument("--show-code", action="store_true",
                    help="Echo the raw OTP (dev only).")
    sp.add_argument("--null-sender", action="store_true",
                    help="Force NullTelegramSender (no outgoing HTTP).")
    _add_common(sp)

    sp = sub.add_parser("verify-otp")
    sp.add_argument("chat_id")
    sp.add_argument("code")
    _add_common(sp)

    sp = sub.add_parser("sessions")
    sp.add_argument("--chat-id", default=None)
    sp.add_argument("--limit", type=int, default=50)
    _add_common(sp)

    sp = sub.add_parser("revoke-sessions")
    sp.add_argument("chat_id")
    _add_common(sp)

    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8770)
    sp.add_argument("--expose-otp", action="store_true",
                    help="Expose raw OTP in request-otp response (dev only).")
    _add_common(sp)

    sub.add_parser("demo")

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if args.cmd == "migration-sql":
        return _handle_migration_sql(args)
    if args.cmd == "apply-migration":
        return _handle_apply_migration(args)
    if args.cmd == "user-add":
        return asyncio.run(_handle_user_add_async(args))
    if args.cmd == "user-list":
        return asyncio.run(_handle_user_list_async(args))
    if args.cmd == "user-disable":
        return asyncio.run(_handle_user_disable_async(args))
    if args.cmd == "request-otp":
        return asyncio.run(_handle_request_otp_async(args))
    if args.cmd == "verify-otp":
        return asyncio.run(_handle_verify_otp_async(args))
    if args.cmd == "sessions":
        return asyncio.run(_handle_sessions_async(args))
    if args.cmd == "revoke-sessions":
        return asyncio.run(_handle_revoke_sessions_async(args))
    if args.cmd == "serve":
        return asyncio.run(_handle_serve_async(args))
    if args.cmd == "demo":
        return asyncio.run(_handle_demo_async(args))
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
