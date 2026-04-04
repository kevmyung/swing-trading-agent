"""
backtest/backtest.py — Unified backtest runner (cold start or snapshot resume).

Runs the full trading loop (MORNING -> INTRADAY -> EOD) each day using
fixture bar data and real LLM calls. Caches news/earnings per day.

Modes:
  Cold start (default):  empty portfolio, runs from scratch.
  Snapshot resume:       loads portfolio state from a prior run's snapshot.

Usage:
    python -m backtest.backtest --days 20 --start-date 2025-09-12
    python -m backtest.backtest --snapshot sessions/precond_.../snapshot.json --days 20
    python -m backtest.backtest --days 10 --start-date 2025-09-12 --dump-prompts
"""

from __future__ import annotations

import logging
import math
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.common import (
    SessionStoppedError,
    SimClock,
    load_json,
    run_one_day,
    session_dir,
)
from providers import MockBroker, FixtureProvider
from config.settings import get_settings
from state.agent_state import AgentState, set_state
from state.portfolio_state import Position
from store.factory import get_store

logger = logging.getLogger(__name__)

# Polygon Stocks Starter: unlimited API calls.
NEWS_SLEEP = 0.2

# Runtime log file handle — set in run()
_log_fh = None


def _log(msg: str) -> None:
    """Print to stdout (flushed) and append to session log file."""
    print(msg, flush=True)
    if _log_fh is not None:
        _log_fh.write(msg + '\n')
        _log_fh.flush()


