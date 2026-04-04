"""
tools/quant/technical.py — Technical indicator tools for the QuantAgent.

Calculates RSI, MACD, Bollinger Bands, ATR, and ADX for entry confirmation
and stop-loss sizing.  Two compute paths are supported:

  1. TA-Lib (C extension, fastest, most accurate Wilder smoothing)
  2. pandas/numpy fallback (pure-Python, used when TA-Lib is not installed)

Both paths expose an identical output schema so callers never need to branch.
The existing ``calculate_technical_indicators`` function is preserved for
backward compatibility with existing tests.
"""

from __future__ import annotations

import logging
from datetime import timezone
from typing import Dict, List

import numpy as np
import pandas as pd

try:
    import talib as _talib
    HAS_TALIB = True
except ImportError:
    HAS_TALIB = False


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private computation helpers — called directly by calculate_all_indicators
# so we avoid the ._tool_func indirection.
# ---------------------------------------------------------------------------

def _rsi(prices: List[float], period: int = 14) -> dict:
    """Compute RSI via TA-Lib or Wilder-smoothed pandas fallback."""
    if len(prices) < period + 1:
        return {
            "rsi": 50.0, "overbought": False, "oversold": False,
            "period": period, "insufficient_data": True,
        }

    close = np.array(prices, dtype=float)

    if HAS_TALIB:
        rsi_arr = _talib.RSI(close, timeperiod=period)
        valid = rsi_arr[~np.isnan(rsi_arr)]
        rsi_val = float(valid[-1]) if len(valid) > 0 else 50.0
    else:
        s = pd.Series(close)
        delta = s.diff()
        # Use Wilder smoothing (EMA with alpha = 1/period)
        avg_gain = delta.clip(lower=0.0).ewm(
            alpha=1.0 / period, min_periods=period, adjust=False
        ).mean()
        avg_loss = (-delta.clip(upper=0.0)).ewm(
            alpha=1.0 / period, min_periods=period, adjust=False
        ).mean()
        rs = avg_gain / avg_loss.replace(0.0, np.nan)
        rsi_s = (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)
        rsi_val = float(rsi_s.iloc[-1])

    rsi_val = round(rsi_val, 4)
    return {
        "rsi": rsi_val,
        "overbought": rsi_val > 70.0,
        "oversold": rsi_val < 30.0,
        "period": period,
    }


def _bollinger_bands(prices: List[float], period: int = 20,
                     std_dev: float = 2.0) -> dict:
    """Compute Bollinger Bands via TA-Lib or pandas rolling fallback."""
    if len(prices) < period:
        price = float(prices[-1]) if prices else 0.0
        return {
            "upper": price, "middle": price, "lower": price,
            "bandwidth": 0.0, "percent_b": 0.5,
            "period": period, "insufficient_data": True,
        }

    close = np.array(prices, dtype=float)
    current_price = close[-1]

    if HAS_TALIB:
        upper_arr, mid_arr, lower_arr = _talib.BBANDS(
            close, timeperiod=period,
            nbdevup=std_dev, nbdevdn=std_dev, matype=0
        )
        def _last(arr: np.ndarray) -> float:
            v = arr[~np.isnan(arr)]
            return float(v[-1]) if len(v) > 0 else float(current_price)
        upper = _last(upper_arr)
        mid = _last(mid_arr)
        lower = _last(lower_arr)
    else:
        s = pd.Series(close)
        mid_s = s.rolling(period).mean()
        std_s = s.rolling(period).std(ddof=1)
        mid = float(mid_s.iloc[-1])
        upper = float(mid + std_dev * std_s.iloc[-1])
        lower = float(mid - std_dev * std_s.iloc[-1])

    band_width = upper - lower
    bandwidth = round((band_width / mid) if mid != 0 else 0.0, 4)
    percent_b = round((current_price - lower) / band_width if band_width > 0 else 0.5, 4)

    return {
        "upper": round(upper, 4),
        "middle": round(mid, 4),
        "lower": round(lower, 4),
        "bandwidth": bandwidth,
        "percent_b": percent_b,
        "period": period,
    }


