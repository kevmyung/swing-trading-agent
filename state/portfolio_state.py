"""
state/portfolio_state.py — In-memory + file-backed portfolio state.

PortfolioState is the single source of truth for all runtime state:
positions, cash, P&L, drawdown metrics, and circuit-breaker status.

Design decisions:
- Dataclass-based for easy serialisation to/from JSON
- Atomic file writes (write-to-temp then rename) to prevent corruption
- No external dependencies beyond Python stdlib
- All monetary values in USD (float)
- All percentages as fractions (0.02 = 2%, not "2")
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_TRADE_HISTORY = 200


def _now_iso() -> str:
    """Return current UTC time as an ISO 8601 string with 'Z' suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Position:
    """Represents a single open position in the portfolio."""

    symbol: str
    qty: int
    avg_entry_price: float
    current_price: float
    stop_loss_price: float
    unrealized_pnl: float = 0.0
    entry_date: str = ""        # ISO format YYYY-MM-DD
    strategy: str = ""          # 'MOMENTUM' or 'MEAN_REVERSION'
    signal_price: float = 0.0   # price at decision time (EOD close) — for slippage tracking
    bracket_order_id: str = ""  # Alpaca parent order ID for bracket modification
    entry_conditions: dict = field(default_factory=dict)  # snapshot of conditions at entry time
    entry_qty: int = 0          # original entry quantity (for partial exit tracking)
    partial_exit_count: int = 0 # number of partial exits taken
    scaled_entry: bool = False  # True when entered via half_size (cleared on ADD fill)
    scaled_up: bool = False     # True after auto-ADD fill (shown once in position table, then cleared)
    tighten_active: bool = False  # PM signalled thesis concern → tighter trailing
    last_conviction: str = ""     # latest PM conviction (high/medium/low) → affects MOM trailing
    highest_close: float = 0.0    # high-water mark for chandelier trailing stop
    consecutive_high_conviction: int = 0  # EOD cycles with consecutive high conviction (for auto-ADD gating)

    @property
    def market_value(self) -> float:
        """Current market value = current_price × qty."""
        return self.current_price * self.qty

    @classmethod
    def from_dict(cls, data: dict) -> "Position":
        """Deserialise a Position from a plain dict (e.g. from JSON load)."""
        known = {k for k in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Trade:
    """Represents a completed (closed) trade."""

    symbol: str
    side: str       # 'buy' or 'sell'
    qty: int
    price: float    # exit price for close trades
    pnl: float      # realised P&L in USD (negative = loss)
    timestamp: str  # ISO 8601 UTC timestamp
    strategy: str   # 'MOMENTUM' | 'MEAN_REVERSION' | 'PEAD' | 'STOP_LOSS'
    entry_price: float = 0.0  # avg entry price — enables return % calculation
    holding_days: int = 0     # calendar days held
    signal_price: float = 0.0   # price at decision time (EOD close)
    slippage_bps: float = 0.0   # entry slippage: (fill - signal) / signal * 10000

    @classmethod
    def from_dict(cls, data: dict) -> "Trade":
        """Deserialise a Trade from a plain dict."""
        known = {k for k in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class PortfolioState:
    """
    Complete runtime state of the trading system.

    Persisted to ``state_file`` (JSON) after every mutation.
    """

    state_file: str = "state/portfolio.json"

    # Core account metrics
    cash: float = 0.0
    portfolio_value: float = 0.0
    peak_value: float = 0.0
    daily_start_value: float = 0.0

    # Risk tracking
    consecutive_losses: int = 0

    # Positions and trade history
    positions: Dict[str, Position] = field(default_factory=dict)
    trade_history: List[Trade] = field(default_factory=list)

    # Pending signals from EOD_SIGNAL cycle (consumed by MORNING cycle)
    pending_signals: Optional[dict] = None
    signal_date: str = ""   # YYYY-MM-DD; signals expire if != today

    # Regime tracking (previous regime helps LLM interpret TRANSITIONAL)
    last_regime: str = ""   # regime from most recent EOD_SIGNAL cycle

    # Metadata
    last_synced: str = ""
    last_saved: str = ""
    trading_day: str = ""

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """
        Load state from the JSON file at ``state_file``.

        If the file does not exist, the state stays at default (empty) values.
        Sets last_synced to the current UTC timestamp after a successful load.
        """
        if not os.path.isfile(self.state_file):
            logger.info(
                "State file '%s' not found — starting with empty state.", self.state_file
            )
            self.last_synced = _now_iso()
            return

        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not parse state file '%s': %s. Using empty state.",
                self.state_file,
                exc,
            )
            return

        self.cash = float(data.get("cash", 0.0))
        self.portfolio_value = float(data.get("portfolio_value", 0.0))
        self.peak_value = float(data.get("peak_value", 0.0))
        self.daily_start_value = float(data.get("daily_start_value", 0.0))
        self.consecutive_losses = int(data.get("consecutive_losses", 0))
        self.trading_day = data.get("trading_day", "")
        self.last_regime = data.get("last_regime", "")
        self.pending_signals = data.get("pending_signals", None)
        self.signal_date = data.get("signal_date", "")
        raw_positions = data.get("positions", {})
        self.positions = {
            sym: Position.from_dict(pos_data)
            for sym, pos_data in raw_positions.items()
        }

        raw_trades = data.get("trade_history", [])
        self.trade_history = [Trade.from_dict(t) for t in raw_trades]

        self.last_synced = _now_iso()
        logger.debug(
            "Loaded state from '%s': %d positions, %d trades.",
            self.state_file,
            len(self.positions),
            len(self.trade_history),
        )

    def save(self) -> None:
        """
        Atomically persist state to the JSON file at ``state_file``.

        Writes to a .tmp file first, then uses os.replace() to avoid
        corruption on interrupted writes. Creates parent directory if needed.
        """
        parent = os.path.dirname(self.state_file)
        if parent:
            os.makedirs(parent, exist_ok=True)

        self.last_saved = _now_iso()
        payload = {
            "cash": self.cash,
            "portfolio_value": self.portfolio_value,
            "peak_value": self.peak_value,
            "daily_start_value": self.daily_start_value,
            "consecutive_losses": self.consecutive_losses,
            "trading_day": self.trading_day,
            "last_regime": self.last_regime,
            "pending_signals": self.pending_signals,
            "signal_date": self.signal_date,
            "last_synced": self.last_synced,
            "last_saved": self.last_saved,
            "positions": {sym: asdict(pos) for sym, pos in self.positions.items()},
            "trade_history": [asdict(t) for t in self.trade_history],
        }

        tmp_path = self.state_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp_path, self.state_file)
        logger.debug("State saved to '%s'.", self.state_file)

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def close_position(self, symbol: str, exit_price: float) -> Trade:
        """
        Close an open position, record the trade, and update cash.

        Args:
            symbol: Ticker symbol of the position to close.
            exit_price: The price at which the position is closed.

        Returns:
            The Trade object created for this close event.

        Raises:
            KeyError: If no open position exists for ``symbol``.
        """
        position = self.positions.pop(symbol)
        realized_pnl = (exit_price - position.avg_entry_price) * position.qty
        trade = Trade(
            symbol=symbol,
            side="sell",
            qty=position.qty,
            price=exit_price,
            pnl=realized_pnl,
            timestamp=_now_iso(),
            strategy=position.strategy,
        )
        self.record_trade(trade)
        # Return proceeds to cash
        self.cash += exit_price * position.qty
        self._recalculate_portfolio_value()
        self.save()
        return trade

    def record_trade(self, trade: Trade) -> None:
        """
        Append a trade to history and update consecutive_losses counter.

        Trims history to the last MAX_TRADE_HISTORY entries.

        Args:
            trade: Completed Trade to record.
        """
        self.trade_history.append(trade)
        if len(self.trade_history) > MAX_TRADE_HISTORY:
            self.trade_history = self.trade_history[-MAX_TRADE_HISTORY:]

        if trade.pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    # ------------------------------------------------------------------
    # Pending signals (EOD_SIGNAL → MORNING handoff)
    # ------------------------------------------------------------------

    def save_pending_signals(self, signals: dict, sim_date: str | None = None) -> None:
        """Persist EOD signals for MORNING cycle consumption.

        Args:
            signals: Signal dict produced by EOD_SIGNAL cycle.
            sim_date: Simulation date (backtest). Defaults to today.
        """
        self.pending_signals = signals
        self.signal_date = sim_date or str(date.today())
        self.save()
        logger.info("Pending signals saved for %s.", self.signal_date)

    def load_pending_signals(self, sim_date: str | None = None) -> Optional[dict]:
        """Return pending signals if present.

        EOD saves signals on Day N; MORNING consumes them on Day N+1.
        No date matching is performed — the clear-after-consume pattern
        in MORNING guarantees signals are used exactly once.

        Args:
            sim_date: For logging only (not used for matching).

        Returns:
            Signal dict if present, None if absent.
        """
        if not self.pending_signals:
            return None
        logger.info(
            "Loading pending signals from %s (consuming on %s).",
            self.signal_date, sim_date or str(date.today()),
        )
        return self.pending_signals

    def clear_pending_signals(self) -> None:
        """Remove pending signals after MORNING cycle consumes them."""
        self.pending_signals = None
        self.signal_date = ""
        self.save()
        logger.debug("Pending signals cleared.")

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    @property
    def current_drawdown_pct(self) -> float:
        """Current drawdown from peak as a fraction (0.0–1.0)."""
        if self.peak_value == 0:
            return 0.0
        return max(0.0, (self.peak_value - self.portfolio_value) / self.peak_value)

    @property
    def daily_loss_pct(self) -> float:
        """
        Today's portfolio loss as a fraction of ``daily_start_value``.

        Returns 0.0 when portfolio is up on the day or when no baseline is set.
        """
        if self.daily_start_value == 0:
            return 0.0
        loss = (self.daily_start_value - self.portfolio_value) / self.daily_start_value
        return max(0.0, loss)

    @property
    def position_count(self) -> int:
        """Number of currently open positions."""
        return len(self.positions)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _recalculate_portfolio_value(self) -> None:
        """Recompute portfolio_value = cash + sum of position market values."""
        positions_value = sum(
            p.current_price * p.qty for p in self.positions.values()
        )
        self.portfolio_value = self.cash + positions_value

    def to_summary_dict(self) -> dict:
        """
        Return a compact dict for injecting into agent prompts.

        Includes all key risk and exposure metrics in a serialisable form.
        """
        return {
            "portfolio_value": self.portfolio_value,
            "cash": self.cash,
            "position_count": self.position_count,
            "current_drawdown_pct": round(self.current_drawdown_pct, 4),
            "daily_loss_pct": round(self.daily_loss_pct, 4),
            "consecutive_losses": self.consecutive_losses,
            "positions": [
                {
                    "symbol": sym,
                    "qty": pos.qty,
                    "unrealized_pnl": round(pos.unrealized_pnl, 2),
                }
                for sym, pos in self.positions.items()
            ],
        }

    def __repr__(self) -> str:
        return (
            f"PortfolioState("
            f"value={self.portfolio_value:.2f}, "
            f"positions={self.position_count}, "
            f"drawdown={self.current_drawdown_pct:.1%})"
        )
