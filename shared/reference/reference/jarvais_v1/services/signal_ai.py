"""
Signal AI — Full Signal Lifecycle System
=========================================
Stage 1: PARSE   — AI extracts structured signals from alpha messages
Stage 2: TRACK   — Monitor candle data to see if entry/SL/TP levels hit
Stage 3: SCORE   — Calculate outcomes, update provider trust scores, feed post-mortem

Architecture:
  news_items (alpha) → SignalParser → parsed_signals table
                                          ↓
                                    SignalTracker (candle checks)
                                          ↓
                                    SignalScorer (outcome + provider scoring)
                                          ↓
                                    Post-Mortem AI (lessons learned)
"""

import json
import logging
import re
import time
import threading
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("jarvais.signal_ai")


# ─────────────────────────────────────────────────────────────────────
# Shared SL/TP enrichment (module-level, used by SignalParser, SignalAI, and AlphaAnalyzer)
# Uses % of entry price as guardrail — universal for forex, crypto, indices
# ─────────────────────────────────────────────────────────────────────

def _load_trade_settings() -> dict:
    import os as _os
    cfg_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "config.json")
    with open(cfg_path, "r") as f:
        cfg = json.load(f)
    accts = cfg.get("accounts", [])
    return accts[0].get("risk_settings", {}) if accts else {}


def _load_cost_optimization() -> dict:
    """Load cost_optimization settings from config.json, reloaded each call for dynamic control."""
    import os as _os
    try:
        cfg_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "config.json")
        with open(cfg_path, "r") as f:
            cfg = json.load(f)
        return cfg.get("global", {}).get("cost_optimization", {})
    except Exception:
        return {}


def _passes_signal_prefilter(news_item: dict) -> bool:
    """Fast pre-filter: skip messages unlikely to contain trade signals.
    Returns True if the message SHOULD be sent to AI, False to skip.
    Controlled by config.json > global > cost_optimization > signal_prefilter.
    """
    opt = _load_cost_optimization()
    pf = opt.get("signal_prefilter", {})
    if not pf.get("enabled", False):
        return True

    source = (news_item.get("source") or "").lower()

    skip_sources = [s.lower() for s in pf.get("skip_sources", [])]
    if source in skip_sources:
        return False
    if any(source.startswith(prefix) for prefix in skip_sources if prefix.endswith("_")):
        return False

    text = ((news_item.get("headline") or "") + " " + (news_item.get("detail") or "")).lower()
    if len(text.strip()) < 10:
        return False

    min_matches = pf.get("min_keyword_matches", 1)
    keywords = pf.get("keywords", [])
    # Emojis kept in code (not config) to avoid Windows encoding issues
    TRADING_EMOJIS = [
        "\U0001f7e2", "\U0001f534", "\U0001f7e9", "\U0001f525",  # green/red circles, fire
        "\u2b06", "\u2b07", "\U0001f4c8", "\U0001f4c9",          # up/down arrows, charts
        "\U0001f680", "\U0001f48e", "\u26a1", "\U0001f3af",      # rocket, diamond, lightning, target
        "\u2705", "\u274c",                                        # check, cross
    ]
    emoji_signals = TRADING_EMOJIS if pf.get("emoji_signals_enabled", True) else []
    pattern_triggers = pf.get("pattern_triggers", [])

    match_count = 0
    for kw in keywords:
        if kw.lower() in text:
            match_count += 1
            if match_count >= min_matches:
                return True

    raw_text = (news_item.get("headline") or "") + " " + (news_item.get("detail") or "")
    for emoji in emoji_signals:
        if emoji in raw_text:
            return True

    for pat in pattern_triggers:
        if re.search(pat, raw_text):
            return True

    if opt.get("log_filtered_messages", False) and match_count < min_matches:
        logger.debug(f"Pre-filter SKIP: source={source}, matches={match_count}/{min_matches}, "
                     f"item_id={news_item.get('id', '?')}, text={text[:80]}...")

    return False


def enrich_missing_levels(signal, db=None) -> Tuple[str, str]:
    """Enrich a ParsedSignal missing SL and/or TP using trade settings.
    Guardrails are expressed as % of entry price (not pips).
    Returns (sl_source, tp_source). Modifies signal in-place.
    """
    sl_source = "human"
    tp_source = "human"

    try:
        settings = _load_trade_settings()
    except Exception as e:
        logger.warning(f"Could not load trade settings for enrichment: {e}")
        return sl_source, tp_source

    entry = getattr(signal, 'entry_price', 0) or 0
    if not entry or entry == 0:
        return sl_source, tp_source

    direction = getattr(signal, 'direction', '') or ''

    # ── Enrich missing SL ──
    sl_val = getattr(signal, 'stop_loss', 0) or 0
    if not sl_val or sl_val == 0:
        strategy = settings.get("no_sl_strategy", "policy")
        max_pct = settings.get("no_sl_max_pct", 2.0)
        max_distance = entry * max_pct / 100.0

        if strategy == "ai":
            ai_sl = _ai_generate_level(signal, settings, "sl")
            if ai_sl and ai_sl > 0:
                # Directional validation: SL must be on correct side of entry
                sl_valid_side = (direction == "BUY" and ai_sl < entry) or \
                                (direction == "SELL" and ai_sl > entry)
                dist = abs(entry - ai_sl)
                if sl_valid_side and dist <= max_distance:
                    signal.stop_loss = ai_sl
                    sl_source = "ai_generated"
                elif not sl_valid_side and dist <= max_distance:
                    # Flip to correct side
                    ai_sl = entry - dist if direction == "BUY" else entry + dist
                    signal.stop_loss = round(ai_sl, 5)
                    sl_source = "ai_generated"
                    logger.info(f"AI SL on wrong side of entry, flipped: {ai_sl}")
                else:
                    capped = entry - max_distance if direction == "BUY" else entry + max_distance
                    signal.stop_loss = round(capped, 5)
                    sl_source = "ai_generated"
                    logger.info(f"AI SL capped at {max_pct}% guardrail: {ai_sl} -> {signal.stop_loss}")
            else:
                signal.stop_loss = entry - max_distance if direction == "BUY" else entry + max_distance
                signal.stop_loss = round(signal.stop_loss, 5)
                sl_source = "policy"
                logger.info(f"AI SL failed, fell back to policy: {signal.stop_loss}")
        else:
            signal.stop_loss = entry - max_distance if direction == "BUY" else entry + max_distance
            signal.stop_loss = round(signal.stop_loss, 5)
            sl_source = "policy"

        logger.info(f"Enriched SL ({sl_source}): {getattr(signal, 'symbol', '?')} {direction} entry={entry} SL={signal.stop_loss}")

    # ── Enrich missing TP ──
    has_any_tp = any([
        getattr(signal, f'take_profit_{i}', 0) for i in range(1, 7)
    ])
    if not has_any_tp:
        strategy = settings.get("no_tp_strategy", "policy")
        max_pct = settings.get("no_tp_max_pct", 4.0)
        max_distance = entry * max_pct / 100.0

        if strategy == "ai":
            ai_tps = _ai_generate_level(signal, settings, "tp")
            if ai_tps and isinstance(ai_tps, list):
                last_tp = None
                for i, tp_val in enumerate(ai_tps[:6], 1):
                    dist = abs(tp_val - entry)
                    if dist <= max_distance:
                        final_tp = tp_val
                    else:
                        stagger_frac = min(1.0, 0.5 + (i * 0.1))
                        staggered_dist = max_distance * stagger_frac
                        if direction == "BUY":
                            final_tp = entry + staggered_dist
                        else:
                            final_tp = entry - staggered_dist
                        final_tp = round(final_tp, 5)
                    # Enforce monotonicity: each TP must be farther from entry
                    if last_tp is not None:
                        if direction == "BUY" and final_tp <= last_tp:
                            final_tp = round(last_tp + (entry * 0.002), 5)
                        elif direction == "SELL" and final_tp >= last_tp:
                            final_tp = round(last_tp - (entry * 0.002), 5)
                    setattr(signal, f"take_profit_{i}", final_tp)
                    last_tp = final_tp
                tp_source = "ai_generated"
            else:
                _apply_policy_tp(signal, settings)
                tp_source = "policy"
        else:
            _apply_policy_tp(signal, settings)
            tp_source = "policy"

        logger.info(f"Enriched TP ({tp_source}): {getattr(signal, 'symbol', '?')} TP1={getattr(signal, 'take_profit_1', 0)}")

    return sl_source, tp_source


def _apply_policy_tp(signal, settings: dict):
    rr = settings.get("no_tp_default_rr", 2.0)
    entry = getattr(signal, 'entry_price', 0) or 0
    sl = getattr(signal, 'stop_loss', 0) or 0
    direction = getattr(signal, 'direction', '') or ''
    if not sl or not entry or not direction:
        return
    risk = abs(entry - sl)
    reward = risk * rr
    if direction == "BUY":
        signal.take_profit_1 = round(entry + reward, 5)
    else:
        signal.take_profit_1 = round(entry - reward, 5)


