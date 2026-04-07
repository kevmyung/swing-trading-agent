"""
providers/broker.py — Abstract base class for broker backends.

MockBroker (backtest) and AlpacaBroker (live) implement this interface,
making PortfolioAgent cycle methods independent of execution venue.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

import pandas as pd

from state.portfolio_state import Position


class Broker(ABC):
    """Abstract interface for order execution and portfolio state."""

    # ------------------------------------------------------------------
    # Portfolio state (read-only properties)
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def portfolio_value(self) -> float:
        """Total portfolio value (cash + positions)."""

    @property
    @abstractmethod
    def cash(self) -> float:
        """Available cash balance."""

    @property
    @abstractmethod
    def positions(self) -> dict[str, Position]:
        """Open positions keyed by ticker."""

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    @abstractmethod
    def sync(self, sim_date: str | None = None, existing_positions=None) -> dict:
        """Fetch current portfolio state and return a sync response dict.

        Returns:
            dict with keys: synced_at, cash, buying_power, portfolio_value,
            peak_value, current_drawdown_pct, position_count, positions,
            open_orders, today_rpl, newly_closed_positions, error
        """

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    @abstractmethod
    def submit_entry(
        self,
        ticker: str,
        shares: int,
        stop_loss: float,
        take_profit: float,
        strategy: str,
        signal_price: float,
        entry_type: str = "MARKET",
        limit_price: float | None = None,
        atr: float = 0.0,
    ) -> dict:
        """Submit an entry order.

        entry_type: 'MARKET' (fill at open), 'LIMIT' (fill if price dips to
        limit_price), or 'STOP' (fill if price rises to limit_price).

        Returns:
            dict with execution result or pending order details.
        """

    @abstractmethod
    def execute_exit(
        self,
        ticker: str,
        qty: int | None = None,
        exit_pct: float = 1.0,
        sim_date: str | None = None,
        **kwargs,
    ) -> dict | None:
        """Execute an exit order (full or partial).

        Returns:
            dict with exit result or None if position not found.
        """

    @abstractmethod
    def update_stop(
        self,
        ticker: str,
        new_stop: float,
        bracket_order_id: str | None = None,
    ) -> dict:
        """Tighten stop-loss for an existing position.

        Returns:
            dict with modified=True/False and error if any.
        """

    # ------------------------------------------------------------------
    # Simulation-only methods (no-op in live broker)
    # ------------------------------------------------------------------

    def set_sim_context(
        self,
        sim_date: str,
        bars: dict[str, pd.DataFrame] | None = None,
        hourly_bars: dict[str, pd.DataFrame] | None = None,
    ) -> None:
        """Store simulation context for the current day (simulation only).

        Called by backtest orchestrators before each cycle so that
        fill_pending() and execute_exit() can use the correct bar data
        without explicit parameters.
        """

    def advance_day(
        self,
        sim_date: str,
        bars: dict[str, pd.DataFrame],
        hourly_bars: dict[str, pd.DataFrame] | None = None,
    ) -> list[dict]:
        """Update prices and trigger stop-loss hits (simulation only).

        Returns list of stop-out events.
        """
        return []

    def fill_pending(
        self,
        sim_date: str | None = None,
        bars: dict[str, pd.DataFrame] | None = None,
        hourly_bars: dict[str, pd.DataFrame] | None = None,
        cutoff_utc: str | None = None,
    ) -> list[dict]:
        """Fill pending entry orders (simulation only).

        MARKET orders fill at open price.
        LIMIT orders fill if hourly low ≤ limit_price up to cutoff_utc.

        When called without arguments, uses context from set_sim_context().

        Returns list of fill/reject events.
        """
        return []
