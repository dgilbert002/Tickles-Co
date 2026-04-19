"""In-memory pool for Phase 36 dashboard tests."""
from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryDashboardPool:
    def __init__(self) -> None:
        self.users: List[Dict[str, Any]] = []
        self.otps: List[Dict[str, Any]] = []
        self.sessions: List[Dict[str, Any]] = []
        self._u = itertools.count(1)
        self._o = itertools.count(1)
        self._s = itertools.count(1)

    async def execute(self, sql: str, params: Sequence[Any]) -> int:
        sql = sql.strip()

        if sql.startswith("UPDATE public.dashboard_users"):
            if "SET last_login_at = NOW()" in sql:
                chat = params[0]
                for u in self.users:
                    if u["chat_id"] == chat:
                        u["last_login_at"] = _now()
                        return 1
                return 0
            if "SET enabled = $2" in sql:
                chat, enabled = params[0], params[1]
                for u in self.users:
                    if u["chat_id"] == chat:
                        u["enabled"] = bool(enabled)
                        return 1
                return 0

        if sql.startswith("UPDATE public.dashboard_otps"):
            oid = int(params[0])
            for o in self.otps:
                if o["id"] == oid:
                    if "consumed_at = NOW()" in sql:
                        o["consumed_at"] = _now()
                    o["attempts"] = int(o.get("attempts") or 0) + 1
                    return 1
            return 0

        if sql.startswith("UPDATE public.dashboard_sessions"):
            if "SET last_seen_at" in sql:
                sid = int(params[0])
                for s in self.sessions:
                    if s["id"] == sid:
                        s["last_seen_at"] = _now()
                        return 1
                return 0
            if "SET revoked_at = NOW()" in sql and "WHERE id = $1" in sql:
                sid = int(params[0])
                for s in self.sessions:
                    if s["id"] == sid:
                        s["revoked_at"] = _now()
                        return 1
                return 0
            if "WHERE chat_id = $1 AND revoked_at IS NULL" in sql:
                chat = params[0]
                count = 0
                for s in self.sessions:
                    if s["chat_id"] == chat and s.get("revoked_at") is None:
                        s["revoked_at"] = _now()
                        count += 1
                return count

        raise NotImplementedError(
            f"InMemoryDashboardPool.execute: {sql!r}")

    async def fetch_one(
        self, sql: str, params: Sequence[Any],
    ) -> Optional[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("INSERT INTO public.dashboard_users"):
            chat_id, display_name, role, enabled = params
            existing = next(
                (u for u in self.users if u["chat_id"] == chat_id),
                None,
            )
            if existing:
                existing["display_name"] = display_name
                existing["role"] = role
                existing["enabled"] = bool(enabled)
                return {"id": existing["id"]}
            uid = next(self._u)
            self.users.append({
                "id": uid, "chat_id": chat_id,
                "display_name": display_name, "role": role,
                "enabled": bool(enabled),
                "created_at": _now(), "last_login_at": None,
            })
            return {"id": uid}

        if sql.startswith("SELECT * FROM public.dashboard_users WHERE chat_id"):
            chat = params[0]
            u = next(
                (x for x in self.users if x["chat_id"] == chat), None,
            )
            return dict(u) if u else None

        if sql.startswith("INSERT INTO public.dashboard_otps"):
            chat_id, code_hash, expires_at, client_ip = params
            oid = next(self._o)
            self.otps.append({
                "id": oid, "chat_id": chat_id, "code_hash": code_hash,
                "issued_at": _now(), "expires_at": expires_at,
                "consumed_at": None, "attempts": 0,
                "client_ip": client_ip,
            })
            return {"id": oid}

        if sql.startswith("SELECT * FROM public.dashboard_otps "
                          "WHERE chat_id = $1 AND code_hash"):
            chat, code = params[0], params[1]
            now = _now()
            rows = [
                o for o in self.otps
                if o["chat_id"] == chat
                and o["code_hash"] == code
                and o.get("consumed_at") is None
                and o.get("expires_at") is not None
                and o["expires_at"] > now
            ]
            rows.sort(
                key=lambda r: r.get("issued_at") or now, reverse=True,
            )
            return dict(rows[0]) if rows else None

        if sql.startswith("INSERT INTO public.dashboard_sessions"):
            chat_id, token_hash, expires_at, user_agent, client_ip = params
            sid = next(self._s)
            self.sessions.append({
                "id": sid, "chat_id": chat_id, "token_hash": token_hash,
                "issued_at": _now(), "expires_at": expires_at,
                "revoked_at": None, "last_seen_at": None,
                "user_agent": user_agent, "client_ip": client_ip,
            })
            return {"id": sid}

        if sql.startswith("SELECT * FROM public.dashboard_sessions_active "
                          "WHERE token_hash"):
            token = params[0]
            now = _now()
            for s in self.sessions:
                if (
                    s["token_hash"] == token
                    and s.get("revoked_at") is None
                    and s.get("expires_at") is not None
                    and s["expires_at"] > now
                ):
                    return dict(s)
            return None

        raise NotImplementedError(
            f"InMemoryDashboardPool.fetch_one: {sql!r}")

    async def fetch_all(
        self, sql: str, params: Sequence[Any],
    ) -> List[Dict[str, Any]]:
        sql = sql.strip()

        if sql.startswith("SELECT * FROM public.dashboard_users "
                          "WHERE enabled = TRUE"):
            rows = [u for u in self.users if u.get("enabled", True)]
            rows.sort(key=lambda r: r["chat_id"])
            return [dict(r) for r in rows]

        if sql.startswith("SELECT * FROM public.dashboard_users"):
            rows = list(self.users)
            rows.sort(key=lambda r: r["chat_id"])
            return [dict(r) for r in rows]

        if sql.startswith("SELECT * FROM public.dashboard_otps WHERE "
                          "chat_id = $1"):
            chat = params[0]
            limit = int(params[1])
            rows = [o for o in self.otps if o["chat_id"] == chat]
            rows.sort(
                key=lambda r: r.get("issued_at") or _now(), reverse=True,
            )
            return [dict(r) for r in rows[:limit]]

        if sql.startswith("SELECT * FROM public.dashboard_sessions_active "
                          "WHERE chat_id"):
            chat = params[0]
            limit = int(params[1])
            now = _now()
            rows = [
                s for s in self.sessions
                if s["chat_id"] == chat
                and s.get("revoked_at") is None
                and s.get("expires_at") is not None
                and s["expires_at"] > now
            ]
            rows.sort(
                key=lambda r: r.get("issued_at") or now, reverse=True,
            )
            return [dict(r) for r in rows[:limit]]

        if sql.startswith("SELECT * FROM public.dashboard_sessions_active"):
            limit = int(params[0])
            now = _now()
            rows = [
                s for s in self.sessions
                if s.get("revoked_at") is None
                and s.get("expires_at") is not None
                and s["expires_at"] > now
            ]
            rows.sort(
                key=lambda r: r.get("issued_at") or now, reverse=True,
            )
            return [dict(r) for r in rows[:limit]]

        raise NotImplementedError(
            f"InMemoryDashboardPool.fetch_all: {sql!r}")


__all__ = ["InMemoryDashboardPool"]
