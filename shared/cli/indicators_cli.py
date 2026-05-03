"""Backward-compat shim — real implementation at shared.candles.cli.indicators_cli.

Tests historically import this module as shared.cli.indicators_cli (both via
from shared.cli import indicators_cli and python -m shared.cli.indicators_cli),
while the real implementation lives under shared.candles.cli. This shim
swaps itself out in sys.modules for the real module so the full namespace
(including underscore helpers) is visible to test introspection.
"""
from __future__ import annotations

import sys as _sys
from shared.candles.cli import indicators_cli as _real

# Replace ourselves in sys.modules with the real module. After this line,
# any subsequent import shared.cli.indicators_cli returns the candles module.
_sys.modules[__name__] = _real

if __name__ == "__main__":
    import runpy
    runpy.run_module("shared.candles.cli.indicators_cli", run_name="__main__")
