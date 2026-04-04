"""
tools/quant/weekly.py — Weekly timeframe context from daily OHLCV data.

Resamples daily bars to weekly bars and computes higher-timeframe indicators
that capture the "big picture" trend structure invisible in daily data alone.

All functions are deterministic — no LLM, no external API calls, no
additional data fetches (reuses daily bars already fetched by QuantEngine).
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Minimum weekly bars required for each indicator
_MIN_BARS_TREND = 12    # trend structure needs 12 weeks
_MIN_BARS_10WMA = 10    # 10-week moving average
_MIN_BARS_40WMA = 40    # 40-week moving average (~200 daily)
_MIN_BARS_RSI = 16      # 14-period RSI + 2 warmup


def resample_to_weekly(daily_df: pd.DataFrame) -> pd.DataFrame:
    """Convert a daily OHLCV DataFrame to weekly bars.

    Uses Monday-start weeks (W-MON) so each bar covers Mon–Fri.
    Drops the final incomplete week if it has < 3 trading days.

    Args:
        daily_df: DataFrame with DatetimeIndex and columns
                  ``open, high, low, close, volume``.

    Returns:
        Weekly DataFrame with the same columns (may be empty).
    """
    if daily_df is None or daily_df.empty or len(daily_df) < 5:
        return pd.DataFrame()

    # Ensure DatetimeIndex
    if not isinstance(daily_df.index, pd.DatetimeIndex):
        return pd.DataFrame()

    weekly = daily_df.resample("W-FRI").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["close"])

    # Drop last week if it has fewer than 3 trading days (incomplete)
    if len(weekly) >= 2:
        last_week_start = weekly.index[-1] - pd.Timedelta(days=6)
        days_in_last = daily_df.loc[daily_df.index >= last_week_start].shape[0]
        if days_in_last < 3:
            weekly = weekly.iloc[:-1]

    return weekly


def compute_weekly_context(daily_df: pd.DataFrame, current_price: float) -> dict:
    """Compute weekly timeframe context from daily OHLCV data.

    Returns a dict with:
      - weekly_trend_score: -1.0 to +1.0 (higher high / higher low structure)
      - price_vs_10wma_pct: distance from 10-week MA
      - price_vs_40wma_pct: distance from 40-week MA
      - weekly_ma_bullish: 10-week MA above 40-week MA
      - weekly_rsi: 14-period RSI on weekly closes
      - weekly_support: nearest support from weekly swing pivots
      - weekly_resistance: nearest resistance from weekly swing pivots
      - weinstein_stage: 1 (basing), 2 (advancing), 3 (topping), 4 (declining)

    Args:
        daily_df: Daily OHLCV DataFrame (needs ~1 year for full context).
        current_price: Current price of the ticker.

    Returns:
        Dict of weekly indicators. Missing values are None.
    """
    ctx: dict = {
        "weekly_trend_score": None,
        "price_vs_10wma_pct": None,
        "price_vs_40wma_pct": None,
        "weekly_ma_bullish": None,
        "weekly_rsi": None,
        "weekly_support": None,
        "weekly_resistance": None,
        "weinstein_stage": None,
    }

    weekly = resample_to_weekly(daily_df)
    if weekly.empty or len(weekly) < _MIN_BARS_TREND:
        return ctx

    closes = weekly["close"].tolist()
    highs = weekly["high"].tolist()
    lows = weekly["low"].tolist()

    # --- 1. Weekly trend structure (higher highs / higher lows) ---
    ctx["weekly_trend_score"] = _compute_trend_score(highs, lows, lookback=12)

    # --- 2. Weekly MA position ---
    wma10 = _sma(closes, 10)
    wma40 = _sma(closes, 40)

    if wma10 is not None:
        ctx["price_vs_10wma_pct"] = round(
            (current_price - wma10) / wma10, 4
        ) if wma10 > 0 else 0.0

    if wma40 is not None:
        ctx["price_vs_40wma_pct"] = round(
            (current_price - wma40) / wma40, 4
        ) if wma40 > 0 else 0.0

    if wma10 is not None and wma40 is not None:
        ctx["weekly_ma_bullish"] = wma10 > wma40

    # --- 3. Weinstein stage ---
    if wma40 is not None:
        ctx["weinstein_stage"] = _classify_weinstein_stage(
            current_price, wma10, wma40, closes,
        )
        # Check for rapid stage transition using prior week's data
        if len(closes) >= 2:
            prior_closes = closes[:-1]
            prior_price = prior_closes[-1]
            prior_wma10 = _sma(prior_closes, 10)
            prior_wma40 = _sma(prior_closes, 40)
            if prior_wma40 is not None:
                stage_prior = _classify_weinstein_stage(
                    prior_price, prior_wma10, prior_wma40, prior_closes,
                )
                ctx["stage_prior"] = stage_prior
                if abs(ctx["weinstein_stage"] - stage_prior) > 1:
                    ctx["stage_jump"] = True

    # --- 4. Weekly RSI ---
    if len(closes) >= _MIN_BARS_RSI:
        ctx["weekly_rsi"] = _rsi_weekly(closes, period=14)

    # --- 5. Weekly support / resistance from swing pivots ---
    from tools.quant.price_levels import find_swing_pivots

    if len(highs) >= 7:
        # Use order=3 for weekly (3 weeks on each side = ~1.5 months)
        pivots = find_swing_pivots(highs, lows, order=3)

        supports = sorted(
            [l for l in pivots["swing_lows"] if l < current_price], reverse=True
        )
        resistances = sorted(
            [h for h in pivots["swing_highs"] if h > current_price]
        )

        if supports:
            ctx["weekly_support"] = round(supports[0], 2)
        if resistances:
            ctx["weekly_resistance"] = round(resistances[0], 2)

    return ctx


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_trend_score(
    highs: list[float],
    lows: list[float],
    lookback: int = 12,
) -> float:
    """Score the higher-high / higher-low structure over the last *lookback* weeks.

    Compares each week's high to the prior week's high and each week's low to
    the prior week's low. The score is (higher_highs + higher_lows - lower_highs
    - lower_lows) / (2 × comparisons), normalized to [-1.0, +1.0].

    +1.0 = perfect staircase up, -1.0 = perfect staircase down, 0 = choppy.
    """
    n = min(lookback, len(highs) - 1)
    if n < 3:
        return 0.0

    hh_count = 0  # higher highs
    hl_count = 0  # higher lows
    lh_count = 0  # lower highs
    ll_count = 0  # lower lows

    start = len(highs) - n
    for i in range(start, len(highs)):
        if highs[i] > highs[i - 1]:
            hh_count += 1
        elif highs[i] < highs[i - 1]:
            lh_count += 1

        if lows[i] > lows[i - 1]:
            hl_count += 1
        elif lows[i] < lows[i - 1]:
            ll_count += 1

    bullish = hh_count + hl_count
    bearish = lh_count + ll_count
    total = bullish + bearish

    if total == 0:
        return 0.0

    return round((bullish - bearish) / total, 2)


def _sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last *period* values."""
    if len(values) < period:
        return None
    return round(float(np.mean(values[-period:])), 4)


