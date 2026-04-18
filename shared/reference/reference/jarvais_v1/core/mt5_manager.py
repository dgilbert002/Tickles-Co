"""
JarvAIs MT5 Manager
Manages MT5 connection, path configuration, and status detection.

This is a singleton manager that handles:
- MT5 path configuration and validation
- Connection status detection
- Auto-detection of MT5 installation
- Graceful fallback when MT5 is unavailable

Usage:
    from core.mt5_manager import MT5Manager, get_mt5_manager
    
    manager = get_mt5_manager()
    manager.configure(path="C:\\Program Files\\MetaTrader 5\\terminal64.exe")
    
    if manager.is_connected():
        candles = manager.get_candles("XAUUSD", "M5", 100)
"""

import os
import sys
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass

from core.time_utils import utcnow

logger = logging.getLogger("jarvais.mt5_manager")

# Attempt to import MetaTrader5 — will fail on non-Windows
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    logger.info("MetaTrader5 package not available. MT5 functions will be disabled.")


# ─────────────────────────────────────────────────────────────────────
# Timeframe Mapping
# ─────────────────────────────────────────────────────────────────────

TIMEFRAME_MAP = {}
if MT5_AVAILABLE:
    TIMEFRAME_MAP = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
        "W1": mt5.TIMEFRAME_W1,
    }


@dataclass
class CandleData:
    """OHLCV candle data."""
    time: datetime = None
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0


# ─────────────────────────────────────────────────────────────────────
# MT5 Manager Singleton
# ─────────────────────────────────────────────────────────────────────

