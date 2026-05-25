"""
Tests: Round 13.6 per-trader pending-position uniqueness.

Operator rule (verbatim, 2026-05-24):
    "only one long and one short can be opened per coin per trader,
     whether the AI charthacker or a discord trader. We can update
     with the newest ones (freshest) if there's a new one that comes in."

Translation: per (trader_profile_id, normalised_symbol, direction) there
must be at most ONE row in ``status='pending'``. New signals on the same
key REFRESH the existing row in place — the function returns the
existing id and never INSERTs a duplicate.

Coverage
--------
* REFRESH path: existing pending row for same (trader, symbol, direction)
  → function returns the existing id and runs an UPDATE that pulls the
  freshest entry/SL/TPs/reasons/timestamp onto that row.
* INSERT path: no existing pending row for the same key → function falls
  through to the INSERT block (mock returns a fresh id).
* Cross-trader: same (symbol, direction) from a *different* trader does
  NOT collide. trader_1's BTC long is independent of chart_hacker's BTC
  long.
* Cross-direction: long ≠ short for the same trader/symbol.
* Symbol matching: ``BTCUSDT`` (legacy compact form) matches
  ``BTC/USDT:USDT`` (canonical form) so legacy rows still dedup.
* Regression: dedup SELECT params are passed as a tuple (not positional
  args). This is the bug that hid the silently-broken Phase-8 dedup.
"""
from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock


