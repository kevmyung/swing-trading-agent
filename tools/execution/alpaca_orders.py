"""
tools/execution/alpaca_orders.py — Alpaca order management tools for the ExecutionAgent.

Wraps the alpaca-py SDK for order placement, cancellation, and status queries.
All order placement goes through bracket orders to ensure stop-loss is always set.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Alpaca import with graceful fallback
# ---------------------------------------------------------------------------

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest,
        LimitOrderRequest,
        StopLimitOrderRequest,
        GetOrdersRequest,
        TakeProfitRequest,
        StopLossRequest,
        ReplaceOrderRequest,
    )
    from alpaca.trading.enums import (
        OrderSide,
        TimeInForce,
        OrderClass,
        QueryOrderStatus,
        OrderType,
    )
    _ALPACA_AVAILABLE = True
except ImportError:
    _ALPACA_AVAILABLE = False
    TradingClient = None        # type: ignore[assignment,misc]
    MarketOrderRequest = None   # type: ignore[assignment]
    LimitOrderRequest = None    # type: ignore[assignment]
    StopLimitOrderRequest = None  # type: ignore[assignment]
    GetOrdersRequest = None     # type: ignore[assignment]
    TakeProfitRequest = None    # type: ignore[assignment]
    StopLossRequest = None      # type: ignore[assignment]
    ReplaceOrderRequest = None  # type: ignore[assignment]
    OrderSide = None            # type: ignore[assignment]
    TimeInForce = None          # type: ignore[assignment]
    OrderClass = None           # type: ignore[assignment]
    QueryOrderStatus = None     # type: ignore[assignment]
    OrderType = None            # type: ignore[assignment]


def _get_trading_client():
    """Return a TradingClient instance using credentials from settings."""
    from config.settings import get_settings
    s = get_settings()
    return TradingClient(
        api_key=s.alpaca_api_key,
        secret_key=s.alpaca_secret_key,
        paper=s.alpaca_paper,
    )


def place_bracket_order(
    symbol: str,
    qty: int,
    side: str,
    stop_loss_price: float,
    take_profit_price: float | None = None,
    order_type: str = "market",
    limit_price: float | None = None,
    time_in_force: str = "gtc",
) -> dict:
    """Place a bracket order via the Alpaca API with automatic stop-loss attachment.

    A bracket order simultaneously creates: (1) the entry order, (2) a stop-loss
    order, and (3) optionally a take-profit order. The stop-loss and take-profit
    are OCO (One-Cancels-Other) — when one fills, the other is cancelled.

    Args:
        symbol: Alpaca-recognised ticker symbol (e.g. 'AAPL').
        qty: Number of shares to trade. Must be > 0 and match position-sizing output.
        side: Trade direction — ``'buy'`` or ``'sell'``.
        stop_loss_price: Stop price in USD. For longs: entry - (ATR × 2).
        take_profit_price: Take-profit limit price in USD. For longs: entry + (ATR × 3).
        order_type: ``'market'``, ``'limit'``, or ``'stop_limit'``.
        limit_price: Required when order_type is ``'limit'`` or ``'stop_limit'``.
                     For limit: max price willing to pay (buy) / min price (sell).
                     For stop_limit: limit price after stop trigger.
        time_in_force: ``'gtc'`` (good-till-cancelled, default for swing trading),
                       ``'day'`` (expires at close), or ``'opg'`` (market-on-open).

    Returns:
        Dict with order details or ``error`` key if submission failed.
    """
    if not _ALPACA_AVAILABLE:
        return {
            'order_id': None,
            'symbol': symbol,
            'qty': qty,
            'side': side,
            'type': order_type,
            'time_in_force': time_in_force,
            'status': None,
            'stop_loss_price': stop_loss_price,
            'take_profit_price': take_profit_price,
            'submitted_at': None,
            'paper_trading': True,
            'error': 'alpaca-py not installed. Run: pip install alpaca-py',
        }

    try:
        from config.settings import get_settings
        settings = get_settings()
        client = _get_trading_client()

        order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL
        tp = (
            TakeProfitRequest(limit_price=round(take_profit_price, 2))
            if take_profit_price is not None
            else None
        )
        sl = StopLossRequest(stop_price=round(stop_loss_price, 2))

        # Map time_in_force string to Alpaca enum
        tif_map = {'day': TimeInForce.DAY, 'opg': TimeInForce.OPG, 'gtc': TimeInForce.GTC}
        tif = tif_map.get(time_in_force.lower(), TimeInForce.DAY)

        ot = order_type.lower()
        if ot == 'limit' and limit_price is not None:
            request = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                limit_price=round(limit_price, 2),
                time_in_force=tif,
                order_class=OrderClass.BRACKET,
                stop_loss=sl,
                take_profit=tp,
            )
        elif ot == 'stop_limit' and limit_price is not None:
            # stop_limit: triggers at stop_price, fills at limit_price
            # For bracket buy: stop_price = breakout level, limit_price = max fill price
            request = StopLimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                stop_price=round(limit_price, 2),  # trigger price
                limit_price=round(limit_price * 1.005, 2),  # small buffer above trigger
                time_in_force=tif,
                order_class=OrderClass.BRACKET,
                stop_loss=sl,
                take_profit=tp,
            )
        else:
            # Default: market order
            request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=order_side,
                time_in_force=tif,
                order_class=OrderClass.BRACKET,
                stop_loss=sl,
                take_profit=tp,
            )

        order = client.submit_order(request)
        return {
            'order_id': str(order.id),
            'symbol': order.symbol,
            'qty': int(order.qty),
            'side': order.side.value,
            'type': order.type.value,
            'time_in_force': order.time_in_force.value,
            'status': order.status.value,
            'stop_loss_price': stop_loss_price,
            'take_profit_price': take_profit_price,
            'submitted_at': order.submitted_at.isoformat() if order.submitted_at else None,
            'paper_trading': settings.alpaca_paper,
            'error': None,
        }
    except Exception as exc:
        logger.error("Failed to place bracket order for %s: %s", symbol, exc)
        return {
            'order_id': None,
            'symbol': symbol,
            'qty': qty,
            'side': side,
            'type': order_type,
            'time_in_force': time_in_force,
            'status': None,
            'stop_loss_price': stop_loss_price,
            'take_profit_price': take_profit_price,
            'submitted_at': None,
            'paper_trading': True,
            'error': str(exc),
        }


def place_market_order(
    symbol: str,
    qty: int,
    side: str,
    time_in_force: str = "day",
) -> dict:
    """Place a plain market order without bracket (used for position exits).

    Unlike ``place_bracket_order``, this submits a simple market order with
    no attached stop-loss or take-profit legs. Use this to close an existing
    long position at the market open after an EOD exit signal.

    Args:
        symbol: Alpaca-recognised ticker symbol (e.g. ``'AAPL'``).
        qty: Number of shares.  Must be > 0.
        side: ``'buy'`` or ``'sell'``.
        time_in_force: ``'day'`` (default) or ``'opg'`` (market-on-open).

    Returns:
        Dict with keys: ``order_id``, ``symbol``, ``qty``, ``side``, ``status``,
        ``submitted_at``, ``paper_trading``, ``error``.
    """
    if not _ALPACA_AVAILABLE:
        return {
            'order_id': None, 'symbol': symbol, 'qty': qty, 'side': side,
            'status': None, 'submitted_at': None, 'paper_trading': True,
            'error': 'alpaca-py not installed. Run: pip install alpaca-py',
        }

    try:
        from config.settings import get_settings
        settings = get_settings()
        client = _get_trading_client()

        order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL
        tif = TimeInForce.OPG if time_in_force.lower() == 'opg' else TimeInForce.DAY
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=order_side,
            time_in_force=tif,
        )
        order = client.submit_order(request)
        return {
            'order_id': str(order.id),
            'symbol': order.symbol,
            'qty': int(order.qty),
            'side': order.side.value,
            'status': order.status.value,
            'submitted_at': order.submitted_at.isoformat() if order.submitted_at else None,
            'paper_trading': settings.alpaca_paper,
            'error': None,
        }
    except Exception as exc:
        logger.error("Failed to place market order for %s: %s", symbol, exc)
        return {
            'order_id': None, 'symbol': symbol, 'qty': qty, 'side': side,
            'status': None, 'submitted_at': None, 'paper_trading': True,
            'error': str(exc),
        }


def modify_bracket_stop(parent_order_id: str, new_stop_price: float) -> dict:
    """Modify the stop-loss leg of an existing bracket order on Alpaca.

    Bracket orders have a parent order with two child legs (stop-loss and
    take-profit). This function finds the stop-loss child order and replaces
    its stop price via the Alpaca Replace Order API.

    Args:
        parent_order_id: The Alpaca order UUID of the parent bracket order
                         (stored in Position.bracket_order_id).
        new_stop_price: New stop price in USD. Must be higher than the current
                        stop (only tightening is supported).

    Returns:
        Dict with keys:
          - ``parent_order_id`` (str)
          - ``stop_order_id`` (str | None): Child stop-loss order UUID
          - ``new_stop_price`` (float)
          - ``modified`` (bool): True if the modification succeeded
          - ``error`` (str | None)
    """
    if not _ALPACA_AVAILABLE:
        return {
            'parent_order_id': parent_order_id,
            'stop_order_id': None,
            'new_stop_price': new_stop_price,
            'modified': False,
            'error': 'alpaca-py not installed. Run: pip install alpaca-py',
        }

    try:
        client = _get_trading_client()

        # Fetch the parent order to locate child legs
        parent = client.get_order_by_id(parent_order_id)
        legs = getattr(parent, 'legs', None) or []

        # Find the stop-loss leg (type == 'stop')
        stop_order = None
        for leg in legs:
            leg_type = getattr(leg, 'type', None)
            if leg_type and str(leg_type).lower() in ('stop', 'stop_limit'):
                stop_order = leg
                break

        if stop_order is None:
            return {
                'parent_order_id': parent_order_id,
                'stop_order_id': None,
                'new_stop_price': new_stop_price,
                'modified': False,
                'error': 'No stop-loss leg found on bracket order',
            }

        stop_order_id = str(stop_order.id)

        # Check stop leg status — can only replace open/held orders
        stop_status = str(getattr(stop_order, 'status', '')).lower()
        if stop_status in ('filled', 'cancelled', 'expired', 'replaced'):
            return {
                'parent_order_id': parent_order_id,
                'stop_order_id': stop_order_id,
                'new_stop_price': new_stop_price,
                'modified': False,
                'error': f'Stop leg already {stop_status} — cannot modify',
            }

        replace_request = ReplaceOrderRequest(stop_price=round(new_stop_price, 2))
        client.replace_order_by_id(stop_order_id, replace_request)

        return {
            'parent_order_id': parent_order_id,
            'stop_order_id': stop_order_id,
            'new_stop_price': new_stop_price,
            'modified': True,
            'error': None,
        }
    except Exception as exc:
        logger.error(
            "Failed to modify bracket stop for order %s: %s", parent_order_id, exc
        )
        return {
            'parent_order_id': parent_order_id,
            'stop_order_id': None,
            'new_stop_price': new_stop_price,
            'modified': False,
            'error': str(exc),
        }


def cancel_order(order_id: str) -> dict:
    """Cancel a pending order by its Alpaca order ID.

    Used by the circuit-breaker when trading is halted to cancel all open
    orders and prevent stale orders from filling.

    Args:
        order_id: Alpaca order UUID (from place_bracket_order output or get_open_orders).

    Returns:
        Dict with keys:
          - ``order_id`` (str)
          - ``cancelled`` (bool): True if cancellation succeeded
          - ``error`` (str | None): Error message if cancellation failed
    """
    if not _ALPACA_AVAILABLE:
        return {
            'order_id': order_id,
            'cancelled': False,
            'error': 'alpaca-py not installed. Run: pip install alpaca-py',
        }

    try:
        client = _get_trading_client()
        client.cancel_order_by_id(order_id)
        return {'order_id': order_id, 'cancelled': True, 'error': None}
    except Exception as exc:
        logger.error("Failed to cancel order %s: %s", order_id, exc)
        return {'order_id': order_id, 'cancelled': False, 'error': str(exc)}


def get_open_orders() -> dict:
    """Retrieve all open and pending orders from Alpaca.

    Used at the start of each trading cycle to check pending fills and
    identify any stale orders that may need cancellation.

    Returns:
        Dict with keys:
          - ``open_orders`` (list): List of order dicts with order_id, symbol,
                                    qty, side, status, submitted_at
          - ``total_count`` (int): Total number of open orders
          - ``fetched_at`` (str): ISO timestamp of the API call
          - ``error`` (str | None)
    """
    if not _ALPACA_AVAILABLE:
        return {
            'open_orders': [],
            'total_count': 0,
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'error': 'alpaca-py not installed. Run: pip install alpaca-py',
        }

    try:
        client = _get_trading_client()
        orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
        fetched_at = datetime.now(timezone.utc).isoformat()

        open_orders = [
            {
                'order_id': str(o.id),
                'symbol': o.symbol,
                'qty': int(o.qty),
                'side': o.side.value,
                'status': o.status.value,
                'submitted_at': o.submitted_at.isoformat() if o.submitted_at else None,
            }
            for o in orders
        ]

        return {
            'open_orders': open_orders,
            'total_count': len(open_orders),
            'fetched_at': fetched_at,
            'error': None,
        }
    except Exception as exc:
        logger.error("Failed to get open orders: %s", exc)
        return {
            'open_orders': [],
            'total_count': 0,
            'fetched_at': datetime.now(timezone.utc).isoformat(),
            'error': str(exc),
        }
