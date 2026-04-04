"""
tests/test_portfolio_state.py — Unit tests for PortfolioState.

Tests cover: load/save round-trip, position CRUD, P&L calculation,
drawdown properties, consecutive loss tracking, and atomic write safety.
"""

import os
import json
import pytest
from state.portfolio_state import PortfolioState, Position, Trade


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def state(tmp_path):
    """Fresh PortfolioState backed by a temp file, pre-seeded with $100k."""
    s = PortfolioState(state_file=str(tmp_path / "portfolio.json"))
    s.cash = 100_000.0
    s.portfolio_value = 100_000.0
    s.peak_value = 100_000.0
    s.daily_start_value = 100_000.0
    return s


def _make_position(symbol="AAPL", qty=10, entry=180.0, current=185.0,
                   stop=175.0, strategy="MOMENTUM") -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry_price=entry,
        current_price=current,
        stop_loss_price=stop,
        strategy=strategy,
    )


def _make_trade(symbol="AAPL", pnl=100.0, strategy="MOMENTUM") -> Trade:
    return Trade(
        symbol=symbol,
        side="sell",
        qty=10,
        price=190.0,
        pnl=pnl,
        timestamp="2024-01-01T00:00:00Z",
        strategy=strategy,
    )


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------

def test_load_no_file_does_not_raise(tmp_path):
    """load() on a missing file should initialise silently."""
    s = PortfolioState(state_file=str(tmp_path / "missing.json"))
    s.load()  # must not raise
    assert s.cash == 0.0
    assert s.positions == {}


def test_load_sets_last_synced(tmp_path):
    """load() should set last_synced to a non-empty UTC timestamp."""
    s = PortfolioState(state_file=str(tmp_path / "missing.json"))
    s.load()
    assert s.last_synced.endswith("Z")


def test_load_corrupt_file_does_not_raise(tmp_path):
    """load() on a corrupt JSON file should not raise."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not valid json {{{{")
    s = PortfolioState(state_file=str(bad_file))
    s.load()
    assert s.cash == 0.0


# ---------------------------------------------------------------------------
# save() and round-trip
# ---------------------------------------------------------------------------

def test_save_creates_file(state, tmp_path):
    """save() must create the state file."""
    state.save()
    assert os.path.exists(state.state_file)
def test_atomic_save_no_tmp_left_behind(state):
    """After save(), the .tmp file must not remain on disk."""
    state.save()
    assert not os.path.exists(state.state_file + ".tmp")
    assert os.path.exists(state.state_file)
def test_consecutive_losses_increment_on_loss(state):
    """Each losing trade increments consecutive_losses by 1."""
    state.record_trade(_make_trade(pnl=-100.0))
    assert state.consecutive_losses == 1
    state.record_trade(_make_trade(pnl=-50.0))
    assert state.consecutive_losses == 2


def test_consecutive_losses_reset_on_win(state):
    """A winning trade resets consecutive_losses to 0."""
    state.record_trade(_make_trade(pnl=-100.0))
    state.record_trade(_make_trade(pnl=-100.0))
    state.record_trade(_make_trade(pnl=50.0))
    assert state.consecutive_losses == 0


def test_consecutive_losses_breakeven_resets(state):
    """A breakeven trade (pnl=0) also resets consecutive_losses."""
    state.record_trade(_make_trade(pnl=-100.0))
    state.record_trade(_make_trade(pnl=0.0))
    assert state.consecutive_losses == 0


def test_trade_history_capped_at_200(state):
    """trade_history must never exceed MAX_TRADE_HISTORY (200) entries."""
    for i in range(250):
        state.record_trade(_make_trade(symbol=f"T{i}", pnl=1.0))
    assert len(state.trade_history) == 200


def test_trade_history_keeps_most_recent(state):
    """When capped, the oldest entries are dropped (keep most recent)."""
    for i in range(210):
        state.record_trade(_make_trade(symbol=f"T{i:03d}", pnl=1.0))
    # The oldest 10 entries should be gone; last entry is T209
    symbols = [t.symbol for t in state.trade_history]
    assert "T000" not in symbols
    assert "T209" in symbols


# ---------------------------------------------------------------------------
# Computed properties
# ---------------------------------------------------------------------------

def test_current_drawdown_pct_calculation(state):
    """Drawdown = (peak - current) / peak."""
    state.peak_value = 100_000.0
    state.portfolio_value = 90_000.0
    assert state.current_drawdown_pct == pytest.approx(0.10)


def test_drawdown_zero_when_at_peak(state):
    """Drawdown is 0.0 when portfolio_value == peak_value."""
    state.peak_value = 100_000.0
    state.portfolio_value = 100_000.0
    assert state.current_drawdown_pct == 0.0


def test_drawdown_zero_when_peak_is_zero(state):
    """Drawdown is 0.0 when peak_value is 0 (no history)."""
    state.peak_value = 0.0
    state.portfolio_value = 50_000.0
    assert state.current_drawdown_pct == 0.0


def test_drawdown_never_negative(state):
    """Drawdown must never be negative (portfolio above peak is not a negative DD)."""
    state.peak_value = 100_000.0
    state.portfolio_value = 105_000.0
    assert state.current_drawdown_pct == 0.0


def test_daily_loss_pct_calculation(state):
    """Daily loss = (start - current) / start."""
    state.daily_start_value = 100_000.0
    state.portfolio_value = 95_000.0
    assert state.daily_loss_pct == pytest.approx(0.05)


def test_daily_loss_pct_zero_on_gain(state):
    """daily_loss_pct returns 0.0 when portfolio is up on the day."""
    state.daily_start_value = 100_000.0
    state.portfolio_value = 103_000.0
    assert state.daily_loss_pct == 0.0


def test_daily_loss_pct_zero_when_no_baseline(state):
    """daily_loss_pct returns 0.0 when daily_start_value is 0."""
    state.daily_start_value = 0.0
    state.portfolio_value = 50_000.0
    assert state.daily_loss_pct == 0.0


def test_position_count(state):
    """position_count reflects actual number of positions."""
    assert state.position_count == 0
    state.positions["AAPL"] = _make_position("AAPL")
    assert state.position_count == 1
    state.positions["MSFT"] = _make_position("MSFT")
    assert state.position_count == 2
def test_to_summary_dict_contains_required_keys(state):
    """to_summary_dict() must contain all required keys."""
    summary = state.to_summary_dict()
    required_keys = [
        "portfolio_value", "cash", "position_count",
        "current_drawdown_pct", "daily_loss_pct",
        "consecutive_losses", "positions",
    ]
    for key in required_keys:
        assert key in summary, f"Missing key: {key}"
def test_to_summary_dict_drawdown_rounded(state):
    """Drawdown in summary must be rounded to 4 decimal places."""
    state.peak_value = 100_000.0
    state.portfolio_value = 91_234.56
    summary = state.to_summary_dict()
    # Check it's a float rounded to 4dp
    dd = summary["current_drawdown_pct"]
    assert isinstance(dd, float)
    assert dd == round(state.current_drawdown_pct, 4)


# ---------------------------------------------------------------------------
# Integration: full cycle
# ---------------------------------------------------------------------------
