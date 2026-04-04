"""
tools/data/cache.py — Disk-based cache for OHLCV DataFrames.

Saves DataFrames as Parquet files with a simple key-based lookup.
Cache entries older than max_age_hours are treated as stale.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class DataCache:
    """Parquet-backed cache for OHLCV DataFrames.

    Args:
        cache_dir: Directory where cache files are stored.
                   Created automatically if it does not exist.
    """

    def __init__(self, cache_dir: str = ".cache/market_data") -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get(self, key: str, max_age_hours: float = 24.0) -> Optional[pd.DataFrame]:
        """Return the cached DataFrame for *key* if it exists and is fresh.

        Args:
            key: Cache key string (e.g. ``AAPL_1Day_2024-01-01_2024-12-31``).
            max_age_hours: Maximum acceptable age in hours (default 24).

        Returns:
            DataFrame if the cache entry is fresh, ``None`` otherwise.
        """
        path = self._path(key)
        if not path.exists():
            logger.debug("Cache miss (not found): %s", key)
            return None
        if not self.is_fresh(key, max_age_hours):
            logger.debug("Cache miss (stale): %s", key)
            return None
        try:
            df = pd.read_parquet(path)
            logger.debug("Cache hit: %s (%d rows)", key, len(df))
            return df
        except Exception as exc:
            logger.warning("Cache read error for %s: %s", key, exc)
            return None

    def put(self, key: str, df: pd.DataFrame) -> None:
        """Persist *df* to the cache under *key*.

        Args:
            key: Cache key string.
            df: DataFrame to store.
        """
        path = self._path(key)
        try:
            df.to_parquet(path, index=True)
            logger.debug("Cache write: %s (%d rows)", key, len(df))
        except Exception as exc:
            logger.warning("Cache write error for %s: %s", key, exc)

    def is_fresh(self, key: str, max_age_hours: float = 24.0) -> bool:
        """Return True if the cached file for *key* is younger than *max_age_hours*.

        Args:
            key: Cache key string.
            max_age_hours: Maximum acceptable age in hours.

        Returns:
            ``True`` if the file exists and is fresh, ``False`` otherwise.
        """
        path = self._path(key)
        if not path.exists():
            return False
        age_seconds = time.time() - path.stat().st_mtime
        return age_seconds < max_age_hours * 3600

    def clear(self) -> int:
        """Remove all ``.parquet`` files from the cache directory.

        Returns:
            Number of files removed.
        """
        removed = 0
        for parquet_file in self._dir.glob("*.parquet"):
            try:
                parquet_file.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("Could not remove cache file %s: %s", parquet_file, exc)
        logger.debug("Cache cleared: %d files removed", removed)
        return removed

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, key: str) -> Path:
        """Return the filesystem path for a given cache key."""
        # Sanitise key so it is safe as a filename
        safe_key = key.replace("/", "_").replace(":", "_").replace(" ", "_")
        return self._dir / f"{safe_key}.parquet"
