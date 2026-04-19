"""Phase 31 — Apex / Quant / Ledger souls tests (Phase 32 adds Scout / Curiosity / Optimiser / RegimeWatcher)."""
from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from typing import Any, List

from shared.cli import souls_cli
from shared.services.registry import SERVICE_REGISTRY, register_builtin_services
from shared.souls import (
    ApexSoul,
    CuriositySoul,
    InMemorySoulsPool,
    LedgerSoul,
    MIGRATION_PATH,
    MIGRATION_PATH_PHASE32,
    MODE_DETERMINISTIC,
    OptimiserCandidate,
    OptimiserSoul,
    OptimiserStore,
    QuantSoul,
    RegimeTransition,
    RegimeTransitionStore,
    RegimeWatcherSoul,
    ROLE_DECISION,
    ROLE_RESEARCH,
    ScoutCandidate,
    ScoutSoul,
    ScoutStore,
    SOUL_APEX,
    SOUL_CURIOSITY,
    SOUL_LEDGER,
    SOUL_NAMES,
    SOUL_OPTIMISER,
    SOUL_QUANT,
    SOUL_REGIME_WATCHER,
    SOUL_SCOUT,
    SoulContext,
    SoulPersona,
    SoulPrompt,
    SoulsService,
    SoulsStore,
    VERDICT_ALERT,
    VERDICT_APPROVE,
    VERDICT_DEFER,
    VERDICT_EXPLORE,
    VERDICT_JOURNAL,
    VERDICT_OBSERVE,
    VERDICT_PROPOSE,
    VERDICT_REJECT,
    VERDICTS,
    read_migration_sql,
    read_migration_sql_phase32,
)


def _run(coro):
    return asyncio.run(coro)


def _ctx(**fields) -> SoulContext:
    return SoulContext(
        correlation_id=fields.pop("correlation_id", "corr-1"),
        company_id=fields.pop("company_id", "tickles"),
        fields=fields,
    )


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_exists_with_expected_objects() -> None:
    assert MIGRATION_PATH.exists()
    sql = read_migration_sql()
    for obj in (
        "public.agent_personas",
        "public.agent_prompts",
        "public.agent_decisions",
        "public.agent_decisions_latest",
        "UNIQUE (persona_id, version)",
    ):
        assert obj in sql


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def test_soul_names_are_canonical() -> None:
    assert SOUL_NAMES == {
        SOUL_APEX, SOUL_QUANT, SOUL_LEDGER,
        SOUL_SCOUT, SOUL_CURIOSITY, SOUL_OPTIMISER, SOUL_REGIME_WATCHER,
    }
    for v in (VERDICT_APPROVE, VERDICT_REJECT, VERDICT_PROPOSE,
              VERDICT_JOURNAL, VERDICT_OBSERVE, VERDICT_DEFER,
              VERDICT_EXPLORE, VERDICT_ALERT):
        assert v in VERDICTS


def test_soul_persona_and_prompt_to_dict() -> None:
    p = SoulPersona(id=1, name=SOUL_APEX, role=ROLE_DECISION)
    d = p.to_dict()
    assert d["name"] == SOUL_APEX
    assert d["enabled"] is True

    pr = SoulPrompt(id=None, persona_id=1, version=1,
                    template="hello {name}", variables=["name"])
    pd = pr.to_dict()
    assert pd["variables"] == ["name"]


# ---------------------------------------------------------------------------
# Apex soul
# ---------------------------------------------------------------------------


def test_apex_rejects_when_guardrails_block() -> None:
    apex = ApexSoul()
    d = apex.decide(_ctx(guardrails_blockers=[{"rule": "regime_crash"}]))
    assert d.verdict == VERDICT_REJECT
    assert d.confidence >= 0.99
    assert "guardrails" in (d.rationale or "")


def test_apex_defers_on_high_importance_event() -> None:
    apex = ApexSoul()
    d = apex.decide(_ctx(
        active_events=[{"name": "US CPI", "importance": 3}],
        regime="bull",
    ))
    assert d.verdict == VERDICT_DEFER


def test_apex_rejects_on_crash_regime() -> None:
    apex = ApexSoul()
    d = apex.decide(_ctx(regime="crash"))
    assert d.verdict == VERDICT_REJECT


def test_apex_rejects_when_treasury_rejects() -> None:
    apex = ApexSoul()
    d = apex.decide(_ctx(
        regime="bull",
        treasury_decision={"approved": False, "reason": "capability violated"},
    ))
    assert d.verdict == VERDICT_REJECT
    assert "capability" in (d.rationale or "")


