"""Exchange connector adapters for Tickles V2."""
from .base import BaseExchangeAdapter, Candle, MarketStatus
from .ccxt_adapter import CCXTAdapter

__all__ = ["BaseExchangeAdapter", "Candle", "MarketStatus", "CCXTAdapter"]