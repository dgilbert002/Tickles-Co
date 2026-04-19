"""Phase 28 — Crash Protection tests.

Covers:
  * migration shape
  * evaluator over each rule type
  * store / memory-pool CRUD
  * service tick + persistence + blocked-intent lookup
  * CLI smoke
  * service-registry entry for 'crash-protection'
"""
from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from typing import List

from shared.guardrails import (
    ACTION_ALERT,
    ACTION_HALT_NEW_ORDERS,
    GuardrailsService,
    GuardrailsStore,
    InMemoryGuardrailsPool,
    MIGRATION_PATH,
    PositionSummary,
    ProtectionRule,
    ProtectionSnapshot,
    RULE_DAILY_LOSS,
    RULE_EQUITY_DRAWDOWN,
    RULE_POSITION_NOTIONAL,
    RULE_REGIME_CRASH,
    RULE_STALE_DATA,
    RegimeLabel,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    STATUS_RESOLVED,
    STATUS_TRIGGERED,
    decisions_block_intent,
    evaluate,
    read_migration_sql,
)
from shared.cli import guardrails_cli
from shared.services.registry import SERVICE_REGISTRY, register_builtin_services


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_file_exists_and_has_expected_objects() -> None:
    assert MIGRATION_PATH.exists()
    sql = read_migration_sql()
    for obj in (
        "public.crash_protection_rules",
        "public.crash_protection_events",
        "public.crash_protection_active",
    ):
        assert obj in sql


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


def _rule(**over) -> ProtectionRule:
    kwargs = dict(
        id=None, company_id="tickles", universe=None, exchange=None, symbol=None,
        rule_type=RULE_REGIME_CRASH, action=ACTION_HALT_NEW_ORDERS, threshold=None,
        params={}, severity=SEVERITY_WARNING, enabled=True,
    )
    kwargs.update(over)
    return ProtectionRule(**kwargs)


def test_regime_crash_rule_triggers_on_crash_label() -> None:
    rule = _rule(rule_type=RULE_REGIME_CRASH)
    snap = ProtectionSnapshot(
        company_id="tickles",
        regimes=[RegimeLabel(
            universe="u", exchange="binance", symbol="BTC/USDT",
            timeframe="1h", classifier="composite",
            regime="crash", as_of=datetime.now(timezone.utc),
        )],
    )
    decisions = evaluate([rule], snap)
    assert len(decisions) == 1
    assert decisions[0].status == STATUS_TRIGGERED


def test_regime_crash_rule_resolves_on_bull_label() -> None:
    rule = _rule(rule_type=RULE_REGIME_CRASH)
    snap = ProtectionSnapshot(
        company_id="tickles",
        regimes=[RegimeLabel(
            universe="u", exchange="binance", symbol="BTC/USDT",
            timeframe="1h", classifier="composite",
            regime="bull", as_of=datetime.now(timezone.utc),
        )],
    )
    decisions = evaluate([rule], snap)
    assert len(decisions) == 1
    assert decisions[0].status == STATUS_RESOLVED


def test_equity_drawdown_triggers_above_threshold() -> None:
    rule = _rule(rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.10, severity=SEVERITY_CRITICAL)
    snap = ProtectionSnapshot(
        company_id="tickles",
        equity_usd=820.0,
        equity_peak_usd=1000.0,
    )
    decisions = evaluate([rule], snap)
    assert len(decisions) == 1
    assert decisions[0].status == STATUS_TRIGGERED
    assert abs((decisions[0].metric or 0) - 0.18) < 1e-9


def test_equity_drawdown_resolved_under_threshold() -> None:
    rule = _rule(rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.10)
    snap = ProtectionSnapshot(
        company_id="tickles", equity_usd=950.0, equity_peak_usd=1000.0,
    )
    decisions = evaluate([rule], snap)
    assert decisions[0].status == STATUS_RESOLVED


