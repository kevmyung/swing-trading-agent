"""
tests/test_technical.py — Unit tests for tools/quant/technical.py.

Tests each indicator function independently, covers edge cases (empty input,
insufficient data), validates known-value outputs, and verifies that
calculate_all_indicators combines everything correctly.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from tools.quant.technical import (
    HAS_TALIB,
    calculate_all_indicators,
    calculate_technical_indicators,
    _rsi,
    _bollinger_bands,
    _adx,
    _atr,
    _macd,
)


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

def _trending(n: int = 60, start: float = 100.0, drift: float = 0.5) -> list[float]:
    """Monotonically increasing price series (no randomness for reproducibility)."""
    return [start + i * drift for i in range(n)]


def _constant(n: int = 60, price: float = 100.0) -> list[float]:
    return [price] * n


def _make_ohlcv(closes: list[float], spread: float = 0.5) -> dict:
    return {
        "open": [c - spread / 2 for c in closes],
        "high": [c + spread for c in closes],
        "low": [c - spread for c in closes],
        "close": closes,
        "volume": [1_000_000.0] * len(closes),
    }
class TestCalculateAllIndicators:
    def _ohlcv(self, n=60, drift=0.5):
        closes = _trending(n, drift=drift)
        return _make_ohlcv(closes)

    def test_output_top_level_keys(self):
        result = calculate_all_indicators(ohlcv=self._ohlcv())
        for key in ("rsi", "bollinger", "adx", "atr", "macd", "timestamp"):
            assert key in result

    def test_rsi_sub_keys(self):
        result = calculate_all_indicators(ohlcv=self._ohlcv())
        for key in ("rsi", "overbought", "oversold", "period"):
            assert key in result["rsi"]

    def test_bollinger_sub_keys(self):
        result = calculate_all_indicators(ohlcv=self._ohlcv())
        for key in ("upper", "middle", "lower", "bandwidth", "percent_b"):
            assert key in result["bollinger"]

    def test_atr_sub_keys(self):
        result = calculate_all_indicators(ohlcv=self._ohlcv())
        for key in ("atr", "atr_percent", "period"):
            assert key in result["atr"]

    def test_macd_sub_keys(self):
        result = calculate_all_indicators(ohlcv=self._ohlcv())
        for key in ("macd", "signal", "histogram", "bullish_crossover", "bearish_crossover"):
            assert key in result["macd"]

    def test_timestamp_is_string(self):
        result = calculate_all_indicators(ohlcv=self._ohlcv())
        assert isinstance(result["timestamp"], str)

    def test_empty_ohlcv_returns_insufficient_data(self):
        result = calculate_all_indicators(ohlcv={
            "open": [], "high": [], "low": [], "close": [], "volume": []
        })
        assert result["rsi"].get("insufficient_data") is True
        assert result["atr"].get("insufficient_data") is True
class TestCalculateTechnicalIndicators:
    def test_returns_dict_per_ticker(self):
        ohlcv = _make_ohlcv(_trending(60))
        result = calculate_technical_indicators(
            ticker_ohlcv={"AAPL": ohlcv}
        )
        assert "AAPL" in result

    def test_required_output_keys(self):
        ohlcv = _make_ohlcv(_trending(60))
        result = calculate_technical_indicators(
            ticker_ohlcv={"AAPL": ohlcv}
        )
        aapl = result["AAPL"]
        for key in ("current_price", "rsi_14", "macd", "bollinger", "atr_14", "suggested_stop_loss"):
            assert key in aapl

    def test_stop_loss_below_price(self):
        ohlcv = _make_ohlcv(_trending(60))
        result = calculate_technical_indicators(
            ticker_ohlcv={"AAPL": ohlcv}
        )
        aapl = result["AAPL"]
        assert aapl["suggested_stop_loss"] < aapl["current_price"]

    def test_rsi_in_valid_range(self):
        ohlcv = _make_ohlcv(_trending(60))
        result = calculate_technical_indicators(
            ticker_ohlcv={"AAPL": ohlcv}
        )
        assert 0 <= result["AAPL"]["rsi_14"] <= 100

    def test_bollinger_price_position_valid(self):
        ohlcv = _make_ohlcv(_trending(60))
        result = calculate_technical_indicators(
            ticker_ohlcv={"AAPL": ohlcv}
        )
        assert result["AAPL"]["bollinger"]["price_position"] in ("upper", "middle", "lower")

    def test_insufficient_data_skipped(self):
        short_ohlcv = _make_ohlcv([100.0] * 5)
        result = calculate_technical_indicators(
            ticker_ohlcv={"SHORT": short_ohlcv}
        )
        assert "SHORT" not in result

    def test_multiple_tickers_all_present(self):
        ohlcv = _make_ohlcv(_trending(60))
        result = calculate_technical_indicators(
            ticker_ohlcv={"AAPL": ohlcv, "MSFT": ohlcv}
        )
        assert "AAPL" in result and "MSFT" in result

    def test_macd_crossover_label_valid(self):
        ohlcv = _make_ohlcv(_trending(60))
        result = calculate_technical_indicators(
            ticker_ohlcv={"AAPL": ohlcv}
        )
        assert result["AAPL"]["macd"]["crossover"] in ("bullish", "bearish", "none")


# ---------------------------------------------------------------------------
# TA-Lib vs fallback consistency (only run when TA-Lib IS available)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_TALIB, reason="TA-Lib not installed")
class TestTalibVsFallback:
    """Verify TA-Lib and pandas fallback produce similar results."""

    def test_rsi_similar(self):
        import tools.quant.technical as mod
        prices = _trending(60)
        # Force talib path
        talib_result = mod._rsi(prices)
        # Force fallback path by temporarily disabling HAS_TALIB
        mod.HAS_TALIB = False
        try:
            fallback_result = mod._rsi(prices)
        finally:
            mod.HAS_TALIB = True
        assert abs(talib_result["rsi"] - fallback_result["rsi"]) < 5.0

    def test_bollinger_similar(self):
        import tools.quant.technical as mod
        prices = _trending(40)
        talib_result = mod._bollinger_bands(prices)
        mod.HAS_TALIB = False
        try:
            fallback_result = mod._bollinger_bands(prices)
        finally:
            mod.HAS_TALIB = True
        assert abs(talib_result["middle"] - fallback_result["middle"]) < 1e-3

    def test_atr_similar(self):
        import tools.quant.technical as mod
        closes = _trending(40)
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        talib_result = mod._atr(highs, lows, closes)
        mod.HAS_TALIB = False
        try:
            fallback_result = mod._atr(highs, lows, closes)
        finally:
            mod.HAS_TALIB = True
        assert abs(talib_result["atr"] - fallback_result["atr"]) < 0.5