def _adx(highs: List[float], lows: List[float], closes: List[float],
         period: int = 14) -> dict:
    """Compute ADX via TA-Lib or pure-pandas Wilder fallback."""
    min_bars = period * 2 + 1
    if len(closes) < min_bars:
        return {
            "adx": 0.0, "trending": False, "strong_trend": False,
            "period": period, "insufficient_data": True,
        }

    high = np.array(highs, dtype=float)
    low = np.array(lows, dtype=float)
    close = np.array(closes, dtype=float)

    if HAS_TALIB:
        adx_arr = _talib.ADX(high, low, close, timeperiod=period)
        valid = adx_arr[~np.isnan(adx_arr)]
        adx_val = float(valid[-1]) if len(valid) > 0 else 0.0
        adx_3ago = float(valid[-3]) if len(valid) >= 3 else adx_val
    else:
        h = pd.Series(high)
        l = pd.Series(low)
        c = pd.Series(close)
        prev_c = c.shift(1)

        tr = pd.concat([
            h - l,
            (h - prev_c).abs(),
            (l - prev_c).abs(),
        ], axis=1).max(axis=1)

        up_move = h.diff()
        dn_move = -l.diff()
        pos_dm = np.where((up_move > dn_move) & (up_move > 0), up_move.values, 0.0)
        neg_dm = np.where((dn_move > up_move) & (dn_move > 0), dn_move.values, 0.0)

        alpha = 1.0 / period
        atr_s = tr.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
        pos_dm_s = pd.Series(pos_dm, dtype=float).ewm(
            alpha=alpha, min_periods=period, adjust=False).mean()
        neg_dm_s = pd.Series(neg_dm, dtype=float).ewm(
            alpha=alpha, min_periods=period, adjust=False).mean()

        safe_atr = atr_s.replace(0.0, np.nan)
        pos_di = 100.0 * pos_dm_s / safe_atr
        neg_di = 100.0 * neg_dm_s / safe_atr
        denom = (pos_di + neg_di).replace(0.0, np.nan)
        dx = 100.0 * (pos_di - neg_di).abs() / denom
        adx_s = dx.ewm(alpha=alpha, min_periods=period, adjust=False).mean()

        valid_s = adx_s.dropna()
        adx_val = float(valid_s.iloc[-1]) if len(valid_s) > 0 else 0.0
        adx_3ago = float(valid_s.iloc[-3]) if len(valid_s) >= 3 else adx_val

    adx_val = round(adx_val, 4)
    adx_change_3d = round(adx_val - adx_3ago, 2)

    return {
        "adx": adx_val,
        "adx_change_3d": adx_change_3d,
        "trending": adx_val > 25.0,
        "strong_trend": adx_val > 40.0,
        "period": period,
    }


def _atr(highs: List[float], lows: List[float], closes: List[float],
         period: int = 14) -> dict:
    """Compute ATR via TA-Lib or true-range pandas fallback."""
    if len(closes) < period + 1:
        return {
            "atr": 0.0, "atr_percent": 0.0,
            "period": period, "insufficient_data": True,
        }

    high = np.array(highs, dtype=float)
    low = np.array(lows, dtype=float)
    close = np.array(closes, dtype=float)
    current_close = close[-1]

    if HAS_TALIB:
        atr_arr = _talib.ATR(high, low, close, timeperiod=period)
        valid = atr_arr[~np.isnan(atr_arr)]
        atr_val = float(valid[-1]) if len(valid) > 0 else 0.0
    else:
        h = pd.Series(high)
        l = pd.Series(low)
        c = pd.Series(close)
        prev_c = c.shift(1)
        tr = pd.concat([
            h - l,
            (h - prev_c).abs(),
            (l - prev_c).abs(),
        ], axis=1).max(axis=1)
        atr_s = tr.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
        valid_s = atr_s.dropna()
        atr_val = float(valid_s.iloc[-1]) if len(valid_s) > 0 else 0.0

    atr_val = round(atr_val, 4)
    atr_pct = round(atr_val / current_close * 100.0, 4) if current_close != 0 else 0.0
    return {
        "atr": atr_val,
        "atr_percent": atr_pct,
        "period": period,
    }


def _macd(prices: List[float], fast: int = 12, slow: int = 26,
          signal: int = 9) -> dict:
    """Compute MACD via TA-Lib or EMA pandas fallback."""
    if len(prices) < slow + signal:
        return {
            "macd": 0.0, "signal": 0.0, "histogram": 0.0,
            "bullish_crossover": False, "bearish_crossover": False,
            "insufficient_data": True,
        }

    close = np.array(prices, dtype=float)

    if HAS_TALIB:
        macd_arr, signal_arr, hist_arr = _talib.MACD(
            close, fastperiod=fast, slowperiod=slow, signalperiod=signal
        )
        def _last2(arr: np.ndarray):
            v = arr[~np.isnan(arr)]
            cur = float(v[-1]) if len(v) > 0 else 0.0
            prev = float(v[-2]) if len(v) >= 2 else None
            return cur, prev
        macd_cur, macd_prev = _last2(macd_arr)
        sig_cur, sig_prev = _last2(signal_arr)
        hist_cur, _ = _last2(hist_arr)
    else:
        s = pd.Series(close)
        ema_fast = s.ewm(span=fast, adjust=False).mean()
        ema_slow = s.ewm(span=slow, adjust=False).mean()
        macd_s = ema_fast - ema_slow
        signal_s = macd_s.ewm(span=signal, adjust=False).mean()
        hist_s = macd_s - signal_s

        macd_cur = float(macd_s.iloc[-1])
        macd_prev = float(macd_s.iloc[-2]) if len(macd_s) >= 2 else None
        sig_cur = float(signal_s.iloc[-1])
        sig_prev = float(signal_s.iloc[-2]) if len(signal_s) >= 2 else None
        hist_cur = float(hist_s.iloc[-1])

    macd_cur = round(macd_cur, 4)
    sig_cur = round(sig_cur, 4)
    hist_cur = round(hist_cur, 4)

    bullish = bool(
        macd_prev is not None and sig_prev is not None
        and macd_cur > sig_cur and macd_prev <= sig_prev
    )
    bearish = bool(
        macd_prev is not None and sig_prev is not None
        and macd_cur < sig_cur and macd_prev >= sig_prev
    )

    return {
        "macd": macd_cur,
        "signal": sig_cur,
        "histogram": hist_cur,
        "bullish_crossover": bullish,
        "bearish_crossover": bearish,
    }


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

