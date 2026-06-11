#!/usr/bin/env python3
"""Insert chart_analysis v9 — path/projection boxes are NOT trader trades."""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/opt/tickles")

from shared.utils.db import get_shared_pool
from shared.intelligence.payload_store import compute_prompt_hash

_V9_ADDON = """
PATH / PROJECTION BOXES — NOT TRADER TRADES (critical):
  - Some traders draw TradingView Long/Short position tools to show a PRICE PATH
    (e.g. orange/zigzag arrow: rally up INTO a short entry, then drop to TP).
    That inbound leg is NOT a separate trade the human is calling — it is setup
    context for the real trade (usually the short at the top of the path).
  - Do NOT emit a `trader_trades` row for a box whose role is only "how price
    gets to" another box. Signs of a path/projection box (skip as trader_trade):
      * An orange or hand-drawn arrow/zigzag connects it to another box.
      * Its profit zone points toward the ENTRY of another position box, not a
        standalone take-profit the trader labelled.
      * It sits below/at support while 2+ SHORT boxes sit above targeting the
        same deep TP — the lower box is the dip-before-shorts scenario, not a long call.
      * The Discord/Telegram text names levels but never says long/buy/enter long.
  - When 2+ clear SHORT position boxes exist with proper short geometry
    (profit below, stop above) and the only "long" box is the path into them,
    `trader_trades` = those SHORT boxes ONLY. Zero long rows in trader_trades.
  - You MAY still discuss the path in `trader_market_view` / `reasoning`. If YOU
    independently like the dip level, put it in `chart_hacker_trades` with
    evidence "inferred" — never in `trader_trades` unless the human explicitly
    marked it as their trade (text says long/buy OR a standalone long box with
    its own TP, not feeding into a short entry).
  - Re-read: N independent TRADES the human is calling -> N `trader_trades`.
    Path illustrations between them do not increase N.
"""

_BODY_V9 = """Extract the trade setup(s) the trader is actually calling from this Discord chart
and their message — NOT every position tool on the chart. Path/projection boxes
(orange routes into a short entry) are context only — do NOT count them as
`trader_trades`. Count only independent short/long boxes the human is trading.
No qualifying box and no explicit text call -> `trader_trades` = [].

Use semantic role and box geometry. Emit per-trade `confidence`, `rationale`,
and `evidence`. Partial setups allowed at lower confidence.

Trader's message / news context: {context}

{recall_context}

Respond ONLY with the JSON object specified in your system instructions.
"""

_TELEGRAM_BODY_V9 = _BODY_V9.replace("Discord", "Telegram")

VERSIONS = (
    ("2026.05.30-discord-semantic-v8", "2026.05.30-discord-semantic-v9", _BODY_V9),
    ("2026.05.30-telegram-rose-semantic-v8", "2026.05.30-telegram-rose-semantic-v9", _TELEGRAM_BODY_V9),
)


async def main() -> None:
    pool = await get_shared_pool()
    for src_ver, dst_ver, body in VERSIONS:
        row = await pool.fetch_one(
            "SELECT system, taxonomy_rule, model_hint, name, source "
            "FROM prompt_versions WHERE name = 'chart_analysis' AND version = $1",
            (src_ver,),
        )
        if not row:
            print(f"SKIP missing source {src_ver}")
            continue
        system = (row["system"] or "") + _V9_ADDON
        phash = compute_prompt_hash(system, body)
        exists = await pool.fetch_one(
            "SELECT id FROM prompt_versions WHERE name = 'chart_analysis' AND version = $1",
            (dst_ver,),
        )
        if exists:
            print(f"EXISTS {dst_ver} id={exists['id']}")
            continue
        new_id = await pool.fetch_val(
            """
            INSERT INTO prompt_versions (
                name, version, prompt_hash, system, body,
                taxonomy_rule, model_hint, created_by, notes, source
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            RETURNING id
            """,
            (
                row["name"],
                dst_ver,
                phash,
                system,
                body,
                row["taxonomy_rule"],
                row["model_hint"],
                "cursor-agent",
                "v9: path/projection boxes are not trader_trades; shorts-only when path box feeds shorts",
                row["source"] or "db",
            ),
        )
        print(f"INSERTED {dst_ver} id={new_id} hash={phash}")


if __name__ == "__main__":
    asyncio.run(main())