def test_apex_approves_when_bull_plus_treasury_plus_proposal() -> None:
    apex = ApexSoul()
    d = apex.decide(_ctx(
        regime="bull",
        treasury_decision={"approved": True},
        proposal_score=0.4,
    ))
    assert d.verdict == VERDICT_APPROVE
    assert d.confidence > 0


def test_apex_defers_when_signals_are_weak() -> None:
    apex = ApexSoul()
    d = apex.decide(_ctx(regime="sideways"))
    assert d.verdict == VERDICT_DEFER


def test_apex_is_deterministic() -> None:
    apex = ApexSoul()
    f = dict(regime="bull", treasury_decision={"approved": True},
             proposal_score=0.4)
    a = apex.decide(_ctx(**f))
    b = apex.decide(_ctx(**f))
    assert a.verdict == b.verdict
    assert a.confidence == b.confidence


# ---------------------------------------------------------------------------
# Quant soul
# ---------------------------------------------------------------------------


def test_quant_proposes_long_in_bull_with_trend() -> None:
    q = QuantSoul()
    d = q.decide(_ctx(regime="bull", trend_score=0.5, volatility=0.01))
    assert d.verdict == VERDICT_PROPOSE
    assert d.outputs["direction"] == "long"
    assert d.outputs["size_bucket"] in ("small", "medium", "large")


def test_quant_observes_without_edge() -> None:
    q = QuantSoul()
    d = q.decide(_ctx(regime="sideways", trend_score=0.01, volatility=0.01))
    assert d.verdict == VERDICT_OBSERVE


def test_quant_steps_back_in_crash() -> None:
    q = QuantSoul()
    d = q.decide(_ctx(regime="crash", trend_score=-0.5))
    assert d.verdict == VERDICT_OBSERVE


def test_quant_funding_tilts_conviction() -> None:
    q = QuantSoul()
    d_low = q.decide(_ctx(regime="bull", trend_score=0.3, volatility=0.01,
                          funding_rate=0.0))
    d_fav = q.decide(_ctx(regime="bull", trend_score=0.3, volatility=0.01,
                          funding_rate=-0.001))
    assert d_fav.confidence >= d_low.confidence


# ---------------------------------------------------------------------------
# Ledger soul
# ---------------------------------------------------------------------------


def test_ledger_journals_fills_and_positions() -> None:
    ledger = LedgerSoul()
    ctx = _ctx(
        fills=[
            {"symbol": "BTC/USDT", "notional_usd": 1000, "fee_usd": 1.0,
             "realized_pnl_usd": 50},
            {"symbol": "BTC/USDT", "notional_usd": -500, "fee_usd": 0.5,
             "realized_pnl_usd": -10},
            {"symbol": "ETH/USDT", "notional_usd": 200, "fee_usd": 0.2,
             "realized_pnl_usd": 0},
        ],
        positions=[{"symbol": "BTC/USDT", "notional_usd": 500}],
    )
    d = ledger.decide(ctx)
    assert d.verdict == VERDICT_JOURNAL
    o = d.outputs
    assert o["n_fills"] == 3
    assert o["gross_notional_usd"] == 1700.0
    assert o["fees_usd"] == 1.7
    assert o["open_notional_usd"] == 500.0
    assert "BTC/USDT" in o["by_symbol"]
    assert o["by_symbol"]["BTC/USDT"]["fills"] == 2


def test_ledger_empty_inputs_still_journal() -> None:
    d = LedgerSoul().decide(_ctx())
    assert d.verdict == VERDICT_JOURNAL
    assert d.outputs["n_fills"] == 0


# ---------------------------------------------------------------------------
# Store + in-memory pool
# ---------------------------------------------------------------------------


def test_store_upsert_persona_round_trip() -> None:
    pool = InMemorySoulsPool()
    store = SoulsStore(pool)

    async def go() -> None:
        p = SoulPersona(id=None, name=SOUL_APEX, role=ROLE_DECISION,
                        description="v1", default_llm=None, enabled=True)
        pid = await store.upsert_persona(p)
        assert pid > 0
        again = await store.upsert_persona(
            SoulPersona(id=None, name=SOUL_APEX, role=ROLE_DECISION,
                        description="v2", default_llm="gpt-5", enabled=False),
        )
        assert again == pid
        fetched = await store.get_persona(SOUL_APEX)
        assert fetched is not None
        assert fetched.description == "v2"
        assert fetched.enabled is False

    _run(go())


