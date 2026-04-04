"""
api/shared.py — Shared state, helpers, and cloud configuration.

Used across all route modules. Avoids circular imports by centralising
globals (_runs, _procs, _run_lock) and common I/O helpers here.
"""

import json
import logging
import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ─── Paths ──────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent
SESSIONS_DIR = BASE_DIR / "backtest" / "sessions"
STATE_DIR = BASE_DIR / "state"
CLOUD_CONFIG_PATH = BASE_DIR / "config" / "cloud_resources.json"
FIXTURES_DIR = BASE_DIR / "backtest" / "fixtures"
SETTINGS_PATH = STATE_DIR / "settings.json"

# ─── JSON helpers ───────────────────────────────────────────────────────────


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ─── Cloud config ───────────────────────────────────────────────────────────

_cloud_config: dict | None = None


def get_cloud_config() -> dict | None:
    """Load cloud resource config. Returns None if not configured."""
    global _cloud_config
    if _cloud_config is not None:
        return _cloud_config if _cloud_config else None
    if CLOUD_CONFIG_PATH.exists():
        try:
            cfg = read_json(CLOUD_CONFIG_PATH)
            if cfg.get("s3_bucket") and cfg.get("agentcore_runtime_arn"):
                _cloud_config = cfg
                logger.info("Cloud mode: %s", cfg.get("agentcore_runtime_arn"))
                return cfg
        except Exception as e:
            logger.warning("Failed to load cloud config: %s", e)
    _cloud_config = {}  # empty dict = checked but not available
    return None


def set_cloud_config(cfg: dict | None):
    """Set the cached cloud config (used by config routes)."""
    global _cloud_config
    _cloud_config = cfg if cfg else {}


def is_cloud_mode() -> bool:
    return get_cloud_config() is not None


def cloud_store():
    """Get a CloudStore instance from current cloud config."""
    from store.cloud import CloudStore
    cfg = get_cloud_config()
    return CloudStore(
        bucket=cfg["s3_bucket"],
        table_name=cfg.get("session_table", ""),
        region=cfg.get("region"),
    )


# ─── Session data helpers ───────────────────────────────────────────────────


def load_json_or_cloud(local_path: Path, cloud_loader) -> Any | None:
    """Load data from local file first, then cloud store."""
    if local_path.exists():
        return read_json(local_path)
    if is_cloud_mode():
        return cloud_loader()
    return None


def session_dir(session_id: str) -> Path:
    return SESSIONS_DIR / session_id


def has_summary(session_id: str) -> bool:
    """Check if a completed summary exists (local or cloud)."""
    return load_json_or_cloud(
        session_dir(session_id) / "summary.json",
        lambda: cloud_store().load_summary(session_id),
    ) is not None


def load_daily_stats(session_id: str) -> list[dict]:
    """Load daily stats from local files or cloud."""
    stats_dir = session_dir(session_id) / "daily_stats"
    if stats_dir.is_dir():
        stats = []
        for f in sorted(stats_dir.glob("*.json")):
            data = read_json(f)
            if data:
                stats.append(data)
        return stats
    if is_cloud_mode():
        return cloud_store().load_daily_stats(session_id)
    return []


def partial_metrics_from_stats(daily_stats: list[dict], start_cash: float = 100_000) -> dict:
    """Compute partial summary metrics from daily_stats (for stopped/incomplete sessions)."""
    if not daily_stats:
        return {}
    start_cash = float(start_cash)
    last = daily_stats[-1]
    end_value = last.get("portfolio_value", start_cash)
    total_return = ((end_value - start_cash) / start_cash * 100) if start_cash else 0
    max_dd = max((s.get("max_drawdown_pct", 0) for s in daily_stats), default=0)

    # Sharpe ratio from daily excess returns
    sharpe = 0.0
    daily_rets = [s.get("daily_return_pct", 0) for s in daily_stats]
    spy_rets = [s.get("spy_daily_return_pct", 0) for s in daily_stats]
    if len(daily_rets) >= 5:
        excess = [d - s for d, s in zip(daily_rets, spy_rets)]
        mean_ex = sum(excess) / len(excess)
        var = sum((x - mean_ex) ** 2 for x in excess) / len(excess)
        if var > 0:
            sharpe = round(mean_ex / (var ** 0.5) * (252 ** 0.5), 2)

    # Average invested percentage
    avg_invested = 0.0
    invested_vals = []
    for s in daily_stats:
        pv = s.get("portfolio_value", 0)
        cash = s.get("cash", 0)
        if pv > 0:
            invested_vals.append((pv - cash) / pv * 100)
    if invested_vals:
        avg_invested = round(sum(invested_vals) / len(invested_vals), 1)

    return {
        "sim_days": len(daily_stats),
        "start_date": daily_stats[0].get("date", ""),
        "end_date": last.get("date", ""),
        "start_value": start_cash,
        "end_value": round(end_value, 2),
        "total_return_pct": round(total_return, 2),
        "spy_total_return_pct": last.get("spy_cumulative_return_pct", 0),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe_ratio": sharpe,
        "avg_invested_pct": avg_invested,
        "final_positions": list(last.get("positions", {}).keys()),
        "final_position_count": last.get("position_count", 0),
    }


