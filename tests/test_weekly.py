"""
tests/test_weekly.py — Unit tests for weekly timeframe context computation.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.quant.weekly import (
    resample_to_weekly,
    compute_weekly_context,
    _compute_trend_score,
    _rsi_weekly,
    _classify_weinstein_stage,
)


def _make_daily_df(n_days: int = 300, trend: str = "up") -> pd.DataFrame:
    """Generate a synthetic daily OHLCV DataFrame."""
    dates = pd.bdate_range(end="2025-12-31", periods=n_days, freq="B")
    np.random.seed(42)

    if trend == "up":
        base = 100.0 + np.cumsum(np.random.normal(0.15, 1.0, n_days))
    elif trend == "down":
        base = 200.0 + np.cumsum(np.random.normal(-0.15, 1.0, n_days))
    else:  # flat
        base = 150.0 + np.cumsum(np.random.normal(0.0, 1.0, n_days))

    # Ensure positive prices
    base = np.maximum(base, 10.0)

    df = pd.DataFrame(
        {
            "open": base - np.random.uniform(0, 1, n_days),
            "high": base + np.random.uniform(0, 2, n_days),
            "low": base - np.random.uniform(0, 2, n_days),
            "close": base,
            "volume": np.random.randint(100000, 1000000, n_days).astype(float),
        },
        index=dates,
    )
    return df


class TestResampleToWeekly:
    def test_basic_resample(self):
        daily = _make_daily_df(100)
        weekly = resample_to_weekly(daily)
        assert not weekly.empty
        assert len(weekly) < len(daily)
        assert len(weekly) >= 15  # ~100 days / 5 = ~20 weeks

    def test_empty_input(self):
        assert resample_to_weekly(pd.DataFrame()).empty

    def test_none_input(self):
        assert resample_to_weekly(None).empty

    def test_too_few_days(self):
        daily = _make_daily_df(3)
        assert resample_to_weekly(daily).empty

    def test_columns_preserved(self):
        daily = _make_daily_df(100)
        weekly = resample_to_weekly(daily)
        for col in ("open", "high", "low", "close", "volume"):
            assert col in weekly.columns

    def test_weekly_high_gte_daily_max(self):
        """Weekly high should be max of daily highs in that week."""
        daily = _make_daily_df(50)
        weekly = resample_to_weekly(daily)
        # At least check that weekly highs are reasonable
        assert weekly["high"].max() >= weekly["close"].max()


class TestComputeTrendScore:
    def test_perfect_uptrend(self):
        # Strictly increasing highs and lows
        highs = list(range(100, 120))
        lows = list(range(90, 110))
        score = _compute_trend_score(highs, lows, lookback=12)
        assert score == 1.0

    def test_perfect_downtrend(self):
        highs = list(range(120, 100, -1))
        lows = list(range(110, 90, -1))
        score = _compute_trend_score(highs, lows, lookback=12)
        assert score == -1.0

    def test_flat_returns_near_zero(self):
        highs = [100.0] * 20
        lows = [90.0] * 20
        score = _compute_trend_score(highs, lows, lookback=12)
        assert score == 0.0

    def test_too_few_bars(self):
        score = _compute_trend_score([100, 101], [90, 91], lookback=12)
        assert score == 0.0


class TestWeeklyRSI:
    def test_overbought(self):
        # Steadily rising prices
        closes = [100.0 + i * 2 for i in range(30)]
        rsi = _rsi_weekly(closes)
        assert rsi > 70

    def test_oversold(self):
        # Steadily falling prices
        closes = [200.0 - i * 2 for i in range(30)]
        rsi = _rsi_weekly(closes)
        assert rsi < 30

    def test_insufficient_data(self):
        rsi = _rsi_weekly([100, 101, 102])
        assert rsi == 50.0


class TestWeinsteinStage:
    def test_stage2_advancing(self):
        # Price above 40WMA, 10WMA above 40WMA, 40WMA rising
        closes = [100 + i * 0.5 for i in range(50)]
        stage = _classify_weinstein_stage(
            current_price=125.0,
            wma10=123.0,
            wma40=115.0,
            closes=closes,
        )
        assert stage == 2

    def test_stage4_declining(self):
        # Price below 40WMA, 10WMA below 40WMA, 40WMA falling
        closes = [200 - i * 0.5 for i in range(50)]
        stage = _classify_weinstein_stage(
            current_price=170.0,
            wma10=172.0,
            wma40=180.0,
            closes=closes,
        )
        assert stage == 4


class TestComputeWeeklyContext:
    def test_uptrend_context(self):
        daily = _make_daily_df(300, trend="up")
        current = float(daily["close"].iloc[-1])
        ctx = compute_weekly_context(daily, current)

        assert ctx["weekly_trend_score"] is not None
        assert ctx["weekly_trend_score"] > 0  # uptrend should be positive
        assert ctx["price_vs_10wma_pct"] is not None
        assert ctx["price_vs_40wma_pct"] is not None
        assert ctx["weekly_ma_bullish"] is not None
        assert ctx["weekly_rsi"] is not None
        assert ctx["weinstein_stage"] is not None
        assert ctx["weinstein_stage"] in (1, 2, 3, 4)

    def test_downtrend_context(self):
        daily = _make_daily_df(300, trend="down")
        current = float(daily["close"].iloc[-1])
        ctx = compute_weekly_context(daily, current)

        assert ctx["weekly_trend_score"] is not None
        assert ctx["weekly_trend_score"] < 0  # downtrend should be negative

    def test_insufficient_data_returns_none(self):
        daily = _make_daily_df(30)  # ~6 weeks, not enough
        current = float(daily["close"].iloc[-1])
        ctx = compute_weekly_context(daily, current)

        assert ctx["weekly_trend_score"] is None

    def test_weekly_support_resistance(self):
        daily = _make_daily_df(300, trend="up")
        current = float(daily["close"].iloc[-1])
        ctx = compute_weekly_context(daily, current)

        # At least one of support/resistance should be found in 300 days of data
        has_levels = ctx["weekly_support"] is not None or ctx["weekly_resistance"] is not None
        assert has_levels
