"""
agents/_formatting.py — Prompt formatting utilities and risk helpers.

Pure functions used by all three cycle modules to format data for LLM prompts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


_CYCLE_TIMES = {
    'EOD_SIGNAL': (16, 30),
    'MORNING': (9, 0),
    'INTRADAY': (10, 30),
}


def _now_et_iso(sim_date: str | None = None, cycle: str | None = None) -> str:
    """Return time as ISO string with ET timezone.

    When *sim_date* is provided (backtest), returns that date at the
    cycle-appropriate time (EOD 16:30, MORNING 09:00, INTRADAY 10:30).
    Defaults to 16:30 if cycle is not specified.
    """
    if sim_date:
        try:
            hour, minute = _CYCLE_TIMES.get(cycle, (16, 30))
            dt = datetime.strptime(sim_date, "%Y-%m-%d").replace(
                hour=hour, minute=minute, tzinfo=_ET,
            )
            return dt.isoformat()
        except ValueError:
            pass
    return datetime.now(_ET).isoformat()


def _format_market_context(market: dict) -> str:
    """Format market context as readable text for the LLM prompt."""
    if not market:
        return "(no market data)\n"
    bd = market.get('breadth_detail', {})
    lines = [
        f"SPY: 1d={market.get('spy_return_1d', 0):+.2%}  5d={market.get('spy_return_5d', 0):+.2%}  "
        f"vol_20d={market.get('spy_realized_vol_20d', 0):.1%}",
        f"QQQ: 1d={market.get('qqq_return_1d', 0):+.2%}  5d={market.get('qqq_return_5d', 0):+.2%}",
        f"Breadth: {market.get('breadth_score', 0):.2f}  "
        f"sectors_pos_5d={bd.get('sectors_positive_5d', 0)}/11  "
        f"sectors_pos_20d={bd.get('sectors_positive_20d', 0)}/11  "
        f"IWM vs SPY 5d={bd.get('iwm_vs_spy_5d') or 0:+.2%}  "
        f"credit={bd.get('credit_trend', '?')}",
    ]
    sm = market.get('sector_momentum', {})
    if sm:
        sector_rows = []
        for etf, data in sm.items():
            sector_rows.append(
                f"  {etf:<4}  5d={data.get('return_5d', 0):>+6.2%}  "
                f"20d={data.get('return_20d', 0):>+6.2%}  "
                f"rank={data.get('rank', 0)}"
            )
        lines.append("Sector momentum:")
        lines.extend(sector_rows)
    return "\n".join(lines) + "\n"


def _format_portfolio_summary(portfolio: dict, portfolio_heat: float) -> str:
    """Format portfolio summary as readable text for the LLM prompt."""
    if not portfolio:
        return "(no portfolio data)\n"
    lines = [
        f"Positions: {portfolio.get('position_count', 0)}  "
        f"Cash: ${portfolio.get('cash', 0):,.0f}  "
        f"Portfolio Value: ${portfolio.get('portfolio_value', 0):,.0f}  "
        f"Cash Ratio: {portfolio.get('cash_ratio', 0):.1%}",
        f"Portfolio Beta: {portfolio.get('portfolio_beta', 1.0):.2f}  "
        f"Avg Correlation: {portfolio.get('avg_pairwise_correlation', 0):.3f}"
        + (" (stress-adjusted)" if portfolio.get('correlation_stress_adjusted') else "")
        + f"  Heat: {portfolio_heat:.1%}",
    ]
    se = portfolio.get('sector_exposure', {})
    if se:
        parts = [f"{s}: {w:.1%}" for s, w in se.items()]
        lines.append(f"Sector exposure: {', '.join(parts)}")
    betas = portfolio.get('position_betas', {})
    if betas:
        parts = [f"{t}: {b:.2f}" for t, b in betas.items()]
        lines.append(f"Position betas: {', '.join(parts)}")
    sm = portfolio.get('strategy_mix', {})
    if sm:
        parts = [f"{s}: {d['count']}pos {d['weight_pct']:.1%}" for s, d in sm.items()]
        lines.append(f"Strategy mix: {', '.join(parts)}")
    ep = portfolio.get('exposure_projection', {})
    if ep:
        lines.append(
            f"Exposure projection: current={ep.get('current_invested_pct', 0):.1%}  "
            f"projected_heat={ep.get('projected_portfolio_heat', 0):.1%}  "
            f"candidates={ep.get('candidate_count', 0)}"
        )
    return "\n".join(lines) + "\n"


def _fmt_vol(v: float | None) -> str:
    return f"{v:>4.1f}x" if v is not None else "   - "


def _format_positions_table(positions: dict) -> str:
    """Format existing positions grouped by strategy, matching candidate style.

    Each strategy group has a summary table (scannable key metrics) and
    per-ticker detail lines (secondary context for deep evaluation).
    """
    if not positions:
        return "(no positions)\n"

    mom_pos = {t: c for t, c in positions.items() if c.get('strategy') == 'MOMENTUM'}
    mr_pos = {t: c for t, c in positions.items() if c.get('strategy') != 'MOMENTUM'}

    sections = []
    if mom_pos:
        sections.append(
            f"── MOMENTUM ({len(mom_pos)}) ──\n"
            + _format_position_group(mom_pos, strategy='MOM')
        )
    if mr_pos:
        sections.append(
            f"── MEAN REVERSION ({len(mr_pos)}) ──\n"
            + _format_position_group(mr_pos, strategy='MR')
        )
    return "\n".join(sections) + "\n"


def _format_position_group(positions: dict, strategy: str = 'MOM') -> str:
    """Format a strategy group of positions as table + detail lines."""
    is_mom = strategy == 'MOM'
    zscore_col = 'Mom_z ' if is_mom else 'MR_z  '
    signal_col = 'ADX  ' if is_mom else 'R:R '
    header = (
        f"Ticker   | Sector          | Price   | Entry   | P&L    | DD/Peak | Day | Stop(%)     | {signal_col} | {zscore_col} | RSI   | VolR  | Stage\n"
        f"---------|-----------------|---------|---------|--------|---------|-----|-------------|-------|--------|-------|-------|------"
    )
    rows = []
    for ticker, ctx in positions.items():
        pnl_pct = ctx.get('unrealized_pnl_pct', 0) or 0
        stage = (ctx.get('weekly') or {}).get('weinstein_stage') or '?'
        stop = ctx.get('stop_loss_price', 0)
        stop_dist = ctx.get('stop_distance_pct', 0) or 0
        zscore_val = ctx.get('momentum_zscore', 0) if is_mom else ctx.get('mean_reversion_zscore', 0)
        signal_val = (ctx.get('adx') or 0) if is_mom else (ctx.get('risk_reward_remaining') or 0)
        # Build flags
        flags = []
        partials = ctx.get('partial_exit_count', 0)
        if partials:
            flags.append(f"PARTIAL {partials}×")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        hwm_dd = ctx.get('high_watermark_drawdown_pct', 0) or 0
        rows.append(
            f"{ticker:<8} | {ctx.get('sector', '?'):<15} "
            f"| ${ctx.get('current_price', 0):>7.2f} | ${ctx.get('entry_price', 0):>7.2f} "
            f"| {pnl_pct:>+5.1%} | {hwm_dd:>5.1%} | {ctx.get('holding_days', 0):>3} "
            f"| ${stop:>6.2f}({stop_dist:>4.1%}) "
            f"| {signal_val:>5.1f} "
            f"| {zscore_val or 0:>+6.2f} "
            f"| {ctx.get('rsi', 0):>5.1f} "
            f"| {_fmt_vol(ctx.get('volume_ratio'))} "
            f"| {stage:>5}{flag_str}"
        )

    detail_lines = []
    for ticker, ctx in positions.items():
        w = ctx.get('weekly') or {}
        parts = [f"  {ticker}:"]
        if is_mom:
            parts.append(f"adx_3d={ctx.get('adx_change_3d', 0) or 0:+.1f}")
        parts.append(f"macd={ctx.get('macd_crossover', 'none')}")
        parts.append(f"weekly_trend={w.get('weekly_trend_score', 0) or 0:+.2f}")
        parts.append(f"20ma={ctx.get('price_vs_20ma_pct', 0) or 0:+.1%}")
        parts.append(f"1d={ctx.get('return_1d', 0) or 0:+.1%}")
        parts.append(f"5d={ctx.get('return_5d', 0) or 0:+.1%}(vs SPY {ctx.get('return_5d_vs_spy', 0) or 0:+.1%})")
        dt = ctx.get('deterioration_tracker')
        if dt and dt.get('peak_pnl_pct', 0) > 1.0:
            parts.append(f"peak_pnl={dt['peak_pnl_pct']:+.1f}%")
        # Trajectory deltas (3-day indicator changes)
        traj_parts = []
        rsi_d = ctx.get('rsi_delta_3d')
        if rsi_d is not None:
            traj_parts.append(f"rsi_Δ3d={rsi_d:+.1f}")
        mht = ctx.get('macd_hist_trend')
        if mht:
            traj_parts.append(f"macd_hist={mht}")
        vt = ctx.get('volume_trend_3d')
        if vt is not None:
            traj_parts.append(f"vol_trend={vt:.2f}")
        if traj_parts:
            parts.append("Δ3d:" + " ".join(traj_parts))
        line = "  ".join(parts)
        # Conditional tags
        if w.get('stage_jump'):
            line += f"  [STAGE_JUMP {w.get('stage_prior', '?')}->{w.get('weinstein_stage', '?')}]"
        if ctx.get('regime_changed_since_entry'):
            line += f"  [REGIME_SHIFT {ctx.get('entry_regime', '?')}->{ctx.get('current_regime', '?')}]"
        ec = ctx.get('earnings_context')
        if ec and isinstance(ec, dict):
            eh = ec.get('earnings_history', {})
            ep = [f"in {ec.get('earnings_days_away', '?')}d"]
            if eh:
                ep.append(f"avg_gap={eh.get('avg_abs_gap', 0)}%")
                ep.append(f"max_gap={eh.get('max_abs_gap', 0)}%")
            if ec.get('cushion_vs_avg') is not None:
                ep.append(f"cushion={ec['cushion_vs_avg']}x avg")
            line += f"  [EARNINGS {' '.join(ep)}]"
        dt = ctx.get('deterioration_tracker')
        if dt and dt.get('consecutive_lower_closes', 0) >= 3:
            line += (
                f"  [DETERIORATING: {dt['consecutive_lower_closes']}d lower closes, "
                f"dd_from_peak={dt.get('drawdown_from_peak_pct', 0):.1f}%]"
            )
        r_risk = ctx.get('research_risk_level', 'none')
        r_summary = ctx.get('research_summary', '')
        if r_risk in ('flag', 'veto'):
            line += f"  research[{r_risk.upper()}]: {r_summary[:120]}"
        elif r_summary:
            line += f"  research[NONE]: {r_summary[:80]}"
        detail_lines.append(line)

    return header + "\n" + "\n".join(rows) + "\n" + "\n".join(detail_lines)


def _format_candidates_table(candidates: dict) -> str:
    """Format new entry candidates grouped by strategy type for the LLM prompt.

    Separates MOM and MR candidates so the PM can evaluate each setup type
    on its own merits before making cross-strategy portfolio decisions.
    """
    if not candidates:
        return "(no candidates)\n"

    # Split into MOM and MR groups
    mom_cands = {t: c for t, c in candidates.items() if c.get('strategy') == 'MOMENTUM'}
    mr_cands = {t: c for t, c in candidates.items() if c.get('strategy') != 'MOMENTUM'}

    sections = []
    if mom_cands:
        sections.append(f"── MOMENTUM SETUPS ({len(mom_cands)}) — trend continuation / breakout ──\n"
                        + _format_candidate_group(mom_cands, strategy='MOM'))
    if mr_cands:
        sections.append(f"── MEAN REVERSION SETUPS ({len(mr_cands)}) — oversold bounce / pullback ──\n"
                        + _format_candidate_group(mr_cands, strategy='MR'))

    return "\n".join(sections) + "\n"


def _format_candidate_group(candidates: dict, strategy: str = 'MOM') -> str:
    """Format a group of candidates (MOM or MR) as table + details.

    MOM uses ADX (trend directionality) instead of R:R because ATR-based
    R:R is structurally constant (~1.5) for momentum candidates. ADX
    measures whether the price movement is a directional trend vs noise —
    information not captured by the screening pipeline (which uses return
    magnitude). MR keeps R:R which varies naturally via the MA20 target.
    """
    # Strategy-specific columns: MOM shows Mom_z + ADX, MR shows MR_z + R:R
    is_mom_group = strategy == 'MOM'
    zscore_col = 'Mom_z ' if is_mom_group else 'MR_z  '
    quality_col = 'ADX ' if is_mom_group else 'R:R '
    header = (
        f"Ticker   | Strategy | Sector          | Price    | {zscore_col} | RSI   | VolR  | {quality_col} | Shares | Stage | Flags\n"
        f"---------|----------|-----------------|----------|--------|-------|-------|------|--------|-------|------"
    )
    rows = []
    for ticker, ctx in candidates.items():
        flags = ctx.get('signal_flags', {})
        flag_strs = []
        if flags.get('volume_confirming'):
            flag_strs.append('vol')
        if flags.get('macd_confirming'):
            flag_strs.append('macd')
        if flags.get('bollinger_extended'):
            flag_strs.append('BB')
        if flags.get('recent_spike'):
            flag_strs.append('spike')
        if flags.get('unexplained_move'):
            sector_5d = ctx.get('sector_return_5d')
            if sector_5d is not None and sector_5d < -0.02:
                flag_strs.append(f'unexpl(sector {sector_5d:+.1%})')
            else:
                flag_strs.append('unexpl')
        if flags.get('above_20ma'):
            flag_strs.append('>20ma')
        # Research risk level as a flag
        research_risk = ctx.get('research_risk_level', 'none')
        if research_risk == 'veto':
            flag_strs.append('VETO!')
        elif research_risk == 'flag':
            flag_strs.append('FLAG!')
        # System constraint flags (visible to PM instead of silent removal)
        if ctx.get('watchlist_entry'):
            flag_strs.append('WATCH')
        if ctx.get('sector_capped'):
            sw = ctx.get('sector_current_weight', 0)
            flag_strs.append(f'SEC!{sw:.0%}')
        if ctx.get('corr_heat_capped'):
            flag_strs.append('CORR!')
        stage = (ctx.get('weekly') or {}).get('weinstein_stage') or '?'
        ticker_strategy = ctx.get('strategy', '?')
        strategy_short = 'MR' if ticker_strategy == 'MEAN_REVERSION' else 'MOM' if ticker_strategy == 'MOMENTUM' else '?'
        # ADX for MOM (trend directionality), R:R for MR
        is_mom = ticker_strategy == 'MOMENTUM'
        quality_val = ctx.get('adx', 0.0) if is_mom else ctx.get('rr_ratio', 0.0)
        zscore_val = ctx.get('momentum_zscore', 0) if is_mom else ctx.get('mean_reversion_zscore', 0)
        rows.append(
            f"{ticker:<8} | {strategy_short:<8} | {ctx.get('sector', '?'):<15} "
            f"| {ctx.get('current_price', 0):>8.2f} "
            f"| {zscore_val:>+6.2f} "
            f"| {ctx.get('rsi', 0):>5.1f} "
            f"| {ctx.get('volume_ratio', 0):>5.2f} "
            f"| {quality_val:>4.1f} "
            f"| {ctx.get('indicative_shares', 0):>6} "
            f"| {stage:>5} "
            f"| {','.join(flag_strs) or '-'}"
        )
    detail_lines = []
    for ticker, ctx in candidates.items():
        w = ctx.get('weekly') or {}
        flags = ctx.get('signal_flags', {})
        research = ctx.get('research', {})
        detail_lines.append(
            f"  {ticker}: stop={ctx.get('suggested_stop_loss', 0):.2f}  "
            f"take_profit={ctx.get('suggested_take_profit', 0):.2f}  "
            f"atr={ctx.get('atr', 0):.2f}  "
            + (f"adx_3d_change={ctx.get('adx_change_3d', 0):+.1f}  " if strategy == 'MOM' else "")
            + f"price_vs_20ma={ctx.get('price_vs_20ma_pct', 0):+.1%}  "
            f"return_1d={ctx.get('return_1d', 0):+.1%}  "
            f"return_1w={ctx.get('return_1w', 0):+.1%}  "
            f"weekly_trend={w.get('weekly_trend_score', 0):+.2f}  "
            f"stop_placement={flags.get('stop_placement', '?')}"
            + (f"  Δ3d: rsi={ctx['rsi_delta_3d']:+.1f}" if ctx.get('rsi_delta_3d') is not None else "")
            + (f" macd_hist={ctx['macd_hist_trend']}" if ctx.get('macd_hist_trend') else "")
            + (f" vol_trend={ctx['volume_trend_3d']:.2f}" if ctx.get('volume_trend_3d') is not None else "")
            + (f"  earnings_in={ctx.get('earnings_days_away') or ctx.get('research_earnings_days')}d" if ctx.get('earnings_days_away') is not None or ctx.get('research_earnings_days') is not None else "")
            + (f"  portfolio_corr={ctx.get('correlation_with_portfolio', 0):.2f}" if ctx.get('correlation_with_portfolio') is not None else "")
            + (f"  research[{ctx.get('research_risk_level', 'none').upper()}]: {ctx.get('research_summary', '')[:120]}" if ctx.get('research_summary') else "")
            + (f"  [SECTOR CAP: {ctx.get('sector', '?')} already {ctx['sector_current_weight']:.0%} of portfolio]" if ctx.get('sector_capped') else "")
            + (f"  [CORR HEAT: projected {ctx.get('projected_correlated_heat', 0):.1%} with {','.join(ctx.get('correlated_cluster_tickers', []))}]" if ctx.get('corr_heat_capped') else "")
        )
        re = ctx.get('re_entry_context')
        if re:
            detail_lines.append(
                f"    [RE-ENTRY] last exit={re.get('previous_exit_date', '?')} "
                f"({re.get('days_since_exit', '?')}d ago)  "
                f"result={re.get('previous_result_pct', 0):+.1f}%  "
                f"reason={re.get('previous_exit_reason', '?')}  "
                f"price_vs_exit={re.get('price_vs_previous_exit', 0):+.1%}"
            )
    return header + "\n" + "\n".join(rows) + "\n\nDetails:\n" + "\n".join(detail_lines) + "\n"


def _drawdown_size_multiplier(drawdown_pct: float) -> float:
    """Map current drawdown percentage to a position size multiplier.

    Aligned with check_drawdown() tiers:
      0–5%:   1.0  — normal sizing
      5–10%:  0.75 — caution, 25% size reduction
      10–15%: 0.5  — warning, 50% size reduction
      ≥15%:   0.0  — halt, no new entries
    """
    if drawdown_pct >= 0.15:
        return 0.0
    if drawdown_pct >= 0.10:
        return 0.5
    if drawdown_pct >= 0.05:
        return 0.75
    return 1.0


def _apply_reentry_cooldown(
    candidates: list[str],
    trade_history: list,
    cooldown_days: int = 3,
) -> tuple[list[str], list[str]]:
    """Remove candidates that were exited within the cooldown window.

    Returns (filtered_candidates, removed_tickers).
    """
    if cooldown_days <= 0 or not trade_history:
        return candidates, []

    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)

    # Collect tickers exited after the cutoff
    recent_exits: set[str] = set()
    for t in trade_history:
        ts = getattr(t, 'timestamp', None)
        if not ts:
            continue
        try:
            exit_dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            continue
        if exit_dt >= cutoff:
            recent_exits.add(getattr(t, 'symbol', ''))

    if not recent_exits:
        return candidates, []

    filtered = [c for c in candidates if c not in recent_exits]
    removed = [c for c in candidates if c in recent_exits]
    return filtered, removed


def _apply_reject_blackout(
    candidates: list[str],
    decision_log: list,
    blackout_days: int = 2,
    sim_date: str | None = None,
) -> tuple[list[str], list[str]]:
    """Remove candidates that were REJECTed in MORNING within the blackout window.

    Prevents the loop: EOD LONG → Morning REJECT → next EOD LONG again.
    Uses trading days (unique dates in decision_log) to handle weekends.
    Returns (filtered_candidates, removed_tickers).
    """
    if blackout_days <= 0 or not decision_log:
        return candidates, []

    from datetime import datetime

    if sim_date:
        ref_date = datetime.strptime(sim_date, "%Y-%m-%d").date()
    else:
        ref_date = datetime.utcnow().date()

    # Build trading day calendar from decision_log dates
    trading_dates = sorted({
        entry.get("date", "")[:10]
        for entry in decision_log
        if entry.get("date", "")
    })

    def _trading_days_between(d1_str: str, d2_str: str) -> int:
        """Count trading days strictly between d1 and d2."""
        return sum(1 for d in trading_dates if d1_str < d < d2_str)

    ref_str = ref_date.isoformat()
    recent_rejects: set[str] = set()
    for entry in reversed(decision_log):
        if entry.get("cycle") != "MORNING":
            continue
        log_date_str = entry.get("date", "")
        if not log_date_str:
            continue
        log_date_str = log_date_str[:10]
        # Use trading days, not calendar days
        if _trading_days_between(log_date_str, ref_str) > blackout_days:
            break
        for dec in entry.get("decisions", []):
            if dec.get("action", "").upper() == "REJECT":
                ticker = dec.get("ticker", "")
                if ticker:
                    recent_rejects.add(ticker)

    if not recent_rejects:
        return candidates, []

    filtered = [c for c in candidates if c not in recent_rejects]
    removed = [c for c in candidates if c in recent_rejects]
    return filtered, removed


def _apply_skip_blackout(
    candidates: list[str],
    decision_log: list,
    blackout_days: int = 5,
    sim_date: str | None = None,
    current_signals: dict[str, set[str]] | None = None,
) -> tuple[list[str], list[str]]:
    """Remove candidates that were SKIPped recently without new signals.

    Signal-based gate: a SKIPped ticker is allowed back only if it now has
    a screening signal it did NOT have at the time of the SKIP decision.
    Max blackout (blackout_days) ensures tickers eventually return even
    without new signals.

    Returns (filtered_candidates, removed_tickers).
    """
    if blackout_days <= 0 or not decision_log:
        return candidates, []

    from datetime import datetime, timedelta

    if sim_date:
        ref_date = datetime.strptime(sim_date, "%Y-%m-%d").date()
    else:
        ref_date = datetime.utcnow().date()

    # Collect recent SKIPs with their signals at skip time
    # {ticker: set of signal names at time of skip}
    recent_skips: dict[str, set[str]] = {}
    for entry in reversed(decision_log):
        log_date_str = entry.get("date", "")
        if not log_date_str:
            continue
        try:
            log_date = datetime.strptime(log_date_str[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if (ref_date - log_date).days > blackout_days:
            break
        for dec in entry.get("decisions", []):
            if dec.get("action", "").upper() == "SKIP":
                if dec.get("from_watchlist") or dec.get("implicit"):
                    continue
                ticker = dec.get("ticker", "")
                if ticker and ticker not in recent_skips:
                    skip_signals = set(dec.get("screening_signals", []))
                    recent_skips[ticker] = skip_signals

    if not recent_skips:
        return candidates, []

    removed = []
    filtered = []
    for c in candidates:
        if c not in recent_skips:
            filtered.append(c)
            continue
        # Check if any NEW signal appeared since skip
        skip_sigs = recent_skips[c]
        curr_sigs = (current_signals or {}).get(c, set())
        new_signals = curr_sigs - skip_sigs
        if new_signals:
            # New signal found — allow re-entry
            filtered.append(c)
        else:
            removed.append(c)

    return filtered, removed


def _format_date_short(date_str: str) -> str:
    """Convert '2026-03-10' to 'Mar 10'."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%b %-d")
    except (ValueError, AttributeError):
        return date_str


