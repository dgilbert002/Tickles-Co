"""
Module: _validate_env
Purpose: Validate required environment keys for the active LLM gateway provider.
Location: /opt/tickles/shared/utils/_validate_env.py
"""

import logging
import os
import sys
from typing import List, Optional

logger = logging.getLogger(__name__)


class EnvValidationError(Exception):
    """Raised when a required environment variable is missing or invalid."""
    pass


# ---------------------------------------------------------------------------
# Provider-specific required keys
# ---------------------------------------------------------------------------
_REQUIRED_BY_PROVIDER: dict[str, List[str]] = {
    "openrouter": [
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
    ],
    "requesty": [
        "REQUESTY_API",  # canonical live name
        "REQUESTY_BASE_URL",
    ],
}

# Keys that must exist for ANY provider (shared infrastructure)
_ALWAYS_REQUIRED: List[str] = [
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASSWORD",
    "DB_NAME_SHARED",
]

# Optional but warned-if-missing (soft checks)
_RECOMMENDED: List[str] = [
    "LLM_GATEWAY_DEFAULT",
    "LLM_COST_LOG_ENABLED",
]


def _resolve_gateway() -> str:
    """Return the active gateway provider name (lowercased)."""
    return (
        os.environ.get("LLM_GATEWAY_DEFAULT", "openrouter")
        .lower()
        .strip()
    )


def validate_env(
    *,
    provider: Optional[str] = None,
    raise_on_error: bool = True,
    log_level: int = logging.WARNING,
) -> List[str]:
    """Validate that all required environment keys are present.

    Args:
        provider: Override the provider to validate against. If None,
            reads LLM_GATEWAY_DEFAULT from the environment.
        raise_on_error: If True, raises EnvValidationError on first missing key.
            If False, returns a list of missing keys.
        log_level: Logging level for warnings about recommended-but-missing keys.

    Returns:
        List of missing required keys (empty if all present).

    Raises:
        EnvValidationError: If raise_on_error is True and any required key is missing.
    """
    gateway = (provider or _resolve_gateway()).lower().strip()

    missing: List[str] = []

    # Always-required keys
    for key in _ALWAYS_REQUIRED:
        if not os.environ.get(key):
            missing.append(key)

    # Provider-specific keys
    provider_keys = _REQUIRED_BY_PROVIDER.get(gateway, [])
    for key in provider_keys:
        if not os.environ.get(key):
            missing.append(key)

    # Special alias handling: REQUESTY_API_KEY is an alias for REQUESTY_API
    if gateway == "requesty" and "REQUESTY_API" in missing:
        if os.environ.get("REQUESTY_API_KEY") or os.environ.get("TICKLES_APP_VISION_API_KEY"):
            missing.remove("REQUESTY_API")

    # Soft checks: recommended keys
    for key in _RECOMMENDED:
        if not os.environ.get(key):
            logger.log(log_level, "Recommended env key %s is not set (using default)", key)

    if missing and raise_on_error:
        raise EnvValidationError(
            f"Missing required environment variables for provider '{gateway}': {missing}"
        )

    return missing


def validate_env_for_provider(provider: str) -> None:
    """Convenience wrapper: validate for a specific provider, always raises.

    Args:
        provider: Provider name ('openrouter' or 'requesty').

    Raises:
        EnvValidationError: If any required key is missing.
    """
    validate_env(provider=provider, raise_on_error=True)


def main() -> int:
    """CLI entrypoint for CI / pre-flight checks.

    Usage:
        python -m shared.utils._validate_env

    Exits:
        0 if all required keys are present.
        1 if any required key is missing.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        missing = validate_env(raise_on_error=False)
        if missing:
            logger.error("Missing required env keys: %s", missing)
            return 1
        logger.info("Environment validation passed for provider '%s'", _resolve_gateway())
        return 0
    except Exception as exc:
        logger.error("Validation failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
