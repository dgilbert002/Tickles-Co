"""
Module: test_mem0_isolation
Purpose: Verify dev/trading memory isolation at the data layer.
Location: /opt/tickles/shared/tests/test_mem0_isolation.py
"""

import logging
import time
import unittest
from typing import Any, Dict, List

import pytest

from shared.utils.mem0_config import get_memory, get_dev_memory

logger = logging.getLogger(__name__)


@pytest.mark.integration
class TestMem0Isolation(unittest.TestCase):
    """Verify that dev and trading memories are isolated at the data layer."""

    def test_dev_and_trading_memories_are_isolated(self) -> None:
        """
        Write sentinel memories to dev and rubicon namespaces, then verify:
        1. Each namespace can find its own sentinel.
        2. No cross-collection leakage (wrong sentinel never appears).
        3. All results from a namespace have the matching user_id.
        """
        ts = str(int(time.time()))
        dev_sentinel = f"ISO_DEV_SENTINEL_{ts}"
        trade_sentinel = f"ISO_TRADE_SENTINEL_{ts}"

        # Write dev memory
        dev_mem, dev_aid = get_dev_memory(agent="isolation_test")
        dev_add_result = dev_mem.add(
            dev_sentinel,
            user_id="dev",
            agent_id=dev_aid,
            metadata={"type": "isolation_test", "sentinel": "dev"},
        )
        self.assertIsNotNone(dev_add_result, "dev_mem.add() returned None — write failed")
        logger.info("Wrote dev sentinel: %s (result=%s)", dev_sentinel, dev_add_result)

        # Write rubicon memory
        trade_mem, trade_aid = get_memory(company="rubicon", agent="isolation_test")
        trade_add_result = trade_mem.add(
            trade_sentinel,
            user_id="rubicon",
            agent_id=trade_aid,
            metadata={"type": "isolation_test", "sentinel": "trade"},
        )
        self.assertIsNotNone(trade_add_result, "trade_mem.add() returned None — write failed")
        logger.info("Wrote rubicon sentinel: %s (result=%s)", trade_sentinel, trade_add_result)

        # Allow Qdrant to index new vectors before searching
        time.sleep(3)

        # --- Verification A: each namespace finds its OWN sentinel ---
        dev_own_results = dev_mem.search(
            dev_sentinel,
            user_id="dev",
            agent_id=dev_aid,
            limit=10,
        )
        dev_own_memories: List[Dict[str, Any]] = dev_own_results.get("results", [])

        trade_own_results = trade_mem.search(
            trade_sentinel,
            user_id="rubicon",
            agent_id=trade_aid,
            limit=10,
        )
        trade_own_memories: List[Dict[str, Any]] = trade_own_results.get("results", [])

        # Assertion A1: Dev sentinel appears in dev results
        dev_own_texts = [m.get("memory", "") for m in dev_own_memories]
        self.assertTrue(
            any(dev_sentinel in t for t in dev_own_texts),
            f"Dev sentinel '{dev_sentinel}' not found in dev results: {dev_own_texts}",
        )

        # Assertion A2: Trade sentinel appears in rubicon results
        trade_own_texts = [m.get("memory", "") for m in trade_own_memories]
        self.assertTrue(
            any(trade_sentinel in t for t in trade_own_texts),
            f"Trade sentinel '{trade_sentinel}' not found in rubicon results: {trade_own_texts}",
        )

        # --- Verification B: cross-search shows no leakage ---
        dev_cross_results = dev_mem.search(
            trade_sentinel,
            user_id="dev",
            agent_id=dev_aid,
            limit=10,
        )
        dev_cross_memories: List[Dict[str, Any]] = dev_cross_results.get("results", [])

        trade_cross_results = trade_mem.search(
            dev_sentinel,
            user_id="rubicon",
            agent_id=trade_aid,
            limit=10,
        )
        trade_cross_memories: List[Dict[str, Any]] = trade_cross_results.get("results", [])

        # Assertion B1: All dev results (cross or own) must have user_id == 'dev'
        for m in dev_own_memories + dev_cross_memories:
            self.assertEqual(
                m.get("user_id"),
                "dev",
                f"Cross-collection leak: dev search returned memory with user_id={m.get('user_id')!r}: {m.get('memory', '')[:80]}",
            )

        # Assertion B2: All rubicon results (cross or own) must have user_id == 'rubicon'
        for m in trade_own_memories + trade_cross_memories:
            self.assertEqual(
                m.get("user_id"),
                "rubicon",
                f"Cross-collection leak: rubicon search returned memory with user_id={m.get('user_id')!r}: {m.get('memory', '')[:80]}",
            )

        # Assertion B3: Trade sentinel must NOT appear in dev results
        dev_cross_texts = [m.get("memory", "") for m in dev_cross_memories]
        self.assertFalse(
            any(trade_sentinel in t for t in dev_cross_texts),
            f"Cross-collection leak: trade sentinel '{trade_sentinel}' found in dev results: {dev_cross_texts}",
        )

        # Assertion B4: Dev sentinel must NOT appear in rubicon results
        trade_cross_texts = [m.get("memory", "") for m in trade_cross_memories]
        self.assertFalse(
            any(dev_sentinel in t for t in trade_cross_texts),
            f"Cross-collection leak: dev sentinel '{dev_sentinel}' found in rubicon results: {trade_cross_texts}",
        )

        logger.info(
            "Isolation verified: dev_own=%d dev_cross=%d trade_own=%d trade_cross=%d",
            len(dev_own_memories),
            len(dev_cross_memories),
            len(trade_own_memories),
            len(trade_cross_memories),
        )
