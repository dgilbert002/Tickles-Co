"""
shared.dashboard.providers — concrete SnapshotProviders that bind
the dashboard to existing Tickles phases.

* :class:`RegistryServicesProvider` exposes the in-memory
  :data:`shared.services.registry.SERVICE_REGISTRY`.
* :class:`SubmissionsStoreProvider` wraps the Phase-35
  :class:`BacktestSubmissionStore`.
* :class:`IntentsSqlProvider` pulls from the Phase-34
  ``strategy_intents_latest`` view via a pool.

Everything else can be added as later phases expose read-only views.
"""
from __future__ import annotations

from typing import Any, List, Optional


class RegistryServicesProvider:
    def __init__(self, registry: Optional[Any] = None) -> None:
        if registry is None:
            from shared.services.registry import (
                SERVICE_REGISTRY,
                register_builtin_services,
            )
            register_builtin_services()
            registry = SERVICE_REGISTRY
        self._registry = registry

    async def list_services(self) -> List[dict]:
        out: List[dict] = []
        for s in self._registry.list_services():
            out.append({
                "name": s.name, "kind": s.kind,
                "module": s.module, "enabled_on_vps": s.enabled_on_vps,
                "phase": s.tags.get("phase", "?") if s.tags else "?",
                "description": (s.description or "").split("\n")[0],
            })
        return out


class SubmissionsStoreProvider:
    def __init__(self, store: Any) -> None:
        self._store = store

    async def list_active(self, limit: int) -> List[dict]:
        rows = await self._store.list(active_only=True, limit=limit)
        return [r.to_dict() for r in rows]

    async def list_recent(self, limit: int) -> List[dict]:
        rows = await self._store.list(limit=limit)
        return [r.to_dict() for r in rows]


class IntentsSqlProvider:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def latest_intents(self, limit: int) -> List[dict]:
        try:
            rows = await self._pool.fetch_all(
                "SELECT * FROM public.strategy_intents_latest "
                "ORDER BY proposed_at DESC LIMIT $1",
                (int(limit),),
            )
        except Exception:
            return []
        return [dict(r) for r in rows]


__all__ = [
    "IntentsSqlProvider",
    "RegistryServicesProvider",
    "SubmissionsStoreProvider",
]
