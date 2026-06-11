#!/usr/bin/env python3
"""Insert chart_analysis v8 prompts (multi-box) cloned from v7."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, "/opt/tickles")

from shared.utils.db import get_shared_pool
from shared.intelligence.payload_store import compute_prompt_hash

_V8_ADDON = """
MULTI-SETUP / MULTI-BOX (critical — read carefully):
  - COUNT every distinct TradingView Long/Short position box (or equivalent
    hand-drawn profit/stop zone pair) the HUMAN drew. Emit ONE object in
    `trader_trades` per box — direction may mix on one chart (long + short + long).
  - NO position box and NO explicit human level/direction commitment ->
    `trader_trades` MUST be []. Commentary, lone S/R lines, or trendlines alone
    do NOT create a trader trade. Those belong in chart_hacker_trades only if YOU
    infer a setup (evidence "inferred", confidence usually < 0.6).
  - Per box: direction from GEOMETRY — profit zone BELOW entry + stop ABOVE = short;
    profit ABOVE + stop BELOW = long. Profit zone is often LARGER than stop zone;
    reflect that in `risk_reward` when numeric labels are absent.
  - Each trade carries its own `symbol` and `timeframe` when readable (montages
    may differ per box). Never copy consensus levels across legs.
  - `chart_hacker_trades`: YOUR independent reads — may agree or disagree with
    each trader leg. Never duplicate a trader leg unless your read meaningfully
    differs in direction or entry (>0.3%% apart).
"""

_BODY_V8 = """Extract every explicit trade setup from this Discord chart screenshot and the trader's
message, following your system instructions. Count position boxes: N visible human-drawn
boxes -> up to N `trader_trades` (any direction mix). No box -> `trader_trades` = [].
Consider Fib zones, labelled lines, S/R + arrow, or explicit text. Use semantic role, not
a fixed colour. Emit per-trade `confidence`, `rationale`, and `evidence`. Partial setups
(entry + only a stop, or entry + only a target) are allowed at lower confidence.

Trader's message / news context: {context}

{recall_context}

Respond ONLY with the JSON object specified in your system instructions.
"""

_TELEGRAM_BODY_V8 = _BODY_V8.replace("Discord", "Telegram")

VERSIONS = (
    ("2026.05.29-discord-semantic-v7", "2026.05.30-discord-semantic-v8", _BODY_V8),
    ("2026.05.29-telegram-rose-semantic-v7", "2026.05.30-telegram-rose-semantic-v8", _TELEGRAM_BODY_V8),
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
        system = (row["system"] or "") + _V8_ADDON
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
                "Phase 5 multi-box: N boxes -> N trader_trades; CH dedup; no inferred trader legs",
                row["source"] or "db",
            ),
        )
        print(f"INSERTED {dst_ver} id={new_id} hash={phash}")


if __name__ == "__main__":
    asyncio.run(main())
