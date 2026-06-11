"""
Module: test_postmortem_service
Purpose: Smoke tests for the F3-rewritten PostMortemService (real LLM,
         schema-correct INSERT, idempotent status update).
Location: /opt/tickles/shared/tests/test_postmortem_service.py
"""

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/opt/tickles")

from shared.intelligence.postmortem_service import (
    PostMortemService,
    _build_postmortem_broadcast,
    _coerce_optional_bool,
    _coerce_regime,
    _hash16,
    _parse_llm_json,
    _truncate,
    _ALLOWED_REGIMES,
    _MEMU_POSTMORTEM_KIND,
    _POSTMORTEM_VERSION,
)


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------
class FakeConn:
    """Minimal asyncpg.Connection stand-in capturing all SQL traffic."""

    def __init__(
        self,
        *,
        pending: Optional[List[Dict[str, Any]]] = None,
        candles: Optional[List[Dict[str, Any]]] = None,
        lock_acquired: bool = True,
        insert_id: Optional[int] = 4242,
    ) -> None:
        self.executed: List[Tuple[str, tuple]] = []
        self.fetched: List[Tuple[str, tuple]] = []
        self._pending = pending or []
        self._candles = candles or []
        self._lock_acquired = lock_acquired
        self._insert_id = insert_id

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.fetched.append((query, args))
        if "pg_try_advisory_lock" in query:
            return self._lock_acquired
        if "INSERT INTO public.position_postmortems" in query:
            return self._insert_id
        return None

    async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
        self.fetched.append((query, args))
        if "FROM public.tracked_positions" in query:
            return self._pending
        if "FROM public.candles" in query:
            return self._candles
        return []

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append((query, args))

    async def fetchrow(self, query: str, *args: Any) -> Optional[Dict[str, Any]]:
        self.fetched.append((query, args))
        return None


class _FakeCtx:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakePool:
    """Minimal DatabasePool stand-in."""

    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def acquire(self) -> _FakeCtx:
        return _FakeCtx(self.conn)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def service() -> PostMortemService:
    """Service instance with a stable prompt_version for deterministic tests."""
    svc = PostMortemService(company_id="testco", batch_size=5, candle_limit=10)
    svc.prompt_version = "test-v1"
    svc._prompts = {
        "postmortem": {
            "system_prompt": "system_prompt_text",
            "user_prompt_template": (
                "{direction}|{entry_price}|{exit_price}|{outcome}"
                "|{realized_pnl_usd_final}|{max_drawdown_pct}|{max_profit_pct}"
                "|{time_in_trade_minutes}|{entry_reason_trader}"
                "|{entry_reason_llm}|{exit_reason}|{candles_json}"
            ),
        }
    }
    return svc


def _sample_position() -> Dict[str, Any]:
    """Return a synthetic closed-position record matching the fetched columns."""
    opened = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    closed = opened + timedelta(hours=2)
    return {
        "id": 42,
        "instrument_symbol": "BTC/USDT",
        "instrument_exchange": "bybit",
        "direction": "long",
        "entry_price": Decimal("76000.00"),
        "exit_price": Decimal("78000.00"),
        "outcome": "tp1_hit",
        "max_drawdown_pct": Decimal("-1.5"),
        "max_profit_pct": Decimal("3.2"),
        "time_in_trade_minutes": 120,
        "entry_reason_trader": "RSI oversold",
        "entry_reason_llm": "Bullish breakout",
        "exit_reason": "tp1",
        "signal_timestamp": opened,
        "closed_at": closed,
        "realized_pnl_usd_final": Decimal("180.00"),
        "status_reason": "expired_auto_close",
        # Phase J columns (pulled by _fetch_pending; used by _push_lessons_to_mem0)
        "signal_source": "trader",
        "actor_id": "rose",
        "actor_type": "trader",
        "trader_profile_id": 7,
        "timeframe": "1h",
        "company_id": "jarvais",
        "trader_handle": "rose",
        "trader_platform": "telegram",
    }


