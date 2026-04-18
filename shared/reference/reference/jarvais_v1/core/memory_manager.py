"""
JarvAIs Memory Manager v2.0
Primary Memory Layer: Mem0 (with Qdrant Cloud backend)
Fallback: Direct Qdrant client → MySQL

Mem0 provides graph-based, portable memory that works across different AI models.
When we switch from Claude Opus 4.6 to a newer model, the memory transfers seamlessly.
Qdrant Cloud's free tier (1GB) serves as the shared vector store, enabling multi-VPS
instances to share the same global brain.

Architecture:
    Mem0 (primary) → Qdrant Cloud (vector store)
    Direct Qdrant (fallback for raw vector ops)
    MySQL (structured fallback + audit trail)

Collections (Qdrant):
    - trade_lessons: Post-mortem analysis of every trade
    - daily_reviews: Daily self-coaching reviews
    - patterns: Self-discovered trading patterns
    - market_context: News, events, and market regime snapshots
    - human_interventions: User-injected memories and corrections

Mem0 User/Agent IDs:
    - user_id: "jarvais_global" (shared brain)
    - agent_id: per-role (trader, coach, analyst, postmortem)

Usage:
    from core.memory_manager import get_memory_manager
    mm = get_memory_manager()
    mm.store_memory("Gold reversed at 2350 after Fed speech", metadata={...})
    results = mm.search_similar("Gold reversal near resistance after news", limit=5)
    mm.store_human_intervention(role="trader", message="Watch 2380 resistance", ...)
"""

import json
import logging
import hashlib
import os
from datetime import datetime
from typing import Dict, Any, Optional, List

from core.time_utils import utcnow

logger = logging.getLogger("jarvais.memory_manager")


# ─────────────────────────────────────────────────────────────────────
# Collection Definitions (for direct Qdrant fallback)
# ─────────────────────────────────────────────────────────────────────

COLLECTIONS = {
    "trade_lessons": {
        "description": "Post-mortem analysis of every trade",
        "vector_size": 1536,  # OpenAI text-embedding-3-small
    },
    "daily_reviews": {
        "description": "Daily self-coaching reviews",
        "vector_size": 1536,
    },
    "patterns": {
        "description": "Self-discovered trading patterns",
        "vector_size": 1536,
    },
    "market_context": {
        "description": "News, events, and market regime snapshots",
        "vector_size": 1536,
    },
    "human_interventions": {
        "description": "User-injected memories and corrections",
        "vector_size": 1536,
    },
    "agent_conversations": {
        "description": "Chat, huddle, and break room conversations",
        "vector_size": 1536,
    },
    "agent_decisions": {
        "description": "AI decision reasoning and outcomes",
        "vector_size": 1536,
    },
    "company_knowledge": {
        "description": "Company policies, procedures, and institutional knowledge",
        "vector_size": 1536,
    },
}

# Mem0 category mapping for structured retrieval
MEM0_CATEGORIES = {
    "trade_lesson": "trade_lessons",
    "daily_review": "daily_reviews",
    "pattern": "patterns",
    "market_context": "market_context",
    "human_intervention": "human_interventions",
}


# ─────────────────────────────────────────────────────────────────────
# Memory Manager Class
# ─────────────────────────────────────────────────────────────────────

