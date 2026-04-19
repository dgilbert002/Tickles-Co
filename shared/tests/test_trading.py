"""Phase 25 — tests for Banker + Treasury + Capabilities + Sizer."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from shared.trading import (
    AccountSnapshot,
    BalanceSnapshot,
    Banker,
    Capability,
    CapabilityChecker,
    CapabilityStore,
    MIGRATION_PATH,
    MarketSnapshot,
    SCOPE_COMPANY,
    SCOPE_STRATEGY,
    StrategyConfig,
    TradeIntent,
    Treasury,
    default_capability,
    read_migration_sql,
    size_intent,
)
from shared.trading.memory_pool import InMemoryTradingPool


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Migration file sanity
# ---------------------------------------------------------------------------


def test_migration_file_exists_and_contains_every_table() -> None:
    assert os.path.exists(MIGRATION_PATH)
    sql = read_migration_sql()
    for table in (
        "public.capabilities",
        "public.banker_balances",
        "public.banker_balances_latest",
        "public.leverage_history",
        "public.treasury_decisions",
    ):
        assert table in sql, f"missing {table} in migration"


# ---------------------------------------------------------------------------
# TradeIntent + hashing
# ---------------------------------------------------------------------------


def test_trade_intent_stable_hash_is_deterministic() -> None:
    a = TradeIntent(company_id="jarvais", exchange="bybit", symbol="BTC/USDT",
                    direction="long", strategy_id="s1",
                    requested_notional_usd=100.0)
    b = TradeIntent(company_id="jarvais", exchange="bybit", symbol="BTC/USDT",
                    direction="long", strategy_id="s1",
                    requested_notional_usd=100.0)
    c = TradeIntent(company_id="jarvais", exchange="bybit", symbol="BTC/USDT",
                    direction="short", strategy_id="s1",
                    requested_notional_usd=100.0)
    assert a.stable_hash() == b.stable_hash()
    assert a.stable_hash() != c.stable_hash()


def test_trade_intent_hash_ignores_metadata() -> None:
    a = TradeIntent(company_id="x", exchange="y", symbol="BTC/USDT",
                    direction="long", metadata={"foo": 1})
    b = TradeIntent(company_id="x", exchange="y", symbol="BTC/USDT",
                    direction="long", metadata={"foo": 2})
    assert a.stable_hash() == b.stable_hash()


# ---------------------------------------------------------------------------
# Capability.applies_to and validation
# ---------------------------------------------------------------------------


def test_capability_scope_validation_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        Capability(company_id="x", scope_kind="nope", scope_id="global")


def test_capability_applies_to_company_scope_matches_any_intent() -> None:
    cap = Capability(company_id="jarvais")
    intent = TradeIntent(company_id="jarvais", exchange="bybit",
                         symbol="BTC/USDT", direction="long")
    assert cap.applies_to(intent)


def test_capability_applies_to_strategy_scope_needs_match() -> None:
    cap = Capability(company_id="j", scope_kind=SCOPE_STRATEGY, scope_id="sma")
    i_match = TradeIntent(company_id="j", exchange="e", symbol="s",
                          direction="long", strategy_id="sma")
    i_mismatch = TradeIntent(company_id="j", exchange="e", symbol="s",
                             direction="long", strategy_id="other")
    assert cap.applies_to(i_match)
    assert not cap.applies_to(i_mismatch)


def test_capability_inactive_never_applies() -> None:
    cap = Capability(company_id="j", active=False)
    intent = TradeIntent(company_id="j", exchange="e", symbol="s", direction="long")
    assert not cap.applies_to(intent)


# ---------------------------------------------------------------------------
# CapabilityChecker — policy evaluation
# ---------------------------------------------------------------------------


def _intent() -> TradeIntent:
    return TradeIntent(
        company_id="jarvais",
        exchange="bybit",
        symbol="BTC/USDT",
        direction="long",
        strategy_id="sma",
    )


def test_checker_fails_closed_on_no_matching_capability() -> None:
    checker = CapabilityChecker([])
    result = checker.evaluate(_intent())
    assert result.approved is False
    assert any("no active capability" in r for r in result.reasons)


def test_checker_direction_denial() -> None:
    cap = Capability(company_id="jarvais", allow_directions=["short"])
    checker = CapabilityChecker([cap])
    result = checker.evaluate(_intent())
    assert not result.approved
    assert any("direction" in r for r in result.reasons)


def test_checker_venue_allowlist_denial() -> None:
    cap = Capability(company_id="jarvais", allow_venues=["binance"])
    checker = CapabilityChecker([cap])
    result = checker.evaluate(_intent())
    assert not result.approved
    assert any("venue" in r for r in result.reasons)


def test_checker_venue_denylist_denial() -> None:
    cap = Capability(company_id="jarvais", deny_venues=["bybit"])
    checker = CapabilityChecker([cap])
    result = checker.evaluate(_intent())
    assert not result.approved


def test_checker_numeric_caps_use_minimum_across_applicable_caps() -> None:
    company_cap = Capability(
        company_id="jarvais", max_notional_usd=5_000.0, max_leverage=10
    )
    strategy_cap = Capability(
        company_id="jarvais",
        scope_kind=SCOPE_STRATEGY,
        scope_id="sma",
        max_notional_usd=1_000.0,
        max_leverage=3,
    )
    checker = CapabilityChecker([company_cap, strategy_cap])
    result = checker.evaluate(_intent())
    assert result.approved is True
    assert result.effective_max_notional_usd == 1_000.0
    assert result.effective_max_leverage == 3


def test_checker_rejects_over_cap_requested_notional() -> None:
    cap = Capability(company_id="jarvais", max_notional_usd=100.0)
    checker = CapabilityChecker([cap])
    i = TradeIntent(
        company_id="jarvais", exchange="bybit", symbol="BTC/USDT",
        direction="long", requested_notional_usd=500.0,
    )
    assert not checker.evaluate(i).approved


# ---------------------------------------------------------------------------
# Sizer — the heart of Rule 1
# ---------------------------------------------------------------------------


def _market(price: float = 50_000.0) -> MarketSnapshot:
    return MarketSnapshot(
        price=price,
        bid=price - 5.0,
        ask=price + 5.0,
        contract_size=1.0,
        lot_step=0.0001,
        min_notional_usd=10.0,
        taker_fee_bps=10.0,
        maker_fee_bps=2.0,
        spread_bps_estimate=1.0,
        slippage_bps_estimate=2.0,
    )


def _account(balance: float = 1_000.0) -> AccountSnapshot:
    return AccountSnapshot(
        company_id="jarvais", exchange="bybit",
        account_id_external="ACC-1", currency="USD",
        balance=balance, margin_used=0.0,
    )


def test_sizer_respects_max_notional_pct_fraction() -> None:
    strategy = StrategyConfig(name="test", max_notional_pct=0.10)
    sized = size_intent(_intent(), _account(balance=1_000.0), _market(), strategy)
    assert not sized.skipped
    # 10% of 1000 = 100 USD, at ~50k price -> ~0.002
    assert sized.notional_usd <= 100.01
    assert sized.quantity > 0


def test_sizer_risk_based_sizing_matches_risk_per_trade() -> None:
    strategy = StrategyConfig(
        name="rb", risk_per_trade_pct=0.01, max_notional_pct=1.0,
        sl_distance_pct=0.02,
    )
    account = _account(balance=10_000.0)
    sized = size_intent(_intent(), account, _market(), strategy)
    # risk = 10000 * 0.01 * lev(1) = 100; sl=2% => notional = 5000
    assert abs(sized.notional_usd - 5_000.0) < 10.0


def test_sizer_applies_capability_cap_as_ceiling() -> None:
    strategy = StrategyConfig(name="s", max_notional_pct=1.0)
    account = _account(balance=10_000.0)
    sized = size_intent(
        _intent(), account, _market(), strategy,
        effective_max_notional_usd=250.0,
    )
    assert sized.notional_usd <= 250.01


def test_sizer_skips_below_min_notional() -> None:
    strategy = StrategyConfig(name="s", max_notional_pct=0.0001)
    account = _account(balance=100.0)
    market = _market()
    sized = size_intent(_intent(), account, market, strategy)
    assert sized.skipped
    assert "below venue minimum" in (sized.skipped_reason or "")


def test_sizer_skips_when_no_capital() -> None:
    strategy = StrategyConfig(name="s")
    account = AccountSnapshot(
        company_id="x", exchange="y", account_id_external="z",
        balance=0.0, margin_used=0.0,
    )
    sized = size_intent(_intent(), account, _market(), strategy)
    assert sized.skipped
    assert "no available capital" in (sized.skipped_reason or "")


def test_sizer_expected_entry_moves_against_direction() -> None:
    market = MarketSnapshot(price=100.0, contract_size=1.0,
                            spread_bps_estimate=10.0, slippage_bps_estimate=5.0)
    strategy = StrategyConfig(name="s", max_notional_pct=0.5)
    account = _account(balance=1_000.0)
    long_intent = _intent()
    short_intent = TradeIntent(
        company_id="jarvais", exchange="bybit", symbol="BTC/USDT",
        direction="short", strategy_id="sma",
    )
    long_sized = size_intent(long_intent, account, market, strategy)
    short_sized = size_intent(short_intent, account, market, strategy)
    assert long_sized.expected_entry_price > 100.0
    assert short_sized.expected_entry_price < 100.0


def test_sizer_fee_uses_maker_when_prefer_maker_is_true() -> None:
    strategy_t = StrategyConfig(name="s", max_notional_pct=0.5, prefer_maker=False)
    strategy_m = StrategyConfig(name="s", max_notional_pct=0.5, prefer_maker=True)
    sized_t = size_intent(_intent(), _account(), _market(), strategy_t)
    sized_m = size_intent(_intent(), _account(), _market(), strategy_m)
    assert sized_t.fee_rate_bps == 10.0
    assert sized_m.fee_rate_bps == 2.0
    assert sized_t.expected_fee_usd > sized_m.expected_fee_usd


def test_sizer_lot_step_rounds_down() -> None:
    strategy = StrategyConfig(name="s", max_notional_pct=1.0)
    market = MarketSnapshot(price=100.0, contract_size=1.0, lot_step=1.0,
                            min_notional_usd=10.0)
    sized = size_intent(_intent(), _account(balance=105.0), market, strategy)
    assert sized.quantity == float(int(sized.quantity))


def test_sizer_is_deterministic() -> None:
    strategy = StrategyConfig(name="g", max_notional_pct=0.3,
                              sl_distance_pct=0.015, tp_distance_pct=0.03)
    a = size_intent(_intent(), _account(balance=5_000.0), _market(), strategy).to_dict()
    b = size_intent(_intent(), _account(balance=5_000.0), _market(), strategy).to_dict()
    a.pop("intent_hash", None)
    b.pop("intent_hash", None)
    assert a == b


# ---------------------------------------------------------------------------
# Banker — DB round-trip via in-memory pool
# ---------------------------------------------------------------------------


def _snapshot(balance: float = 1_000.0, ts: datetime | None = None) -> BalanceSnapshot:
    return BalanceSnapshot(
        company_id="jarvais", exchange="bybit", account_id_external="ACC-1",
        balance=balance, equity=balance, margin_used=0.0, free_margin=balance,
        currency="USD", account_type="demo", source="test",
        ts=ts or datetime.now(timezone.utc),
    )


def test_banker_record_and_latest() -> None:
    pool = InMemoryTradingPool()
    banker = Banker()
    _run(banker.record_snapshot(pool, _snapshot(balance=1_000.0)))
    _run(banker.record_snapshot(pool, _snapshot(balance=1_250.0,
                                                ts=datetime.now(timezone.utc))))
    latest = _run(banker.latest_snapshot(pool, "jarvais", "bybit", "ACC-1"))
    assert latest is not None
    assert latest.balance == 1_250.0
    assert latest.available_capital() == 1_250.0


def test_banker_history_and_company_latest() -> None:
    pool = InMemoryTradingPool()
    banker = Banker()
    base = datetime.now(timezone.utc)
    for i in range(3):
        _run(banker.record_snapshot(
            pool,
            _snapshot(balance=1_000.0 + i, ts=base + timedelta(seconds=i)),
        ))
    history = _run(banker.list_recent(pool, "jarvais", "bybit", "ACC-1", limit=10))
    assert len(history) == 3
    assert history[0].balance == 1_002.0
    assert history[-1].balance == 1_000.0
    company = _run(banker.latest_for_company(pool, "jarvais"))
    assert len(company) == 1
    assert company[0].balance == 1_002.0


def test_banker_available_capital_zero_when_no_snapshot() -> None:
    pool = InMemoryTradingPool()
    banker = Banker()
    v = _run(banker.available_capital_usd(pool, "jarvais", "bybit", "ACC-1"))
    assert v == 0.0


# ---------------------------------------------------------------------------
# CapabilityStore — DB round-trip
# ---------------------------------------------------------------------------


def test_capability_store_upsert_list_delete_roundtrip() -> None:
    pool = InMemoryTradingPool()
    store = CapabilityStore()
    cap = default_capability("jarvais")
    _run(store.upsert(pool, cap))
    caps = _run(store.list_for_company(pool, "jarvais"))
    assert len(caps) == 1
    assert caps[0].company_id == "jarvais"
    assert caps[0].max_notional_usd == 1_000.0
    # Delete
    n = _run(store.delete(pool, "jarvais", SCOPE_COMPANY, "global"))
    assert n >= 1
    assert _run(store.list_for_company(pool, "jarvais")) == []


def test_capability_store_upsert_idempotent_same_scope() -> None:
    pool = InMemoryTradingPool()
    store = CapabilityStore()
    cap = default_capability("jarvais")
    cap.max_notional_usd = 500.0
    _run(store.upsert(pool, cap))
    cap.max_notional_usd = 999.0
    _run(store.upsert(pool, cap))
    caps = _run(store.list_for_company(pool, "jarvais"))
    assert len(caps) == 1
    assert caps[0].max_notional_usd == 999.0


# ---------------------------------------------------------------------------
# Treasury — end-to-end
# ---------------------------------------------------------------------------


def test_treasury_evaluate_approves_and_persists() -> None:
    pool = InMemoryTradingPool()
    # Seed: capability + balance snapshot
    _run(CapabilityStore().upsert(pool, default_capability("jarvais")))
    _run(Banker().record_snapshot(pool, _snapshot(balance=5_000.0)))

    treasury = Treasury()
    intent = TradeIntent(
        company_id="jarvais", exchange="bybit", symbol="BTC/USDT",
        direction="long", strategy_id="sma",
    )
    decision = _run(treasury.evaluate(
        pool=pool,
        intent=intent,
        account_id_external="ACC-1",
        market=_market(),
        strategy=StrategyConfig(name="sma", max_notional_pct=0.05),
    ))
    assert decision.approved is True
    assert decision.sized is not None
    assert decision.sized.notional_usd > 0
    assert len(pool.decisions) == 1
    assert pool.decisions[0]["approved"] is True


def test_treasury_evaluate_denies_when_no_capability() -> None:
    pool = InMemoryTradingPool()
    _run(Banker().record_snapshot(pool, _snapshot(balance=5_000.0)))
    treasury = Treasury()
    intent = _intent()
    decision = _run(treasury.evaluate(
        pool=pool, intent=intent, account_id_external="ACC-1",
        market=_market(), strategy=StrategyConfig(name="s"),
    ))
    assert decision.approved is False
    assert decision.capability_check is not None
    assert not decision.capability_check.approved
    assert pool.decisions[0]["approved"] is False


def test_treasury_evaluate_denies_when_no_balance_snapshot() -> None:
    pool = InMemoryTradingPool()
    _run(CapabilityStore().upsert(pool, default_capability("jarvais")))
    treasury = Treasury()
    decision = _run(treasury.evaluate(
        pool=pool, intent=_intent(), account_id_external="ACC-1",
        market=_market(), strategy=StrategyConfig(name="s"),
    ))
    assert not decision.approved
    assert any("no balance snapshot" in r for r in decision.reasons)


def test_treasury_dry_run_does_not_persist() -> None:
    pool = InMemoryTradingPool()
    _run(CapabilityStore().upsert(pool, default_capability("jarvais")))
    _run(Banker().record_snapshot(pool, _snapshot(balance=5_000.0)))
    treasury = Treasury()
    _run(treasury.evaluate(
        pool=pool, intent=_intent(), account_id_external="ACC-1",
        market=_market(), strategy=StrategyConfig(name="s"),
        persist=False,
    ))
    assert pool.decisions == []


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


def _cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "shared.cli.treasury_cli", *args]
    return subprocess.run(
        cmd, capture_output=True, text=True,
        env={**os.environ, **(env or {})}, timeout=30,
    )


def test_cli_apply_migration_prints_path() -> None:
    cp = _cli("apply-migration")
    assert cp.returncode == 0
    payload = json.loads(cp.stdout.strip())
    assert payload["ok"] is True
    assert payload["migration_path"].endswith(
        "2026_04_19_phase25_banker_treasury.sql"
    )


def test_cli_migration_sql_emits_full_ddl() -> None:
    cp = _cli("migration-sql")
    assert cp.returncode == 0
    assert "public.capabilities" in cp.stdout
    assert "BEGIN" in cp.stdout and "COMMIT" in cp.stdout


def test_cli_pure_size_works_without_db() -> None:
    cp = _cli(
        "pure-size",
        "--company", "jarvais", "--exchange", "bybit",
        "--symbol", "BTC/USDT", "--direction", "long",
        "--account-id", "ACC-1",
        "--price", "50000",
        "--balance", "1000", "--max-notional-pct", "0.1",
    )
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout.strip())
    assert payload["ok"] is True
    assert payload["sized"]["quantity"] > 0
    assert payload["sized"]["notional_usd"] <= 100.01


def test_cli_help_lists_all_subcommands() -> None:
    cp = _cli("--help")
    assert cp.returncode == 0
    for sub in ("caps-list", "caps-upsert", "caps-seed-default",
                "balances-record", "balances-latest",
                "evaluate", "pure-size",
                "apply-migration", "migration-sql"):
        assert sub in cp.stdout, f"missing {sub}"


def test_services_registry_now_includes_banker() -> None:
    # Phase 25 registers the banker service; verify that Phase 22 registry
    # has the new descriptor without duplicating anything existing.
    from shared.services import SERVICE_REGISTRY
    names = {s.name for s in SERVICE_REGISTRY.list_services()}
    assert "banker" in names
    # The pre-existing services are still there.
    for baseline in ("md-gateway", "candle-daemon", "catalog",
                     "bt-workers", "auditor"):
        assert baseline in names
