"""
tools/sentiment/news.py — News sentiment analysis tool.

Live/paper trading: fetches headlines from yfinance (free, no API key).
Backtesting: uses Polygon.io cached fixtures (date-range historical queries).

The LLM (Research Agent) performs qualitative sentiment interpretation on the
headlines — no pre-computed sentiment scores for yfinance articles.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List

from tools._compat import tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Article cache — populated by fetch_and_score_news, read by read_cached_article
# ---------------------------------------------------------------------------

_article_cache: dict[str, list[dict]] = {}  # ticker → full article list


def get_article_cache() -> dict[str, list[dict]]:
    """Return the current article cache (for external access)."""
    return _article_cache


def clear_article_cache() -> None:
    """Clear the article cache between cycles."""
    _article_cache.clear()


@tool
def read_article(ticker: str, index: int) -> str:
    """Read the full content of a news article.

    After reviewing the headline list in your prompt, call this to read
    the full details of articles you find interesting.

    Args:
        ticker: Stock symbol (e.g. "AAPL").
        index: 0-based index from the headline list in your prompt.

    Returns:
        JSON with the article's title, published date, description,
        source, and URL.  For Polygon-sourced articles (backtesting),
        also includes sentiment and sentiment_reasoning.
    """
    import json
    ticker = ticker.upper().strip()
    articles = _article_cache.get(ticker, [])
    if not articles:
        return json.dumps({"error": f"No cached articles for {ticker}."})
    if index < 0 or index >= len(articles):
        return json.dumps({"error": f"Index {index} out of range (0–{len(articles)-1})."})

    article = articles[index]

    result = {
        "title": article.get('title', ''),
        "published_utc": article.get('published_utc', ''),
        "description": article.get('description', ''),
        "source": article.get('source', '') or (article.get('publisher') or {}).get('name', ''),
        "article_url": article.get('article_url', ''),
    }

    # Polygon articles have insights with per-ticker sentiment
    insights = article.get('insights') or []
    if insights:
        ticker_insight = next(
            (i for i in insights if (i.get('ticker') or '').upper() == ticker),
            None,
        )
        result["sentiment"] = (ticker_insight or {}).get('sentiment', 'neutral')
        result["sentiment_reasoning"] = (ticker_insight or {}).get('sentiment_reasoning', '')

    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VETO_KEYWORDS = [
    'sec investigation',
    'bankruptcy',
    'fraud',
    'restatement',
    'delisting',
    'class action',
    'accounting irregularity',
]

_KEY_EVENT_PATTERNS: dict[str, list[str]] = {
    'earnings_beat': ['earnings beat', 'beat estimate', 'beat consensus',
                      'topped estimate', 'surpassed estimate', 'beat expectations'],
    'earnings_miss': ['earnings miss', 'missed estimate', 'below estimate',
                      'missed consensus', 'missed expectations'],
    'guidance_raised': ['raised guidance', 'raises guidance', 'raised outlook',
                        'raises forecast', 'raised forecast'],
    'guidance_cut': ['cut guidance', 'cuts guidance', 'lowered guidance',
                     'lowered outlook', 'lowered forecast'],
    'dividend_increase': ['raised dividend', 'raises dividend', 'increased dividend',
                          'dividend increase'],
    'buyback': ['share buyback', 'stock repurchase', 'buyback program',
                'repurchase program'],
    'merger': ['merger', 'acquisition', 'acquires', 'takeover bid'],
    'lawsuit': ['lawsuit', 'legal action', 'litigation', 'sued'],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _recency_weight(age_hours: float) -> float:
    """Return decay weight for an article of the given age in hours."""
    if age_hours < 2:
        return 1.0
    elif age_hours < 6:
        return 0.7
    elif age_hours < 24:
        return 0.4
    else:
        return 0.15


def _sentiment_score(sentiment_str: str) -> float:
    """Map a Polygon.io sentiment string to a numeric score."""
    mapping = {'positive': 1.0, 'neutral': 0.0, 'negative': -1.0}
    return mapping.get((sentiment_str or 'neutral').lower(), 0.0)


def _check_veto_keywords(text: str) -> bool:
    """Return True if *text* contains any trade-veto keyword."""
    lower = (text or '').lower()
    return any(kw in lower for kw in VETO_KEYWORDS)


def _detect_key_events(headlines: List[str]) -> List[str]:
    """Return sorted list of detected event types from headline text."""
    combined = ' '.join(headlines).lower()
    return sorted(
        event
        for event, patterns in _KEY_EVENT_PATTERNS.items()
        if any(p in combined for p in patterns)
    )


def _fetch_yfinance_news(ticker: str, count: int = 20) -> list[dict]:
    """Fetch recent news for a ticker via yfinance (free, no API key).

    Returns articles normalised to a common format:
        {title, published_utc, description, article_url, source}
    """
    import yfinance as yf

    t = yf.Ticker(ticker)
    raw_items = t.get_news(count=count) or []

    articles = []
    for item in raw_items:
        content = item.get('content') or item  # v2 wraps in 'content'
        pub_date = content.get('pubDate', '') or content.get('published_utc', '')
        provider = content.get('provider') or {}
        summary = content.get('summary', '') or content.get('description', '') or ''

        articles.append({
            'title': content.get('title', ''),
            'published_utc': pub_date,
            'description': summary,
            'article_url': (content.get('canonicalUrl') or content.get('clickThroughUrl') or {}).get('url', ''),
            'source': provider.get('displayName', ''),
        })
    return articles


def _fetch_polygon_news(
    ticker: str, hours_back: int, api_key: str, as_of: datetime | None = None,
) -> list:
    """Call Polygon.io /v2/reference/news and return raw article list.

    Used for backtesting with date-range queries. Live trading uses yfinance.

    Args:
        as_of: If provided, fetch news as of this timestamp (for backtesting).
               Uses ``as_of`` as the upper bound and ``as_of - hours_back`` as lower.
    """
    import requests

    reference_time = as_of or datetime.now(timezone.utc)
    since = reference_time - timedelta(hours=hours_back)
    params = {
        'ticker': ticker,
        'published_utc.gte': since.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'limit': 20,
        'sort': 'published_utc',
        'order': 'desc',
        'apiKey': api_key,
    }
    if as_of is not None:
        params['published_utc.lte'] = reference_time.strftime('%Y-%m-%dT%H:%M:%SZ')
    resp = requests.get(
        'https://api.polygon.io/v2/reference/news',
        params=params,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get('results', [])


def _score_articles(
    articles: list,
    ticker: str,
    now: datetime,
) -> tuple[float, bool, str, List[str], list]:
    """
    Score a list of articles for *ticker*.

    Works with both Polygon articles (have ``insights[].sentiment``) and
    yfinance articles (no pre-computed sentiment — score defaults to neutral).

    Returns:
        (composite_sentiment, veto_trade, top_headline, key_events, raw_articles)
    """
    scored: list[tuple[float, float]] = []
    veto = False
    headlines: List[str] = []

    for article in articles:
        title = article.get('title', '')
        description = article.get('description', '') or ''
        published = article.get('published_utc', '')
        insights = article.get('insights') or []

        # Polygon articles have per-ticker sentiment; yfinance articles don't
        if insights:
            ticker_insight = next(
                (i for i in insights if (i.get('ticker') or '').upper() == ticker.upper()),
                None,
            )
            sentiment_str = (ticker_insight or {}).get('sentiment', 'neutral')
            score = _sentiment_score(sentiment_str)
        else:
            # yfinance: no pre-computed sentiment — default neutral,
            # LLM does qualitative interpretation
            score = 0.0

        try:
            pub_dt = datetime.fromisoformat(published.replace('Z', '+00:00'))
            age_hours = (now - pub_dt).total_seconds() / 3600.0
        except (ValueError, AttributeError):
            age_hours = 999.0

        weight = _recency_weight(age_hours)
        scored.append((score, weight))
        headlines.append(title)

        if _check_veto_keywords(title) or _check_veto_keywords(description):
            veto = True

    # Weighted composite
    total_weight = sum(w for _, w in scored)
    composite = (
        sum(s * w for s, w in scored) / total_weight
        if total_weight > 0
        else 0.0
    )
    if composite < -0.5:
        veto = True

    top_headline = headlines[0] if headlines else ''
    key_events = _detect_key_events(headlines)

    # Top-3 raw articles
    raw_articles = []
    for article in articles[:3]:
        entry: dict = {
            'title': article.get('title', ''),
            'published_utc': article.get('published_utc', ''),
        }
        insights = article.get('insights') or []
        if insights:
            ticker_insight = next(
                (i for i in insights if (i.get('ticker') or '').upper() == ticker.upper()),
                None,
            )
            entry['sentiment'] = (ticker_insight or {}).get('sentiment', 'neutral')
            entry['sentiment_reasoning'] = (ticker_insight or {}).get('sentiment_reasoning', '')
        raw_articles.append(entry)

    return round(composite, 4), veto, top_headline, key_events, raw_articles


def _neutral_result() -> dict:
    """Return a neutral, empty result dict for one ticker."""
    return {
        'composite_sentiment': 0.0,
        'article_count': 0,
        'veto_trade': False,
        'top_headline': '',
        'key_events': [],
        'raw_articles': [],
    }


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

@tool
def fetch_and_score_news(tickers: List[str], hours_back: int = 24, as_of: str = "") -> dict:
    """Fetch recent news articles and calculate a sentiment score per ticker.

    Live/paper trading: uses yfinance (free, no API key). The LLM interprets
    sentiment from headlines directly.

    Backtesting (``as_of`` set): uses Polygon.io for date-range historical
    queries with pre-computed sentiment.

    A ``veto_trade=True`` flag is set when the composite score drops below
    -0.50 or any headline/description contains a high-severity keyword
    (e.g. 'SEC investigation', 'bankruptcy', 'fraud').

    Args:
        tickers: List of ticker symbols to fetch news for.
        hours_back: How many hours of historical news to retrieve (default 24).
        as_of: ISO date/datetime string for backtesting (e.g. "2026-02-02").
               When set, uses Polygon instead of yfinance.

    Returns:
        Dict keyed by ticker symbol, each containing:
          - ``composite_sentiment`` (float): Weighted score −1.0 … +1.0
          - ``article_count`` (int): Number of articles found
          - ``veto_trade`` (bool): True if negative news should block trade entry
          - ``top_headline`` (str): Most recent headline
          - ``key_events`` (list[str]): Detected event labels
          - ``raw_articles`` (list[dict]): Top-3 articles with title, published_utc
        Top-level ``fetched_at`` (str): ISO timestamp of the fetch.
    """
    # Parse as_of — if set, use Polygon for historical backtest
    as_of_dt: datetime | None = None
    if as_of:
        try:
            as_of_dt = datetime.fromisoformat(as_of.replace('Z', '+00:00'))
            if as_of_dt.tzinfo is None:
                as_of_dt = as_of_dt.replace(hour=21, tzinfo=timezone.utc)
        except ValueError:
            logger.warning("Invalid as_of '%s', using current time.", as_of)

    use_polygon = as_of_dt is not None
    now = as_of_dt or datetime.now(timezone.utc)
    result: dict = {'fetched_at': now.isoformat()}

    if use_polygon:
        from config.settings import get_settings
        api_key = get_settings().polygon_api_key
        if not api_key:
            logger.warning("POLYGON_API_KEY not configured — neutral sentiment for backtest.")
            for ticker in tickers:
                result[ticker] = _neutral_result()
            return result

    for ticker in tickers:
        try:
            if use_polygon:
                articles = _fetch_polygon_news(ticker, hours_back, api_key, as_of=as_of_dt)
            else:
                articles = _fetch_yfinance_news(ticker, count=20)

            _article_cache[ticker.upper()] = articles
            composite, veto, top_headline, key_events, raw_articles = _score_articles(
                articles, ticker, now
            )
            entry = {
                'article_count': len(articles),
                'veto_trade': veto,
                'top_headline': top_headline,
                'key_events': key_events,
                'raw_articles': raw_articles,
            }
            # Polygon: include pre-computed composite score
            # yfinance: None signals "LLM must judge sentiment from headlines"
            entry['composite_sentiment'] = composite if use_polygon else None
            result[ticker] = entry
        except Exception as exc:
            logger.warning("Failed to fetch news for %s: %s", ticker, exc)
            result[ticker] = _neutral_result()

        time.sleep(0.1)

    return result


# ---------------------------------------------------------------------------
# Fixture helpers — store raw articles, score at read time with time window
# ---------------------------------------------------------------------------

def compact_articles(articles: list) -> list[dict]:
    """Extract cacheable fields from raw Polygon articles.

    Keeps only the fields needed for scoring and LLM display.
    Typically 5-9 articles per ticker per 24h window.
    """
    compacted = []
    for a in articles:
        insights = a.get('insights') or []
        compacted.append({
            'title': a.get('title', ''),
            'published_utc': a.get('published_utc', ''),
            'description': a.get('description', ''),
            'article_url': a.get('article_url', ''),
            'source': (a.get('publisher') or {}).get('name', ''),
            'insights': [
                {
                    'ticker': ins.get('ticker', ''),
                    'sentiment': ins.get('sentiment', 'neutral'),
                    'sentiment_reasoning': ins.get('sentiment_reasoning', ''),
                }
                for ins in insights
            ],
        })
    return compacted


def score_news_for_window(
    articles_by_ticker: dict[str, list[dict]],
    reference_time: datetime,
    window_start: datetime | None = None,
) -> dict:
    """Score cached articles filtered by a time window.

    Args:
        articles_by_ticker: {ticker: [article_dicts]} from cache fixture.
        reference_time: Upper bound for published_utc (also used as 'now' for
            recency weighting).
        window_start: Lower bound for published_utc. If None, all articles
            before reference_time are included.

    Returns:
        Scored news_data dict in the same format as fetch_and_score_news
        (composite_sentiment, article_count, veto_trade, etc.).
        Also populates _article_cache so the read_article tool works.
    """
    result: dict = {'fetched_at': reference_time.isoformat()}

    for ticker, articles in articles_by_ticker.items():
        # Filter by time window
        filtered = []
        for a in articles:
            pub = a.get('published_utc', '')
            if not pub:
                continue
            try:
                pub_dt = datetime.fromisoformat(pub.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                continue
            if pub_dt > reference_time:
                continue
            if window_start and pub_dt < window_start:
                continue
            filtered.append(a)

        # Populate article cache for read_article tool
        _article_cache[ticker.upper()] = filtered

        if not filtered:
            result[ticker] = _neutral_result()
            continue

        composite, veto, top_headline, key_events, raw_articles = _score_articles(
            filtered, ticker, reference_time,
        )
        result[ticker] = {
            'composite_sentiment': composite,
            'article_count': len(filtered),
            'veto_trade': veto,
            'top_headline': top_headline,
            'key_events': key_events,
            'raw_articles': raw_articles,
        }

    return result
