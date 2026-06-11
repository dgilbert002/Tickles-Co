"""Mem0 configuration for Tickles — lean local recall (2026-05-30).

Embeddings: LOCAL sentence-transformers (all-MiniLM-L6-v2, 384 dims).
Vector store: LOCAL Qdrant (localhost:6333), collection tickles_{company}.

Policy: add/search use local vectors only (infer=False, rerank=False).
Postmortem compression happens in postmortem_service, not here.
"""

import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional, Tuple

from mem0 import Memory

logger = logging.getLogger(__name__)

_MEM0_CACHE: dict = {}
_MEM0_LOCK = threading.Lock()
_EMBED_RETRY_ONCE = 1


def _load_env():
    try:
        from dotenv import load_dotenv
        current = Path(__file__).resolve().parent
        for path in [current, *current.parents]:
            env_file = path / ".env"
            if env_file.exists():
                load_dotenv(dotenv_path=env_file)
                return
    except ImportError:
        pass


_load_env()


@contextmanager
def _mem0_env_guard():
    """Strip OPENROUTER_API_KEY for the full mem0 call (library env hijack)."""
    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["OPENROUTER_API_KEY"] = saved


_LOCAL_STUB_MODEL = os.environ.get("MEM0_STUB_MODEL", "local/noop")
MEM0_LLM_BASE_URL = os.environ.get("MEM0_LLM_BASE_URL", "http://127.0.0.1:9/v1")
MEM0_LLM_API_KEY = os.environ.get("MEM0_LLM_API_KEY", "local-no-key")

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIMS = 384


def _cache_key(company: str, agent: str) -> Tuple[str, str]:
    return (company, f"{company}_{agent}")


def get_cached_memory(company: str, agent: str) -> Tuple["ScopedMemory", str]:
    """Return a process-wide cached ScopedMemory (same pattern as MCP tools)."""
    key = _cache_key(company, agent)
    cached = _MEM0_CACHE.get(key)
    if cached is not None:
        return cached, key[1]
    with _MEM0_LOCK:
        cached = _MEM0_CACHE.get(key)
        if cached is not None:
            return cached, key[1]
        agent_id = key[1]
        sm = ScopedMemory(company, agent_id)
        _MEM0_CACHE[key] = sm
        return sm, agent_id


class ScopedMemory:
    """Company-scoped mem0 wrapper — lean local recall (no cloud LLM)."""

    def __init__(self, company: str, agent_id: str):
        self.company = company
        self.agent_id = agent_id
        self.collection_name = f"tickles_{company}"
        self._mem: Optional[Memory] = None

    def _build_config(self) -> dict:
        return {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "host": "localhost",
                    "port": 6333,
                    "collection_name": self.collection_name,
                    "embedding_model_dims": EMBEDDING_DIMS,
                },
            },
            "llm": {
                "provider": "openai",
                "config": {
                    "api_key": MEM0_LLM_API_KEY,
                    "openai_base_url": MEM0_LLM_BASE_URL,
                    "model": _LOCAL_STUB_MODEL,
                },
            },
            "embedder": {
                "provider": "huggingface",
                "config": {
                    "model": EMBEDDING_MODEL,
                    "embedding_dims": EMBEDDING_DIMS,
                },
            },
        }

    def _reset_client(self) -> None:
        self._mem = None

    def _ensure_mem(self) -> Memory:
        if self._mem is None:
            with _mem0_env_guard():
                self._mem = Memory.from_config(self._build_config())
        return self._mem

    @staticmethod
    def _is_embed_or_store_error(exc: Exception) -> bool:
        name = type(exc).__name__.lower()
        msg = str(exc).lower()
        hints = ("qdrant", "embed", "vector", "connection", "timeout", "sentence")
        return any(h in name or h in msg for h in hints)

    def add(self, text: str, **kwargs):
        logger.debug(
            "ScopedMemory.add collection=%s text_len=%s",
            self.collection_name,
            len(text or ""),
        )
        last_err: Optional[Exception] = None
        for attempt in range(_EMBED_RETRY_ONCE + 1):
            try:
                with _mem0_env_guard():
                    mem = self._ensure_mem()
                    result = mem.add(text, infer=False, **kwargs)
                results = result.get("results", []) if isinstance(result, dict) else []
                if not results:
                    raise RuntimeError("add returned empty results")
                logger.info("[mem0] add OK | collection=%s", self.collection_name)
                return result
            except Exception as exc:
                last_err = exc
                if attempt < _EMBED_RETRY_ONCE and self._is_embed_or_store_error(exc):
                    logger.warning(
                        "[mem0] add retry | collection=%s | %s: %s",
                        self.collection_name,
                        type(exc).__name__,
                        exc,
                    )
                    self._reset_client()
                    time.sleep(0.5)
                    continue
                logger.warning(
                    "[mem0] add FAIL | collection=%s | %s: %s",
                    self.collection_name,
                    type(exc).__name__,
                    exc,
                )
                self._reset_client()
                raise
        raise RuntimeError(f"[mem0] add failed: {last_err}") from last_err

    def search(self, query: str, **kwargs) -> Any:
        kwargs.setdefault("rerank", False)
        logger.debug(
            "ScopedMemory.search collection=%s query=%r limit=%s",
            self.collection_name,
            (query or "")[:80],
            kwargs.get("limit"),
        )
        last_err: Optional[Exception] = None
        for attempt in range(_EMBED_RETRY_ONCE + 1):
            try:
                with _mem0_env_guard():
                    mem = self._ensure_mem()
                    result = mem.search(query, **kwargs)
                logger.info("[mem0] search OK | collection=%s", self.collection_name)
                return result
            except Exception as exc:
                last_err = exc
                if attempt < _EMBED_RETRY_ONCE and self._is_embed_or_store_error(exc):
                    logger.warning(
                        "[mem0] search retry | collection=%s | %s: %s",
                        self.collection_name,
                        type(exc).__name__,
                        exc,
                    )
                    self._reset_client()
                    time.sleep(0.5)
                    continue
                logger.warning(
                    "[mem0] search FAIL | collection=%s | %s: %s",
                    self.collection_name,
                    type(exc).__name__,
                    exc,
                )
                self._reset_client()
                raise
        raise RuntimeError(f"[mem0] search failed: {last_err}") from last_err


def get_memory(company: str, agent: str) -> tuple:
    """Get a cached scoped memory instance for a company/agent pair."""
    return get_cached_memory(company, agent)


def get_dev_memory(agent: str = "default") -> tuple:
    """Dev namespace — isolated from trading agent memory."""
    return get_memory(company="dev", agent=agent)
