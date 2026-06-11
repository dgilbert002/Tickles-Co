"""MCP tools: Intelligence pipeline (Phase 3B+).

Location: /opt/tickles/shared/mcp/tools/intelligence.py
"""

import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..protocol import McpTool
from ..registry import ToolRegistry
from ..tools.context import ToolContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_COMPANY = "jarvais"
_CHART_HACKER_MODEL_PRIMARY = os.environ.get(
    "CHART_HACKER_MODEL_PRIMARY", "anthropic/claude-sonnet-4"
)
_CHART_HACKER_MODEL_FALLBACK = os.environ.get(
    "CHART_HACKER_MODEL_FALLBACK", "google/gemini-2.0-flash-001"
)
_OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
_OPENROUTER_BASE_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
)
MAX_IMAGE_SIZE_MB = float(os.environ.get("CHART_ANALYZE_MAX_IMAGE_MB", "10.0"))
MAX_IMAGE_SIZE_BYTES = int(MAX_IMAGE_SIZE_MB * 1024 * 1024)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_company(p: Dict[str, Any]) -> str:
    """Return company_id from params or default."""
    return str(p.get("companyId") or _DEFAULT_COMPANY)


def _fmt_ts(val: Any) -> Optional[str]:
    """Format a datetime / timestamp for JSON output."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, str):
        return val
    return str(val)


def _now_iso() -> str:
    """Return current UTC timestamp as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


async def _get_pool() -> Any:
    """Return the shared asyncpg pool, or raise if unavailable."""
    from shared.utils.db import DatabasePool

    return await DatabasePool.get_instance()


async def _get_company_pool(company_id: str) -> Any:
    """Return a pool for the company-specific database."""
    from shared.utils.db import get_pool

    dbname = f"tickles_{company_id}"
    return await get_pool(dbname)


def _image_to_base64(path: str) -> str:
    """Read an image file and return a base64-encoded data URI."""
    with open(path, "rb") as f:
        raw = f.read()
    mime = "image/png"
    if path.lower().endswith((".jpg", ".jpeg")):
        mime = "image/jpeg"
    elif path.lower().endswith(".webp"):
        mime = "image/webp"
    elif path.lower().endswith(".gif"):
        mime = "image/gif"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


class _BlankDefaultDict(dict):
    """dict that renders missing ``str.format_map`` keys as empty strings.

    The configured prompt template may carry optional placeholders
    (``{context}``, ``{recall_context}``, …). This lets the handler format
    it without supplying every key, so editing the prompt file can never
    crash the MCP tool with a ``KeyError``.
    """

    def __missing__(self, key: str) -> str:  # noqa: D401
        return ""


def _load_prompts() -> Dict[str, Any]:
    """Load chart analysis prompts from external JSON config.

    Falls back to embedded defaults if the config file is missing or invalid.
    """
    prompt_path = (
        Path(__file__).resolve().parent.parent.parent
        / "intelligence"
        / "prompts"
        / "chart_analysis.json"
    )
    if prompt_path.exists():
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load prompts from %s: %s", prompt_path, exc)
    return {
        "chart_hacker_mcp": {
            "system_prompt": (
                "You are ChartHacker. You analyze crypto chart images and screenshots "
                "shared by traders. You do not trade. You output ONLY a JSON object.\n\n"
                'JSON schema:\n{"direction": "long|short|neutral", "confidence": 0.0-1.0, '
                '"reasoning": "string", "levels": {"support": [float], "resistance": [float]}, '
                '"timeframe": "string", "pattern_detected": "string|null"}'
            ),
            "user_prompt_template": (
                "Analyze this {symbol} chart image. Identify the trend direction, "
                "key support/resistance levels, any chart pattern, and your confidence. "
                "Output ONLY valid JSON."
            ),
        }
    }


async def _call_openrouter_vision(
    model: str,
    image_data_uri: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 2048,
) -> Dict[str, Any]:
    """Call OpenRouter vision endpoint with a base64 image.

    Args:
        model: OpenRouter model identifier.
        image_data_uri: Base64 data URI of the image.
        system_prompt: System prompt text.
        user_prompt: User prompt text.
        max_tokens: Max output tokens.

    Returns:
        Dict with keys: text, model, tokens_in, tokens_out, cost_usd.

    Raises:
        RuntimeError: On HTTP error or malformed response.
    """
    import aiohttp

    if not _OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    url = f"{_OPENROUTER_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {_OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://tickles.co",
        "X-Title": "Tickles ChartHacker",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": image_data_uri}},
                ],
            },
        ],
        "max_tokens": max_tokens,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=60) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(
                    f"OpenRouter vision error {resp.status}: {body[:500]}"
                )
            data = await resp.json()

    choice = data.get("choices", [{}])[0]
    text = choice.get("message", {}).get("content", "")
    usage = data.get("usage", {})
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)

    # Rough cost estimate (varies by model; use conservative over-estimate)
    cost_usd = _estimate_cost_usd(model, tokens_in, tokens_out)

    return {
        "text": text,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
    }


def _estimate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    """Return a conservative cost estimate in USD.

    Args:
        model: Model identifier string.
        tokens_in: Input token count.
        tokens_out: Output token count.

    Returns:
        Estimated cost in USD.
    """
    # Conservative over-estimates per 1M tokens
    rates: Dict[str, Tuple[float, float]] = {
        "anthropic/claude-sonnet-4": (3.0, 15.0),
        "anthropic/claude-sonnet-4.5": (3.0, 15.0),
        "google/gemini-2.0-flash-001": (0.10, 0.40),
        "google/gemini-2.0-flash": (0.10, 0.40),
    }
    in_rate, out_rate = rates.get(model, (5.0, 20.0))
    return (tokens_in * in_rate + tokens_out * out_rate) / 1_000_000


