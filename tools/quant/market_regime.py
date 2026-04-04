"""
tools/quant/market_regime.py — Market regime classification for the QuantAgent.

Two complementary approaches:

  1. Rule-based  (always available) — ADX + volatility percentile + drawdown
  2. HMM-based   (hmmlearn, optional) — Gaussian HMM on log-returns + realized vol

The ``classify_market_regime`` function is preserved for backward compatibility
with existing test_quant_tools.py tests.
"""

from __future__ import annotations

import logging
from datetime import timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

try:
    from hmmlearn import hmm as _hmm_mod
    HAS_HMM = True
except ImportError:
    HAS_HMM = False

try:
    import ta.trend
    _HAS_TA = True
except ImportError:  # pragma: no cover
    _HAS_TA = False

from tools.quant.technical import _adx, _rsi, _atr

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regime / strategy constants
# ---------------------------------------------------------------------------

_REGIME_STRATEGY: Dict[str, str] = {
    "TRENDING": "MOMENTUM",
    "MEAN_REVERTING": "MEAN_REVERSION",
    "HIGH_VOLATILITY": "REDUCE_EXPOSURE",
    "TRANSITIONAL": "HOLD",
}

# Map rule-based regimes to the HMM label considered equivalent
_REGIME_HMM_EQUIVALENT: Dict[str, str] = {
    "TRENDING": "BULL",
    "MEAN_REVERTING": "SIDEWAYS",
    "HIGH_VOLATILITY": "BEAR",
    "TRANSITIONAL": "",
}


# ---------------------------------------------------------------------------
# Rule-based classification
# ---------------------------------------------------------------------------

def classify_regime_rules(
    adx: float,
    rsi: float,
    volatility_percentile: float,
    recent_drawdown_pct: float = 0.0,
) -> dict:
    """Classify the current market regime using interpretable rules.

    Calibrated against SPY 2024-2026 distributions:
      - SPY ADX median ~22, P75 ~28, P90 ~34
      - 20-day drawdown: P75 ~4%, P90 ~6.6%, P95 ~9.7%
      - Forward-return validation: ADX 25-30 → +1.4%/20d win 82%

    Rules (evaluated in priority order):

    * ``HIGH_VOLATILITY`` — drawdown > 8% **or**
                            (volatility_percentile > 90 **and** drawdown > 5%)
    * ``TRENDING``        — ADX > 25
    * ``MEAN_REVERTING``  — ADX < 20 **and** volatility_percentile < 75
    * ``TRANSITIONAL``    — everything else (conflicting signals)

    Args:
        adx: ADX(14) value — directional movement strength.
        rsi: RSI(14) value — momentum oscillator 0–100.
        volatility_percentile: Current realized volatility as a percentile
            (0–100) relative to the lookback window.
        recent_drawdown_pct: Peak-to-trough drawdown over the **20-day**
            window (0–1 fraction, e.g. 0.08 = 8% drawdown).

    Returns:
        ``{regime, confidence, strategy_recommendation, signals}``
    """
    # HIGH_VOLATILITY: acute stress (20-day DD > 8%) or elevated vol + moderate DD
    if recent_drawdown_pct > 0.08 or (volatility_percentile > 90 and recent_drawdown_pct > 0.05):
        regime = "HIGH_VOLATILITY"
        # Confidence scales with drawdown severity
        confidence = min(1.0, (recent_drawdown_pct - 0.05) / 0.10)
        if volatility_percentile > 90:
            confidence = max(confidence, min(1.0, (volatility_percentile - 90.0) / 10.0))
        confidence = max(0.0, confidence)
    elif adx > 25:
        regime = "TRENDING"
        # ADX 25→0%, 30→50%, 35→100% (SPY P90=34)
        confidence = min(1.0, (adx - 25.0) / 10.0)
    elif adx < 20 and volatility_percentile < 75:
        regime = "MEAN_REVERTING"
        # ADX 20→0%, 16→50%, 12→100% (SPY P25=17)
        confidence = min(1.0, (20.0 - adx) / 8.0)
    else:
        regime = "TRANSITIONAL"
        confidence = 0.3

    return {
        "regime": regime,
        "confidence": round(confidence, 4),
        "strategy_recommendation": _REGIME_STRATEGY[regime],
        "signals": {
            "adx": round(float(adx), 4),
            "rsi": round(float(rsi), 4),
            "volatility_percentile": round(float(volatility_percentile), 2),
            "recent_drawdown_pct": round(float(recent_drawdown_pct), 4),
        },
    }


# ---------------------------------------------------------------------------
# HMM-based classification
# ---------------------------------------------------------------------------

