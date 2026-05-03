"""
Module: payload_store
Purpose: Atomic JSON writer for LLM request/response payloads with secret redaction.
Location: /opt/tickles/shared/intelligence/payload_store.py
"""

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_DEFAULT_PAYLOAD_MAX_BYTES = int(os.environ.get("PAYLOAD_MAX_BYTES", "5242880"))  # 5 MB
_PAYLOAD_BASE_DIR = Path(
    os.environ.get("PAYLOAD_BASE_DIR", "/opt/tickles/shared/reports/signal_payloads")
)
_SECRET_DENYLIST: List[str] = [
    "authorization",
    "x-api-key",
    "cookie",
    "api-key",
    "api_key",
    "token",
    "x-auth-token",
    "bearer",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _redact_secrets(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep copy of payload with secret headers/fields scrubbed.

    Recursively walks dicts and lists. Any dict key matching the secret
    denylist (case-insensitive) has its value replaced with "[REDACTED]".
    """
    if isinstance(payload, dict):
        result: Dict[str, Any] = {}
        for key, value in payload.items():
            if key.lower() in _SECRET_DENYLIST:
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact_secrets(value)
        return result
    if isinstance(payload, list):
        return [_redact_secrets(item) for item in payload]  # type: ignore[return-value]
    return payload


def _ensure_dir(path: Path) -> None:
    """Create parent directories if they don't exist."""
    path.parent.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, data: Dict[str, Any], max_bytes: int) -> None:
    """Write JSON atomically via tmpfile + os.replace.

    Raises:
        ValueError: if serialized JSON exceeds max_bytes.
        OSError: on filesystem errors.
    """
    _ensure_dir(path)
    raw = json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    if len(raw) > max_bytes:
        raise ValueError(
            f"Payload size {len(raw)} bytes exceeds max {max_bytes} bytes for {path.name}"
        )
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.tmp", suffix=".json"
    )
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def compute_prompt_hash(system_prompt: str, user_prompt: str) -> str:
    """Compute SHA-256 of system + user prompt, return first 16 hex chars.

    Args:
        system_prompt: The system/instruction text sent to the LLM.
        user_prompt: The user message text sent to the LLM.

    Returns:
        16-character hex string (64 bits of SHA-256).
    """
    combined = f"{system_prompt}\n---USER---\n{user_prompt}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]


def compute_prompt_version(prompts_json: Dict[str, Any]) -> str:
    """Extract the version field from a prompt config JSON.

    Falls back to a hash of the system_prompt if no version is present.

    Args:
        prompts_json: The loaded prompt configuration dict.

    Returns:
        Semver-ish version string or hash fallback.
    """
    chart_cfg = prompts_json.get("chart_analysis", {})
    version = chart_cfg.get("version", "")
    if version:
        return str(version)
    # Fallback: hash of system_prompt for reproducibility
    system = chart_cfg.get("system_prompt", "")
    return hashlib.sha256(system.encode("utf-8")).hexdigest()[:12]


def build_payload_paths(
    correlation_id: str,
    base_dir: Optional[Path] = None,
) -> Tuple[Path, Path]:
    """Build date-bucketed file paths for request and response payloads.

    Args:
        correlation_id: The tracing ID for this operation chain.
        base_dir: Override the default payload base directory.

    Returns:
        (request_path, response_path) as Path objects.
    """
    now = datetime.now(timezone.utc)
    bucket = base_dir or _PAYLOAD_BASE_DIR
    date_dir = bucket / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
    req_path = date_dir / f"{correlation_id}.req.json"
    resp_path = date_dir / f"{correlation_id}.resp.json"
    return req_path, resp_path


async def save_payload_pair(
    correlation_id: str,
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
    max_bytes: Optional[int] = None,
    base_dir: Optional[Path] = None,
) -> Tuple[str, str]:
    """Atomically write redacted request + response JSON to disk.

    Args:
        correlation_id: Tracing ID — used in filename.
        request_payload: The raw request dict (will be redacted).
        response_payload: The raw response dict (will be redacted).
        max_bytes: Max serialized JSON size per file (default 5 MB).
        base_dir: Override default payload directory.

    Returns:
        (request_path_str, response_path_str) as absolute paths.

    Raises:
        ValueError: if either payload exceeds max_bytes after serialization.
        OSError: on filesystem errors.
    """
    limit = max_bytes or _DEFAULT_PAYLOAD_MAX_BYTES
    req_path, resp_path = build_payload_paths(correlation_id, base_dir)

    req_redacted = _redact_secrets(request_payload)
    resp_redacted = _redact_secrets(response_payload)

    try:
        _atomic_write_json(req_path, req_redacted, limit)
        _atomic_write_json(resp_path, resp_redacted, limit)
    except ValueError as exc:
        logger.warning("Payload too large for correlation_id=%s: %s", correlation_id, exc)
        raise
    except OSError as exc:
        logger.error("Failed to write payload for correlation_id=%s: %s", correlation_id, exc)
        raise

    logger.debug(
        "Saved payloads for correlation_id=%s: req=%s resp=%s",
        correlation_id,
        req_path.name,
        resp_path.name,
    )
    return str(req_path), str(resp_path)


def build_request_payload(
    *,
    provider: str,
    model_requested: str,
    system_prompt: str,
    user_text: str,
    image_b64: Optional[str] = None,
    temperature: float = 0.7,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a standardized request payload dict for persistence.

    Args:
        provider: The gateway provider name (e.g. 'openrouter', 'requesty').
        model_requested: The model string sent in the request.
        system_prompt: The system/instruction text.
        user_text: The user message text.
        image_b64: Base64-encoded image data (if vision call).
        temperature: Sampling temperature.
        extra: Additional metadata to include.

    Returns:
        Dict suitable for JSON serialization and redaction.
    """
    payload: Dict[str, Any] = {
        "provider": provider,
        "model_requested": model_requested,
        "system_prompt": system_prompt,
        "user_text": user_text,
        "temperature": temperature,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    if image_b64 is not None:
        # Store a hash of the image, not the full base64 (too large)
        payload["image_b64_sha256"] = hashlib.sha256(
            image_b64.encode("utf-8")
        ).hexdigest()[:32]
        payload["image_b64_length"] = len(image_b64)
    if extra:
        payload["extra"] = extra
    return payload


def build_response_payload(
    *,
    model_resolved: str,
    content: str,
    usage: Optional[Dict[str, Any]] = None,
    latency_ms: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a standardized response payload dict for persistence.

    Args:
        model_resolved: The actual model that produced the response.
        content: The raw response text/content.
        usage: Token usage dict from the provider.
        latency_ms: Round-trip latency in milliseconds.
        extra: Additional metadata.

    Returns:
        Dict suitable for JSON serialization.
    """
    payload: Dict[str, Any] = {
        "model_resolved": model_resolved,
        "content": content,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    if usage is not None:
        payload["usage"] = usage
    if latency_ms is not None:
        payload["latency_ms"] = latency_ms
    if extra:
        payload["extra"] = extra
    return payload
