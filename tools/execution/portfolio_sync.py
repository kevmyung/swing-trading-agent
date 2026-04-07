"""
tools/execution/portfolio_sync.py — Portfolio synchronisation tool for the ExecutionAgent.

Fetches all current positions, cash balance, and buying power from Alpaca and
reconciles with the local PortfolioState object.
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
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus, OrderClass, OrderSide, TimeInForce
    _ALPACA_AVAILABLE = True
except ImportError:
    _ALPACA_AVAILABLE = False
    TradingClient = None          # type: ignore[assignment,misc]
    GetOrdersRequest = None       # type: ignore[assignment]
    QueryOrderStatus = None       # type: ignore[assignment]
    OrderClass = None             # type: ignore[assignment]


def _get_trading_client():
    """Return a TradingClient instance using credentials from settings."""
    from config.settings import get_settings
    s = get_settings()
    return TradingClient(
        api_key=s.alpaca_api_key,
        secret_key=s.alpaca_secret_key,
        paper=s.alpaca_paper,
    )


def _error_response(msg: str) -> dict:
    """Return a zeroed-out sync response with an error message."""
    return {
        'synced_at': datetime.now(timezone.utc).isoformat(),
        'cash': 0.0,
        'buying_power': 0.0,
        'portfolio_value': 0.0,
        'peak_value': 0.0,
        'current_drawdown_pct': 0.0,
        'position_count': 0,
        'positions': [],
        'open_orders': [],
        'today_rpl': 0.0,
        'newly_closed_positions': [],
        'error': msg,
    }


def sync_positions_from_alpaca(existing_positions=None) -> dict:
    """Sync all open positions, cash, and account metrics from Alpaca to local state.

    Fetches the live account state from Alpaca's REST API and reconciles it with
    the local PortfolioState. Detects positions that were closed since the last
    sync (stop-losses hit, manual exits, etc.) and records them as trades.

    Args:
        existing_positions: Optional dict of {symbol: Position} from caller
            (e.g. agent state loaded from S3). When provided, used instead of
            loading from local file — ensures metadata (strategy, entry_date,
            stop_loss) is preserved in serverless environments.

    This function must be called at the START of every trading cycle and after
    any order is placed. Do not make decisions on stale local state.

    Returns:
        Dict with keys:
          - ``synced_at`` (str): ISO timestamp of API call
          - ``cash`` (float): Available cash balance
          - ``buying_power`` (float): Total buying power (cash × margin factor)
          - ``portfolio_value`` (float): Total equity value
          - ``peak_value`` (float): Updated peak value (if new high reached)
          - ``current_drawdown_pct`` (float)
          - ``position_count`` (int): Number of open positions
          - ``positions`` (list): Per-position dicts with symbol, qty, avg_entry_price,
                                  current_price, unrealized_pnl, market_value
          - ``open_orders`` (list): Any pending orders
          - ``today_rpl`` (float): Today's total realised P&L
          - ``newly_closed_positions`` (list): Positions closed since last sync
          - ``error`` (str | None): Present if Alpaca API call failed
    """
    if not _ALPACA_AVAILABLE:
        return _error_response('alpaca-py not installed. Run: pip install alpaca-py')

    try:
        from config.settings import get_settings
        from state.portfolio_state import PortfolioState, Position, Trade

        settings = get_settings()
        client = _get_trading_client()

        # --- Fetch live account data from Alpaca ---
        account = client.get_account()
        cash = float(account.cash)
        buying_power = float(account.buying_power)
        portfolio_value = float(account.portfolio_value)

        alpaca_positions = client.get_all_positions()
        alpaca_symbols = {p.symbol for p in alpaca_positions}

        # --- Load portfolio state ---
        portfolio = PortfolioState(state_file=settings.state_file_path)
        portfolio.load()
        # In cloud/serverless, local file may be empty. If caller provided
        # positions (from S3 agent state), inject them so metadata is preserved.
        if existing_positions:
            for sym, pos in existing_positions.items():
                if sym not in portfolio.positions:
                    portfolio.positions[sym] = pos

        today_str = datetime.now(timezone.utc).date().isoformat()

        # --- Detect closed positions (in local state but not in Alpaca) ---
        newly_closed = []
        for symbol in list(portfolio.positions.keys()):
            if symbol not in alpaca_symbols:
                local_pos = portfolio.positions[symbol]
                exit_price = (
                    local_pos.current_price
                    if local_pos.current_price > 0
                    else local_pos.avg_entry_price
                )
                realized_pnl = (exit_price - local_pos.avg_entry_price) * local_pos.qty
                holding_days = 0
                if local_pos.entry_date:
                    try:
                        entry_dt = datetime.strptime(
                            local_pos.entry_date[:10], '%Y-%m-%d'
                        ).date()
                        holding_days = (datetime.now(timezone.utc).date() - entry_dt).days
                    except (ValueError, TypeError):
                        pass
                trade = Trade(
                    symbol=symbol,
                    side='sell',
                    qty=local_pos.qty,
                    price=exit_price,
                    pnl=realized_pnl,
                    timestamp=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                    strategy=local_pos.strategy or 'STOP_LOSS',
                    entry_price=local_pos.avg_entry_price,
                    holding_days=holding_days,
                )
                portfolio.record_trade(trade)
                del portfolio.positions[symbol]
                newly_closed.append({
                    'symbol': symbol,
                    'qty': local_pos.qty,
                    'avg_entry_price': local_pos.avg_entry_price,
                    'exit_price': exit_price,
                    'realized_pnl': round(realized_pnl, 2),
                    'entry_date': local_pos.entry_date or '',
                    'strategy': local_pos.strategy or 'STOP_LOSS',
                    'holding_days': holding_days,
                })

        # --- Build bracket order ID + stop price map for new positions ---
        # For symbols not yet in local state, look up the most recent filled
        # bracket order to populate bracket_order_id and stop_loss_price.
        new_symbols = {ap.symbol for ap in alpaca_positions
                       if ap.symbol not in portfolio.positions}
        bracket_order_map: dict[str, str] = {}
        bracket_stop_map: dict[str, float] = {}
        bracket_entry_date_map: dict[str, str] = {}
        if new_symbols and _ALPACA_AVAILABLE:
            try:
                filled_orders = client.get_orders(
                    filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED)
                )
                for order in filled_orders:
                    sym = getattr(order, 'symbol', None)
                    order_class = getattr(order, 'order_class', None)
                    if sym in new_symbols and str(order_class).lower() == 'bracket':
                        if sym not in bracket_order_map:
                            bracket_order_map[sym] = str(order.id)
                            # Extract entry date from filled_at
                            filled_at = getattr(order, 'filled_at', None)
                            if filled_at:
                                bracket_entry_date_map[sym] = filled_at.strftime('%Y-%m-%d') if hasattr(filled_at, 'strftime') else str(filled_at)[:10]
                            # Extract stop price from bracket legs
                            legs = getattr(order, 'legs', None) or []
                            for leg in legs:
                                leg_type = getattr(leg, 'type', None)
                                if leg_type and str(leg_type).lower() in ('stop', 'stop_limit'):
                                    stop_price = getattr(leg, 'stop_price', None)
                                    if stop_price is not None:
                                        bracket_stop_map[sym] = float(stop_price)
                                    break
            except Exception as exc:
                logger.debug("Could not fetch bracket order IDs: %s", exc)

        # --- Reconcile Alpaca positions into local state ---
        position_dicts = []
        for ap in alpaca_positions:
            current_price = float(ap.current_price) if ap.current_price else 0.0
            avg_entry = float(ap.avg_entry_price) if ap.avg_entry_price else 0.0
            qty = int(ap.qty)
            unrealized_pl = float(ap.unrealized_pl) if ap.unrealized_pl else 0.0
            market_value = float(ap.market_value) if ap.market_value else 0.0

            if ap.symbol in portfolio.positions:
                local = portfolio.positions[ap.symbol]
                local.current_price = current_price
                local.unrealized_pnl = unrealized_pl
                local.qty = qty
            else:
                stop_price = bracket_stop_map.get(ap.symbol, 0.0)
                entry_date = bracket_entry_date_map.get(ap.symbol, '')
                portfolio.positions[ap.symbol] = Position(
                    symbol=ap.symbol,
                    qty=qty,
                    avg_entry_price=avg_entry,
                    current_price=current_price,
                    stop_loss_price=stop_price,
                    unrealized_pnl=unrealized_pl,
                    bracket_order_id=bracket_order_map.get(ap.symbol, ''),
                    highest_close=current_price,
                    entry_date=entry_date,
                )
                if stop_price > 0:
                    logger.info(
                        "portfolio_sync: new position %s — stop_loss=%.2f from bracket order",
                        ap.symbol, stop_price,
                    )

            position_dicts.append({
                'symbol': ap.symbol,
                'qty': qty,
                'avg_entry_price': avg_entry,
                'current_price': current_price,
                'unrealized_pnl': unrealized_pl,
                'market_value': market_value,
            })

        # --- Ensure stop orders exist on Alpaca for all positions ---
        # If state has a stop_loss_price but no open stop order on Alpaca,
        # place a standalone GTC stop order. This covers cases where bracket
        # orders expired (day TIF) or container restarts lost order state.
        try:
            open_orders = client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
            )
            symbols_with_stop = set()
            for o in open_orders:
                otype = str(getattr(o, 'type', '')).lower()
                if otype in ('stop', 'stop_limit'):
                    symbols_with_stop.add(o.symbol)

            for sym, pos in portfolio.positions.items():
                if pos.stop_loss_price > 0 and sym not in symbols_with_stop and sym in alpaca_symbols:
                    try:
                        from alpaca.trading.requests import StopOrderRequest
                        stop_req = StopOrderRequest(
                            symbol=sym,
                            qty=pos.qty,
                            side=OrderSide.SELL,
                            stop_price=round(pos.stop_loss_price, 2),
                            time_in_force=TimeInForce.GTC,
                        )
                        stop_order = client.submit_order(stop_req)
                        logger.info(
                            "portfolio_sync: placed missing GTC stop for %s @ %.2f (order=%s)",
                            sym, pos.stop_loss_price, stop_order.id,
                        )
                    except Exception as stop_exc:
                        logger.warning("portfolio_sync: failed to place stop for %s: %s", sym, stop_exc)
        except Exception as exc:
            logger.warning("portfolio_sync: stop order sync failed: %s", exc)

        # --- Update portfolio-level metrics ---
        portfolio.cash = cash
        portfolio.portfolio_value = portfolio_value
        if portfolio_value > portfolio.peak_value:
            portfolio.peak_value = portfolio_value

        # Update daily_start_value on a new trading day
        if portfolio.trading_day != today_str:
            portfolio.daily_start_value = portfolio_value
            portfolio.trading_day = today_str

        synced_at = datetime.now(timezone.utc).isoformat()
        portfolio.last_synced = synced_at
        portfolio.save()

        today_rpl = sum(
            t.pnl
            for t in portfolio.trade_history
            if t.timestamp.startswith(today_str)
        )

        # Full Position objects (for propagating to agent_state)
        from dataclasses import asdict
        positions_full = {sym: asdict(pos) for sym, pos in portfolio.positions.items()}
        trade_history_full = [asdict(t) for t in portfolio.trade_history]

        return {
            'synced_at': synced_at,
            'cash': cash,
            'buying_power': buying_power,
            'portfolio_value': portfolio_value,
            'peak_value': portfolio.peak_value,
            'current_drawdown_pct': round(portfolio.current_drawdown_pct, 4),
            'position_count': len(alpaca_positions),
            'positions': position_dicts,
            'positions_full': positions_full,
            'trade_history': trade_history_full,
            'open_orders': [],
            'today_rpl': round(today_rpl, 2),
            'newly_closed_positions': newly_closed,
            'error': None,
        }

    except Exception as exc:
        logger.error("Failed to sync positions from Alpaca: %s", exc)
        return _error_response(str(exc))


