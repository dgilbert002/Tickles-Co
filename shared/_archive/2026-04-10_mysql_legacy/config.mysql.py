"""Configuration loader for Tickles V2 services.

Reads from environment variables with sensible defaults.
Supports .env file loading via python-dotenv.
"""

import os
import logging
from pathlib import Path

logger: logging.Logger = logging.getLogger(__name__)

def _get_int_from_env(key: str, default: int) -> int:
    """Safely get an integer from an environment variable."""
    value_str = os.environ.get(key)
    if value_str is None:
        return default
    try:
        return int(value_str)
    except (ValueError, TypeError):
        logger.warning(
            "Invalid value for %s: '%s'. Using default: %d",
            key, value_str, default
        )
        return default

def _get_bool_from_env(key: str, default: bool) -> bool:
    """Safely get a boolean from an environment variable."""
    value_str = os.environ.get(key, str(default))
    return value_str.lower() in ('true', '1', 'yes')


def load_env():
    """Load .env file from project root if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
        # Start from the current file and traverse up to find the project root
        # that contains the .env file.
        current_path = Path(__file__).resolve().parent
        project_root = None
        for path in [current_path, *current_path.parents]:
            if (path / ".env").exists():
                project_root = path
                break
        
        if project_root:
            env_path = project_root / ".env"
            load_dotenv(dotenv_path=env_path)
            logger.info("Loaded .env file from: %s", env_path)
        else:
            logger.info(".env file not found, using environment variables only.")
            
    except ImportError:
        logger.info("python-dotenv not installed, skipping .env file loading.")


# Auto-load on import
load_env()

# --- Database ---
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = _get_int_from_env("DB_PORT", 3306)
DB_USER = os.environ.get("DB_USER", "tickles_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME = os.environ.get("DB_NAME", "tickles_shared")
DB_POOL_SIZE = _get_int_from_env("DB_POOL_SIZE", 10)

# --- Exchange API keys (never log these) ---
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
BLOFIN_API_KEY = os.environ.get("BLOFIN_API_KEY", "")
BLOFIN_API_SECRET = os.environ.get("BLOFIN_API_SECRET", "")

# --- Exchange settings ---
BYBIT_SANDBOX = _get_bool_from_env("BYBIT_SANDBOX", True)
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "bybit")
SUPPORTED_EXCHANGES = {"bybit", "blofin", "bitget", "capitalcom"}
if DEFAULT_EXCHANGE not in SUPPORTED_EXCHANGES:
    logger.warning(
        "Unsupported DEFAULT_EXCHANGE '%s'. Defaulting to 'bybit'.", DEFAULT_EXCHANGE
    )
    DEFAULT_EXCHANGE = "bybit"

# --- Logging ---
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
if LOG_LEVEL not in VALID_LOG_LEVELS:
    logger.warning("Invalid LOG_LEVEL '%s'. Defaulting to INFO.", LOG_LEVEL)
    LOG_LEVEL = "INFO"

# --- Candle collection ---
CANDLE_FETCH_BATCH_SIZE = _get_int_from_env("CANDLE_FETCH_BATCH_SIZE", 1000)
CANDLE_FETCH_DELAY_MS = _get_int_from_env("CANDLE_FETCH_DELAY_MS", 300)
GAP_DETECTION_ENABLED = _get_bool_from_env("GAP_DETECTION_ENABLED", True)

# --- News collection ---
RSS_FETCH_TIMEOUT = _get_int_from_env("RSS_FETCH_TIMEOUT", 8)
RSS_USER_AGENT = os.environ.get("RSS_USER_AGENT", "TicklesV2/1.0")
