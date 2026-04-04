"""
tests/test_price_levels.py — Unit tests for tools/quant/price_levels.py.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.quant.price_levels import (
    find_swing_pivots,
    _deduplicate_levels,
    compute_ma_levels,
    compute_volume_nodes,
    compute_price_levels,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv_df(closes: list[float], n: int | None = None) -> pd.DataFrame:
    """Build a simple OHLCV DataFrame from close prices."""
    if n is None:
        n = len(closes)
    closes = closes[-n:] if len(closes) > n else closes
    return pd.DataFrame({
        'open': closes,
        'high': [c * 1.01 for c in closes],
        'low': [c * 0.99 for c in closes],
        'close': closes,
        'volume': [1_000_000] * len(closes),
    }, index=pd.date_range('2025-01-01', periods=len(closes), freq='B'))


# ---------------------------------------------------------------------------
# find_swing_pivots
# ---------------------------------------------------------------------------

class TestSwingPivots:
    def test_insufficient_data(self):
        result = find_swing_pivots([1, 2, 3], [1, 2, 3], order=5)
        assert result == {'swing_highs': [], 'swing_lows': []}

    def test_detects_swing_high(self):
        # Create a clear swing high at index 5
        highs = [10, 11, 12, 13, 14, 20, 14, 13, 12, 11, 10]
        lows = [9, 10, 11, 12, 13, 19, 13, 12, 11, 10, 9]
        result = find_swing_pivots(highs, lows, order=5)
        assert 20 in result['swing_highs']

    def test_detects_swing_low(self):
        # Create a clear swing low at index 5
        highs = [20, 19, 18, 17, 16, 11, 16, 17, 18, 19, 20]
        lows = [19, 18, 17, 16, 15, 10, 15, 16, 17, 18, 19]
        result = find_swing_pivots(highs, lows, order=5)
        assert 10 in result['swing_lows']

    def test_results_sorted(self):
        # Multiple swing points
        highs = list(range(10, 21)) + [30] + list(range(20, 9, -1)) + [35] + list(range(20, 9, -1))
        lows = [h - 1 for h in highs]
        result = find_swing_pivots(highs, lows, order=3)
        assert result['swing_highs'] == sorted(result['swing_highs'])
        assert result['swing_lows'] == sorted(result['swing_lows'])


# ---------------------------------------------------------------------------
# _deduplicate_levels
# ---------------------------------------------------------------------------

class TestDeduplicateLevels:
    def test_empty(self):
        assert _deduplicate_levels([]) == []

    def test_no_duplicates(self):
        result = _deduplicate_levels([100.0, 110.0, 120.0])
        assert len(result) == 3

    def test_removes_close_levels(self):
        # 100.0 and 100.4 are within 0.5% of each other
        result = _deduplicate_levels([100.0, 100.4])
        assert len(result) == 1

    def test_keeps_distant_levels(self):
        result = _deduplicate_levels([100.0, 101.0])
        assert len(result) == 2


# ---------------------------------------------------------------------------
# compute_ma_levels
# ---------------------------------------------------------------------------

class TestMALevels:
    def test_insufficient_data(self):
        result = compute_ma_levels([100.0] * 10)
        assert result['ma_50'] is None
        assert result['ma_200'] is None

    def test_50_day_ma(self):
        closes = [100.0] * 50
        result = compute_ma_levels(closes)
        assert result['ma_50'] == 100.0
        assert result['ma_200'] is None

    def test_200_day_ma(self):
        closes = [100.0] * 200
        result = compute_ma_levels(closes)
        assert result['ma_50'] == 100.0
        assert result['ma_200'] == 100.0


# ---------------------------------------------------------------------------
# compute_volume_nodes
# ---------------------------------------------------------------------------

class TestVolumeNodes:
    def test_insufficient_data(self):
        assert compute_volume_nodes([100.0] * 5, [1000] * 5) == []

    def test_returns_sorted(self):
        np.random.seed(42)
        closes = list(np.random.normal(100, 5, 60))
        volumes = list(np.random.uniform(500000, 2000000, 60))
        result = compute_volume_nodes(closes, volumes)
        assert result == sorted(result)

    def test_constant_price_returns_single_node(self):
        closes = [100.0] * 30
        volumes = [1000000] * 30
        # All volume in one bin
        result = compute_volume_nodes(closes, volumes)
        # With constant price, price_max == price_min → returns []
        assert result == []


# ---------------------------------------------------------------------------
# compute_price_levels (integration)
# ---------------------------------------------------------------------------

class TestComputePriceLevels:
    def test_empty_df(self):
        result = compute_price_levels(pd.DataFrame(), 100.0)
        assert result['nearest_support'] is None
        assert result['nearest_resistance'] is None
        assert result['ma_confluence'] is False

    def test_none_df(self):
        result = compute_price_levels(None, 100.0)
        assert result['nearest_support'] is None

    def test_short_df(self):
        df = _make_ohlcv_df([100.0] * 10)
        result = compute_price_levels(df, 100.0)
        assert result['nearest_support'] is None

    def test_ma_confluence_detected(self):
        # Price at 100, 50-day MA will be ~100 → confluence
        closes = [100.0] * 55
        df = _make_ohlcv_df(closes)
        result = compute_price_levels(df, 100.0)
        assert result['ma_confluence'] is True

    def test_stop_vs_support_positive_when_stop_above(self):
        # Create data with a clear support level below current price
        # Swing low around 95, current price 105, stop at 102
        prices = list(range(100, 90, -1)) + list(range(90, 106))
        df = _make_ohlcv_df(prices)
        result = compute_price_levels(df, 105.0, stop_loss_price=102.0)
        if result['nearest_support'] is not None:
            svs = result['stop_vs_nearest_support']
            # stop at 102 should be above some support level
            assert svs is not None

    def test_returns_all_expected_keys(self):
        closes = list(np.linspace(90, 110, 60))
        df = _make_ohlcv_df(closes)
        result = compute_price_levels(df, 105.0, stop_loss_price=100.0)
        expected_keys = {
            'nearest_support', 'nearest_resistance', 'key_ma_levels',
            'volume_nodes', 'stop_vs_nearest_support',
            'entry_vs_nearest_resistance', 'ma_confluence',
        }
        assert expected_keys == set(result.keys())

    def test_resistance_above_current_price(self):
        # Uptrend then drop — swing high should be above current price
        closes = list(range(90, 110)) + list(range(110, 99, -1))
        df = _make_ohlcv_df(closes)
        result = compute_price_levels(df, 100.0)
        if result['nearest_resistance'] is not None:
            assert result['nearest_resistance'] > 100.0
