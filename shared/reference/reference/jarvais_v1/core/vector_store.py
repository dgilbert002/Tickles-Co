"""
JarvAIs — Vector Store Abstraction
Supports Local Qdrant (file-based, DEFAULT), Qdrant Cloud, and Pinecone.
Toggle in config.json -> global.vector_store.backend.
"""

import logging
import uuid
import warnings
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

warnings.filterwarnings("ignore", message="Local mode is not recommended")

logger = logging.getLogger("jarvais.vector_store")


# ─────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────

class VectorPoint:
    """A single vector with its payload."""
    __slots__ = ("id", "vector", "payload")

    def __init__(self, vector: List[float], payload: Dict[str, Any], point_id: Optional[str] = None):
        self.id = point_id or str(uuid.uuid4())
        self.vector = vector
        self.payload = payload


class SearchHit:
    """A single search result."""
    __slots__ = ("id", "score", "payload")

    def __init__(self, point_id: str, score: float, payload: Dict[str, Any]):
        self.id = point_id
        self.score = score
        self.payload = payload


# ─────────────────────────────────────────────────────────────────
# Base class
# ─────────────────────────────────────────────────────────────────

class BaseVectorStore(ABC):
    """Abstract vector store backend."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Identifier: 'local', 'qdrant_cloud', 'pinecone'."""

    @abstractmethod
    def ensure_collection(self, name: str, vector_size: int) -> None:
        """Create collection if it doesn't exist."""

    @abstractmethod
    def store(self, collection: str, point: VectorPoint) -> str:
        """Store a single vector. Returns point ID."""

    @abstractmethod
    def store_batch(self, collection: str, points: List[VectorPoint]) -> int:
        """Store multiple vectors. Returns count stored."""

    @abstractmethod
    def search(self, collection: str, query_vector: List[float],
               limit: int = 10, filters: Optional[Dict] = None) -> List[SearchHit]:
        """Semantic search. Returns results sorted by score descending."""

    @abstractmethod
    def delete(self, collection: str, point_ids: List[str]) -> int:
        """Delete points by ID. Returns count deleted."""

    @abstractmethod
    def count(self, collection: str) -> int:
        """Count vectors in a collection."""

    @abstractmethod
    def list_collections(self) -> List[str]:
        """List all collection names."""

    @abstractmethod
    def delete_collection(self, name: str) -> bool:
        """Drop a collection entirely."""

    def test_connection(self) -> Dict[str, Any]:
        """Verify the backend is reachable. Returns status dict."""
        try:
            colls = self.list_collections()
            return {"ok": True, "backend": self.backend_name, "collections": len(colls)}
        except Exception as e:
            return {"ok": False, "backend": self.backend_name, "error": str(e)}


# ─────────────────────────────────────────────────────────────────
# Local Qdrant (file-based, zero setup)
# ─────────────────────────────────────────────────────────────────

class LocalQdrantStore(BaseVectorStore):
    """
    Uses qdrant-client in local file mode.
    Data stored in ./data/vectors/ by default.
    Same API as cloud — seamless upgrade path.
    """

    def __init__(self, path: str = "./data/vectors"):
        from qdrant_client import QdrantClient
        self._path = path
        self._client = QdrantClient(path=path)
        logger.info(f"[VectorStore] Local Qdrant initialized at {path}")

    @property
    def backend_name(self) -> str:
        return "local"

    def ensure_collection(self, name: str, vector_size: int) -> None:
        from qdrant_client.models import Distance, VectorParams
        existing = [c.name for c in self._client.get_collections().collections]
        if name not in existing:
            self._client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
            logger.info(f"[VectorStore] Created local collection: {name} ({vector_size}d)")

    def store(self, collection: str, point: VectorPoint) -> str:
        from qdrant_client.models import PointStruct
        self._client.upsert(
            collection_name=collection,
            points=[PointStruct(id=point.id, vector=point.vector, payload=point.payload)],
        )
        return point.id

    def store_batch(self, collection: str, points: List[VectorPoint]) -> int:
        from qdrant_client.models import PointStruct
        if not points:
            return 0
        structs = [PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in points]
        self._client.upsert(collection_name=collection, points=structs)
        return len(structs)

    def search(self, collection: str, query_vector: List[float],
               limit: int = 10, filters: Optional[Dict] = None) -> List[SearchHit]:
        qf = None
        if filters:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            conditions = []
            for k, v in filters.items():
                conditions.append(FieldCondition(key=k, match=MatchValue(value=v)))
            qf = Filter(must=conditions) if conditions else None

        hits = self._client.query_points(
            collection_name=collection,
            query=query_vector,
            limit=limit,
            query_filter=qf,
        ).points
        return [SearchHit(point_id=str(h.id), score=h.score, payload=h.payload or {}) for h in hits]

    def delete(self, collection: str, point_ids: List[str]) -> int:
        from qdrant_client.models import PointIdsList
        self._client.delete(
            collection_name=collection,
            points_selector=PointIdsList(points=point_ids),
        )
        return len(point_ids)

    def count(self, collection: str) -> int:
        try:
            info = self._client.get_collection(collection)
            return info.points_count or 0
        except Exception:
            return 0

    def list_collections(self) -> List[str]:
        return [c.name for c in self._client.get_collections().collections]

    def delete_collection(self, name: str) -> bool:
        try:
            self._client.delete_collection(name)
            return True
        except Exception:
            return False


