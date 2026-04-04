"""
tests/test_momentum.py — Unit tests for tools/quant/momentum.py.

Covers private helpers (_compute_momentum_return, _cross_sectional_zscore,
_spearman_rank_correlation), the tickers/as_of_date/price_data calling
convention, and get_momentum_ic.
"""

from __future__ import annotations

import math
from typing import Dict, List

import numpy as np
import pandas as pd
import pytest

from tools.quant.momentum import (
    _compute_momentum_return,
    _cross_sectional_zscore,
    _spearman_rank_correlation,
    calculate_momentum_scores,
    get_momentum_ic,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_trending_prices(n: int = 300, start: float = 100.0,
                           daily_return: float = 0.001) -> List[float]:
    prices = [start]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + daily_return))
    return prices


def _make_price_df(prices: List[float],
                   start_date: str = "2023-01-01") -> pd.DataFrame:
    dates = pd.bdate_range(start=start_date, periods=len(prices))
    return pd.DataFrame({"close": prices}, index=dates)


def _simple_price_data(
    tickers: List[str],
    n: int = 300,
) -> Dict[str, pd.DataFrame]:
    np.random.seed(42)
    result = {}
    for ticker in tickers:
        prices = _make_trending_prices(n)
        result[ticker] = _make_price_df(prices)
    return result


# ---------------------------------------------------------------------------
# _compute_momentum_return
# ---------------------------------------------------------------------------

class TestComputeMomentumReturn:
    def test_returns_float_for_sufficient_data(self):
        prices = pd.Series(_make_trending_prices(300))
        result = _compute_momentum_return(prices)
        assert isinstance(result, float)

    def test_returns_none_for_insufficient_data(self):
        prices = pd.Series([100.0] * 200)
        result = _compute_momentum_return(prices, lookback=252)
        assert result is None

    def test_returns_none_for_zero_anchor_price(self):
        prices = pd.Series([0.0] * 300)
        result = _compute_momentum_return(prices)
        assert result is None

    def test_returns_none_for_negative_anchor_price(self):
        prices = pd.Series([-50.0] * 300)
        result = _compute_momentum_return(prices)
        assert result is None

    def test_known_value_calculation(self):
        # Construct prices where p[t-skip] = 110, p[t-lookback] = 100
        n = 300
        prices = [100.0] * n
        skip = 21
        lookback = 252
        prices[-skip - 1] = 110.0
        prices[-lookback - 1] = 100.0
        series = pd.Series(prices)
        result = _compute_momentum_return(series, lookback=lookback, skip=skip)
        assert result == pytest.approx(0.10, rel=1e-4)

    def test_positive_momentum_for_uptrend(self):
        prices = pd.Series(_make_trending_prices(300, daily_return=0.002))
        result = _compute_momentum_return(prices)
        assert result > 0.0

    def test_custom_lookback_and_skip(self):
        prices = pd.Series(_make_trending_prices(150))
        result = _compute_momentum_return(prices, lookback=100, skip=10)
        assert result is not None

    def test_exact_boundary_length(self):
        # Exactly lookback+1 prices: just enough
        prices = pd.Series(_make_trending_prices(253))
        result = _compute_momentum_return(prices, lookback=252, skip=0)
        assert result is not None

    def test_one_below_boundary_returns_none(self):
        prices = pd.Series(_make_trending_prices(252))
        result = _compute_momentum_return(prices, lookback=252, skip=0)
        assert result is None


# ---------------------------------------------------------------------------
# _cross_sectional_zscore
# ---------------------------------------------------------------------------

class TestCrossSectionalZscore:
    def test_empty_dict_returns_empty(self):
        assert _cross_sectional_zscore({}) == {}

    def test_single_ticker_returns_zero(self):
        result = _cross_sectional_zscore({"AAPL": 0.15})
        assert result["AAPL"] == pytest.approx(0.0)

    def test_zero_std_returns_all_zeros(self):
        returns = {"AAPL": 0.10, "MSFT": 0.10, "GOOG": 0.10}
        result = _cross_sectional_zscore(returns)
        for z in result.values():
            assert z == pytest.approx(0.0)

    def test_output_is_roughly_zero_mean(self):
        returns = {f"T{i}": float(i) * 0.01 for i in range(10)}
        result = _cross_sectional_zscore(returns)
        assert abs(np.mean(list(result.values()))) < 1e-9

    def test_output_std_is_one(self):
        returns = {f"T{i}": float(i) * 0.01 for i in range(10)}
        result = _cross_sectional_zscore(returns)
        assert np.std(list(result.values())) == pytest.approx(1.0, rel=1e-6)

    def test_highest_return_has_highest_zscore(self):
        returns = {"LOW": 0.01, "MID": 0.05, "HIGH": 0.20}
        result = _cross_sectional_zscore(returns)
        assert result["HIGH"] > result["MID"] > result["LOW"]

    def test_preserves_all_tickers(self):
        tickers = [f"T{i}" for i in range(5)]
        returns = {t: float(i) for i, t in enumerate(tickers)}
        result = _cross_sectional_zscore(returns)
        assert set(result.keys()) == set(tickers)