def calculate_all_indicators(ohlcv: Dict[str, List[float]]) -> dict:
    """Calculate all technical indicators from a single OHLCV dict.

    This is the primary entry point for the QuantAgent — it consolidates
    RSI, Bollinger Bands, ADX, ATR, and MACD into one call.

    Args:
        ohlcv: Dict with keys ``'open'``, ``'high'``, ``'low'``, ``'close'``,
               ``'volume'``, each mapping to a list of floats (oldest first).

    Returns:
        ``{"rsi": {...}, "bollinger": {...}, "adx": {...},
           "atr": {...}, "macd": {...}, "timestamp": str}``
    """
    closes = ohlcv.get('close', [])
    highs = ohlcv.get('high', [])
    lows = ohlcv.get('low', [])

    return {
        "rsi": _rsi(closes),
        "bollinger": _bollinger_bands(closes),
        "adx": _adx(highs, lows, closes),
        "atr": _atr(highs, lows, closes),
        "macd": _macd(closes),
        "timestamp": pd.Timestamp.now(tz=timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Backward-compatible multi-ticker function (tested by test_quant_tools.py)
# ---------------------------------------------------------------------------

def calculate_technical_indicators(
    ticker_ohlcv: Dict[str, Dict[str, List[float]]],
    rsi_period: int = 14,
    atr_period: int = 14,
    bb_period: int = 20,
    bb_std: float = 2.0,
) -> dict:
    """Calculate RSI, MACD, Bollinger Bands, and ATR for one or more tickers.

    Args:
        ticker_ohlcv: Dict mapping ticker -> OHLCV dict
                      ``{'open': [...], 'high': [...], 'low': [...],
                         'close': [...], 'volume': [...]}``
        rsi_period: RSI period (default 14).
        atr_period: ATR period (default 14).
        bb_period: Bollinger Band period (default 20).
        bb_std: Bollinger Band standard-deviation multiplier (default 2.0).

    Returns:
        Dict mapping ticker -> ``{rsi_14, macd, bollinger, atr_14,
        suggested_stop_loss, current_price}``.
    """
    result: Dict[str, dict] = {}

    for ticker, ohlcv in ticker_ohlcv.items():
        closes = ohlcv['close']
        highs = ohlcv['high']
        lows = ohlcv['low']

        min_required = max(rsi_period + 1, bb_period + 1, atr_period + 1, 27)
        if len(closes) < min_required:
            logger.debug("Skipping %s: insufficient data (%d bars)", ticker, len(closes))
            continue

        current_price = float(closes[-1])

        rsi_data = _rsi(closes, rsi_period)
        bb_data = _bollinger_bands(closes, bb_period, bb_std)
        atr_data = _atr(highs, lows, closes, atr_period)
        macd_data = _macd(closes)

        crossover = 'none'
        if macd_data.get('bullish_crossover'):
            crossover = 'bullish'
        elif macd_data.get('bearish_crossover'):
            crossover = 'bearish'

        bb_upper = bb_data['upper']
        bb_lower = bb_data['lower']
        if current_price >= bb_upper:
            price_position = 'upper'
        elif current_price <= bb_lower:
            price_position = 'lower'
        else:
            price_position = 'middle'

        atr_val = atr_data['atr']
        suggested_stop_loss = round(current_price - 2.0 * atr_val, 4)

        result[ticker] = {
            'current_price': round(current_price, 4),
            'rsi_14': round(rsi_data['rsi'], 2),
            'macd': {
                'macd': macd_data['macd'],
                'signal': macd_data['signal'],
                'histogram': macd_data['histogram'],
                'crossover': crossover,
            },
            'bollinger': {
                'upper': bb_data['upper'],
                'middle': bb_data['middle'],
                'lower': bb_data['lower'],
                'price_position': price_position,
            },
            'atr_14': atr_val,
            'suggested_stop_loss': suggested_stop_loss,
        }

    return result
