"""
Module: text_signal_extractor
Purpose: Extract trade signals from news_items.content text (parallel to image analysis).
         Uses regex patterns + lightweight LLM for text-based trade detection.
Location: /opt/tickles/shared/intelligence/text_signal_extractor.py

Design (inspired by Jarvais V1 signal_ai.py):
  * Stage 1: Regex extraction — fast, zero-cost pattern matching for common formats.
  * Stage 2: LLM extraction — for text that looks trade-related but regex missed.
  * Pre-filter: skip messages unlikely to contain signals (emoji detection, keywords).
  * Returns structured signal dict or None.

Usage:
    from shared.intelligence.text_signal_extractor import extract_signal_from_text
    signal = await extract_signal_from_text(text, author="TraderJ", context={})
    if signal:
        print(signal["symbol"], signal["direction"], signal["entry"])
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from shared.intelligence.gateway_config import get_gateway_for_service

logger = logging.getLogger("tickles.intelligence.text_signal_extractor")

# ---------------------------------------------------------------------------
# Regex patterns for common trade signal formats
# ---------------------------------------------------------------------------

# Symbol detection: BTCUSDT, BTC/USDT, BTC-USDT, XAUUSD, EURUSD, etc.
# Hardened to require uppercase for standalone short tickers, or standard suffixes/formats
_SYMBOL_RE = re.compile(
    r"\b("
    r"[A-Z]{3,8}"  # Standalone uppercase tickers (e.g. BTC, SOL, ONDO)
    r"|[a-zA-Z]{3,8}(?:USDT|usdt|USD|usd|PERP|perp|BTC|btc|ETH|eth)"  # Suffixes
    r"|[a-zA-Z]{3,8}/[a-zA-Z]{3,8}"  # Slash pairs (e.g. BTC/USDT)
    r"|[a-zA-Z]{3,8}-[a-zA-Z]{3,8}"  # Hyphen pairs (e.g. BTC-USDT)
    r"|XAUUSD|xauusd|XAGUSD|xagusd|US30|us30|US100|us100|NAS100|nas100|GER40|ger40|UK100|uk100"  # Specific indices
    r")\b"
)

_BLACKLIST_WORDS = {
    "LONG", "SHORT", "BUY", "SELL", "ENTRY", "EXIT", "STOP", "LOSS", "TAKE", "PROFIT",
    "REPLY", "IMAGE", "GIF", "VIDEO", "CHART", "INFO", "SETUP", "HIGH", "LOW", "ZONE",
    "TRADE", "CALL", "WEEK", "POOL", "KIDS", "MILF", "LMAO", "HAHA", "MEME", "JOKE"
}

# Entry price: "entry 65000", "@ 65000", "buy at 65000", "long 65000"
_ENTRY_RE = re.compile(
    r"(?:entry|buy|long|short|sell|@|at)\s*[:]?\s*(\d+[\.,]?\d*)",
    re.IGNORECASE,
)

# Stop loss: "sl 64000", "stop loss 64000", "sl: 64000"
_SL_RE = re.compile(
    r"(?:sl|stop[-\s]?loss)\s*[:]?\s*(\d+[\.,]?\d*)",
    re.IGNORECASE,
)

# Take profit: "tp 70000", "tp1 70000", "take profit 70000", "target 70000"
_TP_RE = re.compile(
    r"(?:tp|take[-\s]?profit|target)\s*(\d)?\s*[:]?\s*(\d+[\.,]?\d*)",
    re.IGNORECASE,
)

# Direction: explicit long/short/buy/sell
_DIRECTION_RE = re.compile(
    r"\b(long|short|buy|sell)\b",
    re.IGNORECASE,
)

# Leverage: "10x", "20x leverage"
_LEVERAGE_RE = re.compile(r"(\d+)x\b", re.IGNORECASE)

# Trading emojis that indicate signal intent
_TRADING_EMOJIS = [
    "\U0001f7e2",  # green circle
    "\U0001f534",  # red circle
    "\U0001f7e9",  # green square
    "\U0001f525",  # fire
    "\u2b06",      # up arrow
    "\u2b07",      # down arrow
    "\U0001f4c8",  # chart up
    "\U0001f4c9",  # chart down
    "\U0001f680",  # rocket
    "\U0001f48e",  # diamond
    "\u26a1",      # lightning
    "\U0001f3af",  # target
    "\u2705",      # check
    "\u274c",      # cross
]

# Word-bounded signal keywords. Dropped "trade", "call", "trend" (too
# ambiguous even word-bounded: "nice call", "tradingview", "trending").
_SIGNAL_KW_RE = re.compile(
    r"(?:entry|entries|stop[\s-]?loss|take[\s-]?profit|target|targets|"
    r"sl|tp\d?|long|short|buy|sell|setup|position|leverage|margin|signal|"
    r"breakout|support|resistance)",
    re.IGNORECASE,
)

_COMMENTARY_KW_RE = re.compile(
    r"(?:good\s+morning|gm|hello|hi|hey|how\s+are|thanks|thank\s+you|"
    r"nice|great|congrats|congratulations|well\s+done|awesome)",
    re.IGNORECASE,
)

_MEME_KW_RE = re.compile(
    r"(?:lol|lmao|haha+|meme|joke|funny|lambo|moon|wagmi|ngmi)",
    re.IGNORECASE,
)
_MEME_EMOJIS = ("😂", "🤣", "💀", "🚀")

# Price: 4-6 digit ints (BTC) OR decimals (ONDO 0.85, SOL 150.5) OR k-suffix (71.5k)
_PRICE_ANY_RE = re.compile(
    r"(?:\d{4,6}|\d+\.\d+|\d+(?:\.\d+)?\s?k)",
    re.IGNORECASE,
)


def _signal_score(text: str) -> int:
    """Count UNIQUE word-bounded signal keywords ('sl sl sl' scores 1)."""
    return len({m.lower() for m in _SIGNAL_KW_RE.findall(text)})


def _commentary_score(text: str) -> int:
    return len({m.lower() for m in _COMMENTARY_KW_RE.findall(text)})


def _has_symbol(text: str) -> bool:
    """True if _SYMBOL_RE matches something not in the blacklist."""
    for m in _SYMBOL_RE.finditer(text):
        if m.group(1).upper() not in _BLACKLIST_WORDS:
            return True
    return False


# ---------------------------------------------------------------------------
# Regex extraction
# ---------------------------------------------------------------------------

def _extract_with_regex(text: str) -> Optional[Dict[str, Any]]:
    """Extract trade signal using regex patterns.

    Args:
        text: Message content.

    Returns:
        Signal dict or None if no signal detected.
    """
    # Extract symbol
    symbol_match = _SYMBOL_RE.search(text)
    symbol = symbol_match.group(1).upper() if symbol_match else None

    # Blacklist check
    if symbol and symbol in _BLACKLIST_WORDS:
        symbol = None

    # Extract direction
    direction_match = _DIRECTION_RE.search(text)
    direction = None
    if direction_match:
        d = direction_match.group(1).lower()
        if d in ("long", "buy"):
            direction = "long"
        elif d in ("short", "sell"):
            direction = "short"

    # Extract entry
    entry_match = _ENTRY_RE.search(text)
    entry = None
    if entry_match:
        try:
            entry = float(entry_match.group(1).replace(",", ""))
        except ValueError:
            pass

    # Extract stop loss
    sl_match = _SL_RE.search(text)
    stop_loss = None
    if sl_match:
        try:
            stop_loss = float(sl_match.group(1).replace(",", ""))
        except ValueError:
            pass

    # Extract take profits (up to 3)
    tp_matches = _TP_RE.findall(text)
    take_profits: List[float] = []
    for m in tp_matches:
        # m is a tuple: (tp_number, price) or just (price,)
        price_str = m[-1] if isinstance(m, tuple) else m
        try:
            tp_val = float(price_str.replace(",", ""))
            take_profits.append(tp_val)
        except ValueError:
            pass

    # Extract leverage
    lev_match = _LEVERAGE_RE.search(text)
    leverage = None
    if lev_match:
        try:
            leverage = int(lev_match.group(1))
        except ValueError:
            pass

    # Require at least symbol + direction + entry for a valid signal
    if not symbol or not direction or not entry:
        return None

    # Validate: SL must be on correct side of entry
    if stop_loss:
        if direction == "long" and stop_loss >= entry:
            logger.debug("Regex: invalid SL for long (SL >= entry), ignoring SL")
            stop_loss = None
        elif direction == "short" and stop_loss <= entry:
            logger.debug("Regex: invalid SL for short (SL <= entry), ignoring SL")
            stop_loss = None

    result: Dict[str, Any] = {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profits": take_profits,
        "leverage": leverage,
        "source": "regex",
        "confidence": 0.7 if (stop_loss and take_profits) else 0.5,
        "raw_text": text[:500],
    }

    logger.debug("Regex extracted signal: %s %s @ %.2f", symbol, direction, entry)
    return result


# ---------------------------------------------------------------------------
# LLM extraction (fallback for text regex misses)
# ---------------------------------------------------------------------------

_LLM_TEXT_PROMPT = """You are analyzing a Discord trading message. Extract any trade signal.

