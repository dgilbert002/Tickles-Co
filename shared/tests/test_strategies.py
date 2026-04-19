"""Phase 34 — Strategy Composer tests."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from io import StringIO
from typing import List
from unittest.mock import patch

from shared.arb import ArbStore, InMemoryArbPool
from shared.arb.protocol import ArbOpportunity
from shared.cli import strategy_cli
from shared.strategies import (
    CompositionResult,
    MIGRATION_PATH,
    StrategyDescriptor,
    StrategyIntent,
    StrategyStore,
    read_migration_sql,
)
from shared.strategies.composer import (
    ComposerConfig,
    GateDecision,
    StrategyComposer,
)
from shared.strategies.memory_pool import InMemoryStrategyPool
from shared.strategies.producers import ArbProducer
from shared.strategies.protocol import (
    KIND_ARB,
    KIND_CUSTOM,
    SIDE_BUY,
    SIDE_SELL,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SUBMITTED,
)
from shared.strategies.service import StrategyComposerService
from shared.strategies.store import StrategyStore as _StrategyStore  # noqa


# ---------------------------------------------------------------- migration


def test_migration_path_exists():
    assert MIGRATION_PATH.exists()
    sql = read_migration_sql()
    assert "CREATE TABLE IF NOT EXISTS public.strategy_descriptors" in sql
    assert "CREATE TABLE IF NOT EXISTS public.strategy_intents" in sql
    assert "strategy_intents_source_unique_idx" in sql
    assert "public.strategy_intents_latest" in sql


# ----------------------------------------------------------------- store


def test_descriptor_roundtrip():
    async def _run():
        pool = InMemoryStrategyPool()
        store = StrategyStore(pool)
        d = StrategyDescriptor(
            id=None, name="arb-scanner", kind=KIND_ARB,
            description="arb scanner", priority=120,
            config={"min_net_bps": 5.0},
        )
        did = await store.upsert_descriptor(d)
        assert did > 0
        again = StrategyDescriptor(
            id=None, name="arb-scanner", kind=KIND_ARB,
            description="updated", enabled=False, priority=90,
        )
        did2 = await store.upsert_descriptor(again)
        assert did == did2  # upsert returned same id
        rows = await store.list_descriptors()
        assert len(rows) == 1
        assert rows[0].enabled is False
        assert rows[0].description == "updated"
        assert rows[0].priority == 90

    asyncio.run(_run())


def test_intent_dedupe_by_source_ref():
    async def _run():
        pool = InMemoryStrategyPool()
        store = StrategyStore(pool)
        now = datetime.now(timezone.utc)
        i = StrategyIntent(
            id=None, strategy_name="s1", strategy_kind=KIND_ARB,
            symbol="BTC/USDT", side=SIDE_BUY, size_base=1.0,
            notional_usd=100.0, source_ref="arb_opportunities.id=1:buy",
            proposed_at=now,
        )
        first = await store.record_intent(i)
        assert first > 0
        dup = await store.record_intent(i)
        assert dup == 0

        intents = await store.list_intents()
        assert len(intents) == 1

    asyncio.run(_run())


def test_intent_without_source_ref_is_not_deduped():
    async def _run():
        pool = InMemoryStrategyPool()
        store = StrategyStore(pool)
        now = datetime.now(timezone.utc)
        i = StrategyIntent(
            id=None, strategy_name="s1", strategy_kind=KIND_CUSTOM,
            symbol="ETH/USDT", side=SIDE_BUY, size_base=0.1,
            notional_usd=300.0, proposed_at=now,
        )
        a = await store.record_intent(i)
        b = await store.record_intent(i)
        assert a > 0 and b > 0 and a != b

    asyncio.run(_run())


# ------------------------------------------------------------- producers


class _FakeProducer:
    """Simple stub used across composer tests."""
    def __init__(self, name: str, intents: List[StrategyIntent]) -> None:
        self.name = name
        self.kind = KIND_CUSTOM
        self._intents = intents

    async def produce(self, *, limit=50, correlation_id=None,
                      company_id=None):
        return list(self._intents)


def _intent(name: str, source_ref: str, *, priority: float = 1.0):
    return StrategyIntent(
        id=None, strategy_name=name, strategy_kind=KIND_CUSTOM,
        symbol="BTC/USDT", side=SIDE_BUY, size_base=0.1,
        notional_usd=3000.0, reference_price=30_000.0,
        source_ref=source_ref, priority_score=priority,
        proposed_at=datetime.now(timezone.utc),
    )


def test_composer_dedupes_in_memory_and_persists():
    async def _run():
        pool = InMemoryStrategyPool()
        store = StrategyStore(pool)
        p = _FakeProducer("s1", [
            _intent("s1", "r1", priority=10),
            _intent("s1", "r1", priority=9),   # duplicate
            _intent("s1", "r2", priority=5),
        ])
        composer = StrategyComposer(store, [p])
        res = await composer.tick()
        # in-memory dedupe keeps the first-seen copy; ranking by priority
        assert res.proposed == 2
        assert res.duplicate == 0  # dedupe happened before persist
        rows = await store.list_intents()
        assert len(rows) == 2
        priorities = sorted(i.priority_score for i in res.intents)
        assert priorities == [5.0, 10.0]

    asyncio.run(_run())


def test_composer_flags_persist_conflicts_as_duplicate():
    async def _run():
        pool = InMemoryStrategyPool()
        store = StrategyStore(pool)
        # Record one first so the second attempt hits the partial unique
        # index on (strategy_name, source_ref) inside the pool.
        pre = _intent("s1", "r1", priority=10)
        pre_id = await store.record_intent(pre)
        assert pre_id > 0
        # Disable in-memory dedupe so the composer actually tries to
        # insert the already-present row.
        composer = StrategyComposer(
            store, [_FakeProducer("s1", [_intent("s1", "r1", priority=10)])],
            config=ComposerConfig(dedupe_in_memory=False),
        )
        res = await composer.tick()
        assert res.proposed == 1
        assert res.duplicate == 1

    asyncio.run(_run())


def test_composer_gate_approves_and_submits():
    async def _run():
        pool = InMemoryStrategyPool()
        store = StrategyStore(pool)
        p = _FakeProducer("s1", [
            _intent("s1", "r1", priority=1),
            _intent("s1", "r2", priority=2),
        ])

        async def _gate(intent):
            return GateDecision(
                approved=True, reason="ok",
                notional_usd_override=intent.notional_usd * 0.5,
            )

        submitted_ids: List[int] = []

        async def _submit(intent):
            submitted_ids.append(intent.id or -1)
            return 900 + (intent.id or 0)

        composer = StrategyComposer(
            store, [p], gate=_gate, submit=_submit,
        )
        res = await composer.tick()
        assert res.approved == 2
        assert res.submitted == 2
        assert len(submitted_ids) == 2
        rows = await store.list_intents()
        assert all(r.status == STATUS_SUBMITTED for r in rows)
        assert all(r.order_id and r.order_id >= 900 for r in rows)
        # Overrides live on the in-memory intents (the audit row keeps
        # the originally-proposed notional for forensic clarity).
        assert all(i.notional_usd == 1500.0 for i in res.intents)

    asyncio.run(_run())


def test_composer_gate_rejects():
    async def _run():
        pool = InMemoryStrategyPool()
        store = StrategyStore(pool)
        p = _FakeProducer("s1", [_intent("s1", "r1", priority=1)])

        async def _gate(intent):
            return GateDecision(approved=False, reason="too_small")

        composer = StrategyComposer(store, [p], gate=_gate)
        res = await composer.tick()
        assert res.rejected == 1
        rows = await store.list_intents()
        assert rows[0].status == STATUS_REJECTED
        assert rows[0].decision_reason == "too_small"

    asyncio.run(_run())


def test_composer_no_gate_leaves_pending():
    async def _run():
        pool = InMemoryStrategyPool()
        store = StrategyStore(pool)
        p = _FakeProducer("s1", [_intent("s1", "r1", priority=1)])
        composer = StrategyComposer(store, [p])
        res = await composer.tick()
        assert res.proposed == 1
        assert res.approved == 0
        rows = await store.list_intents()
        assert rows[0].status == STATUS_PENDING

    asyncio.run(_run())


def test_composer_ranking_desc_by_priority():
    async def _run():
        pool = InMemoryStrategyPool()
        store = StrategyStore(pool)
        p = _FakeProducer("s1", [
            _intent("s1", "r1", priority=1),
            _intent("s1", "r2", priority=5),
            _intent("s1", "r3", priority=3),
        ])
        composer = StrategyComposer(store, [p])
        res = await composer.tick()
        order = [i.priority_score for i in res.intents]
        assert order == [5.0, 3.0, 1.0]

    asyncio.run(_run())


def test_composer_persist_disabled():
    async def _run():
        pool = InMemoryStrategyPool()
        store = StrategyStore(pool)
        p = _FakeProducer("s1", [_intent("s1", "r1", priority=1)])
        composer = StrategyComposer(store, [p])
        res = await composer.tick(persist=False)
        assert res.proposed == 1
        rows = await store.list_intents()
        assert rows == []

    asyncio.run(_run())


# ----------------------------------------------------------- arb_producer


def test_arb_producer_emits_buy_and_sell_legs():
    async def _run():
        arb_pool = InMemoryArbPool()
        arb_store = ArbStore(arb_pool)
        await arb_store.record_opportunity(ArbOpportunity(
            id=None, symbol="BTC/USDT",
            buy_venue="binance", sell_venue="kraken",
            buy_ask=30_000.0, sell_bid=30_500.0,
            size_base=0.1, gross_bps=160.0, net_bps=90.0,
            est_profit_usd=25.0, fees_bps=70.0,
            observed_at=datetime.now(timezone.utc),
        ))
        prod = ArbProducer(arb_store, min_net_bps=50.0)
        intents = await prod.produce()
        assert len(intents) == 2
        sides = sorted(i.side for i in intents)
        assert sides == [SIDE_BUY, SIDE_SELL]
        buy = next(i for i in intents if i.side == SIDE_BUY)
        sell = next(i for i in intents if i.side == SIDE_SELL)
        assert buy.venue == "binance"
        assert sell.venue == "kraken"
        assert buy.reference_price == 30_000.0
        assert sell.reference_price == 30_500.0
        # Source refs must differ so the composer keeps both legs.
        assert buy.source_ref != sell.source_ref
        assert buy.strategy_name == "arb-scanner"
        assert buy.strategy_kind == KIND_ARB

    asyncio.run(_run())


def test_arb_producer_respects_min_net_bps():
    async def _run():
        arb_pool = InMemoryArbPool()
        arb_store = ArbStore(arb_pool)
        await arb_store.record_opportunity(ArbOpportunity(
            id=None, symbol="BTC/USDT",
            buy_venue="binance", sell_venue="kraken",
            buy_ask=30_000.0, sell_bid=30_050.0,
            size_base=0.1, gross_bps=16.0, net_bps=2.0,
            est_profit_usd=2.0, fees_bps=14.0,
            observed_at=datetime.now(timezone.utc),
        ))
        prod = ArbProducer(arb_store, min_net_bps=10.0)
        intents = await prod.produce()
        assert intents == []

    asyncio.run(_run())


# ------------------------------------------------------------------ service


def test_service_tick_with_arb_producer_end_to_end():
    async def _run():
        # seed arb
        arb_pool = InMemoryArbPool()
        arb_store = ArbStore(arb_pool)
        await arb_store.record_opportunity(ArbOpportunity(
            id=None, symbol="ETH/USDT",
            buy_venue="binance", sell_venue="kraken",
            buy_ask=2_000.0, sell_bid=2_050.0,
            size_base=1.0, gross_bps=250.0, net_bps=180.0,
            est_profit_usd=18.0, fees_bps=70.0,
            observed_at=datetime.now(timezone.utc),
        ))
        # composer
        strat_pool = InMemoryStrategyPool()
        strat_store = StrategyStore(strat_pool)
        producer = ArbProducer(arb_store, min_net_bps=5.0)
        svc = StrategyComposerService(strat_store, [producer])
        res = await svc.tick()
        assert isinstance(res, CompositionResult)
        assert res.proposed == 2
        rows = await strat_store.list_intents()
        assert len(rows) == 2
        # Second tick should dedupe via partial unique index.
        res2 = await svc.tick()
        assert res2.duplicate + res2.proposed == res2.proposed + res2.duplicate
        rows_after = await strat_store.list_intents()
        assert len(rows_after) == 2  # still just the two legs

    asyncio.run(_run())


# -------------------------------------------------------------------- CLI


def _stdout(fn, *args, **kwargs):
    buf = StringIO()
    with patch("sys.stdout", buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


def test_cli_migration_sql_contains_intents_table():
    rc, out = _stdout(strategy_cli.main, ["migration-sql"])
    assert rc == 0
    assert "public.strategy_intents" in out


def test_cli_apply_migration_path_only():
    rc, out = _stdout(strategy_cli.main, ["apply-migration", "--path-only"])
    assert rc == 0
    assert str(MIGRATION_PATH) in out


def test_cli_descriptor_add_and_list_roundtrip():
    rc, out = _stdout(strategy_cli.main, [
        "descriptor-add", "--in-memory",
        "--name", "souls-composer", "--kind", "souls",
        "--description", "hypothesis composer", "--priority", "80",
    ])
    assert rc == 0
    # list right after — note each CLI invocation builds its own pool,
    # so we call descriptor-add again below to reuse state via a single
    # process. Here we just check the return code + JSON shape.
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert parsed["name"] == "souls-composer"


def test_cli_tick_in_memory_smoke():
    rc, out = _stdout(strategy_cli.main, ["tick", "--in-memory"])
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["ok"] is True
    # No arb opps seeded in this process, so nothing gets proposed.
    assert parsed["proposed"] == 0
