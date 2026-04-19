"""
shared.cli.treasury_cli — Phase 25 operator CLI for Banker + Treasury +
Capabilities.

Subcommands (all stdout is single-line JSON unless noted):

Migration
  * ``apply-migration``   — print migration path + psql command.
  * ``migration-sql``     — raw SQL to stdout (pipe-friendly).

Capabilities
  * ``caps list --company X``
  * ``caps get --company X --scope-kind company --scope-id global``
  * ``caps upsert ... many flags ...``
  * ``caps delete --company X --scope-kind ... --scope-id ...``
  * ``caps seed-default --company X``

Banker
  * ``balances record --company X --exchange Y --account-id A --balance N ...``
  * ``balances latest --company X``
  * ``balances one --company X --exchange Y --account-id A``
  * ``balances history --company X --exchange Y --account-id A [--limit N]``

Treasury
  * ``evaluate`` — full intent evaluation, persists a decision if --persist.
  * ``dry-run`` — same but never writes the decision row.

DSN handling mirrors the services_catalog_cli: ``--dsn`` or
``TICKLES_SHARED_DSN`` or canonical shared pool.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from shared.cli._common import (
    EXIT_FAIL,
    EXIT_OK,
    Subcommand,
    build_parser,
    emit,
    run,
)
from shared.trading import (
    AccountSnapshot,
    BalanceSnapshot,
    Banker,
    Capability,
    CapabilityStore,
    MarketSnapshot,
    MIGRATION_PATH,
    StrategyConfig,
    TradeIntent,
    Treasury,
    default_capability,
    read_migration_sql,
)


# ---------------------------------------------------------------------------
# Pool acquisition — copy of the services_catalog_cli shape.
# ---------------------------------------------------------------------------


async def _acquire_pool(dsn: Optional[str]) -> Any:
    if dsn:
        import asyncpg  # type: ignore[import-not-found]

        class _DsnPool:
            def __init__(self, pool: Any) -> None:
                self._pool = pool

            async def execute(self, sql: str, params: Sequence[Any]) -> int:
                async with self._pool.acquire() as conn:
                    status = await conn.execute(sql, *params)
                try:
                    return int(str(status).rsplit(" ", 1)[-1])
                except (ValueError, AttributeError):
                    return 0

            async def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
                if not rows:
                    return 0
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.executemany(sql, rows)
                return len(rows)

            async def fetch_all(self, sql: str, params: Sequence[Any]) -> List[Dict[str, Any]]:
                async with self._pool.acquire() as conn:
                    records = await conn.fetch(sql, *params)
                return [dict(r) for r in records]

            async def fetch_one(
                self, sql: str, params: Sequence[Any]
            ) -> Optional[Dict[str, Any]]:
                async with self._pool.acquire() as conn:
                    row = await conn.fetchrow(sql, *params)
                return dict(row) if row is not None else None

        return _DsnPool(await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4))

    from shared.utils import db
    return await db.get_shared_pool()


def _dsn(args: argparse.Namespace) -> Optional[str]:
    return getattr(args, "dsn", None) or os.environ.get("TICKLES_SHARED_DSN")


def _split(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _parse_meta(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return dict(v) if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def cmd_apply_migration(_args: argparse.Namespace) -> int:
    emit({
        "ok": True,
        "migration_path": MIGRATION_PATH,
        "apply_example": (
            f"psql -h 127.0.0.1 -U admin -d tickles_shared -f {MIGRATION_PATH}"
        ),
    })
    return EXIT_OK


def cmd_migration_sql(_args: argparse.Namespace) -> int:
    sys.stdout.write(read_migration_sql())
    sys.stdout.flush()
    return EXIT_OK


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


async def _caps_list(dsn: Optional[str], company: str, active_only: bool) -> int:
    pool = await _acquire_pool(dsn)
    rows = await CapabilityStore().list_for_company(pool, company, active_only=active_only)
    emit({"ok": True, "count": len(rows), "capabilities": [r.to_dict() for r in rows]})
    return EXIT_OK


def cmd_caps_list(args: argparse.Namespace) -> int:
    return asyncio.run(_caps_list(_dsn(args), args.company, bool(args.active_only)))


async def _caps_get(dsn: Optional[str], company: str, sk: str, sid: str) -> int:
    pool = await _acquire_pool(dsn)
    cap = await CapabilityStore().get(pool, company, sk, sid)
    if cap is None:
        emit({"ok": False, "error": "not found"})
        return EXIT_FAIL
    emit({"ok": True, "capability": cap.to_dict()})
    return EXIT_OK


def cmd_caps_get(args: argparse.Namespace) -> int:
    return asyncio.run(
        _caps_get(_dsn(args), args.company, args.scope_kind, args.scope_id)
    )


async def _caps_upsert(dsn: Optional[str], cap: Capability) -> int:
    pool = await _acquire_pool(dsn)
    await CapabilityStore().upsert(pool, cap)
    fetched = await CapabilityStore().get(pool, cap.company_id, cap.scope_kind, cap.scope_id)
    emit({
        "ok": True,
        "upserted": True,
        "capability": fetched.to_dict() if fetched else cap.to_dict(),
    })
    return EXIT_OK


def cmd_caps_upsert(args: argparse.Namespace) -> int:
    cap = Capability(
        company_id=args.company,
        scope_kind=args.scope_kind,
        scope_id=args.scope_id,
        max_notional_usd=args.max_notional_usd,
        max_leverage=args.max_leverage,
        max_daily_loss_usd=args.max_daily_loss_usd,
        max_open_positions=args.max_open_positions,
        allow_venues=_split(args.allow_venues),
        deny_venues=_split(args.deny_venues),
        allow_symbols=_split(args.allow_symbols),
        deny_symbols=_split(args.deny_symbols),
        allow_directions=_split(args.allow_directions) or ["long", "short"],
        allow_order_types=_split(args.allow_order_types) or ["market", "limit"],
        active=not bool(args.inactive),
        notes=args.notes or "",
        metadata=_parse_meta(args.metadata),
    )
    return asyncio.run(_caps_upsert(_dsn(args), cap))


async def _caps_delete(dsn: Optional[str], company: str, sk: str, sid: str) -> int:
    pool = await _acquire_pool(dsn)
    n = await CapabilityStore().delete(pool, company, sk, sid)
    emit({"ok": True, "deleted": int(n)})
    return EXIT_OK


def cmd_caps_delete(args: argparse.Namespace) -> int:
    return asyncio.run(
        _caps_delete(_dsn(args), args.company, args.scope_kind, args.scope_id)
    )


async def _caps_seed_default(dsn: Optional[str], company: str) -> int:
    pool = await _acquire_pool(dsn)
    cap = default_capability(company)
    await CapabilityStore().upsert(pool, cap)
    fetched = await CapabilityStore().get(
        pool, cap.company_id, cap.scope_kind, cap.scope_id
    )
    emit({
        "ok": True,
        "seeded": True,
        "capability": fetched.to_dict() if fetched else cap.to_dict(),
    })
    return EXIT_OK


def cmd_caps_seed_default(args: argparse.Namespace) -> int:
    return asyncio.run(_caps_seed_default(_dsn(args), args.company))


# ---------------------------------------------------------------------------
# Banker
# ---------------------------------------------------------------------------


async def _balances_record(dsn: Optional[str], snap: BalanceSnapshot) -> int:
    pool = await _acquire_pool(dsn)
    n = await Banker().record_snapshot(pool, snap)
    emit({"ok": True, "recorded": int(n), "snapshot": snap.to_dict()})
    return EXIT_OK


def cmd_balances_record(args: argparse.Namespace) -> int:
    snap = BalanceSnapshot(
        company_id=args.company,
        exchange=args.exchange,
        account_id_external=args.account_id,
        balance=float(args.balance),
        account_type=args.account_type or "demo",
        currency=args.currency or "USD",
        equity=float(args.equity) if args.equity is not None else None,
        margin_used=float(args.margin_used) if args.margin_used is not None else None,
        free_margin=float(args.free_margin) if args.free_margin is not None else None,
        unrealised_pnl=(
            float(args.unrealised_pnl) if args.unrealised_pnl is not None else None
        ),
        source=args.source or "cli",
        metadata=_parse_meta(args.metadata),
    )
    return asyncio.run(_balances_record(_dsn(args), snap))


async def _balances_latest(dsn: Optional[str], company: str) -> int:
    pool = await _acquire_pool(dsn)
    rows = await Banker().latest_for_company(pool, company)
    emit({"ok": True, "count": len(rows), "balances": [r.to_dict() for r in rows]})
    return EXIT_OK


def cmd_balances_latest(args: argparse.Namespace) -> int:
    return asyncio.run(_balances_latest(_dsn(args), args.company))


async def _balances_one(
    dsn: Optional[str],
    company: str,
    exchange: str,
    account_id: str,
    currency: str,
) -> int:
    pool = await _acquire_pool(dsn)
    snap = await Banker().latest_snapshot(pool, company, exchange, account_id, currency)
    if snap is None:
        emit({"ok": False, "error": "no snapshot"})
        return EXIT_FAIL
    emit({"ok": True, "balance": snap.to_dict()})
    return EXIT_OK


def cmd_balances_one(args: argparse.Namespace) -> int:
    return asyncio.run(
        _balances_one(
            _dsn(args),
            args.company,
            args.exchange,
            args.account_id,
            args.currency or "USD",
        )
    )


async def _balances_history(
    dsn: Optional[str],
    company: str,
    exchange: str,
    account_id: str,
    currency: str,
    limit: int,
) -> int:
    pool = await _acquire_pool(dsn)
    rows = await Banker().list_recent(
        pool, company, exchange, account_id, currency, limit=limit
    )
    emit({
        "ok": True,
        "count": len(rows),
        "history": [r.to_dict() for r in rows],
    })
    return EXIT_OK


def cmd_balances_history(args: argparse.Namespace) -> int:
    return asyncio.run(
        _balances_history(
            _dsn(args),
            args.company,
            args.exchange,
            args.account_id,
            args.currency or "USD",
            int(args.limit),
        )
    )


# ---------------------------------------------------------------------------
# Treasury evaluation
# ---------------------------------------------------------------------------


def _intent_from_args(args: argparse.Namespace) -> TradeIntent:
    return TradeIntent(
        company_id=args.company,
        exchange=args.exchange,
        symbol=args.symbol,
        direction=args.direction,
        strategy_id=args.strategy_id,
        agent_id=args.agent_id,
        order_type=args.order_type or "market",
        requested_notional_usd=(
            float(args.requested_notional_usd)
            if args.requested_notional_usd is not None else None
        ),
        requested_leverage=(
            int(args.requested_leverage)
            if args.requested_leverage is not None else None
        ),
        metadata=_parse_meta(args.intent_metadata),
    )


def _market_from_args(args: argparse.Namespace) -> MarketSnapshot:
    return MarketSnapshot(
        price=float(args.price),
        bid=float(args.bid) if args.bid is not None else None,
        ask=float(args.ask) if args.ask is not None else None,
        contract_size=float(args.contract_size),
        lot_step=float(args.lot_step),
        min_notional_usd=float(args.min_notional_usd),
        taker_fee_bps=float(args.taker_fee_bps),
        maker_fee_bps=float(args.maker_fee_bps),
        spread_bps_estimate=float(args.spread_bps),
        slippage_bps_estimate=float(args.slippage_bps),
        overnight_bps_per_day=float(args.overnight_bps_per_day),
    )


def _strategy_from_args(args: argparse.Namespace) -> StrategyConfig:
    return StrategyConfig(
        name=args.strategy_name or (args.strategy_id or "adhoc"),
        risk_per_trade_pct=float(args.risk_per_trade_pct),
        max_notional_pct=float(args.max_notional_pct),
        sl_distance_pct=(
            float(args.sl_distance_pct) if args.sl_distance_pct is not None else None
        ),
        tp_distance_pct=(
            float(args.tp_distance_pct) if args.tp_distance_pct is not None else None
        ),
        prefer_maker=bool(args.prefer_maker),
    )


async def _evaluate(
    dsn: Optional[str],
    intent: TradeIntent,
    account_id: str,
    market: MarketSnapshot,
    strategy: StrategyConfig,
    currency: str,
    persist: bool,
) -> int:
    pool = await _acquire_pool(dsn)
    treasury = Treasury()
    decision = await treasury.evaluate(
        pool=pool,
        intent=intent,
        account_id_external=account_id,
        market=market,
        strategy=strategy,
        currency=currency,
        persist=persist,
    )
    emit({"ok": True, "decision": decision.to_dict()})
    return EXIT_OK if decision.approved else EXIT_FAIL


def cmd_evaluate(args: argparse.Namespace) -> int:
    intent = _intent_from_args(args)
    market = _market_from_args(args)
    strategy = _strategy_from_args(args)
    return asyncio.run(
        _evaluate(
            _dsn(args),
            intent,
            args.account_id,
            market,
            strategy,
            args.currency or "USD",
            persist=not bool(args.dry_run),
        )
    )


def cmd_pure_size(args: argparse.Namespace) -> int:
    """Pure sizer — no DB touch. Useful for golden-like spot checks."""
    from shared.trading.sizer import size_intent
    intent = _intent_from_args(args)
    market = _market_from_args(args)
    strategy = _strategy_from_args(args)
    account = AccountSnapshot(
        company_id=intent.company_id,
        exchange=intent.exchange,
        account_id_external=args.account_id,
        currency=args.currency or "USD",
        balance=float(args.balance),
        equity=float(args.equity) if args.equity is not None else None,
        margin_used=float(args.margin_used or 0.0),
        free_margin=float(args.free_margin) if args.free_margin is not None else None,
    )
    sized = size_intent(
        intent=intent,
        account=account,
        market=market,
        strategy=strategy,
        effective_max_notional_usd=(
            float(args.cap_max_notional_usd)
            if args.cap_max_notional_usd is not None else None
        ),
        effective_max_leverage=(
            int(args.cap_max_leverage)
            if args.cap_max_leverage is not None else None
        ),
    )
    emit({"ok": True, "sized": sized.to_dict()})
    return EXIT_OK if not sized.skipped else EXIT_FAIL


# ---------------------------------------------------------------------------
# Arg builders
# ---------------------------------------------------------------------------


def _add_dsn(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=None)


def _add_intent_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--company", required=True)
    p.add_argument("--exchange", required=True)
    p.add_argument("--symbol", required=True)
    p.add_argument("--direction", required=True, choices=("long", "short"))
    p.add_argument("--strategy-id", default=None)
    p.add_argument("--strategy-name", default=None)
    p.add_argument("--agent-id", default=None)
    p.add_argument("--order-type", default="market")
    p.add_argument("--requested-notional-usd", type=float, default=None)
    p.add_argument("--requested-leverage", type=int, default=None)
    p.add_argument("--intent-metadata", default=None,
                   help="JSON dict, stamped on the intent.metadata")


def _add_market_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--bid", type=float, default=None)
    p.add_argument("--ask", type=float, default=None)
    p.add_argument("--contract-size", type=float, default=1.0)
    p.add_argument("--lot-step", type=float, default=0.0)
    p.add_argument("--min-notional-usd", type=float, default=0.0)
    p.add_argument("--taker-fee-bps", type=float, default=0.0)
    p.add_argument("--maker-fee-bps", type=float, default=0.0)
    p.add_argument("--spread-bps", type=float, default=0.0)
    p.add_argument("--slippage-bps", type=float, default=0.0)
    p.add_argument("--overnight-bps-per-day", type=float, default=0.0)


def _add_strategy_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--risk-per-trade-pct", type=float, default=0.01)
    p.add_argument("--max-notional-pct", type=float, default=1.0)
    p.add_argument("--sl-distance-pct", type=float, default=None)
    p.add_argument("--tp-distance-pct", type=float, default=None)
    p.add_argument("--prefer-maker", action="store_true")


def _add_account_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--account-id", required=True)
    p.add_argument("--currency", default="USD")


def _build_caps_list(p: argparse.ArgumentParser) -> None:
    _add_dsn(p)
    p.add_argument("--company", required=True)
    p.add_argument("--active-only", action="store_true")


def _build_caps_get(p: argparse.ArgumentParser) -> None:
    _add_dsn(p)
    p.add_argument("--company", required=True)
    p.add_argument("--scope-kind", required=True,
                   choices=("company", "strategy", "agent", "venue"))
    p.add_argument("--scope-id", required=True)


def _build_caps_upsert(p: argparse.ArgumentParser) -> None:
    _add_dsn(p)
    p.add_argument("--company", required=True)
    p.add_argument("--scope-kind", default="company",
                   choices=("company", "strategy", "agent", "venue"))
    p.add_argument("--scope-id", default="global")
    p.add_argument("--max-notional-usd", type=float, default=None)
    p.add_argument("--max-leverage", type=int, default=None)
    p.add_argument("--max-daily-loss-usd", type=float, default=None)
    p.add_argument("--max-open-positions", type=int, default=None)
    p.add_argument("--allow-venues", default=None)
    p.add_argument("--deny-venues", default=None)
    p.add_argument("--allow-symbols", default=None)
    p.add_argument("--deny-symbols", default=None)
    p.add_argument("--allow-directions", default=None)
    p.add_argument("--allow-order-types", default=None)
    p.add_argument("--inactive", action="store_true")
    p.add_argument("--notes", default="")
    p.add_argument("--metadata", default=None)


def _build_caps_delete(p: argparse.ArgumentParser) -> None:
    _add_dsn(p)
    p.add_argument("--company", required=True)
    p.add_argument("--scope-kind", required=True,
                   choices=("company", "strategy", "agent", "venue"))
    p.add_argument("--scope-id", required=True)


def _build_caps_seed_default(p: argparse.ArgumentParser) -> None:
    _add_dsn(p)
    p.add_argument("--company", required=True)


def _build_balances_record(p: argparse.ArgumentParser) -> None:
    _add_dsn(p)
    p.add_argument("--company", required=True)
    p.add_argument("--exchange", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--balance", type=float, required=True)
    p.add_argument("--equity", type=float, default=None)
    p.add_argument("--margin-used", type=float, default=None)
    p.add_argument("--free-margin", type=float, default=None)
    p.add_argument("--unrealised-pnl", type=float, default=None)
    p.add_argument("--account-type", default="demo",
                   choices=("demo", "live", "paper"))
    p.add_argument("--currency", default="USD")
    p.add_argument("--source", default="cli")
    p.add_argument("--metadata", default=None)


def _build_balances_latest(p: argparse.ArgumentParser) -> None:
    _add_dsn(p)
    p.add_argument("--company", required=True)


def _build_balances_one(p: argparse.ArgumentParser) -> None:
    _add_dsn(p)
    p.add_argument("--company", required=True)
    p.add_argument("--exchange", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--currency", default="USD")


def _build_balances_history(p: argparse.ArgumentParser) -> None:
    _add_dsn(p)
    p.add_argument("--company", required=True)
    p.add_argument("--exchange", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--currency", default="USD")
    p.add_argument("--limit", type=int, default=50)


def _build_evaluate(p: argparse.ArgumentParser) -> None:
    _add_dsn(p)
    _add_intent_flags(p)
    _add_account_flags(p)
    _add_market_flags(p)
    _add_strategy_flags(p)
    p.add_argument("--dry-run", action="store_true",
                   help="do not persist the decision row")


def _build_pure_size(p: argparse.ArgumentParser) -> None:
    _add_intent_flags(p)
    _add_account_flags(p)
    _add_market_flags(p)
    _add_strategy_flags(p)
    p.add_argument("--balance", type=float, required=True)
    p.add_argument("--equity", type=float, default=None)
    p.add_argument("--margin-used", type=float, default=None)
    p.add_argument("--free-margin", type=float, default=None)
    p.add_argument("--cap-max-notional-usd", type=float, default=None)
    p.add_argument("--cap-max-leverage", type=int, default=None)


def _build_apply_migration(p: argparse.ArgumentParser) -> None:
    del p


def _build_migration_sql(p: argparse.ArgumentParser) -> None:
    del p


def main() -> int:
    subs = [
        Subcommand("apply-migration", "Print migration path + psql example.",
                   cmd_apply_migration, build=_build_apply_migration),
        Subcommand("migration-sql", "Print the Phase 25 migration SQL.",
                   cmd_migration_sql, build=_build_migration_sql),
        Subcommand("caps-list", "List capabilities for a company.",
                   cmd_caps_list, build=_build_caps_list),
        Subcommand("caps-get", "Get one capability.",
                   cmd_caps_get, build=_build_caps_get),
        Subcommand("caps-upsert", "Create or update a capability.",
                   cmd_caps_upsert, build=_build_caps_upsert),
        Subcommand("caps-delete", "Delete a capability.",
                   cmd_caps_delete, build=_build_caps_delete),
        Subcommand("caps-seed-default", "Insert the safe default capability.",
                   cmd_caps_seed_default, build=_build_caps_seed_default),
        Subcommand("balances-record", "Record a balance snapshot.",
                   cmd_balances_record, build=_build_balances_record),
        Subcommand("balances-latest", "Latest balance per account for a company.",
                   cmd_balances_latest, build=_build_balances_latest),
        Subcommand("balances-one", "Latest balance for one account.",
                   cmd_balances_one, build=_build_balances_one),
        Subcommand("balances-history", "Recent snapshots for one account.",
                   cmd_balances_history, build=_build_balances_history),
        Subcommand("evaluate", "Treasury.evaluate against live DB.",
                   cmd_evaluate, build=_build_evaluate),
        Subcommand("pure-size", "Pure sizer — no DB.",
                   cmd_pure_size, build=_build_pure_size),
    ]
    parser = build_parser(
        "treasury_cli",
        "Operator CLI for the Tickles Banker + Treasury + Capabilities (Phase 25).",
        subs,
    )
    return run(parser)


if __name__ == "__main__":
    raise SystemExit(main())