# ─────────────────────────────────────────────────────────────────
# Qdrant Cloud
# ─────────────────────────────────────────────────────────────────

class CloudQdrantStore(BaseVectorStore):
    """
    Uses qdrant-client with cloud URL + API key.
    Same implementation as local but different constructor.
    """

    def __init__(self, url: str, api_key: str):
        from qdrant_client import QdrantClient
        self._client = QdrantClient(url=url, api_key=api_key)
        logger.info(f"[VectorStore] Qdrant Cloud connected: {url}")

    @property
    def backend_name(self) -> str:
        return "qdrant_cloud"

    def ensure_collection(self, name: str, vector_size: int) -> None:
        from qdrant_client.models import Distance, VectorParams
        existing = [c.name for c in self._client.get_collections().collections]
        if name not in existing:
            self._client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
            logger.info(f"[VectorStore] Created cloud collection: {name} ({vector_size}d)")

    def store(self, collection: str, point: VectorPoint) -> str:
        from qdrant_client.models import PointStruct
        self._client.upsert(
            collection_name=collection,
            points=[PointStruct(id=point.id, vector=point.vector, payload=point.payload)],
        )
        return point.id

    def store_batch(self, collection: str, points: List[VectorPoint]) -> int:
        from qdrant_client.models import PointStruct
        if not points:
            return 0
        structs = [PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in points]
        self._client.upsert(collection_name=collection, points=structs)
        return len(structs)

    def search(self, collection: str, query_vector: List[float],
               limit: int = 10, filters: Optional[Dict] = None) -> List[SearchHit]:
        qf = None
        if filters:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            conditions = []
            for k, v in filters.items():
                conditions.append(FieldCondition(key=k, match=MatchValue(value=v)))
            qf = Filter(must=conditions) if conditions else None

        hits = self._client.query_points(
            collection_name=collection,
            query=query_vector,
            limit=limit,
            query_filter=qf,
        ).points
        return [SearchHit(point_id=str(h.id), score=h.score, payload=h.payload or {}) for h in hits]

    def delete(self, collection: str, point_ids: List[str]) -> int:
        from qdrant_client.models import PointIdsList
        self._client.delete(
            collection_name=collection,
            points_selector=PointIdsList(points=point_ids),
        )
        return len(point_ids)

    def count(self, collection: str) -> int:
        try:
            info = self._client.get_collection(collection)
            return info.points_count or 0
        except Exception:
            return 0

    def list_collections(self) -> List[str]:
        return [c.name for c in self._client.get_collections().collections]

    def delete_collection(self, name: str) -> bool:
        try:
            self._client.delete_collection(name)
            return True
        except Exception:
            return False


# ─────────────────────────────────────────────────────────────────
# Pinecone
# ─────────────────────────────────────────────────────────────────

