#!/usr/bin/env python3
"""Test script for mem0 with Qdrant + OpenRouter."""

import os
import sys

# Make sure config is importable
sys.path.insert(0, "/opt/tickles/shared/utils")
from mem0_config import MEM0_CONFIG

from mem0 import Memory

def main():
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("ERROR: OPENROUTER_API_KEY environment variable not set")
        sys.exit(1)

    print("Initializing mem0 with Qdrant + OpenRouter...")
    m = Memory.from_config(MEM0_CONFIG)

    test_user = "test_user_tickles"
    test_message = "My favorite color is blue and I love Python programming."

    print(f"Adding test memory for user '{test_user}'...")
    result = m.add(test_message, user_id=test_user)
    print(f"Add result: {result}")

    print(f"\nRetrieving memories for user '{test_user}'...")
    memories = m.get_all(user_id=test_user)
    print(f"Retrieved memories: {memories}")

    print(f"\nSearching for 'favorite color'...")
    search_result = m.search("favorite color", user_id=test_user)
    print(f"Search result: {search_result}")

    print("\nMem0 test PASSED successfully!")

if __name__ == "__main__":
    main()