def classify_regime_hmm(
    log_returns: List[float],
    volatilities: List[float],
    n_states: int = 3,
) -> dict:
    """Classify market regime using a Gaussian HMM.

    Trains a ``GaussianHMM`` on stacked ``[log_returns, volatilities]``
    features, then labels hidden states by their mean return:

    * highest mean return → ``BULL``
    * lowest mean return  → ``BEAR``
    * middle              → ``SIDEWAYS``

    Args:
        log_returns: Daily log returns (oldest first).
        volatilities: Daily realized volatility values (same length).
        n_states: Number of HMM hidden states (default 3).

    Returns:
        ``{current_state, state_label, state_probabilities,
           regime_transition_detected, states_summary, hmm_available}``

        If *hmmlearn* is not installed:
        ``{hmm_available: False, error: str}``
    """
    if not HAS_HMM:
        return {"hmm_available": False, "error": "hmmlearn not installed"}

    min_obs = max(n_states * 10, 20)
    if len(log_returns) < min_obs or len(volatilities) < min_obs:
        return {
            "hmm_available": True,
            "current_state": 0,
            "state_label": "SIDEWAYS",
            "state_probabilities": {"BULL": 0.33, "BEAR": 0.33, "SIDEWAYS": 0.34},
            "regime_transition_detected": False,
            "states_summary": [],
            "insufficient_data": True,
        }

    rets = np.nan_to_num(np.array(log_returns, dtype=float), nan=0.0, posinf=0.0, neginf=0.0)
    vols_arr = np.array(volatilities, dtype=float)
    mean_vol = float(np.nanmean(vols_arr)) if np.any(~np.isnan(vols_arr)) else 0.0
    vols_arr = np.nan_to_num(vols_arr, nan=mean_vol)

    features = np.column_stack([rets, vols_arr])

    try:
        model = _hmm_mod.GaussianHMM(
            n_components=n_states,
            covariance_type="diag",   # diag is more numerically stable than full
            n_iter=100,
            random_state=42,
        )
        model.fit(features)
        states = model.predict(features)
        proba_matrix = model.predict_proba(features)
    except Exception as exc:
        logger.warning("HMM fitting failed: %s", exc)
        return {
            "hmm_available": True,
            "current_state": 0,
            "state_label": "SIDEWAYS",
            "state_probabilities": {"BULL": 0.33, "BEAR": 0.33, "SIDEWAYS": 0.34},
            "regime_transition_detected": False,
            "states_summary": [],
            "error": str(exc),
        }

    current_state = int(states[-1])
    prev_state = int(states[-2]) if len(states) >= 2 else current_state
    transition_detected = current_state != prev_state

    # Per-state statistics
    state_mean_returns = []
    state_mean_vols_list = []
    for s in range(n_states):
        mask = states == s
        state_mean_returns.append(float(rets[mask].mean()) if mask.any() else 0.0)
        state_mean_vols_list.append(float(vols_arr[mask].mean()) if mask.any() else 0.0)

    sorted_by_return = sorted(range(n_states), key=lambda i: state_mean_returns[i])
    state_labels = ["SIDEWAYS"] * n_states
    state_labels[sorted_by_return[-1]] = "BULL"
    state_labels[sorted_by_return[0]] = "BEAR"
    # Middle states (n_states > 3) all get SIDEWAYS (already set)

    current_label = state_labels[current_state]
    last_proba = proba_matrix[-1]

    # Aggregate probabilities by label
    label_proba: Dict[str, float] = {"BULL": 0.0, "BEAR": 0.0, "SIDEWAYS": 0.0}
    for s in range(n_states):
        lbl = state_labels[s]
        label_proba[lbl] = label_proba.get(lbl, 0.0) + float(last_proba[s])

    states_summary = [
        {
            "state": s,
            "label": state_labels[s],
            "mean_return": round(state_mean_returns[s], 6),
            "mean_volatility": round(state_mean_vols_list[s], 6),
        }
        for s in range(n_states)
    ]

    return {
        "hmm_available": True,
        "current_state": current_state,
        "state_label": current_label,
        "state_probabilities": {k: round(v, 4) for k, v in label_proba.items()},
        "regime_transition_detected": transition_detected,
        "states_summary": states_summary,
    }


# ---------------------------------------------------------------------------
# Main entry point: rule-based + HMM combined
# ---------------------------------------------------------------------------

