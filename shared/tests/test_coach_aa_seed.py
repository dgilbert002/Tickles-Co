"""
Module: test_coach_aa_seed
Purpose: Verify the A/A seed honours its $5 hard cap and emits the budget-warn row (PHASE_Y §11 Q5).
Location: /opt/tickles/shared/tests/test_coach_aa_seed.py

Test surface:
    - ``run_seed`` happy path: every call within budget logs a normal row, no
      budget-warn row is emitted, and the summary reflects all calls made.
    - ``run_seed`` budget-tripped path: when the next call would breach the
      cap, exactly one ``aa_seed_budget_warn`` row is emitted, no further
      normal rows, and ``budget_tripped=True``.
    - Argument validation (``calls``/``budget_usd``).
    - Hash-divergence guard.
    - Failure of ``_record_assignment`` does not bill that iteration.
    - Variant balance: deterministic ``assign_variant`` output is honoured.
"""

from decimal import Decimal
from datetime import date
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from shared.intelligence import coach_aa_seed as seed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CallSpy:
    """Records keyword args of each invocation, returns ``None`` (or override)."""

    def __init__(self, return_value: Any = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._return_value = return_value

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        # Capture both positional and keyword as a single dict for assertions.
        captured = dict(kwargs)
        if args:
            captured["__args__"] = args
        self.calls.append(captured)
        return self._return_value

    def by_role(self, role: str) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c.get("role") == role]


def _patch_register_prompt(hash_value: str = "abc1234567890def"):
    """Patch :func:`register_prompt` to return ``hash_value`` for both calls."""

    async def _fake_register_prompt(**_kwargs: Any) -> str:
        return hash_value

    return patch.object(seed, "register_prompt", _fake_register_prompt)


def _patch_register_prompt_diverging():
    """Patch :func:`register_prompt` to return two different hashes."""

    seq = iter(["aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"])

    async def _fake_register_prompt(**_kwargs: Any) -> str:
        return next(seq)

    return patch.object(seed, "register_prompt", _fake_register_prompt)


class _StubConn:
    """Minimal asyncpg.Connection stand-in for ``run_seed`` mocking.

    Only :meth:`close` is exercised by the seed when ``_record_assignment``
    is patched out — which is the case for every test in this module.
    """

    def __init__(self) -> None:
        self.closed: bool = False

    async def close(self) -> None:
        self.closed = True

    async def execute(self, *_args: Any, **_kwargs: Any) -> str:
        # Defensive: should never run because tests always patch
        # ``_record_assignment``. Returning a benign asyncpg-style status
        # string keeps things sane if a future test forgets to patch.
        return "INSERT 0 1"


def _patch_db_connection():
    """Patch ``get_company_dsn`` + ``asyncpg.connect`` so ``run_seed`` does
    not attempt a real Postgres connection.

    B-Y5-M1 fix: ``run_seed`` now opens one ``asyncpg`` connection per run
    (was: one per iteration). The tests patch ``_record_assignment`` so the
    stub connection's ``execute`` is never reached, but the seed still
    calls ``get_company_dsn`` + ``asyncpg.connect`` + ``conn.close()``
    around the loop, so we mock all three.
    """

    stub = _StubConn()

    async def _fake_get_dsn(_company: str) -> str:
        return "postgres://stub/stub"

    async def _fake_connect(*_args: Any, **_kwargs: Any) -> _StubConn:
        return stub

    return patch.object(seed, "get_company_dsn", _fake_get_dsn), \
        patch.object(seed.asyncpg, "connect", _fake_connect)


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_seed_rejects_zero_calls() -> None:
    """``calls`` must be >= 1."""
    with pytest.raises(ValueError, match="calls must be >= 1"):
        await seed.run_seed(
            company="rubicon",
            calls=0,
            budget_usd=Decimal("5.00"),
        )


@pytest.mark.asyncio
async def test_run_seed_rejects_zero_budget() -> None:
    """``budget_usd`` must be > 0."""
    with pytest.raises(ValueError, match="budget_usd must be > 0"):
        await seed.run_seed(
            company="rubicon",
            calls=10,
            budget_usd=Decimal("0"),
        )


@pytest.mark.asyncio
async def test_run_seed_rejects_negative_budget() -> None:
    """Negative budget is rejected the same way as zero."""
    with pytest.raises(ValueError, match="budget_usd must be > 0"):
        await seed.run_seed(
            company="rubicon",
            calls=10,
            budget_usd=Decimal("-1.00"),
        )


