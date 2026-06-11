#!/usr/bin/env python3
"""
Run chart_hacker A/B replay: quant on/off × primary vs Requesty Opus 4.8.

Example:
  python3 -m shared.scripts.run_ch_quant_ab_replay --limit 50 --concurrency 4
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from shared.intelligence.ch_replay_experiment import DEFAULT_ARMS, run_experiment


def main() -> int:
    parser = argparse.ArgumentParser(description="ChartHacker quant A/B replay experiment")
    parser.add_argument("--limit", type=int, default=50, help="Number of charts (default 50)")
    parser.add_argument("--concurrency", type=int, default=4, help="Parallel LLM calls")
    parser.add_argument(
        "--output-dir",
        type=str,
        default="",
        help="Output directory (default shared/data/experiments/ch_ab_v2_<ts>)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = Path(args.output_dir) if args.output_dir else Path(
        f"/opt/tickles/shared/data/experiments/ch_ab_v2_{ts}"
    )

    print(f"Starting replay v2 (historical quant at signal time): "
          f"{args.limit} charts × {len(DEFAULT_ARMS)} arms → {out}")
    result = asyncio.run(
        run_experiment(
            limit=args.limit,
            output_dir=out,
            arms=DEFAULT_ARMS,
            concurrency=args.concurrency,
        )
    )
    print(f"Done. {result['rows']} runs written.")
    print(f"CSV: {out / 'results.csv'}")
    print("Summary by arm (avg PnL %, win rate):")
    for arm in result["summary"].get("ranked", []):
        print(
            f"  {arm['arm_id']}: trades={arm['trades']} "
            f"avg_pnl={arm.get('avg_pnl_pct')} win_rate={arm.get('win_rate')} "
            f"errors={arm['errors']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
