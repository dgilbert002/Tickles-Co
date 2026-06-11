"""Round 14 (2026-05-29) — tests for the provider-aware model picker.

Covers the pure-Python pieces (no DB, no network):

  * Pricing normalisation: per-token USD → per-1M-tokens, with the 0/empty/None
    "free placeholder" cases mapping to None (so policies don't show "$0.00").
  * OpenRouter normaliser: vision detection via input_modalities, label/ctx/cost.
  * Requesty normaliser: policy detection via 'policy/' prefix, supports_vision,
    context_window, input_price/output_price, and policy price → None.
  * Slot registry integrity: every slot has the required keys; vision flag is
    correct; list_slots()/slot_is_vision() agree.

Network fetches are patched with sample payloads shaped exactly like the live
provider responses we probed on 2026-05-29.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from shared.intelligence import model_catalogue_sync as mcs
from shared.intelligence import model_config


# ---------------------------------------------------------------------------
# Pricing normalisation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("0.0000003", 0.3),        # 3e-7 per token → $0.30 / Mtok
    (3e-07, 0.3),
    ("0.000003", 3.0),
    (0, None),                 # free placeholder → unknown
    (0.0, None),
    ("", None),
    (None, None),
    ("not-a-number", None),
])
def test_per_token_to_per_mtok(raw, expected):
    assert mcs._per_token_to_per_mtok(raw) == expected


# ---------------------------------------------------------------------------
# OpenRouter normaliser
# ---------------------------------------------------------------------------
_OPENROUTER_SAMPLE = {
    "data": [
        {
            "id": "anthropic/claude-sonnet-4",
            "name": "Anthropic: Claude Sonnet 4",
            "context_length": 200000,
            "architecture": {"input_modalities": ["text", "image"]},
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        },
        {
            "id": "openai/gpt-4o-mini",
            "name": "OpenAI: GPT-4o-mini",
            "context_length": 128000,
            "architecture": {"input_modalities": ["text"]},
            "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
        },
        {"id": "", "name": "junk"},  # must be dropped (no id)
    ]
}


def test_fetch_openrouter_normalises():
    async def run():
        with patch.object(mcs, "_fetch_json", AsyncMock(return_value=_OPENROUTER_SAMPLE)):
            return await mcs.fetch_openrouter()

    rows = asyncio.run(run())
    by_id = {r["model_id"]: r for r in rows}
    assert "" not in by_id  # junk dropped
    sonnet = by_id["anthropic/claude-sonnet-4"]
    assert sonnet["provider"] == "openrouter"
    assert sonnet["is_policy"] is False
    assert sonnet["is_vision"] is True  # has "image" modality
    assert sonnet["context_length"] == 200000
    assert sonnet["input_cost_per_mtok"] == 3.0
    assert sonnet["output_cost_per_mtok"] == 15.0
    mini = by_id["openai/gpt-4o-mini"]
    assert mini["is_vision"] is False  # text-only


# ---------------------------------------------------------------------------
# Requesty normaliser
# ---------------------------------------------------------------------------
_REQUESTY_SAMPLE = {
    "data": [
        {
            "id": "policy/tickles-vision",
            "owned_by": "system",
            "supports_vision": True,
            "context_window": 1000000,
            "input_price": 0,
            "output_price": 0,
        },
        {
            "id": "minimaxi/MiniMax-M2.7",
            "supports_vision": True,
            "context_window": 200000,
            "input_price": 3e-07,
            "output_price": 1.2e-06,
        },
        {
            "id": "some/text-only-model",
            "supports_vision": False,
            "context_window": 32000,
            "input_price": 1e-06,
            "output_price": 2e-06,
        },
    ]
}


def test_fetch_requesty_normalises():
    async def run():
        with patch.object(mcs, "_fetch_json", AsyncMock(return_value=_REQUESTY_SAMPLE)), \
             patch.dict("os.environ", {"REQUESTY_API": "test-key"}, clear=False):
            return await mcs.fetch_requesty()

    rows = asyncio.run(run())
    by_id = {r["model_id"]: r for r in rows}

    pol = by_id["policy/tickles-vision"]
    assert pol["is_policy"] is True
    assert pol["is_vision"] is True
    assert pol["context_length"] == 1000000
    # Policy prices are 0 placeholders → must normalise to None (not $0.00).
    assert pol["input_cost_per_mtok"] is None
    assert pol["output_cost_per_mtok"] is None
    assert pol["label"].endswith("(policy)")

    mm = by_id["minimaxi/MiniMax-M2.7"]
    assert mm["is_policy"] is False
    assert mm["is_vision"] is True
    assert mm["input_cost_per_mtok"] == 0.3
    assert mm["output_cost_per_mtok"] == 1.2

    txt = by_id["some/text-only-model"]
    assert txt["is_vision"] is False
    assert txt["provider"] == "requesty"


def test_fetch_requesty_requires_key():
    async def run():
        with patch.dict("os.environ", {}, clear=True):
            await mcs.fetch_requesty()

    with pytest.raises(RuntimeError):
        asyncio.run(run())


# ---------------------------------------------------------------------------
# Slot registry integrity
# ---------------------------------------------------------------------------
def test_slot_registry_shape():
    required = {
        "kind", "label", "service", "env_gw", "env_model",
        "def_provider", "def_model",
    }
    for slot, sd in model_config.SLOT_REGISTRY.items():
        missing = required - set(sd.keys())
        assert not missing, f"slot {slot} missing keys: {missing}"
        assert sd["kind"] in (model_config.SLOT_KIND_VISION, model_config.SLOT_KIND_TEXT)
        assert sd["def_provider"] in ("openrouter", "requesty")
        assert sd["def_model"]


def test_list_slots_matches_registry():
    slots = {s["slot"] for s in model_config.list_slots()}
    assert slots == set(model_config.SLOT_REGISTRY.keys())


def test_slot_is_vision():
    assert model_config.slot_is_vision("primary") is True
    assert model_config.slot_is_vision("postmortem") is False
    # chart_hacker_opinion is a text-kind slot but vision_capable → True.
    assert model_config.slot_is_vision("chart_hacker_opinion") is True


def test_set_slot_rejects_bad_provider():
    async def run():
        await model_config.set_slot("primary", "azure", "gpt-4")
    with pytest.raises(ValueError):
        asyncio.run(run())
