"""cloud/main.py — AgentCore Runtime entrypoint (fire-and-forget pattern).

Uses BedrockAgentCoreApp SDK for async task lifecycle management.
The entrypoint returns immediately with session_id; actual work runs
in a background thread. AgentCore keeps the container alive via
HealthyBusy ping status while tasks are active.

Endpoints (managed by SDK):
  POST /invocations — Accept request, start background work, return immediately
  GET  /ping        — Auto-managed by SDK (HealthyBusy while tasks exist)

The container receives parameters via the invocation payload:
  Backtest/Simulate:
    {"input": {"action": "run", "mode": "backtest", "start_date": "2026-01-05",
               "end_date": "2026-01-31", "model_id": "...", "user_id": "alice"}}

  Live (single cycle, invoked by EventBridge schedule):
    {"input": {"action": "run", "mode": "live", "cycle": "EOD_SIGNAL",
               "session_id": "live-alice", "user_id": "alice"}}
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from typing import Optional

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# S3 code sync — hot-reload application code from S3 per invocation
# ---------------------------------------------------------------------------

APP_DIR = os.path.join(os.path.dirname(__file__), "..")

_HOT_RELOAD_PACKAGES = (
    "agents.", "backtest.", "tools.", "config.", "state.", "store.",
    "providers.", "playbook.",
)

def _sync_code_from_s3():
    """Download application code from S3, replacing bundled Docker image code.

    Always performs a full clean-slate sync: deletes code directories then
    re-downloads everything from S3.  Called at import time and before each
    invocation so warm containers always run the latest deployed code.
    """
    bucket = os.environ.get("DATA_BUCKET")
    if not bucket:
        logger.warning("DATA_BUCKET not set — using bundled code")
        return

    import shutil
    import boto3

    prefix = "code/swing-trading-agent"
    region = os.environ.get("AWS_REGION", "us-west-2")
    s3 = boto3.client("s3", region_name=region)

    _CODE_DIRS = ("agents", "tools", "config", "state", "store",
                  "providers", "playbook")
    for d in _CODE_DIRS:
        target = os.path.join(APP_DIR, d)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
    bt_dir = os.path.join(APP_DIR, "backtest")
    if os.path.isdir(bt_dir):
        for f in os.listdir(bt_dir):
            fp = os.path.join(bt_dir, f)
            if f.endswith(".py") and os.path.isfile(fp):
                os.remove(fp)

    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(prefix) + 1:]
            if not rel or "__pycache__" in rel or rel.endswith(".pyc"):
                continue
            local_path = os.path.join(APP_DIR, rel)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            s3.download_file(bucket, key, local_path)
            count += 1

    stale = [k for k in sys.modules
             if any(k.startswith(p) for p in _HOT_RELOAD_PACKAGES)]
    # Also evict top-level package entries (e.g. "tools", "agents") so their
    # __path__ / __spec__ caches are rebuilt and new sub-packages are found.
    top_pkgs = {p.rstrip(".") for p in _HOT_RELOAD_PACKAGES}
    for k in list(sys.modules):
        if k in top_pkgs or any(k.startswith(p) for p in _HOT_RELOAD_PACKAGES):
            del sys.modules[k]
            if k not in stale:
                stale.append(k)

    # Invalidate importlib finder caches so new directories (e.g. tools/data/)
    # are discovered on the next import.
    import importlib
    importlib.invalidate_caches()

    logger.info("Code sync: %d files from s3://%s/%s/ (evicted %d modules)",
                count, bucket, prefix, len(stale))


_sync_code_from_s3()


# ---------------------------------------------------------------------------
# Secrets Manager → env vars
# ---------------------------------------------------------------------------

_secrets_loaded = False


def _load_secrets_to_env(force: bool = False):
    """Load API keys from Secrets Manager into environment variables.

    Reads ALPACA_SECRET_ARN and POLYGON_SECRET_ARN env vars (set by CDK),
    fetches the secret JSON, and injects key/secret into env vars that
    pydantic-settings (config/settings.py) reads.

    Args:
        force: If True, reload secrets even if already loaded (warm container).
    """
    global _secrets_loaded
    if _secrets_loaded and not force:
        return

    import boto3
    region = os.environ.get("AWS_REGION", "us-west-2")
    sm = boto3.client("secretsmanager", region_name=region)

    alpaca_arn = os.environ.get("ALPACA_SECRET_ARN")
    if alpaca_arn:
        try:
            resp = sm.get_secret_value(SecretId=alpaca_arn)
            secret = json.loads(resp["SecretString"])
            # Support nested paper/live structure or flat legacy format
            is_paper = os.environ.get("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")
            if "paper" in secret or "live" in secret:
                acct = secret.get("paper" if is_paper else "live", {})
                base_url = "https://paper-api.alpaca.markets" if is_paper else "https://api.alpaca.markets"
                os.environ["ALPACA_API_KEY"] = acct.get("api_key", "")
                os.environ["ALPACA_SECRET_KEY"] = acct.get("secret_key", "")
                os.environ["ALPACA_BASE_URL"] = base_url
            else:
                # Legacy flat format: {"api_key":"...","secret_key":"...","base_url":"..."}
                os.environ["ALPACA_API_KEY"] = secret.get("api_key", "")
                os.environ["ALPACA_SECRET_KEY"] = secret.get("secret_key", "")
                os.environ["ALPACA_BASE_URL"] = secret.get("base_url", "https://paper-api.alpaca.markets")
            logger.info("Loaded Alpaca credentials from Secrets Manager (paper=%s)", is_paper)
        except Exception as e:
            logger.warning("Failed to load Alpaca secret: %s", e)

    polygon_arn = os.environ.get("POLYGON_SECRET_ARN")
    if polygon_arn:
        try:
            resp = sm.get_secret_value(SecretId=polygon_arn)
            secret = json.loads(resp["SecretString"])
            os.environ["POLYGON_API_KEY"] = secret.get("api_key", "")
            logger.info("Loaded Polygon credentials from Secrets Manager")
        except Exception as e:
            logger.warning("Failed to load Polygon secret: %s", e)

    _secrets_loaded = True


# ---------------------------------------------------------------------------
# S3 fixture sync
# ---------------------------------------------------------------------------

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "..", "backtest", "fixtures")

_fixtures_ready = False


def _sync_fixtures_from_s3():
    """Download fixture files from S3 to local backtest/fixtures/ directory."""
    global _fixtures_ready
    if _fixtures_ready:
        return

    bucket = os.environ.get("DATA_BUCKET")
    if not bucket:
        logger.warning("DATA_BUCKET not set — skipping fixture sync")
        _fixtures_ready = True
        return

    import boto3
    s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))

    try:
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket, Prefix="fixtures/")
        count = 0
        total_mb = 0.0
        for page in pages:
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel = key[len("fixtures/"):]
                if not rel:
                    continue
                local_path = os.path.join(FIXTURES_DIR, rel)
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                if os.path.exists(local_path) and os.path.getsize(local_path) == obj["Size"]:
                    continue
                s3.download_file(bucket, key, local_path)
                count += 1
                total_mb += obj["Size"] / (1024 * 1024)
        logger.info("Fixture sync: %d files (%.0f MB) from s3://%s/fixtures/", count, total_mb, bucket)
    except Exception as e:
        logger.error("Fixture sync failed: %s", e)

    _fixtures_ready = True


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------

class InvocationInput(BaseModel):
    action: str = "run"
    mode: str = "backtest"
    cycle: Optional[str] = None  # EOD_SIGNAL | MORNING | INTRADAY (live mode)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    precondition_session_id: Optional[str] = None
    model_id: Optional[str] = None
    enable_playbook: Optional[bool] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = BedrockAgentCoreApp()

# Active sessions for SIGTERM handling
_active_sessions: set[str] = set()


def _handle_sigterm(signum, frame):
    logger.warning("SIGTERM received — marking %d active session(s)", len(_active_sessions))
    for sid in list(_active_sessions):
        try:
            from store.factory import get_store
            store = get_store()
            meta = store.load_meta(sid) or {}
            target = "stopped" if meta.get("status") == "stop_requested" else "failed"
            store.update_status(sid, target)
        except Exception as e:
            logger.error("Failed to update status for %s on SIGTERM: %s", sid, e)
    sys.exit(0)

signal.signal(signal.SIGTERM, _handle_sigterm)


# ---------------------------------------------------------------------------
# Background runners
# ---------------------------------------------------------------------------

def _run_backtest_bg(session_id: str, params: InvocationInput, task_id: int):
    """Run backtest in background thread."""
    _active_sessions.add(session_id)
    try:
        from store.factory import get_store
        store = get_store()

        if params.model_id:
            os.environ["BEDROCK_MODEL_ID"] = params.model_id
        if params.enable_playbook is not None:
            os.environ["ENABLE_PLAYBOOK"] = str(params.enable_playbook).lower()

        from backtest.backtest import Backtest
        bt = Backtest(
            start_date=params.start_date,
            end_date=params.end_date,
            session_id=session_id,
        )

        result = bt.run()

        meta = store.load_meta(session_id) or {}
        if meta.get("status") != "stopped":
            store.update_status(session_id, "completed")

        logger.info("Backtest completed: %s", session_id)
    except Exception as e:
        logger.error("Backtest failed for %s: %s", session_id, e, exc_info=True)
        try:
            from store.factory import get_store
            store = get_store()
            meta = store.load_meta(session_id) or {}
            if meta.get("status") != "stopped":
                store.update_status(session_id, "failed")
        except Exception as e2:
            logger.error("Failed to mark backtest as failed: %s", e2)
    finally:
        import resource
        logger.info(
            "Backtest thread exiting: %s — RSS=%.0f MB",
            session_id,
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024),
        )
        _active_sessions.discard(session_id)
        app.complete_async_task(task_id)


def _run_live_cycle_bg(session_id: str, params: InvocationInput, task_id: int):
    """Run a single live/paper trading cycle in background thread."""
    import traceback as _tb
    # Use print() for critical logs — guaranteed to reach stdout even if
    # logging is misconfigured or the thread crashes early.
    print(f"[THREAD] _run_live_cycle_bg started: session={session_id}, "
          f"cycle={params.cycle}, mode={params.mode}", flush=True)

    _active_sessions.add(session_id)
    try:
        print(f"[THREAD] Step 1: importing store...", flush=True)
        from store.factory import get_store
        store = get_store()
        print(f"[THREAD] Step 1: store ready ({type(store).__name__})", flush=True)

        cycle = params.cycle
        if cycle not in ("EOD_SIGNAL", "MORNING", "INTRADAY"):
            logger.error("Live cycle %s: invalid cycle type '%s'", session_id, cycle)
            store.update_status(session_id, "failed")
            return

        # ── Step 1: Set up environment BEFORE loading settings ──
        is_paper = params.mode == "paper"
        os.environ["ALPACA_PAPER"] = "true" if is_paper else "false"
        if is_paper:
            os.environ["ALPACA_BASE_URL"] = "https://paper-api.alpaca.markets"

        if params.model_id:
            os.environ["BEDROCK_MODEL_ID"] = params.model_id
        if params.enable_playbook is not None:
            os.environ["ENABLE_PLAYBOOK"] = str(params.enable_playbook).lower()

        # Load API secrets — force reload on every invocation for warm containers
        print(f"[THREAD] Step 2: loading secrets...", flush=True)
        _load_secrets_to_env(force=True)

        # Clear cached settings so they pick up the new env vars
        from config.settings import get_settings
        get_settings.cache_clear()
        settings = get_settings()

        api_key_preview = settings.alpaca_api_key[:8] if settings.alpaca_api_key else "MISSING"
        print(f"[THREAD] Step 2: env ready — paper={settings.alpaca_paper}, "
              f"key={api_key_preview}...", flush=True)
        logger.info(
            "Live %s/%s: env ready — alpaca_paper=%s, api_key=%s...",
            params.mode, cycle, settings.alpaca_paper, api_key_preview,
        )

        if not settings.alpaca_api_key or settings.alpaca_api_key in ("paper_key", ""):
            logger.error("Live %s: Alpaca API key not configured", cycle)
            store.save_progress(session_id, {
                "cycle": cycle, "phase": "failed",
                "error": "Alpaca API key not configured — check Settings page",
            })
            return

        # ── Step 2: Load or create AgentState ──
        print(f"[THREAD] Step 3: loading state...", flush=True)
        from state.agent_state import AgentState, set_state

        state = AgentState()
        try:
            state_data = store.load_state(session_id)
            if state_data:
                state.load_from_dict(state_data)
                print(f"[THREAD] Step 3: restored state (positions={len(state.positions)}, "
                      f"cash={state.cash:.0f})", flush=True)
            else:
                print(f"[THREAD] Step 3: cold start", flush=True)
        except Exception as e:
            print(f"[THREAD] Step 3: state load failed — {e}", flush=True)
            logger.warning("Live %s: failed to load state, cold start — %s", cycle, e)
        set_state(state)

        # ── Step 2b: If portfolio values are missing, do an initial broker sync ──
        if state.cash == 0 and state.portfolio_value == 0:
            print(f"[THREAD] Step 3b: cash/value are $0 — running initial broker sync...", flush=True)
            try:
                from tools.execution.portfolio_sync import sync_positions_from_alpaca
                initial_sync = sync_positions_from_alpaca(existing_positions=state.positions)
                if not initial_sync.get('error'):
                    state.cash = initial_sync['cash']
                    state.portfolio_value = initial_sync['portfolio_value']
                    state.peak_value = max(state.peak_value, initial_sync['peak_value'])
                    # Sync positions and trade history into agent state
                    from state.portfolio_state import Position, Trade
                    for sym, pos_data in initial_sync.get('positions_full', {}).items():
                        if sym not in state.positions:
                            state.positions[sym] = Position.from_dict(pos_data)
                        else:
                            # Update live data, preserve metadata
                            local = state.positions[sym]
                            local.current_price = pos_data.get('current_price', local.current_price)
                            local.unrealized_pnl = pos_data.get('unrealized_pnl', local.unrealized_pnl)
                            local.qty = pos_data.get('qty', local.qty)
                    for t_data in initial_sync.get('trade_history', []):
                        state.trade_history.append(Trade.from_dict(t_data))
                    store.save_state(session_id, state.to_dict())
                    print(f"[THREAD] Step 3b: initial sync OK — cash=${state.cash:.0f}, "
                          f"value=${state.portfolio_value:.0f}, "
                          f"positions={len(state.positions)}", flush=True)
                else:
                    print(f"[THREAD] Step 3b: initial sync failed — {initial_sync['error']}", flush=True)
            except Exception as e:
                print(f"[THREAD] Step 3b: initial sync error — {e}", flush=True)
                logger.warning("Live %s: initial broker sync failed — %s", cycle, e)

        # ── Step 3: Run the trading cycle ──
        print(f"[THREAD] Step 4: importing PortfolioAgent...", flush=True)
        from agents.portfolio_agent import PortfolioAgent

        agent = PortfolioAgent(settings=settings, portfolio_state=state)
        print(f"[THREAD] Step 4: starting {cycle} cycle...", flush=True)
        logger.info("Live %s: starting %s cycle", params.mode, cycle)

        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        store.save_progress(session_id, {
            "cycle": cycle,
            "phase": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
        })

        result = agent.run_trading_cycle(cycle)
        print(f"[THREAD] Step 4: {cycle} cycle completed", flush=True)

        # ── Step 4: Persist results ──
        # Use trading_date from cycle result (derived from actual market data),
        # falling back to ET date
        today = (result or {}).get('trading_date') or \
            datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        print(f"[THREAD] Step 4: saving cycle with date={today}", flush=True)

        try:
            store.save_cycle(session_id, today, cycle, result or {})
        except Exception as e:
            logger.error("Live %s: failed to save cycle — %s", cycle, e)

        try:
            store.save_state(session_id, state.to_dict())
        except Exception as e:
            logger.error("Live %s: failed to save state — %s", cycle, e)

        try:
            store.save_progress(session_id, {
                "status": "running",
                "last_cycle": cycle,
                "last_date": today,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            logger.error("Live %s: failed to save progress — %s", cycle, e)

        # Record daily stats (SPY benchmark, drawdown, etc.)
        try:
            spy_close = None
            try:
                import yfinance as yf
                spy = yf.Ticker("SPY")
                hist = spy.history(period="2d")
                if not hist.empty:
                    spy_close = float(hist["Close"].iloc[-1])
            except Exception as e:
                logger.warning("Failed to fetch SPY close: %s", e)
            state.record_daily_stats(
                date=today,
                portfolio_value=state.portfolio_value,
                cash=state.cash,
                positions=state.positions,
                spy_close=spy_close,
                regime=state.last_regime or "",
            )
            if state.daily_stats:
                store.save_daily_stat(session_id, today, state.daily_stats[-1])
        except Exception:
            logger.warning("Failed to record daily stats", exc_info=True)

        print(f"[THREAD] Step 5: all persisted — cycle done", flush=True)
        logger.info("Live %s/%s: cycle completed", params.mode, cycle)
    except BaseException as e:
        # Catch BaseException (not just Exception) to catch SystemExit,
        # KeyboardInterrupt, and any other fatal errors.
        print(f"[THREAD] FATAL: {type(e).__name__}: {e}", flush=True)
        print(f"[THREAD] Traceback:\n{''.join(_tb.format_exception(e))}", flush=True)
        logger.error("Live cycle failed for %s/%s: %s", session_id, params.cycle, e,
                     exc_info=True)
        try:
            from store.factory import get_store
            store = get_store()
            store.save_progress(session_id, {
                "cycle": params.cycle,
                "phase": "failed",
                "error": f"{type(e).__name__}: {e}",
            })
        except Exception as e2:
            logger.error("Failed to save error progress: %s", e2)
        # Re-raise SystemExit so the process can actually exit if requested
        if isinstance(e, SystemExit):
            raise
    finally:
        print(f"[THREAD] finally: completing task {task_id}", flush=True)
        _active_sessions.discard(session_id)
        app.complete_async_task(task_id)


def _run_simulation_bg(session_id: str, params: InvocationInput, task_id: int):
    """Run simulation in background thread."""
    _active_sessions.add(session_id)
    snapshot_path = None
    try:
        from store.factory import get_store
        store = get_store()

        precond_id = params.precondition_session_id
        if not precond_id:
            logger.error("Simulation %s: precondition_session_id required", session_id)
            store.update_status(session_id, "failed")
            return

        snapshot = store.load_snapshot(precond_id)
        if not snapshot:
            logger.error("Simulation %s: snapshot not found for %s", session_id, precond_id)
            store.update_status(session_id, "failed")
            return

        if params.model_id:
            os.environ["BEDROCK_MODEL_ID"] = params.model_id
        if params.enable_playbook is not None:
            os.environ["ENABLE_PLAYBOOK"] = str(params.enable_playbook).lower()

        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as tmp:
            json.dump(snapshot, tmp, default=str)
            snapshot_path = tmp.name

        from backtest.backtest import Backtest as Simulator
        sim = Simulator(
            snapshot_path=snapshot_path,
            start_date=params.start_date,
            end_date=params.end_date,
            session_id=session_id,
        )

        result = sim.run()

        meta = store.load_meta(session_id) or {}
        if meta.get("status") != "stopped":
            store.update_status(session_id, "completed")

        logger.info("Simulation completed: %s", session_id)
    except Exception as e:
        logger.error("Simulation failed for %s: %s", session_id, e, exc_info=True)
        try:
            from store.factory import get_store
            store = get_store()
            meta = store.load_meta(session_id) or {}
            if meta.get("status") != "stopped":
                store.update_status(session_id, "failed")
        except Exception as e2:
            logger.error("Failed to mark simulation as failed: %s", e2)
    finally:
        if snapshot_path:
            try:
                os.unlink(snapshot_path)
            except Exception as e:
                logger.warning("Failed to clean up snapshot file %s: %s", snapshot_path, e)
        _active_sessions.discard(session_id)
        app.complete_async_task(task_id)


# ---------------------------------------------------------------------------
# Entrypoint — returns immediately
# ---------------------------------------------------------------------------

@app.entrypoint
def main(payload):
    """Main entrypoint — must return quickly (no blocking)."""
    input_data = payload.get("input", payload)
    params = InvocationInput(**input_data)

    logger.info("Invocation: action=%s, mode=%s, user=%s",
                params.action, params.mode, params.user_id)

    if params.action == "status":
        return _handle_status(params)
    elif params.action == "list_sessions":
        return _handle_list_sessions(params)
    elif params.action == "run":
        return _handle_run(params)
    else:
        return {"error": f"Unknown action: {params.action}"}


def _handle_run(params: InvocationInput) -> dict:
    """Start a run in background thread, return immediately."""
    # Reset secrets flag for warm containers — secrets are loaded per invocation
    global _secrets_loaded
    _secrets_loaded = False

    logger.info("_handle_run: mode=%s, cycle=%s, session=%s",
                params.mode, params.cycle, params.session_id)

    # Sync code (blocking but fast on warm containers)
    _sync_code_from_s3()
    # Fixtures only needed for backtest/simulate (large data files, slow to sync)
    if params.mode in ("backtest", "simulate"):
        _sync_fixtures_from_s3()

    # Reset store singleton after code sync (warm container may have stale instance)
    import store.factory as _sf
    _sf._instance = None

    from store.factory import get_store
    from store.session_id import generate_session_id

    store = get_store()
    session_id = params.session_id or generate_session_id(params.mode)
    user_id = params.user_id or "default"

    # For paper/live: preserve existing meta (resume case)
    existing_meta = None
    if params.mode in ("paper", "live") and params.session_id:
        try:
            existing_meta = store.load_meta(session_id)
        except Exception as e:
            logger.warning("Could not load existing meta for %s: %s", session_id, e)

    if existing_meta:
        existing_meta["status"] = "running"
        store.save_meta(session_id, existing_meta)
    else:
        store.save_meta(session_id, {
            "session_id": session_id,
            "user_id": user_id,
            "mode": params.mode,
            "status": "running",
            "start_date": params.start_date,
            "end_date": params.end_date,
            "model_id": params.model_id,
            "precondition_session_id": params.precondition_session_id,
        })

    # Register async task — SDK auto-sets ping to HealthyBusy
    task_id = app.add_async_task(f"{params.mode}_{session_id}")

    if params.mode == "backtest":
        t = threading.Thread(
            target=_run_backtest_bg,
            args=(session_id, params, task_id),
            daemon=True,
        )
    elif params.mode == "simulate":
        t = threading.Thread(
            target=_run_simulation_bg,
            args=(session_id, params, task_id),
            daemon=True,
        )
    elif params.mode in ("live", "paper"):
        if not params.cycle:
            app.complete_async_task(task_id)
            return {"error": "cycle required for live/paper mode (EOD_SIGNAL|MORNING|INTRADAY)"}
        t = threading.Thread(
            target=_run_live_cycle_bg,
            args=(session_id, params, task_id),
            daemon=True,
        )
    else:
        app.complete_async_task(task_id)
        return {"error": f"Unknown mode: {params.mode}"}

    print(f"[MAIN] Starting background thread for {params.mode}/{params.cycle} "
          f"session={session_id} task={task_id}", flush=True)
    t.start()
    print(f"[MAIN] Thread started: alive={t.is_alive()}", flush=True)
    return {"session_id": session_id, "status": "running"}


def _handle_status(params: InvocationInput) -> dict:
    """Return current session status and progress."""
    from store.factory import get_store
    store = get_store()
    session_id = params.session_id
    if not session_id:
        return {"error": "session_id required"}
    meta = store.load_meta(session_id)
    progress = store.load_progress(session_id)
    return {"meta": meta, "progress": progress}


def _handle_list_sessions(params: InvocationInput) -> dict:
    """List sessions for a user."""
    from store.factory import get_store
    store = get_store()
    user_id = params.user_id or "default"
    sessions = store.list_sessions(user_id)
    return {"sessions": sessions}


if __name__ == "__main__":
    app.run(port=8080)