def test_store_prompt_versioning_dedupes() -> None:
    pool = InMemorySoulsPool()
    store = SoulsStore(pool)

    async def go() -> None:
        pid = await store.upsert_persona(
            SoulPersona(id=None, name=SOUL_QUANT, role=ROLE_RESEARCH),
        )
        a = await store.add_prompt(
            SoulPrompt(id=None, persona_id=pid, version=1, template="t1"),
        )
        b = await store.add_prompt(
            SoulPrompt(id=None, persona_id=pid, version=1, template="t1-dup"),
        )
        assert a > 0
        assert b == 0  # ON CONFLICT DO NOTHING
        c = await store.add_prompt(
            SoulPrompt(id=None, persona_id=pid, version=2, template="t2"),
        )
        assert c > 0
        rows = await store.list_prompts(pid)
        assert [r.version for r in rows] == [2, 1]

    _run(go())


def test_store_record_decision_and_latest_view() -> None:
    pool = InMemorySoulsPool()
    store = SoulsStore(pool)

    async def go() -> None:
        pid = await store.upsert_persona(
            SoulPersona(id=None, name=SOUL_APEX, role=ROLE_DECISION),
        )
        ctx = SoulContext(correlation_id="c1", fields={"regime": "bull"})
        apex = ApexSoul()
        d1 = apex.decide(ctx)
        await store.record_decision(pid, d1, ctx.fields)
        d2 = apex.decide(SoulContext(
            correlation_id="c1",
            fields={"regime": "bull", "treasury_decision": {"approved": True},
                    "proposal_score": 0.5},
        ))
        await store.record_decision(pid, d2, {"trace": 2})

        all_rows = await store.list_decisions(persona_id=pid)
        assert len(all_rows) == 2

        latest = await store.list_latest_per_correlation(persona_id=pid)
        assert len(latest) == 1
        assert latest[0].verdict == d2.verdict
        assert latest[0].persona_name == SOUL_APEX

    _run(go())


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_service_seed_personas_is_idempotent() -> None:
    pool = InMemorySoulsPool()
    svc = SoulsService(SoulsStore(pool))

    async def go() -> None:
        ids1 = await svc.seed_personas()
        ids2 = await svc.seed_personas()
        assert ids1 == ids2
        assert set(ids1.keys()) == SOUL_NAMES

    _run(go())


def test_service_runs_and_persists_all_three_souls() -> None:
    pool = InMemorySoulsPool()
    svc = SoulsService(SoulsStore(pool))

    async def go() -> None:
        ctx_apex = SoulContext(
            correlation_id="flow-1",
            fields={"regime": "bull",
                    "treasury_decision": {"approved": True},
                    "proposal_score": 0.4},
        )
        ctx_quant = SoulContext(
            correlation_id="flow-1",
            fields={"regime": "bull", "trend_score": 0.4, "volatility": 0.02},
        )
        ctx_ledger = SoulContext(
            correlation_id="flow-1",
            fields={"fills": [{"symbol": "BTC", "notional_usd": 100}]},
        )
        apex_d = await svc.run_apex(ctx_apex)
        quant_d = await svc.run_quant(ctx_quant)
        ledger_d = await svc.run_ledger(ctx_ledger)
        assert apex_d.verdict == VERDICT_APPROVE
        assert quant_d.verdict == VERDICT_PROPOSE
        assert ledger_d.verdict == VERDICT_JOURNAL

        rows = await svc.decisions(correlation_id="flow-1")
        assert len(rows) == 3
        assert {r.verdict for r in rows} == {
            VERDICT_APPROVE, VERDICT_PROPOSE, VERDICT_JOURNAL,
        }

    _run(go())


def test_service_no_persist_skips_recording() -> None:
    pool = InMemorySoulsPool()
    svc = SoulsService(SoulsStore(pool))

    async def go() -> None:
        ctx = SoulContext(correlation_id="c2", fields={"regime": "bull"})
        await svc.run_apex(ctx, persist=False)
        rows = await svc.decisions()
        assert rows == []

    _run(go())


