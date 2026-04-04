"""
tests/test_mean_reversion.py — Unit tests for tools/quant/mean_reversion.py.

Covers private helpers (_compute_zscore, _generate_signal,
_calculate_stop_and_target) and the tickers/as_of_date/price_data calling
convention of calculate_mean_reversion_signals.
"""

from __future__ import annotations

import math
from typing import Dict, List

import numpy as np
import pandas as pd
import pytest

from tools.quant.mean_reversion import (
    _compute_zscore,
    _generate_signal,
    _calculate_stop_and_target,
    calculate_mean_reversion_signals,
)


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------

def _make_oversold_prices(n: int = 100) -> List[float]:
    """Single large drop at the end: z << -2 and RSI ≈ 0 → LONG."""
    return [100.0] * (n - 1) + [65.0]


def _make_overbought_prices(n: int = 100) -> List[float]:
    """Overbought prices: alternating base seeds RSI avg_loss, then a spike.

    80 bars alternating ±1 → avg_loss > 0 in EWM.
    5 bars rising to 110 → avg_gain grows.
    14 bars flat at 110 → avg_loss non-zero via EWM decay.
    1 bar spike to 140  → z ≈ 4.0 > 2.0, RSI ≈ 87 > 70 → SHORT.
    """
    prices = [100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(80)]
    for i in range(5):
        prices.append(102.0 + i * 2.0)   # 102, 104, 106, 108, 110
    prices.extend([110.0] * 14)
    prices.append(140.0)
    return prices[:n]


def _make_mean_prices(n: int = 100) -> List[float]:
    """Full-cycle sine so rolling mean = 100 and current ≈ mean → EXIT."""
    # 5 complete 20-period sine cycles over n=100 bars.
    # Sum of sine over a full cycle is 0 ⇒ rolling mean = 100 exactly.
    # Last bar value ≈ 99.07, |z| ≈ 0.42 < 0.5 → EXIT.
    return [100.0 + 5.0 * math.sin(2 * math.pi * i / 20) for i in range(n)]


def _make_neutral_prices(n: int = 100) -> List[float]:
    """Sine shifted π/4 so last bar has 0.5 < |z| < 2.0 and RSI ≈ 50 → NEUTRAL."""
    return [100.0 + 4.0 * math.sin(2 * math.pi * i / 20 + math.pi / 4)
            for i in range(n)]


def _make_price_df(prices: List[float],
                   start_date: str = "2022-01-03") -> pd.DataFrame:
    """Wrap a price list in a DataFrame with a DatetimeIndex."""
    dates = pd.bdate_range(start=start_date, periods=len(prices))
    return pd.DataFrame({"close": prices}, index=dates)


_AS_OF = "2023-01-01"   # safely after all 100-bar price series that start 2022-01-03


# ---------------------------------------------------------------------------
# _compute_zscore
# ---------------------------------------------------------------------------

class TestComputeZscore:
    def test_negative_zscore_for_oversold(self):
        prices = np.array(_make_oversold_prices())
        z = _compute_zscore(prices, window=20)
        assert z is not None and z < -2.0

    def test_positive_zscore_for_overbought(self):
        prices = np.array(_make_overbought_prices())
        z = _compute_zscore(prices, window=20)
        assert z is not None and z > 2.0

    def test_near_zero_for_mean_prices(self):
        prices = np.array(_make_mean_prices())
        z = _compute_zscore(prices, window=20)
        assert z is not None and abs(z) < 1.0

    def test_returns_none_for_insufficient_data(self):
        prices = np.array([100.0] * 10)
        assert _compute_zscore(prices, window=20) is None

    def test_returns_none_for_constant_prices(self):
        prices = np.array([100.0] * 50)
        assert _compute_zscore(prices, window=20) is None

    def test_returns_float(self):
        prices = np.array(_make_neutral_prices())
        z = _compute_zscore(prices, window=20)
        assert z is None or isinstance(z, float)


# ---------------------------------------------------------------------------
# _generate_signal
# ---------------------------------------------------------------------------

