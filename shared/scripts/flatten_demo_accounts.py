"""
flatten_demo_accounts.py — close all open positions + cancel all resting orders
on the demo exchange accounts, freeing up margin.

WHY THIS EXISTS (2026-05-29, Phase 1):
The demo bridge used to size every mirrored order at the full $1000 wallet
notional. Because it fires for every signal on every account, those orders
stacked until the demo accounts ran completely out of margin (e.g. a BNB
position at 5120 notional, negative free margin). Once an account is maxed out,
EVERY new order is rejected by Bybit with "ab not enough for new order"
(retCode 110007). The sizing bug is now fixed in demo_bridge.py, but the
already-open junk positions still need to be cleared once so the accounts go
back to ~full free margin.

This is a one-shot maintenance utility — it is NOT a daemon.

USAGE:
    # See what WOULD be closed (default, safe):
    python3 -m shared.scripts.flatten_demo_accounts

    # Actually close everything:
    python3 -m shared.scripts.flatten_demo_accounts --apply

    # Limit to one account:
    python3 -m shared.scripts.flatten_demo_accounts --apply --account TicklesCo3
"""
import argparse
import asyncio
import sys

sys.path.insert(0, "/opt/tickles")
from shared.execution.ccxt_adapter import CcxtExecutionAdapter
from shared.utils.db import DatabasePool


async def _active_demo_accounts(account_filter=None):
    pool = await DatabasePool.get_instance()
    rows = await pool.fetch_all(
        "SELECT id, exchange, account_name FROM public.exchange_accounts "
        "WHERE account_type = 'demo' AND is_active = TRUE ORDER BY id"
    )
    out = []
    for r in rows:
        if account_filter and r["account_name"] != account_filter:
            continue
        out.append((r["id"], r["exchange"], r["account_name"]))
    return out


async def flatten(apply: bool, account_filter=None):
    adapter = CcxtExecutionAdapter(demo_trading=True)
    accounts = await _active_demo_accounts(account_filter)
    if not accounts:
        print("No matching active demo accounts.")
        return

    for acct_id, exchange, account_name in accounts:
        print(f"\n=== {exchange}/{account_name} (id {acct_id}) ===")
        try:
            client = adapter._get_client(exchange, account_name)
        except Exception as exc:
            print(f"  cannot connect: {exc}")
            continue

        # Cancel all resting orders first.
        try:
            orders = await asyncio.to_thread(client.fetch_open_orders)
            print(f"  open orders: {len(orders)}")
            for o in orders:
                if apply:
                    try:
                        await asyncio.to_thread(
                            client.cancel_order, o["id"], o["symbol"])
                        print(f"    cancelled order {o['id']} ({o['symbol']})")
                    except Exception as exc:
                        print(f"    cancel {o['id']} failed: {exc}")
                else:
                    print(f"    WOULD cancel order {o['id']} ({o['symbol']})")
        except Exception as exc:
            print(f"  fetch_open_orders failed: {exc}")

        # Close every open position with a reduce-only market order.
        try:
            positions = await asyncio.to_thread(client.fetch_positions)
        except Exception as exc:
            print(f"  fetch_positions failed: {exc}")
            continue
        open_pos = [p for p in positions if float(p.get("contracts") or 0) != 0]
        print(f"  open positions: {len(open_pos)}")
        for p in open_pos:
            sym = p["symbol"]
            contracts = abs(float(p.get("contracts") or 0))
            # long position → sell to close; short → buy to close
            pos_side = (p.get("side") or "").lower()
            close_side = "sell" if pos_side == "long" else "buy"
            label = f"{sym} {pos_side} {contracts} notional={p.get('notional')}"
            if apply:
                try:
                    await asyncio.to_thread(
                        client.create_order,
                        sym, "market", close_side, contracts,
                        None, {"reduceOnly": True})
                    print(f"    CLOSED {label}")
                except Exception as exc:
                    print(f"    close {label} failed: {exc}")
            else:
                print(f"    WOULD close {label} via {close_side}")

    print("\nDone." if apply else "\nDRY RUN — nothing changed. Re-run with --apply to execute.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually close/cancel (default: dry run)")
    ap.add_argument("--account", default=None, help="limit to one account_name")
    args = ap.parse_args()
    asyncio.run(flatten(args.apply, args.account))


if __name__ == "__main__":
    main()
