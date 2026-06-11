#!/usr/bin/env python3
"""Persist a reinterpret onto an existing signal_interpretation and re-arm legs."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

sys.path.insert(0, "/opt/tickles")

from shared.intelligence.reinterpret_persist import persist_reinterpret_and_rearm


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Persist reinterpret and re-arm positions")
    ap.add_argument("--id", type=int, required=True, help="signal_interpretations.id")
    ap.add_argument(
        "--prompt",
        default="2026.05.30-discord-semantic-v9",
        help="prompt_versions.version",
    )
    ap.add_argument("--recall", action="store_true", help="include mem0 recall")
    args = ap.parse_args()
    result = await persist_reinterpret_and_rearm(
        args.id, args.prompt, include_recall=args.recall,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
