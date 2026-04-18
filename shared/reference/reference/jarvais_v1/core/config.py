"""
JarvAIs Configuration Manager
Loads, validates, and provides access to all configuration settings.
Supports runtime updates from the UI that take effect immediately.
"""

import json
import os
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from threading import Lock

logger = logging.getLogger("jarvais.config")

# ─────────────────────────────────────────────────────────────────────
# .env file support — loads environment variables from .env if present
# ─────────────────────────────────────────────────────────────────────

def _load_dotenv():
    """Load environment variables from .env file in project root.
    Supports: KEY=value, KEY="value", KEY='value', and # comments.
    Does NOT override existing environment variables.
    Robust on Windows: resolves paths, handles BOM, handles \\r\\n."""
    env_paths = [
        Path(__file__).resolve().parent.parent / ".env",  # project root (resolved)
        Path.cwd().resolve() / ".env",                     # current working directory
    ]
    # Also check if there's an explicit ENV_FILE environment variable
    if os.environ.get("JARVAIS_ENV_FILE"):
        env_paths.insert(0, Path(os.environ["JARVAIS_ENV_FILE"]).resolve())

    loaded = False
    for env_path in env_paths:
        try:
            if env_path.exists():
                logger.info(f"Loading environment from {env_path}")
                with open(env_path, "r", encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip().replace('\r', '')
                        if not line or line.startswith("#"):
                            continue
                        if "=" not in line:
                            continue
                        key, _, value = line.partition("=")
                        key = key.strip()
                        value = value.strip()
                        # Remove surrounding quotes
                        if (value.startswith('"') and value.endswith('"')) or \
                           (value.startswith("'") and value.endswith("'")):
                            value = value[1:-1]
                        # Don't override existing env vars
                        if key not in os.environ:
                            os.environ[key] = value
                            logger.debug(f"Loaded env var: {key}")
                loaded = True
                return  # Stop after first .env found
        except Exception as e:
            logger.warning(f"Failed to load .env from {env_path}: {e}")

    if not loaded:
        # Print to stderr so it's visible even before logging is configured
        import sys
        print(f"[JarvAIs] WARNING: No .env file found. Searched: {[str(p) for p in env_paths]}", file=sys.stderr)
        print(f"[JarvAIs] Create a .env file in the project root with your API keys.", file=sys.stderr)


def _get_env_path_for_write() -> Path:
    """Return the .env file path to use for reading/writing (project root preferred)."""
    env_paths = [
        Path(__file__).resolve().parent.parent / ".env",
        Path.cwd().resolve() / ".env",
    ]
    if os.environ.get("JARVAIS_ENV_FILE"):
        env_paths.insert(0, Path(os.environ["JARVAIS_ENV_FILE"]).resolve())
    for p in env_paths:
        if p.exists():
            return p
    return env_paths[0]


def update_env_file(env_var: str, value: str) -> bool:
    """
    Update or add a single variable in the .env file so changes persist across restarts.
    Used when saving API keys from the dashboard. Returns True if the file was written.
    """
    try:
        env_path = _get_env_path_for_write()
        lines = []
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8-sig") as f:
                lines = f.readlines()
        replaced = False
        key_prefix = env_var + "="
        for i, line in enumerate(lines):
            stripped = line.strip().replace("\r", "")
            if stripped.startswith(env_var + "=") or stripped.startswith(env_var + " ="):
                lines[i] = f"{env_var}={value}\n"
                replaced = True
                break
        if not replaced:
            lines.append(f"{env_var}={value}\n")
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        logger.info(f"Updated .env: {env_path} ({env_var})")
        return True
    except Exception as e:
        logger.warning(f"Failed to update .env for {env_var}: {e}")
        return False


# Load .env on module import
_load_dotenv()

# ─────────────────────────────────────────────────────────────────────
# Data Classes for Type-Safe Configuration
# ─────────────────────────────────────────────────────────────────────

@dataclass
class DatabaseConfig:
    host: str = "localhost"
    port: int = 3306
    user: str = "jarvais"
    password: str = ""
    database: str = "jarvais"

    @property
    def connection_string(self) -> str:
        return f"mysql+pymysql://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"


@dataclass
class QdrantConfig:
    host: str = "localhost"
    port: int = 6333
    collection_name: str = "jarvais_memory"


@dataclass
class AIModelConfig:
    provider: str = "anthropic"
    model: str = "claude-opus-4-6"
    api_key_env: str = "ANTHROPIC_API_KEY"
    api_key_direct: str = ""  # Direct API key (takes priority over env var)
    max_tokens: int = 4096
    temperature: float = 0.3
    cost_per_1m_input: float = 5.00
    cost_per_1m_output: float = 25.00

    @property
    def api_key(self) -> str:
        """Get API key with fallback chain:
        1. Direct key from config.json (api_key_direct)
        2. Environment variable (OPENAI_API_KEY, etc.)
        3. .env file (loaded on module import)
        4. Database config table
        """
        # 1. Direct key from config.json
        if self.api_key_direct:
            return self.api_key_direct
        # 2 & 3. Environment variable (includes .env file values)
        key = os.environ.get(self.api_key_env, "")
        if key:
            return key
        # 4. Try loading from database system_config table
        try:
            from db.database import get_db
            db = get_db()
            row = db.fetch_one(
                "SELECT config_value FROM system_config WHERE config_key = %s",
                (self.api_key_env,)
            )
            if row and row.get('config_value'):
                logger.info(f"API key loaded from system_config for {self.api_key_env}")
                return row['config_value']
        except Exception:
            pass  # DB not available yet, that's fine
        logger.warning(f"No API key found (direct, env var '{self.api_key_env}', .env file, or DB config)")
        return ""


@dataclass
class DashboardConfig:
    host: str = "0.0.0.0"
    port: int = 5000
    secret_key: str = "CHANGE_ME"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_dir: str = "logs"
    max_file_size_mb: int = 50
    backup_count: int = 30


@dataclass
class MaturityThresholds:
    phase_2_min_trades: int = 100
    phase_2_min_positive_alpha_days: int = 30
    phase_3_min_trades: int = 500
    phase_3_min_alpha_multiplier: float = 2.0
    phase_3_min_alpha_days: int = 90


@dataclass
class RiskSettings:
    daily_profit_target_pct: float = 5.0
    daily_loss_limit_pct: float = 10.0
    max_simultaneous_trades: int = 1
    per_trade_risk_pct: float = 2.0
    ai_confidence_threshold: int = 65
    post_target_mode: str = "sentiment_based"
    tp_strategy: str = "ea_default"
    tp1_close_pct: int = 50
    auto_adjust_confidence: bool = False
    free_trade_confidence_offset: int = -15
    maturity_phase_override: str = "auto"

    # Valid options for dropdowns
    VALID_POST_TARGET_MODES = [
        "stop_trading", "continue_cautiously", "free_trades",
        "sentiment_based", "long_only", "short_only"
    ]
    VALID_TP_STRATEGIES = [
        "ea_default", "partial_close", "ai_suggested",
        "trailing_stop", "time_based"
    ]
    VALID_MATURITY_OVERRIDES = ["auto", "1", "2", "3"]


@dataclass
class AccountConfig:
    account_id: str = ""
    name: str = ""
    is_live: bool = False
    enabled: bool = True
    mt5_path: str = ""
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    signal_port: int = 8001
    symbols: List[str] = field(default_factory=lambda: ["XAUUSD"])
    risk_settings: RiskSettings = field(default_factory=RiskSettings)
    ai_model_override: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────
# Configuration Manager (Singleton)
# ─────────────────────────────────────────────────────────────────────

class ConfigManager:
    """
    Thread-safe configuration manager that loads from config.json
    and supports runtime updates from the UI or AI self-adjustment.
    """

    _instance = None
    _lock = Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, config_path: Optional[str] = None):
        if self._initialized:
            return
        self._initialized = True
        self._config_lock = Lock()

        # Determine config path
        if config_path:
            self._config_path = Path(config_path)
        else:
            self._config_path = Path(__file__).parent.parent / "config.json"

        # Initialize all config sections
        self.database = DatabaseConfig()
        self.qdrant = QdrantConfig()
        self.ai_models: Dict[str, AIModelConfig] = {}
        self.dashboard = DashboardConfig()
        self.logging = LoggingConfig()
        self.maturity_thresholds = MaturityThresholds()
        self.accounts: Dict[str, AccountConfig] = {}

        # Load from file
        self._load_config()
        logger.info(f"Configuration loaded from {self._config_path}")

    @property
    def raw(self) -> Dict[str, Any]:
        """Return the raw global config dict for components that need direct access."""
        return self._raw_config

    def _load_config(self):
        """Load configuration from JSON file."""
        self._raw_config = {}
        if not self._config_path.exists():
            logger.warning(f"Config file not found at {self._config_path}, using defaults")
            return

        with open(self._config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        self._raw_config = raw.get("global", {})

        global_cfg = raw.get("global", {})

        # Database — host, port, user, password, database name all from config.json (global.database).
        # Optional: set JARVAIS_DATABASE in environment to override the database name (e.g. jarvais_tradingco on VPS).
        db_cfg = global_cfg.get("database", {})
        self.database = DatabaseConfig(**{k: v for k, v in db_cfg.items()
                                          if k in DatabaseConfig.__dataclass_fields__})
        if os.environ.get("JARVAIS_DATABASE"):
            override_db = os.environ["JARVAIS_DATABASE"].strip()
            self.database = DatabaseConfig(
                host=self.database.host,
                port=self.database.port,
                user=self.database.user,
                password=self.database.password,
                database=override_db,
            )
            logger.info(f"Database name overridden by JARVAIS_DATABASE: {override_db}")

        # Qdrant
        q_cfg = global_cfg.get("qdrant", {})
        self.qdrant = QdrantConfig(**{k: v for k, v in q_cfg.items()
                                      if k in QdrantConfig.__dataclass_fields__})

        # AI Models
        models_cfg = global_cfg.get("ai_models", {})
        self.ai_models = {}
        for role, mcfg in models_cfg.items():
            # Map 'api_key' from config to 'api_key_direct' in dataclass
            parsed = {}
            for k, v in mcfg.items():
                if k == "api_key":
                    parsed["api_key_direct"] = v
                elif k in AIModelConfig.__dataclass_fields__:
                    parsed[k] = v
            self.ai_models[role] = AIModelConfig(**parsed)

        # Dashboard
        dash_cfg = global_cfg.get("dashboard", {})
        self.dashboard = DashboardConfig(**{k: v for k, v in dash_cfg.items()
                                            if k in DashboardConfig.__dataclass_fields__})

        # Logging
        log_cfg = global_cfg.get("logging", {})
        self.logging = LoggingConfig(**{k: v for k, v in log_cfg.items()
                                        if k in LoggingConfig.__dataclass_fields__})

        # Maturity Thresholds
        mat_cfg = global_cfg.get("maturity_thresholds", {})
        self.maturity_thresholds = MaturityThresholds(**{k: v for k, v in mat_cfg.items()
                                                         if k in MaturityThresholds.__dataclass_fields__})

        # Accounts
        self.accounts = {}
        for acct_raw in raw.get("accounts", []):
            # Create a copy to avoid modifying the original dict if it's reused
            acct_raw_copy = acct_raw.copy()
            risk_raw = acct_raw_copy.pop("risk_settings", {})
            risk = RiskSettings(**{k: v for k, v in risk_raw.items()
                                   if k in RiskSettings.__dataclass_fields__})
            acct_data = {k: v for k, v in acct_raw_copy.items()
                         if k in AccountConfig.__dataclass_fields__ and k != "risk_settings"}
            acct = AccountConfig(**acct_data, risk_settings=risk)
            self.accounts[acct.account_id] = acct

    def get_account(self, account_id: str) -> Optional[AccountConfig]:
        """Get configuration for a specific account."""
        return self.accounts.get(account_id)

    def get_enabled_accounts(self) -> List[AccountConfig]:
        """Get all enabled account configurations."""
        return [a for a in self.accounts.values() if a.enabled]

    def get_ai_model(self, role: str = "primary", account_id: Optional[str] = None) -> AIModelConfig:
        """
        Get AI model config for a given role. If an account has an override, use that.
        Falls back to the global model for the specified role.
        """
        if account_id:
            acct = self.accounts.get(account_id)
            if acct and acct.ai_model_override and acct.ai_model_override in self.ai_models:
                return self.ai_models[acct.ai_model_override]
        return self.ai_models.get(role, self.ai_models.get("primary", AIModelConfig()))

    def update_risk_setting(self, account_id: str, key: str, value: Any, updated_by: str = "user") -> bool:
        """
        Update a risk setting for a specific account at runtime.
        Thread-safe. Returns True if successful.
        """
        with self._config_lock:
            acct = self.accounts.get(account_id)
            if not acct:
                logger.error(f"Account {account_id} not found for risk setting update")
                return False

            if not hasattr(acct.risk_settings, key):
                logger.error(f"Invalid risk setting key: {key}")
                return False

            old_value = getattr(acct.risk_settings, key)
            setattr(acct.risk_settings, key, value)
            logger.info(f"[{account_id}] Risk setting '{key}' updated: {old_value} -> {value} (by {updated_by})")
            return True

    def save_to_file(self):
        """Persist current configuration back to config.json."""
        with self._config_lock:
            data = {
                "global": {
                    "database": self.database.__dict__,
                    "qdrant": self.qdrant.__dict__,
                    "ai_models": {k: v.__dict__ for k, v in self.ai_models.items()},
                    "dashboard": self.dashboard.__dict__,
                    "logging": self.logging.__dict__,
                    "maturity_thresholds": self.maturity_thresholds.__dict__
                },
                "accounts": []
            }
            for acct in self.accounts.values():
                acct_dict = {
                    "account_id": acct.account_id,
                    "name": acct.name,
                    "is_live": acct.is_live,
                    "enabled": acct.enabled,
                    "mt5_path": acct.mt5_path,
                    "mt5_login": acct.mt5_login,
                    "mt5_password": acct.mt5_password,
                    "mt5_server": acct.mt5_server,
                    "signal_port": acct.signal_port,
                    "symbols": acct.symbols,
                    "risk_settings": acct.risk_settings.__dict__,
                    "ai_model_override": acct.ai_model_override
                }
                data["accounts"].append(acct_dict)

            with open(self._config_path, "w") as f:
                json.dump(data, f, indent=4)
            logger.info(f"Configuration saved to {self._config_path}")

    def sync_mt5_account_from_ui(self, data: Dict[str, Any], mt5_path: str = "") -> None:
        """
        Sync an MT5 account from the Configuration UI (DB) into config.accounts
        so config.json stays in sync with the database. Call after saving to mt5_accounts.
        """
        login = data.get("mt5_login")
        if not login:
            return
        account_id = f"mt5_{login}"
        # Preserve password if UI sent empty (edit without changing password)
        password = (data.get("mt5_password") or "").strip()
        if not password and account_id in self.accounts:
            password = getattr(self.accounts[account_id], "mt5_password", "") or ""
        with self._config_lock:
            risk = RiskSettings(
                per_trade_risk_pct=float(data.get("max_risk_pct", 1.0)),
                daily_profit_target_pct=5.0,
                daily_loss_limit_pct=10.0,
                max_simultaneous_trades=1,
                ai_confidence_threshold=65,
                post_target_mode="sentiment_based",
                tp_strategy="ea_default",
                tp1_close_pct=50,
                auto_adjust_confidence=False,
                free_trade_confidence_offset=-15,
                maturity_phase_override="auto",
            )
            acct = AccountConfig(
                account_id=account_id,
                name=(data.get("account_name") or str(login)),
                is_live=(data.get("account_type") == "live"),
                enabled=True,
                mt5_path=mt5_path or "",
                mt5_login=int(login) if str(login).isdigit() else 0,
                mt5_password=password,
                mt5_server=(data.get("mt5_server") or ""),
                signal_port=8001,
                symbols=["XAUUSD"],
                risk_settings=risk,
                ai_model_override=None,
            )
            self.accounts[account_id] = acct
        logger.info(f"[Config] Synced MT5 account to config.json: {account_id}")

    def sync_remove_mt5_account(self, mt5_login: str) -> None:
        """Remove an MT5 account from config.accounts by login. Call after deleting from mt5_accounts."""
        account_id = f"mt5_{mt5_login}"
        with self._config_lock:
            if account_id in self.accounts:
                del self.accounts[account_id]
                logger.info(f"[Config] Removed MT5 account from config.json: {account_id}")

    def to_dict(self) -> Dict[str, Any]:
        """Export full configuration as a dictionary (for API/UI)."""
        return {
            "database": self.database.__dict__,
            "qdrant": self.qdrant.__dict__,
            "ai_models": {k: {**v.__dict__, "api_key": "***"} for k, v in self.ai_models.items()},
            "dashboard": {**self.dashboard.__dict__, "secret_key": "***"},
            "logging": self.logging.__dict__,
            "maturity_thresholds": self.maturity_thresholds.__dict__,
            "accounts": {
                aid: {
                    **{k: v for k, v in acct.__dict__.items() if k != "risk_settings"},
                    "mt5_password": "***",
                    "risk_settings": acct.risk_settings.__dict__
                }
                for aid, acct in self.accounts.items()
            }
        }


# ─────────────────────────────────────────────────────────────────────
# Convenience function
# ─────────────────────────────────────────────────────────────────────

def get_config(config_path: Optional[str] = None) -> ConfigManager:
    """Get the singleton ConfigManager instance."""
    return ConfigManager(config_path)
