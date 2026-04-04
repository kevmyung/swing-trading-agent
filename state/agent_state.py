"""
state/agent_state.py — Unified agent state.

Extends PortfolioState with watchlist, cycle logs, decision log, and daily stats.
All state is persisted to a single JSON file.

Storage design (lifecycle-separated):
  - cycle_logs:   research snapshots per cycle (no decisions). Pruned by date window.
  - decision_log: unified decisions + execution results. Sliding window (N days).
  - daily_stats:  latest-only EOD performance summary.
  - watchlist:    current tickers being tracked.

Usage:
    state = AgentState(state_file="state/agent.json")
    state.load()
    set_state(state)
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from state.portfolio_state import PortfolioState, Position, Trade, _now_iso

logger = logging.getLogger(__name__)


@dataclass
class AgentState(PortfolioState):
    """Unified state: portfolio + watchlist + cycle logs + daily stats."""

    watchlist: list = field(default_factory=list)
    cycle_logs: list = field(default_factory=list)
    decision_log: list = field(default_factory=list)  # unified decisions + execution results
    daily_stats: list = field(default_factory=list)
    pm_notes: dict = field(default_factory=dict)  # PM's persistent notes

    # ------------------------------------------------------------------
    # Persistence (overrides PortfolioState)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialize state to a plain dict (for store persistence)."""
        return {
            # Portfolio
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
            # Journal
            "watchlist": self.watchlist,
            "cycle_logs": self.cycle_logs,
            "decision_log": self.decision_log,
            "daily_stats": self.daily_stats,
            "pm_notes": self.pm_notes,
        }

    def save(self) -> None:
        # Auto-prune stale data before persisting
        self.prune_stale_notes()

        # Cloud mode: state is persisted via store.save_state() — skip local file I/O
        if os.environ.get("STORE_MODE", "").lower() == "cloud":
            self.last_saved = _now_iso()
            return

        parent = os.path.dirname(self.state_file)
        if parent:
            os.makedirs(parent, exist_ok=True)

        self.last_saved = _now_iso()
        payload = self.to_dict()

        tmp_path = self.state_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(tmp_path, self.state_file)
        logger.debug("AgentState saved to '%s'.", self.state_file)

    def load_from_dict(self, data: dict) -> None:
        """Populate state from a plain dict (e.g. from S3 store)."""
        # Portfolio fields
        self.cash = float(data.get("cash", 0.0))
        self.portfolio_value = float(data.get("portfolio_value", 0.0))
        self.peak_value = float(data.get("peak_value", 0.0))
        self.daily_start_value = float(data.get("daily_start_value", 0.0))
        self.consecutive_losses = int(data.get("consecutive_losses", 0))

        self.trading_day = data.get("trading_day", "")
        self.last_regime = data.get("last_regime", "")
        self.pending_signals = data.get("pending_signals", None)
        self.signal_date = data.get("signal_date", "")
        self.positions = {
            sym: Position.from_dict(pos_data)
            for sym, pos_data in data.get("positions", {}).items()
        }
        self.trade_history = [Trade.from_dict(t) for t in data.get("trade_history", [])]
        self.last_synced = _now_iso()

        # Journal fields
        self.watchlist = data.get("watchlist", [])
        self.cycle_logs = data.get("cycle_logs", [])
        self.decision_log = data.get("decision_log", [])
        self.daily_stats = data.get("daily_stats", [])
        self.pm_notes = data.get("pm_notes", {})

    def load(self) -> None:
        if not os.path.isfile(self.state_file):
            logger.info("State file '%s' not found — starting empty.", self.state_file)
            self.last_synced = _now_iso()
            return

        try:
            with open(self.state_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Could not parse '%s': %s. Using empty state.", self.state_file, exc
            )
            return

        self.load_from_dict(data)

    # ------------------------------------------------------------------
    # Watchlist helpers
    # ------------------------------------------------------------------

    def watchlist_add(self, ticker: str, reason: str = "") -> bool:
        """Add ticker to watchlist. Returns False if already present."""
        from datetime import datetime, timezone
        ticker = ticker.upper().strip()
        if any(w["ticker"] == ticker for w in self.watchlist):
            return False
        self.watchlist.append({
            "ticker": ticker,
            "reason": reason,
            "added_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        })
        return True

    def watchlist_remove(self, ticker: str) -> bool:
        """Remove ticker from watchlist. Returns False if not found."""
        ticker = ticker.upper().strip()
        entry = next((w for w in self.watchlist if w["ticker"] == ticker), None)
        if not entry:
            return False
        self.watchlist.remove(entry)
        return True

    # ------------------------------------------------------------------
    # PM Notes — persistent cross-cycle memory
    # ------------------------------------------------------------------

    def update_pm_notes(self, notes: dict[str, str | None], as_of: str = "") -> dict:
        """Update PM notes. Keys are note topics (ticker or general label).

        Each note is stored as {"text": str, "date": "YYYY-MM-DD"} so
        staleness can be shown in prompts. Set value to None or "" to delete.
        Returns the updated notes dict.
        """
        from datetime import date as _date
        today = as_of or str(_date.today())
        for key, value in notes.items():
            key = key.strip()
            if not key:
                continue
            if value is None or (isinstance(value, str) and not value.strip()):
                self.pm_notes.pop(key, None)
            else:
                self.pm_notes[key] = {"text": str(value).strip(), "date": today}
        return self.pm_notes

    def clear_ticker_notes(self, ticker: str) -> list[str]:
        """Remove all notes keyed by ticker (called on EXIT/STOP).

        Returns list of removed keys.
        """
        ticker = ticker.upper().strip()
        removed = [k for k in list(self.pm_notes.keys()) if k.upper() == ticker]
        for k in removed:
            del self.pm_notes[k]
        return removed

    def prune_stale_notes(
        self,
        max_age_days: int = 3,
        orphan_age_days: int = 3,
        as_of: str = "",
    ) -> list[str]:
        """Remove stale notes based on lifecycle rules.

        Lifecycle:
          - Held-position / watchlist ticker notes: NOT pruned here
          - Orphan ticker notes (not in positions or watchlist): pruned after orphan_age_days
          - General notes: pruned after max_age_days
          - Legacy string notes (no date): pruned immediately

        Returns list of pruned keys.
        """
        from datetime import date as _date
        today = _date.fromisoformat(as_of) if as_of else _date.today()
        held_tickers = {t.upper() for t in self.positions}
        watchlist_tickers = {w["ticker"].upper() for w in self.watchlist if isinstance(w, dict)}
        active_tickers = held_tickers | watchlist_tickers

        pruned = []
        for key in list(self.pm_notes.keys()):
            is_ticker = key.isalpha() and key.isupper() and len(key) <= 5
            if is_ticker and key in active_tickers:
                continue

            entry = self.pm_notes[key]
            if isinstance(entry, dict) and entry.get('date'):
                try:
                    written = _date.fromisoformat(entry['date'])
                    age = (today - written).days
                    threshold = orphan_age_days if is_ticker else max_age_days
                    if age > threshold:
                        del self.pm_notes[key]
                        pruned.append(key)
                except (ValueError, TypeError):
                    pass
            elif isinstance(entry, str):
                # Legacy format (no date) — prune
                del self.pm_notes[key]
                pruned.append(key)
        return pruned

    # ------------------------------------------------------------------
    # Cycle logs — single source of truth for research + decisions
    # ------------------------------------------------------------------

    def record_cycle(
        self,
        cycle_type: str,
        date: str,
        *,
        research: dict | None = None,
        regime: str = "",
        candidate_tickers: list[str] | None = None,
    ) -> None:
        """Record a cycle to cycle_logs (research snapshots only).

        Decisions are stored in decision_log via record_decision().
        Heavy data (quant_context, events, risk_state) is persisted
        separately via store.save_day() and is NOT kept in memory.
        """
        entry: dict[str, Any] = {
            "cycle": cycle_type,
            "date": date,
            "timestamp": _now_iso(),
            "regime": regime,
        }
        if research is not None:
            entry["research"] = research
        if candidate_tickers is not None:
            entry["candidate_tickers"] = candidate_tickers

        self.cycle_logs.append(entry)
        self._prune_cycle_logs()

    # Maximum number of unique dates to retain in cycle_logs.
    # All consumers need at most 5-6 days; 10 gives comfortable headroom.
    _CYCLE_LOG_RETENTION_DAYS = 10

    def _prune_cycle_logs(self) -> None:
        """Drop cycle_logs older than the retention window."""
        if len(self.cycle_logs) < 20:
            return
        dates = sorted({log["date"] for log in self.cycle_logs if log.get("date")})
        if len(dates) <= self._CYCLE_LOG_RETENTION_DAYS:
            return
        cutoff = dates[-self._CYCLE_LOG_RETENTION_DAYS]
        before = len(self.cycle_logs)
        self.cycle_logs = [log for log in self.cycle_logs if log.get("date", "") >= cutoff]
        if len(self.cycle_logs) < before:
            logger.debug("Pruned cycle_logs: %d -> %d entries", before, len(self.cycle_logs))

    # ------------------------------------------------------------------
    # Decision log — unified decisions + execution results
    # ------------------------------------------------------------------

    _DECISION_LOG_RETENTION_DAYS = 5

    def record_decision(
        self,
        cycle_type: str,
        date: str,
        decisions: list[dict],
        regime: str = "",
    ) -> None:
        """Record PM decisions to decision_log.

        Each entry: {cycle, date, regime, decisions: [...]}
        EOD_SIGNAL entries also snapshot pm_notes so that
        build_decision_history can show 5-day note trajectories.
        Pruned by sliding window on unique dates.
        """
        if not decisions:
            return
        entry: dict[str, Any] = {
            "cycle": cycle_type,
            "date": date,
            "timestamp": _now_iso(),
            "regime": regime,
            "decisions": decisions,
        }
        # Snapshot ticker-level pm_notes for EOD decisions
        if cycle_type == "EOD_SIGNAL" and self.pm_notes:
            decision_tickers = {
                d.get("ticker", "").upper() for d in decisions if d.get("ticker")
            }
            notes_snap = {}
            for key, val in self.pm_notes.items():
                if key.upper() in decision_tickers:
                    text = val.get("text", val) if isinstance(val, dict) else str(val)
                    if text:
                        notes_snap[key.upper()] = text
            if notes_snap:
                entry["notes_snapshot"] = notes_snap
        self.decision_log.append(entry)
        self._prune_decision_log()

    def record_execution(
        self,
        date: str,
        events: list[dict],
    ) -> None:
        """Record execution events (fills, rejections, stops) to decision_log.

        Stored as cycle_type='EXECUTION' so build_decision_history can
        render decisions and their outcomes together.
        """
        if not events:
            return
        self.decision_log.append({
            "cycle": "EXECUTION",
            "date": date,
            "timestamp": _now_iso(),
            "events": events,
        })
        self._prune_decision_log()

    def _prune_decision_log(self) -> None:
        """Drop decision_log entries older than the retention window."""
        if len(self.decision_log) < 10:
            return
        dates = sorted({e["date"] for e in self.decision_log if e.get("date")})
        if len(dates) <= self._DECISION_LOG_RETENTION_DAYS:
            return
        cutoff = dates[-self._DECISION_LOG_RETENTION_DAYS]
        before = len(self.decision_log)
        self.decision_log = [e for e in self.decision_log if e.get("date", "") >= cutoff]
        if len(self.decision_log) < before:
            logger.debug("Pruned decision_log: %d -> %d entries", before, len(self.decision_log))

    def get_decision_history(self, ticker: str, last_n: int = 3) -> list[dict]:
        """Derive per-ticker decision history from decision_log (newest first)."""
        ticker = ticker.upper()
        results: list[dict] = []
        for entry in reversed(self.decision_log):
            for dec in entry.get("decisions", []):
                if str(dec.get("ticker", "")).upper() == ticker:
                    enriched = {"date": entry.get("date", ""), **dec}
                    results.append(enriched)
                    if len(results) >= last_n:
                        return results
        return results

    # ------------------------------------------------------------------
    # Research history — derived from cycle_logs
    # ------------------------------------------------------------------

    def get_research_history(self, ticker: str, last_n: int = 3) -> list[dict]:
        """Derive per-ticker research history from cycle_logs (newest first)."""
        ticker = ticker.upper()
        results: list[dict] = []
        for log in reversed(self.cycle_logs):
            research = log.get("research")
            if not research:
                continue
            entry = research.get(ticker)
            if entry and isinstance(entry, dict):
                # Include cycle metadata
                enriched = {
                    "date": log.get("date", ""),
                    "cycle": log.get("cycle", ""),
                    **entry,
                }
                results.append(enriched)
                if len(results) >= last_n:
                    break
        return results

    def find_sector_peers_research(
        self, sector: str, exclude_ticker: str, last_n: int = 2,
    ) -> list[dict]:
        """Find recent research for same-sector tickers from cycle_logs."""
        if not sector:
            return []
        exclude = exclude_ticker.upper()
        seen: dict[str, dict] = {}  # ticker -> most recent research
        for log in reversed(self.cycle_logs):
            research = log.get("research")
            if not research:
                continue
            for ticker, entry in research.items():
                if ticker == exclude or ticker in seen:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("sector", "").lower() == sector.lower():
                    seen[ticker] = {
                        "_ticker": ticker,
                        "date": log.get("date", ""),
                        **entry,
                    }
        candidates = sorted(seen.values(), key=lambda x: x.get("date", ""), reverse=True)
        return candidates[:last_n]

    # ------------------------------------------------------------------
    # Daily stats — EOD performance summary
    # ------------------------------------------------------------------

    def record_daily_stats(
        self,
        date: str,
        portfolio_value: float,
        cash: float,
        positions: dict[str, Any],
        spy_close: float | None = None,
        regime: str = "",
        events: list[dict] | None = None,
        start_cash: float = 100_000.0,
    ) -> None:
        """Record end-of-day performance summary."""
        prev = self.daily_stats[-1] if self.daily_stats else None

        # Portfolio returns
        prev_pv = prev["portfolio_value"] if prev else start_cash
        daily_return = (portfolio_value - prev_pv) / prev_pv if prev_pv > 0 else 0.0
        cum_return = (portfolio_value - start_cash) / start_cash if start_cash > 0 else 0.0

        # Drawdown
        peak = max(portfolio_value, prev["peak_value"] if prev else start_cash)
        drawdown = (peak - portfolio_value) / peak if peak > 0 else 0.0
        max_dd = max(drawdown, prev["max_drawdown_pct"] / 100.0 if prev else 0.0)

        # SPY benchmark
        spy_daily_return = 0.0
        spy_cum_return = 0.0
        if spy_close is not None and prev and prev.get("spy_close"):
            spy_daily_return = (spy_close - prev["spy_close"]) / prev["spy_close"]
        if prev:
            spy_cum_return = (1 + prev.get("spy_cumulative_return_pct", 0) / 100.0) * (1 + spy_daily_return) - 1
        elif spy_close:
            spy_cum_return = 0.0  # first day

        # Per-position stats
        position_stats = {}
        for ticker, pos in positions.items():
            entry_price = pos.avg_entry_price if hasattr(pos, 'avg_entry_price') else pos.get('avg_entry_price', 0)
            current_price = pos.current_price if hasattr(pos, 'current_price') else pos.get('current_price', 0)
            qty = pos.qty if hasattr(pos, 'qty') else pos.get('qty', 0)
            entry_date = pos.entry_date if hasattr(pos, 'entry_date') else pos.get('entry_date', '')

            unrealized_pnl = (current_price - entry_price) * qty
            unrealized_return = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0
            weight = (current_price * qty) / portfolio_value if portfolio_value > 0 else 0.0

            # Daily return for this ticker
            prev_price = entry_price
            if prev and ticker in prev.get("positions", {}):
                prev_price = prev["positions"][ticker].get("current_price", entry_price)
            ticker_daily_return = (current_price - prev_price) / prev_price if prev_price > 0 else 0.0

            # Days held
            days_held = 0
            if entry_date:
                try:
                    days_held = (datetime.strptime(date, "%Y-%m-%d") - datetime.strptime(entry_date, "%Y-%m-%d")).days
                except ValueError:
                    pass

            position_stats[ticker] = {
                "qty": qty,
                "avg_entry_price": round(entry_price, 2),
                "current_price": round(current_price, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "unrealized_return_pct": round(unrealized_return * 100, 2),
                "weight_pct": round(weight * 100, 2),
                "daily_return_pct": round(ticker_daily_return * 100, 4),
                "days_held": days_held,
            }

        # Trade summary (cumulative)
        trades = self.trade_history
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        total_realized = sum(t.pnl for t in trades)

        trade_summary = {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades), 3) if trades else 0.0,
            "avg_win_pct": round(
                sum((t.pnl / (t.entry_price * t.qty)) for t in wins if t.entry_price > 0) / len(wins) * 100, 2
            ) if wins else 0.0,
            "avg_loss_pct": round(
                sum((t.pnl / (t.entry_price * t.qty)) for t in losses if t.entry_price > 0) / len(losses) * 100, 2
            ) if losses else 0.0,
            "total_realized_pnl": round(total_realized, 2),
        }

        # Entries / exits from events
        entries = [e.get("ticker", "") for e in (events or []) if e.get("action") == "ENTRY_FILLED"]
        exits = [
            {"ticker": e.get("ticker", ""), "pnl": e.get("pnl", 0.0)}
            for e in (events or [])
            if e.get("action") in ("EXIT", "PARTIAL_EXIT", "STOP_LOSS")
        ]

        stat = {
            "date": date,
            # Portfolio level
            "portfolio_value": round(portfolio_value, 2),
            "cash": round(cash, 2),
            "peak_value": round(peak, 2),
            "daily_return_pct": round(daily_return * 100, 4),
            "cumulative_return_pct": round(cum_return * 100, 4),
            "drawdown_pct": round(drawdown * 100, 4),
            "max_drawdown_pct": round(max_dd * 100, 4),
            # Benchmark
            "spy_close": spy_close,
            "spy_daily_return_pct": round(spy_daily_return * 100, 4),
            "spy_cumulative_return_pct": round(spy_cum_return * 100, 4),
            "excess_daily_return_pct": round((daily_return - spy_daily_return) * 100, 4),
            "excess_cumulative_return_pct": round((cum_return - spy_cum_return) * 100, 4),
            # Positions
            "position_count": len(positions),
            "positions": position_stats,
            # Events
            "entries": entries,
            "exits": exits,
            "regime": regime,
            # Trade summary
            "trade_summary": trade_summary,
        }
        self.daily_stats = [stat]  # keep only latest; history persisted via store

    # ------------------------------------------------------------------
    # Snapshot helpers (for backtest)
    # ------------------------------------------------------------------

    def to_snapshot(self) -> dict:
        """Export full state as a plain dict for backtest snapshot."""
        return {
            "cash": self.cash,
            "portfolio_value": self.portfolio_value,
            "peak_value": self.peak_value,
            "daily_start_value": self.daily_start_value,
            "consecutive_losses": self.consecutive_losses,

            "last_regime": self.last_regime,
            "pending_signals": self.pending_signals,
            "signal_date": self.signal_date,
            "positions": {t: asdict(p) for t, p in self.positions.items()},
            "trade_history": [asdict(t) for t in self.trade_history],
            "watchlist": self.watchlist,
            "cycle_logs": self.cycle_logs,
            "decision_log": self.decision_log,
            "daily_stats": self.daily_stats,
            "pm_notes": dict(self.pm_notes),
        }

    def restore_from_snapshot(self, snapshot: dict) -> None:
        """Restore state from a backtest snapshot dict."""
        self.cash = snapshot.get("cash", 0.0)
        self.portfolio_value = snapshot.get("portfolio_value", 0.0)
        self.peak_value = snapshot.get("peak_value", 0.0)
        self.daily_start_value = snapshot.get("daily_start_value", self.portfolio_value)
        self.consecutive_losses = snapshot.get("consecutive_losses", 0)

        self.last_regime = snapshot.get("last_regime", "")
        self.pending_signals = snapshot.get("pending_signals", None)
        self.signal_date = snapshot.get("signal_date", "")
        self.positions = {
            sym: Position.from_dict(p)
            for sym, p in snapshot.get("positions", {}).items()
        }
        self.trade_history = [Trade.from_dict(t) for t in snapshot.get("trade_history", [])]
        self.watchlist = snapshot.get("watchlist", [])
        self.cycle_logs = snapshot.get("cycle_logs", [])
        self.decision_log = snapshot.get("decision_log", [])
        self.daily_stats = snapshot.get("daily_stats", [])
        self.pm_notes = snapshot.get("pm_notes", {})


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_current_state: AgentState | None = None


def get_state() -> AgentState:
    """Return the current AgentState singleton. Raises if not set."""
    if _current_state is None:
        raise RuntimeError("AgentState not initialized. Call set_state() first.")
    return _current_state


def set_state(state: AgentState) -> None:
    """Set the module-level AgentState singleton."""
    global _current_state
    _current_state = state
