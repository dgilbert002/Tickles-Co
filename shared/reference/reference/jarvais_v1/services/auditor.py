"""
JarvAIs Audit & Accountability Engine
======================================
Ledger's post-mortem system: evidence collection, agent interrogation,
verdict & recommendations, daily roll-up, and change management.

Triggered automatically on trade close (won/lost/abandoned) and via
daily cron at 5am Dubai (1am UTC).
"""

import json
import logging
import math
import os
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.config_loader import (
    get_system_config, get_system_config_int, get_system_config_float,
    get_agent_profile as _shared_get_agent_profile,
    get_agent_soul, load_prompt,
)

logger = logging.getLogger("jarvais.auditor")

AUDITOR_MAX_TOKENS = 8192
AUDITOR_TEMP = 0.2


def _get_agent_profile(db, agent_id: str) -> Optional[Dict]:
    """Fetch an agent's full profile. Delegates to core.config_loader."""
    return _shared_get_agent_profile(db, agent_id)


def _resolve_agent_model_and_provider(profile: Dict) -> tuple:
    """Pick the active model + provider for an agent based on its model_mode setting.
    Returns (model_id, provider)."""
    mode = (profile.get("model_mode") or "free").lower()
    if mode == "paid":
        model = (profile.get("paid_model_primary")
                 or profile.get("free_model_primary")
                 or "gpt-4.1-mini")
        provider = (profile.get("paid_provider_primary")
                    or profile.get("free_provider_primary")
                    or "openrouter")
    else:
        model = (profile.get("free_model_primary")
                 or profile.get("paid_model_primary")
                 or "gpt-4.1-mini")
        provider = (profile.get("free_provider_primary")
                    or profile.get("paid_provider_primary")
                    or "openrouter")
    return model, provider


def _resolve_agent_model(profile: Dict) -> str:
    """Legacy wrapper — returns just the model id."""
    return _resolve_agent_model_and_provider(profile)[0]


def _get_auditor_model_and_provider(db) -> tuple:
    """Resolve the model + provider Ledger should use from agent_profiles."""
    profile = _get_agent_profile(db, "ledger")
    if not profile:
        logger.warning("[Auditor] Ledger profile not found, defaulting to gpt-4.1-mini/openrouter")
        return "gpt-4.1-mini", "openrouter"
    return _resolve_agent_model_and_provider(profile)


def _get_auditor_model(db) -> str:
    """Legacy wrapper — returns just the model id."""
    return _get_auditor_model_and_provider(db)[0]


def _get_model_interface():
    from core.model_interface import get_model_interface
    return get_model_interface()


_CRYPTO_CONTEXT = (
    "DOMAIN: Cryptocurrency perpetual futures traded 24/7 via CCXT on exchanges "
    "like Bybit, Binance, OKX. All positions are USDT-margined perps with "
    "leverage. Key factors: funding rates, liquidation wicks, exchange-specific "
    "wicks/scam wicks, session overlaps (Asia/London/NY), weekend low liquidity, "
    "Smart Money Concepts (OB, FVG, BOS, CHoCH, AMD cycle, liquidity sweeps). "
    "This is NOT equities, forex, or commodities — crypto has unique volatility, "
    "24/7 operation, and exchange-specific microstructure."
)


def _ensure_auditor_crypto_identity(identity: str) -> str:
    """If a DB-loaded identity omits crypto domain markers, prepend _CRYPTO_CONTEXT."""
    low = (identity or "").lower()
    if any(
        x in low
        for x in ("cryptocurrency", "perpetual", "usdt-margined", "ccxt", "domain:")
    ):
        return identity
    return f"{_CRYPTO_CONTEXT}\n\n{identity}"


def _validate_blame_assignment(blame: Any) -> Tuple[bool, float, str]:
    """Return (ok, sum, message). Verdict prompt expects blame buckets summing ~100."""
    if not blame or not isinstance(blame, dict):
        return False, 0.0, "blame_assignment missing or not a dict"
    total = 0.0
    for _k, v in blame.items():
        try:
            total += float(v)
        except (TypeError, ValueError):
            continue
    if abs(total - 100.0) <= 2.51:
        return True, total, ""
    return False, total, f"sum={total:.1f} (expected ~100)"


def _normalize_blame_assignment(blame: dict) -> dict:
    """Scale numeric blame values so they sum to 100."""
    nums = {}
    for k, v in blame.items():
        try:
            nums[k] = float(v)
        except (TypeError, ValueError):
            continue
    s = sum(nums.values())
    if s <= 0:
        return blame
    return {k: round(nums[k] / s * 100.0, 2) for k in nums}


def _forex_hallucination_in_summary(summary: str, trade_symbol: str) -> bool:
    """Detect obvious forex pair mentions in a USDT perp audit (hallucination risk)."""
    if not summary or not trade_symbol:
        return False
    ts = (trade_symbol or "").upper()
    if "USDT" not in ts:
        return False
    return bool(
        re.search(
            r"\b(?:EUR|GBP|JPY|AUD|NZD|CAD|CHF)/(?:USD|EUR|GBP|JPY)\b",
            summary,
            re.I,
        )
    )


def _get_auditor_identity(db, duo_id: str = None) -> str:
    """Load Ledger's system identity from system_config or agent soul."""
    identity = load_prompt(
        db, "auditor_system_identity",
        f"You are Ledger, Chief Auditor at JarvAIs. {_CRYPTO_CONTEXT} "
        f"Respond with valid JSON only.",
        min_length=10, duo_id=duo_id)
    return _ensure_auditor_crypto_identity(identity)


def _call_llm(db, model: str, provider: str, system_prompt: str,
              user_prompt: str, context: str = "audit",
              agent_id: str = "ledger", max_tokens: int = None,
              temperature: float = None,
              dossier_id: int = None,
              duo_id: str = None) -> Dict:
    """Call an LLM and return response text, cost, tokens."""
    mi = _get_model_interface()
    effective_max = max_tokens or get_system_config_int(
        db, "auditor_max_tokens", AUDITOR_MAX_TOKENS)
    effective_temp = temperature if temperature is not None else get_system_config_float(
        db, "auditor_temperature", AUDITOR_TEMP)
    resp = mi.query_with_model(
        model_id=model,
        provider=provider,
        role="auditor",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        account_id="global",
        max_tokens=effective_max,
        temperature=effective_temp,
        context=context,
        source="auditor",
        source_detail=agent_id,
        dossier_id=dossier_id,
        duo_id=duo_id,
    )
    return {
        "text": resp.content if resp else "",
        "cost": resp.cost_usd if resp else 0,
        "tokens": (resp.token_count_input or 0) + (resp.token_count_output or 0) if resp else 0,
        "model": model,
    }


def _parse_llm_json(text: str, required_keys: list = None) -> dict:
    """Extract and validate JSON from LLM output.

    Uses json.JSONDecoder().raw_decode() to correctly handle braces
    inside JSON string values.  Raises ValueError on failure so callers
    can retry.
    """
    if not text:
        raise ValueError("Empty LLM response")
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in LLM response")
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text, start)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON decode failed: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"Expected JSON object, got {type(obj).__name__}")
    if required_keys:
        missing = [k for k in required_keys if k not in obj]
        if missing:
            raise ValueError(f"Missing required keys: {missing}")
    return obj


def _call_llm_json(db, model: str, provider: str, system_prompt: str,
                   user_prompt: str, required_keys: list = None,
                   max_retries: int = 2, **kwargs) -> dict:
    """Call _call_llm and parse JSON output, retrying on parse failure.

    Returns a dict with 'parsed' (the JSON object), 'cost', 'tokens'.
    On total failure returns 'parsed' = {} with 'error' set.
    """
    total_cost, total_tokens = 0.0, 0
    last_error = ""
    for attempt in range(1, max_retries + 1):
        retry_prompt = user_prompt
        if attempt > 1:
            retry_prompt = (
                f"{user_prompt}\n\n"
                f"IMPORTANT: Your previous response was not valid JSON. "
                f"Error: {last_error[:200]}. Respond with ONLY valid JSON.")
        resp = _call_llm(db, model, provider, system_prompt,
                         retry_prompt, **kwargs)
        total_cost += resp.get("cost", 0)
        total_tokens += resp.get("tokens", 0)
        try:
            parsed = _parse_llm_json(resp.get("text", ""), required_keys)
            return {"parsed": parsed, "cost": total_cost,
                    "tokens": total_tokens, "model": model,
                    "text": resp.get("text", "")}
        except (json.JSONDecodeError, ValueError) as e:
            last_error = str(e)
            logger.warning(f"[Auditor] JSON parse attempt {attempt}/{max_retries} "
                          f"failed: {last_error}")
    logger.error(f"[Auditor] JSON parse failed after {max_retries} attempts: {last_error}")
    return {"parsed": {}, "cost": total_cost, "tokens": total_tokens,
            "model": model, "error": last_error, "text": ""}


def _load_auditor_prompt(db, prompt_key: str, code_default: str,
                         duo_id: str = None) -> str:
    """Load an auditor prompt from system_config DB table.
    Delegates to core.config_loader.load_prompt."""
    return load_prompt(db, prompt_key, code_default, min_length=100,
                       duo_id=duo_id)


def _humanise_analysis(raw: str) -> str:
    """Convert Stage 1/2 output to plain English. Strips JSON, keeps markdown."""
    if not raw or raw.strip() == "N/A":
        return "N/A"
    text = raw.strip()
    if text.startswith("{") or text.startswith("["):
        try:
            obj = json.loads(text)
            return _json_to_prose(obj)
        except (json.JSONDecodeError, TypeError):
            pass
    return text[:3000]


def _json_to_prose(obj, depth=0) -> str:
    """Recursively convert a JSON object into readable bullet points."""
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            label = k.replace("_", " ").title()
            if isinstance(v, (dict, list)):
                child = _json_to_prose(v, depth + 1)
                if child.strip():
                    lines.append(f"{'  ' * depth}{label}:\n{child}")
            elif v is not None and str(v).strip():
                lines.append(f"{'  ' * depth}{label}: {v}")
    elif isinstance(obj, list):
        for item in obj[:20]:
            if isinstance(item, dict):
                lines.append(_json_to_prose(item, depth))
            elif item is not None:
                lines.append(f"{'  ' * depth}- {item}")
    return "\n".join(lines)[:3000]


def _interview_agent(db, agent_id: str, question: str,
                     evidence_context: str = "",
                     dossier_id: int = None,
                     duo_id: str = None) -> Dict:
    """Interview an agent using their own model and soul/identity."""
    profile = _get_agent_profile(db, agent_id)
    if not profile:
        return {"text": f"[Agent {agent_id} not found]", "cost": 0,
                "tokens": 0, "model": "none"}

    model, provider = _resolve_agent_model_and_provider(profile)

    identity = profile.get("identity_prompt", "")
    system = (
        f"{identity}\n\n"
        f"CONTEXT: You are being interviewed by Ledger, the Chief Auditor, "
        f"about a trade post-mortem. This is NOT a normal analysis task.\n\n"
        f"MANDATORY RULES FOR THIS INTERVIEW:\n"
        f"1. Respond ONLY in plain flowing prose. NO JSON, NO code, NO structured data.\n"
        f"2. NO bullet points, NO numbered lists, NO markdown headers, NO blank lines between paragraphs.\n"
        f"3. Write in continuous paragraphs like a witness statement.\n"
        f"4. NEVER speculate or fabricate. Only state facts you actually had or observed. "
        f"If the data was empty or you had no information, say exactly that: "
        f"'I did not have this data' or 'No events were provided to me'. "
        f"NEVER invent events, indicators, or data that were not given to you.\n"
        f"5. Be honest and self-critical. Acknowledge failures where they exist.\n"
        f"6. Reference specific prices, timeframes, and conditions by name.\n"
        f"7. Grade yourself numerically where asked.\n"
    )

    user = f"{evidence_context}\n\n---\n\nAUDITOR QUESTION:\n{question}"

    return _call_llm(db, model, provider, system, user,
                     context="audit_interview", agent_id=agent_id,
                     max_tokens=4096, temperature=0.3,
                     dossier_id=dossier_id, duo_id=duo_id)


# ═══════════════════════════════════════════════════════════════════════
# Cross-Duo Comparison (Phase 7)
# ═══════════════════════════════════════════════════════════════════════

def _cross_duo_comparison(db, dossier: Dict, duo_id: str) -> Optional[str]:
    """If another duo traded the same symbol around the same time, build a
    comparison note for the audit report."""
    try:
        symbol = dossier.get("symbol")
        dossier_id = dossier["id"]
        created = dossier.get("created_at")
        if not symbol or not created:
            return None

        others = db.fetch_all("""
            SELECT id, duo_id, direction, status,
                   realised_pnl, realised_pnl_pct,
                   stage1_model_used, stage2_model_used,
                   confidence_score, entry_price, stop_loss,
                   take_profit_1
            FROM trade_dossiers
            WHERE symbol = %s AND id != %s
              AND duo_id != %s
              AND created_at >= DATE_SUB(%s, INTERVAL 24 HOUR)
              AND created_at <= DATE_ADD(%s, INTERVAL 24 HOUR)
              AND status IN ('won', 'lost', 'live', 'open_order')
            ORDER BY created_at DESC LIMIT 5
        """, (symbol, dossier_id, duo_id, created, created))

        if not others:
            return None

        lines = [f"Cross-Duo Analysis for {symbol} (this dossier: {duo_id} #{dossier_id}):"]
        this_dir = dossier.get("direction", "?")
        this_pnl = dossier.get("realised_pnl")
        this_status = dossier.get("status")
        lines.append(f"  This duo ({duo_id}): {this_dir} -> {this_status}"
                     f" | P&L: ${float(this_pnl or 0):.2f}"
                     f" | S1: {dossier.get('stage1_model_used', '?')}"
                     f" | S2: {dossier.get('stage2_model_used', '?')}")
        for o in others:
            o_duo = o.get("duo_id", "?")
            o_dir = o.get("direction", "?")
            o_pnl = float(o.get("realised_pnl") or 0)
            o_status = o.get("status", "?")
            lines.append(
                f"  Other duo ({o_duo} #{o['id']}): {o_dir} -> {o_status}"
                f" | P&L: ${o_pnl:.2f}"
                f" | S1: {o.get('stage1_model_used', '?')}"
                f" | S2: {o.get('stage2_model_used', '?')}"
                f" | Conf: {o.get('confidence_score', '?')}")

            if this_dir and o_dir and this_dir != o_dir:
                lines.append(f"  ** OPPOSING DIRECTIONS: {duo_id}={this_dir} vs {o_duo}={o_dir}")

        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"[Auditor] Cross-duo comparison error: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════
# Phase A: Evidence Collection
# ═══════════════════════════════════════════════════════════════════════

