"""
agents — Trading system agent and engine package.

Components:
- PortfolioAgent:         Sole LLM decision-maker; final portfolio judgment.
- QuantEngine:            Pure-Python numerical signal engine (no LLM). Computes
                          technical, portfolio, and market indicators deterministically.
- ResearchAnalystAgent:   LLM-based qualitative analysis (news, earnings, trade implications).
"""

from agents.base_agent import BaseAgent
from agents.quant_engine import QuantEngine
from agents.research_analyst_agent import ResearchAnalystAgent
from agents.portfolio_agent import PortfolioAgent

__all__ = [
    "BaseAgent",
    "QuantEngine",
    "ResearchAnalystAgent",
    "PortfolioAgent",
]
