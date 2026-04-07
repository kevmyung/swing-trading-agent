"""
providers/mock_broker.py — Simulation broker backed by fixture bar data.

Extracted from backtest/common.py. Implements the Broker ABC so that
PortfolioAgent cycles can be run without any live API calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from providers.broker import Broker
from state.portfolio_state import Position

logger = logging.getLogger(__name__)


@dataclass
class PendingOrder:
    ticker: str
    shares: int
    stop_loss: float
    take_profit: float
    strategy: str
    signal_price: float
    atr: float = 0.0
    entry_type: str = 'MARKET'      # MARKET or LIMIT
    limit_price: float | None = None
    time_in_force: str = 'opg'       # opg (market-on-open) or day (limit, expires EOD)


class MockBroker(Broker):
    """Simulates Alpaca trading state using fixture bar data."""

    def __init__(
        self,
        initial_cash: float,
        slippage_bps: float = 5.0,
        slippage_impact_coeff: float = 0.1,
        min_entry_rr_ratio: float = 1.5,
        atr_stop_multiplier: float = 2.0,
    ) -> None:
        self._cash = initial_cash
        self.initial_cash = initial_cash
        self.peak_value = initial_cash
        self._positions: dict[str, Position] = {}
        self.pending_orders: list[PendingOrder] = []
        self.fill_log: list[dict] = []
        # Slippage: base spread (bps) + volume-aware market impact
        self.slippage_bps = slippage_bps
        self.slippage_impact_coeff = slippage_impact_coeff
        # MORNING R:R recalculation parameters
        self.min_entry_rr_ratio = min_entry_rr_ratio
        self.atr_stop_multiplier = atr_stop_multiplier
        # Simulation context (set by orchestrator before each cycle)
        self._sim_date: str | None = None
        self._sim_bars: dict[str, pd.DataFrame] | None = None
        self._sim_hourly: dict[str, pd.DataFrame] | None = None

    # ------------------------------------------------------------------
    # Broker interface: properties
    # ------------------------------------------------------------------

    @property
    def portfolio_value(self) -> float:
        return self._cash + sum(p.current_price * p.qty for p in self._positions.values())

    @property
    def cash(self) -> float:
        return self._cash

    @cash.setter
    def cash(self, value: float) -> None:
        self._cash = value

    @property
    def positions(self) -> dict[str, Position]:
        return self._positions

    # ------------------------------------------------------------------
    # Broker interface: sync
    # ------------------------------------------------------------------

    def sync(self, sim_date: str | None = None, existing_positions=None) -> dict:
        """Return mock sync response matching the Alpaca sync schema."""
        date_str = sim_date or "unknown"
        position_dicts = [
            {
                'symbol': t, 'qty': p.qty,
                'avg_entry_price': p.avg_entry_price,
                'current_price': p.current_price,
                'unrealized_pnl': round(p.unrealized_pnl, 2),
                'market_value': round(p.current_price * p.qty, 2),
            }
            for t, p in self._positions.items()
        ]
        pv = self.portfolio_value
        return {
            'synced_at': f'{date_str}T16:30:00Z',
            'cash': self._cash, 'buying_power': self._cash,
            'portfolio_value': pv, 'peak_value': self.peak_value,
            'current_drawdown_pct': round(
                (self.peak_value - pv) / self.peak_value if self.peak_value > 0 else 0.0, 4
            ),
            'position_count': len(self._positions),
            'positions': position_dicts,
            'open_orders': [
                {'ticker': o.ticker, 'shares': o.shares, 'order_type': o.entry_type,
                 'limit_price': o.limit_price, 'time_in_force': o.time_in_force,
                 'stop_loss': o.stop_loss}
                for o in self.pending_orders
            ],
            'today_rpl': 0.0, 'newly_closed_positions': [], 'error': None,
        }

    # ------------------------------------------------------------------
    # Simulation context
    # ------------------------------------------------------------------

    def set_sim_context(
        self,
        sim_date: str,
        bars: dict[str, pd.DataFrame] | None = None,
        hourly_bars: dict[str, pd.DataFrame] | None = None,
    ) -> None:
        self._sim_date = sim_date
        if bars is not None:
            self._sim_bars = bars
        if hourly_bars is not None:
            self._sim_hourly = hourly_bars

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
        """Queue a pending entry order.

        entry_type='MARKET' → fill at next open (time_in_force='opg')
        entry_type='LIMIT'  → fill at limit_price if day low reaches it (time_in_force='day')
        entry_type='STOP'   → fill at limit_price (trigger) if day high reaches it (time_in_force='day')
        """
        etype = entry_type.upper() if entry_type else 'MARKET'
        if etype in ('LIMIT', 'STOP') and limit_price:
            tif = 'day'
        else:
            tif = 'opg'
            etype = 'MARKET'  # fallback if LIMIT/STOP without price
        self.pending_orders.append(PendingOrder(
            ticker=ticker, shares=shares, stop_loss=stop_loss,
            take_profit=take_profit, strategy=strategy, signal_price=signal_price,
            atr=atr, entry_type=etype, limit_price=limit_price, time_in_force=tif,
        ))
        return {'ticker': ticker, 'shares': shares, 'status': 'pending',
                'order_type': etype, 'limit_price': limit_price,
                'stop_loss': stop_loss, 'take_profit': take_profit}

    def execute_exit(
        self,
        ticker: str,
        qty: int | None = None,
        exit_pct: float = 1.0,
        sim_date: str | None = None,
        bars: dict[str, pd.DataFrame] | None = None,
        fill_price: float | None = None,
    ) -> dict | None:
        """Execute an exit.

        If fill_price is provided (e.g. intraday exit at current price),
        use it directly with slippage.  Otherwise, fall back to the day's
        open price (used for MORNING exits that execute at market open).
        """
        pos = self._positions.get(ticker)
        if not pos:
            return None
        # Use stored sim context if not explicitly provided
        sim_date = sim_date or self._sim_date
        bars = bars or self._sim_bars

        exit_qty = max(1, int(pos.qty * exit_pct)) if qty is None else qty

        if fill_price is not None:
            # Intraday exit: use provided price with slippage
            slip = self._compute_slippage(
                fill_price, exit_qty, bars.get(ticker) if bars else None, is_buy=False,
            )
            open_price = round(fill_price + slip, 2)
        elif bars is None:
            # Without bars, use current price
            open_price = pos.current_price
        else:
            df = bars.get(ticker)
            if df is None:
                return None
            ref = pd.Timestamp(sim_date) if sim_date else df.index[-1]
            day_data = df[df.index <= ref]
            if day_data.empty:
                return None
            raw_open = float(day_data.iloc[-1]['open'])
            # Apply volume-aware slippage: sell fills slightly worse than open
            slip = self._compute_slippage(
                raw_open, exit_qty, df, is_buy=False,
            )
            open_price = round(raw_open + slip, 2)  # slip is negative for sells

        exit_qty = max(1, int(pos.qty * exit_pct)) if qty is None else qty
        if exit_qty >= pos.qty:
            exit_qty = pos.qty

        is_full_exit = (exit_qty >= pos.qty)
        proceeds = open_price * exit_qty
        pnl = (open_price - pos.avg_entry_price) * exit_qty
        self._cash += proceeds

        if is_full_exit:
            del self._positions[ticker]
        else:
            pos.qty -= exit_qty

        return {
            'ticker': ticker,
            'action': 'EXIT' if is_full_exit else 'PARTIAL_EXIT',
            'exit_qty': exit_qty, 'exit_price': round(open_price, 2),
            'pnl': round(pnl, 2), 'date': sim_date or 'unknown',
        }

    def update_stop(
        self,
        ticker: str,
        new_stop: float,
        bracket_order_id: str | None = None,
    ) -> dict:
        """Update stop-loss price for a held position."""
        pos = self._positions.get(ticker)
        if not pos:
            return {'modified': False, 'error': f'No position for {ticker}'}
        old_stop = pos.stop_loss_price
        pos.stop_loss_price = float(new_stop)
        logger.debug("MockBroker: %s stop updated %.2f → %.2f", ticker, old_stop, new_stop)
        return {'modified': True, 'ticker': ticker, 'old_stop': old_stop, 'new_stop': new_stop}

    # ------------------------------------------------------------------
    # Simulation helpers (override base no-ops)
    # ------------------------------------------------------------------

    def cancel_day_orders(self) -> list[dict]:
        """Cancel unfilled day orders (LIMIT with time_in_force='day').

        Called at EOD. Returns list of cancelled order events.
        """
        cancelled = []
        remaining = []
        for order in self.pending_orders:
            if order.time_in_force == 'day':
                if order.entry_type == 'STOP':
                    reason = f'stop trigger ${order.limit_price:.2f} not reached'
                else:
                    reason = f'limit ${order.limit_price:.2f} not reached'
                cancelled.append({
                    'ticker': order.ticker, 'action': 'ORDER_EXPIRED',
                    'reason': reason,
                    'order_type': order.entry_type,
                    'limit_price': order.limit_price,
                })
            else:
                remaining.append(order)
        self.pending_orders = remaining
        return cancelled

    def check_stops_midday(
        self,
        sim_date: str | None = None,
        bars: dict[str, pd.DataFrame] | None = None,
        hourly_bars: dict[str, pd.DataFrame] | None = None,
        cutoff_utc: str = '15:30',
    ) -> list[dict]:
        """Check stop-loss triggers using hourly bars up to cutoff.

        Called BEFORE intraday LLM so that stopped-out positions are
        removed before the LLM reviews them.  Falls back to daily low
        when hourly data is unavailable.
        """
        sim_date = sim_date or self._sim_date
        bars = bars or self._sim_bars or {}
        hourly_bars = hourly_bars or self._sim_hourly

        stopped_out = []
        for ticker, pos in list(self._positions.items()):
            if pos.stop_loss_price <= 0:
                continue

            low, _ = self._hourly_extremes(hourly_bars, ticker, sim_date, cutoff_utc)
            if low is None:
                # Fallback: daily bar low (conservative — may include post-cutoff data)
                df = bars.get(ticker)
                if df is None or df.empty:
                    continue
                day_data = df[df.index <= pd.Timestamp(sim_date)]
                if day_data.empty:
                    continue
                low = float(day_data.iloc[-1]['low'])

            if low <= pos.stop_loss_price:
                slip = self._compute_slippage(
                    pos.stop_loss_price, pos.qty, bars.get(ticker), is_buy=False,
                )
                exit_price = round(pos.stop_loss_price + slip, 2)
                pnl = (exit_price - pos.avg_entry_price) * pos.qty
                self._cash += exit_price * pos.qty
                stopped_out.append({
                    'ticker': ticker, 'action': 'STOP_LOSS',
                    'exit_price': exit_price, 'entry_price': pos.avg_entry_price,
                    'pnl': round(pnl, 2), 'qty': pos.qty, 'date': sim_date,
                })
                del self._positions[ticker]
                logger.info("MockBroker: %s mid-day stop hit @ %.2f (low=%.2f, stop=%.2f)",
                            ticker, exit_price, low, pos.stop_loss_price)

        return stopped_out

    def advance_day(
        self,
        sim_date: str,
        bars: dict[str, pd.DataFrame],
        hourly_bars: dict[str, pd.DataFrame] | None = None,
    ) -> list[dict]:
        """Update prices, fill remaining LIMIT orders, trigger stops. Returns events.

        1. Final fill attempt for LIMIT orders (hourly bars up to 21:00 UTC / market close)
        2. Expire any still-unfilled day orders
        3. Update positions to closing prices
        4. Trigger stop-loss hits
        """
        stopped_out = []

        # Final fill attempt for remaining LIMIT orders (full day hourly bars)
        if self.pending_orders and hourly_bars:
            final_fills = self.fill_pending(
                sim_date, bars, hourly_bars=hourly_bars, cutoff_utc='21:00',
            )
            stopped_out.extend(final_fills)

        # Expire any still-unfilled day orders
        expired = self.cancel_day_orders()
        stopped_out.extend(expired)
        for ticker, pos in list(self._positions.items()):
            df = bars.get(ticker)
            if df is None or df.empty:
                continue
            day_data = df[df.index <= pd.Timestamp(sim_date)]
            if day_data.empty:
                continue
            row = day_data.iloc[-1]
            pos.current_price = float(row['close'])
            pos.unrealized_pnl = (pos.current_price - pos.avg_entry_price) * pos.qty
            if pos.current_price > pos.highest_close:
                pos.highest_close = pos.current_price

            if pos.stop_loss_price > 0 and float(row['low']) <= pos.stop_loss_price:
                # Stop fills with slippage (stops tend to fill worse, especially with impact)
                slip = self._compute_slippage(
                    pos.stop_loss_price, pos.qty, df, is_buy=False,
                )
                exit_price = round(pos.stop_loss_price + slip, 2)  # slip is negative
                pnl = (exit_price - pos.avg_entry_price) * pos.qty
                self._cash += exit_price * pos.qty
                stopped_out.append({
                    'ticker': ticker, 'action': 'STOP_LOSS',
                    'exit_price': exit_price, 'entry_price': pos.avg_entry_price,
                    'pnl': round(pnl, 2), 'qty': pos.qty, 'date': sim_date,
                })
                del self._positions[ticker]

        if self.portfolio_value > self.peak_value:
            self.peak_value = self.portfolio_value
        return stopped_out

    def _hourly_extremes(
        self,
        hourly_bars: dict[str, pd.DataFrame] | None,
        ticker: str,
        sim_date: str,
        cutoff_utc: str | None,
    ) -> tuple[float | None, float | None]:
        """Return (lowest low, highest high) for ticker on sim_date up to cutoff_utc.

        Returns (None, None) if no hourly data available.
        """
        if hourly_bars is None:
            return None, None
        hdf = hourly_bars.get(ticker)
        if hdf is None or hdf.empty:
            return None, None
        sim_ts = pd.Timestamp(sim_date)
        day = hdf[hdf.index.date == sim_ts.date()]
        if day.empty:
            return None, None
        if cutoff_utc:
            cutoff = pd.Timestamp(f'{sim_date} {cutoff_utc}:00')
            day = day[day.index <= cutoff]
        if day.empty:
            return None, None
        return float(day['low'].min()), float(day['high'].max())

    def _compute_slippage(
        self,
        price: float,
        shares: int,
        df: pd.DataFrame | None,
        is_buy: bool = True,
    ) -> float:
        """Compute realistic slippage: base spread + volume-aware market impact.

        Based on the Almgren (2005) square-root market impact model:
          impact = η × σ_daily × √(shares / ADV) × price

        where η is ``slippage_impact_coeff`` (default 0.1, conservative).
        The base spread (``slippage_bps``) is always applied as a minimum.

        Returns the signed slippage amount (positive for buys, negative for sells).
        """
        import numpy as np

        base = price * self.slippage_bps / 10_000
        impact = 0.0

        if df is not None and len(df) >= 5 and 'volume' in df.columns:
            adv = float(df['volume'].tail(20).mean())
            if adv > 0 and shares > 0:
                participation = shares / adv
                # Daily volatility from recent close-to-close returns
                returns = df['close'].pct_change().dropna().tail(20)
                if len(returns) >= 5:
                    daily_vol = float(returns.std())
                    if daily_vol > 0:
                        impact = (
                            self.slippage_impact_coeff
                            * daily_vol
                            * np.sqrt(participation)
                            * price
                        )

        total = base + impact
        return total if is_buy else -total

    def fill_pending(
        self,
        sim_date: str | None = None,
        bars: dict[str, pd.DataFrame] | None = None,
        hourly_bars: dict[str, pd.DataFrame] | None = None,
        cutoff_utc: str | None = None,
    ) -> list[dict]:
        """Fill pending entry orders. Returns fill/reject events.

        MARKET (opg): fill at open price with slippage.
        LIMIT (day):  fill at limit_price if hourly low ≤ limit_price
                       up to cutoff_utc; else stay pending.

        When called without arguments, uses context from set_sim_context().
        """
        # Use stored sim context if not explicitly provided
        sim_date = sim_date or self._sim_date
        bars = bars or self._sim_bars
        hourly_bars = hourly_bars or self._sim_hourly
        if not sim_date or not bars:
            return []
        fills = []
        remaining = []
        for order in self.pending_orders:
            df = bars.get(order.ticker)
            if df is None:
                remaining.append(order)
                continue
            day_data = df[df.index == pd.Timestamp(sim_date)]
            if day_data.empty:
                day_data = df[df.index <= pd.Timestamp(sim_date)]
                if day_data.empty or day_data.index[-1].strftime('%Y-%m-%d') != sim_date:
                    remaining.append(order)
                    continue
                day_data = day_data.iloc[[-1]]

            row = day_data.iloc[0]
            raw_open = float(row['open'])

            # ── Determine fill price based on order type ────────────────
            h_low, h_high = self._hourly_extremes(
                hourly_bars, order.ticker, sim_date, cutoff_utc,
            )
            if h_low is None:
                h_low = float(row['low'])
            if h_high is None:
                h_high = float(row['high'])

            if order.entry_type == 'LIMIT' and order.limit_price:
                # LIMIT: fill at limit_price if price drops to it
                if h_low > order.limit_price:
                    remaining.append(order)
                    continue
                # LIMIT fills at limit_price + small spread (no impact — passive order)
                fill_price = round(
                    order.limit_price + order.limit_price * self.slippage_bps / 10_000 * 0.5,
                    2,
                )
            elif order.entry_type == 'STOP' and order.limit_price:
                # STOP (buy-stop): fill when price rises to trigger level
                if h_high < order.limit_price:
                    remaining.append(order)
                    continue
                # Filled at stop trigger price with volume-aware slippage
                slip = self._compute_slippage(
                    order.limit_price, order.shares, df, is_buy=True,
                )
                fill_price = round(order.limit_price + slip, 2)
            else:
                # MARKET order: fill at open with volume-aware slippage
                slip = self._compute_slippage(
                    raw_open, order.shares, df, is_buy=True,
                )
                fill_price = round(raw_open + slip, 2)

            # ── Reject if fill price breaches stop loss ─────────────────
            if fill_price <= order.stop_loss:
                fills.append({
                    'ticker': order.ticker, 'action': 'ENTRY_REJECTED',
                    'reason': f'fill {fill_price:.2f} <= stop {order.stop_loss:.2f}',
                    'date': sim_date,
                })
                continue

            # ── Reject if gap from signal price exceeds 3% (MARKET only) ─
            if order.entry_type != 'LIMIT' and order.signal_price > 0:
                gap_pct = (fill_price - order.signal_price) / order.signal_price
                if abs(gap_pct) > 0.03:
                    fills.append({
                        'ticker': order.ticker, 'action': 'ENTRY_REJECTED',
                        'reason': f'gap {gap_pct:+.1%} (signal {order.signal_price:.2f} -> open {fill_price:.2f})',
                        'date': sim_date,
                    })
                    continue

            # ── R:R recalculation at fill price ─────────────────────────
            # For MARKET/STOP orders, R:R was already validated by PM at EOD
            # and by morning triage. ATR-based recalculation here always gives
            # R:R ≈ tp_mult/stop_mult (e.g. 3.0/2.0 = 1.5), so the check is
            # redundant and subject to rounding artifacts.
            # Only revalidate LIMIT orders where the controlled fill price may
            # shift R:R meaningfully vs what PM saw at EOD.
            atr = order.atr or 0.0
            if atr > 0:
                new_stop = round(fill_price - self.atr_stop_multiplier * atr, 2)
                if order.entry_type == 'LIMIT':
                    new_tp = round(fill_price + 3.0 * atr, 2)
                    risk = fill_price - new_stop
                    reward = new_tp - fill_price
                    rr = reward / risk if risk > 0 else 0.0
                    if rr < self.min_entry_rr_ratio - 0.05:
                        fills.append({
                            'ticker': order.ticker, 'action': 'ENTRY_REJECTED',
                            'reason': (f'R:R {rr:.2f} < {self.min_entry_rr_ratio} at '
                                       f'${fill_price:.2f} (stop ${new_stop:.2f}, TP ${new_tp:.2f})'),
                            'date': sim_date,
                        })
                        continue
                order.stop_loss = new_stop

            # ── Cash check ──────────────────────────────────────────────
            cost = fill_price * order.shares
            if cost > self._cash:
                fills.append({
                    'ticker': order.ticker, 'action': 'ENTRY_REJECTED',
                    'reason': f'insufficient cash ({self._cash:.0f} < {cost:.0f})',
                    'date': sim_date,
                })
                continue

            # ── Fill ────────────────────────────────────────────────────
            self._cash -= cost
            existing = self._positions.get(order.ticker)
            if existing:
                # AUTO_ADD to existing position: weighted average entry price
                total_qty = existing.qty + order.shares
                avg_price = (
                    (existing.avg_entry_price * existing.qty + fill_price * order.shares)
                    / total_qty
                )
                existing.qty = total_qty
                existing.avg_entry_price = round(avg_price, 4)
                existing.current_price = fill_price
                existing.unrealized_pnl = (fill_price - existing.avg_entry_price) * total_qty
            else:
                self._positions[order.ticker] = Position(
                    symbol=order.ticker, qty=order.shares,
                    avg_entry_price=fill_price, current_price=fill_price,
                    stop_loss_price=order.stop_loss, unrealized_pnl=0.0,
                    entry_date=sim_date, strategy=order.strategy,
                    signal_price=order.signal_price,
                    highest_close=fill_price,
                )
            fills.append({
                'ticker': order.ticker, 'action': 'ENTRY_FILLED',
                'shares': order.shares, 'fill_price': fill_price,
                'signal_price': order.signal_price,
                'stop_loss': round(order.stop_loss, 2), 'cost': round(cost, 2),
                'date': sim_date, 'strategy': order.strategy,
                'order_type': order.entry_type,
            })
        self.pending_orders = remaining
        return fills
