"""
shared.services — Phase 22 generic long-running service runtime.

The 21-year-old version
-----------------------
Every long-lived Tickles process (market-data gateway, candle
collector, news collector, auditor, etc.) has the same shape:

  * Run forever.
  * Do a unit of work on a cadence.
  * Respect SIGINT / SIGTERM — stop cleanly.
  * Back off when something blows up, don't hot-loop on errors.
  * Emit a heartbeat so humans (and the auditor) can tell you're
    actually alive, not just "systemd says active".

Instead of copy-pasting that shape into every daemon, Phase 22
factors it out into :class:`ServiceDaemon` and wraps every
existing :class:`shared.collectors.base.BaseCollector` with a
thin adapter so the whole zoo of collectors gets systemd-friendly
supervisor semantics for free.

Public surface:

* :class:`ServiceDaemon` — generic supervisor loop.
* :class:`CollectorServiceAdapter` — wraps BaseCollector.
* :class:`ServiceRegistry` + built-in registrations.
* :data:`SERVICE_REGISTRY` — process-global registry instance.

Phase 22 is strictly **additive**. The existing systemd units
(``tickles-md-gateway``, ``tickles-candle-daemon``,
``tickles-discord-collector``, ``tickles-bt-workers``,
``tickles-catalog``, ``paperclip``) are not touched. This module
just gives future services (and retrofits) a common home.
"""

from shared.services.daemon import (
    DaemonConfig,
    DaemonStats,
    ServiceDaemon,
)
from shared.services.collector_service import (
    CollectorServiceAdapter,
    run_collector_once,
)
from shared.services.registry import (
    ServiceDescriptor,
    ServiceRegistry,
    SERVICE_REGISTRY,
    register_builtin_services,
)
from shared.services.catalog import (
    HeartbeatMark,
    MIGRATION_PATH as CATALOG_MIGRATION_PATH,
    ServicesCatalog,
    SystemdState,
    extract_heartbeats_from_audit,
    parse_systemctl_show,
    read_migration_sql,
)

__all__ = [
    "DaemonConfig",
    "DaemonStats",
    "ServiceDaemon",
    "CollectorServiceAdapter",
    "run_collector_once",
    "ServiceDescriptor",
    "ServiceRegistry",
    "SERVICE_REGISTRY",
    "register_builtin_services",
    "ServicesCatalog",
    "SystemdState",
    "HeartbeatMark",
    "CATALOG_MIGRATION_PATH",
    "parse_systemctl_show",
    "extract_heartbeats_from_audit",
    "read_migration_sql",
]

register_builtin_services()
