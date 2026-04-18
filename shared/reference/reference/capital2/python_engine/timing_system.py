"""
Comprehensive Timing System for Backtesting and Live Trading

Supports:
1. Fixed offsets (T-120s calc, T-30s close, T-15s open)
2. Random time windows (morning, afternoon, evening, custom ranges)
3. Specific times (exact hour:minute)
4. Relative offsets (X seconds/minutes/hours before/after Y)
"""

import random
from datetime import datetime, time as dt_time, timedelta
from typing import Dict, Any, Tuple, Optional


class TimingConfig:
    """Configuration for entry/exit timing"""
    
    def __init__(
        self,
        mode: str = 'fixed_offset',
        # Fixed offset parameters (in seconds before market close)
        calc_offset_seconds: int = 120,
        close_offset_seconds: int = 30,
        open_offset_seconds: int = 15,
        # Random range parameters
        entry_range_start: Optional[str] = None,  # "09:30:00"
        entry_range_end: Optional[str] = None,    # "15:00:00"
        exit_range_start: Optional[str] = None,
        exit_range_end: Optional[str] = None,
        # Specific time parameters
        entry_time_specific: Optional[str] = None,  # "10:30:00"
        exit_time_specific: Optional[str] = None,   # "15:45:00"
        # Market hours (per epic)
        market_open: str = "09:30:00",
        market_close: str = "16:00:00",
    ):
        self.mode = mode
        self.calc_offset_seconds = calc_offset_seconds
        self.close_offset_seconds = close_offset_seconds
        self.open_offset_seconds = open_offset_seconds
        self.entry_range_start = entry_range_start
        self.entry_range_end = entry_range_end
        self.exit_range_start = exit_range_start
        self.exit_range_end = exit_range_end
        self.entry_time_specific = entry_time_specific
        self.exit_time_specific = exit_time_specific
        self.market_open = self._parse_time(market_open)
        self.market_close = self._parse_time(market_close)
    
    @staticmethod
    def _parse_time(time_str: str) -> dt_time:
        """Parse time string to dt_time object"""
        parts = time_str.split(':')
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) > 2 else 0
        return dt_time(hour, minute, second)
    
    def get_calc_time(self) -> dt_time:
        """Get calculation time (when to run brain/indicator calculations)"""
        if self.mode == 'fixed_offset':
            # T-calc_offset_seconds before market close
            close_dt = datetime.combine(datetime.today(), self.market_close)
            calc_dt = close_dt - timedelta(seconds=self.calc_offset_seconds)
            return calc_dt.time()
        else:
            # For other modes, calc time = entry time
            return self.get_entry_time()
    
    def get_entry_time(self) -> dt_time:
        """Get entry time (when to open new trades)"""
        if self.mode == 'fixed_offset':
            # T-open_offset_seconds before market close
            close_dt = datetime.combine(datetime.today(), self.market_close)
            entry_dt = close_dt - timedelta(seconds=self.open_offset_seconds)
            return entry_dt.time()
        
        elif self.mode == 'random_range':
            # Random time within specified range
            if self.entry_range_start and self.entry_range_end:
                start = self._parse_time(self.entry_range_start)
                end = self._parse_time(self.entry_range_end)
                return self._random_time_between(start, end)
            else:
                # Default: random between market open and 1 hour before close
                close_dt = datetime.combine(datetime.today(), self.market_close)
                end_dt = close_dt - timedelta(hours=1)
                return self._random_time_between(self.market_open, end_dt.time())
        
        elif self.mode == 'random_morning':
            # Random time in morning (9:30 AM - 12:00 PM)
            return self._random_time_between(
                dt_time(9, 30, 0),
                dt_time(12, 0, 0)
            )
        
        elif self.mode == 'random_afternoon':
            # Random time in afternoon (1:00 PM - 3:00 PM)
            return self._random_time_between(
                dt_time(13, 0, 0),
                dt_time(15, 0, 0)
            )
        
        elif self.mode == 'random_evening':
            # Random time in evening (3:00 PM - 3:59 PM)
            return self._random_time_between(
                dt_time(15, 0, 0),
                dt_time(15, 59, 0)
            )
        
        elif self.mode == 'specific_time':
            # Exact time specified
            if self.entry_time_specific:
                return self._parse_time(self.entry_time_specific)
            else:
                # Default to T-15s
                close_dt = datetime.combine(datetime.today(), self.market_close)
                entry_dt = close_dt - timedelta(seconds=15)
                return entry_dt.time()
        
        else:
            # Default: T-15s before close
            close_dt = datetime.combine(datetime.today(), self.market_close)
            entry_dt = close_dt - timedelta(seconds=15)
            return entry_dt.time()
    
    def get_exit_time(self) -> dt_time:
        """Get exit time (when to close existing trades)"""
        if self.mode == 'fixed_offset':
            # T-close_offset_seconds before market close
            close_dt = datetime.combine(datetime.today(), self.market_close)
            exit_dt = close_dt - timedelta(seconds=self.close_offset_seconds)
            return exit_dt.time()
        
        elif self.mode == 'random_range':
            # Random time within specified range
            if self.exit_range_start and self.exit_range_end:
                start = self._parse_time(self.exit_range_start)
                end = self._parse_time(self.exit_range_end)
                return self._random_time_between(start, end)
            else:
                # Default: random between entry time and market close
                entry_time = self.get_entry_time()
                return self._random_time_between(entry_time, self.market_close)
        
        elif self.mode in ['random_morning', 'random_afternoon', 'random_evening']:
            # Exit at end of day (T-30s)
            close_dt = datetime.combine(datetime.today(), self.market_close)
            exit_dt = close_dt - timedelta(seconds=30)
            return exit_dt.time()
        
        elif self.mode == 'specific_time':
            # Exact time specified
            if self.exit_time_specific:
                return self._parse_time(self.exit_time_specific)
            else:
                # Default to T-30s
                close_dt = datetime.combine(datetime.today(), self.market_close)
                exit_dt = close_dt - timedelta(seconds=30)
                return exit_dt.time()
        
        else:
            # Default: T-30s before close
            close_dt = datetime.combine(datetime.today(), self.market_close)
            exit_dt = close_dt - timedelta(seconds=30)
            return exit_dt.time()
    
    @staticmethod
    def _random_time_between(start: dt_time, end: dt_time) -> dt_time:
        """Generate random time between start and end"""
        # Convert to seconds since midnight
        start_seconds = start.hour * 3600 + start.minute * 60 + start.second
        end_seconds = end.hour * 3600 + end.minute * 60 + end.second
        
        # Random seconds between start and end
        random_seconds = random.randint(start_seconds, end_seconds)
        
        # Convert back to time
        hours = random_seconds // 3600
        minutes = (random_seconds % 3600) // 60
        seconds = random_seconds % 60
        
        return dt_time(hours, minutes, seconds)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            'mode': self.mode,
            'calc_offset_seconds': self.calc_offset_seconds,
            'close_offset_seconds': self.close_offset_seconds,
            'open_offset_seconds': self.open_offset_seconds,
            'entry_range_start': self.entry_range_start,
            'entry_range_end': self.entry_range_end,
            'exit_range_start': self.exit_range_start,
            'exit_range_end': self.exit_range_end,
            'entry_time_specific': self.entry_time_specific,
            'exit_time_specific': self.exit_time_specific,
            'market_open': str(self.market_open),
            'market_close': str(self.market_close),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TimingConfig':
        """Create from dictionary"""
        # Handle 'auto' market_close - use a placeholder that will be overridden by adaptive timing
        market_close = data.get('market_close', "16:00:00")
        if market_close == 'auto':
            # When 'auto', use a placeholder - adaptive timing will determine actual close from data
            market_close = "23:59:00"  # Placeholder, will be overridden per-day
        
        return cls(
            mode=data.get('mode', 'fixed_offset'),
            calc_offset_seconds=data.get('calc_offset_seconds', 120),
            close_offset_seconds=data.get('close_offset_seconds', 30),
            open_offset_seconds=data.get('open_offset_seconds', 15),
            entry_range_start=data.get('entry_range_start'),
            entry_range_end=data.get('entry_range_end'),
            exit_range_start=data.get('exit_range_start'),
            exit_range_end=data.get('exit_range_end'),
            entry_time_specific=data.get('entry_time_specific'),
            exit_time_specific=data.get('exit_time_specific'),
            market_open=data.get('market_open', "09:30:00"),
            market_close=market_close,
        )


