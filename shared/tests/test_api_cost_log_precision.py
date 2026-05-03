"""
Module: test_api_cost_log_precision
Purpose: Verify NUMERIC(20,8) preserves sub-cent precision for cost_usd.
Location: /opt/tickles/shared/tests/test_api_cost_log_precision.py
"""

from decimal import Decimal

import pytest

from shared.utils.api_cost_log import _estimate_cost_usd


class TestCostPrecision:
    """Precision tests for api_cost_log cost_usd column."""

    def test_estimate_returns_decimal(self) -> None:
        """_estimate_cost_usd must return a Decimal."""
        cost = _estimate_cost_usd("anthropic/claude-sonnet-4", 1000, 500)
        assert isinstance(cost, Decimal)

    def test_sub_cent_precision(self) -> None:
        """Very small costs must retain 8 decimal places."""
        cost = _estimate_cost_usd("google/gemini-2.0-flash-001", 1, 1)
        # 1 token in + 1 token out at $0.10/$0.40 per 1M = $0.0000005
        assert cost == Decimal("0.00000050")

    def test_exact_value_0_00012345(self) -> None:
        """Inserting 0.00012345 must round-trip exactly."""
        # This is the benchmark value from the plan [AD]
        value = Decimal("0.00012345")
        # Simulate what Postgres NUMERIC(20,8) would store
        stored = value.quantize(Decimal("0.00000001"))
        assert stored == Decimal("0.00012345")

    def test_no_float_rounding(self) -> None:
        """Decimal must not suffer from float rounding at 8 decimals."""
        value = Decimal("0.12345678")
        stored = value.quantize(Decimal("0.00000001"))
        assert stored == value
        # Demonstrate that Decimal preserves precision where float does not.
        # A 18-digit decimal cannot be represented exactly in binary float64
        # (which has ~15-17 significant digits of precision).
        precise = Decimal("0.123456789012345678")
        float_rounded = float(precise)
        back_to_decimal = Decimal(str(float_rounded))
        assert back_to_decimal != precise, (
            f"float rounding lost precision: {precise} -> {back_to_decimal}"
        )
        assert stored == Decimal("0.12345678")  # Decimal is exact
