"""Shared dependency container for Tickles MCP tools.

``ToolContext`` holds the HTTP clients, connection pools and config that every
tool group needs. We keep it deliberately small and injectable so unit tests
can stand up a registry with fakes.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import urllib.error
import urllib.parse
import urllib.request
import json as _json


@dataclass
class ToolContext:
    paperclip_url: str = field(
        default_factory=lambda: os.environ.get(
            "PAPERCLIP_URL", "http://127.0.0.1:3100"
        )
    )
    paperclip_token: Optional[str] = field(
        default_factory=lambda: os.environ.get("PAPERCLIP_API_TOKEN")
    )
    mem0_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "MEM0_MCP_URL", "http://127.0.0.1:8765"
        )
    )
    memu_dsn: Optional[str] = field(
        default_factory=lambda: os.environ.get("MEMU_DSN")
    )
    qdrant_url: str = field(
        default_factory=lambda: os.environ.get(
            "QDRANT_URL", "http://127.0.0.1:6333"
        )
    )
    # Optional live agent-id for audit trail (set by the daemon per-request)
    caller_agent_id: Optional[str] = None
    caller_company_id: Optional[str] = None

    def paperclip(
        self,
        method: str,
        path: str,
        *,
        body: Any | None = None,
        query: Dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> Any:
        """Call Paperclip's HTTP API. Returns parsed JSON or raises."""
        url = self.paperclip_url.rstrip("/") + path
        if query:
            qs = urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None}
            )
            if qs:
                url += ("&" if "?" in url else "?") + qs
        headers = {"content-type": "application/json"}
        if self.paperclip_token:
            headers["authorization"] = f"Bearer {self.paperclip_token}"
        if self.caller_agent_id:
            headers["x-mcp-agent-id"] = self.caller_agent_id
        data = _json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                raw = resp.read()
                if not raw:
                    return None
                return _json.loads(raw)
        except urllib.error.HTTPError as err:
            payload = err.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(
                f"paperclip {method} {path} -> HTTP {err.code}: {payload}"
            ) from err
        finally:
            # Bounded verbosity on slow calls so we can debug latency.
            elapsed = time.time() - started
            if elapsed > 2.0:  # pragma: no cover - logging only
                import logging
                logging.getLogger("tickles.mcp.tools").info(
                    "[paperclip] slow %s %s elapsed=%.2fs", method, path, elapsed
                )
