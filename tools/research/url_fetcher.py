"""
tools/research/url_fetcher.py — URL content fetcher tool.

Fetches and extracts readable text from a web page URL.
Used by ResearchAnalystAgent to read full articles found via web search.

Uses ``httpx`` for HTTP and ``beautifulsoup4`` for HTML parsing.
"""

from __future__ import annotations

import logging

from tools._compat import tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False
    BeautifulSoup = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_CONTENT_LENGTH = 8000  # chars — truncate to keep LLM context manageable
_REQUEST_TIMEOUT = 15       # seconds
_USER_AGENT = (
    "Mozilla/5.0 (compatible; TradingResearchBot/1.0; "
    "+https://github.com/aws-samples)"
)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@tool
def fetch_url(url: str) -> str:
    """Fetch and extract readable text content from a web page URL.

    Use this to read full articles, press releases, or analyst reports
    discovered via the web_search tool.

    Args:
        url: Full URL to fetch (e.g. "https://www.reuters.com/article/...").

    Returns:
        JSON string with ``url``, ``title``, and ``content`` fields.
        Content is truncated to ~8000 characters to fit LLM context.
    """
    import json

    if not _HTTPX_AVAILABLE:
        return json.dumps({
            "error": "httpx not installed. Run: pip install httpx",
            "url": url, "title": "", "content": "",
        })
    if not _BS4_AVAILABLE:
        return json.dumps({
            "error": "beautifulsoup4 not installed. Run: pip install beautifulsoup4",
            "url": url, "title": "", "content": "",
        })

    try:
        resp = httpx.get(
            url,
            timeout=_REQUEST_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.error("URL fetch failed for '%s': %s", url, exc)
        return json.dumps({"error": str(exc), "url": url, "title": "", "content": ""})

    try:
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove noise elements
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        title = soup.title.string.strip() if soup.title and soup.title.string else ""

        # Extract main content — prefer <article> or <main>, fall back to <body>
        main = soup.find("article") or soup.find("main") or soup.find("body")
        if main is None:
            content = soup.get_text(separator="\n", strip=True)
        else:
            content = main.get_text(separator="\n", strip=True)

        # Collapse multiple blank lines
        lines = [line for line in content.splitlines() if line.strip()]
        content = "\n".join(lines)

        if len(content) > _MAX_CONTENT_LENGTH:
            content = content[:_MAX_CONTENT_LENGTH] + "\n\n[... truncated]"

    except Exception as exc:
        logger.error("HTML parsing failed for '%s': %s", url, exc)
        return json.dumps({"error": str(exc), "url": url, "title": "", "content": ""})

    logger.info("URL fetch: '%s' → %d chars.", url, len(content))
    return json.dumps({"url": url, "title": title, "content": content})
