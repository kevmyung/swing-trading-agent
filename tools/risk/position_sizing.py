"""
tools/risk/position_sizing.py — Position sizing tool for the RiskAgent.

Implements fixed 2% fractional position sizing with ATR-based stop-loss.
All parameters are read from Settings to allow env-based configuration.
"""

from __future__ import annotations

import logging
import math


logger = logging.getLogger(__name__)


# Maximum participation rate: we never trade more than this fraction of
# a stock's average daily volume in a single order to avoid market impact.
_MAX_PARTICIPATION_RATE = 0.01  # 1% of avg daily volume


def estimate_spread_cost_bps(
    avg_daily_volume: int,
    price: float,
    atr: float,
) -> float:
    """Estimate round-trip spread cost in basis points based on liquidity and volatility.

    Volume tiers:
      >10M shares/day: ~3 bps (mega-cap, e.g. AAPL)
      >5M:             ~7 bps (large-cap)
      >1M:             ~12 bps (mid-cap)
      <=1M or unknown: ~25 bps (small-cap / low liquidity)

    Volatility adjustment: high ATR% widens effective spreads.
    """
    if avg_daily_volume > 10_000_000:
        base_bps = 3.0
    elif avg_daily_volume > 5_000_000:
        base_bps = 7.0
    elif avg_daily_volume > 1_000_000:
        base_bps = 12.0
    elif avg_daily_volume > 0:
        base_bps = 25.0
    else:
        base_bps = 15.0  # unknown volume — conservative default

    atr_pct = atr / price if price > 0 else 0
    if atr_pct > 0.05:
        base_bps *= 2.0
    elif atr_pct > 0.03:
        base_bps *= 1.5

    return base_bps


def calculate_position_size(
    ticker: str,
    entry_price: float,
    atr: float,
    portfolio_value: float,
    current_position_count: int,
    max_positions: int = 10,
    position_size_pct: float = 0.02,
    atr_stop_multiplier: float = 2.0,
    avg_daily_volume: int = 0,
) -> dict:
    """
    Calculate position size using fixed fractional risk (2% of portfolio).

    Applies three caps to the raw risk-based share count:
      1. Risk cap: shares = floor(dollar_risk / risk_per_share)
      2. Value cap: single position <= 15% of portfolio value
      3. Liquidity cap: shares <= avg_daily_volume * 1% (if volume data available)

    Spread cost is estimated and deducted from the effective reward to
    produce a more realistic R:R ratio (spread_adjusted_rr).

    Args:
        ticker: Stock symbol
        entry_price: Proposed entry price
        atr: ATR(14) value
        portfolio_value: Current total portfolio value
        current_position_count: Number of currently open positions
        max_positions: Hard ceiling for concurrent positions (default 10)
        position_size_pct: Fraction of portfolio to risk per trade (default 0.02)
        atr_stop_multiplier: ATR multiplier for stop loss (default 2.0)
        avg_daily_volume: 20-day average daily volume (0 = skip liquidity cap)

    Returns:
        Dict: {ticker, entry_price, stop_loss_price, shares, dollar_risk,
               portfolio_pct_at_risk, approved, rejection_reason,
               liquidity_capped, estimated_spread_cost, spread_adjusted_rr}
    """
    def _reject(reason: str) -> dict:
        return {
            'ticker': ticker,
            'entry_price': entry_price,
            'stop_loss_price': None,
            'shares': 0,
            'dollar_risk': 0.0,
            'portfolio_pct_at_risk': 0.0,
            'approved': False,
            'rejection_reason': reason,
        }

    # Validation checks
    if current_position_count >= max_positions:
        return _reject('max_positions_reached')

    if entry_price <= 0:
        return _reject('invalid_entry_price')

    if atr <= 0:
        return _reject('invalid_atr')

    stop_loss_price = entry_price - (atr * atr_stop_multiplier)

    if stop_loss_price <= 0:
        return _reject('stop_loss_below_zero')

    risk_per_share = entry_price - stop_loss_price
    dollar_risk = portfolio_value * position_size_pct
    shares = math.floor(dollar_risk / risk_per_share)

    # Cap single position at 15% of portfolio value
    max_position_value = portfolio_value * 0.15
    max_shares_by_value = math.floor(max_position_value / entry_price)
    shares = min(shares, max_shares_by_value)

    # Liquidity cap: don't exceed 1% of avg daily volume
    liquidity_capped = False
    if avg_daily_volume > 0:
        max_shares_by_liquidity = math.floor(avg_daily_volume * _MAX_PARTICIPATION_RATE)
        if shares > max_shares_by_liquidity:
            shares = max_shares_by_liquidity
            liquidity_capped = True

    if shares < 1:
        return _reject('position_too_small')

    portfolio_pct_at_risk = (shares * risk_per_share) / portfolio_value if portfolio_value > 0 else 0.0

    # Estimated round-trip spread cost (dynamic by liquidity and volatility)
    spread_bps = estimate_spread_cost_bps(avg_daily_volume, entry_price, atr)
    spread_cost_per_share = entry_price * spread_bps / 10_000
    total_spread_cost = round(spread_cost_per_share * shares, 2)

    # Spread-adjusted R:R — deduct spread from reward side
    spread_adjusted_rr = None
    if risk_per_share > 0 and atr > 0:
        gross_reward = 3.0 * atr  # ATR×3 target (conservative estimate)
        net_reward = gross_reward - spread_cost_per_share
        spread_adjusted_rr = round(net_reward / risk_per_share, 2) if net_reward > 0 else 0.0

    return {
        'ticker': ticker,
        'entry_price': round(entry_price, 4),
        'stop_loss_price': round(stop_loss_price, 4),
        'shares': shares,
        'dollar_risk': round(dollar_risk, 2),
        'portfolio_pct_at_risk': round(portfolio_pct_at_risk, 4),
        'approved': True,
        'rejection_reason': None,
        'liquidity_capped': liquidity_capped,
        'estimated_spread_cost': total_spread_cost,
        'spread_cost_bps': round(spread_bps, 1),
        'spread_adjusted_rr': spread_adjusted_rr,
    }


