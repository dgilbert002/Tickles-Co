"""
JarvAIs -- Scout Agent (Sole Discovery Engine)
===============================================
Centralised symbol discovery and duo routing.  The Scout is the **only**
path for opportunity discovery -- duos never find their own symbols.

Scout collects candidate coins from all sources (watchlist, alpha,
mentors, market movers) and routes each to duos via **fair round-robin**
distribution.  Each symbol is assigned to ONE duo at a time, rotating
through eligible duos so every desk gets equal opportunity.

Routing modes (``scout.routing_mode`` in config):
  - ``round_robin`` (default): Fair 1-2-3 rotation, account-aware.
  - ``broadcast``: Legacy mode -- every eligible duo gets every symbol.

Usage::

    from services.scout_agent import ScoutAgent
    scout = ScoutAgent(db, config)
    scout.start()          # spawns background thread
    scout.stop()           # clean shutdown
    scout.get_status()     # for dashboard API
"""

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("jarvais.scout")


def _utcnow() -> datetime:
    """Naive-UTC now -- compatible with MySQL DATETIME columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_duo_allowed(raw) -> list:
    """Safely extract list of duo IDs from ``duo_allowed`` column.
    Handles list, JSON string, dict, or None from MySQL JSON column."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(raw, dict):
        return list(raw.keys())
    return []