Message:
"""  # Will be appended with the actual text

_LLM_TEXT_SYSTEM = """You are a trading signal extractor. Read the message and extract structured data.

Respond ONLY with a JSON object:
{
  "is_trade_setup": true/false,
  "symbol": "BTCUSDT" or null,
  "direction": "long" or "short" or null,
  "entry": number or null,
  "stop_loss": number or null,
  "take_profit_1": number or null,
  "take_profit_2": number or null,
  "leverage": number or null,
  "confidence": 0.0-1.0,
  "is_commentary": true/false,
  "is_meme": true/false,
  "reasoning": "brief explanation"
}

Rules:
- is_trade_setup = true ONLY if there is a clear entry price + direction + symbol.
- is_commentary = true if it's market talk without specific levels.
- is_meme = true if it's a joke, meme, or non-serious content.
- Set confidence based on clarity: 0.9 = explicit numbers, 0.5 = implied, 0.1 = unclear.
- If no trade signal, return is_trade_setup=false and all other fields null."""


async def _extract_with_llm(text: str, author: str = "") -> Optional[Dict[str, Any]]:
    """Extract trade signal using lightweight LLM.

    Args:
        text: Message content.
        author: Message author name.

    Returns:
        Signal dict or None.
    """
    # Skip if pre-filter says no
    # Round 14: provider + model from the "text_extract" slot (dashboard picker).
    from shared.intelligence.gateway_config import resolve_slot_gateway
    gateway, model = await resolve_slot_gateway("text_extract")

    user_prompt = "Message:\n" + text[:2000]

    messages = [
        {"role": "system", "content": _LLM_TEXT_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]

    try:
        from shared.intelligence.gateway_config import chat_completion

        response = await chat_completion(
            cfg=gateway,
            model=model,
            system_prompt=_LLM_TEXT_SYSTEM,
            user_text=user_prompt,
            max_tokens=int(os.environ.get("TEXT_EXTRACTOR_MAX_TOKENS", "4096")),
        )
        content = response.get("content", "")

        # Extract JSON
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if not json_match:
            return None

        parsed = json.loads(json_match.group())

        if not parsed.get("is_trade_setup"):
            return None

        symbol = parsed.get("symbol")
        direction = parsed.get("direction")
        # Bug 10 sibling defense: LLMs sometimes emit `entry_price` instead of
        # the singular `entry` key the system prompt asks for. Accept either
        # so we don't silently drop a valid signal.
        entry = parsed.get("entry")
        if entry is None:
            entry = parsed.get("entry_price")

        if not symbol or not direction or not entry:
            return None

        symbol = symbol.upper()
        if symbol in _BLACKLIST_WORDS:
            return None

        # Normalize direction
        direction = direction.lower()
        if direction not in ("long", "short"):
            return None

        tps = []
        for i in range(1, 4):
            tp = parsed.get(f"take_profit_{i}")
            if tp:
                try:
                    tps.append(float(tp))
                except (TypeError, ValueError):
                    pass
        # Bug 10 sibling defense: LLMs sometimes emit a singular `take_profit`
        # even when the system prompt asks for numbered keys. If we got
        # nothing from the numbered keys, try the singular form so we don't
        # silently drop the trader's TP.
        if not tps:
            singular_tp = parsed.get("take_profit")
            if singular_tp is not None:
                try:
                    tps.append(float(singular_tp))
                except (TypeError, ValueError):
                    pass

        result: Dict[str, Any] = {
            "symbol": symbol.upper(),
            "direction": direction,
            "entry": float(entry),
            "stop_loss": parsed.get("stop_loss"),
            "take_profits": tps,
            "leverage": parsed.get("leverage"),
            "source": "llm_text",
            "confidence": float(parsed.get("confidence", 0.5)),
            "is_commentary": parsed.get("is_commentary", False),
            "is_meme": parsed.get("is_meme", False),
            "reasoning": parsed.get("reasoning", ""),
            "raw_text": text[:500],
        }

        logger.info("LLM extracted signal from text: %s %s @ %.2f (conf=%.2f)",
                    symbol, direction, float(entry), result["confidence"])
        return result

    except Exception as e:
        logger.warning("LLM text extraction failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def strip_reply_prefix(text: str) -> str:
    """Discord reply-prefix stripper. Thin re-export of the canonical helper.

    Kept as a module-level binding because many existing callers in the
    intelligence layer import
    ``shared.intelligence.text_signal_extractor.strip_reply_prefix``
    directly. The actual implementation lives in
    ``shared.utils.reply_prefix`` so all layers share one source of truth.
    """
    from shared.utils.reply_prefix import strip_reply_prefix as _impl
    return _impl(text)


async def extract_signal_from_text(
    text: str,
    author: str = "",
    context: Optional[Dict[str, Any]] = None,
    use_llm_fallback: bool = True,
) -> Optional[Dict[str, Any]]:
    """Extract a trade signal from message text.

    Tries regex first (fast, free), then LLM fallback if enabled and regex misses.

    Args:
        text: Message content to analyze.
        author: Message author name.
        context: Optional context dict (channel, source, etc.).
        use_llm_fallback: Whether to try LLM if regex fails.

    Returns:
        Signal dict with keys: symbol, direction, entry, stop_loss, take_profits,
        leverage, source, confidence, raw_text. None if no signal detected.
    """
    clean_text = strip_reply_prefix(text)
    if not clean_text or len(clean_text.strip()) < 5:
        return None

    # Stage 1: Regex
    signal = _extract_with_regex(clean_text)
    if signal:
        return signal

    # Stage 2: LLM fallback
    if use_llm_fallback:
        try:
            signal = await _extract_with_llm(clean_text, author)
            if signal:
                return signal
        except Exception as e:
            logger.warning("LLM fallback extraction failed: %s", e)

    return None


def classify_message_type(text: str) -> str:
    """Classify a message as trade_setup, commentary, meme, or unknown.

    THE single gating classifier. Uses word-bounded regex (not substring
    matching) for keyword scores, and requires substance alongside emojis.
    _passes_prefilter is removed; callers trust this gate.
    """
    clean_text = strip_reply_prefix(text)
    if not clean_text:
        return "unknown"

    stripped = clean_text.strip()
    sig = _signal_score(clean_text)
    comm = _commentary_score(clean_text)
    has_price = bool(_PRICE_ANY_RE.search(clean_text))
    has_direction = bool(_DIRECTION_RE.search(clean_text))

    # Meme: word-bounded keywords + meme emojis. Signal keywords veto.
    meme_score = len({m.lower() for m in _MEME_KW_RE.findall(clean_text)})
    meme_score += sum(1 for e in _MEME_EMOJIS if e in clean_text)
    if "diamond hands" in clean_text.lower():
        meme_score += 1
    if sig == 0 and (meme_score >= 2 or (meme_score >= 1 and len(stripped) < 30)):
        return "meme"

    if comm >= 2 and sig < 2:
        return "commentary"

    if sig >= 2 and has_price and has_direction:
        return "trade_setup"

    # Emoji shorthand: only when there's SUBSTANCE alongside the emojis.
    # Old branch sent "✅🎯" to Gemini -- pure rate-limit burn.
    # Now requires a price plus a direction word or non-blacklisted symbol.
    if len(stripped) < 80:
        emoji_count = sum(1 for e in _TRADING_EMOJIS if e in clean_text)
        if emoji_count >= 2 and has_price and (has_direction or _has_symbol(clean_text)):
            return "trade_setup"

    return "unknown"


# ---------------------------------------------------------------------------
# Embedding gate — semantic second opinion for 'unknown' messages
# Env-gated off by default.  Enables bge-small-en-v1.5 (~80MB model, ~10-30ms
# per message on CPU).  Calibrate with tools/calibrate_embed_gate.py.
# ---------------------------------------------------------------------------
_EMBED_GATE_ENABLED = os.environ.get("TEXT_EMBED_GATE_ENABLED", "0") == "1"
_EMBED_GATE_MARGIN = float(os.environ.get("TEXT_EMBED_GATE_MARGIN", "0.05"))
_embed_state: Dict[str, Any] = {}

_SIGNAL_ANCHORS = [
    "entering a long on BTC here, target 72k",
    "shorting ETH at 3450, stop above 3500",
    "loading SOL spot here, looking for 180",
    "BTC to 72k from here, stop under 69",
    "moved my stop to break even on the TAO long",
    "taking partials at TP1, runner to 0.95",
    "ONDO looking ready, entry zone 0.84-0.86",
    "adding to my position at this support",
]
_CHATTER_ANCHORS = [
    "gm everyone, how are we doing today",
    "lol that was crazy yesterday",
    "nice one bro congrats on the win",
    "anyone watching the game tonight",
    "market is wild today haha",
    "thanks man appreciate it",
    "what do you guys think in general",
    "good morning, coffee first then charts",
]


def _embed_gate_init() -> Dict[str, Any]:
    """Lazy-load model + centroids. ~80MB model, CPU-fine on Contabo.
    Uses raw AutoModel (not sentence-transformers — its wrapper hangs on this
    VPS). Mean-pooling + L2-normalize matches BGE's recommended usage."""
    if "model" in _embed_state:
        return _embed_state
    import torch
    import numpy as np
    from transformers import AutoTokenizer, AutoModel

    torch.set_num_threads(1)  # prevent thread contention on single-vCPU VPS
    tokenizer = AutoTokenizer.from_pretrained("BAAI/bge-small-en-v1.5")
    model = AutoModel.from_pretrained("BAAI/bge-small-en-v1.5")

    def _encode(texts):
        inputs = tokenizer(texts, return_tensors="pt", padding=True, truncation=True, max_length=512)
        with torch.no_grad():
            outputs = model(**inputs)
            # Mean pooling over token dimension (BGE recommendation)
            v = outputs.last_hidden_state.mean(dim=1)
            # L2 normalize
            v = torch.nn.functional.normalize(v, p=2, dim=1)
        return v.numpy()

    sig = _encode(_SIGNAL_ANCHORS).mean(axis=0)
    cht = _encode(_CHATTER_ANCHORS).mean(axis=0)
    _embed_state["model"] = model
    _embed_state["tokenizer"] = tokenizer
    _embed_state["_encode"] = _encode
    _embed_state["sig"] = sig / np.linalg.norm(sig)
    _embed_state["cht"] = cht / np.linalg.norm(cht)
    logger.info("embed gate: loaded bge-small-en-v1.5 (margin=%.2f)", _EMBED_GATE_MARGIN)
    return _embed_state


def _looks_signal_like_sync(text: str) -> bool:
    st = _embed_gate_init()
    v = st["_encode"]([text[:512]])[0]
    return (float(v @ st["sig"]) - float(v @ st["cht"])) > _EMBED_GATE_MARGIN


async def looks_signal_like(text: str) -> bool:
    """Local semantic check for borderline ('unknown') messages.

    True => escalate to LLM extraction. Errors/disabled => False (no
    escalation; classify's trade_setup branch is unaffected either way).
    """
    if not _EMBED_GATE_ENABLED or not text:
        return False
    try:
        return await asyncio.to_thread(_looks_signal_like_sync, strip_reply_prefix(text))
    except Exception as exc:
        logger.warning("embed gate unavailable, skipping escalation: %s", exc)
        return False
