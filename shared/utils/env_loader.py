"""
Module: env_loader
Purpose: Resolve per-service LLM gateway settings from environment variables.
Location: /opt/tickles/shared/utils/env_loader.py

Design:
  * Reads LLM_GATEWAY_<SERVICE> → falls back to LLM_GATEWAY_DEFAULT → 'openrouter'.
  * Reads per-service model overrides (CHART_HACKER_MODEL_<SERVICE>_PRIMARY).
  * Reads per-service temperature overrides (LLM_TEMPERATURE_<SERVICE>).
  * Returns a plain dict so GatewayConfig can consume it without circular imports.
"""

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def resolve_gateway_provider(service_name: str) -> str:
    """Resolve the active gateway provider for a named service.

    Resolution order:
      1. LLM_GATEWAY_<SERVICE> (e.g. LLM_GATEWAY_INTERPRETATION)
      2. LLM_GATEWAY_DEFAULT
      3. "openrouter"

    Args:
        service_name: Logical service name (e.g. "interpretation", "mcp", "guru").

    Returns:
        Lowercase provider name: "openrouter" or "requesty".
    """
    return (
        os.environ.get(f"LLM_GATEWAY_{service_name.upper()}")
        or os.environ.get("LLM_GATEWAY_DEFAULT", "openrouter")
    ).lower().strip()


def resolve_model_pair(service_name: str) -> Dict[str, str]:
    """Resolve primary + fallback model for a service.

    Resolution order for primary:
      1. CHART_HACKER_MODEL_<SERVICE>_PRIMARY
      2. CHART_HACKER_MODEL_PRIMARY
      3. TICKLES_APP_VISION_API_MODEL (for interpretation only)
      4. Default: "anthropic/claude-sonnet-4"

    Resolution order for fallback:
      1. CHART_HACKER_MODEL_<SERVICE>_FALLBACK
      2. CHART_HACKER_MODEL_FALLBACK
      3. TICKLES_APP_PRE_VISION_API_MODEL (for interpretation only)
      4. Default: "google/gemini-2.0-flash-001"

    Args:
        service_name: Logical service name.

    Returns:
        Dict with keys 'primary' and 'fallback'.
    """
    svc = service_name.upper()

    primary = os.environ.get(f"CHART_HACKER_MODEL_{svc}_PRIMARY")
    if primary is None:
        primary = os.environ.get("CHART_HACKER_MODEL_PRIMARY")
    if primary is None and service_name.lower() == "interpretation":
        primary = os.environ.get("TICKLES_APP_VISION_API_MODEL")
    if primary is None:
        primary = "anthropic/claude-sonnet-4"

    fallback = os.environ.get(f"CHART_HACKER_MODEL_{svc}_FALLBACK")
    if fallback is None:
        fallback = os.environ.get("CHART_HACKER_MODEL_FALLBACK")
    if fallback is None and service_name.lower() == "interpretation":
        fallback = os.environ.get("TICKLES_APP_PRE_VISION_API_MODEL")
    if fallback is None:
        fallback = "google/gemini-2.0-flash-001"

    return {"primary": primary, "fallback": fallback}


def resolve_temperature(service_name: str, default: float = 0.7) -> float:
    """Resolve temperature for a service.

    Args:
        service_name: Logical service name.
        default: Fallback temperature if env key is missing or invalid.

    Returns:
        Float temperature in [0.0, 2.0].
    """
    temp_str = os.environ.get(f"LLM_TEMPERATURE_{service_name.upper()}")
    if temp_str is None:
        return default
    try:
        temp = float(temp_str)
        if not 0.0 <= temp <= 2.0:
            logger.warning(
                "Temperature %.2f for %s out of range [0,2]; clamping to %.2f",
                temp, service_name, default,
            )
            return default
        return temp
    except ValueError:
        logger.warning(
            "Invalid temperature '%s' for %s; using default %.2f",
            temp_str, service_name, default,
        )
        return default


def resolve_api_credentials(provider: str) -> Dict[str, str]:
    """Resolve API key and base URL for a provider.

    Args:
        provider: "openrouter" or "requesty".

    Returns:
        Dict with keys 'api_key' and 'base_url'.
    """
    if provider == "requesty":
        api_key = (
            os.environ.get("REQUESTY_API_KEY")
            or os.environ.get("REQUESTY_API")
            or os.environ.get("TICKLES_APP_VISION_API_KEY", "")
        )
        base_url = (
            os.environ.get("REQUESTY_BASE_URL")
            or os.environ.get("TICKLES_APP_REQUESTY_URL")
            or "https://router.requesty.ai/v1"
        )
    else:
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        base_url = os.environ.get(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        )

    return {"api_key": api_key, "base_url": base_url}


def load_service_env(service_name: str) -> Dict[str, str]:
    """Load all resolved environment settings for a service.

    Args:
        service_name: Logical service name.

    Returns:
        Dict with keys: provider, api_key, base_url, primary_model,
        fallback_model, temperature.
    """
    provider = resolve_gateway_provider(service_name)
    creds = resolve_api_credentials(provider)
    models = resolve_model_pair(service_name)
    temperature = resolve_temperature(service_name)

    return {
        "provider": provider,
        "api_key": creds["api_key"],
        "base_url": creds["base_url"],
        "primary_model": models["primary"],
        "fallback_model": models["fallback"],
        "temperature": str(temperature),
    }
