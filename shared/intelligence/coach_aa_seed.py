"""
Module: coach_aa_seed
Purpose: One-shot A/A seed for the Coach prompt-registry pipeline (PHASE_Y §11 Q5).
Location: /opt/tickles/shared/intelligence/coach_aa_seed.py

Why this exists
---------------
The Coach service can only promote a variant once two variants exist in
``prompt_versions`` AND ``prompt_assignments`` has rows for both. Until the
first promotion runs, the table never gains a second row — the chicken-and-egg
deadlock noted in PHASE_Y v1 issue (v).

This one-shot breaks the deadlock by registering two *identical* variants
(``v1`` and ``v2`` of ``chart_analysis``) and writing assignment rows for
``aa_seed_actor_*`` synthetic actors via :func:`coach_service.assign_variant`.
Because the prompt bodies are byte-identical, any measured "lift" between v1
and v2 must be statistical noise — that is the whole point of an A/A test.

Hard budget
-----------
Per PHASE_Y §11 Q5, the cumulative spend logged to ``api_cost_log`` with
``correlation_id='coach_aa_seed'`` must not exceed ``$5.00`` USD. The seed
terminates immediately on the first call that would breach the cap and emits
a row with ``role='aa_seed_budget_warn'`` so :class:`GuardActivityProvider`
can surface the warning on the Learning dashboard.

Modes
-----
The seed runs synthetic by default (no outbound API traffic; cost is the fake
``SYNTHETIC_PER_CALL_COST_USD``). Real-API mode (``--live``) is reserved for
operator-driven validation against a sandbox key and is intentionally NOT
implemented here — the synthetic mode is sufficient to seed the assignment
table and exercise the full guard pipeline.

CLI
---
    python -m shared.intelligence.coach_aa_seed \\
        --company rubicon \\
        --calls 20 \\
        --budget 5.00
"""

import argparse
import asyncio
import logging
import os
import sys
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional

import asyncpg

from shared.intelligence.coach_service import assign_variant
from shared.intelligence.prompt_registry import register_prompt
from shared.utils.api_cost_log import log_api_call
from shared.utils.companies import get_company_dsn

logger = logging.getLogger(__name__)


# Constants ------------------------------------------------------------------

PROMPT_NAME: str = os.environ.get("COACH_AA_SEED_PROMPT_NAME", "chart_analysis")
VARIANT_A: str = "aa_v1"
VARIANT_B: str = "aa_v2"
CORRELATION_ID: str = "coach_aa_seed"
DEFAULT_BUDGET_USD: Decimal = Decimal("5.00")
DEFAULT_CALLS: int = int(os.environ.get("COACH_AA_SEED_CALLS", "20"))
DEFAULT_COMPANY: str = os.environ.get("COACH_AA_SEED_COMPANY", "rubicon")
SYNTHETIC_PER_CALL_COST_USD: Decimal = Decimal("0.05")
SYNTHETIC_MODEL: str = "synthetic/aa-seed"
SYNTHETIC_PROVIDER: str = "synthetic"
ROLE_NORMAL: str = "aa_seed_call"
ROLE_BUDGET_WARN: str = "aa_seed_budget_warn"
ACTOR_PREFIX: str = "aa_seed_actor"
SEED_PROMPT_BODY: str = (
    "[A/A SEED PROMPT — identical body for v1 and v2]\n"
    "You are participating in a controlled identical-prompt A/A test. "
    "Respond with the single token: OK."
)


# Helpers --------------------------------------------------------------------

def _make_actor_id(index: int) -> str:
    """Return a synthetic actor identifier for assignment ``index``.

    Args:
        index: Zero-based call index.

    Returns:
        Stable string ``aa_seed_actor_<index>`` (no PII).
    """
    return f"{ACTOR_PREFIX}_{index:04d}"


async def _register_both_variants() -> tuple[str, str]:
    """Register the two identical seed variants in ``prompt_versions``.

    Returns:
        ``(hash_v1, hash_v2)`` — both will be the same hex string because
        the bodies are byte-identical, but we keep the API symmetric so a
        future operator can swap in differing bodies without restructuring.

    Raises:
        Exception: if either ``register_prompt`` call raises.
    """
    try:
        hash_a = await register_prompt(
            name=PROMPT_NAME,
            version=VARIANT_A,
            system=None,
            body=SEED_PROMPT_BODY,
            taxonomy_rule=None,
            model_hint=SYNTHETIC_MODEL,
            created_by="coach_aa_seed",
        )
        hash_b = await register_prompt(
            name=PROMPT_NAME,
            version=VARIANT_B,
            system=None,
            body=SEED_PROMPT_BODY,
            taxonomy_rule=None,
            model_hint=SYNTHETIC_MODEL,
            created_by="coach_aa_seed",
        )
        return hash_a, hash_b
    except Exception as exc:
        logger.error("coach_aa_seed: variant registration failed: %s", exc)
        raise


