#!/usr/bin/env python
"""
backtest/fixtures/refresh.py — Re-fetch all API fixtures from live endpoints.

Requires valid API keys in .env. Run from the project root:

    python backtest/fixtures/refresh.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

FIXTURES_DIR = Path(__file__).parent


def _save(relative_path: str, data: object) -> None:
    path = FIXTURES_DIR / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    size = path.stat().st_size
    print(f"  Saved: {relative_path} ({size:,} bytes)")


# ---------------------------------------------------------------------------
# Symbol universe
# ---------------------------------------------------------------------------

# Indices and breadth ETFs (always included)
INDEX_AND_BREADTH: list[str] = [
    "SPY", "QQQ",
    # Breadth proxies
    "RSP", "IWM", "HYG", "TLT",
    # GICS sector ETFs
    "XLK", "XLF", "XLV", "XLE", "XLI", "XLC", "XLY", "XLP", "XLB", "XLRE", "XLU",
]


def _load_sp500_tickers() -> list[str]:
    """Load S&P 500 tickers from the Wikipedia fixture.

    If the fixture doesn't exist yet, fetches live and saves it first.
    Returns the full S&P 500 constituent list.
    """
    fixture_path = FIXTURES_DIR / "wikipedia" / "sp500_tickers.json"
    if fixture_path.exists():
        with open(fixture_path) as f:
            return json.load(f)

    # Fixture not available — fetch live
    print("  [INFO] S&P 500 list not cached, fetching from Wikipedia ...")
    refresh_wikipedia()
    if fixture_path.exists():
        with open(fixture_path) as f:
            return json.load(f)

    # Fallback: minimal set if Wikipedia fetch failed
    print("  [WARN] Wikipedia fetch failed, using fallback equity list")
    return [
        "AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "GOOG", "AVGO",
        "NFLX", "COST", "HD", "NKE", "JPM", "GS", "UNH", "LLY",
    ]


def _build_all_symbols() -> list[str]:
    """Build the full symbol universe: S&P 500 + index/breadth ETFs."""
    sp500 = _load_sp500_tickers()
    return list(dict.fromkeys(sp500 + INDEX_AND_BREADTH))


def refresh_alpaca_bars() -> None:
    """Fetch daily bars (2 years) for S&P 500 + index/breadth ETFs.

    2 years (~500 bars) provides enough history for:
    - Weekly resampling (needs ~420 daily bars for 80+ weeks)
    - 200-day MA and long-term momentum indicators
    - Sufficient warmup for any backtest period
    """
    from tools.data.provider import create_provider

    all_symbols = _build_all_symbols()
    print(f"\n[Alpaca] Daily bars ({len(all_symbols)} symbols, 2 years) ...")
    provider = create_provider(cache_dir=".cache/fixture_refresh")
    end = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    start = end - timedelta(days=730)  # ~2 years

    # Fetch in batches to avoid API limits
    bars = _fetch_in_batches(provider, all_symbols, "day", start, end)
    fixture = {}
    for sym, df in bars.items():
        sample = df.tail(504).copy()  # ~2 years of trading days
        sample.index = sample.index.strftime("%Y-%m-%d")
        fixture[sym] = json.loads(sample.to_json(orient="index"))
        print(f"    {sym}: {len(sample)} bars")
    _save("alpaca/daily_bars.json", fixture)
    print(f"  Total: {len(fixture)} symbols saved")


def refresh_yfinance_daily_bars() -> None:
    """Fetch daily bars (2 years) via yfinance for consolidated volume.

    Alpaca free-tier uses IEX exchange data (~2-4% of total volume),
    which makes volume-based screening unreliable. yfinance provides
    consolidated volume from all exchanges.

    2 years (~500 bars) provides enough history for:
    - 200-day MA and long-term momentum indicators
    - Weekly resampling (needs ~420 daily bars for 80+ weeks)
    - Sufficient warmup for any backtest period
    """
    import yfinance as yf

    all_symbols = _build_all_symbols()
    clean_symbols, reverse_map = _sanitise_symbols(all_symbols)
    print(f"\n[yfinance] Daily bars ({len(clean_symbols)} symbols, 2yr) ...")

    fixture = {}
    failed = 0
    batch_size = 20

    for i in range(0, len(clean_symbols), batch_size):
        batch = clean_symbols[i : i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(clean_symbols) + batch_size - 1) // batch_size
        print(f"    Batch {batch_num}/{total_batches}: {batch[0]}..{batch[-1]}")

        for sym in batch:
            try:
                ticker = yf.Ticker(sym)
                df = ticker.history(period="2y", interval="1d")
                if df.empty:
                    failed += 1
                    continue
                df = df.rename(columns={
                    "Open": "open", "High": "high", "Low": "low",
                    "Close": "close", "Volume": "volume",
                })
                df = df[["open", "high", "low", "close", "volume"]]
                df.index = df.index.tz_localize(None).strftime("%Y-%m-%d")
                original_sym = reverse_map.get(sym, sym)
                fixture[original_sym] = json.loads(df.to_json(orient="index"))
            except Exception as exc:
                print(f"      SKIP: {sym} ({exc})")
                failed += 1

        time.sleep(0.5)

    _save("yfinance/daily_bars.json", fixture)
    print(f"  Total: {len(fixture)} symbols saved ({failed} failed)")


def refresh_hourly_bars() -> None:
    """Fetch hourly bars (6 months, premarket + after hours) via yfinance.

    Uses prepost=True to include extended hours (4AM-7PM ET):
    - Premarket bars (4AM-9:30AM): enables MORNING cycle gap simulation
    - Regular hours (9:30AM-4PM): accurate volume (vs Alpaca IEX ~4%)
    - After hours (4PM-7PM): captures post-close moves

    yfinance hourly limit: ~730 trading days (2 years).
    """
    import yfinance as yf

    all_symbols = _build_all_symbols()
    clean_symbols, reverse_map = _sanitise_symbols(all_symbols)
    print(f"\n[yfinance] Hourly bars with extended hours ({len(clean_symbols)} symbols, 6mo) ...")

    fixture = {}
    failed = 0
    batch_size = 20

    for i in range(0, len(clean_symbols), batch_size):
        batch = clean_symbols[i : i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(clean_symbols) + batch_size - 1) // batch_size
        print(f"    Batch {batch_num}/{total_batches}: {batch[0]}..{batch[-1]}")

        for sym in batch:
            try:
                ticker = yf.Ticker(sym)
                df = ticker.history(period="6mo", interval="1h", prepost=True)
                if df.empty:
                    failed += 1
                    continue
                # Normalize columns to match fixture format
                df = df.rename(columns={
                    "Open": "open", "High": "high", "Low": "low",
                    "Close": "close", "Volume": "volume",
                })
                df = df[["open", "high", "low", "close", "volume"]]
                # Convert timezone-aware index to UTC ISO strings
                df.index = df.index.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%S")
                original_sym = reverse_map.get(sym, sym)
                fixture[original_sym] = json.loads(df.to_json(orient="index"))
            except Exception as exc:
                print(f"      SKIP: {sym} ({exc})")
                failed += 1

        time.sleep(0.5)

    _save("yfinance/hourly_bars.json", fixture)
    print(f"  Total: {len(fixture)} symbols saved ({failed} failed)")


def _sanitise_symbols(symbols: list[str]) -> tuple[list[str], dict[str, str]]:
    """Normalise symbols for yfinance (dots → dashes) and filter unsupported ones.

    Returns:
        (clean_symbols, reverse_map) where reverse_map maps clean→original.
    """
    # yfinance uses dashes: BRK.B → BRK-B, BF.B → BF-B
    _DOT_TO_DASH = {"BRK.B": "BRK-B", "BF.B": "BF-B"}

    clean_syms: list[str] = []
    reverse_map: dict[str, str] = {}
    for sym in symbols:
        mapped = _DOT_TO_DASH.get(sym, sym)
        clean_syms.append(mapped)
        reverse_map[mapped] = sym
    return clean_syms, reverse_map


def _fetch_in_batches(
    provider,
    symbols: list[str],
    timeframe: str,
    start: datetime,
    end: datetime,
    batch_size: int = 20,
) -> dict:
    """Fetch bars in batches to respect API rate limits.

    Skips batches that fail (e.g. invalid symbols) and continues.
    """
    clean_symbols, _ = _sanitise_symbols(symbols)
    all_bars = {}
    total_batches = (len(clean_symbols) + batch_size - 1) // batch_size
    for i in range(0, len(clean_symbols), batch_size):
        batch = clean_symbols[i : i + batch_size]
        batch_num = i // batch_size + 1
        print(f"    Batch {batch_num}/{total_batches}: {batch[0]}..{batch[-1]} ({len(batch)} symbols)")
        try:
            bars = provider.get_bars(batch, timeframe=timeframe, start=start, end=end)
            all_bars.update(bars)
        except Exception as exc:
            # Try symbols one-by-one to isolate the bad one
            print(f"    Batch {batch_num} failed: {exc}")
            print(f"    Retrying individually ...")
            for sym in batch:
                try:
                    bars = provider.get_bars([sym], timeframe=timeframe, start=start, end=end)
                    all_bars.update(bars)
                except Exception:
                    print(f"      SKIP: {sym}")
        if i + batch_size < len(clean_symbols):
            time.sleep(0.5)  # rate limit courtesy
    return all_bars


def refresh_alpaca_quotes() -> None:
    """Fetch latest quotes for S&P 500 + index/breadth ETFs."""
    from tools.data.provider import create_provider

    all_symbols = _build_all_symbols()
    print(f"\n[Alpaca] Latest quotes ({len(all_symbols)} symbols) ...")
    provider = create_provider(cache_dir=".cache/fixture_refresh")
    quotes = provider.get_latest_quotes(all_symbols)
    for sym, q in list(quotes.items())[:5]:
        print(f"    {sym}: mid={q['mid_price']}")
    if len(quotes) > 5:
        print(f"    ... and {len(quotes) - 5} more")
    _save("alpaca/latest_quotes.json", quotes)


def refresh_alpaca_trading() -> None:
    """Fetch account, positions, open orders."""
    from tools.execution.alpaca_orders import _get_trading_client, get_open_orders

    print("\n[Alpaca] Trading state ...")
    client = _get_trading_client()
    account = client.get_account()
    positions = client.get_all_positions()
    orders = get_open_orders()

    fixture = {
        "account": {
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value),
            "equity": float(account.equity),
            "status": str(getattr(account.status, "value", account.status)),
        },
        "positions": [
            {
                "symbol": p.symbol,
                "qty": int(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "unrealized_pl": float(p.unrealized_pl),
                "market_value": float(p.market_value),
            }
            for p in positions
        ],
        "open_orders": orders,
    }
    print(f"    Account: ${fixture['account']['portfolio_value']:,.0f}")
    print(f"    Positions: {len(fixture['positions'])}")
    print(f"    Open orders: {orders['total_count']}")
    _save("alpaca/trading_state.json", fixture)


def refresh_polygon_news() -> None:
    """Fetch news + scored sentiment."""
    from tools.sentiment.news import _fetch_polygon_news, fetch_and_score_news
    from config.settings import get_settings

    print("\n[Polygon] News ...")
    api_key = get_settings().polygon_api_key
    if not api_key:
        print("    SKIP — POLYGON_API_KEY not set")
        return

    articles = _fetch_polygon_news("AAPL", hours_back=48, api_key=api_key)
    _save("polygon/news_raw.json", {"AAPL": articles[:5]})
    print(f"    Raw articles: {len(articles)} (saved top 5)")

    scored = fetch_and_score_news(["AAPL", "NVDA"], hours_back=48)
    _save("polygon/news_scored.json", scored)
    for t in ["AAPL", "NVDA"]:
        r = scored.get(t, {})
        print(f"    {t}: sentiment={r.get('composite_sentiment')}, articles={r.get('article_count')}")


def refresh_yfinance_earnings() -> None:
    """Fetch earnings dates + EPS data from yfinance for the full S&P 500 universe.

    Saves {ticker: [{date, eps_estimate, reported_eps, surprise_pct}, ...]}
    for each ticker. Includes both upcoming (reported_eps=null) and past
    earnings. Used by:
      - earnings_risk.py for historical gap statistics
      - sentiment/earnings.py for blackout/PEAD screening
    """
    import yfinance as yf
    import pandas as pd

    all_symbols = _build_all_symbols()
    # Exclude ETFs — only individual stocks have earnings
    etfs = set(INDEX_AND_BREADTH)
    stock_symbols = [s for s in all_symbols if s not in etfs]
    stock_symbols, reverse_map = _sanitise_symbols(stock_symbols)
    print(f"\n[yfinance] Earnings dates ({len(stock_symbols)} stocks) ...")

    fixture: dict[str, list[dict]] = {}
    failed = 0
    for i, sym in enumerate(stock_symbols):
        try:
            t = yf.Ticker(sym)
            ed = t.earnings_dates
            if ed is not None and not ed.empty:
                entries = []
                for dt_idx, row in ed.head(12).iterrows():
                    reported = row.get("Reported EPS")
                    surprise = row.get("Surprise(%)")
                    estimate = row.get("EPS Estimate")
                    entries.append({
                        "date": dt_idx.date().isoformat(),
                        "eps_estimate": round(float(estimate), 2) if pd.notna(estimate) else None,
                        "reported_eps": round(float(reported), 2) if pd.notna(reported) else None,
                        "surprise_pct": round(float(surprise), 2) if pd.notna(surprise) else None,
                    })
                if entries:
                    original_sym = reverse_map.get(sym, sym)
                    fixture[original_sym] = entries
        except Exception as exc:
            failed += 1
            exc_msg = str(exc).split('\n')[0][:120]
            if failed <= 5 or failed % 50 == 0:
                print(f"    FAIL [{sym}]: {exc_msg}")

        if (i + 1) % 20 == 0 or (i + 1) == len(stock_symbols):
            print(f"    Progress: {i + 1}/{len(stock_symbols)} ({len(fixture)} collected, {failed} failed)")
            # Abort early if rate-limited (>50% failing)
            if failed > (i + 1) * 0.5 and failed > 20:
                print(f"    WARNING: Too many failures ({failed}/{i+1}) — possible rate limit. Stopping early.")
                break

    if fixture:
        _save("yfinance/earnings_dates.json", fixture)
    else:
        print("  SKIP save: no data collected (all failed)")
    print(f"  Total: {len(fixture)} stocks with earnings dates ({failed} failed)")


def refresh_wikipedia() -> None:
    """Fetch S&P 500 tickers + sector mapping."""
    import pandas as pd

    print("\n[Wikipedia] S&P 500 ...")
    try:
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            attrs={"id": "constituents"},
            storage_options={"User-Agent": "TradingSystem/1.0 (pandas read_html)"},
        )
        df = tables[0]
        df["Symbol"] = df["Symbol"].str.strip()
        tickers = df["Symbol"].tolist()
        sector_map = dict(zip(df["Symbol"], df["GICS Sector"]))
        _save("wikipedia/sp500_tickers.json", tickers)
        _save("wikipedia/sp500_sectors.json", sector_map)
        print(f"    Tickers: {len(tickers)}, Sectors: {len(set(sector_map.values()))}")
    except Exception as exc:
        print(f"    FAILED: {exc}")


def refresh_macro_data() -> None:
    """Fetch macro economic data from Polygon/Massive economy endpoints.

    Endpoints:
      /fed/v1/treasury-yields
      /fed/v1/inflation
      /fed/v1/inflation-expectations
      /fed/v1/labor-market

    Saves the latest observations to polygon/macro.json.
    """
    import requests
    from config.settings import get_settings

    print("\n[Polygon] Macro economic data ...")
    api_key = get_settings().polygon_api_key
    if not api_key:
        print("    SKIP — POLYGON_API_KEY not set")
        return

    base = "https://api.polygon.io"
    endpoints = {
        "treasury_yields": "/fed/v1/treasury-yields",
        "inflation": "/fed/v1/inflation",
        "inflation_expectations": "/fed/v1/inflation-expectations",
        "labor_market": "/fed/v1/labor-market",
    }

    macro: dict = {}
    for key, path in endpoints.items():
        try:
            resp = requests.get(
                f"{base}{path}",
                params={"limit": 3, "sort": "date.desc", "apiKey": api_key},
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            macro[key] = results
            latest_date = results[0].get("date", "?") if results else "empty"
            print(f"    {key}: {len(results)} records (latest: {latest_date})")
        except Exception as exc:
            print(f"    {key}: FAILED — {exc}")
            macro[key] = []

    macro["fetched_at"] = datetime.now(timezone.utc).isoformat()
    _save("polygon/macro.json", macro)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Re-fetch backtest fixture data from live APIs")
    parser.add_argument("--only", nargs="*",
                        choices=["daily", "alpaca_daily", "hourly", "quotes", "trading", "news", "earnings_dates", "wikipedia", "macro"],
                        help="Refresh only specific data sources (default: all)")
    args = parser.parse_args()

    targets = set(args.only) if args.only else {
        "daily", "hourly", "quotes", "trading", "news", "earnings_dates", "wikipedia", "macro",
    }

    print(f"=== Fixture refresh: {datetime.now(timezone.utc).isoformat()} ===")
    print(f"    Targets: {', '.join(sorted(targets))}")

    if "daily" in targets:
        refresh_yfinance_daily_bars()
    if "alpaca_daily" in targets:
        refresh_alpaca_bars()
    if "hourly" in targets:
        refresh_hourly_bars()
    if "quotes" in targets:
        refresh_alpaca_quotes()
    if "trading" in targets:
        refresh_alpaca_trading()
    if "news" in targets:
        refresh_polygon_news()
    if "earnings_dates" in targets:
        refresh_yfinance_earnings()
    if "wikipedia" in targets:
        refresh_wikipedia()
    if "macro" in targets:
        refresh_macro_data()
    print("\nDone.")


if __name__ == "__main__":
    main()
