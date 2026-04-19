"""Phase 26 — tests for Execution Layer (adapters + store + router + CLI)."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import List

from shared.execution import (
    ADAPTER_CCXT,
    ADAPTER_NAUTILUS,
    ADAPTER_PAPER,
    DIRECTION_LONG,
    DIRECTION_SHORT,
    ExecutionIntent,
    ExecutionRouter,
    MIGRATION_PATH,
    MarketTick,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    ORDER_TYPE_STOP,
    PaperExecutionAdapter,
    STATUS_ACCEPTED,
    STATUS_CANCELED,
    STATUS_FILLED,
    STATUS_REJECTED,
    default_adapters,
    read_migration_sql,
)
from shared.execution.memory_pool import InMemoryExecutionPool
from shared.execution.nautilus_adapter import NautilusExecutionAdapter
from shared.services.registry import SERVICE_REGISTRY, register_builtin_services


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _intent(
    *,
    company: str = "tickles",
    symbol: str = "BTC/USDT",
    direction: str = DIRECTION_LONG,
    qty: float = 0.01,
    order_type: str = ORDER_TYPE_MARKET,
    price: float | None = None,
    stop: float | None = None,
    client_id: str | None = None,
) -> ExecutionIntent:
    return ExecutionIntent(
        company_id=company,
        strategy_id="sma",
        agent_id=None,
        exchange="bybit",
        account_id_external="ACC-1",
        symbol=symbol,
        direction=direction,
        order_type=order_type,
        quantity=qty,
        requested_price=price,
        stop_price=stop,
        client_order_id=client_id,
        requested_notional_usd=None,
        intent_hash="test-hash",
    )


def _tick(symbol: str = "BTC/USDT", bid: float = 69990.0, ask: float = 70010.0, last: float = 70000.0) -> MarketTick:
    return MarketTick(symbol=symbol, bid=bid, ask=ask, last=last)


# ---------------------------------------------------------------------------
# Migration file
# ---------------------------------------------------------------------------


def test_migration_file_exists_and_has_tables() -> None:
    assert MIGRATION_PATH.exists()
    sql = read_migration_sql()
    for keyword in [
        "CREATE TABLE IF NOT EXISTS public.orders",
        "CREATE TABLE IF NOT EXISTS public.order_events",
        "CREATE TABLE IF NOT EXISTS public.fills",
        "CREATE TABLE IF NOT EXISTS public.position_snapshots",
        "CREATE OR REPLACE VIEW public.positions_current",
        "UNIQUE (adapter, client_order_id)",
    ]:
        assert keyword in sql, f"missing {keyword!r} in migration"


# ---------------------------------------------------------------------------
# ExecutionIntent helpers
# ---------------------------------------------------------------------------


def test_ensure_client_order_id_is_stable_when_set() -> None:
    i = _intent(client_id="my-fixed-id")
    assert i.ensure_client_order_id() == "my-fixed-id"


def test_ensure_client_order_id_generates_unique_prefix() -> None:
    i1 = _intent()
    i2 = _intent()
    id1 = i1.ensure_client_order_id()
    id2 = i2.ensure_client_order_id()
    assert id1.startswith("tk-") and id2.startswith("tk-")
    assert id1 != id2


# ---------------------------------------------------------------------------
# Paper adapter — fills
# ---------------------------------------------------------------------------


def test_paper_market_long_fills_at_ask_with_fee() -> None:
    paper = PaperExecutionAdapter(taker_fee_bps=4.0)
    updates = _run(paper.submit(_intent(), market=_tick(ask=70010.0)))
    fills = [u for u in updates if u.is_fill]
    assert len(fills) == 1
    fill = fills[0]
    assert fill.status == STATUS_FILLED
    assert fill.last_fill_price == 70010.0
    assert fill.last_fill_quantity == 0.01
    assert fill.last_fill_is_maker is False
    assert fill.last_fill_fee_usd is not None
    assert abs(fill.last_fill_fee_usd - 70010.0 * 0.01 * 0.0004) < 1e-9


def test_paper_market_short_fills_at_bid() -> None:
    paper = PaperExecutionAdapter()
    updates = _run(paper.submit(
        _intent(direction=DIRECTION_SHORT),
        market=_tick(bid=69990.0, ask=70010.0, last=70000.0),
    ))
    fills = [u for u in updates if u.is_fill]
    assert fills and fills[0].last_fill_price == 69990.0


def test_paper_market_without_tick_rejects() -> None:
    paper = PaperExecutionAdapter()
    updates = _run(paper.submit(_intent(), market=None))
    last = updates[-1]
    assert last.status == STATUS_REJECTED
    assert "no market tick" in (last.message or "")


def test_paper_market_with_slippage_adjusts_price() -> None:
    paper = PaperExecutionAdapter(slippage_bps=10.0)
    updates = _run(paper.submit(_intent(), market=_tick(ask=70010.0)))
    fill = [u for u in updates if u.is_fill][0]
    assert fill.last_fill_price == 70010.0 * (1 + 0.0010)


def test_paper_market_rejects_non_positive_quantity() -> None:
    paper = PaperExecutionAdapter()
    updates = _run(paper.submit(_intent(qty=0.0), market=_tick()))
    assert updates[-1].status == STATUS_REJECTED


def test_paper_limit_long_accepts_then_fills_on_touch() -> None:
    paper = PaperExecutionAdapter(maker_fee_bps=1.0)
    intent = _intent(order_type=ORDER_TYPE_LIMIT, price=69000.0)
    updates = _run(paper.submit(intent, market=_tick(ask=70010.0)))
    fills = [u for u in updates if u.is_fill]
    assert fills == []
    assert [u for u in updates if u.status == STATUS_ACCEPTED]

    new_ticks = _run(paper.touch(_tick(bid=69000.0, ask=69000.0, last=69000.0)))
    assert new_ticks and new_ticks[0].is_fill
    assert new_ticks[0].last_fill_price == 69000.0
    assert new_ticks[0].last_fill_is_maker is True


def test_paper_limit_short_immediate_fill_if_market_already_crossed() -> None:
    paper = PaperExecutionAdapter()
    intent = _intent(direction=DIRECTION_SHORT, order_type=ORDER_TYPE_LIMIT, price=69990.0)
    updates = _run(paper.submit(intent, market=_tick(bid=70000.0, ask=70010.0, last=70005.0)))
    fills = [u for u in updates if u.is_fill]
    assert fills and fills[0].last_fill_price == 69990.0


def test_paper_stop_triggers_when_last_breaches() -> None:
    paper = PaperExecutionAdapter()
    intent = _intent(order_type=ORDER_TYPE_STOP, stop=70100.0)
    updates = _run(paper.submit(intent, market=_tick(last=70000.0)))
    assert [u for u in updates if u.is_fill] == []
    triggered = _run(paper.touch(_tick(last=70200.0, bid=70190.0, ask=70210.0)))
    assert triggered and triggered[0].is_fill


def test_paper_cancel_is_idempotent() -> None:
    paper = PaperExecutionAdapter()
    intent = _intent(order_type=ORDER_TYPE_LIMIT, price=60000.0, client_id="resting-1")
    _run(paper.submit(intent, market=_tick(ask=70010.0)))
    first = _run(paper.cancel("resting-1"))
    second = _run(paper.cancel("resting-1"))
    assert first.status == STATUS_CANCELED
    assert second.status == STATUS_CANCELED


def test_paper_submit_is_idempotent_for_same_client_id() -> None:
    paper = PaperExecutionAdapter()
    intent = _intent(client_id="retry-1")
    updates1 = _run(paper.submit(intent, market=_tick()))
    updates2 = _run(paper.submit(intent, market=_tick()))
    assert len(updates1) == len(updates2)


def test_paper_cancel_unknown_rejects() -> None:
    paper = PaperExecutionAdapter()
    res = _run(paper.cancel("no-such-id"))
    assert res.status == STATUS_REJECTED


# ---------------------------------------------------------------------------
# Nautilus stub
# ---------------------------------------------------------------------------


def test_nautilus_submit_returns_rejection_by_default() -> None:
    adapter = NautilusExecutionAdapter()
    intent = _intent(client_id="nautilus-1")
    updates = _run(adapter.submit(intent))
    assert len(updates) == 1
    assert updates[0].status == STATUS_REJECTED


# ---------------------------------------------------------------------------
# Router + Store (in-memory)
# ---------------------------------------------------------------------------


def test_router_submit_paper_persists_order_and_fill() -> None:
    pool = InMemoryExecutionPool()
    router = ExecutionRouter(pool, adapters=default_adapters(), default_adapter=ADAPTER_PAPER)
    snap = _run(router.submit(_intent(client_id="r-1"), market=_tick()))
    assert snap.status == STATUS_FILLED
    assert snap.filled_quantity == 0.01
    assert len(pool.orders) == 1
    assert pool.orders[0]["filled_quantity"] == 0.01
    assert len(pool.fills) == 1
    assert len(pool.positions) >= 1


def test_router_submit_is_idempotent_on_duplicate_client_id() -> None:
    pool = InMemoryExecutionPool()
    router = ExecutionRouter(pool, adapters=default_adapters(), default_adapter=ADAPTER_PAPER)
    i1 = _intent(client_id="dup-1")
    _run(router.submit(i1, market=_tick()))
    _run(router.submit(i1, market=_tick()))
    assert len(pool.orders) == 1


def test_router_cancel_marks_status_and_logs_event() -> None:
    pool = InMemoryExecutionPool()
    router = ExecutionRouter(pool, adapters=default_adapters(), default_adapter=ADAPTER_PAPER)
    intent = _intent(
        order_type=ORDER_TYPE_LIMIT, price=60000.0, client_id="cx-1"
    )
    _run(router.submit(intent, market=_tick(ask=70010.0)))
    snap = _run(router.cancel("cx-1"))
    assert snap.status == STATUS_CANCELED
    cancel_events = [e for e in pool.order_events if e["event_type"] == "cancel"]
    assert cancel_events


def test_router_unknown_adapter_raises() -> None:
    pool = InMemoryExecutionPool()
    router = ExecutionRouter(pool, adapters=default_adapters(), default_adapter=ADAPTER_PAPER)
    try:
        router.get_adapter("unknown")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown adapter")


def test_router_rejected_order_records_event() -> None:
    pool = InMemoryExecutionPool()
    paper = PaperExecutionAdapter()
    router = ExecutionRouter(pool, adapters={ADAPTER_PAPER: paper}, default_adapter=ADAPTER_PAPER)
    snap = _run(router.submit(_intent(qty=0.0, client_id="bad-1"), market=_tick()))
    assert snap.status == STATUS_REJECTED
    assert any(e["event_type"] == "reject" for e in pool.order_events)


def test_router_list_open_orders_returns_only_active() -> None:
    pool = InMemoryExecutionPool()
    router = ExecutionRouter(pool, adapters=default_adapters(), default_adapter=ADAPTER_PAPER)
    _run(router.submit(_intent(client_id="m-1"), market=_tick()))
    _run(router.submit(
        _intent(order_type=ORDER_TYPE_LIMIT, price=60000.0, client_id="l-1"),
        market=_tick(ask=70010.0),
    ))
    open_orders = _run(router._store.list_open_orders("tickles", limit=100))  # noqa: SLF001
    assert [o.client_order_id for o in open_orders] == ["l-1"]


def test_router_positions_current_returns_latest_row() -> None:
    pool = InMemoryExecutionPool()
    router = ExecutionRouter(pool, adapters=default_adapters(), default_adapter=ADAPTER_PAPER)
    _run(router.submit(_intent(client_id="p-1"), market=_tick()))
    _run(router.submit(_intent(client_id="p-2"), market=_tick(ask=70100.0, bid=70080.0, last=70090.0)))
    positions = _run(router._store.list_current_positions("tickles"))  # noqa: SLF001
    assert len(positions) == 1
    assert positions[0].symbol == "BTC/USDT"


def test_router_fills_list_is_newest_first() -> None:
    pool = InMemoryExecutionPool()
    router = ExecutionRouter(pool, adapters=default_adapters(), default_adapter=ADAPTER_PAPER)
    _run(router.submit(_intent(client_id="f-1"), market=_tick()))
    _run(router.submit(_intent(client_id="f-2"), market=_tick(ask=70200.0)))
    fills = _run(router._store.list_fills("tickles", limit=10))  # noqa: SLF001
    assert len(fills) == 2
    assert fills[0].ts >= fills[1].ts


def test_router_cancel_unknown_raises() -> None:
    pool = InMemoryExecutionPool()
    router = ExecutionRouter(pool, adapters=default_adapters(), default_adapter=ADAPTER_PAPER)
    try:
        _run(router.cancel("nope"))
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown client_order_id")


# ---------------------------------------------------------------------------
# default_adapters / service registry
# ---------------------------------------------------------------------------


def test_default_adapters_exposes_three_names() -> None:
    adapters = default_adapters()
    assert set(adapters) == {ADAPTER_PAPER, ADAPTER_CCXT, ADAPTER_NAUTILUS}


def test_executor_service_is_registered() -> None:
    register_builtin_services()
    assert "executor" in SERVICE_REGISTRY
    desc = SERVICE_REGISTRY.get("executor")
    assert desc.kind == "worker"
    assert desc.module == "shared.cli.execution_cli"
    assert desc.enabled_on_vps is False


# ---------------------------------------------------------------------------
# CLI smokes
# ---------------------------------------------------------------------------


def _run_cli(args: List[str]) -> subprocess.CompletedProcess:
    root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "-m", "shared.cli.execution_cli", *args],
        capture_output=True, text=True, cwd=root, timeout=30,
    )


def test_cli_migration_sql_prints_ddl() -> None:
    cp = _run_cli(["migration-sql"])
    assert cp.returncode == 0
    assert "CREATE TABLE IF NOT EXISTS public.orders" in cp.stdout


def test_cli_adapters_lists_all_three() -> None:
    cp = _run_cli(["adapters"])
    assert cp.returncode == 0, cp.stderr
    data = json.loads(cp.stdout.strip().splitlines()[-1])
    assert set(data["adapters"]) == {"paper", "ccxt", "nautilus"}


def test_cli_help_contains_all_subcommands() -> None:
    cp = _run_cli(["--help"])
    assert cp.returncode == 0
    for sub in [
        "apply-migration", "migration-sql", "adapters",
        "submit", "cancel", "orders", "fills", "positions",
        "paper-simulate",
    ]:
        assert sub in cp.stdout, f"missing subcommand {sub!r} in --help"


def test_cli_paper_simulate_fills_and_reports_position() -> None:
    cp = _run_cli([
        "paper-simulate",
        "--company", "tickles",
        "--exchange", "bybit",
        "--account-id", "ACC-1",
        "--symbol", "BTC/USDT",
        "--direction", "long",
        "--order-type", "market",
        "--quantity", "0.01",
        "--market-last", "70000",
        "--market-bid", "69990",
        "--market-ask", "70010",
        "--taker-fee-bps", "4",
    ])
    assert cp.returncode == 0, cp.stderr
    # Find the JSON output line among any logs.
    last_json = None
    for line in cp.stdout.strip().splitlines():
        try:
            data = json.loads(line)
            last_json = data
        except Exception:
            continue
    assert last_json is not None, "no JSON output"
    assert last_json["ok"] is True
    assert last_json["order"]["status"] == "filled"
    assert last_json["order"]["filled_quantity"] == 0.01
    assert len(last_json["fills"]) >= 1
    assert last_json["fills"][0]["price"] == 70010.0
    assert len(last_json["positions"]) == 1
