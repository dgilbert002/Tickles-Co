"""
JarvAIs Trade Dossier Builder
Builds comprehensive dossiers per symbol for the two-stage LLM trade decision pipeline.

Architecture:
  Stage 1 (Cheap model): Unbiased technical analysis on raw OHLCV data
  Stage 2 (Premium model): Full dossier + images -> trade hypothesis with conditions

Dossier sections:
  1. OHLCV multi-timeframe candle data
  2. Stage 1 TA output (Fib, S/R, VWAP, BOS/CHoCH, liquidity zones)
  3. DA team analyses (Lens/Scribe chart analysis with images)
  4. Signal provider intelligence (top JTS-scored providers per symbol)
  5. Geopolitical snapshot (from Geo)
  6. Macroeconomic snapshot (from Macro)
  7. Historical AI trade performance on this symbol
"""

import io
import os
import json
import base64
import logging
import traceback
import hashlib
import threading
from collections import OrderedDict
from concurrent.futures import as_completed
from core.thread_pool import DaemonThreadPoolExecutor as ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse

from PIL import Image as _PILImage

from core.config_loader import (
    get_system_config, get_system_config_float, get_system_config_int,
    get_agent_soul, load_prompt, build_agent_system_prompt,
)

logger = logging.getLogger("jarvais.trade_dossier")


def _utcnow() -> datetime:
    """Naive-UTC now — no DeprecationWarning, compatible with MySQL datetimes."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ═══════════════════════════════════════════════════════════════════════
# PER-SYMBOL JTS SCORING
# ═══════════════════════════════════════════════════════════════════════

def get_per_symbol_jts(db, symbol: str, limit: int = 10) -> List[Dict]:
    """
    Calculate JTS (JarvAIs Trust Score) per provider for a specific symbol.
    Returns top N providers ranked by symbol-specific JTS.
    """
    rows = db.fetch_all("""
        SELECT
            author,
            source,
            COUNT(*) as total_signals,
            SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
            SUM(CASE WHEN outcome IN ('win','loss','breakeven') THEN 1 ELSE 0 END) as resolved,
            AVG(CASE WHEN outcome_pips IS NOT NULL THEN outcome_pips ELSE NULL END) as avg_pips,
            AVG(CASE WHEN outcome_rr IS NOT NULL THEN outcome_rr ELSE NULL END) as avg_rr,
            SUM(CASE WHEN stop_loss IS NOT NULL THEN 1 ELSE 0 END) as has_sl,
            SUM(outcome_pips) as total_pips,
            MAX(parsed_at) as last_signal_at
        FROM parsed_signals
        WHERE symbol = %s AND author IS NOT NULL AND author != ''
              AND (parsed_by IS NULL OR parsed_by NOT IN ('trading_floor', 'signal_ai'))
        GROUP BY author, source
        HAVING total_signals >= 1
    """, (symbol,))

    if not rows:
        return []

    scored = []
    best_pips = max((abs(float(r["total_pips"] or 0)) for r in rows), default=1)
    for r in rows:
        resolved = int(r["resolved"] or 0)
        total = int(r["total_signals"] or 0)
        wins = int(r["wins"] or 0)

        win_rate = (wins / resolved) if resolved > 0 else 0
        sl_ratio = (int(r["has_sl"] or 0) / total) if total > 0 else 0
        sample_bonus = min(1.0, resolved / 10)
        avg_rr = float(r["avg_rr"] or 0)
        rr_factor = min(1.0, avg_rr / 3.0) if avg_rr > 0 else 0
        pips_factor = min(1.0, abs(float(r["total_pips"] or 0)) / best_pips) if best_pips else 0

        jts = (win_rate * 40) + (sl_ratio * 15) + (sample_bonus * 15) + \
              (rr_factor * 15) + (pips_factor * 15)

        scored.append({
            "author": r["author"],
            "source": r["source"],
            "jts": round(jts, 1),
            "total_signals": total,
            "resolved": resolved,
            "wins": wins,
            "losses": int(r["losses"] or 0),
            "win_rate": round(win_rate * 100, 1),
            "avg_pips": round(float(r["avg_pips"] or 0), 1),
            "avg_rr": round(avg_rr, 2),
            "total_pips": round(float(r["total_pips"] or 0), 1),
            "last_signal_at": r["last_signal_at"].isoformat() if r["last_signal_at"] else None,
        })

    scored.sort(key=lambda x: (-x["jts"], -x["total_pips"]))
    return scored[:limit]


def get_active_ideas_for_symbol(db, symbol: str, days: int = 7) -> List[Dict]:
    """Get active/pending signals for a symbol from top providers.
    Excludes JarvAIs own signals by both parsed_by and author fields."""
    cutoff = _utcnow() - timedelta(days=days)
    rows = db.fetch_all("""
        SELECT ps.id, ps.author, ps.source, ps.direction, ps.entry_price, ps.stop_loss,
               ps.take_profit_1, ps.take_profit_2, ps.take_profit_3, ps.confidence,
               ps.signal_type, ps.timeframe, ps.risk_reward, ps.ai_reasoning,
               ps.status, ps.parsed_at, ps.raw_text, ps.source_media, ps.parsed_by,
               ps.news_item_id, ni.ai_analysis
        FROM parsed_signals ps
        LEFT JOIN news_items ni ON ps.news_item_id = ni.id
        WHERE ps.symbol = %s AND ps.status IN ('pending','active','entry_hit')
              AND ps.parsed_at >= %s
              AND (ps.parsed_by IS NULL OR ps.parsed_by NOT IN ('trading_floor', 'signal_ai'))
              AND (ps.author IS NULL OR ps.author NOT IN ('JarvAIs', 'jarvais', 'Jarvis', 'jarvis'))
        ORDER BY ps.parsed_at DESC
    """, (symbol, cutoff))
    return rows or []


# ═══════════════════════════════════════════════════════════════════════
# DA TEAM ANALYSIS RETRIEVAL (charts + images)
# ═══════════════════════════════════════════════════════════════════════

def get_da_analyses_for_symbol(db, symbol: str, days: int = 7) -> List[Dict]:
    """
    Retrieve DA team analyses for a symbol, including chart images.
    Pulls from news_items where ai_analysis was generated by Lens/Scribe.
    Excludes JarvAIs self-generated entries to prevent feedback loops.
    """
    cutoff = _utcnow() - timedelta(days=days)
    rows = db.fetch_all("""
        SELECT id, source, author, headline, detail, ai_analysis,
               chart_image_url, media_url, media_type, direction,
               collected_at, source_detail, tv_timeframe
        FROM news_items
        WHERE (symbols LIKE %s OR headline LIKE %s OR detail LIKE %s)
              AND ai_analysis IS NOT NULL AND ai_analysis != ''
              AND collected_at >= %s
              AND (author IS NULL OR author NOT IN ('JarvAIs', 'jarvais', 'Jarvis', 'jarvis'))
        ORDER BY collected_at DESC
        LIMIT 30
    """, (f'%{symbol}%', f'%{symbol}%', f'%{symbol}%', cutoff))
    return rows or []


_MAX_IMAGE_DIM = 1024

def load_chart_image_as_base64(image_path: str) -> Optional[str]:
    """Load a local chart image file, resize if needed, and return base64-encoded data."""
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        with open(image_path, "rb") as f:
            data = f.read()
        if len(data) < 500:
            logger.debug(f"[Dossier] Image too small ({len(data)}b), skipping: {image_path}")
            return None

        ext = os.path.splitext(image_path)[1].lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/png")
        fmt = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG",
               ".gif": "GIF", ".webp": "WEBP"}.get(ext, "PNG")

        img = _PILImage.open(io.BytesIO(data))
        orig_w, orig_h = img.size
        if orig_w > _MAX_IMAGE_DIM or orig_h > _MAX_IMAGE_DIM:
            scale = _MAX_IMAGE_DIM / max(orig_w, orig_h)
            new_w = int(orig_w * scale)
            new_h = int(orig_h * scale)
            img = img.resize((new_w, new_h), _PILImage.LANCZOS)
            logger.debug(f"[Dossier] Resized {image_path} from {orig_w}x{orig_h} to {new_w}x{new_h}")
            buf = io.BytesIO()
            img.save(buf, format=fmt)
            data = buf.getvalue()

        return f"data:{mime};base64,{base64.b64encode(data).decode()}"
    except Exception as e:
        logger.debug(f"[Dossier] Could not load image {image_path}: {e}")
        return None


def _download_image(url: str, dest_path: str, timeout: int = 15) -> bool:
    """Download a remote image to local disk. Returns True on success."""
    import requests
    try:
        resp = requests.get(url, timeout=timeout, stream=True,
                            headers={"User-Agent": "JarvAIs/1.0"})
        if resp.status_code != 200:
            logger.debug(f"[Dossier] Image download HTTP {resp.status_code}: {url[:80]}")
            return False
        content_type = resp.headers.get("content-type", "")
        if "image" not in content_type and "octet" not in content_type:
            logger.debug(f"[Dossier] Not an image ({content_type}): {url[:80]}")
            return False
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        size = 0
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
                size += len(chunk)
                if size > 10_000_000:
                    logger.warning(f"[Dossier] Image too large (>10MB), aborting: {url[:80]}")
                    f.close()
                    os.remove(dest_path)
                    return False
        if size < 500:
            os.remove(dest_path)
            return False
        logger.info(f"[Dossier] Downloaded chart image ({size:,}b): {os.path.basename(dest_path)}")
        return True
    except Exception as e:
        logger.debug(f"[Dossier] Image download failed: {e}")
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False


def resolve_chart_image_path(url_or_path: str, download_dir: str = "data/alpha_downloads") -> Optional[str]:
    """Resolve chart image URL/path to a local file, downloading if needed."""
    if not url_or_path:
        return None

    if os.path.exists(url_or_path):
        return url_or_path

    parsed = urlparse(url_or_path)
    is_remote = parsed.scheme in ("http", "https")

    url_basename = os.path.basename(parsed.path) if is_remote else os.path.basename(url_or_path)
    if url_basename:
        local = os.path.join(download_dir, url_basename)
        if os.path.exists(local):
            return local

    if is_remote:
        safe_name = hashlib.md5(url_or_path.encode()).hexdigest()[:12]
        ext = os.path.splitext(url_basename)[1] if url_basename else ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            ext = ".png"
        dest = os.path.join(download_dir, f"chart_{safe_name}{ext}")
        if os.path.exists(dest):
            return dest
        if _download_image(url_or_path, dest):
            return dest
        return None

    return None


# ═══════════════════════════════════════════════════════════════════════
# HISTORICAL PERFORMANCE
# ═══════════════════════════════════════════════════════════════════════

def get_historical_performance(db, symbol: str, limit: int = 20) -> Dict[str, Any]:
    """Get AI's historical trade performance on this symbol from trade_dossiers."""
    dossiers = db.fetch_all("""
        SELECT id, direction, entry_price, stop_loss,
               take_profit_1, take_profit_2, take_profit_3,
               confidence_score, status, realised_pnl, realised_pnl_pct,
               margin_usd, leverage, mentor_source,
               created_at, updated_at
        FROM trade_dossiers
        WHERE symbol = %s AND status IN ('won','lost','expired','abandoned')
        ORDER BY updated_at DESC
        LIMIT %s
    """, (symbol, limit))

    if not dossiers:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "avg_pnl_usd": 0, "total_pnl_usd": 0, "trades": []}

    wins = sum(1 for d in dossiers if d.get("status") == "won")
    losses = sum(1 for d in dossiers if d.get("status") in ("lost", "abandoned"))
    pnl_list = [float(d.get("realised_pnl") or 0) for d in dossiers]

    return {
        "total": len(dossiers),
        "wins": wins,
        "losses": losses,
        "win_rate": round((wins / len(dossiers)) * 100, 1) if dossiers else 0,
        "avg_pnl_usd": round(sum(pnl_list) / len(pnl_list), 2) if pnl_list else 0,
        "total_pnl_usd": round(sum(pnl_list), 2),
        "trades": [
            {"dossier_id": d["id"],
             "direction": d["direction"],
             "entry": float(d["entry_price"]) if d.get("entry_price") else None,
             "stop_loss": float(d["stop_loss"]) if d.get("stop_loss") else None,
             "tp1": float(d["take_profit_1"]) if d.get("take_profit_1") else None,
             "pnl_usd": float(d.get("realised_pnl") or 0),
             "pnl_pct": float(d.get("realised_pnl_pct") or 0),
             "confidence": d.get("confidence_score"),
             "status": d["status"],
             "leverage": d.get("leverage"),
             "mentor": d.get("mentor_source"),
             "closed_at": d["updated_at"].isoformat() if d.get("updated_at") else None}
            for d in dossiers
        ]
    }


def format_ohlcv_for_prompt(ohlcv_data: Dict[str, Any]) -> str:
    """Format multi-timeframe OHLCV data as text for the Stage 1 prompt."""
    parts = []
    for tf in ["D1", "H4", "H1", "M15", "M5"]:
        tf_data = ohlcv_data.get(tf)
        if not tf_data or not tf_data.get("candles"):
            continue

        candles = tf_data["candles"]
        parts.append(f"### {tf} Timeframe ({tf_data['count']} candles, "
                     f"{tf_data.get('first_time', '?')} to {tf_data.get('last_time', '?')})")
        parts.append(f"Range: {tf_data.get('low_of_range', '?')} - {tf_data.get('high_of_range', '?')}")
        parts.append(f"Latest close: {tf_data.get('latest_close', '?')}")

        recent = candles[-200:]
        parts.append("time,open,high,low,close,volume")
        for c in recent:
            t = c["time"].strftime("%Y-%m-%d %H:%M") if hasattr(c["time"], "strftime") else str(c["time"])
            parts.append(f"{t},{c['open']},{c['high']},{c['low']},{c['close']},{c.get('volume', 0)}")
        parts.append("")

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════
# DOSSIER PROMPTS — All prompts live in DB (system_config table).
# Keys: dossier_stage1_prompt, dossier_stage2_prompt,
#        tracker_system_prompt, dossier_postmortem_prompt
# Edit them via Settings > Prompts in the dashboard.
# ═══════════════════════════════════════════════════════════════════════

_PROMPT_NOT_FOUND = "ERROR: Prompt not found in database (system_config table). " \
                    "Please seed it via Settings > Prompts or db/bootstrap.py."

# Legacy names kept as thin stubs so old imports don't crash
STAGE1_TA_PROMPT = _PROMPT_NOT_FOUND
STAGE2_DECISION_PROMPT = _PROMPT_NOT_FOUND
TRACKER_UPDATE_PROMPT = _PROMPT_NOT_FOUND
POSTMORTEM_PROMPT = _PROMPT_NOT_FOUND

# ═══════════════════════════════════════════════════════════════════════
# DOSSIER BUILDER
# ═══════════════════════════════════════════════════════════════════════

