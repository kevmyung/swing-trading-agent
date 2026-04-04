"""
tools/quant — Quantitative signal generation tools.

Modules:
  momentum.py        → momentum scoring
  mean_reversion.py  → mean-reversion signals
  market_regime.py   → regime classification
  technical.py       → RSI, ATR, MACD, Bollinger, ADX
"""

from .market_regime import classify_market_regime
from .mean_reversion import calculate_mean_reversion_signals
from .momentum import calculate_momentum_scores, get_momentum_ic
from .technical import calculate_technical_indicators

__all__ = [
    "calculate_momentum_scores",
    "get_momentum_ic",
    "calculate_mean_reversion_signals",
    "classify_market_regime",
    "calculate_technical_indicators",
]
