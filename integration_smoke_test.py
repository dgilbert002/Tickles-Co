"""
Integration smoke test: run one cycle of each intelligence pipeline service.
Demonstrates Discord → Interpretation → Position Monitor → Guru flow.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/opt/tickles")

from shared.utils.config import load_env

load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("integration_smoke")


async def run_discord_cycle() -> dict:
    """Run one Discord collection cycle."""
    from shared.collectors.discord.discord_collector import DiscordCollector

    logger.info("=== Discord Collector ===")
    collector = DiscordCollector()
    try:
        items = await collector.collect()
        logger.info(f"Collected {len(items)} news items from Discord")
        return {"status": "ok", "items_collected": len(items)}
    except Exception as e:
        logger.error(f"Discord collector failed: {e}")
        return {"status": "error", "error": str(e)}


async def run_interpretation_cycle() -> dict:
    """Run one interpretation cycle."""
    from shared.intelligence.interpretation_service import InterpretationService, InterpretationConfig

    logger.info("=== Interpretation Service ===")
    cfg = InterpretationConfig()
    svc = InterpretationService(cfg)
    try:
        result = await svc.run_cycle()
        logger.info(f"Interpretation cycle: {result}")
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"Interpretation service failed: {e}")
        return {"status": "error", "error": str(e)}


async def run_position_monitor_cycle() -> dict:
    """Run one position monitor cycle."""
    from shared.intelligence.position_monitor import PositionMonitor

    logger.info("=== Position Monitor ===")
    monitor = PositionMonitor()
    try:
        result = await monitor.run_cycle()
        logger.info(f"Position monitor cycle: {result}")
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"Position monitor failed: {e}")
        return {"status": "error", "error": str(e)}


async def run_guru_cycle() -> dict:
    """Run one ChartHacker guru cycle."""
    from shared.intelligence.chart_hacker_guru import ChartHackerGuru

    logger.info("=== ChartHacker Guru ===")
    guru = ChartHackerGuru()
    try:
        result = await guru.run_cycle(days=30)
        logger.info(f"Guru cycle: {result}")
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"Guru failed: {e}")
        return {"status": "error", "error": str(e)}


async def query_database() -> dict:
    """Query database for current state."""
    import asyncpg

    logger.info("=== Database State ===")
    conn = await asyncpg.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USER", "admin"),
        password=os.environ.get("DB_PASSWORD", "Tickles21!"),
        database=os.environ.get("DB_NAME_SHARED", "tickles_shared"),
    )

    stats = {}
    for table in [
        "news_items",
        "media_items",
        "signal_interpretations",
        "trader_profiles",
        "tracked_positions",
        "position_updates",
        "agent_opinions",
    ]:
        try:
            cnt = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
            stats[table] = cnt
            logger.info(f"  {table}: {cnt} rows")
        except Exception as e:
            stats[table] = f"error: {e}"
            logger.warning(f"  {table}: query failed — {e}")

    # Recent news items
    try:
        recent = await conn.fetch(
            "SELECT id, source_id, title, created_at FROM news_items ORDER BY created_at DESC LIMIT 5"
        )
        stats["recent_news"] = [
            {"id": r["id"], "source_id": r["source_id"], "title": r["title"][:80], "created_at": str(r["created_at"])}
            for r in recent
        ]
    except Exception as e:
        stats["recent_news"] = f"error: {e}"

    await conn.close()
    return {"status": "ok", "stats": stats}


async def main() -> int:
    logger.info("╔══════════════════════════════════════════════════════════════╗")
    logger.info("║  Intelligence Pipeline — Integration Smoke Test            ║")
    logger.info("╚══════════════════════════════════════════════════════════════╝")

    results = {}

    # 1. Discord collection
    results["discord"] = await run_discord_cycle()

    # 2. Interpretation
    results["interpretation"] = await run_interpretation_cycle()

    # 3. Position monitor
    results["position_monitor"] = await run_position_monitor_cycle()

    # 4. Guru
    results["guru"] = await run_guru_cycle()

    # 5. Database state
    results["database"] = await query_database()

    # Summary
    logger.info("")
    logger.info("╔══════════════════════════════════════════════════════════════╗")
    logger.info("║  SUMMARY                                                     ║")
    logger.info("╚══════════════════════════════════════════════════════════════╝")
    for name, res in results.items():
        status = res.get("status", "unknown")
        icon = "✅" if status == "ok" else "❌"
        logger.info(f"{icon} {name}: {status}")

    return 0


if __name__ == "__main__":
    asyncio.run(main())