class TestGenerateSignal:
    def test_long_when_oversold_and_low_rsi(self):
        action, strength = _generate_signal(z_score=-3.0, rsi=20.0, bb_percent_b=0.0)
        assert action == "LONG"
        assert strength > 0.0

    def test_short_when_overbought_and_high_rsi(self):
        action, strength = _generate_signal(z_score=3.0, rsi=80.0, bb_percent_b=1.1)
        assert action == "SHORT"
        assert strength > 0.0

    def test_exit_when_near_mean(self):
        action, strength = _generate_signal(z_score=0.3, rsi=50.0, bb_percent_b=0.5)
        assert action == "EXIT"
        assert strength == 0.0

    def test_exit_for_small_negative_z(self):
        action, strength = _generate_signal(z_score=-0.4, rsi=50.0, bb_percent_b=0.5)
        assert action == "EXIT"

    def test_neutral_when_z_mid_range(self):
        # |z| = 1.2: not extreme enough, RSI not extreme
        action, strength = _generate_signal(z_score=1.2, rsi=55.0, bb_percent_b=0.7)
        assert action == "NEUTRAL"
        assert strength == 0.0

    def test_neutral_when_z_oversold_but_rsi_not_low(self):
        # z < -2 but RSI = 50: confirmation missing → NEUTRAL
        action, _ = _generate_signal(z_score=-2.5, rsi=50.0, bb_percent_b=0.0)
        assert action == "NEUTRAL"

    def test_neutral_when_z_overbought_but_rsi_not_high(self):
        action, _ = _generate_signal(z_score=2.5, rsi=50.0, bb_percent_b=1.0)
        assert action == "NEUTRAL"

    def test_signal_strength_capped_at_1(self):
        _, strength = _generate_signal(z_score=-10.0, rsi=5.0, bb_percent_b=0.0)
        assert strength <= 1.0

    def test_signal_strength_proportional_to_z(self):
        _, s_mild = _generate_signal(z_score=-2.1, rsi=20.0, bb_percent_b=0.0)
        _, s_extreme = _generate_signal(z_score=-5.0, rsi=5.0, bb_percent_b=0.0)
        assert s_extreme > s_mild

    def test_long_boundary_z_exactly_minus2(self):
        # z = -2.0 is NOT strictly < -2.0 → NEUTRAL or EXIT
        action, _ = _generate_signal(z_score=-2.0, rsi=20.0, bb_percent_b=0.0)
        assert action in ("NEUTRAL", "EXIT")

