"""
shared.dashboard.snapshot — assembles the DashboardSnapshot JSON
structure from whatever data sources are wired in.

We keep this defensive: any one lookup can fail (e.g. the regime
service isn't wired yet, or the VPS is offline) and the snapshot
still returns with a clear note in ``snapshot.notes`` explaining
which data was missing. The dashboard UI can then render the
healthy sections and flag the gaps.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional, Protocol

from shared.dashboard.protocol import DashboardSnapshot

LOG = logging.getLogger("tickles.dashboard.snapshot")


class ServicesProvider(Protocol):
    async def list_services(self) -> List[dict]: ...


class SubmissionsProvider(Protocol):
    async def list_active(self, limit: int) -> List[dict]: ...
    async def list_recent(self, limit: int) -> List[dict]: ...


class IntentsProvider(Protocol):
    async def latest_intents(self, limit: int) -> List[dict]: ...


class RegimeProvider(Protocol):
    async def current(self) -> Optional[dict]: ...


class GuardrailsProvider(Protocol):
    async def active(self) -> List[dict]: ...


@dataclass
class SnapshotProviders:
    services: Optional[ServicesProvider] = None
    submissions: Optional[SubmissionsProvider] = None
    intents: Optional[IntentsProvider] = None
    regime: Optional[RegimeProvider] = None
    guardrails: Optional[GuardrailsProvider] = None


@dataclass
class SnapshotBuilder:
    providers: SnapshotProviders = field(default_factory=SnapshotProviders)
    max_services: int = 50
    max_submissions: int = 20
    max_intents: int = 20

    async def build(self) -> DashboardSnapshot:
        snap = DashboardSnapshot(
            generated_at=datetime.now(timezone.utc),
        )
        await self._load_services(snap)
        await self._load_submissions(snap)
        await self._load_intents(snap)
        await self._load_regime(snap)
        await self._load_guardrails(snap)
        return snap

    async def _load_services(self, snap: DashboardSnapshot) -> None:
        if self.providers.services is None:
            snap.notes.append("services: provider not wired")
            return
        try:
            services = await self.providers.services.list_services()
            snap.services = services[: self.max_services]
            snap.services_total = len(services)
        except Exception as e:  # pragma: no cover - defensive
            LOG.warning("services provider failed: %s", e)
            snap.notes.append(f"services: {e}")

    async def _load_submissions(self, snap: DashboardSnapshot) -> None:
        if self.providers.submissions is None:
            snap.notes.append("submissions: provider not wired")
            return
        try:
            active = await self.providers.submissions.list_active(
                self.max_submissions,
            )
            recent = await self.providers.submissions.list_recent(
                self.max_submissions,
            )
            snap.submissions_active = len(active)
            snap.submissions_recent = recent
        except Exception as e:
            LOG.warning("submissions provider failed: %s", e)
            snap.notes.append(f"submissions: {e}")

    async def _load_intents(self, snap: DashboardSnapshot) -> None:
        if self.providers.intents is None:
            snap.notes.append("intents: provider not wired")
            return
        try:
            snap.latest_intents = await self.providers.intents.latest_intents(
                self.max_intents,
            )
        except Exception as e:
            LOG.warning("intents provider failed: %s", e)
            snap.notes.append(f"intents: {e}")

    async def _load_regime(self, snap: DashboardSnapshot) -> None:
        if self.providers.regime is None:
            return
        try:
            snap.regime_current = await self.providers.regime.current()
        except Exception as e:
            LOG.warning("regime provider failed: %s", e)
            snap.notes.append(f"regime: {e}")

    async def _load_guardrails(self, snap: DashboardSnapshot) -> None:
        if self.providers.guardrails is None:
            return
        try:
            snap.guardrails_active = await self.providers.guardrails.active()
        except Exception as e:
            LOG.warning("guardrails provider failed: %s", e)
            snap.notes.append(f"guardrails: {e}")


def snapshot_to_dict(snap: DashboardSnapshot) -> dict:
    def serialise(v: Any) -> Any:
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, list):
            return [serialise(x) for x in v]
        if isinstance(v, dict):
            return {k: serialise(val) for k, val in v.items()}
        return v

    return {
        "generated_at": snap.generated_at.isoformat(),
        "services": serialise(snap.services),
        "services_total": snap.services_total,
        "submissions_active": snap.submissions_active,
        "submissions_recent": serialise(snap.submissions_recent),
        "latest_intents": serialise(snap.latest_intents),
        "regime_current": serialise(snap.regime_current),
        "guardrails_active": serialise(snap.guardrails_active),
        "notes": list(snap.notes),
    }


__all__ = [
    "SnapshotBuilder",
    "SnapshotProviders",
    "snapshot_to_dict",
]
