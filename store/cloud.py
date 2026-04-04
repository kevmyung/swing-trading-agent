"""store/cloud.py — Hybrid DynamoDB + S3 session store for cloud deployment.

Hot path (DynamoDB) — frequently read, small data:
  - Session metadata (status, config, mode, dates)
  - Execution progress (current_day, cycle, phase)
  - Daily portfolio statistics (for charting)

Cold path (S3) — large payloads, read on demand:
  - Portfolio state (positions, cash, watchlist)
  - Session summary (final results)
  - Simulator handoff snapshot
  - Per-day trading data (quant, research, decisions)
  - Cache (pre-fetched news/earnings)

DynamoDB table schema:
  sessions table:
    PK: session_id (S)
    SK: record_type (S)  — "META", "PROGRESS", "DAILY_STAT#2026-01-05", etc.

S3 layout (unchanged for cold path):
  s3://{bucket}/sessions/{session_id}/state.json
  s3://{bucket}/sessions/{session_id}/summary.json
  s3://{bucket}/sessions/{session_id}/snapshot.json
  s3://{bucket}/sessions/{session_id}/days/day_{date}.json
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from store.base import SessionStore

logger = logging.getLogger(__name__)

# DynamoDB record type prefixes
_META = "META"
_PROGRESS = "PROGRESS"
_DAILY_STAT_PREFIX = "DAILY_STAT#"
_CYCLE_PREFIX = "CYCLE#"


class CloudStore(SessionStore):
    """Hybrid DynamoDB (hot) + S3 (cold) session storage."""

    def __init__(
        self,
        bucket: str | None = None,
        table_name: str | None = None,
        region: str | None = None,
        **_kwargs: Any,
    ) -> None:
        region = region or os.environ.get("AWS_REGION", "us-west-2")
        self.bucket = bucket or os.environ.get("DATA_BUCKET", "")
        self.table_name = table_name or os.environ.get("SESSION_TABLE", "")

        self._s3 = boto3.client("s3", region_name=region)
        self._ddb = boto3.resource("dynamodb", region_name=region)
        self._table = self._ddb.Table(self.table_name)

        logger.info(
            "CloudStore: table=%s, bucket=%s, region=%s",
            self.table_name, self.bucket, region,
        )

    # ------------------------------------------------------------------
    # DynamoDB helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _floats_to_decimal(obj: Any) -> Any:
        """Recursively convert float → Decimal for DynamoDB compatibility."""
        if isinstance(obj, float):
            return Decimal(str(obj))
        if isinstance(obj, dict):
            return {k: CloudStore._floats_to_decimal(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [CloudStore._floats_to_decimal(v) for v in obj]
        return obj

    def _ddb_put(self, session_id: str, record_type: str, data: dict) -> None:
        clean = json.loads(json.dumps(data, default=str))
        item = {
            "session_id": session_id,
            "record_type": record_type,
            "data": self._floats_to_decimal(clean),
        }
        self._table.put_item(Item=item)

    @staticmethod
    def _decimals_to_float(obj: Any) -> Any:
        """Recursively convert Decimal → float for JSON serialization."""
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, dict):
            return {k: CloudStore._decimals_to_float(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [CloudStore._decimals_to_float(v) for v in obj]
        return obj

    def _ddb_get(self, session_id: str, record_type: str) -> dict | None:
        resp = self._table.get_item(
            Key={"session_id": session_id, "record_type": record_type},
        )
        item = resp.get("Item")
        return self._decimals_to_float(item["data"]) if item else None

    def _ddb_update_field(
        self, session_id: str, record_type: str, field: str, value: Any,
    ) -> None:
        self._table.update_item(
            Key={"session_id": session_id, "record_type": record_type},
            UpdateExpression=f"SET #d.#f = :v",
            ExpressionAttributeNames={"#d": "data", "#f": field},
            ExpressionAttributeValues={":v": value},
        )

    def _ddb_query_prefix(
        self, session_id: str, sk_prefix: str,
    ) -> list[dict]:
        kwargs = {
            "KeyConditionExpression": (
                Key("session_id").eq(session_id)
                & Key("record_type").begins_with(sk_prefix)
            ),
        }
        items: list[dict] = []
        while True:
            resp = self._table.query(**kwargs)
            items.extend(resp.get("Items", []))
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return [self._decimals_to_float(i) for i in items]

    # ------------------------------------------------------------------
    # S3 helpers (cold path — unchanged)
    # ------------------------------------------------------------------

    def _s3_key(self, session_id: str, filename: str) -> str:
        return f"sessions/{session_id}/{filename}"

    def _s3_put(self, session_id: str, filename: str, data: Any) -> None:
        self._s3.put_object(
            Bucket=self.bucket,
            Key=self._s3_key(session_id, filename),
            Body=json.dumps(data, default=str).encode(),
            ContentType="application/json",
        )

    def _s3_get(self, session_id: str, filename: str) -> Any | None:
        try:
            resp = self._s3.get_object(
                Bucket=self.bucket,
                Key=self._s3_key(session_id, filename),
            )
            return json.loads(resp["Body"].read())
        except self._s3.exceptions.NoSuchKey:
            return None

    def _s3_list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
        return keys

    # ------------------------------------------------------------------
    # Session metadata  → DynamoDB
    # ------------------------------------------------------------------

    def save_meta(self, session_id: str, meta: dict) -> None:
        self._ddb_put(session_id, _META, meta)

    def load_meta(self, session_id: str) -> dict | None:
        return self._ddb_get(session_id, _META)

    def update_status(self, session_id: str, status: str) -> None:
        try:
            self._ddb_update_field(session_id, _META, "status", status)
        except Exception:
            # Fallback: full read-modify-write if item doesn't exist yet
            meta = self.load_meta(session_id) or {}
            meta["status"] = status
            self.save_meta(session_id, meta)

    def list_sessions(self, user_id: str) -> list[dict]:
        # Scan for all META records. For moderate session counts (<1000)
        # this is efficient. For very large scale, add a GSI on user_id.
        resp = self._table.scan(
            FilterExpression=Key("record_type").eq(_META),
        )
        sessions = []
        for item in resp.get("Items", []):
            meta = self._decimals_to_float(item.get("data", {}))
            meta.setdefault("session_id", item.get("session_id"))
            if user_id and meta.get("user_id") and meta["user_id"] != user_id:
                continue
            sessions.append(meta)

        # Handle pagination for large datasets
        while resp.get("LastEvaluatedKey"):
            resp = self._table.scan(
                FilterExpression=Key("record_type").eq(_META),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            for item in resp.get("Items", []):
                meta = self._decimals_to_float(item.get("data", {}))
                meta.setdefault("session_id", item.get("session_id"))
                if user_id and meta.get("user_id") and meta["user_id"] != user_id:
                    continue
                sessions.append(meta)

        return sessions

    # ------------------------------------------------------------------
    # Portfolio state  → S3 (large, read on demand)
    # ------------------------------------------------------------------

    def save_state(self, session_id: str, state: dict) -> None:
        self._s3_put(session_id, "state.json", state)

    def load_state(self, session_id: str) -> dict | None:
        return self._s3_get(session_id, "state.json")

    # ------------------------------------------------------------------
    # Progress  → DynamoDB
    # ------------------------------------------------------------------

    def save_progress(self, session_id: str, progress: dict) -> None:
        self._ddb_put(session_id, _PROGRESS, progress)

    def load_progress(self, session_id: str) -> dict | None:
        return self._ddb_get(session_id, _PROGRESS)

    # ------------------------------------------------------------------
    # Summary  → S3 (written once, read on demand)
    # ------------------------------------------------------------------

    def save_summary(self, session_id: str, summary: dict) -> None:
        self._s3_put(session_id, "summary.json", summary)

    def load_summary(self, session_id: str) -> dict | None:
        return self._s3_get(session_id, "summary.json")

    # ------------------------------------------------------------------
    # Snapshot  → S3 (large, read once for simulation handoff)
    # ------------------------------------------------------------------

    def save_snapshot(self, session_id: str, snapshot: dict) -> None:
        self._s3_put(session_id, "snapshot.json", snapshot)

    def load_snapshot(self, session_id: str) -> dict | None:
        return self._s3_get(session_id, "snapshot.json")

    # ------------------------------------------------------------------
    # Cycle data  → DynamoDB
    # ------------------------------------------------------------------

    # Cycle ordering for consistent sort within a day
    _CYCLE_ORDER = {"EOD_SIGNAL": 0, "MORNING": 1, "INTRADAY": 2}

    def save_cycle(
        self, session_id: str, date: str, cycle_type: str, data: dict,
    ) -> None:
        sk = f"{_CYCLE_PREFIX}{date}#{cycle_type}"
        enriched = {**data, "date": date, "cycle_type": cycle_type}
        self._ddb_put(session_id, sk, enriched)

    def load_cycles(self, session_id: str, date: str) -> list[dict]:
        items = self._ddb_query_prefix(session_id, f"{_CYCLE_PREFIX}{date}#")
        cycles = [item.get("data", {}) for item in items]
        return sorted(
            cycles,
            key=lambda c: self._CYCLE_ORDER.get(c.get("cycle_type", ""), 99),
        )

    def load_all_cycles(self, session_id: str) -> list[dict]:
        items = self._ddb_query_prefix(session_id, _CYCLE_PREFIX)
        cycles = [item.get("data", {}) for item in items]
        return sorted(
            cycles,
            key=lambda c: (
                c.get("date", ""),
                self._CYCLE_ORDER.get(c.get("cycle_type", ""), 99),
            ),
        )

    # ------------------------------------------------------------------
    # Daily stats  → DynamoDB
    # ------------------------------------------------------------------

    def save_daily_stat(self, session_id: str, date: str, stat: dict) -> None:
        self._ddb_put(session_id, f"{_DAILY_STAT_PREFIX}{date}", stat)

    def load_daily_stats(self, session_id: str) -> list[dict]:
        items = self._ddb_query_prefix(session_id, _DAILY_STAT_PREFIX)
        stats = []
        for item in sorted(items, key=lambda x: x.get("record_type", "")):
            stats.append(item.get("data", {}))
        return stats

    # ------------------------------------------------------------------
    # Cache  → local filesystem (transient, run-scoped)
    # ------------------------------------------------------------------

    def save_cache(self, session_id: str, date: str, data: dict) -> None:
        from pathlib import Path
        cache_dir = Path(f"/tmp/backtest_cache/{session_id}")
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_dir / f"day_{date}.json", "w") as f:
            json.dump(data, f, default=str)

    def load_cache(self, session_id: str, date: str) -> dict | None:
        from pathlib import Path
        path = Path(f"/tmp/backtest_cache/{session_id}/day_{date}.json")
        if not path.exists():
            return None
        with open(path) as f:
            return json.load(f)

    # ------------------------------------------------------------------
    # Delete session  → DynamoDB + S3
    # ------------------------------------------------------------------

    def delete_session(self, session_id: str) -> None:
        # Delete all DynamoDB records for this session
        resp = self._table.query(
            KeyConditionExpression=Key("session_id").eq(session_id),
        )
        with self._table.batch_writer() as batch:
            for item in resp.get("Items", []):
                batch.delete_item(Key={
                    "session_id": item["session_id"],
                    "record_type": item["record_type"],
                })
            # Handle pagination
            while resp.get("LastEvaluatedKey"):
                resp = self._table.query(
                    KeyConditionExpression=Key("session_id").eq(session_id),
                    ExclusiveStartKey=resp["LastEvaluatedKey"],
                )
                for item in resp.get("Items", []):
                    batch.delete_item(Key={
                        "session_id": item["session_id"],
                        "record_type": item["record_type"],
                    })

        # Delete all S3 objects for this session
        prefix = f"sessions/{session_id}/"
        keys = self._s3_list_keys(prefix)
        if keys:
            for i in range(0, len(keys), 1000):
                batch = keys[i:i + 1000]
                self._s3.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": k} for k in batch]},
                )
