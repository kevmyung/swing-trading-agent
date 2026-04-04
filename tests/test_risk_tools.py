"""
tests/test_risk_tools.py — Unit tests for risk tool modules.
"""

import json
from datetime import date

import pytest
from tools.risk.position_sizing import calculate_position_size
from tools.risk.drawdown import check_drawdown

# ---------------------------------------------------------------------------
# Position sizing tests
# ---------------------------------------------------------------------------

def test_position_size_basic():
    result = calculate_position_size(
        ticker='AAPL', entry_price=185.0, atr=3.5,
        portfolio_value=100_000.0, current_position_count=2
    )
    assert result['approved'] is True
    assert result['shares'] > 0
    assert result['stop_loss_price'] == pytest.approx(185.0 - 3.5 * 2.0)
    assert result['dollar_risk'] == pytest.approx(100_000.0 * 0.02)


def test_position_size_max_positions_reached():
    result = calculate_position_size(
        ticker='AAPL', entry_price=185.0, atr=3.5,
        portfolio_value=100_000.0, current_position_count=8, max_positions=8
    )
    assert result['approved'] is False
    assert 'max_positions' in result['rejection_reason']


def test_position_size_invalid_atr():
    result = calculate_position_size(
        ticker='AAPL', entry_price=185.0, atr=0.0,
        portfolio_value=100_000.0, current_position_count=0
    )
    assert result['approved'] is False


def test_position_size_too_small_portfolio():
    """Very small portfolio -> shares < 1 -> rejected."""
    result = calculate_position_size(
        ticker='AAPL', entry_price=185.0, atr=3.5,
        portfolio_value=100.0, current_position_count=0  # only $100 portfolio
    )
    assert result['approved'] is False


def test_position_size_shares_calculation():
    """shares = min(floor(dollar_risk / risk_per_share), max_by_value_cap)."""
    import math
    entry = 100.0
    atr = 5.0
    portfolio = 50_000.0
    stop = entry - atr * 2.0
    risk_per_share = entry - stop
    dollar_risk = portfolio * 0.02
    shares_by_risk = math.floor(dollar_risk / risk_per_share)
    # Single position capped at 15% of portfolio value
    max_shares_by_value = math.floor(portfolio * 0.15 / entry)
    expected_shares = min(shares_by_risk, max_shares_by_value)

    result = calculate_position_size(
        ticker='TEST', entry_price=entry, atr=atr,
        portfolio_value=portfolio, current_position_count=0
    )
    assert result['approved'] is True
    assert result['shares'] == expected_shares


def test_position_size_rejection_reason_none_when_approved():
    result = calculate_position_size(
        ticker='AAPL', entry_price=185.0, atr=3.5,
        portfolio_value=100_000.0, current_position_count=0
    )
    assert result['approved'] is True
    assert result['rejection_reason'] is None


def test_position_size_invalid_entry_price():
    result = calculate_position_size(
        ticker='AAPL', entry_price=0.0, atr=3.5,
        portfolio_value=100_000.0, current_position_count=0
    )
    assert result['approved'] is False
    assert result['rejection_reason'] == 'invalid_entry_price'
def test_drawdown_normal():
    result = check_drawdown(current_value=98_000.0, peak_value=100_000.0)
    assert result['current_drawdown_pct'] == pytest.approx(0.02)
    assert result['status'] == 'NORMAL'
    assert result['allow_new_trades'] is True
    assert result['position_size_multiplier'] == 1.0


def test_drawdown_caution():
    result = check_drawdown(current_value=93_000.0, peak_value=100_000.0)
    assert result['status'] == 'CAUTION'
    assert result['position_size_multiplier'] == 0.75


def test_drawdown_warning():
    result = check_drawdown(current_value=88_000.0, peak_value=100_000.0)
    assert result['status'] == 'WARNING'
    assert result['position_size_multiplier'] == 0.5


def test_drawdown_halt():
    result = check_drawdown(current_value=84_000.0, peak_value=100_000.0)
    assert result['status'] == 'HALT'
    assert result['allow_new_trades'] is False
    assert result['position_size_multiplier'] == 0.0


