"""
shared.cli — operator CLIs for The Platform.

One package, one CLI per service family. Every CLI follows the same pattern:

    python -m shared.cli.<name> [subcommand] [flags]

Phase 13 (foundations cleanup) ships the scaffolding — `status` subcommands
work end-to-end, action subcommands print a clearly labelled "lands in Phase N"
stub and exit 2 (EX_USAGE). Later phases fill in the bodies without changing
flag surfaces, so any automation wired today keeps working.

Convention:
    exit 0  = success
    exit 1  = failure (backend unreachable, invalid state, etc.)
    exit 2  = command recognised but not yet implemented in this phase
"""

__all__ = [
    "gateway_cli",
    "validator_cli",
    "forward_test_cli",
    "assets_cli",
    "sufficiency_cli",
    "candles_cli",
    "indicators_cli",
    "engines_cli",
    "features_cli",
    "auditor_cli",
    "services_cli",
    "enrichment_cli",
    "services_catalog_cli",
    "treasury_cli",
    "execution_cli",
    "regime_cli",
    "guardrails_cli",
    "altdata_cli",
    "events_cli",
]
