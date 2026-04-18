"""Configuration loader for Tickles V2 services.

Reads from environment variables with sensible defaults.
Supports .env file loading via python-dotenv.

V2 Postgres edition — same interface as the MySQL version but with
extra fields for Postgres multi-database, ClickHouse, Redis, and MemU.
"""

import logging
import os
from pathlib import Path

logger: logging.Logger = logging.getLogger(__name__)


def _get_int_from_env(key: str, default: int) -> int:
    """Safely read an int env var; fall back to *default* if missing/invalid."""
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
    """Safely read a bool env var; accepts true/1/yes for truthy."""
    value_str = os.environ.get(key, str(default))
    return value_str.lower() in ('true', '1', 'yes')


def load_env():
    """Load a .env file from the nearest project root, if dotenv is installed."""
    try:
        from dotenv import load_dotenv
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

# --- Database (Postgres) ------------------------------------------------------
DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = _get_int_from_env("DB_PORT", 5432)
DB_USER = os.environ.get("DB_USER", "admin")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
# Logical DB names
DB_NAME_SHARED = os.environ.get("DB_NAME_SHARED", "tickles_shared")
DB_NAME_COMPANY = os.environ.get("DB_NAME_COMPANY", "tickles_jarvais")
# Legacy alias — some code still reads config.DB_NAME directly
DB_NAME = os.environ.get("DB_NAME", DB_NAME_SHARED)
DB_POOL_SIZE = _get_int_from_env("DB_POOL_SIZE", 10)

# --- ClickHouse (raw backtest sweeps) -----------------------------------------
CH_HOST = os.environ.get("CH_HOST", "127.0.0.1")
CH_PORT = _get_int_from_env("CH_PORT", 9000)
CH_HTTP_PORT = _get_int_from_env("CH_HTTP_PORT", 8123)
CH_USER = os.environ.get("CH_USER", "admin")
CH_PASSWORD = os.environ.get("CH_PASSWORD", "")
CH_DATABASE = os.environ.get("CH_DATABASE", "backtests")

# --- Redis (agent coordination, task queues) ----------------------------------
REDIS_HOST = os.environ.get("REDIS_HOST", "127.0.0.1")
REDIS_PORT = _get_int_from_env("REDIS_PORT", 6379)
REDIS_DB = _get_int_from_env("REDIS_DB", 0)

# --- MemU (shared agent learning) ---------------------------------------------
MEMU_ENABLED = _get_bool_from_env("MEMU_ENABLED", True)
MEMU_DB_NAME = os.environ.get("MEMU_DB_NAME", "memu")
MEMU_LLM_MODEL = os.environ.get("MEMU_LLM_MODEL", "openrouter/openai/gpt-4.1")
MEMU_EMBED_MODEL = os.environ.get("MEMU_EMBED_MODEL", "all-MiniLM-L6-v2")

# --- Exchange API keys (never log these) -------------------------------------
BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
BLOFIN_API_KEY = os.environ.get("BLOFIN_API_KEY", "")
BLOFIN_API_SECRET = os.environ.get("BLOFIN_API_SECRET", "")

# --- Exchange settings --------------------------------------------------------
BYBIT_SANDBOX = _get_bool_from_env("BYBIT_SANDBOX", True)
DEFAULT_EXCHANGE = os.environ.get("DEFAULT_EXCHANGE", "bybit")
SUPPORTED_EXCHANGES = {"bybit", "blofin", "bitget", "capitalcom"}
if DEFAULT_EXCHANGE not in SUPPORTED_EXCHANGES:
    logger.warning(
        "Unsupported DEFAULT_EXCHANGE '%s'. Defaulting to 'bybit'.", DEFAULT_EXCHANGE
    )
    DEFAULT_EXCHANGE = "bybit"

# --- Logging ------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
if LOG_LEVEL not in VALID_LOG_LEVELS:
    logger.warning("Invalid LOG_LEVEL '%s'. Defaulting to INFO.", LOG_LEVEL)
    LOG_LEVEL = "INFO"

# --- Candle collection --------------------------------------------------------
CANDLE_FETCH_BATCH_SIZE = _get_int_from_env("CANDLE_FETCH_BATCH_SIZE", 1000)
CANDLE_FETCH_DELAY_MS = _get_int_from_env("CANDLE_FETCH_DELAY_MS", 300)
GAP_DETECTION_ENABLED = _get_bool_from_env("GAP_DETECTION_ENABLED", True)

# --- News collection ----------------------------------------------------------
RSS_FETCH_TIMEOUT = _get_int_from_env("RSS_FETCH_TIMEOUT", 8)
RSS_USER_AGENT = os.environ.get("RSS_USER_AGENT", "TicklesV2/1.0")
