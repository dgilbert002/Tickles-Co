"""Round 10 (2026-05-24) — Behavioural tests for the vision-model picker.

These tests cover the regressions Round 10 is designed to prevent:

  * Allow-list bypass: a typoed / pasted-from-OpenRouter model id that we
    haven't vetted as vision-capable must NEVER be persisted, because the
    InterpretationService would then silently fail every interpretation.

  * Slot validity: only 'primary' / 'fallback' / 'prefilter' may ever
    appear in the audit table or system_config.

  * Resolution order: DB row beats env var beats code default. This is
    the contract the dashboard dropdown depends on — if the operator
    sets a model in the dropdown, that value MUST win over any stale env
    var that survived a service restart.

  * Cache invalidation: after `set_model` we must invalidate the slot's
    entry so the next `get_model` reads the new value. Otherwise the
    operator picks a model and the next 60s of interpretations still use
    the previous one.

  * Catalogue shape: every entry must have an ``id``, ``label``, and
    ``vision: true`` (or absent — defaults to True). The Settings UI relies
    on these fields.

We do NOT touch the database from these tests. The DB-level behaviour is
covered by the live smoke test we ran during Phase 1 setup. These are
pure-Python behavioural tests against the model_config module and the
catalogue JSON.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from shared.intelligence import model_config


CATALOGUE_PATH = (
    Path(__file__).resolve().parent.parent
    / "intelligence"
    / "vision_model_catalogue.json"
)


# ---------------------------------------------------------------------------
# Catalogue shape
# ---------------------------------------------------------------------------
def test_catalogue_loads_and_has_qwen3vl():
    """The catalogue MUST contain qwen/qwen3-vl-32b-instruct (the user's
    explicit Round 10 ask). If someone deletes this entry the dashboard's
    'switch to qwen' workflow breaks silently."""
    cat = model_config.get_catalogue()
    ids = [m["id"] for m in cat.get("models", [])]
    assert "qwen/qwen3-vl-32b-instruct" in ids, (
        "Round 10 default primary model is missing from the catalogue."
    )


def test_catalogue_contains_safety_net_fallback():
    """Claude Sonnet 4 is the Round 10 default fallback — losing it from the
    catalogue would mean no curated fallback option exists."""
    cat = model_config.get_catalogue()
    ids = [m["id"] for m in cat.get("models", [])]
    assert "anthropic/claude-sonnet-4" in ids


def test_every_catalogue_entry_has_required_fields():
    """The dashboard relies on these fields. Missing them = broken UI."""
    cat = model_config.get_catalogue()
    for m in cat.get("models", []):
        assert "id" in m, f"catalogue entry missing 'id': {m}"
        assert m["id"], "catalogue entry has empty id"
        assert "label" in m, f"entry {m['id']!r} missing 'label'"
        # vision is True by default; only false explicitly excludes
        assert m.get("vision", True) is not False
        cost = m.get("input_cost_per_million_tokens_usd")
        assert cost is None or isinstance(cost, (int, float))


def test_catalogue_recommends_qwen_for_primary():
    """The dropdown shows a star next to 'recommended_for' models. Qwen3-VL
    must be marked recommended_for primary so the operator sees the star."""
    cat = model_config.get_catalogue()
    qwen = next(m for m in cat["models"] if m["id"] == "qwen/qwen3-vl-32b-instruct")
    assert "primary" in qwen.get("recommended_for", []), (
        "qwen3-vl-32b should be flagged as recommended for the primary slot."
    )


# ---------------------------------------------------------------------------
# Allow-list enforcement
# ---------------------------------------------------------------------------
def test_is_allowed_accepts_curated_models():
    assert model_config.is_allowed("qwen/qwen3-vl-32b-instruct") is True
    assert model_config.is_allowed("anthropic/claude-sonnet-4") is True
    assert model_config.is_allowed("google/gemini-2.5-flash") is True


def test_is_allowed_rejects_unknown():
    assert model_config.is_allowed("foo/bar-model") is False
    assert model_config.is_allowed("") is False
    assert model_config.is_allowed("anthropic/claude-some-future-version") is False


@pytest.mark.asyncio
async def test_set_model_rejects_non_curated():
    """Even the bypass parameter is a footgun, but the default path MUST
    refuse anything not on the allow-list."""
    with pytest.raises(ValueError, match="not in the curated"):
        await model_config.set_model(
            model_config.SLOT_PRIMARY,
            "foo/bar-model",
            pool=AsyncMock(),  # never reached
        )


@pytest.mark.asyncio
async def test_set_model_rejects_unknown_slot():
    with pytest.raises(ValueError, match="unknown model slot"):
        await model_config.set_model(
            "supervisor",  # not a real slot
            "qwen/qwen3-vl-32b-instruct",
            pool=AsyncMock(),
        )


@pytest.mark.asyncio
async def test_set_model_rejects_blank_model():
    with pytest.raises(ValueError, match="non-empty"):
        await model_config.set_model(
            model_config.SLOT_PRIMARY,
            "",
            pool=AsyncMock(),
        )


# ---------------------------------------------------------------------------
# Resolution order: DB > env > code default
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_db_value_beats_env_var():
    """When the operator picks a model in the dashboard dropdown, that DB
    row must win over any env var value the service started with."""
    model_config.invalidate_cache()
    fake_pool = AsyncMock()
    # DB has the new operator-chosen value.
    fake_pool.fetch_one.return_value = {"config_value": "openai/gpt-4o"}

    with patch.dict(
        os.environ,
        {"CHART_HACKER_MODEL_PRIMARY": "anthropic/claude-sonnet-4"},
    ):
        got = await model_config.get_model(model_config.SLOT_PRIMARY, pool=fake_pool)

    assert got == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_env_var_beats_code_default():
    """If no DB row, the env var wins over the hard-coded default."""
    model_config.invalidate_cache()
    fake_pool = AsyncMock()
    fake_pool.fetch_one.return_value = None  # no DB row

    with patch.dict(
        os.environ,
        {"CHART_HACKER_MODEL_FALLBACK": "google/gemini-2.5-pro"},
    ):
        got = await model_config.get_model(model_config.SLOT_FALLBACK, pool=fake_pool)

    assert got == "google/gemini-2.5-pro"


@pytest.mark.asyncio
async def test_falls_back_to_code_default_when_nothing_set():
    """No DB, no env var → must use the Round 10 code default."""
    model_config.invalidate_cache()
    fake_pool = AsyncMock()
    fake_pool.fetch_one.return_value = None

    # Ensure the env var is NOT set.
    env = {k: v for k, v in os.environ.items()
           if k != "CHART_HACKER_MODEL_PRIMARY"}
    with patch.dict(os.environ, env, clear=True):
        got = await model_config.get_model(model_config.SLOT_PRIMARY, pool=fake_pool)

    assert got == "qwen/qwen3-vl-32b-instruct"


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cache_hit_skips_db_read():
    """Two consecutive lookups inside the cache TTL must hit the DB at most once."""
    model_config.invalidate_cache()
    fake_pool = AsyncMock()
    fake_pool.fetch_one.return_value = {"config_value": "openai/gpt-4o"}

    a = await model_config.get_model(model_config.SLOT_PRIMARY, pool=fake_pool)
    b = await model_config.get_model(model_config.SLOT_PRIMARY, pool=fake_pool)
    assert a == b == "openai/gpt-4o"
    # Second call should have been served from cache, not DB.
    assert fake_pool.fetch_one.await_count == 1


@pytest.mark.asyncio
async def test_set_model_invalidates_cache():
    """After set_model, the next get_model MUST re-read the DB so the
    operator's choice is visible immediately, not in 60 seconds."""
    model_config.invalidate_cache()
    fake_pool = AsyncMock()

    # 1. Initial DB state.
    fake_pool.fetch_one.return_value = {"config_value": "openai/gpt-4o"}
    initial = await model_config.get_model(model_config.SLOT_PRIMARY, pool=fake_pool)
    assert initial == "openai/gpt-4o"

    # 2. Operator changes via dropdown.
    fake_pool.execute.return_value = 1  # number of rows affected
    fake_pool.fetch_one.return_value = {"config_value": "openai/gpt-4o"}  # for "previous" lookup
    await model_config.set_model(
        model_config.SLOT_PRIMARY,
        "qwen/qwen3-vl-32b-instruct",
        pool=fake_pool,
        actor_label="test-cache-invalidate",
    )

    # 3. Next get_model should refetch and see the new value.
    fake_pool.fetch_one.return_value = {"config_value": "qwen/qwen3-vl-32b-instruct"}
    after = await model_config.get_model(model_config.SLOT_PRIMARY, pool=fake_pool)
    assert after == "qwen/qwen3-vl-32b-instruct"


