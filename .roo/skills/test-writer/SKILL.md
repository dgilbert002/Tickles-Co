---
name: test-writer
description: Write comprehensive tests for Python code. Use when asked to create tests, add test coverage, or verify functionality.
---

# Test Writer Skill

## When Activated
User asks to "write tests", "add tests", "test this", or "verify this works".

## Test Structure
```python
"""Tests for [module_name]."""
import pytest
from unittest.mock import patch, MagicMock

class TestClassName:
    """Tests for ClassName."""

    def test_happy_path(self):
        """Test normal expected behavior."""

    def test_edge_case_empty_input(self):
        """Test with empty/None input."""

    def test_edge_case_invalid_input(self):
        """Test with invalid/malformed input."""

    def test_error_handling_db_down(self):
        """Test behavior when database is unreachable."""

    def test_error_handling_api_error(self):
        """Test behavior when external API returns error."""
```

## Coverage Requirements
For every function, write tests for:
1. Happy path — normal expected input and output
2. Empty input — None, empty string, empty list, zero
3. Invalid input — wrong type, out of range, special characters
4. Error conditions — network failure, database down, timeout
5. Boundary conditions — max values, min values, exactly at limits

## Trading-Specific Tests
- Test with real-looking price data (not 1.0, 2.0 — use 67543.21)
- Test fee calculations with actual exchange fee rates
- Test position sizing with edge cases (99% balance, minimum order size)
- Test timezone handling (UTC vs local)
- Test decimal precision (no floating point errors on financial values)

## Mock External Dependencies
- Database connections → mock with MagicMock
- Exchange APIs → mock with sample responses
- File system → use tmp directories
- Time-dependent code → mock datetime.now()

## Output
1. Complete test file ready to run with `python -m pytest`
2. Report expected coverage percentage
3. List any untestable code that needs refactoring first