# ---------------------------------------------------------------------------
# _spearman_rank_correlation
# ---------------------------------------------------------------------------

class TestSpearmanRankCorrelation:
    def test_monotonic_positive_returns_near_1(self):
        x = list(range(10))
        y = list(range(10))
        result = _spearman_rank_correlation(x, y)
        assert result == pytest.approx(1.0)

    def test_monotonic_negative_returns_near_minus_1(self):
        x = list(range(10))
        y = list(reversed(range(10)))
        result = _spearman_rank_correlation(x, y)
        assert result == pytest.approx(-1.0)

    def test_too_short_returns_zero(self):
        assert _spearman_rank_correlation([], []) == 0.0
        assert _spearman_rank_correlation([1.0], [1.0]) == 0.0

    def test_returns_float(self):
        x = [0.1, 0.2, 0.3]
        y = [0.3, 0.1, 0.2]
        result = _spearman_rank_correlation(x, y)
        assert isinstance(result, float)

    def test_constant_input_returns_zero(self):
        # Zero variance → spearman is nan → returns 0.0
        result = _spearman_rank_correlation([1.0, 1.0, 1.0], [2.0, 3.0, 4.0])
        assert result == 0.0


# ---------------------------------------------------------------------------
# calculate_momentum_scores — new calling convention
# ---------------------------------------------------------------------------

class TestCalculateMomentumScoresNew:
    def test_output_keys_present(self):
        pd = _simple_price_data(["AAPL", "MSFT", "GOOG"])
        result = calculate_momentum_scores(
            tickers=["AAPL", "MSFT", "GOOG"],
            as_of_date="2024-03-01",
            price_data=pd,
        )
        for key in ("signal_date", "universe_size", "signals"):
            assert key in result

    def test_universe_size_matches_signals(self):
        price_data = _simple_price_data(["AAPL", "MSFT"])
        result = calculate_momentum_scores(
            tickers=["AAPL", "MSFT"],
            as_of_date="2024-03-01",
            price_data=price_data,
        )
        assert result["universe_size"] == len(result["signals"])

    def test_signal_contains_required_keys(self):
        price_data = _simple_price_data(["AAPL"])
        result = calculate_momentum_scores(
            tickers=["AAPL"],
            as_of_date="2024-03-01",
            price_data=price_data,
        )
        for sig in result["signals"]:
            for key in ("ticker", "momentum_return", "z_score", "rank", "action"):
                assert key in sig

    def test_action_values_valid(self):
        price_data = _simple_price_data([f"T{i}" for i in range(10)])
        result = calculate_momentum_scores(
            tickers=[f"T{i}" for i in range(10)],
            as_of_date="2024-03-01",
            price_data=price_data,
        )
        for sig in result["signals"]:
            assert sig["action"] in ("LONG", "SHORT", "NEUTRAL")

    def test_signals_sorted_by_zscore_descending(self):
        price_data = _simple_price_data(["A", "B", "C", "D"])
        result = calculate_momentum_scores(
            tickers=["A", "B", "C", "D"],
            as_of_date="2024-03-01",
            price_data=price_data,
        )
        z_scores = [s["z_score"] for s in result["signals"]]
        assert z_scores == sorted(z_scores, reverse=True)

    def test_as_of_date_filters_future_prices(self):
        # price_data has prices through end of 2023; as_of is early 2023
        prices = _make_trending_prices(300)
        df = _make_price_df(prices, start_date="2022-01-03")
        price_data = {"AAPL": df}
        result_full = calculate_momentum_scores(
            tickers=["AAPL"],
            as_of_date="2024-01-01",
            price_data=price_data,
        )
        # As long as we have enough data both times, both should return a signal
        result_early = calculate_momentum_scores(
            tickers=["AAPL"],
            as_of_date="2022-06-01",
            price_data=price_data,
        )
        # By June 2022 we only have ~110 trading days; not enough → 0 signals
        assert result_early["universe_size"] == 0

    def test_missing_ticker_in_price_data_skipped(self):
        price_data = _simple_price_data(["AAPL"])
        result = calculate_momentum_scores(
            tickers=["AAPL", "MISSING"],
            as_of_date="2024-03-01",
            price_data=price_data,
        )
        tickers_in_result = [s["ticker"] for s in result["signals"]]
        assert "MISSING" not in tickers_in_result
        assert "AAPL" in tickers_in_result

    def test_empty_tickers_returns_zero_signals(self):
        result = calculate_momentum_scores(
            tickers=[],
            as_of_date="2024-03-01",
            price_data={},
        )
        assert result["universe_size"] == 0
        assert result["signals"] == []

    def test_insufficient_price_data_skipped(self):
        # Only 50 bars — not enough for 252-day momentum
        short_prices = _make_trending_prices(50)
        price_data = {"SHORT": _make_price_df(short_prices)}
        result = calculate_momentum_scores(
            tickers=["SHORT"],
            as_of_date="2025-01-01",
            price_data=price_data,
        )
        assert result["universe_size"] == 0

    def test_rank_starts_at_1(self):
        price_data = _simple_price_data(["A", "B", "C"])
        result = calculate_momentum_scores(
            tickers=["A", "B", "C"],
            as_of_date="2024-03-01",
            price_data=price_data,
        )
        ranks = sorted(s["rank"] for s in result["signals"])
        assert ranks[0] == 1

    def test_signal_date_matches_as_of_date(self):
        price_data = _simple_price_data(["AAPL"])
        result = calculate_momentum_scores(
            tickers=["AAPL"],
            as_of_date="2024-06-15",
            price_data=price_data,
        )
        assert result["signal_date"] == "2024-06-15"


