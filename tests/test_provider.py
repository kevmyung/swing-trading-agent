"""
tests/test_provider.py — Unit tests for the market data provider layer.

All tests mock the Alpaca client — no real API calls are made.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from unittest import mock
from unittest.mock import MagicMock, patch, PropertyMock

import pandas as pd
import pytest

from tools.data.cache import DataCache
from tools.data.provider import (
    MarketDataProvider,
    _cache_key,
    _normalise_index,
    _parse_bars_response,
    _strip_tz,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

START = datetime(2024, 1, 1)
END = datetime(2024, 3, 31)


def _make_provider(tmp_path) -> MarketDataProvider:
    """Return a provider wired to a temp cache dir."""
    return MarketDataProvider(
        api_key="test_key",
        secret_key="test_secret",
        cache_dir=str(tmp_path / "cache"),
    )


def _make_multiindex_df(symbols, n_bars=5) -> pd.DataFrame:
    """Build a fake (symbol, timestamp) MultiIndex DataFrame like bars.df."""
    import numpy as np

    rows = []
    base = datetime(2024, 1, 2)
    for sym in symbols:
        for i in range(n_bars):
            ts = pd.Timestamp(base + timedelta(days=i), tz="UTC")
            rows.append({
                "symbol": sym,
                "timestamp": ts,
                "open": 100.0 + i,
                "high": 101.0 + i,
                "low": 99.0 + i,
                "close": 100.5 + i,
                "volume": 1_000_000.0,
            })
    df = pd.DataFrame(rows).set_index(["symbol", "timestamp"])
    return df


def _make_bars_mock(symbols, n_bars=5) -> MagicMock:
    """Build a mock that mimics the object returned by client.get_stock_bars()."""
    bars = MagicMock()
    type(bars).df = PropertyMock(return_value=_make_multiindex_df(symbols, n_bars))
    return bars


# ---------------------------------------------------------------------------
# DataCache tests
# ---------------------------------------------------------------------------

class TestDataCache:
    def test_get_miss_on_empty_cache(self, tmp_path):
        cache = DataCache(str(tmp_path))
        assert cache.get("nonexistent") is None

    def test_put_and_get_roundtrip(self, tmp_path):
        cache = DataCache(str(tmp_path))
        df = pd.DataFrame({"close": [100.0, 101.0]})
        cache.put("test_key", df)
        result = cache.get("test_key")
        assert result is not None
        assert list(result["close"]) == [100.0, 101.0]

    def test_is_fresh_returns_true_immediately(self, tmp_path):
        cache = DataCache(str(tmp_path))
        df = pd.DataFrame({"close": [1.0]})
        cache.put("k", df)
        assert cache.is_fresh("k", max_age_hours=24) is True

    def test_is_fresh_returns_false_for_old_file(self, tmp_path):
        cache = DataCache(str(tmp_path))
        df = pd.DataFrame({"close": [1.0]})
        cache.put("k", df)
        path = cache._path("k")
        # Backdate mtime by 25 hours
        old_mtime = time.time() - 25 * 3600
        import os
        os.utime(path, (old_mtime, old_mtime))
        assert cache.is_fresh("k", max_age_hours=24) is False

    def test_get_returns_none_for_stale_entry(self, tmp_path):
        cache = DataCache(str(tmp_path))
        df = pd.DataFrame({"close": [1.0]})
        cache.put("k", df)
        path = cache._path("k")
        old_mtime = time.time() - 25 * 3600
        import os
        os.utime(path, (old_mtime, old_mtime))
        assert cache.get("k", max_age_hours=24) is None

    def test_clear_removes_all_parquet_files(self, tmp_path):
        cache = DataCache(str(tmp_path))
        for i in range(3):
            cache.put(f"key_{i}", pd.DataFrame({"v": [float(i)]}))
        removed = cache.clear()
        assert removed == 3
        assert cache.get("key_0") is None

    def test_cache_dir_created_automatically(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        DataCache(str(deep))
        assert deep.exists()

    def test_path_sanitises_special_chars(self, tmp_path):
        cache = DataCache(str(tmp_path))
        path = cache._path("AAPL/day:2024-01-01 00:00")
        assert "/" not in path.name
        assert ":" not in path.name

    def test_put_preserves_index(self, tmp_path):
        cache = DataCache(str(tmp_path))
        df = pd.DataFrame(
            {"close": [100.0, 101.0]},
            index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
        )
        cache.put("idx_test", df)
        result = cache.get("idx_test")
        assert isinstance(result.index, pd.DatetimeIndex)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_strip_tz_removes_utc(self):
        import pytz
        dt = datetime(2024, 1, 1, 12, tzinfo=pytz.UTC)
        result = _strip_tz(dt)
        assert result.tzinfo is None
        assert result.year == 2024

    def test_strip_tz_naive_unchanged(self):
        dt = datetime(2024, 6, 15, 9, 30)
        assert _strip_tz(dt) == dt

    def test_cache_key_format(self):
        key = _cache_key("AAPL", "day", datetime(2024, 1, 1), datetime(2024, 12, 31))
        assert key == "AAPL_day_2024-01-01_2024-12-31"

    def test_normalise_index_strips_tz(self):
        df = pd.DataFrame(
            {"close": [100.0]},
            index=pd.DatetimeIndex([pd.Timestamp("2024-01-02", tz="UTC")]),
        )
        result = _normalise_index(df)
        assert result.index.tz is None

    def test_normalise_index_float_columns(self):
        df = pd.DataFrame(
            {"open": [100], "high": [101], "low": [99], "close": [100], "volume": [1000]},
            index=pd.DatetimeIndex([pd.Timestamp("2024-01-02")]),
        )
        result = _normalise_index(df)
        assert result["close"].dtype == float
        assert result["volume"].dtype == float

    def test_parse_bars_response_splits_symbols(self):
        symbols = ["AAPL", "MSFT"]
        bars_mock = _make_bars_mock(symbols, n_bars=3)
        result = _parse_bars_response(bars_mock, symbols)
        assert set(result.keys()) == {"AAPL", "MSFT"}
        assert len(result["AAPL"]) == 3
        assert len(result["MSFT"]) == 3

    def test_parse_bars_response_ohlcv_columns(self):
        bars_mock = _make_bars_mock(["AAPL"], n_bars=5)
        result = _parse_bars_response(bars_mock, ["AAPL"])
        df = result["AAPL"]
        for col in ("open", "high", "low", "close", "volume"):
            assert col in df.columns, f"Missing column: {col}"

    def test_parse_bars_response_timezone_naive_index(self):
        bars_mock = _make_bars_mock(["AAPL"], n_bars=5)
        result = _parse_bars_response(bars_mock, ["AAPL"])
        assert result["AAPL"].index.tz is None

    def test_parse_bars_response_empty_df(self):
        bars_mock = MagicMock()
        type(bars_mock).df = PropertyMock(return_value=pd.DataFrame())
        result = _parse_bars_response(bars_mock, ["AAPL"])
        assert result == {}

    def test_parse_bars_response_missing_symbol_skipped(self):
        bars_mock = _make_bars_mock(["AAPL"], n_bars=2)
        # Ask for MSFT which isn't in the mock data
        result = _parse_bars_response(bars_mock, ["AAPL", "MSFT"])
        assert "AAPL" in result
        assert "MSFT" not in result


# ---------------------------------------------------------------------------
# MarketDataProvider tests
# ---------------------------------------------------------------------------
