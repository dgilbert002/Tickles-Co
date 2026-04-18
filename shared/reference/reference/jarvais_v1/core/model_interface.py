"""
JarvAIs Model Interface
Unified abstraction layer for all AI model providers.
Supports Claude (Anthropic), GPT (OpenAI), and Gemini (Google).
Handles retries, fallback, token counting, cost tracking, and logging.

Usage:
    from core.model_interface import get_model_interface
    mi = get_model_interface()
    response = mi.query(
        role="trader",
        system_prompt="You are a senior trader...",
        user_prompt="Analyze this signal...",
        account_id="DEMO_001"
    )
    print(response.content)
    print(response.cost_usd)
"""

import os
import time
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from enum import Enum

from core.config import get_config, AIModelConfig

logger = logging.getLogger("jarvais.model_interface")


# ─────────────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────────────

class Provider(Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"


@dataclass
class ModelResponse:
    """Standardized response from any AI model."""
    content: str = ""
    provider: str = ""
    model: str = ""
    role: str = ""
    token_count_input: int = 0
    token_count_output: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    success: bool = True
    error_message: str = ""
    raw_response: Any = None
    cost_source: str = "estimated"  # "openrouter_actual" or "estimated"

    @property
    def total_tokens(self) -> int:
        return self.token_count_input + self.token_count_output


@dataclass
class ConversationMessage:
    """A single message in a conversation."""
    role: str  # "user" or "assistant"
    content: str


# ─────────────────────────────────────────────────────────────────────
# Provider-Specific Adapters
# ─────────────────────────────────────────────────────────────────────

class AnthropicAdapter:
    """Adapter for Anthropic Claude models."""

    def __init__(self, model_config: AIModelConfig):
        self.config = model_config
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic
            api_key = self.config.api_key
            if not api_key:
                raise ValueError(f"API key not found in env var: {self.config.api_key_env}")
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    @staticmethod
    def _convert_vision_messages(messages: list) -> list:
        """
        Convert OpenAI-format vision content to Anthropic format.
        OpenAI:    {"type":"image_url","image_url":{"url":"...","detail":"high"}}
        Anthropic: {"type":"image","source":{"type":"url","url":"..."}}
        For base64: {"type":"image","source":{"type":"base64","media_type":"image/png","data":"..."}}
        """
        converted = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                new_parts = []
                for part in content:
                    if part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            # base64 data URI → extract media type and data
                            # Format: data:image/png;base64,<data>
                            header, b64data = url.split(",", 1) if "," in url else (url, "")
                            media_type = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
                            new_parts.append({
                                "type": "image",
                                "source": {"type": "base64", "media_type": media_type, "data": b64data}
                            })
                        else:
                            # Regular URL
                            new_parts.append({
                                "type": "image",
                                "source": {"type": "url", "url": url}
                            })
                    else:
                        new_parts.append(part)
                converted.append({"role": msg.get("role", "user"), "content": new_parts})
            else:
                converted.append(msg)
        return converted

    def query(self, system_prompt: str, messages: List[Dict[str, str]],
              max_tokens: int = None, temperature: float = None) -> ModelResponse:
        """Send a query to Claude."""
        start_time = time.time()
        max_tokens = max_tokens or self.config.max_tokens
        temperature = temperature if temperature is not None else self.config.temperature

        # Anthropic temperature range is 0-1 (not 0-2 like OpenAI)
        temperature = min(temperature, 1.0)

        # Convert OpenAI vision format to Anthropic format if needed
        messages = self._convert_vision_messages(messages)

        try:
            response = self.client.messages.create(
                model=self.config.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                messages=messages
            )

            latency_ms = int((time.time() - start_time) * 1000)
            input_tokens = response.usage.input_tokens
            output_tokens = response.usage.output_tokens
            cost = self._calculate_cost(input_tokens, output_tokens)

            return ModelResponse(
                content=response.content[0].text if response.content else "",
                provider="anthropic",
                model=self.config.model,
                token_count_input=input_tokens,
                token_count_output=output_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
                success=True,
                raw_response=response
            )

        except Exception as e:
            latency_ms = int((time.time() - start_time) * 1000)
            logger.warning(f"Anthropic API error | model={self.config.model} | max_tokens={max_tokens} | temp={temperature} | error={str(e)[:100]}")
            return ModelResponse(
                provider="anthropic",
                model=self.config.model,
                latency_ms=latency_ms,
                success=False,
                error_message=str(e)
            )

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        input_cost = (input_tokens / 1_000_000) * self.config.cost_per_1m_input
        output_cost = (output_tokens / 1_000_000) * self.config.cost_per_1m_output
        return round(input_cost + output_cost, 6)


class OpenAIAdapter:
    """Adapter for OpenAI GPT models (also used for OpenAI-compatible APIs like OpenRouter)."""

    def __init__(self, model_config: AIModelConfig, base_url: str = None,
                 default_headers: dict = None):
        self.config = model_config
        self._base_url_override = base_url
        self._default_headers = default_headers
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI
            api_key = self.config.api_key
            if not api_key:
                logger.error(f"\n{'='*60}\n  API KEY NOT FOUND!\n  Expected env var: {self.config.api_key_env}\n  Make sure your .env file exists in the project root\n  and contains: {self.config.api_key_env}=your-key-here\n{'='*60}")
                raise ValueError(f"API key not found in env var: {self.config.api_key_env}")
            base_url = self._base_url_override or os.environ.get("OPENAI_BASE_URL", "").strip() or None
            logger.info(f"OpenAI client initialized: model={self.config.model}, base_url={base_url or 'default'}")
            kwargs = dict(api_key=api_key, base_url=base_url)
            if self._default_headers:
                kwargs["default_headers"] = self._default_headers
            self._client = OpenAI(**kwargs)
        return self._client

    def query(self, system_prompt: str, messages: List[Dict[str, str]],
              max_tokens: int = None, temperature: float = None,
              extra_params: dict = None) -> ModelResponse:
        """Send a query to GPT / OpenRouter / any OpenAI-compatible endpoint.

        ``extra_params`` is an optional dict merged into the
        ``chat.completions.create`` call.  Common keys:
          - ``extra_body`` – provider-specific fields (reasoning, reasoning_effort)
          - ``tools``      – server-side tools (e.g. ``[{"type":"web_search"}]``)
          - ``tool_choice`` – ``"auto"`` / ``"required"`` / ``"none"``
        """
        start_time = time.time()
        max_tokens = max_tokens or self.config.max_tokens
        temperature = temperature if temperature is not None else self.config.temperature

        # OpenAI temperature range is 0-2, cap at 2.0 for safety
        temperature = min(temperature, 2.0)

        # Build messages with system prompt
        full_messages = [{"role": "system", "content": system_prompt}]
        full_messages.extend(messages)

        _extra = extra_params or {}

        try:
            # Handle newer OpenAI models (gpt-5, o1, etc.) that may not support:
            # - max_completion_tokens (older models use max_tokens)
            # - custom temperature (some models only support default 1)
            
            last_error = None
            
            # Try combinations in order of preference
            param_combinations = [
                {"max_completion_tokens": max_tokens, "temperature": temperature},
                {"max_completion_tokens": max_tokens},  # No temperature
                {"max_tokens": max_tokens, "temperature": temperature},
                {"max_tokens": max_tokens},  # No temperature (most compatible)
            ]
            
            for params in param_combinations:
                try:
                    response = self.client.chat.completions.create(
                        model=self.config.model,
                        messages=full_messages,
                        **params,
                        **_extra,
                    )
                    # Success! Log if we used fallback
                    if params != param_combinations[0]:
                        logger.debug(f"Used fallback params for {self.config.model}: {list(params.keys())}")
                    break
                except Exception as e:
                    last_error = e
                    continue
            else:
                # All combinations failed
                raise last_error

            latency_ms = int((time.time() - start_time) * 1000)
            usage = getattr(response, "usage", None)
            input_tokens = getattr(usage, "prompt_tokens", 0) or 0 if usage else 0
            output_tokens = getattr(usage, "completion_tokens", 0) or 0 if usage else 0

            # Prefer OpenRouter's actual billed cost over our estimate
            openrouter_cost = None
            cost_source = "estimated"
            if usage:
                # OpenRouter returns actual cost in usage.cost (float)
                openrouter_cost = getattr(usage, "cost", None)
                if openrouter_cost is None:
                    # Some SDK versions expose it via model_extra dict
                    extra = getattr(usage, "model_extra", {}) or {}
                    openrouter_cost = extra.get("cost")

            if openrouter_cost is not None and isinstance(openrouter_cost, (int, float)):
                cost = round(float(openrouter_cost), 6)
                cost_source = "openrouter_actual"
            else:
                cost = self._calculate_cost(input_tokens, output_tokens)

            return ModelResponse(
                content=response.choices[0].message.content if response.choices else "",
                provider="openai",
                model=self.config.model,
                token_count_input=input_tokens,
                token_count_output=output_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
                success=True,
                raw_response=response,
                cost_source=cost_source,
            )

        except Exception as e:
            latency_ms = int((time.time() - start_time) * 1000)
            # Log detailed error info on single line for debugging
            logger.warning(f"OpenAI API error | model={self.config.model} | base_url={os.environ.get('OPENAI_BASE_URL', 'default')[:50]} | max_tokens={max_tokens} | temp={temperature} | error={str(e)[:100]}")
            return ModelResponse(
                provider="openai",
                model=self.config.model,
                latency_ms=latency_ms,
                success=False,
                error_message=str(e)
            )

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        input_cost = (input_tokens / 1_000_000) * self.config.cost_per_1m_input
        output_cost = (output_tokens / 1_000_000) * self.config.cost_per_1m_output
        return round(input_cost + output_cost, 6)


class GoogleAdapter:
    """Adapter for Google Gemini models."""

    def __init__(self, model_config: AIModelConfig):
        self.config = model_config
        self._model = None

    @property
    def model(self):
        if self._model is None:
            import google.generativeai as genai
            api_key = self.config.api_key
            if not api_key:
                raise ValueError(f"API key not found in env var: {self.config.api_key_env}")
            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel(
                model_name=self.config.model,
                system_instruction=None  # Set per-query
            )
        return self._model

    def query(self, system_prompt: str, messages: List[Dict[str, str]],
              max_tokens: int = None, temperature: float = None) -> ModelResponse:
        """Send a query to Gemini."""
        import google.generativeai as genai

        start_time = time.time()
        max_tokens = max_tokens or self.config.max_tokens
        temperature = temperature if temperature is not None else self.config.temperature

        # Google temperature range is 0-2, cap at 2.0 for safety
        temperature = min(temperature, 2.0)

        try:
            # Rebuild model with system instruction
            api_key = self.config.api_key
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(
                model_name=self.config.model,
                system_instruction=system_prompt
            )

            # Convert messages to Gemini format
            # Handle both text and vision (image) messages
            gemini_messages = []
            for msg in messages:
                role = "user" if msg["role"] == "user" else "model"
                content = msg.get("content")
                
                # If content is a list (OpenAI vision format), convert to Gemini format
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if isinstance(part, dict):
                            if part.get("type") == "text":
                                parts.append(part.get("text", ""))
                            elif part.get("type") == "image_url":
                                # Convert OpenAI image_url to Gemini format
                                img_url = part.get("image_url", {}).get("url", "")
                                if img_url.startswith("data:"):
                                    # Base64 data URI: data:image/png;base64,<data>
                                    try:
                                        header, b64data = img_url.split(",", 1) if "," in img_url else (img_url, "")
                                        media_type = header.split(":")[1].split(";")[0] if ":" in header else "image/png"
                                        # Gemini expects {"mime_type": "...", "data": "..."} format for inline images
                                        parts.append({
                                            "mime_type": media_type,
                                            "data": b64data
                                        })
                                    except Exception as img_err:
                                        logger.warning(f"Failed to convert base64 image for Gemini: {img_err}")
                                else:
                                    # URL image - download and convert to base64
                                    try:
                                        import httpx
                                        import base64
                                        resp = httpx.get(img_url, timeout=10, follow_redirects=True)
                                        if resp.status_code == 200:
                                            content_type = resp.headers.get("content-type", "image/png")
                                            media_type = content_type.split(";")[0]
                                            b64data = base64.b64encode(resp.content).decode("utf-8")
                                            parts.append({
                                                "mime_type": media_type,
                                                "data": b64data
                                            })
                                        else:
                                            logger.warning(f"Gemini: Failed to download image {img_url[:50]}: HTTP {resp.status_code}")
                                    except Exception as dl_err:
                                        logger.warning(f"Gemini: Error downloading image {img_url[:50]}: {dl_err}")
                            else:
                                # Unknown part type - try to get text
                                if "text" in part:
                                    parts.append(part["text"])
                        elif isinstance(part, str):
                            parts.append(part)
                    gemini_messages.append({"role": role, "parts": parts})
                elif isinstance(content, str):
                    # Simple text content
                    gemini_messages.append({"role": role, "parts": [content]})
                else:
                    # Fallback
                    gemini_messages.append({"role": role, "parts": [str(content)]})

            generation_config = genai.types.GenerationConfig(
                max_output_tokens=max_tokens,
                temperature=temperature
            )

            response = model.generate_content(
                gemini_messages,
                generation_config=generation_config
            )

            latency_ms = int((time.time() - start_time) * 1000)

            # Token counting
            input_tokens = 0
            output_tokens = 0
            if hasattr(response, 'usage_metadata'):
                input_tokens = getattr(response.usage_metadata, 'prompt_token_count', 0) or 0
                output_tokens = getattr(response.usage_metadata, 'candidates_token_count', 0) or 0

            cost = self._calculate_cost(input_tokens, output_tokens)

            return ModelResponse(
                content=response.text if response.text else "",
                provider="google",
                model=self.config.model,
                token_count_input=input_tokens,
                token_count_output=output_tokens,
                cost_usd=cost,
                latency_ms=latency_ms,
                success=True,
                raw_response=response
            )

        except Exception as e:
            latency_ms = int((time.time() - start_time) * 1000)
            logger.warning(f"Google API error | model={self.config.model} | max_tokens={max_tokens} | temp={temperature} | error={str(e)[:100]}")
            return ModelResponse(
                provider="google",
                model=self.config.model,
                latency_ms=latency_ms,
                success=False,
                error_message=str(e)
            )

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        input_cost = (input_tokens / 1_000_000) * self.config.cost_per_1m_input
        output_cost = (output_tokens / 1_000_000) * self.config.cost_per_1m_output
        return round(input_cost + output_cost, 6)


class ManusAdapter:
    """Adapter for the Manus AI Task API (native, not OpenAI-compat).

    Manus uses POST /v1/tasks with an API_KEY header.  Tasks are
    asynchronous: the adapter polls until the task reaches a terminal
    status, then collects the final text and any attached JSON files.
    """

    _BASE = "https://api.manus.ai"
    _POLL_INTERVAL = 10         # seconds between status checks
    _MAX_POLL_SECONDS = 900     # 15 min — Manus browses real websites

    def __init__(self, model_config: AIModelConfig):
        self.config = model_config
        self._api_key = model_config.api_key or _os.environ.get("MANUS_API_KEY", "")

    def _headers(self) -> dict:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "API_KEY": self._api_key,
        }

    def _extract_content(self, data: dict) -> str:
        """Walk the output list and return the best content string.

        Prefers attached JSON files over plain text (Manus often puts
        the structured response in a file attachment).
        """
        import requests as _req
        import json as _json

        output_items = data.get("output", [])
        if not isinstance(output_items, list):
            return str(output_items)

        file_content = None
        last_text = ""

        for item in output_items:
            if item.get("role") != "assistant":
                continue
            for c in item.get("content", []):
                ctype = c.get("type", "")
                if ctype == "output_text" and c.get("text"):
                    last_text = c["text"]
                elif ctype == "output_file" and c.get("fileUrl"):
                    try:
                        fr = _req.get(c["fileUrl"], timeout=30)
                        if fr.status_code == 200:
                            ct = fr.headers.get("content-type", "")
                            if "json" in ct or c.get("mimeType", "") == "application/json":
                                file_content = fr.text
                            else:
                                file_content = fr.text
                    except Exception as e:
                        logger.debug(f"[Manus] File download failed: {e}")

        if file_content:
            return file_content
        return last_text

    def query(self, system_prompt: str, messages: List[Dict[str, str]],
              max_tokens: int = None, temperature: float = None,
              extra_params: dict = None) -> ModelResponse:
        import requests as _req

        start_time = time.time()
        user_text = "\n".join(m["content"] for m in messages if m.get("role") == "user")
        prompt = f"{system_prompt}\n\n{user_text}" if system_prompt else user_text

        try:
            resp = _req.post(
                f"{self._BASE}/v1/tasks",
                json={"prompt": prompt},
                headers=self._headers(),
                timeout=30)
            resp.raise_for_status()
            task = resp.json()
            task_id = task.get("task_id") or task.get("id")
            logger.info(f"[Manus] Task created: {task_id}")

            if not task_id:
                content = task.get("output") or task.get("result") or str(task)
                latency = int((time.time() - start_time) * 1000)
                return ModelResponse(
                    content=str(content), provider="manus",
                    model=self.config.model, latency_ms=latency,
                    success=True)

            last_log = 0
            while (time.time() - start_time) < self._MAX_POLL_SECONDS:
                time.sleep(self._POLL_INTERVAL)
                elapsed = time.time() - start_time
                poll = _req.get(
                    f"{self._BASE}/v1/tasks/{task_id}",
                    headers=self._headers(), timeout=15)
                poll.raise_for_status()
                data = poll.json()
                status = (data.get("status") or "").lower()

                if elapsed - last_log >= 30:
                    logger.info(f"[Manus] Task {task_id} status={status} "
                                f"({elapsed:.0f}s elapsed)")
                    last_log = elapsed

                if status in ("completed", "done", "finished", "success"):
                    content = self._extract_content(data)
                    latency = int((time.time() - start_time) * 1000)
                    logger.info(f"[Manus] Task {task_id} completed "
                                f"in {latency/1000:.0f}s, "
                                f"credits={data.get('credit_usage', '?')}")
                    return ModelResponse(
                        content=content, provider="manus",
                        model=self.config.model, latency_ms=latency,
                        success=True)

                if status in ("failed", "error", "cancelled"):
                    err = data.get("error") or data.get("message") or status
                    latency = int((time.time() - start_time) * 1000)
                    return ModelResponse(
                        provider="manus", model=self.config.model,
                        latency_ms=latency, success=False,
                        error_message=str(err))

            latency = int((time.time() - start_time) * 1000)
            return ModelResponse(
                provider="manus", model=self.config.model,
                latency_ms=latency, success=False,
                error_message=f"Task {task_id} timed out after "
                              f"{self._MAX_POLL_SECONDS}s")

        except Exception as e:
            latency = int((time.time() - start_time) * 1000)
            logger.error(f"[Manus] API error: {e}")
            return ModelResponse(
                provider="manus", model=self.config.model,
                latency_ms=latency, success=False,
                error_message=str(e))