def test_equity_drawdown_skipped_when_missing_values() -> None:
    rule = _rule(rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.1)
    snap = ProtectionSnapshot(company_id="tickles")
    assert evaluate([rule], snap) == []


def test_daily_loss_triggers_on_big_drawdown_from_start() -> None:
    rule = _rule(rule_type=RULE_DAILY_LOSS, threshold=0.05)
    snap = ProtectionSnapshot(
        company_id="tickles",
        equity_usd=900.0,
        equity_daily_start_usd=1000.0,
    )
    decisions = evaluate([rule], snap)
    assert len(decisions) == 1
    assert decisions[0].status == STATUS_TRIGGERED


def test_daily_loss_resolved_when_gain() -> None:
    rule = _rule(rule_type=RULE_DAILY_LOSS, threshold=0.05)
    snap = ProtectionSnapshot(
        company_id="tickles", equity_usd=1100.0, equity_daily_start_usd=1000.0,
    )
    decisions = evaluate([rule], snap)
    assert decisions[0].status == STATUS_RESOLVED


def test_position_notional_triggers_per_position() -> None:
    rule = _rule(rule_type=RULE_POSITION_NOTIONAL, threshold=500.0)
    snap = ProtectionSnapshot(
        company_id="tickles",
        positions=[
            PositionSummary(company_id="tickles", exchange="binance", symbol="BTC/USDT",
                            direction="long", quantity=0.1, notional_usd=700.0),
            PositionSummary(company_id="tickles", exchange="binance", symbol="ETH/USDT",
                            direction="long", quantity=1.0, notional_usd=200.0),
        ],
    )
    decisions = evaluate([rule], snap)
    triggered = [d for d in decisions if d.status == STATUS_TRIGGERED]
    resolved = [d for d in decisions if d.status == STATUS_RESOLVED]
    assert len(triggered) == 1
    assert triggered[0].symbol == "BTC/USDT"
    assert len(resolved) == 1
    assert resolved[0].symbol == "ETH/USDT"


def test_position_notional_respects_symbol_scope() -> None:
    rule = _rule(
        rule_type=RULE_POSITION_NOTIONAL, threshold=500.0,
        exchange="binance", symbol="BTC/USDT",
    )
    snap = ProtectionSnapshot(
        company_id="tickles",
        positions=[
            PositionSummary(company_id="tickles", exchange="binance", symbol="BTC/USDT",
                            direction="long", quantity=0.1, notional_usd=700.0),
            PositionSummary(company_id="tickles", exchange="binance", symbol="ETH/USDT",
                            direction="long", quantity=100.0, notional_usd=9999.0),
        ],
    )
    decisions = evaluate([rule], snap)
    assert len(decisions) == 1
    assert decisions[0].symbol == "BTC/USDT"


def test_stale_data_triggers_after_threshold_minutes() -> None:
    now = datetime.now(timezone.utc)
    rule = _rule(rule_type=RULE_STALE_DATA, threshold=10.0)
    snap = ProtectionSnapshot(
        company_id="tickles",
        last_tick_at=now - timedelta(minutes=30),
        now=now,
    )
    decisions = evaluate([rule], snap)
    assert len(decisions) == 1
    assert decisions[0].status == STATUS_TRIGGERED


def test_stale_data_resolved_when_fresh() -> None:
    now = datetime.now(timezone.utc)
    rule = _rule(rule_type=RULE_STALE_DATA, threshold=10.0)
    snap = ProtectionSnapshot(
        company_id="tickles",
        last_tick_at=now - timedelta(minutes=2),
        now=now,
    )
    decisions = evaluate([rule], snap)
    assert decisions[0].status == STATUS_RESOLVED


def test_disabled_rule_is_ignored() -> None:
    rule = _rule(rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.1, enabled=False)
    snap = ProtectionSnapshot(
        company_id="tickles", equity_usd=700.0, equity_peak_usd=1000.0,
    )
    assert evaluate([rule], snap) == []