async def _record_assignment(
    conn: Any,
    actor_id: str,
    assignment_day: date,
    variant: str,
    prompt_hash: str,
) -> None:
    """Insert a row into the company's ``prompt_assignments`` table.

    Args:
        conn: An open ``asyncpg.Connection`` against the company DB. The
            caller is responsible for opening + closing it; sharing one
            connection across the whole seed run avoids the per-iteration
            handshake storm flagged in B-Y5-M1.
        actor_id: Synthetic seed-actor identifier.
        assignment_day: Date the assignment is anchored to.
        variant: Either :data:`VARIANT_A` or :data:`VARIANT_B`.
        prompt_hash: 16-char hash from :func:`register_prompt`.

    Raises:
        Exception: if the insert fails (caller logs + decides).
    """
    await conn.execute(
        """
        INSERT INTO prompt_assignments
          (actor_id, assignment_day, prompt_name, variant, prompt_hash)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (actor_id, assignment_day, prompt_name) DO NOTHING
        """,
        actor_id,
        assignment_day,
        PROMPT_NAME,
        variant,
        prompt_hash,
    )


async def _log_synthetic_call(
    company: str,
    actor_id: str,
    variant: str,
    cost_usd: Decimal,
    success: bool,
) -> None:
    """Log a synthetic A/A call to ``api_cost_log``.

    Args:
        company: Company short-name.
        actor_id: Synthetic actor identifier (used as ``agent_id``).
        variant: Variant string applied for this call.
        cost_usd: Per-call synthetic cost.
        success: Whether the synthetic call "succeeded".
    """
    try:
        await log_api_call(
            provider=SYNTHETIC_PROVIDER,
            model=SYNTHETIC_MODEL,
            role=ROLE_NORMAL,
            context=f"aa_seed/{variant}",
            tokens_in=0,
            tokens_out=0,
            cost_usd=cost_usd,
            latency_ms=0,
            company_id=company,
            operation=f"aa_seed_{variant}_{uuid.uuid4().hex}",
            agent_id=actor_id,
            correlation_id=CORRELATION_ID,
            success=success,
            http_status=200 if success else 500,
            extra={"variant": variant, "synthetic": True},
        )
    except Exception as exc:
        # Do not raise — telemetry must never break the seed.
        logger.warning("coach_aa_seed: api_cost_log write failed: %s", exc)


async def _log_budget_warning(
    company: str,
    spent_usd: Decimal,
    cap_usd: Decimal,
    calls_made: int,
) -> None:
    """Emit a single ``aa_seed_budget_warn`` row when the cap is hit.

    The :class:`GuardActivityProvider` can surface this row on the Learning
    dashboard via a future query against ``api_cost_log`` filtered by
    ``role='aa_seed_budget_warn'``.

    Args:
        company: Company short-name.
        spent_usd: Cumulative spend at the moment of the breach.
        cap_usd: Configured budget cap.
        calls_made: How many synthetic calls had been logged before tripping.
    """
    try:
        await log_api_call(
            provider=SYNTHETIC_PROVIDER,
            model=SYNTHETIC_MODEL,
            role=ROLE_BUDGET_WARN,
            context=(
                f"aa_seed cap=${cap_usd:.2f} spent=${spent_usd:.2f} "
                f"calls={calls_made}"
            ),
            tokens_in=0,
            tokens_out=0,
            cost_usd=Decimal("0"),
            latency_ms=0,
            company_id=company,
            operation=f"aa_seed_budget_warn_{uuid.uuid4().hex}",
            agent_id="coach_aa_seed",
            correlation_id=CORRELATION_ID,
            success=False,
            http_status=429,
            extra={
                "spent_usd": str(spent_usd),
                "cap_usd": str(cap_usd),
                "calls_made": calls_made,
                "synthetic": True,
            },
        )
    except Exception as exc:
        logger.warning("coach_aa_seed: budget-warn log failed: %s", exc)