def _ai_generate_level(signal, settings: dict, level_type: str):
    """Ask AI to generate SL or TP levels. Returns float (SL) or list (TP)."""
    try:
        from core.model_interface import get_model_interface
        mi = get_model_interface()
        entry = getattr(signal, 'entry_price', 0)
        sym = getattr(signal, 'symbol', '?')
        direction = getattr(signal, 'direction', '?')

        try:
            from db.database import get_db
            from core.config_loader import load_prompt
            sys_prompt = load_prompt(
                get_db(), "signal_level_calc_identity",
                "You are a trading level calculator. Respond ONLY with valid JSON.",
                min_length=10)
        except Exception:
            sys_prompt = "You are a trading level calculator. Respond ONLY with valid JSON."

        if level_type == "sl":
            max_pct = settings.get("no_sl_max_pct", 2.0)
            prompt = (
                f"{direction} trade on {sym} at {entry}."
            )
            tp1 = getattr(signal, 'take_profit_1', 0)
            if tp1:
                prompt += f" TP1={tp1}."
            prompt += (
                f"\nMax allowed SL distance: {max_pct}% of entry price."
                f'\nDetermine a reasonable stop loss. Respond ONLY: {{"stop_loss": <number>}}'
            )
            resp = mi.query(role="signal_ai", system_prompt=sys_prompt,
                            user_prompt=prompt, max_tokens=50,
                            context="signal_sl_generation", duo_id=None)
            text = resp.content if hasattr(resp, 'content') else str(resp)
            match = re.search(r'"stop_loss"\s*:\s*([\d.]+)', text)
            if match:
                return round(float(match.group(1)), 5)
        else:
            max_pct = settings.get("no_tp_max_pct", 4.0)
            sl = getattr(signal, 'stop_loss', 0)
            prompt = (
                f"{direction} on {sym} at {entry}, SL={sl}."
                f"\nMax allowed TP distance: {max_pct}% of entry price."
                f"\nDetermine up to 6 TP levels. Respond ONLY: "
                f'{{"tp1": <num>, "tp2": <num or null>, ..., "tp6": <num or null>}}'
            )
            resp = mi.query(role="signal_ai", system_prompt=sys_prompt,
                            user_prompt=prompt, max_tokens=100,
                            context="signal_tp_generation", duo_id=None)
            text = resp.content if hasattr(resp, 'content') else str(resp)
            tps = []
            for i in range(1, 7):
                m = re.search(rf'"tp{i}"\s*:\s*([\d.]+)', text)
                if m:
                    tps.append(round(float(m.group(1)), 5))
            return tps if tps else None
    except Exception as e:
        logger.warning(f"AI {level_type.upper()} generation failed: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────

@dataclass
class ParsedSignal:
    """A structured trading signal extracted from an alpha message."""
    news_item_id: int = 0
    source: str = ""                    # telegram, tradingview, news
    source_detail: str = ""             # Channel name
    author: str = ""                    # Who posted the signal
    author_badge: str = ""              # Premium, verified, etc.
    symbol: str = ""                    # Standardized: XAUUSD, EURUSD
    direction: str = ""                 # BUY or SELL
    entry_price: float = 0.0           # Entry price (0 = market entry)
    entry_price_max: float = 0.0       # For range entries (e.g., 37.1-36.3)
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    take_profit_3: float = 0.0
    take_profit_4: float = 0.0
    take_profit_5: float = 0.0
    take_profit_6: float = 0.0
    confidence: float = 0.0            # AI confidence in parsing (0-100)
    signal_type: str = "entry"          # entry, update, close, tp_hit, sl_hit, analysis
    timeframe: str = ""                 # M5, H1, H4, D1 if mentioned
    risk_reward: float = 0.0           # Calculated R:R ratio
    raw_text: str = ""                  # Original message
    ai_reasoning: str = ""              # Why AI parsed it this way
    parsed_by: str = "signal_ai"        # Model that parsed it
    is_valid: bool = True               # AI assessment of validity

    # Tracking fields (filled by Stage 2)
    status: str = "pending"             # pending, active, entry_hit, tp1_hit-tp6_hit, sl_hit, expired, missed
    entry_hit_at: str = ""              # When entry price was reached
    entry_actual: float = 0.0           # Actual entry price from candle data
    sl_hit_at: str = ""
    tp1_hit_at: str = ""
    tp2_hit_at: str = ""
    tp3_hit_at: str = ""
    tp4_hit_at: str = ""
    tp5_hit_at: str = ""
    tp6_hit_at: str = ""
    highest_price: float = 0.0         # Highest price after entry (for BUY)
    lowest_price: float = 0.0          # Lowest price after entry (for SELL)
    max_favorable: float = 0.0         # Max pips in our favor
    max_adverse: float = 0.0           # Max pips against us
    outcome: str = ""                   # win, loss, breakeven, missed, invalid, expired
    outcome_pips: float = 0.0          # Pips gained/lost
    outcome_rr: float = 0.0            # R:R achieved
    resolved_at: str = ""
    resolution_method: str = ""         # candle_data, alpha_confirmation, manual, expired

    # Post-mortem fields (filled by Stage 3)
    post_mortem: str = ""               # AI analysis of why trade succeeded/failed
    lesson: str = ""                    # Key takeaway for learning


# ─────────────────────────────────────────────────────────────────────
# DB Schema Setup
# ─────────────────────────────────────────────────────────────────────

PARSED_SIGNALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS parsed_signals (
    id INT AUTO_INCREMENT PRIMARY KEY,
    news_item_id INT NOT NULL,
    source VARCHAR(100) NOT NULL,
    source_detail VARCHAR(200),
    author VARCHAR(100),
    author_badge VARCHAR(50),
    symbol VARCHAR(20) NOT NULL,
    direction ENUM('BUY','SELL') NOT NULL,
    entry_price DECIMAL(15,5) DEFAULT 0,
    entry_price_max DECIMAL(15,5) DEFAULT 0,
    stop_loss DECIMAL(15,5) DEFAULT 0,
    take_profit_1 DECIMAL(15,5) DEFAULT 0,
    take_profit_2 DECIMAL(15,5) DEFAULT 0,
    take_profit_3 DECIMAL(15,5) DEFAULT 0,
    take_profit_4 DECIMAL(15,5) DEFAULT 0,
    take_profit_5 DECIMAL(15,5) DEFAULT 0,
    take_profit_6 DECIMAL(15,5) DEFAULT 0,
    confidence DECIMAL(5,2) DEFAULT 0,
    signal_type ENUM('entry','update','close','tp_hit','sl_hit','analysis') DEFAULT 'entry',
    parent_signal_id INT DEFAULT NULL,
    tier VARCHAR(20) DEFAULT 'full',
    order_type VARCHAR(20) DEFAULT 'limit',
    expires_at DATETIME(3) DEFAULT NULL,
    timeframe VARCHAR(10),
    risk_reward DECIMAL(5,2) DEFAULT 0,
    raw_text TEXT,
    ai_reasoning TEXT,
    parsed_by VARCHAR(50) DEFAULT 'signal_ai',
    is_valid TINYINT(1) DEFAULT 1,

    -- Tracking (Stage 2)
    status ENUM('pending','active','entry_hit','tp1_hit','tp2_hit','tp3_hit','tp4_hit','tp5_hit','tp6_hit','sl_hit','expired','missed','stale') DEFAULT 'pending',
    entry_hit_at DATETIME(3) NULL,
    entry_actual DECIMAL(15,5) DEFAULT 0,
    sl_hit_at DATETIME(3) NULL,
    tp1_hit_at DATETIME(3) NULL,
    tp2_hit_at DATETIME(3) NULL,
    tp3_hit_at DATETIME(3) NULL,
    tp4_hit_at DATETIME(3) NULL,
    tp5_hit_at DATETIME(3) NULL,
    tp6_hit_at DATETIME(3) NULL,
    highest_price DECIMAL(15,5) DEFAULT 0,
    lowest_price DECIMAL(15,5) DEFAULT 0,
    max_favorable DECIMAL(10,1) DEFAULT 0,
    max_adverse DECIMAL(10,1) DEFAULT 0,
    outcome ENUM('win','loss','breakeven','missed','invalid','expired') DEFAULT NULL,
    outcome_pips DECIMAL(10,1) DEFAULT 0,
    outcome_rr DECIMAL(5,2) DEFAULT 0,
    resolved_at DATETIME(3) NULL,
    resolution_method VARCHAR(50),

    -- Post-mortem (Stage 3)
    post_mortem TEXT,
    lesson TEXT,

    -- Timestamps
    parsed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_symbol (symbol),
    INDEX idx_direction (direction),
    INDEX idx_status (status),
    INDEX idx_outcome (outcome),
    INDEX idx_source_detail (source_detail),
    INDEX idx_author (author),
    INDEX idx_parsed_at (parsed_at),
    INDEX idx_news_item (news_item_id)
)
"""

SIGNAL_PROVIDER_SCORES_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_provider_scores (
    id INT AUTO_INCREMENT PRIMARY KEY,
    source VARCHAR(100) NOT NULL,
    source_detail VARCHAR(200) NOT NULL,
    author VARCHAR(100) DEFAULT '',
    total_signals INT DEFAULT 0,
    valid_signals INT DEFAULT 0,
    entry_hit INT DEFAULT 0,
    tp1_hit INT DEFAULT 0,
    tp2_hit INT DEFAULT 0,
    tp3_hit INT DEFAULT 0,
    sl_hit INT DEFAULT 0,
    missed INT DEFAULT 0,
    expired INT DEFAULT 0,
    total_pips DECIMAL(10,1) DEFAULT 0,
    avg_pips DECIMAL(10,1) DEFAULT 0,
    win_rate DECIMAL(5,2) DEFAULT 0,
    avg_rr DECIMAL(5,2) DEFAULT 0,
    trust_score DECIMAL(5,2) DEFAULT 0.00,
    streak INT DEFAULT 0,
    best_streak INT DEFAULT 0,
    worst_streak INT DEFAULT 0,
    last_signal_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_provider (source, source_detail, author),
    INDEX idx_trust (trust_score DESC)
)
"""

# Default prompt for Signal Update AI (detect TP hit, SL hit, close, etc. from alpha messages)
SIGNAL_UPDATE_SYSTEM_PROMPT = """You are the Signal Update AI for JarvAIs. Your job is to decide if a new message from an alpha source (Telegram, TradingView, etc.) is a STATUS UPDATE for one of the open trading signals you will be given.

RULES:
1. Match the message to ONE open signal by symbol and author when possible. Same author + same symbol = likely match.
2. Determine the update type from the message content. Return ONLY valid JSON with no markdown.

VALID update_type values:
- tp1_hit, tp2_hit, tp3_hit, tp4_hit, tp5_hit, tp6_hit — take profit level was reached (include pips_mentioned if stated)
- sl_hit — stop loss was hit (include pips_mentioned if stated, usually negative)
- close — position fully closed (profit, loss, or breakeven; include pips_mentioned)
- partial_close — part of position closed (include close_portion 0-100 and pips_mentioned)
- move_sl — author moved stop loss (include new_sl price)
- add_tp — author added or changed take profit (include new_tp price)
- general_update — other relevant update that doesn't change outcome

If the message is NOT about any of the open signals, or is just commentary/analysis with no outcome, return is_update: false.

OUTPUT FORMAT (JSON only):
{
  "is_update": true,
  "matched_signal_id": <id from the list above>,
  "matched_symbol": "XAUUSD",
  "update_type": "tp1_hit",
  "pips_mentioned": 50,
  "new_sl": null,
  "new_tp": null,
  "should_close": false,
  "close_portion": null,
  "reasoning": "Author stated TP1 reached, +50 pips."
}

If not an update:
{"is_update": false, "reasoning": "Message is general commentary, no signal outcome."}
"""

SIGNAL_UPDATE_USER_TEMPLATE = """Source: {source} / {source_detail}
Author: {author}
Published: {published_at}

MESSAGE TEXT:
---
{text}
---

Open signals from this source (match by symbol/author when possible):
{open_signals}

Return only valid JSON: is_update, and if true: matched_signal_id, matched_symbol, update_type, pips_mentioned (if stated), new_sl/new_tp (if mentioned), should_close, close_portion, reasoning."""

SIGNAL_UPDATE_TRACKER_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_update_tracker (
    id INT PRIMARY KEY,
    last_checked_news_id INT DEFAULT 0,
    last_checked_at DATETIME DEFAULT NULL
)
"""

SIGNAL_UPDATES_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_updates (
    id INT AUTO_INCREMENT PRIMARY KEY,
    signal_id INT NOT NULL,
    news_item_id INT NOT NULL,
    update_type VARCHAR(50) NOT NULL,
    matched_symbol VARCHAR(20) DEFAULT NULL,
    pips_mentioned DECIMAL(10,1) DEFAULT NULL,
    new_sl DECIMAL(15,5) DEFAULT NULL,
    new_tp DECIMAL(15,5) DEFAULT NULL,
    should_close TINYINT(1) DEFAULT 0,
    close_portion DECIMAL(5,2) DEFAULT NULL,
    ai_reasoning TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_signal (signal_id),
    INDEX idx_news_item (news_item_id)
)
"""


def ensure_signal_tables():
    """Create the signal tables if they don't exist, and ensure signal_update prompt exists."""
    from db.database import get_db
    db = get_db()
    try:
        db.execute(PARSED_SIGNALS_SCHEMA)
        db.execute(SIGNAL_PROVIDER_SCORES_SCHEMA)
        db.execute(SIGNAL_UPDATE_TRACKER_SCHEMA)
        db.execute(SIGNAL_UPDATES_SCHEMA)
        # Ensure tracker row exists so _scan_for_signal_updates can read/write last_checked_news_id
        try:
            db.execute("INSERT IGNORE INTO signal_update_tracker (id, last_checked_news_id) VALUES (1, 0)")
        except Exception:
            pass
        # Ensure signal_update prompt exists so we can match alpha messages to open signals (TP/SL/close)
        _ensure_signal_update_prompt(db)
        logger.info("Signal AI tables verified/created")
    except Exception as e:
        logger.error(f"Failed to create signal tables: {e}")


def _ensure_signal_update_prompt(db):
    """One-time seed: insert default signal_update prompt only if none exists. After that, Prompt Engineer is the source of truth — we never overwrite."""
    try:
        existing = db.fetch_one(
            "SELECT id FROM prompt_versions WHERE role = %s LIMIT 1",
            ("signal_update",)
        )
        if existing:
            return
        # One-time insert so the role appears in Prompt Engineer; all edits from then on are dynamic in the UI
        db.execute(
            """INSERT INTO prompt_versions
               (role, category, version, prompt_name, description, system_prompt, user_prompt_template, is_active, activated_at)
               VALUES (%s, %s, 1, %s, %s, %s, %s, 1, NOW())""",
            (
                "signal_update",
                "role",
                "Signal Update — Match alpha messages to open signals",
                "Detects TP hit, SL hit, close, partial close, move SL from author messages and links to the correct open signal.",
                SIGNAL_UPDATE_SYSTEM_PROMPT,
                SIGNAL_UPDATE_USER_TEMPLATE,
            )
        )
        logger.info("Signal Update prompt seeded in prompt_versions (edit in Prompt Engineer from here)")
    except Exception as e:
        logger.debug(f"Could not seed signal_update prompt: {e}")


# ─────────────────────────────────────────────────────────────────────
# Stage 1: Signal Parser (AI-powered)
# ─────────────────────────────────────────────────────────────────────

# System prompt for the signal_ai role (STRICT v2)
SIGNAL_AI_SYSTEM_PROMPT = """You are the Signal AI for JarvAIs, an autonomous trading intelligence system.

YOUR ROLE: Determine if a message contains a REAL, ACTIONABLE trading signal. Most messages are NOT signals.

CRITICAL RULES — READ CAREFULLY:

A signal requires at MINIMUM: DIRECTION + ENTRY PRICE. Signals are classified into tiers:

TIER 1 (FULL) — has ALL of: direction + entry + stop loss (+ optional TPs)
TIER 2 (PARTIAL) — has direction + entry (+ optional TPs) but MISSING stop loss

Both tiers are valid signals. Return is_signal: true for BOTH.
If a message has only TPs and no entry, or only direction and no price, it is NOT a signal.

WHAT IS NOT A SIGNAL (return is_signal: false):
- Market analysis, commentary, or opinions without specific entry levels
- TP hit notifications ("TP reached", "target hit", "closed in profit")
- SL hit notifications ("stopped out", "SL hit")
- Trade updates without new entry/SL/TP levels
- Channel promotions, bragging about results, screenshots of profits
- News articles or geopolitical commentary
- Messages that only mention direction without specific entry price
- Messages like "Selling gold" or "Looking to buy EURUSD" without price levels
- General market outlook or bias statements
- Messages that say "watch this level" without a specific trade setup

WHAT IS A SIGNAL (return is_signal: true):
- "BUY XAUUSD @ 2650 SL 2640 TP 2670" — YES, full: direction + entry + SL
- "SELL EURUSD 1.0850 Stop Loss 1.0900 Take Profit 1.0750" — YES, full
- "Buy limit Gold @ 1920.00 SL @ 1905.00 TP @ 1950.00" — YES, full
- "SHORT NAS100 @ 18500 SL 18700 TP1 18200 TP2 18000" — YES, full (SHORT = SELL)
- "GOLD Entry (Sell Zone): 4975 TP-1 4972 TP-2 4969" — YES, partial: direction + entry + TPs, no SL
- "Entry (Buy Zone): 4977 Targets: TP1 4981 TP2 4985" — YES, partial: direction + entry + TPs, no SL
- "SELL NAS100 @ 18500 TP 18200" — YES, partial: direction + entry + TP, no SL

SYMBOL ALIASES:
- Gold/GOLD/XAU → XAUUSD
- Silver/XAG → XAGUSD
- Euro/EUR → EURUSD
- Cable/Pound/GBP → GBPUSD
- Yen/JPY → USDJPY
- Aussie/AUD → AUDUSD
- Kiwi/NZD → NZDUSD
- Loonie/CAD → USDCAD
- Swissy/CHF → USDCHF
- Nasdaq/NAS/US100/NQ → NAS100
- Dow/US30/DJIA → US30
- S&P/SPX → SPX500
- Bitcoin/BTC → BTCUSD
- Ethereum/ETH → ETHUSD
- Oil/Crude/WTI → USOIL
- For cross pairs like GbpNzd → GBPNZD, AudCad → AUDCAD, EurAud → EURAUD, etc.

EXTRACTION RULES (only if is_signal is true):
1. Symbol: Standardize to broker format
2. Direction: BUY or SELL only (map LONG→BUY, SHORT→SELL). Infer from "Sell Zone"→SELL, "Buy Zone"→BUY
3. Entry price: The exact price. For range entries like "1920-1925", use the midpoint
4. Stop Loss: Extract if present. Set to 0 if not mentioned (partial signal)
5. Take Profit: Extract up to 6 levels (TP1-TP6). TP without a number = TP1
6. Confidence: Rate 0-100 how confident you are in your PARSING accuracy
7. Order type: "limit" if buy/sell limit, "market" if "@ market" or "now"

RESPOND WITH ONLY valid JSON (no markdown, no code blocks).

If NOT a signal:
{"is_signal": false, "reason": "brief explanation why this is not an actionable signal"}

If IS a signal:
{
  "is_signal": true,
  "symbol": "XAUUSD",
  "direction": "BUY",
  "order_type": "limit",
  "entry_price": 2650.00,
  "stop_loss": 2640.00,
  "take_profit_1": 2670.00,
  "take_profit_2": 0,
  "take_profit_3": 0,
  "take_profit_4": 0,
  "take_profit_5": 0,
  "take_profit_6": 0,
  "timeframe": "H4",
  "confidence": 85,
  "reasoning": "Clear BUY signal with entry 2650, SL 2640, TP 2670"
}

REMEMBER: When in doubt, it is NOT a signal. Be strict. Only extract signals you are confident have direction + entry + SL."""

SIGNAL_AI_USER_TEMPLATE = """Parse this trading message:

SOURCE: {source} / {source_detail}
AUTHOR: {author}
CATEGORY: {category}
MESSAGE:
{text}"""


class SignalParser:
    """
    Stage 1: AI-powered signal parser.
    Takes raw alpha messages and extracts structured trading signals.
    """

    def __init__(self):
        self._parse_count = 0
        self._signal_count = 0
        self._error_count = 0
        self._lock = threading.Lock()

    def parse_news_item(self, news_item: Dict[str, Any]) -> Optional[ParsedSignal]:
        """
        Parse a single news_item into a structured signal.
        Returns ParsedSignal if a signal was found, None otherwise.
        """
        text = (news_item.get("headline", "") or "") + "\n" + (news_item.get("detail", "") or "")
        text = text.strip()
        if not text or len(text) < 10:
            return None

        # Cost optimization: pre-filter unlikely messages before AI call
        if not _passes_signal_prefilter(news_item):
            return None

        # Skip already-parsed items
        news_item_id = news_item.get("id", 0)
        if news_item_id and self._already_parsed(news_item_id):
            return None

        try:
            # Call AI to parse the message
            result = self._call_ai(
                text=text,
                source=news_item.get("source", "unknown"),
                source_detail=news_item.get("source_detail", ""),
                author=news_item.get("author", ""),
                category=news_item.get("category", ""),
                news_item_id=news_item_id,
            )

            if not result:
                return None

            with self._lock:
                self._parse_count += 1

            return self._build_signal_from_result(result, news_item, text)

        except Exception as e:
            with self._lock:
                self._error_count += 1
            logger.error(f"Signal parse error for news_item {news_item_id}: {e}")
            return None

    def _build_signal_from_result(self, result: Dict, news_item: Dict,
                                  text: str) -> Optional[ParsedSignal]:
        """Convert an AI parse result dict + original news_item into a ParsedSignal."""
        news_item_id = news_item.get("id", 0)

        if not result.get("is_signal", False):
            return None

        # ── 3-TIER CLASSIFICATION ─────────────────────────────────
        symbol = result.get("symbol", "").upper().strip()
        direction = result.get("direction", "").upper().strip()
        entry_price = float(result.get("entry_price", 0) or 0)
        stop_loss = float(result.get("stop_loss", 0) or 0)
        tp1 = float(result.get("take_profit_1", 0) or 0)
        tp2 = float(result.get("take_profit_2", 0) or 0)
        tp3 = float(result.get("take_profit_3", 0) or 0)
        tp4 = float(result.get("take_profit_4", 0) or 0)
        tp5 = float(result.get("take_profit_5", 0) or 0)
        tp6 = float(result.get("take_profit_6", 0) or 0)
        tier = result.get("tier", "").lower().strip()
        order_type = result.get("order_type", "limit").lower().strip()

        from db.market_symbols import is_valid_symbol
        if not is_valid_symbol(symbol):
            logger.warning(f"[SignalAI] Rejected: invalid symbol '{symbol}' "
                           f"(news_item {news_item_id})")
            return None

        if direction not in ("BUY", "SELL") and entry_price > 0 and stop_loss > 0:
            if entry_price > stop_loss:
                direction = "BUY"
                logger.debug(f"Inferred BUY: entry {entry_price} > SL {stop_loss}")
            elif entry_price < stop_loss:
                direction = "SELL"
                logger.debug(f"Inferred SELL: entry {entry_price} < SL {stop_loss}")

        has_entry = entry_price > 0
        has_sl = stop_loss > 0
        has_direction = direction in ("BUY", "SELL")

        if has_entry and has_sl:
            tier = "full"
            signal_type = "entry"
            if has_direction:
                if direction == "BUY" and stop_loss >= entry_price:
                    logger.debug(f"Rejected: BUY but SL ({stop_loss}) >= entry ({entry_price})")
                    return None
                if direction == "SELL" and stop_loss <= entry_price:
                    logger.debug(f"Rejected: SELL but SL ({stop_loss}) <= entry ({entry_price})")
                    return None
        elif has_entry or has_direction:
            tier = "partial"
            signal_type = "entry"
            logger.debug(f"Partial signal: {symbol} {direction} entry={entry_price} sl={stop_loss}")
        else:
            logger.debug(f"Rejected: no entry and no direction (news_item {news_item_id})")
            return None

        import datetime
        expires_at = None
        if tier == "partial":
            expires_at = datetime.datetime.now() + datetime.timedelta(days=7)

        signal = ParsedSignal(
            news_item_id=news_item_id,
            source=news_item.get("source", "unknown"),
            source_detail=news_item.get("source_detail", ""),
            author=news_item.get("author", ""),
            author_badge=news_item.get("author_badge", ""),
            symbol=symbol,
            direction=direction if has_direction else "",
            entry_price=entry_price,
            entry_price_max=float(result.get("entry_price_max", 0) or 0),
            stop_loss=stop_loss,
            take_profit_1=tp1,
            take_profit_2=tp2,
            take_profit_3=tp3,
            take_profit_4=tp4,
            take_profit_5=tp5,
            take_profit_6=tp6,
            confidence=float(result.get("confidence", 0) or 0),
            signal_type=signal_type,
            timeframe=result.get("timeframe", ""),
            raw_text=text[:2000],
            ai_reasoning=result.get("reasoning", ""),
            parsed_by="signal_ai_v3",
            is_valid=True,
        )

        signal.tier = tier
        signal.order_type = order_type if order_type in ("limit", "market") else "limit"
        signal.expires_at = expires_at
        signal.risk_reward = self._calculate_rr(signal)

        with self._lock:
            self._signal_count += 1

        return signal

    def _call_ai(self, text: str, source: str, source_detail: str,
                 author: str, category: str, news_item_id: int = None,
                 return_raw: bool = False):
        """Call the AI model to parse a message.
        When return_raw=True, returns the cleaned response text instead of parsed JSON.
        """
        try:
            from core.model_interface import get_model_interface
            from db.database import get_db

            db = get_db()
            model_interface = get_model_interface()

            # Try to load prompt from prompt_versions (allows customization)
            prompt_row = db.fetch_one(
                "SELECT system_prompt, user_prompt_template, model, model_provider "
                "FROM prompt_versions WHERE role = 'signal_ai' AND is_active = 1"
            )

            if prompt_row and prompt_row.get("system_prompt"):
                system_prompt = prompt_row["system_prompt"]
                user_template = prompt_row.get("user_prompt_template") or SIGNAL_AI_USER_TEMPLATE
                model_id = prompt_row.get("model")
                provider = prompt_row.get("model_provider")
                logger.debug(f"Signal AI using prompt_versions: model={model_id}, provider={provider}")
            else:
                system_prompt = SIGNAL_AI_SYSTEM_PROMPT
                user_template = SIGNAL_AI_USER_TEMPLATE
                model_id = None
                provider = None
                logger.warning("Signal AI: No active prompt found in prompt_versions for role='signal_ai'. Using hardcoded fallback.")

            # Cost optimization: override model from config if configured
            opt = _load_cost_optimization()
            model_cfg = opt.get("signal_parse_model", {})
            if model_cfg.get("model_id") and (model_cfg.get("override_prompt_versions", False) or not model_id):
                model_id = model_cfg["model_id"]
                provider = model_cfg.get("provider", "openrouter")
                logger.debug(f"Signal AI cost opt: using {model_id} from config")

            user_prompt = user_template.format(
                source=source,
                source_detail=source_detail,
                author=author,
                category=category,
                text=text[:3000],  # Limit text length for cost
            )

            # Use specific model if configured, otherwise default
            if model_id and provider:
                result = model_interface.query_with_model(
                    model_id=model_id,
                    provider=provider,
                    role="signal_ai",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.1,
                    max_tokens=500,
                    context="signal_parse_from_message",
                    source=source,
                    source_detail=source_detail,
                    author=author,
                    news_item_id=news_item_id,
                    media_type="text", duo_id=None,
                )
            else:
                result = model_interface.query(
                    role="analyst",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.1,
                    max_tokens=500,
                    context="signal_parse_from_message",
                    source=source,
                    source_detail=source_detail,
                    author=author,
                    news_item_id=news_item_id,
                    media_type="text", duo_id=None,
                )

            if not result.success:
                logger.error(f"Signal AI query FAILED: {result.error_message}. "
                            f"Check your API key in .env and model config. "
                            f"Model={model_id or 'default'}, Provider={provider or 'default'}")
                return None

            # Parse the JSON response
            content = result.content.strip()
            # Remove markdown code blocks if present
            if content.startswith("```"):
                content = re.sub(r'^```(?:json)?\s*', '', content)
                content = re.sub(r'\s*```$', '', content)

            if return_raw:
                return content

            parsed = json.loads(content)
            # Handle case where AI returns a list instead of dict
            if isinstance(parsed, list):
                parsed = parsed[0] if parsed else None
            return parsed

        except json.JSONDecodeError as e:
            logger.warning(f"Signal AI returned invalid JSON: {e}. Raw content: {content[:200] if 'content' in dir() else 'N/A'}")
            return None
        except Exception as e:
            logger.error(f"Signal AI call FAILED: {type(e).__name__}: {e}. "
                        f"This usually means the API key is missing or invalid. Check .env file.")
            return None

    def _already_parsed(self, news_item_id: int) -> bool:
        """Check if a news_item has already been parsed."""
        try:
            from db.database import get_db
            db = get_db()
            row = db.fetch_one(
                "SELECT id FROM parsed_signals WHERE news_item_id = %s LIMIT 1",
                (news_item_id,)
            )
            return row is not None
        except Exception:
            return False

    def _calculate_rr(self, signal: ParsedSignal) -> float:
        """Calculate risk:reward ratio."""
        if not signal.entry_price or not signal.stop_loss or not signal.take_profit_1:
            return 0.0

        risk = abs(signal.entry_price - signal.stop_loss)
        if risk == 0:
            return 0.0

        # Use TP1 for R:R calculation
        reward = abs(signal.take_profit_1 - signal.entry_price)
        return round(reward / risk, 2)

    # ── Batch Parsing ──────────────────────────────────────────────────

    def parse_news_items_batch(self, news_items: List[Dict]) -> List[Optional[ParsedSignal]]:
        """Parse multiple news items in a single LLM call for efficiency."""
        eligible = []
        for item in news_items:
            text = (item.get("headline", "") or "") + "\n" + (item.get("detail", "") or "")
            text = text.strip()
            if not text or len(text) < 10:
                continue
            if not _passes_signal_prefilter(item):
                continue
            nid = item.get("id", 0)
            if nid and self._already_parsed(nid):
                continue
            eligible.append(item)

        if not eligible:
            return []
        if len(eligible) == 1:
            result = self.parse_news_item(eligible[0])
            return [result]

        batch_text = ""
        for idx, item in enumerate(eligible):
            text = (item.get("headline", "") or "") + "\n" + (item.get("detail", "") or "")
            batch_text += f"\n--- MESSAGE {idx + 1} ---\n"
            batch_text += f"Source: {item.get('source', 'unknown')}\n"
            batch_text += f"Author: {item.get('author', '')}\n"
            batch_text += f"Category: {item.get('category', '')}\n"
            batch_text += f"Text: {text.strip()[:3000]}\n"

        batch_text += f"\n--- END OF MESSAGES ---\n"
        batch_text += (
            f"\nParse ALL {len(eligible)} messages above. For EACH message, "
            f"output a separate JSON object on its own line, prefixed with "
            f"'MESSAGE_N:' where N matches the message number. "
            f"If a message contains no trading signal, output 'MESSAGE_N: null'."
        )

        try:
            raw_result = self._call_ai(
                text=batch_text,
                source=eligible[0].get("source", "unknown"),
                source_detail="batch",
                author="batch",
                category="batch",
                news_item_id=None,
                return_raw=True,
            )
            if not raw_result:
                raise ValueError("Empty AI response for batch")
            return self._parse_batch_response(raw_result, eligible)
        except Exception as e:
            logger.warning(f"[SignalAI] Batch parse failed: {e}, falling back to individual")
            results = []
            for item in eligible:
                results.append(self.parse_news_item(item))
            return results

    def _parse_batch_response(self, raw_text: str,
                              eligible: List[Dict]) -> List[Optional[ParsedSignal]]:
        """Split a batch LLM response by MESSAGE_N: markers into individual signals."""
        results: List[Optional[ParsedSignal]] = []
        lines = raw_text.split("\n")

        # Build a mapping: message_number -> collected JSON text
        message_jsons: Dict[int, str] = {}
        current_msg = None
        current_lines: List[str] = []

        for line in lines:
            # Check for MESSAGE_N: prefix (e.g. "MESSAGE_1: {..." or "MESSAGE_1: null")
            match = re.match(r'MESSAGE_(\d+)\s*:\s*(.*)', line, re.IGNORECASE)
            if match:
                if current_msg is not None:
                    message_jsons[current_msg] = "\n".join(current_lines)
                current_msg = int(match.group(1))
                current_lines = [match.group(2)]
            elif current_msg is not None:
                current_lines.append(line)

        if current_msg is not None:
            message_jsons[current_msg] = "\n".join(current_lines)

        for idx, item in enumerate(eligible):
            msg_num = idx + 1
            json_text = message_jsons.get(msg_num, "").strip()

            if not json_text or json_text.lower() == "null":
                results.append(None)
                continue

            # Remove markdown code fences if wrapped
            if json_text.startswith("```"):
                json_text = re.sub(r'^```(?:json)?\s*', '', json_text)
                json_text = re.sub(r'\s*```$', '', json_text)

            try:
                parsed = json.loads(json_text)
                if isinstance(parsed, list):
                    parsed = parsed[0] if parsed else None
                if not parsed:
                    results.append(None)
                    continue

                text = (item.get("headline", "") or "") + "\n" + (item.get("detail", "") or "")
                signal = self._build_signal_from_result(parsed, item, text.strip())
                results.append(signal)
            except (json.JSONDecodeError, Exception) as e:
                logger.debug(f"[SignalAI] Batch msg {msg_num} parse error: {e}")
                try:
                    signal = self.parse_news_item(item)
                    results.append(signal)
                except Exception:
                    results.append(None)

        return results

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_parsed": self._parse_count,
                "signals_found": self._signal_count,
                "errors": self._error_count,
            }