def test_scope_wildcards_match_any_company() -> None:
    # company_id=None on the rule means "any company"
    rule = _rule(company_id=None, rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.1)
    snap = ProtectionSnapshot(
        company_id="tickles", equity_usd=700.0, equity_peak_usd=1000.0,
    )
    assert len(evaluate([rule], snap)) == 1


def test_decisions_block_intent_filters_by_scope_and_action() -> None:
    halt = _rule(
        rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.1, action=ACTION_HALT_NEW_ORDERS,
    )
    alert = _rule(
        rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.1, action=ACTION_ALERT,
    )
    snap = ProtectionSnapshot(
        company_id="tickles", equity_usd=700.0, equity_peak_usd=1000.0,
    )
    decisions = evaluate([halt, alert], snap)
    assert len(decisions) == 2
    blockers = decisions_block_intent(
        decisions,
        company_id="tickles", universe=None,
        exchange="binance", symbol="BTC/USDT",
    )
    assert len(blockers) == 1
    assert blockers[0].rule.action == ACTION_HALT_NEW_ORDERS


# ---------------------------------------------------------------------------
# Store + InMemory pool
# ---------------------------------------------------------------------------


def test_store_insert_and_list_rule() -> None:
    pool = InMemoryGuardrailsPool()
    store = GuardrailsStore(pool)

    async def go() -> None:
        rule = _rule(rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.1)
        rid = await store.insert_rule(rule)
        assert rid == 1
        rules = await store.list_rules(company_id="tickles")
        assert len(rules) == 1
        assert rules[0].threshold == 0.1

    _run(go())


def test_store_enable_disable_rule() -> None:
    pool = InMemoryGuardrailsPool()
    store = GuardrailsStore(pool)

    async def go() -> None:
        rid = await store.insert_rule(_rule(rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.1))
        updated = await store.set_enabled(rid, False)
        assert updated == 1
        rules = await store.list_rules(company_id="tickles", enabled_only=True)
        assert len(rules) == 0
        rules_all = await store.list_rules(company_id="tickles", enabled_only=False)
        assert rules_all[0].enabled is False

    _run(go())


def test_store_insert_event_and_active_view_dedupes_by_latest() -> None:
    pool = InMemoryGuardrailsPool()
    store = GuardrailsStore(pool)

    async def go() -> None:
        rule = _rule(rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.1)
        rid = await store.insert_rule(rule)
        rule.id = rid
        snap_t = ProtectionSnapshot(
            company_id="tickles", equity_usd=700.0, equity_peak_usd=1000.0,
        )
        snap_r = ProtectionSnapshot(
            company_id="tickles", equity_usd=999.0, equity_peak_usd=1000.0,
        )
        [trig] = evaluate([rule], snap_t)
        [resolved] = evaluate([rule], snap_r)
        await store.insert_event(trig)
        await store.insert_event(resolved)
        active = await store.list_active(company_id="tickles", triggered_only=False)
        assert len(active) == 1
        # Latest event wins
        assert active[0].status == STATUS_RESOLVED
        triggered_only = await store.list_active(company_id="tickles", triggered_only=True)
        assert len(triggered_only) == 0

    _run(go())


def test_store_list_events_limits_and_filters_by_status() -> None:
    pool = InMemoryGuardrailsPool()
    store = GuardrailsStore(pool)

    async def go() -> None:
        rule = _rule(rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.1)
        rid = await store.insert_rule(rule)
        rule.id = rid
        for peak, eq in ((1000, 700), (1000, 820), (1000, 900)):
            [dec] = evaluate([rule], ProtectionSnapshot(
                company_id="tickles", equity_usd=eq, equity_peak_usd=peak,
            ))
            await store.insert_event(dec)
        evs = await store.list_events(company_id="tickles", status=STATUS_TRIGGERED, limit=10)
        assert all(e.status == STATUS_TRIGGERED for e in evs)
        assert len(evs) >= 2

    _run(go())


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_service_tick_writes_one_event_per_decision() -> None:
    pool = InMemoryGuardrailsPool()
    store = GuardrailsStore(pool)
    service = GuardrailsService(store)

    async def go() -> None:
        rule_id = await store.insert_rule(
            _rule(rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.1),
        )
        assert rule_id > 0
        snap = ProtectionSnapshot(
            company_id="tickles", equity_usd=500.0, equity_peak_usd=1000.0,
        )
        decisions = await service.tick(snap)
        assert len(decisions) == 1
        events = await store.list_events(company_id="tickles")
        assert len(events) == 1

    _run(go())