# ─── Settings helpers ───────────────────────────────────────────────────────


def read_settings() -> dict:
    if SETTINGS_PATH.exists():
        return read_json(SETTINGS_PATH)
    return {}


def write_settings(data: dict):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ─── Fixture provider (cached) ─────────────────────────────────────────────

_fixture_provider = None


def get_fixture_provider():
    """Cached FixtureProvider for API use (daily bars only, ~42MB)."""
    global _fixture_provider
    if _fixture_provider is None:
        from providers import FixtureProvider
        _fixture_provider = FixtureProvider(hourly_file="__none__")
    return _fixture_provider


# ─── Run registry (shared across paper + backtest) ──────────────────────────


class BacktestRun(BaseModel):
    """Tracks an in-progress or completed backtest."""
    run_id: str
    mode: str  # "precondition", "simulation", "paper", "fixture_refresh"
    session_id: str
    status: str  # "running", "completed", "failed", "stopped"
    started_at: str
    finished_at: str | None = None
    log_tail: list[str] = []
    error: str | None = None
    config: dict = {}
    runtime_session_id: str | None = None


# In-memory registry of runs
runs: dict[str, BacktestRun] = {}
procs: dict[str, subprocess.Popen] = {}  # run_id -> subprocess
run_lock = threading.Lock()


def run_status_for(session_id: str) -> str:
    """Get run status from in-memory registry, or derive from summary existence."""
    with run_lock:
        for r in runs.values():
            if r.session_id == session_id:
                return r.status
    return "completed" if has_summary(session_id) else "unknown"


def tail_log(log_path: Path, n: int = 30) -> list[str]:
    """Read last N lines from a log file."""
    if not log_path.exists():
        return []
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return []


def safe_log(log_path: Path, msg: str):
    """Append a message to a log file, silently ignoring errors."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(msg if msg.endswith("\n") else msg + "\n")
            lf.flush()
    except Exception:
        pass


def stop_agentcore_session(runtime_session_id: str):
    """Stop an AgentCore runtime session."""
    import boto3

    cfg = get_cloud_config()
    runtime_arn = cfg.get("agentcore_runtime_arn", "")
    region = cfg.get("region", "us-west-2")
    if not runtime_arn:
        return
    try:
        client = boto3.client("bedrock-agentcore", region_name=region)
        client.stop_runtime_session(
            runtimeSessionId=runtime_session_id,
            agentRuntimeArn=runtime_arn,
            qualifier="DEFAULT",
        )
        logger.info("Stopped AgentCore session: %s", runtime_session_id)
    except Exception as e:
        logger.warning("Failed to stop AgentCore session %s: %s", runtime_session_id, e)


def runtime_session_id_for(session_id: str) -> str:
    """Deterministic runtime session ID from session_id (exactly 33 chars)."""
    import hashlib
    return hashlib.sha256(session_id.encode()).hexdigest()[:33]


def run_backtest_subprocess(run_id: str, cmd: list[str], log_path: Path, env: dict | None = None):
    """Execute backtest in a subprocess, update run status on completion."""
    log_fh = None
    try:
        log_fh = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=str(BASE_DIR),
            env=env,
        )
        with run_lock:
            procs[run_id] = proc
        proc.wait()
        with run_lock:
            procs.pop(run_id, None)
            run = runs.get(run_id)
            if run and run.status == "running":
                run.finished_at = datetime.utcnow().isoformat() + "Z"
                run.log_tail = tail_log(log_path)
                if proc.returncode == 0:
                    run.status = "completed"
                else:
                    run.status = "failed"
                    run.error = f"Process exited with code {proc.returncode}"
    except Exception as exc:
        with run_lock:
            procs.pop(run_id, None)
            run = runs.get(run_id)
            if run:
                run.status = "failed"
                run.error = str(exc)
                run.finished_at = datetime.utcnow().isoformat() + "Z"
    finally:
        if log_fh is not None:
            log_fh.close()