# ─────────────────────────────────────────────────────────────────────
# Model Interface (Main Class)
# ─────────────────────────────────────────────────────────────────────

class ModelInterface:
    """
    Unified interface for all AI model providers.
    Handles model selection, retries, fallback, and cost tracking.

    Key features:
    - Provider-agnostic: same API regardless of Claude/GPT/Gemini
    - Automatic fallback: if primary fails, tries fallback model
    - Retry logic: retries on transient errors
    - Cost tracking: calculates and logs cost for every API call
    - Conversation support: multi-turn dialogues for Trader-Coach pattern
    """

    def __init__(self):
        self._config = get_config()
        self._adapters: Dict[str, Any] = {}
        self._db = None  # Lazy-loaded to avoid circular imports
        self._init_adapters()

    def _init_adapters(self):
        """Initialize adapters for all configured models."""
        for role, model_config in self._config.ai_models.items():
            adapter = self._create_adapter(model_config)
            if adapter:
                self._adapters[role] = {
                    "adapter": adapter,
                    "config": model_config
                }
                logger.info(f"Model adapter initialized: {role} -> "
                            f"{model_config.provider}/{model_config.model}")

    def _create_adapter(self, model_config: AIModelConfig):
        """Create the appropriate adapter based on provider.
        'openai' = direct OpenAI (embeddings/whisper only).
        'openai_compatible' = legacy alias, now routed through OpenRouter.
        'openrouter' = OpenRouter proxy (all chat completions).
        """
        provider = model_config.provider.lower()
        if provider == "anthropic":
            return AnthropicAdapter(model_config)
        elif provider == "openai":
            return OpenAIAdapter(model_config)
        elif provider in ("openai_compatible", "openrouter"):
            model_name = model_config.model
            if "/" not in model_name:
                model_name_l = model_name.lower()
                prefix = (
                    "deepseek" if model_name_l.startswith("deepseek") else
                    "openai"   if model_name_l.startswith(("gpt-", "o1", "o3", "o4")) else
                    "anthropic" if model_name_l.startswith("claude") else
                    "google"   if model_name_l.startswith("gemini") else
                    "qwen"     if model_name_l.startswith("qwen") else
                    "openai"
                )
                model_config.model = f"{prefix}/{model_name}"
                logger.info(f"OpenRouter model prefixed: {model_name} → {model_config.model}")
            if provider == "openai_compatible":
                if model_config.api_key_env == "OPENAI_API_KEY":
                    model_config.api_key_env = "OPENROUTER_API_KEY"
                    model_config.api_key_direct = ""
            return OpenAIAdapter(model_config, base_url="https://openrouter.ai/api/v1")
        elif provider == "deepseek":
            return OpenAIAdapter(model_config, base_url="https://api.deepseek.com/v1")
        elif provider == "manus":
            return ManusAdapter(model_config)
        elif provider == "google":
            return GoogleAdapter(model_config)
        else:
            logger.error(f"Unknown provider: {provider}")
            return None

    @property
    def db(self):
        """Lazy-load database manager to avoid circular imports."""
        if self._db is None:
            from db.database import get_db
            self._db = get_db()
        return self._db

    def query(self, role: str, system_prompt: str, user_prompt: str,
              account_id: str = "global", signal_id: int = None,
              trade_id: int = None, max_tokens: int = None,
              temperature: float = None, model_role: str = "primary",
              retry_count: int = 2, context: str = None,
              source: str = None, source_detail: str = None,
              author: str = None, news_item_id: int = None,
              media_type: str = None, dossier_id: int = None,
              duo_id: str = None,
              **kwargs) -> ModelResponse:
        """
        Send a single query to the AI model.

        Args:
            role: The cognitive role making this query (analyst, trader, coach, post_mortem, daily_review)
            system_prompt: The system/instruction prompt
            user_prompt: The user message content
            account_id: Account ID for logging
            signal_id: Optional signal ID for linking
            trade_id: Optional trade ID for linking
            max_tokens: Override max tokens
            temperature: Override temperature
            model_role: Which configured model to use (primary, fallback, alternative)
            retry_count: Number of retries on failure
            context: Cost tracking context (e.g. alpha_5m_summary, signal_parse, media_analysis)
            source: Granular tracking — source name (telegram, discord, etc.)
            source_detail: Granular tracking — channel/sub-source
            author: Granular tracking — who posted the triggering content
            news_item_id: Granular tracking — FK to news_items
            media_type: Granular tracking — text/image/voice/video

        Returns:
            ModelResponse with content, tokens, cost, etc.
        """
        messages = [{"role": "user", "content": user_prompt}]
        return self.query_conversation(
            role=role,
            system_prompt=system_prompt,
            messages=messages,
            account_id=account_id,
            signal_id=signal_id,
            trade_id=trade_id,
            max_tokens=max_tokens,
            temperature=temperature,
            model_role=model_role,
            retry_count=retry_count,
            context=context,
            source=source,
            source_detail=source_detail,
            author=author,
            news_item_id=news_item_id,
            media_type=media_type,
            dossier_id=dossier_id,
            duo_id=duo_id,
        )

    def query_conversation(self, role: str, system_prompt: str,
                           messages: List[Dict[str, str]],
                           account_id: str = "global",
                           signal_id: int = None, trade_id: int = None,
                           max_tokens: int = None, temperature: float = None,
                           model_role: str = "primary",
                           retry_count: int = 2, context: str = None,
                           source: str = None, source_detail: str = None,
                           author: str = None, news_item_id: int = None,
                           media_type: str = None,
                           dossier_id: int = None,
                           duo_id: str = None) -> ModelResponse:
        """
        Send a multi-turn conversation to the AI model.
        Used for the Trader-Coach dialogue pattern.

        Args:
            role: The cognitive role (trader, coach, etc.)
            system_prompt: System instruction
            messages: List of {"role": "user"/"assistant", "content": "..."}
            account_id: Account ID for logging
            signal_id: Optional signal ID
            trade_id: Optional trade ID
            max_tokens: Override max tokens
            temperature: Override temperature
            model_role: Which model config to use
            retry_count: Retries on failure
            context: Cost tracking context tag
            source: Granular tracking — source name
            source_detail: Granular tracking — channel/sub-source
            author: Granular tracking — who posted the triggering content
            news_item_id: Granular tracking — FK to news_items
            media_type: Granular tracking — text/image/voice/video

        Returns:
            ModelResponse
        """
        # Get the adapter for the requested model role
        adapter_info = self._adapters.get(model_role)
        if not adapter_info:
            logger.error(f"No adapter found for model role: {model_role}")
            return ModelResponse(success=False, error_message=f"No model configured for role: {model_role}")

        adapter = adapter_info["adapter"]
        config = adapter_info["config"]

        # Attempt query with retries
        response = None
        last_error = ""
        for attempt in range(retry_count + 1):
            if attempt > 0:
                wait_time = 2 ** attempt  # Exponential backoff: 2s, 4s
                logger.warning(f"Retry {attempt}/{retry_count} for {role} query, "
                               f"waiting {wait_time}s...")
                time.sleep(wait_time)

            response = adapter.query(
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature
            )

            if response.success:
                response.role = role
                break
            else:
                last_error = response.error_message
                logger.warning(f"Attempt {attempt + 1} failed: {last_error}")

        # If primary failed after all retries, try fallback
        if not response.success and model_role == "primary":
            logger.warning(f"Primary model failed, attempting fallback...")
            fallback_info = self._adapters.get("fallback")
            if fallback_info:
                fallback_adapter = fallback_info["adapter"]
                response = fallback_adapter.query(
                    system_prompt=system_prompt,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature
                )
                if response.success:
                    response.role = role
                    logger.info(f"Fallback model succeeded for {role} query")

        # Log the API call with granular tracking fields
        self._log_api_call(response, account_id, role, signal_id, trade_id, context,
                           source=source, source_detail=source_detail,
                           author=author, news_item_id=news_item_id,
                           media_type=media_type, dossier_id=dossier_id,
                           duo_id=duo_id)

        return response

    def _log_api_call(self, response: ModelResponse, account_id: str,
                      role: str, signal_id: int = None, trade_id: int = None,
                      context: str = None, source: str = None,
                      source_detail: str = None, author: str = None,
                      news_item_id: int = None, media_type: str = None,
                      dossier_id: int = None, duo_id: str = None):
        """Log the API call to the database for cost tracking.
        Context should describe what triggered this call (e.g. alpha_5m_summary,
        signal_parse, media_analysis, idea_check, user_chat). If empty, a WARNING
        is logged — every AI call should be tagged for cost attribution.

        Granular tracking fields (for cost-per-source/user breakdown):
            source: 'discord', 'telegram', 'tradingview_ideas', etc.
            source_detail: channel name, sub-source identifier
            author: who posted the content that triggered the call
            news_item_id: FK to news_items table
            media_type: 'text', 'image', 'voice', 'video'
        """
        if not context:
            logger.warning(f"AI API call without context tag: role={role}, model={response.model}, "
                           f"cost=${response.cost_usd:.4f} — tag all calls for cost tracking!")
        if response.success and response.cost_usd and response.cost_usd > 0:
            logger.info(f"[LLM] {response.model} cost=${response.cost_usd:.4f} (duo: {duo_id or 'n/a'})")
        try:
            cs = getattr(response, "cost_source", "estimated")
            actual = response.cost_usd if cs == "openrouter_actual" else None
            self.db.log_api_call({
                "account_id": account_id,
                "provider": response.provider,
                "model": response.model,
                "role": role,
                "context": context,
                "signal_id": signal_id,
                "trade_id": trade_id,
                "token_count_input": response.token_count_input,
                "token_count_output": response.token_count_output,
                "cost_usd": response.cost_usd,
                "actual_cost_usd": actual,
                "latency_ms": response.latency_ms,
                "success": response.success,
                "error_message": (response.error_message or "")[:5000] if not response.success else None,
                "source": source,
                "source_detail": source_detail,
                "author": author,
                "news_item_id": news_item_id,
                "media_type": media_type,
                "dossier_id": dossier_id,
                "cost_source": cs,
                "duo_id": duo_id,
            })
        except Exception as e:
            # Don't let logging failures break the trading flow
            logger.error(f"Failed to log API call: {e}")

    def get_available_models(self) -> Dict[str, Dict[str, str]]:
        """Return a dictionary of available models for the UI dropdown."""
        models = {}
        for role, info in self._adapters.items():
            config = info["config"]
            models[role] = {
                "provider": config.provider,
                "model": config.model,
                "cost_input": f"${config.cost_per_1m_input}/1M tokens",
                "cost_output": f"${config.cost_per_1m_output}/1M tokens"
            }
        return models

    def estimate_cost(self, input_text: str, estimated_output_tokens: int = 1000,
                      model_role: str = "primary") -> Dict[str, float]:
        """
        Estimate the cost of a query before sending it.
        Useful for budget monitoring.
        """
        adapter_info = self._adapters.get(model_role)
        if not adapter_info:
            return {"estimated_cost_usd": 0, "estimated_input_tokens": 0}

        config = adapter_info["config"]
        # Rough estimate: ~4 chars per token for English text
        estimated_input_tokens = len(input_text) // 4

        input_cost = (estimated_input_tokens / 1_000_000) * config.cost_per_1m_input
        output_cost = (estimated_output_tokens / 1_000_000) * config.cost_per_1m_output

        return {
            "estimated_cost_usd": round(input_cost + output_cost, 6),
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "model": config.model,
            "provider": config.provider
        }

    def query_with_model(self, model_id: str, provider: str,
                        role: str, system_prompt: str, user_prompt: str,
                        account_id: str = "global", max_tokens: int = None,
                        temperature: float = None, context: str = None,
                        source: str = None, source_detail: str = None,
                        author: str = None, news_item_id: int = None,
                        media_type: str = None,
                        dossier_id: int = None,
                        duo_id: str = None,
                        extra_params: dict = None) -> ModelResponse:
        """
        Query a specific model directly (not from the configured adapters).
        Used when prompt_versions specifies a model different from the config.
        Creates a temporary adapter on-the-fly.

        Args:
            model_id: The model identifier (e.g., 'gpt-4.1-mini')
            provider: The provider (e.g., 'openai_compatible', 'openai', 'anthropic')
            role: Cognitive role for logging
            system_prompt: System instruction
            user_prompt: User message
            account_id: For logging
            max_tokens: Override max tokens
            temperature: Override temperature

        Returns:
            ModelResponse
        """
        # Check if we already have this model in a cached adapter
        cache_key = f"_dynamic_{provider}_{model_id}"
        adapter_info = self._adapters.get(cache_key)

        if not adapter_info:
            _PROVIDER_PRICING_DEFAULTS = {
                "deepseek":  (0.27, 1.10),
                "openrouter": (0.15, 0.60),
                "openai":    (0.40, 1.60),
                "anthropic": (3.00, 15.00),
                "google":    (0.15, 0.60),
                "manus":     (0.50, 1.50),
            }
            _def_in, _def_out = _PROVIDER_PRICING_DEFAULTS.get(
                provider.lower(), (0.15, 0.60))
            input_price = _def_in
            output_price = _def_out
            max_tokens_param = "max_tokens"
            temperature_max = 2.0

            try:
                row = self.db.fetch_one(
                    """SELECT input_price_per_1m, output_price_per_1m, max_tokens_param, 
                              temperature_max, context_window
                       FROM known_models WHERE model_id=%s AND provider=%s""",
                    (model_id, provider)
                )
                if not row:
                    row = self.db.fetch_one(
                        """SELECT input_price_per_1m, output_price_per_1m, max_tokens_param,
                                  temperature_max, context_window
                           FROM known_models WHERE model_id=%s LIMIT 1""",
                        (model_id,)
                    )
                if row:
                    input_price = float(row.get('input_price_per_1m') or _def_in)
                    output_price = float(row.get('output_price_per_1m') or _def_out)
                    max_tokens_param = row.get('max_tokens_param') or "max_tokens"
                    temperature_max = float(row.get('temperature_max') or 2.0)
            except Exception:
                pass

            # Get the API key based on provider
            import os as _os
            provider_key_map = {
                "anthropic": "ANTHROPIC_API_KEY",
                "google": "GOOGLE_API_KEY",
                "openai": "OPENAI_API_KEY",
                "openai_compatible": "OPENROUTER_API_KEY",
                "openrouter": "OPENROUTER_API_KEY",
                "deepseek": "DEEPSEEK_API_KEY",
                "manus": "MANUS_API_KEY",
            }
            api_key_env = provider_key_map.get(provider, "OPENROUTER_API_KEY")
            api_key = _os.environ.get(api_key_env, "").strip()
            # Fallback: if provider-specific key not found, try primary adapter
            if not api_key:
                primary_info = self._adapters.get('primary')
                if primary_info:
                    api_key = primary_info['config'].api_key
                    api_key_env = primary_info['config'].api_key_env

            # Cap temperature to max allowed for this model
            effective_temp = temperature if temperature is not None else 0.3
            effective_temp = min(effective_temp, temperature_max)

            config = AIModelConfig(
                provider=provider,
                model=model_id,
                api_key_env=api_key_env,
                api_key_direct=api_key if api_key else '',
                max_tokens=max_tokens or 16384,
                temperature=effective_temp,
                cost_per_1m_input=input_price,
                cost_per_1m_output=output_price
            )
            adapter = self._create_adapter(config)
            if not adapter:
                return ModelResponse(
                    success=False,
                    error_message=f"Could not create adapter for {provider}/{model_id}"
                )
            # Cache it for future use
            self._adapters[cache_key] = {
                "adapter": adapter,
                "config": config
            }
            adapter_info = self._adapters[cache_key]
            logger.info(f"Dynamic adapter created: {provider}/{model_id}")

        adapter = adapter_info["adapter"]
        # Support vision: user_prompt can be a string or a list of content parts
        # (e.g. [{"type":"text","text":"..."}, {"type":"image_url","image_url":{...}}])
        messages = [{"role": "user", "content": user_prompt}]

        try:
            query_kwargs = dict(
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if extra_params and hasattr(adapter, 'query'):
                import inspect
                sig = inspect.signature(adapter.query)
                if 'extra_params' in sig.parameters:
                    query_kwargs['extra_params'] = extra_params

            response = adapter.query(**query_kwargs)
            if response.success:
                response.role = role
            # Log the API call with granular tracking
            self._log_api_call(response, account_id, role, context=context,
                               source=source, source_detail=source_detail,
                               author=author, news_item_id=news_item_id,
                               media_type=media_type, dossier_id=dossier_id,
                               duo_id=duo_id)
            return response
        except Exception as e:
            logger.error(f"Dynamic model query failed ({provider}/{model_id}): {e}")
            return ModelResponse(
                success=False,
                error_message=str(e),
                provider=provider,
                model=model_id
            )

    def discover_all_models(self) -> List[Dict[str, Any]]:
        """
        Return all known models from the known_models table.
        Used by the Configuration tab and Prompt Engineer model dropdowns.
        """
        try:
            rows = self.db.fetch_all(
                """SELECT model_id, provider, display_name, is_available, is_recommended,
                          input_price_per_1m, output_price_per_1m, context_window,
                          supports_vision, supports_function_calling,
                          supports_audio, supports_video
                   FROM known_models
                   ORDER BY provider, display_name"""
            )
            return [
                {
                    "id": r.get("model_id", ""),
                    "provider": r.get("provider", ""),
                    "display_name": r.get("display_name", r.get("model_id", "")),
                    "is_available": bool(r.get("is_available", False)),
                    "is_recommended": bool(r.get("is_recommended", False)),
                    "input_price": float(r.get("input_price_per_1m") or 0),
                    "output_price": float(r.get("output_price_per_1m") or 0),
                    "max_context": r.get("context_window", 0),
                    "supports_vision": bool(r.get("supports_vision", False)),
                    "supports_function_calling": bool(r.get("supports_function_calling", False)),
                    "supports_audio": bool(r.get("supports_audio", False)),
                    "supports_video": bool(r.get("supports_video", False)),
                }
                for r in (rows or [])
            ]
        except Exception as e:
            logger.error(f"Failed to discover models: {e}")
            # Fallback: return models from the current adapters
            return [
                {
                    "id": info["config"].model,
                    "provider": info["config"].provider,
                    "display_name": info["config"].model,
                    "is_available": True,
                    "input_price": info["config"].cost_per_1m_input,
                    "output_price": info["config"].cost_per_1m_output,
                }
                for role, info in self._adapters.items()
                if not role.startswith("_dynamic_")
            ]

    def auto_refresh_model_availability(self):
        """
        Auto-refresh known_models availability on startup based on which API keys
        are configured in the environment. Marks models as available/unavailable.
        Called once during bootstrap.
        """
        import os as _os
        provider_key_map = {
            "anthropic": "ANTHROPIC_API_KEY",
            "google": "GOOGLE_API_KEY",
            "openai": "OPENAI_API_KEY",
            "openai_compatible": "OPENROUTER_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "manus": "MANUS_API_KEY",
        }

        try:
            rows = self.db.fetch_all("SELECT id, provider, model_id, is_available FROM known_models")
            if not rows:
                return

            updated = 0
            for r in rows:
                provider = r.get("provider", "")
                key_env = provider_key_map.get(provider, "OPENAI_API_KEY")
                has_key = bool(_os.environ.get(key_env, "").strip())
                current = bool(r.get("is_available"))

                if has_key != current:
                    self.db.execute(
                        "UPDATE known_models SET is_available = %s WHERE id = %s",
                        (1 if has_key else 0, r["id"])
                    )
                    updated += 1

            if updated:
                logger.info(f"[ModelRefresh] Updated availability for {updated} models based on API keys")
                create_notification(
                    f"Model availability updated: {updated} models changed",
                    "Model catalog was refreshed based on configured API keys.",
                    notif_type="model_update"
                )
        except Exception as e:
            logger.warning(f"[ModelRefresh] Error: {e}")

    def switch_model(self, role: str, new_model_config: Dict[str, Any]) -> bool:
        """
        Switch a model at runtime (e.g., from Claude Opus 4.6 to GPT-5.3).
        Used by the UI dropdown or for A/B testing.
        """
        try:
            config = AIModelConfig(**new_model_config)
            adapter = self._create_adapter(config)
            if adapter:
                self._adapters[role] = {
                    "adapter": adapter,
                    "config": config
                }
                logger.info(f"Model switched: {role} -> {config.provider}/{config.model}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to switch model: {e}")
            return False


# ─────────────────────────────────────────────────────────────────────
# Singleton accessor
# ─────────────────────────────────────────────────────────────────────

_mi_instance = None

def get_model_interface() -> ModelInterface:
    """Get the singleton ModelInterface instance."""
    global _mi_instance
    if _mi_instance is None:
        _mi_instance = ModelInterface()
    return _mi_instance


def create_notification(title: str, detail: str = None, notif_type: str = "system"):
    """
    Create a notification in the notifications table.
    Utility function callable from anywhere in the codebase.
    Types: 'system', 'model_update', 'error', 'media', 'cost_alert', 'quota_exceeded', 'balance_low'
    """
    try:
        from db.database import get_db
        get_db().execute(
            "INSERT INTO notifications (type, title, detail) VALUES (%s, %s, %s)",
            (notif_type, title[:300], (detail or "")[:5000])
        )
    except Exception as e:
        logger.debug(f"Failed to create notification: {e}")


def check_api_balance_and_notify():
    """
    Check today's API spend and create notifications if:
    - Spend exceeds warning threshold ($10)
    - Any provider returns balance/quota errors
    
    Called periodically by the dashboard.
    """
    try:
        from db.database import get_db
        db = get_db()
        
        # Get today's total cost (table is ai_api_log; use timestamp for date)
        row = db.fetch_one("""
            SELECT COALESCE(SUM(cost_usd), 0) as today_cost
            FROM ai_api_log
            WHERE DATE(timestamp) = CURDATE()
        """)
        today_cost = float(row['today_cost']) if row else 0.0
        
        # Check for recent quota/balance errors in the last hour
        errors = db.fetch_all("""
            SELECT provider, error_message, COUNT(*) as cnt
            FROM ai_api_log
            WHERE success = 0
            AND timestamp > DATE_SUB(NOW(), INTERVAL 1 HOUR)
            AND (error_message LIKE '%quota%' OR error_message LIKE '%balance%' OR error_message LIKE '%429%' OR error_message LIKE '%rate limit%')
            GROUP BY provider, error_message
        """)
        
        # Get the last time we notified about this to avoid spam
        last_notif = db.fetch_one("""
            SELECT created_at FROM notifications 
            WHERE type = 'quota_exceeded' 
            ORDER BY created_at DESC LIMIT 1
        """)
        
        should_notify = True
        if last_notif:
            from datetime import datetime, timedelta
            last_time = last_notif['created_at']
            if isinstance(last_time, str):
                last_time = datetime.fromisoformat(last_time.replace('Z', '+00:00'))
            # Only notify once per hour
            if datetime.utcnow() - last_time.replace(tzinfo=None) < timedelta(hours=1):
                should_notify = False
        
        if errors and should_notify:
            error_msgs = []
            for e in errors:
                provider = e['provider'] or 'Unknown'
                cnt = e['cnt']
                error_msgs.append(f"{provider}: {cnt} errors")
            
            create_notification(
                "⚠️ API Quota/Balance Exceeded",
                f"API errors detected in the last hour:\n" + "\n".join(error_msgs) + 
                "\n\nFalling back to alternative models. Check your API balances.",
                "quota_exceeded"
            )
            logger.warning(f"[BalanceCheck] Quota errors detected: {error_msgs}")
        
        # Check if daily spend is high
        if today_cost > 10.0:
            last_spend_notif = db.fetch_one("""
                SELECT created_at FROM notifications 
                WHERE type = 'balance_low' AND title LIKE '%Daily API spend%'
                ORDER BY created_at DESC LIMIT 1
            """)
            
            should_notify_spend = True
            if last_spend_notif:
                from datetime import datetime, timedelta
                last_time = last_spend_notif['created_at']
                if isinstance(last_time, str):
                    last_time = datetime.fromisoformat(last_time.replace('Z', '+00:00'))
                if datetime.utcnow() - last_time.replace(tzinfo=None) < timedelta(hours=6):
                    should_notify_spend = False
            
            if should_notify_spend:
                create_notification(
                    f"💰 Daily API spend: ${today_cost:.2f}",
                    f"Today's API costs have reached ${today_cost:.2f}. Consider monitoring usage.",
                    "balance_low"
                )
        
        return {
            "today_cost": today_cost,
            "quota_errors": len(errors) if errors else 0,
            "notified": should_notify and len(errors) > 0
        }
        
    except Exception as e:
        logger.error(f"[BalanceCheck] Error checking balance: {e}")
        return {"error": str(e)}