# ─────────────────────────────────────────────────────────────────────
# Stage 2: Signal Tracker (Candle-based validation)
# ─────────────────────────────────────────────────────────────────────

class SignalTracker:
    """
    Stage 2: Track active signals using candle data.
    Checks if entry was hit, then monitors for SL/TP resolution.
    Uses Yahoo Finance as primary data source (works without MT5).
    Falls back to MT5 if available.
    """

    # Pip values for different instrument types
    PIP_VALUES = {
        "XAUUSD": 0.1, "XAGUSD": 0.01,
        "BTCUSD": 1.0, "ETHUSD": 0.1,
        "NAS100": 1.0, "US30": 1.0, "SPX500": 0.1,
        "USOIL": 0.01,
    }
    DEFAULT_PIP = 0.0001  # Forex pairs

    # Max hours to track a signal before marking expired
    MAX_TRACKING_HOURS = 168  # 7 days

    def __init__(self):
        self._check_count = 0
        self._resolved_count = 0
        self._update_hash_cache = {}  # {hash: timestamp} for dedup
        self._hash_cache_lock = threading.Lock()

    def _msg_hash(self, msg: dict) -> str:
        """Hash a message for dedup in signal update checks."""
        text = ((msg.get("headline") or "") + (msg.get("detail") or "")
                + (msg.get("source_detail") or "") + (msg.get("author") or ""))
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]

    def _is_update_duplicate(self, msg: dict) -> bool:
        """Check if we've already AI-checked this exact message content."""
        opt = _load_cost_optimization()
        hash_cfg = opt.get("signal_update_hash", {})
        if not hash_cfg.get("enabled", False):
            return False

        h = self._msg_hash(msg)
        ttl = hash_cfg.get("hash_ttl_hours", 24) * 3600
        now = time.time()

        with self._hash_cache_lock:
            # Prune expired entries periodically (every 1000 checks)
            if len(self._update_hash_cache) > 5000:
                self._update_hash_cache = {
                    k: v for k, v in self._update_hash_cache.items() if now - v < ttl
                }

            if h in self._update_hash_cache and (now - self._update_hash_cache[h]) < ttl:
                return True
            self._update_hash_cache[h] = now
            return False

    def check_active_signals(self, max_symbols: int = 5):
        """
        Main tracking loop: check all pending/active signals against price data.
        Also handles follow-up linking for partial signals and auto-expire.
        Called periodically by the orchestrator.
        
        Args:
            max_symbols: Maximum number of symbols to check per tick.
                         Uses round-robin so all symbols get checked eventually.
                         This prevents blocking the orchestrator loop.
        """
        from db.database import get_db
        db = get_db()

        try:
            # ── Step 1: Auto-expire partial signals past their expires_at ──
            self._expire_stale_partials(db)

            # ── Step 2: Link follow-up messages to partial signals ──
            self._link_followups(db)

            # ── Step 2.5: Scan alpha for signal updates (TP hit, SL hit, close) ──
            self._scan_for_signal_updates(db)

            # ── Step 3: Check full signals against candle data ──
            signals = db.fetch_all("""
                SELECT * FROM parsed_signals
                WHERE signal_type = 'entry'
                AND is_valid = 1
                AND outcome IS NULL
                AND status IN ('pending', 'active', 'entry_hit')
                AND tier = 'full'
                ORDER BY parsed_at ASC
            """)

            if not signals:
                return

            # Group by symbol to minimize API calls
            by_symbol = {}
            for sig in signals:
                sym = sig["symbol"]
                if sym not in by_symbol:
                    by_symbol[sym] = []
                by_symbol[sym].append(sig)

            # Round-robin through symbols (process max_symbols per tick)
            all_symbols = list(by_symbol.keys())
            if not hasattr(self, '_symbol_index'):
                self._symbol_index = 0
            
            symbols_to_check = []
            for i in range(min(max_symbols, len(all_symbols))):
                idx = (self._symbol_index + i) % len(all_symbols)
                symbols_to_check.append(all_symbols[idx])
            
            self._symbol_index = (self._symbol_index + max_symbols) % max(1, len(all_symbols))

            checked_signals = sum(len(by_symbol[s]) for s in symbols_to_check)
            logger.debug(f"Signal Tracker: checking {checked_signals} signals across {len(symbols_to_check)}/{len(all_symbols)} symbols")

            for symbol in symbols_to_check:
                try:
                    self._check_symbol_signals(db, symbol, by_symbol[symbol])
                except Exception as e:
                    logger.error(f"Signal Tracker error for {symbol}: {e}")

            self._check_count += 1

        except Exception as e:
            logger.error(f"Signal Tracker check failed: {e}")

    def _expire_stale_partials(self, db):
        """Auto-expire partial signals that have passed their expires_at."""
        try:
            result = db.execute("""
                UPDATE parsed_signals
                SET status = 'expired', outcome = 'expired',
                    resolution_method = 'auto_expire_partial',
                    resolved_at = NOW()
                WHERE tier = 'partial'
                AND outcome IS NULL
                AND expires_at IS NOT NULL
                AND expires_at < NOW()
            """)
            if result and result > 0:
                logger.info(f"Auto-expired {result} stale partial signals")
        except Exception as e:
            logger.error(f"Error expiring partial signals: {e}")

    def _link_followups(self, db):
        """
        Check for partial signals and look for follow-up messages from the same
        author/source that might contain SL/TP updates.
        If found, upgrade the partial to full and link the follow-up.
        """
        try:
            # Get pending partial signals
            partials = db.fetch_all("""
                SELECT ps.*, ni.published_at as signal_time
                FROM parsed_signals ps
                LEFT JOIN news_items ni ON ps.news_item_id = ni.id
                WHERE ps.tier = 'partial'
                AND ps.outcome IS NULL
                AND ps.status = 'pending'
                ORDER BY ps.parsed_at ASC
                LIMIT 50
            """)

            if not partials:
                return

            for partial in partials:
                self._try_link_followup(db, partial)

        except Exception as e:
            logger.error(f"Error linking follow-ups: {e}")

    def _try_link_followup(self, db, partial: Dict):
        """
        For a partial signal, search recent alpha from the same author/source
        for follow-up messages that contain SL/TP or close instructions.
        """
        author = partial.get("author", "")
        source_detail = partial.get("source_detail", "")
        symbol = partial.get("symbol", "")
        signal_time = partial.get("signal_time") or partial.get("parsed_at")

        if not author or not symbol or not signal_time:
            return

        # Look for follow-up messages from the same author about the same symbol
        # that were posted AFTER this signal
        followups = db.fetch_all("""
            SELECT id, headline, detail, published_at
            FROM news_items
            WHERE author = %s
            AND published_at > %s
            AND published_at < DATE_ADD(%s, INTERVAL 7 DAY)
            AND (headline LIKE %s OR detail LIKE %s)
            AND id != %s
            AND id NOT IN (SELECT news_item_id FROM parsed_signals WHERE news_item_id IS NOT NULL)
            ORDER BY published_at ASC
            LIMIT 10
        """, (
            author, signal_time, signal_time,
            f"%{symbol}%", f"%{symbol}%",
            partial.get("news_item_id", 0)
        ))

        if not followups:
            return

        # Check each follow-up for SL/TP data using simple pattern matching
        import re
        for fu in followups:
            text = (fu.get("headline", "") or "") + " " + (fu.get("detail", "") or "")
            text_lower = text.lower()

            # Check for close/exit signals
            close_patterns = ["close", "closed", "exit", "exited", "take profit", "tp hit",
                              "stopped out", "sl hit", "breakeven", "be hit"]
            is_close = any(p in text_lower for p in close_patterns)

            # Check for SL/TP numbers
            sl_match = re.search(r'(?:sl|stop.?loss|stoploss)[:\s]*(\d+\.?\d*)', text_lower)
            tp_match = re.search(r'(?:tp|take.?profit|target)[:\s]*(\d+\.?\d*)', text_lower)

            new_sl = float(sl_match.group(1)) if sl_match else 0
            new_tp = float(tp_match.group(1)) if tp_match else 0

            if is_close:
                # Author closed the position — mark as expired/closed
                db.execute("""
                    UPDATE parsed_signals
                    SET status = 'expired', outcome = 'expired',
                        resolution_method = 'author_closed',
                        resolved_at = %s, parent_signal_id = NULL
                    WHERE id = %s
                """, (fu.get("published_at"), partial["id"]))
                logger.info(f"Partial signal #{partial['id']} closed by author follow-up")
                return

            if new_sl > 0 or new_tp > 0:
                # Found SL/TP — upgrade partial to full
                updates = []
                params = []

                if new_sl > 0:
                    updates.append("stop_loss = %s")
                    params.append(new_sl)

                if new_tp > 0:
                    updates.append("take_profit_1 = %s")
                    params.append(new_tp)

                updates.append("tier = 'full'")
                updates.append("parent_signal_id = %s")
                params.append(fu["id"])
                params.append(partial["id"])

                db.execute(
                    f"UPDATE parsed_signals SET {', '.join(updates)} WHERE id = %s",
                    tuple(params)
                )

                # Recalculate direction if needed
                entry = float(partial.get("entry_price", 0) or 0)
                direction = partial.get("direction", "")
                if not direction and entry > 0 and new_sl > 0:
                    direction = "BUY" if entry > new_sl else "SELL"
                    db.execute(
                        "UPDATE parsed_signals SET direction = %s WHERE id = %s",
                        (direction, partial["id"])
                    )

                logger.info(f"Partial signal #{partial['id']} upgraded to full: SL={new_sl} TP={new_tp}")  
                return

    def _scan_for_signal_updates(self, db):
        """
        Scan ALL new alpha messages for signal status updates.
        For each new message, check if it references any open signal from the
        same source/subsource. If so, use AI to determine if it's a TP hit,
        SL hit, close, move SL, etc. and apply the update.
        """
        try:
            # Get the last checked news_item ID
            tracker = db.fetch_one(
                "SELECT last_checked_news_id FROM signal_update_tracker WHERE id = 1"
            )
            last_id = tracker["last_checked_news_id"] if tracker else 0

            # Get all open signals grouped by source_detail for quick lookup
            open_signals = db.fetch_all("""
                SELECT id, symbol, direction, entry_price, stop_loss,
                       take_profit_1, take_profit_2, take_profit_3,
                       source, source_detail, author, tier, status, parsed_at
                FROM parsed_signals
                WHERE outcome IS NULL
                AND status IN ('pending', 'active', 'entry_hit', 'tp1_hit', 'tp2_hit', 'tp3_hit', 'tp4_hit', 'tp5_hit')
                AND is_valid = 1
                ORDER BY parsed_at DESC
            """)

            if not open_signals:
                return

            # Index open signals by source_detail for fast matching
            signals_by_source = {}
            for sig in open_signals:
                sd = sig.get("source_detail", "")
                if sd not in signals_by_source:
                    signals_by_source[sd] = []
                signals_by_source[sd].append(sig)

            # Get new messages from sources that have open signals
            source_details = list(signals_by_source.keys())
            if not source_details:
                return

            placeholders = ",".join(["%s"] * len(source_details))
            new_messages = db.fetch_all(f"""
                SELECT id, source, source_detail, author, headline,
                       LEFT(detail, 1000) as detail, published_at
                FROM news_items
                WHERE id > %s
                AND source_detail IN ({placeholders})
                AND id NOT IN (SELECT news_item_id FROM parsed_signals WHERE news_item_id IS NOT NULL)
                AND id NOT IN (SELECT news_item_id FROM signal_updates)
                ORDER BY id ASC
                LIMIT 50
            """, (last_id, *source_details))

            if not new_messages:
                max_id = db.fetch_one("SELECT MAX(id) as max_id FROM news_items")
                if max_id and max_id["max_id"]:
                    db.execute(
                        "UPDATE signal_update_tracker SET last_checked_news_id = %s, last_checked_at = NOW() WHERE id = 1",
                        (max_id["max_id"],)
                    )
                return

            logger.info(f"Signal Update Detector: scanning {len(new_messages)} new messages")

            updates_found = 0
            max_checked_id = last_id

            for msg in new_messages:
                try:
                    msg_id = msg["id"]
                    max_checked_id = max(max_checked_id, msg_id)
                    sd = msg.get("source_detail", "")

                    source_signals = signals_by_source.get(sd, [])
                    if not source_signals:
                        continue

                    text = ((msg.get("headline") or "") + " " + (msg.get("detail") or "")).lower()
                    update_keywords = [
                        "tp", "sl", "hit", "close", "closed", "profit", "loss",
                        "pip", "point", "breakeven", "be ", "move", "trail",
                        "running", "secured", "exit", "target", "stopped",
                        "+", "win", "won", "congrat", "beautiful",
                    ]
                    if not any(kw in text for kw in update_keywords):
                        continue

                    # Cost optimization: skip if we've already AI-checked this exact text
                    if self._is_update_duplicate(msg):
                        logger.debug(f"Signal update hash skip: msg {msg_id}")
                        continue

                    result = self._call_update_ai(msg, source_signals)
                    if result and result.get("is_update"):
                        self._apply_signal_update(db, msg, result)
                        updates_found += 1

                except Exception as e:
                    logger.debug(f"Update scan error for msg {msg.get('id')}: {e}")

            db.execute(
                "UPDATE signal_update_tracker SET last_checked_news_id = %s, last_checked_at = NOW() WHERE id = 1",
                (max_checked_id,)
            )

            if updates_found > 0:
                logger.info(f"Signal Update Detector: found {updates_found} updates from {len(new_messages)} messages")

        except Exception as e:
            logger.error(f"Signal Update Detector error: {e}")



    def _call_update_ai(self, message, open_signals):
        """
        Call the signal_update AI to determine if a message is a status update
        for any of the open signals.
        """
        try:
            from core.model_interface import get_model_interface
            from db.database import get_db

            db = get_db()
            model_interface = get_model_interface()

            prompt_row = db.fetch_one(
                "SELECT system_prompt, user_prompt_template, model, model_provider "
                "FROM prompt_versions WHERE role = 'signal_update' AND is_active = 1"
            )

            if not prompt_row:
                logger.warning("No signal_update prompt found in DB")
                return None

            system_prompt = prompt_row["system_prompt"]
            user_template = prompt_row.get("user_prompt_template", "")

            signals_text = ""
            for s in open_signals[:20]:
                signals_text += (
                    f"  ID:{s['id']} | {s['symbol']} {s['direction']} "
                    f"Entry:{s['entry_price']} SL:{s['stop_loss']} "
                    f"TP1:{s['take_profit_1']} TP2:{s['take_profit_2']} TP3:{s['take_profit_3']} "
                    f"| Status:{s['status']} | Author:{s['author']} "
                    f"| Opened:{s['parsed_at']}\n"
                )

            user_prompt = user_template.format(
                source=message.get("source", ""),
                source_detail=message.get("source_detail", ""),
                author=message.get("author", ""),
                published_at=message.get("published_at", ""),
                text=((message.get("headline") or "") + "\n" + (message.get("detail") or ""))[:2000],
                open_signals=signals_text,
            )

            model_id = prompt_row.get("model")
            provider = prompt_row.get("model_provider")

            msg_source = message.get("source", "")
            msg_source_detail = message.get("source_detail", "")
            msg_author = message.get("author", "")
            msg_news_id = message.get("id")

            if model_id and provider:
                result = model_interface.query_with_model(
                    model_id=model_id,
                    provider=provider,
                    role="signal_update",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.1,
                    max_tokens=400,
                    context="signal_update_check",
                    source=msg_source,
                    source_detail=msg_source_detail,
                    author=msg_author,
                    news_item_id=msg_news_id,
                    media_type="text", duo_id=None,
                )
            else:
                result = model_interface.query(
                    role="analyst",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.1,
                    max_tokens=400,
                    context="signal_update_check",
                    source=msg_source,
                    source_detail=msg_source_detail,
                    author=msg_author,
                    news_item_id=msg_news_id,
                    media_type="text", duo_id=None,
                )

            if not result.success:
                return None

            import re as _re
            content = result.content.strip()
            if content.startswith("```"):
                content = _re.sub(r'^```(?:json)?\s*', '', content)
                content = _re.sub(r'\s*```$', '', content)

            return json.loads(content)

        except json.JSONDecodeError:
            return None
        except Exception as e:
            logger.debug(f"Signal update AI call failed: {e}")
            return None



    def _apply_signal_update(self, db, message, ai_result):
        """
        Apply a signal update to the matched signal.
        Stores the update in signal_updates table and modifies the parsed_signal.
        """
        try:
            from datetime import datetime as _dt

            signal_id = ai_result.get("matched_signal_id")
            update_type = ai_result.get("update_type", "general_update")
            pips = ai_result.get("pips_mentioned")
            new_sl = ai_result.get("new_sl")
            new_tp = ai_result.get("new_tp")
            should_close = ai_result.get("should_close", False)
            close_portion = ai_result.get("close_portion")
            reasoning = ai_result.get("reasoning", "")
            matched_symbol = ai_result.get("matched_symbol", "")

            if not signal_id:
                return

            signal = db.fetch_one(
                "SELECT * FROM parsed_signals WHERE id = %s AND outcome IS NULL",
                (signal_id,)
            )
            if not signal:
                logger.debug(f"Signal #{signal_id} not found or already resolved")
                return

            # Store the update in audit trail
            db.execute("""
                INSERT INTO signal_updates
                (signal_id, news_item_id, update_type, matched_symbol,
                 pips_mentioned, new_sl, new_tp, should_close, close_portion, ai_reasoning)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                signal_id, message["id"], update_type, matched_symbol,
                pips if pips else None,
                new_sl if new_sl else None,
                new_tp if new_tp else None,
                1 if should_close else 0,
                close_portion,
                reasoning,
            ))

            now = _dt.now().strftime("%Y-%m-%d %H:%M:%S")

            if update_type in ("tp1_hit", "tp2_hit", "tp3_hit", "tp4_hit", "tp5_hit", "tp6_hit"):
                tp_col = f"{update_type[:3]}_hit_at"
                outcome_pips = float(pips) if pips else 0

                if update_type == "tp3_hit" or should_close:
                    db.execute(f"""
                        UPDATE parsed_signals
                        SET status = %s, {tp_col} = %s,
                            outcome = 'win', outcome_pips = %s,
                            resolved_at = %s, resolution_method = 'alpha_confirmation'
                        WHERE id = %s
                    """, (update_type, now, outcome_pips, now, signal_id))
                    logger.info(f"Signal #{signal_id} {matched_symbol}: {update_type} - WIN (+{outcome_pips} pips)")
                else:
                    db.execute(f"""
                        UPDATE parsed_signals
                        SET status = %s, {tp_col} = %s, outcome_pips = %s
                        WHERE id = %s
                    """, (update_type, now, outcome_pips, signal_id))
                    logger.info(f"Signal #{signal_id} {matched_symbol}: {update_type} (+{outcome_pips} pips, still tracking)")

            elif update_type == "sl_hit":
                outcome_pips = float(pips) if pips else 0
                db.execute("""
                    UPDATE parsed_signals
                    SET status = 'sl_hit', sl_hit_at = %s,
                        outcome = 'loss', outcome_pips = %s,
                        resolved_at = %s, resolution_method = 'alpha_confirmation'
                    WHERE id = %s
                """, (now, -abs(outcome_pips), now, signal_id))
                logger.info(f"Signal #{signal_id} {matched_symbol}: SL HIT - LOSS (-{abs(outcome_pips)} pips)")

            elif update_type == "close" or (should_close and close_portion == "full"):
                outcome_pips = float(pips) if pips else 0
                outcome = "win" if outcome_pips > 0 else ("loss" if outcome_pips < 0 else "breakeven")
                db.execute("""
                    UPDATE parsed_signals
                    SET status = 'expired', outcome = %s, outcome_pips = %s,
                        resolved_at = %s, resolution_method = 'alpha_confirmation'
                    WHERE id = %s
                """, (outcome, outcome_pips, now, signal_id))
                logger.info(f"Signal #{signal_id} {matched_symbol}: CLOSED - {outcome} ({outcome_pips} pips)")

            elif update_type == "partial_close":
                outcome_pips = float(pips) if pips else 0
                db.execute("""
                    UPDATE parsed_signals SET outcome_pips = %s WHERE id = %s
                """, (outcome_pips, signal_id))
                logger.info(f"Signal #{signal_id} {matched_symbol}: PARTIAL CLOSE ({outcome_pips} pips)")

            elif update_type in ("move_sl", "breakeven"):
                if update_type == "breakeven":
                    entry = float(signal.get("entry_price", 0) or 0)
                    if entry > 0:
                        db.execute("""
                            UPDATE parsed_signals SET stop_loss = %s WHERE id = %s
                        """, (entry, signal_id))
                        logger.info(f"Signal #{signal_id} {matched_symbol}: SL moved to BREAKEVEN ({entry})")
                elif new_sl:
                    db.execute("""
                        UPDATE parsed_signals SET stop_loss = %s WHERE id = %s
                    """, (float(new_sl), signal_id))
                    logger.info(f"Signal #{signal_id} {matched_symbol}: SL moved to {new_sl}")

            elif update_type in ("add_sl", "add_tp"):
                # Follow-up with SL or TP for a partial signal
                if update_type == "add_sl" and new_sl:
                    sl_val = float(new_sl)
                    entry = float(signal.get("entry_price", 0) or 0)
                    direction = signal.get("direction", "")
                    # Infer direction if missing
                    if not direction and entry > 0 and sl_val > 0:
                        direction = "BUY" if entry > sl_val else "SELL"
                    updates = ["stop_loss = %s", "tier = 'full'"]
                    params = [sl_val]
                    if direction:
                        updates.append("direction = %s")
                        params.append(direction)
                    params.append(signal_id)
                    db.execute(
                        f"UPDATE parsed_signals SET {', '.join(updates)} WHERE id = %s",
                        tuple(params)
                    )
                    logger.info(f"Signal #{signal_id} {matched_symbol}: SL added ({sl_val}), upgraded to full")
                if update_type == "add_tp" and new_tp:
                    tp_val = float(new_tp)
                    db.execute("""
                        UPDATE parsed_signals SET take_profit_1 = %s WHERE id = %s
                    """, (tp_val, signal_id))
                    logger.info(f"Signal #{signal_id} {matched_symbol}: TP added ({tp_val})")

            elif update_type in ("running_profit", "running_loss"):
                if pips:
                    outcome_pips = float(pips) if update_type == "running_profit" else -abs(float(pips))
                    db.execute("""
                        UPDATE parsed_signals SET outcome_pips = %s WHERE id = %s
                    """, (outcome_pips, signal_id))
                    logger.info(f"Signal #{signal_id} {matched_symbol}: {update_type} ({outcome_pips} pips)")

            else:
                logger.info(f"Signal #{signal_id} {matched_symbol}: {update_type} - {reasoning}")

        except Exception as e:
            logger.error(f"Failed to apply signal update: {e}")


    def _check_symbol_signals(self, db, symbol: str, signals: List[Dict]):
        """Check all signals for a given symbol against candle data."""
        # Get candle data — try MT5 first, then Yahoo Finance
        candles = self._get_candle_data(symbol)
        if not candles:
            logger.debug(f"No candle data available for {symbol}")
            return

        current_price = candles[-1]["close"] if candles else 0
        pip_value = self.PIP_VALUES.get(symbol, self.DEFAULT_PIP)

        for sig in signals:
            try:
                self._evaluate_signal(db, sig, candles, current_price, pip_value)
            except Exception as e:
                logger.error(f"Error evaluating signal {sig['id']}: {e}")

    def _evaluate_signal(self, db, sig: Dict, candles: List[Dict],
                         current_price: float, pip_value: float):
        """Evaluate a single signal against candle data."""
        signal_id = sig["id"]
        status = sig["status"]
        direction = sig["direction"]
        entry = float(sig.get("entry_price", 0) or 0)
        entry_max = float(sig.get("entry_price_max", 0) or 0)
        sl = float(sig.get("stop_loss", 0) or 0)
        tp1 = float(sig.get("take_profit_1", 0) or 0)
        tp2 = float(sig.get("take_profit_2", 0) or 0)
        tp3 = float(sig.get("take_profit_3", 0) or 0)
        tp4 = float(sig.get("take_profit_4", 0) or 0)
        tp5 = float(sig.get("take_profit_5", 0) or 0)
        tp6 = float(sig.get("take_profit_6", 0) or 0)
        parsed_at = sig.get("parsed_at")

        # Check for expiry
        if parsed_at:
            hours_since = (datetime.utcnow() - parsed_at).total_seconds() / 3600
            if hours_since > self.MAX_TRACKING_HOURS:
                self._resolve_signal(db, signal_id, "expired", "expired",
                                     0, 0, "candle_data")
                return

        # Filter candles to only those after signal was parsed
        relevant_candles = [c for c in candles if c["time"] >= parsed_at] if parsed_at else candles

        if not relevant_candles:
            return

        # Stage A: Check if entry was hit (for pending signals)
        if status == "pending":
            if entry <= 0:
                # Market entry — consider immediately active at the first candle after signal
                first_candle = relevant_candles[0]
                entry_actual = first_candle["open"]
                db.execute("""
                    UPDATE parsed_signals
                    SET status = 'active', entry_hit_at = %s, entry_actual = %s
                    WHERE id = %s
                """, (first_candle["time"], entry_actual, signal_id))
                sig["status"] = "active"
                sig["entry_actual"] = entry_actual
                status = "active"
            else:
                # Check if price reached entry level
                for candle in relevant_candles:
                    entry_hit = False
                    if direction == "BUY":
                        # For BUY, entry hit when low <= entry price
                        if entry_max > 0:
                            entry_hit = candle["low"] <= max(entry, entry_max)
                        else:
                            entry_hit = candle["low"] <= entry
                    else:
                        # For SELL, entry hit when high >= entry price
                        if entry_max > 0:
                            entry_hit = candle["high"] >= min(entry, entry_max)
                        else:
                            entry_hit = candle["high"] >= entry

                    if entry_hit:
                        entry_actual = entry  # Assume filled at our price
                        db.execute("""
                            UPDATE parsed_signals
                            SET status = 'active', entry_hit_at = %s, entry_actual = %s
                            WHERE id = %s
                        """, (candle["time"], entry_actual, signal_id))
                        sig["status"] = "active"
                        sig["entry_actual"] = entry_actual
                        status = "active"
                        break

                if status == "pending":
                    return  # Entry not yet hit

        # Stage B: Track active signals for SL/TP hits
        if status in ("active", "entry_hit"):
            entry_actual = float(sig.get("entry_actual", 0) or entry)
            entry_time = sig.get("entry_hit_at") or parsed_at

            # Get candles after entry
            post_entry = [c for c in relevant_candles if c["time"] >= entry_time] if entry_time else relevant_candles

            if not post_entry:
                return

            # Track extremes
            highest = max(c["high"] for c in post_entry)
            lowest = min(c["low"] for c in post_entry)

            if direction == "BUY":
                max_favorable_pips = (highest - entry_actual) / pip_value
                max_adverse_pips = (entry_actual - lowest) / pip_value
            else:
                max_favorable_pips = (entry_actual - lowest) / pip_value
                max_adverse_pips = (highest - entry_actual) / pip_value

            # Check SL hit
            sl_hit = False
            sl_hit_time = None
            if sl > 0:
                for candle in post_entry:
                    if direction == "BUY" and candle["low"] <= sl:
                        sl_hit = True
                        sl_hit_time = candle["time"]
                        break
                    elif direction == "SELL" and candle["high"] >= sl:
                        sl_hit = True
                        sl_hit_time = candle["time"]
                        break

            # Check TP hits (all 6 levels)
            tp_levels = [(tp1, "tp1"), (tp2, "tp2"), (tp3, "tp3"), (tp4, "tp4"), (tp5, "tp5"), (tp6, "tp6")]
            tp_hits = {}
            tp_times = {}
            for candle in post_entry:
                for tp_val, tp_name in tp_levels:
                    if tp_val > 0 and tp_name not in tp_hits:
                        if (direction == "BUY" and candle["high"] >= tp_val) or \
                           (direction == "SELL" and candle["low"] <= tp_val):
                            tp_hits[tp_name] = True
                            tp_times[tp_name] = candle["time"]

            # Determine outcome
            # Priority: SL hit before any TP = loss (check timestamps)
            if sl_hit and sl_hit_time:
                # Check if any TP was hit BEFORE SL
                tp_before_sl = None
                for tp_val, tp_name in tp_levels:
                    if tp_name in tp_hits and tp_times[tp_name] <= sl_hit_time:
                        tp_before_sl = (tp_name, tp_val)

                if tp_before_sl:
                    tp_name, tp_price = tp_before_sl
                    pips = abs(tp_price - entry_actual) / pip_value
                    rr = pips / (abs(entry_actual - sl) / pip_value) if sl else 0
                    outcome = "win"
                    outcome_status = f"{tp_name}_hit"
                else:
                    pips = -abs(entry_actual - sl) / pip_value
                    rr = -1.0
                    outcome = "loss"
                    outcome_status = "sl_hit"

                self._resolve_signal(
                    db, signal_id, outcome_status, outcome,
                    round(pips, 1), round(rr, 2), "candle_data",
                    sl_hit_at=sl_hit_time,
                    tp1_hit_at=tp_times.get("tp1"), tp2_hit_at=tp_times.get("tp2"),
                    tp3_hit_at=tp_times.get("tp3"), tp4_hit_at=tp_times.get("tp4"),
                    tp5_hit_at=tp_times.get("tp5"), tp6_hit_at=tp_times.get("tp6"),
                    highest_price=highest, lowest_price=lowest,
                    max_favorable=round(max_favorable_pips, 1),
                    max_adverse=round(max_adverse_pips, 1),
                )
                return

            # Find highest TP hit (tp6 > tp5 > ... > tp1)
            highest_tp = None
            highest_tp_price = 0
            for tp_val, tp_name in reversed(tp_levels):
                if tp_name in tp_hits:
                    highest_tp = tp_name
                    highest_tp_price = tp_val
                    break

            if highest_tp:
                pips = abs(highest_tp_price - entry_actual) / pip_value
                rr = pips / (abs(entry_actual - sl) / pip_value) if sl else 0
                self._resolve_signal(
                    db, signal_id, f"{highest_tp}_hit", "win",
                    round(pips, 1), round(rr, 2), "candle_data",
                    tp1_hit_at=tp_times.get("tp1"), tp2_hit_at=tp_times.get("tp2"),
                    tp3_hit_at=tp_times.get("tp3"), tp4_hit_at=tp_times.get("tp4"),
                    tp5_hit_at=tp_times.get("tp5"), tp6_hit_at=tp_times.get("tp6"),
                    highest_price=highest, lowest_price=lowest,
                    max_favorable=round(max_favorable_pips, 1),
                    max_adverse=round(max_adverse_pips, 1),
                )
                return

            # Still active — update tracking fields
            db.execute("""
                UPDATE parsed_signals
                SET highest_price = %s, lowest_price = %s,
                    max_favorable = %s, max_adverse = %s,
                    status = 'active'
                WHERE id = %s
            """, (highest, lowest, round(max_favorable_pips, 1),
                  round(max_adverse_pips, 1), signal_id))

    def _resolve_signal(self, db, signal_id: int, status: str, outcome: str,
                        pips: float, rr: float, method: str, **kwargs):
        """Mark a signal as resolved with outcome.
        If the signal belongs to a trading_floor dossier, sync the dossier status too."""
        now = datetime.utcnow()

        update_fields = {
            "status": status,
            "outcome": outcome,
            "outcome_pips": pips,
            "outcome_rr": rr,
            "resolved_at": now,
            "resolution_method": method,
        }
        update_fields.update({k: v for k, v in kwargs.items() if v is not None})

        set_parts = []
        params = []
        for k, v in update_fields.items():
            set_parts.append(f"{k} = %s")
            params.append(v)
        params.append(signal_id)

        db.execute(
            f"UPDATE parsed_signals SET {', '.join(set_parts)} WHERE id = %s",
            tuple(params)
        )

        self._resolved_count += 1
        logger.info(f"Signal #{signal_id} resolved: {outcome} ({pips} pips, {rr}R)")

        # Sync back to trading_floor dossier if applicable
        self._sync_dossier_from_signal(db, signal_id, outcome)

    def _sync_dossier_from_signal(self, db, signal_id: int, outcome: str):
        """If a resolved signal belongs to a trading_floor dossier, update the dossier."""
        try:
            sig = db.fetch_one("""
                SELECT source, news_item_id FROM parsed_signals
                WHERE id = %s AND source = 'trading_floor'
            """, (signal_id,))
            if not sig:
                return
            dossier_id = sig.get("news_item_id") or 0
            if dossier_id <= 0:
                return
            dossier = db.fetch_one(
                "SELECT id, status, stop_loss, take_profit_1, current_price "
                "FROM trade_dossiers WHERE id = %s", (dossier_id,))
            if not dossier or dossier["status"] in ("won", "lost", "expired", "abandoned"):
                return

            from services.trading_floor import transition_dossier, _finalise_pnl

            if outcome == "win":
                close_price = float(dossier.get("take_profit_1") or
                                    dossier.get("current_price") or 0)
                if close_price > 0:
                    _finalise_pnl(db, dossier_id, close_price)
                transition_dossier(db, dossier_id, "won",
                                   f"Signal #{signal_id} resolved as win (evaluator)")
            elif outcome == "loss":
                close_price = float(dossier.get("stop_loss") or
                                    dossier.get("current_price") or 0)
                if close_price > 0:
                    _finalise_pnl(db, dossier_id, close_price)
                transition_dossier(db, dossier_id, "lost",
                                   f"Signal #{signal_id} resolved as loss (evaluator)")
            logger.info(f"[SignalAI] _sync_dossier_from_signal signal_id=%d "
                        f"dossier_id=%d outcome=%s", signal_id, dossier_id, outcome)
        except Exception as e:
            logger.debug(f"[SignalAI] Dossier sync skipped for signal #{signal_id}: {e}")

    def _get_candle_data(self, symbol: str) -> List[Dict]:
        """
        Get candle data for a symbol.
        Chain: MT5 -> Yahoo -> CCXT (Bybit -> Blofin).
        Returns list of dicts with: time, open, high, low, close, volume
        """
        candles = self._get_mt5_candles(symbol)
        if candles:
            return candles

        candles = self._get_yahoo_candles(symbol)
        if candles:
            return candles

        candles = self._get_ccxt_candles(symbol)
        if candles:
            return candles

        return []

    def _get_ccxt_candles(self, symbol: str) -> List[Dict]:
        """CCXT fallback: try preferred, fallback, then remaining exchanges for M15 candles (~5 days)."""
        try:
            from services.candle_collector import (
                _resolve_ccxt_symbol, _looks_like_crypto)
            from db.database import get_db
        except ImportError:
            return []

        if not _looks_like_crypto(symbol):
            return []

        _BITGET_OPTS = {"defaultType": "swap", "defaultSubType": "linear"}

        try:
            import ccxt
            from core.config import get_config
            cfg = get_config()
            candle_cfg = cfg.raw.get("candle_collection", {})
            preferred = candle_cfg.get("preferred_exchange", "bybit")
            fallback = candle_cfg.get("fallback_exchange", "blofin")
            _try_order = [preferred] + [e for e in (fallback, "bitget", "bybit", "blofin") if e != preferred]

            db = get_db()
            for exch_id in _try_order:
                ccxt_sym = _resolve_ccxt_symbol(db, symbol, exch_id)
                if not ccxt_sym:
                    continue
                try:
                    opts = {"enableRateLimit": True, "timeout": 15000}
                    if exch_id == "bitget":
                        opts["options"] = _BITGET_OPTS
                    exchange = getattr(ccxt, exch_id)(opts)
                    since_ms = int((
                        __import__("datetime").datetime.utcnow()
                        - __import__("datetime").timedelta(days=5)
                    ).timestamp() * 1000)
                    ohlcv = exchange.fetch_ohlcv(
                        ccxt_sym, timeframe="15m", since=since_ms, limit=500)
                    if ohlcv:
                        logger.info(f"[SignalAI] CCXT candles via {exch_id} "
                                    f"for {symbol} ({ccxt_sym}): {len(ohlcv)} bars")
                        return [
                            {
                                "time": datetime.utcfromtimestamp(bar[0] / 1000),
                                "open": float(bar[1]),
                                "high": float(bar[2]),
                                "low": float(bar[3]),
                                "close": float(bar[4]),
                                "volume": float(bar[5] or 0),
                            }
                            for bar in ohlcv
                        ]
                except Exception as ex:
                    logger.debug(f"[SignalAI] CCXT {exch_id} failed for "
                                 f"{symbol}: {ex}")
                    continue
        except Exception as ex:
            logger.debug(f"[SignalAI] CCXT candle fallback error: {ex}")
        return []

    def _get_mt5_candles(self, symbol: str) -> List[Dict]:
        """Try to get candles from MT5."""
        try:
            from core.mt5_executor import get_mt5_executor
            mt5 = get_mt5_executor()
            if not mt5:
                return []

            candle_objects = mt5.get_candles(symbol, "M15", 500)  # ~5 days of M15
            if not candle_objects:
                return []

            return [
                {
                    "time": c.time,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.tick_volume,
                }
                for c in candle_objects
            ]
        except Exception:
            return []

    def _get_yahoo_candles(self, symbol: str) -> List[Dict]:
        """Get candle data from Yahoo Finance (15m intervals, 5 days)."""
        import requests

        yahoo_ticker = None
        try:
            from db.market_symbols import get_yahoo_ticker
            from db.database import get_db
            yahoo_ticker = get_yahoo_ticker(get_db(), symbol)
        except Exception:
            pass

        if not yahoo_ticker:
            try:
                from services.candle_collector import (
                    YAHOO_SYMBOL_MAP, _CRYPTO_BASES)
                s = symbol.upper()
                if s in YAHOO_SYMBOL_MAP:
                    yahoo_ticker = YAHOO_SYMBOL_MAP[s]
                elif s in _CRYPTO_BASES:
                    yahoo_ticker = f"{s}-USD"
                elif s.endswith("USDT"):
                    yahoo_ticker = f"{s[:-4]}-USD"
                else:
                    yahoo_ticker = symbol
            except ImportError:
                yahoo_ticker = symbol

        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}"
            params = {"interval": "15m", "range": "5d"}
            headers = {"User-Agent": "Mozilla/5.0"}
            r = requests.get(url, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()

            if "chart" not in data or not data["chart"]["result"]:
                return []

            result = data["chart"]["result"][0]
            timestamps = result.get("timestamp", [])
            quotes = result.get("indicators", {}).get("quote", [{}])[0]

            if not timestamps:
                return []

            candles = []
            for i, ts in enumerate(timestamps):
                o = quotes.get("open", [None])[i]
                h = quotes.get("high", [None])[i]
                l = quotes.get("low", [None])[i]
                c = quotes.get("close", [None])[i]
                v = quotes.get("volume", [0])[i]

                if o is None or h is None or l is None or c is None:
                    continue

                candles.append({
                    "time": datetime.utcfromtimestamp(ts),
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                    "volume": int(v or 0),
                })

            return candles

        except Exception as e:
            logger.debug(f"Yahoo Finance candle fetch failed for {symbol}: {e}")
            return []

    def get_stats(self) -> Dict[str, Any]:
        return {
            "checks_run": self._check_count,
            "signals_resolved": self._resolved_count,
        }


# ─────────────────────────────────────────────────────────────────────
# Stage 3: Signal Scorer (Provider trust + Post-mortem)
# ─────────────────────────────────────────────────────────────────────

class SignalScorer:
    """
    Stage 3: Score signal providers and feed resolved signals to Post-Mortem AI.
    Updates trust scores for source → source_detail → author hierarchy.
    """

    def __init__(self):
        self._scored_count = 0

    def score_resolved_signals(self):
        """
        Process all resolved signals that haven't been scored yet.
        Called periodically by the orchestrator.
        """
        from db.database import get_db
        db = get_db()

        try:
            # Find resolved signals without post_mortem (not yet scored)
            resolved = db.fetch_all("""
                SELECT * FROM parsed_signals
                WHERE outcome IS NOT NULL
                AND outcome != ''
                AND post_mortem IS NULL
                AND signal_type = 'entry'
                ORDER BY resolved_at ASC
                LIMIT 10
            """)

            if not resolved:
                return

            logger.info(f"Signal Scorer: processing {len(resolved)} resolved signals")

            for sig in resolved:
                try:
                    # Update provider scores
                    self._update_provider_score(db, sig)

                    # Update managed_sources scores
                    self._update_managed_source_score(db, sig)

                    # Update alpha_source_scores
                    self._update_alpha_source_score(db, sig)

                    # Run post-mortem AI analysis
                    post_mortem = self._run_post_mortem(db, sig)

                    # Store the post-mortem
                    db.execute("""
                        UPDATE parsed_signals
                        SET post_mortem = %s, lesson = %s
                        WHERE id = %s
                    """, (
                        post_mortem.get("analysis", ""),
                        post_mortem.get("lesson", ""),
                        sig["id"]
                    ))

                    self._scored_count += 1

                except Exception as e:
                    logger.error(f"Scoring error for signal {sig['id']}: {e}")
                    # Mark as scored anyway to prevent infinite retry
                    db.execute(
                        "UPDATE parsed_signals SET post_mortem = %s WHERE id = %s",
                        (f"Scoring error: {e}", sig["id"])
                    )

        except Exception as e:
            logger.error(f"Signal Scorer failed: {e}")

    def _update_provider_score(self, db, sig: Dict):
        """Update the signal_provider_scores table for this provider."""
        source = sig["source"]
        source_detail = sig["source_detail"] or ""
        author = sig["author"] or ""
        outcome = sig["outcome"]
        pips = float(sig.get("outcome_pips", 0) or 0)

        # Upsert provider score
        existing = db.fetch_one("""
            SELECT * FROM signal_provider_scores
            WHERE source = %s AND source_detail = %s AND author = %s
        """, (source, source_detail, author))

        if existing:
            # Update existing
            total = existing["total_signals"] + 1
            is_win = outcome == "win"
            all_tp_statuses = ("tp1_hit", "tp2_hit", "tp3_hit", "tp4_hit", "tp5_hit", "tp6_hit")
            tp1 = existing["tp1_hit"] + (1 if sig["status"] in all_tp_statuses else 0)
            tp2 = existing["tp2_hit"] + (1 if sig["status"] in all_tp_statuses[1:] else 0)
            tp3 = existing["tp3_hit"] + (1 if sig["status"] in all_tp_statuses[2:] else 0)
            sl = existing["sl_hit"] + (1 if sig["status"] == "sl_hit" else 0)
            missed = existing["missed"] + (1 if outcome == "missed" else 0)
            expired = existing["expired"] + (1 if outcome == "expired" else 0)
            entry_hit = existing["entry_hit"] + (1 if sig.get("entry_hit_at") else 0)
            valid = existing["valid_signals"] + (1 if sig.get("is_valid") else 0)
            total_pips = float(existing["total_pips"]) + pips
            wins = existing["tp1_hit"] + (1 if is_win else 0)  # Approximate
            win_rate = round((wins / total) * 100, 2) if total > 0 else 0

            # Streak tracking
            streak = existing["streak"]
            if is_win:
                streak = max(streak, 0) + 1
            elif outcome == "loss":
                streak = min(streak, 0) - 1
            best_streak = max(existing["best_streak"], streak)
            worst_streak = min(existing["worst_streak"], streak)

            # Trust score: starts at 50, adjusts based on performance
            # Win = +2, Loss = -3 (asymmetric to penalize losses more)
            # TP2 = +1 bonus, TP3 = +2 bonus
            trust = float(existing["trust_score"])
            if is_win:
                trust += 2.0
                if sig["status"] == "tp2_hit":
                    trust += 1.0
                elif sig["status"] in ("tp3_hit", "tp4_hit"):
                    trust += 2.0
                elif sig["status"] in ("tp5_hit", "tp6_hit"):
                    trust += 3.0
            elif outcome == "loss":
                trust -= 3.0
            elif outcome == "expired":
                trust -= 0.5
            trust = max(0, min(100, trust))

            db.execute("""
                UPDATE signal_provider_scores
                SET total_signals = %s, valid_signals = %s, entry_hit = %s,
                    tp1_hit = %s, tp2_hit = %s, tp3_hit = %s, sl_hit = %s,
                    missed = %s, expired = %s,
                    total_pips = %s, avg_pips = %s, win_rate = %s,
                    avg_rr = %s, trust_score = %s,
                    streak = %s, best_streak = %s, worst_streak = %s,
                    last_signal_at = NOW()
                WHERE id = %s
            """, (
                total, valid, entry_hit,
                tp1, tp2, tp3, sl,
                missed, expired,
                round(total_pips, 1), round(total_pips / total, 1) if total else 0,
                win_rate,
                round(float(sig.get("outcome_rr", 0) or 0), 2),
                round(trust, 2),
                streak, best_streak, worst_streak,
                existing["id"]
            ))
        else:
            # Insert new provider
            is_win = outcome == "win"
            trust = 52.0 if is_win else 47.0  # Slight bias based on first signal

            db.execute("""
                INSERT INTO signal_provider_scores
                (source, source_detail, author, total_signals, valid_signals,
                 entry_hit, tp1_hit, tp2_hit, tp3_hit, sl_hit, missed, expired,
                 total_pips, avg_pips, win_rate, avg_rr, trust_score,
                 streak, best_streak, worst_streak, last_signal_at)
                VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """, (
                source, source_detail, author,
                1 if sig.get("is_valid") else 0,
                1 if sig.get("entry_hit_at") else 0,
                1 if sig["status"] in ("tp1_hit", "tp2_hit", "tp3_hit", "tp4_hit", "tp5_hit", "tp6_hit") else 0,
                1 if sig["status"] in ("tp2_hit", "tp3_hit", "tp4_hit", "tp5_hit", "tp6_hit") else 0,
                1 if sig["status"] in ("tp3_hit", "tp4_hit", "tp5_hit", "tp6_hit") else 0,
                1 if sig["status"] == "sl_hit" else 0,
                1 if outcome == "missed" else 0,
                1 if outcome == "expired" else 0,
                round(pips, 1), round(pips, 1),
                100.0 if is_win else 0.0,
                round(float(sig.get("outcome_rr", 0) or 0), 2),
                round(trust, 2),
                1 if is_win else (-1 if outcome == "loss" else 0),
                1 if is_win else 0,
                -1 if outcome == "loss" else 0,
            ))

    def _update_managed_source_score(self, db, sig: Dict):
        """Update the managed_sources table with signal performance."""
        try:
            source_detail = sig.get("source_detail", "")
            if not source_detail:
                return

            outcome = sig["outcome"]
            pips = float(sig.get("outcome_pips", 0) or 0)
            is_win = outcome == "win"

            db.execute("""
                UPDATE managed_sources
                SET total_signals = total_signals + 1,
                    winning_signals = winning_signals + %s,
                    total_pnl = total_pnl + %s,
                    score = GREATEST(0, LEAST(100,
                        score + CASE WHEN %s THEN 2 ELSE -3 END
                    )),
                    updated_at = NOW()
                WHERE name = %s OR name LIKE %s
            """, (
                1 if is_win else 0,
                pips,
                is_win,
                source_detail,
                f"%{source_detail}%",
            ))
        except Exception as e:
            logger.debug(f"managed_sources update: {e}")

    def _update_alpha_source_score(self, db, sig: Dict):
        """Update the alpha_source_scores table."""
        try:
            source = sig["source"]
            source_detail = sig.get("source_detail", "")
            author = sig.get("author", "")
            outcome = sig["outcome"]
            pips = float(sig.get("outcome_pips", 0) or 0)
            is_win = outcome == "win"

            # Upsert into alpha_source_scores
            existing = db.fetch_one("""
                SELECT id FROM alpha_source_scores
                WHERE source_name = %s AND sub_source = %s
            """, (source, source_detail))

            if existing:
                db.execute("""
                    UPDATE alpha_source_scores
                    SET total_trades = total_trades + 1,
                        winning_trades = winning_trades + %s,
                        losing_trades = losing_trades + %s,
                        total_pnl = total_pnl + %s,
                        avg_pnl = total_pnl / GREATEST(total_trades, 1),
                        win_rate = (winning_trades * 100.0) / GREATEST(total_trades, 1),
                        score = GREATEST(0, LEAST(100,
                            COALESCE(score, 50) + CASE WHEN %s THEN 2 ELSE -3 END
                        )),
                        last_trade_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                """, (
                    1 if is_win else 0,
                    1 if outcome == "loss" else 0,
                    pips,
                    is_win,
                    existing["id"],
                ))
            else:
                db.execute("""
                    INSERT INTO alpha_source_scores
                    (source_name, sub_source, sub_sub_source,
                     total_trades, winning_trades, losing_trades,
                     total_pnl, avg_pnl, win_rate, score, last_trade_at)
                    VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s, %s, NOW())
                """, (
                    source, source_detail, author,
                    1 if is_win else 0,
                    1 if outcome == "loss" else 0,
                    pips, pips,
                    100.0 if is_win else 0.0,
                    52.0 if is_win else 47.0,
                ))
        except Exception as e:
            logger.debug(f"alpha_source_scores update: {e}")

    def _run_post_mortem(self, db, sig: Dict) -> Dict[str, str]:
        """Run post-mortem AI analysis on a resolved signal."""
        try:
            from core.model_interface import get_model_interface
            model_interface = get_model_interface()

            # Build the context for post-mortem
            context = self._build_post_mortem_context(db, sig)

            _PM_FALLBACK = (
                "You are the Signal Post-Mortem Analyst for JarvAIs.\n"
                "Analyze this resolved trading signal and determine:\n"
                "1. Why did this signal succeed or fail?\n"
                "2. Were there warning signs we could have caught?\n"
                "3. How reliable is this signal provider based on this outcome?\n"
                "4. What lesson should the system learn from this?\n\n"
                'Respond with JSON only:\n'
                '{\n'
                '  "analysis": "2-3 sentence analysis of what happened",\n'
                '  "lesson": "One clear, actionable lesson for the system",\n'
                '  "provider_assessment": "brief assessment of provider quality",\n'
                '  "would_take_again": true/false\n'
                '}')
            try:
                from core.config_loader import load_prompt
                system_prompt = load_prompt(
                    db, "signal_postmortem_identity",
                    _PM_FALLBACK, min_length=10)
            except Exception:
                system_prompt = _PM_FALLBACK

            result = model_interface.query(
                role="analyst",
                system_prompt=system_prompt,
                user_prompt=context,
                temperature=0.3,
                max_tokens=400,
                context="signal_postmortem_analysis",
                source=sig.get("source", ""),
                source_detail=sig.get("source_detail", ""),
                author=sig.get("author", ""),
                news_item_id=sig.get("news_item_id"),
                media_type="text", duo_id=None,
            )

            if result.success:
                content = result.content.strip()
                if content.startswith("```"):
                    content = re.sub(r'^```(?:json)?\s*', '', content)
                    content = re.sub(r'\s*```$', '', content)
                return json.loads(content)

        except Exception as e:
            logger.debug(f"Post-mortem AI failed: {e}")

        return {
            "analysis": f"Signal {sig['outcome']}: {sig.get('outcome_pips', 0)} pips",
            "lesson": "Automated tracking — no AI analysis available",
        }

    def _build_post_mortem_context(self, db, sig: Dict) -> str:
        """Build context string for post-mortem analysis."""
        # Get the original alpha message
        original = ""
        if sig.get("news_item_id"):
            ni = db.fetch_one(
                "SELECT headline, detail, source_detail, author FROM news_items WHERE id = %s",
                (sig["news_item_id"],)
            )
            if ni:
                original = f"Original message from {ni.get('author', 'unknown')} in {ni.get('source_detail', 'unknown')}:\n{ni.get('headline', '')}\n{ni.get('detail', '')[:500]}"

        # Check for alpha follow-up messages (bragging, TP confirmations, etc.)
        follow_ups = ""
        if sig.get("source_detail") and sig.get("symbol"):
            fups = db.fetch_all("""
                SELECT headline, detail, published_at FROM news_items
                WHERE source_detail = %s
                AND published_at > %s
                AND (headline LIKE '%%profit%%' OR headline LIKE '%%TP%%'
                     OR headline LIKE '%%target%%' OR headline LIKE '%%reached%%'
                     OR headline LIKE '%%pips%%' OR headline LIKE '%%closed%%')
                ORDER BY published_at ASC
                LIMIT 3
            """, (sig["source_detail"], sig.get("parsed_at")))
            if fups:
                follow_ups = "\n\nFollow-up messages from same channel:\n"
                for f in fups:
                    follow_ups += f"- {f['headline'][:200]}\n"

        # Get provider track record
        provider = db.fetch_one("""
            SELECT trust_score, win_rate, total_signals, total_pips, streak
            FROM signal_provider_scores
            WHERE source = %s AND source_detail = %s AND author = %s
        """, (sig["source"], sig.get("source_detail", ""), sig.get("author", "")))

        provider_info = ""
        if provider:
            provider_info = (
                f"\nProvider track record: Trust={provider['trust_score']}, "
                f"Win Rate={provider['win_rate']}%, "
                f"Total Signals={provider['total_signals']}, "
                f"Total Pips={provider['total_pips']}, "
                f"Current Streak={provider['streak']}"
            )

        return f"""SIGNAL POST-MORTEM ANALYSIS

