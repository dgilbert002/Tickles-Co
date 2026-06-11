"""
Self-learning symbol resolver.

When interpretation_service encounters an unknown symbol:
  1. Check symbol_mappings table → if found, route directly
  2. If not found, insert into unresolved_symbols (status='pending')
  3. Skip trade — will be resolved by nightly cron

Cron (every 12h):
  1. Query unresolved_symbols WHERE status='pending'
  2. Build prompt with ALL active instruments from all exchanges
  3. Ask cheap LLM (Gemini Flash) to match each unknown
  4. Store results in symbol_mappings
  5. Mark unresolved_symbols as 'resolved' or 'unresolvable'

Path: /opt/tickles/shared/utils/symbol_learner.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from shared.utils.db import DatabasePool

logger = logging.getLogger("tickles.intelligence.symbol_learner")

PRIORITY_EXCHANGES = ("bybit", "blofin", "bitget", "capital.com")


@dataclass
class InstrumentSummary:
    """Compact instrument representation for LLM prompt."""
    exchange: str
    base: str
    symbol: str        # exchange_symbol from unified_instruments
    asset_type: str


async def _get_all_instruments(pool: DatabasePool) -> List[InstrumentSummary]:
    """Get ALL active instruments from ALL exchanges (compact for prompt)."""
    rows = await pool.fetch_all(
        """
        SELECT exchange, base_currency, exchange_symbol, asset_type
        FROM unified_instruments
        WHERE is_active = TRUE
        ORDER BY 
          CASE exchange WHEN 'bybit' THEN 1 WHEN 'blofin' THEN 2 
                        WHEN 'bitget' THEN 3 WHEN 'capital.com' THEN 4 ELSE 99 END,
          base_currency
        """
    )
    return [
        InstrumentSummary(
            exchange=r["exchange"],
            base=r["base_currency"],
            symbol=r["exchange_symbol"],
            asset_type=r["asset_type"],
        )
        for r in rows
    ]


def _build_instruments_text(instruments: List[InstrumentSummary]) -> str:
    """Build a compact text list of all instruments grouped by exchange."""
    by_exchange: Dict[str, List[str]] = {}
    for inst in instruments:
        by_exchange.setdefault(inst.exchange, []).append(
            f"{inst.symbol} ({inst.asset_type})"
        )
    
    parts = []
    for ex in PRIORITY_EXCHANGES:
        syms = by_exchange.get(ex, [])
        if syms:
            parts.append(f"  {ex} ({len(syms)} instruments): {', '.join(syms[:500])}")
            if len(syms) > 500:
                parts[-1] += f" ... +{len(syms)-500} more"
    
    return "\n".join(parts)


async def _llm_resolve_symbols(
    unknowns: List[Tuple[str, str]],
    instruments_text: str,
) -> Dict[str, Optional[dict]]:
    """Ask the LLM to map unknown symbols via the gateway (text_extract slot)."""
    from shared.intelligence.gateway_config import chat_completion, resolve_slot_gateway

    unknown_list = "\n".join(
        f"  {i+1}. raw='{raw}' cleaned='{base}'"
        for i, (raw, base) in enumerate(unknowns)
    )

    prompt = f"""You are a financial instrument resolver. Map unknown trading symbols to known exchange instruments.

UNKNOWN SYMBOLS TO RESOLVE:
{unknown_list}

AVAILABLE INSTRUMENTS (by exchange, priority order):
{instruments_text}

RULES:
1. For each unknown, find the BEST matching instrument across ALL exchanges.
2. Priority: bybit > blofin > bitget > capital.com for crypto tokens.
   capital.com is for forex/indices/commodities (EUR/USD, US100, GOLD, OIL, etc.).
3. TradingView aliases: BITTENSOR=TAO, ZCASH=ZEC, BEAMX=BEAM, 1000PEPE=PEPE,
   KASPA=KAS, FANTOM=S, MATIC=POL, BITCOIN=BTC, ETHEREUM=ETH.
4. Forex pairs (EUR/USD, USD/JPY etc.) → capital.com.
5. Index futures (NQ→US100, ES→US500, YM→US30, MNQ→US100, MES→US500) → capital.com.
6. Commodities (XAU/USD, GOLD, OIL, WTI, SILVER, XAG) → check both crypto (tokenized perp)
   AND capital.com (CFD). Prefer crypto perp if available, else capital.com.