def test_service_latest_decisions_exposes_persona_name() -> None:
    pool = InMemorySoulsPool()
    svc = SoulsService(SoulsStore(pool))

    async def go() -> None:
        ctx = SoulContext(correlation_id="c3", fields={"regime": "bull"})
        await svc.run_apex(ctx)
        rows = await svc.latest_decisions()
        assert len(rows) == 1
        assert rows[0].persona_name == SOUL_APEX
        assert rows[0].mode == MODE_DETERMINISTIC

    _run(go())


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_souls_service_is_registered() -> None:
    register_builtin_services()
    assert "souls" in SERVICE_REGISTRY
    desc = SERVICE_REGISTRY.get("souls")
    assert desc.tags.get("phase") in {"31", "31-32"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(argv: List[str]) -> Any:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = souls_cli.main(argv)
    out = buf.getvalue().strip()
    assert code == 0, f"cli exit={code}; stdout={out!r}"
    return json.loads(out)


def test_cli_apply_migration_path_only() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = souls_cli.main(["apply-migration", "--path-only"])
    assert code == 0
    assert buf.getvalue().strip() == str(MIGRATION_PATH)


def test_cli_seed_personas_in_memory() -> None:
    data = _run_cli(["seed-personas", "--in-memory"])
    assert data["ok"] is True
    assert set(data["persona_ids"].keys()) == SOUL_NAMES


def test_cli_run_apex_in_memory_approves() -> None:
    fields = json.dumps({
        "regime": "bull",
        "treasury_decision": {"approved": True},
        "proposal_score": 0.4,
    })
    data = _run_cli([
        "run-apex", "--in-memory",
        "--correlation-id", "cli-flow", "--fields", fields,
    ])
    assert data["ok"] is True
    assert data["decision"]["verdict"] == VERDICT_APPROVE
    assert data["decision"]["persona_name"] == SOUL_APEX


def test_cli_run_quant_no_persist() -> None:
    fields = json.dumps({"regime": "bull", "trend_score": 0.4, "volatility": 0.01})
    data = _run_cli([
        "run-quant", "--in-memory", "--no-persist",
        "--correlation-id", "cli-flow", "--fields", fields,
    ])
    assert data["decision"]["verdict"] == VERDICT_PROPOSE


def test_cli_run_ledger_summarises_fills() -> None:
    fields = json.dumps({
        "fills": [
            {"symbol": "BTC/USDT", "notional_usd": 100, "fee_usd": 0.1},
            {"symbol": "ETH/USDT", "notional_usd": 50, "fee_usd": 0.05},
        ],
    })
    data = _run_cli([
        "run-ledger", "--in-memory",
        "--correlation-id", "cli-ledger", "--fields", fields,
    ])
    assert data["decision"]["verdict"] == VERDICT_JOURNAL
    assert data["decision"]["outputs"]["n_fills"] == 2


def test_cli_personas_empty_in_memory() -> None:
    data = _run_cli(["personas", "--in-memory"])
    assert data["ok"] is True
    assert data["count"] == 0


def test_cli_decisions_empty_in_memory() -> None:
    data = _run_cli(["decisions", "--in-memory"])
    assert data["ok"] is True
    assert data["count"] == 0


def test_cli_latest_empty_in_memory() -> None:
    data = _run_cli(["latest", "--in-memory"])
    assert data["ok"] is True
    assert data["count"] == 0


# ===========================================================================
# Phase 32 — Scout / Curiosity / Optimiser / RegimeWatcher
# ===========================================================================


# ---------------------------------------------------------------------------
# Migration (phase 32)
# ---------------------------------------------------------------------------


def test_phase32_migration_exists_with_expected_objects() -> None:
    assert MIGRATION_PATH_PHASE32.exists()
    sql = read_migration_sql_phase32()
    for obj in (
        "public.scout_candidates",
        "public.optimiser_candidates",
        "public.regime_transitions",
    ):
        assert obj in sql


# ---------------------------------------------------------------------------
# Scout soul
# ---------------------------------------------------------------------------


def test_scout_proposes_above_threshold_and_orders_by_score() -> None:
    scout = ScoutSoul(min_score=0.2, max_proposals=5)
    ctx = _ctx(
        observations=[
            {"symbol": "AAA/USDT", "exchange": "binance",
             "volume_usd": 5e8, "volatility": 0.1,
             "social_mentions": 5000, "funding_rate": 0.0},
            {"symbol": "BBB/USDT", "exchange": "binance",
             "volume_usd": 1e6, "volatility": 0.01,
             "social_mentions": 10, "funding_rate": 0.0},
            {"symbol": "CCC/USDT", "exchange": "binance",
             "volume_usd": 8e8, "volatility": 0.15,
             "social_mentions": 8000, "funding_rate": 0.0},
        ],
        existing_symbols=["DDD/USDT"],
    )
    d = scout.decide(ctx)
    assert d.verdict == VERDICT_PROPOSE
    proposals = d.outputs["proposals"]
    assert [p["symbol"] for p in proposals] == ["CCC/USDT", "AAA/USDT"]
    assert proposals[0]["score"] >= proposals[1]["score"]


def test_scout_filters_existing_symbols() -> None:
    scout = ScoutSoul(min_score=0.05)
    d = scout.decide(_ctx(
        observations=[{"symbol": "BTC/USDT", "exchange": "binance",
                       "volume_usd": 1e9, "volatility": 0.1,
                       "social_mentions": 1000, "funding_rate": 0.0}],
        existing_symbols=["BTC/USDT"],
    ))
    assert d.verdict == VERDICT_OBSERVE
    assert d.outputs["proposals"] == []


def test_scout_observes_when_nothing_qualifies() -> None:
    d = ScoutSoul(min_score=0.9).decide(_ctx(observations=[
        {"symbol": "XYZ/USDT", "exchange": "x",
         "volume_usd": 1, "volatility": 0, "social_mentions": 0},
    ]))
    assert d.verdict == VERDICT_OBSERVE


def test_scout_is_deterministic() -> None:
    scout = ScoutSoul()
    obs = [
        {"symbol": "AAA/USDT", "exchange": "x",
         "volume_usd": 5e8, "volatility": 0.1,
         "social_mentions": 500, "funding_rate": 0.0},
    ]
    a = scout.decide(_ctx(observations=obs))
    b = scout.decide(_ctx(observations=obs))
    assert a.to_dict() == b.to_dict()


# ---------------------------------------------------------------------------
# Curiosity soul
# ---------------------------------------------------------------------------


def test_curiosity_explores_untried_first() -> None:
    c = CuriositySoul()
    d = c.decide(_ctx(
        history=[{"key": "a", "success": True}, {"key": "a", "success": False}],
        candidates=[
            {"key": "a", "prior": 0.5, "params": {"x": 1}},
            {"key": "b", "prior": 0.5, "params": {"x": 2}},
        ],
    ))
    assert d.verdict == VERDICT_EXPLORE
    keys = [p["key"] for p in d.outputs["picks"]]
    assert keys[0] == "b"


def test_curiosity_observes_when_empty() -> None:
    c = CuriositySoul()
    d = c.decide(_ctx(candidates=[]))
    assert d.verdict == VERDICT_OBSERVE


def test_curiosity_respects_novelty_floor() -> None:
    c = CuriositySoul(novelty_floor=0.99)
    d = c.decide(_ctx(candidates=[{"key": "a", "prior": 0.01}],
                      history=[{"key": "a"}] * 50))
    assert d.verdict == VERDICT_OBSERVE


# ---------------------------------------------------------------------------
# Optimiser soul
# ---------------------------------------------------------------------------


def test_optimiser_proposes_first_untried_in_grid() -> None:
    opt = OptimiserSoul()
    d = opt.decide(_ctx(
        strategy="s1",
        space={"a": [1, 2], "b": [10, 20]},
        history=[],
    ))
    assert d.verdict == VERDICT_PROPOSE
    assert d.outputs["params"] == {"a": 1, "b": 10}


def test_optimiser_skips_tried_and_returns_neighbour_when_grid_exhausted() -> None:
    opt = OptimiserSoul()
    space = {"a": [1, 2, 3]}
    history = [
        {"params": {"a": 1}, "score": 0.1},
        {"params": {"a": 2}, "score": 0.9},
        {"params": {"a": 3}, "score": 0.5},
    ]
    d = opt.decide(_ctx(strategy="s1", space=space, history=history))
    assert d.verdict == VERDICT_OBSERVE


def test_optimiser_advances_through_grid() -> None:
    opt = OptimiserSoul()
    space = {"a": [1, 2], "b": [10, 20]}
    history = [{"params": {"a": 1, "b": 10}, "score": 0.5}]
    d = opt.decide(_ctx(strategy="s1", space=space, history=history))
    assert d.verdict == VERDICT_PROPOSE
    assert d.outputs["params"] != {"a": 1, "b": 10}


def test_optimiser_honours_budget() -> None:
    opt = OptimiserSoul(max_total_trials=1)
    d = opt.decide(_ctx(
        strategy="s1", space={"a": [1, 2, 3]},
        history=[{"params": {"a": 1}, "score": 0.5}],
    ))
    assert d.verdict == VERDICT_OBSERVE
    assert "budget" in (d.rationale or "")


# ---------------------------------------------------------------------------
# RegimeWatcher soul
# ---------------------------------------------------------------------------


def test_regime_watcher_alerts_on_transition() -> None:
    rw = RegimeWatcherSoul()
    t0 = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    d = rw.decide(_ctx(observations=[
        {"ts": t0.isoformat(), "regime": "bull", "confidence": 0.7},
        {"ts": (t0 + timedelta(hours=1)).isoformat(),
         "regime": "crash", "confidence": 0.9},
    ]))
    assert d.verdict == VERDICT_ALERT
    latest = d.outputs["latest"]
    assert latest["from_regime"] == "bull"
    assert latest["to_regime"] == "crash"
    assert latest["crash"] is True
    assert d.outputs["severity"] == "high"


def test_regime_watcher_observes_when_no_transition() -> None:
    rw = RegimeWatcherSoul()
    t0 = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    d = rw.decide(_ctx(observations=[
        {"ts": t0.isoformat(), "regime": "bull"},
        {"ts": (t0 + timedelta(hours=1)).isoformat(), "regime": "bull"},
    ]))
    assert d.verdict == VERDICT_OBSERVE


def test_regime_watcher_observes_when_insufficient_data() -> None:
    d = RegimeWatcherSoul().decide(_ctx(observations=[]))
    assert d.verdict == VERDICT_OBSERVE


# ---------------------------------------------------------------------------
# Service — Phase 32 wiring
# ---------------------------------------------------------------------------


def test_service_seed_includes_phase32_personas() -> None:
    pool = InMemorySoulsPool()
    svc = SoulsService(SoulsStore(pool))

    async def go() -> None:
        ids = await svc.seed_personas()
        assert set(ids.keys()) == SOUL_NAMES
        assert SOUL_SCOUT in ids and SOUL_CURIOSITY in ids
        assert SOUL_OPTIMISER in ids and SOUL_REGIME_WATCHER in ids

    _run(go())


def test_service_runs_all_phase32_souls() -> None:
    pool = InMemorySoulsPool()
    svc = SoulsService(SoulsStore(pool))

    async def go() -> None:
        ctx_scout = SoulContext(
            correlation_id="p32",
            fields={"observations": [
                {"symbol": "AAA/USDT", "exchange": "x",
                 "volume_usd": 1e9, "volatility": 0.1,
                 "social_mentions": 5000, "funding_rate": 0.0},
            ]},
        )
        ctx_cur = SoulContext(
            correlation_id="p32",
            fields={"candidates": [{"key": "a", "prior": 0.6}]},
        )
        ctx_opt = SoulContext(
            correlation_id="p32",
            fields={"strategy": "s1", "space": {"a": [1, 2]}, "history": []},
        )
        t0 = datetime(2026, 4, 10, tzinfo=timezone.utc)
        ctx_rw = SoulContext(
            correlation_id="p32",
            fields={"observations": [
                {"ts": t0.isoformat(), "regime": "bull"},
                {"ts": (t0 + timedelta(hours=1)).isoformat(), "regime": "bear"},
            ]},
        )
        ds = await svc.run_scout(ctx_scout)
        dc = await svc.run_curiosity(ctx_cur)
        do = await svc.run_optimiser(ctx_opt)
        dr = await svc.run_regime_watcher(ctx_rw)
        assert ds.verdict == VERDICT_PROPOSE
        assert dc.verdict == VERDICT_EXPLORE
        assert do.verdict == VERDICT_PROPOSE
        assert dr.verdict == VERDICT_ALERT

        rows = await svc.decisions(correlation_id="p32")
        assert len(rows) == 4
        assert {r.verdict for r in rows} == {
            VERDICT_PROPOSE, VERDICT_EXPLORE, VERDICT_ALERT,
        }

    _run(go())


# ---------------------------------------------------------------------------
# Stores — Phase 32
# ---------------------------------------------------------------------------


def test_scout_store_upsert_is_idempotent() -> None:
    pool = InMemorySoulsPool()
    store = ScoutStore(pool)

    async def go() -> None:
        a = await store.upsert(ScoutCandidate(
            id=None, exchange="binance", symbol="AAA/USDT",
            score=0.5, universe="crypto", company_id="tickles",
            reason="volume", correlation_id="c1",
        ))
        b = await store.upsert(ScoutCandidate(
            id=None, exchange="binance", symbol="AAA/USDT",
            score=0.75, universe="crypto", company_id="tickles",
            reason="volume+mentions", correlation_id="c2",
        ))
        assert a == b
        rows = await store.list()
        assert len(rows) == 1
        assert rows[0].score == 0.75
        assert rows[0].reason == "volume+mentions"

    _run(go())


def test_optimiser_store_insert_and_filter() -> None:
    pool = InMemorySoulsPool()
    store = OptimiserStore(pool)

    async def go() -> None:
        for i, st in enumerate(["pending", "running", "done"]):
            await store.insert(OptimiserCandidate(
                id=None, strategy="s1", params={"a": i}, status=st,
            ))
        rows = await store.list(status="pending")
        assert len(rows) == 1
        assert rows[0].params == {"a": 0}
        all_rows = await store.list(strategy="s1")
        assert len(all_rows) == 3

    _run(go())


def test_regime_transition_store_insert_and_list() -> None:
    pool = InMemorySoulsPool()
    store = RegimeTransitionStore(pool)
    t0 = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)

    async def go() -> None:
        await store.insert(RegimeTransition(
            id=None, exchange="binance", symbol="BTC/USDT",
            timeframe="1h", from_regime="bull", to_regime="bear",
            transitioned_at=t0, confidence=0.8,
        ))
        await store.insert(RegimeTransition(
            id=None, exchange="binance", symbol="BTC/USDT",
            timeframe="1h", from_regime="bear", to_regime="crash",
            transitioned_at=t0 + timedelta(hours=1), confidence=0.9,
        ))
        rows = await store.list(symbol="BTC/USDT")
        assert len(rows) == 2
        assert rows[0].to_regime == "crash"

    _run(go())


