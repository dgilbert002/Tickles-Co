"""
Banker service — manages agent wallets, balances, and fund allocation.
Paper-only for now. Live/demo accounts later.

API:
  get_balance(agent_id, company_id) → {balance, equity, free_margin}
  debit(agent_id, amount, reason) → new balance
  credit(agent_id, amount, reason) → new balance
  seed_wallet(agent_id, company_id, starting_usd) → wallet
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from shared.utils.db import get_shared_pool

logger = logging.getLogger("banker")

BANKER_SOURCE = "banker_service"


async def get_balance(agent_id: str, company_id: str = "jarvais") -> Optional[dict]:
    """Get current balance for an agent's paper wallet."""
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT balance, equity, margin_used, free_margin, unrealised_pnl, account_id_external
            FROM banker_balances
            WHERE account_id_external LIKE $1 AND company_id = $2
            ORDER BY ts DESC LIMIT 1
        """, f"%{agent_id}%", company_id)
    if not row:
        return None
    return {
        "balance": float(row["balance"]),
        "equity": float(row["equity"]),
        "margin_used": float(row["margin_used"]),
        "free_margin": float(row["free_margin"]),
        "unrealised_pnl": float(row["unrealised_pnl"] or 0),
        "account_id": row["account_id_external"],
    }


async def update_balance(agent_id: str, company_id: str, new_balance: float,
                         new_equity: float = None, margin_used: float = 0,
                         unrealised_pnl: float = 0):
    """Update banker balance for an agent. Called each tick by copy-trade daemon."""
    pool = await get_shared_pool()
    eq = new_equity if new_equity is not None else new_balance
    free = eq - margin_used
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE banker_balances
            SET balance = $1, equity = $2, margin_used = $3, free_margin = $4,
                unrealised_pnl = $5, ts = NOW()
            WHERE account_id_external LIKE $6 AND company_id = $7
        """, Decimal(str(new_balance)), Decimal(str(eq)), Decimal(str(margin_used)),
            Decimal(str(free)), Decimal(str(unrealised_pnl)),
            f"%{agent_id}%", company_id)


async def seed_wallet(agent_id: str, company_id: str, exchange: str,
                      starting_usd: float, currency: str = "USD",
                      contest_id: str = None) -> dict:
    """Create a new paper wallet and seed banker balance."""
    account_id = f"paper_{company_id}_{agent_id}_{exchange}"
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        # Paper wallet
        await conn.execute("""
            INSERT INTO paper_wallets (company_id, agent_id, exchange, account_id_external,
                starting_balance_usd, currency, account_type, contest_id, is_active, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, 'paper', $7, true, NOW())
            ON CONFLICT DO NOTHING
        """, company_id, agent_id, exchange, account_id, Decimal(str(starting_usd)),
            currency, contest_id)

        # Banker balance
        await conn.execute("""
            INSERT INTO banker_balances (company_id, exchange, account_id_external,
                account_type, currency, balance, equity, margin_used, free_margin,
                source, ts)
            VALUES ($1, $2, $3, 'paper', $4, $5, $5, 0, $5, 'wallet_seed', NOW())
            ON CONFLICT DO NOTHING
        """, company_id, exchange, account_id, currency,
            Decimal(str(starting_usd)))

    return {"agent_id": agent_id, "account_id": account_id, "balance": starting_usd}


async def list_wallets(company_id: str = None, contest_id: str = None) -> list:
    """List all paper wallets, optionally filtered."""
    pool = await get_shared_pool()
    async with pool.acquire() as conn:
        query = """
            SELECT pw.agent_id, pw.company_id, pw.starting_balance_usd, pw.contest_id,
                   pw.is_active, pw.created_at,
                   COALESCE(bb.balance, pw.starting_balance_usd) as current_balance,
                   COALESCE(bb.equity, pw.starting_balance_usd) as current_equity,
                   COALESCE(bb.unrealised_pnl, 0) as unrealised_pnl
            FROM paper_wallets pw
            LEFT JOIN LATERAL (
                SELECT balance, equity, unrealised_pnl FROM banker_balances
                WHERE account_id_external LIKE '%' || pw.agent_id || '%'
                ORDER BY ts DESC LIMIT 1
            ) bb ON true
            WHERE pw.is_active = true
        """
        params = []
        if company_id:
            query += " AND pw.company_id = $1"
            params.append(company_id)
        if contest_id:
            query += f" AND pw.contest_id = ${len(params)+1}"
            params.append(contest_id)
        query += " ORDER BY pw.created_at DESC"

        rows = await conn.fetch(query, *params)
    return [{
        "agent_id": r["agent_id"], "company_id": r["company_id"],
        "starting_usd": float(r["starting_balance_usd"]),
        "current_balance": float(r["current_balance"]),
        "current_equity": float(r["current_equity"]),
        "unrealised_pnl": float(r["unrealised_pnl"]),
        "contest_id": r["contest_id"],
        "is_active": r["is_active"],
    } for r in rows]
