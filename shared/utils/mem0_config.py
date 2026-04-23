"""Mem0 configuration for Tickles & Co — V2 production config.

Embeddings: LOCAL sentence-transformers (all-MiniLM-L6-v2, 384 dims)
  - Runs on CPU, zero API calls, zero cost, zero external dependency
  - Consistent vector dimensions regardless of which LLM model is used
  - Requires: pip install sentence-transformers (already installed)

Vector Store: LOCAL Qdrant (localhost:6333)
  - Collections auto-created per company: tickles_{company}

LLM: Requesty router (swappable — change the model string, embeddings unaffected)
  - Used ONLY for memory extraction/summarization by Mem0's internal pipeline
  - Falls back to OpenRouter only if REQUESTY_API is not set
  - If LLM is down, embeddings still work (search degrades, add fails gracefully)

IMPORTANT: mem0's OpenAILLM hard-codes a priority check for OPENROUTER_API_KEY
  in os.environ. When set, it ignores our api_key/openai_base_url config and
  always routes to OpenRouter. We neutralize that env var below so mem0 respects
  our configured provider.
"""

import os
import logging
from pathlib import Path
from mem0 import Memory

logger = logging.getLogger(__name__)


def _load_env():
    """Load .env from project root (/opt/tickles/.env) if dotenv is available."""
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

# ---------------------------------------------------------------------------
# Neutralize OPENROUTER_API_KEY so mem0's OpenAILLM does NOT hard-route to
# OpenRouter.  mem0's __init__ checks os.environ["OPENROUTER_API_KEY"] FIRST
# and ignores our api_key / openai_base_url config when it is set.  We save
# the value in case other code needs it, then remove it from the environment
# so mem0 falls through to the else-branch that respects our config.
# ---------------------------------------------------------------------------
_OPENROUTER_API_KEY_SAVED = os.environ.pop("OPENROUTER_API_KEY", "")

MEM0_MODEL = os.environ.get("MEM0_MODEL", "deepseek/deepseek-chat")
MEM0_FALLBACK_MODELS = [
    "deepseek/deepseek-chat",
    "google/gemini-2.0-flash-001",
]

# LLM provider: prefer Requesty (has active credit), fall back to OpenRouter.
MEM0_LLM_BASE_URL = os.environ.get(
    "MEM0_LLM_BASE_URL",
    "https://router.requesty.ai/v1" if os.environ.get("REQUESTY_API") else "https://openrouter.ai/api/v1",
)
MEM0_LLM_API_KEY = os.environ.get(
    "MEM0_LLM_API_KEY",
    os.environ.get("REQUESTY_API", _OPENROUTER_API_KEY_SAVED),
)

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
EMBEDDING_DIMS = 384


class ScopedMemory:
    """Mem0 wrapper with company-scoped Qdrant collections and LLM fallback.

    Embeddings are ALWAYS local (sentence-transformers).
    LLM calls go through Requesty (or OpenRouter if REQUESTY_API is unset)
    with automatic model fallback on failure.
    Each company gets its own Qdrant collection: tickles_{company}.
    """

    def __init__(self, company: str, agent_id: str):
        self.company = company
        self.agent_id = agent_id
        self.collection_name = f"tickles_{company}"
        models_to_try = [MEM0_MODEL]
        for model in MEM0_FALLBACK_MODELS:
            if model not in models_to_try:
                models_to_try.append(model)
        self.models_to_try = models_to_try

    def _build_config(self, model: str) -> dict:
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
                    "model": model,
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

    def add(self, text: str, **kwargs):
        """Add memory with automatic LLM fallback. Embeddings are always local."""
        last_err = None
        for model in self.models_to_try:
            try:
                mem = Memory.from_config(self._build_config(model))
                result = mem.add(text, **kwargs)
                logger.info("[mem0] add OK | model=%s | collection=%s", model, self.collection_name)
                return result
            except Exception as e:
                logger.warning("[mem0] add FAIL | model=%s | %s: %s", model, type(e).__name__, e)
                last_err = e
        raise RuntimeError(
            f"[mem0] add exhausted all models {self.models_to_try}: {last_err}"
        ) from last_err

    def search(self, query: str, **kwargs):
        """Search memory with automatic LLM fallback. Embeddings are always local."""
        last_err = None
        for model in self.models_to_try:
            try:
                mem = Memory.from_config(self._build_config(model))
                result = mem.search(query, **kwargs)
                logger.info("[mem0] search OK | model=%s | collection=%s", model, self.collection_name)
                return result
            except Exception as e:
                logger.warning("[mem0] search FAIL | model=%s | %s: %s", model, type(e).__name__, e)
                last_err = e
        raise RuntimeError(
            f"[mem0] search exhausted all models {self.models_to_try}: {last_err}"
        ) from last_err


def get_memory(company: str, agent: str) -> tuple:
    """Get a scoped memory instance for a company/agent pair.

    Args:
        company: Company name (e.g., "jarvais", "shared")
        agent: Agent name (e.g., "cody", "schemy", "ceo")

    Returns:
        (ScopedMemory, agent_id) tuple

    Example:
        memory, agent_id = get_memory("jarvais", "cody")
        memory.add("candle_service.py uses instrument_id FK",
                    user_id="jarvais", agent_id=agent_id)
        results = memory.search("what does candle_service do",
                                user_id="jarvais", agent_id=agent_id)
    """
    agent_id = f"{company}_{agent}"
    return ScopedMemory(company, agent_id), agent_id
