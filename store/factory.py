"""store/factory.py — Store instance factory.

Reads STORE_MODE environment variable to select backend:
  - "local" (default): JSON files under backtest/sessions/
  - "cloud": S3 JSON objects under s3://{DATA_BUCKET}/sessions/
"""

from __future__ import annotations

import os
import logging

from store.base import SessionStore

logger = logging.getLogger(__name__)

_instance: SessionStore | None = None


def get_store() -> SessionStore:
    """Return the singleton SessionStore instance.

    Backend is selected by STORE_MODE env var ("local" or "cloud").
    """
    global _instance
    if _instance is not None:
        return _instance

    mode = os.environ.get("STORE_MODE", "local").lower()

    if mode == "cloud":
        from store.cloud import CloudStore
        _instance = CloudStore()
        logger.info("Store: CloudStore (S3)")
    else:
        from store.local import LocalStore
        _instance = LocalStore()
        logger.info("Store: LocalStore (JSON files)")

    return _instance
