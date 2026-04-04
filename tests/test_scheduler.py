"""
tests/test_scheduler.py — Unit tests for TradingScheduler.

The BlockingScheduler is never started in tests — only job registration
and callback behaviour are exercised.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scheduler.trading_scheduler import TradingScheduler, _parse_time
from config.settings import Settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**overrides) -> Settings:
    """Return a Settings instance with optional field overrides."""
    defaults = {
        'morning_signal_time': '09:00',
        'intraday_signal_time': '10:30',
        'eod_signal_time': '16:00',
        'timezone': 'America/New_York',
    }
    defaults.update(overrides)
    s = MagicMock(spec=Settings)
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


def _make_scheduler(
    morning='09:00', intraday='10:30', eod='16:00',
) -> TradingScheduler:
    """Build a TradingScheduler with a mocked orchestrator (scheduler NOT started)."""
    settings = _make_settings(
        morning_signal_time=morning,
        intraday_signal_time=intraday,
        eod_signal_time=eod,
    )
    orchestrator = MagicMock()
    portfolio_state = MagicMock()
    return TradingScheduler(
        settings=settings,
        orchestrator=orchestrator,
        portfolio_state=portfolio_state,
    )


# ---------------------------------------------------------------------------
# _parse_time
# ---------------------------------------------------------------------------

def test_parse_time_basic():
    h, m = _parse_time('13:30')
    assert h == 13
    assert m == 30


def test_parse_time_eod():
    h, m = _parse_time('16:30')
    assert h == 16
    assert m == 30


def test_parse_time_midnight():
    h, m = _parse_time('00:00')
    assert h == 0
    assert m == 0


def test_parse_time_leading_zeros():
    h, m = _parse_time('09:05')
    assert h == 9
    assert m == 5


def test_parse_time_returns_ints():
    h, m = _parse_time('10:15')
    assert isinstance(h, int)
    assert isinstance(m, int)


# ---------------------------------------------------------------------------
# _register_jobs
# ---------------------------------------------------------------------------

def test_register_jobs_registers_three_jobs():
    sched = _make_scheduler()
    jobs = sched.scheduler.get_jobs()
    assert len(jobs) == 3


def test_register_jobs_morning_id():
    sched = _make_scheduler()
    job_ids = {j.id for j in sched.scheduler.get_jobs()}
    assert 'morning_cycle' in job_ids


def test_register_jobs_intraday_id():
    sched = _make_scheduler()
    job_ids = {j.id for j in sched.scheduler.get_jobs()}
    assert 'intraday_cycle' in job_ids


def test_register_jobs_eod_signal_id():
    sched = _make_scheduler()
    job_ids = {j.id for j in sched.scheduler.get_jobs()}
    assert 'eod_signal_cycle' in job_ids


def test_register_jobs_morning_name():
    sched = _make_scheduler()
    names = {j.name for j in sched.scheduler.get_jobs()}
    assert any('Morning' in n for n in names)


def test_register_jobs_intraday_name():
    sched = _make_scheduler()
    names = {j.name for j in sched.scheduler.get_jobs()}
    assert any('Intraday' in n for n in names)


def test_register_jobs_eod_name():
    sched = _make_scheduler()
    names = {j.name for j in sched.scheduler.get_jobs()}
    assert any('EOD' in n for n in names)


def test_register_jobs_no_research_jobs():
    """Research is now integrated inline into MORNING and EOD pipelines."""
    sched = _make_scheduler()
    job_ids = {j.id for j in sched.scheduler.get_jobs()}
    assert 'morning_research_cycle' not in job_ids
    assert 'eod_research_cycle' not in job_ids
def test_run_intraday_cycle_calls_orchestrator():
    sched = _make_scheduler()
    sched._run_intraday_cycle()
    sched.orchestrator.run_trading_cycle.assert_called_once_with('INTRADAY')


def test_run_intraday_cycle_returns_result():
    sched = _make_scheduler()
    sched.orchestrator.run_trading_cycle.return_value = {'status': 'ok'}
    sched._run_intraday_cycle()  # should not raise


def test_run_intraday_cycle_handles_exception():
    """Exceptions inside the cycle must be swallowed (scheduler stays alive)."""
    sched = _make_scheduler()
    sched.orchestrator.run_trading_cycle.side_effect = RuntimeError('oops')
    sched._run_intraday_cycle()  # must not propagate


# ---------------------------------------------------------------------------
# _run_morning_cycle callback
# ---------------------------------------------------------------------------

def test_run_morning_cycle_calls_orchestrator():
    sched = _make_scheduler()
    sched._run_morning_cycle()
    sched.orchestrator.run_trading_cycle.assert_called_once_with('MORNING')


def test_run_morning_cycle_handles_exception():
    sched = _make_scheduler()
    sched.orchestrator.run_trading_cycle.side_effect = RuntimeError('fail')
    sched._run_morning_cycle()  # must not propagate


def test_run_morning_cycle_returns_result():
    sched = _make_scheduler()
    sched.orchestrator.run_trading_cycle.return_value = {'orders_placed': 2}
    sched._run_morning_cycle()  # should not raise


# ---------------------------------------------------------------------------
# _run_eod_signal_cycle callback
# ---------------------------------------------------------------------------

def test_run_eod_signal_cycle_calls_orchestrator():
    sched = _make_scheduler()
    sched._run_eod_signal_cycle()
    sched.orchestrator.run_trading_cycle.assert_called_once_with('EOD_SIGNAL')


def test_run_eod_signal_cycle_handles_exception():
    sched = _make_scheduler()
    sched.orchestrator.run_trading_cycle.side_effect = ValueError('bad state')
    sched._run_eod_signal_cycle()  # must not propagate


def test_run_eod_signal_cycle_returns_result():
    sched = _make_scheduler()
    sched.orchestrator.run_trading_cycle.return_value = {'signals': 5}
    sched._run_eod_signal_cycle()  # should not raise


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

def test_stop_calls_scheduler_shutdown():
    sched = _make_scheduler()
    with patch.object(sched.scheduler, 'shutdown') as mock_shutdown:
        sched.stop()
    mock_shutdown.assert_called_once_with(wait=False)
