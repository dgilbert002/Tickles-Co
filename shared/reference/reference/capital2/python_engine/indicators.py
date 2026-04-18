#!/usr/bin/env python3
"""
COMPREHENSIVE TECHNICAL INDICATORS LIBRARY
Dynamic indicator calculations with configurable parameters

This library provides a unified interface for calculating technical indicators
with customizable parameters, allowing for dynamic testing and optimization.

Usage:
    from indicators import IndicatorLibrary
    
    lib = IndicatorLibrary(df)
    
    # Calculate RSI with custom period
    rsi_14 = lib.rsi(period=14)
    rsi_7 = lib.rsi(period=7)
    
    # Calculate MACD with custom parameters
    macd = lib.macd(fast_period=12, slow_period=26, signal_period=9)
    
    # Get indicator conditions
    conditions = lib.get_conditions()
    rsi_oversold = conditions['rsi_oversold'](df, idx, period=14, threshold=30)
"""

import pandas as pd
import numpy as np
import talib
from typing import Dict, Callable, Any, Optional, Tuple, List
from numba import jit

# ============================================================================
# NUMBA JIT-COMPILED HELPER FUNCTIONS
# These provide 60-400x speedup for computationally intensive indicators
# ============================================================================

@jit(nopython=True, cache=True)
def _calculate_streak(price_change: np.ndarray) -> np.ndarray:
    """
    Calculate price streak (consecutive up/down days).
    Numba JIT-compiled for speed - used by ConnorsRSI.
    
    Returns:
        Array of streak values (positive for up streaks, negative for down)
    """
    n = len(price_change)
    streak = price_change.copy()
    
    for i in range(1, n):
        if np.isnan(streak[i]):
            continue
        if streak[i] * streak[i-1] > 0:  # Same direction
            if streak[i] > 0:
                streak[i] = streak[i-1] + 1
            else:
                streak[i] = streak[i-1] - 1
        else:  # Direction change
            if streak[i] > 0:
                streak[i] = 1
            else:
                streak[i] = -1
    return streak


@jit(nopython=True, cache=True)
def _supertrend_loop(close: np.ndarray, upper_band: np.ndarray, 
                     lower_band: np.ndarray, period: int):
    """
    Calculate Supertrend indicator with Numba JIT.
    Core loop extracted for 60x speedup.
    
    Returns:
        Tuple of (supertrend, direction, upper, lower) arrays
    """
    n = len(close)
    supertrend = np.zeros(n, dtype=np.float64)
    direction = np.zeros(n, dtype=np.float64)
    upper = upper_band.copy()
    lower = lower_band.copy()
    
    for i in range(period, n):
        # Update lower band
        if lower[i] > lower[i-1] or close[i-1] < lower[i-1]:
            lower[i] = lower[i]
        else:
            lower[i] = lower[i-1]
        
        # Update upper band
        if upper[i] < upper[i-1] or close[i-1] > upper[i-1]:
            upper[i] = upper[i]
        else:
            upper[i] = upper[i-1]
        
        # Determine direction and supertrend value
        if i == period:
            if close[i] <= upper[i]:
                direction[i] = -1
                supertrend[i] = upper[i]
            else:
                direction[i] = 1
                supertrend[i] = lower[i]
        else:
            if direction[i-1] == 1:
                if close[i] <= lower[i]:
                    direction[i] = -1
                    supertrend[i] = upper[i]
                else:
                    direction[i] = 1
                    supertrend[i] = lower[i]
            else:
                if close[i] >= upper[i]:
                    direction[i] = 1
                    supertrend[i] = lower[i]
                else:
                    direction[i] = -1
                    supertrend[i] = upper[i]
    
    return supertrend, direction, upper, lower


@jit(nopython=True, cache=True)
def _fisher_transform_with_minmax(close: np.ndarray, min_low: np.ndarray, 
                                   max_high: np.ndarray, period: int):
    """
    Calculate Fisher Transform using pre-computed min/max.
    Numba JIT-compiled for 231x speedup.
    
    Returns:
        Tuple of (fisher, trigger) arrays
    """
    n = len(close)
    value = np.zeros(n, dtype=np.float64)
    fisher = np.zeros(n, dtype=np.float64)
    
    # Calculate normalized value
    for i in range(period, n):
        if max_high[i] != min_low[i]:
            value[i] = 2 * ((close[i] - min_low[i]) / (max_high[i] - min_low[i]) - 0.5)
            if value[i] > 0.999:
                value[i] = 0.999
            elif value[i] < -0.999:
                value[i] = -0.999
    
    # Apply Fisher Transform
    for i in range(1, n):
        fisher[i] = 0.5 * fisher[i-1] + 0.5 * np.log((1 + value[i]) / (1 - value[i]))
    
    # Trigger is the previous Fisher value
    trigger = np.zeros(n, dtype=np.float64)
    trigger[1:] = fisher[:-1]
    
    return fisher, trigger


@jit(nopython=True, cache=True)
def _wma_numba(values: np.ndarray, period: int) -> np.ndarray:
    """
    Calculate Weighted Moving Average with Numba JIT.
    Used by Coppock Curve for 348x speedup.
    
    Returns:
        WMA values array
    """
    n = len(values)
    result = np.full(n, np.nan, dtype=np.float64)
    weights = np.arange(1, period + 1, dtype=np.float64)
    weight_sum = weights.sum()
    
    for i in range(period - 1, n):
        window = values[i-period+1:i+1]
        if not np.any(np.isnan(window)):
            result[i] = np.sum(window * weights) / weight_sum
    
    return result


def zero_lag_ema(src: np.ndarray, length: int, gain_limit: int = 10) -> np.ndarray:
    """
    John Ehlers Zero-Lag EMA implementation.
    Finds optimal gain to minimize lag while maintaining smoothness.
    Used by Zero-Lag Aroon Oscillator.
    
    Args:
        src: Source data array
        length: EMA period
        gain_limit: Range for gain optimization (default: 10)
    
    Returns:
        Zero-lag smoothed values
    """
    n = len(src)
    alpha = 2.0 / (length + 1)
    
    ema = np.zeros(n)
    ec = np.zeros(n)
    
    # Initialize
    ema[0] = src[0]
    ec[0] = src[0]
    
    for i in range(1, n):
        if np.isnan(src[i]):
            ema[i] = ema[i-1]
            ec[i] = ec[i-1]
            continue
            
        # Standard EMA
        ema[i] = alpha * src[i] + (1 - alpha) * ema[i-1]
        
        # Find best gain to minimize lag
        least_error = float('inf')
        best_gain = 0.0
        
        for value in range(-gain_limit, gain_limit + 1):
            gain = value / 10.0
            # Calculate EC with this gain
            ec_test = alpha * (ema[i] + gain * (src[i] - ec[i-1])) + (1 - alpha) * ec[i-1]
            error = abs(src[i] - ec_test)
            
            if error < least_error:
                least_error = error
                best_gain = gain
        
        # Apply best gain
        ec[i] = alpha * (ema[i] + best_gain * (src[i] - ec[i-1])) + (1 - alpha) * ec[i-1]
    
    return ec


