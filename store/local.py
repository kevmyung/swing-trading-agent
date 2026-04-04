"""store/local.py — Local filesystem store (JSON files).

Reads and writes session data to backtest/sessions/{session_id}/.
This is the default store for local development and testing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from store.base import SessionStore

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path("backtest/sessions")


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def _load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


class LocalStore(SessionStore):
    """JSON file-based session storage under backtest/sessions/."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else SESSIONS_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, session_id: str) -> Path:
        p = self.base_dir / session_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ------------------------------------------------------------------
    # Session metadata
    # ------------------------------------------------------------------

    def save_meta(self, session_id: str, meta: dict) -> None:
        path = self._session_path(session_id) / "meta.json"
        _save_json(path, meta)

    def load_meta(self, session_id: str) -> dict | None:
        return _load_json(self._session_path(session_id) / "meta.json")

    def update_status(self, session_id: str, status: str) -> None:
        meta = self.load_meta(session_id) or {}
        meta["status"] = status
        self.save_meta(session_id, meta)

    def list_sessions(self, user_id: str) -> list[dict]:
        sessions = []
        if not self.base_dir.exists():
            return sessions
        for d in sorted(self.base_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            meta = _load_json(d / "meta.json")
            if meta is None:
                # Legacy session without meta.json — build minimal entry
                summary = _load_json(d / "summary.json")
                if summary:
                    meta = {
                        "session_id": d.name,
                        "status": "completed",
                        **{k: summary[k] for k in (
                            "start_date", "end_date", "sim_days",
                            "total_return_pct", "start_value", "end_value",
                        ) if k in summary},
                    }
                else:
                    meta = {"session_id": d.name, "status": "unknown"}
            if user_id and meta.get("user_id") and meta["user_id"] != user_id:
                continue
            meta.setdefault("session_id", d.name)
            sessions.append(meta)
        return sessions

    # ------------------------------------------------------------------
    # Portfolio state
    # ------------------------------------------------------------------

    def save_state(self, session_id: str, state: dict) -> None:
        _save_json(self._session_path(session_id) / "agent_state.json", state)

    def load_state(self, session_id: str) -> dict | None:
        return _load_json(self._session_path(session_id) / "agent_state.json")

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------

    def save_progress(self, session_id: str, progress: dict) -> None:
        _save_json(self._session_path(session_id) / "progress.json", progress)

    def load_progress(self, session_id: str) -> dict | None:
        return _load_json(self._session_path(session_id) / "progress.json")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def save_summary(self, session_id: str, summary: dict) -> None:
        _save_json(self._session_path(session_id) / "summary.json", summary)

    def load_summary(self, session_id: str) -> dict | None:
        return _load_json(self._session_path(session_id) / "summary.json")

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def save_snapshot(self, session_id: str, snapshot: dict) -> None:
        _save_json(self._session_path(session_id) / "snapshot.json", snapshot)

    def load_snapshot(self, session_id: str) -> dict | None:
        return _load_json(self._session_path(session_id) / "snapshot.json")

    # ------------------------------------------------------------------
    # Cycle data
    # ------------------------------------------------------------------

    _CYCLE_ORDER = {"EOD_SIGNAL": 0, "MORNING": 1, "INTRADAY": 2}

    def save_cycle(
        self, session_id: str, date: str, cycle_type: str, data: dict,
    ) -> None:
        cycles_dir = self._session_path(session_id) / "cycles"
        enriched = {**data, "date": date, "cycle_type": cycle_type}
        _save_json(cycles_dir / f"{date}_{cycle_type}.json", enriched)

    def load_cycles(self, session_id: str, date: str) -> list[dict]:
        cycles_dir = self._session_path(session_id) / "cycles"
        if not cycles_dir.exists():
            # Fallback: try legacy day file
            legacy = _load_json(
                self._session_path(session_id) / "days" / f"day_{date}.json"
            )
            return [legacy] if legacy else []
        cycles = []
        for f in sorted(cycles_dir.glob(f"{date}_*.json")):
            data = _load_json(f)
            if data:
                cycles.append(data)
        return sorted(
            cycles,
            key=lambda c: self._CYCLE_ORDER.get(c.get("cycle_type", ""), 99),
        )

    def load_all_cycles(self, session_id: str) -> list[dict]:
        cycles_dir = self._session_path(session_id) / "cycles"
        if not cycles_dir.exists():
            # Fallback: load all legacy day files
            days_dir = self._session_path(session_id) / "days"
            if not days_dir.exists():
                return []
            cycles = []
            for f in sorted(days_dir.glob("day_*.json")):
                data = _load_json(f)
                if data:
                    cycles.append(data)
            return cycles
        cycles = []
        for f in sorted(cycles_dir.glob("*.json")):
            data = _load_json(f)
            if data:
                cycles.append(data)
        return sorted(
            cycles,
            key=lambda c: (
                c.get("date", ""),
                self._CYCLE_ORDER.get(c.get("cycle_type", ""), 99),
            ),
        )

    # ------------------------------------------------------------------
    # Daily stats
    # ------------------------------------------------------------------

    def save_daily_stat(self, session_id: str, date: str, stat: dict) -> None:
        stats_dir = self._session_path(session_id) / "daily_stats"
        _save_json(stats_dir / f"{date}.json", stat)

    def load_daily_stats(self, session_id: str) -> list[dict]:
        stats_dir = self._session_path(session_id) / "daily_stats"
        if not stats_dir.exists():
            # Fallback: read from agent_state.json (legacy format)
            state = self.load_state(session_id)
            if state and "daily_stats" in state:
                return state["daily_stats"]
            return []
        stats = []
        for f in sorted(stats_dir.glob("*.json")):
            data = _load_json(f)
            if data:
                stats.append(data)
        return stats

    # ------------------------------------------------------------------
    # Cache (pre-fetched news/earnings)
    # ------------------------------------------------------------------

    def save_cache(self, session_id: str, date: str, data: dict) -> None:
        cache_dir = self._session_path(session_id) / "preconditioned"
        _save_json(cache_dir / f"day_{date}.json", data)

    def load_cache(self, session_id: str, date: str) -> dict | None:
        return _load_json(
            self._session_path(session_id) / "preconditioned" / f"day_{date}.json"
        )

    # ------------------------------------------------------------------
    # Delete session
    # ------------------------------------------------------------------

    def delete_session(self, session_id: str) -> None:
        import shutil
        session_path = self.base_dir / session_id
        if session_path.is_dir():
            shutil.rmtree(session_path)
