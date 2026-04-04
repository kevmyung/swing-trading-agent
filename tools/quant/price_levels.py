"""
tools/quant/price_levels.py — Structural price level detection.

Pure-Python computation of support/resistance levels from OHLCV data.
All functions are deterministic — no LLM, no external API calls.

Three independent level sources are combined:
  1. Swing pivots: local highs/lows detected by comparing bars to neighbors
  2. Key moving averages: 50-day and 200-day SMA
  3. Volume profile nodes: high-volume price zones that act as magnets

The wrapper ``compute_price_levels()`` merges all sources and returns
nearest_support, nearest_resistance, key_ma_levels, and derived metrics
ready for injection into QuantEngine's position/candidate context.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Swing pivot detection
# ---------------------------------------------------------------------------

def find_swing_pivots(
    highs: list[float],
    lows: list[float],
    order: int = 5,
) -> dict[str, list[float]]:
    """Detect swing high/low pivot points in price data.

    A swing low is a bar whose low is lower than the lows of *order* bars
    on each side. A swing high is the mirror. Duplicates within 0.5% of
    each other are collapsed to avoid noise.

    Args:
        highs: High prices (oldest first).
        lows: Low prices (oldest first).
        order: Number of bars on each side to compare. Default 5.

    Returns:
        Dict with ``'swing_highs'`` and ``'swing_lows'`` (sorted, unique).
    """
    n = len(highs)
    if n < 2 * order + 1:
        return {'swing_highs': [], 'swing_lows': []}

    swing_highs: list[float] = []
    swing_lows: list[float] = []

    for i in range(order, n - order):
        # Swing high: bar's high >= all neighbors' highs
        if all(highs[i] >= highs[i - j] for j in range(1, order + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, order + 1)):
            swing_highs.append(highs[i])

        # Swing low: bar's low <= all neighbors' lows
        if all(lows[i] <= lows[i - j] for j in range(1, order + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, order + 1)):
            swing_lows.append(lows[i])

    # Deduplicate levels within 0.5% of each other (keep the most recent)
    swing_highs = _deduplicate_levels(swing_highs, tolerance_pct=0.005)
    swing_lows = _deduplicate_levels(swing_lows, tolerance_pct=0.005)

    return {
        'swing_highs': sorted(swing_highs),
        'swing_lows': sorted(swing_lows),
    }


def _deduplicate_levels(levels: list[float], tolerance_pct: float = 0.005) -> list[float]:
    """Remove levels within *tolerance_pct* of each other, keeping the latest."""
    if not levels:
        return []
    result: list[float] = []
    for level in reversed(levels):  # most recent first
        if not any(abs(level - r) / r < tolerance_pct for r in result if r > 0):
            result.append(level)
    return result


# ---------------------------------------------------------------------------
# 2. Key moving average levels
# ---------------------------------------------------------------------------

def compute_ma_levels(closes: list[float]) -> dict[str, float | None]:
    """Compute 50-day and 200-day simple moving averages.

    Returns:
        Dict with ``'ma_50'`` and ``'ma_200'`` (None if insufficient data).
    """
    result: dict[str, float | None] = {'ma_50': None, 'ma_200': None}
    if len(closes) >= 50:
        result['ma_50'] = round(float(np.mean(closes[-50:])), 2)
    if len(closes) >= 200:
        result['ma_200'] = round(float(np.mean(closes[-200:])), 2)
    return result


# ---------------------------------------------------------------------------
# 3. Volume profile — high-volume nodes (HVN)
# ---------------------------------------------------------------------------

def compute_volume_nodes(
    closes: list[float],
    volumes: list[float],
    n_bins: int = 30,
    lookback: int = 60,
    top_n: int = 3,
) -> list[float]:
    """Identify high-volume price zones (Volume Profile HVN).

    Bins the last *lookback* days of price data and sums volume in each bin.
    Returns the center prices of the top *top_n* highest-volume bins.

    Args:
        closes: Close prices (oldest first).
        volumes: Volume values (oldest first).
        n_bins: Number of price bins. Default 30.
        lookback: How many recent bars to use. Default 60.
        top_n: How many HVN levels to return. Default 3.

    Returns:
        List of price levels (sorted), empty if insufficient data.
    """
    if len(closes) < 20 or len(volumes) < 20:
        return []

    recent_closes = closes[-lookback:]
    recent_volumes = volumes[-lookback:]
    n = min(len(recent_closes), len(recent_volumes))
    recent_closes = recent_closes[-n:]
    recent_volumes = recent_volumes[-n:]

    price_min = min(recent_closes)
    price_max = max(recent_closes)
    if price_max <= price_min:
        return []

    bin_edges = np.linspace(price_min, price_max, n_bins + 1)
    bin_volumes = np.zeros(n_bins)

    for price, vol in zip(recent_closes, recent_volumes):
        idx = int((price - price_min) / (price_max - price_min) * n_bins)
        idx = min(idx, n_bins - 1)
        bin_volumes[idx] += vol

    # Top N bins by volume
    top_indices = np.argsort(bin_volumes)[-top_n:]
    hvn_prices = []
    for idx in top_indices:
        if bin_volumes[idx] > 0:
            center = round(float((bin_edges[idx] + bin_edges[idx + 1]) / 2), 2)
            hvn_prices.append(center)

    return sorted(hvn_prices)


# ---------------------------------------------------------------------------
# 4. Wrapper: compute all levels + derived metrics
# ---------------------------------------------------------------------------

def compute_price_levels(
    df: Any,
    current_price: float,
    stop_loss_price: float | None = None,
) -> dict:
    """Compute structural price levels and derived metrics from OHLCV data.

    This is the main entry point called by QuantEngine. It merges swing
    pivots, MA levels, and volume profile nodes into a unified view.

    Args:
        df: DataFrame with 'open', 'high', 'low', 'close', 'volume' columns.
        current_price: The ticker's current (or latest close) price.
        stop_loss_price: ATR-based stop loss price (for stop_vs_support metric).

    Returns:
        Dict with:
          nearest_support, nearest_resistance,
          key_ma_levels (ma_50, ma_200),
          volume_nodes (list of HVN prices),
          stop_vs_nearest_support (% distance, positive = stop is above support),
          entry_vs_nearest_resistance (% distance to nearest resistance),
          ma_confluence (bool — 50 or 200 MA within 2% of current price).
    """
    result = {
        'nearest_support': None,
        'nearest_resistance': None,
        'key_ma_levels': {'ma_50': None, 'ma_200': None},
        'volume_nodes': [],
        'stop_vs_nearest_support': None,
        'entry_vs_nearest_resistance': None,
        'ma_confluence': False,
    }

    if df is None or df.empty or len(df) < 20:
        return result

    closes = df['close'].tolist()
    highs = df['high'].tolist()
    lows = df['low'].tolist()
    volumes = df['volume'].tolist() if 'volume' in df.columns else []

    # --- Collect all potential support/resistance levels ---
    support_levels: list[float] = []
    resistance_levels: list[float] = []

    # Swing pivots
    pivots = find_swing_pivots(highs, lows)
    for level in pivots['swing_lows']:
        if level < current_price:
            support_levels.append(level)
        else:
            resistance_levels.append(level)
    for level in pivots['swing_highs']:
        if level > current_price:
            resistance_levels.append(level)
        elif level < current_price:
            support_levels.append(level)

    # Key MAs
    ma_levels = compute_ma_levels(closes)
    result['key_ma_levels'] = ma_levels

    for ma_name in ('ma_50', 'ma_200'):
        ma_val = ma_levels.get(ma_name)
        if ma_val is not None:
            if ma_val < current_price:
                support_levels.append(ma_val)
            elif ma_val > current_price:
                resistance_levels.append(ma_val)

    # Volume nodes
    if volumes:
        vnodes = compute_volume_nodes(closes, volumes)
        result['volume_nodes'] = vnodes
        for vn in vnodes:
            if vn < current_price:
                support_levels.append(vn)
            elif vn > current_price:
                resistance_levels.append(vn)

    # --- Find nearest levels ---
    if support_levels:
        nearest_sup = max(support_levels)  # closest below current price
        result['nearest_support'] = round(nearest_sup, 2)
    if resistance_levels:
        nearest_res = min(resistance_levels)  # closest above current price
        result['nearest_resistance'] = round(nearest_res, 2)

    # --- Derived metrics ---
    nearest_sup = result['nearest_support']
    nearest_res = result['nearest_resistance']

    # stop_vs_nearest_support: how far the ATR stop is from the nearest support
    # Positive = stop is above support (good), Negative = stop is below support (risky)
    if stop_loss_price is not None and nearest_sup is not None and nearest_sup > 0:
        result['stop_vs_nearest_support'] = round(
            (stop_loss_price - nearest_sup) / nearest_sup, 4
        )

    # entry_vs_nearest_resistance: distance from entry to nearest overhead resistance
    if nearest_res is not None and current_price > 0:
        result['entry_vs_nearest_resistance'] = round(
            (nearest_res - current_price) / current_price, 4
        )

    # ma_confluence: is a major MA within 2% of current price?
    for ma_name in ('ma_50', 'ma_200'):
        ma_val = ma_levels.get(ma_name)
        if ma_val is not None and current_price > 0:
            distance_pct = abs(current_price - ma_val) / current_price
            if distance_pct <= 0.02:
                result['ma_confluence'] = True
                break

    return result