def _extract_json_block(text: str) -> Optional[Dict[str, Any]]:
    """Extract a JSON object from markdown code fences or raw text.

    Args:
        text: Raw LLM output string.

    Returns:
        Parsed dict or None if no valid JSON found.
    """
    import re

    # Try fenced JSON first
    matches = re.findall(r"```json\s*(.*?)\s*```", text, flags=re.DOTALL)
    for m in matches:
        try:
            return json.loads(m)
        except json.JSONDecodeError:
            continue
    # Try raw JSON object
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try first { ... } block
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def _resolve_reinterpret_target(p: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve media row + local image path for reinterpret.

    Returns (context_dict, error_message).
    """
    media_id = p.get("mediaId") or p.get("media_id")
    interp_id = p.get("interpretationId") or p.get("interpretation_id")
    image_path = str(p.get("imagePath") or p.get("image_path") or "").strip()

    if media_id is not None:
        try:
            media_id = int(media_id)
        except (TypeError, ValueError):
            return None, "mediaId must be an integer"
    if interp_id is not None:
        try:
            interp_id = int(interp_id)
        except (TypeError, ValueError):
            return None, "interpretationId must be an integer"

    if not media_id and not interp_id and not image_path:
        return None, "one of mediaId, interpretationId, or imagePath is required"

    pool = await _get_pool()

    if media_id or interp_id:
        if interp_id:
            row = await pool.fetch_one(
                """
                SELECT
                  m.id AS media_id, m.local_path, m.source_url, m.mime_type,
                  n.id AS news_item_id, n.headline, n.content, n.source,
                  n.author, n.channel_name, n.instruments, n.source_id,
                  n.context_window,
                  si.id AS interpretation_id,
                  si.trader_profile_id,
                  si.instrument_symbol AS prior_symbol,
                  si.exchange AS prior_exchange,
                  si.timeframe AS prior_timeframe,
                  si.created_at AS interp_created_at,
                  si.prompt_version AS prior_prompt_version,
                  n.published_at AS news_published_at
                FROM public.signal_interpretations si
                JOIN public.media_items m ON m.id = si.media_item_id
                JOIN public.news_items n ON n.id = si.news_item_id
                WHERE si.id = $1
                """,
                (interp_id,),
            )
        else:
            row = await pool.fetch_one(
                """
                SELECT
                  m.id AS media_id, m.local_path, m.source_url, m.mime_type,
                  n.id AS news_item_id, n.headline, n.content, n.source,
                  n.author, n.channel_name, n.instruments, n.source_id,
                  n.context_window,
                  si.id AS interpretation_id,
                  si.trader_profile_id,
                  si.instrument_symbol AS prior_symbol,
                  si.exchange AS prior_exchange,
                  si.timeframe AS prior_timeframe,
                  si.created_at AS interp_created_at,
                  si.prompt_version AS prior_prompt_version,
                  n.published_at AS news_published_at
                FROM public.media_items m
                JOIN public.news_items n ON n.id = m.news_item_id
                LEFT JOIN LATERAL (
                  SELECT id, trader_profile_id, instrument_symbol, prompt_version,
                         exchange, timeframe, created_at
                  FROM public.signal_interpretations
                  WHERE media_item_id = m.id
                  ORDER BY created_at DESC
                  LIMIT 1
                ) si ON TRUE
                WHERE m.id = $1
                """,
                (media_id,),
            )
        if not row:
            key = f"interpretationId={interp_id}" if interp_id else f"mediaId={media_id}"
            return None, f"no media/news context found for {key}"
        ctx = dict(row)
        local_path = ctx.get("local_path")
        if not local_path or not os.path.isfile(local_path):
            source_url = ctx.get("source_url")
            if not source_url:
                return None, f"media_id={ctx['media_id']} has no local file and no source_url"
            try:
                import aiohttp
                import tempfile
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        source_url, timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status != 200:
                            return None, f"CDN download failed HTTP {resp.status}"
                        suffix = ".png" if ".png" in source_url.lower() else ".jpg"
                        fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="tickles_reinterpret_")
                        os.close(fd)
                        with open(tmp_path, "wb") as fh:
                            fh.write(await resp.read())
                        ctx["local_path"] = tmp_path
                        ctx["_temp_path"] = tmp_path
            except Exception as exc:
                return None, f"CDN download error: {exc}"
        return ctx, None

    if not os.path.isfile(image_path):
        return None, f"imagePath not found: {image_path}"
    return {
        "media_id": None,
        "local_path": image_path,
        "news_item_id": None,
        "headline": str(p.get("headline") or ""),
        "content": str(p.get("context") or p.get("newsContext") or ""),
        "source": str(p.get("source") or "discord"),
        "author": str(p.get("author") or "unknown"),
        "channel_name": str(p.get("channelName") or ""),
        "instruments": None,
        "source_id": None,
        "context_window": None,
        "interpretation_id": interp_id,
        "trader_profile_id": int(p.get("traderProfileId") or 0),
        "prior_symbol": str(p.get("symbol") or "UNKNOWN"),
        "prior_prompt_version": None,
    }, None


async def _handle_reinterpret(p: Dict[str, Any]) -> Dict[str, Any]:
    """Re-read a chart with the production Lens prompt (DB prompt_versions).

    By default returns preview only (no DB writes). Set ``persist`` to update an
    existing ``signal_interpretations`` row; set ``rearm`` to also cancel pending
    legs and arm new tracked_positions (requires ``interpretationId``).
    """
    persist = bool(p.get("persist", False))
    rearm = bool(p.get("rearm", False))
    if rearm:
        persist = True
    interp_id = p.get("interpretationId") or p.get("interpretation_id")
    if (persist or rearm) and not interp_id:
        return {
            "ok": False,
            "error": "interpretationId is required when persist or rearm is true",
        }

    ctx, err = await _resolve_reinterpret_target(p)
    if err:
        return {"ok": False, "error": err}

    prompt_version = str(
        p.get("promptVersion") or p.get("prompt_version") or ""
    ).strip() or None
    include_quant = bool(p.get("includeQuant", True))
    include_recall = bool(p.get("includeRecall", False))
    include_candles = bool(p.get("includeCandles", True))
    symbol_override = str(p.get("symbol") or "").strip()

    try:
        from datetime import datetime, timezone
        from shared.utils.correlation import new_correlation_id
        from shared.intelligence.interpretation_service import (
            InterpretationConfig,
            build_reinterpret_response,
            get_or_create_trader_profile,
            resolve_instrument_symbol,
            run_consensus,
            run_llm_track,
            run_quant_multiframe,
            _format_context_window,
            _parse_llm_json,
            _recall_relevant_memories,
        )
        from shared.intelligence.reinterpret_legs import enrich_reinterpret_with_legs
        from shared.intelligence.reinterpret_persist import persist_reinterpret_from_llm
        from shared.intelligence.text_signal_extractor import strip_reply_prefix

        pool = await _get_pool()
        cfg = InterpretationConfig()

        news_source = (ctx.get("source") or "discord").lower()
        author = ctx.get("author") or "unknown"
        platform = "telegram" if news_source == "telegram" else "discord"

        trader_profile_id = int(ctx.get("trader_profile_id") or 0)
        if not trader_profile_id:
            trader_profile_id = await get_or_create_trader_profile(
                pool, platform, author or "unknown", display_name=author,
            )

        symbol, exchange = await resolve_instrument_symbol(pool, ctx.get("instruments"))
        if symbol_override:
            symbol = symbol_override
        elif not symbol:
            symbol = str(ctx.get("prior_symbol") or "UNKNOWN")
        if not exchange:
            exchange = "bybit"

        clean_headline = strip_reply_prefix(ctx.get("headline") or "")
        clean_content = strip_reply_prefix(ctx.get("content") or "")
        news_context = f"{clean_headline}\n{clean_content}"[:1000]
        ctx_txt = _format_context_window(ctx.get("context_window"), author)
        if ctx_txt:
            news_context = f"{news_context}\n\n{ctx_txt}"

        cid = new_correlation_id("mcp_reinterpret")
        recall_context = ""
        if include_recall:
            recall_context = await _recall_relevant_memories(
                company=_default_company(p),
                symbol=symbol if symbol and symbol != "UNKNOWN" else None,
                direction=None,
                trader_handle=author if author != "unknown" else None,
                shared_pool=pool,
                correlation_id=cid,
            )

        quant = None
        consensus = None
        if include_quant and symbol and symbol != "UNKNOWN":
            quant, _ = await run_quant_multiframe(
                pool, pool, symbol, exchange, cfg.freshness_threshold_s,
            )

        llm = await run_llm_track(
            cfg=cfg,
            image_path=ctx["local_path"],
            news_context=news_context,
            instrument_symbol=symbol,
            correlation_id=cid,
            recall_context=recall_context,
            news_source=news_source,
            channel_name=str(ctx.get("channel_name") or ""),
            trader_profile_id=trader_profile_id,
            shared_pool=pool,
            prompt_version_override=prompt_version,
            chart_hacker_quant=quant,
        )

        if llm.instrument and (not symbol or symbol == "UNKNOWN"):
            symbol = llm.instrument

        parsed = _parse_llm_json(llm.raw_response)
        if include_quant and quant is not None:
            consensus = run_consensus(llm, quant)

        meta = {
            "media_id": ctx.get("media_id"),
            "interpretation_id": ctx.get("interpretation_id"),
            "news_item_id": ctx.get("news_item_id"),
            "prior_prompt_version": ctx.get("prior_prompt_version"),
            "correlation_id": cid,
            "image_path": ctx.get("local_path"),
            "author": author,
            "source": news_source,
            "instrument_symbol_resolved": symbol,
            "exchange": exchange,
            "persisted": persist,
            "rearmed": rearm,
        }
        result = build_reinterpret_response(
            llm, parsed=parsed, consensus=consensus, quant=quant, meta=meta,
        )

        call_ts = ctx.get("news_published_at") or ctx.get("interp_created_at")
        if call_ts is None:
            call_ts = datetime.now(timezone.utc)
        elif isinstance(call_ts, datetime) and call_ts.tzinfo is None:
            call_ts = call_ts.replace(tzinfo=timezone.utc)

        if persist or rearm:
            from shared.intelligence.interpretation_service import QuantResult
            persist_quant = quant if quant is not None else QuantResult()
            persist_consensus = consensus if consensus is not None else run_consensus(llm, persist_quant)
            persist_result = await persist_reinterpret_from_llm(
                pool,
                int(ctx["interpretation_id"]),
                ctx,
                llm,
                persist_consensus,
                persist_quant,
                rearm=rearm,
                correlation_id=cid,
            )
            result["persist_result"] = persist_result
            meta["persisted"] = True
            meta["rearmed"] = rearm

        if include_candles:
            from shared.dashboard.market_routes import _resolve_candle_symbol
            candle_symbol = _resolve_candle_symbol(
                llm.instrument or symbol,
                symbol if symbol and symbol != "UNKNOWN" else "BTC/USDT",
            )
            result = await enrich_reinterpret_with_legs(
                result,
                pool=pool,
                default_symbol=candle_symbol,
                default_exchange=exchange,
                call_ts=call_ts,
                interpretation_id=int(ctx["interpretation_id"]) if ctx.get("interpretation_id") else None,
                include_candles=True,
                include_position_updates=bool(persist or rearm),
            )

        return result
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("reinterpret failed")
        return {"ok": False, "error": str(exc)}
    finally:
        tmp = (ctx or {}).get("_temp_path")
        if tmp and os.path.isfile(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


async def _handle_chart_analyze(p: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze a chart image with a vision LLM.

    Params:
        imagePath (str): Absolute path to the image file.
        symbol (str, optional): Trading pair symbol (default BTCUSDT).
        model (str, optional): Override the vision model.
        maxTokens (int, optional): Max output tokens (default 2048).

    Returns:
        Dict with direction, confidence, reasoning, levels, pattern_detected,
        model_used, tokens_in, tokens_out, cost_usd, raw_text.
    """
    image_path = str(p.get("imagePath", ""))
    if not image_path or not os.path.isfile(image_path):
        return {
            "ok": False,
            "error": f"imagePath not found: {image_path}",
        }

    image_size = os.path.getsize(image_path)
    if image_size > MAX_IMAGE_SIZE_BYTES:
        return {
            "ok": False,
            "error": (
                f"Image too large: {image_size / (1024*1024):.1f}MB "
                f"(max {MAX_IMAGE_SIZE_MB}MB). "
                f"Resize or compress before analysis."
            ),
        }

    symbol = str(p.get("symbol", "BTCUSDT"))
    model = str(p.get("model") or _CHART_HACKER_MODEL_PRIMARY)
    max_tokens = int(p.get("maxTokens", 2048))

    prompts = _load_prompts().get("chart_hacker_mcp", {})
    system_prompt = prompts.get("system_prompt", "")
    user_template = prompts.get(
        "user_prompt_template",
        "Analyze this {symbol} chart image. Identify the trend direction, "
        "key support/resistance levels, any chart pattern, and your confidence. "
        "Output ONLY valid JSON.",
    )
    user_prompt = user_template.format_map(
        _BlankDefaultDict(symbol=symbol, context=str(p.get("context", "")))
    )

    try:
        data_uri = _image_to_base64(image_path)
    except OSError as exc:
        return {"ok": False, "error": f"Failed to read image: {exc}"}

    # Try primary model, then fallback
    last_error: Optional[str] = None
    for attempt_model in (model, _CHART_HACKER_MODEL_FALLBACK):
        try:
            result = await _call_openrouter_vision(
                attempt_model, data_uri, system_prompt, user_prompt, max_tokens
            )
            break
        except RuntimeError as exc:
            last_error = str(exc)
            logger.warning(
                "chart_analyze model=%s failed: %s", attempt_model, exc
            )
            continue
    else:
        return {
            "ok": False,
            "error": f"All vision models failed. Last: {last_error}",
        }

    parsed = _extract_json_block(result["text"]) or {}
    return {
        "ok": True,
        "direction": parsed.get("direction", "neutral"),
        "confidence": parsed.get("confidence", 0.0),
        "reasoning": parsed.get("reasoning", ""),
        "levels": parsed.get("levels", {}),
        "timeframe": parsed.get("timeframe", ""),
        "pattern_detected": parsed.get("pattern_detected"),
        "model_used": result["model"],
        "tokens_in": result["tokens_in"],
        "tokens_out": result["tokens_out"],
        "cost_usd": result["cost_usd"],
        "raw_text": result["text"],
    }


async def _handle_interpret(p: Dict[str, Any]) -> Dict[str, Any]:
    """Trigger one cycle of the InterpretationService.

    Params:
        companyId (str, optional): Target company database.
        batchSize (int, optional): Max items to process (default 10).

    Returns:
        Dict with processed_count, skipped_count, error_count, details.
    """
    company_id = _default_company(p)
    batch_size = int(p.get("batchSize", 10))

    try:
        from shared.intelligence.interpretation_service import (
            InterpretationConfig,
            InterpretationService,
        )

        cfg = InterpretationConfig(batch_size=batch_size)
        svc = InterpretationService(cfg)
        stats = await svc.run_cycle(company_id)
        return {
            "ok": True,
            "company_id": company_id,
            "processed_count": stats.get("processed", 0),
            "skipped_count": stats.get("skipped", 0),
            "error_count": stats.get("errors", 0),
            "details": stats.get("details", []),
        }
    except Exception as exc:
        logger.exception("interpret cycle failed for %s", company_id)
        return {"ok": False, "error": str(exc)}


async def _handle_trader_profile(p: Dict[str, Any]) -> Dict[str, Any]:
    """Get or create a trader profile.

    Params:
        platform (str): e.g. discord, telegram, twitter.
        handle (str): Raw handle / username.
        displayName (str, optional): Human-readable name.
        traderType (str, optional): e.g. scalper, swing, investor.
        primaryAssetClass (str, optional): e.g. crypto, forex.
        primaryTimeframe (str, optional): e.g. 1m, 5m, 1h, 4h, 1d.

    Returns:
        Dict with profile id, handle_normalized, and created flag.
    """
    platform = str(p.get("platform", "")).lower()
    handle_raw = str(p.get("handle", ""))
    if not platform or not handle_raw:
        return {"ok": False, "error": "platform and handle are required"}

    handle_normalized = handle_raw.lower().strip().lstrip("@")
    display_name = str(p.get("displayName") or handle_raw)
    trader_type = str(p.get("traderType") or "unknown")
    asset_class = str(p.get("primaryAssetClass") or "crypto")
    timeframe = str(p.get("primaryTimeframe") or "unknown")

    try:
        pool = await _get_pool()
        row = await pool.fetch_one(
            """
            INSERT INTO public.trader_profiles (
                platform, handle_raw, handle_normalized, display_name,
                trader_type, primary_asset_class, primary_timeframe,
                first_seen_at, last_seen_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NOW())
            ON CONFLICT (platform, handle_normalized)
            DO UPDATE SET
                last_seen_at = NOW(),
                display_name = EXCLUDED.display_name,
                trader_type = EXCLUDED.trader_type,
                primary_asset_class = EXCLUDED.primary_asset_class,
                primary_timeframe = EXCLUDED.primary_timeframe
            RETURNING id, created_at
            """,
            platform,
            handle_raw,
            handle_normalized,
            display_name,
            trader_type,
            asset_class,
            timeframe,
        )
        if row is None:
            return {"ok": False, "error": "upsert returned no row"}
        return {
            "ok": True,
            "profile_id": row["id"],
            "handle_normalized": handle_normalized,
            "created": row["created_at"] == _now_iso(),
        }
    except Exception as exc:
        logger.exception("trader_profile upsert failed")
        return {"ok": False, "error": str(exc)}


async def _handle_trader_score(p: Dict[str, Any]) -> Dict[str, Any]:
    """Get the latest performance score for a trader.

    Params:
        profileId (int): Trader profile ID.
        companyId (str, optional): Company database.
        scorePeriod (str, optional): e.g. 7d, 30d, 90d, all (default 30d).

    Returns:
        Dict with accuracy_pct, avg_confidence, total_signals, etc.
    """
    profile_id = int(p.get("profileId", 0))
    if profile_id <= 0:
        return {"ok": False, "error": "profileId is required"}

    company_id = _default_company(p)
    score_period = str(p.get("scorePeriod", "30d"))

    try:
        pool = await _get_company_pool(company_id)
        row = await pool.fetch_one(
            """
            SELECT
                accuracy_pct, avg_confidence, confidence_calibration,
                total_signals, validated_signals, correct_direction,
                total_pnl_usd, avg_pnl_per_signal, max_win_usd, max_loss_usd,
                sharpe_ratio, max_drawdown_pct, scored_at
            FROM public.trader_performance
            WHERE trader_profile_id = $1 AND score_period = $2
            ORDER BY scored_at DESC
            LIMIT 1
            """,
            profile_id,
            score_period,
        )
        if row is None:
            return {
                "ok": True,
                "found": False,
                "profile_id": profile_id,
                "score_period": score_period,
            }
        return {
            "ok": True,
            "found": True,
            "profile_id": profile_id,
            "score_period": score_period,
            "accuracy_pct": float(row["accuracy_pct"]) if row["accuracy_pct"] else None,
            "avg_confidence": float(row["avg_confidence"]) if row["avg_confidence"] else None,
            "confidence_calibration": float(row["confidence_calibration"])
            if row["confidence_calibration"]
            else None,
            "total_signals": row["total_signals"],
            "validated_signals": row["validated_signals"],
            "correct_direction": row["correct_direction"],
            "total_pnl_usd": float(row["total_pnl_usd"]) if row["total_pnl_usd"] else None,
            "avg_pnl_per_signal": float(row["avg_pnl_per_signal"])
            if row["avg_pnl_per_signal"]
            else None,
            "max_win_usd": float(row["max_win_usd"]) if row["max_win_usd"] else None,
            "max_loss_usd": float(row["max_loss_usd"]) if row["max_loss_usd"] else None,
            "sharpe_ratio": float(row["sharpe_ratio"]) if row["sharpe_ratio"] else None,
            "max_drawdown_pct": float(row["max_drawdown_pct"])
            if row["max_drawdown_pct"]
            else None,
            "scored_at": _fmt_ts(row["scored_at"]),
        }
    except Exception as exc:
        logger.exception("trader_score query failed")
        return {"ok": False, "error": str(exc)}


async def _handle_signals_recent(p: Dict[str, Any]) -> Dict[str, Any]:
    """List recent signal interpretations.

    Params:
        companyId (str, optional): Company database.
        limit (int, optional): Max rows (default 20, max 100).
        since (str, optional): ISO timestamp filter.
        direction (str, optional): Filter by consensus_direction.
        minConfidence (float, optional): Minimum consensus confidence.
        profileId (int, optional): Filter by trader_profile_id.

    Returns:
        Dict with signals list and count.
    """
    company_id = _default_company(p)
    limit = min(int(p.get("limit", 20)), 100)
    since = p.get("since")
    direction = p.get("direction")
    min_confidence = p.get("minConfidence")
    profile_id = p.get("profileId")

    conditions: List[str] = []
    args: List[Any] = []
    arg_idx = 1

    if since:
        conditions.append(f"created_at >= ${arg_idx}")
        args.append(since)
        arg_idx += 1
    if direction:
        conditions.append(f"consensus_direction = ${arg_idx}")
        args.append(str(direction).lower())
        arg_idx += 1
    if min_confidence is not None:
        conditions.append(f"consensus_confidence >= ${arg_idx}")
        args.append(float(min_confidence))
        arg_idx += 1
    if profile_id is not None:
        conditions.append(f"trader_profile_id = ${arg_idx}")
        args.append(int(profile_id))
        arg_idx += 1

    where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""

    try:
        pool = await _get_company_pool(company_id)
        rows = await pool.fetch_all(
            f"""
            SELECT
                id, news_item_id, media_item_id, trader_profile_id,
                model_version, param_hash, consensus_direction, consensus_confidence,
                consensus_method, instrument_symbol, exchange, market_data_fresh,
                market_data_at, created_at
            FROM public.signal_interpretations
            {where_clause}
            ORDER BY created_at DESC
            LIMIT ${arg_idx}
            """,
            *args,
            limit,
        )
        signals = []
        for r in rows:
            signals.append(
                {
                    "id": r["id"],
                    "news_item_id": r["news_item_id"],
                    "media_item_id": r["media_item_id"],
                    "trader_profile_id": r["trader_profile_id"],
                    "model_version": r["model_version"],
                    "param_hash": r["param_hash"],
                    "consensus_direction": r["consensus_direction"],
                    "consensus_confidence": float(r["consensus_confidence"])
                    if r["consensus_confidence"]
                    else None,
                    "consensus_method": r["consensus_method"],
                    "instrument_symbol": r["instrument_symbol"],
                    "exchange": r["exchange"],
                    "market_data_fresh": r["market_data_fresh"],
                    "market_data_at": _fmt_ts(r["market_data_at"]),
                    "created_at": _fmt_ts(r["created_at"]),
                }
            )
        return {
            "ok": True,
            "company_id": company_id,
            "count": len(signals),
            "signals": signals,
        }
    except Exception as exc:
        logger.exception("signals_recent query failed")
        return {"ok": False, "error": str(exc)}


async def _handle_signals_pending(p: Dict[str, Any]) -> Dict[str, Any]:
    """Count media items awaiting interpretation.

    Params:
        companyId (str, optional): Not used (shared table).
        sourceId (int, optional): Filter by collector source.
        mediaType (str, optional): Filter by media_type.

    Returns:
        Dict with pending_count and oldest_pending_age_seconds.
    """
    source_id = p.get("sourceId")
    media_type = p.get("mediaType")

    conditions = ["processing_status = 'downloaded'"]
    args: List[Any] = []
    arg_idx = 1

    if source_id is not None:
        conditions.append(f"source_id = ${arg_idx}")
        args.append(int(source_id))
        arg_idx += 1
    if media_type:
        conditions.append(f"media_type = ${arg_idx}")
        args.append(str(media_type))
        arg_idx += 1

    where_clause = "WHERE " + " AND ".join(conditions)

    try:
        pool = await _get_pool()
        row = await pool.fetch_one(
            f"""
            SELECT
                COUNT(*) AS pending_count,
                EXTRACT(EPOCH FROM (NOW() - MIN(created_at)))::FLOAT
                    AS oldest_age_seconds
            FROM public.media_items
            {where_clause}
            """,
            *args,
        )
        if row is None:
            return {"ok": True, "pending_count": 0, "oldest_age_seconds": None}
        return {
            "ok": True,
            "pending_count": row["pending_count"] or 0,
            "oldest_age_seconds": row["oldest_age_seconds"],
        }
    except Exception as exc:
        logger.exception("signals_pending query failed")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def _build_tools(_ctx: ToolContext) -> List[Tuple[McpTool, Any]]:
    """Build all intelligence MCP tools."""
    return [
        (
            McpTool(
                name="intelligence.reinterpret",
                description=(
                    "Re-read a chart with the production Lens prompt from prompt_versions "
                    "(DB). Returns the FULL schema: instrument, timeframe, setup_state, "
                    "trader_trades[] (multiple legs), chart_hacker_trades[], chart_analysis, "
                    "market views, sentiments, ai_agreement_with_trader, reasoning, parsed "
                    "raw JSON, legacy primary fields, optional quant+consensus, per-leg "
                    "candles (includeCandles), and optional persist/rearm onto an existing "
                    "interpretationId."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "mediaId": {
                            "type": "integer",
                            "description": "media_items.id to re-read.",
                        },
                        "interpretationId": {
                            "type": "integer",
                            "description": "signal_interpretations.id (resolves media + news).",
                        },
                        "imagePath": {
                            "type": "string",
                            "description": "Absolute path when no DB media row (standalone test).",
                        },
                        "promptVersion": {
                            "type": "string",
                            "description": (
                                "Exact prompt_versions.version, e.g. "
                                "2026.05.30-discord-semantic-v8. Default: loader chain "
                                "(trader prompt_id → source → newest Discord)."
                            ),
                        },
                        "companyId": {
                            "type": "string",
                            "description": "Company for recall context (default jarvais).",
                        },
                        "includeQuant": {
                            "type": "boolean",
                            "description": "Run quant track + consensus (default true).",
                        },
                        "includeRecall": {
                            "type": "boolean",
                            "description": "Inject mem0 recall into prompt (default false).",
                        },
                        "includeCandles": {
                            "type": "boolean",
                            "description": (
                                "Attach legs[] with per-trade timeframe + candles (default true). "
                                "Uses same symbol/TF resolution as dashboard signal-replay."
                            ),
                        },
                        "persist": {
                            "type": "boolean",
                            "description": (
                                "Write LLM result to signal_interpretations (requires interpretationId). "
                                "Default false — preview only."
                            ),
                        },
                        "rearm": {
                            "type": "boolean",
                            "description": (
                                "Cancel pending/open legs and arm new tracked_positions "
                                "(requires interpretationId; implies persist). Default false."
                            ),
                        },
                        "symbol": {
                            "type": "string",
                            "description": "Override instrument symbol for imagePath-only runs.",
                        },
                        "context": {
                            "type": "string",
                            "description": "News/discord text for imagePath-only runs.",
                        },
                        "author": {
                            "type": "string",
                            "description": "Trader handle for imagePath-only runs.",
                        },
                        "source": {
                            "type": "string",
                            "description": "discord|telegram for prompt selection (default discord).",
                        },
                    },
                },
                tags={"phase": "3b", "group": "intelligence"},
            ),
            _handle_reinterpret,
        ),
        (
            McpTool(
                name="intelligence.chart_analyze",
                description=(
                    "Quick vision read using the legacy MCP JSON file prompt (simplified "
                    "schema). For production dual-track output with multiple trades, use "
                    "intelligence.reinterpret instead."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "imagePath": {
                            "type": "string",
                            "description": "Absolute path to the chart image file.",
                        },
                        "symbol": {
                            "type": "string",
                            "description": "Trading pair symbol (default BTCUSDT).",
                        },
                        "model": {
                            "type": "string",
                            "description": "Override vision model (default from env).",
                        },
                        "maxTokens": {
                            "type": "integer",
                            "description": "Max output tokens (default 2048).",
                        },
                    },
                    "required": ["imagePath"],
                },
                tags={"phase": "3b", "group": "intelligence"},
            ),
            _handle_chart_analyze,
        ),
        (
            McpTool(
                name="intelligence.interpret",
                description="Trigger one cycle of the InterpretationService to process pending media items.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "companyId": {
                            "type": "string",
                            "description": "Target company database (default jarvais).",
                        },
                        "batchSize": {
                            "type": "integer",
                            "description": "Max items to process per cycle (default 10).",
                        },
                    },
                },
                tags={"phase": "3b", "group": "intelligence"},
            ),
            _handle_interpret,
        ),
        (
            McpTool(
                name="intelligence.trader_profile",
                description="Get or create a trader profile in the shared catalog.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "platform": {
                            "type": "string",
                            "description": "Source platform: discord, telegram, twitter, etc.",
                        },
                        "handle": {
                            "type": "string",
                            "description": "Raw username / handle.",
                        },
                        "displayName": {
                            "type": "string",
                            "description": "Human-readable display name.",
                        },
                        "traderType": {
                            "type": "string",
                            "description": "e.g. scalper, swing, investor.",
                        },
                        "primaryAssetClass": {
                            "type": "string",
                            "description": "e.g. crypto, forex, equities.",
                        },
                        "primaryTimeframe": {
                            "type": "string",
                            "description": "e.g. 1m, 5m, 1h, 4h, 1d.",
                        },
                    },
                    "required": ["platform", "handle"],
                },
                tags={"phase": "3b", "group": "intelligence"},
            ),
            _handle_trader_profile,
        ),
        (
            McpTool(
                name="intelligence.trader_score",
                description="Get the latest performance score for a trader profile.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "profileId": {
                            "type": "integer",
                            "description": "Trader profile ID.",
                        },
                        "companyId": {
                            "type": "string",
                            "description": "Company database (default jarvais).",
                        },
                        "scorePeriod": {
                            "type": "string",
                            "description": "7d, 30d, 90d, or all (default 30d).",
                        },
                    },
                    "required": ["profileId"],
                },
                tags={"phase": "3b", "group": "intelligence"},
            ),
            _handle_trader_score,
        ),
        (
            McpTool(
                name="intelligence.signals.recent",
                description="List recent signal interpretations with optional filters.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "companyId": {
                            "type": "string",
                            "description": "Company database (default jarvais).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max rows (default 20, max 100).",
                        },
                        "since": {
                            "type": "string",
                            "description": "ISO timestamp filter.",
                        },
                        "direction": {
                            "type": "string",
                            "description": "Filter by consensus_direction.",
                        },
                        "minConfidence": {
                            "type": "number",
                            "description": "Minimum consensus confidence.",
                        },
                        "profileId": {
                            "type": "integer",
                            "description": "Filter by trader_profile_id.",
                        },
                    },
                },
                tags={"phase": "3b", "group": "intelligence"},
            ),
            _handle_signals_recent,
        ),
        (
            McpTool(
                name="intelligence.signals.pending",
                description="Count media items awaiting interpretation.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "companyId": {
                            "type": "string",
                            "description": "Ignored (shared table).",
                        },
                        "sourceId": {
                            "type": "integer",
                            "description": "Filter by collector source ID.",
                        },
                        "mediaType": {
                            "type": "string",
                            "description": "Filter by media_type.",
                        },
                    },
                },
                tags={"phase": "3b", "group": "intelligence"},
            ),
            _handle_signals_pending,
        ),
        # Phase 3C: Position tracking tools
        (
            McpTool(
                name="intelligence.positions.open",
                description="List open tracked positions with live metrics for monitored traders.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "traderProfileId": {
                            "type": "integer",
                            "description": "Filter by trader profile ID.",
                        },
                        "sourceSlug": {
                            "type": "string",
                            "description": "Source slug (default charthackers_discord).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max rows (default 50).",
                        },
                    },
                },
                tags={"phase": "3c", "group": "intelligence"},
            ),
            _handle_positions_open,
        ),
        (
            McpTool(
                name="intelligence.positions.history",
                description="List closed position history for a trader with summary stats.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "traderProfileId": {
                            "type": "integer",
                            "description": "Trader profile ID (required).",
                        },
                        "days": {
                            "type": "integer",
                            "description": "Lookback period (default 30).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max rows (default 50).",
                        },
                    },
                    "required": ["traderProfileId"],
                },
                tags={"phase": "3c", "group": "intelligence"},
            ),
            _handle_positions_history,
        ),
        (
            McpTool(
                name="intelligence.traders.leaderboard",
                description="Get trader leaderboard ranked by P&L, win rate, or trade count.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "sourceSlug": {
                            "type": "string",
                            "description": "Source slug (default charthackers_discord).",
                        },
                        "days": {
                            "type": "integer",
                            "description": "Lookback period (default 30).",
                        },
                        "metric": {
                            "type": "string",
                            "description": "Sort by 'pnl', 'winrate', or 'trades' (default 'pnl').",
                        },
                    },
                },
                tags={"phase": "3c", "group": "intelligence"},
            ),
            _handle_traders_leaderboard,
        ),
        (
            McpTool(
                name="intelligence.epic.resolve",
                description="Resolve a trading symbol to a Capital.com epic code for CFD trading.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Trading symbol (e.g. GOLD, NAS100, BTCUSD).",
                        },
                        "channel": {
                            "type": "string",
                            "description": "Discord channel name for context-specific overrides.",
                        },
                        "assetClass": {
                            "type": "string",
                            "description": "Asset class hint: metals, indices, crypto, forex, commodities.",
                        },
                    },
                    "required": ["symbol"],
                },
                tags={"phase": "3c", "group": "intelligence"},
            ),
            _handle_epic_resolve,
        ),
        (
            McpTool(
                name="intelligence.critic.compare",
                description=(
                    "On-demand chart_hacker critic run vs trader setup. Fetches open "
                    "positions (by id list or latest N), enriches with live quant/funding/"
                    "RSI/trader score, calls the critic LLM, and returns a side-by-side "
                    "comparison with any stored agent_opinion. Default is dry-run (no DB "
                    "write); set persist=true to save opinions."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "companyId": {
                            "type": "string",
                            "description": "Optional tenant filter (tracked_positions.company_id).",
                        },
                        "positionIds": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Specific tracked_position ids to compare.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max positions when positionIds omitted (default 10, max 25).",
                            "default": 10,
                        },
                        "status": {
                            "type": "string",
                            "description": "Position status filter (default open).",
                            "default": "open",
                        },
                        "traderProfileId": {
                            "type": "integer",
                            "description": "Filter to one trader profile when no positionIds.",
                        },
                        "actorId": {
                            "type": "string",
                            "description": "Exact actor_id match.",
                        },
                        "actorIds": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Match any of these actor_id values.",
                        },
                        "actorIdPrefix": {
                            "type": "string",
                            "description": "Optional actor_id prefix filter (SQL LIKE).",
                        },
                        "persist": {
                            "type": "boolean",
                            "description": "Write agent_opinions + level backfill (default false).",
                            "default": False,
                        },
                    },
                },
                tags={"phase": "8", "group": "intelligence", "status": "live"},
            ),
            _handle_critic_compare,
        ),
        (
            McpTool(
                name="intelligence.techniques_top",
                description=(
                    "Graded technique ledger: which observed trading techniques "
                    "(order blocks, anchored VWAP, fib retrace, liquidity sweeps, ...) "
                    "actually win, per trader and symbol, from closed tracked positions. "
                    "Use this before taking a setup: a technique with a proven track "
                    "record is a reason to size up; a losing one is a warning."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "trader": {"type": "string", "description": "Trader handle filter (optional)."},
                        "symbol": {"type": "string", "description": "Base symbol filter, e.g. BTC (optional)."},
                        "minSamples": {"type": "integer", "description": "Minimum closed trades (default 1)."},
                        "limit": {"type": "integer", "description": "Max rows (default 25, cap 100)."},
                    },
                },
                read_only=True,
                tags={"category": "intelligence"},
            ),
            _handle_techniques_top,
        ),
    ]