def _sample_candles(n: int = 3) -> List[Dict[str, Any]]:
    """Return ``n`` synthetic 1m candle rows oldest-first."""
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    return [
        {
            "timestamp": base + timedelta(minutes=i),
            "open": Decimal("76000") + i,
            "high": Decimal("76050") + i,
            "low": Decimal("75950") + i,
            "close": Decimal("76020") + i,
            "volume": Decimal("1.5"),
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Pure-helper unit tests
# ---------------------------------------------------------------------------
def test_hash16_returns_16_lowercase_hex() -> None:
    """``_hash16`` produces a 16-char lowercase hex digest."""
    h = _hash16("hello world")
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_coerce_regime_known_value() -> None:
    """A regime present in ``_ALLOWED_REGIMES`` survives unchanged."""
    assert _coerce_regime("trending_up") == "trending_up"
    assert _coerce_regime("Trending_Up") == "trending_up"


def test_coerce_regime_unknown_falls_back() -> None:
    """Unknown / empty regime values collapse to ``unknown``."""
    assert _coerce_regime("garbage") == "unknown"
    assert _coerce_regime(None) == "unknown"
    assert _coerce_regime("") == "unknown"


def test_allowed_regimes_set_is_complete() -> None:
    """The allow-list contains the canonical 5-regime taxonomy."""
    assert _ALLOWED_REGIMES == {
        "trending_up",
        "trending_down",
        "ranging",
        "volatile",
        "unknown",
    }


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("FALSE", False),
        ("yes", True),
        ("no", False),
        ("1", True),
        ("0", False),
        (None, None),
        ("garbage", None),
    ],
)
def test_coerce_optional_bool(raw: Any, expected: Optional[bool]) -> None:
    """Bool coercion handles the canonical truthy/falsy strings + bool/None."""
    assert _coerce_optional_bool(raw) is expected


def test_truncate_clamps_long_text() -> None:
    """``_truncate`` clamps overlong strings to the configured limit."""
    out = _truncate("x" * 5000, 100)
    assert out is not None
    assert len(out) <= 100


def test_truncate_returns_none_for_empty() -> None:
    """Empty / whitespace inputs collapse to ``None``."""
    assert _truncate("", 50) is None
    assert _truncate(None, 50) is None
    assert _truncate("   ", 50) is None


def test_parse_llm_json_strips_code_fence() -> None:
    """JSON wrapped in a ```json fence is extracted cleanly."""
    raw = '```json\n{"what_happened": "ok", "regime_at_entry": "ranging"}\n```'
    parsed = _parse_llm_json(raw)
    assert parsed["what_happened"] == "ok"
    assert parsed["regime_at_entry"] == "ranging"


def test_parse_llm_json_handles_loose_object() -> None:
    """A naked JSON object embedded in prose is still extractable."""
    raw = "Sure, here is the analysis: {\"what_happened\": \"price ran\"} thanks!"
    parsed = _parse_llm_json(raw)
    assert parsed["what_happened"] == "price ran"


# ---------------------------------------------------------------------------
# Service state tests
# ---------------------------------------------------------------------------
def test_service_init_defaults() -> None:
    """Default constructor values match the documented contract."""
    svc = PostMortemService()
    assert svc.company_id == "jarvais"
    assert svc.batch_size > 0
    assert svc.interval_seconds > 0
    assert svc.candle_limit > 0
    assert svc._stop.is_set() is False


def test_postmortem_version_constant() -> None:
    """The schema-aligned rewrite bumps the version to v2."""
    assert _POSTMORTEM_VERSION == "v2"


# ---------------------------------------------------------------------------
# tick() — no pending positions
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tick_no_pending_positions_releases_lock(service: PostMortemService) -> None:
    """An empty pending set still acquires + releases the advisory lock."""
    conn = FakeConn(pending=[])
    pool = FakePool(conn)
    service._pool = pool

    summary = await service.tick()

    assert summary["pending_total"] == 0
    assert summary["processed"] == 0
    assert any("pg_try_advisory_lock" in q for q, _ in conn.fetched)
    assert any("pg_advisory_unlock" in q for q, _ in conn.executed)


@pytest.mark.asyncio
async def test_tick_skips_when_lock_busy(service: PostMortemService) -> None:
    """If another instance holds the advisory lock, tick exits cleanly."""
    conn = FakeConn(pending=[], lock_acquired=False)
    pool = FakePool(conn)
    service._pool = pool

    summary = await service.tick()

    # Did NOT proceed past lock check
    assert summary.get("skipped_lock_busy") is True
    # No SELECT against tracked_positions
    assert not any(
        "FROM public.tracked_positions" in q for q, _ in conn.fetched
    )


