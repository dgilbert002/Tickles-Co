"""
JarvAIs Signal Backtester Service
Pure data-driven signal resolution using stored candle data.

This service:
1. Gets pending/active signals from parsed_signals table
2. Fetches stored candles from candles table
3. Replays each candle chronologically to determine:
   - Entry hit (pending → active)
   - SL hit → LOSS
   - TP1/TP2/TP3 hit → WIN (partial or full)
4. Calculates outcome_pips, outcome_rr
5. Updates signal with resolution_method = 'candle_data'
6. Marks stale signals after configurable period

NO AI CALLS - Pure data comparison.

Usage:
    from services.signal_backtester import SignalBacktester
    
    backtester = SignalBacktester(db, config)
    result = backtester.backtest_signal(123)  # Single signal
    result = backtester.backtest_all_signals()  # All signals
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass
from core.time_utils import utcnow

logger = logging.getLogger("jarvais.signal_backtester")

_MAX_DISTANCE_PCT = 999999.99

def _clamp_distance(val: float) -> float:
    """Clamp entry_distance_pct to fit DECIMAL(8,2) column bounds."""
    return max(-_MAX_DISTANCE_PCT, min(_MAX_DISTANCE_PCT, round(val, 2)))


# ─────────────────────────────────────────────────────────────────────
# Pip Values by Asset Class
# ─────────────────────────────────────────────────────────────────────

PIP_VALUES = {
    "forex_jpy": 0.01,      # JPY pairs: 1 pip = 0.01
    "forex": 0.0001,        # Other forex: 1 pip = 0.0001
    "commodity": 0.01,      # Gold: 1 pip = 0.01
    "index": 1.0,           # Indices: 1 pip = 1 point
    "cryptocurrency": 0.01, # Crypto: varies
    "stock": 0.01,          # Stocks
    "default": 0.0001
}

# Symbols with special pip values
# IMPORTANT: For indices, 1 point = 1 pip
SYMBOL_PIP_OVERRIDES = {
    # Precious metals
    "XAUUSD": 0.1,          # Gold: 1 pip = $0.10 (so $1 move = 10 pips)
    "XAGUSD": 0.01,         # Silver: 1 pip = $0.01
    
    # Energy
    "USOIL": 0.01,          # WTI Crude
    "UKOIL": 0.01,          # Brent Crude
    
    # Indices - 1 point = 1 pip
    "NAS100": 1.0,          # Nasdaq 100
    "US100": 1.0,           # Nasdaq 100 (alternative)
    "NAS": 1.0,             # Nasdaq prefix
    "NDX": 1.0,             # Nasdaq 100
    "US30": 1.0,            # Dow Jones 30
    "DJ30": 1.0,            # Dow Jones (alternative)
    "SPX500": 0.1,          # S&P 500 (different contract)
    "US500": 0.1,           # S&P 500 (alternative)
    "US2000": 1.0,          # Russell 2000
    "GER30": 1.0,           # DAX
    "UK100": 1.0,           # FTSE 100
    
    # Crypto
    "BTCUSD": 1.0,          # Bitcoin: 1 pip = $1
    "ETHUSD": 0.1,          # Ethereum: 1 pip = $0.10
    "XRPUSD": 0.0001,       # Ripple
    "LTCUSD": 0.01,         # Litecoin
    
    # JPY pairs
    "USDJPY": 0.01,
    "EURJPY": 0.01,
    "GBPJPY": 0.01,
    "AUDJPY": 0.01,
    "CADJPY": 0.01,
}


@dataclass
class BacktestConfig:
    """Configuration for backtesting."""
    timeframe: str = "M5"
    assume_tp1_is_win: bool = True
    stale_days: int = 30
    stale_forever_days: int = 60
    entry_tolerance_pips: int = 5
    max_tp_levels: int = 6  # Configurable: 1-6 TP levels
    refine_with_m1: bool = False  # If True, use M1 candles to refine hit timestamps (extra fetch per resolved signal)


# ─────────────────────────────────────────────────────────────────────
# SIGNAL BACKTESTER CLASS
# ─────────────────────────────────────────────────────────────────────

class SignalBacktester:
    """
    Pure data-driven signal resolution using stored candle data.
    """
    
    def __init__(self, db, config):
        self.db = db
        self.config = config
        self._load_config()
        
    def _load_config(self):
        """Load configuration from system_config table."""
        try:
            rows = self.db.fetch_all("""
                SELECT config_key, config_value FROM system_config 
                WHERE config_key IN ('backtest_timeframe', 'backtest_stale_days', 
                    'backtest_stale_forever_days', 'backtest_assume_tp1_is_win',
                    'backtest_entry_tolerance_pips', 'backtest_refine_with_m1')
            """)
            config_dict = {r['config_key']: r['config_value'] for r in rows} if rows else {}
            raw = getattr(self.config, 'raw', {}) if self.config else {}
            td = raw.get("trade_decision", {}) if isinstance(raw, dict) else {}
            refine_val = td.get("backtester_refine_with_m1", raw.get("backtester_refine_with_m1", config_dict.get('backtest_refine_with_m1', '0')))
            refine = str(refine_val).lower() in ('1', 'true', 'yes')
            self.cfg = BacktestConfig(
                timeframe=config_dict.get('backtest_timeframe', 'M5'),
                assume_tp1_is_win=config_dict.get('backtest_assume_tp1_is_win', '1') == '1',
                stale_days=int(config_dict.get('backtest_stale_days', 30)),
                stale_forever_days=int(config_dict.get('backtest_stale_forever_days', 60)),
                entry_tolerance_pips=int(config_dict.get('backtest_entry_tolerance_pips', 5)),
                max_tp_levels=int(config_dict.get('backtest_max_tp_levels', 6)),
                refine_with_m1=refine
            )
        except Exception as e:
            logger.warning(f"[SignalBacktester] Could not load config: {e}")
            self.cfg = BacktestConfig()
    
    # ── Pip Calculation ───────────────────────────────────────────────
    
    def _get_pip_value(self, symbol: str, asset_class: str = None) -> float:
        """Get pip value for a symbol."""
        # Check overrides first
        for key, pip_val in SYMBOL_PIP_OVERRIDES.items():
            if key.upper() in symbol.upper():
                return pip_val
        
        # Check asset class
        if asset_class:
            ac = asset_class.lower()
            if 'jpy' in symbol.lower():
                return PIP_VALUES['forex_jpy']
            return PIP_VALUES.get(ac, PIP_VALUES['default'])
        
        # Default
        return PIP_VALUES['default']
    
    def _calculate_pips(self, symbol: str, price_diff: float, asset_class: str = None) -> float:
        """Calculate pips from price difference."""
        pip_value = self._get_pip_value(symbol, asset_class)
        return round(price_diff / pip_value, 1)
    
    # ── Single Signal Backtest ─────────────────────────────────────────
    
    def backtest_signal(self, signal_id: int, force: bool = False) -> Dict[str, Any]:
        """
        Backtest a single signal using stored candle data.
        
        Args:
            signal_id: Signal ID to backtest
            force: If True, re-evaluate even if already resolved (resets hit timestamps)
        """
        result = {
            "success": False,
            "signal_id": signal_id,
            "status_before": None,
            "status_after": None,
            "outcome": None,
            "outcome_pips": 0.0,
            "resolution_method": None,
            "entry_hit_at": None,
            "tp1_hit_at": None,
            "tp2_hit_at": None,
            "tp3_hit_at": None,
            "tp4_hit_at": None,
            "tp5_hit_at": None,
            "tp6_hit_at": None,
            "sl_hit_at": None,
            "candles_used": 0,
            "entry_distance_pct": None,
            "error": None
        }
        
        try:
            # Get signal
            signal = self.db.fetch_one(
                "SELECT * FROM parsed_signals WHERE id = %s", (signal_id,)
            )
            if not signal:
                result["error"] = "Signal not found"
                return result
            
            result["status_before"] = signal["status"]
            
            if force:
                # Reset tracking fields so _evaluate_signal starts fresh
                signal = dict(signal)
                signal["status"] = "pending"
                signal["entry_hit_at"] = None
                signal["sl_hit_at"] = None
                signal["highest_price"] = 0
                signal["lowest_price"] = 0
                signal["max_favorable"] = 0
                signal["max_adverse"] = 0
                for i in range(1, 7):
                    signal[f"tp{i}_hit_at"] = None
            else:
                # Skip if already fully resolved
                if signal["status"] in ("tp3_hit", "tp4_hit", "tp5_hit", "tp6_hit", "sl_hit", "expired", "missed"):
                    if signal.get("resolution_method"):
                        result["status_after"] = signal["status"]
                        result["outcome"] = signal.get("outcome")
                        result["resolution_method"] = signal.get("resolution_method")
                        result["success"] = True
                        return result
            
            # Get candles from parsed_at to now
            parsed_at = signal["parsed_at"]
            symbol = signal["symbol"]
            direction = signal["direction"]
            entry = float(signal.get("entry_price") or 0)
            sl = float(signal.get("stop_loss") or 0)
            tp1 = float(signal.get("take_profit_1") or 0)
            tp2 = float(signal.get("take_profit_2") or 0)
            tp3 = float(signal.get("take_profit_3") or 0)
            
            # Get candles
            candles = self._get_candles(symbol, parsed_at)
            result["candles_used"] = len(candles)
            
            if not candles:
                # No candles available - try to get current price from Yahoo/MT5 for distance calc
                current_price = self._fetch_current_price(symbol)
                if current_price and entry > 0:
                    if direction == "BUY":
                        dist = ((entry - current_price) / entry) * 100
                    else:  # SELL
                        dist = ((current_price - entry) / entry) * 100
                    result["entry_distance_pct"] = _clamp_distance(dist)
                    try:
                        self.db.execute("UPDATE parsed_signals SET entry_distance_pct = %s WHERE id = %s", (_clamp_distance(dist), signal_id))
                    except:
                        pass
                
                # No candles available for full backtest
                result["error"] = "No candle data available"
                result["status_after"] = signal["status"]
                return result
            
            # Get pip value
            pip_val = self._get_pip_value(symbol)
            
            # Evaluate signal
            eval_result = self._evaluate_signal(
                signal, candles, pip_val
            )
            
            # Update result
            result.update(eval_result)
            result["success"] = True
            
            # Update database
            self._update_signal(signal_id, eval_result, force=force)
            
            logger.info(f"[BacktestSignal] {signal_id}: {result['status_before']} -> {result['status_after']}, outcome={result['outcome']}")
            
        except Exception as e:
            result["error"] = str(e)
            logger.error(f"[BacktestSignal] Error for signal {signal_id}: {e}", exc_info=True)
        
        return result
    
    def _get_candles(self, symbol: str, from_date: datetime) -> List[Dict]:
        """Get stored candles for a symbol."""
        try:
            rows = self.db.fetch_all("""
                SELECT candle_time as time, open, high, low, close, volume
                FROM candles
                WHERE symbol = %s AND timeframe = %s AND candle_time >= %s
                ORDER BY candle_time ASC
            """, (symbol, self.cfg.timeframe, from_date))

            candles = []
            for r in rows:
                candles.append({
                    "time": r['time'],
                    "open": float(r['open']),
                    "high": float(r['high']),
                    "low": float(r['low']),
                    "close": float(r['close']),
                    "volume": int(r['volume'] or 0)
                })
            return candles
        except Exception as e:
            logger.error(f"[BacktestSignal] Error getting candles: {e}")
            return []

    def _get_candles_for_window(self, symbol: str, from_time: datetime, to_time: datetime,
                                timeframe: str = "M1") -> List[Dict]:
        """Fetch candles for a specific time window (e.g. M1 for 5-min refinement)."""
        try:
            rows = self.db.fetch_all("""
                SELECT candle_time as time, open, high, low, close, volume
                FROM candles
                WHERE symbol = %s AND timeframe = %s
                AND candle_time >= %s AND candle_time < %s
                ORDER BY candle_time ASC
            """, (symbol, timeframe, from_time, to_time))
            return [{"time": r['time'], "open": float(r['open']), "high": float(r['high']),
                     "low": float(r['low']), "close": float(r['close']),
                     "volume": int(r['volume'] or 0)} for r in rows]
        except Exception as e:
            logger.warning(f"[BacktestSignal] M1 window fetch failed: {e}")
            return []
    
    def _evaluate_signal(self, signal: Dict, candles: List[Dict], pip_val: float) -> Dict[str, Any]:
        """
        Evaluate a signal against candle data.
        Core backtesting logic with support for up to 6 TP levels.
        """
        max_tps = self.cfg.max_tp_levels
        
        # Build result dict with dynamic TP fields
        result = {
            "status_after": signal["status"],
            "outcome": signal.get("outcome"),
            "outcome_pips": 0.0,
            "outcome_rr": 0.0,
            "resolution_method": None,
            "entry_hit_at": signal.get("entry_hit_at"),
            "sl_hit_at": signal.get("sl_hit_at"),
            "entry_distance_pct": None,
            "highest_price": float(signal.get("highest_price") or 0),
            "lowest_price": float(signal.get("lowest_price") or 0),
            "max_favorable": float(signal.get("max_favorable") or 0),
            "max_adverse": float(signal.get("max_adverse") or 0),
        }
        
        # Add dynamic TP hit times
        tp_hit_times = {}
        for i in range(1, max_tps + 1):
            result[f"tp{i}_hit_at"] = signal.get(f"tp{i}_hit_at")
            tp_hit_times[i] = None
        
        direction = signal["direction"]
        status = signal["status"]
        entry = float(signal.get("entry_price") or 0)
        sl = float(signal.get("stop_loss") or 0)
        
        # Get all TP levels dynamically
        tps = []
        for i in range(1, max_tps + 1):
            tp_val = float(signal.get(f"take_profit_{i}") or 0)
            tps.append(tp_val if tp_val > 0 else None)
        
        # Track prices
        highest_price = result["highest_price"]
        lowest_price = result["lowest_price"]
        
        # Check if already entry hit
        entry_hit = status not in ("pending", "stale")
        entry_hit_time = result["entry_hit_at"]
        
        # Track hits with timestamps
        tp_hits = {i: False for i in range(1, max_tps + 1)}
        sl_hit = False
        sl_hit_time = None
        
        # Tolerance for entry (in price, not pips)
        entry_tolerance = self.cfg.entry_tolerance_pips * pip_val
        
        # Process each candle chronologically
        for candle in candles:
            c_open = candle["open"]
            c_high = candle["high"]
            c_low = candle["low"]
            c_close = candle["close"]
            c_time = candle["time"]
            
            # Update tracking prices
            if highest_price == 0 or c_high > highest_price:
                highest_price = c_high
            if lowest_price == 0 or c_low < lowest_price:
                lowest_price = c_low
            
            # ── PENDING: Check entry hit ─────────────────────────────
            if not entry_hit:
                if entry > 0:
                    if direction == "BUY":
                        if c_low <= entry + entry_tolerance:
                            entry_hit = True
                            entry_hit_time = c_time
                    else:  # SELL
                        if c_high >= entry - entry_tolerance:
                            entry_hit = True
                            entry_hit_time = c_time
                else:
                    entry_hit = True
                    entry_hit_time = c_time
                    entry = c_open
            
            # ── ACTIVE: Check SL/TP hits ─────────────────────────────
            # Stop if assume_tp1_is_win and TP1 already hit with no TP2
            has_higher_tp = any(tps[i-1] for i in range(2, max_tps + 1) if not tp_hits.get(i-1, False))
            should_continue = not (tp_hits[1] and self.cfg.assume_tp1_is_win and not has_higher_tp)
            
            if entry_hit and not sl_hit and should_continue:
                # Check SL first
                if sl > 0:
                    if direction == "BUY":
                        if c_low <= sl:
                            sl_hit = True
                            sl_hit_time = c_time
                    else:  # SELL
                        if c_high >= sl:
                            sl_hit = True
                            sl_hit_time = c_time
                
                # Check TPs sequentially
                if not sl_hit or (sl_hit_time and sl_hit_time > c_time):
                    for i in range(1, max_tps + 1):
                        tp = tps[i-1]
                        if tp and tp > 0 and not tp_hits[i]:
                            # Must have hit previous TP first (except TP1)
                            if i == 1 or tp_hits.get(i-1, False):
                                if direction == "BUY":
                                    if c_high >= tp:
                                        tp_hits[i] = True
                                        tp_hit_times[i] = c_time
                                else:  # SELL
                                    if c_low <= tp:
                                        tp_hits[i] = True
                                        tp_hit_times[i] = c_time
            
            # Calculate max favorable/adverse after entry
            if entry_hit and entry > 0:
                if direction == "BUY":
                    favorable = self._calculate_pips(signal["symbol"], highest_price - entry, signal.get("asset_class"))
                    adverse = self._calculate_pips(signal["symbol"], entry - lowest_price, signal.get("asset_class"))
                else:  # SELL
                    favorable = self._calculate_pips(signal["symbol"], entry - lowest_price, signal.get("asset_class"))
                    adverse = self._calculate_pips(signal["symbol"], highest_price - entry, signal.get("asset_class"))
                
                if favorable > result["max_favorable"]:
                    result["max_favorable"] = favorable
                if adverse > result["max_adverse"]:
                    result["max_adverse"] = adverse
            
            # Break conditions
            if tp_hits[1] and self.cfg.assume_tp1_is_win and not has_higher_tp:
                break
            if sl_hit:
                break
        
        # ── Determine final outcome ─────────────────────────────────
        
        # Calculate entry distance if still pending
        if not entry_hit and len(candles) > 0:
            last_close = candles[-1]["close"]
            if entry > 0:
                if direction == "BUY":
                    dist = ((entry - last_close) / entry) * 100
                else:
                    dist = ((last_close - entry) / entry) * 100
                result["entry_distance_pct"] = _clamp_distance(dist)
        
        result["highest_price"] = highest_price
        result["lowest_price"] = lowest_price
        
        # Find highest TP hit
        highest_tp_hit = 0
        for i in range(max_tps, 0, -1):
            if tp_hits[i]:
                highest_tp_hit = i
                break
        
        # Update status based on hits
        if entry_hit:
            result["entry_hit_at"] = entry_hit_time
            
            if sl_hit:
                # Check if any TP was hit before SL
                tp_before_sl = 0
                for i in range(1, max_tps + 1):
                    if tp_hits[i] and tp_hit_times[i] and sl_hit_time:
                        if tp_hit_times[i] < sl_hit_time:
                            tp_before_sl = i
                
                if tp_before_sl > 0:
                    # TP hit before SL - win at that TP level
                    result["outcome"] = "win"
                    tp_price = tps[tp_before_sl - 1]
                    result["outcome_pips"] = self._calculate_pips(
                        signal["symbol"],
                        abs(tp_price - entry) if direction == "BUY" else abs(entry - tp_price),
                        signal.get("asset_class")
                    )
                    result["status_after"] = f"tp{tp_before_sl}_hit"
                    result[f"tp{tp_before_sl}_hit_at"] = tp_hit_times[tp_before_sl]
                else:
                    # SL hit before any TP - loss
                    result["outcome"] = "loss"
                    result["outcome_pips"] = -self._calculate_pips(
                        signal["symbol"],
                        abs(sl - entry) if direction == "BUY" else abs(entry - sl),
                        signal.get("asset_class")
                    )
                    result["status_after"] = "sl_hit"
                    result["sl_hit_at"] = sl_hit_time
            
            elif highest_tp_hit > 0:
                # TP hit, no SL - win at highest TP
                result["outcome"] = "win"
                tp_price = tps[highest_tp_hit - 1]
                result["outcome_pips"] = self._calculate_pips(
                    signal["symbol"],
                    abs(tp_price - entry) if direction == "BUY" else abs(entry - tp_price),
                    signal.get("asset_class")
                )
                result["status_after"] = f"tp{highest_tp_hit}_hit"
                for i in range(1, highest_tp_hit + 1):
                    if tp_hit_times[i]:
                        result[f"tp{i}_hit_at"] = tp_hit_times[i]
            
            else:
                # Entry hit but no SL/TP hit yet - still active
                result["status_after"] = "active"
                result["outcome"] = None
        else:
            # Entry not hit - still pending
            result["status_after"] = "pending"
        
        # Set resolution method if resolved
        if result["outcome"]:
            result["resolution_method"] = "candle_data"
            # Optional M1 refinement for hit timestamps (audit trail)
            if self.cfg.refine_with_m1 and candles:
                tp_before_sl = 0
                if sl_hit and sl_hit_time:
                    for i in range(1, max_tps + 1):
                        if tp_hits[i] and tp_hit_times[i] and tp_hit_times[i] < sl_hit_time:
                            tp_before_sl = i
                resolving_time = sl_hit_time if sl_hit else (tp_hit_times.get(highest_tp_hit) if highest_tp_hit else None)
                if resolving_time:
                    refine_tp = tp_before_sl if tp_before_sl > 0 else highest_tp_hit
                    self._refine_hit_timestamps_m1(signal, result, resolving_time, sl, tps, direction, sl_hit, refine_tp)

        # Calculate outcome_rr
        if result["outcome_pips"] != 0 and sl > 0 and entry > 0:
            sl_pips = self._calculate_pips(
                signal["symbol"],
                abs(sl - entry) if direction == "BUY" else abs(entry - sl),
                signal.get("asset_class")
            )
            if sl_pips > 0:
                result["outcome_rr"] = round(result["outcome_pips"] / sl_pips, 2)
        
        return result

    def _refine_hit_timestamps_m1(self, signal: Dict, result: Dict, resolving_time: datetime,
                                   sl: float, tps: List, direction: str, sl_hit: bool,
                                   refine_tp: int):
        """Refine sl_hit_at / tp*_hit_at using M1 candles for the 5-min window. Improves audit trail."""
        to_time = resolving_time + timedelta(minutes=5)
        m1_candles = self._get_candles_for_window(signal["symbol"], resolving_time, to_time, "M1")
        if not m1_candles:
            return
        for c in m1_candles:
            ch, cl = c["high"], c["low"]
            ct = c["time"]
            if sl_hit and sl > 0 and refine_tp == 0:
                if direction == "BUY" and cl <= sl:
                    result["sl_hit_at"] = ct
                    return
                if direction == "SELL" and ch >= sl:
                    result["sl_hit_at"] = ct
                    return
            if refine_tp > 0:
                tp = tps[refine_tp - 1] if refine_tp <= len(tps) else None
                if tp and tp > 0:
                    if direction == "BUY" and ch >= tp:
                        result[f"tp{refine_tp}_hit_at"] = ct
                        return
                    if direction == "SELL" and cl <= tp:
                        result[f"tp{refine_tp}_hit_at"] = ct
                        return

    def _update_signal(self, signal_id: int, result: Dict, force: bool = False):
        """Update signal in database with backtest results."""
        try:
            if force:
                # Overwrite everything — fresh evaluation
                self.db.execute("""
                    UPDATE parsed_signals SET
                        status = %s,
                        outcome = %s,
                        outcome_pips = %s,
                        outcome_rr = %s,
                        resolution_method = %s,
                        entry_hit_at = %s,
                        tp1_hit_at = %s, tp2_hit_at = %s, tp3_hit_at = %s,
                        tp4_hit_at = %s, tp5_hit_at = %s, tp6_hit_at = %s,
                        sl_hit_at = %s,
                        highest_price = %s,
                        lowest_price = %s,
                        max_favorable = %s,
                        max_adverse = %s,
                        entry_distance_pct = %s,
                        resolved_at = CASE WHEN %s IS NOT NULL THEN NOW() ELSE NULL END,
                        updated_at = NOW()
                    WHERE id = %s
                """, (
                    result.get("status_after"),
                    result.get("outcome"),
                    result.get("outcome_pips", 0),
                    result.get("outcome_rr", 0),
                    result.get("resolution_method"),
                    result.get("entry_hit_at"),
                    result.get("tp1_hit_at"), result.get("tp2_hit_at"), result.get("tp3_hit_at"),
                    result.get("tp4_hit_at"), result.get("tp5_hit_at"), result.get("tp6_hit_at"),
                    result.get("sl_hit_at"),
                    result.get("highest_price", 0),
                    result.get("lowest_price", 0),
                    result.get("max_favorable", 0),
                    result.get("max_adverse", 0),
                    result.get("entry_distance_pct"),
                    result.get("outcome"),
                    signal_id
                ))
            else:
                # Incremental — preserve existing values with COALESCE
                self.db.execute("""
                    UPDATE parsed_signals SET
                        status = %s,
                        outcome = %s,
                        outcome_pips = %s,
                        outcome_rr = %s,
                        resolution_method = COALESCE(%s, resolution_method),
                        entry_hit_at = COALESCE(%s, entry_hit_at),
                        tp1_hit_at = COALESCE(%s, tp1_hit_at),
                        tp2_hit_at = COALESCE(%s, tp2_hit_at),
                        tp3_hit_at = COALESCE(%s, tp3_hit_at),
                        tp4_hit_at = COALESCE(%s, tp4_hit_at),
                        tp5_hit_at = COALESCE(%s, tp5_hit_at),
                        tp6_hit_at = COALESCE(%s, tp6_hit_at),
                        sl_hit_at = COALESCE(%s, sl_hit_at),
                        highest_price = %s,
                        lowest_price = %s,
                        max_favorable = %s,
                        max_adverse = %s,
                        entry_distance_pct = COALESCE(%s, entry_distance_pct),
                        resolved_at = CASE WHEN %s IS NOT NULL THEN NOW() ELSE resolved_at END,
                        updated_at = NOW()
                    WHERE id = %s
                """, (
                    result.get("status_after"),
                    result.get("outcome"),
                    result.get("outcome_pips", 0),
                    result.get("outcome_rr", 0),
                    result.get("resolution_method"),
                    result.get("entry_hit_at"),
                    result.get("tp1_hit_at"), result.get("tp2_hit_at"), result.get("tp3_hit_at"),
                    result.get("tp4_hit_at"), result.get("tp5_hit_at"), result.get("tp6_hit_at"),
                    result.get("sl_hit_at"),
                    result.get("highest_price", 0),
                    result.get("lowest_price", 0),
                    result.get("max_favorable", 0),
                    result.get("max_adverse", 0),
                    result.get("entry_distance_pct"),
                    result.get("outcome"),
                    signal_id
                ))
            # If signal has a terminal outcome, close any linked TradingView idea
            outcome = result.get("outcome")
            if outcome in ("win", "loss", "expired"):
                self._close_linked_idea(signal_id, outcome, result.get("status_after"))

        except Exception as e:
            logger.error(f"[BacktestSignal] Error updating signal {signal_id}: {e}")

    def _close_linked_idea(self, signal_id: int, outcome: str, status_after: str):
        """When a signal resolves (TP/SL/expired), close the linked TradingView idea
        so the idea monitor stops replaying it and wasting AI credits."""
        try:
            row = self.db.fetch_one(
                "SELECT ps.news_item_id, tit.id AS tracking_id, tit.status "
                "FROM parsed_signals ps "
                "JOIN tradingview_ideas_tracking tit ON tit.news_item_id = ps.news_item_id "
                "WHERE ps.id = %s AND tit.status = 'tracking'",
                (signal_id,)
            )
            if not row:
                return

            idea_status_map = {
                "win": "hit_target",
                "loss": "hit_stop",
                "expired": "expired",
            }
            new_idea_status = idea_status_map.get(outcome, "expired")

            self.db.execute(
                "UPDATE tradingview_ideas_tracking "
                "SET status = %s, next_check_at = NULL "
                "WHERE id = %s AND status = 'tracking'",
                (new_idea_status, row["tracking_id"])
            )
            logger.info(f"[BacktestSignal] Closed idea tracking #{row['tracking_id']} -> {new_idea_status} "
                        f"(signal #{signal_id} outcome={outcome}, status={status_after})")

        except Exception as e:
            logger.debug(f"[BacktestSignal] Idea close check for signal {signal_id}: {e}")

    # ── Batch Backtest ─────────────────────────────────────────────────
    
    def backtest_all_signals(self, callback=None) -> Dict[str, Any]:
        """
        Backtest all eligible signals.
        
        Args:
            callback: Optional callback function(signal_id, current, total) for progress
            
        Returns:
            {
                "total": int,
                "resolved_by_candle": int,
                "still_unresolved": int,
                "stale_marked": int,
                "errors": int,
                "duration_ms": int
            }
        """
        start_time = utcnow()
        
        result = {
            "total": 0,
            "resolved_by_candle": 0,
            "still_unresolved": 0,
            "stale_marked": 0,
            "errors": 0,
            "duration_ms": 0
        }
        
        try:
            # Get all eligible signals
            signals = self.db.fetch_all("""
                SELECT id FROM parsed_signals 
                WHERE status IN ('pending', 'active', 'entry_hit', 'tp1_hit', 'tp2_hit', 'tp3_hit', 'tp4_hit', 'tp5_hit', 'stale')
                AND parsed_at > DATE_SUB(NOW(), INTERVAL %s DAY)
                ORDER BY parsed_at DESC
            """, (self.cfg.stale_forever_days,))
            
            if not signals:
                logger.info("[BacktestAll] No eligible signals")
                return result
            
            result["total"] = len(signals)
            logger.info(f"[BacktestAll] Starting backtest for {len(signals)} signals")
            
            for i, sig in enumerate(signals):
                signal_id = sig["id"]
                
                if callback:
                    callback(signal_id, i + 1, len(signals))
                
                try:
                    res = self.backtest_signal(signal_id)
                    
                    if res.get("outcome"):
                        result["resolved_by_candle"] += 1
                    elif res.get("status_after") in ("pending", "active", "entry_hit", "tp1_hit", "tp2_hit", "tp3_hit", "tp4_hit", "tp5_hit"):
                        result["still_unresolved"] += 1
                    
                    if res.get("error"):
                        result["errors"] += 1
                        
                except Exception as e:
                    logger.error(f"[BacktestAll] Error for signal {signal_id}: {e}")
                    result["errors"] += 1
            
            # Mark stale signals
            stale_count = self.detect_stale_signals()
            result["stale_marked"] = stale_count
            
        except Exception as e:
            logger.error(f"[BacktestAll] Error: {e}", exc_info=True)
        
        end_time = utcnow()
        result["duration_ms"] = int((end_time - start_time).total_seconds() * 1000)
        
        logger.info(f"[BacktestAll] Complete: {result}")
        return result
    
    # ── Full Database Rebacktest ──────────────────────────────────────
    
    def rebacktest_all(self) -> Dict[str, Any]:
        """
        Force re-evaluate EVERY signal in the database using candle data.
        Ignores current status — replays all candles from scratch.
        After re-evaluation, rebuilds all provider scores from the updated data.
        """
        start_time = utcnow()
        
        result = {
            "total": 0,
            "upgraded": 0,
            "downgraded": 0,
            "unchanged": 0,
            "no_candles": 0,
            "errors": 0,
            "duration_ms": 0,
            "scores_rebuilt": 0,
        }
        
        try:
            signals = self.db.fetch_all("""
                SELECT id, symbol, direction, status, outcome, parsed_at,
                       entry_price, stop_loss, take_profit_1, take_profit_2, take_profit_3,
                       take_profit_4, take_profit_5, take_profit_6,
                       entry_hit_at, sl_hit_at, highest_price, lowest_price,
                       max_favorable, max_adverse
                FROM parsed_signals WHERE is_valid = 1 ORDER BY parsed_at ASC
            """)
            
            if not signals:
                logger.info("[RebacktestAll] No signals in database")
                return result
            
            result["total"] = len(signals)
            logger.info(f"[RebacktestAll] Force re-evaluating {len(signals)} signals (batched by symbol)")

            by_symbol = {}
            for sig in signals:
                sym = sig.get("symbol") or "UNKNOWN"
                if sym not in by_symbol:
                    by_symbol[sym] = []
                by_symbol[sym].append(sig)

            processed = 0
            for symbol, sym_signals in by_symbol.items():
                parsed_dates = [s["parsed_at"] for s in sym_signals if s.get("parsed_at")]
                if not parsed_dates:
                    result["no_candles"] += len(sym_signals)
                    continue
                from_date = min(parsed_dates)
                candles = self._get_candles(symbol, from_date)
                pip_val = self._get_pip_value(symbol)

                for sig in sym_signals:
                    signal_id = sig["id"]
                    old_status = sig["status"]
                    old_outcome = sig.get("outcome")
                    parsed_at = sig.get("parsed_at")

                    try:
                        sig_copy = dict(sig)
                        sig_copy["status"] = "pending"
                        sig_copy["entry_hit_at"] = None
                        sig_copy["sl_hit_at"] = None
                        sig_copy["highest_price"] = 0
                        sig_copy["lowest_price"] = 0
                        sig_copy["max_favorable"] = 0
                        sig_copy["max_adverse"] = 0
                        for i in range(1, 7):
                            sig_copy[f"tp{i}_hit_at"] = None

                        filtered = [c for c in candles if c["time"] >= parsed_at] if parsed_at else candles
                        if not filtered:
                            result["no_candles"] += 1
                            continue

                        eval_result = self._evaluate_signal(sig_copy, filtered, pip_val)
                        self._update_signal(signal_id, eval_result, force=True)

                        new_status = eval_result.get("status_after", "")
                        new_outcome = eval_result.get("outcome")

                        tp_rank = {"pending": 0, "active": 0, "entry_hit": 1,
                                   "tp1_hit": 2, "tp2_hit": 3, "tp3_hit": 4,
                                   "tp4_hit": 5, "tp5_hit": 6, "tp6_hit": 7,
                                   "sl_hit": -1, "expired": -2, "missed": -2, "stale": -2}
                        old_rank = tp_rank.get(old_status, 0)
                        new_rank = tp_rank.get(new_status, 0)

                        if new_rank > old_rank:
                            result["upgraded"] += 1
                        elif new_rank < old_rank:
                            result["downgraded"] += 1
                        else:
                            result["unchanged"] += 1

                        processed += 1
                        if processed % 100 == 0:
                            logger.info(f"[RebacktestAll] Progress: {processed}/{len(signals)}")

                    except Exception as e:
                        logger.error(f"[RebacktestAll] Error for signal {signal_id}: {e}")
                        result["errors"] += 1

            result["scores_rebuilt"] = self.rebuild_provider_scores()
            
        except Exception as e:
            logger.error(f"[RebacktestAll] Fatal error: {e}", exc_info=True)
        
        end_time = utcnow()
        result["duration_ms"] = int((end_time - start_time).total_seconds() * 1000)
        
        logger.info(f"[RebacktestAll] Complete: {result}")
        return result
    
    # ── Provider Score Rebuild ────────────────────────────────────────
    
    def rebuild_provider_scores(self) -> int:
        """
        Recalculate ALL provider scores from scratch using parsed_signals.
        Returns number of providers updated.
        """
        try:
            # Ensure extended columns exist (safe migrations)
            for col, col_def in [
                ("entry_hit", "INT DEFAULT 0"),
                ("valid_signals", "INT DEFAULT 0"),
                ("tp1_hit", "INT DEFAULT 0"),
                ("tp2_hit", "INT DEFAULT 0"),
                ("tp3_hit", "INT DEFAULT 0"),
                ("tp4_hit", "INT DEFAULT 0"),
                ("tp5_hit", "INT DEFAULT 0"),
                ("tp6_hit", "INT DEFAULT 0"),
                ("sl_hit", "INT DEFAULT 0"),
                ("missed", "INT DEFAULT 0"),
                ("expired", "INT DEFAULT 0"),
                ("avg_rr", "DECIMAL(5,2) DEFAULT 0"),
                ("trust_score", "DECIMAL(5,2) DEFAULT 50"),
                ("streak", "INT DEFAULT 0"),
                ("best_streak", "INT DEFAULT 0"),
                ("worst_streak", "INT DEFAULT 0"),
            ]:
                try:
                    self.db.execute(f"ALTER TABLE signal_provider_scores ADD COLUMN {col} {col_def}")
                except Exception:
                    pass  # Column already exists
            
            # Wipe existing scores
            self.db.execute("DELETE FROM signal_provider_scores")
            
            # Aggregate per provider from parsed_signals
            providers = self.db.fetch_all("""
                SELECT source, COALESCE(source_detail, '') as source_detail,
                       COALESCE(author, '') as author,
                       COUNT(*) as total_signals,
                       SUM(CASE WHEN is_valid = 1 THEN 1 ELSE 0 END) as valid_signals,
                       SUM(CASE WHEN entry_hit_at IS NOT NULL THEN 1 ELSE 0 END) as entry_hit,
                       SUM(CASE WHEN status IN ('tp1_hit','tp2_hit','tp3_hit','tp4_hit','tp5_hit','tp6_hit') THEN 1 ELSE 0 END) as tp1_hit,
                       SUM(CASE WHEN status IN ('tp2_hit','tp3_hit','tp4_hit','tp5_hit','tp6_hit') THEN 1 ELSE 0 END) as tp2_hit,
                       SUM(CASE WHEN status IN ('tp3_hit','tp4_hit','tp5_hit','tp6_hit') THEN 1 ELSE 0 END) as tp3_hit,
                       SUM(CASE WHEN status IN ('tp4_hit','tp5_hit','tp6_hit') THEN 1 ELSE 0 END) as tp4_hit,
                       SUM(CASE WHEN status IN ('tp5_hit','tp6_hit') THEN 1 ELSE 0 END) as tp5_hit,
                       SUM(CASE WHEN status = 'tp6_hit' THEN 1 ELSE 0 END) as tp6_hit,
                       SUM(CASE WHEN status = 'sl_hit' THEN 1 ELSE 0 END) as sl_hit,
                       SUM(CASE WHEN outcome = 'missed' THEN 1 ELSE 0 END) as missed,
                       SUM(CASE WHEN outcome = 'expired' THEN 1 ELSE 0 END) as expired,
                       SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) as losses,
                       SUM(COALESCE(outcome_pips, 0)) as total_pips,
                       AVG(CASE WHEN outcome IS NOT NULL THEN outcome_pips ELSE NULL END) as avg_pips,
                       AVG(CASE WHEN outcome IS NOT NULL AND outcome_rr IS NOT NULL THEN outcome_rr ELSE NULL END) as avg_rr,
                       MAX(parsed_at) as last_signal_at
                FROM parsed_signals
                WHERE is_valid = 1
                GROUP BY source, COALESCE(source_detail, ''), COALESCE(author, '')
            """)
            
            if not providers:
                return 0
            
            count = 0
            for p in providers:
                total = p["total_signals"]
                wins = p["wins"] or 0
                losses = p["losses"] or 0
                resolved = wins + losses
                win_rate = round((wins / resolved) * 100, 2) if resolved > 0 else 0
                total_pips = round(float(p["total_pips"] or 0), 1)
                avg_pips = round(float(p["avg_pips"] or 0), 1)
                avg_rr = round(float(p["avg_rr"] or 0), 2)
                
                # Trust score: 50 base + 2 per win - 3 per loss + escalating TP bonuses
                trust = 50.0
                trust += wins * 2.0
                trust -= losses * 3.0
                trust += (p["tp2_hit"] or 0) * 1.0   # TP2+ = +1 each
                trust += (p["tp3_hit"] or 0) * 1.5   # TP3+ = +1.5 each (stacks with TP2)
                trust += (p["tp4_hit"] or 0) * 2.0   # TP4+ = +2 each
                trust += (p["tp5_hit"] or 0) * 2.5   # TP5+ = +2.5 each
                trust += (p["tp6_hit"] or 0) * 3.0   # TP6  = +3 each (max bonus)
                trust = max(0, min(100, trust))
                
                # Streak: compute from chronological signal list
                streak = self._compute_streak(
                    p["source"], p["source_detail"], p["author"]
                )
                
                try:
                    self.db.execute("""
                        INSERT INTO signal_provider_scores
                        (source, source_detail, author, total_signals, valid_signals,
                         entry_hit, tp1_hit, tp2_hit, tp3_hit, tp4_hit, tp5_hit, tp6_hit,
                         sl_hit, missed, expired,
                         total_pips, avg_pips, win_rate, avg_rr, trust_score,
                         streak, best_streak, worst_streak, last_signal_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        p["source"], p["source_detail"], p["author"],
                        total, p["valid_signals"] or 0, p["entry_hit"] or 0,
                        p["tp1_hit"] or 0, p["tp2_hit"] or 0, p["tp3_hit"] or 0,
                        p["tp4_hit"] or 0, p["tp5_hit"] or 0, p["tp6_hit"] or 0,
                        p["sl_hit"] or 0, p["missed"] or 0, p["expired"] or 0,
                        total_pips, avg_pips, win_rate, avg_rr, round(trust, 2),
                        streak["current"], streak["best"], streak["worst"],
                        p["last_signal_at"]
                    ))
                    count += 1
                except Exception as e:
                    logger.warning(f"[RebuildScores] Error inserting provider {p['source']}/{p['source_detail']}: {e}")
            
            logger.info(f"[RebuildScores] Rebuilt scores for {count} providers")
            return count
            
        except Exception as e:
            logger.error(f"[RebuildScores] Error: {e}", exc_info=True)
            return 0
    
    def _compute_streak(self, source: str, source_detail: str, author: str) -> Dict[str, int]:
        """Compute current/best/worst streak for a provider from signal history."""
        try:
            rows = self.db.fetch_all("""
                SELECT outcome FROM parsed_signals
                WHERE source = %s AND COALESCE(source_detail, '') = %s
                  AND COALESCE(author, '') = %s
                  AND outcome IS NOT NULL AND is_valid = 1
                ORDER BY parsed_at ASC
            """, (source, source_detail, author))
            
            current = best = worst = 0
            for r in (rows or []):
                if r["outcome"] == "win":
                    current = max(current, 0) + 1
                elif r["outcome"] == "loss":
                    current = min(current, 0) - 1
                else:
                    continue
                best = max(best, current)
                worst = min(worst, current)
            
            return {"current": current, "best": best, "worst": worst}
        except Exception:
            return {"current": 0, "best": 0, "worst": 0}
    
    # ── Stale Detection ────────────────────────────────────────────────
    
    def detect_stale_signals(self, dry_run: bool = False) -> List[int]:
        """
        Find and mark stale signals.
        
        Stale = pending signal older than stale_days without entry hit.
        Stale forever = older than stale_forever_days, never rechecked.
        
        Returns:
            List of signal IDs marked as stale (or would be if dry_run)
        """
        stale_ids = []
        
        try:
            # Find pending signals older than stale_days
            cutoff_stale = utcnow() - timedelta(days=self.cfg.stale_days)
            cutoff_forever = utcnow() - timedelta(days=self.cfg.stale_forever_days)
            
            # Signals that are stale but not forever
            signals = self.db.fetch_all("""
                SELECT id, symbol, entry_price, direction
                FROM parsed_signals 
                WHERE status = 'pending'
                AND parsed_at < %s
                AND parsed_at > %s
            """, (cutoff_stale, cutoff_forever))
            
            for sig in signals:
                # Calculate entry distance
                entry = float(sig.get("entry_price") or 0)
                if entry > 0:
                    current_price = self._get_latest_price(sig["symbol"])
                    if current_price:
                        direction = sig["direction"]
                        if direction == "BUY":
                            dist_pct = ((entry - current_price) / entry) * 100
                        else:
                            dist_pct = ((current_price - entry) / entry) * 100
                        
                        # If > 100% away, mark as stale
                        if abs(dist_pct) > 100:
                            stale_ids.append(sig["id"])
                            if not dry_run:
                                self.db.execute("""
                                    UPDATE parsed_signals SET 
                                        status = 'stale',
                                        entry_distance_pct = %s,
                                        updated_at = NOW()
                                    WHERE id = %s
                                """, (_clamp_distance(dist_pct), sig["id"]))
            
            # Signals that are stale forever (never rechecked)
            forever_signals = self.db.fetch_all("""
                SELECT id FROM parsed_signals 
                WHERE status IN ('pending', 'stale')
                AND parsed_at < %s
            """, (cutoff_forever,))
            
            for sig in forever_signals:
                stale_ids.append(sig["id"])
                if not dry_run:
                    self.db.execute("""
                        UPDATE parsed_signals SET 
                            status = 'stale',
                            outcome = 'missed',
                            resolution_method = 'expired',
                            resolved_at = NOW(),
                            updated_at = NOW()
                        WHERE id = %s
                    """, (sig["id"],))
            
            if stale_ids:
                logger.info(f"[StaleDetector] Marked {len(stale_ids)} signals as stale")
            
        except Exception as e:
            logger.error(f"[StaleDetector] Error: {e}")
        
        return stale_ids
    
    def _get_latest_price(self, symbol: str) -> Optional[float]:
        """Get latest price for a symbol from candles or try MT5."""
        try:
            row = self.db.fetch_one("""
                SELECT close FROM candles 
                WHERE symbol = %s 
                ORDER BY candle_time DESC LIMIT 1
            """, (symbol,))
            if row:
                return float(row["close"])
        except:
            pass
        
        # Try MT5
        try:
            from core.mt5_manager import get_mt5_manager
            manager = get_mt5_manager()
            prices = manager.get_current_price(symbol)
            if prices:
                return prices[0]  # bid
        except:
            pass
        
        return None
    
    def _fetch_current_price(self, symbol: str) -> Optional[float]:
        """
        Fetch current price for a symbol from any available source.
        Tries: stored candles -> MT5 -> Yahoo Finance
        """
        # First try stored candles
        price = self._get_latest_price(symbol)
        if price:
            return price
        
        # Try Yahoo Finance
        try:
            from services.candle_collector import fetch_yahoo_candles
            candles = fetch_yahoo_candles(symbol, "M5", days=1)
            if candles:
                return candles[-1]["close"]
        except Exception as e:
            logger.warning(f"[SignalBacktester] Yahoo price fetch failed for {symbol}: {e}")
        
        # Try MT5 directly
        try:
            from core.mt5_manager import get_mt5_manager
            manager = get_mt5_manager()
            prices = manager.get_current_price(symbol)
            if prices:
                return prices[0]
        except:
            pass
        
        return None