class TestCalculateMRSignalsNew:
    def _price_data(self, tickers, prices_fn=None):
        if prices_fn is None:
            prices_fn = _make_oversold_prices
        return {t: _make_price_df(prices_fn()) for t in tickers}

    def test_oversold_generates_long(self):
        pd_data = {"AAPL": _make_price_df(_make_oversold_prices())}
        result = calculate_mean_reversion_signals(
            tickers=["AAPL"], as_of_date=_AS_OF, price_data=pd_data
        )
        assert result["signals"][0]["action"] == "LONG"

    def test_overbought_generates_short(self):
        pd_data = {"AAPL": _make_price_df(_make_overbought_prices())}
        result = calculate_mean_reversion_signals(
            tickers=["AAPL"], as_of_date=_AS_OF, price_data=pd_data
        )
        assert result["signals"][0]["action"] == "SHORT"

    def test_mean_prices_generates_exit(self):
        pd_data = {"AAPL": _make_price_df(_make_mean_prices())}
        result = calculate_mean_reversion_signals(
            tickers=["AAPL"], as_of_date=_AS_OF, price_data=pd_data
        )
        assert result["signals"][0]["action"] == "EXIT"

    def test_neutral_prices_generate_neutral(self):
        pd_data = {"AAPL": _make_price_df(_make_neutral_prices())}
        result = calculate_mean_reversion_signals(
            tickers=["AAPL"], as_of_date=_AS_OF, price_data=pd_data
        )
        assert result["signals"][0]["action"] == "NEUTRAL"

    def test_output_keys_present(self):
        pd_data = self._price_data(["AAPL"])
        result = calculate_mean_reversion_signals(
            tickers=["AAPL"], as_of_date=_AS_OF, price_data=pd_data
        )
        for key in ("signal_date", "universe_size", "signals"):
            assert key in result

    def test_signal_keys_present(self):
        pd_data = self._price_data(["AAPL"])
        result = calculate_mean_reversion_signals(
            tickers=["AAPL"], as_of_date=_AS_OF, price_data=pd_data
        )
        sig = result["signals"][0]
        for key in ("ticker", "z_score", "rsi", "bollinger_percent_b",
                    "action", "signal_strength", "entry_price",
                    "stop_loss", "take_profit"):
            assert key in sig

    def test_universe_size_matches_signals_count(self):
        pd_data = self._price_data(["A", "B", "C"])
        result = calculate_mean_reversion_signals(
            tickers=["A", "B", "C"], as_of_date=_AS_OF, price_data=pd_data
        )
        assert result["universe_size"] == len(result["signals"])

    def test_empty_tickers_returns_empty(self):
        result = calculate_mean_reversion_signals(
            tickers=[], as_of_date=_AS_OF, price_data={}
        )
        assert result["universe_size"] == 0
        assert result["signals"] == []

    def test_missing_ticker_skipped(self):
        pd_data = {"AAPL": _make_price_df(_make_oversold_prices())}
        result = calculate_mean_reversion_signals(
            tickers=["AAPL", "MISSING"], as_of_date=_AS_OF, price_data=pd_data
        )
        tickers_found = [s["ticker"] for s in result["signals"]]
        assert "MISSING" not in tickers_found

    def test_stop_loss_below_take_profit_for_long(self):
        # stop = mean - 3*std, target = mean  → stop is always below target
        pd_data = {"AAPL": _make_price_df(_make_oversold_prices())}
        result = calculate_mean_reversion_signals(
            tickers=["AAPL"], as_of_date=_AS_OF, price_data=pd_data
        )
        sig = result["signals"][0]
        assert sig["stop_loss"] < sig["take_profit"]

    def test_take_profit_above_entry_for_long(self):
        pd_data = {"AAPL": _make_price_df(_make_oversold_prices())}
        result = calculate_mean_reversion_signals(
            tickers=["AAPL"], as_of_date=_AS_OF, price_data=pd_data
        )
        sig = result["signals"][0]
        assert sig["take_profit"] > sig["entry_price"]

    def test_stop_loss_above_take_profit_for_short(self):
        # stop = mean + 3*std, target = mean  → stop is always above target
        pd_data = {"AAPL": _make_price_df(_make_overbought_prices())}
        result = calculate_mean_reversion_signals(
            tickers=["AAPL"], as_of_date=_AS_OF, price_data=pd_data
        )
        sig = result["signals"][0]
        assert sig["stop_loss"] > sig["take_profit"]

    def test_signal_date_matches_as_of_date(self):
        pd_data = self._price_data(["AAPL"])
        result = calculate_mean_reversion_signals(
            tickers=["AAPL"], as_of_date="2024-06-15", price_data=pd_data
        )
        assert result["signal_date"] == "2024-06-15"

    def test_as_of_date_filters_future_prices(self):
        # Price series has 100 bars starting 2022-01-03; as_of cuts off most of them
        pd_data = {"AAPL": _make_price_df(_make_oversold_prices())}
        # Using a very early as_of date leaves too few bars → ticker skipped
        result = calculate_mean_reversion_signals(
            tickers=["AAPL"], as_of_date="2022-02-01", price_data=pd_data
        )
        assert result["universe_size"] == 0

    def test_signal_strength_in_0_1(self):
        pd_data = self._price_data(["AAPL"])
        result = calculate_mean_reversion_signals(
            tickers=["AAPL"], as_of_date=_AS_OF, price_data=pd_data
        )
        for sig in result["signals"]:
            assert 0.0 <= sig["signal_strength"] <= 1.0
