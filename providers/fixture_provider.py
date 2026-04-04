"""
providers/fixture_provider.py — DataProvider backed by JSON fixture files.

Wraps the existing backtest/fixtures/loader.py FixtureProvider and extends
it to implement the full DataProvider ABC, including news, earnings, universe,
and sector data loaded from fixture JSON files.

Supports simulation context: call set_sim_context() before each cycle so that
get_quotes(), get_snapshots(), and get_news() return sim-date-aware data
from hourly bars and cached articles.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pandas as pd

from providers.data_provider import DataProvider

logger = logging.getLogger(__name__)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "backtest" / "fixtures"


class FixtureProvider(DataProvider):
    """DataProvider backed by fixture JSON files (for backtesting).

    Args:
        news_cache_dir: Optional path to a directory containing cached
            news files (``day_YYYY-MM-DD.json``). Used by ``get_news()``
            to load pre-fetched Polygon news instead of calling the API.
    """

    _DAILY_ALIASES = {"day", "1d", "daily"}
    _HOURLY_ALIASES = {"hour", "1h", "hourly"}

    def __init__(
        self,
        daily_file: str = "yfinance/daily_bars.json",
        hourly_file: str = "yfinance/hourly_bars.json",
        news_cache_dir: Path | str | None = None,
    ) -> None:
        self._daily: dict[str, pd.DataFrame] = _load_bars_fixture(daily_file)
        self._hourly: dict[str, pd.DataFrame] = {}

        hourly_path = FIXTURES_DIR / hourly_file
        if hourly_path.exists():
            self._hourly = _load_bars_fixture(hourly_file)
            logger.info("FixtureProvider: loaded hourly fixture: %d symbols", len(self._hourly))

        self._news_cache_dir: Path | None = (
            Path(news_cache_dir) if news_cache_dir else None
        )

        # Lazy-loaded fixtures
        self._universe: list[str] | None = None
        self._sector_map: dict[str, str] | None = None
        self._earnings_screened: dict | None = None

        # Simulation context (set by orchestrator before each cycle)
        self._sim_date: str | None = None
        self._sim_articles: dict[str, list[dict]] | None = None
        self._sim_prev_eod_time: datetime | None = None

    # ------------------------------------------------------------------
    # Simulation context
    # ------------------------------------------------------------------

    def set_sim_context(
        self,
        sim_date: str,
        articles: dict[str, list[dict]] | None = None,
        prev_eod_time: datetime | None = None,
    ) -> None:
        """Set simulation context for the current day/cycle.

        Called by backtest orchestrators before MORNING/INTRADAY cycles
        so that get_quotes(), get_snapshots(), and get_news() return
        sim-date-appropriate data from hourly bars and cached articles.
        """
        self._sim_date = sim_date
        if articles is not None:
            self._sim_articles = articles
        if prev_eod_time is not None:
            self._sim_prev_eod_time = prev_eod_time

    # ------------------------------------------------------------------
    # DataProvider: bars
    # ------------------------------------------------------------------

    @property
    def available_symbols(self) -> List[str]:
        return list(self._daily.keys())

    def get_bars(
        self,
        symbols: List[str],
        timeframe: str = "day",
        end=None,
    ) -> dict[str, pd.DataFrame]:
        tf = timeframe.lower()
        if tf in self._DAILY_ALIASES:
            source = self._daily
        elif tf in self._HOURLY_ALIASES:
            source = self._hourly
        else:
            logger.warning("FixtureProvider: unknown timeframe '%s', defaulting to daily", timeframe)
            source = self._daily

        result: dict[str, pd.DataFrame] = {}
        end_ts = None
        if end is not None:
            if isinstance(end, str):
                end_ts = pd.Timestamp(end)
            elif isinstance(end, datetime):
                end_ts = pd.Timestamp(end)
            else:
                end_ts = pd.Timestamp(end)

        for symbol in symbols:
            df = source.get(symbol)
            if df is None:
                continue
            if end_ts is not None:
                df = df[df.index <= end_ts]
            if not df.empty:
                result[symbol] = df
        return result

    # ------------------------------------------------------------------
    # DataProvider: quotes (sim-date-aware premarket from hourly bars)
    # ------------------------------------------------------------------

    def get_quotes(self, symbols: List[str]) -> dict[str, dict]:
        """Return premarket quotes from hourly bars when sim context is set.

        FixtureProvider is used exclusively for backtesting. Real-time quotes
        are only available via LiveProvider. When sim context is set, premarket
        quotes are synthesized from hourly bars; otherwise returns empty.
        """
        if self._sim_date and self._hourly:
            return self._premarket_quotes(symbols)
        return {}

    def _premarket_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Extract premarket close prices from hourly bars.

        Premarket = bars before 14:30 UTC (9:30 ET).
        """
        sim_ts = pd.Timestamp(self._sim_date)
        market_open = pd.Timestamp(f'{self._sim_date} 14:30:00')
        result: dict[str, dict] = {}
        for sym in symbols:
            hdf = self._hourly.get(sym)
            if hdf is None or hdf.empty:
                continue
            day = hdf[hdf.index.date == sim_ts.date()]
            premarket = day[day.index < market_open]
            if premarket.empty:
                continue
            price = float(premarket.iloc[-1]['close'])
            result[sym] = {
                'ask_price': price,
                'bid_price': price,
                'mid_price': price,
            }
        return result

    # ------------------------------------------------------------------
    # DataProvider: snapshots (sim-date-aware from hourly bars)
    # ------------------------------------------------------------------

    def get_snapshots(self, symbols: List[str], cutoff_utc: str = '15:30') -> dict[str, dict]:
        """Synthesize intraday snapshots from hourly fixture bars.

        Requires sim context (set_sim_context) to be called first.
        Returns empty dict if no sim context or hourly data available.
        """
        if self._sim_date and self._hourly:
            return self._hourly_snapshots(symbols, cutoff_utc)
        return {}

    def _hourly_snapshots(
        self, symbols: list[str], cutoff_utc: str = '15:30',
    ) -> dict[str, dict]:
        """Build intraday snapshot dicts from hourly fixture bars at sim_date."""
        sim_ts = pd.Timestamp(self._sim_date)
        cutoff = pd.Timestamp(f'{self._sim_date} {cutoff_utc}:00')
        result: dict[str, dict] = {}
        for sym in symbols:
            hdf = self._hourly.get(sym)
            if hdf is None or hdf.empty:
                continue
            today = hdf[(hdf.index.date == sim_ts.date()) & (hdf.index <= cutoff)]
            if today.empty:
                continue
            ddf = self._daily.get(sym)
            prev_close = 0.0
            prev_volume = 0.0
            if ddf is not None:
                prev = ddf[ddf.index < sim_ts]
                if not prev.empty:
                    prev_close = float(prev.iloc[-1]['close'])
                    prev_volume = float(prev.iloc[-1]['volume'])
            latest = float(today.iloc[-1]['close'])
            result[sym] = {
                'latest_price': latest,
                'today_open': float(today.iloc[0]['open']),
                'today_high': float(today['high'].max()),
                'today_low': float(today['low'].min()),
                'today_close': latest,
                'today_volume': float(today['volume'].sum()),
                'prev_close': prev_close,
                'prev_volume': prev_volume,
                'ask_price': latest,
                'bid_price': latest,
                'mid_price': latest,
            }
        return result

    # ------------------------------------------------------------------
    # DataProvider: news (sim-date-aware from articles)
    # ------------------------------------------------------------------

    def get_news(self, tickers: List[str], hours_back: int = 24) -> dict:
        """Return scored news.

        When sim context is set with articles, scores them for a time window
        based on sim_date and hours_back. Otherwise loads from cache directory.
        """
        if self._sim_date and self._sim_articles:
            return self._score_sim_news(tickers, hours_back)
        if not self._news_cache_dir:
            return {}
        if not self._news_cache_dir.exists():
            return {}
        cache_files = sorted(self._news_cache_dir.glob("day_*.json"), reverse=True)
        if not cache_files:
            return {}
        try:
            with open(cache_files[0]) as f:
                cached = json.load(f)
            return cached.get('news', {})
        except Exception as exc:
            logger.warning("FixtureProvider: failed to load news cache: %s", exc)
            return {}

    def _score_sim_news(self, tickers: list[str], hours_back: int) -> dict:
        """Score cached articles for a sim-date time window."""
        from tools.sentiment.news import score_news_for_window

        # Determine reference time from hours_back
        if hours_back <= 12:
            # MORNING: ref = 9AM ET = 14:00 UTC
            ref_time = datetime.strptime(self._sim_date, '%Y-%m-%d').replace(
                hour=14, tzinfo=timezone.utc,
            )
        else:
            # EOD: ref = 4PM ET = 21:00 UTC
            ref_time = datetime.strptime(self._sim_date, '%Y-%m-%d').replace(
                hour=21, tzinfo=timezone.utc,
            )
        window_start = self._sim_prev_eod_time
        # Filter articles to requested tickers only — scoring all tickers
        # causes non-held tickers to leak into INTRADAY flagging.
        ticker_set = set(t.upper() for t in tickers)
        filtered_articles = {
            t: arts for t, arts in self._sim_articles.items()
            if t.upper() in ticker_set
        }
        return score_news_for_window(filtered_articles, ref_time, window_start=window_start)

    def get_news_for_date(self, sim_date: str) -> dict:
        """Load news for a specific simulation date from cache.

        Args:
            sim_date: Date string ``'YYYY-MM-DD'``.

        Returns:
            News dict for that date, or empty dict if not cached.
        """
        if not self._news_cache_dir:
            return {}
        cache_path = self._news_cache_dir / f"day_{sim_date}.json"
        if not cache_path.exists():
            return {}
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            if 'news_articles' in cached:
                return cached['news_articles']
            if 'news' in cached:
                return cached['news']
            if any(isinstance(v, list) for v in cached.values()):
                return cached
            return {}
        except Exception as exc:
            logger.warning("FixtureProvider: failed to load news for %s: %s", sim_date, exc)
            return {}

    # ------------------------------------------------------------------
    # DataProvider: earnings
    # ------------------------------------------------------------------

    def get_earnings(self, tickers: List[str], as_of: str | None = None) -> dict[str, int]:
        """Return days-to-earnings from yfinance earnings fixture.

        Args:
            tickers: Symbols to check.
            as_of: Sim date string (YYYY-MM-DD). If None, uses sim context or today.
        """
        from datetime import date as _date
        from tools.sentiment.earnings import _load_earnings_fixture, _count_trading_days

        as_of = as_of or self._sim_date
        ref_date = _date.fromisoformat(as_of) if as_of else _date.today()
        fixture = _load_earnings_fixture()

        earnings_map: dict[str, int] = {}
        for t in tickers:
            entries = fixture.get(t.upper(), [])
            for entry in entries:
                d_str = entry.get("date", "")
                try:
                    entry_date = _date.fromisoformat(d_str)
                except (ValueError, TypeError):
                    continue
                if entry_date >= ref_date:
                    days_until = _count_trading_days(ref_date, entry_date)
                    if 0 <= days_until <= 10:
                        earnings_map[t] = days_until
                    break
        return earnings_map

    # ------------------------------------------------------------------
    # DataProvider: universe
    # ------------------------------------------------------------------

    def get_universe(self) -> List[str]:
        """Return S&P 500 tickers from Wikipedia fixture."""
        if self._universe is None:
            try:
                self._universe = _load_fixture("wikipedia/sp500_tickers.json") or []
            except FileNotFoundError:
                self._universe = list(self._daily.keys())
        return self._universe  # type: ignore[return-value]

    def get_sector_map(self) -> dict[str, str]:
        """Return ticker→sector from Wikipedia S&P 500 sectors fixture."""
        if self._sector_map is None:
            try:
                self._sector_map = _load_fixture("wikipedia/sp500_sectors.json") or {}
            except FileNotFoundError:
                self._sector_map = {}
        return self._sector_map  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_fixture(relative_path: str):
    path = FIXTURES_DIR / relative_path
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    with open(path) as f:
        return json.load(f)


def _load_bars_fixture(relative_path: str) -> dict[str, pd.DataFrame]:
    raw = _load_fixture(relative_path)
    bars: dict[str, pd.DataFrame] = {}
    for symbol, date_dict in raw.items():
        df = pd.DataFrame.from_dict(date_dict, orient="index")
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        for col in ("open", "high", "low", "close"):
            if col in df.columns:
                df[col] = df[col].astype(float)
        if "volume" in df.columns:
            df["volume"] = df["volume"].astype(float)
        bars[symbol] = df
    return bars