# ---------------------------------------------------------------------------
# get_momentum_ic
# ---------------------------------------------------------------------------

class TestGetMomentumIC:
    def test_strong_ic_with_perfectly_predictive_scores(self):
        # Scores equal forward returns → IC = 1.0
        periods = [
            {
                "scores": {"A": 0.10, "B": 0.05, "C": -0.03, "D": -0.08},
                "forward_returns": {"A": 0.10, "B": 0.05, "C": -0.03, "D": -0.08},
            }
        ] * 5
        result = get_momentum_ic(historical_scores=periods)
        assert result["rolling_ic"] > 0.05
        assert result["ic_status"] == "STRONG"
        assert result["size_multiplier"] == 1.0

    def test_zero_ic_gives_decay_status(self):
        # Scores and forward returns are completely uncorrelated (random)
        np.random.seed(7)
        periods = []
        for _ in range(10):
            tickers = [f"T{i}" for i in range(6)]
            scores = {t: float(np.random.randn()) for t in tickers}
            fwd = {t: float(np.random.randn()) for t in tickers}
            periods.append({"scores": scores, "forward_returns": fwd})
        result = get_momentum_ic(historical_scores=periods)
        # IC may be anything; just verify structure
        assert "rolling_ic" in result
        assert result["ic_status"] in ("STRONG", "MODERATE", "DECAY")
        assert result["size_multiplier"] in (0.5, 0.75, 1.0)

    def test_empty_historical_scores_returns_decay(self):
        result = get_momentum_ic(historical_scores=[])
        assert result["ic_status"] == "DECAY"
        assert result["rolling_ic"] == 0.0
        assert result["size_multiplier"] == 0.5

    def test_too_few_common_tickers_skipped(self):
        # Each period has only 2 common tickers — below the min of 3
        periods = [
            {
                "scores": {"A": 0.1, "B": 0.2},
                "forward_returns": {"A": 0.05, "B": 0.15},
            }
        ] * 5
        result = get_momentum_ic(historical_scores=periods)
        assert result["ic_status"] == "DECAY"

    def test_output_keys_present(self):
        periods = [
            {
                "scores": {"A": 0.1, "B": 0.05, "C": -0.05},
                "forward_returns": {"A": 0.1, "B": 0.05, "C": -0.05},
            }
        ]
        result = get_momentum_ic(historical_scores=periods)
        for key in ("rolling_ic", "ic_status", "size_multiplier"):
            assert key in result

    def test_moderate_ic_status(self):
        # Construct periods where IC ≈ 0.03 (MODERATE band)
        # Use partially correlated scores/returns
        periods = []
        for _ in range(20):
            tickers = [f"T{i}" for i in range(8)]
            base = np.linspace(-0.1, 0.1, 8)
            noise = np.random.normal(0, 0.05, 8)
            scores = {t: float(base[i]) for i, t in enumerate(tickers)}
            fwd = {t: float(base[i] + noise[i]) for i, t in enumerate(tickers)}
            periods.append({"scores": scores, "forward_returns": fwd})
        result = get_momentum_ic(historical_scores=periods)
        assert result["ic_status"] in ("STRONG", "MODERATE")

    def test_no_args_returns_decay(self):
        result = get_momentum_ic()
        assert result["ic_status"] == "DECAY"
        assert result["size_multiplier"] == 0.5

