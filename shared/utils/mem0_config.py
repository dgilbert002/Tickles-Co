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

ISOLATION VERIFICATION NOTE (2026-04-26)
=========================================
Memory isolation between namespaces (dev vs trading companies) is enforced
at FIVE layers: collection_name, vector store object, mem0 client, user_id,
agent_id. All five are verified by shared/tests/test_mem0_isolation.py.

GOTCHA: Semantic search in a SPARSE collection (few points) will return
the closest matches available even when none are semantically relevant.
A search for "X" in a collection containing only "Y" memories will return
"Y" results — this is NOT an isolation leak. It is normal vector search
behavior in low-cardinality collections.

To verify isolation, check the user_id field on returned payloads, NOT
the count of returned results. See test_mem0_isolation.py for the
correct verification pattern.
"""

import os
import logging
from contextlib import contextmanager
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
# OPENROUTER_API_KEY handling.
#
# mem0's OpenAILLM hard-codes a priority check for ``os.environ["OPENROUTER_API_KEY"]``
# inside ``Memory.from_config(...)``. When the env var is set, mem0 ignores
# our api_key / openai_base_url config and force-routes to OpenRouter.
#
# Historically this module *popped* the key from os.environ at import time so
# mem0 would respect our config. That side-effect was destructive: every other
# service in the same process (e.g. shared.intelligence.gateway_config) reads
# OPENROUTER_API_KEY at call time and would break with
# "OPENROUTER API key not set for service interpretation" the moment any code
# imported this module.
#
# The correct pattern is to scope the pop to the duration of the
# ``Memory.from_config`` call only, then restore the environment immediately.
# We snapshot the value once at module import so callers that read
# ``_OPENROUTER_API_KEY_SAVED`` (e.g. as the LLM fallback key) continue to
# work without disturbing os.environ.
# ---------------------------------------------------------------------------
_OPENROUTER_API_KEY_SAVED = os.environ.get("OPENROUTER_API_KEY", "")


@contextmanager
def _mem0_env_guard():
    """Temporarily remove ``OPENROUTER_API_KEY`` so mem0 respects our LLM config.

    mem0's ``Memory.from_config`` checks ``os.environ["OPENROUTER_API_KEY"]``
    first and force-routes to OpenRouter when present, ignoring our explicit
    ``api_key`` / ``openai_base_url``. Removing the var only for the duration
    of that call avoids the side-effect leaking into the rest of the process.

    Yields:
        None. The previous value is restored in ``finally`` even on exception.
    """
    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["OPENROUTER_API_KEY"] = saved


MEM0_MODEL = os.environ.get("MEM0_MODEL", "deepseek/deepseek-chat")
MEM0_FALLBACK_MODELS = [
    "openai/gpt-4o-mini",
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
        """Add memory with automatic LLM fallback. Embeddings are always local.

        Uses infer=False to bypass LLM extraction, storing raw text directly.
        This avoids silent failures when the LLM provider is unavailable.
        """
        last_err = None
        for model in self.models_to_try:
            try:
                with _mem0_env_guard():
                    mem = Memory.from_config(self._build_config(model))
                # infer=False bypasses LLM extraction; embeddings are still computed locally.
                result = mem.add(text, infer=False, **kwargs)
                # Some providers return empty results on auth/model errors instead of raising.
                # Treat empty results as a failure so the fallback chain continues.
                results = result.get("results", []) if isinstance(result, dict) else []
                if not results:
                    logger.warning(
                        "[mem0] add EMPTY | model=%s | collection=%s | result=%s",
                        model,
                        self.collection_name,
                        result,
                    )
                    last_err = RuntimeError(f"add returned empty results for model {model}")
                    continue
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
                with _mem0_env_guard():
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
        company: Company name (e.g., "rubicon", "shared")
        agent: Agent name (e.g., "cody", "schemy", "ceo")

    Returns:
        (ScopedMemory, agent_id) tuple

    Example:
        memory, agent_id = get_memory("rubicon", "cody")
        memory.add("candle_service.py uses instrument_id FK",
                    user_id="rubicon", agent_id=agent_id)
        results = memory.search("what does candle_service do",
                                user_id="rubicon", agent_id=agent_id)
    """
    agent_id = f"{company}_{agent}"
    return ScopedMemory(company, agent_id), agent_id


def get_dev_memory(agent: str = "default") -> tuple:
    """Get memory scoped to the development environment.

    Completely isolated from trading agent memory.

    The dev namespace uses Qdrant collection 'tickles_dev' with
    user_id='dev'. This is intentionally separate from any trading
    company namespace (e.g. tickles_rubicon, tickles_jarvais).

    Args:
        agent: Subscope within dev memory.
               Examples: "roo", "architect", "code", "subagent_vision"

    Returns:
        Tuple of (ScopedMemory, agent_id) — same shape as get_memory().

    Use this for:
        - Dev session decisions (Roo, Architect, Code, Debug, Orchestrator modes)
        - Build progress and architectural choices
        - Bug fixes and refactoring notes
        - Anything related to BUILDING the platform

    Do NOT use this for:
        - Live trading signals
        - Trader scorecards or positions
        - Anything related to running trading agents
    """
    return get_memory(company="dev", agent=agent)
