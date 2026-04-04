"""
backtest/common.py — Shared utilities for backtesting.

Contains SimClock, session helpers, and the core EOD day-loop logic used
by backtest.py (cold start and snapshot resume).

MockBroker has been moved to providers/mock_broker.py.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd

from agents._formatting import _extract_playbook_reads
from state.portfolio_state import PortfolioState, Position

logger = logging.getLogger(__name__)

SESSIONS_DIR = Path("backtest/sessions")


class SessionStoppedError(Exception):
    """Raised when the session has been stopped by the user."""
    pass


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def sanitise_numpy(obj: Any) -> Any:
    """Recursively convert numpy scalar types to native Python types in-place."""
    import numpy as np
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (np.bool_, np.integer, np.floating)):
                obj[k] = v.item()
            elif isinstance(v, (dict, list)):
                sanitise_numpy(v)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, (np.bool_, np.integer, np.floating)):
                obj[i] = v.item()
            elif isinstance(v, (dict, list)):
                sanitise_numpy(v)
    return obj


def session_dir(run_id: str) -> Path:
    d = SESSIONS_DIR / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def load_json(path: Path) -> Any:
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# SimClock
# ---------------------------------------------------------------------------

class SimClock:
    def __init__(self, trading_days: list[str]) -> None:
        self.trading_days = trading_days
        self._idx = 0

    @property
    def today(self) -> str:
        return self.trading_days[self._idx]

    @property
    def yesterday(self) -> str:
        return self.trading_days[self._idx - 1] if self._idx > 0 else self.trading_days[0]

    def advance(self) -> bool:
        if self._idx < len(self.trading_days) - 1:
            self._idx += 1
            return True
        return False

    @property
    def day_number(self) -> int:
        return self._idx + 1


# ---------------------------------------------------------------------------
# Screener (deterministic, pure Python on bars)
# ---------------------------------------------------------------------------

def _get_blackout_excluded(sim_date: str = "") -> set[str]:
    """Return tickers in an active skip blackout period.

    Every SKIP gets a 1-day blackout (configurable via skip_blackout_days).
    The ticker is excluded from screening until the blackout expires.
    """
    try:
        from state.agent_state import get_state
        from config.settings import get_settings
    except ImportError:
        return set()

    if not sim_date:
        return set()

    state = get_state()
    blackout_days = get_settings().skip_blackout_days
    excluded: set[str] = set()
    # Check most recent decision per ticker from decision_log.
    seen: set[str] = set()
    for entry in reversed(getattr(state, 'decision_log', [])):
        for dec in entry.get('decisions', []):
            ticker = dec.get('ticker', '').upper()
            if not ticker or ticker in seen:
                continue
            if dec.get('action') != 'SKIP':
                seen.add(ticker)
                continue
            seen.add(ticker)
            skip_date = entry.get('date', '')
            if not skip_date:
                continue
            trading_days_since = _count_trading_days_between(
                skip_date, sim_date, state,
            )
            if trading_days_since <= blackout_days:
                excluded.add(ticker)
    return excluded


def _count_trading_days_between(
    start_date: str, end_date: str, state: object,
) -> int:
    """Count trading days between two dates using cycle_logs as calendar."""
    dates: set[str] = set()
    for log in getattr(state, 'cycle_logs', []):
        d = log.get('date', '')
        if d and start_date < d <= end_date:
            dates.add(d)
    return len(dates)


def _get_staleness_excluded(sim_date: str = "") -> set[str]:
    """Return tickers that appeared as candidates N+ consecutive days without
    being selected (LONG or WATCH) by the PM.

    This prevents the same oversold stock from consuming candidate slots
    day after day when the PM has no interest. The ticker re-enters the
    pool after the cooldown period (skip_blackout_max_days).
    """
    try:
        from state.agent_state import get_state
        from config.settings import get_settings
    except ImportError:
        return set()

    state = get_state()
    settings = get_settings()
    threshold = settings.candidate_staleness_threshold
    if threshold <= 0:
        return set()

    # Build per-date EOD decisions from decision_log
    selected_actions = {'LONG', 'WATCH'}
    date_decisions: dict[str, set[str]] = {}  # date -> set of selected tickers
    for entry in state.decision_log:
        if entry.get('cycle') != 'EOD_SIGNAL':
            continue
        d = entry.get('date', '')
        if not d:
            continue
        if d not in date_decisions:
            date_decisions[d] = set()
        for dec in entry.get('decisions', []):
            if dec.get('action', '').upper() in selected_actions:
                date_decisions[d].add(dec.get('ticker', '').upper())

    # Walk backwards through EOD cycle_logs (which have candidate_tickers)
    ticker_streak: dict[str, int] = {}
    eod_days_seen = 0
    max_lookback = settings.skip_blackout_days + threshold
    for log in reversed(state.cycle_logs):
        if log.get('cycle') != 'EOD_SIGNAL':
            continue
        eod_days_seen += 1
        if eod_days_seen > max_lookback:
            break

        candidate_tickers = set(log.get('candidate_tickers') or [])
        if not candidate_tickers:
            continue

        d = log.get('date', '')
        selected_this_day = date_decisions.get(d, set())

        for ticker in candidate_tickers:
            if ticker in selected_this_day:
                ticker_streak.pop(ticker, None)
            else:
                ticker_streak[ticker] = ticker_streak.get(ticker, 0) + 1

    excluded = {t for t, count in ticker_streak.items() if count >= threshold}
    return excluded


def screen_universe(
    bars: dict[str, pd.DataFrame],
    sim_date: str,
    extra_exclude: set[str] | None = None,
) -> list[str]:
    from config.settings import get_settings
    from tools.data.screener import _avg_volume, _atr_pct, _multi_signal_screen, _passes_structural, _DUAL_CLASS_DROP
    from tools.quant.market_breadth import ALL_BREADTH_TICKERS

    s = get_settings()
    exclude = {'SPY', 'QQQ'} | set(ALL_BREADTH_TICKERS) | _DUAL_CLASS_DROP | (extra_exclude or set())
    liquid = [
        t for t, df in bars.items()
        if t not in exclude and len(df) >= 5
        and _avg_volume(df) >= s.screener_min_avg_volume
    ]
    volatile = [
        t for t in liquid
        if s.screener_min_atr_pct <= _atr_pct(bars[t]) <= s.screener_max_atr_pct
    ]
    structural = [t for t in volatile if _passes_structural(bars[t])]
    # Stage 4: multi-signal screen (ranking, not hard filter)
    # Overbought/R:R are handled as ranking factors in the quant engine,
    # not as hard filters — the PM should see the full signal spectrum.
    candidates = _multi_signal_screen(structural, bars, n=s.screener_momentum_candidates)
    logger.info("Screener (%s): %d liquid -> %d volatile -> %d structural -> %d signal",
                sim_date, len(liquid), len(volatile), len(structural), len(candidates))
    return candidates


# ---------------------------------------------------------------------------
# Core EOD logic
# ---------------------------------------------------------------------------

def build_quant_context(
    agent_settings: Any,
    existing_positions: dict,
    candidates: list[str],
    broker: Any,
    bars: dict[str, pd.DataFrame],
    earnings_map: dict[str, int] | None,
    trade_history: list | None = None,
    sector_map: dict | None = None,
    watchlist_tickers: list[str] | None = None,
) -> dict:
    """Build quant context using QuantEngine (pure Python, no API calls)."""
    from agents.quant_engine import QuantEngine
    from tools.quant.market_breadth import ALL_BREADTH_TICKERS

    quant = QuantEngine(settings=agent_settings)
    all_tickers = list(set(
        list(existing_positions.keys()) + candidates
        + ['SPY', 'QQQ'] + ALL_BREADTH_TICKERS
    ))
    filtered_bars = {t: bars[t] for t in all_tickers if t in bars and not bars[t].empty}

    with patch('agents.quant_engine._get_sector_map', return_value=sector_map or {}):
        quant_ctx = quant.build_eod_context(
            existing_positions=existing_positions,
            candidates=candidates,
            portfolio_cash=broker.cash,
            portfolio_value=broker.portfolio_value,
            bars=filtered_bars,
            earnings_map=earnings_map or None,
            trade_history=trade_history or [],
            watchlist_tickers=watchlist_tickers or [],
        )

    sanitise_numpy(quant_ctx)
    return quant_ctx


def compute_risk_state(
    agent_settings: Any,
    portfolio_state: PortfolioState,
    broker: Any,
) -> tuple[dict, float, bool]:
    """Compute drawdown, size_multiplier, new_entries_allowed."""
    from tools.risk.drawdown import check_drawdown
    from agents._formatting import _drawdown_size_multiplier

    dd = check_drawdown(
        current_value=broker.portfolio_value,
        peak_value=broker.peak_value,
        max_drawdown_pct=agent_settings.max_drawdown_pct,
    )
    size_multiplier = _drawdown_size_multiplier(dd['current_drawdown_pct'])
    new_entries_allowed = size_multiplier > 0
    return dd, size_multiplier, new_entries_allowed


def _has_news(ticker: str, news_data: dict) -> bool:
    """Check if a ticker has any news articles in the provided data."""
    ticker_news = news_data.get(ticker.upper(), {})
    return ticker_news.get('article_count', 0) > 0


def _extract_price_context(quant_ctx: dict, bars: dict | None) -> dict:
    """Extract today's price action per ticker for research prompts."""
    price_ctx: dict[str, dict] = {}
    # From positions
    for ticker, ctx in quant_ctx.get('positions', {}).items():
        entry = {'current_price': ctx.get('current_price')}
        if ctx.get('volume_ratio') is not None:
            entry['volume_ratio'] = ctx['volume_ratio']
        price_ctx[ticker] = entry
    # From candidates
    for ticker, ctx in quant_ctx.get('candidates', {}).items():
        entry = {'current_price': ctx.get('current_price')}
        if ctx.get('volume_ratio') is not None:
            entry['volume_ratio'] = ctx['volume_ratio']
        price_ctx[ticker] = entry
    # Compute daily return from bars
    if bars:
        for ticker, entry in price_ctx.items():
            df = bars.get(ticker)
            if df is not None and len(df) >= 2:
                prev_close = df['close'].iloc[-2]
                cur_close = df['close'].iloc[-1]
                if prev_close > 0:
                    entry['daily_return_pct'] = round((cur_close / prev_close) - 1.0, 4)
    return price_ctx