# Preset timing configurations
TIMING_PRESETS = {
    # ============================================================
    # SIGNAL-BASED TRADING (ignores market close timing)
    # ============================================================
    # Signal-Based - Trade on indicator signals, not fixed times
    # Entry: When indicator condition is true
    # Exit: Next signal, stop loss, or max hold period
    'SignalBased': TimingConfig(
        mode='signal_based',  # Special mode handled by backtest runner
        calc_offset_seconds=0,
        close_offset_seconds=0,
        open_offset_seconds=0,
        # market_close='auto' - still needed for max hold period calculations
    ),
    
    # ============================================================
    # AUTO-DETECT MARKET CLOSE FROM DATA (RECOMMENDED)
    # ============================================================
    # Market Close - Uses the last candle of each day (auto-detects close time)
    'MarketClose': TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=0,     # Use the closing candle itself
        close_offset_seconds=30,
        open_offset_seconds=15,
        # market_close='auto' signals Python to detect from data per day
    ),
    # T-5min Before Close - Uses candle 5 min before detected close
    'T5BeforeClose': TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=300,   # T-5 min (300 seconds)
        close_offset_seconds=30,
        open_offset_seconds=15,
        # market_close='auto' signals Python to detect from data per day
    ),
    # T-60 Fake Candle - Uses 4x 1-min candles to build fake 5-min at T-60s
    # This is the MOST REALISTIC mode as it matches exactly what the live brain sees
    'T60FakeCandle': TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=60,    # T-60s (1 minute)
        close_offset_seconds=30,
        open_offset_seconds=15,
        # market_close='auto' signals Python to detect from data per day
        # NOTE: This mode requires 1-min data and builds fake 5-min from 4x 1-min candles
    ),
    
    # ============================================================
    # FAKE 5-MIN CANDLE MODES (RECOMMENDED FOR REALISTIC BACKTESTING)
    # ============================================================
    # 4th Candle = Fake 5-min
    # Uses 4 complete 1-min candles (e.g., 19:56, 19:57, 19:58, 19:59) to build fake 5-min
    # Best for backtesting accuracy - simulates what we'd have at T-60s with full 4th candle
    'Fake5min_4thCandle': TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=60,    # T-60s - when 4th candle would be complete
        close_offset_seconds=30,
        open_offset_seconds=15,
        # market_close='auto' - detects from data per day
        # NOTE: Requires 1-min data. Uses fake_5min_close column if available.
    ),
    
    # 3rd Candle + API = Fake 5-min (DEFAULT - Most Realistic for Live Trading)
    # Uses 3 complete 1-min candles + current price API call to build fake 5-min
    # This is what live trading would actually use - candles arrive at ~T+33s
    # At T-120s we have 3 complete candles, then call API for current price
    'Fake5min_3rdCandle_API': TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=120,   # T-120s - when 3rd candle would be complete
        close_offset_seconds=30,
        open_offset_seconds=15,
        # market_close='auto' - detects from data per day
        # NOTE: In backtest, uses 3 candles + simulated 4th candle close.
        # In live trading, would use 3 candles + real-time API call.
    ),
    
    # Second-to-Last Candle of Day
    # Uses the second-to-last 5-min candle (useful for comparison with T-5min)
    'SecondLastCandle': TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=300,   # T-5min - second-to-last 5-min candle
        close_offset_seconds=30,
        open_offset_seconds=15,
        # market_close='auto' - detects from data per day
    ),
    
    # ============================================================
    # FIXED TIME MODES
    # ============================================================
    # US Market Close - Fixed 16:00 ET regardless of data
    'USMarketClose': TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=0,
        close_offset_seconds=30,
        open_offset_seconds=15,
        market_close="16:00:00",
    ),
    # Extended Hours Close - Fixed 20:00 ET regardless of data
    'ExtendedHoursClose': TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=0,
        close_offset_seconds=30,
        open_offset_seconds=15,
        market_close="20:00:00",
    ),
    
    # ============================================================
    # LEGACY MODES (for backwards compatibility)
    # ============================================================
    'EpicClosingTimeBrainCalc': TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=300,  # T-5 min
        close_offset_seconds=30,
        open_offset_seconds=15,
        # market_close='auto'
    ),
    'EpicClosingTime': TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=30,
        close_offset_seconds=30,
        open_offset_seconds=15,
        # market_close will be set dynamically from database
    ),
    'USMarketClosingTime': TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=30,
        close_offset_seconds=30,
        open_offset_seconds=15,
        market_close="16:00:00",  # Standard US market hours
    ),
    'ManusTime': TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=30,
        close_offset_seconds=30,
        open_offset_seconds=15,
    ),
    'OriginalBotTime': TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=120,
        close_offset_seconds=30,
        open_offset_seconds=15,
    ),
    'MorningRandom': TimingConfig(
        mode='random_morning',
    ),
    'AfternoonRandom': TimingConfig(
        mode='random_afternoon',
    ),
    'EveningRandom': TimingConfig(
        mode='random_evening',
    ),
    'FullDayRandom': TimingConfig(
        mode='random_range',
        entry_range_start="09:30:00",
        entry_range_end="15:30:00",
        exit_range_start="14:00:00",
        exit_range_end="15:59:30",
    ),
}


