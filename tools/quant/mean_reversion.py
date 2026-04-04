"""
tools/quant/mean_reversion.py — Mean reversion signal tools for the QuantAgent.

Implements z-score based mean reversion signals using rolling price statistics,
Bollinger Bands, and RSI confirmation.  Tools are decorated with

"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from tools.quant.technical import _rsi, _bollinger_bands

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private helper functions
# ---------------------------------------------------------------------------

def _compute_zscore(prices: np.ndarray, window: int = 20) -> Optional[float]:
    """Compute the z-score of the last price relative to the rolling window.

    Args:
        prices: Array of closing prices (oldest first).
        window: Rolling window size (default 20).

    Returns:
        Z-score as a float, or ``None`` if there is insufficient data or the
        rolling std is effectively zero.
    """
    if len(prices) < window + 1:
        return None
    s = pd.Series(prices.astype(float))
    mean = float(s.rolling(window).mean().iloc[-1])
    std = float(s.rolling(window).std().iloc[-1])
    if std < 1e-12 or np.isnan(std) or np.isnan(mean):
        return None
    return float((prices[-1] - mean) / std)


def _generate_signal(
    z_score: float,
    rsi: float,
    bb_percent_b: float,
) -> Tuple[str, float]:
    """Map z-score, RSI, and Bollinger %B to an action and signal strength.

    Signal logic (priority order):

    * ``LONG``  — z < -2.0 **and** RSI < 30   (oversold confirmation)
    * ``SHORT`` — z > +2.0 **and** RSI > 70   (overbought confirmation)
    * ``EXIT``  — |z| < 0.5                    (price back near mean)
    * ``NEUTRAL`` — all other cases

    ``signal_strength`` is proportional to |z| for LONG/SHORT (capped at 1.0),
    and 0.0 for EXIT/NEUTRAL.

    Args:
        z_score: Current z-score of price vs rolling mean.
        rsi: RSI(14) value (0–100).
        bb_percent_b: Bollinger Band %B (0 = lower, 1 = upper).

    Returns:
        ``(action, signal_strength)`` tuple.
    """
    if z_score < -2.0 and rsi < 30:
        return "LONG", min(1.0, abs(z_score) / 3.0)
    if z_score > 2.0 and rsi > 70:
        return "SHORT", min(1.0, abs(z_score) / 3.0)
    if abs(z_score) < 0.5:
        return "EXIT", 0.0
    return "NEUTRAL", 0.0


def _calculate_stop_and_target(
    prices: np.ndarray,
    z_score: float,
    window: int = 20,
) -> Tuple[float, float]:
    """Compute stop-loss and take-profit levels.

    * LONG  (z < 0): stop = mean − 3σ,  target = mean
    * SHORT (z > 0): stop = mean + 3σ,  target = mean
    * NEUTRAL/EXIT : stop = target = current price

    Args:
        prices: Array of closing prices (oldest first).
        z_score: Current z-score (used to determine direction).
        window: Rolling window used for mean/std (default 20).

    Returns:
        ``(stop_loss, take_profit)`` as a float tuple.
    """
    s = pd.Series(prices.astype(float))
    mean = float(s.rolling(window).mean().iloc[-1])
    std = float(s.rolling(window).std().iloc[-1])
    current = float(prices[-1])

    if z_score < 0:
        stop = round(mean - 3.0 * std, 4)
        target = round(mean, 4)
    elif z_score > 0:
        stop = round(mean + 3.0 * std, 4)
        target = round(mean, 4)
    else:
        stop = round(current, 4)
        target = round(current, 4)

    return stop, target


def _resolve_price_df(
    ticker: str,
    price_data: Optional[Dict[str, "pd.DataFrame"]],
    as_of: "pd.Timestamp",
) -> Optional["pd.DataFrame"]:
    """Return a price DataFrame filtered to *as_of*, or None on failure."""
    if price_data is not None and ticker in price_data:
        df = price_data[ticker]
        return df[df.index <= as_of]
    try:
        from tools.data.provider import create_provider  # noqa: PLC0415
        provider = create_provider()
        start = (as_of - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
        end = as_of.strftime("%Y-%m-%d")
        bars = provider.get_bars([ticker], start=start, end=end)
        return bars.get(ticker)
    except Exception as exc:
        logger.debug("Could not fetch live data for %s: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

def calculate_mean_reversion_signals(
    tickers: List[str] = None,
    as_of_date: str = None,
    price_data: Optional[Dict[str, Any]] = None,
    window: int = 20,
) -> dict:
    """Calculate mean-reversion signals using z-score, RSI, and Bollinger Bands.

    Args:
        tickers: List of tickers to analyse.
        as_of_date: ISO date string for the signal date (e.g. ``"2024-01-15"``).
                    Only prices on or before this date are used.
        price_data: Dict mapping ticker -> DataFrame with a ``'close'`` column
                    and a ``DatetimeIndex``.  When a ticker is absent the
                    function attempts a live fetch via ``MarketDataProvider``;
                    if that also fails the ticker is skipped.
        window: Rolling window for z-score and Bollinger Bands (default 20).

    Returns:
        ``{signal_date, universe_size, signals}`` where each signal contains
        ``{ticker, z_score, rsi, bollinger_percent_b, action, signal_strength,
        entry_price, stop_loss, take_profit}``.
    """
    return _new_signals(
        tickers=tickers or [],
        as_of_date=as_of_date,
        price_data=price_data,
        window=window,
    )


def _new_signals(
    tickers: List[str],
    as_of_date: Optional[str],
    price_data: Optional[Dict[str, "pd.DataFrame"]],
    window: int = 20,
) -> dict:
    """New-convention implementation returning LONG/SHORT/EXIT/NEUTRAL."""
    as_of_ts = pd.Timestamp(as_of_date) if as_of_date else pd.Timestamp.now()
    signals = []

    for ticker in tickers:
        df = _resolve_price_df(ticker, price_data, as_of_ts)
        if df is None or df.empty:
            continue
        close = df["close"].values
        if len(close) < window + 14:
            logger.debug("Skipping %s: insufficient data (%d bars)", ticker, len(close))
            continue

        z = _compute_zscore(close, window=window)
        if z is None:
            continue

        rsi_data = _rsi(list(close), period=14)
        bb_data = _bollinger_bands(list(close), period=window, std_dev=2.0)
        action, strength = _generate_signal(z, rsi_data["rsi"], bb_data["percent_b"])
        stop_loss, take_profit = _calculate_stop_and_target(close, z, window=window)

        signals.append({
            "ticker": ticker,
            "z_score": round(z, 4),
            "rsi": round(rsi_data["rsi"], 4),
            "bollinger_percent_b": round(bb_data["percent_b"], 4),
            "action": action,
            "signal_strength": round(strength, 4),
            "entry_price": round(float(close[-1]), 4),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        })

    return {
        "signal_date": as_of_ts.date().isoformat(),
        "universe_size": len(signals),
        "signals": signals,
    }



