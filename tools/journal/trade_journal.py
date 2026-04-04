"""
tools/journal/trade_journal.py — Trading journal for recording decision rationale.

Stores per-ticker JSONL files under ``data/journal/``.  Each line is one
timestamped note capturing why an action was taken, what factors mattered,
and any lessons learned.  The PortfolioAgent reads past notes before making
buy/sell decisions to maintain consistency and learn from prior trades.

Registered as direct @tool functions on the PortfolioAgent.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

from strands import tool

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

JOURNAL_DIR = os.path.join("data", "journal")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def write_trade_note(
    ticker: str,
    action: str,
    rationale: str,
    key_factors: str,
    cycle: str = "",
    price: float = 0.0,
    target_price: float = 0.0,
    stop_price: float = 0.0,
    conviction: str = "MEDIUM",
    lesson: str = "",
) -> str:
    """Write a trade note to the journal for a specific ticker.

    Call this whenever you make a meaningful decision about a position —
    entries, exits, holds with updated thesis, stop adjustments, or
    when you learn something that should inform future trades on this ticker.

    Args:
        ticker: Stock symbol (e.g. "AAPL").
        action: What was decided — e.g. "LONG_ENTRY", "EXIT", "HOLD",
                "TIGHTEN", "SKIP", "REJECT", "SIGNAL_APPROVED".
        rationale: 1-3 sentence explanation of WHY this decision was made.
                   Think like a trader writing in their journal at end of day.
        key_factors: Comma-separated list of the most important factors
                     (e.g. "momentum 0.85, PEAD detected, sector rotation, Fed dovish").
        cycle: Which cycle produced this note (e.g. "EOD_SIGNAL", "MORNING", "INTRADAY").
        price: Price at time of decision (0.0 if unknown).
        target_price: Price target for this trade (0.0 if not applicable).
                      For entries: where you expect the move to go.
                      For SKIPs: price level where you would reconsider entering.
        stop_price: Stop-loss price (0.0 if not applicable).
                    For entries: planned stop level.
                    For TIGHTEN: the new stop level.
        conviction: "HIGH", "MEDIUM", or "LOW".
        lesson: Optional hindsight or forward-looking note
                (e.g. "should have sized smaller given earnings proximity").

    Returns:
        JSON confirmation with the saved note id.
    """
    os.makedirs(JOURNAL_DIR, exist_ok=True)

    now = datetime.now(timezone.utc)
    note = {
        "id": now.strftime("%Y%m%d_%H%M%S"),
        "ticker": ticker.upper().strip(),
        "date": now.strftime("%Y-%m-%d"),
        "time_utc": now.strftime("%H:%M:%S"),
        "cycle": cycle,
        "action": action,
        "price": price,
        "target_price": target_price or None,
        "stop_price": stop_price or None,
        "rationale": rationale,
        "key_factors": [f.strip() for f in key_factors.split(",") if f.strip()],
        "conviction": conviction.upper(),
        "lesson": lesson or None,
    }

    file_path = os.path.join(JOURNAL_DIR, f"{note['ticker']}.jsonl")
    with open(file_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(note, ensure_ascii=False) + "\n")

    logger.info("Journal note saved: %s %s %s", note["ticker"], action, note["id"])
    return json.dumps({"status": "saved", "id": note["id"], "ticker": note["ticker"]})


@tool
def read_trade_notes(
    ticker: str,
    last_n: int = 10,
) -> str:
    """Read recent trade journal notes for a specific ticker.

    Call this BEFORE making a buy or sell decision to review your past
    reasoning, lessons, and conviction levels for this ticker.

    Args:
        ticker: Stock symbol to look up (e.g. "AAPL").
        last_n: Number of most recent notes to return (default 10, max 30).

    Returns:
        JSON with the list of notes (newest first), or an empty list
        if no notes exist for this ticker.
    """
    ticker = ticker.upper().strip()
    last_n = min(max(1, last_n), 30)
    file_path = os.path.join(JOURNAL_DIR, f"{ticker}.jsonl")

    if not os.path.exists(file_path):
        return json.dumps({"ticker": ticker, "notes": [], "count": 0})

    notes = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    notes.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    # newest first, limited to last_n
    notes = notes[-last_n:][::-1]

    return json.dumps({"ticker": ticker, "notes": notes, "count": len(notes)})
