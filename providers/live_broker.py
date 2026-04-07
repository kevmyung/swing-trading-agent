"""
providers/live_broker.py — Broker backend backed by Alpaca API.

Wraps tools/execution/alpaca_orders.py and tools/execution/portfolio_sync.py
to implement the Broker interface for live trading.
"""

from __future__ import annotations

import logging

from providers.broker import Broker
from state.portfolio_state import Position

logger = logging.getLogger(__name__)


class AlpacaBroker(Broker):
    """Live broker that executes orders via the Alpaca API.

    State is lazily synced from Alpaca; local state is populated
    on the first call to sync() and kept in sync after each order.
    """

    def __init__(self, settings) -> None:
        self._settings = settings
        self._cash: float = 0.0
        self._portfolio_value: float = 0.0
        self._peak_value: float = 0.0
        self._positions: dict[str, Position] = {}
        self._synced = False

    # ------------------------------------------------------------------
    # Broker interface: properties
    # ------------------------------------------------------------------

    @property
    def portfolio_value(self) -> float:
        return self._portfolio_value

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def positions(self) -> dict[str, Position]:
        return self._positions

    # ------------------------------------------------------------------
    # Broker interface: sync
    # ------------------------------------------------------------------

    def sync(self, sim_date: str | None = None, existing_positions=None) -> dict:
        """Sync portfolio state from Alpaca and return sync response."""
        from tools.execution.portfolio_sync import sync_positions_from_alpaca
        result = sync_positions_from_alpaca(existing_positions=existing_positions)
        if not result.get('error'):
            self._cash = result['cash']
            self._portfolio_value = result['portfolio_value']
            self._peak_value = result.get('peak_value', self._portfolio_value)
            # Rebuild local positions from full sync data
            for sym, pos_data in result.get('positions_full', {}).items():
                self._positions[sym] = Position.from_dict(pos_data)
            # Remove positions no longer in Alpaca
            synced_syms = set(result.get('positions_full', {}).keys())
            for sym in list(self._positions.keys()):
                if sym not in synced_syms:
                    del self._positions[sym]
            self._synced = True
        return result

    # ------------------------------------------------------------------
    # Broker interface: order execution
    # ------------------------------------------------------------------

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
        """Place a bracket order via Alpaca.

        Uses 'gtc' TIF for bracket orders so stop-loss legs persist
        across trading sessions (swing trading requires overnight protection).
        """
        from tools.execution.alpaca_orders import place_bracket_order
        order_type = entry_type.lower()
        result = place_bracket_order(
            symbol=ticker,
            qty=shares,
            side='buy',
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            order_type=order_type,
            limit_price=limit_price,
            time_in_force='gtc',
        )
        return result

    def execute_exit(
        self,
        ticker: str,
        qty: int | None = None,
        exit_pct: float = 1.0,
        sim_date: str | None = None,
        bars=None,
        **kwargs,
    ) -> dict | None:
        """Place a market sell order via Alpaca.

        Also cancels the bracket order (stop+TP legs) before placing
        the exit to avoid conflicting orders.
        """
        from tools.execution.alpaca_orders import place_market_order, cancel_open_orders_for_symbol
        pos = self._positions.get(ticker)
        if pos is None and qty is None:
            logger.warning("AlpacaBroker.execute_exit: no position for %s", ticker)
            return None
        sell_qty = qty
        if sell_qty is None:
            sell_qty = max(1, int(pos.qty * exit_pct))

        # Cancel ALL open orders for this symbol to release held shares
        cancel_result = cancel_open_orders_for_symbol(ticker)
        if cancel_result['cancelled_count'] > 0:
            logger.info("AlpacaBroker.execute_exit: cancelled %d open orders for %s",
                        cancel_result['cancelled_count'], ticker)

        result = place_market_order(
            symbol=ticker, qty=sell_qty, side='sell', time_in_force='day',
        )
        return result

    def update_stop(
        self,
        ticker: str,
        new_stop: float,
        bracket_order_id: str | None = None,
    ) -> dict:
        """Modify stop-loss on Alpaca bracket order.

        Updates both Alpaca and local state. If Alpaca modification fails
        but the error indicates the bracket is gone (filled/cancelled), we
        place a standalone stop order as fallback.
        """
        from tools.execution.alpaca_orders import modify_bracket_stop
        pos = self._positions.get(ticker)
        if not pos:
            return {'modified': False, 'error': f'No position for {ticker}'}

        old_stop = pos.stop_loss_price

        bid = bracket_order_id or pos.bracket_order_id
        if not bid:
            # No bracket order — update local state only
            pos.stop_loss_price = float(new_stop)
            logger.info(
                "AlpacaBroker.update_stop: %s local state updated %.2f → %.2f (no bracket_id)",
                ticker, old_stop, new_stop,
            )
            return {'modified': True, 'ticker': ticker, 'old_stop': old_stop, 'new_stop': new_stop,
                    'alpaca_updated': False}

        result = modify_bracket_stop(
            parent_order_id=bid, new_stop_price=float(new_stop),
        )
        if result.get('modified'):
            # Alpaca updated successfully — update local state to match
            pos.stop_loss_price = float(new_stop)
        else:
            # Alpaca update failed — still update local state but log warning
            pos.stop_loss_price = float(new_stop)
            logger.warning(
                "AlpacaBroker.update_stop: %s Alpaca update FAILED — %s "
                "(local state updated %.2f → %.2f, may diverge from broker)",
                ticker, result.get('error'), old_stop, new_stop,
            )
        result['ticker'] = ticker
        result['old_stop'] = old_stop
        result['new_stop'] = new_stop
        return result