# ---------------------------------------------------------------------------
# _process_one — instrument unresolved
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_process_one_marks_skipped_when_instrument_unresolved(
    service: PostMortemService,
) -> None:
    """Unresolvable instrument → status = skipped_no_candles, no LLM call."""
    conn = FakeConn()
    pool = FakePool(conn)
    service._pool = pool
    position = _sample_position()

    with patch(
        "shared.intelligence.postmortem_service._resolve_instrument_id",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await service._process_one(conn, position)

    assert any(
        "UPDATE public.tracked_positions" in q
        and args[0] == "skipped_no_candles"
        and args[1] == position["id"]
        for q, args in conn.executed
    )


# ---------------------------------------------------------------------------
# _process_one — no candles in window
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_process_one_marks_skipped_when_no_candles(
    service: PostMortemService,
) -> None:
    """Empty candle window → status = skipped_no_candles, no LLM call."""
    conn = FakeConn(candles=[])
    pool = FakePool(conn)
    service._pool = pool
    position = _sample_position()

    with patch(
        "shared.intelligence.postmortem_service._resolve_instrument_id",
        new_callable=AsyncMock,
        return_value=99,
    ), patch.object(
        service, "_call_llm", new_callable=AsyncMock
    ) as mock_llm:
        await service._process_one(conn, position)
        mock_llm.assert_not_called()

    assert any(
        args[0] == "skipped_no_candles" and args[1] == position["id"]
        for q, args in conn.executed
        if "UPDATE public.tracked_positions" in q
    )


# ---------------------------------------------------------------------------
# _process_one — happy path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_process_one_writes_postmortem_and_marks_done(
    service: PostMortemService,
) -> None:
    """Full pipeline: instrument → candles → LLM → INSERT → status=done."""
    conn = FakeConn(candles=_sample_candles(3), insert_id=777)
    pool = FakePool(conn)
    service._pool = pool
    position = _sample_position()

    parsed = {
        "what_happened": "Long held; TP1 reached after 2h consolidation.",
        "why_it_worked": "Order flow confirmed breakout above pivot.",
        "why_it_failed": None,
        "trader_thesis_validated": True,
        "llm_thesis_validated": True,
        "regime_at_entry": "trending_up",
        "regime_at_exit": "trending_up",
        "lessons_for_actor": "Keep using RSI<30 + breakout confirmation.",
        "lessons_for_company": "Pattern works across BTC majors.",
    }

    with patch(
        "shared.intelligence.postmortem_service._resolve_instrument_id",
        new_callable=AsyncMock,
        return_value=11,
    ), patch.object(
        service, "_call_llm", new_callable=AsyncMock
    ) as mock_llm, patch(
        # Isolate the learning-loop side-effect (mem0 + MemU outbox) — that path
        # is covered separately by test_build_postmortem_broadcast_*.
        "shared.intelligence.postmortem_service._push_lessons_to_mem0",
        new_callable=AsyncMock,
    ):
        mock_llm.return_value = (parsed, 1234, "openrouter/openai/gpt-4o-mini")
        await service._process_one(conn, position)

    # INSERT was called with $1=position_id, $2=version, $3=prompt_version,
    # $8=what_happened …
    insert_calls = [
        (q, args)
        for q, args in conn.fetched
        if "INSERT INTO public.position_postmortems" in q
    ]
    assert len(insert_calls) == 1
    _, insert_args = insert_calls[0]
    assert insert_args[0] == position["id"]
    assert insert_args[1] == _POSTMORTEM_VERSION
    assert insert_args[2] == "test-v1"
    assert insert_args[7] == parsed["what_happened"]
    assert insert_args[12] in _ALLOWED_REGIMES  # regime_at_entry
    assert insert_args[13] in _ALLOWED_REGIMES  # regime_at_exit

    # Status update wrote 'done' for this exact position id
    assert any(
        args == ("done", position["id"])
        for q, args in conn.executed
        if "UPDATE public.tracked_positions" in q
    )


