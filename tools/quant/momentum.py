"""
tools/quant/momentum.py — Momentum factor tools for the QuantAgent.

Implements 12-1 cross-sectional momentum scoring.

Reference: Jegadeesh & Titman (1993) "Returns to Buying Winners and Selling
Losers: Implications for Stock Market Efficiency".
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from scipy import stats


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Private helper functions
# ---------------------------------------------------------------------------

def _compute_momentum_return(
    prices: pd.Series,
    lookback: int = 252,
    skip: int = 21,
) -> Optional[float]:
    """Compute 12-1 momentum return for a single price series.

    Args:
        prices: Closing prices indexed oldest-first.
        lookback: Total lookback in trading days (default 252 ≈ 12 months).
        skip: Skip period to avoid short-term reversal (default 21 ≈ 1 month).

    Returns:
        ``price[t-skip] / price[t-lookback] - 1``, or ``None`` if the series
        is too short or the anchor price is non-positive.
    """
    if len(prices) < lookback + 1:
        return None
    p_recent = float(prices.iloc[-skip - 1])
    p_old = float(prices.iloc[-lookback - 1])
    if p_old <= 0:
        return None
    return p_recent / p_old - 1


def _cross_sectional_zscore(returns: Dict[str, float]) -> Dict[str, float]:
    """Compute cross-sectional z-scores across a universe of returns.

    Args:
        returns: Dict mapping ticker -> raw momentum return.

    Returns:
        Dict mapping ticker -> z-score ``(r - mean) / std``.
        Returns all-zeros when ``std == 0`` or the dict is empty.
    """
    if not returns:
        return {}
    vals = np.array(list(returns.values()), dtype=float)
    mean = float(vals.mean())
    std = float(vals.std())
    if std < 1e-12 or np.isnan(std):
        return {t: 0.0 for t in returns}
    return {t: float((r - mean) / std) for t, r in returns.items()}


def _spearman_rank_correlation(x: list, y: list) -> float:
    """Compute Spearman rank correlation between two lists.

    Returns 0.0 when the lists are too short, have zero variance, or
    ``scipy`` raises an exception.
    """
    try:
        if len(x) < 2 or len(y) < 2:
            return 0.0
        corr, _ = stats.spearmanr(x, y)
        return 0.0 if np.isnan(corr) else float(corr)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

def calculate_momentum_scores(
    tickers: List[str] = None,
    as_of_date: str = None,
    price_data: Optional[Dict[str, Any]] = None,
    lookback: int = 252,
    skip: int = 21,
) -> dict:
    """Calculate 12-1 cross-sectional momentum scores.

    Args:
        tickers: List of tickers to score.
        as_of_date: ISO date string for the signal date (e.g. ``"2024-01-15"``).
                    Only prices on or before this date are used.
        price_data: Dict mapping ticker -> DataFrame with a ``'close'`` column
                    and a ``DatetimeIndex``.  When a ticker is absent the
                    function attempts a live fetch via ``MarketDataProvider``;
                    if that is also unavailable the ticker is skipped.
        lookback: Lookback window in trading days (default 252 ≈ 12 months).
        skip: Skip period in trading days (default 21 ≈ 1 month).

    Returns:
        ``{signal_date, universe_size, signals}`` where ``signals`` is a list
        sorted by ``z_score`` descending.  Each element contains
        ``{ticker, momentum_return, z_score, rank, action}``.
    """
    if tickers is None:
        tickers = []

    as_of_ts = pd.Timestamp(as_of_date) if as_of_date else pd.Timestamp.now()

    raw_returns: Dict[str, float] = {}

    for ticker in tickers:
        df = _resolve_price_df(ticker, price_data, as_of_ts)
        if df is None or df.empty:
            continue
        mom_ret = _compute_momentum_return(
            df["close"], lookback=lookback, skip=skip
        )
        if mom_ret is None:
            continue
        raw_returns[ticker] = mom_ret

    return _build_signals(raw_returns, as_of_ts.date().isoformat())


def _resolve_price_df(
    ticker: str,
    price_data: Optional[Dict[str, "pd.DataFrame"]],
    as_of: "pd.Timestamp",
) -> Optional["pd.DataFrame"]:
    """Return a DataFrame of prices up to *as_of*, or None on failure."""
    if price_data is not None and ticker in price_data:
        df = price_data[ticker]
        return df[df.index <= as_of]

    # Attempt live fetch from MarketDataProvider
    try:
        from tools.data.provider import create_provider  # noqa: PLC0415
        provider = create_provider()
        start = (as_of - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
        end = as_of.strftime("%Y-%m-%d")
        bars = provider.get_bars([ticker], start=start, end=end)
        return bars.get(ticker)
    except Exception as exc:
        logger.debug("Could not fetch live data for %s: %s", ticker, exc)
        return None


def _build_signals(
    raw_returns: Dict[str, float],
    signal_date: str,
) -> dict:
    """Convert raw returns dict to the standard signals output."""
    if not raw_returns:
        return {"signal_date": signal_date, "universe_size": 0, "signals": []}

    z_scores = _cross_sectional_zscore(raw_returns)
    sorted_tickers = sorted(z_scores, key=lambda t: z_scores[t], reverse=True)

    signals = []
    for rank, ticker in enumerate(sorted_tickers, start=1):
        z = z_scores[ticker]
        action = "LONG" if z > 1.0 else ("SHORT" if z < -1.0 else "NEUTRAL")
        signals.append({
            "ticker": ticker,
            "momentum_return": round(raw_returns[ticker], 6),
            "z_score": round(z, 4),
            "rank": rank,
            "action": action,
        })

    return {
        "signal_date": signal_date,
        "universe_size": len(signals),
        "signals": signals,
    }


def get_momentum_ic(
    historical_scores: list = None,
) -> dict:
    """Calculate rolling Information Coefficient (IC) for the momentum factor.

    IC = Spearman rank correlation between momentum scores and forward returns.
    Used to detect alpha decay.

    Args:
        historical_scores: List of period dicts, each containing:

            * ``"scores"`` — ``{ticker: momentum_score}``
            * ``"forward_returns"`` — ``{ticker: forward_return}``

            The IC is computed for each period and averaged.

    Returns:
        ``{rolling_ic, ic_status, size_multiplier}`` where ``ic_status`` is
        ``'STRONG'`` / ``'MODERATE'`` / ``'DECAY'``.
    """
    if historical_scores is not None:
        return _ic_from_historical_scores(historical_scores)

    return {"rolling_ic": 0.0, "ic_status": "DECAY", "size_multiplier": 0.5}


def _ic_from_historical_scores(historical_scores: list) -> dict:
    """Compute IC from pre-computed score/forward-return pairs."""
    ic_values: List[float] = []

    for period in historical_scores:
        scores = period.get("scores", {})
        fwd_rets = period.get("forward_returns", {})
        common = [t for t in scores if t in fwd_rets]
        if len(common) < 3:
            continue
        ic = _spearman_rank_correlation(
            [scores[t] for t in common],
            [fwd_rets[t] for t in common],
        )
        ic_values.append(ic)

    return _ic_result(ic_values)


def _ic_result(ic_values: List[float]) -> dict:
    """Convert a list of per-period IC values to the standard output dict."""
    if not ic_values:
        return {"rolling_ic": 0.0, "ic_status": "DECAY", "size_multiplier": 0.5}

    rolling_ic = float(np.mean(ic_values))

    if rolling_ic > 0.05:
        ic_status = "STRONG"
        size_multiplier = 1.0
    elif rolling_ic >= 0.02:
        ic_status = "MODERATE"
        size_multiplier = 0.75
    else:
        ic_status = "DECAY"
        size_multiplier = 0.5

    return {
        "rolling_ic": round(rolling_ic, 4),
        "ic_status": ic_status,
        "size_multiplier": size_multiplier,
    }
