"""
tools/journal/research_log.py — Per-ticker research history.

All reads/writes go through the AgentState singleton (cycle_logs).

The ``submit_research`` tool is called by the LLM to submit research
for one ticker at a time. Results are buffered for system code to
consume via ``consume_research_buffer``, then persisted to cycle_logs
by ``save_research_results``.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from strands import tool


def _get_state():
    from state.agent_state import get_state
    return get_state()


# Global buffer keyed by ticker — thread-safe via lock.
# Previous thread-local buffer failed because Strands SDK may execute
# tools on a different thread (stream processing) than the caller.
_research_results: dict[str, dict] = {}
_results_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Tool — submit research (called by LLM, one ticker at a time)
# ---------------------------------------------------------------------------

@tool
def submit_research(research_json: str) -> str:
    """Submit research findings for ONE ticker.

    Call this ONCE per ticker after completing your analysis.

    The JSON object must include:
      - ticker (str): stock symbol
      - summary (str): 1-2 sentence key finding

    Optional fields:
      - risk_level (str): "none" (default) / "flag" (notable risk) / "veto" (hard stop — do not trade)
      - earnings_days (int or null): trading days until earnings (null if >14 days)
      - facts (list[str]): supporting evidence as bullet points

    Args:
        research_json: JSON object with research fields.

    Returns:
        Confirmation message.
    """
    try:
        data = json.loads(research_json)
    except json.JSONDecodeError as exc:
        return f"ERROR: invalid JSON — {exc}"

    if not isinstance(data, dict):
        return "ERROR: research_json must be a JSON object."

    ticker = str(data.get("ticker", "")).upper().strip()
    if not ticker:
        return "ERROR: ticker is required."

    with _results_lock:
        _research_results[ticker] = data
    logger.info("submit_research: research for %s buffered.", ticker)
    return f"Research for {ticker} recorded."


def consume_research_for(ticker: str) -> dict | None:
    """Pop and return the research result for a specific ticker.

    Thread-safe: uses global dict + lock instead of thread-local.
    Called by ResearchAnalystAgent._research_one() after each per-ticker run().
    """
    with _results_lock:
        return _research_results.pop(ticker.upper(), None)


def consume_research_buffer() -> dict[str, dict]:
    """Retrieve and clear ALL buffered research.

    Legacy API kept for backward compatibility (e.g. clearing stale data).
    Prefer consume_research_for(ticker) for per-ticker retrieval.
    """
    with _results_lock:
        result = dict(_research_results)
        _research_results.clear()
    return result


# ---------------------------------------------------------------------------
# Save (system code — persists research to AgentState.cycle_logs)
# ---------------------------------------------------------------------------

def save_research_results(
    results: dict[str, dict | None],
    cycle: str,
    sector_map: dict[str, str] | None = None,
    sim_date: str | None = None,
) -> None:
    """Save research results for multiple tickers as a cycle_log entry."""
    state = _get_state()
    date_str = sim_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sector_map = sector_map or {}

    # Build research dict: {ticker: enriched_record}
    research: dict[str, dict] = {}
    for ticker, result in results.items():
        if result is None:
            continue
        record = {
            "sector": sector_map.get(ticker, ""),
            **result,
        }
        research[ticker] = record

    if research:
        state.record_cycle(
            cycle_type=cycle,
            date=date_str,
            research=research,
        )
        state.save()


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_research_history(ticker: str, last_n: int = 3) -> list[dict]:
    """Load the most recent N research entries for a ticker (newest first)."""
    return _get_state().get_research_history(ticker, last_n)


def find_sector_peers_research(
    sector: str,
    exclude_ticker: str,
    last_n: int = 2,
) -> list[dict]:
    """Find recent research entries from same-sector tickers."""
    return _get_state().find_sector_peers_research(sector, exclude_ticker, last_n)


# ---------------------------------------------------------------------------
# Context builder (for research prompts)
# ---------------------------------------------------------------------------

def build_prior_context(
    tickers: list[str],
    last_n_research: int = 3,
    sector_map: dict[str, str] | None = None,
) -> str:
    """Build a prior-context block for research prompts.

    For each ticker, includes:
    - Recent research history (last N entries) for delta-focused analysis
    - Sector peer research if no ticker history exists

    Returns a formatted string ready for prompt injection,
    or empty string if no prior context exists.
    """
    state = _get_state()
    sector_map = sector_map or {}
    sections: list[str] = []

    for ticker in tickers:
        parts: list[str] = []

        # Prior research
        history = state.get_research_history(ticker, last_n=last_n_research)
        if history:
            parts.append("  Prior research:")
            for r in history:
                date = r.get("date", "?")
                cycle = r.get("cycle", "?")
                summary = (
                    r.get("summary")
                    or r.get("overnight_summary")
                    or "N/A"
                )
                risk_level = r.get("risk_level", "none")
                line = f"    [{date} {cycle}] {summary}"
                if risk_level and risk_level != "none":
                    line += f" | risk={risk_level}"
                parts.append(line)

        # Sector fallback (no prior research for this ticker)
        if not history:
            sector = sector_map.get(ticker, "")
            if sector:
                peers = state.find_sector_peers_research(sector, ticker, last_n=2)
                if peers:
                    parts.append(f"  No prior research. Sector peers ({sector}):")
                    for p in peers:
                        pticker = p.get("_ticker", "?")
                        date = p.get("date", "?")
                        summary = (
                            p.get("summary")
                            or p.get("overnight_summary")
                            or "N/A"
                        )
                        parts.append(f"    [{pticker} {date}] {summary}")

        if parts:
            sections.append(f"{ticker}:\n" + "\n".join(parts))

    if not sections:
        return ""

    return (
        "=== Prior Context ===\n"
        "Focus on what's CHANGED since the last research. "
        "Don't repeat known findings.\n\n"
        + "\n\n".join(sections)
        + "\n"
    )
