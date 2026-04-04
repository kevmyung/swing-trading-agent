#!/usr/bin/env python
"""
backtest/fixtures/refresh_news.py — Download Polygon news articles for backtesting.

Fetches all articles per trading day and organizes them by ticker.
Stores compact article data with published_utc for time-window scoring.

Usage:
    python backtest/fixtures/refresh_news.py
    python backtest/fixtures/refresh_news.py --months 3
    python backtest/fixtures/refresh_news.py --start 2025-10-01 --end 2026-03-11

Output:
    backtest/fixtures/polygon/news/day_YYYY-MM-DD.json
    Each file: {ticker: [{title, published_utc, description, ...}, ...]}

Polygon Starter plan: unlimited API calls, 5 calls/min for free tier.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURES_DIR = Path(__file__).parent
NEWS_DIR = FIXTURES_DIR / "polygon" / "news"


def _load_universe() -> set[str]:
    """Load S&P 500 + ETF universe for filtering."""
    sp500_path = FIXTURES_DIR / "wikipedia" / "sp500_tickers.json"
    if sp500_path.exists():
        with open(sp500_path) as f:
            tickers = json.load(f)
    else:
        tickers = []
    # Add index/breadth ETFs
    etfs = ["SPY", "QQQ", "RSP", "IWM", "HYG", "TLT",
            "XLK", "XLF", "XLV", "XLE", "XLI", "XLC", "XLY", "XLP", "XLB", "XLRE", "XLU"]
    return {t.upper() for t in tickers + etfs}


def _get_trading_days(start: str, end: str) -> list[str]:
    """Get trading days from daily bars fixture or generate weekdays."""
    bars_path = FIXTURES_DIR / "alpaca" / "daily_bars.json"
    if bars_path.exists():
        with open(bars_path) as f:
            bars = json.load(f)
        spy = bars.get("SPY", {})
        all_dates = sorted(spy.keys())
        return [d for d in all_dates if start <= d <= end]

    # Fallback: weekdays
    import pandas as pd
    dates = pd.bdate_range(start, end)
    return [d.strftime("%Y-%m-%d") for d in dates]


def _fetch_day_articles(
    date_str: str, api_key: str, universe: set[str],
) -> dict[str, list[dict]]:
    """Fetch all articles for one trading day, filter to universe tickers.

    Uses date range prev_day 16:00 ET (21:00 UTC) to current_day 21:00 UTC
    to capture overnight + market hours news.
    """
    import requests
    from tools.sentiment.news import compact_articles

    # Window: previous day 21:00 UTC to this day 21:00 UTC (~24h)
    day = datetime.strptime(date_str, "%Y-%m-%d")
    end_utc = day.replace(hour=21, tzinfo=timezone.utc)
    start_utc = end_utc - timedelta(hours=24)

    all_articles: list[dict] = []
    url = "https://api.polygon.io/v2/reference/news"
    params = {
        "published_utc.gte": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_utc.lte": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 1000,
        "sort": "published_utc",
        "order": "desc",
        "apiKey": api_key,
    }

    # Paginate
    page = 1
    while True:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        all_articles.extend(results)

        next_url = data.get("next_url")
        if not next_url or not results:
            break

        # next_url already contains cursor, just append apiKey
        url = next_url
        params = {"apiKey": api_key}
        page += 1
        time.sleep(0.15)

    # Organize by ticker, filter to universe
    by_ticker: dict[str, list[dict]] = {}
    for article in all_articles:
        article_tickers = article.get("tickers", [])
        for t in article_tickers:
            t_upper = t.upper()
            if t_upper in universe:
                by_ticker.setdefault(t_upper, []).append(article)

    # Compact articles (remove unnecessary fields)
    return {t: compact_articles(arts) for t, arts in by_ticker.items()}


def refresh_news(
    start_date: str,
    end_date: str,
    skip_existing: bool = True,
) -> None:
    """Download news articles for all trading days in range."""
    from config.settings import get_settings

    api_key = get_settings().polygon_api_key
    if not api_key:
        print("ERROR: POLYGON_API_KEY not set in .env")
        return

    universe = _load_universe()
    print(f"Universe: {len(universe)} tickers")

    trading_days = _get_trading_days(start_date, end_date)
    print(f"Trading days: {len(trading_days)} ({trading_days[0]} -> {trading_days[-1]})")

    NEWS_DIR.mkdir(parents=True, exist_ok=True)

    skipped = 0
    fetched = 0
    total_articles = 0

    for i, date_str in enumerate(trading_days):
        out_path = NEWS_DIR / f"day_{date_str}.json"

        if skip_existing and out_path.exists():
            skipped += 1
            continue

        articles = _fetch_day_articles(date_str, api_key, universe)
        day_total = sum(len(arts) for arts in articles.values())
        tickers_with_news = len(articles)

        with open(out_path, "w") as f:
            json.dump(articles, f, indent=2, default=str)

        fetched += 1
        total_articles += day_total
        print(f"  [{i+1}/{len(trading_days)}] {date_str}: "
              f"{tickers_with_news} tickers, {day_total} articles")

        # Rate limit courtesy (Polygon free: 5/min)
        time.sleep(0.2)

    print(f"\nDone: {fetched} days fetched, {skipped} skipped (existing)")
    print(f"Total articles: {total_articles}")
    print(f"Saved to: {NEWS_DIR}/")


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Download Polygon news articles for backtesting",
    )
    parser.add_argument("--months", type=int, default=6,
                        help="Months of history to download (default: 6)")
    parser.add_argument("--start", type=str, default=None,
                        help="Start date (YYYY-MM-DD), overrides --months")
    parser.add_argument("--end", type=str, default=None,
                        help="End date (YYYY-MM-DD, default: yesterday)")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if file exists")
    args = parser.parse_args()

    end_date = args.end or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if args.start:
        start_date = args.start
    else:
        start_date = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=args.months * 30)).strftime("%Y-%m-%d")

    print(f"=== Polygon News Download: {start_date} -> {end_date} ===")
    refresh_news(start_date, end_date, skip_existing=not args.force)


if __name__ == "__main__":
    main()
