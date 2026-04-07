"""
agents/_morning_cycle.py — MORNING cycle implementation.

Mixin class providing _run_morning_cycle and its helpers.
Works for both live trading (AlpacaBroker + LiveProvider) and
backtesting (MockBroker + FixtureProvider) via dependency injection.
"""

from __future__ import annotations

import json
import logging
from datetime import date as _date
from typing import Any

from agents._formatting import _now_et_iso, _extract_playbook_reads
from tools.journal.decision_log import consume_cycle_decisions, try_rescue_from_text

logger = logging.getLogger(__name__)


def _format_quant_metric(
    label: str, eod: dict, morn: dict, key: str, fmt: str = '.2f',
) -> str:
    """Format one quant metric as a side-by-side EOD vs Morning line."""
    ev = eod.get(key)
    mv = morn.get(key)
    if ev is None and mv is None:
        return ""

    is_pct = '%' in fmt
    is_dollar = fmt.startswith('$')

    def _fv(v):
        if v is None:
            return f"{'n/a':>10}"
        if is_pct:
            return f"{v * 100:>+9.1f}%"
        if is_dollar:
            return f"${v:>8.2f}"
        return f"{v:>10{fmt.lstrip('+$')}}"

    delta = ""
    if ev is not None and mv is not None:
        d = mv - ev
        if is_pct:
            delta = f"{d * 100:>+9.1f}%"
        else:
            delta = f"{d:>+10{fmt.lstrip('+$%')}}"
    else:
        delta = f"{'':>10}"

    return f"  {label:<10} {_fv(ev)}  {_fv(mv)}  {delta}"


def _record_trade(portfolio_state, pos, result, sim_date: str) -> None:
    """Record a closed trade into portfolio_state.trade_history."""
    from state.portfolio_state import Trade

    try:
        entry_dt = _date.fromisoformat(pos.entry_date)
        exit_dt = _date.fromisoformat(sim_date)
        holding_days = (exit_dt - entry_dt).days
    except (ValueError, AttributeError):
        holding_days = 0
    sig_price = getattr(pos, 'signal_price', 0.0) or 0.0
    entry_slippage = 0.0
    if sig_price > 0 and pos.avg_entry_price > 0:
        entry_slippage = round(
            (pos.avg_entry_price - sig_price) / sig_price * 10_000, 1,
        )
    trade = Trade(
        symbol=pos.symbol,
        side='sell',
        qty=result.get('exit_qty') or result.get('qty', pos.qty),
        price=result.get('exit_price') or result.get('entry_price', 0),
        pnl=result.get('pnl', 0),
        timestamp=f'{sim_date}T09:30:00Z',
        strategy=getattr(pos, 'strategy', '') or result.get('action', 'EXIT'),
        entry_price=pos.avg_entry_price,
        holding_days=holding_days,
        signal_price=sig_price,
        slippage_bps=entry_slippage,
    )
    portfolio_state.record_trade(trade)

    # Auto-clean PM notes for closed ticker
    try:
        from state.agent_state import get_state
        removed = get_state().clear_ticker_notes(pos.symbol)
        if removed:
            logger.info("PM notes auto-cleaned for %s: %s", pos.symbol, removed)
    except Exception:
        pass