def build_decision_history(
    decision_log: list,
    active_positions: set[str] | None = None,
    max_days: int = 5,
) -> str:
    """Build compact action-timeline grouped by ticker.

    Shows action sequences (no reasoning — that lives in pm_notes).
    Tickers are grouped into Positions (active), Closed, and Candidates (WATCH).

    Args:
        decision_log: AgentState.decision_log list (newest entries at end).
        active_positions: Set of currently held ticker symbols. Used to
            separate active vs closed tickers. If None, all treated as active.
        max_days: Maximum number of unique trading days to include.

    Returns:
        Formatted decision history string, or empty string if no data.
    """
    if not decision_log:
        return ""

    active = {t.upper() for t in (active_positions or set())}

    # Collect unique dates (newest first), limited to max_days
    seen_dates: list[str] = []
    for entry in reversed(decision_log):
        d = entry.get("date", "")
        if d and d not in seen_dates:
            seen_dates.append(d)
        if len(seen_dates) >= max_days:
            break

    if not seen_dates:
        return ""

    date_set = set(seen_dates)

    # ── Collect per-ticker EOD actions, AM/Intra events, and exec events ──
    from collections import defaultdict

    # ticker → [(date, action, conviction, note)] chronological
    eod_actions: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    # ticker → [(date, cycle, detail)]
    sub_cycle_events: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    # ticker → (date, pnl, exit_type)  for closed positions
    close_events: dict[str, tuple[str, float, str]] = {}
    # Track regimes per date
    regimes: dict[str, str] = {}
    # Track implicit skips (candidates not addressed by PM)
    implicit_skips: dict[str, list[str]] = {}

    for entry in decision_log:
        d = entry.get("date", "")
        if d not in date_set:
            continue
        cycle = entry.get("cycle", "")

        if entry.get("regime"):
            regimes[d] = entry["regime"]

        if cycle == "EOD_SIGNAL":
            notes_snap = entry.get("notes_snapshot", {})
            for dec in (entry.get("decisions") or []):
                ticker = dec.get("ticker", "").upper()
                action = dec.get("action", "").upper()
                if not ticker:
                    continue
                if action == "SKIP":
                    skip_reason = dec.get("reason", "")
                    if skip_reason and "not addressed" in skip_reason.lower():
                        implicit_skips.setdefault(d, []).append(ticker)
                    continue
                label = action
                if action == "PARTIAL_EXIT" and dec.get("exit_pct"):
                    label = f"PARTIAL_EXIT {int(dec['exit_pct'] * 100)}%"
                elif action == "LONG" and dec.get("half_size"):
                    label = "LONG ½"
                conv = dec.get("conviction", "")
                note = notes_snap.get(ticker, "")
                eod_actions[ticker].append((d, label, conv, note))

        elif cycle in ("MORNING", "INTRADAY"):
            for dec in (entry.get("decisions") or []):
                ticker = dec.get("ticker", "").upper()
                action = (dec.get("action") or dec.get("decision") or "").upper()
                if not ticker or not action:
                    continue
                cl = "AM" if cycle == "MORNING" else "Intra"
                # Only track non-trivial actions
                if action in ("CONFIRM", "HOLD"):
                    if cycle == "INTRADAY" and action == "HOLD":
                        text = f"{dec.get('for', '')} {dec.get('against', '')}"
                        matched = [kw for kw in _INTRADAY_FLAG_KEYWORDS if kw in text]
                        if matched:
                            sub_cycle_events[ticker].append(
                                (d, cl, f"HOLD({'+'.join(sorted(matched))})")
                            )
                    continue
                sub_cycle_events[ticker].append((d, cl, action))

        elif cycle == "EXECUTION":
            for ev in (entry.get("events") or []):
                ticker = ev.get("ticker", "").upper()
                action = ev.get("action", "")
                if action in ("STOPPED_OUT", "STOP_LOSS"):
                    pnl = ev.get("pnl", 0)
                    close_events[ticker] = (d, pnl, "STOP_EXIT")
                elif action == "EXIT_FILLED":
                    pnl = ev.get("pnl", 0)
                    close_events[ticker] = (d, pnl, "EXIT")

        elif cycle == "STOP_EVENT":
            for dec in (entry.get("decisions") or []):
                ticker = dec.get("ticker", "").upper()
                pnl = dec.get("pnl", 0)
                close_events[ticker] = (d, pnl, "STOP_EXIT")

    if not eod_actions:
        return ""

    # ── Regime summary line ──
    regime_vals = list(regimes.values())
    if regime_vals and all(r == regime_vals[0] for r in regime_vals):
        regime_summary = f"Regime: {regime_vals[0]} throughout"
    elif regime_vals:
        regime_parts = []
        prev = None
        for d in sorted(regimes.keys()):
            r = regimes[d]
            if r != prev:
                regime_parts.append(f"{r}({_format_date_short(d)})")
                prev = r
        regime_summary = "Regime: " + " → ".join(regime_parts)
    else:
        regime_summary = ""

    n_days = len(seen_dates)
    lines = [f"=== ACTION LOG ({n_days} trading day{'s' if n_days > 1 else ''}) ==="]
    if regime_summary:
        lines.append(regime_summary)

    # ── Categorize tickers ──
    all_tickers = set(eod_actions.keys())
    position_tickers = sorted(all_tickers & active) if active else sorted(all_tickers)
    closed_tickers = sorted(t for t in all_tickers if t in close_events and t not in active)
    watch_tickers = sorted(
        t for t in all_tickers
        if t not in active and t not in close_events
        and any(a == "WATCH" for _, a, _, _ in eod_actions.get(t, []))
    )

    def _build_ticker_compact(ticker: str) -> list[str]:
        """Build compact action timeline (for closed/watch tickers)."""
        actions = eod_actions.get(ticker, [])
        timeline = " → ".join(a for _, a, _, _ in actions)
        result = [f"  {ticker}: {timeline}"]
        sub = sub_cycle_events.get(ticker, [])
        if sub:
            parts = [f"{cl}:{detail}({_format_date_short(d)})" for d, cl, detail in sub]
            result.append(f"    [{', '.join(parts)}]")
        return result

    def _build_ticker_detailed(ticker: str) -> list[str]:
        """Build action timeline + latest note only (for active positions)."""
        actions = eod_actions.get(ticker, [])
        result = [f"  {ticker}:"]
        # Action sequence (all days) but note only on the most recent day
        for i, (d, action, conv, note) in enumerate(actions):
            ds = _format_date_short(d)
            is_last = (i == len(actions) - 1)
            note_str = f' — "{note}"' if note and is_last else ""
            result.append(f"    {ds}: {action}{note_str}")
        # Sub-cycle annotations
        sub = sub_cycle_events.get(ticker, [])
        if sub:
            parts = [f"{cl}:{detail}({_format_date_short(d)})" for d, cl, detail in sub]
            result.append(f"    [{', '.join(parts)}]")
        return result

    # ── Positions section ──
    if position_tickers:
        lines.append("\n── Positions ──")
        for t in position_tickers:
            lines.extend(_build_ticker_detailed(t))

    # ── Closed section ──
    if closed_tickers:
        lines.append("\n── Closed ──")
        for t in closed_tickers:
            actions = eod_actions.get(t, [])
            d, pnl, exit_type = close_events[t]
            timeline = " → ".join(a for _, a, _, _ in actions)
            lines.append(
                f"  {t}: {timeline} → {exit_type}({_format_date_short(d)}, P&L ${pnl:+,.0f})"
            )

    # ── Watch section ──
    if watch_tickers:
        lines.append("\n── Watch ──")
        for t in watch_tickers:
            lines.extend(_build_ticker_compact(t))

    # ── Implicit skips (candidates not addressed) ──
    if implicit_skips:
        # Show most recent day's implicit skips only
        latest_date = max(implicit_skips.keys())
        skipped = implicit_skips[latest_date]
        if skipped:
            lines.append(f"\n── Not addressed ({_format_date_short(latest_date)}): "
                         + ", ".join(sorted(skipped)) + " ──")

    lines.append("")
    return "\n".join(lines)