class PineconeStore(BaseVectorStore):
    """
    Uses Pinecone serverless. Each collection maps to a namespace
    within a single Pinecone index (jarvais).
    """

    def __init__(self, api_key: str, environment: str = "", index_name: str = "jarvais"):
        try:
            from pinecone import Pinecone
        except ImportError:
            raise RuntimeError("pinecone-client not installed. Run: pip install pinecone-client")

        self._pc = Pinecone(api_key=api_key)
        self._index_name = index_name
        self._index = None
        self._environment = environment
        logger.info(f"[VectorStore] Pinecone initialized (index={index_name})")

    def _get_index(self):
        if self._index is not None:
            return self._index
        existing = [idx.name for idx in self._pc.list_indexes()]
        if self._index_name not in existing:
            raise RuntimeError(
                f"Pinecone index '{self._index_name}' does not exist. "
                "Create it in the Pinecone console first."
            )
        self._index = self._pc.Index(self._index_name)
        return self._index

    @property
    def backend_name(self) -> str:
        return "pinecone"

    def ensure_collection(self, name: str, vector_size: int) -> None:
        # Pinecone uses namespaces, no explicit creation needed
        pass

    def store(self, collection: str, point: VectorPoint) -> str:
        idx = self._get_index()
        idx.upsert(
            vectors=[{"id": point.id, "values": point.vector, "metadata": point.payload}],
            namespace=collection,
        )
        return point.id

    def store_batch(self, collection: str, points: List[VectorPoint]) -> int:
        if not points:
            return 0
        idx = self._get_index()
        vectors = [{"id": p.id, "values": p.vector, "metadata": p.payload} for p in points]
        for i in range(0, len(vectors), 100):
            idx.upsert(vectors=vectors[i:i + 100], namespace=collection)
        return len(vectors)

    def search(self, collection: str, query_vector: List[float],
               limit: int = 10, filters: Optional[Dict] = None) -> List[SearchHit]:
        idx = self._get_index()
        resp = idx.query(
            vector=query_vector,
            top_k=limit,
            namespace=collection,
            include_metadata=True,
            filter=filters,
        )
        return [SearchHit(point_id=m.id, score=m.score, payload=m.metadata or {}) for m in resp.matches]

    def delete(self, collection: str, point_ids: List[str]) -> int:
        idx = self._get_index()
        idx.delete(ids=point_ids, namespace=collection)
        return len(point_ids)

    def count(self, collection: str) -> int:
        try:
            idx = self._get_index()
            stats = idx.describe_index_stats()
            ns = stats.namespaces.get(collection)
            return ns.vector_count if ns else 0
        except Exception:
            return 0

    def list_collections(self) -> List[str]:
        try:
            idx = self._get_index()
            stats = idx.describe_index_stats()
            return list(stats.namespaces.keys())
        except Exception:
            return []

    def delete_collection(self, name: str) -> bool:
        try:
            idx = self._get_index()
            idx.delete(delete_all=True, namespace=name)
            return True
        except Exception:
            return False


# ─────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────

# Standard collection names used throughout the system
COLLECTIONS = [
    "feed_items",
    "alpha_analysis",
    "alpha_timeframes",
    "signals",
    "trade_memory",
    "agent_activity",
    "company_knowledge",
    "conversations",
]

import threading
_store_instance: Optional[BaseVectorStore] = None
_store_lock = threading.Lock()


def get_vector_store(config: Optional[dict] = None, force_new: bool = False) -> BaseVectorStore:
    """
    Return the configured vector store backend (thread-safe singleton).
    Config path: global.vector_store.backend ('local', 'qdrant_cloud', 'pinecone').
    """
    global _store_instance
    if _store_instance is not None and not force_new:
        return _store_instance

    with _store_lock:
        if _store_instance is not None and not force_new:
            return _store_instance

        if config is None:
            try:
                import json, os as _os
                _cfg_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "config.json")
                with open(_cfg_path, "r", encoding="utf-8") as _f:
                    config = json.load(_f)
            except Exception:
                config = {}

        vs_cfg = config.get("global", {}).get("vector_store", {})
        backend = vs_cfg.get("backend", "local")

        if backend == "qdrant_cloud":
            import os
            url = vs_cfg.get("qdrant_cloud_url") or os.environ.get("QDRANT_URL", "")
            api_key = vs_cfg.get("qdrant_cloud_api_key") or os.environ.get("QDRANT_API_KEY", "")
            if not url:
                logger.warning("[VectorStore] Qdrant Cloud URL not set, falling back to local")
                backend = "local"
            else:
                _store_instance = CloudQdrantStore(url=url, api_key=api_key)

        if backend == "pinecone":
            import os
            api_key = vs_cfg.get("pinecone_api_key") or os.environ.get("PINECONE_API_KEY", "")
            env = vs_cfg.get("pinecone_environment", "")
            if not api_key:
                logger.warning("[VectorStore] Pinecone API key not set, falling back to local")
                backend = "local"
            else:
                _store_instance = PineconeStore(api_key=api_key, environment=env)

        if backend == "local" and _store_instance is None:
            import os
            path = vs_cfg.get("local_path", "./data/vectors")
            os.makedirs(path, exist_ok=True)
            _store_instance = LocalQdrantStore(path=path)

        return _store_instance


def ensure_all_collections(store: BaseVectorStore, vector_size: int) -> None:
    """Create all standard collections if they don't exist."""
    for name in COLLECTIONS:
        try:
            store.ensure_collection(name, vector_size)
        except Exception as e:
            logger.warning(f"[VectorStore] Could not ensure collection {name}: {e}")
