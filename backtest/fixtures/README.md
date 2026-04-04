# API Fixtures

Raw API response snapshots captured on 2026-03-08.
Used for deterministic backtesting and offline development.

## Structure

```
fixtures/
  alpaca/
    daily_bars.json        # 250-day OHLCV (20 symbols, multi-sector)
    latest_quotes.json     # bid/ask/mid snapshot
    trading_state.json     # account, positions, open orders
  polygon/
    news_raw.json          # raw Polygon /v2/reference/news response
    news_scored.json       # scored sentiment output
  finnhub/
    earnings_raw.json      # raw /calendar/earnings + /stock/earnings
    earnings_screened.json # screened blackout + PEAD output
  wikipedia/
    sp500_tickers.json     # 503 S&P 500 constituent tickers
    sp500_sectors.json     # ticker -> GICS sector mapping
```

## Usage

```python
from backtest.fixtures.loader import load_fixture, FixtureProvider

# Load a single fixture
bars = load_fixture("alpaca/daily_bars.json")

# Use FixtureProvider as drop-in replacement for MarketDataProvider
provider = FixtureProvider()
df = provider.get_bars(["AAPL"], timeframe="day")
```

## Refreshing fixtures

Run `python backtest/fixtures/refresh.py` to re-fetch all fixtures from live APIs.
Requires valid API keys in `.env`.