class TradeDossierBuilder:
    """
    Orchestrates the complete dossier-building and two-stage decision pipeline.
    """

    _stage1_cache: OrderedDict = OrderedDict()
    _stage2_dnt_cache: OrderedDict = OrderedDict()
    _cache_lock = threading.Lock()
    _S1_CACHE_MAX = 200
    _S2_CACHE_MAX = 400

    def __init__(self, db, config, candle_collector=None,
                 duo_id: Optional[str] = None):
        self.db = db
        self.config = config
        self.candle_collector = candle_collector
        self.duo_id = duo_id

        if duo_id:
            from core.duo_config import get_duo_config
            self._td_cfg = get_duo_config(config, duo_id, db=db)
        else:
            self._td_cfg = config.raw.get("trade_decision", {}) if config else {}

    @staticmethod
    def _extract_latest_price(ohlcv: Dict) -> float:
        """Pull the most recent close price from gathered OHLCV sections.

        Prefers shorter timeframes (M5 > M15 > H1 > H4 > D1) since they
        are fresher.  Returns 0.0 if nothing usable is found.
        """
        for tf in ("M5", "M15", "H1", "H4", "D1"):
            tf_data = ohlcv.get(tf)
            if isinstance(tf_data, dict):
                lc = tf_data.get("latest_close")
                if lc:
                    try:
                        return float(lc)
                    except (ValueError, TypeError):
                        continue
        logger.warning("[Dossier] _extract_latest_price: no valid close price "
                       f"found across any timeframe — Stage 1 cache DISABLED "
                       f"for this build (keys: {list(ohlcv.keys()) if ohlcv else 'empty'})")
        return 0.0

    # ── Prompt Loading (DB → code fallback) ─────────────────────────

    def _load_prompt_from_db(self, stage: str, code_default: str = "",
                            dossier_id: int = None) -> str:
        """Load a dossier prompt from system_config DB table.
        Supports A/B testing: if a challenger prompt_version exists (is_active=0,
        with a 'testing' change_reason), 20% of dossiers use the challenger.
        Returns the DB value or code_default (which should be empty/stub)."""
        db_key = f"dossier_{stage}_prompt"
        role_map = {"stage1": "analyst", "stage2": "trader"}
        role = role_map.get(stage)

        # A/B testing: check for challenger versions
        if role and dossier_id:
            try:
                import random
                challenger = self.db.fetch_one("""
                    SELECT id, system_prompt FROM prompt_versions
                    WHERE role = %s AND is_active = 0
                      AND change_reason LIKE '%%testing%%'
                    ORDER BY created_at DESC LIMIT 1
                """, (role,))
                ab_rate = get_system_config_float(self.db, "ab_test_sample_rate", 0.20)
                if challenger and challenger.get("system_prompt") and random.random() < ab_rate:
                    self.db.execute(
                        "UPDATE trade_dossiers SET prompt_version_id = %s WHERE id = %s",
                        (challenger["id"], dossier_id))
                    logger.info(f"[Dossier] A/B TEST: using challenger prompt v#{challenger['id']} "
                                f"for dossier #{dossier_id} ({stage})")
                    return challenger["system_prompt"]
            except Exception as e:
                logger.debug(f"[Dossier] A/B test check error: {e}")

        db_val = load_prompt(self.db, db_key, code_default, min_length=100,
                             duo_id=self.duo_id)
        if db_val != code_default:
            # Record champion version for A/B tracking
            if role and dossier_id:
                try:
                    champ = self.db.fetch_one(
                        "SELECT id FROM prompt_versions WHERE role = %s AND is_active = 1 LIMIT 1",
                        (role,))
                    if champ:
                        self.db.execute(
                            "UPDATE trade_dossiers SET prompt_version_id = %s WHERE id = %s",
                            (champ["id"], dossier_id))
                except Exception:
                    pass
        return db_val

    # ── Existing Dossier Context ─────────────────────────────────────

    def _build_existing_dossiers_context(self, symbol: str) -> str:
        """Build context about active dossiers for this symbol within this duo
        so the trader can avoid duplicates and consider staggered entries."""
        try:
            active = self.db.fetch_all("""
                SELECT id, direction, entry_price, stop_loss,
                       take_profit_1, take_profit_2, take_profit_3,
                       confidence, status, trade_decision, created_at
                FROM trade_dossiers
                WHERE symbol = %s AND duo_id = %s
                  AND status IN ('proposed','monitoring','open_order','live')
                ORDER BY created_at DESC LIMIT 10
            """, (symbol, self.duo_id or "apex"))
            if not active:
                return ("No other active dossiers exist for this symbol. "
                        "You are free to propose any trade setup.")
            lines = [
                f"You currently have {len(active)} active dossier(s) for {symbol}. "
                f"Review them before proposing a new trade:\n"
            ]
            for d in active:
                lines.append(
                    f"- **Dossier #{d['id']}** ({d.get('status','?')}): "
                    f"{d.get('direction','?')} @ entry {d.get('entry_price','?')}, "
                    f"SL {d.get('stop_loss','?')}, "
                    f"TP1 {d.get('take_profit_1','?')}/{d.get('take_profit_2','?')}/"
                    f"{d.get('take_profit_3','?')}, "
                    f"confidence {d.get('confidence','?')}%, "
                    f"decision: {d.get('trade_decision','?')}"
                )
            lines.append(
                f"\n**IMPORTANT:** If your proposed entry/SL/TP is very similar to an "
                f"existing dossier, explain WHY a new one is needed — is it a better entry? "
                f"Updated analysis? Different timeframe play? If you can't justify the "
                f"difference, consider whether WAIT FOR CONDITIONS on the existing dossier "
                f"is more appropriate.\n"
                f"If you see a range and want staggered entries across it, that IS a valid "
                f"reason for multiple dossiers — but label your entry zone clearly "
                f"(primary/secondary/aggressive) so Tracker knows the strategy."
            )
            return "\n".join(lines)
        except Exception as e:
            logger.debug(f"[Dossier] Existing dossier context failed: {e}")
            return "Could not retrieve existing dossier data."

    # ── Model Tier Resolution ────────────────────────────────────────

    def _resolve_stage_model(self, stage: str) -> Dict[str, Any]:
        """Resolve model config for a stage from the 3-tier config structure.
        Falls back to legacy flat keys (stage1_model/stage1_provider) for backward compat.
        Returns dict with keys: model, provider, max_tokens, temperature, supports_vision, tier.
        """
        models_cfg = self._td_cfg.get(f"{stage}_models", {})
        if models_cfg:
            active_tier = models_cfg.get("active_tier", "primary")
            tier_cfg = models_cfg.get(active_tier, {})
            if tier_cfg and tier_cfg.get("model"):
                return {
                    "model": tier_cfg["model"],
                    "provider": tier_cfg.get("provider", "openrouter"),
                    "max_tokens": tier_cfg.get("max_tokens", 8192),
                    "temperature": tier_cfg.get("temperature", 0.3),
                    "supports_vision": tier_cfg.get("supports_vision", True),
                    "context_window": tier_cfg.get("context_window", 200000),
                    "tier": active_tier,
                }
            logger.warning(f"[Dossier] No config for {stage} tier '{active_tier}', trying primary")
            primary = models_cfg.get("primary", {})
            if primary and primary.get("model"):
                return {
                    "model": primary["model"],
                    "provider": primary.get("provider", "openrouter"),
                    "max_tokens": primary.get("max_tokens", 8192),
                    "temperature": primary.get("temperature", 0.3),
                    "supports_vision": primary.get("supports_vision", True),
                    "context_window": primary.get("context_window", 200000),
                    "tier": "primary",
                }

        defaults = {
            "stage1": ("google/gemini-2.5-pro", "openrouter", 8192, 0.3),
            "stage2": ("anthropic/claude-opus-4", "openrouter", 4096, 0.2),
        }
        d = defaults.get(stage, ("google/gemini-2.5-pro", "openrouter", 8192, 0.3))
        model = self._td_cfg.get(f"{stage}_model", d[0])
        provider = self._td_cfg.get(f"{stage}_provider", d[1])
        return {
            "model": model, "provider": provider,
            "max_tokens": d[2], "temperature": d[3],
            "supports_vision": True, "context_window": 200000, "tier": "legacy",
        }

    # ── Main Entry Point ─────────────────────────────────────────────

    def build_dossier(self, symbol: str,
                      include_stage1: bool = True,
                      include_stage2: bool = True,
                      on_stage=None,
                      mentor_triggered: bool = False,
                      mentor_signal: Optional[Dict] = None,
                      market_regime: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Build a complete dossier for a symbol and optionally run both stages.
        on_stage: optional callback(stage_name, detail) for real-time pipeline tracking.
        mentor_triggered: if True, the R:R hard reject becomes a warning instead of override.
        mentor_signal: when provided (mentor assessment), the signal's entry/SL/TP
            are used for the R:R gate instead of Stage 2's parsed output.

        Returns:
            Dict with all sections, stage outputs, and dossier_id if saved.
        """
        _notify = on_stage or (lambda s, d="": None)

        # ── Normalize symbol to canonical USDT form BEFORE anything else ──
        raw_symbol = symbol
        norm = None
        try:
            from db.market_symbols import normalize_for_dossier
            norm = normalize_for_dossier(symbol, self.db)
            symbol = norm["normalized"]
            if symbol != raw_symbol:
                logger.info(f"[Dossier] Normalized '{raw_symbol}' -> '{symbol}' "
                            f"(method={norm['method']}, verified={norm['exchange_verified']})")
        except Exception as e:
            logger.debug(f"[Dossier] Normalization skipped for {symbol}: {e}")

        _duo_tag = f"[{self.duo_id}] " if self.duo_id else ""
        logger.info(f"[Dossier] {_duo_tag}Building dossier for {symbol}")

        # Guard: block dossiers for untradeable / unresolvable symbols
        try:
            sym_row = self.db.fetch_one(
                "SELECT tradable, asset_class FROM market_symbols WHERE symbol = %s",
                (symbol,))
            if not sym_row and raw_symbol != symbol:
                sym_row = self.db.fetch_one(
                    "SELECT tradable, asset_class FROM market_symbols WHERE symbol = %s",
                    (raw_symbol,))
            if sym_row and not sym_row.get("tradable"):
                logger.warning(f"[Dossier] BLOCKED: {symbol} is not tradable "
                               f"(asset_class={sym_row.get('asset_class')})")
                blocked = {"symbol": symbol, "dossier_id": -1,
                           "blocked_reason": f"{symbol} is not tradable",
                           "stage2_output": {"trade_decision": "do_not_trade",
                                             "confidence_score": 0}}
                try:
                    self._update_symbol_intel(symbol, blocked)
                except Exception:
                    pass
                return blocked
            if not sym_row:
                logger.warning(f"[Dossier] BLOCKED: {symbol} not found in market_symbols")
                blocked = {"symbol": symbol, "dossier_id": -1,
                           "blocked_reason": f"{symbol} not in market_symbols",
                           "stage2_output": {"trade_decision": "do_not_trade",
                                             "confidence_score": 0}}
                try:
                    self._update_symbol_intel(symbol, blocked)
                except Exception:
                    pass
                return blocked
        except Exception as e:
            logger.debug(f"[Dossier] Tradability check skipped for {symbol}: {e}")

        try:
            bl_raw = self._get_tuning_param("symbol_blacklist", "[]")
            blacklist = [s.upper() for s in json.loads(bl_raw)]
            if symbol.upper() in blacklist:
                logger.warning(f"[Dossier] BLOCKED: {symbol} is on symbol_blacklist")
                blocked = {"symbol": symbol, "dossier_id": -1,
                           "blocked_reason": f"{symbol} is blacklisted (negative EV)",
                           "stage2_output": {"trade_decision": "do_not_trade",
                                             "confidence_score": 0}}
                try:
                    self._update_symbol_intel(symbol, blocked)
                except Exception:
                    pass
                return blocked
        except Exception:
            pass

        # Dedup check: warn if similar dossiers already active for this symbol+duo
        try:
            existing = self.db.fetch_all("""
                SELECT id, direction, entry_price FROM trade_dossiers
                WHERE symbol = %s AND duo_id = %s
                  AND status IN ('proposed', 'monitoring', 'open_order')
            """, (symbol, self.duo_id))
            if existing:
                logger.info(f"[Dossier] {len(existing)} active dossier(s) already exist for {symbol}")
        except Exception:
            existing = []

        ds_cfg = self._td_cfg.get("data_sources", {})

        dossier = {
            "symbol": symbol,
            "raw_symbol": raw_symbol if raw_symbol != symbol else None,
            "created_at": _utcnow().isoformat(),
            "sections": {},
            "stage1_output": None,
            "stage2_output": None,
            "chart_images": [],
            "duplicate_warning": len(existing) > 0,
            "existing_dossier_count": len(existing),
        }

        # Store market regime if provided
        if market_regime:
            dossier["sections"]["market_regime"] = market_regime

        # Record pipeline start
        _gather_start = _utcnow()
        dossier["_timing"] = {"gathering_started": _gather_start.isoformat()}

        def _sec(label):
            """Log elapsed time for a gathering section."""
            elapsed = (_utcnow() - _gather_start).total_seconds()
            logger.info(f"[Dossier] {symbol} gather +{elapsed:.1f}s after {label}")

        # ── Phase 3: Parallel dossier gathering ────────────────────────
        # Independent sections run concurrently; dependent ones run after.
        _GATHER_TIMEOUT = 120

        def _g_ohlcv():
            return ("ohlcv", self._gather_ohlcv(symbol)
                    if ds_cfg.get("ohlcv_candles", True) else {})

        def _g_da():
            if ds_cfg.get("da_charts", True):
                return ("da_analyses", self._gather_da_analyses(symbol))
            return ("da_analyses", {"analyses": [], "seen_summary": "",
                                    "seen_count": 0, "images": []})

        def _g_signal():
            return ("signal_intelligence",
                    self._gather_signal_intel(symbol)
                    if ds_cfg.get("signal_intelligence", True) else {})

        def _g_mentor():
            if ds_cfg.get("mentor_intelligence", True):
                return ("mentor_intelligence",
                        self._gather_mentor_intelligence(symbol))
            return ("mentor_intelligence", {})

        def _g_geo():
            if ds_cfg.get("geo_macro", True):
                return ("geo_macro", {
                    "geopolitical": self._gather_geopolitical(symbol),
                    "macroeconomic": self._gather_macroeconomic(symbol),
                })
            return ("geo_macro", {"geopolitical": {}, "macroeconomic": {}})

        def _g_hist():
            if ds_cfg.get("historical_performance", True):
                return ("historical_performance",
                        get_historical_performance(self.db, symbol))
            return ("historical_performance", {})

        def _g_lessons():
            if ds_cfg.get("symbol_lessons", True):
                win_window = self._td_cfg.get("lesson_window_wins", 25)
                loss_window = self._td_cfg.get("lesson_window_losses", 25)
                return ("symbol_lessons",
                        self._gather_symbol_lessons(
                            symbol, top_wins=win_window, top_losses=loss_window))
            return ("symbol_lessons", {})

        def _g_shadow():
            if self._get_tuning_param(
                    "shadow_trade_tracking_enabled", "true").lower() == "true":
                return ("shadow_lessons",
                        self._gather_shadow_lessons(symbol))
            return ("shadow_lessons", {})

        gatherers = [_g_ohlcv, _g_da, _g_signal, _g_mentor,
                     _g_geo, _g_hist, _g_lessons, _g_shadow]

        _da_images = []
        _mentor_charts = []

        pool = ThreadPoolExecutor(max_workers=6,
                                  thread_name_prefix="dossier")
        try:
            futures = {pool.submit(fn): fn.__name__ for fn in gatherers}
            try:
                for fut in as_completed(futures, timeout=_GATHER_TIMEOUT):
                    try:
                        section_key, section_data = fut.result()
                    except Exception as e:
                        section_key = futures[fut]
                        logger.warning(f"[Dossier] {symbol} section {section_key} "
                                       f"failed: {e}")
                        continue

                    if section_key == "ohlcv":
                        dossier["sections"]["ohlcv"] = section_data
                        _sec("ohlcv")
                    elif section_key == "da_analyses":
                        dossier["sections"]["da_analyses"] = section_data.get(
                            "analyses", section_data if isinstance(section_data, list) else [])
                        dossier["da_analyses"] = section_data if isinstance(section_data, dict) else {
                            "analyses": [], "seen_summary": "", "seen_count": 0, "images": []}
                        _da_images = section_data.get("images", []) if isinstance(
                            section_data, dict) else []
                        _sec("da_charts")
                    elif section_key == "signal_intelligence":
                        dossier["sections"]["signal_intelligence"] = section_data
                        _sec("signal_intel")
                    elif section_key == "mentor_intelligence":
                        dossier["sections"]["mentor_intelligence"] = section_data
                        if isinstance(section_data, dict):
                            _mentor_charts = section_data.get("charts", [])
                            if section_data.get("calls"):
                                dossier["has_mentor_call"] = True
                                logger.info(
                                    f"[Dossier] {symbol}: "
                                    f"{len(section_data['calls'])} mentor "
                                    f"call(s) found — flagging as priority")
                        _sec("mentor_intel")
                    elif section_key == "geo_macro":
                        dossier["sections"]["geopolitical"] = section_data.get(
                            "geopolitical", {})
                        dossier["sections"]["macroeconomic"] = section_data.get(
                            "macroeconomic", {})
                        _sec("geo_macro")
                    elif section_key == "historical_performance":
                        dossier["sections"]["historical_performance"] = section_data
                        _sec("hist_perf")
                    elif section_key == "symbol_lessons":
                        dossier["sections"]["symbol_lessons"] = section_data
                        _sec("lessons")
                    elif section_key == "shadow_lessons":
                        dossier["sections"]["shadow_lessons"] = section_data
                        _sec("shadow")
            except TimeoutError:
                timed_out = [futures[f] for f in futures if not f.done()]
                logger.error(f"[Dossier] {symbol} gathering timed out after "
                             f"{_GATHER_TIMEOUT}s — missing: {timed_out}")
                for f in futures:
                    f.cancel()
            except Exception as _gather_err:
                logger.error(f"[Dossier] {symbol} gathering error: {_gather_err}")
        finally:
            pool.shutdown(wait=False)

        # Merge chart images after all threads complete (order-safe)
        dossier["chart_images"] = _mentor_charts + _da_images

        # Chart generation fallback (needs da/mentor results gathered above)
        chart_gen_enabled = self._get_tuning_param("chart_generation_enabled", "true")
        if (not dossier.get("chart_images")
                and ds_cfg.get("generated_charts", True)
                and chart_gen_enabled.lower() == "true"):
            try:
                from core.chart_generator import generate_chart_for_dossier
                gen_chart = generate_chart_for_dossier(self.db, symbol)
                if gen_chart:
                    dossier["chart_images"] = [gen_chart]
                    logger.info(f"[Dossier] BillNye generated fallback chart for {symbol}: "
                                f"{gen_chart['path']}")
            except Exception as e:
                logger.debug(f"[Dossier] BillNye chart generation fallback failed: {e}")

        # Dependent section: Data Scientist TA + Companion (needs ohlcv)
        try:
            from services.data_scientist import get_data_scientist, get_companion_feed

            if ds_cfg.get("billnye_ta", True):
                ds = get_data_scientist(self.db)
                candles = ds.get_candles_from_db(symbol)
                if candles:
                    ta_results = ds.compute_all(symbol, candles)
                    dossier["sections"]["data_scientist_ta"] = ta_results
                    dossier["sections"]["data_scientist_text"] = ds.format_for_prompt(ta_results)

            if ds_cfg.get("companion_data", True):
                cf = get_companion_feed(self.db)
                companion = cf.get_full_companion_summary(symbol)
                dossier["sections"]["companion_data"] = companion
                dossier["sections"]["companion_text"] = cf.format_companion_for_prompt(companion)
        except Exception as e:
            logger.warning(f"[Dossier] Data Scientist/Companion error: {e}")
        _sec("data_scientist")

        dossier["_timing"]["gathering_completed"] = _utcnow().isoformat()
        gather_total = (_utcnow() - _gather_start).total_seconds()
        logger.info(f"[Dossier] {symbol} GATHERING COMPLETE in {gather_total:.1f}s")
        sections_gathered = [k for k in dossier["sections"] if dossier["sections"][k]]
        _notify("gathering_done", f"Collected: {', '.join(sections_gathered)}")

        # Stage 1: Unbiased TA (cheap model) — with 1hr LLM output cache
        if include_stage1 and dossier["sections"].get("ohlcv"):
            ohlcv_sections = dossier["sections"]["ohlcv"]
            cur_price = self._extract_latest_price(ohlcv_sections)
            cache_key = f"{symbol}:{self.duo_id or 'default'}"
            s1_cache_ttl = int(self._td_cfg.get("stage1_cache_ttl_minutes", 60))
            s1_drift_pct = float(self._td_cfg.get("stage1_cache_drift_pct", 2.0))
            cache_valid = False
            with self._cache_lock:
                cached = self._stage1_cache.get(cache_key)
            if cached and cur_price and cur_price > 0:
                age_min = (_utcnow() - cached["ts"]).total_seconds() / 60
                old_price = cached.get("price", 0)
                drift = abs(cur_price - old_price) / old_price * 100 if old_price > 0 else 999
                cache_valid = age_min < s1_cache_ttl and drift < s1_drift_pct

            if cache_valid:
                dossier["stage1_output"] = cached["output"]
                dossier["sections"]["technical_analysis"] = cached["output"]
                dossier["_timing"]["stage1_started"] = _utcnow().isoformat()
                dossier["_timing"]["stage1_completed"] = _utcnow().isoformat()
                dossier["_stage1_cached"] = True
                logger.info(f"[Dossier] {symbol}: Stage 1 CACHE HIT "
                            f"(age={(_utcnow() - cached['ts']).total_seconds()/60:.0f}m, "
                            f"drift={abs(cur_price - cached.get('price',0)) / max(cached.get('price',1),1) * 100:.1f}%)")
                _notify("stage1_done", "TA complete (cached)")
            else:
                try:
                    s1_cfg = self._resolve_stage_model("stage1")
                    _notify("stage1_ta", f"LLM: {s1_cfg.get('model','unknown')}")
                except Exception:
                    _notify("stage1_ta", "Starting Stage 1 TA")
                dossier["_timing"]["stage1_started"] = _utcnow().isoformat()
                dossier["stage1_output"] = self._run_stage1(
                    symbol, ohlcv_sections,
                    billnye_text=dossier["sections"].get("data_scientist_text", ""),
                    geo_data=dossier["sections"].get("geopolitical", {}),
                    macro_data=dossier["sections"].get("macroeconomic", {}))
                dossier["sections"]["technical_analysis"] = dossier["stage1_output"]
                dossier["_timing"]["stage1_completed"] = _utcnow().isoformat()
                dossier["_stage1_cached"] = False
                if dossier.get("stage1_output") and cur_price and cur_price > 0:
                    with self._cache_lock:
                        self._stage1_cache[cache_key] = {
                            "output": dossier["stage1_output"],
                            "ts": _utcnow(),
                            "price": cur_price,
                        }
                        self._stage1_cache.move_to_end(cache_key)
                        while len(self._stage1_cache) > self._S1_CACHE_MAX:
                            self._stage1_cache.popitem(last=False)
                _notify("stage1_done", "TA complete")

        dossier["_mentor_triggered"] = mentor_triggered
        dossier["_mentor_signal"] = mentor_signal

        # Gate: skip Stage 2 if Stage 1 was required but failed/empty
        s1_out = dossier.get("stage1_output")
        s1_failed = (include_stage1 and
                     (not s1_out or
                      (isinstance(s1_out, str) and s1_out.startswith("[Stage 1"))))
        if s1_failed:
            logger.warning(f"[Dossier] Stage 1 failed for {symbol} — skipping Stage 2 "
                           f"(no TA to base decision on)")
            dossier["stage2_output"] = {
                "trade_decision": "do_not_trade",
                "confidence_score": 0,
                "reasoning": "Stage 1 TA unavailable — cannot make informed decision"}
            try:
                self._update_symbol_intel(symbol, dossier)
            except Exception:
                pass
            return dossier

        # Quant Score Gate: extract OVERALL_SETUP_SCORE from S1, gate S2 if enabled
        qs_enabled = self._td_cfg.get("quant_score_gate_enabled", False)
        qs_minimum = int(self._td_cfg.get("quant_score_gate_minimum", 35))
        quant_score = self._extract_quant_score(s1_out)
        dossier["quant_score"] = quant_score

        if quant_score is not None:
            logger.info(f"[Dossier] {symbol}: Quant Score = {quant_score} "
                        f"(gate {'ON' if qs_enabled else 'OFF'}, min={qs_minimum})")

        if qs_enabled and quant_score is not None and quant_score < qs_minimum:
            logger.info(f"[Dossier] {symbol}: GATED — Quant Score {quant_score} < "
                        f"{qs_minimum} minimum. Skipping Stage 2 to save API cost.")
            _notify("qs_gate", f"Quant Score {quant_score} < {qs_minimum} — S2 skipped")
            dossier["stage2_output"] = {
                "trade_decision": "do_not_trade",
                "confidence_score": 0,
                "reasoning": (f"Quant Score gate: {quant_score}/100 is below the "
                              f"minimum threshold of {qs_minimum}. Setup quality "
                              f"too low to justify Stage 2 analysis.")}
            try:
                self._update_symbol_intel(symbol, dossier)
            except Exception:
                pass
            return dossier

        # Stage 2: Premium model decision (with images) — with do_not_trade cooldown
        if include_stage2:
            s2_cooldown_hrs = int(self._td_cfg.get("stage2_dnt_cooldown_hours", 24))
            s2_cache_key = f"{symbol}:{self.duo_id or 'default'}"
            with self._cache_lock:
                s2_cached = self._stage2_dnt_cache.get(s2_cache_key)
            mentor_bypass = dossier.get("_mentor_triggered", False)
            s2_cooldown_hit = False
            if s2_cached and not mentor_bypass and s2_cooldown_hrs > 0:
                age_hrs = (_utcnow() - s2_cached["ts"]).total_seconds() / 3600
                s2_cooldown_hit = age_hrs < s2_cooldown_hrs

            if s2_cooldown_hit:
                dossier["stage2_output"] = s2_cached["output"]
                dossier["_timing"]["stage2_started"] = _utcnow().isoformat()
                dossier["_timing"]["stage2_completed"] = _utcnow().isoformat()
                dossier["_stage2_cached"] = True
                age_hrs = (_utcnow() - s2_cached["ts"]).total_seconds() / 3600
                logger.info(f"[Dossier] {symbol}: Stage 2 COOLDOWN — do_not_trade "
                            f"was issued {age_hrs:.1f}h ago, skipping API call")
                _notify("stage2_done", "Decision: do_not_trade (cooldown)")
            else:
                try:
                    s2_cfg = self._resolve_stage_model("stage2")
                    _notify("stage2_decision", f"LLM: {s2_cfg.get('model','unknown')}")
                except Exception:
                    _notify("stage2_decision", "Starting Stage 2 Decision")
                dossier["_timing"]["stage2_started"] = _utcnow().isoformat()
                dossier["stage2_output"] = self._run_stage2(symbol, dossier)
                dossier["_timing"]["stage2_completed"] = _utcnow().isoformat()
                dossier["_stage2_cached"] = False
                decision_str = dossier['stage2_output'].get('trade_decision','unknown') if dossier.get('stage2_output') else 'failed'
                _notify("stage2_done", f"Decision: {decision_str}")
                if dossier.get("stage2_output", {}).get("trade_decision") == "do_not_trade":
                    with self._cache_lock:
                        self._stage2_dnt_cache[s2_cache_key] = {
                            "output": dossier["stage2_output"],
                            "ts": _utcnow(),
                        }
                        self._stage2_dnt_cache.move_to_end(s2_cache_key)
                        while len(self._stage2_dnt_cache) > self._S2_CACHE_MAX:
                            self._stage2_dnt_cache.popitem(last=False)

        # Bull/Bear Pre-Trade Debate (agents loaded from DB, thresholds from system_config)
        s2_out = dossier.get("stage2_output") or {}
        debate_enabled = self._td_cfg.get("bull_bear_debate_enabled", False)
        if (debate_enabled
                and s2_out.get("trade_decision") in ("trade_now", "wait_for_conditions")
                and s2_out.get("direction") and s2_out.get("entry_price")):
            # Bull case: advocacy
            _notify("bull_case", "Running Bull Agent advocacy")
            dossier["_timing"]["bull_case_started"] = _utcnow().isoformat()
            bull_result = self._run_bull_case(symbol, dossier)
            dossier["_timing"]["bull_case_completed"] = _utcnow().isoformat()
            if bull_result:
                s2_out["bull_case"] = bull_result.get("bull_argument", "")
                s2_out["bull_confidence"] = bull_result.get("bull_confidence", 0)
                s2_out["bull_supporting_factors"] = bull_result.get("supporting_factors", [])
                s2_out["bull_verdict"] = bull_result.get("verdict", "")
                _notify("bull_case_done",
                        f"Bull confidence: {bull_result.get('bull_confidence', 0)}%")

            # Bear case: challenge
            _notify("bear_case", "Running Bear Agent challenge")
            dossier["_timing"]["bear_case_started"] = _utcnow().isoformat()
            bear_result = self._run_bear_case(symbol, dossier)
            dossier["_timing"]["bear_case_completed"] = _utcnow().isoformat()
            if bear_result:
                s2_out["bear_case"] = bear_result.get("bear_argument", "")
                s2_out["bear_confidence"] = bear_result.get("bear_confidence", 0)
                s2_out["bear_key_risks"] = bear_result.get("key_risks", [])
                s2_out["bear_verdict"] = bear_result.get("verdict", "")

                # Config-driven confidence adjustment
                bear_conf = bear_result.get("bear_confidence", 0)
                orig_conf = s2_out.get("confidence_score") or 0
                soft_thresh = int(self._get_tuning_param("bear_confidence_soft_threshold", "55"))
                hard_thresh = int(self._get_tuning_param("bear_confidence_hard_threshold", "75"))
                soft_reduction = int(self._get_tuning_param("bear_confidence_reduction_soft", "8"))
                hard_reduction = int(self._get_tuning_param("bear_confidence_reduction_hard", "15"))

                if bear_conf >= hard_thresh and orig_conf > 0:
                    penalty = min(hard_reduction, (bear_conf - 60) // 3)
                    new_conf = max(30, orig_conf - penalty)
                    logger.info(
                        f"[Dossier] Bear STRONG_OBJECTION ({bear_conf}%) — "
                        f"confidence {orig_conf}% -> {new_conf}% (penalty -{penalty})")
                    s2_out["confidence_score"] = new_conf
                    s2_out["bear_confidence_adjustment"] = -penalty
                elif bear_conf >= soft_thresh and orig_conf > 0:
                    penalty = min(soft_reduction, (bear_conf - 40) // 4)
                    new_conf = max(40, orig_conf - penalty)
                    logger.info(
                        f"[Dossier] Bear CHALLENGE ({bear_conf}%) — "
                        f"confidence {orig_conf}% -> {new_conf}% (penalty -{penalty})")
                    s2_out["confidence_score"] = new_conf
                    s2_out["bear_confidence_adjustment"] = -penalty

                _notify("bear_case_done",
                        f"Bear confidence: {bear_conf}%")

        # P3-C: Direction-aware confidence threshold for SELL/SHORT
        direction = (s2_out.get("direction") or "").upper()
        if direction in ("SELL", "SHORT"):
            short_min = int(self._get_tuning_param("short_min_confidence", "75"))
            current_conf = s2_out.get("confidence_score") or 0
            if current_conf < short_min:
                logger.info(
                    f"[Dossier] {symbol}: SHORT blocked — confidence {current_conf}% "
                    f"< short_min_confidence {short_min}%")
                s2_out["trade_decision"] = "do_not_trade"
                s2_out["confidence_score"] = 0
                s2_out["reasoning"] = (
                    f"SHORT direction blocked: confidence {current_conf}% below "
                    f"short_min_confidence threshold ({short_min}%). Directional edge "
                    f"data shows ~42% WR on shorts vs ~70% on longs.")
                s2_out["short_blocked"] = True
                _notify("short_blocked",
                        f"{symbol}: SHORT confidence {current_conf}% < {short_min}%")

        # Save to database (with timing columns)
        dossier_id = self._save_dossier(symbol, dossier)
        dossier["dossier_id"] = dossier_id

        # Flush deferred compliance_note_influence rows (collected during prompt build)
        cni_ids = getattr(self, "_pending_cni_report_ids", None)
        if dossier_id and dossier_id > 0 and cni_ids:
            try:
                for rid in cni_ids:
                    self.db.execute(
                        "INSERT INTO compliance_note_influence "
                        "(dossier_id, note_report_id) VALUES (%s, %s)",
                        (dossier_id, rid))
            except Exception as cni_err:
                logger.debug(f"[Dossier] Compliance influence tracking error: {cni_err}")
            finally:
                self._pending_cni_report_ids = None

        # Write pipeline timing to DB
        try:
            t = dossier.get("_timing", {})
            self.db.execute("""
                UPDATE trade_dossiers
                SET gathering_started_at = %s, stage1_started_at = %s,
                    stage1_completed_at = %s, stage2_started_at = %s,
                    stage2_completed_at = %s
                WHERE id = %s
            """, (t.get("gathering_started"), t.get("stage1_started"),
                  t.get("stage1_completed"), t.get("stage2_started"),
                  t.get("stage2_completed"), dossier_id))
        except Exception as e:
            logger.debug(f"[Dossier] Timing update error: {e}")

        # Persist waterfall resolution flag so the UI can show whether
        # this symbol is tradeable on connected accounts
        if dossier_id and dossier_id > 0 and norm:
            try:
                wf_resolved = 1 if norm.get("method") == "waterfall" else (
                    1 if norm.get("exchange_verified") else 0)
                wf_account = norm.get("waterfall_account")
                self.db.execute(
                    "UPDATE trade_dossiers "
                    "SET waterfall_resolved = %s, waterfall_account = %s "
                    "WHERE id = %s",
                    (wf_resolved, wf_account, dossier_id))
            except Exception as e:
                logger.debug(f"[Dossier] Waterfall flag update: {e}")

        # Store apex_entry_reasoning from Stage 2 output
        try:
            s2 = dossier.get("stage2_output") or {}
            td = s2.get("trade_decision", "")
            if td in ("trade_now", "wait_for_conditions"):
                parts = [
                    f"Decision: {td}",
                    f"Direction: {s2.get('direction', '')}",
                    f"Rationale: {str(s2.get('rationale', ''))[:500]}",
                    f"SL Logic: {str(s2.get('stop_loss_reasoning', s2.get('rationale', '')))[:300]}",
                ]
                mentor_refs = s2.get("mentor_references", s2.get("signal_references", ""))
                if mentor_refs:
                    parts.append(f"Mentor refs: {str(mentor_refs)[:300]}")
                reasoning = " | ".join(parts)
                self.db.execute(
                    "UPDATE trade_dossiers SET apex_entry_reasoning = %s WHERE id = %s",
                    (reasoning[:2000], dossier_id))
                logger.debug(f"[Dossier] #{dossier_id} apex_entry_reasoning stored")
        except Exception as e:
            logger.debug(f"[Dossier] #{dossier_id} entry reasoning error: {e}")

        # Record which DA analyses Apex saw in this dossier
        if dossier_id and dossier_id > 0 and dossier.get("da_analyses"):
            try:
                for a in dossier["da_analyses"].get("analyses", []):
                    aid = a.get("id")
                    if aid:
                        self.db.execute(
                            "INSERT IGNORE INTO apex_seen_items (symbol, news_item_id, dossier_id) "
                            "VALUES (%s, %s, %s)",
                            (symbol, aid, dossier_id))
            except Exception as e:
                logger.debug(f"[Dossier] Error recording seen items: {e}")

        logger.info(f"[Dossier] Dossier #{dossier_id} built for {symbol}")

        if dossier_id and dossier_id > 0:
            try:
                self._update_symbol_intel(symbol, dossier)
            except Exception as e:
                logger.debug(f"[Dossier] symbol_intel writeback: {e}")

        return dossier

    # ── Section Gatherers ────────────────────────────────────────────

    def _gather_ohlcv(self, symbol: str) -> Dict:
        """Section 1: Multi-timeframe OHLCV data."""
        if self.candle_collector:
            timeframes = self._td_cfg.get("ohlcv_timeframes")
            return self.candle_collector.get_ohlcv_for_dossier(symbol, timeframes)
        logger.warning(f"[Dossier] No candle_collector available for {symbol}")
        return {}

    def _gather_da_analyses(self, symbol: str, dossier_id: int = None) -> Dict:
        """Section 3: DA analyses. Only full-detail for NEW items; summary for previously-seen."""
        days = self._td_cfg.get("news_lookback_days", 7)
        analyses = get_da_analyses_for_symbol(self.db, symbol, days)
        download_dir = self.config.raw.get(
            "data_management", {}).get("alpha_download_dir", "data/alpha_downloads")

        # Find which items Apex already saw for this symbol
        seen_ids = set()
        try:
            seen_rows = self.db.fetch_all(
                "SELECT news_item_id FROM apex_seen_items WHERE symbol = %s",
                (symbol,))
            seen_ids = {r["news_item_id"] for r in (seen_rows or [])}
        except Exception:
            pass

        # Cleanup old seen records (>7 days)
        try:
            self.db.execute(
                "DELETE FROM apex_seen_items WHERE seen_at < NOW() - INTERVAL 7 DAY")
        except Exception:
            pass

        formatted_new = []
        formatted_seen_summary = []
        images = []

        for a in analyses:
            aid = a["id"]
            entry = {
                "id": aid,
                "author": a.get("author"),
                "source": a.get("source"),
                "headline": a.get("headline"),
                "direction": a.get("direction"),
                "timeframe": a.get("tv_timeframe"),
                "ai_analysis": a.get("ai_analysis"),
                "collected_at": a["collected_at"].isoformat() if a.get("collected_at") else None,
            }

            if aid in seen_ids:
                formatted_seen_summary.append({
                    "id": aid,
                    "author": a.get("author"),
                    "direction": a.get("direction"),
                    "headline": (a.get("headline") or "")[:100],
                    "collected_at": entry["collected_at"],
                })
            else:
                formatted_new.append(entry)
                for img_field in ["chart_image_url", "media_url"]:
                    img_url = a.get(img_field)
                    if img_url:
                        local_path = resolve_chart_image_path(img_url, download_dir)
                        if local_path:
                            images.append({
                                "path": local_path,
                                "source": a.get("source"),
                                "author": a.get("author"),
                                "analysis_id": aid,
                                "description": f"Chart from {a.get('author', 'unknown')} "
                                               f"via {a.get('source', 'unknown')} - "
                                               f"{a.get('headline', '')[:80]}"
                            })

        max_images = 15
        signal_charts = self._gather_signal_provider_charts(symbol, download_dir)
        images.extend(signal_charts)
        if len(images) > max_images:
            images = images[:max_images]

        # Build seen summary text
        seen_summary = ""
        if formatted_seen_summary:
            directions = [s.get("direction") or "?" for s in formatted_seen_summary]
            authors = list(set(s.get("author") or "?" for s in formatted_seen_summary))
            seen_summary = (
                f"[PREVIOUSLY REVIEWED: {len(formatted_seen_summary)} analyses from "
                f"{', '.join(authors[:5])}. "
                f"Directions: {', '.join(directions[:10])}. "
                f"These were reviewed in prior dossiers — focus on NEW analyses above.]"
            )

        logger.debug(f"[Dossier] DA analyses for {symbol}: "
                     f"{len(formatted_new)} new, {len(formatted_seen_summary)} seen")

        return {
            "analyses": formatted_new,
            "seen_summary": seen_summary,
            "seen_count": len(formatted_seen_summary),
            "images": images,
        }

    def _gather_signal_provider_charts(self, symbol: str, download_dir: str) -> List[Dict]:
        """Fetch chart images from signal providers (last 24h, still viable)."""
        try:
            cutoff = _utcnow() - timedelta(hours=24)
            rows = self.db.fetch_all("""
                SELECT ps.id, ps.author, ps.source_detail, ps.direction,
                       ni.chart_image_url, ni.media_url, ni.headline,
                       ni.source AS ni_source
                FROM parsed_signals ps
                LEFT JOIN news_items ni ON ps.news_item_id = ni.id
                WHERE ps.symbol = %s
                      AND ps.parsed_at >= %s
                      AND ps.status NOT IN ('expired', 'stopped_out', 'closed', 'tp_hit')
                      AND (ni.chart_image_url IS NOT NULL OR ni.media_url IS NOT NULL)
                      AND ps.parsed_by NOT IN ('trading_floor', 'signal_ai')
                      AND ps.author NOT IN ('JarvAIs', 'jarvais', 'Jarvis', 'jarvis')
                LIMIT 20
            """, (symbol, cutoff))

            charts = []
            for r in (rows or []):
                for img_field in ["chart_image_url", "media_url"]:
                    img_url = r.get(img_field)
                    if img_url:
                        local_path = resolve_chart_image_path(img_url, download_dir)
                        if local_path:
                            charts.append({
                                "path": local_path,
                                "source": r.get("ni_source", r.get("source_detail", "signal")),
                                "author": r.get("author", "signal_provider"),
                                "analysis_id": r.get("id"),
                                "description": (
                                    f"[SIGNAL PROVIDER] {r.get('author','?')} "
                                    f"({r.get('direction','?')}) - reference chart. "
                                    f"Trade may have expired. Use for confluence only, not bias."
                                ),
                            })
            return charts
        except Exception as e:
            logger.warning(f"[Dossier] Signal provider chart gathering error: {e}")
            return []

    def _get_confidence_calibration_card(self) -> str:
        """Build a calibration card showing historical accuracy per confidence
        bucket. Injected into Stage 2 so the model can self-calibrate its
        confidence assignments based on actual outcomes."""
        try:
            rows = self.db.fetch_all("""
                SELECT confidence_score, status
                FROM trade_dossiers
                WHERE status IN ('won','lost')
                  AND confidence_score IS NOT NULL
                ORDER BY confidence_score ASC
            """)
            if not rows or len(rows) < 15:
                return ""

            buckets = {}
            for r in rows:
                conf = int(r["confidence_score"] or 0)
                bucket = (conf // 5) * 5
                if bucket not in buckets:
                    buckets[bucket] = {"wins": 0, "losses": 0}
                if r["status"] == "won":
                    buckets[bucket]["wins"] += 1
                else:
                    buckets[bucket]["losses"] += 1

            lines = ["## YOUR CONFIDENCE CALIBRATION (historical accuracy)",
                      "Use this data to calibrate your confidence score. "
                      "Do not inflate confidence beyond what the data supports.",
                      ""]
            total_w = sum(b["wins"] for b in buckets.values())
            total_l = sum(b["losses"] for b in buckets.values())
            total = total_w + total_l
            lines.append(f"Overall: {total_w}W/{total_l}L "
                         f"({total_w/total*100:.0f}% WR, {total} trades)")
            lines.append("")

            for b in sorted(buckets.keys()):
                d = buckets[b]
                n = d["wins"] + d["losses"]
                if n < 2:
                    continue
                wr = d["wins"] / n * 100
                label = "well-calibrated" if abs(wr - b) < 15 else (
                    "OVERCONFIDENT" if wr < b - 15 else "underconfident")
                lines.append(f"- {b}-{b+4}%: actual WR={wr:.0f}% "
                             f"({d['wins']}W/{d['losses']}L, n={n}) — {label}")

            lines.append("")
            lines.append("RULES: If your historical WR at a confidence level is "
                         "much lower than the confidence score itself, you are "
                         "overconfident at that level. Adjust downward. "
                         "Only assign 80%+ confidence when the setup is exceptional.")
            return "\n".join(lines) + "\n"
        except Exception as e:
            logger.debug(f"[Dossier] Confidence calibration card error: {e}")
            return ""

    def _gather_symbol_lessons(self, symbol: str, top_wins: int = 25,
                               top_losses: int = 25) -> Dict:
        """Symbol Knowledge Bank: gather structured lessons from prior trades.
        Returns top N recent wins and top N recent losses with their lessons,
        root causes, and optimal trade recommendations from Auditor postmortems.

        Memory decay: lessons older than 30 days are ranked lower via a
        freshness score (1.0 for today, decays by 50% every 30 days).
        SQL sorts by freshness-weighted P&L so recent high-impact lessons
        surface first while stale lessons naturally drop off.

        Time-budgeted: SQL phase runs first; RAG phase only executes if
        at least 30s remain before the gather timeout (120s default)."""
        import time as _time
        _t0 = _time.monotonic()
        _LESSON_TIMEOUT = 90
        _RAG_BUDGET_SECS = 30
        result = {"wins": [], "losses": [], "total_lessons": 0}

        try:
            # Wins with lessons (freshness-weighted sort)
            win_rows = self.db.fetch_all("""
                SELECT tl.lesson_text, tl.root_cause, tl.optimal_trade_summary,
                       tl.what_worked, tl.pnl_usd, tl.timestamp,
                       td.id as dossier_id, td.direction, td.entry_price,
                       td.stop_loss, td.take_profit_1, td.confidence_score,
                       td.realised_pnl, td.realised_pnl_pct, td.leverage,
                       td.mentor_source,
                       DATEDIFF(NOW(), tl.timestamp) AS age_days
                FROM trade_lessons tl
                JOIN trade_dossiers td ON tl.dossier_id = td.id
                WHERE tl.symbol = %s AND tl.outcome = 'WIN'
                ORDER BY (ABS(COALESCE(td.realised_pnl_pct,0))
                          * POW(0.5, DATEDIFF(NOW(), tl.timestamp) / 30.0)) DESC,
                         tl.timestamp DESC
                LIMIT %s
            """, (symbol, top_wins))

            for r in (win_rows or []):
                result["wins"].append({
                    "dossier_id": r["dossier_id"],
                    "direction": r.get("direction"),
                    "entry": float(r["entry_price"]) if r.get("entry_price") else None,
                    "sl": float(r["stop_loss"]) if r.get("stop_loss") else None,
                    "tp1": float(r["take_profit_1"]) if r.get("take_profit_1") else None,
                    "pnl": float(r.get("realised_pnl") or 0),
                    "pnl_pct": float(r.get("realised_pnl_pct") or 0),
                    "confidence": r.get("confidence_score"),
                    "leverage": r.get("leverage"),
                    "mentor": r.get("mentor_source"),
                    "lesson": (r.get("lesson_text") or "")[:1000],
                    "what_worked": (r.get("what_worked") or "")[:500],
                    "root_cause": (r.get("root_cause") or "")[:500],
                })

            # Losses with lessons (freshness-weighted sort)
            loss_rows = self.db.fetch_all("""
                SELECT tl.lesson_text, tl.root_cause, tl.optimal_trade_summary,
                       tl.what_failed, tl.pnl_usd, tl.timestamp,
                       td.id as dossier_id, td.direction, td.entry_price,
                       td.stop_loss, td.take_profit_1, td.confidence_score,
                       td.realised_pnl, td.realised_pnl_pct, td.leverage,
                       td.mentor_source,
                       DATEDIFF(NOW(), tl.timestamp) AS age_days
                FROM trade_lessons tl
                JOIN trade_dossiers td ON tl.dossier_id = td.id
                WHERE tl.symbol = %s AND tl.outcome IN ('LOSS','BREAKEVEN')
                ORDER BY (ABS(COALESCE(td.realised_pnl_pct,0))
                          * POW(0.5, DATEDIFF(NOW(), tl.timestamp) / 30.0)) DESC,
                         tl.timestamp DESC
                LIMIT %s
            """, (symbol, top_losses))

            for r in (loss_rows or []):
                result["losses"].append({
                    "dossier_id": r["dossier_id"],
                    "direction": r.get("direction"),
                    "entry": float(r["entry_price"]) if r.get("entry_price") else None,
                    "sl": float(r["stop_loss"]) if r.get("stop_loss") else None,
                    "tp1": float(r["take_profit_1"]) if r.get("take_profit_1") else None,
                    "pnl": float(r.get("realised_pnl") or 0),
                    "pnl_pct": float(r.get("realised_pnl_pct") or 0),
                    "confidence": r.get("confidence_score"),
                    "leverage": r.get("leverage"),
                    "mentor": r.get("mentor_source"),
                    "lesson": (r.get("lesson_text") or "")[:1000],
                    "what_failed": (r.get("what_failed") or "")[:500],
                    "root_cause": (r.get("root_cause") or "")[:500],
                    "optimal_trade": (r.get("optimal_trade_summary") or "")[:500],
                })

            result["total_lessons"] = len(result["wins"]) + len(result["losses"])

            # Also pull from audit_reports for any dossiers not yet in trade_lessons
            covered_ids = set()
            for w in result["wins"]:
                covered_ids.add(w["dossier_id"])
            for lo in result["losses"]:
                covered_ids.add(lo["dossier_id"])

            remaining = (top_wins + top_losses) - len(covered_ids)
            if remaining > 0:
                exclude_ids = ",".join(str(int(i)) for i in covered_ids) if covered_ids else "0"
                fallback = self.db.fetch_all(f"""
                    SELECT ar.dossier_id, ar.root_cause, ar.auditor_summary,
                           ar.trade_outcome, ar.pnl_amount, ar.pnl_pct,
                           td.direction, td.entry_price, td.confidence_score,
                           td.leverage
                    FROM audit_reports ar
                    JOIN trade_dossiers td ON ar.dossier_id = td.id
                    WHERE td.symbol = %s
                      AND ar.status = 'completed'
                      AND ar.report_type = 'trade_postmortem'
                      AND ar.dossier_id NOT IN ({exclude_ids})
                    ORDER BY ar.completed_at DESC
                    LIMIT %s
                """, (symbol, remaining))

                for r in (fallback or []):
                    entry = {
                        "dossier_id": r["dossier_id"],
                        "direction": r.get("direction"),
                        "entry": float(r["entry_price"]) if r.get("entry_price") else None,
                        "pnl": float(r.get("pnl_amount") or 0),
                        "pnl_pct": float(r.get("pnl_pct") or 0),
                        "confidence": r.get("confidence_score"),
                        "leverage": r.get("leverage"),
                        "lesson": (r.get("auditor_summary") or "")[:500],
                        "root_cause": (r.get("root_cause") or "")[:500],
                    }
                    outcome = r.get("trade_outcome", "")
                    if outcome == "won":
                        result["wins"].append(entry)
                    else:
                        result["losses"].append(entry)
                    result["total_lessons"] += 1

        except Exception as e:
            logger.warning(f"[Dossier] Symbol lessons gathering error for {symbol}: {e}")

        # Supplement with RAG search for semantic matches (catches nuance SQL misses).
        # Skip RAG entirely if SQL phase already consumed most of the time budget
        # to avoid exceeding the gather timeout (120s default).
        _elapsed = _time.monotonic() - _t0
        if _elapsed > (_LESSON_TIMEOUT - _RAG_BUDGET_SECS):
            logger.debug(f"[Dossier] {symbol} lessons: skipping RAG "
                         f"(SQL took {_elapsed:.1f}s, budget exhausted)")
            return result

        try:
            from core.rag_search import RagSearchEngine
            rag = RagSearchEngine(db=self.db)

            covered = set()
            for w in result["wins"]:
                covered.add(w.get("dossier_id"))
            for lo in result["losses"]:
                covered.add(lo.get("dossier_id"))

            # Same-symbol RAG with specific trade-context query
            rag_results = rag.search(
                query=(f"trade outcomes and lessons for {symbol}: "
                       f"what worked in winning trades, what caused losses, "
                       f"optimal entry/exit patterns, stop-loss effectiveness, "
                       f"risk-reward analysis, confidence calibration"),
                collections=["trade_memory"],
                limit=10, hybrid=True)

            # Skip cross-symbol RAG if time budget is thin
            _elapsed2 = _time.monotonic() - _t0
            if _elapsed2 > (_LESSON_TIMEOUT - 15):
                logger.debug(f"[Dossier] {symbol} lessons: skipping cross-symbol RAG "
                             f"({_elapsed2:.1f}s elapsed)")
                cross_results = []
            else:
                # Cross-symbol RAG: find similar setups from OTHER symbols
                asset_class = "cryptocurrency"
                try:
                    ac_row = self.db.fetch_one(
                        "SELECT asset_class FROM market_symbols WHERE symbol = %s",
                        (symbol,))
                    if ac_row and ac_row.get("asset_class"):
                        asset_class = ac_row["asset_class"]
                except Exception:
                    pass
                cross_results = rag.search(
                    query=(f"common mistakes trading {asset_class} "
                           f"false breakout stop loss placement leverage risk"),
                    collections=["trade_memory"],
                    limit=5, hybrid=True) or []
            all_rag = (rag_results or []) + cross_results

            rag_extras = []
            for r in all_rag:
                meta = r.metadata if hasattr(r, "metadata") else {}
                did = meta.get("dossier_id")
                if did and did in covered:
                    continue
                covered.add(did)
                rag_extras.append({
                    "source": "rag",
                    "text": r.text[:800] if hasattr(r, "text") else str(r)[:800],
                    "score": r.score if hasattr(r, "score") else 0,
                    "symbol": meta.get("symbol", ""),
                    "type": meta.get("type", ""),
                    "cross_symbol": meta.get("symbol", "") != symbol,
                })
            if rag_extras:
                result["rag_context"] = rag_extras[:8]
        except Exception as e:
            logger.debug(f"[Dossier] RAG supplement error for {symbol}: {e}")

        # Fetch mentor comparison lessons (Apex vs Mentor learning)
        try:
            mentor_cmp_rows = self.db.fetch_all("""
                SELECT lesson_text FROM trade_lessons
                WHERE symbol = %s AND model_used = 'mentor_comparison'
                ORDER BY timestamp DESC LIMIT 5
            """, (symbol,))
            if mentor_cmp_rows:
                result["mentor_comparisons"] = [
                    {"lesson": r["lesson_text"][:500]} for r in mentor_cmp_rows
                ]
        except Exception as e:
            logger.debug(f"[Dossier] Mentor comparison fetch error: {e}")

        logger.info(f"[Dossier] Symbol lessons for {symbol}: "
                    f"{len(result['wins'])} wins, {len(result['losses'])} losses"
                    f", {len(result.get('rag_context', []))} RAG extras"
                    f", {len(result.get('mentor_comparisons', []))} mentor comparisons")
        return result

    def _gather_shadow_lessons(self, symbol: str) -> Dict:
        """Retrieve evaluated shadow trades for this symbol (and same asset class)
        to inject as 'REJECTED TRADE HINDSIGHT' into Stage 2. Gives Apex the full
        trade setup, his original reasoning, what happened, and Ledger's lesson."""
        max_items = int(self._get_tuning_param("shadow_max_lessons_in_prompt", "5"))
        band_days = int(self._get_tuning_param("shadow_confidence_band_window_days", "14"))

        result = {
            "shadows": [],
            "band_stats": [],
            "summary": {},
        }

        try:
            ms_row = self.db.fetch_one(
                "SELECT asset_class FROM market_symbols WHERE symbol = %s", (symbol,))
            asset_class = (ms_row or {}).get("asset_class", "unknown")

            shadows = self.db.fetch_all("""
                SELECT id, symbol, direction, entry_price, stop_loss, take_profit_1,
                       confidence_score, rationale, shadow_status, exit_reason,
                       counterfactual_pnl_pct, lesson_text, rejected_at, evaluated_at
                FROM apex_shadow_trades
                WHERE (symbol = %s OR asset_class = %s)
                  AND shadow_status IN ('shadow_won', 'shadow_lost')
                  AND lesson_text IS NOT NULL
                  AND rejected_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                ORDER BY CASE WHEN symbol = %s THEN 0 ELSE 1 END,
                         rejected_at DESC
                LIMIT %s
            """, (symbol, asset_class, band_days, symbol, max_items))

            for s in (shadows or []):
                age_days = 0
                if s.get("rejected_at"):
                    age_days = (_utcnow() - s["rejected_at"]).days

                result["shadows"].append({
                    "id": s["id"],
                    "symbol": s["symbol"],
                    "direction": s.get("direction"),
                    "entry": s.get("entry_price"),
                    "sl": s.get("stop_loss"),
                    "tp1": s.get("take_profit_1"),
                    "confidence": s.get("confidence_score"),
                    "rationale": (s.get("rationale") or "")[:1000],
                    "status": s["shadow_status"],
                    "exit_reason": s.get("exit_reason"),
                    "pnl_pct": float(s.get("counterfactual_pnl_pct") or 0),
                    "lesson": (s.get("lesson_text") or "")[:500],
                    "age_days": age_days,
                })

        except Exception as e:
            logger.debug(f"[Dossier] Shadow lessons fetch error for {symbol}: {e}")

        # Confidence band analysis: win rate by 5-point confidence buckets
        try:
            bands = self.db.fetch_all("""
                SELECT
                    FLOOR(confidence_score / 5) * 5 AS band_low,
                    COUNT(*) AS total,
                    SUM(shadow_status = 'shadow_won') AS wins,
                    SUM(shadow_status = 'shadow_lost') AS losses,
                    ROUND(AVG(counterfactual_pnl_pct), 2) AS avg_pnl_pct
                FROM apex_shadow_trades
                WHERE shadow_status IN ('shadow_won', 'shadow_lost')
                  AND confidence_score IS NOT NULL
                  AND rejected_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
                GROUP BY band_low
                ORDER BY band_low
            """, (band_days,))

            for b in (bands or []):
                low = int(b["band_low"])
                total = int(b["total"])
                wins = int(b.get("wins") or 0)
                result["band_stats"].append({
                    "range": f"{low}-{low + 4}%",
                    "total": total,
                    "wins": wins,
                    "losses": total - wins,
                    "win_rate": round(wins / total * 100, 1) if total else 0,
                    "avg_pnl_pct": float(b.get("avg_pnl_pct") or 0),
                })

        except Exception as e:
            logger.debug(f"[Dossier] Shadow band stats error: {e}")

        # Aggregate summary
        try:
            summary = self.db.fetch_one("""
                SELECT COUNT(*) AS total,
                       SUM(shadow_status = 'shadow_won') AS wins,
                       SUM(shadow_status = 'shadow_lost') AS losses,
                       SUM(shadow_status = 'shadow_expired') AS expired,
                       ROUND(AVG(CASE WHEN shadow_status = 'shadow_won'
                                 THEN counterfactual_pnl_pct END), 2) AS avg_win_pct,
                       ROUND(AVG(CASE WHEN shadow_status = 'shadow_lost'
                                 THEN counterfactual_pnl_pct END), 2) AS avg_loss_pct
                FROM apex_shadow_trades
                WHERE shadow_status IN ('shadow_won', 'shadow_lost', 'shadow_expired')
                  AND rejected_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            """, (band_days,))

            if summary:
                result["summary"] = {
                    "total_evaluated": int(summary.get("total") or 0),
                    "would_have_won": int(summary.get("wins") or 0),
                    "would_have_lost": int(summary.get("losses") or 0),
                    "expired": int(summary.get("expired") or 0),
                    "avg_win_pct": float(summary.get("avg_win_pct") or 0),
                    "avg_loss_pct": float(summary.get("avg_loss_pct") or 0),
                }
        except Exception as e:
            logger.debug(f"[Dossier] Shadow summary error: {e}")

        shadow_count = len(result["shadows"])
        if shadow_count:
            logger.info(f"[Dossier] Shadow lessons for {symbol}: {shadow_count} items, "
                        f"{len(result['band_stats'])} confidence bands")
        return result

    def _gather_mentor_intelligence(self, symbol: str) -> Dict:
        """Gather ALL content from designated mentor traders for this symbol.

        Pulls from news_items where the author is a mentor (via user_profiles +
        user_profile_links). Includes their posts, charts, signals, parsed
        analyses, and accumulated learnings. No time limit — mentors' content
        is always included.
        """
        mentor_cfg = self._td_cfg.get("mentor", {})
        lookback = mentor_cfg.get("lookback_days", 30)
        max_learnings = mentor_cfg.get("max_learnings_in_prompt", 20)

        mentor_usernames = self._get_mentor_usernames()
        if not mentor_usernames:
            return {"mentors": [], "calls": [], "learnings": [], "charts": []}

        placeholders = ",".join(["%s"] * len(mentor_usernames))
        download_dir = self.config.raw.get(
            "data_management", {}).get("alpha_download_dir", "data/alpha_downloads")

        posts = self.db.fetch_all(f"""
            SELECT ni.id, ni.author, ni.source, ni.source_detail,
                   ni.headline, ni.detail, ni.direction, ni.symbols,
                   ni.ai_analysis, ni.chart_image_url, ni.media_url,
                   ni.media_type, ni.collected_at
            FROM news_items ni
            WHERE ni.author IN ({placeholders})
              AND (ni.symbols LIKE %s OR ni.headline LIKE %s OR ni.detail LIKE %s)
              AND ni.collected_at >= NOW() - INTERVAL %s DAY
            ORDER BY ni.collected_at DESC
            LIMIT 50
        """, (*mentor_usernames, f'%{symbol}%', f'%{symbol}%', f'%{symbol}%', lookback))

        mentors = []
        charts = []
        calls = []

        for p in (posts or []):
            entry = {
                "id": p["id"],
                "author": p.get("author"),
                "source": p.get("source"),
                "headline": p.get("headline"),
                "detail": (p.get("detail") or "")[:2000],
                "direction": p.get("direction"),
                "ai_analysis": (p.get("ai_analysis") or "")[:3000],
                "media_type": p.get("media_type"),
                "collected_at": p["collected_at"].isoformat() if p.get("collected_at") else None,
                "is_mentor": True,
            }
            mentors.append(entry)

            if p.get("direction"):
                calls.append(entry)

            for img_field in ["chart_image_url", "media_url"]:
                img_url = p.get(img_field)
                if img_url:
                    local_path = resolve_chart_image_path(img_url, download_dir)
                    if local_path:
                        charts.append({
                            "path": local_path,
                            "source": p.get("source"),
                            "author": p.get("author"),
                            "analysis_id": p["id"],
                            "is_mentor": True,
                            "description": (
                                f"[MENTOR CHART] {p.get('author', '?')} "
                                f"({p.get('direction') or 'analysis'}) — "
                                f"{p.get('headline', '')[:80]}"
                            ),
                        })

        signals = self.db.fetch_all(f"""
            SELECT ps.id, ps.author, ps.symbol, ps.direction,
                   ps.entry_price, ps.stop_loss, ps.take_profit_1,
                   ps.take_profit_2, ps.take_profit_3, ps.confidence,
                   ps.status, ps.outcome, ps.outcome_pips, ps.parsed_at,
                   ps.raw_text, ps.ai_reasoning, ps.news_item_id
            FROM parsed_signals ps
            WHERE ps.author IN ({placeholders})
              AND ps.symbol = %s
              AND ps.parsed_at >= NOW() - INTERVAL %s DAY
            ORDER BY ps.parsed_at DESC
            LIMIT 20
        """, (*mentor_usernames, symbol, lookback))

        for s in (signals or []):
            commentary = (s.get("raw_text") or "")[:500]
            ai_note = (s.get("ai_reasoning") or "")[:500]
            if not commentary and not ai_note and s.get("news_item_id"):
                try:
                    ni = self.db.fetch_one(
                        "SELECT LEFT(ai_analysis, 1000) as ai_analysis FROM news_items WHERE id = %s",
                        (s["news_item_id"],))
                    if ni and ni.get("ai_analysis"):
                        ai_note = str(ni["ai_analysis"])[:500]
                except Exception:
                    pass
            call = {
                "author": s.get("author"),
                "direction": s.get("direction"),
                "entry_price": float(s["entry_price"]) if s.get("entry_price") else None,
                "stop_loss": float(s["stop_loss"]) if s.get("stop_loss") else None,
                "tp1": float(s["take_profit_1"]) if s.get("take_profit_1") else None,
                "tp2": float(s["take_profit_2"]) if s.get("take_profit_2") else None,
                "tp3": float(s["take_profit_3"]) if s.get("take_profit_3") else None,
                "confidence": float(s["confidence"]) if s.get("confidence") else None,
                "status": s.get("status"),
                "outcome": s.get("outcome"),
                "outcome_pips": float(s["outcome_pips"]) if s.get("outcome_pips") else None,
                "parsed_at": s["parsed_at"].isoformat() if s.get("parsed_at") else None,
                "is_mentor": True,
            }
            if commentary:
                call["raw_text"] = commentary
            if ai_note:
                call["ai_analysis"] = ai_note
            calls.append(call)

        # Mentor historical SL patterns — how do mentors typically place stops?
        sl_history = []
        try:
            mentor_sl_rows = self.db.fetch_all(f"""
                SELECT ps.author, ps.symbol, ps.direction, ps.entry_price,
                       ps.stop_loss, ps.outcome, ps.outcome_pips
                FROM parsed_signals ps
                WHERE ps.author IN ({placeholders})
                  AND ps.entry_price IS NOT NULL AND ps.entry_price > 0
                  AND ps.stop_loss IS NOT NULL AND ps.stop_loss > 0
                  AND ps.parsed_at >= NOW() - INTERVAL 90 DAY
                ORDER BY ps.parsed_at DESC
                LIMIT 100
            """, tuple(mentor_usernames))
            for row in (mentor_sl_rows or []):
                ep = float(row["entry_price"])
                sl = float(row["stop_loss"])
                if ep > 0:
                    dist_pct = round(abs(ep - sl) / ep * 100, 3)
                    sl_history.append({
                        "author": row.get("author"),
                        "symbol": row.get("symbol"),
                        "direction": row.get("direction"),
                        "sl_distance_pct": dist_pct,
                        "outcome": row.get("outcome"),
                    })
        except Exception as e:
            logger.debug(f"[Dossier] Mentor SL history fetch: {e}")

        half_cap = max(max_learnings // 2, 5)

        symbol_learnings = self.db.fetch_all("""
            SELECT ml.insight_title, ml.insight_detail, ml.learning_category,
                   ml.symbol, ml.confidence, up.display_name
            FROM mentor_learnings ml
            JOIN user_profiles up ON ml.user_profile_id = up.id
            WHERE up.is_mentor = 1
              AND ml.symbol = %s
            ORDER BY ml.confidence DESC, ml.created_at DESC
            LIMIT %s
        """, (symbol, half_cap))

        transferable_cats = (
            'trading_style', 'entry_pattern', 'exit_strategy',
            'risk_management', 'session_preference', 'psychology',
        )
        cat_placeholders = ",".join(["%s"] * len(transferable_cats))
        general_learnings = self.db.fetch_all(f"""
            SELECT ml.insight_title, ml.insight_detail, ml.learning_category,
                   ml.symbol, ml.confidence, up.display_name
            FROM mentor_learnings ml
            JOIN user_profiles up ON ml.user_profile_id = up.id
            WHERE up.is_mentor = 1
              AND (ml.symbol IS NULL OR ml.symbol != %s)
              AND ml.learning_category IN ({cat_placeholders})
            ORDER BY ml.confidence DESC, ml.created_at DESC
            LIMIT %s
        """, (symbol, *transferable_cats, half_cap))

        def _to_learning(row, is_general=False):
            return {
                "mentor": row.get("display_name"),
                "category": row.get("learning_category"),
                "title": row.get("insight_title"),
                "detail": row.get("insight_detail"),
                "symbol": row.get("symbol"),
                "confidence": float(row["confidence"]) if row.get("confidence") else None,
                "transferable": is_general,
            }

        learning_list = [_to_learning(l) for l in (symbol_learnings or [])]
        general_list = [_to_learning(l, True) for l in (general_learnings or [])]

        return {
            "mentors": mentors,
            "calls": calls,
            "learnings": learning_list,
            "general_learnings": general_list,
            "charts": charts,
            "sl_history": sl_history,
        }

    def _get_mentor_usernames(self) -> List[str]:
        """Get all usernames linked to mentor profiles across all sources."""
        from db.database import get_mentor_usernames
        return get_mentor_usernames(self.db)

    def _build_dossier_intelligence(self, symbol: str, sections: Dict,
                                     has_mentor_call: bool) -> Optional[str]:
        """Compile dossier intelligence from mentor data, signal context, and DA analyses.
        Stored in dossier_intelligence column so users can see exactly what the dossier
        is based on -- especially for mentor-driven dossiers."""
        parts = []

        # Mentor calls (the most important part)
        mentor_data = sections.get("mentor_intelligence", {})
        calls = mentor_data.get("calls", [])
        mentors = mentor_data.get("mentors", [])

        if calls:
            parts.append("=== MENTOR TRADE SETUPS ===")
            for c in calls[:10]:
                author = c.get("author", "?")
                direction = c.get("direction", "?")
                line = f"Mentor: {author} | Direction: {direction}"
                if c.get("entry_price"):
                    line += f" | Entry: {c['entry_price']}"
                if c.get("stop_loss"):
                    line += f" | SL: {c['stop_loss']}"
                for i in range(1, 7):
                    tp = c.get(f"take_profit_{i}")
                    if tp:
                        line += f" | TP{i}: {tp}"
                parts.append(line)
                if c.get("raw_text"):
                    parts.append(f"  Message: {str(c['raw_text'])[:500]}")
                if c.get("ai_analysis"):
                    parts.append(f"  AI Analysis: {str(c['ai_analysis'])[:500]}")
                parts.append("")

        if mentors and not calls:
            parts.append("=== MENTOR ALPHA (no specific trade call) ===")
            for m in mentors[:5]:
                author = m.get("author", "?")
                headline = m.get("headline", "")
                detail = m.get("detail", "")[:300]
                parts.append(f"Mentor: {author}")
                if headline:
                    parts.append(f"  Headline: {headline}")
                if detail:
                    parts.append(f"  Detail: {detail}")
                if m.get("ai_analysis"):
                    parts.append(f"  AI Analysis: {str(m['ai_analysis'])[:400]}")
                parts.append("")

        # Signal intelligence summary
        sig_intel = sections.get("signal_intelligence", {})
        active_signals = sig_intel.get("active_signals", [])
        if active_signals:
            parts.append("=== ACTIVE SIGNALS FROM PROVIDERS ===")
            for s in active_signals[:8]:
                author = s.get("author", "?")
                direction = s.get("direction", "?")
                entry = s.get("entry_price", "?")
                parts.append(f"  {author}: {direction} @ {entry}")
            parts.append("")

        # DA team analyses summary
        da_list = sections.get("da_analyses", [])
        if da_list:
            parts.append("=== DATA ANALYTICS TEAM ANALYSES ===")
            for da in da_list[:5]:
                source = da.get("source_detail", da.get("source", "?"))
                analysis = str(da.get("ai_analysis", ""))[:400]
                if analysis:
                    parts.append(f"  Source: {source}")
                    parts.append(f"  Analysis: {analysis}")
                    parts.append("")

        if not parts:
            return None

        return "\n".join(parts)

    def _gather_signal_intel(self, symbol: str) -> Dict:
        """Section 4: Per-symbol JTS + active ideas."""
        limit = self._td_cfg.get("top_providers_limit", 10)
        days = self._td_cfg.get("signal_lookback_days", 7)
        providers = get_per_symbol_jts(self.db, symbol, limit)
        ideas = get_active_ideas_for_symbol(self.db, symbol, days)

        formatted_ideas = []
        for idea in ideas:
            formatted_ideas.append({
                "id": idea["id"],
                "author": idea.get("author"),
                "direction": idea.get("direction"),
                "entry_price": float(idea["entry_price"]) if idea.get("entry_price") else None,
                "stop_loss": float(idea["stop_loss"]) if idea.get("stop_loss") else None,
                "tp1": float(idea["take_profit_1"]) if idea.get("take_profit_1") else None,
                "tp2": float(idea["take_profit_2"]) if idea.get("take_profit_2") else None,
                "tp3": float(idea["take_profit_3"]) if idea.get("take_profit_3") else None,
                "confidence": idea.get("confidence"),
                "timeframe": idea.get("timeframe"),
                "rr": float(idea["risk_reward"]) if idea.get("risk_reward") else None,
                "reasoning": idea.get("ai_reasoning"),
                "status": idea.get("status"),
                "parsed_at": idea["parsed_at"].isoformat() if idea.get("parsed_at") else None,
            })

        return {"top_providers": providers, "active_ideas": formatted_ideas}

    def _gather_geopolitical(self, symbol: str) -> Dict:
        """Section 6: Recent geopolitical context from alpha/news.
        Filters for genuine geopolitical events, excludes trade signals."""
        rows = self.db.fetch_all("""
            SELECT headline, detail, ai_analysis, sentiment, collected_at, source
            FROM news_items
            WHERE (category LIKE '%%politi%%' OR category LIKE '%%geo%%'
                   OR headline LIKE '%%war%%' OR headline LIKE '%%sanction%%'
                   OR headline LIKE '%%tariff%%' OR headline LIKE '%%election%%'
                   OR headline LIKE '%%conflict%%' OR headline LIKE '%%tension%%'
                   OR tags LIKE '%%geopolitics%%' OR tags LIKE '%%political%%')
                  AND collected_at >= %s
                  AND category NOT IN ('signal', 'trade', 'alpha')
            ORDER BY collected_at DESC
            LIMIT 30
        """, (_utcnow() - timedelta(days=7),))

        windows = self._categorize_time_windows(rows or [])
        total = len(rows or [])

        severity = "none"
        crisis_kw = ["war", "invasion", "sanction", "tariff hike", "escalat",
                     "nuclear", "military", "emergency"]
        if rows:
            for r in rows:
                hl = (r.get("headline") or "").lower()
                if any(k in hl for k in crisis_kw):
                    severity = "high"
                    break
            if severity == "none" and total > 5:
                severity = "medium"
            elif severity == "none" and total > 0:
                severity = "low"

        rag_insights = self._rag_query_geo_macro(
            symbol, "geopolitical risks sanctions tariffs war elections impact")
        return {"time_windows": windows, "total_items": total,
                "severity": severity, "rag_insights": rag_insights}

    def _gather_macroeconomic(self, symbol: str) -> Dict:
        """Section 7: Recent macroeconomic data from alpha/news.
        Filters for high-impact economic events, excludes trade signals."""
        rows = self.db.fetch_all("""
            SELECT headline, detail, ai_analysis, sentiment, collected_at, source
            FROM news_items
            WHERE (category LIKE '%%macro%%' OR category LIKE '%%econom%%'
                   OR headline LIKE '%%FOMC%%' OR headline LIKE '%%CPI%%'
                   OR headline LIKE '%%PMI%%' OR headline LIKE '%%NFP%%'
                   OR headline LIKE '%%rate%%' OR headline LIKE '%%inflation%%'
                   OR headline LIKE '%%employment%%' OR headline LIKE '%%GDP%%'
                   OR headline LIKE '%%Fed%%' OR headline LIKE '%%central bank%%'
                   OR tags LIKE '%%economics%%' OR tags LIKE '%%macro%%')
                  AND collected_at >= %s
                  AND category NOT IN ('signal', 'trade', 'alpha')
            ORDER BY collected_at DESC
            LIMIT 30
        """, (_utcnow() - timedelta(days=7),))

        windows = self._categorize_time_windows(rows or [])
        total = len(rows or [])

        severity = "none"
        high_impact_kw = ["fomc", "rate decision", "rate hike", "rate cut",
                          "nfp", "non-farm", "cpi", "inflation surprise",
                          "recession", "banking crisis", "sovereign default"]
        if rows:
            for r in rows:
                hl = (r.get("headline") or "").lower()
                if any(k in hl for k in high_impact_kw):
                    severity = "high"
                    break

        rag_insights = self._rag_query_geo_macro(
            symbol, "macroeconomic FOMC CPI NFP interest rates inflation GDP impact")
        return {"time_windows": windows, "total_items": total,
                "severity": severity, "rag_insights": rag_insights}

    def _rag_query_geo_macro(self, symbol: str, query_context: str) -> str:
        """Run a RAG query for geopolitical/macro intelligence on this symbol."""
        try:
            from core.rag_search import RAGSearch
            rag = RAGSearch(self.db)
            results = rag.search(
                f"{symbol} {query_context}",
                collection_names=["feed_items", "alpha_analysis"],
                limit=5)
            if results:
                snippets = []
                for r in results[:5]:
                    text = r.get("text", r.get("content", ""))[:300]
                    if text:
                        snippets.append(text)
                return "\n".join(snippets) if snippets else ""
        except Exception as e:
            logger.debug(f"[Dossier] RAG geo/macro query failed: {e}")
        return ""

    def _categorize_time_windows(self, rows: List[Dict]) -> Dict:
        """Categorize news items into time windows."""
        now = _utcnow()
        windows = {
            "last_1h": [],
            "last_4h": [],
            "last_24h": [],
            "last_3d": [],
            "last_7d": [],
        }
        for r in rows:
            ct = r.get("collected_at")
            if not ct:
                continue
            age = now - ct
            item = {
                "headline": r.get("headline", ""),
                "sentiment": r.get("sentiment"),
                "source": r.get("source"),
                "time": ct.isoformat(),
                "analysis": (r.get("ai_analysis") or "")[:500],
            }
            if age <= timedelta(hours=1):
                windows["last_1h"].append(item)
            elif age <= timedelta(hours=4):
                windows["last_4h"].append(item)
            elif age <= timedelta(hours=24):
                windows["last_24h"].append(item)
            elif age <= timedelta(days=3):
                windows["last_3d"].append(item)
            else:
                windows["last_7d"].append(item)
        return windows

    @staticmethod
    def _extract_quant_score(s1_output) -> Optional[int]:
        """Extract OVERALL_SETUP_SCORE from Stage 1 TA output.

        Looks for the explicit line we asked for in the S1 prompt suffix,
        with fallbacks for common LLM variations. Returns None if no score found.
        """
        if not s1_output or not isinstance(s1_output, str):
            return None
        import re
        patterns = [
            r'OVERALL_SETUP_SCORE\s*[:=]\s*(\d{1,3})',
            r'Overall\s+Setup\s+Score\s*[:=]\s*(\d{1,3})',
            r'Setup\s+Score\s*[:=]\s*(\d{1,3})',
            r'Quant\s+Score\s*[:=]\s*(\d{1,3})',
            r'TA\s+Score\s*[:=]\s*(\d{1,3})',
            r'SCORE\s*[:=]\s*(\d{1,3})\s*/\s*100',
            r'(\d{1,3})\s*/\s*100\s*$',
        ]
        for pat in patterns:
            m = re.search(pat, s1_output, re.IGNORECASE | re.MULTILINE)
            if m:
                val = int(m.group(1))
                if 0 <= val <= 100:
                    return val
        return None

    def _update_symbol_intel(self, symbol: str, dossier: Dict):
        """Write Stage 1 + Stage 2 results back to ``symbol_intel``.

        Called once at the end of ``build_dossier`` so the Scout's verdict
        cache stays current.  Upserts so it works on first-ever build too.
        Uses canonical symbol form to match the Scout's key format.
        """
        from db.market_symbols import SYMBOL_ALIASES, _normalize_crypto_to_usdt
        s = (symbol or "").upper().strip()
        s = SYMBOL_ALIASES.get(s, s)
        canon = _normalize_crypto_to_usdt(s) or s
        symbol = canon
        duo_id = getattr(self, "duo_id", None) or "apex"
        qs = dossier.get("quant_score")
        s2 = dossier.get("stage2_output") or {}
        confidence = s2.get("confidence_score")
        decision = s2.get("trade_decision", "")

        verdict_map = {
            "trade_now": "strong_setup",
            "wait_for_conditions": "moderate_setup",
            "do_not_trade": "no_setup",
        }
        if qs is not None and qs < 35:
            verdict = "gated"
        else:
            verdict = verdict_map.get(decision, "weak_setup")

        price_at = None
        try:
            lp = self.db.fetch_one(
                "SELECT price FROM live_prices WHERE symbol = %s", (symbol,))
            if lp and lp.get("price"):
                price_at = float(lp["price"])
        except Exception:
            pass

        vol_at = None
        try:
            vr = self.db.fetch_one(
                "SELECT AVG(volume) as v FROM candles "
                "WHERE symbol = %s AND timeframe = 'M5' "
                "AND candle_time > DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 HOUR)",
                (symbol,))
            if vr and vr.get("v"):
                vol_at = int(float(vr["v"]))
        except Exception:
            pass

        try:
            self.db.execute("""
                INSERT INTO symbol_intel
                    (symbol, duo_id, last_verdict, last_quant_score,
                     last_confidence, last_decision, last_analyzed_at,
                     price_at_analysis, volume_at_analysis, total_builds,
                     consecutive_skips, skip_reason)
                VALUES (%s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(), %s, %s, 1, 0, NULL)
                ON DUPLICATE KEY UPDATE
                    last_verdict       = VALUES(last_verdict),
                    last_quant_score   = COALESCE(VALUES(last_quant_score), last_quant_score),
                    last_confidence    = COALESCE(VALUES(last_confidence), last_confidence),
                    last_decision      = VALUES(last_decision),
                    last_analyzed_at   = UTC_TIMESTAMP(),
                    price_at_analysis  = VALUES(price_at_analysis),
                    volume_at_analysis = VALUES(volume_at_analysis),
                    total_builds       = total_builds + 1,
                    consecutive_skips  = 0,
                    skip_reason        = NULL
            """, (symbol, duo_id, verdict, qs, confidence, decision,
                  price_at, vol_at))
            logger.debug(f"[Dossier] symbol_intel updated: {symbol}/{duo_id} "
                         f"verdict={verdict} qs={qs} conf={confidence}")
        except Exception as e:
            logger.debug(f"[Dossier] symbol_intel update failed: {e}")

        chart_quality = s2.get("chart_quality")
        if chart_quality and isinstance(chart_quality, (int, float)) and 1 <= chart_quality <= 10:
            try:
                self.db.execute("""
                    UPDATE symbol_intel
                    SET avg_chart_quality = CASE
                            WHEN avg_chart_quality IS NULL THEN %s
                            ELSE ROUND(
                                (avg_chart_quality * COALESCE(chart_quality_samples, 0) + %s)
                                / (COALESCE(chart_quality_samples, 0) + 1), 2)
                        END,
                        chart_quality_samples = COALESCE(chart_quality_samples, 0) + 1
                    WHERE symbol = %s AND duo_id = %s
                """, (chart_quality, chart_quality, symbol, duo_id))
                logger.debug(f"[Dossier] chart_quality={chart_quality} stored for "
                             f"{symbol}/{duo_id}")
            except Exception as e:
                logger.debug(f"[Dossier] chart_quality update failed: {e}")

    def _format_geo_macro_for_stage1(self, data: Dict, label: str) -> str:
        """Format geo/macro data into a concise text block for Stage 1."""
        if not data:
            return ""
        items = []
        for window_name in ("last_1h", "last_4h", "last_24h", "last_3d"):
            window_items = data.get("time_windows", {}).get(window_name, [])
            for item in window_items[:5]:
                headline = item.get("headline", "")
                sentiment = item.get("sentiment", "")
                if headline:
                    items.append(f"  [{window_name}] {headline} (sentiment: {sentiment})")
        rag = data.get("rag_insights", "")
        if not items and not rag:
            return ""
        severity = data.get("severity", "none")
        result = f"Severity: {severity}\n" + "\n".join(items[:15])
        if rag:
            result += f"\n\nRAG Intelligence:\n{rag[:2000]}"
        return result

    # ── Stage 1: Technical Analysis ──────────────────────────────────

    def _run_stage1(self, symbol: str, ohlcv_data: Dict,
                    billnye_text: str = "",
                    geo_data: Dict = None, macro_data: Dict = None) -> Optional[str]:
        """Run Stage 1 TA with OHLCV + BillNye TA + Geo/Macro context."""
        try:
            from core.model_interface import get_model_interface

            ohlcv_text = format_ohlcv_for_prompt(ohlcv_data)
            if not ohlcv_text.strip():
                logger.warning(f"[Dossier] No OHLCV data for Stage 1 TA on {symbol}")
                return None

            s1_prompt = self._load_prompt_from_db("stage1", STAGE1_TA_PROMPT)
            prompt = s1_prompt.format(symbol=symbol, ohlcv_summary=ohlcv_text)

            if billnye_text:
                prompt += (
                    "\n\n## BILLNYE DATA SCIENTIST — COMPUTED INDICATORS\n"
                    + str(billnye_text)[:15000])

            geo_summary = self._format_geo_macro_for_stage1(geo_data, "geopolitical")
            macro_summary = self._format_geo_macro_for_stage1(macro_data, "macroeconomic")
            if geo_summary:
                prompt += f"\n\n## GEOPOLITICAL CONTEXT\n{geo_summary}"
            if macro_summary:
                prompt += f"\n\n## MACROECONOMIC CONTEXT\n{macro_summary}"

            prompt += (
                "\n\n## REQUIRED: At the very end of your analysis, output exactly this line:\n"
                "OVERALL_SETUP_SCORE: <number 0-100>\n"
                "where 0 = no tradable setup exists, 100 = textbook A+ setup with "
                "perfect confluence. Be honest — most setups are 30-60.")

            mcfg = self._resolve_stage_model("stage1")
            model, provider = mcfg["model"], mcfg["provider"]
            self._last_stage1_model = model
            self._last_stage1_tier = mcfg["tier"]

            logger.info(f"[Dossier] Running Stage 1 TA for {symbol} "
                        f"({model} [{mcfg['tier']}], ~{len(prompt)} chars)")

            mi = get_model_interface()
            s1_identity = load_prompt(
                self.db, "stage1_system_identity",
                "You are a technical analyst.", min_length=10,
                duo_id=self.duo_id)
            resp = mi.query_with_model(
                model_id=model, provider=provider,
                role="stage1_ta", system_prompt=s1_identity,
                user_prompt=prompt,
                max_tokens=mcfg["max_tokens"],
                temperature=mcfg["temperature"],
                context="trade_dossier", source="trading_floor",
                duo_id=self.duo_id)

            ta_output = resp.content if resp else ""
            logger.info(f"[Dossier] Stage 1 TA complete for {symbol}: "
                        f"{len(ta_output)} chars")
            return ta_output

        except Exception as e:
            logger.error(f"[Dossier] Stage 1 TA failed for {symbol}: {e}")
            return f"[Stage 1 TA Error: {e}]"

    # ── Stage 2: Premium Model Decision ──────────────────────────────

    def _run_stage2(self, symbol: str, dossier: Dict) -> Optional[Dict]:
        """
        Run Stage 2 decision using a premium model.
        Sends the full dossier including chart images (multi-modal).
        """
        try:
            from core.model_interface import get_model_interface

            dossier_text = self._format_dossier_for_prompt(symbol, dossier)
            mcfg = self._resolve_stage_model("stage2")
            model, provider = mcfg["model"], mcfg["provider"]

            user_content = self._build_stage2_content(
                symbol, dossier_text, dossier.get("chart_images", []),
                supports_vision=mcfg["supports_vision"],
                dossier_id=dossier.get("id"))

            _s2_img_total = len(dossier.get("chart_images", []))
            _s2_img_used = min(_s2_img_total, 15)
            logger.info(f"[Dossier] Running Stage 2 decision for {symbol} "
                        f"({model} [{mcfg['tier']}], {len(dossier_text)} chars, "
                        f"{_s2_img_used} images [of {_s2_img_total} collected])")

            mi = get_model_interface()
            s2_identity = load_prompt(
                self.db, "stage2_system_identity",
                "You are Apex, senior trader.", min_length=10,
                duo_id=self.duo_id)
            resp = mi.query_with_model(
                model_id=model, provider=provider,
                role="stage2_decision", system_prompt=s2_identity,
                user_prompt=user_content,
                max_tokens=mcfg["max_tokens"],
                temperature=mcfg["temperature"],
                context="trade_dossier", source="trading_floor",
                dossier_id=dossier.get("id"),
                duo_id=self.duo_id)

            content = resp.content if resp and resp.success else ""
            if not content and resp:
                logger.error(f"[Dossier] Stage 2 empty response: {resp.error_message}")
                content = ""
            mentor_triggered = dossier.get("_mentor_triggered", False)
            mentor_signal = dossier.get("_mentor_signal")
            parsed = self._parse_stage2_response(content,
                                                  symbol=symbol,
                                                  mentor_triggered=mentor_triggered,
                                                  mentor_signal=mentor_signal)
            parsed["raw_response"] = content
            parsed["model_used"] = model

            logger.info(f"[Dossier] Stage 2 decision for {symbol}: "
                        f"{parsed.get('trade_decision', 'unknown')}")
            return parsed

        except Exception as e:
            logger.error(f"[Dossier] Stage 2 decision failed for {symbol}: {e}")
            return {"trade_decision": "error", "error": str(e)}

    def _build_stage2_content(self, symbol: str, dossier_text: str,
                                chart_images: List[Dict],
                                supports_vision: bool = True,
                                dossier_id: int = None):
        """Build prompt content for Stage 2. Returns string or list for multi-modal."""
        now_utc = _utcnow()
        dubai_hour = (now_utc.hour + 4) % 24
        dubai_time_str = (now_utc + timedelta(hours=4)).strftime('%Y-%m-%d %H:%M')
        day_name = (now_utc + timedelta(hours=4)).strftime('%A')

        # Session boundaries in Dubai time (UTC+4) matching prompt Kill Zone definitions
        if dubai_hour < 4:
            active_session = "Asian Session (range building)"
            next_session = "CBDR Build (14:00 Dubai)"
            session_note = ("Asia range forming — mark the high and low. London and NY will "
                            "sweep one side before the real move. Do NOT trade the range; "
                            "wait for London to establish direction.")
            silver_bullet = None
        elif 4 <= dubai_hour < 10:
            active_session = "CBDR / Pre-London"
            next_session = "London Open Kill Zone (10:00 Dubai)"
            session_note = ("CBDR range may still be building (14:00-17:00 Dubai window). "
                            "Mark Asian range high/low. Prepare for London open manipulation.")
            silver_bullet = None
        elif 10 <= dubai_hour < 11:
            active_session = "London Open Kill Zone (FIRST 60 MIN)"
            next_session = "London Silver Bullet (11:00 Dubai)"
            session_note = ("London just opened. WAIT for the opening range (first 15-30 min). "
                            "The Judas Swing is most likely NOW — watch for a fake directional "
                            "move that sweeps stops then reverses. Do NOT chase the opening candle.")
            silver_bullet = None
        elif 11 <= dubai_hour < 12:
            active_session = "London Kill Zone — SILVER BULLET WINDOW"
            next_session = "London Established"
            session_note = ("SILVER BULLET LONDON ACTIVE (11:00-12:00 Dubai). Look for a "
                            "liquidity sweep followed by an FVG in the reversal direction. "
                            "Enter on FVG retracement. M1-M5 timeframe.")
            silver_bullet = "London (11:00-12:00 Dubai)"
        elif 12 <= dubai_hour < 17:
            active_session = "London Established"
            next_session = "NY Open Kill Zone (17:30 Dubai)"
            session_note = ("London session established. Look for continuation or reversal "
                            "of the London ORB break. Price may hunt Asia session liquidity. "
                            "Prepare NY session levels.")
            silver_bullet = None
        elif 17 <= dubai_hour < 18:
            active_session = "NY Open / London-NY Overlap Kill Zone (FIRST 60 MIN)"
            next_session = "NY AM Silver Bullet (18:00 Dubai)"
            session_note = ("NY just opened (London-NY overlap). WAIT for the NY opening range. "
                            "NY often sweeps London session highs/lows in the first 30 min. "
                            "This is PEAK manipulation — do NOT chase.")
            silver_bullet = None
        elif 18 <= dubai_hour < 19:
            active_session = "NY Kill Zone — SILVER BULLET WINDOW (HIGHEST PROBABILITY)"
            next_session = "NY Established"
            session_note = ("SILVER BULLET NY AM ACTIVE (18:00-19:00 Dubai). This is the "
                            "HIGHEST PROBABILITY Silver Bullet window. Look for liquidity sweep "
                            "into FVG reversal on M1-M5.")
            silver_bullet = "NY AM (18:00-19:00 Dubai) — HIGHEST PROBABILITY"
        elif 19 <= dubai_hour < 20:
            active_session = "NY Open Kill Zone (final hour)"
            next_session = "London Close (20:00 Dubai)"
            session_note = ("NY Kill Zone wrapping up. Distribution phase likely active. "
                            "Look for trend continuation after NY ORB break.")
            silver_bullet = None
        elif 20 <= dubai_hour < 22:
            active_session = "London Close / NY Continuation"
            next_session = "NY PM Silver Bullet (22:00 Dubai)"
            session_note = ("London Close zone — often reversal or profit-taking. "
                            "NY session continues. Watch for end-of-London liquidity grabs.")
            silver_bullet = None
        elif 22 <= dubai_hour < 23:
            active_session = "NY PM — SILVER BULLET WINDOW"
            next_session = "Post-market"
            session_note = ("SILVER BULLET NY PM ACTIVE (22:00-23:00 Dubai). Lower probability "
                            "than NY AM — only valid on strong trending days. M1-M5.")
            silver_bullet = "NY PM (22:00-23:00 Dubai) — lower probability"
        else:
            active_session = "Post-market / Pre-Asia"
            next_session = "Asian Session (00:00 Dubai)"
            session_note = "Low liquidity period. No Kill Zones active. Exercise extra caution."
            silver_bullet = None

        # Day of week bias
        dow_note = ""
        if day_name == "Monday":
            dow_note = "**Day Bias: MONDAY** — Often the manipulation candle. Expect fake moves. Be cautious.\n"
        elif day_name in ("Tuesday", "Wednesday"):
            dow_note = f"**Day Bias: {day_name.upper()}** — Real directional move day. Weekly H/L forms ~70% of the time on Tue/Wed.\n"
        elif day_name == "Thursday":
            dow_note = "**Day Bias: THURSDAY** — Continuation or reversal of Wednesday's move.\n"
        elif day_name == "Friday":
            if dubai_hour >= 16:
                dow_note = "**Day Bias: FRIDAY AFTER 16:00 DUBAI — NO NEW ENTRIES.** No Trade Zone active.\n"
            else:
                dow_note = "**Day Bias: FRIDAY** — Profit-taking day. No new entries after 16:00 Dubai.\n"

        # Fetch PDH/PDL from yesterday's candles if available
        pdh_pdl_note = ""
        try:
            yesterday = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")
            pdhl = self.db.fetch_one(
                "SELECT MAX(high) as pdh, MIN(low) as pdl "
                "FROM candles WHERE symbol=%s AND timeframe='D1' AND DATE(candle_time)=%s",
                (symbol, yesterday)
            )
            if not pdhl or not pdhl.get("pdh"):
                pdhl = self.db.fetch_one(
                    "SELECT MAX(high) as pdh, MIN(low) as pdl "
                    "FROM candles WHERE symbol=%s AND timeframe='H1' AND DATE(candle_time)=%s",
                    (symbol, yesterday)
                )
            if pdhl and pdhl.get("pdh"):
                pdh_pdl_note = (f"\n**Previous Day Levels:**\n"
                               f"- PDH (Previous Day High): {pdhl['pdh']}\n"
                               f"- PDL (Previous Day Low): {pdhl['pdl']}\n"
                               f"- These are PRIMARY liquidity targets. If price sweeps PDH, "
                               f"look for shorts. If price sweeps PDL, look for longs.\n")
        except Exception as e:
            logger.debug(f"[Dossier] PDH/PDL fetch failed: {e}")

        sb_line = f"**Silver Bullet Window:** {silver_bullet}\n" if silver_bullet else ""

        session_context = (f"\n## SESSION CONTEXT\n"
                          f"Current Dubai time (UTC+4): {dubai_time_str}\n"
                          f"Current UTC time: {now_utc.strftime('%Y-%m-%d %H:%M')}\n"
                          f"Day: {day_name}\n"
                          f"Active session: {active_session}\n"
                          f"Next session: {next_session}\n"
                          f"{sb_line}"
                          f"{dow_note}"
                          f"**Session Guidance:** {session_note}\n"
                          f"{pdh_pdl_note}"
                          f"Plan trades around session dynamics — do not enter in the first "
                          f"15 minutes of a session open or 5 minutes before a session close.\n")

        existing_ctx = self._build_existing_dossiers_context(symbol)

        # Inject active trading strategies (CEO-approved, with full rules)
        # and mature, symbol-relevant patterns as advisory context
        strategy_section = ""
        try:
            parts = []

            # 1. CEO-approved strategies with detailed rules (always injected)
            active_strats = self.db.fetch_all(
                "SELECT id, name, structured_rules, tags FROM trading_strategies "
                "WHERE status = 'active'")
            if active_strats:
                strat_lines = []
                for st in active_strats:
                    rules = (st.get("structured_rules") or "")[:8000]
                    strat_lines.append(f"### {st['name']} (ID: {st['id']})\n{rules}")
                parts.append(
                    "## APPROVED TRADING STRATEGIES\n"
                    "These are CEO-approved strategies with detailed rules. "
                    "If this trade setup matches one, include:\n"
                    "STRATEGY_MATCH: {strategy_name} | CONFIDENCE: {0-100}%\n\n"
                    + "\n\n".join(strat_lines))

            # 2. Mature patterns for THIS symbol (advisory, not blocking)
            # Only inject patterns with maturity >= 'maturing' (8+ occurrences)
            sym_patterns = self.db.fetch_all("""
                SELECT pattern_name, pattern_type, timeframe, session,
                       direction, setup_type, lesson, occurrences,
                       win_rate, maturity_score
                FROM trade_patterns
                WHERE status IN ('maturing', 'mature')
                  AND (symbol = %s OR symbol = 'MULTI')
                ORDER BY maturity_score DESC, occurrences DESC
                LIMIT 4
            """, (symbol,))
            if sym_patterns:
                pat_lines = []
                for p in sym_patterns:
                    tag = "WIN" if p["pattern_type"] == "winning" else (
                          "CAUTION" if p["pattern_type"] == "losing" else "NOTE")
                    tf = f" | TF: {p['timeframe']}" if p.get("timeframe") else ""
                    sess = f" | Session: {p['session']}" if p.get("session") else ""
                    pat_lines.append(
                        f"- [{tag}] {p['pattern_name']}: {(p.get('lesson') or '')[:200]} "
                        f"(seen {p['occurrences']}x, WR {p['win_rate']}%{tf}{sess})")
                parts.append(
                    f"## PATTERN INTELLIGENCE FOR {symbol} (advisory)\n"
                    "These are observations from past trades on this symbol. "
                    "Use as supporting context — they do NOT override your analysis.\n"
                    + "\n".join(pat_lines))

            if parts:
                strategy_section = "\n\n" + "\n\n".join(parts) + "\n"
        except Exception as e:
            logger.debug(f"[Dossier] Strategy/pattern fetch for prompt skipped: {e}")

        # Fetch multi-timeframe ATR profile for SL context
        atr_note = ""
        try:
            from services.data_scientist import get_data_scientist
            ds = get_data_scientist(self.db)
            atr_profile = ds.compute_multi_tf_atr(symbol)
            if atr_profile:
                lines = [f"\n**MULTI-TIMEFRAME VOLATILITY PROFILE FOR {symbol}:**"]
                for tf in ("M5", "M15", "H1", "H4"):
                    d = atr_profile.get(tf, {})
                    if d.get("value"):
                        lines.append(
                            f"- {tf} ({d.get('label','')}) ATR(14) = {d['value']:.6f} "
                            f"({d.get('pct_of_price', 0):.3f}% of price, "
                            f"volatility: {d.get('volatility','?')}) "
                            f"| 1.5x = {d.get('sl_floor_1_5x', 0):.6f}")
                    else:
                        lines.append(f"- {tf} ({d.get('label','')}) — {d.get('status', 'no data')}")
                lines.append(
                    "\nThis is REFERENCE DATA — use it to cross-check your SL placement.\n"
                    "A sensible SL should generally be at least 1x ATR(14) from entry on the "
                    "timeframe matching your trade horizon (M15 for intraday, H1 for swing).\n"
                    "But structure (order blocks, liquidity zones, wicks) always takes priority "
                    "over raw ATR numbers. ATR tells you the MINIMUM breathing room the market "
                    "needs; structure tells you WHERE the SL actually belongs.")
                atr_note = "\n".join(lines)
        except Exception:
            pass

        min_sl_pct_val = self._get_min_sl_distance_pct(symbol)
        try:
            acct_row = self.db.fetch_one(
                "SELECT min_sl_pct FROM trading_accounts "
                "WHERE enabled = 1 AND live_trading = 1 "
                "ORDER BY waterfall_priority ASC, id ASC LIMIT 1")
            if acct_row and acct_row.get("min_sl_pct"):
                min_sl_pct_val = max(min_sl_pct_val, float(acct_row["min_sl_pct"]))
        except Exception:
            pass

        sl_guidance = (
            "\n## STOP LOSS PLACEMENT (LIVE TRADING)\n"
            "SL placement is the FIRST decision. R:R is calculated AFTER.\n"
            "NEVER tighten an SL to improve R:R — if the smart SL makes R:R "
            "unacceptable, abandon the trade (do_not_trade).\n"
            f"\n**MINIMUM STOP LOSS: {min_sl_pct_val:.1f}% from entry.** "
            f"Any trade with SL tighter than {min_sl_pct_val:.1f}% from entry "
            "will be REJECTED by the execution engine. You may set a WIDER SL "
            "(2-3% or more is fine and often preferred — deeper SLs survive "
            "liquidity sweeps better). A wider SL reduces max leverage but "
            "dramatically improves survival. Choose the SL that structure demands; "
            f"just ensure it is at least {min_sl_pct_val:.1f}%.\n\n"
            "Your SL must be placed at a STRUCTURAL level — beyond order blocks, "
            "beyond liquidity pools, beyond recent wick clusters. If you see many "
            "wicks at a level, that is where stops are being hunted. Your SL goes "
            "BEYOND that hunting ground.\n"
            + atr_note + "\n")

        # SL crowd intelligence: extract SL levels from dossier text for liquidity awareness
        sl_crowd = ""
        try:
            import re as _re
            sl_matches = _re.findall(
                r'(?:^|\n)\s*-\s*\*?\*?([^:*]+?)\*?\*?\s*:\s*(?:LONG|SHORT|BUY|SELL)\b[^,]*?'
                r'SL\s*=\s*([\d.,]+)',
                dossier_text, _re.IGNORECASE)
            if sl_matches:
                sl_lines = ["\n## SL PLACEMENT INTELLIGENCE (CROWD MAP)"]
                sl_lines.append(
                    "Other traders in your alpha feed placed their stops at these levels. "
                    "If you see CLUSTERING (multiple stops near the same price), that zone "
                    "is a LIQUIDITY TARGET — market makers will hunt those stops. Your SL "
                    "should be BEYOND the cluster, not inside it.\n")
                for author, sl_val in sl_matches:
                    sl_lines.append(f"- {author.strip()}: SL @ {sl_val}")
                sl_crowd = "\n".join(sl_lines) + "\n"
        except Exception:
            pass

        # Experience Library (curated by Mentor): inject few-shot exemplars
        exemplar_section = ""
        exp_enabled = self._get_tuning_param("experience_library_enabled", "true")
        if exp_enabled.lower() == "true":
            try:
                import re as _re2
                dir_hint = None
                if _re2.search(r'\b(bull|long|buy)\b', dossier_text[-5000:], _re2.IGNORECASE):
                    dir_hint = "BUY"
                elif _re2.search(r'\b(bear|short|sell)\b', dossier_text[-5000:], _re2.IGNORECASE):
                    dir_hint = "SELL"
                max_tok = int(self._get_tuning_param("experience_library_max_tokens", "2000"))
                exemplar_section = self._retrieve_few_shot_exemplars(
                    symbol, direction=dir_hint, max_tokens=max_tok)
            except Exception as e:
                logger.debug(f"[Dossier] Few-shot exemplar injection skipped: {e}")

        s2_prompt = self._load_prompt_from_db("stage2", STAGE2_DECISION_PROMPT,
                                               dossier_id=dossier_id)
        prompt_text = s2_prompt.format(
            symbol=symbol,
            dossier_content=dossier_text
        ) + session_context + sl_guidance + sl_crowd + f"\n\n## EXISTING DOSSIER AWARENESS\n{existing_ctx}\n" + strategy_section + exemplar_section

        # Inject Ledger's systemic compliance notes from recent audits
        try:
            compliance_rows = self.db.fetch_all("""
                SELECT id, systemic_recs, completed_at,
                       DATEDIFF(NOW(), completed_at) as age_days
                FROM audit_reports
                WHERE status = 'completed' AND systemic_recs IS NOT NULL
                  AND completed_at >= NOW() - INTERVAL 7 DAY
                ORDER BY completed_at DESC LIMIT 5
            """)
            all_recs = []
            seen = set()
            report_ids = []
            for cr in (compliance_rows or []):
                try:
                    report_ids.append(cr["id"])
                    age_days = cr.get("age_days", 0) or 0
                    age_prefix = "[Recent]" if age_days <= 2 else "[Older]"
                    recs = json.loads(cr["systemic_recs"])
                    for rec in recs:
                        txt = rec.get("recommendation", rec.get("action", str(rec)))[:200]
                        key = txt[:80].lower()
                        if key not in seen:
                            seen.add(key)
                            sev = rec.get("severity", "")
                            inner = f"[{sev}] {txt}" if sev else txt
                            all_recs.append(f"- {age_prefix} {inner}")
                except (json.JSONDecodeError, TypeError):
                    pass
            if all_recs:
                prompt_text += (
                    "\n\n## COMPLIANCE NOTES (from Ledger audits — last 7 days)\n"
                    "These are systemic findings from post-mortem reviews. "
                    "Consider them when making your decision:\n"
                    + "\n".join(all_recs[:5]) + "\n")
                if report_ids:
                    self._pending_cni_report_ids = report_ids
        except Exception as e:
            logger.debug(f"[Dossier] Compliance notes error: {e}")

        # Inject confidence calibration card so the model can self-calibrate
        cal_card = self._get_confidence_calibration_card()
        if cal_card:
            prompt_text += "\n\n" + cal_card

        prompt_text += """

## CRITICAL: OUTPUT FORMATTING RULES (SYSTEM WILL REJECT NON-COMPLIANT RESPONSES)

Your response is parsed by regex. You MUST follow these rules EXACTLY:

1. DO NOT use markdown bold (**text**), italic (*text*), or any special formatting.
   Write plain ASCII text only. No asterisks, no backticks, no HTML.
2. Use these EXACT labels on their own line, with a colon and a space before the value:
   Direction: BUY
   Entry price: 0.02715
   Stop loss: 0.02655
   Take Profit 1: 0.0287
   Take Profit 2: 0.0310
   Take Profit 3: 0.0335
   Overall confidence: 72
3. For confidence, write JUST the number (no % sign needed): Overall confidence: 72
4. Use ## for section headers only. Everything else is plain text.
5. DO NOT wrap values in bold. Write "Overall confidence: 72" NOT "**Overall confidence:** 72%"

WRONG: **Overall confidence:** 68%
WRONG: **Entry price:** $0.02715
CORRECT: Overall confidence: 68
CORRECT: Entry price: 0.02715

6. REQUIRED: Rate the overall chart structure quality on a 1-10 scale.
   This measures how "tradeable" the chart looks — whether it respects levels,
   has clean structure, and shows confluence. Write on its own line:
   Chart quality: 7
   (1 = total chaos/no structure, 10 = textbook setup with perfect confluence)
"""

        include_images = self._td_cfg.get("include_chart_images", True)
        logger.info(f"[Dossier] Stage2 content: include_images={include_images}, "
                    f"chart_count={len(chart_images)}, vision={supports_vision}")
        if not include_images or not chart_images or not supports_vision:
            if chart_images and not supports_vision:
                descs = [f"- {img.get('author','?')} via {img.get('source','?')}: "
                         f"{img.get('description','chart')}"
                         for img in chart_images[:15]]
                prompt_text += ("\n\n## CHART REFERENCES (images not sent -- "
                                "text-only model)\n" + "\n".join(descs) + "\n")
            if not chart_images:
                logger.warning(f"[Dossier] No chart images available for {symbol}")
            return prompt_text

        content_parts = [{"type": "text", "text": prompt_text}]
        loaded_images = 0
        reused_analyses = 0
        freshness_hours = self._td_cfg.get("chart_freshness_hours", 4)

        for img in chart_images[:15]:
            analysis_id = img.get("analysis_id")
            author_label = img.get("author", "analyst")
            source_label = img.get("source", "?")

            # Try to use pre-existing text analysis instead of raw image
            existing_analysis = None
            analysis_age_str = ""
            if analysis_id:
                try:
                    ni = self.db.fetch_one(
                        "SELECT ai_analysis, collected_at FROM news_items WHERE id = %s",
                        (analysis_id,))
                    if ni and ni.get("ai_analysis") and len(ni["ai_analysis"]) > 50:
                        existing_analysis = ni["ai_analysis"]
                        if ni.get("collected_at"):
                            age_hours = (_utcnow() - ni["collected_at"]).total_seconds() / 3600
                            if age_hours > freshness_hours:
                                analysis_age_str = f" [STALE -- analyzed {age_hours:.1f}h ago]"
                            else:
                                analysis_age_str = f" [FRESH -- analyzed {age_hours:.1f}h ago]"
                except Exception:
                    pass

            if existing_analysis:
                reused_analyses += 1
                # Truncate long analyses to keep prompt reasonable
                truncated = existing_analysis[:8000]
                if len(existing_analysis) > 8000:
                    truncated += "\n[... analysis truncated for brevity]"
                content_parts.append({
                    "type": "text",
                    "text": (f"\n[CHART ANALYSIS from {author_label} via {source_label}"
                             f"{analysis_age_str}]\n{truncated}\n")
                })
            else:
                # Fallback: send raw base64 image (new/unanalyzed chart)
                b64 = load_chart_image_as_base64(img.get("path", ""))
                if b64:
                    loaded_images += 1
                    media_type = "image/png"
                    path = img.get("path", "")
                    if ".jpg" in path or ".jpeg" in path:
                        media_type = "image/jpeg"
                    elif ".webp" in path:
                        media_type = "image/webp"

                    content_parts.append({
                        "type": "text",
                        "text": f"\n[NEW CHART from {author_label} via {source_label} -- no prior analysis]\n"
                    })
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{b64.split(',', 1)[-1] if ',' in b64 else b64}"}
                    })
                else:
                    logger.debug(f"[Dossier] Could not load chart image: {img.get('path', '?')}")

        logger.info(f"[Dossier] Stage2 content: {reused_analyses} text analyses reused, "
                    f"{loaded_images} new images as base64 "
                    f"(of {len(chart_images[:15])} total charts)")
        return content_parts

    def _format_dossier_for_prompt(self, symbol: str, dossier: Dict) -> str:
        """Format all dossier sections into a single text block."""
        parts = [f"# TRADE DOSSIER: {symbol}",
                 f"Generated: {dossier.get('created_at', 'now')}\n"]

        sections = dossier.get("sections", {})

        # Section 0: Market Regime (injected from MarketRegime service)
        regime_enabled = self._get_tuning_param("regime_injection_enabled", "true").lower() == "true"
        regime = sections.get("market_regime")
        if regime and regime_enabled:
            r_score = regime.get("score", 0)
            r_label = regime.get("label", "UNKNOWN")
            r_quality = regime.get("data_quality", "unknown")
            comps = regime.get("components", {})
            enrich = regime.get("enrichment", {})
            duration = regime.get("duration", {})
            vol_regime = regime.get("volatility_regime", "NORMAL")
            session = regime.get("session", {})
            parts.append("## 0. MARKET REGIME (Current)")
            parts.append(f"Overall Score: {r_score} / 100 ({r_label})")
            parts.append(f"Data Quality: {r_quality}")
            if duration:
                dur_ticks = duration.get("duration_ticks", 0)
                dur_hours = duration.get("duration_hours", 0)
                prev_regime = duration.get("previous_regime", "UNKNOWN")
                flipped = duration.get("flipped_this_tick", False)
                flip_dir = duration.get("flip_direction")
                parts.append(f"Duration: {dur_ticks} ticks ({dur_hours}h)")
                parts.append(f"Previous Regime: {prev_regime}")
                if flipped and flip_dir:
                    parts.append(f"Recent Flip: {flip_dir}")
            parts.append(f"Volatility Regime: {vol_regime}")
            if session:
                sess_name = session.get("session", "Unknown")
                sess_hours = session.get("hours", "N/A")
                sess_guidance = session.get("guidance", "")
                parts.append(f"Session: {sess_name} ({sess_hours})")
                if sess_guidance:
                    parts.append(f"Session Guidance: {sess_guidance}")
            divergence = regime.get("altcoin_divergence", {})
            if divergence and divergence.get("divergence_detected"):
                div_pct = divergence.get("divergence_pct", 0.0)
                div_reason = divergence.get("reason", "")
                div_trend = divergence.get("altcoin_trend", "")
                parts.append(f"Altcoin Divergence: {div_pct:.0f}% of altcoins diverging")
                parts.append(f"  Altcoin Trend: {div_trend}")
                parts.append(f"  Reason: {div_reason}")
            parts.append(f"  Trend Alignment: {comps.get('trend_alignment', 0):.0f} / 40")
            parts.append(f"  Momentum: {comps.get('momentum', 0):.0f} / 30")
            parts.append(f"  Volatility: {comps.get('volatility', 0):.0f} / 20")
            parts.append(f"  Volume Flow: {comps.get('volume_flow', 0):.0f} / 10")
            if enrich.get("funding_rate") is not None:
                parts.append(f"  Funding Rate: {enrich['funding_rate']:.6f}")
            if enrich.get("long_short_ratio") is not None:
                parts.append(f"  L/S Ratio: {enrich['long_short_ratio']:.3f}")
            if enrich.get("open_interest"):
                oi = enrich["open_interest"]
                parts.append(f"  Open Interest: {oi.get('oi_base', 0):.2f}")
            if r_score <= -50:
                parts.append(">>> CAUTION: Strong bearish regime. BUY trades require exceptional confluence. <<<")
            elif r_score >= 50:
                parts.append(">>> FAVORABLE: Strong bullish regime. SELL trades require exceptional confluence. <<<")
            parts.append("")

        # Section 1: OHLCV summary (not raw data -- Stage 1 already analyzed it)
        ohlcv = sections.get("ohlcv", {})
        parts.append("## 1. OHLCV DATA SUMMARY")
        for tf, tf_data in ohlcv.items():
            if isinstance(tf_data, dict) and tf_data.get("count"):
                parts.append(f"- {tf}: {tf_data['count']} candles, "
                             f"range {tf_data.get('low_of_range')} - {tf_data.get('high_of_range')}, "
                             f"latest close {tf_data.get('latest_close')}")
        parts.append("")

        # Section 2: Stage 1 TA output
        ta = sections.get("technical_analysis") or dossier.get("stage1_output")
        if ta:
            parts.append("## 2. UNBIASED TECHNICAL ANALYSIS (Independent AI)")
            parts.append(str(ta)[:50000])
            parts.append("")

        # Section 3: DA team analyses (new items only; previously-seen get summary)
        da = sections.get("da_analyses", [])
        if da:
            parts.append(f"## 3. DATA ANALYTICS TEAM ANALYSES ({len(da)} NEW reports)")
            for a in da[:15]:
                parts.append(f"### {a.get('author', 'Unknown')} via {a.get('source', '?')} "
                             f"({a.get('collected_at', '?')})")
                if a.get("direction"):
                    parts.append(f"Direction: {a['direction']}")
                if a.get("ai_analysis"):
                    parts.append(a["ai_analysis"][:6000])
                parts.append("")

        da_raw = dossier.get("da_analyses", {})
        seen_summary = da_raw.get("seen_summary", "") if isinstance(da_raw, dict) else ""
        if seen_summary:
            parts.append(f"\n{seen_summary}\n")

        # Section 4: Signal intelligence
        intel = sections.get("signal_intelligence", {})
        providers = intel.get("top_providers", [])
        ideas = intel.get("active_ideas", [])
        if providers:
            parts.append(f"## 4. SIGNAL PROVIDER INTELLIGENCE ({len(providers)} top providers)")
            for p in providers:
                parts.append(f"- {p['author']} (JTS: {p['jts']}, "
                             f"WR: {p['win_rate']}%, "
                             f"Signals: {p['total_signals']}, "
                             f"Avg RR: {p['avg_rr']})")
        if ideas:
            parts.append(f"\n### Active Ideas ({len(ideas)})")
            for idea in ideas[:10]:
                parts.append(f"- {idea.get('author', '?')}: {idea.get('direction', '?')} "
                             f"entry={idea.get('entry_price')}, "
                             f"SL={idea.get('stop_loss')}, "
                             f"TP1={idea.get('tp1')}, "
                             f"conf={idea.get('confidence')}, "
                             f"RR={idea.get('rr')}")
                # Include chart analysis from the signal provider's news_item
                idea_analysis = idea.get("ai_analysis")
                if not idea_analysis and idea.get("news_item_id"):
                    try:
                        ni = self.db.fetch_one(
                            "SELECT ai_analysis FROM news_items WHERE id = %s",
                            (idea["news_item_id"],))
                        if ni:
                            idea_analysis = ni.get("ai_analysis")
                    except Exception:
                        pass
                if idea_analysis and len(str(idea_analysis)) > 50:
                    parts.append(f"  Chart Analysis: {str(idea_analysis)[:5000]}")
        parts.append("")

        # Section 5: Geopolitical
        geo = sections.get("geopolitical", {})
        if geo and geo.get("total_items", 0) > 0:
            parts.append(f"## 5. GEOPOLITICAL SNAPSHOT ({geo['total_items']} items)")
            for window_name, items in geo.get("time_windows", {}).items():
                if items:
                    parts.append(f"### {window_name.replace('_', ' ').title()} ({len(items)} items)")
                    for item in items[:5]:
                        parts.append(f"  - [{item.get('sentiment', '?')}] {item.get('headline', '')}")
                        if item.get("analysis"):
                            parts.append(f"    Analysis: {item['analysis'][:200]}")
            parts.append("")

        # Section 6: Macroeconomic
        macro = sections.get("macroeconomic", {})
        if macro and macro.get("total_items", 0) > 0:
            parts.append(f"## 6. MACROECONOMIC SNAPSHOT ({macro['total_items']} items)")
            for window_name, items in macro.get("time_windows", {}).items():
                if items:
                    parts.append(f"### {window_name.replace('_', ' ').title()} ({len(items)} items)")
                    for item in items[:5]:
                        parts.append(f"  - [{item.get('sentiment', '?')}] {item.get('headline', '')}")
                        if item.get("analysis"):
                            parts.append(f"    Analysis: {item['analysis'][:200]}")
            parts.append("")

        # Section MENTOR: Mentor Intelligence (PREMIUM — highest weighting)
        mentor = sections.get("mentor_intelligence", {})
        mentor_posts = mentor.get("mentors", [])
        mentor_calls = mentor.get("calls", [])
        mentor_learnings = mentor.get("learnings", [])
        if mentor_posts or mentor_calls or mentor_learnings:
            parts.append("## ⭐ MENTOR INTELLIGENCE (PREMIUM — HIGHEST WEIGHTING)")
            parts.append("The following comes from designated MENTOR traders — professional "
                         "traders with proven million-dollar track records. Their analysis "
                         "carries MORE weight than any other source.\n")

            if mentor_calls:
                parts.append(f"### 🎯 Mentor Trade Calls ({len(mentor_calls)})")
                for c in mentor_calls[:10]:
                    line = f"- **{c.get('author', '?')}**: {c.get('direction', '?')}"
                    if c.get("entry_price"):
                        line += f" entry={c['entry_price']}"
                    if c.get("stop_loss"):
                        line += f", SL={c['stop_loss']}"
                    if c.get("tp1"):
                        line += f", TP1={c['tp1']}"
                    if c.get("tp2"):
                        line += f", TP2={c['tp2']}"
                    if c.get("outcome"):
                        line += f" [outcome: {c['outcome']}"
                        if c.get("outcome_pips"):
                            line += f" {c['outcome_pips']}%"
                        line += "]"
                    parts.append(line)
                    if c.get("raw_text"):
                        parts.append(f"  Mentor said: \"{c['raw_text'][:300]}\"")
                    if c.get("ai_analysis"):
                        parts.append(f"  AI analysis: {c['ai_analysis'][:400]}")
                parts.append("")

            if mentor_posts:
                parts.append(f"### 📝 Mentor Analysis & Posts ({len(mentor_posts)})")
                for p in mentor_posts[:15]:
                    parts.append(f"#### {p.get('author', '?')} "
                                 f"({p.get('collected_at', '?')})")
                    if p.get("headline"):
                        parts.append(f"Post: {p['headline']}")
                    if p.get("detail"):
                        parts.append(p["detail"][:4000])
                    if p.get("ai_analysis"):
                        parts.append(f"AI Analysis: {p['ai_analysis'][:5000]}")
                    if p.get("media_type"):
                        parts.append(f"[Contains {p['media_type']} content]")
                    parts.append("")

            if mentor_learnings:
                parts.append(f"### 🧠 Learned Patterns from Mentors — {symbol} ({len(mentor_learnings)})")
                for l in mentor_learnings[:15]:
                    parts.append(f"- [{l.get('category', 'general')}] "
                                 f"**{l.get('mentor', '?')}**: {l.get('title', '')} "
                                 f"— {l.get('detail', '')[:800]}")
                parts.append("")

            general_learnings = mentor.get("general_learnings", [])
            if general_learnings:
                parts.append(f"### 🌍 General Trading Patterns (learned from all markets) ({len(general_learnings)})")
                parts.append("These are transferable trading style insights from mentors across "
                             "crypto, forex, and indices — applicable to any market.\n")
                for l in general_learnings[:15]:
                    sym_tag = f" [{l['symbol']}]" if l.get("symbol") else ""
                    parts.append(f"- [{l.get('category', 'general')}] "
                                 f"**{l.get('mentor', '?')}**{sym_tag}: {l.get('title', '')} "
                                 f"— {l.get('detail', '')[:800]}")
                parts.append("")

        # Section MENTOR SL HISTORY: Recent mentor trade examples with SL context
        mentor_sl_hist = (mentor.get("sl_history", []) if mentor_posts
                          or mentor_calls else [])
        if mentor_sl_hist:
            parts.append(f"### 📊 Mentor Recent Trades with SL (last 90 days, {len(mentor_sl_hist)} trades)")
            parts.append(
                "IMPORTANT: Every stop loss below was placed for a UNIQUE structural "
                "reason specific to that trade's chart at that moment. Do NOT average "
                "these, do NOT derive a formula, do NOT treat them as targets. They are "
                "reference examples only — observe where mentors placed stops relative "
                "to their entries, but YOUR SL must be determined by the current chart "
                "structure, order blocks, and liquidity zones for THIS trade.\n")
            for h in mentor_sl_hist[:30]:
                outcome_tag = f" [{h['outcome']}]" if h.get("outcome") else ""
                parts.append(
                    f"- {h.get('author','?')} | {h.get('symbol','?')} "
                    f"{h.get('direction','?')} | SL was {h['sl_distance_pct']}% "
                    f"from entry{outcome_tag}")
            parts.append("")

        # Section 7: Historical performance (from trade_dossiers)
        perf = sections.get("historical_performance", {})
        if perf and perf.get("total", 0) > 0:
            parts.append(f"## 7. HISTORICAL AI PERFORMANCE ON {symbol}")
            parts.append(f"Total closed dossiers: {perf['total']}, "
                         f"Wins: {perf['wins']}, Losses: {perf['losses']}, "
                         f"Win rate: {perf['win_rate']}%, "
                         f"Avg P&L: ${perf.get('avg_pnl_usd', 0)}, "
                         f"Total P&L: ${perf.get('total_pnl_usd', 0)}")
            for t in perf.get("trades", [])[:10]:
                lev_str = f", lev={t['leverage']}x" if t.get("leverage") else ""
                mentor_str = f" (mentor: {t['mentor']})" if t.get("mentor") else ""
                parts.append(f"  - #{t.get('dossier_id','?')} {t.get('direction', '?')}: "
                             f"${t.get('pnl_usd', 0)} ({t.get('pnl_pct', 0)}%), "
                             f"conf={t.get('confidence')}, "
                             f"status={t.get('status')}{lev_str}{mentor_str}")
            parts.append("")

        # Section 8: Trade Lessons & History (Symbol Knowledge Bank)
        lessons = sections.get("symbol_lessons", {})
        if lessons and lessons.get("total_lessons", 0) > 0:
            parts.append(f"## 8. TRADE LESSONS & HISTORY FOR {symbol}")
            parts.append(f"Total lessons available: {lessons['total_lessons']}\n")

            win_list = lessons.get("wins", [])
            if win_list:
                parts.append(f"### RECENT WINS ({len(win_list)}) — What Worked")
                for w in win_list:
                    lev = f", {w['leverage']}x lev" if w.get("leverage") else ""
                    mentor = f" (mentor: {w['mentor']})" if w.get("mentor") else ""
                    parts.append(
                        f"  Dossier #{w.get('dossier_id','?')} {w.get('direction','?')}: "
                        f"entry={w.get('entry','?')}, P&L=${w.get('pnl',0)} "
                        f"({w.get('pnl_pct',0)}%){lev}{mentor}")
                    if w.get("lesson"):
                        parts.append(f"    Lesson: {w['lesson']}")
                    if w.get("what_worked"):
                        parts.append(f"    What worked: {w['what_worked']}")
                    if w.get("root_cause"):
                        parts.append(f"    Root cause of success: {w['root_cause']}")
                parts.append("")

            loss_list = lessons.get("losses", [])
            if loss_list:
                parts.append(f"### RECENT LOSSES ({len(loss_list)}) — What Failed")
                for lo in loss_list:
                    lev = f", {lo['leverage']}x lev" if lo.get("leverage") else ""
                    mentor = f" (mentor: {lo['mentor']})" if lo.get("mentor") else ""
                    parts.append(
                        f"  Dossier #{lo.get('dossier_id','?')} {lo.get('direction','?')}: "
                        f"entry={lo.get('entry','?')}, P&L=${lo.get('pnl',0)} "
                        f"({lo.get('pnl_pct',0)}%){lev}{mentor}")
                    if lo.get("lesson"):
                        parts.append(f"    Lesson: {lo['lesson']}")
                    if lo.get("what_failed"):
                        parts.append(f"    What failed: {lo['what_failed']}")
                    if lo.get("root_cause"):
                        parts.append(f"    Root cause of failure: {lo['root_cause']}")
                    if lo.get("optimal_trade"):
                        parts.append(f"    What should have been done: {lo['optimal_trade']}")
                parts.append("")

            # RAG-retrieved additional context
            rag_ctx = lessons.get("rag_context", [])
            if rag_ctx:
                parts.append(f"### ADDITIONAL CONTEXT (semantic retrieval, {len(rag_ctx)} items)")
                for rc in rag_ctx:
                    parts.append(f"  [{rc.get('type','?')}] {rc.get('text','')}")
                parts.append("")

            # Mentor comparison lessons (what mentor did vs what Apex did)
            mentor_cmp = lessons.get("mentor_comparisons", [])
            if mentor_cmp:
                parts.append(f"### MENTOR VS APEX COMPARISONS ({len(mentor_cmp)} recent)")
                parts.append("Learn from these: where did mentors outperform you, "
                             "and where did you outperform them?")
                for mc in mentor_cmp:
                    parts.append(f"  {mc.get('lesson', '')}")
                parts.append("")

            parts.append("### SELF-ASSESSMENT REQUIRED")
            parts.append("Based on the above lessons, you MUST address in your analysis:")
            parts.append("1. Does this proposed trade repeat any pattern from recent LOSSES?")
            parts.append("2. Does this trade align with patterns from recent WINS?")
            parts.append("3. Which specific lesson from above is most relevant to this trade?")
            parts.append("4. Rate your confidence that prior lessons are being applied: "
                         "HIGH / MEDIUM / LOW")
            parts.append("5. If this trade fails, what is the most likely reason based on "
                         "historical patterns?")
            parts.append("")

        # Section 8b: Shadow Trade Hindsight (do_not_trade counterfactual)
        shadow_data = sections.get("shadow_lessons", {})
        shadow_items = shadow_data.get("shadows", [])
        if shadow_items:
            parts.append(f"## 8b. REJECTED TRADE HINDSIGHT — What You Missed (and Dodged)")
            parts.append("These are trades you REJECTED with do_not_trade. BillNye tracked "
                         "what actually happened. Learn from the ones you got wrong AND "
                         "the ones you got right.\n")

            for sh in shadow_items:
                outcome_label = ("MISSED OPPORTUNITY" if sh["status"] == "shadow_won"
                                 else "GOOD REJECTION")
                sym_label = sh["symbol"]
                if sym_label != symbol:
                    sym_label += f" (same asset class)"

                parts.append(f"### SHADOW #{sh['id']} — {sym_label} "
                             f"{sh.get('direction','?')} ({outcome_label})")
                parts.append(f"  Rejected {sh.get('age_days', '?')} days ago at "
                             f"{sh.get('confidence', '?')}% confidence")
                parts.append(f"  Setup: Entry={sh.get('entry','?')}, "
                             f"SL={sh.get('sl','?')}, TP1={sh.get('tp1','?')}")
                parts.append(f"  Your concern: \"{sh.get('rationale', 'no rationale')[:500]}\"")
                parts.append(f"  What happened: {sh.get('exit_reason','?')} "
                             f"— counterfactual P&L: {sh.get('pnl_pct', 0):+.2f}%")
                if sh.get("lesson"):
                    parts.append(f"  **Lesson:** {sh['lesson']}")
                parts.append("")

            # Confidence band analysis
            band_stats = shadow_data.get("band_stats", [])
            if band_stats:
                parts.append("### CONFIDENCE BAND PERFORMANCE (All Rejected Trades)")
                parts.append("How your rejections performed by confidence level:\n")
                for b in band_stats:
                    wr = b.get("win_rate", 0)
                    flag = " <-- YOU'RE LEAVING MONEY HERE" if wr >= 60 else ""
                    parts.append(
                        f"  {b['range']}: {b['total']} rejected, "
                        f"{b['wins']} would-have-won ({wr:.0f}% win rate), "
                        f"avg P&L {b.get('avg_pnl_pct', 0):+.2f}%{flag}")
                parts.append("")

            # Aggregate summary
            summary = shadow_data.get("summary", {})
            if summary.get("total_evaluated", 0) > 0:
                total = summary["total_evaluated"]
                won = summary.get("would_have_won", 0)
                lost = summary.get("would_have_lost", 0)
                parts.append(f"### SHADOW TRADE SUMMARY (Last "
                             f"{shadow_data.get('band_days', 14)} days)")
                parts.append(f"  Total rejected & evaluated: {total}")
                parts.append(f"  Would have won: {won} "
                             f"({round(won/total*100) if total else 0}%)")
                parts.append(f"  Good rejections: {lost} "
                             f"({round(lost/total*100) if total else 0}%)")
                parts.append(f"  Expired (entry never hit): "
                             f"{summary.get('expired', 0)}")
                if won > lost:
                    parts.append(f"  ** You are rejecting MORE winners than losers. "
                                 f"Consider being more aggressive with similar setups. "
                                 f"This is PAPER TRADING — use it to calibrate. **")
                parts.append("")

            parts.append("### SHADOW SELF-CHECK")
            parts.append("Before deciding do_not_trade, ask yourself:")
            parts.append("1. Does this setup match any MISSED OPPORTUNITY patterns above?")
            parts.append("2. Is my concern the same type that was INVALIDATED previously?")
            parts.append("3. What confidence band is this trade in, and what's the "
                         "historical win rate for rejections at that level?")
            parts.append("4. Remember: we are PAPER TRADING. An educated, calculated "
                         "risk teaches us more than another rejection.")
            parts.append("")

        # Section 9: Data Scientist TA-Lib analysis
        ds_text = sections.get("data_scientist_text", "")
        if ds_text:
            parts.append(ds_text)
            parts.append("")

        # Section 10: Market companion data (VIX, correlations, calendar, crypto)
        comp_text = sections.get("companion_text", "")
        if comp_text:
            parts.append(comp_text)
            parts.append("")

        # Section 10.5: Manus Deep Market Intelligence
        parts.extend(self._build_manus_intel_section(symbol))

        # Section 11: Indicator manifest (what the Data Scientist can compute)
        try:
            from services.data_scientist import get_indicator_manifest_text
            parts.append(get_indicator_manifest_text())
            parts.append("")
        except Exception:
            pass

        max_chars = self._td_cfg.get("max_dossier_chars", 300000)
        full_text = "\n".join(parts)
        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n\n[DOSSIER TRUNCATED]"
        return full_text

    def _build_manus_intel_section(self, symbol: str) -> list:
        """Build Section 10.5: Manus Deep Market Intelligence for the dossier.

        Queries market_regime_intel and symbol_intel for Manus data.
        Returns empty list if Manus intel is disabled or too stale.
        """
        try:
            shadow_cfg = {}
            try:
                rows = self.db.fetch_all(
                    "SELECT config_key, config_value FROM shadow_config "
                    "WHERE config_key IN ('manus_intel_enabled', 'manus_intel_max_age_minutes')")
                shadow_cfg = {r["config_key"]: r["config_value"] for r in (rows or [])}
            except Exception:
                pass

            manus_enabled = shadow_cfg.get("manus_intel_enabled", "false")
            if str(manus_enabled).lower() != "true":
                return []

            max_age = int(shadow_cfg.get("manus_intel_max_age_minutes", 120))

            mri = self.db.fetch_one(
                "SELECT * FROM market_regime_intel "
                "WHERE timestamp >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s MINUTE) "
                "ORDER BY timestamp DESC LIMIT 1", (max_age,))

            if not mri:
                return []

            lines = ["## 10.5 MANUS DEEP MARKET INTELLIGENCE",
                     f"Extracted at: {mri.get('timestamp')}",
                     ""]

            lines.append("### Overall Market Regime Assessment")
            assessment = mri.get("manus_regime_assessment") or "Not available"
            lines.append(assessment)
            lines.append("")

            avg_rsi = mri.get("average_crypto_rsi")
            pct_ob = mri.get("percent_overbought")
            pct_os = mri.get("percent_oversold")
            fr = mri.get("btc_oi_weighted_funding_rate")
            hl_long = mri.get("hyperliquid_long_traders")
            hl_short = mri.get("hyperliquid_short_traders")
            ls_ratio = mri.get("hyperliquid_ls_ratio")
            liq_bias = mri.get("btc_liquidation_cluster_bias")

            lines.append("### Crowd Positioning")
            if avg_rsi is not None:
                lines.append(f"- Average Crypto RSI: {float(avg_rsi):.1f}")
            if pct_ob is not None:
                lines.append(f"- % Coins Overbought (RSI>70): {float(pct_ob):.1f}%")
            if pct_os is not None:
                lines.append(f"- % Coins Oversold (RSI<30): {float(pct_os):.1f}%")
            if fr is not None:
                lines.append(f"- BTC OI-Weighted Funding Rate: {float(fr):.6f}")
            if hl_long and hl_short and ls_ratio is not None:
                lines.append(f"- HyperLiquid Traders: {hl_long} long / "
                             f"{hl_short} short (L/S {float(ls_ratio):.2f})")
            if liq_bias:
                lines.append(f"- BTC Liquidation Cluster Bias: {liq_bias.upper()}")
            lines.append("")

            # Alerts
            alerts = []
            if avg_rsi is not None:
                if float(avg_rsi) > 70:
                    alerts.append("OVERBOUGHT MARKET: Avg RSI > 70")
                elif float(avg_rsi) < 30:
                    alerts.append("OVERSOLD MARKET: Avg RSI < 30")
            if ls_ratio is not None:
                _ls = float(ls_ratio)
                if _ls > 3.0:
                    alerts.append(f"EXTREME LONG CROWDING: L/S ratio {_ls:.2f}")
                elif _ls < 0.33:
                    alerts.append(f"EXTREME SHORT CROWDING: L/S ratio {_ls:.2f}")
            if fr is not None and abs(float(fr)) > 0.05:
                alerts.append(f"EXTREME FUNDING: {float(fr):.6f}")
            if alerts:
                lines.append("### ⚠ ALERTS")
                for a in alerts:
                    lines.append(f"- {a}")
                lines.append("")

            # Per-coin Manus data for this symbol
            base_sym = symbol.replace("USDT", "")
            si = self.db.fetch_one(
                "SELECT manus_rsi_1h, manus_rsi_4h, manus_rsi_24h, "
                "manus_funding_rate_binance, manus_funding_rate_bybit, "
                "manus_oi_change_24h, manus_direction, manus_confidence, "
                "manus_reasoning, llm_consensus "
                "FROM symbol_intel WHERE symbol = %s AND duo_id = '_grok_setup'",
                (symbol,))

            if si:
                lines.append(f"### {symbol} Manus Per-Coin Intelligence")
                if si.get("manus_rsi_1h") is not None:
                    lines.append(f"- RSI: 1h={si['manus_rsi_1h']}, "
                                 f"4h={si.get('manus_rsi_4h')}, "
                                 f"24h={si.get('manus_rsi_24h')}")
                if si.get("manus_funding_rate_binance") is not None:
                    lines.append(f"- Funding: Binance={si['manus_funding_rate_binance']}, "
                                 f"Bybit={si.get('manus_funding_rate_bybit')}")
                if si.get("manus_oi_change_24h") is not None:
                    lines.append(f"- 24h OI Change: {si['manus_oi_change_24h']}%")
                if si.get("manus_direction"):
                    lines.append(f"- Manus View: {si['manus_direction']} "
                                 f"(conf: {si.get('manus_confidence')}%)")
                if si.get("manus_reasoning"):
                    lines.append(f"- Reasoning: {si['manus_reasoning']}")
                consensus = si.get("llm_consensus")
                if consensus:
                    lines.append("- ✓ LLM CONSENSUS: Grok and Manus AGREE on direction")
                lines.append("")

            return lines

        except Exception as e:
            logger.debug(f"[Dossier] Manus intel section build failed: {e}")
            return []

    def _get_min_rr(self) -> float:
        """Read minimum risk:reward ratio from trade_settings config.
        Uses 'min_acceptable_rr' first, falls back to legacy 'no_tp_default_rr',
        then to 2.0 default. Mentor trades get a soft warning (see caller)."""
        try:
            accts = self.config.raw.get("accounts", []) if self.config else []
            rs = accts[0].get("risk_settings", {}) if accts else {}
            return float(rs.get("min_acceptable_rr",
                                rs.get("no_tp_default_rr", 2.0)))
        except Exception:
            return 2.0

    def _store_rr_rejection_lesson(self, symbol: str, result: dict,
                                     rr: float, min_rr: float,
                                     risk: float, reward: float,
                                     entry: float, sl: float, tp1: float):
        """Store low-R:R rejection as a trade_lesson so Ledger can analyze
        whether SL was too wide, TP too close, or setup genuinely poor."""
        try:
            direction = result.get("direction", "?")
            sl_dist_pct = abs(entry - sl) / entry * 100 if entry else 0
            tp_dist_pct = abs(tp1 - entry) / entry * 100 if entry else 0
            detail = (
                f"R:R rejection: {rr}:1 (min {min_rr}:1). "
                f"Entry={entry}, SL={sl} ({sl_dist_pct:.2f}% away), "
                f"TP1={tp1} ({tp_dist_pct:.2f}% away). "
                f"Direction={direction}. Risk={risk:.6g}, Reward={reward:.6g}. "
                f"REVIEW: Is SL too wide (could use tighter structure)? "
                f"Is TP1 too conservative (swing high/ATR extension available)? "
                f"Or is setup genuinely low-reward?"
            )
            hypothesis = result.get("stage2_hypothesis", "")
            if isinstance(hypothesis, str) and len(hypothesis) > 20:
                detail += f"\nHypothesis: {hypothesis[:500]}"

            self.db.execute("""
                INSERT INTO trade_lessons
                (dossier_id, symbol, direction, outcome, category,
                 title, detail, mentor, lesson_source, created_at)
                VALUES (%s, %s, %s, 'rr_rejected', 'risk_management',
                        %s, %s, %s, 'system_rr_gate', NOW())
            """, (
                result.get("dossier_id"),
                symbol,
                direction,
                f"R:R {rr}:1 rejected ({symbol} {direction})",
                detail,
                result.get("mentor_source", ""),
            ))
            logger.info(f"[Dossier] Stored R:R rejection lesson for {symbol} "
                        f"({rr}:1) — queued for Ledger review")
        except Exception as e:
            logger.debug(f"[Dossier] Could not store R:R rejection lesson: {e}")

    def _get_min_sl_distance_pct(self, symbol: str) -> float:
        """Minimum SL distance as % from entry, by asset class.

        Prevents liquidity sweeps from hitting tight SLs. These are
        floor values — ATR-based widening may increase them further.
        Configurable via trade_decision.min_sl_pct in config.json.
        """
        try:
            td_cfg = self.config.raw.get("trade_decision", {}) if self.config else {}
            custom = td_cfg.get("min_sl_pct")
            if custom:
                return float(custom)
        except Exception:
            pass

        # Lookup asset class from market_symbols
        asset_class = "cryptocurrency"
        try:
            row = self.db.fetch_one(
                "SELECT asset_class FROM market_symbols WHERE symbol = %s", (symbol,))
            if row and row.get("asset_class"):
                asset_class = row["asset_class"].lower()
        except Exception:
            pass

        # Floor percentages per asset class (beyond order block + buffer)
        defaults = {
            "cryptocurrency": 0.8,   # crypto: min 0.8% from entry
            "meme":           1.5,   # memes are volatile, need more room
            "commodity":      0.4,
            "forex":          0.3,
            "stock":          0.5,
            "index":          0.3,
        }
        return defaults.get(asset_class, 0.8)

    def _parse_stage2_response(self, raw: str,
                                  symbol: str = "",
                                  mentor_triggered: bool = False,
                                  mentor_signal: Optional[Dict] = None) -> Dict:
        """Parse the premium model's response into structured fields."""
        import re
        result = {
            "trade_decision": "do_not_trade",
            "direction": None,
            "entry_price": None,
            "stop_loss": None,
            "take_profit_1": None,
            "take_profit_2": None,
            "take_profit_3": None,
            "confidence_score": None,
            "chart_quality": None,
            "conditions": [],
            "invalidations": [],
            "rationale": "",
            "time_horizon": None,
            "risk_reward": None,
            "limit_order_guidance": None,
            "dormant_tracking": False,
        }

        raw_lower = raw.lower()
        # Check for "trade now" while rejecting negated forms
        _neg = r"(?:not|don'?t|never|no|avoid|against|wouldn'?t|shouldn'?t)\s+"
        has_trade_now = re.search(r'\btrade[_ ]now\b', raw_lower)
        negated_trade = re.search(rf'\b{_neg}trade[_ ]now\b', raw_lower)
        if has_trade_now and not negated_trade:
            result["trade_decision"] = "trade_now"
        elif "trade_decision" not in result or result["trade_decision"] == "do_not_trade":
            if re.search(r'\bwait[_ ]for[_ ]conditions\b', raw_lower):
                result["trade_decision"] = "wait_for_conditions"
            elif re.search(r'\bdo[_ ]not[_ ]trade\b', raw_lower):
                result["trade_decision"] = "do_not_trade"

        # Direction: look for labelled fields, not bare substrings
        dir_buy = re.search(r'direction[\s:*]*buy\b', raw_lower)
        dir_sell = re.search(r'direction[\s:*]*sell\b', raw_lower)
        if dir_buy and not dir_sell:
            result["direction"] = "BUY"
        elif dir_sell and not dir_buy:
            result["direction"] = "SELL"

        # Parse entry/SL/TP prices (handles comma-separated thousands like 69,450)
        for label, field in [("entry price", "entry_price"), ("entry", "entry_price"),
                             ("stop loss", "stop_loss"),
                             ("take profit 1", "take_profit_1"), ("tp1", "take_profit_1"),
                             ("take profit 2", "take_profit_2"), ("tp2", "take_profit_2"),
                             ("take profit 3", "take_profit_3"), ("tp3", "take_profit_3")]:
            m = re.search(
                rf'{label}[:\s]*\*?\*?\s*\$?\s*([\d,]+\.?\d*)',
                raw, re.IGNORECASE)
            if m and not result[field]:
                try:
                    result[field] = float(m.group(1).replace(",", ""))
                except ValueError:
                    pass

        # Fallback: if entry_price missing, try "entry zone" or "enter at/around/near"
        if not result["entry_price"]:
            fallbacks = [
                r'entry\s+zone[:\s]*([\d,]+\.?\d*)',
                r'enter\s+(?:at|around|near)\s+([\d,]+\.?\d*)',
                r'limit\s+order\s+(?:at|@)\s+([\d,]+\.?\d*)',
            ]
            for pattern in fallbacks:
                m = re.search(pattern, raw, re.IGNORECASE)
                if m:
                    try:
                        result["entry_price"] = float(m.group(1).replace(",", ""))
                        break
                    except ValueError:
                        pass

        # Parse conditions
        cond_blocks = re.findall(
            r'CONDITION\s*\[?(\d+)\]?\s*:\s*(.*?)(?=CONDITION\s*\[?\d|INVALIDATION|##|\Z)',
            raw, re.IGNORECASE | re.DOTALL
        )
        for cid, block in cond_blocks:
            desc_match = re.match(r'(.*?)(?:TYPE:|$)', block, re.DOTALL)
            desc = desc_match.group(1).strip() if desc_match else block.strip()
            type_match = re.search(r'TYPE:\s*(\S+)', block, re.IGNORECASE)
            meas_match = re.search(r'MEASUREMENT:\s*(.*?)(?:WEIGHT:|CURRENT|$)', block, re.IGNORECASE | re.DOTALL)
            weight_match = re.search(r'WEIGHT:\s*(\d+)', block, re.IGNORECASE)
            status_match = re.search(r'CURRENT_STATUS:\s*(\S+)', block, re.IGNORECASE)

            if desc and len(desc) > 5:
                result["conditions"].append({
                    "id": int(cid),
                    "description": desc[:500],
                    "type": type_match.group(1).strip() if type_match else "unknown",
                    "measurement": meas_match.group(1).strip()[:300] if meas_match else "",
                    "weight": int(weight_match.group(1)) if weight_match else 5,
                    "status": (status_match.group(1).strip().lower() if status_match else "not_met"),
                    "met_at": None,
                })

        # Parse invalidation criteria
        inv_blocks = re.findall(
            r'INVALIDATION\s*\[?(\d+)\]?\s*:\s*(.*?)(?=INVALIDATION\s*\[?\d|DORMANT|##|\Z)',
            raw, re.IGNORECASE | re.DOTALL
        )
        for iid, block in inv_blocks:
            desc_match = re.match(r'(.*?)(?:TRIGGER:|$)', block, re.DOTALL)
            desc = desc_match.group(1).strip() if desc_match else block.strip()
            trigger_match = re.search(r'TRIGGER:\s*(.*?)(?:SEVERITY:|$)', block, re.IGNORECASE | re.DOTALL)
            severity_match = re.search(r'SEVERITY:\s*(\S+)', block, re.IGNORECASE)
            expl_match = re.search(r'EXPLANATION:\s*(.*?)(?=$)', block, re.IGNORECASE | re.DOTALL)

            if desc and len(desc) > 5:
                result["invalidations"].append({
                    "id": int(iid),
                    "description": desc[:500],
                    "trigger": trigger_match.group(1).strip()[:300] if trigger_match else "",
                    "severity": severity_match.group(1).strip().upper() if severity_match else "REASSESS",
                    "explanation": expl_match.group(1).strip()[:300] if expl_match else "",
                    "triggered": False,
                    "triggered_at": None,
                })

        raw_stripped = re.sub(r'\*+', '', raw)

        conf_match = re.search(r'overall confidence:?\s*(\d+)', raw_stripped, re.IGNORECASE)
        if not conf_match:
            conf_match = re.search(
                r'(?:confidence(?:\s*score)?|conviction)\s*[:=]?\s*(\d+)',
                raw_stripped, re.IGNORECASE)
        if not conf_match:
            conf_match = re.search(r'\b(\d{1,3})\s*%\s*(?:confidence|conviction)',
                                   raw_stripped, re.IGNORECASE)
        if not conf_match:
            conf_match = re.search(
                r'(?:confidence|conviction)[^0-9]{0,20}(\d{1,3})\s*%',
                raw_stripped, re.IGNORECASE)
        if conf_match:
            val = int(conf_match.group(1))
            if 0 <= val <= 100:
                result["confidence_score"] = val
            elif val > 100:
                result["confidence_score"] = 100

        # Parse chart_quality (1-10 rating from Stage 2 for feedback loop)
        cq_match = re.search(
            r'chart[_ ]?quality\s*[:=]?\s*(\d{1,2})',
            raw_stripped, re.IGNORECASE)
        if not cq_match:
            cq_match = re.search(
                r'chart[_ ]?(?:structure[_ ]?)?(?:score|rating|grade)\s*[:=]?\s*(\d{1,2})',
                raw_stripped, re.IGNORECASE)
        if cq_match:
            cq_val = int(cq_match.group(1))
            if 1 <= cq_val <= 10:
                result["chart_quality"] = cq_val

        # Parse time horizon
        for horizon, keywords in [("scalp", ["scalp"]), ("intraday", ["intraday"]),
                                   ("swing", ["swing"]), ("position", ["position"])]:
            if any(k in raw_lower for k in keywords):
                if "time horizon" in raw_lower[:raw_lower.index(keywords[0]) + 200] if keywords[0] in raw_lower else False:
                    result["time_horizon"] = horizon

        if "dormant" in raw_lower and ("yes" in raw_lower[raw_lower.index("dormant"):raw_lower.index("dormant")+100] if "dormant" in raw_lower else False):
            result["dormant_tracking"] = True

        # Entry price enforcement: derive from context if Stage 2 didn't provide one
        if not result["entry_price"] and result["trade_decision"] != "do_not_trade":
            entry_patterns = [
                r'entry\s*(?:price|zone|level)?\s*[:=]?\s*\$?\s*([\d,]+\.?\d*)',
                r'enter\s*(?:at|around|near)?\s*\$?\s*([\d,]+\.?\d*)',
                r'limit\s*(?:order\s*)?(?:at|@)\s*\$?\s*([\d,]+\.?\d*)',
            ]
            for pattern in entry_patterns:
                m = re.search(pattern, raw, re.IGNORECASE)
                if m:
                    try:
                        result["entry_price"] = float(m.group(1).replace(",", ""))
                        break
                    except ValueError:
                        pass

        # System default stop loss if none provided (2% from entry).
        # Uses 6 decimal places for forex/micro-cap assets.
        # Falls back to BUY-side SL when direction is unknown (conservative).
        if result["entry_price"] and not result["stop_loss"]:
            entry = result["entry_price"]
            direction = result.get("direction")
            if direction == "BUY":
                result["stop_loss"] = round(entry * 0.98, 6)
            elif direction == "SELL":
                result["stop_loss"] = round(entry * 1.02, 6)
            else:
                result["stop_loss"] = round(entry * 0.98, 6)
                logger.warning(f"[Dossier] Direction is None — applied default BUY-side SL "
                               f"as conservative fallback: {result['stop_loss']}")
            if direction:
                logger.info(f"[Dossier] Applied system default SL (2%% from entry): "
                            f"{result['stop_loss']}")

        # Guard: trade_now without a direction is invalid — force do_not_trade
        if result.get("trade_decision") == "trade_now" and not result.get("direction"):
            result["trade_decision"] = "do_not_trade"
            result["rationale"] = (result.get("rationale", "") +
                                   " | SYSTEM: No direction extracted — cannot trade without BUY/SELL.")
            logger.warning(f"[Dossier] trade_now with no direction — forced to do_not_trade")

        # ── R:R Calculation & Hard Minimum Filter ────────────────────
        # For mentor-triggered dossiers, use the mentor's original entry/SL/TP
        # for R:R validation instead of Stage 2's output.  Stage 2 often
        # hallucinates its own levels that differ from the mentor's actual setup.
        _mentor_ep = _mentor_sl = _mentor_tp1 = None
        if mentor_triggered and mentor_signal:
            _mentor_ep = float(mentor_signal["entry_price"]) if mentor_signal.get("entry_price") else None
            _mentor_sl = float(mentor_signal["stop_loss"]) if mentor_signal.get("stop_loss") else None
            _mentor_tp1 = float(mentor_signal["take_profit_1"]) if mentor_signal.get("take_profit_1") else None

        # Use either ALL mentor prices or ALL Stage 2 prices — never mix
        # the two sources, as a mentor entry with a Stage 2 SL produces
        # a franken-R:R that doesn't reflect either setup.
        if _mentor_ep and _mentor_sl and _mentor_tp1:
            rr_ep = _mentor_ep
            rr_sl = _mentor_sl
            rr_tp1 = _mentor_tp1
            used_mentor_levels = True
        else:
            rr_ep = result["entry_price"]
            rr_sl = result["stop_loss"]
            rr_tp1 = result["take_profit_1"]
            used_mentor_levels = False

        if rr_ep and rr_sl and rr_tp1:
            ep = rr_ep
            sl = rr_sl
            tp1 = rr_tp1
            risk = abs(ep - sl)
            reward_tp1 = abs(tp1 - ep)

            if used_mentor_levels and result["entry_price"] and result["stop_loss"]:
                s2_risk = abs(result["entry_price"] - result["stop_loss"])
                s2_reward = abs((result["take_profit_1"] or 0) - result["entry_price"]) if result["take_profit_1"] else 0
                s2_rr = round(s2_reward / s2_risk, 2) if s2_risk > 0 else 0
                logger.info(f"[Dossier] Mentor vs Stage2 levels: "
                            f"MENTOR entry={ep} SL={sl} TP1={tp1} | "
                            f"STAGE2 entry={result['entry_price']} "
                            f"SL={result['stop_loss']} TP1={result['take_profit_1']} "
                            f"(mentor R:R would be {round(reward_tp1/risk,2) if risk>0 else 0}:1, "
                            f"stage2 R:R would be {s2_rr}:1)")

            if risk > 0:
                rr = round(reward_tp1 / risk, 2)
                result["risk_reward"] = rr
                source_tag = "mentor" if used_mentor_levels else "stage2"
                logger.info(f"[Dossier] Calculated R:R to TP1 = {rr}:1 "
                            f"(risk={risk:.2f}, reward={reward_tp1:.2f}, "
                            f"source={source_tag})")

                min_rr = self._get_min_rr()
                ABSOLUTE_FLOOR_RR = float(self._td_cfg.get("min_rr_floor", 1.0))
                if rr < ABSOLUTE_FLOOR_RR and result["trade_decision"] != "do_not_trade":
                    logger.warning(f"[Dossier] HARD REJECT: R:R {rr}:1 < absolute "
                                   f"floor {ABSOLUTE_FLOOR_RR}:1. "
                                   f"{'Mentor' if mentor_triggered else 'Apex'} "
                                   f"trade abandoned.")
                    result["trade_decision"] = "do_not_trade"
                    result["rationale"] = (
                        f"SYSTEM: R:R to TP1 is {rr}:1 (below {ABSOLUTE_FLOOR_RR}:1 "
                        f"absolute floor — applies to ALL trades including mentors). "
                        f"Risk={risk:.6g}, Reward={reward_tp1:.6g}. "
                        f"Trade abandoned — never risk more than you can gain.")
                    self._store_rr_rejection_lesson(
                        symbol, result, rr, ABSOLUTE_FLOOR_RR, risk, reward_tp1,
                        ep, sl, tp1)
                elif rr < min_rr and result["trade_decision"] != "do_not_trade":
                    if mentor_triggered:
                        logger.info(f"[Dossier] R:R {rr}:1 < {min_rr}:1 — noted for Apex's "
                                    f"assessment (mentor signal, no hard reject)")
                        result["rr_warning"] = (f"R:R to TP1 is {rr}:1, below the "
                                                f"usual minimum of {min_rr}:1. "
                                                f"Apex may still trade if other factors justify it.")
                    else:
                        logger.warning(f"[Dossier] HARD REJECT: R:R {rr}:1 < minimum "
                                       f"{min_rr}:1. Apex trade abandoned.")
                        result["trade_decision"] = "do_not_trade"
                        result["rationale"] = (
                            f"SYSTEM: R:R to TP1 is {rr}:1 (below {min_rr}:1 minimum). "
                            f"Risk={risk:.6g}, Reward={reward_tp1:.6g}. "
                            f"Trade abandoned — SL placement was correct (smart SL), "
                            f"but the setup does not offer enough reward for the risk.")
                        self._store_rr_rejection_lesson(
                            symbol, result, rr, min_rr, risk, reward_tp1,
                            ep, sl, tp1)

        # When mentor levels were used for R:R and the trade wasn't rejected,
        # write the mentor's entry/SL/TP into the result so the dossier stores
        # the actual trade setup, not Stage 2's hallucinated levels.
        # Controlled by shadow_config: mentor_level_override_enabled (default true).
        override_enabled = self._get_tuning_param(
            "mentor_level_override_enabled", "true").lower() == "true"
        if used_mentor_levels and result["trade_decision"] != "do_not_trade" and override_enabled:
            result["entry_price"] = _mentor_ep
            result["stop_loss"] = _mentor_sl
            result["take_profit_1"] = _mentor_tp1
            for tp_key in ["take_profit_2", "take_profit_3"]:
                ms_tp = mentor_signal.get(tp_key) if mentor_signal else None
                if ms_tp:
                    result[tp_key] = float(ms_tp)
            logger.info(f"[Dossier] Overwrote Stage 2 levels with mentor's: "
                        f"entry={_mentor_ep}, SL={_mentor_sl}, TP1={_mentor_tp1}")
        elif used_mentor_levels and result["trade_decision"] != "do_not_trade" and not override_enabled:
            logger.info(f"[Dossier] Mentor override disabled — using Stage 2 levels. "
                        f"Mentor had: entry={_mentor_ep}, SL={_mentor_sl}, TP1={_mentor_tp1}")

        # ── Minimum SL Distance (liquidity sweep protection) ────────────
        # Prevents tight SLs that get swept by wicks before real moves.
        # Behavior is configurable: "skip" = reject trade (benchmark approach),
        # "widen" = widen SL to minimum (original live approach).
        # Short-circuit: if R:R already rejected, skip SL validation.
        ep = result["entry_price"]
        sl = result["stop_loss"]
        direction = result["direction"]
        tight_sl_behavior = self._td_cfg.get("tight_sl_behavior", "skip")
        _already_rejected = result.get("trade_decision") == "do_not_trade"

        if ep and sl and direction and ep > 0 and not _already_rejected:
            sl_distance_pct = abs(ep - sl) / ep * 100
            cfg_min_sl = self._td_cfg.get("min_sl_distance_pct")
            if cfg_min_sl is not None:
                min_sl_pct = float(cfg_min_sl)
            else:
                min_sl_pct = self._get_min_sl_distance_pct(symbol)

            if tight_sl_behavior == "widen":
                atr_min_distance = None
                try:
                    from services.data_scientist import get_data_scientist
                    ds = get_data_scientist(self.db)
                    candles_df = ds._fetch_candles_df(symbol, "M15", limit=30)
                    if candles_df is not None and len(candles_df) >= 15:
                        atr_data = ds._compute_atr(candles_df, period=14)
                        atr_val = atr_data.get("value")
                        if atr_val and atr_val > 0:
                            atr_min_distance = atr_val * 1.5
                            atr_min_pct = (atr_min_distance / ep) * 100
                            if atr_min_pct > min_sl_pct:
                                min_sl_pct = atr_min_pct
                                logger.info(f"[Dossier] ATR-based min SL: {atr_min_pct:.2f}% "
                                            f"(1.5x ATR={atr_min_distance:.6f})")
                except Exception:
                    pass

            if sl_distance_pct < min_sl_pct:
                if tight_sl_behavior == "skip":
                    logger.info(f"[Dossier] SL too tight ({sl_distance_pct:.2f}% < "
                                f"{min_sl_pct:.2f}% min). SKIPPING trade "
                                f"(tight_sl_behavior=skip).")
                    result["trade_decision"] = "do_not_trade"
                    result["rationale"] = (
                        f"SYSTEM: SL distance {sl_distance_pct:.2f}% is below the "
                        f"minimum {min_sl_pct:.2f}%. Trade skipped (benchmark approach).")
                else:
                    old_sl = sl
                    min_distance = ep * (min_sl_pct / 100)
                    if direction == "BUY":
                        result["stop_loss"] = round(ep - min_distance, 6)
                    else:
                        result["stop_loss"] = round(ep + min_distance, 6)
                    logger.warning(f"[Dossier] SL too tight ({sl_distance_pct:.2f}% from entry). "
                                   f"Widened from {old_sl} to {result['stop_loss']} "
                                   f"(min={min_sl_pct:.2f}%)")
                sl = result.get("stop_loss", sl)

        # ── Directional Sanity Checks ─────────────────────────────────
        if ep and sl and direction:
            if direction == "BUY" and sl >= ep:
                logger.warning(f"[Dossier] SL ({sl}) >= entry ({ep}) for BUY — flipping to correct side")
                result["stop_loss"] = round(ep - abs(ep - sl), 6)
            elif direction == "SELL" and sl <= ep:
                logger.warning(f"[Dossier] SL ({sl}) <= entry ({ep}) for SELL — flipping to correct side")
                result["stop_loss"] = round(ep + abs(ep - sl), 6)

            # Guard: if SL landed exactly at entry (zero distance), apply minimum
            # distance floor to prevent leverage collapse to 1x
            if abs(result.get("stop_loss", 0) - ep) < (ep * 1e-8):
                min_dist = ep * 0.005  # 0.5% minimum distance
                if direction == "BUY":
                    result["stop_loss"] = round(ep - min_dist, 6)
                else:
                    result["stop_loss"] = round(ep + min_dist, 6)
                logger.warning(f"[Dossier] SL at entry after flip — applied 0.5% min distance: "
                               f"SL={result['stop_loss']}")

        if ep and direction:
            for tp_key in ("take_profit_1", "take_profit_2", "take_profit_3"):
                tp = result.get(tp_key)
                if not tp:
                    continue
                if direction == "BUY" and tp <= ep:
                    logger.warning(f"[Dossier] {tp_key} ({tp}) <= entry ({ep}) for BUY — discarding")
                    result[tp_key] = None
                elif direction == "SELL" and tp >= ep:
                    logger.warning(f"[Dossier] {tp_key} ({tp}) >= entry ({ep}) for SELL — discarding")
                    result[tp_key] = None

        if result["confidence_score"] is not None:
            result["confidence_score"] = max(0, min(100, result["confidence_score"]))

        # Parse strategy match if present (LLM explicit match)
        strat_match = re.search(r'STRATEGY_MATCH:\s*(.+?)\s*\|\s*CONFIDENCE:\s*(\d+)', raw, re.IGNORECASE)
        if strat_match:
            strat_name = strat_match.group(1).strip()
            strat_conf = int(strat_match.group(2))
            try:
                strat_row = self.db.fetch_one(
                    "SELECT id FROM trading_strategies WHERE name = %s AND status = 'active'",
                    (strat_name,))
                if strat_row:
                    result["strategy_id"] = strat_row["id"]
                    result["strategy_confidence"] = strat_conf
                    logger.info(f"[Dossier] Strategy match: '{strat_name}' (ID {strat_row['id']}) "
                                f"confidence {strat_conf}%")
                else:
                    # Fuzzy fallback: LLM returned a name that doesn't exactly match.
                    # Search by LIKE to handle minor spelling differences.
                    fuzzy = self.db.fetch_one(
                        "SELECT id, name FROM trading_strategies "
                        "WHERE status = 'active' AND name LIKE %s LIMIT 1",
                        (f"%{strat_name[:20]}%",))
                    if fuzzy:
                        result["strategy_id"] = fuzzy["id"]
                        result["strategy_confidence"] = max(strat_conf - 10, 0)
                        logger.info(f"[Dossier] Strategy fuzzy match: '{strat_name}' -> "
                                    f"'{fuzzy['name']}' (ID {fuzzy['id']})")
            except Exception as e:
                logger.debug(f"[Dossier] Strategy match lookup failed: {e}")

        # Embedding-based strategy matching fallback: if no explicit match from LLM,
        # use vector similarity between the dossier's hypothesis and stored strategy rules.
        if not result.get("strategy_id") and result.get("trade_decision") != "do_not_trade":
            try:
                result["strategy_id"], result["strategy_confidence"] = \
                    self._match_strategy_by_embedding(result, raw)
            except Exception as e:
                logger.debug(f"[Dossier] Embedding strategy match error: {e}")

        # Extract rationale from raw text for ALL decisions (including do_not_trade)
        if not result.get("rationale"):
            for header in (r"##?\s*(?:TRADE\s+)?RATIONALE", r"##?\s*REASONING",
                           r"##?\s*WHY\s+THIS\s+TRADE", r"##?\s*ANALYSIS\s+SUMMARY",
                           r"##?\s*WHY\s+NOT\s+TRADE", r"##?\s*REJECTION\s+REASON",
                           r"##?\s*(?:DO\s+NOT\s+TRADE|DNT)\s+REASON"):
                m = re.search(header + r'\s*\n(.*?)(?=\n##|\Z)', raw, re.IGNORECASE | re.DOTALL)
                if m and len(m.group(1).strip()) > 20:
                    result["rationale"] = m.group(1).strip()[:500]
                    break

        # Extract stop-loss reasoning from raw text
        sl_hdr = re.search(
            r'##?\s*(?:STOP\s*LOSS|SL)\s*(?:LOGIC|REASONING|RATIONALE|PLACEMENT)?\s*\n(.*?)(?=\n##|\Z)',
            raw, re.IGNORECASE | re.DOTALL)
        if sl_hdr and len(sl_hdr.group(1).strip()) > 10:
            result["stop_loss_reasoning"] = sl_hdr.group(1).strip()[:300]

        # Extract mentor/signal references
        ref_hdr = re.search(
            r'##?\s*(?:MENTOR|SIGNAL)\s*(?:REFERENCES?|INPUT)?\s*\n(.*?)(?=\n##|\Z)',
            raw, re.IGNORECASE | re.DOTALL)
        if ref_hdr and len(ref_hdr.group(1).strip()) > 10:
            result["mentor_references"] = ref_hdr.group(1).strip()[:300]

        return result

    # ── Persistence ──────────────────────────────────────────────────

    def _save_dossier(self, symbol: str, dossier: Dict) -> int:
        """Save dossier to trade_dossiers table."""
        from db.market_symbols import is_valid_symbol
        if not is_valid_symbol(symbol):
            logger.error(f"[Dossier] REJECTED save: invalid symbol '{symbol}'")
            return -1

        s2 = dossier.get("stage2_output") or {}
        sections_json = {}
        for k, v in dossier.get("sections", {}).items():
            if k == "ohlcv":
                sections_json[k] = {tf: {"count": d.get("count", 0),
                                          "latest_close": d.get("latest_close"),
                                          "high_of_range": d.get("high_of_range"),
                                          "low_of_range": d.get("low_of_range")}
                                    for tf, d in v.items() if isinstance(d, dict)}
            else:
                sections_json[k] = v

        valid_decisions = ("trade_now", "wait_for_conditions", "do_not_trade")
        decision_val = s2.get("trade_decision") if s2.get("trade_decision") in valid_decisions else None

        if decision_val == "do_not_trade" or decision_val is None:
            logger.info(f"[Dossier] Stage 2 returned '{decision_val}' for {symbol} — skipping save")
            if not dossier.get("_stage2_cached"):
                self._save_shadow_trade(symbol, s2, dossier)
            else:
                logger.debug(f"[Dossier] {symbol}: S2 was cached cooldown — "
                             f"skipping shadow trade save to avoid duplicates")
            return -1

        if not s2.get("entry_price") or not s2.get("direction"):
            logger.error(f"[Dossier] REJECTED save for {symbol}: missing entry_price={s2.get('entry_price')} "
                         f"or direction={s2.get('direction')} — cannot create tradeable dossier")
            return -1

        # Build dossier intelligence from mentor data + signal context
        intelligence = self._build_dossier_intelligence(
            symbol, dossier.get("sections", {}), dossier.get("has_mentor_call", False))

        raw_sym = dossier.get("raw_symbol")

        try:
            dossier_id = self.db.execute_returning_id("""
                INSERT INTO trade_dossiers
                (symbol, raw_symbol, status, trade_decision, direction,
                 entry_price, stop_loss, take_profit_1, take_profit_2, take_profit_3,
                 confidence_score, dossier_sections, stage1_ta_output,
                 stage2_hypothesis, stage2_model_used, stage2_raw_response,
                 conditions_for_entry, probability_history, expires_at,
                 stage1_model_used, model_tier, dossier_intelligence,
                 strategy_id, strategy_confidence, duo_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                symbol,
                raw_sym,
                "proposed",
                decision_val,
                s2.get("direction"),
                s2.get("entry_price"),
                s2.get("stop_loss"),
                s2.get("take_profit_1"),
                s2.get("take_profit_2"),
                s2.get("take_profit_3"),
                s2.get("confidence_score"),
                json.dumps(sections_json, default=str),
                dossier.get("stage1_output"),
                json.dumps(s2, default=str),
                s2.get("model_used"),
                s2.get("raw_response"),
                json.dumps(s2.get("conditions", []), default=str),
                json.dumps([{
                    "time": _utcnow().isoformat(),
                    "probability": s2.get("confidence_score", 0),
                    "reason": "Initial dossier assessment"
                }], default=str),
                (_utcnow() + timedelta(
                    hours=self._td_cfg.get("dossier_expiry_hours", 24))).strftime(
                    "%Y-%m-%d %H:%M:%S"),
                getattr(self, '_last_stage1_model', None),
                getattr(self, '_last_stage1_tier', None),
                intelligence,
                s2.get("strategy_id"),
                s2.get("strategy_confidence"),
                self.duo_id or "unknown",
            ))

            # Post-hoc calibration enforcement: flag if confidence is significantly
            # out of line with historical accuracy for this confidence bucket
            self._enforce_calibration(dossier_id, s2.get("confidence_score"))

            return dossier_id
        except Exception as e:
            logger.error(f"[Dossier] Failed to save dossier: {e}")
            return -1

    # ── Shadow Trade Capture ─────────────────────────────────────────

    def _save_shadow_trade(self, symbol: str, s2: Dict, dossier: Dict):
        """Persist a rejected (do_not_trade) decision as a shadow trade
        for counterfactual P&L tracking and Ledger analysis.
        Zero LLM cost — captures data already computed by Stage 2."""
        if self._get_tuning_param("shadow_trade_tracking_enabled", "true").lower() != "true":
            return

        entry = s2.get("entry_price")
        direction = s2.get("direction")
        sl = s2.get("stop_loss")
        tp1 = s2.get("take_profit_1")
        confidence = s2.get("confidence_score")
        rationale = (s2.get("rationale") or s2.get("reasoning") or "")[:5000]
        raw_resp = (s2.get("raw_response") or "")[:50000]
        model = s2.get("model_used") or ""

        stage1_text = (dossier.get("stage1_output") or "")[:10000]

        conditions = s2.get("conditions")
        conditions_json = None
        if conditions:
            import json as _json
            try:
                conditions_json = _json.dumps(conditions, default=str)[:10000]
            except Exception:
                pass

        status = "pending" if (entry and sl) else "no_levels"

        sizing_mult = 1.0
        try:
            sections = dossier.get("sections") or {}
            regime = sections.get("market_regime") or {}
            sizing = regime.get("sizing_guidance") or {}
            sizing_mult = float(sizing.get("size_multiplier", 1.0))
        except Exception:
            pass

        try:
            ms_row = self.db.fetch_one(
                "SELECT asset_class FROM market_symbols WHERE symbol = %s", (symbol,))
            asset_class = (ms_row or {}).get("asset_class", "unknown")
        except Exception:
            asset_class = "unknown"

        # Dedup: skip if near-identical shadow trade already exists (within 1%)
        try:
            from services.trade_dedup import is_duplicate_trade
            if is_duplicate_trade(
                    self.db, symbol, direction,
                    float(entry or 0), float(sl or 0), float(tp1 or 0)):
                logger.info(f"[Dossier] Skipped duplicate shadow trade: {symbol} {direction} "
                            f"E:{entry} SL:{sl} TP:{tp1}")
                return
        except Exception as e:
            logger.debug(f"[Dossier] Shadow dedup check: {e}")

        try:
            new_shadow_id = self.db.execute_returning_id("""
                INSERT INTO apex_shadow_trades
                (symbol, direction, entry_price, stop_loss,
                 take_profit_1, take_profit_2, take_profit_3,
                 confidence_score, rationale, stage2_raw_response,
                 stage1_summary, model_used, conditions_snapshot,
                 asset_class, shadow_status, duo_id,
                 regime_sizing_multiplier)
                VALUES (%s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s)
            """, (
                symbol, direction, entry, sl,
                tp1, s2.get("take_profit_2"), s2.get("take_profit_3"),
                confidence, rationale, raw_resp,
                stage1_text, model, conditions_json,
                asset_class, status,
                self.duo_id,
                sizing_mult,
            ))
            logger.info(f"[Dossier] Shadow trade saved for {symbol} "
                        f"(id={new_shadow_id}, conf={confidence}%, status={status})")

            try:
                from services.shadow_queue import get_shadow_queue_manager
                sqm = get_shadow_queue_manager()
                if sqm and sqm.is_running and new_shadow_id:
                    sqm.notify_new_shadow(new_shadow_id)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[Dossier] Shadow trade save failed for {symbol}: {e}")

    # ── Agent Soul Loader ─────────────────────────────────────────────

    def _load_agent_soul(self, agent_id: str) -> Optional[Dict]:
        """Load an agent's soul and identity_prompt from agent_profiles DB.
        Delegates to core.config_loader.get_agent_soul."""
        return get_agent_soul(self.db, agent_id)

    def _get_tuning_param(self, key: str, default: str = "") -> str:
        """Read a single value from system_config.
        Delegates to core.config_loader.get_system_config."""
        return get_system_config(self.db, key, default)

    # ── Bull/Bear Pre-Trade Debate ──────────────────────────────────

    def _build_trade_context_prompt(self, symbol: str, dossier: Dict) -> str:
        """Build the shared trade context used by Bull and Bear.
        Now includes Stage 1 analysis, BillNye TA, and Geo/Macro context
        so advocates can build evidence-based cases."""
        s2 = dossier.get("stage2_output") or {}
        direction = s2.get("direction", "BUY")
        entry = s2.get("entry_price")
        sl = s2.get("stop_loss")
        tp1 = s2.get("take_profit_1")
        tp2 = s2.get("take_profit_2")
        tp3 = s2.get("take_profit_3")
        confidence = s2.get("confidence_score", 50)
        rationale = str(s2.get("rationale", ""))[:3000]
        raw_snippet = str(s2.get("raw_response", ""))[:5000]
        parts = [
            f"## PROPOSED TRADE\n"
            f"- Symbol: {symbol}\n"
            f"- Direction: {direction}\n"
            f"- Entry: {entry}\n"
            f"- Stop Loss: {sl}\n"
            f"- TP1: {tp1} | TP2: {tp2} | TP3: {tp3}\n"
            f"- Apex Confidence: {confidence}%\n",
            f"## APEX'S REASONING\n{rationale}\n",
        ]

        stage1 = dossier.get("stage1_output") or ""
        if stage1:
            parts.append(f"## STAGE 1 TECHNICAL ANALYSIS (Quant)\n{str(stage1)[:8000]}\n")

        sections = dossier.get("sections", {})
        ds_text = sections.get("data_scientist_text", "")
        if ds_text:
            parts.append(f"## BILLNYE COMPUTED INDICATORS\n{str(ds_text)[:5000]}\n")

        geo_text = self._format_geo_macro_for_stage1(
            sections.get("geopolitical", {}), "geopolitical")
        if geo_text:
            parts.append(f"## GEOPOLITICAL CONTEXT\n{geo_text}\n")

        macro_text = self._format_geo_macro_for_stage1(
            sections.get("macroeconomic", {}), "macroeconomic")
        if macro_text:
            parts.append(f"## MACROECONOMIC CONTEXT\n{macro_text}\n")

        companion = sections.get("companion_text", "")
        if companion:
            parts.append(f"## MARKET CONTEXT (VIX, correlations, calendar)\n"
                         f"{str(companion)[:3000]}\n")

        parts.append(f"## APEX'S FULL ANALYSIS (truncated)\n{raw_snippet}\n")
        return "\n".join(parts)

    def _run_bull_case(self, symbol: str, dossier: Dict) -> Optional[Dict]:
        """Run the Bull Agent to advocate FOR the proposed trade.
        Soul, analysis framework, and output format all come from agent_profiles DB.
        This method only provides the trade data — Bull decides how to analyze it."""
        if self._get_tuning_param("bull_debate_enabled", "true").lower() != "true":
            logger.info(f"[Dossier] Bull debate disabled via system_config, skipping for {symbol}")
            return None

        bull_soul = self._load_agent_soul("bull")
        system_prompt = bull_soul["soul"] if bull_soul else ""
        if not system_prompt:
            logger.warning(f"[Dossier] Bull agent has no soul in DB, skipping for {symbol}")
            return None

        trade_ctx = self._build_trade_context_prompt(symbol, dossier)
        s2 = dossier.get("stage2_output") or {}
        direction = s2.get("direction", "BUY")
        bull_directive = (
            f"\n\n## YOUR DIRECTIVE\n"
            f"The proposed trade is a {direction}. As Bull (Bullish Advocate), "
            f"build the STRONGEST possible case FOR this trade. Use Quant's "
            f"'THE LONG CASE' section from Stage 1 TA as your evidence foundation. "
            f"Reference specific levels, indicators, confluences, and geo/macro tailwinds. "
            f"Challenge Bear's objections preemptively. Be the prosecutor making the case "
            f"for why this trade SHOULD be taken.\n\n"
            f"## REQUIRED OUTPUT FORMAT (use these exact headers):\n"
            f"BULL_ARGUMENT: <your full argument paragraph>\n"
            f"SUPPORTING_FACTORS:\n"
            f"1. <factor one>\n"
            f"2. <factor two>\n"
            f"3. <factor three>\n"
            f"BULL_CONFIDENCE: <number 0-100>\n"
            f"VERDICT: <STRONG_CONVICTION or MODERATE_CONVICTION or WEAK_CONVICTION>")
        trade_ctx += bull_directive

        try:
            from core.model_interface import get_model_interface
            mcfg = self._resolve_stage_model("stage1")
            model, provider = mcfg["model"], mcfg["provider"]

            mi = get_model_interface()
            resp = mi.query_with_model(
                model_id=model, provider=provider,
                role="bull_agent",
                system_prompt=system_prompt,
                user_prompt=trade_ctx,
                max_tokens=self._td_cfg.get("bull_max_tokens",
                           get_system_config_int(self.db, "bull_max_tokens", 2000)),
                temperature=self._td_cfg.get("bull_temperature",
                             get_system_config_float(self.db, "bull_temperature", 0.4)),
                context="bull_case", source="trading_floor",
                dossier_id=dossier.get("id"),
                duo_id=self.duo_id)

            content = resp.content if resp and resp.success else ""
            if not content:
                logger.warning(f"[Dossier] Bull case returned empty for {symbol}")
                return None

            import re
            result = {
                "bull_argument": "",
                "bull_confidence": 0,
                "supporting_factors": [],
                "verdict": "MODERATE_CONVICTION",
                "model_used": model,
            }

            arg_match = re.search(
                r'BULL_ARGUMENT:\s*(.*?)(?=SUPPORTING_FACTORS:|BULL_CONFIDENCE:|$)',
                content, re.DOTALL | re.IGNORECASE)
            if arg_match:
                result["bull_argument"] = arg_match.group(1).strip()[:4000]

            conf_match = re.search(r'BULL_CONFIDENCE:\s*(\d+)', content, re.IGNORECASE)
            if conf_match:
                result["bull_confidence"] = min(100, int(conf_match.group(1)))

            factors = re.findall(
                r'(?:^|\n)\s*\d+\.\s*(.*?)(?=\n\s*\d+\.|\nBULL_CONFIDENCE|\nVERDICT|\Z)',
                content[content.lower().find("supporting_factors"):] if "supporting_factors" in content.lower() else "",
                re.DOTALL)
            result["supporting_factors"] = [f.strip()[:500] for f in factors[:5] if f.strip()]

            for verdict in ("STRONG_CONVICTION", "MODERATE_CONVICTION", "WEAK_CONVICTION"):
                if verdict.lower().replace("_", " ") in content.lower().replace("_", " "):
                    result["verdict"] = verdict
                    break

            logger.info(f"[Dossier] Bull case for {symbol}: "
                        f"confidence={result['bull_confidence']}%, "
                        f"verdict={result['verdict']}, "
                        f"factors={len(result['supporting_factors'])}")
            return result

        except Exception as e:
            logger.error(f"[Dossier] Bull case failed for {symbol}: {e}")
            return None

    def _run_bear_case(self, symbol: str, dossier: Dict) -> Optional[Dict]:
        """Run the Bear Agent against Stage 2's proposed trade.
        Soul, analysis framework, and output format all come from agent_profiles DB.
        Thresholds come from system_config (CEO/Ledger adjustable).
        This method only provides the trade data — Bear decides how to challenge it."""
        if self._get_tuning_param("bear_debate_enabled", "true").lower() != "true":
            logger.info(f"[Dossier] Bear debate disabled via system_config, skipping for {symbol}")
            return None

        bear_soul = self._load_agent_soul("bear")
        system_prompt = bear_soul["soul"] if bear_soul else ""
        if not system_prompt:
            logger.warning(f"[Dossier] Bear agent has no soul in DB, skipping for {symbol}")
            return None

        trade_ctx = self._build_trade_context_prompt(symbol, dossier)
        s2 = dossier.get("stage2_output") or {}
        direction = s2.get("direction", "BUY")
        bear_directive = (
            f"\n\n## YOUR DIRECTIVE\n"
            f"The proposed trade is a {direction}. As Bear (Bearish Challenger), "
            f"build the STRONGEST possible case AGAINST this trade. Use Quant's "
            f"'THE SHORT CASE' section from Stage 1 TA as your evidence foundation "
            f"(or 'THE LONG CASE' if the trade is a SELL — you argue for the opposite direction). "
            f"Reference specific levels, indicators, geo/macro headwinds, and risk factors "
            f"that could invalidate this setup. Be the defense attorney poking holes in "
            f"Bull's case. What could go WRONG?\n\n"
            f"## REQUIRED OUTPUT FORMAT (use these exact headers):\n"
            f"BEAR_ARGUMENT: <your full argument paragraph>\n"
            f"KEY_RISKS:\n"
            f"1. <risk one>\n"
            f"2. <risk two>\n"
            f"3. <risk three>\n"
            f"BEAR_CONFIDENCE: <number 0-100>\n"
            f"VERDICT: <STRONG_OBJECTION or CHALLENGE or APPROVE_WITH_CAUTION>")
        trade_ctx += bear_directive

        try:
            from core.model_interface import get_model_interface
            mcfg = self._resolve_stage_model("stage1")
            model, provider = mcfg["model"], mcfg["provider"]

            mi = get_model_interface()
            resp = mi.query_with_model(
                model_id=model, provider=provider,
                role="bear_agent",
                system_prompt=system_prompt,
                user_prompt=trade_ctx,
                max_tokens=self._td_cfg.get("bear_max_tokens",
                           get_system_config_int(self.db, "bear_max_tokens", 2000)),
                temperature=self._td_cfg.get("bear_temperature",
                             get_system_config_float(self.db, "bear_temperature", 0.4)),
                context="bear_case", source="trading_floor",
                dossier_id=dossier.get("id"),
                duo_id=self.duo_id)

            content = resp.content if resp and resp.success else ""
            if not content:
                logger.warning(f"[Dossier] Bear case returned empty for {symbol}")
                return None

            import re
            result = {
                "bear_argument": "",
                "bear_confidence": 0,
                "key_risks": [],
                "verdict": "APPROVE_WITH_CAUTION",
                "model_used": model,
            }

            arg_match = re.search(
                r'BEAR_ARGUMENT:\s*(.*?)(?=KEY_RISKS:|BEAR_CONFIDENCE:|$)',
                content, re.DOTALL | re.IGNORECASE)
            if arg_match:
                result["bear_argument"] = arg_match.group(1).strip()[:4000]

            conf_match = re.search(r'BEAR_CONFIDENCE:\s*(\d+)', content, re.IGNORECASE)
            if conf_match:
                result["bear_confidence"] = min(100, int(conf_match.group(1)))

            risks = re.findall(
                r'(?:^|\n)\s*\d+\.\s*(.*?)(?=\n\s*\d+\.|\nBEAR_CONFIDENCE|\nVERDICT|\Z)',
                content[content.lower().find("key_risks"):] if "key_risks" in content.lower() else "",
                re.DOTALL)
            result["key_risks"] = [r.strip()[:500] for r in risks[:5] if r.strip()]

            for verdict in ("STRONG_OBJECTION", "CHALLENGE", "APPROVE_WITH_CAUTION"):
                if verdict.lower() in content.lower():
                    result["verdict"] = verdict
                    break

            logger.info(f"[Dossier] Bear case for {symbol}: "
                        f"confidence={result['bear_confidence']}%, "
                        f"verdict={result['verdict']}, "
                        f"risks={len(result['key_risks'])}")
            return result

        except Exception as e:
            logger.error(f"[Dossier] Bear case failed for {symbol}: {e}")
            return None

    # ── Few-Shot Exemplar Retrieval (Experience Library) ─────────────

    def _retrieve_few_shot_exemplars(self, symbol: str,
                                      direction: Optional[str] = None,
                                      max_tokens: int = 2000) -> str:
        """Retrieve winning + corrected-failure dossier reasoning as few-shot
        examples for Stage 2. SiriuS-style experience library.
        Returns formatted text block to inject into Stage 2 prompt."""
        exemplars = []

        try:
            # 1) Best winning dossier on this symbol (same direction preferred)
            dir_clause = "AND direction = %s" if direction else ""
            params = [symbol]
            if direction:
                params.append(direction)

            win = self.db.fetch_one(f"""
                SELECT id, symbol, direction, entry_price, stop_loss,
                       take_profit_1, confidence_score, realised_pnl_pct,
                       LEFT(stage2_raw_response, {max_tokens}) AS reasoning
                FROM trade_dossiers
                WHERE symbol = %s AND status = 'won' {dir_clause}
                      AND stage2_raw_response IS NOT NULL
                      AND LENGTH(stage2_raw_response) > 200
                ORDER BY realised_pnl_pct DESC
                LIMIT 1
            """, tuple(params))

            if not win:
                # Fallback: winning dossier on same asset class
                asset_row = self.db.fetch_one(
                    "SELECT asset_class FROM market_symbols WHERE symbol = %s",
                    (symbol,))
                asset_class = (asset_row or {}).get("asset_class", "")
                if asset_class:
                    win = self.db.fetch_one(f"""
                        SELECT d.id, d.symbol, d.direction, d.entry_price,
                               d.stop_loss, d.take_profit_1, d.confidence_score,
                               d.realised_pnl_pct,
                               LEFT(d.stage2_raw_response, {max_tokens}) AS reasoning
                        FROM trade_dossiers d
                        JOIN market_symbols m ON m.symbol = d.symbol
                        WHERE d.status = 'won' AND m.asset_class = %s
                              AND d.stage2_raw_response IS NOT NULL
                              AND LENGTH(d.stage2_raw_response) > 200
                        ORDER BY d.realised_pnl_pct DESC
                        LIMIT 1
                    """, (asset_class,))

            if win and win.get("reasoning"):
                exemplars.append(
                    f"### WINNING TRADE EXAMPLE (Dossier #{win['id']})\n"
                    f"**{win['symbol']} {win['direction']}** | "
                    f"Entry: {win['entry_price']}, SL: {win['stop_loss']}, "
                    f"TP1: {win['take_profit_1']} | "
                    f"Confidence: {win['confidence_score']}% | "
                    f"Result: +{win['realised_pnl_pct']}%\n"
                    f"**How Apex reasoned on this winning trade:**\n"
                    f"{win['reasoning']}\n")

            # 2) Best corrected failure: same symbol or asset class
            loss_dir_clause = "AND d.symbol = %s" if symbol else ""
            loss_params = [symbol] if symbol else []
            loss = self.db.fetch_one(f"""
                SELECT d.id, d.symbol, d.direction, d.entry_price, d.stop_loss,
                       d.confidence_score, d.realised_pnl_pct,
                       LEFT(d.postmortem_output, {max_tokens}) AS postmortem,
                       LEFT(d.lessons_learned, 500) AS lesson
                FROM trade_dossiers d
                WHERE d.status = 'lost'
                      AND d.postmortem_output IS NOT NULL
                      AND LENGTH(d.postmortem_output) > 200
                      {loss_dir_clause}
                ORDER BY ABS(d.realised_pnl_pct) DESC
                LIMIT 1
            """, tuple(loss_params) if loss_params else None)

            if not loss:
                loss = self.db.fetch_one(f"""
                    SELECT id, symbol, direction, entry_price, stop_loss,
                           confidence_score, realised_pnl_pct,
                           LEFT(postmortem_output, {max_tokens}) AS postmortem,
                           LEFT(lessons_learned, 500) AS lesson
                    FROM trade_dossiers
                    WHERE status = 'lost'
                          AND postmortem_output IS NOT NULL
                          AND LENGTH(postmortem_output) > 200
                    ORDER BY ABS(realised_pnl_pct) DESC
                    LIMIT 1
                """)

            if loss and loss.get("postmortem"):
                exemplars.append(
                    f"### CORRECTED FAILURE (Dossier #{loss['id']})\n"
                    f"**{loss['symbol']} {loss['direction']}** | "
                    f"Entry: {loss['entry_price']}, SL: {loss['stop_loss']} | "
                    f"Confidence: {loss['confidence_score']}% | "
                    f"Result: {loss['realised_pnl_pct']}%\n"
                    f"**Lesson:** {loss.get('lesson', 'N/A')}\n"
                    f"**Post-mortem analysis (what should have been done):**\n"
                    f"{loss['postmortem']}\n")

            # 3) Overconfidence example: high-confidence trade that lost (teaches humility)
            # Prefer same-symbol exemplar; fallback to global if none exists
            overconf = self.db.fetch_one("""
                SELECT id, symbol, direction, confidence_score, realised_pnl_pct,
                       LEFT(lessons_learned, 400) AS lesson
                FROM trade_dossiers
                WHERE status = 'lost' AND confidence_score >= 80
                      AND lessons_learned IS NOT NULL
                      AND symbol = %s
                ORDER BY created_at DESC LIMIT 1
            """, (symbol,))
            if not overconf or not overconf.get("lesson"):
                overconf = self.db.fetch_one("""
                    SELECT id, symbol, direction, confidence_score, realised_pnl_pct,
                           LEFT(lessons_learned, 400) AS lesson
                    FROM trade_dossiers
                    WHERE status = 'lost' AND confidence_score >= 80
                          AND lessons_learned IS NOT NULL
                    ORDER BY created_at DESC LIMIT 1
                """)
            if overconf and overconf.get("lesson"):
                exemplars.append(
                    f"### OVERCONFIDENCE WARNING (Dossier #{overconf['id']})\n"
                    f"**{overconf['symbol']} {overconf['direction']}** was rated "
                    f"{overconf['confidence_score']}% confidence but LOST "
                    f"{overconf['realised_pnl_pct']}%.\n"
                    f"**Lesson:** {overconf['lesson']}\n"
                    f"*High confidence does not mean certain. Always validate.*\n")

        except Exception as e:
            logger.debug(f"[Dossier] Few-shot exemplar retrieval error: {e}")

        if not exemplars:
            return ""

        return (
            "\n\n## EXPERIENCE LIBRARY (Your Own Past Trades)\n"
            "Study these examples of your own past reasoning. The winning trade "
            "shows what GOOD analysis looks like. The corrected failure shows what "
            "to AVOID and what the post-mortem revealed.\n\n"
            + "\n".join(exemplars))

    # ── Calibration Enforcement ──────────────────────────────────────

    def _match_strategy_by_embedding(self, parsed_result: dict,
                                      raw_response: str) -> tuple:
        """Match a dossier to a strategy via embedding similarity.

        Compares the dossier's hypothesis/conditions/reasoning against each
        active strategy's structured_rules using the embedding model. Returns
        (strategy_id, confidence) or (None, None) if no strong match.

        Minimum similarity threshold: 0.75 (configurable via system_config).
        """
        strategies = self.db.fetch_all(
            "SELECT id, name, structured_rules FROM trading_strategies "
            "WHERE status = 'active' AND structured_rules IS NOT NULL "
            "AND LENGTH(structured_rules) > 50")
        if not strategies:
            return (None, None)

        # Build a compact representation of this dossier's trade thesis
        parts = []
        if parsed_result.get("hypothesis"):
            parts.append(str(parsed_result["hypothesis"])[:500])
        if parsed_result.get("rationale"):
            parts.append(str(parsed_result["rationale"])[:500])
        conditions = parsed_result.get("conditions_for_entry")
        if conditions and isinstance(conditions, list):
            parts.append(" | ".join(str(c.get("description", c))[:100]
                                    for c in conditions[:5]))
        if not parts:
            parts.append(raw_response[:1000])
        dossier_text = " ".join(parts)

        try:
            from core.embeddings import get_embedding
        except ImportError:
            return (None, None)

        try:
            dossier_vec = get_embedding(dossier_text)
            if not dossier_vec:
                return (None, None)
        except Exception:
            return (None, None)

        import numpy as np
        dossier_arr = np.array(dossier_vec, dtype=np.float32)

        best_id = None
        best_score = 0.0
        best_name = ""

        for strat in strategies:
            try:
                strat_vec = get_embedding(strat["structured_rules"][:2000])
                if not strat_vec:
                    continue
                strat_arr = np.array(strat_vec, dtype=np.float32)
                cos_sim = float(np.dot(dossier_arr, strat_arr) /
                                (np.linalg.norm(dossier_arr) * np.linalg.norm(strat_arr) + 1e-9))
                if cos_sim > best_score:
                    best_score = cos_sim
                    best_id = strat["id"]
                    best_name = strat["name"]
            except Exception:
                continue

        try:
            from db.database import get_system_config
            threshold = float(get_system_config(
                self.db, "strategy_embedding_threshold", "0.75"))
        except Exception:
            threshold = 0.75

        if best_score >= threshold and best_id:
            confidence = int(min(100, best_score * 100))
            logger.info(f"[Dossier] Embedding strategy match: '{best_name}' "
                        f"(ID {best_id}, sim={best_score:.3f}, conf={confidence}%)")
            return (best_id, confidence)

        return (None, None)

    def _enforce_calibration(self, dossier_id: int, confidence: int) -> int:
        """Check if the assigned confidence deviates significantly from
        historical win rate at that level. If deviation exceeds the threshold,
        apply a dampening multiplier to correct overconfidence.

        Returns the (possibly adjusted) confidence score."""
        if not confidence or confidence < 30:
            return confidence
        try:
            from db.database import get_system_config
            threshold = float(get_system_config(
                self.db, "calibration_deviation_threshold", "25"))

            bucket = (confidence // 10) * 10
            row = self.db.fetch_one("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN status='won' THEN 1 ELSE 0 END) as wins
                FROM trade_dossiers
                WHERE confidence_score BETWEEN %s AND %s
                  AND status IN ('won','lost')
            """, (bucket, bucket + 9))
            if not row or (row.get("total") or 0) < 10:
                return confidence

            actual_wr = row["wins"] / row["total"] * 100
            deviation = confidence - actual_wr

            if deviation > threshold:
                # Apply dampening: reduce confidence proportionally to deviation
                # Steeper curve: 50pp deviation yields 0.5 (50% reduction)
                dampen = max(0.5, 1.0 - (deviation - threshold) / 50)
                adjusted = max(30, int(confidence * dampen))

                logger.warning(
                    f"[Dossier] CALIBRATION CORRECTION on #{dossier_id}: "
                    f"confidence {confidence}%→{adjusted}% (historical WR at "
                    f"{bucket}-{bucket+9}% bucket is {actual_wr:.0f}%, "
                    f"deviation +{deviation:.0f}pp > threshold {threshold:.0f}pp)")

                self.db.execute(
                    "UPDATE trade_dossiers SET confidence_score = %s WHERE id = %s",
                    (adjusted, dossier_id))

                # Append calibration note to probability_history
                import json as _json
                hist_raw = self.db.fetch_one(
                    "SELECT probability_history FROM trade_dossiers WHERE id=%s",
                    (dossier_id,))
                if hist_raw and hist_raw.get("probability_history"):
                    hist = _json.loads(hist_raw["probability_history"])
                    hist.append({
                        "time": _utcnow().isoformat(),
                        "probability": adjusted,
                        "reason": (f"CALIBRATION: {confidence}%→{adjusted}% "
                                   f"(WR at this level={actual_wr:.0f}%, "
                                   f"deviation={deviation:.0f}pp)")
                    })
                    self.db.execute(
                        "UPDATE trade_dossiers SET probability_history=%s WHERE id=%s",
                        (_json.dumps(hist), dossier_id))

                return adjusted

        except Exception as e:
            logger.debug(f"[Dossier] Calibration enforcement error: {e}")
        return confidence