# ---------------------------------------------------------------------------
# Phase 3C: Position tracking query tools
# ---------------------------------------------------------------------------

async def _handle_positions_open(p: Dict[str, Any]) -> Dict[str, Any]:
    """List open tracked positions with live metrics.

    Params:
        traderProfileId (int, optional): Filter by trader profile ID.
        sourceSlug (str, optional): Filter by source slug (default charthackers_discord).
        limit (int, optional): Max rows (default 50).

    Returns:
        Dict with positions list and count.
    """
    trader_id = p.get("traderProfileId")
    source_slug = str(p.get("sourceSlug", "charthackers_discord"))
    limit = min(int(p.get("limit", 50)), 200)

    conditions: List[str] = ["tp.status IN ('open', 'partial_exit')"]
    args: List[Any] = []
    arg_idx = 1

    if trader_id is not None:
        conditions.append(f"tp.trader_profile_id = ${arg_idx}")
        args.append(int(trader_id))
        arg_idx += 1

    where_clause = " AND ".join(conditions)

    try:
        pool = await _get_pool()
        rows = await pool.fetch_all(
            f"""
            SELECT
                tp.id, tp.trader_profile_id, tpf.display_name, tpf.handle_normalized AS platform_handle,
                tp.instrument_symbol, tp.epic_code AS instrument_epic, tp.direction AS side,
                tp.entry_price, tp.stop_loss, tp.take_profit_1 AS take_profit,
                tp.status, tp.outcome, tp.realized_pnl_usd AS realized_pnl,
                tp.created_at AS detected_at, tp.exit_timestamp AS closed_at, tp.risk_reward_ratio AS rr_at_entry,
                tp.metadata,
                n.channel_name, n.collected_at AS posted_at
            FROM public.tracked_positions tp
            JOIN public.trader_profiles tpf ON tpf.id = tp.trader_profile_id
            JOIN public.news_items n ON n.id = tp.news_item_id
            JOIN public.collector_catalog cc ON cc.source_type = tpf.platform
            WHERE {where_clause}
            AND cc.source_slug = ${arg_idx}
            ORDER BY tp.created_at DESC
            LIMIT ${arg_idx + 1}
            """,
            tuple(args) + (source_slug, limit),
        )
        positions = []
        for r in rows:
            positions.append(
                {
                    "id": r["id"],
                    "trader_profile_id": r["trader_profile_id"],
                    "display_name": r["display_name"],
                    "platform_handle": r["platform_handle"],
                    "instrument_symbol": r["instrument_symbol"],
                    "instrument_epic": r["instrument_epic"],
                    "side": r["side"],
                    "entry_price": float(r["entry_price"]) if r["entry_price"] else None,
                    "stop_loss": float(r["stop_loss"]) if r["stop_loss"] else None,
                    "take_profit": float(r["take_profit"]) if r["take_profit"] else None,
                    "status": r["status"],
                    "outcome": r["outcome"],
                    "realized_pnl": float(r["realized_pnl"]) if r["realized_pnl"] else None,
                    "detected_at": _fmt_ts(r["detected_at"]),
                    "closed_at": _fmt_ts(r["closed_at"]),
                    "rr_at_entry": float(r["rr_at_entry"]) if r["rr_at_entry"] else None,
                    "metadata": r["metadata"],
                    "channel_name": r["channel_name"],
                    "posted_at": _fmt_ts(r["posted_at"]),
                }
            )
        return {"ok": True, "count": len(positions), "positions": positions}
    except Exception as exc:
        logger.exception("positions_open query failed")
        return {"ok": False, "error": str(exc)}


