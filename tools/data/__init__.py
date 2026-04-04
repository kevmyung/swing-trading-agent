"""
tools/data — Market data provider and universe screener.

Exports:
  MarketDataProvider  — fetch OHLCV bar data from Alpaca
  create_provider     — factory using credentials from settings
  DataCache           — transparent disk-based Parquet cache
  screen_universe     — S&P 500 screener (liquidity + volatility + momentum)
  get_sp500_tickers   — fetch current S&P 500 constituent list
"""

from .provider import MarketDataProvider, create_provider
from .cache import DataCache
from .screener import screen_universe, get_sp500_tickers

__all__ = [
    "MarketDataProvider",
    "create_provider",
    "DataCache",
    "screen_universe",
    "get_sp500_tickers",
]