def get_timing_config(
    preset: Optional[str] = None,
    custom_config: Optional[Dict[str, Any]] = None
) -> TimingConfig:
    """
    Get timing configuration from preset or custom config
    
    Args:
        preset: Name of preset ('ManusTime', 'OriginalBotTime', etc.)
        custom_config: Custom timing configuration dict
    
    Returns:
        TimingConfig object
    """
    if preset and preset in TIMING_PRESETS:
        return TIMING_PRESETS[preset]
    elif custom_config:
        return TimingConfig.from_dict(custom_config)
    else:
        # Default to ManusTime
        return TIMING_PRESETS['ManusTime']


# Test the timing system
if __name__ == '__main__':
    print("Testing Timing System\n")
    
    # Test 1: ManusTime
    print("1. ManusTime (T-30s calc, T-30s close, T-15s open):")
    config = TIMING_PRESETS['ManusTime']
    print(f"   Calc time: {config.get_calc_time()}")
    print(f"   Entry time: {config.get_entry_time()}")
    print(f"   Exit time: {config.get_exit_time()}\n")
    
    # Test 2: OriginalBotTime
    print("2. OriginalBotTime (T-120s calc, T-30s close, T-15s open):")
    config = TIMING_PRESETS['OriginalBotTime']
    print(f"   Calc time: {config.get_calc_time()}")
    print(f"   Entry time: {config.get_entry_time()}")
    print(f"   Exit time: {config.get_exit_time()}\n")
    
    # Test 3: Morning Random
    print("3. Morning Random (9:30 AM - 12:00 PM entry, T-30s exit):")
    config = TIMING_PRESETS['MorningRandom']
    for i in range(3):
        print(f"   Sample {i+1}: Entry {config.get_entry_time()}, Exit {config.get_exit_time()}")
    print()
    
    # Test 4: Custom Range
    print("4. Custom Range (10:00 AM - 2:00 PM entry, 2:00 PM - 3:55 PM exit):")
    config = TimingConfig(
        mode='random_range',
        entry_range_start="10:00:00",
        entry_range_end="14:00:00",
        exit_range_start="14:00:00",
        exit_range_end="15:55:00",
    )
    for i in range(3):
        print(f"   Sample {i+1}: Entry {config.get_entry_time()}, Exit {config.get_exit_time()}")
    print()
    
    # Test 5: Specific Times
    print("5. Specific Times (Entry 10:30 AM, Exit 3:45 PM):")
    config = TimingConfig(
        mode='specific_time',
        entry_time_specific="10:30:00",
        exit_time_specific="15:45:00",
    )
    print(f"   Entry time: {config.get_entry_time()}")
    print(f"   Exit time: {config.get_exit_time()}\n")
    
    # Test 6: Custom Offsets
    print("6. Custom Offsets (T-60s calc, T-45s close, T-20s open):")
    config = TimingConfig(
        mode='fixed_offset',
        calc_offset_seconds=60,
        close_offset_seconds=45,
        open_offset_seconds=20,
    )
    print(f"   Calc time: {config.get_calc_time()}")
    print(f"   Entry time: {config.get_entry_time()}")
    print(f"   Exit time: {config.get_exit_time()}\n")
    
    print("All tests completed!")

