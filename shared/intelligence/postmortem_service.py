"""
Module: postmortem_service
Purpose: Centralised post-mortem daemon for every closed tracked_position.
Location: /opt/tickles/shared/intelligence/postmortem_service.py

F3: This service was previously a stub that wrote canned text into columns
that did not exist in the live ``public.position_postmortems`` schema. F3
replaces the stub with a real LLM call (``chat_completion``) over the last
50 1m candles inside the trade window, parses the structured JSON result,
and persists it through a schema-correct INSERT.

The service is idempotent at three layers:
  1. Advisory lock (``pg_try_advisory_lock``) so only one instance ticks.
  2. ``uq_postmortem_composite (position_id, postmortem_version, prompt_version)``
     prevents duplicate rows.
  3. ``WHERE postmortem_status = 'pending'`` skips rows we already finished.

Failure modes:
  * No candles available  -> postmortem_status = 'skipped_no_candles'
  * LLM transport failure -> postmortem_status = 'failed'
  * LLM JSON parse failure -> postmortem_status = 'failed'
  * Successful write       -> postmortem_status = 'done'
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

# Ensure shared imports resolve
_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SHARED)
for p in (_ROOT, _SHARED):
    if p not in sys.path:
        sys.path.insert(0, p)

from shared.intelligence.gateway_config import GatewayConfig, chat_completion
from shared.intelligence.heartbeat import record_heartbeat
from shared.intelligence.position_monitor import _resolve_instrument_id
from shared.intelligence.prompt_registry import register_prompt
from shared.utils.correlation import new_correlation_id
from shared.utils.db import DatabasePool, get_shared_pool

logger = logging.getLogger("tickles.postmortem")

_POSTMORTEM_VERSION = "v2"
_PROMPT_PATH = os.path.join(_HERE, "prompts", "postmortem.json")
_DEFAULT_BATCH = int(os.getenv("POSTMORTEM_BATCH_SIZE", "10"))
_DEFAULT_INTERVAL_S = int(os.getenv("POSTMORTEM_INTERVAL_S", "60"))
_CANDLE_LIMIT = int(os.getenv("POSTMORTEM_CANDLE_LIMIT", "50"))
# OpenRouter expects model IDs WITHOUT the leading "openrouter/" provider
# prefix (it routes by the part after the first slash). We accept both forms
# in env so legacy configs keep working, but normalise on read.
_RAW_MODEL = os.getenv("SIGNAL_POSTMORTEM_MODEL", "openai/gpt-4o-mini")
_DEFAULT_MODEL = _RAW_MODEL[len("openrouter/"):] if _RAW_MODEL.startswith("openrouter/") else _RAW_MODEL
_GATEWAY_SERVICE = os.getenv("POSTMORTEM_GATEWAY_SERVICE", "postmortem")

_ALLOWED_REGIMES = {
    "trending_up",
    "trending_down",
    "ranging",
    "volatile",
    "unknown",
}

_INSERT_SQL = """
INSERT INTO public.position_postmortems (
    position_id, postmortem_version, prompt_version,
    postmortem_provider, postmortem_model, param_hash, candle_data_hash,
    what_happened, why_it_worked, why_it_failed,
    trader_thesis_validated, llm_thesis_validated,
    regime_at_entry, regime_at_exit,
    lessons_for_actor, lessons_for_company,
    cost_usd, latency_ms, correlation_id
) VALUES (
    $1, $2, $3,
    $4, $5, $6, $7,
    $8, $9, $10,
    $11, $12,
    $13, $14,
    $15, $16,
    $17, $18, $19
)
ON CONFLICT (position_id, postmortem_version, prompt_version) DO NOTHING
RETURNING id
"""

# Note: ``tracked_positions`` does NOT have an ``exit_reason_postmortem``
# column on this database — earlier code paths that referenced it were
# dead. We only update ``postmortem_status`` here. The detailed reason
# (skipped_no_candles / failed / done) is captured by ``status_reason``
# at the position-monitor layer; the postmortem service writes the rich
# narrative into ``position_postmortems.what_happened`` instead.
_UPDATE_STATUS_SQL = """
UPDATE public.tracked_positions
SET postmortem_status = $1
WHERE id = $2
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _hash16(text: str) -> str:
    """Return the first 16 hex chars of the sha256 digest of ``text``.

    Used to fill the ``char(16)`` columns ``param_hash`` and
    ``candle_data_hash`` on ``position_postmortems``.

    Args:
        text: Any string to hash.

    Returns:
        16-character lowercase hex string.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _decimal_str(value: Any) -> str:
    """Render a numeric DB value as a plain decimal string.

    asyncpg returns ``Decimal`` for numeric columns. Standard ``str()``
    is fine for these but we centralise it so future formatting (rounding,
    locale, etc.) lives in one place.

    Args:
        value: Decimal | float | int | None.

    Returns:
        Plain decimal string, or ``"None"`` when value is None.
    """
    if value is None:
        return "None"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_llm_json(content: str) -> Dict[str, Any]:
    """Extract the JSON object from an LLM response.

    Some models wrap JSON in markdown fences or prose. We strip a fenced
    block first, then fall back to the widest ``{ ... }`` slice.

    Args:
        content: Raw text returned by the LLM.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: when no JSON object can be parsed.
    """
    text = content.strip()
    if text.startswith("```"):
        # strip the first fence and any "json" tag, then strip the closing fence
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(text)
        if not match:
            raise ValueError("LLM response contained no JSON object")
        return json.loads(match.group(0))


def _coerce_regime(value: Any) -> str:
    """Map an LLM-supplied regime label to one of the allowed values.

    Args:
        value: Whatever the LLM returned for a regime field.

    Returns:
        A value from ``_ALLOWED_REGIMES`` (defaults to ``'unknown'``).
    """
    if not isinstance(value, str):
        return "unknown"
    norm = value.strip().lower().replace(" ", "_").replace("-", "_")
    return norm if norm in _ALLOWED_REGIMES else "unknown"


def _coerce_optional_bool(value: Any) -> Optional[bool]:
    """Coerce a value to ``Optional[bool]``.

    Strings like ``'true'``/``'false'``/``'null'`` are recognised; anything
    else becomes ``None``. Used for ``trader_thesis_validated`` and
    ``llm_thesis_validated`` which are nullable bool columns.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0"):
            return False
    return None


