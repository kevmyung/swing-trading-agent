"""Fixture management + live state routes — /api/fixtures/*, /api/live/*"""

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.shared import (
    BASE_DIR,
    FIXTURES_DIR,
    STATE_DIR,
    BacktestRun,
    get_cloud_config,
    is_cloud_mode,
    procs,
    read_json,
    read_settings,
    run_lock,
    tail_log,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["fixtures"])

# Fixture-specific run registry (separate from backtest runs)
_fixture_runs: dict[str, BacktestRun] = {}


# ─── Helpers ────────────────────────────────────────────────────────────────


def _get_bars_date_range(fixture_path: Path) -> dict:
    """Extract date range from a bars fixture JSON using SPY as reference."""
    if not fixture_path.exists():
        return {}
    try:
        data = read_json(fixture_path)
        ref = data.get("SPY", {})
        if not ref:
            ref = next(iter(data.values()), {})
        dates = sorted(ref.keys())
        if dates:
            return {
                "first_date": dates[0][:10],
                "last_date": dates[-1][:10],
                "bar_count": len(dates),
            }
    except Exception:
        pass
    return {}


def _fixture_status_local() -> dict:
    """Read fixture status from local filesystem."""
    files = {
        "daily_bars": FIXTURES_DIR / "yfinance" / "daily_bars.json",
        "hourly_bars": FIXTURES_DIR / "yfinance" / "hourly_bars.json",
        "earnings_dates": FIXTURES_DIR / "yfinance" / "earnings_dates.json",
        "sp500_tickers": FIXTURES_DIR / "wikipedia" / "sp500_tickers.json",
        "sp500_sectors": FIXTURES_DIR / "wikipedia" / "sp500_sectors.json",
    }
    result = {}
    for name, path in files.items():
        if path.exists():
            stat = path.stat()
            info: dict = {
                "exists": True,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
            if name in ("daily_bars", "hourly_bars"):
                info.update(_get_bars_date_range(path))
            result[name] = info
        else:
            result[name] = {"exists": False}

    news_dir = FIXTURES_DIR / "polygon" / "news"
    if news_dir.is_dir():
        news_files = sorted(news_dir.glob("day_*.json"))
        day_names = [f.stem.replace("day_", "") for f in news_files]
        total_size = sum(f.stat().st_size for f in news_files)
        latest_mtime = max((f.stat().st_mtime for f in news_files), default=0)
        result["news"] = {
            "exists": len(day_names) > 0,
            "day_count": len(day_names),
            "first_date": day_names[0] if day_names else None,
            "last_date": day_names[-1] if day_names else None,
            "size_bytes": total_size,
            "modified": datetime.fromtimestamp(latest_mtime).isoformat() if latest_mtime else None,
        }
    else:
        result["news"] = {"exists": False, "day_count": 0}

    return result


def _fixture_status_s3(bucket: str, region: str) -> dict:
    """Read fixture status from S3 bucket."""
    import boto3

    s3 = boto3.client("s3", region_name=region)
    s3_keys = {
        "daily_bars": "fixtures/yfinance/daily_bars.json",
        "hourly_bars": "fixtures/yfinance/hourly_bars.json",
        "earnings_dates": "fixtures/yfinance/earnings_dates.json",
        "sp500_tickers": "fixtures/wikipedia/sp500_tickers.json",
        "sp500_sectors": "fixtures/wikipedia/sp500_sectors.json",
    }
    result = {}
    for name, key in s3_keys.items():
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
            result[name] = {
                "exists": True,
                "size_bytes": head["ContentLength"],
                "modified": head["LastModified"].isoformat(),
            }
        except Exception:
            result[name] = {"exists": False}

    try:
        paginator = s3.get_paginator("list_objects_v2")
        day_files = []
        total_size = 0
        latest_modified = None
        for page in paginator.paginate(Bucket=bucket, Prefix="fixtures/polygon/news/day_"):
            for obj in page.get("Contents", []):
                stem = obj["Key"].rsplit("/", 1)[-1].replace("day_", "").replace(".json", "")
                day_files.append(stem)
                total_size += obj["Size"]
                if latest_modified is None or obj["LastModified"] > latest_modified:
                    latest_modified = obj["LastModified"]
        day_files.sort()
        result["news"] = {
            "exists": len(day_files) > 0,
            "day_count": len(day_files),
            "first_date": day_files[0] if day_files else None,
            "last_date": day_files[-1] if day_files else None,
            "size_bytes": total_size,
            "modified": latest_modified.isoformat() if latest_modified else None,
        }
    except Exception:
        result["news"] = {"exists": False, "day_count": 0}

    return result


def _snapshot_fixture_mtimes() -> dict[str, float]:
    """Capture mtime of all .json files under FIXTURES_DIR."""
    mtimes: dict[str, float] = {}
    for root, _dirs, files in os.walk(FIXTURES_DIR):
        for fname in files:
            if not fname.endswith(".json"):
                continue
            p = Path(root) / fname
            mtimes[str(p.relative_to(FIXTURES_DIR))] = p.stat().st_mtime
    return mtimes


def _sync_fixtures_to_s3(before_mtimes: dict[str, float], log_file: Path | None = None):
    """Upload only changed fixtures to S3 (compares mtime before/after refresh)."""
    cfg = get_cloud_config()
    if not cfg or not cfg.get("s3_bucket"):
        return
    bucket = cfg["s3_bucket"]
    region = cfg.get("region", "us-west-2")

    def _log(msg: str):
        logger.info(msg)
        if log_file:
            try:
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(f"{msg}\n")
            except Exception:
                pass

    try:
        import boto3
        s3 = boto3.client("s3", region_name=region)

        after_mtimes = _snapshot_fixture_mtimes()
        changed = [
            rel for rel, mtime in after_mtimes.items()
            if mtime != before_mtimes.get(rel)
        ]

        # Also upload local news files that are missing from S3
        news_dir = FIXTURES_DIR / "polygon" / "news"
        if news_dir.is_dir():
            local_news = {f"polygon/news/{f.name}" for f in news_dir.glob("day_*.json")}
            if local_news:
                try:
                    paginator = s3.get_paginator("list_objects_v2")
                    s3_news: set[str] = set()
                    for page in paginator.paginate(Bucket=bucket, Prefix="fixtures/polygon/news/day_"):
                        for obj in page.get("Contents", []):
                            s3_news.add(obj["Key"].replace("fixtures/", "", 1))
                    missing = local_news - s3_news
                    if missing:
                        changed = list(set(changed) | missing)
                        _log(f">>> {len(missing)} news file(s) missing from S3, adding to upload")
                except Exception as e:
                    _log(f">>> Failed to check S3 news: {e}")

        if not changed:
            _log(">>> S3 sync: no files changed, skipping upload")
            return

        _log(f">>> Syncing {len(changed)} changed file(s) to s3://{bucket}/fixtures/ ...")

        uploaded = 0
        for idx, rel in enumerate(changed, 1):
            local_path = FIXTURES_DIR / rel
            s3_key = f"fixtures/{rel}"
            size_mb = local_path.stat().st_size / (1024 * 1024)
            _log(f"  [{idx}/{len(changed)}] Uploading {rel} ({size_mb:.1f} MB) ...")
            try:
                s3.upload_file(str(local_path), bucket, s3_key)
                uploaded += 1
            except Exception as e:
                _log(f"  Failed to upload {s3_key}: {e}")

        _log(f"  Uploaded {uploaded}/{len(changed)} files to S3")
    except ImportError:
        _log("  SKIP S3 sync: boto3 not installed")
    except Exception as e:
        _log(f"  S3 sync failed: {e}")


# ─── Fixture endpoints ──────────────────────────────────────────────────────


@router.get("/api/fixtures/status")
def get_fixture_status():
    """Check which fixture files exist and their last modified time."""
    cfg = get_cloud_config()
    if cfg and cfg.get("s3_bucket"):
        try:
            result = _fixture_status_s3(cfg["s3_bucket"], cfg.get("region", "us-west-2"))
            local = _fixture_status_local()
            for key in ("daily_bars", "hourly_bars"):
                if key in local and local[key].get("first_date"):
                    result[key].update({
                        k: local[key][k]
                        for k in ("first_date", "last_date", "bar_count")
                        if k in local[key]
                    })
            return result
        except Exception as exc:
            logger.warning("S3 fixture status failed (%s), falling back to local", exc)

    return _fixture_status_local()


class FixtureRefreshRequest(BaseModel):
    targets: list[str]
    news_start_date: str | None = None
    news_end_date: str | None = None


@router.post("/api/fixtures/refresh")
def start_fixture_refresh(req: FixtureRefreshRequest):
    """Launch fixture refresh scripts in a background process."""
    with run_lock:
        for r in _fixture_runs.values():
            if r.status == "running":
                raise HTTPException(409, "A fixture refresh is already running")

    run_id = f"fixture_{int(time.time())}"
    log_path = FIXTURES_DIR / "refresh.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    cmds: list[list[str]] = []
    non_news = [t for t in req.targets if t != "news"]
    if non_news:
        cmds.append([
            sys.executable, "-m", "backtest.fixtures.refresh",
            "--only", *non_news,
        ])
    if "news" in req.targets:
        news_cmd = [sys.executable, "-m", "backtest.fixtures.refresh_news"]
        if req.news_start_date:
            news_cmd += ["--start", req.news_start_date]
        if req.news_end_date:
            news_cmd += ["--end", req.news_end_date]
        cmds.append(news_cmd)

    run = BacktestRun(
        run_id=run_id,
        mode="fixture_refresh",
        session_id="fixtures",
        status="running",
        started_at=datetime.utcnow().isoformat() + "Z",
        config=req.model_dump(),
    )
    with run_lock:
        _fixture_runs[run_id] = run

    before_mtimes = _snapshot_fixture_mtimes()

    def _run_fixture_cmds():
        try:
            with open(log_path, "w", encoding="utf-8") as lf:
                for cmd in cmds:
                    lf.write(f">>> {' '.join(cmd)}\n")
                    lf.flush()
                    # Inject API keys from settings.json so refresh scripts can use them
                    saved_keys = read_settings().get("keys", {})
                    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
                    if saved_keys.get("polygon_api_key") and not env.get("POLYGON_API_KEY"):
                        env["POLYGON_API_KEY"] = saved_keys["polygon_api_key"]
                    proc = subprocess.Popen(
                        cmd, stdout=lf, stderr=subprocess.STDOUT,
                        cwd=str(BASE_DIR), env=env,
                    )
                    with run_lock:
                        procs[run_id] = proc
                    proc.wait()
                    if proc.returncode != 0:
                        with run_lock:
                            r = _fixture_runs.get(run_id)
                            if r and r.status == "running":
                                r.status = "failed"
                                r.error = f"Command failed with code {proc.returncode}"
                                r.finished_at = datetime.utcnow().isoformat() + "Z"
                                r.log_tail = tail_log(log_path)
                            procs.pop(run_id, None)
                        return
            with run_lock:
                procs.pop(run_id, None)
                r = _fixture_runs.get(run_id)
                if r and r.status == "running":
                    r.status = "syncing"
                    r.log_tail = tail_log(log_path)

            _sync_fixtures_to_s3(before_mtimes, log_path)

            with run_lock:
                r = _fixture_runs.get(run_id)
                if r and r.status == "syncing":
                    r.status = "completed"
                    r.finished_at = datetime.utcnow().isoformat() + "Z"
                    r.log_tail = tail_log(log_path)
        except Exception as exc:
            with run_lock:
                procs.pop(run_id, None)
                r = _fixture_runs.get(run_id)
                if r:
                    r.status = "failed"
                    r.error = str(exc)
                    r.finished_at = datetime.utcnow().isoformat() + "Z"

    t = threading.Thread(target=_run_fixture_cmds, daemon=True)
    t.start()

    cfg = get_cloud_config()
    return {
        "run_id": run_id,
        "status": "running",
        "storage": "s3" if cfg and cfg.get("s3_bucket") else "local",
    }


@router.get("/api/fixtures/runs")
def list_fixture_runs():
    """List fixture refresh runs."""
    log_path = FIXTURES_DIR / "refresh.log"
    with run_lock:
        result = []
        for r in _fixture_runs.values():
            if r.status == "running":
                r.log_tail = tail_log(log_path)
            result.append(r.model_dump())
    return result


@router.post("/api/fixtures/runs/{run_id}/stop")
def stop_fixture_run(run_id: str):
    """Stop a running fixture refresh."""
    with run_lock:
        run = _fixture_runs.get(run_id)
        if not run:
            raise HTTPException(404, f"Run '{run_id}' not found")
        if run.status != "running":
            raise HTTPException(409, f"Run is not running (status: {run.status})")
        proc = procs.get(run_id)
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            procs.pop(run_id, None)
        log_path = FIXTURES_DIR / "refresh.log"
        run.status = "stopped"
        run.finished_at = datetime.utcnow().isoformat() + "Z"
        run.log_tail = tail_log(log_path)
        run.error = "Stopped by user"
    return {"run_id": run_id, "status": "stopped"}


# ─── Live state endpoints ───────────────────────────────────────────────────


@router.get("/api/live/portfolio")
def get_live_portfolio():
    """Current live portfolio state."""
    path = STATE_DIR / "portfolio.json"
    if not path.exists():
        raise HTTPException(404, "No live portfolio state")
    return read_json(path)


@router.get("/api/live/watchlist")
def get_live_watchlist():
    """Current watchlist."""
    path = STATE_DIR / "watchlist.json"
    if not path.exists():
        return []
    return read_json(path)
