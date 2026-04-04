"""
backtest/fixtures/loader.py — Load API fixture files for backtesting and offline dev.

Provides two interfaces:
  1. ``load_fixture(path)`` — load any fixture JSON by relative path
  2. ``FixtureProvider`` — drop-in replacement for MarketDataProvider
     that serves OHLCV data from saved fixtures instead of Alpaca API
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

FIXTURES_DIR = Path(__file__).parent


def load_fixture(relative_path: str) -> dict | list:
    """Load a fixture JSON file by path relative to the fixtures directory.

    Args:
        relative_path: e.g. ``"alpaca/daily_bars.json"``

    Returns:
        Parsed JSON (dict or list).

    Raises:
        FileNotFoundError: If the fixture file does not exist.
    """
    path = FIXTURES_DIR / relative_path
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    with open(path) as f:
        return json.load(f)


class FixtureProvider:
    """Drop-in replacement for MarketDataProvider backed by fixture files.

    Loads OHLCV data from fixture JSON files and serves it through
    the same ``get_bars()`` / ``get_latest_quotes()`` interface.

    Supports multiple timeframes:
    - ``"day"`` / ``"1d"`` / ``"daily"`` → daily bars
    - ``"hour"`` / ``"1h"`` / ``"hourly"`` → hourly bars

    Usage::

        provider = FixtureProvider()
        daily = provider.get_bars(["AAPL"], timeframe="day")
        hourly = provider.get_bars(["AAPL"], timeframe="hour")
    """

    _DAILY_ALIASES = {"day", "1d", "daily"}
    _HOURLY_ALIASES = {"hour", "1h", "hourly"}

    def __init__(
        self,
        daily_file: str = "yfinance/daily_bars.json",
        hourly_file: str = "yfinance/hourly_bars.json",
    ) -> None:
        self._daily: Dict[str, pd.DataFrame] = _load_bars_fixture(daily_file)
        self._hourly: Dict[str, pd.DataFrame] = {}

        # Hourly fixture is optional — load if it exists
        hourly_path = FIXTURES_DIR / hourly_file
        if hourly_path.exists():
            self._hourly = _load_bars_fixture(hourly_file)
            logger.info("Loaded hourly fixture: %d symbols", len(self._hourly))

    @property
    def available_symbols(self) -> List[str]:
        """Symbols available in the daily fixture."""
        return list(self._daily.keys())

    @property
    def available_hourly_symbols(self) -> List[str]:
        """Symbols available in the hourly fixture."""
        return list(self._hourly.keys())

    def get_bars(
        self,
        symbols: List[str],
        timeframe: str = "day",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Return OHLCV bars for requested symbols, filtered by date range."""
        tf = timeframe.lower()
        if tf in self._DAILY_ALIASES:
            source = self._daily
        elif tf in self._HOURLY_ALIASES:
            source = self._hourly
        else:
            logger.warning("Unknown timeframe '%s', defaulting to daily", timeframe)
            source = self._daily

        result: Dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            df = source.get(symbol)
            if df is None:
                logger.warning("Fixture: no %s data for %s", timeframe, symbol)
                continue
            if start is not None:
                df = df[df.index >= pd.Timestamp(start)]
            if end is not None:
                df = df[df.index <= pd.Timestamp(end)]
            if not df.empty:
                result[symbol] = df
        return result

    def get_latest_quotes(self, symbols: List[str]) -> Dict[str, dict]:
        """Return last-bar-based pseudo-quotes from fixture data."""
        quotes = load_fixture("alpaca/latest_quotes.json")
        return {s: quotes[s] for s in symbols if s in quotes}


def _load_bars_fixture(relative_path: str) -> Dict[str, pd.DataFrame]:
    """Load a bars fixture JSON into a dict of DataFrames."""
    raw = load_fixture(relative_path)
    bars: Dict[str, pd.DataFrame] = {}
    for symbol, date_dict in raw.items():
        df = pd.DataFrame.from_dict(date_dict, orient="index")
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        for col in ("open", "high", "low", "close"):
            if col in df.columns:
                df[col] = df[col].astype(float)
        if "volume" in df.columns:
            df["volume"] = df["volume"].astype(float)
        bars[symbol] = df
    return bars
