"""
tools/quant/earnings_risk.py — Earnings gap history and cushion analysis.

Computes empirical earnings-day price gap statistics from historical data,
and calculates cushion ratios for held positions approaching earnings.
No scoring — provides factual context for LLM judgment.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

logger = logging.getLogger(__name__)


def compute_earnings_gap_history(
    bars_df: pd.DataFrame,
    past_earnings_dates: list[date],
) -> dict | None:
    """Compute gap statistics from actual earnings-day price moves.

    For each past earnings date, calculates:
      - Overnight gap: previous close → next open
      - Full-day move: previous close → earnings-day close

    Args:
        bars_df: OHLCV DataFrame with DatetimeIndex.
        past_earnings_dates: List of past earnings announcement dates.

    Returns:
        Dict with gap statistics, or None if insufficient data.
    """
    if bars_df is None or bars_df.empty or not past_earnings_dates:
        return None

    abs_gaps: list[float] = []
    abs_moves: list[float] = []

    for earn_date in past_earnings_dates:
        after_mask = bars_df.index.date >= earn_date
        before_mask = bars_df.index.date < earn_date
        if not after_mask.any() or not before_mask.any():
            continue

        day_after = bars_df[after_mask].iloc[0]
        day_before = bars_df[before_mask].iloc[-1]

        prev_close = day_before['close']
        if prev_close <= 0:
            continue

        gap = (day_after['open'] / prev_close - 1.0) * 100
        move = (day_after['close'] / prev_close - 1.0) * 100

        abs_gaps.append(abs(gap))
        abs_moves.append(abs(move))

    if not abs_gaps:
        return None

    return {
        'quarters_analyzed': len(abs_gaps),
        'avg_abs_gap': round(sum(abs_gaps) / len(abs_gaps), 2),
        'max_abs_gap': round(max(abs_gaps), 2),
        'avg_abs_move': round(sum(abs_moves) / len(abs_moves), 2),
        'max_abs_move': round(max(abs_moves), 2),
    }


def build_earnings_context(
    days_to_earnings: int,
    unrealized_pnl_pct: float,
    gap_history: dict | None,
    position_weight_pct: float = 0.0,
) -> dict:
    """Build earnings context for a held position approaching earnings.

    Combines gap history with current position state to produce
    factual context the LLM can reason about.

    Args:
        days_to_earnings: Trading days until earnings.
        unrealized_pnl_pct: Unrealized P&L as fraction (e.g. 0.08 = +8%).
        gap_history: Output from compute_earnings_gap_history(), or None.
        position_weight_pct: Position weight as fraction of portfolio.

    Returns:
        Dict with earnings context fields for LLM prompt injection.
    """
    ctx: dict = {
        'earnings_days_away': days_to_earnings,
    }

    if gap_history:
        ctx['earnings_history'] = gap_history
        avg_gap = gap_history['avg_abs_gap']
        max_gap = gap_history['max_abs_gap']

        # Cushion ratios: P&L / gap (how many gaps of cushion)
        pnl_pct = unrealized_pnl_pct * 100  # convert to percentage points
        if avg_gap > 0:
            ctx['cushion_vs_avg'] = round(pnl_pct / avg_gap, 1)
        if max_gap > 0:
            ctx['cushion_vs_max'] = round(pnl_pct / max_gap, 1)

    return ctx


def fetch_past_earnings_dates(ticker: str, n_quarters: int = 8) -> list[date]:
    """Fetch past earnings dates, checking fixture first then yfinance.

    Uses the shared fixture loader from tools.sentiment.earnings which
    handles both legacy (list of date strings) and new (list of dicts) formats.

    Args:
        ticker: Stock symbol.
        n_quarters: Number of past quarters to retrieve (default 8).

    Returns:
        List of past earnings dates (most recent first), or empty list on failure.
    """
    from tools.sentiment.earnings import _load_earnings_fixture

    fixture = _load_earnings_fixture()
    entries = fixture.get(ticker.upper(), [])
    if entries:
        dates: list[date] = []
        for e in entries:
            d_str = e.get("date", "")
            # Skip future dates (reported_eps explicitly None in new format)
            if "reported_eps" in e and e["reported_eps"] is None:
                continue
            try:
                dates.append(date.fromisoformat(d_str))
            except (ValueError, TypeError):
                continue
        if dates:
            return dates[:n_quarters]

    # Fallback to live yfinance
    try:
        import yfinance as yf

        t = yf.Ticker(ticker)
        ed = t.earnings_dates
        if ed is None or ed.empty:
            return []

        past = ed[ed['Reported EPS'].notna()].head(n_quarters)
        return [dt.date() for dt in past.index]

    except Exception as exc:
        logger.debug("Failed to fetch earnings dates for %s: %s", ticker, exc)
        return []
