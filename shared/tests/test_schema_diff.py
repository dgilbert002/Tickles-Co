"""
Module: test_schema_diff
Purpose: Smoke tests for schema drift detector
Location: /opt/tickles/shared/tests/test_schema_diff.py
"""

import unittest
from shared.scripts.schema_diff import _normalise

class TestSchemaDiff(unittest.TestCase):
    def test_normalise_strips_noise(self) -> None:
        sql = """
-- Dumped from database version 16.1
-- Started on 2024-01-01 00:00:00
SET statement_timeout = 0;
SELECT pg_catalog.set_config('search_path', '', false);
ALTER TABLE public.instruments OWNER TO admin;

CREATE TABLE public.instruments (
    id bigint NOT NULL
);

-- Completed on 2024-01-01 00:00:01
"""
        expected = """
CREATE TABLE public.instruments (
    id bigint NOT NULL
);
"""
        normalized = _normalise(sql)
        self.assertEqual(normalized.strip(), expected.strip())

    def test_normalise_collapses_newlines(self) -> None:
        sql = "SELECT 1;\n\n\n\nSELECT 2;"
        expected = "SELECT 1;\n\nSELECT 2;\n"
        self.assertEqual(_normalise(sql), expected)

if __name__ == "__main__":
    unittest.main()
