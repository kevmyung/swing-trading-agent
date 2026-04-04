"""
tools/data/screener.py — Universe screener for EOD_SIGNAL cycle.

Defines the tradeable universe by applying five deterministic filters on top
of the S&P 500 constituent list:

  Stage 1 — Liquidity:   20-day avg daily volume >= min_avg_volume (default 1M)
  Stage 2 — Volatility:  ATR(14)/close between min_atr_pct and max_atr_pct
  Stage 3 — Structural:  reject price < declining 200-day MA (long-term downtrend)
  Stage 4 — Signals:     top N by multi-signal union (momentum, volume, MACD)
  Stage 5 — Quality:     reject overbought (mr_z > 1.0) and poor R:R (< 1.5)

This is a pure data/computation module — no LLM involved.

The S&P 500 list is fetched from Wikipedia at runtime; a hardcoded fallback of
~100 large-cap names is used when the network request fails.

Usage::

    from tools.data.screener import screen_universe
    tickers = screen_universe()           # 30–50 names
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_WIKI_STORAGE_OPTS = {"User-Agent": "TradingSystem/1.0 (pandas read_html)"}

# ---------------------------------------------------------------------------
# Fallback universe (~100 large-cap S&P 500 names, used when Wikipedia fails)
# ---------------------------------------------------------------------------

_FALLBACK_UNIVERSE: List[str] = [
    'AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META', 'TSLA', 'BRK.B',
    'JPM', 'JNJ', 'V', 'UNH', 'XOM', 'PG', 'MA', 'HD', 'CVX', 'MRK', 'ABBV',
    'LLY', 'AVGO', 'COST', 'PEP', 'KO', 'ADBE', 'TMO', 'NKE', 'WMT', 'BAC',
    'MCD', 'CSCO', 'ABT', 'ORCL', 'CRM', 'ACN', 'LIN', 'DHR', 'TXN', 'VZ',
    'CMCSA', 'NEE', 'NFLX', 'PM', 'AMGN', 'RTX', 'BMY', 'QCOM', 'T', 'HON',
    'GS', 'MS', 'BLK', 'INTC', 'AMD', 'CAT', 'BA', 'NOW', 'AMAT', 'INTU',
    'IBM', 'DE', 'SPGI', 'AXP', 'ISRG', 'ELV', 'BKNG', 'ADI', 'LRCX', 'GE',
    'SYK', 'MDT', 'MMM', 'WFC', 'C', 'USB', 'PLD', 'AMT', 'COP', 'EOG',
    'MO', 'DUK', 'SO', 'F', 'GM', 'UBER', 'ABNB', 'SQ', 'PYPL', 'SHOP',
    'SNOW', 'DDOG', 'ZS', 'NET', 'CRWD', 'PANW', 'MRVL', 'KLAC', 'SNPS', 'CDNS',
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Dual-class share pairs: keep the first (typically Class A / higher liquidity),
# drop the second.  Both track the same company — screening both wastes a slot
# and risks doubling exposure to the same underlying.
_DUAL_CLASS_DROP = {'GOOG', 'NWS', 'FOX', 'LENT'}  # Class C / B duplicates


def get_sp500_tickers() -> List[str]:
    """Fetch current S&P 500 constituent tickers from Wikipedia.

    Falls back to ``_FALLBACK_UNIVERSE`` if the network request fails.
    Removes duplicate share classes (e.g. GOOG when GOOGL is present).

    Returns:
        List of ticker symbols in Alpaca-compatible format
        (dots preserved, e.g. ``BRK.B``).
    """
    try:
        tables = pd.read_html(
            _WIKI_URL,
            attrs={"id": "constituents"},
            storage_options=_WIKI_STORAGE_OPTS,
        )
        df = tables[0]
        tickers = df["Symbol"].tolist()
        # Wikipedia sometimes uses hyphens (BRK-B); Alpaca needs dots (BRK.B)
        result = [t.strip().replace("-", ".") for t in tickers if isinstance(t, str) and t.strip()]
        logger.info("Fetched %d S&P 500 tickers from Wikipedia.", len(result))
    except Exception as exc:
        logger.warning(
            "Failed to fetch S&P 500 tickers from Wikipedia (%s) — "
            "using hardcoded fallback universe (%d tickers).",
            exc,
            len(_FALLBACK_UNIVERSE),
        )
        result = list(_FALLBACK_UNIVERSE)

    # Remove duplicate share classes
    before = len(result)
    result = [t for t in result if t not in _DUAL_CLASS_DROP]
    dropped = before - len(result)
    if dropped:
        logger.info("Screener: dropped %d dual-class duplicates.", dropped)
    return result


# Module-level cache for sector map (populated on first call per process).
_sector_map_cache: Optional[Dict[str, str]] = None


def get_sp500_sector_map() -> Dict[str, str]:
    """Fetch S&P 500 ticker-to-GICS-sector mapping from Wikipedia.

    The result is cached in-process after the first successful fetch.
    Falls back to an empty dict if the network request fails.

    Returns:
        Dict mapping ticker (Alpaca format) to GICS sector string,
        e.g. ``{"AAPL": "Information Technology", "JPM": "Financials"}``.
    """
    global _sector_map_cache
    if _sector_map_cache is not None:
        return _sector_map_cache

    try:
        tables = pd.read_html(
            _WIKI_URL,
            attrs={"id": "constituents"},
            storage_options=_WIKI_STORAGE_OPTS,
        )
        df = tables[0]
        df["Symbol"] = df["Symbol"].str.strip()
        sector_col = "GICS Sector"
        if sector_col not in df.columns:
            logger.warning("Wikipedia S&P 500 table missing '%s' column.", sector_col)
            _sector_map_cache = {}
            return _sector_map_cache

        _sector_map_cache = dict(zip(df["Symbol"], df[sector_col]))
        logger.info(
            "Fetched sector map for %d S&P 500 tickers from Wikipedia.",
            len(_sector_map_cache),
        )
        return _sector_map_cache
    except Exception as exc:
        logger.warning("Failed to fetch sector map from Wikipedia (%s).", exc)
        _sector_map_cache = {}
        return _sector_map_cache


def screen_universe(
    min_avg_volume: int = 1_000_000,
    min_atr_pct: float = 0.01,
    max_atr_pct: float = 0.08,
    momentum_candidates: int = 50,
    lookback_days: int = 30,
    min_rr_ratio: float = 1.5,
    atr_stop_multiplier: float = 2.0,
    max_overbought_zscore: float = 1.0,
) -> List[str]:
    """Screen the S&P 500 universe and return a filtered candidate list.

    Applies five deterministic stages:
      1. Liquidity:   20-day avg volume >= min_avg_volume
      2. Volatility:  ATR(14)/close between min_atr_pct and max_atr_pct
      3. Structural:  price > declining 200-day MA
      4. Signals:     top N by multi-signal union (momentum, volume, MACD)
      5. Quality:     reject overbought (mr_z > threshold) and poor R:R

    Args:
        min_avg_volume: Minimum average daily share volume (default 1M).
        min_atr_pct: Minimum ATR/price ratio — filters out illiquid/too-quiet names.
        max_atr_pct: Maximum ATR/price ratio — filters out excessively volatile names.
        momentum_candidates: Maximum number of tickers to return (default 50).
        lookback_days: Calendar days of OHLCV history to fetch (default 30;
                       provides ~21 trading days, enough for ATR + 20-day return).
        min_rr_ratio: Minimum risk:reward ratio for entry (default 1.5).
        atr_stop_multiplier: ATR multiplier for stop loss (default 2.0).
        max_overbought_zscore: Max mean-reversion z-score (default 1.0).

    Returns:
        Sorted list of ticker symbols — typically 30–50 names.
        Returns a truncated fallback list if data fetch fails entirely.
    """
    universe = get_sp500_tickers()

    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=lookback_days + 10)  # buffer for weekends/holidays

    bars = _fetch_bars(universe, start, end)
    if not bars:
        logger.warning(
            "Screener: bar data fetch returned nothing — "
            "falling back to first %d tickers of the universe.",
            momentum_candidates,
        )
        return universe[:momentum_candidates]

    # Stage 1: Liquidity filter
    liquid = [
        t for t, df in bars.items()
        if len(df) >= 5 and _avg_volume(df) >= min_avg_volume
    ]
    logger.info(
        "Screener stage 1 (liquidity ≥ %s): %d/%d passed.",
        f"{min_avg_volume:,}", len(liquid), len(universe),
    )

    # Stage 2: Volatility filter
    volatile = [
        t for t in liquid
        if min_atr_pct <= _atr_pct(bars[t]) <= max_atr_pct
    ]
    logger.info(
        "Screener stage 2 (ATR/price %.0f%%–%.0f%%): %d/%d passed.",
        min_atr_pct * 100, max_atr_pct * 100, len(volatile), len(liquid),
    )

    # Stage 3: Structural filter — exclude long-term downtrends
    # Price below declining 200-day MA approximates Weinstein Stage 4.
    structural = [t for t in volatile if _passes_structural(bars[t])]
    logger.info(
        "Screener stage 3 (structural: price > declining 200MA): %d/%d passed.",
        len(structural), len(volatile),
    )

    # Stage 4: Multi-signal union pre-screen
    # Any ticker that fires at least one signal qualifies; final ranking by
    # absolute 20-day return ensures the strongest movers surface first.
    # Request more than final target to allow headroom for quality filtering.
    signal_pool_size = int(momentum_candidates * 1.8)
    signal_candidates = _multi_signal_screen(structural, bars, n=signal_pool_size)
    logger.info(
        "Screener stage 4 (multi-signal top %d): %d candidates returned.",
        signal_pool_size, len(signal_candidates),
    )

    # Stage 5: Quality filter — remove decisive disqualifiers
    # These are hard blockers: no amount of other signal strength can
    # compensate for overbought condition or poor risk:reward.
    quality = _quality_filter(
        signal_candidates, bars,
        min_rr=min_rr_ratio,
        atr_mult=atr_stop_multiplier,
        max_mr_z=max_overbought_zscore,
    )
    # Cap to requested count after quality filter
    quality = quality[:momentum_candidates]
    logger.info(
        "Screener stage 5 (quality: R:R>=%.1f, mr_z<=%.1f): %d/%d passed.",
        min_rr_ratio, max_overbought_zscore, len(quality), len(signal_candidates),
    )

    return quality


# ---------------------------------------------------------------------------
# Internal calculation helpers
# ---------------------------------------------------------------------------

def _fetch_bars(
    tickers: List[str],
    start: datetime,
    end: datetime,
) -> Dict[str, pd.DataFrame]:
    """Fetch daily OHLCV bars for all tickers using MarketDataProvider."""
    try:
        from .provider import create_provider
        provider = create_provider()
        return provider.get_bars(tickers, timeframe="day", start=start, end=end)
    except Exception as exc:
        logger.error("Screener: failed to fetch bars: %s", exc)
        return {}


def _avg_volume(df: pd.DataFrame, window: int = 20) -> float:
    """Return average daily volume over the last ``window`` trading days."""
    if df.empty or "volume" not in df.columns:
        return 0.0
    return float(df["volume"].tail(window).mean())


def _atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    """Return ATR(period) as a fraction of the most recent close price.

    Uses Wilder's true range definition:
      TR = max(high-low, |high-prev_close|, |low-prev_close|)
    """
    if len(df) < period + 1:
        return 0.0
    required = {"high", "low", "close"}
    if not required.issubset(df.columns):
        return 0.0

    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = float(true_range.rolling(period).mean().iloc[-1])
    last_close = float(df["close"].iloc[-1])
    if last_close <= 0 or pd.isna(atr):
        return 0.0
    return atr / last_close


def _passes_structural(df: pd.DataFrame) -> bool:
    """Return True if the stock is NOT in a long-term downtrend.

    Rejects stocks where price is below the 200-day MA AND the 200-day MA
    is declining. This approximates Weinstein Stage 4 using only daily bars.
    Stocks with insufficient history (< 200 bars) pass by default.
    """
    if len(df) < 200 or "close" not in df.columns:
        return True
    closes = df["close"]
    ma200 = float(closes.rolling(200).mean().iloc[-1])
    if ma200 <= 0:
        return True
    price = float(closes.iloc[-1])
    price_below = price < ma200
    # Check if 200MA is declining: compare current vs 20 days ago
    ma200_prev = float(closes.rolling(200).mean().iloc[-21])
    ma_declining = ma200 < ma200_prev * 0.998  # > 0.2% decline
    return not (price_below and ma_declining)


def _multi_signal_screen(
    tickers: List[str],
    bars: Dict[str, pd.DataFrame],
    n: int = 50,
    window: int = 20,
    volume_spike_threshold: float = 2.0,
    return_signals: bool = False,
) -> "List[str] | tuple[List[str], Dict[str, set[str]]]":
    """Return top ``n`` tickers that fire at least one entry signal.

    Signals (OR union — any one qualifies):
      1. Strong 20-day return (3%+ move)
      2. Volume spike: latest volume >= volume_spike_threshold × 20-day avg
      3. MACD bullish crossover in the last 2 bars

    Qualified tickers are split into two pools — momentum (positive return)
    and mean-reversion (negative return) — each sorted by magnitude.
    The final list takes n/2 from each pool so the PM always sees a balanced
    mix of breakout and oversold candidates regardless of market regime.

    If return_signals=True, returns (tickers, signal_map) where signal_map
    maps ticker → set of triggered signal names.
    """
    mom_pool: List[tuple[str, float]] = []   # above 20MA (trend continuation)
    mr_pool: List[tuple[str, float]] = []    # near/below 20MA (mean-reversion)
    _signal_map: Dict[str, set[str]] = {}    # ticker → triggered signal names

    for t in tickers:
        df = bars.get(t)
        if df is None or len(df) < window + 1:
            continue

        closes = df["close"].tolist()
        start_close = float(closes[-window - 1])
        end_close = float(closes[-1])
        if start_close <= 0:
            continue
        ret_20d = (end_close - start_close) / start_close

        # Short-term returns for freshness scoring
        close_5 = float(closes[-6]) if len(closes) >= 6 else start_close
        close_15 = float(closes[-16]) if len(closes) >= 16 else start_close
        ret_5d = (end_close - close_5) / close_5 if close_5 > 0 else 0.0
        ret_15d = (end_close - close_15) / close_15 if close_15 > 0 else 0.0

        # Volume ratio (latest vs 20d avg) — used for scoring
        vol_ratio = 1.0
        if "volume" in df.columns and len(df) >= window + 1:
            volumes = df["volume"].tolist()
            avg_vol = float(pd.Series(volumes[-window - 1:-1]).mean())
            if avg_vol > 0:
                vol_ratio = float(volumes[-1]) / avg_vol

        # Price vs 20MA — used for pool assignment to align with
        # _classify_strategy (which also uses price_vs_20ma_pct).
        # This ensures screener MR pool → downstream MR classification.
        ma20 = float(pd.Series(closes).rolling(window).mean().iloc[-1]) if len(closes) >= window else None
        vs_20ma = (end_close - ma20) / ma20 if ma20 and ma20 > 0 else None

        # Signal 1: momentum with acceleration — recent 5d move contributes
        # meaningfully to the 20d trend (not stale drift).
        sig_momentum = (
            abs(ret_20d) >= 0.03
            and abs(ret_5d) > abs(ret_15d) * 0.5
        )

        # Signal 2: volume spike
        sig_volume = vol_ratio >= volume_spike_threshold

        # Signal 3: MACD bullish crossover (last 2 bars)
        sig_macd = False
        if len(closes) >= 27:  # need 26 bars for MACD(12,26,9)
            sig_macd = _macd_bullish_crossover(closes)

        # Signal 4: fresh MR pullback — price below 20MA AND recent decline,
        # filtering out stale below-MA stocks that aren't actively pulling back.
        sig_mr_pullback = (
            vs_20ma is not None
            and vs_20ma < 0
            and ret_5d < -0.02
        )

        # Collect triggered signals for this ticker
        triggered: set[str] = set()
        if sig_momentum:
            triggered.add('momentum')
        if sig_volume:
            triggered.add('volume_spike')
        if sig_macd:
            triggered.add('macd_crossover')
        if sig_mr_pullback:
            triggered.add('mr_pullback')

        if triggered:
            # Pool by price vs 20MA (not return sign) so downstream
            # _classify_strategy sees consistent MR/MOM classification.
            if vs_20ma is not None and vs_20ma < 0:
                # Freshness-weighted: depth below MA + recent drop speed
                mr_score = 0.5 * vs_20ma + 0.5 * ret_5d
                mr_pool.append((t, mr_score))
            else:
                # Freshness-weighted: trend strength + recent acceleration + volume
                mom_score = (
                    0.4 * abs(ret_20d)
                    + 0.4 * abs(ret_5d)
                    + 0.2 * min(vol_ratio / volume_spike_threshold, 1.0)
                )
                mom_pool.append((t, mom_score))
            _signal_map[t] = triggered

    # Sort: momentum by composite score descending, MR by composite ascending
    mom_pool.sort(key=lambda x: x[1], reverse=True)
    mr_pool.sort(key=lambda x: x[1])

    # Take n/2 from each pool; if one pool is short, fill from the other
    half = n // 2
    mom_pick = mom_pool[:half]
    mr_pick = mr_pool[:half]
    remainder = n - len(mom_pick) - len(mr_pick)
    if remainder > 0:
        if len(mom_pick) < half:
            mr_pick = mr_pool[:n - len(mom_pick)]
        else:
            mom_pick = mom_pool[:n - len(mr_pick)]

    result = [t for t, _ in mom_pick] + [t for t, _ in mr_pick]
    if return_signals:
        return result, {t: _signal_map.get(t, set()) for t in result}
    return result


def _quality_filter(
    tickers: List[str],
    bars: Dict[str, pd.DataFrame],
    min_rr: float = 1.5,
    atr_mult: float = 2.0,
    max_mr_z: float = 1.0,
) -> List[str]:
    """Remove tickers with decisive disqualifiers: overbought or poor R:R.

    These are hard blockers — no combination of other signals can compensate.
    Tickers that pass retain their original ordering.

    R:R threshold applies only to momentum candidates. Mean-reversion
    candidates have structurally lower R:R (conservative targets) but
    compensate with higher win rates, so R:R is a ranking factor for MR,
    not a hard filter.
    """
    passed: List[str] = []
    removed_overbought: List[str] = []
    removed_rr: List[str] = []

    for t in tickers:
        df = bars.get(t)
        if df is None or len(df) < 21:
            passed.append(t)
            continue

        closes = df["close"].tolist()
        current_price = closes[-1]

        # Mean-reversion z-score (20-day)
        s = pd.Series(closes, dtype=float)
        ma20 = float(s.rolling(20).mean().iloc[-1])
        std20 = float(s.rolling(20).std().iloc[-1])
        mr_z = (current_price - ma20) / std20 if std20 > 1e-12 else 0.0

        if mr_z > max_mr_z:
            removed_overbought.append(t)
            continue

        # R:R check — only decisive for momentum candidates
        # MR candidates have tighter targets by design; R:R is a ranking
        # factor handled by the quant engine, not a hard blocker.
        is_mr = current_price < ma20
        if not is_mr:
            atr = _atr_pct(df) * current_price
            if atr > 0:
                stop_loss = current_price - atr_mult * atr
                take_profit = current_price + 3.0 * atr
                if stop_loss > 0 and current_price > stop_loss:
                    rr = (take_profit - current_price) / (current_price - stop_loss)
                    if rr < min_rr:
                        removed_rr.append(t)
                        continue

        passed.append(t)

    if removed_overbought:
        logger.info(
            "Screener quality: removed %d overbought: %s",
            len(removed_overbought), removed_overbought[:10],
        )
    if removed_rr:
        logger.info(
            "Screener quality: removed %d momentum poor R:R (<%.1f): %s",
            len(removed_rr), min_rr, removed_rr[:10],
        )
    return passed


def _macd_bullish_crossover(closes: List[float]) -> bool:
    """Check if MACD crossed above signal line in the last 2 bars."""
    s = pd.Series(closes, dtype=float)
    ema12 = s.ewm(span=12, adjust=False).mean()
    ema26 = s.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    diff = macd_line - signal_line
    # Bullish crossover: diff was negative and is now positive
    if len(diff) < 2:
        return False
    return float(diff.iloc[-1]) > 0 and float(diff.iloc[-2]) <= 0


def _top_momentum(
    tickers: List[str],
    bars: Dict[str, pd.DataFrame],
    n: int = 50,
    window: int = 20,
) -> List[str]:
    """Return top ``n`` tickers ranked by absolute 20-day price return.

    Using absolute return means both strong gainers (LONG momentum candidates)
    and strong losers (mean-reversion candidates) are represented in the output.

    Args:
        tickers: Pre-filtered ticker list (after liquidity + volatility stages).
        bars: Dict mapping ticker -> OHLCV DataFrame.
        n: Maximum number of candidates to return.
        window: Number of trading days for the return calculation.

    Returns:
        List of tickers sorted by descending absolute return magnitude.
    """
    scores: List[tuple[str, float]] = []
    for t in tickers:
        df = bars.get(t)
        if df is None or len(df) < window + 1:
            continue
        start_close = float(df["close"].iloc[-window - 1])
        end_close = float(df["close"].iloc[-1])
        if start_close <= 0:
            continue
        ret = abs((end_close - start_close) / start_close)
        scores.append((t, ret))

    scores.sort(key=lambda x: x[1], reverse=True)
    return [t for t, _ in scores[:n]]
