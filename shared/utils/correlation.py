"""
Module: correlation
Purpose: Generate lightweight correlation IDs for tracing requests across services.
Location: /opt/tickles/shared/utils/correlation.py
"""

import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


def new_correlation_id(prefix: Optional[str] = None) -> str:
    """Generate a short, unique correlation ID for tracing a request chain.

    Every top-level operation (signal interpretation, post-mortem, chart analysis)
    generates one ID and threads it through every downstream call so that
    api_cost_log rows, signal_interpretations, and tracked_positions can be
    joined back to the originating operation.

    Args:
        prefix: Optional prefix (e.g. 'sig', 'pm', 'ch') for human readability.

    Returns:
        12-char hex string, optionally prefixed, e.g. 'sig-a1b2c3d4e5f6'.
    """
    cid = uuid.uuid4().hex[:12]
    if prefix:
        return f"{prefix}-{cid}"
    return cid
