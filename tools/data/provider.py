"""
tools/data/provider.py — Market data provider backed by Alpaca.

Fetches daily and hourly OHLCV bar data via the alpaca-py SDK,
with transparent disk-based caching to avoid redundant API calls.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd

from .cache import DataCache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Alpaca import with graceful fallback for environments without alpaca-py
# ---------------------------------------------------------------------------

try:
    from alpaca.data import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest, StockSnapshotRequest
    from alpaca.data.timeframe import TimeFrame

    _ALPACA_AVAILABLE = True
except ImportError:  # pragma: no cover
    _ALPACA_AVAILABLE = False
    StockHistoricalDataClient = None  # type: ignore[assignment,misc]
    StockBarsRequest = None           # type: ignore[assignment]
    StockLatestQuoteRequest = None    # type: ignore[assignment]
    StockSnapshotRequest = None       # type: ignore[assignment]
    TimeFrame = None                  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# MarketDataProvider
# ---------------------------------------------------------------------------

class MarketDataProvider:
    """Fetches OHLCV bar data from Alpaca with transparent disk caching.

    Args:
        api_key: Alpaca API key ID.
        secret_key: Alpaca secret key.
        cache_dir: Directory for the on-disk Parquet cache.
                   Defaults to ``.cache/market_data``.

    Example::

        provider = MarketDataProvider(api_key="...", secret_key="...")
        bars = provider.get_bars(
            symbols=["AAPL", "MSFT"],
            timeframe="day",
            start=datetime(2024, 1, 1),
            end=datetime(2024, 12, 31),
        )
        aapl_df = bars["AAPL"]
    """

    #: Map of friendly timeframe name -> Alpaca TimeFrame constant (or string fallback)
    _TIMEFRAME_MAP: Dict[str, object] = {}

    # Fallback map used when alpaca-py is not installed (e.g. unit tests with mocked client)
    _TIMEFRAME_FALLBACK: Dict[str, str] = {
        "day": "day",
        "1d": "day",
        "daily": "day",
        "hour": "hour",
        "1h": "hour",
        "hourly": "hour",
    }

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        cache_dir: str = ".cache/market_data",
        data_feed: str = "iex",
    ) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._data_feed = data_feed
        self._cache = DataCache(cache_dir)
        self._client: Optional[object] = None  # lazy-initialised

        if _ALPACA_AVAILABLE:
            self._TIMEFRAME_MAP = {
                "day": TimeFrame.Day,
                "1d": TimeFrame.Day,
                "daily": TimeFrame.Day,
                "hour": TimeFrame.Hour,
                "1h": TimeFrame.Hour,
                "hourly": TimeFrame.Hour,
            }
        else:
            self._TIMEFRAME_MAP = dict(self._TIMEFRAME_FALLBACK)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_bars(
        self,
        symbols: List[str],
        timeframe: str = "day",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch OHLCV bars for multiple symbols.

        Returns cached data when available and fresh (< 24 hours old).
        Otherwise fetches from Alpaca and updates the cache.

        Args:
            symbols: List of ticker symbols, e.g. ``["AAPL", "MSFT"]``.
            timeframe: Timeframe string — ``"day"`` / ``"1d"`` / ``"daily"``
                       or ``"hour"`` / ``"1h"`` / ``"hourly"``.
            start: Start of the requested date range (inclusive).
                   Defaults to 365 days before *end*.
            end: End of the requested date range (inclusive).
                 Defaults to today (UTC).

        Returns:
            Dict mapping each symbol to a ``pd.DataFrame`` with columns
            ``open, high, low, close, volume`` and a timezone-naive
            ``DatetimeIndex``.  Symbols with no data are omitted.
        """
        if not symbols:
            return {}

        end = end or datetime.now(timezone.utc).replace(tzinfo=None)
        if start is None:
            from datetime import timedelta
            start = end - timedelta(days=365)

        # Normalise to timezone-naive datetimes for cache key consistency
        start = _strip_tz(start)
        end = _strip_tz(end)

        tf_str = timeframe.lower()

        result: Dict[str, pd.DataFrame] = {}
        symbols_to_fetch: List[str] = []

        for symbol in symbols:
            key = _cache_key(symbol, tf_str, start, end)
            cached = self._cache.get(key)
            if cached is not None:
                result[symbol] = cached
            else:
                symbols_to_fetch.append(symbol)

        if symbols_to_fetch:
            fetched = self._fetch_bars_batched(symbols_to_fetch, tf_str, start, end)
            for symbol, df in fetched.items():
                key = _cache_key(symbol, tf_str, start, end)
                self._cache.put(key, df)
                result[symbol] = df

        return result

    def _fetch_bars_batched(
        self,
        symbols: List[str],
        timeframe: str,
        start: datetime,
        end: datetime,
        batch_size: int = 50,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch bars in batches so one bad symbol doesn't kill the entire request."""
        result: Dict[str, pd.DataFrame] = {}
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            try:
                fetched = self._fetch_from_alpaca(batch, timeframe, start, end)
                result.update(fetched)
            except Exception as exc:
                logger.warning(
                    "Batch %d–%d failed (%d symbols): %s — retrying individually.",
                    i, i + len(batch), len(batch), exc,
                )
                for sym in batch:
                    try:
                        fetched = self._fetch_from_alpaca([sym], timeframe, start, end)
                        result.update(fetched)
                    except Exception:
                        logger.warning("Skipping symbol: %s", sym)
        return result

    def get_latest_quotes(
        self,
        symbols: List[str],
    ) -> Dict[str, dict]:
        """Fetch the latest quote (bid/ask) for multiple symbols.

        Used by the MORNING cycle to check pre-market / opening prices
        before executing EOD-generated entry signals.

        Args:
            symbols: Ticker symbols to query.

        Returns:
            Dict mapping symbol to ``{ask_price, bid_price, mid_price, timestamp}``.
            Symbols with no data are omitted.
        """
        if not symbols or not _ALPACA_AVAILABLE:
            return {}

        try:
            client = self._get_client()
            if StockLatestQuoteRequest is not None:
                request = StockLatestQuoteRequest(symbol_or_symbols=symbols)
            else:
                from types import SimpleNamespace
                request = SimpleNamespace(symbol_or_symbols=symbols)

            quotes = client.get_stock_latest_quote(request)
        except Exception as exc:
            logger.error("Failed to fetch latest quotes: %s", exc)
            return {}

        result: Dict[str, dict] = {}
        for symbol in symbols:
            q = quotes.get(symbol)
            if q is None:
                continue
            ask = float(q.ask_price) if q.ask_price else 0.0
            bid = float(q.bid_price) if q.bid_price else 0.0
            mid = round((ask + bid) / 2, 4) if ask > 0 and bid > 0 else (ask or bid)
            result[symbol] = {
                'ask_price': ask,
                'bid_price': bid,
                'mid_price': mid,
                'timestamp': q.timestamp.isoformat() if q.timestamp else None,
            }
        return result

    def get_snapshots(
        self,
        symbols: List[str],
    ) -> Dict[str, dict]:
        """Fetch real-time snapshots for multiple symbols.

        Each snapshot contains today's daily bar (open/high/low/close/volume),
        previous daily bar, latest trade, latest quote, and latest minute bar.
        This is the primary data source for intraday context.

        Returns:
            Dict mapping symbol to a flat dict with intraday-relevant fields.
            Symbols with no data are omitted.
        """
        if not symbols or not _ALPACA_AVAILABLE:
            return {}

        try:
            client = self._get_client()
            if StockSnapshotRequest is not None:
                request = StockSnapshotRequest(symbol_or_symbols=symbols)
            else:
                from types import SimpleNamespace
                request = SimpleNamespace(symbol_or_symbols=symbols)

            snapshots = client.get_stock_snapshot(request)
        except Exception as exc:
            logger.error("Failed to fetch snapshots: %s", exc)
            return {}

        result: Dict[str, dict] = {}
        for symbol in symbols:
            snap = snapshots.get(symbol)
            if snap is None:
                continue
            entry: dict = {}

            # Latest trade
            if snap.latest_trade:
                entry['latest_price'] = float(snap.latest_trade.price)
                entry['latest_trade_ts'] = (
                    snap.latest_trade.timestamp.isoformat()
                    if snap.latest_trade.timestamp else None
                )

            # Today's daily bar (in-progress)
            if snap.daily_bar:
                entry['today_open'] = float(snap.daily_bar.open)
                entry['today_high'] = float(snap.daily_bar.high)
                entry['today_low'] = float(snap.daily_bar.low)
                entry['today_close'] = float(snap.daily_bar.close)
                entry['today_volume'] = float(snap.daily_bar.volume)

            # Previous daily bar
            if snap.previous_daily_bar:
                entry['prev_close'] = float(snap.previous_daily_bar.close)
                entry['prev_volume'] = float(snap.previous_daily_bar.volume)

            # Latest quote
            if snap.latest_quote:
                ask = float(snap.latest_quote.ask_price) if snap.latest_quote.ask_price else 0.0
                bid = float(snap.latest_quote.bid_price) if snap.latest_quote.bid_price else 0.0
                entry['ask_price'] = ask
                entry['bid_price'] = bid
                entry['mid_price'] = round((ask + bid) / 2, 4) if ask > 0 and bid > 0 else (ask or bid)

            result[symbol] = entry
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> object:
        """Return a lazily-initialised Alpaca client."""
        if not _ALPACA_AVAILABLE:
            raise ImportError(
                "alpaca-py is not installed. Run: pip install alpaca-py"
            )
        if self._client is None:
            self._client = StockHistoricalDataClient(
                api_key=self._api_key,
                secret_key=self._secret_key,
            )
        return self._client

    def _fetch_from_alpaca(
        self,
        symbols: List[str],
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch bars from the Alpaca API and return per-symbol DataFrames."""
        tf = self._TIMEFRAME_MAP.get(timeframe)
        if tf is None:
            raise ValueError(
                f"Unknown timeframe '{timeframe}'. "
                f"Valid options: {list(self._TIMEFRAME_MAP.keys())}"
            )

        logger.info(
            "Fetching %s bars for %d symbol(s) from %s to %s",
            timeframe, len(symbols), start.date(), end.date(),
        )

        client = self._get_client()

        try:
            # StockBarsRequest is None when alpaca-py is not installed.
            # In that case (e.g. mocked client in tests) pass a plain dict.
            if StockBarsRequest is not None:
                request = StockBarsRequest(
                    symbol_or_symbols=symbols,
                    timeframe=tf,
                    start=start,
                    end=end,
                    feed=self._data_feed,
                )
            else:
                from types import SimpleNamespace  # noqa: PLC0415
                request = SimpleNamespace(
                    symbol_or_symbols=symbols,
                    timeframe=tf,
                    start=start,
                    end=end,
                    feed=self._data_feed,
                )
            bars = client.get_stock_bars(request)
        except Exception as exc:
            logger.error("Alpaca API error: %s", exc)
            raise

        return _parse_bars_response(bars, symbols)


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def create_provider(cache_dir: str = ".cache/market_data") -> MarketDataProvider:
    """Create a :class:`MarketDataProvider` using credentials from settings.

    Args:
        cache_dir: Directory for the on-disk Parquet cache.

    Returns:
        A fully-configured :class:`MarketDataProvider` instance.
    """
    from config.settings import get_settings
    settings = get_settings()
    return MarketDataProvider(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        cache_dir=cache_dir,
        data_feed=settings.alpaca_data_feed,
    )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _strip_tz(dt: datetime) -> datetime:
    """Return *dt* with timezone info removed (convert to UTC first if needed)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _cache_key(symbol: str, timeframe: str, start: datetime, end: datetime) -> str:
    """Build a deterministic cache key string."""
    return f"{symbol}_{timeframe}_{start.date()}_{end.date()}"


def _parse_bars_response(bars: object, requested_symbols: List[str]) -> Dict[str, pd.DataFrame]:
    """Split a multi-symbol Alpaca bars response into per-symbol DataFrames.

    The ``bars.df`` property returns a ``pd.DataFrame`` with a
    ``(symbol, timestamp)`` MultiIndex.  This function extracts each symbol's
    slice, normalises column names to lower-case, and strips timezone info
    from the DatetimeIndex.

    Args:
        bars: The raw response object from ``client.get_stock_bars()``.
        requested_symbols: The list of symbols that were requested (used to
                           detect missing symbols and log a warning).

    Returns:
        Dict mapping symbol -> DataFrame.
    """
    result: Dict[str, pd.DataFrame] = {}

    try:
        raw_df: pd.DataFrame = bars.df
    except Exception as exc:
        logger.error("Failed to access bars.df: %s", exc)
        return result

    if raw_df is None or raw_df.empty:
        logger.warning("Alpaca returned empty bar data for symbols: %s", requested_symbols)
        return result

    # Normalise column names
    raw_df.columns = [c.lower() for c in raw_df.columns]

    ohlcv_cols = [c for c in ["open", "high", "low", "close", "volume"] if c in raw_df.columns]

    if isinstance(raw_df.index, pd.MultiIndex):
        # Standard case: (symbol, timestamp) MultiIndex
        for symbol in requested_symbols:
            try:
                symbol_df = raw_df.xs(symbol, level="symbol")[ohlcv_cols].copy()
            except KeyError:
                logger.warning("No data returned for symbol: %s", symbol)
                continue

            symbol_df = _normalise_index(symbol_df)
            if symbol_df.empty:
                logger.warning("Empty DataFrame after normalisation for: %s", symbol)
                continue

            result[symbol] = symbol_df
    else:
        # Single-symbol response (no MultiIndex)
        symbol = requested_symbols[0] if requested_symbols else "UNKNOWN"
        df = raw_df[ohlcv_cols].copy()
        df = _normalise_index(df)
        if not df.empty:
            result[symbol] = df

    return result


def _normalise_index(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure the DataFrame has a timezone-naive DatetimeIndex."""
    if not isinstance(df.index, pd.DatetimeIndex):
        try:
            df.index = pd.to_datetime(df.index)
        except Exception as exc:
            logger.warning("Could not convert index to DatetimeIndex: %s", exc)
            return df

    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)

    # Ensure float dtype for price columns
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = df[col].astype(float)
    if "volume" in df.columns:
        df["volume"] = df["volume"].astype(float)

    return df
