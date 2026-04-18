"""Seed the instruments table with Bybit markets.

Filters to USDT perpetual futures and major spot pairs.
Uses CCXT synchronously and inserts with INSERT IGNORE.
"""

import ccxt
import logging
import os
import pymysql
import sys
from typing import List, Dict, Any

# Add project root to path for absolute imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from shared.utils import config

logger = logging.getLogger("tickles.migration.seed_instruments")
logging.basicConfig(level=logging.INFO)

def get_db_connection():
    """Create a synchronous MySQL connection."""
    try:
        conn = pymysql.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", "3306")),
            user=os.environ.get("DB_USER", "admin"),
            password=os.environ.get("DB_PASSWORD", ""),
            db="tickles_shared",
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor
        )
        conn.ping()  # Test the connection
        return conn
    except pymysql.Error as e:
        logger.error("Failed to connect to database: %s", e)
        logger.info("Please set DB_HOST, DB_PORT, DB_USER, and DB_PASSWORD environment variables")
        raise

def get_bybit_markets() -> List[Dict[str, Any]]:
    """Fetch Bybit markets using CCXT."""
    exchange = ccxt.bybit({
        "apiKey": config.BYBIT_API_KEY,
        "secret": config.BYBIT_API_SECRET,
    })
    
    markets = exchange.load_markets()
    instruments = []
    
    # Filter for USDT perpetual futures and major spot pairs
    for symbol, market in markets.items():
        if (
            (market.get("type") == "swap" and market.get("quote") == "USDT") or
            (market.get("type") == "spot" and symbol in ["BTC/USDT", "ETH/USDT", "SOL/USDT"])
        ):
            instruments.append({
                "exchange": "bybit",
                "symbol": symbol,
                "base_currency": market.get("base"),
                "quote_currency": market.get("quote"),
                "asset_class": "crypto",
                "min_size": market.get("limits", {}).get("amount", {}).get("min", 0.001),
                "max_size": market.get("limits", {}).get("amount", {}).get("max", None),
                "size_increment": market.get("precision", {}).get("amount", 0.001),
                "contract_multiplier": market.get("contractSize") or 1.0,
                "maker_fee_pct": 0.0002,
                "taker_fee_pct": 0.0005,
                "is_active": 1,
            })
    
    return instruments

def seed_instruments(instruments: List[Dict[str, Any]]) -> Dict[str, int]:
    """Insert instruments into the database."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # Count existing instruments for logging
            cursor.execute("SELECT COUNT(*) as count FROM instruments")
            before_count = cursor.fetchone()["count"]
            
            # Insert with IGNORE to skip duplicates
            sql = """
            INSERT INTO instruments (
                exchange, symbol, base_currency, quote_currency, asset_class, min_size,
                max_size, size_increment, contract_multiplier, maker_fee_pct, taker_fee_pct, is_active
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                min_size = VALUES(min_size),
                max_size = VALUES(max_size),
                size_increment = VALUES(size_increment),
                contract_multiplier = VALUES(contract_multiplier),
                maker_fee_pct = VALUES(maker_fee_pct),
                taker_fee_pct = VALUES(taker_fee_pct),
                is_active = VALUES(is_active),
                last_synced_at = NOW(3)
            """
            values = [
                (
                    inst["exchange"],
                    inst["symbol"],
                    inst["base_currency"],
                    inst["quote_currency"],
                    inst["asset_class"],
                    inst["min_size"],
                    inst["max_size"],
                    inst["size_increment"],
                    inst["contract_multiplier"],
                    inst["maker_fee_pct"],
                    inst["taker_fee_pct"],
                    inst["is_active"],
                )
                for inst in instruments
            ]
            
            cursor.executemany(sql, values)
            inserted = cursor.rowcount
            
            # Count after insert for logging
            cursor.execute("SELECT COUNT(*) as count FROM instruments")
            after_count = cursor.fetchone()["count"]
            
            conn.commit()
            
            return {
                "total_found": len(instruments),
                "inserted": inserted,
                "skipped": len(instruments) - inserted,
                "before_count": before_count,
                "after_count": after_count,
            }
    finally:
        conn.close()

if __name__ == "__main__":
    logger.info("Starting instrument seeding process")
    
    try:
        instruments = get_bybit_markets()
        logger.info("Found %d instruments from Bybit", len(instruments))
        
        if not instruments:
            logger.warning("No instruments found - check API connection")
            sys.exit(1)
            
        results = seed_instruments(instruments)
        
        logger.info(
            "Instrument seeding complete:\n"
            "  Total found: %d\n"
            "  Inserted: %d\n"
            "  Skipped (duplicates): %d\n"
            "  Total before: %d\n"
            "  Total after: %d",
            results["total_found"],
            results["inserted"],
            results["skipped"],
            results["before_count"],
            results["after_count"],
        )
    except (pymysql.Error, ccxt.BaseError) as e:
        logger.error("Failed to seed instruments: %s", e, exc_info=True)
        sys.exit(1)