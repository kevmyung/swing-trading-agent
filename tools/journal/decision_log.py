"""
tools/journal/decision_log.py — Decision logging via tool call.

All reads go through the AgentState singleton (cycle_logs).

The PortfolioAgent submits cycle decisions by calling a cycle-specific
submit tool (submit_eod_decisions, submit_morning_decisions, or
submit_intraday_decisions). Decisions are validated and buffered for
system code to consume. Persistence happens upstream via
``AgentState.record_cycle(decisions=...)``.

The ``read_decision_logs`` tool lets the LLM review its own past reasoning.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

from strands import tool


def _get_state():
    from state.agent_state import get_state
    return get_state()


# Cycle-scoped buffer — written by submit tools, read by consume_cycle_decisions
_cycle_decision_buffer: list[dict] = []
_cycle_submitted: bool = False  # guards against double-submit

# Expected tickers for the current cycle — set by the prompt builder so
# submit tools can report which tickers are still missing decisions.
_cycle_expected_tickers: set[str] = set()
_cycle_watchlist_tickers: set[str] = set()


def set_cycle_expected_tickers(
    positions: list[str],
    candidates: list[str],
    watchlist_tickers: list[str] | None = None,
) -> None:
    """Register which tickers the PM is expected to decide on this cycle."""
    global _cycle_expected_tickers, _cycle_watchlist_tickers
    _cycle_expected_tickers = {t.upper() for t in positions} | {t.upper() for t in candidates}
    _cycle_watchlist_tickers = {t.upper() for t in (watchlist_tickers or [])}


def _coverage_report() -> str:
    """Build a coverage summary showing pending tickers, highlighting WATCH items."""
    if not _cycle_expected_tickers:
        return ""
    submitted = {rec['ticker'] for rec in _cycle_decision_buffer}
    missing = sorted(_cycle_expected_tickers - submitted)
    if not missing:
        return f"\n\nAll {len(_cycle_expected_tickers)} tickers covered."
    watch_missing = sorted(set(missing) & _cycle_watchlist_tickers)
    other_missing = sorted(set(missing) - _cycle_watchlist_tickers)
    parts = [f"\n\nPENDING — {len(missing)} tickers still need decisions:"]
    if watch_missing:
        parts.append(f"  WATCH (must review): {', '.join(watch_missing)}")
    if other_missing:
        parts.append(f"  Candidates: {', '.join(other_missing)}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Valid actions per cycle
# ---------------------------------------------------------------------------

_EOD_ACTIONS = {"HOLD", "EXIT", "PARTIAL_EXIT", "TIGHTEN", "LONG", "WATCH"}
_MORNING_ACTIONS = {"CONFIRM", "REJECT", "ADJUST", "EXIT", "HOLD"}
_INTRADAY_ACTIONS = {"HOLD", "EXIT", "PARTIAL_EXIT", "TIGHTEN"}


# ---------------------------------------------------------------------------
# Shared internal — validate and buffer
# ---------------------------------------------------------------------------

def _save_decisions(
    decisions_json: str,
    valid_actions: set[str],
    notes_json: str | None = None,
) -> str:
    """Validate decisions and buffer for system code to consume.

    Persistence to AgentState.cycle_logs happens upstream via record_cycle().
    If notes_json is provided, writes them to pm_notes atomically.
    """
    global _cycle_decision_buffer, _cycle_submitted

    try:
        decisions = json.loads(decisions_json)
    except json.JSONDecodeError as exc:
        return f"ERROR: invalid JSON — {exc}"

    if not isinstance(decisions, list):
        return "ERROR: decisions_json must be a JSON array."

    validated: list[dict] = []

    for dec in decisions:
        ticker = str(dec.get("ticker", "")).upper().strip()
        action = str(dec.get("action", "")).upper().strip()
        if not ticker:
            continue
        if action not in valid_actions:
            logger.warning("submit_decisions: unknown action '%s' for %s", action, ticker)
            continue

        record = {
            "ticker": ticker,
            "action": action,
            "conviction": dec.get("conviction", ""),
            "for": dec.get("for", ""),
            "against": dec.get("against", ""),
            "entry_type": dec.get("entry_type"),
            "limit_price": dec.get("limit_price"),
            "half_size": dec.get("half_size"),
            "new_stop_loss": dec.get("new_stop_loss"),
            "adjusted_limit_price": dec.get("adjusted_limit_price"),
            "playbook_ref": dec.get("playbook_ref"),
            "playbook_gap": dec.get("playbook_gap"),
        }
        # Remove None values to keep records compact
        record = {k: v for k, v in record.items() if v is not None}
        # Strip playbook fields when playbook is disabled
        from config.settings import get_settings
        if not get_settings().enable_playbook:
            record.pop("playbook_ref", None)
            record.pop("playbook_gap", None)
        validated.append(record)

        # WATCH → add/keep on watchlist; SKIP → remove from watchlist
        if action == "WATCH":
            try:
                from tools.journal.watchlist import add_to_watchlist
                add_to_watchlist(ticker, dec.get("for", ""))
            except Exception as exc:
                logger.warning("Failed to add %s to watchlist: %s", ticker, exc)
        elif action == "SKIP":
            try:
                from tools.journal.watchlist import remove_from_watchlist
                remove_from_watchlist(ticker)
            except Exception as exc:
                pass  # not on watchlist — fine

    # Deduplicate: keep only the last decision per ticker
    seen: dict[str, int] = {}
    dup_tickers: list[str] = []
    for i, rec in enumerate(validated):
        t = rec["ticker"]
        if t in seen:
            dup_tickers.append(t)
        seen[t] = i
    if dup_tickers:
        logger.warning("submit_decisions: duplicate ticker(s) removed: %s", dup_tickers)
        validated = [validated[i] for i in sorted(seen.values())]

    # Save PM notes atomically with decisions
    notes_saved = 0
    notes_dict: dict = {}
    if notes_json:
        try:
            notes_dict = json.loads(notes_json) if isinstance(notes_json, str) else notes_json
            if isinstance(notes_dict, dict) and notes_dict:
                state = _get_state()
                # Use sim_date (trading_day) so backtest notes get correct dates
                as_of = getattr(state, 'trading_day', '') or ''
                state.update_pm_notes(notes_dict, as_of=as_of)
                state.save()
                notes_saved = len(notes_dict)
                logger.info("submit_decisions: %d PM notes saved.", notes_saved)
        except Exception as exc:
            logger.warning("submit_decisions: notes save failed: %s", exc)

    # Check for held/entered positions missing notes
    _NEEDS_NOTE = {"HOLD", "TIGHTEN", "LONG", "WATCH"}
    notes_keys_upper = {k.upper() for k in notes_dict} if isinstance(notes_dict, dict) else set()
    missing_notes = [
        rec["ticker"] for rec in validated
        if rec["action"] in _NEEDS_NOTE and rec["ticker"] not in notes_keys_upper
    ]

    # Append to buffer (supports multiple submit calls per cycle).
    # Dedup across calls: new decisions overwrite earlier ones for the same ticker.
    _cycle_decision_buffer.extend(validated)
    existing: dict[str, int] = {}
    for i, rec in enumerate(_cycle_decision_buffer):
        existing[rec["ticker"]] = i
    _cycle_decision_buffer = [_cycle_decision_buffer[i] for i in sorted(existing.values())]
    _cycle_submitted = True
    logger.info("submit_decisions: %d decisions buffered (total).", len(_cycle_decision_buffer))

    # Build concise summary for LLM
    summary_parts = [f"{rec['ticker']}={rec['action']}" for rec in validated]
    total = len(_cycle_decision_buffer)
    summary = f"Recorded {len(validated)} decisions: {', '.join(summary_parts)} ({total} total)."
    if dup_tickers:
        summary += f" (duplicates overwritten: {', '.join(dup_tickers)})"
    if notes_saved:
        summary += f" {notes_saved} notes saved."
    if missing_notes:
        summary += f"\n\nWARNING: Missing notes for {', '.join(missing_notes)}. Resubmit notes for these tickers."

    summary += _coverage_report()
    return summary


# ---------------------------------------------------------------------------
# Cycle-specific submit tools (called by LLM)
# ---------------------------------------------------------------------------

@tool
def submit_eod_decisions(decisions_json: str, notes: str = "") -> str:
    """Submit all EOD cycle decisions as a JSON array. Call ONCE after analysis.

    Include positions (HOLD/EXIT/PARTIAL_EXIT/TIGHTEN) and
    positive candidate actions (LONG/WATCH) here. Use submit_skips() separately
    for candidates you're passing on.

    Every decision must include (write in this order):
      - ticker (str)
      - for (str): evidence supporting this decision
      - against (str): key risk or failure scenario
      - conviction (str: high/medium/low): classify Against as thesis-level or sizing-level first
      - action (str): chosen AFTER determining conviction

    Action-specific fields:
      - TIGHTEN: signals thesis concern — system automatically tightens trailing stop (no stop price needed)
      - PARTIAL_EXIT: system sells half of remaining shares (no extra fields needed)
      - LONG: always MARKET order at next open. No entry_type or limit_price needed.
        half_size (bool, optional — true = enter half indicative_shares for scaled entry)

    Optional for all actions:
      - playbook_ref: playbook path that guided this decision (e.g. "entry/momentum_setup")
      - playbook_gap: if this scenario is not covered by the playbook, describe what's missing in a short phrase

    Args:
        decisions_json: JSON array of decision objects.
        notes: JSON object of PM notes. Required for every HOLD/TIGHTEN/LONG/WATCH ticker.
               Ticker keys auto-delete when position closes. Set value to null to delete a note.

    Returns:
        Confirmation message with count.
    """
    return _save_decisions(decisions_json, _EOD_ACTIONS, notes_json=notes or None)


@tool
def submit_skips(skips_json: str) -> str:
    """Submit skipped candidates separately from main decisions.

    Lightweight format — just say why you're passing, not a full for/against.

    Args:
        skips_json: JSON array of skip objects, each with:
          - ticker (str): symbol
          - reason (str): ~10-word skip reason — why you're passing, not what's good about it
          - playbook_ref (str, optional): playbook path consulted

    Returns:
        Confirmation message with count.
    """
    global _cycle_decision_buffer

    try:
        skips = json.loads(skips_json)
    except json.JSONDecodeError as exc:
        return f"ERROR: invalid JSON — {exc}"

    if not isinstance(skips, list):
        return "ERROR: skips_json must be a JSON array."

    added = 0
    for s in skips:
        ticker = str(s.get("ticker", "")).upper().strip()
        if not ticker:
            continue
        reason = str(s.get("reason", "")).strip()
        record: dict = {
            "ticker": ticker,
            "action": "SKIP",
            "reason": reason,
        }
        from config.settings import get_settings
        if get_settings().enable_playbook:
            pr = s.get("playbook_ref")
            if pr:
                record["playbook_ref"] = pr

        # Check if this ticker was on the watchlist before buffering
        was_watched = False
        try:
            from tools.journal.watchlist import load_watchlist, remove_from_watchlist
            was_watched = any(w["ticker"] == ticker for w in load_watchlist())
            if was_watched:
                remove_from_watchlist(ticker)
                record["from_watchlist"] = True
        except Exception:
            pass

        # Clean up PM note for watchlist SKIPs (thesis is abandoned)
        if was_watched:
            try:
                from state.agent_state import get_state
                state = get_state()
                if ticker in state.pm_notes:
                    state.update_pm_notes({ticker: None})
                    state.save()
            except Exception:
                pass

        _cycle_decision_buffer.append(record)
        added += 1

    logger.info("submit_skips: %d skips buffered.", added)

    summary = f"Recorded {added} skips."
    summary += _coverage_report()
    return summary


@tool
def submit_morning_decisions(decisions_json: str) -> str:
    """Submit all morning cycle decisions as a JSON array. Call ONCE after analysis.

    Every decision must include:
      - ticker (str), action (str), conviction (str)
      - for (str): evidence supporting this decision
      - against (str): evidence against or risks

    Entry candidates: CONFIRM / REJECT / ADJUST
      - ADJUST requires adjusted_limit_price (float): converts to LIMIT order.
        Stop and sizing are recalculated from the limit price automatically.

    Exit review (deferred positions): EXIT / HOLD
      - These are positions where overnight research conflicts with the EOD decision.

    Args:
        decisions_json: JSON array of decision objects.

    Returns:
        Confirmation message with count.
    """
    return _save_decisions(decisions_json, _MORNING_ACTIONS)


@tool
def submit_intraday_decisions(decisions_json: str) -> str:
    """Submit all intraday cycle decisions as a JSON array. Call ONCE after analysis.

    Every decision must include:
      - ticker (str), action (str: HOLD/TIGHTEN/EXIT/PARTIAL_EXIT), conviction (str)
      - for (str): evidence supporting this decision
      - against (str): evidence against or risks

    TIGHTEN: signals thesis concern — system tightens trailing stop automatically.
    PARTIAL_EXIT sells half automatically. Stop management is handled by the system.

    Optional for all actions:
      - playbook_ref: playbook section that guided this decision (e.g. "intraday/flag_response")
      - playbook_gap: note if the playbook lacked clear guidance (omit if well-covered)

    Args:
        decisions_json: JSON array of decision objects.

    Returns:
        Confirmation message with count.
    """
    return _save_decisions(decisions_json, _INTRADAY_ACTIONS)


# ---------------------------------------------------------------------------
# Backward-compat alias (used by tests and scripts)
# ---------------------------------------------------------------------------

def submit_decisions(decisions_json: str) -> str:
    """Submit decisions with all actions valid. For test/script use only."""
    all_actions = _EOD_ACTIONS | _MORNING_ACTIONS | _INTRADAY_ACTIONS | {"SKIP"}
    return _save_decisions(decisions_json, all_actions)


# ---------------------------------------------------------------------------
# Fallback — rescue decisions from LLM text when tool calls weren't made
# ---------------------------------------------------------------------------

import re as _re

_ALL_KNOWN_ACTIONS = {"HOLD", "EXIT", "PARTIAL_EXIT", "TIGHTEN", "LONG",
                      "WATCH", "SKIP", "CONFIRM", "REJECT", "ADJUST"}


def try_rescue_from_text(llm_text: str) -> bool:
    """Parse decisions from LLM text output when the model wrote tool calls as text.

    Some models (e.g. Nova) emit tool-call syntax as markdown text instead of
    actually invoking tools. This function detects that scenario and feeds
    extracted decisions through the normal ``_save_decisions`` pipeline.

    Call this BEFORE ``consume_cycle_decisions`` when ``_cycle_submitted`` is False.

    Returns True if decisions were rescued (buffer now has real decisions).
    """
    global _cycle_submitted, _cycle_decision_buffer
    if _cycle_submitted:
        return False  # tool was called normally — no rescue needed

    if not llm_text or not isinstance(llm_text, str):
        return False

    # Extract JSON arrays from markdown code blocks or raw text
    rescued_decisions: list[dict] = []
    rescued_skips: list[dict] = []

    # Find all JSON arrays in the text (greedy within code blocks, lazy otherwise)
    # Pattern: look for ```json ... ``` blocks first, then bare [...] arrays
    code_blocks = _re.findall(r'```(?:json)?\s*(\[[\s\S]*?\])\s*```', llm_text)
    if not code_blocks:
        # Fallback: find bare JSON arrays (be careful with nested brackets)
        code_blocks = []
        for m in _re.finditer(r'\[[\s\S]{10,}?\](?=\s*(?:```|$|\n\n))', llm_text):
            code_blocks.append(m.group(0))

    for block in code_blocks:
        try:
            arr = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(arr, list):
            continue
        for item in arr:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get('ticker', '')).upper().strip()
            action = str(item.get('action', '')).upper().strip()
            if not ticker:
                continue
            if action in _ALL_KNOWN_ACTIONS:
                if action == 'SKIP':
                    rescued_skips.append(item)
                else:
                    rescued_decisions.append(item)
            elif item.get('reason') and not action:
                # Skip entries without explicit action
                rescued_skips.append({**item, 'action': 'SKIP'})

    if not rescued_decisions and not rescued_skips:
        return False

    # Also try to extract notes JSON (object, not array)
    notes_json: str | None = None
    notes_blocks = _re.findall(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', llm_text)
    for nb in notes_blocks:
        try:
            obj = json.loads(nb)
            if isinstance(obj, dict) and all(isinstance(v, str) or v is None for v in obj.values()):
                notes_json = nb
                break
        except json.JSONDecodeError:
            continue

    count_before = len(_cycle_decision_buffer)

    # Feed decisions through the normal pipeline
    if rescued_decisions:
        decisions_str = json.dumps(rescued_decisions)
        _save_decisions(decisions_str, _ALL_KNOWN_ACTIONS, notes_json=notes_json)

    # Feed skips directly into buffer (avoid circular call through @tool wrapper)
    for s in rescued_skips:
        ticker = str(s.get('ticker', '')).upper().strip()
        if not ticker:
            continue
        _cycle_decision_buffer.append({
            "ticker": ticker,
            "action": "SKIP",
            "reason": str(s.get('reason', '')).strip(),
            **({"playbook_ref": s["playbook_ref"]} if s.get("playbook_ref") else {}),
        })

    # Dedup entire buffer (LLM text often contains duplicate JSON blocks)
    seen: dict[str, int] = {}
    for i, rec in enumerate(_cycle_decision_buffer):
        seen[rec["ticker"]] = i
    _cycle_decision_buffer = [_cycle_decision_buffer[i] for i in sorted(seen.values())]

    rescued_count = len(_cycle_decision_buffer) - count_before
    if rescued_count > 0:
        logger.warning(
            "try_rescue_from_text: rescued %d decisions from LLM text output "
            "(model did not call tools properly).", rescued_count,
        )
        return True
    return False


# ---------------------------------------------------------------------------
# System code interface — consume buffered decisions
# ---------------------------------------------------------------------------

def consume_cycle_decisions() -> list[dict]:
    """Retrieve and clear the decisions submitted by the LLM this cycle.

    Called by PortfolioAgent after ``self.run()`` to get the decisions
    that were submitted via the cycle-specific submit tool call.

    Any expected tickers that the PM did not address are auto-filled as
    implicit SKIPs so that downstream systems (blackout, decision_log)
    treat them consistently.

    Returns:
        List of decision dicts, or empty list if no tool call was made.
    """
    global _cycle_decision_buffer, _cycle_submitted, _cycle_expected_tickers, _cycle_watchlist_tickers

    # Auto-fill missing tickers as implicit SKIP
    if _cycle_expected_tickers:
        submitted = {rec['ticker'] for rec in _cycle_decision_buffer}
        missing = sorted(_cycle_expected_tickers - submitted)
        if missing:
            logger.warning(
                "consume_cycle_decisions: %d tickers not addressed by PM, "
                "auto-filling as SKIP: %s", len(missing), missing,
            )
            for ticker in missing:
                was_watched = ticker in _cycle_watchlist_tickers
                _cycle_decision_buffer.append({
                    "ticker": ticker,
                    "action": "SKIP",
                    "reason": "not addressed by PM this cycle",
                    "implicit": True,
                    **({"from_watchlist": True} if was_watched else {}),
                })
                # Treat same as explicit SKIP: clean up watchlist + note
                if was_watched:
                    try:
                        from tools.journal.watchlist import remove_from_watchlist
                        remove_from_watchlist(ticker)
                    except Exception:
                        pass
                    try:
                        from state.agent_state import get_state
                        state = get_state()
                        if ticker in state.pm_notes:
                            state.update_pm_notes({ticker: None})
                            state.save()
                    except Exception:
                        pass

    result = _cycle_decision_buffer
    _cycle_decision_buffer = []
    _cycle_submitted = False
    _cycle_expected_tickers = set()
    _cycle_watchlist_tickers = set()
    return result


# ---------------------------------------------------------------------------
# Tool — read past decisions
# ---------------------------------------------------------------------------

@tool
def read_decision_logs(
    tickers: str,
    last_n: int = 3,
) -> str:
    """Read past decision logs for one or more tickers in a single call.

    Returns the most recent decisions (action, conviction, for/against)
    the portfolio manager made for each ticker. Use this to recall
    prior reasoning before making new decisions.

    Args:
        tickers: Comma-separated stock symbols (e.g. "AAPL" or "AAPL,MSFT,NVDA").
        last_n: Number of most recent decisions per ticker (default 3, max 10).

    Returns:
        JSON with per-ticker decision summaries (newest first).
    """
    last_n = min(max(1, last_n), 10)
    state = _get_state()
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    result: dict = {}
    for ticker in ticker_list:
        records = state.get_decision_history(ticker, last_n)
        if records:
            # Filter out SKIP — recorded for audit but not useful for LLM
            # reasoning (blackout handles re-screening).
            # WATCH is kept: "why I was interested" context helps next decision.
            filtered = [
                {k: r[k] for k in ("date", "action", "conviction", "for", "against", "playbook_ref", "playbook_gap") if k in r}
                for r in records
                if r.get("action", "").upper() != "SKIP"
            ]
            result[ticker] = filtered
        else:
            result[ticker] = []
    return json.dumps(result)