def test_service_only_triggered_skips_resolved_persistence() -> None:
    pool = InMemoryGuardrailsPool()
    store = GuardrailsStore(pool)
    service = GuardrailsService(store)

    async def go() -> None:
        await store.insert_rule(_rule(rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.5))
        snap = ProtectionSnapshot(
            company_id="tickles", equity_usd=950.0, equity_peak_usd=1000.0,
        )
        await service.tick(snap, only_triggered=True, persist=True)
        events = await store.list_events(company_id="tickles")
        assert events == []

    _run(go())


def test_service_is_intent_blocked_honours_scope() -> None:
    pool = InMemoryGuardrailsPool()
    store = GuardrailsStore(pool)
    service = GuardrailsService(store)

    async def go() -> None:
        await store.insert_rule(_rule(
            rule_type=RULE_EQUITY_DRAWDOWN, threshold=0.1, action=ACTION_HALT_NEW_ORDERS,
        ))
        snap = ProtectionSnapshot(
            company_id="tickles", equity_usd=700.0, equity_peak_usd=1000.0,
        )
        await service.tick(snap, persist=True)
        blockers = await service.is_intent_blocked(
            company_id="tickles", universe=None,
            exchange="binance", symbol="BTC/USDT",
        )
        assert len(blockers) == 1

        wrong_company = await service.is_intent_blocked(
            company_id="other", universe=None,
            exchange="binance", symbol="BTC/USDT",
        )
        assert wrong_company == []

    _run(go())


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_crash_protection_service_is_registered() -> None:
    register_builtin_services()
    assert "crash-protection" in SERVICE_REGISTRY
    desc = SERVICE_REGISTRY.get("crash-protection")
    assert desc.tags.get("phase") == "28"


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def _run_cli(argv: List[str]) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = guardrails_cli.main(argv)
    out = buf.getvalue().strip()
    assert code == 0, f"cli exit={code}; stdout={out!r}"
    return json.loads(out)


def test_cli_apply_migration_path_only() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = guardrails_cli.main(["apply-migration", "--path-only"])
    assert code == 0
    assert buf.getvalue().strip() == str(MIGRATION_PATH)


def test_cli_rule_list_empty_in_memory() -> None:
    data = _run_cli(["rule-list", "--in-memory"])
    assert data["ok"] is True
    assert data["count"] == 0


def test_cli_evaluate_snapshot_inline_triggers() -> None:
    snap = {
        "company_id": "tickles",
        "equity_usd": 500,
        "equity_peak_usd": 1000,
    }
    # No persisted rules in the in-memory pool, so no decisions.
    data = _run_cli([
        "evaluate", "--in-memory",
        "--snapshot", json.dumps(snap),
    ])
    assert data["ok"] is True
    assert data["count"] == 0


def test_cli_active_empty_in_memory() -> None:
    data = _run_cli(["active", "--in-memory"])
    assert data["ok"] is True
    assert data["count"] == 0


def test_cli_check_intent_empty_in_memory() -> None:
    data = _run_cli([
        "check-intent", "--in-memory",
        "--company-id", "tickles",
        "--exchange", "binance", "--symbol", "BTC/USDT",
    ])
    assert data["ok"] is True
    assert data["blocked"] is False