class MT5Manager:
    """
    Singleton manager for MT5 connection.
    Handles path configuration, connection status, and graceful fallback.
    """
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        
        self.mt5_path: Optional[str] = None
        self.login: Optional[int] = None
        self.password: Optional[str] = None
        self.server: Optional[str] = None
        self.timeout: int = 15  # Default 15 seconds
        self._connected = False
        self._last_error: Optional[str] = None
        self._last_success: Optional[datetime] = None
        
    @classmethod
    def get_instance(cls) -> 'MT5Manager':
        """Get the singleton instance."""
        return cls()
    
    # ── Configuration ───────────────────────────────────────────────
    
    def configure(self, path: str = None, login: int = None, 
                  password: str = None, server: str = None, timeout: int = None) -> Dict[str, Any]:
        """
        Configure MT5 connection parameters.
        Returns config dict with status.
        """
        if path:
            self.mt5_path = path
        if login is not None:
            self.login = login
        if password:
            self.password = password
        if server:
            self.server = server
        if timeout is not None:
            self.timeout = timeout
            
        logger.info(f"[MT5Manager] Configured: path={self.mt5_path}, login={self.login}, server={self.server}, timeout={self.timeout}s")
        
        return {
            "path": self.mt5_path,
            "login": self.login,
            "server": self.server,
            "timeout": self.timeout,
            "configured": bool(self.mt5_path)
        }
    
    def load_from_config(self, config: Dict) -> None:
        """Load configuration from dict (e.g., system_config)."""
        self.mt5_path = config.get('mt5_path', '')
        login_str = config.get('mt5_login', '')
        self.login = int(login_str) if login_str else None
        self.password = config.get('mt5_password', '')
        self.server = config.get('mt5_server', '')
        self.timeout = int(config.get('mt5_timeout', 15))
        
    # ── Path Validation ─────────────────────────────────────────────
    
    def detect_mt5_path(self) -> Optional[str]:
        """
        Try to auto-detect MT5 installation path.
        Returns path if found, None otherwise.
        """
        if not sys.platform.startswith('win'):
            return None
            
        # Common MT5 installation paths
        common_paths = [
            os.path.expandvars(r"%ProgramFiles%\MetaTrader 5\terminal64.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\MetaTrader 5\terminal64.exe"),
            os.path.expandvars(r"%ProgramFiles%\MetaTrader 5\metatrader64.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\MetaTrader 5\metatrader64.exe"),
            os.path.expanduser(r"~\AppData\Roaming\MetaQuotes\Terminal\*\terminal64.exe"),
        ]
        
        # Also check common broker installations
        broker_folders = [
            "ICMarkets", "Pepperstone", "XM", "FxPro", "OANDA", "IG", "Forex.com",
            "AdmiralMarkets", "RoboForex", "FXTM", "Exness", "Tickmill"
        ]
        
        for broker in broker_folders:
            common_paths.extend([
                os.path.expandvars(f"%ProgramFiles%\\{broker} MetaTrader 5\\terminal64.exe"),
                os.path.expandvars(f"%ProgramFiles(x86)%\\{broker} MetaTrader 5\\terminal64.exe"),
                os.path.expanduser(f"~\\AppData\\Roaming\\MetaQuotes\\Terminal\\{broker}\\terminal64.exe"),
            ])
        
        for path in common_paths:
            # Handle wildcards
            if '*' in path:
                import glob
                matches = glob.glob(path)
                if matches:
                    return matches[0]
            elif os.path.isfile(path):
                logger.info(f"[MT5Manager] Auto-detected MT5 at: {path}")
                return path
                
        return None
    
    def validate_path(self, path: str) -> Tuple[bool, str]:
        """
        Check if path is valid MT5 installation.
        Returns (valid, message).
        """
        if not path:
            return False, "Path is empty"
            
        if not os.path.exists(path):
            return False, f"Path does not exist: {path}"
            
        if not os.path.isfile(path):
            return False, f"Path is not a file: {path}"
            
        # Check if it looks like MT5
        basename = os.path.basename(path).lower()
        if 'terminal' not in basename and 'metatrader' not in basename:
            return False, f"File does not appear to be MT5 terminal: {basename}"
            
        # Try to get file version info (Windows only)
        try:
            import win32api
            info = win32api.GetFileVersionInfo(path, "\\")
            if info:
                logger.info(f"[MT5Manager] Validated MT5: {path}")
                return True, "Valid MT5 installation"
        except:
            pass
            
        # If we can't verify, assume it's valid if it exists
        return True, "Path exists (version check skipped)"
    
    # ── Connection Status ───────────────────────────────────────────
    
    def is_available(self) -> bool:
        """Check if MT5 package is available (Windows only)."""
        return MT5_AVAILABLE
        
    def is_configured(self) -> bool:
        """Check if MT5 path is configured."""
        return bool(self.mt5_path)
        
    def is_running(self) -> bool:
        """Check if MT5 terminal is running."""
        if not sys.platform.startswith('win'):
            return False
            
        try:
            import psutil
            for proc in psutil.process_iter(['name']):
                if proc.info['name'] and 'terminal64' in proc.info['name'].lower():
                    return True
        except:
            # Fallback: try to find MT5 window
            try:
                import win32gui
                def callback(hwnd, windows):
                    title = win32gui.GetWindowText(hwnd)
                    if 'MetaTrader 5' in title or 'MT5' in title:
                        windows.append(title)
                    return True
                windows = []
                win32gui.EnumWindows(callback, windows)
                return len(windows) > 0
            except:
                pass
                
        return False
    
    def is_connected(self) -> bool:
        """Check if we have an active MT5 connection."""
        return self._connected and MT5_AVAILABLE
    
    # ── Connection Management ───────────────────────────────────────
    
    def connect(self) -> Tuple[bool, str]:
        """
        Attempt to connect to MT5.
        Returns (success, message).
        """
        if not MT5_AVAILABLE:
            return False, "MT5 package not available (non-Windows or not installed)"
            
        if not self.is_running():
            return False, "MT5 terminal is not running. Please start MT5 first."
            
        try:
            # When MT5 is already running, don't pass path - connect to existing instance
            # Passing path tries to create a new process which fails with "Process create failed"
            has_password = bool(self.password)
            logger.info(f"[MT5Manager] connect: login={self.login}, server={self.server}, password={'***' if has_password else 'MISSING'}, timeout={self.timeout}s")
            
            if not has_password:
                return False, "MT5 password not configured - check config.json mt5_password"
            
            import threading
            import time
            
            result = {'success': False, 'error': 'Timeout waiting for MT5 connection'}
            
            def _connect():
                try:
                    if self.login and self.password and self.server:
                        if mt5.initialize(
                            login=self.login,
                            password=self.password,
                            server=self.server
                        ):
                            result['success'] = True
                            result['error'] = None
                        else:
                            result['error'] = f"MT5 initialize failed: {mt5.last_error()}"
                    else:
                        result['error'] = "Missing credentials (login/password/server)"
                except Exception as e:
                    result['error'] = str(e)
            
            # Run connect in daemon thread with timeout (daemon=True prevents blocking on timeout)
            thread = threading.Thread(target=_connect, daemon=True)
            thread.start()
            thread.join(timeout=self.timeout)
            
            if thread.is_alive():
                self._last_error = f"MT5 connection timed out after {self.timeout}s (thread still running)"
                return False, self._last_error
            
            if not result['success']:
                self._last_error = result['error']
                return False, self._last_error
            
            self._connected = True
            self._last_success = utcnow()
            self._last_error = None
            
            # Log connection info
            account_info = mt5.account_info()
            if account_info:
                logger.info(f"[MT5Manager] Connected: login={account_info.login}, server={account_info.server}")
            
            return True, "Connected successfully"
            
        except Exception as e:
            self._connected = False
            self._last_error = str(e)
            return False, f"Connection error: {e}"
    
    def disconnect(self) -> None:
        """Disconnect from MT5."""
        if MT5_AVAILABLE:
            mt5.shutdown()
        self._connected = False
        logger.info("[MT5Manager] Disconnected")
    
    def ensure_connected(self) -> Tuple[bool, str]:
        """
        Ensure we have a connection. Connect if needed.
        Returns (success, message).
        """
        if self._connected:
            # Verify connection is still valid
            try:
                info = mt5.account_info()
                if info:
                    return True, "Already connected"
            except:
                self._connected = False
                
        return self.connect()
    
    # ── Status for UI ───────────────────────────────────────────────
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get full status for UI display.
        """
        status = {
            "available": MT5_AVAILABLE,
            "configured": self.is_configured(),
            "running": self.is_running() if MT5_AVAILABLE else False,
            "connected": self._connected,
            "path": self.mt5_path,
            "login": self.login,
            "server": self.server,
            "error": self._last_error,
            "last_success": self._last_success.isoformat() if self._last_success else None
        }
        
        # Add account info if connected
        if self._connected and MT5_AVAILABLE:
            try:
                info = mt5.account_info()
                if info:
                    status["account"] = {
                        "login": info.login,
                        "server": info.server,
                        "balance": info.balance,
                        "equity": info.equity,
                        "currency": info.currency,
                    }
            except:
                pass
                
        return status
    
    # ── Candle Data ─────────────────────────────────────────────────
    
    def get_candles(self, symbol: str, timeframe: str = "M5", 
                    count: int = 500) -> List[CandleData]:
        """
        Fetch candles from MT5.
        
        Args:
            symbol: MT5 symbol name (e.g., "XAUUSD", "EURUSD")
            timeframe: Timeframe string (M1, M5, M15, H1, etc.)
            count: Number of candles to fetch
            
        Returns:
            List of CandleData objects
        """
        if not MT5_AVAILABLE:
            logger.warning("[MT5Manager] Cannot get candles: MT5 not available")
            return []
            
        # Ensure connection
        ok, msg = self.ensure_connected()
        if not ok:
            logger.warning(f"[MT5Manager] Cannot get candles: {msg}")
            return []
        
        # Map timeframe
        tf = TIMEFRAME_MAP.get(timeframe)
        if not tf:
            logger.warning(f"[MT5Manager] Unknown timeframe: {timeframe}")
            return []
        
        try:
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
            if rates is None or len(rates) == 0:
                error = mt5.last_error()
                logger.warning(f"[MT5Manager] No candles returned for {symbol}: {error}")
                return []
            
            candles = []
            for rate in rates:
                candle = CandleData(
                    time=datetime.fromtimestamp(rate['time']),
                    open=float(rate['open']),
                    high=float(rate['high']),
                    low=float(rate['low']),
                    close=float(rate['close']),
                    volume=int(rate['tick_volume'])
                )
                candles.append(candle)
            
            self._last_success = utcnow()
            logger.info(f"[MT5Manager] Fetched {len(candles)} candles for {symbol} {timeframe}")
            return candles
            
        except Exception as e:
            self._last_error = str(e)
            logger.error(f"[MT5Manager] Error fetching candles: {e}")
            return []
    
    def get_candles_from(self, symbol: str, timeframe: str,
                         from_date: datetime, to_date: datetime = None) -> List[CandleData]:
        """
        Fetch candles from MT5 for a date range.
        
        Args:
            symbol: MT5 symbol name
            timeframe: Timeframe string
            from_date: Start date
            to_date: End date (default: now)
            
        Returns:
            List of CandleData objects
        """
        if not MT5_AVAILABLE:
            return []
            
        ok, msg = self.ensure_connected()
        if not ok:
            return []
        
        tf = TIMEFRAME_MAP.get(timeframe)
        if not tf:
            return []
        
        if to_date is None:
            to_date = utcnow()
        
        try:
            from_ts = int(from_date.timestamp())
            to_ts = int(to_date.timestamp())
            
            rates = mt5.copy_rates_range(symbol, tf, from_ts, to_ts)
            if rates is None or len(rates) == 0:
                return []
            
            candles = []
            for rate in rates:
                candle = CandleData(
                    time=datetime.fromtimestamp(rate['time']),
                    open=float(rate['open']),
                    high=float(rate['high']),
                    low=float(rate['low']),
                    close=float(rate['close']),
                    volume=int(rate['tick_volume'])
                )
                candles.append(candle)
            
            return candles
            
        except Exception as e:
            self._last_error = str(e)
            logger.error(f"[MT5Manager] Error fetching candles range: {e}")
            return []
    
    def get_current_price(self, symbol: str) -> Optional[Tuple[float, float]]:
        """
        Get current bid/ask prices for a symbol.
        Returns (bid, ask) or None.
        """
        if not MT5_AVAILABLE:
            return None
            
        ok, msg = self.ensure_connected()
        if not ok:
            return None
        
        try:
            tick = mt5.symbol_info_tick(symbol)
            if tick:
                return (tick.bid, tick.ask)
        except:
            pass
        return None


# ─────────────────────────────────────────────────────────────────────
# Convenience function
# ─────────────────────────────────────────────────────────────────────

def get_mt5_manager() -> MT5Manager:
    """Get the MT5Manager singleton instance."""
    return MT5Manager.get_instance()
