"""
Module: test_instrument_normaliser
Purpose: Unit tests for shared.utils.instrument_normaliser.
Location: /opt/tickles/shared/tests/test_instrument_normaliser.py
"""

import sys

sys.path.insert(0, "/opt/tickles")

import pytest

from shared.utils.instrument_normaliser import (
    normalise_instrument,
    normalise_venue,
    to_canonical_symbol,
)


@pytest.mark.parametrize(
    "raw_symbol,raw_exchange,expected",
    [
        # Slash-form input is the canonical form — round-trips losslessly.
        ("btc/usdt", "BYBIT", ("BTC/USDT", "bybit")),
        ("BTC/USDT", "bybit", ("BTC/USDT", "bybit")),
        # Concatenated form is split via the known-quotes table.
        ("BTCUSDT", "binance-futures", ("BTC/USDT", "binance")),
        ("ethusdc", "okx-swap", ("ETH/USDC", "okx")),
        # Dash-form input is normalised to slash form.
        ("BTC-USDT", "BYBIT", ("BTC/USDT", "bybit")),
        ("SOL-USD", "bybit-perp", ("SOL/USD", "bybit")),
        # Whitespace and case are tolerated.
        ("  btc usdt  ", "kraken", ("BTC/USDT", "kraken")),
        # Perpetual / contract suffix preserved as a -tag.
        ("BTCUSDT.P", "binance-futures", ("BTC/USDT-P", "binance")),
        ("ETH-PERP", None, ("ETH-PERP", "unknown")),
        # Unrecognised quote falls back to the cleaned uppercase token.
        ("UNKNOWN123", "kraken", ("UNKNOWN123", "kraken")),
        # FX-style instruments split on the trailing 3-letter quote.
        ("XAUUSD", "capital", ("XAU/USD", "capital")),
        # Empty inputs.
        ("", "", ("", "unknown")),
    ],
)
def test_normalise_instrument(raw_symbol, raw_exchange, expected):
    assert normalise_instrument(raw_symbol, raw_exchange) == expected


@pytest.mark.parametrize(
    "raw_symbol,expected",
    [
        ("BTCUSDT", "BTC/USDT"),
        ("BTC/USDT", "BTC/USDT"),
        ("BTC-USDT", "BTC/USDT"),
        ("btcusdt", "BTC/USDT"),
        ("ETH-PERP", "ETH-PERP"),
        ("BTCUSDT.P", "BTC/USDT-P"),
        ("", ""),
        (None, ""),
        # 2026-05-22 — CCXT perp / swap forms must round-trip back to the
        # spot slash form (BTC/USDT), not the buggy ``BTCUSDT/USDT`` that
        # the original implementation produced when collapsing separators
        # before splitting base/quote.
        ("BTC/USDT:USDT", "BTC/USDT"),
        ("1000PEPE/USDT:USDT", "1000PEPE/USDT"),
        ("RSR/USDT:USDT", "RSR/USDT"),
        # Inverse perp on Bybit — settle currency differs from quote.
        ("XLM/USD:XLM", "XLM/USD"),
        # CCXT-style dated futures / options (``-yymmdd-strike-side``).
        ("BTC/USDT:USDT-260626-90000-C", "BTC/USDT"),
    ],
)
def test_to_canonical_symbol(raw_symbol, expected):
    assert to_canonical_symbol(raw_symbol) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BYBIT", "bybit"),
        ("binance-futures", "binance"),
        ("bybit-perp", "bybit"),
        ("okx-swap", "okx"),
        ("kraken-futures", "kraken"),
        (None, "unknown"),
        ("", "unknown"),
        ("Capital", "capital"),
    ],
)
def test_normalise_venue(raw, expected):
    assert normalise_venue(raw) == expected