def detect_market_regime(
    ohlcv: Dict[str, List[float]],
    lookback_days: int = 60,
) -> dict:
    """Detect the current market regime from raw OHLCV data.

    Steps:

    1. Compute ADX, RSI, ATR from price data.
    2. Compute log-returns and 21-day rolling realized volatility.
    3. Rank current volatility as a percentile within the lookback window.
    4. Compute recent peak-to-trough drawdown.
    5. Run rule-based classification (always).
    6. Run HMM classification (when *hmmlearn* is installed).
    7. Report whether both methods agree.

    Args:
        ohlcv: Dict with keys ``'open'``, ``'high'``, ``'low'``, ``'close'``,
               ``'volume'`` (lists of floats, oldest first).
        lookback_days: Window for volatility percentile and HMM training.

    Returns:
        ``{regime, confidence, strategy_recommendation,
           rule_based, hmm_based, agreement,
           volatility_metrics, timestamp}``
    """
    closes = ohlcv.get("close", [])
    highs = ohlcv.get("high", [])
    lows = ohlcv.get("low", [])

    min_bars = max(lookback_days, 30)
    if len(closes) < min_bars:
        rule_result = classify_regime_rules(
            adx=20.0, rsi=50.0,
            volatility_percentile=50.0,
            recent_drawdown_pct=0.0,
        )
        return {
            "regime": "TRANSITIONAL",
            "confidence": 0.3,
            "strategy_recommendation": "HOLD",
            "rule_based": rule_result,
            "hmm_based": {"hmm_available": False, "error": "insufficient_data"},
            "agreement": False,
            "volatility_metrics": {
                "current_volatility": 0.0,
                "volatility_percentile": 50.0,
                "avg_true_range": 0.0,
            },
            "timestamp": pd.Timestamp.now(tz=timezone.utc).isoformat(),
            "insufficient_data": True,
        }

    close_arr = np.array(closes, dtype=float)

    # Log returns (length = len(closes) - 1)
    log_rets = np.log(close_arr[1:] / np.where(close_arr[:-1] > 0, close_arr[:-1], 1.0))
    log_rets = np.nan_to_num(log_rets, nan=0.0, posinf=0.0, neginf=0.0)

    # 21-day rolling annualised realized volatility
    vol_window = min(21, max(2, len(log_rets)))
    rol_vol = (
        pd.Series(log_rets)
        .rolling(vol_window)
        .std()
        .fillna(0.0)
        .values
        * np.sqrt(252)
    )

    current_vol = float(rol_vol[-1]) if len(rol_vol) > 0 else 0.0

    # Volatility percentile within lookback window
    window_vols = rol_vol[-lookback_days:] if len(rol_vol) >= lookback_days else rol_vol
    nonzero_vols = window_vols[window_vols > 0]
    if len(nonzero_vols) > 1 and current_vol > 0:
        vol_pct = float(np.mean(nonzero_vols <= current_vol) * 100.0)
    else:
        vol_pct = 50.0

    # Recent peak-to-trough drawdown (20-day window — captures acute stress
    # without lingering for months after a single sell-off event)
    dd_window = min(20, len(close_arr))
    recent_closes = close_arr[-dd_window:]
    running_peak = np.maximum.accumulate(recent_closes)
    drawdown_arr = (running_peak - recent_closes) / np.where(running_peak > 0, running_peak, 1.0)
    recent_drawdown = float(drawdown_arr.max()) if len(drawdown_arr) > 0 else 0.0

    # Technical indicators
    adx_data = _adx(list(highs), list(lows), list(closes))
    rsi_data = _rsi(list(closes))
    atr_data = _atr(list(highs), list(lows), list(closes))

    adx_val = adx_data.get("adx", 20.0)
    rsi_val = rsi_data.get("rsi", 50.0)
    atr_val = atr_data.get("atr", 0.0)

    # Rule-based
    rule_result = classify_regime_rules(
        adx=adx_val,
        rsi=rsi_val,
        volatility_percentile=vol_pct,
        recent_drawdown_pct=recent_drawdown,
    )

    # HMM-based
    hmm_log_rets = list(log_rets[-lookback_days:])
    hmm_vols = list(rol_vol[-lookback_days:])
    hmm_result = classify_regime_hmm(
        log_returns=hmm_log_rets,
        volatilities=hmm_vols,
    )

    # Agreement — None when HMM is unavailable (avoids false "no agreement" signal)
    rule_regime = rule_result["regime"]
    hmm_label = hmm_result.get("state_label", "")
    expected_hmm = _REGIME_HMM_EQUIVALENT.get(rule_regime, "")
    if not hmm_result.get("hmm_available"):
        agreement = None
    else:
        agreement = bool(expected_hmm and expected_hmm == hmm_label)

    return {
        "regime": rule_result["regime"],
        "confidence": rule_result["confidence"],
        "strategy_recommendation": rule_result["strategy_recommendation"],
        "rule_based": rule_result,
        "hmm_based": hmm_result,
        "agreement": agreement,
        "volatility_metrics": {
            "current_volatility": round(current_vol, 6),
            "volatility_percentile": round(vol_pct, 2),
            "avg_true_range": round(atr_val, 4),
        },
        "timestamp": pd.Timestamp.now(tz=timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Backward-compatible function (tested by test_quant_tools.py)
# ---------------------------------------------------------------------------

def classify_market_regime(
    spy_prices: Dict[str, List[float]],
    vix_prices: Optional[List[float]] = None,
    adx_period: int = 14,
    ma_short: int = 50,
    ma_long: int = 200,
) -> dict:
    """Classify market regime using ADX, VIX level, and MA crossover.

    Args:
        spy_prices: OHLCV dict for SPY:
                    ``{'open': [...], 'high': [...], 'low': [...],
                       'close': [...], 'volume': [...]}``
        vix_prices: Optional list of daily VIX closing prices (newest last).
        adx_period: ADX calculation period (default 14).
        ma_short: Short MA period (default 50).
        ma_long: Long MA period (default 200).

    Returns:
        ``{regime, confidence, indicators, recommended_strategy,
           position_size_multiplier}``
    """
    closes = spy_prices['close']
    highs = spy_prices['high']
    lows = spy_prices['low']

    close_s = pd.Series(closes)

    # ADX via private helper (no external API call)
    adx_data = _adx(list(highs), list(lows), list(closes), adx_period)
    adx_val = adx_data.get("adx", 20.0)
    # If not enough data for _adx, try ta library
    if adx_data.get("insufficient_data") and _HAS_TA:
        high_s = pd.Series(highs)
        low_s = pd.Series(lows)
        adx_indicator = ta.trend.ADXIndicator(
            high=high_s, low=low_s, close=close_s, window=adx_period
        )
        adx_series = adx_indicator.adx().dropna()
        adx_val = float(adx_series.iloc[-1]) if len(adx_series) > 0 else 20.0

    # Moving averages
    ma_short_val = (
        float(close_s.rolling(ma_short).mean().iloc[-1])
        if len(close_s) >= ma_short else float(close_s.mean())
    )
    ma_long_val = (
        float(close_s.rolling(ma_long).mean().iloc[-1])
        if len(close_s) >= ma_long else float(close_s.mean())
    )

    # MA crossover signal
    if len(close_s) >= ma_long + 1:
        prev_short = float(close_s.rolling(ma_short).mean().iloc[-2])
        prev_long = float(close_s.rolling(ma_long).mean().iloc[-2])
        if ma_short_val > ma_long_val and prev_short <= prev_long:
            ma_crossover = 'golden_cross'
        elif ma_short_val < ma_long_val and prev_short >= prev_long:
            ma_crossover = 'death_cross'
        else:
            ma_crossover = 'neutral'
    else:
        ma_crossover = 'golden_cross' if ma_short_val >= ma_long_val else 'death_cross'

    ma_bullish = ma_short_val >= ma_long_val

    # VIX analysis
    vix_current = float(vix_prices[-1]) if vix_prices else None
    vix_spike = False
    if vix_prices and len(vix_prices) >= 2:
        prev_vix = float(vix_prices[-2])
        if prev_vix > 0 and (vix_current - prev_vix) / prev_vix > 0.20:
            vix_spike = True

    confidence = 0.0
    regime = 'RANGING'

    if vix_current is not None and (vix_current > 30 or vix_spike):
        regime = 'HIGH_VOLATILITY'
        confidence = min(1.0, 0.6 + (vix_current - 30) / 30 if vix_current > 30 else 0.6)
    else:
        if adx_val > 25 and ma_bullish:
            regime = 'TRENDING'
            confidence += 0.4
        elif adx_val < 20:
            regime = 'RANGING'
            confidence += 0.4

        if ma_crossover == 'golden_cross':
            confidence += 0.3
        elif ma_crossover == 'death_cross':
            confidence += 0.2

        if adx_val > 30:
            confidence += 0.2
        elif adx_val < 15:
            confidence += 0.2

        if vix_current is not None and 20 <= vix_current <= 30:
            confidence = max(0.0, confidence - 0.2)

        confidence = min(1.0, confidence)

    if regime == 'TRENDING':
        recommended_strategy = 'MOMENTUM'
        position_size_multiplier = 1.0
    elif regime == 'RANGING':
        recommended_strategy = 'MEAN_REVERSION'
        position_size_multiplier = 1.0
    else:
        recommended_strategy = 'REDUCE_EXPOSURE'
        position_size_multiplier = 0.5

    indicators = {
        'adx_14': round(adx_val, 2),
        'spy_50ma': round(ma_short_val, 2),
        'spy_200ma': round(ma_long_val, 2),
        'ma_crossover': ma_crossover,
        'vix_current': round(vix_current, 2) if vix_current is not None else None,
    }

    return {
        'regime': regime,
        'confidence': round(confidence, 3),
        'indicators': indicators,
        'recommended_strategy': recommended_strategy,
        'position_size_multiplier': position_size_multiplier,
    }