Signal Details:
- Symbol: {sig['symbol']}
- Direction: {sig['direction']}
- Entry: {sig.get('entry_price', 'market')} (Actual: {sig.get('entry_actual', 'N/A')})
- Stop Loss: {sig.get('stop_loss', 'none')}
- TP1: {sig.get('take_profit_1', 'none')} | TP2: {sig.get('take_profit_2', 'none')} | TP3: {sig.get('take_profit_3', 'none')}
- R:R Target: {sig.get('risk_reward', 'N/A')}

Outcome:
- Result: {sig['outcome'].upper()}
- Status: {sig['status']}
- Pips: {sig.get('outcome_pips', 0)}
- R:R Achieved: {sig.get('outcome_rr', 0)}
- Max Favorable: {sig.get('max_favorable', 0)} pips
- Max Adverse: {sig.get('max_adverse', 0)} pips
- Duration: {sig.get('parsed_at')} → {sig.get('resolved_at', 'N/A')}

{original}
{follow_ups}
{provider_info}

Source: {sig['source']} / {sig.get('source_detail', '')}
Author: {sig.get('author', 'unknown')}
AI Parsing Confidence: {sig.get('confidence', 0)}%"""

    def get_stats(self) -> Dict[str, Any]:
        return {"signals_scored": self._scored_count}


# ─────────────────────────────────────────────────────────────────────
# Signal AI Service (Singleton orchestrator for all 3 stages)
# ─────────────────────────────────────────────────────────────────────

_signal_ai_instance = None
_signal_ai_lock = threading.Lock()


class SignalAI:
    """
    Master Signal AI service that coordinates all 3 stages.
    Integrates with the Alpha Orchestrator.
    """

    def __init__(self):
        ensure_signal_tables()
        self.parser = SignalParser()
        self.tracker = SignalTracker()
        self.scorer = SignalScorer()
        self._running = False
        self._parse_queue: List[Dict] = []
        self._queue_lock = threading.Lock()
        self._last_track_time = 0
        self._last_score_time = 0
        self._track_interval = 120    # Check candles every 2 minutes
        self._score_interval = 300    # Score resolved signals every 5 minutes
        logger.info("Signal AI initialized (Parser + Tracker + Scorer)")

    def queue_for_parsing(self, news_item: Dict[str, Any]):
        """Queue a news_item for signal parsing (called by orchestrator)."""
        with self._queue_lock:
            self._parse_queue.append(news_item)

    def process_queue(self):
        """Process all queued items. Batch 3-5 similar items per LLM call."""
        with self._queue_lock:
            items = self._parse_queue[:]
            self._parse_queue.clear()

        if not items:
            return

        from db.database import get_db
        db = get_db()

        # Group items by source for efficient batching
        from collections import defaultdict
        source_groups = defaultdict(list)
        for item in items:
            source_groups[item.get("source", "unknown")].append(item)

        parsed_count = 0
        for source, group_items in source_groups.items():
            for i in range(0, len(group_items), 4):
                batch = group_items[i:i + 4]
                if len(batch) == 1:
                    try:
                        signal = self.parser.parse_news_item(batch[0])
                        if signal:
                            self._store_parsed_signal(db, signal)
                            parsed_count += 1
                    except Exception as e:
                        logger.error(f"Parse queue error: {e}")
                else:
                    try:
                        signals = self.parser.parse_news_items_batch(batch)
                        for signal in signals:
                            if signal:
                                self._store_parsed_signal(db, signal)
                                parsed_count += 1
                    except Exception as e:
                        logger.error(f"Batch parse error, falling back to individual: {e}")
                        for item in batch:
                            try:
                                signal = self.parser.parse_news_item(item)
                                if signal:
                                    self._store_parsed_signal(db, signal)
                                    parsed_count += 1
                            except Exception as e2:
                                logger.error(f"Parse queue error: {e2}")

        if parsed_count > 0:
            logger.info(f"Signal AI: parsed {parsed_count} signals from {len(items)} messages")

    def run_tracking(self):
        """Run Stage 2 (tracking) if enough time has passed."""
        now = time.time()
        if now - self._last_track_time < self._track_interval:
            return
        self._last_track_time = now
        self.tracker.check_active_signals()

    def run_scoring(self):
        """Run Stage 3 (scoring) if enough time has passed."""
        now = time.time()
        if now - self._last_score_time < self._score_interval:
            return
        self._last_score_time = now
        self.scorer.score_resolved_signals()

    def run_all_stages(self, max_time_seconds: float = 10.0):
        """Run all 3 stages in sequence. Called by the orchestrator.
        
        Args:
            max_time_seconds: Maximum time to spend on Signal AI per tick.
                              This prevents blocking the orchestrator loop so
                              AI summaries can generate on time.
        """
        start_time = time.time()
        try:
            self.process_queue()
            if time.time() - start_time > max_time_seconds:
                logger.debug("Signal AI: max time reached after process_queue")
                return
            
            # Catch-up: parse any news_items that were missed (API failure, restart, etc.)
            self._catchup_unprocessed(max_items=10)  # Reduced from 100 to avoid blocking
            if time.time() - start_time > max_time_seconds:
                logger.debug("Signal AI: max time reached after catchup")
                return
            
            self.run_tracking()
            if time.time() - start_time > max_time_seconds:
                logger.debug("Signal AI: max time reached after tracking")
                return
            
            self.run_scoring()
        except Exception as e:
            logger.error(f"Signal AI run_all_stages error: {e}")

    def _catchup_unprocessed(self, max_items: int = 100):
        """Find and parse news_items that should have been parsed but weren't.
        This handles: items missed due to API key failure, restarts, hash mismatches, etc.
        Only runs every 2 minutes to avoid hammering the AI API.
        
        Args:
            max_items: Max items to process per catchup run (default 100, but can be
                       reduced to avoid blocking the orchestrator loop).
        """
        now = time.time()
        if not hasattr(self, '_last_catchup_time'):
            self._last_catchup_time = 0
        if now - self._last_catchup_time < 120:  # Every 2 minutes
            return
        self._last_catchup_time = now
        try:
            count = self.parse_existing_signals(limit=max_items)
            if count > 0:
                logger.info(f"Signal AI catch-up: parsed {count} missed signals")
        except Exception as e:
            logger.error(f"Catch-up scan FAILED: {type(e).__name__}: {e}")

    def parse_existing_signals(self, limit: int = 50):
        """
        Parse existing news_items that haven't been parsed yet.
        Useful for backfilling when Signal AI is first deployed.
        """
        from db.database import get_db
        db = get_db()

        try:
            # Find news_items not yet parsed. Exclude sources known to produce 0 signals.
            opt = _load_cost_optimization()
            skip = opt.get("signal_prefilter", {}).get("skip_sources", [])
            skip_clauses = ["ni.source NOT LIKE 'ai_alpha_%%'"]
            skip_params = []
            for s in skip:
                if not s.startswith("ai_alpha_"):
                    skip_clauses.append("ni.source != %s")
                    skip_params.append(s)

            where_skip = " AND ".join(skip_clauses)
            items = db.fetch_all(f"""
                SELECT ni.* FROM news_items ni
                LEFT JOIN parsed_signals ps ON ps.news_item_id = ni.id
                WHERE ps.id IS NULL
                AND {where_skip}
                AND ni.headline IS NOT NULL
                AND ni.headline != ''
                ORDER BY ni.published_at DESC
                LIMIT %s
            """, (*skip_params, limit))

            if not items:
                logger.info("Signal AI catch-up: all items have been processed (0 unparsed remaining)")
                return 0

            # Log source breakdown so we can see what's being processed
            from collections import Counter
            src_counts = Counter(i.get('source', 'unknown') for i in items)
            logger.info(f"Signal AI backfill: parsing {len(items)} items — {dict(src_counts)}")

            parsed = 0
            for item in items:
                try:
                    signal = self.parser.parse_news_item(item)
                    if signal:
                        self._store_parsed_signal(db, signal)
                        parsed += 1
                except Exception as e:
                    logger.warning(f"Backfill parse error for item {item.get('id')}: {type(e).__name__}: {e}")

            logger.info(f"Signal AI backfill complete: {parsed}/{len(items)} parsed")
            return parsed

        except Exception as e:
            logger.error(f"Backfill FAILED: {type(e).__name__}: {e}")
            return 0

    def _store_parsed_signal(self, db, signal: ParsedSignal):
        """Store a parsed signal in the database.

        Gate: Symbol must have detect_signals=1 in market_symbols.
        Missing SL/TP: enriched via policy or AI based on trade settings.
        """
        try:
            # ── GATE: Skip JarvAIs-internal signals (prevent feedback loops) ──
            _internal_authors = {"jarvais", "jarvis", "signal_ai", "trading_floor",
                                 "apex", "apex (ai trading floor)"}
            if signal.author and signal.author.lower().strip() in _internal_authors:
                logger.debug(f"[SignalAI] Skipping internal author signal: {signal.author}")
                return
            if signal.parsed_by and signal.parsed_by in ("trading_floor", "signal_ai"):
                logger.debug(f"[SignalAI] Skipping self-parsed signal: {signal.parsed_by}")
                return

            # ── Resolve symbol aliases (GOLD→XAUUSD, TAO→TAOUSDT, etc.) ──
            if signal.symbol:
                from db.market_symbols import resolve_symbol as _resolve_sym
                resolved = _resolve_sym(signal.symbol, db)
                if resolved != signal.symbol:
                    logger.info(f"[SignalAI] Symbol alias: {signal.symbol} -> {resolved}")
                    signal.symbol = resolved

            # ── Enrich missing SL/TP ──
            sl_source, tp_source = enrich_missing_levels(signal, db)

            # ── GATE: Symbol must be in user's configured markets ──
            # Exception: mentors with track_all_symbols=1 bypass this gate
            if signal.symbol:
                market_check = db.fetch_one(
                    "SELECT detect_signals FROM market_symbols WHERE symbol = %s",
                    (signal.symbol,)
                )
                if not market_check or not market_check.get('detect_signals'):
                    # Check if author is a mentor with track_all_symbols enabled
                    mentor_bypass = False
                    if signal.author:
                        mentor_row = db.fetch_one(
                            "SELECT up.track_all_symbols FROM user_profiles up "
                            "JOIN user_profile_links upl ON up.id = upl.user_profile_id "
                            "WHERE upl.source_username = %s AND up.is_mentor = 1 "
                            "AND up.track_all_symbols = 1 LIMIT 1",
                            (signal.author,)
                        )
                        if mentor_row:
                            mentor_bypass = True
                            logger.info(f"[SignalAI] Mentor bypass: {signal.author} signal for "
                                        f"{signal.symbol} allowed (track_all_symbols=ON)")
                            try:
                                from db.market_symbols import register_symbol
                                source_label = f"mentor/{signal.author}"
                                register_symbol(db, signal.symbol, source=source_label,
                                                detect_signals=True)
                            except Exception:
                                pass

                    if not mentor_bypass:
                        logger.debug(f"Signal dropped (market not configured): "
                                     f"{signal.symbol} from {signal.author}")
                        try:
                            from db.market_symbols import register_symbol
                            source_label = (f"{signal.source}/{signal.source_detail}"
                                            if signal.source_detail else signal.source)
                            register_symbol(db, signal.symbol, source=source_label)
                        except Exception:
                            pass
                        return

            # Dedup: skip if near-identical signal exists
            if signal.entry_price and float(signal.entry_price) > 0 and signal.author:
                tol = float(signal.entry_price) * 0.003
                existing_sig = db.fetch_one(
                    "SELECT id FROM parsed_signals "
                    "WHERE author = %s AND symbol = %s AND direction = %s "
                    "AND ABS(entry_price - %s) < %s "
                    "AND parsed_at > DATE_SUB(NOW(), INTERVAL 24 HOUR) LIMIT 1",
                    (signal.author, signal.symbol, signal.direction,
                     float(signal.entry_price), tol)
                )
                if existing_sig:
                    logger.debug(f"[SignalAI] Signal dedup: {signal.author} {signal.symbol} "
                                 f"{signal.direction} @ {signal.entry_price} "
                                 f"already exists as #{existing_sig['id']}")
                    return

            # Get tier, order_type, expires_at from signal (set by v3 parser)
            tier = getattr(signal, 'tier', 'full')
            order_type = getattr(signal, 'order_type', 'limit')
            expires_at = getattr(signal, 'expires_at', None)
            parent_id = getattr(signal, 'parent_signal_id', None)

            # Dedup: skip if near-identical signal exists (within 1% on entry/SL/TP)
            try:
                from services.trade_dedup import is_duplicate_signal
                dup_id = is_duplicate_signal(
                    db, signal.symbol, signal.direction,
                    float(signal.entry_price or 0),
                    float(signal.stop_loss or 0),
                    float(getattr(signal, 'take_profit_1', 0) or 0))
                if dup_id:
                    logger.info(f"[SignalAI] Skipped duplicate {signal.symbol} {signal.direction} "
                                f"(matches #{dup_id})")
                    return
            except Exception as e:
                logger.debug(f"[SignalAI] Dedup check: {e}")

            db.execute("""
                INSERT INTO parsed_signals
                (news_item_id, parent_signal_id, source, source_detail, author, author_badge,
                 symbol, direction, entry_price, entry_price_max,
                 stop_loss, sl_source, take_profit_1, take_profit_2, take_profit_3,
                 take_profit_4, take_profit_5, take_profit_6, tp_source,
                 confidence, signal_type, tier, order_type, timeframe, risk_reward,
                 raw_text, ai_reasoning, parsed_by, is_valid, status, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
            """, (
                signal.news_item_id,
                parent_id,
                signal.source,
                signal.source_detail,
                signal.author,
                signal.author_badge,
                signal.symbol,
                signal.direction or '',
                signal.entry_price,
                signal.entry_price_max,
                signal.stop_loss,
                sl_source,
                signal.take_profit_1,
                signal.take_profit_2,
                signal.take_profit_3,
                signal.take_profit_4,
                signal.take_profit_5,
                signal.take_profit_6,
                tp_source,
                signal.confidence,
                signal.signal_type,
                tier,
                order_type,
                signal.timeframe,
                signal.risk_reward,
                signal.raw_text[:2000] if signal.raw_text else "",
                signal.ai_reasoning,
                signal.parsed_by,
                1 if signal.is_valid else 0,
                expires_at,
            ))
            # Auto-register symbol in market_symbols table
            if signal.symbol:
                try:
                    from db.market_symbols import register_symbol
                    source_label = f"{signal.source}/{signal.source_detail}" if signal.source_detail else signal.source
                    register_symbol(db, signal.symbol, source=source_label)
                except Exception as sym_err:
                    logger.debug(f"Symbol registration note: {sym_err}")

            # Vectorize signal + record lineage to source news item
            try:
                from services.vectorization_worker import queue_for_vectorization, record_lineage
                sig_row = db.fetch_one("SELECT LAST_INSERT_ID() as lid")
                sig_id = sig_row["lid"] if sig_row else 0
                if sig_id:
                    rationale = signal.ai_reasoning or signal.raw_text or ""
                    vec_text = f"{signal.symbol} {signal.direction} @ {signal.entry_price} SL={signal.stop_loss}\n{rationale}"[:30000]
                    queue_for_vectorization(db, "parsed_signals", sig_id, "signals", vec_text, {
                        "symbol": signal.symbol,
                        "direction": signal.direction or "",
                        "author": signal.author or "",
                        "source": signal.source,
                        "confidence": signal.confidence,
                        "signal_type": signal.signal_type,
                    })
                    if signal.news_item_id:
                        record_lineage(db, "parsed_signals", sig_id,
                                       [("news_items", signal.news_item_id)], "signal_derived_from")
            except Exception as vec_err:
                logger.debug(f"Signal vectorization error: {vec_err}")

        except Exception as e:
            logger.error(f"Failed to store parsed signal: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get Signal AI status for dashboard."""
        from db.database import get_db
        db = get_db()

        try:
            counts = db.fetch_one("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active,
                    SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN outcome = 'expired' THEN 1 ELSE 0 END) as expired,
                    SUM(CASE WHEN outcome IS NOT NULL THEN outcome_pips ELSE 0 END) as total_pips
                FROM parsed_signals
                WHERE signal_type = 'entry'
            """)
        except Exception:
            counts = {}

        return {
            "parser": self.parser.get_stats(),
            "tracker": self.tracker.get_stats(),
            "scorer": self.scorer.get_stats(),
            "signals": {
                "total": counts.get("total", 0) if counts else 0,
                "pending": counts.get("pending", 0) if counts else 0,
                "active": counts.get("active", 0) if counts else 0,
                "wins": counts.get("wins", 0) if counts else 0,
                "losses": counts.get("losses", 0) if counts else 0,
                "expired": counts.get("expired", 0) if counts else 0,
                "total_pips": float(counts.get("total_pips", 0) or 0) if counts else 0,
            },
        }


def get_signal_ai() -> SignalAI:
    """Get or create the singleton SignalAI instance."""
    global _signal_ai_instance
    if _signal_ai_instance is None:
        with _signal_ai_lock:
            if _signal_ai_instance is None:
                _signal_ai_instance = SignalAI()
    return _signal_ai_instance


# ─────────────────────────────────────────────────────────────────────
# Per-Symbol JTS Scoring (for Dossier Builder)
# ─────────────────────────────────────────────────────────────────────

def get_top_providers_for_symbol(symbol: str, limit: int = 10,
                                  lookback_days: int = 7) -> List[Dict]:
    """
    Calculate JTS (JarvAIs Trust Score) per symbol and return top providers.

    This queries parsed_signals for a specific symbol, computes win/loss stats
    per author, and returns the top N providers ranked by per-symbol trust score.

    Args:
        symbol: The trading symbol (e.g., "XAUUSD")
        limit: Max number of top providers to return
        lookback_days: How far back to look for signals

    Returns:
        List of dicts with author, symbol_jts, win_rate, total, wins, losses,
        avg_pips, active_ideas (unresolved signals)
    """
    from db.database import get_db
    db = get_db()

    rows = db.fetch_all("""
        SELECT
            ps.author,
            ps.source,
            ps.source_detail,
            COUNT(*) as total_signals,
            SUM(CASE WHEN ps.outcome = 'win' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN ps.outcome = 'loss' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN ps.outcome = 'expired' THEN 1 ELSE 0 END) as expired_count,
            SUM(CASE WHEN ps.status IN ('tp2_hit','tp3_hit','tp4_hit','tp5_hit','tp6_hit') THEN 1 ELSE 0 END) as multi_tp_hits,
            AVG(COALESCE(ps.outcome_pips, 0)) as avg_pips,
            MAX(ps.parsed_at) as last_signal_at,
            sps.trust_score as global_jts
        FROM parsed_signals ps
        LEFT JOIN signal_provider_scores sps
            ON sps.source = ps.source AND sps.source_detail = ps.source_detail AND sps.author = ps.author
        WHERE ps.symbol = %s
            AND ps.author IS NOT NULL AND ps.author != ''
            AND ps.parsed_by NOT IN ('trading_floor', 'signal_ai')
            AND ps.outcome IN ('win','loss','expired','breakeven')
            AND ps.parsed_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        GROUP BY ps.author, ps.source, ps.source_detail, sps.trust_score
        HAVING total_signals >= 1
        ORDER BY wins DESC, total_signals DESC
        LIMIT %s
    """, (symbol, lookback_days, limit * 2))

    if not rows:
        return []

    results = []
    for r in rows:
        total = int(r["total_signals"] or 0)
        wins = int(r["wins"] or 0)
        losses = int(r["losses"] or 0)
        expired_count = int(r["expired_count"] or 0)
        multi_tp = int(r["multi_tp_hits"] or 0)

        win_rate = round(wins / total * 100, 1) if total > 0 else 0
        symbol_jts = 50.0
        symbol_jts += wins * 2.0
        symbol_jts -= losses * 3.0
        symbol_jts -= expired_count * 0.5
        symbol_jts += multi_tp * 1.5
        symbol_jts = max(0, min(100, symbol_jts))

        active_ideas = db.fetch_all("""
            SELECT id, direction, entry_price, stop_loss, take_profit_1, take_profit_2,
                   confidence, status, parsed_at, raw_text
            FROM parsed_signals
            WHERE symbol = %s AND author = %s
                AND status IN ('pending','active','entry_hit','tp1_hit')
                AND parsed_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            ORDER BY parsed_at DESC LIMIT 5
        """, (symbol, r["author"], lookback_days))

        results.append({
            "author": r["author"],
            "source": r["source"],
            "source_detail": r.get("source_detail", ""),
            "symbol_jts": round(symbol_jts, 1),
            "global_jts": float(r["global_jts"] or 50) if r.get("global_jts") else None,
            "win_rate": win_rate,
            "total": total,
            "wins": wins,
            "losses": losses,
            "avg_pips": round(float(r["avg_pips"] or 0), 1),
            "last_signal_at": r["last_signal_at"].isoformat() if r.get("last_signal_at") else None,
            "active_ideas": active_ideas or [],
        })

    results.sort(key=lambda x: x["symbol_jts"], reverse=True)
    return results[:limit]


def get_da_analyses_for_symbol(symbol: str, lookback_days: int = 7,
                                limit: int = 20) -> List[Dict]:
    """
    Retrieve DA team analyses (Lens, Scribe) for a symbol.
    Includes chart images and AI analysis from news_items.

    Returns list of analysis items with chart_image_url, ai_analysis text,
    author info, and source metadata.
    """
    from db.database import get_db
    db = get_db()

    rows = db.fetch_all("""
        SELECT id, source, source_detail, author, headline, detail,
               chart_image_url, ai_analysis, direction, sentiment,
               media_url, media_type, collected_at
        FROM news_items
        WHERE (
            JSON_CONTAINS(symbols, %s)
            OR headline LIKE %s
            OR detail LIKE %s
        )
        AND collected_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
        AND (ai_analysis IS NOT NULL AND ai_analysis != '')
        ORDER BY collected_at DESC
        LIMIT %s
    """, (
        f'"{symbol}"',
        f'%{symbol}%',
        f'%{symbol}%',
        lookback_days,
        limit
    ))

    results = []
    for r in (rows or []):
        image_path = None
        if r.get("chart_image_url"):
            image_path = r["chart_image_url"]
        elif r.get("media_url") and r.get("media_type", "").startswith("image"):
            image_path = r["media_url"]

        results.append({
            "news_id": r["id"],
            "source": r["source"],
            "source_detail": r.get("source_detail", ""),
            "author": r.get("author", ""),
            "headline": r.get("headline", ""),
            "ai_analysis": r.get("ai_analysis", ""),
            "direction": r.get("direction", ""),
            "sentiment": r.get("sentiment", ""),
            "chart_image_path": image_path,
            "collected_at": r["collected_at"].isoformat() if r.get("collected_at") else None,
        })

    return results
