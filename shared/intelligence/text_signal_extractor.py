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
_SYMBOL_RE = re.compile(
    r"\b([A-Z]{3,8}(?:USDT|USD|BTC|ETH|PERP)?|[A-Z]{3}/[A-Z]{3}|"
    r"[A-Z]{3}-[A-Z]{3}|XAUUSD|XAGUSD|US30|US100|NAS100|GER40|UK100)\b",
    re.IGNORECASE,
)

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

# Keywords that strongly indicate a trade signal
_SIGNAL_KEYWORDS = [
    "entry", "stop loss", "take profit", "target", "sl", "tp",
    "long", "short", "buy", "sell", "setup", "position",
    "leverage", "margin", "call", "signal", "trade",
    "breakout", "support", "resistance", "trend",
]

# Keywords that indicate commentary / non-signal
_COMMENTARY_KEYWORDS = [
    "good morning", "gm", "hello", "hi ", "how are", "thanks", "thank you",
    "lol", "lmao", "haha", "meme", "joke", "funny", "nice", "great",
    "congrats", "congratulations", "well done", "awesome",
]


# ---------------------------------------------------------------------------
# Pre-filter (inspired by Jarvais V1 _passes_signal_prefilter)
# ---------------------------------------------------------------------------

def _passes_prefilter(text: str, author: str = "") -> bool:
    """Fast pre-filter: skip messages unlikely to contain trade signals.

    Returns True if the message SHOULD be processed, False to skip.
    """
    if not text or len(text.strip()) < 10:
        return False

    text_lower = text.lower()

    # Skip pure commentary
    commentary_score = sum(1 for kw in _COMMENTARY_KEYWORDS if kw in text_lower)
    if commentary_score >= 2:
        return False

    # Check for signal keywords
    signal_score = sum(1 for kw in _SIGNAL_KEYWORDS if kw in text_lower)
    if signal_score >= 2:
        return True

    # Check for trading emojis
    emoji_score = sum(1 for e in _TRADING_EMOJIS if e in text)
    if emoji_score >= 2:
        return True

    # Check for price levels (numbers that look like prices)
    price_matches = re.findall(r"\b\d{4,6}\b", text)
    if len(price_matches) >= 2 and signal_score >= 1:
        return True

    # Very short messages with emojis only — might be updates
    if emoji_score >= 1 and len(text.strip()) < 100:
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
    if not _passes_prefilter(text):
        return None

    # Extract symbol
    symbol_match = _SYMBOL_RE.search(text)
    symbol = symbol_match.group(1).upper() if symbol_match else None

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
    if not _passes_prefilter(text, author):
        return None

    gateway = get_gateway_for_service("text_extraction")
    model = os.environ.get("TEXT_EXTRACTION_MODEL", "google/gemini-2.0-flash-001")

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
            max_tokens=500,
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
        entry = parsed.get("entry")

        if not symbol or not direction or not entry:
            return None

        # Normalize direction
        direction = direction.lower()
        if direction not in ("long", "short"):
            return None

        tps = []
        for i in range(1, 4):
            tp = parsed.get(f"take_profit_{i}")
            if tp:
                tps.append(float(tp))

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
    if not text or len(text.strip()) < 5:
        return None

    # Stage 1: Regex
    signal = _extract_with_regex(text)
    if signal:
        return signal

    # Stage 2: LLM fallback
    if use_llm_fallback:
        try:
            signal = await _extract_with_llm(text, author)
            if signal:
                return signal
        except Exception as e:
            logger.warning("LLM fallback extraction failed: %s", e)

    return None


def classify_message_type(text: str) -> str:
    """Classify a message as trade_setup, commentary, meme, or unknown.

    Args:
        text: Message content.

    Returns:
        One of: 'trade_setup', 'commentary', 'meme', 'unknown'.
    """
    if not text:
        return "unknown"

    text_lower = text.lower()

    # Meme detection
    meme_indicators = ["lol", "lmao", "haha", "meme", "joke", "funny", "😂", "🤣", "💀", "🚀", "lambo", "moon", "diamond hands"]
    meme_score = sum(1 for m in meme_indicators if m in text_lower)
    if meme_score >= 2 or (meme_score >= 1 and len(text.strip()) < 30):
        return "meme"

    # Commentary detection
    commentary_score = sum(1 for kw in _COMMENTARY_KEYWORDS if kw in text_lower)
    signal_score = sum(1 for kw in _SIGNAL_KEYWORDS if kw in text_lower)

    if commentary_score >= 2 and signal_score < 2:
        return "commentary"

    # Trade setup detection
    if signal_score >= 2:
        has_price = bool(re.search(r"\b\d{4,6}\b", text))
        has_direction = bool(_DIRECTION_RE.search(text))
        if has_price and has_direction:
            return "trade_setup"

    # Emoji-only short messages
    if len(text.strip()) < 50:
        emoji_count = sum(1 for e in _TRADING_EMOJIS if e in text)
        if emoji_count >= 2:
            return "trade_setup"  # Likely a chart repost with emojis

    return "unknown"
