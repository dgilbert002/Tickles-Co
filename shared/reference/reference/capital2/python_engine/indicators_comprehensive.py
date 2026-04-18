"""
COMPREHENSIVE TECHNICAL INDICATORS LIBRARY - 162+ Indicators
Dynamic indicator calculations with categorization and direction tagging

This library provides a unified interface for 162+ technical indicators
organized by direction (bullish/bearish/neutral) and category (trend/momentum/volatility/volume/crash_protection).

Usage:
    lib = create_indicator_library()
    conditions = lib.get_conditions()
    
    # Get all bullish indicators
    bullish = lib.get_by_direction('bullish')
    
    # Get all momentum indicators
    momentum = lib.get_by_category('momentum')
    
    # Test a specific indicator
    signal = conditions['rsi_oversold'](df, idx, period=14, threshold=30)
"""

import pandas as pd
import numpy as np
import talib
from typing import Dict, Callable, Any, List, Tuple

# Import the full IndicatorLibrary class from legacy reference
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# We'll use the legacy IndicatorLibrary class as-is since it has all the calculation methods
# Just need to add the categorization metadata on top

INDICATOR_METADATA = {
    # RSI Indicators (Momentum, Bullish/Bearish)
    'rsi_oversold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'RSI below threshold (oversold condition)',
        'params': {'period': 14, 'threshold': 30},
        'param_ranges': {
            'period': [7, 10, 14, 20, 21],
            'threshold': list(range(20, 36))
        }
    },
    'rsi_overbought': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'RSI above threshold (overbought condition)',
        'params': {'period': 14, 'threshold': 70},
        'param_ranges': {
            'period': [7, 10, 14, 20, 21],
            'threshold': list(range(65, 81))
        }
    },
    'rsi_bullish_cross_50': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'RSI crosses above 50',
        'params': {'period': 14, 'cross_level': 50},
        'param_ranges': {
            'period': [7, 10, 14, 20, 21],
            'cross_level': [40, 45, 50, 55, 60]
        }
    },
    'rsi_bearish_cross_50': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'RSI crosses below 50',
        'params': {'period': 14, 'cross_level': 50},
        'param_ranges': {
            'period': [7, 10, 14, 20, 21],
            'cross_level': [40, 45, 50, 55, 60]
        }
    },
    
    # ConnorsRSI Indicators
    'connors_rsi_oversold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'ConnorsRSI oversold condition',
        'params': {'rsi_period': 3, 'streak_period': 2, 'rank_period': 100, 'threshold': 25},
        'param_ranges': {
            'rsi_period': [3, 5, 10],
            'streak_period': [2, 3, 5],
            'rank_period': [50, 100, 200],
            'threshold': [10, 20, 25, 30]
        }
    },
    'connors_rsi_very_oversold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'ConnorsRSI very oversold condition',
        'params': {'rsi_period': 3, 'streak_period': 2, 'rank_period': 100, 'threshold': 20},
        'param_ranges': {
            'rsi_period': [3, 5, 10],
            'streak_period': [2, 3, 5],
            'rank_period': [50, 100, 200],
            'threshold': [5, 10, 15, 20]
        }
    },
    'connors_rsi_overbought': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'ConnorsRSI overbought condition',
        'params': {'rsi_period': 3, 'streak_period': 2, 'rank_period': 100, 'threshold': 75},
        'param_ranges': {
            'rsi_period': [3, 5, 10],
            'streak_period': [2, 3, 5],
            'rank_period': [50, 100, 200],
            'threshold': [70, 75, 80, 90]
        }
    },
    'connors_rsi_bullish_cross': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'ConnorsRSI crosses above 50',
        'params': {'rsi_period': 3, 'streak_period': 2, 'rank_period': 100, 'cross_level': 50},
        'param_ranges': {
            'rsi_period': [3, 5, 10],
            'streak_period': [2, 3, 5],
            'rank_period': [50, 100, 200],
            'cross_level': [40, 45, 50, 55, 60]
        }
    },
    
    # MACD Indicators
    'macd_positive': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'MACD line above threshold',
        'params': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9, 'threshold': 0},
        'param_ranges': {
            'fast_period': [8, 10, 12, 14, 16],
            'slow_period': [20, 24, 26, 30, 34],
            'signal_period': [6, 9, 12],
            'threshold': [-0.5, -0.2, 0, 0.2, 0.5]
        }
    },
    'macd_negative': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'MACD line below threshold',
        'params': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9, 'threshold': 0},
        'param_ranges': {
            'fast_period': [8, 10, 12, 14, 16],
            'slow_period': [20, 24, 26, 30, 34],
            'signal_period': [6, 9, 12],
            'threshold': [-0.5, -0.2, 0, 0.2, 0.5]
        }
    },
    'macd_bullish_cross': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'MACD line crosses above signal line',
        'params': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9},
        'param_ranges': {
            'fast_period': [8, 10, 12, 14, 16],
            'slow_period': [20, 24, 26, 30, 34],
            'signal_period': [6, 9, 12]
        }
    },
    'macd_bearish_cross': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'MACD line crosses below signal line',
        'params': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9},
        'param_ranges': {
            'fast_period': [8, 10, 12, 14, 16],
            'slow_period': [20, 24, 26, 30, 34],
            'signal_period': [6, 9, 12]
        }
    },
    'macd_histogram_positive': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'MACD histogram above threshold',
        'params': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9, 'threshold': 0},
        'param_ranges': {
            'fast_period': [8, 10, 12, 14, 16],
            'slow_period': [20, 24, 26, 30, 34],
            'signal_period': [6, 9, 12],
            'threshold': [-0.2, -0.1, 0, 0.1, 0.2]
        }
    },
    'macd_histogram_negative': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'MACD histogram below threshold',
        'params': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9, 'threshold': 0},
        'param_ranges': {
            'fast_period': [8, 10, 12, 14, 16],
            'slow_period': [20, 24, 26, 30, 34],
            'signal_period': [6, 9, 12],
            'threshold': [-0.2, -0.1, 0, 0.1, 0.2]
        }
    },
    'macd_histogram_increasing': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'MACD histogram increasing',
        'params': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9},
        'param_ranges': {
            'fast_period': [8, 10, 12, 14, 16],
            'slow_period': [20, 24, 26, 30, 34],
            'signal_period': [6, 9, 12]
        }
    },
    
    # Bollinger Bands Indicators
    'bb_oversold': {
        'direction': 'bullish',
        'category': 'volatility',
        'description': 'Price in lower portion of Bollinger Bands',
        'params': {'period': 20, 'std_dev': 2.0, 'threshold': 0.2},
        'param_ranges': {
            'period': [14, 20, 21, 25],
            'std_dev': [1.5, 2.0, 2.5, 3.0],
            'threshold': [0.1, 0.15, 0.2, 0.25, 0.3]
        }
    },
    'bb_very_oversold': {
        'direction': 'bullish',
        'category': 'volatility',
        'description': 'Price very close to lower Bollinger Band',
        'params': {'period': 20, 'std_dev': 2.0, 'threshold': 0.1},
        'param_ranges': {
            'period': [14, 20, 21, 25],
            'std_dev': [1.5, 2.0, 2.5, 3.0],
            'threshold': [0.05, 0.08, 0.1, 0.12, 0.15]
        }
    },
    'bb_overbought': {
        'direction': 'bearish',
        'category': 'volatility',
        'description': 'Price in upper portion of Bollinger Bands',
        'params': {'period': 20, 'std_dev': 2.0, 'threshold': 0.8},
        'param_ranges': {
            'period': [14, 20, 21, 25],
            'std_dev': [1.5, 2.0, 2.5, 3.0],
            'threshold': [0.7, 0.75, 0.8, 0.85, 0.9]
        }
    },
    'bb_very_overbought': {
        'direction': 'bearish',
        'category': 'volatility',
        'description': 'Price very close to upper Bollinger Band',
        'params': {'period': 20, 'std_dev': 2.0, 'threshold': 0.9},
        'param_ranges': {
            'period': [14, 20, 21, 25],
            'std_dev': [1.5, 2.0, 2.5, 3.0],
            'threshold': [0.85, 0.88, 0.9, 0.92, 0.95]
        }
    },
    'bb_squeeze': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'Bollinger Bands squeeze (low volatility)',
        'params': {'period': 20, 'std_dev': 2.0, 'threshold': 0.02},
        'param_ranges': {
            'period': [14, 20, 21, 25],
            'std_dev': [1.5, 2.0, 2.5, 3.0],
            'threshold': [0.01, 0.015, 0.02, 0.025, 0.03]
        }
    },
    'bb_expansion': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'Bollinger Bands expansion (high volatility)',
        'params': {'period': 20, 'std_dev': 2.0, 'threshold': 0.05},
        'param_ranges': {
            'period': [14, 20, 21, 25],
            'std_dev': [1.5, 2.0, 2.5, 3.0],
            'threshold': [0.03, 0.04, 0.05, 0.06, 0.08]
        }
    },
    'bb_upper_break': {
        'direction': 'bullish',
        'category': 'volatility',
        'description': 'Price breaks above upper Bollinger Band',
        'params': {'period': 20, 'std_dev': 2.0},
        'param_ranges': {
            'period': [14, 20, 21, 25],
            'std_dev': [1.5, 2.0, 2.5, 3.0]
        }
    },
    'bb_lower_break': {
        'direction': 'bullish',
        'category': 'volatility',
        'description': 'Price breaks below lower Bollinger Band',
        'params': {'period': 20, 'std_dev': 2.0},
        'param_ranges': {
            'period': [14, 20, 21, 25],
            'std_dev': [1.5, 2.0, 2.5, 3.0]
        }
    },
    
    # Stochastic Indicators
    'stoch_oversold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Stochastic oscillator oversold',
        'params': {'fastk_period': 5, 'slowk_period': 3, 'slowd_period': 3, 'threshold': 20},
        'param_ranges': {
            'fastk_period': [5, 7, 9, 14],
            'slowk_period': [3, 5],
            'slowd_period': [3, 5],
            'threshold': [10, 15, 20, 25]
        }
    },
    'stoch_overbought': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Stochastic oscillator overbought',
        'params': {'fastk_period': 5, 'slowk_period': 3, 'slowd_period': 3, 'threshold': 80},
        'param_ranges': {
            'fastk_period': [5, 7, 9, 14],
            'slowk_period': [3, 5],
            'slowd_period': [3, 5],
            'threshold': [75, 80, 85, 90]
        }
    },
    'stoch_rsi_oversold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Stochastic RSI oversold',
        'params': {'period': 14, 'fastk_period': 5, 'fastd_period': 3, 'threshold': 0.2},
        'param_ranges': {
            'period': [14, 21],
            'fastk_period': [3, 5, 7],
            'fastd_period': [3, 5, 7],
            'threshold': [0.1, 0.15, 0.2, 0.25]
        }
    },
    'stoch_rsi_overbought': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Stochastic RSI overbought',
        'params': {'period': 14, 'fastk_period': 5, 'fastd_period': 3, 'threshold': 0.8},
        'param_ranges': {
            'period': [14, 21],
            'fastk_period': [3, 5, 7],
            'fastd_period': [3, 5, 7],
            'threshold': [0.75, 0.8, 0.85, 0.9]
        }
    },
    'stoch_bullish_cross': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Stochastic %K crosses above %D',
        'params': {'fastk_period': 5, 'slowk_period': 3, 'slowd_period': 3},
        'param_ranges': {
            'fastk_period': [5, 7, 9, 14],
            'slowk_period': [3, 5],
            'slowd_period': [3, 5]
        }
    },
    'stoch_bearish_cross': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Stochastic %K crosses below %D',
        'params': {'fastk_period': 5, 'slowk_period': 3, 'slowd_period': 3},
        'param_ranges': {
            'fastk_period': [5, 7, 9, 14],
            'slowk_period': [3, 5],
            'slowd_period': [3, 5]
        }
    },
    
    # Williams %R Indicators
    'willr_oversold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Williams %R oversold',
        'params': {'period': 14, 'threshold': -80},
        'param_ranges': {
            'period': [10, 14, 20],
            'threshold': [-90, -85, -80, -75]
        }
    },
    'willr_very_oversold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Williams %R very oversold',
        'params': {'period': 14, 'threshold': -90},
        'param_ranges': {
            'period': [10, 14, 20],
            'threshold': [-95, -90, -85]
        }
    },
    'willr_overbought': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Williams %R overbought',
        'params': {'period': 14, 'threshold': -20},
        'param_ranges': {
            'period': [10, 14, 20],
            'threshold': [-25, -20, -15, -10]
        }
    },
    'willr_very_overbought': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Williams %R very overbought',
        'params': {'period': 14, 'threshold': -10},
        'param_ranges': {
            'period': [10, 14, 20],
            'threshold': [-15, -10, -5]
        }
    },
    
    # CCI Indicators
    'cci_oversold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'CCI oversold',
        'params': {'period': 14, 'threshold': -100},
        'param_ranges': {
            'period': [14, 20, 30],
            'threshold': [-150, -100, -75]
        }
    },
    'cci_very_oversold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'CCI very oversold',
        'params': {'period': 14, 'threshold': -200},
        'param_ranges': {
            'period': [14, 20, 30],
            'threshold': [-250, -200, -150]
        }
    },
    'cci_overbought': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'CCI overbought',
        'params': {'period': 14, 'threshold': 100},
        'param_ranges': {
            'period': [14, 20, 30],
            'threshold': [75, 100, 150]
        }
    },
    'cci_very_overbought': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'CCI very overbought',
        'params': {'period': 14, 'threshold': 200},
        'param_ranges': {
            'period': [14, 20, 30],
            'threshold': [150, 200, 250]
        }
    },
    'cci_bullish_cross_zero': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'CCI crosses above zero',
        'params': {'period': 14, 'cross_level': 0},
        'param_ranges': {
            'period': [14, 20, 30],
            'cross_level': [-20, -10, 0, 10, 20]
        }
    },
    'cci_bearish_cross_zero': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'CCI crosses below zero',
        'params': {'period': 14, 'cross_level': 0},
        'param_ranges': {
            'period': [14, 20, 30],
            'cross_level': [-20, -10, 0, 10, 20]
        }
    },
    
    # VWAP Indicators
    'price_above_vwap': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'Price above VWAP',
        'params': {'threshold': 0.01},
        'param_ranges': {
            'threshold': [0.0, 0.005, 0.01, 0.02]
        }
    },
    'price_below_vwap': {
        'direction': 'bearish',
        'category': 'volume',
        'description': 'Price below VWAP',
        'params': {'threshold': -0.01},
        'param_ranges': {
            'threshold': [-0.02, -0.01, -0.005, 0.0]
        }
    },
    'price_near_vwap': {
        'direction': 'neutral',
        'category': 'volume',
        'description': 'Price near VWAP',
        'params': {'threshold': 0.005},
        'param_ranges': {
            'threshold': [0.002, 0.005, 0.01]
        }
    },
    'vwap_bullish_cross': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'Price crosses above VWAP with confirmation bars staying above',
        'params': {'confirmation_bars': 1},
        'param_ranges': {
            'confirmation_bars': [1, 2, 3]
        }
    },
    'vwap_bearish_cross': {
        'direction': 'bearish',
        'category': 'volume',
        'description': 'Price crosses below VWAP with confirmation bars staying below',
        'params': {'confirmation_bars': 1},
        'param_ranges': {
            'confirmation_bars': [1, 2, 3]
        }
    },
    
    # Momentum Indicators
    'momentum_positive': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Momentum above threshold',
        'params': {'period': 10, 'threshold': 0},
        'param_ranges': {
            'period': [5, 10, 14, 20],
            'threshold': [0, 0.5, 1.0]
        }
    },
    'momentum_negative': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Momentum below threshold',
        'params': {'period': 10, 'threshold': 0},
        'param_ranges': {
            'period': [5, 10, 14, 20],
            'threshold': [-1.0, -0.5, 0]
        }
    },
    'momentum_increasing': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Momentum increasing',
        'params': {'period': 10},
        'param_ranges': {
            'period': [5, 10, 14, 20]
        }
    },
    'momentum_decreasing': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Momentum decreasing',
        'params': {'period': 10},
        'param_ranges': {
            'period': [5, 10, 14, 20]
        }
    },
    
    # ROC Indicators
    'roc_positive': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Rate of Change positive',
        'params': {'period': 10, 'threshold': 0},
        'param_ranges': {
            'period': [5, 10, 14, 20],
            'threshold': [0, 0.5, 1.0, 2.0]
        }
    },
    'roc_negative': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Rate of Change negative',
        'params': {'period': 10, 'threshold': 0},
        'param_ranges': {
            'period': [5, 10, 14, 20],
            'threshold': [-2.0, -1.0, -0.5, 0]
        }
    },
    'roc_above_threshold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'ROC above threshold',
        'params': {'period': 10, 'threshold': 1.0},
        'param_ranges': {
            'period': [5, 10, 14, 20],
            'threshold': [0.5, 1.0, 2.0, 3.0]
        }
    },
    'roc_below_threshold': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'ROC below threshold',
        'params': {'period': 10, 'threshold': -1.0},
        'param_ranges': {
            'period': [5, 10, 14, 20],
            'threshold': [-3.0, -2.0, -1.0, -0.5]
        }
    },
    
    # ADX Indicators
    'adx_trending': {
        'direction': 'neutral',
        'category': 'trend',
        'description': 'ADX indicates trending market',
        'params': {'period': 14, 'threshold': 25},
        'param_ranges': {
            'period': [10, 14, 20],
            'threshold': [20, 25, 30]
        }
    },
    'adx_strong_trend': {
        'direction': 'neutral',
        'category': 'trend',
        'description': 'ADX indicates strong trend',
        'params': {'period': 14, 'threshold': 40},
        'param_ranges': {
            'period': [10, 14, 20],
            'threshold': [35, 40, 45, 50]
        }
    },
    'adx_weak_trend': {
        'direction': 'neutral',
        'category': 'trend',
        'description': 'ADX indicates weak trend',
        'params': {'period': 14, 'threshold': 20},
        'param_ranges': {
            'period': [10, 14, 20],
            'threshold': [15, 20, 25]
        }
    },
    'adx_increasing': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'ADX increasing (strengthening trend)',
        'params': {'period': 14},
        'param_ranges': {
            'period': [10, 14, 20]
        }
    },
    'adx_decreasing': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'ADX decreasing (weakening trend)',
        'params': {'period': 14},
        'param_ranges': {
            'period': [10, 14, 20]
        }
    },
    
    # Aroon Indicators
    'aroon_bullish': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Aroon Up > Aroon Down',
        'params': {'period': 14},
        'param_ranges': {
            'period': [10, 14, 20, 25]
        }
    },
    'aroon_bearish': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'Aroon Down > Aroon Up',
        'params': {'period': 14},
        'param_ranges': {
            'period': [10, 14, 20, 25]
        }
    },
    'aroon_up_strong': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Aroon Up above threshold',
        'params': {'period': 14, 'threshold': 70},
        'param_ranges': {
            'period': [10, 14, 20, 25],
            'threshold': [60, 70, 80, 90]
        }
    },
    'aroon_down_strong': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'Aroon Down above threshold',
        'params': {'period': 14, 'threshold': 70},
        'param_ranges': {
            'period': [10, 14, 20, 25],
            'threshold': [60, 70, 80, 90]
        }
    },
    'aroon_consolidation': {
        'direction': 'neutral',
        'category': 'trend',
        'description': 'Aroon indicates consolidation',
        'params': {'period': 14, 'threshold': 50},
        'param_ranges': {
            'period': [10, 14, 20, 25],
            'threshold': [40, 50, 60]
        }
    },
    
    # ATR Indicators
    'atr_high_volatility': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'ATR indicates high volatility',
        'params': {'period': 14, 'percentile_period': 50, 'percentile_threshold': 0.8},
        'param_ranges': {
            'period': [10, 14, 20],
            'percentile_period': [20, 50, 100, 200],
            'percentile_threshold': [0.7, 0.8, 0.9]
        }
    },
    'atr_low_volatility': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'ATR indicates low volatility',
        'params': {'period': 14, 'percentile_period': 50, 'percentile_threshold': 0.2},
        'param_ranges': {
            'period': [10, 14, 20],
            'percentile_period': [20, 50, 100, 200],
            'percentile_threshold': [0.1, 0.2, 0.3]
        }
    },
    'atr_above_threshold': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'ATR above absolute threshold',
        'params': {'period': 14, 'threshold': 2.0},
        'param_ranges': {
            'period': [10, 14, 20],
            'threshold': [1.0, 1.5, 2.0, 2.5, 3.0]
        }
    },
    'atr_below_threshold': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'ATR below absolute threshold',
        'params': {'period': 14, 'threshold': 1.0},
        'param_ranges': {
            'period': [10, 14, 20],
            'threshold': [0.5, 1.0, 1.5, 2.0]
        }
    },
    
    # Parabolic SAR Indicators
    'sar_bullish': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'SAR below price (bullish)',
        'params': {'acceleration': 0.02, 'maximum': 0.2},
        'param_ranges': {
            'acceleration': [0.02, 0.03, 0.04],
            'maximum': [0.2, 0.25, 0.3]
        }
    },
    'sar_bearish': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'SAR above price (bearish)',
        'params': {'acceleration': 0.02, 'maximum': 0.2},
        'param_ranges': {
            'acceleration': [0.02, 0.03, 0.04],
            'maximum': [0.2, 0.25, 0.3]
        }
    },
    'sar_bullish_cross': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'SAR flips to bullish',
        'params': {'acceleration': 0.02, 'maximum': 0.2},
        'param_ranges': {
            'acceleration': [0.02, 0.03, 0.04],
            'maximum': [0.2, 0.25, 0.3]
        }
    },
    'sar_bearish_cross': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'SAR flips to bearish',
        'params': {'acceleration': 0.02, 'maximum': 0.2},
        'param_ranges': {
            'acceleration': [0.02, 0.03, 0.04],
            'maximum': [0.2, 0.25, 0.3]
        }
    },
    
    # Price vs MA Indicators
    'price_above_sma': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Price above SMA',
        'params': {'period': 20},
        'param_ranges': {
            'period': [10, 20, 50, 100, 200]
        }
    },
    'price_below_sma': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'Price below SMA',
        'params': {'period': 20},
        'param_ranges': {
            'period': [10, 20, 50, 100, 200]
        }
    },
    'price_above_ema': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Price above EMA',
        'params': {'period': 20},
        'param_ranges': {
            'period': [10, 12, 20, 26, 50, 100, 200]
        }
    },
    'price_below_ema': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'Price below EMA',
        'params': {'period': 20},
        'param_ranges': {
            'period': [10, 12, 20, 26, 50, 100, 200]
        }
    },
    
    # MA Crossover Indicators
    'sma_bullish_cross': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Fast SMA crosses above slow SMA',
        'params': {'fast_period': 10, 'slow_period': 20},
        'param_ranges': {
            'fast_period': [5, 10, 20, 50],
            'slow_period': [20, 50, 100, 200]
        }
    },
    'sma_bearish_cross': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'Fast SMA crosses below slow SMA',
        'params': {'fast_period': 10, 'slow_period': 20},
        'param_ranges': {
            'fast_period': [5, 10, 20, 50],
            'slow_period': [20, 50, 100, 200]
        }
    },
    'ema_bullish_cross': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Fast EMA crosses above slow EMA',
        'params': {'fast_period': 12, 'slow_period': 26},
        'param_ranges': {
            'fast_period': [8, 10, 12, 20, 50],
            'slow_period': [20, 26, 50, 100, 200]
        }
    },
    'ema_bearish_cross': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'Fast EMA crosses below slow EMA',
        'params': {'fast_period': 12, 'slow_period': 26},
        'param_ranges': {
            'fast_period': [8, 10, 12, 20, 50],
            'slow_period': [20, 26, 50, 100, 200]
        }
    },
    'ema12_above_ema26': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'EMA12 above EMA26',
        'params': {'fast_period': 12, 'slow_period': 26},
        'param_ranges': {
            'fast_period': [8, 10, 12, 15],
            'slow_period': [20, 24, 26, 30]
        }
    },
    'ema12_below_ema26': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'EMA12 below EMA26',
        'params': {'fast_period': 12, 'slow_period': 26},
        'param_ranges': {
            'fast_period': [8, 10, 12, 15],
            'slow_period': [20, 24, 26, 30]
        }
    },
    'sma10_above_sma20': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'SMA10 above SMA20',
        'params': {'fast_period': 10, 'slow_period': 20},
        'param_ranges': {
            'fast_period': [5, 10, 15],
            'slow_period': [20, 25, 30]
        }
    },
    'sma10_below_sma20': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'SMA10 below SMA20',
        'params': {'fast_period': 10, 'slow_period': 20},
        'param_ranges': {
            'fast_period': [5, 10, 15],
            'slow_period': [20, 25, 30]
        }
    },
    
    # Volume Indicators
    'volume_spike': {
        'direction': 'neutral',
        'category': 'volume',
        'description': 'Volume spike detected',
        'params': {'period': 20, 'threshold': 2.0},
        'param_ranges': {
            'period': [10, 20, 30],
            'threshold': [1.5, 2.0, 2.5, 3.0]
        }
    },
    'volume_high': {
        'direction': 'neutral',
        'category': 'volume',
        'description': 'High volume',
        'params': {'period': 20, 'threshold': 1.5},
        'param_ranges': {
            'period': [10, 20, 30],
            'threshold': [1.2, 1.5, 1.8, 2.0]
        }
    },
    'volume_low': {
        'direction': 'neutral',
        'category': 'volume',
        'description': 'Low volume',
        'params': {'period': 20, 'threshold': 0.5},
        'param_ranges': {
            'period': [10, 20, 30],
            'threshold': [0.3, 0.5, 0.7, 0.8]
        }
    },
    'volume_above_sma': {
        'direction': 'neutral',
        'category': 'volume',
        'description': 'Volume above SMA',
        'params': {'period': 20, 'threshold': 1.0},
        'param_ranges': {
            'period': [10, 20, 30],
            'threshold': [1.0, 1.2, 1.5]
        }
    },
    'volume_below_sma': {
        'direction': 'neutral',
        'category': 'volume',
        'description': 'Volume below SMA',
        'params': {'period': 20, 'threshold': 1.0},
        'param_ranges': {
            'period': [10, 20, 30],
            'threshold': [0.5, 0.8, 1.0]
        }
    },
    
    # OBV Indicators
    'obv_increasing': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'OBV increasing - On-Balance Volume above its SMA and increasing over lookback period',
        'params': {'lookback_period': 20, 'obv_sma_period': 20},
        'param_ranges': {
            'lookback_period': [10, 15, 20, 25, 30],
            'obv_sma_period': [10, 15, 20, 25, 30]
        }
    },
    'obv_decreasing': {
        'direction': 'bearish',
        'category': 'volume',
        'description': 'OBV decreasing - On-Balance Volume below its SMA and decreasing over lookback period',
        'params': {'lookback_period': 20, 'obv_sma_period': 20},
        'param_ranges': {
            'lookback_period': [10, 15, 20, 25, 30],
            'obv_sma_period': [10, 15, 20, 25, 30]
        }
    },
    'obv_divergence_bullish': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'Bullish OBV divergence',
        'params': {'lookback': 5},
        'param_ranges': {
            'lookback': [3, 5, 7, 10]
        }
    },
    'obv_divergence_bearish': {
        'direction': 'bearish',
        'category': 'volume',
        'description': 'Bearish OBV divergence',
        'params': {'lookback': 5},
        'param_ranges': {
            'lookback': [3, 5, 7, 10]
        }
    },
    
    # Supertrend Indicators
    'supertrend_bullish': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Supertrend bullish',
        'params': {'period': 10, 'multiplier': 3.0},
        'param_ranges': {
            'period': [7, 10, 14, 20],
            'multiplier': [1.5, 2.0, 2.5, 3.0, 3.5]
        }
    },
    'supertrend_bearish': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'Supertrend bearish',
        'params': {'period': 10, 'multiplier': 3.0},
        'param_ranges': {
            'period': [7, 10, 14, 20],
            'multiplier': [1.5, 2.0, 2.5, 3.0, 3.5]
        }
    },
    'supertrend_bullish_cross': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Supertrend flips to bullish',
        'params': {'period': 10, 'multiplier': 3.0},
        'param_ranges': {
            'period': [7, 10, 14, 20],
            'multiplier': [1.5, 2.0, 2.5, 3.0, 3.5]
        }
    },
    'supertrend_bearish_cross': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'Supertrend flips to bearish',
        'params': {'period': 10, 'multiplier': 3.0},
        'param_ranges': {
            'period': [7, 10, 14, 20],
            'multiplier': [1.5, 2.0, 2.5, 3.0, 3.5]
        }
    },
    
    # Keltner Channel Indicators
    'keltner_upper_break': {
        'direction': 'bullish',
        'category': 'volatility',
        'description': 'Price breaks above upper Keltner Channel',
        'params': {'period': 20, 'multiplier': 2.0},
        'param_ranges': {
            'period': [10, 15, 20, 25],
            'multiplier': [1.5, 2.0, 2.5, 3.0]
        }
    },
    'keltner_lower_break': {
        'direction': 'bullish',
        'category': 'volatility',
        'description': 'Price breaks below lower Keltner Channel',
        'params': {'period': 20, 'multiplier': 2.0},
        'param_ranges': {
            'period': [10, 15, 20, 25],
            'multiplier': [1.5, 2.0, 2.5, 3.0]
        }
    },
    'keltner_squeeze': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'Keltner Channel squeeze',
        'params': {'period': 20, 'multiplier': 2.0},
        'param_ranges': {
            'period': [10, 15, 20, 25],
            'multiplier': [1.5, 2.0, 2.5, 3.0]
        }
    },
    
    # TTM Squeeze Indicators
    'ttm_squeeze_on': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'TTM Squeeze active (consolidation)',
        'params': {'bb_period': 20, 'bb_std': 2.0, 'kc_period': 20, 'kc_mult': 1.5},
        'param_ranges': {
            'bb_period': [15, 20, 25],
            'bb_std': [1.5, 2.0, 2.5],
            'kc_period': [15, 20, 25],
            'kc_mult': [1.0, 1.5, 2.0]
        }
    },
    'ttm_squeeze_off': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'TTM Squeeze released (breakout)',
        'params': {'bb_period': 20, 'bb_std': 2.0, 'kc_period': 20, 'kc_mult': 1.5},
        'param_ranges': {
            'bb_period': [15, 20, 25],
            'bb_std': [1.5, 2.0, 2.5],
            'kc_period': [15, 20, 25],
            'kc_mult': [1.0, 1.5, 2.0]
        }
    },
    
    # Fisher Transform Indicators
    'fisher_buy_signal': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Fisher Transform buy signal',
        'params': {'period': 10, 'threshold': -1.0},
        'param_ranges': {
            'period': [5, 7, 10, 14],
            'threshold': [-1.5, -1.0, -0.5]
        }
    },
    'fisher_sell_signal': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Fisher Transform sell signal',
        'params': {'period': 10, 'threshold': 1.0},
        'param_ranges': {
            'period': [5, 7, 10, 14],
            'threshold': [0.5, 1.0, 1.5]
        }
    },
    'fisher_extreme_oversold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Fisher Transform extreme oversold',
        'params': {'period': 10, 'threshold': -2.0},
        'param_ranges': {
            'period': [5, 7, 10, 14],
            'threshold': [-3.0, -2.5, -2.0]
        }
    },
    'fisher_extreme_overbought': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Fisher Transform extreme overbought',
        'params': {'period': 10, 'threshold': 2.0},
        'param_ranges': {
            'period': [5, 7, 10, 14],
            'threshold': [2.0, 2.5, 3.0]
        }
    },
    
    # CMF (Chaikin Money Flow) Indicators
    'cmf_positive': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'CMF positive (buying pressure)',
        'params': {'period': 20, 'threshold': 0},
        'param_ranges': {
            'period': [10, 14, 20, 21],
            'threshold': [0, 0.05, 0.1]
        }
    },
    'cmf_negative': {
        'direction': 'bearish',
        'category': 'volume',
        'description': 'CMF negative (selling pressure)',
        'params': {'period': 20, 'threshold': 0},
        'param_ranges': {
            'period': [10, 14, 20, 21],
            'threshold': [-0.1, -0.05, 0]
        }
    },
    'cmf_bullish_divergence': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'CMF bullish divergence',
        'params': {'period': 20, 'lookback': 10},
        'param_ranges': {
            'period': [10, 14, 20, 21],
            'lookback': [5, 10, 15]
        }
    },
    
    # Donchian Channel Indicators
    'donchian_upper_break': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Price breaks above Donchian upper',
        'params': {'period': 20},
        'param_ranges': {
            'period': [10, 20, 30, 50]
        }
    },
    'donchian_lower_break': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Price breaks below Donchian lower',
        'params': {'period': 20},
        'param_ranges': {
            'period': [10, 20, 30, 50]
        }
    },
    'donchian_middle_cross_up': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Price crosses above Donchian middle',
        'params': {'period': 20},
        'param_ranges': {
            'period': [10, 20, 30, 50]
        }
    },
    'donchian_middle_cross_down': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'Price crosses below Donchian middle',
        'params': {'period': 20},
        'param_ranges': {
            'period': [10, 20, 30, 50]
        }
    },
    
    # TRIX Indicators
    'trix_positive': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'TRIX above zero',
        'params': {'period': 14, 'threshold': 0},
        'param_ranges': {
            'period': [9, 12, 14, 18],
            'threshold': [0, 0.01, 0.02]
        }
    },
    'trix_negative': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'TRIX below zero',
        'params': {'period': 14, 'threshold': 0},
        'param_ranges': {
            'period': [9, 12, 14, 18],
            'threshold': [-0.02, -0.01, 0]
        }
    },
    'trix_bullish_cross': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'TRIX crosses above zero',
        'params': {'period': 14},
        'param_ranges': {
            'period': [9, 12, 14, 18]
        }
    },
    'trix_bearish_cross': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'TRIX crosses below zero',
        'params': {'period': 14},
        'param_ranges': {
            'period': [9, 12, 14, 18]
        }
    },
    
    # TSI (True Strength Index) Indicators
    'tsi_positive': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'TSI above zero',
        'params': {'fast_period': 13, 'slow_period': 25, 'threshold': 0},
        'param_ranges': {
            'fast_period': [8, 10, 13, 15],
            'slow_period': [20, 25, 30, 35],
            'threshold': [0, 5, 10]
        }
    },
    'tsi_negative': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'TSI below zero',
        'params': {'fast_period': 13, 'slow_period': 25, 'threshold': 0},
        'param_ranges': {
            'fast_period': [8, 10, 13, 15],
            'slow_period': [20, 25, 30, 35],
            'threshold': [-10, -5, 0]
        }
    },
    'tsi_oversold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'TSI oversold',
        'params': {'fast_period': 13, 'slow_period': 25, 'threshold': -25},
        'param_ranges': {
            'fast_period': [8, 10, 13, 15],
            'slow_period': [20, 25, 30, 35],
            'threshold': [-30, -25, -20]
        }
    },
    'tsi_overbought': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'TSI overbought',
        'params': {'fast_period': 13, 'slow_period': 25, 'threshold': 25},
        'param_ranges': {
            'fast_period': [8, 10, 13, 15],
            'slow_period': [20, 25, 30, 35],
            'threshold': [20, 25, 30]
        }
    },
    
    # Ulcer Index (Crash Protection)
    'ulcer_low_risk': {
        'direction': 'bullish',
        'category': 'crash_protection',
        'description': 'Ulcer Index low (low downside risk)',
        'params': {'period': 14, 'threshold': 5},
        'param_ranges': {
            'period': [7, 10, 14, 20],
            'threshold': [3, 5, 7]
        }
    },
    'ulcer_high_risk': {
        'direction': 'bearish',
        'category': 'crash_protection',
        'description': 'Ulcer Index high (high downside risk)',
        'params': {'period': 14, 'threshold': 10},
        'param_ranges': {
            'period': [7, 10, 14, 20],
            'threshold': [8, 10, 12]
        }
    },
    'ulcer_extreme_risk': {
        'direction': 'bearish',
        'category': 'crash_protection',
        'description': 'Ulcer Index extreme (extreme risk)',
        'params': {'period': 14, 'threshold': 15},
        'param_ranges': {
            'period': [7, 10, 14, 20],
            'threshold': [12, 15, 20]
        }
    },
    'ulcer_decreasing': {
        'direction': 'bullish',
        'category': 'crash_protection',
        'description': 'Ulcer Index decreasing (risk reducing)',
        'params': {'period': 14},
        'param_ranges': {
            'period': [7, 10, 14, 20]
        }
    },
    
    # Drawdown Indicators (Crash Protection)
    'drawdown_shallow': {
        'direction': 'bullish',
        'category': 'crash_protection',
        'description': 'Shallow drawdown detected',
        'params': {'period': 20, 'threshold': -5},
        'param_ranges': {
            'period': [10, 20, 30, 50],
            'threshold': [-3, -5, -7]
        }
    },
    'drawdown_moderate': {
        'direction': 'neutral',
        'category': 'crash_protection',
        'description': 'Moderate drawdown detected',
        'params': {'period': 20, 'threshold': -10},
        'param_ranges': {
            'period': [10, 20, 30, 50],
            'threshold': [-8, -10, -12]
        }
    },
    'drawdown_deep': {
        'direction': 'bearish',
        'category': 'crash_protection',
        'description': 'Deep drawdown detected',
        'params': {'period': 20, 'threshold': -20},
        'param_ranges': {
            'period': [10, 20, 30, 50],
            'threshold': [-15, -20, -25]
        }
    },
    'drawdown_recovery': {
        'direction': 'bullish',
        'category': 'crash_protection',
        'description': 'Recovering from drawdown',
        'params': {'period': 20, 'recovery_threshold': 0.5},
        'param_ranges': {
            'period': [10, 20, 30, 50],
            'recovery_threshold': [0.3, 0.5, 0.7]
        }
    },
    
    # Coppock Curve (Crash Protection/Recovery)
    'coppock_buy_signal': {
        'direction': 'bullish',
        'category': 'crash_protection',
        'description': 'Coppock Curve buy signal',
        'params': {'roc1_period': 14, 'roc2_period': 11, 'wma_period': 10},
        'param_ranges': {
            'roc1_period': [11, 14, 17],
            'roc2_period': [8, 11, 14],
            'wma_period': [8, 10, 12]
        }
    },
    'coppock_positive': {
        'direction': 'bullish',
        'category': 'crash_protection',
        'description': 'Coppock Curve above zero',
        'params': {'roc1_period': 14, 'roc2_period': 11, 'wma_period': 10, 'threshold': 0},
        'param_ranges': {
            'roc1_period': [11, 14, 17],
            'roc2_period': [8, 11, 14],
            'wma_period': [8, 10, 12],
            'threshold': [-2, -1, 0, 1, 2]
        }
    },
    'coppock_turning_up': {
        'direction': 'bullish',
        'category': 'crash_protection',
        'description': 'Coppock Curve turning up',
        'params': {'roc1_period': 14, 'roc2_period': 11, 'wma_period': 10},
        'param_ranges': {
            'roc1_period': [11, 14, 17],
            'roc2_period': [8, 11, 14],
            'wma_period': [8, 10, 12]
        }
    },
    
    # Volatility Stop (Crash Protection)
    'volatility_stop_long': {
        'direction': 'bullish',
        'category': 'crash_protection',
        'description': 'Volatility stop signals long',
        'params': {'period': 20, 'multiplier': 2.5},
        'param_ranges': {
            'period': [10, 15, 20, 25],
            'multiplier': [2.0, 2.5, 3.0, 3.5]
        }
    },
    'volatility_stop_short': {
        'direction': 'bearish',
        'category': 'crash_protection',
        'description': 'Volatility stop signals short',
        'params': {'period': 20, 'multiplier': 2.5},
        'param_ranges': {
            'period': [10, 15, 20, 25],
            'multiplier': [2.0, 2.5, 3.0, 3.5]
        }
    },
    
    # Market Regime Detection (Crash Protection)
    'regime_trending_bullish': {
        'direction': 'bullish',
        'category': 'crash_protection',
        'description': 'Market regime: trending bullish',
        'params': {'adx_period': 14, 'adx_threshold': 25},
        'param_ranges': {
            'adx_period': [10, 14, 20],
            'adx_threshold': [20, 25, 30]
        }
    },
    'regime_trending_bearish': {
        'direction': 'bearish',
        'category': 'crash_protection',
        'description': 'Market regime: trending bearish',
        'params': {'adx_period': 14, 'adx_threshold': 25},
        'param_ranges': {
            'adx_period': [10, 14, 20],
            'adx_threshold': [20, 25, 30]
        }
    },
    'regime_ranging': {
        'direction': 'neutral',
        'category': 'crash_protection',
        'description': 'Market regime: ranging',
        'params': {'adx_period': 14, 'adx_threshold': 25},
        'param_ranges': {
            'adx_period': [10, 14, 20],
            'adx_threshold': [15, 20, 25]
        }
    },
    
    # A/D Line (Accumulation/Distribution)
    'ad_accumulation': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'Accumulation detected',
        'params': {'ma_period': 20},
        'param_ranges': {
            'ma_period': [10, 20, 30]
        }
    },
    'ad_distribution': {
        'direction': 'bearish',
        'category': 'volume',
        'description': 'Distribution detected',
        'params': {'ma_period': 20},
        'param_ranges': {
            'ma_period': [10, 20, 30]
        }
    },
    'ad_divergence_bullish': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'Bullish A/D divergence',
        'params': {'lookback': 10},
        'param_ranges': {
            'lookback': [5, 10, 15, 20]
        }
    },
    'ad_divergence_bearish': {
        'direction': 'bearish',
        'category': 'volume',
        'description': 'Bearish A/D divergence',
        'params': {'lookback': 10},
        'param_ranges': {
            'lookback': [5, 10, 15, 20]
        }
    },
    
    # Inverse Fisher RSI
    'iftrsi_buy_signal': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Inverse Fisher RSI buy signal',
        'params': {'period': 5, 'threshold': -0.5},
        'param_ranges': {
            'period': [3, 5, 7, 9],
            'threshold': [-0.7, -0.5, -0.3]
        }
    },
    'iftrsi_sell_signal': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Inverse Fisher RSI sell signal',
        'params': {'period': 5, 'threshold': 0.5},
        'param_ranges': {
            'period': [3, 5, 7, 9],
            'threshold': [0.3, 0.5, 0.7]
        }
    },
    'iftrsi_extreme_oversold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Inverse Fisher RSI extreme oversold',
        'params': {'period': 5, 'threshold': -0.9},
        'param_ranges': {
            'period': [3, 5, 7, 9],
            'threshold': [-0.95, -0.9, -0.85]
        }
    },
    'iftrsi_extreme_overbought': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Inverse Fisher RSI extreme overbought',
        'params': {'period': 5, 'threshold': 0.9},
        'param_ranges': {
            'period': [3, 5, 7, 9],
            'threshold': [0.85, 0.9, 0.95]
        }
    },
    
    # VWMACD (Volume-Weighted MACD)
    'vwmacd_bullish_cross': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'Volume-weighted MACD bullish cross',
        'params': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9},
        'param_ranges': {
            'fast_period': [8, 10, 12, 15],
            'slow_period': [20, 24, 26, 30],
            'signal_period': [7, 9, 11]
        }
    },
    'vwmacd_bearish_cross': {
        'direction': 'bearish',
        'category': 'volume',
        'description': 'Volume-weighted MACD bearish cross',
        'params': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9},
        'param_ranges': {
            'fast_period': [8, 10, 12, 15],
            'slow_period': [20, 24, 26, 30],
            'signal_period': [7, 9, 11]
        }
    },
    'vwmacd_positive': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'Volume-weighted MACD positive',
        'params': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9, 'threshold': 0},
        'param_ranges': {
            'fast_period': [8, 10, 12, 15],
            'slow_period': [20, 24, 26, 30],
            'signal_period': [7, 9, 11],
            'threshold': [-0.5, -0.2, 0, 0.2, 0.5]
        }
    },
    'vwmacd_histogram_positive': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'Volume-weighted MACD histogram positive',
        'params': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9, 'threshold': 0},
        'param_ranges': {
            'fast_period': [8, 10, 12, 15],
            'slow_period': [20, 24, 26, 30],
            'signal_period': [7, 9, 11],
            'threshold': [-0.2, -0.1, 0, 0.1, 0.2]
        }
    },
    
    # Combination/Advanced Indicators
    'crash_shield_active': {
        'direction': 'bearish',
        'category': 'crash_protection',
        'description': 'Crash protection shield activated',
        'params': {'ulcer_threshold': 10, 'dd_threshold': -15, 'lookback': 5},
        'param_ranges': {
            'ulcer_threshold': [8, 10, 12, 15],
            'dd_threshold': [-10, -15, -20],
            'lookback': [3, 5, 7]
        }
    },
    'bottom_hunter_signal': {
        'direction': 'bullish',
        'category': 'combination',
        'description': 'Bottom hunter signal (multi-indicator)',
        # NOTE: Lambda in indicators.py accepts rsi_threshold and volume_spike (not bb_threshold or volume_threshold)
        'params': {'rsi_threshold': 30, 'volume_spike': 1.5},
        'param_ranges': {
            'rsi_threshold': [25, 30, 35],
            'volume_spike': [1.2, 1.5, 2.0]
        }
    },
    'anchored_vwap_pullback_long': {
        'direction': 'bullish',
        'category': 'combination',
        'description': 'Anchored VWAP pullback long signal - uptrend filter with VWAP pullback',
        'params': {'pullback': 0.01, 'ema_fast': 50, 'ema_slow': 200},
        'param_ranges': {
            'pullback': [0.005, 0.01, 0.015, 0.02],
            'ema_fast': [20, 50],
            'ema_slow': [100, 200]
        }
    },
    'squeeze_retest_go': {
        'direction': 'bullish',
        'category': 'combination',
        'description': 'Squeeze retest go signal - low-vol squeeze, breakout above BB, then successful retest',
        'params': {'period': 20, 'std_dev': 2.0, 'lookback': 30, 'retest_tolerance': 0.005},
        'param_ranges': {
            'period': [15, 20, 25],
            'std_dev': [1.5, 2.0, 2.5],
            'lookback': [20, 30, 40],
            'retest_tolerance': [0.003, 0.005, 0.01]
        }
    },
    'smart_entry_signal': {
        'direction': 'bullish',
        'category': 'combination',
        'description': 'Smart entry signal - MACD golden cross with RSI and ADX confirmation',
        'params': {'rsi_period': 14, 'rsi_threshold': 50, 'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9, 'adx_period': 14, 'adx_threshold': 25},
        'param_ranges': {
            'rsi_period': [7, 10, 14, 21],
            'rsi_threshold': [40, 45, 50, 55, 60],
            'macd_fast': [6, 8, 10, 12, 15],
            'macd_slow': [18, 22, 26, 30, 35],
            'macd_signal': [5, 7, 9, 12],
            'adx_period': [10, 14, 20],
            'adx_threshold': [20, 25, 30]
        }
    },
    
    # VWAP Deviation
    'vwap_deviation': {
        'direction': 'neutral',
        'category': 'volume',
        'description': 'Price deviation from VWAP',
        'params': {'threshold': 0.02},
        'param_ranges': {
            'threshold': [0.01, 0.02, 0.03, 0.05]
        }
    },
    
    # ============================================================================
    # NEW TIER-1 INDICATORS (From Manus Analysis - December 2025)
    # ============================================================================
    
    # TTM Squeeze Momentum (Pre-breakout detection)
    'ttm_squeeze_momentum_bullish': {
        'direction': 'bullish',
        'category': 'volatility',
        'description': 'TTM Squeeze releases with positive momentum - pre-breakout signal',
        'params': {'bb_length': 20, 'bb_mult': 2.0, 'kc_length': 20, 'kc_mult': 1.5, 'momentum_length': 12, 'momentum_threshold': 0.0},
        'param_ranges': {
            'bb_length': [15, 20, 25],
            'bb_mult': [1.5, 2.0, 2.5],
            'kc_length': [15, 20, 25],
            'kc_mult': [1.0, 1.5, 2.0],
            'momentum_length': [8, 12, 16],
            'momentum_threshold': [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
        }
    },
    'ttm_squeeze_momentum_bearish': {
        'direction': 'bearish',
        'category': 'volatility',
        'description': 'TTM Squeeze releases with negative momentum - pre-breakdown signal',
        'params': {'bb_length': 20, 'bb_mult': 2.0, 'kc_length': 20, 'kc_mult': 1.5, 'momentum_length': 12, 'momentum_threshold': 0.0},
        'param_ranges': {
            'bb_length': [15, 20, 25],
            'bb_mult': [1.5, 2.0, 2.5],
            'kc_length': [15, 20, 25],
            'kc_mult': [1.0, 1.5, 2.0],
            'momentum_length': [8, 12, 16],
            'momentum_threshold': [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
        }
    },
    'ttm_squeeze_on': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'TTM Squeeze is ON - low volatility consolidation (potential breakout setup)',
        'params': {'bb_length': 20, 'bb_mult': 2.0, 'kc_length': 20, 'kc_mult': 1.5},
        'param_ranges': {
            'bb_length': [15, 20, 25],
            'bb_mult': [1.5, 2.0, 2.5],
            'kc_length': [15, 20, 25],
            'kc_mult': [1.0, 1.5, 2.0]
        }
    },
    
    # RSI Hidden Divergence (Bottom/Top hunting)
    'rsi_hidden_bullish_divergence': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'RSI Hidden Bullish Divergence - price higher low, RSI lower low (trend continuation)',
        'params': {'rsi_period': 14, 'lookback': 20, 'min_rsi': 30, 'max_rsi': 70},
        'param_ranges': {
            'rsi_period': [9, 10, 11, 12, 13, 14],
            'lookback': [15, 20, 25],
            'min_rsi': [25, 30, 35],
            'max_rsi': [65, 70, 75]
        }
    },
    'rsi_hidden_bearish_divergence': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'RSI Hidden Bearish Divergence - price lower high, RSI higher high (trend continuation)',
        'params': {'rsi_period': 14, 'lookback': 20, 'min_rsi': 30, 'max_rsi': 70},
        'param_ranges': {
            'rsi_period': [9, 10, 11, 12, 13, 14],
            'lookback': [15, 20, 25],
            'min_rsi': [25, 30, 35],
            'max_rsi': [65, 70, 75]
        }
    },
    
    # Volume Accumulation Divergence (Pre-breakout detection)
    'volume_accumulation_bullish': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'Volume increasing while price flat - accumulation before breakout',
        'params': {'volume_period': 14, 'price_period': 14, 'min_strength': 5.0},
        'param_ranges': {
            'volume_period': [10, 15, 20],
            'price_period': [10, 15, 20],
            'min_strength': [3.0, 4.0, 5.0, 6.0, 7.0]
        }
    },
    'volume_accumulation_trend': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'Volume accumulation with EMA 20/50 trend confirmation',
        'params': {'volume_period': 14, 'price_period': 14, 'min_strength': 3.0},
        'param_ranges': {
            'volume_period': [10, 15, 20],
            'price_period': [10, 15, 20],
            'min_strength': [2.0, 3.0, 4.0, 5.0]
        }
    },
    
    # ============================================================================
    # NEW TIER-2 INDICATORS (From Manus Analysis - December 2025)
    # ============================================================================
    
    # EMA 8/21 Cross (Crypto Standard)
    'ema_8_21_bullish_cross': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'EMA 8 crosses above EMA 21 - crypto standard fast trend signal',
        'params': {'confirmation': 1},
        'param_ranges': {
            'confirmation': [1, 2, 3]
        }
    },
    'ema_8_21_bearish_cross': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'EMA 8 crosses below EMA 21 - crypto standard fast trend signal',
        'params': {'confirmation': 1},
        'param_ranges': {
            'confirmation': [1, 2, 3]
        }
    },
    
    # Choppiness Index
    'choppiness_trending': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'Choppiness Index below threshold - market is trending (safe to trade)',
        'params': {'period': 14, 'choppy_threshold': 61.8},
        'param_ranges': {
            'period': [10, 14, 20],
            'choppy_threshold': [55, 60, 61.8, 65]
        }
    },
    
    # VWAP Bounce
    'vwap_bounce_bullish': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'Price touches VWAP and bounces up - institutional support',
        'params': {'distance_threshold': 0.002, 'bounce_confirmation': 1},
        'param_ranges': {
            'distance_threshold': [0.001, 0.002, 0.003, 0.004, 0.005],
            'bounce_confirmation': [1, 2, 3]
        }
    },
    'vwap_bounce_bearish': {
        'direction': 'bearish',
        'category': 'volume',
        'description': 'Price touches VWAP and bounces down - institutional resistance',
        'params': {'distance_threshold': 0.002, 'bounce_confirmation': 1},
        'param_ranges': {
            'distance_threshold': [0.001, 0.002, 0.003, 0.004, 0.005],
            'bounce_confirmation': [1, 2, 3]
        }
    },
    
    # ============================================================================
    # ZERO-LAG AROON OSCILLATOR (BigBeluga-style with Ehlers smoothing)
    # ============================================================================
    'aroon_oscillator_zerolag_strong': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Zero-lag Aroon Oscillator above threshold (strong trend confirmation)',
        'params': {'period': 29, 'smooth': 25, 'threshold': 50, 'gain_limit': 10, 'mode': 'strong'},
        'param_ranges': {
            'period': [14, 20, 25, 29, 35],
            'smooth': [10, 15, 20, 25, 30],
            'threshold': [40, 50, 60, 70],
            'gain_limit': [5, 10, 15],
            'mode': ['strong']
        }
    },
    'aroon_oscillator_zerolag_bullish': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Zero-lag Aroon Oscillator is positive (simple bullish bias)',
        'params': {'period': 29, 'smooth': 25, 'gain_limit': 10, 'mode': 'bullish'},
        'param_ranges': {
            'period': [14, 20, 25, 29, 35],
            'smooth': [10, 15, 20, 25, 30],
            'gain_limit': [5, 10, 15],
            'mode': ['bullish']
        }
    },
    'aroon_oscillator_zerolag_trend': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Zero-lag Aroon Oscillator crosses above zero (trend change signal)',
        'params': {'period': 29, 'smooth': 25, 'gain_limit': 10, 'mode': 'trend'},
        'param_ranges': {
            'period': [14, 20, 25, 29, 35],
            'smooth': [10, 15, 20, 25, 30],
            'gain_limit': [5, 10, 15],
            'mode': ['trend']
        }
    },
    'aroon_oscillator_zerolag_reversion': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Zero-lag Aroon Oscillator crosses above signal line (mean reversion)',
        'params': {'period': 29, 'smooth': 25, 'signal_len': 10, 'gain_limit': 10, 'mode': 'reversion'},
        'param_ranges': {
            'period': [14, 20, 25, 29, 35],
            'smooth': [10, 15, 20, 25, 30],
            'signal_len': [5, 10, 15],
            'gain_limit': [5, 10, 15],
            'mode': ['reversion']
        }
    },
    
    # ============================================================================
    # MULTI-MA CROSSOVER (Flexible MA type crossover signals)
    # ============================================================================
    'multi_ma_bullish_cross': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Flexible multi-moving average bullish crossover (fast MA crosses above slow)',
        'params': {'fast_ma_type': 'EMA', 'fast_ma_period': 50, 'slow_ma_type': 'EMA', 'slow_ma_period': 200, 'source': 'close'},
        'param_ranges': {
            'fast_ma_type': ['EMA', 'SMA', 'VWMA'],
            'fast_ma_period': [10, 20, 50, 100],
            'slow_ma_type': ['EMA', 'SMA', 'VWMA'],
            'slow_ma_period': [50, 100, 200],
            'source': ['close']
        }
    },
    'multi_ma_golden_cross': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Classic Golden Cross - 50 SMA crosses above 200 SMA',
        'params': {'fast_ma_type': 'SMA', 'fast_ma_period': 50, 'slow_ma_type': 'SMA', 'slow_ma_period': 200, 'source': 'close'},
        'param_ranges': {
            'fast_ma_type': ['SMA'],
            'fast_ma_period': [50],
            'slow_ma_type': ['SMA'],
            'slow_ma_period': [200],
            'source': ['close']
        }
    },
    'multi_ma_ema_cross': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'EMA crossover - configurable fast/slow periods',
        'params': {'fast_ma_type': 'EMA', 'fast_ma_period': 20, 'slow_ma_type': 'EMA', 'slow_ma_period': 50, 'source': 'close'},
        'param_ranges': {
            'fast_ma_type': ['EMA'],
            'fast_ma_period': [9, 12, 20, 26],
            'slow_ma_type': ['EMA'],
            'slow_ma_period': [26, 50, 100],
            'source': ['close']
        }
    },
    
    # ============================================================================
    # LETF STRATEGY INDICATORS (SOXL/TECL Optimized)
    # Based on Composer and academic research for leveraged ETF trading
    # ============================================================================
    
    # Intraday Return Indicators
    'intraday_return_bullish': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Intraday return from prior close exceeds threshold (Rebalancing Front-Run strategy)',
        'params': {'threshold': 6.0},
        'param_ranges': {
            'threshold': [4.0, 5.0, 6.0, 7.0, 8.0]
        }
    },
    'intraday_return_bearish': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Intraday return from prior close below negative threshold',
        'params': {'threshold': -6.0},
        'param_ranges': {
            'threshold': [-8.0, -7.0, -6.0, -5.0, -4.0]
        }
    },
    
    # Daily Spike Detectors
    'daily_spike_up': {
        'direction': 'bearish',  # Bearish because it signals to go defensive/exit
        'category': 'momentum',
        'description': 'Large daily UP move detected - Composer strategy flips defensive after >8.5% spike',
        'params': {'threshold': 8.5},
        'param_ranges': {
            'threshold': [6.0, 7.0, 8.0, 8.5, 9.0, 10.0]
        }
    },
    'daily_spike_down': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'Large daily DOWN move detected - defensive signal',
        'params': {'threshold': 2.0},
        'param_ranges': {
            'threshold': [1.5, 2.0, 2.5, 3.0, 4.0]
        }
    },
    
    # MA Band Position Indicators
    'ma_band_bullish': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Price above both short and long MAs - safe to enter long',
        'params': {'short_period': 20, 'long_period': 90},
        'param_ranges': {
            'short_period': [10, 15, 20, 25, 30],
            'long_period': [50, 70, 90, 100, 120]
        }
    },
    'ma_band_defensive': {
        'direction': 'bearish',  # Bearish because it signals NOT to enter
        'category': 'trend',
        'description': 'Price between short and long MAs - Composer defensive zone',
        'params': {'short_period': 20, 'long_period': 90},
        'param_ranges': {
            'short_period': [10, 15, 20, 25, 30],
            'long_period': [50, 70, 90, 100, 120]
        }
    },
    
    # Multi-Day Momentum
    'multi_day_momentum_bullish': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'N-day momentum exceeds threshold - tactical momentum signal',
        'params': {'days': 5, 'threshold': 5.0},
        'param_ranges': {
            'days': [3, 5, 7, 10],
            'threshold': [3.0, 4.0, 5.0, 6.0, 7.0]
        }
    },
    
    # RSI Extreme Levels
    'rsi_extreme_oversold': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'RSI at extreme oversold levels - strong mean reversion signal',
        'params': {'period': 10, 'threshold': 15.0},
        'param_ranges': {
            'period': [7, 10, 14],
            'threshold': [10.0, 12.0, 15.0, 18.0, 20.0]
        }
    },
    'rsi_extreme_overbought': {
        'direction': 'bearish',
        'category': 'momentum',
        'description': 'RSI at extreme overbought levels - avoid new entries',
        'params': {'period': 10, 'threshold': 90.0},
        'param_ranges': {
            'period': [7, 10, 14],
            'threshold': [85.0, 88.0, 90.0, 92.0, 95.0]
        }
    },
    
    # Composite Strategy Indicators
    'composer_rsi_strategy': {
        'direction': 'bullish',
        'category': 'combination',
        'description': 'Composer RSI Strategy for LETFs (117,356% return!) - RSI oversold + defensive filters',
        'params': {
            'rsi_period': 10, 
            'rsi_oversold': 29.0, 
            'rsi_overbought': 80.0, 
            'ma_short': 20, 
            'ma_long': 90, 
            'spike_down_threshold': 2.0
        },
        'param_ranges': {
            'rsi_period': [7, 10, 14],
            'rsi_oversold': [25.0, 27.0, 29.0, 30.0, 32.0],
            'rsi_overbought': [75.0, 78.0, 80.0, 82.0, 85.0],
            'ma_short': [15, 20, 25],
            'ma_long': [70, 80, 90, 100],
            'spike_down_threshold': [1.5, 2.0, 2.5, 3.0]
        }
    },
    'aroon_strong_uptrend': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'Aroon Strong Uptrend (90% win rate!) - AroonUp>70 AND AroonDown<30',
        'params': {'period': 25, 'up_threshold': 70.0, 'down_threshold': 30.0},
        'param_ranges': {
            'period': [14, 20, 25, 29],
            'up_threshold': [65.0, 70.0, 75.0, 80.0],
            'down_threshold': [25.0, 30.0, 35.0]
        }
    },
    'rebalancing_frontrun_bullish': {
        'direction': 'bullish',
        'category': 'momentum',
        'description': 'Rebalancing Front-Run Strategy (Sharpe 1.95, CAGR 31%) - Buy on large intraday return',
        'params': {'threshold': 6.0},
        'param_ranges': {
            'threshold': [4.0, 5.0, 6.0, 7.0, 8.0]
        }
    },
    'trend_with_sma_filter': {
        'direction': 'bullish',
        'category': 'trend',
        'description': '200 SMA trend filter with buffer zones - buy when price sufficiently above SMA',
        'params': {'sma_period': 200, 'buffer_above': 5.0, 'buffer_below': 3.0},
        'param_ranges': {
            'sma_period': [100, 150, 200],
            'buffer_above': [3.0, 4.0, 5.0, 6.0],
            'buffer_below': [2.0, 3.0, 4.0]
        }
    },
    
    # ============================================================================
    # 5-MINUTE SIGNAL ENGINES (SOXL/TECL Intraday Optimized)
    # Grid-searchable parameters with min/max/step for optimization
    # ============================================================================
    
    # 1. VWAP Reclaim/Lose Signals
    'vwap_reclaim_bullish': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'VWAP Reclaim - N consecutive closes above VWAP after being below',
        'params': {'confirmation_bars': 2, 'distance_pct': 0.05},
        'param_ranges': {
            'confirmation_bars': [1, 2, 3, 4, 5, 6],
            'distance_pct': [0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5]
        }
    },
    'vwap_lose_bearish': {
        'direction': 'bearish',
        'category': 'volume',
        'description': 'VWAP Lose - N consecutive closes below VWAP after being above',
        'params': {'confirmation_bars': 2, 'distance_pct': 0.05},
        'param_ranges': {
            'confirmation_bars': [1, 2, 3, 4, 5, 6],
            'distance_pct': [0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5]
        }
    },
    
    # 2. EMA Pullback Bounce
    'ema_pullback_bounce_bullish': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'EMA Pullback Bounce - Price dips below fast EMA then bounces in uptrend',
        'params': {'ema_fast': 9, 'ema_slow': 200, 'pullback_pct': 0.5, 'bounce_bars': 2},
        'param_ranges': {
            'ema_fast': [5, 8, 9, 12, 15, 21],
            'ema_slow': [100, 150, 200, 250],
            'pullback_pct': [0.25, 0.5, 0.75, 1.0, 1.25, 1.5],
            'bounce_bars': [1, 2, 3, 4, 5]
        }
    },
    
    # 3. Opening Range Breakout (ORB)
    'opening_range_breakout_bullish': {
        'direction': 'bullish',
        'category': 'breakout',
        'description': 'ORB Bullish - Price breaks above N-bar range high with buffer',
        'params': {'range_bars': 3, 'breakout_buffer_pct': 0.1},
        'param_ranges': {
            'range_bars': [1, 2, 3, 4, 5, 6],  # 5-30 min on 5m candles
            'breakout_buffer_pct': [0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5]
        }
    },
    'opening_range_breakout_bearish': {
        'direction': 'bearish',
        'category': 'breakout',
        'description': 'ORB Bearish - Price breaks below N-bar range low with buffer',
        'params': {'range_bars': 3, 'breakout_buffer_pct': 0.1},
        'param_ranges': {
            'range_bars': [1, 2, 3, 4, 5, 6],
            'breakout_buffer_pct': [0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5]
        }
    },
    
    # 4. Bollinger Bandwidth Squeeze/Expansion
    'bb_bandwidth_squeeze': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'BB Bandwidth Squeeze - Bollinger Bands width below threshold (pre-breakout)',
        'params': {'bb_period': 20, 'bb_std': 2.0, 'bandwidth_threshold_pct': 3.0},
        'param_ranges': {
            'bb_period': [14, 20, 25, 30, 40, 50],
            'bb_std': [1.5, 2.0, 2.5, 3.0, 3.5],
            'bandwidth_threshold_pct': [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
        }
    },
    'bb_bandwidth_expansion_bullish': {
        'direction': 'bullish',
        'category': 'breakout',
        'description': 'BB Squeeze Breakout - Squeeze released with upward breakout above upper band',
        'params': {'bb_period': 20, 'bb_std': 2.0, 'bandwidth_threshold_pct': 3.0, 'confirm_bars': 2},
        'param_ranges': {
            'bb_period': [14, 20, 25, 30],
            'bb_std': [1.5, 2.0, 2.5],
            'bandwidth_threshold_pct': [2.0, 3.0, 4.0],
            'confirm_bars': [1, 2, 3, 4, 5]
        }
    },
    
    # 5. RSI + MACD Combination Signals
    'rsi_macd_combo_bullish': {
        'direction': 'bullish',
        'category': 'combination',
        'description': 'RSI + MACD Combo Bullish - Both RSI above threshold and MACD bullish',
        'params': {'rsi_len': 14, 'rsi_threshold': 50, 'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9},
        'param_ranges': {
            'rsi_len': [5, 7, 10, 14, 21],
            'rsi_threshold': [45, 48, 50, 52, 55, 60],
            'macd_fast': [6, 8, 10, 12, 15],
            'macd_slow': [18, 22, 26, 30, 35],
            'macd_signal': [5, 7, 9, 12]
        }
    },
    'rsi_macd_combo_bearish': {
        'direction': 'bearish',
        'category': 'combination',
        'description': 'RSI + MACD Combo Bearish - Both RSI below threshold and MACD bearish',
        'params': {'rsi_len': 14, 'rsi_threshold': 50, 'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9},
        'param_ranges': {
            'rsi_len': [5, 7, 10, 14, 21],
            'rsi_threshold': [40, 45, 48, 50, 52, 55],
            'macd_fast': [6, 8, 10, 12, 15],
            'macd_slow': [18, 22, 26, 30, 35],
            'macd_signal': [5, 7, 9, 12]
        }
    },
    'rsi_dipbuy_macd_bullish': {
        'direction': 'bullish',
        'category': 'combination',
        'description': 'RSI Dip-Buy with MACD - Mean reversion: RSI was oversold, now recovering with MACD confirmation',
        'params': {'rsi_len': 14, 'rsi_oversold': 30, 'macd_fast': 12, 'macd_slow': 26, 'macd_signal': 9},
        'param_ranges': {
            'rsi_len': [5, 7, 10, 14, 21],
            'rsi_oversold': [20, 25, 30, 35, 40],
            'macd_fast': [6, 8, 10, 12, 15],
            'macd_slow': [18, 22, 26, 30, 35],
            'macd_signal': [5, 7, 9, 12]
        }
    },
    
    # 6. Supertrend + Swing Break
    'supertrend_swing_break_bullish': {
        'direction': 'bullish',
        'category': 'breakout',
        'description': 'Supertrend Flip + Swing Break - Supertrend turns bullish AND price breaks swing high',
        'params': {'st_atr_len': 10, 'st_mult': 3.0, 'swing_lookback': 10},
        'param_ranges': {
            'st_atr_len': [7, 10, 14, 20, 28],
            'st_mult': [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
            'swing_lookback': [3, 5, 7, 10, 15, 20]
        }
    },
    'supertrend_swing_break_bearish': {
        'direction': 'bearish',
        'category': 'breakout',
        'description': 'Supertrend Flip + Swing Break - Supertrend turns bearish AND price breaks swing low',
        'params': {'st_atr_len': 10, 'st_mult': 3.0, 'swing_lookback': 10},
        'param_ranges': {
            'st_atr_len': [7, 10, 14, 20, 28],
            'st_mult': [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
            'swing_lookback': [3, 5, 7, 10, 15, 20]
        }
    },
    
    # 7. ATR Volatility Signals
    'atr_volatility_rising': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'ATR Rising - Volatility expanding (good for trend trades)',
        'params': {'atr_len': 14, 'atr_sma_len': 20, 'expansion_threshold': 1.2},
        'param_ranges': {
            'atr_len': [7, 10, 14, 20, 28],
            'atr_sma_len': [10, 15, 20, 30, 50],
            'expansion_threshold': [1.1, 1.2, 1.3, 1.5, 1.8, 2.0]
        }
    },
    'atr_volatility_contracting': {
        'direction': 'neutral',
        'category': 'volatility',
        'description': 'ATR Contracting - Low volatility (squeeze setup)',
        'params': {'atr_len': 14, 'atr_sma_len': 20, 'contraction_threshold': 0.8},
        'param_ranges': {
            'atr_len': [7, 10, 14, 20, 28],
            'atr_sma_len': [10, 15, 20, 30, 50],
            'contraction_threshold': [0.5, 0.6, 0.7, 0.8, 0.9]
        }
    },
    
    # 8. SOXL Trend Surge Strategy (Full Combined)
    'soxl_trend_surge_bullish': {
        'direction': 'bullish',
        'category': 'combination',
        'description': 'SOXL Trend Surge - Full combined strategy: EMA trend + Supertrend + Volume + ATR rising',
        'params': {'ema_len': 200, 'st_factor': 3.0, 'st_atr': 10, 'vol_len': 20, 'ema_buffer_pct': 0.5},
        'param_ranges': {
            'ema_len': [100, 150, 200, 250],
            'st_factor': [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
            'st_atr': [7, 10, 14, 20],
            'vol_len': [10, 15, 20, 30, 50],
            'ema_buffer_pct': [0.0, 0.25, 0.5, 0.75, 1.0]
        }
    },
    'volume_spike_trend_bullish': {
        'direction': 'bullish',
        'category': 'volume',
        'description': 'Volume Spike + Trend - High volume spike with price above EMA on green candle',
        'params': {'vol_sma_len': 20, 'vol_ratio_threshold': 1.5, 'ema_len': 50},
        'param_ranges': {
            'vol_sma_len': [10, 15, 20, 30, 50],
            'vol_ratio_threshold': [1.2, 1.5, 2.0, 2.5, 3.0],
            'ema_len': [20, 50, 100, 200]
        }
    },

    # ============================================================================
    # SMART MONEY CONCEPTS (SMC) INDICATORS
    # ============================================================================

    'smc_order_block_bullish': {
        'direction': 'bullish',
        'category': 'smart_money',
        'description': 'SMC Order Block Bullish - Last bearish candle before strong up move (institutional buying zone)',
        'params': {'lookback': 20, 'min_move_pct': 1.0},
        'param_ranges': {
            'lookback': [10, 15, 20, 30, 50],
            'min_move_pct': [0.5, 0.75, 1.0, 1.5, 2.0]
        }
    },
    'smc_order_block_bearish': {
        'direction': 'bearish',
        'category': 'smart_money',
        'description': 'SMC Order Block Bearish - Strong down move detected (signals when move happens)',
        'params': {'lookback': 20, 'min_move_pct': 1.0},
        'param_ranges': {
            'lookback': [10, 15, 20, 30, 50],
            'min_move_pct': [0.5, 0.75, 1.0, 1.5, 2.0]
        }
    },
    # NEW: Order Block Zone Revisit indicators (No Look-Ahead Bias!)
    # These implement PROPER SMC trading: wait for price to RETURN to the zone
    'smc_ob_zone_revisit_bullish': {
        'direction': 'bullish',
        'category': 'smart_money',
        'description': 'SMC Zone Revisit Bullish - Price returns to DEMAND zone after strong up move (BUY at support)',
        'params': {'lookback': 20, 'min_move_pct': 1.0, 'zone_valid_bars': 50, 'min_wait_bars': 3},
        'param_ranges': {
            'lookback': [10, 14, 20, 30],
            'min_move_pct': [0.75, 1.0, 1.5, 2.0],
            'zone_valid_bars': [30, 50, 75, 100],
            'min_wait_bars': [2, 3, 5, 10]
        }
    },
    'smc_ob_zone_revisit_bearish': {
        'direction': 'bearish',
        'category': 'smart_money',
        'description': 'SMC Zone Revisit Bearish - Price returns to SUPPLY zone after strong down move (SELL at resistance)',
        'params': {'lookback': 20, 'min_move_pct': 1.0, 'zone_valid_bars': 50, 'min_wait_bars': 3},
        'param_ranges': {
            'lookback': [10, 14, 20, 30],
            'min_move_pct': [0.75, 1.0, 1.5, 2.0],
            'zone_valid_bars': [30, 50, 75, 100],
            'min_wait_bars': [2, 3, 5, 10]
        }
    },
    'smc_fvg_bullish': {
        'direction': 'bullish',
        'category': 'smart_money',
        'description': 'SMC Fair Value Gap Bullish - Imbalance gap up indicating institutional buying',
        'params': {'min_gap_pct': 0.1},
        'param_ranges': {
            'min_gap_pct': [0.05, 0.1, 0.15, 0.2, 0.3]
        }
    },
    'smc_fvg_bearish': {
        'direction': 'bearish',
        'category': 'smart_money',
        'description': 'SMC Fair Value Gap Bearish - Imbalance gap down indicating institutional selling',
        'params': {'min_gap_pct': 0.1},
        'param_ranges': {
            'min_gap_pct': [0.05, 0.1, 0.15, 0.2, 0.3]
        }
    },
    'smc_liquidity_sweep_bullish': {
        'direction': 'bullish',
        'category': 'smart_money',
        'description': 'SMC Liquidity Sweep Bullish - Price sweeps below swing low then reverses (stop hunt)',
        'params': {'swing_lookback': 10, 'confirmation_bars': 2},
        'param_ranges': {
            'swing_lookback': [5, 7, 10, 15, 20],
            'confirmation_bars': [1, 2, 3, 4]
        }
    },
    'smc_liquidity_sweep_bearish': {
        'direction': 'bearish',
        'category': 'smart_money',
        'description': 'SMC Liquidity Sweep Bearish - Price sweeps above swing high then reverses (stop hunt)',
        'params': {'swing_lookback': 10, 'confirmation_bars': 2},
        'param_ranges': {
            'swing_lookback': [5, 7, 10, 15, 20],
            'confirmation_bars': [1, 2, 3, 4]
        }
    },
    'smc_bos_bullish': {
        'direction': 'bullish',
        'category': 'smart_money',
        'description': 'SMC Break of Structure Bullish - Price breaks above swing high (trend continuation)',
        'params': {'swing_lookback': 5},
        'param_ranges': {
            'swing_lookback': [3, 5, 7, 10, 15]
        }
    },
    'smc_bos_bearish': {
        'direction': 'bearish',
        'category': 'smart_money',
        'description': 'SMC Break of Structure Bearish - Price breaks below swing low (trend continuation)',
        'params': {'swing_lookback': 5},
        'param_ranges': {
            'swing_lookback': [3, 5, 7, 10, 15]
        }
    },
    'smc_sweep_volume_bullish': {
        'direction': 'bullish',
        'category': 'smart_money',
        'description': 'SMC Liquidity Sweep + Volume - Sweep with high volume and price above EMA (confirmed institutional)',
        'params': {'swing_lookback': 10, 'vol_mult': 1.5, 'ema_period': 31},
        'param_ranges': {
            'swing_lookback': [5, 10, 15, 20],
            'vol_mult': [1.2, 1.5, 2.0, 2.5],
            'ema_period': [21, 31, 50]
        }
    },
    'smc_sweep_volume_bearish': {
        'direction': 'bearish',
        'category': 'smart_money',
        'description': 'SMC Liquidity Sweep + Volume - Sweep with high volume and price below EMA (confirmed institutional)',
        'params': {'swing_lookback': 10, 'vol_mult': 1.5, 'ema_period': 31},
        'param_ranges': {
            'swing_lookback': [5, 10, 15, 20],
            'vol_mult': [1.2, 1.5, 2.0, 2.5],
            'ema_period': [21, 31, 50]
        }
    },

    # ============================================================================
    # TURTLE EVOLUTION (Heikin Ashi Enhanced)
    # ============================================================================

    'turtle_ha_breakout_bullish': {
        'direction': 'bullish',
        'category': 'breakout',
        'description': 'Turtle HA Breakout Bullish - Donchian breakout on Heikin Ashi with ADX trend filter',
        'params': {'donchian_period': 20, 'adx_period': 14, 'adx_threshold': 20},
        'param_ranges': {
            'donchian_period': [10, 15, 20, 30, 55],
            'adx_period': [10, 14, 20],
            'adx_threshold': [15, 20, 25, 30]
        }
    },
    'turtle_ha_breakout_bearish': {
        'direction': 'bearish',
        'category': 'breakout',
        'description': 'Turtle HA Breakout Bearish - Donchian breakdown on Heikin Ashi with ADX trend filter',
        'params': {'donchian_period': 20, 'adx_period': 14, 'adx_threshold': 20},
        'param_ranges': {
            'donchian_period': [10, 15, 20, 30, 55],
            'adx_period': [10, 14, 20],
            'adx_threshold': [15, 20, 25, 30]
        }
    },

    # ============================================================================
    # EMA STACK & RECLAIM
    # ============================================================================

    'ema_stack_bullish': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'EMA Stack Bullish - Perfect EMA alignment (fast>medium>slow) indicating strong uptrend',
        'params': {'ema_fast': 8, 'ema_medium': 21, 'ema_slow': 50},
        'param_ranges': {
            'ema_fast': [5, 8, 9, 12, 15],
            'ema_medium': [15, 20, 21, 26, 30],
            'ema_slow': [40, 50, 55, 60]
        }
    },
    'ema_stack_bearish': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'EMA Stack Bearish - Perfect EMA alignment (fast<medium<slow) indicating strong downtrend',
        'params': {'ema_fast': 8, 'ema_medium': 21, 'ema_slow': 50},
        'param_ranges': {
            'ema_fast': [5, 8, 9, 12, 15],
            'ema_medium': [15, 20, 21, 26, 30],
            'ema_slow': [40, 50, 55, 60]
        }
    },
    'ema_reclaim_bullish': {
        'direction': 'bullish',
        'category': 'trend',
        'description': 'EMA Reclaim Bullish - Price pulls back below EMA then reclaims it in uptrend',
        'params': {'ema_period': 20, 'pullback_bars': 3},
        'param_ranges': {
            'ema_period': [10, 20, 30, 50],
            'pullback_bars': [2, 3, 4, 5]
        }
    },
    'ema_reclaim_bearish': {
        'direction': 'bearish',
        'category': 'trend',
        'description': 'EMA Reclaim Bearish - Price pulls back above EMA then loses it in downtrend',
        'params': {'ema_period': 20, 'pullback_bars': 3},
        'param_ranges': {
            'ema_period': [10, 20, 30, 50],
            'pullback_bars': [2, 3, 4, 5]
        }
    },

    # ============================================================================
    # DUAL-PATH TREND CATCHER
    # ============================================================================

    'dual_path_mou_bullish': {
        'direction': 'bullish',
        'category': 'breakout',
        'description': 'Dual-Path MOU (Breakout) - Volume breakout with MACD near zero in aligned EMA stack',
        'params': {'ema_short': 5, 'ema_medium': 13, 'ema_long': 26, 'vol_mult': 1.3},
        'param_ranges': {
            'ema_short': [5, 8],
            'ema_medium': [10, 13, 15],
            'ema_long': [20, 26, 30],
            'vol_mult': [1.2, 1.3, 1.5, 2.0]
        }
    },
    'dual_path_kaku_bullish': {
        'direction': 'bullish',
        'category': 'pullback',
        'description': 'Dual-Path KAKU (Pullback) - Pin bar pullback to EMA with strong MACD above zero',
        'params': {'ema_short': 5, 'ema_medium': 13, 'ema_long': 26, 'vol_mult': 1.5},
        'param_ranges': {
            'ema_short': [5, 8],
            'ema_medium': [10, 13, 15],
            'ema_long': [20, 26, 30],
            'vol_mult': [1.2, 1.5, 2.0, 2.5]
        }
    },
    'smart_pullback_hunter_bullish': {
        'direction': 'bullish',
        'category': 'pullback',
        'description': 'Smart Pullback Hunter - VWAP pullback with ADX in optimal range (20-35)',
        'params': {'ema_fast': 20, 'ema_slow': 50, 'adx_min': 20, 'adx_max': 35},
        'param_ranges': {
            'ema_fast': [10, 20, 30],
            'ema_slow': [50, 100],
            'adx_min': [15, 20, 25],
            'adx_max': [30, 35, 40, 45]
        }
    },
}


def create_indicator_library():
    """
    Factory function to create indicator library with metadata
    Returns a wrapper that includes both calculation methods and metadata
    """
    try:
        from indicators_legacy_reference import IndicatorLibrary
    except ImportError:
        # Fallback if legacy reference not available
        IndicatorLibrary = None
    
    class ComprehensiveIndicatorLibrary:
        """Wrapper class that adds metadata to the legacy IndicatorLibrary"""
        
        def __init__(self):
            self._lib_class = IndicatorLibrary
            self.metadata = INDICATOR_METADATA
        
        def create(self, df: pd.DataFrame):
            """Create an instance of the indicator library with data"""
            return self._lib_class(df)
        
        def get_by_direction(self, direction: str) -> List[str]:
            """Get all indicators for a specific direction"""
            return [name for name, meta in self.metadata.items() 
                   if meta['direction'] == direction]
        
        def get_by_category(self, category: str) -> List[str]:
            """Get all indicators for a specific category"""
            return [name for name, meta in self.metadata.items() 
                   if meta['category'] == category]
        
        def get_all_indicators(self) -> List[str]:
            """Get all indicator names"""
            return list(self.metadata.keys())
        
        def get_metadata(self, indicator_name: str) -> Dict[str, Any]:
            """Get metadata for a specific indicator"""
            return self.metadata.get(indicator_name, {})
        
        def get_default_params(self, indicator_name: str) -> Dict[str, Any]:
            """Get default parameters for an indicator"""
            meta = self.metadata.get(indicator_name, {})
            return meta.get('params', {})
        
        def get_param_ranges(self, indicator_name: str) -> Dict[str, List]:
            """Get parameter ranges for optimization"""
            meta = self.metadata.get(indicator_name, {})
            return meta.get('param_ranges', {})
        
        def get_summary(self) -> Dict[str, Any]:
            """Get summary statistics of the indicator library"""
            by_direction = {}
            by_category = {}
            
            for name, meta in self.metadata.items():
                direction = meta['direction']
                category = meta['category']
                
                by_direction[direction] = by_direction.get(direction, 0) + 1
                by_category[category] = by_category.get(category, 0) + 1
            
            return {
                'total': len(self.metadata),
                'by_direction': by_direction,
                'by_category': by_category,
                'all_indicators': list(self.metadata.keys())
            }
    
    # Return an instance of the wrapper class
    return ComprehensiveIndicatorLibrary()


# Export the factory function
__all__ = ['create_indicator_library', 'INDICATOR_METADATA']


if __name__ == '__main__':
    # Test the library
    factory = create_indicator_library()
    summary = factory.get_summary()
    
    print("=" * 80)
    print("COMPREHENSIVE INDICATOR LIBRARY SUMMARY")
    print("=" * 80)
    print(f"\nTotal Indicators: {summary['total']}")
    print(f"\nBy Direction:")
    for direction, count in summary['by_direction'].items():
        print(f"  {direction.capitalize()}: {count}")
    print(f"\nBy Category:")
    for category, count in summary['by_category'].items():
        print(f"  {category.replace('_', ' ').title()}: {count}")
    print("\n" + "=" * 80)

