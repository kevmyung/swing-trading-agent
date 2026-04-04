"""
tools/journal/pm_notes.py — PM notes tool for cross-cycle memory.

The PM can write notes that persist across cycles. Notes are keyed by topic
(ticker symbol or general label like "regime_observation", "lesson").

Notes for a ticker are auto-deleted when that ticker's position is closed.
General notes persist until the PM explicitly deletes them.

All reads/writes go through AgentState.pm_notes.
"""

from __future__ import annotations

import json
import logging

from tools._compat import tool

logger = logging.getLogger(__name__)


def _get_state():
    from state.agent_state import get_state
    return get_state()


@tool
def update_notes(notes: dict) -> str:
    """Write or update persistent notes for future cycles.

    Use this to remember observations, plans, or lessons across cycles.
    Notes persist until you delete them or the system auto-cleans them.

    Key rules:
      - Ticker key (e.g. "AAPL"): use the bare ticker symbol. Auto-deleted
        when position is closed. Do NOT add suffixes (AAPL_plan, AAPL_entry).
        Put all info for a ticker in one note under "AAPL".
      - General key (e.g. "regime", "portfolio_construction", "lesson"):
        persists until you delete. Use sparingly.

    To delete a note, set its value to null or empty string.

    Args:
        notes: Dict of {key: note_text}. Key = bare ticker or general label.
               Set value to null or "" to delete that note.

    Returns:
        JSON with the full updated notes.

    Examples:
        update_notes({"AAPL": "ADD if pulls back to 175. Half-size entry, plan ADD on confirm.",
                       "regime": "TRANSITIONAL — favor MR over MOM"})
        update_notes({"AAPL": null})  # delete AAPL note
    """
    state = _get_state()
    as_of = getattr(state, 'trading_day', '') or ''
    updated = state.update_pm_notes(notes, as_of=as_of)
    state.save()
    logger.info("PM notes updated: %d total notes", len(updated))
    return json.dumps({"status": "ok", "notes": updated}, indent=2)


def load_pm_notes() -> dict[str, str]:
    """Load current PM notes (for prompt injection). Not a tool — called by system."""
    try:
        return dict(_get_state().pm_notes)
    except RuntimeError:
        return {}


def format_pm_notes_for_prompt(notes: dict, as_of: str = "") -> str:
    """Format PM notes as a readable section for the prompt.

    Notes are displayed in a flat list. Ticker-symbol keys (all uppercase,
    ≤5 chars) are shown as "TICK: note", other keys as "[key] note".
    Each note includes its age in days so PM can judge staleness.
    """
    if not notes:
        return ""

    from datetime import date

    today = date.fromisoformat(as_of) if as_of else date.today()

    lines = []
    for key, value in notes.items():
        if isinstance(value, dict):
            text = value.get('text', str(value))
            written = value.get('date', '')
        else:
            text = value
            written = ''

        age_str = ""
        if written:
            try:
                d = date.fromisoformat(written)
                age = (today - d).days
                if age > 0:
                    age_str = f" ({age}d ago)"
            except (ValueError, TypeError):
                pass

        is_ticker = key.isalpha() and key.isupper() and len(key) <= 5
        if is_ticker:
            lines.append(f"  {key}: {text}{age_str}")
        else:
            lines.append(f"  [{key}] {text}{age_str}")

    return "\n".join(lines)