async def _handle_positions_history(p: Dict[str, Any]) -> Dict[str, Any]:
    """List closed position history for a trader.

    Params:
        traderProfileId (int): Trader profile ID (required).
        days (int, optional): Lookback period (default 30).
        limit (int, optional): Max rows (default 50).

    Returns:
        Dict with closed positions list and summary stats.
    """
    trader_id = int(p.get("traderProfileId", 0))
    if trader_id <= 0:
        return {"ok": False, "error": "traderProfileId is required"}

    days = min(int(p.get("days", 30)), 365)
    limit = min(int(p.get("limit", 50)), 200)

    try:
        pool = await _get_pool()
        rows = await pool.fetch_all(
            """
            SELECT
                tp.id, tp.instrument_symbol, tp.instrument_epic, tp.side,
                tp.entry_price, tp.stop_loss, tp.take_profit,
                tp.outcome, tp.realized_pnl, tp.detected_at, tp.closed_at,
                tp.rr_at_entry, tp.metadata
            FROM public.tracked_positions tp
            WHERE tp.trader_profile_id = $1
            AND tp.status = 'closed'
            AND tp.detected_at >= NOW() - INTERVAL '%s days'
            ORDER BY tp.detected_at DESC
            LIMIT $2
            """,
            trader_id,
            days,
            limit,
        )
        positions = []
        total_pnl = 0.0
        wins = 0
        losses = 0
        for r in rows:
            pnl = float(r["realized_pnl"]) if r["realized_pnl"] else 0.0
            total_pnl += pnl
            if r["outcome"] == "take_profit":
                wins += 1
            elif r["outcome"] == "stop_loss":
                losses += 1
            positions.append(
                {
                    "id": r["id"],
                    "instrument_symbol": r["instrument_symbol"],
                    "instrument_epic": r["instrument_epic"],
                    "side": r["side"],
                    "entry_price": float(r["entry_price"]) if r["entry_price"] else None,
                    "stop_loss": float(r["stop_loss"]) if r["stop_loss"] else None,
                    "take_profit": float(r["take_profit"]) if r["take_profit"] else None,
                    "outcome": r["outcome"],
                    "realized_pnl": pnl,
                    "detected_at": _fmt_ts(r["detected_at"]),
                    "closed_at": _fmt_ts(r["closed_at"]),
                    "rr_at_entry": float(r["rr_at_entry"]) if r["rr_at_entry"] else None,
                }
            )
        total_closed = wins + losses
        win_rate = (wins / total_closed * 100.0) if total_closed > 0 else 0.0
        return {
            "ok": True,
            "count": len(positions),
            "trader_profile_id": trader_id,
            "days": days,
            "total_pnl": round(total_pnl, 2),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(win_rate, 1),
            "positions": positions,
        }
    except Exception as exc:
        logger.exception("positions_history query failed")
        return {"ok": False, "error": str(exc)}