class MemoryManager:
    """
    Central memory system for JarvAIs.

    Primary: Mem0 (graph-based, portable, model-agnostic memory)
    Backend: Qdrant Cloud (shared vector store across all instances)
    Fallback: Direct Qdrant client for raw vector operations
    Audit: MySQL for structured data and backup

    All instances share the same Qdrant Cloud cluster = global brain.
    """

    # Mem0 global user ID (shared brain across all accounts)
    GLOBAL_USER_ID = "jarvais_global"

    # Role-specific agent IDs for Mem0
    ROLE_AGENT_IDS = {
        "trader": "jarvais_trader",
        "coach": "jarvais_coach",
        "analyst": "jarvais_analyst",
        "postmortem": "jarvais_postmortem",
        "all": "jarvais_global",
    }

    def __init__(self):
        self._mem0_client = None
        self._qdrant_client = None
        self._config = None
        self._mem0_available = False
        self._qdrant_available = False
        self._initialized = False

        logger.info("Memory Manager v2.0 created (lazy initialization, Mem0 primary)")

    @property
    def config(self):
        if self._config is None:
            from core.config import get_config
            self._config = get_config()
        return self._config

    # ─────────────────────────────────────────────────────────────────
    # Initialization
    # ─────────────────────────────────────────────────────────────────

    def _ensure_initialized(self):
        """Lazy initialization of Mem0 and Qdrant connections."""
        if self._initialized:
            return

        # Try Mem0 first (primary)
        self._init_mem0()

        # Also init direct Qdrant (for raw vector ops and fallback)
        self._init_qdrant()

        self._initialized = True

    def _init_mem0(self):
        """Initialize Mem0 as the primary memory layer.

        Supports two modes:
        1. Mem0 Platform API (hosted) — uses just an API key, Mem0 hosts everything
           Config: memory.provider = "mem0_platform", memory.mem0_api_key = "m0-..."
        2. Mem0 Self-Hosted (OSS) — uses local/cloud Qdrant + embedder + LLM
           Config: memory.provider = "mem0_selfhosted" (or omitted)
        """
        try:
            memory_cfg = self.config.raw.get("memory", {})
            provider = memory_cfg.get("provider", "mem0_selfhosted")

            if provider == "mem0_platform":
                # ── Mem0 Platform API (hosted) ──
                # Simplest path: just an API key, Mem0 handles embeddings, vectors, LLM
                from mem0 import MemoryClient

                api_key = memory_cfg.get("mem0_api_key", "")
                if not api_key:
                    logger.warning("Mem0 Platform API key not set in config.json (memory.mem0_api_key)")
                    self._mem0_available = False
                    return

                self._mem0_client = MemoryClient(api_key=api_key)
                self._mem0_mode = "platform"
                self._mem0_available = True
                logger.info("Mem0 Platform API initialized successfully (hosted mode)")

            else:
                # ── Mem0 Self-Hosted (OSS) ──
                # Requires Qdrant (local or cloud) + embedder + LLM
                from mem0 import Memory

                qdrant_cfg = memory_cfg.get("qdrant", {})
                qdrant_url = qdrant_cfg.get("url", "") or os.environ.get("QDRANT_URL", "")
                qdrant_api_key = qdrant_cfg.get("api_key", "") or os.environ.get("QDRANT_API_KEY", "")

                mem0_config = {"version": "v1.1"}

                # Vector store
                if qdrant_url and qdrant_api_key:
                    mem0_config["vector_store"] = {
                        "provider": "qdrant",
                        "config": {
                            "url": qdrant_url,
                            "api_key": qdrant_api_key,
                            "collection_name": "jarvais_memories",
                        }
                    }
                    logger.info(f"Mem0 configured with Qdrant Cloud: {qdrant_url}")
                else:
                    host = qdrant_cfg.get("host", "localhost")
                    port = qdrant_cfg.get("port", 6333)
                    mem0_config["vector_store"] = {
                        "provider": "qdrant",
                        "config": {
                            "host": host,
                            "port": port,
                            "collection_name": "jarvais_memories",
                        }
                    }
                    logger.info(f"Mem0 configured with local Qdrant: {host}:{port}")

                # Embedder
                embedder_cfg = memory_cfg.get("embedder", {})
                embedder_provider = embedder_cfg.get("provider", "openai")
                if embedder_provider == "openai":
                    mem0_config["embedder"] = {
                        "provider": "openai",
                        "config": {"model": embedder_cfg.get("model", "text-embedding-3-small")}
                    }
                elif embedder_provider == "huggingface":
                    mem0_config["embedder"] = {
                        "provider": "huggingface",
                        "config": {"model": embedder_cfg.get("model", "sentence-transformers/all-MiniLM-L6-v2")}
                    }

                # LLM for Mem0 internal processing
                llm_cfg = memory_cfg.get("llm", {})
                if llm_cfg.get("provider", "openai") == "openai":
                    mem0_config["llm"] = {
                        "provider": "openai",
                        "config": {
                            "model": llm_cfg.get("model", "gpt-4.1-nano"),
                            "temperature": 0.1,
                        }
                    }

                self._mem0_client = Memory.from_config(mem0_config)
                self._mem0_mode = "selfhosted"
                self._mem0_available = True
                logger.info("Mem0 Self-Hosted initialized successfully (OSS mode)")

        except ImportError as e:
            logger.warning(f"mem0ai package not installed ({e}). Run: pip install mem0ai")
            self._mem0_available = False
        except Exception as e:
            logger.warning(f"Failed to initialize Mem0: {e}. Will use direct Qdrant fallback.")
            self._mem0_available = False

    def _init_qdrant(self):
        """Initialize vector store via the new abstraction layer (vector_store.py).
        Falls back to legacy direct Qdrant client if abstraction unavailable."""
        try:
            from core.vector_store import get_vector_store, ensure_all_collections
            from core.embeddings import get_embedder

            self._vector_store = get_vector_store()
            self._embedder_instance = get_embedder()
            ensure_all_collections(self._vector_store, self._embedder_instance.dimensions)

            # Legacy compatibility: also set _qdrant_client for old code paths
            if hasattr(self._vector_store, '_client'):
                self._qdrant_client = self._vector_store._client

            self._qdrant_available = True
            logger.info(f"Vector store ready: backend={self._vector_store.backend_name}, "
                        f"embedder={self._embedder_instance.provider_name} ({self._embedder_instance.dimensions}d)")

        except Exception as e:
            logger.warning(f"Vector store abstraction init failed, trying legacy: {e}")
            self._vector_store = None
            self._embedder_instance = None
            # Legacy fallback
            try:
                from qdrant_client import QdrantClient
                from qdrant_client.models import Distance, VectorParams

                memory_cfg = self.config.raw.get("memory", {})
                qdrant_cfg = memory_cfg.get("qdrant", {})
                qdrant_url = qdrant_cfg.get("url", "") or os.environ.get("QDRANT_URL", "")
                qdrant_api_key = qdrant_cfg.get("api_key", "") or os.environ.get("QDRANT_API_KEY", "")

                if qdrant_url and qdrant_api_key:
                    self._qdrant_client = QdrantClient(url=qdrant_url, api_key=qdrant_api_key, timeout=15)
                else:
                    host = qdrant_cfg.get("host", "localhost")
                    port = qdrant_cfg.get("port", 6333)
                    self._qdrant_client = QdrantClient(host=host, port=port, timeout=10)

                existing = [c.name for c in self._qdrant_client.get_collections().collections]
                for name, spec in COLLECTIONS.items():
                    if name not in existing:
                        self._qdrant_client.create_collection(
                            collection_name=name,
                            vectors_config=VectorParams(size=spec["vector_size"], distance=Distance.COSINE)
                        )
                self._qdrant_available = True
            except Exception as e2:
                logger.warning(f"Legacy Qdrant also failed: {e2}")
                self._qdrant_available = False

    # ─────────────────────────────────────────────────────────────────
    # Mem0 Operations (Primary)
    # ─────────────────────────────────────────────────────────────────

    def store_memory(self, text: str, metadata: Dict[str, Any],
                     collection: str = "trade_lessons",
                     role: str = "all") -> bool:
        """
        Store a memory using Mem0 (primary) with Qdrant fallback.

        Mem0 handles embedding, deduplication, and graph relationships.
        Also stores in direct Qdrant collection for structured queries.

        Args:
            text: The memory content
            metadata: Structured metadata (trade_id, symbol, outcome, etc.)
            collection: Qdrant collection name for direct storage
            role: Which AI role this memory belongs to

        Returns:
            True if stored successfully via any method
        """
        self._ensure_initialized()

        success = False

        # 1. Store via Mem0 (primary — handles embedding + graph)
        if self._mem0_available:
            try:
                agent_id = self.ROLE_AGENT_IDS.get(role, self.ROLE_AGENT_IDS["all"])

                # Build metadata for Mem0
                mem0_metadata = {
                    "category": collection,
                    "role": role,
                    "stored_at": utcnow().isoformat(),
                }
                # Add key metadata fields (Mem0 metadata must be flat)
                for k, v in metadata.items():
                    if isinstance(v, (str, int, float, bool)):
                        mem0_metadata[k] = v

                if getattr(self, '_mem0_mode', 'selfhosted') == 'platform':
                    # Platform API: positional messages, keyword user_id/agent_id
                    self._mem0_client.add(
                        text,
                        user_id=self.GLOBAL_USER_ID,
                        agent_id=agent_id,
                        metadata=mem0_metadata,
                    )
                else:
                    # OSS API: keyword messages
                    self._mem0_client.add(
                        messages=text,
                        user_id=self.GLOBAL_USER_ID,
                        agent_id=agent_id,
                        metadata=mem0_metadata,
                    )

                logger.info(f"[Mem0] Stored memory for {role} in '{collection}': {text[:80]}...")
                success = True

            except Exception as e:
                logger.error(f"[Mem0] Failed to store memory: {e}")

        # 2. Also store in direct Qdrant collection (for structured queries)
        if self._qdrant_available and collection in COLLECTIONS:
            try:
                self._store_direct_qdrant(text, metadata, collection)
            except Exception as e:
                logger.warning(f"[Qdrant Direct] Failed to store: {e}")

        # 3. MySQL audit trail (always attempt)
        self._store_mysql_audit(text, metadata, collection, role)

        if not success:
            # Fallback to direct Qdrant if Mem0 failed
            if self._qdrant_available:
                success = self._store_direct_qdrant(text, metadata, collection)
            if not success:
                success = self._store_mysql_fallback(text, metadata, collection)

        return success

    def search_similar(self, query: str, collection: str = "trade_lessons",
                       limit: int = 5, min_score: float = 0.3,
                       role: str = "all",
                       filters: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """
        Search for similar memories using Mem0 (primary) with Qdrant fallback.

        Mem0 provides semantic search with graph-aware ranking.
        Falls back to direct Qdrant vector search if Mem0 is unavailable.

        Args:
            query: Natural language query
            collection: Collection to search (used for Qdrant fallback)
            limit: Max results
            min_score: Minimum similarity score
            role: Which role's memories to search
            filters: Optional metadata filters

        Returns:
            List of memory dicts with text, score, and metadata
        """
        self._ensure_initialized()

        # 1. Try Mem0 first (primary)
        if self._mem0_available:
            try:
                agent_id = self.ROLE_AGENT_IDS.get(role, self.ROLE_AGENT_IDS["all"])

                if getattr(self, '_mem0_mode', 'selfhosted') == 'platform':
                    # Platform API: uses filters dict, returns {"results": [...]}
                    search_filters = {"user_id": self.GLOBAL_USER_ID}
                    if role != "all":
                        search_filters = {
                            "AND": [
                                {"user_id": self.GLOBAL_USER_ID},
                                {"agent_id": agent_id},
                            ]
                        }

                    raw_results = self._mem0_client.search(
                        query=query,
                        filters=search_filters,
                        limit=limit,
                    )

                    # Platform returns dict with "results" key
                    if isinstance(raw_results, dict):
                        results = raw_results.get("results", [])
                    elif isinstance(raw_results, list):
                        results = raw_results
                    else:
                        results = []
                else:
                    # OSS API: uses keyword args, returns list directly
                    results = self._mem0_client.search(
                        query=query,
                        user_id=self.GLOBAL_USER_ID,
                        agent_id=agent_id if role != "all" else None,
                        limit=limit,
                    )
                    if not isinstance(results, list):
                        results = []

                memories = []
                for hit in results:
                    memory = hit if isinstance(hit, dict) else {}
                    # Normalize Mem0 result format
                    mem_text = memory.get("memory", memory.get("text", ""))
                    score = memory.get("score", memory.get("relevance", 0.5))
                    meta = memory.get("metadata", {})
                    if not isinstance(meta, dict):
                        meta = {}

                    if score >= min_score:
                        memories.append({
                            "text": mem_text,
                            "score": score,
                            "id": memory.get("id", ""),
                            "source": "mem0",
                            **meta
                        })

                logger.info(f"[Mem0] Search '{query[:50]}...' -> {len(memories)} results")
                return memories

            except Exception as e:
                logger.warning(f"[Mem0] Search failed: {e}. Falling back to Qdrant.")

        # 2. Fallback to direct Qdrant
        if self._qdrant_available:
            return self._search_direct_qdrant(query, collection, limit, min_score, filters)

        # 3. Final fallback to MySQL
        return self._search_mysql_fallback(query, collection, limit)

    def get_role_memories(self, role: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Get all memories for a specific role.
        Useful for building role-specific context in the cognitive engine.
        """
        self._ensure_initialized()

        if self._mem0_available:
            try:
                agent_id = self.ROLE_AGENT_IDS.get(role, self.ROLE_AGENT_IDS["all"])

                if getattr(self, '_mem0_mode', 'selfhosted') == 'platform':
                    # Platform API: uses filters dict
                    get_filters = {
                        "AND": [
                            {"user_id": self.GLOBAL_USER_ID},
                            {"agent_id": agent_id},
                        ]
                    }
                    raw_results = self._mem0_client.get_all(
                        filters=get_filters,
                        limit=limit,
                    )
                    # Platform returns dict with "results" key
                    if isinstance(raw_results, dict):
                        results = raw_results.get("results", raw_results.get("memories", []))
                    elif isinstance(raw_results, list):
                        results = raw_results
                    else:
                        results = []
                else:
                    # OSS API: uses keyword args
                    results = self._mem0_client.get_all(
                        user_id=self.GLOBAL_USER_ID,
                        agent_id=agent_id,
                        limit=limit,
                    )
                    if not isinstance(results, list):
                        results = []

                return [
                    {
                        "text": m.get("memory", m.get("text", "")),
                        "id": m.get("id", ""),
                        "metadata": m.get("metadata", {}),
                        "created_at": m.get("created_at", ""),
                    }
                    for m in results if isinstance(m, dict)
                ]
            except Exception as e:
                logger.warning(f"[Mem0] get_role_memories failed: {e}")

        return []

    # ─────────────────────────────────────────────────────────────────
    # Human Intervention (Coach Injection)
    # ─────────────────────────────────────────────────────────────────

    def store_human_intervention(self, role: str, message: str,
                                  category: str = "correction",
                                  priority: str = "high") -> bool:
        """
        Store a human-injected memory/correction.
        These are high-priority memories that influence future decisions.

        Args:
            role: Target role (trader, coach, analyst, postmortem, all)
            message: The human's instruction/correction
            category: correction, insight, rule, warning, encouragement
            priority: high, medium, low

        Returns:
            True if stored successfully
        """
        metadata = {
            "type": "human_intervention",
            "category": category,
            "priority": priority,
            "target_role": role,
            "injected_by": "human_coach",
            "injected_at": utcnow().isoformat(),
        }

        # Prefix the message for high-priority retrieval
        prefixed = f"[HUMAN COACH - {priority.upper()} - {category.upper()}] {message}"

        success = self.store_memory(
            text=prefixed,
            metadata=metadata,
            collection="human_interventions",
            role=role,
        )

        if success:
            logger.info(f"Human intervention stored for {role}: {message[:80]}...")

        return success

    # ─────────────────────────────────────────────────────────────────
    # Pattern Management
    # ─────────────────────────────────────────────────────────────────

    def store_pattern(self, pattern: Dict[str, Any]) -> bool:
        """
        Store a self-discovered trading pattern.

        Expected pattern format:
        {
            "name": "London Open + ADX > 35 + Fibonacci Bounce",
            "conditions": ["London session open", "ADX > 35", "Price at Fib 61.8%"],
            "win_rate": 0.85,
            "sample_size": 40,
            "avg_pnl": 120.50,
            "avg_confidence": 82,
            "symbols": ["XAUUSD"],
            "timeframes": ["M5"],
            "discovered_date": "2026-02-10",
            "last_validated": "2026-02-10"
        }
        """
        text = (f"Pattern: {pattern.get('name', 'Unknown')}. "
                f"Conditions: {', '.join(pattern.get('conditions', []))}. "
                f"Win rate: {pattern.get('win_rate', 0)*100:.0f}% over "
                f"{pattern.get('sample_size', 0)} trades. "
                f"Avg P&L: ${pattern.get('avg_pnl', 0):.2f}.")

        metadata = {
            "type": "pattern",
            "name": pattern.get("name", "Unknown"),
            "win_rate": pattern.get("win_rate", 0),
            "sample_size": pattern.get("sample_size", 0),
            "avg_pnl": pattern.get("avg_pnl", 0),
        }

        # Store in Mem0 + Qdrant
        self.store_memory(text, metadata, collection="patterns", role="postmortem")

        # Also store in MySQL patterns table for structured queries
        try:
            from db.database import get_db
            db = get_db()
            db.insert_pattern(pattern)
            logger.info(f"Pattern stored: {pattern.get('name', 'Unknown')}")
            return True
        except Exception as e:
            logger.error(f"Failed to store pattern in MySQL: {e}")
            return False

    def get_relevant_patterns(self, symbol: str, conditions: List[str],
                              min_win_rate: float = 0.6) -> List[Dict[str, Any]]:
        """Find patterns that match the current market conditions."""
        query = f"Trading pattern for {symbol} with conditions: {', '.join(conditions)}"
        results = self.search_similar(
            query, collection="patterns", limit=5, min_score=0.4, role="postmortem"
        )

        filtered = []
        for r in results:
            try:
                win_rate = float(r.get("win_rate", 0))
                if win_rate >= min_win_rate:
                    filtered.append(r)
            except (ValueError, TypeError):
                continue

        return filtered

    # ─────────────────────────────────────────────────────────────────
    # Direct Qdrant Operations (Fallback + Raw Vector Ops)
    # ─────────────────────────────────────────────────────────────────

    def _get_embedding(self, text: str) -> Optional[List[float]]:
        """Generate embedding vector using the configured embedder (local or OpenAI)."""
        # Use new abstraction if available
        if hasattr(self, '_embedder_instance') and self._embedder_instance:
            try:
                return self._embedder_instance.embed(text[:8000])
            except Exception as e:
                logger.warning(f"Embedder failed: {e}. Trying OpenAI direct.")

        # Legacy OpenAI fallback
        try:
            import openai
            base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
            client = openai.OpenAI(base_url=base_url) if base_url else openai.OpenAI()
            response = client.embeddings.create(
                model="text-embedding-3-small",
                input=text[:8000]
            )
            return response.data[0].embedding
        except Exception as e:
            logger.warning(f"Embedding generation failed: {e}. Using hash fallback.")
            return self._hash_fallback_embedding(text)

    def _hash_fallback_embedding(self, text: str) -> List[float]:
        """Deterministic pseudo-embedding from text hash (emergency fallback)."""
        import struct
        vectors = []
        for i in range(24):
            h = hashlib.sha512(f"{text}_{i}".encode()).digest()
            for j in range(0, len(h), 4):
                val = struct.unpack('f', h[j:j+4])[0]
                val = max(-1.0, min(1.0, val / 1e38)) if abs(val) > 1e-38 else 0.0
                vectors.append(val)
        return vectors[:1536]

    def _store_direct_qdrant(self, text: str, metadata: Dict[str, Any],
                              collection: str) -> bool:
        """Store directly in Qdrant collection (bypassing Mem0)."""
        if not self._qdrant_available or collection not in COLLECTIONS:
            return False

        try:
            from qdrant_client.models import PointStruct

            embedding = self._get_embedding(text)
            if not embedding:
                return False

            point_id = self._generate_point_id(text, metadata)

            # Sanitize metadata
            clean_metadata = {"text": text, "stored_at": utcnow().isoformat()}
            for k, v in metadata.items():
                if isinstance(v, (str, int, float, bool)):
                    clean_metadata[k] = v
                elif isinstance(v, (list, dict)):
                    clean_metadata[k] = json.dumps(v)
                elif v is None:
                    clean_metadata[k] = ""
                else:
                    clean_metadata[k] = str(v)

            self._qdrant_client.upsert(
                collection_name=collection,
                points=[PointStruct(id=point_id, vector=embedding, payload=clean_metadata)]
            )

            logger.info(f"[Qdrant Direct] Stored in '{collection}': {text[:80]}...")
            return True

        except Exception as e:
            logger.error(f"[Qdrant Direct] Store failed: {e}")
            return False

    def _search_direct_qdrant(self, query: str, collection: str,
                               limit: int, min_score: float,
                               filters: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """Search directly in Qdrant collection (bypassing Mem0)."""
        if not self._qdrant_available or collection not in COLLECTIONS:
            return []

        try:
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            query_embedding = self._get_embedding(query)
            if not query_embedding:
                return []

            qdrant_filter = None
            if filters:
                conditions = [
                    FieldCondition(key=k, match=MatchValue(value=v))
                    for k, v in filters.items()
                ]
                qdrant_filter = Filter(must=conditions)

            results = self._qdrant_client.search(
                collection_name=collection,
                query_vector=query_embedding,
                limit=limit,
                score_threshold=min_score,
                query_filter=qdrant_filter
            )

            memories = []
            for hit in results:
                memory = {
                    "text": hit.payload.get("text", ""),
                    "score": hit.score,
                    "id": hit.id,
                    "source": "qdrant_direct",
                    **{k: v for k, v in hit.payload.items() if k != "text"}
                }
                memories.append(memory)

            logger.info(f"[Qdrant Direct] Search '{query[:50]}...' -> {len(memories)} results")
            return memories

        except Exception as e:
            logger.error(f"[Qdrant Direct] Search failed: {e}")
            return []

    # ─────────────────────────────────────────────────────────────────
    # MySQL Fallback + Audit Trail
    # ─────────────────────────────────────────────────────────────────

    def _store_mysql_audit(self, text: str, metadata: Dict, collection: str, role: str):
        """Store an audit record in MySQL (always, regardless of Mem0/Qdrant success)."""
        try:
            from db.database import get_db
            db = get_db()
            db.insert_memory_audit({
                "collection": collection,
                "role": role,
                "text_preview": text[:2000],
                "metadata": metadata,
            })
        except Exception as e:
            logger.debug(f"MySQL audit trail write failed (non-critical): {e}")

    def _store_mysql_fallback(self, text: str, metadata: Dict, collection: str) -> bool:
        """Fallback: store memory in MySQL if both Mem0 and Qdrant are unavailable."""
        try:
            from db.database import get_db
            db = get_db()
            db.insert_memory_fallback({
                "collection": collection,
                "text_content": text,
                "metadata": metadata,
            })
            logger.info(f"[MySQL Fallback] Stored memory: {text[:80]}...")
            return True
        except Exception as e:
            logger.error(f"[MySQL Fallback] Storage also failed: {e}")
            return False

    def _search_mysql_fallback(self, query: str, collection: str,
                                limit: int) -> List[Dict[str, Any]]:
        """Fallback: search MySQL if both Mem0 and Qdrant are unavailable."""
        try:
            from db.database import get_db
            db = get_db()
            return db.search_memories_fallback(collection, query, limit)
        except Exception as e:
            logger.error(f"[MySQL Fallback] Search also failed: {e}")
            return []

    # ─────────────────────────────────────────────────────────────────
    # Memory Export/Import (for model migration)
    # ─────────────────────────────────────────────────────────────────

    def export_all_memories(self, output_path: str) -> int:
        """
        Export ALL memories from all collections to a JSON file.
        Used for model migration — preserving knowledge when switching AI models.
        """
        self._ensure_initialized()

        all_memories = {}
        total = 0

        # Export from Mem0 if available
        if self._mem0_available:
            try:
                for role_name, agent_id in self.ROLE_AGENT_IDS.items():
                    if getattr(self, '_mem0_mode', 'selfhosted') == 'platform':
                        raw = self._mem0_client.get_all(
                            filters={
                                "AND": [
                                    {"user_id": self.GLOBAL_USER_ID},
                                    {"agent_id": agent_id},
                                ]
                            },
                            limit=10000,
                        )
                        if isinstance(raw, dict):
                            memories = raw.get("results", raw.get("memories", []))
                        elif isinstance(raw, list):
                            memories = raw
                        else:
                            memories = []
                    else:
                        memories = self._mem0_client.get_all(
                            user_id=self.GLOBAL_USER_ID,
                            agent_id=agent_id,
                            limit=10000,
                        )
                        if not isinstance(memories, list):
                            memories = []

                    if memories:
                        all_memories[f"mem0_{role_name}"] = memories
                        total += len(memories)
            except Exception as e:
                logger.error(f"Mem0 export failed: {e}")

        # Also export from direct Qdrant collections
        if self._qdrant_available:
            for collection_name in COLLECTIONS:
                memories = self._export_qdrant_collection(collection_name)
                all_memories[f"qdrant_{collection_name}"] = memories
                total += len(memories)

        with open(output_path, 'w') as f:
            json.dump({
                "export_date": utcnow().isoformat(),
                "total_memories": total,
                "mem0_available": self._mem0_available,
                "qdrant_available": self._qdrant_available,
                "collections": all_memories
            }, f, indent=2, default=str)

        logger.info(f"Exported {total} memories to {output_path}")
        return total

    def _export_qdrant_collection(self, collection_name: str) -> List[Dict]:
        """Export all points from a single Qdrant collection."""
        if not self._qdrant_available:
            return []

        try:
            memories = []
            offset = None
            batch_size = 100

            while True:
                results = self._qdrant_client.scroll(
                    collection_name=collection_name,
                    limit=batch_size,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False
                )

                points, next_offset = results
                for point in points:
                    memories.append({"id": point.id, "payload": point.payload})

                if next_offset is None:
                    break
                offset = next_offset

            return memories

        except Exception as e:
            logger.error(f"Failed to export Qdrant collection {collection_name}: {e}")
            return []

    def import_memories(self, input_path: str) -> int:
        """
        Import memories from a JSON export file.
        Re-generates embeddings for each memory (necessary when switching models).
        """
        self._ensure_initialized()

        with open(input_path, 'r') as f:
            data = json.load(f)

        total = 0
        collections = data.get("collections", {})

        for key, memories in collections.items():
            if not isinstance(memories, list):
                continue

            for mem in memories:
                # Handle both Mem0 and Qdrant export formats
                if "memory" in mem:
                    text = mem["memory"]
                elif "payload" in mem:
                    text = mem["payload"].get("text", "")
                else:
                    text = mem.get("text", "")

                if text:
                    metadata = mem.get("metadata", mem.get("payload", {}))
                    if isinstance(metadata, dict):
                        metadata = {k: v for k, v in metadata.items() if k != "text"}
                    else:
                        metadata = {}

                    collection = metadata.get("category", "trade_lessons")
                    role = metadata.get("role", "all")

                    if self.store_memory(text, metadata, collection=collection, role=role):
                        total += 1

        logger.info(f"Imported {total} memories from {input_path}")
        return total

    # ─────────────────────────────────────────────────────────────────
    # Statistics
    # ─────────────────────────────────────────────────────────────────

    def get_memory_stats(self) -> Dict[str, Any]:
        """Get comprehensive statistics about the memory system."""
        self._ensure_initialized()

        mem0_mode = getattr(self, '_mem0_mode', 'selfhosted')
        provider_label = (
            f"Mem0 Platform ({mem0_mode})" if self._mem0_available else (
                "Qdrant Direct" if self._qdrant_available else "MySQL Fallback"
            )
        )

        stats = {
            "mem0_available": self._mem0_available,
            "mem0_mode": mem0_mode if self._mem0_available else "none",
            "qdrant_available": self._qdrant_available,
            "provider": provider_label,
            "total_memories": 0,
            "trade_lessons": 0,
            "patterns": 0,
            "interventions": 0,
            "db_size": "0 MB",
        }

        # Get Mem0 stats
        if self._mem0_available:
            try:
                if mem0_mode == 'platform':
                    raw = self._mem0_client.get_all(
                        filters={"user_id": self.GLOBAL_USER_ID},
                        limit=1,
                    )
                    # Platform may return total count in response
                    if isinstance(raw, dict):
                        stats["total_memories"] = len(raw.get("results", []))
                else:
                    for role_name, agent_id in self.ROLE_AGENT_IDS.items():
                        self._mem0_client.get_all(
                            user_id=self.GLOBAL_USER_ID,
                            agent_id=agent_id,
                            limit=1,
                        )
            except Exception:
                pass

        # Get Qdrant collection stats
        if self._qdrant_available:
            try:
                total = 0
                for name in COLLECTIONS:
                    try:
                        info = self._qdrant_client.get_collection(name)
                        count = info.points_count or 0
                        total += count
                        if name == "trade_lessons":
                            stats["trade_lessons"] = count
                        elif name == "patterns":
                            stats["patterns"] = count
                        elif name == "human_interventions":
                            stats["interventions"] = count
                    except Exception:
                        pass

                stats["total_memories"] = total

                # Estimate DB size (rough: ~2KB per vector point with 1536 dims)
                size_bytes = total * 2048
                if size_bytes > 1024 * 1024:
                    stats["db_size"] = f"{size_bytes / (1024*1024):.1f} MB"
                else:
                    stats["db_size"] = f"{size_bytes / 1024:.0f} KB"

            except Exception as e:
                stats["error"] = str(e)

        # Always supplement with MySQL counts (the actual source of truth)
        try:
            from db.database import get_db
            db = get_db()
            lessons_count = db.fetch_one("SELECT COUNT(*) as cnt FROM trade_lessons")
            patterns_count = db.fetch_one("SELECT COUNT(*) as cnt FROM self_discovered_patterns")
            news_count = db.fetch_one("SELECT COUNT(*) as cnt FROM news_items")
            stats["trade_lessons"] = max(stats.get("trade_lessons", 0), (lessons_count or {}).get('cnt', 0))
            stats["patterns"] = max(stats.get("patterns", 0), (patterns_count or {}).get('cnt', 0))
            stats["news_items"] = (news_count or {}).get('cnt', 0)
            stats["total_memories"] = max(stats.get("total_memories", 0), stats["trade_lessons"] + stats["patterns"])
        except Exception:
            pass

        return stats

    # ─────────────────────────────────────────────────────────────────
    # Utilities
    # ─────────────────────────────────────────────────────────────────

    def _generate_point_id(self, text: str, metadata: Dict) -> int:
        """Generate a unique integer ID for a Qdrant point."""
        unique_str = f"{text}_{json.dumps(metadata, sort_keys=True, default=str)}"
        hash_hex = hashlib.md5(unique_str.encode()).hexdigest()
        return int(hash_hex[:15], 16)

    def clear_collection(self, collection_name: str) -> bool:
        """Clear all points from a collection. Use with extreme caution."""
        self._ensure_initialized()

        if not self._qdrant_available:
            return False

        try:
            from qdrant_client.models import Distance, VectorParams

            spec = COLLECTIONS.get(collection_name)
            if not spec:
                return False

            self._qdrant_client.delete_collection(collection_name)
            self._qdrant_client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(
                    size=spec["vector_size"],
                    distance=Distance.COSINE
                )
            )
            logger.info(f"Cleared collection: {collection_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to clear collection {collection_name}: {e}")
            return False

    def health_check(self) -> Dict[str, Any]:
        """Run a health check on all memory subsystems."""
        self._ensure_initialized()

        mem0_mode = getattr(self, '_mem0_mode', 'selfhosted')
        health = {
            "mem0": {
                "status": "ok" if self._mem0_available else "unavailable",
                "mode": mem0_mode if self._mem0_available else "none",
            },
            "qdrant": {"status": "ok" if self._qdrant_available else "unavailable"},
            "mysql": {"status": "unknown"},
        }

        # Test MySQL
        try:
            from db.database import get_db
            get_db()
            health["mysql"]["status"] = "ok"
        except Exception as e:
            health["mysql"]["status"] = f"error: {e}"

        # Test Qdrant connectivity
        if self._qdrant_available:
            try:
                collections = self._qdrant_client.get_collections()
                health["qdrant"]["collections"] = len(collections.collections)
            except Exception as e:
                health["qdrant"]["status"] = f"error: {e}"

        return health


# ─────────────────────────────────────────────────────────────────────
# Singleton Instance
# ─────────────────────────────────────────────────────────────────────

_memory_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    """Get or create the shared MemoryManager instance."""
    global _memory_manager
    if _memory_manager is None:
        _memory_manager = MemoryManager()
    return _memory_manager
