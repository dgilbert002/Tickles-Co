"""Package marker for the Tickles cost shipper.

We keep the two public entrypoints (``shipper``, ``reconciler``) importable
from the package root for convenience, but they can also be run directly as
scripts (``python shipper.py ...``) so the module has zero required deps.
"""

from .pricing import estimate_cost_cents  # noqa: F401

__all__ = ["estimate_cost_cents"]
