"""store/session_id.py — Session ID generation and parsing.

Format: {mode}-{YYYYMMDD}T{HHmmss}-{random4}
  e.g.  bt-20260105T143022-a7k2
        sim-20260203T091500-m3p8
        live-20260311T163000-x1n4

Display: "Backtest 2026-01-05 14:30:22"
"""

from __future__ import annotations

import random
import string
from datetime import datetime, timezone


_MODE_PREFIXES = {
    "backtest": "bt",
    "simulate": "sim",
    "live": "live",
}

_PREFIX_TO_LABEL = {
    "bt": "Backtest",
    "sim": "Simulation",
    "live": "Live",
}


def generate_session_id(mode: str = "backtest") -> str:
    """Generate a datetime-based session ID.

    Args:
        mode: "backtest", "simulate", or "live"

    Returns:
        Session ID like "bt-20260105T143022-a7k2"
    """
    prefix = _MODE_PREFIXES.get(mode, mode[:4])
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S")
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{prefix}-{ts}-{rand}"


def parse_session_id(session_id: str) -> dict:
    """Parse a session ID into its components.

    Returns:
        Dict with keys: mode, prefix, timestamp, display_name
    """
    parts = session_id.split("-", 1)
    prefix = parts[0] if parts else ""
    label = _PREFIX_TO_LABEL.get(prefix, prefix.upper())

    # Try to parse timestamp from the rest
    ts_str = ""
    if len(parts) > 1:
        # bt-20260105T143022-a7k2 → 20260105T143022-a7k2
        rest = parts[1]
        ts_part = rest.rsplit("-", 1)[0] if "-" in rest else rest
        try:
            dt = datetime.strptime(ts_part, "%Y%m%dT%H%M%S")
            ts_str = dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            ts_str = rest

    return {
        "mode": prefix,
        "prefix": prefix,
        "timestamp": ts_str,
        "display_name": f"{label} {ts_str}" if ts_str else label,
    }
