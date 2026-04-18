"""
Module: seed_indicator_catalog
Purpose: Seed the tickles_shared.indicator_catalog table with indicators from Capital 2.0 and JarvAIs V1
Location: /opt/tickles/shared/migration/
"""

import re
import ast
import logging
import json
import os
import sys
import pymysql
from pymysql import Error as PyMySQLError
from typing import Dict, Any, List

# Add project root to path for absolute imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

logger = logging.getLogger("tickles.migration.seed_indicator_catalog")

# Constants
INDICATOR_FILE = "/opt/tickles/shared/reference/reference/capital2/python_engine/indicators_comprehensive.py"

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

# JarvAIs V1 SMC indicators (not in Capital 2.0)
JARVAIS_INDICATORS = {
    "smc_order_block_bullish": {
        "direction": "bullish",
        "category": "smart_money",
        "description": "Smart Money Concept Order Block (Bullish)",
        "params": {},
        "param_ranges": {}
    },
    "smc_order_block_bearish": {
        "direction": "bearish",
        "category": "smart_money",
        "description": "Smart Money Concept Order Block (Bearish)",
        "params": {},
        "param_ranges": {}
    },
    "smc_fvg_bullish": {
        "direction": "bullish",
        "category": "smart_money",
        "description": "Fair Value Gap (Bullish)",
        "params": {},
        "param_ranges": {}
    },
    "smc_fvg_bearish": {
        "direction": "bearish",
        "category": "smart_money",
        "description": "Fair Value Gap (Bearish)",
        "params": {},
        "param_ranges": {}
    },
    "smc_bos_bullish": {
        "direction": "bullish",
        "category": "smart_money",
        "description": "Break of Structure (Bullish)",
        "params": {},
        "param_ranges": {}
    },
    "smc_bos_bearish": {
        "direction": "bearish",
        "category": "smart_money",
        "description": "Break of Structure (Bearish)",
        "params": {},
        "param_ranges": {}
    },
    "smc_choch_bullish": {
        "direction": "bullish",
        "category": "smart_money",
        "description": "Change of Character (Bullish)",
        "params": {},
        "param_ranges": {}
    },
    "smc_choch_bearish": {
        "direction": "bearish",
        "category": "smart_money",
        "description": "Change of Character (Bearish)",
        "params": {},
        "param_ranges": {}
    },
    "smc_liquidity_grab": {
        "direction": "neutral",
        "category": "smart_money",
        "description": "Liquidity Grab",
        "params": {},
        "param_ranges": {}
    },
    "smc_amd_cycle": {
        "direction": "neutral",
        "category": "smart_money",
        "description": "AMD Cycle",
        "params": {},
        "param_ranges": {}
    },
    "confluence_score": {
        "direction": "neutral",
        "category": "combination",
        "description": "Multi-indicator agreement score",
        "params": {},
        "param_ranges": {}
    }
}

def extract_indicator_metadata() -> Dict[str, Any]:
    """Extract INDICATOR_METADATA dict from indicators_comprehensive.py without importing it."""
    try:
        with open(INDICATOR_FILE, 'r') as f:
            content = f.read()
            
        # Find the INDICATOR_METADATA dict using regex
        pattern = r'INDICATOR_METADATA\s*=\s*({.*?})\s*(?=\n\w|\Z)'
        match = re.search(pattern, content, re.DOTALL)
        
        if not match:
            raise ValueError("Could not find INDICATOR_METADATA in file")
            
        # Pre-process the dict string to handle list(range()) calls
        dict_str = match.group(1)
        dict_str = re.sub(r'list\(range\((\d+),\s*(\d+)\)\)', 
                         lambda m: str(list(range(int(m.group(1)), int(m.group(2))))), 
                         dict_str)
        
        # Safely evaluate the dict string
        indicator_metadata = ast.literal_eval(dict_str)
        
        # Handle the duplicate 'ttm_squeeze_on' key (keep last occurrence)
        if 'ttm_squeeze_on' in indicator_metadata:
            logger.warning("Duplicate indicator 'ttm_squeeze_on' found - keeping last definition")
            
        return indicator_metadata
    except (IOError, ValueError, SyntaxError) as e:
        logger.error("Error extracting indicator metadata: %s", e, exc_info=True)
        raise

def seed_database(indicators: Dict[str, Any]) -> None:
    """Seed the indicator_catalog table with the extracted indicators."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            values = []
            for name, metadata in indicators.items():
                values.append((
                    name,
                    metadata.get('direction', 'neutral'),
                    metadata.get('category', 'combination'),
                    metadata.get('description', ''),
                    json.dumps(metadata.get('params', {})),
                    json.dumps(metadata.get('param_ranges', {}))
                ))

            sql = """INSERT IGNORE INTO indicator_catalog
                     (name, direction, category, description, default_params, param_ranges)
                     VALUES (%s, %s, %s, %s, %s, %s)"""
            
            inserted_count = cursor.executemany(sql, values)
            conn.commit()
            
            skipped_count = len(values) - inserted_count
            logger.info("Inserted %d new indicators, skipped %d duplicates.", inserted_count, skipped_count)

    except PyMySQLError as err:
        if err.args[0] == 1045:  # ER_ACCESS_DENIED_ERROR
            logger.error("Database access denied")
        elif err.args[0] == 1049:  # ER_BAD_DB_ERROR
            logger.error("Database does not exist")
        else:
            logger.error(f"Database error: {err}")
        raise
    finally:
        if 'conn' in locals() and conn.open:
            cursor.close()
            conn.close()

def main():
    logging.basicConfig(level=logging.INFO)
    
    try:
        # Extract indicators from Capital 2.0
        indicators = extract_indicator_metadata()
        
        # Add JarvAIs V1 indicators
        indicators.update(JARVAIS_INDICATORS)
        
        # Seed database
        seed_database(indicators)
    except (ValueError, IOError, PyMySQLError) as e:
        logger.error("Script failed: %s", e, exc_info=True)
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main())