def run_research(
    agent: Any,
    position_tickers: list[str],
    candidate_tickers: list[str],
    news_data: dict,
    earnings_map: dict,
    new_entries_allowed: bool,
    backtest_mode: bool = True,
    prev_day_news: dict | None = None,  # kept for caller compat, unused
    sim_date: str | None = None,
    price_context: dict | None = None,
) -> dict:
    """Run research triage + LLM for triggered tickers.

    Backtest triage (news-based):
      1. Ticker has today's news → trigger research with today's news
      2. No today's news + fresh prior research → reuse
      3. No news at all → skip (PM decides on quant data alone)
    """
    from tools.journal.research_log import load_research_history

    # NOTE: article cache is already populated by score_news_for_window()
    # in _get_data(). Do NOT clear/repopulate here — that would replace
    # full compact articles (with description, URL) with top-3 raw_articles.

    researcher = agent._get_researcher()
    all_research: dict = {}

    # Triage: news-based for backtest
    triggered_positions = []
    triggered_candidates = []

    _RESEARCH_TTL_DAYS = 5  # reuse prior research only if fresher than this

    def _triage(ticker: str) -> bool:
        """Return True if ticker should be researched."""
        # 1. Today's news exists → trigger
        if _has_news(ticker, news_data):
            return True
        # 2. No today's news, but has prior research → reuse if fresh
        prior = load_research_history(ticker, last_n=1)
        if prior:
            prior_entry = prior[0]
            prior_date = prior_entry.get('date', '')
            # Staleness check: reuse only within TTL
            if prior_date and sim_date:
                try:
                    age = (datetime.strptime(sim_date, '%Y-%m-%d')
                           - datetime.strptime(prior_date, '%Y-%m-%d')).days
                except ValueError:
                    age = 0
                if age > _RESEARCH_TTL_DAYS:
                    # Stale — neutral placeholder preserving risk_level only
                    all_research[ticker] = {
                        'summary': f'No recent news (prior research: {prior_date}).',
                        'risk_level': prior_entry.get('risk_level', 'none'),
                        'date': prior_date,
                    }
                    return False
            all_research[ticker] = prior_entry
            return False
        # 3. No news at all → skip (PM decides on quant data alone)
        return False

    for ticker in position_tickers:
        if _triage(ticker):
            triggered_positions.append(ticker)

    for ticker in candidate_tickers:
        if _triage(ticker):
            triggered_candidates.append(ticker)

    print(f"  [RESEARCH] triage: positions {len(triggered_positions)}/{len(position_tickers)}, "
          f"candidates {len(triggered_candidates)}/{len(candidate_tickers)} triggered", flush=True)

    if triggered_positions:
        try:
            pos_res = researcher.eod_research_positions(
                triggered_positions,
                pre_fetched_news=news_data,
                earnings_map=earnings_map or None,
                sim_date=sim_date,
                price_context=price_context,
            )
            all_research.update({k: v for k, v in pos_res.items() if v is not None})
        except Exception as exc:
            logger.warning("Position research failed: %s", exc)

    if triggered_candidates and new_entries_allowed:
        try:
            cand_res = researcher.eod_research_candidates(
                triggered_candidates,
                pre_fetched_news=news_data,
                earnings_map=earnings_map or None,
                sim_date=sim_date,
                price_context=price_context,
            )
            all_research.update({k: v for k, v in cand_res.items() if v is not None})
        except Exception as exc:
            logger.warning("Candidate research failed: %s", exc)

    # Attach token usage metadata
    all_research['_meta'] = {
        'model_id': agent.settings.bedrock_model_id,
        'research_token_usage': researcher.get_token_usage(),
        'triggered_positions': len(triggered_positions),
        'triggered_candidates': len(triggered_candidates),
    }
    return all_research


