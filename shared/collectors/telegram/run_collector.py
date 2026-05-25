"""Run the Telegram collector as a long-lived daemon."""
import asyncio, logging, os, signal, sys, time
sys.path.insert(0, "/opt/tickles")

from dotenv import load_dotenv
load_dotenv("/opt/tickles/.env")

from shared.collectors.telegram.telegram_collector import TelegramCollector

COLLECT_INTERVAL = int(os.environ.get("TELEGRAM_COLLECT_INTERVAL_S", "120"))

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("tickles.telegram.daemon")
    logger.info("Telegram collector daemon starting (interval=%ds)", COLLECT_INTERVAL)
    
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    
    collector = TelegramCollector()
    
    while not stop.is_set():
        try:
            items = await collector.collect()
            logger.info("Cycle complete: %d items collected", len(items))
        except Exception as exc:
            logger.exception("Collection cycle failed: %s", exc)
        
        try:
            await asyncio.wait_for(stop.wait(), timeout=COLLECT_INTERVAL)
        except asyncio.TimeoutError:
            pass
    
    logger.info("Telegram collector daemon stopped")

if __name__ == "__main__":
    asyncio.run(main())
