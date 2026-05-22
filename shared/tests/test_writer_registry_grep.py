"""
Module: test_writer_registry_grep
Purpose: Smoke tests for writer-registry static analysis
Location: /opt/tickles/shared/tests/test_writer_registry_grep.py
"""

import unittest
from pathlib import Path
from shared.scripts.writer_registry_grep import _scan, _infer_service

class TestWriterRegistryGrep(unittest.TestCase):
    def test_scan_finds_operations(self) -> None:
        content = """
        await conn.execute("INSERT INTO public.tracked_positions (id) VALUES (1)")
        # writer-registry: skip
        await conn.execute("UPDATE news_items SET status = 'done'")
        await conn.execute("DELETE FROM agent_opinions WHERE id = 1")
        """
        found = list(_scan(content))
        self.assertEqual(len(found), 2)
        self.assertIn(("INSERT", "tracked_positions"), found)
        self.assertIn(("DELETE", "agent_opinions"), found)
        # news_items should be skipped due to marker

    def test_infer_service_from_marker(self) -> None:
        content = "# writer-registry: my_custom_service\nimport os"
        svc = _infer_service(Path("any/path.py"), content)
        self.assertEqual(svc, "my_custom_service")

    def test_infer_service_from_path(self) -> None:
        svc = _infer_service(Path("/opt/tickles/shared/intelligence/postmortem_service.py"), "")
        self.assertEqual(svc, "postmortem_service")

if __name__ == "__main__":
    unittest.main()
