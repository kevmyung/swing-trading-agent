"""
providers/live_provider.py — DataProvider backed by live APIs.

Uses yfinance for market data (bars, quotes, snapshots) and Polygon for news.
Alpaca is used only for order execution (see live_broker.py).

Bar data is cached in S3 to avoid re-fetching full history on every cycle.
On subsequent runs, only the last few days are fetched incrementally.
"""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List

import pandas as pd

from providers.data_provider import DataProvider

logger = logging.getLogger(__name__)


class LiveProvider(DataProvider):
    """DataProvider backed by yfinance (data) and Polygon (news).

    Args:
        settings: Application settings (API keys, etc.).
    """

    _DAILY_ALIASES = {"day", "1d", "daily"}
    _HOURLY_ALIASES = {"hour", "1h", "hourly"}
    _S3_CACHE_PREFIX = "cache/bars"

    def __init__(self, settings) -> None:
        self._settings = settings
        self._s3_bucket = self._resolve_bucket()
        self._s3 = None  # lazy init

    def _resolve_bucket(self) -> str | None:
        bucket = os.environ.get("DATA_BUCKET")
        if bucket:
            return bucket
        try:
            from api.shared import get_cloud_config
            cfg = get_cloud_config()
            if cfg:
                return cfg.get("s3_bucket")
        except Exception:
            pass
        return None

    def _get_s3(self):
        if self._s3 is None:
            import boto3
            region = os.environ.get("AWS_REGION", "us-west-2")
            self._s3 = boto3.client("s3", region_name=region)
        return self._s3

    def _s3_cache_key(self, interval: str) -> str:
        return f"{self._S3_CACHE_PREFIX}/bars_{interval}.parquet"

    def _load_s3_cache(self, interval: str) -> Dict[str, pd.DataFrame]:
        if not self._s3_bucket:
            return {}
        try:
            s3 = self._get_s3()
            resp = s3.get_object(Bucket=self._s3_bucket, Key=self._s3_cache_key(interval))
            buf = io.BytesIO(resp["Body"].read())
            df = pd.read_parquet(buf)
            if df.empty:
                return {}
            result: Dict[str, pd.DataFrame] = {}
            for sym in df.index.get_level_values("symbol").unique():
                sym_df = df.xs(sym, level="symbol")
                if not sym_df.empty:
                    result[sym] = sym_df
            logger.info("S3 bar cache loaded: %d tickers, key=%s", len(result), self._s3_cache_key(interval))
            return result
        except self._get_s3().exceptions.NoSuchKey:
            logger.info("S3 bar cache not found — will do full fetch.")
            return {}
        except Exception as exc:
            logger.warning("S3 bar cache load failed: %s", exc)
            return {}

    def _save_s3_cache(self, interval: str, bars: Dict[str, pd.DataFrame]) -> None:
        if not self._s3_bucket or not bars:
            return
        try:
            frames = []
            for sym, df in bars.items():
                tagged = df.copy()
                tagged.index.name = "date"
                tagged["symbol"] = sym
                tagged = tagged.set_index("symbol", append=True).reorder_levels(["symbol", "date"])
                frames.append(tagged)
            combined = pd.concat(frames)
            buf = io.BytesIO()
            combined.to_parquet(buf, index=True)
            buf.seek(0)
            self._get_s3().put_object(Bucket=self._s3_bucket, Key=self._s3_cache_key(interval), Body=buf.getvalue())
            logger.info("S3 bar cache saved: %d tickers, %.1f MB", len(bars), buf.getbuffer().nbytes / 1e6)
        except Exception as exc:
            logger.warning("S3 bar cache save failed: %s", exc)

    # ------------------------------------------------------------------
    # DataProvider: bars
    # ------------------------------------------------------------------

    def get_bars(
        self,
        symbols: List[str],
        timeframe: str = "day",
        end=None,
    ) -> dict[str, pd.DataFrame]:
        """Fetch OHLCV bars from yfinance with S3 caching.

        On first run, fetches full history and saves to S3.
        On subsequent runs, loads cache from S3 and fetches only recent days.
        """
        import yfinance as yf

        if not symbols:
            return {}

        tf = timeframe.lower()
        if tf in self._DAILY_ALIASES:
            interval = "1d"
            period_days = 730
        elif tf in self._HOURLY_ALIASES:
            interval = "1h"
            period_days = 180
        else:
            logger.warning("Unknown timeframe '%s', defaulting to daily", timeframe)
            interval = "1d"
            period_days = 730

        if end is not None:
            end_dt = datetime.strptime(end, "%Y-%m-%d") if isinstance(end, str) else (end.replace(tzinfo=None) if end.tzinfo else end)
        else:
            end_dt = datetime.now(timezone.utc).replace(tzinfo=None)

        full_start_dt = end_dt - timedelta(days=period_days)

        # Step 1: Load S3 cache
        cached_bars = self._load_s3_cache(interval)

        # Step 2: Split symbols into cached (incremental) vs uncached (full fetch)
        cached_symbols = []
        uncached_symbols = []
        min_bars_required = 200  # need enough history for 200MA

        for sym in symbols:
            cached_df = cached_bars.get(sym)
            if cached_df is not None and len(cached_df) >= min_bars_required:
                cached_symbols.append(sym)
            else:
                uncached_symbols.append(sym)

        fetch_end_str = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        fetched: Dict[str, pd.DataFrame] = {}

        # Step 3a: Incremental fetch for cached symbols (recent days only)
        if cached_symbols:
            latest_dates = [cached_bars[s].index[-1] for s in cached_symbols]
            cache_end = max(latest_dates)
            incremental_start = cache_end - timedelta(days=1)
            logger.info(
                "Incremental fetch: %d cached tickers (up to %s), fetching from %s",
                len(cached_symbols), cache_end.date(), incremental_start.date(),
            )
            inc_fetched = self._yf_batch_download(
                cached_symbols, incremental_start.strftime("%Y-%m-%d"), fetch_end_str, interval,
            )
            fetched.update(inc_fetched)

        # Step 3b: Full fetch for uncached symbols
        if uncached_symbols:
            logger.info("Full fetch: %d uncached tickers (%d days)", len(uncached_symbols), period_days)
            full_fetched = self._yf_batch_download(
                uncached_symbols, full_start_dt.strftime("%Y-%m-%d"), fetch_end_str, interval,
            )
            fetched.update(full_fetched)

        # Step 4: Merge cached + fetched
        result: Dict[str, pd.DataFrame] = {}

        for sym in set(symbols):
            cached_df = cached_bars.get(sym)
            fetched_df = fetched.get(sym)

            if cached_df is not None and fetched_df is not None:
                combined = pd.concat([cached_df, fetched_df])
                combined = combined[~combined.index.duplicated(keep="last")]
                combined = combined.sort_index()
                combined = combined[combined.index >= pd.Timestamp(full_start_dt)]
                result[sym] = combined
            elif fetched_df is not None:
                result[sym] = fetched_df
            elif cached_df is not None:
                result[sym] = cached_df

        # Step 5: Save merged result to S3
        if fetched:
            self._save_s3_cache(interval, result)

        logger.info("get_bars: %d/%d symbols with data", len(result), len(symbols))
        return result

    def _yf_batch_download(
        self,
        symbols: List[str],
        start_str: str,
        end_str: str,
        interval: str,
    ) -> Dict[str, pd.DataFrame]:
        """Download bars from yfinance in batches with retry and throttle."""
        import yfinance as yf
        import time

        result: Dict[str, pd.DataFrame] = {}
        batch_size = 20
        max_retries = 3

        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]

            for attempt in range(1, max_retries + 1):
                try:
                    df = yf.download(
                        batch,
                        start=start_str,
                        end=end_str,
                        interval=interval,
                        prepost=(interval == "1h"),
                        progress=False,
                        threads=True,
                    )

                    if df.empty:
                        break

                    if isinstance(df.columns, pd.MultiIndex):
                        for sym in batch:
                            try:
                                sym_df = df.xs(sym, level="Ticker", axis=1)
                                sym_df = _normalise_yf_df(sym_df)
                                if not sym_df.empty:
                                    result[sym] = sym_df
                            except KeyError:
                                continue
                    else:
                        sym = batch[0]
                        sym_df = _normalise_yf_df(df)
                        if not sym_df.empty:
                            result[sym] = sym_df
                    break  # success

                except Exception as exc:
                    if attempt < max_retries:
                        wait = 2 ** attempt
                        logger.warning(
                            "yfinance batch %d attempt %d/%d failed: %s — retrying in %ds",
                            i // batch_size + 1, attempt, max_retries, exc, wait,
                        )
                        time.sleep(wait)
                    else:
                        logger.warning(
                            "yfinance batch %d failed after %d attempts: %s",
                            i // batch_size + 1, max_retries, exc,
                        )

            # Throttle between batches
            if i + batch_size < len(symbols):
                time.sleep(2)

        return result

    # ------------------------------------------------------------------
    # DataProvider: quotes
    # ------------------------------------------------------------------

    def get_quotes(self, symbols: List[str]) -> dict[str, dict]:
        """Fetch latest quotes from yfinance fast_info."""
        import yfinance as yf

        if not symbols:
            return {}

        result: Dict[str, dict] = {}
        for sym in symbols:
            try:
                ticker = yf.Ticker(sym)
                info = ticker.fast_info
                price = float(info.last_price)
                prev = float(info.previous_close)
                result[sym] = {
                    "ask_price": price,
                    "bid_price": price,
                    "mid_price": price,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "prev_close": prev,
                }
            except Exception as exc:
                logger.debug("yfinance quote failed for %s: %s", sym, exc)
        return result

    # ------------------------------------------------------------------
    # DataProvider: snapshots
    # ------------------------------------------------------------------

    def get_snapshots(self, symbols: List[str]) -> dict[str, dict]:
        """Fetch intraday snapshots from yfinance."""
        import yfinance as yf

        if not symbols:
            return {}

        result: Dict[str, dict] = {}
        for sym in symbols:
            try:
                ticker = yf.Ticker(sym)
                info = ticker.fast_info
                price = float(info.last_price)
                prev_close = float(info.previous_close)
                today_open = float(info.open)
                today_high = float(info.day_high)
                today_low = float(info.day_low)
                today_volume = float(info.last_volume)

                # Previous day volume from 2-day history
                prev_volume = 0.0
                try:
                    hist = ticker.history(period="2d", interval="1d")
                    if len(hist) >= 2:
                        prev_volume = float(hist.iloc[-2]["Volume"])
                except Exception:
                    pass

                result[sym] = {
                    "latest_price": price,
                    "today_open": today_open,
                    "today_high": today_high,
                    "today_low": today_low,
                    "today_close": price,
                    "today_volume": today_volume,
                    "prev_close": prev_close,
                    "prev_volume": prev_volume,
                    "ask_price": price,
                    "bid_price": price,
                    "mid_price": price,
                }
            except Exception as exc:
                logger.debug("yfinance snapshot failed for %s: %s", sym, exc)
        return result

    # ------------------------------------------------------------------
    # DataProvider: news
    # ------------------------------------------------------------------

    def get_news(self, tickers: List[str], hours_back: int = 24) -> dict:
        """Fetch and score news from Polygon."""
        from tools.sentiment.news import fetch_and_score_news, clear_article_cache
        clear_article_cache()
        if not tickers:
            return {}
        try:
            return fetch_and_score_news(tickers, hours_back=hours_back)
        except Exception as exc:
            logger.warning("LiveProvider.get_news failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # DataProvider: earnings
    # ------------------------------------------------------------------

    def get_earnings(self, tickers: List[str]) -> dict[str, int]:
        """Fetch upcoming earnings via yfinance (fixture-first)."""
        if not tickers:
            return {}
        try:
            from tools.sentiment.earnings import screen_earnings_events
            result = screen_earnings_events(tickers)
            earnings_map: dict[str, int] = {}
            for entry in result.get('upcoming_earnings', []):
                t = entry.get('ticker', '')
                days_until = entry.get('days_until')
                if t and days_until is not None:
                    earnings_map[t] = int(days_until)
            return earnings_map
        except Exception as exc:
            logger.warning("LiveProvider.get_earnings failed: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # DataProvider: universe
    # ------------------------------------------------------------------

    def get_universe(self) -> List[str]:
        """Fetch S&P 500 tickers from Wikipedia (cached by screener)."""
        try:
            from tools.data.screener import get_sp500_tickers
            return get_sp500_tickers()
        except Exception as exc:
            logger.warning("LiveProvider.get_universe failed: %s", exc)
            return []

    def get_sector_map(self) -> dict[str, str]:
        """Return ticker→sector from S&P 500 Wikipedia data."""
        try:
            from tools.data.screener import get_sp500_sector_map
            return get_sp500_sector_map()
        except Exception as exc:
            logger.warning("LiveProvider.get_sector_map failed: %s", exc)
            return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_yf_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise a yfinance DataFrame to standard OHLCV format."""
    # Rename columns to lowercase
    col_map = {
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
        "Adj Close": "adj_close",
    }
    df = df.rename(columns=col_map)

    # Keep only OHLCV
    ohlcv = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[ohlcv].copy()

    # Strip timezone from index
    if hasattr(df.index, "tz") and df.index.tz is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)

    # Ensure correct dtypes
    for col in ["open", "high", "low", "close"]:
        if col in df.columns:
            df[col] = df[col].astype(float)
    if "volume" in df.columns:
        df["volume"] = df["volume"].astype(float)

    # Drop rows with NaN prices
    df = df.dropna(subset=["close"])
    return df
