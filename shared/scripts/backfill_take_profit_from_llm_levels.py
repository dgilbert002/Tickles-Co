"""
Script: backfill_take_profit_from_llm_levels.py
Purpose: Restore `take_profit_1` on historical signal_interpretations rows where
         the LLM correctly extracted a singular `take_profit` value into
         `llm_levels` but it was silently dropped by the previous flattener
         (which only looked for take_profit_1..take_profit_6, not the singular
         `take_profit` key the prompt actually asks for).
Location: /opt/tickles/shared/scripts/backfill_take_profit_from_llm_levels.py

This is a one-time data-recovery utility. It DOES NOT call the LLM again — it
only re-reads the raw `llm_levels` JSONB column already stored in the DB and
copies the singular `take_profit` value into `take_profit_1` when:
  * `take_profit_1 IS NULL`, AND
  * `llm_levels->>'take_profit' IS NOT NULL`, AND
  * the value parses as a positive number.

Also handles tracked_positions: if the position still references the
interpretation and has a NULL take_profit_1, sync the recovered value down.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from shared.utils.db import DatabasePool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("backfill_take_profit_from_llm_levels")


def _to_pos_float(raw):
    """Parse a numeric value (str/int/float) into a positive float, or None.

    Args:
        raw: the value pulled from JSONB.

    Returns:
        Positive float or None when missing/zero/invalid.
    """
    if raw is None:
        return None
    try:
        v = float(str(raw).strip().replace(",", ""))
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return None


async def backfill(apply: bool) -> int:
    """Backfill take_profit_1 from llm_levels.take_profit.

    Args:
        apply: when False, only print what would change.

    Returns:
        Number of interpretations updated (or that would be updated in dry-run).
    """
    logger.info("backfill(apply=%s) - starting", apply)
    pool = DatabasePool()
    await pool.initialize()

    rows = await pool.fetch_all(
        """
        SELECT id, llm_levels->>'take_profit' AS llm_tp,
               entry_price, stop_loss
        FROM public.signal_interpretations
        WHERE take_profit_1 IS NULL
          AND llm_levels IS NOT NULL
          AND (llm_levels ? 'take_profit')
          AND llm_levels->>'take_profit' IS NOT NULL
        """
    )
    logger.info("backfill() - found %d candidate rows", len(rows))

    updated = 0
    for r in rows:
        tp = _to_pos_float(r["llm_tp"])
        if tp is None:
            continue

        logger.info(
            "backfill() - interp_id=%d entry=%s sl=%s recovered_tp1=%s",
            r["id"], r["entry_price"], r["stop_loss"], tp,
        )

        if apply:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE public.signal_interpretations SET take_profit_1 = $1 WHERE id = $2",
                    tp, r["id"],
                )
                # Mirror down to any tracked_position that quoted NULL
                await conn.execute(
                    """
                    UPDATE public.tracked_positions
                       SET take_profit_1 = $1
                     WHERE signal_interpretation_id = $2
                       AND take_profit_1 IS NULL
                    """,
                    tp, r["id"],
                )

        updated += 1

    logger.info("backfill() - done. updated=%d (apply=%s)", updated, apply)
    return updated


if __name__ == "__main__":
    apply_mode = "--apply" in sys.argv
    if not apply_mode:
        logger.info("DRY-RUN. Pass --apply to commit changes.")
    asyncio.run(backfill(apply=apply_mode))