def _truncate(value: Any, max_len: int) -> Optional[str]:
    """Clamp a string-ish value to ``max_len`` chars.

    None / empty becomes None so the column stores SQL NULL.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class PostMortemService:
    """Polls closed tracked_positions and runs causal LLM post-mortems."""

    def __init__(
        self,
        company_id: str = "jarvais",
        *,
        batch_size: int = _DEFAULT_BATCH,
        interval_seconds: int = _DEFAULT_INTERVAL_S,
        candle_limit: int = _CANDLE_LIMIT,
        model: Optional[str] = None,
    ) -> None:
        """Build a PostMortemService.

        Args:
            company_id: Filters tracked_positions to one company.
            batch_size: Max positions to process per tick.
            interval_seconds: Sleep between ticks in ``run_forever``.
            candle_limit: Max 1m candles to feed the LLM per position.
            model: Override the LLM model identifier (else env / default).
        """
        self.company_id = company_id
        self.batch_size = batch_size
        self.interval_seconds = interval_seconds
        self.candle_limit = candle_limit
        self._stop = asyncio.Event()
        self._pool: Optional[DatabasePool] = None
        self._gateway: Optional[GatewayConfig] = None
        self.prompt_version: Optional[str] = None
        self._prompts: Dict[str, Any] = {}
        self._model = model or _DEFAULT_MODEL

    # ------------------------------------------------------------------
    # Lazy init
    # ------------------------------------------------------------------
    async def _ensure_pool(self) -> DatabasePool:
        """Return a memoised shared Postgres pool."""
        if self._pool is None:
            self._pool = await get_shared_pool()
        return self._pool

    def _ensure_gateway(self) -> GatewayConfig:
        """Return a memoised LLM gateway config for the postmortem service."""
        if self._gateway is None:
            self._gateway = GatewayConfig.for_service(_GATEWAY_SERVICE)
        return self._gateway

    def _load_prompts(self) -> Dict[str, Any]:
        """Load the postmortem prompt JSON from disk."""
        if not os.path.exists(_PROMPT_PATH):
            logger.warning("postmortem prompt file not found at %s", _PROMPT_PATH)
            return {}
        try:
            with open(_PROMPT_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("failed to load postmortem prompt: %s", exc)
            return {}

    async def _register_prompt(self) -> str:
        """Register the postmortem prompt body in ``prompt_registry``.

        Returns:
            The registered prompt version hash, or ``'fallback'`` when the
            prompt file is missing.
        """
        self._prompts = self._load_prompts()
        pm = self._prompts.get("postmortem", {})
        if not pm:
            logger.warning("no postmortem prompt found; using 'fallback' version")
            return "fallback"
        return await register_prompt(
            name="postmortem",
            version=pm.get("version", "unknown"),
            system=pm.get("system_prompt"),
            body=pm.get("user_prompt_template", ""),
            taxonomy_rule=pm.get("taxonomy_rule"),
            model_hint=self._model,
            created_by="postmortem_service",
        )

    # ------------------------------------------------------------------
    # DB queries
    # ------------------------------------------------------------------
    async def _fetch_pending(self, conn: asyncpg.Connection) -> List[asyncpg.Record]:
        """Fetch closed positions awaiting post-mortem for this company."""
        try:
            return await conn.fetch(
                """
                SELECT id, instrument_symbol, instrument_exchange, direction,
                       entry_price, exit_price, outcome,
                       max_drawdown_pct, max_profit_pct, time_in_trade_minutes,
                       entry_reason_trader, entry_reason_llm, exit_reason,
                       signal_timestamp, closed_at,
                       realized_pnl_usd_final, status_reason
                FROM public.tracked_positions
                WHERE status = 'closed'
                  AND postmortem_status = 'pending'
                  AND company_id = $1
                ORDER BY closed_at ASC
                LIMIT $2
                """,
                self.company_id,
                self.batch_size,
            )
        except Exception as exc:
            logger.exception("postmortem: _fetch_pending failed: %s", exc)
            raise

    async def _fetch_candles(
        self,
        conn: asyncpg.Connection,
        instrument_id: int,
        start: datetime,
        end: datetime,
    ) -> List[asyncpg.Record]:
        """Return up to ``self.candle_limit`` 1m candles inside the window.

        We pull the most recent ``candle_limit`` candles ending at ``end``
        (most relevant to causation around the close) but never older than
        ``start``. The list is returned oldest-first for prompt readability.

        Args:
            conn: Live asyncpg connection (held under advisory lock).
            instrument_id: ``public.instruments.id`` to filter on.
            start: lower bound (inclusive) — typically opened_at.
            end: upper bound (inclusive) — typically closed_at.

        Returns:
            Candle records ordered oldest-first.
        """
        try:
            rows = await conn.fetch(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM public.candles
                WHERE instrument_id = $1
                  AND timeframe = '1m'::timeframe_t
                  AND timestamp BETWEEN $2 AND $3
                ORDER BY timestamp DESC
                LIMIT $4
                """,
                instrument_id,
                start,
                end,
                self.candle_limit,
            )
        except Exception as exc:
            logger.exception(
                "postmortem: _fetch_candles instrument_id=%s start=%s end=%s: %s",
                instrument_id,
                start,
                end,
                exc,
            )
            return []
        return list(reversed(rows))

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------
    def _build_user_prompt(
        self,
        position: asyncpg.Record,
        candles: List[asyncpg.Record],
    ) -> str:
        """Render the user prompt template with position + candle context.

        The template lives in ``prompts/postmortem.json``. Missing fields
        fall back to ``'unknown'`` strings so the prompt is always
        well-formed.
        """
        template = self._prompts.get("postmortem", {}).get("user_prompt_template", "")
        candle_rows = [
            {
                "t": c["timestamp"].isoformat(),
                "o": _decimal_str(c["open"]),
                "h": _decimal_str(c["high"]),
                "l": _decimal_str(c["low"]),
                "c": _decimal_str(c["close"]),
                "v": _decimal_str(c["volume"]),
            }
            for c in candles
        ]
        return template.format(
            instrument_symbol=position["instrument_symbol"] or "unknown",
            instrument_exchange=position["instrument_exchange"] or "unknown",
            direction=position["direction"] or "unknown",
            entry_price=_decimal_str(position["entry_price"]),
            exit_price=_decimal_str(position["exit_price"]),
            outcome=position["outcome"] or "unknown",
            max_drawdown_pct=_decimal_str(position["max_drawdown_pct"]),
            max_profit_pct=_decimal_str(position["max_profit_pct"]),
            time_in_trade_minutes=position["time_in_trade_minutes"] or 0,
            realized_pnl_usd_final=_decimal_str(position["realized_pnl_usd_final"]),
            entry_reason_trader=(position["entry_reason_trader"] or "(none provided)"),
            entry_reason_llm=(position["entry_reason_llm"] or "(none provided)"),
            exit_reason=(position["exit_reason"] or "(none recorded)"),
            candles_json=json.dumps(candle_rows, separators=(",", ":")),
        )

    async def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        correlation_id: str,
    ) -> Tuple[Dict[str, Any], int, str]:
        """Invoke the configured LLM gateway and return parsed JSON.

        Args:
            system_prompt: Static system prompt loaded from JSON.
            user_prompt: Rendered per-position user prompt.
            correlation_id: 12-char ID propagated to cost logs.

        Returns:
            Tuple of (parsed_json_dict, latency_ms, model_actually_used).
        """
        cfg = self._ensure_gateway()
        start = datetime.now(timezone.utc)
        resp = await chat_completion(
            cfg,
            model=self._model,
            system_prompt=system_prompt,
            user_text=user_prompt,
            correlation_id=correlation_id,
            operation="postmortem",
            company_id=self.company_id,
            agent_id="intelligence-postmortem",
        )
        latency_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        parsed = _parse_llm_json(resp.get("content", ""))
        return parsed, latency_ms, str(resp.get("model") or self._model)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    async def _write_postmortem(
        self,
        conn: asyncpg.Connection,
        position_id: int,
        parsed: Dict[str, Any],
        *,
        provider: str,
        model: str,
        param_hash: str,
        candle_data_hash: Optional[str],
        latency_ms: int,
        correlation_id: str,
    ) -> Optional[int]:
        """Insert a ``position_postmortems`` row with the parsed LLM output.

        ``ON CONFLICT DO NOTHING`` makes this idempotent across reruns —
        the unique key is ``(position_id, postmortem_version, prompt_version)``.

        Returns:
            New row id, or None when the row already existed.
        """
        what_happened = _truncate(parsed.get("what_happened"), 2000)
        if not what_happened:
            raise ValueError("LLM returned empty 'what_happened' (required column)")

        try:
            return await conn.fetchval(
                _INSERT_SQL,
                position_id,
                _POSTMORTEM_VERSION,
                self.prompt_version or "fallback",
                provider,
                model,
                param_hash,
                candle_data_hash,
                what_happened,
                _truncate(parsed.get("why_it_worked"), 2000),
                _truncate(parsed.get("why_it_failed"), 2000),
                _coerce_optional_bool(parsed.get("trader_thesis_validated")),
                _coerce_optional_bool(parsed.get("llm_thesis_validated")),
                _coerce_regime(parsed.get("regime_at_entry")),
                _coerce_regime(parsed.get("regime_at_exit")),
                _truncate(parsed.get("lessons_for_actor"), 2000),
                _truncate(parsed.get("lessons_for_company"), 2000),
                Decimal("0"),  # cost_usd populated by api_cost_log; placeholder column value
                latency_ms,
                correlation_id[:36],
            )
        except Exception as exc:
            logger.exception(
                "postmortem: INSERT failed for position_id=%s: %s", position_id, exc
            )
            raise

    # ------------------------------------------------------------------
    # Per-position pipeline
    # ------------------------------------------------------------------
    async def _process_one(
        self,
        conn: asyncpg.Connection,
        position: asyncpg.Record,
    ) -> None:
        """Run the full postmortem pipeline for one closed position.

        Steps:
          1. Resolve ``instruments.id`` from symbol/exchange.
          2. Fetch up to ``candle_limit`` 1m candles inside the trade window.
          3. Render prompts and call the LLM.
          4. Persist a ``position_postmortems`` row.
          5. Update ``tracked_positions.postmortem_status``.

        Any error is caught and translates to a ``'failed'`` status with a
        truncated reason; we never re-raise so a single bad row does not
        kill the batch.
        """
        pos_id = int(position["id"])
        correlation_id = new_correlation_id()
        logger.info("postmortem: processing position_id=%s cid=%s", pos_id, correlation_id)

        pool = await self._ensure_pool()
        instrument_id = await _resolve_instrument_id(
            pool,
            position["instrument_symbol"],
            position["instrument_exchange"],
        )
        if instrument_id is None:
            logger.warning(
                "postmortem: cannot resolve instrument for position_id=%s symbol=%r exchange=%r",
                pos_id,
                position["instrument_symbol"],
                position["instrument_exchange"],
            )
            await conn.execute(
                _UPDATE_STATUS_SQL,
                "skipped_no_candles",
                pos_id,
            )
            return

        # ``tracked_positions`` has no ``opened_at`` column on this DB —
        # ``signal_timestamp`` (NOT NULL) is the only authoritative open
        # marker we have for both live signals and F9-backfilled orphans.
        opened_at = position["signal_timestamp"]
        closed_at = position["closed_at"]
        if opened_at is None or closed_at is None:
            logger.warning(
                "postmortem: position_id=%s missing signal_timestamp/closed_at — skipping",
                pos_id,
            )
            await conn.execute(
                _UPDATE_STATUS_SQL, "skipped_no_candles", pos_id
            )
            return

        candles = await self._fetch_candles(conn, instrument_id, opened_at, closed_at)
        if not candles:
            logger.info(
                "postmortem: no candles for position_id=%s window=%s..%s",
                pos_id,
                opened_at,
                closed_at,
            )
            await conn.execute(
                _UPDATE_STATUS_SQL, "skipped_no_candles", pos_id
            )
            return

        prompts = self._prompts.get("postmortem", {})
        system_prompt = prompts.get("system_prompt", "")
        user_prompt = self._build_user_prompt(position, candles)
        if not system_prompt or not user_prompt:
            logger.error("postmortem: empty prompt for position_id=%s", pos_id)
            await conn.execute(
                _UPDATE_STATUS_SQL, "failed", pos_id
            )
            return

        try:
            parsed, latency_ms, used_model = await self._call_llm(
                system_prompt, user_prompt, correlation_id
            )
        except Exception as exc:
            logger.exception(
                "postmortem: LLM call failed for position_id=%s: %s", pos_id, exc
            )
            await conn.execute(
                _UPDATE_STATUS_SQL, "failed", pos_id
            )
            return

        param_hash = _hash16(
            f"{used_model}|{self.prompt_version}|{_POSTMORTEM_VERSION}|{system_prompt}"
        )
        candle_data_hash = _hash16(
            f"{candles[0]['timestamp']}|{candles[-1]['timestamp']}|{len(candles)}"
        )
        provider = self._gateway.gateway if self._gateway else "unknown"

        try:
            row_id = await self._write_postmortem(
                conn,
                pos_id,
                parsed,
                provider=provider,
                model=used_model,
                param_hash=param_hash,
                candle_data_hash=candle_data_hash,
                latency_ms=latency_ms,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            logger.exception(
                "postmortem: INSERT failed for position_id=%s: %s", pos_id, exc
            )
            await conn.execute(
                _UPDATE_STATUS_SQL, "failed", pos_id
            )
            return

        if row_id:
            await conn.execute(_UPDATE_STATUS_SQL, "done", pos_id)
            logger.info(
                "postmortem: wrote row id=%s for position_id=%s in %sms",
                row_id,
                pos_id,
                latency_ms,
            )
        else:
            logger.info(
                "postmortem: position_id=%s already had a postmortem (ON CONFLICT)",
                pos_id,
            )
            await conn.execute(_UPDATE_STATUS_SQL, "done", pos_id)

    # ------------------------------------------------------------------
    # Tick / loop
    # ------------------------------------------------------------------
    async def tick(self) -> Dict[str, Any]:
        """Single processing tick under an advisory lock.

        Returns:
            Summary dict with counts of processed/failed/skipped rows.
        """
        pool = await self._ensure_pool()
        summary = {"processed": 0, "failed": 0, "skipped": 0, "pending_total": 0}
        async with pool.acquire() as conn:
            got_lock = await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtext('postmortem_service'))"
            )
            if not got_lock:
                logger.info(
                    "postmortem_service: another instance holds the lock; skipping tick"
                )
                summary["skipped_lock_busy"] = True
                return summary
            try:
                pending = await self._fetch_pending(conn)
                summary["pending_total"] = len(pending)
                if not pending:
                    logger.debug("postmortem: no pending positions")
                else:
                    for position in pending:
                        if self._stop.is_set():
                            break
                        try:
                            await self._process_one(conn, position)
                            summary["processed"] += 1
                        except Exception as exc:
                            summary["failed"] += 1
                            logger.exception(
                                "postmortem: failed processing position %s: %s",
                                position["id"],
                                exc,
                            )
                            try:
                                await conn.execute(
                                    _UPDATE_STATUS_SQL,
                                    "failed",
                                    position["id"],
                                )
                            except Exception as ue:
                                logger.exception(
                                    "postmortem: status update also failed for %s: %s",
                                    position["id"],
                                    ue,
                                )
            finally:
                try:
                    await conn.execute(
                        "SELECT pg_advisory_unlock(hashtext('postmortem_service'))"
                    )
                except Exception as exc:
                    logger.warning("postmortem: advisory_unlock failed: %s", exc)
                try:
                    # cron_heartbeats.last_status_check only allows
                    # 'ok' | 'error' | 'partial'. We always report 'ok' on a
                    # successful tick (regardless of whether work was done);
                    # individual position failures are recorded in summary.
                    hb_status = "partial" if summary.get("failed", 0) else "ok"
                    await record_heartbeat(
                        agent_id="intelligence-postmortem",
                        status=hb_status,
                        expected_interval_seconds=self.interval_seconds,
                    )
                except Exception as exc:
                    logger.warning("postmortem: heartbeat write failed: %s", exc)
        return summary

    async def run_forever(self) -> None:
        """Main loop: register prompt, then tick on a fixed interval."""
        self.prompt_version = await self._register_prompt()
        logger.info(
            "postmortem_service: prompt_version=%s model=%s gateway_service=%s",
            self.prompt_version,
            self._model,
            _GATEWAY_SERVICE,
        )
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                logger.exception("postmortem_service: tick failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        """Signal the run loop to exit after the current tick."""
        self._stop.set()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
async def main() -> None:
    """Async entrypoint for ``python -m shared.intelligence.postmortem_service``."""
    parser = argparse.ArgumentParser(description="Tickles PostMortemService")
    parser.add_argument("--company", default="jarvais", help="Company ID")
    parser.add_argument("--once", action="store_true", help="Run one tick and exit")
    parser.add_argument(
        "--interval", type=int, default=_DEFAULT_INTERVAL_S, help="Poll interval seconds"
    )
    parser.add_argument(
        "--batch", type=int, default=_DEFAULT_BATCH, help="Max positions per tick"
    )
    parser.add_argument(
        "--candle-limit",
        type=int,
        default=_CANDLE_LIMIT,
        help="Max 1m candles per position",
    )
    parser.add_argument(
        "--model", default=None, help="Override LLM model (else env / default)"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    svc = PostMortemService(
        company_id=args.company,
        batch_size=args.batch,
        interval_seconds=args.interval,
        candle_limit=args.candle_limit,
        model=args.model,
    )
    svc.prompt_version = await svc._register_prompt()

    if args.once:
        summary = await svc.tick()
        logger.info("postmortem: once-tick summary=%s", summary)
        return

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, svc.stop)
        except NotImplementedError:
            # Windows / restricted runtimes
            logger.debug("postmortem: signal handler %s not installable", sig)

    await svc.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
