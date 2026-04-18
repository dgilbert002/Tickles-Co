"""
JarvAIs MT5 Executor
Handles all interaction with MetaTrader 5: trade execution, position management,
account data retrieval, and candle data fetching.

Each account instance has its own MT5Executor that connects to a specific
MT5 terminal installation via the MetaTrader5 Python package.

Note: The MetaTrader5 package only works on Windows. This module will
gracefully handle import errors on non-Windows systems for development.

Usage:
    from core.mt5_executor import get_mt5_executor
    executor = get_mt5_executor("DEMO_001")
    executor.connect()
    balance = executor.get_account_balance()
    result = executor.place_order("XAUUSD", "BUY", 0.1, sl=2340.0, tp=2365.0)
"""

import os
import time
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("jarvais.mt5_executor")

# Attempt to import MetaTrader5 — will fail on non-Windows
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    logger.warning("MetaTrader5 package not available (non-Windows environment). "
                   "MT5 functions will operate in simulation mode.")


# ─────────────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────────────

@dataclass
class OrderResult:
    """Result of a trade execution attempt."""
    success: bool = False
    order_ticket: int = 0
    deal_ticket: int = 0
    volume: float = 0.0
    price: float = 0.0
    symbol: str = ""
    direction: str = ""
    sl: float = 0.0
    tp: float = 0.0
    magic_number: int = 0
    comment: str = ""
    error_code: int = 0
    error_message: str = ""
    retcode: int = 0


@dataclass
class PositionInfo:
    """Information about an open position."""
    ticket: int = 0
    symbol: str = ""
    direction: str = ""  # "BUY" or "SELL"
    volume: float = 0.0
    open_price: float = 0.0
    current_price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    profit: float = 0.0
    swap: float = 0.0
    commission: float = 0.0
    magic_number: int = 0
    comment: str = ""
    open_time: Optional[datetime] = None


@dataclass
class AccountInfo:
    """MT5 account information."""
    login: int = 0
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    free_margin: float = 0.0
    margin_level: float = 0.0
    profit: float = 0.0
    currency: str = "USD"
    leverage: int = 0
    server: str = ""
    name: str = ""
    trade_mode: str = ""  # "demo" or "real"


@dataclass
class CandleData:
    """OHLCV candle data."""
    time: datetime = None
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    tick_volume: int = 0
    spread: int = 0
    real_volume: int = 0


# ─────────────────────────────────────────────────────────────────────
# MT5 Timeframe Mapping
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
        "MN1": mt5.TIMEFRAME_MN1,
    }


# ─────────────────────────────────────────────────────────────────────
# MT5 Executor Class
# ─────────────────────────────────────────────────────────────────────

