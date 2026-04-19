"""Tickles-Co Rule-1 Continuous Auditor (Phase 21).

Rule 1 of the trading house: **backtests must equal live.**

This module monitors that invariant continuously. Its jobs today:

  1. Run the Phase 19 parity harness on a schedule and persist
     per-engine divergence records to an audit store.
  2. Provide hooks (``record_live_trade_divergence``) that Phase 26's
     execution layer will call on every fill so every live trade gets
     compared against its replayed-backtest counterpart.
  3. Expose a query API + CLI so Dean can see at a glance whether
     Rule 1 is currently respected, and inspect the most recent
     breaches.

Storage is deliberately lightweight (SQLite — one file at
``/opt/tickles/var/audit/rule1.sqlite3``) so that we don't have to
migrate ``tickles_shared``, and so Dean can ``scp`` the file locally
for forensic analysis.

Public surface::

    from shared.auditor import (
        AuditStore, AuditRecord, AuditEventType, AuditSeverity,
        ContinuousAuditor, ParityComparator, LiveVsBacktestComparator,
    )
"""

from shared.auditor.schema import (  # noqa: F401
    AuditEventType,
    AuditRecord,
    AuditSeverity,
    DivergenceSummary,
)
from shared.auditor.storage import AuditStore  # noqa: F401
from shared.auditor.comparator import (  # noqa: F401
    ParityComparator,
    LiveVsBacktestComparator,
    FeeSlippageComparator,
)
from shared.auditor.auditor import ContinuousAuditor  # noqa: F401
