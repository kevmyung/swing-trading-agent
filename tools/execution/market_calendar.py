"""
tools/execution/market_calendar.py — Market calendar utilities using Alpaca API.

Provides a simple check for whether the US stock market is open today,
used by schedulers to skip cycles on holidays (e.g. Good Friday, MLK Day).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

try:
    from alpaca.trading.client import TradingClient
    _ALPACA_AVAILABLE = True
except ImportError:
    _ALPACA_AVAILABLE = False
    TradingClient = None  # type: ignore[assignment,misc]


def _get_trading_client():
    """Return a TradingClient instance using credentials from settings."""
    from config.settings import get_settings
    s = get_settings()
    return TradingClient(
        api_key=s.alpaca_api_key,
        secret_key=s.alpaca_secret_key,
        paper=s.alpaca_paper,
    )


def is_market_open_today() -> bool:
    """Check if the US stock market is open today using Alpaca's clock API.

    Returns True if today is a regular trading day (market will open or is open).
    Returns True as a fallback if Alpaca is unavailable (fail-open).
    """
    if not _ALPACA_AVAILABLE:
        logger.warning("alpaca-py not installed — skipping market calendar check (assuming open).")
        return True

    try:
        client = _get_trading_client()
        clock = client.get_clock()

        # clock.is_open: True if market is currently open
        # clock.next_open / clock.next_close: timestamps for next open/close
        if clock.is_open:
            return True

        # Market is currently closed. Check if it opens today.
        # If next_open is today (ET), the market hasn't opened yet but will.
        # If next_open is a future date, today is a holiday or weekend.
        now_et = datetime.now(clock.next_open.tzinfo)
        today = now_et.date()
        next_open_date = clock.next_open.date()

        if next_open_date == today:
            # Market will open later today
            return True

        # next_open is a future date — today is not a trading day
        logger.info(
            "Market closed today (%s). Next open: %s.",
            today.isoformat(), clock.next_open.isoformat(),
        )
        return False

    except Exception as exc:
        # Fail-open: if we can't check, assume market is open
        logger.warning("Market calendar check failed (%s) — assuming open.", exc)
        return True