class MT5Executor:
    """
    Manages all MetaTrader 5 operations for a single account.
    Each account instance has its own executor connected to its own MT5 terminal.
    """

    # Magic number base — each account gets a unique range
    MAGIC_BASE = 100000
    _instance_counter = 0

    def __init__(self, account_id: str, mt5_path: str = None,
                 login: int = None, password: str = None, server: str = None):
        self.account_id = account_id
        self.mt5_path = mt5_path  # Path to terminal64.exe
        self.login = login
        self.password = password
        self.server = server
        self.connected = False
        self._next_magic = 0

        # Assign a unique magic number range for this account
        MT5Executor._instance_counter += 1
        self._magic_base = self.MAGIC_BASE + (MT5Executor._instance_counter * 1000)

        logger.info(f"[{account_id}] MT5Executor created. "
                    f"Magic base: {self._magic_base}, MT5 path: {mt5_path}")

    def connect(self) -> bool:
        """
        Initialize connection to the MT5 terminal.
        Must be called before any other operations.
        """
        if not MT5_AVAILABLE:
            logger.warning(f"[{self.account_id}] MT5 not available — running in simulation mode")
            self.connected = True
            return True

        try:
            # Initialize MT5 - when MT5 is already running, don't pass path
            # Passing path tries to create a new process which fails with "Process create failed"
            init_kwargs = {}
            # Only pass path if MT5 is NOT already running (not implemented here, assume running)
            # if self.mt5_path and not self._is_mt5_running():
            #     init_kwargs["path"] = self.mt5_path
            if self.login:
                init_kwargs["login"] = self.login
            if self.password:
                init_kwargs["password"] = self.password
            if self.server:
                init_kwargs["server"] = self.server
            
            has_password = bool(self.password)
            logger.info(f"[{self.account_id}] MT5 connect: login={self.login}, server={self.server}, password={'***' if has_password else 'MISSING'}")
            
            if not has_password:
                logger.error(f"[{self.account_id}] MT5 password not configured")
                return False

            if not mt5.initialize(**init_kwargs):
                error = mt5.last_error()
                logger.error(f"[{self.account_id}] MT5 initialization failed: {error}")
                return False

            # Verify connection
            account_info = mt5.account_info()
            if account_info is None:
                logger.error(f"[{self.account_id}] Failed to get account info after initialization")
                mt5.shutdown()
                return False

            self.connected = True
            trade_mode = "LIVE" if account_info.trade_mode == 0 else "DEMO"
            logger.info(f"[{self.account_id}] Connected to MT5: "
                        f"Login={account_info.login}, "
                        f"Server={account_info.server}, "
                        f"Mode={trade_mode}, "
                        f"Balance={account_info.balance} {account_info.currency}")
            return True

        except Exception as e:
            logger.error(f"[{self.account_id}] MT5 connection error: {e}", exc_info=True)
            return False

    def disconnect(self):
        """Shutdown the MT5 connection."""
        if MT5_AVAILABLE and self.connected:
            mt5.shutdown()
            self.connected = False
            logger.info(f"[{self.account_id}] MT5 disconnected")

    def _ensure_connected(self):
        """Ensure MT5 is connected, attempt reconnection if not."""
        if not self.connected:
            if not self.connect():
                raise ConnectionError(f"[{self.account_id}] Not connected to MT5")

    def _generate_magic_number(self) -> int:
        """Generate a unique magic number for a new trade."""
        self._next_magic += 1
        return self._magic_base + self._next_magic

    # ─────────────────────────────────────────────────────────────────
    # Account Information
    # ─────────────────────────────────────────────────────────────────

    def get_account_info(self) -> AccountInfo:
        """Get full account information."""
        self._ensure_connected()

        if not MT5_AVAILABLE:
            return AccountInfo(
                login=0, balance=10000.0, equity=10000.0,
                free_margin=10000.0, currency="USD", leverage=100,
                trade_mode="demo"
            )

        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"[{self.account_id}] Failed to get account info: {mt5.last_error()}")

        return AccountInfo(
            login=info.login,
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            free_margin=info.margin_free,
            margin_level=info.margin_level if info.margin_level else 0.0,
            profit=info.profit,
            currency=info.currency,
            leverage=info.leverage,
            server=info.server,
            name=info.name,
            trade_mode="demo" if info.trade_mode != 0 else "live"
        )

    def get_account_balance(self) -> float:
        """Get current account balance."""
        return self.get_account_info().balance

    def get_account_equity(self) -> float:
        """Get current account equity."""
        return self.get_account_info().equity

    # ─────────────────────────────────────────────────────────────────
    # Market Data
    # ─────────────────────────────────────────────────────────────────

    def get_candles(self, symbol: str, timeframe: str = "M5",
                    count: int = 200) -> List[CandleData]:
        """
        Fetch historical candle data from MT5.

        Args:
            symbol: Trading instrument (e.g., "XAUUSD")
            timeframe: Timeframe string (M1, M5, M15, M30, H1, H4, D1, W1, MN1)
            count: Number of candles to fetch

        Returns:
            List of CandleData objects, oldest first
        """
        self._ensure_connected()

        if not MT5_AVAILABLE:
            logger.warning(f"[{self.account_id}] Simulation mode — returning empty candles")
            return []

        tf = TIMEFRAME_MAP.get(timeframe.upper())
        if tf is None:
            raise ValueError(f"Invalid timeframe: {timeframe}. "
                             f"Valid: {list(TIMEFRAME_MAP.keys())}")

        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            error = mt5.last_error()
            logger.warning(f"[{self.account_id}] No candle data for {symbol}/{timeframe}: {error}")
            return []

        candles = []
        for rate in rates:
            candles.append(CandleData(
                time=datetime.utcfromtimestamp(rate['time']),
                open=rate['open'],
                high=rate['high'],
                low=rate['low'],
                close=rate['close'],
                tick_volume=rate['tick_volume'],
                spread=rate['spread'],
                real_volume=rate['real_volume']
            ))

        return candles

    def get_current_price(self, symbol: str) -> Tuple[float, float]:
        """
        Get the current bid/ask price for a symbol.

        Returns:
            Tuple of (bid, ask)
        """
        self._ensure_connected()

        if not MT5_AVAILABLE:
            return (0.0, 0.0)

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"[{self.account_id}] Failed to get tick for {symbol}: {mt5.last_error()}")

        return (tick.bid, tick.ask)

    def get_symbol_info(self, symbol: str) -> Dict[str, Any]:
        """Get symbol specification (point value, lot size, etc.)."""
        self._ensure_connected()

        if not MT5_AVAILABLE:
            return {
                "symbol": symbol,
                "point": 0.01 if "JPY" in symbol else 0.00001,
                "digits": 2 if "XAU" in symbol else 5,
                "trade_contract_size": 100.0 if "XAU" in symbol else 100000.0,
                "volume_min": 0.01,
                "volume_max": 100.0,
                "volume_step": 0.01
            }

        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"[{self.account_id}] Symbol info not found for {symbol}: {mt5.last_error()}")

        return {
            "symbol": info.name,
            "point": info.point,
            "digits": info.digits,
            "trade_contract_size": info.trade_contract_size,
            "volume_min": info.volume_min,
            "volume_max": info.volume_max,
            "volume_step": info.volume_step,
            "trade_tick_value": info.trade_tick_value,
            "trade_tick_size": info.trade_tick_size,
            "spread": info.spread,
            "currency_base": info.currency_base,
            "currency_profit": info.currency_profit,
            "currency_margin": info.currency_margin
        }

    # ─────────────────────────────────────────────────────────────────
    # Trade Execution
    # ─────────────────────────────────────────────────────────────────

    def place_order(self, symbol: str, direction: str, volume: float,
                    sl: float = 0.0, tp: float = 0.0,
                    comment: str = "", magic_number: int = None) -> OrderResult:
        """
        Place a market order.

        Args:
            symbol: Trading instrument
            direction: "BUY" or "SELL"
            volume: Lot size
            sl: Stop loss price (0 = no SL)
            tp: Take profit price (0 = no TP)
            comment: Order comment (e.g., "JarvAIs_signal_123")
            magic_number: Unique identifier. Auto-generated if None.

        Returns:
            OrderResult with execution details
        """
        self._ensure_connected()

        if magic_number is None:
            magic_number = self._generate_magic_number()

        direction_upper = direction.upper()
        if direction_upper not in ("BUY", "SELL"):
            return OrderResult(success=False, error_message=f"Invalid direction: {direction}")

        logger.info(f"[{self.account_id}] Placing order: {direction_upper} {volume} {symbol} "
                    f"SL={sl} TP={tp} Magic={magic_number}")

        if not MT5_AVAILABLE:
            logger.info(f"[{self.account_id}] SIMULATION: Order would be placed")
            return OrderResult(
                success=True, order_ticket=int(time.time()),
                deal_ticket=int(time.time()), volume=volume,
                price=0.0, symbol=symbol, direction=direction_upper,
                sl=sl, tp=tp, magic_number=magic_number,
                comment=comment
            )

        # Ensure symbol is visible in Market Watch
        if not mt5.symbol_select(symbol, True):
            return OrderResult(
                success=False,
                error_message=f"Failed to select symbol {symbol}: {mt5.last_error()}"
            )

        # Get current price
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return OrderResult(
                success=False,
                error_message=f"Failed to get tick for {symbol}: {mt5.last_error()}"
            )

        price = tick.ask if direction_upper == "BUY" else tick.bid
        order_type = mt5.ORDER_TYPE_BUY if direction_upper == "BUY" else mt5.ORDER_TYPE_SELL

        # Build the order request
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 20,  # Max slippage in points
            "magic": magic_number,
            "comment": comment[:31] if comment else f"JarvAIs_{self.account_id}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        # Send the order
        result = mt5.order_send(request)
        if result is None:
            return OrderResult(
                success=False,
                error_message=f"order_send returned None: {mt5.last_error()}"
            )

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"[{self.account_id}] Order failed: retcode={result.retcode}, "
                         f"comment={result.comment}")
            return OrderResult(
                success=False,
                retcode=result.retcode,
                error_code=result.retcode,
                error_message=f"Order failed: {result.comment} (retcode: {result.retcode})"
            )

        logger.info(f"[{self.account_id}] Order executed: ticket={result.order}, "
                    f"deal={result.deal}, price={result.price}, volume={result.volume}")

        return OrderResult(
            success=True,
            order_ticket=result.order,
            deal_ticket=result.deal,
            volume=result.volume,
            price=result.price,
            symbol=symbol,
            direction=direction_upper,
            sl=sl,
            tp=tp,
            magic_number=magic_number,
            comment=comment,
            retcode=result.retcode
        )

    def close_position(self, ticket: int, volume: float = None,
                       comment: str = "") -> OrderResult:
        """
        Close an open position (fully or partially).

        Args:
            ticket: Position ticket number
            volume: Volume to close. None = close entire position.
            comment: Close comment

        Returns:
            OrderResult
        """
        self._ensure_connected()

        if not MT5_AVAILABLE:
            logger.info(f"[{self.account_id}] SIMULATION: Position {ticket} would be closed")
            return OrderResult(success=True, order_ticket=ticket)

        # Get position info
        position = mt5.positions_get(ticket=ticket)
        if not position or len(position) == 0:
            return OrderResult(
                success=False,
                error_message=f"Position {ticket} not found"
            )

        pos = position[0]
        close_volume = volume if volume else pos.volume
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY

        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            return OrderResult(
                success=False,
                error_message=f"Failed to get tick for {pos.symbol}"
            )

        close_price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": close_volume,
            "type": close_type,
            "position": ticket,
            "price": close_price,
            "deviation": 20,
            "magic": pos.magic,
            "comment": comment[:31] if comment else "JarvAIs_close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = mt5.order_send(request)
        if result is None:
            return OrderResult(success=False, error_message=f"Close failed: {mt5.last_error()}")

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return OrderResult(
                success=False,
                retcode=result.retcode,
                error_message=f"Close failed: {result.comment}"
            )

        logger.info(f"[{self.account_id}] Position {ticket} closed: "
                    f"volume={close_volume}, price={result.price}")

        return OrderResult(
            success=True,
            order_ticket=result.order,
            deal_ticket=result.deal,
            volume=close_volume,
            price=result.price,
            symbol=pos.symbol,
            direction="SELL" if pos.type == mt5.ORDER_TYPE_BUY else "BUY",
            retcode=result.retcode
        )

    def partial_close(self, ticket: int, close_pct: float,
                      comment: str = "") -> OrderResult:
        """
        Partially close a position by a percentage.

        Args:
            ticket: Position ticket
            close_pct: Percentage to close (e.g., 0.5 = 50%)
            comment: Close comment

        Returns:
            OrderResult
        """
        self._ensure_connected()

        if not MT5_AVAILABLE:
            return OrderResult(success=True, order_ticket=ticket)

        position = mt5.positions_get(ticket=ticket)
        if not position or len(position) == 0:
            return OrderResult(success=False, error_message=f"Position {ticket} not found")

        pos = position[0]
        symbol_info = mt5.symbol_info(pos.symbol)
        if symbol_info is None:
            return OrderResult(success=False, error_message=f"Symbol info not found for {pos.symbol}")

        # Calculate volume to close, respecting volume step
        close_volume = pos.volume * close_pct
        step = symbol_info.volume_step
        close_volume = round(close_volume / step) * step
        close_volume = max(close_volume, symbol_info.volume_min)
        close_volume = min(close_volume, pos.volume)

        logger.info(f"[{self.account_id}] Partial close: ticket={ticket}, "
                    f"pct={close_pct*100}%, volume={close_volume}/{pos.volume}")

        return self.close_position(ticket, volume=close_volume, comment=comment)

    def modify_position(self, ticket: int, sl: float = None,
                        tp: float = None) -> bool:
        """
        Modify the SL/TP of an open position.

        Args:
            ticket: Position ticket
            sl: New stop loss (None = keep current)
            tp: New take profit (None = keep current)

        Returns:
            True if modification succeeded
        """
        self._ensure_connected()

        if not MT5_AVAILABLE:
            logger.info(f"[{self.account_id}] SIMULATION: Position {ticket} SL/TP modified")
            return True

        position = mt5.positions_get(ticket=ticket)
        if not position or len(position) == 0:
            logger.error(f"[{self.account_id}] Position {ticket} not found for modification")
            return False

        pos = position[0]
        new_sl = sl if sl is not None else pos.sl
        new_tp = tp if tp is not None else pos.tp

        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": ticket,
            "sl": new_sl,
            "tp": new_tp,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            error_msg = result.comment if result else str(mt5.last_error())
            logger.error(f"[{self.account_id}] Modify position {ticket} failed: {error_msg}")
            return False

        logger.info(f"[{self.account_id}] Position {ticket} modified: SL={new_sl}, TP={new_tp}")
        return True

    def move_to_breakeven(self, ticket: int, buffer_points: int = 0) -> bool:
        """
        Move stop loss to break-even (entry price + buffer).

        Args:
            ticket: Position ticket
            buffer_points: Points above/below entry to set SL

        Returns:
            True if successful
        """
        self._ensure_connected()

        if not MT5_AVAILABLE:
            return True

        position = mt5.positions_get(ticket=ticket)
        if not position or len(position) == 0:
            return False

        pos = position[0]
        symbol_info = mt5.symbol_info(pos.symbol)
        if symbol_info is None:
            return False

        buffer = buffer_points * symbol_info.point

        if pos.type == mt5.ORDER_TYPE_BUY:
            new_sl = pos.price_open + buffer
            # Only move SL up, never down
            if new_sl <= pos.sl and pos.sl > 0:
                return True  # Already at or above breakeven
        else:
            new_sl = pos.price_open - buffer
            # Only move SL down for sells, never up
            if new_sl >= pos.sl and pos.sl > 0:
                return True

        return self.modify_position(ticket, sl=new_sl)

    # ─────────────────────────────────────────────────────────────────
    # Position Queries
    # ─────────────────────────────────────────────────────────────────

    def get_open_positions(self, symbol: str = None) -> List[PositionInfo]:
        """
        Get all open positions, optionally filtered by symbol.
        Only returns positions opened by JarvAIs (matching magic number range).
        """
        self._ensure_connected()

        if not MT5_AVAILABLE:
            return []

        if symbol:
            positions = mt5.positions_get(symbol=symbol)
        else:
            positions = mt5.positions_get()

        if positions is None:
            return []

        result = []
        for pos in positions:
            # Filter to only our positions (matching magic number range)
            if pos.magic < self._magic_base or pos.magic >= self._magic_base + 1000:
                continue

            result.append(PositionInfo(
                ticket=pos.ticket,
                symbol=pos.symbol,
                direction="BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL",
                volume=pos.volume,
                open_price=pos.price_open,
                current_price=pos.price_current,
                sl=pos.sl,
                tp=pos.tp,
                profit=pos.profit,
                swap=pos.swap,
                commission=pos.commission if hasattr(pos, 'commission') else 0.0,
                magic_number=pos.magic,
                comment=pos.comment,
                open_time=datetime.utcfromtimestamp(pos.time)
            ))

        return result

    def get_position_count(self, symbol: str = None) -> int:
        """Get the number of open positions (JarvAIs only)."""
        return len(self.get_open_positions(symbol))

    def get_total_open_profit(self) -> float:
        """Get total unrealized P&L across all open positions."""
        positions = self.get_open_positions()
        return sum(p.profit + p.swap for p in positions)

    # ─────────────────────────────────────────────────────────────────
    # Lot Size Calculation
    # ─────────────────────────────────────────────────────────────────

    def calculate_lot_size(self, symbol: str, risk_pct: float,
                           sl_distance_points: float) -> float:
        """
        Calculate the lot size based on risk percentage and SL distance.
        Implements compounding: risk is always based on current balance.

        Args:
            symbol: Trading instrument
            risk_pct: Risk percentage (e.g., 2.0 for 2%)
            sl_distance_points: Distance to stop loss in points

        Returns:
            Calculated lot size, rounded to volume step
        """
        self._ensure_connected()

        if not MT5_AVAILABLE:
            # Simulation: rough calculation
            balance = 10000.0
            risk_amount = balance * (risk_pct / 100)
            lot_size = risk_amount / (sl_distance_points * 10)  # Rough estimate
            return max(0.01, round(lot_size, 2))

        account = mt5.account_info()
        if account is None:
            logger.error(f"[{self.account_id}] Cannot calculate lot size: no account info")
            return 0.01

        symbol_info = mt5.symbol_info(symbol)
        if symbol_info is None:
            logger.error(f"[{self.account_id}] Cannot calculate lot size: no symbol info for {symbol}")
            return 0.01

        # Risk amount in account currency
        risk_amount = account.balance * (risk_pct / 100)

        # Calculate tick value
        tick_value = symbol_info.trade_tick_value
        tick_size = symbol_info.trade_tick_size

        if tick_value <= 0 or tick_size <= 0 or sl_distance_points <= 0:
            logger.warning(f"[{self.account_id}] Invalid values for lot calculation. "
                           f"tick_value={tick_value}, tick_size={tick_size}, "
                           f"sl_distance={sl_distance_points}")
            return symbol_info.volume_min

        # Lot size = Risk Amount / (SL in ticks * tick value)
        sl_ticks = sl_distance_points / tick_size
        lot_size = risk_amount / (sl_ticks * tick_value)

        # Round to volume step
        step = symbol_info.volume_step
        lot_size = round(lot_size / step) * step

        # Clamp to min/max
        lot_size = max(lot_size, symbol_info.volume_min)
        lot_size = min(lot_size, symbol_info.volume_max)

        logger.info(f"[{self.account_id}] Lot calculation: balance={account.balance}, "
                    f"risk={risk_pct}%, risk_amount={risk_amount:.2f}, "
                    f"sl_distance={sl_distance_points}, lot_size={lot_size}")

        return lot_size

    # ─────────────────────────────────────────────────────────────────
    # Trade History
    # ─────────────────────────────────────────────────────────────────

    def get_trade_history(self, days: int = 30) -> List[Dict[str, Any]]:
        """
        Get closed trade history from MT5.

        Args:
            days: Number of days to look back

        Returns:
            List of deal dictionaries
        """
        self._ensure_connected()

        if not MT5_AVAILABLE:
            return []

        from_date = datetime.utcnow() - timedelta(days=days)
        to_date = datetime.utcnow()

        deals = mt5.history_deals_get(from_date, to_date)
        if deals is None:
            return []

        history = []
        for deal in deals:
            # Filter to our deals
            if deal.magic < self._magic_base or deal.magic >= self._magic_base + 1000:
                continue

            history.append({
                "ticket": deal.ticket,
                "order": deal.order,
                "time": datetime.utcfromtimestamp(deal.time),
                "symbol": deal.symbol,
                "type": "BUY" if deal.type == 0 else "SELL",
                "volume": deal.volume,
                "price": deal.price,
                "profit": deal.profit,
                "swap": deal.swap,
                "commission": deal.commission,
                "magic": deal.magic,
                "comment": deal.comment
            })

        return history



    # -----------------------------------------------------------------
    # Multi-TP Management
    # -----------------------------------------------------------------

    def manage_tp_levels(self, ticket: int, entry_price: float,
                         direction: str, tp_levels: Dict[str, float],
                         current_price: float = None) -> Dict[str, Any]:
        """
        Check and execute multi-TP partial closes.

        TP Strategy:
        - TP1 hit: Close 40% of position, move SL to breakeven
        - TP2 hit: Close 30% of remaining, activate trailing stop
        - TP3 hit: Close remaining position

        Args:
            ticket: Position ticket
            entry_price: Original entry price
            direction: "BUY" or "SELL"
            tp_levels: {"tp1": price, "tp2": price, "tp3": price}
            current_price: Current market price (fetched if None)

        Returns:
            Dict with actions taken
        """
        actions = {"tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
                   "partial_closed": 0.0, "sl_moved": False, "trailing_active": False}

        if not current_price:
            positions = self.get_open_positions()
            pos = next((p for p in positions if p.ticket == ticket), None)
            if not pos:
                return actions
            current_price = pos.current_price

        tp1 = tp_levels.get("tp1", 0)
        tp2 = tp_levels.get("tp2", 0)
        tp3 = tp_levels.get("tp3", 0)

        is_buy = direction.upper() == "BUY"

        # Check TP3 first (full close)
        if tp3 > 0:
            if (is_buy and current_price >= tp3) or (not is_buy and current_price <= tp3):
                result = self.close_position(ticket, comment="JarvAIs_TP3")
                if result.success:
                    actions["tp3_hit"] = True
                    actions["partial_closed"] = result.volume
                    logger.info(f"[{self.account_id}] TP3 HIT: ticket={ticket}, "
                                f"price={current_price}, full close")
                return actions

        # Check TP2 (partial close 30% of remaining, trailing stop disabled)
        if tp2 > 0:
            if (is_buy and current_price >= tp2) or (not is_buy and current_price <= tp2):
                result = self.partial_close(ticket, close_pct=0.30,
                                            comment="JarvAIs_TP2")
                if result.success:
                    actions["tp2_hit"] = True
                    actions["partial_closed"] = result.volume
                    # actions["trailing_active"] = True  # Trailing stop disabled — using TP1/TP2/TP3 strategy
                    logger.info(f"[{self.account_id}] TP2 HIT: ticket={ticket}, "
                                f"closed 30%")
                return actions

        # Check TP1 (partial close 40% + move to breakeven)
        if tp1 > 0:
            if (is_buy and current_price >= tp1) or (not is_buy and current_price <= tp1):
                result = self.partial_close(ticket, close_pct=0.40,
                                            comment="JarvAIs_TP1")
                if result.success:
                    actions["tp1_hit"] = True
                    actions["partial_closed"] = result.volume
                    # Move SL to breakeven + small buffer
                    be_result = self.move_to_breakeven(ticket, buffer_points=5)
                    actions["sl_moved"] = be_result
                    logger.info(f"[{self.account_id}] TP1 HIT: ticket={ticket}, "
                                f"closed 40%, SL moved to breakeven")
                return actions

        return actions

    def apply_trailing_stop(self, ticket: int, trail_distance_points: float,
                            step_points: float = 0) -> bool:
        """
        Apply a trailing stop to a position.

        The SL follows price by trail_distance_points, but only moves in
        the profitable direction. Optionally requires a minimum step before
        moving (to avoid excessive modifications).

        Args:
            ticket: Position ticket
            trail_distance_points: Distance in points to trail behind price
            step_points: Minimum points the price must move before updating SL

        Returns:
            True if SL was modified
        """
        self._ensure_connected()

        if not MT5_AVAILABLE:
            return True

        position = mt5.positions_get(ticket=ticket)
        if not position or len(position) == 0:
            return False

        pos = position[0]
        symbol_info = mt5.symbol_info(pos.symbol)
        if symbol_info is None:
            return False

        point = symbol_info.point
        trail_distance = trail_distance_points * point
        step = step_points * point if step_points > 0 else point

        if pos.type == mt5.ORDER_TYPE_BUY:
            # For BUY: trail SL below current price
            new_sl = pos.price_current - trail_distance
            # Only move SL up, never down
            if pos.sl > 0 and new_sl <= pos.sl + step:
                return False  # Not enough movement
            if new_sl <= pos.price_open:
                return False  # Don't trail below entry
        else:
            # For SELL: trail SL above current price
            new_sl = pos.price_current + trail_distance
            # Only move SL down, never up
            if pos.sl > 0 and new_sl >= pos.sl - step:
                return False
            if new_sl >= pos.price_open:
                return False

        result = self.modify_position(ticket, sl=new_sl)
        if result:
            logger.info(f"[{self.account_id}] Trailing stop updated: ticket={ticket}, "
                        f"new_sl={new_sl:.5f}")
        return result

    # -----------------------------------------------------------------
    # Position Monitor (Background Loop)
    # -----------------------------------------------------------------

    async def monitor_positions(self, check_interval: float = 5.0,
                                on_close_callback=None,
                                on_tp_callback=None):
        """
        Background coroutine that monitors open positions for:
        - TP level hits (triggers partial closes)
        - SL hits / position closures (triggers post-mortem)
        - Trailing stop updates

        Args:
            check_interval: Seconds between checks
            on_close_callback: async callable(trade_data) when position closes
            on_tp_callback: async callable(ticket, tp_level, actions) on TP hit
        """
        import asyncio

        logger.info(f"[{self.account_id}] Position monitor started "
                    f"(interval: {check_interval}s)")

        # Track known positions to detect closures
        known_positions: Dict[int, PositionInfo] = {}
        # Track TP states per position
        tp_states: Dict[int, Dict[str, bool]] = {}
        # Track trailing stop activations
        trailing_active: set = set()

        while True:
            try:
                current_positions = self.get_open_positions()
                current_tickets = {p.ticket for p in current_positions}

                # Detect closed positions
                for ticket, old_pos in list(known_positions.items()):
                    if ticket not in current_tickets:
                        logger.info(f"[{self.account_id}] Position CLOSED detected: "
                                    f"ticket={ticket}, symbol={old_pos.symbol}")

                        # Build close data for post-mortem
                        close_data = {
                            "ticket": ticket,
                            "symbol": old_pos.symbol,
                            "direction": old_pos.direction,
                            "volume": old_pos.volume,
                            "entry_price": old_pos.open_price,
                            "exit_price": old_pos.current_price,
                            "profit": old_pos.profit,
                            "magic_number": old_pos.magic_number,
                            "comment": old_pos.comment,
                            "close_reason": "unknown"  # Will be enriched
                        }

                        if on_close_callback:
                            try:
                                await on_close_callback(close_data)
                            except Exception as e:
                                logger.error(f"Close callback error: {e}")

                        del known_positions[ticket]
                        tp_states.pop(ticket, None)
                        trailing_active.discard(ticket)

                # Update known positions and check TP levels
                for pos in current_positions:
                    known_positions[pos.ticket] = pos

                    # Initialize TP state tracking
                    if pos.ticket not in tp_states:
                        tp_states[pos.ticket] = {
                            "tp1_done": False,
                            "tp2_done": False,
                            "tp3_done": False
                        }

                    # Trailing stop disabled — using TP1/TP2/TP3 strategy per user preference
                    # if pos.ticket in trailing_active:
                    #     self.apply_trailing_stop(
                    #         pos.ticket, trail_distance_points=50, step_points=10
                    #     )

                await asyncio.sleep(check_interval)

            except asyncio.CancelledError:
                logger.info(f"[{self.account_id}] Position monitor stopped")
                break
            except Exception as e:
                logger.error(f"[{self.account_id}] Position monitor error: {e}")
                await asyncio.sleep(check_interval * 2)

    # -----------------------------------------------------------------
    # Batch Operations
    # -----------------------------------------------------------------

    def close_all_positions(self, symbol: str = None,
                            comment: str = "JarvAIs_close_all") -> List[OrderResult]:
        """
        Close all open positions, optionally filtered by symbol.
        Used for emergency shutdown or end-of-day close.

        Args:
            symbol: If provided, only close positions for this symbol
            comment: Close comment

        Returns:
            List of OrderResult for each close attempt
        """
        positions = self.get_open_positions(symbol)
        results = []

        for pos in positions:
            result = self.close_position(pos.ticket, comment=comment)
            results.append(result)
            if result.success:
                logger.info(f"[{self.account_id}] Closed {pos.symbol} {pos.direction} "
                            f"ticket={pos.ticket} P&L={pos.profit:.2f}")
            else:
                logger.error(f"[{self.account_id}] Failed to close ticket={pos.ticket}: "
                             f"{result.error_message}")

        logger.info(f"[{self.account_id}] Close all: {sum(1 for r in results if r.success)}/"
                    f"{len(results)} positions closed")
        return results

    def get_position_by_magic(self, magic_number: int) -> Optional[PositionInfo]:
        """Find a position by its magic number."""
        positions = self.get_open_positions()
        for pos in positions:
            if pos.magic_number == magic_number:
                return pos
        return None

    def get_position_by_comment(self, comment_contains: str) -> List[PositionInfo]:
        """Find positions whose comment contains the given string."""
        positions = self.get_open_positions()
        return [p for p in positions if comment_contains in (p.comment or "")]

    def get_exposure_by_symbol(self) -> Dict[str, Dict[str, Any]]:
        """
        Get net exposure per symbol across all open positions.

        Returns:
            Dict keyed by symbol with net_volume, net_direction, total_profit
        """
        positions = self.get_open_positions()
        exposure = {}

        for pos in positions:
            if pos.symbol not in exposure:
                exposure[pos.symbol] = {
                    "buy_volume": 0.0,
                    "sell_volume": 0.0,
                    "total_profit": 0.0,
                    "position_count": 0
                }

            exp = exposure[pos.symbol]
            if pos.direction == "BUY":
                exp["buy_volume"] += pos.volume
            else:
                exp["sell_volume"] += pos.volume
            exp["total_profit"] += pos.profit
            exp["position_count"] += 1

        # Calculate net
        for symbol, exp in exposure.items():
            exp["net_volume"] = exp["buy_volume"] - exp["sell_volume"]
            exp["net_direction"] = "BUY" if exp["net_volume"] > 0 else "SELL" if exp["net_volume"] < 0 else "FLAT"

        return exposure



# ─────────────────────────────────────────────────────────────────────
# Instance Management
# ─────────────────────────────────────────────────────────────────────

_executors: Dict[str, MT5Executor] = {}


def get_mt5_executor(account_id: str, **kwargs) -> MT5Executor:
    """Get or create an MT5Executor for the given account."""
    global _executors
    if account_id not in _executors:
        _executors[account_id] = MT5Executor(account_id=account_id, **kwargs)
    return _executors[account_id]
