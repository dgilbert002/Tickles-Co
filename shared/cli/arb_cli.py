"""Backward-compat shim — real implementation at shared.candles.cli.arb_cli.

Tests historically import this module as shared.cli.arb_cli (both via
from shared.cli import arb_cli and python -m shared.cli.arb_cli),
while the real implementation lives under shared.candles.cli. This shim
swaps itself out in sys.modules for the real module so the full namespace
(including underscore helpers) is visible to test introspection.
"""
from __future__ import annotations

import sys as _sys
from shared.candles.cli import arb_cli as _real

# Replace ourselves in sys.modules with the real module. After this line,
# any subsequent import shared.cli.arb_cli returns the candles module.
_sys.modules[__name__] = _real

if __name__ == "__main__":
    import runpy
    runpy.run_module("shared.candles.cli.arb_cli", run_name="__main__")
