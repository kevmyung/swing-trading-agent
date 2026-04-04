"""Session routes — /api/sessions/*"""

import json
import logging
import shutil
import subprocess
from datetime import datetime

from fastapi import APIRouter, HTTPException

from api.shared import (
    SESSIONS_DIR,
    STATE_DIR,
    BacktestRun,
    cloud_store,
    get_cloud_config,
    get_fixture_provider,
    has_summary,
    is_cloud_mode,
    load_daily_stats,
    load_json_or_cloud,
    partial_metrics_from_stats,
    procs,
    read_json,
    run_lock,
    run_status_for,
    runs,
    session_dir,
    stop_agentcore_session,
    runtime_session_id_for,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/sessions", tags=["sessions"])


# ─── List helpers ───────────────────────────────────────────────────────────


def _meta_only(meta: dict, session_id: str, source: str = "local") -> dict:
    """Return lightweight session dict from meta only (no summary)."""
    return {
        "session_id": session_id,
        "display_name": meta.get("display_name", ""),
        "source": source,
        "status": meta.get("status", ""),
        "phase": meta.get("phase", ""),
        "start_date": meta.get("start_date", ""),
        "end_date": meta.get("end_date", ""),
        "sim_days": meta.get("sim_days", 0),
        "start_value": meta.get("start_cash", 0),
        "end_value": 0,
        "total_return_pct": 0,
        "spy_total_return_pct": 0,
        "max_drawdown_pct": 0,
        "sharpe_ratio": 0,
        "final_positions": [],
        "final_position_count": 0,
        "model_id": meta.get("model_id", ""),
        "mode": meta.get("mode", "backtest"),
        "enable_playbook": meta.get("enable_playbook"),
        "extended_thinking": meta.get("extended_thinking"),
    }


def _list_local_sessions(lite: bool = False) -> list[dict]:
    """List sessions from local filesystem."""
    if not SESSIONS_DIR.is_dir():
        return []
    sessions = []
    for entry in sorted(SESSIONS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        summary_path = entry / "summary.json"
        meta_path = entry / "meta.json"
        if not summary_path.exists() and not meta_path.exists():
            continue
        meta = read_json(meta_path) if meta_path.exists() else {}
        if lite:
            # Fast path: use cached summary if available, otherwise meta-only
            summary = read_json(summary_path) if summary_path.exists() else {}
            if not summary:
                sessions.append(_meta_only(meta, entry.name, "local"))
                continue
        else:
            summary = read_json(summary_path) if summary_path.exists() else {}
            # For incomplete sessions, compute partial metrics from daily_stats and cache
            if not summary and meta:
                status = meta.get("status", "")
                stats_dir = entry / "daily_stats"
                if stats_dir.is_dir():
                    stats = [read_json(f) for f in sorted(stats_dir.glob("*.json"))]
                    stats = [s for s in stats if s]
                    summary = partial_metrics_from_stats(stats, meta.get("start_cash", 100_000))
                    if summary and status in ("stopped", "failed"):
                        summary["session_id"] = entry.name
                        summary["status"] = status
                        summary["phase"] = meta.get("phase", "")
                        with open(summary_path, "w", encoding="utf-8") as f:
                            json.dump(summary, f, indent=2, default=str)
        sessions.append({
            "session_id": summary.get("session_id", entry.name),
            "display_name": meta.get("display_name", ""),
            "source": "local",
            "status": meta.get("status", ""),
            "phase": summary.get("phase", meta.get("phase", "")),
            "start_date": summary.get("start_date", meta.get("start_date", "")),
            "end_date": summary.get("end_date", meta.get("end_date", "")),
            "sim_days": summary.get("sim_days", 0),
            "start_value": summary.get("start_value", 0),
            "end_value": summary.get("end_value", 0),
            "total_return_pct": summary.get("total_return_pct", 0),
            "spy_total_return_pct": summary.get("spy_total_return_pct", 0),
            "max_drawdown_pct": summary.get("max_drawdown_pct", 0),
            "spy_max_drawdown_pct": summary.get("spy_max_drawdown_pct", 0),
            "sharpe_ratio": summary.get("sharpe_ratio", 0),
            "avg_invested_pct": summary.get("avg_invested_pct", 0),
            "final_positions": summary.get("final_positions", []),
            "final_position_count": summary.get("final_position_count", 0),
            "model_id": meta.get("model_id", ""),
            "mode": meta.get("mode", "backtest"),
            "enable_playbook": meta.get("enable_playbook"),
            "extended_thinking": meta.get("extended_thinking"),
        })
    return sessions


def _list_cloud_sessions(lite: bool = False) -> list[dict]:
    """List sessions from cloud store (S3)."""
    try:
        store = cloud_store()
        metas = store.list_sessions(user_id="default")
        sessions = []
        for meta in metas:
            sid = meta.get("session_id", "")
            if lite:
                sessions.append(_meta_only(meta, sid, "cloud"))
                continue
            summary = store.load_summary(sid) or {}
            if not summary:
                try:
                    stats = store.load_daily_stats(sid)
                    summary = partial_metrics_from_stats(
                        stats, meta.get("start_cash", 100_000),
                    )
                    if summary and meta.get("status") in ("stopped", "failed"):
                        summary["session_id"] = sid
                        summary["status"] = meta.get("status")
                        summary["phase"] = meta.get("phase", "")
                        store.save_summary(sid, summary)
                except Exception:
                    pass
            sessions.append({
                "session_id": sid,
                "display_name": meta.get("display_name", ""),
                "source": "cloud",
                "phase": meta.get("phase", summary.get("phase", "")),
                "status": meta.get("status", ""),
                "start_date": meta.get("start_date", summary.get("start_date", "")),
                "end_date": meta.get("end_date", summary.get("end_date", "")),
                "sim_days": summary.get("sim_days", meta.get("sim_days", 0)),
                "start_value": summary.get("start_value", 0),
                "end_value": summary.get("end_value", 0),
                "total_return_pct": summary.get("total_return_pct", 0),
                "spy_total_return_pct": summary.get("spy_total_return_pct", 0),
                "max_drawdown_pct": summary.get("max_drawdown_pct", 0),
                "sharpe_ratio": summary.get("sharpe_ratio", 0),
                "avg_invested_pct": summary.get("avg_invested_pct", 0),
                "final_positions": summary.get("final_positions", []),
                "final_position_count": summary.get("final_position_count", 0),
                "model_id": meta.get("model_id", ""),
                "mode": meta.get("mode", "backtest"),
                "enable_playbook": meta.get("enable_playbook"),
                "extended_thinking": meta.get("extended_thinking"),
            })
        return sessions
    except Exception as e:
        logger.warning("Failed to list cloud sessions: %s", e)
        return []


# ─── Endpoints ──────────────────────────────────────────────────────────────


@router.get("")
def list_sessions(lite: bool = False):
    """List all sessions. Merges local + cloud when cloud is configured.
    With lite=true, returns meta-only (skips heavy summary loading)."""
    sessions = _list_local_sessions(lite=lite)
    seen_ids = {s["session_id"] for s in sessions}

    with run_lock:
        for r in runs.values():
            if r.status in ("completed", "failed", "stopped"):
                continue
            if r.session_id in seen_ids:
                continue
            meta = load_json_or_cloud(
                session_dir(r.session_id) / "meta.json",
                lambda sid=r.session_id: cloud_store().load_meta(sid),
            ) or {}
            partial = {}
            try:
                stats = load_daily_stats(r.session_id)
                if stats:
                    partial = partial_metrics_from_stats(
                        stats, meta.get("start_cash", r.config.get("start_cash", 100_000)),
                    )
            except Exception:
                pass
            start_cash = meta.get("start_cash", r.config.get("start_cash", 100_000))
            sessions.append({
                "session_id": r.session_id,
                "display_name": meta.get("display_name", ""),
                "source": "cloud" if r.config.get("run_mode") == "cloud" else "local",
                "status": r.status,
                "phase": meta.get("phase", r.mode),
                "start_date": partial.get("start_date", meta.get("start_date", r.config.get("start_date", ""))),
                "end_date": partial.get("end_date", meta.get("end_date", r.config.get("end_date", ""))),
                "sim_days": partial.get("sim_days", meta.get("sim_days", 0)),
                "start_value": start_cash,
                "end_value": partial.get("end_value", 0),
                "total_return_pct": partial.get("total_return_pct", 0),
                "spy_total_return_pct": partial.get("spy_total_return_pct", 0),
                "max_drawdown_pct": partial.get("max_drawdown_pct", 0),
                "sharpe_ratio": partial.get("sharpe_ratio", 0),
                "final_positions": partial.get("final_positions", []),
                "final_position_count": partial.get("final_position_count", 0),
                "run_id": r.run_id,
                "model_id": meta.get("model_id", ""),
                "mode": meta.get("mode", r.mode),
            })
            seen_ids.add(r.session_id)

    if is_cloud_mode():
        cloud_sessions = _list_cloud_sessions(lite=lite)
        for cs in cloud_sessions:
            if cs["session_id"] not in seen_ids:
                sessions.append(cs)
    return sessions


@router.get("/{session_id}")
def get_session(session_id: str):
    """Full session summary including daily_log."""
    summary = load_json_or_cloud(
        session_dir(session_id) / "summary.json",
        lambda: cloud_store().load_summary(session_id),
    )
    if summary:
        summary.setdefault("status", "completed")
        summary.setdefault("sharpe_ratio", 0)
        summary.setdefault("avg_invested_pct", 0)
        summary.setdefault("spy_max_drawdown_pct", 0)
        if not summary.get("daily_log"):
            raw_stats = load_daily_stats(session_id)
            if raw_stats:
                summary["daily_log"] = [
                    {
                        "date": s.get("date", ""),
                        "day": i + 1,
                        "portfolio_value": s.get("portfolio_value", 0),
                        "cash": s.get("cash", 0),
                        "daily_return_pct": s.get("daily_return_pct", 0),
                        "spy_return_pct": s.get("spy_daily_return_pct", 0),
                        "excess_return_pct": s.get("excess_daily_return_pct", 0),
                        "positions": list(s.get("positions", {}).keys()),
                        "position_count": s.get("position_count", 0),
                        "events": [],
                        "new_entries": s.get("entries", []),
                        "regime": s.get("regime", ""),
                    }
                    for i, s in enumerate(raw_stats)
                ]
        return summary

    # Running/incomplete session
    meta = load_json_or_cloud(
        session_dir(session_id) / "meta.json",
        lambda: cloud_store().load_meta(session_id),
    )
    is_known_run = session_id in {r.session_id for r in runs.values()}
    if meta is None and not is_known_run:
        raise HTTPException(404, f"Session '{session_id}' not found")

    meta = meta or {}
    status = run_status_for(session_id)

    raw_stats = load_daily_stats(session_id)
    daily_log = []
    for stat in raw_stats:
        daily_log.append({
            "date": stat.get("date", ""),
            "day": len(daily_log) + 1,
            "portfolio_value": stat.get("portfolio_value", 0),
            "cash": stat.get("cash", 0),
            "daily_return_pct": stat.get("daily_return_pct", 0),
            "spy_return_pct": stat.get("spy_daily_return_pct", 0),
            "excess_return_pct": stat.get("excess_daily_return_pct", 0),
            "positions": list(stat.get("positions", {}).keys()),
            "position_count": stat.get("position_count", 0),
            "events": [],
            "new_entries": stat.get("entries", []),
            "regime": stat.get("regime", ""),
        })

    start_value = meta.get("start_cash", 100000)
    last_stat = raw_stats[-1] if raw_stats else {}
    end_value = daily_log[-1]["portfolio_value"] if daily_log else start_value
    total_return = ((end_value - start_value) / start_value * 100) if start_value else 0
    return {
        "session_id": session_id,
        "status": status,
        "phase": meta.get("phase", ""),
        "start_date": meta.get("start_date", daily_log[0]["date"] if daily_log else ""),
        "end_date": meta.get("end_date", daily_log[-1]["date"] if daily_log else ""),
        "sim_days": len(daily_log),
        "start_value": start_value,
        "end_value": round(end_value, 2),
        "total_return_pct": round(total_return, 2),
        "spy_total_return_pct": last_stat.get("spy_cumulative_return_pct", 0),
        "max_drawdown_pct": last_stat.get("max_drawdown_pct", 0),
        "sharpe_ratio": 0,
        "avg_invested_pct": 0,
        "final_positions": daily_log[-1]["positions"] if daily_log else [],
        "final_position_count": daily_log[-1]["position_count"] if daily_log else 0,
        "daily_log": daily_log,
    }


@router.patch("/{session_id}/meta")
def update_session_meta(session_id: str, body: dict):
    """Update session metadata (e.g. display_name)."""
    allowed = {"display_name"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(400, f"No valid fields. Allowed: {allowed}")

    meta_path = session_dir(session_id) / "meta.json"
    if meta_path.exists():
        meta = read_json(meta_path) or {}
        meta.update(updates)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, default=str)

    if is_cloud_mode():
        try:
            store = cloud_store()
            meta = store.load_meta(session_id) or {}
            meta.update(updates)
            store.save_meta(session_id, meta)
        except Exception as e:
            logger.warning("Failed to update cloud meta for %s: %s", session_id, e)

    return {"session_id": session_id, **updates}


@router.post("/{session_id}/stop")
def stop_session(session_id: str):
    """Stop a running session."""
    locally_killed = False
    with run_lock:
        for run_id, run in runs.items():
            if run.session_id == session_id and run.status == "running":
                proc = procs.get(run_id)
                if proc:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    procs.pop(run_id, None)
                    locally_killed = True
                run.status = "stopped"
                run.finished_at = datetime.utcnow().isoformat() + "Z"
                run.error = "Stopped by user"
                break

    if is_cloud_mode():
        stop_agentcore_session(runtime_session_id_for(session_id))
        target_status = "stopped" if locally_killed else "stop_requested"
        try:
            cloud_store().update_status(session_id, target_status)
        except Exception:
            pass
        return {"session_id": session_id, "status": target_status}

    if locally_killed:
        return {"session_id": session_id, "status": "stopped"}

    meta_path = session_dir(session_id) / "meta.json"
    if meta_path.exists():
        meta = read_json(meta_path)
        meta["status"] = "stopped"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, default=str)
        return {"session_id": session_id, "status": "stopped"}

    raise HTTPException(404, f"Session '{session_id}' not found")


@router.delete("/{session_id}")
def delete_session(session_id: str):
    """Delete a backtest session from local filesystem and/or cloud store."""
    deleted_from = []

    with run_lock:
        run_ids_to_remove = [
            rid for rid, r in runs.items() if r.session_id == session_id
        ]
        for rid in run_ids_to_remove:
            proc = procs.pop(rid, None)
            if proc and proc.poll() is None:
                proc.terminate()
            runs.pop(rid, None)
            deleted_from.append("run_registry")

    if is_cloud_mode():
        stop_agentcore_session(runtime_session_id_for(session_id))

    local_dir = session_dir(session_id)
    if local_dir.is_dir():
        shutil.rmtree(local_dir)
        deleted_from.append("local")

    if is_cloud_mode():
        try:
            cloud_store().delete_session(session_id)
            deleted_from.append("cloud")
        except Exception as e:
            logger.warning("Failed to delete cloud session %s: %s", session_id, e)

    if not deleted_from:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return {"deleted": session_id, "deleted_from": deleted_from}


@router.get("/{session_id}/progress")
def get_session_progress(session_id: str):
    """Live execution progress for a running session."""
    progress = load_json_or_cloud(
        session_dir(session_id) / "progress.json",
        lambda: cloud_store().load_progress(session_id),
    ) or {}

    with run_lock:
        for r in runs.values():
            if r.session_id == session_id:
                if r.status == "running" and is_cloud_mode():
                    try:
                        meta = cloud_store().load_meta(session_id) or {}
                        cloud_status = meta.get("status", "")
                        if cloud_status in ("completed", "failed", "stopped"):
                            r.status = cloud_status
                            r.finished_at = r.finished_at or datetime.utcnow().isoformat() + "Z"
                    except Exception:
                        pass
                progress["run_status"] = r.status
                progress["run_id"] = r.run_id
                return progress

    if is_cloud_mode():
        try:
            meta = cloud_store().load_meta(session_id) or {}
            status = meta.get("status", "")
            if status:
                progress["run_status"] = status
                return progress
        except Exception:
            pass

    progress["run_status"] = "completed" if has_summary(session_id) else "unknown"
    return progress


@router.get("/{session_id}/state")
def get_session_state(session_id: str):
    """Agent state for a session (cycle_logs, daily_stats, trades, positions)."""
    state = load_json_or_cloud(
        session_dir(session_id) / "agent_state.json",
        lambda: cloud_store().load_state(session_id),
    )
    return state


@router.get("/{session_id}/days/{date}")
def get_session_day(session_id: str, date: str):
    """All cycles for a given date (EOD_SIGNAL, MORNING, INTRADAY)."""
    cfg = get_cloud_config()
    if cfg:
        cycles = cloud_store().load_cycles(session_id, date)
        if cycles:
            return {"date": date, "cycles": cycles}
    else:
        from store.local import LocalStore
        local = LocalStore()
        cycles = local.load_cycles(session_id, date)
        if cycles:
            return {"date": date, "cycles": cycles}
    raise HTTPException(404, f"Day '{date}' not found in session '{session_id}'")


@router.get("/{session_id}/cycles")
def get_all_cycles(session_id: str):
    """All cycles for the entire session."""
    cfg = get_cloud_config()
    if cfg:
        cycles = cloud_store().load_all_cycles(session_id)
    else:
        from store.local import LocalStore
        local = LocalStore()
        cycles = local.load_all_cycles(session_id)
    return {"session_id": session_id, "cycles": cycles}


@router.get("/{session_id}/days/{date}/forward-returns")
def get_forward_returns(session_id: str, date: str, days: int = 20):
    """Forward returns for quant candidates + positions from a given date."""
    cfg = get_cloud_config()
    if cfg:
        store = cloud_store()
    else:
        from store.local import LocalStore
        store = LocalStore()
    cycles = store.load_cycles(session_id, date)
    if not cycles:
        raise HTTPException(404, f"Day '{date}' not found")

    # Merge cycles but take EOD_SIGNAL decisions specifically
    eod_decisions: list[dict] = []
    quant: dict = {}
    for c in cycles:
        if c.get("cycle_type") == "EOD_SIGNAL":
            eod_decisions = c.get("decisions") or []
            quant = c.get("quant_context") or {}
    if not quant:
        merged: dict = {}
        for c in cycles:
            merged.update(c)
        quant = merged.get("quant_context") or {}

    candidates = quant.get("candidates") or {}
    positions = quant.get("positions") or {}

    selected_tickers = set()
    for d in eod_decisions:
        action = d.get("action", "").upper()
        if action in ("LONG", "ADD"):
            selected_tickers.add(d.get("ticker", ""))

    all_tickers = sorted(set(list(candidates.keys()) + list(positions.keys())))
    if not all_tickers:
        return {"date": date, "days": days, "tickers": []}

    bench_tickers = ["SPY"]
    load_tickers = sorted(set(all_tickers + bench_tickers))

    provider = get_fixture_provider()
    bars = provider.get_bars(load_tickers, timeframe="day")

    result_tickers = []
    all_returns_by_date: dict[str, list[float]] = {}
    for ticker in load_tickers:
        df = bars.get(ticker)
        if df is None or df.empty:
            continue
        dates = df.index.strftime("%Y-%m-%d").tolist()
        if date not in dates:
            after = [d for d in dates if d >= date]
            if not after:
                continue
            start_idx = dates.index(after[0])
        else:
            start_idx = dates.index(date)

        base_price = float(df.iloc[start_idx]["close"])
        end_idx = min(start_idx + days + 1, len(df))
        forward_slice = df.iloc[start_idx:end_idx]

        returns = []
        for row_date, row in forward_slice.iterrows():
            returns.append({
                "date": row_date.strftime("%Y-%m-%d"),
                "return_pct": round((float(row["close"]) / base_price - 1) * 100, 2),
            })

        is_bench = ticker in bench_tickers
        is_position = ticker in positions
        is_selected = ticker in selected_tickers
        strategy = (candidates.get(ticker) or positions.get(ticker) or {}).get("strategy", "")

        if not is_bench:
            for r in returns:
                all_returns_by_date.setdefault(r["date"], []).append(r["return_pct"])

        result_tickers.append({
            "ticker": ticker,
            "strategy": strategy,
            "is_position": is_position,
            "is_selected": is_selected,
            "is_benchmark": is_bench,
            "base_price": base_price,
            "returns": returns,
        })

    if all_returns_by_date:
        avg_returns = [
            {"date": d, "return_pct": round(sum(vals) / len(vals), 2)}
            for d, vals in sorted(all_returns_by_date.items())
        ]
        result_tickers.append({
            "ticker": "AVG",
            "strategy": "",
            "is_position": False,
            "is_selected": False,
            "is_benchmark": True,
            "base_price": 0,
            "returns": avg_returns,
        })

    return {"date": date, "days": days, "tickers": result_tickers}


@router.get("/{session_id}/days/{date}/backward-returns")
def get_backward_returns(session_id: str, date: str, days: int = 20):
    """Backward price returns leading up to a decision date."""
    cfg = get_cloud_config()
    if cfg:
        store = cloud_store()
    else:
        from store.local import LocalStore
        store = LocalStore()
    cycles = store.load_cycles(session_id, date)
    if not cycles:
        raise HTTPException(404, f"Day '{date}' not found")

    eod_decisions: list[dict] = []
    quant: dict = {}
    for c in cycles:
        if c.get("cycle_type") == "EOD_SIGNAL":
            eod_decisions = c.get("decisions") or []
            quant = c.get("quant_context") or {}
    if not quant:
        merged: dict = {}
        for c in cycles:
            merged.update(c)
        quant = merged.get("quant_context") or {}

    candidates = quant.get("candidates") or {}
    positions = quant.get("positions") or {}

    selected_tickers = set()
    for d in eod_decisions:
        action = d.get("action", "").upper()
        if action in ("LONG", "ADD"):
            selected_tickers.add(d.get("ticker", ""))

    all_tickers = sorted(set(list(candidates.keys()) + list(positions.keys())))
    if not all_tickers:
        return {"date": date, "days": days, "tickers": []}

    bench_tickers = ["SPY"]
    load_tickers = sorted(set(all_tickers + bench_tickers))

    provider = get_fixture_provider()
    bars = provider.get_bars(load_tickers, timeframe="day")

    result_tickers = []
    all_returns_by_date: dict[str, list[float]] = {}
    for ticker in load_tickers:
        df = bars.get(ticker)
        if df is None or df.empty:
            continue
        dates = df.index.strftime("%Y-%m-%d").tolist()
        if date not in dates:
            before = [d for d in dates if d <= date]
            if not before:
                continue
            end_idx = dates.index(before[-1])
        else:
            end_idx = dates.index(date)

        base_price = float(df.iloc[end_idx]["close"])
        start_idx = max(end_idx - days, 0)
        backward_slice = df.iloc[start_idx:end_idx + 1]

        returns = []
        for row_date, row in backward_slice.iterrows():
            returns.append({
                "date": row_date.strftime("%Y-%m-%d"),
                "return_pct": round((float(row["close"]) / base_price - 1) * 100, 2),
            })

        is_bench = ticker in bench_tickers
        is_position = ticker in positions
        is_selected = ticker in selected_tickers
        strategy = (candidates.get(ticker) or positions.get(ticker) or {}).get("strategy", "")

        if not is_bench:
            for r in returns:
                all_returns_by_date.setdefault(r["date"], []).append(r["return_pct"])

        result_tickers.append({
            "ticker": ticker,
            "strategy": strategy,
            "is_position": is_position,
            "is_selected": is_selected,
            "is_benchmark": is_bench,
            "base_price": base_price,
            "returns": returns,
        })

    if all_returns_by_date:
        avg_returns = [
            {"date": d, "return_pct": round(sum(vals) / len(vals), 2)}
            for d, vals in sorted(all_returns_by_date.items())
        ]
        result_tickers.append({
            "ticker": "AVG",
            "strategy": "",
            "is_position": False,
            "is_selected": False,
            "is_benchmark": True,
            "base_price": 0,
            "returns": avg_returns,
        })

    return {"date": date, "days": days, "tickers": result_tickers}


@router.get("/{session_id}/log")
def get_session_log(session_id: str, date: str | None = None):
    """Raw run log for a session, optionally filtered by date prefix."""
    log_path = session_dir(session_id) / "run.log"
    if not log_path.exists():
        return {"lines": []}
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = [l.rstrip() for l in f.readlines()]
        if date:
            lines = [l for l in lines if date in l]
        return {"lines": lines}
    except Exception:
        return {"lines": []}


@router.get("/{session_id}/daily_stats")
def get_session_daily_stats(session_id: str):
    """Daily portfolio stats for charting."""
    return load_daily_stats(session_id)


@router.get("/{session_id}/snapshot")
def get_session_snapshot(session_id: str):
    """Load snapshot for a session (used by simulation to pick precondition)."""
    snap = load_json_or_cloud(
        session_dir(session_id) / "snapshot.json",
        lambda: cloud_store().load_snapshot(session_id),
    )
    if snap:
        return snap
    raise HTTPException(404, f"Snapshot not found for session '{session_id}'")
