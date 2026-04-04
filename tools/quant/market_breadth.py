"""
tools/quant/market_breadth.py — Market breadth indicators from ETF price data.

Measures market health beyond index returns by comparing equal-weight vs
cap-weight performance, sector participation, small-cap leadership, and
credit conditions. All computed from OHLCV bars — no additional API calls.

Breadth tickers are fetched alongside SPY/QQQ in the same batch request
managed by QuantEngine._fetch_bars().
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ETFs used for breadth computation (fetched in the same batch as SPY/QQQ)
BREADTH_TICKERS: list[str] = [
    "RSP",   # S&P 500 Equal Weight — breadth proxy
    "IWM",   # Russell 2000 — small-cap participation
    "HYG",   # High Yield Corporate Bond — credit proxy
    "TLT",   # 20+ Year Treasury — risk-free / flight-to-safety proxy
]

# GICS sector ETFs for sector participation breadth
SECTOR_TICKERS: list[str] = [
    "XLK",   # Technology
    "XLF",   # Financials
    "XLV",   # Health Care
    "XLE",   # Energy
    "XLI",   # Industrials
    "XLC",   # Communication Services
    "XLY",   # Consumer Discretionary
    "XLP",   # Consumer Staples
    "XLB",   # Materials
    "XLRE",  # Real Estate
    "XLU",   # Utilities
]

ALL_BREADTH_TICKERS: list[str] = BREADTH_TICKERS + SECTOR_TICKERS


def compute_market_breadth(bars: dict[str, Any]) -> dict:
    """Compute market breadth indicators from pre-fetched ETF bars.

    Args:
        bars: Dict of ticker → DataFrame (daily OHLCV). Must include SPY
              and as many of ALL_BREADTH_TICKERS as available.

    Returns:
        Dict with:
          - breadth_score: composite score (-1.0 to +1.0)
          - rsp_vs_spy_5d / rsp_vs_spy_20d: equal-weight relative strength
          - sectors_positive_5d / sectors_positive_20d: count of sectors with positive returns
          - sector_momentum: per-sector return data with rank
          - iwm_vs_spy_5d: small-cap relative strength
          - credit_trend: "improving" / "stable" / "deteriorating"
    """
    result: dict = {
        "breadth_score": 0.0,
        "rsp_vs_spy_5d": None,
        "rsp_vs_spy_20d": None,
        "sectors_positive_5d": None,
        "sectors_positive_20d": None,
        "sector_momentum": {},
        "iwm_vs_spy_5d": None,
        "credit_trend": None,
    }

    spy_df = bars.get("SPY")
    if spy_df is None or spy_df.empty or len(spy_df) < 21:
        return result

    spy_closes = spy_df["close"].tolist()
    components: list[float] = []  # sub-scores for composite breadth

    # --- 1. RSP vs SPY: equal-weight relative strength ---
    rsp_df = bars.get("RSP")
    if rsp_df is not None and len(rsp_df) >= 21:
        rsp_closes = rsp_df["close"].tolist()
        rsp_spy_5d = _relative_return(rsp_closes, spy_closes, 5)
        rsp_spy_20d = _relative_return(rsp_closes, spy_closes, 20)
        result["rsp_vs_spy_5d"] = rsp_spy_5d
        result["rsp_vs_spy_20d"] = rsp_spy_20d
        # Score: positive relative strength = breadth healthy
        if rsp_spy_20d is not None:
            components.append(_clip(rsp_spy_20d * 20, -1.0, 1.0))  # scale ±5% → ±1.0

    # --- 2. Sector participation ---
    sector_returns_5d: dict[str, float] = {}
    sector_returns_20d: dict[str, float] = {}
    for ticker in SECTOR_TICKERS:
        df = bars.get(ticker)
        if df is None or df.empty or len(df) < 21:
            continue
        closes = df["close"].tolist()
        r5 = _period_return(closes, 5)
        r20 = _period_return(closes, 20)
        if r5 is not None:
            sector_returns_5d[ticker] = r5
        if r20 is not None:
            sector_returns_20d[ticker] = r20

    if sector_returns_5d:
        pos_5d = sum(1 for r in sector_returns_5d.values() if r > 0)
        result["sectors_positive_5d"] = pos_5d
    if sector_returns_20d:
        pos_20d = sum(1 for r in sector_returns_20d.values() if r > 0)
        result["sectors_positive_20d"] = pos_20d
        # Score: 11 sectors → normalize to [-1, 1]
        n = len(sector_returns_20d)
        if n > 0:
            participation_ratio = pos_20d / n
            components.append(participation_ratio * 2.0 - 1.0)  # 0→-1, 0.5→0, 1→+1

    # Sector momentum table with ranks
    if sector_returns_5d:
        ranked = sorted(sector_returns_5d.items(), key=lambda x: x[1], reverse=True)
        for rank, (ticker, r5) in enumerate(ranked, 1):
            result["sector_momentum"][ticker] = {
                "return_5d": round(r5, 4),
                "return_20d": round(sector_returns_20d.get(ticker, 0.0), 4),
                "rank": rank,
            }

    # --- 3. IWM vs SPY: small-cap participation ---
    iwm_df = bars.get("IWM")
    if iwm_df is not None and len(iwm_df) >= 6:
        iwm_closes = iwm_df["close"].tolist()
        iwm_spy_5d = _relative_return(iwm_closes, spy_closes, 5)
        result["iwm_vs_spy_5d"] = iwm_spy_5d
        if iwm_spy_5d is not None:
            components.append(_clip(iwm_spy_5d * 15, -1.0, 1.0))

    # --- 4. Credit spread proxy: HYG / TLT ratio trend ---
    hyg_df = bars.get("HYG")
    tlt_df = bars.get("TLT")
    if (hyg_df is not None and len(hyg_df) >= 21 and
            tlt_df is not None and len(tlt_df) >= 21):
        hyg_closes = hyg_df["close"].tolist()
        tlt_closes = tlt_df["close"].tolist()
        n = min(len(hyg_closes), len(tlt_closes))
        hyg_closes = hyg_closes[-n:]
        tlt_closes = tlt_closes[-n:]

        # HYG/TLT ratio: rising = credit improving, falling = stress
        ratio_now = hyg_closes[-1] / tlt_closes[-1] if tlt_closes[-1] > 0 else 0.0
        ratio_20d = hyg_closes[-21] / tlt_closes[-21] if tlt_closes[-21] > 0 else 0.0
        if ratio_20d > 0:
            credit_change = (ratio_now - ratio_20d) / ratio_20d
            if credit_change > 0.01:
                result["credit_trend"] = "improving"
            elif credit_change < -0.01:
                result["credit_trend"] = "deteriorating"
            else:
                result["credit_trend"] = "stable"
            components.append(_clip(credit_change * 30, -1.0, 1.0))

    # --- Composite breadth score ---
    if components:
        result["breadth_score"] = round(float(np.mean(components)), 2)

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _period_return(closes: list[float], period: int) -> float | None:
    """Return over the last *period* trading days."""
    if len(closes) < period + 1 or closes[-(period + 1)] <= 0:
        return None
    return round(closes[-1] / closes[-(period + 1)] - 1.0, 4)


def _relative_return(
    ticker_closes: list[float],
    bench_closes: list[float],
    period: int,
) -> float | None:
    """Relative return of ticker vs benchmark over *period* days."""
    tr = _period_return(ticker_closes, period)
    br = _period_return(bench_closes, period)
    if tr is None or br is None:
        return None
    return round(tr - br, 4)


def _clip(value: float, lo: float, hi: float) -> float:
    """Clamp *value* to [lo, hi]."""
    return max(lo, min(hi, value))
