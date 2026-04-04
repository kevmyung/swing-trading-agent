"""
tests/test_market_breadth.py — Unit tests for market breadth indicators.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.quant.market_breadth import (
    compute_market_breadth,
    ALL_BREADTH_TICKERS,
    SECTOR_TICKERS,
    _period_return,
    _relative_return,
)


def _make_bars(n_days: int = 60, trend: float = 0.001) -> dict[str, pd.DataFrame]:
    """Generate synthetic daily bars for all breadth-relevant tickers."""
    dates = pd.bdate_range(end="2025-12-31", periods=n_days, freq="B")
    np.random.seed(42)
    bars: dict[str, pd.DataFrame] = {}

    all_tickers = ["SPY", "QQQ"] + ALL_BREADTH_TICKERS
    for i, ticker in enumerate(all_tickers):
        # Vary trend per ticker to create diversity
        ticker_trend = trend + (i - len(all_tickers) / 2) * 0.0002
        base = 100.0 + np.cumsum(np.random.normal(ticker_trend, 0.5, n_days))
        base = np.maximum(base, 10.0)
        bars[ticker] = pd.DataFrame(
            {
                "open": base - 0.3,
                "high": base + 0.5,
                "low": base - 0.5,
                "close": base,
                "volume": np.random.randint(1e6, 1e7, n_days).astype(float),
            },
            index=dates,
        )
    return bars


class TestPeriodReturn:
    def test_basic(self):
        closes = [100.0, 102.0, 104.0, 106.0, 108.0, 110.0]
        r = _period_return(closes, 5)
        assert r is not None
        assert abs(r - 0.10) < 0.001

    def test_insufficient_data(self):
        assert _period_return([100.0, 110.0], 5) is None

    def test_zero_base(self):
        assert _period_return([0.0, 100.0], 1) is None


class TestRelativeReturn:
    def test_outperformance(self):
        ticker = [100.0, 110.0]
        bench = [100.0, 105.0]
        r = _relative_return(ticker, bench, 1)
        assert r is not None
        assert r > 0  # ticker outperformed

    def test_underperformance(self):
        ticker = [100.0, 102.0]
        bench = [100.0, 110.0]
        r = _relative_return(ticker, bench, 1)
        assert r is not None
        assert r < 0


class TestComputeMarketBreadth:
    def test_basic_output_structure(self):
        bars = _make_bars()
        result = compute_market_breadth(bars)

        assert "breadth_score" in result
        assert "rsp_vs_spy_5d" in result
        assert "sectors_positive_5d" in result
        assert "sectors_positive_20d" in result
        assert "sector_momentum" in result
        assert "iwm_vs_spy_5d" in result
        assert "credit_trend" in result

    def test_breadth_score_range(self):
        bars = _make_bars()
        result = compute_market_breadth(bars)
        assert -1.0 <= result["breadth_score"] <= 1.0

    def test_sector_momentum_has_ranks(self):
        bars = _make_bars()
        result = compute_market_breadth(bars)
        sm = result["sector_momentum"]
        if sm:
            ranks = [v["rank"] for v in sm.values()]
            assert min(ranks) == 1
            assert len(set(ranks)) == len(ranks)  # unique ranks

    def test_sector_count(self):
        bars = _make_bars()
        result = compute_market_breadth(bars)
        if result["sectors_positive_5d"] is not None:
            assert 0 <= result["sectors_positive_5d"] <= len(SECTOR_TICKERS)

    def test_credit_trend_values(self):
        bars = _make_bars()
        result = compute_market_breadth(bars)
        if result["credit_trend"] is not None:
            assert result["credit_trend"] in ("improving", "stable", "deteriorating")

    def test_no_spy_returns_defaults(self):
        result = compute_market_breadth({})
        assert result["breadth_score"] == 0.0
        assert result["rsp_vs_spy_5d"] is None

    def test_broad_rally_positive_breadth(self):
        """When all tickers trend up, breadth should be positive."""
        bars = _make_bars(trend=0.005)  # strong positive trend for all
        result = compute_market_breadth(bars)
        # Most sectors should be positive
        if result["sectors_positive_5d"] is not None:
            assert result["sectors_positive_5d"] >= 5

    def test_narrow_rally_weaker_breadth(self):
        """When SPY rises but most sectors fall, breadth should weaken."""
        bars = _make_bars(trend=-0.003)  # negative trend base
        # Make SPY strongly positive
        n = len(bars["SPY"])
        bars["SPY"]["close"] = 100.0 + np.cumsum(np.random.normal(0.5, 0.3, n))
        result = compute_market_breadth(bars)
        # RSP should underperform SPY
        if result["rsp_vs_spy_5d"] is not None:
            assert result["rsp_vs_spy_5d"] < 0.05  # not strongly outperforming
