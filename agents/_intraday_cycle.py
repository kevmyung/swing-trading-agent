"""
agents/_intraday_cycle.py — INTRADAY cycle implementation.

Mixin class providing _run_intraday_cycle and its helpers.
Works for both live trading (AlpacaBroker + LiveProvider) and
backtesting (MockBroker + FixtureProvider) via dependency injection.
"""

from __future__ import annotations

import json
import logging
from datetime import date as _date
from typing import Any

from agents._formatting import _now_et_iso, _extract_playbook_reads
from agents._morning_cycle import _record_trade
from tools.journal.decision_log import consume_cycle_decisions, try_rescue_from_text

logger = logging.getLogger(__name__)


class IntradayCycleMixin:
    """Methods for the INTRADAY cycle, mixed into PortfolioAgent."""

    def _run_intraday_cycle(self) -> dict[str, Any]:
        """
        INTRADAY anomaly-detection cycle (10:30 ET).

        Checks all positions but only escalates anomalous ones to LLM.
        Normal positions are auto-HOLD — no LLM call, no action bias.

        Steps:
          0. fill unfilled LIMIT/STOP orders (simulation only — no-op for live)
          1. portfolio-sync              (system)
          2. drawdown check              (system)
          3. snapshot + intraday context  (system — snapshots + ATR from daily bars)
          4. auto trailing stop           (system — ATR-based, before anomaly check)
          5. anomaly detection            (system — threshold filter)
          6. news check                   (Research Agent — flagged tickers only)
          7. LLM review                   (only flagged positions, with flag reasons)
          8. execute decisions + sync     (system)
        """
        from tools.risk.drawdown import check_drawdown
        from agents.quant_engine import QuantEngine
        from state.portfolio_state import Position

        s = self.settings
        sim_date = getattr(self, '_sim_date', None)
        today_str = sim_date or str(_date.today())
        prompt = ''

        intraday_events: list[dict] = []

        # ── Step 0: Fill unfilled LIMIT/STOP orders (simulation only) ──────
        if getattr(self._get_broker(), 'pending_orders', None):
            limit_fills = self._get_broker().fill_pending(cutoff_utc='15:30')
            for f in limit_fills:
                intraday_events.append(f)
                if f.get('action') == 'ENTRY_FILLED':
                    ticker = f['ticker']
                    existing = self.portfolio_state.positions.get(ticker)
                    if existing:
                        # AUTO_ADD fill: sync from broker (weighted avg price)
                        bp = self._get_broker().positions.get(ticker)
                        if bp:
                            existing.qty = bp.qty
                            existing.avg_entry_price = bp.avg_entry_price
                            existing.current_price = bp.current_price
                            existing.entry_qty = bp.qty
                            existing.scaled_entry = False  # AUTO_ADD filled → full size
                            existing.scaled_up = True      # signal to show in next EOD position table
                    else:
                        self.portfolio_state.positions[ticker] = Position(
                            symbol=ticker, qty=f['shares'],
                            avg_entry_price=f['fill_price'], current_price=f['fill_price'],
                            stop_loss_price=f.get('stop_loss', 0.0),
                            signal_price=f.get('signal_price', 0.0),
                            entry_date=today_str, strategy=f.get('strategy', 'MOMENTUM'),
                            entry_qty=f['shares'],
                        )
                    print(f"  [INTRADAY] LIMIT FILL: {f['ticker']} @ ${f['fill_price']:.2f} "
                          f"x{f['shares']}", flush=True)
                elif f.get('action') == 'ENTRY_REJECTED':
                    print(f"  [INTRADAY] LIMIT REJECTED: {f['ticker']} — {f.get('reason', '')}",
                          flush=True)

        # Report remaining unfilled orders
        if hasattr(self._get_broker(), 'pending_orders') and self._get_broker().pending_orders:
            for o in self._get_broker().pending_orders:
                lp = f" limit=${o.limit_price:.2f}" if getattr(o, 'limit_price', None) else ''
                print(f"  [INTRADAY] unfilled: {o.ticker} {o.entry_type}{lp}", flush=True)

        # ── Step 0b: Mid-day stop-loss check ─────────────────────────────
        # Check stops BEFORE LLM review so stopped-out positions are removed.
        if hasattr(self._get_broker(), 'check_stops_midday'):
            midday_stops = self._get_broker().check_stops_midday(cutoff_utc='15:30')
            for evt in midday_stops:
                intraday_events.append(evt)
                ticker = evt['ticker']
                pos = self.portfolio_state.positions.get(ticker)
                if pos:
                    _record_trade(self.portfolio_state, pos, evt, today_str)
                    del self.portfolio_state.positions[ticker]
                print(f"  [INTRADAY] STOP HIT: {ticker} @ ${evt['exit_price']:.2f} "
                      f"P&L ${evt['pnl']:.0f}", flush=True)

        # ── Step 1: Sync portfolio ───────────────────────────────────────
        portfolio = self._get_broker().sync(sim_date)
        if portfolio.get('error'):
            logger.error("INTRADAY: portfolio sync failed: %s", portfolio['error'])
            return {"cycle_type": "INTRADAY", "error": "portfolio_sync_failed",
                    "day_events": intraday_events}

        # Propagate broker-synced values back to agent state
        self.portfolio_state.cash = portfolio['cash']
        self.portfolio_state.portfolio_value = portfolio['portfolio_value']
        self.portfolio_state.peak_value = max(
            self.portfolio_state.peak_value, portfolio['peak_value']
        )
        # Sync positions from Alpaca into agent state
        from state.portfolio_state import Position
        positions_full = portfolio.get('positions_full', {})
        if positions_full:
            synced_syms = set(positions_full.keys())
            for sym, pos_data in positions_full.items():
                if sym not in self.portfolio_state.positions:
                    self.portfolio_state.positions[sym] = Position.from_dict(pos_data)
                else:
                    local = self.portfolio_state.positions[sym]
                    local.current_price = pos_data.get('current_price', local.current_price)
                    local.unrealized_pnl = pos_data.get('unrealized_pnl', local.unrealized_pnl)
                    local.qty = pos_data.get('qty', local.qty)
            for sym in list(self.portfolio_state.positions.keys()):
                if sym not in synced_syms:
                    del self.portfolio_state.positions[sym]

        # ── Step 2: Drawdown check ───────────────────────────────────────
        dd = check_drawdown(
            current_value=portfolio['portfolio_value'],
            peak_value=portfolio['peak_value'],
            max_drawdown_pct=s.max_drawdown_pct,
        )

        held_tickers = [p['symbol'] for p in portfolio.get('positions', [])]
        if not held_tickers:
            logger.info("INTRADAY: no open positions — nothing to manage.")
            return {"cycle_type": "INTRADAY", "decisions": [], "positions_managed": 0,
                    "drawdown_status": dd['status'], "day_events": intraday_events}

        # ── Step 3: Snapshots + intraday context ─────────────────────────
        snapshots = self._get_provider().get_snapshots(held_tickers + ['SPY'])
        bars = self._get_provider().get_bars(held_tickers + ['SPY'], end=today_str)
        quant = QuantEngine(settings=s)
        technicals = quant.generate_signals(held_tickers, cycle_type='INTRADAY', bars=bars)
        technicals.pop('_bars', None)

        atr_map: dict[str, float] = {}
        for signal in technicals.get('signals', []):
            atr_map[signal.get('ticker', '')] = signal.get('atr', 0.0)

        spy_snap = snapshots.get('SPY', {})
        spy_today_open = spy_snap.get('today_open', 0.0)
        spy_latest = spy_snap.get('latest_price', 0.0)
        spy_intraday_return = (
            (spy_latest - spy_today_open) / spy_today_open
            if spy_today_open > 0 and spy_latest > 0 else 0.0
        )
        market_shock = spy_intraday_return < -s.intraday_market_shock_pct

        # Carry forward EOD quant context for richer position info
        pending = self.portfolio_state.pending_signals or {}
        eod_positions_ctx = pending.get('quant_positions', {})

        intraday_ctx: dict[str, dict] = {}
        for ticker in held_tickers:
            pos = self.portfolio_state.positions.get(ticker)
            snap = snapshots.get(ticker, {})
            atr = atr_map.get(ticker, 0.0)
            if not pos:
                continue

            latest_price = snap.get('latest_price', pos.current_price)
            today_open = snap.get('today_open', 0.0)
            today_high = snap.get('today_high', 0.0)
            prev_volume = snap.get('prev_volume', 0.0)
            today_volume = snap.get('today_volume', 0.0)

            intraday_return = (
                (latest_price - today_open) / today_open
                if today_open > 0 else 0.0
            )
            intraday_drawdown = (
                (latest_price - today_high) / today_high
                if today_high > 0 else 0.0
            )
            stop_distance = latest_price - pos.stop_loss_price if pos.stop_loss_price > 0 else 999.0
            stop_proximity_atr = stop_distance / atr if atr > 0 else 999.0
            unrealized_pnl_atr = (
                (latest_price - pos.avg_entry_price) / atr if atr > 0 else 0.0
            )
            volume_ratio = today_volume / prev_volume if prev_volume > 0 else 1.0
            vs_spy = intraday_return - spy_intraday_return

            ctx_entry = {
                'latest_price': round(latest_price, 2),
                'entry_price': round(pos.avg_entry_price, 2),
                'stop_loss_price': round(pos.stop_loss_price, 2),
                'qty': pos.qty,
                'atr': round(atr, 2),
                'today_open': round(today_open, 2),
                'today_high': round(today_high, 2),
                'today_low': round(snap.get('today_low', 0.0), 2),
                'intraday_return_pct': round(intraday_return * 100, 2),
                'intraday_drawdown_pct': round(intraday_drawdown * 100, 2),
                'stop_proximity_atr': round(stop_proximity_atr, 2),
                'unrealized_pnl_atr': round(unrealized_pnl_atr, 2),
                'volume_ratio': round(volume_ratio, 1),
                'vs_spy_pct': round(vs_spy * 100, 2),
                'holding_days': (_date.fromisoformat(today_str) - _date.fromisoformat(pos.entry_date)).days if pos.entry_date else 0,
            }
            # Merge EOD quant context (strategy, technicals)
            eod_ctx = eod_positions_ctx.get(ticker, {})
            if eod_ctx:
                ctx_entry['strategy'] = eod_ctx.get('strategy', '')
                ctx_entry['partial_exit_count'] = getattr(pos, 'partial_exit_count', 0)
                for k in ('momentum_zscore', 'rsi', 'macd_crossover',
                          'price_vs_20ma_pct', 'return_5d', 'return_5d_vs_spy',
                          'risk_reward_remaining', 'weekly',
                          'research_summary', 'research_risk_level',
                          'deterioration_tracker'):
                    if k in eod_ctx:
                        ctx_entry[k] = eod_ctx[k]
            intraday_ctx[ticker] = ctx_entry

        # ── Step 4: Auto trailing stop (chandelier + breakeven) ────────────
        # Chandelier exit: highest_close - N×ATR. HWM is updated at EOD
        # using closing prices only — intraday uses the prior close HWM so
        # stops don't ratchet on intraday noise. Stops only move up.
        # When PM has TIGHTENed (thesis concern), ATR multiplier narrows.
        # Skip on fresh positions (holding_days < 2).
        auto_tightened: list[dict] = []
        for ticker in held_tickers:
            atr = atr_map.get(ticker, 0.0)
            ctx = intraday_ctx.get(ticker)
            pos = self.portfolio_state.positions.get(ticker)
            if not pos or not ctx or atr <= 0:
                continue
            holding_days = ctx.get('holding_days', 0)
            if holding_days < 2:
                continue
            latest_price = ctx['latest_price']
            entry_price = pos.avg_entry_price

            # Use existing HWM (closing-price based, updated in EOD cycle).
            # Do NOT update highest_close here — intraday spikes would
            # ratchet the stop up based on noise, then a normal retracement
            # leaves the stop dangerously tight relative to the close.
            hwm = pos.highest_close if pos.highest_close > 0 else latest_price

            # ATR multiplier: strategy × conviction × tighten state
            #   MOM: high=2.0, medium=1.75, low=1.5, TIGHTEN=1.5
            #   MR:  normal=2.0, TIGHTEN=1.5 (conviction doesn't affect — early medium is normal)
            if pos.tighten_active:
                tighten_mult = 1.5
            elif pos.strategy == 'MOMENTUM' and pos.last_conviction:
                _conv_mult = {'high': s.atr_stop_multiplier, 'medium': 1.75, 'low': 1.5}
                tighten_mult = _conv_mult.get(pos.last_conviction, s.atr_stop_multiplier)
            else:
                tighten_mult = s.atr_stop_multiplier

            # Candidate 1: Chandelier trailing (highest close - N×ATR)
            stop_candidates = [hwm - tighten_mult * atr]

            # Candidate 2: breakeven lock after 8% unrealized gain
            pnl_pct = (latest_price - entry_price) / entry_price if entry_price > 0 else 0.0
            if pnl_pct >= 0.08:
                stop_candidates.append(entry_price)

            trailing_stop = round(max(stop_candidates), 2)
            # Never set stop above current price — would trigger immediate stop-out
            if trailing_stop >= latest_price:
                trailing_stop = pos.stop_loss_price  # keep existing
            if trailing_stop > pos.stop_loss_price:
                old_stop = pos.stop_loss_price
                bracket_id = getattr(pos, 'bracket_order_id', None)
                mod = self._get_broker().update_stop(
                    ticker, trailing_stop, bracket_order_id=bracket_id,
                )
                pos.stop_loss_price = trailing_stop
                if mod.get('modified'):
                    self.portfolio_state.save()
                else:
                    logger.warning("INTRADAY: %s auto trailing stop — broker update failed: %s "
                                   "(local state updated, may diverge)",
                                   ticker, mod.get('error'))
                auto_tightened.append({
                    'ticker': ticker, 'old_stop': old_stop, 'new_stop': trailing_stop,
                    'alpaca_updated': mod.get('modified', False),
                })
                ctx['stop_loss_price'] = trailing_stop
                ctx['stop_proximity_atr'] = round(
                    (latest_price - trailing_stop) / atr, 2) if atr > 0 else 999.0
                logger.info("INTRADAY: %s auto trailing stop %.2f → %.2f",
                            ticker, old_stop, trailing_stop)

        if auto_tightened:
            for t in auto_tightened:
                print(f"  [INTRADAY] trailing stop: {t['ticker']} "
                      f"${t['old_stop']:.2f} → ${t['new_stop']:.2f}", flush=True)

        # ── Step 5: Anomaly detection ────────────────────────────────────
        auto_tightened_tickers = {t['ticker'] for t in auto_tightened}
        flagged: dict[str, list[str]] = {}
        for ticker, ctx in intraday_ctx.items():
            flags: list[str] = []
            atr = ctx.get('atr', 0.0)
            latest_price = ctx.get('latest_price', 0.0)

            # Skip STOP_IMMINENT for positions the system just tightened —
            # flagging our own tightening is a self-trigger, not a market signal.
            if ticker not in auto_tightened_tickers and ctx['stop_proximity_atr'] < s.intraday_stop_proximity_atr:
                flags.append(
                    f"STOP_IMMINENT: stop {ctx['stop_proximity_atr']:.1f} ATR away "
                    f"(threshold: {s.intraday_stop_proximity_atr})"
                )

            if ctx['unrealized_pnl_atr'] > s.intraday_profit_review_atr:
                flags.append(
                    f"PROFIT_REVIEW: unrealized PnL {ctx['unrealized_pnl_atr']:.1f} ATR "
                    f"(threshold: {s.intraday_profit_review_atr})"
                )

            if atr > 0 and latest_price > 0:
                drop_in_atr = abs(ctx['intraday_return_pct'] / 100 * latest_price / atr)
                if ctx['intraday_return_pct'] < 0 and drop_in_atr > s.intraday_sharp_drop_atr:
                    flags.append(
                        f"SHARP_DROP: intraday {ctx['intraday_return_pct']:+.1f}% "
                        f"({drop_in_atr:.1f} ATR, threshold: {s.intraday_sharp_drop_atr})"
                    )

            if ctx['volume_ratio'] > s.intraday_volume_ratio:
                flags.append(
                    f"UNUSUAL_VOLUME: {ctx['volume_ratio']:.1f}× prev day "
                    f"(threshold: {s.intraday_volume_ratio}×)"
                )

            if market_shock:
                flags.append(
                    f"MARKET_SHOCK: SPY {spy_intraday_return:+.1%} intraday "
                    f"(threshold: -{s.intraday_market_shock_pct:.0%})"
                )

            if flags:
                flagged[ticker] = flags

        logger.info(
            "INTRADAY: anomaly scan — %d/%d positions flagged. %s",
            len(flagged), len(held_tickers),
            ", ".join(f"{t}({len(f)})" for t, f in flagged.items()) if flagged else "all normal",
        )

        # ── Step 6: News check (all held positions) ────────────────────
        # News is an independent flag source — negative news alone should
        # trigger LLM review even if technical anomalies are absent.
        sentiment: dict = {}
        held_set = set(held_tickers)
        try:
            sentiment = self._get_provider().get_news(held_tickers, hours_back=6)
            for tick, news_data in sentiment.items():
                if tick not in held_set:
                    continue
                if isinstance(news_data, dict):
                    score = news_data.get('composite_sentiment', 0.0)
                    if score < s.intraday_news_sentiment_threshold:
                        if tick not in flagged:
                            flagged[tick] = []
                        flagged[tick].append(
                            f"NEWS_ALERT: sentiment {score:.2f} "
                            f"(threshold: {s.intraday_news_sentiment_threshold})"
                        )
        except Exception as exc:
            logger.warning("INTRADAY: news fetch failed: %s", exc)

        # ── No flags → skip LLM, auto-HOLD all ──────────────────────
        if not flagged:
            print(f"  [INTRADAY] {len(held_tickers)} positions checked, "
                  f"no anomalies (auto={len(auto_tightened)} trailing stops)", flush=True)
            logger.info("INTRADAY: no anomalies detected — all positions auto-HOLD.")
            return {
                'cycle_type': 'INTRADAY',
                'decisions': [{'ticker': t, 'decision': 'HOLD', 'reason': 'no anomaly'}
                              for t in held_tickers],
                'exits_placed': 0,
                'partial_exits_placed': 0,
                'exits_failed': 0,
                'stops_tightened_llm': 0,
                'stops_tightened_auto': len(auto_tightened),
                'positions_managed': len(held_tickers),
                'positions_flagged': 0,
                'llm_skipped': True,
                'drawdown_status': dd['status'],
                'day_events': intraday_events,
                'market_shock': market_shock,
                'spy_intraday_return': round(spy_intraday_return * 100, 2),
                'auto_tightened_details': auto_tightened,
                'flagged_details': {},
                'quant_context': {'positions': intraday_ctx},
                'playbook_reads': [],
                'prompt': '',
            }

        # ── Step 7: LLM review (flagged positions only) ─────────────────
        flagged_ctx: dict[str, dict] = {}
        for ticker in flagged:
            ctx = intraday_ctx.get(ticker, {})
            flagged_ctx[ticker] = {
                **ctx,
                'flag_reasons': flagged[ticker],
                'news': sentiment.get(ticker) if isinstance(sentiment.get(ticker), dict) else None,
            }

        prompt = self._build_intraday_prompt(
            portfolio, dd, flagged_ctx, auto_tightened,
            unflagged_tickers=[t for t in held_tickers if t not in flagged],
            spy_intraday_return=spy_intraday_return,
            regime=pending.get('regime'),
        )
        print(f"  [INTRADAY] LLM review: {len(flagged)}/{len(held_tickers)} flagged — "
              f"{', '.join(f'{t}({len(f)})' for t, f in flagged.items())}", flush=True)

        self._swap_submit_tool('INTRADAY')
        msg_idx_before_intraday = len(getattr(self.agent, 'messages', []))
        llm_text = self.run(prompt)
        try_rescue_from_text(llm_text)
        intraday_decisions = consume_cycle_decisions()
        if not intraday_decisions:
            print("  [INTRADAY] WARNING: no decisions from LLM", flush=True)
            logger.error("INTRADAY: no decisions submitted via submit_intraday_decisions tool.")
            return {"cycle_type": "INTRADAY", "error": "no_decisions_submitted",
                    "day_events": intraday_events}

        # ── Step 8: Execute LLM decisions ────────────────────────────────
        # Build intraday price map for realistic exit fill prices
        intraday_prices = {t: ctx.get('latest_price') for t, ctx in intraday_ctx.items()}
        exits_placed: list[dict] = []
        exits_failed: list[dict] = []
        partial_exits_placed: list[dict] = []
        stops_tightened: list[dict] = []

        for dec in intraday_decisions:
            ticker = dec.get('ticker', '')
            decision = (dec.get('action') or dec.get('decision') or 'HOLD').upper()

            # Save conviction and clear tighten if conviction restored to high
            conviction = dec.get('conviction', '')
            pos_for_conv = self.portfolio_state.positions.get(ticker)
            if pos_for_conv and conviction:
                pos_for_conv.last_conviction = conviction
                if pos_for_conv.tighten_active and conviction == 'high' and decision != 'TIGHTEN':
                    pos_for_conv.tighten_active = False
                    logger.info("INTRADAY: %s tighten_active cleared (conviction=high)", ticker)

            if decision == 'EXIT':
                pos = self.portfolio_state.positions.get(ticker)
                if not pos:
                    continue
                order_result = self._get_broker().execute_exit(
                    ticker, qty=pos.qty, fill_price=intraday_prices.get(ticker))
                if order_result is None or order_result.get('error'):
                    err = order_result.get('error') if order_result else 'no result'
                    exits_failed.append({'symbol': ticker, 'error': err})
                    logger.error("INTRADAY: exit failed for %s — %s", ticker, err)
                else:
                    exits_placed.append(order_result)
                    intraday_events.append(order_result)
                    _record_trade(self.portfolio_state, pos, order_result, today_str)
                    if ticker in self.portfolio_state.positions:
                        del self.portfolio_state.positions[ticker]
                    logger.info("INTRADAY: exit placed for %s qty=%d — %s",
                                ticker, pos.qty, dec.get('reason', ''))
                    print(f"  [INTRADAY] {ticker}: EXIT — {dec.get('reason', '')[:60]}",
                          flush=True)

            elif decision == 'PARTIAL_EXIT':
                pos = self.portfolio_state.positions.get(ticker)
                if not pos:
                    continue
                # Already had a partial exit — escalate to full EXIT
                if pos.partial_exit_count >= 1:
                    decision = 'EXIT'
                    order_result = self._get_broker().execute_exit(
                        ticker, qty=pos.qty, fill_price=intraday_prices.get(ticker))
                    if order_result is None or order_result.get('error'):
                        err = order_result.get('error') if order_result else 'no result'
                        exits_failed.append({'symbol': ticker, 'error': err})
                    else:
                        exits_placed.append(order_result)
                        intraday_events.append(order_result)
                        _record_trade(self.portfolio_state, pos, order_result, today_str)
                        if ticker in self.portfolio_state.positions:
                            del self.portfolio_state.positions[ticker]
                        logger.info("INTRADAY: %s PARTIAL_EXIT → EXIT (already %d partials)",
                                    ticker, pos.partial_exit_count)
                        print(f"  [INTRADAY] {ticker}: PARTIAL→EXIT (max partials reached)", flush=True)
                else:
                    # 1st partial: sell half. 2nd partial: sell all remaining.
                    if pos.partial_exit_count >= 1:
                        sell_qty = pos.qty
                    else:
                        sell_qty = max(1, pos.qty // 2)
                    order_result = self._get_broker().execute_exit(
                        ticker, qty=sell_qty, fill_price=intraday_prices.get(ticker))
                    if order_result is None or order_result.get('error'):
                        err = order_result.get('error') if order_result else 'no result'
                        exits_failed.append({'symbol': ticker, 'error': err})
                    else:
                        partial_exits_placed.append({**order_result, 'exit_pct': 0.5})
                        intraday_events.append(order_result)
                        if sell_qty >= pos.qty:
                            _record_trade(self.portfolio_state, pos, order_result, today_str)
                            if ticker in self.portfolio_state.positions:
                                del self.portfolio_state.positions[ticker]
                        else:
                            _record_trade(self.portfolio_state, pos, order_result, today_str)
                            if ticker in self.portfolio_state.positions and ticker in self._get_broker().positions:
                                self.portfolio_state.positions[ticker].qty = self._get_broker().positions[ticker].qty
                            pos.partial_exit_count += 1
                        logger.info("INTRADAY: partial exit for %s qty=%d (partial #%d)",
                                    ticker, sell_qty, pos.partial_exit_count if pos.partial_exit_count else 1)
                        print(f"  [INTRADAY] {ticker}: PARTIAL_EXIT qty={sell_qty}", flush=True)

            elif decision == 'TIGHTEN':
                pos = self.portfolio_state.positions.get(ticker)
                if pos:
                    pos.tighten_active = True
                    conviction = dec.get('conviction', '')
                    if conviction:
                        pos.last_conviction = conviction
                    stops_tightened.append({'ticker': ticker, 'conviction': conviction})
                    logger.info("INTRADAY: %s TIGHTEN flagged (tighten_active=True)", ticker)
                    print(f"  [INTRADAY] {ticker}: TIGHTEN flagged", flush=True)

            else:
                print(f"  [INTRADAY] {ticker}: {decision}", flush=True)

        # Final sync
        if exits_placed or partial_exits_placed:
            final_portfolio = self._get_broker().sync(sim_date)
            if not final_portfolio.get('error'):
                self.portfolio_state.cash = final_portfolio['cash']
                self.portfolio_state.portfolio_value = final_portfolio['portfolio_value']
                self.portfolio_state.peak_value = max(
                    self.portfolio_state.peak_value, final_portfolio['peak_value']
                )

        logger.info(
            "INTRADAY: flagged=%d/%d, exits=%d, partial=%d, failed=%d, "
            "tightened=%d (auto=%d, llm=%d), drawdown=%s",
            len(flagged), len(held_tickers),
            len(exits_placed), len(partial_exits_placed), len(exits_failed),
            len(stops_tightened) + len(auto_tightened),
            len(auto_tightened), len(stops_tightened), dd['status'],
        )

        # ── Step 9: Structured log ─────────────────────────────────────
        intraday_playbook_reads = _extract_playbook_reads(
            self, since_msg_idx=msg_idx_before_intraday,
        )
        self._save_intraday_cycle_log(
            flagged_ctx=flagged_ctx,
            sentiment=sentiment,
            decisions=intraday_decisions,
            exits_placed=exits_placed,
            partial_exits_placed=partial_exits_placed,
            stops_tightened=stops_tightened,
            auto_tightened=auto_tightened,
            portfolio_snapshot={'cash': portfolio['cash'], 'value': portfolio['portfolio_value']},
            playbook_reads=intraday_playbook_reads,
        )

        return {
            'cycle_type': 'INTRADAY',
            'decisions': intraday_decisions,
            'exits_placed': len(exits_placed),
            'partial_exits_placed': len(partial_exits_placed),
            'exits_failed': len(exits_failed),
            'stops_tightened_llm': len(stops_tightened),
            'stops_tightened_auto': len(auto_tightened),
            'positions_managed': len(held_tickers),
            'positions_flagged': len(flagged),
            'llm_skipped': False,
            'drawdown_status': dd['status'],
            'day_events': intraday_events,
            'market_shock': market_shock,
            'spy_intraday_return': round(spy_intraday_return * 100, 2),
            'auto_tightened_details': auto_tightened,
            'flagged_details': {t: f for t, f in flagged.items()},
            'quant_context': {'positions': intraday_ctx},
            'playbook_reads': intraday_playbook_reads,
            'prompt': prompt,
            'pm_token_usage': self.get_token_usage(),
        }

    # ------------------------------------------------------------------
    # Structured log
    # ------------------------------------------------------------------

    def _save_intraday_cycle_log(
        self,
        flagged_ctx: dict,
        sentiment: dict,
        decisions: list[dict],
        exits_placed: list[dict],
        partial_exits_placed: list[dict],
        stops_tightened: list[dict],
        auto_tightened: list[dict],
        portfolio_snapshot: dict,
        playbook_reads: list[str] | None = None,
    ) -> None:
        """Save structured INTRADAY cycle log for later review."""
        from pathlib import Path

        log_dir = Path("state/logs/intraday")
        log_dir.mkdir(parents=True, exist_ok=True)

        today = _now_et_iso()[:10]
        log_data = {
            "cycle_type": "INTRADAY",
            "date": today,
            "generated_at": _now_et_iso(),
            "portfolio_snapshot": portfolio_snapshot,
            "flagged": {
                ticker: {
                    "flag_reasons": ctx.get("flag_reasons", []),
                    "latest_price": ctx.get("latest_price"),
                    "entry_price": ctx.get("entry_price"),
                    "stop_loss_price": ctx.get("stop_loss_price"),
                    "intraday_return_pct": ctx.get("intraday_return_pct"),
                    "stop_proximity_atr": ctx.get("stop_proximity_atr"),
                    "volume_ratio": ctx.get("volume_ratio"),
                    "news": ctx.get("news"),
                }
                for ticker, ctx in flagged_ctx.items()
            },
            "sentiment": {
                t: s for t, s in sentiment.items() if isinstance(s, dict)
            } if sentiment else {},
            "decisions": decisions,
            "execution": {
                "exits_placed": exits_placed,
                "partial_exits_placed": partial_exits_placed,
                "stops_tightened_llm": stops_tightened,
                "stops_tightened_auto": auto_tightened,
            },
            "playbook_reads": playbook_reads or [],
        }

        log_path = log_dir / f"{today}.json"
        try:
            import json
            with open(log_path, "w") as f:
                json.dump(log_data, f, indent=2, default=str)
            logger.info("INTRADAY cycle log saved: %s", log_path)
        except Exception as exc:
            logger.warning("INTRADAY cycle log save failed: %s", exc)

    # ------------------------------------------------------------------
    # INTRADAY prompt builder
    # ------------------------------------------------------------------

    def _build_intraday_prompt(
        self,
        portfolio: dict,
        drawdown: dict,
        flagged_positions: dict,
        auto_tightened: list | None = None,
        unflagged_tickers: list | None = None,
        spy_intraday_return: float = 0.0,
        regime: str | None = None,
    ) -> str:
        """Build the INTRADAY prompt for flagged positions only."""
        from agents.prompts.v1_0 import INTRADAY_INSTRUCTIONS
        from tools.journal.playbook import set_allowed_chapters
        set_allowed_chapters('intraday')
        now_et = _now_et_iso(getattr(self, '_sim_date', None), cycle='INTRADAY')

        # Auto trailing stops are system-managed — no PM notification needed.
        auto_stop_note = ""

        unflagged_note = ""
        if unflagged_tickers:
            unflagged_note = (
                f"=== UNFLAGGED POSITIONS (auto-HOLD, {len(unflagged_tickers)} tickers) ===\n"
                f"{', '.join(unflagged_tickers)}\n"
                "These positions are within normal range — no action needed.\n\n"
            )

        # Format flagged positions as readable text
        flagged_lines: list[str] = []
        for ticker, ctx in flagged_positions.items():
            holding = ctx.get('holding_days', 0)
            partials = ctx.get('partial_exit_count', 0)
            scaled = ctx.get('scaled_entry', False)
            lines = [f"--- {ticker} ({ctx.get('strategy', '?')}, day {holding}) ---"]
            lines.append(
                f"  Price: ${ctx.get('latest_price', 0):.2f}  "
                f"entry: ${ctx.get('entry_price', 0):.2f}  "
                f"stop: ${ctx.get('stop_loss_price', 0):.2f}  "
                f"qty: {ctx.get('qty', 0)}"
                + (f"  [half-size]" if scaled else "")
                + (f"  partials: {partials}" if partials else "")
            )
            lines.append(
                f"  Intraday: {ctx.get('intraday_return_pct', 0):+.1f}%  "
                f"high: ${ctx.get('today_high', 0):.2f}  "
                f"low: ${ctx.get('today_low', 0):.2f}  "
                f"vs SPY: {ctx.get('vs_spy_pct', 0):+.1f}%"
            )
            lines.append(
                f"  Stop proximity: {ctx.get('stop_proximity_atr', 0):.1f} ATR  "
                f"PnL: {ctx.get('unrealized_pnl_atr', 0):+.1f} ATR  "
                f"vol: {ctx.get('volume_ratio', 1.0):.1f}×"
            )
            # Weekly context if available
            w = ctx.get('weekly')
            if w:
                lines.append(
                    f"  Weekly: stage={w.get('weinstein_stage', '?')}  "
                    f"trend={w.get('weekly_trend_score', 0):+.2f}  "
                    f"ma_bull={w.get('weekly_ma_bullish', False)}  "
                    f"support=${w.get('weekly_support', 0):.2f}"
                )
            # Flag reasons (the key info)
            for flag in ctx.get('flag_reasons', []):
                lines.append(f"  >> {flag}")
            # News if present
            news = ctx.get('news')
            if isinstance(news, dict) and news.get('composite_sentiment') is not None:
                lines.append(
                    f"  News sentiment: {news['composite_sentiment']:.2f}  "
                    f"({news.get('article_count', 0)} articles)"
                )
            flagged_lines.append("\n".join(lines))

        flagged_text = "\n".join(flagged_lines)

        from agents.prompts.v1_0 import build_playbook_chapters
        from tools.journal.pm_notes import load_pm_notes, format_pm_notes_for_prompt

        # Ablation flags
        ablation = getattr(self, 'settings', None)
        notes_enabled = getattr(ablation, 'enable_pm_notes', True)
        playbook_enabled = getattr(ablation, 'enable_playbook', True)

        pm_notes = load_pm_notes() if notes_enabled else {}

        sections = []

        sections.append(
            f"<cycle_header>\n"
            f"INTRADAY anomaly review. Current time (ET): {now_et}\n"
            f"You are reviewing ONLY positions that crossed anomaly thresholds.\n"
            f"Most positions are normal and auto-HOLD — you only see the exceptions.\n"
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

        sections.append(
            f"<portfolio_state>\n"
            f"Portfolio: ${portfolio.get('portfolio_value', 0):,.0f}, "
            f"{portfolio.get('position_count', 0)} positions\n"
            f"Drawdown: {drawdown['status']} ({drawdown['current_drawdown_pct']:.1%})\n"
            f"SPY intraday: {spy_intraday_return:+.1%}"
            + (f"  |  Regime: {regime}" if regime else "")
            + "\n</portfolio_state>"
        )

        if auto_stop_note:
            sections.append(f"<auto_trailing_stops>\n{auto_stop_note.strip()}\n</auto_trailing_stops>")

        if unflagged_note:
            sections.append(f"<unflagged_positions>\n{unflagged_note.strip()}\n</unflagged_positions>")

        sections.append(
            f"<flagged_positions count=\"{len(flagged_positions)}\">\n"
            f"Each position below was flagged by system anomaly detection.\n"
            f"The >> lines explain WHY this position needs your attention.\n\n"
            f"{flagged_text}\n"
            f"</flagged_positions>"
        )

        if playbook_enabled:
            sections.append(
                f"<playbook_chapters>\n{build_playbook_chapters('intraday')}\n</playbook_chapters>"
            )

        if playbook_enabled:
            sections.append(INTRADAY_INSTRUCTIONS)
        else:
            from agents.prompts.v1_0 import INTRADAY_INSTRUCTIONS_NO_PLAYBOOK
            sections.append(INTRADAY_INSTRUCTIONS_NO_PLAYBOOK)

        return "\n\n".join(sections)