class Backtest:
    """Unified backtest runner: cold start or snapshot resume."""

    def __init__(
        self,
        sim_days: int = 20,
        start_date: str | None = None,
        end_date: str | None = None,
        start_cash: float = 100_000.0,
        session_id: str | None = None,
        snapshot_path: str | None = None,
        dump_prompts: bool = False,
    ) -> None:
        self.start_cash = start_cash
        self.snapshot: dict | None = None
        self.dump_prompts = dump_prompts
        self.provider = FixtureProvider()

        # Load snapshot if provided
        if snapshot_path:
            self.snapshot = load_json(Path(snapshot_path))

        # Trading days from SPY fixture
        ref = 'SPY' if 'SPY' in self.provider.available_symbols else self.provider.available_symbols[0]
        spy_dates = self.provider.get_bars([ref])[ref].index.strftime('%Y-%m-%d').tolist()

        if start_date:
            start_idx = next(
                (i for i, d in enumerate(spy_dates) if d >= start_date),
                len(spy_dates) - sim_days,
            )
        elif self.snapshot:
            snap_date = self.snapshot['final_date']
            start_idx = next((i for i, d in enumerate(spy_dates) if d > snap_date), 0)
        else:
            start_idx = max(0, len(spy_dates) - sim_days - 30) + 30

        if end_date:
            end_idx = next(
                (i for i, d in enumerate(spy_dates) if d > end_date),
                len(spy_dates),
            )
            self.sim_dates = spy_dates[start_idx : end_idx]
        else:
            self.sim_dates = spy_dates[start_idx : start_idx + sim_days]
        self.sim_days = len(self.sim_dates)
        self.all_dates = spy_dates

        # Session setup
        if session_id:
            self.session_id = session_id
        elif self.snapshot:
            self.session_id = f"sim_{self.sim_dates[0]}_{self.sim_dates[-1]}"
        else:
            self.session_id = f"bt_{self.sim_dates[0]}_{self.sim_dates[-1]}"
        self.session_path = session_dir(self.session_id)
        self.cache_dir = self.session_path / "preconditioned"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.sector_map = self.provider.get_sector_map()
        self.store = get_store()

        # Prompt dump directory
        if self.dump_prompts:
            self.dump_dir = self.session_path / "prompts"
            self.dump_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # State initialization
    # ------------------------------------------------------------------

    def _init_cold(self, settings) -> tuple[AgentState, MockBroker]:
        """Initialize fresh state for cold-start backtest."""
        state_file = str(self.session_path / 'agent_state.json')
        agent_state = AgentState(state_file=state_file)
        agent_state.cash = self.start_cash
        agent_state.portfolio_value = self.start_cash
        agent_state.peak_value = self.start_cash
        agent_state.daily_start_value = self.start_cash
        agent_state.save()
        set_state(agent_state)

        broker = MockBroker(
            self.start_cash,
            slippage_bps=settings.slippage_base_bps,
            slippage_impact_coeff=settings.slippage_impact_coeff,
            min_entry_rr_ratio=settings.min_entry_rr_ratio,
            atr_stop_multiplier=settings.atr_stop_multiplier,
        )
        return agent_state, broker

    def _init_from_snapshot(self, settings) -> tuple[AgentState, MockBroker]:
        """Restore state from a prior run's snapshot."""
        state_file = str(self.session_path / 'agent_state.json')
        agent_state = AgentState(state_file=state_file)
        agent_state.restore_from_snapshot(self.snapshot)
        agent_state.save()
        set_state(agent_state)

        broker = MockBroker(
            self.snapshot['cash'],
            slippage_bps=settings.slippage_base_bps,
            slippage_impact_coeff=settings.slippage_impact_coeff,
            min_entry_rr_ratio=settings.min_entry_rr_ratio,
            atr_stop_multiplier=settings.atr_stop_multiplier,
        )
        broker.peak_value = self.snapshot['peak_value']
        for t, p in self.snapshot.get('positions', {}).items():
            broker.positions[t] = Position(
                symbol=p['symbol'], qty=p['qty'],
                avg_entry_price=p['avg_entry_price'],
                current_price=p['current_price'],
                stop_loss_price=p.get('stop_loss_price', 0.0),
                unrealized_pnl=p.get('unrealized_pnl', 0.0),
                entry_date=p.get('entry_date', ''),
                strategy=p.get('strategy', 'MOMENTUM'),
            )
        return agent_state, broker

    def _init_resume(self, settings) -> tuple[AgentState, MockBroker]:
        """Resume from a previously interrupted run."""
        snapshot_path = self.session_path / 'snapshot.json'
        snapshot = load_json(snapshot_path)

        state_file = str(self.session_path / 'agent_state.json')
        agent_state = AgentState(state_file=state_file)
        agent_state.restore_from_snapshot(snapshot)
        agent_state.save()
        set_state(agent_state)

        broker = MockBroker(
            snapshot.get('cash', self.start_cash),
            slippage_bps=settings.slippage_base_bps,
            slippage_impact_coeff=settings.slippage_impact_coeff,
            min_entry_rr_ratio=settings.min_entry_rr_ratio,
            atr_stop_multiplier=settings.atr_stop_multiplier,
        )
        for ticker, pos_data in snapshot.get('positions', {}).items():
            broker.positions[ticker] = pos_data
        broker.peak_value = snapshot.get('peak_value', self.start_cash)
        # Restore pending orders
        for po in snapshot.get('pending_orders', []):
            from providers.mock_broker import PendingOrder
            broker.pending_orders.append(PendingOrder(**po))

        resume_after = snapshot.get('final_date', '')
        self.sim_dates = [d for d in self.sim_dates if d > resume_after]
        self.sim_days = len(self.sim_dates)
        return agent_state, broker

    # ------------------------------------------------------------------
    # Prompt dump wrappers
    # ------------------------------------------------------------------

    def _install_prompt_dumpers(self, agent):
        """Monkey-patch prompt builders to dump prompts to disk."""
        cls = type(agent)
        self._orig_build_eod = cls._build_eod_prompt
        self._orig_build_morning = cls._build_morning_prompt
        self._orig_build_intraday = cls._build_intraday_prompt
        self._prompt_counter = {'eod': 0, 'morning': 0, 'intraday': 0}

        dump_dir = self.dump_dir

        def _dumping_eod(self_agent, *a, **kw):
            prompt = self._orig_build_eod(self_agent, *a, **kw)
            sim_date = getattr(self_agent, '_sim_date', 'unknown')
            self._prompt_counter['eod'] += 1
            path = dump_dir / f"eod_{sim_date}.md"
            path.write_text(
                f"# EOD Prompt — {sim_date}\nLength: {len(prompt):,} chars\n\n---\n\n{prompt}\n",
                encoding="utf-8",
            )
            _log(f"  [DUMP] EOD prompt → {path.name} ({len(prompt):,} chars)")
            return prompt

        def _dumping_morning(self_agent, *a, **kw):
            prompt = self._orig_build_morning(self_agent, *a, **kw)
            sim_date = getattr(self_agent, '_sim_date', 'unknown')
            self._prompt_counter['morning'] += 1
            path = dump_dir / f"morning_{sim_date}.md"
            path.write_text(
                f"# MORNING Prompt — {sim_date}\nLength: {len(prompt):,} chars\n\n---\n\n{prompt}\n",
                encoding="utf-8",
            )
            _log(f"  [DUMP] MORNING prompt → {path.name} ({len(prompt):,} chars)")
            return prompt

        def _dumping_intraday(self_agent, *a, **kw):
            prompt = self._orig_build_intraday(self_agent, *a, **kw)
            sim_date = getattr(self_agent, '_sim_date', 'unknown')
            self._prompt_counter['intraday'] += 1
            path = dump_dir / f"intraday_{sim_date}.md"
            path.write_text(
                f"# INTRADAY Prompt — {sim_date}\nLength: {len(prompt):,} chars\n\n---\n\n{prompt}\n",
                encoding="utf-8",
            )
            _log(f"  [DUMP] INTRADAY prompt → {path.name} ({len(prompt):,} chars)")
            return prompt

        cls._build_eod_prompt = _dumping_eod
        cls._build_morning_prompt = _dumping_morning
        cls._build_intraday_prompt = _dumping_intraday

    def _uninstall_prompt_dumpers(self, agent):
        """Restore original prompt builders."""
        cls = type(agent)
        cls._build_eod_prompt = self._orig_build_eod
        cls._build_morning_prompt = self._orig_build_morning
        cls._build_intraday_prompt = self._orig_build_intraday

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    def run(self) -> dict:
        global _log_fh

        settings = get_settings()
        phase = 'simulation' if self.snapshot else 'backtest'

        # Detect resume (prior session data exists, no snapshot arg)
        snapshot_path = self.session_path / 'snapshot.json'
        state_file_path = self.session_path / 'agent_state.json'
        strands_dir = self.session_path / 'strands_sessions'
        has_prior = not self.snapshot and (strands_dir.exists() or state_file_path.exists())
        resuming = False

        if has_prior:
            print(f"\nExisting session data found in: {self.session_path}/")
            answer = input("Clear previous session data and start fresh? [y/N] ").strip().lower()
            if answer == 'y':
                import shutil
                for p in [strands_dir, state_file_path, self.session_path / 'days']:
                    if p.exists():
                        if p.is_dir():
                            shutil.rmtree(p)
                        else:
                            p.unlink()
                        print(f"  Cleared: {p}")
                print()
            else:
                resuming = snapshot_path.exists()
                print("  Keeping existing session data (resuming).\n")

        log_path = self.session_path / 'run.log'
        _log_fh = open(log_path, 'a', encoding='utf-8')

        # Save session metadata
        existing_meta = self.store.load_meta(self.session_id) or {}
        existing_meta.update({
            'session_id': self.session_id,
            'mode': 'backtest',
            'phase': phase,
            'status': 'running',
            'start_date': self.sim_dates[0],
            'end_date': self.sim_dates[-1],
            'sim_days': self.sim_days,
            'start_cash': self.start_cash,
            'model_id': settings.bedrock_model_id,
            'enable_playbook': settings.enable_playbook,
            'extended_thinking': settings.extended_thinking_enabled,
        })
        if self.snapshot:
            existing_meta['snapshot_from'] = self.snapshot.get('session_id')
        self.store.save_meta(self.session_id, existing_meta)

        # Initialize state
        if resuming:
            agent_state, broker = self._init_resume(settings)
            if not self.sim_dates:
                _log("All dates already processed. Nothing to do.")
                return {}
            _log(f"\n{'='*70}")
            _log(f"  BACKTEST (RESUME)")
            _log(f"  Session: {self.session_id}")
            _log(f"  Remaining: {self.sim_days} days ({self.sim_dates[0]} -> {self.sim_dates[-1]})")
            _log(f"  Portfolio: ${broker.portfolio_value:,.0f} | "
                 f"Positions: {list(broker.positions.keys()) or 'none'}")
            _log(f"{'='*70}\n")
            start_value = broker.portfolio_value
        elif self.snapshot:
            agent_state, broker = self._init_from_snapshot(settings)
            start_value = broker.portfolio_value
            snap_positions = list(broker.positions.keys())
            _log(f"\n{'='*70}")
            _log(f"  BACKTEST (from snapshot)")
            _log(f"  Session: {self.session_id}")
            _log(f"  Snapshot: {self.snapshot['session_id']} ({self.snapshot['final_date']})")
            _log(f"  Days: {self.sim_days} ({self.sim_dates[0]} -> {self.sim_dates[-1]})")
            _log(f"  Starting: ${start_value:,.0f} | "
                 f"{len(snap_positions)} positions: {', '.join(snap_positions) or 'none'}")
            _log(f"{'='*70}\n")
        else:
            agent_state, broker = self._init_cold(settings)
            start_value = self.start_cash
            _log(f"\n{'='*70}")
            _log(f"  BACKTEST (cold start)")
            _log(f"  Session: {self.session_id}")
            _log(f"  Days: {self.sim_days} | Cash: ${self.start_cash:,.0f}")
            _log(f"  Date range: {self.sim_dates[0]} -> {self.sim_dates[-1]}")
            _log(f"  Model: {settings.bedrock_model_id}")
            if settings.extended_thinking_enabled:
                _log(f"  Extended thinking: ON (budget={settings.extended_thinking_budget})")
            if self.dump_prompts:
                _log(f"  Prompt dumps: {self.dump_dir}")
            _log(f"{'='*70}\n")

        # Create agent
        settings.session_dir = str(self.session_path / 'strands_sessions')
        from agents.portfolio_agent import PortfolioAgent
        agent = PortfolioAgent(
            settings=settings, portfolio_state=agent_state,
            provider=self.provider, broker=broker,
        )
        agent._session_id = self.session_id

        if self.dump_prompts:
            self._install_prompt_dumpers(agent)

        clock = SimClock(self.sim_dates)
        daily_log: list[dict] = []
        prev_day_news: dict | None = None

        # Pre-load hourly bars once
        hourly_bars = self.provider.get_bars(
            self.provider.available_symbols, timeframe='hour',
        )

        try:
            for day_num in range(self.sim_days):
                sim_date = clock.today
                bars = self.provider.get_bars(
                    self.provider.available_symbols,
                    end=datetime.strptime(sim_date, '%Y-%m-%d'),
                )
                prev_date = clock.yesterday if day_num > 0 else sim_date
                prev_eod_time = datetime.strptime(prev_date, '%Y-%m-%d').replace(
                    hour=21, tzinfo=timezone.utc,
                )

                try:
                    day_result = run_one_day(
                        agent, agent_state, broker, sim_date, bars, hourly_bars,
                        day_num + 1, daily_log,
                        provider=self.provider, sector_map=self.sector_map,
                        store=self.store, session_id=self.session_id,
                        start_cash=start_value,
                        data_fn=self._fetch_data,
                        total_days=self.sim_days,
                        prev_day_news=prev_day_news, prev_eod_time=prev_eod_time,
                        log_fn=_log, phase=phase,
                        is_first_day=(day_num == 0 and not self.snapshot and not resuming),
                        is_last_day=(day_num == self.sim_days - 1),
                    )
                except SessionStoppedError:
                    _log("Backtest stopped by user.")
                    return {}

                prev_day_news = day_result.get('_news_data')
                daily_log.append(day_result)
                if not clock.advance():
                    break
        finally:
            if self.dump_prompts:
                self._uninstall_prompt_dumpers(agent)

        # Save snapshot
        snapshot = self._build_snapshot(broker, agent_state, daily_log)
        self.store.save_snapshot(self.session_id, snapshot)

        summary = self._build_summary(daily_log, broker, start_value)
        self._print_summary(summary)
        self.store.save_summary(self.session_id, summary)
        self.store.update_status(self.session_id, 'completed')
        _log(f"Results saved to: {self.session_path}/")

        if self.dump_prompts:
            _log(f"Prompts dumped: EOD={self._prompt_counter['eod']}, "
                 f"MORNING={self._prompt_counter['morning']}, "
                 f"INTRADAY={self._prompt_counter['intraday']}")

        if _log_fh:
            _log_fh.close()
        return summary

    # ------------------------------------------------------------------
    # Data fetch (single path: store cache -> fixture -> live)
    # ------------------------------------------------------------------

    def _fetch_data(self, sim_date: str, tickers: list[str]) -> tuple[dict, dict]:
        """Fetch news + earnings for a given day. Returns (news_data, earnings_map).

        Lookup order (first hit wins, no fallback chains):
          1. Store cache (session-specific, from a prior run of this session)
          2. Polygon fixture files (pre-downloaded via refresh_news.py)
          3. Live API fetch (Polygon news + yfinance earnings)
        """
        from tools.sentiment.news import score_news_for_window

        eod_ref = datetime.strptime(sim_date, '%Y-%m-%d').replace(
            hour=21, tzinfo=timezone.utc,
        )

        # 1. Store cache
        cached = self.store.load_cache(self.session_id, sim_date)
        if cached:
            articles = cached.get('news_articles')
            if articles is not None:
                news_data = score_news_for_window(articles, eod_ref)
            else:
                news_data = cached.get('news', {})
            earnings_map = {}
            for entry in cached.get('earnings', {}).get('upcoming_earnings', []):
                t, d = entry.get('ticker', ''), entry.get('days_until')
                if t and d is not None:
                    earnings_map[t] = int(d)
            _log(f"  [CACHE] loaded {sim_date}")
            return news_data, earnings_map

        # 2. Polygon fixture files
        fixture_path = Path('backtest/fixtures/polygon/news') / f'day_{sim_date}.json'
        if fixture_path.exists():
            articles = load_json(fixture_path)
        else:
            # 3. Live API fetch
            articles = self._fetch_news_articles(tickers, sim_date)
        news_data = score_news_for_window(articles, eod_ref)

        # Fetch earnings
        earnings_data = {'blackout_tickers': [], 'upcoming_earnings': [], 'recent_earnings': []}
        earnings_map = {}
        try:
            from datetime import date as _date
            from tools.sentiment.earnings import screen_earnings_events
            earnings_data = screen_earnings_events(tickers, as_of=_date.fromisoformat(sim_date))
            for entry in earnings_data.get('upcoming_earnings', []):
                t, d = entry.get('ticker', ''), entry.get('days_until')
                if t and d is not None:
                    earnings_map[t] = int(d)
        except Exception as exc:
            logger.warning("Earnings fetch failed: %s", exc)

        # Cache for reproducibility
        self.store.save_cache(self.session_id, sim_date, {
            'date': sim_date,
            'tickers': tickers,
            'news_articles': articles,
            'earnings': earnings_data,
        })

        news_count = sum(1 for t, a in articles.items() if a)
        _log(f"  [API] news={news_count}/{len(tickers)} | earnings={len(earnings_map)}")
        return news_data, earnings_map

    def _fetch_news_articles(self, tickers: list[str], sim_date: str) -> dict[str, list[dict]]:
        """Fetch raw articles from Polygon and return compact per-ticker lists."""
        from config.settings import get_settings
        from tools.sentiment.news import _fetch_polygon_news, compact_articles, clear_article_cache

        clear_article_cache()
        api_key = get_settings().polygon_api_key
        if not api_key:
            return {}

        as_of_dt = datetime.strptime(sim_date, '%Y-%m-%d').replace(hour=21, tzinfo=timezone.utc)
        result: dict[str, list[dict]] = {}

        for i, ticker in enumerate(tickers):
            try:
                articles = _fetch_polygon_news(ticker, 24, api_key, as_of=as_of_dt)
                if articles:
                    result[ticker] = compact_articles(articles)
            except Exception as exc:
                logger.warning("News fetch %s: %s", ticker, exc)
            if i < len(tickers) - 1:
                time.sleep(NEWS_SLEEP)

        return result

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def _build_snapshot(
        self,
        broker: MockBroker,
        agent_state: AgentState,
        daily_log: list[dict],
    ) -> dict:
        """Build snapshot for resume or simulation continuation."""
        # Sync broker state into agent_state
        agent_state.positions = {
            t: broker.positions[t] for t in broker.positions
        }
        agent_state.cash = broker.cash
        agent_state.portfolio_value = broker.portfolio_value
        agent_state.peak_value = broker.peak_value

        snapshot = agent_state.to_snapshot()

        # Persist broker pending orders for resume
        snapshot['pending_orders'] = [
            asdict(o) for o in broker.pending_orders
        ]

        snapshot.update({
            'session_id': self.session_id,
            'final_date': self.sim_dates[-1],
            'start_date': self.sim_dates[0],
            'sim_days': len(self.sim_dates),
            'daily_returns': [
                {
                    'date': d['date'],
                    'portfolio_value': d['portfolio_value'],
                    'daily_return_pct': d['daily_return_pct'],
                    'spy_return_pct': d['spy_return_pct'],
                    'excess_return_pct': d['excess_return_pct'],
                    'positions': d['positions'],
                    'regime': d['regime'],
                }
                for d in daily_log
            ],
            'regime_history': [
                {'date': d['date'], 'regime': d['regime']}
                for d in daily_log
            ],
        })

        pm_session_path = self.session_path / 'strands_sessions' / f"{self.session_id}.json"
        snapshot['pm_session_file'] = str(pm_session_path) if pm_session_path.exists() else None

        return snapshot

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _build_summary(self, daily_log: list[dict], broker: MockBroker, start_value: float) -> dict:
        if not daily_log:
            return {}
        n = len(daily_log)
        end_value = broker.portfolio_value
        total_return = (end_value - start_value) / start_value

        daily_returns = [d['daily_return_pct'] / 100.0 for d in daily_log]
        spy_returns = [d['spy_return_pct'] / 100.0 for d in daily_log]

        spy_cum = 1.0
        for r in spy_returns:
            spy_cum *= (1 + r)

        mean_ret = sum(daily_returns) / n if n else 0.0
        daily_vol = math.sqrt(sum((r - mean_ret) ** 2 for r in daily_returns) / (n - 1)) if n > 1 else 0.0

        pv_series = [d['portfolio_value'] for d in daily_log]
        peak = pv_series[0]
        max_dd = 0.0
        for pv in pv_series:
            if pv > peak:
                peak = pv
            dd = (peak - pv) / peak
            if dd > max_dd:
                max_dd = dd

        spy_peak = 1.0
        spy_max_dd = 0.0
        spy_cum_dd = 1.0
        for r in spy_returns:
            spy_cum_dd *= (1 + r)
            if spy_cum_dd > spy_peak:
                spy_peak = spy_cum_dd
            dd = (spy_peak - spy_cum_dd) / spy_peak
            if dd > spy_max_dd:
                spy_max_dd = dd

        invested_ratios = []
        for d in daily_log:
            pv = d['portfolio_value']
            cash = d['cash']
            if pv > 0:
                invested_ratios.append(1.0 - cash / pv)
        avg_invested_pct = sum(invested_ratios) / len(invested_ratios) if invested_ratios else 0.0

        phase = 'simulation' if self.snapshot else 'backtest'
        summary = {
            'session_id': self.session_id,
            'phase': phase,
            'start_date': self.sim_dates[0],
            'end_date': self.sim_dates[-1],
            'sim_days': n,
            'start_value': round(start_value, 2),
            'end_value': round(end_value, 2),
            'total_return_pct': round(total_return * 100, 2),
            'spy_total_return_pct': round((spy_cum - 1.0) * 100, 2),
            'max_drawdown_pct': round(max_dd * 100, 2),
            'spy_max_drawdown_pct': round(spy_max_dd * 100, 2),
            'sharpe_ratio': round(mean_ret / daily_vol * math.sqrt(252), 3) if daily_vol > 0 else 0.0,
            'avg_invested_pct': round(avg_invested_pct * 100, 1),
            'final_positions': list(broker.positions.keys()),
            'final_position_count': len(broker.positions),
            'daily_log': daily_log,
        }
        if self.snapshot:
            summary['snapshot_from'] = self.snapshot.get('session_id')
        return summary

    def _print_summary(self, summary: dict) -> None:
        label = "SIMULATION" if self.snapshot else "BACKTEST"
        _log(f"\n{'='*70}")
        _log(f"  {label} COMPLETE — {summary.get('session_id', '')}")
        _log(f"{'='*70}")
        _log(f"  Period: {summary['start_date']} -> {summary['end_date']} ({summary['sim_days']} days)")
        _log(f"  Total Return: {summary['total_return_pct']:+.2f}%  (SPY: {summary['spy_total_return_pct']:+.2f}%)")
        _log(f"  Max Drawdown: {summary['max_drawdown_pct']:.2f}%  (SPY: {summary['spy_max_drawdown_pct']:.2f}%)")
        _log(f"  Sharpe: {summary['sharpe_ratio']:.3f}")
        _log(f"  Avg Invested: {summary['avg_invested_pct']:.1f}%")
        _log(f"  Start: ${summary['start_value']:,.2f}  End: ${summary['end_value']:,.2f}")
        if summary.get('final_positions'):
            _log(f"  Open Positions: {', '.join(summary['final_positions'])}")
        _log(f"{'='*70}\n")


