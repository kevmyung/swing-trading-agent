"""
tests/test_execution_tools.py — Unit tests for execution tool modules.

All tests mock the Alpaca TradingClient so no real credentials are needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import tools.execution.alpaca_orders as alpaca_orders_module
import tools.execution.portfolio_sync as portfolio_sync_module
from tools.execution.alpaca_orders import (
    place_bracket_order,
    cancel_order,
    get_open_orders,
)
from tools.execution.portfolio_sync import sync_positions_from_alpaca


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_order(
    order_id='order-abc-123',
    symbol='AAPL',
    qty=10,
    side_value='buy',
    type_value='market',
    tif_value='day',
    status_value='accepted',
    submitted_at=None,
):
    order = MagicMock()
    order.id = order_id
    order.symbol = symbol
    order.qty = qty
    order.side.value = side_value
    order.type.value = type_value
    order.time_in_force.value = tif_value
    order.status.value = status_value
    if submitted_at is None:
        order.submitted_at.isoformat.return_value = '2026-03-07T13:30:00+00:00'
    else:
        order.submitted_at = submitted_at
    return order


def _make_mock_open_order(order_id='o1', symbol='MSFT', qty=5, side_value='buy', status_value='new'):
    o = MagicMock()
    o.id = order_id
    o.symbol = symbol
    o.qty = qty
    o.side.value = side_value
    o.status.value = status_value
    o.submitted_at.isoformat.return_value = '2026-03-07T13:00:00+00:00'
    return o


def _mock_alpaca_available(module, available: bool):
    """Context-manager-style patcher for _ALPACA_AVAILABLE."""
    return patch.object(module, '_ALPACA_AVAILABLE', available)


# ---------------------------------------------------------------------------
# place_bracket_order — alpaca not available
# ---------------------------------------------------------------------------

def test_place_bracket_order_alpaca_not_available():
    with _mock_alpaca_available(alpaca_orders_module, False):
        result = place_bracket_order(
            symbol='AAPL', qty=10, side='buy',
            stop_loss_price=145.0, take_profit_price=160.0,
        )
    assert result['error'] is not None
    assert 'alpaca-py' in result['error']
    assert result['order_id'] is None
    assert result['symbol'] == 'AAPL'


# ---------------------------------------------------------------------------
# place_bracket_order — success
# ---------------------------------------------------------------------------

def test_place_bracket_order_success():
    mock_order = _make_mock_order()
    mock_client = MagicMock()
    mock_client.submit_order.return_value = mock_order

    with _mock_alpaca_available(alpaca_orders_module, True), \
         patch.object(alpaca_orders_module, 'OrderSide', MagicMock(BUY='BUY', SELL='SELL')), \
         patch.object(alpaca_orders_module, 'TimeInForce', MagicMock(DAY='DAY')), \
         patch.object(alpaca_orders_module, 'OrderClass', MagicMock(BRACKET='BRACKET')), \
         patch.object(alpaca_orders_module, 'MarketOrderRequest', MagicMock(return_value=MagicMock())), \
         patch.object(alpaca_orders_module, 'StopLossRequest', MagicMock(return_value=MagicMock())), \
         patch.object(alpaca_orders_module, 'TakeProfitRequest', MagicMock(return_value=MagicMock())), \
         patch('tools.execution.alpaca_orders._get_trading_client', return_value=mock_client):
        result = place_bracket_order(
            symbol='AAPL', qty=10, side='buy',
            stop_loss_price=145.0, take_profit_price=160.0,
        )

    assert result['error'] is None
    assert result['order_id'] == 'order-abc-123'
    assert result['symbol'] == 'AAPL'
    assert result['qty'] == 10
    assert result['stop_loss_price'] == 145.0
    assert result['take_profit_price'] == 160.0


def test_place_bracket_order_sell_side():
    mock_order = _make_mock_order(side_value='sell')
    mock_client = MagicMock()
    mock_client.submit_order.return_value = mock_order

    with _mock_alpaca_available(alpaca_orders_module, True), \
         patch.object(alpaca_orders_module, 'OrderSide', MagicMock(BUY='BUY', SELL='SELL')), \
         patch.object(alpaca_orders_module, 'TimeInForce', MagicMock(DAY='DAY')), \
         patch.object(alpaca_orders_module, 'OrderClass', MagicMock(BRACKET='BRACKET')), \
         patch.object(alpaca_orders_module, 'MarketOrderRequest', MagicMock(return_value=MagicMock())), \
         patch.object(alpaca_orders_module, 'StopLossRequest', MagicMock(return_value=MagicMock())), \
         patch.object(alpaca_orders_module, 'TakeProfitRequest', MagicMock(return_value=MagicMock())), \
         patch('tools.execution.alpaca_orders._get_trading_client', return_value=mock_client):
        result = place_bracket_order(
            symbol='TSLA', qty=5, side='sell', stop_loss_price=200.0,
            take_profit_price=210.0,
        )

    assert result['side'] == 'sell'


def test_place_bracket_order_no_take_profit():
    """No TakeProfitRequest created when take_profit_price is None."""
    mock_order = _make_mock_order()
    mock_client = MagicMock()
    mock_client.submit_order.return_value = mock_order
    tp_mock = MagicMock()

    with _mock_alpaca_available(alpaca_orders_module, True), \
         patch.object(alpaca_orders_module, 'OrderSide', MagicMock(BUY='BUY', SELL='SELL')), \
         patch.object(alpaca_orders_module, 'TimeInForce', MagicMock(DAY='DAY')), \
         patch.object(alpaca_orders_module, 'OrderClass', MagicMock(BRACKET='BRACKET')), \
         patch.object(alpaca_orders_module, 'MarketOrderRequest', MagicMock(return_value=MagicMock())), \
         patch.object(alpaca_orders_module, 'StopLossRequest', MagicMock(return_value=MagicMock())), \
         patch.object(alpaca_orders_module, 'TakeProfitRequest', tp_mock), \
         patch('tools.execution.alpaca_orders._get_trading_client', return_value=mock_client):
        result = place_bracket_order(
            symbol='AAPL', qty=10, side='buy', stop_loss_price=145.0,
        )

    tp_mock.assert_not_called()
    assert result['take_profit_price'] is None


def test_place_bracket_order_api_exception():
    mock_client = MagicMock()
    mock_client.submit_order.side_effect = RuntimeError('API timeout')

    with _mock_alpaca_available(alpaca_orders_module, True), \
         patch.object(alpaca_orders_module, 'OrderSide', MagicMock(BUY='BUY')), \
         patch.object(alpaca_orders_module, 'TimeInForce', MagicMock(DAY='DAY')), \
         patch.object(alpaca_orders_module, 'OrderClass', MagicMock(BRACKET='BRACKET')), \
         patch.object(alpaca_orders_module, 'MarketOrderRequest', MagicMock(return_value=MagicMock())), \
         patch.object(alpaca_orders_module, 'StopLossRequest', MagicMock(return_value=MagicMock())), \
         patch.object(alpaca_orders_module, 'TakeProfitRequest', MagicMock(return_value=MagicMock())), \
         patch('tools.execution.alpaca_orders._get_trading_client', return_value=mock_client):
        result = place_bracket_order(
            symbol='AAPL', qty=10, side='buy', stop_loss_price=145.0,
            take_profit_price=160.0,
        )

    assert result['error'] == 'API timeout'
    assert result['order_id'] is None


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

def test_cancel_order_alpaca_not_available():
    with _mock_alpaca_available(alpaca_orders_module, False):
        result = cancel_order(order_id='order-xyz')
    assert result['cancelled'] is False
    assert 'alpaca-py' in result['error']
    assert result['order_id'] == 'order-xyz'


def test_cancel_order_success():
    mock_client = MagicMock()
    mock_client.cancel_order_by_id.return_value = None

    with _mock_alpaca_available(alpaca_orders_module, True), \
         patch('tools.execution.alpaca_orders._get_trading_client', return_value=mock_client):
        result = cancel_order(order_id='order-123')

    assert result['cancelled'] is True
    assert result['error'] is None
    assert result['order_id'] == 'order-123'
    mock_client.cancel_order_by_id.assert_called_once_with('order-123')


def test_cancel_order_already_filled():
    mock_client = MagicMock()
    mock_client.cancel_order_by_id.side_effect = Exception('order already filled')

    with _mock_alpaca_available(alpaca_orders_module, True), \
         patch('tools.execution.alpaca_orders._get_trading_client', return_value=mock_client):
        result = cancel_order(order_id='order-456')

    assert result['cancelled'] is False
    assert 'order already filled' in result['error']


def test_cancel_order_not_found():
    mock_client = MagicMock()
    mock_client.cancel_order_by_id.side_effect = Exception('order not found')

    with _mock_alpaca_available(alpaca_orders_module, True), \
         patch('tools.execution.alpaca_orders._get_trading_client', return_value=mock_client):
        result = cancel_order(order_id='missing-order')

    assert result['cancelled'] is False
    assert result['order_id'] == 'missing-order'


# ---------------------------------------------------------------------------
# get_open_orders
# ---------------------------------------------------------------------------

def test_get_open_orders_alpaca_not_available():
    with _mock_alpaca_available(alpaca_orders_module, False):
        result = get_open_orders()
    assert result['open_orders'] == []
    assert result['total_count'] == 0
    assert 'alpaca-py' in result['error']


def test_get_open_orders_empty():
    mock_client = MagicMock()
    mock_client.get_orders.return_value = []

    with _mock_alpaca_available(alpaca_orders_module, True), \
         patch.object(alpaca_orders_module, 'GetOrdersRequest', MagicMock(return_value=MagicMock())), \
         patch.object(alpaca_orders_module, 'QueryOrderStatus', MagicMock(OPEN='open')), \
         patch('tools.execution.alpaca_orders._get_trading_client', return_value=mock_client):
        result = get_open_orders()

    assert result['open_orders'] == []
    assert result['total_count'] == 0
    assert result['error'] is None


def test_get_open_orders_with_orders():
    mock_orders = [
        _make_mock_open_order('o1', 'AAPL', 10, 'buy', 'new'),
        _make_mock_open_order('o2', 'MSFT', 5, 'sell', 'pending_new'),
    ]
    mock_client = MagicMock()
    mock_client.get_orders.return_value = mock_orders

    with _mock_alpaca_available(alpaca_orders_module, True), \
         patch.object(alpaca_orders_module, 'GetOrdersRequest', MagicMock(return_value=MagicMock())), \
         patch.object(alpaca_orders_module, 'QueryOrderStatus', MagicMock(OPEN='open')), \
         patch('tools.execution.alpaca_orders._get_trading_client', return_value=mock_client):
        result = get_open_orders()

    assert result['total_count'] == 2
    assert result['open_orders'][0]['order_id'] == 'o1'
    assert result['open_orders'][0]['symbol'] == 'AAPL'
    assert result['open_orders'][1]['symbol'] == 'MSFT'
    assert result['error'] is None


def test_get_open_orders_api_exception():
    mock_client = MagicMock()
    mock_client.get_orders.side_effect = ConnectionError('network error')

    with _mock_alpaca_available(alpaca_orders_module, True), \
         patch.object(alpaca_orders_module, 'GetOrdersRequest', MagicMock(return_value=MagicMock())), \
         patch.object(alpaca_orders_module, 'QueryOrderStatus', MagicMock(OPEN='open')), \
         patch('tools.execution.alpaca_orders._get_trading_client', return_value=mock_client):
        result = get_open_orders()

    assert result['error'] == 'network error'
    assert result['total_count'] == 0
def test_sync_positions_alpaca_not_available():
    with _mock_alpaca_available(portfolio_sync_module, False):
        result = sync_positions_from_alpaca()
    assert result['error'] is not None
    assert 'alpaca-py' in result['error']
    assert result['position_count'] == 0


def test_sync_positions_api_exception():
    mock_client = MagicMock()
    mock_client.get_account.side_effect = RuntimeError('API error')

    with _mock_alpaca_available(portfolio_sync_module, True), \
         patch('tools.execution.portfolio_sync._get_trading_client', return_value=mock_client):
        result = sync_positions_from_alpaca()

    assert result['error'] == 'API error'
    assert result['position_count'] == 0


def test_sync_positions_empty_portfolio():
    """Sync with no positions → zeroed state."""
    mock_account = MagicMock()
    mock_account.cash = '100000.0'
    mock_account.buying_power = '200000.0'
    mock_account.portfolio_value = '100000.0'
    mock_client = MagicMock()
    mock_client.get_account.return_value = mock_account
    mock_client.get_all_positions.return_value = []

    with _mock_alpaca_available(portfolio_sync_module, True), \
         patch('tools.execution.portfolio_sync._get_trading_client', return_value=mock_client), \
         patch('state.portfolio_state.PortfolioState') as mock_ps_class:
        mock_state = MagicMock()
        mock_state.positions = {}
        mock_state.trade_history = []
        mock_state.peak_value = 0.0
        mock_state.portfolio_value = 0.0
        mock_state.trading_day = ''
        mock_state.current_drawdown_pct = 0.0
        mock_ps_class.return_value = mock_state

        result = sync_positions_from_alpaca()

    assert result['position_count'] == 0
    assert result['cash'] == pytest.approx(100_000.0)
    assert result['error'] is None


def test_sync_positions_with_open_position():
    """Sync with one Alpaca position → position recorded in result."""
    mock_account = MagicMock()
    mock_account.cash = '80000.0'
    mock_account.buying_power = '160000.0'
    mock_account.portfolio_value = '105000.0'

    mock_pos = MagicMock()
    mock_pos.symbol = 'AAPL'
    mock_pos.qty = 50
    mock_pos.avg_entry_price = '180.0'
    mock_pos.current_price = '185.0'
    mock_pos.unrealized_pl = '250.0'
    mock_pos.market_value = '9250.0'

    mock_client = MagicMock()
    mock_client.get_account.return_value = mock_account
    mock_client.get_all_positions.return_value = [mock_pos]

    with _mock_alpaca_available(portfolio_sync_module, True), \
         patch('tools.execution.portfolio_sync._get_trading_client', return_value=mock_client), \
         patch('state.portfolio_state.PortfolioState') as mock_ps_class:
        mock_state = MagicMock()
        mock_state.positions = {}
        mock_state.trade_history = []
        mock_state.peak_value = 100_000.0
        mock_state.portfolio_value = 100_000.0
        mock_state.trading_day = ''
        mock_state.current_drawdown_pct = 0.0
        mock_ps_class.return_value = mock_state

        result = sync_positions_from_alpaca()

    assert result['position_count'] == 1
    assert result['positions'][0]['symbol'] == 'AAPL'
    assert result['error'] is None


def test_sync_positions_response_keys():
    with _mock_alpaca_available(portfolio_sync_module, False):
        result = sync_positions_from_alpaca()

    expected_keys = (
        'synced_at', 'cash', 'buying_power', 'portfolio_value', 'peak_value',
        'current_drawdown_pct', 'position_count', 'positions', 'open_orders',
        'today_rpl', 'newly_closed_positions', 'error',
    )
    for key in expected_keys:
        assert key in result