class MorningCycleMixin:
    """Methods for the MORNING cycle, mixed into PortfolioAgent."""

    def _run_morning_cycle(self) -> dict[str, Any]:
        """
        MORNING pipeline (09:00 ET).

        Steps:
          1. load pending_signals        (system — abort if stale/missing)
          2. portfolio-sync              (system)
          3. overnight research          (Research Agent — inline, stateless)
          4. execute exits + TIGHTEN     (system — immediate risk management)
             - conflict detection: HOLD+risk_flag / EXIT+positive_catalyst → defer to LLM
          5. entry triage                (system — split auto-approved vs needs-LLM)
             - negative_catalyst or gap ±3% → needs LLM
          6. LLM re-judgment             (only conflicting entries + deferred exits)
          7. sizing + place orders       (system — re-validate with live portfolio)
          8. fill pending orders          (simulation only — no-op for live broker)
          9. structured log + clear      (system)
        """
        from tools.risk.position_sizing import calculate_position_size
        from state.portfolio_state import Position

        s = self.settings
        sim_date = getattr(self, '_sim_date', None)
        today_str = sim_date or str(_date.today())
        morning_prompt = ''

        # ── Step 1: Load pending signals ────────────────────────────────────
        pending = self.portfolio_state.load_pending_signals(sim_date=sim_date)
        if not pending:
            logger.warning("MORNING: no valid pending signals — skipping cycle.")
            return {"cycle_type": "MORNING", "orders_placed": 0,
                    "skipped_reason": "no_valid_pending_signals"}

        entry_signals = pending.get('signals', [])
        exit_signals = pending.get('exit_signals', [])

        if not entry_signals and not exit_signals:
            self.portfolio_state.clear_pending_signals()
            return {"cycle_type": "MORNING", "orders_placed": 0,
                    "skipped_reason": "empty_signal_list"}

        # ── Step 2: Sync portfolio ──────────────────────────────────────────
        portfolio = self._get_broker().sync(sim_date, existing_positions=self.portfolio_state.positions)
        if portfolio.get('error'):
            logger.error("MORNING: portfolio sync failed: %s", portfolio['error'])
            return {"cycle_type": "MORNING", "orders_placed": 0, "error": "portfolio_sync_failed"}

        # Propagate broker-synced values back to agent state
        self.portfolio_state.cash = portfolio['cash']
        self.portfolio_state.portfolio_value = portfolio['portfolio_value']
        self.portfolio_state.peak_value = max(
            self.portfolio_state.peak_value, portfolio['peak_value']
        )
        # Sync positions from Alpaca into agent state
        positions_full = portfolio.get('positions_full', {})
        if positions_full:
            synced_syms = set(positions_full.keys())
            for sym, pos_data in positions_full.items():
                if sym not in self.portfolio_state.positions:
                    self.portfolio_state.positions[sym] = Position.from_dict(pos_data)
                else:
                    # Update price/pnl but keep local fields (strategy, stop, etc.)
                    local = self.portfolio_state.positions[sym]
                    local.current_price = pos_data.get('current_price', local.current_price)
                    local.unrealized_pnl = pos_data.get('unrealized_pnl', local.unrealized_pnl)
                    local.qty = pos_data.get('qty', local.qty)
            # Remove positions closed on Alpaca
            for sym in list(self.portfolio_state.positions.keys()):
                if sym not in synced_syms:
                    del self.portfolio_state.positions[sym]

        # ── Step 3: Overnight research (inline, stateless) ─────────────────
        researcher = self._get_researcher()

        existing_tickers = list(self.portfolio_state.positions.keys())

        position_research: dict = {}
        candidate_research: dict = {}

        entry_tickers = [sig['ticker'] for sig in entry_signals]
        all_morning_tickers = existing_tickers + entry_tickers

        # Earnings screening
        earnings_map: dict[str, int] = {}
        if all_morning_tickers:
            try:
                earnings_map = self._get_provider().get_earnings(all_morning_tickers)
            except Exception as exc:
                logger.warning("MORNING: earnings screening failed: %s", exc)

        # Batch overnight news fetch (12 hours)
        morning_news: dict = {}
        if all_morning_tickers:
            try:
                morning_news = self._get_provider().get_news(all_morning_tickers, hours_back=12)
                logger.info("MORNING: batch news fetch — %d tickers.", len(all_morning_tickers))
            except Exception as exc:
                logger.warning("MORNING: batch news fetch failed: %s", exc)

        # Triage: only research tickers with overnight news
        triggered_positions = [
            t for t in existing_tickers
            if morning_news.get(t, {}).get('article_count', 0) > 0
        ]
        triggered_candidates = [
            t for t in entry_tickers
            if morning_news.get(t, {}).get('article_count', 0) > 0
        ]
        logger.info(
            "MORNING: research triage — positions %d/%d, candidates %d/%d triggered.",
            len(triggered_positions), len(existing_tickers),
            len(triggered_candidates), len(entry_tickers),
        )

        if triggered_positions:
            try:
                position_research = researcher.morning_research_positions(
                    triggered_positions,
                    pre_fetched_news=morning_news,
                    earnings_map=earnings_map or None,
                    sim_date=sim_date,
                )
                logger.info("MORNING: position research complete — %d tickers.",
                            sum(1 for v in position_research.values() if v is not None))
            except Exception as exc:
                logger.warning("MORNING: position research failed: %s", exc)

        if triggered_candidates:
            try:
                candidate_research = researcher.morning_research_candidates(
                    triggered_candidates,
                    pre_fetched_news=morning_news,
                    earnings_map=earnings_map or None,
                    sim_date=sim_date,
                )
                logger.info("MORNING: candidate research complete — %d tickers.",
                            len(candidate_research))
            except Exception as exc:
                logger.warning("MORNING: candidate research failed: %s", exc)

        if triggered_positions or triggered_candidates:
            print(f"  [MORNING] overnight research: {len(triggered_positions)} positions, "
                  f"{len(triggered_candidates)} candidates", flush=True)

        # ── Step 4: Execute exits + TIGHTEN (always — halted or not) ──
        exit_orders_placed: list[dict] = []
        exit_orders_failed: list[dict] = []
        partial_exits_placed: list[dict] = []
        stops_tightened: list[dict] = []
        exit_deferred_to_llm: list[dict] = []
        exits_count: int = 0  # track exits for position_count adjustment

        for sig in exit_signals:
            ticker = sig['ticker']
            action = sig.get('action', 'HOLD')
            pr = position_research.get(ticker)

            risk_flag = (isinstance(pr, dict)
                         and pr.get('risk_level', 'none') in ('flag', 'veto'))
            pos_catalyst = isinstance(pr, dict) and pr.get('positive_catalyst')
            has_conflict = (
                (action == 'HOLD' and risk_flag)
                or (action == 'EXIT' and pos_catalyst)
            )
            if has_conflict:
                logger.info("MORNING: %s (%s) deferred to LLM — risk_flag=%s, positive_catalyst=%s",
                            ticker, action, risk_flag, pos_catalyst)
                exit_deferred_to_llm.append({
                    **sig,
                    'overnight_summary': pr.get('summary', ''),
                    'time_sensitive_facts': pr.get('facts', []),
                    'risk_flag': risk_flag,
                    'positive_catalyst': pos_catalyst,
                })
                continue

            if action == 'TIGHTEN':
                pos = self.portfolio_state.positions.get(ticker)
                if pos:
                    pos.tighten_active = True
                    conviction = sig.get('conviction', '')
                    if conviction:
                        pos.last_conviction = conviction
                    stops_tightened.append({'ticker': ticker, 'conviction': conviction})
                continue

            if action == 'PARTIAL_EXIT':
                pos = self.portfolio_state.positions.get(ticker)
                if not pos:
                    logger.warning("MORNING: PARTIAL_EXIT for %s but no position found.", ticker)
                    continue
                # Already had a partial exit — escalate to full EXIT
                if pos.partial_exit_count >= 1:
                    logger.info("MORNING: %s PARTIAL_EXIT → EXIT (already %d partial exits)",
                                ticker, pos.partial_exit_count)
                    action = 'EXIT'
                    sig = {**sig, 'qty': pos.qty, 'action': 'EXIT'}
                else:
                    # 1st partial: sell half. 2nd partial: sell all remaining.
                    if pos.partial_exit_count >= 1:
                        sell_qty = pos.qty
                    else:
                        sell_qty = max(1, pos.qty // 2)
                    if sell_qty >= pos.qty:
                        action = 'EXIT'
                        sig = {**sig, 'qty': pos.qty}
                    else:
                        result = self._get_broker().execute_exit(ticker, qty=sell_qty)
                        if result and result.get('error'):
                            exit_orders_failed.append({'symbol': ticker, 'error': result['error']})
                            logger.error("MORNING: partial exit failed for %s — %s",
                                         ticker, result['error'])
                        elif result:
                            partial_exits_placed.append({**result, 'exit_pct': 0.5})
                            # Record partial exit in trade history
                            _record_trade(self.portfolio_state, pos, result, today_str)
                            if ticker in self.portfolio_state.positions and ticker in self._get_broker().positions:
                                self.portfolio_state.positions[ticker].qty = self._get_broker().positions[ticker].qty
                            pos.partial_exit_count += 1
                            logger.info(
                                "MORNING: partial exit for %s qty=%d (50%% of %d, partial #%d) (MOO)",
                                ticker, sell_qty, pos.qty + sell_qty, pos.partial_exit_count,
                            )
                        continue

            if action != 'EXIT':
                continue

            qty = sig.get('qty', 0)
            if qty <= 0:
                pos = self.portfolio_state.positions.get(ticker)
                qty = pos.qty if pos else 0
            if qty <= 0:
                logger.warning("MORNING: cannot exit %s — qty unknown.", ticker)
                continue

            pos = self.portfolio_state.positions.get(ticker)
            result = self._get_broker().execute_exit(ticker, qty=qty)
            if result and result.get('error'):
                exit_orders_failed.append({'symbol': ticker, 'error': result.get('error')})
                logger.error("MORNING: exit order failed for %s — %s", ticker, result.get('error'))
            elif result:
                exit_orders_placed.append(result)
                if pos:
                    _record_trade(self.portfolio_state, pos, result, today_str)
                if ticker in self.portfolio_state.positions:
                    del self.portfolio_state.positions[ticker]
                    exits_count += 1
                logger.info("MORNING: exit order placed for %s qty=%d (MOO)", ticker, qty)

        logger.info("MORNING: exits=%d, partial=%d, failed=%d, tightened=%d.",
                    len(exit_orders_placed), len(partial_exits_placed),
                    len(exit_orders_failed), len(stops_tightened))

        # ── Step 5–7: Entry triage, LLM re-judgment, sizing + orders ────────
        placed_orders: list[dict] = []
        failed_orders: list[dict] = []
        llm_rejected: list[dict] = []
        rejected_sizing: list[dict] = []
        morning_decisions: list[dict] = []
        msg_idx_before_morning: int | None = None

        if entry_signals or exit_deferred_to_llm:
            # ── Step 5: Entry triage ──
            all_entry_tickers = [sig['ticker'] for sig in entry_signals]
            premarket_quotes = self._get_provider().get_quotes(
                all_entry_tickers + [sig['ticker'] for sig in exit_deferred_to_llm]
            )

            auto_approved: list[dict] = []
            needs_llm: list[dict] = []

            for sig in entry_signals:
                tick = sig['ticker']
                cr = candidate_research.get(tick)
                neg_catalyst = (isinstance(cr, dict)
                                and (cr.get('negative_catalyst')
                                     or cr.get('risk_level', 'none') in ('flag', 'veto')))
                if isinstance(cr, dict):
                    sig = {**sig,
                           'overnight_summary': cr.get('summary', ''),
                           'time_sensitive_facts': cr.get('facts', []),
                           'negative_catalyst': neg_catalyst}

                has_negative = neg_catalyst
                if has_negative:
                    needs_llm.append(sig)
                    continue

                # LIMIT/STOP orders: skip gap/premarket triage — R:R checked at fill price
                if sig.get('entry_type', '').upper() in ('LIMIT', 'STOP') and sig.get('limit_price'):
                    auto_approved.append(sig)
                    continue

                eod_price = sig.get('entry_price', 0.0)
                quote = premarket_quotes.get(tick)
                if quote and eod_price:
                    live_price = quote.get('ask_price') or quote.get('mid_price') or 0.0
                    if live_price:
                        gap_pct = abs((live_price - eod_price) / eod_price)
                        if gap_pct >= s.gap_threshold_pct:
                            sig = {**sig, 'premarket_price': live_price,
                                   'gap_pct': round((live_price - eod_price) / eod_price * 100, 2)}
                            needs_llm.append(sig)
                            continue

                        atr = sig.get('atr') or 0.0
                        strategy = sig.get('strategy', 'MOMENTUM')

                        # Use expected fill price for R:R check, not raw live_price.
                        if strategy == 'MEAN_REVERSION' and atr > 0:
                            rr_price = round(eod_price - 0.3 * atr, 2)
                        elif strategy == 'MOMENTUM' and atr > 0:
                            rr_price = round(eod_price + 0.1 * atr, 2)
                        else:
                            rr_price = live_price

                        new_stop = round(rr_price - s.atr_stop_multiplier * atr, 2) if atr > 0 else sig.get('stop_loss_price', 0.0)

                        # Preserve EOD take-profit for MR (MA20 target) — only
                        # recalculate with ATR formula if EOD TP is missing.
                        eod_tp = sig.get('take_profit_price', 0.0)
                        is_mr = strategy == 'MEAN_REVERSION'
                        is_pead = strategy == 'PEAD'
                        if is_mr and eod_tp > rr_price:
                            new_tp = eod_tp
                        elif is_pead:
                            tp_multiplier = s.pead_take_profit_atr
                            new_tp = round(rr_price + tp_multiplier * atr, 2) if atr > 0 else eod_tp
                        else:
                            new_tp = round(rr_price + 3.0 * atr, 2) if atr > 0 else eod_tp
                        # No R:R revalidation here — gap is below threshold,
                        # so fill price ≈ EOD price. PM already evaluated R:R
                        # at EOD; rechecking with a system threshold would
                        # override PM judgment on the same numbers.
                        # R:R degradation from large gaps is caught by the
                        # gap_threshold check above (→ LLM re-judgment).
                        sig = {**sig,
                               'entry_price': rr_price,
                               'stop_loss_price': new_stop,
                               'take_profit_price': new_tp}

                auto_approved.append(sig)

            logger.info("MORNING: entry triage — %d auto-approved, %d needs LLM.",
                        len(auto_approved), len(needs_llm))

            # ── Step 6: LLM re-judgment ──
            approved_by_llm: list[dict] = []
            if needs_llm or exit_deferred_to_llm:
                # Build quant context for flagged items
                morning_quant: dict = {}
                flagged_tickers = (
                    [sig['ticker'] for sig in needs_llm]
                    + [sig['ticker'] for sig in exit_deferred_to_llm]
                )
                if flagged_tickers:
                    try:
                        from agents.quant_engine import QuantEngine
                        qe = QuantEngine(self.settings)
                        morning_bars = self._get_provider().get_bars(
                            flagged_tickers, end=sim_date,
                        )
                        morning_quant = qe.build_morning_context(
                            tickers=flagged_tickers,
                            bars=morning_bars,
                            existing_positions=self.portfolio_state.positions,
                            eod_quant_positions=pending.get('quant_positions'),
                            eod_quant_candidates=pending.get('quant_candidates'),
                        )
                    except Exception as exc:
                        logger.warning("MORNING: quant context build failed: %s", exc)

                morning_prompt = self._build_morning_prompt(
                    needs_llm,
                    pending.get('regime', 'UNKNOWN'), portfolio,
                    latest_quotes=premarket_quotes,
                    exit_deferred=exit_deferred_to_llm,
                    quant_context=morning_quant,
                    regime_confidence=pending.get('regime_confidence'),
                )
                print(f"  [MORNING] LLM re-judgment: {len(needs_llm)} entries, "
                      f"{len(exit_deferred_to_llm)} exits deferred", flush=True)
                self._swap_submit_tool('MORNING')
                msg_idx_before_morning = len(getattr(self.agent, 'messages', []))
                llm_text = self.run(morning_prompt)
                try_rescue_from_text(llm_text)
                morning_decisions = consume_cycle_decisions()
                if not morning_decisions:
                    logger.error("MORNING: no decisions submitted — rejecting all entries, "
                                 "executing deferred exits as safety fallback.")

                entry_dec_list = [
                    d for d in morning_decisions
                    if d.get('action', '').upper() in ('CONFIRM', 'REJECT', 'ADJUST')
                ]
                exit_review_list = [
                    d for d in morning_decisions
                    if d.get('action', '').upper() in ('EXIT', 'HOLD')
                ]

                llm_decisions = {
                    d.get('ticker', '').upper(): d
                    for d in entry_dec_list
                }
                for sig in needs_llm:
                    tick = sig['ticker']
                    dec = llm_decisions.get(tick, {})
                    action = dec.get('action', 'CONFIRM').upper()
                    if action == 'REJECT':
                        reject_reason = dec.get('against') or dec.get('reason') or 'LLM rejected'
                        llm_rejected.append({
                            'ticker': tick,
                            'reason': reject_reason,
                            'eod_action': 'LONG',
                            'for': dec.get('for', ''),
                            'against': dec.get('against', ''),
                        })
                        # Clean up EOD notes for rejected ticker
                        try:
                            from state.agent_state import get_state
                            removed = get_state().clear_ticker_notes(tick)
                            if removed:
                                logger.info("PM notes auto-cleaned for rejected %s: %s", tick, removed)
                        except Exception:
                            pass
                        logger.info("MORNING: %s rejected by LLM — %s",
                                    tick, reject_reason)
                        continue
                    if action == 'ADJUST':
                        if dec.get('adjusted_limit_price'):
                            sig = {**sig,
                                   'entry_type': 'LIMIT',
                                   'limit_price': float(dec['adjusted_limit_price'])}
                            logger.info("MORNING: %s ADJUST → LIMIT @ $%.2f",
                                        tick, float(dec['adjusted_limit_price']))
                    approved_by_llm.append(sig)

                logger.info("MORNING: LLM review — %d confirmed, %d rejected out of %d.",
                            len(approved_by_llm), len(llm_rejected), len(needs_llm))

                exit_review_map = {
                    d.get('ticker', '').upper(): d
                    for d in exit_review_list
                }
                morning_exit_details: list[dict] = []
                for sig in exit_deferred_to_llm:
                    tick = sig['ticker']
                    dec = exit_review_map.get(tick, {})
                    review_action = dec.get('action', 'EXIT').upper()
                    eod_action = sig.get('action', 'HOLD')
                    conflict_reason = sig.get('overnight_summary', '')
                    if review_action == 'HOLD':
                        reason = dec.get('for') or dec.get('reason') or dec.get('against') or 'LLM kept position (no reason provided)'
                        morning_exit_details.append({
                            'ticker': tick,
                            'eod_action': eod_action,
                            'morning_action': 'HOLD',
                            'reason': reason,
                            'against': dec.get('against', ''),
                            'conflict': conflict_reason,
                        })
                        logger.info("MORNING: %s EXIT cancelled by LLM — %s",
                                    tick, reason)
                        print(f"  [MORNING] {tick}: EXIT cancelled — {reason[:60]}",
                              flush=True)
                    else:
                        pos = self.portfolio_state.positions.get(tick)
                        qty = sig.get('qty', 0) or (pos.qty if pos else 0)
                        if qty > 0:
                            result = self._get_broker().execute_exit(tick, qty=qty)
                            if result and result.get('error'):
                                exit_orders_failed.append({'symbol': tick, 'error': result['error']})
                            elif result:
                                exit_orders_placed.append(result)
                                if pos:
                                    _record_trade(self.portfolio_state, pos, result, today_str)
                                if tick in self.portfolio_state.positions:
                                    del self.portfolio_state.positions[tick]
                                    exits_count += 1
                                exit_reason = dec.get('for') or dec.get('reason') or dec.get('against') or 'LLM confirmed exit (no reason provided)'
                                morning_exit_details.append({
                                    'ticker': tick,
                                    'eod_action': eod_action,
                                    'morning_action': 'EXIT',
                                    'reason': exit_reason,
                                    'against': dec.get('against', ''),
                                    'conflict': conflict_reason,
                                    'fill_price': result.get('fill_price'),
                                    'pnl': result.get('pnl'),
                                })
                                logger.info("MORNING: %s EXIT confirmed by LLM after review (MOO).", tick)

            approved_signals = auto_approved + approved_by_llm

            # ── Step 7: Sizing + place orders ───────────────────────────────
            portfolio_value = portfolio['portfolio_value']
            position_count = portfolio['position_count'] - exits_count
            orders_to_place: list[dict] = []

            for sig in approved_signals:
                atr = sig.get('atr', 0.0)
                entry_price = sig.get('entry_price', 0.0)
                if entry_price <= 0:
                    entry_price = (
                        sig.get('suggested_stop_loss', 0.0) + atr * s.atr_stop_multiplier
                    )

                # If ADJUST converted entry to LIMIT, use limit_price as entry
                # and let the system recompute stop/sizing from the new price.
                adjusted_limit = sig.get('limit_price')
                limit_entry_type = sig.get('entry_type', '').upper()
                if limit_entry_type == 'LIMIT' and adjusted_limit:
                    entry_price = float(adjusted_limit)
                    sig = {**sig, 'entry_price': entry_price,
                           'stop_loss_price': round(entry_price - atr * s.atr_stop_multiplier, 2)}
                    # Force re-sizing from new entry price
                    sig.pop('shares', None)

                # Use EOD-computed shares if available (preserves half_size, drawdown adjustment).
                # Fall back to live re-sizing only if EOD shares are missing.
                is_add = sig.get('held_position', False)
                eod_shares = sig.get('shares', 0)
                if eod_shares > 0:
                    # AUTO_ADD (scaling existing position) skips slot check
                    if not is_add and position_count >= s.max_positions_hard:
                        rejected_sizing.append({'ticker': sig['ticker'],
                                                'reason': 'max_positions_reached'})
                        continue
                    stop_loss_price = sig.get('stop_loss_price',
                                              round(entry_price - atr * s.atr_stop_multiplier, 2))
                    sizing = {
                        'approved': True,
                        'shares': eod_shares,
                        'stop_loss_price': stop_loss_price,
                    }
                else:
                    sizing = calculate_position_size(
                        ticker=sig['ticker'],
                        entry_price=entry_price,
                        atr=atr,
                        portfolio_value=portfolio_value,
                        current_position_count=position_count,
                        max_positions=s.max_positions_hard,
                        position_size_pct=s.position_size_pct,
                        atr_stop_multiplier=s.atr_stop_multiplier,
                    )
                if not sizing['approved']:
                    rejected_sizing.append({'ticker': sig['ticker'],
                                            'reason': sizing.get('rejection_reason', 'sizing_failed')})
                    continue

                take_profit = sig.get('take_profit_price',
                                      round(entry_price + atr * 3.0, 2))

                # MARKET is default for all EOD LONGs. LIMIT only when
                # MORNING ADJUST explicitly sets entry_type + limit_price.
                llm_entry_type = sig.get('entry_type', '').upper()
                sig_limit = sig.get('limit_price')

                strategy = sig.get('strategy', 'MOMENTUM')

                if llm_entry_type == 'LIMIT' and sig_limit:
                    order_type = 'limit'
                    order_limit_price = round(float(sig_limit), 2)
                    order_tif = 'day'
                else:
                    order_type = 'market'
                    order_limit_price = None
                    order_tif = 'opg'

                orders_to_place.append({
                    'symbol': sig['ticker'],
                    'qty': sizing['shares'],
                    'side': 'buy',
                    'stop_loss_price': round(sizing['stop_loss_price'], 2),
                    'take_profit_price': round(take_profit, 2),
                    'order_type': order_type,
                    'limit_price': order_limit_price,
                    'time_in_force': order_tif,
                    'strategy': strategy,
                    'signal_price': entry_price,
                    'half_size': sig.get('half_size', False),
                })
                if not is_add:
                    position_count += 1

            half_size_tickers: set[str] = set()
            for order in orders_to_place:
                strategy = order.pop('strategy', 'MOMENTUM')
                is_half = order.pop('half_size', False)
                result = self._get_broker().submit_entry(
                    ticker=order['symbol'],
                    shares=order['qty'],
                    stop_loss=order['stop_loss_price'],
                    take_profit=order['take_profit_price'],
                    strategy=strategy,
                    signal_price=order.get('signal_price', 0.0),
                    entry_type=order.get('order_type', 'MARKET').upper(),
                    limit_price=order.get('limit_price'),
                )
                if result.get('error'):
                    failed_orders.append({'symbol': order['symbol'], 'error': result['error']})
                    logger.error("MORNING: buy order failed for %s — %s",
                                 order['symbol'], result['error'])
                else:
                    result['strategy'] = strategy
                    placed_orders.append(result)
                    if is_half:
                        half_size_tickers.add(order['symbol'])
                    result['bracket_order_id'] = result.get('order_id', '')
                    # Remove from watchlist only after order is successfully placed
                    from tools.journal.watchlist import remove_from_watchlist
                    remove_from_watchlist(order['symbol'])
                    logger.info("MORNING: %s buy order placed for %s qty=%d (order_id=%s)",
                                strategy, order['symbol'], order['qty'],
                                result.get('order_id', 'n/a'))

            # ── Step 8: Fill pending orders (simulation only) ──────────────
            regime = pending.get('regime', 'UNKNOWN')
            fills = self._get_broker().fill_pending(cutoff_utc='14:30')
            for f in fills:
                if f['action'] == 'ENTRY_FILLED':
                    ticker = f['ticker']
                    existing = self.portfolio_state.positions.get(ticker)
                    if existing:
                        bp = self._get_broker().positions.get(ticker)
                        if bp:
                            existing.qty = bp.qty
                            existing.avg_entry_price = bp.avg_entry_price
                            existing.current_price = bp.current_price
                            existing.entry_qty = bp.qty  # update after AUTO_ADD fill
                            existing.scaled_entry = False  # AUTO_ADD filled → no longer half-size
                            existing.scaled_up = True      # signal to show in next EOD position table
                    else:
                        self.portfolio_state.positions[ticker] = Position(
                            symbol=ticker, qty=f['shares'],
                            avg_entry_price=f['fill_price'], current_price=f['fill_price'],
                            stop_loss_price=f.get('stop_loss', 0.0),
                            signal_price=f.get('signal_price', 0.0),
                            entry_date=today_str, strategy=f.get('strategy', 'MOMENTUM'),
                            entry_conditions={'regime': regime},
                            entry_qty=f['shares'],
                            scaled_entry=ticker in half_size_tickers,
                        )
                    print(f"  [MORNING] FILL: {ticker} @ ${f['fill_price']:.2f} "
                          f"x{f['shares']} ({f.get('order_type', 'MARKET')})", flush=True)
                elif f['action'] == 'ENTRY_REJECTED':
                    print(f"  [MORNING] REJECTED: {f['ticker']} — {f.get('reason', '')}",
                          flush=True)

        # ── Step 9: Structured log + final sync + clear ─────────────────────
        morning_playbook_reads = (
            _extract_playbook_reads(self, since_msg_idx=msg_idx_before_morning)
            if msg_idx_before_morning is not None else []
        )
        self._save_morning_cycle_log(
            position_research=position_research,
            candidate_research=candidate_research,
            exit_signals=exit_signals,
            entry_signals=entry_signals,
            exit_orders_placed=exit_orders_placed,
            placed_orders=placed_orders,
            llm_rejected=llm_rejected,
            portfolio_snapshot={'cash': portfolio['cash'], 'value': portfolio['portfolio_value']},
            playbook_reads=morning_playbook_reads,
        )
        final_portfolio = self._get_broker().sync(sim_date, existing_positions=self.portfolio_state.positions)
        if not final_portfolio.get('error'):
            self.portfolio_state.cash = final_portfolio['cash']
            self.portfolio_state.portfolio_value = final_portfolio['portfolio_value']
            self.portfolio_state.peak_value = max(
                self.portfolio_state.peak_value, final_portfolio['peak_value']
            )
        self.portfolio_state.clear_pending_signals()

        filled_count = sum(1 for f in (fills if entry_signals or exit_deferred_to_llm else [])
                          if f.get('action') == 'ENTRY_FILLED')
        rejected_count = sum(1 for f in (fills if entry_signals or exit_deferred_to_llm else [])
                            if f.get('action') == 'ENTRY_REJECTED')
        print(f"  [MORNING] exits={len(exit_orders_placed)}, partial={len(partial_exits_placed)}, "
              f"fills={filled_count}, rejected={rejected_count}, "
              f"llm_reviewed={len(needs_llm) if entry_signals else 0}",
              flush=True)

        logger.info(
            "MORNING: exits=%d, partial=%d, buys=%d, tightened=%d, "
            "llm_rejected=%d, sizing_rejected=%d.",
            len(exit_orders_placed), len(partial_exits_placed), len(placed_orders),
            len(stops_tightened), len(llm_rejected), len(rejected_sizing),
        )

        day_events: list[dict] = []
        day_events.extend(exit_orders_placed)
        day_events.extend(partial_exits_placed)
        if entry_signals or exit_deferred_to_llm:
            day_events.extend(fills)
        # Merge position + candidate research for frontend display
        all_research: dict = {}
        for ticker, r in {**position_research, **candidate_research}.items():
            if r is not None:
                all_research[ticker] = r

        return {
            'cycle_type': 'MORNING',
            'regime': pending.get('regime', 'UNKNOWN'),
            'regime_confidence': pending.get('regime_confidence'),
            'exits_placed': len(exit_orders_placed),
            'partial_exits_placed': len(partial_exits_placed),
            'exits_failed': len(exit_orders_failed),
            'stops_tightened': len(stops_tightened),
            'orders_placed': len(placed_orders),
            'orders_failed': len(failed_orders),
            'llm_rejected': len(llm_rejected),
            'llm_rejected_details': llm_rejected,
            'morning_exit_details': morning_exit_details if exit_deferred_to_llm else [],
            'decisions': morning_decisions,
            'rejected_by_sizing': len(rejected_sizing),
            'day_events': day_events,
            # Research & quant for frontend display
            'research': all_research,
            'quant_context': {
                'positions': pending.get('quant_positions', {}),
                'candidates': pending.get('quant_candidates', {}),
            },
            'entry_signals': entry_signals,
            'exit_signals': exit_signals,
            'playbook_reads': morning_playbook_reads,
            # Research triage metadata
            'news_checked': len(all_morning_tickers),
            'news_with_articles': len(triggered_positions) + len(triggered_candidates),
            'triggered_positions': len(triggered_positions),
            'triggered_candidates': len(triggered_candidates),
            'prompt': morning_prompt,
            'pm_token_usage': self.get_token_usage(),
        }

    # ------------------------------------------------------------------
    # MORNING helpers
    # ------------------------------------------------------------------

    def _save_morning_cycle_log(
        self,
        position_research: dict,
        candidate_research: dict,
        exit_signals: list[dict],
        entry_signals: list[dict],
        exit_orders_placed: list[dict],
        placed_orders: list[dict],
        llm_rejected: list[dict],
        portfolio_snapshot: dict,
        playbook_reads: list[str] | None = None,
    ) -> None:
        """Save structured MORNING cycle log for later review."""
        from pathlib import Path

        log_dir = Path("state/logs/morning")
        log_dir.mkdir(parents=True, exist_ok=True)

        today = _now_et_iso()[:10]
        log_data = {
            "cycle_type": "MORNING",
            "date": today,
            "generated_at": _now_et_iso(),
            "portfolio_snapshot": portfolio_snapshot,
            "research": {
                "positions": position_research,
                "candidates": candidate_research,
            },
            "pending_signals": {
                "exit_signals": exit_signals,
                "entry_signals": entry_signals,
            },
            "execution": {
                "exits_placed": exit_orders_placed,
                "entries_placed": placed_orders,
                "llm_rejected": llm_rejected,
            },
            "playbook_reads": playbook_reads or [],
        }

        log_path = log_dir / f"{today}.json"
        try:
            with open(log_path, "w") as f:
                json.dump(log_data, f, indent=2, default=str)
            logger.info("MORNING cycle log saved: %s", log_path)
        except Exception as exc:
            logger.warning("MORNING cycle log save failed: %s", exc)

    def _build_morning_prompt(
        self,
        entry_signals: list[dict],
        regime: str,
        portfolio: dict,
        latest_quotes: dict | None = None,
        exit_deferred: list[dict] | None = None,
        quant_context: dict | None = None,
        regime_confidence: float | None = None,
    ) -> str:
        """Build the MORNING LLM re-judgment prompt."""
        from agents.prompts.v1_0 import (
            MORNING_ENTRY_FLAGS, MORNING_EXIT_REVIEW, MORNING_INSTRUCTIONS,
        )
        from tools.journal.playbook import set_allowed_chapters
        set_allowed_chapters('morning')
        now_et = _now_et_iso(getattr(self, '_sim_date', None), cycle='MORNING')
        quotes = latest_quotes or {}

        candidates_ctx: list[dict] = []
        candidates_text_lines: list[str] = []
        for sig in entry_signals:
            tick = sig['ticker']
            entry_price = sig.get('entry_price') or 0.0

            quote = quotes.get(tick)
            if quote:
                live_price = quote.get('ask_price') or quote.get('mid_price') or 0.0
                gap_pct = round((live_price - entry_price) / entry_price * 100, 2) if entry_price else None
            else:
                live_price = None
                gap_pct = None

            # MR: compute premarket R:R so LLM can see gap impact on risk/reward
            strategy = sig.get('strategy', 'MOMENTUM')
            stop = sig.get('stop_loss_price', 0)
            tp = sig.get('take_profit_price', 0)
            eod_rr = None
            premarket_rr = None
            if strategy == 'MEAN_REVERSION' and stop and tp and entry_price > stop:
                eod_risk = entry_price - stop
                eod_rr = round((tp - entry_price) / eod_risk, 2) if eod_risk > 0 else None
                if live_price and live_price > stop:
                    pm_risk = live_price - stop
                    premarket_rr = round((tp - live_price) / pm_risk, 2) if pm_risk > 0 else None

            candidates_ctx.append({
                'ticker': tick,
                'strategy': strategy,
                'eod_reason': sig.get('reason', ''),
                'entry_price_eod': entry_price,
                'premarket_price': live_price,
                'gap_pct_vs_eod': gap_pct,
                'stop_loss': stop,
                'take_profit': tp,
                'atr': sig.get('atr'),
                'eod_rr': eod_rr,
                'premarket_rr': premarket_rr,
                'overnight_summary': sig.get('overnight_summary', ''),
                'time_sensitive_facts': sig.get('time_sensitive_facts', []),
                'negative_catalyst': sig.get('negative_catalyst', False),
            })

            # Build readable text
            strat_short = 'MR' if strategy == 'MEAN_REVERSION' else 'MOM'
            atr = sig.get('atr', 0)
            lines = [f"--- {tick} ({strat_short}) ---"]
            lines.append(
                f"  EOD price: ${entry_price:.2f}  premarket: "
                f"${live_price:.2f} ({gap_pct:+.1f}%)" if live_price else
                f"  EOD price: ${entry_price:.2f}  premarket: n/a"
            )
            lines.append(f"  stop={stop:.2f}  tp={tp:.2f}  atr={atr:.2f}")
            if eod_rr is not None:
                rr_line = f"  R:R  EOD={eod_rr:.2f}"
                if premarket_rr is not None:
                    rr_line += f" → premarket={premarket_rr:.2f}"
                lines.append(rr_line)
            conv = sig.get('conviction', '')
            conv_str = f" [{conv}]" if conv else ""
            lines.append(f"  EOD decision: LONG{conv_str}")
            eod_for = sig.get('for', '') or sig.get('reason', '')
            if eod_for:
                lines.append(f"    For: {eod_for}")
            eod_against = sig.get('against', '')
            if eod_against:
                lines.append(f"    Against: {eod_against}")
            overnight = sig.get('overnight_summary', '')
            if overnight:
                lines.append(f"  Overnight: {overnight}")
            facts = sig.get('time_sensitive_facts', [])
            if facts:
                lines.append(f"  Facts: {'; '.join(facts)}")
            neg = sig.get('negative_catalyst', False)
            if neg:
                lines.append("  ** NEGATIVE CATALYST FLAGGED **")
            candidates_text_lines.append("\n".join(lines))

        exit_review_ctx = []
        exit_review_text_lines: list[str] = []
        for s in (exit_deferred or []):
            tick = s['ticker']
            q = quotes.get(tick)
            live_price = (q.get('ask_price') or q.get('mid_price') or None) if q else None
            pos = self.portfolio_state.positions.get(tick)
            entry_price = pos.avg_entry_price if pos else 0.0
            unrealized_pnl_pct = (
                round((live_price - entry_price) / entry_price * 100, 2)
                if pos and live_price and entry_price > 0 else None
            )
            exit_review_ctx.append({
                'ticker': tick,
                'eod_action': s.get('action', 'HOLD'),
                'eod_reason': s.get('reason', ''),
                'current_price': live_price,
                'entry_price': round(entry_price, 2) if entry_price else None,
                'stop_loss_price': round(pos.stop_loss_price, 2) if pos else None,
                'qty': pos.qty if pos else None,
                'holding_days': (_date.fromisoformat(getattr(self, '_sim_date', None) or str(_date.today())) - _date.fromisoformat(pos.entry_date)).days if pos and pos.entry_date else None,
                'unrealized_pnl_pct': unrealized_pnl_pct,
                'overnight_summary': s.get('overnight_summary', ''),
                'time_sensitive_facts': s.get('time_sensitive_facts', []),
                'risk_flag': s.get('risk_flag'),
                'positive_catalyst': s.get('positive_catalyst', False),
            })

            # Build readable text
            lines = [f"--- {tick} (EOD: {s.get('action', 'HOLD')}) ---"]
            price_str = f"${live_price:.2f}" if live_price else "n/a"
            entry_str = f"${entry_price:.2f}" if entry_price else "n/a"
            stop_str = f"${pos.stop_loss_price:.2f}" if pos else "n/a"
            lines.append(f"  Current: {price_str}  entry: {entry_str}  stop: {stop_str}")
            if pos:
                lines.append(f"  Qty: {pos.qty}  holding: {getattr(pos, 'holding_days', '?')}d")
            if unrealized_pnl_pct is not None:
                lines.append(f"  Unrealized P&L: {unrealized_pnl_pct:+.1f}%")
            eod_for = s.get('for', '') or s.get('reason', '')
            eod_against = s.get('against', '')
            conv = s.get('conviction', '')
            conv_str = f" [{conv}]" if conv else ""
            lines.append(f"  EOD decision: {s.get('action', 'HOLD')}{conv_str}")
            if eod_for:
                lines.append(f"    For: {eod_for}")
            if eod_against:
                lines.append(f"    Against: {eod_against}")
            overnight = s.get('overnight_summary', '')
            if overnight:
                lines.append(f"  Overnight: {overnight}")
            facts = s.get('time_sensitive_facts', [])
            if facts:
                lines.append(f"  Facts: {'; '.join(facts)}")
            risk_flag = s.get('risk_flag')
            if risk_flag:
                lines.append(f"  Research risk: {risk_flag}")
            note = getattr(pos, 'note', '') if pos else ''
            if note:
                lines.append(f"  Position note: {note}")
            exit_review_text_lines.append("\n".join(lines))

        # Build quant context section if available
        quant_section = ""
        if quant_context:
            quant_lines: list[str] = ["=== QUANT CONTEXT (EOD vs Morning) ==="]
            for tick, qctx in quant_context.items():
                eod = qctx.get('eod')
                morn = qctx.get('morning')
                quant_lines.append(f"\n--- {tick} ---")
                if eod and morn:
                    # Side-by-side comparison of key metrics
                    quant_lines.append(
                        f"           {'EOD':>10}  {'Morning':>10}  {'Change':>10}"
                    )
                    _qm = _format_quant_metric
                    quant_lines.append(_qm('Price', eod, morn, 'current_price', fmt='$.2f'))
                    quant_lines.append(_qm('RSI', eod, morn, 'rsi', fmt='.1f'))
                    quant_lines.append(_qm('R:R', eod, morn, 'rr_ratio', fmt='.2f'))
                    quant_lines.append(_qm('vs 20MA', eod, morn, 'price_vs_20ma_pct', fmt='+.1%'))
                    quant_lines.append(_qm('Bollinger', eod, morn, 'bollinger_position', fmt='+.2f'))
                    quant_lines.append(_qm('ATR', eod, morn, 'atr', fmt='.2f'))
                    macd_eod = eod.get('macd_crossover', '-')
                    macd_morn = morn.get('macd_crossover', '-')
                    quant_lines.append(f"  {'MACD':<10} {str(macd_eod):>10}  {str(macd_morn):>10}")
                elif eod:
                    quant_lines.append(f"  EOD: price=${eod.get('current_price', 0):.2f}  "
                                       f"RSI={eod.get('rsi', 0):.1f}  R:R={eod.get('rr_ratio', 0):.2f}  "
                                       f"vs_20ma={eod.get('price_vs_20ma_pct', 0):+.1%}")
                    quant_lines.append("  Morning: (not available)")
                else:
                    quant_lines.append("  (no quant data)")
            quant_section = "\n".join(quant_lines) + "\n\n"

        # PM notes (ablation-aware)
        from tools.journal.pm_notes import load_pm_notes, format_pm_notes_for_prompt
        notes_enabled = getattr(getattr(self, 'settings', None), 'enable_pm_notes', True)
        pm_notes = load_pm_notes() if notes_enabled else {}

        sections = []

        sections.append(
            f"<cycle_header>\n"
            f"MORNING re-judgment. Current time (ET): {now_et}\n"
            f"You are reviewing flagged items that need re-judgment before market open.\n"
            f"Regime: {regime}"
            + (f" ({regime_confidence:.0%})" if regime_confidence is not None else "")
            + f"  |  Portfolio: ${portfolio.get('portfolio_value', 0):,.0f}, "
            f"{portfolio.get('position_count', 0)} positions\n"
            f"</cycle_header>"
        )

        if pm_notes:
            sim_date = getattr(self, '_sim_date', None) or ''
            notes_text = format_pm_notes_for_prompt(pm_notes, as_of=sim_date)
            sections.append(
                f"<pm_notes>\n"
                f"Your notes from previous cycles (use notes param on submit to modify):\n"
                f"{notes_text}\n"
                f"</pm_notes>"
            )

        if candidates_ctx:
            sections.append(
                f"<entry_candidates>\n"
                + "\n".join(candidates_text_lines) + "\n\n"
                + MORNING_ENTRY_FLAGS + "\n"
                f"</entry_candidates>"
            )

        if exit_deferred:
            sections.append(
                f"<exit_review>\n"
                + MORNING_EXIT_REVIEW + "\n\n"
                + "\n".join(exit_review_text_lines) + "\n"
                f"</exit_review>"
            )

        if quant_section:
            sections.append(
                f"<quant_context>\n{quant_section.strip()}\n</quant_context>"
            )

        sections.append(MORNING_INSTRUCTIONS)

        action_notes = []
        if candidates_ctx:
            action_notes.append("For each entry candidate decide CONFIRM / REJECT / ADJUST.")
        if exit_deferred:
            action_notes.append(
                "For each exit review item decide EXIT or HOLD.\n"
                "Consider both the EOD reasoning and overnight research."
            )
        if action_notes:
            sections.append(
                f"<action_required>\n" + "\n".join(action_notes) + "\n</action_required>"
            )

        return "\n\n".join(sections)