# ═══════════════════════════════════════════════════════════════════════
# MINI-DOSSIER UPDATE (for 15-minute Tracker checks)
# ═══════════════════════════════════════════════════════════════════════

def build_mini_dossier_update(db, config, dossier_id: int) -> Optional[Dict]:
    """
    Build a lightweight dossier update for Tracker's 15-min condition checks.
    Sends current conditions status + fresh alpha to the premium model.
    """
    dossier = db.fetch_one(
        "SELECT * FROM trade_dossiers WHERE id = %s", (dossier_id,))
    if not dossier:
        return None

    symbol = dossier["symbol"]
    conditions = json.loads(dossier.get("conditions_for_entry") or "[]")
    prob_hist = json.loads(dossier.get("probability_history") or "[]")

    recent_alpha = db.fetch_all("""
        SELECT headline, sentiment, direction, ai_analysis, collected_at, source, author
        FROM news_items
        WHERE (symbols LIKE %s OR headline LIKE %s)
              AND collected_at >= %s
        ORDER BY collected_at DESC
        LIMIT 10
    """, (f'%{symbol}%', f'%{symbol}%',
          _utcnow() - timedelta(minutes=30)))

    from services.candle_collector import get_candle_collector
    collector = get_candle_collector()
    latest_price = collector.get_latest_price(symbol) if collector else None

    return {
        "dossier_id": dossier_id,
        "symbol": symbol,
        "current_status": dossier["status"],
        "trade_decision": dossier.get("trade_decision"),
        "direction": dossier.get("direction"),
        "entry_price": float(dossier["entry_price"]) if dossier.get("entry_price") else None,
        "stop_loss": float(dossier["stop_loss"]) if dossier.get("stop_loss") else None,
        "latest_price": latest_price,
        "conditions": conditions,
        "probability_history": prob_hist,
        "recent_alpha": [
            {"headline": r.get("headline"), "sentiment": r.get("sentiment"),
             "source": r.get("source"), "author": r.get("author"),
             "analysis": (r.get("ai_analysis") or "")[:300],
             "time": r["collected_at"].isoformat() if r.get("collected_at") else None}
            for r in (recent_alpha or [])
        ],
        "time_since_creation": (
            _utcnow() - dossier["created_at"]).total_seconds() / 3600
            if dossier.get("created_at") else None,
    }