# ---------------------------------------------------------------------------
# _process_one — LLM failure path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_process_one_marks_failed_on_llm_exception(
    service: PostMortemService,
) -> None:
    """LLM call raising → status = failed, no INSERT."""
    conn = FakeConn(candles=_sample_candles(3))
    pool = FakePool(conn)
    service._pool = pool
    position = _sample_position()

    with patch(
        "shared.intelligence.postmortem_service._resolve_instrument_id",
        new_callable=AsyncMock,
        return_value=11,
    ), patch.object(
        service, "_call_llm", new_callable=AsyncMock, side_effect=RuntimeError("gateway down")
    ):
        await service._process_one(conn, position)

    # No INSERT happened
    assert not any(
        "INSERT INTO public.position_postmortems" in q for q, _ in conn.fetched
    )
    # Status set to 'failed' for this position
    assert any(
        args == ("failed", position["id"])
        for q, args in conn.executed
        if "UPDATE public.tracked_positions" in q
    )


# ---------------------------------------------------------------------------
# _load_prompts fallback
# ---------------------------------------------------------------------------
def test_load_prompts_returns_empty_when_file_missing() -> None:
    """Missing prompt JSON yields ``{}`` (caller falls back gracefully)."""
    svc = PostMortemService(company_id="testco")
    with patch("os.path.exists", return_value=False):
        prompts = svc._load_prompts()
    assert prompts == {}


# ---------------------------------------------------------------------------
# Phase A — _build_postmortem_broadcast (pure; MemU lesson promotion)
# ---------------------------------------------------------------------------
def test_build_postmortem_broadcast_skips_when_no_institutional_content() -> None:
    """No company lesson and no edge → None (nothing worth promoting)."""
    payload = _build_postmortem_broadcast(
        company="jarvais",
        position_id=1,
        symbol="BTC/USDT",
        exchange="bybit",
        direction="long",
        outcome="sl_hit",
        signal_source="trader",
        trader_handle="rose",
        parsed={"lessons_for_actor": "only an actor note", "lessons_for_company": ""},
    )
    assert payload is None


def test_build_postmortem_broadcast_trader_payload() -> None:
    """A human-trader postmortem promotes as actor_type='trader'."""
    payload = _build_postmortem_broadcast(
        company="jarvais",
        position_id=99,
        symbol="ETH/USDT",
        exchange="bybit",
        direction="short",
        outcome="tp1_hit",
        signal_source="trader",
        trader_handle="Rose",
        parsed={
            "lessons_for_company": "Shorts into resistance after RSI divergence work.",
            "edge_detected": "RSI divergence at HTF resistance",
            "why_it_worked": "Price rejected the level cleanly.",
        },
    )
    assert payload is not None
    assert payload["insight_kind"] == _MEMU_POSTMORTEM_KIND
    assert payload["actor_type"] == "trader"
    assert payload["actor_id"] == "rose"  # normalised lower
    assert payload["company"] == "jarvais"
    assert payload["position_id"] == 99
    assert payload["instrument_symbol_normalised"] == "ETH/USDT"
    assert payload["instrument_exchange"] == "bybit"
    assert payload["correlation_id"] == "pm-99"  # default when none supplied
    assert payload["schema_version"] == 1
    assert "RSI divergence" in payload["body_md"]
    assert payload["summary"] == "ETH/USDT short → tp1_hit"


def test_build_postmortem_broadcast_chart_hacker_is_agent() -> None:
    """chart_hacker's own postmortem promotes as actor_type='agent'."""
    payload = _build_postmortem_broadcast(
        company="jarvais",
        position_id=7,
        symbol="SOL/USDT",
        exchange=None,
        direction="long",
        outcome="sl_hit",
        signal_source="chart_hacker",
        trader_handle="",
        parsed={"lessons_for_company": "Avoid chasing SOL longs in chop."},
        correlation_id="cid-abc",
    )
    assert payload is not None
    assert payload["actor_type"] == "agent"
    assert payload["actor_id"] == "chart_hacker"
    assert payload["correlation_id"] == "cid-abc"
    # exchange omitted when None (NotRequired field)
    assert "instrument_exchange" not in payload