def test_drawdown_at_peak():
    result = check_drawdown(current_value=100_000.0, peak_value=100_000.0)
    assert result['current_drawdown_pct'] == 0.0
    assert result['status'] == 'NORMAL'


def test_drawdown_zero_peak():
    """peak_value <= 0 should return NORMAL with 0 drawdown."""
    result = check_drawdown(current_value=50_000.0, peak_value=0.0)
    assert result['status'] == 'NORMAL'
    assert result['current_drawdown_pct'] == 0.0


def test_drawdown_above_peak():
    """Portfolio above peak -> 0 drawdown (clamped)."""
    result = check_drawdown(current_value=105_000.0, peak_value=100_000.0)
    assert result['current_drawdown_pct'] == 0.0
    assert result['status'] == 'NORMAL'

# ---------------------------------------------------------------------------
# Earnings gap history and context tests
# ---------------------------------------------------------------------------

from datetime import date
from tools.quant.earnings_risk import (
    compute_earnings_gap_history, build_earnings_context,
)


def test_earnings_gap_history_basic():
    """Compute gap stats from bars and earnings dates."""
    import pandas as pd
    import numpy as np

    # Create simple bar data: 60 trading days
    dates = pd.bdate_range('2025-01-01', periods=60)
    np.random.seed(42)
    closes = 100 + np.cumsum(np.random.randn(60) * 0.5)
    opens = closes + np.random.randn(60) * 0.3
    df = pd.DataFrame({
        'open': opens, 'high': closes + 1, 'low': closes - 1,
        'close': closes, 'volume': 1_000_000,
    }, index=dates)

    # Pick two dates that fall within our bar range
    past_dates = [date(2025, 2, 3), date(2025, 1, 15)]
    result = compute_earnings_gap_history(df, past_dates)

    assert result is not None
    assert result['quarters_analyzed'] == 2
    assert result['avg_abs_gap'] > 0
    assert result['max_abs_gap'] >= result['avg_abs_gap']
    assert result['avg_abs_move'] > 0


def test_earnings_gap_history_no_data():
    """Returns None when no bars or no dates provided."""
    assert compute_earnings_gap_history(None, []) is None
    assert compute_earnings_gap_history(None, [date(2025, 1, 1)]) is None


def test_build_earnings_context_with_history():
    """Build context with gap history and cushion ratios."""
    gap_history = {
        'quarters_analyzed': 4,
        'avg_abs_gap': 2.0,
        'max_abs_gap': 5.0,
        'avg_abs_move': 2.5,
        'max_abs_move': 6.0,
    }
    ctx = build_earnings_context(
        days_to_earnings=3,
        unrealized_pnl_pct=0.06,  # +6%
        gap_history=gap_history,
    )
    assert ctx['earnings_days_away'] == 3
    assert ctx['earnings_history'] == gap_history
    # 6% / 2% avg_gap = 3.0x cushion
    assert ctx['cushion_vs_avg'] == 3.0
    # 6% / 5% max_gap = 1.2x cushion
    assert ctx['cushion_vs_max'] == 1.2


def test_build_earnings_context_no_history():
    """Build context without gap history — just days_away."""
    ctx = build_earnings_context(
        days_to_earnings=5,
        unrealized_pnl_pct=0.03,
        gap_history=None,
    )
    assert ctx['earnings_days_away'] == 5
    assert 'earnings_history' not in ctx
    assert 'cushion_vs_avg' not in ctx


def _make_state(**kwargs) -> str:
    """Build a minimal portfolio_state JSON string."""
    defaults = {
        'portfolio_value': 100_000.0,
        'cash': 50_000.0,
        'position_count': 2,
        'current_drawdown_pct': 0.0,
        'daily_loss_pct': 0.0,
        'consecutive_losses': 0,
        'positions': [],
        'trading_day': str(date.today()),
    }
    defaults.update(kwargs)
    return json.dumps(defaults)
