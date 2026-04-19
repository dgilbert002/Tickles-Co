"""Phase 31 — Apex / Quant / Ledger souls tests."""
from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout
from typing import Any, List

from shared.cli import souls_cli
from shared.services.registry import SERVICE_REGISTRY, register_builtin_services
from shared.souls import (
    ApexSoul,
    InMemorySoulsPool,
    LedgerSoul,
    MIGRATION_PATH,
    MODE_DETERMINISTIC,
    QuantSoul,
    ROLE_DECISION,
    ROLE_RESEARCH,
    SOUL_APEX,
    SOUL_LEDGER,
    SOUL_NAMES,
    SOUL_QUANT,
    SoulContext,
    SoulPersona,
    SoulPrompt,
    SoulsService,
    SoulsStore,
    VERDICT_APPROVE,
    VERDICT_DEFER,
    VERDICT_JOURNAL,
    VERDICT_OBSERVE,
    VERDICT_PROPOSE,
    VERDICT_REJECT,
    VERDICTS,
    read_migration_sql,
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
    assert SOUL_NAMES == {SOUL_APEX, SOUL_QUANT, SOUL_LEDGER}
    for v in (VERDICT_APPROVE, VERDICT_REJECT, VERDICT_PROPOSE,
              VERDICT_JOURNAL, VERDICT_OBSERVE, VERDICT_DEFER):
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
    assert desc.tags.get("phase") == "31"


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
