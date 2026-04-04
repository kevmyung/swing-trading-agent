"""
tools/sentiment — Sentiment and qualitative analysis tools.

Modules:
  news.py      → news fetching and scoring
  earnings.py  → earnings event screening
"""

from .earnings import screen_earnings_events
from .news import fetch_and_score_news

__all__ = [
    "fetch_and_score_news",
    "screen_earnings_events",
]