# ---------------------------------------------------------------------------
# Bootstrap helpers
# ---------------------------------------------------------------------------
def test_get_model_sync_does_not_touch_db():
    """Sync helper used at module import time — must NEVER touch the DB."""
    primary = model_config.get_model_sync(model_config.SLOT_PRIMARY)
    fallback = model_config.get_model_sync(model_config.SLOT_FALLBACK)
    prefilter = model_config.get_model_sync(model_config.SLOT_PREFILTER)
    assert primary
    assert fallback
    assert prefilter
    # Round 10 defaults — these are the values shipped with the code.
    # If a CHART_HACKER_MODEL_* env var is set in the runner, it wins.
    if not os.environ.get("CHART_HACKER_MODEL_PRIMARY"):
        assert primary == "qwen/qwen3-vl-32b-instruct"
    if not os.environ.get("CHART_HACKER_MODEL_FALLBACK"):
        assert fallback == "anthropic/claude-sonnet-4"
    if not os.environ.get("CHART_HACKER_PREFILTER_MODEL"):
        assert prefilter == "google/gemini-2.5-flash"


def test_valid_slots_constant():
    """The three slot names are part of the public API. Renaming any of
    them breaks the dashboard, the audit table, and the SQL CHECK."""
    assert model_config.VALID_SLOTS == (
        model_config.SLOT_PRIMARY,
        model_config.SLOT_FALLBACK,
        model_config.SLOT_PREFILTER,
    )
    assert model_config.SLOT_PRIMARY == "primary"
    assert model_config.SLOT_FALLBACK == "fallback"
    assert model_config.SLOT_PREFILTER == "prefilter"


def test_get_model_sync_rejects_unknown_slot():
    with pytest.raises(ValueError):
        model_config.get_model_sync("supervisor")