async def _handle_traders_leaderboard(p: Dict[str, Any]) -> Dict[str, Any]:
    """Get trader leaderboard from tracked positions.

    Params:
        sourceSlug (str, optional): Source slug (default charthackers_discord).
        days (int, optional): Lookback period (default 30).
        metric (str, optional): Sort by 'pnl', 'winrate', 'trades' (default 'pnl').

    Returns:
        Dict with ranked trader list.
    """
    source_slug = str(p.get("sourceSlug", "charthackers_discord"))
    days = min(int(p.get("days", 30)), 365)
    metric = str(p.get("metric", "pnl")).lower()
    if metric not in ("pnl", "winrate", "trades"):
        metric = "pnl"

    order_col = {
        "pnl": "total_pnl",
        "winrate": "win_rate_pct",
        "trades": "total_trades",
    }[metric]

    try:
        pool = await _get_pool()
        rows = await pool.fetch_all(
            """
            SELECT
                tpf.id AS trader_profile_id,
                tpf.display_name,
                tpf.handle_normalized AS platform_handle,
                COUNT(tp.id) AS total_trades,
                SUM(CASE WHEN tp.outcome = 'tp1_hit' THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN tp.outcome = 'sl_hit' THEN 1 ELSE 0 END) AS losses,
                SUM(CASE WHEN tp.outcome = 'breakeven' THEN 1 ELSE 0 END) AS breakevens,
                COALESCE(SUM(tp.realized_pnl_usd), 0) AS total_pnl,
                CASE
                    WHEN COUNT(CASE WHEN tp.outcome IS NOT NULL THEN 1 END) > 0
                    THEN SUM(CASE WHEN tp.outcome = 'tp1_hit' THEN 1 ELSE 0 END)::float
                         / COUNT(CASE WHEN tp.outcome IS NOT NULL THEN 1 END) * 100
                    ELSE 0
                END AS win_rate_pct
            FROM public.trader_profiles tpf
            JOIN public.collector_catalog cc ON cc.source_type = tpf.platform
            LEFT JOIN public.tracked_positions tp
                ON tp.trader_profile_id = tpf.id
                AND tp.created_at >= NOW() - INTERVAL '%s days'
            WHERE cc.source_slug = $1
            GROUP BY tpf.id, tpf.display_name, tpf.handle_normalized
            ORDER BY %s DESC
            """
            % (days, order_col),
            (source_slug,),
        )
        traders = []
        for r in rows:
            total_closed = (r["wins"] or 0) + (r["losses"] or 0) + (r["breakevens"] or 0)
            traders.append(
                {
                    "trader_profile_id": r["trader_profile_id"],
                    "display_name": r["display_name"],
                    "platform_handle": r["platform_handle"],
                    "total_trades": r["total_trades"],
                    "wins": r["wins"] or 0,
                    "losses": r["losses"] or 0,
                    "breakevens": r["breakevens"] or 0,
                    "win_rate_pct": round(float(r["win_rate_pct"]), 1) if r["win_rate_pct"] else 0.0,
                    "total_pnl": round(float(r["total_pnl"]), 2),
                    "closed_trades": total_closed,
                }
            )
        return {"ok": True, "count": len(traders), "metric": metric, "days": days, "traders": traders}
    except Exception as exc:
        logger.exception("traders_leaderboard query failed")
        return {"ok": False, "error": str(exc)}