# ---------------------------------------------------------------------------
# Hash divergence guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_seed_aborts_when_register_prompt_returns_divergent_hashes() -> None:
    """Identical bodies that hash differently must abort before assignments."""
    record_spy = _CallSpy()
    log_spy = _CallSpy()
    dsn_patch, connect_patch = _patch_db_connection()
    with _patch_register_prompt_diverging(), \
            dsn_patch, connect_patch, \
            patch.object(seed, "_record_assignment", record_spy), \
            patch.object(seed, "log_api_call", log_spy):
        with pytest.raises(RuntimeError, match="divergent hashes"):
            await seed.run_seed(
                company="rubicon",
                calls=5,
                budget_usd=Decimal("5.00"),
                per_call_cost_usd=Decimal("0.05"),
                today=date(2026, 5, 3),
            )

    assert record_spy.calls == []
    assert log_spy.calls == []


# ---------------------------------------------------------------------------
# Happy path — fully within budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_seed_happy_path_logs_one_row_per_call_and_no_budget_warn() -> None:
    """20 calls × $0.05 = $1.00 < $5.00 cap → 20 normal rows, 0 warn rows."""
    record_spy = _CallSpy()
    log_spy = _CallSpy()
    dsn_patch, connect_patch = _patch_db_connection()
    with _patch_register_prompt("0123456789abcdef"), \
            dsn_patch, connect_patch, \
            patch.object(seed, "_record_assignment", record_spy), \
            patch.object(seed, "log_api_call", log_spy):
        summary = await seed.run_seed(
            company="rubicon",
            calls=20,
            budget_usd=Decimal("5.00"),
            per_call_cost_usd=Decimal("0.05"),
            today=date(2026, 5, 3),
        )

    assert summary["calls_made"] == 20
    assert summary["budget_tripped"] is False
    assert summary["spent_usd"] == "1.00"
    assert summary["v1_count"] + summary["v2_count"] == 20
    assert summary["company"] == "rubicon"
    assert summary["prompt_name"] == seed.PROMPT_NAME
    assert summary["prompt_hash"] == "0123456789abcdef"

    # 20 prompt-assignment writes, all on the same day, with valid variants.
    # B-Y5-M1 fix: ``_record_assignment`` no longer takes ``company``;
    # the caller (``run_seed``) now opens one connection per run and
    # passes it as ``conn``.
    assert len(record_spy.calls) == 20
    for call in record_spy.calls:
        assert "conn" in call
        assert call["assignment_day"] == date(2026, 5, 3)
        assert call["variant"] in (seed.VARIANT_A, seed.VARIANT_B)
        assert call["prompt_hash"] == "0123456789abcdef"
        assert call["actor_id"].startswith(seed.ACTOR_PREFIX + "_")

    # 20 normal cost-log rows, 0 budget-warn rows.
    normal_rows = log_spy.by_role(seed.ROLE_NORMAL)
    warn_rows = log_spy.by_role(seed.ROLE_BUDGET_WARN)
    assert len(normal_rows) == 20
    assert len(warn_rows) == 0
    for row in normal_rows:
        assert row["correlation_id"] == seed.CORRELATION_ID
        assert row["cost_usd"] == Decimal("0.05")
        assert row["company_id"] == "rubicon"
        assert row["success"] is True
        assert row["http_status"] == 200
        assert row["extra"]["synthetic"] is True
        assert row["extra"]["variant"] in (seed.VARIANT_A, seed.VARIANT_B)


@pytest.mark.asyncio
async def test_run_seed_exact_cap_does_not_trip() -> None:
    """100 calls × $0.05 = $5.00 exactly hits the cap — must NOT trip on the last call.

    Pre-flight check is ``spent + per_call > budget`` (strict ``>``), so
    spending exactly the budget is permitted.
    """
    record_spy = _CallSpy()
    log_spy = _CallSpy()
    dsn_patch, connect_patch = _patch_db_connection()
    with _patch_register_prompt(), \
            dsn_patch, connect_patch, \
            patch.object(seed, "_record_assignment", record_spy), \
            patch.object(seed, "log_api_call", log_spy):
        summary = await seed.run_seed(
            company="rubicon",
            calls=100,
            budget_usd=Decimal("5.00"),
            per_call_cost_usd=Decimal("0.05"),
            today=date(2026, 5, 3),
        )

    assert summary["calls_made"] == 100
    assert summary["budget_tripped"] is False
    assert summary["spent_usd"] == "5.00"
    assert len(log_spy.by_role(seed.ROLE_NORMAL)) == 100
    assert len(log_spy.by_role(seed.ROLE_BUDGET_WARN)) == 0


