"""
Module: api_cost_log
Purpose: Universal writer for api_cost_log — every outbound API call writes one row.
Location: /opt/tickles/shared/utils/api_cost_log.py

Design:
  * All LLM gateway calls, exchange REST calls, Discord/Telegram API calls,
    and any other outbound HTTP request MUST call log_api_call() on completion.
  * Writes to tickles_shared.public.api_cost_log via the shared pool.
  * Respects the writer-domain registry (Phase 10 [BC]) — service_name must be
    registered for table 'api_cost_log'.
  * Correlation ID propagation: every caller passes the same correlation_id
    that was generated at the top of the operation chain.
  * Cost estimation fallback: if the provider does not return usage tokens,
    the helper falls back to a rough estimate based on payload size.
  * Idempotent: duplicate (correlation_id, operation) pairs are ignored via
    UNIQUE index so retries do not inflate the log.
"""

import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, Optional

import asyncpg

from shared.utils.db import get_shared_pool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (env-driven, no hardcodes)
# ---------------------------------------------------------------------------
_COST_LOG_ENABLED = os.environ.get("API_COST_LOG_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)

# Approximate per-1M-token pricing (input / output) for cost estimation fallback.
# Keys are model identifiers; values are (input_price, output_price) in USD.
_DEFAULT_PRICING: Dict[str, tuple[float, float]] = {
    "anthropic/claude-sonnet-4": (3.0, 15.0),
    "google/gemini-2.0-flash-001": (0.10, 0.40),
    "google/gemini-2.5-flash": (0.15, 0.60),
    "openai/gpt-4o": (5.0, 15.0),
    "openai/gpt-4o-mini": (0.15, 0.60),
}

# ---------------------------------------------------------------------------
# Budget circuit-breaker — pre-call gate (Phase BP)
# ---------------------------------------------------------------------------
_DEFAULT_BUDGETS: Dict[str, float] = {
    "vision": float(os.environ.get("LLM_BUDGET_VISION", "20.0")),
    "text": float(os.environ.get("LLM_BUDGET_TEXT", "10.0")),
    "embedding": float(os.environ.get("LLM_BUDGET_EMBEDDING", "2.0")),
    "default": float(os.environ.get("LLM_BUDGET_DEFAULT", "50.0")),
}
_GLOBAL_DAILY_BUDGET = float(os.environ.get("LLM_BUDGET_GLOBAL_DAILY", "100.0"))


class BudgetExceededError(Exception):
    """Raised when a role or global budget is exceeded for the day."""
    pass


async def check_budget(
    role: str = "default",
    company_id: str = "",
    estimated_cost_usd: float = 0.0,
) -> None:
    """Pre-call budget gate — raises BudgetExceededError if over limit.

    Checks role-specific and global daily budgets. Fails open on DB errors
    (never blocks a call due to a budget-check infrastructure failure).
    """
    budget_role = "default"
    for key in ("vision", "text", "embedding"):
        if key in role.lower():
            budget_role = key
            break
    role_limit = _DEFAULT_BUDGETS.get(budget_role, _DEFAULT_BUDGETS["default"])

    try:
        pool = await get_shared_pool()
        async with pool.acquire() as conn:
            role_spent = float(await conn.fetchval(
                "SELECT COALESCE(SUM(cost_usd),0) FROM api_cost_log "
                "WHERE created_at>=CURRENT_DATE AND role LIKE $1",
                f"%{budget_role}%") or 0)
            global_spent = float(await conn.fetchval(
                "SELECT COALESCE(SUM(cost_usd),0) FROM api_cost_log "
                "WHERE created_at>=CURRENT_DATE") or 0)
    except Exception as exc:
        logger.warning("check_budget query failed: %s — allowing call", exc)
        return

    if role_spent + estimated_cost_usd > role_limit:
        raise BudgetExceededError(
            f"{budget_role} budget: ${role_spent:.2f}+${estimated_cost_usd:.4f} > ${role_limit:.2f}")
    if global_spent + estimated_cost_usd > _GLOBAL_DAILY_BUDGET:
        raise BudgetExceededError(
            f"Global budget: ${global_spent:.2f}+${estimated_cost_usd:.4f} > ${_GLOBAL_DAILY_BUDGET:.2f}")

# SQL — additive columns from Phase 1 migration must exist before this runs.
_INSERT_SQL = """
INSERT INTO api_cost_log (
    provider, model, role, context,
    tokens_in, tokens_out, cost_usd, latency_ms,
    company_id, operation, agent_id, temperature,
    correlation_id, request_path, response_path,
    success, http_status, extra,
    created_at
) VALUES (
    $1, $2, $3, $4,
    $5, $6, $7, $8,
    $9, $10, $11, $12,
    $13, $14, $15,
    $16, $17, $18,
    NOW()
)
ON CONFLICT (correlation_id, operation) DO NOTHING
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> Decimal:
    """Rough cost estimator when the provider does not return usage tokens.

    Args:
        model: Model identifier (e.g. 'anthropic/claude-sonnet-4').
        input_tokens: Number of input / prompt tokens.
        output_tokens: Number of output / completion tokens.

    Returns:
        Estimated cost in USD as Decimal(20,8).
    """
    inp_p, out_p = _DEFAULT_PRICING.get(model, (3.0, 15.0))
    cost = (input_tokens * inp_p + output_tokens * out_p) / 1_000_000.0
    return Decimal(str(cost)).quantize(Decimal("0.00000001"))


def _extract_tokens(usage: Optional[Dict[str, Any]]) -> tuple[int, int]:
    """Extract input/output token counts from provider usage dict.

    Handles OpenRouter, Requesty, and OpenAI response shapes.

    Args:
        usage: Raw usage dict from the API response, or None.

    Returns:
        (input_tokens, output_tokens) — both default to 0 if missing.
    """
    if not usage:
        return 0, 0
    inp = (
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or usage.get("total_tokens", 0)
    )
    out = (
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or 0
    )
    return int(inp or 0), int(out or 0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def log_api_call(
    *,
    provider: str,
    model: str,
    role: str,
    context: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: Optional[Decimal] = None,
    latency_ms: int = 0,
    company_id: str = "",
    operation: str = "",
    agent_id: str = "",
    temperature: Optional[float] = None,
    correlation_id: str = "",
    request_path: str = "",
    response_path: str = "",
    success: bool = True,
    http_status: int = 200,
    extra: Optional[Dict[str, Any]] = None,
    usage: Optional[Dict[str, Any]] = None,
    conn: Optional[asyncpg.Connection] = None,
) -> bool:
    """Write a single row to api_cost_log.

    This is the ONE writer that every outbound API call in the platform must use.
    It is idempotent: duplicate (correlation_id, operation) pairs are ignored.

    Args:
        provider: Gateway name — 'openrouter', 'requesty', 'discord', 'telegram',
            'bybit', 'capital', etc.
        model: Model identifier (e.g. 'anthropic/claude-sonnet-4') or endpoint path.
        role: Logical service role (e.g. 'signal_interpretation_vision',
            'postmortem_text', 'mcp_tool_call').
        context: Human-readable context (max 100 chars).
        tokens_in: Input token count (or 0 if unknown).
        tokens_out: Output token count (or 0 if unknown).
        cost_usd: Explicit cost in USD. If None, estimated from tokens + model.
        latency_ms: Round-trip latency in milliseconds.
        company_id: Company short-name (e.g. 'rubicon') or empty for shared calls.
        operation: Operation identifier within the correlation chain
            (e.g. 'vision_primary', 'vision_fallback', 'text_completion').
        agent_id: Agent or service instance that made the call
            (e.g. 'surgeon2-host1', 'chart_hacker_opinion').
        temperature: Sampling temperature used (if applicable).
        correlation_id: 12-char hex ID from new_correlation_id() that ties this
            row back to the originating operation.
        request_path: API endpoint path (e.g. '/chat/completions').
        response_path: Response identifier or cached path.
        success: Whether the call returned a 2xx and usable payload.
        http_status: HTTP status code returned.
        extra: JSON-serialisable dict for provider-specific metadata.
        usage: Raw usage dict from the provider response. If given, tokens_in/out
            are extracted from it and override the explicit parameters.
        conn: Optional existing asyncpg connection. If None, acquires from pool.

    Returns:
        True if a row was inserted (or already existed), False on error.
    """
    if not _COST_LOG_ENABLED:
        return True

    # Extract tokens from usage dict if provided
    if usage is not None:
        tokens_in, tokens_out = _extract_tokens(usage)

    # Estimate cost if not provided
    if cost_usd is None:
        cost_usd = _estimate_cost_usd(model, tokens_in, tokens_out)

    # Clamp temperature to NUMERIC(4,2) range
    temp_val: Optional[Decimal] = None
    if temperature is not None:
        temp_val = Decimal(str(temperature)).quantize(Decimal("0.01"))

    extra_json = json.dumps(extra) if extra else None

    try:
        if conn is not None:
            await conn.execute(
                _INSERT_SQL,
                provider,
                model,
                role,
                context,
                tokens_in,
                tokens_out,
                cost_usd,
                latency_ms,
                company_id,
                operation,
                agent_id,
                temp_val,
                correlation_id,
                request_path,
                response_path,
                success,
                http_status,
                extra_json,
            )
        else:
            pool = await get_shared_pool()
            async with pool.acquire() as c:
                await c.execute(
                    _INSERT_SQL,
                    provider,
                    model,
                    role,
                    context,
                    tokens_in,
                    tokens_out,
                    cost_usd,
                    latency_ms,
                    company_id,
                    operation,
                    agent_id,
                    temp_val,
                    correlation_id,
                    request_path,
                    response_path,
                    success,
                    http_status,
                    extra_json,
                )
        return True
    except Exception as exc:
        logger.warning(
            "api_cost_log write failed for %s/%s/%s: %s",
            provider,
            role,
            correlation_id,
            exc,
            exc_info=True,
        )
        return False