def inject_research_into_context(quant_ctx: dict, research: dict) -> None:
    """Inject research results into quant context."""
    for ticker in list(quant_ctx.get('positions', {})):
        r = research.get(ticker)
        if isinstance(r, dict):
            quant_ctx['positions'][ticker]['research_summary'] = r.get('summary', '')
            quant_ctx['positions'][ticker]['research_risk_level'] = r.get('risk_level', 'none')
            quant_ctx['positions'][ticker]['research_earnings_days'] = r.get('earnings_days')
            quant_ctx['positions'][ticker]['research_facts'] = r.get('facts', [])
            if r.get('date'):
                quant_ctx['positions'][ticker]['research_date'] = r['date']

    for ticker in list(quant_ctx.get('candidates', {})):
        r = research.get(ticker)
        if isinstance(r, dict):
            quant_ctx['candidates'][ticker]['research_summary'] = r.get('summary', '')
            quant_ctx['candidates'][ticker]['research_risk_level'] = r.get('risk_level', 'none')
            quant_ctx['candidates'][ticker]['research_earnings_days'] = r.get('earnings_days')
            quant_ctx['candidates'][ticker]['research_facts'] = r.get('facts', [])
            if r.get('date'):
                quant_ctx['candidates'][ticker]['research_date'] = r['date']