async def run_seed(
    *,
    company: str,
    calls: int,
    budget_usd: Decimal,
    per_call_cost_usd: Decimal = SYNTHETIC_PER_CALL_COST_USD,
    today: Optional[date] = None,
) -> dict:
    """Execute the A/A seed run end-to-end.

    Workflow:

    1. Register ``aa_v1`` and ``aa_v2`` of ``chart_analysis`` (idempotent).
    2. For each of ``calls`` synthetic actors:
        a. Use :func:`assign_variant` to get a deterministic variant.
        b. Insert a ``prompt_assignments`` row for that actor.
        c. If the next call would exceed ``budget_usd``, emit
           :data:`ROLE_BUDGET_WARN` and stop.
        d. Otherwise log a synthetic per-call cost row and continue.

    Args:
        company: Company short-name whose ``prompt_assignments`` is written.
        calls: Maximum number of synthetic calls to make.
        budget_usd: Hard cap on cumulative ``cost_usd`` spent.
        per_call_cost_usd: Synthetic cost per call (default 5¢).
        today: Override for ``date.today()`` — used by tests.

    Returns:
        Summary dict with ``calls_made``, ``spent_usd``, ``budget_tripped``,
        ``v1_count``, ``v2_count`` and ``hash`` for audit.

    Raises:
        ValueError: if ``calls`` < 1 or ``budget_usd`` <= 0.
    """
    if calls < 1:
        raise ValueError(f"calls must be >= 1, got {calls}")
    if budget_usd <= 0:
        raise ValueError(f"budget_usd must be > 0, got {budget_usd}")

    if today is None:
        today = datetime.now(timezone.utc).date()

    hash_a, hash_b = await _register_both_variants()
    if hash_a != hash_b:
        # Defensive: seed prompts are identical so hashes MUST match. If they
        # diverge, abort before polluting the assignment table.
        raise RuntimeError(
            f"coach_aa_seed: identical bodies produced divergent hashes "
            f"({hash_a} vs {hash_b}) — refusing to proceed"
        )

    spent = Decimal("0")
    calls_made = 0
    v1_count = 0
    v2_count = 0
    budget_tripped = False
    variants: List[str] = [VARIANT_A, VARIANT_B]
    hash_by_variant = {VARIANT_A: hash_a, VARIANT_B: hash_b}

    # B-Y5-M1 fix: open the company connection ONCE and reuse across all
    # iterations. The previous implementation opened a fresh asyncpg
    # connection per call, which would storm Postgres ``max_connections``
    # at the default 100-call setting.
    dsn = await get_company_dsn(company)
    conn = await asyncpg.connect(dsn=dsn, timeout=10.0)
    try:
        for i in range(calls):
            # Pre-flight budget check — refuse to spend if next call would breach.
            if spent + per_call_cost_usd > budget_usd:
                budget_tripped = True
                await _log_budget_warning(
                    company=company,
                    spent_usd=spent,
                    cap_usd=budget_usd,
                    calls_made=calls_made,
                )
                logger.warning(
                    "coach_aa_seed: budget cap $%s reached after %d call(s); halting",
                    budget_usd,
                    calls_made,
                )
                break

            actor_id = _make_actor_id(i)
            variant = assign_variant(actor_id, today, PROMPT_NAME, variants)
            try:
                await _record_assignment(
                    conn=conn,
                    actor_id=actor_id,
                    assignment_day=today,
                    variant=variant,
                    prompt_hash=hash_by_variant[variant],
                )
            except Exception as exc:
                logger.error(
                    "coach_aa_seed: assignment write failed for %s: %s",
                    actor_id,
                    exc,
                )
                # Skip this iteration; do not bill.
                continue

            await _log_synthetic_call(
                company=company,
                actor_id=actor_id,
                variant=variant,
                cost_usd=per_call_cost_usd,
                success=True,
            )
            spent += per_call_cost_usd
            calls_made += 1
            if variant == VARIANT_A:
                v1_count += 1
            else:
                v2_count += 1
    finally:
        try:
            await conn.close()
        except Exception as close_exc:
            logger.warning(
                "coach_aa_seed: connection close failed: %s", close_exc
            )

    summary = {
        "calls_made": calls_made,
        "spent_usd": str(spent),
        "budget_tripped": budget_tripped,
        "v1_count": v1_count,
        "v2_count": v2_count,
        "prompt_hash": hash_a,
        "company": company,
        "prompt_name": PROMPT_NAME,
    }
    logger.info("coach_aa_seed: complete %s", summary)
    return summary


# CLI ------------------------------------------------------------------------

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments for the seed."""
    parser = argparse.ArgumentParser(
        prog="coach_aa_seed",
        description="One-shot A/A seed for the Coach prompt registry "
        "(PHASE_Y §11 Q5).",
    )
    parser.add_argument(
        "--company",
        default=DEFAULT_COMPANY,
        help=f"Company short-name (default: {DEFAULT_COMPANY!r}).",
    )
    parser.add_argument(
        "--calls",
        type=int,
        default=DEFAULT_CALLS,
        help=f"Max synthetic calls to make (default: {DEFAULT_CALLS}).",
    )
    parser.add_argument(
        "--budget",
        type=str,
        default=str(DEFAULT_BUDGET_USD),
        help=f"Hard cost cap in USD (default: {DEFAULT_BUDGET_USD}).",
    )
    parser.add_argument(
        "--per-call-cost",
        type=str,
        default=str(SYNTHETIC_PER_CALL_COST_USD),
        help=(
            f"Synthetic per-call cost in USD "
            f"(default: {SYNTHETIC_PER_CALL_COST_USD})."
        ),
    )
    return parser.parse_args(argv)


async def main(argv: Optional[List[str]] = None) -> int:
    """Module entry-point. Returns process exit-code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    args = _parse_args(argv)
    try:
        budget = Decimal(args.budget)
        per_call = Decimal(args.per_call_cost)
    except Exception as exc:
        logger.error("coach_aa_seed: invalid decimal argument: %s", exc)
        return 2

    try:
        summary = await run_seed(
            company=args.company,
            calls=args.calls,
            budget_usd=budget,
            per_call_cost_usd=per_call,
        )
    except Exception as exc:
        logger.exception("coach_aa_seed: aborted: %s", exc)
        return 1

    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