class IndicatorLibrary:
    """
    Comprehensive technical indicators library with configurable parameters
    """
    
    def __init__(self, df: pd.DataFrame):
        """
        Initialize the indicator library with price data
        
        Args:
            df: DataFrame with columns: openPrice, highPrice, lowPrice, closePrice, lastTradedVolume
        """
        self.df = df.copy()
        self.open = df['openPrice'].values
        self.high = df['highPrice'].values
        self.low = df['lowPrice'].values
        self.close = df['closePrice'].values
        self.volume = df['lastTradedVolume'].values
        
        # Cache for calculated indicators
        self._cache = {}
    
    def _get_cache_key(self, indicator_name: str, **kwargs) -> str:
        """Generate cache key for indicator with parameters"""
        params = '_'.join([f"{k}={v}" for k, v in sorted(kwargs.items())])
        return f"{indicator_name}_{params}" if params else indicator_name
    
    # ============================================================================
    # RSI INDICATORS
    # ============================================================================
    
    def rsi(self, period: int = 14) -> np.ndarray:
        """
        Calculate RSI (Relative Strength Index)
        
        Args:
            period: RSI calculation period (default: 14)
            
        Returns:
            RSI values as numpy array
        """
        cache_key = self._get_cache_key('rsi', period=period)
        if cache_key not in self._cache:
            self._cache[cache_key] = talib.RSI(self.close, timeperiod=period)
        return self._cache[cache_key]
    
    def connors_rsi(self, rsi_period: int = 3, streak_period: int = 2, rank_period: int = 100) -> np.ndarray:
        """
        Calculate ConnorsRSI (3-component RSI system) - OPTIMIZED with Numba JIT
        Achieves ~83-95x speedup over original implementation.
        
        Args:
            rsi_period: RSI component period (default: 3)
            streak_period: Streak RSI period (default: 2)
            rank_period: Percent rank lookback period (default: 100)
            
        Returns:
            ConnorsRSI values as numpy array
        """
        cache_key = self._get_cache_key('connors_rsi', rsi_period=rsi_period, 
                                       streak_period=streak_period, rank_period=rank_period)
        
        if cache_key not in self._cache:
            # Component 1: Standard RSI (talib is already optimized)
            rsi = talib.RSI(self.close, timeperiod=rsi_period)
            
            # Component 2: Streak RSI - uses Numba JIT-compiled _calculate_streak
            price_change = pd.Series(self.close).diff().values
            streak = _calculate_streak(price_change)
            streak_rsi = talib.RSI(streak, timeperiod=streak_period)
            
            # Component 3: Percent Rank
            returns = pd.Series(self.close).pct_change()
            percent_rank = returns.rolling(rank_period).rank(pct=True) * 100
            
            # Combine components
            connors_rsi = (rsi + streak_rsi + percent_rank.values) / 3
            self._cache[cache_key] = connors_rsi
            
        return self._cache[cache_key]
    
    def stochastic_rsi(self, period: int = 14, fastk_period: int = 5, fastd_period: int = 3) -> Tuple[np.ndarray, np.ndarray]:
        """
        Calculate Stochastic RSI
        
        Args:
            period: RSI period for Stochastic calculation (default: 14)
            fastk_period: Fast %K period (default: 5)
            fastd_period: Fast %D period (default: 3)
            
        Returns:
            Tuple of (Fast %K, Fast %D) as numpy arrays
        """
        cache_key = self._get_cache_key('stoch_rsi', period=period, 
                                       fastk_period=fastk_period, fastd_period=fastd_period)
        
        if cache_key not in self._cache:
            fastk, fastd = talib.STOCHRSI(self.close, timeperiod=period, 
                                         fastk_period=fastk_period, fastd_period=fastd_period)
            self._cache[cache_key] = (fastk, fastd)
            
        return self._cache[cache_key]
    
    # ============================================================================
    # MACD INDICATORS
    # ============================================================================
    
    def macd(self, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Calculate MACD (Moving Average Convergence Divergence)
        
        Args:
            fast_period: Fast EMA period (default: 12)
            slow_period: Slow EMA period (default: 26)
            signal_period: Signal line EMA period (default: 9)
            
        Returns:
            Tuple of (MACD line, Signal line, Histogram) as numpy arrays
        """
        cache_key = self._get_cache_key('macd', fast_period=fast_period, 
                                       slow_period=slow_period, signal_period=signal_period)
        
        if cache_key not in self._cache:
            macd_line, signal_line, histogram = talib.MACD(self.close, 
                                                          fastperiod=fast_period,
                                                          slowperiod=slow_period, 
                                                          signalperiod=signal_period)
            self._cache[cache_key] = (macd_line, signal_line, histogram)
            
        return self._cache[cache_key]
    
    # ============================================================================
    # STOCHASTIC INDICATORS
    # ============================================================================
    
    def stochastic(self, fastk_period: int = 5, slowk_period: int = 3, slowd_period: int = 3) -> Tuple[np.ndarray, np.ndarray]:
        """
        Calculate Stochastic Oscillator
        
        Args:
            fastk_period: Fast %K period (default: 5)
            slowk_period: Slow %K period (default: 3)
            slowd_period: Slow %D period (default: 3)
            
        Returns:
            Tuple of (Slow %K, Slow %D) as numpy arrays
        """
        cache_key = self._get_cache_key('stochastic', fastk_period=fastk_period,
                                       slowk_period=slowk_period, slowd_period=slowd_period)
        
        if cache_key not in self._cache:
            slowk, slowd = talib.STOCH(self.high, self.low, self.close,
                                      fastk_period=fastk_period, slowk_period=slowk_period,
                                      slowk_matype=0, slowd_period=slowd_period, slowd_matype=0)
            self._cache[cache_key] = (slowk, slowd)
            
        return self._cache[cache_key]
    
    # ============================================================================
    # BOLLINGER BANDS
    # ============================================================================
    
    def bollinger_bands(self, period: int = 20, std_dev: float = 2.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Calculate Bollinger Bands
        
        Args:
            period: Moving average period (default: 20)
            std_dev: Standard deviation multiplier (default: 2.0)
            
        Returns:
            Tuple of (Upper band, Middle band, Lower band) as numpy arrays
        """
        cache_key = self._get_cache_key('bb', period=period, std_dev=std_dev)
        
        if cache_key not in self._cache:
            upper, middle, lower = talib.BBANDS(self.close, timeperiod=period, 
                                               nbdevup=std_dev, nbdevdn=std_dev)
            self._cache[cache_key] = (upper, middle, lower)
            
        return self._cache[cache_key]
    
    def bb_position(self, period: int = 20, std_dev: float = 2.0) -> np.ndarray:
        """
        Calculate position within Bollinger Bands (0 = lower band, 1 = upper band)
        
        Args:
            period: Moving average period (default: 20)
            std_dev: Standard deviation multiplier (default: 2.0)
            
        Returns:
            Position values as numpy array
        """
        upper, middle, lower = self.bollinger_bands(period, std_dev)
        return (self.close - lower) / (upper - lower)
    
    def bb_width(self, period: int = 20, std_dev: float = 2.0) -> np.ndarray:
        """
        Calculate Bollinger Bands width (normalized by middle band)
        
        Args:
            period: Moving average period (default: 20)
            std_dev: Standard deviation multiplier (default: 2.0)
            
        Returns:
            Width values as numpy array
        """
        upper, middle, lower = self.bollinger_bands(period, std_dev)
        return (upper - lower) / middle
    
    # ============================================================================
    # OTHER OSCILLATORS
    # ============================================================================
    
    def williams_r(self, period: int = 14) -> np.ndarray:
        """
        Calculate Williams %R
        
        Args:
            period: Lookback period (default: 14)
            
        Returns:
            Williams %R values as numpy array
        """
        cache_key = self._get_cache_key('willr', period=period)
        if cache_key not in self._cache:
            self._cache[cache_key] = talib.WILLR(self.high, self.low, self.close, timeperiod=period)
        return self._cache[cache_key]
    
    def cci(self, period: int = 14) -> np.ndarray:
        """
        Calculate Commodity Channel Index (CCI)
        
        Args:
            period: Calculation period (default: 14)
            
        Returns:
            CCI values as numpy array
        """
        cache_key = self._get_cache_key('cci', period=period)
        if cache_key not in self._cache:
            self._cache[cache_key] = talib.CCI(self.high, self.low, self.close, timeperiod=period)
        return self._cache[cache_key]
    
    # ============================================================================
    # VOLATILITY INDICATORS
    # ============================================================================
    
    def atr(self, period: int = 14) -> np.ndarray:
        """
        Calculate Average True Range (ATR)
        
        Args:
            period: ATR period (default: 14)
            
        Returns:
            ATR values as numpy array
        """
        cache_key = self._get_cache_key('atr', period=period)
        if cache_key not in self._cache:
            self._cache[cache_key] = talib.ATR(self.high, self.low, self.close, timeperiod=period)
        return self._cache[cache_key]
    
    def atr_percent(self, period: int = 14) -> np.ndarray:
        """
        Calculate ATR as percentage of close price
        
        Args:
            period: ATR period (default: 14)
            
        Returns:
            ATR percentage values as numpy array
        """
        atr_values = self.atr(period)
        return (atr_values / self.close) * 100
    
    # ============================================================================
    # TREND INDICATORS
    # ============================================================================
    
    def parabolic_sar(self, acceleration: float = 0.02, maximum: float = 0.2) -> np.ndarray:
        """
        Calculate Parabolic SAR
        
        Args:
            acceleration: Acceleration factor (default: 0.02)
            maximum: Maximum acceleration (default: 0.2)
            
        Returns:
            SAR values as numpy array
        """
        cache_key = self._get_cache_key('sar', acceleration=acceleration, maximum=maximum)
        if cache_key not in self._cache:
            self._cache[cache_key] = talib.SAR(self.high, self.low, 
                                              acceleration=acceleration, maximum=maximum)
        return self._cache[cache_key]
    
    def adx(self, period: int = 14) -> np.ndarray:
        """
        Calculate Average Directional Index (ADX)
        
        Args:
            period: ADX period (default: 14)
            
        Returns:
            ADX values as numpy array
        """
        cache_key = self._get_cache_key('adx', period=period)
        if cache_key not in self._cache:
            self._cache[cache_key] = talib.ADX(self.high, self.low, self.close, timeperiod=period)
        return self._cache[cache_key]
    
    def aroon(self, period: int = 14) -> Tuple[np.ndarray, np.ndarray]:
        """
        Calculate Aroon Up and Aroon Down
        
        Args:
            period: Aroon period (default: 14)
            
        Returns:
            Tuple of (Aroon Up, Aroon Down) as numpy arrays
        """
        cache_key = self._get_cache_key('aroon', period=period)
        if cache_key not in self._cache:
            aroon_up, aroon_down = talib.AROON(self.high, self.low, timeperiod=period)
            self._cache[cache_key] = (aroon_up, aroon_down)
        return self._cache[cache_key]
    
    # ============================================================================
    # MOVING AVERAGES
    # ============================================================================
    
    def sma(self, period: int = 20) -> np.ndarray:
        """
        Calculate Simple Moving Average (SMA)
        
        Args:
            period: SMA period (default: 20)
            
        Returns:
            SMA values as numpy array
        """
        cache_key = self._get_cache_key('sma', period=period)
        if cache_key not in self._cache:
            self._cache[cache_key] = talib.SMA(self.close, timeperiod=period)
        return self._cache[cache_key]
    
    def ema(self, period: int = 20) -> np.ndarray:
        """
        Calculate Exponential Moving Average (EMA)
        
        Args:
            period: EMA period (default: 20)
            
        Returns:
            EMA values as numpy array
        """
        cache_key = self._get_cache_key('ema', period=period)
        if cache_key not in self._cache:
            self._cache[cache_key] = talib.EMA(self.close, timeperiod=period)
        return self._cache[cache_key]
    
    # ============================================================================
    # VOLUME INDICATORS
    # ============================================================================
    
    def obv(self) -> np.ndarray:
        """
        Calculate On-Balance Volume (OBV)
        
        Returns:
            OBV values as numpy array
        """
        cache_key = 'obv'
        if cache_key not in self._cache:
            self._cache[cache_key] = talib.OBV(self.close, self.volume)
        return self._cache[cache_key]
    
    def volume_sma(self, period: int = 20) -> np.ndarray:
        """
        Calculate Volume Simple Moving Average
        
        Args:
            period: SMA period (default: 20)
            
        Returns:
            Volume SMA values as numpy array
        """
        cache_key = self._get_cache_key('volume_sma', period=period)
        if cache_key not in self._cache:
            self._cache[cache_key] = talib.SMA(self.volume, timeperiod=period)
        return self._cache[cache_key]
    
    def volume_ratio(self, period: int = 20) -> np.ndarray:
        """
        Calculate Volume Ratio (current volume / average volume)
        
        Args:
            period: Average volume period (default: 20)
            
        Returns:
            Volume ratio values as numpy array
        """
        volume_avg = self.volume_sma(period)
        return self.volume / volume_avg
    
    def vwap(self) -> np.ndarray:
        """
        Calculate Volume Weighted Average Price (VWAP)
        
        Returns:
            VWAP values as numpy array
        """
        cache_key = 'vwap'
        if cache_key not in self._cache:
            typical_price = (self.high + self.low + self.close) / 3
            cumulative_volume = np.cumsum(self.volume)
            cumulative_pv = np.cumsum(typical_price * self.volume)
            
            # Avoid division by zero
            vwap = np.where(cumulative_volume != 0, cumulative_pv / cumulative_volume, typical_price)
            self._cache[cache_key] = vwap
            
        return self._cache[cache_key]
    
    def vwap_deviation(self) -> np.ndarray:
        """
        Calculate deviation from VWAP as percentage
        
        Returns:
            VWAP deviation values as numpy array
        """
        vwap_values = self.vwap()
        return (self.close - vwap_values) / vwap_values
    
    # ============================================================================
    # MOMENTUM INDICATORS
    # ============================================================================
    
    def momentum(self, period: int = 10) -> np.ndarray:
        """
        Calculate Momentum
        
        Args:
            period: Momentum period (default: 10)
            
        Returns:
            Momentum values as numpy array
        """
        cache_key = self._get_cache_key('momentum', period=period)
        if cache_key not in self._cache:
            self._cache[cache_key] = talib.MOM(self.close, timeperiod=period)
        return self._cache[cache_key]
    
    def roc(self, period: int = 10) -> np.ndarray:
        """
        Calculate Rate of Change (ROC)
        
        Args:
            period: ROC period (default: 10)
            
        Returns:
            ROC values as numpy array
        """
        cache_key = self._get_cache_key('roc', period=period)
        if cache_key not in self._cache:
            self._cache[cache_key] = talib.ROC(self.close, timeperiod=period)
        return self._cache[cache_key]
    
    # ============================================================================
    # MODERN INDICATORS (ADDED)
    # ============================================================================
    
    def supertrend(self, period: int = 10, multiplier: float = 3.0) -> Tuple[np.ndarray, np.ndarray]:
        """
        Calculate Supertrend indicator - OPTIMIZED with Numba JIT
        Achieves ~60-61x speedup over original implementation.
        
        Args:
            period: ATR period (default: 10)
            multiplier: ATR multiplier (default: 3.0)
            
        Returns:
            Tuple of (supertrend_line, direction) where direction is 1 for bullish, -1 for bearish
        """
        cache_key = self._get_cache_key('supertrend', period=period, multiplier=multiplier)
        if cache_key not in self._cache:
            # Use talib ATR (already optimized)
            atr = talib.ATR(self.high, self.low, self.close, timeperiod=period)
            hl_avg = (self.high + self.low) / 2
            
            # Calculate basic bands
            upper_band = hl_avg + (multiplier * atr)
            lower_band = hl_avg - (multiplier * atr)
            
            # Use Numba JIT-compiled loop for massive speedup
            supertrend, direction, _, _ = _supertrend_loop(
                self.close, upper_band, lower_band, period
            )
            
            self._cache[cache_key] = (supertrend, direction)
        return self._cache[cache_key]
    
    def keltner_channels(self, period: int = 20, multiplier: float = 2.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Calculate Keltner Channels
        
        Args:
            period: EMA period (default: 20)
            multiplier: ATR multiplier (default: 2.0)
            
        Returns:
            Tuple of (middle_band, upper_band, lower_band)
        """
        cache_key = self._get_cache_key('keltner_channels', period=period, multiplier=multiplier)
        if cache_key not in self._cache:
            middle = self.ema(period)
            atr = self.atr(period)
            upper = middle + (multiplier * atr)
            lower = middle - (multiplier * atr)
            self._cache[cache_key] = (middle, upper, lower)
        return self._cache[cache_key]
    
    def ttm_squeeze(self, bb_period: int = 20, bb_std: float = 2.0, 
                    kc_period: int = 20, kc_mult: float = 1.5) -> np.ndarray:
        """
        Calculate TTM Squeeze indicator
        
        Args:
            bb_period: Bollinger Bands period (default: 20)
            bb_std: Bollinger Bands standard deviation (default: 2.0)
            kc_period: Keltner Channel period (default: 20)
            kc_mult: Keltner Channel ATR multiplier (default: 1.5)
            
        Returns:
            Squeeze values: 1 for squeeze on, 0 for squeeze off
        """
        cache_key = self._get_cache_key('ttm_squeeze', bb_period=bb_period, bb_std=bb_std,
                                       kc_period=kc_period, kc_mult=kc_mult)
        if cache_key not in self._cache:
            # Get Bollinger Bands
            bb_middle, bb_upper, bb_lower = self.bollinger_bands(bb_period, bb_std)
            
            # Get Keltner Channels
            kc_middle, kc_upper, kc_lower = self.keltner_channels(kc_period, kc_mult)
            
            # Squeeze is on when BB are inside KC
            squeeze = (bb_upper < kc_upper) & (bb_lower > kc_lower)
            self._cache[cache_key] = squeeze.astype(float)
        return self._cache[cache_key]
    
    def fisher_transform(self, period: int = 10) -> Tuple[np.ndarray, np.ndarray]:
        """
        Calculate Fisher Transform - OPTIMIZED with Numba JIT
        Achieves ~231-265x speedup over original implementation.
        
        Args:
            period: Lookback period (default: 10)
            
        Returns:
            Tuple of (fisher, trigger) values
        """
        cache_key = self._get_cache_key('fisher_transform', period=period)
        if cache_key not in self._cache:
            # Pre-compute min/max with pandas (efficient)
            min_low = pd.Series(self.low).rolling(period).min().values
            max_high = pd.Series(self.high).rolling(period).max().values
            
            # Use Numba JIT-compiled function for massive speedup
            fisher, trigger = _fisher_transform_with_minmax(
                self.close, min_low, max_high, period
            )
            
            self._cache[cache_key] = (fisher, trigger)
        return self._cache[cache_key]
    
    def chaikin_money_flow(self, period: int = 20) -> np.ndarray:
        """
        Calculate Chaikin Money Flow (CMF)
        
        Args:
            period: Lookback period (default: 20)
            
        Returns:
            CMF values
        """
        cache_key = self._get_cache_key('chaikin_money_flow', period=period)
        if cache_key not in self._cache:
            # Calculate Money Flow Multiplier (avoid division by zero)
            high_low_range = self.high - self.low
            # Use np.where to avoid division by zero
            mf_mult = np.where(
                high_low_range != 0,
                ((self.close - self.low) - (self.high - self.close)) / high_low_range,
                0.0
            )
            
            # Calculate Money Flow Volume
            mf_volume = mf_mult * self.volume
            
            # Calculate CMF
            cmf = pd.Series(mf_volume).rolling(period).sum() / pd.Series(self.volume).rolling(period).sum()
            self._cache[cache_key] = cmf.values
        return self._cache[cache_key]
    
    def donchian_channels(self, period: int = 20) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Calculate Donchian Channels
        
        Args:
            period: Lookback period (default: 20)
            
        Returns:
            Tuple of (middle, upper, lower) channels
        """
        cache_key = self._get_cache_key('donchian_channels', period=period)
        if cache_key not in self._cache:
            upper = pd.Series(self.high).rolling(period).max().values
            lower = pd.Series(self.low).rolling(period).min().values
            middle = (upper + lower) / 2
            self._cache[cache_key] = (middle, upper, lower)
        return self._cache[cache_key]
    
    def trix(self, period: int = 14) -> np.ndarray:
        """
        Calculate TRIX (Triple Exponential Average)
        
        Args:
            period: EMA period (default: 14)
            
        Returns:
            TRIX values (rate of change of triple EMA)
        """
        cache_key = self._get_cache_key('trix', period=period)
        if cache_key not in self._cache:
            # Calculate triple exponential moving average
            ema1 = talib.EMA(self.close, timeperiod=period)
            ema2 = talib.EMA(ema1, timeperiod=period)
            ema3 = talib.EMA(ema2, timeperiod=period)
            
            # Calculate rate of change
            trix = talib.ROC(ema3, timeperiod=1)
            self._cache[cache_key] = trix
        return self._cache[cache_key]
    
    def true_strength_index(self, fast_period: int = 13, slow_period: int = 25) -> np.ndarray:
        """
        Calculate True Strength Index (TSI)
        
        Args:
            fast_period: Fast EMA period (default: 13)
            slow_period: Slow EMA period (default: 25)
            
        Returns:
            TSI values
        """
        cache_key = self._get_cache_key('true_strength_index', fast_period=fast_period, slow_period=slow_period)
        if cache_key not in self._cache:
            # Calculate price momentum
            mom = pd.Series(self.close).diff()
            
            # Calculate double smoothed momentum
            ema_slow = mom.ewm(span=slow_period, adjust=False).mean()
            ema_fast = ema_slow.ewm(span=fast_period, adjust=False).mean()
            
            # Calculate double smoothed absolute momentum
            abs_mom = mom.abs()
            abs_ema_slow = abs_mom.ewm(span=slow_period, adjust=False).mean()
            abs_ema_fast = abs_ema_slow.ewm(span=fast_period, adjust=False).mean()
            
            # Calculate TSI
            tsi = 100 * (ema_fast / abs_ema_fast)
            self._cache[cache_key] = tsi.values
        return self._cache[cache_key]

    # ============================================================================
    # CRASH PROTECTION & RECOVERY INDICATORS
    # ============================================================================
    
    def ulcer_index(self, period: int = 14) -> np.ndarray:
        """
        Calculate Ulcer Index - measures downside volatility and crash risk
        Higher values indicate greater downside risk/stress

        Parameters:
            period: Lookback period for calculation (default: 14)

        Returns:
            Array of Ulcer Index values
        """
        # Ensure integer parameter
        period = int(period)
        cache_key = f'ulcer_index_{period}'
        if cache_key not in self._cache:
            close = self.df['closePrice'].values
            
            # Calculate rolling maximum (peak)
            rolling_max = pd.Series(close).rolling(window=period, min_periods=1).max()
            
            # Calculate percentage drawdown from peak
            drawdown_pct = ((close - rolling_max) / rolling_max) * 100
            
            # Square the drawdowns (penalizes larger drawdowns more)
            squared_dd = drawdown_pct ** 2
            
            # Calculate mean of squared drawdowns
            mean_squared_dd = pd.Series(squared_dd).rolling(window=period, min_periods=1).mean()
            
            # Take square root to get Ulcer Index
            ulcer_index = np.sqrt(mean_squared_dd)
            
            self._cache[cache_key] = ulcer_index.values
        return self._cache[cache_key]
    
    def maximum_drawdown(self, period: int = 252) -> Tuple[np.ndarray, np.ndarray]:
        """
        Calculate Maximum Drawdown indicator
        Tracks running drawdown percentage from recent peak
        
        Parameters:
            period: Lookback period for rolling maximum (default: 252 for 1 year)
            
        Returns:
            Tuple of (drawdown_pct, drawdown_duration)
        """
        cache_key = f'maximum_drawdown_{period}'
        if cache_key not in self._cache:
            close = self.df['closePrice'].values
            
            # Calculate rolling maximum (peak)
            rolling_max = pd.Series(close).rolling(window=period, min_periods=1).max()
            
            # Calculate percentage drawdown from peak
            drawdown_pct = ((close - rolling_max) / rolling_max) * 100
            
            # Calculate drawdown duration (bars since last peak)
            duration = np.zeros(len(close))
            for i in range(1, len(close)):
                if close[i] >= rolling_max.iloc[i]:
                    duration[i] = 0
                else:
                    duration[i] = duration[i-1] + 1
            
            self._cache[cache_key] = (drawdown_pct.values, duration)
        return self._cache[cache_key]
    
    def coppock_curve(self, roc1_period: int = 14, roc2_period: int = 11, wma_period: int = 10) -> np.ndarray:
        """
        Calculate Coppock Curve - identifies major market bottoms - OPTIMIZED with Numba JIT
        Achieves ~348-400x speedup over original implementation.
        Designed specifically to identify buying opportunities after major declines.
        
        Parameters:
            roc1_period: First ROC period (default: 14)
            roc2_period: Second ROC period (default: 11)
            wma_period: Weighted MA period (default: 10)
            
        Returns:
            Array of Coppock Curve values
        """
        cache_key = f'coppock_curve_{roc1_period}_{roc2_period}_{wma_period}'
        if cache_key not in self._cache:
            # Calculate two ROC values using talib (already optimized)
            roc1 = talib.ROC(self.close, timeperiod=roc1_period)
            roc2 = talib.ROC(self.close, timeperiod=roc2_period)
            
            # Sum the ROCs
            roc_sum = roc1 + roc2
            
            # Use Numba JIT-compiled WMA for massive speedup
            coppock = _wma_numba(roc_sum, wma_period)
            
            self._cache[cache_key] = coppock
        return self._cache[cache_key]
    
    def volatility_stop(self, period: int = 20, multiplier: float = 2.5) -> Tuple[np.ndarray, np.ndarray]:
        """
        Calculate Volatility Stop (Chandelier Exit)
        Dynamic stop loss that adapts to market volatility
        
        Parameters:
            period: ATR period (default: 20)
            multiplier: ATR multiplier (default: 2.5)
            
        Returns:
            Tuple of (long_stop, short_stop)
        """
        cache_key = f'volatility_stop_{period}_{multiplier}'
        if cache_key not in self._cache:
            high = self.df['highPrice'].values
            low = self.df['lowPrice'].values
            close = self.df['closePrice'].values
            
            atr = self.atr(period)
            
            # Calculate highest high and lowest low
            highest = pd.Series(high).rolling(window=period, min_periods=1).max().values
            lowest = pd.Series(low).rolling(window=period, min_periods=1).min().values
            
            # Calculate stops
            long_stop = highest - (atr * multiplier)
            short_stop = lowest + (atr * multiplier)
            
            self._cache[cache_key] = (long_stop, short_stop)
        return self._cache[cache_key]
    
    def market_regime(self, adx_period: int = 14, adx_threshold: float = 25) -> np.ndarray:
        """
        Calculate Market Regime Filter
        Identifies trending vs ranging vs crash markets

        Parameters:
            adx_period: Period for ADX calculation (default: 14)
            adx_threshold: Threshold for trend strength (default: 25)

        Returns:
            Array of regime values: 2=Strong Trend, 1=Weak Trend, 0=Range, -1=Crash
        """
        # Ensure integer parameter
        adx_period = int(adx_period)
        
        cache_key = f'market_regime_{adx_period}_{adx_threshold}'
        if cache_key not in self._cache:
            close = self.df['closePrice'].values
            
            # Get ADX for trend strength
            adx = self.adx(adx_period)
            
            # Get drawdown for crash detection
            dd_pct, _ = self.maximum_drawdown(period=20)
            
            # Calculate short and long MAs for trend direction
            ma_short = pd.Series(close).rolling(window=10, min_periods=1).mean().values
            ma_long = pd.Series(close).rolling(window=50, min_periods=1).mean().values
            
            # Determine regime
            regime = np.zeros(len(close))
            for i in range(len(close)):
                if dd_pct[i] < -10:  # Crash mode
                    regime[i] = -1
                elif adx[i] > adx_threshold:  # Trending
                    if ma_short[i] > ma_long[i]:
                        regime[i] = 2  # Strong uptrend
                    else:
                        regime[i] = -2  # Strong downtrend
                else:  # Ranging
                    regime[i] = 0
            
            self._cache[cache_key] = regime
        return self._cache[cache_key]
    
    def accumulation_distribution(self) -> np.ndarray:
        """
        Calculate Accumulation/Distribution Line
        Shows if smart money is accumulating or distributing
        
        Returns:
            Array of A/D Line values
        """
        cache_key = 'accumulation_distribution'
        if cache_key not in self._cache:
            high = self.df['highPrice'].values
            low = self.df['lowPrice'].values
            close = self.df['closePrice'].values
            volume = self.df['lastTradedVolume'].values
            
            # Calculate money flow multiplier
            # Use np.errstate to suppress divide-by-zero warning when high == low
            with np.errstate(divide='ignore', invalid='ignore'):
                mfm = np.where(high != low,
                              ((close - low) - (high - close)) / (high - low),
                              0)
            
            # Calculate money flow volume
            mfv = mfm * volume
            
            # Calculate cumulative A/D Line
            ad_line = np.cumsum(mfv)
            
            self._cache[cache_key] = ad_line
        return self._cache[cache_key]
    
    def inverse_fisher_rsi(self, period: int = 5) -> np.ndarray:
        """
        Calculate Inverse Fisher Transform of RSI
        Provides sharper, more binary signals (-1 to +1)
        
        Parameters:
            period: RSI period (default: 5)
            
        Returns:
            Array of IFT RSI values (-1 to +1)
        """
        cache_key = f'inverse_fisher_rsi_{period}'
        if cache_key not in self._cache:
            # Get RSI
            rsi = self.rsi(period)
            
            # Normalize RSI to -1 to +1
            normalized_rsi = 0.1 * (rsi - 50)
            
            # Apply Inverse Fisher Transform
            ift_rsi = (np.exp(2 * normalized_rsi) - 1) / (np.exp(2 * normalized_rsi) + 1)
            
            self._cache[cache_key] = ift_rsi
        return self._cache[cache_key]
    
    def heikin_ashi(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Calculate Heikin-Ashi candles
        Smoothed candles that filter out market noise
        
        Returns:
            Tuple of (ha_open, ha_high, ha_low, ha_close)
        """
        cache_key = 'heikin_ashi'
        if cache_key not in self._cache:
            open_price = self.df['openPrice'].values
            high = self.df['highPrice'].values
            low = self.df['lowPrice'].values
            close = self.df['closePrice'].values
            
            ha_close = (open_price + high + low + close) / 4
            ha_open = np.zeros(len(close))
            ha_high = np.zeros(len(close))
            ha_low = np.zeros(len(close))
            
            # First candle
            ha_open[0] = (open_price[0] + close[0]) / 2
            ha_high[0] = high[0]
            ha_low[0] = low[0]
            
            # Calculate rest
            for i in range(1, len(close)):
                ha_open[i] = (ha_open[i-1] + ha_close[i-1]) / 2
                ha_high[i] = max(high[i], ha_open[i], ha_close[i])
                ha_low[i] = min(low[i], ha_open[i], ha_close[i])
            
            self._cache[cache_key] = (ha_open, ha_high, ha_low, ha_close)
        return self._cache[cache_key]
    
    def pivot_points(self, pivot_type: str = 'camarilla') -> Dict[str, np.ndarray]:
        """
        Calculate Pivot Points (Camarilla or Classic)
        Key support and resistance levels
        
        Parameters:
            pivot_type: 'camarilla' or 'classic' (default: 'camarilla')
            
        Returns:
            Dictionary with pivot levels (PP, R1-R4, S1-S4)
        """
        cache_key = f'pivot_points_{pivot_type}'
        if cache_key not in self._cache:
            high = self.df['highPrice'].values
            low = self.df['lowPrice'].values
            close = self.df['closePrice'].values
            
            # Calculate pivot point
            pp = (high + low + close) / 3
            
            levels = {'PP': pp}
            
            if pivot_type == 'camarilla':
                # Camarilla pivot points
                range_hl = high - low
                levels['R4'] = close + range_hl * 1.1 / 2
                levels['R3'] = close + range_hl * 1.1 / 4
                levels['R2'] = close + range_hl * 1.1 / 6
                levels['R1'] = close + range_hl * 1.1 / 12
                levels['S1'] = close - range_hl * 1.1 / 12
                levels['S2'] = close - range_hl * 1.1 / 6
                levels['S3'] = close - range_hl * 1.1 / 4
                levels['S4'] = close - range_hl * 1.1 / 2
            else:  # classic
                levels['R3'] = pp + 2 * (high - low)
                levels['R2'] = pp + (high - low)
                levels['R1'] = 2 * pp - low
                levels['S1'] = 2 * pp - high
                levels['S2'] = pp - (high - low)
                levels['S3'] = pp - 2 * (high - low)
            
            self._cache[cache_key] = levels
        return self._cache[cache_key]
    
    def volume_weighted_macd(self, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Calculate Volume-Weighted MACD
        MACD that considers volume for confirmation
        
        Parameters:
            fast_period: Fast EMA period (default: 12)
            slow_period: Slow EMA period (default: 26)
            signal_period: Signal line period (default: 9)
            
        Returns:
            Tuple of (vwmacd_line, vwmacd_signal, vwmacd_histogram)
        """
        cache_key = f'volume_weighted_macd_{fast_period}_{slow_period}_{signal_period}'
        if cache_key not in self._cache:
            close = self.df['closePrice'].values
            volume = self.df['lastTradedVolume'].values
            
            # Calculate volume-weighted price
            vwp = close * volume
            
            # Calculate volume-weighted EMAs
            vwp_series = pd.Series(vwp)
            vol_series = pd.Series(volume)
            
            # Fast VWMA
            vwp_fast_ema = vwp_series.ewm(span=fast_period, adjust=False).mean()
            vol_fast_ema = vol_series.ewm(span=fast_period, adjust=False).mean()
            vwma_fast = vwp_fast_ema / vol_fast_ema
            
            # Slow VWMA
            vwp_slow_ema = vwp_series.ewm(span=slow_period, adjust=False).mean()
            vol_slow_ema = vol_series.ewm(span=slow_period, adjust=False).mean()
            vwma_slow = vwp_slow_ema / vol_slow_ema
            
            # VWMACD line
            vwmacd_line = vwma_fast - vwma_slow
            
            # Signal line
            vwmacd_signal = vwmacd_line.ewm(span=signal_period, adjust=False).mean()
            
            # Histogram
            vwmacd_hist = vwmacd_line - vwmacd_signal
            
            self._cache[cache_key] = (vwmacd_line.values, vwmacd_signal.values, vwmacd_hist.values)
        return self._cache[cache_key]

    # ============================================================================
    # NEW TIER-1 INDICATORS (From Manus Analysis)
    # ============================================================================
    
    def ttm_squeeze(self, bb_length: int = 20, bb_mult: float = 2.0, 
                    kc_length: int = 20, kc_mult: float = 1.5,
                    momentum_length: int = 12) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        TTM Squeeze Momentum Indicator
        Detects low-volatility squeeze conditions that precede major breakouts.
        
        Returns:
            Tuple of (squeeze_on, squeeze_off, momentum) arrays
            - squeeze_on: Boolean, True when squeeze is active (BB inside KC)
            - squeeze_off: Boolean, True when squeeze just released
            - momentum: Float, momentum histogram values
        """
        # Ensure integer parameters (can receive floats from parameter space sampling)
        bb_length = int(bb_length)
        kc_length = int(kc_length)
        momentum_length = int(momentum_length)
        cache_key = f'ttm_squeeze_{bb_length}_{bb_mult}_{kc_length}_{kc_mult}_{momentum_length}'
        if cache_key not in self._cache:
            close = pd.Series(self.close)
            high = pd.Series(self.high)
            low = pd.Series(self.low)
            
            # Calculate Bollinger Bands
            basis = close.rolling(window=bb_length).mean()
            std_dev = close.rolling(window=bb_length).std()
            bb_upper = basis + (bb_mult * std_dev)
            bb_lower = basis - (bb_mult * std_dev)
            
            # Calculate Keltner Channels
            typical_price = (high + low + close) / 3
            kc_basis = typical_price.rolling(window=kc_length).mean()
            
            # True Range for ATR
            tr1 = high - low
            tr2 = abs(high - close.shift(1))
            tr3 = abs(low - close.shift(1))
            true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = true_range.rolling(window=kc_length).mean()
            
            kc_upper = kc_basis + (kc_mult * atr)
            kc_lower = kc_basis - (kc_mult * atr)
            
            # Squeeze is ON when BB inside KC
            squeeze_on = (bb_lower > kc_lower) & (bb_upper < kc_upper)
            
            # Squeeze OFF when it was ON and now it's not (release moment)
            squeeze_off = (squeeze_on.shift(1).fillna(False).astype(bool)) & ~squeeze_on
            
            # Calculate Momentum using Linear Regression
            momentum = pd.Series(index=close.index, dtype=float)
            for i in range(momentum_length, len(close)):
                y = close.iloc[i - momentum_length:i].values
                x = np.arange(len(y))
                coeffs = np.polyfit(x, y, 1)
                regression_line = np.polyval(coeffs, x)
                momentum.iloc[i] = y[-1] - regression_line[-1]
            
            self._cache[cache_key] = (
                squeeze_on.fillna(False).values.astype(bool),
                squeeze_off.fillna(False).values.astype(bool),
                momentum.fillna(0).values
            )
        return self._cache[cache_key]
    
    def rsi_hidden_divergence(self, rsi_period: int = 14, lookback: int = 20) -> Tuple[np.ndarray, np.ndarray]:
        """
        RSI Hidden Divergence Detection
        
        Hidden bullish divergence: Price makes higher low, RSI makes lower low
        Hidden bearish divergence: Price makes lower high, RSI makes higher high
        
        Returns:
            Tuple of (hidden_bullish, hidden_bearish) boolean arrays
        """
        # Ensure integer parameters
        rsi_period = int(rsi_period)
        lookback = int(lookback)
        cache_key = f'rsi_hidden_div_{rsi_period}_{lookback}'
        if cache_key not in self._cache:
            close = pd.Series(self.close)
            rsi = pd.Series(self.rsi(rsi_period))
            
            hidden_bullish = np.zeros(len(close), dtype=bool)
            hidden_bearish = np.zeros(len(close), dtype=bool)
            
            for i in range(lookback + 5, len(close)):
                # Look for local lows in price
                window_close = close.iloc[i-lookback:i+1]
                window_rsi = rsi.iloc[i-lookback:i+1]
                
                if len(window_close) < lookback:
                    continue
                    
                # Find the minimum in first half and current value
                half = lookback // 2
                first_half_min_idx = window_close.iloc[:half].idxmin()
                second_half_min_idx = window_close.iloc[half:].idxmin()
                
                if first_half_min_idx is not np.nan and second_half_min_idx is not np.nan:
                    prev_price_low = window_close.loc[first_half_min_idx]
                    curr_price_low = window_close.loc[second_half_min_idx]
                    
                    prev_rsi_low = window_rsi.loc[first_half_min_idx] if first_half_min_idx in window_rsi.index else np.nan
                    curr_rsi_low = window_rsi.loc[second_half_min_idx] if second_half_min_idx in window_rsi.index else np.nan
                    
                    # Hidden bullish: Price higher low, RSI lower low
                    if (not np.isnan(prev_rsi_low) and not np.isnan(curr_rsi_low) and
                        curr_price_low > prev_price_low * 1.001 and  # Price making higher low
                        curr_rsi_low < prev_rsi_low - 1):  # RSI making lower low
                        hidden_bullish[i] = True
                
                # Find highs for bearish divergence
                first_half_max_idx = window_close.iloc[:half].idxmax()
                second_half_max_idx = window_close.iloc[half:].idxmax()
                
                if first_half_max_idx is not np.nan and second_half_max_idx is not np.nan:
                    prev_price_high = window_close.loc[first_half_max_idx]
                    curr_price_high = window_close.loc[second_half_max_idx]
                    
                    prev_rsi_high = window_rsi.loc[first_half_max_idx] if first_half_max_idx in window_rsi.index else np.nan
                    curr_rsi_high = window_rsi.loc[second_half_max_idx] if second_half_max_idx in window_rsi.index else np.nan
                    
                    # Hidden bearish: Price lower high, RSI higher high
                    if (not np.isnan(prev_rsi_high) and not np.isnan(curr_rsi_high) and
                        curr_price_high < prev_price_high * 0.999 and  # Price making lower high
                        curr_rsi_high > prev_rsi_high + 1):  # RSI making higher high
                        hidden_bearish[i] = True
            
            self._cache[cache_key] = (hidden_bullish, hidden_bearish)
        return self._cache[cache_key]
    
    def volume_accumulation(self, volume_period: int = 14, price_period: int = 14) -> Tuple[np.ndarray, np.ndarray]:
        """
        Volume Accumulation Divergence Detection (OPTIMIZED - vectorized, no loops)
        Detects when volume is increasing while price remains flat (accumulation before breakout)
        
        Returns:
            Tuple of (accumulation_signal, accumulation_strength) arrays
            - accumulation_signal: Boolean, True when accumulation detected
            - accumulation_strength: Float, strength of accumulation
        """
        cache_key = f'vol_accum_{volume_period}_{price_period}'
        if cache_key not in self._cache:
            n = len(self.close)
            close = pd.Series(self.close)
            volume = pd.Series(self.volume)
            
            # ================================================================
            # VECTORIZED LINEAR REGRESSION SLOPE CALCULATION
            # Instead of calling np.polyfit() 39,000 times (which takes hours),
            # we use the closed-form formula for linear regression slope:
            # slope = (n*Σxy - Σx*Σy) / (n*Σx² - (Σx)²)
            # 
            # For a rolling window of fixed size n:
            # - Σx = 0+1+2+...+(n-1) = n*(n-1)/2 (constant)
            # - Σx² = 0²+1²+...+(n-1)² = n*(n-1)*(2n-1)/6 (constant)
            # - Σxy and Σy are the only variables we need to compute per-window
            # ================================================================
            
            # Pre-compute constants for the regression formula
            n_period = volume_period
            sum_x = n_period * (n_period - 1) / 2  # Sum of 0,1,2,...,n-1
            sum_x2 = n_period * (n_period - 1) * (2 * n_period - 1) / 6  # Sum of squares
            denominator = n_period * sum_x2 - sum_x * sum_x
            
            # Compute Σy (sum of volume in rolling window) - vectorized
            sum_y = volume.rolling(window=n_period).sum()
            
            # Compute Σxy (sum of index * volume in rolling window)
            # For this, we multiply volume by position weights [0,1,2,...,n-1]
            # and use rolling sum
            # Create weighted volume series: for each window, sum(i * vol[i]) for i in 0..n-1
            # We can compute this using: rolling_sum(vol * range) 
            # But since range changes per window, we use a trick:
            # Σxy = Σ(i * y_i) for i=0..n-1 in the window
            # Using the formula for cumulative weighted sum
            weights = np.arange(n_period)
            
            # Compute sum_xy using rolling window with pre-computed weights
            # This is equivalent to: for each position, sum of (0*v0 + 1*v1 + ... + (n-1)*v_{n-1})
            def compute_sum_xy(x):
                if len(x) < n_period:
                    return np.nan
                return np.dot(weights, x[-n_period:])
            
            sum_xy = volume.rolling(window=n_period).apply(lambda x: np.dot(weights, x), raw=True)
            
            # Calculate slope for all windows at once
            # slope = (n*Σxy - Σx*Σy) / (n*Σx² - (Σx)²)
            volume_trend = (n_period * sum_xy - sum_x * sum_y) / denominator
            
            # Average volume (for normalization)
            avg_volume = volume.rolling(window=volume_period).mean()
            
            # Normalize volume trend
            normalized_trend = np.where(avg_volume > 0, volume_trend / avg_volume, 0)
            
            # Price volatility
            returns = close.pct_change()
            price_volatility = returns.rolling(window=price_period).std()
            
            # Accumulation conditions (vectorized)
            # Note: normalized_trend is already a numpy array from np.where()
            # price_volatility is a pandas Series, so we convert to numpy
            price_vol_arr = price_volatility.values
            
            volume_increasing = normalized_trend > 0.1  # 10% increase in trend
            price_flat = (price_vol_arr < 0.02) & (~np.isnan(price_vol_arr))
            
            # Signal: both conditions must be true
            accumulation_signal = volume_increasing & price_flat
            
            # Strength calculation (vectorized)
            accumulation_strength = np.zeros(n, dtype=float)
            valid_mask = accumulation_signal & (price_vol_arr > 0)
            accumulation_strength[valid_mask] = np.abs(normalized_trend[valid_mask]) / price_vol_arr[valid_mask]
            # For price_volatility == 0, use simplified strength
            zero_vol_mask = accumulation_signal & (price_vol_arr == 0)
            accumulation_strength[zero_vol_mask] = np.abs(normalized_trend[zero_vol_mask]) * 100
            
            self._cache[cache_key] = (accumulation_signal, accumulation_strength)
        return self._cache[cache_key]

    # ============================================================================
    # NEW INDICATORS - Zero-Lag Aroon Oscillator & Multi-MA Crossover
    # ============================================================================
    
    def aroon_oscillator_zerolag(self, period: int = 29, smooth: int = 25, 
                                  signal_len: int = 10, threshold: int = 0,
                                  gain_limit: int = 10, mode: str = 'strong') -> pd.Series:
        """
        BigBeluga-style Aroon Oscillator with Zero-Lag smoothing.
        Based on John Ehlers' zero-lag EMA technique for minimal lag while maintaining smoothness.
        
        Parameters:
            period: Aroon lookback period (default: 29)
            smooth: Zero-lag EMA smoothing period (default: 25)
            signal_len: Signal line SMA period for reversion mode (default: 10)
            threshold: Oscillator threshold for entry in strong mode (default: 0)
            gain_limit: Zero-lag gain optimization range (default: 10)
            mode: Signal mode - 'strong', 'trend', 'reversion', or 'bullish'
                  - 'strong': Oscillator > threshold (strong trend confirmation)
                  - 'trend': Oscillator crosses above zero (trend change)
                  - 'reversion': Oscillator crosses above signal line (mean reversion)
                  - 'bullish': Oscillator is positive (simple bullish bias)
        
        Returns:
            pd.Series of True/False buy signals
        """
        cache_key = self._get_cache_key('aroon_oscillator_zerolag', period=period, 
                                         smooth=smooth, signal_len=signal_len,
                                         threshold=threshold, gain_limit=gain_limit, mode=mode)
        
        if cache_key not in self._cache:
            # Calculate Aroon Up and Down using talib
            aroon_down, aroon_up = talib.AROON(self.high, self.low, timeperiod=period)
            
            # Calculate Aroon Oscillator
            aroon_osc = aroon_up - aroon_down
            
            # Apply Zero-Lag EMA smoothing
            aroon_osc_smooth = zero_lag_ema(aroon_osc, smooth, gain_limit)
            
            # Calculate signal line for reversion mode
            signal_line = pd.Series(aroon_osc_smooth).rolling(signal_len).mean().values
            
            # Generate signals based on mode
            if mode == 'strong':
                # Strong trend: Oscillator above threshold
                signal = pd.Series(aroon_osc_smooth > threshold, index=self.df.index)
            elif mode == 'trend':
                # Trend change: Oscillator crosses above zero
                cross_above = (aroon_osc_smooth > 0) & (np.roll(aroon_osc_smooth, 1) <= 0)
                cross_above[0] = False  # First value can't be a crossover
                signal = pd.Series(cross_above, index=self.df.index)
            elif mode == 'reversion':
                # Mean reversion: Oscillator crosses above signal line
                cross_above = (aroon_osc_smooth > signal_line) & (np.roll(aroon_osc_smooth, 1) <= np.roll(signal_line, 1))
                cross_above[0] = False
                signal = pd.Series(cross_above, index=self.df.index)
            elif mode == 'bullish':
                # Simple bullish: Oscillator is positive
                signal = pd.Series(aroon_osc_smooth > 0, index=self.df.index)
            else:
                # Default to strong mode
                signal = pd.Series(aroon_osc_smooth > threshold, index=self.df.index)
            
            self._cache[cache_key] = signal
        return self._cache[cache_key]

    def multi_ma_cross(self, fast_ma_type: str = 'EMA', fast_ma_period: int = 50,
                       slow_ma_type: str = 'EMA', slow_ma_period: int = 200,
                       source: str = 'close') -> np.ndarray:
        """
        Flexible multi-moving average crossover signal.
        Supports EMA, SMA, VWMA, and VWAP for both fast and slow moving averages.
        
        Parameters:
            fast_ma_type: Type of fast MA ('EMA', 'SMA', 'VWMA', 'VWAP')
            fast_ma_period: Period for fast MA (default: 50)
            slow_ma_type: Type of slow MA ('EMA', 'SMA', 'VWMA', 'VWAP')
            slow_ma_period: Period for slow MA (default: 200)
            source: Price source ('open', 'high', 'low', 'close')
        
        Returns:
            Boolean array where True indicates bullish crossover
        """
        cache_key = self._get_cache_key('multi_ma_cross', fast_ma_type=fast_ma_type,
                                         fast_ma_period=fast_ma_period, slow_ma_type=slow_ma_type,
                                         slow_ma_period=slow_ma_period, source=source)
        
        if cache_key not in self._cache:
            # Select price source
            if source == 'open':
                price_src = self.open
            elif source == 'high':
                price_src = self.high
            elif source == 'low':
                price_src = self.low
            else:
                price_src = self.close

            def get_ma(ma_type: str, period: int, src: np.ndarray) -> np.ndarray:
                """Calculate moving average based on type."""
                if ma_type == 'SMA':
                    return talib.SMA(src, timeperiod=period)
                elif ma_type == 'VWMA':
                    # Volume Weighted Moving Average
                    return talib.WMA(src * self.volume, timeperiod=period) / talib.SMA(self.volume, timeperiod=period)
                elif ma_type == 'VWAP':
                    # Rolling VWAP approximation
                    return talib.SUM(src * self.volume, timeperiod=period) / talib.SUM(self.volume, timeperiod=period)
                else:  # Default to EMA
                    return talib.EMA(src, timeperiod=period)

            # Calculate both moving averages
            fast_ma = get_ma(fast_ma_type, fast_ma_period, price_src)
            slow_ma = get_ma(slow_ma_type, slow_ma_period, price_src)

            # Detect bullish crossover (fast crosses above slow)
            cross_above = (fast_ma > slow_ma) & (np.roll(fast_ma, 1) <= np.roll(slow_ma, 1))
            cross_above[0] = False  # First value can't be a crossover
            
            self._cache[cache_key] = cross_above
        return self._cache[cache_key]

    # ============================================================================
    # LETF STRATEGY INDICATORS (SOXL/TECL Optimized)
    # Based on Composer and academic research for leveraged ETF trading
    # ============================================================================
    
    def intraday_return_from_prior_close(self) -> np.ndarray:
        """
        Calculate the return from prior day's close to current candle's close.
        This is essential for LETF strategies that trade based on intraday momentum.
        
        Used by: Rebalancing Front-Run Strategy (6% threshold)
        - If return >= +6% at 2:15 PM → BUY, hold until close
        - If return <= -6% at 2:15 PM → SHORT, cover at close
        
        Returns:
            Array of percentage returns from prior close (e.g., 0.06 = 6%)
        """
        cache_key = 'intraday_return_from_prior_close'
        if cache_key not in self._cache:
            # Get daily grouping - need to find prior day's close for each candle
            df = self.df.copy()
            
            # Extract date from snapshotTime
            if 'date' not in df.columns:
                df['date'] = pd.to_datetime(df['snapshotTime']).dt.date
            
            # Get last close of each day
            daily_close = df.groupby('date')['closePrice'].last()
            
            # Map prior day's close to each row
            dates = df['date'].values
            prior_closes = np.full(len(df), np.nan)
            
            unique_dates = sorted(df['date'].unique())
            date_to_prior_close = {}
            
            for i, date in enumerate(unique_dates):
                if i > 0:
                    prior_date = unique_dates[i-1]
                    date_to_prior_close[date] = daily_close[prior_date]
            
            for idx, date in enumerate(dates):
                if date in date_to_prior_close:
                    prior_closes[idx] = date_to_prior_close[date]
            
            # Calculate return from prior close
            returns = (self.close - prior_closes) / prior_closes
            self._cache[cache_key] = returns
        return self._cache[cache_key]

    def intraday_return_bullish(self, threshold: float = 6.0) -> np.ndarray:
        """
        Signal when intraday return from prior close exceeds threshold.
        
        Args:
            threshold: Percentage threshold (default 6.0 = 6%)
        
        Returns:
            Boolean array - True when return >= threshold%
        """
        cache_key = self._get_cache_key('intraday_return_bullish', threshold=threshold)
        if cache_key not in self._cache:
            returns = self.intraday_return_from_prior_close()
            self._cache[cache_key] = returns >= (threshold / 100.0)
        return self._cache[cache_key]

    def intraday_return_bearish(self, threshold: float = -6.0) -> np.ndarray:
        """
        Signal when intraday return from prior close is below negative threshold.
        
        Args:
            threshold: Percentage threshold (default -6.0 = -6%)
        
        Returns:
            Boolean array - True when return <= threshold%
        """
        cache_key = self._get_cache_key('intraday_return_bearish', threshold=threshold)
        if cache_key not in self._cache:
            returns = self.intraday_return_from_prior_close()
            self._cache[cache_key] = returns <= (threshold / 100.0)
        return self._cache[cache_key]

    def daily_return(self) -> np.ndarray:
        """
        Calculate the daily return (open to close) for each candle's day.
        Uses the day's open price to current close.
        
        Returns:
            Array of daily returns (e.g., 0.085 = 8.5%)
        """
        cache_key = 'daily_return'
        if cache_key not in self._cache:
            df = self.df.copy()
            
            if 'date' not in df.columns:
                df['date'] = pd.to_datetime(df['snapshotTime']).dt.date
            
            # Get first open of each day
            daily_open = df.groupby('date')['openPrice'].first()
            
            # Map day's open to each row
            dates = df['date'].values
            day_opens = np.array([daily_open.get(d, np.nan) for d in dates])
            
            # Calculate return from day's open
            returns = (self.close - day_opens) / day_opens
            self._cache[cache_key] = returns
        return self._cache[cache_key]

    def daily_spike_up(self, threshold: float = 8.5) -> np.ndarray:
        """
        Detect large daily UP moves - used for position management in LETF strategies.
        The Composer strategy flips to inverse after >8.5% daily spike.
        
        For single-epic trading: This signals to EXIT or go DEFENSIVE (not buy).
        
        Args:
            threshold: Percentage threshold (default 8.5%)
        
        Returns:
            Boolean array - True when daily return >= threshold%
        """
        cache_key = self._get_cache_key('daily_spike_up', threshold=threshold)
        if cache_key not in self._cache:
            daily_ret = self.daily_return()
            self._cache[cache_key] = daily_ret >= (threshold / 100.0)
        return self._cache[cache_key]

    def daily_spike_down(self, threshold: float = 2.0) -> np.ndarray:
        """
        Detect large daily DOWN moves - signals defensive mode.
        The Composer strategy goes defensive after >2% daily loss.
        
        Args:
            threshold: Percentage threshold (default 2.0%)
        
        Returns:
            Boolean array - True when daily return <= -threshold%
        """
        cache_key = self._get_cache_key('daily_spike_down', threshold=threshold)
        if cache_key not in self._cache:
            daily_ret = self.daily_return()
            self._cache[cache_key] = daily_ret <= -(threshold / 100.0)
        return self._cache[cache_key]

    def ma_band_position(self, short_period: int = 20, long_period: int = 90) -> np.ndarray:
        """
        Calculate position relative to two moving average bands.
        Used by Composer strategy for defensive mode detection.
        
        Returns:
            Array of positions:
            - 1: Price above BOTH MAs (bullish - OK to trade)
            - 0: Price BETWEEN MAs (defensive - don't enter new positions)
            - -1: Price below BOTH MAs (bearish)
        """
        cache_key = self._get_cache_key('ma_band_position', short_period=short_period, long_period=long_period)
        if cache_key not in self._cache:
            short_ma = talib.SMA(self.close, timeperiod=short_period)
            long_ma = talib.SMA(self.close, timeperiod=long_period)
            
            position = np.zeros(len(self.close))
            
            # Above both MAs = bullish (1)
            above_both = (self.close > short_ma) & (self.close > long_ma)
            position[above_both] = 1
            
            # Below both MAs = bearish (-1)
            below_both = (self.close < short_ma) & (self.close < long_ma)
            position[below_both] = -1
            
            # Between MAs = neutral/defensive (0) - already initialized
            
            self._cache[cache_key] = position
        return self._cache[cache_key]

    def ma_band_bullish(self, short_period: int = 20, long_period: int = 90) -> np.ndarray:
        """
        Signal when price is above both MAs (bullish zone).
        Safe to enter long positions.
        
        Returns:
            Boolean array - True when price > both short and long MA
        """
        cache_key = self._get_cache_key('ma_band_bullish', short_period=short_period, long_period=long_period)
        if cache_key not in self._cache:
            position = self.ma_band_position(short_period, long_period)
            self._cache[cache_key] = position == 1
        return self._cache[cache_key]

    def ma_band_defensive(self, short_period: int = 20, long_period: int = 90) -> np.ndarray:
        """
        Signal when price is BETWEEN the two MAs (defensive zone).
        The Composer strategy avoids new entries in this zone.
        
        Returns:
            Boolean array - True when price is between short and long MA
        """
        cache_key = self._get_cache_key('ma_band_defensive', short_period=short_period, long_period=long_period)
        if cache_key not in self._cache:
            position = self.ma_band_position(short_period, long_period)
            self._cache[cache_key] = position == 0
        return self._cache[cache_key]

    def multi_day_momentum(self, days: int = 5) -> np.ndarray:
        """
        Calculate return over multiple days (using daily closes).
        Used for momentum-based strategies like "SOXX up >5% over 5 days".
        
        Args:
            days: Number of trading days to look back
        
        Returns:
            Array of N-day returns (e.g., 0.05 = 5%)
        """
        cache_key = self._get_cache_key('multi_day_momentum', days=days)
        if cache_key not in self._cache:
            df = self.df.copy()
            
            if 'date' not in df.columns:
                df['date'] = pd.to_datetime(df['snapshotTime']).dt.date
            
            # Get daily closes
            daily_close = df.groupby('date')['closePrice'].last()
            unique_dates = sorted(df['date'].unique())
            
            # Map N-day return to each row
            returns = np.full(len(df), np.nan)
            
            date_to_n_day_return = {}
            for i, date in enumerate(unique_dates):
                if i >= days:
                    current_close = daily_close[date]
                    prior_close = daily_close[unique_dates[i - days]]
                    date_to_n_day_return[date] = (current_close - prior_close) / prior_close
            
            dates = df['date'].values
            for idx, date in enumerate(dates):
                if date in date_to_n_day_return:
                    returns[idx] = date_to_n_day_return[date]
            
            self._cache[cache_key] = returns
        return self._cache[cache_key]

    def multi_day_momentum_bullish(self, days: int = 5, threshold: float = 5.0) -> np.ndarray:
        """
        Signal when N-day momentum exceeds threshold.
        
        Args:
            days: Number of days for momentum calculation
            threshold: Percentage threshold (default 5.0%)
        
        Returns:
            Boolean array - True when N-day return >= threshold%
        """
        cache_key = self._get_cache_key('multi_day_momentum_bullish', days=days, threshold=threshold)
        if cache_key not in self._cache:
            momentum = self.multi_day_momentum(days)
            self._cache[cache_key] = momentum >= (threshold / 100.0)
        return self._cache[cache_key]

    def rsi_extreme(self, period: int = 10, high_threshold: float = 90, low_threshold: float = 15) -> np.ndarray:
        """
        Detect extreme RSI levels (very overbought or oversold).
        Used for NVDA/AMD heat detection in tactical chip strategies.
        
        Returns:
            Array of values:
            - 1: RSI >= high_threshold (extremely overbought - bearish signal)
            - -1: RSI <= low_threshold (extremely oversold - bullish signal)
            - 0: RSI between thresholds (neutral)
        """
        cache_key = self._get_cache_key('rsi_extreme', period=period, high_threshold=high_threshold, low_threshold=low_threshold)
        if cache_key not in self._cache:
            rsi = self.rsi(period)
            
            result = np.zeros(len(rsi))
            result[rsi >= high_threshold] = 1  # Overbought
            result[rsi <= low_threshold] = -1  # Oversold
            
            self._cache[cache_key] = result
        return self._cache[cache_key]

    def rsi_extreme_oversold(self, period: int = 10, threshold: float = 15) -> np.ndarray:
        """
        Signal when RSI is at extreme oversold levels.
        Strong bullish signal for mean reversion.
        
        Returns:
            Boolean array - True when RSI <= threshold
        """
        cache_key = self._get_cache_key('rsi_extreme_oversold', period=period, threshold=threshold)
        if cache_key not in self._cache:
            rsi = self.rsi(period)
            self._cache[cache_key] = rsi <= threshold
        return self._cache[cache_key]

    def rsi_extreme_overbought(self, period: int = 10, threshold: float = 90) -> np.ndarray:
        """
        Signal when RSI is at extreme overbought levels.
        Defensive signal - avoid new entries.
        
        Returns:
            Boolean array - True when RSI >= threshold
        """
        cache_key = self._get_cache_key('rsi_extreme_overbought', period=period, threshold=threshold)
        if cache_key not in self._cache:
            rsi = self.rsi(period)
            self._cache[cache_key] = rsi >= threshold
        return self._cache[cache_key]

    def composer_rsi_strategy(self, rsi_period: int = 10, rsi_oversold: float = 29.0, 
                               rsi_overbought: float = 80.0, ma_short: int = 20, 
                               ma_long: int = 90, spike_down_threshold: float = 2.0) -> np.ndarray:
        """
        Composer RSI Strategy for SOXL/TECL (117,356% cumulative return!)
        
        BUY signal when ALL conditions are met:
        1. RSI(10) < 29 (oversold - the core signal)
        2. NOT in defensive zone (price NOT between 20-day and 90-day MA)
        3. NOT after a big down day (>2% loss)
        4. NOT overbought (RSI < 80)
        
        This is the single-epic (SOXL only) version that keeps BUY logic
        and uses "flip to inverse" conditions as reasons to NOT enter.
        
        Args:
            rsi_period: RSI period (default 10)
            rsi_oversold: RSI oversold threshold (default 29)
            rsi_overbought: RSI overbought threshold (default 80) - defensive
            ma_short: Short MA period for band (default 20)
            ma_long: Long MA period for band (default 90)
            spike_down_threshold: Daily loss threshold for defensive mode (default 2%)
        
        Returns:
            Boolean array - True when all BUY conditions are met
        """
        cache_key = self._get_cache_key('composer_rsi_strategy', 
                                        rsi_period=rsi_period, rsi_oversold=rsi_oversold,
                                        rsi_overbought=rsi_overbought, ma_short=ma_short,
                                        ma_long=ma_long, spike_down_threshold=spike_down_threshold)
        if cache_key not in self._cache:
            # Core signal: RSI oversold
            rsi = self.rsi(rsi_period)
            rsi_buy_signal = rsi < rsi_oversold
            
            # Defensive filters (must NOT be true for a valid buy)
            # 1. Not in MA band (defensive zone)
            ma_position = self.ma_band_position(ma_short, ma_long)
            not_in_defensive_zone = ma_position != 0  # Either bullish (1) or bearish (-1), not between
            
            # 2. Not after big down day
            daily_ret = self.daily_return()
            not_big_down_day = daily_ret > -(spike_down_threshold / 100.0)
            
            # 3. Not overbought
            not_overbought = rsi < rsi_overbought
            
            # Combined signal: BUY only when core signal AND all defensive filters pass
            buy_signal = rsi_buy_signal & not_in_defensive_zone & not_big_down_day & not_overbought
            
            self._cache[cache_key] = buy_signal
        return self._cache[cache_key]

    def aroon_strong_uptrend(self, period: int = 25, up_threshold: float = 70.0, 
                              down_threshold: float = 30.0) -> np.ndarray:
        """
        Aroon Strong Uptrend Signal - 90% win rate in backtests!
        
        BUY when:
        - AroonUp > 70 (strong upward momentum)
        - AroonDown < 30 (weak downward pressure)
        
        Backtested: 248 cases, 233 successful = 90% odds of success
        
        Args:
            period: Aroon period (default 25)
            up_threshold: AroonUp must be above this (default 70)
            down_threshold: AroonDown must be below this (default 30)
        
        Returns:
            Boolean array - True when strong uptrend conditions met
        """
        cache_key = self._get_cache_key('aroon_strong_uptrend', period=period, 
                                        up_threshold=up_threshold, down_threshold=down_threshold)
        if cache_key not in self._cache:
            aroon_down, aroon_up = talib.AROON(self.high, self.low, timeperiod=period)
            
            strong_uptrend = (aroon_up > up_threshold) & (aroon_down < down_threshold)
            self._cache[cache_key] = strong_uptrend
        return self._cache[cache_key]

    def rebalancing_frontrun_bullish(self, threshold: float = 6.0) -> np.ndarray:
        """
        Rebalancing Front-Run Strategy (Sharpe: 1.95, CAGR: 31%)
        
        BUY when return since prior close >= threshold% at end of day.
        This captures the momentum from large intraday moves.
        
        Args:
            threshold: Return threshold (default 6% for 3x LETFs = 2% underlying)
        
        Returns:
            Boolean array - True when intraday return >= threshold%
        """
        cache_key = self._get_cache_key('rebalancing_frontrun_bullish', threshold=threshold)
        if cache_key not in self._cache:
            returns = self.intraday_return_from_prior_close()
            self._cache[cache_key] = returns >= (threshold / 100.0)
        return self._cache[cache_key]

    def trend_with_sma_filter(self, sma_period: int = 200, buffer_above: float = 5.0, 
                               buffer_below: float = 3.0) -> np.ndarray:
        """
        200 SMA Trend Filter with buffer zones.
        
        BUY when price > SMA + buffer_above%
        SELL/EXIT when price < SMA - buffer_below%
        
        This version returns BUY signal when in bullish zone.
        
        Args:
            sma_period: SMA period (default 200)
            buffer_above: % above SMA to trigger buy (default 5%)
            buffer_below: % below SMA to trigger exit (default 3%)
        
        Returns:
            Boolean array - True when price is sufficiently above SMA
        """
        cache_key = self._get_cache_key('trend_with_sma_filter', sma_period=sma_period,
                                        buffer_above=buffer_above, buffer_below=buffer_below)
        if cache_key not in self._cache:
            sma = talib.SMA(self.close, timeperiod=sma_period)
            
            # Buy when price is buffer_above% above SMA
            buy_level = sma * (1 + buffer_above / 100.0)
            buy_signal = self.close > buy_level
            
            self._cache[cache_key] = buy_signal
        return self._cache[cache_key]

    # ============================================================================
    # SMART MONEY CONCEPTS (SMC) INDICATORS
    # ============================================================================
    # 
    # FIX (Dec 2025): All SMC indicators rewritten to work WITHOUT look-ahead.
    # In live trading, we only have past and current candles - no future data.
    # Signals now fire at CONFIRMATION time (when we KNOW the condition is true),
    # not retroactively at the origin bar.
    # ============================================================================

    def swing_points(self, lookback: int = 5) -> Tuple[np.ndarray, np.ndarray]:
        """
        Detect swing highs and swing lows for SMC analysis.
        
        NO LOOK-AHEAD VERSION (Dec 2025 Fix):
        A swing is CONFIRMED when we have `lookback` bars on BOTH sides.
        Signal fires at bar `i` when bar `i - lookback` is confirmed as a swing.
        This means we only use data from bars 0 to i (no future data).
        
        Parameters:
            lookback: Number of bars on each side to confirm swing (default: 5)
            
        Returns:
            Tuple of (swing_highs, swing_lows) boolean arrays
            - swing_highs[i] = True means "as of bar i, bar i-lookback is a confirmed swing high"
            - swing_lows[i] = True means "as of bar i, bar i-lookback is a confirmed swing low"
        """
        lookback = int(lookback)
        cache_key = f'swing_points_{lookback}'
        if cache_key not in self._cache:
            high = self.high
            low = self.low
            n = len(high)
            swing_highs = np.zeros(n, dtype=bool)
            swing_lows = np.zeros(n, dtype=bool)
            
            # Start from 2*lookback because we need lookback bars on each side of the candidate
            for i in range(lookback * 2, n):
                # Candidate bar is at i - lookback (the middle of our window)
                candidate = i - lookback
                
                # Check if candidate is the highest/lowest in window [candidate-lookback : candidate+lookback+1]
                # Since we're at bar i, we have all data up to i, so candidate+lookback = i is available
                window_start = candidate - lookback
                window_end = candidate + lookback + 1  # = i + 1, which is valid (we're at bar i)
                
                # Swing high: highest point in lookback window on both sides
                if high[candidate] == max(high[window_start:window_end]):
                    swing_highs[i] = True  # Signal fires NOW (at bar i), confirming candidate bar
                    
                # Swing low: lowest point in lookback window on both sides
                if low[candidate] == min(low[window_start:window_end]):
                    swing_lows[i] = True  # Signal fires NOW (at bar i), confirming candidate bar
            
            self._cache[cache_key] = (swing_highs, swing_lows)
        return self._cache[cache_key]

    def order_block(self, lookback: int = 20, min_move_pct: float = 1.0) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Detect Smart Money Concept Order Blocks.
        Order blocks are the last opposing candle before a strong move.
        
        NO LOOK-AHEAD VERSION (Dec 2025 Fix):
        Signal fires at bar `i` when a strong move is DETECTED (current bar).
        Previously, signal fired at bar `j` (past bar) which required future knowledge.
        
        Now:
        - bullish_ob[i] = True when strong BULLISH move detected at bar i
          (indicates there was an order block zone in the recent lookback)
        - bearish_ob[i] = True when strong BEARISH move detected at bar i
          (indicates there was an order block zone in the recent lookback)
        
        Parameters:
            lookback: Bars to look back for order blocks (default: 20)
            min_move_pct: Minimum price move % to qualify as strong (default: 1.0)
            
        Returns:
            Tuple of (bullish_ob, bearish_ob, bullish_ob_high, bullish_ob_low)
            - bullish_ob: Boolean array, True when strong bullish move confirms OB
            - bearish_ob: Boolean array, True when strong bearish move confirms OB
            - bullish_ob_high: Price level of the OB zone high (for zone trading)
            - bullish_ob_low: Price level of the OB zone low (for zone trading)
        """
        lookback = int(lookback)
        cache_key = f'order_block_{lookback}_{min_move_pct}'
        if cache_key not in self._cache:
            open_price = self.open
            high = self.high
            low = self.low
            close = self.close
            n = len(close)
            
            bullish_ob = np.zeros(n, dtype=bool)
            bearish_ob = np.zeros(n, dtype=bool)
            bullish_ob_high = np.full(n, np.nan)
            bullish_ob_low = np.full(n, np.nan)
            bearish_ob_high = np.full(n, np.nan)
            bearish_ob_low = np.full(n, np.nan)
            
            for i in range(lookback, n):
                # Look for strong bullish move - signal fires NOW (at bar i)
                if close[i] > close[i-1] * (1 + min_move_pct/100):
                    # Find last bearish candle before the move (for zone info)
                    for j in range(i-1, max(0, i-lookback), -1):
                        if close[j] < open_price[j]:  # Bearish candle
                            bullish_ob[i] = True  # FIX: Signal at current bar i, not past bar j
                            bullish_ob_high[i] = high[j]
                            bullish_ob_low[i] = low[j]
                            break
                
                # Look for strong bearish move - signal fires NOW (at bar i)
                if close[i] < close[i-1] * (1 - min_move_pct/100):
                    # Find last bullish candle before the move (for zone info)
                    for j in range(i-1, max(0, i-lookback), -1):
                        if close[j] > open_price[j]:  # Bullish candle
                            bearish_ob[i] = True  # FIX: Signal at current bar i, not past bar j
                            bearish_ob_high[i] = high[j]
                            bearish_ob_low[i] = low[j]
                            break
            
            self._cache[cache_key] = (bullish_ob, bearish_ob, bullish_ob_high, bullish_ob_low)
        return self._cache[cache_key]

    def order_block_zone_revisit(self, lookback: int = 20, min_move_pct: float = 1.0, 
                                  zone_valid_bars: int = 50, min_wait_bars: int = 3) -> Tuple[np.ndarray, np.ndarray]:
        """
        PROPER SMC Order Block Strategy - Zone Revisit
        
        This implements how REAL SMC traders use order blocks:
        1. Detect a strong move → Mark the zone (opposing candle before the move)
        2. WAIT for price to move away from the zone
        3. Signal when price RETURNS to the zone
        
        NO LOOK-AHEAD BIAS - This is a valid trading strategy!
        
        For BULLISH zone revisit (entry signal):
        - Strong UP move detected → Mark the bearish candle as "demand zone"
        - Wait min_wait_bars for price to move away
        - Signal when price pulls BACK DOWN into the zone
        
        For BEARISH zone revisit (exit signal / short entry):
        - Strong DOWN move detected → Mark the bullish candle as "supply zone"
        - Wait min_wait_bars for price to move away
        - Signal when price rallies BACK UP into the zone
        
        Parameters:
            lookback: Bars to look back for the order block candle (default: 20)
            min_move_pct: Minimum price move % to qualify as strong (default: 1.0)
            zone_valid_bars: How many bars a zone remains valid (default: 50)
            min_wait_bars: Minimum bars to wait before zone can trigger (default: 3)
            
        Returns:
            Tuple of (bullish_zone_revisit, bearish_zone_revisit) boolean arrays
            - bullish_zone_revisit[i] = True when price revisits a demand zone (BUY signal)
            - bearish_zone_revisit[i] = True when price revisits a supply zone (SELL signal)
        """
        lookback = int(lookback)
        zone_valid_bars = int(zone_valid_bars)
        min_wait_bars = int(min_wait_bars)
        cache_key = f'ob_zone_revisit_{lookback}_{min_move_pct}_{zone_valid_bars}_{min_wait_bars}'
        
        if cache_key not in self._cache:
            open_price = self.open
            high = self.high
            low = self.low
            close = self.close
            n = len(close)
            
            bullish_zone_revisit = np.zeros(n, dtype=bool)
            bearish_zone_revisit = np.zeros(n, dtype=bool)
            
            # Track active zones: list of (zone_high, zone_low, detected_bar, zone_type)
            # zone_type: 'demand' (bullish) or 'supply' (bearish)
            active_demand_zones = []  # Zones for bullish entries (buy when price returns)
            active_supply_zones = []  # Zones for bearish exits (sell when price returns)
            
            for i in range(lookback, n):
                # =====================================================================
                # STEP 1: Detect new zones when strong moves happen
                # =====================================================================
                
                # Strong BULLISH move → Creates a DEMAND zone (bearish candle before)
                if close[i] > close[i-1] * (1 + min_move_pct/100):
                    # Find last bearish candle before the move
                    for j in range(i-1, max(0, i-lookback), -1):
                        if close[j] < open_price[j]:  # Bearish candle = demand zone
                            zone_high = high[j]
                            zone_low = low[j]
                            active_demand_zones.append({
                                'high': zone_high,
                                'low': zone_low,
                                'detected_bar': i,
                                'triggered': False
                            })
                            break
                
                # Strong BEARISH move → Creates a SUPPLY zone (bullish candle before)
                if close[i] < close[i-1] * (1 - min_move_pct/100):
                    # Find last bullish candle before the move
                    for j in range(i-1, max(0, i-lookback), -1):
                        if close[j] > open_price[j]:  # Bullish candle = supply zone
                            zone_high = high[j]
                            zone_low = low[j]
                            active_supply_zones.append({
                                'high': zone_high,
                                'low': zone_low,
                                'detected_bar': i,
                                'triggered': False
                            })
                            break
                
                # =====================================================================
                # STEP 2: Check if price revisits any active zones
                # =====================================================================
                
                # Check DEMAND zones (price pulling back = potential buy)
                new_demand_zones = []
                for zone in active_demand_zones:
                    bars_since = i - zone['detected_bar']
                    
                    # Skip if zone is too old
                    if bars_since > zone_valid_bars:
                        continue
                    
                    # Skip if not enough time has passed (avoid immediate re-trigger)
                    if bars_since < min_wait_bars:
                        new_demand_zones.append(zone)
                        continue
                    
                    # Check if price enters the zone (low touches or enters zone)
                    price_in_zone = low[i] <= zone['high'] and high[i] >= zone['low']
                    
                    if price_in_zone and not zone['triggered']:
                        bullish_zone_revisit[i] = True
                        zone['triggered'] = True  # Only trigger once per zone
                    
                    # Keep zone if not triggered
                    if not zone['triggered']:
                        new_demand_zones.append(zone)
                
                active_demand_zones = new_demand_zones
                
                # Check SUPPLY zones (price rallying back = potential sell/exit)
                new_supply_zones = []
                for zone in active_supply_zones:
                    bars_since = i - zone['detected_bar']
                    
                    # Skip if zone is too old
                    if bars_since > zone_valid_bars:
                        continue
                    
                    # Skip if not enough time has passed
                    if bars_since < min_wait_bars:
                        new_supply_zones.append(zone)
                        continue
                    
                    # Check if price enters the zone (high touches or enters zone)
                    price_in_zone = high[i] >= zone['low'] and low[i] <= zone['high']
                    
                    if price_in_zone and not zone['triggered']:
                        bearish_zone_revisit[i] = True
                        zone['triggered'] = True  # Only trigger once per zone
                    
                    # Keep zone if not triggered
                    if not zone['triggered']:
                        new_supply_zones.append(zone)
                
                active_supply_zones = new_supply_zones
            
            self._cache[cache_key] = (bullish_zone_revisit, bearish_zone_revisit)
        return self._cache[cache_key]

    def fair_value_gap(self, min_gap_pct: float = 0.1) -> Tuple[np.ndarray, np.ndarray]:
        """
        Detect Fair Value Gaps (FVG) / Imbalances.
        FVG occurs when there's a gap between candle 1's high and candle 3's low (bullish)
        or candle 1's low and candle 3's high (bearish).
        
        Parameters:
            min_gap_pct: Minimum gap size as % of price (default: 0.1)
            
        Returns:
            Tuple of (bullish_fvg, bearish_fvg) boolean arrays
        """
        cache_key = f'fair_value_gap_{min_gap_pct}'
        if cache_key not in self._cache:
            high = self.high
            low = self.low
            close = self.close
            n = len(close)
            
            bullish_fvg = np.zeros(n, dtype=bool)
            bearish_fvg = np.zeros(n, dtype=bool)
            
            for i in range(2, n):
                # Bullish FVG: Gap up - candle 3's low > candle 1's high
                gap_up = low[i] - high[i-2]
                if gap_up > close[i] * min_gap_pct / 100:
                    bullish_fvg[i] = True
                
                # Bearish FVG: Gap down - candle 3's high < candle 1's low
                gap_down = low[i-2] - high[i]
                if gap_down > close[i] * min_gap_pct / 100:
                    bearish_fvg[i] = True
            
            self._cache[cache_key] = (bullish_fvg, bearish_fvg)
        return self._cache[cache_key]

    def liquidity_sweep(self, swing_lookback: int = 10, confirmation_bars: int = 2) -> Tuple[np.ndarray, np.ndarray]:
        """
        Detect Liquidity Sweeps (stop hunts).
        A sweep occurs when price breaks a swing high/low then reverses.
        
        NO LOOK-AHEAD VERSION (Dec 2025 Fix):
        Uses fixed swing_points which confirms swings without future data.
        
        Parameters:
            swing_lookback: Bars to identify swing points (default: 10)
            confirmation_bars: Bars to confirm reversal (default: 2)
            
        Returns:
            Tuple of (bullish_sweep, bearish_sweep) boolean arrays
        """
        swing_lookback = int(swing_lookback)
        confirmation_bars = int(confirmation_bars)
        cache_key = f'liquidity_sweep_{swing_lookback}_{confirmation_bars}'
        if cache_key not in self._cache:
            high = self.high
            low = self.low
            close = self.close
            n = len(close)
            
            bullish_sweep = np.zeros(n, dtype=bool)
            bearish_sweep = np.zeros(n, dtype=bool)
            
            # Get swing points (FIXED: no look-ahead)
            # swing_lows[j] = True means bar (j - swing_lookback) is a confirmed swing
            swing_highs, swing_lows = self.swing_points(swing_lookback)
            
            # Track recent swing levels
            recent_swing_low = np.nan
            recent_swing_high = np.nan
            
            # Need more lookback now to account for swing offset
            for i in range(swing_lookback * 2 + confirmation_bars, n):
                # Update swing levels
                # FIX: swing_lows[j] confirms bar (j - swing_lookback), so get price from there
                for j in range(i - swing_lookback, i):
                    if swing_lows[j] and j >= swing_lookback:
                        recent_swing_low = low[j - swing_lookback]  # FIX: Price at actual swing bar
                    if swing_highs[j] and j >= swing_lookback:
                        recent_swing_high = high[j - swing_lookback]  # FIX: Price at actual swing bar
                
                # Bullish sweep: Price swept below swing low then closed back above
                if not np.isnan(recent_swing_low):
                    swept_low = any(low[i-k] < recent_swing_low for k in range(1, confirmation_bars + 1))
                    closed_above = close[i] > recent_swing_low
                    if swept_low and closed_above:
                        bullish_sweep[i] = True
                
                # Bearish sweep: Price swept above swing high then closed back below
                if not np.isnan(recent_swing_high):
                    swept_high = any(high[i-k] > recent_swing_high for k in range(1, confirmation_bars + 1))
                    closed_below = close[i] < recent_swing_high
                    if swept_high and closed_below:
                        bearish_sweep[i] = True
            
            self._cache[cache_key] = (bullish_sweep, bearish_sweep)
        return self._cache[cache_key]

    def break_of_structure(self, swing_lookback: int = 5) -> Tuple[np.ndarray, np.ndarray]:
        """
        Detect Break of Structure (BOS) - key SMC concept.
        Bullish BOS: Price breaks above a swing high
        Bearish BOS: Price breaks below a swing low
        
        NO LOOK-AHEAD VERSION (Dec 2025 Fix):
        Uses fixed swing_points which confirms swings without future data.
        
        Parameters:
            swing_lookback: Bars for swing detection (default: 5)
            
        Returns:
            Tuple of (bullish_bos, bearish_bos) boolean arrays
        """
        swing_lookback = int(swing_lookback)
        cache_key = f'break_of_structure_{swing_lookback}'
        if cache_key not in self._cache:
            high = self.high
            low = self.low
            close = self.close
            n = len(close)
            
            bullish_bos = np.zeros(n, dtype=bool)
            bearish_bos = np.zeros(n, dtype=bool)
            
            # Get swing points (FIXED: no look-ahead)
            # swing_lows[j] = True means bar (j - swing_lookback) is a confirmed swing
            swing_highs, swing_lows = self.swing_points(swing_lookback)
            
            # Track most recent confirmed swing levels
            last_swing_high = np.nan
            last_swing_low = np.nan
            
            # Need more lookback to account for swing confirmation offset
            for i in range(swing_lookback * 3, n):
                # Update swing levels from confirmed swings
                # FIX: swing_highs[j] confirms bar (j - swing_lookback), so get price from there
                for j in range(i - swing_lookback * 2, i - swing_lookback):
                    if swing_highs[j] and j >= swing_lookback:
                        last_swing_high = high[j - swing_lookback]  # FIX: Price at actual swing bar
                    if swing_lows[j] and j >= swing_lookback:
                        last_swing_low = low[j - swing_lookback]  # FIX: Price at actual swing bar
                
                # Bullish BOS: Close breaks above last swing high
                if not np.isnan(last_swing_high) and close[i] > last_swing_high and close[i-1] <= last_swing_high:
                    bullish_bos[i] = True
                    last_swing_high = np.nan  # Reset after break
                
                # Bearish BOS: Close breaks below last swing low
                if not np.isnan(last_swing_low) and close[i] < last_swing_low and close[i-1] >= last_swing_low:
                    bearish_bos[i] = True
                    last_swing_low = np.nan  # Reset after break
            
            self._cache[cache_key] = (bullish_bos, bearish_bos)
        return self._cache[cache_key]

    def ema_stack(self, periods: list = None) -> Tuple[np.ndarray, np.ndarray]:
        """
        Detect EMA stack alignment (e.g., 5>10>20>50 for bullish).
        
        Parameters:
            periods: List of EMA periods (default: [5, 10, 20, 50])
            
        Returns:
            Tuple of (bullish_stack, bearish_stack) boolean arrays
        """
        if periods is None:
            periods = [5, 10, 20, 50]
        periods = [int(p) for p in periods]
        cache_key = f'ema_stack_{"_".join(map(str, periods))}'
        if cache_key not in self._cache:
            n = len(self.close)
            
            # Calculate all EMAs
            emas = [self.ema(p) for p in periods]
            
            # Check alignment
            bullish_stack = np.ones(n, dtype=bool)
            bearish_stack = np.ones(n, dtype=bool)
            
            for i in range(len(periods) - 1):
                bullish_stack &= (emas[i] > emas[i+1])
                bearish_stack &= (emas[i] < emas[i+1])
            
            self._cache[cache_key] = (bullish_stack, bearish_stack)
        return self._cache[cache_key]

    def donchian_ha_breakout(self, period: int = 20) -> Tuple[np.ndarray, np.ndarray]:
        """
        Donchian Channel breakout using Heikin Ashi smoothing.
        Enhanced Turtle system with noise filtering.
        
        Parameters:
            period: Donchian channel period (default: 20)
            
        Returns:
            Tuple of (bullish_breakout, bearish_breakout) boolean arrays
        """
        period = int(period)
        cache_key = f'donchian_ha_breakout_{period}'
        if cache_key not in self._cache:
            # Get Heikin Ashi data
            ha_open, ha_high, ha_low, ha_close = self.heikin_ashi()
            
            # Calculate Donchian on HA data
            ha_high_series = pd.Series(ha_high)
            ha_low_series = pd.Series(ha_low)
            
            upper = ha_high_series.rolling(window=period).max().values
            lower = ha_low_series.rolling(window=period).min().values
            
            # Breakout signals
            prev_upper = np.roll(upper, 1)
            prev_lower = np.roll(lower, 1)
            
            bullish_breakout = (ha_close > prev_upper) & (np.roll(ha_close, 1) <= np.roll(prev_upper, 1))
            bearish_breakout = (ha_close < prev_lower) & (np.roll(ha_close, 1) >= np.roll(prev_lower, 1))
            
            bullish_breakout[:period+1] = False
            bearish_breakout[:period+1] = False
            
            self._cache[cache_key] = (bullish_breakout, bearish_breakout)
        return self._cache[cache_key]

    # ============================================================================
    # CONDITION FUNCTIONS
    # ============================================================================

    def get_conditions(self) -> Dict[str, Callable]:
        """
        Get dictionary of condition functions for strategy testing
        
        Returns:
            Dictionary mapping condition names to callable functions
        """
        
        def safe_get_value(arr, idx: int, default: float = np.nan) -> float:
            """Safely get value from array at index. Handles scalars and arrays."""
            # Handle scalar values (numpy.float64 or python float)
            if np.isscalar(arr):
                return default if np.isnan(arr) else float(arr)
            # Handle array values
            try:
                arr_len = len(arr)
                if idx < 0 or idx >= arr_len or np.isnan(arr[idx]):
                    return default
                return float(arr[idx])
            except (TypeError, IndexError):
                return default
        
        def safe_get_prev_value(arr, idx: int, default: float = np.nan) -> float:
            """Safely get previous value from array. Handles scalars and arrays."""
            # Handle scalar values
            if np.isscalar(arr):
                return default if np.isnan(arr) else float(arr)
            # Handle array values
            try:
                arr_len = len(arr)
                if idx <= 0 or idx >= arr_len or np.isnan(arr[idx-1]):
                    return default
                return float(arr[idx-1])
            except (TypeError, IndexError):
                return default
        
        conditions = {
            # RSI Conditions
            'rsi_oversold': lambda df, idx, period=14, threshold=30: (
                safe_get_value(self.rsi(period), idx) < threshold
            ),
            'rsi_overbought': lambda df, idx, period=14, threshold=70: (
                safe_get_value(self.rsi(period), idx) > threshold
            ),
            'rsi_extreme_overbought': lambda df, idx, period=14, threshold=80: (
                # Extreme overbought: RSI above very high threshold
                safe_get_value(self.rsi(int(period)), idx) > threshold
            ),
            'rsi_extreme_oversold': lambda df, idx, period=14, threshold=20: (
                # Extreme oversold: RSI below very low threshold
                safe_get_value(self.rsi(int(period)), idx) < threshold
            ),
            'rsi_bullish_cross_50': lambda df, idx, period=14, cross_level=50: (
                safe_get_value(self.rsi(period), idx) > cross_level and 
                safe_get_prev_value(self.rsi(period), idx) <= cross_level
            ),
            'rsi_bearish_cross_50': lambda df, idx, period=14, cross_level=50: (
                safe_get_value(self.rsi(period), idx) < cross_level and 
                safe_get_prev_value(self.rsi(period), idx) >= cross_level
            ),
            
            # ConnorsRSI Conditions
            'connors_rsi_oversold': lambda df, idx, rsi_period=3, streak_period=2, rank_period=100, threshold=25: (
                safe_get_value(self.connors_rsi(rsi_period, streak_period, rank_period), idx) < threshold
            ),
            'connors_rsi_very_oversold': lambda df, idx, rsi_period=3, streak_period=2, rank_period=100, threshold=20: (
                safe_get_value(self.connors_rsi(rsi_period, streak_period, rank_period), idx) < threshold
            ),
            'connors_rsi_overbought': lambda df, idx, rsi_period=3, streak_period=2, rank_period=100, threshold=75: (
                safe_get_value(self.connors_rsi(rsi_period, streak_period, rank_period), idx) > threshold
            ),
            'connors_rsi_bullish_cross': lambda df, idx, rsi_period=3, streak_period=2, rank_period=100, cross_level=50: (
                safe_get_value(self.connors_rsi(rsi_period, streak_period, rank_period), idx) > cross_level and 
                safe_get_prev_value(self.connors_rsi(rsi_period, streak_period, rank_period), idx) <= cross_level
            ),
            
            # MACD Conditions
            'macd_positive': lambda df, idx, fast_period=12, slow_period=26, signal_period=9, threshold=0: (
                safe_get_value(self.macd(fast_period, slow_period, signal_period)[0], idx) > threshold
            ),
            'macd_negative': lambda df, idx, fast_period=12, slow_period=26, signal_period=9, threshold=0: (
                safe_get_value(self.macd(fast_period, slow_period, signal_period)[0], idx) < threshold
            ),
            'macd_bullish_cross': lambda df, idx, fast_period=12, slow_period=26, signal_period=9: (
                safe_get_value(self.macd(fast_period, slow_period, signal_period)[0], idx) > 
                safe_get_value(self.macd(fast_period, slow_period, signal_period)[1], idx) and
                safe_get_prev_value(self.macd(fast_period, slow_period, signal_period)[0], idx) <= 
                safe_get_prev_value(self.macd(fast_period, slow_period, signal_period)[1], idx)
            ),
            'macd_bearish_cross': lambda df, idx, fast_period=12, slow_period=26, signal_period=9: (
                safe_get_value(self.macd(fast_period, slow_period, signal_period)[0], idx) < 
                safe_get_value(self.macd(fast_period, slow_period, signal_period)[1], idx) and
                safe_get_prev_value(self.macd(fast_period, slow_period, signal_period)[0], idx) >= 
                safe_get_prev_value(self.macd(fast_period, slow_period, signal_period)[1], idx)
            ),
            'macd_histogram_positive': lambda df, idx, fast_period=12, slow_period=26, signal_period=9, threshold=0: (
                safe_get_value(self.macd(fast_period, slow_period, signal_period)[2], idx) > threshold
            ),
            'macd_histogram_negative': lambda df, idx, fast_period=12, slow_period=26, signal_period=9, threshold=0: (
                safe_get_value(self.macd(fast_period, slow_period, signal_period)[2], idx) < threshold
            ),
            'macd_histogram_increasing': lambda df, idx, fast_period=12, slow_period=26, signal_period=9: (
                safe_get_value(self.macd(fast_period, slow_period, signal_period)[2], idx) > 
                safe_get_prev_value(self.macd(fast_period, slow_period, signal_period)[2], idx)
            ),
            
            # Stochastic Conditions
            'stoch_oversold': lambda df, idx, fastk_period=5, slowk_period=3, slowd_period=3, threshold=20: (
                safe_get_value(self.stochastic(fastk_period, slowk_period, slowd_period)[0], idx) < threshold and
                safe_get_value(self.stochastic(fastk_period, slowk_period, slowd_period)[1], idx) < threshold
            ),
            'stoch_overbought': lambda df, idx, fastk_period=5, slowk_period=3, slowd_period=3, threshold=80: (
                safe_get_value(self.stochastic(fastk_period, slowk_period, slowd_period)[0], idx) > threshold and
                safe_get_value(self.stochastic(fastk_period, slowk_period, slowd_period)[1], idx) > threshold
            ),
            'stoch_rsi_oversold': lambda df, idx, period=14, fastk_period=5, fastd_period=3, threshold=0.2: (
                safe_get_value(self.stochastic_rsi(period, fastk_period, fastd_period)[0], idx) < threshold
            ),
            'stoch_rsi_overbought': lambda df, idx, period=14, fastk_period=5, fastd_period=3, threshold=0.8: (
                safe_get_value(self.stochastic_rsi(period, fastk_period, fastd_period)[0], idx) > threshold
            ),
            'stoch_bullish_cross': lambda df, idx, fastk_period=5, slowk_period=3, slowd_period=3: (
                safe_get_value(self.stochastic(fastk_period, slowk_period, slowd_period)[0], idx) >
                safe_get_value(self.stochastic(fastk_period, slowk_period, slowd_period)[1], idx) and
                safe_get_prev_value(self.stochastic(fastk_period, slowk_period, slowd_period)[0], idx) <=
                safe_get_prev_value(self.stochastic(fastk_period, slowk_period, slowd_period)[1], idx)
            ),
            'stoch_bearish_cross': lambda df, idx, fastk_period=5, slowk_period=3, slowd_period=3: (
                safe_get_value(self.stochastic(fastk_period, slowk_period, slowd_period)[0], idx) <
                safe_get_value(self.stochastic(fastk_period, slowk_period, slowd_period)[1], idx) and
                safe_get_prev_value(self.stochastic(fastk_period, slowk_period, slowd_period)[0], idx) >=
                safe_get_prev_value(self.stochastic(fastk_period, slowk_period, slowd_period)[1], idx)
            ),
            
            # Bollinger Bands Conditions
            'bb_oversold': lambda df, idx, period=20, std_dev=2.0, threshold=0.2: (
                safe_get_value(self.bb_position(period, std_dev), idx) < threshold
            ),
            'bb_very_oversold': lambda df, idx, period=20, std_dev=2.0, threshold=0.1: (
                safe_get_value(self.bb_position(period, std_dev), idx) < threshold
            ),
            'bb_overbought': lambda df, idx, period=20, std_dev=2.0, threshold=0.8: (
                safe_get_value(self.bb_position(period, std_dev), idx) > threshold
            ),
            'bb_very_overbought': lambda df, idx, period=20, std_dev=2.0, threshold=0.9: (
                safe_get_value(self.bb_position(period, std_dev), idx) > threshold
            ),
            'bb_squeeze': lambda df, idx, period=20, std_dev=2.0, threshold=0.02: (
                safe_get_value(self.bb_width(period, std_dev), idx) < threshold
            ),
            'bb_expansion': lambda df, idx, period=20, std_dev=2.0, threshold=0.05: (
                safe_get_value(self.bb_width(period, std_dev), idx) > threshold
            ),
            'bb_upper_break': lambda df, idx, period=20, std_dev=2.0: (
                safe_get_value(self.close, idx) > safe_get_value(self.bollinger_bands(period, std_dev)[0], idx)
            ),
            'bb_lower_break': lambda df, idx, period=20, std_dev=2.0: (
                safe_get_value(self.close, idx) < safe_get_value(self.bollinger_bands(period, std_dev)[2], idx)
            ),
            
            # Williams %R Conditions
            'willr_oversold': lambda df, idx, period=14, threshold=-80: (
                safe_get_value(self.williams_r(period), idx) < threshold
            ),
            'willr_very_oversold': lambda df, idx, period=14, threshold=-90: (
                safe_get_value(self.williams_r(period), idx) < threshold
            ),
            'willr_overbought': lambda df, idx, period=14, threshold=-20: (
                safe_get_value(self.williams_r(period), idx) > threshold
            ),
            'willr_very_overbought': lambda df, idx, period=14, threshold=-10: (
                safe_get_value(self.williams_r(period), idx) > threshold
            ),
            
            # CCI Conditions
            'cci_oversold': lambda df, idx, period=14, threshold=-100: (
                safe_get_value(self.cci(period), idx) < threshold
            ),
            'cci_very_oversold': lambda df, idx, period=14, threshold=-200: (
                safe_get_value(self.cci(period), idx) < threshold
            ),
            'cci_overbought': lambda df, idx, period=14, threshold=100: (
                safe_get_value(self.cci(period), idx) > threshold
            ),
            'cci_very_overbought': lambda df, idx, period=14, threshold=200: (
                safe_get_value(self.cci(period), idx) > threshold
            ),
            'cci_bullish_cross_zero': lambda df, idx, period=14, cross_level=0: (
                safe_get_value(self.cci(period), idx) > cross_level and 
                safe_get_prev_value(self.cci(period), idx) <= cross_level
            ),
            'cci_bearish_cross_zero': lambda df, idx, period=14, cross_level=0: (
                safe_get_value(self.cci(period), idx) < cross_level and 
                safe_get_prev_value(self.cci(period), idx) >= cross_level
            ),
            
            # ATR Conditions
            'atr_high_volatility': lambda df, idx, period=14, percentile_period=50, percentile_threshold=0.8: (
                idx >= percentile_period and
                safe_get_value(self.atr_percent(period), idx) > 
                np.nanpercentile(self.atr_percent(period)[max(0, idx-percentile_period):idx+1], percentile_threshold*100)
            ),
            'atr_low_volatility': lambda df, idx, period=14, percentile_period=50, percentile_threshold=0.2: (
                idx >= percentile_period and
                safe_get_value(self.atr_percent(period), idx) < 
                np.nanpercentile(self.atr_percent(period)[max(0, idx-percentile_period):idx+1], percentile_threshold*100)
            ),
            'atr_above_threshold': lambda df, idx, period=14, threshold=2.0: (
                safe_get_value(self.atr_percent(period), idx) > threshold
            ),
            'atr_below_threshold': lambda df, idx, period=14, threshold=1.0: (
                safe_get_value(self.atr_percent(period), idx) < threshold
            ),
            
            # SAR Conditions
            'sar_bullish': lambda df, idx, acceleration=0.02, maximum=0.2: (
                safe_get_value(self.close, idx) > safe_get_value(self.parabolic_sar(acceleration, maximum), idx)
            ),
            'sar_bearish': lambda df, idx, acceleration=0.02, maximum=0.2: (
                safe_get_value(self.close, idx) < safe_get_value(self.parabolic_sar(acceleration, maximum), idx)
            ),
            'sar_bullish_cross': lambda df, idx, acceleration=0.02, maximum=0.2: (
                safe_get_value(self.close, idx) > safe_get_value(self.parabolic_sar(acceleration, maximum), idx) and
                safe_get_prev_value(self.close, idx) <= safe_get_prev_value(self.parabolic_sar(acceleration, maximum), idx)
            ),
            'sar_bearish_cross': lambda df, idx, acceleration=0.02, maximum=0.2: (
                safe_get_value(self.close, idx) < safe_get_value(self.parabolic_sar(acceleration, maximum), idx) and
                safe_get_prev_value(self.close, idx) >= safe_get_prev_value(self.parabolic_sar(acceleration, maximum), idx)
            ),
            
            # Moving Average Conditions
            'price_above_sma': lambda df, idx, period=20: (
                safe_get_value(self.close, idx) > safe_get_value(self.sma(period), idx)
            ),
            'price_below_sma': lambda df, idx, period=20: (
                safe_get_value(self.close, idx) < safe_get_value(self.sma(period), idx)
            ),
            'price_above_ema': lambda df, idx, period=20: (
                safe_get_value(self.close, idx) > safe_get_value(self.ema(period), idx)
            ),
            'price_below_ema': lambda df, idx, period=20: (
                safe_get_value(self.close, idx) < safe_get_value(self.ema(period), idx)
            ),
            'sma_bullish_cross': lambda df, idx, fast_period=10, slow_period=20: (
                safe_get_value(self.sma(fast_period), idx) > safe_get_value(self.sma(slow_period), idx) and
                safe_get_prev_value(self.sma(fast_period), idx) <= safe_get_prev_value(self.sma(slow_period), idx)
            ),
            'sma_bearish_cross': lambda df, idx, fast_period=10, slow_period=20: (
                safe_get_value(self.sma(fast_period), idx) < safe_get_value(self.sma(slow_period), idx) and
                safe_get_prev_value(self.sma(fast_period), idx) >= safe_get_prev_value(self.sma(slow_period), idx)
            ),
            'ema_bullish_cross': lambda df, idx, fast_period=12, slow_period=26: (
                safe_get_value(self.ema(fast_period), idx) > safe_get_value(self.ema(slow_period), idx) and
                safe_get_prev_value(self.ema(fast_period), idx) <= safe_get_prev_value(self.ema(slow_period), idx)
            ),
            'ema_bearish_cross': lambda df, idx, fast_period=12, slow_period=26: (
                safe_get_value(self.ema(fast_period), idx) < safe_get_value(self.ema(slow_period), idx) and
                safe_get_prev_value(self.ema(fast_period), idx) >= safe_get_prev_value(self.ema(slow_period), idx)
            ),
            'ema12_above_ema26': lambda df, idx, fast_period=12, slow_period=26: (
                safe_get_value(self.ema(fast_period), idx) > safe_get_value(self.ema(slow_period), idx)
            ),
            'ema12_below_ema26': lambda df, idx, fast_period=12, slow_period=26: (
                safe_get_value(self.ema(fast_period), idx) < safe_get_value(self.ema(slow_period), idx)
            ),
            'sma10_above_sma20': lambda df, idx, fast_period=10, slow_period=20: (
                safe_get_value(self.sma(fast_period), idx) > safe_get_value(self.sma(slow_period), idx)
            ),
            'sma10_below_sma20': lambda df, idx, fast_period=10, slow_period=20: (
                safe_get_value(self.sma(fast_period), idx) < safe_get_value(self.sma(slow_period), idx)
            ),
            
            # Volume Conditions
            'volume_spike': lambda df, idx, period=20, threshold=2.0: (
                safe_get_value(self.volume_ratio(period), idx) > threshold
            ),
            'volume_high': lambda df, idx, period=20, threshold=1.5: (
                safe_get_value(self.volume_ratio(period), idx) > threshold
            ),
            'volume_low': lambda df, idx, period=20, threshold=0.5: (
                safe_get_value(self.volume_ratio(period), idx) < threshold
            ),
            'volume_above_sma': lambda df, idx, period=20, threshold=1.0: (
                safe_get_value(self.volume_ratio(period), idx) > threshold
            ),
            'volume_below_sma': lambda df, idx, period=20, threshold=1.0: (
                safe_get_value(self.volume_ratio(period), idx) < threshold
            ),
            
            # VWAP Conditions
            'price_above_vwap': lambda df, idx, threshold=0.01: (
                safe_get_value(self.vwap_deviation(), idx) > threshold
            ),
            'price_below_vwap': lambda df, idx, threshold=-0.01: (
                safe_get_value(self.vwap_deviation(), idx) < threshold
            ),
            'price_near_vwap': lambda df, idx, threshold=0.005: (
                abs(safe_get_value(self.vwap_deviation(), idx)) < threshold
            ),
            'vwap_bullish_cross': lambda df, idx, confirmation_bars=1: (
                # Cross above VWAP with confirmation bars staying above
                idx >= int(confirmation_bars) and
                safe_get_value(self.close, idx) > safe_get_value(self.vwap(), idx) and
                safe_get_prev_value(self.close, idx) <= safe_get_prev_value(self.vwap(), idx) and
                # All confirmation bars must be above VWAP
                all(safe_get_value(self.close, idx - i) > safe_get_value(self.vwap(), idx - i) 
                    for i in range(int(confirmation_bars)))
            ),
            'vwap_bearish_cross': lambda df, idx, confirmation_bars=1: (
                # Cross below VWAP with confirmation bars staying below
                idx >= int(confirmation_bars) and
                safe_get_value(self.close, idx) < safe_get_value(self.vwap(), idx) and
                safe_get_prev_value(self.close, idx) >= safe_get_prev_value(self.vwap(), idx) and
                # All confirmation bars must be below VWAP
                all(safe_get_value(self.close, idx - i) < safe_get_value(self.vwap(), idx - i) 
                    for i in range(int(confirmation_bars)))
            ),
            
            # Momentum Conditions
            'momentum_positive': lambda df, idx, period=10, threshold=0: (
                safe_get_value(self.momentum(period), idx) > threshold
            ),
            'momentum_negative': lambda df, idx, period=10, threshold=0: (
                safe_get_value(self.momentum(period), idx) < threshold
            ),
            'momentum_increasing': lambda df, idx, period=10: (
                safe_get_value(self.momentum(period), idx) > safe_get_prev_value(self.momentum(period), idx)
            ),
            'momentum_decreasing': lambda df, idx, period=10: (
                safe_get_value(self.momentum(period), idx) < safe_get_prev_value(self.momentum(period), idx)
            ),
            'roc_positive': lambda df, idx, period=10, threshold=0: (
                safe_get_value(self.roc(period), idx) > threshold
            ),
            'roc_negative': lambda df, idx, period=10, threshold=0: (
                safe_get_value(self.roc(period), idx) < threshold
            ),
            'roc_above_threshold': lambda df, idx, period=10, threshold=1.0: (
                safe_get_value(self.roc(period), idx) > threshold
            ),
            
            # Daily/Intraday Return Conditions
            'daily_spike_down': lambda df, idx, threshold=2.0: (
                # Daily return is below -threshold%
                idx >= 1 and
                ((safe_get_value(self.close, idx) - safe_get_value(self.close, idx-1)) / 
                 safe_get_value(self.close, idx-1) * 100) < -threshold
            ),
            'daily_spike_up': lambda df, idx, threshold=8.5: (
                # Daily return is above threshold%
                idx >= 1 and
                ((safe_get_value(self.close, idx) - safe_get_value(self.close, idx-1)) / 
                 safe_get_value(self.close, idx-1) * 100) > threshold
            ),
            'intraday_return_bearish': lambda df, idx, threshold=-6.0: (
                # Intraday return (close - open) / open below threshold
                ((safe_get_value(self.close, idx) - safe_get_value(self.open, idx)) / 
                 safe_get_value(self.open, idx) * 100) < threshold
            ),
            'intraday_return_bullish': lambda df, idx, threshold=6.0: (
                # Intraday return (close - open) / open above threshold
                ((safe_get_value(self.close, idx) - safe_get_value(self.open, idx)) / 
                 safe_get_value(self.open, idx) * 100) > threshold
            ),
            'multi_day_momentum_bullish': lambda df, idx, days=5, threshold=5.0: (
                # N-day cumulative return above threshold
                idx >= int(days) and
                ((safe_get_value(self.close, idx) - safe_get_value(self.close, idx - int(days))) / 
                 safe_get_value(self.close, idx - int(days)) * 100) > threshold
            ),
            'rebalancing_frontrun_bullish': lambda df, idx, threshold=6.0: (
                # Large intraday move suggesting ETF rebalancing opportunity
                ((safe_get_value(self.close, idx) - safe_get_value(self.open, idx)) / 
                 safe_get_value(self.open, idx) * 100) > threshold
            ),
            'roc_below_threshold': lambda df, idx, period=10, threshold=-1.0: (
                safe_get_value(self.roc(period), idx) < threshold
            ),
            
            # ADX Conditions
            'adx_trending': lambda df, idx, period=14, threshold=25: (
                safe_get_value(self.adx(period), idx) > threshold
            ),
            'adx_strong_trend': lambda df, idx, period=14, threshold=40: (
                safe_get_value(self.adx(period), idx) > threshold
            ),
            'adx_weak_trend': lambda df, idx, period=14, threshold=20: (
                safe_get_value(self.adx(period), idx) < threshold
            ),
            'adx_increasing': lambda df, idx, period=14: (
                safe_get_value(self.adx(period), idx) > safe_get_prev_value(self.adx(period), idx)
            ),
            'adx_decreasing': lambda df, idx, period=14: (
                safe_get_value(self.adx(period), idx) < safe_get_prev_value(self.adx(period), idx)
            ),
            
            # Aroon Conditions
            'aroon_bullish': lambda df, idx, period=14: (
                safe_get_value(self.aroon(period)[0], idx) > safe_get_value(self.aroon(period)[1], idx)
            ),
            'aroon_bearish': lambda df, idx, period=14: (
                safe_get_value(self.aroon(period)[0], idx) < safe_get_value(self.aroon(period)[1], idx)
            ),
            'aroon_up_strong': lambda df, idx, period=14, threshold=70: (
                safe_get_value(self.aroon(period)[0], idx) > threshold
            ),
            'aroon_down_strong': lambda df, idx, period=14, threshold=70: (
                safe_get_value(self.aroon(period)[1], idx) > threshold
            ),
            'aroon_consolidation': lambda df, idx, period=14, threshold=50: (
                safe_get_value(self.aroon(period)[0], idx) < threshold and
                safe_get_value(self.aroon(period)[1], idx) < threshold
            ),
            
            # Aroon Strong Uptrend - Aroon Up above threshold AND Aroon Down below threshold
            'aroon_strong_uptrend': lambda df, idx, period=25, up_threshold=70, down_threshold=30: (
                idx >= int(period) and
                safe_get_value(self.aroon(int(period))[0], idx) > up_threshold and
                safe_get_value(self.aroon(int(period))[1], idx) < down_threshold
            ),
            
            # Aroon Oscillator Zero-Lag variants (using smoothed Aroon oscillator)
            'aroon_oscillator_zerolag_bullish': lambda df, idx, period=29, smooth=25, gain_limit=10, mode='bullish': (
                idx >= max(int(period), int(smooth)) and
                # Aroon oscillator = Aroon Up - Aroon Down, smoothed with EMA for zero-lag
                safe_get_value(self.aroon(int(period))[0], idx) - safe_get_value(self.aroon(int(period))[1], idx) > gain_limit
            ),
            'aroon_oscillator_zerolag_reversion': lambda df, idx, period=29, smooth=25, signal_len=10, gain_limit=10, mode='reversion': (
                idx >= max(int(period), int(smooth), int(signal_len)) and
                # Mean reversion: Aroon oscillator crossing back toward zero from extreme
                safe_get_value(self.aroon(int(period))[0], idx) - safe_get_value(self.aroon(int(period))[1], idx) < gain_limit and
                safe_get_prev_value(self.aroon(int(period))[0], idx) - safe_get_prev_value(self.aroon(int(period))[1], idx) > gain_limit
            ),
            'aroon_oscillator_zerolag_strong': lambda df, idx, period=29, smooth=25, threshold=50, gain_limit=10, mode='strong': (
                idx >= max(int(period), int(smooth)) and
                # Strong trend: Aroon oscillator above threshold
                safe_get_value(self.aroon(int(period))[0], idx) - safe_get_value(self.aroon(int(period))[1], idx) > threshold
            ),
            'aroon_oscillator_zerolag_trend': lambda df, idx, period=29, smooth=25, gain_limit=10, mode='trend': (
                idx >= max(int(period), int(smooth)) and
                # Trend following: Aroon oscillator positive and increasing
                safe_get_value(self.aroon(int(period))[0], idx) - safe_get_value(self.aroon(int(period))[1], idx) > 0 and
                (safe_get_value(self.aroon(int(period))[0], idx) - safe_get_value(self.aroon(int(period))[1], idx)) >
                (safe_get_prev_value(self.aroon(int(period))[0], idx) - safe_get_prev_value(self.aroon(int(period))[1], idx))
            ),
            
            # OBV Conditions
            'obv_increasing': lambda df, idx, lookback_period=20, obv_sma_period=20: (
                # OBV is above its SMA (trending up) AND OBV increasing over lookback
                idx >= max(int(lookback_period), int(obv_sma_period)) and
                safe_get_value(self.obv(), idx) > safe_get_value(talib.SMA(self.obv(), timeperiod=int(obv_sma_period)), idx) and
                safe_get_value(self.obv(), idx) > safe_get_value(self.obv(), idx - int(lookback_period))
            ),
            'obv_decreasing': lambda df, idx, lookback_period=20, obv_sma_period=20: (
                # OBV is below its SMA (trending down) AND OBV decreasing over lookback
                idx >= max(int(lookback_period), int(obv_sma_period)) and
                safe_get_value(self.obv(), idx) < safe_get_value(talib.SMA(self.obv(), timeperiod=int(obv_sma_period)), idx) and
                safe_get_value(self.obv(), idx) < safe_get_value(self.obv(), idx - int(lookback_period))
            ),
            'obv_divergence_bullish': lambda df, idx, lookback=5: (
                idx >= lookback and
                safe_get_value(self.close, idx) < safe_get_value(self.close, idx-lookback) and
                safe_get_value(self.obv(), idx) > safe_get_value(self.obv(), idx-lookback)
            ),
            'obv_divergence_bearish': lambda df, idx, lookback=5: (
                idx >= lookback and
                safe_get_value(self.close, idx) > safe_get_value(self.close, idx-lookback) and
                safe_get_value(self.obv(), idx) < safe_get_value(self.obv(), idx-lookback)
            ),
            
            # Supertrend Conditions
            'supertrend_bullish': lambda df, idx, period=10, multiplier=3.0: (
                safe_get_value(self.supertrend(period, multiplier)[1], idx) == 1
            ),
            'supertrend_bearish': lambda df, idx, period=10, multiplier=3.0: (
                safe_get_value(self.supertrend(period, multiplier)[1], idx) == -1
            ),
            'supertrend_bullish_cross': lambda df, idx, period=10, multiplier=3.0: (
                safe_get_value(self.supertrend(period, multiplier)[1], idx) == 1 and
                safe_get_prev_value(self.supertrend(period, multiplier)[1], idx) == -1
            ),
            'supertrend_bearish_cross': lambda df, idx, period=10, multiplier=3.0: (
                safe_get_value(self.supertrend(period, multiplier)[1], idx) == -1 and
                safe_get_prev_value(self.supertrend(period, multiplier)[1], idx) == 1
            ),
            
            # Keltner Channel Conditions
            'keltner_upper_break': lambda df, idx, period=20, multiplier=2.0: (
                safe_get_value(self.close, idx) > safe_get_value(self.keltner_channels(period, multiplier)[1], idx)
            ),
            'keltner_lower_break': lambda df, idx, period=20, multiplier=2.0: (
                safe_get_value(self.close, idx) < safe_get_value(self.keltner_channels(period, multiplier)[2], idx)
            ),
            'keltner_squeeze': lambda df, idx, period=20, multiplier=2.0: (
                safe_get_value(self.keltner_channels(period, multiplier)[1], idx) -
                safe_get_value(self.keltner_channels(period, multiplier)[2], idx) <
                safe_get_value(self.atr(period), idx) * 1.5  # Narrow channel
            ),
            
            # TTM Squeeze Conditions - returns tuple (squeeze_on, squeeze_off, momentum)
            'ttm_squeeze_on': lambda df, idx, bb_period=20, bb_std=2.0, kc_period=20, kc_mult=1.5: (
                safe_get_value(self.ttm_squeeze(bb_period, bb_std, kc_period, kc_mult)[0], idx) == True
            ),
            'ttm_squeeze_off': lambda df, idx, bb_period=20, bb_std=2.0, kc_period=20, kc_mult=1.5: (
                safe_get_value(self.ttm_squeeze(bb_period, bb_std, kc_period, kc_mult)[1], idx) == True and
                safe_get_value(self.ttm_squeeze(bb_period, bb_std, kc_period, kc_mult)[0], idx - 1) == True
            ),
            
            # Fisher Transform Conditions
            'fisher_bullish_cross': lambda df, idx, period=10: (
                safe_get_value(self.fisher_transform(period)[0], idx) > 
                safe_get_value(self.fisher_transform(period)[1], idx) and
                safe_get_prev_value(self.fisher_transform(period)[0], idx) <=
                safe_get_prev_value(self.fisher_transform(period)[1], idx)
            ),
            'fisher_bearish_cross': lambda df, idx, period=10: (
                safe_get_value(self.fisher_transform(period)[0], idx) < 
                safe_get_value(self.fisher_transform(period)[1], idx) and
                safe_get_prev_value(self.fisher_transform(period)[0], idx) >=
                safe_get_prev_value(self.fisher_transform(period)[1], idx)
            ),
            # Fisher Transform buy/sell signals - crossing threshold
            'fisher_buy_signal': lambda df, idx, period=10, threshold=-1.0: (
                safe_get_value(self.fisher_transform(period)[0], idx) > threshold and
                safe_get_prev_value(self.fisher_transform(period)[0], idx) <= threshold
            ),
            'fisher_sell_signal': lambda df, idx, period=10, threshold=1.0: (
                safe_get_value(self.fisher_transform(period)[0], idx) < threshold and
                safe_get_prev_value(self.fisher_transform(period)[0], idx) >= threshold
            ),
            'fisher_extreme_oversold': lambda df, idx, period=10, threshold=-2.0: (
                safe_get_value(self.fisher_transform(period)[0], idx) < threshold
            ),
            'fisher_extreme_overbought': lambda df, idx, period=10, threshold=2.0: (
                safe_get_value(self.fisher_transform(period)[0], idx) > threshold
            ),
            
            # Chaikin Money Flow Conditions
            'cmf_positive': lambda df, idx, period=20, threshold=0: (
                safe_get_value(self.chaikin_money_flow(period), idx) > threshold
            ),
            'cmf_negative': lambda df, idx, period=20, threshold=0: (
                safe_get_value(self.chaikin_money_flow(period), idx) < threshold
            ),
            'cmf_bullish_divergence': lambda df, idx, period=20, lookback=5: (
                idx >= lookback and
                safe_get_value(self.close, idx) < safe_get_value(self.close, idx-lookback) and
                safe_get_value(self.chaikin_money_flow(period), idx) > 
                safe_get_value(self.chaikin_money_flow(period), idx-lookback)
            ),
            
            # Donchian Channel Conditions
            'donchian_upper_break': lambda df, idx, period=20: (
                safe_get_value(self.close, idx) >= safe_get_value(self.donchian_channels(period)[1], idx)
            ),
            'donchian_lower_break': lambda df, idx, period=20: (
                safe_get_value(self.close, idx) <= safe_get_value(self.donchian_channels(period)[2], idx)
            ),
            'donchian_middle_cross_up': lambda df, idx, period=20: (
                safe_get_value(self.close, idx) > safe_get_value(self.donchian_channels(period)[0], idx) and
                safe_get_value(self.close, idx-1) <= safe_get_value(self.donchian_channels(period)[0], idx-1)
            ),
            
            # TRIX Conditions
            'trix_positive': lambda df, idx, period=14: (
                safe_get_value(self.trix(period), idx) > 0
            ),
            'trix_negative': lambda df, idx, period=14: (
                safe_get_value(self.trix(period), idx) < 0
            ),
            'trix_bullish_cross': lambda df, idx, period=14: (
                safe_get_value(self.trix(period), idx) > 0 and
                safe_get_prev_value(self.trix(period), idx) <= 0
            ),
            'trix_bearish_cross': lambda df, idx, period=14: (
                safe_get_value(self.trix(period), idx) < 0 and
                safe_get_prev_value(self.trix(period), idx) >= 0
            ),
            
            # True Strength Index Conditions
            'tsi_positive': lambda df, idx, fast_period=13, slow_period=25: (
                safe_get_value(self.true_strength_index(fast_period, slow_period), idx) > 0
            ),
            'tsi_negative': lambda df, idx, fast_period=13, slow_period=25: (
                safe_get_value(self.true_strength_index(fast_period, slow_period), idx) < 0
            ),
            'tsi_overbought': lambda df, idx, fast_period=13, slow_period=25, threshold=25: (
                safe_get_value(self.true_strength_index(fast_period, slow_period), idx) > threshold
            ),
            'tsi_oversold': lambda df, idx, fast_period=13, slow_period=25, threshold=-25: (
                safe_get_value(self.true_strength_index(fast_period, slow_period), idx) < threshold
            ),
            
            # ============================================================================
            # CRASH PROTECTION & RECOVERY CONDITIONS
            # ============================================================================
            
            # Ulcer Index Conditions
            'ulcer_low_risk': lambda df, idx, period=14, threshold=5: (
                safe_get_value(self.ulcer_index(period), idx) < threshold
            ),
            'ulcer_high_risk': lambda df, idx, period=14, threshold=10: (
                safe_get_value(self.ulcer_index(period), idx) > threshold
            ),
            'ulcer_extreme_risk': lambda df, idx, period=14, threshold=15: (
                safe_get_value(self.ulcer_index(period), idx) > threshold
            ),
            'ulcer_decreasing': lambda df, idx, period=14: (
                safe_get_value(self.ulcer_index(period), idx) < 
                safe_get_prev_value(self.ulcer_index(period), idx)
            ),
            
            # Maximum Drawdown Conditions
            'drawdown_shallow': lambda df, idx, period=20, threshold=-5: (
                safe_get_value(self.maximum_drawdown(period)[0], idx) > threshold
            ),
            'drawdown_moderate': lambda df, idx, period=20, threshold=-10: (
                safe_get_value(self.maximum_drawdown(period)[0], idx) < threshold
            ),
            'drawdown_deep': lambda df, idx, period=20, threshold=-20: (
                safe_get_value(self.maximum_drawdown(period)[0], idx) < threshold
            ),
            'drawdown_recovering': lambda df, idx, period=20: (
                safe_get_value(self.maximum_drawdown(period)[0], idx) > 
                safe_get_prev_value(self.maximum_drawdown(period)[0], idx)
            ),
            # Drawdown recovery with threshold - recovering by at least threshold amount
            'drawdown_recovery': lambda df, idx, period=20, recovery_threshold=0.5: (
                safe_get_value(self.maximum_drawdown(period)[0], idx) > 
                safe_get_prev_value(self.maximum_drawdown(period)[0], idx) + recovery_threshold
            ),
            
            # Coppock Curve Conditions
            'coppock_buy_signal': lambda df, idx, roc1_period=14, roc2_period=11, wma_period=10: (
                safe_get_value(self.coppock_curve(roc1_period, roc2_period, wma_period), idx) > 0 and
                safe_get_prev_value(self.coppock_curve(roc1_period, roc2_period, wma_period), idx) <= 0
            ),
            'coppock_turning_up': lambda df, idx, roc1_period=14, roc2_period=11, wma_period=10: (
                safe_get_value(self.coppock_curve(roc1_period, roc2_period, wma_period), idx) > 
                safe_get_prev_value(self.coppock_curve(roc1_period, roc2_period, wma_period), idx) and
                safe_get_value(self.coppock_curve(roc1_period, roc2_period, wma_period), idx) < 0
            ),
            'coppock_positive': lambda df, idx, roc1_period=14, roc2_period=11, wma_period=10: (
                safe_get_value(self.coppock_curve(roc1_period, roc2_period, wma_period), idx) > 0
            ),
            
            # Volatility Stop Conditions
            'volatility_stop_long': lambda df, idx, period=20, multiplier=2.5: (
                safe_get_value(self.df['closePrice'].values, idx) > 
                safe_get_value(self.volatility_stop(period, multiplier)[0], idx)
            ),
            'volatility_stop_short': lambda df, idx, period=20, multiplier=2.5: (
                safe_get_value(self.df['closePrice'].values, idx) < 
                safe_get_value(self.volatility_stop(period, multiplier)[1], idx)
            ),
            
            # Market Regime Conditions
            'regime_bullish': lambda df, idx, adx_period=14, adx_threshold=25: (
                safe_get_value(self.market_regime(adx_period, adx_threshold), idx) == 2
            ),
            'regime_bearish': lambda df, idx, adx_period=14, adx_threshold=25: (
                safe_get_value(self.market_regime(adx_period, adx_threshold), idx) == -2
            ),
            'regime_ranging': lambda df, idx, adx_period=14, adx_threshold=25: (
                safe_get_value(self.market_regime(adx_period, adx_threshold), idx) == 0
            ),
            'regime_crash': lambda df, idx, adx_period=14, adx_threshold=25: (
                safe_get_value(self.market_regime(adx_period, adx_threshold), idx) == -1
            ),
            # Regime trending bullish - ADX trending + price above MA
            'regime_trending_bullish': lambda df, idx, adx_period=14, adx_threshold=25: (
                safe_get_value(self.adx(adx_period), idx) > adx_threshold and
                safe_get_value(self.market_regime(adx_period, adx_threshold), idx) > 0
            ),
            # Regime trending bearish - ADX trending + price below MA
            'regime_trending_bearish': lambda df, idx, adx_period=14, adx_threshold=25: (
                safe_get_value(self.adx(adx_period), idx) > adx_threshold and
                safe_get_value(self.market_regime(adx_period, adx_threshold), idx) < 0
            ),
            # MA Band bullish - price above BOTH MAs
            'ma_band_bullish': lambda df, idx, short_period=20, long_period=90: (
                safe_get_value(self.close, idx) > safe_get_value(self.sma(short_period), idx) and
                safe_get_value(self.close, idx) > safe_get_value(self.sma(long_period), idx)
            ),
            # MA Band defensive - price BETWEEN MAs (below short, above long OR vice versa)
            'ma_band_defensive': lambda df, idx, short_period=20, long_period=90: (
                not (safe_get_value(self.close, idx) > safe_get_value(self.sma(short_period), idx) and
                     safe_get_value(self.close, idx) > safe_get_value(self.sma(long_period), idx)) and
                not (safe_get_value(self.close, idx) < safe_get_value(self.sma(short_period), idx) and
                     safe_get_value(self.close, idx) < safe_get_value(self.sma(long_period), idx))
            ),
            # Aroon strong uptrend - AroonUp > up_threshold AND AroonDown < down_threshold
            'aroon_strong_uptrend': lambda df, idx, period=25, up_threshold=70.0, down_threshold=30.0: (
                safe_get_value(self.aroon(period)[0], idx) > up_threshold and
                safe_get_value(self.aroon(period)[1], idx) < down_threshold
            ),
            # Donchian middle cross down - price crosses below middle band
            'donchian_middle_cross_down': lambda df, idx, period=20: (
                safe_get_value(self.close, idx) < safe_get_value(self.donchian_channels(period)[0], idx) and
                safe_get_prev_value(self.close, idx) >= safe_get_prev_value(self.donchian_channels(period)[0], idx)
            ),
            # NOTE: supertrend_swing_break_bullish defined later in advanced section
            # Trend with SMA filter - bullish trend with price above SMA200
            'trend_with_sma_filter': lambda df, idx, sma_period=200, trend_period=50: (
                idx >= sma_period and
                safe_get_value(self.close, idx) > safe_get_value(self.sma(sma_period), idx) and
                safe_get_value(self.sma(trend_period), idx) > safe_get_prev_value(self.sma(trend_period), idx)
            ),
            # SOXL Trend Surge bullish - EMA trend + Supertrend + Volume + ATR rising
            'soxl_trend_surge_bullish': lambda df, idx, ema_len=200, st_factor=3.0, st_atr=10, vol_len=20, ema_buffer_pct=0.5: (
                idx >= max(ema_len, vol_len, st_atr) and
                # Price above EMA (with buffer)
                safe_get_value(self.close, idx) > safe_get_value(self.ema(ema_len), idx) * (1 + ema_buffer_pct/100) and
                # Supertrend bullish
                safe_get_value(self.supertrend(st_atr, st_factor)[0], idx) == 1 and
                # Volume above average
                safe_get_value(self.df['lastTradedVolume'].values, idx) > 
                pd.Series(self.df['lastTradedVolume'].values).rolling(window=vol_len, min_periods=1).mean().iloc[idx]
            ),
            
            # Accumulation/Distribution Conditions
            'ad_accumulation': lambda df, idx, ma_period=20: (
                safe_get_value(self.accumulation_distribution(), idx) > 
                pd.Series(self.accumulation_distribution()).rolling(window=ma_period, min_periods=1).mean().iloc[idx]
            ),
            'ad_distribution': lambda df, idx, ma_period=20: (
                safe_get_value(self.accumulation_distribution(), idx) < 
                pd.Series(self.accumulation_distribution()).rolling(window=ma_period, min_periods=1).mean().iloc[idx]
            ),
            'ad_divergence_bullish': lambda df, idx, lookback=10: (
                idx >= lookback and
                self.df['closePrice'].iloc[idx] < self.df['closePrice'].iloc[idx-lookback] and
                safe_get_value(self.accumulation_distribution(), idx) > 
                safe_get_value(self.accumulation_distribution(), idx-lookback)
            ),
            'ad_divergence_bearish': lambda df, idx, lookback=10: (
                idx >= lookback and
                self.df['closePrice'].iloc[idx] > self.df['closePrice'].iloc[idx-lookback] and
                safe_get_value(self.accumulation_distribution(), idx) < 
                safe_get_value(self.accumulation_distribution(), idx-lookback)
            ),
            
            # Composer RSI Strategy (Canonical: RSI(10) + Trend Filter + No Spike Down)
            # Based on Composer.trade 117,356% return strategy for LETFs
            # Buy when: RSI(10) < 29 AND price > SMA(20) AND price > SMA(90) AND no -2% spike
            'composer_rsi_strategy': lambda df, idx, rsi_period=10, rsi_oversold=29.0, ma_short=20, ma_long=90, spike_down=-2.0: (
                idx >= max(rsi_period, ma_long) and
                # RSI deeply oversold (speedometer)
                safe_get_value(self.rsi(rsi_period), idx) < rsi_oversold and
                # Trend filter: price above both MAs (not in defensive mode)
                safe_get_value(self.close, idx) > safe_get_value(self.sma(ma_short), idx) and
                safe_get_value(self.close, idx) > safe_get_value(self.sma(ma_long), idx) and
                # No crash spike: daily return > -2%
                (idx == 0 or (safe_get_value(self.close, idx) / safe_get_value(self.close, idx - 1) - 1) * 100 > spike_down)
            ),
            
            # Multi-MA bullish cross - flexible MA type crossover
            'multi_ma_bullish_cross': lambda df, idx, fast_ma_period=50, slow_ma_period=200: (
                safe_get_value(self.ema(fast_ma_period), idx) > safe_get_value(self.ema(slow_ma_period), idx) and
                safe_get_prev_value(self.ema(fast_ma_period), idx) <= safe_get_prev_value(self.ema(slow_ma_period), idx)
            ),
            
            # Multi-MA EMA cross - fast EMA crosses slow EMA
            'multi_ma_ema_cross': lambda df, idx, fast_period=12, slow_period=26: (
                safe_get_value(self.ema(fast_period), idx) > safe_get_value(self.ema(slow_period), idx) and
                safe_get_prev_value(self.ema(fast_period), idx) <= safe_get_prev_value(self.ema(slow_period), idx)
            ),
            'multi_ma_golden_cross': lambda df, idx, fast_period=50, slow_period=200: (
                # Classic golden cross: 50 SMA crosses above 200 SMA
                idx >= int(slow_period) and
                safe_get_value(self.sma(int(fast_period)), idx) > safe_get_value(self.sma(int(slow_period)), idx) and
                safe_get_prev_value(self.sma(int(fast_period)), idx) <= safe_get_prev_value(self.sma(int(slow_period)), idx)
            ),
            
            # VWAP deviation - price deviates from VWAP by threshold
            'vwap_deviation': lambda df, idx, threshold=0.02: (
                abs(safe_get_value(self.close, idx) - safe_get_value(self.vwap(), idx)) / 
                (safe_get_value(self.vwap(), idx) + 1e-9) > threshold
            ),
            
            # Inverse Fisher RSI Conditions
            'iftrsi_buy_signal': lambda df, idx, period=5, threshold=-0.5: (
                safe_get_value(self.inverse_fisher_rsi(period), idx) > threshold and
                safe_get_prev_value(self.inverse_fisher_rsi(period), idx) <= threshold
            ),
            'iftrsi_sell_signal': lambda df, idx, period=5, threshold=0.5: (
                safe_get_value(self.inverse_fisher_rsi(period), idx) < threshold and
                safe_get_prev_value(self.inverse_fisher_rsi(period), idx) >= threshold
            ),
            'iftrsi_extreme_oversold': lambda df, idx, period=5, threshold=-0.9: (
                safe_get_value(self.inverse_fisher_rsi(period), idx) < threshold
            ),
            'iftrsi_extreme_overbought': lambda df, idx, period=5, threshold=0.9: (
                safe_get_value(self.inverse_fisher_rsi(period), idx) > threshold
            ),
            
            # Heikin-Ashi Conditions
            'heikin_ashi_bullish': lambda df, idx: (
                safe_get_value(self.heikin_ashi()[3], idx) > safe_get_value(self.heikin_ashi()[0], idx)
            ),
            'heikin_ashi_bearish': lambda df, idx: (
                safe_get_value(self.heikin_ashi()[3], idx) < safe_get_value(self.heikin_ashi()[0], idx)
            ),
            'heikin_ashi_reversal_bull': lambda df, idx: (
                safe_get_value(self.heikin_ashi()[3], idx) > safe_get_value(self.heikin_ashi()[0], idx) and
                safe_get_prev_value(self.heikin_ashi()[3], idx) <= safe_get_prev_value(self.heikin_ashi()[0], idx)
            ),
            'heikin_ashi_reversal_bear': lambda df, idx: (
                safe_get_value(self.heikin_ashi()[3], idx) < safe_get_value(self.heikin_ashi()[0], idx) and
                safe_get_prev_value(self.heikin_ashi()[3], idx) >= safe_get_prev_value(self.heikin_ashi()[0], idx)
            ),
            
            # Pivot Point Conditions
            'pivot_above_r3': lambda df, idx, pivot_type='camarilla': (
                'R3' in self.pivot_points(pivot_type) and
                safe_get_value(self.df['closePrice'].values, idx) > 
                safe_get_value(self.pivot_points(pivot_type)['R3'], idx)
            ),
            'pivot_below_s3': lambda df, idx, pivot_type='camarilla': (
                'S3' in self.pivot_points(pivot_type) and
                safe_get_value(self.df['closePrice'].values, idx) < 
                safe_get_value(self.pivot_points(pivot_type)['S3'], idx)
            ),
            'pivot_bounce_s3': lambda df, idx, pivot_type='camarilla': (
                'S3' in self.pivot_points(pivot_type) and
                safe_get_value(self.df['lowPrice'].values, idx) <= safe_get_value(self.pivot_points(pivot_type)['S3'], idx) and
                safe_get_value(self.df['closePrice'].values, idx) > safe_get_value(self.pivot_points(pivot_type)['S3'], idx)
            ),
            'pivot_reject_r3': lambda df, idx, pivot_type='camarilla': (
                'R3' in self.pivot_points(pivot_type) and
                safe_get_value(self.df['highPrice'].values, idx) >= safe_get_value(self.pivot_points(pivot_type)['R3'], idx) and
                safe_get_value(self.df['closePrice'].values, idx) < safe_get_value(self.pivot_points(pivot_type)['R3'], idx)
            ),
            
            # Volume-Weighted MACD Conditions
            'vwmacd_bullish_cross': lambda df, idx, fast_period=12, slow_period=26, signal_period=9: (
                safe_get_value(self.volume_weighted_macd(fast_period, slow_period, signal_period)[0], idx) > 
                safe_get_value(self.volume_weighted_macd(fast_period, slow_period, signal_period)[1], idx) and
                safe_get_prev_value(self.volume_weighted_macd(fast_period, slow_period, signal_period)[0], idx) <= 
                safe_get_prev_value(self.volume_weighted_macd(fast_period, slow_period, signal_period)[1], idx)
            ),
            'vwmacd_bearish_cross': lambda df, idx, fast_period=12, slow_period=26, signal_period=9: (
                safe_get_value(self.volume_weighted_macd(fast_period, slow_period, signal_period)[0], idx) < 
                safe_get_value(self.volume_weighted_macd(fast_period, slow_period, signal_period)[1], idx) and
                safe_get_prev_value(self.volume_weighted_macd(fast_period, slow_period, signal_period)[0], idx) >= 
                safe_get_prev_value(self.volume_weighted_macd(fast_period, slow_period, signal_period)[1], idx)
            ),
            'vwmacd_positive': lambda df, idx, fast_period=12, slow_period=26, signal_period=9, threshold=0: (
                safe_get_value(self.volume_weighted_macd(fast_period, slow_period, signal_period)[0], idx) > threshold
            ),
            'vwmacd_histogram_positive': lambda df, idx, fast_period=12, slow_period=26, signal_period=9: (
                safe_get_value(self.volume_weighted_macd(fast_period, slow_period, signal_period)[2], idx) > 0
            ),
            
            # ============================================================================
            # COMBINATION CONDITIONS - CRASH PROTECTION & BOTTOM DETECTION
            # ============================================================================
            
            # CRASH_SHIELD - Prevents trading during market crashes
            # Combines: Ulcer Index + Drawdown + Market Regime
            'crash_shield_active': lambda df, idx, ulcer_threshold=10, dd_threshold=-15, lookback=5: (
                # High stress (Ulcer > threshold)
                safe_get_value(self.ulcer_index(14), idx) > ulcer_threshold and
                # Deep drawdown
                safe_get_value(self.maximum_drawdown(20)[0], idx) < dd_threshold and
                # Not recovering yet (drawdown still worsening or flat)
                (idx < lookback or 
                 safe_get_value(self.maximum_drawdown(20)[0], idx) <= 
                 safe_get_value(self.maximum_drawdown(20)[0], idx-lookback))
            ),
            
            # BOTTOM_HUNTER - Detects major market bottoms (Canonical: MACD Divergence + RSI + BB)
            # 1. MACD bullish divergence (price lower low, MACD higher low)
            # 2. RSI(14) < 31 (oversold)
            # 3. Price below BB middle line
            'bottom_hunter_signal': lambda df, idx, rsi_threshold=31, bb_period=20, bb_std=2.0, lookback=60: (
                idx >= lookback and
                # RSI oversold
                safe_get_value(self.rsi(14), idx) < rsi_threshold and
                # Price below BB middle (20 SMA)
                safe_get_value(self.close, idx) < safe_get_value(self.bollinger_bands(bb_period, bb_std)[1], idx) and
                # MACD bullish divergence: price made lower low but MACD made higher low
                (lambda price_ll, macd_hl: price_ll and macd_hl)(
                    # Price lower low: current low < min of first half
                    safe_get_value(self.low, idx) < min(safe_get_value(self.low, idx - i) for i in range(lookback // 2, lookback)),
                    # MACD higher low: current MACD > MACD at price low point
                    safe_get_value(self.macd(12, 26, 9)[0], idx) > safe_get_value(self.macd(12, 26, 9)[0], idx - lookback // 2)
                )
            ),
            
            # RECOVERY_RIDER - Catches the bounce after crash
            # Combines: IFT RSI buy + Heikin-Ashi reversal + Drawdown recovering
            'recovery_rider_signal': lambda df, idx, dd_improvement=5: (
                # IFT RSI buy signal
                (safe_get_value(self.inverse_fisher_rsi(5), idx) > -0.5 and
                 safe_get_prev_value(self.inverse_fisher_rsi(5), idx) <= -0.5) and
                # Heikin-Ashi bullish reversal
                (safe_get_value(self.heikin_ashi()[3], idx) > safe_get_value(self.heikin_ashi()[0], idx) and
                 safe_get_prev_value(self.heikin_ashi()[3], idx) <= safe_get_prev_value(self.heikin_ashi()[0], idx)) and
                # Drawdown improving (less negative than 5 bars ago)
                (idx >= 5 and
                 safe_get_value(self.maximum_drawdown(20)[0], idx) > 
                 safe_get_value(self.maximum_drawdown(20)[0], idx-5) + dd_improvement)
            ),
            
            # RANGE_TRADER - Trades ranging markets safely
            # Combines: Low ADX (ranging) + BB oversold + Pivot bounce
            'range_trader_buy': lambda df, idx, adx_threshold=25: (
                # Market is ranging (low ADX)
                safe_get_value(self.adx(14), idx) < adx_threshold and
                # Price at lower Bollinger Band
                safe_get_value(self.bb_position(), idx) < 0.2 and
                # Bouncing from pivot support (S2 or S3)
                ('S2' in self.pivot_points('camarilla') and
                 safe_get_value(self.df['lowPrice'].values, idx) <= 
                 safe_get_value(self.pivot_points('camarilla')['S2'], idx) and
                 safe_get_value(self.df['closePrice'].values, idx) > 
                 safe_get_value(self.pivot_points('camarilla')['S2'], idx))
            ),
            
            # MOMENTUM_SURGE - Catches strong momentum after consolidation
            # Combines: TTM Squeeze off + VWMACD cross + Volume confirmation
            'momentum_surge_signal': lambda df, idx, volume_ratio=1.3: (
                # TTM Squeeze firing (was on, now off)
                (idx > 0 and
                 safe_get_value(self.ttm_squeeze()[2], idx) == False and
                 safe_get_prev_value(self.ttm_squeeze()[2], idx) == True) and
                # VWMACD bullish cross with volume
                (safe_get_value(self.volume_weighted_macd()[0], idx) > 
                 safe_get_value(self.volume_weighted_macd()[1], idx) and
                 safe_get_prev_value(self.volume_weighted_macd()[0], idx) <= 
                 safe_get_prev_value(self.volume_weighted_macd()[1], idx)) and
                # Volume confirmation
                (safe_get_value(self.df['lastTradedVolume'].values, idx) > 
                 pd.Series(self.df['lastTradedVolume'].values).rolling(10).mean().iloc[idx] * volume_ratio)
            ),

            # ANCHORED VWAP PULLBACK (long-only, trend filter)
            # Approximates anchored VWAP using current session VWAP and pullback threshold
            # Params: pullback (0.5%-2.0%), ema_fast (20/50), ema_slow (100/200)
            'anchored_vwap_pullback_long': lambda df, idx, pullback=0.01, ema_fast=50, ema_slow=200: (
                idx > 0 and
                # Uptrend filter
                safe_get_value(self.ema(ema_fast), idx) > safe_get_value(self.ema(ema_slow), idx) and
                safe_get_value(self.ema(ema_slow), idx) > safe_get_prev_value(self.ema(ema_slow), idx - 4) and
                # Pullback to VWAP or slightly below
                (lambda cp, vw: (cp - vw) / vw <= -abs(pullback))(
                    safe_get_value(self.df['closePrice'].values, idx),
                    safe_get_value(self.vwap(), idx)
                )
            ),

            # SQUEEZE → RETEST → GO (long-only)
            # Detects low-vol squeeze, breakout above BB upper, then successful retest
            # Params: period, std_dev, lookback (for squeeze width), retest_tolerance
            'squeeze_retest_go': lambda df, idx, period=20, std_dev=2.0, lookback=30, retest_tolerance=0.005: (
                idx > 1 and
                # Compute BB width and detect squeeze vs. recent average
                (lambda upper, middle, lower: (
                    (pd.Series((upper - lower) / (middle + 1e-9)).rolling(lookback).mean().iloc[idx] <
                     pd.Series((upper - lower) / (middle + 1e-9)).rolling(lookback * 3).mean().iloc[idx] * 0.8) and
                    # Prior bar breakout above upper band
                    safe_get_prev_value(self.df['closePrice'].values, idx) > safe_get_prev_value(upper, idx) and
                    # Current bar retest holds near upper band
                    abs(safe_get_value(self.df['closePrice'].values, idx) - safe_get_value(upper, idx)) /
                        max(1e-9, safe_get_value(upper, idx)) <= retest_tolerance and
                    # Trend confirmation
                    safe_get_value(self.ema(50), idx) > safe_get_value(self.ema(200), idx)
                ))(*self.bollinger_bands(period=period, std_dev=std_dev))
            ),

            # HURST-ADAPTIVE ROUTER (proxy using ADX for regime)
            # Range regime: mean-revert longs near lower band; Trend regime: breakout/continuation longs
            # Params: adx_threshold, rsi_period
            'hurst_adaptive_router_long': lambda df, idx, adx_threshold=20, rsi_period=14: (
                idx > 0 and (
                    # Range regime path
                    (
                        safe_get_value(self.adx(14), idx) < adx_threshold and
                        # In lower half of BB channel and RSI supportive
                        safe_get_value(self.bb_position(), idx) < 0.4 and
                        safe_get_value(self.rsi(period=rsi_period), idx) > 45 and
                        # Avoid downtrends
                        safe_get_value(self.ema(200), idx) >= safe_get_prev_value(self.ema(200), idx)
                    )
                    or
                    # Trend regime path
                    (
                        safe_get_value(self.adx(14), idx) >= adx_threshold and
                        safe_get_value(self.df['closePrice'].values, idx) > safe_get_value(self.ema(20), idx) and
                        safe_get_value(self.ema(20), idx) > safe_get_value(self.ema(50), idx) and
                        safe_get_value(self.rsi(period=rsi_period), idx) > 50
                    )
                )
            ),
            
            # SMART_ENTRY - Composite: MACD Golden Cross + RSI + ADX confirmation
            # High-probability trend-following entry with ADX trend strength filter
            'smart_entry_signal': lambda df, idx, rsi_period=14, rsi_threshold=50, macd_fast=12, macd_slow=26, macd_signal=9, adx_period=14, adx_threshold=25: (
                idx >= max(int(macd_slow), int(adx_period)) and
                # MACD golden cross: MACD line crosses above signal line
                safe_get_value(self.macd(int(macd_fast), int(macd_slow), int(macd_signal))[0], idx) > 
                safe_get_value(self.macd(int(macd_fast), int(macd_slow), int(macd_signal))[1], idx) and
                safe_get_prev_value(self.macd(int(macd_fast), int(macd_slow), int(macd_signal))[0], idx) <= 
                safe_get_prev_value(self.macd(int(macd_fast), int(macd_slow), int(macd_signal))[1], idx) and
                # RSI confirmation: in favorable range
                safe_get_value(self.rsi(int(rsi_period)), idx) < rsi_threshold and
                # ADX filter: trend is strong enough
                safe_get_value(self.adx(int(adx_period)), idx) >= adx_threshold
            ),
            
            # ============================================================================
            # NEW TIER-1 INDICATORS (From Manus Analysis - December 2025)
            # ============================================================================
            
            # TTM Squeeze Momentum - Pre-breakout detection
            # Returns True when squeeze releases with positive momentum
            'ttm_squeeze_momentum_bullish': lambda df, idx, bb_length=20, bb_mult=2.0, kc_length=20, kc_mult=1.5, momentum_length=12, momentum_threshold=0.0: (
                self.ttm_squeeze(bb_length, bb_mult, kc_length, kc_mult, momentum_length)[1][idx] and  # squeeze_off
                self.ttm_squeeze(bb_length, bb_mult, kc_length, kc_mult, momentum_length)[2][idx] > momentum_threshold and  # momentum > threshold
                (idx == 0 or self.ttm_squeeze(bb_length, bb_mult, kc_length, kc_mult, momentum_length)[2][idx] > 
                 self.ttm_squeeze(bb_length, bb_mult, kc_length, kc_mult, momentum_length)[2][idx-1])  # momentum increasing
            ),
            
            # TTM Squeeze Momentum Bearish - Pre-breakdown detection
            'ttm_squeeze_momentum_bearish': lambda df, idx, bb_length=20, bb_mult=2.0, kc_length=20, kc_mult=1.5, momentum_length=12, momentum_threshold=0.0: (
                self.ttm_squeeze(bb_length, bb_mult, kc_length, kc_mult, momentum_length)[1][idx] and  # squeeze_off
                self.ttm_squeeze(bb_length, bb_mult, kc_length, kc_mult, momentum_length)[2][idx] < -momentum_threshold and  # momentum < -threshold
                (idx == 0 or self.ttm_squeeze(bb_length, bb_mult, kc_length, kc_mult, momentum_length)[2][idx] < 
                 self.ttm_squeeze(bb_length, bb_mult, kc_length, kc_mult, momentum_length)[2][idx-1])  # momentum decreasing
            ),
            
            # TTM Squeeze On - Low volatility consolidation (potential breakout setup)
            'ttm_squeeze_on': lambda df, idx, bb_length=20, bb_mult=2.0, kc_length=20, kc_mult=1.5: (
                self.ttm_squeeze(bb_length, bb_mult, kc_length, kc_mult, 12)[0][idx]  # squeeze_on
            ),
            
            # RSI Hidden Bullish Divergence - Bottom hunting for trend continuation
            # Price makes higher low, RSI makes lower low
            'rsi_hidden_bullish_divergence': lambda df, idx, rsi_period=14, lookback=20, min_rsi=30, max_rsi=70: (
                self.rsi_hidden_divergence(rsi_period, lookback)[0][idx] and
                min_rsi <= safe_get_value(self.rsi(rsi_period), idx) <= max_rsi
            ),
            
            # RSI Hidden Bearish Divergence - Top detection for trend continuation
            # Price makes lower high, RSI makes higher high  
            'rsi_hidden_bearish_divergence': lambda df, idx, rsi_period=14, lookback=20, min_rsi=30, max_rsi=70: (
                self.rsi_hidden_divergence(rsi_period, lookback)[1][idx] and
                min_rsi <= safe_get_value(self.rsi(rsi_period), idx) <= max_rsi
            ),
            
            # Volume Accumulation Divergence - Pre-breakout detection through volume
            # Volume increasing while price remains flat
            'volume_accumulation_bullish': lambda df, idx, volume_period=14, price_period=14, min_strength=5.0: (
                self.volume_accumulation(volume_period, price_period)[0][idx] and
                self.volume_accumulation(volume_period, price_period)[1][idx] >= min_strength
            ),
            
            # Volume Accumulation with Trend Filter
            # Combines volume accumulation with trend confirmation
            'volume_accumulation_trend': lambda df, idx, volume_period=14, price_period=14, min_strength=3.0: (
                self.volume_accumulation(volume_period, price_period)[0][idx] and
                self.volume_accumulation(volume_period, price_period)[1][idx] >= min_strength and
                safe_get_value(self.ema(20), idx) > safe_get_value(self.ema(50), idx)  # Trend filter
            ),
            
            # ============================================================================
            # TIER 2 INDICATORS (From Manus Analysis - December 2025)
            # ============================================================================
            
            # EMA 8/21 Cross (Crypto Standard) - Fast trend following
            'ema_8_21_bullish_cross': lambda df, idx, confirmation=1: (
                safe_get_value(self.ema(8), idx) > safe_get_value(self.ema(21), idx) and
                (idx < confirmation or safe_get_prev_value(self.ema(8), idx - confirmation + 1) <= 
                 safe_get_prev_value(self.ema(21), idx - confirmation + 1))
            ),
            
            'ema_8_21_bearish_cross': lambda df, idx, confirmation=1: (
                safe_get_value(self.ema(8), idx) < safe_get_value(self.ema(21), idx) and
                (idx < confirmation or safe_get_prev_value(self.ema(8), idx - confirmation + 1) >= 
                 safe_get_prev_value(self.ema(21), idx - confirmation + 1))
            ),
            
            # Choppiness Index - Avoid trading in choppy/ranging markets
            # Returns True when market is NOT choppy (safe to trade)
            'choppiness_trending': lambda df, idx, period=14, choppy_threshold=61.8: (
                idx >= period and
                (lambda tr, high_low: (
                    100 * np.log10(tr.sum() / (high_low + 1e-9)) / np.log10(period) < choppy_threshold
                ))(
                    pd.concat([
                        pd.Series(self.high - self.low),
                        pd.Series(abs(self.high - pd.Series(self.close).shift(1))),
                        pd.Series(abs(self.low - pd.Series(self.close).shift(1)))
                    ], axis=1).max(axis=1).iloc[idx-period+1:idx+1],
                    pd.Series(self.high).iloc[idx-period+1:idx+1].max() - 
                    pd.Series(self.low).iloc[idx-period+1:idx+1].min()
                )
            ),
            
            # VWAP Bounce Signal - Institutional support/resistance level
            'vwap_bounce_bullish': lambda df, idx, distance_threshold=0.002, bounce_confirmation=1: (
                idx > bounce_confirmation and
                # Recently touched VWAP (within threshold)
                abs(safe_get_value(self.df['closePrice'].values, idx - bounce_confirmation) - 
                    safe_get_value(self.vwap(), idx - bounce_confirmation)) / 
                    (safe_get_value(self.vwap(), idx - bounce_confirmation) + 1e-9) < distance_threshold and
                # Now above VWAP
                safe_get_value(self.df['closePrice'].values, idx) > safe_get_value(self.vwap(), idx)
            ),
            
            'vwap_bounce_bearish': lambda df, idx, distance_threshold=0.002, bounce_confirmation=1: (
                idx > bounce_confirmation and
                # Recently touched VWAP (within threshold)
                abs(safe_get_value(self.df['closePrice'].values, idx - bounce_confirmation) - 
                    safe_get_value(self.vwap(), idx - bounce_confirmation)) / 
                    (safe_get_value(self.vwap(), idx - bounce_confirmation) + 1e-9) < distance_threshold and
                # Now below VWAP
                safe_get_value(self.df['closePrice'].values, idx) < safe_get_value(self.vwap(), idx)
            ),
            
            # ============================================================================
            # 5-MINUTE SIGNAL ENGINES (SOXL/TECL Intraday Optimized)
            # Tunable parameters with min/max/step for grid-search optimization
            # ============================================================================
            
            # 1. VWAP RECLAIM - N consecutive closes above VWAP after being below
            # Params: confirmation_bars (1-6), distance_pct (0-1%)
            'vwap_reclaim_bullish': lambda df, idx, confirmation_bars=2, distance_pct=0.05: (
                idx >= confirmation_bars + 1 and
                # Was below VWAP before the confirmation period
                safe_get_value(self.close, idx - confirmation_bars - 1) < safe_get_value(self.vwap(), idx - confirmation_bars - 1) and
                # All confirmation bars closed above VWAP
                all(
                    safe_get_value(self.close, idx - i) > safe_get_value(self.vwap(), idx - i) * (1 + distance_pct / 100)
                    for i in range(confirmation_bars)
                )
            ),
            
            # VWAP LOSE - N consecutive closes below VWAP (bearish)
            'vwap_lose_bearish': lambda df, idx, confirmation_bars=2, distance_pct=0.05: (
                idx >= confirmation_bars + 1 and
                # Was above VWAP before the confirmation period
                safe_get_value(self.close, idx - confirmation_bars - 1) > safe_get_value(self.vwap(), idx - confirmation_bars - 1) and
                # All confirmation bars closed below VWAP
                all(
                    safe_get_value(self.close, idx - i) < safe_get_value(self.vwap(), idx - i) * (1 - distance_pct / 100)
                    for i in range(confirmation_bars)
                )
            ),
            
            # 2. EMA PULLBACK BOUNCE - Price dips X% below fast EMA then bounces back
            # Params: ema_fast (5-21), ema_slow (100-250), pullback_pct (0-1.5%), bounce_bars (1-5)
            'ema_pullback_bounce_bullish': lambda df, idx, ema_fast=9, ema_slow=200, pullback_pct=0.5, bounce_bars=2: (
                idx >= bounce_bars + 2 and
                # Uptrend: fast EMA above slow EMA
                safe_get_value(self.ema(ema_fast), idx) > safe_get_value(self.ema(ema_slow), idx) and
                # Price dipped below fast EMA by pullback_pct
                any(
                    safe_get_value(self.low, idx - i) < safe_get_value(self.ema(ema_fast), idx - i) * (1 - pullback_pct / 100)
                    for i in range(1, bounce_bars + 2)
                ) and
                # Now closed back above fast EMA (bounce confirmed)
                safe_get_value(self.close, idx) > safe_get_value(self.ema(ema_fast), idx)
            ),
            
            # 3. OPENING RANGE BREAKOUT (ORB) - Breakout above N-bar range
            # Params: range_bars (1-6 = 5-30 min on 5m), breakout_buffer_pct (0-0.5%)
            'opening_range_breakout_bullish': lambda df, idx, range_bars=3, breakout_buffer_pct=0.1: (
                idx >= range_bars + 1 and
                # Calculate range high from first N bars (approximation: use lookback)
                (lambda range_high: (
                    safe_get_value(self.close, idx) > range_high * (1 + breakout_buffer_pct / 100)
                ))(
                    max(safe_get_value(self.high, idx - i) for i in range(1, range_bars + 1))
                )
            ),
            
            'opening_range_breakout_bearish': lambda df, idx, range_bars=3, breakout_buffer_pct=0.1: (
                idx >= range_bars + 1 and
                # Calculate range low from first N bars
                (lambda range_low: (
                    safe_get_value(self.close, idx) < range_low * (1 - breakout_buffer_pct / 100)
                ))(
                    min(safe_get_value(self.low, idx - i) for i in range(1, range_bars + 1))
                )
            ),
            
            # 4. BOLLINGER BANDWIDTH SQUEEZE - BB width below threshold (pre-breakout)
            # Params: bb_period (14-50), bb_std (1.5-3.5), bandwidth_threshold_pct (0.2-5.0)
            'bb_bandwidth_squeeze': lambda df, idx, bb_period=20, bb_std=2.0, bandwidth_threshold_pct=3.0: (
                idx >= bb_period and
                (lambda middle, upper, lower: (
                    ((upper - lower) / (middle + 1e-9)) * 100 < bandwidth_threshold_pct
                ))(
                    safe_get_value(self.bollinger_bands(bb_period, bb_std)[0], idx),
                    safe_get_value(self.bollinger_bands(bb_period, bb_std)[1], idx),
                    safe_get_value(self.bollinger_bands(bb_period, bb_std)[2], idx)
                )
            ),
            
            # BB BANDWIDTH EXPANSION - Squeeze released with upward breakout
            'bb_bandwidth_expansion_bullish': lambda df, idx, bb_period=20, bb_std=2.0, bandwidth_threshold_pct=3.0, confirm_bars=2: (
                idx >= bb_period + confirm_bars and
                # Was in squeeze (narrow bands)
                (lambda prev_mid, prev_upper, prev_lower: (
                    ((prev_upper - prev_lower) / (prev_mid + 1e-9)) * 100 < bandwidth_threshold_pct
                ))(
                    safe_get_value(self.bollinger_bands(bb_period, bb_std)[0], idx - confirm_bars),
                    safe_get_value(self.bollinger_bands(bb_period, bb_std)[1], idx - confirm_bars),
                    safe_get_value(self.bollinger_bands(bb_period, bb_std)[2], idx - confirm_bars)
                ) and
                # Broke above upper band
                safe_get_value(self.close, idx) > safe_get_value(self.bollinger_bands(bb_period, bb_std)[1], idx)
            ),
            
            # 5. RSI + MACD COMBO - Both indicators confirm bullish
            # Params: rsi_len (5-21), rsi_threshold (45-60), macd_fast (6-15), macd_slow (18-35), macd_signal (5-12)
            'rsi_macd_combo_bullish': lambda df, idx, rsi_len=14, rsi_threshold=50, macd_fast=12, macd_slow=26, macd_signal=9: (
                idx >= max(rsi_len, macd_slow) and
                # RSI above threshold (momentum)
                safe_get_value(self.rsi(rsi_len), idx) > rsi_threshold and
                # MACD line above signal line (bullish)
                (lambda macd_line, signal_line, _: macd_line > signal_line)(
                    *[safe_get_value(arr, idx) for arr in self.macd(macd_fast, macd_slow, macd_signal)]
                )
            ),
            
            'rsi_macd_combo_bearish': lambda df, idx, rsi_len=14, rsi_threshold=50, macd_fast=12, macd_slow=26, macd_signal=9: (
                idx >= max(rsi_len, macd_slow) and
                # RSI below threshold
                safe_get_value(self.rsi(rsi_len), idx) < rsi_threshold and
                # MACD line below signal line (bearish)
                (lambda macd_line, signal_line, _: macd_line < signal_line)(
                    *[safe_get_value(arr, idx) for arr in self.macd(macd_fast, macd_slow, macd_signal)]
                )
            ),
            
            # RSI DIP-BUY + MACD (mean reversion style)
            'rsi_dipbuy_macd_bullish': lambda df, idx, rsi_len=14, rsi_oversold=30, macd_fast=12, macd_slow=26, macd_signal=9: (
                idx >= max(rsi_len, macd_slow) + 1 and
                # RSI was oversold recently
                any(safe_get_value(self.rsi(rsi_len), idx - i) < rsi_oversold for i in range(1, 4)) and
                # RSI now recovering
                safe_get_value(self.rsi(rsi_len), idx) > safe_get_value(self.rsi(rsi_len), idx - 1) and
                # MACD histogram turning positive or improving
                (lambda _, __, hist: hist > 0 or (idx > 0 and hist > safe_get_value(self.macd(macd_fast, macd_slow, macd_signal)[2], idx - 1)))(
                    *[safe_get_value(arr, idx) for arr in self.macd(macd_fast, macd_slow, macd_signal)]
                )
            ),
            
            # 6. SUPERTREND + SWING BREAK - Supertrend flips bullish AND breaks swing high
            # Params: st_atr_len (7-28), st_mult (1.0-5.0), swing_lookback (3-20)
            'supertrend_swing_break_bullish': lambda df, idx, st_atr_len=10, st_mult=3.0, swing_lookback=10: (
                idx >= max(st_atr_len, swing_lookback) + 1 and
                # Supertrend just flipped bullish (was -1, now 1)
                safe_get_value(self.supertrend(st_atr_len, st_mult)[1], idx) == 1 and
                safe_get_value(self.supertrend(st_atr_len, st_mult)[1], idx - 1) == -1 and
                # Price breaks above recent swing high
                safe_get_value(self.close, idx) > max(
                    safe_get_value(self.high, idx - i) for i in range(1, swing_lookback + 1)
                )
            ),
            
            'supertrend_swing_break_bearish': lambda df, idx, st_atr_len=10, st_mult=3.0, swing_lookback=10: (
                idx >= max(st_atr_len, swing_lookback) + 1 and
                # Supertrend just flipped bearish (was 1, now -1)
                safe_get_value(self.supertrend(st_atr_len, st_mult)[1], idx) == -1 and
                safe_get_value(self.supertrend(st_atr_len, st_mult)[1], idx - 1) == 1 and
                # Price breaks below recent swing low
                safe_get_value(self.close, idx) < min(
                    safe_get_value(self.low, idx - i) for i in range(1, swing_lookback + 1)
                )
            ),
            
            # 7. ATR VOLATILITY RISING - ATR expanding (good for trend trades)
            # Params: atr_len (7-28), atr_sma_len (10-50), expansion_threshold (1.0-2.0)
            'atr_volatility_rising': lambda df, idx, atr_len=14, atr_sma_len=20, expansion_threshold=1.2: (
                idx >= max(atr_len, atr_sma_len) and
                (lambda atr_val, atr_sma: atr_val > atr_sma * expansion_threshold)(
                    safe_get_value(self.atr(atr_len), idx),
                    np.mean([safe_get_value(self.atr(atr_len), idx - i) for i in range(atr_sma_len)])
                )
            ),
            
            # ATR VOLATILITY CONTRACTING - Low volatility (squeeze setup)
            'atr_volatility_contracting': lambda df, idx, atr_len=14, atr_sma_len=20, contraction_threshold=0.8: (
                idx >= max(atr_len, atr_sma_len) and
                (lambda atr_val, atr_sma: atr_val < atr_sma * contraction_threshold)(
                    safe_get_value(self.atr(atr_len), idx),
                    np.mean([safe_get_value(self.atr(atr_len), idx - i) for i in range(atr_sma_len)])
                )
            ),
            
            # 8. SOXL TREND SURGE - Full combined strategy (EMA + Supertrend + Volume + ATR)
            # Based on PineScript "SOXL Trend Surge v3.0.2"
            # Params: ema_len (100-250), st_factor (1.0-5.0), st_atr (7-28), vol_len (10-50), ema_buffer_pct (0-1%)
            'soxl_trend_surge_bullish': lambda df, idx, ema_len=200, st_factor=3.0, st_atr=10, vol_len=20, ema_buffer_pct=0.5: (
                idx >= max(ema_len, vol_len, st_atr) and
                # Price above EMA (trend filter)
                safe_get_value(self.close, idx) > safe_get_value(self.ema(ema_len), idx) and
                # Outside EMA buffer zone (not choppy)
                abs(safe_get_value(self.close, idx) - safe_get_value(self.ema(ema_len), idx)) / 
                    (safe_get_value(self.ema(ema_len), idx) + 1e-9) > ema_buffer_pct / 100 and
                # Supertrend bullish
                safe_get_value(self.supertrend(st_atr, st_factor)[1], idx) == 1 and
                # Volume confirmation
                safe_get_value(self.volume, idx) > np.mean([safe_get_value(self.volume, idx - i) for i in range(1, vol_len + 1)]) and
                # ATR rising (volatility expanding)
                safe_get_value(self.atr(14), idx) > np.mean([safe_get_value(self.atr(14), idx - i) for i in range(1, 21)])
            ),
            
            # VOLUME SPIKE WITH TREND - High volume + trending
            'volume_spike_trend_bullish': lambda df, idx, vol_sma_len=20, vol_ratio_threshold=1.5, ema_len=50: (
                idx >= max(vol_sma_len, ema_len) and
                # Volume spike
                safe_get_value(self.volume, idx) > np.mean([safe_get_value(self.volume, idx - i) for i in range(1, vol_sma_len + 1)]) * vol_ratio_threshold and
                # Uptrend (close above EMA)
                safe_get_value(self.close, idx) > safe_get_value(self.ema(ema_len), idx) and
                # Green candle
                safe_get_value(self.close, idx) > safe_get_value(self.open, idx)
            ),

            # ============================================================================
            # SMART MONEY CONCEPTS (SMC) INDICATORS
            # ============================================================================

            # SMC Order Block Bullish - Price enters bullish order block zone
            'smc_order_block_bullish': lambda df, idx, lookback=20, min_move_pct=1.0: (
                idx >= lookback and
                safe_get_value(self.order_block(lookback, min_move_pct)[0], idx)
            ),

            # SMC Order Block Bearish - Strong bearish move detected (exit signal)
            'smc_order_block_bearish': lambda df, idx, lookback=20, min_move_pct=1.0: (
                idx >= lookback and
                safe_get_value(self.order_block(lookback, min_move_pct)[1], idx)
            ),

            # =====================================================================
            # NEW: SMC ORDER BLOCK ZONE REVISIT (No Look-Ahead Bias!)
            # These implement PROPER SMC trading: wait for price to RETURN to zone
            # =====================================================================
            
            # SMC Order Block Zone Revisit Bullish - Price returns to DEMAND zone (BUY signal)
            # Use this as ENTRY indicator: buy when price pulls back to support
            'smc_ob_zone_revisit_bullish': lambda df, idx, lookback=20, min_move_pct=1.0, zone_valid_bars=50, min_wait_bars=3: (
                idx >= lookback + min_wait_bars and
                safe_get_value(self.order_block_zone_revisit(lookback, min_move_pct, zone_valid_bars, min_wait_bars)[0], idx)
            ),
            
            # SMC Order Block Zone Revisit Bearish - Price returns to SUPPLY zone (SELL signal)
            # Use this as EXIT indicator: sell when price rallies back to resistance
            'smc_ob_zone_revisit_bearish': lambda df, idx, lookback=20, min_move_pct=1.0, zone_valid_bars=50, min_wait_bars=3: (
                idx >= lookback + min_wait_bars and
                safe_get_value(self.order_block_zone_revisit(lookback, min_move_pct, zone_valid_bars, min_wait_bars)[1], idx)
            ),

            # SMC Fair Value Gap Bullish - Bullish imbalance detected
            'smc_fvg_bullish': lambda df, idx, min_gap_pct=0.1: (
                idx >= 2 and
                safe_get_value(self.fair_value_gap(min_gap_pct)[0], idx)
            ),

            # SMC Fair Value Gap Bearish - Bearish imbalance detected
            'smc_fvg_bearish': lambda df, idx, min_gap_pct=0.1: (
                idx >= 2 and
                safe_get_value(self.fair_value_gap(min_gap_pct)[1], idx)
            ),

            # SMC Liquidity Sweep Bullish - Buy-side liquidity grabbed then reversed
            'smc_liquidity_sweep_bullish': lambda df, idx, swing_lookback=10, confirmation_bars=2: (
                idx >= swing_lookback + confirmation_bars and
                safe_get_value(self.liquidity_sweep(swing_lookback, confirmation_bars)[0], idx)
            ),

            # SMC Liquidity Sweep Bearish - Sell-side liquidity grabbed then reversed
            'smc_liquidity_sweep_bearish': lambda df, idx, swing_lookback=10, confirmation_bars=2: (
                idx >= swing_lookback + confirmation_bars and
                safe_get_value(self.liquidity_sweep(swing_lookback, confirmation_bars)[1], idx)
            ),

            # SMC Break of Structure Bullish - Price breaks above swing high
            'smc_bos_bullish': lambda df, idx, swing_lookback=5: (
                idx >= swing_lookback * 2 and
                safe_get_value(self.break_of_structure(swing_lookback)[0], idx)
            ),

            # SMC Break of Structure Bearish - Price breaks below swing low
            'smc_bos_bearish': lambda df, idx, swing_lookback=5: (
                idx >= swing_lookback * 2 and
                safe_get_value(self.break_of_structure(swing_lookback)[1], idx)
            ),

            # SMC Liquidity Sweep + Volume Confirmation Bullish
            'smc_sweep_volume_bullish': lambda df, idx, swing_lookback=10, vol_mult=1.5, ema_period=31: (
                idx >= max(swing_lookback + 2, ema_period) and
                safe_get_value(self.liquidity_sweep(swing_lookback, 2)[0], idx) and
                safe_get_value(self.volume, idx) > safe_get_value(pd.Series(self.volume).rolling(11).mean().values, idx) * vol_mult and
                safe_get_value(self.close, idx) > safe_get_value(self.ema(ema_period), idx)
            ),

            # SMC Liquidity Sweep + Volume Confirmation Bearish
            'smc_sweep_volume_bearish': lambda df, idx, swing_lookback=10, vol_mult=1.5, ema_period=31: (
                idx >= max(swing_lookback + 2, ema_period) and
                safe_get_value(self.liquidity_sweep(swing_lookback, 2)[1], idx) and
                safe_get_value(self.volume, idx) > safe_get_value(pd.Series(self.volume).rolling(11).mean().values, idx) * vol_mult and
                safe_get_value(self.close, idx) < safe_get_value(self.ema(ema_period), idx)
            ),

            # ============================================================================
            # TURTLE EVOLUTION (Heikin Ashi Enhanced)
            # ============================================================================

            # Turtle Breakout Bullish with Heikin Ashi + ADX filter
            'turtle_ha_breakout_bullish': lambda df, idx, donchian_period=20, adx_period=14, adx_threshold=20: (
                idx >= max(donchian_period, adx_period) and
                safe_get_value(self.donchian_ha_breakout(donchian_period)[0], idx) and
                safe_get_value(self.adx(adx_period), idx) > adx_threshold
            ),

            # Turtle Breakout Bearish with Heikin Ashi + ADX filter
            'turtle_ha_breakout_bearish': lambda df, idx, donchian_period=20, adx_period=14, adx_threshold=20: (
                idx >= max(donchian_period, adx_period) and
                safe_get_value(self.donchian_ha_breakout(donchian_period)[1], idx) and
                safe_get_value(self.adx(adx_period), idx) > adx_threshold
            ),

            # ============================================================================
            # EMA STACK ALIGNMENT
            # ============================================================================

            # EMA Stack Bullish (fast>medium>slow) - configurable periods
            'ema_stack_bullish': lambda df, idx, ema_fast=8, ema_medium=21, ema_slow=50: (
                idx >= ema_slow and
                safe_get_value(self.ema_stack([int(ema_fast), int(ema_medium), int(ema_slow)])[0], idx)
            ),

            # EMA Stack Bearish (fast<medium<slow) - configurable periods
            'ema_stack_bearish': lambda df, idx, ema_fast=8, ema_medium=21, ema_slow=50: (
                idx >= ema_slow and
                safe_get_value(self.ema_stack([int(ema_fast), int(ema_medium), int(ema_slow)])[1], idx)
            ),

            # EMA Reclaim Bullish - Price pulls back below EMA then reclaims it
            'ema_reclaim_bullish': lambda df, idx, ema_period=20, pullback_bars=3: (
                idx >= ema_period + pullback_bars and
                # Was below EMA recently
                any(safe_get_value(self.close, idx - i) < safe_get_value(self.ema(ema_period), idx - i) for i in range(1, pullback_bars + 1)) and
                # Now reclaimed (closed above)
                safe_get_value(self.close, idx) > safe_get_value(self.ema(ema_period), idx) and
                # EMA stack aligned (fast > slow)
                safe_get_value(self.ema(ema_period), idx) > safe_get_value(self.ema(50), idx)
            ),

            # EMA Reclaim Bearish - Price pulls back above EMA then loses it
            'ema_reclaim_bearish': lambda df, idx, ema_period=20, pullback_bars=3: (
                idx >= ema_period + pullback_bars and
                # Was above EMA recently
                any(safe_get_value(self.close, idx - i) > safe_get_value(self.ema(ema_period), idx - i) for i in range(1, pullback_bars + 1)) and
                # Now lost (closed below)
                safe_get_value(self.close, idx) < safe_get_value(self.ema(ema_period), idx) and
                # EMA stack aligned (fast < slow)
                safe_get_value(self.ema(ema_period), idx) < safe_get_value(self.ema(50), idx)
            ),

            # ============================================================================
            # DUAL-PATH TREND CATCHER
            # ============================================================================

            # MOU Path (Breakout) - Volume breakout with MACD near zero
            'dual_path_mou_bullish': lambda df, idx, ema_short=5, ema_medium=13, ema_long=26, vol_mult=1.3: (
                idx >= max(ema_long, 20) and
                # Trend aligned (EMA stack)
                safe_get_value(self.ema(ema_short), idx) > safe_get_value(self.ema(ema_medium), idx) > safe_get_value(self.ema(ema_long), idx) and
                # Volume spike
                safe_get_value(self.volume, idx) > safe_get_value(pd.Series(self.volume).rolling(20).mean().values, idx) * vol_mult and
                # MACD near zero line (within 0.5% of price)
                abs(safe_get_value(self.macd(12, 26, 9)[0], idx)) < safe_get_value(self.close, idx) * 0.005 and
                # MACD crossing up
                safe_get_value(self.macd(12, 26, 9)[0], idx) > safe_get_value(self.macd(12, 26, 9)[1], idx)
            ),

            # KAKU Path (Pullback) - Pin bar pullback with strong MACD
            'dual_path_kaku_bullish': lambda df, idx, ema_short=5, ema_medium=13, ema_long=26, vol_mult=1.5: (
                idx >= max(ema_long, 20) and
                # Trend aligned
                safe_get_value(self.ema(ema_short), idx) > safe_get_value(self.ema(ema_medium), idx) > safe_get_value(self.ema(ema_long), idx) and
                # Pullback: price touched medium EMA
                safe_get_value(self.low, idx) <= safe_get_value(self.ema(ema_medium), idx) * 1.01 and
                # Pin bar: lower wick > 2x body
                (safe_get_value(self.close, idx) - safe_get_value(self.low, idx)) > 2 * abs(safe_get_value(self.close, idx) - safe_get_value(self.open, idx)) and
                # Volume confirmation
                safe_get_value(self.volume, idx) > safe_get_value(pd.Series(self.volume).rolling(20).mean().values, idx) * vol_mult and
                # MACD above zero
                safe_get_value(self.macd(12, 26, 9)[0], idx) > 0
            ),

            # Smart Pullback Hunter - VWAP pullback with ADX confirmation
            'smart_pullback_hunter_bullish': lambda df, idx, ema_fast=20, ema_slow=50, adx_min=20, adx_max=35: (
                idx >= max(ema_slow, 14) and
                # Trend: fast EMA > slow EMA
                safe_get_value(self.ema(ema_fast), idx) > safe_get_value(self.ema(ema_slow), idx) and
                # Pullback to VWAP (within 0.5%)
                abs(safe_get_value(self.close, idx) - safe_get_value(self.vwap(), idx)) / safe_get_value(self.vwap(), idx) < 0.005 and
                # Closed above VWAP (recovery)
                safe_get_value(self.close, idx) > safe_get_value(self.vwap(), idx) and
                # ADX in optimal range
                adx_min <= safe_get_value(self.adx(14), idx) <= adx_max
            ),
        }

        return conditions
    
    def get_available_indicators(self) -> List[str]:
        """
        Get list of all available indicators
        
        Returns:
            List of indicator names
        """
        return [
            'rsi', 'connors_rsi', 'stochastic_rsi', 'macd', 'stochastic',
            'bollinger_bands', 'bb_position', 'bb_width', 'williams_r', 'cci',
            'atr', 'atr_percent', 'parabolic_sar', 'adx', 'aroon',
            'sma', 'ema', 'obv', 'volume_sma', 'volume_ratio', 'vwap', 'vwap_deviation',
            'momentum', 'roc'
        ]
    
    def get_available_conditions(self) -> List[str]:
        """
        Get list of all available condition names
        
        Returns:
            List of condition names
        """
        return list(self.get_conditions().keys())
    
    def clear_cache(self):
        """Clear the indicator cache"""
        self._cache.clear()

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def create_indicator_library(df: pd.DataFrame) -> IndicatorLibrary:
    """
    Convenience function to create an IndicatorLibrary instance
    
    Args:
        df: DataFrame with OHLCV data
        
    Returns:
        IndicatorLibrary instance
    """
    return IndicatorLibrary(df)

def get_default_parameters() -> Dict[str, Dict[str, Any]]:
    """
    Get default parameters for all indicators
    
    Returns:
        Dictionary mapping indicator names to their default parameters
    """
    return {
        'rsi': {'period': 14},
        'connors_rsi': {'rsi_period': 3, 'streak_period': 2, 'rank_period': 100},
        'stochastic_rsi': {'period': 14, 'fastk_period': 5, 'fastd_period': 3},
        'macd': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9},
        'stochastic': {'fastk_period': 5, 'slowk_period': 3, 'slowd_period': 3},
        'bollinger_bands': {'period': 20, 'std_dev': 2.0},
        'williams_r': {'period': 14},
        'cci': {'period': 14},
        'atr': {'period': 14},
        'parabolic_sar': {'acceleration': 0.02, 'maximum': 0.2},
        'adx': {'period': 14},
        'aroon': {'period': 14},
        'sma': {'period': 20},
        'ema': {'period': 20},
        'volume_sma': {'period': 20},
        'momentum': {'period': 10},
        'roc': {'period': 10}
    }

# Example usage and testing
if __name__ == "__main__":
    # Example usage
    print("Technical Indicators Library")
    print("=" * 50)
    
    # Create sample data
    import numpy as np
    dates = pd.date_range('2023-01-01', periods=100, freq='D')
    np.random.seed(42)
    
    sample_data = pd.DataFrame({
        'openPrice': 100 + np.cumsum(np.random.randn(100) * 0.5),
        'highPrice': 100 + np.cumsum(np.random.randn(100) * 0.5) + np.random.rand(100) * 2,
        'lowPrice': 100 + np.cumsum(np.random.randn(100) * 0.5) - np.random.rand(100) * 2,
        'closePrice': 100 + np.cumsum(np.random.randn(100) * 0.5),
        'lastTradedVolume': np.random.randint(1000, 10000, 100)
    })
    
    # Initialize library
    lib = IndicatorLibrary(sample_data)
    
    # Test indicators
    print(f"Available indicators: {len(lib.get_available_indicators())}")
    print(f"Available conditions: {len(lib.get_available_conditions())}")
    
    # Calculate some indicators
    rsi_14 = lib.rsi(period=14)
    rsi_7 = lib.rsi(period=7)
    macd_line, macd_signal, macd_hist = lib.macd()
    bb_upper, bb_middle, bb_lower = lib.bollinger_bands()
    
    print(f"\\nRSI-14 last value: {rsi_14[-1]:.2f}")
    print(f"RSI-7 last value: {rsi_7[-1]:.2f}")
    print(f"MACD last value: {macd_line[-1]:.4f}")
    print(f"BB position last value: {lib.bb_position()[-1]:.4f}")
    
    # Test conditions
    conditions = lib.get_conditions()
    idx = len(sample_data) - 1
    
    print(f"\\nCondition tests at index {idx}:")
    print(f"RSI oversold: {conditions['rsi_oversold'](sample_data, idx)}")
    print(f"MACD positive: {conditions['macd_positive'](sample_data, idx)}")
    print(f"BB oversold: {conditions['bb_oversold'](sample_data, idx)}")
    
    print("\\n✅ Library test completed successfully!")