def _collect_evidence(db, dossier: Dict) -> Dict:
    """Gather all evidence for a trade post-mortem. Pure data, no LLM."""
    dossier_id = dossier["id"]
    symbol = dossier["symbol"]
    created = dossier.get("created_at")
    # Prefer explicit closed_at from live_trades, then dossier updated_at as fallback
    lt_close = None
    if dossier.get("id"):
        lt_row = db.fetch_one(
            "SELECT closed_at FROM live_trades WHERE dossier_id = %s "
            "AND status = 'closed' ORDER BY closed_at DESC LIMIT 1",
            (dossier["id"],))
        if lt_row and lt_row.get("closed_at"):
            lt_close = lt_row["closed_at"]
    closed = lt_close or dossier.get("updated_at") or datetime.utcnow()

    def _safe_json(val, fallback=None):
        """Parse JSON safely; return fallback on any error."""
        if fallback is None:
            fallback = []
        if not val:
            return fallback
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, type(fallback)) else fallback
        except (json.JSONDecodeError, TypeError, ValueError):
            if isinstance(fallback, list) and isinstance(val, str):
                return [{"raw": line} for line in val.split("\n") if line.strip()]
            return fallback

    evidence = {
        "dossier": {k: _serialise(v) for k, v in dossier.items()},
        "stage1_output": dossier.get("stage1_ta_output", "") or dossier.get("stage1_raw_response", ""),
        "stage2_output": dossier.get("stage2_raw_response", ""),
        "conditions": _safe_json(dossier.get("conditions_for_entry"), []),
        "probability_history": _safe_json(dossier.get("probability_history"), []),
        "tracker_log": _safe_json(dossier.get("tracker_log"), []),
        "tracker_conversation": _safe_json(dossier.get("tracker_conversation"), []),
    }

    evidence["hypothesis"] = _safe_json(dossier.get("stage2_hypothesis"), {})

    # Candle data from decision to close (lightweight — M5 not needed for postmortem)
    try:
        from services.data_scientist import get_data_scientist
        ds = get_data_scientist(db)
        timeframes = {"M5": 2, "M15": 7, "H1": 14, "H4": 30}
        evidence["candles"] = {}
        for tf, days in timeframes.items():
            candles = ds.get_candles_from_db(symbol, {tf: days})
            if candles and tf in candles:
                evidence["candles"][tf] = candles[tf][:100]
    except Exception as e:
        logger.debug(f"[Auditor] Candle fetch for evidence: {e}")
        evidence["candles"] = {}

    # Original TA from dossier (what Apex saw) + minimal fresh TA for hindsight comparison
    evidence["original_ta"] = dossier.get("stage1_ta_output", "") or dossier.get("stage1_raw_response", "")
    # Full fresh TA from BillNye — all indicators, multiple timeframes.
    # The Auditor needs complete hindsight: what Apex saw (original_ta above)
    # vs what actually played out (fresh_ta below). No restrictions.
    try:
        from services.data_scientist import get_data_scientist
        ds = get_data_scientist(db)
        candles = ds.get_candles_from_db(symbol, {"M5": 2, "M15": 7, "H1": 14, "H4": 30})
        if candles:
            evidence["fresh_ta"] = ds.compute_all(symbol, candles)
        else:
            evidence["fresh_ta"] = {"note": "No candles available for fresh TA"}
    except Exception as e:
        logger.debug(f"[Auditor] Fresh TA: {e}")
        evidence["fresh_ta"] = {}

    # What Apex originally saw for geo/macro context
    evidence["original_geo_macro"] = {
        "geo": dossier.get("geopolitical_context", ""),
        "macro": dossier.get("macroeconomic_context", ""),
    }

    # Geo/Macro news during trade window (expanded to ±2 hours for context)
    try:
        if created and isinstance(created, datetime):
            window_start = (created - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        elif created:
            window_start = str(created)
        else:
            window_start = None

        if isinstance(closed, datetime):
            window_end = (closed + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        else:
            window_end = str(closed) if closed else None

        if window_start and window_end:
            sym_u = (symbol or "").upper().replace("/", "")
            sym_base = sym_u.replace("USDT", "").replace("PERP", "").strip()[:24]
            sym_like = f"%{sym_base}%" if len(sym_base) >= 2 else None
            # Crypto-relevant headlines + symbol match reduce forex-only noise (plan 2A).
            if sym_like:
                rows = db.fetch_all("""
                    SELECT source, source_detail, title, content, published_at
                    FROM news_items
                    WHERE published_at BETWEEN %s AND %s
                      AND (
                        source = 'news'
                        OR source_detail LIKE '%%eopolitic%%'
                        OR source_detail LIKE '%%macro%%'
                        OR source_detail LIKE '%%econom%%'
                        OR source_detail LIKE '%%CNBC%%'
                        OR source_detail LIKE '%%FXStreet%%'
                        OR source_detail LIKE '%%Investing.com%%'
                        OR source_detail LIKE '%%Yahoo Finance%%'
                        OR source_detail LIKE '%%Fed%%'
                        OR source_detail LIKE '%%Calendar%%'
                        OR source_detail LIKE '%%BRICS%%'
                        OR source_detail LIKE '%%AntiFakeNews%%'
                        OR source_detail LIKE '%%daily-bias%%'
                        OR source_detail LIKE '%%daily-market%%'
                        OR LOWER(title) LIKE '%%crypto%%'
                        OR LOWER(title) LIKE '%%bitcoin%%'
                        OR LOWER(title) LIKE '%%ethereum%%'
                        OR LOWER(content) LIKE '%%crypto%%'
                        OR title LIKE %s
                        OR content LIKE %s
                      )
                    ORDER BY published_at
                    LIMIT 50
                """, (window_start, window_end, sym_like, sym_like))
            else:
                rows = db.fetch_all("""
                    SELECT source, source_detail, title, content, published_at
                    FROM news_items
                    WHERE published_at BETWEEN %s AND %s
                      AND (
                        source = 'news'
                        OR source_detail LIKE '%%eopolitic%%'
                        OR source_detail LIKE '%%macro%%'
                        OR source_detail LIKE '%%econom%%'
                        OR source_detail LIKE '%%CNBC%%'
                        OR source_detail LIKE '%%FXStreet%%'
                        OR source_detail LIKE '%%Investing.com%%'
                        OR source_detail LIKE '%%Yahoo Finance%%'
                        OR source_detail LIKE '%%Fed%%'
                        OR source_detail LIKE '%%Calendar%%'
                        OR source_detail LIKE '%%BRICS%%'
                        OR source_detail LIKE '%%AntiFakeNews%%'
                        OR source_detail LIKE '%%daily-bias%%'
                        OR source_detail LIKE '%%daily-market%%'
                        OR LOWER(title) LIKE '%%crypto%%'
                        OR LOWER(title) LIKE '%%bitcoin%%'
                        OR LOWER(title) LIKE '%%ethereum%%'
                        OR LOWER(content) LIKE '%%crypto%%'
                      )
                    ORDER BY published_at
                    LIMIT 50
                """, (window_start, window_end))
            evidence["geo_macro_news"] = [
                {k: _serialise(v) for k, v in r.items()} for r in (rows or [])]
        else:
            evidence["geo_macro_news"] = []
    except Exception as e:
        logger.debug(f"[Auditor] Geo/macro news fetch: {e}")
        evidence["geo_macro_news"] = []

    # Agent profiles (for interrogation context and verdict recommendations)
    for aid in ("quant", "apex", "tracker", "geo", "macro"):
        p = _get_agent_profile(db, aid)
        if p:
            evidence[f"profile_{aid}"] = {
                "soul": p.get("soul", ""),
                "identity": p.get("identity_prompt", ""),
            }

    # Multi-TF ATR for SL quality analysis
    try:
        from services.data_scientist import get_data_scientist
        ds = get_data_scientist(db)
        evidence["multi_tf_atr"] = ds.compute_multi_tf_atr(symbol)
    except Exception as e:
        logger.debug(f"[Auditor] Multi-TF ATR: {e}")
        evidence["multi_tf_atr"] = {}

    # Companion data during trade
    try:
        evidence["companion"] = json.loads(
            dossier.get("dossier_intelligence") or "{}"
        ).get("companion_data", {})
    except (json.JSONDecodeError, TypeError):
        evidence["companion"] = {}

    return evidence


# ═══════════════════════════════════════════════════════════════════════
# Phase B: Independent Assessment
# ═══════════════════════════════════════════════════════════════════════

ASSESSMENT_PROMPT = """You are Ledger, Chief Auditor at JarvAIs. You are reviewing trade evidence BEFORE interviewing any agents. Form your independent opinion.

Analyse the following evidence and provide your preliminary assessment. Go BEYOND just checking whether conditions were met. Evaluate the QUALITY of the trade setup itself.

## TRADE SUMMARY
Symbol: {symbol} | Direction: {direction} | Outcome: {outcome}
Entry: {entry} | SL: {sl} | TP1: {tp1}
P&L: {pnl}
Created: {created} | Closed: {closed}

## STAGE 1 (QUANT) ANALYSIS
{stage1_excerpt}

## STAGE 2 (APEX) DECISION
{stage2_excerpt}

## CONDITIONS AT CLOSE
{conditions_summary}

## FRESH TA (BillNye retroactive)
{fresh_ta_summary}

## GEO/MACRO NEWS DURING TRADE
{news_summary}

## PROBABILITY TIMELINE
{prob_timeline}

## YOUR ANALYSIS MANDATE

Do NOT just check whether conditions were met before entry. That is the MINIMUM. You must also evaluate:

1. DIRECTION ANALYSIS: Was {direction} the right call? Using the fresh TA and candle data, would the OPPOSITE direction have been more profitable? On M5-M15, was there a quick counter-trend scalp opportunity that Apex ignored?

2. ENTRY QUALITY: Was the entry price optimal? Could Apex have placed a limit order at a deeper pullback level (e.g., a lower OB/FVG for longs, a higher one for shorts)? How many points/pips of improvement was available?

3. SMC FRAMEWORK REVIEW: Did Apex correctly identify the AMD phase? Was there a proper liquidity sweep before entry? Was displacement confirmed? Was the entry at an OB/FVG or was it a low-quality entry (e.g., chasing, entering during manipulation)?

4. SETUP QUALITY (independent of conditions): Rate the overall setup. A trade can have all conditions met but still be a poor setup (weak displacement, exhausted OB, wrong session). Conversely a trade can miss a condition but be structurally sound.

5. ALTERNATIVE SETUPS: What would the optimal trade have looked like on this symbol during this window? Consider both directions. Even in a bullish trend, a resistance rejection or overbought condition with strong bear order blocks can give a profitable M5-M15 short.

6. STOP LOSS QUALITY: Was the SL placed at a structural level (beyond an order block, beyond a liquidity pool, beyond recent wick clusters)? Or was it arbitrarily tight/wide? Cross-reference the SL distance against the ATR data provided. Was the SL vulnerable to a liquidity sweep? If the SL was hit, was it a genuine invalidation or a stop hunt that reversed immediately after?

7. TAKE PROFIT QUALITY: Were TPs placed at logical structural levels (previous swing highs/lows, order blocks, FVGs, session highs/lows)? Were they too ambitious (price never reached) or too conservative (left significant profit on the table)?

## MULTI-TIMEFRAME ATR CONTEXT
{atr_context}

Respond with ONLY this JSON:
{{
  "preliminary_blame": {{"quant": <0-100>, "apex": <0-100>, "tracker": <0-100>, "geo_macro": <0-100>, "data_quality": <0-100>, "market": <0-100>}},
  "key_questions_for_quant": ["<specific question 1>", "<specific question 2>"],
  "key_questions_for_apex": ["<question about direction choice>", "<question about entry price optimization>", "<question about SL structural placement>", "<question about M5-M15 scalp opportunities>"],
  "key_questions_for_tracker": ["<specific question 1>"],
  "key_questions_for_geo_macro": ["<specific question 1>"],
  "initial_root_cause": "<one sentence>",
  "direction_assessment": "<Was the direction correct? What does the fresh TA show about the opposite direction?>",
  "entry_quality_score": <1-10>,
  "entry_quality_notes": "<Where was the optimal entry vs where Apex entered? How many pips/points difference?>",
  "sl_quality_score": <1-10>,
  "sl_quality_notes": "<Was SL at a structural level? Was it beyond the nearest liquidity pocket? Was the distance sensible given the ATR? Was it swept by a wick that then reversed?>",
  "tp_quality_score": <1-10>,
  "tp_quality_notes": "<Were TPs at structural levels? Too ambitious or too conservative? How much profit was left on the table or never reached?>",
  "smc_framework_score": <1-10>,
  "smc_framework_notes": "<Did Apex correctly identify AMD phase, liquidity sweep, displacement, OB/FVG? What was missed?>",
  "setup_quality_score": <1-10>,
  "setup_quality_notes": "<Overall setup quality independent of whether conditions were met>",
  "optimal_alternative": "<What would the best trade on this symbol have been during this window? Direction, approximate entry, SL, TP>",
  "scalp_opportunities_missed": "<Were there any M5-M15 counter-trend scalps visible in the data?>",
  "data_quality_concerns": ["<concern 1>", "<concern 2>"],
  "what_was_correct": ["<thing that was right>"]
}}"""


def _run_independent_assessment(db, evidence: Dict, dossier: Dict,
                                duo_id: str = None) -> Dict:
    """Phase B: Auditor forms preliminary opinion before interviews."""
    conds = evidence.get("conditions", [])
    cond_summary = "\n".join(
        f"  C{c.get('id','?')}: {c.get('description','?')[:80]} — "
        f"{c.get('status','?')} (weight:{c.get('weight',5)})"
        for c in conds[:10])

    prob_hist = evidence.get("probability_history", [])
    prob_tl = "\n".join(
        f"  {p.get('timestamp','?')}: {p.get('probability','?')}% — "
        f"{p.get('reason','')[:60]}"
        for p in prob_hist[-10:])

    fresh_ta = evidence.get("fresh_ta", {})
    ta_summary = _json_to_prose(fresh_ta) if fresh_ta else "N/A"

    news = evidence.get("geo_macro_news", [])
    news_summary = "\n".join(
        f"  [{n.get('published_at','')}] {n.get('source_detail','')}: {n.get('title','')[:80]}"
        for n in news[:10]) or "No relevant news found"

    # Multi-TF ATR context for SL quality analysis
    atr_ctx_lines = []
    atr_data = evidence.get("multi_tf_atr", {})
    if atr_data:
        for tf in ("M5", "M15", "H1", "H4"):
            d = atr_data.get(tf, {})
            if d.get("value"):
                atr_ctx_lines.append(
                    f"  {tf} ATR(14) = {d['value']:.6f} "
                    f"({d.get('pct_of_price', 0):.3f}% of price, {d.get('volatility','?')})")
    atr_context = "\n".join(atr_ctx_lines) if atr_ctx_lines else "ATR data unavailable"

    assessment_template = _load_auditor_prompt(
        db, "auditor_assessment_prompt", ASSESSMENT_PROMPT, duo_id=duo_id)
    # Prefer actual exchange prices over paper targets
    actual_entry = (dossier.get("live_entry_price")
                    or dossier.get("actual_entry_price")
                    or dossier.get("entry_price") or "?")
    actual_exit = (dossier.get("live_exit_price")
                   or dossier.get("actual_exit_price")
                   or "?")
    live_margin = dossier.get("live_margin") or dossier.get("margin_usd")
    live_lev = dossier.get("live_leverage") or dossier.get("leverage")

    # Build execution context for the auditor
    exec_ctx = ""
    if live_margin or live_lev:
        exec_ctx = (f"\nExecution: Margin=${live_margin}, "
                    f"Leverage={live_lev}x, "
                    f"Exit={actual_exit}")
        if dossier.get("entry_fee"):
            exec_ctx += f", Entry Fee=${dossier['entry_fee']:.4f}"

    prompt = assessment_template.format(
        symbol=dossier.get("symbol", "?"),
        direction=dossier.get("direction", "?"),
        outcome=dossier.get("status", "?"),
        entry=actual_entry,
        sl=dossier.get("stop_loss", "?"),
        tp1=dossier.get("take_profit_1", "?"),
        pnl=f"{dossier.get('realised_pnl', 0)} ({dossier.get('realised_pnl_pct', 0)}%)"
            f"{exec_ctx}",
        created=str(dossier.get("created_at", "")),
        closed=str(dossier.get("updated_at", "")),
        stage1_excerpt=_humanise_analysis(evidence.get("stage1_output") or "N/A"),
        stage2_excerpt=_humanise_analysis(evidence.get("stage2_output") or "N/A"),
        conditions_summary=cond_summary or "No conditions",
        fresh_ta_summary=ta_summary,
        news_summary=news_summary,
        prob_timeline=prob_tl or "No history",
        atr_context=atr_context,
    )

    auditor_model, auditor_provider = _get_auditor_model_and_provider(db)
    jresp = _call_llm_json(
        db, auditor_model, auditor_provider,
        _get_auditor_identity(db, duo_id=duo_id),
        prompt,
        required_keys=["preliminary_blame"],
        context="audit_assessment",
        dossier_id=dossier.get("id"),
        duo_id=duo_id)

    if jresp.get("parsed"):
        return {**jresp["parsed"],
                "cost": jresp.get("cost", 0),
                "tokens": jresp.get("tokens", 0)}

    return {"preliminary_blame": {}, "key_questions_for_quant": [],
            "key_questions_for_apex": [], "cost": jresp.get("cost", 0),
            "tokens": jresp.get("tokens", 0)}


# ═══════════════════════════════════════════════════════════════════════
# Phase C: Agent Interrogation (4 rounds)
# ═══════════════════════════════════════════════════════════════════════

def _build_evidence_context(evidence: Dict, dossier: Dict) -> str:
    """Build a shared evidence summary agents see during interrogation."""
    actual_entry = (dossier.get("live_entry_price")
                    or dossier.get("actual_entry_price")
                    or dossier.get("entry_price") or "?")
    actual_exit = (dossier.get("live_exit_price")
                   or dossier.get("actual_exit_price") or "N/A")
    live_margin = dossier.get("live_margin") or dossier.get("margin_usd")
    live_lev = dossier.get("live_leverage") or dossier.get("leverage")
    rpnl = dossier.get("realised_pnl") or 0
    rpnl_pct = dossier.get("realised_pnl_pct") or 0
    return (
        f"## TRADE EVIDENCE\n"
        f"Symbol: {dossier.get('symbol')} | Direction: {dossier.get('direction')} | "
        f"Outcome: {dossier.get('status')}\n"
        f"Entry: {actual_entry} | SL: {dossier.get('stop_loss')} | "
        f"TP1: {dossier.get('take_profit_1')} | Exit: {actual_exit}\n"
        f"P&L: ${rpnl} ({rpnl_pct}%) | Margin: ${live_margin} | "
        f"Leverage: {live_lev}x\n"
        f"Created: {dossier.get('created_at')} | Closed: {dossier.get('updated_at')}\n\n"
        f"## CONDITIONS\n" +
        "\n".join(f"  C{c.get('id','?')}: {c.get('description','')[:80]} — "
                  f"{c.get('status','?')}" for c in evidence.get("conditions", [])[:8])
    )


def _run_interrogation(db, report_id: int, evidence: Dict,
                       dossier: Dict, assessment: Dict,
                       duo_id: str = None) -> List[Dict]:
    """Run all 4 rounds of interrogation. Returns list of interview records."""
    interviews = []
    total_cost = 0.0
    total_tokens = 0
    _duo = duo_id
    ctx = _build_evidence_context(evidence, dossier)
    s1_readable = _humanise_analysis(evidence.get("stage1_output") or "N/A")
    s2_readable = _humanise_analysis(evidence.get("stage2_output") or "N/A")

    prob_hist = evidence.get("probability_history", [])[-5:]
    prob_readable = "\n".join(
        f"  {p.get('timestamp','?')}: {p.get('probability','?')}% — {p.get('reason','')[:80]}"
        for p in prob_hist) or "No probability history recorded"

    news = evidence.get("geo_macro_news", [])
    news_readable = "\n".join(
        f"  [{n.get('published_at','')}] {n.get('source_detail','')}: {n.get('title','')[:80]}"
        for n in news[:10]) or "No geo/macro news found during this trade window"

    # ── ROUND 1: DEFENSE (outcome-aware framing) ──
    outcome = (dossier.get("status") or "").lower()
    is_win = outcome == "won"
    pnl_str = dossier.get("realised_pnl", "?")
    opp_dir = 'SHORT' if dossier.get('direction', '').upper() == 'BUY' else 'LONG'

    if is_win:
        quant_r1 = (
            f"Here is your Stage 1 analysis:\n{s1_readable}\n\n"
            f"OUTCOME: WON (P&L: {pnl_str}). This trade was profitable.\n"
            f"1. What did your analysis get RIGHT that contributed to this win?\n"
            f"2. Was the direction call well-supported by the data you had?\n"
            f"3. Were there any aspects you got lucky on vs genuinely nailed?\n"
            f"4. Would you repeat this exact analysis approach? What would you keep?\n"
            f"5. Was there anything you almost missed that could have turned this into a loss?\n"
            f"Grade your work 1-10 with specifics.")
        apex_r1 = (
            f"Here is everything you received and your Stage 2 decision:\n"
            f"{s2_readable}\n\nOUTCOME: WON, P&L: {pnl_str}.\n"
            f"1. What was your strategic reasoning for this entry? Walk through your thought process.\n"
            f"2. How many conditions were met when you entered? Did you enter with partial conditions? "
            f"If so, what gave you conviction despite missing conditions?\n"
            f"3. Was this a calculated decision to enter early, and did it pay off as expected?\n"
            f"4. Would you repeat this strategy? Rate your confidence in replicating this 1-10.\n"
            f"5. Were your entry, SL, and TP levels optimal, or could you have extracted more profit?\n"
            f"6. Did you consider {opp_dir}? Was your directional conviction strong or marginal?\n"
            f"7. Was there a better entry via a limit order at a deeper OB/FVG level?\n"
            f"8. SL REVIEW (even for wins): Was your SL at a structural level that would "
            f"have survived a normal liquidity sweep? Did price come close to your SL before "
            f"going in your favour? If so, was the SL well-placed or did you just get lucky?\n"
            f"9. TP REVIEW: Did price hit all your TPs or could you have set them more "
            f"ambitiously? Did you leave significant profit on the table?\n"
            f"Grade your decision 1-10 with specifics.")
    else:
        quant_r1 = (
            f"Here is your Stage 1 analysis:\n{s1_readable}\n\n"
            f"The trade resulted in: {outcome}. "
            f"Defend your analysis. What data did you have? What did you get right? "
            f"What did you miss? Were you given sufficient data? Were there gaps "
            f"in the candles? Grade your own work 1-10 with specifics.")
        apex_r1 = (
            f"Here is everything you received and your Stage 2 decision:\n"
            f"{s2_readable}\n\nThe result: {outcome}, "
            f"P&L: {pnl_str}. "
            f"Defend your entry price, SL placement, and TP targets. "
            f"Were you too aggressive or conservative? Did you ignore warning signs? "
            f"Grade your decision 1-10 with specifics.\n\n"
            f"ADDITIONAL QUESTIONS YOU MUST ANSWER:\n"
            f"- Did you consider the OPPOSITE direction? What would a "
            f"{opp_dir} setup have looked like at the time of your analysis?\n"
            f"- Was there a quick M5-M15 counter-trend scalp available? "
            f"For example, a resistance rejection, overbought condition, or "
            f"strong opposing order block that could have given a profitable "
            f"5-15 minute trade in the other direction?\n"
            f"- Could you have set a limit order at a deeper OB or FVG level "
            f"for a better entry price instead of entering where you did?\n"
            f"- STOP LOSS DEEP DIVE: Was your SL placed at a structural level "
            f"(beyond an order block, beyond a liquidity pool) or was it "
            f"arbitrary? Did you check multiple timeframes (M5, M15, H1, H4) "
            f"for hidden liquidity pockets near your SL? Were there wick "
            f"clusters near your SL that indicated stop-hunting activity? "
            f"Where should the SL have been placed to survive normal market "
            f"noise while still invalidating the trade thesis if hit?\n"
            f"- TP ASSESSMENT: Were your take profits at structural levels "
            f"(swing highs/lows, OBs, FVGs) or arbitrary? Were they too "
            f"ambitious given the ATR and market structure?")

    _all_defense_questions = {
        "quant": quant_r1,
        "apex": apex_r1,
        "tracker": (f"You monitored dossier #{dossier['id']} from creation to close. "
                    f"Probability history:\n{prob_readable}\n\n"
                    f"Did you escalate warning signs? Were your condition checks accurate? "
                    f"Should you have recommended abandoning earlier?"),
        "geo": (f"During this trade ({dossier.get('created_at')} to {dossier.get('updated_at')}), "
                f"the following news events were published:\n{news_readable}\n\n"
                f"Based ONLY on the events listed above, did you flag any of these? "
                f"Should you have flagged them earlier? If the list is empty or says "
                f"'No geo/macro news found', state that no events were available to you "
                f"and do NOT invent or guess what might have happened."),
        "macro": (f"During this trade window, the following macro events were recorded:\n"
                  f"{news_readable}\n\n"
                  f"Based ONLY on the events listed above, was there a macro event that "
                  f"should have prevented or altered this trade? If the list is empty or "
                  f"says 'No geo/macro news found', state clearly that you had no macro "
                  f"data for this window. Do NOT speculate about what 'could have' or "
                  f"'likely' happened. Only reference events that are explicitly listed above."),
    }
    _defense_list_raw = get_system_config(
        db, "auditor_defense_agents", "quant,apex,tracker,geo,macro")
    _defense_agents = [a.strip() for a in _defense_list_raw.split(",") if a.strip()]
    agents_to_interview = [
        (a, _all_defense_questions.get(a, f"You are being interviewed about dossier #{dossier['id']}. "
                                          f"Describe your role and what you contributed."))
        for a in _defense_agents
    ]

    did = dossier.get("id")
    for agent_id, question in agents_to_interview:
        resp = _interview_agent(db, agent_id, question, ctx, dossier_id=did, duo_id=_duo)
        rec = _save_interview(db, report_id, 1, "defense", agent_id,
                              question, resp)
        interviews.append(rec)
        total_cost += resp.get("cost", 0)
        total_tokens += resp.get("tokens", 0)

    # ── ROUND 2: CROSS-EXAMINATION ──
    quant_defense = interviews[0]["response"][:1000] if interviews else ""
    apex_defense = interviews[1]["response"][:1000] if len(interviews) > 1 else ""

    _cross_list_raw = get_system_config(
        db, "auditor_cross_exam_agents", "apex,quant")
    _cross_agents = [a.strip() for a in _cross_list_raw.split(",") if a.strip()]
    _all_cross_questions = {
        "apex": (f"Here is EVERYTHING Quant provided in the dossier. "
                 f"Quant's defense: '{quant_defense}'\n\n"
                 f"Review Quant's work critically. Was the Stage 1 analysis accurate? "
                 f"Was anything missing that would have changed your decision? "
                 f"Grade Quant's work 1-10 with specifics.\n\n"
                 f"Also answer: Looking at this trade in hindsight, what was the SINGLE "
                 f"BEST trade on {dossier.get('symbol')} during this time window? "
                 f"Consider both directions. Was the chosen direction truly the best?"),
        "quant": (f"Here is the full dossier you built and what actually happened. "
                  f"Apex's defense: '{apex_defense}'\n\n"
                  f"Was your data sufficient? Were there charts or indicators you SHOULD "
                  f"have included but didn't? Was the volume analysis adequate? "
                  f"Grade your own dossier completeness 1-10.\n\n"
                  f"Also: Did your Stage 1 analysis present both bullish AND bearish "
                  f"scenarios clearly enough for Apex to evaluate both directions?"),
    }
    cross_exams = [
        (a, _all_cross_questions.get(a, f"Cross-examine your colleagues about dossier #{dossier['id']}."))
        for a in _cross_agents
    ]

    for agent_id, question in cross_exams:
        resp = _interview_agent(db, agent_id, question, ctx, dossier_id=did, duo_id=_duo)
        rec = _save_interview(db, report_id, 2, "cross_examination",
                              agent_id, question, resp)
        interviews.append(rec)
        total_cost += resp.get("cost", 0)
        total_tokens += resp.get("tokens", 0)

    # ── ROUND 3: SELF-IMPROVEMENT ──
    self_q = (
        "Based on this trade and its outcome, what SPECIFIC changes would you "
        "recommend to:\n"
        "1. Your own prompt/instructions (be exact, show edits)\n"
        "2. Your data inputs (too much? too little? wrong timeframe?)\n"
        "3. Your decision thresholds or parameters\n"
        "4. Your soul/identity (any behavioural weakness?)\n"
        "5. The data you receive from other agents\n"
        "6. System parameters (SL defaults, TP splits, etc.)\n"
        "7. DIRECTION ASSESSMENT: Should you have considered both directions more "
        "carefully? Should there be a mandatory opposite-direction check before "
        "committing to a trade?\n"
        "8. SCALP AWARENESS: Should you actively look for M5-M15 counter-trend "
        "scalps alongside your main trade idea? If a resistance/support rejection "
        "or overbought/oversold condition was visible, should you have flagged a "
        "quick scalp opportunity?\n"
        "9. ENTRY OPTIMIZATION: Should you use limit orders at deeper OB/FVG levels "
        "more aggressively instead of market entries?\n"
        "10. SL PLACEMENT MASTERY: Was the stop loss placed at a structural level "
        "(beyond an order block, beyond a liquidity pool, beyond wick clusters)? "
        "Looking at multiple timeframes (M5, M15, H1, H4), where should the SL "
        "have been to give the trade room to breathe while still being meaningful? "
        "Did you check for hidden liquidity pockets or stop-hunt zones?\n"
        "11. ATR SUFFICIENCY: Was the ATR timeframe and candle count sufficient "
        "for your SL calculation? Should you have cross-referenced M5, M15, H1, "
        "and H4 ATR values before deciding the SL distance? Which timeframe's ATR "
        "was most relevant for this trade's horizon?\n"
        "12. TP PLACEMENT: Were take profits at structural levels (previous swing "
        "highs/lows, order blocks, FVGs, session levels) or were they arbitrary? "
        "Should you have been more conservative or more ambitious?\n"
        "13. CROWD SL AWARENESS: Did you notice where signal providers and mentors "
        "placed their stops for this symbol? Were your stops clustered with theirs "
        "(making you a target for market makers) or sensibly separated?\n\n"
        "Be BRUTALLY specific. Not 'improve analysis' but 'add RSI divergence "
        "check on M15 before confirming bullish entries'. Reference specific SMC "
        "concepts, timeframes, and decision points."
    )

    _self_list_raw = get_system_config(
        db, "auditor_self_improvement_agents", "quant,apex,tracker")
    _self_agents = [a.strip() for a in _self_list_raw.split(",") if a.strip()]
    for agent_id in _self_agents:
        resp = _interview_agent(db, agent_id, self_q, ctx, dossier_id=did, duo_id=_duo)
        rec = _save_interview(db, report_id, 3, "self_improvement",
                              agent_id, self_q, resp)
        interviews.append(rec)
        total_cost += resp.get("cost", 0)
        total_tokens += resp.get("tokens", 0)

    # ── ROUND 4: REBUTTAL (if blame disputed) ──
    apex_cross = next((i for i in interviews
                       if i["phase"] == "cross_examination"
                       and i["interviewee"] == "apex"), None)
    if apex_cross and ("quant" in apex_cross.get("response", "").lower()
                       or "data" in apex_cross.get("response", "").lower()):
        rebuttal_q = (
            f"Apex said this about your work: "
            f"'{apex_cross['response'][:500]}'\n\n"
            f"Respond to their criticism. Do you accept or reject it?"
        )
        resp = _interview_agent(db, "quant", rebuttal_q, ctx, dossier_id=did, duo_id=_duo)
        rec = _save_interview(db, report_id, 4, "rebuttal", "quant",
                              rebuttal_q, resp)
        interviews.append(rec)
        total_cost += resp.get("cost", 0)
        total_tokens += resp.get("tokens", 0)

    return interviews, total_cost, total_tokens


def _save_interview(db, report_id: int, round_num: int, phase: str,
                    interviewee: str, question: str, resp: Dict) -> Dict:
    """Persist a single interview exchange."""
    db.execute("""
        INSERT INTO audit_interviews
        (audit_report_id, round_number, phase, interviewer, interviewee,
         question, response, model_used, tokens_used, cost)
        VALUES (%s, %s, %s, 'ledger', %s, %s, %s, %s, %s, %s)
    """, (report_id, round_num, phase, interviewee,
          question[:5000], (resp.get("text") or "")[:10000],
          resp.get("model", ""), resp.get("tokens", 0),
          resp.get("cost", 0)))

    return {
        "round": round_num,
        "phase": phase,
        "interviewee": interviewee,
        "question": question[:2000],
        "response": (resp.get("text") or "")[:5000],
        "model": resp.get("model", ""),
        "cost": resp.get("cost", 0),
    }


# ═══════════════════════════════════════════════════════════════════════
# Phase D: Verdict & Recommendations
# ═══════════════════════════════════════════════════════════════════════

VERDICT_PROMPT = """You are Ledger, Chief Auditor. You have completed your investigation.

## TRADE
Symbol: {symbol} | Direction: {direction} | Outcome: {outcome} | P&L: {pnl}

## INTERVIEW TRANSCRIPTS
{interview_summary}

## FRESH TA vs ORIGINAL
{ta_comparison}

## MULTI-TIMEFRAME ATR (for SL quality analysis)
{atr_context}

## ACTUAL AGENT PROMPTS (what they currently have)
{agent_prompts}

Produce your final verdict. Go DEEP. Do not just report whether conditions were met. Analyse the QUALITY of the trade, the direction choice, the entry precision, the stop loss placement, the take profit targets, and what the optimal trade would have been.

Respond with ONLY this JSON:
{{
  "root_cause": "<single most important reason for the outcome>",
  "contributing_factors": ["<factor 1>", "<factor 2>"],
  "blame_assignment": {{
    "quant": <0-100>,
    "apex": <0-100>,
    "tracker": <0-100>,
    "geo_macro": <0-100>,
    "data_quality": <0-100>,
    "market": <0-100>
  }},
  "severity": "<critical|major|minor|no_fault>",
  "summary": "<2-3 paragraph plain English summary of what happened and why>",
  "lessons": ["<lesson 1>", "<lesson 2>", "<lesson 3>"],
  "direction_analysis": {{
    "chosen_direction": "{direction}",
    "was_correct": <true|false>,
    "opposite_would_have_profited": <true|false>,
    "opposite_setup_description": "<If the opposite direction had a valid setup, describe it: entry zone, SL, TP, and what SMC signals supported it>",
    "short_term_counter_scalp": "<Was there a profitable M5-M15 counter-trend scalp available? Describe it or state none visible>"
  }},
  "entry_quality": {{
    "score": <1-10>,
    "actual_entry": "{entry}",
    "optimal_entry": "<where the best entry would have been based on the candle data>",
    "improvement_available": "<how many points/pips better the optimal entry was>",
    "limit_order_possible": "<could a limit order at a deeper OB/FVG have been used? describe>",
    "notes": "<explain why the entry was good or poor>"
  }},
  "smc_review": {{
    "score": <1-10>,
    "amd_phase_correct": <true|false>,
    "liquidity_sweep_identified": <true|false>,
    "displacement_confirmed": <true|false>,
    "ob_fvg_entry": <true|false>,
    "missed_elements": ["<SMC element Apex missed>"],
    "notes": "<detailed review of how well Apex applied the ICT/SMC framework>"
  }},
  "methodology_fit": {{
    "score": <1-10>,
    "regime_at_entry": "<trending|ranging|choppy|volatile>",
    "smc_was_appropriate": <true|false>,
    "alternative_framework": "<what would have worked better, or null>",
    "reasoning": "<why>"
  }},
  "sl_quality": {{
    "score": <1-10>,
    "actual_sl": "{sl}",
    "optimal_sl": "<where the SL should have been based on structure, OBs, liquidity zones>",
    "distance_pct": "<actual SL distance as % from entry>",
    "atr_comparison": "<was the SL distance sensible relative to the ATR values above? which TF's ATR was most relevant?>",
    "structural_placement": "<was SL beyond an order block? beyond a liquidity pool? or in no-man's land?>",
    "was_swept": <true|false>,
    "sweep_reversed": "<if swept, did price reverse immediately after (stop hunt) or continue (genuine invalidation)?>",
    "notes": "<detailed analysis of SL quality — was it smart, tight, arbitrary, or structural?>"
  }},
  "tp_quality": {{
    "score": <1-10>,
    "actual_tps": "<TP1, TP2, TP3 that Apex set>",
    "optimal_tps": "<where TPs should have been based on structure: previous swing H/L, OBs, FVGs, session levels>",
    "profit_left_on_table": "<if trade won, how much more could have been captured?>",
    "tp_never_reached": "<if trade lost, were TPs too ambitious? what % was needed?>",
    "notes": "<were TPs at logical structural levels or arbitrary?>"
  }},
  "position_placement_review": {{
    "overall_score": <1-10>,
    "entry_vs_optimal": "<how far was Apex's entry from the optimal? e.g. '0.3% worse than OB entry'>",
    "sl_vs_optimal": "<how far was Apex's SL from the structural optimal?>",
    "tp_vs_optimal": "<how far were Apex's TPs from structural levels?>",
    "classification": "<learning_point|statistical_noise|critical_insight>",
    "learning_summary": "<if learning_point or critical_insight: what specific lesson should Apex internalise for future trades on this symbol or setup type? if statistical_noise: why is this just normal market variance?>"
  }},
  "optimal_trade": {{
    "direction": "<BUY or SELL>",
    "entry": "<optimal entry price>",
    "stop_loss": "<optimal SL — must be at a structural level, not arbitrary>",
    "take_profit_1": "<optimal TP1 — at a structural level>",
    "rationale": "<why this was the best trade on this symbol during this window, using both directions>"
  }},
  "scalp_missed": {{
    "opportunities_count": <0-5>,
    "best_scalp": "<describe the highest-probability M5-M15 scalp that was visible: direction, entry, SL, TP, timeframe>",
    "notes": "<were there resistance rejections, overbought/oversold conditions, or strong OBs that gave quick counter-trend moves?>"
  }},
  "recommendations": [
    {{
      "category": "<prompt_change|soul_change|parameter_change|threshold_change|data_source_change|process_change>",
      "target_agent": "<agent_id>",
      "target_field": "<soul or identity_prompt>",
      "current_value": "<EXACT quote from the agent's soul or identity_prompt shown above>",
      "proposed_value": "<exact replacement text to swap in>",
      "evidence": "<which interview rounds support this>",
      "business_justification": "<why this improves profitability>",
      "expected_impact": "<measurable improvement>",
      "severity": "<critical|important|suggestion>"
    }}
  ],
  "what_went_right": ["<positive 1>", "<positive 2>"],
  "apex_strategic_rationale": {{
    "entered_with_partial_conditions": <true|false>,
    "conditions_met_vs_total": "<e.g. '3/4' or 'all 5/5'>",
    "override_reasoning": "<If Apex entered with fewer than all conditions met, explain the strategic rationale — what gave conviction? If all conditions were met, state 'Full conditions met'>",
    "would_repeat": <true|false>,
    "confidence_in_approach": <1-10>,
    "strategic_insight": "<What did Apex learn about this type of setup? What pattern or technique was tried? Was it innovative or conventional?>"
  }},
  "lesson_attribution": {{
    "win_due_to_prior_lesson": <true|false>,
    "referenced_lesson": "<If win was due to a prior lesson, describe which lesson and what improvement it represented. If loss, state 'N/A'>",
    "improvement_trajectory": "<Is the system getting better at this symbol? What trend do you see across recent trades?>",
    "recommended_lesson_window": "<Based on the value of lessons in this audit, recommend how many recent wins and losses the system should reference: e.g. 'top 25 wins + top 25 losses is sufficient' or 'increase to 40+40 -- many lessons are still relevant'>"
  }}
}}

RULES:
- blame_assignment values MUST total exactly 100
- 'market' blame capped at 30 unless genuine black swan
- Recommendations MUST be SYSTEM-LEVEL improvements, not trade-specific. Conditions (C1, C2, etc.) are generated dynamically per trade and cannot be changed.
- current_value MUST be an EXACT quote copied from the agent's soul or identity_prompt shown above. Do NOT fabricate or paraphrase. If you cannot find a specific text to change, set current_value to "" and proposed_value to the new text to APPEND.
- proposed_value must be a general instruction that improves ALL future trades, not just this one.
- For prompt_change or soul_change: target_field must be "soul" or "identity_prompt".
- For parameter_change, threshold_change, process_change, data_source_change: current_value and proposed_value should describe the system behaviour change needed (these require manual implementation).
- Do NOT reference specific condition numbers (C1, C2, etc.) or specific trade prices in recommendations.
- Recommendations must reference specific SMC concepts, timeframes, or decision points. "Improve analysis" is NOT acceptable. "Add M15 displacement check before confirming bullish entries when H1 RSI is above 70" IS acceptable.
- direction_analysis and optimal_trade are MANDATORY. You must always assess whether the opposite direction or a counter-trend scalp would have worked.
- entry_quality, sl_quality, tp_quality, and smc_review scores should be harsh and honest. A score of 7+ means genuinely excellent work.
- methodology_fit: Evaluate whether ICT/SMC was the RIGHT framework for this market regime. A ranging market with no clear liquidity sweeps may not suit SMC at all. Score low if the methodology was force-fit to conditions that favoured mean-reversion or momentum-only strategies.
- sl_quality: Evaluate WHERE the SL was placed structurally, not just its distance. A tight SL at a perfect structural level scores higher than a wide SL in no-man's land.
- position_placement_review.classification: Use "learning_point" when the placement reveals a repeatable mistake Apex can learn from. Use "critical_insight" for fundamental strategic errors. Use "statistical_noise" when the outcome was simply market variance and the placement was reasonable.
- apex_strategic_rationale is MANDATORY. Capture whether Apex entered with partial conditions and WHY. For wins, this is especially valuable — we want to know if a calculated early entry was the right call. For losses, we want to know if entering with partial conditions contributed to the failure. This field helps the CEO track whether flexibility in condition requirements is helping or hurting performance."""


def _run_verdict(db, report_id: int, dossier: Dict, evidence: Dict,
                 interviews: List[Dict], duo_id: str = None) -> Dict:
    """Phase D: Auditor synthesises everything into verdict + recommendations."""
    interview_text = "\n\n".join(
        f"--- {i['interviewee'].upper()} (Round {i['round']}: {i['phase']}) ---\n"
        f"Q: {i['question'][:500]}\n"
        f"A: {i['response'][:1500]}"
        for i in interviews)

    fresh = evidence.get("fresh_ta", {})
    ta_cmp = f"Fresh TA:\n{_json_to_prose(fresh)}" if fresh else "N/A"

    agent_prompt_sections = []
    for aid in ("quant", "apex", "tracker", "geo", "macro"):
        profile = _get_agent_profile(db, aid)
        if profile:
            soul = (profile.get("soul") or "")[:2000]
            identity = (profile.get("identity_prompt") or "")[:1000]
            agent_prompt_sections.append(
                f"--- {aid.upper()} ---\n"
                f"[identity_prompt]: {identity}\n"
                f"[soul]: {soul}")
    agent_prompts_text = "\n\n".join(agent_prompt_sections) or "No agent profiles available"

    # Multi-TF ATR for verdict SL quality analysis
    atr_data = evidence.get("multi_tf_atr", {})
    atr_lines = []
    for tf in ("M5", "M15", "H1", "H4"):
        d = atr_data.get(tf, {})
        if d.get("value"):
            atr_lines.append(
                f"  {tf} ATR(14) = {d['value']:.6f} "
                f"({d.get('pct_of_price', 0):.3f}% of price, {d.get('volatility','?')})")
    atr_context = "\n".join(atr_lines) if atr_lines else "ATR data unavailable"

    verdict_template = _load_auditor_prompt(
        db, "auditor_verdict_prompt", VERDICT_PROMPT, duo_id=duo_id)
    actual_entry_v = (dossier.get("live_entry_price")
                      or dossier.get("actual_entry_price")
                      or dossier.get("entry_price") or "?")
    actual_exit_v = (dossier.get("live_exit_price")
                     or dossier.get("actual_exit_price") or "N/A")
    live_margin_v = dossier.get("live_margin") or dossier.get("margin_usd")
    live_lev_v = dossier.get("live_leverage") or dossier.get("leverage")

    exec_detail = ""
    if live_margin_v:
        exec_detail = (f" | Margin=${live_margin_v}, Leverage={live_lev_v}x, "
                       f"Exit={actual_exit_v}")
    prompt = verdict_template.format(
        symbol=dossier.get("symbol", "?"),
        direction=dossier.get("direction", "?"),
        outcome=dossier.get("status", "?"),
        entry=actual_entry_v,
        sl=dossier.get("stop_loss", "?"),
        pnl=f"{dossier.get('realised_pnl', 0)}{exec_detail}",
        interview_summary=interview_text[:6000],
        ta_comparison=ta_cmp,
        atr_context=atr_context,
        agent_prompts=agent_prompts_text,
    )

    auditor_model, auditor_provider = _get_auditor_model_and_provider(db)
    jresp = _call_llm_json(
        db, auditor_model, auditor_provider,
        _get_auditor_identity(db, duo_id=duo_id),
        prompt,
        required_keys=["root_cause", "blame_assignment"],
        context="audit_verdict",
        dossier_id=dossier.get("id"),
        duo_id=duo_id)
    resp = {"text": jresp.get("text", ""), "cost": jresp.get("cost", 0),
            "tokens": jresp.get("tokens", 0), "model": jresp.get("model", "")}

    if jresp.get("error") or not jresp.get("parsed"):
        err = jresp.get("error") or "empty parsed verdict"
        logger.error(f"[Auditor] Verdict LLM JSON failed: {err}")
        return {
            "root_cause": "Verdict JSON parse failed after retries",
            "blame_assignment": {},
            "recommendations": [],
            "summary": f"[verdict_parse_failed] {err}"[:2000],
            "severity": "critical",
            "verdict_parse_failed": True,
            "cost": resp["cost"],
            "tokens": resp["tokens"],
        }

    verdict = jresp.get("parsed") or {}
    verdict.pop("verdict_parse_failed", None)

    blame_raw = verdict.get("blame_assignment")
    ok_blame, _tot, blame_msg = _validate_blame_assignment(blame_raw)
    if not ok_blame and isinstance(blame_raw, dict) and blame_raw:
        logger.warning(
            f"[Auditor] blame_assignment invalid ({blame_msg}) — normalizing to ~100")
        normalized = _normalize_blame_assignment(blame_raw)
        if normalized and isinstance(normalized, dict):
            verdict["blame_assignment"] = normalized
        else:
            verdict["blame_assignment"] = {
                "quant": 20, "apex": 20, "tracker": 20,
                "geo_macro": 20, "data_quality": 10, "market": 10,
            }

    summary_text = verdict.get("summary") or ""
    sym = dossier.get("symbol") or ""
    if _forex_hallucination_in_summary(summary_text, sym):
        logger.warning(
            f"[Auditor] Verdict summary references forex-style pairs; trade symbol {sym}")
        verdict["summary"] = (
            "[AUDIT: verify summary — forex pair mention on USDT trade] "
            + summary_text)[:8000]

    # Store raw recommendations in audit_reports.systemic_recs (NOT audit_recommendations).
    # Individual per-trade recs are raw material for the daily synthesis job, which
    # combines patterns across all audits into holistic, actionable soul changes.
    raw_recs = verdict.get("recommendations", [])
    if raw_recs:
        try:
            db.execute(
                "UPDATE audit_reports SET systemic_recs = %s WHERE id = %s",
                (json.dumps(raw_recs, default=str)[:10000], report_id))
        except Exception as e:
            logger.error(f"[Auditor] Save systemic_recs: {e}")

    verdict["cost"] = resp.get("cost", 0)
    verdict["tokens"] = resp.get("tokens", 0)
    return verdict


# ═══════════════════════════════════════════════════════════════════════
# Main Entry Point: Run Post-Mortem
# ═══════════════════════════════════════════════════════════════════════

def run_full_postmortem(db, config, dossier_id: int) -> Optional[int]:
    """Run the complete 4-phase post-mortem on a single dossier.
    Returns the audit_report ID or None on failure."""
    existing = db.fetch_one(
        "SELECT id FROM audit_reports WHERE dossier_id = %s "
        "AND report_type = 'trade_postmortem' "
        "AND status IN ('in_progress', 'completed', 'failed')", (dossier_id,))
    if existing:
        logger.info(f"[Auditor] Postmortem already exists for #{dossier_id} "
                    f"(report #{existing['id']}), skipping duplicate")
        return existing["id"]

    dossier = db.fetch_one(
        "SELECT * FROM trade_dossiers WHERE id = %s", (dossier_id,))
    if not dossier:
        logger.error(f"[Auditor] Dossier #{dossier_id} not found")
        return None

    # Prefer live exchange P&L over paper simulation when available
    if dossier.get("live_pnl") is not None and dossier.get("realised_pnl") is None:
        dossier["realised_pnl"] = dossier["live_pnl"]
        dossier["realised_pnl_pct"] = dossier.get("live_pnl_pct")
        logger.info(f"[Auditor] Using live P&L for #{dossier_id}: "
                    f"${dossier['live_pnl']}")

    logger.info(f"[Auditor] Starting post-mortem for dossier #{dossier_id} "
                f"({dossier['symbol']} — {dossier['status']})")

    # Create report record (execute_returning_id returns LAST_INSERT_ID, not rowcount)
    # Store actual exchange prices in audit record, not paper targets
    audit_entry = (dossier.get("live_entry_price")
                   or dossier.get("actual_entry_price")
                   or dossier.get("entry_price"))
    audit_exit = (dossier.get("live_exit_price")
                  or dossier.get("actual_exit_price")
                  or dossier.get("exit_price"))
    duo_id = dossier.get("duo_id") or "unknown"
    report_id = db.execute_returning_id("""
        INSERT INTO audit_reports
        (report_type, dossier_id, symbol, trade_direction, trade_outcome,
         entry_price, exit_price, stop_loss, trade_opened_at, trade_closed_at,
         duo_id, status)
        VALUES ('trade_postmortem', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'in_progress')
    """, (
        dossier_id, dossier.get("symbol"), dossier.get("direction"),
        dossier.get("status"),
        audit_entry, audit_exit,
        dossier.get("stop_loss"),
        dossier.get("created_at"), dossier.get("updated_at"),
        duo_id,
    ))

    auditor_model, _auditor_prov = _get_auditor_model_and_provider(db)
    logger.info(f"[Auditor] Using Ledger's model: {auditor_model} ({_auditor_prov})")

    total_cost = 0.0
    total_tokens = 0

    try:
        # Phase A: Evidence
        logger.info(f"[Auditor] #{dossier_id} Phase A: Collecting evidence")
        evidence = _collect_evidence(db, dossier)

        # Phase B: Independent Assessment
        logger.info(f"[Auditor] #{dossier_id} Phase B: Independent assessment")
        assessment = _run_independent_assessment(db, evidence, dossier, duo_id=duo_id)
        total_cost += assessment.get("cost", 0)
        total_tokens += assessment.get("tokens", 0)

        # Phase C: Interrogation
        logger.info(f"[Auditor] #{dossier_id} Phase C: Agent interrogation")
        interviews, int_cost, int_tokens = _run_interrogation(
            db, report_id, evidence, dossier, assessment, duo_id=duo_id)
        total_cost += int_cost
        total_tokens += int_tokens

        # Phase D: Verdict
        logger.info(f"[Auditor] #{dossier_id} Phase D: Verdict & recommendations")
        verdict = _run_verdict(db, report_id, dossier, evidence, interviews, duo_id=duo_id)
        total_cost += verdict.get("cost", 0)
        total_tokens += verdict.get("tokens", 0)

        cross_duo_text = _cross_duo_comparison(db, dossier, duo_id)

        if verdict.get("verdict_parse_failed"):
            db.execute("""
                UPDATE audit_reports SET
                    root_cause = %s,
                    blame_assignment = %s,
                    severity = %s,
                    auditor_summary = %s,
                    evidence_json = %s,
                    stage1_snapshot = %s,
                    stage2_snapshot = %s,
                    tracker_snapshot = %s,
                    fresh_ta_report = %s,
                    geo_macro_during = %s,
                    interview_transcript = %s,
                    interview_count = %s,
                    pnl_amount = %s,
                    pnl_pct = %s,
                    model_used = %s,
                    total_cost = %s,
                    total_tokens = %s,
                    cross_duo_analysis = %s,
                    status = 'failed',
                    completed_at = NOW()
                WHERE id = %s
            """, (
                verdict.get("root_cause", ""),
                json.dumps(verdict.get("blame_assignment") or {}, default=str),
                verdict.get("severity", "critical"),
                verdict.get("summary", ""),
                json.dumps({"conditions": evidence.get("conditions"),
                            "prob_history": evidence.get("probability_history")},
                           default=str)[:50000],
                (evidence.get("stage1_output") or "")[:20000],
                (evidence.get("stage2_output") or "")[:20000],
                json.dumps(evidence.get("tracker_conversation", []),
                           default=str)[:20000],
                json.dumps(evidence.get("fresh_ta", {}), default=str)[:20000],
                json.dumps(evidence.get("geo_macro_news", []), default=str)[:10000],
                json.dumps([{k: v for k, v in i.items() if k != "cost"}
                            for i in interviews], default=str)[:50000],
                len(interviews),
                dossier.get("realised_pnl"),
                dossier.get("realised_pnl_pct"),
                auditor_model,
                round(total_cost, 4),
                total_tokens,
                cross_duo_text,
                report_id,
            ))
            logger.warning(
                f"[Auditor] Post-mortem #{report_id} marked failed — verdict JSON parse error")
            return report_id

        blame = verdict.get("blame_assignment", {})
        recs = verdict.get("recommendations", [])

        db.execute("""
            UPDATE audit_reports SET
                root_cause = %s,
                blame_assignment = %s,
                severity = %s,
                auditor_summary = %s,
                evidence_json = %s,
                stage1_snapshot = %s,
                stage2_snapshot = %s,
                tracker_snapshot = %s,
                fresh_ta_report = %s,
                geo_macro_during = %s,
                interview_transcript = %s,
                interview_count = %s,
                pnl_amount = %s,
                pnl_pct = %s,
                model_used = %s,
                total_cost = %s,
                total_tokens = %s,
                cross_duo_analysis = %s,
                status = 'completed',
                completed_at = NOW()
            WHERE id = %s
        """, (
            verdict.get("root_cause", ""),
            json.dumps(blame, default=str),
            verdict.get("severity", "minor"),
            verdict.get("summary", ""),
            json.dumps({"conditions": evidence.get("conditions"),
                        "prob_history": evidence.get("probability_history")},
                       default=str)[:50000],
            (evidence.get("stage1_output") or "")[:20000],
            (evidence.get("stage2_output") or "")[:20000],
            json.dumps(evidence.get("tracker_conversation", []),
                       default=str)[:20000],
            json.dumps(evidence.get("fresh_ta", {}), default=str)[:20000],
            json.dumps(evidence.get("geo_macro_news", []), default=str)[:10000],
            json.dumps([{k: v for k, v in i.items() if k != "cost"}
                        for i in interviews], default=str)[:50000],
            len(interviews),
            dossier.get("realised_pnl"),
            dossier.get("realised_pnl_pct"),
            auditor_model,
            round(total_cost, 4),
            total_tokens,
            cross_duo_text,
            report_id,
        ))

        logger.info(f"[Auditor] Post-mortem #{report_id} completed for "
                    f"dossier #{dossier_id}: {verdict.get('severity','?')} | "
                    f"Root: {verdict.get('root_cause','?')[:80]} | "
                    f"Recs: {len(recs)} | Cost: ${total_cost:.4f}")

        # ── Extract lessons into trade_lessons for Symbol Knowledge Bank ──
        _extract_lessons_to_trade_lessons(db, dossier, verdict, auditor_model, total_cost)

        # ── Vectorize audit verdict for RAG retrieval ──
        _vectorize_audit_verdict(db, report_id, dossier, verdict)

        # ── Vectorize the full dossier AFTER postmortem is complete ──
        # Moved here from trading_floor._trigger_postmortem so the vector
        # store includes postmortem findings, lessons, and root cause analysis.
        try:
            from services.trading_floor import _vectorize_dossier
            _vectorize_dossier(db, dossier_id)
        except Exception as vec_e:
            logger.debug(f"[Auditor] Post-postmortem vectorization error: {vec_e}")

        return report_id

    except Exception as e:
        logger.error(f"[Auditor] Post-mortem failed for #{dossier_id}: {e}",
                     exc_info=True)
        db.execute("""
            UPDATE audit_reports SET status = 'failed',
                   auditor_summary = %s, completed_at = NOW()
            WHERE id = %s
        """, (f"FAILED: {str(e)[:500]}", report_id))
        return None


def _extract_lessons_to_trade_lessons(db, dossier: dict, verdict: dict,
                                      model_used: str, total_cost: float):
    """After a postmortem verdict, persist each lesson into trade_lessons
    so the Symbol Knowledge Bank can feed them into future dossier builds."""
    lessons = verdict.get("lessons", [])
    if not lessons:
        logger.debug(f"[Auditor] No lessons to extract for dossier #{dossier.get('id')}")
        return

    dossier_id = dossier.get("id")
    symbol = dossier.get("symbol")
    status = (dossier.get("status") or "").lower()
    outcome_map = {"won": "WIN", "lost": "LOSS", "abandoned": "LOSS",
                   "expired": "BREAKEVEN"}
    outcome = outcome_map.get(status, "LOSS")

    optimal = verdict.get("optimal_trade", {})
    optimal_summary = ""
    if optimal:
        optimal_summary = (
            f"Direction: {optimal.get('direction','?')}, "
            f"Entry: {optimal.get('entry','?')}, SL: {optimal.get('stop_loss','?')}, "
            f"TP1: {optimal.get('take_profits',['?'])[0] if optimal.get('take_profits') else '?'}, "
            f"Reasoning: {optimal.get('reasoning','')[:500]}")

    what_worked = None
    what_failed = None
    if outcome == "WIN":
        what_worked = "; ".join(lessons)
    else:
        what_failed = "; ".join(lessons)

    # Build confidence calibration string for this trade
    conf_score = dossier.get("confidence_score")
    cal_text = None
    if conf_score is not None:
        cal_text = (f"Confidence {conf_score} vs outcome {outcome}. "
                    f"PnL: {dossier.get('realised_pnl', '?')} USD.")

    stored = 0
    for lesson_text in lessons:
        if not lesson_text or not isinstance(lesson_text, str):
            continue
        try:
            db.insert_lesson({
                "dossier_id": dossier_id,
                "signal_id": None,
                "symbol": symbol,
                "account_id": "jarvais",
                "model_used": model_used,
                "outcome": outcome,
                "pnl_usd": dossier.get("realised_pnl"),
                "what_worked": what_worked,
                "what_failed": what_failed,
                "lesson_text": lesson_text[:5000],
                "root_cause": (verdict.get("root_cause") or "")[:2000],
                "optimal_trade_summary": optimal_summary[:2000] if optimal_summary else None,
                "confidence_calibration": cal_text,
                "full_post_mortem": json.dumps(verdict, default=str)[:50000],
                "api_cost_usd": round(total_cost / max(len(lessons), 1), 6),
            })
            stored += 1
        except Exception as e:
            logger.error(f"[Auditor] Failed to store lesson for dossier #{dossier_id}: {e}")

    # Also update trade_dossiers.lessons_learned with the combined lessons
    if stored > 0:
        try:
            lessons_json = json.dumps(lessons, default=str)[:10000]
            db.execute(
                "UPDATE trade_dossiers SET lessons_learned = %s WHERE id = %s",
                (lessons_json, dossier_id))
        except Exception as e:
            logger.debug(f"[Auditor] Could not update dossier lessons_learned: {e}")

    logger.info(f"[Auditor] Extracted {stored} lessons for dossier #{dossier_id} "
                f"({symbol} {outcome})")


def _vectorize_audit_verdict(db, report_id: int, dossier: dict, verdict: dict):
    """Queue the audit verdict for vectorization into trade_memory collection
    so future dossier builds can retrieve relevant audit insights via RAG."""
    try:
        symbol = dossier.get("symbol", "")
        parts = [
            f"Audit verdict for dossier #{dossier.get('id')} ({symbol} "
            f"{dossier.get('direction','')} — {dossier.get('status','')})",
            f"Root cause: {verdict.get('root_cause', 'unknown')}",
            f"Severity: {verdict.get('severity', '?')}",
            f"Summary: {verdict.get('summary', '')[:1000]}",
        ]

        lessons = verdict.get("lessons", [])
        if lessons:
            parts.append(f"Lessons: {'; '.join(str(l) for l in lessons)}")

        recs = verdict.get("recommendations", [])
        if recs:
            parts.append(f"Recommendations: {'; '.join(str(r) for r in recs[:5])}")

        optimal = verdict.get("optimal_trade", {})
        if optimal:
            parts.append(f"Optimal trade: {optimal.get('direction','?')} "
                         f"entry={optimal.get('entry','?')} "
                         f"SL={optimal.get('stop_loss','?')}")

        blame = verdict.get("blame_assignment", {})
        if blame:
            top_blame = sorted(blame.items(), key=lambda x: x[1], reverse=True)[:3]
            parts.append(f"Blame: {', '.join(f'{k}={v}%' for k,v in top_blame)}")

        vec_text = "\n".join(parts)[:30000]

        from services.vectorization_worker import queue_for_vectorization
        queue_for_vectorization(db, "audit_reports", report_id, "trade_memory", vec_text, {
            "symbol": symbol,
            "direction": dossier.get("direction", ""),
            "status": dossier.get("status", ""),
            "severity": verdict.get("severity", ""),
            "root_cause": (verdict.get("root_cause") or "")[:200],
            "type": "audit_verdict",
            "dossier_id": dossier.get("id"),
        })
        logger.info(f"[Auditor] Audit #{report_id} queued for vectorization ({symbol})")
    except Exception as e:
        logger.debug(f"[Auditor] Audit vectorization error #{report_id}: {e}")


_last_synthesis_audit_count = 0

# ═══════════════════════════════════════════════════════════════════════
# Shadow Trade Evaluation (Counterfactual P&L for do_not_trade decisions)
# ═══════════════════════════════════════════════════════════════════════

def evaluate_shadow_trades(db, config):
    """BillNye candle evaluation of shadow trades.
    For each pending shadow with entry/SL/TP, checks candle data to determine
    what would have happened if Apex had taken the trade.
    Also backfills counterfactual_pnl for exchange-placed shadows that are
    already closed, so we can compare forecast vs actual side-by-side.
    Zero LLM cost — purely computational using candle highs/lows."""
    if get_system_config(db, "shadow_trade_tracking_enabled", "true").lower() != "true":
        return

    expiry_hours = get_system_config_int(db, "shadow_expiry_hours", 24)

    exit_mode = "tp1_full"
    try:
        row = db.fetch_one(
            "SELECT config_value FROM shadow_config WHERE config_key = 'shadow_exit_mode'")
        if row and row.get("config_value") in ("tp1_full", "partial_tp", "tp2_full", "tp3_full"):
            exit_mode = row["config_value"]
    except Exception:
        pass

    # --- Phase A: evaluate non-exchange pending shadows (original logic) ---
    pending = db.fetch_all("""
        SELECT id, symbol, direction, entry_price, stop_loss,
               take_profit_1, take_profit_2, take_profit_3,
               confidence_score, rejected_at
        FROM apex_shadow_trades
        WHERE shadow_status = 'pending'
          AND entry_price IS NOT NULL AND stop_loss IS NOT NULL
          AND placed_on_exchange = 0
        ORDER BY rejected_at
        LIMIT 50
    """)

    evaluated = 0
    for shadow in (pending or []):
        try:
            result = _evaluate_one_shadow(db, shadow, expiry_hours, exit_mode)
            if result:
                evaluated += 1
        except Exception as e:
            logger.warning(f"[Auditor] Shadow #{shadow['id']} eval error: {e}")

    # Expire shadows older than expiry window that still haven't hit entry
    try:
        expired_cnt = db.execute("""
            UPDATE apex_shadow_trades
            SET shadow_status = 'shadow_expired', evaluated_at = NOW(),
                exit_reason = 'entry_never_hit'
            WHERE shadow_status = 'pending'
              AND rejected_at < DATE_SUB(NOW(), INTERVAL %s HOUR)
        """, (expiry_hours,))
        if expired_cnt:
            logger.info(f"[Auditor] Expired {expired_cnt} shadow trades "
                        f"(entry never hit within {expiry_hours}h)")
    except Exception as e:
        logger.debug(f"[Auditor] Shadow expiry sweep: {e}")

    if evaluated:
        logger.info(f"[Auditor] Evaluated {evaluated} shadow trades")
        if get_system_config(db, "shadow_lesson_enabled", "true").lower() == "true":
            _generate_shadow_lessons(db, config)

    # --- Phase B: backfill counterfactual for exchange-traded shadows ---
    _backfill_exchange_counterfactual(db, exit_mode)


def _evaluate_one_shadow(db, shadow: dict, expiry_hours: int,
                         exit_mode: str = "tp1_full") -> bool:
    """Check candle data for a single shadow trade.
    exit_mode controls how wins are calculated:
      'tp1_full'    — 100% exit at TP1 (default, apples-to-apples with exchange)
      'partial_tp'  — 50% at TP1, 25% at TP2, 25% at TP3 (weighted P&L)
      'tp2_full'    — 100% exit at TP2 (ignore TP1)
      'tp3_full'    — 100% exit at TP3 (ignore TP1/TP2)
    Returns True if shadow was evaluated (status changed)."""
    sid = shadow["id"]
    symbol = shadow["symbol"]
    direction = (shadow.get("direction") or "").upper()
    entry = float(shadow.get("entry_price") or 0)
    sl = float(shadow.get("stop_loss") or 0)
    tp1 = float(shadow.get("take_profit_1") or 0)
    tp2 = float(shadow.get("take_profit_2") or 0)
    tp3 = float(shadow.get("take_profit_3") or 0)
    rejected_at = shadow.get("rejected_at")

    if not entry or not sl or not direction:
        db.execute(
            "UPDATE apex_shadow_trades SET shadow_status = 'no_levels', "
            "evaluated_at = NOW() WHERE id = %s", (sid,))
        return True

    # Graceful degrade: if chosen exit mode targets a NULL TP, fall back
    if exit_mode == "tp3_full" and not tp3:
        exit_mode = "tp2_full" if tp2 else "tp1_full"
    if exit_mode == "tp2_full" and not tp2:
        exit_mode = "tp1_full"

    candles = db.fetch_all("""
        SELECT candle_time, open, high, low, close
        FROM candles
        WHERE symbol = %s AND timeframe = 'M5'
          AND candle_time >= %s
        ORDER BY candle_time
        LIMIT 2000
    """, (symbol, rejected_at))

    if not candles or len(candles) < 3:
        age_hours = (datetime.utcnow() - rejected_at).total_seconds() / 3600
        if age_hours > expiry_hours:
            db.execute(
                "UPDATE apex_shadow_trades SET shadow_status = 'shadow_expired', "
                "evaluated_at = NOW(), exit_reason = 'no_candle_data' WHERE id = %s",
                (sid,))
            return True
        return False

    entry_hit = False
    entry_hit_time = None
    tp1_hit = False
    tp2_hit = False

    def _pnl(exit_p):
        if direction == "BUY":
            return ((exit_p - entry) / entry) * 100
        return ((entry - exit_p) / entry) * 100

    def _check_sl(h, l):
        if direction == "BUY" and l <= sl:
            return True
        if direction == "SELL" and h >= sl:
            return True
        return False

    def _check_tp(h, l, tp_level):
        if not tp_level:
            return False
        if direction == "BUY" and h >= tp_level:
            return True
        if direction == "SELL" and l <= tp_level:
            return True
        return False

    for c in candles:
        h = float(c["high"])
        l = float(c["low"])
        ct = c["candle_time"]

        if not entry_hit:
            if direction == "BUY" and l <= entry:
                entry_hit = True
                entry_hit_time = ct
            elif direction == "SELL" and h >= entry:
                entry_hit = True
                entry_hit_time = ct
            continue

        if _check_sl(h, l):
            if exit_mode == "partial_tp" and (tp1_hit or tp2_hit):
                partial_pnl = 0.0
                if tp1_hit:
                    partial_pnl += 0.50 * _pnl(tp1)
                if tp2_hit:
                    partial_pnl += 0.25 * _pnl(tp2)
                remaining = 1.0 - (0.50 if tp1_hit else 0) - (0.25 if tp2_hit else 0)
                partial_pnl += remaining * _pnl(sl)
                # Net P&L may be positive if partial TP gains outweigh SL loss
                status = "shadow_won" if partial_pnl > 0 else "shadow_lost"
                _finalize_shadow(db, sid, status, sl, ct,
                                 entry_hit_time, partial_pnl, "sl_hit_after_partial")
            else:
                _finalize_shadow(db, sid, "shadow_lost", sl, ct,
                                 entry_hit_time, _pnl(sl), "sl_hit")
            return True

        if _check_tp(h, l, tp1) and not tp1_hit:
            tp1_hit = True
            if exit_mode == "tp1_full":
                _finalize_shadow(db, sid, "shadow_won", tp1, ct,
                                 entry_hit_time, _pnl(tp1), "tp1_hit")
                return True
            if exit_mode in ("tp2_full", "tp3_full", "partial_tp"):
                pass

        if _check_tp(h, l, tp2) and not tp2_hit:
            tp2_hit = True
            if exit_mode == "tp2_full":
                _finalize_shadow(db, sid, "shadow_won", tp2, ct,
                                 entry_hit_time, _pnl(tp2), "tp2_hit")
                return True
            if exit_mode == "tp3_full":
                pass

        if _check_tp(h, l, tp3):
            if exit_mode == "tp3_full":
                _finalize_shadow(db, sid, "shadow_won", tp3, ct,
                                 entry_hit_time, _pnl(tp3), "tp3_hit")
                return True
            if exit_mode == "partial_tp":
                w_tp1 = _pnl(tp1) if tp1 else _pnl(tp3)
                w_tp2 = _pnl(tp2) if tp2 else w_tp1
                weighted = 0.50 * w_tp1 + 0.25 * w_tp2 + 0.25 * _pnl(tp3)
                _finalize_shadow(db, sid, "shadow_won", tp3, ct,
                                 entry_hit_time, weighted, "tp3_partial_close")
                return True

        if exit_mode == "partial_tp" and tp1_hit and not tp2 and not tp3:
            _finalize_shadow(db, sid, "shadow_won", tp1, ct,
                             entry_hit_time, _pnl(tp1), "tp1_hit")
            return True

    return False


def _finalize_shadow(db, shadow_id: int, status: str, exit_price: float,
                     exit_time, entry_hit_time, pnl_pct: float,
                     exit_reason: str):
    """Write the evaluation result back to the shadow trade row."""
    db.execute("""
        UPDATE apex_shadow_trades
        SET shadow_status = %s, exit_price = %s, exit_at = %s,
            entry_hit_at = %s, counterfactual_pnl_pct = %s,
            exit_reason = %s, evaluated_at = NOW()
        WHERE id = %s
    """, (status, exit_price, exit_time, entry_hit_time,
          round(pnl_pct, 4), exit_reason, shadow_id))
    logger.info(f"[Auditor] Shadow #{shadow_id}: {status} "
                f"({exit_reason}, {pnl_pct:+.2f}%)")


def _backfill_exchange_counterfactual(db, exit_mode: str = "tp1_full"):
    """Run the candle resolver on exchange-placed shadows that are closed
    but have no counterfactual_pnl_pct yet. This gives us forecast vs actual
    comparison without touching the shadow_status or exchange data."""
    missing = db.fetch_all("""
        SELECT id, symbol, direction, entry_price, stop_loss,
               take_profit_1, take_profit_2, take_profit_3,
               rejected_at
        FROM apex_shadow_trades
        WHERE placed_on_exchange = 1
          AND counterfactual_pnl_pct IS NULL
          AND entry_price IS NOT NULL AND stop_loss IS NOT NULL
          AND rejected_at IS NOT NULL
        ORDER BY rejected_at DESC
        LIMIT 50
    """)
    if not missing:
        return

    filled = 0
    for s in missing:
        sid = s["id"]
        symbol = s["symbol"]
        direction = (s.get("direction") or "").upper()
        entry = float(s.get("entry_price") or 0)
        sl = float(s.get("stop_loss") or 0)
        tp1 = float(s.get("take_profit_1") or 0)
        tp2 = float(s.get("take_profit_2") or 0)
        tp3 = float(s.get("take_profit_3") or 0)
        rejected_at = s.get("rejected_at")

        if not entry or not sl or not direction or not rejected_at:
            continue

        mode = exit_mode
        if mode == "tp3_full" and not tp3:
            mode = "tp2_full" if tp2 else "tp1_full"
        if mode == "tp2_full" and not tp2:
            mode = "tp1_full"

        candles = db.fetch_all("""
            SELECT candle_time, high, low FROM candles
            WHERE symbol = %s AND timeframe = 'M5'
              AND candle_time >= %s
            ORDER BY candle_time LIMIT 2000
        """, (symbol, rejected_at))
        if not candles or len(candles) < 3:
            continue

        def _pnl(exit_p):
            if direction == "BUY":
                return ((exit_p - entry) / entry) * 100
            return ((entry - exit_p) / entry) * 100

        entry_hit = False
        tp1_hit = False
        tp2_hit = False
        cf_pnl = None
        cf_exit = None
        cf_reason = None

        for c in candles:
            h = float(c["high"])
            l = float(c["low"])

            if not entry_hit:
                if (direction == "BUY" and l <= entry) or \
                   (direction == "SELL" and h >= entry):
                    entry_hit = True
                continue

            sl_hit = (direction == "BUY" and l <= sl) or \
                     (direction == "SELL" and h >= sl)
            tp1_h = tp1 and ((direction == "BUY" and h >= tp1) or
                             (direction == "SELL" and l <= tp1))
            tp2_h = tp2 and ((direction == "BUY" and h >= tp2) or
                             (direction == "SELL" and l <= tp2))
            tp3_h = tp3 and ((direction == "BUY" and h >= tp3) or
                             (direction == "SELL" and l <= tp3))

            if sl_hit:
                if mode == "partial_tp" and (tp1_hit or tp2_hit):
                    pp = 0.0
                    if tp1_hit:
                        pp += 0.50 * _pnl(tp1)
                    if tp2_hit:
                        pp += 0.25 * _pnl(tp2)
                    rem = 1.0 - (0.50 if tp1_hit else 0) - (0.25 if tp2_hit else 0)
                    pp += rem * _pnl(sl)
                    cf_pnl = pp
                else:
                    cf_pnl = _pnl(sl)
                cf_exit = sl
                cf_reason = "sl_hit"
                break

            if tp1_h and not tp1_hit:
                tp1_hit = True
                if mode == "tp1_full":
                    cf_pnl = _pnl(tp1)
                    cf_exit = tp1
                    cf_reason = "tp1_hit"
                    break
            if tp2_h and not tp2_hit:
                tp2_hit = True
                if mode == "tp2_full":
                    cf_pnl = _pnl(tp2)
                    cf_exit = tp2
                    cf_reason = "tp2_hit"
                    break
            if tp3_h:
                if mode == "tp3_full":
                    cf_pnl = _pnl(tp3)
                    cf_exit = tp3
                    cf_reason = "tp3_hit"
                    break
                if mode == "partial_tp":
                    w1 = _pnl(tp1) if tp1 else _pnl(tp3)
                    w2 = _pnl(tp2) if tp2 else w1
                    cf_pnl = 0.50 * w1 + 0.25 * w2 + 0.25 * _pnl(tp3)
                    cf_exit = tp3
                    cf_reason = "tp3_partial"
                    break

        if cf_pnl is not None:
            try:
                db.execute("""
                    UPDATE apex_shadow_trades
                    SET counterfactual_pnl_pct = %s,
                        counterfactual_pnl = %s
                    WHERE id = %s
                """, (round(cf_pnl, 4), round(cf_exit, 8) if cf_exit else None, sid))
                filled += 1
            except Exception as e:
                logger.debug(f"[Auditor] Backfill cf #{sid}: {e}")

    if filled:
        logger.info(f"[Auditor] Backfilled counterfactual for "
                    f"{filled} exchange-traded shadows")


def _generate_shadow_lessons(db, config):
    """Ledger generates lessons for shadow trades that would have won.
    One lightweight LLM call per shadow — focused on what Apex's concern was
    and whether candle data validated or invalidated it."""
    unlessoned = db.fetch_all("""
        SELECT id, symbol, direction, entry_price, stop_loss, take_profit_1,
               confidence_score, rationale, shadow_status, exit_reason,
               counterfactual_pnl_pct, rejected_at, evaluated_at, asset_class
        FROM apex_shadow_trades
        WHERE shadow_status IN ('shadow_won', 'shadow_lost')
          AND lesson_text IS NULL
          AND evaluated_at IS NOT NULL
        ORDER BY evaluated_at DESC
        LIMIT 10
    """)

    if not unlessoned:
        return

    auditor_model, auditor_provider = _get_auditor_model_and_provider(db)
    identity = get_system_config(
        db, "auditor_system_identity",
        "You are Ledger, Chief Auditor at JarvAIs. Respond with valid JSON only.")

    for shadow in unlessoned:
        try:
            _generate_one_shadow_lesson(db, config, shadow, auditor_model,
                                       auditor_provider, identity)
        except Exception as e:
            logger.warning(f"[Auditor] Shadow lesson #{shadow['id']} error: {e}")


def _generate_one_shadow_lesson(db, config, shadow: dict,
                                auditor_model: str, auditor_provider: str,
                                identity: str):
    """Generate a lesson for a single evaluated shadow trade."""
    sid = shadow["id"]
    outcome = "WOULD HAVE WON" if shadow["shadow_status"] == "shadow_won" else "WOULD HAVE LOST"
    pnl_str = f"{shadow.get('counterfactual_pnl_pct', 0):+.2f}%"

    prompt = (
        f"## SHADOW TRADE AUDIT — {shadow['symbol']} {shadow.get('direction','?')}\n\n"
        f"Apex REJECTED this trade at {shadow.get('confidence_score', '?')}% confidence.\n"
        f"**Outcome:** {outcome} ({pnl_str})\n"
        f"**Exit reason:** {shadow.get('exit_reason', '?')}\n\n"
        f"**Apex's rationale for rejecting:**\n{(shadow.get('rationale') or 'No rationale recorded')[:3000]}\n\n"
        f"**Trade setup:** Entry={shadow.get('entry_price')}, "
        f"SL={shadow.get('stop_loss')}, TP1={shadow.get('take_profit_1')}, "
        f"Direction={shadow.get('direction')}\n\n"
        f"**Your task:** Write a CONCISE lesson (2-4 sentences) for Apex.\n"
        f"- If it WOULD HAVE WON: What was Apex's concern? Was it validated or "
        f"invalidated by what actually happened? What pattern should Apex recognise "
        f"next time to avoid missing this opportunity?\n"
        f"- If it WOULD HAVE LOST: Confirm the rejection was smart. What specific "
        f"factor in Apex's reasoning proved correct?\n\n"
        f"Reply with JSON: {{\"lesson\": \"...\", \"apex_concern_valid\": true/false, "
        f"\"pattern_tag\": \"short_tag_for_pattern\"}}"
    )

    try:
        resp = _call_llm(
            db, auditor_model, auditor_provider,
            system_prompt=identity,
            user_prompt=prompt,
            context="shadow_lesson",
            agent_id="ledger",
            max_tokens=500,
            temperature=0.2,
        )
        raw = resp.get("text", "")
    except Exception as e:
        logger.warning(f"[Auditor] Shadow lesson LLM failed #{sid}: {e}")
        return

    lesson_text = ""
    try:
        parsed = json.loads(raw)
        lesson_text = parsed.get("lesson", raw)
    except (json.JSONDecodeError, TypeError):
        lesson_text = raw[:2000] if raw else ""

    if not lesson_text:
        return

    db.execute("""
        UPDATE apex_shadow_trades
        SET lesson_text = %s, lesson_generated_at = NOW()
        WHERE id = %s
    """, (lesson_text[:5000], sid))

    # Also persist into trade_lessons for Symbol Knowledge Bank injection
    outcome_val = "WIN" if shadow["shadow_status"] == "shadow_won" else "LOSS"
    try:
        db.insert_lesson({
            "dossier_id": None,
            "signal_id": None,
            "symbol": shadow["symbol"],
            "account_id": "jarvais",
            "model_used": "shadow_audit",
            "outcome": outcome_val,
            "pnl_usd": None,
            "what_worked": lesson_text[:5000] if outcome_val == "WIN" else None,
            "what_failed": lesson_text[:5000] if outcome_val == "LOSS" else None,
            "lesson_text": lesson_text[:5000],
            "root_cause": f"Shadow trade (rejected at {shadow.get('confidence_score','?')}% confidence)",
            "optimal_trade_summary": (
                f"Direction: {shadow.get('direction','?')}, "
                f"Entry: {shadow.get('entry_price','?')}, "
                f"SL: {shadow.get('stop_loss','?')}, "
                f"TP1: {shadow.get('take_profit_1','?')}"
            ),
            "confidence_calibration": (
                f"Confidence {shadow.get('confidence_score','?')} — "
                f"REJECTED (do_not_trade) — {outcome} {pnl_str}"
            ),
            "full_post_mortem": None,
            "api_cost_usd": 0,
        })
    except Exception as e:
        logger.debug(f"[Auditor] Shadow lesson to trade_lessons failed #{sid}: {e}")

    logger.info(f"[Auditor] Shadow lesson generated for #{sid} "
                f"({shadow['symbol']} {outcome})")


def _maybe_trigger_synthesis(db, config, batch_size: int = 10):
    """Trigger recommendation synthesis every N completed audits.
    Counts audits completed since the last synthesis run. When the count
    reaches batch_size, runs a mid-cycle synthesis with a 3-day lookback.
    Uses a module-level counter as a fallback to prevent re-triggering
    if synthesis produces 0 recommendations."""
    global _last_synthesis_audit_count
    try:
        total_row = db.fetch_one(
            "SELECT COUNT(*) as cnt FROM audit_reports WHERE status = 'completed'")
        total_audits = total_row.get("cnt", 0) if total_row else 0

        last_rec = db.fetch_one(
            "SELECT MAX(created_at) as last_ts FROM audit_recommendations")
        last_ts = last_rec.get("last_ts") if last_rec else None

        if last_ts:
            row = db.fetch_one(
                "SELECT COUNT(*) as cnt FROM audit_reports "
                "WHERE status = 'completed' AND created_at > %s", (last_ts,))
        else:
            row = db.fetch_one(
                "SELECT COUNT(*) as cnt FROM audit_reports WHERE status = 'completed'")

        cnt = row.get("cnt", 0) if row else 0

        if cnt >= batch_size and total_audits > _last_synthesis_audit_count:
            logger.info(f"[Auditor] {cnt} audits since last synthesis — "
                        f"triggering mid-cycle synthesis (every {batch_size} trades)")
            _last_synthesis_audit_count = total_audits
            run_daily_recommendation_synthesis(db, config, lookback_days=3)
    except Exception as e:
        logger.error(f"[Auditor] Mid-cycle synthesis check error: {e}")


def run_postmortem_async(db, config, dossier_id: int):
    """Fire-and-forget post-mortem in a background thread.
    After the postmortem completes, checks if enough audits have accumulated
    to trigger a mid-cycle recommendation synthesis (every 10 trades).
    Also evaluates pending shadow trades on each cycle."""
    def _run():
        try:
            run_full_postmortem(db, config, dossier_id)
            _maybe_trigger_synthesis(db, config, batch_size=10)
            evaluate_shadow_trades(db, config)
        except Exception as e:
            logger.error(f"[Auditor] Async post-mortem error #{dossier_id}: {e}")

    t = threading.Thread(target=_run, daemon=True,
                         name=f"audit-pm-{dossier_id}")
    t.start()
    logger.info(f"[Auditor] Post-mortem started in background for #{dossier_id}")


# ═══════════════════════════════════════════════════════════════════════
# Daily Roll-Up (5am Dubai / 1am UTC cron)
# ═══════════════════════════════════════════════════════════════════════

def run_daily_rollup(db, config) -> Optional[int]:
    """Review all trades from previous day, find patterns, make systemic recs."""
    dubai_offset = timedelta(hours=4)
    now_dubai = datetime.utcnow() + dubai_offset
    yesterday = (now_dubai - timedelta(days=1)).date()

    # Dubai UTC+4: yesterday 00:00 to today 00:00 Dubai = UTC range
    dubai_offset = timedelta(hours=4)
    start_utc = datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0) - dubai_offset
    end_utc = datetime(now_dubai.date().year, now_dubai.date().month, now_dubai.date().day, 0, 0, 0) - dubai_offset

    reports = db.fetch_all("""
        SELECT ar.*, td.symbol, td.direction, td.status as trade_status,
               td.realised_pnl, td.realised_pnl_pct
        FROM audit_reports ar
        JOIN trade_dossiers td ON ar.dossier_id = td.id
        WHERE ar.report_type = 'trade_postmortem'
          AND ar.status = 'completed'
          AND ar.created_at >= %s AND ar.created_at < %s
        ORDER BY ar.created_at
    """, (start_utc, end_utc))

    if not reports:
        logger.info(f"[Auditor] Daily rollup: no completed audits for {yesterday}")
        return None

    summaries = "\n\n".join(
        f"## Dossier #{r.get('dossier_id')} ({r.get('symbol')} {r.get('trade_status')})\n"
        f"Blame: {r.get('blame_assignment','{}')}\n"
        f"Root cause: {r.get('root_cause','?')}\n"
        f"P&L: {r.get('realised_pnl','?')}"
        for r in reports)

    prompt = (
        f"You are Ledger doing the daily audit rollup for {yesterday}.\n\n"
        f"{_CRYPTO_CONTEXT}\n\n"
        f"## TODAY'S AUDITED TRADES ({len(reports)} total)\n{summaries}\n\n"
        f"Analyse patterns across trades. Find systemic issues. Consider crypto-specific "
        f"factors: funding rate impact, session timing, exchange wicks, leverage sizing, "
        f"liquidation cascades. Respond JSON:\n"
        f'{{"patterns": ["<pattern>"], "systemic_recommendations": ["<rec>"], '
        f'"overall_severity": "<critical|major|minor>", '
        f'"daily_summary": "<paragraph>"}}'
    )

    auditor_model, auditor_provider = _get_auditor_model_and_provider(db)
    resp = _call_llm(db, auditor_model, auditor_provider,
                     _get_auditor_identity(db),
                     prompt, context="audit_daily_rollup")

    wins = sum(1 for r in reports if r.get("trade_status") == "won")
    losses = sum(1 for r in reports if r.get("trade_status") == "lost")

    report_id = db.execute_returning_id("""
        INSERT INTO audit_reports
        (report_type, report_date, trades_reviewed, wins_count, losses_count,
         auditor_summary, systemic_recs, model_used, total_cost, status, completed_at)
        VALUES ('daily_summary', %s, %s, %s, %s, %s, %s, %s, %s, 'completed', NOW())
    """, (yesterday, len(reports), wins, losses,
          resp.get("text", "")[:5000], resp.get("text", "")[:5000],
          auditor_model, resp.get("cost", 0)))

    # Update learning metrics
    _update_learning_metrics(db, yesterday, reports)

    # Run recommendation synthesis (combines raw recs from all recent audits)
    try:
        synth_count = run_daily_recommendation_synthesis(db, config, lookback_days=7)
        logger.info(f"[Auditor] Daily synthesis: {synth_count} holistic recommendations created")
    except Exception as e:
        logger.error(f"[Auditor] Daily synthesis failed: {e}")

    # Compute and store system trust score
    try:
        _compute_trust_score(db, yesterday)
    except Exception as e:
        logger.debug(f"[Auditor] Trust score computation error: {e}")

    # Retire underperforming strategies and auto-activate graduated ones
    try:
        _retire_underperforming_strategies(db)
    except Exception as e:
        logger.debug(f"[Auditor] Strategy retirement error: {e}")

    # Auto-resolve A/B prompt tests that have enough data
    try:
        _resolve_ab_tests(db)
    except Exception as e:
        logger.debug(f"[Auditor] A/B test resolution error: {e}")

    # Evaluate pending shadow trades and generate lessons
    try:
        evaluate_shadow_trades(db, config)
    except Exception as e:
        logger.debug(f"[Auditor] Shadow trade evaluation in daily rollup: {e}")

    logger.info(f"[Auditor] Daily rollup #{report_id} for {yesterday}: "
                f"{len(reports)} trades, {wins}W/{losses}L")
    return report_id


def _compute_trust_score(db, score_date):
    """Compute a composite trust score from confidence accuracy, recommendation
    success rate, and rolling profitability. Used to govern CEO autonomy graduation.

    Score components (each 0-100, weighted):
      - Confidence accuracy (40%): how well Apex's confidence predictions match reality
      - Recommendation success rate (25%): pct of applied recs that improved outcomes
      - Profitability score (35%): normalised 30-day rolling PnL
    """
    # 30-day trade window
    trades = db.fetch_all("""
        SELECT confidence_score, status, realised_pnl
        FROM trade_dossiers
        WHERE status IN ('won','lost')
          AND confidence_score IS NOT NULL
          AND created_at >= DATE_SUB(%s, INTERVAL 30 DAY)
    """, (score_date,))

    if not trades or len(trades) < 10:
        return

    # 1. Confidence accuracy (Brier-like score)
    buckets = {}
    for t in trades:
        conf = int(t["confidence_score"] or 0)
        bucket = (conf // 10) * 10
        if bucket not in buckets:
            buckets[bucket] = {"wins": 0, "total": 0}
        buckets[bucket]["total"] += 1
        if t["status"] == "won":
            buckets[bucket]["wins"] += 1

    cal_errors = []
    for b, d in buckets.items():
        if d["total"] >= 3:
            actual_wr = d["wins"] / d["total"] * 100
            predicted = b + 5  # midpoint of bucket
            cal_errors.append(abs(predicted - actual_wr))

    avg_cal_error = sum(cal_errors) / len(cal_errors) if cal_errors else 50
    # Lower error = higher score. Perfect = 100, error of 50 = 0
    confidence_accuracy = max(0, min(100, 100 - avg_cal_error * 2))

    # 2. Recommendation success rate (30 days)
    rec_stats = db.fetch_one("""
        SELECT
            COUNT(*) as total_applied,
            SUM(CASE WHEN status = 'rolled_back' THEN 1 ELSE 0 END) as rolled_back
        FROM audit_recommendations
        WHERE status IN ('applied', 'rolled_back')
          AND applied_at >= DATE_SUB(%s, INTERVAL 30 DAY)
    """, (score_date,))
    total_applied = rec_stats.get("total_applied", 0) if rec_stats else 0
    rolled_back = rec_stats.get("rolled_back", 0) if rec_stats else 0
    recs_successful = max(0, total_applied - rolled_back)
    rec_success_rate = (recs_successful / total_applied * 100) if total_applied > 0 else 50

    # 3. Profitability score
    wins = sum(1 for t in trades if t["status"] == "won")
    total_pnl = sum(float(t.get("realised_pnl") or 0) for t in trades)
    wr_30d = wins / len(trades) * 100

    # Normalise PnL to 0-100 (breakeven=50, +$500=100, -$500=0)
    pnl_normalised = max(0, min(100, 50 + total_pnl / 10))

    w_conf = get_system_config_float(db, "trust_weight_confidence", 0.40)
    w_recs = get_system_config_float(db, "trust_weight_recs", 0.25)
    w_prof = get_system_config_float(db, "trust_weight_profitability", 0.35)
    overall = (confidence_accuracy * w_conf
               + rec_success_rate * w_recs
               + pnl_normalised * w_prof)

    thresh_auto = get_system_config_float(db, "trust_autonomous_threshold", 85)
    thresh_sup = get_system_config_float(db, "trust_supervised_threshold", 70)
    thresh_asst = get_system_config_float(db, "trust_assisted_threshold", 55)
    if overall >= thresh_auto and len(trades) >= 100:
        eligible = "autonomous"
    elif overall >= thresh_sup and len(trades) >= 50:
        eligible = "supervised"
    elif overall >= thresh_asst and len(trades) >= 30:
        eligible = "assisted"
    else:
        eligible = "manual"

    db.execute("""
        INSERT INTO system_trust_score
        (score_date, overall_score, confidence_accuracy, rec_success_rate,
         profitability_score, calibration_error, win_rate_30d, total_pnl_30d,
         trades_30d, recs_applied_30d, recs_successful_30d, autonomy_eligible)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            overall_score = VALUES(overall_score),
            confidence_accuracy = VALUES(confidence_accuracy),
            rec_success_rate = VALUES(rec_success_rate),
            profitability_score = VALUES(profitability_score),
            calibration_error = VALUES(calibration_error),
            win_rate_30d = VALUES(win_rate_30d),
            total_pnl_30d = VALUES(total_pnl_30d),
            trades_30d = VALUES(trades_30d),
            recs_applied_30d = VALUES(recs_applied_30d),
            recs_successful_30d = VALUES(recs_successful_30d),
            autonomy_eligible = VALUES(autonomy_eligible)
    """, (score_date, round(overall, 2), round(confidence_accuracy, 2),
          round(rec_success_rate, 2), round(pnl_normalised, 2),
          round(avg_cal_error, 2), round(wr_30d, 2), round(total_pnl, 4),
          len(trades), total_applied, recs_successful, eligible))

    logger.info(f"[Auditor] Trust score for {score_date}: {overall:.1f}/100 "
                f"(conf_acc={confidence_accuracy:.0f}, rec_rate={rec_success_rate:.0f}, "
                f"profit={pnl_normalised:.0f}) -> eligible: {eligible}")


def _update_learning_metrics(db, date, reports: List[Dict]):
    """Compute and store learning metrics for a given date."""
    wins = sum(1 for r in reports if r.get("trade_status") == "won")
    losses = sum(1 for r in reports if r.get("trade_status") == "lost")
    abandoned = sum(1 for r in reports if r.get("trade_status") == "abandoned")
    total = len(reports)
    win_rate = round((wins / total * 100), 2) if total > 0 else 0

    # Aggregate blame
    blame_totals = {"quant": [], "apex": [], "tracker": [],
                    "data_quality": [], "market": [], "geo_macro": []}
    for r in reports:
        try:
            blame = json.loads(r.get("blame_assignment") or "{}")
            for k in blame_totals:
                if k in blame:
                    blame_totals[k].append(blame[k])
        except (json.JSONDecodeError, TypeError):
            pass

    def _avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else 0

    try:
        db.execute("""
            INSERT INTO audit_learning_metrics
            (metric_date, total_trades, wins, losses, abandoned, win_rate,
             quant_blame_avg, apex_blame_avg, tracker_blame_avg,
             data_blame_avg, market_blame_avg, geo_macro_blame_avg)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                total_trades = VALUES(total_trades),
                wins = VALUES(wins), losses = VALUES(losses),
                win_rate = VALUES(win_rate),
                quant_blame_avg = VALUES(quant_blame_avg),
                apex_blame_avg = VALUES(apex_blame_avg)
        """, (date, total, wins, losses, abandoned, win_rate,
              _avg(blame_totals["quant"]), _avg(blame_totals["apex"]),
              _avg(blame_totals["tracker"]), _avg(blame_totals["data_quality"]),
              _avg(blame_totals["market"]), _avg(blame_totals["geo_macro"])))
    except Exception as e:
        logger.error(f"[Auditor] Learning metrics error: {e}")


def _serialise(v):
    """Make a value JSON-serialisable."""
    if isinstance(v, (datetime,)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return v


# ═══════════════════════════════════════════════════════════════════════
# Daily Recommendation Synthesis
# ═══════════════════════════════════════════════════════════════════════

SYNTHESIS_PROMPT = """You are Ledger, Chief Auditor at JarvAIs. You are performing a DAILY REVIEW of all post-mortem findings.

Below are the findings from {report_count} recent trade post-mortems, including root causes, blame assignments, summaries, and any per-trade recommendations. Many of them repeat similar themes. Your job is to SYNTHESISE these into a small number of holistic, actionable changes to agent prompts/souls.

## AUDIT FINDINGS
{raw_recommendations}

## CURRENT AGENT PROMPTS (what they actually have today)
{agent_prompts}

Produce a CONDENSED set of changes. Respond with ONLY this JSON:
{{
  "synthesis_summary": "<2-3 paragraph overview of the patterns you observed across all audits>",
  "lesson_window_recommendation": {{
    "current_wins": 25,
    "current_losses": 25,
    "recommended_wins": <number -- how many recent winning trade lessons should Apex/Quant reference>,
    "recommended_losses": <number -- how many recent losing trade lessons should Apex/Quant reference>,
    "reasoning": "<Why you recommend this window size. Are older lessons still relevant? Are more needed? Or can we reduce?>"
  }},
  "improvement_trajectory": "<Is the system improving? Describe the trend across the {report_count} audits.>",
  "recommendations": [
    {{
      "category": "<prompt_change|soul_change|identity_change|process_change>",
      "target_agent": "<agent_id>",
      "target_field": "<soul or identity_prompt>",
      "current_value": "<EXACT quote from the agent's current soul/identity_prompt above, or empty string if adding new text>",
      "proposed_value": "<exact replacement or new text to add>",
      "evidence": "<which audit patterns support this, e.g. '4 of 5 audits showed premature entries'>",
      "business_justification": "<why this improves overall profitability>",
      "expected_impact": "<measurable improvement>",
      "severity": "<critical|important|suggestion>"
    }}
  ]
}}

RULES:
- MAXIMUM 5 recommendations. Combine related issues into single changes.
- Each recommendation must address a PATTERN across multiple audits, not a single trade.
- current_value MUST be an EXACT quote from the agent's prompt above, or "" if appending new text.
- proposed_value must be a general system-level improvement that helps ALL future trades.
- Do NOT reference specific trade prices, condition numbers (C1, C2), or individual dossier IDs.
- For prompt_change/soul_change: target_field must be "soul" or "identity_prompt".
- For process_change: describe the systemic behaviour change needed (manual implementation required).
- Prioritise the changes that would have the HIGHEST impact on win rate and loss reduction.

DEDUPLICATION (CRITICAL):
- If the EXISTING OPEN RECOMMENDATIONS section above lists recommendations that are
  already pending, you MUST check each new recommendation against them.
- If your new finding overlaps with an existing rec (same agent, same field, same theme),
  include "update_existing_id": <id> in that recommendation object. The system will UPDATE
  the existing rec instead of creating a duplicate.
- Only create a new recommendation (without update_existing_id) if it is genuinely about
  a DIFFERENT agent, DIFFERENT field, or a fundamentally DIFFERENT improvement.
- If an existing rec covers the same ground but your new evidence strengthens it, use
  update_existing_id and provide the improved proposed_value that incorporates both."""


def run_daily_recommendation_synthesis(db, config, lookback_days: int = 7) -> Optional[int]:
    """Review all raw post-mortem recs from the last N days and synthesise
    them into a small set of holistic, actionable soul/prompt changes.
    Returns the number of synthesised recommendations created, or None on failure."""

    logger.info(f"[Auditor] Starting daily recommendation synthesis (lookback={lookback_days}d)")

    reports = db.fetch_all("""
        SELECT id, dossier_id, symbol, trade_direction, trade_outcome,
               severity, root_cause, auditor_summary, blame_assignment,
               systemic_recs, created_at, duo_id
        FROM audit_reports
        WHERE status = 'completed'
          AND root_cause IS NOT NULL
          AND created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        ORDER BY created_at DESC
    """, (lookback_days,))

    if not reports:
        logger.info("[Auditor] No completed audits in lookback window")
        return 0

    # Build findings from both systemic_recs (new audits) and root_cause/blame (all audits)
    raw_recs_text = []
    finding_num = 0

    for report in reports:
        finding_num += 1
        blame_str = ""
        try:
            blame = json.loads(report.get("blame_assignment") or "{}")
            top_agents = sorted(blame.items(), key=lambda x: x[1], reverse=True)[:3]
            blame_str = ", ".join(f"{a}: {p}%" for a, p in top_agents)
        except (json.JSONDecodeError, TypeError):
            pass

        raw_recs_text.append(
            f"AUDIT #{report['id']}: {report.get('symbol','?')} "
            f"{(report.get('trade_direction') or '?').upper()} -> "
            f"{report.get('trade_outcome','?')} | "
            f"Severity: {report.get('severity','?')} | "
            f"Blame: {blame_str}\n"
            f"  Root cause: {(report.get('root_cause') or 'N/A')[:200]}\n"
            f"  Summary: {(report.get('auditor_summary') or 'N/A')[:300]}")

        # Also include per-trade recs if they exist (new-style audits)
        if report.get("systemic_recs"):
            try:
                recs = json.loads(report["systemic_recs"])
                for rec in recs[:3]:
                    raw_recs_text.append(
                        f"  -> Rec: [{rec.get('severity','?')}] {rec.get('category','?')} "
                        f"for {rec.get('target_agent','?')}: "
                        f"{rec.get('business_justification','')[:150]}")
            except (json.JSONDecodeError, TypeError):
                pass

    agent_prompt_sections = []
    for aid in ("quant", "apex", "tracker", "geo", "macro"):
        profile = _get_agent_profile(db, aid)
        if profile:
            soul = (profile.get("soul") or "")[:3000]
            identity = (profile.get("identity_prompt") or "")[:1500]
            agent_prompt_sections.append(
                f"--- {aid.upper()} ---\n"
                f"[identity_prompt]: {identity}\n"
                f"[soul]: {soul}")

    # Fetch existing open recommendations so the AI can merge/update instead of duplicating
    existing_recs = db.fetch_all("""
        SELECT id, category, target_agent, target_field,
               current_value, proposed_value, diff_summary,
               evidence, business_justification, severity, created_at
        FROM audit_recommendations
        WHERE status IN ('pending', 'approved')
        ORDER BY created_at DESC
    """) or []

    existing_recs_text = ""
    if existing_recs:
        lines = []
        for er in existing_recs:
            lines.append(
                f"  REC #{er['id']}: [{er.get('severity','?')}] "
                f"{er.get('category','?')} -> {er.get('target_agent','?')}.{er.get('target_field','?')}\n"
                f"    Justification: {(er.get('business_justification') or '')[:200]}\n"
                f"    Proposed: {(er.get('proposed_value') or '')[:300]}")
        existing_recs_text = (
            f"\n\n## EXISTING OPEN RECOMMENDATIONS ({len(existing_recs)} total)\n"
            "These recommendations are ALREADY pending CEO approval. Do NOT create duplicates.\n"
            "If a new finding matches or overlaps an existing rec, UPDATE it (use its ID) "
            "instead of creating a new one. Only create a genuinely new recommendation if "
            "the finding is about a DIFFERENT agent, DIFFERENT field, or a fundamentally "
            "DIFFERENT issue.\n\n"
            + "\n".join(lines))

    # Inject shadow trade analysis into synthesis context
    shadow_context = ""
    try:
        shadow_stats = db.fetch_one("""
            SELECT COUNT(*) AS total,
                   SUM(shadow_status = 'shadow_won') AS wins,
                   SUM(shadow_status = 'shadow_lost') AS losses,
                   SUM(shadow_status = 'shadow_expired') AS expired,
                   ROUND(AVG(CASE WHEN shadow_status IN ('shadow_won','shadow_lost')
                             THEN counterfactual_pnl_pct END), 2) AS avg_pnl
            FROM apex_shadow_trades
            WHERE shadow_status IN ('shadow_won', 'shadow_lost', 'shadow_expired')
              AND rejected_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        """, (lookback_days,))

        if shadow_stats and int(shadow_stats.get("total") or 0) > 0:
            sw = int(shadow_stats.get("wins") or 0)
            sl = int(shadow_stats.get("losses") or 0)
            st = int(shadow_stats["total"])
            shadow_bands = db.fetch_all("""
                SELECT FLOOR(confidence_score / 5) * 5 AS band,
                       COUNT(*) AS cnt,
                       SUM(shadow_status = 'shadow_won') AS w
                FROM apex_shadow_trades
                WHERE shadow_status IN ('shadow_won', 'shadow_lost')
                  AND confidence_score IS NOT NULL
                  AND rejected_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                GROUP BY band ORDER BY band
            """, (lookback_days,))
            band_lines = []
            for b in (shadow_bands or []):
                bl = int(b["band"])
                bc = int(b["cnt"])
                bw = int(b.get("w") or 0)
                band_lines.append(
                    f"    {bl}-{bl+4}%: {bc} rejected, "
                    f"{bw} would-have-won ({round(bw/bc*100)}% WR)")

            shadow_context = (
                f"\n\n## SHADOW TRADE ANALYSIS (Rejected do_not_trade decisions)\n"
                f"Last {lookback_days} days: {st} evaluated shadows\n"
                f"  Would have won: {sw} ({round(sw/st*100) if st else 0}%)\n"
                f"  Good rejections: {sl} ({round(sl/st*100) if st else 0}%)\n"
                f"  Expired (entry never hit): {int(shadow_stats.get('expired') or 0)}\n"
                f"  Avg counterfactual P&L: {shadow_stats.get('avg_pnl', 0)}%\n"
            )
            if band_lines:
                shadow_context += (
                    "  Confidence band breakdown:\n" + "\n".join(band_lines) + "\n")
            if sw > sl:
                shadow_context += (
                    "\n  ** ALERT: Apex is rejecting MORE winners than losers. "
                    "Consider recommending a threshold adjustment or prompt change "
                    "to encourage more trades in the profitable confidence bands. **\n")
    except Exception as e:
        logger.debug(f"[Auditor] Shadow stats for synthesis: {e}")

    synthesis_template = _load_auditor_prompt(
        db, "auditor_synthesis_prompt", SYNTHESIS_PROMPT)
    prompt = synthesis_template.format(
        report_count=len(reports),
        raw_recommendations="\n".join(raw_recs_text) + existing_recs_text + shadow_context,
        agent_prompts="\n\n".join(agent_prompt_sections),
    )

    auditor_model, auditor_provider = _get_auditor_model_and_provider(db)
    resp = _call_llm(db, auditor_model, auditor_provider,
                     _get_auditor_identity(db),
                     prompt, context="daily_synthesis",
                     max_tokens=8192, temperature=0.2)

    try:
        text = resp["text"]
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])
        else:
            logger.error("[Auditor] Daily synthesis: failed to parse JSON response")
            return None
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"[Auditor] Daily synthesis parse error: {e}")
        return None

    saved = 0
    updated = 0
    for rec in result.get("recommendations", [])[:5]:
        try:
            update_id = rec.get("update_existing_id")
            r_agent = rec.get("target_agent", "")
            r_field = rec.get("target_field", "soul")
            r_cat = rec.get("category", "process_change")

            if update_id:
                existing = db.fetch_one(
                    "SELECT id FROM audit_recommendations WHERE id = %s AND status = 'pending'",
                    (update_id,))
                if existing:
                    db.execute("""
                        UPDATE audit_recommendations
                        SET proposed_value = %s,
                            evidence = CONCAT(COALESCE(evidence, ''), '\n--- Updated by synthesis ---\n', %s),
                            business_justification = %s,
                            expected_impact = %s,
                            severity = %s,
                            diff_summary = %s
                        WHERE id = %s
                    """, (
                        (rec.get("proposed_value") or "")[:5000],
                        rec.get("evidence", "")[:2000],
                        rec.get("business_justification", "")[:2000],
                        rec.get("expected_impact", "")[:1000],
                        rec.get("severity", "suggestion"),
                        result.get("synthesis_summary", "")[:500],
                        update_id,
                    ))
                    updated += 1
                    logger.info(f"[Auditor] Updated existing rec #{update_id} instead of creating duplicate")
                    continue

            # Code-level dedup: if a pending rec already targets the same
            # agent+field+category, update it instead of creating a duplicate.
            dup = db.fetch_one("""
                SELECT id FROM audit_recommendations
                WHERE status = 'pending'
                  AND category = %s AND target_agent = %s AND target_field = %s
                ORDER BY created_at DESC LIMIT 1
            """, (r_cat, r_agent, r_field))
            if dup:
                db.execute("""
                    UPDATE audit_recommendations
                    SET proposed_value = %s,
                        evidence = CONCAT(COALESCE(evidence,''), '\n--- Refreshed by synthesis ---\n', %s),
                        business_justification = %s,
                        expected_impact = %s,
                        severity = %s,
                        diff_summary = %s
                    WHERE id = %s
                """, (
                    (rec.get("proposed_value") or "")[:5000],
                    rec.get("evidence", "")[:2000],
                    rec.get("business_justification", "")[:2000],
                    rec.get("expected_impact", "")[:1000],
                    rec.get("severity", "suggestion"),
                    result.get("synthesis_summary", "")[:500],
                    dup["id"],
                ))
                updated += 1
                logger.info(f"[Auditor] Merged into existing rec #{dup['id']} "
                            f"({r_cat} -> {r_agent}.{r_field})")
                continue

            db.execute("""
                INSERT INTO audit_recommendations
                (audit_report_id, duo_id, category, target_agent, target_field,
                 current_value, proposed_value, diff_summary,
                 evidence, business_justification, expected_impact, severity)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                reports[0]["id"],
                reports[0].get("duo_id"),
                r_cat,
                r_agent,
                r_field,
                (rec.get("current_value") or "")[:5000],
                (rec.get("proposed_value") or "")[:5000],
                result.get("synthesis_summary", "")[:500],
                rec.get("evidence", "")[:2000],
                rec.get("business_justification", "")[:2000],
                rec.get("expected_impact", "")[:1000],
                rec.get("severity", "suggestion"),
            ))
            saved += 1

            # Bridge: prompt/soul changes -> A/B testing challenger
            if rec.get("category") in ("prompt_change", "soul_change", "identity_change"):
                try:
                    _bridge_rec_to_ab_challenger(db, rec)
                except Exception as ab_err:
                    logger.debug(f"[Auditor] A/B bridge error: {ab_err}")
        except Exception as e:
            logger.error(f"[Auditor] Save synthesised rec error: {e}")

    # Process lesson window recommendation from Ledger
    window_rec = result.get("lesson_window_recommendation", {})
    if window_rec and window_rec.get("recommended_wins"):
        try:
            rec_wins = int(window_rec.get("recommended_wins", 25))
            rec_losses = int(window_rec.get("recommended_losses", 25))
            reasoning = window_rec.get("reasoning", "")
            logger.info(f"[Auditor] Lesson window recommendation: "
                        f"wins={rec_wins}, losses={rec_losses} -- {reasoning[:200]}")

            existing_lw = db.fetch_one("""
                SELECT id FROM audit_recommendations
                WHERE category = 'parameter_change' AND target_field = 'lesson_window'
                  AND status = 'pending'
            """)
            if existing_lw:
                db.execute("""
                    UPDATE audit_recommendations
                    SET proposed_value = %s,
                        evidence = CONCAT(COALESCE(evidence,''), '\n--- Updated ---\n', %s),
                        business_justification = %s
                    WHERE id = %s
                """, (
                    f"lesson_window_wins={rec_wins}, lesson_window_losses={rec_losses}",
                    reasoning[:2000],
                    f"Optimise trade context by using {rec_wins} wins and {rec_losses} losses",
                    existing_lw["id"],
                ))
                logger.info(f"[Auditor] Updated existing lesson_window rec #{existing_lw['id']}")
            else:
                db.execute("""
                    INSERT INTO audit_recommendations
                    (audit_report_id, duo_id, category, target_agent, target_field,
                     current_value, proposed_value, diff_summary,
                     evidence, business_justification, expected_impact, severity)
                    VALUES (%s, %s, 'parameter_change', 'system', 'lesson_window',
                            %s, %s, %s, %s, %s, %s, 'suggestion')
                """, (
                    reports[0]["id"],
                    reports[0].get("duo_id"),
                    f"lesson_window_wins=25, lesson_window_losses=25",
                    f"lesson_window_wins={rec_wins}, lesson_window_losses={rec_losses}",
                    f"Ledger recommends adjusting the lesson window based on {len(reports)} audits",
                    reasoning[:2000],
                    f"Optimise trade context by using {rec_wins} wins and {rec_losses} losses",
                    result.get("improvement_trajectory", "")[:500],
                ))
                logger.info(f"[Auditor] Lesson window adjustment recommendation saved "
                            f"(pending CEO approval)")
        except Exception as e:
            logger.debug(f"[Auditor] Lesson window rec save error: {e}")

    # Data-driven threshold adaptation: compute optimal thresholds from trade data
    _first_report_id = reports[0]["id"] if reports else None
    try:
        _propose_adaptive_thresholds(db, _first_report_id)
    except Exception as e:
        logger.debug(f"[Auditor] Adaptive threshold proposal error: {e}")
    try:
        _propose_condition_thresholds(db, _first_report_id)
    except Exception as e:
        logger.debug(f"[Auditor] Condition threshold proposal error: {e}")

    # Pattern discovery: identify recurring winning/losing patterns
    try:
        _discover_strategies(db, config)
    except Exception as e:
        logger.debug(f"[Auditor] Pattern discovery error: {e}")

    # Pattern maturation: age-out stale patterns, promote consistent ones
    try:
        _mature_patterns(db)
    except Exception as e:
        logger.debug(f"[Auditor] Pattern maturation error: {e}")

    # LLM enrichment: merge duplicates, correlate, find strategy candidates
    strategy_candidates = []
    try:
        strategy_candidates = _enrich_patterns(db, config)
    except Exception as e:
        logger.debug(f"[Auditor] Pattern enrichment error: {e}")

    # Strategy graduation: promote qualified pattern groups to draft strategies
    graduated = 0
    for cand in (strategy_candidates or []):
        try:
            if _graduate_patterns_to_strategy(db, config, cand):
                graduated += 1
        except Exception as e:
            logger.debug(f"[Auditor] Graduation error: {e}")
    if graduated:
        logger.info(f"[Auditor] {graduated} new strategy/strategies graduated from patterns")

    logger.info(f"[Auditor] Daily synthesis complete: {len(reports)} audits, "
                f"{len(raw_recs_text)} raw recs -> {saved} new, {updated} updated recommendations")

    _auto_approve_if_autonomous(db, config)

    return saved + updated


def _propose_adaptive_thresholds(db, report_id: int):
    """Analyse recent trade data and propose threshold adjustments if the data
    suggests a better configuration. Covers min_confidence_for_trade and
    opp_score_weights. Only proposes if there's strong statistical evidence."""

    rows = db.fetch_all("""
        SELECT confidence_score, status, realised_pnl
        FROM trade_dossiers
        WHERE status IN ('won','lost')
          AND confidence_score IS NOT NULL
        ORDER BY created_at DESC LIMIT 300
    """)
    if not rows or len(rows) < 50:
        return

    # Find the confidence threshold that maximises cumulative PnL
    best_thresh = 78
    best_pnl = -999999
    for thresh in range(65, 95):
        above = [r for r in rows if (r["confidence_score"] or 0) >= thresh]
        if len(above) < 20:
            continue
        pnl = sum(float(r.get("realised_pnl") or 0) for r in above)
        wins = sum(1 for r in above if r["status"] == "won")
        wr = wins / len(above) * 100
        if pnl > best_pnl and wr >= 50:
            best_pnl = pnl
            best_thresh = thresh

    # Read current threshold from config.json
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        current_thresh = cfg.get("global", {}).get("trade_decision", {}).get(
            "min_confidence_for_trade", 60)
    except Exception:
        current_thresh = 60

    if abs(best_thresh - current_thresh) >= 3:
        # Check for existing pending threshold rec
        existing = db.fetch_one("""
            SELECT id FROM audit_recommendations
            WHERE category = 'threshold_change' AND target_field = 'trade_decision'
              AND status = 'pending'
        """)
        if not existing:
            above_best = [r for r in rows if (r["confidence_score"] or 0) >= best_thresh]
            wins_best = sum(1 for r in above_best if r["status"] == "won")
            pnl_best = sum(float(r.get("realised_pnl") or 0) for r in above_best)
            wr_best = wins_best / len(above_best) * 100

            above_curr = [r for r in rows if (r["confidence_score"] or 0) >= current_thresh]
            wins_curr = sum(1 for r in above_curr if r["status"] == "won")
            pnl_curr = sum(float(r.get("realised_pnl") or 0) for r in above_curr)
            wr_curr = wins_curr / len(above_curr) * 100

            db.execute("""
                INSERT INTO audit_recommendations
                (audit_report_id, category, target_agent, target_field,
                 current_value, proposed_value, diff_summary,
                 evidence, business_justification, expected_impact, severity)
                VALUES (%s, 'threshold_change', 'system', 'trade_decision',
                        %s, %s, %s, %s, %s, %s, 'important')
            """, (
                report_id,
                f"min_confidence_for_trade={current_thresh}",
                f"min_confidence_for_trade={best_thresh}",
                f"Data analysis: optimal confidence threshold is {best_thresh}% "
                f"(currently {current_thresh}%)",
                f"Analysed {len(rows)} recent trades. "
                f"Current threshold ({current_thresh}%): {len(above_curr)} trades, "
                f"{wr_curr:.0f}% WR, ${pnl_curr:.0f} PnL. "
                f"Proposed ({best_thresh}%): {len(above_best)} trades, "
                f"{wr_best:.0f}% WR, ${pnl_best:.0f} PnL.",
                f"Shift threshold from {current_thresh}% to {best_thresh}% to "
                f"maximise PnL while maintaining >50% WR",
                f"Expected WR improvement: {wr_curr:.0f}% -> {wr_best:.0f}%. "
                f"PnL change: ${pnl_curr:.0f} -> ${pnl_best:.0f}",
            ))
            logger.info(f"[Auditor] Adaptive threshold proposal: "
                        f"min_confidence {current_thresh} -> {best_thresh}")


def _propose_condition_thresholds(db, report_id: int):
    """Analyse trigger_probability vs outcomes to find optimal condition
    thresholds (execute + limit_order). Generates audit recommendations
    and lessons learned when thresholds are sub-optimal."""

    rows = db.fetch_all("""
        SELECT trigger_probability, trigger_threshold_execute,
               trigger_threshold_limit, confidence_score,
               status, realised_pnl, realised_pnl_pct
        FROM trade_dossiers
        WHERE status IN ('won', 'lost')
          AND trigger_probability IS NOT NULL
        ORDER BY created_at DESC LIMIT 300
    """)
    if not rows or len(rows) < 20:
        return

    best_exec = 65
    best_exec_pnl = -999999
    best_limit = 50
    best_limit_pnl = -999999

    for thresh in range(40, 95, 5):
        above = [r for r in rows if (r["trigger_probability"] or 0) >= thresh]
        if len(above) < 10:
            continue
        pnl = sum(float(r.get("realised_pnl") or 0) for r in above)
        wins = sum(1 for r in above if r["status"] == "won")
        wr = wins / len(above) * 100
        if pnl > best_exec_pnl and wr >= 45:
            best_exec_pnl = pnl
            best_exec = thresh

    for thresh in range(30, 80, 5):
        above = [r for r in rows if (r["trigger_probability"] or 0) >= thresh]
        if len(above) < 10:
            continue
        pnl = sum(float(r.get("realised_pnl") or 0) for r in above)
        wins = sum(1 for r in above if r["status"] == "won")
        if pnl > best_limit_pnl:
            best_limit_pnl = pnl
            best_limit = thresh

    current_exec = rows[0].get("trigger_threshold_execute") or 65
    current_limit = rows[0].get("trigger_threshold_limit") or 50

    if abs(best_exec - current_exec) >= 5:
        above_best = [r for r in rows if (r["trigger_probability"] or 0) >= best_exec]
        above_curr = [r for r in rows if (r["trigger_probability"] or 0) >= current_exec]
        pnl_b = sum(float(r.get("realised_pnl") or 0) for r in above_best)
        pnl_c = sum(float(r.get("realised_pnl") or 0) for r in above_curr)
        wr_b = sum(1 for r in above_best if r["status"] == "won") / max(len(above_best), 1) * 100
        wr_c = sum(1 for r in above_curr if r["status"] == "won") / max(len(above_curr), 1) * 100

        existing = db.fetch_one("""
            SELECT id FROM audit_recommendations
            WHERE category = 'threshold_change' AND target_field = 'condition_threshold_execute'
              AND status = 'pending'
        """)
        if not existing:
            try:
                db.execute("""
                    INSERT INTO audit_recommendations
                    (audit_report_id, category, target_agent, target_field,
                     current_value, proposed_value, diff_summary,
                     evidence, business_justification, expected_impact, severity)
                    VALUES (%s, 'threshold_change', 'system', 'condition_threshold_execute',
                            %s, %s, %s, %s, %s, %s, 'important')
                """, (
                    report_id,
                    f"condition_threshold_execute={current_exec}",
                    f"condition_threshold_execute={best_exec}",
                    f"Trigger probability analysis: optimal execute threshold is "
                    f"{best_exec}% (currently {current_exec}%)",
                    f"Analysed {len(rows)} completed trades with trigger data. "
                    f"Current ({current_exec}%): {len(above_curr)} trades, "
                    f"{wr_c:.0f}% WR, ${pnl_c:.0f} PnL. "
                    f"Proposed ({best_exec}%): {len(above_best)} trades, "
                    f"{wr_b:.0f}% WR, ${pnl_b:.0f} PnL.",
                    f"Shift execute threshold from {current_exec}% to {best_exec}%",
                    f"WR: {wr_c:.0f}% -> {wr_b:.0f}%. "
                    f"PnL: ${pnl_c:.0f} -> ${pnl_b:.0f}",
                ))
                logger.info(f"[Auditor] Condition threshold proposal: "
                            f"execute {current_exec} -> {best_exec}")
            except Exception as e:
                logger.debug(f"[Auditor] condition threshold rec: {e}")

    # Generate lessons learned for trade_lessons table
    try:
        bands = {}
        for r in rows:
            tp = r.get("trigger_probability") or 0
            bucket = (tp // 10) * 10
            bands.setdefault(bucket, {"wins": 0, "losses": 0, "pnl": 0})
            if r["status"] == "won":
                bands[bucket]["wins"] += 1
            else:
                bands[bucket]["losses"] += 1
            bands[bucket]["pnl"] += float(r.get("realised_pnl") or 0)

        for bucket, stats in sorted(bands.items()):
            total = stats["wins"] + stats["losses"]
            if total < 5:
                continue
            wr = stats["wins"] / total * 100
            avg_pnl = stats["pnl"] / total
            lesson = (
                f"Trigger probability {int(bucket)}-{int(bucket)+9}%: "
                f"{total} trades, {wr:.0f}% WR, avg P&L ${avg_pnl:.2f}, "
                f"cumulative ${stats['pnl']:.2f}. "
                f"{'Sweet spot — high WR and positive P&L.' if wr >= 55 and stats['pnl'] > 0 else ''}"
                f"{'Danger zone — negative P&L despite trades being triggered.' if stats['pnl'] < 0 else ''}"
            )
            db.execute("""
                INSERT INTO trade_lessons
                (symbol, lesson_type, lesson_text, source_model, source_trade_id)
                VALUES ('_SYSTEM', 'threshold_analysis', %s, 'auditor_threshold', NULL)
            """, (lesson,))
    except Exception as e:
        logger.debug(f"[Auditor] threshold lessons: {e}")


def _retire_underperforming_strategies(db):
    """Suspend strategies with poor recent performance.

    Uses 30-day rolling window from trade_dossiers; falls back to lifetime
    (trading_strategies.total_*) if < 10 trades in 30 days.

    Criteria for suspension:
    - At least 10 trades completed with this strategy
    - Win rate below 35% OR cumulative P&L below -$50
    Also auto-activates graduated draft strategies with 20+ trades and 60%+ WR.
    """
    # Retire underperformers: prefer 30-day stats, fallback to lifetime
    active = db.fetch_all("""
        SELECT s.id, s.name, s.total_trades, s.total_wins, s.total_pnl,
               COALESCE(r.recent_cnt, 0) as recent_cnt,
               COALESCE(r.recent_wins, 0) as recent_wins,
               COALESCE(r.recent_losses, 0) as recent_losses,
               COALESCE(r.recent_pnl, 0) as recent_pnl
        FROM trading_strategies s
        LEFT JOIN (
            SELECT strategy_id,
                   COUNT(*) as recent_cnt,
                   SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) as recent_wins,
                   SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) as recent_losses,
                   SUM(COALESCE(realised_pnl, live_pnl, 0)) as recent_pnl
            FROM trade_dossiers
            WHERE status IN ('won', 'lost')
              AND created_at >= NOW() - INTERVAL 30 DAY
              AND strategy_id IS NOT NULL
            GROUP BY strategy_id
        ) r ON r.strategy_id = s.id
        WHERE s.status = 'active' AND s.total_trades >= 10
    """)
    for s in (active or []):
        recent_cnt = int(s.get("recent_cnt", 0) or 0)
        if recent_cnt >= 10:
            total = recent_cnt
            wins = int(s.get("recent_wins", 0) or 0)
            pnl = float(s.get("recent_pnl", 0) or 0)
        else:
            total = s.get("total_trades", 0)
            wins = s.get("total_wins", 0)
            pnl = float(s.get("total_pnl", 0) or 0)
        wr = (wins / total * 100) if total > 0 else 0
        if wr < 35 or pnl < -50:
            db.execute(
                "UPDATE trading_strategies SET status = 'suspended' WHERE id = %s",
                (s["id"],))
            logger.info(f"[Auditor] Suspended strategy '{s['name']}' "
                        f"(id={s['id']}): WR={wr:.0f}%, P&L=${pnl:.2f}")

    # Auto-activate graduated drafts with sufficient evidence
    drafts = db.fetch_all(
        "SELECT id, name, total_trades, total_wins, total_pnl "
        "FROM trading_strategies WHERE status = 'draft' AND auto_discovered = 1 "
        "AND total_trades >= 20")
    for d in (drafts or []):
        total = d.get("total_trades", 0)
        wins = d.get("total_wins", 0)
        wr = (wins / total * 100) if total > 0 else 0
        if wr >= 60 and float(d.get("total_pnl", 0) or 0) > 0:
            db.execute(
                "UPDATE trading_strategies SET status = 'active' WHERE id = %s",
                (d["id"],))
            logger.info(f"[Auditor] Auto-activated strategy '{d['name']}' "
                        f"(id={d['id']}): WR={wr:.0f}%, trades={total}")


def _resolve_ab_tests(db, min_samples: int = None):
    """Auto-resolve A/B prompt tests once enough dossiers use each variant.

    For each role (analyst, trader), find inactive challengers with a
    parent_version_id (meaning they were created as A/B challengers).
    Compare win-rate + avg P&L of dossiers using the champion vs challenger.
    If the challenger significantly outperforms (configurable WR threshold
    or >10% avg P&L), promote it; otherwise retire it.

    Configurable via system_config:
      ab_test_min_samples         — min dossiers per variant (default 20)
      ab_test_se_multiplier       — significance guard multiplier (default 2.0)
      ab_test_promote_wr_delta    — WR delta in pp to trigger promotion (default 5)
    """
    cfg_min = get_system_config_int(db, "ab_test_min_samples", 20)
    cfg_se_mult = get_system_config_float(db, "ab_test_se_multiplier", 2.0)
    cfg_wr_delta = get_system_config_float(db, "ab_test_promote_wr_delta", 5.0)
    if min_samples is None:
        min_samples = cfg_min

    challengers = db.fetch_all("""
        SELECT id, role, parent_version_id, prompt_name
        FROM prompt_versions
        WHERE is_active = 0
          AND parent_version_id IS NOT NULL
          AND changed_by = 'ledger_synthesis'
    """)
    for ch in (challengers or []):
        ch_id = ch["id"]
        parent_id = ch["parent_version_id"]

        ch_stats = db.fetch_one("""
            SELECT COUNT(*) AS cnt,
                   SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) AS wins,
                   AVG(realised_pnl) AS avg_pnl
            FROM trade_dossiers
            WHERE prompt_version_id = %s AND status IN ('won','lost')
        """, (ch_id,))

        parent_stats = db.fetch_one("""
            SELECT COUNT(*) AS cnt,
                   SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) AS wins,
                   AVG(realised_pnl) AS avg_pnl
            FROM trade_dossiers
            WHERE prompt_version_id = %s AND status IN ('won','lost')
        """, (parent_id,))

        ch_cnt = int(ch_stats.get("cnt", 0) or 0) if ch_stats else 0
        parent_cnt = int(parent_stats.get("cnt", 0) or 0) if parent_stats else 0
        if ch_cnt < min_samples or parent_cnt < min_samples:
            continue

        ch_wr = (int(ch_stats["wins"] or 0) / ch_cnt * 100) if ch_cnt else 0
        p_wr = (int(parent_stats["wins"] or 0) / parent_cnt * 100) if parent_cnt else 0
        ch_pnl = float(ch_stats.get("avg_pnl") or 0)
        p_pnl = float(parent_stats.get("avg_pnl") or 0)

        ch_wins = int(ch_stats.get("wins", 0) or 0)
        p_wins = int(parent_stats.get("wins", 0) or 0)
        p_pooled = (ch_wins + p_wins) / (ch_cnt + parent_cnt) if (ch_cnt + parent_cnt) > 0 else 0.5
        se = math.sqrt(p_pooled * (1 - p_pooled) * (1 / ch_cnt + 1 / parent_cnt)) * 100
        wr_delta = ch_wr - p_wr
        significance_threshold = cfg_se_mult * se
        if abs(wr_delta) <= significance_threshold:
            logger.debug(f"[Auditor] A/B skip challenger #{ch_id}: "
                         f"|wr_delta|={abs(wr_delta):.1f}pp <= "
                         f"{cfg_se_mult}*SE={significance_threshold:.1f}pp")
            continue

        if p_pnl <= 0 and ch_pnl <= 0:
            continue
        pnl_better = ch_pnl > p_pnl * 1.10 if p_pnl > 0 else ch_pnl > p_pnl

        if wr_delta > cfg_wr_delta or pnl_better:
            db.execute("UPDATE prompt_versions SET is_active = 0 WHERE id = %s",
                       (parent_id,))
            db.execute("UPDATE prompt_versions SET is_active = 1 WHERE id = %s",
                       (ch_id,))
            logger.info(f"[Auditor] A/B PROMOTED challenger #{ch_id} over "
                        f"champion #{parent_id} (WR: {ch_wr:.0f}% vs {p_wr:.0f}%, "
                        f"P&L: ${ch_pnl:.2f} vs ${p_pnl:.2f})")
        else:
            db.execute(
                "UPDATE prompt_versions SET is_active = 0, "
                "change_reason = CONCAT(COALESCE(change_reason,''), "
                "' | RETIRED: underperformed champion') WHERE id = %s",
                (ch_id,))
            logger.info(f"[Auditor] A/B RETIRED challenger #{ch_id} vs "
                        f"champion #{parent_id} (WR: {ch_wr:.0f}% vs {p_wr:.0f}%)")


def _discover_strategies(db, config, min_trades: int = 10):
    """Analyse recent winning and losing trades to identify recurring patterns.
    Creates strategy proposals (winning patterns) and anti-strategy proposals
    (losing patterns to avoid) in trading_strategies with status='draft'.

    Runs after daily synthesis when enough trades have accumulated.
    min_trades configurable via system_config 'strategy_discovery_min_trades'."""
    try:
        from db.database import get_system_config
        cfg_min = get_system_config(db, "strategy_discovery_min_trades")
        if cfg_min:
            min_trades = int(cfg_min)
    except Exception:
        pass

    completed = db.fetch_all("""
        SELECT id, symbol, direction, entry_price, stop_loss, take_profit_1,
               status, realised_pnl, realised_pnl_pct, confidence_score,
               leverage, stage2_hypothesis, conditions_for_entry,
               apex_entry_reasoning, mentor_source, strategy_id
        FROM trade_dossiers
        WHERE status IN ('won','lost')
          AND confidence_score IS NOT NULL
          AND strategy_id IS NULL
        ORDER BY created_at DESC LIMIT 200
    """)
    if not completed or len(completed) < min_trades:
        logger.debug(f"[Auditor] Strategy discovery: insufficient trades "
                     f"({len(completed or [])} < {min_trades})")
        return

    wins = [t for t in completed if t["status"] == "won"]
    losses = [t for t in completed if t["status"] == "lost"]

    if len(wins) < 5 and len(losses) < 5:
        return

    # Build trade summaries for the LLM
    def _summarise(trades, label, limit=30):
        lines = []
        for t in trades[:limit]:
            hyp = ""
            if t.get("stage2_hypothesis"):
                try:
                    h = json.loads(t["stage2_hypothesis"]) if isinstance(
                        t["stage2_hypothesis"], str) else t["stage2_hypothesis"]
                    hyp = h.get("hypothesis", "")[:150] if isinstance(h, dict) else str(h)[:150]
                except Exception:
                    pass
            conds = ""
            if t.get("conditions_for_entry"):
                try:
                    c = json.loads(t["conditions_for_entry"]) if isinstance(
                        t["conditions_for_entry"], str) else t["conditions_for_entry"]
                    if isinstance(c, list):
                        conds = "; ".join(str(x.get("description", x))[:60] for x in c[:3])
                except Exception:
                    pass
            mentor = f" (mentor: {t['mentor_source']})" if t.get("mentor_source") else ""
            lines.append(
                f"  #{t['id']} {t['symbol']} {t['direction']} | "
                f"Entry={t['entry_price']} SL={t['stop_loss']} TP1={t['take_profit_1']} | "
                f"PnL=${t.get('realised_pnl', 0)} ({t.get('realised_pnl_pct', 0)}%) "
                f"Conf={t.get('confidence_score')}% Lev={t.get('leverage')}x{mentor}\n"
                f"    Hypothesis: {hyp}\n"
                f"    Conditions: {conds}")
        return f"\n{label} ({len(trades)} total, showing {min(limit, len(trades))}):\n" + "\n".join(lines)

    trade_context = _summarise(wins, "WINNING TRADES") + "\n\n" + _summarise(losses, "LOSING TRADES")

    prompt = f"""You are Ledger, the chief auditor. Analyse these {len(completed)} recent trades
and identify RECURRING PATTERNS — both winning and losing.

{_CRYPTO_CONTEXT}

These are OBSERVATIONS to track over time, not trading strategies.
Be as specific as possible about symbol, timeframe, session, and setup.
Consider crypto-specific patterns: session liquidity sweeps, funding rate flips,
exchange-specific wicks, AMD cycle phases, weekend vs weekday behaviour.

{trade_context}

Respond with JSON:
{{
  "patterns": [
    {{
      "name": "<short descriptive name>",
      "type": "winning|losing",
      "symbol": "<specific symbol or 'MULTI' if across symbols>",
      "market_class": "cryptocurrency",
      "timeframe": "<M5|M15|H1|H4|D1 or 'MULTI'>",
      "session": "<asian|london|new_york|overlap or 'any'>",
      "direction": "<long|short|both>",
      "setup_type": "<e.g. liquidity_grab, false_breakout, bos_continuation, etc>",
      "description": "<what this pattern is and why it works/fails>",
      "lesson": "<1-2 sentence actionable takeaway>",
      "evidence_ids": [<list of dossier IDs that demonstrate this>],
      "frequency": <how many trades match>,
      "win_rate": <0-100>
    }}
  ]
}}

RULES:
- Minimum 3 trades must match a pattern
- Be specific: name the symbol, timeframe, session, setup type
- If a pattern spans multiple symbols, use "MULTI" but note which symbols
- Maximum 3 winning + 3 losing patterns per analysis"""

    try:
        auditor_model, auditor_provider = _get_auditor_model_and_provider(db)
        jresp = _call_llm_json(
            db, auditor_model, auditor_provider,
            _get_auditor_identity(db),
            prompt,
            required_keys=["patterns"],
            context="pattern_discovery",
            max_tokens=4096, temperature=0.2)
        result = jresp.get("parsed")
        if not result:
            return
    except Exception as e:
        logger.error(f"[Auditor] Pattern discovery LLM error: {e}")
        return

    saved = 0
    for pat in result.get("patterns", [])[:6]:
        try:
            name = pat.get("name", "")[:200]
            if not name:
                continue
            p_type = pat.get("type", "neutral")
            symbol = pat.get("symbol", "")[:30] or None
            evidence = pat.get("evidence_ids", [])
            freq = int(pat.get("frequency", 1) or 1)
            wr = float(pat.get("win_rate", 0) or 0)
            win_c = int(freq * wr / 100) if freq and wr else 0
            loss_c = freq - win_c

            # Calculate actual P&L from evidence dossier IDs
            avg_pnl = 0.0
            if evidence and isinstance(evidence, list):
                int_ids = [int(e) for e in evidence if str(e).isdigit()]
                if int_ids:
                    placeholders = ",".join(["%s"] * len(int_ids))
                    pnl_row = db.fetch_one(
                        f"SELECT AVG(COALESCE(realised_pnl, live_pnl, 0)) as ap "
                        f"FROM trade_dossiers WHERE id IN ({placeholders})",
                        tuple(int_ids))
                    avg_pnl = round(float(pnl_row["ap"] or 0), 4) if pnl_row else 0.0

            # Check if a similar pattern already exists (same name or same
            # symbol+setup_type combo) and update it instead.
            existing = db.fetch_one(
                "SELECT id, occurrences, win_count, loss_count, evidence_ids "
                "FROM trade_patterns WHERE pattern_name = %s", (name,))
            if not existing and symbol and pat.get("setup_type"):
                existing = db.fetch_one(
                    "SELECT id, occurrences, win_count, loss_count, evidence_ids "
                    "FROM trade_patterns WHERE symbol = %s AND setup_type = %s",
                    (symbol, pat.get("setup_type", "")[:100]))

            if existing:
                old_ev = []
                if existing.get("evidence_ids"):
                    try:
                        old_ev = json.loads(existing["evidence_ids"]) if isinstance(
                            existing["evidence_ids"], str) else existing["evidence_ids"]
                    except Exception:
                        old_ev = []
                merged_ev = list(set(old_ev + (evidence if isinstance(evidence, list) else [])))

                new_occ = (existing["occurrences"] or 0) + freq
                new_wins = (existing["win_count"] or 0) + win_c
                new_losses = (existing["loss_count"] or 0) + loss_c
                new_wr = round(new_wins / new_occ * 100, 1) if new_occ else 0
                new_maturity = min(100, new_occ * 5)

                status = "emerging"
                if new_occ >= 20:
                    status = "mature"
                elif new_occ >= 8:
                    status = "maturing"

                # Recalculate avg_pnl from all merged evidence
                all_ev_ints = [int(e) for e in merged_ev if str(e).isdigit()]
                if all_ev_ints:
                    ph = ",".join(["%s"] * len(all_ev_ints))
                    prow = db.fetch_one(
                        f"SELECT AVG(COALESCE(realised_pnl, live_pnl, 0)) as ap "
                        f"FROM trade_dossiers WHERE id IN ({ph})",
                        tuple(all_ev_ints))
                    avg_pnl = round(float(prow["ap"] or 0), 4) if prow else avg_pnl

                db.execute("""
                    UPDATE trade_patterns
                    SET occurrences = %s, win_count = %s, loss_count = %s,
                        win_rate = %s, maturity_score = %s, status = %s,
                        evidence_ids = %s, last_seen = NOW(),
                        description = %s, lesson = %s, avg_pnl = %s
                    WHERE id = %s
                """, (new_occ, new_wins, new_losses, new_wr, new_maturity,
                      status, json.dumps(merged_ev[-50:]),
                      pat.get("description", "")[:2000],
                      pat.get("lesson", "")[:1000],
                      avg_pnl, existing["id"]))
                saved += 1
                logger.info(f"[Auditor] Updated pattern '{name}': "
                            f"occ={new_occ}, WR={new_wr}%, maturity={status}")
            else:
                maturity = min(100, freq * 5)
                status = "emerging" if freq < 8 else ("maturing" if freq < 20 else "mature")
                db.execute("""
                    INSERT INTO trade_patterns
                    (pattern_name, pattern_type, symbol, market_class, timeframe,
                     session, direction, setup_type, description, lesson,
                     evidence_ids, occurrences, win_count, loss_count,
                     win_rate, maturity_score, status, avg_pnl)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    name, p_type, symbol,
                    pat.get("market_class", "")[:30] or None,
                    pat.get("timeframe", "")[:10] or None,
                    pat.get("session", "")[:20] or None,
                    pat.get("direction", "")[:10] or None,
                    pat.get("setup_type", "")[:100] or None,
                    pat.get("description", "")[:2000],
                    pat.get("lesson", "")[:1000],
                    json.dumps(evidence[:50] if isinstance(evidence, list) else []),
                    freq, win_c, loss_c, wr, maturity, status, avg_pnl,
                ))
                saved += 1
                logger.info(f"[Auditor] New pattern '{name}' ({p_type}): "
                            f"occ={freq}, WR={wr}%, status={status}")
        except Exception as e:
            logger.debug(f"[Auditor] Pattern save error: {e}")

    if saved:
        logger.info(f"[Auditor] Pattern discovery: {saved} patterns saved/updated "
                    f"in trade_patterns table")


def _mature_patterns(db):
    """Daily pattern maturation: update statuses, composite scores, and P&L.
    - Patterns not seen in 14+ days -> 'stale'
    - Patterns with 8+ occurrences -> 'maturing'
    - Patterns with 20+ occurrences -> 'mature'
    - Recalculate maturity_score = min(100, occurrences * 5)
    - Composite score = (maturity/100) * (win_rate/100) * pnl_factor * age_factor
    """
    db.execute("""
        UPDATE trade_patterns
        SET status = 'stale'
        WHERE status != 'stale'
          AND last_seen < DATE_SUB(NOW(), INTERVAL 14 DAY)
    """)

    active = db.fetch_all("""
        SELECT id, occurrences, win_count, loss_count, avg_pnl,
               evidence_ids, first_seen, DATEDIFF(NOW(), first_seen) as age_days
        FROM trade_patterns
        WHERE status != 'stale'
    """)
    for p in (active or []):
        occ = p["occurrences"] or 0
        maturity = min(100, occ * 5)
        if occ >= 20:
            status = "mature"
        elif occ >= 8:
            status = "maturing"
        else:
            status = "emerging"
        total = (p["win_count"] or 0) + (p["loss_count"] or 0)
        wr = round((p["win_count"] or 0) / total * 100, 1) if total else 0

        # Refresh avg_pnl from evidence dossiers
        current_pnl = float(p.get("avg_pnl") or 0)
        ev_raw = p.get("evidence_ids")
        if ev_raw:
            try:
                ev_list = json.loads(ev_raw) if isinstance(ev_raw, str) else ev_raw
                int_ids = [int(e) for e in (ev_list or []) if str(e).isdigit()]
                if int_ids:
                    ph = ",".join(["%s"] * len(int_ids))
                    prow = db.fetch_one(
                        f"SELECT AVG(COALESCE(realised_pnl, live_pnl, 0)) as ap "
                        f"FROM trade_dossiers WHERE id IN ({ph})", tuple(int_ids))
                    current_pnl = round(float(prow["ap"] or 0), 4) if prow else current_pnl
            except Exception:
                pass

        # Composite score: leaderboard ranking
        age_days = float(p.get("age_days") or 0)
        age_factor = min(1.0, age_days / 7.0) if age_days > 0 else 0.0
        pnl_factor = 1.0 + max(-0.5, min(2.0, current_pnl / 10.0))
        composite = round(
            (maturity / 100.0) * (wr / 100.0) * pnl_factor * age_factor, 2)

        db.execute("""
            UPDATE trade_patterns
            SET maturity_score = %s, status = %s, win_rate = %s,
                avg_pnl = %s, composite_score = %s, last_reviewed = NOW()
            WHERE id = %s
        """, (maturity, status, wr, current_pnl, composite, p["id"]))

    stale_cnt = db.fetch_one(
        "SELECT COUNT(*) as c FROM trade_patterns WHERE status = 'stale'")
    mature_cnt = db.fetch_one(
        "SELECT COUNT(*) as c FROM trade_patterns WHERE status = 'mature'")
    total_cnt = db.fetch_one("SELECT COUNT(*) as c FROM trade_patterns")
    logger.info(f"[Auditor] Pattern maturation: {total_cnt['c']} total, "
                f"{mature_cnt['c']} mature, {stale_cnt['c']} stale")


def _enrich_patterns(db, config):
    """LLM cross-pattern analysis: merge duplicates, correlate related patterns,
    and identify strategy candidates from maturing/mature patterns.
    Only runs when 3+ active patterns exist."""
    active = db.fetch_all("""
        SELECT id, pattern_name, pattern_type, symbol, timeframe, session,
               direction, setup_type, description, lesson, occurrences,
               win_count, loss_count, win_rate, avg_pnl, composite_score,
               status, related_pattern_ids
        FROM trade_patterns
        WHERE status IN ('maturing', 'mature')
        ORDER BY composite_score DESC
        LIMIT 30
    """)
    if not active or len(active) < 3:
        logger.debug("[Auditor] Enrichment skipped: <3 maturing/mature patterns")
        return []

    pattern_summaries = []
    for p in active:
        pattern_summaries.append(
            f"ID:{p['id']} | {p['pattern_name']} | type={p['pattern_type']} | "
            f"symbol={p['symbol']} | tf={p['timeframe']} | session={p['session']} | "
            f"dir={p['direction']} | setup={p['setup_type']} | "
            f"occ={p['occurrences']} | WR={p['win_rate']}% | PnL={p['avg_pnl']} | "
            f"score={p['composite_score']} | status={p['status']}\n"
            f"  Desc: {(p['description'] or '')[:200]}\n"
            f"  Lesson: {(p['lesson'] or '')[:150]}")

    prompt = f"""You are JarvAIs' Pattern Intelligence Analyst. Analyse these {len(active)} trade patterns
and identify relationships between them.

PATTERNS:
{chr(10).join(pattern_summaries)}

TASKS:
1. MERGE: Find patterns that describe the same concept but have different names. Return their IDs.
2. CORRELATE: Find patterns that complement each other (e.g. a setup pattern + a timing pattern).
3. STRATEGY CANDIDATES: Identify groups of 2-4 patterns that together could form a coherent
   trading strategy. These should have a combined win rate >= 65%, positive P&L, and represent
   a complete trading approach (entry, timing, risk, exit).

Return ONLY valid JSON:
{{
  "merges": [
    {{"keep_id": <int>, "merge_id": <int>, "reason": "<why they're duplicates>"}}
  ],
  "correlations": [
    {{"pattern_ids": [<int>, <int>], "relationship": "<how they relate>"}}
  ],
  "strategy_candidates": [
    {{
      "pattern_ids": [<int>, ...],
      "name": "<proposed strategy name>",
      "thesis": "<1-2 sentence strategy thesis>",
      "combined_win_rate": <float>,
      "combined_pnl": <float>
    }}
  ]
}}

RULES:
- Only merge if they truly describe the same observation
- Correlations should be actionable (patterns that work TOGETHER)
- Strategy candidates need 2-4 patterns minimum, combined WR >= 65%, positive P&L
- Be conservative: only propose strong, clear groupings"""

    try:
        auditor_model, auditor_provider = _get_auditor_model_and_provider(db)
        jresp = _call_llm_json(
            db, auditor_model, auditor_provider,
            "You are a quantitative crypto perpetual futures pattern "
            "analyst. You identify relationships between trading "
            "patterns in 24/7 USDT-margined cryptocurrency markets.",
            prompt,
            required_keys=["merges"],
            context="pattern_enrichment",
            max_tokens=4096, temperature=0.2)
        result = jresp.get("parsed")
        if not result:
            logger.debug("[Auditor] Enrichment: no valid JSON from LLM")
            return []
    except Exception as e:
        logger.error(f"[Auditor] Enrichment LLM error: {e}")
        return []

    # Process merges
    for merge in result.get("merges", [])[:5]:
        try:
            keep_id = int(merge["keep_id"])
            merge_id = int(merge["merge_id"])
            if keep_id == merge_id:
                continue
            keep_row = db.fetch_one(
                "SELECT evidence_ids, occurrences, win_count, loss_count "
                "FROM trade_patterns WHERE id = %s", (keep_id,))
            merge_row = db.fetch_one(
                "SELECT evidence_ids, occurrences, win_count, loss_count "
                "FROM trade_patterns WHERE id = %s", (merge_id,))
            if not keep_row or not merge_row:
                continue

            def _parse_ev(raw):
                if not raw:
                    return []
                try:
                    return json.loads(raw) if isinstance(raw, str) else (raw or [])
                except Exception:
                    return []

            merged_ev = list(set(_parse_ev(keep_row["evidence_ids"]) +
                                 _parse_ev(merge_row["evidence_ids"])))
            new_occ = (keep_row["occurrences"] or 0) + (merge_row["occurrences"] or 0)
            new_wins = (keep_row["win_count"] or 0) + (merge_row["win_count"] or 0)
            new_losses = (keep_row["loss_count"] or 0) + (merge_row["loss_count"] or 0)
            new_wr = round(new_wins / new_occ * 100, 1) if new_occ else 0

            db.execute("""
                UPDATE trade_patterns
                SET occurrences = %s, win_count = %s, loss_count = %s,
                    win_rate = %s, evidence_ids = %s, maturity_score = %s
                WHERE id = %s
            """, (new_occ, new_wins, new_losses, new_wr,
                  json.dumps(merged_ev[-50:]), min(100, new_occ * 5), keep_id))

            db.execute(
                "UPDATE trade_patterns SET status = 'stale' WHERE id = %s",
                (merge_id,))
            logger.info(f"[Auditor] Merged pattern #{merge_id} into #{keep_id}: "
                        f"{merge.get('reason', '')[:80]}")
        except Exception as e:
            logger.debug(f"[Auditor] Merge error: {e}")

    # Process correlations
    for corr in result.get("correlations", [])[:10]:
        try:
            pids = [int(x) for x in corr.get("pattern_ids", [])]
            if len(pids) < 2:
                continue
            for pid in pids:
                existing = db.fetch_one(
                    "SELECT related_pattern_ids FROM trade_patterns WHERE id = %s",
                    (pid,))
                if not existing:
                    continue
                try:
                    current = json.loads(existing["related_pattern_ids"]) \
                        if existing.get("related_pattern_ids") else []
                except Exception:
                    current = []
                others = [x for x in pids if x != pid]
                updated = list(set(current + others))
                db.execute(
                    "UPDATE trade_patterns SET related_pattern_ids = %s WHERE id = %s",
                    (json.dumps(updated), pid))
        except Exception as e:
            logger.debug(f"[Auditor] Correlation error: {e}")

    candidates = result.get("strategy_candidates", [])
    if candidates:
        logger.info(f"[Auditor] Enrichment found {len(candidates)} strategy candidate(s)")
    else:
        logger.info("[Auditor] Enrichment: no strategy candidates identified")
    return candidates


def _graduate_patterns_to_strategy(db, config, candidate: dict):
    """Promote a qualified pattern group to a draft strategy with structured rules.

    Graduation criteria (all must be met):
    - Pattern group has 20+ combined occurrences
    - Combined win rate >= 65%
    - Positive aggregate P&L across evidence dossiers
    - Patterns seen consistently for 7+ days
    - At least 3 unique symbols OR 15+ trades in evidence
    - No existing active/draft strategy already covers same pattern group
    """
    pattern_ids = [int(x) for x in candidate.get("pattern_ids", [])]
    if len(pattern_ids) < 2:
        return False

    placeholders = ",".join(["%s"] * len(pattern_ids))
    patterns = db.fetch_all(
        f"SELECT id, pattern_name, pattern_type, symbol, timeframe, session, "
        f"direction, setup_type, description, lesson, occurrences, "
        f"win_count, loss_count, win_rate, avg_pnl, composite_score, "
        f"evidence_ids, first_seen, last_seen, graduated_strategy_id "
        f"FROM trade_patterns WHERE id IN ({placeholders})",
        tuple(pattern_ids))

    if not patterns or len(patterns) < 2:
        return False

    # Already graduated?
    if any(p.get("graduated_strategy_id") for p in patterns):
        logger.debug("[Auditor] Graduation skipped: pattern(s) already graduated")
        return False

    # Aggregate metrics
    total_occ = sum(p["occurrences"] or 0 for p in patterns)
    total_wins = sum(p["win_count"] or 0 for p in patterns)
    total_losses = sum(p["loss_count"] or 0 for p in patterns)
    combined_wr = round(total_wins / total_occ * 100, 1) if total_occ else 0

    # Aggregate evidence IDs for P&L calculation
    all_evidence = []
    for p in patterns:
        ev_raw = p.get("evidence_ids")
        if ev_raw:
            try:
                ev = json.loads(ev_raw) if isinstance(ev_raw, str) else (ev_raw or [])
                all_evidence.extend(ev)
            except Exception:
                pass
    all_evidence = list(set(all_evidence))
    int_evidence = [int(e) for e in all_evidence if str(e).isdigit()]

    agg_pnl = 0.0
    if int_evidence:
        ph = ",".join(["%s"] * len(int_evidence))
        row = db.fetch_one(
            f"SELECT SUM(COALESCE(realised_pnl, live_pnl, 0)) as total_pnl "
            f"FROM trade_dossiers WHERE id IN ({ph})", tuple(int_evidence))
        agg_pnl = float(row["total_pnl"] or 0) if row else 0.0

    # Unique symbols
    symbols = set()
    for p in patterns:
        s = p.get("symbol") or ""
        if s and s != "MULTI":
            symbols.add(s)

    # Age span
    first_dates = [p["first_seen"] for p in patterns if p.get("first_seen")]
    last_dates = [p["last_seen"] for p in patterns if p.get("last_seen")]
    age_span_days = 0
    if first_dates and last_dates:
        try:
            earliest = min(first_dates)
            latest = max(last_dates)
            age_span_days = (latest - earliest).days
        except Exception:
            pass

    # Check graduation criteria
    fails = []
    if total_occ < 20:
        fails.append(f"occurrences={total_occ} < 20")
    if combined_wr < 65:
        fails.append(f"win_rate={combined_wr}% < 65%")
    if agg_pnl <= 0:
        fails.append(f"aggregate_pnl={agg_pnl:.2f} <= 0")
    if age_span_days < 7:
        fails.append(f"age_span={age_span_days}d < 7d")
    if len(symbols) < 3 and len(int_evidence) < 15:
        fails.append(f"symbols={len(symbols)}, evidence={len(int_evidence)} "
                      f"(need 3+ symbols or 15+ trades)")

    if fails:
        logger.debug(f"[Auditor] Graduation criteria not met for "
                     f"'{candidate.get('name', '?')}': {'; '.join(fails)}")
        return False

    # Check no existing strategy covers these patterns
    existing_strat = db.fetch_one(
        "SELECT id FROM trading_strategies "
        "WHERE source_pattern_ids IS NOT NULL AND status IN ('active', 'draft')")
    if existing_strat:
        try:
            existing_pids = json.loads(
                existing_strat.get("source_pattern_ids") or "[]")
            if set(pattern_ids).issubset(set(existing_pids)):
                logger.debug("[Auditor] Strategy already exists for these patterns")
                return False
        except Exception:
            pass

    # Generate structured rules via LLM
    pattern_descriptions = "\n".join(
        f"- {p['pattern_name']}: {(p['description'] or '')[:300]}\n"
        f"  Lesson: {(p['lesson'] or '')[:200]}\n"
        f"  WR={p['win_rate']}%, PnL={p['avg_pnl']}, "
        f"Symbol={p['symbol']}, TF={p['timeframe']}, "
        f"Session={p['session']}, Setup={p['setup_type']}"
        for p in patterns)

    rules_prompt = f"""You are creating a detailed cryptocurrency perpetual futures strategy from proven trade patterns.
{_CRYPTO_CONTEXT}

STRATEGY NAME: {candidate.get('name', 'Auto-Discovered Strategy')}
THESIS: {candidate.get('thesis', '')}
COMBINED METRICS: WR={combined_wr}%, Total P&L=${agg_pnl:.2f}, Trades={len(int_evidence)}, Patterns={len(patterns)}

SOURCE PATTERNS:
{pattern_descriptions}

Generate a COMPLETE trading strategy with 10+ numbered rules covering:
1. Market conditions required (trend, volatility, session)
2. Entry criteria (specific setups, confirmations needed)
3. Stop loss placement rules
4. Take profit levels and scaling
5. Position sizing
6. Time-based rules (sessions, days to avoid)
7. Risk management
8. Exit rules
9. What to avoid
10. Performance monitoring

Return ONLY valid JSON:
{{
  "structured_rules": "<numbered rules as a single string, each rule on its own line>",
  "summary": "<2-3 sentence strategy summary>",
  "recommended_symbols": [<list of symbols this works best on>],
  "recommended_timeframes": [<list of timeframes>]
}}"""

    try:
        auditor_model, auditor_provider = _get_auditor_model_and_provider(db)
        jresp = _call_llm_json(
            db, auditor_model, auditor_provider,
            "You are a professional cryptocurrency perpetual futures "
            "strategy architect. You create detailed, structured "
            "strategies for 24/7 USDT-margined perp markets.",
            rules_prompt,
            required_keys=["structured_rules"],
            context="strategy_graduation",
            max_tokens=4096, temperature=0.3)
        rules_result = jresp.get("parsed")
        if not rules_result:
            logger.error("[Auditor] Graduation: failed to parse rules JSON")
            return False
    except Exception as e:
        logger.error(f"[Auditor] Graduation LLM error: {e}")
        return False

    strat_name = candidate.get("name", "Auto Strategy")[:200]
    structured_rules = rules_result.get("structured_rules", "")
    summary = rules_result.get("summary", candidate.get("thesis", ""))

    avg_composite = round(sum(
        float(p.get("composite_score") or 0) for p in patterns) / len(patterns), 2)

    try:
        db.execute("""
            INSERT INTO trading_strategies
            (name, description, structured_rules, status,
             source_pattern_ids, auto_discovered, graduation_score,
             min_win_rate, evidence_trade_count, comprehension_pct)
            VALUES (%s, %s, %s, 'draft', %s, 1, %s, %s, %s, 0)
        """, (strat_name, summary[:2000], structured_rules[:5000],
              json.dumps(pattern_ids), avg_composite, combined_wr,
              len(int_evidence)))

        new_strat = db.fetch_one(
            "SELECT id FROM trading_strategies WHERE name = %s "
            "ORDER BY id DESC LIMIT 1", (strat_name,))
        strat_id = new_strat["id"] if new_strat else None

        if strat_id:
            db.execute(
                f"UPDATE trade_patterns SET graduated_strategy_id = %s "
                f"WHERE id IN ({placeholders})",
                (strat_id, *pattern_ids))

        logger.info(f"[Auditor] STRATEGY GRADUATED: '{strat_name}' "
                    f"(id={strat_id}) from patterns {pattern_ids} | "
                    f"WR={combined_wr}%, PnL=${agg_pnl:.2f}, "
                    f"evidence={len(int_evidence)} trades")
        return True
    except Exception as e:
        logger.error(f"[Auditor] Strategy graduation DB error: {e}")
        return False


def _bridge_rec_to_ab_challenger(db, rec: dict):
    """When Ledger proposes a prompt/soul/identity change, create a challenger
    prompt_versions row so the A/B testing mechanism in trade_dossier can
    pick it up for 20% of future dossiers.

    Maps target_agent to prompt_versions role:
      quant/analyst -> analyst (Stage 1)
      apex/trader   -> trader  (Stage 2)
    """
    agent_to_role = {
        "quant": "analyst", "analyst": "analyst",
        "apex": "trader", "trader": "trader",
    }
    target_agent = rec.get("target_agent", "")
    role = agent_to_role.get(target_agent)
    if not role:
        return

    proposed = rec.get("proposed_value", "")
    if not proposed or len(proposed) < 50:
        return

    # Get current champion prompt for this role
    champion = db.fetch_one("""
        SELECT id, system_prompt, version FROM prompt_versions
        WHERE role = %s AND is_active = 1
        ORDER BY version DESC LIMIT 1
    """, (role,))

    if not champion or not champion.get("system_prompt"):
        return

    current_prompt = champion["system_prompt"]
    current_val = rec.get("current_value", "")

    # Build challenger: apply the proposed change to the current champion
    if current_val and current_val in current_prompt:
        challenger_prompt = current_prompt.replace(current_val, proposed, 1)
    else:
        challenger_prompt = current_prompt + "\n\n" + proposed

    next_ver = (champion.get("version") or 1) + 1
    justification = rec.get("business_justification", "Ledger synthesis")[:500]

    db.execute("""
        INSERT INTO prompt_versions
        (role, category, version, prompt_name, description, system_prompt,
         is_active, changed_by, change_reason, parent_version_id, created_at)
        VALUES (%s, 'role', %s, %s, %s, %s, 0, 'ledger_synthesis',
                %s, %s, NOW())
    """, (
        role,
        next_ver,
        f"Ledger challenger v{next_ver} for {role}",
        justification[:200],
        challenger_prompt,
        f"testing - {justification[:300]}",
        champion["id"],
    ))
    logger.info(f"[Auditor] A/B challenger created for '{role}' "
                f"(v{next_ver}, parent=#{champion['id']})")


def _auto_approve_if_autonomous(db, config):
    """AI CEO autonomy with graduated risk tiers.

    Autonomy levels (stored in system_config 'ceo_autonomy_level'):
      manual     -- all proposals require human CEO (default)
      assisted   -- AI CEO pre-screens, flags low-risk for auto-approval
      supervised -- AI CEO auto-approves low-risk, human reviews high-risk
      autonomous -- AI CEO handles all (original behaviour)

    Risk classification:
      LOW:  parameter_change/threshold_change with quantitative evidence,
            process_change, data_source_change
      HIGH: soul_change, identity_change, prompt_change, tool_change
    """
    try:
        # Check autonomy level (new) or legacy owner_approval_required
        row = db.fetch_one(
            "SELECT config_value FROM system_config "
            "WHERE config_key = 'ceo_autonomy_level'")
        level = row.get("config_value", "manual") if row else None

        if not level:
            row2 = db.fetch_one(
                "SELECT config_value FROM system_config "
                "WHERE config_key = 'owner_approval_required'")
            needs_approval = (row2.get("config_value", "true") if row2 else "true") == "true"
            level = "manual" if needs_approval else "autonomous"

        if level == "manual":
            return

        LOW_RISK_CATS = ("parameter_change", "threshold_change",
                         "process_change", "data_source_change")
        HIGH_RISK_CATS = ("soul_change", "identity_change", "prompt_change",
                          "tool_change")

        pending = db.fetch_all(
            "SELECT * FROM audit_recommendations WHERE status = 'pending' "
            "ORDER BY id ASC")
        if not pending:
            return

        auto_count = 0
        flagged_count = 0
        for rec in pending:
            cat = rec.get("category", "")
            sev = rec.get("severity", "suggestion")
            has_evidence = bool(rec.get("evidence") and len(rec.get("evidence", "")) > 50)
            has_justification = bool(rec.get("business_justification")
                                     and len(rec.get("business_justification", "")) > 30)

            # Determine if this rec qualifies for auto-approval at this level
            can_auto = False
            if level == "autonomous":
                can_auto = True
            elif level == "supervised":
                can_auto = (cat in LOW_RISK_CATS and has_evidence)
            elif level == "assisted":
                can_auto = (cat in LOW_RISK_CATS
                            and sev == "suggestion"
                            and has_evidence and has_justification)

            if can_auto:
                try:
                    _auto_apply_single_rec(db, rec, config)
                    auto_count += 1
                except Exception as e:
                    logger.error(f"[CEO-AI] Auto-approve rec #{rec.get('id')} failed: {e}")
            else:
                flagged_count += 1
                logger.info(f"[CEO-AI] Rec #{rec.get('id')} ({cat}/{sev}) "
                            f"flagged for human review (level={level})")

        if auto_count or flagged_count:
            logger.info(f"[CEO-AI] Autonomy={level}: {auto_count} auto-applied, "
                        f"{flagged_count} flagged for human review")

        # Check if trust score suggests a graduation is warranted
        try:
            ts = db.fetch_one("""
                SELECT overall_score, autonomy_eligible, score_date
                FROM system_trust_score
                ORDER BY score_date DESC LIMIT 1
            """)
            if ts and ts.get("autonomy_eligible"):
                eligible = ts["autonomy_eligible"]
                LEVEL_ORDER = {"manual": 0, "assisted": 1, "supervised": 2, "autonomous": 3}
                if LEVEL_ORDER.get(eligible, 0) > LEVEL_ORDER.get(level, 0):
                    logger.info(
                        f"[CEO-AI] Trust score {ts.get('overall_score')}/100 "
                        f"(date={ts.get('score_date')}) qualifies for '{eligible}' "
                        f"autonomy (currently '{level}'). CEO can upgrade in settings.")
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[CEO-AI] Auto-approve check error: {e}")


def _smart_merge_soul(old_text: str, proposed: str, current: str = "") -> str:
    """Intelligently merge a proposed change into an agent soul/prompt.

    Strategy (in priority order):
    1. Section-aware replace: if proposed contains ## headers that already
       exist in old_text, replace those entire sections.
    2. Current-text replace: if current is provided and matches a unique
       passage in old_text, swap that passage for proposed.
    3. Append new sections: if proposed has ## headers not present in
       old_text, append them at the end.
    4. Similarity guard: if proposed has no ## headers, check that the text
       isn't substantially already present before appending.
    """
    import re
    import difflib

    if not old_text or not old_text.strip():
        return proposed

    def _parse_sections(text: str) -> list:
        """Split markdown text into (header, body) tuples.
        Non-headed preamble gets header=''."""
        parts = re.split(r'(?=^## )', text, flags=re.MULTILINE)
        sections = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            match = re.match(r'^(## .+?)(?:\n|$)', part)
            header = match.group(1).strip() if match else ""
            sections.append((header, part))
        return sections

    def _normalise_header(h: str) -> str:
        """Normalise for comparison: lowercase, collapse whitespace."""
        return re.sub(r'\s+', ' ', h.lower().strip().lstrip('#').strip())

    old_sections = _parse_sections(old_text)
    proposed_sections = _parse_sections(proposed)
    old_header_map = {}
    for idx, (h, _body) in enumerate(old_sections):
        if h:
            old_header_map[_normalise_header(h)] = idx

    if proposed_sections and any(h for h, _ in proposed_sections):
        replaced_indices = set()
        new_sections_to_add = []

        for prop_hdr, prop_body in proposed_sections:
            if not prop_hdr:
                if prop_body.strip() and prop_body.strip() not in old_text:
                    new_sections_to_add.append(prop_body)
                continue
            norm = _normalise_header(prop_hdr)
            if norm in old_header_map:
                idx = old_header_map[norm]
                old_sections[idx] = (prop_hdr, prop_body)
                replaced_indices.add(idx)
                logger.info(f"[SoulMerge] Replaced section '{prop_hdr}'")
            else:
                new_sections_to_add.append(prop_body)
                logger.info(f"[SoulMerge] Adding new section '{prop_hdr}'")

        result_parts = [body for _, body in old_sections]
        result_parts.extend(new_sections_to_add)
        return "\n\n".join(result_parts)

    if current and current.strip() and current.strip() in old_text:
        return old_text.replace(current.strip(), proposed.strip(), 1)

    similarity = difflib.SequenceMatcher(
        None, old_text[-2000:], proposed[:2000]).ratio()
    if similarity > 0.6:
        logger.warning(f"[SoulMerge] Proposed text is {similarity:.0%} similar "
                       f"to end of soul — skipping append to avoid duplication")
        return old_text

    return old_text.rstrip() + "\n\n" + proposed.strip()


def _auto_apply_single_rec(db, rec, config):
    """Apply a single recommendation (same logic as the CEO /decide endpoint)."""
    rec_id = rec["id"]
    cat = rec.get("category", "")
    agent_id = rec.get("target_agent", "")
    proposed = rec.get("proposed_value", "")
    current = rec.get("current_value", "")

    if cat in ("prompt_change", "soul_change", "identity_change") and agent_id and proposed:
        duo_id = rec.get("duo_id")
        target_field = rec.get("target_field", "")

        if cat == "prompt_change" and duo_id and target_field:
            from core.duo_config import save_duo_prompt, PROMPT_KEYS_TO_CLONE
            if target_field in PROMPT_KEYS_TO_CLONE:
                duo_key = f"{target_field}:{duo_id}"
                old_row = db.fetch_one(
                    "SELECT config_value FROM system_config WHERE config_key = %s",
                    (duo_key,))
                old_value = old_row["config_value"] if old_row else ""
                save_duo_prompt(db, duo_id, target_field, proposed)
                db.execute("""
                    UPDATE audit_recommendations
                    SET status = 'applied',
                        ceo_comments = %s,
                        ceo_decision_at = NOW(), applied_at = NOW(),
                        applied_by = 'ceo_auto', rollback_value = %s
                    WHERE id = %s
                """, (f"Auto-approved: duo={duo_id}, key={target_field}",
                      old_value[:10000], rec_id))
                logger.info(f"[Auditor] Auto-applied #{rec_id}: "
                            f"prompt_change -> {duo_key}")
                return

        profile = db.fetch_one(
            "SELECT id, soul, identity_prompt FROM agent_profiles "
            "WHERE agent_id = %s", (agent_id,))
        if not profile:
            logger.warning(f"[Auditor] Auto-approve #{rec_id}: "
                          f"agent '{agent_id}' not found")
            return

        target_col = "identity_prompt" if cat == "identity_change" else "soul"
        old_value = profile.get(target_col, "") or ""

        new_value = _smart_merge_soul(old_value, proposed, current)

        db.execute(
            f"UPDATE agent_profiles SET {target_col} = %s, updated_at = NOW() "
            f"WHERE agent_id = %s", (new_value, agent_id))
        db.execute("""
            UPDATE audit_recommendations
            SET status = 'applied', ceo_comments = 'Auto-approved (autonomous mode)',
                ceo_decision_at = NOW(), applied_at = NOW(), applied_by = 'ceo_auto',
                rollback_value = %s
            WHERE id = %s
        """, (old_value[:10000], rec_id))
        logger.info(f"[Auditor] Auto-applied #{rec_id}: {cat} -> "
                    f"{agent_id}.{target_col}")

    elif cat in ("parameter_change", "threshold_change"):
        import re
        target_field = rec.get("target_field", "")
        kv_pairs = {}
        for match in re.finditer(r'(\w+)\s*=\s*([\d.]+)', proposed):
            kv_pairs[match.group(1)] = match.group(2)

        if not kv_pairs:
            db.execute("""
                UPDATE audit_recommendations
                SET status = 'applied', ceo_comments = 'Auto-approved (autonomous mode, no parseable values)',
                    ceo_decision_at = NOW(), applied_at = NOW(), applied_by = 'ceo_auto'
                WHERE id = %s
            """, (rec_id,))
            return

        from pathlib import Path
        config_path = Path(__file__).resolve().parent.parent / "config.json"
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        old_snapshot = json.dumps(cfg)

        td = cfg.setdefault("global", {}).setdefault("trade_decision", {})
        accts = cfg.setdefault("accounts", [{}])
        if not accts:
            accts.append({})
        rs = accts[0].setdefault("risk_settings", {})

        changes = []
        for key, raw_val in kv_pairs.items():
            val = int(raw_val) if raw_val.isdigit() else float(raw_val)
            if key in td:
                td[key] = val
                changes.append(f"{key}={val}")
            elif key in rs:
                rs[key] = val
                changes.append(f"{key}={val}")
            elif target_field == "lesson_window" and key.startswith("lesson_window"):
                td[key] = val
                changes.append(f"{key}={val}")

        if changes:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
            if config and hasattr(config, 'raw'):
                config.raw.setdefault("trade_decision", {}).update(td)

        db.execute("""
            UPDATE audit_recommendations
            SET status = 'applied', ceo_comments = %s,
                ceo_decision_at = NOW(), applied_at = NOW(), applied_by = 'ceo_auto',
                rollback_value = %s
            WHERE id = %s
        """, (f"Auto-approved: {', '.join(changes) if changes else 'acknowledged'}",
              old_snapshot[:10000], rec_id))
        logger.info(f"[Auditor] Auto-applied #{rec_id}: config "
                    f"{', '.join(changes) if changes else 'acknowledged'}")

    else:
        db.execute("""
            UPDATE audit_recommendations
            SET status = 'applied', ceo_comments = 'Auto-approved (autonomous mode)',
                ceo_decision_at = NOW(), applied_at = NOW(), applied_by = 'ceo_auto'
            WHERE id = %s
        """, (rec_id,))
        logger.info(f"[Auditor] Auto-approved #{rec_id}: {cat} (acknowledged)")