_INTRADAY_FLAG_KEYWORDS = frozenset({
    "SHARP_DROP", "STOP_IMMINENT", "UNUSUAL_VOLUME",
    "MARKET_SHOCK", "PROFIT_REVIEW", "NEWS_ALERT",
})


def _extract_playbook_reads(agent_obj, since_msg_idx: int = 0) -> list[str]:
    """Extract playbook topics read by PM from Strands agent messages.

    First checks if CycleAwareConversationManager cached the reads
    (messages are cleared after each cycle), then falls back to scanning
    live messages.

    Args:
        agent_obj: The BaseAgent instance (accesses .agent.messages).
        since_msg_idx: Only scan messages from this index onward.
    """
    # Check cached reads from CycleAwareConversationManager (messages cleared after cycle)
    try:
        conv_mgr = getattr(agent_obj.agent, 'conversation_manager', None)
        cached = getattr(conv_mgr, 'last_cycle_playbook_reads', None)
        if cached is not None and len(cached) > 0:
            return cached
    except Exception:
        pass

    # Fallback: scan live messages (for non-LACM or mid-cycle extraction)
    topics: list[str] = []
    try:
        messages = getattr(agent_obj.agent, 'messages', [])
        for msg in messages[since_msg_idx:]:
            for block in msg.get('content', []):
                tu = block.get('toolUse', {})
                if tu.get('name') == 'read_playbook':
                    topic = tu.get('input', {}).get('topic', '')
                    topics.append(topic if topic else '(overview)')
    except Exception:
        pass
    return topics