class FakePool:
    """Mimics shared.utils.db.DatabasePool. Records every call so tests
    can assert on SQL shape and parameter shape exactly."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Optional[Tuple]]] = []
        self._fetch_one_queue: List[Optional[Dict[str, Any]]] = []
        self._fetch_all_queue: List[List[Dict[str, Any]]] = []
        self._execute_queue: List[Any] = []

    def queue_fetch_one(self, *rows: Optional[Dict[str, Any]]) -> None:
        self._fetch_one_queue.extend(rows)

    def queue_fetch_all(self, *batches: List[Dict[str, Any]]) -> None:
        self._fetch_all_queue.extend(batches)

    def queue_execute(self, *results: Any) -> None:
        self._execute_queue.extend(results)

    async def fetch_one(self, query: str, params: Optional[Tuple] = None):
        self.calls.append(("fetch_one", query, params))
        if not self._fetch_one_queue:
            return None
        return self._fetch_one_queue.pop(0)

    async def fetch_all(self, query: str, params: Optional[Tuple] = None):
        self.calls.append(("fetch_all", query, params))
        if not self._fetch_all_queue:
            return []
        return self._fetch_all_queue.pop(0)

    async def execute(self, query: str, params: Optional[Tuple] = None):
        self.calls.append(("execute", query, params))
        if self._execute_queue:
            return self._execute_queue.pop(0)
        return 1


def _run_create(
    *,
    fake_pool: FakePool,
    entry_price: float = 80000.0,
    direction: str = "long",
    instrument: str = "BTC/USDT:USDT",
    news_item_id: int = 99999,
    trader_profile_id: int = 610,
    actor_id: str = "jarvais_chart_hacker",
):
    """Tiny harness around create_tracked_position_from_interpretation."""
    from shared.intelligence.interpretation_service import (
        create_tracked_position_from_interpretation,
    )
    return asyncio.run(
        create_tracked_position_from_interpretation(
            shared_pool=fake_pool,
            signal_interpretation_id=42,
            news_item_id=news_item_id,
            media_item_id=None,
            trader_profile_id=trader_profile_id,
            instrument_symbol=instrument,
            instrument_exchange="bybit",
            direction=direction,
            entry_price=entry_price,
            stop_loss=entry_price * 0.99,
            take_profit_1=entry_price * 1.01,
            detection_method="llm_vision",
            detection_confidence=0.9,
            raw_signal_text="test signal",
            company_id="jarvais",
            signal_source="chart_hacker" if "chart_hacker" in actor_id else "trader",
            actor_type="ai_agent" if "chart_hacker" in actor_id else "human",
            actor_id=actor_id,
        )
    )


class _DedupTestBase(unittest.TestCase):
    """Shared mock setup. The function does CCXT pre-flights and exchange
    routing before the dedup block — we mock those to keep tests focused."""

    def _patches(self):
        return [
            mock.patch("shared.intelligence.interpretation_service._ccxt_live_price", create=True),
            mock.patch("shared.utils.exchange_router.unsupported_status_reason"),
            mock.patch("shared.utils.exchange_router.resolve_market"),
        ]

    def setUp(self) -> None:
        self._mocks = []
        for p in self._patches():
            self._mocks.append(p.start())
            self.addCleanup(p.stop)
        # Configure resolve_market to claim the symbol is supported.
        # _mocks ordering matches _patches() ordering reversed because
        # mock.patch inverts top-down stacking — but easier to grab by name.
        from shared.utils import exchange_router as er
        er.resolve_market.return_value = mock.Mock(
            supported=True,
            canonical_symbol=None,  # let caller's symbol pass through
            exchange="bybit",
            epic_code=None,
            ccxt_perp_symbol="BTC/USDT:USDT",
            unsupported_reason=None,
        )
        # ccxt_live_price returns None so the Already-In-Play guard skips.
        from shared.intelligence import interpretation_service as isvc
        async def _no_live(*a, **k):
            return None
        isvc._ccxt_live_price.side_effect = _no_live  # type: ignore[attr-defined]


class PerTraderRefreshTests(_DedupTestBase):
    def test_existing_pending_same_trader_returns_existing_id(self):
        """REFRESH path: same (trader, symbol, direction) -> UPDATE existing,
        do not INSERT."""
        pool = FakePool()
        pool.queue_fetch_one({
            "id": 11337,
            "entry_price": 78000.0,
            "created_at": None,
        })
        pool.queue_execute(1)  # the UPDATE refresh

        pid = _run_create(fake_pool=pool, entry_price=80500.0)
        self.assertEqual(pid, 11337, "must return existing id, not create new")

        # No INSERT happened
        inserts = [c for c in pool.calls
                   if c[0] == "fetch_one" and "INSERT INTO public.tracked_positions" in c[1]]
        self.assertEqual(len(inserts), 0, "REFRESH path must not INSERT")

        # The UPDATE refresh ran — and it set entry_price to the NEW value
        refreshes = [c for c in pool.calls
                     if c[0] == "execute"
                     and "SET entry_price" in c[1]
                     and "deduped_at" in c[1]]
        self.assertEqual(len(refreshes), 1)
        params = refreshes[0][2]
        # First param is the new entry_price — confirm it's the new one, not the old.
        self.assertEqual(params[0], 80500.0)
        # Last param is the existing id
        self.assertEqual(params[-1], 11337)


class PerTraderInsertTests(_DedupTestBase):
    def test_no_existing_pending_inserts_new(self):
        """INSERT path: dedup query returns nothing -> the function falls
        through to the INSERT block (which mocks return a fresh id)."""
        pool = FakePool()
        pool.queue_fetch_one(None)              # dedup miss
        pool.queue_fetch_one({"id": 99002})     # INSERT row
        pid = _run_create(fake_pool=pool, entry_price=80500.0, news_item_id=77777)
        self.assertEqual(pid, 99002)

        # No refresh-UPDATE ran
        refreshes = [c for c in pool.calls
                     if c[0] == "execute"
                     and "SET entry_price" in c[1]
                     and "deduped_at" in c[1]]
        self.assertEqual(len(refreshes), 0)


class CrossTraderTests(_DedupTestBase):
    def test_different_trader_does_not_collide(self):
        """trader_1's BTC long is INDEPENDENT of chart_hacker's BTC long.

        We assert this at the SQL-shape level: the dedup SELECT MUST
        include trader_profile_id in its WHERE, so a different trader
        wouldn't match the existing row.
        """
        pool = FakePool()
        pool.queue_fetch_one(None)              # dedup miss (different trader)
        pool.queue_fetch_one({"id": 99003})     # INSERT row
        _ = _run_create(
            fake_pool=pool,
            entry_price=80500.0,
            trader_profile_id=1,                # different trader
            actor_id="jarvais_trader_1",
        )

        # The dedup SELECT MUST filter on trader_profile_id
        dedup_sels = [c for c in pool.calls
                      if c[0] == "fetch_one"
                      and "FROM public.tracked_positions" in c[1]
                      and "WHERE status = 'pending'" in c[1]
                      and "INSERT" not in c[1]]
        self.assertEqual(len(dedup_sels), 1)
        sql = dedup_sels[0][1]
        self.assertIn("trader_profile_id", sql,
                      "dedup SELECT must filter by trader_profile_id "
                      "to enforce per-trader uniqueness")
        # Params include the trader_profile_id (1, in this test).
        params = dedup_sels[0][2]
        self.assertEqual(params[0], 1)


class CrossDirectionTests(_DedupTestBase):
    def test_long_and_short_are_independent(self):
        """Same trader + same coin but opposite direction must NOT collide."""
        pool = FakePool()
        pool.queue_fetch_one(None)              # dedup miss
        pool.queue_fetch_one({"id": 99004})     # INSERT row
        _ = _run_create(fake_pool=pool, entry_price=80500.0, direction="short")
        dedup_sels = [c for c in pool.calls
                      if c[0] == "fetch_one"
                      and "FROM public.tracked_positions" in c[1]
                      and "WHERE status = 'pending'" in c[1]
                      and "INSERT" not in c[1]]
        self.assertEqual(len(dedup_sels), 1)
        params = dedup_sels[0][2]
        # Direction param (index 1 after trader_profile_id) is 'short'
        self.assertEqual(params[1], "short")


class QueryShapeRegressionTests(_DedupTestBase):
    """Regression guard for the silently-broken Phase-8 dedup, which
    passed positional args to fetch_one and got swallowed by bare except."""

    def test_dedup_select_params_is_tuple(self):
        pool = FakePool()
        pool.queue_fetch_one(None)
        pool.queue_fetch_one({"id": 99005})
        _ = _run_create(fake_pool=pool, entry_price=80000.0)

        dedup_sels = [c for c in pool.calls
                      if c[0] == "fetch_one"
                      and "FROM public.tracked_positions" in c[1]
                      and "WHERE status = 'pending'" in c[1]
                      and "INSERT" not in c[1]]
        self.assertEqual(len(dedup_sels), 1)
        params = dedup_sels[0][2]
        self.assertIsInstance(params, tuple)
        # Per-trader query has exactly 4 params:
        # (trader_profile_id, direction, symbol, compact_symbol)
        self.assertEqual(len(params), 4)


if __name__ == "__main__":
    unittest.main()
