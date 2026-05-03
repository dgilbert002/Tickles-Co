"""
Module: freshness
Purpose: Data freshness validation for algorithmic trading.
Location: /opt/tickles/shared/utils/freshness.py
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

logger = logging.getLogger(__name__)

class StaleDataError(Exception):
    """Raised when data is older than the allowed threshold."""
    def __init__(self, message: str, lag_seconds: float, threshold_seconds: float):
        super().__init__(message)
        self.lag_seconds = lag_seconds
        self.threshold_seconds = threshold_seconds

def validate_freshness(
    ts: Union[datetime, str, int, float, None],
    threshold_seconds: float = 180.0,
    context: str = "data"
) -> float:
    """
    Validates that a timestamp is within the allowed threshold from now (UTC).

    Args:
        ts: The timestamp to check (datetime, ISO string, or unix timestamp).
        threshold_seconds: Maximum allowed age in seconds. Default 180s (3m).
        context: Description of the data being checked for error messages.

    Returns:
        The calculated lag in seconds.

    Raises:
        StaleDataError: If the data is older than the threshold.
        ValueError: If the timestamp format is invalid.
    """
    if ts is None:
        raise ValueError(f"Cannot validate freshness of null timestamp for {context}")

    now = datetime.now(timezone.utc)
    
    if isinstance(ts, str):
        try:
            # Handle Z suffix for UTC
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"Invalid ISO timestamp format: {ts}")
    elif isinstance(ts, (int, float)):
        # Assume unix timestamp
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    elif isinstance(ts, datetime):
        dt = ts
    else:
        raise ValueError(f"Unsupported timestamp type: {type(ts)}")

    # Ensure dt is timezone-aware UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    lag = (now - dt).total_seconds()

    if lag > threshold_seconds:
        msg = f"STALE_DATA: {context} is {lag:.1f}s old (threshold: {threshold_seconds}s)"
        logger.warning(msg)
        raise StaleDataError(msg, lag, threshold_seconds)

    if lag < -60: # Allow for some clock skew
        logger.warning("Future timestamp detected for %s: %s (lag: %s)", context, dt, lag)

    return lag

def freshness_envelope(
    data: Dict[str, Any],
    ts_field: str = "timestamp",
    threshold_seconds: float = 180.0,
    context: str = "data"
) -> Dict[str, Any]:
    """
    Wraps a data dictionary with freshness metadata or returns an error envelope.
    Supports nested keys via dot notation (e.g., 'ticker.timestamp').
    
    Args:
        data: The data dictionary to check.
        ts_field: The field containing the timestamp.
        threshold_seconds: Max allowed age.
        context: Context for error messages.
        
    Returns:
        The original data with 'freshness' metadata added, or an error dict.
    """
    try:
        # Support nested key resolution
        ts = data
        for key in ts_field.split("."):
            if isinstance(ts, dict):
                ts = ts.get(key)
            else:
                ts = None
                break

        lag = validate_freshness(ts, threshold_seconds, context)
        data["freshness"] = {
            "status": "fresh",
            "lag_seconds": round(lag, 2),
            "threshold_seconds": threshold_seconds,
            "checked_at": datetime.now(timezone.utc).isoformat()
        }
        return data
    except StaleDataError as e:
        return {
            "status": "error",
            "error_code": "STALE_DATA",
            "message": str(e),
            "lag_seconds": round(e.lag_seconds, 2),
            "threshold_seconds": threshold_seconds
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Freshness check failed: {e}"
        }
