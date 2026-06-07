"""
Module: gateway_config
Purpose: Unified LLM gateway abstraction supporting OpenRouter and Requesty.
Location: /opt/tickles/shared/intelligence/gateway_config.py

Usage:
    from shared.intelligence.gateway_config import GatewayConfig, call_vision_llm, chat_completion

    cfg = GatewayConfig.for_service("interpretation")
    result = await call_vision_llm(
        cfg, model, system_prompt, user_text, image_b64, image_mime,
        correlation_id="sig-a1b2c3d4e5f6", operation="vision_primary"
    )
    text_result = await chat_completion(
        cfg, model, system_prompt, user_text,
        correlation_id="pm-a1b2c3d4e5f6", operation="text_completion"
    )

Environment variables:
    LLM_GATEWAY_DEFAULT          — "openrouter" or "requesty" (default: openrouter)
    LLM_GATEWAY_INTERPRETATION   — per-service override
    LLM_GATEWAY_MCP
    LLM_GATEWAY_GURU
    OPENROUTER_API_KEY
    OPENROUTER_BASE_URL
    REQUESTY_API_KEY             — also falls back to REQUESTY_API
    REQUESTY_BASE_URL            — also falls back to TICKLES_APP_REQUESTY_URL
    API_COST_LOG_ENABLED         — "true" | "false" (default: true)
    LLM_LOOP_DETECTOR_ENABLED    — "true" | "false" (default: true)
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (env-driven, no hardcodes)
# ---------------------------------------------------------------------------
_COST_LOG_ENABLED = os.environ.get("API_COST_LOG_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
_LOOP_DETECTOR_ENABLED = os.environ.get("LLM_LOOP_DETECTOR_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)


@dataclass(frozen=True)
class GatewayConfig:
    """Immutable gateway configuration for a specific service."""

    gateway: str          # "openrouter" or "requesty"
    api_key: str
    base_url: str
    service_name: str
    default_model: str
    fallback_model: str
    max_tokens: int = 2048
    timeout_s: int = 60
    temperature: float = 0.7
    cost_log_enabled: bool = True

    @classmethod
    def for_service(cls, service_name: str) -> "GatewayConfig":
        """Build a GatewayConfig for a named service from environment variables.

        Resolution order:
          1. LLM_GATEWAY_<SERVICE> (e.g. LLM_GATEWAY_INTERPRETATION)
          2. LLM_GATEWAY_DEFAULT
          3. "openrouter"

        Args:
            service_name: Logical service name (e.g. "interpretation", "mcp", "guru")

        Returns:
            GatewayConfig instance ready for use.
        """
        gateway = (
            os.environ.get(f"LLM_GATEWAY_{service_name.upper()}")
            or os.environ.get("LLM_GATEWAY_DEFAULT", "openrouter")
        ).lower().strip()

        if gateway == "requesty":
            # Phase 1 fix: REQUESTY_API is the live env var name; REQUESTY_API_KEY is the plan alias.
            api_key = os.environ.get(
                "REQUESTY_API_KEY",
                os.environ.get(
                    "REQUESTY_API",
                    os.environ.get("TICKLES_APP_VISION_API_KEY", "")
                )
            )
            # Phase 1 fix: default URL was api.requesty.ai (wrong) → router.requesty.ai (correct)
            base_url = os.environ.get(
                "REQUESTY_BASE_URL",
                os.environ.get(
                    "TICKLES_APP_REQUESTY_URL",
                    "https://router.requesty.ai/v1"
                )
            )
        else:
            gateway = "openrouter"
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
            base_url = os.environ.get(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            )

        # Model selection per service
        default_model = os.environ.get(
            f"CHART_HACKER_MODEL_{service_name.upper()}_PRIMARY",
            os.environ.get(
                "CHART_HACKER_MODEL_PRIMARY",
                os.environ.get("TICKLES_APP_VISION_API_MODEL", "anthropic/claude-sonnet-4")
                if service_name.lower() == "interpretation"
                else os.environ.get("CHART_HACKER_MODEL_PRIMARY", "anthropic/claude-sonnet-4"),
            ),
        )
        fallback_model = os.environ.get(
            f"CHART_HACKER_MODEL_{service_name.upper()}_FALLBACK",
            os.environ.get(
                "CHART_HACKER_MODEL_FALLBACK",
                os.environ.get("TICKLES_APP_PRE_VISION_API_MODEL", "google/gemini-2.5-flash")
                if service_name.lower() == "interpretation"
                else os.environ.get("CHART_HACKER_MODEL_FALLBACK", "google/gemini-2.0-flash-001"),
            ),
        )

        # Temperature per service (optional override)
        temp_str = os.environ.get(f"LLM_TEMPERATURE_{service_name.upper()}")
        temperature = 0.7
        if temp_str is not None:
            try:
                temperature = float(temp_str)
            except ValueError:
                logger.warning("Invalid LLM_TEMPERATURE_%s: %s", service_name.upper(), temp_str)

        # HEAL-2026-05-29 — max_tokens per service (env-overridable).
        # The previous hard-pinned 2048 default truncated the interpretation
        # vision response on complex dual-track charts, producing invalid JSON
        # that the parser dropped (charts lost entirely). The vision schema
        # (two trade arrays + chart_analysis + reasoning) needs more headroom,
        # so interpretation now defaults to 8192 (operator wants COMPREHENSIVE
        # reasoning for post-mortems/learning, not brevity); other services keep
        # 2048. Override via LLM_MAX_TOKENS_<SERVICE> or LLM_MAX_TOKENS_DEFAULT.
        _mt_default = 8192 if service_name.lower() == "interpretation" else 2048
        _mt_str = (
            os.environ.get(f"LLM_MAX_TOKENS_{service_name.upper()}")
            or os.environ.get("LLM_MAX_TOKENS_DEFAULT")
        )
        max_tokens = _mt_default
        if _mt_str:
            try:
                max_tokens = int(_mt_str)
            except ValueError:
                logger.warning("Invalid LLM_MAX_TOKENS_%s: %s", service_name.upper(), _mt_str)

        return cls(
            gateway=gateway,
            api_key=api_key,
            base_url=base_url,
            service_name=service_name,
            default_model=default_model,
            fallback_model=fallback_model,
            max_tokens=max_tokens,
            temperature=temperature,
            cost_log_enabled=_COST_LOG_ENABLED,
        )


    @classmethod
    def for_provider(cls, provider: str, service_name: str) -> "GatewayConfig":
        """Build a GatewayConfig for an EXPLICIT provider, bypassing the
        LLM_GATEWAY_* env resolution.

        Round 14 (2026-05-29): the model picker stores a per-slot provider in
        the DB. When a service resolves its slot, it knows the provider already
        and must NOT let the global env override it — otherwise choosing
        "Requesty" for one slot and "OpenRouter" for another could not work.
        Model/temperature/max_tokens still resolve per-service as before; the
        caller passes the slot's model explicitly to call_vision_llm/
        chat_completion, so ``default_model`` here is only a safety net.

        Args:
            provider: "openrouter" or "requesty".
            service_name: Logical service name (drives cost-log role + per-service
                model/temperature/max_tokens env overrides).
        """
        provider = (provider or "openrouter").strip().lower()
        base = cls.for_service(service_name)
        if provider == "requesty":
            api_key = os.environ.get(
                "REQUESTY_API_KEY",
                os.environ.get(
                    "REQUESTY_API",
                    os.environ.get("TICKLES_APP_VISION_API_KEY", ""),
                ),
            )
            # Treat present-but-empty as unset (the .env gotcha we just fixed).
            if not (api_key or "").strip():
                api_key = (
                    os.environ.get("REQUESTY_API", "")
                    or os.environ.get("TICKLES_APP_VISION_API_KEY", "")
                )
            base_url = os.environ.get(
                "REQUESTY_BASE_URL",
                os.environ.get("TICKLES_APP_REQUESTY_URL", "https://router.requesty.ai/v1"),
            )
        else:
            provider = "openrouter"
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
            base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")

        # Rebuild the frozen dataclass with the explicit provider/key/url,
        # preserving the per-service model/temp/max_tokens already resolved.
        from dataclasses import replace
        return replace(base, gateway=provider, api_key=api_key, base_url=base_url)


def get_gateway_for_service(service_name: str) -> "GatewayConfig":
    """Alias for GatewayConfig.for_service() — returns a configured gateway.

    Args:
        service_name: Logical service name (e.g. "interpretation", "mcp", "guru")

    Returns:
        GatewayConfig instance ready for use.
    """
    return GatewayConfig.for_service(service_name)


async def resolve_slot_gateway(slot: str) -> "tuple[GatewayConfig, str]":
    """Round 14: resolve a model-picker slot → (GatewayConfig, model_string).

    The slot's provider (DB → env → default) decides the endpoint/key; the
    slot's model is returned for the caller to pass to call_vision_llm /
    chat_completion. This is the single entry point services should use so the
    dashboard picker fully controls provider + model.

    Falls back gracefully to the legacy per-service gateway if model_config is
    unavailable for any reason.
    """
    try:
        from shared.intelligence.model_config import get_slot
        s = await get_slot(slot)
        cfg = GatewayConfig.for_provider(s["provider"], s["service"])
        return cfg, s["model"]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("resolve_slot_gateway(%s) failed, using legacy: %s", slot, exc)
        cfg = GatewayConfig.for_service(slot)
        return cfg, cfg.default_model


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _build_headers(cfg: GatewayConfig) -> Dict[str, str]:
    """Build HTTP headers for the configured gateway."""
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    if cfg.gateway == "openrouter":
        headers["HTTP-Referer"] = "https://tickles.co"
        headers["X-Title"] = "TicklesChartHacker"
    return headers


async def _log_call(
    *,
    cfg: GatewayConfig,
    model: str,
    correlation_id: str,
    operation: str,
    start_ts: float,
    data: Dict[str, Any],
    resp_status: int,
    success: bool,
    company_id: str = "",
    agent_id: str = "",
) -> None:
    """Write a cost-log row after a gateway call completes.

    This is a best-effort fire-and-forget log; failures are warned but not raised.
    """
    if not cfg.cost_log_enabled:
        return

    try:
        from shared.utils.api_cost_log import log_api_call

        latency_ms = int((time.time() - start_ts) * 1000)
        usage = data.get("usage", {})
        await log_api_call(
            provider=cfg.gateway,
            model=data.get("model", model),
            role=cfg.service_name,
            context=f"{operation}:{model}"[:100],
            usage=usage,
            latency_ms=latency_ms,
            company_id=company_id,
            operation=operation,
            agent_id=agent_id or cfg.service_name,
            temperature=cfg.temperature,
            correlation_id=correlation_id,
            request_path="/chat/completions",
            response_path="",
            success=success,
            http_status=resp_status,
        )
    except Exception as exc:
        logger.warning("Cost-log write failed for %s/%s: %s", cfg.service_name, operation, exc)


async def _check_loop(
    cfg: GatewayConfig,
    model: str,
    system_prompt: str,
    user_text: str,
    correlation_id: str,
    operation: str,
) -> None:
    """Check for repeated identical/similar calls via the loop detector.

    Raises:
        RuntimeError if a loop or burst is detected.
    """
    if not _LOOP_DETECTOR_ENABLED:
        return

    try:
        from shared.intelligence.loop_detector import CallFingerprint, LoopDetector

        fp = CallFingerprint(
            role=cfg.service_name,
            model=model,
            prompt_hash=LoopDetector.hash_prompt(system_prompt + user_text),
            image_hash=None,
            company_id="",
        )
        detector = LoopDetector()
        is_anomaly, reason = detector.record(fp)
        if is_anomaly:
            raise RuntimeError(
                f"LLM loop/burst detected for {cfg.service_name}/{operation}: {reason}"
            )
    except RuntimeError:
        raise
    except Exception as exc:
        logger.warning("Loop-detector check failed for %s/%s: %s", cfg.service_name, operation, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def call_vision_llm(
    cfg: GatewayConfig,
    model: str,
    system_prompt: str,
    user_text: str,
    image_b64: str,
    image_mime: str,
    max_tokens: Optional[int] = None,
    correlation_id: str = "",
    operation: str = "vision_call",
    company_id: str = "",
    agent_id: str = "",
) -> Dict[str, Any]:
    """Call a vision-capable LLM through the configured gateway.

    Phase 1 additions:
      * correlation_id — threads through the operation chain for tracing.
      * operation — sub-operation name within the chain (e.g. 'vision_primary').
      * Automatic cost-log write via api_cost_log.
      * Loop-detector gate before the HTTP call.
      * Temperature injected into payload.

    Args:
        cfg: GatewayConfig from GatewayConfig.for_service()
        model: OpenRouter/Requesty model identifier
        system_prompt: System prompt text
        user_text: User prompt text
        image_b64: Base64-encoded image bytes
        image_mime: MIME type of the image
        max_tokens: Override default max_tokens
        correlation_id: 12-char hex ID for tracing this call through the pipeline.
        operation: Sub-operation name for cost-log granularity.
        company_id: Company short-name for cost attribution.
        agent_id: Agent or service instance that initiated the call.

    Returns:
        Dict with keys: content (str), model (str), usage (dict)

    Raises:
        RuntimeError on HTTP error, timeout, missing API key, or loop detection.
    """
    import aiohttp

    if not cfg.api_key:
        raise RuntimeError(f"{cfg.gateway.upper()} API key not set for service {cfg.service_name}")

    # Phase 1: loop-detector gate
    await _check_loop(cfg, model, system_prompt, user_text, correlation_id, operation)

    # Phase BP: budget circuit-breaker
    from shared.utils.api_cost_log import check_budget, BudgetExceededError
    try:
        await check_budget(cfg.service_name, company_id, 0.01)
    except BudgetExceededError as exc:
        raise RuntimeError(f"Budget exceeded: {exc}") from exc

    headers = _build_headers(cfg)

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{image_mime};base64,{image_b64}"
                        },
                    },
                ],
            },
        ],
        "max_tokens": max_tokens or cfg.max_tokens,
    }
    # Phase 1: inject temperature if non-default
    if cfg.temperature != 0.7:
        payload["temperature"] = cfg.temperature

    url = f"{cfg.base_url}/chat/completions"
    timeout = aiohttp.ClientTimeout(total=cfg.timeout_s)

    start_ts = time.time()
    resp_status = 0
    success = False
    data: Dict[str, Any] = {}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                resp_status = resp.status
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(
                        f"{cfg.gateway.upper()} HTTP {resp.status}: {text[:500]}"
                    )
                data = await resp.json()
                success = True
    finally:
        # Phase 1: best-effort cost log (fire-and-forget)
        await _log_call(
            cfg=cfg,
            model=model,
            correlation_id=correlation_id,
            operation=operation,
            start_ts=start_ts,
            data=data,
            resp_status=resp_status,
            success=success,
            company_id=company_id,
            agent_id=agent_id,
        )

        choices = data.get("choices", [])
    if not choices:
        logger.warning("LLM returned empty choices array (model=%s, service=%s) — response may be blocked/filtered", model, cfg.service_name)
    choice = (choices[0] if choices else {})
    content = choice.get("message", {}).get("content", "")
    finish_reason = choice.get("finish_reason", "stop")
    if finish_reason != "stop":
        logger.warning(
            "LLM finish_reason=%s (model=%s, service=%s, cid=%s) — "
            "response may be truncated. Consider increasing max_tokens.",
            finish_reason, model, cfg.service_name, correlation_id,
        )
    return {
        "content": content,
        "model": data.get("model", model),
        "usage": data.get("usage", {}),
    }


async def chat_completion(
    cfg: GatewayConfig,
    model: str,
    system_prompt: str,
    user_text: str,
    max_tokens: Optional[int] = None,
    correlation_id: str = "",
    operation: str = "chat_completion",
    company_id: str = "",
    agent_id: str = "",
) -> Dict[str, Any]:
    """Call a text-only LLM through the configured gateway.

    Phase 1 additions:
      * correlation_id — threads through the operation chain for tracing.
      * operation — sub-operation name within the chain.
      * Automatic cost-log write via api_cost_log.
      * Loop-detector gate before the HTTP call.
      * Temperature injected into payload.

    Args:
        cfg: GatewayConfig from GatewayConfig.for_service()
        model: OpenRouter/Requesty model identifier
        system_prompt: System prompt text
        user_text: User prompt text
        max_tokens: Override default max_tokens
        correlation_id: 12-char hex ID for tracing this call through the pipeline.
        operation: Sub-operation name for cost-log granularity.
        company_id: Company short-name for cost attribution.
        agent_id: Agent or service instance that initiated the call.

    Returns:
        Dict with keys: content (str), model (str), usage (dict)

    Raises:
        RuntimeError on HTTP error, timeout, missing API key, or loop detection.
    """
    import aiohttp

    if not cfg.api_key:
        raise RuntimeError(f"{cfg.gateway.upper()} API key not set for service {cfg.service_name}")

    # Phase 1: loop-detector gate
    await _check_loop(cfg, model, system_prompt, user_text, correlation_id, operation)

    # Phase BP: budget circuit-breaker
    from shared.utils.api_cost_log import check_budget, BudgetExceededError
    try:
        await check_budget(cfg.service_name, company_id, 0.01)
    except BudgetExceededError as exc:
        raise RuntimeError(f"Budget exceeded: {exc}") from exc

    headers = _build_headers(cfg)

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "max_tokens": max_tokens or cfg.max_tokens,
    }
    # Phase 1: inject temperature if non-default
    if cfg.temperature != 0.7:
        payload["temperature"] = cfg.temperature

    url = f"{cfg.base_url}/chat/completions"
    timeout = aiohttp.ClientTimeout(total=cfg.timeout_s)

    start_ts = time.time()
    resp_status = 0
    success = False
    data: Dict[str, Any] = {}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                resp_status = resp.status
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(
                        f"{cfg.gateway.upper()} HTTP {resp.status}: {text[:500]}"
                    )
                data = await resp.json()
                success = True
    finally:
        # Phase 1: best-effort cost log (fire-and-forget)
        await _log_call(
            cfg=cfg,
            model=model,
            correlation_id=correlation_id,
            operation=operation,
            start_ts=start_ts,
            data=data,
            resp_status=resp_status,
            success=success,
            company_id=company_id,
            agent_id=agent_id,
        )

        choices = data.get("choices", [])
    if not choices:
        logger.warning("LLM returned empty choices array (model=%s, service=%s) — response may be blocked/filtered", model, cfg.service_name)
    choice = (choices[0] if choices else {})
    content = choice.get("message", {}).get("content", "")
    finish_reason = choice.get("finish_reason", "stop")
    if finish_reason != "stop":
        logger.warning(
            "LLM finish_reason=%s (model=%s, service=%s, cid=%s) — "
            "response may be truncated. Consider increasing max_tokens.",
            finish_reason, model, cfg.service_name, correlation_id,
        )
    return {
        "content": content,
        "model": data.get("model", model),
        "usage": data.get("usage", {}),
    }
