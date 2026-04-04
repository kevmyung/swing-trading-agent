"""
providers/data_provider.py — Abstract base class for market data providers.

Both FixtureProvider (backtest) and LiveProvider (live trading) implement
this interface, making the pipeline cycles in portfolio_agent.py agnostic
to where the data comes from.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

import pandas as pd


class DataProvider(ABC):
    """Abstract interface for market data.

    All cycle methods in PortfolioAgent call these methods rather than
    making direct API/fixture calls.
    """

    @abstractmethod
    def get_bars(
        self,
        symbols: List[str],
        timeframe: str = "day",
        end=None,
    ) -> dict[str, pd.DataFrame]:
        """Return OHLCV bars for each symbol.

        Args:
            symbols: Ticker symbols to fetch.
            timeframe: ``'day'`` or ``'hour'``.
            end: Latest date to include (datetime or str ``'YYYY-MM-DD'``).
                 None means current date.

        Returns:
            dict mapping symbol → DataFrame with columns open/high/low/close/volume.
        """

    @abstractmethod
    def get_quotes(self, symbols: List[str]) -> dict[str, dict]:
        """Return latest bid/ask/mid quote per symbol.

        Returns:
            dict[symbol] = {ask_price, bid_price, mid_price, timestamp}
        """

    @abstractmethod
    def get_snapshots(self, symbols: List[str]) -> dict[str, dict]:
        """Return intraday snapshot per symbol.

        Returns:
            dict[symbol] = {latest_price, today_open, today_high, today_low,
                             today_close, today_volume, prev_close, prev_volume,
                             ask_price, bid_price, mid_price}
        """

    @abstractmethod
    def get_news(self, tickers: List[str], hours_back: int = 24) -> dict:
        """Return scored news per ticker.

        Returns:
            dict[ticker] = {composite_sentiment, article_count, veto_trade,
                             top_headline, key_events, raw_articles}
        """

    @abstractmethod
    def get_earnings(self, tickers: List[str]) -> dict[str, int]:
        """Return days-to-earnings map.

        Returns:
            dict[ticker] = days_until_earnings (int, positive = upcoming)
        """

    @abstractmethod
    def get_universe(self) -> List[str]:
        """Return S&P 500 ticker list."""

    @abstractmethod
    def get_sector_map(self) -> dict[str, str]:
        """Return ticker → GICS sector mapping."""
