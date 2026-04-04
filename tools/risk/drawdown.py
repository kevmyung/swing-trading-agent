"""
tools/risk/drawdown.py — Portfolio drawdown monitoring tool for the RiskAgent.

Tracks peak-to-trough portfolio drawdown and returns appropriate position
size multipliers and trading permission status.
"""

from __future__ import annotations

import logging
from typing import List

import numpy as np
import pandas as pd


logger = logging.getLogger(__name__)


def check_drawdown(
    current_value: float,
    peak_value: float,
    max_drawdown_pct: float = 0.15,
) -> dict:
    """
    Check current drawdown vs MDD limit.

    Returns:
        Dict: {current_drawdown_pct, mdd_limit_pct, status, position_size_multiplier, allow_new_trades}
        status: 'NORMAL' (0-5%), 'CAUTION' (5-10%), 'WARNING' (10-15%), 'HALT' (>15%)
        position_size_multiplier: 1.0 / 0.75 / 0.5 / 0.0
    """
    if peak_value <= 0:
        return {
            'current_drawdown_pct': 0.0,
            'mdd_limit_pct': max_drawdown_pct,
            'status': 'NORMAL',
            'position_size_multiplier': 1.0,
            'allow_new_trades': True,
        }

    drawdown = max(0.0, (peak_value - current_value) / peak_value)

    if drawdown >= max_drawdown_pct:
        status = 'HALT'
        multiplier = 0.0
        allow_new_trades = False
    elif drawdown >= 0.10:
        status = 'WARNING'
        multiplier = 0.5
        allow_new_trades = True
    elif drawdown >= 0.05:
        status = 'CAUTION'
        multiplier = 0.75
        allow_new_trades = True
    else:
        status = 'NORMAL'
        multiplier = 1.0
        allow_new_trades = True

    return {
        'current_drawdown_pct': round(drawdown, 4),
        'mdd_limit_pct': max_drawdown_pct,
        'status': status,
        'position_size_multiplier': multiplier,
        'allow_new_trades': allow_new_trades,
    }