# ---------------------------------------------------------------------------
# Phase 32 CLI
# ---------------------------------------------------------------------------


def test_cli_phase32_migration_path() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = souls_cli.main(["apply-migration", "--phase", "32",
                               "--path-only"])
    assert code == 0
    assert buf.getvalue().strip() == str(MIGRATION_PATH_PHASE32)


def test_cli_phase32_migration_sql() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = souls_cli.main(["migration-sql", "--phase", "32"])
    assert code == 0
    assert "scout_candidates" in buf.getvalue()


def test_cli_run_scout_in_memory() -> None:
    fields = json.dumps({"observations": [
        {"symbol": "AAA/USDT", "exchange": "x",
         "volume_usd": 1e9, "volatility": 0.1,
         "social_mentions": 5000, "funding_rate": 0.0},
    ]})
    data = _run_cli([
        "run-scout", "--in-memory",
        "--correlation-id", "cli-scout", "--fields", fields,
    ])
    assert data["decision"]["verdict"] == VERDICT_PROPOSE
    assert data["decision"]["persona_name"] == SOUL_SCOUT


def test_cli_run_curiosity_in_memory() -> None:
    fields = json.dumps({"candidates": [{"key": "a", "prior": 0.5}]})
    data = _run_cli([
        "run-curiosity", "--in-memory",
        "--correlation-id", "cli-cur", "--fields", fields,
    ])
    assert data["decision"]["verdict"] == VERDICT_EXPLORE


def test_cli_run_optimiser_in_memory() -> None:
    fields = json.dumps({"strategy": "s1", "space": {"a": [1, 2]}})
    data = _run_cli([
        "run-optimiser", "--in-memory",
        "--correlation-id", "cli-opt", "--fields", fields,
    ])
    assert data["decision"]["verdict"] == VERDICT_PROPOSE


def test_cli_run_regime_watcher_in_memory() -> None:
    t0 = datetime(2026, 4, 10, tzinfo=timezone.utc)
    fields = json.dumps({"observations": [
        {"ts": t0.isoformat(), "regime": "bull"},
        {"ts": (t0 + timedelta(hours=1)).isoformat(), "regime": "bear"},
    ]})
    data = _run_cli([
        "run-regime-watcher", "--in-memory",
        "--correlation-id", "cli-rw", "--fields", fields,
    ])
    assert data["decision"]["verdict"] == VERDICT_ALERT
