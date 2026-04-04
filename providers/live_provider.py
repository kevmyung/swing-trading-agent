"""
providers/live_provider.py — DataProvider backed by live APIs.

Uses yfinance for market data (bars, quotes, snapshots) and Polygon for news.
Alpaca is used only for order execution (see live_broker.py).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pandas as pd

from providers.data_provider import DataProvider

logger = logging.getLogger(__name__)


class LiveProvider(DataProvider):
    """DataProvider backed by yfinance (data) and Polygon (news).

    Args:
        settings: Application settings (API keys, etc.).
    """

    _DAILY_ALIASES = {"day", "1d", "daily"}
    _HOURLY_ALIASES = {"hour", "1h", "hourly"}

    def __init__(self, settings) -> None:
        self._settings = settings

    # ------------------------------------------------------------------
    # DataProvider: bars
    # ------------------------------------------------------------------

    def get_bars(
        self,
        symbols: List[str],
        timeframe: str = "day",
        end=None,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV bars from yfinance."""
        import yfinance as yf

        if not symbols:
            return {}

        tf = timeframe.lower()
        if tf in self._DAILY_ALIASES:
            interval = "1d"
            period_days = 730  # ~2 years
        elif tf in self._HOURLY_ALIASES:
            interval = "1h"
            period_days = 180  # yfinance hourly limit ~730 days, use 6mo
        else:
            logger.warning("Unknown timeframe '%s', defaulting to daily", timeframe)
            interval = "1d"
            period_days = 730

        # Determine date range
        if end is not None:
            if isinstance(end, str):
                end_dt = datetime.strptime(end, "%Y-%m-%d")
            else:
                end_dt = end.replace(tzinfo=None) if end.tzinfo else end
            start_dt = end_dt - timedelta(days=period_days)
        else:
            end_dt = None
            start_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=period_days)

        result: Dict[str, pd.DataFrame] = {}
        batch_size = 20

        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            try:
                # yf.download for batch efficiency
                start_str = start_dt.strftime("%Y-%m-%d")
                end_str = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d") if end_dt else None

                df = yf.download(
                    batch,
                    start=start_str,
                    end=end_str,
                    interval=interval,
                    prepost=(interval == "1h"),
                    progress=False,
                    threads=True,
                )

                if df.empty:
                    continue

                # yf.download returns MultiIndex columns for multiple symbols
                if isinstance(df.columns, pd.MultiIndex):
                    for sym in batch:
                        try:
                            sym_df = df.xs(sym, level="Ticker", axis=1)
                            sym_df = _normalise_yf_df(sym_df)
                            if not sym_df.empty:
                                result[sym] = sym_df
                        except KeyError:
                            continue
                else:
                    # Single symbol — flat columns
                    sym = batch[0]
                    sym_df = _normalise_yf_df(df)
                    if not sym_df.empty:
                        result[sym] = sym_df

            except Exception as exc:
                logger.warning("yfinance batch %d failed: %s", i // batch_size + 1, exc)

        return result

    # ------------------------------------------------------------------
    # DataProvider: quotes
    # ------------------------------------------------------------------

    def get_quotes(self, symbols: List[str]) -> dict[str, dict]:
        """Fetch latest quotes from yfinance fast_info."""
        import yfinance as yf

        if not symbols:
            return {}

        result: Dict[str, dict] = {}
        for sym in symbols:
            try:
                ticker = yf.Ticker(sym)
                info = ticker.fast_info
                price = float(info.last_price)
                prev = float(info.previous_close)
                result[sym] = {
                    "ask_price": price,
                    "bid_price": price,
                    "mid_price": price,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "prev_close": prev,
                }
            except Exception as exc:
                logger.debug("yfinance quote failed for %s: %s", sym, exc)
        return result

    # ------------------------------------------------------------------
    # DataProvider: snapshots
    # ------------------------------------------------------------------

    def get_snapshots(self, symbols: List[str]) -> dict[str, dict]:
        """Fetch intraday snapshots from yfinance."""
        import yfinance as yf

        if not symbols:
            return {}

        result: Dict[str, dict] = {}
        for sym in symbols:
            try:
                ticker = yf.Ticker(sym)
                info = ticker.fast_info
                price = float(info.last_price)
                prev_close = float(info.previous_close)
                today_open = float(info.open)
                today_high = float(info.day_high)
                today_low = float(info.day_low)
                today_volume = float(info.last_volume)

                # Previous day volume from 2-day history
                prev_volume = 0.0
                try:
                    hist = ticker.history(period="2d", interval="1d")
                    if len(hist) >= 2:
                        prev_volume = float(hist.iloc[-2]["Volume"])
                except Exception:
                    pass

                result[sym] = {
                    "latest_price": price,
                    "today_open": today_open,
                    "today_high": today_high,
                    "today_low": today_low,
                    "today_close": price,
                    "today_volume": today_volume,
                    "prev_close": prev_close,
                    "prev_volume": prev_volume,
                    "ask_price": price,
                    "bid_price": price,
                    "mid_price": price,
                }
            except Exception as exc:
                logger.debug("yfinance snapshot failed for %s: %s", sym, exc)
        return result

    # ------------------------------------------------------------------
    # DataProvider: news
    # ------------------------------------------------------------------

    def get_news(self, tickers: List[str], hours_back: int = 24) -> dict:
        """Fetch and score news from Polygon."""
        from tools.sentiment.news import fetch_and_score_news, clear_article_cache
        clear_article_cache()
        if not tickers:
            return {}
        try:
            return fetch_and_score_news(tickers, hours_back=hours_back)
        except Exception as exc:
            logger.warning("LiveProvider.get_news failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # DataProvider: earnings
    # ------------------------------------------------------------------

    def get_earnings(self, tickers: List[str]) -> dict[str, int]:
        """Fetch upcoming earnings via yfinance (fixture-first)."""
        if not tickers:
            return {}
        try:
            from tools.sentiment.earnings import screen_earnings_events
            result = screen_earnings_events(tickers)
            earnings_map: dict[str, int] = {}
            for entry in result.get('upcoming_earnings', []):
                t = entry.get('ticker', '')
                days_until = entry.get('days_until')
                if t and days_until is not None:
                    earnings_map[t] = int(days_until)
            return earnings_map
        except Exception as exc:
            logger.warning("LiveProvider.get_earnings failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # DataProvider: universe
    # ------------------------------------------------------------------

    def get_universe(self) -> List[str]:
        """Fetch S&P 500 tickers from Wikipedia (cached by screener)."""
        try:
            from tools.data.screener import get_sp500_tickers
            return get_sp500_tickers()
        except Exception as exc:
            logger.warning("LiveProvider.get_universe failed: %s", exc)
            return []

    def get_sector_map(self) -> dict[str, str]:
        """Return ticker→sector from S&P 500 Wikipedia data."""
        try:
            from tools.data.screener import get_sp500_sector_map
            return get_sp500_sector_map()
        except Exception as exc:
            logger.warning("LiveProvider.get_sector_map failed: %s", exc)
            return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_yf_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise a yfinance DataFrame to standard OHLCV format."""
    # Rename columns to lowercase
    col_map = {
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
        "Adj Close": "adj_close",
    }
    df = df.rename(columns=col_map)

    # Keep only OHLCV
    ohlcv = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[ohlcv].copy()

    # Strip timezone from index
    if hasattr(df.index, "tz") and df.index.tz is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)

    # Ensure correct dtypes
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = df[col].astype(float)
    if "volume" in df.columns:
        df["volume"] = df["volume"].astype(float)

    # Drop rows with NaN prices
    df = df.dropna(subset=["close"])
    return df
