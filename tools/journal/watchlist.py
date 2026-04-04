"""
tools/journal/watchlist.py — Watchlist helpers.

All reads/writes go through the AgentState singleton. No file-level globals.

Watchlist tickers are always included in EOD review even if the screener doesn't
pick them up. Watchlist management is handled automatically by submit_eod_decisions
(WATCH → add, SKIP → remove).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def _get_state():
    from state.agent_state import get_state
    return get_state()


def add_to_watchlist(ticker: str, reason: str = "") -> None:
    """Add a ticker to watchlist if not already present."""
    from config.settings import get_settings
    max_size = get_settings().watchlist_max_size

    ticker = ticker.upper().strip()
    state = _get_state()

    if any(w["ticker"] == ticker for w in state.watchlist):
        return
    if len(state.watchlist) >= max_size:
        logger.warning("Watchlist full (%d) — cannot add %s", max_size, ticker)
        return
    state.watchlist_add(ticker, reason)
    state.save()
    logger.info("Watchlist ADD: %s (total: %d)", ticker, len(state.watchlist))


def load_watchlist() -> list[dict]:
    """Return current watchlist entries. Not a tool — for internal prompt injection."""
    return _get_state().watchlist


def remove_from_watchlist(ticker: str) -> bool:
    """Remove a ticker from watchlist if present. Returns True if removed."""
    state = _get_state()
    removed = state.watchlist_remove(ticker)
    if removed:
        state.save()
        logger.info("Watchlist auto-remove: %s (entered position)", ticker)
    return removed