# ---------------------------------------------------------------------------
# Legacy aliases for callers that import old names
# ---------------------------------------------------------------------------
Preconditioner = Backtest
Simulator = Backtest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run backtest (cold start or from snapshot)")
    parser.add_argument("--days", type=int, default=20, help="Number of trading days")
    parser.add_argument("--start-date", type=str, default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=str, default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--start-cash", type=float, default=100_000, help="Starting cash")
    parser.add_argument("--session", type=str, default=None, help="Session ID")
    parser.add_argument("--snapshot", type=str, default=None, help="Path to snapshot.json (resume from prior run)")
    parser.add_argument("--model", type=str, default=None, help="Bedrock model ID override")
    parser.add_argument("--dump-prompts", action="store_true", help="Dump all cycle prompts to disk")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    if args.model:
        get_settings.cache_clear()
        os.environ['BEDROCK_MODEL_ID'] = args.model
        get_settings.cache_clear()
        print(f"Model override: {args.model}")

    bt = Backtest(
        sim_days=args.days,
        start_date=args.start_date,
        end_date=args.end_date,
        start_cash=args.start_cash,
        session_id=args.session,
        snapshot_path=args.snapshot,
        dump_prompts=args.dump_prompts,
    )
    bt.run()


if __name__ == "__main__":
    main()
