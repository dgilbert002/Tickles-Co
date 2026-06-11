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