def _rsi_weekly(closes: list[float], period: int = 14) -> float:
    """Compute RSI on weekly close prices."""
    if len(closes) < period + 1:
        return 50.0

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(0.0, c) for c in changes]
    losses = [max(0.0, -c) for c in changes]

    # Wilder's smoothed average
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss < 1e-12:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 1)


def _classify_weinstein_stage(
    current_price: float,
    wma10: float | None,
    wma40: float,
    closes: list[float],
) -> int:
    """Classify Weinstein stage (1-4) from price and MA relationships.

    Stage 1 (Basing):     40WMA flat, price near 40WMA, no directional structure
    Stage 2 (Advancing):  10WMA > 40WMA, 40WMA rising or flat-to-rising
                          Includes pullbacks where price dips below 40WMA
                          temporarily while MA structure remains bullish.
    Stage 3 (Topping):    10WMA crossing below 40WMA, momentum fading
    Stage 4 (Declining):  Price < 40WMA, 10WMA < 40WMA, 40WMA falling
    """
    if wma40 <= 0:
        return 1

    price_above_40 = current_price > wma40
    ma10_above_40 = wma10 is not None and wma10 > wma40

    # Check 40WMA slope (is it rising or falling?)
    # Compare current 40WMA to 40WMA ~8 weeks ago
    if len(closes) >= 48:
        old_wma40 = float(np.mean(closes[-48:-8]))
        wma40_rising = wma40 > old_wma40 * 1.005  # >0.5% increase
        wma40_falling = wma40 < old_wma40 * 0.995
    else:
        wma40_rising = False
        wma40_falling = False

    # Stage 2: 10WMA > 40WMA with rising 40WMA.  Price may temporarily dip
    # below 40WMA during a pullback — the MA structure (10>40, 40 rising)
    # is what defines the advancing stage, not the instantaneous price.
    if ma10_above_40 and wma40_rising:
        return 2  # Advancing (includes pullbacks)
    elif ma10_above_40 and price_above_40:
        return 2  # Advancing (price + 10WMA both above 40WMA)
    elif not price_above_40 and not ma10_above_40:
        if wma40_falling:
            return 4  # Declining
        else:
            return 1  # Basing (below 40WMA but 40WMA not falling)
    elif price_above_40 and not ma10_above_40:
        return 3  # Topping (price above but 10WMA crossing below)
    elif not price_above_40 and ma10_above_40:
        # Price dipped below 40WMA but 10WMA still above — early pullback.
        # If 40WMA is not falling, the uptrend structure is intact.
        if not wma40_falling:
            return 2  # Stage 2 pullback
        else:
            return 3  # Topping (structure breaking down)
    else:
        return 3  # Topping (momentum fading)
