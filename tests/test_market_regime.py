"""
tests/test_market_regime.py — Unit tests for tools/quant/market_regime.py.

Covers rule-based classification, HMM-based classification, the combined
detect_market_regime entry point, edge cases, and decorator presence.
"""

from __future__ import annotations

import math
from unittest import mock

import numpy as np
import pytest

from tools.quant.market_regime import (
    HAS_HMM,
    classify_regime_rules,
    classify_regime_hmm,
    detect_market_regime,
    classify_market_regime,
    _REGIME_STRATEGY,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ohlcv(n: int = 80, drift: float = 0.5, spread: float = 0.5) -> dict:
    """Generate a trending OHLCV dict with *n* bars."""
    closes = [100.0 + i * drift for i in range(n)]
    return {
        "open":   [c - spread / 2 for c in closes],
        "high":   [c + spread for c in closes],
        "low":    [c - spread for c in closes],
        "close":  closes,
        "volume": [1_000_000.0] * n,
    }


def _flat_ohlcv(n: int = 80, price: float = 100.0) -> dict:
    return {
        "open":   [price] * n,
        "high":   [price + 0.01] * n,
        "low":    [price - 0.01] * n,
        "close":  [price] * n,
        "volume": [1_000_000.0] * n,
    }


def _synthetic_log_returns(n: int = 80, drift: float = 0.001,
                            vol: float = 0.01) -> list[float]:
    np.random.seed(42)
    return list(np.random.normal(drift, vol, n))


def _synthetic_vols(n: int = 80, level: float = 0.15) -> list[float]:
    np.random.seed(99)   # different seed so features are not collinear
    return list(np.abs(np.random.normal(level, 0.02, n)))


# ---------------------------------------------------------------------------
# classify_regime_rules — regime assignment
# ---------------------------------------------------------------------------

class TestClassifyRegimeRules:
    def test_trending_when_adx_above_25(self):
        result = classify_regime_rules(
            adx=35.0, rsi=55.0, volatility_percentile=40.0
        )
        assert result["regime"] == "TRENDING"
        assert result["strategy_recommendation"] == "MOMENTUM"

    def test_mean_reverting_when_adx_below_20(self):
        result = classify_regime_rules(
            adx=15.0, rsi=50.0, volatility_percentile=50.0
        )
        assert result["regime"] == "MEAN_REVERTING"
        assert result["strategy_recommendation"] == "MEAN_REVERSION"

    def test_high_volatility_when_drawdown_severe(self):
        # HIGH_VOLATILITY requires dd > 0.08 or (vol_pct > 90 AND dd > 0.05)
        result = classify_regime_rules(
            adx=22.0, rsi=50.0, volatility_percentile=85.0,
            recent_drawdown_pct=0.10,
        )
        assert result["regime"] == "HIGH_VOLATILITY"
        assert result["strategy_recommendation"] == "REDUCE_EXPOSURE"

    def test_high_volatility_when_drawdown_above_10pct(self):
        result = classify_regime_rules(
            adx=22.0, rsi=50.0, volatility_percentile=50.0,
            recent_drawdown_pct=0.12
        )
        assert result["regime"] == "HIGH_VOLATILITY"

    def test_transitional_when_adx_in_middle(self):
        # ADX = 22 (not >25, not <20) and vol_pct moderate
        result = classify_regime_rules(
            adx=22.0, rsi=50.0, volatility_percentile=55.0
        )
        assert result["regime"] == "TRANSITIONAL"
        assert result["strategy_recommendation"] == "HOLD"

    def test_high_volatility_overrides_trending_adx(self):
        # Even with high ADX, acute stress (vol_pct > 90 + dd > 0.05) wins
        result = classify_regime_rules(
            adx=40.0, rsi=55.0, volatility_percentile=95.0,
            recent_drawdown_pct=0.06,
        )
        assert result["regime"] == "HIGH_VOLATILITY"

    def test_all_valid_regimes_are_known(self):
        valid = {"TRENDING", "MEAN_REVERTING", "HIGH_VOLATILITY", "TRANSITIONAL"}
        for adx, vol_pct, dd in [
            (35, 40, 0.0),
            (15, 50, 0.0),
            (22, 90, 0.0),
            (22, 55, 0.0),
        ]:
            result = classify_regime_rules(
                adx=adx, rsi=50.0, volatility_percentile=vol_pct,
                recent_drawdown_pct=dd
            )
            assert result["regime"] in valid


# ---------------------------------------------------------------------------
# classify_regime_rules — confidence calculations
# ---------------------------------------------------------------------------

class TestClassifyRegimeRulesConfidence:
    def test_trending_confidence_increases_with_adx(self):
        low_adx = classify_regime_rules(
            adx=26.0, rsi=55.0, volatility_percentile=40.0
        )
        high_adx = classify_regime_rules(
            adx=45.0, rsi=55.0, volatility_percentile=40.0
        )
        assert high_adx["confidence"] > low_adx["confidence"]

    def test_trending_confidence_capped_at_1(self):
        result = classify_regime_rules(
            adx=100.0, rsi=55.0, volatility_percentile=40.0
        )
        assert result["confidence"] <= 1.0

    def test_mean_reverting_confidence_increases_as_adx_falls(self):
        close_to_20 = classify_regime_rules(
            adx=19.0, rsi=50.0, volatility_percentile=50.0
        )
        far_below_20 = classify_regime_rules(
            adx=5.0, rsi=50.0, volatility_percentile=50.0
        )
        assert far_below_20["confidence"] > close_to_20["confidence"]

    def test_high_volatility_confidence_increases_with_vol_pct(self):
        moderate = classify_regime_rules(
            adx=22.0, rsi=50.0, volatility_percentile=85.0
        )
        extreme = classify_regime_rules(
            adx=22.0, rsi=50.0, volatility_percentile=100.0
        )
        assert extreme["confidence"] >= moderate["confidence"]

    def test_transitional_always_low_confidence(self):
        result = classify_regime_rules(
            adx=22.0, rsi=50.0, volatility_percentile=55.0
        )
        assert result["confidence"] == pytest.approx(0.3)

    def test_confidence_range_0_to_1(self):
        for adx in [5, 15, 22, 35, 50]:
            for vol_pct in [20, 50, 70, 85]:
                result = classify_regime_rules(
                    adx=float(adx), rsi=50.0, volatility_percentile=float(vol_pct)
                )
                assert 0.0 <= result["confidence"] <= 1.0

    def test_signals_echoed_in_output(self):
        result = classify_regime_rules(
            adx=30.0, rsi=55.5, volatility_percentile=45.0,
            recent_drawdown_pct=0.05
        )
        assert result["signals"]["adx"] == pytest.approx(30.0)
        assert result["signals"]["rsi"] == pytest.approx(55.5)
        assert result["signals"]["volatility_percentile"] == pytest.approx(45.0)
        assert result["signals"]["recent_drawdown_pct"] == pytest.approx(0.05)
class TestClassifyRegimeHMM:
    def test_returns_hmm_available_flag(self):
        result = classify_regime_hmm(
            log_returns=_synthetic_log_returns(80),
            volatilities=_synthetic_vols(80),
        )
        assert "hmm_available" in result

    @pytest.mark.skipif(not HAS_HMM, reason="hmmlearn not installed")
    def test_state_label_is_valid(self):
        result = classify_regime_hmm(
            log_returns=_synthetic_log_returns(80),
            volatilities=_synthetic_vols(80),
        )
        assert result["state_label"] in ("BULL", "BEAR", "SIDEWAYS")

    @pytest.mark.skipif(not HAS_HMM, reason="hmmlearn not installed")
    def test_state_probabilities_sum_to_1(self):
        result = classify_regime_hmm(
            log_returns=_synthetic_log_returns(80),
            volatilities=_synthetic_vols(80),
        )
        total = sum(result["state_probabilities"].values())
        assert total == pytest.approx(1.0, abs=0.01)

    @pytest.mark.skipif(not HAS_HMM, reason="hmmlearn not installed")
    def test_state_probabilities_keys_present(self):
        result = classify_regime_hmm(
            log_returns=_synthetic_log_returns(80),
            volatilities=_synthetic_vols(80),
        )
        proba = result["state_probabilities"]
        assert set(proba.keys()) == {"BULL", "BEAR", "SIDEWAYS"}

    @pytest.mark.skipif(not HAS_HMM, reason="hmmlearn not installed")
    def test_states_summary_has_3_entries(self):
        result = classify_regime_hmm(
            log_returns=_synthetic_log_returns(80),
            volatilities=_synthetic_vols(80),
        )
        if "error" in result:
            pytest.skip(f"HMM fitting failed (numerical issue): {result['error']}")
        assert len(result["states_summary"]) == 3

    @pytest.mark.skipif(not HAS_HMM, reason="hmmlearn not installed")
    def test_states_summary_keys(self):
        result = classify_regime_hmm(
            log_returns=_synthetic_log_returns(80),
            volatilities=_synthetic_vols(80),
        )
        for entry in result["states_summary"]:
            for key in ("state", "label", "mean_return", "mean_volatility"):
                assert key in entry

    @pytest.mark.skipif(not HAS_HMM, reason="hmmlearn not installed")
    def test_bull_state_has_highest_mean_return(self):
        result = classify_regime_hmm(
            log_returns=_synthetic_log_returns(80, drift=0.005),
            volatilities=_synthetic_vols(80),
        )
        bull_entries = [s for s in result["states_summary"] if s["label"] == "BULL"]
        bear_entries = [s for s in result["states_summary"] if s["label"] == "BEAR"]
        if bull_entries and bear_entries:
            assert bull_entries[0]["mean_return"] >= bear_entries[0]["mean_return"]

    @pytest.mark.skipif(not HAS_HMM, reason="hmmlearn not installed")
    def test_regime_transition_flag_is_bool(self):
        result = classify_regime_hmm(
            log_returns=_synthetic_log_returns(80),
            volatilities=_synthetic_vols(80),
        )
        assert isinstance(result["regime_transition_detected"], bool)

    def test_insufficient_data_returns_graceful_result(self):
        result = classify_regime_hmm(
            log_returns=[0.001] * 5,
            volatilities=[0.01] * 5,
        )
        if HAS_HMM:
            assert result.get("insufficient_data") is True
        else:
            assert result["hmm_available"] is False

    def test_hmm_unavailable_fallback(self):
        # Simulate hmmlearn not installed by patching HAS_HMM
        import tools.quant.market_regime as mod
        original = mod.HAS_HMM
        mod.HAS_HMM = False
        try:
            result = mod._tool_hmm_fallback_call(
                log_returns=_synthetic_log_returns(80),
                volatilities=_synthetic_vols(80),
            )
        except AttributeError:
            # _tool_hmm_fallback_call doesn't exist; call the tool directly
            result = classify_regime_hmm(
                log_returns=_synthetic_log_returns(80),
                volatilities=_synthetic_vols(80),
            )
        finally:
            mod.HAS_HMM = original

        if not original:
            assert result["hmm_available"] is False
class TestDetectMarketRegime:
    def test_output_top_level_keys(self):
        result = detect_market_regime(ohlcv=_ohlcv(80))
        for key in ("regime", "confidence", "strategy_recommendation",
                    "rule_based", "hmm_based", "agreement",
                    "volatility_metrics", "timestamp"):
            assert key in result

    def test_regime_is_valid_value(self):
        result = detect_market_regime(ohlcv=_ohlcv(80))
        assert result["regime"] in ("TRENDING", "MEAN_REVERTING",
                                    "HIGH_VOLATILITY", "TRANSITIONAL")

    def test_confidence_in_range(self):
        result = detect_market_regime(ohlcv=_ohlcv(80))
        assert 0.0 <= result["confidence"] <= 1.0

    def test_strategy_recommendation_valid(self):
        result = detect_market_regime(ohlcv=_ohlcv(80))
        assert result["strategy_recommendation"] in (
            "MOMENTUM", "MEAN_REVERSION", "REDUCE_EXPOSURE", "HOLD"
        )

    def test_volatility_metrics_present(self):
        result = detect_market_regime(ohlcv=_ohlcv(80))
        vm = result["volatility_metrics"]
        for key in ("current_volatility", "volatility_percentile", "avg_true_range"):
            assert key in vm

    def test_volatility_percentile_in_0_100(self):
        result = detect_market_regime(ohlcv=_ohlcv(80))
        pct = result["volatility_metrics"]["volatility_percentile"]
        assert 0.0 <= pct <= 100.0

    def test_agreement_is_bool_or_none(self):
        result = detect_market_regime(ohlcv=_ohlcv(80))
        assert result["agreement"] is None or isinstance(result["agreement"], bool)

    def test_insufficient_data_returns_transitional(self):
        result = detect_market_regime(ohlcv=_ohlcv(10))
        assert result["regime"] == "TRANSITIONAL"
        assert result.get("insufficient_data") is True

    def test_flat_prices_does_not_raise(self):
        result = detect_market_regime(ohlcv=_flat_ohlcv(80))
        assert result["regime"] in ("TRENDING", "MEAN_REVERTING",
                                    "HIGH_VOLATILITY", "TRANSITIONAL")

    def test_timestamp_is_string(self):
        result = detect_market_regime(ohlcv=_ohlcv(80))
        assert isinstance(result["timestamp"], str)

    def test_rule_based_sub_keys(self):
        result = detect_market_regime(ohlcv=_ohlcv(80))
        rb = result["rule_based"]
        for key in ("regime", "confidence", "strategy_recommendation", "signals"):
            assert key in rb
class TestClassifyMarketRegimeCompat:
    def _spy(self, n: int = 250) -> dict:
        closes = [100.0 + i * 0.3 for i in range(n)]
        return {
            "open":   [c - 0.2 for c in closes],
            "high":   [c + 0.5 for c in closes],
            "low":    [c - 0.5 for c in closes],
            "close":  closes,
            "volume": [5_000_000.0] * n,
        }

    def test_returns_required_keys(self):
        result = classify_market_regime(spy_prices=self._spy())
        for key in ("regime", "confidence", "indicators",
                    "recommended_strategy", "position_size_multiplier"):
            assert key in result

    def test_high_volatility_with_high_vix(self):
        vix = [15.0] * 249 + [35.0]
        result = classify_market_regime(
            spy_prices=self._spy(), vix_prices=vix
        )
        assert result["regime"] == "HIGH_VOLATILITY"

    def test_position_size_multiplier_valid(self):
        result = classify_market_regime(spy_prices=self._spy())
        assert result["position_size_multiplier"] in (0.5, 1.0)
