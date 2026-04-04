"""
agents/_eod_cycle.py — EOD_SIGNAL cycle implementation.

Mixin class providing _run_eod_signal_cycle and its helpers.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from agents._formatting import (
    _now_et_iso, _format_market_context, _format_portfolio_summary,
    _format_positions_table, _format_candidates_table,
    _drawdown_size_multiplier, _apply_reentry_cooldown, _apply_reject_blackout,
    _apply_skip_blackout,
    _extract_playbook_reads, build_decision_history,
)
from tools.journal.decision_log import consume_cycle_decisions, try_rescue_from_text
from tools.journal.watchlist import load_watchlist, remove_from_watchlist

logger = logging.getLogger(__name__)


def _regime_inline_guidance(regime: str) -> str:
    """Return 2-3 lines of regime-specific context for the EOD prompt."""
    guidance = {
        "TRENDING": (
            "  Regime favors momentum entries — but screened candidates are likely\n"
            "  already extended. Prioritize trend pullbacks (lower price_vs_20ma)\n"
            "  over high-ADX extension plays. See entry/momentum Entry Timing.\n"
            "  MR entries at Stage 2 support are valid dip-buys within the trend."
        ),
        "TRANSITIONAL": (
            "  Trend tailwind is weakening — prioritize setups with volume confirmation\n"
            "  or strong weekly structure. MR entries at Stage 2 support remain natural.\n"
            "  Pullbacks may deepen — half_size entries are natural here."
        ),
        "MEAN_REVERTING": (
            "  Range-bound market — MR is natural habitat. Be selective on quality.\n"
            "  MOM entries face headwinds, but individual setups with strong momentum_z,\n"
            "  weekly structure, or sector leadership can still work — weigh the full picture."
        ),
        "HIGH_VOLATILITY": (
            "  Wider swings — higher R:R setups matter more. Fewer positions (4-5) may be appropriate.\n"
            "  Tighten stops on all positions. Strong setups are still valid regardless of VIX."
        ),
    }
    return guidance.get(regime, "  Regime not recognized — weight individual setup quality.")


def _inject_auto_add_into_decisions(
    decisions: list[dict],
    auto_add_signals: list[dict],
) -> list[dict]:
    """Replace HOLD with ADD for auto-ADD tickers in frontend decisions.

    Returns a new list — original decisions are not mutated.
    """
    add_tickers = {s['ticker'] for s in auto_add_signals}
    if not add_tickers:
        return decisions
    result = []
    for d in decisions:
        if d.get('ticker') in add_tickers and d.get('action', '').upper() == 'HOLD':
            result.append({**d, 'action': 'ADD'})
        else:
            result.append(d)
    return result


class EODCycleMixin:
    """Methods for the EOD_SIGNAL cycle, mixed into PortfolioAgent."""

    def _run_eod_signal_cycle(self) -> dict[str, Any]:
        """
        EOD_SIGNAL hybrid cycle.

        Steps:
          1. portfolio-sync                   (system)
          2. circuit-breaker + drawdown       (system → size_multiplier, new_entries_allowed)
          3. fetch price bars                 (system — existing + SPY/QQQ + candidates if allowed)
          4. quant context                    (QuantEngine — position + candidate + portfolio + market metrics)
          5. sentiment analysis               (ResearchAnalystAgent LLM — existing + candidates)
          6. indicative sizing for candidates (system — applied before LLM call so LLM sees position sizes)
          7. Orchestrator LLM decision        (one call with full context)
          8. extract + save pending_signals   (system)
        """
        from tools.risk.drawdown import check_drawdown
        from tools.sentiment.news import clear_article_cache
        from tools.sentiment.earnings import clear_earnings_cache
        from agents.quant_engine import QuantEngine

        # Clear caches at start of each EOD cycle (prevent unbounded growth)
        clear_article_cache()
        clear_earnings_cache()

        # ── Step 1: Sync portfolio ────────────────────────────────────────────
        portfolio = self._get_broker().sync()
        if portfolio.get('error'):
            logger.error("EOD_SIGNAL: portfolio sync failed: %s", portfolio['error'])
            return {"cycle_type": "EOD_SIGNAL", "error": "portfolio_sync_failed",
                    "details": portfolio['error']}

        # Propagate broker-synced values back to agent state
        self.portfolio_state.cash = portfolio['cash']
        self.portfolio_state.portfolio_value = portfolio['portfolio_value']
        self.portfolio_state.peak_value = max(
            self.portfolio_state.peak_value, portfolio['peak_value']
        )
        # Sync positions from Alpaca into agent state
        from state.portfolio_state import Position as _Pos
        positions_full = portfolio.get('positions_full', {})
        if positions_full:
            synced_syms = set(positions_full.keys())
            for sym, pos_data in positions_full.items():
                if sym not in self.portfolio_state.positions:
                    self.portfolio_state.positions[sym] = _Pos.from_dict(pos_data)
                else:
                    local = self.portfolio_state.positions[sym]
                    local.current_price = pos_data.get('current_price', local.current_price)
                    local.unrealized_pnl = pos_data.get('unrealized_pnl', local.unrealized_pnl)
                    local.qty = pos_data.get('qty', local.qty)
            for sym in list(self.portfolio_state.positions.keys()):
                if sym not in synced_syms:
                    del self.portfolio_state.positions[sym]

        existing_positions = self.portfolio_state.positions  # {ticker: Position}

        # Update high-water mark using closing prices (not intraday snapshots).
        # Trailing stops are chandelier-based and should ratchet from daily
        # closes to avoid tightening on intraday noise.
        for pos in existing_positions.values():
            if pos.current_price > pos.highest_close:
                pos.highest_close = pos.current_price

        # Capture positions closed during today's session — passed to EOD prompt
        # so the LLM can write a post-trade reflection for each.
        recently_closed_eod = [
            {
                'symbol': c['symbol'],
                'entry_price': c['avg_entry_price'],
                'exit_price': c['exit_price'],
                'pnl': c['realized_pnl'],
                'return_pct': round(
                    (c['exit_price'] - c['avg_entry_price']) / c['avg_entry_price'] * 100, 2
                ) if c['avg_entry_price'] else 0.0,
                'exit_type': c.get('strategy', 'UNKNOWN'),
                'holding_days': c.get('holding_days', 0),
            }
            for c in portfolio.get('newly_closed_positions', [])
        ]

        # ── Step 2: Risk state evaluation ────────────────────────────────────
        dd = check_drawdown(
            current_value=portfolio['portfolio_value'],
            peak_value=portfolio['peak_value'],
            max_drawdown_pct=self.settings.max_drawdown_pct,
        )
        size_multiplier = _drawdown_size_multiplier(dd['current_drawdown_pct'])

        new_entries_allowed = size_multiplier > 0

        # Portfolio heat ceiling: block new entries if total stop-loss risk
        # exceeds the configured threshold (default 8%).
        heat_ceiling_breached = False
        portfolio_heat_early = 0.0
        pv = portfolio['portfolio_value']
        if pv > 0 and existing_positions:
            total_dollar_risk = sum(
                max(0.0, pos.current_price - pos.stop_loss_price) * pos.qty
                for pos in existing_positions.values()
                if pos.stop_loss_price > 0
            )
            portfolio_heat_early = total_dollar_risk / pv
            if portfolio_heat_early >= self.settings.portfolio_heat_ceiling_pct:
                heat_ceiling_breached = True
                new_entries_allowed = False

        logger.info(
            "EOD_SIGNAL: risk state — drawdown=%.1f%%, size_multiplier=%.2f, "
            "heat=%.1f%%, heat_ceiling_breached=%s",
            dd['current_drawdown_pct'] * 100, size_multiplier,
            portfolio_heat_early * 100, heat_ceiling_breached,
        )

        # ── Step 3: Determine candidates + fetch all bars at once ─────────────
        existing_tickers = list(existing_positions.keys())
        s = self.settings
        sim_date = getattr(self, '_sim_date', None)

        watchlist_entries = load_watchlist()
        from tools.quant.market_breadth import ALL_BREADTH_TICKERS

        # Fetch bars for the full universe so the screener can filter on pre-loaded data
        if new_entries_allowed:
            universe = self._get_provider().get_universe()
        else:
            universe = []

        today = _now_et_iso()[:10]
        all_fetch = list(set(
            existing_tickers + universe + ['SPY', 'QQQ'] + ALL_BREADTH_TICKERS
        ))
        bars = self._get_provider().get_bars(all_fetch, end=today)

        # Determine actual last trading date from market data (not clock time)
        spy_bars = bars.get('SPY')
        if spy_bars is not None and not spy_bars.empty:
            trading_date = spy_bars.index[-1].strftime("%Y-%m-%d")
        else:
            trading_date = today

        # Apply screener filters to pre-loaded bars
        if new_entries_allowed:
            from tools.data.screener import _avg_volume, _atr_pct, _multi_signal_screen
            exclude = {'SPY', 'QQQ'} | set(ALL_BREADTH_TICKERS)
            liquid = [
                t for t, df in bars.items()
                if t not in exclude and len(df) >= 5
                and _avg_volume(df) >= s.screener_min_avg_volume
            ]
            volatile = [
                t for t in liquid
                if s.screener_min_atr_pct <= _atr_pct(bars[t]) <= s.screener_max_atr_pct
            ]
            screened, signal_map = _multi_signal_screen(
                volatile, bars, n=s.screener_momentum_candidates, return_signals=True,
            )
            # Exclude already-held positions
            candidates = [t for t in screened if t not in existing_positions]
            # Exclude recently exited tickers (re-entry cooldown)
            candidates, cooldown_filtered = _apply_reentry_cooldown(
                candidates, self.portfolio_state.trade_history,
                cooldown_days=s.reentry_cooldown_days,
            )
            if cooldown_filtered:
                logger.info(
                    "EOD_SIGNAL: re-entry cooldown removed %d tickers: %s",
                    len(cooldown_filtered), cooldown_filtered,
                )
            # Exclude tickers SKIPped recently unless a new signal has appeared
            from state.agent_state import get_state
            candidates, skip_filtered = _apply_skip_blackout(
                candidates, get_state().decision_log,
                blackout_days=5,
                sim_date=sim_date,
                current_signals=signal_map,
            )
            if skip_filtered:
                logger.info(
                    "EOD_SIGNAL: skip blackout removed %d tickers: %s",
                    len(skip_filtered), skip_filtered,
                )
            # Exclude tickers REJECTed in MORNING within last 2 days
            candidates, reject_filtered = _apply_reject_blackout(
                candidates, get_state().decision_log,
                blackout_days=2,
                sim_date=sim_date,
            )
            if reject_filtered:
                logger.info(
                    "EOD_SIGNAL: reject blackout removed %d tickers: %s",
                    len(reject_filtered), reject_filtered,
                )
            # Merge watchlist tickers (always reviewed even if screener misses them)
            for entry in watchlist_entries:
                wt = entry["ticker"]
                if wt not in existing_positions and wt not in candidates:
                    candidates.append(wt)
            logger.info(
                "EOD_SIGNAL: %d screened + %d watchlist → %d new candidates (held=%d).",
                len(screened), len(watchlist_entries),
                len(candidates), len(existing_tickers),
            )
        else:
            candidates = []
            screened = []
            logger.info("EOD_SIGNAL: new entries blocked — reviewing existing positions only.")

        quant = QuantEngine(settings=self.settings)

        # ── Step 3b: Earnings screening (positions + candidates) ────────────
        all_earnings_tickers = existing_tickers + candidates
        earnings_map: dict[str, int] = {}
        if all_earnings_tickers:
            try:
                earnings_map = self._get_provider().get_earnings(all_earnings_tickers)
                logger.info(
                    "EOD_SIGNAL: earnings screening — %d tickers, %d approaching earnings.",
                    len(all_earnings_tickers), len(earnings_map),
                )
            except Exception as exc:
                logger.warning("EOD_SIGNAL: earnings screening failed (%s) — skipping.", exc)

        # ── Step 3c: Earnings blackout — remove candidates ≤2 days ───────────
        if earnings_map and candidates:
            blackout = [t for t in candidates if earnings_map.get(t, 99) <= 2]
            if blackout:
                candidates = [t for t in candidates if t not in set(blackout)]
                logger.info(
                    "EOD_SIGNAL: earnings blackout removed %d candidates: %s",
                    len(blackout), blackout,
                )

        # ── Step 4: Quant context (system) ────────────────────────────────────
        watchlist_tickers = [e['ticker'] for e in watchlist_entries] if watchlist_entries else []
        quant_ctx = quant.build_eod_context(
            existing_positions=existing_positions,
            candidates=candidates,
            portfolio_cash=portfolio['cash'],
            portfolio_value=portfolio['portfolio_value'],
            bars=bars,
            earnings_map=earnings_map if earnings_map else None,
            trade_history=self.portfolio_state.trade_history,
            watchlist_tickers=watchlist_tickers,
        )
        logger.info(
            "EOD_SIGNAL: quant context — regime=%s, %d positions, %d candidates.",
            quant_ctx['regime'], len(quant_ctx['positions']), len(quant_ctx['candidates']),
        )

        # ── Step 5: Apply size_multiplier to indicative shares ───────────────
        if size_multiplier < 1.0 and size_multiplier > 0:
            for ctx in quant_ctx['candidates'].values():
                raw = ctx.get('indicative_shares', 0)
                ctx['indicative_shares'] = max(1, int(raw * size_multiplier))

        # ── Step 6: Research (triage → LLM only for triggered tickers) ─────────
        from tools.journal.research_log import load_research_history
        researcher = self._get_researcher()

        position_research: dict = {}
        candidate_research: dict = {}

        ranked_tickers = list(quant_ctx['candidates'].keys())
        all_research_tickers = existing_tickers + ranked_tickers

        # ── Step 6a: Batch news fetch (system code, no LLM) ──────────────────
        news_data: dict = {}
        if all_research_tickers:
            try:
                news_data = self._get_provider().get_news(all_research_tickers)
                logger.info("EOD: batch news fetch — %d tickers.", len(all_research_tickers))
            except Exception as exc:
                logger.warning("EOD: batch news fetch failed: %s", exc)

        # ── Step 6b: Triage — decide which tickers need LLM research ─────────
        s = self.settings
        triggered_positions: list[str] = []
        triggered_candidates: list[str] = []

        for ticker in existing_tickers:
            if self._should_trigger_research(
                ticker, news_data, quant_ctx.get('positions', {}),
                s.research_volume_trigger, s.research_price_trigger_atr,
            ):
                triggered_positions.append(ticker)
            else:
                prior = load_research_history(ticker, last_n=1)
                if prior:
                    position_research[ticker] = prior[0]

        for ticker in ranked_tickers:
            if self._should_trigger_research(
                ticker, news_data, quant_ctx.get('candidates', {}),
                s.research_volume_trigger, s.research_price_trigger_atr,
            ):
                triggered_candidates.append(ticker)
            else:
                prior = load_research_history(ticker, last_n=1)
                if prior:
                    candidate_research[ticker] = prior[0]

        logger.info(
            "EOD: research triage — positions %d/%d triggered, candidates %d/%d triggered.",
            len(triggered_positions), len(existing_tickers),
            len(triggered_candidates), len(ranked_tickers),
        )

        # ── Step 6c: LLM research for triggered tickers only ─────────────────
        if triggered_positions:
            try:
                pos_res = researcher.eod_research_positions(
                    triggered_positions,
                    pre_fetched_news=news_data,
                    earnings_map=earnings_map or None,
                    sim_date=getattr(self, '_sim_date', None),
                )
                position_research.update(pos_res)
                logger.info(
                    "EOD: position research complete — %d tickers.",
                    sum(1 for v in pos_res.values() if v is not None),
                )
            except Exception as exc:
                logger.warning("EOD: position research failed: %s", exc)

        if triggered_candidates and new_entries_allowed:
            try:
                cand_res = researcher.eod_research_candidates(
                    triggered_candidates,
                    pre_fetched_news=news_data,
                    earnings_map=earnings_map or None,
                    sim_date=getattr(self, '_sim_date', None),
                )
                candidate_research.update(cand_res)
                logger.info(
                    "EOD: candidate research complete — %d tickers.",
                    len(cand_res),
                )
            except Exception as exc:
                logger.warning("EOD: candidate research failed: %s", exc)

        # Inject research + prior decisions into quant context for inline display
        for ticker in list(quant_ctx.get('positions', {})):
            r = position_research.get(ticker)
            if isinstance(r, dict):
                quant_ctx['positions'][ticker]['research_summary'] = r.get('summary', '')
                quant_ctx['positions'][ticker]['research_risk_level'] = r.get('risk_level', 'none')
                quant_ctx['positions'][ticker]['research_earnings_days'] = r.get('earnings_days')
                quant_ctx['positions'][ticker]['research_facts'] = r.get('facts', [])
        for ticker in list(quant_ctx.get('candidates', {})):
            r = candidate_research.get(ticker)
            if isinstance(r, dict):
                quant_ctx['candidates'][ticker]['research_summary'] = r.get('summary', '')
                quant_ctx['candidates'][ticker]['research_risk_level'] = r.get('risk_level', 'none')
                quant_ctx['candidates'][ticker]['research_earnings_days'] = r.get('earnings_days')
                quant_ctx['candidates'][ticker]['research_facts'] = r.get('facts', [])

        # ── Step 6c: Earnings context for positions approaching earnings ─────
        from tools.quant.earnings_risk import (
            fetch_past_earnings_dates, compute_earnings_gap_history,
            build_earnings_context,
        )
        for ticker, pctx in quant_ctx.get('positions', {}).items():
            e_days = pctx.get('earnings_days_away')
            if e_days is None or e_days < 0 or e_days > 10:
                continue
            pos = existing_positions.get(ticker)
            pos_weight = (pos.current_price * pos.qty) / pv if pos and pv > 0 else 0.0
            past_dates = fetch_past_earnings_dates(ticker)
            gap_history = None
            if past_dates:
                gap_history = compute_earnings_gap_history(bars.get(ticker), past_dates)
            earnings_ctx = build_earnings_context(
                days_to_earnings=e_days,
                unrealized_pnl_pct=pctx.get('unrealized_pnl_pct', 0.0),
                gap_history=gap_history,
                position_weight_pct=pos_weight,
            )
            pctx['earnings_context'] = earnings_ctx

        # ── Step 7: Portfolio Agent LLM decision (one call, full context) ─────
        recent_trades = self._format_recent_trades()
        prompt = self._build_eod_prompt(
            quant_ctx, new_entries_allowed,
            recent_trades=recent_trades,
            recently_closed=recently_closed_eod or None,
        )
        self._swap_submit_tool('EOD_SIGNAL')
        msg_idx_before = len(getattr(self.agent, 'messages', []))
        llm_text = self.run(prompt)
        try_rescue_from_text(llm_text)
        decisions = consume_cycle_decisions()
        if not decisions:
            logger.error("EOD_SIGNAL: no decisions submitted via submit_eod_decisions tool.")
            return {"cycle_type": "EOD_SIGNAL", "error": "no_decisions_submitted"}

        # Attach screening signals to SKIP decisions for signal-based blackout
        for d in decisions:
            if d.get('action', '').upper() == 'SKIP':
                ticker = d.get('ticker', '').upper()
                sigs = signal_map.get(ticker, set())
                if sigs:
                    d['screening_signals'] = sorted(sigs)

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
                logger.info("EOD_SIGNAL: injecting implicit HOLD for %s", ticker)

        # Categorize decisions by action
        # Fix misclassified HOLD on candidates: if a ticker is not in existing
        # positions but LLM said HOLD, reclassify as WATCH (watchlist keep).
        candidate_tickers = set(quant_ctx.get('candidates', {}).keys())
        for d in decisions:
            action = d.get('action', '').upper()
            ticker = d.get('ticker', '').upper()
            if action == 'HOLD' and ticker not in existing_positions and ticker in candidate_tickers:
                logger.info("EOD_SIGNAL: reclassifying HOLD→WATCH for candidate %s", ticker)
                d['action'] = 'WATCH'

        existing_decisions = [
            d for d in decisions
            if d.get('action', '').upper() in ('HOLD', 'EXIT', 'PARTIAL_EXIT', 'TIGHTEN')
        ]
        new_entries = [
            d for d in decisions
            if d.get('action', '').upper() in ('LONG', 'SKIP', 'WATCH')
        ]

        # ── Step 8: Extract exit_signals and entry signals, save pending ──────
        exit_signals = self._extract_eod_exit_signals(
            existing_decisions,
            existing_positions,
            quant_ctx['positions'],
        )
        entry_signals = self._extract_eod_entry_signals(
            new_entries,
            quant_ctx['candidates'],
        ) if new_entries_allowed else []

        # ── Step 8b: Update position state from PM decisions ──────────────
        # Must run BEFORE auto-ADD so consecutive_high_conviction is current.
        # - Save conviction for trailing stop multiplier (MOM uses it)
        # - Set/clear tighten_active flag based on action and conviction
        # - Immediately recalculate trailing stop when conviction changes
        s = self.settings
        for sig in exit_signals:
            ticker = sig['ticker']
            pos = self.portfolio_state.positions.get(ticker)
            if not pos:
                continue
            conviction = sig.get('conviction', '')
            prev_conviction = pos.last_conviction
            if conviction:
                pos.last_conviction = conviction
                # Track consecutive high conviction days for auto-ADD gating
                if conviction == 'high':
                    pos.consecutive_high_conviction += 1
                else:
                    pos.consecutive_high_conviction = 0
            if sig.get('action') == 'TIGHTEN':
                pos.tighten_active = True
            elif pos.tighten_active and conviction == 'high':
                pos.tighten_active = False
                logger.info("EOD: %s tighten_active cleared (conviction=high)", ticker)

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
                    old_stop = pos.stop_loss_price
                    bracket_id = getattr(pos, 'bracket_order_id', None)
                    mod = self._get_broker().update_stop(
                        ticker, new_stop, bracket_order_id=bracket_id,
                    )
                    pos.stop_loss_price = new_stop
                    if mod.get('modified'):
                        logger.info("EOD: %s conviction %s→%s, stop tightened %.2f → %.2f",
                                    ticker, prev_conviction, conviction, old_stop, new_stop)
                    else:
                        logger.warning("EOD: %s stop update broker failed: %s (local updated)",
                                       ticker, mod.get('error'))
        # Clear scaled_up flag on all positions after it's been shown
        for pos in self.portfolio_state.positions.values():
            if getattr(pos, 'scaled_up', False):
                pos.scaled_up = False
        self.portfolio_state.save()

        # Auto-ADD: half-size positions with high conviction get scaled up.
        # Runs AFTER Step 8b so consecutive_high_conviction reflects this cycle.
        auto_add_signals = self._generate_auto_add_signals(
            existing_decisions,
            existing_positions,
            quant_ctx['positions'],
        ) if new_entries_allowed else []
        entry_signals.extend(auto_add_signals)

        exits_count = sum(1 for s in exit_signals if s['action'] in ('EXIT', 'PARTIAL_EXIT'))
        logger.info(
            "EOD_SIGNAL: LLM decisions — EXIT=%d, PARTIAL_EXIT=%d, TIGHTEN=%d, HOLD=%d, new_entries=%d.",
            sum(1 for s in exit_signals if s['action'] == 'EXIT'),
            sum(1 for s in exit_signals if s['action'] == 'PARTIAL_EXIT'),
            sum(1 for s in exit_signals if s['action'] == 'TIGHTEN'),
            sum(1 for s in exit_signals if s['action'] == 'HOLD'),
            len(entry_signals),
        )

        # Watchlist removal deferred to MORNING — if entry is rejected
        # (gap/LLM/sizing), the ticker stays on watchlist for re-evaluation.

        regime = quant_ctx.get('regime', 'UNKNOWN')
        # Carry forward quant context for MORNING/INTRADAY reuse.
        # Positions context: held positions (used by both morning and intraday).
        # Candidates context: only LONG signal tickers (used by morning LLM).
        entry_tickers = {s['ticker'] for s in entry_signals}
        pending = {
            'cycle_type': 'EOD_SIGNAL',
            'regime': regime,
            'regime_confidence': quant_ctx.get('regime_confidence'),
            'strategy': quant_ctx.get('strategy', 'UNKNOWN'),
            'generated_at': quant_ctx.get('generated_at'),
            'drawdown_status': dd,
            'size_multiplier': size_multiplier,
            'signals': entry_signals,
            'exit_signals': exit_signals,
            'quant_positions': quant_ctx.get('positions', {}),
            'quant_candidates': {
                t: c for t, c in quant_ctx.get('candidates', {}).items()
                if t in entry_tickers
            },
        }
        if entry_signals or exit_signals:
            self.portfolio_state.save_pending_signals(pending)
            logger.info(
                "EOD_SIGNAL: saved %d entry signals + %d position review signals.",
                len(entry_signals), len(exit_signals),
            )
        else:
            logger.info("EOD_SIGNAL: no actionable signals — nothing saved.")

        # Save regime for TRANSITIONAL context in next EOD cycle
        self.portfolio_state.last_regime = regime
        self.portfolio_state.save()

        # ── Step 9: Save structured EOD cycle log ─────────────────────────
        playbook_reads = _extract_playbook_reads(self, since_msg_idx=msg_idx_before)
        self._save_eod_cycle_log(
            screened_tickers=candidates,
            quant_ctx=quant_ctx,
            position_research=position_research,
            candidate_research=candidate_research,
            prompt=prompt,
            llm_response={'decisions': decisions},
            entry_signals=entry_signals,
            exit_signals=exit_signals,
            risk_state={'drawdown': dd, 'size_multiplier': size_multiplier},
            portfolio_snapshot={'cash': portfolio['cash'], 'value': portfolio['portfolio_value']},
            playbook_reads=playbook_reads,
        )

        # Build per-ticker research summaries (compact — no raw articles)
        research_summaries = {}
        for ticker, r in {**candidate_research, **position_research}.items():
            if not r:
                continue
            research_summaries[ticker] = {
                'summary': r.get('summary', ''),
                'risk_level': r.get('risk_level', 'none'),
                'facts': r.get('facts', []),
                'veto_trade': r.get('veto_trade', False),
                'earnings_days': r.get('earnings_days'),
            }

        return {
            'cycle_type': 'EOD_SIGNAL',
            'regime': pending['regime'],
            'strategy': pending['strategy'],
            'regime_confidence': quant_ctx.get('regime_confidence'),
            'new_entries_allowed': new_entries_allowed,
            'existing_positions_reviewed': len(existing_tickers),
            'candidates_evaluated': len(quant_ctx['candidates']),
            'entry_signals_count': len(entry_signals),
            'exit_signals_count': exits_count,
            'size_multiplier': size_multiplier,
            'drawdown_status': dd['status'],
            'heat_ceiling_breached': heat_ceiling_breached,
            # Rich data for frontend
            'screened': candidates,
            'decisions': _inject_auto_add_into_decisions(decisions, auto_add_signals),
            'entry_signals': entry_signals,
            'exit_signals': exit_signals,
            'research': research_summaries,
            'quant_context': {
                'regime': quant_ctx.get('regime'),
                'strategy': quant_ctx.get('strategy'),
                'regime_confidence': quant_ctx.get('regime_confidence'),
                'candidates': quant_ctx.get('candidates', {}),
                'positions': quant_ctx.get('positions', {}),
            },
            'playbook_reads': playbook_reads or [],
            'pm_token_usage': self.get_token_usage(),
            'trading_date': trading_date,
        }

    # ------------------------------------------------------------------
    # EOD helpers
    # ------------------------------------------------------------------

    def _save_eod_cycle_log(
        self,
        screened_tickers: list[str],
        quant_ctx: dict,
        position_research: dict,
        candidate_research: dict,
        prompt: str,
        llm_response: dict,
        entry_signals: list[dict],
        exit_signals: list[dict],
        risk_state: dict,
        portfolio_snapshot: dict,
        playbook_reads: list[str] | None = None,
    ) -> None:
        """Save structured EOD cycle log for later review."""
        from pathlib import Path

        log_dir = Path("state/logs/eod")
        log_dir.mkdir(parents=True, exist_ok=True)

        today = _now_et_iso()[:10]
        log_data = {
            "cycle_type": "EOD_SIGNAL",
            "date": today,
            "generated_at": _now_et_iso(),
            "portfolio_snapshot": portfolio_snapshot,
            "risk_state": risk_state,
            "pipeline": {
                "screened_candidates": screened_tickers,
                "screened_count": len(screened_tickers),
                "ranked_candidates": list(quant_ctx.get("candidates", {}).keys()),
                "ranked_count": len(quant_ctx.get("candidates", {})),
                "existing_positions": list(quant_ctx.get("positions", {}).keys()),
            },
            "quant_context": {
                "regime": quant_ctx.get("regime"),
                "strategy": quant_ctx.get("strategy"),
                "regime_confidence": quant_ctx.get("regime_confidence"),
                "market": quant_ctx.get("market", {}),
                "portfolio": quant_ctx.get("portfolio", {}),
                "positions": quant_ctx.get("positions", {}),
                "candidates": quant_ctx.get("candidates", {}),
            },
            "research": {
                "positions": position_research,
                "candidates": candidate_research,
            },
            "portfolio_agent": {
                "prompt": prompt,
                "llm_response": llm_response,
                "playbook_reads": playbook_reads or [],
            },
            "decisions": {
                "entry_signals": entry_signals,
                "exit_signals": exit_signals,
            },
        }

        log_path = log_dir / f"{today}.json"
        try:
            with open(log_path, "w") as f:
                json.dump(log_data, f, indent=2, default=str)
            logger.info("EOD cycle log saved: %s", log_path)
        except Exception as exc:
            logger.warning("EOD cycle log save failed: %s", exc)

    def _format_recent_trades(self, n: int = 10) -> list[dict]:
        """Return a summary of the last n closed trades, newest first."""
        trades = sorted(
            self.portfolio_state.trade_history,
            key=lambda t: t.timestamp,
            reverse=True,
        )[:n]
        result = []
        for t in trades:
            entry: dict = {
                'symbol': t.symbol,
                'date': t.timestamp[:10],
                'exit_type': t.strategy,
                'pnl': round(t.pnl, 2),
                'holding_days': t.holding_days,
            }
            if t.entry_price > 0:
                entry['return_pct'] = round(
                    (t.price - t.entry_price) / t.entry_price * 100, 2
                )
            result.append(entry)
        return result

    def _build_eod_prompt(
        self,
        quant_ctx: dict,
        new_entries_allowed: bool,
        recent_trades: list | None = None,
        recently_closed: list | None = None,
    ) -> str:
        """Build the PortfolioAgent LLM prompt for the EOD_SIGNAL decision."""
        from agents.prompts.v1_0 import EOD_INSTRUCTIONS, build_playbook_chapters
        from tools.journal.playbook import set_allowed_chapters
        from tools.journal.decision_log import set_cycle_expected_tickers
        set_allowed_chapters('eod')
        # Register expected tickers so submit tools can report coverage
        set_cycle_expected_tickers(
            positions=list(quant_ctx.get('positions', {}).keys()),
            candidates=list(quant_ctx.get('candidates', {}).keys()),
            watchlist_tickers=[e['ticker'] for e in load_watchlist()],
        )
        now_et = _now_et_iso(getattr(self, '_sim_date', None), cycle='EOD_SIGNAL')
        regime = quant_ctx.get('regime', 'UNKNOWN')
        regime_confidence = quant_ctx.get('regime_confidence', 0.3)
        regime_agreement = quant_ctx.get('regime_agreement', False)
        portfolio_heat = quant_ctx.get('portfolio', {}).get('portfolio_heat', 0.0)
        previous_regime = self.portfolio_state.last_regime

        regime_transition_note = ""
        if regime == "TRANSITIONAL" and previous_regime and previous_regime != "TRANSITIONAL":
            regime_transition_note = (
                f"  - Previous regime: {previous_regime} → TRANSITIONAL "
                f"(transitioning FROM {previous_regime})\n"
            )

        # Build PERSISTENT decision history preamble from AgentState
        from state.agent_state import get_state
        from tools.journal.pm_notes import load_pm_notes, format_pm_notes_for_prompt
        agent_state = get_state()

        # Ablation flags
        ablation = getattr(self, 'settings', None)
        notes_enabled = getattr(ablation, 'enable_pm_notes', True)
        history_enabled = getattr(ablation, 'enable_decision_history', True)
        playbook_enabled = getattr(ablation, 'enable_playbook', True)

        pm_notes: dict = {}
        if notes_enabled:
            pruned = agent_state.prune_stale_notes(
                as_of=getattr(self, '_sim_date', '') or '',
            )
            if pruned:
                agent_state.save()
                logger.info("EOD: pruned %d stale PM notes: %s", len(pruned), pruned)
            pm_notes = load_pm_notes()

        decision_history_section = ""
        if history_enabled:
            decision_history = build_decision_history(
                agent_state.decision_log,
                active_positions=set(self.portfolio_state.positions.keys()),
            )
            decision_history_section = (
                decision_history + "\n\n" if decision_history else ""
            )

        # ── Build XML-structured prompt ────────────────────────────────────
        sections = []

        sections.append(
            f"<cycle_header>\n"
            f"EOD_SIGNAL portfolio review. Current time (ET): {now_et}\n"
            f"</cycle_header>"
        )

        if pm_notes:
            # Ticker-level notes are now shown in <action_log> per-day.
            # Only show non-ticker keys (regime, lesson, etc.) here.
            general_notes = {
                k: v for k, v in pm_notes.items()
                if not (k.isalpha() and k.isupper() and len(k) <= 5)
            }
            if general_notes:
                sim_date = getattr(self, '_sim_date', None) or ''
                notes_text = format_pm_notes_for_prompt(general_notes, as_of=sim_date)
                sections.append(
                    f"<pm_notes>\n"
                    f"Your general notes (use notes param on submit to modify):\n"
                    f"{notes_text}\n"
                    f"</pm_notes>"
                )

        if decision_history_section:
            sections.append(
                f"<action_log>\n"
                f"{decision_history_section.strip()}\n"
                f"</action_log>"
            )

        regime_guidance = _regime_inline_guidance(regime)
        regime_lines = (
            f"Regime: {regime} ({regime_confidence:.0%} confidence, "
            f"rule/HMM {'agree' if regime_agreement else 'disagree'})\n"
            + regime_transition_note
            + ("  (Low confidence or disagreement — weight individual setup quality)\n"
               if not regime_agreement or regime_confidence < 0.6 else "")
            + regime_guidance
        )
        market_context = _format_market_context(quant_ctx.get('market', {})).strip()
        sections.append(
            f"<market>\n{regime_lines}\n{market_context}\n</market>"
        )

        pos_actions = "Actions: EXIT / PARTIAL_EXIT (sells half) / HOLD"
        sections.append(
            f"<portfolio>\n"
            f"{_format_portfolio_summary(quant_ctx.get('portfolio', {}), portfolio_heat).strip()}\n"
            f"{pos_actions}\n\n"
            f"{_format_positions_table(quant_ctx.get('positions', {})).strip()}\n"
            f"</portfolio>"
        )

        if new_entries_allowed and quant_ctx.get('candidates'):
            sections.append(
                f"<candidates>\n"
                f"All candidates below have passed system-level filters (liquidity, "
                f"volatility, sector cap, correlation, cooldown). "
                f"Evaluate them on setup quality, not on filter criteria.\n"
                f"Candidates are grouped by strategy type (MOMENTUM / MEAN REVERSION). "
                f"Evaluate the best setup within each group, then compare across groups "
                f"to decide which entries best fit the current regime and portfolio.\n"
                f"Capital budget: ~{self._entry_budget(quant_ctx)} new entries this cycle "
                f"(based on available position slots and cash).\n"
                f"Flags: SEC! = sector over-concentrated (can enter if planning to exit same-sector), "
                f"CORR! = correlated risk concentration.\n\n"
                f"{_format_candidates_table(quant_ctx.get('candidates', {})).strip()}\n"
                f"</candidates>"
            )
        else:
            sections.append("<candidates>\nNo new entry candidates this cycle.\n</candidates>")

        if recent_trades:
            sections.append(
                f"<trade_history>\n"
                f"Recent closed trades (last 10):\n"
                f"{json.dumps(recent_trades, indent=2)}\n"
                f"Patterns to consider: stops hit repeatedly? winners closed too early?\n"
                f"</trade_history>"
            )

        if recently_closed:
            sections.append(
                f"<closed_today>\n"
                f"{json.dumps(recently_closed, indent=2)}\n"
                f"Review these outcomes to inform today's decisions.\n"
                f"</closed_today>"
            )

        if playbook_enabled:
            sections.append(
                f"<playbook_chapters>\n{build_playbook_chapters('eod')}\n</playbook_chapters>"
            )

        if playbook_enabled:
            sections.append(EOD_INSTRUCTIONS)
        else:
            from agents.prompts.v1_0 import EOD_INSTRUCTIONS_NO_PLAYBOOK
            sections.append(EOD_INSTRUCTIONS_NO_PLAYBOOK)

        return "\n\n".join(sections)

    def _entry_budget(self, quant_ctx: dict) -> int:
        """Available position slots for new entries this cycle."""
        s = self.settings
        current_positions = len(self.portfolio_state.positions)
        return max(0, s.max_positions - current_positions)

    @staticmethod
    def _should_trigger_research(
        ticker: str,
        news_data: dict,
        quant_ctx_section: dict,
        volume_trigger: float,
        price_trigger_atr: float,
    ) -> bool:
        """Decide if a ticker needs LLM research based on news + quant signals."""
        from tools.journal.research_log import load_research_history

        ticker_news = news_data.get(ticker.upper(), {})
        if ticker_news.get('article_count', 0) > 0:
            return True

        ctx = quant_ctx_section.get(ticker, {})
        if ctx.get('volume_ratio', 1.0) >= volume_trigger:
            return True

        atr_pct = ctx.get('atr', 0) / ctx.get('current_price', 1) if ctx.get('current_price') else 0
        return_1d = abs(ctx.get('return_1d', 0))
        if atr_pct > 0 and return_1d > price_trigger_atr * atr_pct:
            return True

        if not load_research_history(ticker, last_n=1):
            return True

        return False

    def _extract_eod_exit_signals(
        self,
        decisions: list[dict],
        existing_positions: dict,
        position_ctx: dict,
    ) -> list[dict]:
        """Convert LLM existing_decisions to exit_signals format."""
        exit_signals = []
        for dec in decisions:
            ticker = dec.get('ticker', '')
            action = dec.get('action', 'HOLD').upper()
            pos = existing_positions.get(ticker)
            if not pos:
                continue

            if action not in ('EXIT', 'HOLD', 'PARTIAL_EXIT', 'TIGHTEN'):
                action = 'HOLD'

            ctx = position_ctx.get(ticker, {})
            sig: dict = {
                'ticker': ticker,
                'held_position': True,
                'action': action,
                'reason': dec.get('reason', ''),
                'for': dec.get('for', ''),
                'against': dec.get('against', ''),
                'conviction': dec.get('conviction', ''),
                'current_price': pos.current_price,
                'current_stop_loss': pos.stop_loss_price,
                'qty': pos.qty,
                'atr': ctx.get('atr', 0.0),
                'bracket_order_id': pos.bracket_order_id,
            }
            if action == 'PARTIAL_EXIT':
                if pos.partial_exit_count >= 1:
                    # Already had a partial exit — escalate to full EXIT
                    logger.info("EOD_SIGNAL: %s PARTIAL_EXIT escalated to EXIT (already %d partial(s))",
                                ticker, pos.partial_exit_count)
                    action = 'EXIT'
                    sig['action'] = 'EXIT'
            exit_signals.append(sig)
            logger.debug("EOD_SIGNAL: %s → %s (%s)", ticker, action, sig['reason'])
        return exit_signals

    def _generate_auto_add_signals(
        self,
        decisions: list[dict],
        existing_positions: dict,
        position_ctx: dict,
    ) -> list[dict]:
        """Auto-ADD: half-size positions with high conviction are scaled to full.

        The agent does not select ADD — it only evaluates conviction.
        When a half-size position receives high conviction on HOLD,
        the system automatically generates an ADD signal.
        """
        add_signals = []
        for dec in decisions:
            action = dec.get('action', '').upper()
            conviction = dec.get('conviction', '').lower()
            if action != 'HOLD' or conviction != 'high':
                continue

            ticker = dec.get('ticker', '')
            pos = existing_positions.get(ticker)
            if not pos or not pos.scaled_entry:
                continue  # Only half-size positions
            if pos.consecutive_high_conviction < 1:
                continue  # Require at least 1 EOD cycle with high conviction

            ctx = position_ctx.get(ticker, {})
            atr = ctx.get('atr', 0.0)
            current_price = pos.current_price
            if atr <= 0 or current_price <= 0:
                continue

            add_shares = ctx.get('add_shares') or pos.qty

            add_signals.append({
                'ticker': ticker,
                'held_position': True,
                'action': 'AUTO_ADD',
                'strategy': pos.strategy or 'MOMENTUM',
                'reason': dec.get('for', ''),
                'shares': add_shares,
                'stop_loss_price': pos.stop_loss_price,
                'take_profit_price': round(current_price + 3.0 * atr, 2),
                'entry_price': current_price,
                'atr': atr,
                'suggested_stop_loss': pos.stop_loss_price,
                'entry_type': 'MARKET',
            })
            logger.info("EOD_SIGNAL: Auto-ADD %s +%d shares (half-size + high conviction)", ticker, add_shares)
        return add_signals

    def _extract_eod_entry_signals(
        self,
        new_entries: list[dict],
        candidate_ctx: dict,
    ) -> list[dict]:
        """Convert LLM new_entries to sized entry signals."""
        entry_signals = []
        for entry in new_entries:
            ticker = entry.get('ticker', '')
            if entry.get('action', '').upper() != 'LONG':
                continue
            ctx = candidate_ctx.get(ticker)
            if not ctx or ctx.get('indicative_shares', 0) < 1:
                logger.debug("EOD_SIGNAL: skipping %s — no indicative shares.", ticker)
                continue

            # EOD LONG is always MARKET — LIMIT is only available via MORNING ADJUST
            entry_type = 'MARKET'
            strategy = ctx.get('strategy', 'MOMENTUM')
            # Half-size scaled entry: PM sets half_size=true → enter 50% shares
            shares = ctx['indicative_shares']
            if entry.get('half_size'):
                shares = max(1, shares // 2)
                logger.info("EOD_SIGNAL: %s half_size → %d shares (full=%d)", ticker, shares, ctx['indicative_shares'])
            is_half = bool(entry.get('half_size'))
            entry_signals.append({
                'ticker': ticker,
                'held_position': False,
                'action': 'LONG',
                'strategy': strategy,
                'reason': entry.get('reason', ''),
                'conviction': entry.get('conviction', ''),
                'for': entry.get('for', ''),
                'against': entry.get('against', ''),
                'shares': shares,
                'stop_loss_price': ctx['suggested_stop_loss'],
                'take_profit_price': ctx['suggested_take_profit'],
                'entry_price': ctx['current_price'],
                'atr': ctx['atr'],
                'suggested_stop_loss': ctx['suggested_stop_loss'],
                'entry_type': entry_type,
                'half_size': is_half,
            })
        return entry_signals
