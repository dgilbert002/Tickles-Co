"""
Module: verify_learning_loop
Purpose: Verify that memories are correctly stored and retrievable via ScopedMemory.
Location: /opt/tickles/shared/tests/verify_learning_loop.py
"""
import sys
import logging
import json

sys.path.append("/opt/tickles")
from shared.utils.mem0_config import ScopedMemory
from shared.utils.config import load_env

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def verify():
    load_env()
    try:
        # Check Surgeon 1 memories
        logger.info("Checking Surgeon 1 memories...")
        mem1 = ScopedMemory(company="rubicon", agent_id="surgeon1")
        results1 = mem1.search("autopsy", limit=5, user_id="rubicon", agent_id="surgeon1")
        logger.info("Surgeon 1 results: %s", json.dumps(results1, indent=2))
        
        found1 = False
        for r in results1.get("results", []):
            if r.get("metadata", {}).get("type") == "autopsy" or "closed via" in r.get("memory", ""):
                found1 = True
                break
        
        if found1:
            logger.info("SUCCESS: Found autopsy in Surgeon 1 memory")
        else:
            logger.warning("FAILURE: No autopsy found in Surgeon 1 memory")

        # Check Surgeon 2 memories
        logger.info("Checking Surgeon 2 memories...")
        mem2 = ScopedMemory(company="rubicon", agent_id="surgeon2")
        results2 = mem2.search("autopsy", limit=5, user_id="rubicon", agent_id="surgeon2")
        logger.info("Surgeon 2 results: %s", json.dumps(results2, indent=2))
        
        found2 = False
        for r in results2.get("results", []):
            if r.get("metadata", {}).get("type") == "autopsy" or "closed via" in r.get("memory", ""):
                found2 = True
                break
        
        if found2:
            logger.info("SUCCESS: Found autopsy in Surgeon 2 memory")
        else:
            logger.warning("FAILURE: No autopsy found in Surgeon 2 memory (expected if no trades closed yet)")

    except Exception as e:
        logger.exception("Verification failed: %s", e)

if __name__ == "__main__":
    verify()
