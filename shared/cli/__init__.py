"""Backward-compat alias package for ``shared.candles.cli``.

The real CLI modules live under ``shared.candles.cli`` (historically nested
under the candles tree). A swathe of test files import them as
``shared.cli.X`` — the new flat name.

Each public CLI submodule has a thin shim file in this directory
(``altdata_cli.py``, ``backtest_cli.py``, ...) that re-exports from the
real module and, when run as ``python -m shared.cli.X``, hands off to the
real module's ``__main__`` entry point via ``runpy``.

Generation: when a new module is added to ``shared/candles/cli/``, also
add a one-line shim under ``shared/cli/`` (or regenerate them all via the
loop in ``shared/scripts/`` — left as an exercise for whoever next adds a
CLI).
"""
from __future__ import annotations

# No eager imports here. The shim files in this directory are real .py
# modules on disk, which makes both ``from shared.cli import X`` and
# ``python -m shared.cli.X`` work uniformly through Python's normal
# import machinery. Pre-populating sys.modules in __init__ would break
# runpy's loader-name check on subprocess invocations.