# ---------------------------------------------------------------------------
# Budget-tripped path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_seed_emits_budget_warn_when_next_call_would_breach() -> None:
    """calls=10, budget=$0.10, per_call=$0.05.

    Iter 0: spent=0, 0+0.05=0.05 ≤ 0.10 → spend (spent=0.05).
    Iter 1: spent=0.05, 0.05+0.05=0.10 ≤ 0.10 → spend (spent=0.10).
    Iter 2: spent=0.10, 0.10+0.05=0.15 > 0.10 → trip & break.
    Expected: 2 normal rows, 1 budget-warn row, ``budget_tripped=True``.
    """
    record_spy = _CallSpy()
    log_spy = _CallSpy()
    dsn_patch, connect_patch = _patch_db_connection()
    with _patch_register_prompt(), \
            dsn_patch, connect_patch, \
            patch.object(seed, "_record_assignment", record_spy), \
            patch.object(seed, "log_api_call", log_spy):
        summary = await seed.run_seed(
            company="rubicon",
            calls=10,
            budget_usd=Decimal("0.10"),
            per_call_cost_usd=Decimal("0.05"),
            today=date(2026, 5, 3),
        )

    assert summary["calls_made"] == 2
    assert summary["budget_tripped"] is True
    assert summary["spent_usd"] == "0.10"

    normal_rows = log_spy.by_role(seed.ROLE_NORMAL)
    warn_rows = log_spy.by_role(seed.ROLE_BUDGET_WARN)
    assert len(normal_rows) == 2
    assert len(warn_rows) == 1

    warn = warn_rows[0]
    assert warn["correlation_id"] == seed.CORRELATION_ID
    assert warn["company_id"] == "rubicon"
    assert warn["success"] is False
    assert warn["http_status"] == 429
    assert warn["cost_usd"] == Decimal("0")
    assert warn["agent_id"] == "coach_aa_seed"
    assert warn["extra"]["spent_usd"] == "0.10"
    assert warn["extra"]["cap_usd"] == "0.10"
    assert warn["extra"]["calls_made"] == 2
    assert warn["extra"]["synthetic"] is True

    # Only 2 prompt_assignments writes — the budget cut us off before #3.
    assert len(record_spy.calls) == 2


@pytest.mark.asyncio
async def test_run_seed_budget_warn_row_carries_cost_summary_in_context() -> None:
    """Verify the human-readable ``context`` field on the warn row."""
    record_spy = _CallSpy()
    log_spy = _CallSpy()
    dsn_patch, connect_patch = _patch_db_connection()
    with _patch_register_prompt(), \
            dsn_patch, connect_patch, \
            patch.object(seed, "_record_assignment", record_spy), \
            patch.object(seed, "log_api_call", log_spy):
        await seed.run_seed(
            company="rubicon",
            calls=5,
            budget_usd=Decimal("0.05"),
            per_call_cost_usd=Decimal("0.05"),
            today=date(2026, 5, 3),
        )

    warn_rows = log_spy.by_role(seed.ROLE_BUDGET_WARN)
    assert len(warn_rows) == 1
    ctx = warn_rows[0]["context"]
    assert "cap=$0.05" in ctx
    assert "spent=$0.05" in ctx
    assert "calls=1" in ctx