7. If genuinely no match exists across any exchange, return null.
8. Return JSON array: [{{"raw":"...","cleaned":"...","exchange":"...","symbol":"...","base":"...","asset_type":"..."}}]
   Only include entries that resolved. Skip unresolvable ones.
   Do NOT invent instruments. Output ONLY the JSON array, no explanation."""

    try:
        cfg, model = await resolve_slot_gateway("text_extract")
        response = await chat_completion(
            cfg=cfg,
            model=model,
            system_prompt="You resolve trading symbols to exchange instruments. Output JSON only. If there are many instruments, just provide the most likely matches.",
            user_text=prompt,
            max_tokens=8192,
            operation="symbol_resolver",
            agent_id="symbol_learner",
        )
        content = response.get("content", "")
        content = re.sub(r"```(?:json)?\s*", "", content).strip()
        content = re.sub(r"```\s*$", "", content).strip()
        result = json.loads(content)
        items = result if isinstance(result, list) else result.get("matches", [])
        resolved: Dict[str, Optional[dict]] = {s: None for s, _ in unknowns}
        for item in items:
            raw = item.get("raw", "")
            if raw in resolved:
                resolved[raw] = {
                    "exchange": item.get("exchange", ""),
                    "symbol": item.get("symbol", ""),
                    "base": item.get("base", ""),
                    "asset_type": item.get("asset_type", "unknown"),
                    "llm_response": content[:2000],
                    "llm_model": model,
                }
        logger.info(
            "symbol_learner: resolved %d/%d unknowns via %s",
            sum(1 for v in resolved.values() if v),
            len(unknowns),
            model,
        )
        return resolved
    except Exception as exc:
        logger.error("symbol_learner: gateway LLM call failed: %s", exc)
        return {s: None for s, _ in unknowns}


async def resolve_pending(pool: DatabasePool) -> int:
    """Resolve all pending unknown symbols. Returns count resolved."""
    
    # Get pending unknowns
    rows = await pool.fetch_all(
        """
        SELECT raw_symbol, cleaned_base
        FROM unresolved_symbols
        WHERE status = 'pending'
        ORDER BY first_seen_at
        LIMIT 50  -- batch to keep prompt manageable
        """
    )
    
    if not rows:
        logger.info("symbol_learner: no pending unknowns")
        return 0
    
    unknowns = [(r["raw_symbol"], r["cleaned_base"] or "") for r in rows]
    logger.info("symbol_learner: resolving %d unknowns...", len(unknowns))
    
    # Get all instruments
    instruments = await _get_all_instruments(pool)
    instruments_text = _build_instruments_text(instruments)
    
    # LLM resolution
    resolved = await _llm_resolve_symbols(unknowns, instruments_text)
    
    # Store results
    resolved_count = 0
    # Guard: if LLM call completely failed (all None), leave all as pending.
    # Only mark individual symbols unresolvable if the LLM explicitly said so.
    all_failed = all(v is None for v in resolved.values())
    if all_failed:
        logger.warning(
            "symbol_learner: LLM resolution failed for all %d unknowns — "
            "leaving as pending for next cycle", len(unknowns),
        )
        return 0
    
    for raw_symbol, mapping in resolved.items():
        if mapping and mapping.get("symbol") and mapping.get("exchange"):
            # Insert into symbol_mappings
            await pool.execute(
                """
                INSERT INTO symbol_mappings 
                  (raw_symbol, cleaned_base, resolved_base, resolved_exchange,
                   resolved_symbol, asset_type, priority, resolution_method,
                   llm_model, llm_response, resolved_at)
                VALUES ($1, $2, $3, $4, $5, $6, 1, 'llm_auto', $7, $8, NOW())
                ON CONFLICT (raw_symbol, resolved_exchange, priority) DO NOTHING
                """,
                (
                    raw_symbol,
                    mapping.get("cleaned_base", ""),
                    mapping["base"],
                    mapping["exchange"],
                    mapping["symbol"],
                    mapping["asset_type"],
                    mapping.get("llm_model", "text_extract"),
                    mapping.get("llm_response", "")[:2000],
                ),
            )
            
            # Mark as resolved
            await pool.execute(
                """
                UPDATE unresolved_symbols
                SET status = 'resolved', resolved_at = NOW()
                WHERE raw_symbol = $1
                """,
                (raw_symbol,),
            )
            resolved_count += 1
            logger.info(
                "symbol_learner: %s → %s/%s (%s)",
                raw_symbol, mapping["exchange"], mapping["symbol"], mapping["asset_type"],
            )
        else:
            # Mark as unresolvable
            await pool.execute(
                """
                UPDATE unresolved_symbols
                SET status = 'unresolvable', resolved_at = NOW(),
                    notes = 'LLM could not match to any exchange instrument'
                WHERE raw_symbol = $1
                """,
                (raw_symbol,),
            )
            logger.info("symbol_learner: %s → UNRESOLVABLE", raw_symbol)
    
    return resolved_count


async def lookup_mapping(pool: DatabasePool, raw_symbol: str) -> Optional[dict]:
    """Check if a symbol has a known mapping. Returns highest-priority match."""
    row = await pool.fetch_one(
        """
        SELECT resolved_base, resolved_exchange, resolved_symbol, asset_type
        FROM symbol_mappings
        WHERE raw_symbol = $1 AND is_active = TRUE
        ORDER BY priority
        LIMIT 1
        """,
        (raw_symbol.upper().strip(),),
    )
    return dict(row) if row else None


async def register_unknown(pool: DatabasePool, raw_symbol: str, cleaned_base: str = ""):
    """Register an unknown symbol for future resolution. No-op if already seen."""
    await pool.execute(
        """
        INSERT INTO unresolved_symbols (raw_symbol, cleaned_base, status)
        VALUES ($1, $2, 'pending')
        ON CONFLICT (raw_symbol) DO UPDATE SET
          last_seen_at = NOW(),
          seen_count = unresolved_symbols.seen_count + 1
        """,
        (raw_symbol.upper().strip(), cleaned_base or ""),
    )
