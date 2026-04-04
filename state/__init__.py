"""
state — Portfolio and agent state management package.

AgentState extends PortfolioState with watchlist, research logs, and
decision logs. All runtime state lives in one object, persisted to a
single JSON file.
"""

from .portfolio_state import PortfolioState, Position, Trade
from .agent_state import AgentState, get_state, set_state

__all__ = ["PortfolioState", "Position", "Trade", "AgentState", "get_state", "set_state"]
