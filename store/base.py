"""store/base.py — Abstract base for session data storage.

Defines the interface that LocalStore (JSON files) and CloudStore (S3)
both implement. The backtest orchestrators (Preconditioner, Simulator) and
the frontend API call these methods without knowing the storage backend.

Data categories:
  META       — session config, status, mode, dates
  STATE      — current portfolio state (positions, cash, watchlist)
  PROGRESS   — live execution progress (current_day, cycle, phase)
  SUMMARY    — final session results (returns, sharpe, mdd)
  SNAPSHOT   — simulator handoff (positions + journal state)
  DAY        — per-day trading data (quant, research, decisions, signals)
  DAILY_STAT — per-day portfolio statistics (pv, returns, drawdown)
  CACHE      — pre-fetched external data (news, earnings)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class SessionStore(ABC):
    """Abstract data access layer for trading session storage."""

    # ------------------------------------------------------------------
    # Session metadata
    # ------------------------------------------------------------------

    @abstractmethod
    def save_meta(self, session_id: str, meta: dict) -> None:
        """Save session metadata (config, status, mode, dates)."""

    @abstractmethod
    def load_meta(self, session_id: str) -> dict | None:
        """Load session metadata. Returns None if not found."""

    @abstractmethod
    def update_status(self, session_id: str, status: str) -> None:
        """Update session status (running, completed, failed)."""

    @abstractmethod
    def list_sessions(self, user_id: str) -> list[dict]:
        """List all sessions for a user, sorted by creation time desc."""

    # ------------------------------------------------------------------
    # Portfolio state (mutable, updated each cycle)
    # ------------------------------------------------------------------

    @abstractmethod
    def save_state(self, session_id: str, state: dict) -> None:
        """Save current portfolio state (positions, cash, peak, watchlist)."""

    @abstractmethod
    def load_state(self, session_id: str) -> dict | None:
        """Load current portfolio state."""

    # ------------------------------------------------------------------
    # Progress (live execution tracking)
    # ------------------------------------------------------------------

    @abstractmethod
    def save_progress(self, session_id: str, progress: dict) -> None:
        """Save execution progress (current_day, cycle, phase)."""

    @abstractmethod
    def load_progress(self, session_id: str) -> dict | None:
        """Load execution progress."""

    # ------------------------------------------------------------------
    # Summary (final results, written once at session end)
    # ------------------------------------------------------------------

    @abstractmethod
    def save_summary(self, session_id: str, summary: dict) -> None:
        """Save session summary (returns, sharpe, mdd, spy comparison)."""

    @abstractmethod
    def load_summary(self, session_id: str) -> dict | None:
        """Load session summary."""

    # ------------------------------------------------------------------
    # Snapshot (simulator handoff, written once at precondition end)
    # ------------------------------------------------------------------

    @abstractmethod
    def save_snapshot(self, session_id: str, snapshot: dict) -> None:
        """Save simulator handoff snapshot."""

    @abstractmethod
    def load_snapshot(self, session_id: str) -> dict | None:
        """Load simulator handoff snapshot."""

    # ------------------------------------------------------------------
    # Cycle data (per-cycle trading pipeline results)
    # ------------------------------------------------------------------

    @abstractmethod
    def save_cycle(
        self, session_id: str, date: str, cycle_type: str, data: dict,
    ) -> None:
        """Save one cycle's results (EOD_SIGNAL, MORNING, INTRADAY, etc.)."""

    @abstractmethod
    def load_cycles(self, session_id: str, date: str) -> list[dict]:
        """Load all cycles for a given date, sorted by cycle order."""

    @abstractmethod
    def load_all_cycles(self, session_id: str) -> list[dict]:
        """Load all cycles for the entire session, sorted by date then cycle."""

    # Backwards-compatible day-level helpers (aggregate cycles into day view)

    def save_day(self, session_id: str, date: str, data: dict) -> None:
        """Legacy: save a full day as a single EOD_SIGNAL cycle."""
        self.save_cycle(session_id, date, "EOD_SIGNAL", data)

    def load_day(self, session_id: str, date: str) -> dict | None:
        """Legacy: load all cycles for a date, merged into one dict."""
        cycles = self.load_cycles(session_id, date)
        if not cycles:
            return None
        merged: dict = {}
        for c in cycles:
            merged.update(c)
        return merged

    # ------------------------------------------------------------------
    # Daily stats (per-day portfolio metrics for charting)
    # ------------------------------------------------------------------

    @abstractmethod
    def save_daily_stat(self, session_id: str, date: str, stat: dict) -> None:
        """Save daily portfolio statistics."""

    @abstractmethod
    def load_daily_stats(self, session_id: str) -> list[dict]:
        """Load all daily stats for a session, sorted by date."""

    # ------------------------------------------------------------------
    # Cache (pre-fetched external data — news, earnings)
    # ------------------------------------------------------------------

    @abstractmethod
    def save_cache(self, session_id: str, date: str, data: dict) -> None:
        """Save cached external data for a date."""

    @abstractmethod
    def load_cache(self, session_id: str, date: str) -> dict | None:
        """Load cached external data. Returns None if not cached."""

    # ------------------------------------------------------------------
    # Delete session
    # ------------------------------------------------------------------

    @abstractmethod
    def delete_session(self, session_id: str) -> None:
        """Delete all data for a session."""
