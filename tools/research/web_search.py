"""
tools/research/web_search.py — DuckDuckGo web search tool.

Provides a synchronous web search capability for the ResearchAnalystAgent
to proactively find overnight news, macro events, and sector developments.

Uses the ``duckduckgo-search`` library (``pip install duckduckgo-search``).
"""

from __future__ import annotations

import logging
from typing import List

from tools._compat import tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency
# ---------------------------------------------------------------------------

try:
    from duckduckgo_search import DDGS
    _DDGS_AVAILABLE = True
except ImportError:
    _DDGS_AVAILABLE = False
    DDGS = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web using DuckDuckGo and return summarised results.

    Useful for finding overnight news, macro events, earnings surprises,
    sector developments, and analyst commentary.

    Args:
        query: Search query string (e.g. "AAPL earnings Q4 2025",
               "Fed interest rate decision March 2026",
               "semiconductor sector outlook").
        max_results: Maximum number of results to return (default 5, max 10).

    Returns:
        JSON string with a list of search results, each containing
        ``title``, ``url``, and ``snippet`` fields.
    """
    import json

    if not _DDGS_AVAILABLE:
        return json.dumps({
            "error": "duckduckgo-search not installed. Run: pip install duckduckgo-search",
            "results": [],
        })

    max_results = min(max(1, max_results), 10)

    try:
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(query, max_results=max_results))
    except Exception as exc:
        logger.error("Web search failed for query '%s': %s", query, exc)
        return json.dumps({"error": str(exc), "results": []})

    results: List[dict] = []
    for r in raw_results:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("href", r.get("link", "")),
            "snippet": r.get("body", r.get("snippet", "")),
        })

    logger.info("Web search: '%s' → %d results.", query, len(results))
    return json.dumps({"query": query, "results": results})