class ScoutAgent:
    """Sole discovery engine -- the only path for opportunity discovery.

    Collects symbol candidates from multiple sources and routes them to
    every enabled duo TradingFloorService for dossier building.

    Each duo receives symbols that pass these gates:
        1. Duo enabled check
        2. Per-duo cooldown (configurable per duo)
        3. Exchange tradability + account permissions (``duo_allowed``)
        4. Max-active dossier limit per symbol per duo
    """

    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._cooldowns: Dict[str, Dict[str, datetime]] = {}  # duo_id -> {symbol -> last_build}
        self._cooldown_lock = threading.Lock()
        self._activity_log = deque(maxlen=200)
        self._scout_cfg = self._load_scout_config()
        self._cycle_count = 0
        self._last_cycle_at: Optional[datetime] = None
        self._last_cycle_routed = 0
        self._intel_approvals: Dict[str, set] = {}  # symbol -> set of duo_ids approved by intel

        self._rr_index = 0
        self._rr_stats: Dict[str, int] = {}
        self._last_desk_brief_at: Optional[datetime] = None
        self._current_regime: Optional[Dict] = None

        routing_mode = self._scout_cfg.get("routing_mode", "round_robin")
        logger.info(f"[Scout] Initialised (enabled={self._scout_cfg.get('enabled', False)}, "
                    f"routing={routing_mode})")

    def _load_scout_config(self) -> Dict[str, Any]:
        """Load scout config from ``trade_decision.scout`` in config.json."""
        td = self.config.raw.get("trade_decision", {}) if self.config else {}
        return td.get("scout", {})

    def _si_cfg(self) -> Dict[str, Any]:
        """Symbol Intelligence sub-config (``scout.symbol_intel``)."""
        return self._scout_cfg.get("symbol_intel", {})

    # ── Symbol Intelligence ─────────────────────────────────────────

    def _load_symbol_intel(self, symbols: List[str],
                           duo_id: str) -> Optional[Dict[str, Dict]]:
        """Batch-load symbol_intel rows for a list of symbols on one duo.

        Returns ``{symbol: row_dict}`` (may be empty on fresh DB).
        Returns ``None`` on DB error so callers can distinguish failure
        from a legitimately empty table.
        """
        if not symbols:
            return {}
        placeholders = ",".join(["%s"] * len(symbols))
        try:
            rows = self.db.fetch_all(
                f"SELECT * FROM symbol_intel "
                f"WHERE symbol IN ({placeholders}) AND duo_id = %s",
                (*symbols, duo_id))
            return {r["symbol"]: dict(r) for r in (rows or [])}
        except Exception as e:
            logger.warning(f"[Scout] _load_symbol_intel failed: {e}")
            return None

    def _batch_load_prices(self, symbols: List[str]) -> Dict[str, float]:
        """Pre-fetch current prices for all candidate symbols in one query.

        Returns ``{symbol: price_float}``.  Missing symbols are simply
        absent from the map.  Called once per tick to avoid N individual
        ``live_prices`` lookups inside ``_should_rescan``.
        """
        if not symbols:
            return {}
        placeholders = ",".join(["%s"] * len(symbols))
        try:
            rows = self.db.fetch_all(
                f"SELECT symbol, price FROM live_prices "
                f"WHERE symbol IN ({placeholders})",
                tuple(symbols))
            return {r["symbol"]: float(r["price"])
                    for r in (rows or []) if r.get("price")}
        except Exception as e:
            logger.debug(f"[Scout] _batch_load_prices: {e}")
            return {}

    def _batch_load_volumes(self, symbols: List[str]) -> Dict[str, float]:
        """Pre-fetch recent average volumes for all candidate symbols in one query.

        Returns ``{symbol: avg_vol_float}``.  Uses M5 candles from the last hour.
        """
        if not symbols:
            return {}
        placeholders = ",".join(["%s"] * len(symbols))
        try:
            rows = self.db.fetch_all(
                f"SELECT symbol, AVG(volume) as avg_vol FROM candles "
                f"WHERE symbol IN ({placeholders}) AND timeframe = 'M5' "
                f"AND candle_time > DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 HOUR) "
                f"GROUP BY symbol",
                tuple(symbols))
            return {r["symbol"]: float(r["avg_vol"])
                    for r in (rows or []) if r.get("avg_vol")}
        except Exception as e:
            logger.debug(f"[Scout] _batch_load_volumes: {e}")
            return {}

    def _batch_load_new_signals(self, symbols: List[str],
                                cutoff: datetime) -> Dict[str, int]:
        """Pre-fetch new signal counts since *cutoff* for all symbols in one query.

        Returns ``{symbol: signal_count}``.
        """
        if not symbols:
            return {}
        placeholders = ",".join(["%s"] * len(symbols))
        try:
            rows = self.db.fetch_all(
                f"SELECT symbol, COUNT(*) as cnt FROM parsed_signals "
                f"WHERE symbol IN ({placeholders}) AND parsed_at > %s "
                f"GROUP BY symbol",
                tuple(symbols) + (cutoff,))
            return {r["symbol"]: int(r.get("cnt", 0))
                    for r in (rows or []) if int(r.get("cnt", 0)) > 0}
        except Exception as e:
            logger.debug(f"[Scout] _batch_load_new_signals: {e}")
            return {}

    def _should_rescan(self, symbol: str, duo_id: str,
                       intel: Optional[Dict],
                       duo_routing: Dict, *,
                       price_cache: Optional[Dict[str, float]] = None,
                       volume_cache: Optional[Dict[str, float]] = None,
                       signal_cache: Optional[Dict[str, int]] = None
                       ) -> Tuple[bool, str]:
        """Decide whether *symbol* needs a fresh dossier build on *duo_id*.

        All checks use pure math / DB lookups -- zero LLM cost.

        Returns ``(should_build, reason)`` where *reason* is a short string
        explaining why the symbol was skipped or why it needs a rescan.
        """
        si = self._si_cfg()
        if not si.get("enabled", False):
            return True, "intel_disabled"

        if intel is None:
            return True, "first_scan"

        last_at = intel.get("last_analyzed_at")
        if last_at is None:
            return True, "never_analyzed"

        now = _utcnow()
        if hasattr(last_at, 'tzinfo') and last_at.tzinfo is not None:
            last_at = last_at.replace(tzinfo=None)
        age_min = (now - last_at).total_seconds() / 60.0

        staleness = max(1, duo_routing.get(
            "staleness_minutes",
            si.get("default_staleness_minutes", 120)))
        if age_min >= staleness:
            return True, f"stale_{int(age_min)}m"

        price_thresh = si.get("price_change_threshold_pct", 2.0) / 100.0
        old_price = float(intel.get("price_at_analysis") or 0)
        if old_price > 0:
            cur = (price_cache or {}).get(symbol) if price_cache else None
            if cur is None:
                try:
                    lp = self.db.fetch_one(
                        "SELECT price FROM live_prices WHERE symbol = %s",
                        (symbol,))
                    if lp and lp.get("price"):
                        cur = float(lp["price"])
                except Exception:
                    pass
            if cur is not None:
                change = abs(cur - old_price) / old_price
                if change >= price_thresh:
                    return True, f"price_moved_{change*100:.1f}pct"

        vol_mult = si.get("volume_spike_multiplier", 2.0)
        old_vol = int(intel.get("volume_at_analysis") or 0)
        if old_vol > 0:
            cur_vol = (volume_cache or {}).get(symbol) if volume_cache else None
            if cur_vol is None:
                try:
                    vr = self.db.fetch_one(
                        "SELECT AVG(volume) as avg_vol FROM candles "
                        "WHERE symbol = %s AND timeframe = 'M5' "
                        "AND candle_time > DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 HOUR)",
                        (symbol,))
                    if vr and vr.get("avg_vol"):
                        cur_vol = float(vr["avg_vol"])
                except Exception:
                    pass
            if cur_vol is not None and cur_vol >= old_vol * vol_mult:
                return True, f"volume_spike_{cur_vol/old_vol:.1f}x"

        if si.get("force_rescan_on_new_signal", True):
            cached_sig = signal_cache.get(symbol) if signal_cache else None
            if cached_sig is not None and cached_sig == 0 and len(signal_cache) > 0:
                sig_count = 0
            else:
                try:
                    sig = self.db.fetch_one(
                        "SELECT COUNT(*) as cnt FROM parsed_signals "
                        "WHERE symbol = %s AND parsed_at > %s",
                        (symbol, last_at))
                    sig_count = int(sig["cnt"]) if sig else 0
                except Exception:
                    sig_count = 0
            if sig_count > 0:
                return True, f"new_signal_{sig_count}"

        return False, "fresh"

    def _compute_priority_rank(self, symbol: str, source: str,
                               intel: Optional[Dict]) -> float:
        """Compute a composite priority score for ranking candidates.

        Uses only data already in ``symbol_intel`` and config weights --
        no additional DB queries.  Higher = more important.
        """
        si = self._si_cfg()
        w = si.get("weights", {})
        w_quant  = w.get("quant_score", 0.25)
        w_wr     = w.get("win_rate", 0.30)
        w_pnl    = w.get("pnl", 0.20)
        decay    = w.get("freshness_decay_per_hour", 0.1)
        src_bonus_map = si.get("source_bonus", {})
        lesson_bonus  = si.get("lesson_depth_bonus", 5)

        score = 0.0

        src_bonus = src_bonus_map.get(source, 0)
        score += src_bonus

        if intel is None:
            score += 10.0
            return round(score, 4)

        qs = intel.get("last_quant_score")
        if qs is not None:
            try:
                score += float(qs) * w_quant
            except (ValueError, TypeError):
                pass

        wins   = int(intel.get("total_wins", 0))
        losses = int(intel.get("total_losses", 0))
        total  = wins + losses
        if total > 0:
            bayesian_wr = (wins + 1) / (total + 2) * 100.0
            score += bayesian_wr * w_wr

        pnl_sum = float(intel.get("realized_pnl_sum") or 0)
        norm_pnl = max(-100, min(100, pnl_sum))
        score += norm_pnl * w_pnl

        if int(intel.get("lesson_count") or 0) > 0:
            score += lesson_bonus

        last_at = intel.get("last_analyzed_at")
        if last_at:
            now = _utcnow()
            if hasattr(last_at, 'tzinfo') and last_at.tzinfo is not None:
                last_at = last_at.replace(tzinfo=None)
            hours_old = min((now - last_at).total_seconds() / 3600.0, 168.0)
            score -= hours_old * decay

        return round(score, 4)

    def _update_intel_skip(self, symbol: str, duo_id: str, reason: str):
        """Increment consecutive_skips and record skip_reason.

        Uses INSERT ... ON DUPLICATE KEY UPDATE so it works even if the
        row doesn't exist yet (e.g. symbol was written by a previous
        version of the code that didn't create stub rows).
        """
        try:
            self.db.execute(
                "INSERT INTO symbol_intel (symbol, duo_id, consecutive_skips, skip_reason) "
                "VALUES (%s, %s, 1, %s) "
                "ON DUPLICATE KEY UPDATE "
                "consecutive_skips = consecutive_skips + 1, skip_reason = VALUES(skip_reason)",
                (symbol, duo_id, reason))
        except Exception as e:
            logger.debug(f"[Scout] _update_intel_skip: {e}")

    def _ensure_intel_row(self, symbol: str, duo_id: str, source: str):
        """Insert a stub symbol_intel row if one doesn't exist.

        Uses INSERT IGNORE so existing rows (with real analysis data from
        the dossier builder) are never overwritten.
        """
        try:
            self.db.execute(
                "INSERT IGNORE INTO symbol_intel (symbol, duo_id, best_source) "
                "VALUES (%s, %s, %s)",
                (symbol, duo_id, source))
        except Exception as e:
            logger.debug(f"[Scout] _ensure_intel_row: {e}")

    def _persist_priority_rank(self, symbol: str, duo_id: str, rank: float):
        """Write the computed priority_rank back to the DB row."""
        try:
            self.db.execute(
                "UPDATE symbol_intel SET priority_rank = %s, consecutive_skips = 0, "
                "skip_reason = NULL WHERE symbol = %s AND duo_id = %s",
                (rank, symbol, duo_id))
        except Exception as e:
            logger.debug(f"[Scout] _persist_priority_rank: {e}")

    # ── LLM Advisor (Future Phase — disabled by default) ────────────

    def _llm_advisor_refine(self, ranked: List[Dict],
                            duo_id: str) -> List[Dict]:
        """Ask an LLM to refine the priority ranking using trade_memory RAG.

        **Disabled by default** (``symbol_intel.llm_advisor_enabled = false``).
        When enabled, sends the top-N ranked candidates plus relevant context
        from the vector store to an LLM and returns a re-ordered list.

        Falls back to the original ranking on any error.
        """
        si = self._si_cfg()
        if not si.get("llm_advisor_enabled", False):
            return ranked

        top_n = si.get("llm_advisor_top_n", 20)
        subset = ranked[:top_n]
        if not subset:
            return ranked

        try:
            from core.rag_search import RagSearchEngine
            rag = RagSearchEngine()
            symbols_str = ", ".join(c["symbol"] for c in subset)

            rag_results = rag.search(
                query=f"Recent trade outcomes and lessons for: {symbols_str}",
                collections=["trade_memory"],
                top_k=10)

            context_lines = []
            for r in (rag_results or []):
                text = r.get("text", r.get("content", ""))
                if text:
                    context_lines.append(text[:500])
            rag_context = "\n---\n".join(context_lines) if context_lines else "No prior trade memory."

            from core.model_interface import query_llm
            prompt = (
                "You are a trading prioritisation advisor. Given the ranked candidate "
                "symbols below and the historical trade memory context, return ONLY a "
                "JSON array of the symbols in your recommended priority order (highest "
                "priority first). Consider win rates, recent P&L, and lesson quality.\n\n"
                f"## Candidates (current ranking)\n"
            )
            for i, c in enumerate(subset, 1):
                prompt += f"{i}. {c['symbol']} (source={c['source']})\n"
            prompt += f"\n## Trade Memory Context\n{rag_context}\n\n"
            prompt += "Respond with ONLY a JSON array of symbol strings, e.g. [\"BTCUSDT\",\"ETHUSDT\"]"

            resp = query_llm(prompt, model="gpt-4.1-mini", provider="openai",
                             max_tokens=500, temperature=0.1)

            import json as _json
            text = resp if isinstance(resp, str) else str(resp)
            start = text.find("[")
            end = text.rfind("]")
            if start >= 0 and end > start:
                ordered_syms = _json.loads(text[start:end + 1])
                sym_to_cand = {c["symbol"]: c for c in subset}
                reordered = [sym_to_cand[s] for s in ordered_syms
                             if s in sym_to_cand]
                remaining = [c for c in subset if c not in reordered]
                result = reordered + remaining + ranked[top_n:]
                logger.info(f"[Scout] LLM Advisor re-ranked {len(reordered)} "
                            f"candidates for {duo_id}")
                return result
        except Exception as e:
            logger.warning(f"[Scout] LLM Advisor failed, using math ranking: {e}")

        return ranked

    # ── Desk Curator (CTS + Desk Brief) ──────────────────────────

    def _curator_cfg(self) -> Dict[str, Any]:
        """Desk Curator sub-config (``scout.desk_curator``)."""
        return self._scout_cfg.get("desk_curator", {})

    def _compute_chart_scores(self, candidates: List[Dict]) -> List[Dict]:
        """Score every non-mentor candidate with the Chart Tradeability Score.

        Uses DataScientist.compute_chart_tradeability_score (pure math, zero LLM).
        Persists the CTS into symbol_intel and filters out symbols below the
        configured minimum.  Mentor candidates always pass through.

        Defensive: if DataScientist is unavailable or CTS returns 0 due to
        missing candle data, the candidate passes through unfiltered (we don't
        punish symbols we can't judge).
        """
        dc = self._curator_cfg()
        cts_min = dc.get("cts_minimum", 35)

        mentor_cands = [c for c in candidates
                        if c.get("source") in ("mentor", "mentor_observation")]
        non_mentor = [c for c in candidates
                      if c.get("source") not in ("mentor", "mentor_observation")]

        if not non_mentor:
            return candidates

        try:
            from services.data_scientist import get_data_scientist
            ds = get_data_scientist(self.db)
        except Exception as e:
            logger.warning(f"[Scout] CTS: DataScientist unavailable: {e}")
            return candidates

        scored = []
        filtered = 0
        for cand in non_mentor:
            sym = cand["symbol"]
            try:
                result = ds.compute_chart_tradeability_score(sym)
                cts = result.get("cts", 0)
                grade = result.get("grade", "F")
                reason = result.get("reason")
                cand["_cts"] = cts
                cand["_cts_grade"] = grade

                if reason == "no_candle_data":
                    scored.append(cand)
                    continue

                self._persist_cts(sym, cts, grade)

                if cts < cts_min:
                    filtered += 1
                    self._log_activity("cts_filtered", sym,
                                       f"CTS={cts} ({grade}) < min {cts_min}")
                    continue
                scored.append(cand)
            except Exception as e:
                logger.debug(f"[Scout] CTS computation failed for {sym}: {e}")
                scored.append(cand)

        scored.sort(key=lambda c: c.get("_cts", 0), reverse=True)

        logger.info(f"[Scout] CTS: {len(non_mentor)} scored, {filtered} filtered "
                    f"(< {cts_min}), {len(scored)} passed")
        return mentor_cands + scored

    def _persist_cts(self, symbol: str, cts: float, grade: str):
        """Write CTS score to symbol_intel rows for this symbol.

        Uses UPDATE first (fast path — hits all duo rows at once).
        If zero rows affected (new coin with no intel row yet), inserts a
        stub row for a generic duo so the CTS is at least stored once.
        """
        try:
            affected = self.db.execute(
                "UPDATE symbol_intel SET chart_tradeability_score = %s, "
                "cts_grade = %s WHERE symbol = %s",
                (cts, grade, symbol))
            rows_updated = affected if isinstance(affected, int) else 0
            if rows_updated == 0:
                self.db.execute(
                    "INSERT IGNORE INTO symbol_intel "
                    "(symbol, duo_id, chart_tradeability_score, cts_grade) "
                    "VALUES (%s, '_cts_stub', %s, %s)",
                    (symbol, cts, grade))
        except Exception as e:
            logger.debug(f"[Scout] _persist_cts: {e}")

    def _curate_desk_brief(self, candidates: List[Dict]) -> List[Dict]:
        """Run the Desk Brief: one batch LLM call to rank the best candidates.

        Only runs every ``desk_brief_interval_hours``.  Between runs, candidates
        pass through with their CTS/priority_rank ordering unchanged.

        The LLM receives data cards for the top N candidates and returns a
        ranked JSON array of symbols.  This re-orders (not filters) the
        candidate list so the best coins get routed first.
        """
        dc = self._curator_cfg()

        if not self._should_run_desk_brief():
            return candidates

        mentor_cands = [c for c in candidates
                        if c.get("source") in ("mentor", "mentor_observation")]
        non_mentor = [c for c in candidates
                      if c.get("source") not in ("mentor", "mentor_observation")]

        top_n = dc.get("desk_brief_top_n", 40)
        select_n = dc.get("desk_brief_select_n", 20)
        subset = non_mentor[:top_n]

        if len(subset) < 3:
            return candidates

        data_cards = self._build_data_cards(subset)
        prompt = self._build_desk_brief_prompt(data_cards, select_n)

        model = dc.get("desk_brief_model", "x-ai/grok-4.20-multi-agent")
        provider = dc.get("desk_brief_provider", "openrouter")
        max_tokens = dc.get("desk_brief_max_tokens", 8192)
        temperature = dc.get("desk_brief_temperature", 0.3)
        deep = dc.get("desk_brief_deep_analysis", False)

        # Build extra_params for Grok: reasoning + (optionally) web search.
        # Note: web_search tool is only supported on xAI's direct API, NOT
        # through OpenRouter.  We only include it when provider != "openrouter".
        extra = {}
        extra_body = {}
        if dc.get("desk_brief_reasoning_enabled", True):
            extra_body["reasoning"] = {"enabled": True}
            effort = dc.get("desk_brief_reasoning_effort", "high")
            if effort:
                extra_body["reasoning"]["effort"] = effort
        if extra_body:
            extra["extra_body"] = extra_body
        web_search_ok = (dc.get("desk_brief_web_search", False)
                         and provider.lower() != "openrouter")
        if web_search_ok:
            extra["tools"] = [{"type": "web_search"}]
            extra["tool_choice"] = "auto"

        sys_prompt = (self._get_desk_brief_system_prompt_deep()
                      if deep else self._get_desk_brief_system_prompt())

        try:
            from core.model_interface import get_model_interface
            mi = get_model_interface()
            response = mi.query_with_model(
                model_id=model, provider=provider,
                role="desk_curator",
                system_prompt=sys_prompt,
                user_prompt=prompt,
                account_id="global",
                max_tokens=max_tokens,
                temperature=temperature,
                context="desk_brief",
                extra_params=extra if extra else None,
            )

            if not response.success:
                logger.warning(f"[Scout] Desk Brief LLM failed: {response.error_message}")
                self._last_desk_brief_at = _utcnow()
                return candidates

            if deep:
                ranked_symbols, trade_setups = self._parse_desk_brief_deep(
                    response.content)
            else:
                ranked_symbols = self._parse_desk_brief_response(response.content)
                trade_setups = {}

            if not ranked_symbols:
                logger.warning("[Scout] Desk Brief returned empty ranking — "
                               "LLM may have ignored JSON-only instruction")
                self._last_desk_brief_at = _utcnow()
                return candidates

            # Parallel Manus call (if enabled)
            _sh_cfg = {}
            try:
                _sh_rows = self.db.fetch_all(
                    "SELECT config_key, config_value FROM shadow_config "
                    "WHERE config_key LIKE 'manus_%'")
                _sh_cfg = {r["config_key"]: r["config_value"]
                           for r in (_sh_rows or [])}
            except Exception:
                pass
            manus_enabled = (_sh_cfg.get("manus_desk_brief_enabled", "false")
                            .strip().lower() == "true")
            manus_data = None
            manus_setups = []
            if manus_enabled:
                try:
                    from concurrent.futures import TimeoutError as FutTimeout
                    from core.thread_pool import DaemonThreadPoolExecutor as ThreadPoolExecutor
                    manus_timeout = int(_sh_cfg.get(
                        "manus_intel_timeout_seconds", 300))
                    candidate_symbols = [c["symbol"] for c in subset]
                    with ThreadPoolExecutor(max_workers=1,
                                           thread_name_prefix="manus") as pool:
                        future = pool.submit(
                            self._extract_manus_market_intel,
                            candidate_symbols, data_cards, select_n)
                        manus_data = future.result(timeout=manus_timeout)
                except FutTimeout:
                    logger.warning("[Scout] Manus call timed out — proceeding "
                                   "with Grok results only")
                except Exception as e:
                    logger.warning(f"[Scout] Manus parallel call failed: {e}")

                if manus_data:
                    self._store_manus_market_intel(manus_data)
                    manus_setups = manus_data.get("trade_setups", [])
                    if manus_setups:
                        self._persist_manus_setups(manus_setups)

            sym_to_cand = {c["symbol"]: c for c in non_mentor}
            reordered = []
            seen_ranked: Set[str] = set()
            for i, sym in enumerate(ranked_symbols, 1):
                if sym in seen_ranked:
                    continue
                seen_ranked.add(sym)
                if sym in sym_to_cand:
                    cand = sym_to_cand.pop(sym)
                    cand["_desk_brief_rank"] = i
                    reordered.append(cand)
                    self._persist_desk_brief_rank(sym, i)

            # Merge Grok + Manus trade setups
            if trade_setups and manus_setups:
                merged_setups = self._merge_grok_and_manus_setups(
                    trade_setups, manus_setups)
                grok_authored = {k: v for k, v in merged_setups.items()
                                 if v.get("_source") != "manus_only"}
                if grok_authored:
                    self._persist_grok_setups(grok_authored)
            elif trade_setups:
                self._persist_grok_setups(trade_setups)

            remaining = list(sym_to_cand.values())
            result = mentor_cands + reordered + remaining

            setup_count = len(trade_setups)
            manus_label = (f", manus={len(manus_setups)} setups"
                           if manus_enabled and manus_data else "")
            logger.info(f"[Scout] Desk Brief: LLM ranked {len(ranked_symbols)} symbols, "
                        f"{len(reordered)} matched candidates, "
                        f"{setup_count} Grok setups{manus_label} "
                        f"(cost=${response.cost_usd:.4f})")

            self._last_desk_brief_at = _utcnow()
            self._log_activity("desk_brief",
                               f"{len(reordered)} ranked, {setup_count} setups{manus_label}",
                               f"model={model} cost=${response.cost_usd:.4f}")
            return result

        except Exception as e:
            logger.error(f"[Scout] Desk Brief error: {e}", exc_info=True)
            self._last_desk_brief_at = _utcnow()
            return candidates

    def _should_run_desk_brief(self) -> bool:
        """Session-aware scheduling for the Desk Brief.

        Weekdays (Mon-Fri):  run at 05:00, 11:00, 18:00, 22:00 UTC
        Weekends (Sat-Sun):  run at 05:00, 18:00 UTC  (crypto only)

        ``tolerance_minutes`` (default 30) defines a window around each
        scheduled hour.  If the current time is within that window AND the
        brief hasn't already run in this window, it fires.

        Falls back to the old ``desk_brief_interval_hours`` if the schedule
        config is missing (backward compatibility).
        """
        dc = self._curator_cfg()
        schedule = dc.get("desk_brief_schedule")

        if not schedule:
            interval_h = dc.get("desk_brief_interval_hours", 4)
            last = getattr(self, "_last_desk_brief_at", None)
            if last is None:
                return True
            elapsed = (_utcnow() - last).total_seconds() / 3600.0
            return elapsed >= interval_h

        now = _utcnow()
        is_weekend = now.weekday() >= 5  # Sat=5, Sun=6
        hours = schedule.get("weekend_utc" if is_weekend else "weekday_utc",
                             [5, 11, 18, 22])
        tolerance = schedule.get("tolerance_minutes", 30)

        current_hour = now.hour
        current_min = now.minute
        current_minutes = current_hour * 60 + current_min

        for h in hours:
            window_start = h * 60 - tolerance
            window_end = h * 60 + tolerance

            if window_start <= current_minutes <= window_end:
                last = getattr(self, "_last_desk_brief_at", None)
                if last is None:
                    return True
                last_minutes = last.hour * 60 + last.minute
                if last.date() < now.date():
                    return True
                if abs(last_minutes - h * 60) > tolerance:
                    return True
                return False

        return False

    def _build_data_cards(self, candidates: List[Dict]) -> str:
        """Build compact data cards for each candidate to include in the prompt.

        RAG snippets are expensive (~1min each via hybrid Qdrant search), so
        only the top 10 candidates by CTS get RAG enrichment.  The rest still
        get symbol_intel data (free DB query).
        """
        dc = self._curator_cfg()
        rag_top_n = dc.get("rag_top_n_for_cards", 10)

        cards = []
        for idx, cand in enumerate(candidates):
            sym = cand["symbol"]
            cts = cand.get("_cts", "?")
            grade = cand.get("_cts_grade", "?")
            source = cand.get("source", "watchlist")

            intel = {}
            try:
                row = self.db.fetch_one(
                    "SELECT last_verdict, last_quant_score, last_confidence, "
                    "total_wins, total_losses, realized_pnl_sum, avg_chart_quality, "
                    "chart_quality_samples "
                    "FROM symbol_intel WHERE symbol = %s LIMIT 1",
                    (sym,))
                if row:
                    intel = dict(row)
            except Exception:
                try:
                    row = self.db.fetch_one(
                        "SELECT last_verdict, last_quant_score, last_confidence, "
                        "total_wins, total_losses, realized_pnl_sum "
                        "FROM symbol_intel WHERE symbol = %s LIMIT 1",
                        (sym,))
                    if row:
                        intel = dict(row)
                except Exception:
                    pass

            rag_snippet = ""
            if idx < rag_top_n:
                rag_snippet = self._get_rag_snippet(sym)

            card = f"### {sym}\n"
            card += f"- CTS: {cts}/100 ({grade}) | Source: {source}\n"
            if intel:
                qs = intel.get("last_quant_score") or "n/a"
                verdict = intel.get("last_verdict") or "n/a"
                wins = intel.get("total_wins", 0)
                losses = intel.get("total_losses", 0)
                pnl = float(intel.get("realized_pnl_sum") or 0)
                avg_cq = intel.get("avg_chart_quality")
                card += f"- Quant: {qs}/100 | Verdict: {verdict}\n"
                card += f"- W/L: {wins}/{losses} | PnL: ${pnl:.2f}\n"
                if avg_cq:
                    card += f"- Avg Chart Quality: {avg_cq}/10 ({intel.get('chart_quality_samples', 0)} samples)\n"
            if rag_snippet:
                card += f"- Intel: {rag_snippet}\n"
            cards.append(card)

        logger.info(f"[Scout] Desk Brief: built {len(cards)} data cards "
                    f"(RAG enriched top {min(rag_top_n, len(cards))})")
        return "\n".join(cards)

    def _get_rag_snippet(self, symbol: str) -> str:
        """Fetch a short RAG snippet for *symbol* from trade memory.  Free (no LLM).

        Uses a tick-level cache flag to avoid hammering a dead Qdrant 40 times.
        """
        if getattr(self, "_rag_unavailable_this_tick", False):
            return ""

        dc = self._curator_cfg()
        collections = dc.get("rag_collections", ["trade_memory"])
        max_snippets = dc.get("rag_snippets_per_symbol", 2)

        try:
            from core.rag_search import get_rag_engine
            rag = get_rag_engine()
            if rag is None:
                self._rag_unavailable_this_tick = True
                return ""
            results = rag.search(
                query=f"Recent trade outcomes and chart quality for {symbol}",
                collections=collections,
                limit=max_snippets,
                hybrid=True)
            if results:
                texts = []
                for r in results:
                    text = getattr(r, "text", str(r))
                    if text:
                        texts.append(text[:200])
                return " | ".join(texts)
        except Exception as e:
            logger.debug(f"[Scout] RAG snippet for {symbol}: {e}")
            self._rag_unavailable_this_tick = True
        return ""

    def _get_desk_brief_system_prompt(self) -> str:
        """Load the Desk Brief system prompt from prompt_versions (DB-driven).

        Falls back to a hardcoded default if the DB row doesn't exist yet.
        """
        try:
            row = self.db.fetch_one(
                "SELECT system_prompt FROM prompt_versions "
                "WHERE role = 'desk_curator' AND is_active = 1 "
                "ORDER BY version DESC LIMIT 1")
            if row and row.get("system_prompt"):
                return row["system_prompt"]
        except Exception:
            pass

        return (
            "You are the Head Quant Trader at JarvAIs, a professional crypto trading firm.\n"
            "Your job is to review the Desk Brief — a batch of candidate trading symbols — "
            "and rank them by tradeability.\n\n"
            "TRADEABILITY means:\n"
            "1. Chart structure is clean: respects Fibonacci levels, EMAs are aligned, "
            "swing highs/lows are orderly, BOS/CHoCH events are present\n"
            "2. Confluence: multiple independent signals agree (RSI, MACD, Bollinger, volume)\n"
            "3. Historical reliability: coins that have previously respected levels and "
            "produced winning trades are preferred\n"
            "4. Volume confirms the move: relative volume is healthy, not dying\n"
            "5. Risk/reward potential: clear entry, stop loss, and take profit zones exist\n\n"
            "You are NOT looking for:\n"
            "- Coins that are just volatile (chaos is not tradeability)\n"
            "- Coins with no structure (random chop)\n"
            "- Low-volume coins where slippage is a risk\n\n"
            "Respond with ONLY a JSON array of symbols in your ranked order, "
            "best first. Example: [\"BTCUSDT\", \"ETHUSDT\", \"SOLUSDT\"]\n"
            "No explanations, no markdown, just the JSON array."
        )

    def _build_desk_brief_prompt(self, data_cards: str, select_n: int) -> str:
        """Build the user prompt for the Desk Brief LLM call."""
        dc = self._curator_cfg()
        if dc.get("desk_brief_deep_analysis", False):
            return (
                f"## DESK BRIEF — Identify the Top {select_n} Trade Setups\n\n"
                f"Review the following {len(data_cards.split('###')) - 1} candidates. "
                f"Analyse chart structure, confluence, and volume from the data provided. "
                f"Select the top {select_n} with genuine trade setups and return the "
                f"JSON array of objects as specified in your instructions.\n\n"
                f"Remember: ALL of entry, stop_loss, tp1, tp2, tp3 are MANDATORY for "
                f"each symbol. Minimum R:R of 2:1 to TP1. No setup = don't include it.\n\n"
                f"{data_cards}\n\n"
                f"Respond with ONLY the JSON array of trade setup objects."
            )
        return (
            f"## DESK BRIEF — Select the Top {select_n} Most Tradeable Charts\n\n"
            f"Review the following candidate symbols and their data. "
            f"Rank them by tradeability (chart structure, confluence, reliability, volume). "
            f"Return the top {select_n} as a JSON array of symbol strings, best first.\n\n"
            f"{data_cards}\n\n"
            f"Respond with ONLY the JSON array. No commentary."
        )

    def _parse_desk_brief_response(self, content: str) -> List[str]:
        """Extract the ranked symbol list from the LLM response."""
        if not content:
            return []
        text = content.strip()
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            return []
        try:
            parsed = json.loads(text[start:end + 1])
            if isinstance(parsed, list):
                return [str(s).upper().strip() for s in parsed if isinstance(s, str)]
        except (json.JSONDecodeError, TypeError):
            pass
        return []

    def _persist_desk_brief_rank(self, symbol: str, rank: int):
        """Write the Desk Brief rank and timestamp to symbol_intel."""
        try:
            self.db.execute(
                "UPDATE symbol_intel SET desk_brief_rank = %s, "
                "last_desk_brief_at = UTC_TIMESTAMP() WHERE symbol = %s",
                (rank, symbol))
        except Exception as e:
            logger.debug(f"[Scout] _persist_desk_brief_rank: {e}")

    # ── Deep Desk Brief (Grok Multi-Agent with trade setups) ──────

    def _get_desk_brief_system_prompt_deep(self) -> str:
        """System prompt for the enhanced Desk Brief that returns full trade setups.

        Grok Multi-Agent with web search and reasoning produces:
        - Ranked symbols
        - Direction, entry, SL, TP1-3, confidence for each
        - Brief reasoning justifying the setup
        """
        return (
            "You are the Head Quant Trader at JarvAIs, a professional crypto trading firm. "
            "Your team of analysts (including yourself) use technical analysis, market structure, "
            "and quantitative signals to identify the best trading opportunities.\n\n"

            "## YOUR TASK\n"
            "Review the Desk Brief data cards below. For each candidate, you have:\n"
            "- Chart Tradeability Score (CTS): a math-only structural quality score\n"
            "- Technical indicators: EMAs, RSI, MACD, Bollinger Bands, ATR, volume\n"
            "- Historical trade performance and chart quality feedback\n"
            "- RAG context: recent alpha analysis, signals, and trade memory\n\n"

            "If you have web search capability, use it to check CURRENT prices and "
            "recent price action for the top candidates. If web search is unavailable, "
            "use the data provided in the cards and your training knowledge to estimate "
            "current price levels. Entry/SL/TP should be based on the most recent data available.\n\n"

            "## WHAT MAKES A TRADEABLE CHART\n"
            "1. **Structure**: Clean swing highs/lows, respects Fib levels (0.618, 0.786), "
            "EMA ribbon aligned, BOS/CHoCH present, market structure shift (MSS)\n"
            "2. **Confluence**: 3+ independent signals agree — RSI, MACD cross, Bollinger position, "
            "volume confirmation, VWAP respect, key S/R zones\n"
            "3. **Volume**: Relative volume healthy (>1.0), not dying. Volume confirms direction\n"
            "4. **History**: Coins that previously respected levels, had liquidity sweeps that recovered, "
            "showed predictable behavior at session opens/closes\n"
            "5. **R:R**: Minimum 2:1 risk-reward to TP1. Clear invalidation zone for stop loss\n\n"

            "## WHAT TO AVOID\n"
            "- Chaos/random chop with no structure\n"
            "- Dead volume / illiquid coins\n"
            "- Coins mid-range with no clear direction\n"
            "- Setups where SL is too tight (< 1% from entry) or too wide (> 5%)\n\n"

            "## RESPONSE FORMAT\n"
            "Return ONLY a JSON array of objects. Each object MUST have these fields:\n"
            "```\n"
            '{"symbol": "BTCUSDT", "direction": "BUY", "confidence": 82, '
            '"entry": 65000.00, "stop_loss": 63500.00, '
            '"tp1": 67000.00, "tp2": 69500.00, "tp3": 72000.00, '
            '"reasoning": "Clean H4 bullish structure..."}\n'
            "```\n\n"
            "Rules:\n"
            "- `direction`: \"BUY\" or \"SELL\" only\n"
            "- `confidence`: 0-100 integer (your conviction level)\n"
            "- `entry`: realistic entry price based on CURRENT market (use web search)\n"
            "- `stop_loss`: below recent swing low (BUY) or above swing high (SELL)\n"
            "- `tp1`, `tp2`, `tp3`: ALL THREE are MANDATORY. Based on key levels, Fib extensions, R:R\n"
            "- `reasoning`: 1-3 sentences explaining the setup (key levels, confluence)\n"
            "- Best trade first, worst last\n"
            "- If a coin has no viable setup, do NOT include it\n\n"
            "Respond with ONLY the JSON array. No markdown fences, no commentary outside the array."
        )

    def _parse_desk_brief_deep(self, content: str) -> tuple:
        """Parse the enhanced Desk Brief response with trade setups.

        Returns (ranked_symbols, trade_setups_dict).
        Gracefully falls back to simple symbol list if deep parsing fails.
        """
        if not content:
            return [], {}

        text = content.strip()
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            return [], {}

        try:
            parsed = json.loads(text[start:end + 1])
        except (json.JSONDecodeError, TypeError):
            return self._parse_desk_brief_response(content), {}

        if not isinstance(parsed, list):
            return [], {}

        # If it's a list of strings, fall back to simple mode
        if parsed and isinstance(parsed[0], str):
            return [s.upper().strip() for s in parsed if isinstance(s, str)], {}

        ranked = []
        setups = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol", "")).upper().strip()
            if not sym:
                continue
            ranked.append(sym)

            # Extract trade setup fields
            direction = str(item.get("direction", "")).upper()
            if direction not in ("BUY", "SELL"):
                continue

            try:
                setup = {
                    "direction": direction,
                    "confidence": int(item.get("confidence", 0)),
                    "entry": float(item.get("entry", 0)),
                    "stop_loss": float(item.get("stop_loss", 0)),
                    "tp1": float(item.get("tp1", 0)),
                    "tp2": float(item.get("tp2", 0)),
                    "tp3": float(item.get("tp3", 0)),
                    "reasoning": str(item.get("reasoning", ""))[:2000],
                }
                # Validate: all prices must be positive
                if all(setup[k] > 0 for k in ("entry", "stop_loss", "tp1", "tp2", "tp3")):
                    setups[sym] = setup
            except (ValueError, TypeError):
                continue

        logger.info(f"[Scout] Desk Brief deep parse: {len(ranked)} ranked, "
                    f"{len(setups)} valid trade setups")
        return ranked, setups

    def _persist_grok_setups(self, setups: Dict[str, Dict]):
        """Store Grok's trade setup predictions in symbol_intel for shadow tracking.

        Uses a dedicated ``_grok_setup`` duo_id row so each symbol has exactly
        one Grok prediction row, independent of per-duo intel rows.
        """
        for sym, s in setups.items():
            try:
                affected = self.db.execute(
                    "UPDATE symbol_intel SET "
                    "grok_direction = %s, grok_confidence = %s, "
                    "grok_entry = %s, grok_stop_loss = %s, "
                    "grok_tp1 = %s, grok_tp2 = %s, grok_tp3 = %s, "
                    "grok_reasoning = %s, grok_setup_at = UTC_TIMESTAMP(), "
                    "grok_setup_outcome = 'pending', "
                    "grok_setup_resolved_at = NULL "
                    "WHERE symbol = %s AND duo_id = '_grok_setup'",
                    (s["direction"], s["confidence"],
                     s["entry"], s["stop_loss"],
                     s["tp1"], s["tp2"], s["tp3"],
                     s["reasoning"], sym))
                if affected == 0:
                    self.db.execute(
                        "INSERT IGNORE INTO symbol_intel "
                        "(symbol, duo_id, grok_direction, grok_confidence, "
                        "grok_entry, grok_stop_loss, grok_tp1, grok_tp2, grok_tp3, "
                        "grok_reasoning, grok_setup_at, grok_setup_outcome) "
                        "VALUES (%s, '_grok_setup', %s, %s, %s, %s, %s, %s, %s, %s, "
                        "UTC_TIMESTAMP(), 'pending')",
                        (sym, s["direction"], s["confidence"],
                         s["entry"], s["stop_loss"],
                         s["tp1"], s["tp2"], s["tp3"],
                         s["reasoning"]))
                logger.debug(f"[Scout] Grok setup stored: {sym} {s['direction']} "
                             f"entry={s['entry']} conf={s['confidence']}%")
            except Exception as e:
                logger.debug(f"[Scout] _persist_grok_setup {sym}: {e}")

    # ── Manus Market Intelligence ─────────────────────────────────

    def _extract_manus_market_intel(self, symbols: List[str],
                                     data_cards: str,
                                     select_n: int) -> Optional[Dict]:
        """Call Manus API to extract live market data and generate trade setups.

        Runs in parallel with the Grok desk brief. Manus browses 5 URLs
        (CoinMarketCap RSI, Coinglass Funding, OI Heatmap, L/S Ratio,
        Liquidation Map) AND reviews the same data cards as Grok.

        Returns parsed JSON dict with market_context, coin_data,
        manus_regime_assessment, and trade_setups[], or None on failure.
        """
        coin_list = ", ".join(s.replace("USDT", "") for s in symbols)

        user_prompt = (
            f"You are JarvAIs's Lead Data Extraction Agent and Quant Trader. "
            f"I am Scout, the market discovery agent.\n"
            f"I have identified {len(symbols)} candidate coins.\n\n"
            f"Your task is TWOFOLD:\n"
            f"PART 1: Visit 4 specific market dashboards to extract aggregate "
            f"live regime data.\n"
            f"PART 2: Review the candidate coins and generate the top "
            f"{select_n} trade setups.\n\n"
            f"The candidate coins are: {coin_list}\n\n"
            f"=========================================\n"
            f"PART 1: MARKET DATA EXTRACTION\n"
            f"=========================================\n"
            f"STEP 1: Visit https://coinmarketcap.com/charts/rsi/\n"
            f"- Read the \"Average Crypto RSI\", \"Overbought %\", and "
            f"\"Oversold %\" from the summary bar at the top.\n"
            f"- Do NOT extract per-coin RSI data.\n\n"
            f"STEP 2: Visit https://www.coinglass.com/FundingRate\n"
            f"- Read the \"BTC OI-Weighted Funding Rate\" from the header "
            f"panel.\n"
            f"- Do NOT extract per-coin funding rates.\n\n"
            f"STEP 3: Visit https://www.coinglass.com/pro/futures/"
            f"hyperliquid-long-short-ratio\n"
            f"- Read the \"Long Traders\", \"Short Traders\", and "
            f"\"Long/Short Trader Ratio\" from the top cards.\n\n"
            f"STEP 4: Visit https://www.coinglass.com/pro/futures/"
            f"LiquidationMap\n"
            f"- Scroll down to the \"Hyperliquid Liquidation Map\" section.\n"
            f"- Visually assess the chart: are the largest liquidation "
            f"clusters (tallest bars / steepest cumulative lines) ABOVE or "
            f"BELOW the current price?\n"
            f"- Note: If the Binance map requires login, use the Hyperliquid "
            f"map.\n\n"
            f"=========================================\n"
            f"PART 2: TRADE SETUPS\n"
            f"=========================================\n"
            f"Act as a Quant Trader. Review the {len(symbols)} candidate "
            f"coins below. Select the top {select_n} best setups based on "
            f"the current market regime you just extracted and relative "
            f"strength.\n"
            f"ALL of entry, stop_loss, tp1, tp2, tp3 are MANDATORY. "
            f"Min R:R 2:1 to TP1.\n\n"
            f"For coins WITH Grok intel in the data cards: use the Grok "
            f"entry/SL/TP as a reference but apply your own judgement.\n"
            f"For coins WITHOUT Grok intel: you MAY still generate a setup "
            f"if the data card and market context give you enough conviction.\n\n"
            f"{data_cards}\n\n"
            f"=========================================\n"
            f"RESPONSE FORMAT\n"
            f"=========================================\n"
            f"Return the JSON as a file attachment named "
            f"jarvais_trade_setups.json.\n"
            f"The JSON object must have this structure:\n"
            f'{{\n'
            f'  "market_context": {{\n'
            f'    "average_crypto_rsi": 48.5,\n'
            f'    "percent_overbought": 12.3,\n'
            f'    "percent_oversold": 28.7,\n'
            f'    "btc_oi_weighted_funding_rate": -0.0014,\n'
            f'    "hyperliquid_long_traders": 33455,\n'
            f'    "hyperliquid_short_traders": 20857,\n'
            f'    "hyperliquid_ls_ratio": 1.604,\n'
            f'    "btc_liquidation_cluster_bias": "below"\n'
            f'  }},\n'
            f'  "manus_regime_assessment": "2-3 sentences synthesizing the '
            f'market data.",\n'
            f'  "trade_setups": [\n'
            f'    {{"symbol": "BTCUSDT", "direction": "BUY", '
            f'"confidence": 85,\n'
            f'      "entry": 85400.00, "stop_loss": 83800.00,\n'
            f'      "tp1": 87500.00, "tp2": 90000.00, "tp3": 93500.00,\n'
            f'      "reasoning": "Clean H4 bullish structure..."}}\n'
            f'  ]\n'
            f'}}\n\n'
            f"RULES:\n"
            f"- Funding rates must be decimals, NOT percentages "
            f"(e.g. -0.0040% on screen = -0.00004 in JSON).\n"
            f"- Use plain ASCII dashes (-) in reasoning text; do NOT use "
            f"Unicode em-dashes (\\u2014) to avoid parser errors.\n"
            f"- trade_setups[].symbol must end with USDT "
            f"(e.g. \"BTCUSDT\" not \"BTC\")."
        )

        try:
            from core.model_interface import get_model_interface
            mi = get_model_interface()
            response = mi.query_with_model(
                model_id="manus-1",
                provider="manus",
                role="manus_market_intel",
                system_prompt="You are a data extraction agent. Follow instructions precisely.",
                user_prompt=user_prompt,
                account_id="global",
                max_tokens=8192,
                temperature=0.1,
                context="manus_desk_brief",
            )

            if not response.success:
                logger.warning(f"[Scout] Manus intel failed: {response.error_message}")
                return None

            content = (response.content or "").strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content.rsplit("```", 1)[0].strip()
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                import json
                parsed = json.loads(content[start:end])
                logger.info(f"[Scout] Manus intel received: "
                            f"{len(parsed.get('trade_setups', []))} setups, "
                            f"bias={parsed.get('market_context', {}).get('btc_liquidation_cluster_bias', '?')} "
                            f"(cost=${response.cost_usd:.4f})")
                return parsed

            logger.warning("[Scout] Manus response did not contain valid JSON")
            return None

        except Exception as e:
            logger.error(f"[Scout] Manus market intel extraction failed: {e}",
                         exc_info=True)
            return None

    def _store_manus_market_intel(self, manus_data: Dict):
        """Store Manus aggregate market intelligence in the database.

        Writes market_context to market_regime_intel table.
        Per-coin data (RSI, funding, OI) is handled by CCXT/BillNye,
        not Manus — the optimised prompt only extracts aggregate data.
        """
        now = _utcnow()
        mc = manus_data.get("market_context", {})

        try:
            self.db.execute(
                "INSERT INTO market_regime_intel "
                "(timestamp, average_crypto_rsi, percent_overbought, percent_oversold, "
                "btc_oi_weighted_funding_rate, hyperliquid_long_traders, "
                "hyperliquid_short_traders, hyperliquid_ls_ratio, "
                "btc_liquidation_cluster_bias, manus_regime_assessment) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (now,
                 mc.get("average_crypto_rsi"),
                 mc.get("percent_overbought"),
                 mc.get("percent_oversold"),
                 mc.get("btc_oi_weighted_funding_rate"),
                 mc.get("hyperliquid_long_traders"),
                 mc.get("hyperliquid_short_traders"),
                 mc.get("hyperliquid_ls_ratio"),
                 mc.get("btc_liquidation_cluster_bias"),
                 manus_data.get("manus_regime_assessment")))
        except Exception as e:
            logger.error(f"[Scout] Failed to store Manus market context: {e}")

        logger.info(f"[Scout] Stored Manus market intel: "
                    f"RSI={mc.get('average_crypto_rsi')}, "
                    f"L/S={mc.get('hyperliquid_ls_ratio')}, "
                    f"bias={mc.get('btc_liquidation_cluster_bias')}")

    def _persist_manus_setups(self, setups: List[Dict]):
        """Store Manus trade setup predictions in symbol_intel for shadow tracking."""
        for s in setups:
            sym = s.get("symbol", "")
            if not sym:
                continue
            try:
                affected = self.db.execute(
                    "UPDATE symbol_intel SET "
                    "manus_direction = %s, manus_confidence = %s, "
                    "manus_entry = %s, manus_stop_loss = %s, "
                    "manus_tp1 = %s, manus_reasoning = %s, "
                    "manus_setup_at = UTC_TIMESTAMP() "
                    "WHERE symbol = %s AND duo_id = '_grok_setup'",
                    (s.get("direction"), s.get("confidence"),
                     s.get("entry"), s.get("stop_loss"),
                     s.get("tp1"), s.get("reasoning"), sym))
                if affected == 0:
                    self.db.execute(
                        "INSERT IGNORE INTO symbol_intel "
                        "(symbol, duo_id, manus_direction, manus_confidence, "
                        "manus_entry, manus_stop_loss, manus_tp1, "
                        "manus_reasoning, manus_setup_at) "
                        "VALUES (%s, '_grok_setup', %s, %s, %s, %s, %s, %s, "
                        "UTC_TIMESTAMP())",
                        (sym, s.get("direction"), s.get("confidence"),
                         s.get("entry"), s.get("stop_loss"),
                         s.get("tp1"), s.get("reasoning")))
                logger.debug(f"[Scout] Manus setup stored: {sym} "
                             f"{s.get('direction')} conf={s.get('confidence')}%")
            except Exception as e:
                logger.debug(f"[Scout] _persist_manus_setup {sym}: {e}")

    def _merge_grok_and_manus_setups(self, grok_setups: Dict[str, Dict],
                                      manus_setups: List[Dict]) -> Dict[str, Dict]:
        """Merge Grok and Manus trade setups, flagging consensus.

        Priority: agreement (same direction) > Grok-only > Manus-only.
        Disagreements are flagged as conflicted.
        """
        manus_by_sym = {}
        for s in (manus_setups or []):
            sym = s.get("symbol", "")
            if sym:
                manus_by_sym[sym] = s

        merged = {}
        all_symbols = set(list(grok_setups.keys()) + list(manus_by_sym.keys()))

        for sym in all_symbols:
            grok = grok_setups.get(sym)
            manus = manus_by_sym.get(sym)

            if grok and manus:
                g_dir = (grok.get("direction") or "").upper()
                m_dir = (manus.get("direction") or "").upper()
                consensus = g_dir == m_dir
                merged[sym] = grok.copy()
                merged[sym]["_manus_agrees"] = consensus
                merged[sym]["_manus_direction"] = m_dir
                merged[sym]["_manus_confidence"] = manus.get("confidence", 0)
                if consensus:
                    merged[sym]["_consensus_boost"] = True
                try:
                    self.db.execute(
                        "UPDATE symbol_intel SET llm_consensus = %s "
                        "WHERE symbol = %s AND duo_id = '_grok_setup'",
                        (1 if consensus else 0, sym))
                except Exception:
                    pass
            elif grok:
                merged[sym] = grok.copy()
                merged[sym]["_manus_agrees"] = None
            elif manus:
                merged[sym] = {
                    "direction": manus.get("direction"),
                    "confidence": manus.get("confidence", 50),
                    "entry": manus.get("entry"),
                    "stop_loss": manus.get("stop_loss"),
                    "tp1": manus.get("tp1"),
                    "tp2": manus.get("tp2"),
                    "tp3": manus.get("tp3"),
                    "reasoning": manus.get("reasoning"),
                    "_source": "manus_only",
                    "_manus_agrees": None,
                }

        agreed = sum(1 for v in merged.values() if v.get("_consensus_boost"))
        logger.info(f"[Scout] Merged setups: {len(merged)} total, "
                    f"{agreed} consensus, "
                    f"{len(grok_setups)} Grok, {len(manus_by_sym)} Manus")
        return merged

    def _next_desk_brief_window(self) -> str:
        """Return the next scheduled Desk Brief time as a human-readable string."""
        dc = self._curator_cfg()
        schedule = dc.get("desk_brief_schedule")
        if not schedule:
            return "interval-based"

        now = _utcnow()
        current_minutes = now.hour * 60 + now.minute

        for day_offset in range(3):
            check_date = now.date() + timedelta(days=day_offset)
            check_day = (now.weekday() + day_offset) % 7
            is_wknd = check_day >= 5
            hours = schedule.get("weekend_utc" if is_wknd else "weekday_utc",
                                 [5, 11, 18, 22])
            for h in sorted(hours):
                target_min = h * 60
                if day_offset == 0 and target_min <= current_minutes:
                    continue
                day_label = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][check_day]
                return f"{day_label} {h:02d}:00 UTC"

        return "unknown"

    # ── Grok Shadow Prediction Tracker ────────────────────────────

    def _check_grok_shadow_predictions(self):
        """Compare pending Grok trade setups against live prices.

        Resolves predictions as tp1_hit / tp2_hit / tp3_hit / sl_hit.
        Runs every tick — lightweight SQL + price check.
        """
        try:
            pending = self.db.fetch_all(
                "SELECT symbol, grok_direction, grok_entry, grok_stop_loss, "
                "grok_tp1, grok_tp2, grok_tp3, grok_setup_at "
                "FROM symbol_intel "
                "WHERE grok_setup_outcome = 'pending' "
                "AND grok_entry IS NOT NULL "
                "AND grok_setup_at IS NOT NULL "
                "AND duo_id = '_grok_setup'")
            if not pending:
                return

            expired_cutoff = _utcnow() - timedelta(hours=72)
            resolved = 0

            for row in pending:
                sym = row["symbol"]
                direction = row.get("grok_direction", "BUY")
                entry = float(row.get("grok_entry") or 0)
                sl = float(row.get("grok_stop_loss") or 0)
                tp1 = float(row.get("grok_tp1") or 0)
                tp2 = float(row.get("grok_tp2") or 0)
                tp3 = float(row.get("grok_tp3") or 0)
                setup_at = row.get("grok_setup_at")

                if not entry or not sl:
                    continue

                # Check if expired (72h window)
                if setup_at and setup_at < expired_cutoff:
                    self._resolve_grok_prediction(sym, "expired")
                    resolved += 1
                    continue

                # Get current price
                price = self._get_current_price(sym)
                if not price:
                    continue

                outcome = None
                if direction == "BUY":
                    if price <= sl:
                        outcome = "sl_hit"
                    elif price >= tp3:
                        outcome = "tp3_hit"
                    elif price >= tp2:
                        outcome = "tp2_hit"
                    elif price >= tp1:
                        outcome = "tp1_hit"
                else:  # SELL
                    if price >= sl:
                        outcome = "sl_hit"
                    elif price <= tp3:
                        outcome = "tp3_hit"
                    elif price <= tp2:
                        outcome = "tp2_hit"
                    elif price <= tp1:
                        outcome = "tp1_hit"

                if outcome:
                    self._resolve_grok_prediction(sym, outcome)
                    resolved += 1
                    logger.info(f"[Scout] Grok shadow: {sym} {direction} → {outcome} "
                                f"(price={price}, entry={entry})")

            if resolved:
                logger.info(f"[Scout] Grok shadow tracker: {resolved} predictions resolved")

        except Exception as e:
            logger.debug(f"[Scout] Grok shadow check: {e}")

    def _resolve_grok_prediction(self, symbol: str, outcome: str):
        """Mark a Grok prediction as resolved."""
        try:
            self.db.execute(
                "UPDATE symbol_intel SET grok_setup_outcome = %s, "
                "grok_setup_resolved_at = UTC_TIMESTAMP() "
                "WHERE symbol = %s AND duo_id = '_grok_setup' "
                "AND grok_setup_outcome = 'pending'",
                (outcome, symbol))
        except Exception as e:
            logger.debug(f"[Scout] _resolve_grok_prediction {symbol}: {e}")

    def _get_current_price(self, symbol: str) -> float:
        """Quick price lookup from any exchange for shadow tracking."""
        try:
            from services.exchange_manager import ExchangeManager
            em = ExchangeManager._instance if hasattr(ExchangeManager, '_instance') else None
            if not em:
                return 0.0
            for ex_name, executor in em._executors.items():
                try:
                    ticker = executor.exchange.fetch_ticker(symbol)
                    last = ticker.get("last", 0)
                    if last and last > 0:
                        return float(last)
                except Exception:
                    continue
        except Exception:
            pass
        return 0.0

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self):
        """Start the background discovery thread."""
        if not self._scout_cfg.get("enabled", False):
            logger.info("[Scout] Disabled in config, not starting")
            return

        if self._running:
            logger.warning("[Scout] Already running")
            return

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="Scout-Discovery")
        self._thread.start()
        logger.info("[Scout] Started discovery thread")

    def reload_config(self):
        """Hot-reload scout config from the config object (no restart needed)."""
        self._scout_cfg = self._load_scout_config()
        logger.info(f"[Scout] Config reloaded (enabled={self._scout_cfg.get('enabled', False)}, "
                    f"interval={self._scout_cfg.get('discovery_interval_minutes', 15)}m, "
                    f"routing={self._scout_cfg.get('routing_mode', 'round_robin')})")

    def stop(self):
        """Signal the discovery thread to stop."""
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        logger.info("[Scout] Stopped")

    # ── Main Loop ──────────────────────────────────────────────────

    def _loop(self):
        """Background loop: run _discovery_tick every interval."""
        while self._running and not self._stop_event.is_set():
            interval = self._scout_cfg.get("discovery_interval_minutes", 15) * 60
            try:
                self._discovery_tick()
            except Exception as e:
                logger.error(f"[Scout] Discovery tick error: {e}", exc_info=True)
            self._stop_event.wait(interval)

    def _evict_stale_cooldowns(self):
        """Sweep all cooldown entries regardless of whether they're checked this tick."""
        now = _utcnow()
        max_age = self._scout_cfg.get("discovery_interval_minutes", 15) * 60 * 2
        with self._cooldown_lock:
            for duo_id, cds in list(self._cooldowns.items()):
                expired = [s for s, t in cds.items()
                           if (now - t).total_seconds() > max_age]
                for k in expired:
                    del cds[k]

    def _discovery_tick(self):
        """One scan cycle: collect → regime → intel filter → CTS score → desk brief → route."""
        self._cycle_count += 1
        self._last_cycle_at = _utcnow()
        self._last_cycle_routed = 0
        self._rag_unavailable_this_tick = False
        self._evict_stale_cooldowns()
        logger.info(f"[Scout] === Tick #{self._cycle_count} ===")

        # Full regime computation at start of tick
        try:
            from services.market_regime import MarketRegime, store_regime_history, cleanup_regime_history
            from services.data_scientist import get_data_scientist
            ds = get_data_scientist(self.db)

            # Fetch altcoin M5 candles for divergence detection
            altcoin_prices = {}
            try:
                watchlist = self._get_global_watchlist()
                alt_symbols = [s for s in watchlist if s != "BTCUSDT"][:10]
                for sym in alt_symbols:
                    try:
                        c = ds.get_candles_from_db(sym, {"M5": 5})
                        m5 = c.get("M5", [])
                        if m5:
                            altcoin_prices[sym] = m5
                    except Exception:
                        pass
            except Exception:
                pass

            regime = MarketRegime().evaluate("BTCUSDT", ds, altcoin_prices=altcoin_prices)
            self._current_regime = regime
            btc_price = None
            try:
                btc_candles = ds.get_candles_from_db("BTCUSDT", {"M5": 1})
                m5 = btc_candles.get("M5", [])
                if m5:
                    btc_price = float(m5[-1].get("close", 0))
            except Exception:
                pass
            store_regime_history(self.db, regime, btc_price=btc_price, source="full")
            logger.info(f"[Scout] Full regime: {regime.get('score')} ({regime.get('label')})")
            if self._cycle_count % 96 == 1:
                cleanup_regime_history(self.db, retention_days=30)
        except Exception as e:
            logger.warning(f"[Scout] Regime computation failed (non-fatal): {e}")
            self._current_regime = None

        candidates = self._collect_candidates()
        if not candidates:
            logger.info("[Scout] No candidates this cycle")
            return

        si = self._si_cfg()
        if si.get("enabled", False):
            candidates = self._apply_symbol_intelligence(candidates)

        dc = self._curator_cfg()
        if dc.get("enabled", False) and dc.get("cts_enabled", False):
            candidates = self._compute_chart_scores(candidates)

        if dc.get("enabled", False) and dc.get("desk_brief_enabled", False):
            candidates = self._curate_desk_brief(candidates)

        routed = self._route_to_duos(candidates)
        self._last_cycle_routed = routed
        logger.info(f"[Scout] Tick #{self._cycle_count}: "
                    f"{len(candidates)} candidates -> {routed} routed to duos")

        # Check Grok shadow predictions against live prices
        if dc.get("enabled", False) and dc.get("desk_brief_deep_analysis", False):
            self._check_grok_shadow_predictions()

    # ── Candidate Collection ───────────────────────────────────────

    @staticmethod
    def _canonical(raw: str) -> str:
        """Fast, DB-free normalization for dedup: alias + crypto USDT conversion.

        Converts variant forms (BTC, BTCUSD, BTCUSDT.P) to a single canonical
        key (BTCUSDT) so the ``seen`` set catches duplicates before routing.
        """
        from db.market_symbols import SYMBOL_ALIASES, _normalize_crypto_to_usdt
        s = (raw or "").upper().strip()
        s = SYMBOL_ALIASES.get(s, s)
        usdt = _normalize_crypto_to_usdt(s)
        return usdt or s

    def _collect_candidates(self) -> List[Dict[str, Any]]:
        """Gather coins from all configured sources.

        Returns list of dicts: ``{symbol, source, direction?, signal?}``
        Symbols are normalized before dedup so variant forms (1INCH / 1INCHUSDT,
        SUI / SUIUSD / SUIUSDT) collapse to a single candidate.
        """
        sources = self._scout_cfg.get("sources",
                                      ["watchlist", "alpha", "mentors"])
        candidates: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        raw_count = 0

        if "watchlist" in sources:
            wl = self._get_global_watchlist()
            raw_count += len(wl)
            for sym in wl:
                canon = self._canonical(sym)
                if canon not in seen:
                    candidates.append({"symbol": canon, "source": "watchlist"})
                    seen.add(canon)

        if "alpha" in sources:
            alpha_syms = self._get_alpha_ideas()
            raw_count += len(alpha_syms)
            for sym in alpha_syms:
                canon = self._canonical(sym)
                if canon not in seen:
                    candidates.append({"symbol": canon, "source": "alpha"})
                    seen.add(canon)

        if "mentors" in sources:
            mentor_signals = self._get_pending_mentor_signals()
            raw_count += len(mentor_signals)
            mentor_seen: Set[str] = set()
            for sig in mentor_signals:
                sym = sig.get("symbol", "")
                if not sym:
                    continue
                canon = self._canonical(sym)
                if canon not in mentor_seen:
                    candidates.append({
                        "symbol": canon,
                        "source": "mentor",
                        "direction": sig.get("direction"),
                        "signal": sig,
                    })
                    mentor_seen.add(canon)
                    seen.add(canon)

        if "market_movers" in sources and self._scout_cfg.get("market_movers_enabled"):
            movers = self._get_market_movers()
            raw_count += len(movers)
            for sym in movers:
                canon = self._canonical(sym)
                if canon not in seen:
                    candidates.append({"symbol": canon, "source": "market_mover"})
                    seen.add(canon)

        deduped = raw_count - len(candidates)
        logger.info(f"[Scout] Collected {len(candidates)} candidates from "
                    f"{sources} ({raw_count} raw, {deduped} duplicates removed)")
        return candidates

    def _apply_symbol_intelligence(self,
                                   candidates: List[Dict]) -> List[Dict]:
        """Filter and rank candidates using the symbol_intel verdict cache.

        For each enabled duo:
          1. Batch-load intel rows for all candidate symbols.
          2. Skip symbols with fresh verdicts and no market change.
          3. Compute priority_rank for symbols that need a rescan.
          4. Sort by rank and cap at ``max_per_duo``.

        Mentor-sourced candidates always pass through (never filtered).
        Returns the filtered+ranked candidate list.
        """
        from core.duo_config import get_active_duo_ids
        si = self._si_cfg()
        max_per_duo = max(1, si.get("max_per_duo", 15))
        per_duo_cfg = self._scout_cfg.get("per_duo_routing", {})

        mentor_candidates = [c for c in candidates
                             if c.get("source") in ("mentor", "mentor_observation")]
        non_mentor = [c for c in candidates
                      if c.get("source") not in ("mentor", "mentor_observation")]

        if not non_mentor:
            return candidates

        duo_ids = get_active_duo_ids(self.config)
        if not duo_ids:
            logger.warning("[Scout] Symbol Intel: no active duos, "
                           "passing all candidates through unfiltered")
            return candidates

        symbols = [c["symbol"] for c in non_mentor]

        price_cache = self._batch_load_prices(symbols)
        volume_cache = self._batch_load_volumes(symbols)

        oldest_analysis = _utcnow() - timedelta(hours=48)
        signal_cache = self._batch_load_new_signals(symbols, oldest_analysis)

        scored: List[Dict] = []
        scored_syms: set = set()
        approvals: Dict[str, set] = {}
        skipped = 0

        for duo_id in duo_ids:
            intel_map = self._load_symbol_intel(symbols, duo_id)
            duo_routing = per_duo_cfg.get(duo_id, {})

            if intel_map is None:
                logger.warning(f"[Scout] Symbol Intel: DB query failed for "
                               f"{len(symbols)} symbols on {duo_id} — "
                               f"skipping duo this tick")
                continue

            duo_ranked = []
            for cand in non_mentor:
                sym = cand["symbol"]
                intel = intel_map.get(sym)

                should_build, reason = self._should_rescan(
                    sym, duo_id, intel, duo_routing,
                    price_cache=price_cache,
                    volume_cache=volume_cache,
                    signal_cache=signal_cache)

                if not should_build:
                    self._update_intel_skip(sym, duo_id, reason)
                    skipped += 1
                    continue

                rank = self._compute_priority_rank(
                    sym, cand["source"], intel)

                self._ensure_intel_row(sym, duo_id, cand["source"])
                self._persist_priority_rank(sym, duo_id, rank)

                duo_ranked.append((rank, cand))

            duo_ranked.sort(key=lambda t: t[0], reverse=True)
            top_cands = [cand for _, cand in duo_ranked[:max_per_duo]]

            if si.get("llm_advisor_enabled", False):
                top_cands = self._llm_advisor_refine(top_cands, duo_id)

            for cand in top_cands:
                sym = cand["symbol"]
                approvals.setdefault(sym, set()).add(duo_id)
                if sym not in scored_syms:
                    scored.append(cand)
                    scored_syms.add(sym)

        self._intel_approvals = approvals

        result = mentor_candidates + scored
        logger.info(
            f"[Scout] Symbol Intel: {len(non_mentor)} non-mentor candidates "
            f"-> {skipped} skipped (fresh), {len(scored)} to build "
            f"(max {max_per_duo}/duo), {len(mentor_candidates)} mentors pass-through")
        return result

    def _get_global_watchlist(self) -> List[str]:
        """Get the Scout's watchlist — exchange-verified USDT futures only.

        Primary source: symbols in ``market_symbols`` that have at least one
        exchange ticker (bybit_ticker, blofin_ticker, or bitget_ticker) populated.
        This ensures Scout only routes symbols tradeable on registered exchanges.
        Mentor signals bypass this filter (handled separately in _collect_candidates).

        Fallback: ``trade_decision.scout.watchlist`` from config.json.
        """
        try:
            rows = self.db.fetch_all("""
                SELECT symbol FROM market_symbols
                WHERE asset_class = 'cryptocurrency'
                  AND (bybit_ticker IS NOT NULL
                       OR blofin_ticker IS NOT NULL
                       OR bitget_ticker IS NOT NULL)
                ORDER BY RAND()
            """)
            if rows:
                symbols = [r["symbol"] for r in rows]
                logger.info(f"[Scout] Watchlist loaded from market_symbols: "
                            f"{len(symbols)} exchange-verified futures symbols")
                return symbols
        except Exception as e:
            logger.warning(f"[Scout] Failed to load exchange-verified symbols from DB: {e}")
        fallback = list(self._scout_cfg.get("watchlist", []))
        logger.info(f"[Scout] Using config fallback watchlist: {len(fallback)} symbols")
        return fallback

    def _get_alpha_ideas(self) -> List[str]:
        """Get recent alpha-tracked symbols from parsed_signals.

        Joins through user_follow_preferences (track_alpha=1) to find
        signals from sources the user is actively tracking for alpha ideas.
        """
        try:
            rows = self.db.fetch_all("""
                SELECT DISTINCT ps.symbol
                FROM parsed_signals ps
                JOIN user_follow_preferences ufp
                    ON ufp.author = ps.author AND ufp.track_alpha = 1
                WHERE ps.parsed_at > DATE_SUB(NOW(), INTERVAL 8 HOUR)
                  AND ps.symbol IS NOT NULL AND ps.symbol != ''
            """)
            symbols = [r["symbol"] for r in (rows or [])]
            if symbols:
                logger.info(f"[Scout] Alpha ideas: {len(symbols)} symbols from tracked sources")
            return symbols
        except Exception as e:
            logger.warning(f"[Scout] Alpha ideas fetch error: {e}")
            return []

    def _get_pending_mentor_signals(self) -> List[Dict]:
        """Get recent mentor signals not yet processed.

        Joins parsed_signals → user_profile_links (on author = source_username)
        → user_profiles (on user_profile_id). Filters by is_mentor, trading_enabled,
        8-hour window, and excludes signals already linked to dossiers.
        """
        try:
            rows = self.db.fetch_all("""
                SELECT ps.id, ps.symbol, ps.direction,
                       ps.entry_price, ps.stop_loss,
                       ps.take_profit_1, ps.take_profit_2, ps.take_profit_3,
                       ps.take_profit_4, ps.take_profit_5, ps.take_profit_6,
                       ANY_VALUE(up.display_name) as author, ps.author as raw_author,
                       ps.news_item_id, ps.confidence, ps.source,
                       ps.raw_text, ps.ai_reasoning, ps.source_detail
                FROM parsed_signals ps
                JOIN user_profile_links upl ON ps.author = upl.source_username
                JOIN user_profiles up ON upl.user_profile_id = up.id
                WHERE up.is_mentor = 1
                  AND COALESCE(up.mentor_trading_enabled, 1) = 1
                  AND ps.parsed_at > DATE_SUB(NOW(), INTERVAL 8 HOUR)
                  AND ps.symbol IS NOT NULL
                  AND ps.symbol != ''
                  AND NOT EXISTS (
                      SELECT 1 FROM trade_dossiers td
                      WHERE td.linked_signal_id = ps.id
                  )
                GROUP BY ps.id
                ORDER BY ps.parsed_at DESC
            """)
            signals = [dict(r) for r in (rows or [])]

            # Smart Queue: filter out blacklisted mentors at source (saves LLM costs)
            try:
                bl_row = self.db.fetch_one(
                    "SELECT config_value FROM shadow_config WHERE config_key = 'shadow_mentor_blacklist'")
                if bl_row and bl_row.get("config_value"):
                    mentor_bl = json.loads(bl_row["config_value"])
                    if mentor_bl:
                        mentor_bl_lower = {m.lower() for m in mentor_bl}
                        before = len(signals)
                        signals = [s for s in signals
                                   if (s.get("raw_author") or "").lower() not in mentor_bl_lower]
                        skipped = before - len(signals)
                        if skipped:
                            logger.info(f"[Scout] Mentor blacklist filtered {skipped} signals "
                                        f"(blacklist: {mentor_bl})")
            except Exception as e:
                logger.debug(f"[Scout] Mentor blacklist check: {e}")

            logger.info(f"[Scout] Found {len(signals)} pending mentor signals")
            return signals
        except Exception as e:
            logger.warning(f"[Scout] Mentor signals fetch error: {e}")
            return []

    def _get_market_movers(self) -> List[str]:
        """Get top market movers (placeholder for CCXT integration)."""
        top_n = self._scout_cfg.get("market_movers_top_n", 5)
        try:
            rows = self.db.fetch_all("""
                SELECT symbol FROM market_symbols
                WHERE tradable = 1 AND asset_class = 'cryptocurrency'
                ORDER BY RAND() LIMIT %s
            """, (top_n,))
            return [r["symbol"] for r in (rows or [])]
        except Exception as e:
            logger.debug(f"[Scout] Market movers: {e}")
            return []

    # ── Duo Routing ────────────────────────────────────────────────

    def _get_eligible_duos(self, symbol: str, floors: Dict,
                           per_duo_cfg: Dict,
                           skip_intel: bool = False) -> List[str]:
        """Return an ordered list of duo_ids that can receive *symbol* right now.

        Gates applied (in order per duo):
          1. Duo enabled in config
          2. Symbol Intelligence approval (unless *skip_intel*)
          3. Per-duo cooldown not active
          3b. Trading floor abandon-cooldown not active
          4. At least one account the duo can trade this symbol on
          5. Not at max-active dossiers for this symbol

        The ordering matches ``get_active_duo_ids`` so the round-robin
        index produces a deterministic rotation.
        """
        from core.duo_config import is_duo_enabled, get_active_duo_ids

        ordered_ids = get_active_duo_ids(self.config)
        eligible: List[str] = []

        for duo_id in ordered_ids:
            if duo_id not in floors:
                continue
            if not is_duo_enabled(self.config, duo_id):
                continue

            if not skip_intel and self._intel_approvals:
                approved = self._intel_approvals.get(symbol, set())
                if approved and duo_id not in approved:
                    continue

            duo_routing = per_duo_cfg.get(duo_id, {})
            if self._in_cooldown(symbol, duo_id, duo_routing):
                continue

            tf = floors.get(duo_id)
            if tf and hasattr(tf, "_discovery_cooldown"):
                abandon_cd_min = int(self._scout_cfg.get(
                    "abandon_cooldown_minutes",
                    self._scout_cfg.get("discovery_cooldown_minutes", 30)))
                canon = self._canonical(symbol)
                last_abandon = tf._discovery_cooldown.get(canon)
                if last_abandon:
                    elapsed = (_utcnow() - last_abandon).total_seconds()
                    if elapsed < abandon_cd_min * 60:
                        continue

            if not self._check_account_access(symbol, duo_id):
                continue

            if self._at_max_active(symbol, duo_id):
                continue

            eligible.append(duo_id)

        return eligible

    def _pick_rr_duo(self, eligible: List[str]) -> Optional[str]:
        """Choose one duo from *eligible* using the round-robin counter.

        Advances ``_rr_index`` so the next call rotates to the next duo.
        If *eligible* has only one duo, returns it without advancing.
        """
        if not eligible:
            return None
        if len(eligible) == 1:
            return eligible[0]

        from core.duo_config import get_active_duo_ids
        all_duos = get_active_duo_ids(self.config)
        if not all_duos:
            return eligible[0]

        chosen = None
        attempts = len(all_duos)
        while attempts > 0:
            target = all_duos[self._rr_index % len(all_duos)]
            self._rr_index += 1
            if target in eligible:
                chosen = target
                break
            attempts -= 1

        return chosen or eligible[0]

    def _route_to_duos(self, candidates: List[Dict]) -> int:
        """Route candidates to duos via fair round-robin or legacy broadcast.

        **round_robin** (default): Each non-mentor candidate is assigned to
        exactly ONE duo, rotating through eligible duos so every desk gets
        equal opportunity.  If the assigned duo fails dispatch, the symbol
        falls through to the next eligible duo in rotation order.

        **broadcast** (legacy): Every eligible duo receives every candidate.

        Mentor signals always go to ALL eligible duos regardless of mode.

        Returns total number of (symbol, duo) pairs routed.
        """
        from services.trading_floor import get_all_trading_floors

        floors = get_all_trading_floors()
        if not floors:
            logger.warning("[Scout] No TradingFloorService instances available")
            return 0

        per_duo_cfg = self._scout_cfg.get("per_duo_routing", {})
        routing_mode = self._scout_cfg.get("routing_mode", "round_robin")
        total_routed = 0

        for candidate in candidates:
            symbol = candidate["symbol"]
            source = candidate["source"]
            is_mentor = source in ("mentor", "mentor_observation")

            eligible = self._get_eligible_duos(
                symbol, floors, per_duo_cfg, skip_intel=is_mentor)

            if not eligible:
                logger.info(f"[Scout] {symbol} ({source}) — no eligible duo "
                            f"(account access / cooldown / max-active)")
                continue

            if is_mentor or routing_mode == "broadcast":
                routed_any = False
                for duo_id in eligible:
                    tf = floors[duo_id]
                    if self._dispatch_to_duo(tf, candidate):
                        self._set_cooldown(symbol, duo_id)
                        total_routed += 1
                        routed_any = True
                        self._rr_stats[duo_id] = self._rr_stats.get(duo_id, 0) + 1
                        self._log_activity(f"routed:{duo_id}", symbol,
                            f"{source} -> {duo_id} ({'mentor' if is_mentor else 'broadcast'})")
                if not routed_any:
                    logger.info(f"[Scout] {symbol} ({source}) — dispatch failed "
                                f"for all {len(eligible)} eligible duos")
            else:
                chosen = self._pick_rr_duo(eligible)
                if not chosen:
                    logger.info(f"[Scout] {symbol} ({source}) — round-robin "
                                f"returned no duo from {eligible}")
                    continue

                tf = floors[chosen]
                if self._dispatch_to_duo(tf, candidate):
                    self._set_cooldown(symbol, chosen)
                    total_routed += 1
                    self._rr_stats[chosen] = self._rr_stats.get(chosen, 0) + 1
                    self._log_activity(f"routed:{chosen}", symbol,
                        f"{source} -> {chosen} (rr)")
                else:
                    fallback_duos = [d for d in eligible if d != chosen]
                    dispatched = False
                    for fb_duo in fallback_duos:
                        tf_fb = floors[fb_duo]
                        if self._dispatch_to_duo(tf_fb, candidate):
                            self._set_cooldown(symbol, fb_duo)
                            total_routed += 1
                            self._rr_stats[fb_duo] = self._rr_stats.get(fb_duo, 0) + 1
                            self._log_activity(f"routed:{fb_duo}", symbol,
                                f"{source} -> {fb_duo} (rr-fallback)")
                            dispatched = True
                            break
                    if not dispatched:
                        logger.info(f"[Scout] {symbol} ({source}) — "
                                    f"round-robin + fallback exhausted")

        if total_routed > 0 and routing_mode == "round_robin":
            dist = ", ".join(f"{d}={c}" for d, c in sorted(self._rr_stats.items()))
            logger.info(f"[Scout] Round-robin distribution this session: {dist}")

        return total_routed

    def _in_cooldown(self, symbol: str, duo_id: str,
                     duo_routing: Dict) -> bool:
        """Check if the symbol is in cooldown for this duo. Thread-safe.
        Also evicts expired entries (TTL = 2x cooldown) to prevent unbounded growth.
        Uses canonical symbol so variant forms share a single cooldown."""
        canon = self._canonical(symbol)
        cooldown_min = duo_routing.get("cooldown_minutes",
                        self._scout_cfg.get("discovery_interval_minutes", 15))
        ttl_sec = cooldown_min * 60 * 2
        now = _utcnow()
        with self._cooldown_lock:
            duo_cds = self._cooldowns.get(duo_id, {})
            expired_keys = [s for s, t in duo_cds.items()
                            if (now - t).total_seconds() > ttl_sec]
            for k in expired_keys:
                del duo_cds[k]
            last_build = duo_cds.get(canon)
        if last_build:
            elapsed = (now - last_build).total_seconds()
            if elapsed < cooldown_min * 60:
                return True
        return False

    def _set_cooldown(self, symbol: str, duo_id: str):
        """Record that a dossier build was dispatched for this symbol+duo. Thread-safe.
        Stores under canonical symbol so variant forms share a single cooldown."""
        canon = self._canonical(symbol)
        with self._cooldown_lock:
            if duo_id not in self._cooldowns:
                self._cooldowns[duo_id] = {}
            self._cooldowns[duo_id][canon] = _utcnow()

    def _get_cooldowns_snapshot(self) -> dict:
        """Thread-safe snapshot of cooldown state for status reporting."""
        with self._cooldown_lock:
            return {
                duo_id: {sym: ts.isoformat() for sym, ts in cds.items()}
                for duo_id, cds in self._cooldowns.items()
            }

    def _at_max_active(self, symbol: str, duo_id: str) -> bool:
        """Check if duo already has max *uncommitted* dossiers for this symbol.

        Only ``proposed`` and ``monitoring`` count toward the cap.
        ``open_order`` and ``live`` are committed positions that should
        not block new analysis from being queued.
        Uses canonical symbol so variant forms share the same count.
        Returns True (blocked) on exception to fail-safe (prevent runaway builds).
        """
        from core.duo_config import get_duo_config
        canon = self._canonical(symbol)
        duo_cfg = get_duo_config(self.config, duo_id)
        max_active = duo_cfg.get("max_active_dossiers_per_symbol", 2)
        try:
            row = self.db.fetch_one(
                "SELECT COUNT(*) as cnt FROM trade_dossiers "
                "WHERE symbol = %s AND duo_id = %s "
                "AND status IN ('proposed','monitoring')",
                (canon, duo_id))
            return row and int(row["cnt"]) >= max_active
        except Exception as e:
            logger.error(f"[Scout] _at_max_active DB error for {canon}/{duo_id}: {e}")
            return True

    def _check_account_access(self, symbol: str, duo_id: str) -> bool:
        """Check if this duo has at least one account that can trade
        this symbol on its exchange.  Uses canonical symbol for consistent
        exchange resolution.

        Checks:
            1. Which accounts are assigned to this duo (``duo_allowed`` on
               ``trading_accounts``; empty/NULL means no duos allowed)
            2. For each eligible account, can the exchange trade this symbol?
        """
        symbol = self._canonical(symbol)
        try:
            accounts = self.db.fetch_all(
                "SELECT account_id, exchange, duo_allowed FROM trading_accounts "
                "WHERE enabled = 1 AND live_trading = 1")
            if not accounts:
                return False

            from db.market_symbols import can_trade_on_exchange

            for acct in accounts:
                allowed_list = _parse_duo_allowed(acct.get("duo_allowed"))
                if not allowed_list or duo_id not in allowed_list:
                    continue

                exchange = acct.get("exchange", "")
                can_trade, _ = can_trade_on_exchange(symbol, exchange, self.db)
                if can_trade:
                    return True

            return False
        except Exception as e:
            logger.warning(f"[Scout] Account access check failed for "
                           f"{symbol}/{duo_id}: {e}")
            return False

    def _dispatch_to_duo(self, tf, candidate: Dict) -> bool:
        """Queue a dossier build on a duo's TradingFloorService.

        Returns True on success so the caller knows whether to set cooldown.
        """
        symbol = candidate["symbol"]
        source = candidate["source"]
        signal = candidate.get("signal")

        try:
            if self._current_regime and hasattr(tf, '_market_regime'):
                tf._market_regime = self._current_regime
            if hasattr(tf, 'queue_dossier_build'):
                tf.queue_dossier_build(symbol, source=source, mentor_signal=signal)
            else:
                self._direct_build(tf, symbol, source, signal)
            return True
        except Exception as e:
            logger.error(f"[Scout] _dispatch_to_duo failed for {symbol} -> "
                         f"{getattr(tf, 'duo_id', '?')}: {e}")
            return False

    def _direct_build(self, tf, symbol: str, source: str,
                      signal: Optional[Dict] = None):
        """Direct dossier build when queue method isn't available."""
        try:
            from services.trade_dossier import TradeDossierBuilder
            from services.candle_collector import get_candle_collector

            collector = get_candle_collector()
            builder = TradeDossierBuilder(
                tf.db, tf.config, collector, duo_id=tf.duo_id)

            regime = getattr(tf, '_market_regime', None) or self._current_regime
            is_mentor = source in ("mentor", "mentor_observation")
            dossier = builder.build_dossier(
                symbol,
                mentor_triggered=(source == "mentor"),
                mentor_signal=signal if source == "mentor" else None,
                market_regime=regime,
            )

            if source == "mentor_observation" and dossier and dossier.get("dossier_id"):
                tf.db.execute(
                    "UPDATE trade_dossiers SET "
                    "mentor_type = 'mentor_observation', "
                    "mentor_source = %s, "
                    "paper_reason = 'mentor_observation' "
                    "WHERE id = %s",
                    (signal.get("author", "Mentor") if signal else "Mentor",
                     dossier["dossier_id"]))

            logger.info(f"[Scout] Built dossier for {symbol} "
                       f"via {tf.duo_id} (source={source})")
        except Exception as e:
            logger.error(f"[Scout] Direct build failed for {symbol}/{tf.duo_id}: {e}")

    # ── Mentor-Specific Routing ────────────────────────────────────

    def route_mentor_signal(self, signal: Dict) -> List[str]:
        """Route a single mentor signal: mirror executes, duos observe.

        Flow:
            1. Create a mechanical mirror dossier (no AI, ``duo_id='mentors'``)
               that goes straight to execution on any eligible account.
            2. Route the same signal to ALL active duos as **observation only**.
               Each duo builds its own independent AI dossier tagged
               ``mentor_type='mentor_observation'`` — it never executes,
               only generates learning material for postmortems.

        Returns list of duo_ids that received the observation signal.
        """
        from services.trading_floor import get_all_trading_floors, get_trading_floor
        from core.duo_config import is_duo_enabled

        symbol = signal.get("symbol", "")
        if not symbol:
            return []

        floors = get_all_trading_floors()
        routed_to: List[str] = []

        try:
            mentor_accounts = self.db.fetch_all(
                "SELECT account_id, exchange, duo_allowed FROM trading_accounts "
                "WHERE enabled = 1 AND live_trading = 1 AND mentor_enabled = 1")
        except Exception as e:
            logger.error(f"[Scout] Mentor account fetch: {e}")
            return []

        if not mentor_accounts:
            return []

        from db.market_symbols import can_trade_on_exchange

        has_exchange_access = False
        for acct in mentor_accounts:
            exchange = acct.get("exchange", "")
            can_trade, _ = can_trade_on_exchange(symbol, exchange, self.db)
            if can_trade:
                has_exchange_access = True
                break

        if not has_exchange_access:
            logger.info(f"[Scout] Mentor signal {symbol}: no exchange can trade "
                        f"this symbol — mirror skipped, observation only")

        # ── Step 1: Create mechanical mirror dossier (no AI) ──
        if has_exchange_access:
            self._create_mentor_mirror(signal, symbol)

        # ── Step 2: Route to ALL duos as observation-only ──
        for duo_id, tf in floors.items():
            if not is_duo_enabled(self.config, duo_id):
                continue

            per_duo_cfg = self._scout_cfg.get("per_duo_routing", {})
            duo_routing = per_duo_cfg.get(duo_id, {})
            if self._in_cooldown(symbol, duo_id, duo_routing):
                logger.debug(f"[Scout] Mentor {symbol}: {duo_id} in cooldown, skipped")
                continue

            candidate = {
                "symbol": symbol,
                "source": "mentor_observation",
                "direction": signal.get("direction"),
                "signal": signal,
            }
            if self._dispatch_to_duo(tf, candidate):
                self._set_cooldown(symbol, duo_id)
                routed_to.append(duo_id)
                logger.info(f"[Scout] Mentor signal {symbol} -> {duo_id} (observation)")

        return routed_to

    def _create_mentor_mirror(self, signal: Dict, symbol: str):
        """Create a mechanical mirror dossier that copies the mentor's exact
        trade levels and goes straight to execution.  Zero LLM cost.

        Uses the first available TradingFloorService to access the mirror
        builder, but tags the dossier with ``duo_id='mentors'`` so it
        appears on the Mentors equity line, not under any duo.
        """
        from services.trading_floor import get_all_trading_floors

        floors = get_all_trading_floors()
        if not floors:
            logger.warning("[Scout] No TF instance available for mentor mirror")
            return

        tf = next(iter(floors.values()))
        try:
            from services.candle_collector import get_candle_collector
            from services.trade_dossier import TradeDossierBuilder

            collector = get_candle_collector()
            builder = TradeDossierBuilder(
                tf.db, tf.config, collector, duo_id="mentors")

            try:
                dossier = builder.build_dossier(
                    symbol, mentor_triggered=True, mentor_signal=signal)
                if dossier and dossier.get("dossier_id", -1) > 0:
                    logger.info(f"[Scout] Mentor mirror dossier #{dossier['dossier_id']} "
                                f"built for {symbol} (duo_id=mentors)")
            except Exception as build_err:
                logger.error(f"[Scout] Mentor mirror build failed: {build_err}")

            author = signal.get("author", "Mentor")
            logger.info(f"[Scout] Mentor mirror created for {symbol} "
                        f"by {author} (duo_id=mentors, straight to execution)")
        except Exception as e:
            logger.error(f"[Scout] Mentor mirror creation failed for "
                         f"{symbol}: {e}", exc_info=True)

    # ── Status / Dashboard ─────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Return Scout status for the dashboard API."""
        dc = self._curator_cfg()
        return {
            "enabled": self._scout_cfg.get("enabled", False),
            "running": self._running,
            "cycle_count": self._cycle_count,
            "last_cycle_at": self._last_cycle_at.isoformat() if self._last_cycle_at else None,
            "last_cycle_routed": self._last_cycle_routed,
            "cooldowns": self._get_cooldowns_snapshot(),
            "sources": self._scout_cfg.get("sources", []),
            "interval_minutes": self._scout_cfg.get("discovery_interval_minutes", 15),
            "routing_mode": self._scout_cfg.get("routing_mode", "round_robin"),
            "rr_index": self._rr_index,
            "rr_distribution": dict(self._rr_stats),
            "desk_curator": {
                "enabled": dc.get("enabled", False),
                "cts_enabled": dc.get("cts_enabled", False),
                "cts_minimum": dc.get("cts_minimum", 35),
                "desk_brief_enabled": dc.get("desk_brief_enabled", False),
                "desk_brief_model": dc.get("desk_brief_model", "n/a"),
                "desk_brief_schedule": dc.get("desk_brief_schedule", {}),
                "last_desk_brief_at": (self._last_desk_brief_at.isoformat()
                                       if self._last_desk_brief_at else None),
                "next_desk_brief_window": self._next_desk_brief_window(),
            },
            "activity_log": list(self._activity_log)[-20:],
        }

    def _log_activity(self, event: str, symbol: str, detail: str = ""):
        """Record scout activity for dashboard display."""
        self._activity_log.append({
            "ts": _utcnow().isoformat() + "Z",
            "event": event,
            "symbol": symbol,
            "detail": detail,
        })


# ── Module-level singleton ──────────────────────────────────────────

_scout_instance: Optional[ScoutAgent] = None
_scout_lock = threading.Lock()


def get_scout(db=None, config=None) -> Optional[ScoutAgent]:
    """Get or create the Scout singleton. Thread-safe via double-checked lock."""
    global _scout_instance
    if _scout_instance is None and db is not None:
        with _scout_lock:
            if _scout_instance is None:
                _scout_instance = ScoutAgent(db, config)
    return _scout_instance


def start_scout(db, config) -> Optional[ScoutAgent]:
    """Create and start Scout if enabled in config."""
    scout = get_scout(db, config)
    if scout:
        scout.start()
    return scout
