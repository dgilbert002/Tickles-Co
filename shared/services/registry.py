"""
shared.services.registry — registry of long-running Tickles services.

The registry is a single source of truth for the operator CLI and
the systemd unit templates. Each :class:`ServiceDescriptor` knows:

  * a stable name (matches the systemd unit name minus the
    ``tickles-`` prefix and ``.service`` suffix),
  * the "kind" (collector / gateway / worker / auditor / catalog /
    api),
  * the module path that a systemd template can execute via
    ``python -m <module>``,
  * an optional factory returning a :class:`ServiceDaemon` for
    in-process execution (used by tests and ``collectors_cli
    run-once``),
  * whether the service is enabled on the VPS today (so operators
    can see the mismatch between what we *can* run and what we
    *do* run).

Keeping the registry pure-Python has two benefits:

  1. Tests can inspect it.
  2. New services added in later phases (regime, banker, etc.)
     get a single place to register themselves — same pattern as
     :mod:`shared.backtest.engines` and :mod:`shared.features`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("tickles.services.registry")


DaemonFactory = Callable[[], Any]


@dataclass
class ServiceDescriptor:
    """Describe a long-running Tickles service."""

    name: str
    kind: str  # collector | gateway | worker | auditor | catalog | api | custom
    module: str
    description: str = ""
    systemd_unit: str = ""
    enabled_on_vps: bool = False
    factory: Optional[DaemonFactory] = None
    tags: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "module": self.module,
            "description": self.description,
            "systemd_unit": self.systemd_unit or f"tickles-{self.name}.service",
            "enabled_on_vps": self.enabled_on_vps,
            "has_factory": self.factory is not None,
            "tags": dict(self.tags),
        }


class ServiceRegistry:
    """Process-global registry of :class:`ServiceDescriptor`s."""

    def __init__(self) -> None:
        self._services: Dict[str, ServiceDescriptor] = {}

    def register(self, descriptor: ServiceDescriptor) -> None:
        if descriptor.name in self._services:
            logger.debug("service %s already registered; overwriting", descriptor.name)
        self._services[descriptor.name] = descriptor

    def get(self, name: str) -> ServiceDescriptor:
        if name not in self._services:
            raise KeyError(f"service not registered: {name}")
        return self._services[name]

    def list_services(self) -> List[ServiceDescriptor]:
        return sorted(self._services.values(), key=lambda s: (s.kind, s.name))

    def by_kind(self, kind: str) -> List[ServiceDescriptor]:
        return [s for s in self.list_services() if s.kind == kind]

    def __contains__(self, name: str) -> bool:
        return name in self._services

    def __len__(self) -> int:
        return len(self._services)


SERVICE_REGISTRY = ServiceRegistry()


def _seed_known_services() -> None:
    """Register the services we already run on the VPS today.

    Phase 22 does not start or rewire these — it only describes
    them so operators can see the full lineup in one CLI call.
    """
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="md-gateway",
            kind="gateway",
            module="shared.gateway.daemon",
            description="CCXT Pro WebSocket fan-out into Redis. Phase 17.",
            systemd_unit="tickles-md-gateway.service",
            enabled_on_vps=True,
            tags={"phase": "17"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="candle-daemon",
            kind="collector",
            module="candles.daemon",
            description="1m candle collector via CCXT, writes to tickles_shared.candles.",
            systemd_unit="tickles-candle-daemon.service",
            enabled_on_vps=True,
            tags={"phase": "13"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="catalog",
            kind="catalog",
            module="shared.catalog.daemon",
            description="Asset + instrument catalog service. Phase 14.",
            systemd_unit="tickles-catalog.service",
            enabled_on_vps=True,
            tags={"phase": "14"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="bt-workers",
            kind="worker",
            module="shared.backtest.worker",
            description="Backtest worker pool. Phase 16.",
            systemd_unit="tickles-bt-workers.service",
            enabled_on_vps=True,
            tags={"phase": "16"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="discord-collector",
            kind="collector",
            module="shared.collectors.discord.discord_collector",
            description="Discord message collector. Phase 3A.1 — disabled by default.",
            systemd_unit="tickles-discord-collector.service",
            enabled_on_vps=False,
            tags={"phase": "3A.1"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="news-rss",
            kind="collector",
            module="shared.collectors.news.run_news_collection",
            description="RSS news collector.",
            enabled_on_vps=False,
            tags={"phase": "3A.1"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="telegram-collector",
            kind="collector",
            module="shared.collectors.telegram.telegram_collector",
            description="Telegram channel collector.",
            enabled_on_vps=False,
            tags={"phase": "3A.1"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="tradingview-monitor",
            kind="collector",
            module="shared.collectors.telegram.tradingview_monitor",
            description="TradingView alert monitor.",
            enabled_on_vps=False,
            tags={"phase": "3A.1"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="auditor",
            kind="auditor",
            module="shared.cli.auditor_cli",
            description="Rule-1 Continuous Auditor. Phase 21 — runs via auditor_cli run.",
            enabled_on_vps=False,
            tags={"phase": "21"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="banker",
            kind="worker",
            module="shared.cli.treasury_cli",
            description=(
                "Balance/equity snapshot recorder + Treasury decision logger. "
                "Phase 25 — invoked via treasury_cli balances-record / evaluate."
            ),
            enabled_on_vps=False,
            tags={"phase": "25"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="executor",
            kind="worker",
            module="shared.cli.execution_cli",
            description=(
                "Execution Layer (Phase 26). Routes approved Treasury decisions "
                "through paper/ccxt/nautilus adapters and persists every order, "
                "fill, and position snapshot. Disabled on VPS until live "
                "strategy wiring lands in Phase 31+."
            ),
            enabled_on_vps=False,
            tags={"phase": "26"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="regime",
            kind="worker",
            module="shared.cli.regime_cli",
            description=(
                "Regime Service (Phase 27). Classifies market regime "
                "(bull/bear/sideways/crash/recovery/high_vol/low_vol) per "
                "universe/exchange/symbol/timeframe and persists signals to "
                "regime_states. Strategies and Treasury read regime_current. "
                "Invoked via regime_cli tick; disabled on VPS until a "
                "universe config is seeded in Phase 32."
            ),
            enabled_on_vps=False,
            tags={"phase": "27"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="events-calendar",
            kind="worker",
            module="shared.cli.events_cli",
            description=(
                "Events Calendar (Phase 30). Manages scheduled events "
                "(macro releases, earnings, maintenance windows, funding "
                "rolls, halvings) and derives active trading windows. "
                "Guardrails and Treasury consume events_active via "
                "EventsCalendarService.any_active(). Disabled on VPS "
                "until an event loader is wired in Phase 32."
            ),
            enabled_on_vps=False,
            tags={"phase": "30"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="altdata-ingestor",
            kind="worker",
            module="shared.cli.altdata_cli",
            description=(
                "Alt-Data Ingestor (Phase 29). Pulls funding rates, open "
                "interest, social / on-chain / macro metrics from pluggable "
                "sources and persists them to alt_data_items (latest view: "
                "alt_data_latest). Disabled on VPS until sources are "
                "configured in Phase 32."
            ),
            enabled_on_vps=False,
            tags={"phase": "29"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="crash-protection",
            kind="worker",
            module="shared.cli.guardrails_cli",
            description=(
                "Crash Protection (Phase 28). Evaluates regime, equity, "
                "position, daily-loss, and data-staleness rules and writes "
                "events to crash_protection_events. Treasury/Execution read "
                "crash_protection_active to gate new orders. Disabled on VPS "
                "until rules are seeded in Phase 32."
            ),
            enabled_on_vps=False,
            tags={"phase": "28"},
        )
    )
    SERVICE_REGISTRY.register(
        ServiceDescriptor(
            name="souls",
            kind="worker",
            module="shared.cli.souls_cli",
            description=(
                "Soul agents (Phases 31-32). Deterministic decision "
                "agents that persist every verdict to agent_decisions. "
                "Apex aggregates guardrails/events/regime/treasury into "
                "approve/reject/defer, Quant proposes trade hypotheses, "
                "Ledger journals fills/positions, Scout proposes new "
                "symbols, Curiosity picks experiments, Optimiser tunes "
                "parameters, RegimeWatcher alerts on regime transitions. "
                "Disabled on VPS until Phase 34 wires composer + router."
            ),
            enabled_on_vps=False,
            tags={"phase": "31-32"},
        )
    )


def register_builtin_services() -> None:
    """Idempotent registration of the built-in services."""
    if len(SERVICE_REGISTRY) == 0:
        _seed_known_services()
