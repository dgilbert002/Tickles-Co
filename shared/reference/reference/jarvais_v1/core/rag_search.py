"""
JarvAIs — RAG Search Engine (Bidirectional + Hybrid)
Unified search for humans AND agents. Combines:
  1. Semantic search (vector similarity via Qdrant/Pinecone)
  2. Keyword search (direct DB text matching as fallback/supplement)
Supports lineage-enriched results and logs agent queries for accountability.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("jarvais.rag")

# Stopwords stripped before keyword DB search — keeps only meaningful terms.
# IMPORTANT: Domain terms (signal, trade, price, market, analysis) are intentionally
# KEPT because they are meaningful search qualifiers in a trading system.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "what", "whats", "where", "when", "how", "why", "who", "which",
    "that", "this", "these", "those", "it", "its",
    "i", "me", "my", "you", "your", "we", "our", "us", "they", "them", "their",
    "he", "she", "him", "her", "his", "hers",
    "in", "on", "at", "to", "for", "of", "with", "by", "from", "up", "about",
    "into", "through", "during", "before", "after", "above", "below", "between",
    "out", "off", "over", "under", "again", "further", "then", "once",
    "and", "but", "or", "nor", "not", "no", "so", "very", "just", "also",
    "more", "most", "some", "any", "all", "both", "each", "few", "other",
    "than", "too", "own", "same", "as", "only", "such",
    "do", "does", "did", "doing", "have", "has", "had", "having",
    "would", "could", "should", "may", "might", "can", "will", "shall",
    "here", "there", "now", "tell", "give", "get", "got", "know", "like",
    "make", "find", "say", "said", "go", "going", "come", "take", "see",
    "look", "think", "want", "need", "use", "try", "ask", "show", "let",
    "keep", "start", "help", "new", "first",
    "old", "long", "great", "little", "right", "big", "different",
    "small", "large", "next", "early", "young", "important", "public",
    "bad", "good", "please", "thanks", "thank", "hey", "hi", "hello",
    "yes", "yeah", "ok", "okay", "sure", "well",
    "happening", "happened", "info", "information",
})


def _recency_boost(timestamp) -> float:
    """Score boost from 0.0 to 1.0 based on how recent the item is.
    Items from last hour get ~1.0, last day ~0.7, last week ~0.4, older ~0.1."""
    if not timestamp:
        return 0.1
    try:
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if not isinstance(timestamp, datetime):
            return 0.1
        now = datetime.now(timestamp.tzinfo) if timestamp.tzinfo else datetime.now()
        age_hours = max(0, (now - timestamp).total_seconds() / 3600)
        if age_hours < 1:
            return 1.0
        if age_hours < 6:
            return 0.9
        if age_hours < 24:
            return 0.7
        if age_hours < 72:
            return 0.5
        if age_hours < 168:
            return 0.3
        return 0.1
    except Exception:
        return 0.1


def _importance_score(payload: dict) -> float:
    """FinMem-lite importance score from 0.0 to 1.0.
    Computed from PnL magnitude and outcome quality stored in vector metadata.
    Wins with clear lessons get 1.5x weight, losses with lessons get 1.2x.
    Falls back to 0.5 (neutral) when no importance metadata is present."""
    raw = payload.get("importance")
    if raw is not None:
        try:
            return max(0.0, min(1.0, float(raw)))
        except (ValueError, TypeError):
            pass
    # Infer from PnL metadata if present but no pre-computed importance
    pnl = payload.get("pnl_pct") or payload.get("realised_pnl_pct")
    if pnl is not None:
        try:
            abs_pnl = abs(float(pnl))
            base = min(1.0, abs_pnl / 10.0)  # 10% PnL = max importance
            outcome = str(payload.get("status", "")).lower()
            if outcome == "won":
                base = min(1.0, base * 1.5)
            elif outcome == "lost" and payload.get("lessons_learned"):
                base = min(1.0, base * 1.2)
            return round(base, 3)
        except (ValueError, TypeError):
            pass
    return 0.5


def _tf_label(minutes: int) -> str:
    """Human-readable timeframe label from minutes."""
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 1440:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"


@dataclass
class SearchResult:
    """A single RAG search result."""
    text: str
    score: float
    collection: str
    source_table: str = ""
    source_id: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: Optional[str] = None
    lineage: Optional[List[Dict]] = None


class RagSearchEngine:
    """
    Hybrid RAG search — semantic (vector) + keyword (DB text).
    When semantic scores are weak, automatically supplements with direct
    database text search so topics like 'Iran' actually return Iran content.
    """

    def __init__(self, vector_store=None, embedder=None, db=None):
        self._store = vector_store
        self._embedder = embedder
        self._db = db
        self._hybrid_cfg = None

    def _get_store(self):
        if self._store:
            return self._store
        from core.vector_store import get_vector_store
        self._store = get_vector_store()
        return self._store

    def _get_embedder(self):
        if self._embedder:
            return self._embedder
        from core.embeddings import get_embedder
        self._embedder = get_embedder()
        return self._embedder

    def _get_db(self):
        if self._db:
            return self._db
        from db.database import get_db
        self._db = get_db()
        return self._db

    def _get_hybrid_config(self) -> Dict:
        """Load hybrid search config from config.json (cached per instance)."""
        if self._hybrid_cfg is not None:
            return self._hybrid_cfg
        try:
            from core.config import load_config
            cfg = load_config()
            self._hybrid_cfg = cfg.get("global", {}).get("rag_search", {})
        except Exception:
            self._hybrid_cfg = {}
        return self._hybrid_cfg

    def _get_rag_weights(self) -> tuple:
        """Load RAG scoring weights from system_config (CEO/Ledger adjustable).
        Returns (w_similarity, w_recency, w_importance). Falls back to defaults."""
        try:
            db = self._get_db()
            rows = db.fetch_all(
                "SELECT config_key, config_value FROM system_config "
                "WHERE config_key IN ('rag_weight_similarity', 'rag_weight_recency', 'rag_weight_importance')")
            wmap = {r["config_key"]: float(r["config_value"]) for r in (rows or [])}
            return (
                wmap.get("rag_weight_similarity", 0.50),
                wmap.get("rag_weight_recency", 0.20),
                wmap.get("rag_weight_importance", 0.30),
            )
        except Exception:
            return (0.50, 0.20, 0.30)

    # ─────────────────────────────────────────────────────────────
    # Keyword extraction
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def extract_keywords(query: str, max_keywords: int = 6) -> List[str]:
        """Pull meaningful words from a query for DB text search."""
        tokens = re.findall(r"[a-zA-Z0-9/]{2,}", query.lower())
        keywords = [t for t in tokens if t not in _STOPWORDS]
        return keywords[:max_keywords]

    # ─────────────────────────────────────────────────────────────
    # Keyword DB search
    # ─────────────────────────────────────────────────────────────

    def keyword_search_db(self, keywords: List[str],
                          limit: int = 20) -> List[SearchResult]:
        """
        Direct text search against news_items, parsed_signals, and alpha_summaries.
        Matches against headline, detail, AND ai_analysis fields.
        Scores reflect actual keyword match ratio with recency boost.
        """
        if not keywords:
            return []

        results: List[SearchResult] = []
        db = self._get_db()
        kw_list = keywords[:6]

        # ── Search news_items (headline + detail + ai_analysis) ──────────
        results.extend(self._keyword_search_news(db, kw_list, limit))

        # ── Search parsed_signals ────────────────────────────────────────
        results.extend(self._keyword_search_signals(db, kw_list, limit // 2))

        # ── Search alpha_summaries ───────────────────────────────────────
        results.extend(self._keyword_search_summaries(db, kw_list, limit // 2))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]

    def _keyword_search_news(self, db, kw_list: List[str],
                             limit: int) -> List[SearchResult]:
        """Keyword search against news_items including ai_analysis field."""
        match_exprs = []
        params: list = []
        for kw in kw_list:
            match_exprs.append(
                "IF(LOWER(headline) LIKE %s OR LOWER(detail) LIKE %s "
                "OR LOWER(ai_analysis) LIKE %s, 1, 0)"
            )
            params.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%"])

        match_sum = " + ".join(match_exprs)
        min_matches = min(2, len(kw_list))

        try:
            rows = db.fetch_all(f"""
                SELECT id, source, author, headline,
                       LEFT(detail, 800) AS detail,
                       LEFT(ai_analysis, 2000) AS ai_analysis,
                       published_at, collected_at, source_detail,
                       ({match_sum}) AS kw_matches
                FROM news_items
                WHERE ({match_sum}) >= %s
                ORDER BY kw_matches DESC, collected_at DESC
                LIMIT %s
            """, (*params, *params, min_matches, limit))
        except Exception as e:
            logger.debug(f"[RAG] Keyword news search error: {e}")
            return []

        results: List[SearchResult] = []
        for r in (rows or []):
            text = self._build_news_text(r)
            kw_matched = r.get("kw_matches", 1)
            base_score = kw_matched / max(len(kw_list), 1)
            recency = _recency_boost(r.get("collected_at") or r.get("published_at"))
            score = round(min(base_score * 0.7 + recency * 0.3, 0.95), 3)

            results.append(SearchResult(
                text=text,
                score=score,
                collection="db_keyword",
                source_table="news_items",
                source_id=r["id"],
                metadata={
                    "source": r.get("source", ""),
                    "author": r.get("author", ""),
                    "search_type": "keyword",
                    "keywords_matched": kw_matched,
                },
                timestamp=str(r.get("collected_at", "") or r.get("published_at", "")),
            ))
        return results

    def _keyword_search_signals(self, db, kw_list: List[str],
                                limit: int) -> List[SearchResult]:
        """Keyword search against parsed_signals."""
        match_exprs = []
        params: list = []
        for kw in kw_list:
            match_exprs.append(
                "IF(LOWER(symbol) LIKE %s OR LOWER(raw_text) LIKE %s "
                "OR LOWER(ai_reasoning) LIKE %s OR LOWER(author) LIKE %s, 1, 0)"
            )
            params.extend([f"%{kw}%", f"%{kw}%", f"%{kw}%", f"%{kw}%"])

        match_sum = " + ".join(match_exprs)

        try:
            rows = db.fetch_all(f"""
                SELECT id, symbol, direction, entry_price, stop_loss,
                       take_profit_1, take_profit_2, take_profit_3,
                       confidence, source, author, status, outcome,
                       LEFT(ai_reasoning, 500) AS ai_reasoning,
                       parsed_at,
                       ({match_sum}) AS kw_matches
                FROM parsed_signals
                WHERE ({match_sum}) >= 1
                ORDER BY kw_matches DESC, parsed_at DESC
                LIMIT %s
            """, (*params, *params, limit))
        except Exception as e:
            logger.debug(f"[RAG] Keyword signal search error: {e}")
            return []

        results: List[SearchResult] = []
        for r in (rows or []):
            text = (
                f"[Signal] {r.get('symbol','')} {r.get('direction','')} "
                f"@ {r.get('entry_price','')} SL={r.get('stop_loss','')} "
                f"TP1={r.get('take_profit_1','')} TP2={r.get('take_profit_2','')} "
                f"TP3={r.get('take_profit_3','')} "
                f"confidence={r.get('confidence','')}% "
                f"status={r.get('status','')} outcome={r.get('outcome','pending')} "
                f"source={r.get('source','')}/{r.get('author','')}"
            )
            if r.get("ai_reasoning"):
                text += f"\nReasoning: {r['ai_reasoning']}"

            kw_matched = r.get("kw_matches", 1)
            base_score = kw_matched / max(len(kw_list), 1)
            recency = _recency_boost(r.get("parsed_at"))
            score = round(min(base_score * 0.7 + recency * 0.3, 0.95), 3)

            results.append(SearchResult(
                text=text,
                score=score,
                collection="db_keyword_signals",
                source_table="parsed_signals",
                source_id=r["id"],
                metadata={
                    "source": r.get("source", ""),
                    "author": r.get("author", ""),
                    "symbol": r.get("symbol", ""),
                    "direction": r.get("direction", ""),
                    "search_type": "keyword",
                    "keywords_matched": kw_matched,
                },
                timestamp=str(r.get("parsed_at", "")),
            ))
        return results

    def _keyword_search_summaries(self, db, kw_list: List[str],
                                  limit: int) -> List[SearchResult]:
        """Keyword search against alpha_summaries."""
        match_exprs = []
        params: list = []
        for kw in kw_list:
            match_exprs.append("IF(LOWER(summary_text) LIKE %s, 1, 0)")
            params.append(f"%{kw}%")

        match_sum = " + ".join(match_exprs)

        try:
            rows = db.fetch_all(f"""
                SELECT id, pane_id, timeframe_minutes,
                       LEFT(summary_text, 3000) AS summary_text,
                       symbols_mentioned, sentiment_overall, created_at,
                       ({match_sum}) AS kw_matches
                FROM alpha_summaries
                WHERE ({match_sum}) >= 1
                ORDER BY kw_matches DESC, created_at DESC
                LIMIT %s
            """, (*params, *params, limit))
        except Exception as e:
            logger.debug(f"[RAG] Keyword summary search error: {e}")
            return []

        results: List[SearchResult] = []
        for r in (rows or []):
            tf = r.get("timeframe_minutes", 0)
            tf_label = _tf_label(tf)
            text = (
                f"[AI Alpha Summary — {tf_label}] "
                f"sentiment={r.get('sentiment_overall','neutral')}\n"
                f"{r.get('summary_text','')}"
            )
            kw_matched = r.get("kw_matches", 1)
            base_score = kw_matched / max(len(kw_list), 1)
            recency = _recency_boost(r.get("created_at"))
            score = round(min(base_score * 0.7 + recency * 0.3, 0.95), 3)

            results.append(SearchResult(
                text=text,
                score=score,
                collection="db_keyword_summaries",
                source_table="alpha_summaries",
                source_id=r["id"],
                metadata={
                    "source": "ai_alpha",
                    "timeframe": tf_label,
                    "sentiment": r.get("sentiment_overall", ""),
                    "search_type": "keyword",
                    "keywords_matched": kw_matched,
                },
                timestamp=str(r.get("created_at", "")),
            ))
        return results

    @staticmethod
    def _build_news_text(r: dict) -> str:
        """Build rich text from a news_items row including ai_analysis content."""
        headline = r.get("headline", "") or ""
        detail_text = r.get("detail", "") or ""
        text = f"[{r.get('source', '')}] {r.get('author', '') or ''}: {headline}"
        if detail_text:
            text += f"\n{detail_text}"

        analysis_raw = r.get("ai_analysis", "") or ""
        if analysis_raw:
            try:
                parsed = json.loads(analysis_raw) if isinstance(analysis_raw, str) else analysis_raw
                if isinstance(parsed, dict):
                    summary = (parsed.get("holistic_summary", "")
                               or parsed.get("summary", "")
                               or parsed.get("agent_analysis", {}).get("analysis", "")
                               or parsed.get("raw", ""))
                    if summary:
                        text += f"\nAnalysis: {str(summary)[:1000]}"
            except (json.JSONDecodeError, TypeError):
                if isinstance(analysis_raw, str) and len(analysis_raw) > 10:
                    text += f"\nAnalysis: {analysis_raw[:1000]}"
        return text

    # ─────────────────────────────────────────────────────────────
    # Core search (now hybrid)
    # ─────────────────────────────────────────────────────────────

    def search(self, query: str, collections: Optional[List[str]] = None,
               limit: int = 20, filters: Optional[Dict] = None,
               hybrid: bool = True) -> List[SearchResult]:
        """
        Hybrid search: semantic (vector) + keyword (DB text across news, signals,
        summaries). Results are deduplicated, recency-boosted, and merged.
        Set hybrid=False only for internal calls that need pure semantic.
        """
        cfg = self._get_hybrid_config()
        hybrid_enabled = cfg.get("hybrid_enabled", True) and hybrid
        keyword_limit = cfg.get("keyword_result_limit", 20)

        semantic_hits = self._semantic_search(query, collections, limit, filters)

        # Apply FinMem-lite three-axis scoring: similarity + recency + importance
        # Weights are dynamically loaded from system_config (CEO/Ledger adjustable)
        w_sim, w_rec, w_imp = self._get_rag_weights()
        for hit in semantic_hits:
            sim = hit.score
            ts = hit.timestamp
            recency = 0.1
            if ts:
                try:
                    if isinstance(ts, str):
                        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    recency = _recency_boost(ts)
                except Exception:
                    pass
            importance = _importance_score(hit.metadata or {})
            hit.score = round(sim * w_sim + recency * w_rec + importance * w_imp, 3)

        if not hybrid_enabled:
            return semantic_hits

        keywords = self.extract_keywords(query, max_keywords=cfg.get("max_keywords", 6))
        if not keywords:
            return semantic_hits

        top_score = max((r.score for r in semantic_hits), default=0)
        logger.info(f"[RAG] Hybrid search: semantic={len(semantic_hits)} hits "
                     f"(top={top_score:.2f}), keywords={keywords}")

        kw_hits = self.keyword_search_db(keywords, limit=keyword_limit)

        # Deduplicated merge: same (table, id) keeps highest score
        best: Dict[tuple, SearchResult] = {}
        for r in semantic_hits + kw_hits:
            key = (r.source_table, r.source_id) if r.source_id else (r.collection, id(r))
            existing = best.get(key)
            if existing is None or r.score > existing.score:
                best[key] = r

        merged = sorted(best.values(), key=lambda r: r.score, reverse=True)
        return merged[:limit]

    def _semantic_search(self, query: str, collections: Optional[List[str]] = None,
                         limit: int = 10, filters: Optional[Dict] = None) -> List[SearchResult]:
        """Pure vector-similarity search across collections."""
        embedder = self._get_embedder()
        store = self._get_store()

        query_vec = embedder.embed(query)

        if collections is None:
            from core.vector_store import COLLECTIONS
            collections = COLLECTIONS

        all_hits: List[SearchResult] = []
        per_coll = max(1, limit // len(collections)) + 2 if len(collections) > 1 else limit

        for coll in collections:
            try:
                hits = store.search(coll, query_vec, limit=per_coll, filters=filters)
                for h in hits:
                    all_hits.append(SearchResult(
                        text=h.payload.get("text_content", h.payload.get("text", "")),
                        score=h.score,
                        collection=coll,
                        source_table=h.payload.get("source_table", ""),
                        source_id=h.payload.get("source_id", 0),
                        metadata=h.payload,
                        timestamp=h.payload.get("timestamp") or h.payload.get("created_at"),
                    ))
            except Exception as e:
                logger.debug(f"[RAG] Search error in {coll}: {e}")

        all_hits.sort(key=lambda r: r.score, reverse=True)
        return all_hits[:limit]

    # ─────────────────────────────────────────────────────────────
    # Lineage-enriched search
    # ─────────────────────────────────────────────────────────────

    def search_with_lineage(self, query: str, collections: Optional[List[str]] = None,
                            limit: int = 10, filters: Optional[Dict] = None) -> List[SearchResult]:
        """
        Same as search() but enriches each result with its provenance chain
        from the source_lineage table.
        """
        results = self.search(query, collections, limit, filters)
        db = self._get_db()

        for r in results:
            if not r.source_table or not r.source_id:
                continue
            try:
                # What fed INTO this result
                upstream = db.fetch_all("""
                    SELECT input_table, input_id, relationship, agent_id, created_at
                    FROM source_lineage
                    WHERE output_table = %s AND output_id = %s
                    ORDER BY created_at DESC LIMIT 20
                """, (r.source_table, r.source_id))

                # What this result FED INTO
                downstream = db.fetch_all("""
                    SELECT output_table, output_id, relationship, agent_id, created_at
                    FROM source_lineage
                    WHERE input_table = %s AND input_id = %s
                    ORDER BY created_at DESC LIMIT 20
                """, (r.source_table, r.source_id))

                r.lineage = {
                    "upstream": [dict(row) for row in (upstream or [])],
                    "downstream": [dict(row) for row in (downstream or [])],
                }
            except Exception as e:
                logger.debug(f"[RAG] Lineage lookup error: {e}")

        return results

    # ─────────────────────────────────────────────────────────────
    # Agent-facing search (with accountability logging)
    # ─────────────────────────────────────────────────────────────

    def agent_search(self, agent_id: str, query: str,
                     motivation: str, reasoning: str,
                     trigger: str = "unknown",
                     collections: Optional[List[str]] = None,
                     limit: int = 15, filters: Optional[Dict] = None,
                     session_id: Optional[str] = None,
                     parent_activity_id: Optional[int] = None,
                     hybrid: bool = True) -> List[SearchResult]:
        """
        Hybrid search with full accountability. Logs WHY the agent queried,
        WHAT it hoped to learn, and WHAT triggered the query.
        Automatically supplements weak semantic results with keyword DB search.
        """
        results = self.search(query, collections, limit, filters, hybrid=hybrid)
        collections_searched = list(set(r.collection for r in results))

        # Log the query to agent_activity_log
        db = self._get_db()
        try:
            rag_entry = {
                "query": query,
                "motivation": motivation,
                "reasoning": reasoning,
                "trigger": trigger,
                "results_count": len(results),
                "collections_searched": collections_searched,
                "timestamp": datetime.now().isoformat(),
            }

            db.execute("""
                INSERT INTO agent_activity_log
                    (agent_id, activity_type, summary, detail, rag_queries,
                     session_id, parent_activity_id)
                VALUES (%s, 'query', %s, %s, %s, %s, %s)
            """, (
                agent_id,
                f"RAG query: {query[:200]}",
                f"Motivation: {motivation}\nReasoning: {reasoning}\nTrigger: {trigger}\nResults: {len(results)}",
                json.dumps([rag_entry]),
                session_id,
                parent_activity_id,
            ))
            logger.info(f"[RAG] Agent {agent_id} queried: {query[:80]}... ({len(results)} results, trigger={trigger})")
        except Exception as e:
            logger.debug(f"[RAG] Activity log error: {e}")

        return results

    # ─────────────────────────────────────────────────────────────
    # Timeline search (entity-centric)
    # ─────────────────────────────────────────────────────────────

    def get_entity_timeline(self, entity_type: str, entity_value: str,
                            days: int = 30, limit: int = 50) -> List[SearchResult]:
        """
        Get everything related to an entity over time.
        entity_type: 'author', 'symbol', 'agent', 'source'
        entity_value: e.g. 'TraderJ', 'XAUUSD', 'atlas', 'telegram'
        """
        query = f"{entity_type} {entity_value} recent activity and signals"
        results = self.search(query, limit=limit, filters={entity_type: entity_value})

        # Also do a broader search without filter for context
        broad = self.search(f"{entity_value}", limit=limit // 2)

        seen_ids = set()
        merged = []
        for r in results + broad:
            key = f"{r.source_table}:{r.source_id}"
            if key not in seen_ids:
                seen_ids.add(key)
                merged.append(r)

        merged.sort(key=lambda r: r.timestamp or "", reverse=True)
        return merged[:limit]

    # ─────────────────────────────────────────────────────────────
    # Conversation context for chain continuation
    # ─────────────────────────────────────────────────────────────

    def get_conversation_context(self, chain_id: str, max_tokens: int = 4000) -> str:
        """
        Load the most recent snapshot + recent turns for a conversation chain.
        Ready to inject into an agent's system prompt for continuation.
        """
        from services.context_snapshot_service import ContextSnapshotService
        svc = ContextSnapshotService(db=self._get_db())
        return svc.load_chain_context(chain_id, max_tokens)

    # ─────────────────────────────────────────────────────────────
    # Proof: who said what, when
    # ─────────────────────────────────────────────────────────────

    def find_proof(self, query: str, agent_id: Optional[str] = None,
                   date_range: Optional[tuple] = None,
                   limit: int = 20) -> Dict[str, Any]:
        """
        Find proof of who said what. Combines:
        1. Semantic search in conversations + agent_activity collections
        2. MySQL text search in conversation_turns + agent_activity_log
        Returns direct links to source rows.
        """
        db = self._get_db()

        # Semantic search
        filters = {}
        if agent_id:
            filters["speaker_id"] = agent_id
        rag_hits = self.search(query, collections=["conversations", "agent_activity"],
                               limit=limit, filters=filters if filters else None)

        # MySQL text search on turns
        sql = "SELECT id, conversation_id, turn_number, speaker_id, message_content, created_at FROM conversation_turns WHERE message_content LIKE %s"
        params = [f"%{query}%"]
        if agent_id:
            sql += " AND speaker_id = %s"
            params.append(agent_id)
        if date_range and len(date_range) == 2:
            sql += " AND created_at BETWEEN %s AND %s"
            params.extend(date_range)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        db_turns = db.fetch_all(sql, tuple(params)) or []

        # MySQL text search on activity log
        sql2 = "SELECT id, agent_id, activity_type, summary, detail, created_at FROM agent_activity_log WHERE (summary LIKE %s OR detail LIKE %s)"
        params2 = [f"%{query}%", f"%{query}%"]
        if agent_id:
            sql2 += " AND agent_id = %s"
            params2.append(agent_id)
        if date_range and len(date_range) == 2:
            sql2 += " AND created_at BETWEEN %s AND %s"
            params2.extend(date_range)
        sql2 += " ORDER BY created_at DESC LIMIT %s"
        params2.append(limit)
        db_activities = db.fetch_all(sql2, tuple(params2)) or []

        return {
            "query": query,
            "rag_results": [{"text": h.text[:500], "score": h.score, "collection": h.collection,
                             "source_table": h.source_table, "source_id": h.source_id}
                            for h in rag_hits],
            "conversation_turns": [dict(r) for r in db_turns],
            "activity_log": [dict(r) for r in db_activities],
        }

    # ─────────────────────────────────────────────────────────────
    # Reasoning chain reconstruction
    # ─────────────────────────────────────────────────────────────

    def get_reasoning_chain(self, activity_id: int) -> List[Dict]:
        """
        Reconstruct the full reasoning chain for an activity by following
        parent_activity_id links upward, then returning in chronological order.
        """
        db = self._get_db()
        chain = []
        current_id = activity_id
        visited = set()

        while current_id and current_id not in visited:
            visited.add(current_id)
            row = db.fetch_one("""
                SELECT id, agent_id, activity_type, summary, detail,
                       rag_queries, input_refs, output_refs,
                       parent_activity_id, session_id, created_at
                FROM agent_activity_log WHERE id = %s
            """, (current_id,))
            if not row:
                break
            chain.append(dict(row))
            current_id = row.get("parent_activity_id")

        chain.reverse()
        return chain


# ─────────────────────────────────────────────────────────────────
# Reasoning Chain Tracker
# ─────────────────────────────────────────────────────────────────

class ReasoningChainTracker:
    """
    Tracks an agent's full reasoning chain (perceive -> think -> decide -> act -> respond)
    as linked agent_activity_log entries. Produces the compressed reasoning_chain JSON
    for storage in conversation_turns.
    """

    STEPS = ("perceive", "think", "decide", "act", "respond")

    def __init__(self, agent_id: str, session_id: Optional[str] = None, db=None):
        self.agent_id = agent_id
        self.session_id = session_id
        self._db = db
        self._chain: List[Dict] = []
        self._activity_ids: List[int] = []
        self._parent_id: Optional[int] = None

    def _get_db(self):
        if self._db:
            return self._db
        from db.database import get_db
        self._db = get_db()
        return self._db

    def log_step(self, step: str, thought: str, conclusion: str = "",
                 rag_queries: Optional[List[Dict]] = None,
                 input_refs: Optional[List[Dict]] = None,
                 model_used: str = "", token_count: int = 0,
                 cost_usd: float = 0) -> int:
        """
        Log one step of the reasoning chain to agent_activity_log.
        Returns the activity_id for chaining.
        """
        db = self._get_db()

        activity_type = "thought" if step in ("perceive", "think") else "decision" if step == "decide" else "action"

        activity_id = db.execute_returning_id("""
            INSERT INTO agent_activity_log
                (agent_id, activity_type, summary, detail, rag_queries,
                 input_refs, session_id, parent_activity_id,
                 model_used, token_count, cost_usd)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            self.agent_id,
            activity_type,
            f"[{step}] {conclusion[:250]}" if conclusion else f"[{step}] {thought[:250]}",
            f"Step: {step}\nThought: {thought}\nConclusion: {conclusion}",
            json.dumps(rag_queries) if rag_queries else None,
            json.dumps(input_refs) if input_refs else None,
            self.session_id,
            self._parent_id,
            model_used,
            token_count,
            cost_usd,
        ))

        self._chain.append({
            "step": step,
            "thought": thought,
            "conclusion": conclusion,
            "activity_id": activity_id,
        })
        self._activity_ids.append(activity_id)
        self._parent_id = activity_id

        return activity_id

    def get_chain_json(self) -> List[Dict]:
        """Return the compressed reasoning chain for conversation_turns.reasoning_chain."""
        return [{"step": s["step"], "thought": s["thought"], "conclusion": s["conclusion"]}
                for s in self._chain]

    def get_activity_ids(self) -> List[int]:
        """Return all activity IDs in this chain, for linking."""
        return self._activity_ids

    def get_context_used_json(self, rag_results: Optional[List] = None) -> List[Dict]:
        """Format RAG results into the context_used JSON for conversation_turns."""
        if not rag_results:
            return []
        return [{
            "query": getattr(r, "text", str(r))[:200] if hasattr(r, "text") else str(r)[:200],
            "results_summary": f"score={getattr(r, 'score', 0):.2f} from {getattr(r, 'collection', 'unknown')}",
            "source_ids": [{"table": getattr(r, "source_table", ""), "id": getattr(r, "source_id", 0)}],
        } for r in (rag_results or []) if hasattr(r, "score")]


# ─────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────

_rag_instance: Optional[RagSearchEngine] = None


def get_rag_engine() -> RagSearchEngine:
    """Return the singleton RAG search engine."""
    global _rag_instance
    if _rag_instance is None:
        _rag_instance = RagSearchEngine()
    return _rag_instance
