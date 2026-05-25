"""Round 9 (2026-05-24) — Behavioural tests for the trader-setup gate.

These tests cover the regression that motivated the round:
  Before:  every level-commentary post (e.g. "btc must get above here")
           created a tracked_position attributed to the trader, falsely
           inflating the trader's accuracy stats.
  After:   `_is_explicit_trader_setup` rejects commentary posts under the
           new prompt, the prompt itself instructs the LLM to leave
           `trader_trades` empty for commentary, and the performance
           scorer only counts signal_interpretations that have at least
           one tracked_positions row with signal_source='trader'.

We do NOT hit the live database here — these are pure-Python behavioural
tests against the gate function and the prompt JSON shape. The DB-level
behaviour is covered by the dry-run / apply path of the backfill script.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.intelligence.interpretation_service import (
    _DIRECTION_WORDS,
    _LEVEL_WORDS,
    _TRADER_GATE_PROMPT_TAG,
    _VALID_EVIDENCE_CODES,
    _is_explicit_trader_setup,
)


CURRENT_PROMPT = "2026.05.24-trader-explicit-only-v1"
LEGACY_PROMPT = "2026.05.04-no-symbol-guessing-v2"


# ---------------------------------------------------------------------------
# Gate: commentary / chart-only / banter MUST fail
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "btc must get above here",
    "@scalpers eyes on H for potential break",
    "watching this level",
    "key support holding",
    "if it breaks 80k I'm in",          # conditional, not an entry
    "this looks bullish",
    "waiting for confirmation",
    "free money",
    "gave it all back",
    "lets goh",
    "like this?",
    "these are just doodles.",
    "THANKS guys\ngold proper banger",
    "https://giphy.com/gifs/pokemon-psyduck-s01e68-FwQ6yV6Adh48U",
    "",  # empty post — chart-only with no LLM evidence
])
def test_gate_rejects_commentary(text):
    ok, ev = _is_explicit_trader_setup({}, text, CURRENT_PROMPT)
    assert ok is False, f"expected commentary {text!r} to fail, got {ev}"
    assert ev == "no_explicit_evidence"


# ---------------------------------------------------------------------------
# Gate: explicit setups MUST pass
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected_evidence", [
    ("long BTC entry 78000 SL 74850 TP 79500",      "A3_text_fallback"),
    ("BTCUSDT.P ... BUY 70130-69830 SL 69k @scalpers", "A3_text_fallback"),
    ("eth long at 2700, stop 2650",                 "A3_text_fallback"),
    ("shorting eth target 3000",                    "A3_text_fallback"),
    # Casual entry (loose price-window heuristic)
    ("Jumped in a BTC long to 79100. Low value",    "A3_price_window"),
    ("entered btc 70k",                             "A3_price_window"),
    ("buying $0.85 here",                           "A3_price_window"),
])
def test_gate_accepts_explicit_setups(text, expected_evidence):
    ok, ev = _is_explicit_trader_setup({}, text, CURRENT_PROMPT)
    assert ok is True, f"expected setup {text!r} to pass, got {ev}"
    assert ev == expected_evidence, f"unexpected evidence code: {ev}"


# ---------------------------------------------------------------------------
# Gate: LLM-declared evidence codes (A1/A2/A3/A4) override text scanning
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", sorted(_VALID_EVIDENCE_CODES))
def test_llm_evidence_codes_override_text(code):
    # Even with commentary text, an LLM-declared A-code should win.
    ok, ev = _is_explicit_trader_setup(
        {"explicit_evidence": code},
        "btc must get above here",  # commentary
        CURRENT_PROMPT,
    )
    assert ok is True
    assert ev == code


def test_invalid_evidence_code_falls_through_to_text_scan():
    # An unknown evidence code should be ignored — gate falls back to text.
    ok_pass, _ = _is_explicit_trader_setup(
        {"explicit_evidence": "Z99"},
        "long BTC entry 78000 SL 74850",
        CURRENT_PROMPT,
    )
    ok_fail, _ = _is_explicit_trader_setup(
        {"explicit_evidence": "Z99"},
        "watching this level",
        CURRENT_PROMPT,
    )
    assert ok_pass is True
    assert ok_fail is False


# ---------------------------------------------------------------------------
# Gate: legacy / empty / unknown prompt versions bypass the gate
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("prompt_version", [
    "",
    None,
    LEGACY_PROMPT,
    "2026.04.01-some-old-version",
])
def test_legacy_prompt_bypasses_gate(prompt_version):
    # Whatever the text is, legacy callers should pass-through unchanged so
    # historical interpretations aren't broken by the new gate.
    ok, ev = _is_explicit_trader_setup({}, "btc must get above here", prompt_version)
    assert ok is True
    assert ev == "legacy_prompt"


def test_invalid_trade_object_rejected():
    ok, ev = _is_explicit_trader_setup(None, "long btc 70k", CURRENT_PROMPT)
    assert ok is False
    assert ev == "invalid_trade_object"


# ---------------------------------------------------------------------------
# Prompt JSON: the new version + the explicit_evidence schema must be wired
# ---------------------------------------------------------------------------
def _load_prompt():
    p = Path(__file__).resolve().parents[1] / "intelligence" / "prompts" / "chart_analysis.json"
    return json.loads(p.read_text())


def test_prompt_version_bumped():
    data = _load_prompt()
    version = data["chart_analysis"]["version"]
    # Must be the round-9 explicit-only version.
    assert _TRADER_GATE_PROMPT_TAG in version, (
        f"prompt version must contain '{_TRADER_GATE_PROMPT_TAG}', "
        f"got {version!r}"
    )
    # Must be parseable as date-prefixed semver.
    parts = version.split("-", 1)
    assert len(parts) == 2 and parts[0].count(".") == 2
    assert parts[0] >= "2026.05.24"


def test_prompt_separates_trader_and_chart_hacker_tracks():
    """The system_prompt must clearly enforce the two-track separation."""
    sys = _load_prompt()["chart_analysis"]["system_prompt"]
    # Track-A header.
    assert "TRACK A" in sys and "trader_trades" in sys
    # Track-B header.
    assert "TRACK B" in sys and "chart_hacker_trades" in sys
    # Strict-explicit gate hint.
    assert "STRICT EXPLICIT-ONLY" in sys
    # Final-check section that reminds the LLM to leave trader_trades empty.
    assert "FINAL CHECK" in sys
    # The prompt must NOT contain the old "Construct a trade" hallucination
    # instruction in the trader_trades section. It can still appear in the
    # chart_hacker section because that's where AI-inferred trades belong.
    # The key removed phrase from the OLD prompt was the bullet:
    # "Construct a trade: entry near support for BUY".
    # The NEW prompt only allows that under TRACK B.
    track_a_section = sys.split("TRACK B")[0]
    assert "Construct a trade" not in track_a_section, (
        "Track A (trader_trades) must NOT instruct the LLM to construct trades"
    )


def test_prompt_advertises_explicit_evidence_field():
    """Each trader_trade should carry an explicit_evidence tag (A1/A2/A3/A4)."""
    sys = _load_prompt()["chart_analysis"]["system_prompt"]
    assert '"explicit_evidence"' in sys
    for code in _VALID_EVIDENCE_CODES:
        assert code in sys


# ---------------------------------------------------------------------------
# Sanity: the keyword sets are non-empty (catches accidental refactor wipes)
# ---------------------------------------------------------------------------
def test_keyword_sets_nonempty():
    assert len(_DIRECTION_WORDS) >= 6
    assert len(_LEVEL_WORDS) >= 6
    assert "long" in _DIRECTION_WORDS
    assert "short" in _DIRECTION_WORDS
    assert "entry" in _LEVEL_WORDS
    assert "sl" in _LEVEL_WORDS
