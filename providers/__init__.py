"""
providers/ — Swappable data and broker backends for the trading pipeline.

DataProvider ABC + implementations:
  FixtureProvider  — JSON fixtures + cached news (backtest)
  LiveProvider     — Alpaca + Polygon + Finnhub (live)

Broker ABC + implementations:
  MockBroker       — Simulation with price updates, stop-loss, gap checks
  AlpacaBroker     — Wraps alpaca_orders.py + portfolio_sync.py
"""

from .data_provider import DataProvider
from .broker import Broker
from .mock_broker import MockBroker
from .fixture_provider import FixtureProvider
from .live_provider import LiveProvider
from .live_broker import AlpacaBroker

__all__ = [
    "DataProvider",
    "Broker",
    "MockBroker",
    "FixtureProvider",
    "LiveProvider",
    "AlpacaBroker",
]