def run_eod_llm(
    agent: Any,
    quant_ctx: dict,
    new_entries_allowed: bool,
    existing_positions: dict,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    """Run PM LLM for EOD cycle. Returns (decisions, entry_signals, exit_signals, meta)."""
    from tools.journal.decision_log import consume_cycle_decisions, try_rescue_from_text

    prompt = agent._build_eod_prompt(
        quant_ctx, new_entries_allowed,
    )

    print(f"  [LLM] Sending EOD prompt ({len(prompt)} chars, "
          f"{len(quant_ctx['positions'])} positions, "
          f"{len(quant_ctx['candidates'])} candidates)...", flush=True)

    agent.reset_agent()  # Fresh Strands Agent per LLM call (prevents OOM)

    # Track message index before LLM call for playbook extraction
    msg_idx_before = len(getattr(agent.agent, 'messages', []))

    agent._swap_submit_tool('EOD_SIGNAL')
    llm_text = agent.run(prompt)

    # Context window overflow → trim candidates and retry
    if '"error"' in llm_text and 'context length' in llm_text.lower():
        candidates = quant_ctx.get('candidates', {})
        if candidates:
            keep = max(2, len(candidates) // 2)
            trimmed = dict(list(candidates.items())[:keep])
            quant_ctx['candidates'] = trimmed
            print(f"  [LLM] Context overflow — trimmed candidates to {keep}, retrying...", flush=True)
            prompt = agent._build_eod_prompt(quant_ctx, new_entries_allowed)
            agent.reset_agent()
            msg_idx_before = len(getattr(agent.agent, 'messages', []))
            agent._swap_submit_tool('EOD_SIGNAL')
            llm_text = agent.run(prompt)

    try_rescue_from_text(llm_text)
    decisions = consume_cycle_decisions()

    if not decisions:
        print("  [LLM] WARNING: no decisions submitted, retrying...", flush=True)
        llm_text = agent.run(
            prompt + "\n\nIMPORTANT: You MUST call submit_eod_decisions() with a JSON array "
            "of all your decisions. Even if you have no changes, submit an empty array [].",
        )
        try_rescue_from_text(llm_text)
        decisions = consume_cycle_decisions()

    if not decisions:
        print("  [LLM] ERROR: no decisions submitted after retry", flush=True)
        return [], [], [], {}

    # Inject synthetic HOLD for positions the LLM didn't mention
    mentioned = {d.get('ticker', '').upper() for d in decisions}
    pos_contexts = quant_ctx.get('positions', {})
    for ticker, pos in existing_positions.items():
        if ticker.upper() not in mentioned:
            conv = getattr(pos, 'last_conviction', '') or 'medium'
            ctx = pos_contexts.get(ticker, {})
            pnl = ctx.get('unrealized_pnl_pct', 0) or 0
            days = ctx.get('holding_days', 0)
            decisions.append({
                'ticker': ticker,
                'action': 'HOLD',
                'conviction': conv,
                'notes': f"Day {days}, P&L {pnl:+.1%}. No change — maintaining current position.",
            })
            print(f"  [FIX] Injecting implicit HOLD for {ticker}", flush=True)

    # Fix misclassified HOLD on candidates → WATCH
    candidate_tickers = set(quant_ctx.get('candidates', {}).keys())
    for d in decisions:
        action = d.get('action', '').upper()
        ticker = d.get('ticker', '').upper()
        if action == 'HOLD' and ticker not in existing_positions and ticker in candidate_tickers:
            print(f"  [FIX] Reclassifying HOLD→WATCH for candidate {ticker}", flush=True)
            d['action'] = 'WATCH'

    # Categorize
    existing_decisions = [
        d for d in decisions
        if d.get('action', '').upper() in ('HOLD', 'EXIT', 'PARTIAL_EXIT', 'TIGHTEN')
    ]
    new_entries = [
        d for d in decisions
        if d.get('action', '').upper() in ('LONG', 'SKIP', 'WATCH')
    ]

    # Print
    for dec in existing_decisions:
        print(f"  [LLM] {dec.get('ticker', '?')}: {dec.get('action', '?')} — "
              f"{dec.get('for', '')[:80]}", flush=True)
    for ent in new_entries:
        print(f"  [LLM] {ent.get('ticker', '?')}: {ent.get('action', '?')} — "
              f"{ent.get('reason', '')[:80]}", flush=True)

    # Extract signals
    exit_signals = agent._extract_eod_exit_signals(
        existing_decisions, existing_positions, quant_ctx['positions'],
    )
    entry_signals = agent._extract_eod_entry_signals(
        new_entries, quant_ctx['candidates'],
    ) if new_entries_allowed else []

    # Update position state from PM decisions (mirrors live _eod_cycle.py Step 8b)
    s = agent.settings
    for sig in exit_signals:
        pos = existing_positions.get(sig['ticker'])
        if not pos:
            continue
        conviction = sig.get('conviction', '')
        prev_conviction = pos.last_conviction
        if conviction:
            pos.last_conviction = conviction
            if conviction == 'high':
                pos.consecutive_high_conviction += 1
            else:
                pos.consecutive_high_conviction = 0
        if sig.get('action') == 'TIGHTEN':
            pos.tighten_active = True
        elif pos.tighten_active and conviction == 'high':
            pos.tighten_active = False

        # Immediately recalculate trailing stop on conviction/tighten change
        atr = sig.get('atr', 0.0)
        conviction_changed = conviction and conviction != prev_conviction
        tighten_changed = sig.get('action') == 'TIGHTEN'
        if atr > 0 and (conviction_changed or tighten_changed):
            if pos.tighten_active:
                mult = 1.5
            elif pos.strategy == 'MOMENTUM' and pos.last_conviction:
                _conv_mult = {'high': s.atr_stop_multiplier, 'medium': 1.75, 'low': 1.5}
                mult = _conv_mult.get(pos.last_conviction, s.atr_stop_multiplier)
            else:
                mult = s.atr_stop_multiplier
            hwm = pos.highest_close if pos.highest_close > 0 else pos.current_price
            new_stop = round(hwm - mult * atr, 2)
            if new_stop >= pos.current_price:
                new_stop = pos.stop_loss_price
            if new_stop > pos.stop_loss_price:
                pos.stop_loss_price = new_stop

    # Auto-ADD: half-size positions with high conviction get scaled to full
    auto_add_signals = agent._generate_auto_add_signals(
        existing_decisions, existing_positions, quant_ctx['positions'],
    ) if new_entries_allowed else []
    entry_signals.extend(auto_add_signals)

    # Extract playbook reads from current cycle only
    playbook_reads = _extract_playbook_reads(agent, since_msg_idx=msg_idx_before)

    meta = {
        'model_id': agent.settings.bedrock_model_id,
        'pm_token_usage': agent.get_token_usage(),
        'prompt_chars': len(prompt),
        'prompt': prompt,
        'playbook_reads': playbook_reads,
    }
    return decisions, entry_signals, exit_signals, meta


def process_eod_signals(
    broker: Any,
    portfolio_state: PortfolioState,
    entry_signals: list[dict],
    exit_signals: list[dict],
    regime: str = 'UNKNOWN',
    sim_date: str | None = None,
) -> None:
    """Save EOD signals as pending for MORNING consumption.

    Entry orders are NOT submitted to broker here — the MORNING cycle
    handles sizing + order submission so that live and backtest share
    the same code path.

    Exit TIGHTEN signals are applied immediately (needed for
    overnight stop-loss checks in advance_day).
    """
    for sig in entry_signals:
        strategy = sig.get('strategy', 'MOMENTUM')
        atr = sig.get('atr', 0.0)
        eod_price = sig.get('entry_price', 0.0)

        # EOD entries are always MARKET (fill at next open).
        # LIMIT is only set via MORNING ADJUST when the LLM converts
        # a gapped entry to a pullback order.
        if not sig.get('entry_type'):
            sig['entry_type'] = 'MARKET'

        et = sig.get('entry_type', 'MARKET')
        lp = sig.get('limit_price')
        price_note = f" @${lp:.2f}" if lp else ""
        print(f"  SIGNAL: BUY {sig['ticker']} x{sig.get('shares', 0)} "
              f"({et}{price_note} stop={sig.get('stop_loss_price', 0):.2f})",
              flush=True)

    if entry_signals or exit_signals:
        signals_dict = {
            'exit_signals': exit_signals,
            'signals': entry_signals,
            'regime': regime,
        }
        portfolio_state.save_pending_signals(signals_dict, sim_date=sim_date)

        # Position state (conviction, tighten, stop) already updated in run_eod_llm.
        for sig in exit_signals:
            action = sig.get('action', 'HOLD')
            if action in ('EXIT', 'PARTIAL_EXIT', 'TIGHTEN'):
                print(f"  SIGNAL: {action} {sig['ticker']} — {sig.get('reason', '')[:60]}", flush=True)
        portfolio_state.save()
    else:
        portfolio_state.save()


def sync_broker_to_state(
    broker: Any,
    portfolio_state: PortfolioState,
    sim_date: str,
    is_first_day: bool = False,
) -> None:
    """Sync portfolio state from broker after price updates."""
    sync = broker.sync(sim_date)
    portfolio_state.cash = sync['cash']
    portfolio_state.portfolio_value = sync['portfolio_value']
    portfolio_state.peak_value = sync['peak_value']
    portfolio_state.trading_day = sim_date
    if is_first_day:
        portfolio_state.daily_start_value = sync['portfolio_value']
    for ticker, pos in portfolio_state.positions.items():
        if ticker in broker.positions:
            bp = broker.positions[ticker]
            pos.current_price = bp.current_price
            pos.unrealized_pnl = bp.unrealized_pnl
            # Sync stop_loss_price — trailing stops and tighten updates
            # modify broker positions but may not propagate to portfolio_state
            if bp.stop_loss_price > pos.stop_loss_price:
                pos.stop_loss_price = bp.stop_loss_price
    portfolio_state.save()


# ---------------------------------------------------------------------------
# Unified daily loop
# ---------------------------------------------------------------------------

def run_one_day(
    agent: Any,
    portfolio_state: PortfolioState,
    broker: Any,
    sim_date: str,
    bars: dict[str, pd.DataFrame],
    hourly_bars: dict[str, pd.DataFrame],
    day_num: int,
    daily_log: list[dict],
    *,
    provider: Any,
    sector_map: dict,
    store: Any,
    session_id: str,
    start_cash: float,
    data_fn: Any,
    total_days: int,
    prev_day_news: dict | None = None,
    prev_eod_time: Any = None,
    log_fn: Any = print,
    phase: str = "precondition",
    is_first_day: bool = False,
    is_last_day: bool = False,
) -> dict:
    """Run one trading day with cycle selection based on position in schedule.

    Cycle schedule:
      - First day:  EOD only (no prior signals for MORNING, no positions for INTRADAY)
      - Middle days: MORNING → INTRADAY → EOD
      - Last day:   MORNING → INTRADAY only (EOD signals would never be consumed)

    This is the single source of truth for the daily loop, used by both
    Preconditioner (warm-up) and Simulator (measurement).

    Args:
        data_fn: callable(sim_date, tickers) -> (news_data, earnings_map)
        log_fn: callable(msg) for logging (e.g. _log or print)
        phase: "precondition" or "simulation" (for progress tracking)
        is_first_day: skip MORNING and INTRADAY (no prior EOD signals exist)
        is_last_day: skip EOD (signals would never be consumed)
    """
    from datetime import timedelta
    from agents._morning_cycle import _record_trade
    from tools.journal.watchlist import load_watchlist

    # Check if session was stopped by user (S3 meta status)
    try:
        meta = store.load_meta(session_id)
        if meta and meta.get('status') in ('stopped', 'stop_requested'):
            log_fn(f"  [STOPPED] Session stopped by user — aborting.")
            store.update_status(session_id, 'stopped')
            raise SessionStoppedError("Session stopped by user")
    except SessionStoppedError:
        raise
    except Exception:
        pass  # Non-critical — continue if meta check fails

    # Update progress
    store.save_progress(session_id, {
        'current_day': day_num,
        'total_days': total_days,
        'current_date': sim_date,
        'phase': phase,
    })

    # --- Load day's articles for MORNING/INTRADAY news windows ---
    day_articles: dict[str, list[dict]] | None = None
    fixture_path = Path('backtest/fixtures/polygon/news') / f'day_{sim_date}.json'
    if fixture_path.exists():
        day_articles = load_json(fixture_path)

    # --- Set sim context on provider + broker ---
    if prev_eod_time is None:
        prev_eod_time = datetime.strptime(sim_date, '%Y-%m-%d').replace(
            hour=21, tzinfo=timezone.utc,
        ) - timedelta(days=1)
    provider.set_sim_context(
        sim_date=sim_date, articles=day_articles, prev_eod_time=prev_eod_time,
    )
    broker.set_sim_context(sim_date=sim_date, bars=bars, hourly_bars=hourly_bars)

    day_events: list[dict] = []

    # Capture PM notes at start of day (before any cycle modifies them)
    notes_before = dict(getattr(portfolio_state, 'pm_notes', {}))

    # Track half-size entries from pending signals for late fill Position creation
    _half_size_tickers: set[str] = set()
    pending = portfolio_state.pending_signals
    if pending:
        for sig in pending.get('signals', []):
            if sig.get('half_size'):
                _half_size_tickers.add(sig['ticker'])

    # --- MORNING + INTRADAY cycles (skip on first day — no prior EOD signals) ---
    morning_meta: dict = {}
    intraday_meta: dict = {}
    if not is_first_day:
        # --- MORNING cycle (overnight research + entry triage + fills) ---
        morning_result = agent.run_trading_cycle('MORNING', sim_date=sim_date)
        day_events = morning_result.get('day_events', [])

        # Record MORNING cycle to agent state
        morning_meta = {
            'orders_placed': morning_result.get('orders_placed', 0),
            'exits_placed': morning_result.get('exits_placed', 0),
            'llm_rejected': morning_result.get('llm_rejected', 0),
            'llm_rejected_details': morning_result.get('llm_rejected_details', []),
            'rr_skipped': morning_result.get('rr_skipped', 0),
            'rr_skipped_details': morning_result.get('rr_skipped_details', []),
            'morning_exit_details': morning_result.get('morning_exit_details', []),
            'news_checked': morning_result.get('news_checked', 0),
            'news_with_articles': morning_result.get('news_with_articles', 0),
            'triggered_positions': morning_result.get('triggered_positions', 0),
            'triggered_candidates': morning_result.get('triggered_candidates', 0),
            'prompt': morning_result.get('prompt', ''),
            'pm_token_usage': morning_result.get('pm_token_usage'),
        }
        if morning_result.get('skipped_reason'):
            morning_meta['skipped_reason'] = morning_result['skipped_reason']
        portfolio_state.record_cycle(
            cycle_type='MORNING',
            date=sim_date,
            regime=morning_result.get('regime', ''),
        )
        if morning_result.get('decisions'):
            portfolio_state.record_decision(
                cycle_type='MORNING',
                date=sim_date,
                decisions=morning_result['decisions'],
                regime=morning_result.get('regime', ''),
            )

        # --- Update news window for INTRADAY ---
        morning_time = datetime.strptime(sim_date, '%Y-%m-%d').replace(
            hour=14, tzinfo=timezone.utc,
        )
        provider.set_sim_context(
            sim_date=sim_date, articles=day_articles, prev_eod_time=morning_time,
        )

        # --- INTRADAY cycle (anomaly detection + LLM if flagged) ---
        intraday_result = agent.run_trading_cycle('INTRADAY', sim_date=sim_date)
        day_events.extend(intraday_result.get('day_events', []))

        # Record INTRADAY cycle to agent state
        intraday_decisions = intraday_result.get('decisions', [])
        intraday_meta = {
            'positions_managed': intraday_result.get('positions_managed', 0),
            'positions_flagged': intraday_result.get('positions_flagged', 0),
            'llm_skipped': intraday_result.get('llm_skipped', False),
            'stops_tightened_auto': intraday_result.get('stops_tightened_auto', 0),
            'stops_tightened_llm': intraday_result.get('stops_tightened_llm', 0),
            'spy_intraday_return': intraday_result.get('spy_intraday_return'),
            'market_shock': intraday_result.get('market_shock', False),
            'auto_tightened_details': intraday_result.get('auto_tightened_details', []),
            'flagged_details': intraday_result.get('flagged_details', {}),
            'prompt': intraday_result.get('prompt', ''),
            'pm_token_usage': intraday_result.get('pm_token_usage'),
        }
        portfolio_state.record_cycle(
            cycle_type='INTRADAY',
            date=sim_date,
        )
        if intraday_result.get('decisions'):
            portfolio_state.record_decision(
                cycle_type='INTRADAY',
                date=sim_date,
                decisions=intraday_result['decisions'],
            )
    else:
        intraday_decisions = []
        log_fn(f"  [SCHEDULE] First day — skipping MORNING/INTRADAY, starting from EOD")

    # --- Price updates + stops ---
    stopped = broker.advance_day(sim_date, bars, hourly_bars=hourly_bars)
    stop_decisions: list[dict] = []  # Synthetic decisions for stop-outs
    for s in stopped:
        day_events.append(s)
        ticker = s['ticker']
        action = s.get('action', '')
        if action == 'ENTRY_FILLED' and ticker not in portfolio_state.positions:
            # LIMIT/STOP orders filled during advance_day (after morning cutoff)
            fill_qty = s.get('shares', 0)
            portfolio_state.positions[ticker] = Position(
                symbol=ticker, qty=fill_qty,
                avg_entry_price=s.get('fill_price', 0.0),
                current_price=s.get('fill_price', 0.0),
                stop_loss_price=s.get('stop_loss', 0.0),
                signal_price=s.get('signal_price', 0.0),
                entry_date=sim_date,
                strategy=s.get('strategy', 'MOMENTUM'),
                entry_qty=fill_qty,
                scaled_entry=ticker in _half_size_tickers,
            )
            log_fn(f"  [LATE FILL] {ticker} @ ${s.get('fill_price', 0):.2f} "
                   f"x{s.get('shares', 0)} ({s.get('order_type', '')})")
        elif ticker in portfolio_state.positions:
            if action in ('STOP_LOSS', 'EXIT_FILLED'):
                pos = portfolio_state.positions[ticker]
                _record_trade(portfolio_state, pos, s, sim_date)
                # Create synthetic decision so stop-outs appear in decision history
                stop_decisions.append({
                    'ticker': ticker,
                    'action': 'STOP_EXIT',
                    'conviction': 'system',
                    'for': (f"Stop-loss triggered @ ${s.get('exit_price', 0):.2f} "
                            f"(entry ${s.get('entry_price', pos.avg_entry_price):.2f}, "
                            f"P&L ${s.get('pnl', 0):+,.0f})"),
                    'against': '',
                    'exit_price': s.get('exit_price', 0),
                    'entry_price': s.get('entry_price', pos.avg_entry_price),
                    'pnl': s.get('pnl', 0),
                    'qty': s.get('qty', pos.qty),
                })
                del portfolio_state.positions[ticker]

    # Record stop-outs to decision_log so they appear in decision history
    if stop_decisions:
        portfolio_state.record_decision(
            cycle_type='STOP_EVENT',
            date=sim_date,
            decisions=stop_decisions,
        )

    # Record execution events (fills, rejections, stops) to decision_log
    exec_events = [
        e for e in day_events
        if e.get('action') in ('ENTRY_FILLED', 'ENTRY_REJECTED', 'STOP_LOSS', 'EXIT_FILLED', 'STOPPED_OUT', 'PARTIAL_EXIT')
    ]
    if exec_events:
        portfolio_state.record_execution(date=sim_date, events=exec_events)

    portfolio_state.daily_start_value = broker.portfolio_value
    sync_broker_to_state(broker, portfolio_state, sim_date, is_first_day=(day_num == 1))

    # --- Day header ---
    pv = broker.portfolio_value
    prev_pv = daily_log[-1]['portfolio_value'] if daily_log else start_cash
    day_ret = (pv - prev_pv) / prev_pv * 100 if prev_pv > 0 else 0.0
    log_fn(f"\n--- Day {day_num}: {sim_date} | PV: ${pv:,.0f} ({day_ret:+.2f}%) | "
           f"Positions: {len(broker.positions)} | Cash: ${broker.cash:,.0f} ---")
    for ev in day_events:
        action = ev.get('action', '')
        ticker = ev.get('ticker', '')
        if 'pnl' in ev:
            log_fn(f"  {action}: {ticker} P&L=${ev['pnl']:+,.2f}")
        elif 'fill_price' in ev:
            log_fn(f"  {action}: {ticker} @ ${ev['fill_price']:.2f} x{ev.get('shares', 0)}")
        else:
            log_fn(f"  {action}: {ticker} — {ev.get('reason', '')}")

    # --- EOD cycle (skip on last day — signals would never be consumed) ---
    screened: list[str] = []
    quant_ctx: dict = {}
    research: dict = {}
    decisions: list[dict] = []
    entry_signals: list[dict] = []
    exit_signals: list[dict] = []
    llm_meta: dict = {}
    regime = 'UNKNOWN'
    news_data: dict = {}

    if not is_last_day:
        # --- Watchlist ---
        watchlist = load_watchlist()
        watchlist_tickers = [w['ticker'] for w in watchlist if 'ticker' in w]

        # --- Screener (exclude blackout + stale candidates) ---
        blackout_excluded = _get_blackout_excluded(sim_date=sim_date)
        if blackout_excluded:
            log_fn(f"  [BLACKOUT] {len(blackout_excluded)} excluded (skip blackout): "
                   f"{sorted(blackout_excluded)[:8]}")
        staleness_excluded = _get_staleness_excluded(sim_date=sim_date)
        if staleness_excluded:
            log_fn(f"  [STALENESS] {len(staleness_excluded)} excluded (repeated without selection): "
                   f"{sorted(staleness_excluded)[:8]}")
        all_excluded = blackout_excluded | staleness_excluded
        screened = screen_universe(bars, sim_date, extra_exclude=all_excluded)
        existing_tickers = list(portfolio_state.positions.keys())
        candidates = [t for t in screened if t not in portfolio_state.positions]

        # Re-entry cooldown: exclude recently exited tickers
        cooldown_days = agent.settings.reentry_cooldown_days
        if cooldown_days > 0 and portfolio_state.trade_history:
            sim_dt = datetime.fromisoformat(sim_date + 'T00:00:00+00:00')
            cutoff = sim_dt - timedelta(days=cooldown_days)
            recent_exits = set()
            for t in portfolio_state.trade_history:
                ts = getattr(t, 'timestamp', None)
                if not ts:
                    continue
                try:
                    exit_dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                except (ValueError, AttributeError):
                    continue
                if exit_dt >= cutoff:
                    recent_exits.add(getattr(t, 'symbol', ''))
            cooldown_removed = [c for c in candidates if c in recent_exits]
            if cooldown_removed:
                candidates = [c for c in candidates if c not in recent_exits]
                log_fn(f"  [COOLDOWN] removed {len(cooldown_removed)} tickers "
                       f"(exited within {cooldown_days}d): {cooldown_removed}")

        # Reject blackout: exclude tickers REJECTed in MORNING within last 2 days
        from agents._formatting import _apply_reject_blackout
        candidates, reject_filtered = _apply_reject_blackout(
            candidates, portfolio_state.decision_log,
            blackout_days=2,
            sim_date=sim_date,
        )
        if reject_filtered:
            log_fn(f"  [REJECT BLACKOUT] removed {len(reject_filtered)} tickers "
                   f"(morning reject): {reject_filtered}")

        for t in watchlist_tickers:
            if t not in candidates and t not in portfolio_state.positions:
                candidates.append(t)
        log_fn(f"  [SCREENER] {len(screened)} screened -> {len(candidates)} candidates "
               f"(incl {len(watchlist_tickers)} watchlist)")

        # --- Fetch news + earnings ---
        all_tickers = list(set(existing_tickers + candidates))
        news_data, earnings_map = data_fn(sim_date, all_tickers)

        # --- Earnings blackout: remove candidates with earnings ≤2 days ---
        blackout = [t for t in candidates if (earnings_map or {}).get(t, 99) <= 2]
        if blackout:
            candidates = [t for t in candidates if t not in set(blackout)]
            log_fn(f"  [EARNINGS BLACKOUT] removed {len(blackout)} candidates: {blackout}")

        # --- Quant context ---
        quant_ctx = build_quant_context(
            agent.settings, portfolio_state.positions, candidates,
            broker, bars, earnings_map or None,
            trade_history=portfolio_state.trade_history,
            sector_map=sector_map,
            watchlist_tickers=watchlist_tickers,
        )
        regime = quant_ctx.get('regime', 'UNKNOWN')

        # --- Risk ---
        dd, size_multiplier, new_entries_allowed = compute_risk_state(
            agent.settings, portfolio_state, broker,
        )
        if 0 < size_multiplier < 1.0:
            for ctx in quant_ctx['candidates'].values():
                raw = ctx.get('indicative_shares', 0)
                ctx['indicative_shares'] = max(1, int(raw * size_multiplier))

        # --- Research ---
        ranked_tickers = list(quant_ctx['candidates'].keys())
        price_context = _extract_price_context(quant_ctx, bars)
        research = run_research(
            agent, existing_tickers, ranked_tickers,
            news_data, earnings_map, new_entries_allowed,
            backtest_mode=True,
            prev_day_news=prev_day_news,
            sim_date=sim_date,
            price_context=price_context,
        )
        inject_research_into_context(quant_ctx, research)

        # --- PM LLM ---
        decisions, entry_signals, exit_signals, llm_meta = run_eod_llm(
            agent, quant_ctx,
            new_entries_allowed, portfolio_state.positions,
        )
        research_meta = research.pop('_meta', {})
        llm_meta['research_token_usage'] = research_meta.get('research_token_usage', {})
        llm_meta['research_triggered'] = {
            'positions': research_meta.get('triggered_positions', 0),
            'candidates': research_meta.get('triggered_candidates', 0),
        }
        log_fn(f"  [LLM] Regime: {regime}")

        # --- Process signals ---
        process_eod_signals(broker, portfolio_state, entry_signals, exit_signals,
                            regime=regime, sim_date=sim_date)

        # --- Record cycle to agent state (lightweight — heavy data in store.save_day) ---
        portfolio_state.record_cycle(
            cycle_type='EOD_SIGNAL',
            date=sim_date,
            research=research,
            regime=regime,
            candidate_tickers=list(quant_ctx.get('candidates', {}).keys()),
        )
        if decisions:
            portfolio_state.record_decision(
                cycle_type='EOD_SIGNAL',
                date=sim_date,
                decisions=decisions,
                regime=regime,
            )
    else:
        log_fn(f"  [SCHEDULE] Last day — skipping EOD (signals would not be consumed)")

    # --- SPY benchmark ---
    spy_close = None
    spy_return = 0.0
    spy_bars = bars.get('SPY')
    if spy_bars is not None and len(spy_bars) >= 1:
        spy_today = spy_bars[spy_bars.index <= pd.Timestamp(sim_date)]
        if len(spy_today) >= 1:
            spy_close = float(spy_today.iloc[-1]['close'])
        if len(spy_today) >= 2 and daily_log:
            spy_return = (float(spy_today.iloc[-1]['close']) - float(spy_today.iloc[-2]['close'])) / float(spy_today.iloc[-2]['close'])

    # --- Record daily stats ---
    portfolio_state.record_daily_stats(
        date=sim_date,
        portfolio_value=pv,
        cash=broker.cash,
        positions=portfolio_state.positions,
        spy_close=spy_close,
        regime=regime,
        events=day_events,
        start_cash=start_cash,
    )
    portfolio_state.save()

    # Persist agent state to store (S3 in cloud mode)
    store.save_state(session_id, portfolio_state.to_dict())

    if portfolio_state.daily_stats:
        store.save_daily_stat(session_id, sim_date, portfolio_state.daily_stats[-1])

    # --- Save cycle details ---
    playbook_reads = llm_meta.get('playbook_reads', [])
    if playbook_reads:
        log_fn(f"  [PLAYBOOK] PM read: {playbook_reads}")

    _broker_snapshot = {
        'cash': round(broker.cash, 2),
        'portfolio_value': round(broker.portfolio_value, 2),
        'positions': {t: round(p.unrealized_pnl, 2) for t, p in broker.positions.items()},
    }

    # EOD_SIGNAL cycle
    if not is_last_day:
        # Replace HOLD→ADD for auto-ADD tickers in frontend display
        add_tickers = {s['ticker'] for s in entry_signals if s.get('action') == 'AUTO_ADD'}
        frontend_decisions = [
            {**d, 'action': 'ADD'} if d.get('ticker') in add_tickers and d.get('action', '').upper() == 'HOLD'
            else d
            for d in decisions
        ] if add_tickers else decisions
        store.save_cycle(session_id, sim_date, 'EOD_SIGNAL', {
            'screened': screened,
            'quant_context': quant_ctx,
            'research': research,
            'decisions': frontend_decisions,
            'entry_signals': entry_signals,
            'exit_signals': exit_signals,
            'playbook_reads': playbook_reads,
            'notes_before': notes_before,
            'notes_after': dict(getattr(portfolio_state, 'pm_notes', {})),
            'prompt': llm_meta.get('prompt', ''),
            'token_usage': {
                'pm': llm_meta.get('pm_token_usage', {}),
                'research': llm_meta.get('research_token_usage', {}),
            },
            'broker': _broker_snapshot,
        })

    # MORNING cycle
    if morning_meta:
        store.save_cycle(session_id, sim_date, 'MORNING', {
            'events': [e for e in day_events if e.get('cycle') != 'INTRADAY'],
            **morning_meta,
            'token_usage': {'pm': morning_meta.get('pm_token_usage') or {}},
            'broker': _broker_snapshot,
        })

    # INTRADAY cycle
    if intraday_meta:
        store.save_cycle(session_id, sim_date, 'INTRADAY', {
            'events': [e for e in day_events if e.get('cycle') == 'INTRADAY'],
            'decisions': intraday_decisions,
            **intraday_meta,
            'token_usage': {'pm': intraday_meta.get('pm_token_usage') or {}},
            'broker': _broker_snapshot,
        })

    daily_return = (pv - prev_pv) / prev_pv if prev_pv > 0 else 0.0
    return {
        'date': sim_date, 'day': day_num,
        'portfolio_value': round(pv, 2), 'cash': round(broker.cash, 2),
        'daily_return_pct': round(daily_return * 100, 4),
        'spy_return_pct': round(spy_return * 100, 4),
        'excess_return_pct': round((daily_return - spy_return) * 100, 4),
        'positions': list(broker.positions.keys()),
        'position_count': len(broker.positions),
        'events': day_events,
        'new_entries': [s['ticker'] for s in entry_signals],
        'regime': regime,
        '_news_data': news_data,
    }
