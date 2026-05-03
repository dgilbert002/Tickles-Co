"""
Module: test_epic_resolver
Purpose: Unit tests for Capital.com epic resolution.
Location: /opt/tickles/shared/intelligence/test_epic_resolver.py
"""

import unittest

from shared.intelligence.epic_resolver import (
    add_custom_mapping,
    extract_symbol_from_epic,
    is_valid_epic_format,
    list_known_epics,
    resolve_epic,
    resolve_epic_with_fallback,
)


class TestResolveEpic(unittest.TestCase):
    def test_gold(self) -> None:
        self.assertEqual(resolve_epic("GOLD"), "CS.D.XAUUSD.CFD.IP")

    def test_xauusd(self) -> None:
        self.assertEqual(resolve_epic("XAUUSD"), "CS.D.XAUUSD.CFD.IP")

    def test_xau_slash_usd(self) -> None:
        self.assertEqual(resolve_epic("XAU/USD"), "CS.D.XAUUSD.CFD.IP")

    def test_nas100(self) -> None:
        self.assertEqual(resolve_epic("NAS100"), "IX.D.NASDAQ.CFD.IP")

    def test_btcusd(self) -> None:
        self.assertEqual(resolve_epic("BTCUSD"), "CS.D.BTCUSD.CFD.IP")

    def test_eurusd(self) -> None:
        self.assertEqual(resolve_epic("EURUSD"), "CS.D.EURUSD.CFD.IP")

    def test_unknown(self) -> None:
        self.assertIsNone(resolve_epic("UNKNOWN_SYMBOL_123"))

    def test_empty(self) -> None:
        self.assertIsNone(resolve_epic(""))
        self.assertIsNone(resolve_epic(None))  # type: ignore[arg-type]


class TestResolveWithFallback(unittest.TestCase):
    def test_channel_override(self) -> None:
        mappings = {"GOLD": "CS.D.XAUUSD.MINI.IP"}
        result = resolve_epic_with_fallback("GOLD", mappings)
        self.assertEqual(result, "CS.D.XAUUSD.MINI.IP")

    def test_fallback_to_seed(self) -> None:
        result = resolve_epic_with_fallback("SILVER", {})
        self.assertEqual(result, "CS.D.XAGUSD.CFD.IP")


class TestEpicFormat(unittest.TestCase):
    def test_valid_cs(self) -> None:
        self.assertTrue(is_valid_epic_format("CS.D.XAUUSD.CFD.IP"))

    def test_valid_ix(self) -> None:
        self.assertTrue(is_valid_epic_format("IX.D.NASDAQ.CFD.IP"))

    def test_invalid(self) -> None:
        self.assertFalse(is_valid_epic_format("XAUUSD"))
        self.assertFalse(is_valid_epic_format(""))


class TestExtractSymbol(unittest.TestCase):
    def test_extract(self) -> None:
        self.assertEqual(extract_symbol_from_epic("CS.D.XAUUSD.CFD.IP"), "XAUUSD")

    def test_none(self) -> None:
        self.assertIsNone(extract_symbol_from_epic(""))


class TestCustomMapping(unittest.TestCase):
    def test_add_and_resolve(self) -> None:
        add_custom_mapping("CUSTOM", "CS.D.CUSTOM.CFD.IP")
        self.assertEqual(resolve_epic("CUSTOM"), "CS.D.CUSTOM.CFD.IP")


class TestListKnown(unittest.TestCase):
    def test_not_empty(self) -> None:
        epics = list_known_epics()
        self.assertGreater(len(epics), 10)
        # Check GOLD is in there
        symbols = [s for s, _ in epics]
        self.assertIn("GOLD", symbols)


if __name__ == "__main__":
    unittest.main()
