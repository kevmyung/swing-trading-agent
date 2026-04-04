"""
tools/sentiment/earnings.py — Earnings event screening.

Detects upcoming earnings dates (blackout enforcement) and identifies
post-earnings announcement drift (PEAD) opportunities from recent reports.
Uses yfinance with fixture-first loading for backtest performance.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import List

import pandas as pd

from tools._compat import tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _count_trading_days(start: date, end: date) -> int:
    """Count Mon–Fri days strictly between *start* (exclusive) and *end* (inclusive)."""
    if end <= start:
        return 0
    count = 0
    current = start + timedelta(days=1)
    while current <= end:
        if current.weekday() < 5:  # 0=Mon … 4=Fri
            count += 1
        current += timedelta(days=1)
    return count


def _pead_confidence(surprise_pct: float, days_since: int) -> str:
    """Return 'HIGH', 'MEDIUM', or 'LOW' confidence for a PEAD signal."""
    if surprise_pct >= 10.0 and days_since <= 1:
        return 'HIGH'
    elif surprise_pct >= 5.0 and days_since <= 3:
        return 'MEDIUM'
    return 'LOW'


def _to_float(value) -> float:
    """Safely convert a value to float; return 0.0 on failure."""
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Fixture loading (singleton)
# ---------------------------------------------------------------------------

_earnings_cache: dict[str, list[dict]] | None = None


def clear_earnings_cache() -> None:
    """Reset the earnings cache (call between days in long-running processes)."""
    global _earnings_cache
    _earnings_cache = None


def _load_earnings_fixture() -> dict[str, list[dict]]:
    """Load cached earnings data from fixture (singleton).

    Returns {ticker: [{date, eps_estimate, reported_eps, surprise_pct}, ...]}.
    Handles both new dict format and legacy string-list format.
    """
    global _earnings_cache
    if _earnings_cache is not None:
        return _earnings_cache

    import json
    from pathlib import Path

    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "backtest" / "fixtures" / "yfinance" / "earnings_dates.json"
    )
    if fixture_path.exists():
        try:
            with open(fixture_path) as f:
                raw = json.load(f)
            result: dict[str, list[dict]] = {}
            for ticker, entries in raw.items():
                if entries and isinstance(entries[0], str):
                    # Legacy format: ["2026-01-29", ...] — no EPS data
                    result[ticker] = [{"date": d} for d in entries]
                else:
                    result[ticker] = entries
            _earnings_cache = result
            logger.debug("Loaded earnings fixture: %d tickers", len(result))
            return result
        except Exception:
            pass
    _earnings_cache = {}
    return _earnings_cache


def _fetch_yfinance_earnings(ticker: str, n_entries: int = 12) -> list[dict]:
    """Fetch earnings dates from yfinance (live API fallback).

    Returns list of {date, eps_estimate, reported_eps, surprise_pct} dicts,
    newest first.
    """
    try:
        import yfinance as yf

        t = yf.Ticker(ticker)
        ed = t.earnings_dates
        if ed is None or ed.empty:
            return []

        result = []
        for dt_idx, row in ed.head(n_entries).iterrows():
            reported = row.get("Reported EPS")
            surprise = row.get("Surprise(%)")
            estimate = row.get("EPS Estimate")
            result.append({
                "date": dt_idx.date().isoformat(),
                "eps_estimate": round(float(estimate), 2) if pd.notna(estimate) else None,
                "reported_eps": round(float(reported), 2) if pd.notna(reported) else None,
                "surprise_pct": round(float(surprise), 2) if pd.notna(surprise) else None,
            })
        return result

    except Exception as exc:
        logger.debug("Failed to fetch earnings for %s: %s", ticker, exc)
        return []


def _get_ticker_earnings(ticker: str) -> list[dict]:
    """Get earnings entries for a ticker: fixture first, then yfinance."""
    fixture = _load_earnings_fixture()
    entries = fixture.get(ticker.upper())
    if entries is not None:
        return entries
    entries = _fetch_yfinance_earnings(ticker)
    time.sleep(0.2)
    return entries


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@tool
def screen_earnings_events(tickers: List[str], as_of: date | None = None) -> dict:
    """Screen a list of tickers for upcoming earnings and PEAD opportunities.

    Performs two scans per ticker using yfinance earnings data:

    1. **Blackout detection** — Identifies tickers reporting within the next
       2 trading days; these should NOT be entered.
    2. **PEAD detection** — Identifies tickers that reported within the last
       3 trading days with a positive earnings surprise > 5 %.

    Args:
        tickers: List of ticker symbols to screen.
        as_of: Reference date (default: today). Pass sim_date for backtesting.

    Returns:
        Dict with:
          - ``screened_at`` (str): ISO timestamp
          - ``screened_tickers`` (int): Number of tickers checked
          - ``blackout_tickers`` (list): Earnings within 2 trading days — do NOT enter.
            Each item: {ticker, earnings_date, days_until}
          - ``upcoming_earnings`` (list): Earnings within 7 trading days — use for held
            position context (PARTIAL_EXIT, stop tightening decisions).
            Each item: {ticker, earnings_date, days_until}
          - ``recent_earnings`` (list): Each item: {ticker, report_date, actual_eps,
            consensus_eps, surprise_pct, pead_signal, pead_confidence, suggested_action}
    """
    today = as_of or date.today()
    now = datetime.now(timezone.utc)

    result: dict = {
        'screened_at': now.isoformat(),
        'screened_tickers': len(tickers),
        'blackout_tickers': [],
        'upcoming_earnings': [],
        'recent_earnings': [],
    }

    for ticker in tickers:
        try:
            entries = _get_ticker_earnings(ticker)
            if not entries:
                continue

            found_upcoming = False
            found_recent = False

            for entry in entries:
                if found_upcoming and found_recent:
                    break

                entry_date_str = entry.get("date", "")
                try:
                    entry_date = date.fromisoformat(entry_date_str)
                except (ValueError, TypeError):
                    continue

                reported_eps = entry.get("reported_eps")

                # Upcoming: date >= today and not yet reported (or no data)
                if not found_upcoming and entry_date >= today:
                    # In fixture with full data: reported_eps is None for future
                    # In legacy fixture: no reported_eps key at all, but all dates
                    # are past, so entry_date >= today won't match
                    days_until = _count_trading_days(today, entry_date)
                    if 0 <= days_until <= 7:
                        upcoming = {
                            'ticker': ticker,
                            'earnings_date': entry_date.isoformat(),
                            'days_until': days_until,
                        }
                        result['upcoming_earnings'].append(upcoming)
                        if days_until <= 2:
                            result['blackout_tickers'].append(upcoming)
                        found_upcoming = True

                # Recent: reported within last 5 trading days with surprise data
                elif not found_recent and entry_date < today and reported_eps is not None:
                    surprise_pct = _to_float(entry.get("surprise_pct"))
                    days_since = _count_trading_days(entry_date, today)
                    if days_since <= 5:
                        pead_signal = surprise_pct > 5.0 and days_since <= 3
                        confidence = _pead_confidence(surprise_pct, days_since) if pead_signal else 'LOW'
                        if pead_signal:
                            suggested_action = 'BUY_OPEN_TOMORROW'
                        elif surprise_pct > 5.0:
                            suggested_action = 'MONITOR'
                        else:
                            suggested_action = 'NONE'

                        result['recent_earnings'].append({
                            'ticker': ticker,
                            'report_date': entry_date.isoformat(),
                            'actual_eps': float(reported_eps),
                            'consensus_eps': _to_float(entry.get("eps_estimate")),
                            'surprise_pct': round(surprise_pct, 2),
                            'pead_signal': pead_signal,
                            'pead_confidence': confidence,
                            'suggested_action': suggested_action,
                        })
                        found_recent = True

        except Exception as exc:
            logger.warning("Failed to screen earnings for %s: %s", ticker, exc)

    return result