@pytest.mark.asyncio
async def test_run_seed_trips_immediately_when_first_call_exceeds_budget() -> None:
    """budget=$0.01 < per_call=$0.05 → trip on iter 0 with zero normal rows."""
    record_spy = _CallSpy()
    log_spy = _CallSpy()
    dsn_patch, connect_patch = _patch_db_connection()
    with _patch_register_prompt(), \
            dsn_patch, connect_patch, \
            patch.object(seed, "_record_assignment", record_spy), \
            patch.object(seed, "log_api_call", log_spy):
        summary = await seed.run_seed(
            company="rubicon",
            calls=20,
            budget_usd=Decimal("0.01"),
            per_call_cost_usd=Decimal("0.05"),
            today=date(2026, 5, 3),
        )

    assert summary["calls_made"] == 0
    assert summary["budget_tripped"] is True
    assert summary["spent_usd"] == "0"
    assert len(log_spy.by_role(seed.ROLE_NORMAL)) == 0
    assert len(log_spy.by_role(seed.ROLE_BUDGET_WARN)) == 1
    assert record_spy.calls == []


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_seed_skips_billing_when_assignment_write_fails() -> None:
    """If ``_record_assignment`` raises, that iteration must not be billed.

    We make every other call fail; cost-log rows must equal the count of
    successful assignments, not ``calls``.
    """
    failures = {0, 2, 4}  # zero-based iteration indices that should fail

    counter = {"i": 0}

    async def flaky(**_kwargs: Any) -> None:
        idx = counter["i"]
        counter["i"] += 1
        if idx in failures:
            raise RuntimeError(f"forced failure {idx}")

    log_spy = _CallSpy()
    dsn_patch, connect_patch = _patch_db_connection()
    with _patch_register_prompt(), \
            dsn_patch, connect_patch, \
            patch.object(seed, "_record_assignment", flaky), \
            patch.object(seed, "log_api_call", log_spy):
        summary = await seed.run_seed(
            company="rubicon",
            calls=6,
            budget_usd=Decimal("5.00"),
            per_call_cost_usd=Decimal("0.05"),
            today=date(2026, 5, 3),
        )

    # 6 attempts, 3 failed → 3 successful billings, no budget breach.
    assert summary["calls_made"] == 3
    assert summary["budget_tripped"] is False
    assert summary["spent_usd"] == "0.15"
    assert len(log_spy.by_role(seed.ROLE_NORMAL)) == 3
    assert len(log_spy.by_role(seed.ROLE_BUDGET_WARN)) == 0


@pytest.mark.asyncio
async def test_run_seed_swallows_log_api_call_errors() -> None:
    """A failing ``log_api_call`` must not abort the seed (telemetry-best-effort)."""
    record_spy = _CallSpy()

    async def boom(**_kwargs: Any) -> None:
        raise RuntimeError("api_cost_log down")

    dsn_patch, connect_patch = _patch_db_connection()
    with _patch_register_prompt(), \
            dsn_patch, connect_patch, \
            patch.object(seed, "_record_assignment", record_spy), \
            patch.object(seed, "log_api_call", boom):
        summary = await seed.run_seed(
            company="rubicon",
            calls=3,
            budget_usd=Decimal("5.00"),
            per_call_cost_usd=Decimal("0.05"),
            today=date(2026, 5, 3),
        )

    # Spend and call counter still advance — the seed believed each call
    # was billed even though telemetry was suppressed. This is intentional
    # per the docstring: "telemetry must never break the seed".
    assert summary["calls_made"] == 3
    assert summary["spent_usd"] == "0.15"
    assert len(record_spy.calls) == 3


# ---------------------------------------------------------------------------
# Variant assignment determinism
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_seed_uses_assign_variant_for_each_actor() -> None:
    """The variant chosen for each actor must come from ``assign_variant``."""
    record_spy = _CallSpy()
    log_spy = _CallSpy()

    captured_variants: List[str] = []
    real_assign = seed.assign_variant

    def spying_assign(actor_id, day, prompt_name, variants):
        v = real_assign(actor_id, day, prompt_name, variants)
        captured_variants.append(v)
        return v

    dsn_patch, connect_patch = _patch_db_connection()
    with _patch_register_prompt(), \
            dsn_patch, connect_patch, \
            patch.object(seed, "assign_variant", spying_assign), \
            patch.object(seed, "_record_assignment", record_spy), \
            patch.object(seed, "log_api_call", log_spy):
        summary = await seed.run_seed(
            company="rubicon",
            calls=10,
            budget_usd=Decimal("5.00"),
            per_call_cost_usd=Decimal("0.05"),
            today=date(2026, 5, 3),
        )

    assert len(captured_variants) == 10
    assert all(v in (seed.VARIANT_A, seed.VARIANT_B) for v in captured_variants)
    assert summary["v1_count"] == sum(1 for v in captured_variants if v == seed.VARIANT_A)
    assert summary["v2_count"] == sum(1 for v in captured_variants if v == seed.VARIANT_B)
    assert summary["v1_count"] + summary["v2_count"] == 10


@pytest.mark.asyncio
async def test_make_actor_id_is_zero_padded_and_unique() -> None:
    """Actor IDs must be ``aa_seed_actor_NNNN`` with 4-digit zero-padding."""
    ids = [seed._make_actor_id(i) for i in (0, 7, 99, 1234)]
    assert ids == [
        "aa_seed_actor_0000",
        "aa_seed_actor_0007",
        "aa_seed_actor_0099",
        "aa_seed_actor_1234",
    ]
    # Uniqueness across a 100-element range.
    bulk = {seed._make_actor_id(i) for i in range(100)}
    assert len(bulk) == 100
