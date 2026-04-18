"""Centralised UTC time helper.

Every module that needs the current UTC time should import from here
so we have a single place to swap in timezone-aware datetimes later.
"""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)