def run_postmortem(db, config, dossier_id: int) -> Optional[str]:
    """Run post-mortem analysis on a completed dossier."""
    dossier = db.fetch_one(
        "SELECT * FROM trade_dossiers WHERE id = %s", (dossier_id,))
    if not dossier:
        return None

    try:
        from core.model_interface import get_model_interface

        from core.duo_config import get_duo_config
        td_cfg = get_duo_config(config, dossier.get("duo_id"), db=db)
        model = td_cfg.get("postmortem_model", "claude-sonnet-4")
        provider = td_cfg.get("postmortem_provider", "anthropic")

        hypothesis = json.loads(dossier.get("stage2_hypothesis") or "{}")

        linked_signal = None
        if dossier.get("linked_signal_id"):
            linked_signal = db.fetch_one(
                "SELECT * FROM parsed_signals WHERE id = %s",
                (dossier["linked_signal_id"],))

        tracker_log = dossier.get("tracker_log", "No tracker updates recorded.")

        _duo_id = dossier.get("duo_id")
        pm_template = load_prompt(
            db, "dossier_postmortem_prompt", POSTMORTEM_PROMPT, min_length=100,
            duo_id=_duo_id)

        prompt = pm_template.format(
            symbol=dossier["symbol"],
            original_hypothesis=json.dumps(hypothesis, indent=2, default=str)[:5000],
            outcome_status=dossier.get("status", "unknown"),
            entry_price=dossier.get("entry_price", "N/A"),
            exit_price=linked_signal.get("entry_actual", "N/A") if linked_signal else "N/A",
            pnl_pips=linked_signal.get("outcome_pips", "N/A") if linked_signal else "N/A",
            duration="N/A",
            close_reason=linked_signal.get("outcome", "N/A") if linked_signal else "N/A",
            tracker_log=tracker_log,
            price_action_summary="[Price action data would be provided from candle data]"
        )

        pm_identity = load_prompt(
            db, "postmortem_system_identity",
            "You are a trading post-mortem investigator.", min_length=10,
            duo_id=_duo_id)

        mi = get_model_interface()
        resp = mi.query_with_model(
            model_id=model, provider=provider,
            role="postmortem", system_prompt=pm_identity,
            user_prompt=prompt,
            max_tokens=get_system_config_int(db, "postmortem_max_tokens", 6000),
            temperature=get_system_config_float(db, "postmortem_temperature", 0.3),
            context="trade_dossier", source="trading_floor",
            dossier_id=dossier_id,
            duo_id=dossier.get("duo_id"))
        content = resp.content if resp and resp.success else ""

        db.execute("""
            UPDATE trade_dossiers
            SET postmortem_output = %s, postmortem_at = NOW()
            WHERE id = %s
        """, (content, dossier_id))

        return content

    except Exception as e:
        logger.error(f"[Dossier] Postmortem failed for #{dossier_id}: {e}")
        return None
