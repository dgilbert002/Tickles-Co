"""Backward-compat shim — real implementation at shared.candles.cli._common.

Several candles CLI modules import shared helpers via shared.cli._common.
This shim aliases the real module via sys.modules so the full namespace
(including underscore-prefixed helpers) is visible.
"""
from __future__ import annotations

import sys as _sys
from shared.candles.cli import _common as _real

_sys.modules[__name__] = _real