async def _handle_epic_resolve(p: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a trading symbol to a Capital.com epic code.

    Params:
        symbol (str): Trading symbol to resolve (e.g. GOLD, NAS100, BTCUSD).
        channel (str, optional): Discord channel name for context-specific overrides.
        assetClass (str, optional): Asset class hint (metals, indices, crypto, forex, commodities).

    Returns:
        Dict with epic code, confidence, and alternatives.
    """
    symbol = str(p.get("symbol", "")).strip().upper()
    if not symbol:
        return {"ok": False, "error": "symbol is required"}

    channel = p.get("channel")
    asset_class = p.get("assetClass")

    try:
        from shared.intelligence.epic_resolver import (
            resolve_epic,
            resolve_epic_with_fallback,
            is_valid_epic_format,
        )

        if channel:
            epic = resolve_epic_with_fallback(symbol, channel_override=str(channel))
        else:
            epic = resolve_epic(symbol)

        if epic:
            return {
                "ok": True,
                "symbol": symbol,
                "epic": epic,
                "valid_format": is_valid_epic_format(epic),
                "channel": channel,
                "asset_class": asset_class,
            }

        # No direct match — suggest alternatives
        from shared.intelligence.epic_resolver import _SEED_EPIC_MAP

        suggestions = []
        sym_lower = symbol.lower()
        for k, v in _SEED_EPIC_MAP.items():
            if sym_lower in k.lower() or k.lower() in sym_lower:
                suggestions.append({"symbol": k, "epic": v})
            if len(suggestions) >= 5:
                break

        return {
            "ok": True,
            "symbol": symbol,
            "epic": None,
            "valid_format": False,
            "suggestions": suggestions,
            "channel": channel,
            "asset_class": asset_class,
        }
    except Exception as exc:
        logger.exception("epic_resolve failed")
        return {"ok": False, "error": str(exc)}


async def _handle_critic_compare(p: Dict[str, Any]) -> Dict[str, Any]:
    """Run fresh chart_hacker critic vs trader setup (on-demand, no daemon wait).

    By default dry-run: calls the LLM but does NOT write ``agent_opinions``.
    Set ``persist`` true to save results like the opinion daemon would.
    """
    from shared.intelligence.critic_compare import run_critic_compare

    company_id = p.get("companyId") or p.get("company_id")
    if company_id is not None:
        company_id = str(company_id).strip() or None
    position_ids = p.get("positionIds") or p.get("position_ids")
    if position_ids is not None:
        position_ids = [int(x) for x in position_ids]

    limit = min(int(p.get("limit", 10)), 25)
    status = str(p.get("status", "open"))
    trader_profile_id = p.get("traderProfileId") or p.get("trader_profile_id")
    if trader_profile_id is not None:
        trader_profile_id = int(trader_profile_id)
    actor_id = p.get("actorId") or p.get("actor_id")
    actor_ids = p.get("actorIds") or p.get("actor_ids")
    actor_prefix = p.get("actorIdPrefix") or p.get("actor_id_prefix")
    persist = bool(p.get("persist", False))

    try:
        return await run_critic_compare(
            company_id=company_id,
            position_ids=position_ids,
            limit=limit,
            status=status,
            trader_profile_id=trader_profile_id,
            actor_id=str(actor_id) if actor_id else None,
            actor_ids=[str(a) for a in actor_ids] if actor_ids else None,
            actor_id_prefix=str(actor_prefix) if actor_prefix else None,
            persist=persist,
        )
    except Exception as exc:
        logger.exception("critic_compare failed")
        return {"ok": False, "error": str(exc)}


async def _handle_techniques_top(p: Dict[str, Any]) -> Dict[str, Any]:
    """Query the graded technique ledger (technique_stats).

    Params:
        trader (str, optional): Filter to one trader handle (plus globals).
        symbol (str, optional): Filter to one base symbol (plus globals).
        minSamples (int, optional): Minimum closed trades (default 1).
        limit (int, optional): Max rows (default 25).

    Returns:
        Dict with techniques list: technique, trader, symbol, wins, losses,
        win_rate, total_pnl_usd, sample_count, last_outcome.
    """
    try:
        pool = await _get_pool()
        min_samples = max(int(p.get("minSamples", 1)), 1)
        limit = min(int(p.get("limit", 25)), 100)
        trader = str(p.get("trader", "") or "").lower()
        symbol = str(p.get("symbol", "") or "").upper()
        for sep in ("/", ":", "-"):
            if sep in symbol:
                symbol = symbol.split(sep)[0]
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT technique, trader_handle, symbol_base, timeframe,
                       wins, losses, total_pnl_usd, sample_count,
                       last_outcome, last_position_id, updated_at
                FROM public.technique_stats
                WHERE sample_count >= $1
                  AND ($2 = '' OR trader_handle = $2 OR trader_handle = '')
                  AND ($3 = '' OR symbol_base = $3 OR symbol_base = '')
                ORDER BY (wins::float / GREATEST(wins + losses, 1)) DESC,
                         total_pnl_usd DESC
                LIMIT $4
                """,
                min_samples, trader, symbol, limit,
            )
        techniques = []
        for r in rows:
            total = r["wins"] + r["losses"]
            techniques.append({
                "technique": r["technique"],
                "trader": r["trader_handle"] or None,
                "symbol": r["symbol_base"] or None,
                "timeframe": r["timeframe"] or None,
                "wins": r["wins"],
                "losses": r["losses"],
                "win_rate": round(r["wins"] / max(total, 1), 3),
                "total_pnl_usd": float(r["total_pnl_usd"]),
                "sample_count": r["sample_count"],
                "last_outcome": r["last_outcome"],
                "last_position_id": r["last_position_id"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            })
        return {"ok": True, "count": len(techniques), "techniques": techniques}
    except Exception as exc:
        logger.exception("techniques_top failed")
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(registry: ToolRegistry, ctx: ToolContext) -> None:
    """Register all intelligence MCP tools on the given registry."""
    for tool, handler in _build_tools(ctx):
        registry.register(tool, handler)
    logger.info(
        "[intelligence] registered %d tools", len(_build_tools(ctx))
    )
